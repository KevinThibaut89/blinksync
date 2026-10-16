"""Web UI: add Blink accounts (with 2FA), see cameras and their stream URLs.

Runs inside the container on `settings.web_port`, protected by HTTP basic
auth (`admin` + the password the installer printed). Account changes rewrite
go2rtc.yaml and restart go2rtc right away, so there is no separate "apply"
step.
"""

from __future__ import annotations

import hmac
import logging
import secrets
import time
from base64 import b64decode
from html import escape
from urllib.parse import quote, urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout, web

from . import __version__, go2rtc
from .accounts import (
    AccountError,
    AccountStore,
    PendingLogin,
    ReauthRequired,
    open_blink,
    slugify,
    valid_account_name,
)
from .settings import Settings, load_settings
from .snapshots import SnapshotCache

_LOGGER = logging.getLogger(__name__)

PENDING_TTL = 600  # seconds a half-finished 2FA login is kept

STORE = web.AppKey("store", AccountStore)
SETTINGS = web.AppKey("settings", Settings)
PENDING = web.AppKey("pending", dict)
SNAPSHOTS = web.AppKey("snapshots", SnapshotCache)

PUBLIC_PREFIXES = ("/healthz", "/snapshot/")


# --------------------------------------------------------------------------
# Security
# --------------------------------------------------------------------------


def _authorized(request: web.Request, settings: Settings) -> bool:
    if not settings.web_password:
        return True
    header = request.headers.get("Authorization", "")
    if not header.startswith("Basic "):
        return False
    try:
        user, _, password = b64decode(header[6:]).decode().partition(":")
    except (ValueError, UnicodeDecodeError):
        return False
    return hmac.compare_digest(user, settings.web_username) & hmac.compare_digest(
        password, settings.web_password
    )


def _same_origin(request: web.Request) -> bool:
    """Reject cross-site form posts (browsers resend basic auth automatically)."""
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if not source:
        return True
    return urlsplit(source).netloc == request.host


