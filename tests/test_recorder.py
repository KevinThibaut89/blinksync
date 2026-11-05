import asyncio
import os
import stat

from aiohttp import web as aioweb
from aiohttp.test_utils import TestServer

from blinksync import recorder
from blinksync.events import EventLog, EventWatcher
from blinksync.go2rtc import StreamEntry, basic_auth
from blinksync.recorder import Recorder, match_camera
from blinksync.settings import Settings

ENTRIES = [
    StreamEntry("front_door", "home", {"id": "7", "name": "Front Door"}),
    StreamEntry("front_door_2", "home", {"id": "8", "name": "Front Door 2"}),
    StreamEntry("garden", "home", {"id": "9", "name": "Garden"}),
]


def test_match_camera():
    assert match_camera(ENTRIES, camera="garden").name == "garden"
    assert match_camera(ENTRIES, camera="Front Door").name == "front_door"
    assert match_camera(ENTRIES, camera="8").name == "front_door_2"
    assert match_camera(ENTRIES, camera="attic") is None
    assert match_camera(ENTRIES, text="Motion detected at FRONT DOOR 2").name == "front_door_2"
    assert match_camera(ENTRIES, text="Someone is at your Front Door").name == "front_door"
    assert match_camera(ENTRIES, text="Blink: motion detected") is None
    assert match_camera(ENTRIES, text="") is None


# --------------------------------------------------------------------------
# Recorder with go2rtc and ffmpeg faked out
# --------------------------------------------------------------------------


def make_recorder(data, monkeypatch, video=b"v" * 10_000, frame=b"\xff\xd8jpg"):
    settings = Settings(clips_dir=str(data / "clips"), record_seconds=5, record_cooldown=60)
    watcher = EventWatcher(None, settings)
    received = []
    calls = []

    async def on_event(event, image):
        received.append((event, image))

    async def record_mp4(settings_, stream, seconds, target):
        calls.append((stream, seconds))
        await asyncio.sleep(0.01)
        if not video:
            return 0
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(video)
        return len(video)

    async def extract_frame(video_path, target):
        if frame:
            target.write_bytes(frame)
        return frame

    monkeypatch.setattr(recorder, "record_mp4", record_mp4)
    monkeypatch.setattr(recorder, "extract_frame", extract_frame)
    return Recorder(settings, watcher, on_event=on_event), received, calls


def test_records_once_then_cools_down(data, monkeypatch):
    rec, received, calls = make_recorder(data, monkeypatch)

    async def scenario():
        assert rec.trigger(ENTRIES[0]) == "recording"
        assert rec.recording("front_door")
        assert rec.trigger(ENTRIES[0]) == "busy"  # second phone, same notification
        assert rec.trigger(ENTRIES[2]) == "recording"  # other cameras are independent
        await asyncio.gather(*rec._active.values())
        assert not rec.recording("front_door")
        assert rec.trigger(ENTRIES[0]) == "cooldown"

    asyncio.run(scenario())
    assert sorted(calls) == [("front_door", 5), ("garden", 5)]

    event, image = next((e, i) for e, i in received if e.stream == "front_door")
    assert image == b"\xff\xd8jpg"
    assert event.source == "notification" and event.camera_id == "7"
    root = data / "clips"
    assert (root / event.clip).read_bytes()[:3] == b"vvv"
    assert event.clip.startswith("front_door/") and event.clip.endswith(".mp4")
    assert (root / event.thumbnail).read_bytes() == b"\xff\xd8jpg"
    assert abs(os.path.getmtime(root / event.clip) - event.created) < 1
    assert {e.stream for e in EventLog(root).recent()} == {"front_door", "garden"}


def test_failed_recording_is_logged_without_clip(data, monkeypatch):
    rec, received, _ = make_recorder(data, monkeypatch, video=b"")

    async def scenario():
        rec.trigger(ENTRIES[2])
        await asyncio.gather(*rec._active.values())

    asyncio.run(scenario())
    event, image = received[0]
    assert event.clip == "" and event.thumbnail == "" and image is None
    assert EventLog(data / "clips").recent()[0].clip == ""


def test_stop_cancels_recordings(data, monkeypatch):
    rec, received, _ = make_recorder(data, monkeypatch)

    async def slow(settings_, stream, seconds, target):
        await asyncio.sleep(60)

    monkeypatch.setattr(recorder, "record_mp4", slow)

    async def scenario():
        rec.trigger(ENTRIES[0])
        await asyncio.sleep(0)
        await rec.stop()
        assert not rec.recording("front_door")

    asyncio.run(scenario())
    assert received == []


# --------------------------------------------------------------------------
# record_mp4 against a fake go2rtc, extract_frame with a fake ffmpeg
# --------------------------------------------------------------------------


def run_go2rtc(handler, scenario):
    async def main():
        app = aioweb.Application()
        app.router.add_get("/api/stream.mp4", handler)
        async with TestServer(app) as server:
            return await scenario(server.port)

    return asyncio.run(main())


def test_record_mp4_saves_go2rtc_stream(tmp_path):
    seen = {}

    async def handler(request):
        seen.update(request.query, auth=request.headers.get("Authorization"))
        return aioweb.Response(body=b"\x00" * 50_000, content_type="video/mp4")

    async def scenario(port):
        settings = Settings(web_password="pw", go2rtc_port=port)
        return await recorder.record_mp4(settings, "garden", 15, tmp_path / "a/b.mp4")

    assert run_go2rtc(handler, scenario) == 50_000
    assert (tmp_path / "a/b.mp4").stat().st_size == 50_000
    assert seen == {"src": "garden", "duration": str(15 + recorder.LIVEVIEW_STARTUP),
                    "auth": basic_auth("admin", "pw")}


def test_record_mp4_rejects_errors_and_empty_streams(tmp_path):
    responses = [aioweb.Response(status=500, text="exec failed"), aioweb.Response(body=b"tiny")]

    async def handler(request):
        return responses.pop(0)

    async def scenario(port):
        settings = Settings(go2rtc_port=port)
        first = await recorder.record_mp4(settings, "garden", 5, tmp_path / "x.mp4")
        second = await recorder.record_mp4(settings, "garden", 5, tmp_path / "x.mp4")
        return first, second

    assert run_go2rtc(handler, scenario) == (0, 0)
    assert list(tmp_path.iterdir()) == []  # no partial files left


def test_extract_frame(tmp_path, monkeypatch):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"mp4")
    monkeypatch.setattr(recorder.shutil, "which", lambda name: None)
    assert asyncio.run(recorder.extract_frame(video, tmp_path / "a.jpg")) is None

    # A stand-in ffmpeg: fails at -ss 2 (clip too short), works at -ss 0.
    fake = tmp_path / "ffmpeg"
    fake.write_text(
        '#!/bin/sh\n'
        'for a; do out="$a"; done\n'
        'case "$*" in *"-ss 2 "*) echo "no frame" >&2; exit 1;; esac\n'
        'printf "JPEG" > "$out"\n'
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setattr(recorder.shutil, "which", lambda name: str(fake))
    assert asyncio.run(recorder.extract_frame(video, tmp_path / "a.jpg")) == b"JPEG"
    assert (tmp_path / "a.jpg").read_bytes() == b"JPEG"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.jpg", "clip.mp4", "ffmpeg"]
