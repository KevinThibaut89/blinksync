import asyncio
import json
import os
import time
from types import SimpleNamespace

import pytest

from blinksync import accounts, events
from blinksync.accounts import AccountError, ReauthRequired
from blinksync.events import Clip, EventLog, EventWatcher, prune
from blinksync.settings import Settings

from .conftest import add_account

NOW = 1_800_000_000.0


def clip(key, created, camera="Front Door", source="local"):
    return Clip(key, camera, created, source)


# --------------------------------------------------------------------------
# EventLog
# --------------------------------------------------------------------------


def test_first_check_only_records_existing_clips(tmp_path):
    log = EventLog(tmp_path)
    assert log.new_clips("home", [clip("a", NOW - 60)], NOW) == []
    assert "a" in log.seen["home"]


def test_new_clips_skips_seen_and_too_old(tmp_path):
    log = EventLog(tmp_path)
    log.new_clips("home", [clip("a", NOW - 60)], NOW)
    log.last_ok["home"] = NOW - 60

    clips = [
        clip("a", NOW - 60),  # seen
        clip("b", NOW - 10),
        clip("c", NOW - 30),
        clip("old", NOW - events.WINDOW - 120),  # before the look-back window
    ]
    new = log.new_clips("home", clips, NOW)
    assert [c.key for c in new] == ["c", "b"]  # oldest first

    # Not marked until handled, so a failed check retries them.
    assert [c.key for c in log.new_clips("home", clips, NOW)] == ["c", "b"]
    for c in new:
        log.mark("home", c)
    assert log.new_clips("home", clips, NOW) == []


def test_lookback_after_downtime_is_capped(tmp_path):
    log = EventLog(tmp_path)
    log.seen["home"] = {}
    log.last_ok["home"] = NOW - 5 * 86400
    assert log.since("home", NOW) == NOW - events.MAX_LOOKBACK


def test_seen_keys_expire(tmp_path):
    log = EventLog(tmp_path)
    log.seen["home"] = {"ancient": NOW - events.SEEN_TTL - 1, "recent": NOW - 5}
    log.new_clips("home", [], NOW)
    assert list(log.seen["home"]) == ["recent"]


def test_log_round_trip(tmp_path):
    log = EventLog(tmp_path)
    log.seen["home"] = {"a": NOW}
    log.last_ok["home"] = NOW
    log.add(events.MotionEvent("a", "home", "Cam", "7", "cam", events.iso(NOW), "local"))
    log.save()

    again = EventLog(tmp_path)
    assert again.seen == {"home": {"a": NOW}}
    assert again.recent("home")[0].camera == "Cam"
    assert again.recent("other") == []


# --------------------------------------------------------------------------
# prune
# --------------------------------------------------------------------------


def _file(path, size, age_days):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    t = time.time() - age_days * 86400
    os.utime(path, (t, t))


def test_prune_by_age_and_size(tmp_path):
    _file(tmp_path / "cam/2020-01-01/old.mp4", 10, age_days=30)
    _file(tmp_path / "cam/d/a.mp4", 100, age_days=3)
    _file(tmp_path / "cam/d/b.mp4", 100, age_days=2)
    _file(tmp_path / "cam/d/c.jpg", 100, age_days=1)
    (tmp_path / ".events.json").write_text("{}")

    assert prune(tmp_path, keep_days=14, max_bytes=250) == 2
    left = sorted(p.name for p in tmp_path.rglob("*") if p.is_file())
    assert left == [".events.json", "b.mp4", "c.jpg"]
    assert not (tmp_path / "cam/2020-01-01").exists()  # empty folder removed


# --------------------------------------------------------------------------
# EventWatcher with Blink faked out
# --------------------------------------------------------------------------


class FakeBlink:
    def __init__(self):
        self.cameras = {"Front Door": SimpleNamespace(camera_id=7)}


@pytest.fixture
def fake_blink(monkeypatch):
    state = SimpleNamespace(clips=[], logins=0, downloads=[], error=None)

    async def start_blink(store, name, session):
        state.logins += 1
        return FakeBlink()

    async def sync_tokens(store, name, blink, min_valid=600):
        if state.error:
            raise state.error

    async def list_clips(blink, since):
        return [c for c in state.clips if c.created > since]

    async def download_clip(blink, c, video, thumb):
        state.downloads.append(c.key)
        if c.key == "broken":
            return False, False
        for path in (video, thumb) if c.source == "cloud" else (video,):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"data")
        return True, c.source == "cloud"

    async def latest_thumbnails(blink, cameras):
        return {name: b"\xff\xd8thumb" for name in cameras}

    for name, fn in [
        ("start_blink", start_blink), ("sync_tokens", sync_tokens),
        ("list_clips", list_clips), ("download_clip", download_clip),
        ("latest_thumbnails", latest_thumbnails),
    ]:
        monkeypatch.setattr(events, name, fn)
    return state


