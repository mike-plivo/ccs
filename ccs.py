#!/usr/bin/env python3
"""
ccs — Claude Code Session Manager
A terminal UI for browsing, managing, and resuming Claude Code sessions.

Usage:
    ccs              Interactive TUI to browse & resume sessions
    ccs new <name>   Start a named persistent session
    ccs tmp          Start an ephemeral session (auto-deleted)
    ccs help         Show help
"""

import curses
import json
import os
import glob
import datetime
import getpass
import subprocess
import sys
import shutil
import uuid as uuid_mod
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

# ── Paths ─────────────────────────────────────────────────────────────

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
TAGS_FILE = CLAUDE_DIR / "session_tags.json"
PINS_FILE = CLAUDE_DIR / "session_pins.json"
EPHEMERAL_FILE = CLAUDE_DIR / "ephemeral_sessions.txt"

# ── Color pair IDs ────────────────────────────────────────────────────

CP_HEADER = 1
CP_BORDER = 2
CP_PIN = 3
CP_TAG = 4
CP_SELECTED = 5
CP_DIM = 6
CP_PROJECT = 7
CP_WARN = 8
CP_NORMAL = 9
CP_INPUT = 10
CP_STATUS = 11
CP_SEL_PIN = 12
CP_SEL_TAG = 13
CP_SEL_PROJ = 14
CP_ACCENT = 15

# ── Launch option definitions ─────────────────────────────────────────

PROFILES_FILE = CLAUDE_DIR / "ccs_profiles.json"

MODELS = [
    ("default", ""),
    ("opus", "claude-opus-4-6"),
    ("sonnet", "claude-sonnet-4-5-20250929"),
    ("haiku", "claude-haiku-4-5-20251001"),
]

PERMISSION_MODES = [
    ("default", ""),
    ("plan", "plan"),
    ("acceptEdits", "acceptEdits"),
    ("dontAsk", "dontAsk"),
    ("bypassPermissions", "bypassPermissions"),
]

# Toggleable flags: (display_name, cli_flag)
TOGGLE_FLAGS = [
    ("--verbose",                        "--verbose"),
    ("--dangerously-skip-permissions",   "--dangerously-skip-permissions"),
    ("--print",                          "--print"),
    ("--continue",                       "--continue"),
    ("--no-session-persistence",         "--no-session-persistence"),
]

# Row types in launch overlay
ROW_PROFILE = "profile"
ROW_MODEL = "model"
ROW_PERMMODE = "permmode"
ROW_TOGGLE = "toggle"
ROW_SYSPROMPT = "sysprompt"
ROW_TOOLS = "tools"
ROW_MCP = "mcp"
ROW_CUSTOM = "custom"
ROW_LAUNCH = "launch"
ROW_SAVE = "save"
ROW_PROF_NAME = "prof_name"
ROW_PROF_SAVE = "prof_save"

# ── Data ──────────────────────────────────────────────────────────────


@dataclass
class Session:
    id: str
    project_raw: str
    project_display: str
    cwd: str
    summary: str
    first_msg: str
    first_msg_long: str
    tag: str
    pinned: bool
    mtime: float
    summaries: List[str] = field(default_factory=list)
    path: str = ""

    @property
    def ts(self) -> str:
        return datetime.datetime.fromtimestamp(self.mtime).strftime("%Y-%m-%d %H:%M")

    @property
    def age(self) -> str:
        delta = datetime.datetime.now() - datetime.datetime.fromtimestamp(self.mtime)
        if delta.days > 365:
            return f"{delta.days // 365}y ago"
        if delta.days > 30:
            return f"{delta.days // 30}mo ago"
        if delta.days > 0:
            return f"{delta.days}d ago"
        hours = delta.seconds // 3600
        if hours > 0:
            return f"{hours}h ago"
        mins = delta.seconds // 60
        return f"{mins}m ago" if mins > 0 else "just now"

    @property
    def label(self) -> str:
        return self.summary or self.first_msg or "(empty session)"

    @property
    def sort_key(self) -> Tuple:
        return (0 if self.pinned else 1, -self.mtime)


# ── Session Manager ───────────────────────────────────────────────────


class SessionManager:
    def __init__(self):
        self.user = getpass.getuser()
        self._ensure()

    def _ensure(self):
        CLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        if not TAGS_FILE.exists():
            TAGS_FILE.write_text("{}")
        if not PINS_FILE.exists():
            PINS_FILE.write_text("[]")
        if not EPHEMERAL_FILE.exists():
            EPHEMERAL_FILE.touch()

    def _load(self, p, default):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            return default

    def _save(self, p, data):
        with open(p, "w") as f:
            json.dump(data, f, indent=2)

    def _decode_proj(self, raw: str) -> str:
        p = raw
        pfx = "-Users-" + self.user
        if p.startswith(pfx):
            p = p.replace(pfx, "~", 1)
        if p in ("~", "-workdir"):
            return p
        if p.startswith("~-"):
            return "~/" + p[2:].replace("-", "/")
        return p.replace("-", "/")

    @staticmethod
    def _extract_text(msg) -> str:
        if isinstance(msg, str):
            return msg
        if isinstance(msg, dict):
            c = msg.get("content", "")
            if isinstance(c, list):
                for x in c:
                    if isinstance(x, dict) and x.get("type") == "text":
                        return x.get("text", "")
            elif isinstance(c, str):
                return c
        return ""

    def scan(self) -> List[Session]:
        tags = self._load(TAGS_FILE, {})
        pins = set(self._load(PINS_FILE, []))
        out: List[Session] = []
        pattern = str(PROJECTS_DIR / "*" / "*.jsonl")

        for jp in glob.glob(pattern):
            sid = os.path.basename(jp).replace(".jsonl", "")
            praw = os.path.basename(os.path.dirname(jp))
            pdisp = self._decode_proj(praw)
            tag = tags.get(sid, "")
            pinned = sid in pins
            summary, fm, fm_long, cwd = "", "", "", ""
            sums: List[str] = []

            try:
                with open(jp, "r", errors="replace") as f:
                    for ln in f:
                        try:
                            d = json.loads(ln)
                        except Exception:
                            continue
                        if d.get("type") == "summary":
                            s = d.get("summary", "")
                            if s:
                                sums.append(s)
                                summary = s
                        elif d.get("type") == "user" and not fm:
                            cwd = d.get("cwd", "")
                            txt = self._extract_text(d.get("message", {}))
                            if txt:
                                fm = txt[:120].replace("\n", " ").replace("\t", " ")
                                fm_long = txt[:800]
            except Exception:
                pass

            out.append(Session(
                id=sid, project_raw=praw, project_display=pdisp,
                cwd=cwd, summary=summary, first_msg=fm,
                first_msg_long=fm_long, tag=tag, pinned=pinned,
                mtime=os.path.getmtime(jp), summaries=sums, path=jp,
            ))

        out.sort(key=lambda s: s.sort_key)
        return out

    def toggle_pin(self, sid: str) -> bool:
        pins = self._load(PINS_FILE, [])
        if sid in pins:
            pins.remove(sid)
            result = False
        else:
            pins.append(sid)
            result = True
        self._save(PINS_FILE, pins)
        return result

    def set_tag(self, sid: str, tag: str):
        tags = self._load(TAGS_FILE, {})
        if tag:
            tags[sid] = tag
        else:
            tags.pop(sid, None)
        self._save(TAGS_FILE, tags)

    def remove_tag(self, sid: str):
        self.set_tag(sid, "")

    def delete(self, s: Session):
        if os.path.exists(s.path):
            os.remove(s.path)
        tags = self._load(TAGS_FILE, {})
        tags.pop(s.id, None)
        self._save(TAGS_FILE, tags)
        pins = self._load(PINS_FILE, [])
        pins = [p for p in pins if p != s.id]
        self._save(PINS_FILE, pins)

    # ── Profile management ──────────────────────────────────────────

    def load_profiles(self) -> List[dict]:
        data = self._load(PROFILES_FILE, [])
        return data if isinstance(data, list) else []

    def save_profile(self, profile: dict):
        profiles = self.load_profiles()
        # Replace if same name exists
        profiles = [p for p in profiles if p.get("name") != profile["name"]]
        profiles.append(profile)
        profiles.sort(key=lambda p: p.get("name", ""))
        self._save(PROFILES_FILE, profiles)

    def delete_profile(self, name: str):
        profiles = self.load_profiles()
        profiles = [p for p in profiles if p.get("name") != name]
        self._save(PROFILES_FILE, profiles)

    def purge_ephemeral(self):
        if not EPHEMERAL_FILE.exists():
            return
        try:
            text = EPHEMERAL_FILE.read_text().strip()
            if not text:
                return
            lines = text.split("\n")
        except Exception:
            return
        for uid in lines:
            uid = uid.strip()
            if not uid:
                continue
            for f in glob.glob(str(PROJECTS_DIR / "*" / f"{uid}.jsonl")):
                try:
                    os.remove(f)
                except Exception:
                    pass
        EPHEMERAL_FILE.write_text("")


# ── TUI Application ──────────────────────────────────────────────────


