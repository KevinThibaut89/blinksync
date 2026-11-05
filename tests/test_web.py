import asyncio
import re

import pytest
from aiohttp.test_utils import TestClient, TestServer

from blinksync import web
from blinksync.accounts import AccountError
from blinksync.go2rtc import basic_auth

from .conftest import add_account

AUTH = {"Authorization": basic_auth("admin", "pw")}


@pytest.fixture(autouse=True)
def no_go2rtc(monkeypatch):
    calls = []

    async def fake_apply(store, settings):
        calls.append(store.names())
        return True

    async def not_running(settings):
        return False

    monkeypatch.setattr(web.go2rtc, "apply", fake_apply)
    monkeypatch.setattr(web, "go2rtc_running", not_running)
    return calls


def run(store, settings, scenario):
    async def main():
        async with TestClient(TestServer(web.create_app(store, settings))) as client:
            return await scenario(client)

    return asyncio.run(main())


def test_requires_password(store, settings):
    async def scenario(client):
        assert (await client.get("/")).status == 401
        bad = await client.get(
            "/", headers={"Authorization": basic_auth("admin", "nope")}
        )
        assert bad.status == 401
        ok = await client.get("/", headers=AUTH)
        assert ok.status == 200
        assert "Add Blink account" in await ok.text()

    run(store, settings, scenario)


def test_blocks_cross_site_post(store, settings):
    async def scenario(client):
        r = await client.post(
            "/accounts/add", headers={**AUTH, "Origin": "http://evil.example"},
            data={"email": "a@b.c", "password": "x"},
        )
        assert r.status == 403

    run(store, settings, scenario)


def test_dashboard_lists_stream_urls(store, settings):
    add_account(store, "home", [{"id": "7", "name": "Front Door", "battery": "ok"}])

    async def scenario(client):
        html = await (await client.get("/", headers=AUTH)).text()
        assert re.search(r"rtsp://127\.0\.0\.1:8554/front_door", html)
        assert "/snapshot/front_door.jpg?key=k3y" in html
        assert "me@x.com" in html

    run(store, settings, scenario)


def test_add_account_with_2fa(store, settings, monkeypatch, no_go2rtc):
    class FakeLogin:
        def __init__(self, store_, name, username, password):
            self.store, self.name = store_, name

        async def begin(self):
            return True

        async def verify(self, code):
            if code != "123456":
                raise AccountError("Blink rejected the 2FA code.")
            add_account(self.store, self.name, [])

        async def close(self):
            pass

    monkeypatch.setattr(web, "PendingLogin", FakeLogin)

    async def scenario(client):
        r = await client.post(
            "/accounts/add", headers=AUTH, allow_redirects=False,
            data={"email": "Jane.Doe@example.com", "password": "secret"},
        )
        assert r.status == 302
        verify_url = r.headers["Location"]
        assert verify_url.startswith("/accounts/verify/")

        r = await client.post(verify_url, headers=AUTH, data={"code": "000000"})
        assert "rejected the 2FA code" in await r.text()

        r = await client.post(
            verify_url, headers=AUTH, data={"code": "123456"}, allow_redirects=False
        )
        assert r.status == 302
        assert "Added%20jane_doe" in r.headers["Location"]

        # The token is single-use.
        r = await client.get(verify_url, headers=AUTH, allow_redirects=False)
        assert "expired" in r.headers["Location"]

    run(store, settings, scenario)
    assert store.names() == ["jane_doe"]
    assert no_go2rtc[-1] == ["jane_doe"]


def test_delete_account(store, settings, no_go2rtc):
    add_account(store, "home", [])

    async def scenario(client):
        r = await client.post("/accounts/home/delete", headers=AUTH, allow_redirects=False)
        assert r.status == 302

    run(store, settings, scenario)
    assert store.names() == []
    assert no_go2rtc[-1] == []


def test_snapshot_needs_key_not_password(store, settings, monkeypatch):
    add_account(store, "home", [{"id": "7", "name": "Garage"}])

    async def scenario(client):
        async def fake_get(account, camera_id):
            assert (account, camera_id) == ("home", "7")
            return b"\xff\xd8jpeg"

        monkeypatch.setattr(client.server.app[web.SNAPSHOTS], "get", fake_get)
        assert (await client.get("/snapshot/garage.jpg?key=wrong")).status == 403
        assert (await client.get("/snapshot/nope.jpg?key=k3y")).status == 404
        r = await client.get("/snapshot/garage.jpg?key=k3y")
        assert r.status == 200
        assert r.content_type == "image/jpeg"
        assert await r.read() == b"\xff\xd8jpeg"

    run(store, settings, scenario)


