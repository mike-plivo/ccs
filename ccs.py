#!/usr/bin/env python3
"""
ccs — Claude Code Session Manager
A terminal UI and CLI for browsing, managing, and resuming Claude Code sessions.

Usage:
    ccs                                    Interactive TUI
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
    ccs profile list|info|set|new|delete    Manage profiles
    ccs theme list|set                     Manage themes
    ccs tmux list                          List running tmux sessions
    ccs tmux attach <name>                 Attach to tmux session
    ccs tmux kill <name>                   Kill a tmux session
    ccs tmux kill --all                    Kill all tmux sessions
    ccs help                               Show help
"""

import json
import os
import glob
import datetime
import getpass
import re
import subprocess
import sys
import shlex
import shutil
import time
import uuid as uuid_mod
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Container, ScrollableContainer, Horizontal, Vertical
    from textual.screen import ModalScreen
    from textual.theme import Theme
    from textual.widget import Widget
    from textual.widgets import Static, OptionList, RichLog, Input, TextArea, Button, Label
    from textual.widgets.option_list import Option
    from textual.reactive import reactive
    from textual import work, on
    from rich.text import Text
    from rich.style import Style
except ImportError as e:
    print(f"\033[31mError: Missing required dependency: {e}\033[0m")
    print("Install with: pip install textual rich")
    sys.exit(1)

VERSION = "1.2.0"

# ── Paths ─────────────────────────────────────────────────────────────

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
CCS_DIR = Path.home() / ".config" / "ccs"
META_FILE = CCS_DIR / "sessions.json"
PROFILES_FILE = CCS_DIR / "ccs_profiles.json"
ACTIVE_PROFILE_FILE = CCS_DIR / "ccs_active_profile.txt"
THEME_FILE = CCS_DIR / "ccs_theme.txt"
CACHE_FILE = CCS_DIR / "session_cache.json"
HAS_TMUX = shutil.which("tmux") is not None
HAS_GIT = shutil.which("git") is not None
TMUX_PREFIX = "ccs-"
TMUX_IDLE_SECS = 30   # seconds of no output before marking session idle
TMUX_POLL_INTERVAL = 5  # seconds between activity polls
TMUX_CAPTURE_INTERVAL = 0.3  # seconds between pane capture polls
TMUX_CAPTURE_LINES = 20      # number of lines to capture from tmux pane
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][0-9A-Z]')

# ── Themes ───────────────────────────────────────────────────────────

THEME_NAMES = ["dark", "blue", "red", "green", "light", "purple", "yellow", "white", "black"]
DEFAULT_THEME = "dark"

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
    summary: str
    first_msg: str
    first_msg_long: str
    last_msg: str
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
        return self.summary or self.last_msg or self.first_msg or "(empty session)"

    def get_sort_key(self, sort_mode: str = "date") -> Tuple:
        tier = 0 if self.pinned else 1
        if sort_mode == "name":
            return (tier, self.label.lower(), -self.mtime)
        elif sort_mode == "project":
            return (tier, self.project_display.lower(), -self.mtime)
        elif sort_mode == "messages":
            return (tier, -self.msg_count, -self.mtime)
        elif sort_mode == "tag":
            return (tier, 0 if self.tag else 1, (self.tag or "").lower(), -self.mtime)
        return (tier, -self.mtime)


# ── Session Manager ───────────────────────────────────────────────────


class SessionManager:
    def __init__(self):
        self.user = getpass.getuser()
        self._scan_cache = None
        self._ensure()

    def _ensure(self):
        CCS_DIR.mkdir(parents=True, exist_ok=True)
        self._migrate_old_meta()
        if not META_FILE.exists():
            META_FILE.write_text("{}")

    def _migrate_old_meta(self):
        """One-time migration from old multi-file metadata to sessions.json."""
        old_tags = CCS_DIR / "session_tags.json"
        old_pins = CCS_DIR / "session_pins.json"
        old_cwds = CCS_DIR / "session_cwds.json"
        old_ephemeral = CCS_DIR / "ephemeral_sessions.txt"
        old_tmux = CCS_DIR / "tmux_sessions.json"
        if not any(f.exists() for f in [old_tags, old_pins, old_cwds, old_ephemeral]):
            # Remove tmux file if it exists (transient data)
            if old_tmux.exists():
                old_tmux.unlink()
            return
        meta = self._load_meta()
        # Merge tags
        tags = self._load(old_tags, {})
        for sid, tag in tags.items():
            if tag:
                meta.setdefault(sid, {})["tag"] = tag
        # Merge pins
        pins = self._load(old_pins, [])
        for sid in pins:
            meta.setdefault(sid, {})["pinned"] = True
        # Merge ephemeral
        try:
            text = old_ephemeral.read_text().strip()
            for line in text.split("\n"):
                sid = line.strip()
                if sid:
                    meta.setdefault(sid, {})["ephemeral"] = True
        except Exception:
            pass
        self._save_meta(meta)
        # Remove old files
        for f in [old_tags, old_pins, old_cwds, old_ephemeral, old_tmux]:
            try:
                if f.exists():
                    f.unlink()
            except Exception:
                pass

    def _load(self, p, default):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            return default

    def _save(self, p, data):
        with open(p, "w") as f:
            json.dump(data, f, indent=2)

    def _load_meta(self) -> dict:
        return self._load(META_FILE, {})

    def _save_meta(self, data: dict):
        self._save(META_FILE, data)

    def _get_meta(self, sid: str) -> dict:
        return self._load_meta().get(sid, {})

    def _set_meta(self, sid: str, **kwargs):
        meta = self._load_meta()
        entry = meta.get(sid, {})
        for k, v in kwargs.items():
            if v in (None, "", False):
                entry.pop(k, None)
            else:
                entry[k] = v
        if entry:
            meta[sid] = entry
        else:
            meta.pop(sid, None)
        self._save_meta(meta)

    def _delete_meta(self, sid: str):
        meta = self._load_meta()
        if sid in meta:
            meta.pop(sid)
            self._save_meta(meta)

    def _load_project_paths(self) -> dict:
        """Load projectPath from all sessions-index.json files.

        Returns dict mapping session ID to projectPath.
        Uses per-entry projectPath first, falls back to originalPath
        for sessions not listed in entries.
        """
        home = str(Path.home())
        result = {}
        for idx_path in glob.glob(str(PROJECTS_DIR / "*" / "sessions-index.json")):
            try:
                with open(idx_path) as f:
                    data = json.load(f)
                orig = data.get("originalPath", "")
                for entry in data.get("entries", []):
                    sid = entry.get("sessionId", "")
                    pp = entry.get("projectPath", "") or orig
                    if sid and pp:
                        if pp.startswith(home):
                            pp = "~" + pp[len(home):]
                        result[sid] = pp
                # For .jsonl files in this project dir not listed in entries
                if orig:
                    proj_dir = os.path.dirname(idx_path)
                    entry_sids = {e.get("sessionId") for e in data.get("entries", [])}
                    for fname in os.listdir(proj_dir):
                        if fname.endswith(".jsonl"):
                            sid = fname[:-6]
                            if sid not in entry_sids and sid not in result:
                                pp = orig
                                if pp.startswith(home):
                                    pp = "~" + pp[len(home):]
                                result[sid] = pp
            except Exception:
                pass
        return result

    @staticmethod
    def _decode_proj_fallback(raw: str, user: str) -> str:
        """Fallback project display when sessions-index.json is unavailable."""
        p = raw
        pfx = "-Users-" + user
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
        meta = self._load_meta()
        if force:
            cache = {}
        elif self._scan_cache is not None:
            cache = self._scan_cache
        else:
            cache = self._load(CACHE_FILE, {})
        out: List[Session] = []
        seen_sids: set = set()
        cache_dirty = False
        empty_sids: List[str] = []
        proj_paths = self._load_project_paths()
        pattern = str(PROJECTS_DIR / "*" / "*.jsonl")

        for jp in glob.glob(pattern):
            sid = os.path.basename(jp).replace(".jsonl", "")
            seen_sids.add(sid)
            praw = os.path.basename(os.path.dirname(jp))
            pdisp = proj_paths.get(sid) or self._decode_proj_fallback(praw, self.user)
            sm = meta.get(sid, {})
            tag = sm.get("tag", "")
            pinned = sm.get("pinned", False)
            file_mtime = os.path.getmtime(jp)

            # Check cache
            cached = cache.get(sid)
            if cached and cached.get("mtime") == file_mtime:
                summary = cached.get("summary", "")
                fm = cached.get("first_msg", "")
                fm_long = cached.get("first_msg_long", "")
                lm = cached.get("last_msg", "")
                sums = cached.get("summaries", [])
                msg_count = cached.get("msg_count", 0)
                praw = cached.get("project_raw", praw)
                pdisp = cached.get("project_display", pdisp)
            else:
                summary, fm, fm_long, lm = "", "", "", ""
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
                                if msg_type == "user":
                                    txt = self._extract_text(d.get("message", {}))
                                    if txt:
                                        clean = txt[:120].replace("\n", " ").replace("\t", " ")
                                        if not fm:
                                            fm = clean
                                            fm_long = txt[:800]
                                        lm = clean
                except Exception:
                    pass
                cache[sid] = {
                    "mtime": file_mtime,
                    "summary": summary,
                    "first_msg": fm,
                    "first_msg_long": fm_long,
                    "last_msg": lm,
                    "msg_count": msg_count,
                    "summaries": sums,
                    "project_raw": praw,
                    "project_display": pdisp,
                }
                cache_dirty = True

            # Auto-delete sessions with no user/assistant messages
            if msg_count == 0:
                try:
                    os.remove(jp)
                except OSError:
                    pass
                empty_sids.append(sid)
                seen_sids.discard(sid)
                cache.pop(sid, None)
                cache_dirty = True
                continue

            out.append(Session(
                id=sid, project_raw=praw, project_display=pdisp,
                summary=summary, first_msg=fm,
                first_msg_long=fm_long, last_msg=lm,
                tag=tag, pinned=pinned,
                mtime=file_mtime, summaries=sums, path=jp,
                msg_count=msg_count,
            ))

        # Batch-delete metadata for empty sessions
        if empty_sids:
            for sid in empty_sids:
                meta.pop(sid, None)

        # Prune metadata entries for sessions no longer on disk
        orphaned = [sid for sid in meta if sid not in seen_sids]
        for sid in orphaned:
            meta.pop(sid)
        if empty_sids or orphaned:
            self._save_meta(meta)

        # Prune cache entries for sessions no longer on disk
        prev_len = len(cache)
        cache = {k: v for k, v in cache.items() if k in seen_sids}
        if len(cache) != prev_len:
            cache_dirty = True
        self._scan_cache = cache
        if cache_dirty:
            try:
                self._save(CACHE_FILE, cache)
            except Exception:
                pass

        out.sort(key=lambda s: s.get_sort_key(sort_mode))
        return out

    def toggle_pin(self, sid: str) -> bool:
        current = self._get_meta(sid).get("pinned", False)
        self._set_meta(sid, pinned=not current)
        return not current

    def set_tag(self, sid: str, tag: str):
        self._set_meta(sid, tag=tag[:10] if tag else "")

    def remove_tag(self, sid: str):
        self.set_tag(sid, "")

    def delete(self, s: Session):
        if os.path.exists(s.path):
            os.remove(s.path)
        self._delete_meta(s.id)

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

    # ── Tmux session discovery ─────────────────────────────────

    def tmux_alive_sids(self) -> set:
        """Return set of session IDs with live ccs tmux sessions."""
        if not HAS_TMUX:
            return set()
        try:
            r = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True, text=True, timeout=2,
            )
            if r.returncode != 0:
                return set()
            sids = set()
            for name in r.stdout.strip().split("\n"):
                name = name.strip()
                if name.startswith(TMUX_PREFIX):
                    sids.add(name[len(TMUX_PREFIX):])
            return sids
        except Exception:
            return set()

    def purge_ephemeral(self):
        meta = self._load_meta()
        ephemeral_sids = [sid for sid, m in meta.items() if m.get("ephemeral")]
        if not ephemeral_sids:
            return
        for uid in ephemeral_sids:
            for f in glob.glob(str(PROJECTS_DIR / "*" / f"{uid}.jsonl")):
                try:
                    os.remove(f)
                except Exception:
                    pass
            meta.pop(uid, None)
        self._save_meta(meta)


# ── Standalone utility functions ─────────────────────────────────────


def strip_ansi(text: str) -> str:
    """Strip ANSI escape sequences from text."""
    return _ANSI_RE.sub('', text)


def word_wrap(text: str, width: int) -> List[str]:
    """Word-wrap text to the given width."""
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


def build_args_from_profile(profile: dict) -> List[str]:
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


def profile_summary(p: dict) -> str:
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


def build_profile_edit_rows(expert_mode: bool) -> List[Tuple[str, int]]:
    """Build the list of (row_type, index) tuples for profile editor."""
    rows: List[Tuple[str, int]] = []
    rows.append((ROW_PROF_NAME, 0))
    rows.append((ROW_TMUX, 0))
    if expert_mode:
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
# ── Textual Themes ────────────────────────────────────────────────────

CCS_THEMES = {
    "ccs-dark": Theme(
        name="ccs-dark",
        primary="#00cccc",
        secondary="#cc00cc",
        warning="#cc0000",
        success="#00cc00",
        accent="#00cccc",
        dark=True,
        variables={
            "header-color": "#00ffff",
            "border-color": "#00cccc",
            "pin-color": "#ffff00",
            "tag-color": "#00ff00",
            "project-color": "#cc00cc",
            "selected-bg": "#003366",
            "selected-fg": "#ffffff",
            "dim-color": "#888888",
            "age-today": "#00ff00",
            "age-week": "#ffff00",
            "age-old": "#666666",
            "status-color": "#00ff00",
            "badge-bg": "#00aa00",
            "badge-fg": "#000000",
            "warn-color": "#ff4444",
            "accent-color": "#00cccc",
            "input-color": "#ffff00",
            "tmux-thinking": "#00cc00",
            "tmux-input": "#ffff00",
            "tmux-approval": "#ff4444",
            "tmux-idle": "#666666",
        },
    ),
    "ccs-blue": Theme(
        name="ccs-blue",
        primary="#4488ff",
        secondary="#00cccc",
        warning="#cc0000",
        success="#00cc00",
        accent="#4488ff",
        dark=True,
        variables={
            "header-color": "#4488ff",
            "border-color": "#4488ff",
            "pin-color": "#ffff00",
            "tag-color": "#00cccc",
            "project-color": "#00cccc",
            "selected-bg": "#003366",
            "selected-fg": "#ffffff",
            "dim-color": "#6688aa",
            "age-today": "#00ff00",
            "age-week": "#00cccc",
            "age-old": "#666666",
            "status-color": "#00cccc",
            "badge-bg": "#4488ff",
            "badge-fg": "#ffffff",
            "warn-color": "#ff4444",
            "accent-color": "#4488ff",
            "input-color": "#00cccc",
            "tmux-thinking": "#00cccc",
            "tmux-input": "#00cccc",
            "tmux-approval": "#ff4444",
            "tmux-idle": "#555577",
        },
    ),
    "ccs-red": Theme(
        name="ccs-red",
        primary="#cc4444",
        secondary="#ffff00",
        warning="#ff0000",
        success="#00cc00",
        accent="#cc4444",
        dark=True,
        variables={
            "header-color": "#ff4444",
            "border-color": "#cc4444",
            "pin-color": "#ffff00",
            "tag-color": "#00ff00",
            "project-color": "#ffff00",
            "selected-bg": "#660000",
            "selected-fg": "#ffffff",
            "dim-color": "#aa8888",
            "age-today": "#00ff00",
            "age-week": "#ffff00",
            "age-old": "#666666",
            "status-color": "#ff4444",
            "badge-bg": "#cc0000",
            "badge-fg": "#ffffff",
            "warn-color": "#ff4444",
            "accent-color": "#cc4444",
            "input-color": "#ffff00",
            "tmux-thinking": "#00cc00",
            "tmux-input": "#ffff00",
            "tmux-approval": "#ff4444",
            "tmux-idle": "#775555",
        },
    ),
    "ccs-green": Theme(
        name="ccs-green",
        primary="#00cc00",
        secondary="#ffff00",
        warning="#cc0000",
        success="#00cc00",
        accent="#00cc00",
        dark=True,
        variables={
            "header-color": "#00ff00",
            "border-color": "#00cc00",
            "pin-color": "#ffff00",
            "tag-color": "#00ff00",
            "project-color": "#00cc00",
            "selected-bg": "#003300",
            "selected-fg": "#ffffff",
            "dim-color": "#88aa88",
            "age-today": "#00ff00",
            "age-week": "#ffff00",
            "age-old": "#666666",
            "status-color": "#00ff00",
            "badge-bg": "#00aa00",
            "badge-fg": "#000000",
            "warn-color": "#ff4444",
            "accent-color": "#00cc00",
            "input-color": "#00ff00",
            "tmux-thinking": "#00cc00",
            "tmux-input": "#ffff00",
            "tmux-approval": "#ff4444",
            "tmux-idle": "#557755",
        },
    ),
    "ccs-light": Theme(
        name="ccs-light",
        primary="#0044cc",
        secondary="#cc00cc",
        warning="#cc0000",
        success="#00aa00",
        accent="#0044cc",
        dark=False,
        variables={
            "header-color": "#0044cc",
            "border-color": "#0044cc",
            "pin-color": "#cc0000",
            "tag-color": "#00aa00",
            "project-color": "#cc00cc",
            "selected-bg": "#cce0ff",
            "selected-fg": "#000000",
            "dim-color": "#888888",
            "age-today": "#00aa00",
            "age-week": "#0044cc",
            "age-old": "#999999",
            "status-color": "#00aa00",
            "badge-bg": "#0044cc",
            "badge-fg": "#ffffff",
            "warn-color": "#cc0000",
            "accent-color": "#0044cc",
            "input-color": "#0044cc",
            "tmux-thinking": "#00aa00",
            "tmux-input": "#0044cc",
            "tmux-approval": "#cc0000",
            "tmux-idle": "#999999",
        },
    ),
    "ccs-purple": Theme(
        name="ccs-purple",
        primary="#aa66ff",
        secondary="#ff66aa",
        warning="#cc0000",
        success="#00cc00",
        accent="#aa66ff",
        dark=True,
        variables={
            "header-color": "#cc88ff",
            "border-color": "#aa66ff",
            "pin-color": "#ffcc00",
            "tag-color": "#66ffcc",
            "project-color": "#ff66aa",
            "selected-bg": "#330066",
            "selected-fg": "#ffffff",
            "dim-color": "#9988aa",
            "age-today": "#66ffcc",
            "age-week": "#ffcc00",
            "age-old": "#666666",
            "status-color": "#cc88ff",
            "badge-bg": "#7744bb",
            "badge-fg": "#ffffff",
            "warn-color": "#ff4444",
            "accent-color": "#aa66ff",
            "input-color": "#ffcc00",
            "tmux-thinking": "#66ffcc",
            "tmux-input": "#ffcc00",
            "tmux-approval": "#ff4444",
            "tmux-idle": "#665577",
        },
    ),
    "ccs-yellow": Theme(
        name="ccs-yellow",
        primary="#ccaa00",
        secondary="#ff8800",
        warning="#cc0000",
        success="#00cc00",
        accent="#ccaa00",
        dark=True,
        variables={
            "header-color": "#ffdd00",
            "border-color": "#ccaa00",
            "pin-color": "#ff8800",
            "tag-color": "#00ff00",
            "project-color": "#ff8800",
            "selected-bg": "#333300",
            "selected-fg": "#ffffff",
            "dim-color": "#aa9966",
            "age-today": "#00ff00",
            "age-week": "#ffdd00",
            "age-old": "#666666",
            "status-color": "#ffdd00",
            "badge-bg": "#aa8800",
            "badge-fg": "#000000",
            "warn-color": "#ff4444",
            "accent-color": "#ccaa00",
            "input-color": "#ffdd00",
            "tmux-thinking": "#00cc00",
            "tmux-input": "#ffdd00",
            "tmux-approval": "#ff4444",
            "tmux-idle": "#777755",
        },
    ),
    "ccs-white": Theme(
        name="ccs-white",
        primary="#333333",
        secondary="#666666",
        warning="#cc0000",
        success="#00aa00",
        accent="#333333",
        dark=False,
        variables={
            "header-color": "#333333",
            "border-color": "#999999",
            "pin-color": "#cc0000",
            "tag-color": "#007700",
            "project-color": "#6600aa",
            "selected-bg": "#dddddd",
            "selected-fg": "#000000",
            "dim-color": "#999999",
            "age-today": "#007700",
            "age-week": "#333333",
            "age-old": "#aaaaaa",
            "status-color": "#007700",
            "badge-bg": "#333333",
            "badge-fg": "#ffffff",
            "warn-color": "#cc0000",
            "accent-color": "#333333",
            "input-color": "#0044cc",
            "tmux-thinking": "#007700",
            "tmux-input": "#0044cc",
            "tmux-approval": "#cc0000",
            "tmux-idle": "#aaaaaa",
        },
    ),
    "ccs-black": Theme(
        name="ccs-black",
        primary="#999999",
        secondary="#666666",
        warning="#cc0000",
        success="#00cc00",
        accent="#999999",
        dark=True,
        variables={
            "header-color": "#aaaaaa",
            "border-color": "#555555",
            "pin-color": "#cc8800",
            "tag-color": "#00aa00",
            "project-color": "#888888",
            "selected-bg": "#222222",
            "selected-fg": "#ffffff",
            "dim-color": "#555555",
            "age-today": "#00aa00",
            "age-week": "#888888",
            "age-old": "#444444",
            "status-color": "#aaaaaa",
            "badge-bg": "#555555",
            "badge-fg": "#ffffff",
            "warn-color": "#cc4444",
            "accent-color": "#999999",
            "input-color": "#aaaaaa",
            "tmux-thinking": "#00aa00",
            "tmux-input": "#888888",
            "tmux-approval": "#cc4444",
            "tmux-idle": "#333333",
        },
    ),
}

