from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - the repository already declares python-dotenv
    load_dotenv = None


if load_dotenv is not None:
    load_dotenv(Path(__file__).with_name(".env"), override=False)
    load_dotenv(Path.cwd() / ".env", override=False)


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise RuntimeError(f"环境变量 {name} 必须是整数") from exc


@dataclass(frozen=True)
class Settings:
    mysql_host: str
    mysql_port: int
    mysql_database: str
    mysql_user: str
    mysql_password: str
    mysql_connect_timeout: int
    mysql_read_timeout: int
    mysql_write_timeout: int
    mcp_auth_token: str
    mcp_host: str
    mcp_port: int

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            mysql_host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            mysql_port=_int_env("MYSQL_PORT", 3306),
            mysql_database=os.getenv("MYSQL_DATABASE", "enterprise_mcp"),
            mysql_user=os.getenv("MYSQL_USER", "mcp_reader"),
            mysql_password=os.getenv("MYSQL_PASSWORD", ""),
            mysql_connect_timeout=_int_env("MYSQL_CONNECT_TIMEOUT", 3),
            mysql_read_timeout=_int_env("MYSQL_READ_TIMEOUT", 8),
            mysql_write_timeout=_int_env("MYSQL_WRITE_TIMEOUT", 8),
            mcp_auth_token=os.getenv("MCP_AUTH_TOKEN", "").strip(),
            mcp_host=os.getenv("MCP_HOST", "127.0.0.1"),
            mcp_port=_int_env("MCP_PORT", 8100),
        )
