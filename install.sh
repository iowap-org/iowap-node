#!/usr/bin/env bash
# Install iowap-node on Linux (systemd). macOS/Windows: see README "Manual install".
set -euo pipefail

REPO_URL="${IOWAP_REPO_URL:-https://github.com/iowap-org/iowap-node.git}"
INSTALL_DIR="${IOWAP_INSTALL_DIR:-$HOME/iowap-node}"
PYTHON_MIN="${IOWAP_PYTHON_MIN:-3.11}"
RELAY_URL="${IOWAP_RELAY_URL:-}"

log() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing: $1"; }

# 1. OS gate
case "$(uname -s)" in
  Linux) ;;
  *) die "this installer supports Linux (systemd). For macOS see docs/node/setup.md." ;;
esac

# 2. Dependencies: check first, optionally install (apt/dnf/pacman).
check_python() {
  if command -v python3 >/dev/null 2>&1; then
    local v
    v="$(python3 -c 'import sys;print(".".join(map(str,sys.version_info[:2])))')"
    python3 -c "import sys;exit(0 if sys.version_info[:2] >= (3,11) else 1)" 2>/dev/null || die "Python >= ${PYTHON_MIN} required (found $v)"
  else
    return 1
  fi
  return 0
}

ensure_deps() {
  need git
  if ! check_python; then
    log "Python ${PYTHON_MIN}+ not found — attempting install"
    if command -v apt-get >/dev/null 2>&1; then
      sudo apt-get update -qq
      sudo apt-get install -y python3 python3-venv python3-pip git
    elif command -v dnf >/dev/null 2>&1; then
      sudo dnf install -y python3 python3-pip python3-venv git
    elif command -v pacman >/dev/null 2>&1; then
      sudo pacman -Sy --noconfirm python python-pip git
    else
      die "no supported package manager; install Python >= ${PYTHON_MIN}, pip, git, venv manually"
    fi
  fi
  # python venv module present?
  python3 -c "import venv" >/dev/null 2>&1 || die "python venv module missing"
}

ensure_deps

# 3. Clone or update
if [ -d "$INSTALL_DIR/.git" ]; then
  log "Updating existing checkout: $INSTALL_DIR"
  git -C "$INSTALL_DIR" pull --ff-only
else
  log "Cloning iowap-node into $INSTALL_DIR"
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

# 4. venv + install
cd "$INSTALL_DIR"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
. .venv/bin/activate
pip install --upgrade pip >/dev/null
pip install -e . >/dev/null

# 5. Config scaffold
mkdir -p "$HOME/.relay"
if [ ! -f "$HOME/.relay/relay_config.json" ] && [ -f relay_config.json.example ]; then
  cp relay_config.json.example "$HOME/.relay/relay_config.json"
  log "Created $HOME/.relay/relay_config.json — edit relay_url before registering"
fi
if [ -n "$RELAY_URL" ]; then
  python3 - "$HOME/.relay/relay_config.json" "$RELAY_URL" <<'PY'
import json,sys
p,u=sys.argv[1],sys.argv[2]
try:
    d=json.load(open(p))
except FileNotFoundError:
    d={}
d.setdefault("relay_url", u)
d["relay_url"]=u
json.dump(d, open(p,"w"), indent=2)
PY
fi

# 6. systemd user unit (daemon)
UNIT_DIR="$HOME/.config/systemd/user"
mkdir -p "$UNIT_DIR"
cat >"$UNIT_DIR/iowap-node-daemon.service" <<EOF
[Unit]
Description=IOWAP node daemon
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/node-daemon
Restart=on-failure
RestartSec=5
Environment=HOME=$HOME

[Install]
WantedBy=default.target
EOF
systemctl --user daemon-reload 2>/dev/null || true
log "Systemd user unit written: iowap-node-daemon.service"

# 7. Next steps
log ""
log "Installed. Next:"
log "  1. Edit $HOME/.relay/relay_config.json (relay_url) if not set"
log "  2. $INSTALL_DIR/.venv/bin/node-cli node register"
log "  3. $INSTALL_DIR/.venv/bin/node-cli capabilities publish default"
log "  4. systemctl --user enable --now iowap-node-daemon"
log ""
log "Upgrade later with:  IOWAP_INSTALL_DIR=$INSTALL_DIR bash $INSTALL_DIR/install.sh   (or re-run curl | bash)"