# Map original theme names to Textual theme names
TEXTUAL_THEME_MAP = {name: f"ccs-{name}" for name in THEME_NAMES}


# ── Theme color lookup ────────────────────────────────────────────────
# Rich Text styles cannot reference CSS $variables, so we provide a
# lookup dict keyed by Textual theme name -> semantic role -> hex color.
# Widget render() methods use this to pick text colors that match the
# active theme.

_THEME_COLORS = {}
for _tname, _tobj in CCS_THEMES.items():
    _THEME_COLORS[_tname] = dict(_tobj.variables)


def _tc(app, role: str, fallback: str = "") -> str:
    """Return the hex color string for *role* in the current theme.

    Falls back to *fallback* (or empty string) if the theme or role is
    not found.  Callers can use the result directly in Rich styles, e.g.
    ``Style(color=_tc(self.app, "header-color", "#00ffff"))``.
    """
    theme_name = getattr(app, "_ccs_theme_name", "ccs-dark")
    colors = _THEME_COLORS.get(theme_name, {})
    return colors.get(role, fallback)


# ── Default CSS ───────────────────────────────────────────────────────

DEFAULT_CSS = """
Screen {
    background: $surface;
}

ModalScreen {
    background: $background 25%;
}

#header {
    height: 5;
    dock: top;
    padding: 0 1;
    border: heavy $accent;
    background: $surface;
    layout: horizontal;
}
#header-content {
    width: 1fr;
    height: auto;
}
#menu-button {
    width: auto;
    min-width: 12;
    height: 1;
    dock: right;
    padding: 0 1;
    margin-top: 1;
}

#sessions-view {
    height: 1fr;
}

#detail-view {
    height: 1fr;
    display: none;
}

#detail-view.active {
    display: block;
}

#sessions-view.hidden {
    display: none;
}

#session-columns {
    height: 1;
    padding: 0 1;
    color: $text-muted;
    text-style: dim;
}

SessionListWidget {
    height: 3fr;
    border: heavy $accent;
    border-title-color: $accent-darken-2;
    border-title-style: dim;
    scrollbar-size: 1 1;
}

SessionListWidget > .option-list--option-highlighted {
    background: $accent-darken-3;
    color: $text;
}

PreviewPane {
    height: 2fr;
    border: heavy $accent;
    border-title-color: $accent-darken-2;
    border-title-style: dim;
    padding: 0 1;
    overflow-y: auto;
}

#info-scroll {
    height: 1fr;
    border: heavy $accent;
    border-title-color: $accent-darken-2;
    border-title-style: dim;
    scrollbar-size: 1 1;
}

#info-scroll.focused {
    border: heavy $accent-lighten-2;
    border-title-color: $accent-lighten-3;
    border-title-style: bold;
}

InfoPane {
    padding: 0 1;
}

TmuxPane {
    height: 1fr;
    border: heavy $accent;
    border-title-color: $accent-darken-2;
    border-title-style: dim;
    scrollbar-size: 1 1;
}

TmuxPane.focused {
    border: heavy $accent-lighten-2;
    border-title-color: $accent-lighten-3;
    border-title-style: bold;
}

#footer {
    height: 1;
    dock: bottom;
    background: $surface;
    padding: 0 1;
}
"""


# ── Widget classes ────────────────────────────────────────────────────


class MenuButton(Static):
    """Clickable menu button in the header."""

    def render(self) -> Text:
        tc = lambda role, fb="": _tc(self.app, role, fb)
        return Text(
            " \u2261 Menu ",
            style=Style(
                color=tc("badge-fg", "#000000"),
                bgcolor=tc("accent-color", "#00cccc"),
                bold=True,
            ),
        )


class HeaderBox(Static):
    """Header showing title, profile badge, view label, and hints."""

    view_name = reactive("Sessions")
    profile_name = reactive("default")
    session_count = reactive(0)
    total_count = reactive(0)
    sort_mode = reactive("date")
    search_query = reactive("")
    hints = reactive("")

    def render(self) -> Text:
        """Build a multi-line Rich Text header.

        Line 1: centered title + menu button
        Line 2: Profile badge + View label
        Line 3: context-sensitive hints
        Line 4: session count / sort mode
        """
        tc = lambda role, fb="": _tc(self.app, role, fb)
        text = Text()

        # Line 1 -- title
        title = f" \u25c6 CCS v{VERSION} \u2014 Claude Code Session Manager "
        text.append(title, style=Style(color=tc("header-color", "#00ffff"), bold=True))
        text.append("\n")

        # Line 2 -- profile + view
        text.append("Profile: ", style=Style(color=tc("dim-color", "#888888")))
        text.append(
            f" {self.profile_name} ",
            style=Style(
                color=tc("badge-fg", "#000000"),
                bgcolor=tc("badge-bg", "#00aa00"),
                bold=True,
            ),
        )
        text.append("  View: ", style=Style(color=tc("dim-color", "#888888")))
        text.append(
            f" {self.view_name} ",
            style=Style(color=tc("tag-color", "#00ff00"), bold=True),
        )
        if self.view_name == "Session View":
            text.append("  ")
            text.append(
                " \u25c0 Back ",
                style=Style(color=tc("badge-fg", "#000000"), bgcolor=tc("dim-color", "#888888"), bold=True),
            )
        text.append("\n")

        # Line 3 -- hints
        text.append(self.hints, style=Style(color=tc("dim-color", "#888888")))
        text.append("\n")

        # Line 4 -- info
        labels = {
            "date": "Date",
            "name": "Name",
            "project": "Project",
            "tag": "Tag",
            "messages": "Messages",
            "tmux": "Tmux",
        }
        sort_label = labels.get(self.sort_mode, "Date")
        n = self.session_count
        info = f"{n} session{'s' if n != 1 else ''} \u00b7 Sort: {sort_label}"
        text.append(
            info,
            style=Style(color=tc("accent-color", "#00cccc")),
        )
        if self.search_query:
            text.append("  \u00b7 Filter: ", style=Style(color=tc("dim-color", "#888888")))
            text.append(
                f" {self.search_query} ",
                style=Style(color=tc("warn-color", "#ff4444"), bold=True, reverse=True),
            )
            text.append(" (Esc to clear)", style=Style(color=tc("dim-color", "#888888")))

        return text


# ── Row builder helpers ───────────────────────────────────────────────


def _tmux_state_style(app, state: Optional[str], is_idle: bool) -> Style:
    """Return a Rich Style for the tmux state indicator."""
    tc = lambda role, fb="": _tc(app, role, fb)
    if is_idle:
        return Style(color=tc("tmux-idle", "#666666"))
    if state == "approval":
        return Style(color=tc("tmux-approval", "#ff4444"), bold=True)
    if state == "input":
        return Style(color=tc("tmux-input", "#ffff00"), bold=True)
    if state == "done":
        return Style(color=tc("tmux-idle", "#666666"))
    # thinking / unknown -> green
    return Style(color=tc("tmux-thinking", "#00cc00"), bold=True)


def _tmux_state_label(state: Optional[str], is_idle: bool) -> str:
    """Short label for tmux state shown after the icon."""
    if is_idle:
        return "idle"
    if state == "approval":
        return "APPROVE"
    if state == "input":
        return "input"
    if state == "done":
        return "done"
    if state == "thinking":
        return "working"
    return ""


def _age_style(app, mtime: float) -> Style:
    """Return a Rich Style based on session age."""
    tc = lambda role, fb="": _tc(app, role, fb)
    delta = datetime.datetime.now() - datetime.datetime.fromtimestamp(mtime)
    if delta.days == 0:
        return Style(color=tc("age-today", "#00ff00"))
    elif delta.days < 7:
        return Style(color=tc("age-week", "#ffff00"))
    return Style(color=tc("age-old", "#666666"), dim=True)


def build_session_row(
    app,
    s: Session,
    has_tmux: bool,
    is_idle: bool,
    tmux_state: Optional[str],
    is_marked: bool,
    tag_col_w: int = 0,
) -> Text:
    """Build a Rich Text row for a session in the option list.

    The *app* argument is used to look up theme colors.
    """
    tc = lambda role, fb="": _tc(app, role, fb)
    text = Text()

    # Mark indicator (3 cols)
    if is_marked:
        text.append(" \u25cf ", style=Style(color=tc("accent-color", "#00cccc"), bold=True))
    else:
        text.append("   ")

    # Pin / tmux icons (3 display-cols)
    pin_style = Style(color=tc("pin-color", "#ffff00"), bold=True)
    tmux_ch = "\U0001f4a4" if is_idle else "\u26a1"
    tmux_sty = _tmux_state_style(app, tmux_state, is_idle)

    if s.pinned and has_tmux:
        text.append("\u2605", style=pin_style)
        text.append(tmux_ch, style=tmux_sty)
    elif s.pinned:
        text.append("\u2605  ", style=pin_style)
    elif has_tmux:
        text.append(tmux_ch, style=tmux_sty)
        text.append(" ")
    else:
        text.append("   ")

    # Tag column
    if s.tag:
        disp_tag = f"[{s.tag}]"
        if tag_col_w and len(disp_tag) > tag_col_w - 1:
            disp_tag = disp_tag[: tag_col_w - 2] + "]"
        text.append(disp_tag, style=Style(color=tc("tag-color", "#00ff00"), bold=True))
        pad = max(0, tag_col_w - len(disp_tag))
        text.append(" " * pad)
    elif tag_col_w:
        text.append(" " * tag_col_w)

    # Timestamp with age coloring
    age_sty = _age_style(app, s.mtime)
    text.append(f"{s.ts}  ", style=age_sty)

    # Message count (6 cols)
    if s.msg_count >= 10000:
        msg_str = f"{s.msg_count // 1000:>3d}k  "
    elif s.msg_count >= 1000:
        msg_str = f"{s.msg_count // 1000}.{(s.msg_count % 1000) // 100}k  "
    elif s.msg_count:
        msg_str = f"{s.msg_count:>3d}m  "
    else:
        msg_str = "      "
    text.append(msg_str, style=Style(color=tc("dim-color", "#888888")))

    # Project (24 cols)
    proj = s.project_display
    if len(proj) > 24:
        proj = proj[:22] + ".."
    text.append(
        f"{proj:<24s} ",
        style=Style(color=tc("project-color", "#cc00cc")),
    )

    # Description (remainder)
    desc = s.label
    if len(desc) > 50:
        desc = desc[:49] + "\u2026"
    text.append(desc)

    return text


# ── SessionListWidget ─────────────────────────────────────────────────


class SessionListWidget(OptionList):
    """Scrollable session list with Rich Text rows."""

    # Disable built-in OptionList bindings — all key routing done in CCSApp.on_key
    BINDINGS = []

    def rebuild(
        self,
        sessions: list,
        tmux_sids: set,
        tmux_idle: set,
        tmux_claude_state: dict,
        marked: set,
    ):
        """Clear and rebuild the option list from *sessions*."""
        # Compute tag column width (widest "[tag]" + padding)
        max_tag_w = 0
        for s in sessions:
            if s.tag:
                tw = len(s.tag) + 3  # "[" + tag + "] "
                if tw > max_tag_w:
                    max_tag_w = tw

        self.clear_options()
        for s in sessions:
            has_tmux = s.id in tmux_sids
            is_idle = s.id in tmux_idle
            tmux_state = tmux_claude_state.get(s.id)
            is_marked = s.id in marked
            row = build_session_row(
                self.app, s, has_tmux, is_idle, tmux_state,
                is_marked, max_tag_w,
            )
            self.add_option(Option(row, id=s.id))


# ── Session metadata helper ──────────────────────────────────────────


def _append_session_meta(
    text: Text,
    s: Session,
    mgr: SessionManager,
    tmux_sids: set,
    tmux_idle: set,
    tmux_claude_state: dict,
    git_cache: dict,
    detail: bool = False,
    app=None,
):
    """Append session metadata lines to a Rich Text object.

    *app* is used for theme-aware color lookups.  When ``None``, sensible
    fallback colors are used.
    """
    tc = lambda role, fb="": _tc(app, role, fb) if app else fb

    # Tag
    if s.tag:
        text.append(
            f"  Tag:     {s.tag}\n",
            style=Style(color=tc("tag-color", "#00ff00"), bold=True),
        )

    # Session ID (truncated) with optional pinned indicator
    sid_display = s.id[:36] + ("..." if len(s.id) > 36 else "")
    text.append(
        f"  Session: {sid_display}",
        style=Style(color=tc("dim-color", "#888888")),
    )
    if s.pinned:
        text.append(
            " (pinned)",
            style=Style(color=tc("pin-color", "#ffff00")),
        )
    text.append("\n")

    # Project
    text.append(
        f"  Project: {s.project_display}\n",
        style=Style(color=tc("project-color", "#cc00cc")),
    )

    # Modified timestamp with age coloring
    if app:
        age_sty = _age_style(app, s.mtime)
    else:
        delta = datetime.datetime.now() - datetime.datetime.fromtimestamp(s.mtime)
        if delta.days == 0:
            age_sty = Style(color="#00ff00")
        elif delta.days < 7:
            age_sty = Style(color="#ffff00")
        else:
            age_sty = Style(color="#666666", dim=True)
    text.append(f"  Modified: {s.ts}  ({s.age})\n", style=age_sty)

    # Message count
    text.append(
        f"  Messages: {s.msg_count}\n",
        style=Style(color=tc("accent-color", "#00cccc")),
    )

    # Tmux status
    if s.id in tmux_sids:
        tmux_name = TMUX_PREFIX + s.id
        is_idle = s.id in tmux_idle
        state = tmux_claude_state.get(s.id, "unknown")
        state_sty = _tmux_state_style(app, state, is_idle)
        state_labels = {
            "thinking": "working...",
            "input": "waiting for input",
            "approval": "\u26a0 WAITING FOR APPROVAL",
            "done": "session ended",
            "unknown": "active",
        }
        if is_idle:
            icon = "\U0001f4a4"
            label = "idle"
        else:
            icon = "\u26a1"
            label = state_labels.get(state, "active")
        kill_hint = "  (k to kill)" if detail else ""
        text.append(f"  Tmux:    {icon} {tmux_name} — ", style=Style(color=tc("dim-color", "#888888")))
        text.append(f"{label}", style=state_sty)
        text.append(f"{kill_hint}\n", style=Style(color=tc("dim-color", "#888888")))

    # Git info (from project path)
    proj_path = os.path.expanduser(s.project_display) if s.project_display else ""
    if proj_path and os.path.isdir(proj_path):
        git_info = git_cache.get(proj_path)
        if git_info:
            repo_name, branch, _commits = git_info
            branch_str = f" ({branch})" if branch else ""
            text.append(
                f"  Git:     {repo_name}{branch_str}\n",
                style=Style(color=tc("accent-color", "#00cccc")),
            )


# ── PreviewPane ───────────────────────────────────────────────────────


class PreviewPane(Static):
    """Session metadata preview panel (bottom of sessions view)."""

    def update_preview(
        self,
        s: Optional[Session],
        mgr: SessionManager,
        tmux_sids: set,
        tmux_idle: set,
        tmux_claude_state: dict,
        git_cache: dict,
    ):
        """Rebuild the preview content for session *s*."""
        if s is None:
            self.update(Text("Select a session to preview", style="dim"))
            return
        text = Text()
        _append_session_meta(
            text, s, mgr, tmux_sids, tmux_idle,
            tmux_claude_state, git_cache, detail=False, app=self.app,
        )
        if (
            s.id not in tmux_sids
            and not s.first_msg
            and not s.summary
        ):
            text.append(
                "  (empty session \u2014 no messages yet)\n",
                style=Style(color=_tc(self.app, "dim-color", "#888888")),
            )
        self.update(text)


# ── InfoPane ──────────────────────────────────────────────────────────


class InfoPane(Static):
    """Detailed session info panel (top of detail view)."""

    def update_info(
        self,
        s: Optional[Session],
        mgr: SessionManager,
        tmux_sids: set,
        tmux_idle: set,
        tmux_claude_state: dict,
        git_cache: dict,
        tmux_pane_cache: dict,
    ):
        """Rebuild the detailed info content for session *s*."""
        if s is None:
            self.update(Text("Select a session to preview", style="dim"))
            return

        tc = lambda role, fb="": _tc(self.app, role, fb)
        text = Text()
        _append_session_meta(
            text, s, mgr, tmux_sids, tmux_idle,
            tmux_claude_state, git_cache, detail=True, app=self.app,
        )

        # Git commit log (detail view only)
        proj_path = os.path.expanduser(s.project_display) if s.project_display else ""
        git_info = git_cache.get(proj_path) if proj_path else None
        if git_info:
            _repo, _branch, commits = git_info
            for sha, subject in commits:
                text.append(
                    f"    {sha} {subject}\n",
                    style=Style(color=tc("dim-color", "#888888")),
                )

        # First message + topics (only if no tmux pane content)
        has_tmux = s.id in tmux_sids
        has_pane = bool(tmux_pane_cache.get(s.id))
        if not has_tmux or not has_pane:
            if s.first_msg_long:
                text.append("\n")
                text.append(
                    "  First Message:\n",
                    style=Style(color=tc("header-color", "#00ffff"), bold=True),
                )
                for wl in word_wrap(s.first_msg_long, 80):
                    text.append(f"    {wl}\n")
            if s.summaries:
                text.append("\n")
                text.append(
                    "  Topics:\n",
                    style=Style(color=tc("header-color", "#00ffff"), bold=True),
                )
                for sm in s.summaries[-6:]:
                    text.append(f"    \u2022 {sm[:80]}\n")
            elif not s.first_msg_long:
                text.append(
                    "  (empty session \u2014 no messages yet)\n",
                    style=Style(color=tc("dim-color", "#888888")),
                )

        self.update(text)


# ── TmuxPane ─────────────────────────────────────────────────────────


