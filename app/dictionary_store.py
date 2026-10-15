"""Versioned, frontend-maintainable dictionaries used by preprocessing."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from app.config import get_settings
from app.schemas.dictionary import DictionaryDefinitionInput


class DictionaryError(ValueError):
    """A client-correctable dictionary or version error."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _slug(value: str) -> str:
    result = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "-", value.strip().lower()).strip("-")
    return result or f"dictionary-{uuid4().hex[:8]}"


def _snapshot(dictionary: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "dictionary_id", "id", "name", "description", "enabled", "entries",
        "version", "published_version", "status", "updated_at", "updated_by",
    )
    return {key: copy.deepcopy(dictionary.get(key)) for key in keys if key in dictionary}


def _entry(
    entry_id: str,
    dictionary_type: str,
    canonical_value: str,
    *,
    aliases: list[str] | None = None,
    query_terms: list[str] | None = None,
    evidence_terms: list[str] | None = None,
    min_digits: int = 2,
    max_digits: int = 5,
    description: str = "",
    confidence: float = 0.8,
    priority: int = 100,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "entry_id": entry_id,
        "dictionary_type": dictionary_type,
        "canonical_value": canonical_value,
        "aliases": aliases or [],
        "query_terms": query_terms or [],
        "evidence_terms": evidence_terms or [],
        "min_digits": min_digits,
        "max_digits": max_digits,
        "resolved_id": None,
        "description": description,
        "confidence": confidence,
        "priority": priority,
        "enabled": True,
        "metadata": metadata or {},
    }


