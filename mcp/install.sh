#!/usr/bin/env bash
set -Eeuo pipefail

TUNNEL_ID="${FINANCE_TUNNEL_ID:-}"
INSTALL_DIR=/opt/finance-mcp
CONFIG_DIR=/etc/finance-mcp
STATE_DIR=/var/lib/finance-mcp
SERVICE_USER=finance-mcp
HEALTH_ADDR=127.0.0.1:18081

if [[ ${EUID:-$(id -u)} -eq 0 ]]; then
  echo "Run as your normal user with sudo access, not as root." >&2
  exit 2
fi

need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing required command: $1" >&2; exit 1; }; }
need sudo
need python3

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

echo "== Finance Core MCP install =="
if [[ -z "$TUNNEL_ID" ]]; then
  echo "FINANCE_TUNNEL_ID is required, e.g. export FINANCE_TUNNEL_ID=tunnel_..." >&2
  exit 2
fi
echo "Tunnel: $TUNNEL_ID"

if ! [[ "$TUNNEL_ID" =~ ^tunnel_[0-9a-f]{32}$ ]]; then
  echo "Invalid tunnel id: $TUNNEL_ID" >&2
  exit 2
fi

if ! python3 -m venv --help >/dev/null 2>&1; then
  sudo apt-get update
  sudo apt-get install -y python3-venv
fi

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  sudo useradd --system --home "$STATE_DIR" --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi

sudo install -d -o root -g root -m 0755 "$INSTALL_DIR" "$CONFIG_DIR"
sudo install -d -o "$SERVICE_USER" -g "$SERVICE_USER" -m 0750 "$STATE_DIR" "$STATE_DIR/reports" "$STATE_DIR/backups"

# Install unit files EARLY so systemctl edit/status always has a real unit even if a later step fails.
sudo install -o root -g root -m 0644 "$ROOT_DIR/systemd/finance-mcp-tunnel.service" /etc/systemd/system/finance-mcp-tunnel.service
sudo install -o root -g root -m 0644 "$ROOT_DIR/systemd/finance-daily-summary.service" /etc/systemd/system/finance-daily-summary.service
sudo install -o root -g root -m 0644 "$ROOT_DIR/systemd/finance-daily-summary.timer" /etc/systemd/system/finance-daily-summary.timer
sudo systemctl daemon-reload

# Application files.
sudo rm -rf "$INSTALL_DIR/src" "$INSTALL_DIR/systemd" "$INSTALL_DIR/tests"
sudo cp -a "$ROOT_DIR/src" "$ROOT_DIR/systemd" "$ROOT_DIR/pyproject.toml" "$INSTALL_DIR/"
sudo chown -R root:root "$INSTALL_DIR"

if [[ ! -x "$INSTALL_DIR/.venv/bin/python" ]]; then
  sudo python3 -m venv "$INSTALL_DIR/.venv"
fi
sudo "$INSTALL_DIR/.venv/bin/python" -m pip install --upgrade pip setuptools wheel
sudo "$INSTALL_DIR/.venv/bin/pip" install --upgrade "$INSTALL_DIR"

if [[ ! -f "$CONFIG_DIR/finance.env" ]]; then
  sudo install -o root -g "$SERVICE_USER" -m 0640 "$ROOT_DIR/finance.env.example" "$CONFIG_DIR/finance.env"
else
  sudo chown root:"$SERVICE_USER" "$CONFIG_DIR/finance.env"
  sudo chmod 0640 "$CONFIG_DIR/finance.env"
fi

# Initialize DB before tunnel startup.
sudo -u "$SERVICE_USER" env \
  FINANCE_DB_PATH="$STATE_DIR/finance.sqlite3" \
  FINANCE_REPORT_DIR="$STATE_DIR/reports" \
  "$INSTALL_DIR/.venv/bin/finance" doctor >/dev/null

# Install tunnel-client from PATH or user's Downloads.
TUNNEL_BIN=""
if command -v tunnel-client >/dev/null 2>&1; then
  TUNNEL_BIN="$(command -v tunnel-client)"
elif [[ -x /usr/local/bin/tunnel-client ]]; then
  TUNNEL_BIN=/usr/local/bin/tunnel-client
else
  while IFS= read -r candidate; do
    [[ -x "$candidate" ]] && { TUNNEL_BIN="$candidate"; break; }
  done < <(find "$HOME/Downloads" -maxdepth 4 -type f -name tunnel-client 2>/dev/null | sort -V -r)
fi
if [[ -z "$TUNNEL_BIN" ]]; then
  echo "ERROR: tunnel-client not found in PATH, /usr/local/bin, or ~/Downloads." >&2
  exit 3
fi
sudo install -o root -g root -m 0755 "$TUNNEL_BIN" /usr/local/bin/tunnel-client

# Migrate old secret file name from v1.0.0 if present.
if [[ ! -s "$CONFIG_DIR/runtime.env" && -s "$CONFIG_DIR/tunnel.env" ]]; then
  echo "Migrating /etc/finance-mcp/tunnel.env -> runtime.env"
  sudo cp "$CONFIG_DIR/tunnel.env" "$CONFIG_DIR/runtime.env"
