#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# ccs (Claude Code Session manager) — Installer
# Usage: bash ccs_install.sh
# ============================================================

INSTALL_DIR="$HOME/.claude/shell_functions"
TARGET="$INSTALL_DIR/ccs.zsh"
ZSHRC="$HOME/.zshrc"
SOURCE_LINE='source "$HOME/.claude/shell_functions/ccs.zsh"'

# --- Pre-flight checks ---
echo "Checking dependencies..."

missing=()
command -v python3 >/dev/null 2>&1 || missing+=("python3")
command -v fzf     >/dev/null 2>&1 || missing+=("fzf")
command -v claude  >/dev/null 2>&1 || missing+=("claude (Claude Code CLI)")

if [[ ${#missing[@]} -gt 0 ]]; then
  echo ""
  echo "Missing dependencies:"
  for dep in "${missing[@]}"; do
    echo "  - $dep"
  done
  echo ""
  echo "Install them first, then re-run this script."
  echo "  brew install fzf       # if fzf is missing"
  echo "  brew install python3   # if python3 is missing"
  echo "  npm install -g @anthropic-ai/claude-code  # if claude is missing"
  exit 1
fi

echo "  python3 ... OK"
echo "  fzf     ... OK"
echo "  claude  ... OK"
echo ""

# --- Install ccs.zsh ---
mkdir -p "$INSTALL_DIR"

cat > "$TARGET" << 'CCSEOF'
# ccs - Claude Code Session manager
# Usage:
#   ccs          - fzf picker to browse & resume sessions
#   ccs new <n>  - start a named persistent session
#   ccs tmp      - start an ephemeral session (no persistence)
#   ccs tag [n]  - pick a session via fzf and tag it
#   ccs rm       - pick a session via fzf and delete it
#   ccs pin      - toggle pin on a session (pinned sessions appear first)
#   ccs help     - show help

_CCS_TAGS="$HOME/.claude/session_tags.json"
_CCS_EPHEMERAL="$HOME/.claude/ephemeral_sessions.txt"
_CCS_PINS="$HOME/.claude/session_pins.json"
_CCS_PREVIEW="/tmp/_ccs_preview.py"
_CCS_SCAN="/tmp/_ccs_scan.py"

# Ensure files exist
[[ -f "$_CCS_TAGS" ]] || echo '{}' > "$_CCS_TAGS"
[[ -f "$_CCS_EPHEMERAL" ]] || touch "$_CCS_EPHEMERAL"
[[ -f "$_CCS_PINS" ]] || echo '[]' > "$_CCS_PINS"

# Write shared helper scripts
cat > "$_CCS_SCAN" << 'PYSCAN'
import json, os, glob, datetime, getpass

claude_dir = os.path.expanduser("~/.claude/projects")
username = getpass.getuser()

tags = {}
try:
    with open(os.path.expanduser("~/.claude/session_tags.json")) as f:
        tags = json.load(f)
except: pass

pins = []
try:
    with open(os.path.expanduser("~/.claude/session_pins.json")) as f:
        pins = json.load(f)
except: pass
pin_set = set(pins)

entries = []
for jsonl_path in glob.glob(os.path.join(claude_dir, "*", "*.jsonl")):
    session_id = os.path.basename(jsonl_path).replace(".jsonl", "")
    project_raw = os.path.basename(os.path.dirname(jsonl_path))
    proj = project_raw
    # Generalised home-dir replacement
    home_prefix = "-Users-" + username
    if proj.startswith(home_prefix):
        proj = proj.replace(home_prefix, "~", 1)
    if proj in ("~", "-workdir"): proj_display = proj
    elif proj.startswith("~-"): proj_display = "~/" + proj[2:].replace("-", "/")
    else: proj_display = proj.replace("-", "/")

    tag = tags.get(session_id, "")
    pinned = session_id in pin_set
    summary, first_user_msg, cwd = "", "", ""
    try:
        with open(jsonl_path, "r", errors="replace") as f:
            for line in f:
                try: d = json.loads(line)
                except: continue
                if d.get("type") == "summary": summary = d.get("summary", "")
                elif d.get("type") == "user" and not first_user_msg:
                    cwd = d.get("cwd", "")
                    msg = d.get("message", {})
                    if isinstance(msg, dict):
                        content = msg.get("content", "")
                        if isinstance(content, list):
                            for c in content:
                                if isinstance(c, dict) and c.get("type") == "text":
                                    first_user_msg = c["text"][:120].replace("\n", " ").replace("\t", " "); break
                        elif isinstance(content, str): first_user_msg = content[:120].replace("\n", " ").replace("\t", " ")
                    elif isinstance(msg, str): first_user_msg = msg[:120].replace("\n", " ").replace("\t", " ")
    except: pass

    prefix = ""
    if pinned: prefix += "[pinned] "
    if tag: prefix += "[" + tag + "] "
    display = prefix + (summary or first_user_msg or "(empty session)")

    mod_time = os.path.getmtime(jsonl_path)
    ts = datetime.datetime.fromtimestamp(mod_time).strftime("%Y-%m-%d %H:%M")
    # Sort: pinned first, then by mod_time descending
    entries.append((0 if pinned else 1, -mod_time, f"{ts}\t{session_id}\t{project_raw}\t{proj_display}\t{cwd}\t{display}"))

for _, _, line in sorted(entries):
    print(line)
PYSCAN

cat > "$_CCS_PREVIEW" << 'PYPREVIEW'
import json, sys, os

line = sys.argv[1]
fields = line.split("\t")
if len(fields) < 5:
    print("(invalid entry)")
    sys.exit(0)

session_id = fields[1]
project_raw = fields[2]
cwd = fields[4]
jsonl = os.path.expanduser("~") + "/.claude/projects/" + project_raw + "/" + session_id + ".jsonl"

tags = {}
try:
    with open(os.path.expanduser("~/.claude/session_tags.json")) as f: tags = json.load(f)
except: pass
tag = tags.get(session_id, "")

pins = []
try:
    with open(os.path.expanduser("~/.claude/session_pins.json")) as f: pins = json.load(f)
except: pass
pinned = session_id in pins

summaries = []
first_msg = ""
try:
    with open(jsonl, "r", errors="replace") as f:
        for ln in f:
            try: d = json.loads(ln)
            except: continue
            if d.get("type") == "summary":
                summaries.append(d.get("summary", ""))
            elif d.get("type") == "user" and not first_msg:
                msg = d.get("message", {})
                if isinstance(msg, dict):
                    c = msg.get("content", "")
                    if isinstance(c, list):
                        for x in c:
                            if isinstance(x, dict) and x.get("type") == "text":
                                first_msg = x["text"][:300]; break
                    elif isinstance(c, str): first_msg = c[:300]
                elif isinstance(msg, str): first_msg = msg[:300]
except: pass

if pinned: print("PINNED")
if tag:    print("TAG:     " + tag)
print("SESSION: " + session_id)
print("PROJECT: " + project_raw)
print("CWD:     " + cwd)
print()
if first_msg:
    print("FIRST MESSAGE:")
    print(first_msg)
    print()
if summaries:
    print("CONVERSATION TOPICS:")
    for s in summaries[-10:]:
        print("  - " + s)
else:
    print("(no summaries yet)")
PYPREVIEW

# Purge any leftover ephemeral sessions
_ccs_purge_ephemeral() {
  [[ -s "$_CCS_EPHEMERAL" ]] || return 0
  local claude_dir="$HOME/.claude/projects"
  while IFS= read -r uuid; do
    [[ -z "$uuid" ]] && continue
    for f in "$claude_dir"/*/"$uuid".jsonl(N) "$claude_dir"/*/"$uuid"(N); do
      [[ -e "$f" ]] && rm -rf "$f"
    done
  done < "$_CCS_EPHEMERAL"
  : > "$_CCS_EPHEMERAL"
}

# Shared fzf session picker (returns selected line)
_ccs_fzf() {
  local prompt="${1:-  Claude Session > }"
  local extra_flags="${2:-}"
  local data
  data=$(python3 "$_CCS_SCAN")

  if [[ -z "$data" ]]; then
    echo "No sessions found." >&2
    return 1
  fi

  echo "$data" | \
    fzf --height=80% \
        --reverse \
        $extra_flags \
        --prompt="$prompt" \
        --header=$'  DATE          PROJECT                      TOPIC' \
        --delimiter='\t' \
        --with-nth=1,4,6 \
        --tabstop=4 \
        --preview="python3 $_CCS_PREVIEW {}" \
        --preview-window=right:45%:wrap
}

ccs() {
  # Always purge stale ephemeral sessions first
  _ccs_purge_ephemeral
  case "$1" in
    new)  _ccs_new "${@:2}" ;;
    tmp)  _ccs_tmp "${@:2}" ;;
    tag)  _ccs_tag "${@:2}" ;;
    rm)   _ccs_rm ;;
    pin)  _ccs_pin ;;
    help) _ccs_help ;;
    *)    _ccs_pick ;;
  esac
}

