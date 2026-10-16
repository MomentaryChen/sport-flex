#!/usr/bin/env bash
# Idempotent Cloud Agent build: Python venv, Google Chrome, Playwright system libraries.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export SPORT_FLEX_ENV=development

# PAM sessions read /etc/environment, so agent shells inherit this after a build.
if ! grep -q '^SPORT_FLEX_ENV=' /etc/environment 2>/dev/null; then
  echo 'SPORT_FLEX_ENV=development' | sudo tee -a /etc/environment >/dev/null
fi
sudo tee /etc/profile.d/sport-flex-env.sh >/dev/null <<'EOF'
export SPORT_FLEX_ENV=development
EOF

sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip wget ca-certificates

if ! command -v google-chrome >/dev/null 2>&1; then
  wget -q -O /tmp/google-chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
  sudo apt-get install -y /tmp/google-chrome.deb
fi

python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
sudo .venv/bin/playwright install-deps chromium
