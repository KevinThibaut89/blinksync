"""`blinksync` command line.

    blinksync init              create settings.json (prints the web password once)
    blinksync web               run the web UI (systemd: blinksync-web.service)
    blinksync stream ACCT ID    write a camera's live view to stdout (go2rtc exec:)
    blinksync login [ACCT]      add a Blink account from the terminal instead of the web UI
    blinksync cameras           list cameras and their stream names
    blinksync apply             regenerate go2rtc.yaml and restart go2rtc
    blinksync password          show the web UI password
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import os
import sys

from . import go2rtc
from .accounts import AccountError, AccountStore, PendingLogin, slugify, valid_account_name
from .settings import init_settings, load_settings


def _setup_logging(verbose: bool) -> None:
    level = os.environ.get("BLINKSYNC_LOG", "DEBUG" if verbose else "INFO").upper()
    logging.basicConfig(
        level=level,
        stream=sys.stderr,  # stdout is the video stream
        format="%(levelname)s %(name)s: %(message)s",
    )
    if level != "DEBUG":
        logging.getLogger("blinkpy").setLevel(logging.WARNING)


def cmd_init(args) -> int:
    settings, created = init_settings()
    go2rtc.write_config(AccountStore(), settings)
    if created:
        print(settings.web_password)
    return 0


def cmd_web(args) -> int:
    from .web import run

    run(port=args.port)
    return 0


def cmd_stream(args) -> int:
    from .streamer import stream_forever

    max_minutes = args.max_minutes or load_settings().max_minutes
    return stream_forever(
        AccountStore(), args.account, args.camera_id, max_minutes, sys.stdout.buffer
    )


def cmd_login(args) -> int:
    email = input("Blink email: ").strip()
    password = getpass.getpass("Blink password: ")
    account = args.account or slugify(email.split("@")[0], fallback="blink")[:32]
    if not valid_account_name(account):
        print(f"Invalid account name: {account}", file=sys.stderr)
        return 2

    async def run() -> None:
        login = PendingLogin(AccountStore(), account, email, password)
        try:
            if await login.begin():
                await login.verify(input("Blink verification code: "))
        finally:
            await login.close()

    try:
        asyncio.run(run())
    except AccountError as err:
        print(err, file=sys.stderr)
        return 1
    print(f"Saved account '{account}'.")
    return cmd_apply(args)


def cmd_cameras(args) -> int:
    entries = go2rtc.stream_entries(AccountStore())
    if not entries:
        print("No cameras. Add a Blink account first.")
    for e in entries:
        cam = e.camera
        print(f"{e.name:24} {cam['name']:24} {cam.get('model') or '':10} "
              f"account={e.account} id={cam['id']}")
    return 0


def cmd_apply(args) -> int:
    ok = asyncio.run(go2rtc.apply(AccountStore(), load_settings()))
    print(f"Wrote {go2rtc.config_path()}" + ("" if ok else " (go2rtc not restarted)"))
    return 0


def cmd_password(args) -> int:
    settings = load_settings()
    print(f"user: {settings.web_username}\npassword: {settings.web_password}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="blinksync", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init").set_defaults(func=cmd_init)

    p = sub.add_parser("web")
    p.add_argument("--port", type=int)
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("stream")
    p.add_argument("account")
    p.add_argument("camera_id")
    p.add_argument("--max-minutes", type=float)
    p.set_defaults(func=cmd_stream)

    p = sub.add_parser("login")
    p.add_argument("account", nargs="?")
    p.set_defaults(func=cmd_login)

    sub.add_parser("cameras").set_defaults(func=cmd_cameras)
    sub.add_parser("apply").set_defaults(func=cmd_apply)
    sub.add_parser("password").set_defaults(func=cmd_password)

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
