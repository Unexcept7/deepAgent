from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from .settings import Settings


DEVICE_FIELDS = (
    "device_id",
    "model",
    "device_name",
    "status",
    "ai_channels",
    "di_channels",
    "do_channels",
    "protocol",
    "description",
    "updated_at",
)
DEVICE_SEARCH_FIELDS = (
    "device_id",
    "model",
    "device_name",
    "status",
    "protocol",
    "description",
)
ALARM_SEARCH_FIELDS = (
    "a.alarm_id",
    "a.device_id",
    "d.model",
    "d.device_name",
    "a.level",
    "a.message",
)
ALLOWED_STATUSES = {"online", "offline", "warning"}


def _query_terms(value: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9][A-Za-z0-9_-]*|[\u4e00-\u9fff]{2,}", value.strip())


def _limit(value: int, maximum: int) -> int:
    return max(1, min(int(value), maximum))


def _iso(value: Any) -> Any:
    return value.isoformat() if isinstance(value, (datetime,)) else value


def _normalize_device(row: dict[str, Any]) -> dict[str, Any]:
    return {key: _iso(row.get(key)) for key in DEVICE_FIELDS}


def _normalize_alarm(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "alarm_id": row.get("alarm_id"),
        "device_id": row.get("device_id"),
        "model": row.get("model"),
        "device_name": row.get("device_name"),
        "level": row.get("level"),
        "message": row.get("message"),
        "created_at": _iso(row.get("created_at")),
    }


def _text_where(alias_fields: tuple[str, ...], terms: list[str]) -> tuple[str, list[str]]:
    clauses: list[str] = []
    params: list[str] = []
    for term in terms:
        clauses.append("(" + " OR ".join(f"{field} LIKE %s" for field in alias_fields) + ")")
        params.extend([f"%{term}%"] * len(alias_fields))
    return " AND ".join(clauses), params


class MySQLRepository:
    """Small read-only repository; every query is allowlisted and parameterized."""

    def __init__(self, settings: Settings):
        self.settings = settings

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        try:
            import pymysql
        except ImportError as exc:  # pragma: no cover - exercised only in an uninstalled runtime
            raise RuntimeError("缺少 PyMySQL，请先安装 requirements.txt") from exc

        connection = pymysql.connect(
            host=self.settings.mysql_host,
            port=self.settings.mysql_port,
            user=self.settings.mysql_user,
            password=self.settings.mysql_password,
            database=self.settings.mysql_database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=self.settings.mysql_connect_timeout,
            read_timeout=self.settings.mysql_read_timeout,
            write_timeout=self.settings.mysql_write_timeout,
            autocommit=True,
        )
        try:
            yield connection
        finally:
            connection.close()

    def ping(self) -> None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()

    def list_devices(
        self,
        query: str = "",
        status: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if status and status not in ALLOWED_STATUSES:
            raise ValueError(f"不支持的设备状态: {status}")

        terms = _query_terms(query)
        where: list[str] = []
        params: list[Any] = []
        if terms:
            condition, term_params = _text_where(DEVICE_SEARCH_FIELDS, terms)
            where.append(condition)
            params.extend(term_params)
        if status:
            where.append("status = %s")
            params.append(status)

        sql = "SELECT " + ", ".join(DEVICE_FIELDS) + " FROM devices"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC LIMIT %s"
        params.append(_limit(limit, 100))

        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, params)
                return [_normalize_device(row) for row in cursor.fetchall()]

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT " + ", ".join(DEVICE_FIELDS) + " FROM devices WHERE device_id = %s",
                    (device_id.strip(),),
                )
                row = cursor.fetchone()
                return _normalize_device(row) if row else None

    def list_alarms(self, device_id: str, days: int = 30, limit: int = 50) -> list[dict[str, Any]]:
        if days < 1 or days > 365:
            raise ValueError("days 必须在 1 到 365 之间")
        cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=days)
        sql = (
            "SELECT a.alarm_id, a.device_id, d.model, d.device_name, a.level, "
            "a.message, a.created_at FROM alarms a "
            "JOIN devices d ON d.device_id = a.device_id "
            "WHERE a.device_id = %s AND a.created_at >= %s "
            "ORDER BY a.created_at DESC LIMIT %s"
        )
        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(sql, (device_id.strip(), cutoff, _limit(limit, 100)))
                return [_normalize_alarm(row) for row in cursor.fetchall()]

    def search(
        self,
        query: str,
        top_k: int = 10,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        terms = _query_terms(query)
        if not terms:
            raise ValueError("query 不能为空")

        filters = filters or {}
        status = str(filters.get("status") or "").strip() or None
        device_id = str(filters.get("device_id") or "").strip() or None
        if status and status not in ALLOWED_STATUSES:
            raise ValueError(f"不支持的设备状态: {status}")

        device_where, device_params = _text_where(DEVICE_SEARCH_FIELDS, terms)
        device_conditions = [device_where]
        if status:
            device_conditions.append("status = %s")
            device_params.append(status)
        if device_id:
            device_conditions.append("device_id = %s")
            device_params.append(device_id)

        alarm_where, alarm_params = _text_where(ALARM_SEARCH_FIELDS, terms)
        alarm_conditions = [alarm_where]
        if status:
            alarm_conditions.append("d.status = %s")
            alarm_params.append(status)
        if device_id:
            alarm_conditions.append("a.device_id = %s")
            alarm_params.append(device_id)

        device_sql = (
            "SELECT " + ", ".join(DEVICE_FIELDS) + " FROM devices WHERE "
            + " AND ".join(device_conditions)
            + " ORDER BY updated_at DESC LIMIT %s"
        )
        alarm_sql = (
            "SELECT a.alarm_id, a.device_id, d.model, d.device_name, d.status, "
            "a.level, a.message, a.created_at FROM alarms a "
            "JOIN devices d ON d.device_id = a.device_id WHERE "
            + " AND ".join(alarm_conditions)
            + " ORDER BY a.created_at DESC LIMIT %s"
        )
        row_limit = _limit(top_k, 50)

        with self._connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(device_sql, [*device_params, row_limit])
                device_rows = cursor.fetchall()
                cursor.execute(alarm_sql, [*alarm_params, row_limit])
                alarm_rows = cursor.fetchall()

        items: list[dict[str, Any]] = []
        for row in device_rows:
            device = _normalize_device(row)
            items.append(
                {
                    "id": device["device_id"],
                    "title": f'{device["model"]} · {device["device_name"]}',
                    "content": device["description"] or "",
                    "source_type": "sql",
                    "source_name": "MySQL 设备表",
                    "evidence_id": device["device_id"],
                    "score": 1.0,
                    "metadata": device,
                }
            )
        for row in alarm_rows:
            alarm = _normalize_alarm(row)
            items.append(
                {
                    "id": alarm["alarm_id"],
                    "title": f'{alarm["device_id"]} · {alarm["level"]}',
                    "content": alarm["message"] or "",
                    "source_type": "sql",
                    "source_name": "MySQL 告警表",
                    "evidence_id": alarm["alarm_id"],
                    "score": 0.9,
                    "metadata": alarm,
                }
            )
        return items[:row_limit]