@web.middleware
async def security(request: web.Request, handler):
    if request.path.startswith(PUBLIC_PREFIXES):
        return await handler(request)
    if not _authorized(request, request.app[SETTINGS]):
        return web.Response(
            status=401,
            text="Login required",
            headers={"WWW-Authenticate": 'Basic realm="blinkSync", charset="UTF-8"'},
        )
    if request.method == "POST" and not _same_origin(request):
        return web.Response(status=403, text="Cross-site request blocked")
    response = await handler(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------

CSS = """
:root {
  --bg: #f6f7f9; --card: #ffffff; --text: #16181d; --muted: #5d6573;
  --border: #e2e5ea; --accent: #1f6feb; --accent-text: #ffffff;
  --ok-bg: #e7f6ec; --ok-text: #135c2a; --err-bg: #fdecec; --err-text: #8a1c1c;
  --code-bg: #f0f2f5;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0f1115; --card: #171a21; --text: #e6e8ec; --muted: #9aa3b2;
    --border: #2a2f3a; --accent: #4c8dff; --accent-text: #0b0d12;
    --ok-bg: #12301d; --ok-text: #8fdca8; --err-bg: #3a1717; --err-text: #f5a3a3;
    --code-bg: #10131a;
  }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif; }
a { color: var(--accent); }
main { max-width: 960px; margin: 0 auto; padding: 24px 16px 64px; }
header { display: flex; align-items: center; justify-content: space-between;
  gap: 12px; margin-bottom: 20px; }
header h1 { font-size: 20px; margin: 0; }
header h1 a { color: inherit; text-decoration: none; }
h2 { font-size: 16px; margin: 0; }
.card { background: var(--card); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px; margin-bottom: 16px; }
.card-head { display: flex; align-items: center; justify-content: space-between;
  gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
.muted { color: var(--muted); }
.small { font-size: 13px; }
.row { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.btn { display: inline-block; border: 1px solid var(--border); background: var(--card);
  color: var(--text); border-radius: 7px; padding: 6px 12px; font: inherit;
  font-size: 14px; cursor: pointer; text-decoration: none; }
.btn.primary { background: var(--accent); border-color: var(--accent); color: var(--accent-text); }
.btn.danger { color: var(--err-text); }
form.inline { display: inline; margin: 0; }
.flash { border-radius: 8px; padding: 10px 12px; margin-bottom: 16px; }
.flash.ok { background: var(--ok-bg); color: var(--ok-text); }
.flash.err { background: var(--err-bg); color: var(--err-text); }
.pill { font-size: 12px; border-radius: 999px; padding: 2px 8px;
  border: 1px solid var(--border); color: var(--muted); white-space: nowrap; }
.pill.ok { background: var(--ok-bg); color: var(--ok-text); border-color: transparent; }
.pill.err { background: var(--err-bg); color: var(--err-text); border-color: transparent; }
.cams { display: grid; gap: 12px; }
.cam { border: 1px solid var(--border); border-radius: 8px; padding: 12px;
  display: grid; gap: 8px; }
.cam-title { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
.cam-title strong { font-size: 15px; }
.url { display: flex; gap: 6px; align-items: center; min-width: 0; }
.url .label { width: 72px; flex: none; color: var(--muted); font-size: 13px; }
code { background: var(--code-bg); border-radius: 5px; padding: 3px 6px;
  font: 13px/1.4 ui-monospace, SFMono-Regular, Menlo, monospace;
  overflow-x: auto; white-space: nowrap; min-width: 0; flex: 1; }
label { display: grid; gap: 4px; margin-bottom: 12px; font-size: 14px; }
input { font: inherit; padding: 8px 10px; border-radius: 7px; width: 100%;
  border: 1px solid var(--border); background: var(--bg); color: var(--text); }
ol { padding-left: 20px; margin: 8px 0 0; }
li { margin-bottom: 6px; }
.empty { text-align: center; padding: 32px 16px; }
"""

COPY_JS = """
document.addEventListener('click', e => {
  const b = e.target.closest('[data-copy]');
  if (!b) return;
  navigator.clipboard.writeText(b.dataset.copy).then(() => {
    const t = b.textContent; b.textContent = 'Copied'; setTimeout(() => b.textContent = t, 1200);
  });
});
"""


def page(title: str, body: str, request: web.Request | None = None) -> web.Response:
    flash = ""
    if request is not None:
        if msg := request.query.get("msg"):
            flash += f'<div class="flash ok">{escape(msg)}</div>'
        if err := request.query.get("err"):
            flash += f'<div class="flash err">{escape(err)}</div>'
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)}</title><style>{CSS}</style></head>
<body><main>
<header><h1><a href="/">blinkSync</a></h1>
<span class="muted small">v{__version__}</span></header>
{flash}{body}
</main><script>{COPY_JS}</script></body></html>"""
    return web.Response(text=html, content_type="text/html")


def redirect(path: str = "/", msg: str = "", err: str = "") -> web.HTTPFound:
    query = "&".join(f"{k}={quote(v)}" for k, v in (("msg", msg), ("err", err)) if v)
    return web.HTTPFound(f"{path}?{query}" if query else path)


def copy_row(label: str, value: str) -> str:
    v = escape(value)
    return (
        f'<div class="url"><span class="label">{label}</span><code>{v}</code>'
        f'<button class="btn small" type="button" data-copy="{v}">Copy</button></div>'
    )


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


async def go2rtc_running(settings: Settings) -> bool:
    try:
        async with ClientSession(timeout=ClientTimeout(total=2)) as session:
            url = f"{settings.go2rtc_api}/api"
            async with session.get(url, headers=go2rtc.api_headers(settings)) as r:
                return r.status == 200
    except (ClientError, TimeoutError):
        return False


async def apply_config(app: web.Application) -> None:
    if not await go2rtc.apply(app[STORE], app[SETTINGS]):
        _LOGGER.warning("go2rtc.yaml updated but go2rtc could not be restarted")


async def purge_pending(app: web.Application) -> None:
    now = time.monotonic()
    for token, (login, started) in list(app[PENDING].items()):
        if now - started > PENDING_TTL:
            del app[PENDING][token]
            await login.close()


def stream_urls(request: web.Request, settings: Settings, name: str) -> tuple[str, str, str]:
    host = request.url.host
    creds = ""
    if settings.rtsp_username:
        creds = f"{quote(settings.rtsp_username)}:{quote(settings.rtsp_password)}@"
    rtsp = f"rtsp://{creds}{host}:{settings.rtsp_port}/{name}"
    snapshot = (
        f"http://{host}:{settings.web_port}/snapshot/{name}.jpg"
        f"?key={quote(settings.snapshot_key)}"
    )
    preview = f"http://{host}:{settings.go2rtc_port}/stream.html?src={quote(name)}"
    return rtsp, snapshot, preview


# --------------------------------------------------------------------------
# Handlers
# --------------------------------------------------------------------------


async def dashboard(request: web.Request) -> web.Response:
    store, settings = request.app[STORE], request.app[SETTINGS]
    entries = go2rtc.stream_entries(store)
    running = await go2rtc_running(settings)
    status = (
        '<span class="pill ok">go2rtc running</span>'
        if running
        else '<span class="pill err">go2rtc not reachable</span>'
    )

    accounts = store.names()
    if not accounts:
        body = f"""
