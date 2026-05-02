"""Codex ChatGPT OAuth credential loading and refresh.

The Codex CLI stores ChatGPT OAuth credentials in ``~/.codex/auth.json``.
Those tokens are valid for the ChatGPT Codex Responses endpoint, not for
the public OpenAI API endpoint.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

from .config import (
    CODEX_AUTH_PATH,
    CODEX_OAUTH_CLIENT_ID,
    CODEX_REFRESH_TOKEN_URL,
)


TOKEN_REFRESH_INTERVAL = timedelta(days=8)


class CodexAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class CodexTokens:
    access_token: str
    refresh_token: str
    account_id: str


class CodexAuth:
    def __init__(self, auth_path: Path = CODEX_AUTH_PATH) -> None:
        self.auth_path = auth_path
        self._lock = asyncio.Lock()

    async def headers(self) -> dict[str, str]:
        tokens = await self.get_tokens()
        return {
            "Authorization": f"Bearer {tokens.access_token}",
            "ChatGPT-Account-Id": tokens.account_id,
        }

    async def get_tokens(self) -> CodexTokens:
        auth = self._read_auth()
        if self._is_stale(auth):
            await self.refresh()
            auth = self._read_auth()
        return self._tokens_from_auth(auth)

    async def refresh(self) -> CodexTokens:
        async with self._lock:
            auth = self._read_auth()
            tokens = self._tokens_from_auth(auth)
            payload = {
                "client_id": CODEX_OAUTH_CLIENT_ID,
                "grant_type": "refresh_token",
                "refresh_token": tokens.refresh_token,
            }
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    CODEX_REFRESH_TOKEN_URL,
                    headers={"Content-Type": "application/json"},
                    json=payload,
                )
            if response.status_code >= 400:
                raise CodexAuthError(
                    f"Codex OAuth token refresh failed: "
                    f"{response.status_code} {response.text[:500]}"
                )
            data = response.json()
            self._merge_refreshed_tokens(auth, data)
            self._write_auth(auth)
            return self._tokens_from_auth(auth)

    def _read_auth(self) -> dict[str, Any]:
        try:
            return json.loads(self.auth_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise CodexAuthError(
                f"Codex auth file not found: {self.auth_path}. "
                "Run `codex login` first."
            ) from exc
        except Exception as exc:
            raise CodexAuthError(
                f"Cannot read Codex auth file {self.auth_path}: {exc}"
            ) from exc

    def _write_auth(self, auth: dict[str, Any]) -> None:
        self.auth_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.auth_path.with_name(f".{self.auth_path.name}.tmp")
        text = json.dumps(auth, ensure_ascii=False, indent=2)
        tmp_path.write_text(text + "\n", encoding="utf-8")
        tmp_path.chmod(0o600)
        tmp_path.replace(self.auth_path)

    def _tokens_from_auth(self, auth: dict[str, Any]) -> CodexTokens:
        tokens = auth.get("tokens")
        if not isinstance(tokens, dict):
            raise CodexAuthError(
                f"Codex auth file has no tokens object: {self.auth_path}"
            )
        access_token = str(tokens.get("access_token") or "").strip()
        refresh_token = str(tokens.get("refresh_token") or "").strip()
        account_id = str(tokens.get("account_id") or "").strip()
        if not access_token or not refresh_token or not account_id:
            raise CodexAuthError(
                "Codex auth file is missing access_token, refresh_token, "
                f"or account_id: {self.auth_path}"
            )
        return CodexTokens(
            access_token=access_token,
            refresh_token=refresh_token,
            account_id=account_id,
        )

    def _is_stale(self, auth: dict[str, Any]) -> bool:
        raw = auth.get("last_refresh")
        if not isinstance(raw, str) or not raw.strip():
            return False
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return False
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed <= datetime.now(timezone.utc) - TOKEN_REFRESH_INTERVAL

    def _merge_refreshed_tokens(
        self, auth: dict[str, Any], refreshed: dict[str, Any]
    ) -> None:
        tokens = auth.setdefault("tokens", {})
        if not isinstance(tokens, dict):
            raise CodexAuthError("Codex auth file has an invalid tokens value.")
        for key in ("id_token", "access_token", "refresh_token"):
            value = refreshed.get(key)
            if isinstance(value, str) and value:
                tokens[key] = value
        auth["last_refresh"] = datetime.now(timezone.utc).isoformat()
