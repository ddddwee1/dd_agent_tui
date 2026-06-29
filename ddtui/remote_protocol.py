"""Remote-control protocol helpers for ddtui.

The remote feature intentionally treats the local TUI process as the
authority. The VPS process is only a relay: it authenticates sockets,
keeps a short event cache, and forwards commands back to the active
local session.
"""

from __future__ import annotations

import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


DEFAULT_REMOTE_PORT = 10000
DEFAULT_SESSION_PATH = "/ws/session"
DEFAULT_CONTROL_PATH = "/ws/control"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_client_id(prefix: str) -> str:
    return f"{prefix}-{secrets.token_hex(6)}"


def extract_host(raw: str) -> str:
    """Extract a host from ssh-ish or URL-ish text.

    Accepts values like ``root@vps.example.com``,
    ``ssh://root@host:22``, ``host:2222``, or a plain hostname.
    """
    value = raw.strip()
    if not value:
        return ""
    if "://" in value:
        parsed = urlsplit(value)
        return parsed.hostname or ""
    if "@" in value:
        value = value.rsplit("@", 1)[1]
    if value.startswith("[") and "]" in value:
        return value[1:value.index("]")]
    return value.split(":", 1)[0].strip()


def remote_url_from_addr(
    raw: str,
    *,
    role: str = "session",
    secure: bool = False,
    port: int = DEFAULT_REMOTE_PORT,
) -> str:
    host = extract_host(raw)
    if not host:
        return ""
    path = DEFAULT_CONTROL_PATH if role == "control" else DEFAULT_SESSION_PATH
    scheme = "wss" if secure else "ws"
    return f"{scheme}://{host}:{port}{path}"


def normalize_remote_url(
    raw: str,
    *,
    role: str = "session",
    secure: bool = False,
    port: int = DEFAULT_REMOTE_PORT,
) -> str:
    """Normalize user input into a WebSocket URL.

    ``raw`` may already be a ws(s)/http(s) URL, a bare host, or an ssh
    target from ``~/vps_addr.txt``.
    """
    value = raw.strip()
    if not value:
        return ""
    path = DEFAULT_CONTROL_PATH if role == "control" else DEFAULT_SESSION_PATH
    if "://" not in value:
        return remote_url_from_addr(value, role=role, secure=secure, port=port)

    parsed = urlsplit(value)
    scheme = parsed.scheme.lower()
    if scheme == "http":
        scheme = "ws"
    elif scheme == "https":
        scheme = "wss"
    elif scheme not in ("ws", "wss"):
        return remote_url_from_addr(value, role=role, secure=secure, port=port)

    normalized_path = parsed.path or path
    if normalized_path == "/":
        normalized_path = path
    return urlunsplit(
        (
            scheme,
            parsed.netloc,
            normalized_path,
            parsed.query,
            parsed.fragment,
        )
    )


def url_with_query(url: str, params: dict[str, Any]) -> str:
    parsed = urlsplit(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    for key, value in params.items():
        if value is None or value == "":
            continue
        query[str(key)] = str(value)
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urlencode(query),
            parsed.fragment,
        )
    )


def read_optional_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        return ""
    except Exception:
        return ""


def safe_json(value: Any) -> Any:
    """Best-effort JSON-safe conversion for app state snapshots."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [safe_json(v) for v in value]
    if isinstance(value, tuple):
        return [safe_json(v) for v in value]
    if isinstance(value, dict):
        return {str(k): safe_json(v) for k, v in value.items()}
    return str(value)
