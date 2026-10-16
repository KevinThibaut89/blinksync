#!/usr/bin/env bash
# blinkSync LXC installer for Proxmox VE, in the style of community-scripts.
#
# Run on the Proxmox host as root:
#
#   Straight from GitHub:
#     bash -c "$(curl -fsSL https://raw.githubusercontent.com/KevinThibaut89/blinksync/main/proxmox/blinksync.sh)"
#
#   From a copy of this project on the host (installs that copy instead):
#     bash proxmox/blinksync.sh
#
#   Update an existing container (append "update <CTID>" to either form):
#     bash -c "$(curl -fsSL .../proxmox/blinksync.sh)" _ update <CTID>
#     bash proxmox/blinksync.sh update <CTID>
set -euo pipefail

APP="blinkSync"
REPO="${BLINKSYNC_REPO:-https://github.com/KevinThibaut89/blinksync}"
BRANCH="${BLINKSYNC_BRANCH:-main}"

# Defaults ("Default settings" in the menu).
CT_ID=""
CT_HOSTNAME="blinksync"
CT_DISK_GB=4
CT_CORES=1
CT_RAM_MB=512
CT_BRIDGE="vmbr0"
CT_IP="dhcp"
CT_GW=""
CT_STORAGE=""
TMPL_STORAGE="local"
DEBIAN_RELEASE=12

# -- output ---------------------------------------------------------------
RD=$'\e[31m'; GN=$'\e[32m'; YW=$'\e[33m'; BL=$'\e[36m'; CL=$'\e[0m'; BOLD=$'\e[1m'
msg_info()  { echo -ne " ${YW}…${CL} $1"; }
msg_ok()    { echo -e "\r\e[K ${GN}✓${CL} $1"; }
msg_error() { echo -e "\r\e[K ${RD}✗${CL} $1" >&2; }
die()       { msg_error "$1"; exit 1; }

header() {
  clear
  cat <<'EOF'
    __    ___       __   _____
   / /_  / (_)___  / /__/ ___/__  ______  _____
  / __ \/ / / __ \/ //_/\__ \/ / / / __ \/ ___/
 / /_/ / / / / / / ,<  ___/ / /_/ / / / / /__
/_.___/_/_/_/ /_/_/|_|/____/\__, /_/ /_/\___/
                           /____/
EOF
  echo -e "  ${BOLD}Blink cameras → go2rtc → Home Assistant${CL}\n"
}

trap 'msg_error "Failed at line ${LINENO}: ${BASH_COMMAND}"' ERR

# -- checks ---------------------------------------------------------------
preflight() {
  [[ $EUID -eq 0 ]] || die "Run this as root on the Proxmox host."
  command -v pct >/dev/null || die "pct not found. Run this on the Proxmox VE host, not inside a container."
  command -v whiptail >/dev/null || die "whiptail not found (apt install whiptail)."
}

# -- source code ----------------------------------------------------------
# Produces $SRC_TARBALL: a .tar.gz with the project at its root.
prepare_source() {
  local script_dir="" tmp
  if [[ -n "${BASH_SOURCE[0]:-}" && -f "${BASH_SOURCE[0]}" ]]; then
    script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  fi
  tmp="$(mktemp -d)"
  SRC_TARBALL="${tmp}/blinksync.tar.gz"

  if [[ -n "$script_dir" && -f "${script_dir}/../pyproject.toml" ]]; then
    msg_info "Packing local project ${script_dir%/proxmox}"
    tar -C "${script_dir}/.." -czf "$SRC_TARBALL" \
      --exclude=.venv --exclude=.git --exclude=data --exclude='__pycache__' \
      --exclude='*.egg-info' --exclude=.pytest_cache .
    msg_ok "Packed local project"
  else
    [[ -n "$REPO" ]] || die "No local project found and BLINKSYNC_REPO is empty."
    msg_info "Downloading ${REPO} (${BRANCH})"
    mkdir -p "${tmp}/src"
    curl -fsSL "${REPO%/}/archive/refs/heads/${BRANCH}.tar.gz" \
      | tar -xz -C "${tmp}/src" --strip-components=1
    tar -C "${tmp}/src" -czf "$SRC_TARBALL" .
    msg_ok "Downloaded ${REPO}"
  fi
}

push_and_install() {
  local ctid="$1"
  msg_info "Copying blinkSync into CT ${ctid}"
  pct push "$ctid" "$SRC_TARBALL" /root/blinksync.tar.gz
  pct exec "$ctid" -- bash -c 'rm -rf /opt/blinksync/src && mkdir -p /opt/blinksync/src \
    && tar -xzf /root/blinksync.tar.gz -C /opt/blinksync/src && rm /root/blinksync.tar.gz'
  msg_ok "Copied blinkSync into CT ${ctid}"
  echo
  pct exec "$ctid" -- bash /opt/blinksync/src/proxmox/blinksync-install.sh
  echo
}

