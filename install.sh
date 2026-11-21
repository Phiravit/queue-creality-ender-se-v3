#!/usr/bin/env bash
# Install or update the print queue on the Raspberry Pi.
# Run from the folder that holds app.py:   bash install.sh
# Safe to run again after copying a newer version: it keeps config.env and data/.
set -euo pipefail
cd "$(dirname "$0")"
DIR="$(pwd)"
ME="$(id -un)"

echo "== Print queue install in $DIR (user $ME)"

if ss -ltn 2>/dev/null | grep -q ':8000 ' && ! systemctl is-active --quiet print-queue 2>/dev/null; then
  echo "!! Something else already uses port 8000. Set PORT=... in config.env to another port."
fi

if ! python3 -m venv --help >/dev/null 2>&1 || ! python3 -c "import ensurepip" >/dev/null 2>&1; then
  echo "== Installing python3-venv (needs sudo)"
  sudo apt-get update -qq && sudo apt-get install -y -qq python3-venv
fi
[ -x venv/bin/python ] || python3 -m venv venv
echo "== Installing Python packages"
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt

if [ ! -f config.env ]; then
  cp config.example.env config.env
  chmod 600 config.env
  echo "== Created config.env. Fill in ADMIN_PASSWORD and the two PRINTERx_APIKEY lines."
  if [ -t 0 ]; then
    read -r -p "   Press Enter to open it in nano (Ctrl+O Enter to save, Ctrl+X to exit)... " _
    nano config.env
  fi
fi

echo "== Setting up the service (needs sudo)"
sed -e "s|^User=.*|User=$ME|" \
    -e "s|^WorkingDirectory=.*|WorkingDirectory=$DIR|" \
    -e "s|^ExecStart=.*|ExecStart=$DIR/venv/bin/python app.py|" \
    print-queue.service | sudo tee /etc/systemd/system/print-queue.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable print-queue >/dev/null 2>&1
sudo systemctl restart print-queue
sleep 4

PORT="$(grep -E '^PORT=' config.env | cut -d= -f2 || true)"; PORT="${PORT:-8000}"
if curl -fsS -o /dev/null "http://127.0.0.1:$PORT/login"; then
  echo "== OK: the queue is running on port $PORT"
else
  echo "!! The queue did not answer. Last log lines:"
fi
sudo journalctl -u print-queue -n 15 --no-pager | sed 's/^/   /'
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
echo "== Open http://${IP:-<pi-ip>}:$PORT from a lab computer."