DEFAULT_ENTRIES: list[dict[str, Any]] = [
    _entry("subject-modbus-rtu", "technical_subject", "Modbus RTU", aliases=["modbus-rtu", "modbus rtu", "modbus_rtu"]),
    _entry("subject-modbus-tcp", "technical_subject", "Modbus TCP", aliases=["modbus-tcp", "modbus tcp", "modbus_tcp"]),
    _entry("subject-opc-ua", "technical_subject", "OPC UA", aliases=["opc ua", "opc-ua", "opc_ua"]),
    _entry("subject-ethernet-ip", "technical_subject", "EtherNet/IP", aliases=["ethernet/ip", "ethernet-ip", "ethernet ip"]),
    _entry("subject-profinet", "technical_subject", "PROFINET", aliases=["profinet"]),
    _entry("subject-ethercat", "technical_subject", "EtherCAT", aliases=["ethercat"]),
    _entry("subject-mqtt", "technical_subject", "MQTT", aliases=["mqtt"]),
    _entry("subject-canopen", "technical_subject", "CANopen", aliases=["canopen"]),
    _entry("subject-profibus", "technical_subject", "PROFIBUS", aliases=["profibus"]),
    _entry("subject-rs485", "technical_subject", "RS-485", aliases=["rs-485", "rs485", "rs 485"]),
    _entry("subject-rs232", "technical_subject", "RS-232", aliases=["rs-232", "rs232", "rs 232"]),
    _entry("subject-tcp-ip", "technical_subject", "TCP/IP", aliases=["tcp/ip", "tcp-ip", "tcp ip"]),
    _entry("subject-ethernet", "technical_subject", "Ethernet", aliases=["ethernet", "以太网"]),
    _entry("subject-can", "technical_subject", "CAN", aliases=["can 总线", "can总线", "can bus"]),
    _entry("subject-usb", "technical_subject", "USB", aliases=["usb"]),
    _entry("subject-http", "technical_subject", "HTTP", aliases=["http ", "http/", "http协议", "http 协议"]),
    _entry("subject-https", "technical_subject", "HTTPS", aliases=["https ", "https/", "https协议", "https 协议"]),
    _entry("subject-wifi", "technical_subject", "Wi-Fi", aliases=["wi-fi", "wifi", "无线局域网"]),
    _entry("subject-bluetooth", "technical_subject", "Bluetooth", aliases=["bluetooth", "蓝牙"]),
    _entry("aspect-function-code", "aspect", "function_code", query_terms=["功能码", "功能代码", "命令码", "function code"], evidence_terms=["功能码", "功能代码", "命令码", "function code", "modbus命令", "modbus 命令"]),
    _entry("aspect-register-address", "aspect", "register_address", query_terms=["寄存器", "线圈地址", "起始地址", "地址表", "register"], evidence_terms=["寄存器", "线圈", "起始地址", "地址", "register"]),
    _entry("aspect-message-frame", "aspect", "message_frame", query_terms=["报文", "请求帧", "响应帧", "数据帧", "crc", "指令帧"], evidence_terms=["报文", "请求帧", "响应帧", "数据帧", "crc", "指令"]),
    _entry("aspect-channels-io", "aspect", "channels_io", query_terms=["数字量输入", "数字量输出", "开关量输入", "开关量输出", "输入输出能力", " di", " do"], evidence_terms=["路", "通道", "数字量输入", "数字量输出", "开关量输入", "开关量输出", "di", "do"]),
    _entry("aspect-channels-generic", "aspect", "channels_generic", query_terms=["多少路", "几路", "几通道", "通道数", "通道", "采集模块", "采集能力"], evidence_terms=["路", "通道", "采集", "输入", "输出"]),
    _entry("aspect-communication", "aspect", "communication_parameters", query_terms=["波特率", "数据位", "停止位", "校验方式", "通信参数", "设备地址", "从站地址"], evidence_terms=["波特率", "数据位", "停止位", "校验", "通信参数", "设备地址", "从站地址", "bps"]),
    _entry("aspect-protocol", "aspect", "protocol_interface", query_terms=["协议", "接口", "通信方式", "通讯方式", "支持哪些通信"], evidence_terms=["协议", "接口", "通信", "通讯", "modbus", "opc", "rs-", "ethernet", "profinet", "mqtt"]),
    _entry("aspect-electrical", "aspect", "electrical", query_terms=["电压", "电流", "功率", "供电", "负载", "隔离", "电气参数"], evidence_terms=["电压", "电流", "功率", "供电", "负载", "隔离", "vdc", "vac", "ma"]),
    _entry("aspect-performance", "aspect", "performance", query_terms=["精度", "频率", "采样", "响应时间", "延迟", "实时性", "性能"], evidence_terms=["精度", "频率", "采样", "响应时间", "延迟", "实时", "hz", "ms"]),
    _entry("aspect-physical", "aspect", "physical_environment", query_terms=["尺寸", "重量", "温度", "湿度", "防护等级", "安装方式", "环境"], evidence_terms=["尺寸", "重量", "温度", "湿度", "防护", "安装", "mm", "kg", "℃"]),
    _entry("aspect-version", "aspect", "version", query_terms=["版本", "固件", "修订", "发布日期"], evidence_terms=["版本", "固件", "修订", "发布日期", "version"]),
    _entry("aspect-manufacturer", "aspect", "manufacturer", query_terms=["厂商", "厂家", "制造商", "品牌", "供应商"], evidence_terms=["厂商", "厂家", "制造商", "品牌", "供应商", "有限公司", "company"]),
    _entry("aspect-document", "aspect", "document_presence", query_terms=["说明书", "文档", "资料", "文件名", "知识库中是否有", "知识库里有没有"], evidence_terms=["说明书", "手册", "资料", ".pdf", ".doc", ".txt", "用户手册"]),
    _entry("stop-rs232", "stop_exact", "RS232"),
    _entry("stop-rs422", "stop_exact", "RS422"),
    _entry("stop-rs485", "stop_exact", "RS485"),
    _entry("stop-rj45", "stop_exact", "RJ45"),
    _entry("stop-ipv4", "stop_exact", "IPV4"),
    _entry("stop-ipv6", "stop_exact", "IPV6"),
    _entry("stop-crc16", "stop_exact", "CRC16"),
    _entry("stop-utf8", "stop_exact", "UTF8"),
    _entry("stop-ascii7", "stop_exact", "ASCII7"),
    _entry("stop-n81", "stop_exact", "N81"),
    _entry("stop-prefix-0x", "stop_prefix", "0X"),
    _entry("stop-prefix-di", "stop_prefix", "DI"),
    _entry("stop-prefix-do", "stop_prefix", "DO"),
    _entry("stop-prefix-ai", "stop_prefix", "AI"),
    _entry("stop-prefix-ao", "stop_prefix", "AO"),
    _entry("stop-prefix-fc", "stop_prefix", "FC"),
    _entry("fault-prefix-e", "fault_code", "E", min_digits=2, max_digits=5, confidence=0.98),
    _entry("fault-prefix-al", "fault_code", "AL", min_digits=2, max_digits=5, confidence=0.95),
    _entry("fault-context", "fault_context", "fault_context", aliases=["故障码", "错误码", "报警码", "故障", "错误", "报警"]),
]


