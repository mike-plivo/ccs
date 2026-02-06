#!/usr/bin/env python3
"""
ccs — Claude Code Session Manager
A terminal UI and CLI for browsing, managing, and resuming Claude Code sessions.

Usage:
    ccs                                    Interactive TUI
    ccs list                               List all sessions
    ccs resume <id|tag> [-p <profile>]     Resume session
    ccs resume <id|tag> --claude <opts>    Resume with raw claude options
    ccs new <name>                         New named session
    ccs tmp                                Ephemeral session
    ccs pin/unpin <id|tag>                 Pin/unpin a session
    ccs tag <id|tag> <tag>                 Set tag on session
    ccs untag <id|tag>                     Remove tag
    ccs delete <id|tag>                    Delete a session
    ccs delete --empty                     Delete all empty sessions
    ccs search <query>                     Search sessions
    ccs profile list|set|new|delete        Manage profiles
    ccs theme list|set                     Manage themes
    ccs help                               Show help
"""

import curses
import json
import os
import glob
import datetime
import getpass
import signal
import subprocess
import sys
import shutil
import time
import uuid as uuid_mod
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

# ── Paths ─────────────────────────────────────────────────────────────

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
CCS_DIR = Path.home() / ".config" / "ccs"
TAGS_FILE = CCS_DIR / "session_tags.json"
PINS_FILE = CCS_DIR / "session_pins.json"
EPHEMERAL_FILE = CCS_DIR / "ephemeral_sessions.txt"
PROFILES_FILE = CCS_DIR / "ccs_profiles.json"
ACTIVE_PROFILE_FILE = CCS_DIR / "ccs_active_profile.txt"
THEME_FILE = CCS_DIR / "ccs_theme.txt"

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
CP_PROFILE_BADGE = 16

# ── Themes ───────────────────────────────────────────────────────────
# Raw color values: 0=BLACK 1=RED 2=GREEN 3=YELLOW 4=BLUE 5=MAGENTA 6=CYAN 7=WHITE
_BLK, _RED, _GRN, _YLW, _BLU, _MAG, _CYN, _WHT = 0, 1, 2, 3, 4, 5, 6, 7
_DEF = -1  # terminal default

THEME_NAMES = ["dark", "blue", "red", "green", "light"]
DEFAULT_THEME = "dark"

# Each theme: 16 (fg, bg) tuples for CP_HEADER(1) .. CP_PROFILE_BADGE(16)
THEMES = {
    "dark": [
        (_CYN, _DEF), (_CYN, _DEF), (_YLW, _DEF), (_GRN, _DEF),
        (_WHT, _BLU), (_WHT, _DEF), (_MAG, _DEF), (_RED, _DEF),
        (_WHT, _DEF), (_YLW, _DEF), (_GRN, _DEF), (_YLW, _BLU),
        (_GRN, _BLU), (_MAG, _BLU), (_CYN, _DEF), (_BLK, _GRN),
    ],
    "blue": [
        (_BLU, _DEF), (_BLU, _DEF), (_YLW, _DEF), (_CYN, _DEF),
        (_WHT, _BLU), (_CYN, _DEF), (_CYN, _DEF), (_RED, _DEF),
        (_WHT, _DEF), (_CYN, _DEF), (_CYN, _DEF), (_YLW, _BLU),
        (_CYN, _BLU), (_WHT, _BLU), (_BLU, _DEF), (_WHT, _BLU),
    ],
    "red": [
        (_RED, _DEF), (_RED, _DEF), (_YLW, _DEF), (_GRN, _DEF),
        (_WHT, _RED), (_WHT, _DEF), (_YLW, _DEF), (_RED, _DEF),
        (_WHT, _DEF), (_YLW, _DEF), (_RED, _DEF), (_YLW, _RED),
        (_GRN, _RED), (_YLW, _RED), (_RED, _DEF), (_WHT, _RED),
    ],
    "green": [
        (_GRN, _DEF), (_GRN, _DEF), (_YLW, _DEF), (_GRN, _DEF),
        (_BLK, _GRN), (_GRN, _DEF), (_GRN, _DEF), (_RED, _DEF),
        (_GRN, _DEF), (_GRN, _DEF), (_GRN, _DEF), (_YLW, _GRN),
        (_WHT, _GRN), (_BLK, _GRN), (_GRN, _DEF), (_BLK, _GRN),
    ],
    "light": [
        (_BLU, _DEF), (_BLU, _DEF), (_RED, _DEF), (_GRN, _DEF),
        (_WHT, _BLU), (_BLK, _DEF), (_MAG, _DEF), (_RED, _DEF),
        (_BLK, _DEF), (_BLU, _DEF), (_GRN, _DEF), (_RED, _BLU),
        (_GRN, _BLU), (_MAG, _BLU), (_BLU, _DEF), (_WHT, _BLU),
    ],
}

