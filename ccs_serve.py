#!/usr/bin/env python3
"""
ccs_serve — WebSocket server for remote ccs session management.

Usage:
    ccs serve [--port 7433] [--bind 0.0.0.0] [--debug]
    ccs serve pair                   # Generate a new pairing code
    ccs serve clients                # List paired clients
    ccs serve revoke <client_id>     # Revoke a client
    ccs serve revoke --all           # Revoke all clients
"""

import asyncio
import datetime
import fcntl
import json
import os
import pty
import re
import shlex
import signal
import ssl
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path
from typing import Dict, Optional

import bcrypt
import websockets
import websockets.server

from ccs_protocol import (
    DEFAULT_PORT, CMD_AUTH, CMD_PAIR, CMD_REFRESH, CMD_SCAN, CMD_INFO,
    CMD_ATTACH, CMD_NEW, CMD_KILL, CMD_RESIZE, CMD_PING,
    EVT_DETACHED, EVT_ERROR,
    PAIR_CODE_EXPIRY, REFRESH_TOKEN_EXPIRY,
    CLIENTS_FILE, CCS_DIR,
    generate_pair_code, generate_client_id, create_access_token,
    create_refresh_token, verify_access_token, load_or_create_secret,
    ensure_tls_certs, cert_fingerprint, error_msg, ok_msg,
)

# ── Logging ──────────────────────────────────────────────────────────────

_debug = False  # Set via --debug flag


def _ts() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S")


def log(msg: str):
    """Always-on activity log (connections, auth, commands)."""
    print(f"  \033[2m{_ts()}\033[0m  {msg}")


def debug(msg: str):
    """Verbose log, only shown with --debug."""
    if _debug:
        print(f"  \033[2m{_ts()}\033[0m  \033[2m{msg}\033[0m")


# ── Input validation ─────────────────────────────────────────────────────

# Session IDs are UUIDs (hex + dashes) or opencode numeric IDs.
_SAFE_SID_RE = re.compile(r'^[a-zA-Z0-9_-]{1,200}$')

# CLI args: letters, digits, dashes, dots, underscores, colons, equals,
# slashes, tildes, @, plus, commas.  No shell metacharacters (; | & $ ` etc).
_SAFE_ARG_RE = re.compile(r'^[a-zA-Z0-9_./:=~@+,-]+$')

# Allowed CLI names — reject anything else.
_ALLOWED_CLIS = frozenset({"claude", "codex", "opencode"})


def _valid_session_id(sid) -> bool:
    """Validate session ID: alphanumeric, dashes, underscores only."""
    return isinstance(sid, str) and bool(_SAFE_SID_RE.match(sid))


def _valid_cli(cli: str) -> bool:
    """Validate CLI name against the allowlist."""
    return cli in _ALLOWED_CLIS


def _sanitize_cwd(cwd: Optional[str]) -> Optional[str]:
    """Validate and resolve cwd, restricting to real directories under $HOME."""
    if not cwd:
        return None
    # Expand ~ to the server user's home
    if cwd.startswith("~"):
        cwd = str(Path.home()) + cwd[1:]
    try:
        resolved = Path(cwd).resolve(strict=False)
    except (ValueError, OSError):
        return None
    home = Path.home().resolve()
    # Must be under $HOME
    if resolved != home and home not in resolved.parents:
        return None
    if not resolved.is_dir():
        return None
    return str(resolved)


# ── Pairing state ────────────────────────────────────────────────────────

_pair_codes: Dict[str, float] = {}  # code -> expiry timestamp


def new_pair_code() -> str:
    """Generate and register a new pairing code."""
    code = generate_pair_code()
    _pair_codes[code] = time.time() + PAIR_CODE_EXPIRY
    return code


def validate_pair_code(code: str) -> bool:
    """Check if a pairing code is valid and consume it."""
    expiry = _pair_codes.pop(code, None)
    if expiry is None:
        return False
    return time.time() < expiry


