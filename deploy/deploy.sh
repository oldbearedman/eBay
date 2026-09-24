#!/usr/bin/env bash
# Überträgt den aktuellen Stand auf den Wyse und startet den Dienst neu.
# Aufruf aus dem Projektordner: bash deploy/deploy.sh
set -euo pipefail
HOST="papa@192.168.178.33"
SSH=(ssh -o IdentitiesOnly=yes -i ~/.ssh/familienserver_ed25519 "$HOST")

git ls-files | tar -czf - -T - | "${SSH[@]}" 'mkdir -p ~/ebay-manager && tar -xzf - -C ~/ebay-manager'
"${SSH[@]}" 'set -e
  cd ~/ebay-manager
  [ -d .venv ] || python3 -m venv .venv
  .venv/bin/pip install -q --upgrade pip
  .venv/bin/pip install -q -r requirements.txt
  mkdir -p ~/.config/systemd/user
  cp deploy/ebay-manager.service ~/.config/systemd/user/
  systemctl --user daemon-reload
  systemctl --user enable --now ebay-manager >/dev/null 2>&1
  systemctl --user restart ebay-manager
  sleep 3
  systemctl --user is-active ebay-manager'
