"""
ccs_remote — WebSocket client for connecting to remote ccs servers.

Provides functions for pairing, scanning, attaching, and managing remote sessions.
"""

import asyncio
import json
import os
import select
import signal
import ssl
import sys
import termios
import tty
from pathlib import Path
from typing import List, Optional

import websockets
import websockets.client

from ccs_protocol import (
    DEFAULT_PORT, CMD_AUTH, CMD_PAIR, CMD_REFRESH, CMD_SCAN, CMD_INFO,
    CMD_ATTACH, CMD_NEW, CMD_KILL, CMD_RESIZE, CMD_PING,
    EVT_DETACHED,
    REMOTES_FILE, CCS_DIR,
    verify_access_token, load_or_create_secret, cert_fingerprint,
    error_msg, ok_msg,
)

# ── Remote configuration ────────────────────────────────────────────────

def load_remotes() -> list:
    """Load remote server configurations."""
    if REMOTES_FILE.exists():
        try:
            return json.loads(REMOTES_FILE.read_text())
        except Exception:
            return []
    return []


def save_remotes(remotes: list):
    """Save remote server configurations."""
    CCS_DIR.mkdir(parents=True, exist_ok=True)
    REMOTES_FILE.write_text(json.dumps(remotes, indent=2))


def get_remote(name: str) -> Optional[dict]:
    """Get a remote by name."""
    for r in load_remotes():
        if r["name"] == name:
            return r
    return None


def add_remote(name: str, host: str, port: int, token: str, refresh_token: str,
               client_id: str, fingerprint: str) -> None:
    """Add or update a remote configuration."""
    remotes = load_remotes()
    # Remove existing with same name
    remotes = [r for r in remotes if r["name"] != name]
    remotes.append({
        "name": name,
        "host": host,
        "port": port,
        "token": token,
        "refresh_token": refresh_token,
        "client_id": client_id,
        "fingerprint": fingerprint,
        "enabled": True,
    })
    save_remotes(remotes)


def remove_remote(name: str) -> bool:
    """Remove a remote. Returns True if found and removed."""
    remotes = load_remotes()
    new = [r for r in remotes if r["name"] != name]
    if len(new) == len(remotes):
        return False
    save_remotes(new)
    return True


def set_remote_enabled(name: str, enabled: bool) -> bool:
    """Enable or disable a remote. Returns True if found."""
    remotes = load_remotes()
    for r in remotes:
        if r["name"] == name:
            r["enabled"] = enabled
            save_remotes(remotes)
            return True
    return False


def update_remote_token(name: str, token: str):
    """Update the access token for a remote after refresh."""
    remotes = load_remotes()
    for r in remotes:
        if r["name"] == name:
            r["token"] = token
            save_remotes(remotes)
            return


# ── SSL context ──────────────────────────────────────────────────────────

def _make_ssl_ctx(fingerprint: Optional[str] = None) -> ssl.SSLContext:
    """Create an SSL context that accepts self-signed certs with optional fingerprint pinning."""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE  # We do fingerprint verification ourselves
    return ctx


# ── Connection helpers ───────────────────────────────────────────────────

async def _connect(host: str, port: int, fingerprint: Optional[str] = None):
    """Open a WebSocket connection with TLS."""
    ssl_ctx = _make_ssl_ctx(fingerprint)
    uri = f"wss://{host}:{port}"
    ws = await websockets.connect(uri, ssl=ssl_ctx, max_size=2**20)

    # Verify TLS fingerprint if provided
    if fingerprint:
        # Get the server certificate
        transport = ws.transport
        ssl_object = transport.get_extra_info("ssl_object")
        if ssl_object:
            der_cert = ssl_object.getpeercert(binary_form=True)
            if der_cert:
                import hashlib
                digest = hashlib.sha256(der_cert).hexdigest()
                actual = "SHA256:" + ":".join(digest[i:i+2] for i in range(0, len(digest), 2))
                if actual != fingerprint:
                    await ws.close()
                    raise ssl.SSLError(
                        f"TLS fingerprint mismatch!\n"
                        f"  Expected: {fingerprint}\n"
                        f"  Got:      {actual}\n"
                        f"  Use 'ccs remote repin <name>' if the server cert was regenerated."
                    )

    return ws


