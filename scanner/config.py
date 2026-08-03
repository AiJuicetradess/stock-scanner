"""Load Public.com API credentials from environment / .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _load_dotenv() -> None:
    """Minimal .env loader — no dependency required."""
    candidates = [
        Path.cwd() / ".env",
        Path(__file__).resolve().parent.parent / ".env",
    ]
    for path in candidates:
        if not path.is_file():
            continue
        try:
            for raw in path.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                # do not override already-exported env
                if key and key not in os.environ:
                    os.environ[key] = val
        except OSError:
            continue
        break


@dataclass(frozen=True)
class PublicCredentials:
    api_secret_key: str
    account_id: str

    @classmethod
    def from_env(cls) -> "PublicCredentials":
        _load_dotenv()
        key = (
            os.environ.get("PUBLIC_API_SECRET_KEY")
            or os.environ.get("API_SECRET_KEY")
            or os.environ.get("PUBLIC_SECRET_KEY")
            or ""
        ).strip()
        account = (
            os.environ.get("PUBLIC_COM_ACCOUNT_ID")
            or os.environ.get("DEFAULT_ACCOUNT_NUMBER")
            or os.environ.get("PUBLIC_ACCOUNT_NUMBER")
            or ""
        ).strip()
        if not key:
            raise RuntimeError(
                "Missing PUBLIC_API_SECRET_KEY. Put it in stock-scanner/.env "
                "or export it in the shell."
            )
        if not account:
            raise RuntimeError(
                "Missing PUBLIC_COM_ACCOUNT_ID (account number). "
                "Put it in stock-scanner/.env or export it."
            )
        return cls(api_secret_key=key, account_id=account)