<div class="card empty">
  <h2>No Blink account yet</h2>
  <p class="muted">Sign in with your Blink email and password. Blink will send
  you a verification code by email or SMS.</p>
  <a class="btn primary" href="/accounts/add">Add Blink account</a>
  <p class="small muted" style="margin-top:16px">{status}</p>
</div>"""
        return page("blinkSync", body, request)

    cards = []
    for account in accounts:
        cams = []
        for entry in (e for e in entries if e.account == account):
            cam = entry.camera
            rtsp, snapshot, preview = stream_urls(request, settings, entry.name)
            battery = cam.get("battery")
            battery_pill = (
                f'<span class="pill">battery {escape(str(battery))}</span>'
                if battery
                else ""
            )
            cams.append(f"""
<div class="cam">
  <div class="cam-title"><strong>{escape(cam["name"])}</strong>
    <span class="pill">{escape(cam.get("model") or cam.get("kind") or "camera")}</span>
    {battery_pill}
    <span class="muted small">{escape(cam.get("sync_module") or "")}</span></div>
  {copy_row("Stream", rtsp)}
  {copy_row("Snapshot", snapshot)}
  <div class="row small"><a href="{escape(preview)}" target="_blank" rel="noopener">
    Open live preview</a><span class="muted">wakes the camera</span></div>
</div>""")
        cam_html = "".join(cams) or '<p class="muted">No cameras on this account.</p>'
        user = escape(store.username(account))
        a = escape(account)
        cards.append(f"""
<section class="card">
  <div class="card-head">
    <div><h2>{a}</h2><div class="muted small">{user}</div></div>
    <div class="row">
      <form class="inline" method="post" action="/accounts/{a}/refresh">
        <button class="btn">Refresh cameras</button></form>
      <a class="btn" href="/accounts/add?account={a}">Sign in again</a>
      <form class="inline" method="post" action="/accounts/{a}/delete"
        onsubmit="return confirm('Remove {a}? Its streams will stop working.')">
        <button class="btn danger">Remove</button></form>
    </div>
  </div>
  <div class="cams">{cam_html}</div>
</section>""")

    help_html = """
<section class="card">
  <h2>Add a camera to Home Assistant</h2>
  <ol class="small">
    <li>Settings → Devices &amp; services → Add integration → <strong>Generic Camera</strong>.</li>
    <li><strong>Still image URL</strong>: the camera's Snapshot URL above. It shows Blink's
      latest thumbnail and doesn't wake the camera.</li>
    <li><strong>Stream source URL</strong>: the camera's Stream URL. RTSP transport: TCP.</li>
    <li>Leave username and password empty. Turn off <em>Preload stream</em> in the camera
      entity settings, otherwise HA keeps the camera streaming.</li>
  </ol>
  <p class="small muted">Live view starts when you open the camera in Home Assistant and
  stops when you close it, or after %d minutes at most.</p>
