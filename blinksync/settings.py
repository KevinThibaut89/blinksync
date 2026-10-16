"""Paths and persistent settings.

Everything lives under one data directory (``BLINKSYNC_DATA``, default
``/etc/blinksync``) so the container has a single place to back up:

    settings.json        web UI password, ports, per-view time limit
    go2rtc.yaml          generated; never edit by hand
    accounts/<name>.json Blink credentials (refresh token), mode 0600
    accounts/<name>.cameras.json  camera list cached at login/refresh
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sys
from dataclasses import asdict, dataclass, fields
from pathlib import Path

DEFAULT_DATA_DIR = "/etc/blinksync"


def data_dir() -> Path:
    return Path(os.environ.get("BLINKSYNC_DATA", DEFAULT_DATA_DIR))


def blinksync_bin() -> str:
    """Absolute path of the installed `blinksync` script (used in go2rtc exec:)."""
    candidate = Path(sys.executable).parent / "blinksync"
    if candidate.exists():
        return str(candidate)
    return shutil.which("blinksync") or str(candidate)


@dataclass
class Settings:
    web_username: str = "admin"
    web_password: str = ""
    web_port: int = 8080
    rtsp_port: int = 8554
    webrtc_port: int = 8555
    go2rtc_port: int = 1984
    rtsp_username: str = ""
    rtsp_password: str = ""
    # Secret in snapshot URLs, so Home Assistant can fetch stills without
    # the web UI password.
    snapshot_key: str = ""
    # Upper bound for one viewing session. Battery cameras drain fast while
    # live view is on, so a forgotten dashboard tab must not stream forever.
    max_minutes: int = 10

    @property
    def go2rtc_api(self) -> str:
        return f"http://127.0.0.1:{self.go2rtc_port}"


def write_private(path: Path, text: str) -> None:
    """Atomically write a file readable only by its owner."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(text)
    os.replace(tmp, path)


def settings_path() -> Path:
    return data_dir() / "settings.json"


def load_settings() -> Settings:
    path = settings_path()
    if not path.exists():
        return Settings()
    raw = json.loads(path.read_text())
    known = {f.name for f in fields(Settings)}
    return Settings(**{k: v for k, v in raw.items() if k in known})


def save_settings(settings: Settings) -> None:
    write_private(settings_path(), json.dumps(asdict(settings), indent=2) + "\n")


def init_settings() -> tuple[Settings, bool]:
    """Create settings.json with a random web password if it doesn't exist.

    Returns the settings and whether they were newly created.
    """
    created = not settings_path().exists()
    settings = load_settings()
    if not settings.web_password:
        settings.web_password = secrets.token_urlsafe(12)
    if not settings.snapshot_key:
        settings.snapshot_key = secrets.token_urlsafe(16)
    save_settings(settings)
    return settings, created
