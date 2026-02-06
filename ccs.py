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
    ccs tag rename <oldtag> <newtag>       Rename a tag
    ccs untag <id|tag>                     Remove tag
    ccs chdir <id|tag> <path>              Set session working directory
    ccs delete <id|tag>                    Delete a session
    ccs delete --empty                     Delete all empty sessions
    ccs search <query>                     Search sessions
    ccs export <id|tag>                    Export session as markdown
    ccs profile list|set|new|delete        Manage profiles
    ccs theme list|set                     Manage themes
    ccs tmux list                          List running tmux sessions
    ccs tmux attach <name>                 Attach to tmux session
    ccs tmux kill <name>                   Kill a tmux session
    ccs tmux kill --all                    Kill all tmux sessions
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
import shlex
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
CWDS_FILE = CCS_DIR / "session_cwds.json"
EPHEMERAL_FILE = CCS_DIR / "ephemeral_sessions.txt"
PROFILES_FILE = CCS_DIR / "ccs_profiles.json"
ACTIVE_PROFILE_FILE = CCS_DIR / "ccs_active_profile.txt"
THEME_FILE = CCS_DIR / "ccs_theme.txt"
CACHE_FILE = CCS_DIR / "session_cache.json"
TMUX_FILE = CCS_DIR / "tmux_sessions.json"
HAS_TMUX = shutil.which("tmux") is not None
TMUX_PREFIX = "ccs-"

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
CP_AGE_TODAY = 17
CP_AGE_WEEK = 18
CP_AGE_OLD = 19

# ── Themes ───────────────────────────────────────────────────────────
# Raw color values: 0=BLACK 1=RED 2=GREEN 3=YELLOW 4=BLUE 5=MAGENTA 6=CYAN 7=WHITE
_BLK, _RED, _GRN, _YLW, _BLU, _MAG, _CYN, _WHT = 0, 1, 2, 3, 4, 5, 6, 7
_DEF = -1  # terminal default

THEME_NAMES = ["dark", "blue", "red", "green", "light"]
DEFAULT_THEME = "dark"

