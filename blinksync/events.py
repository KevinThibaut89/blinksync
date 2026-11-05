"""Motion events: find new Blink clips, save them, and tell Home Assistant.

This module polls Blink for clips Blink itself saved, which only exist with a
Sync Module 2 USB stick or a Blink plan; it's off unless `poll_seconds` is
set. Without either, motion clips come from recorder.py instead. The event
log and clips folder here are shared by both.

Blink has no push API we can use, so the web service checks every
`poll_seconds`. A motion event is a new clip, either on the Sync Module 2's
USB stick or in Blink's cloud.

One `Blink` session per account stays open between checks instead of logging
in every time. Stream processes keep rotating the saved token, so each check
starts with `sync_tokens`, which adopts the newest one from the account file.

Each new clip is downloaded to the clips folder with a thumbnail, recorded in
`<clips>/.events.json`, and passed to `on_event` (the web app uses it to
update the camera's snapshot and POST to the Home Assistant webhook).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import time
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import ClientError, ClientSession, ClientTimeout
from blinkpy import api

from . import go2rtc
from .accounts import (
    AccountStore,
    ReauthRequired,
    slugify,
    start_blink,
    sync_tokens,
)
from .settings import Settings, clips_dir, write_private

_LOGGER = logging.getLogger(__name__)

# A clip shows up in Blink's lists only after it finished recording and, for
# USB clips, after the Sync Module indexed it. Look back this far past the last
# successful check so a slow clip isn't missed.
WINDOW = 15 * 60
# Clips older than this when we first see them (e.g. after blinkSync was down)
# are still saved, but don't trigger a notification.
NOTIFY_WINDOW = 30 * 60
# Never look back further than this, and forget seen clips after twice as long.
MAX_LOOKBACK = 24 * 3600
SEEN_TTL = 2 * MAX_LOOKBACK
MAX_EVENTS = 200
# Downloads per check; a backlog is worked off over the next checks.
MAX_DOWNLOADS = 10
# Log in fresh now and then so new or renamed cameras are picked up.
SESSION_MAX_AGE = 6 * 3600
MAX_BACKOFF = 15 * 60
PRUNE_EVERY = 10 * 60


@dataclass
class Clip:
    """A clip Blink lists, before it's downloaded."""

    key: str  # unique per account: "local:<sync id>:<clip id>" or "cloud:<media id>"
    camera: str  # camera name, as in blink.cameras
    created: float  # epoch seconds
    source: str  # "local" (Sync Module USB) or "cloud"
    item: Any = None  # blinkpy LocalStorageMediaItem, or the cloud media dict
    sync: Any = None  # the sync module, for local clips


@dataclass
class MotionEvent:
    id: str
    account: str
    camera: str
    camera_id: str
    stream: str  # go2rtc stream name; also used for file names
    time: str  # ISO 8601, UTC
    source: str
    clip: str = ""  # path relative to the clips folder; "" if the download failed
    thumbnail: str = ""
    late: bool = False  # found long after it happened; no notification

    @property
    def created(self) -> float:
        return parse_time(self.time)


@dataclass
class AccountStatus:
    last_ok: float = 0
    error: str = ""
    failures: int = 0
    retry_at: float = 0
    paused: bool = False  # needs a new sign-in; don't retry until reload()


def parse_time(value) -> float:
    """Blink timestamp (ISO 8601 string or datetime) → epoch seconds."""
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# What we've seen and saved
# --------------------------------------------------------------------------


