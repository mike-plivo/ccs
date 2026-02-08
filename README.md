# CCS - Claude Code Session Manager

A terminal UI and CLI for browsing, managing, and resuming [Claude Code](https://docs.anthropic.com/en/docs/claude-code) sessions.

**Original idea and first version created by: Varun Wahi**

CCS reads session data from `~/.claude/projects/` and provides a full-featured TUI with session previews, tmux integration, profiles, themes, and bulk operations.


## Requirements

- Python 3.9+
- [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and configured
- Optional: [tmux](https://github.com/tmux/tmux) (for background session management)
- Optional: [git](https://git-scm.com/) (for repository info in session details)

## Install

```bash
# Install dependencies
pip install textual rich

# Clone and install
git clone https://github.com/mike-plivo/ccs.git
cd ccs
./install.sh
```

The install script copies `ccs.py` to `~/.local/bin/` and adds a shell alias. After install, restart your terminal or run `source ~/.zshrc` (or `~/.bashrc`).

### Manual install

```bash
pip install textual rich
cp ccs.py ~/.local/bin/ccs.py
chmod +x ~/.local/bin/ccs.py
alias ccs='python3 ~/.local/bin/ccs.py'
```

## Usage

```bash
ccs                  # Launch interactive TUI
ccs list             # List all sessions
ccs resume <id|tag>  # Resume a session
ccs help             # Show all commands
```

### CLI Commands

```
ccs list                               List all sessions
ccs scan [-n|--dry-run]                Rescan all Claude sessions
ccs resume <id|tag> [-p <profile>]     Resume session
ccs resume <id|tag> --claude <opts>    Resume with raw claude options
ccs new <name>                         New named session
ccs new -e [name]                      Ephemeral session (auto-deleted on exit)
ccs pin/unpin <id|tag>                 Pin/unpin a session
ccs tag <id|tag> <tag>                 Set tag on session
ccs tag rename <oldtag> <newtag>       Rename a tag
ccs untag <id|tag>                     Remove tag
ccs delete <id|tag>                    Delete a session
ccs delete --empty                     Delete all empty sessions
ccs info <id|tag>                      Show session details
ccs search <query>                     Search sessions
ccs export <id|tag>                    Export session as markdown
ccs profile list|info|set|new|delete   Manage profiles
ccs theme list|set                     Manage themes
ccs tmux list                          List running tmux sessions
ccs tmux attach <name>                 Attach to tmux session
ccs tmux kill <name>                   Kill a tmux session
ccs tmux kill --all                    Kill all ccs tmux sessions
```

## TUI Keyboard Shortcuts

### Sessions List

| Key | Action |
|-----|--------|
| `Up/Down` | Navigate sessions |
| `g / G` | Jump to first / last |
| `PgUp/PgDn` | Page up / down |
| `Right` | Open Session View |
| `Enter` | Resume session |
| `n` | New named session |
| `e` | New ephemeral session |
| `p` | Toggle pin |
| `t / T` | Set / remove tag |
| `d` | Delete session |
| `D` | Delete all empty sessions |
| `k / K` | Kill tmux session / all |
| `Space` | Mark / unmark session |
| `u` | Unmark all |
| `s` | Cycle sort mode |
| `/` | Search / filter |
| `S` | Rescan all sessions |
| `r` | Refresh |
| `P` | Profile manager |
| `H` | Change theme |
| `m` | Open menu |
| `?` | Help |

### Session View

| Key | Action |
|-----|--------|
| `Tab` | Switch Info / Preview pane |
| `Up/Down` | Scroll focused pane |
| `Enter` | Resume session |
| `i` | Send text to tmux |
| `k` | Kill tmux session |
| `p` | Toggle pin |
| `t / T` | Set / remove tag |
| `d` | Delete session |
| `Left / Esc` | Back to sessions list |

## Profiles

Profiles store launch configurations (model, permission mode, flags, system prompt, tools, MCP config). Create and manage profiles from the TUI (`P`) or CLI:

```bash
ccs profile list           # List all profiles
ccs profile info <name>    # Show profile details
ccs profile set <name>     # Set active profile
ccs profile new <name>     # Create new profile
ccs profile delete <name>  # Delete a profile
```

## Configuration

All CCS data is stored in `~/.config/ccs/`:

| File | Purpose |
|------|---------|
| `sessions.json` | Session metadata (tags, pins, ephemeral flags) |
| `session_cache.json` | Scan cache for faster startup |
| `ccs_profiles.json` | Profile configurations |
| `ccs_active_profile.txt` | Currently active profile |
| `ccs_theme.txt` | Selected theme |

CCS reads session data from `~/.claude/projects/` but never modifies Claude's own configuration.

## License

MIT
