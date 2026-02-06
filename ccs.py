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

# ── Launch option presets ─────────────────────────────────────────────

MODELS = [
    ("default", ""),
    ("opus", "claude-opus-4-6"),
    ("sonnet", "claude-sonnet-4-5-20250929"),
    ("haiku", "claude-haiku-4-5-20251001"),
]

# Launch overlay row indices
LR_MODEL = 0
LR_VERBOSE = 1
LR_NOPERMS = 2
LR_PRINT = 3
LR_CUSTOM = 4
LR_LAUNCH = 5
LR_COUNT = 6

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
        self.mode = "normal"  # normal | search | tag | delete | delete_empty | new | launch | help
        self.ibuf = ""
        self.delete_label = ""  # label shown in delete confirmation popup
        self.empty_count = 0    # count for delete_empty confirmation

        # Launch options state
        self.launch_session: Optional[Session] = None
        self.launch_cur = 0           # selected row in launch overlay
        self.launch_model_idx = 0     # index into MODELS
        self.launch_verbose = False
        self.launch_noperms = False
        self.launch_print = False
        self.launch_custom = ""
        self.launch_editing = False   # typing in custom args field
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
            "normal":  "⏎ Resume  o Options  p Pin  t Tag  d Del  D Purge  n New  / Search  ? Help  q Quit",
            "search":  "Type to filter  ·  ⏎ Apply  ·  Esc Cancel",
            "tag":     "Type tag name  ·  ⏎ Apply  ·  Esc Cancel",
            "delete":  "y Confirm  ·  n / Esc Cancel",
            "delete_empty": "y Confirm  ·  n / Esc Cancel",
            "new":     "Type session name  ·  ⏎ Create  ·  Esc Cancel",
            "launch":  "↑↓ Navigate  Space Toggle  ⏎ Launch  Esc Cancel",
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
        elif self.mode in ("delete", "delete_empty"):
            pass  # confirmation shown as centered popup overlay
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
            ("    o              Resume with options (model, flags)", 0),
            ("    p              Toggle pin (pinned sort to top)", 0),
            ("    t              Tag a session", 0),
            ("    T              Remove tag from session", 0),
            ("    d              Delete a session (default: N)", 0),
            ("    D              Delete all empty sessions", 0),
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

    def _draw_launch_overlay(self, h: int, w: int):
        """Draw the launch options popup."""
        bdr = curses.color_pair(CP_BORDER) | curses.A_BOLD
        hdr = curses.color_pair(CP_HEADER) | curses.A_BOLD
        dim = curses.color_pair(CP_DIM) | curses.A_DIM
        normal = curses.color_pair(CP_NORMAL)
        sel = curses.color_pair(CP_SELECTED) | curses.A_BOLD
        accent = curses.color_pair(CP_ACCENT) | curses.A_BOLD

        s = self.launch_session
        model_name = MODELS[self.launch_model_idx][0]

        # Build rows: (text, attr, is_selected)
        def row_attr(r):
            return sel if self.launch_cur == r else normal

        def indicator(r):
            return " ▸ " if self.launch_cur == r else "   "

        def checkbox(val):
            return "[x]" if val else "[ ]"

        rows = []
        # Row 0: Model
        rows.append((f"{indicator(LR_MODEL)}Model:  {model_name}", row_attr(LR_MODEL)))
        # Row 1: verbose
        rows.append((f"{indicator(LR_VERBOSE)}--verbose             {checkbox(self.launch_verbose)}", row_attr(LR_VERBOSE)))
        # Row 2: no-permissions
        rows.append((f"{indicator(LR_NOPERMS)}--no-permissions      {checkbox(self.launch_noperms)}", row_attr(LR_NOPERMS)))
        # Row 3: print
        rows.append((f"{indicator(LR_PRINT)}--print               {checkbox(self.launch_print)}", row_attr(LR_PRINT)))
        # Row 4: custom args
        cursor_ch = "▏" if (self.launch_cur == LR_CUSTOM and self.launch_editing) else ""
        rows.append((f"{indicator(LR_CUSTOM)}Custom: {self.launch_custom}{cursor_ch}", row_attr(LR_CUSTOM)))
        # Row 5: Launch button
        launch_attr = curses.color_pair(CP_STATUS) | curses.A_BOLD if self.launch_cur == LR_LAUNCH else accent
        rows.append((f"{indicator(LR_LAUNCH)}>>> Launch <<<", launch_attr))

        # Session label
        slabel = s.tag or s.label[:40] if s else ""

        box_w = min(50, w - 4)
        box_h = len(rows) + 6  # title + session + blank + rows + blank + hints
        sx = max(0, (w - box_w) // 2)
        sy = max(0, (h - box_h) // 2)

        # Top border
        self._safe(sy, sx, "┌", bdr)
        self._hline(sy, sx + 1, "─", box_w - 2, bdr)
        self._safe(sy, sx + box_w - 1, "┐", bdr)
        title = " Launch Options "
        ttx = sx + max(1, (box_w - len(title)) // 2)
        self._safe(sy, ttx, title, hdr)

        # Content area
        for i in range(box_h - 2):
            y = sy + 1 + i
            self._safe(y, sx, "│" + " " * (box_w - 2) + "│", bdr)

        # Session name
        self._safe(sy + 1, sx + 2, f"Session: {slabel}"[:box_w - 4], dim)
        self._safe(sy + 2, sx + 1, "", normal)  # blank line

        # Option rows
        for i, (text, attr) in enumerate(rows):
            self._safe(sy + 3 + i, sx + 1, text[:box_w - 3], attr)

        # Hints
        hints = " Space toggle · ⏎ launch · Esc cancel "
        hx = sx + max(1, (box_w - len(hints)) // 2)
        self._safe(sy + 3 + len(rows), hx, hints[:box_w - 3], dim)

        # Bottom border
        self._safe(sy + box_h - 1, sx, "└", bdr)
        self._hline(sy + box_h - 1, sx + 1, "─", box_w - 2, bdr)
        self._safe(sy + box_h - 1, sx + box_w - 1, "┘", bdr)

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
        if self.mode not in ("normal", "help", "delete", "delete_empty", "launch"):
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
                self.launch_cur = LR_LAUNCH
                self.launch_model_idx = 0
                self.launch_verbose = False
                self.launch_noperms = False
                self.launch_print = False
                self.launch_custom = ""
                self.launch_editing = False
                self.mode = "launch"
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
        if self.launch_editing:
            # Typing into the custom args field
            if k == 27:  # Esc exits editing
                self.launch_editing = False
            elif k in (ord("\n"), curses.KEY_ENTER, 10, 13):
                self.launch_editing = False
            elif k in (curses.KEY_BACKSPACE, 127, 8):
                self.launch_custom = self.launch_custom[:-1]
            elif 32 <= k <= 126:
                self.launch_custom += chr(k)
            return None

        # Normal navigation in the overlay
        if k == 27:  # Esc
            self.mode = "normal"
        elif k in (curses.KEY_UP, ord("k")):
            self.launch_cur = max(0, self.launch_cur - 1)
        elif k in (curses.KEY_DOWN, ord("j")):
            self.launch_cur = min(LR_COUNT - 1, self.launch_cur + 1)

        elif k == ord(" "):
            # Toggle / cycle the current row
            if self.launch_cur == LR_MODEL:
                self.launch_model_idx = (self.launch_model_idx + 1) % len(MODELS)
            elif self.launch_cur == LR_VERBOSE:
                self.launch_verbose = not self.launch_verbose
            elif self.launch_cur == LR_NOPERMS:
                self.launch_noperms = not self.launch_noperms
            elif self.launch_cur == LR_PRINT:
                self.launch_print = not self.launch_print
            elif self.launch_cur == LR_CUSTOM:
                self.launch_editing = True
            elif self.launch_cur == LR_LAUNCH:
                return self._do_launch()

        elif k in (ord("\n"), curses.KEY_ENTER, 10, 13):
            if self.launch_cur == LR_CUSTOM:
                self.launch_editing = True
            else:
                return self._do_launch()

        return None

    def _do_launch(self) -> str:
        """Build CLI args from launch options and set exit action."""
        s = self.launch_session
        extra: List[str] = []

        model_id = MODELS[self.launch_model_idx][1]
        if model_id:
            extra.extend(["--model", model_id])
        if self.launch_verbose:
            extra.append("--verbose")
        if self.launch_noperms:
            extra.append("--dangerously-skip-permissions")
        if self.launch_print:
            extra.append("--print")
        if self.launch_custom.strip():
            extra.extend(self.launch_custom.strip().split())

        self.exit_action = ("resume", s.id, s.cwd, extra)
        return "action"

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
  \033[36mo\033[0m                 Resume with options (model, flags)
  \033[36mp\033[0m                 Toggle pin (pinned sort to top)
  \033[36mt\033[0m                 Tag a session
  \033[36mT\033[0m                 Remove tag from session
  \033[36md\033[0m                 Delete a session
  \033[36mD\033[0m                 Delete all empty sessions
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