async def _send_cmd(ws, cmd: dict) -> dict:
    """Send a JSON command and wait for the response."""
    await ws.send(json.dumps(cmd))
    resp = await ws.recv()
    return json.loads(resp)


# ── Auth flow ────────────────────────────────────────────────────────────

async def _authenticate(ws, remote: dict) -> bool:
    """Authenticate with a remote server. Handles token refresh if needed."""
    # Try auth with current token
    resp = await _send_cmd(ws, {"cmd": CMD_AUTH, "token": remote.get("token", "")})
    if resp.get("ok"):
        return True

    # Token expired — try refresh
    refresh_token = remote.get("refresh_token", "")
    client_id = remote.get("client_id", "")
    if not refresh_token or not client_id:
        return False

    resp = await _send_cmd(ws, {
        "cmd": CMD_REFRESH,
        "refresh_token": refresh_token,
        "client_id": client_id,
    })
    if not resp.get("ok"):
        return False

    new_token = resp.get("access_token", "")
    if not new_token:
        return False

    # Save new token
    update_remote_token(remote["name"], new_token)
    remote["token"] = new_token

    # Auth with new token
    resp = await _send_cmd(ws, {"cmd": CMD_AUTH, "token": new_token})
    return resp.get("ok", False)


# ── Public API ───────────────────────────────────────────────────────────

async def pair_remote(host: str, port: int, code: str, name: str = "") -> dict:
    """
    Pair with a remote server. Returns remote config dict on success.
    Raises Exception on failure.
    """
    ws = await _connect(host, port)
    try:
        # Get fingerprint
        transport = ws.transport
        ssl_object = transport.get_extra_info("ssl_object")
        fingerprint = ""
        if ssl_object:
            der_cert = ssl_object.getpeercert(binary_form=True)
            if der_cert:
                import hashlib
                digest = hashlib.sha256(der_cert).hexdigest()
                fingerprint = "SHA256:" + ":".join(digest[i:i+2] for i in range(0, len(digest), 2))

        resp = await _send_cmd(ws, {
            "cmd": CMD_PAIR,
            "code": code,
            "name": name,
        })
        if not resp.get("ok"):
            raise Exception(resp.get("error", "Pairing failed"))

        return {
            "host": host,
            "port": port,
            "token": resp["access_token"],
            "refresh_token": resp["refresh_token"],
            "client_id": resp["client_id"],
            "fingerprint": fingerprint,
        }
    finally:
        await ws.close()


async def scan_remote(remote: dict) -> List[dict]:
    """Scan a remote server for sessions. Returns list of session dicts."""
    try:
        ws = await asyncio.wait_for(
            _connect(remote["host"], remote["port"], remote.get("fingerprint")),
            timeout=10,
        )
    except Exception:
        return []

    try:
        if not await _authenticate(ws, remote):
            return []
        resp = await _send_cmd(ws, {"cmd": CMD_SCAN})
        if resp.get("ok"):
            return resp.get("sessions", [])
        return []
    except Exception:
        return []
    finally:
        try:
            await ws.close()
        except Exception:
            pass


async def scan_all_remotes() -> dict:
    """
    Scan all enabled remotes concurrently.
    Returns {remote_name: [sessions]} dict.
    """
    remotes = load_remotes()
    enabled = [r for r in remotes if r.get("enabled", True)]
    if not enabled:
        return {}

    tasks = {r["name"]: asyncio.create_task(scan_remote(r)) for r in enabled}
    results = {}
    for name, task in tasks.items():
        try:
            results[name] = await asyncio.wait_for(task, timeout=15)
        except (asyncio.TimeoutError, Exception):
            results[name] = []  # Mark as offline/failed
    return results


