"""Blink IMMIS live view written straight to a byte sink (stdout for go2rtc).

blinkpy's `BlinkLiveStream` relays the stream through a local TCP server. We
only need one consumer, so this subclass skips the server and writes the
MPEG-TS packets directly. It also carries two fixes that blinkpy 0.25.9
doesn't have yet:

* `recv()` uses `readexactly` (fronzbot/blinkpy#1304). Plain `read(n)` can
  return a short read on a busy TLS socket, and 0.25.9 then aborts the stream
  ("Insufficient data for payload").
* The relay auth header carries the `liveview_token` from the liveview
  response instead of 64 null bytes (fronzbot/blinkpy#1303, #1309).
"""

from __future__ import annotations

import asyncio
import logging
import ssl
from typing import BinaryIO

from blinkpy.livestream import BlinkLiveStream

_LOGGER = logging.getLogger(__name__)

HEADER_LEN = 9
MSG_VIDEO = 0x00
TS_SYNC_BYTE = 0x47
# Byte range of the 64-byte token field inside the 122-byte auth header.
TOKEN_SLICE = slice(34, 98)


class PipeLiveStream(BlinkLiveStream):
    def __init__(self, camera, response: dict, sink: BinaryIO):
        super().__init__(camera, response)
        self.liveview_token = (
            response.get("liveview_token") or response.get("liveViewToken") or ""
        )
        self.sink = sink
        self.bytes_out = 0
        self.sink_closed = False

    def get_auth_header(self):
        header = super().get_auth_header()
        if self.liveview_token:
            token = self.liveview_token.encode()[:64].ljust(64, b"\x00")
            header[TOKEN_SLICE] = token
        return header

    async def recv(self):
        """Read IMMIS frames from the relay and write video payloads to the sink."""
        try:
            while not self.target_reader.at_eof():
                header = await self.target_reader.readexactly(HEADER_LEN)
                msgtype = header[0]
                length = int.from_bytes(header[5:9], "big")
                if length <= 0:
                    continue
                payload = await self.target_reader.readexactly(length)
                if msgtype != MSG_VIDEO or payload[0] != TS_SYNC_BYTE:
                    continue
                try:
                    self.sink.write(payload)
                    self.sink.flush()
                except (BrokenPipeError, ConnectionResetError, ValueError):
                    # go2rtc closed our stdout: the last viewer left.
                    self.sink_closed = True
                    break
                self.bytes_out += len(payload)
                await asyncio.sleep(0)
        except asyncio.IncompleteReadError:
            _LOGGER.info("Blink relay closed the live view")
        except ssl.SSLError as err:
            if err.reason != "APPLICATION_DATA_AFTER_CLOSE_NOTIFY":
                _LOGGER.exception("TLS error from Blink relay")
        except ConnectionError:
            _LOGGER.info("Blink relay connection dropped")
        finally:
            # Ends send() and poll() too, which makes feed() return.
            self.target_writer.close()