# ── Launch option definitions ─────────────────────────────────────────

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

# Row types in profile editor
ROW_MODEL = "model"
ROW_PERMMODE = "permmode"
ROW_TOGGLE = "toggle"
ROW_SYSPROMPT = "sysprompt"
ROW_TOOLS = "tools"
ROW_MCP = "mcp"
ROW_CUSTOM = "custom"
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
        CCS_DIR.mkdir(parents=True, exist_ok=True)
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
        profiles = data if isinstance(data, list) else []
        # Ensure "default" profile always exists
        if not any(p.get("name") == "default" for p in profiles):
            default_prof = {
                "name": "default", "model": "", "permission_mode": "",
                "flags": [], "system_prompt": "", "tools": "",
                "mcp_config": "", "custom_args": "",
            }
            profiles.insert(0, default_prof)
            self._save(PROFILES_FILE, profiles)
        return profiles

    def save_profile(self, profile: dict):
        profiles = self.load_profiles()
        # Replace if same name exists
        profiles = [p for p in profiles if p.get("name") != profile["name"]]
        profiles.append(profile)
        profiles.sort(key=lambda p: p.get("name", ""))
        self._save(PROFILES_FILE, profiles)

    def delete_profile(self, name: str):
        if name.lower() == "default":
            return
        profiles = self.load_profiles()
        profiles = [p for p in profiles if p.get("name") != name]
        self._save(PROFILES_FILE, profiles)

    def load_active_profile_name(self) -> str:
        try:
            if ACTIVE_PROFILE_FILE.exists():
                name = ACTIVE_PROFILE_FILE.read_text().strip()
                if name:
                    return name
        except Exception:
            pass
        return "default"

    def save_active_profile_name(self, name: str):
        ACTIVE_PROFILE_FILE.write_text(name)

    # ── Theme management ─────────────────────────────────────────

    def load_theme(self) -> str:
        try:
            if THEME_FILE.exists():
                name = THEME_FILE.read_text().strip()
                if name in THEME_NAMES:
                    return name
        except Exception:
            pass
        return DEFAULT_THEME

    def save_theme(self, name: str):
        THEME_FILE.write_text(name)

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
        self.mode = "normal"  # normal | search | tag | delete | delete_empty | new | profiles | profile_edit | help | quit
        self.ibuf = ""
        self.delete_label = ""  # label shown in delete confirmation popup
        self.empty_count = 0    # count for delete_empty confirmation

        # Active profile & theme
        self.active_profile_name = self.mgr.load_active_profile_name()
        self.active_theme = self.mgr.load_theme()

        # Profile editor state (shared with profile_edit mode)
        self.launch_model_idx = 0
        self.launch_perm_idx = 0
        self.launch_toggles: List[bool] = [False] * len(TOGGLE_FLAGS)
        self.launch_sysprompt = ""
        self.launch_tools = ""
        self.launch_mcp = ""
        self.launch_custom = ""
        self.launch_editing: Optional[str] = None  # which text field is active

        # Profile manager state
        self.prof_cur = 0             # cursor in profile list
        self.prof_edit_rows: List[Tuple[str, int]] = []
        self.prof_edit_cur = 0        # cursor in profile editor
        self.prof_edit_name = ""      # name field in editor
        self.prof_editing_existing: Optional[str] = None  # original name if editing
        self.prof_delete_confirm = False

        self.status = ""
        self.status_ttl = 0
        self.exit_action: Optional[Tuple] = None
        self.last_ctrl_c: float = 0.0

        self._init_colors()
        self._refresh()

    def _init_colors(self):
        curses.start_color()
        curses.use_default_colors()
        self._apply_theme(self.active_theme)
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.scr.keypad(True)
        self.scr.timeout(100)

    def _apply_theme(self, name: str):
        """Apply a theme by reinitializing all 16 color pairs."""
        color_map = [
            curses.COLOR_BLACK, curses.COLOR_RED, curses.COLOR_GREEN,
            curses.COLOR_YELLOW, curses.COLOR_BLUE, curses.COLOR_MAGENTA,
            curses.COLOR_CYAN, curses.COLOR_WHITE,
        ]
        pairs = THEMES.get(name, THEMES[DEFAULT_THEME])
        for i, (fg, bg) in enumerate(pairs):
            cfn = color_map[fg] if fg >= 0 else -1
            cbn = color_map[bg] if bg >= 0 else -1
            curses.init_pair(i + 1, cfn, cbn)
        self.active_theme = name

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
        # Ignore SIGINT so Ctrl-C comes through as key 3
        old_handler = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
        try:
            self._run_loop()
        finally:
            signal.signal(signal.SIGINT, old_handler)

    def _run_loop(self):
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

            # Ctrl-C: double-tap within 1 second to quit
            if k == 3:
                now = time.monotonic()
                if now - self.last_ctrl_c < 1.0:
                    break
                self.last_ctrl_c = now
                self._set_status("Press Ctrl-C again to quit")
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

        if self.mode == "quit":
            self._draw_confirm_overlay(h, w,
                "Quit",
                "Quit ccs?",
                "")
        elif self.mode == "help":
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

        # │  [active profile]  hints  │
        self._safe(1, 0, "│", bdr)
        self._safe(1, w - 1, "│", bdr)
        prof_badge = f" {self.active_profile_name} "
        self._safe(1, 2, prof_badge,
                   curses.color_pair(CP_PROFILE_BADGE) | curses.A_BOLD)

        hints_map = {
            "normal":  "⏎ Resume  P Profiles  H Theme  p Pin  t Tag  d Del  n New  / Search  ? Help  q Quit",
            "search":  "Type to filter  ·  ⏎ Apply  ·  Esc Cancel",
            "tag":     "Type tag name  ·  ⏎ Apply  ·  Esc Cancel",
            "quit":    "y Quit  ·  n / Esc Cancel",
            "delete":  "y Confirm  ·  n / Esc Cancel",
            "delete_empty": "y Confirm  ·  n / Esc Cancel",
            "new":     "Type session name  ·  ⏎ Create  ·  Esc Cancel",
            "profiles": "⏎ Set active  n New  e Edit  d Delete  Esc Back",
            "profile_edit": "↑↓ Navigate  Space Toggle  ⏎ Save/Edit  Esc Cancel",
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
        elif self.mode in ("quit", "delete", "delete_empty", "profiles", "profile_edit"):
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
            ("    Enter          Resume with active profile", 0),
            ("    P              Profile picker / manager", 0),
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
            ("    H              Cycle theme", 0),
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

    @staticmethod
    def _build_args_from_profile(profile: dict) -> List[str]:
        """Build CLI args list from a profile dict."""
        extra: List[str] = []
        model = profile.get("model", "")
        if model:
            extra.extend(["--model", model])
        perm = profile.get("permission_mode", "")
        if perm:
            extra.extend(["--permission-mode", perm])
        for flag in profile.get("flags", []):
            extra.append(flag)
        if profile.get("system_prompt", "").strip():
            extra.extend(["--system-prompt", profile["system_prompt"].strip()])
        if profile.get("tools", "").strip():
            extra.extend(["--tools", profile["tools"].strip()])
        if profile.get("mcp_config", "").strip():
            extra.extend(["--mcp-config", profile["mcp_config"].strip()])
        if profile.get("custom_args", "").strip():
            extra.extend(profile["custom_args"].strip().split())
        return extra

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
        """Unified profile picker / manager overlay."""
        bdr = curses.color_pair(CP_BORDER) | curses.A_BOLD
        hdr = curses.color_pair(CP_HEADER) | curses.A_BOLD
        dim = curses.color_pair(CP_DIM) | curses.A_DIM
        normal = curses.color_pair(CP_NORMAL)
        sel_attr = curses.color_pair(CP_SELECTED) | curses.A_BOLD
        tag_attr = curses.color_pair(CP_TAG) | curses.A_BOLD
        warn = curses.color_pair(CP_WARN) | curses.A_BOLD
        badge = curses.color_pair(CP_PROFILE_BADGE) | curses.A_BOLD

        profiles = self.mgr.load_profiles()
        box_w = min(62, w - 4)
        list_h = max(3, min(len(profiles) + 2, h - 10))
        box_h = list_h + 5
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
            scroll = 0
            if self.prof_cur >= scroll + list_h:
                scroll = self.prof_cur - list_h + 1
            if self.prof_cur < scroll:
                scroll = self.prof_cur

            for i in range(list_h):
                idx = scroll + i
                if idx >= len(profiles):
                    break
                p = profiles[idx]
                is_sel = (idx == self.prof_cur)
                y = sy + 2 + i
                name = p.get("name", "?")
                summary = self._profile_summary(p)
                is_active = (name == self.active_profile_name)
                marker = " * " if is_active else "   "

                if is_sel:
                    line = f" ▸{marker}{name:<16s} {summary}"
                    line = line.ljust(box_w - 3)[:box_w - 3]
                    self._safe(y, sx + 1, line, sel_attr)
                    if is_active:
                        self._safe(y, sx + 3, " * ", badge)
                else:
                    self._safe(y, sx + 1, "  ", normal)
                    if is_active:
                        self._safe(y, sx + 3, " * ", badge)
                    else:
                        self._safe(y, sx + 3, "   ", normal)
                    self._safe(y, sx + 6, name, tag_attr)
                    self._safe(y, sx + 6 + 16 + 1, summary[:box_w - 26], dim)

        # Delete confirmation
        if self.prof_delete_confirm and profiles:
            pname = profiles[self.prof_cur].get("name", "?")
            self._safe(sy + box_h - 3, sx + 3,
                       f"Delete '{pname}'? y/N", warn)

        # Hints
        hints = " ⏎ Set active  n New  e Edit  d Delete  Esc Back "
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
        if self.mode not in ("normal", "help", "delete", "delete_empty", "quit", "profiles", "profile_edit"):
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
            "profiles": self._input_profiles,
            "profile_edit": self._input_profile_edit,
            "help": self._input_help,
            "quit": self._input_quit,
        }
        handler = dispatch.get(self.mode, self._input_normal)
        return handler(k)

    def _input_normal(self, k: int) -> Optional[str]:
        if k == ord("q"):
            self.mode = "quit"
            return None
        elif k == 27:  # Esc
            if self.query:
                self.query = ""
                self._apply_filter()
            else:
                self.mode = "quit"
                return None

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
                profiles = self.mgr.load_profiles()
                active = next(
                    (p for p in profiles if p.get("name") == self.active_profile_name),
                    None,
                )
                extra = self._build_args_from_profile(active) if active else []
                self.exit_action = ("resume", s.id, s.cwd, extra)
                return "action"
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
        elif k == ord("H"):
            # Cycle theme
            idx = THEME_NAMES.index(self.active_theme) if self.active_theme in THEME_NAMES else 0
            idx = (idx + 1) % len(THEME_NAMES)
            self._apply_theme(THEME_NAMES[idx])
            self.mgr.save_theme(self.active_theme)
            self._set_status(f"Theme: {self.active_theme}")
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

    def _launch_edit_backspace(self):
        if self.launch_editing == ROW_SYSPROMPT:
            self.launch_sysprompt = self.launch_sysprompt[:-1]
        elif self.launch_editing == ROW_TOOLS:
            self.launch_tools = self.launch_tools[:-1]
        elif self.launch_editing == ROW_MCP:
            self.launch_mcp = self.launch_mcp[:-1]
        elif self.launch_editing == ROW_CUSTOM:
            self.launch_custom = self.launch_custom[:-1]

    def _launch_edit_char(self, ch: str):
        if self.launch_editing == ROW_SYSPROMPT:
            self.launch_sysprompt += ch
        elif self.launch_editing == ROW_TOOLS:
            self.launch_tools += ch
        elif self.launch_editing == ROW_MCP:
            self.launch_mcp += ch
        elif self.launch_editing == ROW_CUSTOM:
            self.launch_custom += ch

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
                # If deleted profile was active, revert to default
                if self.active_profile_name == pname:
                    self.active_profile_name = "default"
                    self.mgr.save_active_profile_name("default")
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

        elif k in (ord("\n"), curses.KEY_ENTER, 10, 13):
            # Set as active profile
            if profiles:
                pname = profiles[self.prof_cur].get("name", "")
                self.active_profile_name = pname
                self.mgr.save_active_profile_name(pname)
                self._set_status(f"Active profile: {pname}")
                self.mode = "normal"

        elif k == ord("n"):
            # New profile
            self._prof_open_editor(None)

        elif k == ord("e"):
            # Edit selected profile
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

    def _input_quit(self, k: int) -> Optional[str]:
        if k == ord("y"):
            return "quit"
        self.mode = "normal"
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


