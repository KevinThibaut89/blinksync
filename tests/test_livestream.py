import asyncio
import io
from types import SimpleNamespace

from blinksync.livestream import PipeLiveStream

RESPONSE = {
    "command_id": 1,
    "polling_interval": 15,
    "server": "immis://1.2.3.4:443/ABCDEFGHIJKLMNOP__IMDS_xyz?client_id=123",
    "liveview_token": "tok-123",
}
CAMERA = SimpleNamespace(serial="SERIAL123")


def frame(msgtype: int, payload: bytes, seq: int = 0) -> bytes:
    return bytes([msgtype]) + seq.to_bytes(4, "big") + len(payload).to_bytes(4, "big") + payload


class FakeWriter:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True

    def is_closing(self):
        return self.closed


async def feed_in_chunks(reader: asyncio.StreamReader, data: bytes, size: int):
    for i in range(0, len(data), size):
        reader.feed_data(data[i : i + size])
        await asyncio.sleep(0)
    reader.feed_eof()


def run_recv(data: bytes, chunk: int, sink=None):
    sink = sink or io.BytesIO()

    async def main():
        stream = PipeLiveStream(CAMERA, RESPONSE, sink)
        stream.target_reader = asyncio.StreamReader()
        stream.target_writer = FakeWriter()
        await asyncio.gather(stream.recv(), feed_in_chunks(stream.target_reader, data, chunk))
        return stream

    return asyncio.run(main()), sink


def test_auth_header_carries_liveview_token():
    header = PipeLiveStream(CAMERA, RESPONSE, io.BytesIO()).get_auth_header()
    assert len(header) == 122
    assert header[30:34] == (64).to_bytes(4, "big")
    assert header[34:98] == b"tok-123".ljust(64, b"\x00")
    # Connection id still lands where blinkpy puts it.
    assert header[102:118] == b"ABCDEFGHIJKLMNOP"


def test_auth_header_without_token_matches_blinkpy():
    response = {k: v for k, v in RESPONSE.items() if k != "liveview_token"}
    header = PipeLiveStream(CAMERA, response, io.BytesIO()).get_auth_header()
    assert header[34:98] == b"\x00" * 64


def test_recv_survives_short_reads_and_filters_frames():
    ts1 = b"\x47" + b"a" * 187
    ts2 = b"\x47" + b"b" * 1315
    data = (
        frame(0x00, ts1)
        + frame(0x12, b"\x00" * 24)  # stats frame: skipped
        + frame(0x00, b"\x00junk")  # video frame without TS sync byte: skipped
        + frame(0x00, ts2)
    )
    # 7-byte chunks split every header and payload across reads.
    stream, sink = run_recv(data, chunk=7)
    assert sink.getvalue() == ts1 + ts2
    assert stream.bytes_out == len(ts1) + len(ts2)
    assert stream.target_writer.closed


def test_recv_stops_when_viewer_leaves():
    class ClosedSink(io.BytesIO):
        def write(self, b):
            raise BrokenPipeError

    stream, _ = run_recv(frame(0x00, b"\x47" * 188) * 3, chunk=1000, sink=ClosedSink())
    assert stream.sink_closed
    assert stream.bytes_out == 0


def test_recv_handles_truncated_stream():
    data = frame(0x00, b"\x47" + b"x" * 187) + frame(0x00, b"\x47" * 100)[:50]
    stream, sink = run_recv(data, chunk=64)
    assert len(sink.getvalue()) == 188
