#!/usr/bin/env bash
# One-shot installer for the NumberHub bot on a fresh Linux server (Rocky/RHEL/Debian).
# Run as root from inside the cloned repo:  bash deploy/install.sh
set -euo pipefail

APP=/opt/smsbot
REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "==> Installing system packages (python3.11, git)…"
if command -v dnf >/dev/null 2>&1; then
    dnf install -y python3.11 python3.11-pip git >/dev/null 2>&1 || dnf install -y python3 python3-pip git
    PY=$(command -v python3.11 || command -v python3)
elif command -v apt-get >/dev/null 2>&1; then
    apt-get update -y && apt-get install -y python3 python3-venv python3-pip git
    PY=$(command -v python3)
else
    PY=$(command -v python3)
fi
echo "    using interpreter: $PY ($($PY --version 2>&1))"

# Place the app at $APP (copy if we cloned elsewhere).
if [ "$REPO_DIR" != "$APP" ]; then
    mkdir -p "$APP"
    cp -r "$REPO_DIR"/. "$APP"/
fi
cd "$APP"

echo "==> Creating virtualenv + installing deps…"
"$PY" -m venv .venv
.venv/bin/pip install --upgrade pip >/dev/null
.venv/bin/pip install -r requirements.txt

echo "==> Installing systemd service…"
cp deploy/smsbot.service /etc/systemd/system/smsbot.service
systemctl daemon-reload
systemctl enable smsbot >/dev/null 2>&1 || true

echo
echo "DONE. Next:"
echo "  1) Create $APP/.env  (copy .env.example and fill the real secrets)"
echo "  2) (optional) copy your existing smsbot.db into $APP to keep balances"
echo "  3) systemctl start smsbot   &&   journalctl -u smsbot -f"