# ── CLI helpers ──────────────────────────────────────────────────────


def _find_session(mgr: SessionManager, query: str) -> Session:
    """Resolve a session by exact tag or ID prefix. Exits on ambiguity."""
    sessions = mgr.scan()
    # Exact tag match
    by_tag = [s for s in sessions if s.tag and s.tag == query]
    if len(by_tag) == 1:
        return by_tag[0]
    if len(by_tag) > 1:
        print(f"\033[31mAmbiguous tag '{query}' — matches {len(by_tag)} sessions\033[0m")
        sys.exit(1)
    # ID prefix match
    by_id = [s for s in sessions if s.id.startswith(query)]
    if len(by_id) == 1:
        return by_id[0]
    if len(by_id) > 1:
        print(f"\033[31mAmbiguous ID prefix '{query}' — matches {len(by_id)} sessions\033[0m")
        sys.exit(1)
    print(f"\033[31mNo session found matching '{query}'\033[0m")
    sys.exit(1)


def _get_profile_extra(mgr: SessionManager, profile_name: Optional[str] = None) -> List[str]:
    """Get CLI args from a profile (active by default)."""
    profiles = mgr.load_profiles()
    name = profile_name or mgr.load_active_profile_name()
    prof = next((p for p in profiles if p.get("name") == name), None)
    if prof:
        return CCSApp._build_args_from_profile(prof)
    return []


