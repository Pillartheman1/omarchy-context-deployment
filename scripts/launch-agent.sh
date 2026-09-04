#!/usr/bin/env bash
# Launch the user's default agent. Always go through omarchy-agent so new
# agent names keep working without this plugin forking their flags.
set -euo pipefail

if [[ ${1:-} == --continue ]]; then
  shift
fi

if (($# < 1)); then
  echo "Usage: launch-agent.sh <prompt-file>" >&2
  exit 1
fi

prompt_file=$1
if [[ ! -f "$prompt_file" ]]; then
  echo "Missing prompt file: $prompt_file" >&2
  exit 1
fi

notify() {
  omarchy notification send -g "󰀦" "Context Deployment" "$1" || true
}

prompt=$(cat "$prompt_file")

# omarchy-agent relocates $HOME launches into ~/Work. Stay in ~/.config so
# desktop edits resolve without that hop.
mkdir -p "$HOME/.config"
cd "$HOME/.config"

agent=$(omarchy-default-agent 2>/dev/null || true)
if [[ -z $agent ]]; then
  notify "Choose a default agent first."
  omarchy-menu summon setup.default.agent >/dev/null 2>&1 || true
  exit 1
fi
if omarchy-cmd-missing "$agent"; then
  notify "$agent is not installed. Pick another default agent."
  omarchy-menu summon setup.default.agent >/dev/null 2>&1 || true
  exit 1
fi

exec omarchy-agent --prompt "$prompt"
