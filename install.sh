#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SRC="$SCRIPT_DIR/ccs.py"
DEST="$HOME/.local/bin/ccs.py"
ALIAS_LINE="alias ccs='python3 ~/.local/bin/ccs.py'"

# ── Copy script ──────────────────────────────────────────────────

mkdir -p "$HOME/.local/bin"
cp "$SRC" "$DEST"
chmod +x "$DEST"
echo "Installed ccs.py → $DEST"

# ── Detect shell and rc file ─────────────────────────────────────

SHELL_NAME="$(basename "$SHELL")"
case "$SHELL_NAME" in
    zsh)  RC="$HOME/.zshrc" ;;
    bash) RC="$HOME/.bashrc" ;;
    *)
        echo "Unknown shell: $SHELL_NAME"
        echo "Add this manually to your shell rc:"
        echo "  $ALIAS_LINE"
        exit 0
        ;;
esac

# ── Add alias if not already present ─────────────────────────────

if grep -qF "alias ccs=" "$RC" 2>/dev/null; then
    echo "Alias already exists in $RC — skipping"
else
    printf '\n# ccs - Claude Code Session Manager\n%s\n' "$ALIAS_LINE" >> "$RC"
    echo "Added alias to $RC"
fi

echo ""
echo "Run 'source $RC' or open a new terminal, then type 'ccs'"
