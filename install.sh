#!/usr/bin/env bash
# Install iowap-node on Linux (systemd). macOS/Windows: see README "Manual install".
set -euo pipefail

REPO_URL="${IOWAP_REPO_URL:-https://github.com/iowap-org/iowap-node.git}"
INSTALL_DIR="${IOWAP_INSTALL_DIR:-$HOME/iowap-node}"
PYTHON_MIN="${IOWAP_PYTHON_MIN:-3.11}"
RELAY_URL="${IOWAP_RELAY_URL:-}"
NONINTERACTIVE="${IOWAP_NONINTERACTIVE:-0}"
MODE="${IOWAP_MODE:-}"

log() { printf '%s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing: $1"; }
ask() {
  local var="$1" prompt="$2" def="${3:-}" reply
  if [ "$NONINTERACTIVE" = "1" ]; then
    reply="$def"
  else
    read -r -p "$prompt " reply </dev/tty || reply=""
    reply="${reply:-$def}"
  fi
  printf -v "$var" '%s' "$reply"
}

case "$(uname -s)" in
  Linux) ;;
  *) die "this installer supports Linux (systemd). For macOS see docs/node/setup.md." ;;
esac

check_python() {
  if command -v python3 >/dev/null 2>&1; then
    python3 -c "import sys;exit(0 if sys.version_info[:2] >= (3,11) else 1)" 2>/dev/null || return 1
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
  python3 -c "import venv" >/dev/null 2>&1 || die "python venv module missing"
  log "✓ Dependencies verified (python >= ${PYTHON_MIN}, git, venv)"
}

installed=0
if [ -d "$INSTALL_DIR/.git" ] && [ -d "$INSTALL_DIR/.venv" ]; then
  installed=1
fi

if [ "$installed" = "1" ] && [ -z "$MODE" ]; then
  log "iowap-node already installed in $INSTALL_DIR"
  ask MODE "Choose: [u]pdate / [o]verride / [x]uninstall / [a]bort? (u/o/x/a)" "u"
  case "$MODE" in
    u|U|update) MODE=update ;;
    o|O|override) MODE=override ;;
    x|X|uninstall) MODE=uninstall ;;
    *) MODE=abort ;;
  esac
fi

case "$MODE" in
  abort) log "Aborted."; exit 0 ;;
  uninstall)
    log "Uninstalling..."
    systemctl --user disable --now iowap-node-daemon 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/iowap-node-daemon.service"
    rm -rf "$INSTALL_DIR"
    ask DELRELAY "Remove $HOME/.relay entirely? (y/n)" "n"
    if [[ "$DELRELAY" =~ ^[Yy] ]]; then rm -rf "$HOME/.relay"; fi
    log "Uninstalled."
    log "Note: registration on the relay is NOT removed automatically - delete it manually in Relay/Admin."
    log "Local git sources removed: $INSTALL_DIR"
    exit 0
    ;;
esac

ensure_deps

if [ "$MODE" = "override" ]; then
  rm -rf "$INSTALL_DIR"
fi

if [ -d "$INSTALL_DIR/.git" ]; then
  log "Updating existing checkout: $INSTALL_DIR"
  git -C "$INSTALL_DIR" pull --ff-only
else
  log "Cloning iowap-node into $INSTALL_DIR"
  git clone "$REPO_URL" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
. .venv/bin/activate
pip install --upgrade pip >/dev/null
pip install -e . >/dev/null

if [ "$MODE" != "update" ]; then
  mkdir -p "$HOME/.relay"
  if [ -z "$RELAY_URL" ]; then
    ask RELAY_CHOICE "Relay: [u]rl or [d]iscover (mDNS)? (u/d)" "u"
    case "$RELAY_CHOICE" in
      d|D|discover) RELAY_DISCOVER=1 ;;
      *) ask RELAY_URL "Relay URL (e.g. http://192.168.2.60:8788):" ""
         RELAY_DISCOVER=0 ;;
    esac
  else
    RELAY_DISCOVER=0
  fi

  if [ ! -f "$HOME/.relay/relay_config.json" ] || [ "$MODE" = "override" ]; then
    cp relay_config.json.example "$HOME/.relay/relay_config.json" 2>/dev/null || true
  fi

  # Resolve the relay URL: discovery prints it on stdout, `relay set` pins base_url.
  if [ "${RELAY_DISCOVER:-0}" = "1" ]; then
    DISCOVERED_URL="$("$INSTALL_DIR/.venv/bin/node-cli" relay discover 2>/dev/null | head -n1 || true)"
    RELAY_URL="${DISCOVERED_URL:-}"
  fi

  if [ -n "$RELAY_URL" ]; then
    "$INSTALL_DIR/.venv/bin/node-cli" relay set --server-url "$RELAY_URL" >/dev/null || true
    log "Relay pinned: $RELAY_URL"
  else
    # No URL (discover found nothing): unpin so daemon-side mDNS discovery works.
    "$INSTALL_DIR/.venv/bin/node-cli" relay set --discover >/dev/null 2>&1 || true
    log "No relay URL set - discovery enabled (mDNS)"
  fi

  if [ -n "$RELAY_URL" ]; then
    if [ -f "$HOME/.relay/iowap-agent.json" ]; then
      ask REREG "Node identity already exists. Re-register fresh (new node on server)? (y/n)" "n"
      if [[ "$REREG" =~ ^[Yy] ]]; then
        log "Registering with relay ($RELAY_URL)..."
        "$INSTALL_DIR/.venv/bin/node-cli" node register "$RELAY_URL" --force || log "Registration failed - run manually: node-cli node register $RELAY_URL --force"
      else
        log "Keeping existing node identity - registration skipped"
      fi
    else
      log "Registering with relay ($RELAY_URL)..."
      "$INSTALL_DIR/.venv/bin/node-cli" node register "$RELAY_URL" || log "Registration failed - run manually: node-cli node register $RELAY_URL"
    fi
  fi
fi

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

if [ "$MODE" != "update" ]; then
  ask ENABLE "Enable/start iowap-node-daemon via systemd? (y/n)" "y"
  if [[ "$ENABLE" =~ ^[Yy] ]]; then
    systemctl --user enable --now iowap-node-daemon 2>/dev/null || log "systemctl enable failed (user session?)"
  fi
fi

log ""
log "Done. Next:"
log "  - config: $HOME/.relay/relay_config.json"
log "  - node-cli: $INSTALL_DIR/.venv/bin/node-cli"
log "  - daemon: systemctl --user status iowap-node-daemon"