class EventLog:
    """Seen clip keys and recent events, kept in `<clips>/.events.json`."""

    def __init__(self, root: Path):
        self.root = root
        self.path = root / ".events.json"
        self.seen: dict[str, dict[str, float]] = {}
        self.last_ok: dict[str, float] = {}
        self.events: list[dict] = []
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return
        self.seen = raw.get("seen", {})
        self.last_ok = raw.get("last_ok", {})
        self.events = raw.get("events", [])

    def save(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        data = {"seen": self.seen, "last_ok": self.last_ok, "events": self.events}
        write_private(self.path, json.dumps(data))

    def since(self, account: str, now: float) -> float:
        """Oldest clip time worth asking Blink about."""
        last_ok = self.last_ok.get(account, now)
        return max(min(last_ok, now) - WINDOW, now - MAX_LOOKBACK)

    def new_clips(self, account: str, clips: list[Clip], now: float) -> list[Clip]:
        """Clips not handled yet, oldest first. Call `mark` once handled.

        The first check for an account only records what's already there, so
        installing blinkSync doesn't download and announce old clips.
        """
        seen = self.seen.get(account)
        if seen is None:
            self.seen[account] = {c.key: c.created for c in clips}
            return []
        for key, created in list(seen.items()):
            if created < now - SEEN_TTL:
                del seen[key]
        since = self.since(account, now)
        fresh = [c for c in clips if c.key not in seen and c.created > since]
        return sorted(fresh, key=lambda c: c.created)

    def mark(self, account: str, clip: Clip) -> None:
        self.seen.setdefault(account, {})[clip.key] = clip.created

    def add(self, event: MotionEvent) -> None:
        self.events.append(asdict(event))
        del self.events[:-MAX_EVENTS]

    def forget(self, account: str) -> None:
        self.seen.pop(account, None)
        self.last_ok.pop(account, None)

    def recent(self, account: str | None = None, limit: int = 20) -> list[MotionEvent]:
        known = {f.name for f in fields(MotionEvent)}
        events = [
            MotionEvent(**{k: v for k, v in e.items() if k in known})
            for e in reversed(self.events)
            if account is None or e.get("account") == account
        ]
        return events[:limit]


@dataclass
class SavedClip:
    """A clip file in the clips folder (whichever way it got there)."""

    rel: str  # e.g. garden/2026-11-05/132338_notification1793885018.mp4
    stream: str
    day: str
    mtime: float  # the clip's start time
    size: int
    thumbnail: str  # rel path of the .jpg next to it, or ""


def saved_clips(root: Path) -> list[SavedClip]:
    """Every clip on disk, newest first. Layout: <stream>/<day>/<name>.mp4."""
    clips = []
    if not root.is_dir():
        return clips
    for path in root.glob("*/*/*.mp4"):
        if path.name.startswith("."):
            continue  # a download or remux in progress
        st = path.stat()
        rel = path.relative_to(root)
        thumb = path.with_suffix(".jpg")
        clips.append(SavedClip(
            rel.as_posix(), rel.parts[0], rel.parts[1], st.st_mtime, st.st_size,
            thumb.relative_to(root).as_posix() if thumb.exists() else "",
        ))
    clips.sort(key=lambda c: c.mtime, reverse=True)
    return clips


def delete_clip(root: Path, rel: str) -> bool:
    """Delete a clip and its thumbnail. False if `rel` isn't a clip in `root`."""
    root = root.resolve()
    path = (root / rel).resolve()
    if not path.is_relative_to(root) or path.suffix != ".mp4" or not path.is_file():
        return False
    path.unlink()
    path.with_suffix(".jpg").unlink(missing_ok=True)
    for folder in (path.parent, path.parent.parent):
        if folder != root:
            try:
                folder.rmdir()  # only succeeds when empty
            except OSError:
                break
    return True


def prune(root: Path, keep_days: float, max_bytes: float) -> int:
    """Delete clips older than `keep_days`, then the oldest until under `max_bytes`."""
    if not root.exists():
        return 0
    files = []
    for path in root.rglob("*"):
        if path.is_file() and path.suffix in (".mp4", ".jpg"):
            st = path.stat()
            files.append((st.st_mtime, st.st_size, path))
    files.sort()
    total = sum(size for _, size, _ in files)
    cutoff = time.time() - keep_days * 86400
    removed = 0
    for mtime, size, path in files:
        if mtime >= cutoff and total <= max_bytes:
            break
        path.unlink(missing_ok=True)
        total -= size
        removed += 1
    for folder in sorted((p for p in root.rglob("*") if p.is_dir()), reverse=True):
        try:
            folder.rmdir()  # only succeeds when empty
        except OSError:
            pass
    return removed


# --------------------------------------------------------------------------
# Talking to Blink
# --------------------------------------------------------------------------


async def list_clips(blink, since: float) -> list[Clip]:
    """Clips newer than `since` on every Sync Module's USB stick and in the cloud."""
    clips: list[Clip] = []
    for sync in blink.sync.values():
        if not getattr(sync, "local_storage", False):
            continue
        if not await sync.update_local_storage_manifest():
            _LOGGER.warning("Couldn't read the USB clip list of %s", sync.name)
            continue
        for item in sync._local_storage["manifest"]:
            created = parse_time(item.created_at)
            if created > since:
                key = f"local:{sync.sync_id}:{item.id}"
                clips.append(Clip(key, item.name, created, "local", item, sync))

    response = await api.request_videos(blink, time=since, page=1)
    media = response.get("media") if isinstance(response, dict) else None
    for entry in media or []:
        try:
            if entry.get("deleted"):
                continue
            created = parse_time(entry["created_at"])
            clips.append(
                Clip(f"cloud:{entry['id']}", entry["device_name"], created, "cloud", entry)
            )
        except (KeyError, TypeError, ValueError):
            continue
    return clips


async def _get(blink, path: str) -> bytes | None:
    url = path if path.startswith("http") else f"{blink.urls.base_url}{path}"
    response = await api.http_get(blink, url=url, json=False)
    if response is None or response.status != 200:
        return None
    return await response.read()


async def download_clip(blink, clip: Clip, video: Path, thumb: Path) -> tuple[bool, bool]:
    """Save the clip to `video` (and, for cloud clips, its thumbnail to `thumb`).

    Returns (video saved, thumbnail saved).
    """
    data = thumb_data = None
    if clip.source == "local":
        item = clip.item
        # The manifest is rebuilt each check; clip URLs embed its id.
        item.url(clip.sync._local_storage["last_manifest_id"])
        # Ask the Sync Module to upload the clip to Blink, then fetch it.
        if await item.prepare_download(blink):
            for attempt in range(3):
                if data := await _get(blink, item.url()):
                    break
                await asyncio.sleep(3 * (attempt + 1))
    else:
        data = await _get(blink, clip.item["media"])
        if path := clip.item.get("thumbnail"):
            thumb_data = await _get(blink, path)
            if not thumb_data and not path.endswith(".jpg"):
                thumb_data = await _get(blink, f"{path}.jpg")
    for target, payload in ((video, data), (thumb, thumb_data)):
        if payload:
            write_file(target, payload, clip.created)
    return bool(data), bool(thumb_data)


async def latest_thumbnails(blink, cameras: set[str]) -> dict[str, bytes]:
    """Each camera's current Blink thumbnail (usually from its newest clip)."""
    images: dict[str, bytes] = {}
    try:
        await blink.get_homescreen()  # minis and doorbells read their info from it
        for name in cameras:
            camera = blink.cameras.get(name)
            if camera is None:
                continue
            sync = camera.sync
            info = await sync.get_camera_info(
                camera.camera_id, unique_info=sync.get_unique_info(name)
            )
            await camera.update(info, force_cache=True, expire_clips=False)
            if image := camera.image_from_cache():
                images[name] = image
    except Exception as err:
        _LOGGER.warning("Couldn't fetch new thumbnails: %r", err)
    return images


def clip_rel(event: "MotionEvent", suffix: str) -> str:
    """Where an event's files go, relative to the clips folder."""
    local = datetime.fromtimestamp(event.created).astimezone()
    short = event.id.rsplit(":", 1)[-1][-12:]
    name = f"{local:%H%M%S}_{event.source}{slugify(short, 'clip')}{suffix}"
    return f"{event.stream}/{local:%Y-%m-%d}/{name}"


def write_file(path: Path, data: bytes, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    os.utime(path, (mtime, mtime))  # pruning goes by clip time


# --------------------------------------------------------------------------
# Home Assistant
# --------------------------------------------------------------------------


def lan_ip() -> str:
    """This machine's address on the LAN (no packet is actually sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 80))
            return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"


def base_url(settings: Settings) -> str:
    if settings.public_url:
        return settings.public_url.rstrip("/")
    return f"http://{lan_ip()}:{settings.web_port}"


def webhook_payload(event: MotionEvent, settings: Settings) -> dict:
    """The JSON Home Assistant gets as `trigger.json`."""
    base, key = base_url(settings), settings.snapshot_key

    def link(rel: str) -> str | None:
        return f"{base}/clips/{rel}?key={key}" if rel else None

    return {
        "event": "motion",
        "camera": event.camera,
        "stream": event.stream,
        "account": event.account,
        "camera_id": event.camera_id,
        "time": event.time,
        "source": event.source,
        "clip_url": link(event.clip),
        "thumbnail_url": link(event.thumbnail),
        "snapshot_url": f"{base}/snapshot/{event.stream}.jpg?key={key}",
    }


async def send_webhook(url: str, payload: dict) -> bool:
    try:
        async with ClientSession(timeout=ClientTimeout(total=10)) as session:
            async with session.post(url, json=payload) as response:
                if response.status >= 400:
                    _LOGGER.warning("Home Assistant webhook returned HTTP %s", response.status)
                    return False
    except (ClientError, OSError, asyncio.TimeoutError) as err:
        _LOGGER.warning("Couldn't reach the Home Assistant webhook: %r", err)
        return False
    return True


# --------------------------------------------------------------------------
# The watcher
# --------------------------------------------------------------------------

OnEvent = Callable[[MotionEvent, "bytes | None"], Awaitable[None]]


class EventWatcher:
    def __init__(
        self, store: AccountStore, settings: Settings, on_event: OnEvent | None = None
    ):
        self.store = store
        self.settings = settings
        self.root = clips_dir(settings)
        self.log = EventLog(self.root)
        self.on_event = on_event
        self.status: dict[str, AccountStatus] = {}
        self._sessions: dict[str, tuple[ClientSession, Any, float]] = {}
        self._stale: set[str] = set()
        self._wake = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._pruned = 0.0

    @property
    def enabled(self) -> bool:
        return self.settings.poll_seconds > 0

    def start(self) -> None:
        """Run the loop: polling (if enabled) and deleting old clips."""
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for account in list(self._sessions):
            await self._drop(account)

    def reload(self, account: str) -> None:
        """The account was signed in again, refreshed or removed: start over."""
        self._stale.add(account)
        self.status.pop(account, None)
        if not self.store.exists(account):
            self.log.forget(account)
            self.log.save()
        self._wake.set()

    async def _run(self) -> None:
        while True:
            if self.enabled:
                await self.check_all()
            await self.maybe_prune()
            interval = self.settings.poll_seconds if self.enabled else PRUNE_EVERY
            try:
                await asyncio.wait_for(self._wake.wait(), interval)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()

    async def check_all(self) -> None:
        now = time.time()
        for account in self.store.names():
            status = self.status.setdefault(account, AccountStatus())
            if status.paused or now < status.retry_at:
                continue
            try:
                await self.check(account)
            except ReauthRequired as err:
                status.paused, status.error = True, str(err)
                _LOGGER.warning("%s Motion events paused until you sign in again.", err)
                await self._drop(account)
            except Exception as err:
                status.failures += 1
                status.error = str(err) or type(err).__name__
                delay = min(MAX_BACKOFF, self.settings.poll_seconds * 2 ** status.failures)
                status.retry_at = time.time() + delay
                _LOGGER.warning(
                    "Checking %s for motion clips failed (retry in %ds): %r",
                    account, delay, err, exc_info=_LOGGER.isEnabledFor(logging.DEBUG),
                )
                await self._drop(account)
            else:
                status.last_ok, status.error, status.failures = time.time(), "", 0

        for account in list(self._sessions):
            if not self.store.exists(account):
                await self._drop(account)
                self.status.pop(account, None)

    async def maybe_prune(self) -> None:
        """Delete old clips (recorded or downloaded) at most every PRUNE_EVERY."""
        if time.time() - self._pruned > PRUNE_EVERY:
            self._pruned = time.time()
            max_bytes = self.settings.max_clips_gb * 1e9
            removed = await asyncio.to_thread(
                prune, self.root, self.settings.keep_days, max_bytes
            )
            if removed:
                _LOGGER.info("Deleted %d old clip file(s)", removed)

    async def check(self, account: str) -> list[MotionEvent]:
        """One check of one account. Returns the new events."""
        blink = await self._blink(account)
        await sync_tokens(self.store, account, blink)
        now = time.time()
        clips = await list_clips(blink, self.log.since(account, now))
        new = self.log.new_clips(account, clips, now)
        if len(new) > MAX_DOWNLOADS:
            _LOGGER.info("%d new clips; saving %d now, the rest next time", len(new), MAX_DOWNLOADS)
            new = new[:MAX_DOWNLOADS]

        streams = {
            (e.account, str(e.camera["id"])): e.name
            for e in go2rtc.stream_entries(self.store)
        }
        events = []
        for clip in new:
            events.append(await self._save(account, blink, clip, streams, now))
            self.log.mark(account, clip)

        images = await latest_thumbnails(blink, {e.camera for e in events}) if events else {}
        for event in events:
            image = images.get(event.camera)
            if not event.thumbnail and image:
                rel = clip_rel(event, ".jpg")
                write_file(self.root / rel, image, event.created)
                event.thumbnail = rel
            self.log.add(event)
        self.log.last_ok[account] = now
        self.log.save()

        for event in events:
            _LOGGER.info(
                "Motion: %s at %s (%s)%s", event.camera, event.time, event.source,
                "" if event.clip else ", clip not downloaded",
            )
            if self.on_event:
                try:
                    await self.on_event(event, images.get(event.camera))
                except Exception:
                    _LOGGER.exception("Motion event handler failed")
        return events

    async def _save(self, account, blink, clip: Clip, streams, now) -> MotionEvent:
        camera = blink.cameras.get(clip.camera)
        camera_id = str(camera.camera_id) if camera else ""
        stream = streams.get((account, camera_id)) or slugify(clip.camera)
        event = MotionEvent(
            id=clip.key, account=account, camera=clip.camera, camera_id=camera_id,
            stream=stream, time=iso(clip.created), source=clip.source,
            late=clip.created < now - NOTIFY_WINDOW,
        )
        video = clip_rel(event, ".mp4")
        thumb = clip_rel(event, ".jpg")
        try:
            got_video, got_thumb = await download_clip(
                blink, clip, self.root / video, self.root / thumb
            )
        except Exception as err:
            _LOGGER.warning("Downloading %s clip from %s failed: %r", clip.source, clip.camera, err)
            got_video = got_thumb = False
        event.clip = video if got_video else ""
        event.thumbnail = thumb if got_thumb else ""
        return event

    async def _blink(self, account: str):
        entry = self._sessions.get(account)
        if entry and account not in self._stale and time.time() - entry[2] < SESSION_MAX_AGE:
            return entry[1]
        await self._drop(account)
        self._stale.discard(account)
        session = ClientSession()
        try:
            blink = await start_blink(self.store, account, session)
        except BaseException:
            await session.close()
            raise
        self._sessions[account] = (session, blink, time.time())
        return blink

    async def _drop(self, account: str) -> None:
        entry = self._sessions.pop(account, None)
        if entry and not entry[0].closed:
            await entry[0].close()