</section>""" % settings.max_minutes

    body = f"""
<div class="row" style="justify-content:space-between;margin-bottom:16px">
  {status}
  <a class="btn primary" href="/accounts/add">Add Blink account</a>
</div>
{"".join(cards)}
{help_html}"""
    return page("blinkSync", body, request)


def account_form(account: str = "", email: str = "", error: str = "") -> web.Response:
    err = f'<div class="flash err">{escape(error)}</div>' if error else ""
    body = f"""
{err}
<section class="card" style="max-width:440px">
  <h2 style="margin-bottom:12px">Sign in to Blink</h2>
  <form method="post" action="/accounts/add" autocomplete="off">
    <label>Email<input name="email" type="email" required value="{escape(email)}"
      autocomplete="username"></label>
    <label>Password<input name="password" type="password" required
      autocomplete="current-password"></label>
    <label>Account name <span class="muted small">used in stream names; optional</span>
      <input name="account" value="{escape(account)}" placeholder="home"
      pattern="[a-z0-9][a-z0-9_\\-]{{0,31}}"></label>
    <div class="row"><button class="btn primary">Continue</button>
      <a class="btn" href="/">Cancel</a></div>
  </form>
  <p class="small muted">Your login is sent only to Blink. The container keeps it,
  with the token Blink returns, in a file only the blinkSync service can read,
  so it can sign in again without asking you.</p>
</section>"""
    return page("Add Blink account", body)


async def add_account_form(request: web.Request) -> web.Response:
    return account_form(account=request.query.get("account", ""))


async def add_account(request: web.Request) -> web.Response:
    app = request.app
    await purge_pending(app)
    form = await request.post()
    email = str(form.get("email", "")).strip()
    password = str(form.get("password", ""))
    account = str(form.get("account", "")).strip().lower()
    if not account:
        account = slugify(email.split("@")[0], fallback="blink")[:32]
    if not valid_account_name(account):
        return account_form(account, email, "Account name: lowercase letters, digits, - and _.")
    if not email or not password:
        return account_form(account, email, "Email and password are required.")

    login = PendingLogin(app[STORE], account, email, password)
    try:
        needs_code = await login.begin()
    except AccountError as err:
        return account_form(account, email, str(err))
    except Exception:
        _LOGGER.exception("Blink login failed")
        await login.close()
        return account_form(account, email, "Couldn't reach Blink. Check the container's internet access and try again.")

    if needs_code:
        token = secrets.token_urlsafe(16)
        app[PENDING][token] = (login, time.monotonic())
        raise web.HTTPFound(f"/accounts/verify/{token}")
    await finish_account(app, account)
    raise redirect(msg=f"Added {account}.")


def verify_form(token: str, error: str = "") -> web.Response:
    err = f'<div class="flash err">{escape(error)}</div>' if error else ""
    body = f"""
{err}
<section class="card" style="max-width:440px">
  <h2 style="margin-bottom:12px">Enter verification code</h2>
  <p class="small muted">Blink just sent a code to your email or phone.</p>
  <form method="post" action="/accounts/verify/{escape(token)}" autocomplete="off">
    <label>Code<input name="code" inputmode="numeric" autocomplete="one-time-code"
      required autofocus></label>
    <div class="row"><button class="btn primary">Verify</button>
      <a class="btn" href="/">Cancel</a></div>
  </form>