class DictionaryStore:
    def __init__(self, path: str | Path | None = None) -> None:
        configured_path = path or getattr(get_settings(), "dictionary_store_path", None)
        self.path = Path(configured_path) if configured_path else Path(__file__).parent / "data" / "dictionaries.json"
        self._lock = RLock()
        self._runtime_cache: dict[str, Any] | None = None

    def _empty_document(self) -> dict[str, Any]:
        now = _now()
        dictionary = {
            "dictionary_id": "enterprise-default",
            "id": "enterprise-default",
            "name": "企业知识助手业务词典",
            "description": "技术主题、查询属性、故障码和排除词的可维护词典。",
            "enabled": True,
            "entries": copy.deepcopy(DEFAULT_ENTRIES),
            "status": "published",
            "version": 1,
            "published_version": 1,
            "created_at": now,
            "updated_at": now,
            "updated_by": "system",
        }
        dictionary["versions"] = [{
            "version": 1,
            "status": "published",
            "created_at": now,
            "published_at": now,
            "note": "系统默认业务词典",
            "snapshot": _snapshot(dictionary),
        }]
        return {"schema_version": 1, "updated_at": now, "dictionaries": [dictionary]}

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            document = self._empty_document()
            self._write(document)
            return document
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise DictionaryError(f"业务词典文件无法读取：{exc}") from exc
        if not isinstance(document, dict) or not isinstance(document.get("dictionaries"), list):
            raise DictionaryError("业务词典文件格式无效")
        document.setdefault("schema_version", 1)
        return document

    def _write(self, document: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document["updated_at"] = _now()
        fd, temp_name = tempfile.mkstemp(prefix="dictionaries-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        self._runtime_cache = None

    @staticmethod
    def _find(document: dict[str, Any], dictionary_id: str) -> dict[str, Any]:
        for dictionary in document.get("dictionaries", []):
            if str(dictionary.get("dictionary_id") or dictionary.get("id")) == dictionary_id:
                return dictionary
        raise DictionaryError(f"业务词典不存在：{dictionary_id}")

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            result = []
            for dictionary in self._read()["dictionaries"]:
                item = copy.deepcopy(dictionary)
                item.pop("versions", None)
                item.pop("entries", None)
                item["entry_count"] = len(dictionary.get("entries") or [])
                result.append(item)
            return sorted(result, key=lambda item: (-int(item.get("version") or 0), str(item.get("name") or "")))

    def get(self, dictionary_id: str) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._find(self._read(), dictionary_id))

    def get_version(self, dictionary_id: str, version: int | None = None) -> dict[str, Any]:
        with self._lock:
            current = self._find(self._read(), dictionary_id)
            if version is None or int(current.get("version") or 0) == version:
                return copy.deepcopy(current)
            historical = next(
                (item.get("snapshot") for item in current.get("versions", [])
                 if int(item.get("version") or 0) == version),
                None,
            )
            if not historical:
                raise DictionaryError(f"不存在词典版本 {version}")
            return copy.deepcopy(historical)

    def published(self, dictionary_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            result: list[dict[str, Any]] = []
            for current in self._read()["dictionaries"]:
                current_id = str(current.get("dictionary_id") or current.get("id"))
                if dictionary_id and current_id != dictionary_id:
                    continue
                version = int(current.get("published_version") or 0)
                if version == int(current.get("version") or 0) and current.get("status") == "published":
                    result.append(copy.deepcopy(current))
                    continue
                historical = next(
                    (item.get("snapshot") for item in current.get("versions", [])
                     if int(item.get("version") or 0) == version),
                    None,
                )
                if historical:
                    result.append(copy.deepcopy(historical))
            return result

    def runtime(self, dictionary_id: str = "enterprise-default") -> dict[str, Any] | None:
        with self._lock:
            if self._runtime_cache is not None and dictionary_id == "enterprise-default":
                return copy.deepcopy(self._runtime_cache)
            dictionaries = self.published(dictionary_id)
            if not dictionaries:
                return None
            dictionary = dictionaries[0]
            if not dictionary.get("enabled", True):
                return None
            if dictionary_id == "enterprise-default":
                self._runtime_cache = copy.deepcopy(dictionary)
            return copy.deepcopy(dictionary)

    def runtime_entries(self, dictionary_type: str) -> list[dict[str, Any]]:
        dictionary = self.runtime()
        if not dictionary:
            return []
        return [
            copy.deepcopy(item)
            for item in dictionary.get("entries", [])
            if item.get("enabled", True) and item.get("dictionary_type") == dictionary_type
        ]

    def upsert(self, payload: DictionaryDefinitionInput, dictionary_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            document = self._read()
            target_id = dictionary_id or payload.dictionary_id or _slug(payload.name)
            try:
                current = self._find(document, target_id)
            except DictionaryError:
                current = None
            if current is not None:
                if payload.base_version is None or int(current.get("version") or 0) != payload.base_version:
                    raise DictionaryError(
                        f"词典版本已变化：当前为 {current.get('version')}，提交基于 {payload.base_version}"
                    )
                version = int(current.get("version") or 0) + 1
                created_at = current.get("created_at") or _now()
                published_version = current.get("published_version")
                versions = current.get("versions", [])
            else:
                version = 1
                created_at = _now()
                published_version = None
                versions = []
            values = payload.model_dump(mode="json")
            values.pop("base_version", None)
            normalized_entries = []
            for item in values.get("entries") or []:
                entry = dict(item)
                entry["entry_id"] = entry.get("entry_id") or f"{entry.get('dictionary_type')}-{_slug(entry.get('canonical_value') or '')}"
                normalized_entries.append(entry)
            values["entries"] = normalized_entries
            updated = {
                **values,
                "dictionary_id": target_id,
                "id": target_id,
                "version": version,
                "published_version": published_version,
                "status": "draft",
                "created_at": created_at,
                "updated_at": _now(),
                "updated_by": "api",
                "versions": versions,
            }
            if current is None:
                document["dictionaries"].append(updated)
            else:
                document["dictionaries"][document["dictionaries"].index(current)] = updated
            self._write(document)
            return copy.deepcopy(updated)

    def publish(self, dictionary_id: str, version: int | None = None, note: str = "") -> dict[str, Any]:
        with self._lock:
            document = self._read()
            dictionary = self._find(document, dictionary_id)
            if version is not None and int(dictionary.get("version") or 0) != version:
                raise DictionaryError(f"当前编辑版本为 {dictionary.get('version')}，不能发布版本 {version}")
            current_version = int(dictionary.get("version") or 0)
            published_at = _now()
            dictionary["status"] = "published"
            dictionary["published_version"] = current_version
            dictionary["updated_at"] = published_at
            snapshot = _snapshot(dictionary)
            existing = next((item for item in dictionary.get("versions", []) if int(item.get("version") or 0) == current_version), None)
            if existing is None:
                dictionary.setdefault("versions", []).append({
                    "version": current_version,
                    "status": "published",
                    "created_at": published_at,
                    "published_at": published_at,
                    "note": note,
                    "snapshot": snapshot,
                })
            else:
                existing.update({"status": "published", "published_at": published_at, "note": note, "snapshot": snapshot})
            self._write(document)
            return copy.deepcopy(dictionary)

    def rollback(self, dictionary_id: str, version: int, *, publish: bool = True, note: str = "") -> dict[str, Any]:
        with self._lock:
            document = self._read()
            dictionary = self._find(document, dictionary_id)
            target = next((item for item in dictionary.get("versions", []) if int(item.get("version") or 0) == version), None)
            if target is None:
                raise DictionaryError(f"词典 {dictionary_id} 不存在可回滚版本 {version}")
            snapshot = copy.deepcopy(target.get("snapshot") or {})
            next_version = int(dictionary.get("version") or 0) + 1
            for key in ("name", "description", "enabled", "entries"):
                dictionary[key] = copy.deepcopy(snapshot.get(key))
            dictionary["version"] = next_version
            dictionary["status"] = "published" if publish else "draft"
            dictionary["published_version"] = next_version if publish else dictionary.get("published_version")
            dictionary["updated_at"] = _now()
            if publish:
                dictionary.setdefault("versions", []).append({
                    "version": next_version,
                    "status": "published",
                    "created_at": dictionary["updated_at"],
                    "published_at": dictionary["updated_at"],
                    "note": note or f"回滚到版本 {version}",
                    "rolled_back_from": version,
                    "snapshot": _snapshot(dictionary),
                })
            self._write(document)
            return copy.deepcopy(dictionary)

    def compare(self, dictionary_id: str, from_version: int, to_version: int) -> dict[str, Any]:
        dictionary = self.get(dictionary_id)

        def snapshot_for(version: int) -> dict[str, Any]:
            if version == int(dictionary.get("version") or 0):
                return _snapshot(dictionary)
            for item in dictionary.get("versions", []):
                if int(item.get("version") or 0) == version:
                    return copy.deepcopy(item.get("snapshot") or {})
            raise DictionaryError(f"不存在版本 {version}")

        before = snapshot_for(from_version)
        after = snapshot_for(to_version)
        fields = ("name", "description", "enabled", "entries")
        changed_fields = [key for key in fields if before.get(key) != after.get(key)]
        return {
            "dictionary_id": dictionary_id,
            "from_version": from_version,
            "to_version": to_version,
            "changed": bool(changed_fields),
            "changed_fields": changed_fields,
            "before": before,
            "after": after,
        }


def _entry_terms(entry: dict[str, Any]) -> list[str]:
    return [
        str(item).strip()
        for item in [
            entry.get("canonical_value"),
            *(entry.get("aliases") or []),
            *(entry.get("query_terms") or []),
            *(entry.get("evidence_terms") or []),
        ]
        if str(item).strip()
    ]


def dictionary_fault_code_match(query: str, dictionary: dict[str, Any] | None = None) -> bool | None:
    """Match configured fault-code prefixes without exposing raw regex editing.

    ``None`` means no published fault-code dictionary is available, so callers
    may use their compatibility fallback.  Once a published dictionary exists,
    ``False`` is authoritative and allows an administrator to remove a prefix
    such as ``E`` without the old hard-coded rule reappearing.
    """
    if dictionary is None:
        dictionary = get_dictionary_store().runtime()
    if not dictionary:
        return None
    entries = [
        item for item in dictionary.get("entries", [])
        if item.get("enabled", True) and item.get("dictionary_type") == "fault_code"
    ]
    if not entries:
        return False
    text = str(query or "")
    for entry in entries:
        prefixes = [
            canonical_identifier(value)
            for value in [entry.get("canonical_value"), *(entry.get("aliases") or [])]
            if canonical_identifier(str(value or ""))
        ]
        if not prefixes:
            continue
        min_digits = int(entry.get("min_digits") or 2)
        max_digits = max(min_digits, int(entry.get("max_digits") or 5))
        pattern = rf"(?<![A-Za-z0-9])(?:{'|'.join(re.escape(item) for item in dict.fromkeys(prefixes))})[-_ ]?\d{{{min_digits},{max_digits}}}(?![A-Za-z0-9])"
        if re.search(pattern, text, flags=re.IGNORECASE):
            return True

    # Keep the context word configurable as well, while the code shape itself
    # stays a bounded backend safety rule rather than user-entered regex.
    context_terms = [
        term.casefold()
        for entry in dictionary.get("entries", [])
        if entry.get("enabled", True) and entry.get("dictionary_type") == "fault_context"
        for term in _entry_terms(entry)
    ]
    if context_terms and any(term and term in text.casefold() for term in context_terms):
        return bool(re.search(r"(?<![A-Za-z0-9])[A-Z]{1,4}[-_ ]?\d{2,5}(?![A-Za-z0-9])", text, re.IGNORECASE))
    return False


def preview_dictionary_matches(query: str, dictionary: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return explainable dictionary hits for the frontend test panel."""
    if dictionary is None:
        dictionary = get_dictionary_store().runtime()
    if not dictionary:
        return []
    text = str(query or "").casefold()
    hits: list[dict[str, Any]] = []
    for entry in dictionary.get("entries", []):
        if not entry.get("enabled", True):
            continue
        entry_type = str(entry.get("dictionary_type") or "")
        terms = _entry_terms(entry)
        # 故障码的 canonical_value（例如 E、AL）只是前缀，不能按普通
        # 子串判断，否则任何包含字母 E 的问题都会被误判为故障码。
        matched = [] if entry_type == "fault_code" else [term for term in terms if term.casefold() in text]
        if entry_type == "fault_code":
            prefixes = [re.escape(term) for term in terms if term]
            min_digits = int(entry.get("min_digits") or 2)
            max_digits = int(entry.get("max_digits") or 5)
            pattern = rf"(?<![A-Za-z0-9])(?:{'|'.join(prefixes)})[-_ ]?\d{{{min_digits},{max_digits}}}(?![A-Za-z0-9])" if prefixes else ""
            if pattern:
                found = re.findall(pattern, str(query or ""), flags=re.IGNORECASE)
                matched.extend(found)
        if matched:
            hits.append({
                "entry_id": entry.get("entry_id"),
                "dictionary_type": entry_type,
                "canonical_value": entry.get("canonical_value"),
                "matched_terms": list(dict.fromkeys(matched)),
                "priority": entry.get("priority", 0),
                "confidence": entry.get("confidence", 0),
            })
    return sorted(hits, key=lambda item: (-int(item.get("priority") or 0), str(item.get("entry_id") or "")))


@lru_cache(maxsize=1)
def get_dictionary_store() -> DictionaryStore:
    return DictionaryStore()