# Each theme: 19 (fg, bg) tuples for CP_HEADER(1) .. CP_AGE_OLD(19)
THEMES = {
    "dark": [
        (_CYN, _DEF), (_CYN, _DEF), (_YLW, _DEF), (_GRN, _DEF),
        (_WHT, _BLU), (_WHT, _DEF), (_MAG, _DEF), (_RED, _DEF),
        (_WHT, _DEF), (_YLW, _DEF), (_GRN, _DEF), (_YLW, _BLU),
        (_GRN, _BLU), (_MAG, _BLU), (_CYN, _DEF), (_BLK, _GRN),
        (_GRN, _DEF), (_YLW, _DEF), (_WHT, _DEF),
    ],
    "blue": [
        (_BLU, _DEF), (_BLU, _DEF), (_YLW, _DEF), (_CYN, _DEF),
        (_WHT, _BLU), (_CYN, _DEF), (_CYN, _DEF), (_RED, _DEF),
        (_WHT, _DEF), (_CYN, _DEF), (_CYN, _DEF), (_YLW, _BLU),
        (_CYN, _BLU), (_WHT, _BLU), (_BLU, _DEF), (_WHT, _BLU),
        (_GRN, _DEF), (_CYN, _DEF), (_WHT, _DEF),
    ],
    "red": [
        (_RED, _DEF), (_RED, _DEF), (_YLW, _DEF), (_GRN, _DEF),
        (_WHT, _RED), (_WHT, _DEF), (_YLW, _DEF), (_RED, _DEF),
        (_WHT, _DEF), (_YLW, _DEF), (_RED, _DEF), (_YLW, _RED),
        (_GRN, _RED), (_YLW, _RED), (_RED, _DEF), (_WHT, _RED),
        (_GRN, _DEF), (_YLW, _DEF), (_WHT, _DEF),
    ],
    "green": [
        (_GRN, _DEF), (_GRN, _DEF), (_YLW, _DEF), (_GRN, _DEF),
        (_BLK, _GRN), (_GRN, _DEF), (_GRN, _DEF), (_RED, _DEF),
        (_GRN, _DEF), (_GRN, _DEF), (_GRN, _DEF), (_YLW, _GRN),
        (_WHT, _GRN), (_BLK, _GRN), (_GRN, _DEF), (_BLK, _GRN),
        (_GRN, _DEF), (_YLW, _DEF), (_WHT, _DEF),
    ],
    "light": [
        (_BLU, _DEF), (_BLU, _DEF), (_RED, _DEF), (_GRN, _DEF),
        (_WHT, _BLU), (_BLK, _DEF), (_MAG, _DEF), (_RED, _DEF),
        (_BLK, _DEF), (_BLU, _DEF), (_GRN, _DEF), (_RED, _BLU),
        (_GRN, _BLU), (_MAG, _BLU), (_BLU, _DEF), (_WHT, _BLU),
        (_GRN, _DEF), (_BLU, _DEF), (_BLK, _DEF),
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
ROW_EXPERT = "expert"
ROW_TMUX = "tmux"
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
    msg_count: int = 0

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

    def get_sort_key(self, sort_mode: str = "date") -> Tuple:
        tier = 0 if self.pinned else 1
        if sort_mode == "name":
            return (tier, self.label.lower(), -self.mtime)
        elif sort_mode == "project":
            return (tier, self.project_display.lower(), -self.mtime)
        return (tier, -self.mtime)


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

    def scan(self, sort_mode: str = "date", force: bool = False) -> List[Session]:
        tags = self._load(TAGS_FILE, {})
        pins = set(self._load(PINS_FILE, []))
        cwd_overrides = self._load(CWDS_FILE, {})
        cache = {} if force else self._load(CACHE_FILE, {})
        out: List[Session] = []
        seen_sids: set = set()
        pattern = str(PROJECTS_DIR / "*" / "*.jsonl")

        for jp in glob.glob(pattern):
            sid = os.path.basename(jp).replace(".jsonl", "")
            seen_sids.add(sid)
            praw = os.path.basename(os.path.dirname(jp))
            pdisp = self._decode_proj(praw)
            tag = tags.get(sid, "")
            pinned = sid in pins
            file_mtime = os.path.getmtime(jp)

            # Check cache
            cached = cache.get(sid)
            if cached and cached.get("mtime") == file_mtime:
                summary = cached.get("summary", "")
                fm = cached.get("first_msg", "")
                fm_long = cached.get("first_msg_long", "")
                cwd = cached.get("cwd", "")
                sums = cached.get("summaries", [])
                msg_count = cached.get("msg_count", 0)
                praw = cached.get("project_raw", praw)
                pdisp = cached.get("project_display", pdisp)
            else:
                summary, fm, fm_long, cwd = "", "", "", ""
                sums: List[str] = []
                msg_count = 0
                try:
                    with open(jp, "r", errors="replace") as f:
                        for ln in f:
                            try:
                                d = json.loads(ln)
                            except Exception:
                                continue
                            msg_type = d.get("type")
                            if msg_type == "summary":
                                s = d.get("summary", "")
                                if s:
                                    sums.append(s)
                                    summary = s
                            elif msg_type in ("user", "assistant"):
                                msg_count += 1
                                if msg_type == "user" and not fm:
                                    cwd = d.get("cwd", "")
                                    txt = self._extract_text(d.get("message", {}))
                                    if txt:
                                        fm = txt[:120].replace("\n", " ").replace("\t", " ")
                                        fm_long = txt[:800]
                except Exception:
                    pass
                cache[sid] = {
                    "mtime": file_mtime,
                    "summary": summary,
                    "first_msg": fm,
                    "first_msg_long": fm_long,
                    "cwd": cwd,
                    "msg_count": msg_count,
                    "summaries": sums,
                    "project_raw": praw,
                    "project_display": pdisp,
                }

            if sid in cwd_overrides:
                cwd = cwd_overrides[sid]

            out.append(Session(
                id=sid, project_raw=praw, project_display=pdisp,
                cwd=cwd, summary=summary, first_msg=fm,
                first_msg_long=fm_long, tag=tag, pinned=pinned,
                mtime=file_mtime, summaries=sums, path=jp,
                msg_count=msg_count,
            ))

        # Prune cache entries for sessions no longer on disk
        pruned = {k: v for k, v in cache.items() if k in seen_sids}
        try:
            self._save(CACHE_FILE, pruned)
        except Exception:
            pass

        out.sort(key=lambda s: s.get_sort_key(sort_mode))
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

    def is_tag_unique(self, tag: str, exclude_sid: str = "") -> bool:
        tags = self._load(TAGS_FILE, {})
        return not any(sid != exclude_sid and t == tag for sid, t in tags.items())

    def set_cwd(self, sid: str, path: str):
        cwds = self._load(CWDS_FILE, {})
        if path:
            cwds[sid] = path
        else:
            cwds.pop(sid, None)
        self._save(CWDS_FILE, cwds)

    def remove_cwd(self, sid: str):
        self.set_cwd(sid, "")

    def delete(self, s: Session):
        if os.path.exists(s.path):
            os.remove(s.path)
        tags = self._load(TAGS_FILE, {})
        tags.pop(s.id, None)
        self._save(TAGS_FILE, tags)
        pins = self._load(PINS_FILE, [])
        pins = [p for p in pins if p != s.id]
        self._save(PINS_FILE, pins)
        cwds = self._load(CWDS_FILE, {})
        cwds.pop(s.id, None)
        self._save(CWDS_FILE, cwds)

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

    # ── Tmux session tracking ────────────────────────────────────

    def tmux_sessions(self) -> dict:
        """Load tracked ccs tmux sessions, prune dead ones."""
        if not HAS_TMUX:
            return {}
        data = self._load(TMUX_FILE, {})
        alive = {}
        for name, info in data.items():
            rc = subprocess.run(["tmux", "has-session", "-t", name],
                                capture_output=True).returncode
            if rc == 0:
                alive[name] = info
        if len(alive) != len(data):
            self._save(TMUX_FILE, alive)
        return alive

    def tmux_register(self, tmux_name: str, session_id: str, profile: str):
        data = self._load(TMUX_FILE, {})
        data[tmux_name] = {"session_id": session_id, "profile": profile,
                           "launched": datetime.datetime.now().isoformat()}
        self._save(TMUX_FILE, data)

    def tmux_unregister(self, tmux_name: str):
        data = self._load(TMUX_FILE, {})
        data.pop(tmux_name, None)
        self._save(TMUX_FILE, data)

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
        self.sort_mode = "date"  # "date" | "name" | "project"
        self.marked: set = set()  # session IDs for bulk operations
        self.chdir_pending = None  # ("resume", sid, cwd, extra) or ("set_cwd", sid, cwd, None)

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
        self.launch_expert_args = ""  # raw CLI args for expert mode
        self.launch_tmux = True  # tmux launch mode toggle
        self.launch_editing: Optional[str] = None  # which text field is active

        # Profile manager state
        self.prof_cur = 0             # cursor in profile list
        self.prof_edit_rows: List[Tuple[str, int]] = []
        self.prof_edit_cur = 0        # cursor in profile editor
        self.prof_edit_name = ""      # name field in editor
        self.prof_editing_existing: Optional[str] = None  # original name if editing
        self.prof_expert_mode = False  # True = expert (raw CLI), False = structured
        self.prof_delete_confirm = False

        # View system
        self.view = "sessions"  # "sessions" | "tmux"
        self.tmux_data: dict = {}  # loaded tmux session data
        self.tmux_cur = 0  # cursor in tmux view
        self.tmux_list: list = []  # sorted list of (name, info) for display

        self.status = ""
        self.status_ttl = 0
        self.exit_action: Optional[Tuple] = None
        self.last_ctrl_c: float = 0.0
        self.confirm_sel = 0  # 0=No (default), 1=Yes — for y/n popups

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
        curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)

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

    def _age_color(self, mtime: float) -> int:
        delta = datetime.datetime.now() - datetime.datetime.fromtimestamp(mtime)
        if delta.days == 0:
            return curses.color_pair(CP_AGE_TODAY)
        elif delta.days < 7:
            return curses.color_pair(CP_AGE_WEEK)
        return curses.color_pair(CP_AGE_OLD) | curses.A_DIM

    def _get_page_size(self) -> int:
        h, _ = self.scr.getmaxyx()
        hdr_h, ftr_h, sep_h = 5, 1, 1
        preview_h = min(14, max(6, (h - hdr_h - ftr_h - sep_h) * 2 // 5))
        return max(1, h - hdr_h - ftr_h - sep_h - preview_h)

    def _handle_mouse(self) -> Optional[str]:
        try:
            _, mx, my, _, bstate = curses.getmouse()
        except curses.error:
            return None
        if self.mode != "normal":
            return None
        h, w = self.scr.getmaxyx()
        hdr_h = 5
        ftr_h = 1
        sep_h = 1
        preview_h = min(14, max(6, (h - hdr_h - ftr_h - sep_h) * 2 // 5))
        list_h = h - hdr_h - ftr_h - sep_h - preview_h
        list_top = hdr_h
        list_bot = list_top + list_h

        # Click on tab bar (row 4) to switch views
        if my == 4 and (bstate & (curses.BUTTON1_CLICKED | curses.BUTTON1_DOUBLE_CLICKED)):
            # "Sessions" tab starts at x=2, "Tmux" tab starts after it
            sess_label = " Sessions "
            tmux_count = len(self.tmux_list)
            tmux_label = f" Tmux ({tmux_count}) "
            sess_end = 2 + len(sess_label)
            tmux_start = sess_end + 1
            tmux_end = tmux_start + len(tmux_label)
            if 2 <= mx < sess_end and self.view != "sessions":
                self.view = "sessions"
                return None
            elif tmux_start <= mx < tmux_end and self.view != "tmux":
                self.view = "tmux"
                self._refresh_tmux()
                return None

        if self.view == "sessions":
            if list_top <= my < list_bot and self.filtered:
                row_idx = self.scroll + (my - list_top)
                if row_idx < len(self.filtered):
                    if bstate & curses.BUTTON1_DOUBLE_CLICKED:
                        self.cur = row_idx
                        s = self.filtered[self.cur]
                        profiles = self.mgr.load_profiles()
                        active = next(
                            (p for p in profiles if p.get("name") == self.active_profile_name),
                            None,
                        )
                        extra = self._build_args_from_profile(active) if active else []
                        use_tmux = active.get("tmux", True) if active else True
                        if use_tmux:
                            if not HAS_TMUX:
                                self._set_status("tmux is not installed — install it or disable in profile")
                                return None
                            if s.cwd and not os.path.isdir(s.cwd):
                                self.chdir_pending = ("resume", s.id, s.cwd, extra)
                                self.mode = "chdir"
                                self.ibuf = str(Path.home())
                                self._set_status(f"Directory missing: {s.cwd}")
                                return None
                            self._tmux_launch(s, extra)
                            self._refresh()
                            return None
                        if s.cwd and not os.path.isdir(s.cwd):
                            self.chdir_pending = ("resume", s.id, s.cwd, extra)
                            self.mode = "chdir"
                            self.ibuf = str(Path.home())
                            self._set_status(f"Directory missing: {s.cwd}")
                            return None
                        self.exit_action = ("resume", s.id, s.cwd, extra)
                        return "action"
                    elif bstate & curses.BUTTON1_CLICKED:
                        self.cur = row_idx
            if bstate & getattr(curses, "BUTTON4_PRESSED", 0):
                self.cur = max(0, self.cur - 3)
            elif bstate & getattr(curses, "BUTTON5_PRESSED", 0):
                if self.filtered:
                    self.cur = min(len(self.filtered) - 1, self.cur + 3)

        elif self.view == "tmux":
            if list_top <= my < list_bot and self.tmux_list:
                row_idx = my - list_top
                if row_idx < len(self.tmux_list):
                    if bstate & curses.BUTTON1_DOUBLE_CLICKED:
                        self.tmux_cur = row_idx
                        name, _ = self.tmux_list[self.tmux_cur]
                        self._tmux_attach(name)
                    elif bstate & curses.BUTTON1_CLICKED:
                        self.tmux_cur = row_idx
            if bstate & getattr(curses, "BUTTON4_PRESSED", 0):
                self.tmux_cur = max(0, self.tmux_cur - 3)
            elif bstate & getattr(curses, "BUTTON5_PRESSED", 0):
                if self.tmux_list:
                    self.tmux_cur = min(len(self.tmux_list) - 1, self.tmux_cur + 3)

        return None

    def _refresh(self, force: bool = False):
        self.sessions = self.mgr.scan(self.sort_mode, force=force)
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
        valid_ids = {s.id for s in self.filtered}
        self.marked &= valid_ids

    def _refresh_tmux(self):
        if HAS_TMUX:
            self.tmux_data = self.mgr.tmux_sessions()
            self.tmux_list = sorted(self.tmux_data.items(),
                                    key=lambda x: x[1].get("launched", ""))
            if self.tmux_cur >= len(self.tmux_list):
                self.tmux_cur = max(0, len(self.tmux_list) - 1)
        else:
            self.tmux_data = {}
            self.tmux_list = []

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
            if k == curses.KEY_MOUSE:
                result = self._handle_mouse()
                if result in ("quit", "action"):
                    break
                continue

            # Ctrl-C: double-tap within 1 second to open quit confirmation
            if k == 3:
                now = time.monotonic()
                if now - self.last_ctrl_c < 1.0:
                    self.confirm_sel = 0
                    self.mode = "quit"
                self.last_ctrl_c = now
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
        hdr_h = 5       # header box + input line + tab bar
        ftr_h = 1       # footer / status bar
        sep_h = 1       # separator between list and preview
        preview_h = min(14, max(6, (h - hdr_h - ftr_h - sep_h) * 2 // 5))
        list_h = h - hdr_h - ftr_h - sep_h - preview_h

        self._draw_header(w)
        self._draw_tab_bar(4, w)

        if self.view == "sessions":
            self._draw_list(hdr_h, list_h, w)
            self._draw_separator(hdr_h + list_h, w)
            self._draw_preview(hdr_h + list_h + sep_h, preview_h, w)
        elif self.view == "tmux":
            self._draw_tmux_list(hdr_h, list_h, w)
            self._draw_separator(hdr_h + list_h, w)
            self._draw_tmux_preview(hdr_h + list_h + sep_h, preview_h, w)
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
            "normal":  "⏎ Resume  R Last  → Tmux  s Sort  Space Mark  P Profiles  d Del  n New  / Search  ? Help  q Quit",
            "normal_tmux": "⏎ Attach  x Kill  ← Sessions  r Refresh  P Profiles  ? Help  q Quit",
            "search":  "Type to filter  ·  ⏎ Apply  ·  Esc Cancel",
            "tag":     "Type tag name  ·  ⏎ Apply  ·  Esc Cancel",
            "quit":    "←/→ Select  ·  ⏎ Confirm  ·  y/n  ·  Esc Cancel",
            "delete":  "←/→ Select  ·  ⏎ Confirm  ·  y/n  ·  Esc Cancel",
            "delete_empty": "←/→ Select  ·  ⏎ Confirm  ·  y/n  ·  Esc Cancel",
            "chdir":   "Type directory path  ·  ⏎ Apply  ·  Esc Cancel",
            "new":     "Type session name  ·  ⏎ Create  ·  Esc Cancel",
            "profiles": "⏎ Set active  n New  e Edit  d Delete  Esc Back",
            "profile_edit": "Tab Expert/Structured  ↑↓ Navigate  Space Toggle  ⏎ Save/Edit  Esc Cancel",
            "help":    "Press any key to close",
        }
        hint_key = "normal_tmux" if self.mode == "normal" and self.view == "tmux" else self.mode
        hints = hints_map.get(hint_key, "")
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
        elif self.mode == "chdir":
            self._safe(y, 1, " CWD:", curses.color_pair(CP_WARN) | curses.A_BOLD)
            self._safe(y, 7, self.ibuf + "▏", curses.color_pair(CP_NORMAL))
        elif self.mode in ("quit", "delete", "delete_empty", "profiles", "profile_edit"):
            pass  # handled by overlay popups
        elif self.query:
            self._safe(y, 1, f" Filter: {self.query}", dim)
            cx = 10 + len(self.query) + 2
            self._safe(y, cx, "(Esc to clear)", curses.color_pair(CP_DIM) | curses.A_DIM)
        else:
            n = len(self.filtered)
            total = len(self.sessions)
            labels = {"date": "Date", "name": "Name", "project": "Project"}
            sort_label = labels.get(self.sort_mode, "Date")
            if n < total:
                info = f" {n}/{total} sessions · Sort: {sort_label}"
            else:
                info = f" {n} session{'s' if n != 1 else ''} · Sort: {sort_label}"
            self._safe(y, 1, info, curses.color_pair(CP_ACCENT))

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

        # Scroll indicators
        if self.scroll > 0:
            self._safe(sy, w - 3, " ▲ ",
                       curses.color_pair(CP_ACCENT) | curses.A_BOLD)
        if self.scroll + height < len(self.filtered):
            last = min(height - 1, len(self.filtered) - self.scroll - 1)
            self._safe(sy + last, w - 3, " ▼ ",
                       curses.color_pair(CP_ACCENT) | curses.A_BOLD)

    def _draw_row(self, y: int, w: int, s: Session, sel: bool):
        """Draw a single session row with color-coded segments."""
        marked = s.id in self.marked

        # Column widths
        ind_w = 3     # " ▸ " or " ● " or "   "
        pin_w = 2     # "★ " or "  "
        ts_w = 18     # "2025-01-15 14:30  "
        msg_w = 5     # "12m " or "  0m "
        proj_w = min(28, max(12, (w - ind_w - pin_w - ts_w - msg_w - 4) // 3))

        tag_str = f"[{s.tag}] " if s.tag else ""
        tag_w = len(tag_str)

        desc_w = max(8, w - ind_w - pin_w - tag_w - ts_w - msg_w - proj_w - 2)

        proj = s.project_display
        if len(proj) > proj_w:
            proj = proj[:proj_w - 2] + ".."
        proj = proj.ljust(proj_w)

        desc = s.label
        if len(desc) > desc_w:
            desc = desc[:desc_w - 1] + "…"

        msg_str = f"{s.msg_count:>3d}m " if s.msg_count else "     "

        # Mark indicator
        if marked:
            mark_ch = "●"
        elif sel:
            mark_ch = "▸"
        else:
            mark_ch = " "

        if sel:
            # Highlight entire row
            base = curses.color_pair(CP_SELECTED) | curses.A_BOLD

            line = f" {mark_ch} {'★ ' if s.pinned else '  '}{tag_str}{s.ts}  {msg_str}{proj} {desc}"
            if len(line) < w - 1:
                line += " " * (w - 1 - len(line))
            line = line[:w - 1]
            self._safe(y, 0, line, base)

            # Overlay colored segments on selection background
            x = 3
            if s.pinned:
                self._safe(y, x, "★", curses.color_pair(CP_SEL_PIN) | curses.A_BOLD)
            x += pin_w
            if s.tag:
                self._safe(y, x, f"[{s.tag}]",
                           curses.color_pair(CP_SEL_TAG) | curses.A_BOLD)
            x += tag_w + ts_w + msg_w
            self._safe(y, x, proj.rstrip(),
                       curses.color_pair(CP_SEL_PROJ) | curses.A_BOLD)
            if marked:
                self._safe(y, 1, "●", curses.color_pair(CP_ACCENT) | curses.A_BOLD)
        else:
            x = 0
            # Indicator
            if marked:
                self._safe(y, x, f" ● ", curses.color_pair(CP_ACCENT) | curses.A_BOLD)
            else:
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

            # Timestamp (age-colored)
            age_attr = self._age_color(s.mtime)
            self._safe(y, x, s.ts + "  ", age_attr)
            x += ts_w

            # Message count
            self._safe(y, x, msg_str, curses.color_pair(CP_DIM) | curses.A_DIM)
            x += msg_w

            # Project
            self._safe(y, x, proj, curses.color_pair(CP_PROJECT))
            x += proj_w

            # Description
            self._safe(y, x + 1, desc, curses.color_pair(CP_NORMAL))

    def _draw_tab_bar(self, y: int, w: int):
        """Draw view tabs: [Sessions] [Tmux (N)]."""
        sel_attr = curses.color_pair(CP_SELECTED) | curses.A_BOLD
        dim = curses.color_pair(CP_DIM) | curses.A_DIM
        tmux_count = len(self.tmux_list)
        sess_label = " Sessions "
        tmux_label = f" Tmux ({tmux_count}) "
        sess_attr = sel_attr if self.view == "sessions" else dim
        tmux_attr = sel_attr if self.view == "tmux" else dim
        x = 2
        self._safe(y, x, sess_label, sess_attr)
        x += len(sess_label) + 1
        self._safe(y, x, tmux_label, tmux_attr)
        x += len(tmux_label) + 1
        hint = "←/→ switch view"
        self._safe(y, x + 1, hint, curses.color_pair(CP_DIM) | curses.A_DIM)

    def _draw_tmux_list(self, sy: int, height: int, w: int):
        """Draw the list of tracked tmux sessions."""
        if not self.tmux_list:
            if not HAS_TMUX:
                msg = "tmux is not installed."
                self._safe(sy + height // 2, max(1, (w - len(msg)) // 2),
                           msg, curses.color_pair(CP_WARN) | curses.A_BOLD)
                hint = "Install tmux or set profile to direct mode."
                self._safe(sy + height // 2 + 1, max(1, (w - len(hint)) // 2),
                           hint, curses.color_pair(CP_DIM) | curses.A_DIM)
            else:
                msg = "No active tmux sessions."
                self._safe(sy + height // 2, max(1, (w - len(msg)) // 2),
                           msg, curses.color_pair(CP_DIM) | curses.A_DIM)
                hint = "Launch a session with Enter to start one."
                self._safe(sy + height // 2 + 1, max(1, (w - len(hint)) // 2),
                           hint, curses.color_pair(CP_DIM) | curses.A_DIM)
            return

        # Scroll handling
        tmux_scroll = 0
        if self.tmux_cur < tmux_scroll:
            tmux_scroll = self.tmux_cur
        if self.tmux_cur >= tmux_scroll + height:
            tmux_scroll = self.tmux_cur - height + 1

        for i in range(height):
            idx = tmux_scroll + i
            if idx >= len(self.tmux_list):
                break
            name, info = self.tmux_list[idx]
            sel = (idx == self.tmux_cur)
            sid = info.get("session_id", "")[:12]
            profile = info.get("profile", "")
            launched = info.get("launched", "")[:16]

            ind = " ▸ " if sel else "   "
            line = f"{ind}{name:<20s}  {sid:<14s}  {profile:<16s}  {launched}"
            if len(line) < w - 1:
                line += " " * (w - 1 - len(line))
            line = line[:w - 1]

            if sel:
                self._safe(sy + i, 0, line,
                           curses.color_pair(CP_SELECTED) | curses.A_BOLD)
            else:
                self._safe(sy + i, 0, ind, curses.color_pair(CP_NORMAL))
                x = len(ind)
                self._safe(sy + i, x, name[:20].ljust(20),
                           curses.color_pair(CP_TAG) | curses.A_BOLD)
                x += 22
                self._safe(sy + i, x, sid.ljust(14),
                           curses.color_pair(CP_DIM) | curses.A_DIM)
                x += 16
                self._safe(sy + i, x, profile[:16].ljust(16),
                           curses.color_pair(CP_PROJECT))
                x += 18
                self._safe(sy + i, x, launched,
                           curses.color_pair(CP_DIM) | curses.A_DIM)

    def _draw_tmux_preview(self, sy: int, h: int, w: int):
        """Show details of selected tmux session."""
        if not self.tmux_list:
            self._safe(sy + 1, 3, "No tmux session selected",
                       curses.color_pair(CP_DIM) | curses.A_DIM)
            return

        name, info = self.tmux_list[self.tmux_cur]
        lines: List[Tuple[str, int]] = []
        lines.append((f"  Tmux name: {name}",
                       curses.color_pair(CP_TAG) | curses.A_BOLD))
        lines.append((f"  Session:   {info.get('session_id', 'N/A')}",
                       curses.color_pair(CP_DIM) | curses.A_DIM))
        lines.append((f"  Profile:   {info.get('profile', 'N/A')}",
                       curses.color_pair(CP_PROJECT)))
        lines.append((f"  Launched:  {info.get('launched', 'N/A')}",
                       curses.color_pair(CP_ACCENT)))
        lines.append(("", 0))
        lines.append(("  Press Enter to attach, x to kill",
                       curses.color_pair(CP_DIM) | curses.A_DIM))

        for i, (text, attr) in enumerate(lines[:h]):
            self._safe(sy + i, 0, text[:w - 1], attr)

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
            cwd_suffix = " (override)" if self.mgr._load(CWDS_FILE, {}).get(s.id) else ""
            lines.append((f"  CWD:     {s.cwd}{cwd_suffix}", curses.color_pair(CP_DIM) | curses.A_DIM))
        lines.append((f"  Modified: {s.ts}  ({s.age})", self._age_color(s.mtime)))
        lines.append((f"  Messages: {s.msg_count}",
                       curses.color_pair(CP_ACCENT)))
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
            ("    Shift+↑/↓      Jump 10 rows", 0),
            ("    PgUp / PgDn    Page up / down", 0),
            ("", 0),
            ("  Actions", curses.color_pair(CP_HEADER) | curses.A_BOLD),
            ("    Enter          Resume with active profile", 0),
            ("    R              Quick resume most recent", 0),
            ("    P              Profile picker / manager", 0),
            ("                   (Tab: expert/structured mode)", 0),
            ("    p              Toggle pin (bulk if marked)", 0),
            ("    t              Set / rename tag", 0),
            ("    T              Remove tag from session", 0),
            ("    c              Change session CWD", 0),
            ("    d              Delete session (bulk if marked)", 0),
            ("    D              Delete all empty sessions", 0),
            ("", 0),
            ("  Bulk & Sort", curses.color_pair(CP_HEADER) | curses.A_BOLD),
            ("    Space          Mark / unmark session", 0),
            ("    u              Unmark all", 0),
            ("    s              Cycle sort: date/name/project", 0),
            ("", 0),
            ("  Sessions", curses.color_pair(CP_HEADER) | curses.A_BOLD),
            ("    n              Create a new named session", 0),
            ("    e              Start an ephemeral session", 0),
            ("", 0),
            ("  Tmux View", curses.color_pair(CP_HEADER) | curses.A_BOLD),
            ("    ←/→ or Tab     Switch Sessions / Tmux view", 0),
            ("    Enter          Attach to tmux session", 0),
            ("    x              Kill tmux session", 0),
            ("", 0),
            ("  Other", curses.color_pair(CP_HEADER) | curses.A_BOLD),
            ("    H              Cycle theme", 0),
            ("    /              Search / filter sessions", 0),
            ("    r              Refresh session list", 0),
            ("    Mouse          Click / dbl-click / scroll", 0),
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
        """Draw a centered y/n confirmation popup with arrow-selectable buttons."""
        warn = curses.color_pair(CP_WARN) | curses.A_BOLD
        bdr = curses.color_pair(CP_WARN)
        dim = curses.color_pair(CP_DIM) | curses.A_DIM
        normal = curses.color_pair(CP_NORMAL)
        sel_attr = curses.color_pair(CP_SELECTED) | curses.A_BOLD

        # Button labels
        yes_label = "  Yes  "
        no_label = "  No   "
        yes_a = sel_attr if self.confirm_sel == 1 else dim
        no_a = sel_attr if self.confirm_sel == 0 else dim

        content_lines = [
            ("", 0),
            (f"  {message}", warn),
            ("", 0),
        ]
        if detail:
            content_lines.append((f"  {detail}", normal))
            content_lines.append(("", 0))
        # Placeholder row for buttons (drawn separately)
        content_lines.append(("", 0))
        content_lines.append(("  ←/→ Select  ·  ⏎ Confirm  ·  y/n  ·  Esc", dim))
        content_lines.append(("", 0))

        box_w = min(max(len(message) + 6, len(detail) + 6 if detail else 0, len(title) + 8, 40), w - 4)
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
        btn_row = -1
        for i in range(box_h - 2):
            y = sy + 1 + i
            self._safe(y, sx, "│" + " " * (box_w - 2) + "│", bdr)
            if i < len(content_lines):
                text, attr = content_lines[i]
                # The button placeholder row (first empty after detail)
                if text == "" and attr == 0 and i > 2 and btn_row < 0:
                    btn_row = y
                else:
                    self._safe(y, sx + 1, text[:box_w - 3], attr)

        # Draw buttons on their row
        if btn_row >= 0:
            gap = 4
            total_w = len(yes_label) + len(no_label) + gap
            bx = sx + max(2, (box_w - total_w) // 2)
            self._safe(btn_row, bx, yes_label, yes_a)
            self._safe(btn_row, bx + len(yes_label) + gap, no_label, no_a)

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
        self.launch_expert_args = profile.get("expert_args", "")
        self.launch_tmux = profile.get("tmux", True)

    def _launch_to_profile_dict(self, name: str) -> dict:
        """Serialize current launch state to a profile dict."""
        if self.prof_expert_mode:
            return {
                "name": name,
                "model": "", "permission_mode": "", "flags": [],
                "system_prompt": "", "tools": "", "mcp_config": "",
                "custom_args": "",
                "expert_args": self.launch_expert_args,
                "tmux": self.launch_tmux,
            }
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
            "expert_args": "",
            "tmux": self.launch_tmux,
        }

    @staticmethod
    def _build_args_from_profile(profile: dict) -> List[str]:
        """Build CLI args list from a profile dict."""
        expert = profile.get("expert_args", "").strip()
        if expert:
            return expert.split()
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
        tmux_label = "[tmux]" if p.get("tmux", True) else "[direct]"
        expert = p.get("expert_args", "").strip()
        if expert:
            label = expert[:50] + ("..." if len(expert) > 50 else "")
            return f"{tmux_label} [expert] {label}"
        parts: List[str] = [tmux_label]
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
        rows.append((ROW_TMUX, 0))
        if self.prof_expert_mode:
            rows.append((ROW_EXPERT, 0))
        else:
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
        warn = curses.color_pair(CP_WARN) | curses.A_BOLD

        rows = self.prof_edit_rows
        is_new = self.prof_editing_existing is None
        mode_label = "Expert" if self.prof_expert_mode else "Structured"
        title_text = f" {'New' if is_new else 'Edit'} Profile ({mode_label}) "

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
            elif rtype == ROW_TMUX:
                display.append((f"{prefix}Launch mode:  {cb(self.launch_tmux)} tmux"
                                f"   {cb(not self.launch_tmux)} direct", a))
            elif rtype == ROW_EXPERT:
                editing = self.launch_editing == ROW_EXPERT
                cursor = "▏" if editing else ""
                display.append((f"{prefix}claude {self.launch_expert_args}{cursor}", a))
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

        box_w = min(64, w - 4)
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
        if self.prof_expert_mode:
            hints = " Tab structured · ⏎ edit/save · Esc cancel "
        else:
            hints = " Tab expert · Space toggle · ⏎ edit/save · Esc cancel "
        self._safe(sy + box_h - 2, sx + max(1, (box_w - len(hints)) // 2),
                   hints[:box_w - 3], dim)

    def _draw_footer(self, y: int, w: int):
        dim = curses.color_pair(CP_DIM) | curses.A_DIM

        # Left: status or app name
        if self.status:
            self._safe(y, 1, f" {self.status} ",
                       curses.color_pair(CP_STATUS) | curses.A_BOLD)
        else:
            self._safe(y, 1, " ccs ", dim)
            self._safe(y, 6, "? help", dim)

        # Right: [marked ·] position (pg X/Y)
        right_parts = []
        if self.marked:
            right_parts.append(f"{len(self.marked)} marked")
        if self.filtered:
            page_size = self._get_page_size()
            page = (self.cur // page_size) + 1 if page_size > 0 else 1
            pages = ((len(self.filtered) - 1) // page_size) + 1 if page_size > 0 else 1
            pos = f"{self.cur + 1}/{len(self.filtered)}"
            if pages > 1:
                pos += f" pg {page}/{pages}"
            right_parts.append(pos)
        if right_parts:
            right_text = " · ".join(right_parts)
            self._safe(y, w - len(right_text) - 2, f" {right_text} ", dim)

        # Center: mode indicator
        if self.mode not in ("normal", "help", "delete", "delete_empty", "quit", "profiles", "profile_edit"):
            mode_label = f" [{self.mode.upper()}] "
            mx = (w - len(mode_label)) // 2
            self._safe(y, mx, mode_label,
                       curses.color_pair(CP_INPUT) | curses.A_BOLD)

    # ── Tmux launch helpers ────────────────────────────────────────

    def _tmux_attach(self, tmux_name: str):
        curses.endwin()
        os.system(f"tmux attach-session -t {shlex.quote(tmux_name)}")
        self.scr.refresh()
        curses.doupdate()
        self._refresh_tmux()

    def _tmux_launch(self, s: Session, extra: List[str]):
        tmux_name = TMUX_PREFIX + s.id[:8]
        # Check if already running
        existing = self.mgr.tmux_sessions()
        if tmux_name in existing:
            self._tmux_attach(tmux_name)
            return
        # Build claude command
        cmd_parts = ["claude", "--resume", s.id] + extra
        cmd_str = " ".join(shlex.quote(p) for p in cmd_parts)
        if s.cwd and os.path.isdir(s.cwd):
            full_cmd = f"cd {shlex.quote(s.cwd)} && {cmd_str}"
        else:
            full_cmd = cmd_str
        subprocess.run(["tmux", "new-session", "-d", "-s", tmux_name,
                        "-x", "200", "-y", "50",
                        "bash", "-c", full_cmd])
        self.mgr.tmux_register(tmux_name, s.id, self.active_profile_name)
        self._tmux_attach(tmux_name)

    def _tmux_launch_new(self, name: str, extra: List[str]):
        uid = str(uuid_mod.uuid4())
        tmux_name = TMUX_PREFIX + uid[:8]
        # Tag the new session
        tags = self.mgr._load(TAGS_FILE, {})
        tags[uid] = name
        self.mgr._save(TAGS_FILE, tags)
        cmd_parts = ["claude", "--session-id", uid] + extra
        cmd_str = " ".join(shlex.quote(p) for p in cmd_parts)
        subprocess.run(["tmux", "new-session", "-d", "-s", tmux_name,
                        "-x", "200", "-y", "50",
                        "bash", "-c", cmd_str])
        self.mgr.tmux_register(tmux_name, uid, self.active_profile_name)
        self._tmux_attach(tmux_name)

    def _tmux_launch_ephemeral(self, extra: List[str]):
        uid = str(uuid_mod.uuid4())
        tmux_name = TMUX_PREFIX + uid[:8]
        with open(EPHEMERAL_FILE, "a") as f:
            f.write(uid + "\n")
        cmd_parts = ["claude", "--session-id", uid] + extra
        cmd_str = " ".join(shlex.quote(p) for p in cmd_parts)
        subprocess.run(["tmux", "new-session", "-d", "-s", tmux_name,
                        "-x", "200", "-y", "50",
                        "bash", "-c", cmd_str])
        self.mgr.tmux_register(tmux_name, uid, self.active_profile_name)
        self._tmux_attach(tmux_name)

    def _get_use_tmux(self) -> bool:
        """Check if the active profile wants tmux launch."""
        profiles = self.mgr.load_profiles()
        active = next(
            (p for p in profiles if p.get("name") == self.active_profile_name),
            None,
        )
        return active.get("tmux", True) if active else True

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
            "chdir": self._input_chdir,
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
        # ── Global keys (work in both views) ──────────────────
        if k == ord("q"):
            self.confirm_sel = 0
            self.mode = "quit"
            return None
        elif k == 27:  # Esc
            if self.view == "tmux":
                self.view = "sessions"
                return None
            if self.query:
                self.query = ""
                self._apply_filter()
            else:
                self.confirm_sel = 0
                self.mode = "quit"
                return None
        elif k in (9, curses.KEY_RIGHT) and self.view == "sessions":  # Tab / → — switch to tmux
            self.view = "tmux"
            self._refresh_tmux()
            return None
        elif k in (9, curses.KEY_LEFT) and self.view == "tmux":  # Tab / ← — switch to sessions
            self.view = "sessions"
            return None
        elif k == ord("?"):
            self.mode = "help"
            return None
        elif k == ord("P"):
            self.prof_cur = 0
            self.prof_delete_confirm = False
            self.mode = "profiles"
            return None
        elif k == ord("H"):
            idx = THEME_NAMES.index(self.active_theme) if self.active_theme in THEME_NAMES else 0
            idx = (idx + 1) % len(THEME_NAMES)
            self._apply_theme(THEME_NAMES[idx])
            self.mgr.save_theme(self.active_theme)
            self._set_status(f"Theme: {self.active_theme}")
            return None

        # ── Tmux view input ───────────────────────────────────
        if self.view == "tmux":
            return self._input_tmux(k)

        # ── Sessions view input ───────────────────────────────

        # Navigation
        if k in (curses.KEY_UP, ord("k")):
            self.cur = max(0, self.cur - 1)
        elif k in (curses.KEY_DOWN, ord("j")):
            if self.filtered:
                self.cur = min(len(self.filtered) - 1, self.cur + 1)
        elif k == curses.KEY_SR:  # Shift+Up
            self.cur = max(0, self.cur - 10)
        elif k == curses.KEY_SF:  # Shift+Down
            if self.filtered:
                self.cur = min(len(self.filtered) - 1, self.cur + 10)
        elif k == curses.KEY_PPAGE:
            self.cur = max(0, self.cur - self._get_page_size())
        elif k == curses.KEY_NPAGE:
            if self.filtered:
                self.cur = min(len(self.filtered) - 1, self.cur + self._get_page_size())
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
                use_tmux = active.get("tmux", True) if active else True

                if use_tmux:
                    if not HAS_TMUX:
                        self._set_status("tmux is not installed — install it or disable in profile")
                        return None
                    if s.cwd and not os.path.isdir(s.cwd):
                        self.chdir_pending = ("resume", s.id, s.cwd, extra)
                        self.mode = "chdir"
                        self.ibuf = str(Path.home())
                        self._set_status(f"Directory missing: {s.cwd}")
                    else:
                        self._tmux_launch(s, extra)
                        self._refresh()
                else:
                    if s.cwd and not os.path.isdir(s.cwd):
                        self.chdir_pending = ("resume", s.id, s.cwd, extra)
                        self.mode = "chdir"
                        self.ibuf = str(Path.home())
                        self._set_status(f"Directory missing: {s.cwd}")
                    else:
                        self.exit_action = ("resume", s.id, s.cwd, extra)
                        return "action"
        elif k == ord(" "):
            # Toggle mark
            if self.filtered:
                s = self.filtered[self.cur]
                if s.id in self.marked:
                    self.marked.discard(s.id)
                else:
                    self.marked.add(s.id)
                if self.cur < len(self.filtered) - 1:
                    self.cur += 1
        elif k == ord("u"):
            if self.marked:
                self.marked.clear()
                self._set_status("Cleared all marks")
        elif k == ord("p"):
            if self.marked:
                for sid in self.marked:
                    self.mgr.toggle_pin(sid)
                self._set_status(f"Toggled pin for {len(self.marked)} session(s)")
                self.marked.clear()
                self._refresh()
            elif self.filtered:
                s = self.filtered[self.cur]
                pinned = self.mgr.toggle_pin(s.id)
                icon = "★ Pinned" if pinned else "Unpinned"
                self._set_status(f"{icon}: {s.tag or s.id[:12]}")
                self._refresh()
        elif k == ord("t"):
            if self.filtered:
                s = self.filtered[self.cur]
                self.mode = "tag"
                self.ibuf = s.tag if s.tag else ""
        elif k == ord("T"):
            if self.filtered:
                s = self.filtered[self.cur]
                if s.tag:
                    self.mgr.remove_tag(s.id)
                    self._set_status(f"Removed tag from: {s.id[:12]}")
                    self._refresh()
                else:
                    self._set_status("No tag to remove")
        elif k == ord("c"):
            if self.filtered:
                s = self.filtered[self.cur]
                self.chdir_pending = ("set_cwd", s.id, s.cwd, None)
                self.mode = "chdir"
                self.ibuf = s.cwd or str(Path.home())
        elif k == ord("d"):
            if self.marked:
                self.delete_label = f"{len(self.marked)} marked sessions"
                self.confirm_sel = 0
                self.mode = "delete"
            elif self.filtered:
                s = self.filtered[self.cur]
                self.delete_label = s.tag or s.label[:40]
                self.confirm_sel = 0
                self.mode = "delete"
        elif k == ord("D"):
            empty = [s for s in self.sessions if not s.first_msg and not s.summary]
            if empty:
                self.empty_count = len(empty)
                self.confirm_sel = 0
                self.mode = "delete_empty"
            else:
                self._set_status("No empty sessions to delete")
        elif k == ord("n"):
            self.mode = "new"
            self.ibuf = ""
        elif k == ord("e"):
            use_tmux = self._get_use_tmux()
            if use_tmux:
                if not HAS_TMUX:
                    self._set_status("tmux is not installed — install it or disable in profile")
                    return None
                profiles = self.mgr.load_profiles()
                active = next(
                    (p for p in profiles if p.get("name") == self.active_profile_name),
                    None,
                )
                extra = self._build_args_from_profile(active) if active else []
                self._tmux_launch_ephemeral(extra)
                self._refresh()
            else:
                self.exit_action = ("tmp",)
                return "action"
        elif k == ord("/"):
            self.mode = "search"
        elif k == ord("R"):
            # Quick resume most recent session
            if self.sessions:
                most_recent = max(self.sessions, key=lambda s: s.mtime)
                profiles = self.mgr.load_profiles()
                active = next(
                    (p for p in profiles if p.get("name") == self.active_profile_name),
                    None,
                )
                extra = self._build_args_from_profile(active) if active else []
                use_tmux = active.get("tmux", True) if active else True

                if use_tmux:
                    if not HAS_TMUX:
                        self._set_status("tmux is not installed — install it or disable in profile")
                        return None
                    if most_recent.cwd and not os.path.isdir(most_recent.cwd):
                        self.chdir_pending = ("resume", most_recent.id, most_recent.cwd, extra)
                        self.mode = "chdir"
                        self.ibuf = str(Path.home())
                        self._set_status(f"Directory missing: {most_recent.cwd}")
                    else:
                        self._tmux_launch(most_recent, extra)
                        self._refresh()
                else:
                    if most_recent.cwd and not os.path.isdir(most_recent.cwd):
                        self.chdir_pending = ("resume", most_recent.id, most_recent.cwd, extra)
                        self.mode = "chdir"
                        self.ibuf = str(Path.home())
                        self._set_status(f"Directory missing: {most_recent.cwd}")
                    else:
                        self.exit_action = ("resume", most_recent.id, most_recent.cwd, extra)
                        return "action"
            else:
                self._set_status("No sessions to resume")
        elif k == ord("s"):
            modes = ["date", "name", "project"]
            idx = modes.index(self.sort_mode) if self.sort_mode in modes else 0
            self.sort_mode = modes[(idx + 1) % len(modes)]
            self._refresh()
            labels = {"date": "Date", "name": "Name", "project": "Project"}
            self._set_status(f"Sort: {labels[self.sort_mode]}")
        elif k in (ord("r"), curses.KEY_F5):
            self._refresh(force=True)
            self._set_status("Refreshed session list")

        return None

    def _input_tmux(self, k: int) -> Optional[str]:
        """Handle input when in tmux view."""
        if k in (curses.KEY_UP, ord("k")):
            self.tmux_cur = max(0, self.tmux_cur - 1)
        elif k in (curses.KEY_DOWN, ord("j")):
            if self.tmux_list:
                self.tmux_cur = min(len(self.tmux_list) - 1, self.tmux_cur + 1)
        elif k in (curses.KEY_HOME, ord("g")):
            self.tmux_cur = 0
        elif k == ord("G"):
            if self.tmux_list:
                self.tmux_cur = len(self.tmux_list) - 1
        elif k in (ord("\n"), curses.KEY_ENTER, 10, 13):
            if self.tmux_list:
                name, _ = self.tmux_list[self.tmux_cur]
                self._tmux_attach(name)
        elif k == ord("x"):
            if self.tmux_list:
                name, _ = self.tmux_list[self.tmux_cur]
                subprocess.run(["tmux", "kill-session", "-t", name],
                               capture_output=True)
                self.mgr.tmux_unregister(name)
                self._set_status(f"Killed tmux session: {name}")
                self._refresh_tmux()
        elif k in (ord("r"), curses.KEY_F5):
            self._refresh_tmux()
            self._set_status("Refreshed tmux sessions")
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
                new_tag = self.ibuf.strip()
                if not self.mgr.is_tag_unique(new_tag, s.id):
                    self._set_status(f"Tag '{new_tag}' already used by another session")
                    return None
                self.mgr.set_tag(s.id, new_tag)
                self._set_status(f"Tagged: [{new_tag}]")
                self._refresh()
            self.mode = "normal"
        elif k in (curses.KEY_BACKSPACE, 127, 8):
            self.ibuf = self.ibuf[:-1]
        elif 32 <= k <= 126:
            self.ibuf += chr(k)
        return None

    def _input_delete(self, k: int) -> Optional[str]:
        if k == ord("y") or (k in (ord("\n"), curses.KEY_ENTER, 10, 13) and self.confirm_sel == 1):
            if self.marked:
                count = 0
                for s in list(self.sessions):
                    if s.id in self.marked:
                        self.mgr.delete(s)
                        count += 1
                self.marked.clear()
                self._set_status(f"Deleted {count} session{'s' if count != 1 else ''}")
                self._refresh()
            elif self.filtered:
                s = self.filtered[self.cur]
                self.mgr.delete(s)
                self._set_status(f"Deleted: {s.tag or s.id[:12]}")
                self._refresh()
            self.mode = "normal"
        elif k in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("h"), ord("l")):
            self.confirm_sel = 1 - self.confirm_sel
        elif k == ord("n") or k == 27 or (k in (ord("\n"), curses.KEY_ENTER, 10, 13) and self.confirm_sel == 0):
            self.mode = "normal"
        return None

    def _input_delete_empty(self, k: int) -> Optional[str]:
        if k == ord("y") or (k in (ord("\n"), curses.KEY_ENTER, 10, 13) and self.confirm_sel == 1):
            empty = [s for s in self.sessions if not s.first_msg and not s.summary]
            count = 0
            for s in empty:
                self.mgr.delete(s)
                count += 1
            self._set_status(f"Deleted {count} empty session{'s' if count != 1 else ''}")
            self._refresh()
            self.mode = "normal"
        elif k in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("h"), ord("l")):
            self.confirm_sel = 1 - self.confirm_sel
        elif k == ord("n") or k == 27 or (k in (ord("\n"), curses.KEY_ENTER, 10, 13) and self.confirm_sel == 0):
            self.mode = "normal"
        return None

    def _input_new(self, k: int) -> Optional[str]:
        if k == 27:  # Esc
            self.mode = "normal"
        elif k in (ord("\n"), curses.KEY_ENTER, 10, 13):
            if self.ibuf.strip():
                name = self.ibuf.strip()
                use_tmux = self._get_use_tmux()
                if use_tmux:
                    if not HAS_TMUX:
                        self._set_status("tmux is not installed — install it or disable in profile")
                        self.mode = "normal"
                        return None
                    profiles = self.mgr.load_profiles()
                    active = next(
                        (p for p in profiles if p.get("name") == self.active_profile_name),
                        None,
                    )
                    extra = self._build_args_from_profile(active) if active else []
                    self.mode = "normal"
                    self._tmux_launch_new(name, extra)
                    self._refresh()
                else:
                    self.exit_action = ("new", name)
                    return "action"
            self.mode = "normal"
        elif k in (curses.KEY_BACKSPACE, 127, 8):
            self.ibuf = self.ibuf[:-1]
        elif 32 <= k <= 126:
            self.ibuf += chr(k)
        return None

    def _input_chdir(self, k: int) -> Optional[str]:
        if k == 27:  # Esc
            self.chdir_pending = None
            self.mode = "normal"
        elif k in (ord("\n"), curses.KEY_ENTER, 10, 13):
            path = self.ibuf.strip()
            if not path:
                self.chdir_pending = None
                self.mode = "normal"
                return None
            expanded = os.path.expanduser(path)
            if not os.path.isdir(expanded):
                self._set_status(f"Not a valid directory: {path}")
                return None
            action_type = self.chdir_pending[0]
            sid = self.chdir_pending[1]
            if action_type == "resume":
                extra = self.chdir_pending[3]
                self.chdir_pending = None
                use_tmux = self._get_use_tmux()
                if use_tmux and HAS_TMUX:
                    # Create a temporary Session to pass to _tmux_launch
                    tmp_s = Session(id=sid, project_raw="", project_display="",
                                   cwd=expanded, summary="", first_msg="",
                                   first_msg_long="", tag="", pinned=False,
                                   mtime=0.0)
                    self._tmux_launch(tmp_s, extra)
                    self._refresh()
                    self.mode = "normal"
                else:
                    self.exit_action = ("resume", sid, expanded, extra)
                    return "action"
            elif action_type == "set_cwd":
                self.mgr.set_cwd(sid, expanded)
                self._set_status(f"CWD set to: {expanded}")
                self.chdir_pending = None
                self._refresh()
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
            if (k == ord("y") or (k in (ord("\n"), curses.KEY_ENTER, 10, 13)
                                  and self.confirm_sel == 1)) and profiles:
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
            elif k in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("h"), ord("l")):
                self.confirm_sel = 1 - self.confirm_sel
            else:
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
                    self.confirm_sel = 0
                    self.prof_delete_confirm = True

        return None

    def _prof_open_editor(self, profile: Optional[dict]):
        """Open the profile editor, optionally pre-filled from an existing profile."""
        self.launch_editing = None

        if profile:
            # Edit existing — detect expert mode
            self.prof_editing_existing = profile.get("name", "")
            self.prof_edit_name = profile.get("name", "")
            self.prof_expert_mode = bool(profile.get("expert_args", "").strip())
            self._launch_apply_profile(profile)
        else:
            # New - blank slate
            self.prof_editing_existing = None
            self.prof_edit_name = ""
            self.prof_expert_mode = False
            self.launch_model_idx = 0
            self.launch_perm_idx = 0
            self.launch_toggles = [False] * len(TOGGLE_FLAGS)
            self.launch_sysprompt = ""
            self.launch_tools = ""
            self.launch_mcp = ""
            self.launch_custom = ""
            self.launch_expert_args = ""
            self.launch_tmux = True
            # Start with name field editing immediately
            self.launch_editing = ROW_PROF_NAME

        self.prof_edit_rows = self._build_profile_edit_rows()
        self.prof_edit_cur = 0
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
                elif self.launch_editing == ROW_EXPERT:
                    self.launch_expert_args = self.launch_expert_args[:-1]
                else:
                    self._launch_edit_backspace()
            elif 32 <= k <= 126:
                if self.launch_editing == ROW_PROF_NAME:
                    self.prof_edit_name += chr(k)
                elif self.launch_editing == ROW_EXPERT:
                    self.launch_expert_args += chr(k)
                else:
                    self._launch_edit_char(chr(k))
            return None

        # ── Normal navigation ─────────────────────────────────────
        if k == 27:  # Esc → back to profiles list
            self.mode = "profiles"

        elif k == 9:  # Tab → toggle expert/structured mode
            self.launch_editing = None
            self.prof_expert_mode = not self.prof_expert_mode
            self.prof_edit_rows = self._build_profile_edit_rows()
            if self.prof_edit_cur >= len(self.prof_edit_rows):
                self.prof_edit_cur = len(self.prof_edit_rows) - 1

        elif k in (curses.KEY_UP, ord("k")):
            self.prof_edit_cur = max(0, self.prof_edit_cur - 1)
        elif k in (curses.KEY_DOWN, ord("j")):
            self.prof_edit_cur = min(len(rows) - 1, self.prof_edit_cur + 1)

        elif k == ord(" "):
            self._prof_edit_toggle_current()

        elif k in (ord("\n"), curses.KEY_ENTER, 10, 13):
            if cur_type == ROW_PROF_SAVE:
                self._prof_do_save()
            elif cur_type in (ROW_PROF_NAME, ROW_EXPERT, ROW_SYSPROMPT, ROW_TOOLS, ROW_MCP, ROW_CUSTOM):
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
        elif rtype == ROW_TMUX:
            self.launch_tmux = not self.launch_tmux
        elif rtype in (ROW_PROF_NAME, ROW_EXPERT):
            self.launch_editing = rtype
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
        if k == ord("y") or (k in (ord("\n"), curses.KEY_ENTER, 10, 13) and self.confirm_sel == 1):
            return "quit"
        elif k in (curses.KEY_LEFT, curses.KEY_RIGHT, ord("h"), ord("l")):
            self.confirm_sel = 1 - self.confirm_sel
        elif k == ord("n") or k == 27 or (k in (ord("\n"), curses.KEY_ENTER, 10, 13) and self.confirm_sel == 0):
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
  ccs tag rename <oldtag> <newtag>       Rename a tag
  ccs untag <id|tag>                     Remove tag from session
  ccs chdir <id|tag> <path>              Set session working directory
  ccs delete <id|tag>                    Delete a session
  ccs delete --empty                     Delete all empty sessions
  ccs search <query>                     Search sessions by text
  ccs export <id|tag>                    Export session as markdown
  ccs profile list                       List profiles
  ccs profile set <name>                 Set active profile
  ccs profile new <name> [flags]         Create profile from CLI flags
  ccs profile delete <name>              Delete a profile
  ccs theme list                         List themes
  ccs theme set <name>                   Set theme
  ccs tmux list                          List running tmux sessions
  ccs tmux attach <name>                 Attach to tmux session
  ccs tmux kill <name>                   Kill a tmux session
  ccs tmux kill --all                    Kill all tmux sessions
  ccs help                               Show this help

\033[1mProfile creation flags:\033[0m
  --model <model>                        Model name
  --permission-mode <mode>               Permission mode
  --verbose                              Verbose flag
  --dangerously-skip-permissions         Skip permissions flag
  --print                                Print flag
  --continue                             Continue flag
  --no-session-persistence               No session persistence
  --no-tmux                              Disable tmux (use direct launch)
  --system-prompt <prompt>               System prompt
  --tools <tools>                        Tools
  --mcp-config <path>                    MCP config path

\033[1mTUI Keybindings:\033[0m
  \033[36m↑/↓\033[0m or \033[36mj/k\033[0m       Navigate sessions
  \033[36mEnter\033[0m             Resume with active profile
  \033[36mR\033[0m                 Quick resume most recent session
  \033[36mP\033[0m                 Profile picker / manager
  \033[36mH\033[0m                 Cycle theme
  \033[36ms\033[0m                 Cycle sort (date/name/project)
  \033[36mp\033[0m                 Toggle pin (bulk if marked)
  \033[36mt\033[0m / \033[36mT\033[0m             Set/rename tag / remove tag
  \033[36mc\033[0m                 Change session working directory
  \033[36md\033[0m / \033[36mD\033[0m             Delete session / delete empties
  \033[36mSpace\033[0m             Mark/unmark session for bulk ops
  \033[36mu\033[0m                 Unmark all
  \033[36mn\033[0m                 New named session
  \033[36me\033[0m                 Ephemeral session
  \033[36m/\033[0m                 Search / filter
  \033[36m←/→\033[0m or \033[36mTab\033[0m       Switch Sessions / Tmux view
  \033[36mx\033[0m                 Kill tmux session (in Tmux view)
  \033[36mr\033[0m                 Refresh
  \033[36mq\033[0m                 Quit
  Mouse: click select, double-click resume, scroll navigate""")


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
            home = str(Path.home())
            print(f"\033[1;33m◆ Warning:\033[0m Directory no longer exists: \033[33m{s.cwd}\033[0m")
            print(f"  Falling back to: \033[36m{home}\033[0m")
            os.chdir(home)
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
    if not mgr.is_tag_unique(tag, s.id):
        print(f"\033[31mTag '{tag}' is already used by another session\033[0m")
        sys.exit(1)
    mgr.set_tag(s.id, tag)
    print(f"Tagged [{tag}]: {s.id[:12]}")


def cmd_tag_rename(mgr: SessionManager, old_tag: str, new_tag: str):
    tags = mgr._load(TAGS_FILE, {})
    matches = [sid for sid, t in tags.items() if t == old_tag]
    if not matches:
        print(f"\033[31mNo session with tag '{old_tag}'\033[0m")
        sys.exit(1)
    if len(matches) > 1:
        print(f"\033[31mAmbiguous: {len(matches)} sessions have tag '{old_tag}'\033[0m")
        sys.exit(1)
    sid = matches[0]
    if not mgr.is_tag_unique(new_tag, sid):
        print(f"\033[31mTag '{new_tag}' is already used by another session\033[0m")
        sys.exit(1)
    mgr.set_tag(sid, new_tag)
    print(f"Renamed tag [{old_tag}] → [{new_tag}]: {sid[:12]}")


def cmd_untag(mgr: SessionManager, query: str):
    s = _find_session(mgr, query)
    if s.tag:
        mgr.remove_tag(s.id)
        print(f"Removed tag from: {s.id[:12]}")
    else:
        print(f"No tag on: {s.id[:12]}")


def cmd_chdir(mgr: SessionManager, query: str, path: str):
    s = _find_session(mgr, query)
    expanded = os.path.expanduser(path)
    if not os.path.isdir(expanded):
        print(f"\033[31mDirectory does not exist: {expanded}\033[0m")
        sys.exit(1)
    mgr.set_cwd(s.id, expanded)
    print(f"CWD set to [{expanded}]: {s.tag or s.id[:12]}")


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
        "mcp_config": "", "custom_args": "", "tmux": True,
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
        elif a == "--no-tmux":
            profile["tmux"] = False
            i += 1
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


def cmd_export(mgr: SessionManager, query: str):
    s = _find_session(mgr, query)
    ts = datetime.datetime.fromtimestamp(s.mtime).strftime("%Y-%m-%d %H:%M")
    print(f"# Session: {s.label}")
    print(f"- **ID:** `{s.id}`")
    if s.tag:
        print(f"- **Tag:** {s.tag}")
    print(f"- **Project:** {s.project_display}")
    print(f"- **CWD:** {s.cwd}")
    print(f"- **Modified:** {ts}")
    if s.pinned:
        print("- **Pinned:** yes")
    print(f"- **Messages:** {s.msg_count}")
    print()
    try:
        with open(s.path, "r", errors="replace") as f:
            for ln in f:
                try:
                    d = json.loads(ln)
                except Exception:
                    continue
                msg_type = d.get("type")
                if msg_type == "user":
                    txt = SessionManager._extract_text(d.get("message", {}))
                    if txt:
                        print(f"## User\n\n{txt}\n")
                elif msg_type == "assistant":
                    txt = SessionManager._extract_text(d.get("message", {}))
                    if txt:
                        print(f"## Assistant\n\n{txt}\n")
    except Exception as e:
        print(f"\n*Error reading session file: {e}*")


def cmd_tmux_list(mgr: SessionManager):
    sessions = mgr.tmux_sessions()
    if not sessions:
        print("No active ccs tmux sessions.")
        return
    for name, info in sorted(sessions.items(), key=lambda x: x[1].get("launched", "")):
        sid = info.get("session_id", "")[:12]
        profile = info.get("profile", "")
        launched = info.get("launched", "")[:16]
        print(f"  {name:<20s}  {sid:<14s}  {profile:<16s}  {launched}")


def cmd_tmux_attach(mgr: SessionManager, name: str):
    if not HAS_TMUX:
        print("\033[31mtmux is not installed.\033[0m")
        sys.exit(1)
    sessions = mgr.tmux_sessions()
    if name not in sessions:
        print(f"\033[31mNo ccs tmux session named '{name}'.\033[0m")
        sys.exit(1)
    os.execvp("tmux", ["tmux", "attach-session", "-t", name])


def cmd_tmux_kill(mgr: SessionManager, name: str):
    if not HAS_TMUX:
        print("\033[31mtmux is not installed.\033[0m")
        sys.exit(1)
    sessions = mgr.tmux_sessions()
    if name not in sessions:
        print(f"\033[31mNo ccs tmux session named '{name}'.\033[0m")
        sys.exit(1)
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)
    mgr.tmux_unregister(name)
    print(f"Killed tmux session: {name}")


def cmd_tmux_kill_all(mgr: SessionManager):
    if not HAS_TMUX:
        print("\033[31mtmux is not installed.\033[0m")
        sys.exit(1)
    sessions = mgr.tmux_sessions()
    if not sessions:
        print("No active ccs tmux sessions to kill.")
        return
    count = 0
    for name in list(sessions.keys()):
        subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)
        mgr.tmux_unregister(name)
        count += 1
    print(f"Killed {count} tmux session{'s' if count != 1 else ''}.")


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
                    home = str(Path.home())
                    print(f"\033[1;33m◆ Warning:\033[0m Session directory no longer exists: \033[33m{cwd}\033[0m")
                    print(f"  Falling back to: \033[36m{home}\033[0m")
                    os.chdir(home)
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
        if len(args) >= 2 and args[1] == "rename":
            if len(args) < 4:
                print("\033[31mUsage: ccs tag rename <oldtag> <newtag>\033[0m")
                sys.exit(1)
            cmd_tag_rename(mgr, args[2], args[3])
        elif len(args) < 3:
            print("\033[31mUsage: ccs tag <id|tag> <newtag>  |  ccs tag rename <old> <new>\033[0m")
            sys.exit(1)
        else:
            cmd_tag(mgr, args[1], args[2])

    elif verb == "untag":
        if len(args) < 2:
            print("\033[31mUsage: ccs untag <id|tag>\033[0m")
            sys.exit(1)
        cmd_untag(mgr, args[1])

    elif verb == "chdir":
        if len(args) < 3:
            print("\033[31mUsage: ccs chdir <id|tag> <path>\033[0m")
            sys.exit(1)
        cmd_chdir(mgr, args[1], args[2])

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

    elif verb == "export":
        if len(args) < 2:
            print("\033[31mUsage: ccs export <id|tag>\033[0m")
            sys.exit(1)
        cmd_export(mgr, args[1])

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

    elif verb == "tmux":
        if not HAS_TMUX:
            print("\033[31mtmux is not installed.\033[0m")
            sys.exit(1)
        if len(args) < 2:
            print("\033[31mUsage: ccs tmux list|attach|kill\033[0m")
            sys.exit(1)
        sub = args[1]
        if sub == "list":
            cmd_tmux_list(mgr)
        elif sub == "attach":
            if len(args) < 3:
                print("\033[31mUsage: ccs tmux attach <name>\033[0m")
                sys.exit(1)
            cmd_tmux_attach(mgr, args[2])
        elif sub == "kill":
            if len(args) >= 3 and args[2] == "--all":
                cmd_tmux_kill_all(mgr)
            elif len(args) >= 3:
                cmd_tmux_kill(mgr, args[2])
            else:
                print("\033[31mUsage: ccs tmux kill <name> | ccs tmux kill --all\033[0m")
                sys.exit(1)
        else:
            print(f"\033[31mUnknown tmux command: {sub}\033[0m")
            sys.exit(1)

    else:
        print(f"\033[31mUnknown command: {verb}\033[0m")
        print("Run 'ccs help' for usage information.")
        sys.exit(1)


if __name__ == "__main__":
    main()