def make_watcher(store, data, received):
    async def on_event(event, image):
        received.append((event, image))

    settings = Settings(poll_seconds=60, clips_dir=str(data / "clips"))
    return EventWatcher(store, settings, on_event=on_event)


def test_watcher_saves_new_clips(store, data, fake_blink):
    add_account(store, "home", [{"id": "7", "name": "Front Door"}])
    received = []
    watcher = make_watcher(store, data, received)
    now = time.time()
    fake_blink.clips = [clip("existing", now - 120)]

    async def scenario():
        assert await watcher.check("home") == []  # first check: baseline
        fake_blink.clips += [
            clip("local:1:42", now - 5),
            clip("cloud:9", now - 3, source="cloud"),
            clip("broken", now - 1),
        ]
        new = await watcher.check("home")
        assert await watcher.check("home") == []  # nothing twice
        await watcher.stop()
        return new

    new = asyncio.run(scenario())
    assert fake_blink.logins == 1  # the session is reused between checks
    assert [e.id for e in new] == ["local:1:42", "cloud:9", "broken"]

    local, cloud, broken = new
    assert local.stream == "front_door" and local.camera_id == "7"
    assert local.clip.startswith("front_door/") and local.clip.endswith("_local42.mp4")
    assert (watcher.root / local.clip).read_bytes() == b"data"
    # No thumbnail from the download: the camera's latest one is saved instead.
    assert (watcher.root / local.thumbnail).read_bytes() == b"\xff\xd8thumb"
    assert cloud.thumbnail.endswith("_cloud9.jpg")
    assert (watcher.root / cloud.thumbnail).read_bytes() == b"data"
    assert broken.clip == ""

    assert [(e.id, img) for e, img in received] == [
        (e.id, b"\xff\xd8thumb") for e in new
    ]
    assert [e.id for e in EventLog(watcher.root).recent("home")] == [
        "broken", "cloud:9", "local:1:42"
    ]


def test_late_clips_are_saved_but_flagged(store, data, fake_blink):
    add_account(store, "home", [{"id": "7", "name": "Front Door"}])
    received = []
    watcher = make_watcher(store, data, received)
    now = time.time()
    watcher.log.seen["home"] = {}
    watcher.log.last_ok["home"] = now - 3 * 3600  # blinkSync was down
    fake_blink.clips = [clip("during-downtime", now - 2 * 3600), clip("fresh", now - 5)]

    new = asyncio.run(watcher.check("home"))
    assert [(e.id, e.late) for e in new] == [("during-downtime", True), ("fresh", False)]


def test_check_all_backs_off_and_pauses(store, data, fake_blink):
    add_account(store, "home", [])
    watcher = make_watcher(store, data, [])

    async def scenario():
        fake_blink.error = AccountError("Couldn't reach Blink")
        await watcher.check_all()
        status = watcher.status["home"]
        assert status.failures == 1 and status.retry_at > time.time()
        assert "home" not in watcher._sessions  # a failed session is dropped

        await watcher.check_all()  # still backing off: not retried
        assert status.failures == 1

        status.retry_at = 0
        fake_blink.error = ReauthRequired("Blink wants a new 2FA login")
        await watcher.check_all()
        assert status.paused
        logins = fake_blink.logins
        await watcher.check_all()
        assert fake_blink.logins == logins  # paused: no more login attempts

        fake_blink.error = None
        watcher.reload("home")  # user signed in again
        await watcher.check_all()
        assert watcher.status["home"].last_ok and not watcher.status["home"].error
        await watcher.stop()

    asyncio.run(scenario())


def test_webhook_payload(settings):
    settings.public_url = "http://10.0.0.5:8080/"
    event = events.MotionEvent(
        "local:1:2", "home", "Front Door", "7", "front_door",
        "2026-11-05T10:00:00+00:00", "local", clip="front_door/d/a.mp4",
    )
    payload = events.webhook_payload(event, settings)
    assert payload["clip_url"] == "http://10.0.0.5:8080/clips/front_door/d/a.mp4?key=k3y"
    assert payload["thumbnail_url"] is None
    assert payload["snapshot_url"] == "http://10.0.0.5:8080/snapshot/front_door.jpg?key=k3y"
    assert payload["camera"] == "Front Door" and payload["stream"] == "front_door"


# --------------------------------------------------------------------------
# Downloads and tokens
# --------------------------------------------------------------------------