# ── CLI commands ─────────────────────────────────────────────────────


def cmd_help():
    print("""\033[1;36m◆ ccs — Claude Code Session Manager\033[0m

\033[1mUsage:\033[0m
  ccs                                    Interactive TUI
  ccs list                               List all sessions
  ccs resume <id|tag> [-p <profile>]     Resume session
  ccs resume <id|tag> --claude <opts>    Resume with raw claude options
  ccs new <name>                         New named session
  ccs tmp                                Ephemeral session
  ccs pin <id|tag>                       Pin a session
  ccs unpin <id|tag>                     Unpin a session
  ccs tag <id|tag> <tag>                 Set tag on session
  ccs untag <id|tag>                     Remove tag from session
  ccs delete <id|tag>                    Delete a session
  ccs delete --empty                     Delete all empty sessions
  ccs search <query>                     Search sessions by text
  ccs profile list                       List profiles
  ccs profile set <name>                 Set active profile
  ccs profile new <name> [flags]         Create profile from CLI flags
  ccs profile delete <name>              Delete a profile
  ccs theme list                         List themes
  ccs theme set <name>                   Set theme
  ccs help                               Show this help

\033[1mProfile creation flags:\033[0m
  --model <model>                        Model name
  --permission-mode <mode>               Permission mode
  --verbose                              Verbose flag
  --dangerously-skip-permissions         Skip permissions flag
  --print                                Print flag
  --continue                             Continue flag
  --no-session-persistence               No session persistence
  --system-prompt <prompt>               System prompt
  --tools <tools>                        Tools
  --mcp-config <path>                    MCP config path

\033[1mTUI Keybindings:\033[0m
  \033[36m↑/↓\033[0m or \033[36mj/k\033[0m       Navigate sessions
  \033[36mEnter\033[0m             Resume with active profile
  \033[36mP\033[0m                 Profile picker / manager
  \033[36mH\033[0m                 Cycle theme
  \033[36mp\033[0m                 Toggle pin
  \033[36mt\033[0m / \033[36mT\033[0m             Tag / remove tag
  \033[36md\033[0m / \033[36mD\033[0m             Delete session / delete empties
  \033[36mn\033[0m                 New named session
  \033[36me\033[0m                 Ephemeral session
  \033[36m/\033[0m                 Search / filter
  \033[36mr\033[0m                 Refresh
  \033[36mq\033[0m                 Quit""")