# ── Client store ─────────────────────────────────────────────────────────

def _load_clients() -> dict:
    if CLIENTS_FILE.exists():
        try:
            return json.loads(CLIENTS_FILE.read_text())
        except Exception:
            return {}
    return {}


def _save_clients(clients: dict):
    CCS_DIR.mkdir(parents=True, exist_ok=True)
    CLIENTS_FILE.write_text(json.dumps(clients, indent=2))
    CLIENTS_FILE.chmod(0o600)


def register_client(client_id: str, refresh_token: str, name: str = "") -> None:
    """Register a paired client with hashed refresh token."""
    clients = _load_clients()
    hashed = bcrypt.hashpw(refresh_token.encode(), bcrypt.gensalt()).decode()
    clients[client_id] = {
        "refresh_hash": hashed,
        "name": name,
        "created": int(time.time()),
        "last_seen": int(time.time()),
    }
    _save_clients(clients)


def verify_refresh(client_id: str, refresh_token: str) -> bool:
    """Verify a refresh token for a client."""
    clients = _load_clients()
    client = clients.get(client_id)
    if not client:
        return False
    stored_hash = client.get("refresh_hash", "")
    if not stored_hash:
        return False
    if not bcrypt.checkpw(refresh_token.encode(), stored_hash.encode()):
        return False
    # Update last_seen
    client["last_seen"] = int(time.time())
    _save_clients(clients)
    return True


def revoke_client(client_id: str) -> bool:
    """Revoke a client's access."""
    clients = _load_clients()
    if client_id in clients:
        del clients[client_id]
        _save_clients(clients)
        return True
    return False


def revoke_all_clients() -> int:
    """Revoke all clients. Returns count revoked."""
    clients = _load_clients()
    count = len(clients)
    _save_clients({})
    return count


def list_clients() -> list:
    """List all registered clients."""
    clients = _load_clients()
    out = []
    for cid, info in clients.items():
        out.append({
            "client_id": cid,
            "name": info.get("name", ""),
            "created": info.get("created", 0),
            "last_seen": info.get("last_seen", 0),
        })
    return out


# ── Session scanning (reuses ccs SessionManager) ────────────────────────