def test_clips_need_key_or_login(store, settings, data):
    clips = data / "clips"
    (clips / "garage/2026-11-05").mkdir(parents=True)
    (clips / "garage/2026-11-05/100000_local1.mp4").write_bytes(b"mp4")
    (data / "secret.mp4").write_bytes(b"nope")
    settings.clips_dir = str(clips)

    async def scenario(client):
        url = "/clips/garage/2026-11-05/100000_local1.mp4"
        assert (await client.get(url)).status == 401
        assert (await client.get(url + "?key=wrong")).status == 401
        r = await client.get(url + "?key=k3y")
        assert r.status == 200 and await r.read() == b"mp4"
        assert (await client.get(url, headers=AUTH)).status == 200
        assert (await client.get("/clips/garage/none.mp4?key=k3y")).status == 404
        assert (await client.get("/clips/..%2Fsecret.mp4?key=k3y")).status == 404
        assert (await client.get("/clips/.events.json?key=k3y")).status == 404

    run(store, settings, scenario)


def test_send_test_event(store, settings, monkeypatch):
    add_account(store, "home", [{"id": "7", "name": "Front Door"}])
    sent = []

    async def fake_send(url, payload):
        sent.append((url, payload))
        return True

    monkeypatch.setattr(web, "send_webhook", fake_send)

    async def scenario(client):
        r = await client.post("/events/test", headers=AUTH, allow_redirects=False)
        assert "ha_webhook_url" in r.headers["Location"]
        settings.ha_webhook_url = "http://ha:8123/api/webhook/x"
        r = await client.post("/events/test", headers=AUTH, allow_redirects=False)
        assert "Sent%20a%20test%20event" in r.headers["Location"]

    run(store, settings, scenario)
    assert sent[0][0] == "http://ha:8123/api/webhook/x"
    assert sent[0][1]["stream"] == "front_door" and sent[0][1]["source"] == "test"


def test_dashboard_lists_motion_events(store, settings, data, monkeypatch):
    from blinksync.events import EventLog, MotionEvent

    add_account(store, "home", [{"id": "7", "name": "Front Door"}])
    settings.poll_seconds = 60
    settings.clips_dir = str(data / "clips")
    monkeypatch.setattr(web.EventWatcher, "start", lambda self: None)
    log = EventLog(data / "clips")
    log.add(MotionEvent("local:1:2", "home", "Front Door", "7", "front_door",
                        "2026-11-05T10:00:00+00:00", "local", clip="front_door/a.mp4"))
    log.save()
    (data / "clips/front_door").mkdir(parents=True)
    (data / "clips/front_door/a.mp4").write_bytes(b"mp4")

    async def scenario(client):
        html = await (await client.get("/", headers=AUTH)).text()
        assert "Motion events" in html
        assert 'href="/clips/front_door/a.mp4"' in html
        assert "first check pending" in html

    run(store, settings, scenario)


def test_api_motion(store, settings, monkeypatch):
    add_account(store, "home", [{"id": "7", "name": "Front Door"}, {"id": "9", "name": "Garden"}])
    triggered = []

    def fake_trigger(self, entry, source="notification"):
        triggered.append((entry.name, source))
        return "recording" if len(triggered) == 1 else "busy"

    monkeypatch.setattr(web.Recorder, "trigger", fake_trigger)

    async def scenario(client):
        r = await client.post("/api/motion?key=nope", json={"text": "Garden"})
        assert r.status == 403

        r = await client.post("/api/motion?key=k3y", json={"text": "Motion detected at Garden"})
        assert r.status == 202
        assert await r.json() == {"status": "recording", "camera": "Garden", "stream": "garden"}

        # Header key and form body work too; a repeat reports "busy".
        r = await client.post(
            "/api/motion", headers={"X-Blinksync-Key": "k3y"}, data={"camera": "front_door"}
        )
        assert r.status == 200 and (await r.json())["status"] == "busy"

        r = await client.post("/api/motion?key=k3y", json={"text": "Battery low"})
        assert r.status == 404
        body = await r.json()
        assert body["text"] == "Battery low" and body["cameras"] == ["Front Door", "Garden"]

        r = await client.post(
            "/api/motion?key=k3y", data=b"{nope", headers={"Content-Type": "application/json"}
        )
        assert r.status == 400

    run(store, settings, scenario)
    assert triggered == [("garden", "notification"), ("front_door", "notification")]


def test_record_button(store, settings, monkeypatch):
    add_account(store, "home", [{"id": "9", "name": "Garden"}])
    monkeypatch.setattr(web.Recorder, "trigger", lambda self, entry, source: "recording")

    async def scenario(client):
        html = await (await client.get("/", headers=AUTH)).text()
        assert 'action="/record/garden"' in html and "Record 30 s" in html
        r = await client.post("/record/garden", headers=AUTH, allow_redirects=False)
        assert "Recording%20Garden" in r.headers["Location"]
        r = await client.post("/record/attic", headers=AUTH, allow_redirects=False)
        assert "No%20camera" in r.headers["Location"]
        # Needs the web login, unlike /api/motion.
        assert (await client.post("/record/garden")).status == 401

    run(store, settings, scenario)
