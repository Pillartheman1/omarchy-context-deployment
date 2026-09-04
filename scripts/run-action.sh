#!/usr/bin/env bash
set -euo pipefail

plugin_dir=$(cd "$(dirname "$0")/.." && pwd)
scripts_dir="$plugin_dir/scripts"
state_dir="${XDG_STATE_HOME:-$HOME/.local/state}/omarchy/desktop-context"
mkdir -p "$state_dir"

usage() {
  echo "Usage: run-action.sh <edit|inspect|continue|undo> [prompt] [context.json]" >&2
  exit 1
}

if (($# < 1)); then
  usage
fi

mode=$1
shift

prompt_file="$state_dir/prompt.md"
context_json="$state_dir/context.json"
user_prompt=""
last_prompt=""
last_caption=""
last_mode=""

case "$mode" in
edit | continue)
  if (($# < 1)); then
    usage
  fi
  user_prompt=$1
  context_json=${2:-"$state_dir/context.json"}
  ;;
inspect)
  context_json=${1:-"$state_dir/context.json"}
  ;;
undo)
  if python3 "$scripts_dir/session.py" restore; then
    omarchy notification send -g "󰕍" "Context Deployment" "Restored config from before the last edit"
    exit 0
  fi
  omarchy notification send -u critical -g "󰅚" "Context Deployment" "Undo failed. The last snapshot is unchanged."
  exit 1
  ;;
*)
  usage
  ;;
esac

if [[ ! -f "$context_json" ]]; then
  omarchy notification send -g "󰀦" "Context Deployment" "Missing click context." || true
  echo "Missing click context: $context_json" >&2
  exit 1
fi

screenshot=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8")).get("screenshot") or "")' "$context_json") || {
  omarchy notification send -g "󰀦" "Context Deployment" "Click context is not valid JSON." || true
  exit 1
}
if [[ $mode == continue ]]; then
  eval "$(python3 "$scripts_dir/session.py" last | python3 -c '
import json, sys, shlex
data = json.load(sys.stdin)
for key, dest in (("prompt", "last_prompt"), ("caption", "last_caption"), ("lastMode", "last_mode")):
    print(f"{dest}={shlex.quote(str(data.get(key) or ""))}")
')"
fi

write_screenshot() {
  if [[ -n "$screenshot" && -f "$screenshot" ]]; then
    printf '\nA screenshot of the area around the click is at:\n%s\n\nRead that image first so you can see what they clicked on.\n' "$screenshot"
  fi
}

case "$mode" in
edit)
  {
    printf '%s\n\n' '/omarchy'
    printf '%s\n\n' 'The user Super+right-clicked the desktop and chose Edit. Treat this as an operating-system customization request. Use the omarchy skill. Change only what the request and the clicked area call for. Prefer ~/.config edits; never edit /usr/share/omarchy/.'
    printf 'User request:\n%s\n\n' "$user_prompt"
    printf 'Click context JSON:\n```json\n'
    cat "$context_json"
    printf '\n```\n'
    write_screenshot
  } >"$prompt_file"
  python3 "$scripts_dir/session.py" record --mode edit --prompt="$user_prompt" --context "$context_json" --snapshot
  exec "$scripts_dir/launch-agent.sh" "$prompt_file"
  ;;
inspect)
  {
    printf '%s\n\n' '/omarchy'
    printf '%s\n\n' 'The user Super+right-clicked the desktop and chose "What is this?". Identify what they clicked on and explain how they could change it. Use the omarchy skill. Do not edit any files. Do not run commands that change system state. Reading config and the screenshot is fine.'
    printf 'Click context JSON:\n```json\n'
    cat "$context_json"
    printf '\n```\n'
    write_screenshot
  } >"$prompt_file"
  python3 "$scripts_dir/session.py" record --mode inspect --prompt="What is this?" --context "$context_json"
  exec "$scripts_dir/launch-agent.sh" "$prompt_file"
  ;;
continue)
  {
    printf '%s\n\n' '/omarchy'
    printf '%s\n\n' 'The user Super+right-clicked again and chose Continue. This is a follow-up to the previous desktop-context agent session. Use the omarchy skill. Change only what this follow-up and the new clicked area call for. Prefer ~/.config edits; never edit /usr/share/omarchy/.'
    if [[ -n "$last_mode" || -n "$last_prompt" ]]; then
      printf 'Previous action: %s\n' "${last_mode:-unknown}"
      if [[ -n "$last_caption" ]]; then
        printf 'Previous click: %s\n' "$last_caption"
      fi
      printf 'Previous request:\n%s\n\n' "$last_prompt"
    fi
    printf 'New user request:\n%s\n\n' "$user_prompt"
    printf 'New click context JSON:\n```json\n'
    cat "$context_json"
    printf '\n```\n'
    write_screenshot
  } >"$prompt_file"
  # First mutating continue after an inspect needs a snapshot; later
  # continues keep the original undo point.
  snapshot_flag=()
  if ! python3 "$scripts_dir/session.py" can-undo; then
    snapshot_flag=(--snapshot)
  fi
  python3 "$scripts_dir/session.py" record --mode continue --prompt="$user_prompt" --context "$context_json" "${snapshot_flag[@]}"
  exec "$scripts_dir/launch-agent.sh" "$prompt_file"
  ;;
esac