def scan_sessions_direct() -> list:
    """Scan sessions directly using SessionManager without textual."""
    sessions = []
    try:
        # Import only the parts we need
        import dataclasses
        import importlib.util

        ccs_path = Path(__file__).parent / "ccs.py"

        # We can't easily import ccs.py without textual, so we do a lightweight
        # scan by reading the DB/files directly. This mirrors what ccs does.
        from ccs_protocol import CCS_DIR

        home = Path.home()

        # Claude sessions
        projects_dir = home / ".claude" / "projects"
        if projects_dir.exists():
            for jsonl in projects_dir.rglob("*.jsonl"):
                sid = jsonl.stem
                # Skip non-UUID filenames and subagent sessions
                if len(sid) < 30 or sid.startswith("agent-"):
                    continue
                mtime = jsonl.stat().st_mtime
                # Read first/last user messages
                first_msg = ""
                last_msg = ""
                try:
                    with open(jsonl, "r", errors="replace") as f:
                        for ln in f:
                            try:
                                d = json.loads(ln)
                            except Exception:
                                continue
                            msg_type = d.get("type", "")
                            if msg_type == "user":
                                content = d.get("message", {}).get("content", "")
                                if isinstance(content, list):
                                    content = " ".join(
                                        c.get("text", "") for c in content
                                        if isinstance(c, dict) and c.get("type") == "text"
                                    )
                                if not first_msg:
                                    first_msg = str(content)[:120].replace("\n", " ")
                                last_msg = str(content)[:120].replace("\n", " ")
                except Exception:
                    pass

                # Derive project path from directory structure
                proj_raw = str(jsonl.parent.name)
                proj_display = proj_raw.replace("-", "/")
                if proj_display.startswith("/"):
                    proj_display = proj_display.replace(str(home), "~", 1)

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

        # Codex sessions
        codex_home = Path(os.environ.get("CODEX_HOME", "").split(",")[0] or str(home / ".codex"))
        for db_path in sorted(codex_home.glob("state_*.sqlite"), reverse=True):
            try:
                conn = sqlite3.connect(str(db_path), timeout=5)
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT id, title, first_user_message, cwd, updated_at, "
                    "rollout_path FROM threads WHERE archived = 0"
                ).fetchall()
                for row in rows:
                    rollout = row["rollout_path"] or ""
                    if rollout and not os.path.exists(rollout):
                        continue
                    cwd = row["cwd"] or ""
                    pdisp = cwd.replace(str(home), "~") if cwd else ""
                    fm = (row["first_user_message"] or "")[:120].replace("\n", " ")
                    sessions.append({
                        "id": row["id"],
                        "cli": "codex",
                        "summary": row["title"] or "",
                        "first_msg": fm,
                        "last_msg": fm,
                        "project": pdisp,
                        "mtime": row["updated_at"] or 0,
                        "kind": "primary",
                    })
                conn.close()
            except Exception:
                pass
            break  # Only use the first (newest) DB

        # opencode sessions
        opencode_db = home / ".local" / "share" / "opencode" / "opencode.db"
        if opencode_db.exists():
            try:
                conn = sqlite3.connect(str(opencode_db), timeout=5)
                conn.row_factory = sqlite3.Row
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()}
                if "session" in tables and "message" in tables:
                    cols = {r[1] for r in conn.execute("PRAGMA table_info(session)").fetchall()}
                    created_col = "time_created" if "time_created" in cols else "created_at" if "created_at" in cols else "NULL"
                    updated_col = "time_updated" if "time_updated" in cols else "updated_at" if "updated_at" in cols else created_col
                    dir_col = "directory" if "directory" in cols else "NULL"

                    rows = conn.execute(
                        f"SELECT s.id, s.title, s.{dir_col} as directory, "
                        f"COUNT(m.id) as msg_count, "
                        f"s.{updated_col} as updated_at "
                        f"FROM session s LEFT JOIN message m ON m.session_id = s.id "
                        f"GROUP BY s.id"
                    ).fetchall()

                    # Get first user messages from part table
                    first_msgs = {}
                    if "part" in tables:
                        fm_rows = conn.execute(
                            "SELECT p.session_id, p.data FROM part p "
                            "INNER JOIN message m ON m.id = p.message_id "
                            "WHERE json_extract(m.data, '$.role') = 'user' "
                            "ORDER BY p.rowid"
                        ).fetchall()
                        for fr in fm_rows:
                            sid = fr["session_id"]
                            if sid in first_msgs:
                                continue
                            try:
                                pd = json.loads(fr["data"]) if fr["data"] else {}
                                if pd.get("type") == "text" and pd.get("text"):
                                    first_msgs[sid] = pd["text"]
                            except Exception:
                                pass

                    for row in rows:
                        if (row["msg_count"] or 0) == 0:
                            continue
                        sid = str(row["id"])
                        cwd = row["directory"] or ""
                        pdisp = cwd.replace(str(home), "~") if cwd else ""
                        fm = first_msgs.get(sid, row["title"] or "")[:120].replace("\n", " ")
                        mtime = row["updated_at"] or 0
                        if mtime > 1e12:
                            mtime = mtime / 1000.0
                        sessions.append({
                            "id": sid,
                            "cli": "opencode",
                            "summary": row["title"] or "",
                            "first_msg": fm,
                            "last_msg": fm,
                            "project": pdisp,
                            "mtime": mtime,
                            "kind": "primary",
                        })
                conn.close()
            except Exception:
                pass

    except Exception:
        pass

    return sessions


# ── Message reading ──────────────────────────────────────────────────────