class TmuxPane(RichLog):
    """Live tmux output with ANSI color rendering."""

    # Disable built-in RichLog bindings — all key routing done in CCSApp.on_key
    BINDINGS = []

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._last_raw_lines: Optional[list] = None
        self._last_state: str = ""

    def update_content(self, raw_lines: Optional[list], state: str = "unknown"):
        """Update with raw ANSI lines from ``tmux capture-pane -e``.

        *raw_lines* is ``None`` when there is no tmux session at all,
        or an empty list when the session exists but has no output yet.
        Preserves scroll position when content changes.
        """
        # Skip if content unchanged
        if raw_lines == self._last_raw_lines and state == self._last_state:
            return
        self._last_raw_lines = raw_lines
        self._last_state = state

        # Remember scroll position — check if user was at the bottom
        was_at_bottom = self.scroll_y >= self.max_scroll_y - 1
        old_scroll_y = self.scroll_y

        self.clear()
        if raw_lines is None:
            if not HAS_TMUX:
                self.write(Text("(tmux not installed)", style="dim"))
            else:
                self.write(Text("(no active tmux session)", style="dim"))
            return
        if not raw_lines:
            self.write(Text("(tmux session active, no output yet)", style="dim"))
            return

        state_labels = {
            "thinking": "thinking...",
            "input": "waiting for input",
            "approval": "waiting for approval",
            "done": "session ended",
            "unknown": "active",
        }
        tc = lambda role, fb="": _tc(self.app, role, fb)
        self.auto_scroll = was_at_bottom
        self.write(
            Text(
                f"Output ({state_labels.get(state, 'active')}):",
                style=Style(color=tc("header-color", "#00ffff"), bold=True),
            )
        )

        # Join lines and render with ANSI color codes preserved
        raw_text = "\n".join(raw_lines)
        ansi_text = Text.from_ansi(raw_text)
        self.write(ansi_text)

        # Restore scroll position if user was scrolled up
        if not was_at_bottom:
            self.scroll_y = min(old_scroll_y, self.max_scroll_y)


# ── FooterBar ─────────────────────────────────────────────────────────


class FooterBar(Static):
    """Single-line status bar at the bottom of the screen."""

    status = reactive("")
    position = reactive("")
    marked_count = reactive(0)

    def render(self) -> Text:
        tc = lambda role, fb="": _tc(self.app, role, fb)
        text = Text()

        if self.status:
            text.append(
                f" {self.status} ",
                style=Style(color=tc("status-color", "#00ff00"), bold=True),
            )
        else:
            text.append(" ccs ", style=Style(color=tc("dim-color", "#888888")))
            text.append("? help", style=Style(color=tc("dim-color", "#888888")))

        # Right side: marked count + position
        right_parts: list = []
        if self.marked_count:
            right_parts.append(f"{self.marked_count} marked")
        if self.position:
            right_parts.append(self.position)
        if right_parts:
            right = " \u00b7 ".join(right_parts)
            text.append("  ")
            text.append(right, style=Style(color=tc("dim-color", "#888888")))

        return text
# ── Modal Screens ────────────────────────────────────────────────────


class HelpModal(ModalScreen):
    """Help overlay showing keyboard shortcuts."""

    DEFAULT_CSS = """
    HelpModal {
        align: center middle;
        background: $background 25%;
    }
    #help-box {
        width: 80;
        max-height: 90%;
        border: heavy $accent;
        background: $surface;
        padding: 2 3;
        overflow-y: auto;
    }
    """

    def __init__(self, view: str = "sessions"):
        super().__init__()
        self.help_view = view

    def compose(self) -> ComposeResult:
        with ScrollableContainer(id="help-box"):
            yield Static(id="help-text")

    def on_mount(self):
        tc = lambda role, fb="": _tc(self.app, role, fb)
        hdr = Style(color=tc("header-color", "#00ffff"), bold=True)
        dim = Style(color=tc("dim-color", "#888888"))
        text = Text()

        if self.help_view == "detail":
            text.append("Session View\n\n", style=Style(bold=True))
            text.append("Panes\n", style=hdr)
            text.append("  Tab            Switch Info / Tmux pane\n")
            text.append("  \u2191 / \u2193          Scroll focused pane\n")
            text.append("  PgUp / PgDn    Page up / down\n")
            text.append("  g / G          Scroll to top / bottom\n\n")
            text.append("Actions\n", style=hdr)
            text.append("  \u23ce              Resume / attach session\n")
            text.append("  k              Kill tmux session\n")
            text.append("  i              Send text to tmux (Ctrl+D to send)\n")
            text.append("  p              Toggle pin\n")
            text.append("  t / T          Set / remove tag\n")
            text.append("  d              Delete Claude session\n\n")
            text.append("Other\n", style=hdr)
            text.append("  P              Profile picker / manager\n")
            text.append("  H              Cycle theme\n")
            text.append("  r              Refresh session list\n")
            text.append("  S              Rescan all Claude sessions\n")
            text.append("  Esc / \u2190        Back to Sessions list\n")
            text.append("  Ctrl-C         Quit\n")
        else:
            text.append("Sessions List\n\n", style=Style(bold=True))
            text.append("Navigation\n", style=hdr)
            text.append("  \u2191              Move up\n")
            text.append("  \u2193              Move down\n")
            text.append("  g / G          Jump to first / last\n")
            text.append("  PgUp / PgDn    Page up / down\n")
            text.append("  \u2192              Open Session View\n\n")
            text.append("Actions\n", style=hdr)
            text.append("  \u23ce              Resume with active profile\n")
            text.append("  P              Profile picker / manager\n")
            text.append("  p              Toggle pin (bulk if marked)\n")
            text.append("  t / T          Set / remove tag\n")
            text.append("  d              Delete session (bulk if marked)\n")
            text.append("  D              Delete all empty sessions\n")
            text.append("  k              Kill tmux session\n")
            text.append("  K              Kill all tmux sessions\n\n")
            text.append("Bulk & Sort\n", style=hdr)
            text.append("  Space          Mark / unmark session\n")
            text.append("  u              Unmark all\n")
            text.append("  s              Cycle sort mode\n")
            text.append("  /              Search / filter sessions\n\n")
            text.append("Sessions\n", style=hdr)
            text.append("  n              Create a new named session\n")
            text.append("  e              Start an ephemeral session\n\n")
            text.append("Other\n", style=hdr)
            text.append("  H              Cycle theme\n")
            text.append("  r              Refresh session list\n")
            text.append("  S              Rescan all Claude sessions\n")
            text.append("  Esc            Quit\n")
            text.append("  Ctrl-C         Quit\n")

        text.append("\nPress any key to close", style=dim)
        self.query_one("#help-text", Static).update(text)
        # Disable scrollable container bindings so keys reach on_key
        self.query_one("#help-box", ScrollableContainer).BINDINGS = []

    def on_click(self, event):
        self.dismiss()

    def on_key(self, event):
        event.stop()
        self.dismiss()


class InfoModal(ModalScreen):
    """Simple info popup with OK button. Dismissed by any key, click, or Esc."""

    DEFAULT_CSS = """
    InfoModal {
        align: center middle;
        background: $background 25%;
    }
    #info-modal-box {
        width: 60;
        height: auto;
        border: heavy $accent;
        background: $surface;
        padding: 2 3;
    }
    #info-modal-message { text-align: center; }
    #info-modal-ok { text-align: center; margin-top: 1; }
    #info-modal-hints { text-align: center; margin-top: 1; }
    """

    def __init__(self, title: str, message: str, color_style: str = "normal"):
        super().__init__()
        self.title_text = title
        self.message_text = message
        self.color_style = color_style

    def compose(self) -> ComposeResult:
        with Vertical(id="info-modal-box"):
            yield Static(id="info-modal-message")
            yield Static(id="info-modal-ok")
            yield Static(id="info-modal-hints")

    def on_mount(self):
        tc = lambda role, fb="": _tc(self.app, role, fb)
        if self.color_style == "warning":
            color = "#ff8800"
        elif self.color_style == "danger":
            color = tc("warn-color", "#ff4444")
        else:
            color = tc("header-color", "#00ffff")
        box = self.query_one("#info-modal-box")
        box.styles.border = ("heavy", color)
        text = Text()
        text.append(f"{self.title_text}\n\n", style=Style(color=color, bold=True))
        text.append(self.message_text, style=Style(color=tc("dim-color", "#888888")))
        self.query_one("#info-modal-message", Static).update(text)
        ok_style = Style(color=color, bold=True, reverse=True)
        self.query_one("#info-modal-ok", Static).update(Text("  OK  ", style=ok_style, justify="center"))
        hints = Text("Enter/Esc/Click to close", style=Style(color=tc("dim-color", "#555555")), justify="center")
        self.query_one("#info-modal-hints", Static).update(hints)

    def on_click(self, event):
        self.dismiss()

    def on_key(self, event):
        event.stop()
        self.dismiss()


class ConfirmModal(ModalScreen[bool]):
    """Yes/No confirmation dialog with arrow-key navigation."""

    DEFAULT_CSS = """
    ConfirmModal {
        align: center middle;
        background: $background 25%;
    }
    #confirm-box {
        width: 68;
        height: auto;
        border: heavy $warning;
        background: $surface;
        padding: 2 3;
    }
    #confirm-message { text-align: center; }
    #confirm-buttons { text-align: center; height: auto; }
    #confirm-hints { text-align: center; margin-top: 1; }
    """

    # color_style: "danger" (red), "warning" (orange), "normal" (theme accent)
    def __init__(self, title: str, message: str, detail: str = "", color_style: str = "danger", default_yes: bool = False):
        super().__init__()
        self.title_text = title
        self.message_text = message
        self.detail_text = detail
        self.color_style = color_style
        self.sel = 0 if default_yes else 1  # 0=Yes, 1=No

    def _get_color(self):
        tc = lambda role, fb="": _tc(self.app, role, fb)
        if self.color_style == "warning":
            return "#ff8800"
        elif self.color_style == "normal":
            return tc("header-color", "#00ffff")
        return tc("warn-color", "#ff4444")

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-box"):
            yield Static(id="confirm-message")
            yield Static(id="confirm-buttons")
            yield Static(id="confirm-hints")

    def on_mount(self):
        tc = lambda role, fb="": _tc(self.app, role, fb)
        color = self._get_color()
        box = self.query_one("#confirm-box")
        if self.color_style == "warning":
            box.styles.border = ("heavy", "#ff8800")
        elif self.color_style == "normal":
            box.styles.border = ("heavy", tc("accent-color", "#00cccc"))
        text = Text()
        text.append(f"{self.title_text}\n\n", style=Style(color=color, bold=True))
        text.append(f"{self.message_text}", style=Style(color=color))
        if self.detail_text:
            text.append(f"\n\n{self.detail_text}", style=Style(color=tc("dim-color", "#888888")))
        self.query_one("#confirm-message", Static).update(text)
        hints = Text("\u2190/\u2192 Select  \u00b7  \u23ce/y Confirm  \u00b7  Esc/n Cancel",
                     style=Style(color=tc("dim-color", "#888888")), justify="center")
        self.query_one("#confirm-hints", Static).update(hints)
        self._render_buttons()

    def _render_buttons(self):
        tc = lambda role, fb="": _tc(self.app, role, fb)
        color = self._get_color()
        sel_style = Style(color=color, bold=True, reverse=True)
        dim_style = Style(color=tc("dim-color", "#888888"))
        text = Text(justify="center")
        yes_label = "  Yes (y)  "
        no_label = "  No (n/Esc)  "
        text.append(yes_label, style=sel_style if self.sel == 0 else dim_style)
        text.append("    ")
        text.append(no_label, style=sel_style if self.sel == 1 else dim_style)
        self.query_one("#confirm-buttons", Static).update(text)

    def on_click(self, event):
        try:
            btns = self.query_one("#confirm-buttons", Static)
            r = btns.region
            if r.contains(event.screen_x, event.screen_y):
                mid = r.x + r.width // 2
                self.dismiss(event.screen_x < mid)
                return
        except Exception:
            pass
        try:
            box = self.query_one("#confirm-box")
            if not box.region.contains(event.screen_x, event.screen_y):
                self.dismiss(False)
        except Exception:
            pass

    def on_key(self, event):
        key = event.key
        event.stop()
        event.prevent_default()
        if key in ("y", "Y", "ctrl+c"):
            self.dismiss(True)
        elif key in ("n", "N", "escape"):
            self.dismiss(False)
        elif key in ("enter", "return"):
            self.dismiss(self.sel == 0)
        elif key in ("left", "right", "up", "down"):
            self.sel = 1 - self.sel
            self._render_buttons()


class LaunchModal(ModalScreen[str]):
    """Launch mode selector with arrow/vim key navigation."""

    # 0=Tmux  1=Tmux Expert  2=Terminal  3=Session View  4=Cancel
    _ACTIONS = ["tmux", "tmux_expert", "terminal", "view", None]
    _LABELS = ["\u26a1 Tmux", "\u26a1 Tmux Expert", "Terminal", "Session View", "Cancel"]

    DEFAULT_CSS = """
    LaunchModal {
        align: center middle;
        background: $background 25%;
    }
    #launch-box {
        width: 68;
        height: auto;
        border: heavy $accent;
        background: $surface;
        padding: 2 3;
    }
    #launch-title {
        text-align: center;
        margin-bottom: 1;
    }
    .launch-opt {
        height: 1;
        text-align: center;
        content-align: center middle;
        padding: 0 1;
    }
    #launch-hints {
        text-align: center;
        margin-top: 1;
    }
    """

    def __init__(self, label: str, show_view: bool = True):
        super().__init__()
        self.session_label = label
        self.show_view = show_view
        self._actions = list(self._ACTIONS)
        self._labels = list(self._LABELS)
        if not show_view:
            idx = self._actions.index("view")
            self._actions.pop(idx)
            self._labels.pop(idx)
        self.sel = 0 if HAS_TMUX else 1

    def compose(self) -> ComposeResult:
        with Vertical(id="launch-box"):
            yield Static(id="launch-title")
            for i in range(len(self._actions)):
                yield Static(id=f"launch-opt-{i}", classes="launch-opt")
            yield Static(id="launch-hints")

    def on_mount(self):
        tc = lambda role, fb="": _tc(self.app, role, fb)
        title = Text(justify="center")
        title.append("Launch Mode\n\n", style=Style(color=tc("header-color", "#00ffff"), bold=True))
        title.append(f"Resume: {self.session_label}", style=Style(color=tc("header-color", "#00ffff")))
        self.query_one("#launch-title", Static).update(title)
        hints = Text("\u2190/\u2192/\u2191/\u2193 Select  \u00b7  \u23ce Confirm  \u00b7  Esc/n Cancel",
                     style=Style(color=tc("dim-color", "#888888")), justify="center")
        self.query_one("#launch-hints", Static).update(hints)
        self._render_options()

    def _render_options(self):
        tc = lambda role, fb="": _tc(self.app, role, fb)
        sel_style = Style(color=tc("header-color", "#00ffff"), bold=True, reverse=True)
        dim_style = Style(color=tc("dim-color", "#888888"))
        disabled_style = Style(color="#555555", dim=True)
        cancel_idx = len(self._actions) - 1

        for i in range(len(self._actions)):
            label = self._labels[i]
            if i == cancel_idx:
                label = f"{label} (Esc/n)"
            padded = f"  {label}  "
            if i == 0 and not HAS_TMUX:
                style = disabled_style
            elif i == self.sel:
                style = sel_style
            else:
                style = dim_style
            self.query_one(f"#launch-opt-{i}", Static).update(
                Text(padded, style=style, justify="center")
            )

    def on_click(self, event):
        for i in range(len(self._actions)):
            try:
                w = self.query_one(f"#launch-opt-{i}", Static)
                if w.region.contains(event.screen_x, event.screen_y):
                    if i == 0 and not HAS_TMUX:
                        return
                    self.dismiss(self._actions[i])
                    return
            except Exception:
                pass
        try:
            box = self.query_one("#launch-box")
            if not box.region.contains(event.screen_x, event.screen_y):
                self.dismiss(None)
        except Exception:
            pass

    def on_key(self, event):
        key = event.key
        event.stop()
        event.prevent_default()

        if key in ("escape", "n", "N"):
            self.dismiss(None)
            return
        if key in ("enter", "return"):
            self.dismiss(self._actions[self.sel])
            return

        cancel_idx = len(self._actions) - 1
        if key in ("up", "left"):
            self.sel = (self.sel - 1) % (cancel_idx + 1)
            if self.sel == 0 and not HAS_TMUX:
                self.sel = cancel_idx
        elif key in ("down", "right"):
            self.sel = (self.sel + 1) % (cancel_idx + 1)
            if self.sel == 0 and not HAS_TMUX:
                self.sel = 1

        self._render_options()


class InputModal(ModalScreen[str]):
    """Multiline text input for sending to tmux."""

    DEFAULT_CSS = """
    InputModal {
        align: center middle;
        background: $background 25%;
    }
    #input-container {
        width: 80%;
        height: 70%;
        border: heavy $accent;
        background: $surface;
        padding: 2 3;
    }
    #input-title { text-align: center; }
    #input-area {
        height: 1fr;
    }
    #input-hints {
        height: 1;
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, target_name: str = "tmux", subtitle: str = ""):
        super().__init__()
        self.target_name = target_name
        self.subtitle = subtitle

    def compose(self) -> ComposeResult:
        with Vertical(id="input-container"):
            yield Static(id="input-title")
            yield TextArea(id="input-area")
            yield Static(id="input-hints")

    def on_mount(self):
        tc = lambda role, fb="": _tc(self.app, role, fb)
        text = Text()
        text.append(f"Send to {self.target_name}", style=Style(color=tc("header-color", "#00ffff"), bold=True))
        if self.subtitle:
            text.append(f"\n{self.subtitle}", style=Style(color=tc("dim-color", "#888888")))
        self.query_one("#input-title", Static).update(text)
        hints = Text("Ctrl+D Send  \u00b7  \u23ce New line  \u00b7  Esc Cancel/Skip", style=Style(color=tc("dim-color", "#888888")))
        self.query_one("#input-hints", Static).update(hints)
        self.query_one("#input-area", TextArea).focus()

    def on_click(self, event):
        try:
            box = self.query_one("#input-container")
            if not box.region.contains(event.screen_x, event.screen_y):
                self.dismiss(None)
        except Exception:
            pass

    def on_key(self, event):
        if event.key == "ctrl+d":
            event.stop()
            ta = self.query_one("#input-area", TextArea)
            text = ta.text
            self.dismiss(text if text.strip() else None)

    def action_cancel(self):
        self.dismiss(None)


class SimpleInputModal(ModalScreen[str]):
    """Single-line text input modal (for tag, new session name)."""

    DEFAULT_CSS = """
    SimpleInputModal {
        align: center middle;
        background: $background 25%;
    }
    #simple-input-container {
        width: 72;
        height: auto;
        border: heavy $accent;
        background: $surface;
        padding: 2 3;
    }
    #simple-input-title { text-align: center; }
    #simple-input-field {
        margin-top: 1;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, title: str, initial: str = "", placeholder: str = ""):
        super().__init__()
        self.title_text = title
        self.initial = initial
        self.placeholder = placeholder

    def compose(self) -> ComposeResult:
        with Vertical(id="simple-input-container"):
            yield Static(id="simple-input-title")
            yield Input(value=self.initial, placeholder=self.placeholder, id="simple-input-field")
            yield Static(id="simple-input-hints")

    def on_mount(self):
        tc = lambda role, fb="": _tc(self.app, role, fb)
        title = Text(self.title_text, style=Style(color=tc("header-color", "#00ffff"), bold=True))
        self.query_one("#simple-input-title", Static).update(title)
        hints = Text("\u23ce Confirm  \u00b7  Esc Cancel", style=Style(color=tc("dim-color", "#888888")))
        self.query_one("#simple-input-hints", Static).update(hints)
        inp = self.query_one("#simple-input-field", Input)
        inp.focus()
        # Move cursor to end
        inp.cursor_position = len(self.initial)

    def on_click(self, event):
        try:
            box = self.query_one("#simple-input-container")
            if not box.region.contains(event.screen_x, event.screen_y):
                self.dismiss(None)
        except Exception:
            pass

    def on_input_submitted(self, event: Input.Submitted):
        self.dismiss(event.value.strip())

    def action_cancel(self):
        self.dismiss(None)


