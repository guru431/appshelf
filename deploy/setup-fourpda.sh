#!/bin/bash
# One-time browser prerequisites for the interactive 4PDA login window (Debian).
set -euo pipefail
apt-get update
apt-get install -y --no-install-recommends chromium chromium-sandbox xvfb