def cmd_list(mgr: SessionManager):
    sessions = mgr.scan()
    if not sessions:
        print("No sessions found.")
        return
    for s in sessions:
        pin = "★ " if s.pinned else "  "
        tag = f"[{s.tag}] " if s.tag else ""
        label = s.label[:60]
        print(f"  {pin}{tag}{s.ts}  {s.id[:12]}  {s.project_display[:24]:<24s}  {label}")


def cmd_resume(mgr: SessionManager, query: str, profile_name: Optional[str],
               claude_args: Optional[List[str]]):
    s = _find_session(mgr, query)
    if claude_args is not None:
        extra = claude_args
    else:
        extra = _get_profile_extra(mgr, profile_name)
    if s.cwd:
        if os.path.isdir(s.cwd):
            os.chdir(s.cwd)
        else:
            print(f"\033[1;31m◆ Error:\033[0m Directory no longer exists: \033[33m{s.cwd}\033[0m")
            sys.exit(1)
    opts = f" {' '.join(extra)}" if extra else ""
    print(f"\033[1;36m◆\033[0m Resuming session \033[2m({s.id[:8]}…)\033[0m{opts}")
    cmd = ["claude", "--resume", s.id] + extra
    os.execvp("claude", cmd)


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


def cmd_pin(mgr: SessionManager, query: str):
    s = _find_session(mgr, query)
    pins = mgr._load(PINS_FILE, [])
    if s.id not in pins:
        pins.append(s.id)
        mgr._save(PINS_FILE, pins)
        print(f"★ Pinned: {s.tag or s.id[:12]}")
    else:
        print(f"Already pinned: {s.tag or s.id[:12]}")


