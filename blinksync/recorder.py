"""Record a camera on demand, e.g. when a Blink motion notification arrives.

Without a Sync Module 2 or a Blink plan, Blink saves no clips at all. Its
motion and doorbell notifications still reach your phone, though, and an iOS
Shortcut can pass them on: Home Assistant calls POST /api/motion with the
notification text, `match_camera` finds the camera it names, and we record
`record_seconds` from go2rtc's MP4 endpoint. That starts a Blink live view,
or shares the one already running for someone watching.

Live view takes a few seconds to start, so a recording begins roughly 10 s
after the motion (notification delay included).
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from pathlib import Path

from aiohttp import ClientError, ClientSession, ClientTimeout

from . import go2rtc
from .events import EventWatcher, MotionEvent, OnEvent, clip_rel, iso
from .go2rtc import StreamEntry
from .settings import Settings

_LOGGER = logging.getLogger(__name__)

MIN_CLIP_BYTES = 4096  # less than this is an error page or an empty file


def match_camera(
    entries: list[StreamEntry], camera: str = "", text: str = ""
) -> StreamEntry | None:
    """The camera named by `camera` (stream name, camera name or id), or else
    the one whose name appears in `text` (a notification)."""
    if camera := camera.strip().lower():
        for entry in entries:
            if camera in (entry.name, entry.camera["name"].lower(), str(entry.camera["id"])):
                return entry
        return None
    text = text.lower()
    # Longest name first, so "Front Door 2" wins over "Front Door".
    for entry in sorted(entries, key=lambda e: len(e.camera["name"]), reverse=True):
        if entry.camera["name"].lower() in text:
            return entry
    return None


async def record_mp4(settings: Settings, stream: str, seconds: int, target: Path) -> int:
    """Save `seconds` of video of `stream` from go2rtc to `target`.

    go2rtc's `duration` counts video time from the first frame, so live view
    startup doesn't eat into it. Returns the size in bytes (0 if nothing
    usable came back).
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.tmp")
    size = 0
    timeout = ClientTimeout(total=seconds + 60, sock_read=30)
    try:
        async with ClientSession(timeout=timeout) as session:
            async with session.get(
                f"{settings.go2rtc_api}/api/stream.mp4",
                params={"src": stream, "duration": str(seconds)},
                headers=go2rtc.api_headers(settings),
            ) as response:
                if response.status != 200:
                    _LOGGER.warning("go2rtc returned HTTP %s for %s", response.status, stream)
                    return 0
                with open(tmp, "wb") as fh:
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        fh.write(chunk)
                        size += len(chunk)
        if size < MIN_CLIP_BYTES:
            _LOGGER.warning("No video from %s (is the camera awake and online?)", stream)
            return 0
        os.replace(tmp, target)
        return size
    finally:
        tmp.unlink(missing_ok=True)


async def _ffmpeg(*args: str) -> bool:
    """Run ffmpeg; False if it isn't installed, fails or hangs."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        _LOGGER.debug("ffmpeg not installed")
        return False
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-v", "error", "-y", *args,
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, err = await asyncio.wait_for(proc.communicate(), 60)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False
    if proc.returncode:
        _LOGGER.debug("ffmpeg %s failed: %s", args, err.decode(errors="replace").strip())
    return proc.returncode == 0


async def finish_clip(video: Path) -> None:
    """Drop the audio track and put the MP4 index first, in place.

    Blink's audio reaches us declared as 16 kHz AAC but with twice as many
    samples per second (measured: 42 s of video, 84 s of audio), so players
    showed every clip at double length with a frozen picture. The index at the
    front lets phones start playing before the whole file has loaded.
    """
    tmp = video.with_name(f".{video.stem}.fixed.mp4")
    if await _ffmpeg("-i", str(video), "-map", "0:v", "-c", "copy",
                     "-movflags", "+faststart", str(tmp)) and tmp.exists():
        os.replace(tmp, video)
    tmp.unlink(missing_ok=True)


async def extract_frame(video: Path, target: Path) -> bytes | None:
    """A JPEG from early in the clip, via ffmpeg (None if it isn't installed)."""
    tmp = target.with_name(f".{target.stem}.tmp.jpg")
    for offset in ("2", "0"):  # 2 s in skips the first, often grey, frames
        if not await _ffmpeg("-ss", offset, "-i", str(video),
                             "-frames:v", "1", "-q:v", "3", str(tmp)):
            continue
        if tmp.exists() and tmp.stat().st_size:
            data = tmp.read_bytes()
            os.replace(tmp, target)
            return data
    tmp.unlink(missing_ok=True)
    return None


class Recorder:
    """At most one recording per camera, with a cooldown after each."""

    def __init__(self, settings: Settings, watcher: EventWatcher, on_event: OnEvent | None = None):
        self.settings = settings
        self.watcher = watcher
        self.on_event = on_event
        self._active: dict[str, asyncio.Task] = {}
        self._ended: dict[str, float] = {}

    def recording(self, stream: str) -> bool:
        return stream in self._active

    def trigger(self, entry: StreamEntry, source: str = "notification") -> str:
        """Start recording `entry`. Returns "recording", "busy" or "cooldown"."""
        name = entry.name
        if name in self._active:
            return "busy"
        if time.time() - self._ended.get(name, 0) < self.settings.record_cooldown:
            return "cooldown"
        task = asyncio.create_task(self._record(entry, source))
        self._active[name] = task
        task.add_done_callback(lambda _: self._done(name))
        return "recording"

    def _done(self, name: str) -> None:
        self._active.pop(name, None)
        self._ended[name] = time.time()

    async def stop(self) -> None:
        tasks = list(self._active.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _record(self, entry: StreamEntry, source: str) -> MotionEvent:
        start = time.time()
        event = MotionEvent(
            id=f"{source}:{entry.name}:{int(start)}",
            account=entry.account,
            camera=entry.camera["name"],
            camera_id=str(entry.camera["id"]),
            stream=entry.name,
            time=iso(start),
            source=source,
        )
        root = self.watcher.root
        video = clip_rel(event, ".mp4")
        _LOGGER.info("Recording %s for %ss (%s)", event.camera, self.settings.record_seconds, source)
        try:
            size = await record_mp4(
                self.settings, entry.name, self.settings.record_seconds, root / video
            )
        except (ClientError, OSError, asyncio.TimeoutError) as err:
            _LOGGER.warning("Recording %s failed: %r", event.camera, err)
            size = 0

        image = None
        if size:
            await finish_clip(root / video)
            event.clip = video
            thumb = clip_rel(event, ".jpg")
            image = await extract_frame(root / video, root / thumb)
            if image:
                event.thumbnail = thumb
            for rel in (event.clip, event.thumbnail):
                if rel:
                    os.utime(root / rel, (start, start))  # pruning goes by clip time
            _LOGGER.info("Saved %s (%.1f MB)", video, size / 1e6)

        self.watcher.log.add(event)
        self.watcher.log.save()
        if self.on_event:
            try:
                await self.on_event(event, image)
            except Exception:
                _LOGGER.exception("Motion event handler failed")
        return event