fi

# Persist runtime key. Prefer existing permanent file, then current shell env, otherwise ask once without echo.
if sudo test -s "$CONFIG_DIR/runtime.env" && sudo grep -q '^CONTROL_PLANE_API_KEY=' "$CONFIG_DIR/runtime.env"; then
  echo "Runtime API key: using existing persistent file."
elif [[ -n "${CONTROL_PLANE_API_KEY:-}" ]]; then
  printf 'CONTROL_PLANE_API_KEY=%s\n' "$CONTROL_PLANE_API_KEY" | sudo tee "$CONFIG_DIR/runtime.env" >/dev/null
  echo "Runtime API key: persisted from current shell environment."
else
  echo
  echo "Runtime API key is required for tunnel-client."
  echo "Paste the Restricted Runtime API key with Tunnels Read + Use. Input is hidden."
  IFS= read -r -s -p 'CONTROL_PLANE_API_KEY: ' RUNTIME_KEY </dev/tty || true
  echo
  if [[ -z "${RUNTIME_KEY:-}" ]]; then
    echo "No runtime key entered. Core Finance is installed, but tunnel cannot start." >&2
    sudo systemctl enable --now finance-daily-summary.timer
    exit 4
  fi
  printf 'CONTROL_PLANE_API_KEY=%s\n' "$RUNTIME_KEY" | sudo tee "$CONFIG_DIR/runtime.env" >/dev/null
  unset RUNTIME_KEY
fi
sudo chown root:"$SERVICE_USER" "$CONFIG_DIR/runtime.env"
sudo chmod 0640 "$CONFIG_DIR/runtime.env"
# Remove obsolete old secret file after successful migration/persist.
sudo rm -f "$CONFIG_DIR/tunnel.env"

# Build a fresh validated native profile. Secrets remain env references, never literals.
TMP_PROFILE_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_PROFILE_DIR"' EXIT
HOME="$TMP_PROFILE_DIR" XDG_CONFIG_HOME="$TMP_PROFILE_DIR" \
  /usr/local/bin/tunnel-client init \
    --sample sample_mcp_stdio_local \
    --profile Finance \
    --tunnel-id "$TUNNEL_ID" \
    --health-listen-addr "$HEALTH_ADDR" \
    --mcp-command "$INSTALL_DIR/.venv/bin/finance-mcp-server" >/dev/null
profile_path="$(find "$TMP_PROFILE_DIR" -type f -name 'Finance.yaml' -print -quit)"
if [[ -z "$profile_path" ]]; then
  echo "ERROR: tunnel-client did not generate Finance.yaml" >&2
  exit 5
fi
sudo install -o root -g "$SERVICE_USER" -m 0640 "$profile_path" "$CONFIG_DIR/Finance.yaml"

sudo systemctl daemon-reload
sudo systemctl enable --now finance-daily-summary.timer
sudo systemctl enable finance-mcp-tunnel.service
sudo systemctl restart finance-mcp-tunnel.service

# Load only locally for checks; never print secret.
set -a
# shellcheck disable=SC1091
source <(sudo cat "$CONFIG_DIR/runtime.env")
set +a

echo
echo "== Local Finance core =="
sudo -u "$SERVICE_USER" env \
  FINANCE_DB_PATH="$STATE_DIR/finance.sqlite3" \
  FINANCE_REPORT_DIR="$STATE_DIR/reports" \
  "$INSTALL_DIR/.venv/bin/finance" doctor

echo
echo "== Tunnel doctor =="
/usr/local/bin/tunnel-client doctor --profile-file "$CONFIG_DIR/Finance.yaml" --explain || true

echo
echo "== Runtime key -> tunnel metadata =="
/usr/local/bin/tunnel-client admin tunnels get "$TUNNEL_ID" || true

echo
echo "== systemd =="
systemctl --no-pager --full status finance-mcp-tunnel.service || true
systemctl --no-pager --full status finance-daily-summary.timer || true

echo
echo "Waiting for tunnel readiness..."
ready=0
for _ in $(seq 1 15); do
  if curl -fsS "http://$HEALTH_ADDR/readyz" >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
if [[ "$ready" -eq 1 ]]; then
  echo "READY: http://$HEALTH_ADDR/readyz -> 200"
else
  echo "NOT READY yet. Last service logs:"
  sudo journalctl -u finance-mcp-tunnel.service -n 80 --no-pager || true
fi

echo
echo "Installed permanently:"
echo "  unit: /etc/systemd/system/finance-mcp-tunnel.service"
echo "  runtime key: /etc/finance-mcp/runtime.env (root:finance-mcp 0640)"
echo "  profile: /etc/finance-mcp/Finance.yaml"
echo "  database: /var/lib/finance-mcp/finance.sqlite3"
echo "  autostart: $(systemctl is-enabled finance-mcp-tunnel.service 2>/dev/null || true)"
echo "  active: $(systemctl is-active finance-mcp-tunnel.service 2>/dev/null || true)"
