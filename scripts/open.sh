#!/usr/bin/env bash
set -euo pipefail

plugin_dir=$(cd "$(dirname "$0")/.." && pwd)

if ! command -v python3 >/dev/null; then
  omarchy notification send -g "󰀦" "Context Deployment" "python3 is required." || true
  exit 1
fi

exec python3 "$plugin_dir/scripts/context.py" --open
