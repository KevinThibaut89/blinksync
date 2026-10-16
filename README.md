# blinkSync

Blink cameras have no local video stream. Live view only comes from Blink's
cloud, over Blink's own protocol. blinkSync runs in a small Proxmox LXC and
turns that into standard **RTSP / WebRTC streams** that Home Assistant can use
like any other IP camera.

```
Home Assistant ──rtsp──► go2rtc ──exec──► blinksync stream ──► Blink cloud ──► camera
                   ▲                                │
                   └── snapshot (no wake-up) ── blinksync web UI :8080
```

- **On demand.** A camera streams only while someone is watching. go2rtc starts
  `blinksync stream` when HA opens the camera and stops it when the last
  viewer leaves. Each viewing session is capped at 10 minutes (configurable),
  so a forgotten dashboard tab can't drain a battery camera.
- **Web UI** for adding Blink accounts, including the 2FA code, and copying each
  camera's stream and snapshot URLs.
- **Snapshots that don't wake the camera.** HA thumbnails use Blink's latest
  cloud thumbnail instead of starting a live view every few seconds.

## Limits

- Battery cameras (Outdoor, Indoor, XT) and doorbells: live view uses a lot of
  battery. This is for "check what's happening", not 24/7 recording.
- Expect about 3–8 s before video starts and a second or two of delay: video
  goes camera → Blink cloud → your LAN.
- Blink ends each live view after about 5 minutes. blinkSync immediately
  requests a new one, so expect a brief glitch at that point.
- Needs internet access and Blink's unofficial API (via
  [blinkpy](https://github.com/fronzbot/blinkpy)). If Blink changes their API,
  streams stop until blinkpy catches up.

## Install on Proxmox

On the Proxmox host, as root:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/KevinThibaut89/blinksync/main/proxmox/blinksync.sh)"
```

It asks for **Default** or **Advanced** settings (CT ID, disk, RAM, bridge,
static IP), then:

1. creates an unprivileged Debian 12 LXC (1 core, 512 MB, 4 GB),
2. installs blinkSync in `/opt/blinksync` and go2rtc 1.9.14,
3. starts the `go2rtc` and `blinksync-web` services,
4. prints the web UI address and the admin password.

**Update** an existing container to the latest code on GitHub. Settings and
accounts are kept:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/KevinThibaut89/blinksync/main/proxmox/blinksync.sh)" _ update <CTID>
```

To install unpushed local changes instead, copy the project to the host
(`scp -r blinkSync root@<proxmox>:/root/`) and run `bash proxmox/blinksync.sh`
(or `bash proxmox/blinksync.sh update <CTID>`) from that copy.

## Set up

1. Open `http://<container-ip>:8080` and log in as `admin` with the printed password.
2. **Add Blink account**: enter your email and password, then the code Blink sends you.
3. Each camera appears with a **Stream** URL (`rtsp://<ip>:8554/<camera>`) and a
   **Snapshot** URL.

### Home Assistant

For each camera:

1. **Settings → Devices & services → Add integration → Generic Camera**.
2. **Still image URL**: the camera's Snapshot URL.
3. **Stream source URL**: the camera's Stream URL. RTSP transport: **TCP**.
4. Leave username and password empty and finish.
5. Open the new camera entity → settings (gear icon) → turn **off** *Preload stream*.

HA 2024.11+ serves the stream to browsers and the companion app over WebRTC
through its built-in go2rtc. Nothing else is needed.

For dashboards, show the snapshot and open the live view on tap. See
[`ha/dashboard-card.yaml`](ha/dashboard-card.yaml). Avoid `camera_view: live`:
it keeps the camera streaming the whole time the dashboard is open.

## Inside the container

| What | Where |
| --- | --- |
| Accounts, settings, generated `go2rtc.yaml` | `/etc/blinksync` (back this up) |
| Code / virtualenv | `/opt/blinksync/src`, `/opt/blinksync/venv` |
| Services | `go2rtc`, `blinksync-web` |
| go2rtc UI (same admin login) | `http://<ip>:1984` |

```bash
blinksync cameras            # stream names, camera ids
blinksync login              # add an account from the terminal instead of the web UI
blinksync password           # show the web UI password
blinksync apply              # regenerate go2rtc.yaml + restart go2rtc
journalctl -u go2rtc -f      # stream logs (blinksync stream writes here)
journalctl -u blinksync-web -f
```

Settings such as `max_minutes`, ports and RTSP credentials live in
`/etc/blinksync/settings.json`. After editing it, run
`systemctl restart blinksync-web` (this also regenerates go2rtc.yaml).

## Troubleshooting

- **Black screen / stream never starts**: check `journalctl -u go2rtc -f` while
  opening the camera. "Blink refused live view" usually means the camera is
  asleep with a low battery, offline, or already in a live view in the Blink app.
- **"Blink needs you to sign in again"**: Blink revoked the token. Use *Sign in
  again* in the web UI.
- **Test a stream without HA** (from any machine with ffmpeg):
  `ffplay -rtsp_transport tcp rtsp://<ip>:8554/<camera>`
- **Test the Blink side directly** in the container:
  `blinksync stream <account> <camera-id> --max-minutes 1 > /tmp/test.ts`, then
  play `test.ts`.

## Security

- The web UI and the go2rtc API (port 1984) need the admin password. go2rtc
  can only run `blinksync`, because the generated config's `exec.allow_paths`
  allows nothing else.
- RTSP (8554) has no password by default. Set `rtsp_username` and
  `rtsp_password` in `settings.json` if your LAN isn't trusted.
- Don't forward any of these ports to the internet.
- `/etc/blinksync/accounts/*.json` holds your Blink login, including the
  password, which blinkpy keeps so it can log in again silently. These files
  are readable only by the `blinksync` user.

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
.venv/bin/pytest

# Run the web UI locally against a scratch data dir
BLINKSYNC_DATA=./data .venv/bin/blinksync init
BLINKSYNC_DATA=./data .venv/bin/blinksync web
```

`blinksync/livestream.py` patches two bugs in blinkpy 0.25.9's livestream
(short reads and the missing liveview token). Re-check those patches before
bumping the blinkpy pin.