class PathInputModal(ModalScreen[str]):
    """Path input modal with live autocompletion and arrow key navigation."""

    DEFAULT_CSS = """
    PathInputModal {
        align: center middle;
        background: $background 25%;
    }
    #path-input-container {
        width: 96;
        height: auto;
        max-height: 36;
        border: heavy $accent;
        background: $surface;
        padding: 2 3;
    }
    #path-input-title { text-align: center; }
    #path-input-field { margin-top: 1; }
    #path-completions {
        height: auto;
        max-height: 20;
        margin-top: 1;
    }
    #path-input-hints { margin-top: 1; }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel", show=False),
    ]

    def __init__(self, title: str, initial: str = "", placeholder: str = ""):
        super().__init__()
        self.title_text = title
        self.initial = initial
        self.placeholder = placeholder
        self._completions: List[str] = []
        self._comp_idx = -1
        self._last_input = ""

    def compose(self) -> ComposeResult:
        with Vertical(id="path-input-container"):
            yield Static(id="path-input-title")
            yield Input(value=self.initial, placeholder=self.placeholder, id="path-input-field")
            yield Static(id="path-completions")
            yield Static(id="path-input-hints")

    def on_mount(self):
        tc = lambda role, fb="": _tc(self.app, role, fb)
        title = Text(self.title_text, style=Style(color=tc("header-color", "#00ffff"), bold=True))
        self.query_one("#path-input-title", Static).update(title)
        hints = Text("\u2191\u2193 Navigate  \u00b7  Tab Select  \u00b7  \u23ce Confirm  \u00b7  Esc Cancel",
                      style=Style(color=tc("dim-color", "#888888")))
        self.query_one("#path-input-hints", Static).update(hints)
        inp = self.query_one("#path-input-field", Input)
        inp.focus()
        inp.cursor_position = len(self.initial)
        self._last_input = self.initial
        self._refresh_completions(self.initial)

    def _get_completions(self, text: str) -> List[str]:
        """Get directory completions for the current input."""
        expanded = os.path.expanduser(text.rstrip("/"))
        if os.path.isdir(expanded):
            parent = expanded
            prefix = ""
        else:
            parent = os.path.dirname(expanded)
            prefix = os.path.basename(expanded).lower()
        if not os.path.isdir(parent):
            return []
        try:
            entries = []
            home = str(Path.home())
            for name in sorted(os.listdir(parent)):
                if name.startswith("."):
                    continue
                full = os.path.join(parent, name)
                if os.path.isdir(full) and name.lower().startswith(prefix):
                    if full.startswith(home):
                        entries.append("~" + full[len(home):])
                    else:
                        entries.append(full)
            return entries
        except OSError:
            return []

    def _refresh_completions(self, text: str):
        self._completions = self._get_completions(text)
        self._comp_idx = -1
        self._show_completions()

    def _show_completions(self):
        tc = lambda role, fb="": _tc(self.app, role, fb)
        comp_widget = self.query_one("#path-completions", Static)
        if not self._completions:
            comp_widget.update("")
            return
        text = Text()
        # Show window of completions around the selected index
        max_visible = 15
        total = len(self._completions)
        if total <= max_visible:
            start, end = 0, total
        else:
            half = max_visible // 2
            start = max(0, self._comp_idx - half)
            end = start + max_visible
            if end > total:
                end = total
                start = end - max_visible
        for i in range(start, end):
            c = self._completions[i]
            if i == self._comp_idx:
                text.append(f"  \u25b6 {c}\n", style=Style(color=tc("accent-color", "#00cccc"), bold=True))
            else:
                text.append(f"    {c}\n", style=Style(color=tc("dim-color", "#888888")))
        if total > max_visible:
            text.append(f"    ({total} directories)",
                        style=Style(color=tc("dim-color", "#666666")))
        comp_widget.update(text)

    def on_input_changed(self, event: Input.Changed):
        val = event.value
        if val != self._last_input:
            self._last_input = val
            self._refresh_completions(val)

    def on_key(self, event) -> None:
        if event.key in ("down", "up") and self._completions:
            event.prevent_default()
            event.stop()
            if event.key == "down":
                self._comp_idx = (self._comp_idx + 1) % len(self._completions)
            else:
                self._comp_idx = (self._comp_idx - 1) % len(self._completions)
            # Update input to show selected path
            inp = self.query_one("#path-input-field", Input)
            self._last_input = self._completions[self._comp_idx] + "/"
            inp.value = self._last_input
            inp.cursor_position = len(inp.value)
            self._show_completions()
            return

        if event.key == "tab" and self._completions:
            event.prevent_default()
            event.stop()
            inp = self.query_one("#path-input-field", Input)
            if self._comp_idx >= 0:
                # Accept current selection and drill into it
                selected = self._completions[self._comp_idx] + "/"
            elif len(self._completions) == 1:
                selected = self._completions[0] + "/"
            else:
                # Select first
                self._comp_idx = 0
                selected = self._completions[0] + "/"
                inp.value = selected
                inp.cursor_position = len(selected)
                self._last_input = selected
                self._show_completions()
                return
            inp.value = selected
            inp.cursor_position = len(selected)
            self._last_input = selected
            self._refresh_completions(selected)
            return

    def on_click(self, event):
        try:
            box = self.query_one("#path-input-container")
            if not box.region.contains(event.screen_x, event.screen_y):
                self.dismiss(None)
        except Exception:
            pass

    def on_input_submitted(self, event: Input.Submitted):
        self.dismiss(event.value.strip().rstrip("/"))

    def action_cancel(self):
        self.dismiss(None)


class ThemeModal(ModalScreen[str]):
    """Theme picker with live preview on navigation."""

    DEFAULT_CSS = """
    ThemeModal {
        align: center middle;
        background: $background 25%;
    }
    #theme-box {
        width: 56;
        height: auto;
        border: heavy $accent;
        background: $surface;
        padding: 2 3;
    }
    #theme-title { text-align: center; }
    #theme-list-text { height: auto; }
    #theme-hints { margin-top: 1; }
    """

    def __init__(self, current_theme: str, on_preview=None):
        super().__init__()
        self._original = current_theme
        self.cur = THEME_NAMES.index(current_theme) if current_theme in THEME_NAMES else 0
        self._on_preview = on_preview

    def compose(self) -> ComposeResult:
        with Vertical(id="theme-box"):
            yield Static(id="theme-title")
            yield Static(id="theme-list-text")
            yield Static(id="theme-hints")

    def on_mount(self):
        tc = lambda role, fb="": _tc(self.app, role, fb)
        title = Text("Select Theme", style=Style(color=tc("header-color", "#00ffff"), bold=True))
        self.query_one("#theme-title", Static).update(title)
        hints = Text("\u2191/\u2193 navigate  \u00b7  \u23ce select  \u00b7  Esc cancel",
                     style=Style(color=tc("dim-color", "#888888")), justify="center")
        self.query_one("#theme-hints", Static).update(hints)
        self._refresh_display()

    def _refresh_display(self):
        tc = lambda role, fb="": _tc(self.app, role, fb)
        sel_style = Style(color=tc("header-color", "#00ffff"), bold=True, reverse=True)
        dim_style = Style(color=tc("dim-color", "#888888"))
        active_style = Style(color=tc("tag-color", "#00ff00"), bold=True)

        text = Text()
        for i, name in enumerate(THEME_NAMES):
            is_sel = (i == self.cur)
            is_active = (name == self._original)
            prefix = " \u25b8 " if is_sel else "   "
            badge = " *" if is_active else ""
            line = f"{prefix}{name}{badge}"
            if is_sel:
                text.append(line, style=sel_style)
            elif is_active:
                text.append(line, style=active_style)
            else:
                text.append(line, style=dim_style)
            if i < len(THEME_NAMES) - 1:
                text.append("\n")
        self.query_one("#theme-list-text", Static).update(text)

    def _preview_current(self):
        name = THEME_NAMES[self.cur]
        if self._on_preview:
            self._on_preview(name)

    def on_click(self, event):
        try:
            widget = self.query_one("#theme-list-text", Static)
            r = widget.content_region
            if r.contains(event.screen_x, event.screen_y):
                row = event.screen_y - r.y
                if 0 <= row < len(THEME_NAMES):
                    self.cur = row
                    self._refresh_display()
                    self._preview_current()
                    self.dismiss(THEME_NAMES[self.cur])
                    return
        except Exception:
            pass
        try:
            box = self.query_one("#theme-box")
            if not box.region.contains(event.screen_x, event.screen_y):
                if self._on_preview:
                    self._on_preview(self._original)
                self.dismiss(None)
        except Exception:
            pass

    def on_key(self, event):
        key = event.key
        event.stop()
        event.prevent_default()
        n = len(THEME_NAMES)

        if key == "down":
            if self.cur < n - 1:
                self.cur += 1
                self._refresh_display()
                self._preview_current()
        elif key == "up":
            if self.cur > 0:
                self.cur -= 1
                self._refresh_display()
                self._preview_current()
        elif key in ("enter", "return"):
            self.dismiss(THEME_NAMES[self.cur])
        elif key == "escape":
            # Restore original theme
            if self._on_preview:
                self._on_preview(self._original)
            self.dismiss(None)


class ProfilesModal(ModalScreen[str]):
    """Profile picker/manager with text-based rendering."""

    DEFAULT_CSS = """
    ProfilesModal {
        align: center middle;
        background: $background 25%;
    }
    #profiles-box {
        width: 76;
        height: auto;
        max-height: 80%;
        border: heavy $accent;
        background: $surface;
        padding: 2 3;
    }
    #profiles-title { text-align: center; }
    #profiles-list-text { height: auto; }
    #profiles-hints { margin-top: 1; }
    """

    def __init__(self, mgr: SessionManager, active_name: str):
        super().__init__()
        self.mgr = mgr
        self.active_name = active_name
        self._delete_pending = False
        self.cur = 0

    def compose(self) -> ComposeResult:
        with Vertical(id="profiles-box"):
            yield Static(id="profiles-title")
            yield Static(id="profiles-list-text")
            yield Static(id="profiles-hints")

    def on_mount(self):
        tc = lambda role, fb="": _tc(self.app, role, fb)
        title = Text("Profiles", style=Style(color=tc("header-color", "#00ffff"), bold=True))
        self.query_one("#profiles-title", Static).update(title)
        self._refresh_display()

    def _get_profiles(self):
        return self.mgr.load_profiles()

    def _refresh_display(self):
        tc = lambda role, fb="": _tc(self.app, role, fb)
        sel_style = Style(color=tc("header-color", "#00ffff"), bold=True, reverse=True)
        dim_style = Style(color=tc("dim-color", "#888888"))
        tag_style = Style(color=tc("tag-color", "#00ff00"), bold=True)
        badge_style = Style(color=tc("badge-fg", "#000000"), bgcolor=tc("badge-bg", "#00aa00"), bold=True)
        warn_style = Style(color=tc("warn-color", "#ff4444"), bold=True)

        profiles = self._get_profiles()
        text = Text()
        if not profiles:
            text.append("No profiles yet.\n", style=dim_style)
            text.append("Press n to create your first profile.", style=dim_style)
        else:
            for i, p in enumerate(profiles):
                name = p.get("name", "?")
                summary = profile_summary(p)
                is_active = (name == self.active_name)
                is_sel = (i == self.cur)

                prefix = " \u25b8 " if is_sel else "   "
                marker = " * " if is_active else "   "
                line = f"{prefix}{marker}{name:<16s} {summary}"

                if is_sel:
                    text.append(line, style=sel_style)
                else:
                    text.append(prefix)
                    if is_active:
                        text.append(marker, style=badge_style)
                    else:
                        text.append(marker)
                    text.append(f"{name:<16s} ", style=tag_style)
                    text.append(summary, style=dim_style)
                if i < len(profiles) - 1:
                    text.append("\n")

        self.query_one("#profiles-list-text", Static).update(text)

        # Hints
        if self._delete_pending:
            pname = profiles[self.cur].get("name", "?") if profiles and self.cur < len(profiles) else "?"
            hints = Text(f"Delete '{pname}'? y/N", style=warn_style, justify="center")
        else:
            hints = Text("\u23ce Set active  n New  e Edit  d Delete  Esc Back",
                         style=dim_style, justify="center")
        self.query_one("#profiles-hints", Static).update(hints)

    def _get_selected_name(self) -> str:
        profiles = self._get_profiles()
        if 0 <= self.cur < len(profiles):
            return profiles[self.cur].get("name", "")
        return ""

    def on_click(self, event):
        if self._delete_pending:
            return
        try:
            widget = self.query_one("#profiles-list-text", Static)
            r = widget.content_region
            if r.contains(event.screen_x, event.screen_y):
                row = event.screen_y - r.y
                profiles = self._get_profiles()
                if 0 <= row < len(profiles):
                    self.cur = row
                    self._refresh_display()
                    name = self._get_selected_name()
                    if name:
                        self.dismiss(f"activate:{name}")
                    return
        except Exception:
            pass
        try:
            box = self.query_one("#profiles-box")
            if not box.region.contains(event.screen_x, event.screen_y):
                self.dismiss(None)
        except Exception:
            pass

    def on_key(self, event):
        key = event.key
        event.stop()
        event.prevent_default()
        profiles = self._get_profiles()
        n = len(profiles)

        if self._delete_pending:
            if key in ("y", "Y"):
                name = self._get_selected_name()
                if name:
                    self.dismiss(f"delete:{name}")
            elif key in ("n", "N", "escape"):
                self._delete_pending = False
                self._refresh_display()
            return

        if key in ("escape",):
            self.dismiss(None)
        elif key == "down":
            if self.cur < n - 1:
                self.cur += 1
                self._refresh_display()
        elif key == "up":
            if self.cur > 0:
                self.cur -= 1
                self._refresh_display()
        elif key in ("enter", "return"):
            name = self._get_selected_name()
            if name:
                self.dismiss(f"activate:{name}")
        elif key == "n":
            self.dismiss("new")
        elif key == "e":
            name = self._get_selected_name()
            if name:
                self.dismiss(f"edit:{name}")
        elif key == "d":
            name = self._get_selected_name()
            if name and name.lower() != "default":
                self._delete_pending = True
                self._refresh_display()


class ProfileEditModal(ModalScreen[dict]):
    """Profile editor with text-based rendering and full key navigation."""

    DEFAULT_CSS = """
    ProfileEditModal {
        align: center middle;
        background: $background 25%;
    }
    #profedit-box {
        width: 88;
        height: auto;
        max-height: 90%;
        border: heavy $accent;
        background: $surface;
        padding: 2 3;
    }
    #profedit-title { text-align: center; }
    #profedit-rows-text { height: auto; }
    #profedit-hints { margin-top: 1; }
    """

    _TEXT_FIELDS = {ROW_PROF_NAME, ROW_EXPERT, ROW_SYSPROMPT, ROW_TOOLS, ROW_MCP, ROW_CUSTOM}

    def __init__(self, profile: dict = None):
        super().__init__()
        self.editing_profile = profile  # None = new
        self.expert_mode = bool(profile.get("expert_args", "").strip()) if profile else False
        if profile:
            self.prof_name = profile.get("name", "")
            self.model_idx = 0
            model = profile.get("model", "")
            for i, (_, mid) in enumerate(MODELS):
                if mid == model:
                    self.model_idx = i
                    break
            self.perm_idx = 0
            perm = profile.get("permission_mode", "")
            for i, (_, pid) in enumerate(PERMISSION_MODES):
                if pid == perm:
                    self.perm_idx = i
                    break
            self.toggles = [False] * len(TOGGLE_FLAGS)
            flags = profile.get("flags", [])
            for i, (_, cli_flag) in enumerate(TOGGLE_FLAGS):
                self.toggles[i] = cli_flag in flags
            self.sysprompt = profile.get("system_prompt", "")
            self.tools_val = profile.get("tools", "")
            self.mcp_val = profile.get("mcp_config", "")
            self.custom_val = profile.get("custom_args", "")
            self.expert_args = profile.get("expert_args", "")
            self.use_tmux = profile.get("tmux", True)
        else:
            self.prof_name = ""
            self.model_idx = 0
            self.perm_idx = 0
            self.toggles = [False] * len(TOGGLE_FLAGS)
            self.sysprompt = ""
            self.tools_val = ""
            self.mcp_val = ""
            self.custom_val = ""
            self.expert_args = ""
            self.use_tmux = True
        self.rows = build_profile_edit_rows(self.expert_mode)
        self.cur = 0
        self._editing_field = None

    def compose(self) -> ComposeResult:
        with Vertical(id="profedit-box"):
            yield Static(id="profedit-title")
            yield Static(id="profedit-rows-text")
            yield Static(id="profedit-hints")

    def on_mount(self):
        self._update_title()
        self._refresh_display()
        if self.editing_profile is None:
            self.call_after_refresh(self._start_name_edit)

    def _start_name_edit(self):
        self._edit_text_field(ROW_PROF_NAME)

    def _update_title(self):
        tc = lambda role, fb="": _tc(self.app, role, fb)
        is_new = self.editing_profile is None
        mode_label = "Expert" if self.expert_mode else "Structured"
        title = Text(f"{'New' if is_new else 'Edit'} Profile ({mode_label})",
                     style=Style(color=tc("header-color", "#00ffff"), bold=True))
        self.query_one("#profedit-title", Static).update(title)

    def _refresh_display(self):
        self.rows = build_profile_edit_rows(self.expert_mode)
        if self.cur >= len(self.rows):
            self.cur = max(0, len(self.rows) - 1)

        tc = lambda role, fb="": _tc(self.app, role, fb)
        sel_style = Style(color=tc("header-color", "#00ffff"), bold=True, reverse=True)
        dim_style = Style(color=tc("dim-color", "#888888"))
        tag_style = Style(color=tc("tag-color", "#00ff00"), bold=True)
        save_style = Style(color=tc("status-color", "#00ff00"), bold=True)

        def cb(val):
            return "[x]" if val else "[ ]"

        text = Text()
        for ri, (rtype, ridx) in enumerate(self.rows):
            is_sel = (ri == self.cur)
            prefix = " \u25b8 " if is_sel else "   "
            line = ""
            line_style = sel_style if is_sel else None

            if rtype == ROW_PROF_NAME:
                line = f"{prefix}Name: {self.prof_name or '(enter name)'}"
                if not is_sel:
                    line_style = tag_style
            elif rtype == ROW_TMUX:
                line = f"{prefix}Launch mode:  {cb(self.use_tmux)} tmux   {cb(not self.use_tmux)} direct"
            elif rtype == ROW_EXPERT:
                line = f"{prefix}claude {self.expert_args or '(enter args)'}"
            elif rtype == ROW_MODEL:
                line = f"{prefix}Model:       {MODELS[self.model_idx][0]}"
            elif rtype == ROW_PERMMODE:
                line = f"{prefix}Permissions: {PERMISSION_MODES[self.perm_idx][0]}"
            elif rtype == ROW_TOGGLE:
                flag_name = TOGGLE_FLAGS[ridx][0]
                line = f"{prefix}{flag_name:<38s} {cb(self.toggles[ridx])}"
            elif rtype == ROW_SYSPROMPT:
                v = self.sysprompt[:40] + ("..." if len(self.sysprompt) > 40 else "")
                line = f"{prefix}System prompt: {v or '(none)'}"
            elif rtype == ROW_TOOLS:
                v = self.tools_val[:40] + ("..." if len(self.tools_val) > 40 else "")
                line = f"{prefix}Tools: {v or '(none)'}"
            elif rtype == ROW_MCP:
                v = self.mcp_val[:40] + ("..." if len(self.mcp_val) > 40 else "")
                line = f"{prefix}MCP config: {v or '(none)'}"
            elif rtype == ROW_CUSTOM:
                v = self.custom_val[:40] + ("..." if len(self.custom_val) > 40 else "")
                line = f"{prefix}Custom args: {v or '(none)'}"
            elif rtype == ROW_PROF_SAVE:
                line = f"{prefix}>>> Save <<<"
                if not is_sel:
                    line_style = save_style

            text.append(line, style=line_style or dim_style)
            if ri < len(self.rows) - 1:
                text.append("\n")

        self.query_one("#profedit-rows-text", Static).update(text)

        # Hints
        if self.expert_mode:
            hints_str = "Tab structured \u00b7 \u23ce edit/save \u00b7 Esc cancel"
        else:
            hints_str = "Tab expert \u00b7 Space toggle \u00b7 \u23ce edit/save \u00b7 Esc cancel"
        hints = Text(hints_str, style=dim_style, justify="center")
        self.query_one("#profedit-hints", Static).update(hints)

    def _to_profile_dict(self) -> dict:
        name = self.prof_name.strip()
        if self.expert_mode:
            return {
                "name": name, "model": "", "permission_mode": "", "flags": [],
                "system_prompt": "", "tools": "", "mcp_config": "",
                "custom_args": "", "expert_args": self.expert_args,
                "tmux": self.use_tmux,
            }
        flags = [TOGGLE_FLAGS[i][1] for i, v in enumerate(self.toggles) if v]
        return {
            "name": name,
            "model": MODELS[self.model_idx][1],
            "permission_mode": PERMISSION_MODES[self.perm_idx][1],
            "flags": flags,
            "system_prompt": self.sysprompt,
            "tools": self.tools_val,
            "mcp_config": self.mcp_val,
            "custom_args": self.custom_val,
            "expert_args": "",
            "tmux": self.use_tmux,
        }

    def _get_field_value(self, rtype: str) -> str:
        mapping = {
            ROW_PROF_NAME: lambda: self.prof_name,
            ROW_EXPERT: lambda: self.expert_args,
            ROW_SYSPROMPT: lambda: self.sysprompt,
            ROW_TOOLS: lambda: self.tools_val,
            ROW_MCP: lambda: self.mcp_val,
            ROW_CUSTOM: lambda: self.custom_val,
        }
        getter = mapping.get(rtype)
        return getter() if getter else ""

    def _set_field_value(self, rtype: str, val: str):
        if rtype == ROW_PROF_NAME:
            self.prof_name = val
        elif rtype == ROW_EXPERT:
            self.expert_args = val
        elif rtype == ROW_SYSPROMPT:
            self.sysprompt = val
        elif rtype == ROW_TOOLS:
            self.tools_val = val
        elif rtype == ROW_MCP:
            self.mcp_val = val
        elif rtype == ROW_CUSTOM:
            self.custom_val = val

    def _edit_text_field(self, rtype: str):
        labels = {
            ROW_PROF_NAME: "Profile Name",
            ROW_EXPERT: "Expert CLI Args",
            ROW_SYSPROMPT: "System Prompt",
            ROW_TOOLS: "Tools",
            ROW_MCP: "MCP Config Path",
            ROW_CUSTOM: "Custom Args",
        }
        title = labels.get(rtype, "Edit")
        current = self._get_field_value(rtype)
        self._editing_field = rtype

        def on_result(result: str) -> None:
            if result is not None:
                self._set_field_value(self._editing_field, result)
            self._editing_field = None
            self._refresh_display()

        self.app.push_screen(SimpleInputModal(title, current), on_result)

    def _toggle_current(self, rtype, ridx):
        if rtype == ROW_MODEL:
            self.model_idx = (self.model_idx + 1) % len(MODELS)
        elif rtype == ROW_PERMMODE:
            self.perm_idx = (self.perm_idx + 1) % len(PERMISSION_MODES)
        elif rtype == ROW_TOGGLE:
            self.toggles[ridx] = not self.toggles[ridx]
        elif rtype == ROW_TMUX:
            self.use_tmux = not self.use_tmux
        self._refresh_display()

    def _do_save(self):
        name = self.prof_name.strip()
        if not name:
            self.notify("Profile name cannot be empty", severity="warning")
            return
        self.dismiss(self._to_profile_dict())

    def _activate_current(self):
        if self.cur >= len(self.rows):
            return
        rtype, ridx = self.rows[self.cur]
        if rtype == ROW_PROF_SAVE:
            self._do_save()
        elif rtype in self._TEXT_FIELDS:
            self._edit_text_field(rtype)
        else:
            self._toggle_current(rtype, ridx)

    def on_click(self, event):
        try:
            widget = self.query_one("#profedit-rows-text", Static)
            r = widget.content_region
            if r.contains(event.screen_x, event.screen_y):
                row = event.screen_y - r.y
                if 0 <= row < len(self.rows):
                    self.cur = row
                    self._refresh_display()
                    self._activate_current()
                    return
        except Exception:
            pass
        try:
            box = self.query_one("#profedit-box")
            if not box.region.contains(event.screen_x, event.screen_y):
                self.dismiss(None)
        except Exception:
            pass

    def on_key(self, event):
        key = event.key
        event.stop()
        event.prevent_default()
        n = len(self.rows)

        if key == "escape":
            self.dismiss(None)
        elif key == "tab":
            self.expert_mode = not self.expert_mode
            self._update_title()
            self._refresh_display()
        elif key == "down":
            if self.cur < n - 1:
                self.cur += 1
                self._refresh_display()
        elif key == "up":
            if self.cur > 0:
                self.cur -= 1
                self._refresh_display()
        elif key in ("enter", "return", "space"):
            self._activate_current()


class ContextMenuModal(ModalScreen[str]):
    """Context menu with clickable items and separator support.

    Items with action_key "---" are rendered as horizontal separators
    and are not selectable.
    """

    DEFAULT_CSS = """
    ContextMenuModal {
        align: right top;
        background: $background 15%;
    }
    #ctx-menu-box {
        width: 40;
        max-height: 36;
        background: $surface;
        border: solid $primary;
        padding: 0 1;
    }
    #ctx-menu-title {
        text-align: center;
        text-style: bold;
        padding: 0 0 1 0;
    }
    .ctx-item {
        height: 1;
        padding: 0 1;
    }
    """

    def __init__(self, title, items):
        """items: list of (label, action_key) tuples. action_key "---" = separator."""
        super().__init__()
        self.title_text = title
        self.items = items
        # Find first selectable index
        self.cur = 0
        for i, (_, key) in enumerate(items):
            if key != "---":
                self.cur = i
                break

    def _is_separator(self, idx):
        return self.items[idx][1] == "---"

    def compose(self):
        with Vertical(id="ctx-menu-box"):
            yield Static("", id="ctx-menu-title")
            for i, (label, _key) in enumerate(self.items):
                yield Static(label, id=f"ctx-item-{i}", classes="ctx-item")

    def on_mount(self):
        tc = lambda role, fb="": _tc(self.app, role, fb)
        accent = tc("accent-color", "#00cccc")
        box = self.query_one("#ctx-menu-box")
        box.styles.border = ("heavy", accent)
        title = Text(self.title_text, style=Style(color=tc("header-color", "#00ffff"), bold=True))
        self.query_one("#ctx-menu-title", Static).update(title)
        self._refresh_display()

    def _refresh_display(self):
        tc = lambda role, fb="": _tc(self.app, role, fb)
        sel_color = tc("header-color", "#00ffff")
        dim_color = tc("dim-color", "#888888")
        sep_color = tc("dim-color", "#555555")
        for i, (label, key) in enumerate(self.items):
            widget = self.query_one(f"#ctx-item-{i}", Static)
            if key == "---":
                widget.update(Text("  " + "\u2500" * 34, style=Style(color=sep_color)))
            elif i == self.cur:
                widget.update(Text(f" > {label}", style=Style(color=sel_color, bold=True, reverse=True)))
            else:
                widget.update(Text(f"   {label}", style=Style(color=dim_color)))

    def on_click(self, event):
        """Handle clicks on menu items."""
        for i in range(len(self.items)):
            if self._is_separator(i):
                continue
            try:
                widget = self.query_one(f"#ctx-item-{i}", Static)
                if widget.region.contains(event.screen_x, event.screen_y):
                    self.dismiss(self.items[i][1])
                    return
            except Exception:
                pass
        # Click outside items dismisses
        try:
            box = self.query_one("#ctx-menu-box")
            if not box.region.contains(event.screen_x, event.screen_y):
                self.dismiss(None)
        except Exception:
            self.dismiss(None)

    def _move_cursor(self, direction):
        """Move cursor skipping separators. direction: 1=down, -1=up."""
        n = len(self.items)
        new = self.cur + direction
        while 0 <= new < n and self._is_separator(new):
            new += direction
        if 0 <= new < n:
            self.cur = new
            self._refresh_display()

    def on_key(self, event):
        key = event.key
        event.stop()
        event.prevent_default()
        if key in ("escape", "m"):
            self.dismiss(None)
        elif key == "up":
            self._move_cursor(-1)
        elif key == "down":
            self._move_cursor(1)
        elif key in ("enter", "return"):
            if not self._is_separator(self.cur):
                self.dismiss(self.items[self.cur][1])


class CCSApp(App):
    """Textual TUI for Claude Code Session Manager."""

    CSS = DEFAULT_CSS  # from part2.py

    BINDINGS = [
        Binding("ctrl+c", "quit_confirm", "Quit", show=False, priority=True),
        Binding("f5", "refresh", "Refresh", show=False),
    ]

    exit_action = None  # Set before exit for terminal-mode launch

    def __init__(self):
        super().__init__()
        # Register themes early so CSS variables are available
        for name, theme_obj in CCS_THEMES.items():
            self.register_theme(theme_obj)
        self.mgr = SessionManager()
        self.mgr.purge_ephemeral()
        self.sessions = []
        self.filtered = []
        self.search_query = ""
        self.sort_mode = "date"
        self.marked = set()
        self.view = "sessions"  # "sessions" | "detail"

        # Active profile & theme
        self.active_profile_name = self.mgr.load_active_profile_name()
        self._ccs_theme_name = TEXTUAL_THEME_MAP.get(
            self.mgr.load_theme(), "ccs-dark"
        )
        self.theme = self._ccs_theme_name

        # Tmux state
        self.tmux_sids = set()  # set of session IDs with live tmux
        self.tmux_idle = set()
        self.tmux_idle_prev = set()
        self.tmux_last_poll = 0.0
        self.tmux_pane_cache = {}  # sid -> list[str] (raw ANSI lines)
        self.tmux_pane_cache_stripped = {}  # sid -> list[str] (stripped lines)
        self.tmux_pane_ts = {}
        self.tmux_claude_state = {}
        self._git_cache = {}

        self.detail_focus = "info"
        self.exit_action = None
        self._status_timer = None
        self._last_click_time = 0.0
        self._last_click_idx = -1
        self._last_preview_click = 0.0

    def compose(self) -> ComposeResult:
        with Container(id="header"):
            yield HeaderBox(id="header-content")
            yield MenuButton(id="menu-button")
        with Container(id="sessions-view"):
            yield Static(id="session-columns")
            sl = SessionListWidget(id="session-list")
            sl.border_title = "Sessions"
            yield sl
            pp = PreviewPane(id="preview")
            pp.border_title = "Details"
            yield pp
        with Container(id="detail-view"):
            info_scroll = ScrollableContainer(id="info-scroll")
            info_scroll.border_title = "Session Info"
            with info_scroll:
                yield InfoPane(id="info-pane")
            tmux_pane = TmuxPane(id="tmux-pane")
            tmux_pane.border_title = "Claude Preview"
            yield tmux_pane
        yield FooterBar(id="footer")

    def on_mount(self):
        # Initial data load
        self._do_refresh()

        # Timers
        self.set_interval(TMUX_POLL_INTERVAL, self._poll_tmux_activity)
        self.set_interval(TMUX_CAPTURE_INTERVAL, self._poll_tmux_capture)

        # Startup warnings
        warnings = []
        if not HAS_TMUX:
            warnings.append("tmux")
        if not HAS_GIT:
            warnings.append("git")
        if warnings:
            self._set_status(f"Not installed: {', '.join(warnings)}")

        # Update header hints
        self._update_header()

        # Focus session list for key routing
        self.query_one("#session-list", SessionListWidget).focus()

    # -- Data management ---------------------------------------------------

    def _do_refresh(self, force=False):
        """Refresh session data and rebuild UI."""
        self.sessions = self.mgr.scan(self.sort_mode, force=force)
        if HAS_TMUX:
            self.tmux_sids = self.mgr.tmux_alive_sids()
        else:
            self.tmux_sids = set()
        # Re-sort for tmux mode
        if self.sort_mode == "tmux":
            sids = self.tmux_sids
            self.sessions.sort(
                key=lambda s: (
                    0 if s.pinned else 1,
                    0 if s.id in sids else 1,
                    -s.mtime,
                )
            )
        self._apply_filter()
        # Prune stale pane cache
        stale = set(self.tmux_pane_cache) - set(self.tmux_sids)
        for sid in stale:
            self.tmux_pane_cache.pop(sid, None)
            self.tmux_pane_cache_stripped.pop(sid, None)
            self.tmux_pane_ts.pop(sid, None)
            self.tmux_claude_state.pop(sid, None)
        self.tmux_last_poll = 0
        self._rebuild_list()
        self._update_preview()
        self._update_header()

        valid_ids = {s.id for s in self.filtered}
        self.marked &= valid_ids

    def _apply_filter(self):
        q = self.search_query.lower()
        if not q:
            self.filtered = list(self.sessions)
        else:
            self.filtered = [
                s for s in self.sessions
                if q in (s.tag or "").lower()
                or q in (s.label or "").lower()
                or q in s.project_display.lower()
                or q in s.id.lower()
            ]

    def _rebuild_list(self):
        sl = self.query_one("#session-list", SessionListWidget)
        # Preserve current selection across rebuild
        prev_id = None
        if sl.highlighted is not None and sl.highlighted < len(self.filtered):
            prev_id = self.filtered[sl.highlighted].id
        elif sl.highlighted is not None:
            # Try to get the option ID from the OptionList
            try:
                opt = sl.get_option_at_index(sl.highlighted)
                prev_id = opt.id
            except Exception:
                pass
        sl.rebuild(
            self.filtered,
            self.tmux_sids,
            self.tmux_idle,
            self.tmux_claude_state,
            self.marked,
        )
        # Update column header
        max_tag_w = 0
        for s in self.filtered:
            if s.tag:
                tw = len(s.tag) + 3
                if tw > max_tag_w:
                    max_tag_w = tw
        tag_hdr = f"{'Tag':<{max_tag_w}}" if max_tag_w else ""
        hdr = f"      {tag_hdr}{'Modified':<18s}{'Msgs':<6s}{'Project':<25s}Description"
        self.query_one("#session-columns", Static).update(
            Text(hdr, style=Style(dim=True))
        )
        # Restore selection
        if prev_id is not None:
            for i, s in enumerate(self.filtered):
                if s.id == prev_id:
                    sl.highlighted = i
                    break
            else:
                # Session no longer in list; select first if available
                if self.filtered:
                    sl.highlighted = 0
        elif self.filtered:
            sl.highlighted = 0
        self._update_footer()

    def _update_preview(self):
        s = self._current_session()
        preview = self.query_one("#preview", PreviewPane)
        preview.update_preview(
            s,
            self.mgr,
            self.tmux_sids,
            self.tmux_idle,
            self.tmux_claude_state,
            self._git_cache,
        )

    def _update_detail(self):
        s = self._current_session()
        if s is None:
            return
        # Ensure git info is loaded from project path
        proj_path = os.path.expanduser(s.project_display) if s.project_display else ""
        if proj_path and os.path.isdir(proj_path) and proj_path not in self._git_cache:
            self._get_git_info(proj_path)
        info = self.query_one("#info-pane", InfoPane)
        info.update_info(
            s,
            self.mgr,
            self.tmux_sids,
            self.tmux_idle,
            self.tmux_claude_state,
            self._git_cache,
            self.tmux_pane_cache,
        )
        # Update tmux pane
        tmux_pane = self.query_one("#tmux-pane", TmuxPane)
        if s.id in self.tmux_sids:
            raw_lines = self.tmux_pane_cache.get(s.id, [])
            state = self.tmux_claude_state.get(s.id, "unknown")
            tmux_pane.update_content(raw_lines, state)
        else:
            tmux_pane.update_content(None)

    def _update_header(self):
        header = self.query_one("#header-content", HeaderBox)
        header.view_name = "Session View" if self.view == "detail" else "Sessions"
        header.profile_name = self.active_profile_name
        header.session_count = len(self.filtered)
        header.total_count = len(self.sessions)
        header.sort_mode = self.sort_mode
        header.search_query = self.search_query
        if self.view == "detail":
            header.hints = (
                "\u2190/Esc back \u00b7 \u2191/\u2193 scroll \u00b7 Tab switch panel \u00b7 \u2192/\u23ce resume"
                " \u00b7 p pin \u00b7 t tag \u00b7 d del \u00b7 m menu"
            )
        else:
            header.hints = (
                "\u2191/\u2193 nav \u00b7 \u2192 view \u00b7 \u23ce resume \u00b7 p pin \u00b7 t tag"
                " \u00b7 n new \u00b7 / search \u00b7 s sort \u00b7 m menu \u00b7 ? help"
            )

    def _update_footer(self):
        footer = self.query_one("#footer", FooterBar)
        footer.marked_count = len(self.marked)
        sl = self.query_one("#session-list", SessionListWidget)
        if sl.option_count > 0 and sl.highlighted is not None:
            footer.position = f"{sl.highlighted + 1}/{sl.option_count}"
        else:
            footer.position = ""

    def _current_session(self):
        sl = self.query_one("#session-list", SessionListWidget)
        if sl.highlighted is not None and sl.highlighted < len(self.filtered):
            return self.filtered[sl.highlighted]
        return None

    def _set_status(self, msg, ttl=5):
        footer = self.query_one("#footer", FooterBar)
        footer.status = msg
        if self._status_timer:
            self._status_timer.stop()
        self._status_timer = self.set_timer(ttl, self._clear_status)

    def _clear_status(self):
        try:
            footer = self.query_one("#footer", FooterBar)
            footer.status = ""
        except Exception:
            pass

    # -- Git info ----------------------------------------------------------

    def _get_git_info(self, cwd):
        if not HAS_GIT or not cwd:
            return None
        if cwd in self._git_cache:
            return self._git_cache[cwd]
        try:
            r = subprocess.run(
                ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if r.returncode != 0:
                self._git_cache[cwd] = None
                return None
            repo_name = os.path.basename(r.stdout.strip())
        except Exception:
            self._git_cache[cwd] = None
            return None
        branch = ""
        try:
            r = subprocess.run(
                ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if r.returncode == 0:
                branch = r.stdout.strip()
        except Exception:
            pass
        commits = []
        try:
            r = subprocess.run(
                ["git", "-C", cwd, "log", "--oneline", "-5", "--no-color"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if r.returncode == 0:
                for line in r.stdout.strip().splitlines():
                    parts = line.split(" ", 1)
                    commits.append(
                        (parts[0], parts[1] if len(parts) == 2 else "")
                    )
        except Exception:
            pass
        result = (repo_name, branch, commits)
        self._git_cache[cwd] = result
        return result

    # -- Tmux polling ------------------------------------------------------

    def _load_ephemeral_ids(self):
        """Load set of ephemeral session IDs from meta."""
        return {sid for sid, m in self.mgr._load_meta().items() if m.get("ephemeral")}

    def _remove_ephemeral_id(self, sid):
        """Clear the ephemeral flag for a session ID."""
        self.mgr._set_meta(sid, ephemeral=False)

    def _cleanup_gone_sessions(self, gone_sids):
        """Auto-delete ephemeral sessions whose tmux has exited."""
        meta = self.mgr._load_meta()
        changed = False
        for sid in gone_sids:
            is_ephemeral = meta.get(sid, {}).get("ephemeral", False)
            if is_ephemeral:
                for f in glob.glob(str(PROJECTS_DIR / "*" / f"{sid}.jsonl")):
                    try:
                        os.remove(f)
                    except OSError:
                        pass
                self.mgr._delete_meta(sid)
                changed = True
                self._set_status("Ephemeral session cleaned up")
            elif not self._session_file_exists(sid):
                self.mgr._delete_meta(sid)

    def _poll_tmux_activity(self):
        if not HAS_TMUX:
            self.tmux_idle = set()
            return
        # Refresh tmux_sids from live tmux sessions
        old_sids = set(self.tmux_sids)
        try:
            self.tmux_sids = self.mgr.tmux_alive_sids()
        except Exception:
            pass
        new_sids = set(self.tmux_sids)
        sids_changed = (old_sids != new_sids)
        # Prune stale pane cache and auto-delete ephemeral sessions
        gone = old_sids - new_sids
        if gone:
            self._cleanup_gone_sessions(gone)
        for sid in gone:
            self.tmux_pane_cache.pop(sid, None)
            self.tmux_pane_cache_stripped.pop(sid, None)
            self.tmux_pane_ts.pop(sid, None)
            self.tmux_claude_state.pop(sid, None)
        if not self.tmux_sids:
            self.tmux_idle = set()
            if sids_changed:
                self._rebuild_list()
                self._update_footer()
            return
        try:
            r = subprocess.run(
                [
                    "tmux",
                    "list-sessions",
                    "-F",
                    "#{session_name} #{session_activity}",
                ],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if r.returncode != 0:
                if sids_changed:
                    self._rebuild_list()
                    self._update_footer()
                return
        except Exception:
            if sids_changed:
                self._rebuild_list()
                self._update_footer()
            return
        now = time.time()
        activity = {}
        for line in r.stdout.strip().splitlines():
            parts = line.rsplit(" ", 1)
            if len(parts) == 2:
                try:
                    activity[parts[0]] = float(parts[1])
                except ValueError:
                    pass
        self.tmux_idle_prev = self.tmux_idle.copy()
        new_idle = set()
        for sid in self.tmux_sids:
            tmux_name = TMUX_PREFIX + sid
            ts = activity.get(tmux_name)
            if ts is not None and (now - ts) > TMUX_IDLE_SECS:
                new_idle.add(sid)
        newly_idle = new_idle - self.tmux_idle_prev
        if newly_idle:
            names = []
            for sid in newly_idle:
                s = next((s for s in self.sessions if s.id == sid), None)
                names.append(s.tag or s.id[:12] if s else sid[:12])
            self._set_status(f"Idle: {', '.join(names)}")
        idle_changed = (self.tmux_idle != new_idle)
        self.tmux_idle = new_idle
        # Rebuild list if tmux state changed
        if sids_changed or idle_changed:
            self._rebuild_list()
            self._update_footer()

    def _poll_tmux_capture(self):
        """Capture tmux pane output for all active sessions."""
        if not HAS_TMUX or not self.tmux_sids:
            return
        now = time.monotonic()
        old_states = dict(self.tmux_claude_state)
        for sid in self.tmux_sids:
            last = self.tmux_pane_ts.get(sid, 0.0)
            if now - last < TMUX_CAPTURE_INTERVAL:
                continue
            self._capture_one_pane(sid, TMUX_PREFIX + sid)
        # Rebuild session list if any claude state changed
        if self.tmux_claude_state != old_states:
            self._rebuild_list()
            self._update_footer()
        # If in detail view, update tmux pane widget
        if self.view == "detail":
            self._update_detail()

    def _capture_one_pane(self, sid, tmux_name):
        try:
            # Capture WITH ANSI preserved (-e flag) for rendering
            r = subprocess.run(
                ["tmux", "capture-pane", "-t", tmux_name, "-p", "-e"],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if r.returncode != 0:
                return
            raw_lines = r.stdout.splitlines()
            # Trim trailing empty lines
            while raw_lines and not raw_lines[-1].strip():
                raw_lines.pop()
            raw_lines = raw_lines[-TMUX_CAPTURE_LINES:]
            self.tmux_pane_cache[sid] = raw_lines
            self.tmux_pane_ts[sid] = time.monotonic()
            # Strip ANSI for state detection
            stripped = [strip_ansi(ln) for ln in raw_lines]
            self.tmux_pane_cache_stripped[sid] = stripped
            self._detect_claude_state(sid, stripped)
        except Exception:
            pass

    def _detect_claude_state(self, sid, lines):
        if not lines:
            self.tmux_claude_state[sid] = "unknown"
            return
        last_nonempty = ""
        for line in reversed(lines):
            stripped = line.strip()
            if stripped:
                last_nonempty = stripped
                break
        if "Session ended" in last_nonempty or "Returning to ccs" in last_nonempty:
            self.tmux_claude_state[sid] = "done"
            return
        for line in reversed(lines[-5:]):
            low = line.strip().lower()
            if (
                ("allow" in low and ("y/n" in low or "(y)" in low))
                or "do you want to proceed" in low
                or ("permit" in low and "y/n" in low)
            ):
                self.tmux_claude_state[sid] = "approval"
                return
        if last_nonempty in (">", "$") or last_nonempty.endswith("> "):
            self.tmux_claude_state[sid] = "input"
            return
        self.tmux_claude_state[sid] = "thinking"

    def _tmux_send_text(self, tmux_name, text):
        try:
            subprocess.run(
                ["tmux", "send-keys", "-t", tmux_name, "-l", text],
                capture_output=True,
                timeout=2,
            )
            subprocess.run(
                ["tmux", "send-keys", "-t", tmux_name, "Enter"],
                capture_output=True,
                timeout=2,
            )
        except Exception:
            self._set_status("Failed to send input to tmux")

    @staticmethod
    def _tmux_wrap_cmd(cmd_str, tmux_name):
        tn = shlex.quote(tmux_name)
        return (
            f'{cmd_str}; echo ""; echo "Session ended.'
            f' Returning to ccs..."; sleep 1;'
            f' tmux kill-session -t {tn} 2>/dev/null || true'
        )

    def _tmux_launch(self, s, extra, env_vars=""):
        tmux_name = TMUX_PREFIX + s.id
        if s.id in self.mgr.tmux_alive_sids():
            self._tmux_attach(tmux_name, s.id)
            return
        cmd_parts = ["claude", "--resume", s.id] + extra
        cmd_str = " ".join(shlex.quote(p) for p in cmd_parts)
        # cd to project directory so claude can find the session
        proj_dir = os.path.expanduser(s.project_display) if s.project_display else ""
        if proj_dir and os.path.isdir(proj_dir):
            cmd_str = f"cd {shlex.quote(proj_dir)} && {cmd_str}"
        full_cmd = self._tmux_wrap_cmd(cmd_str, tmux_name)
        shell = os.environ.get("SHELL", "/bin/sh")
        tmux_args = [
            "tmux", "new-session", "-d", "-s", tmux_name,
            "-x", "200", "-y", "50",
        ]
        # Use env command to set vars for the shell process
        if env_vars:
            tmux_args.append("env")
            for line in env_vars.strip().splitlines():
                line = line.strip()
                if line and "=" in line and not line.startswith("#"):
                    tmux_args.append(line)
        tmux_args.extend([shell, "-c", full_cmd])
        subprocess.run(tmux_args)
        self._tmux_attach(tmux_name, s.id)

    def _session_file_exists(self, sid):
        """Check if a Claude session .jsonl file exists for this ID."""
        return bool(glob.glob(str(PROJECTS_DIR / "*" / f"{sid}.jsonl")))

    def _session_is_empty(self, sid):
        """Check if a session .jsonl has no user/assistant messages."""
        for f in glob.glob(str(PROJECTS_DIR / "*" / f"{sid}.jsonl")):
            try:
                with open(f) as fh:
                    for line in fh:
                        d = json.loads(line)
                        if d.get("type") in ("user", "assistant"):
                            return False
            except Exception:
                pass
        return True

    def _tmux_attach(self, tmux_name, session_id=None):
        try:
            with self.suspend():
                os.system(f"tmux attach-session -t {shlex.quote(tmux_name)}")
        except Exception:
            self._set_status("Cannot suspend in this environment")
            return
        if not session_id:
            self._do_refresh(force=True)
            return
        is_ephemeral = self.mgr._get_meta(session_id).get("ephemeral", False)
        tmux_alive = subprocess.run(
            ["tmux", "has-session", "-t", tmux_name],
            capture_output=True,
        ).returncode == 0
        has_session = self._session_file_exists(session_id)
        if is_ephemeral:
            # Ephemeral: always kill tmux + delete session + clean up
            if tmux_alive:
                subprocess.run(["tmux", "kill-session", "-t", tmux_name], capture_output=True)
            for f in glob.glob(str(PROJECTS_DIR / "*" / f"{session_id}.jsonl")):
                try:
                    os.remove(f)
                except OSError:
                    pass
            self.mgr._delete_meta(session_id)
            self._set_status("Ephemeral session cleaned up")
        elif not has_session:
            # No session file — nothing to keep, kill tmux + clean up
            if tmux_alive:
                subprocess.run(["tmux", "kill-session", "-t", tmux_name], capture_output=True)
            self.mgr._delete_meta(session_id)
            self._set_status("No session created — tmux killed")
        elif not tmux_alive and self._session_is_empty(session_id):
            # Session exited with no messages — delete the empty session
            for f in glob.glob(str(PROJECTS_DIR / "*" / f"{session_id}.jsonl")):
                try:
                    os.remove(f)
                except OSError:
                    pass
            self.mgr._delete_meta(session_id)
            self._set_status("Empty session deleted")
        self._do_refresh(force=True)

    def _cleanup_session_metadata(self, sid):
        """Remove all metadata for a session ID."""
        self.mgr._delete_meta(sid)

    def _tmux_launch_new(self, name, extra, cwd=None):
        uid = str(uuid_mod.uuid4())
        tmux_name = TMUX_PREFIX + uid
        if name:
            self.mgr._set_meta(uid, tag=name)
        cmd_parts = ["claude", "--session-id", uid] + extra
        cmd_str = " ".join(shlex.quote(p) for p in cmd_parts)
        if cwd:
            cmd_str = f"cd {shlex.quote(cwd)} && {cmd_str}"
        full_cmd = self._tmux_wrap_cmd(cmd_str, tmux_name)
        shell = os.environ.get("SHELL", "/bin/sh")
        subprocess.run(
            [
                "tmux", "new-session", "-d", "-s", tmux_name,
                "-x", "200", "-y", "50",
                shell, "-c", full_cmd,
            ]
        )
        self._tmux_attach(tmux_name, uid)

    def _tmux_launch_ephemeral(self, extra, cwd=None):
        uid = str(uuid_mod.uuid4())
        tmux_name = TMUX_PREFIX + uid
        self.mgr._set_meta(uid, ephemeral=True)
        cmd_parts = ["claude", "--session-id", uid] + extra
        cmd_str = " ".join(shlex.quote(p) for p in cmd_parts)
        if cwd:
            cmd_str = f"cd {shlex.quote(cwd)} && {cmd_str}"
        full_cmd = self._tmux_wrap_cmd(cmd_str, tmux_name)
        shell = os.environ.get("SHELL", "/bin/sh")
        subprocess.run(
            [
                "tmux", "new-session", "-d", "-s", tmux_name,
                "-x", "200", "-y", "50",
                shell, "-c", full_cmd,
            ]
        )
        self._tmux_attach(tmux_name, uid)

    def _active_profile_args(self):
        profiles = self.mgr.load_profiles()
        active = next(
            (p for p in profiles if p.get("name") == self.active_profile_name),
            None,
        )
        return build_args_from_profile(active) if active else []

    def _get_use_tmux(self):
        profiles = self.mgr.load_profiles()
        active = next(
            (p for p in profiles if p.get("name") == self.active_profile_name),
            None,
        )
        return active.get("tmux", True) if active else True

    # -- View switching ----------------------------------------------------

    def _switch_to_detail(self):
        self.view = "detail"
        self.query_one("#sessions-view").add_class("hidden")
        self.query_one("#detail-view").add_class("active")
        self.detail_focus = "info"
        s = self._current_session()
        proj_path = os.path.expanduser(s.project_display) if s and s.project_display else ""
        if proj_path and os.path.isdir(proj_path) and proj_path not in self._git_cache:
            self._get_git_info(proj_path)
        self._update_detail()
        self._update_header()
        # Keep focus on session list so on_key always fires
        self.query_one("#session-list", SessionListWidget).focus()

    def _switch_to_sessions(self):
        self.view = "sessions"
        self.query_one("#sessions-view").remove_class("hidden")
        self.query_one("#detail-view").remove_class("active")
        self._update_header()
        self._update_preview()
        self.query_one("#session-list", SessionListWidget).focus()

    # -- Event handlers ----------------------------------------------------

    def on_option_list_option_highlighted(
        self, event: OptionList.OptionHighlighted
    ):
        if event.option_list.id == "session-list":
            self._update_preview()
            self._update_footer()
            if self.view == "detail":
                self._update_detail()

    def on_option_list_option_selected(
        self, event: OptionList.OptionSelected
    ):
        if event.option_list.id == "session-list":
            now = time.monotonic()
            idx = event.option_index
            if idx == self._last_click_idx and (now - self._last_click_time) < 0.4:
                self._last_click_time = 0.0
                self._last_click_idx = -1
                if self.view == "sessions":
                    self.action_launch()
                elif self.view == "detail":
                    self.action_launch()
            else:
                self._last_click_time = now
                self._last_click_idx = idx

    def _show_session_context_menu(self):
        s = self._current_session()
        if not s:
            return
        label = s.tag or s.label[:30] or s.id[:12]
        is_marked = s.id in self.marked
        mark_label = "Unmark" if is_marked else "Mark"
        items = [
            ("Launch Session", "launch"),
            ("View Details", "view"),
            (mark_label, "mark"),
            ("Toggle Pin", "pin"),
            ("Set Tag", "tag"),
        ]
        if s.id in self.tmux_sids:
            items.append(("Kill Tmux", "kill_tmux"))
        items.append(("Delete Session", "delete"))

        def on_result(action):
            if action == "launch":
                self.action_launch()
            elif action == "view":
                self._switch_to_detail()
            elif action == "mark":
                self.action_mark()
            elif action == "pin":
                self.action_toggle_pin()
            elif action == "tag":
                self.action_set_tag()
            elif action == "kill_tmux":
                self.action_kill_tmux()
            elif action == "delete":
                self.action_delete_session()

        self.push_screen(ContextMenuModal(label, items), on_result)

    def on_click(self, event) -> None:
        """Handle clicks — header opens menu, detail panels switch focus."""
        if isinstance(self.screen, ModalScreen):
            return
        now = time.monotonic()
        # Click on detail view panels to switch focus; double-click on preview opens launch
        if self.view == "detail":
            try:
                tmux = self.query_one("#tmux-pane")
                if tmux.region.contains(event.screen_x, event.screen_y):
                    if (now - self._last_preview_click) < 0.4:
                        self._last_preview_click = 0.0
                        self.action_launch()
                        return
                    self._last_preview_click = now
                    self._set_detail_focus("tmux")
                    return
                info = self.query_one("#info-scroll")
                if info.region.contains(event.screen_x, event.screen_y):
                    self._set_detail_focus("info")
                    return
            except Exception:
                pass
        elif self.view == "sessions":
            # Double-click on preview pane opens launch modal
            try:
                pp = self.query_one("#preview", PreviewPane)
                if pp.region.contains(event.screen_x, event.screen_y):
                    if (now - self._last_preview_click) < 0.4:
                        self._last_preview_click = 0.0
                        self.action_launch()
                        return
                    self._last_preview_click = now
            except Exception:
                pass
        # Click on header view/profile labels
        try:
            hdr = self.query_one("#header-content", HeaderBox)
            if hdr.region.contains(event.screen_x, event.screen_y):
                rel_y = event.screen_y - hdr.region.y
                if rel_y == 1:
                    rel_x = event.screen_x - hdr.region.x
                    # Profile badge area
                    badge_end = len("Profile:  ") + len(self.active_profile_name) + 2
                    if rel_x <= badge_end:
                        self.action_profiles()
                        return
                    # Back button (only in detail view)
                    if self.view == "detail":
                        view_label = " Session View "
                        back_label = " \u25c0 Back "
                        back_start = badge_end + len("  View: ") + len(view_label) + 2
                        back_end = back_start + len(back_label)
                        if back_start <= rel_x <= back_end:
                            self._switch_to_sessions()
                            return
        except Exception:
            pass
        # Click on menu button opens action menu
        try:
            btn = self.query_one("#menu-button", MenuButton)
            if btn.region.contains(event.screen_x, event.screen_y):
                self._show_action_menu()
        except Exception:
            pass

    def _show_action_menu(self):
        """Show a menu with all available actions for the current view."""
        s = self._current_session()
        if self.view == "detail":
            has_tmux = s and s.id in self.tmux_sids
            is_pinned = s and s.pinned
            pin_label = "Unpin" if is_pinned else "Pin"
            items = [
                ("\u23ce  Resume Session", "launch"),
                ("\u26a1  Tmux Expert", "tmux_expert"),
                ("\u2190  Back to Sessions", "back"),
                ("Tab Switch Panel", "switch_pane"),
                ("", "---"),
                (f"p   {pin_label} Session", "pin"),
                ("t   Set Tag", "tag"),
                ("T   Remove Tag", "remove_tag"),
                ("d   Delete Session", "delete"),
            ]
            if has_tmux:
                items.append(("", "---"))
                items.append(("k   Kill Tmux", "kill_tmux"))
                items.append(("i   Send Input", "send_input"))
            items.extend([
                ("", "---"),
                ("r   Refresh", "refresh"),
                ("S   Rescan All Sessions", "rescan"),
                ("H   Change Theme", "theme"),
                ("P   Profiles", "profiles"),
                ("?   Help", "help"),
                ("    Exit", "exit"),
            ])
        else:
            is_pinned = s and s.pinned
            is_marked = s and s.id in self.marked
            pin_label = "Unpin" if is_pinned else "Pin"
            mark_label = "Unmark" if is_marked else "Mark"
            has_tmux = s and s.id in self.tmux_sids
            items = [
                ("\u23ce  Resume Session", "launch"),
                ("\u26a1  Tmux Expert", "tmux_expert"),
                ("\u2192  Session View", "view"),
                ("", "---"),
                (f"Spc {mark_label} Session", "mark"),
                ("u   Unmark All", "unmark"),
                (f"p   {pin_label} Session", "pin"),
                ("t   Set Tag", "tag"),
                ("T   Remove Tag", "remove_tag"),
                ("", "---"),
                ("n   New Session", "new"),
                ("e   Ephemeral Session", "ephemeral"),
                ("", "---"),
                ("d   Delete Session", "delete"),
                ("D   Delete All Empty", "delete_empty"),
            ]
            if has_tmux:
                items.append(("k   Kill Tmux", "kill_tmux"))
            items.extend([
                ("K   Kill All Tmux", "kill_all_tmux"),
                ("i   Send Input", "send_input"),
                ("", "---"),
                ("s   Cycle Sort", "sort"),
                ("/   Search", "search"),
                ("r   Refresh", "refresh"),
                ("S   Rescan All Sessions", "rescan"),
                ("", "---"),
                ("H   Change Theme", "theme"),
                ("P   Profiles", "profiles"),
                ("?   Help", "help"),
                ("    Exit", "exit"),
            ])

        def on_result(action):
            actions = {
                "launch": self.action_launch,
                "tmux_expert": self._action_tmux_expert,
                "back": self._switch_to_sessions,
                "switch_pane": self.action_switch_pane,
                "view": self._switch_to_detail,
                "new": self.action_new_session,
                "ephemeral": self.action_ephemeral_session,
                "mark": self.action_mark,
                "unmark": self.action_unmark_all,
                "pin": self.action_toggle_pin,
                "tag": self.action_set_tag,
                "remove_tag": self.action_remove_tag,
                "delete": self.action_delete_session,
                "delete_empty": self.action_delete_empty,
                "kill_tmux": self.action_kill_tmux,
                "kill_all_tmux": self.action_kill_all_tmux,
                "send_input": self.action_send_input,
                "sort": self.action_cycle_sort,
                "search": self.action_search,
                "refresh": self.action_refresh,
                "rescan": self.action_rescan,
                "theme": self.action_cycle_theme,
                "profiles": self.action_profiles,
                "help": self.action_help,
                "exit": self.action_quit_confirm,
            }
            fn = actions.get(action)
            if fn:
                fn()

        title = "Detail Actions" if self.view == "detail" else "Session Actions"
        self.push_screen(ContextMenuModal(title, items), on_result)

    def on_key(self, event) -> None:
        """Central key handler — mirrors the curses _handle_input dispatch."""
        # Don't handle keys when a modal screen is active
        if isinstance(self.screen, ModalScreen):
            return

        key = event.key
        event.stop()
        event.prevent_default()
        sl = self.query_one("#session-list", SessionListWidget)

        # ── Global keys ──────────────────────────────────────────
        if key == "ctrl+c":
            self.action_quit_confirm()
            return
        if key in ("question_mark", "?"):
            self.action_help()
            return
        if key == "P":
            self.action_profiles()
            return
        if key == "H":
            self.action_cycle_theme()
            return
        if key == "m":
            self._show_action_menu()
            return
        if key in ("r", "f5"):
            self.action_refresh()
            return
        if key == "S":
            self.action_rescan()
            return
        if key == "escape":
            self.action_escape_action()
            return

        # ── View switching ───────────────────────────────────────
        if key == "right":
            if self.view == "sessions" and self._current_session():
                self._switch_to_detail()
                return
            elif self.view == "detail":
                self.action_launch()
                return
        if key == "left":
            if self.view == "detail":
                self._switch_to_sessions()
            return

        # ── Detail view keys ─────────────────────────────────────
        if self.view == "detail":
            if key == "tab":
                self.action_switch_pane()
            elif key == "enter":
                self.action_launch()
            elif key == "p":
                self.action_toggle_pin()
            elif key == "t":
                self.action_set_tag()
            elif key == "T":
                self.action_remove_tag()
            elif key == "d":
                self.action_delete_session()
            elif key == "k":
                self.action_kill_tmux()
            elif key == "i":
                self.action_send_input()
            elif key == "up":
                self.query_one("#info-scroll" if self.detail_focus == "info" else "#tmux-pane").scroll_up()
            elif key == "down":
                self.query_one("#info-scroll" if self.detail_focus == "info" else "#tmux-pane").scroll_down()
            elif key == "pageup":
                self.query_one("#info-scroll" if self.detail_focus == "info" else "#tmux-pane").scroll_page_up()
            elif key == "pagedown":
                self.query_one("#info-scroll" if self.detail_focus == "info" else "#tmux-pane").scroll_page_down()
            elif key == "g":
                self.query_one("#info-scroll" if self.detail_focus == "info" else "#tmux-pane").scroll_home()
            elif key == "G":
                self.query_one("#info-scroll" if self.detail_focus == "info" else "#tmux-pane").scroll_end()
            return

        # ── Sessions view keys ───────────────────────────────────
        if key == "up":
            if sl.highlighted is not None and sl.highlighted > 0:
                sl.highlighted -= 1
        elif key == "down":
            if sl.highlighted is not None and sl.highlighted < sl.option_count - 1:
                sl.highlighted += 1
        elif key == "g":
            if sl.option_count > 0:
                sl.highlighted = 0
        elif key == "G":
            if sl.option_count > 0:
                sl.highlighted = sl.option_count - 1
        elif key == "pageup":
            if sl.highlighted is not None:
                sl.highlighted = max(0, sl.highlighted - 20)
        elif key == "pagedown":
            if sl.highlighted is not None:
                sl.highlighted = min(sl.option_count - 1, sl.highlighted + 20)
        elif key == "enter":
            self.action_launch()
        elif key == "space":
            self.action_mark()
        elif key == "u":
            self.action_unmark_all()
        elif key == "p":
            self.action_toggle_pin()
        elif key == "t":
            self.action_set_tag()
        elif key == "T":
            self.action_remove_tag()
        elif key == "d":
            self.action_delete_session()
        elif key == "D":
            self.action_delete_empty()
        elif key == "k":
            self.action_kill_tmux()
        elif key == "K":
            self.action_kill_all_tmux()
        elif key == "n":
            self.action_new_session()
        elif key == "e":
            self.action_ephemeral_session()
        elif key == "s":
            self.action_cycle_sort()
        elif key == "i":
            self.action_send_input()
        elif key == "slash":
            self.action_search()

    # -- Actions -----------------------------------------------------------

    def action_quit_confirm(self):
        if isinstance(self.screen, ModalScreen):
            return
        def on_result(confirmed):
            if confirmed:
                self.exit()

        self.push_screen(ConfirmModal("Quit", "Exit CCS?", color_style="normal", default_yes=True), on_result)

    def action_help(self):
        self.push_screen(HelpModal(self.view))

    def action_profiles(self):
        def on_result(result):
            if result is None:
                return
            if result.startswith("activate:"):
                name = result[9:]
                self.active_profile_name = name
                self.mgr.save_active_profile_name(name)
                self._set_status(f"Active profile: {name}")
                self._update_header()
            elif result == "new":
                self._open_profile_editor(None)
            elif result.startswith("edit:"):
                name = result[5:]
                profiles = self.mgr.load_profiles()
                prof = next(
                    (p for p in profiles if p.get("name") == name), None
                )
                if prof:
                    self._open_profile_editor(prof)
            elif result.startswith("delete:"):
                name = result[7:]
                self.mgr.delete_profile(name)
                if self.active_profile_name == name:
                    self.active_profile_name = "default"
                    self.mgr.save_active_profile_name("default")
                self._set_status(f"Deleted profile: {name}")
                self._update_header()

        self.push_screen(
            ProfilesModal(self.mgr, self.active_profile_name), on_result
        )

    def _open_profile_editor(self, profile):
        def on_result(result):
            if result is None:
                return
            # result is a profile dict
            old_name = profile.get("name") if profile else None
            new_name = result.get("name", "")
            if old_name and old_name != new_name:
                self.mgr.delete_profile(old_name)
            self.mgr.save_profile(result)
            self._set_status(f"Saved profile: {new_name}")

        self.push_screen(ProfileEditModal(profile), on_result)

    def _apply_theme(self, short_name: str):
        """Apply a theme by short name and refresh the UI."""
        textual_name = TEXTUAL_THEME_MAP.get(short_name, "ccs-dark")
        self._ccs_theme_name = textual_name
        self.theme = textual_name
        self._rebuild_list()
        self._update_preview()
        self._update_header()

    def action_cycle_theme(self):
        # Find current short name
        current_short = "dark"
        for short, textual_name in TEXTUAL_THEME_MAP.items():
            if textual_name == self._ccs_theme_name:
                current_short = short
                break

        def on_preview(name):
            self._apply_theme(name)

        def on_result(result):
            if result is None:
                return  # already restored by modal
            self._apply_theme(result)
            self.mgr.save_theme(result)
            self._set_status(f"Theme: {result}")

        self.push_screen(
            ThemeModal(current_theme=current_short, on_preview=on_preview),
            on_result,
        )

    def action_refresh(self):
        self._do_refresh(force=True)
        self._set_status("Refreshed session list")

    def action_rescan(self):
        """Full rescan: clear caches and rediscover all Claude sessions."""
        def on_result(text):
            if text and text.strip() == "SCAN":
                prev_count = len(self.sessions)
                prev_ids = {s.id for s in self.sessions}
                self.mgr._scan_cache = None
                try:
                    os.remove(CACHE_FILE)
                except OSError:
                    pass
                self._git_cache.clear()
                self._do_refresh(force=True)
                new_count = len(self.sessions)
                new_ids = {s.id for s in self.sessions}
                added = len(new_ids - prev_ids)
                removed = len(prev_ids - new_ids)
                lines = [f"Sessions found: {new_count}"]
                if added:
                    lines.append(f"New sessions discovered: {added}")
                if removed:
                    lines.append(f"Sessions removed (empty): {removed}")
                if not added and not removed:
                    lines.append("No changes detected")
                detail = "\n".join(lines)
                self._set_status(f"Rescan complete: {new_count} session{'s' if new_count != 1 else ''} found")
                self.push_screen(InfoModal("Rescan Complete", detail))
            elif text is not None:
                self._set_status("Rescan cancelled (type SCAN to confirm)")

        self.push_screen(
            SimpleInputModal(
                "Rescan all Claude sessions?\n\n"
                "This will clear all caches and re-read every session file.\n"
                "Empty sessions will be deleted.\n"
                "Type SCAN to confirm:",
                placeholder="Type SCAN to confirm",
            ),
            on_result,
        )

    def action_cursor_down(self):
        if self.view == "sessions":
            self.query_one(
                "#session-list", SessionListWidget
            ).action_cursor_down()

    def action_cursor_up(self):
        if self.view == "sessions":
            self.query_one(
                "#session-list", SessionListWidget
            ).action_cursor_up()

    def action_cursor_first(self):
        if self.view == "sessions":
            sl = self.query_one("#session-list", SessionListWidget)
            if sl.option_count > 0:
                sl.highlighted = 0

    def action_cursor_last(self):
        if self.view == "sessions":
            sl = self.query_one("#session-list", SessionListWidget)
            if sl.option_count > 0:
                sl.highlighted = sl.option_count - 1

    def action_detail_view(self):
        if self.view == "sessions" and self._current_session():
            self._switch_to_detail()

    def action_sessions_view(self):
        if self.view == "detail":
            self._switch_to_sessions()

    def action_escape_action(self):
        if self.view == "detail":
            self._switch_to_sessions()
            return
        if self.search_query:
            self.search_query = ""
            self._apply_filter()
            self._rebuild_list()
            self._update_preview()
            self._update_header()
            self._set_status("Filter cleared")
            return
        self.action_quit_confirm()

    def action_launch(self):
        s = self._current_session()
        if not s:
            return
        extra = self._active_profile_args()
        label = s.tag or s.label[:40] or s.id[:12]

        def on_env_result(env_text):
            env_vars = env_text if env_text and env_text.strip() else ""
            self._tmux_launch(s, extra, env_vars=env_vars)
            self._do_refresh()

        def on_result(choice):
            if choice is None:
                return
            if choice == "view":
                self._switch_to_detail()
            elif choice == "tmux":
                self._tmux_launch(s, extra)
                self._do_refresh()
            elif choice == "tmux_expert":
                self.push_screen(
                    InputModal(
                        target_name="Environment Variables",
                        subtitle="One per line: KEY=VALUE (optional, Esc to skip)\nNot stored anywhere \u2014 lives only in this tmux session.",
                    ),
                    on_env_result,
                )
            elif choice == "terminal":
                proj_dir = os.path.expanduser(s.project_display) if s.project_display else ""
                self.exit_action = ("resume", s.id, extra, proj_dir)
                self.exit()

        self.push_screen(LaunchModal(label, show_view=(self.view != "detail")), on_result)

    def _action_tmux_expert(self):
        """Launch tmux with env vars prompt (called from menu)."""
        s = self._current_session()
        if not s:
            return
        extra = self._active_profile_args()

        def on_env_result(env_text):
            env_vars = env_text if env_text and env_text.strip() else ""
            self._tmux_launch(s, extra, env_vars=env_vars)
            self._do_refresh()

        self.push_screen(
            InputModal(
                target_name="Environment Variables",
                subtitle="One per line: KEY=VALUE (optional, Esc to skip)\nNot stored anywhere \u2014 lives only in this tmux session.",
            ),
            on_env_result,
        )

    def action_mark(self):
        if self.view != "sessions":
            return
        s = self._current_session()
        if not s:
            return
        if s.id in self.marked:
            self.marked.discard(s.id)
        else:
            self.marked.add(s.id)
        # Move cursor down
        sl = self.query_one("#session-list", SessionListWidget)
        sl.action_cursor_down()
        self._rebuild_list()

    def action_unmark_all(self):
        if self.marked:
            self.marked.clear()
            self._set_status("Cleared all marks")
            self._rebuild_list()

    def action_toggle_pin(self):
        if self.view == "sessions" and self.marked:
            for sid in self.marked:
                self.mgr.toggle_pin(sid)
            self._set_status(
                f"Toggled pin for {len(self.marked)} session(s)"
            )
            self.marked.clear()
            self._do_refresh()
            return
        s = self._current_session()
        if s:
            pinned = self.mgr.toggle_pin(s.id)
            icon = "\u2605 Pinned" if pinned else "Unpinned"
            self._set_status(f"{icon}: {s.tag or s.id[:12]}")
            self._do_refresh()

    def action_set_tag(self):
        s = self._current_session()
        if not s:
            return

        def on_result(tag):
            if tag:
                self.mgr.set_tag(s.id, tag)
                self._set_status(f"Tagged: [{tag[:10]}]")
                self._do_refresh()

        self.push_screen(
            SimpleInputModal("Set Tag", s.tag or "", "Enter tag name"),
            on_result,
        )

    def action_remove_tag(self):
        s = self._current_session()
        if not s:
            return
        if s.tag:
            self.mgr.remove_tag(s.id)
            self._set_status(f"Removed tag from: {s.id[:12]}")
            self._do_refresh()
        else:
            self._set_status("No tag to remove")

    def _kill_tmux_for_session(self, sid):
        """Kill tmux session for a given session ID if it exists."""
        if not HAS_TMUX:
            return
        tmux_name = TMUX_PREFIX + sid
        subprocess.run(["tmux", "kill-session", "-t", tmux_name], capture_output=True)
        self.tmux_sids.discard(sid)

    def action_delete_session(self):
        if self.view == "sessions" and self.marked:
            count = len(self.marked)

            def on_result(text):
                if text and text.strip() == "DELETE":
                    deleted = 0
                    for s in list(self.sessions):
                        if s.id in self.marked:
                            self._kill_tmux_for_session(s.id)
                            self.mgr.delete(s)
                            self._remove_ephemeral_id(s.id)
                            deleted += 1
                    self.marked.clear()
                    self._set_status(f"Deleted {deleted} session(s)")
                    self._do_refresh()
                elif text is not None:
                    self._set_status("Delete cancelled (type DELETE to confirm)")

            self.push_screen(
                SimpleInputModal(
                    f"Delete {count} marked sessions?\n\n"
                    "WARNING: This permanently deletes the Claude session data.\n"
                    "Type DELETE to confirm:",
                    placeholder="Type DELETE to confirm",
                ),
                on_result,
            )
            return
        s = self._current_session()
        if not s:
            return
        label = s.tag or s.label[:40] or s.id[:12]

        def on_result(text):
            if text and text.strip() == "DELETE":
                self._kill_tmux_for_session(s.id)
                self.mgr.delete(s)
                self._remove_ephemeral_id(s.id)
                self._set_status(f"Deleted: {label}")
                if self.view == "detail":
                    self._switch_to_sessions()
                self._do_refresh()
            elif text is not None:
                self._set_status("Delete cancelled (type DELETE to confirm)")

        tmux_warning = ""
        if s.id in self.tmux_sids:
            tmux_warning = "\nThe active tmux session will also be killed."
        self.push_screen(
            SimpleInputModal(
                f"Delete '{label}'?\n\n"
                "WARNING: This permanently deletes the Claude session data.\n"
                "This cannot be recovered." + tmux_warning + "\n"
                "Type DELETE to confirm:",
                placeholder="Type DELETE to confirm",
            ),
            on_result,
        )

    def action_delete_empty(self):
        if self.view != "sessions":
            return
        empty = [
            s for s in self.sessions if not s.first_msg and not s.summary
        ]
        if not empty:
            self._set_status("No empty sessions to delete")
            return
        count = len(empty)

        def on_result(confirmed):
            if confirmed:
                for s in empty:
                    self._kill_tmux_for_session(s.id)
                    self.mgr.delete(s)
                self._set_status(f"Deleted {count} empty session(s)")
                self._do_refresh()

        self.push_screen(
            ConfirmModal("Delete Empty", f"Delete {count} empty sessions?"),
            on_result,
        )

    def action_kill_tmux(self):
        s = self._current_session()
        if not s:
            return
        if not HAS_TMUX:
            self._set_status("tmux is not installed")
            return
        if s.id not in self.tmux_sids:
            self._set_status("No active tmux session for this session")
            return
        tmux_name = TMUX_PREFIX + s.id
        label = s.tag or s.id[:12]

        def on_result(confirmed):
            if confirmed:
                subprocess.run(
                    ["tmux", "kill-session", "-t", tmux_name],
                    capture_output=True,
                )
                self.tmux_sids.discard(s.id)
                self._set_status(f"Killed tmux: {label}")
                self._do_refresh()

        self.push_screen(
            ConfirmModal(
                "Kill Tmux",
                f"Kill tmux session for '{label}'?",
                "The Claude session data is preserved and can be resumed later.",
                color_style="warning",
            ),
            on_result,
        )

    def action_kill_all_tmux(self):
        if self.view != "sessions":
            return
        if not HAS_TMUX or not self.tmux_sids:
            self._set_status("No active tmux sessions")
            return
        count = len(self.tmux_sids)

        def on_result(confirmed):
            if confirmed:
                for sid in list(self.tmux_sids):
                    tmux_name = TMUX_PREFIX + sid
                    subprocess.run(
                        ["tmux", "kill-session", "-t", tmux_name],
                        capture_output=True,
                    )
                self.tmux_sids.clear()
                self._set_status(f"Killed {count} tmux session{'s' if count != 1 else ''}")
                self._do_refresh()

        self.push_screen(
            ConfirmModal(
                "Kill All Tmux",
                f"Kill all {count} active tmux session{'s' if count != 1 else ''}?",
                "Session data is preserved and can be resumed later.",
                color_style="warning",
            ),
            on_result,
        )

    def action_new_session(self):
        if self.view != "sessions":
            return

        def on_path(path, name):
            path = path.strip() if path else ""
            if path and not os.path.isdir(os.path.expanduser(path)):
                self._set_status(f"Directory not found: {path}")
                return
            use_tmux = self._get_use_tmux()
            if use_tmux:
                if not HAS_TMUX:
                    self._set_status("tmux is not installed")
                    return
                extra = self._active_profile_args()
                cwd = os.path.expanduser(path) if path else None
                self._tmux_launch_new(name, extra, cwd=cwd)
                self._do_refresh()
            else:
                self.exit_action = ("new", name)
                self.exit()

        def on_name(name):
            if name is None:
                return
            name = name.strip()
            if self._get_use_tmux():
                self.push_screen(
                    PathInputModal("Project Path", os.getcwd(), "Path (Tab to autocomplete)"),
                    lambda path: on_path(path, name),
                )
            else:
                self.exit_action = ("new", name)
                self.exit()

        self.push_screen(
            SimpleInputModal("New Session Name", "", "Enter session name (optional)"),
            on_name,
        )

    def action_ephemeral_session(self):
        if self.view != "sessions":
            return
        use_tmux = self._get_use_tmux()
        if not use_tmux:
            self.exit_action = ("tmp",)
            self.exit()
            return
        if not HAS_TMUX:
            self._set_status("tmux is not installed")
            return

        def on_path(path):
            path = path.strip() if path else ""
            if path and not os.path.isdir(os.path.expanduser(path)):
                self._set_status(f"Directory not found: {path}")
                return
            extra = self._active_profile_args()
            cwd = os.path.expanduser(path) if path else None
            self._tmux_launch_ephemeral(extra, cwd=cwd)
            self._do_refresh()

        self.push_screen(
            PathInputModal("Project Path", os.getcwd(), "Path (Tab to autocomplete)"),
            on_path,
        )

    def action_search(self):
        if self.view != "sessions":
            return

        def on_result(query):
            if query is None:
                return
            self.search_query = query.strip()
            self._apply_filter()
            self._rebuild_list()
            self._update_preview()
            self._update_header()
            if self.search_query:
                self._set_status(f"Filter: {self.search_query} ({len(self.filtered)} matches)")
            else:
                self._set_status("Filter cleared")

        self.push_screen(
            SimpleInputModal("Search", self.search_query, "Filter sessions..."),
            on_result,
        )

    def action_cycle_sort(self):
        if self.view != "sessions":
            return
        modes = ["date", "name", "project", "tag", "messages", "tmux"]
        idx = (
            modes.index(self.sort_mode) if self.sort_mode in modes else 0
        )
        self.sort_mode = modes[(idx + 1) % len(modes)]
        self._do_refresh()
        labels = {
            "date": "Date",
            "name": "Name",
            "project": "Project",
            "tag": "Tag",
            "messages": "Messages",
            "tmux": "Tmux",
        }
        self._set_status(f"Sort: {labels[self.sort_mode]}")

    def action_send_input(self):
        if self.view != "detail":
            return
        s = self._current_session()
        if not s:
            return
        if not HAS_TMUX:
            self._set_status("tmux is not installed")
            return
        if s.id not in self.tmux_sids:
            self._set_status("No active tmux session")
            return
        tmux_name = TMUX_PREFIX + s.id

        def on_result(text):
            if text:
                self._tmux_send_text(tmux_name, text)
                self._set_status(f"Sent to {tmux_name}")
                self.tmux_pane_ts.pop(s.id, None)
                self.tmux_pane_cache.pop(s.id, None)

        self.push_screen(InputModal(tmux_name), on_result)

    def _set_detail_focus(self, panel: str):
        """Set the focused panel in detail view ('info' or 'tmux')."""
        if self.view != "detail" or self.detail_focus == panel:
            return
        self.detail_focus = panel
        if panel == "tmux":
            self.query_one("#info-scroll").remove_class("focused")
            self.query_one("#tmux-pane").add_class("focused")
            self.query_one("#tmux-pane").focus()
        else:
            self.query_one("#tmux-pane").remove_class("focused")
            self.query_one("#info-scroll").add_class("focused")
            self.query_one("#info-scroll").focus()

    def action_switch_pane(self):
        if self.view != "detail":
            return
        self._set_detail_focus("tmux" if self.detail_focus == "info" else "info")
# ── CLI helpers ───────────────────────────────────────────────────────


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
        return build_args_from_profile(prof)
    return []


# ── CLI commands ─────────────────────────────────────────────────────


def cmd_help():
    print("""\033[1;36m◆ ccs — Claude Code Session Manager\033[0m

\033[1mUsage:\033[0m
  ccs                                    Interactive TUI
  ccs list                               List all sessions
  ccs scan [-n|--dry-run]                Rescan all Claude sessions
  ccs resume <id|tag> [-p <profile>]     Resume session
  ccs resume <id|tag> --claude <opts>    Resume with raw claude options
  ccs new <name>                         New named session
  ccs new -e [name]                      Ephemeral session (auto-deleted on exit)
  ccs pin <id|tag>                       Pin a session
  ccs unpin <id|tag>                     Unpin a session
  ccs tag <id|tag> <tag>                 Set tag on session
  ccs tag rename <oldtag> <newtag>       Rename a tag
  ccs untag <id|tag>                     Remove tag from session
  ccs delete <id|tag>                    Delete a session
  ccs delete --empty                     Delete all empty sessions
  ccs info <id|tag>                      Show session details
  ccs search <query>                     Search sessions by text
  ccs export <id|tag>                    Export session as markdown
  ccs profile list                       List profiles
  ccs profile info <name>                Show profile details
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

\033[2mPress ? in the TUI for keybindings help.\033[0m""")


def cmd_scan(mgr: SessionManager, dry_run: bool = False):
    """Full rescan: clear caches and rediscover all Claude sessions."""
    if dry_run:
        cmd_scan_dry_run(mgr)
        return
    print("\033[33mThis will clear all caches and re-read every session file.\033[0m")
    print("\033[33mEmpty sessions will be deleted.\033[0m")
    print("Type SCAN to confirm: ", end="", flush=True)
    try:
        answer = input().strip()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer != "SCAN":
        print("Cancelled.")
        return
    mgr._scan_cache = None
    try:
        os.remove(CACHE_FILE)
    except OSError:
        pass
    sessions = mgr.scan(force=True)
    print(f"\033[1;36m◆\033[0m Rescan complete: {len(sessions)} session{'s' if len(sessions) != 1 else ''} found")


def cmd_scan_dry_run(mgr: SessionManager):
    """Show what scan would find and clean up, without making changes."""
    meta = mgr._load_meta()
    proj_paths = mgr._load_project_paths()
    pattern = str(PROJECTS_DIR / "*" / "*.jsonl")
    seen_sids = set()
    keep = []
    delete_empty = []
    delete_missing_proj = []

    for jp in glob.glob(pattern):
        sid = os.path.basename(jp).replace(".jsonl", "")
        seen_sids.add(sid)
        praw = os.path.basename(os.path.dirname(jp))
        pdisp = proj_paths.get(sid) or mgr._decode_proj_fallback(praw, mgr.user)
        proj_path = os.path.expanduser(pdisp) if pdisp else ""

        # Count messages
        msg_count = 0
        summary = ""
        fm = ""
        try:
            with open(jp, "r", errors="replace") as f:
                for ln in f:
                    try:
                        d = json.loads(ln)
                    except Exception:
                        continue
                    t = d.get("type")
                    if t == "summary":
                        summary = d.get("summary", "") or summary
                    elif t in ("user", "assistant"):
                        msg_count += 1
                        if t == "user" and not fm:
                            fm = mgr._extract_text(d.get("message", {}))[:80]
        except Exception:
            pass

        tag = meta.get(sid, {}).get("tag", "")
        label = tag or summary or fm or sid[:12]

        if msg_count == 0:
            delete_empty.append((sid, pdisp, label))
        elif proj_path and not os.path.isdir(proj_path):
            delete_missing_proj.append((sid, pdisp, label))
        else:
            keep.append((sid, pdisp, label, msg_count))

    orphaned_meta = [sid for sid in meta if sid not in seen_sids]

    print(f"\033[1;36m◆\033[0m Dry run — no changes made\n")
    print(f"  \033[1;32mKeep:\033[0m {len(keep)} sessions")
    for sid, pdisp, label, mc in keep:
        print(f"    {sid[:12]}  {pdisp[:30]:<30s}  {mc:>4d}m  {label[:40]}")

    if delete_empty:
        print(f"\n  \033[1;31mDelete (empty):\033[0m {len(delete_empty)} sessions")
        for sid, pdisp, label in delete_empty:
            print(f"    {sid[:12]}  {pdisp[:30]:<30s}  {label[:40]}")

    if delete_missing_proj:
        print(f"\n  \033[1;31mDelete (missing project dir):\033[0m {len(delete_missing_proj)} sessions")
        for sid, pdisp, label in delete_missing_proj:
            print(f"    {sid[:12]}  {pdisp[:30]:<30s}  {label[:40]}")

    if orphaned_meta:
        print(f"\n  \033[1;33mOrphaned metadata:\033[0m {len(orphaned_meta)} entries")
        for sid in orphaned_meta:
            tag = meta[sid].get("tag", "")
            print(f"    {sid[:12]}  {tag or '-'}")

    if not delete_empty and not delete_missing_proj and not orphaned_meta:
        print("\n  Nothing to clean up.")


def cmd_list(mgr: SessionManager):
    sessions = mgr.scan()
    if not sessions:
        print("No sessions found.")
        return
    max_tag_w = 0
    for s in sessions:
        if s.tag:
            tw = len(s.tag) + 3
            if tw > max_tag_w:
                max_tag_w = tw
    tag_hdr = f"{'Tag':<{max_tag_w}}" if max_tag_w else ""
    print(f"  \033[2m  {tag_hdr}{'Modified':<18s}{'ID':<14s}{'Project':<24s}  Description\033[0m")
    for s in sessions:
        pin = "★ " if s.pinned else "  "
        tag = f"[{s.tag}]" if s.tag else ""
        tag_col = f"{tag:<{max_tag_w}}" if max_tag_w else ""
        label = s.label[:60]
        print(f"  {pin}{tag_col}{s.ts}  {s.id[:12]}  {s.project_display[:24]:<24s}  {label}")


def cmd_resume(mgr: SessionManager, query: str, profile_name: Optional[str],
               claude_args: Optional[List[str]]):
    s = _find_session(mgr, query)
    if claude_args is not None:
        extra = claude_args
    else:
        extra = _get_profile_extra(mgr, profile_name)
    opts = f" {' '.join(extra)}" if extra else ""
    print(f"\033[1;36m◆\033[0m Resuming session \033[2m({s.id[:8]}…)\033[0m{opts}")
    proj_dir = os.path.expanduser(s.project_display) if s.project_display else ""
    if proj_dir and os.path.isdir(proj_dir):
        os.chdir(proj_dir)
    cmd = ["claude", "--resume", s.id] + extra
    os.execvp("claude", cmd)


def cmd_new(mgr: SessionManager, name: str, extra: List[str], ephemeral: bool = False):
    uid = str(uuid_mod.uuid4())
    if ephemeral:
        mgr._set_meta(uid, ephemeral=True)
        if name:
            mgr._set_meta(uid, tag=name, ephemeral=True)
        print(f"\033[1;36m◆\033[0m Starting ephemeral session \033[2m({uid[:8]}…)\033[0m")
        cmd = ["claude", "--session-id", uid] + extra
        try:
            subprocess.run(cmd)
        except KeyboardInterrupt:
            pass
        finally:
            mgr.purge_ephemeral()
    else:
        mgr._set_meta(uid, tag=name)
        print(f"\033[1;36m◆\033[0m Starting named session: "
              f"\033[1;32m{name}\033[0m \033[2m({uid[:8]}…)\033[0m")
        cmd = ["claude", "--session-id", uid] + extra
        os.execvp("claude", cmd)


def cmd_pin(mgr: SessionManager, query: str):
    s = _find_session(mgr, query)
    if not mgr._get_meta(s.id).get("pinned"):
        mgr._set_meta(s.id, pinned=True)
        print(f"\u2605 Pinned: {s.tag or s.id[:12]}")
    else:
        print(f"Already pinned: {s.tag or s.id[:12]}")


def cmd_unpin(mgr: SessionManager, query: str):
    s = _find_session(mgr, query)
    if mgr._get_meta(s.id).get("pinned"):
        mgr._set_meta(s.id, pinned=False)
        print(f"Unpinned: {s.tag or s.id[:12]}")
    else:
        print(f"Not pinned: {s.tag or s.id[:12]}")


def cmd_tag(mgr: SessionManager, query: str, tag: str):
    s = _find_session(mgr, query)
    mgr.set_tag(s.id, tag)
    print(f"Tagged [{tag}]: {s.id[:12]}")


def cmd_tag_rename(mgr: SessionManager, old_tag: str, new_tag: str):
    meta = mgr._load_meta()
    matches = [sid for sid, m in meta.items() if m.get("tag") == old_tag]
    if not matches:
        print(f"\033[31mNo session with tag '{old_tag}'\033[0m")
        sys.exit(1)
    for sid in matches:
        mgr.set_tag(sid, new_tag)
    if len(matches) == 1:
        print(f"Renamed tag [{old_tag}] → [{new_tag}]: {matches[0][:12]}")
    else:
        print(f"Renamed tag [{old_tag}] → [{new_tag}] on {len(matches)} sessions")


def cmd_untag(mgr: SessionManager, query: str):
    s = _find_session(mgr, query)
    if s.tag:
        mgr.remove_tag(s.id)
        print(f"Removed tag from: {s.id[:12]}")
    else:
        print(f"No tag on: {s.id[:12]}")


def _cli_kill_tmux(sid):
    """Kill tmux session for a session ID (CLI helper)."""
    if HAS_TMUX:
        tmux_name = TMUX_PREFIX + sid
        subprocess.run(["tmux", "kill-session", "-t", tmux_name], capture_output=True)


def cmd_delete_session(mgr: SessionManager, query: str):
    s = _find_session(mgr, query)
    label = s.tag or s.label[:40] or s.id[:12]
    print(f"\033[1;31mDelete '{label}'?\033[0m")
    print("\033[33mWARNING: This permanently deletes the Claude session data and cannot be recovered.\033[0m")
    print("Type DELETE to confirm: ", end="", flush=True)
    try:
        answer = input().strip()
    except (EOFError, KeyboardInterrupt):
        answer = ""
    if answer == "DELETE":
        _cli_kill_tmux(s.id)
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
            _cli_kill_tmux(s.id)
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
    ]
    if not matches:
        print(f"No sessions matching '{query}'.")
        return
    print(f"{len(matches)} match{'es' if len(matches) != 1 else ''}:")
    max_tag_w = 0
    for s in matches:
        if s.tag:
            tw = len(s.tag) + 3
            if tw > max_tag_w:
                max_tag_w = tw
    tag_hdr = f"{'Tag':<{max_tag_w}}" if max_tag_w else ""
    print(f"  \033[2m  {tag_hdr}{'Modified':<18s}{'ID':<14s}{'Project':<24s}  Description\033[0m")
    for s in matches:
        pin = "★ " if s.pinned else "  "
        tag = f"[{s.tag}]" if s.tag else ""
        tag_col = f"{tag:<{max_tag_w}}" if max_tag_w else ""
        label = s.label[:60]
        print(f"  {pin}{tag_col}{s.ts}  {s.id[:12]}  {s.project_display[:24]:<24s}  {label}")


def cmd_profile_list(mgr: SessionManager):
    profiles = mgr.load_profiles()
    active = mgr.load_active_profile_name()
    if not profiles:
        print("No profiles.")
        return
    for p in profiles:
        name = p.get("name", "?")
        marker = " *" if name == active else "  "
        summary = profile_summary(p)
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


def cmd_profile_info(mgr: SessionManager, name: str):
    profiles = mgr.load_profiles()
    profile = next((p for p in profiles if p.get("name") == name), None)
    if not profile:
        print(f"\033[31mProfile '{name}' not found.\033[0m")
        sys.exit(1)
    active = mgr.load_active_profile_name()
    active_str = " (active)" if name == active else ""
    print(f"\033[1;36m◆\033[0m Profile: \033[1m{name}\033[0m{active_str}")
    print(f"  Mode:            {'tmux' if profile.get('tmux', True) else 'terminal'}")
    model = profile.get("model", "") or "default"
    for display_name, mid in MODELS:
        if mid == profile.get("model", ""):
            model = display_name
            break
    print(f"  Model:           {model}")
    perm = profile.get("permission_mode", "") or "default"
    print(f"  Permissions:     {perm}")
    flags = profile.get("flags", [])
    if flags:
        print(f"  Flags:           {' '.join(flags)}")
    sp = profile.get("system_prompt", "").strip()
    if sp:
        display = sp[:80] + ("..." if len(sp) > 80 else "")
        print(f"  System prompt:   {display}")
    tools = profile.get("tools", "").strip()
    if tools:
        print(f"  Tools:           {tools}")
    mcp = profile.get("mcp_config", "").strip()
    if mcp:
        print(f"  MCP config:      {mcp}")
    custom = profile.get("custom_args", "").strip()
    if custom:
        print(f"  Custom args:     {custom}")
    expert = profile.get("expert_args", "").strip()
    if expert:
        print(f"  Expert args:     {expert}")
    args = build_args_from_profile(profile)
    if args:
        print(f"  \033[2mCLI: claude {' '.join(args)}\033[0m")


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


def cmd_info(mgr: SessionManager, query: str):
    s = _find_session(mgr, query)
    ts = datetime.datetime.fromtimestamp(s.mtime).strftime("%Y-%m-%d %H:%M")
    pinned_str = " (pinned)" if s.pinned else ""
    print(f"\033[1;36m◆\033[0m Session: \033[1m{s.id}\033[0m{pinned_str}")
    if s.tag:
        print(f"  Tag:             {s.tag}")
    print(f"  Project:         {s.project_display}")
    print(f"  Modified:        {ts} ({s.age})")
    print(f"  Messages:        {s.msg_count}")
    if s.summary:
        print(f"  Summary:         {s.summary}")
    if s.first_msg:
        print(f"  First message:   {s.first_msg}")
    if s.last_msg and s.last_msg != s.first_msg:
        print(f"  Last message:    {s.last_msg}")
    print(f"  File:            {s.path}")


def cmd_export(mgr: SessionManager, query: str):
    s = _find_session(mgr, query)
    ts = datetime.datetime.fromtimestamp(s.mtime).strftime("%Y-%m-%d %H:%M")
    print(f"# Session: {s.label}")
    print(f"- **ID:** `{s.id}`")
    if s.tag:
        print(f"- **Tag:** {s.tag}")
    print(f"- **Project:** {s.project_display}")
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


def _list_ccs_tmux_names():
    """Return list of tmux session names starting with ccs- prefix."""
    try:
        r = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=2,
        )
        if r.returncode != 0:
            return []
        return [n.strip() for n in r.stdout.strip().split("\n")
                if n.strip().startswith(TMUX_PREFIX)]
    except Exception:
        return []


def cmd_tmux_list(mgr: SessionManager):
    names = _list_ccs_tmux_names()
    if not names:
        print("No active ccs tmux sessions.")
        return
    for name in sorted(names):
        sid = name[len(TMUX_PREFIX):]
        meta = mgr._get_meta(sid)
        tag = meta.get("tag", "")
        label = f"  {name}"
        if tag:
            label += f"  tag={tag}"
        print(label)


def cmd_tmux_attach(mgr: SessionManager, name: str):
    if not HAS_TMUX:
        print("\033[31mtmux is not installed.\033[0m")
        sys.exit(1)
    rc = subprocess.run(["tmux", "has-session", "-t", name], capture_output=True).returncode
    if rc != 0:
        print(f"\033[31mNo tmux session named '{name}'.\033[0m")
        sys.exit(1)
    os.execvp("tmux", ["tmux", "attach-session", "-t", name])


def cmd_tmux_kill(mgr: SessionManager, name: str):
    if not HAS_TMUX:
        print("\033[31mtmux is not installed.\033[0m")
        sys.exit(1)
    rc = subprocess.run(["tmux", "has-session", "-t", name], capture_output=True).returncode
    if rc != 0:
        print(f"\033[31mNo tmux session named '{name}'.\033[0m")
        sys.exit(1)
    subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)
    print(f"Killed tmux session: {name}")


def cmd_tmux_kill_all(mgr: SessionManager):
    if not HAS_TMUX:
        print("\033[31mtmux is not installed.\033[0m")
        sys.exit(1)
    names = _list_ccs_tmux_names()
    if not names:
        print("No active ccs tmux sessions to kill.")
        return
    for name in names:
        subprocess.run(["tmux", "kill-session", "-t", name], capture_output=True)
    print(f"Killed {len(names)} tmux session{'s' if len(names) != 1 else ''}.")


# ── Main ─────────────────────────────────────────────────────────────


def main():
    args = sys.argv[1:]

    mgr = SessionManager()
    mgr.purge_ephemeral()
    mgr.scan(force=True)  # clean up empty sessions on startup

    if not args:
        # Launch Textual TUI
        app = CCSApp()
        app.run()
        action = app.exit_action
        if action is None:
            return

        if action[0] == "resume":
            _, sid, extra, proj_dir = action
            cmd = ["claude", "--resume", sid] + extra
            opts = f" {' '.join(extra)}" if extra else ""
            print(f"\033[1;36m◆\033[0m Resuming session \033[2m({sid[:8]}…)\033[0m{opts}")
            if proj_dir and os.path.isdir(proj_dir):
                os.chdir(proj_dir)
            os.execvp("claude", cmd)

        elif action[0] == "new":
            _, name = action
            cmd_new(mgr, name, [])

        elif action[0] == "tmp":
            cmd_new(mgr, "", [], ephemeral=True)

        return

    verb = args[0]

    if verb in ("help", "-h", "--help"):
        cmd_help()

    elif verb == "list":
        cmd_list(mgr)

    elif verb == "scan":
        dry = "--dry-run" in args[1:] or "-n" in args[1:]
        cmd_scan(mgr, dry_run=dry)

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
        ephemeral = False
        rest = args[1:]
        if "-e" in rest:
            ephemeral = True
            rest.remove("-e")
        if "--ephemeral" in rest:
            ephemeral = True
            rest.remove("--ephemeral")
        if not rest and not ephemeral:
            print("\033[31mUsage: ccs new <name> | ccs new -e [name]\033[0m")
            sys.exit(1)
        name = rest[0] if rest else ""
        cmd_new(mgr, name, rest[1:], ephemeral=ephemeral)

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

    elif verb == "info":
        if len(args) < 2:
            print("\033[31mUsage: ccs info <id|tag>\033[0m")
            sys.exit(1)
        cmd_info(mgr, args[1])

    elif verb == "export":
        if len(args) < 2:
            print("\033[31mUsage: ccs export <id|tag>\033[0m")
            sys.exit(1)
        cmd_export(mgr, args[1])

    elif verb == "profile":
        if len(args) < 2:
            print("\033[31mUsage: ccs profile list|info|set|new|delete\033[0m")
            sys.exit(1)
        sub = args[1]
        if sub == "list":
            cmd_profile_list(mgr)
        elif sub == "info":
            if len(args) < 3:
                print("\033[31mUsage: ccs profile info <name>\033[0m")
                sys.exit(1)
            cmd_profile_info(mgr, args[2])
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
