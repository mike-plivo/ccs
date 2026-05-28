# Session Kind Taxonomy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace boolean session classification flags with a single `kind` field across all providers.

**Architecture:** Modify the Session dataclass, update ClaudeProvider detection logic, replace all boolean checks in filtering/display/toggle code, and update ccs_serve.py to return `kind` in scan results.

**Tech Stack:** Python dataclasses, Textual TUI, Rich text rendering

---

### Task 1: Update Session dataclass

**Files:**
- Modify: `ccs.py:160-183` (Session dataclass)

- [ ] **Step 1: Remove old fields, add `kind`**

In the Session dataclass at line 160, replace:

```python
    is_continuation: bool = False
    parent_id: str = ""
    continuation_count: int = 0
    hide_when_collapsed: bool = False
    chain_root: str = ""
    is_subagent: bool = False
```

With:

```python
    kind: str = "primary"          # primary | continuation | subagent | worktree
    parent_id: str = ""
    continuation_count: int = 0
```

- [ ] **Step 2: Verify syntax**

Run: `python3 -c "import py_compile; py_compile.compile('ccs.py', doraise=True)"`

Expected: exits cleanly (will have runtime errors from references to removed fields — that's fine, we fix those in later tasks)

- [ ] **Step 3: Commit**

```bash
git add ccs.py
git commit -m "refactor: replace session boolean flags with kind field"
```

---

### Task 2: Update ClaudeProvider.scan_sessions()

**Files:**
- Modify: `ccs.py:913-922` (Session construction in ClaudeProvider)
- Modify: `ccs.py:840-902` (scanning loop — add worktree detection)

- [ ] **Step 1: Add worktree detection and set `kind`**

In ClaudeProvider.scan_sessions(), the scanning loop iterates over project directories. The project directory path is available as `praw` (the raw directory name like `-Users-mike-GIT-ccs--claude-worktrees-xxx`). The session ID is `sid` and the file path is `jp`.

At line 902, where `is_cont` is computed, add worktree detection right after:

```python
            is_cont = bool(has_cont_text and first_entry_sid and first_entry_sid != sid)
            cont_parent = first_entry_sid if is_cont else ""
```

Add below that:

```python
            # Classify session kind (priority: subagent > worktree > continuation > primary)
            is_subagent = sid.startswith("agent-") or "/subagents/" in str(jp)
            is_worktree = "--claude-worktrees-" in praw
            if is_subagent:
                kind = "subagent"
            elif is_worktree and not is_cont:
                kind = "worktree"
            elif is_cont:
                kind = "continuation"
            else:
                kind = "primary"
```

- [ ] **Step 2: Update Session construction**

Replace lines 913-922:

```python
            out.append(Session(
                id=sid, project_raw=praw, project_display=pdisp,
                summary=summary, first_msg=fm,
                first_msg_long=fm_long, last_msg=lm,
                tag=tag, pinned=pinned,
                mtime=file_mtime, cli="claude", summaries=sums, path=jp,
                msg_count=msg_count,
                is_continuation=is_cont, parent_id=cont_parent,
                is_subagent=sid.startswith("agent-"),
            ))
```

With:

```python
            out.append(Session(
                id=sid, project_raw=praw, project_display=pdisp,
                summary=summary, first_msg=fm,
                first_msg_long=fm_long, last_msg=lm,
                tag=tag, pinned=pinned,
                mtime=file_mtime, cli="claude", summaries=sums, path=jp,
                msg_count=msg_count,
                kind=kind, parent_id=cont_parent,
            ))
```

- [ ] **Step 3: Verify syntax**

Run: `python3 -c "import py_compile; py_compile.compile('ccs.py', doraise=True)"`

- [ ] **Step 4: Commit**

```bash
git add ccs.py
git commit -m "feat: classify Claude sessions by kind (subagent/worktree/continuation/primary)"
```

---

### Task 3: Update remote session construction

**Files:**
- Modify: `ccs.py:440-452` (remote session construction in SessionManager._scan_all)

- [ ] **Step 1: Replace `is_subagent` with `kind` in remote scan**

At line 440-452, where remote sessions are constructed, replace:

```python
                        is_subagent=sid.startswith("agent-"),
```

With:

```python
                        kind=sd.get("kind", "subagent" if sid.startswith("agent-") else "primary"),
```

This reads the `kind` field from the server response (set in Task 6), with a fallback for older servers that don't send it.

- [ ] **Step 2: Verify syntax**

Run: `python3 -c "import py_compile; py_compile.compile('ccs.py', doraise=True)"`

- [ ] **Step 3: Commit**

```bash
git add ccs.py
git commit -m "feat: read session kind from remote scan response"
```

---

### Task 4: Update build_continuation_chains()

**Files:**
- Modify: `ccs.py:482-523` (SessionManager.build_continuation_chains)

- [ ] **Step 1: Replace boolean references with `kind` checks**

Replace the entire method body of `build_continuation_chains` (lines 482-523):

```python
    @staticmethod
    def build_continuation_chains(sessions: List["Session"]) -> None:
        """Walk continuation chains, find latest per chain, set badges.

        Orphan continuations (parent not in session list) are promoted to
        standalone sessions so they aren't hidden.
        The latest session in each chain gets the +N badge and stays visible;
        all other chain members get kind='continuation'.
        """
        by_id = {s.id: s for s in sessions}
        # Promote orphan continuations (parent deleted) to standalone
        for s in sessions:
            if s.kind == "continuation" and s.parent_id and s.parent_id not in by_id:
                s.kind = "primary"
                s.parent_id = ""
        # Build chains: map each session to its root ancestor
        chain_members: dict = {}  # root_id -> [all sessions in chain]
        for s in sessions:
            if s.kind != "continuation" or not s.parent_id:
                continue
            root = s.parent_id
            visited = {s.id}
            while root in by_id and by_id[root].kind == "continuation" and by_id[root].parent_id:
                if root in visited:
                    break
                visited.add(root)
                root = by_id[root].parent_id
            chain_members.setdefault(root, []).append(s)
        # For each chain, find the latest session and set flags
        for root_id, children in chain_members.items():
            # Include the root itself in the chain
            all_in_chain = list(children)
            if root_id in by_id:
                all_in_chain.append(by_id[root_id])
            # Find the session with the most recent mtime
            latest = max(all_in_chain, key=lambda s: s.mtime)
            count = len(all_in_chain) - 1  # exclude the latest from count
            latest.continuation_count = count
            # Mark all non-latest as continuations
            for s in all_in_chain:
                if s.id != latest.id:
                    s.kind = "continuation"
```

- [ ] **Step 2: Verify syntax**

Run: `python3 -c "import py_compile; py_compile.compile('ccs.py', doraise=True)"`

- [ ] **Step 3: Commit**

```bash
git add ccs.py
git commit -m "refactor: build_continuation_chains uses kind field"
```

---

### Task 5: Update display and filtering

**Files:**
- Modify: `ccs.py:2038` (session row rendering — continuation badge)
- Modify: `ccs.py:2092` (session row rendering — dim text)
- Modify: `ccs.py:2046-2050` (add worktree badge)
- Modify: `ccs.py:4303-4325` (filtering logic)
- Modify: `ccs.py:4424-4427` (header subagent count)
- Modify: `ccs.py:5851` (toggle_subagents count)
- Modify: `ccs.py:5861` (archive_continuations filter)

- [ ] **Step 1: Update session row rendering — continuation badge**

At line 2038, replace:

```python
    if show_continuations and s.hide_when_collapsed:
```

With:

```python
    if show_continuations and s.kind == "continuation":
```

- [ ] **Step 2: Add worktree badge after CLI badge**

At lines 2046-2050, after the CLI badge block:

```python
    badge = CLI_BADGES.get(s.cli, "[?]")
    badge_colors = {"claude": "#00cccc", "codex": "#ff8800", "opencode": "#88ff00"}
    text.append(badge, style=Style(color=badge_colors.get(s.cli, "#888888"), bold=True))
    text.append(" ")
```

Replace with:

```python
    badge = CLI_BADGES.get(s.cli, "[?]")
    badge_colors = {"claude": "#00cccc", "codex": "#ff8800", "opencode": "#88ff00"}
    text.append(badge, style=Style(color=badge_colors.get(s.cli, "#888888"), bold=True))
    if s.kind == "worktree":
        text.append("[W]", style=Style(color="#ff00ff", bold=True))
    text.append(" ")
```

- [ ] **Step 3: Update dim text for continuation description**

At line 2092, replace:

```python
    if show_continuations and s.hide_when_collapsed:
```

With:

```python
    if show_continuations and s.kind == "continuation":
```

- [ ] **Step 4: Update filtering logic**

At lines 4303-4325, replace the entire continuation and subagent filter block:

```python
        # Hide chain members unless toggled on (search always shows all)
        if not self.show_continuations and not q:
            self.filtered = [s for s in self.filtered if not s.hide_when_collapsed]
        elif self.show_continuations and not q:
            # Group chain members under the latest session (badge holder)
            visible = [s for s in self.filtered if not s.hide_when_collapsed]
            chain_children: dict = {}
            for s in self.filtered:
                if s.hide_when_collapsed and s.chain_root:
                    chain_children.setdefault(s.chain_root, []).append(s)
            for children in chain_children.values():
                children.sort(key=lambda s: -s.mtime)
            result = []
            for s in visible:
                result.append(s)
                if s.id in chain_children:
                    result.extend(chain_children.pop(s.id))
            for children in chain_children.values():
                result.extend(children)
            self.filtered = result
        # Hide subagent sessions unless toggled on (search always shows all)
        if not self.show_subagents and not q:
            self.filtered = [s for s in self.filtered if not s.is_subagent]
```

With:

```python
        # Hide continuations unless toggled on (search always shows all)
        if not self.show_continuations and not q:
            self.filtered = [s for s in self.filtered if s.kind != "continuation"]
        elif self.show_continuations and not q:
            # Group continuations under their parent (the session with +N badge)
            visible = [s for s in self.filtered if s.kind != "continuation"]
            chain_children: dict = {}
            for s in self.filtered:
                if s.kind == "continuation" and s.parent_id:
                    # Find the chain head (session with continuation_count > 0)
                    head = s.parent_id
                    for v in visible:
                        if v.continuation_count > 0 and any(
                            c.parent_id == v.id or c.parent_id in {m.id for m in self.filtered if m.kind == "continuation"}
                            for c in [s]
                        ):
                            head = v.id
                            break
                    chain_children.setdefault(head, []).append(s)
            for children in chain_children.values():
                children.sort(key=lambda s: -s.mtime)
            result = []
            for s in visible:
                result.append(s)
                if s.id in chain_children:
                    result.extend(chain_children.pop(s.id))
            for children in chain_children.values():
                result.extend(children)
            self.filtered = result
        # Hide subagent sessions unless toggled on (search always shows all)
        if not self.show_subagents and not q:
            self.filtered = [s for s in self.filtered if s.kind != "subagent"]
```

- [ ] **Step 5: Update header subagent count**

At lines 4424-4427, replace:

```python
        if not self.show_subagents:
            header.hidden_subagents = sum(1 for s in self.sessions if s.is_subagent)
```

With:

```python
        if not self.show_subagents:
            header.hidden_subagents = sum(1 for s in self.sessions if s.kind == "subagent")
```

- [ ] **Step 6: Update toggle_subagents count**

At line 5851, replace:

```python
        total = sum(1 for s in self.sessions if s.is_subagent)
```

With:

```python
        total = sum(1 for s in self.sessions if s.kind == "subagent")
```

- [ ] **Step 7: Update archive_continuations filter**

At line 5861, replace:

```python
        cont_sessions = [s for s in self.sessions if s.hide_when_collapsed]
```

With:

```python
        cont_sessions = [s for s in self.sessions if s.kind == "continuation"]
```

- [ ] **Step 8: Verify syntax**

Run: `python3 -c "import py_compile; py_compile.compile('ccs.py', doraise=True)"`

- [ ] **Step 9: Commit**

```bash
git add ccs.py
git commit -m "feat: update display and filtering to use session kind"
```

---

### Task 6: Update ccs_serve.py

**Files:**
- Modify: `ccs_serve.py:255-275` (Claude session scanning in scan_sessions_direct)

- [ ] **Step 1: Add `kind` field and worktree detection to Claude session scan**

In `scan_sessions_direct()`, the Claude scanning block builds session dicts around line 270. Currently:

```python
                sessions.append({
                    "id": sid,
                    "cli": "claude",
                    "summary": first_msg or "",
                    "first_msg": first_msg,
                    "last_msg": last_msg,
                    "project": proj_display,
                    "mtime": mtime,
                })
```

Replace with:

```python
                # Classify session kind
                is_worktree = "--claude-worktrees-" in str(jsonl.parent.name)
                kind = "worktree" if is_worktree else "primary"

                sessions.append({
                    "id": sid,
                    "cli": "claude",
                    "summary": first_msg or "",
                    "first_msg": first_msg,
                    "last_msg": last_msg,
                    "project": proj_display,
                    "mtime": mtime,
                    "kind": kind,
                })
```

Note: subagents are already skipped by the `sid.startswith("agent-")` check earlier. Continuations are detected client-side. Only worktree vs primary needs to be set here.

- [ ] **Step 2: Add `kind` to Codex and opencode session dicts**

In the Codex session block (around line 300), add `"kind": "primary"` to the dict:

```python
                    sessions.append({
                        "id": row["id"],
                        "cli": "codex",
                        ...
                        "mtime": row["updated_at"] or 0,
                        "kind": "primary",
                    })
```

In the opencode session block (around line 365), add `"kind": "primary"` to the dict:

```python
                        sessions.append({
                            "id": sid,
                            "cli": "opencode",
                            ...
                            "mtime": mtime,
                            "kind": "primary",
                        })
```

- [ ] **Step 3: Verify syntax**

Run: `python3 -c "import py_compile; py_compile.compile('ccs_serve.py', doraise=True)"`

- [ ] **Step 4: Commit**

```bash
git add ccs_serve.py
git commit -m "feat: return session kind in scan results"
```

---

### Task 7: Final verification and version bump

**Files:**
- Modify: `ccs.py:80` (VERSION)
- Modify: `pyproject.toml:7` (version)

- [ ] **Step 1: Grep for any remaining references to removed fields**

Run: `grep -n "is_subagent\|is_continuation\|hide_when_collapsed\|chain_root" ccs.py ccs_serve.py ccs_remote.py`

Expected: zero matches. If any remain, fix them.

- [ ] **Step 2: Compile check all files**

Run: `python3 -c "import py_compile; py_compile.compile('ccs.py', doraise=True); py_compile.compile('ccs_serve.py', doraise=True); py_compile.compile('ccs_remote.py', doraise=True)"`

- [ ] **Step 3: Bump version**

In `ccs.py` line 80, change `VERSION = "1.5.5"` to `VERSION = "1.6.0"`.
In `pyproject.toml` line 7, change `version = "1.5.5"` to `version = "1.6.0"`.

- [ ] **Step 4: Reinstall and smoke test**

```bash
uv tool install --force --no-config '.[remote]'
ccs list
```

Expected: sessions listed without errors, subagent sessions not shown, worktree sessions (if any) have their project paths displayed.

- [ ] **Step 5: Commit, tag, push**

```bash
git add ccs.py ccs_serve.py pyproject.toml
git commit -m "feat: session kind taxonomy — v1.6.0"
git tag v1.6.0
git push origin main --tags
```
