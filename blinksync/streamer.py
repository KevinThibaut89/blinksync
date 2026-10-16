"""The `blinksync stream` process that go2rtc launches for each viewing session.

Blink ends a live view after about 5 minutes. While go2rtc still has a viewer
(our stdout is open), we ask for a new one and keep writing, until
`max_minutes` is reached. go2rtc sends SIGTERM when the last viewer leaves;
cancelling the feed lets blinkpy tell Blink the live view is done so the
camera can go back to sleep.
"""

from __future__ import annotations

import asyncio
import logging
import signal
from typing import BinaryIO

from blinkpy import api

from .accounts import AccountError, AccountStore, open_blink
from .livestream import PipeLiveStream

_LOGGER = logging.getLogger(__name__)

MAX_FAILED_SESSIONS = 3
RETRY_DELAY = 5


def find_camera(blink, camera_id: str):
    for camera in blink.cameras.values():
        if str(camera.camera_id) == str(camera_id):
            return camera
    names = ", ".join(f"{c.name} ({c.camera_id})" for c in blink.cameras.values())
    raise AccountError(f"No camera with id {camera_id}. Cameras: {names or 'none'}")


async def run_stream(
    store: AccountStore, account: str, camera_id: str, max_minutes: float, sink: BinaryIO
) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max_minutes * 60

    async with open_blink(store, account, thumbnails=False) as blink:
        camera = find_camera(blink, camera_id)
        failures = 0
        while (remaining := deadline - loop.time()) > RETRY_DELAY:
            _LOGGER.info("Requesting live view for %s", camera.name)
            response = await api.request_camera_liveview(
                blink, camera.sync.network_id, camera.camera_id,
                camera_type=camera.camera_type,
            )
            server = (response or {}).get("server", "")
            if not server.startswith("immis://"):
                failures += 1
                _LOGGER.warning("Blink refused live view (%s): %s", failures, response)
                if failures >= MAX_FAILED_SESSIONS:
                    raise AccountError(f"Blink refused live view for {camera.name}")
                await asyncio.sleep(RETRY_DELAY)
                continue

            stream = PipeLiveStream(camera, response, sink)
            try:
                await asyncio.wait_for(stream.feed(), remaining)
            except asyncio.TimeoutError:
                _LOGGER.info("Reached the %s minute limit, stopping", max_minutes)
                return
            if stream.sink_closed:
                _LOGGER.info("Viewer left, stopping")
                return
            if stream.bytes_out:
                failures = 0
                _LOGGER.info(
                    "Live view ended after %.1f MB, renewing", stream.bytes_out / 1e6
                )
            else:
                failures += 1
                _LOGGER.warning("Live view produced no video (%s)", failures)
                if failures >= MAX_FAILED_SESSIONS:
                    raise AccountError(f"No video from {camera.name}")
                await asyncio.sleep(RETRY_DELAY)


def stream_forever(
    store: AccountStore, account: str, camera_id: str, max_minutes: float, sink: BinaryIO
) -> int:
    """Run `run_stream` until done, cancelling cleanly on SIGTERM/SIGINT."""

    async def main() -> int:
        task = asyncio.current_task()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, task.cancel)
        try:
            await run_stream(store, account, camera_id, max_minutes, sink)
        except asyncio.CancelledError:
            _LOGGER.info("Stopped by signal")
        except AccountError as err:
            _LOGGER.error("%s", err)
            return 1
        except Exception as err:
            _LOGGER.error(
                "Stream failed: %r", err, exc_info=_LOGGER.isEnabledFor(logging.DEBUG)
            )
            return 1
        return 0

    return asyncio.run(main())