async def get_session_info(remote: dict, session_id: str, cli: str = "claude") -> list:
    """Get message preview for a remote session."""
    try:
        ws = await _connect(remote["host"], remote["port"], remote.get("fingerprint"))
    except Exception:
        return []
    try:
        if not await _authenticate(ws, remote):
            return []
        resp = await _send_cmd(ws, {"cmd": CMD_INFO, "id": session_id, "cli": cli})
        if resp.get("ok"):
            return resp.get("messages", [])
        return []
    except Exception:
        return []
    finally:
        try:
            await ws.close()
        except Exception:
            pass


async def kill_remote_session(remote: dict, session_id: str) -> bool:
    """Kill a remote tmux session."""
    try:
        ws = await _connect(remote["host"], remote["port"], remote.get("fingerprint"))
    except Exception:
        return False
    try:
        if not await _authenticate(ws, remote):
            return False
        resp = await _send_cmd(ws, {"cmd": CMD_KILL, "id": session_id})
        return resp.get("ok", False)
    except Exception:
        return False
    finally:
        try:
            await ws.close()
        except Exception:
            pass


async def ping_remote(remote: dict) -> bool:
    """Test connectivity to a remote."""
    try:
        ws = await asyncio.wait_for(
            _connect(remote["host"], remote["port"], remote.get("fingerprint")),
            timeout=5,
        )
    except Exception:
        return False
    try:
        if not await _authenticate(ws, remote):
            return False
        resp = await _send_cmd(ws, {"cmd": CMD_PING})
        return resp.get("pong", False)
    except Exception:
        return False
    finally:
        try:
            await ws.close()
        except Exception:
            pass


async def attach_remote_session(remote: dict, session_id: str) -> None:
    """
    Attach to a remote tmux session interactively.
    Takes over the terminal — raw mode, bidirectional PTY relay.
    """
    ws = await _connect(remote["host"], remote["port"], remote.get("fingerprint"))
    try:
        if not await _authenticate(ws, remote):
            raise Exception("Authentication failed")

        resp = await _send_cmd(ws, {"cmd": CMD_ATTACH, "id": session_id})
        if not resp.get("ok"):
            raise Exception(resp.get("error", "Attach failed"))

        await _pty_client_loop(ws)
    finally:
        try:
            await ws.close()
        except Exception:
            pass


async def new_remote_session(remote: dict, cli: str = "claude",
                              args: list = None, cwd: str = None) -> None:
    """
    Create a new session on a remote and attach interactively.
    """
    ws = await _connect(remote["host"], remote["port"], remote.get("fingerprint"))
    try:
        if not await _authenticate(ws, remote):
            raise Exception("Authentication failed")

        resp = await _send_cmd(ws, {
            "cmd": CMD_NEW,
            "cli": cli,
            "args": args or [],
            "cwd": cwd,
        })
        if not resp.get("ok"):
            raise Exception(resp.get("error", "New session failed"))

        await _pty_client_loop(ws)
    finally:
        try:
            await ws.close()
        except Exception:
            pass


# ── PTY client loop ──────────────────────────────────────────────────────

