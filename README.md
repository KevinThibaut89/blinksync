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
- **Motion clips without a Blink plan or Sync Module 2.** An iPhone Shortcut
  passes Blink's motion notification to Home Assistant, which tells blinkSync
  to record that camera for 30 s. Clips are kept in the container and HA can
  notify you with a picture.

## Limits

- Battery cameras (Outdoor, Indoor, XT) and doorbells: live view uses a lot of
  battery. This is for "check what's happening", not 24/7 recording.
- Expect about 3–8 s before video starts and a second or two of delay: video
  goes camera → Blink cloud → your LAN.
- Blink ends each live view after about 5 minutes. blinkSync immediately
  requests a new one, so expect a brief glitch at that point.
- Motion recordings start about 10 s after the motion, so the first seconds
  are missed. Blink's own notification still arrives first.
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

## Motion recordings

Without a Sync Module 2 or a Blink plan, Blink saves no motion clips at all.
It still sends motion and doorbell notifications to your phone, so blinkSync
records on those:

```
iPhone: Blink notification ──Shortcut──► Home Assistant webhook
Home Assistant ──rest_command──► blinkSync POST /api/motion ──► go2rtc ──► 30 s MP4
blinkSync ──webhook (optional)──► Home Assistant ──► notification with picture
```

blinkSync finds the camera named in the notification text, records
`record_seconds` through go2rtc (sharing the live view if someone is
watching), and pulls a thumbnail out of the clip with ffmpeg. That thumbnail
becomes the camera's Snapshot. Clips go to
`/var/lib/blinksync/clips/<camera>/<date>/` and are listed in the web UI and
by `blinksync events`.

**Set up**: follow [`ha/blink-notification.yaml`](ha/blink-notification.yaml)
(the HA `rest_command`, the automation, and the iPhone Shortcut), then
optionally [`ha/motion-automation.yaml`](ha/motion-automation.yaml) for a
notification with the picture once the clip is saved. Each camera card in the
web UI has a **Record 30 s** button to test recording on its own.

Things to know:

- Recording starts about 10 s after the motion: the notification takes a
  moment, then the live view 3–8 s. You'll miss what happened first.
- Each recording wakes a battery camera for `record_seconds`.
- A camera that's already recording, or finished less than
  `record_cooldown` seconds ago, ignores new requests. That covers repeat
  notifications and several phones in one household.
- If you open the Blink app on the notification, the camera may refuse a
  second live view and the recording fails.

### With a Sync Module 2 or a Blink plan

Then Blink saves clips itself. Set `poll_seconds` (e.g. 60) and blinkSync
checks for new ones (USB stick or cloud), downloads them and sends the same
webhook. Clips that were already there when it first runs are skipped.

### Settings

| Setting | Default | |
| --- | --- | --- |
| `record_seconds` | `30` | Length of each recording. |
| `record_cooldown` | `60` | Seconds after a recording during which the same camera won't record again. |
| `keep_days` | `14` | Delete clips older than this. |
| `max_clips_gb` | `2` | Delete the oldest clips beyond this size. The default container disk is 4 GB. |
| `clips_dir` | `/var/lib/blinksync/clips` | Point it at a mounted NAS share to keep more. |
| `ha_webhook_url` | empty | Home Assistant webhook to call after each clip. |
| `public_url` | `http://<container-ip>:8080` | How HA reaches blinkSync, for the clip links in the webhook. |
| `poll_seconds` | `0` (off) | Check Blink for its own clips; needs a Sync Module 2 or a plan. |

`/api/motion`, clip and thumbnail URLs need the same `?key=` as snapshot URLs
(the web UI login also works for clips).

## Inside the container

| What | Where |
| --- | --- |
| Accounts, settings, generated `go2rtc.yaml` | `/etc/blinksync` (back this up) |
| Motion clips and the event list | `/var/lib/blinksync/clips` |
| Code / virtualenv | `/opt/blinksync/src`, `/opt/blinksync/venv` |
| Services | `go2rtc`, `blinksync-web` |
| go2rtc UI (same admin login) | `http://<ip>:1984` |

```bash
blinksync cameras            # stream names, camera ids
blinksync events             # recent motion events and their clip files
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
- **No motion recordings**: watch `journalctl -u blinksync-web -f` while you
  walk past a camera. Nothing logged: the Shortcut or HA automation didn't
  run (check the automation's trace in HA). "No camera matched" shows the
  notification text it got: rename the camera so its name appears there, or
  send `camera` instead. "No video from …" is the same as a stream that won't
  start (see above).
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

## License

MIT. See [LICENSE](LICENSE).