def read_session_messages(session_id: str, cli: str) -> list:
    """Read messages for a session. Returns list of {role, content, timestamp}."""
    # Defense-in-depth: reject session IDs with glob/path metacharacters.
    if not _SAFE_SID_RE.match(session_id):
        return []
    msgs = []
    home = Path.home()

    if cli == "claude":
        projects_dir = home / ".claude" / "projects"
        for jsonl in projects_dir.rglob(f"{session_id}.jsonl"):
            try:
                with open(jsonl, "r", errors="replace") as f:
                    for ln in f:
                        try:
                            d = json.loads(ln)
                        except Exception:
                            continue
                        msg_type = d.get("type", "")
                        if msg_type in ("user", "assistant"):
                            content = d.get("message", {}).get("content", "")
                            if isinstance(content, list):
                                content = " ".join(
                                    c.get("text", "") for c in content
                                    if isinstance(c, dict) and c.get("type") == "text"
                                )
                            msgs.append({
                                "role": msg_type,
                                "content": str(content)[:2000],
                                "timestamp": d.get("timestamp", ""),
                            })
            except Exception:
                pass
            break

    elif cli == "codex":
        codex_home = Path(os.environ.get("CODEX_HOME", "").split(",")[0] or str(home / ".codex"))
        for db_path in sorted(codex_home.glob("state_*.sqlite"), reverse=True):
            try:
                conn = sqlite3.connect(str(db_path), timeout=5)
                conn.row_factory = sqlite3.Row
                row = conn.execute(
                    "SELECT rollout_path FROM threads WHERE id = ?",
                    (session_id,)
                ).fetchone()
                if row and row["rollout_path"] and os.path.exists(row["rollout_path"]):
                    with open(row["rollout_path"], "r", errors="replace") as f:
                        for ln in f:
                            try:
                                d = json.loads(ln)
                            except Exception:
                                continue
                            payload = d.get("payload", d)
                            role = payload.get("role", d.get("type", ""))
                            if role in ("user", "assistant"):
                                content = payload.get("content", "")
                                if isinstance(content, list):
                                    content = " ".join(
                                        c.get("text", "") for c in content
                                        if isinstance(c, dict) and c.get("type") == "text"
                                    )
                                msgs.append({
                                    "role": role,
                                    "content": str(content)[:2000],
                                    "timestamp": d.get("timestamp", ""),
                                })
                conn.close()
            except Exception:
                pass
            break

    elif cli == "opencode":
        opencode_db = home / ".local" / "share" / "opencode" / "opencode.db"
        if opencode_db.exists():
            try:
                import sqlite3
                conn = sqlite3.connect(str(opencode_db), timeout=5)
                conn.row_factory = sqlite3.Row
                tables = {r[0] for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()}
                if "message" in tables:
                    # Build part text lookup
                    part_texts = {}
                    if "part" in tables:
                        part_rows = conn.execute(
                            "SELECT message_id, data FROM part WHERE session_id = ? ORDER BY rowid",
                            (session_id,),
                        ).fetchall()
                        for pr in part_rows:
                            try:
                                pd = json.loads(pr["data"]) if pr["data"] else {}
                                if pd.get("type") == "text" and pd.get("text"):
                                    mid = pr["message_id"]
                                    part_texts[mid] = (part_texts.get(mid, "") + " " + pd["text"]).strip()
                            except Exception:
                                pass

                    msg_rows = conn.execute(
                        "SELECT id, data, time_created FROM message WHERE session_id = ? ORDER BY rowid",
                        (session_id,),
                    ).fetchall()
                    for mr in msg_rows:
                        try:
                            d = json.loads(mr["data"]) if mr["data"] else {}
                        except Exception:
                            continue
                        role = d.get("role", "assistant")
                        if role not in ("user", "assistant"):
                            continue
                        content = part_texts.get(mr["id"], "")
                        if not content:
                            content = d.get("content", d.get("text", ""))
                        msgs.append({
                            "role": role,
                            "content": str(content)[:2000],
                            "timestamp": str(d.get("time", {}).get("created", mr["time_created"] or "")),
                        })
                conn.close()
            except Exception:
                pass

    return msgs