async def _pty_client_loop(ws):
    """
    Interactive PTY relay client.
    Puts terminal in raw mode and relays I/O with the WebSocket.
    """
    # Save terminal state
    old_settings = termios.tcgetattr(sys.stdin.fileno())
    try:
        # Set raw mode
        tty.setraw(sys.stdin.fileno())

        # Send initial terminal size
        try:
            import struct, fcntl
            result = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ,
                                  b'\x00' * 8)
            rows, cols = struct.unpack('HHHH', result)[:2]
            await ws.send(json.dumps({"cmd": CMD_RESIZE, "cols": cols, "rows": rows}))
        except Exception:
            pass

        loop = asyncio.get_event_loop()

        # Handle SIGWINCH (terminal resize)
        def on_resize():
            try:
                import struct, fcntl
                result = fcntl.ioctl(sys.stdout.fileno(), termios.TIOCGWINSZ,
                                      b'\x00' * 8)
                rows, cols = struct.unpack('HHHH', result)[:2]
                asyncio.ensure_future(
                    ws.send(json.dumps({"cmd": CMD_RESIZE, "cols": cols, "rows": rows}))
                )
            except Exception:
                pass

        # Signal handlers only work on the main thread.  When called from a
        # worker thread (e.g. TUI context) we skip — terminal resize won't be
        # forwarded but the session is otherwise fully functional.
        _has_sigwinch = False
        try:
            loop.add_signal_handler(signal.SIGWINCH, on_resize)
            _has_sigwinch = True
        except (ValueError, RuntimeError):
            pass

        # Read stdin → send to server
        async def stdin_to_ws():
            try:
                while True:
                    # Wait for stdin to be ready
                    await loop.run_in_executor(
                        None,
                        lambda: select.select([sys.stdin], [], [], None)
                    )
                    data = os.read(sys.stdin.fileno(), 4096)
                    if not data:
                        break
                    await ws.send(data)
            except (OSError, websockets.exceptions.ConnectionClosed):
                pass

        # Read from server → write to stdout
        async def ws_to_stdout():
            try:
                async for message in ws:
                    if isinstance(message, bytes):
                        os.write(sys.stdout.fileno(), message)
                    elif isinstance(message, str):
                        try:
                            msg = json.loads(message)
                            if msg.get("event") == EVT_DETACHED:
                                break
                        except json.JSONDecodeError:
                            pass
            except websockets.exceptions.ConnectionClosed:
                pass

        reader = asyncio.create_task(stdin_to_ws())
        writer = asyncio.create_task(ws_to_stdout())

        done, pending = await asyncio.wait(
            [reader, writer],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in pending:
            task.cancel()

    finally:
        # Restore terminal
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
        if _has_sigwinch:
            try:
                loop.remove_signal_handler(signal.SIGWINCH)
            except Exception:
                pass


# ── Synchronous wrappers (for use from ccs.py) ──────────────────────────

# asyncio.run() fails when called from within a running event loop (e.g. the
# Textual TUI).  On Python 3.12+ even new_event_loop().run_until_complete()
# checks for a running loop.  The reliable fix: detect an existing loop and
# run the coroutine in a worker thread where asyncio.run() gets its own loop.

import concurrent.futures


def _run_coro(coro):
    """Run a coroutine from sync context, safe even inside an existing event loop."""
    try:
        asyncio.get_running_loop()
        # Inside an existing event loop — run in a thread with its own loop
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(asyncio.run, coro).result()
    except RuntimeError:
        # No running event loop — safe to use asyncio.run directly
        return asyncio.run(coro)


def sync_scan_all_remotes() -> dict:
    """Synchronous wrapper for scan_all_remotes."""
    try:
        return _run_coro(scan_all_remotes())
    except Exception:
        return {}


def sync_scan_remote(remote: dict) -> list:
    """Synchronous wrapper for scan_remote."""
    try:
        return _run_coro(scan_remote(remote))
    except Exception:
        return []


def sync_pair_remote(host: str, port: int, code: str, name: str = "") -> dict:
    """Synchronous wrapper for pair_remote."""
    return _run_coro(pair_remote(host, port, code, name))


def sync_ping_remote(remote: dict) -> bool:
    """Synchronous wrapper for ping_remote."""
    try:
        return _run_coro(ping_remote(remote))
    except Exception:
        return False


def sync_kill_remote_session(remote: dict, session_id: str) -> bool:
    """Synchronous wrapper for kill_remote_session."""
    try:
        return _run_coro(kill_remote_session(remote, session_id))
    except Exception:
        return False


def sync_attach_remote(remote: dict, session_id: str) -> None:
    """Synchronous wrapper for attach_remote_session."""
    _run_coro(attach_remote_session(remote, session_id))


def sync_new_remote(remote: dict, cli: str = "claude",
                     args: list = None, cwd: str = None) -> None:
    """Synchronous wrapper for new_remote_session."""
    _run_coro(new_remote_session(remote, cli, args, cwd))


def sync_get_session_info(remote: dict, session_id: str, cli: str = "claude") -> list:
    """Synchronous wrapper for get_session_info."""
    try:
        return _run_coro(get_session_info(remote, session_id, cli))
    except Exception:
        return []
