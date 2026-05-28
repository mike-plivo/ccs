"""
ccs_protocol — Shared constants, message types, and token utilities for ccs remote.

Used by both ccs_serve.py (server) and ccs_remote.py (client).
"""

import hashlib
import os
import secrets
import time
from pathlib import Path
from typing import Optional, Tuple

import jwt

# ── Constants ────────────────────────────────────────────────────────────

DEFAULT_PORT = 7433
PROTOCOL_VERSION = 1

# Paths
CCS_DIR = Path(os.environ.get("CCS_DIR", "")) or Path.home() / ".config" / "ccs"
TLS_DIR = CCS_DIR / "tls"
TLS_CERT = TLS_DIR / "server.crt"
TLS_KEY = TLS_DIR / "server.key"
SERVE_SECRET_FILE = CCS_DIR / "serve_secret"
CLIENTS_FILE = CCS_DIR / "clients.json"
REMOTES_FILE = CCS_DIR / "remotes.json"

# Auth timing
PAIR_CODE_EXPIRY = 300        # 5 minutes
ACCESS_TOKEN_EXPIRY = 3600    # 1 hour
REFRESH_TOKEN_EXPIRY = 90 * 86400  # 90 days

# ── Commands ─────────────────────────────────────────────────────────────

CMD_AUTH = "auth"
CMD_PAIR = "pair"
CMD_REFRESH = "refresh"
CMD_SCAN = "scan"
CMD_INFO = "info"
CMD_ATTACH = "attach"
CMD_NEW = "new"
CMD_KILL = "kill"
CMD_RESIZE = "resize"
CMD_PING = "ping"

# Events (server → client)
EVT_DETACHED = "detached"
EVT_ERROR = "error"


# ── Pairing codes ────────────────────────────────────────────────────────

def generate_pair_code() -> str:
    """Generate an 8-character alphanumeric pairing code."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I/O/0/1 for readability
    return "".join(secrets.choice(alphabet) for _ in range(8))


# ── JWT tokens ───────────────────────────────────────────────────────────

def load_or_create_secret() -> str:
    """Load the JWT signing secret, creating one if it doesn't exist."""
    CCS_DIR.mkdir(parents=True, exist_ok=True)
    if SERVE_SECRET_FILE.exists():
        return SERVE_SECRET_FILE.read_text().strip()
    secret = secrets.token_hex(32)
    SERVE_SECRET_FILE.write_text(secret)
    SERVE_SECRET_FILE.chmod(0o600)
    return secret


def create_access_token(client_id: str, secret: str) -> str:
    """Create a short-lived JWT access token."""
    payload = {
        "client_id": client_id,
        "iat": int(time.time()),
        "exp": int(time.time()) + ACCESS_TOKEN_EXPIRY,
        "type": "access",
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def verify_access_token(token: str, secret: str) -> Optional[str]:
    """Verify a JWT access token. Returns client_id or None."""
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
        if payload.get("type") != "access":
            return None
        return payload.get("client_id")
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None


def create_refresh_token() -> str:
    """Create a long-lived refresh token (random 256-bit secret)."""
    return secrets.token_hex(32)


def generate_client_id() -> str:
    """Generate a unique client identifier."""
    return secrets.token_hex(8)


# ── TLS fingerprint ─────────────────────────────────────────────────────

def cert_fingerprint(cert_path: Path) -> str:
    """Compute SHA-256 fingerprint of a PEM certificate file."""
    import ssl
    der = ssl.PEM_cert_to_DER_cert(cert_path.read_text())
    digest = hashlib.sha256(der).hexdigest()
    return "SHA256:" + ":".join(digest[i:i+2] for i in range(0, len(digest), 2))


# ── TLS certificate generation ──────────────────────────────────────────

def ensure_tls_certs() -> Tuple[Path, Path]:
    """Generate self-signed TLS cert+key if they don't exist. Returns (cert_path, key_path)."""
    TLS_DIR.mkdir(parents=True, exist_ok=True)
    if TLS_CERT.exists() and TLS_KEY.exists():
        return TLS_CERT, TLS_KEY

    # Use openssl to generate a self-signed cert (available on macOS/Linux)
    import subprocess
    import tempfile

    with tempfile.NamedTemporaryFile(mode="w", suffix=".cnf", delete=False) as f:
        f.write(
            "[req]\n"
            "default_bits = 2048\n"
            "prompt = no\n"
            "default_md = sha256\n"
            "distinguished_name = dn\n"
            "x509_extensions = v3_ca\n"
            "[dn]\n"
            "CN = ccs-serve\n"
            "[v3_ca]\n"
            "subjectAltName = DNS:localhost,IP:127.0.0.1\n"
            "basicConstraints = CA:FALSE\n"
            "keyUsage = digitalSignature, keyEncipherment\n"
        )
        cnf_path = f.name

    try:
        subprocess.run(
            [
                "openssl", "req", "-x509", "-newkey", "rsa:2048",
                "-keyout", str(TLS_KEY), "-out", str(TLS_CERT),
                "-days", "3650", "-nodes", "-config", cnf_path,
            ],
            check=True, capture_output=True,
        )
        TLS_KEY.chmod(0o600)
    finally:
        os.unlink(cnf_path)

    return TLS_CERT, TLS_KEY


# ── Error responses ──────────────────────────────────────────────────────

def error_msg(msg: str) -> dict:
    """Build an error response dict."""
    return {"ok": False, "error": msg}


def ok_msg(**kwargs) -> dict:
    """Build a success response dict."""
    return {"ok": True, **kwargs}
