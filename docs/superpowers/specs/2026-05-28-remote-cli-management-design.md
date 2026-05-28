# Remote CLI Session Management

**Date:** 2026-05-28
**Status:** Draft
**Scope:** Add remote server session management to ccs — list, manage, and attach to coding CLI sessions running on remote machines over a secure WebSocket protocol.

## Overview

ccs currently manages local sessions for Claude Code, Codex, and opencode. This design adds the ability to connect to remote servers running `ccs serve`, scan their sessions, manage metadata, and attach interactively — all over a self-contained secure WebSocket protocol with no SSH dependency.

## Requirements

- Full remote session management: list, tag, pin, delete, attach, create new
- Self-contained protocol — no SSH dependency
- Secure auth with pairing, short-lived access tokens, and refresh tokens
- Seamless interactive attach (terminal relay over WebSocket)
- 4-10 remote servers, scanned in parallel
- Sessions displayed grouped by host in the TUI
- Remote metadata (tags, pins) stored locally

## Protocol

### Transport

Secure WebSocket (wss://) over TLS. The `websockets` Python library handles both client and server.

- **Text frames** carry JSON command/response messages
- **Binary frames** carry raw PTY data during interactive attach
- WebSocket's built-in ping/pong handles keepalive

### TLS

- `ccs serve` generates a self-signed certificate + key on first run, stored in `~/.config/ccs/tls/`
- The client pins the server's certificate fingerprint on first connection (trust-on-first-use, like SSH known_hosts)
- Future connections reject fingerprint mismatches — user can re-pin with `ccs remote repin <name>`

### Commands

| Command | Request | Response |
|---------|---------|----------|
| `auth` | `{"cmd":"auth","token":"<access_token>"}` | `{"ok":true}` |
| `pair` | `{"cmd":"pair","code":"ABCD1234"}` | `{"access_token":"...","refresh_token":"...","client_id":"..."}` |
| `refresh` | `{"cmd":"refresh","refresh_token":"...","client_id":"..."}` | `{"access_token":"..."}` |
| `scan` | `{"cmd":"scan"}` | `{"sessions":[...]}` |
| `info` | `{"cmd":"info","id":"<sid>"}` | `{"messages":[...]}` |
| `attach` | `{"cmd":"attach","id":"<sid>"}` | `{"ok":true,"pty":true}` then binary PTY relay |
| `new` | `{"cmd":"new","cli":"...","args":[...],"cwd":"..."}` | `{"ok":true,"pty":true,"id":"..."}` then binary PTY relay |
| `kill` | `{"cmd":"kill","id":"<sid>"}` | `{"ok":true}` |
| `resize` | `{"cmd":"resize","cols":N,"rows":N}` | (no response, applied immediately during PTY relay) |
| `ping` | `{"cmd":"ping"}` | `{"pong":true}` |

## Authentication

### Pairing (one-time per client)

1. Remote runs `ccs serve` — generates and displays a one-time **pairing code** (8-char alphanumeric, expires in 5 minutes)
2. Local runs `ccs remote add dev1 host:7433 --pair <code>`
3. Local connects via wss://, sends `pair` command with the code
4. Remote validates the code (single-use, immediate invalidation), issues:
   - **Access token** — JWT signed with a per-server secret, 1-hour expiry
   - **Refresh token** — random 256-bit secret, 90-day expiry
   - **Client ID** — unique identifier for this local machine
5. Local stores all three in `remotes.json`
6. Remote stores client ID + bcrypt hash of refresh token in `~/.config/ccs/clients.json`

### Normal operation

- After connecting, the client sends `{"cmd":"auth","token":"<jwt>"}` once to authenticate the session
- All subsequent commands on that connection are authenticated (no need to repeat the token per message)
- If the connection is re-established (e.g. after disconnect), the client re-authenticates
- If the JWT has expired at re-auth time, the client first sends `refresh` to get a new token, then `auth`
- Seamless — no user interaction needed for token refresh

### Security properties

- Pairing code is single-use and expires in 5 minutes
- Access tokens expire in 1 hour — limited window if leaked
- Refresh tokens are hashed server-side (bcrypt) — DB leak doesn't expose them
- JWTs signed with a per-server secret generated at first `ccs serve` run
- TLS fingerprint pinning prevents MITM even with valid tokens
- Server tracks registered clients with revocation capability

### Client management

```
ccs serve clients              # List paired clients (ID, name, last seen, created)
ccs serve revoke <client_id>   # Revoke a specific client
ccs serve revoke --all         # Revoke all clients
```

## Remote Server (`ccs serve`)

### Startup

```
ccs serve [--port 7433] [--bind 0.0.0.0]
```

On first run:
1. Generates self-signed TLS cert + key in `~/.config/ccs/tls/`
2. Generates JWT signing secret in `~/.config/ccs/serve_secret`
3. Generates initial pairing code, prints to stdout
4. Starts WebSocket server on the specified port

### Behavior

- Long-lived process (foreground or backgrounded)
- Serves multiple concurrent WebSocket connections
- Uses existing `SessionManager` and CLI providers to scan sessions
- Each `attach`/`new` command spawns a tmux process with a PTY and relays bytes bidirectionally
- New pairing codes generated on demand: `ccs serve pair` (while server is running, writes to a shared file the server watches)

### PTY relay

When `attach` or `new` is received:
1. Server forks the appropriate tmux command with a PTY (`pty.openpty()`)
2. Switches the WebSocket connection to relay mode:
   - Remote PTY stdout → binary WebSocket frames → local stdout
   - Local stdin → binary WebSocket frames → remote PTY stdin
3. Text frames are still accepted for control: `resize`, detach
4. On tmux detach or process exit, server sends `{"event":"detached"}` text frame and exits relay mode

## Local Client

### Remote configuration

Stored in `~/.config/ccs/remotes.json`:

```json
[
  {
    "name": "dev1",
    "host": "192.168.1.50",
    "port": 7433,
    "token": "<access_token_jwt>",
    "refresh_token": "<refresh_token>",
    "client_id": "<client_id>",
    "fingerprint": "SHA256:xxxx",
    "enabled": true
  }
]
```

### CLI commands

```
ccs remote add <name> <host:port> --pair <code>   # Pair with a remote server
ccs remote list                                    # List all remotes and status
ccs remote remove <name>                           # Remove a remote
ccs remote test <name>                             # Test connectivity (ping)
ccs remote enable/disable <name>                   # Toggle without removing
ccs remote repin <name>                            # Re-pin TLS fingerprint
```

### Scanning

On TUI startup and rescan (`S`):
1. Local providers scan synchronously (as today)
2. All enabled remotes are scanned concurrently via async tasks
3. Each async task: connect wss://, auth, send `scan`, receive session JSON
4. Results merge into the unified session list
5. Unreachable remotes show as offline — no error, just `── dev1 (offline) ──`

Total scan time for 10 remotes: ~2-3s (dominated by slowest connection).

### Display

Sessions are grouped by host, local first:

```
   [C] ◆ Fix auth middleware          api-server     ~/projects/api     2m ago
   [C]   Deploy pipeline setup        infra          ~/projects/infra   1h ago
   [X]   Debug rate limiter           api-server     ~/projects/api     3h ago
  ── dev1 ──────────────────────────────────────────────────────────
   [C]   Refactor user service        backend        ~/work/users       15m ago
   [C]   Add caching layer            backend        ~/work/cache       2h ago
  ── staging ───────────────────────────────────────────────────────
   [X]   Load test analysis           perf           ~/tests            30m ago
```

- Local sessions appear first with no header
- Each remote gets a separator line with its name
- Offline remotes show: `── dev1 (offline) ──`
- All operations work on remote sessions: tag, pin, search, sort (within groups)
- `@dev1` filter narrows to a specific remote; `@local` shows only local

### Attach

1. User presses Enter on a remote session
2. ccs suspends the TUI (`with self.suspend()`)
3. Opens wss:// to the remote, authenticates
4. Sends `{"cmd":"attach","id":"<sid>"}`
5. Server responds `{"ok":true,"pty":true}`, spawns tmux attach
6. Local puts terminal in raw mode
7. Bidirectional relay: stdin → binary frames → remote PTY; remote PTY → binary frames → stdout
8. On detach (Ctrl+B, D): server sends `{"event":"detached"}`, local restores terminal, returns to TUI

### New remote session

When creating a new session and selecting a remote as target:
1. Send `{"cmd":"new","cli":"claude","args":[...],"cwd":"..."}`
2. Server creates the tmux session, switches to PTY relay
3. Same attach flow from that point

### Terminal resize

On SIGWINCH during attached session, local sends:
```json
{"cmd":"resize","cols":200,"rows":50}
```
Server calls `ioctl(TIOCSWINSZ)` on the remote PTY.

### Connection interruption

- Local detects closed WebSocket, restores terminal, returns to TUI
- Status message: "Connection to dev1 lost"
- Remote tmux session is unaffected — re-attachable on reconnect

## TUI Changes

| Area | Change |
|------|--------|
| `Session` dataclass | New fields: `remote: str = ""`, `remote_host: str = ""` |
| `SessionManager` | `load_remotes()`, `save_remotes()`, async `scan_remotes()` |
| `CCSApp` | Parallel remote scan on startup/rescan, group-by-host rendering |
| Row builder | Remote separator rows, host badge in info pane |
| `_tmux_attach` | Branch on `s.remote`: WebSocket attach for remote, local tmux for local |
| `_tmux_launch_new` | Branch: remote target sends `new` over WebSocket |
| Keybindings | `R` opens Remotes modal (list, add, remove, toggle) |
| Search | `@host` filter support |
| Metadata | Remote session keys in `sessions.json` prefixed: `dev1:session_id` |

## File Structure

| File | Purpose |
|------|---------|
| `ccs.py` | Main TUI + CLI (existing, modified to import remote client) |
| `ccs_serve.py` | WebSocket server, TLS setup, pairing, client management, PTY relay |
| `ccs_remote.py` | WebSocket client, auth flow, token refresh, remote scan, PTY attach |
| `ccs_protocol.py` | Shared constants, message types, token generation/validation |

## Dependencies

| Package | Purpose |
|---------|---------|
| `websockets` | WebSocket client + server (asyncio-native, pure Python) |
| `PyJWT` | JWT creation + validation |
| `bcrypt` | Refresh token hashing (server-side only) |

All pip-installable. No system-level dependencies beyond Python 3.9+.

## What stays unchanged

- All local session management (providers, tmux, profiles, themes)
- Existing keybindings and modals (remote operations reuse same patterns)
- Local metadata storage format (extended with remote-prefixed keys)