</section>"""
    return page("Verify Blink login", body)


async def verify_page(request: web.Request) -> web.Response:
    token = request.match_info["token"]
    if token not in request.app[PENDING]:
        raise redirect(err="That login expired. Please sign in again.")
    return verify_form(token)


async def verify(request: web.Request) -> web.Response:
    app = request.app
    token = request.match_info["token"]
    await purge_pending(app)
    pending = app[PENDING].get(token)
    if not pending:
        raise redirect(err="That login expired. Please sign in again.")
    login = pending[0]
    code = str((await request.post()).get("code", ""))
    try:
        await login.verify(code)
    except AccountError as err:
        return verify_form(token, str(err))
    except Exception:
        _LOGGER.exception("Blink 2FA verification failed")
        return verify_form(token, "Couldn't reach Blink. Try again.")
    del app[PENDING][token]
    await finish_account(app, login.name)
    raise redirect(msg=f"Added {login.name}.")


async def finish_account(app: web.Application, account: str) -> None:
    app[SNAPSHOTS].forget(account)
    await apply_config(app)


def known_account(request: web.Request) -> str:
    account = request.match_info["account"]
    if not request.app[STORE].exists(account):
        raise redirect(err=f"No account named {account}.")
    return account


async def refresh_account(request: web.Request) -> web.Response:
    account = known_account(request)
    try:
        async with open_blink(request.app[STORE], account, thumbnails=False) as blink:
            count = len(blink.cameras)
    except ReauthRequired:
        raise redirect(err=f"Blink needs you to sign in to {account} again.")
    except AccountError as err:
        raise redirect(err=str(err))
    await finish_account(request.app, account)
    raise redirect(msg=f"{account}: {count} camera(s).")


async def delete_account(request: web.Request) -> web.Response:
    account = known_account(request)
    request.app[STORE].delete(account)
    await finish_account(request.app, account)
    raise redirect(msg=f"Removed {account}.")


async def snapshot(request: web.Request) -> web.Response:
    settings = request.app[SETTINGS]
    key = request.query.get("key", "")
    if not settings.snapshot_key or not hmac.compare_digest(key, settings.snapshot_key):
        raise web.HTTPForbidden()
    name = request.match_info["name"]
    entry = next(
        (e for e in go2rtc.stream_entries(request.app[STORE]) if e.name == name), None
    )
    if entry is None:
        raise web.HTTPNotFound()
    image = await request.app[SNAPSHOTS].get(entry.account, entry.camera["id"])
    if image is None:
        raise web.HTTPNotFound(text="No thumbnail yet")
    return web.Response(
        body=image, content_type="image/jpeg", headers={"Cache-Control": "no-store"}
    )


async def healthz(request: web.Request) -> web.Response:
    return web.Response(text="ok")


# --------------------------------------------------------------------------
# App
# --------------------------------------------------------------------------


async def _on_startup(app: web.Application) -> None:
    # Keep go2rtc.yaml in sync with this install (e.g. after an update moved
    # the venv or changed settings).
    await apply_config(app)


async def _on_cleanup(app: web.Application) -> None:
    for login, _ in app[PENDING].values():
        await login.close()


def create_app(
    store: AccountStore | None = None, settings: Settings | None = None
) -> web.Application:
    app = web.Application(middlewares=[security])
    app[STORE] = store or AccountStore()
    app[SETTINGS] = settings or load_settings()
    app[PENDING] = {}

    async def on_refresh(account: str) -> None:
        await apply_config(app)  # camera list may have changed

    app[SNAPSHOTS] = SnapshotCache(app[STORE], on_refresh=on_refresh)
    app.router.add_get("/", dashboard)
    app.router.add_get("/accounts/add", add_account_form)
    app.router.add_post("/accounts/add", add_account)
    app.router.add_get("/accounts/verify/{token}", verify_page)
    app.router.add_post("/accounts/verify/{token}", verify)
    app.router.add_post("/accounts/{account}/refresh", refresh_account)
    app.router.add_post("/accounts/{account}/delete", delete_account)
    app.router.add_get("/snapshot/{name}.jpg", snapshot)
    app.router.add_get("/healthz", healthz)
    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


def run(host: str = "0.0.0.0", port: int | None = None) -> None:
    app = create_app()
    port = port or app[SETTINGS].web_port
    _LOGGER.info("Web UI on http://%s:%s", host, port)
    web.run_app(app, host=host, port=port, print=None, access_log=None)
