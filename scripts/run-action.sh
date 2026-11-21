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

action_id=$(python3 -c 'import uuid; print(uuid.uuid4().hex)')
mkdir -p "$state_dir/prompts"
prompt_file="$state_dir/prompts/${action_id}.md"
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
  restore_out=$(mktemp)
  if python3 "$scripts_dir/session.py" restore >"$restore_out"; then
    if grep -q '^conflict:' "$restore_out"; then
      omarchy notification send -g "󰕍" "Context Deployment" "Restored the last edit. Newer edits to some files were kept."
    else
      omarchy notification send -g "󰕍" "Context Deployment" "Restored the files changed by the last edit"
    fi
    rm -f "$restore_out"
    exit 0
  fi
  rm -f "$restore_out"
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

embed_context() {
  python3 - "$scripts_dir" "$context_json" "$1" <<'PY'
import json
import sys

sys.path.insert(0, sys.argv[1])
from context import context_for_agent

with open(sys.argv[2], encoding="utf-8") as handle:
    payload = json.load(handle)
if not isinstance(payload, dict):
    payload = {}
json.dump(context_for_agent(payload, sys.argv[3]), sys.stdout, indent=2)
sys.stdout.write("\n")
PY
}

write_screenshot() {
  if [[ -n "$screenshot" && -f "$screenshot" ]]; then
    printf '\nA screenshot of the area around the click is at:\n%s\n\nRead that image first so you can see what they clicked on.\n' "$screenshot"
  fi
}

start_watch() {
  local ready="$1"
  local action="$2"
  rm -f "$ready"
  setsid python3 "$scripts_dir/session.py" watch --ready "$ready" --action "$action" >/dev/null 2>&1 </dev/null &
  local _try
  for _try in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    [[ -f $ready ]] && break
    sleep 0.05
  done
}

record_or_stop() {
  if python3 "$scripts_dir/session.py" record "$@"; then
    return 0
  fi
  omarchy notification send -u critical -g "󰅚" "Context Deployment" "Could not save a restore point, so the edit was not started." || true
  exit 1
}

case "$mode" in
edit)
  {
    printf '%s\n\n' '/omarchy'
    printf '%s\n\n' 'The user Super+right-clicked the desktop and chose Edit. Treat this as an operating-system customization request. Use the omarchy skill. Change only what the request and the clicked area call for. Prefer ~/.config edits; never edit /usr/share/omarchy/.'
    printf 'User request:\n%s\n\n' "$user_prompt"
    printf 'Click context JSON:\n```json\n'
    embed_context edit
    printf '```\n'
    write_screenshot
  } >"$prompt_file"
  record_or_stop --mode edit --prompt="$user_prompt" --context "$context_json" --snapshot
  start_watch "$state_dir/prompts/${action_id}.ready" "$action_id"
  export DESKTOP_CONTEXT_ACTION="$action_id"
  exec "$scripts_dir/launch-agent.sh" "$prompt_file"
  ;;
inspect)
  {
    printf '%s\n\n' '/omarchy'
    printf '%s\n\n' 'The user Super+right-clicked the desktop and chose "What is this?". Identify what they clicked on and explain how they could change it. Use the omarchy skill. Do not edit any files. Do not run commands that change system state. Reading config and the screenshot is fine.'
    printf 'Click context JSON:\n```json\n'
    embed_context inspect
    printf '```\n'
    write_screenshot
  } >"$prompt_file"
  python3 "$scripts_dir/session.py" record --mode inspect --prompt="What is this?" --context "$context_json"
  export DESKTOP_CONTEXT_ACTION="$action_id"
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
    embed_context continue
    printf '```\n'
    write_screenshot
  } >"$prompt_file"
  # First mutating continue after an inspect needs a snapshot; later
  # continues keep the original undo point.
  snapshot_flag=()
  if ! python3 "$scripts_dir/session.py" can-undo; then
    snapshot_flag=(--snapshot)
  fi
  record_or_stop --mode continue --prompt="$user_prompt" --context "$context_json" "${snapshot_flag[@]}"
  start_watch "$state_dir/prompts/${action_id}.ready" "$action_id"
  export DESKTOP_CONTEXT_ACTION="$action_id"
  exec "$scripts_dir/launch-agent.sh" "$prompt_file"
  ;;
esac
