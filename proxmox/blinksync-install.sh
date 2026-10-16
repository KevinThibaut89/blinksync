#!/usr/bin/env bash
# Installs (or updates) blinkSync + go2rtc inside a Debian 12/13 container.
# Normally run by proxmox/blinksync.sh on the Proxmox host, but it works on
# any Debian machine: unpack the project to /opt/blinksync/src and run it.
set -euo pipefail

GO2RTC_VERSION="1.9.14"
APP_DIR="/opt/blinksync"
SRC_DIR="${APP_DIR}/src"
VENV="${APP_DIR}/venv"
DATA_DIR="/etc/blinksync"
HOME_DIR="/var/lib/blinksync"
SVC_USER="blinksync"

say() { echo -e " \e[32m✓\e[0m $*"; }
step() { echo -e " \e[33m…\e[0m $*"; }

[[ -f "${SRC_DIR}/pyproject.toml" ]] || { echo "Project not found in ${SRC_DIR}" >&2; exit 1; }

step "Installing system packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv curl ca-certificates >/dev/null
say "System packages installed"

if ! id "${SVC_USER}" &>/dev/null; then
  useradd --system --home-dir "${HOME_DIR}" --create-home --shell /usr/sbin/nologin "${SVC_USER}"
fi
install -d -o "${SVC_USER}" -g "${SVC_USER}" -m 700 "${DATA_DIR}" "${HOME_DIR}"

step "Installing blinkSync"
[[ -x "${VENV}/bin/python" ]] || python3 -m venv "${VENV}"
"${VENV}/bin/pip" install -q --upgrade pip
"${VENV}/bin/pip" install -q --upgrade "${SRC_DIR}"
say "blinkSync $("${VENV}/bin/python" -c 'import blinksync; print(blinksync.__version__)') installed"

step "Installing go2rtc ${GO2RTC_VERSION}"
case "$(dpkg --print-architecture)" in
  amd64) GO2RTC_ARCH="amd64" ;;
  arm64) GO2RTC_ARCH="arm64" ;;
  *) echo "Unsupported architecture: $(dpkg --print-architecture)" >&2; exit 1 ;;
esac
curl -fsSL -o /usr/local/bin/go2rtc.new \
  "https://github.com/AlexxIT/go2rtc/releases/download/v${GO2RTC_VERSION}/go2rtc_linux_${GO2RTC_ARCH}"
chmod 755 /usr/local/bin/go2rtc.new
mv /usr/local/bin/go2rtc.new /usr/local/bin/go2rtc
say "go2rtc ${GO2RTC_VERSION} installed"

# `blinksync` for root runs as the service user, so files it writes (e.g.
# from `blinksync login`) stay readable by the services.
cat >/usr/local/bin/blinksync <<EOF
#!/bin/sh
export BLINKSYNC_DATA="${DATA_DIR}"
if [ "\$(id -u)" = 0 ]; then
  exec runuser -u ${SVC_USER} -- env BLINKSYNC_DATA="${DATA_DIR}" "${VENV}/bin/blinksync" "\$@"
fi
exec "${VENV}/bin/blinksync" "\$@"
EOF
chmod 755 /usr/local/bin/blinksync

# Creates settings.json with a random web password on first install and
# writes go2rtc.yaml so go2rtc can start.
/usr/local/bin/blinksync init >/dev/null

step "Starting services"
install -m 644 "${SRC_DIR}/proxmox/systemd/go2rtc.service" /etc/systemd/system/go2rtc.service
install -m 644 "${SRC_DIR}/proxmox/systemd/blinksync-web.service" /etc/systemd/system/blinksync-web.service
systemctl daemon-reload
systemctl enable -q go2rtc blinksync-web
systemctl restart go2rtc blinksync-web
say "Services running"

apt-get autoremove -y -qq >/dev/null
apt-get clean
