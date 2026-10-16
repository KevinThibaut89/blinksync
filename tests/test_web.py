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