# ── tmux helpers ─────────────────────────────────────────────────────────

TMUX_PREFIX = "ccs-"


def _resolve_cli_binary(cli: str) -> str:
    """Find the binary path for a CLI provider."""
    import shutil
    if cli == "opencode":
        candidate = Path.home() / ".opencode" / "bin" / "opencode"
        return str(candidate) if candidate.exists() else (shutil.which("opencode") or "opencode")
    elif cli == "codex":
        return shutil.which("codex") or "codex"
    else:
        return shutil.which("claude") or "claude"


def tmux_session_exists(session_id: str) -> bool:
    """Check if a ccs tmux session exists."""
    name = TMUX_PREFIX + session_id
    r = subprocess.run(
        ["tmux", "has-session", "-t", name],
        capture_output=True,
    )
    return r.returncode == 0


def tmux_kill(session_id: str) -> bool:
    """Kill a ccs tmux session."""
    name = TMUX_PREFIX + session_id
    r = subprocess.run(
        ["tmux", "kill-session", "-t", name],
        capture_output=True,
    )
    return r.returncode == 0


# ── PTY relay ────────────────────────────────────────────────────────────

async def pty_relay(ws, cmd: list, cwd: Optional[str] = None):
    """
    Spawn a command with a PTY and relay I/O over WebSocket.

    - Binary frames: raw PTY data
    - Text frames: JSON control messages (resize, detach)
    """
    # Create PTY
    master_fd, slave_fd = pty.openpty()

    # Set initial terminal size
    cols, rows = 200, 50
    fcntl.ioctl(slave_fd, termios.TIOCSWINSZ,
                struct.pack("HHHH", rows, cols, 0, 0))

    # Spawn process
    env = os.environ.copy()
    env["TERM"] = "xterm-256color"
    proc = subprocess.Popen(
        cmd,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        cwd=cwd,
        env=env,
        preexec_fn=os.setsid,
    )
    os.close(slave_fd)

    loop = asyncio.get_event_loop()

    # Read from PTY → send binary frames
    async def pty_to_ws():
        try:
            while True:
                data = await loop.run_in_executor(None, os.read, master_fd, 4096)
                if not data:
                    break
                await ws.send(data)
        except (OSError, websockets.exceptions.ConnectionClosed):
            pass

    # Read from WebSocket → write to PTY
    async def ws_to_pty():
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    os.write(master_fd, message)
                elif isinstance(message, str):
                    try:
                        msg = json.loads(message)
                        cmd_type = msg.get("cmd")
                        if cmd_type == CMD_RESIZE:
                            try:
                                new_cols = int(msg.get("cols", cols))
                                new_rows = int(msg.get("rows", rows))
                                # Clamp to valid terminal size range
                                new_cols = max(1, min(new_cols, 500))
                                new_rows = max(1, min(new_rows, 500))
                            except (TypeError, ValueError):
                                continue
                            fcntl.ioctl(master_fd, termios.TIOCSWINSZ,
                                        struct.pack("HHHH", new_rows, new_cols, 0, 0))
                            # Signal the process group
                            try:
                                os.killpg(os.getpgid(proc.pid), signal.SIGWINCH)
                            except (OSError, ProcessLookupError):
                                pass
                    except (json.JSONDecodeError, KeyError):
                        pass
        except websockets.exceptions.ConnectionClosed:
            pass

    # Run both directions concurrently
    reader = asyncio.create_task(pty_to_ws())
    writer = asyncio.create_task(ws_to_pty())

    try:
        # Wait for either direction to finish
        done, pending = await asyncio.wait(
            [reader, writer],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()
    finally:
        # Clean up
        try:
            os.close(master_fd)
        except OSError:
            pass
        if proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                proc.kill()

        # Notify client
        try:
            await ws.send(json.dumps({"event": EVT_DETACHED}))
        except Exception:
            pass


# ── WebSocket handler ────────────────────────────────────────────────────

async def handle_connection(ws):
    """Handle a single WebSocket client connection."""
    secret = load_or_create_secret()
    authenticated_client: Optional[str] = None
    peer = ws.remote_address[0] if ws.remote_address else "?"
    log(f"\033[33m→ connect\033[0m  {peer}")

    try:
        async for message in ws:
            if isinstance(message, bytes):
                continue  # Binary frames only during PTY relay

            try:
                msg = json.loads(message)
            except json.JSONDecodeError:
                await ws.send(json.dumps(error_msg("Invalid JSON")))
                continue

            cmd = msg.get("cmd", "")

            debug(f"cmd={cmd} from {peer}")

            # ── Pairing (no auth required) ──
            if cmd == CMD_PAIR:
                code = msg.get("code", "")
                if not validate_pair_code(code):
                    log(f"\033[31m✗ pair\033[0m     {peer}  invalid code")
                    await ws.send(json.dumps(error_msg("Invalid or expired pairing code")))
                    continue
                client_id = generate_client_id()
                access_token = create_access_token(client_id, secret)
                refresh_token = create_refresh_token()
                register_client(client_id, refresh_token, name=msg.get("name", ""))
                await ws.send(json.dumps(ok_msg(
                    access_token=access_token,
                    refresh_token=refresh_token,
                    client_id=client_id,
                )))
                authenticated_client = client_id
                log(f"\033[32m✓ pair\033[0m     {peer}  client={client_id[:8]} name={msg.get('name', '')}")
                continue

            # ── Token refresh (needs refresh token, not access token) ──
            if cmd == CMD_REFRESH:
                client_id = msg.get("client_id", "")
                refresh_token = msg.get("refresh_token", "")
                if not verify_refresh(client_id, refresh_token):
                    log(f"\033[31m✗ refresh\033[0m  {peer}  invalid token")
                    await ws.send(json.dumps(error_msg("Invalid refresh token")))
                    continue
                access_token = create_access_token(client_id, secret)
                await ws.send(json.dumps(ok_msg(access_token=access_token)))
                authenticated_client = client_id
                log(f"\033[32m✓ refresh\033[0m  {peer}  client={client_id[:8]}")
                continue

            # ── Auth ──
            if cmd == CMD_AUTH:
                token = msg.get("token", "")
                client_id = verify_access_token(token, secret)
                if not client_id:
                    debug(f"auth failed from {peer}")
                    await ws.send(json.dumps(error_msg("Invalid or expired token")))
                    continue
                authenticated_client = client_id
                debug(f"auth ok for {client_id[:8]} from {peer}")
                await ws.send(json.dumps(ok_msg()))
                continue

            # ── All other commands require auth ──
            if not authenticated_client:
                await ws.send(json.dumps(error_msg("Not authenticated")))
                continue

            if cmd == CMD_PING:
                debug(f"ping from {peer}")
                await ws.send(json.dumps(ok_msg(pong=True)))

            elif cmd == CMD_SCAN:
                t0 = time.time()
                sessions = scan_sessions_direct()
                elapsed = time.time() - t0
                log(f"\033[36mscan\033[0m      {peer}  {len(sessions)} sessions ({elapsed:.1f}s)")
                await ws.send(json.dumps(ok_msg(sessions=sessions)))

            elif cmd == CMD_INFO:
                sid = msg.get("id", "")
                cli = msg.get("cli", "claude")
                if not _valid_session_id(sid):
                    await ws.send(json.dumps(error_msg("Invalid session ID")))
                    continue
                if not _valid_cli(cli):
                    await ws.send(json.dumps(error_msg("Invalid CLI")))
                    continue
                debug(f"info {sid[:12]} from {peer}")
                messages = read_session_messages(sid, cli)
                await ws.send(json.dumps(ok_msg(messages=messages)))

            elif cmd == CMD_KILL:
                sid = msg.get("id", "")
                if not _valid_session_id(sid):
                    await ws.send(json.dumps(error_msg("Invalid session ID")))
                    continue
                if tmux_kill(sid):
                    log(f"\033[31m✗ kill\033[0m    {peer}  {sid[:12]}")
                    await ws.send(json.dumps(ok_msg()))
                else:
                    log(f"\033[31m✗ kill\033[0m    {peer}  {sid[:12]}  not found")
                    await ws.send(json.dumps(error_msg("Session not found or already dead")))

            elif cmd == CMD_ATTACH:
                sid = msg.get("id", "")
                cli = msg.get("cli", "claude")
                cwd = msg.get("cwd")
                if not _valid_session_id(sid):
                    await ws.send(json.dumps(error_msg("Invalid session ID")))
                    continue
                if not _valid_cli(cli):
                    await ws.send(json.dumps(error_msg("Invalid CLI")))
                    continue
                cwd = _sanitize_cwd(cwd)
                tmux_name = TMUX_PREFIX + sid
                if not tmux_session_exists(sid):
                    # Auto-launch: create a tmux session that resumes the
                    # CLI session, so the user doesn't have to do it manually.
                    binary = _resolve_cli_binary(cli)
                    resume_cmd = (
                        f"{shlex.quote(binary)} --resume {shlex.quote(sid)}"
                    )
                    full_cmd = (
                        f"{resume_cmd}; echo ''; echo 'Session ended.';"
                        f" sleep 1; tmux kill-session -t"
                        f" {shlex.quote(tmux_name)} 2>/dev/null || true"
                    )
                    subprocess.run([
                        "tmux", "new-session", "-d", "-s", tmux_name,
                        "-x", "200", "-y", "50",
                    ] + (["-c", cwd] if cwd else []) + [full_cmd])
                    log(f"\033[32m▶ resume\033[0m  {peer}  {sid[:12]} cli={cli}")
                else:
                    log(f"\033[32m▶ attach\033[0m  {peer}  {sid[:12]}")
                await ws.send(json.dumps(ok_msg(pty=True)))
                await pty_relay(ws, ["tmux", "attach-session", "-t", tmux_name])
                log(f"\033[33m■ detach\033[0m  {peer}  {sid[:12]}")
                # pty_relay cancels its ws recv task, leaving the WebSocket
                # in a state where the outer loop can't call recv again.
                # Break out — the client reconnects for new commands.
                break

            elif cmd == CMD_NEW:
                cli = msg.get("cli", "claude")
                args = msg.get("args", [])
                cwd = msg.get("cwd")
                if not _valid_cli(cli):
                    await ws.send(json.dumps(error_msg("Invalid CLI")))
                    continue
                if not isinstance(args, list):
                    await ws.send(json.dumps(error_msg("Invalid args")))
                    continue
                # Reject args containing shell metacharacters.
                for a in args:
                    if not isinstance(a, str) or not _SAFE_ARG_RE.match(a):
                        await ws.send(json.dumps(
                            error_msg(f"Invalid argument: {str(a)[:40]}")
                        ))
                        break
                else:
                    # All args passed validation — proceed
                    cwd = _sanitize_cwd(cwd)
                    binary = _resolve_cli_binary(cli)

                    uid = generate_client_id()  # Reuse for session naming
                    tmux_name = TMUX_PREFIX + uid

                    cmd_parts = [binary] + args
                    cmd_str = " ".join(shlex.quote(p) for p in cmd_parts)
                    full_cmd = (
                        f"{cmd_str}; echo ''; echo 'Session ended.';"
                        f" sleep 1; tmux kill-session -t"
                        f" {shlex.quote(tmux_name)} 2>/dev/null || true"
                    )
                    subprocess.run([
                        "tmux", "new-session", "-d", "-s", tmux_name,
                        "-x", "200", "-y", "50",
                    ] + (["-c", cwd] if cwd else []) + [full_cmd])

                    log(f"\033[32m+ new\033[0m     {peer}  cli={cli} id={uid[:12]}")
                    await ws.send(json.dumps(ok_msg(pty=True, id=uid)))
                    await pty_relay(ws, ["tmux", "attach-session", "-t", tmux_name])
                    log(f"\033[33m■ detach\033[0m  {peer}  {uid[:12]}")
                    break  # WebSocket recv state is consumed by pty_relay
                # If we broke out of the for loop (bad arg), skip to next message
                continue

            else:
                await ws.send(json.dumps(error_msg(f"Unknown command: {cmd}")))

    except websockets.exceptions.ConnectionClosed:
        log(f"\033[33m← disconnect\033[0m {peer}")


# ── Server startup ───────────────────────────────────────────────────────

async def start_server(port: int = DEFAULT_PORT, bind: str = "0.0.0.0"):
    """Start the ccs WebSocket server."""
    cert_path, key_path = ensure_tls_certs()

    ssl_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ssl_ctx.load_cert_chain(str(cert_path), str(key_path))

    fingerprint = cert_fingerprint(cert_path)
    code = new_pair_code()

    print(f"\033[1;36m◆ ccs serve\033[0m")
    print(f"  Listening on wss://{bind}:{port}")
    print(f"  TLS fingerprint: {fingerprint}")
    print(f"  Pairing code: \033[1;33m{code}\033[0m (expires in 5 minutes)")
    print(f"\n  To pair from a local machine:")
    print(f"    ccs remote add <name> <this-host>:{port} --pair {code}")
    print(f"\n  Generate new pairing code:")
    print(f"    ccs serve pair")
    print()

    async with websockets.serve(
        handle_connection,
        bind,
        port,
        ssl=ssl_ctx,
        max_size=2**20,  # 1MB max message
    ):
        await asyncio.Future()  # Run forever


# ── CLI entry points ─────────────────────────────────────────────────────

def cmd_serve(args: list):
    """Entry point for 'ccs serve' command."""
    import sqlite3  # Ensure available for scanning

    if args and args[0] == "pair":
        code = new_pair_code()
        print(f"New pairing code: \033[1;33m{code}\033[0m (expires in 5 minutes)")
        # Write to file so the running server can pick it up
        pair_file = CCS_DIR / "pending_pair_code"
        pair_file.write_text(f"{code}:{int(time.time()) + PAIR_CODE_EXPIRY}")
        return

    if args and args[0] == "clients":
        clients = list_clients()
        if not clients:
            print("No paired clients.")
            return
        print(f"\033[1;36m◆\033[0m Paired clients:\n")
        for c in clients:
            created = time.strftime("%Y-%m-%d %H:%M", time.localtime(c["created"]))
            seen = time.strftime("%Y-%m-%d %H:%M", time.localtime(c["last_seen"]))
            print(f"  {c['client_id']}  name={c['name'] or '(none)'}  created={created}  last_seen={seen}")
        return

    if args and args[0] == "revoke":
        if len(args) > 1:
            if args[1] == "--all":
                count = revoke_all_clients()
                print(f"Revoked {count} client(s).")
            else:
                if revoke_client(args[1]):
                    print(f"Client {args[1]} revoked.")
                else:
                    print(f"Client {args[1]} not found.")
        else:
            print("Usage: ccs serve revoke <client_id> | --all")
        return

    # Parse --port, --bind, --debug
    global _debug
    port = DEFAULT_PORT
    bind = "0.0.0.0"
    i = 0
    while i < len(args):
        if args[i] == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        elif args[i] == "--bind" and i + 1 < len(args):
            bind = args[i + 1]
            i += 2
        elif args[i] == "--debug":
            _debug = True
            i += 1
        else:
            i += 1

    asyncio.run(start_server(port, bind))


if __name__ == "__main__":
    cmd_serve(sys.argv[1:])
