from blinksync import go2rtc
from blinksync.accounts import slugify

from .conftest import add_account


def cam(id_, name):
    return {"id": id_, "name": name, "kind": "default"}


def test_slugify():
    assert slugify("Front Door") == "front_door"
    assert slugify("  Back-yard #2 ") == "back_yard_2"
    assert slugify("!!!") == "camera"


def test_stream_names_single_account(store):
    add_account(store, "home", [cam("1", "Front Door"), cam("2", "Garage")])
    names = [e.name for e in go2rtc.stream_entries(store)]
    assert names == ["front_door", "garage"]


def test_stream_names_prefixed_only_on_collision(store):
    add_account(store, "home", [cam("1", "Front Door"), cam("2", "Garage")])
    add_account(store, "cabin", [cam("9", "Front Door")])
    by_account = {(e.account, e.camera["id"]): e.name for e in go2rtc.stream_entries(store)}
    assert by_account[("home", "1")] == "home_front_door"
    assert by_account[("cabin", "9")] == "cabin_front_door"
    assert by_account[("home", "2")] == "garage"


def test_stream_names_dedupe_same_account(store):
    add_account(store, "home", [cam("1", "Cam"), cam("2", "cam")])
    names = sorted(e.name for e in go2rtc.stream_entries(store))
    assert names == ["home_cam", "home_cam_2"]


def test_render_config(store, settings):
    add_account(store, "home", [cam("123", 'Front "Door"')])
    text = go2rtc.render_config(go2rtc.stream_entries(store), settings, "/opt/b/blinksync")
    assert 'allow_paths: ["/opt/b/blinksync"]' in text
    assert 'password: "pw"' in text
    assert (
        '  front_door: "exec:/opt/b/blinksync stream home 123 --max-minutes 10'
        '#killsignal=15#killtimeout=10"' in text
    )


def test_render_config_without_cameras(store, settings):
    text = go2rtc.render_config([], settings, "/x")
    assert text.rstrip().endswith("streams: {}")


def test_write_config_reports_changes(store, settings, data, monkeypatch):
    monkeypatch.setattr(go2rtc, "blinksync_bin", lambda: "/x/blinksync")
    assert go2rtc.write_config(store, settings) is True
    assert go2rtc.write_config(store, settings) is False
    add_account(store, "home", [cam("1", "Garage")])
    assert go2rtc.write_config(store, settings) is True
    assert (data / "go2rtc.yaml").stat().st_mode & 0o777 == 0o600