def test_local_download_retries_until_uploaded(tmp_path, monkeypatch):
    responses = [None, SimpleNamespace(status=404), "video"]

    class Resp:
        status = 200

        async def read(self):
            return b"mp4"

    async def http_get(blink, url, json=True):
        assert url == "https://base/clip/9?manifest=m2"
        r = responses.pop(0)
        return Resp() if r == "video" else r

    async def no_sleep(_):
        pass

    monkeypatch.setattr(events.api, "http_get", http_get)
    monkeypatch.setattr(events.asyncio, "sleep", no_sleep)

    class Item:
        manifest = "m1"

        def url(self, manifest_id=None):
            if manifest_id:
                self.manifest = manifest_id
            return f"/clip/9?manifest={self.manifest}"

        async def prepare_download(self, blink):
            return {"id": 1}

    blink = SimpleNamespace(urls=SimpleNamespace(base_url="https://base"))
    sync = SimpleNamespace(_local_storage={"last_manifest_id": "m2"})
    c = Clip("local:1:9", "Cam", NOW, "local", Item(), sync)
    video, thumb = tmp_path / "a.mp4", tmp_path / "a.jpg"

    assert asyncio.run(events.download_clip(blink, c, video, thumb)) == (True, False)
    assert video.read_bytes() == b"mp4"
    assert os.path.getmtime(video) == NOW


class FakeAuth:
    def __init__(self, refresh_token, expiration_date):
        self.token = "old-access"
        self.refresh_token = refresh_token
        self.expiration_date = expiration_date
        self.expires_in = 3600
        self.startups = 0

    async def startup(self):
        self.startups += 1
        self.token, self.refresh_token = "new-access", "new-refresh"
        self.expiration_date = time.time() + 3600

    @property
    def login_attributes(self):
        return {"token": self.token, "refresh_token": self.refresh_token,
                "expiration_date": self.expiration_date}


def test_sync_tokens_adopts_newer_saved_token(store):
    add_account(store, "home", [])
    later = time.time() + 3000
    store.creds_path("home").write_text(json.dumps(
        {"token": "file-access", "refresh_token": "file-refresh", "expiration_date": later}
    ))
    blink = SimpleNamespace(auth=FakeAuth("mine", time.time() + 3000))

    asyncio.run(accounts.sync_tokens(store, "home", blink))
    assert (blink.auth.token, blink.auth.refresh_token) == ("file-access", "file-refresh")
    assert blink.auth.startups == 0


def test_sync_tokens_refreshes_and_saves_when_expiring(store):
    add_account(store, "home", [])
    blink = SimpleNamespace(auth=FakeAuth(None, time.time() + 30))

    asyncio.run(accounts.sync_tokens(store, "home", blink))
    assert blink.auth.startups == 1
    saved = json.loads(store.creds_path("home").read_text())
    assert saved["refresh_token"] == "new-refresh"


# --------------------------------------------------------------------------
# Saved clips on disk
# --------------------------------------------------------------------------


def test_saved_clips_and_delete(tmp_path):
    _file(tmp_path / "garden/2026-11-04/080000_x.mp4", 10, age_days=1)
    _file(tmp_path / "garden/2026-11-04/080000_x.jpg", 5, age_days=1)
    _file(tmp_path / "front_door/2026-11-05/090000_y.mp4", 20, age_days=0)
    _file(tmp_path / "front_door/2026-11-05/.090500_z.mp4.tmp", 1, age_days=0)
    _file(tmp_path / "front_door/2026-11-05/.hidden.mp4", 1, age_days=0)
    (tmp_path / "outside.mp4").write_bytes(b"x")

    clips = events.saved_clips(tmp_path)
    assert [c.rel for c in clips] == [
        "front_door/2026-11-05/090000_y.mp4", "garden/2026-11-04/080000_x.mp4"
    ]
    assert clips[1].thumbnail == "garden/2026-11-04/080000_x.jpg"
    assert clips[0].thumbnail == "" and clips[0].stream == "front_door"
    assert clips[1].day == "2026-11-04" and clips[1].size == 10

    assert not events.delete_clip(tmp_path, "../outside.mp4")
    assert not events.delete_clip(tmp_path, "garden/2026-11-04/080000_x.jpg")
    assert not events.delete_clip(tmp_path, "garden/nope.mp4")
    assert events.delete_clip(tmp_path, "garden/2026-11-04/080000_x.mp4")
    assert not (tmp_path / "garden").exists()  # emptied folders go too
    assert (tmp_path / "outside.mp4").exists()
    assert events.saved_clips(tmp_path / "missing") == []