def cmd_unpin(mgr: SessionManager, query: str):
    s = _find_session(mgr, query)
    pins = mgr._load(PINS_FILE, [])
    if s.id in pins:
        pins.remove(s.id)
        mgr._save(PINS_FILE, pins)
        print(f"Unpinned: {s.tag or s.id[:12]}")
    else:
        print(f"Not pinned: {s.tag or s.id[:12]}")


def cmd_tag(mgr: SessionManager, query: str, tag: str):
    s = _find_session(mgr, query)
    mgr.set_tag(s.id, tag)
    print(f"Tagged [{tag}]: {s.id[:12]}")


def cmd_untag(mgr: SessionManager, query: str):
    s = _find_session(mgr, query)
    if s.tag:
        mgr.remove_tag(s.id)
        print(f"Removed tag from: {s.id[:12]}")
    else:
        print(f"No tag on: {s.id[:12]}")


def cmd_delete_session(mgr: SessionManager, query: str):
    s = _find_session(mgr, query)
    label = s.tag or s.label[:40] or s.id[:12]
    print(f"Delete '{label}'? [y/N] ", end="", flush=True)
    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer == "y":
        mgr.delete(s)
        print(f"Deleted: {label}")
    else:
        print("Cancelled.")


def cmd_delete_empty(mgr: SessionManager):
    sessions = mgr.scan()
    empty = [s for s in sessions if not s.first_msg and not s.summary]
    if not empty:
        print("No empty sessions to delete.")
        return
    print(f"Delete {len(empty)} empty session{'s' if len(empty) != 1 else ''}? [y/N] ",
          end="", flush=True)
    try:
        answer = input().strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer == "y":
        for s in empty:
            mgr.delete(s)
        print(f"Deleted {len(empty)} empty session{'s' if len(empty) != 1 else ''}.")
    else:
        print("Cancelled.")