# --- help ---
_ccs_help() {
  cat << 'EOF'
ccs - Claude Code Session manager

Usage:
  ccs              Browse & resume sessions (fzf picker)
  ccs new <name>   Start a named persistent session
  ccs tmp          Start an ephemeral session (auto-deleted on exit)
  ccs tag [name]   Tag an existing session (fzf picker)
  ccs rm           Delete sessions (fzf picker, TAB for multi-select)
  ccs pin          Toggle pin on a session (pinned sort to top)
  ccs help         Show this help
EOF
}

# --- ephemeral session ---
_ccs_tmp() {
  local uuid=$(python3 -c "import uuid; print(uuid.uuid4())")
  echo "$uuid" >> "$_CCS_EPHEMERAL"
  echo "Starting ephemeral session..."
  claude --session-id "$uuid" "$@"
  _ccs_purge_ephemeral
}

# --- named session ---
_ccs_new() {
  if [[ -z "$1" ]]; then
    echo "Usage: ccs new <name>"
    return 1
  fi
  local name="$1"; shift
  local uuid=$(python3 -c "import uuid; print(uuid.uuid4())")

  python3 -c "
import json
tags_file = '$_CCS_TAGS'
try:
    with open(tags_file) as f: tags = json.load(f)
except: tags = {}
tags['$uuid'] = '$name'
with open(tags_file, 'w') as f: json.dump(tags, f, indent=2)
"
  echo "Starting named session: $name ($uuid)"
  claude --session-id "$uuid" "$@"
}

