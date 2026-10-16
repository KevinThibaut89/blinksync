import json

import pytest

from blinksync.accounts import AccountStore
from blinksync.settings import Settings


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("BLINKSYNC_DATA", str(tmp_path))
    return tmp_path


@pytest.fixture
def store(data):
    return AccountStore(data)


@pytest.fixture
def settings():
    return Settings(web_password="pw", snapshot_key="k3y")


def add_account(store: AccountStore, name: str, cameras: list[dict], username="me@x.com"):
    store.dir.mkdir(parents=True, exist_ok=True)
    store.creds_path(name).write_text(json.dumps({"username": username}))
    store.cameras_path(name).write_text(json.dumps(cameras))