def cmd_search(mgr: SessionManager, query: str):
    sessions = mgr.scan()
    q = query.lower()
    matches = [
        s for s in sessions
        if q in s.label.lower()
        or q in s.project_display.lower()
        or q in s.tag.lower()
        or q in s.id.lower()
        or q in s.cwd.lower()
    ]
    if not matches:
        print(f"No sessions matching '{query}'.")
        return
    print(f"{len(matches)} match{'es' if len(matches) != 1 else ''}:")
    for s in matches:
        pin = "★ " if s.pinned else "  "
        tag = f"[{s.tag}] " if s.tag else ""
        label = s.label[:60]
        print(f"  {pin}{tag}{s.ts}  {s.id[:12]}  {s.project_display[:24]:<24s}  {label}")


def cmd_profile_list(mgr: SessionManager):
    profiles = mgr.load_profiles()
    active = mgr.load_active_profile_name()
    if not profiles:
        print("No profiles.")
        return
    for p in profiles:
        name = p.get("name", "?")
        marker = " *" if name == active else "  "
        summary = CCSApp._profile_summary(p)
        print(f"  {marker} {name:<16s}  {summary}")


def cmd_profile_set(mgr: SessionManager, name: str):
    profiles = mgr.load_profiles()
    if not any(p.get("name") == name for p in profiles):
        print(f"\033[31mProfile '{name}' not found.\033[0m")
        sys.exit(1)
    mgr.save_active_profile_name(name)
    print(f"Active profile: {name}")


def cmd_profile_new(mgr: SessionManager, name: str, cli_args: List[str]):
    """Create a profile from CLI flags."""
    profile = {
        "name": name, "model": "", "permission_mode": "",
        "flags": [], "system_prompt": "", "tools": "",
        "mcp_config": "", "custom_args": "",
    }
    i = 0
    flags_list = []
    while i < len(cli_args):
        a = cli_args[i]
        if a == "--model" and i + 1 < len(cli_args):
            profile["model"] = cli_args[i + 1]
            i += 2
        elif a == "--permission-mode" and i + 1 < len(cli_args):
            profile["permission_mode"] = cli_args[i + 1]
            i += 2
        elif a == "--system-prompt" and i + 1 < len(cli_args):
            profile["system_prompt"] = cli_args[i + 1]
            i += 2
        elif a == "--tools" and i + 1 < len(cli_args):
            profile["tools"] = cli_args[i + 1]
            i += 2
        elif a == "--mcp-config" and i + 1 < len(cli_args):
            profile["mcp_config"] = cli_args[i + 1]
            i += 2
        elif a in ("--verbose", "--dangerously-skip-permissions",
                    "--print", "--continue", "--no-session-persistence"):
            flags_list.append(a)
            i += 1
        else:
            # Unknown flag → custom args
            profile["custom_args"] = " ".join(cli_args[i:])
            break
    profile["flags"] = flags_list
    mgr.save_profile(profile)
    print(f"Created profile: {name}")


def cmd_profile_delete(mgr: SessionManager, name: str):
    if name.lower() == "default":
        print("\033[31mCannot delete the default profile.\033[0m")
        sys.exit(1)
    profiles = mgr.load_profiles()
    if not any(p.get("name") == name for p in profiles):
        print(f"\033[31mProfile '{name}' not found.\033[0m")
        sys.exit(1)
    mgr.delete_profile(name)
    # Revert active if deleted
    if mgr.load_active_profile_name() == name:
        mgr.save_active_profile_name("default")
    print(f"Deleted profile: {name}")


def cmd_theme_list(mgr: SessionManager):
    current = mgr.load_theme()
    for t in THEME_NAMES:
        marker = " *" if t == current else "  "
        print(f"  {marker} {t}")


