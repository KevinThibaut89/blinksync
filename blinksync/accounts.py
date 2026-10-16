"""Blink account storage and login.

Every process (each go2rtc-launched stream, the web UI, the CLI) logs in from
the saved refresh token, and Blink rotates that token on every refresh. To
keep two cameras starting at once from invalidating each other's token, the
whole load → refresh → save sequence runs under an exclusive file lock.
"""

from __future__ import annotations

import asyncio
import contextvars
import fcntl
import json
import re
from contextlib import asynccontextmanager
from pathlib import Path

from aiohttp import ClientError, ClientSession
from blinkpy.auth import Auth, BlinkTwoFARequiredError
from blinkpy.blinkpy import Blink
from blinkpy.camera import BlinkCamera

from .settings import data_dir, write_private

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

# blinkpy downloads every camera's latest thumbnail *and* motion clip on each
# login. We never use the clips, and a stream process doesn't need the
# thumbnails either; skipping them saves seconds on every stream start.
_fetch_thumbnails = contextvars.ContextVar("fetch_thumbnails", default=True)
_original_get_thumbnail = BlinkCamera.get_thumbnail


async def _get_thumbnail(self, url=None):
    if not _fetch_thumbnails.get():
        return None
    return await _original_get_thumbnail(self, url)


async def _no_video_clip(self, url=None):
    return None


BlinkCamera.get_thumbnail = _get_thumbnail
BlinkCamera.get_video_clip = _no_video_clip


class AccountError(Exception):
    """Login or account lookup failed."""


class ReauthRequired(AccountError):
    """Saved token no longer works; the account must be added again (with 2FA)."""


NETWORK_ERRORS = (ClientError, OSError, asyncio.TimeoutError)


def unreachable(err: Exception) -> AccountError:
    return AccountError(f"Couldn't reach Blink: {err or type(err).__name__}")


def slugify(text: str, fallback: str = "camera") -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")
    return slug or fallback


def valid_account_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))


def camera_summary(camera) -> dict:
    return {
        "id": str(camera.camera_id),
        "name": camera.name,
        "kind": camera.camera_type or "default",
        "model": camera.product_type,
        "battery": camera.battery,
        "sync_module": camera.sync.name,
    }


class AccountStore:
    def __init__(self, root: Path | None = None):
        self.dir = (root or data_dir()) / "accounts"

    def names(self) -> list[str]:
        if not self.dir.exists():
            return []
        return sorted(
            p.stem for p in self.dir.glob("*.json") if not p.stem.endswith(".cameras")
        )

    def exists(self, name: str) -> bool:
        return self.creds_path(name).exists()

    def creds_path(self, name: str) -> Path:
        return self.dir / f"{name}.json"

    def cameras_path(self, name: str) -> Path:
        return self.dir / f"{name}.cameras.json"

    def lock_path(self, name: str) -> Path:
        return self.dir / f".{name}.lock"

    def cameras(self, name: str) -> list[dict]:
        path = self.cameras_path(name)
        if not path.exists():
            return []
        return json.loads(path.read_text())

    def username(self, name: str) -> str:
        try:
            return json.loads(self.creds_path(name).read_text()).get("username") or ""
        except (OSError, ValueError):
            return ""

    def save(self, name: str, blink: Blink) -> None:
        write_private(
            self.creds_path(name), json.dumps(blink.auth.login_attributes, indent=2)
        )
        cameras = [camera_summary(c) for c in blink.cameras.values()]
        cameras.sort(key=lambda c: c["name"].lower())
        write_private(self.cameras_path(name), json.dumps(cameras, indent=2))

    def delete(self, name: str) -> None:
        for path in (self.creds_path(name), self.cameras_path(name), self.lock_path(name)):
            path.unlink(missing_ok=True)

    @asynccontextmanager
    async def locked(self, name: str):
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.lock_path(name), "a") as fh:
            await asyncio.to_thread(fcntl.flock, fh, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)


@asynccontextmanager
async def open_blink(store: AccountStore, name: str, thumbnails: bool = True):
    """Log in with the saved token and yield a ready `Blink` instance.

    With `thumbnails`, each camera's `image_from_cache()` holds its latest
    thumbnail afterwards.
    """
    if not store.exists(name):
        raise AccountError(f"No Blink account named '{name}'. Add it in the web UI.")
    _fetch_thumbnails.set(thumbnails)
    async with ClientSession() as session:
        async with store.locked(name):
            data = json.loads(store.creds_path(name).read_text())
            blink = Blink(session=session)
            blink.auth = Auth(data, no_prompt=True, session=session)
            try:
                ok = await blink.start()
            except BlinkTwoFARequiredError as err:
                raise ReauthRequired(
                    f"Blink wants a new 2FA login for account '{name}'."
                ) from err
            except NETWORK_ERRORS as err:
                raise unreachable(err) from err
            if not ok:
                raise AccountError(f"Blink login failed for account '{name}'.")
            store.save(name, blink)
        yield blink


class PendingLogin:
    """A first-time login that may pause for a 2FA code.

    Used by both the web UI (which keeps it between two requests) and the CLI.
    """

    def __init__(self, store: AccountStore, name: str, username: str, password: str):
        self.store = store
        self.name = name
        self._session = ClientSession()
        self.blink = Blink(session=self._session)
        self.blink.auth = Auth(
            {"username": username, "password": password},
            no_prompt=True,
            session=self._session,
        )

    async def begin(self) -> bool:
        """Start the login. Returns True if a 2FA code is needed."""
        try:
            ok = await self.blink.start()
        except BlinkTwoFARequiredError:
            return True
        except NETWORK_ERRORS as err:
            await self.close()
            raise unreachable(err) from err
        if not ok:
            await self.close()
            raise AccountError("Blink rejected the email or password.")
        await self._finish()
        return False

    async def verify(self, code: str) -> None:
        try:
            ok = await self.blink.send_2fa_code(code.strip())
        except NETWORK_ERRORS as err:
            raise unreachable(err) from err
        if not ok:
            raise AccountError("Blink rejected the 2FA code.")
        await self._finish()

    async def _finish(self) -> None:
        async with self.store.locked(self.name):
            self.store.save(self.name, self.blink)
        await self.close()

    async def close(self) -> None:
        if not self._session.closed:
            await self._session.close()
