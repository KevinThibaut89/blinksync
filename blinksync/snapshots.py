"""Cached camera stills for Home Assistant.

HA asks a camera entity for a still image every few seconds while a dashboard
is open. If those stills came from the RTSP stream, every refresh would start
a live view and drain the battery cameras. Instead we serve Blink's own latest
thumbnail (from the last motion clip or snapshot), which doesn't wake the
camera, and refresh it from the cloud at most every `ttl` seconds.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable

from .accounts import AccountError, AccountStore, open_blink

_LOGGER = logging.getLogger(__name__)


class SnapshotCache:
    def __init__(
        self,
        store: AccountStore,
        ttl: float = 300,
        on_refresh: Callable[[str], Awaitable[None]] | None = None,
    ):
        self.store = store
        self.ttl = ttl
        self.on_refresh = on_refresh
        self._images: dict[tuple[str, str], bytes] = {}
        self._fetched: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()

    async def get(self, account: str, camera_id: str) -> bytes | None:
        key = (account, camera_id)
        fresh = time.monotonic() - self._fetched.get(account, -1e9) < self.ttl
        if key in self._images:
            if not fresh:
                # Serve the stale image now, refresh in the background.
                task = asyncio.create_task(self.refresh(account))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
            return self._images[key]
        if not fresh:
            await self.refresh(account)
        return self._images.get(key)

    async def refresh(self, account: str) -> None:
        lock = self._locks.setdefault(account, asyncio.Lock())
        if lock.locked():
            async with lock:  # someone else is refreshing; just wait for them
                return
        async with lock:
            try:
                async with open_blink(self.store, account) as blink:
                    for camera in blink.cameras.values():
                        image = camera.image_from_cache()
                        if image:
                            self._images[(account, str(camera.camera_id))] = image
            except AccountError as err:
                _LOGGER.warning("Snapshot refresh failed: %s", err)
            # Also on failure, so a broken account isn't retried on every request.
            self._fetched[account] = time.monotonic()
        if self.on_refresh:
            await self.on_refresh(account)

    def forget(self, account: str) -> None:
        self._fetched.pop(account, None)
        for key in [k for k in self._images if k[0] == account]:
            del self._images[key]