def cmd_theme_set(mgr: SessionManager, name: str):
    if name not in THEME_NAMES:
        print(f"\033[31mUnknown theme '{name}'. Available: {', '.join(THEME_NAMES)}\033[0m")
        sys.exit(1)
    mgr.save_theme(name)
    print(f"Theme set to: {name}")


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

        return

    verb = args[0]

    if verb in ("help", "-h", "--help"):
        cmd_help()

    elif verb == "list":
        cmd_list(mgr)

    elif verb == "resume":
        if len(args) < 2:
            print("\033[31mUsage: ccs resume <id|tag> [-p <profile>] [--claude <opts>]\033[0m")
            sys.exit(1)
        query = args[1]
        profile_name = None
        claude_args = None
        i = 2
        while i < len(args):
            if args[i] == "-p" and i + 1 < len(args):
                profile_name = args[i + 1]
                i += 2
            elif args[i] == "--claude":
                claude_args = args[i + 1:]
                break
            else:
                i += 1
        cmd_resume(mgr, query, profile_name, claude_args)

    elif verb == "new":
        if len(args) < 2:
            print("\033[31mUsage: ccs new <name>\033[0m")
            sys.exit(1)
        cmd_new(mgr, args[1], args[2:])

    elif verb == "tmp":
        cmd_tmp(mgr, args[1:])

    elif verb == "pin":
        if len(args) < 2:
            print("\033[31mUsage: ccs pin <id|tag>\033[0m")
            sys.exit(1)
        cmd_pin(mgr, args[1])

    elif verb == "unpin":
        if len(args) < 2:
            print("\033[31mUsage: ccs unpin <id|tag>\033[0m")
            sys.exit(1)
        cmd_unpin(mgr, args[1])

    elif verb == "tag":
        if len(args) < 3:
            print("\033[31mUsage: ccs tag <id|tag> <newtag>\033[0m")
            sys.exit(1)
        cmd_tag(mgr, args[1], args[2])

    elif verb == "untag":
        if len(args) < 2:
            print("\033[31mUsage: ccs untag <id|tag>\033[0m")
            sys.exit(1)
        cmd_untag(mgr, args[1])

    elif verb == "delete":
        if len(args) >= 2 and args[1] == "--empty":
            cmd_delete_empty(mgr)
        elif len(args) >= 2:
            cmd_delete_session(mgr, args[1])
        else:
            print("\033[31mUsage: ccs delete <id|tag> | ccs delete --empty\033[0m")
            sys.exit(1)

    elif verb == "search":
        if len(args) < 2:
            print("\033[31mUsage: ccs search <query>\033[0m")
            sys.exit(1)
        cmd_search(mgr, " ".join(args[1:]))

    elif verb == "profile":
        if len(args) < 2:
            print("\033[31mUsage: ccs profile list|set|new|delete\033[0m")
            sys.exit(1)
        sub = args[1]
        if sub == "list":
            cmd_profile_list(mgr)
        elif sub == "set":
            if len(args) < 3:
                print("\033[31mUsage: ccs profile set <name>\033[0m")
                sys.exit(1)
            cmd_profile_set(mgr, args[2])
        elif sub == "new":
            if len(args) < 3:
                print("\033[31mUsage: ccs profile new <name> [--model ...] [flags]\033[0m")
                sys.exit(1)
            cmd_profile_new(mgr, args[2], args[3:])
        elif sub == "delete":
            if len(args) < 3:
                print("\033[31mUsage: ccs profile delete <name>\033[0m")
                sys.exit(1)
            cmd_profile_delete(mgr, args[2])
        else:
            print(f"\033[31mUnknown profile command: {sub}\033[0m")
            sys.exit(1)

    elif verb == "theme":
        if len(args) < 2:
            print("\033[31mUsage: ccs theme list|set\033[0m")
            sys.exit(1)
        sub = args[1]
        if sub == "list":
            cmd_theme_list(mgr)
        elif sub == "set":
            if len(args) < 3:
                print(f"\033[31mUsage: ccs theme set <{'|'.join(THEME_NAMES)}>\033[0m")
                sys.exit(1)
            cmd_theme_set(mgr, args[2])
        else:
            print(f"\033[31mUnknown theme command: {sub}\033[0m")
            sys.exit(1)

    else:
        print(f"\033[31mUnknown command: {verb}\033[0m")
        print("Run 'ccs help' for usage information.")
        sys.exit(1)


if __name__ == "__main__":
    main()