# --- tag existing session ---
_ccs_tag() {
  local tag_name="$1"
  local selected
  selected=$(_ccs_fzf "  Tag session > ") || return 1
  [[ -z "$selected" ]] && return 0

  local chosen_id
  chosen_id=$(echo "$selected" | cut -f2)

  if [[ -z "$tag_name" ]]; then
    printf "Tag name: "
    read -r tag_name
    [[ -z "$tag_name" ]] && { echo "Cancelled."; return 0; }
  fi

  python3 -c "
import json
tags_file = '$_CCS_TAGS'
try:
    with open(tags_file) as f: tags = json.load(f)
except: tags = {}
tags['$chosen_id'] = '$tag_name'
with open(tags_file, 'w') as f: json.dump(tags, f, indent=2)
"
  echo "Tagged session $chosen_id as: $tag_name"
}

# --- pin/unpin session ---
_ccs_pin() {
  local selected
  selected=$(_ccs_fzf "  Pin/unpin session > " "--multi") || return 1
  [[ -z "$selected" ]] && return 0

  echo "$selected" | while IFS= read -r line; do
    local sid=$(echo "$line" | cut -f2)
    python3 -c "
import json
pins_file = '$_CCS_PINS'
try:
    with open(pins_file) as f: pins = json.load(f)
except: pins = []
if '$sid' in pins:
    pins.remove('$sid')
    print('Unpinned: $sid')
else:
    pins.append('$sid')
    print('Pinned: $sid')
with open(pins_file, 'w') as f: json.dump(pins, f, indent=2)
"
  done
}

# --- delete session ---
_ccs_rm() {
  local claude_dir="$HOME/.claude/projects"
  local selected
  selected=$(_ccs_fzf "  Delete session > " "--multi") || return 1
  [[ -z "$selected" ]] && return 0

  echo "$selected" | while IFS= read -r line; do
    local sid=$(echo "$line" | cut -f2)
    local proj=$(echo "$line" | cut -f3)
    local session_file="$claude_dir/$proj/$sid.jsonl"

    if [[ -f "$session_file" ]]; then
      rm -f "$session_file"
      # Remove tag and pin if they exist
      python3 -c "
import json
for fpath, key in [('$_CCS_TAGS', 'dict'), ('$_CCS_PINS', 'list')]:
    try:
        with open(fpath) as f: data = json.load(f)
    except: continue
    if key == 'dict':
        data.pop('$sid', None)
    else:
        data = [x for x in data if x != '$sid']
    with open(fpath, 'w') as f: json.dump(data, f, indent=2)
"
      echo "Deleted: $sid"
    fi
  done
}

# --- fzf picker (browse & resume) ---
_ccs_pick() {
  local selected
  selected=$(_ccs_fzf "  Claude Session > ") || return 1
  [[ -z "$selected" ]] && return 0

  local chosen_id chosen_cwd
  chosen_id=$(echo "$selected" | cut -f2)
  chosen_cwd=$(echo "$selected" | cut -f5)

  echo "Resuming session: $chosen_id"
  if [[ -n "$chosen_cwd" && -d "$chosen_cwd" ]]; then
    (cd "$chosen_cwd" && claude --resume "$chosen_id")
  else
    claude --resume "$chosen_id"
  fi
}
CCSEOF

echo "Installed ccs.zsh to: $TARGET"

# --- Wire up .zshrc ---
if [[ -f "$ZSHRC" ]]; then
  if grep -qF "ccs.zsh" "$ZSHRC"; then
    echo ".zshrc already sources ccs.zsh — skipping."
  else
    echo "" >> "$ZSHRC"
    echo "# Claude Code Session manager" >> "$ZSHRC"
    echo "$SOURCE_LINE" >> "$ZSHRC"
    echo "Added source line to $ZSHRC"
  fi
else
  echo "# Claude Code Session manager" > "$ZSHRC"
  echo "$SOURCE_LINE" >> "$ZSHRC"
  echo "Created $ZSHRC with source line"
fi

echo ""
echo "Done! Restart your terminal or run:"
echo "  source ~/.zshrc"
echo ""
echo "Then try:  ccs help"
