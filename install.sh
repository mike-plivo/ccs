#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"

# ── Parse args ───────────────────────────────────────────────────

EXTRAS="remote"
for arg in "$@"; do
    case "$arg" in
        --minimal) EXTRAS="" ;;
        --help|-h)
            echo "Usage: ./install.sh [OPTIONS]"
            echo ""
            echo "Options:"
            echo "  --minimal   Install without remote server dependencies"
            echo "              (websockets, PyJWT, bcrypt)"
            echo "  --help      Show this help"
            echo ""
            echo "Requires: uv (https://docs.astral.sh/uv/)"
            exit 0
            ;;
        *)
            echo "Unknown option: $arg"
            echo "Run './install.sh --help' for usage"
            exit 1
            ;;
    esac
done

# ── Check for uv ─────────────────────────────────────────────────

if ! command -v uv &>/dev/null; then
    echo "Error: uv is required but not installed."
    echo ""
    echo "Install uv:"
    echo "  curl -LsSf https://astral.sh/uv/install.sh | sh"
    echo ""
    echo "Or with Homebrew:"
    echo "  brew install uv"
    exit 1
fi

# ── Clean up old installation ────────────────────────────────────

OLD_FILES=(
    "$HOME/.local/bin/ccs.py"
    "$HOME/.local/bin/ccs_protocol.py"
    "$HOME/.local/bin/ccs_remote.py"
    "$HOME/.local/bin/ccs_serve.py"
)

cleaned=false
for f in "${OLD_FILES[@]}"; do
    if [[ -f "$f" ]]; then
        rm -f "$f"
        cleaned=true
    fi
done
$cleaned && echo "Removed old file-based installation from ~/.local/bin/"

# Remove old alias and shell function sources from rc files
for rc in "$HOME/.zshrc" "$HOME/.bashrc"; do
    [[ -f "$rc" ]] || continue
    changed=false

    # Remove old alias (alias ccs='python3 ~/.local/bin/ccs.py')
    if grep -q "alias ccs=.*ccs\.py" "$rc" 2>/dev/null; then
        grep -v "alias ccs=.*ccs\.py" "$rc" | grep -v "# ccs - Claude Code Session Manager" > "${rc}.tmp"
        mv "${rc}.tmp" "$rc"
        changed=true
    fi

    # Remove old shell function source (source ~/.claude/shell_functions/ccs.zsh)
    if grep -q "shell_functions/ccs\.zsh" "$rc" 2>/dev/null; then
        grep -v "shell_functions/ccs\.zsh" "$rc" > "${rc}.tmp"
        mv "${rc}.tmp" "$rc"
        changed=true
    fi

    $changed && echo "Cleaned up old ccs entries from $rc"
done

# ── Install with uv ─────────────────────────────────────────────

echo "Installing ccs into an isolated virtualenv..."

# Build the install spec
if [[ -n "$EXTRAS" ]]; then
    SPEC="$SCRIPT_DIR[$EXTRAS]"
else
    SPEC="$SCRIPT_DIR"
fi

# Try install; fall back to --no-config if global uv config is broken
if ! uv tool install "$SPEC" --reinstall 2>/dev/null; then
    uv tool install "$SPEC" --reinstall --no-config
fi

echo ""
echo "Installed. Run 'ccs' to start."
echo ""
echo "  Upgrade:    cd $SCRIPT_DIR && git pull && ./install.sh"
echo "  Uninstall:  uv tool uninstall ccs"