# -- settings menus -------------------------------------------------------
pick_storage() {
  local content="$1" title="$2" var="$3" list=() count
  while read -r name type _; do
    list+=("$name" "$type")
  done < <(pvesm status -content "$content" | awk 'NR>1 && $3=="active" {print $1, $2}')
  count=$(( ${#list[@]} / 2 ))
  (( count > 0 )) || die "No active storage supports '${content}'."
  if (( count == 1 )); then
    printf -v "$var" '%s' "${list[0]}"
  else
    local choice
    choice="$(whiptail --title "$title" \
      --menu "Select storage for ${title,,}:" 16 60 6 "${list[@]}" 3>&1 1>&2 2>&3)" || exit 1
    printf -v "$var" '%s' "$choice"
  fi
}

ask() {  # ask <var> <title> <prompt>
  local value
  value="$(whiptail --title "$2" --inputbox "$3" 10 60 "${!1}" 3>&1 1>&2 2>&3)" || exit 1
  printf -v "$1" '%s' "$value"
}

settings_menu() {
  CT_ID="$(pvesh get /cluster/nextid)"
  if whiptail --title "$APP" --yes-button "Default" --no-button "Advanced" \
      --yesno "Use default settings?\n\n\
  CT ID       ${CT_ID}\n  Hostname    ${CT_HOSTNAME}\n  Disk        ${CT_DISK_GB} GB\n\
  CPU / RAM   ${CT_CORES} core / ${CT_RAM_MB} MB\n  Network     ${CT_BRIDGE}, ${CT_IP}\n\
  OS          Debian ${DEBIAN_RELEASE}, unprivileged" 18 60; then
    :
  else
    ask CT_ID "$APP" "Container ID"
    ask CT_HOSTNAME "$APP" "Hostname"
    ask CT_DISK_GB "$APP" "Disk size (GB)"
    ask CT_CORES "$APP" "CPU cores"
    ask CT_RAM_MB "$APP" "RAM (MB)"
    ask CT_BRIDGE "$APP" "Network bridge"
    ask CT_IP "$APP" "IPv4: 'dhcp' or a static address in CIDR form (e.g. 192.168.1.50/24)"
    if [[ "$CT_IP" != "dhcp" ]]; then
      ask CT_GW "$APP" "Gateway (e.g. 192.168.1.1)"
    fi
  fi
  [[ "$CT_ID" =~ ^[0-9]+$ ]] || die "Invalid container ID: ${CT_ID}"
  if pct status "$CT_ID" &>/dev/null; then die "CT ${CT_ID} already exists."; fi
  pick_storage rootdir "Container storage" CT_STORAGE
  pick_storage vztmpl "Template storage" TMPL_STORAGE
}

# -- container ------------------------------------------------------------
ensure_template() {
  msg_info "Updating LXC template list"
  pveam update >/dev/null
  TEMPLATE="$(pveam available --section system \
    | awk -v r="debian-${DEBIAN_RELEASE}-standard" '$2 ~ "^"r {print $2}' | sort -V | tail -1)"
  [[ -n "$TEMPLATE" ]] || die "No Debian ${DEBIAN_RELEASE} template available."
  msg_ok "Using ${TEMPLATE}"
  if ! pveam list "$TMPL_STORAGE" | grep -q "$TEMPLATE"; then
    msg_info "Downloading ${TEMPLATE}"
    pveam download "$TMPL_STORAGE" "$TEMPLATE" >/dev/null
    msg_ok "Downloaded ${TEMPLATE}"
  fi
}

create_container() {
  local net="name=eth0,bridge=${CT_BRIDGE},ip=${CT_IP}"
  [[ -n "$CT_GW" ]] && net+=",gw=${CT_GW}"
  msg_info "Creating CT ${CT_ID}"
  pct create "$CT_ID" "${TMPL_STORAGE}:vztmpl/${TEMPLATE}" \
    --hostname "$CT_HOSTNAME" \
    --cores "$CT_CORES" --memory "$CT_RAM_MB" --swap 512 \
    --rootfs "${CT_STORAGE}:${CT_DISK_GB}" \
    --net0 "$net" \
    --unprivileged 1 --features nesting=1 \
    --onboot 1 --tags blinksync \
    --description "blinkSync: Blink live view for Home Assistant. Web UI on port 8080." \
    >/dev/null
  msg_ok "Created CT ${CT_ID}"

  msg_info "Starting CT ${CT_ID}"
  pct start "$CT_ID"
  for _ in $(seq 1 30); do
    if pct exec "$CT_ID" -- getent hosts deb.debian.org &>/dev/null; then
      msg_ok "CT ${CT_ID} is online"
      return
    fi
    sleep 2
  done
  die "CT ${CT_ID} has no network after 60 s. Check the bridge/IP settings."
}

summary() {
  local ctid="$1" ip password
  ip="$(pct exec "$ctid" -- hostname -I | awk '{print $1}')"
  password="$(pct exec "$ctid" -- /usr/local/bin/blinksync password | awk '/password:/ {print $2}')"
  echo -e "${GN}${BOLD}${APP} is ready.${CL}\n"
  echo -e "  Web UI      ${BL}http://${ip}:8080${CL}"
  echo -e "  Login       admin / ${BOLD}${password}${CL}"
  echo -e "  Streams     rtsp://${ip}:8554/<camera>\n"
  echo -e "  Next: open the web UI, add your Blink account, then add each camera"
  echo -e "  to Home Assistant with the URLs it shows."
  echo -e "  Forgot the password? ${BL}pct exec ${ctid} -- blinksync password${CL}\n"
}

main() {
  preflight
  if [[ "${1:-}" == "update" ]]; then
    local ctid="${2:-}"
    [[ "$ctid" =~ ^[0-9]+$ ]] || die "Usage: $0 update <CTID>"
    pct status "$ctid" | grep -q running || die "CT ${ctid} isn't running."
    header
    prepare_source
    push_and_install "$ctid"
    summary "$ctid"
    return
  fi

  header
  whiptail --title "$APP" --yesno "This creates a new LXC container running blinkSync \
(Blink live view → RTSP/WebRTC for Home Assistant).\n\nContinue?" 11 60 || exit 0
  settings_menu
  prepare_source
  ensure_template
  create_container
  push_and_install "$CT_ID"
  summary "$CT_ID"
}

main "$@"