class CCSApp:
    """Curses-based interactive TUI for session management."""

    def __init__(self, scr):
        self.scr = scr
        self.mgr = SessionManager()
        self.mgr.purge_ephemeral()

        self.sessions: List[Session] = []
        self.filtered: List[Session] = []
        self.cur = 0
        self.scroll = 0
        self.query = ""
        self.mode = "normal"  # normal | search | tag | delete | delete_empty | new | launch | profiles | profile_edit | quick_profile | help
        self.ibuf = ""
        self.delete_label = ""  # label shown in delete confirmation popup
        self.empty_count = 0    # count for delete_empty confirmation

        # Launch options state
        self.launch_session: Optional[Session] = None
        self.launch_rows: List[Tuple[str, int]] = []  # (row_type, index)
        self.launch_cur = 0
        self.launch_model_idx = 0
        self.launch_perm_idx = 0
        self.launch_toggles: List[bool] = [False] * len(TOGGLE_FLAGS)
        self.launch_sysprompt = ""
        self.launch_tools = ""
        self.launch_mcp = ""
        self.launch_custom = ""
        self.launch_editing: Optional[str] = None  # which text field is active
        self.launch_profile_idx = 0  # 0 = (custom), 1+ = saved profiles
        self.launch_save_name = ""   # buffer for saving a profile name

        # Profile manager state
        self.prof_cur = 0             # cursor in profile list
        self.prof_edit_rows: List[Tuple[str, int]] = []
        self.prof_edit_cur = 0        # cursor in profile editor
        self.prof_edit_name = ""      # name field in editor
        self.prof_editing_existing: Optional[str] = None  # original name if editing
        self.prof_delete_confirm = False

        # Quick profile picker state
        self.qprof_cur = 0            # cursor in quick profile picker
        self.qprof_delete_confirm = False

        self.status = ""
        self.status_ttl = 0
        self.exit_action: Optional[Tuple] = None

        self._init_colors()
        self._refresh()

    def _init_colors(self):
        curses.start_color()
        curses.use_default_colors()
        curses.init_pair(CP_HEADER, curses.COLOR_CYAN, -1)
        curses.init_pair(CP_BORDER, curses.COLOR_CYAN, -1)
        curses.init_pair(CP_PIN, curses.COLOR_YELLOW, -1)
        curses.init_pair(CP_TAG, curses.COLOR_GREEN, -1)
        curses.init_pair(CP_SELECTED, curses.COLOR_WHITE, curses.COLOR_BLUE)
        curses.init_pair(CP_DIM, curses.COLOR_WHITE, -1)
        curses.init_pair(CP_PROJECT, curses.COLOR_MAGENTA, -1)
        curses.init_pair(CP_WARN, curses.COLOR_RED, -1)
        curses.init_pair(CP_NORMAL, curses.COLOR_WHITE, -1)
        curses.init_pair(CP_INPUT, curses.COLOR_YELLOW, -1)
        curses.init_pair(CP_STATUS, curses.COLOR_GREEN, -1)
        curses.init_pair(CP_SEL_PIN, curses.COLOR_YELLOW, curses.COLOR_BLUE)
        curses.init_pair(CP_SEL_TAG, curses.COLOR_GREEN, curses.COLOR_BLUE)
        curses.init_pair(CP_SEL_PROJ, curses.COLOR_MAGENTA, curses.COLOR_BLUE)
        curses.init_pair(CP_ACCENT, curses.COLOR_CYAN, -1)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.scr.keypad(True)
        self.scr.timeout(100)

    def _refresh(self):
        self.sessions = self.mgr.scan()
        self._apply_filter()

    def _apply_filter(self):
        if not self.query:
            self.filtered = list(self.sessions)
        else:
            q = self.query.lower()
            self.filtered = [
                s for s in self.sessions
                if q in s.label.lower()
                or q in s.project_display.lower()
                or q in s.tag.lower()
                or q in s.id.lower()
                or q in s.cwd.lower()
            ]
        if self.cur >= len(self.filtered):
            self.cur = max(0, len(self.filtered) - 1)

    def _set_status(self, msg: str, ttl: int = 30):
        self.status = msg
        self.status_ttl = ttl

    def _safe(self, y: int, x: int, text: str, attr: int = 0):
        """Safely write text to the screen, handling boundary conditions."""
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        try:
            self.scr.addnstr(y, x, text, max(0, w - x - 1), attr)
        except curses.error:
            pass

    def _hline(self, y: int, x: int, ch: str, length: int, attr: int = 0):
        """Draw a horizontal line."""
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h:
            return
        text = ch * min(length, w - x - 1)
        self._safe(y, x, text, attr)

    # ── Main loop ─────────────────────────────────────────────────

    def run(self):
        while True:
            self._draw()
            k = self.scr.getch()

            if self.status_ttl > 0:
                self.status_ttl -= 1
                if self.status_ttl == 0:
                    self.status = ""

            if k == -1:
                continue
            if k == curses.KEY_RESIZE:
                self.scr.clear()
                continue

            result = self._handle_input(k)
            if result in ("quit", "action"):
                break

    # ── Drawing ───────────────────────────────────────────────────

    def _draw(self):
        self.scr.erase()
        h, w = self.scr.getmaxyx()

        if h < 10 or w < 40:
            self._safe(0, 0, "Terminal too small! (min 40x10)",
                       curses.color_pair(CP_WARN) | curses.A_BOLD)
            self.scr.refresh()
            return

        # Layout allocation
        hdr_h = 4       # header box + input line
        ftr_h = 1       # footer / status bar
        sep_h = 1       # separator between list and preview
        preview_h = min(14, max(6, (h - hdr_h - ftr_h - sep_h) * 2 // 5))
        list_h = h - hdr_h - ftr_h - sep_h - preview_h

        self._draw_header(w)
        self._draw_list(hdr_h, list_h, w)
        self._draw_separator(hdr_h + list_h, w)
        self._draw_preview(hdr_h + list_h + sep_h, preview_h, w)
        self._draw_footer(h - ftr_h, w)

        if self.mode == "help":
            self._draw_help_overlay(h, w)
        elif self.mode == "delete":
            self._draw_confirm_overlay(h, w,
                "Delete Session",
                f"Delete '{self.delete_label}'?",
                "This cannot be undone.")
        elif self.mode == "delete_empty":
            self._draw_confirm_overlay(h, w,
                "Delete Empty Sessions",
                f"Delete {self.empty_count} empty session{'s' if self.empty_count != 1 else ''}?",
                "All sessions with no messages will be removed.")
        elif self.mode == "launch":
            self._draw_launch_overlay(h, w)
        elif self.mode == "quick_profile":
            self._draw_quick_profile_overlay(h, w)
        elif self.mode == "profiles":
            self._draw_profiles_overlay(h, w)
        elif self.mode == "profile_edit":
            self._draw_profile_edit_overlay(h, w)

        self.scr.refresh()

    def _draw_header(self, w: int):
        bdr = curses.color_pair(CP_BORDER)
        hdr = curses.color_pair(CP_HEADER) | curses.A_BOLD
        dim = curses.color_pair(CP_DIM) | curses.A_DIM

        # ┌─ Title ─────────────────────────────┐
        self._safe(0, 0, "┌", bdr)
        self._hline(0, 1, "─", w - 2, bdr)
        self._safe(0, w - 1, "┐", bdr)
        title = " ◆ CCS — Claude Code Session Manager "
        tx = max(2, (w - len(title)) // 2)
        self._safe(0, tx, title, hdr)

        # │  hints  │
        self._safe(1, 0, "│", bdr)
        self._safe(1, w - 1, "│", bdr)

        hints_map = {
            "normal":  "⏎ Resume  o Options  O Quick Profile  P Profiles  p Pin  t Tag  d Del  / Search  ? Help  q Quit",
            "search":  "Type to filter  ·  ⏎ Apply  ·  Esc Cancel",
            "tag":     "Type tag name  ·  ⏎ Apply  ·  Esc Cancel",
            "delete":  "y Confirm  ·  n / Esc Cancel",
            "delete_empty": "y Confirm  ·  n / Esc Cancel",
            "new":     "Type session name  ·  ⏎ Create  ·  Esc Cancel",
            "launch":  "↑↓ Navigate  Space Toggle  ⏎ Launch  Esc Cancel",
            "profiles": "↑↓ Navigate  n New  ⏎ Edit  d Delete  Esc Back",
            "profile_edit": "↑↓ Navigate  Space Toggle  ⏎ Save/Edit  Esc Cancel",
            "quick_profile": "1-9 Quick pick  ↑↓ Navigate  ⏎ Launch  d Delete  Esc Cancel",
            "help":    "Press any key to close",
        }
        hints = hints_map.get(self.mode, "")
        if len(hints) > w - 4:
            hints = hints[:w - 7] + "..."
        hx = max(2, (w - len(hints)) // 2)
        self._safe(1, hx, hints, dim)

        # └──────────────────────────────────────┘
        self._safe(2, 0, "└", bdr)
        self._hline(2, 1, "─", w - 2, bdr)
        self._safe(2, w - 1, "┘", bdr)

        # Row 3: input or info line
        y = 3
        if self.mode == "search":
            self._safe(y, 1, " /", curses.color_pair(CP_INPUT) | curses.A_BOLD)
            self._safe(y, 4, self.query + "▏", curses.color_pair(CP_NORMAL))
        elif self.mode == "tag":
            self._safe(y, 1, " Tag:", curses.color_pair(CP_TAG) | curses.A_BOLD)
            self._safe(y, 7, self.ibuf + "▏", curses.color_pair(CP_NORMAL))
        elif self.mode == "new":
            self._safe(y, 1, " Name:", curses.color_pair(CP_HEADER) | curses.A_BOLD)
            self._safe(y, 8, self.ibuf + "▏", curses.color_pair(CP_NORMAL))
        elif self.mode in ("delete", "delete_empty", "profiles", "profile_edit"):
            pass  # handled by overlay popups
        elif self.query:
            self._safe(y, 1, f" Filter: {self.query}", dim)
            cx = 10 + len(self.query) + 2
            self._safe(y, cx, "(Esc to clear)", curses.color_pair(CP_DIM) | curses.A_DIM)
        else:
            n = len(self.filtered)
            self._safe(y, 1, f" {n} session{'s' if n != 1 else ''}",
                       curses.color_pair(CP_ACCENT))

    def _draw_list(self, sy: int, height: int, w: int):
        if not self.filtered:
            msg = "No sessions found." if not self.query else "No matching sessions."
            self._safe(sy + height // 2, max(1, (w - len(msg)) // 2),
                       msg, curses.color_pair(CP_DIM) | curses.A_DIM)
            if not self.query:
                hint = "Press 'n' to create a new session or 'e' for ephemeral"
                self._safe(sy + height // 2 + 1, max(1, (w - len(hint)) // 2),
                           hint, curses.color_pair(CP_DIM) | curses.A_DIM)
            return

        # Adjust scroll to keep cursor visible
        if self.cur < self.scroll:
            self.scroll = self.cur
        if self.cur >= self.scroll + height:
            self.scroll = self.cur - height + 1

        for i in range(height):
            idx = self.scroll + i
            if idx >= len(self.filtered):
                break
            s = self.filtered[idx]
            sel = (idx == self.cur)
            self._draw_row(sy + i, w, s, sel)

    def _draw_row(self, y: int, w: int, s: Session, sel: bool):
        """Draw a single session row with color-coded segments."""
        _, scr_w = self.scr.getmaxyx()

        # Column widths
        ind_w = 3     # " ▸ " or "   "
        pin_w = 2     # "★ " or "  "
        ts_w = 18     # "2025-01-15 14:30  "
        age_w = 10    # "3d ago    "
        proj_w = min(28, max(12, (w - ind_w - pin_w - ts_w - age_w - 4) // 3))

        tag_str = f"[{s.tag}] " if s.tag else ""
        tag_w = len(tag_str)

        desc_w = max(8, w - ind_w - pin_w - tag_w - ts_w - age_w - proj_w - 2)

        proj = s.project_display
        if len(proj) > proj_w:
            proj = proj[:proj_w - 2] + ".."
        proj = proj.ljust(proj_w)

        desc = s.label
        if len(desc) > desc_w:
            desc = desc[:desc_w - 1] + "…"

        age = s.age
        if len(age) > age_w:
            age = age[:age_w]
        age = age.rjust(age_w)

        if sel:
            # Highlight entire row
            base = curses.color_pair(CP_SELECTED) | curses.A_BOLD

            # Build full line and pad
            line = f" ▸ {'★ ' if s.pinned else '  '}{tag_str}{s.ts}  {proj} {desc}"
            # Pad to fill width
            if len(line) < w - 1:
                line += " " * (w - 1 - len(line))
            line = line[:w - 1]
            self._safe(y, 0, line, base)

            # Overlay colored segments on selection background
            x = 3  # after indicator
            if s.pinned:
                self._safe(y, x, "★", curses.color_pair(CP_SEL_PIN) | curses.A_BOLD)
            x += pin_w
            if s.tag:
                self._safe(y, x, f"[{s.tag}]",
                           curses.color_pair(CP_SEL_TAG) | curses.A_BOLD)
            x += tag_w + ts_w
            self._safe(y, x, proj.rstrip(),
                       curses.color_pair(CP_SEL_PROJ) | curses.A_BOLD)
        else:
            x = 0
            # Indicator
            self._safe(y, x, "   ", curses.color_pair(CP_NORMAL))
            x += ind_w

            # Pin
            if s.pinned:
                self._safe(y, x, "★ ", curses.color_pair(CP_PIN) | curses.A_BOLD)
            x += pin_w

            # Tag
            if s.tag:
                self._safe(y, x, f"[{s.tag}] ",
                           curses.color_pair(CP_TAG) | curses.A_BOLD)
            x += tag_w

            # Timestamp
            self._safe(y, x, s.ts + "  ", curses.color_pair(CP_DIM) | curses.A_DIM)
            x += ts_w

            # Project
            self._safe(y, x, proj, curses.color_pair(CP_PROJECT))
            x += proj_w

            # Description
            self._safe(y, x + 1, desc, curses.color_pair(CP_NORMAL))

    def _draw_separator(self, y: int, w: int):
        bdr = curses.color_pair(CP_BORDER)
        self._safe(y, 0, "├", bdr)
        self._hline(y, 1, "─", w - 2, bdr)
        self._safe(y, w - 1, "┤", bdr)
        label = " Preview "
        self._safe(y, 2, label, curses.color_pair(CP_BORDER) | curses.A_BOLD)

    def _draw_preview(self, sy: int, h: int, w: int):
        if not self.filtered:
            self._safe(sy + 1, 3, "Select a session to preview",
                       curses.color_pair(CP_DIM) | curses.A_DIM)
            return

        s = self.filtered[self.cur]
        lines: List[Tuple[str, int]] = []  # (text, color_pair | attr)

        # Session metadata
        if s.pinned:
            lines.append(("  ★ PINNED", curses.color_pair(CP_PIN) | curses.A_BOLD))
        if s.tag:
            lines.append((f"  Tag:     {s.tag}", curses.color_pair(CP_TAG) | curses.A_BOLD))

        lines.append((f"  Session: {s.id[:36]}{'...' if len(s.id) > 36 else ''}",
                       curses.color_pair(CP_DIM) | curses.A_DIM))
        lines.append((f"  Project: {s.project_display}",
                       curses.color_pair(CP_PROJECT)))
        if s.cwd:
            lines.append((f"  CWD:     {s.cwd}", curses.color_pair(CP_DIM) | curses.A_DIM))
        lines.append((f"  Modified: {s.ts}  ({s.age})",
                       curses.color_pair(CP_DIM) | curses.A_DIM))
        lines.append(("", 0))

        # First message
        if s.first_msg_long:
            lines.append(("  First Message:",
                           curses.color_pair(CP_HEADER) | curses.A_BOLD))
            for wl in self._word_wrap(s.first_msg_long, w - 8):
                lines.append((f"    {wl}", curses.color_pair(CP_NORMAL)))
            lines.append(("", 0))

        # Summaries / topics
        if s.summaries:
            lines.append(("  Topics:",
                           curses.color_pair(CP_HEADER) | curses.A_BOLD))
            for sm in s.summaries[-6:]:
                tl = sm[:w - 10]
                lines.append((f"    • {tl}", curses.color_pair(CP_NORMAL)))
        elif not s.first_msg_long:
            lines.append(("  (empty session — no messages yet)",
                           curses.color_pair(CP_DIM) | curses.A_DIM))

        # Render
        for i, (text, attr) in enumerate(lines[:h]):
            self._safe(sy + i, 0, text[:w - 1], attr)

    def _draw_help_overlay(self, h: int, w: int):
        """Draw a centered help box over the main UI."""
        help_lines = [
            ("", 0),
            ("  Keybindings", curses.A_BOLD),
            ("", 0),
            ("  Navigation", curses.color_pair(CP_HEADER) | curses.A_BOLD),
            ("    ↑ / k          Move up", 0),
            ("    ↓ / j          Move down", 0),
            ("    g              Jump to first", 0),
            ("    G              Jump to last", 0),
            ("    PgUp / PgDn    Page up / down", 0),
            ("", 0),
            ("  Actions", curses.color_pair(CP_HEADER) | curses.A_BOLD),
            ("    Enter          Resume selected session", 0),
            ("    o              Resume with options / profiles", 0),
            ("    O              Quick launch with a profile", 0),
            ("    p              Toggle pin (pinned sort to top)", 0),
            ("    t              Tag a session", 0),
            ("    T              Remove tag from session", 0),
            ("    d              Delete a session (default: N)", 0),
            ("    D              Delete all empty sessions", 0),
            ("    P              Manage launch profiles", 0),
            ("", 0),
            ("  Sessions", curses.color_pair(CP_HEADER) | curses.A_BOLD),
            ("    n              Create a new named session", 0),
            ("    e              Start an ephemeral session", 0),
            ("", 0),
            ("  Other", curses.color_pair(CP_HEADER) | curses.A_BOLD),
            ("    /              Search / filter sessions", 0),
            ("    r              Refresh session list", 0),
            ("    Esc            Clear filter, or quit", 0),
            ("    q              Quit", 0),
            ("", 0),
            ("  Press any key to close", curses.color_pair(CP_DIM) | curses.A_DIM),
            ("", 0),
        ]

        box_w = min(54, w - 4)
        box_h = min(len(help_lines) + 2, h - 2)
        sx = max(0, (w - box_w) // 2)
        sy = max(0, (h - box_h) // 2)

        bdr = curses.color_pair(CP_BORDER) | curses.A_BOLD

        # Top border
        self._safe(sy, sx, "┌", bdr)
        self._hline(sy, sx + 1, "─", box_w - 2, bdr)
        self._safe(sy, sx + box_w - 1, "┐", bdr)
        title = " ? Help "
        ttx = sx + max(1, (box_w - len(title)) // 2)
        self._safe(sy, ttx, title, curses.color_pair(CP_HEADER) | curses.A_BOLD)

        # Content rows
        for i in range(box_h - 2):
            y = sy + 1 + i
            # Clear row inside box
            self._safe(y, sx, "│" + " " * (box_w - 2) + "│", bdr)
            if i < len(help_lines):
                text, attr = help_lines[i]
                # Default color for plain lines
                if attr == 0:
                    attr = curses.color_pair(CP_NORMAL)
                self._safe(y, sx + 1, text[:box_w - 3], attr)

        # Bottom border
        self._safe(sy + box_h - 1, sx, "└", bdr)
        self._hline(sy + box_h - 1, sx + 1, "─", box_w - 2, bdr)
        self._safe(sy + box_h - 1, sx + box_w - 1, "┘", bdr)

    def _draw_confirm_overlay(self, h: int, w: int,
                               title: str, message: str, detail: str):
        """Draw a centered y/n confirmation popup."""
        warn = curses.color_pair(CP_WARN) | curses.A_BOLD
        bdr = curses.color_pair(CP_WARN)
        dim = curses.color_pair(CP_DIM) | curses.A_DIM
        normal = curses.color_pair(CP_NORMAL)

        content_lines = [
            ("", 0),
            (f"  {message}", warn),
            ("", 0),
            (f"  {detail}", normal),
            ("", 0),
            ("  y  Confirm", dim),
            ("  N  Cancel  (default)", warn),
            ("", 0),
        ]

        box_w = min(max(len(message) + 6, len(detail) + 6, len(title) + 8, 40), w - 4)
        box_h = len(content_lines) + 2
        sx = max(0, (w - box_w) // 2)
        sy = max(0, (h - box_h) // 2)

        # Top border
        self._safe(sy, sx, "┌", bdr)
        self._hline(sy, sx + 1, "─", box_w - 2, bdr)
        self._safe(sy, sx + box_w - 1, "┐", bdr)
        ttl = f" {title} "
        ttx = sx + max(1, (box_w - len(ttl)) // 2)
        self._safe(sy, ttx, ttl, warn)

        # Content rows
        for i in range(box_h - 2):
            y = sy + 1 + i
            self._safe(y, sx, "│" + " " * (box_w - 2) + "│", bdr)
            if i < len(content_lines):
                text, attr = content_lines[i]
                self._safe(y, sx + 1, text[:box_w - 3], attr)

        # Bottom border
        self._safe(sy + box_h - 1, sx, "└", bdr)
        self._hline(sy + box_h - 1, sx + 1, "─", box_w - 2, bdr)
        self._safe(sy + box_h - 1, sx + box_w - 1, "┘", bdr)

    def _build_launch_rows(self) -> List[Tuple[str, int]]:
        """Build the list of (row_type, sub_index) for the launch overlay."""
        rows: List[Tuple[str, int]] = []
        rows.append((ROW_PROFILE, 0))
        rows.append((ROW_MODEL, 0))
        rows.append((ROW_PERMMODE, 0))
        for i in range(len(TOGGLE_FLAGS)):
            rows.append((ROW_TOGGLE, i))
        rows.append((ROW_SYSPROMPT, 0))
        rows.append((ROW_TOOLS, 0))
        rows.append((ROW_MCP, 0))
        rows.append((ROW_CUSTOM, 0))
        rows.append((ROW_LAUNCH, 0))
        rows.append((ROW_SAVE, 0))
        return rows

    def _launch_apply_profile(self, profile: dict):
        """Load a saved profile's settings into the launch state."""
        # Model
        model = profile.get("model", "")
        self.launch_model_idx = 0
        for i, (_, mid) in enumerate(MODELS):
            if mid == model:
                self.launch_model_idx = i
                break
        # Permission mode
        perm = profile.get("permission_mode", "")
        self.launch_perm_idx = 0
        for i, (_, pid) in enumerate(PERMISSION_MODES):
            if pid == perm:
                self.launch_perm_idx = i
                break
        # Toggles
        flags = profile.get("flags", [])
        for i, (_, cli_flag) in enumerate(TOGGLE_FLAGS):
            self.launch_toggles[i] = cli_flag in flags
        # Text fields
        self.launch_sysprompt = profile.get("system_prompt", "")
        self.launch_tools = profile.get("tools", "")
        self.launch_mcp = profile.get("mcp_config", "")
        self.launch_custom = profile.get("custom_args", "")

    def _launch_to_profile_dict(self, name: str) -> dict:
        """Serialize current launch state to a profile dict."""
        flags = [TOGGLE_FLAGS[i][1] for i, v in enumerate(self.launch_toggles) if v]
        return {
            "name": name,
            "model": MODELS[self.launch_model_idx][1],
            "permission_mode": PERMISSION_MODES[self.launch_perm_idx][1],
            "flags": flags,
            "system_prompt": self.launch_sysprompt,
            "tools": self.launch_tools,
            "mcp_config": self.launch_mcp,
            "custom_args": self.launch_custom,
        }

    def _draw_launch_overlay(self, h: int, w: int):
        """Draw the launch options popup with profile support."""
        bdr = curses.color_pair(CP_BORDER) | curses.A_BOLD
        hdr = curses.color_pair(CP_HEADER) | curses.A_BOLD
        dim = curses.color_pair(CP_DIM) | curses.A_DIM
        normal = curses.color_pair(CP_NORMAL)
        sel_attr = curses.color_pair(CP_SELECTED) | curses.A_BOLD
        accent = curses.color_pair(CP_ACCENT) | curses.A_BOLD
        tag_attr = curses.color_pair(CP_TAG) | curses.A_BOLD

        s = self.launch_session
        slabel = (s.tag or s.label[:36]) if s else ""
        profiles = self.mgr.load_profiles()
        profile_names = ["(custom)"] + [p.get("name", "?") for p in profiles]

        def is_sel(i):
            return i == self.launch_cur

        def ind(i):
            return " ▸ " if is_sel(i) else "   "

        def cb(val):
            return "[x]" if val else "[ ]"

        def text_field(label, value, row_type, idx):
            editing = self.launch_editing == row_type
            cursor = "▏" if editing else ""
            return f"{label} {value}{cursor}"

        # Build display lines: list of (text, attr)
        display: List[Tuple[str, int]] = []
        rows = self.launch_rows

        for ri, (rtype, ridx) in enumerate(rows):
            a = sel_attr if is_sel(ri) else normal
            prefix = ind(ri)

            if rtype == ROW_PROFILE:
                pname = profile_names[self.launch_profile_idx] if self.launch_profile_idx < len(profile_names) else "(custom)"
                display.append((f"{prefix}Profile: {pname}", accent if is_sel(ri) else tag_attr))
            elif rtype == ROW_MODEL:
                display.append((f"{prefix}Model:       {MODELS[self.launch_model_idx][0]}", a))
            elif rtype == ROW_PERMMODE:
                display.append((f"{prefix}Permissions: {PERMISSION_MODES[self.launch_perm_idx][0]}", a))
            elif rtype == ROW_TOGGLE:
                flag_name = TOGGLE_FLAGS[ridx][0]
                display.append((f"{prefix}{flag_name:<38s} {cb(self.launch_toggles[ridx])}", a))
            elif rtype == ROW_SYSPROMPT:
                display.append((f"{prefix}{text_field('System prompt:', self.launch_sysprompt, ROW_SYSPROMPT, ri)}", a))
            elif rtype == ROW_TOOLS:
                display.append((f"{prefix}{text_field('Tools:', self.launch_tools, ROW_TOOLS, ri)}", a))
            elif rtype == ROW_MCP:
                display.append((f"{prefix}{text_field('MCP config:', self.launch_mcp, ROW_MCP, ri)}", a))
            elif rtype == ROW_CUSTOM:
                display.append((f"{prefix}{text_field('Custom args:', self.launch_custom, ROW_CUSTOM, ri)}", a))
            elif rtype == ROW_LAUNCH:
                la = curses.color_pair(CP_STATUS) | curses.A_BOLD if is_sel(ri) else accent
                display.append((f"{prefix}>>> Launch <<<", la))
            elif rtype == ROW_SAVE:
                if self.launch_editing == ROW_SAVE:
                    display.append((f"{prefix}Save as: {self.launch_save_name}▏", a))
                else:
                    display.append((f"{prefix}Save as profile...  (x = delete profile)", dim if not is_sel(ri) else a))

        box_w = min(60, w - 4)
        # +4 for: title, session line, blank, hints
        box_h = min(len(display) + 5, h - 2)
        sx = max(0, (w - box_w) // 2)
        sy = max(0, (h - box_h) // 2)

        # Top border
        self._safe(sy, sx, "┌", bdr)
        self._hline(sy, sx + 1, "─", box_w - 2, bdr)
        self._safe(sy, sx + box_w - 1, "┐", bdr)
        title = " Launch Options "
        ttx = sx + max(1, (box_w - len(title)) // 2)
        self._safe(sy, ttx, title, hdr)

        # Clear content area
        for i in range(box_h - 2):
            self._safe(sy + 1 + i, sx, "│" + " " * (box_w - 2) + "│", bdr)

        # Session info
        self._safe(sy + 1, sx + 2, f"Session: {slabel}"[:box_w - 4], dim)

        # Scrollable rows area
        row_area_h = box_h - 5  # minus title, session, blank, hints, bottom border
        row_start = sy + 3

        # Scroll within the overlay if needed
        scroll = 0
        if self.launch_cur >= scroll + row_area_h:
            scroll = self.launch_cur - row_area_h + 1
        if self.launch_cur < scroll:
            scroll = self.launch_cur

        for i in range(row_area_h):
            di = scroll + i
            if di >= len(display):
                break
            text, attr = display[di]
            self._safe(row_start + i, sx + 1, text[:box_w - 3], attr)

        # Hints
        hints_y = sy + box_h - 2
        hints = " Space toggle · ⏎ launch · S save · x del profile · Esc back "
        hx = sx + max(1, (box_w - len(hints)) // 2)
        self._safe(hints_y, hx, hints[:box_w - 3], dim)

        # Bottom border
        self._safe(sy + box_h - 1, sx, "└", bdr)
        self._hline(sy + box_h - 1, sx + 1, "─", box_w - 2, bdr)
        self._safe(sy + box_h - 1, sx + box_w - 1, "┘", bdr)

    # ── Profile manager overlays ──────────────────────────────────

    @staticmethod
    def _profile_summary(p: dict) -> str:
        """One-line summary of a profile's settings."""
        parts: List[str] = []
        model = p.get("model", "")
        for name, mid in MODELS:
            if mid == model and name != "default":
                parts.append(name)
                break
        perm = p.get("permission_mode", "")
        if perm:
            parts.append(perm)
        for flag in p.get("flags", []):
            short = flag.lstrip("-")
            if len(short) > 20:
                short = short[:18] + ".."
            parts.append(short)
        if p.get("system_prompt"):
            parts.append("sys-prompt")
        if p.get("custom_args"):
            parts.append("+" + p["custom_args"][:15])
        return " · ".join(parts) if parts else "default settings"

    def _draw_profiles_overlay(self, h: int, w: int):
        """Profile list overlay."""
        bdr = curses.color_pair(CP_BORDER) | curses.A_BOLD
        hdr = curses.color_pair(CP_HEADER) | curses.A_BOLD
        dim = curses.color_pair(CP_DIM) | curses.A_DIM
        normal = curses.color_pair(CP_NORMAL)
        sel_attr = curses.color_pair(CP_SELECTED) | curses.A_BOLD
        tag_attr = curses.color_pair(CP_TAG) | curses.A_BOLD
        warn = curses.color_pair(CP_WARN) | curses.A_BOLD

        profiles = self.mgr.load_profiles()
        box_w = min(62, w - 4)
        list_h = max(3, min(len(profiles) + 2, h - 10))
        box_h = list_h + 5  # title + blank + list + blank + hints + border
        sx = max(0, (w - box_w) // 2)
        sy = max(0, (h - box_h) // 2)

        # Box
        self._safe(sy, sx, "┌", bdr)
        self._hline(sy, sx + 1, "─", box_w - 2, bdr)
        self._safe(sy, sx + box_w - 1, "┐", bdr)
        title = " Profiles "
        self._safe(sy, sx + max(1, (box_w - len(title)) // 2), title, hdr)

        for i in range(box_h - 2):
            self._safe(sy + 1 + i, sx, "│" + " " * (box_w - 2) + "│", bdr)

        self._safe(sy + box_h - 1, sx, "└", bdr)
        self._hline(sy + box_h - 1, sx + 1, "─", box_w - 2, bdr)
        self._safe(sy + box_h - 1, sx + box_w - 1, "┘", bdr)

        # Content
        if not profiles:
            self._safe(sy + 2, sx + 3, "No profiles yet.", dim)
            self._safe(sy + 3, sx + 3, "Press n to create your first profile.", dim)
        else:
            for i in range(list_h):
                idx = i
                if idx >= len(profiles):
                    break
                p = profiles[idx]
                is_sel = (idx == self.prof_cur)
                y = sy + 2 + i
                name = p.get("name", "?")
                summary = self._profile_summary(p)

                if is_sel:
                    line = f" ▸ {name:<18s} {summary}"
                    line = line.ljust(box_w - 3)[:box_w - 3]
                    self._safe(y, sx + 1, line, sel_attr)
                else:
                    self._safe(y, sx + 1, "   ", normal)
                    self._safe(y, sx + 4, name, tag_attr)
                    self._safe(y, sx + 4 + 18 + 1, summary[:box_w - 26],
                               curses.color_pair(CP_DIM))

        # Delete confirmation
        if self.prof_delete_confirm and profiles:
            pname = profiles[self.prof_cur].get("name", "?")
            self._safe(sy + box_h - 3, sx + 3,
                       f"Delete '{pname}'? y/N", warn)

        # Hints
        hints = " n New  ⏎ Edit  d Delete  Esc Back "
        self._safe(sy + box_h - 2, sx + max(1, (box_w - len(hints)) // 2),
                   hints[:box_w - 3], dim)

    def _build_profile_edit_rows(self) -> List[Tuple[str, int]]:
        rows: List[Tuple[str, int]] = []
        rows.append((ROW_PROF_NAME, 0))
        rows.append((ROW_MODEL, 0))
        rows.append((ROW_PERMMODE, 0))
        for i in range(len(TOGGLE_FLAGS)):
            rows.append((ROW_TOGGLE, i))
        rows.append((ROW_SYSPROMPT, 0))
        rows.append((ROW_TOOLS, 0))
        rows.append((ROW_MCP, 0))
        rows.append((ROW_CUSTOM, 0))
        rows.append((ROW_PROF_SAVE, 0))
        return rows

    def _draw_profile_edit_overlay(self, h: int, w: int):
        """Profile editor overlay."""
        bdr = curses.color_pair(CP_BORDER) | curses.A_BOLD
        hdr = curses.color_pair(CP_HEADER) | curses.A_BOLD
        dim = curses.color_pair(CP_DIM) | curses.A_DIM
        normal = curses.color_pair(CP_NORMAL)
        sel_attr = curses.color_pair(CP_SELECTED) | curses.A_BOLD
        accent = curses.color_pair(CP_ACCENT) | curses.A_BOLD
        tag_attr = curses.color_pair(CP_TAG) | curses.A_BOLD

        rows = self.prof_edit_rows
        is_new = self.prof_editing_existing is None
        title_text = " New Profile " if is_new else " Edit Profile "

        def is_sel(i):
            return i == self.prof_edit_cur

        def ind(i):
            return " ▸ " if is_sel(i) else "   "

        def cb(val):
            return "[x]" if val else "[ ]"

        display: List[Tuple[str, int]] = []
        for ri, (rtype, ridx) in enumerate(rows):
            a = sel_attr if is_sel(ri) else normal
            prefix = ind(ri)

            if rtype == ROW_PROF_NAME:
                editing = self.launch_editing == ROW_PROF_NAME
                cursor = "▏" if editing else ""
                display.append((f"{prefix}Name: {self.prof_edit_name}{cursor}",
                                accent if is_sel(ri) else tag_attr))
            elif rtype == ROW_MODEL:
                display.append((f"{prefix}Model:       {MODELS[self.launch_model_idx][0]}", a))
            elif rtype == ROW_PERMMODE:
                display.append((f"{prefix}Permissions: {PERMISSION_MODES[self.launch_perm_idx][0]}", a))
            elif rtype == ROW_TOGGLE:
                flag_name = TOGGLE_FLAGS[ridx][0]
                display.append((f"{prefix}{flag_name:<38s} {cb(self.launch_toggles[ridx])}", a))
            elif rtype == ROW_SYSPROMPT:
                editing = self.launch_editing == ROW_SYSPROMPT
                cursor = "▏" if editing else ""
                v = self.launch_sysprompt
                display.append((f"{prefix}System prompt: {v}{cursor}", a))
            elif rtype == ROW_TOOLS:
                editing = self.launch_editing == ROW_TOOLS
                cursor = "▏" if editing else ""
                display.append((f"{prefix}Tools: {self.launch_tools}{cursor}", a))
            elif rtype == ROW_MCP:
                editing = self.launch_editing == ROW_MCP
                cursor = "▏" if editing else ""
                display.append((f"{prefix}MCP config: {self.launch_mcp}{cursor}", a))
            elif rtype == ROW_CUSTOM:
                editing = self.launch_editing == ROW_CUSTOM
                cursor = "▏" if editing else ""
                display.append((f"{prefix}Custom args: {self.launch_custom}{cursor}", a))
            elif rtype == ROW_PROF_SAVE:
                la = curses.color_pair(CP_STATUS) | curses.A_BOLD if is_sel(ri) else accent
                display.append((f"{prefix}>>> Save <<<", la))

        box_w = min(60, w - 4)
        box_h = min(len(display) + 4, h - 2)
        sx = max(0, (w - box_w) // 2)
        sy = max(0, (h - box_h) // 2)

        # Box
        self._safe(sy, sx, "┌", bdr)
        self._hline(sy, sx + 1, "─", box_w - 2, bdr)
        self._safe(sy, sx + box_w - 1, "┐", bdr)
        self._safe(sy, sx + max(1, (box_w - len(title_text)) // 2), title_text, hdr)

        for i in range(box_h - 2):
            self._safe(sy + 1 + i, sx, "│" + " " * (box_w - 2) + "│", bdr)

        self._safe(sy + box_h - 1, sx, "└", bdr)
        self._hline(sy + box_h - 1, sx + 1, "─", box_w - 2, bdr)
        self._safe(sy + box_h - 1, sx + box_w - 1, "┘", bdr)

        # Content (scrollable)
        row_area_h = box_h - 3
        scroll = 0
        if self.prof_edit_cur >= scroll + row_area_h:
            scroll = self.prof_edit_cur - row_area_h + 1
        if self.prof_edit_cur < scroll:
            scroll = self.prof_edit_cur

        for i in range(row_area_h):
            di = scroll + i
            if di >= len(display):
                break
            text, attr = display[di]
            self._safe(sy + 1 + i, sx + 1, text[:box_w - 3], attr)

        # Hints
        hints = " Space toggle · ⏎ edit/save · Esc cancel "
        self._safe(sy + box_h - 2, sx + max(1, (box_w - len(hints)) // 2),
                   hints[:box_w - 3], dim)

    def _draw_quick_profile_overlay(self, h: int, w: int):
        """Draw a compact profile picker popup for quick launch."""
        bdr = curses.color_pair(CP_BORDER) | curses.A_BOLD
        hdr = curses.color_pair(CP_HEADER) | curses.A_BOLD
        dim = curses.color_pair(CP_DIM) | curses.A_DIM
        normal = curses.color_pair(CP_NORMAL)
        sel_attr = curses.color_pair(CP_SELECTED) | curses.A_BOLD
        tag_attr = curses.color_pair(CP_TAG) | curses.A_BOLD

        profiles = self.mgr.load_profiles()
        if not profiles:
            self.mode = "normal"
            return

        s = self.launch_session
        slabel = (s.tag or s.label[:30]) if s else ""

        box_w = min(56, w - 4)
        list_h = min(len(profiles), h - 8)
        box_h = list_h + 5  # title + session + blank + list + hints + border
        sx = max(0, (w - box_w) // 2)
        sy = max(0, (h - box_h) // 2)

        # Box
        self._safe(sy, sx, "┌", bdr)
        self._hline(sy, sx + 1, "─", box_w - 2, bdr)
        self._safe(sy, sx + box_w - 1, "┐", bdr)
        title = " Quick Launch with Profile "
        self._safe(sy, sx + max(1, (box_w - len(title)) // 2), title, hdr)

        for i in range(box_h - 2):
            self._safe(sy + 1 + i, sx, "│" + " " * (box_w - 2) + "│", bdr)

        self._safe(sy + box_h - 1, sx, "└", bdr)
        self._hline(sy + box_h - 1, sx + 1, "─", box_w - 2, bdr)
        self._safe(sy + box_h - 1, sx + box_w - 1, "┘", bdr)

        # Session info
        self._safe(sy + 1, sx + 2, f"Session: {slabel}"[:box_w - 4], dim)

        # Profile list
        scroll = 0
        if self.qprof_cur >= scroll + list_h:
            scroll = self.qprof_cur - list_h + 1
        if self.qprof_cur < scroll:
            scroll = self.qprof_cur

        for i in range(list_h):
            idx = scroll + i
            if idx >= len(profiles):
                break
            p = profiles[idx]
            is_sel = (idx == self.qprof_cur)
            y = sy + 3 + i
            name = p.get("name", "?")
            summary = self._profile_summary(p)
            num = str(idx + 1) if idx < 9 else " "

            if is_sel:
                line = f" ▸ {num} {name:<15s} {summary}"
                line = line.ljust(box_w - 3)[:box_w - 3]
                self._safe(y, sx + 1, line, sel_attr)
            else:
                self._safe(y, sx + 1, f"   {num} ", dim)
                self._safe(y, sx + 6, name, tag_attr)
                self._safe(y, sx + 6 + 15 + 1, summary[:box_w - 24],
                           curses.color_pair(CP_DIM))

        # Delete confirmation
        if self.qprof_delete_confirm and profiles:
            pname = profiles[self.qprof_cur].get("name", "?")
            warn = curses.color_pair(CP_WARN) | curses.A_BOLD
            self._safe(sy + box_h - 3, sx + 3,
                       f"Delete '{pname}'? y/N", warn)

        # Hints
        hints = " 1-9 quick pick · ⏎ Launch · d Delete · Esc Cancel "
        self._safe(sy + box_h - 2, sx + max(1, (box_w - len(hints)) // 2),
                   hints[:box_w - 3], dim)

    def _draw_footer(self, y: int, w: int):
        if self.status:
            self._safe(y, 1, f" {self.status} ",
                       curses.color_pair(CP_STATUS) | curses.A_BOLD)
        else:
            self._safe(y, 1, " ccs ",
                       curses.color_pair(CP_DIM) | curses.A_DIM)
            # Hint for help
            self._safe(y, 7, "? help",
                       curses.color_pair(CP_DIM) | curses.A_DIM)

        # Scroll position
        if self.filtered:
            pos = f" {self.cur + 1}/{len(self.filtered)} "
            self._safe(y, w - len(pos) - 1, pos,
                       curses.color_pair(CP_DIM) | curses.A_DIM)

        # Show mode indicator
        if self.mode not in ("normal", "help", "delete", "delete_empty", "launch", "profiles", "profile_edit", "quick_profile"):
            mode_label = f" [{self.mode.upper()}] "
            mx = (w - len(mode_label)) // 2
            self._safe(y, mx, mode_label,
                       curses.color_pair(CP_INPUT) | curses.A_BOLD)

    # ── Text wrapping ─────────────────────────────────────────────

    @staticmethod
    def _word_wrap(text: str, width: int) -> List[str]:
        lines: List[str] = []
        for para in text.split("\n"):
            if not para.strip():
                lines.append("")
                continue
            words = para.split()
            line = ""
            for word in words:
                if line and len(line) + 1 + len(word) > width:
                    lines.append(line)
                    line = word
                else:
                    line = (line + " " + word) if line else word
            if line:
                lines.append(line)
        return lines

    # ── Input handling ────────────────────────────────────────────

    def _handle_input(self, k: int) -> Optional[str]:
        dispatch = {
            "normal": self._input_normal,
            "search": self._input_search,
            "tag": self._input_tag,
            "delete": self._input_delete,
            "delete_empty": self._input_delete_empty,
            "new": self._input_new,
            "launch": self._input_launch,
            "profiles": self._input_profiles,
            "profile_edit": self._input_profile_edit,
            "quick_profile": self._input_quick_profile,
            "help": self._input_help,
        }
        handler = dispatch.get(self.mode, self._input_normal)
        return handler(k)

    def _input_normal(self, k: int) -> Optional[str]:
        if k == ord("q"):
            return "quit"
        elif k == 27:  # Esc
            if self.query:
                self.query = ""
                self._apply_filter()
            else:
                return "quit"

        # Navigation
        elif k in (curses.KEY_UP, ord("k")):
            self.cur = max(0, self.cur - 1)
        elif k in (curses.KEY_DOWN, ord("j")):
            if self.filtered:
                self.cur = min(len(self.filtered) - 1, self.cur + 1)
        elif k == curses.KEY_PPAGE:
            self.cur = max(0, self.cur - 10)
        elif k == curses.KEY_NPAGE:
            if self.filtered:
                self.cur = min(len(self.filtered) - 1, self.cur + 10)
        elif k in (curses.KEY_HOME, ord("g")):
            self.cur = 0
        elif k == ord("G"):
            if self.filtered:
                self.cur = len(self.filtered) - 1

        # Actions
        elif k in (ord("\n"), curses.KEY_ENTER, 10, 13):
            if self.filtered:
                s = self.filtered[self.cur]
                self.exit_action = ("resume", s.id, s.cwd, [])
                return "action"
        elif k == ord("o"):
            if self.filtered:
                s = self.filtered[self.cur]
                self.launch_session = s
                self.launch_rows = self._build_launch_rows()
                self.launch_cur = len(self.launch_rows) - 2  # default to Launch row
                self.launch_model_idx = 0
                self.launch_perm_idx = 0
                self.launch_toggles = [False] * len(TOGGLE_FLAGS)
                self.launch_sysprompt = ""
                self.launch_tools = ""
                self.launch_mcp = ""
                self.launch_custom = ""
                self.launch_editing = None
                self.launch_profile_idx = 0
                self.launch_save_name = ""
                self.mode = "launch"
        elif k == ord("O"):
            if self.filtered:
                profiles = self.mgr.load_profiles()
                if not profiles:
                    self._set_status("No profiles yet — press P to create one")
                else:
                    self.launch_session = self.filtered[self.cur]
                    self.qprof_cur = 0
                    self.mode = "quick_profile"
        elif k == ord("p"):
            if self.filtered:
                s = self.filtered[self.cur]
                pinned = self.mgr.toggle_pin(s.id)
                icon = "★ Pinned" if pinned else "Unpinned"
                self._set_status(f"{icon}: {s.tag or s.id[:12]}")
                self._refresh()
        elif k == ord("t"):
            if self.filtered:
                self.mode = "tag"
                self.ibuf = ""
        elif k == ord("T"):
            if self.filtered:
                s = self.filtered[self.cur]
                if s.tag:
                    self.mgr.remove_tag(s.id)
                    self._set_status(f"Removed tag from: {s.id[:12]}")
                    self._refresh()
                else:
                    self._set_status("No tag to remove")
        elif k == ord("d"):
            if self.filtered:
                s = self.filtered[self.cur]
                self.delete_label = s.tag or s.label[:40]
                self.mode = "delete"
        elif k == ord("D"):
            empty = [s for s in self.sessions if not s.first_msg and not s.summary]
            if empty:
                self.empty_count = len(empty)
                self.mode = "delete_empty"
            else:
                self._set_status("No empty sessions to delete")
        elif k == ord("n"):
            self.mode = "new"
            self.ibuf = ""
        elif k == ord("e"):
            self.exit_action = ("tmp",)
            return "action"
        elif k == ord("/"):
            self.mode = "search"
        elif k == ord("P"):
            self.prof_cur = 0
            self.prof_delete_confirm = False
            self.mode = "profiles"
        elif k == ord("?"):
            self.mode = "help"
        elif k in (ord("r"), curses.KEY_F5):
            self._refresh()
            self._set_status("Refreshed session list")

        return None

    def _input_search(self, k: int) -> Optional[str]:
        if k == 27:  # Esc
            self.mode = "normal"
        elif k in (ord("\n"), curses.KEY_ENTER, 10, 13):
            self.mode = "normal"
        elif k in (curses.KEY_BACKSPACE, 127, 8):
            self.query = self.query[:-1]
            self._apply_filter()
        elif 32 <= k <= 126:
            self.query += chr(k)
            self._apply_filter()
        return None

    def _input_tag(self, k: int) -> Optional[str]:
        if k == 27:  # Esc
            self.mode = "normal"
        elif k in (ord("\n"), curses.KEY_ENTER, 10, 13):
            if self.filtered and self.ibuf.strip():
                s = self.filtered[self.cur]
                self.mgr.set_tag(s.id, self.ibuf.strip())
                self._set_status(f"Tagged: [{self.ibuf.strip()}]")
                self._refresh()
            self.mode = "normal"
        elif k in (curses.KEY_BACKSPACE, 127, 8):
            self.ibuf = self.ibuf[:-1]
        elif 32 <= k <= 126:
            self.ibuf += chr(k)
        return None

    def _input_delete(self, k: int) -> Optional[str]:
        if k == ord("y"):
            if self.filtered:
                s = self.filtered[self.cur]
                self.mgr.delete(s)
                self._set_status(f"Deleted: {s.tag or s.id[:12]}")
                self._refresh()
            self.mode = "normal"
        else:
            self.mode = "normal"
        return None

    def _input_delete_empty(self, k: int) -> Optional[str]:
        if k == ord("y"):
            empty = [s for s in self.sessions if not s.first_msg and not s.summary]
            count = 0
            for s in empty:
                self.mgr.delete(s)
                count += 1
            self._set_status(f"Deleted {count} empty session{'s' if count != 1 else ''}")
            self._refresh()
            self.mode = "normal"
        else:
            self.mode = "normal"
        return None

    def _input_new(self, k: int) -> Optional[str]:
        if k == 27:  # Esc
            self.mode = "normal"
        elif k in (ord("\n"), curses.KEY_ENTER, 10, 13):
            if self.ibuf.strip():
                self.exit_action = ("new", self.ibuf.strip())
                return "action"
            self.mode = "normal"
        elif k in (curses.KEY_BACKSPACE, 127, 8):
            self.ibuf = self.ibuf[:-1]
        elif 32 <= k <= 126:
            self.ibuf += chr(k)
        return None

    def _input_launch(self, k: int) -> Optional[str]:
        rows = self.launch_rows
        cur_type = rows[self.launch_cur][0] if self.launch_cur < len(rows) else None

        # ── Text field editing mode ───────────────────────────────
        if self.launch_editing is not None:
            if k == 27:
                self.launch_editing = None
            elif k in (ord("\n"), curses.KEY_ENTER, 10, 13):
                if self.launch_editing == ROW_SAVE and self.launch_save_name.strip():
                    prof = self._launch_to_profile_dict(self.launch_save_name.strip())
                    self.mgr.save_profile(prof)
                    self._set_status(f"Saved profile: {self.launch_save_name.strip()}")
                    self.launch_save_name = ""
                self.launch_editing = None
            elif k in (curses.KEY_BACKSPACE, 127, 8):
                self._launch_edit_backspace()
            elif 32 <= k <= 126:
                self._launch_edit_char(chr(k))
            return None

        # ── Normal overlay navigation ─────────────────────────────
        if k == 27:  # Esc
            self.mode = "normal"
        elif k in (curses.KEY_UP, ord("k")):
            self.launch_cur = max(0, self.launch_cur - 1)
        elif k in (curses.KEY_DOWN, ord("j")):
            self.launch_cur = min(len(rows) - 1, self.launch_cur + 1)

        elif k == ord(" "):
            self._launch_toggle_current()

        elif k in (ord("\n"), curses.KEY_ENTER, 10, 13):
            if cur_type in (ROW_SYSPROMPT, ROW_TOOLS, ROW_MCP, ROW_CUSTOM):
                self.launch_editing = cur_type
            elif cur_type == ROW_SAVE:
                self.launch_editing = ROW_SAVE
                self.launch_save_name = ""
            elif cur_type == ROW_PROFILE:
                self._launch_toggle_current()
            elif cur_type in (ROW_MODEL, ROW_PERMMODE, ROW_TOGGLE):
                self._launch_toggle_current()
            else:
                return self._do_launch()

        elif k == ord("x"):
            # Delete the currently selected profile
            profiles = self.mgr.load_profiles()
            if self.launch_profile_idx > 0 and self.launch_profile_idx <= len(profiles):
                pname = profiles[self.launch_profile_idx - 1].get("name", "")
                self.mgr.delete_profile(pname)
                self.launch_profile_idx = 0
                self._set_status(f"Deleted profile: {pname}")

        return None

    def _launch_toggle_current(self):
        """Toggle/cycle the currently selected launch row."""
        rows = self.launch_rows
        rtype, ridx = rows[self.launch_cur]
        profiles = self.mgr.load_profiles()
        profile_count = 1 + len(profiles)  # (custom) + saved

        if rtype == ROW_PROFILE:
            self.launch_profile_idx = (self.launch_profile_idx + 1) % profile_count
            if self.launch_profile_idx > 0:
                self._launch_apply_profile(profiles[self.launch_profile_idx - 1])
        elif rtype == ROW_MODEL:
            self.launch_model_idx = (self.launch_model_idx + 1) % len(MODELS)
            self.launch_profile_idx = 0
        elif rtype == ROW_PERMMODE:
            self.launch_perm_idx = (self.launch_perm_idx + 1) % len(PERMISSION_MODES)
            self.launch_profile_idx = 0
        elif rtype == ROW_TOGGLE:
            self.launch_toggles[ridx] = not self.launch_toggles[ridx]
            self.launch_profile_idx = 0
        elif rtype == ROW_SYSPROMPT:
            self.launch_editing = ROW_SYSPROMPT
        elif rtype == ROW_TOOLS:
            self.launch_editing = ROW_TOOLS
        elif rtype == ROW_MCP:
            self.launch_editing = ROW_MCP
        elif rtype == ROW_CUSTOM:
            self.launch_editing = ROW_CUSTOM
        elif rtype == ROW_SAVE:
            self.launch_editing = ROW_SAVE
            self.launch_save_name = ""
        elif rtype == ROW_LAUNCH:
            pass  # handled by Enter

    def _launch_edit_backspace(self):
        if self.launch_editing == ROW_SYSPROMPT:
            self.launch_sysprompt = self.launch_sysprompt[:-1]
        elif self.launch_editing == ROW_TOOLS:
            self.launch_tools = self.launch_tools[:-1]
        elif self.launch_editing == ROW_MCP:
            self.launch_mcp = self.launch_mcp[:-1]
        elif self.launch_editing == ROW_CUSTOM:
            self.launch_custom = self.launch_custom[:-1]
        elif self.launch_editing == ROW_SAVE:
            self.launch_save_name = self.launch_save_name[:-1]

    def _launch_edit_char(self, ch: str):
        if self.launch_editing == ROW_SYSPROMPT:
            self.launch_sysprompt += ch
        elif self.launch_editing == ROW_TOOLS:
            self.launch_tools += ch
        elif self.launch_editing == ROW_MCP:
            self.launch_mcp += ch
        elif self.launch_editing == ROW_CUSTOM:
            self.launch_custom += ch
        elif self.launch_editing == ROW_SAVE:
            self.launch_save_name += ch

    def _do_launch(self) -> str:
        """Build CLI args from launch options and set exit action."""
        s = self.launch_session
        extra: List[str] = []

        model_id = MODELS[self.launch_model_idx][1]
        if model_id:
            extra.extend(["--model", model_id])

        perm_id = PERMISSION_MODES[self.launch_perm_idx][1]
        if perm_id:
            extra.extend(["--permission-mode", perm_id])

        for i, (_, cli_flag) in enumerate(TOGGLE_FLAGS):
            if self.launch_toggles[i]:
                extra.append(cli_flag)

        if self.launch_sysprompt.strip():
            extra.extend(["--system-prompt", self.launch_sysprompt.strip()])
        if self.launch_tools.strip():
            extra.extend(["--tools", self.launch_tools.strip()])
        if self.launch_mcp.strip():
            extra.extend(["--mcp-config", self.launch_mcp.strip()])
        if self.launch_custom.strip():
            extra.extend(self.launch_custom.strip().split())

        self.exit_action = ("resume", s.id, s.cwd, extra)
        return "action"

    # ── Profile manager input ────────────────────────────────────

    def _input_profiles(self, k: int) -> Optional[str]:
        profiles = self.mgr.load_profiles()

        # Delete confirmation sub-mode
        if self.prof_delete_confirm:
            if k == ord("y") and profiles:
                pname = profiles[self.prof_cur].get("name", "")
                self.mgr.delete_profile(pname)
                self._set_status(f"Deleted profile: {pname}")
                if self.prof_cur >= len(profiles) - 1:
                    self.prof_cur = max(0, self.prof_cur - 1)
            self.prof_delete_confirm = False
            return None

        if k == 27:  # Esc
            self.mode = "normal"
        elif k in (curses.KEY_UP, ord("k")):
            if profiles:
                self.prof_cur = max(0, self.prof_cur - 1)
        elif k in (curses.KEY_DOWN, ord("j")):
            if profiles:
                self.prof_cur = min(len(profiles) - 1, self.prof_cur + 1)

        elif k == ord("n"):
            # New profile
            self._prof_open_editor(None)

        elif k in (ord("\n"), curses.KEY_ENTER, 10, 13):
            # Edit selected
            if profiles:
                self._prof_open_editor(profiles[self.prof_cur])

        elif k == ord("d"):
            if profiles:
                pname = profiles[self.prof_cur].get("name", "")
                if pname.lower() == "default":
                    self._set_status("Cannot delete the default profile")
                else:
                    self.prof_delete_confirm = True

        return None

    def _prof_open_editor(self, profile: Optional[dict]):
        """Open the profile editor, optionally pre-filled from an existing profile."""
        self.prof_edit_rows = self._build_profile_edit_rows()
        self.prof_edit_cur = 0  # start on Name
        self.launch_editing = None

        if profile:
            # Edit existing
            self.prof_editing_existing = profile.get("name", "")
            self.prof_edit_name = profile.get("name", "")
            self._launch_apply_profile(profile)
        else:
            # New - blank slate
            self.prof_editing_existing = None
            self.prof_edit_name = ""
            self.launch_model_idx = 0
            self.launch_perm_idx = 0
            self.launch_toggles = [False] * len(TOGGLE_FLAGS)
            self.launch_sysprompt = ""
            self.launch_tools = ""
            self.launch_mcp = ""
            self.launch_custom = ""
            # Start with name field editing immediately
            self.launch_editing = ROW_PROF_NAME

        self.mode = "profile_edit"

    def _input_profile_edit(self, k: int) -> Optional[str]:
        rows = self.prof_edit_rows
        cur_type = rows[self.prof_edit_cur][0] if self.prof_edit_cur < len(rows) else None

        # ── Text field editing ────────────────────────────────────
        if self.launch_editing is not None:
            if k == 27:
                self.launch_editing = None
            elif k in (ord("\n"), curses.KEY_ENTER, 10, 13):
                self.launch_editing = None
            elif k in (curses.KEY_BACKSPACE, 127, 8):
                if self.launch_editing == ROW_PROF_NAME:
                    self.prof_edit_name = self.prof_edit_name[:-1]
                else:
                    self._launch_edit_backspace()
            elif 32 <= k <= 126:
                if self.launch_editing == ROW_PROF_NAME:
                    self.prof_edit_name += chr(k)
                else:
                    self._launch_edit_char(chr(k))
            return None

        # ── Normal navigation ─────────────────────────────────────
        if k == 27:  # Esc → back to profiles list
            self.mode = "profiles"
        elif k in (curses.KEY_UP, ord("k")):
            self.prof_edit_cur = max(0, self.prof_edit_cur - 1)
        elif k in (curses.KEY_DOWN, ord("j")):
            self.prof_edit_cur = min(len(rows) - 1, self.prof_edit_cur + 1)

        elif k == ord(" "):
            self._prof_edit_toggle_current()

        elif k in (ord("\n"), curses.KEY_ENTER, 10, 13):
            if cur_type == ROW_PROF_SAVE:
                self._prof_do_save()
            elif cur_type in (ROW_PROF_NAME, ROW_SYSPROMPT, ROW_TOOLS, ROW_MCP, ROW_CUSTOM):
                self.launch_editing = cur_type
            else:
                self._prof_edit_toggle_current()

        return None

    def _prof_edit_toggle_current(self):
        rtype, ridx = self.prof_edit_rows[self.prof_edit_cur]
        if rtype == ROW_MODEL:
            self.launch_model_idx = (self.launch_model_idx + 1) % len(MODELS)
        elif rtype == ROW_PERMMODE:
            self.launch_perm_idx = (self.launch_perm_idx + 1) % len(PERMISSION_MODES)
        elif rtype == ROW_TOGGLE:
            self.launch_toggles[ridx] = not self.launch_toggles[ridx]
        elif rtype == ROW_PROF_NAME:
            self.launch_editing = ROW_PROF_NAME
        elif rtype in (ROW_SYSPROMPT, ROW_TOOLS, ROW_MCP, ROW_CUSTOM):
            self.launch_editing = rtype

    def _prof_do_save(self):
        name = self.prof_edit_name.strip()
        if not name:
            self._set_status("Profile name cannot be empty")
            return
        # If renaming, delete the old one
        if self.prof_editing_existing and self.prof_editing_existing != name:
            self.mgr.delete_profile(self.prof_editing_existing)
        prof = self._launch_to_profile_dict(name)
        self.mgr.save_profile(prof)
        self._set_status(f"Saved profile: {name}")
        self.mode = "profiles"
        # Update cursor to point at the saved profile
        profiles = self.mgr.load_profiles()
        for i, p in enumerate(profiles):
            if p.get("name") == name:
                self.prof_cur = i
                break

    def _input_quick_profile(self, k: int) -> Optional[str]:
        profiles = self.mgr.load_profiles()
        if not profiles:
            self.mode = "normal"
            return None

        # Delete confirmation sub-mode
        if self.qprof_delete_confirm:
            if k == ord("y") and profiles:
                p = profiles[self.qprof_cur]
                pname = p.get("name", "")
                if pname.lower() == "default":
                    self._set_status("Cannot delete the default profile")
                else:
                    self.mgr.delete_profile(pname)
                    self._set_status(f"Deleted profile: {pname}")
                    profiles = self.mgr.load_profiles()
                    if self.qprof_cur >= len(profiles):
                        self.qprof_cur = max(0, len(profiles) - 1)
                    if not profiles:
                        self.mode = "normal"
            self.qprof_delete_confirm = False
            return None

        if k == 27:  # Esc
            self.mode = "normal"
        elif k in (curses.KEY_UP, ord("k")):
            self.qprof_cur = max(0, self.qprof_cur - 1)
        elif k in (curses.KEY_DOWN, ord("j")):
            self.qprof_cur = min(len(profiles) - 1, self.qprof_cur + 1)
        elif k == ord("d"):
            if profiles:
                p = profiles[self.qprof_cur]
                if p.get("name", "").lower() == "default":
                    self._set_status("Cannot delete the default profile")
                else:
                    self.qprof_delete_confirm = True
        elif k in (ord("\n"), curses.KEY_ENTER, 10, 13):
            # Launch with the selected profile
            profile = profiles[self.qprof_cur]
            self._launch_apply_profile(profile)
            return self._do_launch()
        elif ord("1") <= k <= ord("9"):
            # Number keys for instant selection
            idx = k - ord("1")
            if idx < len(profiles):
                self._launch_apply_profile(profiles[idx])
                return self._do_launch()
        return None

    def _input_help(self, k: int) -> Optional[str]:
        # Any key closes the help overlay
        self.mode = "normal"
        return None


# ── TUI entry point ──────────────────────────────────────────────────


def run_tui(stdscr) -> Optional[Tuple]:
    app = CCSApp(stdscr)
    app.run()
    return app.exit_action


# ── CLI commands ─────────────────────────────────────────────────────


def cmd_help():
    print("""
\033[1;36m◆ ccs — Claude Code Session Manager\033[0m

\033[1mUsage:\033[0m
  ccs              Interactive TUI to browse & resume sessions
  ccs new <name>   Start a named persistent session
  ccs tmp          Start an ephemeral session (auto-deleted)
  ccs help         Show this help

\033[1mTUI Keybindings:\033[0m
  \033[36m↑/↓\033[0m or \033[36mj/k\033[0m       Navigate sessions
  \033[36mEnter\033[0m             Resume selected session
  \033[36mo\033[0m                 Resume with options / profiles
  \033[36mO\033[0m                 Quick launch with a profile
  \033[36mp\033[0m                 Toggle pin (pinned sort to top)
  \033[36mt\033[0m                 Tag a session
  \033[36mT\033[0m                 Remove tag from session
  \033[36md\033[0m                 Delete a session
  \033[36mD\033[0m                 Delete all empty sessions
  \033[36mP\033[0m                 Manage launch profiles
  \033[36mn\033[0m                 Create a new named session
  \033[36me\033[0m                 Start an ephemeral session
  \033[36m/\033[0m                 Search / filter sessions
  \033[36mr\033[0m                 Refresh session list
  \033[36mg\033[0m / \033[36mG\033[0m             Jump to first / last
  \033[36mPgUp\033[0m / \033[36mPgDn\033[0m       Page up / down
  \033[36mEsc\033[0m               Clear filter, or quit
  \033[36mq\033[0m                 Quit
""")


def cmd_new(mgr: SessionManager, name: str, extra: List[str]):
    uid = str(uuid_mod.uuid4())
    tags = mgr._load(TAGS_FILE, {})
    tags[uid] = name
    mgr._save(TAGS_FILE, tags)
    print(f"\033[1;36m◆\033[0m Starting named session: "
          f"\033[1;32m{name}\033[0m \033[2m({uid[:8]}…)\033[0m")
    cmd = ["claude", "--session-id", uid] + extra
    os.execvp("claude", cmd)


def cmd_tmp(mgr: SessionManager, extra: List[str]):
    uid = str(uuid_mod.uuid4())
    with open(EPHEMERAL_FILE, "a") as f:
        f.write(uid + "\n")
    print(f"\033[1;36m◆\033[0m Starting ephemeral session \033[2m({uid[:8]}…)\033[0m")
    cmd = ["claude", "--session-id", uid] + extra
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        pass
    finally:
        mgr.purge_ephemeral()


# ── Main ─────────────────────────────────────────────────────────────


def main():
    args = sys.argv[1:]

    mgr = SessionManager()
    mgr.purge_ephemeral()

    if not args:
        # Launch TUI
        action = curses.wrapper(run_tui)
        if action is None:
            return

        if action[0] == "resume":
            _, sid, cwd, extra = action
            if cwd:
                if os.path.isdir(cwd):
                    os.chdir(cwd)
                else:
                    print(f"\033[1;31m◆ Error:\033[0m Session directory no longer exists: \033[33m{cwd}\033[0m")
                    print("  The session cannot be resumed from a missing directory.")
                    sys.exit(1)
            cmd = ["claude", "--resume", sid] + extra
            opts = f" {' '.join(extra)}" if extra else ""
            print(f"\033[1;36m◆\033[0m Resuming session \033[2m({sid[:8]}…)\033[0m{opts}")
            os.execvp("claude", cmd)

        elif action[0] == "new":
            _, name = action
            cmd_new(mgr, name, [])

        elif action[0] == "tmp":
            cmd_tmp(mgr, [])

    elif args[0] == "help" or args[0] in ("-h", "--help"):
        cmd_help()

    elif args[0] == "new":
        if len(args) < 2:
            print("\033[31mUsage: ccs new <name>\033[0m")
            sys.exit(1)
        cmd_new(mgr, args[1], args[2:])

    elif args[0] == "tmp":
        cmd_tmp(mgr, args[1:])

    else:
        print(f"\033[31mUnknown command: {args[0]}\033[0m")
        print("Run 'ccs help' for usage information.")
        sys.exit(1)


if __name__ == "__main__":
    main()
