# Session Kind Taxonomy

Replace boolean session classification flags with a single `kind` field that cleanly separates session types across all three CLI providers.

## Problem

CCS applies Claude-specific concepts (`is_subagent`, `is_continuation`, `hide_when_collapsed`, `chain_root`) to a generic Session dataclass shared by all providers. Codex and opencode always leave these as `False`/empty. Worktree sessions aren't detected at all — they show up as normal sessions with mangled project paths. The result is a confusing mix of boolean flags that only one CLI uses.

## Design

### Session `kind` field

Remove from Session dataclass:
- `is_subagent: bool`
- `is_continuation: bool`
- `hide_when_collapsed: bool`
- `chain_root: str`

Add:
- `kind: str = "primary"`

Keep unchanged:
- `parent_id: str` (for continuation chain linking)
- `continuation_count: int` (for `+N` badge on parent)

Valid values for `kind`:

| Value | Set by | Meaning | Default visibility |
|---|---|---|---|
| `"primary"` | All CLIs | Normal work session | Shown |
| `"continuation"` | Claude only | Continues a parent session | Hidden (collapsed under parent) |
| `"subagent"` | Claude only | Background task | Hidden |
| `"worktree"` | Claude only | Session in a git worktree | Shown with `[W]` badge |

### Detection rules (ClaudeProvider only)

Priority order (first match wins):

1. **Subagent**: file path contains `/subagents/` OR session ID starts with `agent-`
2. **Worktree**: project directory name contains `--claude-worktrees-`
3. **Continuation**: first JSONL entry has type `"summary"` with a different `sessionId` than the filename, OR first user message starts with "This session is being continued"
4. **Primary**: everything else

Codex and opencode: always `kind="primary"`.

### Display

Badges in the session list:

- `primary`: no extra badge, just the CLI badge `[C]`, `[X]`, or `[O]`
- `worktree`: `[W]` badge after the CLI badge — e.g. `[C][W] Fix auth bug`
- `continuation`: `↳` prefix with dimmed text (existing behavior, unchanged)
- `subagent`: hidden by default (existing behavior, unchanged)

### Filtering

Replace boolean checks with `kind` checks:

```python
# Subagent filter
if not self.show_subagents and not q:
    self.filtered = [s for s in self.filtered if s.kind != "subagent"]

# Continuation filter — hide non-root continuations
if not self.show_continuations and not q:
    self.filtered = [s for s in self.filtered if s.kind != "continuation"]
```

Existing toggles (`A` for subagents, `C` for continuations) unchanged. No toggle needed for worktree sessions — they're always shown.

Header counts: `(+N subagent hidden, A to show)` derived from `kind == "subagent"`.

### Server-side (ccs_serve.py)

`scan_sessions_direct()`:
- Skip sessions where path contains `/subagents/` or ID starts with `agent-` (existing behavior, already works)
- Detect worktree by project dir containing `--claude-worktrees-`
- Return `kind` field in session JSON so the client doesn't re-detect
- Continuations are not detected server-side (requires reading file content); client classifies them after receiving scan results

### Code changes

| File | Location | Change |
|---|---|---|
| `ccs.py` | Session dataclass (~line 160) | Remove 4 boolean fields, add `kind: str = "primary"` |
| `ccs.py` | ClaudeProvider.scan_sessions() (~line 880) | Set `kind` using detection rules above |
| `ccs.py` | CodexProvider.scan_sessions() (~line 970) | No change (default `kind="primary"`) |
| `ccs.py` | OpencodeProvider.scan_sessions() (~line 1135) | No change (default `kind="primary"`) |
| `ccs.py` | CCSApp filtering (~line 4350) | Replace `s.is_subagent` with `s.kind == "subagent"`, etc. |
| `ccs.py` | CCSApp session rendering | Add `[W]` badge for `kind == "worktree"` |
| `ccs.py` | HeaderBox reactive (~line 4185) | Count by `kind` instead of `is_subagent` |
| `ccs_serve.py` | scan_sessions_direct() (~line 210) | Add `kind` to returned dicts, detect worktree |
