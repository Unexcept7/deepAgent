"""Deterministic evidence gates used between RAGFlow and the answer model.

RAG similarity is a candidate-generation signal, not proof.  This module keeps
high-risk checks local and deterministic so that the normal request path does
not need another LLM call.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher
from typing import Any, Iterable

from app.dictionary_store import get_dictionary_store


IDENTIFIER_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])"
    r"(?:[A-Za-z]{1,12}[A-Za-z0-9]*\d[A-Za-z0-9]*"
    r"(?:[-_/][A-Za-z0-9]+)*|"
    r"[A-Za-z]{1,12}(?:[-_/][A-Za-z0-9]+)+\d[A-Za-z0-9]*)"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)

# Communication standards, encodings, addresses and version markers contain
# letters and digits but are not product identifiers.  They are validated as
# technical subjects or values instead.
IDENTIFIER_EXACT_STOP = {
    "RS232",
    "RS422",
    "RS485",
    "RJ45",
    "IPV4",
    "IPV6",
    "CRC16",
    "UTF8",
    "ASCII7",
    "N81",
}
IDENTIFIER_PREFIX_STOP = ("0X", "DI", "DO", "AI", "AO", "FC")
HEX_VALUE_CONTEXT_TERMS = (
    "功能码",
    "功能代码",
    "命令码",
    "报文",
    "请求帧",
    "响应帧",
    "数据帧",
    "数据",
    "字节",
    "十六进制",
    "hex",
    "寄存器",
    "线圈",
    "地址",
    "读取",
    "写入",
    "请求",
    "响应",
    "发送",
    "接收",
    "返回",
)
ENTITY_CONTEXT_TERMS = (
    "型号",
    "设备型号",
    "产品型号",
    "模块型号",
    "设备",
    "模块",
    "产品",
    "机型",
    "model",
    "型号为",
)


TECHNICAL_SUBJECTS: dict[str, tuple[str, ...]] = {
    "Modbus RTU": ("modbus-rtu", "modbus rtu", "modbus_rtu"),
    "Modbus TCP": ("modbus-tcp", "modbus tcp", "modbus_tcp"),
    "OPC UA": ("opc ua", "opc-ua", "opc_ua"),
    "EtherNet/IP": ("ethernet/ip", "ethernet-ip", "ethernet ip"),
    "PROFINET": ("profinet",),
    "EtherCAT": ("ethercat",),
    "MQTT": ("mqtt",),
    "CANopen": ("canopen",),
    "PROFIBUS": ("profibus",),
    "RS-485": ("rs-485", "rs485", "rs 485"),
    "RS-232": ("rs-232", "rs232", "rs 232"),
    "TCP/IP": ("tcp/ip", "tcp-ip", "tcp ip"),
    "Ethernet": ("ethernet", "以太网"),
    "CAN": ("can 总线", "can总线", "can bus"),
    "USB": ("usb",),
    "HTTP": ("http ", "http/", "http协议", "http 协议"),
    "HTTPS": ("https ", "https/", "https协议", "https 协议"),
    "Wi-Fi": ("wi-fi", "wifi", "无线局域网"),
    "Bluetooth": ("bluetooth", "蓝牙"),
}


SCALAR_FACT_PATTERNS: dict[str, re.Pattern[str]] = {
    "digital_input_channels": re.compile(
        r"(?:(?:有|具备|支持|提供|可接|共)\s*|[：:（(、,，\n]\s*|^)"
        r"(\d+)\s*路(?:数字量|开关量)?\s*输入",
        re.IGNORECASE,
    ),
    "digital_output_channels": re.compile(
        r"(?:(?:有|具备|支持|提供|可接|共)\s*|[：:（(、,，\n]\s*|^)"
        r"(\d+)\s*路(?:数字量|开关量)?\s*输出",
        re.IGNORECASE,
    ),
    "default_baud_rate": re.compile(
        r"(?:默认|缺省|出厂)[^。；\n]{0,16}(?:波特率|通信速率|通讯速率)[^0-9]{0,8}(\d{3,7})\s*(?:bps)?",
        re.IGNORECASE,
    ),
    "default_device_address": re.compile(
        r"(?:默认|缺省|出厂)[^。；\n]{0,16}(?:设备地址|从站地址|站号)[^0-9]{0,8}(\d{1,3})",
        re.IGNORECASE,
    ),
    "data_bits": re.compile(r"数据位[^0-9]{0,8}([5-9])(?:\s*位)?", re.IGNORECASE),
    "stop_bits": re.compile(r"停止位[^0-9]{0,8}([12])(?:\s*位)?", re.IGNORECASE),
}

SCALAR_FIELDS_BY_ASPECT: dict[str, set[str]] = {
    "channels_io": {"digital_input_channels", "digital_output_channels"},
    "communication_parameters": {
        "default_baud_rate",
        "default_device_address",
        "data_bits",
        "stop_bits",
    },
}

ASPECT_REQUIREMENT_GROUPS: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    "channels_io": (
        ("digital_input", ("数字量输入", "开关量输入", "di", "输入通道")),
        ("digital_output", ("数字量输出", "开关量输出", "输入输出", "do", "输出通道")),
    ),
    "communication_parameters": (
        ("device_address", ("设备地址", "从站地址", "模块地址", "站号")),
        ("baud_rate", ("波特率", "通信速率", "通讯速率", "bps")),
        ("data_bits", ("数据位", "n,8,1", "n81")),
        ("stop_bits", ("停止位", "n,8,1", "n81")),
        ("parity", ("校验方式", "无校验", "奇校验", "偶校验", "n,8,1", "n81")),
    ),
}


ASPECT_RULES: dict[str, dict[str, tuple[str, ...]]] = {
    "function_code": {
        "query": ("功能码", "功能代码", "命令码", "function code"),
        "evidence": ("功能码", "功能代码", "命令码", "function code", "modbus命令", "modbus 命令"),
    },
    "register_address": {
        "query": ("寄存器", "线圈地址", "起始地址", "地址表", "register"),
        "evidence": ("寄存器", "线圈", "起始地址", "地址", "register"),
    },
    "message_frame": {
        "query": ("报文", "请求帧", "响应帧", "数据帧", "crc", "指令帧"),
        "evidence": ("报文", "请求帧", "响应帧", "数据帧", "crc", "指令"),
    },
    "channels_io": {
        "query": ("数字量输入", "数字量输出", "开关量输入", "开关量输出", "输入输出能力", " di", " do"),
        "evidence": ("路", "通道", "数字量输入", "数字量输出", "开关量输入", "开关量输出", "di", "do"),
    },
    "channels_generic": {
        "query": ("多少路", "几路", "几通道", "通道数", "通道", "采集模块", "采集能力"),
        "evidence": ("路", "通道", "采集", "输入", "输出"),
    },
    "communication_parameters": {
        "query": ("波特率", "数据位", "停止位", "校验方式", "通信参数", "设备地址", "从站地址"),
        "evidence": ("波特率", "数据位", "停止位", "校验", "通信参数", "设备地址", "从站地址", "bps"),
    },
    "protocol_interface": {
        "query": ("协议", "接口", "通信方式", "通讯方式", "支持哪些通信"),
        "evidence": ("协议", "接口", "通信", "通讯", "modbus", "opc", "rs-", "ethernet", "profinet", "mqtt"),
    },
    "electrical": {
        "query": ("电压", "电流", "功率", "供电", "负载", "隔离", "电气参数"),
        "evidence": ("电压", "电流", "功率", "供电", "负载", "隔离", "vdc", "vac", "ma"),
    },
    "performance": {
        "query": ("精度", "频率", "采样", "响应时间", "延迟", "实时性", "性能"),
        "evidence": ("精度", "频率", "采样", "响应时间", "延迟", "实时", "hz", "ms"),
    },
    "physical_environment": {
        "query": ("尺寸", "重量", "温度", "湿度", "防护等级", "安装方式", "环境"),
        "evidence": ("尺寸", "重量", "温度", "湿度", "防护", "安装", "mm", "kg", "℃"),
    },
    "version": {
        "query": ("版本", "固件", "修订", "发布日期"),
        "evidence": ("版本", "固件", "修订", "发布日期", "version"),
    },
    "manufacturer": {
        "query": ("厂商", "厂家", "制造商", "品牌", "供应商"),
        "evidence": ("厂商", "厂家", "制造商", "品牌", "供应商", "有限公司", "company"),
    },
    "document_presence": {
        "query": ("说明书", "文档", "资料", "文件名", "知识库中是否有", "知识库里有没有"),
        "evidence": ("说明书", "手册", "资料", ".pdf", ".doc", ".txt", "用户手册"),
    },
}


DIRECT_RESPONSE_PATTERNS = (
    "你好",
    "您好",
    "早上好",
    "下午好",
    "晚上好",
    "谢谢",
    "感谢",
    "再见",
    "你是谁",
    "你能做什么",
    "介绍一下你的能力",
)
TRANSFORMATION_PATTERNS = (
    "润色",
    "改写",
    "翻译",
    "总结下面",
    "整理下面",
    "调整格式",
)
FACT_QUESTION_PATTERNS = (
    "什么",
    "哪些",
    "多少",
    "几路",
    "如何",
    "怎么",
    "为什么",
    "是否",
    "能否",
    "支持",
    "区别",
    "比较",
    "建议",
    "参数",
    "规格",
    "功能",
    "协议",
    "设备",
    "厂商",
    "知识库",
)
COMPLETE_COVERAGE_PATTERNS = (
    "系统中所有",
    "知识库中所有",
    "知识库里所有",
    "所有厂商",
    "所有厂家",
    "所有设备",
    "所有协议",
    "所有文档",
    "全部厂商",
    "全部厂家",
    "全部设备",
    "全部协议",
    "全部文档",
    "完整清单",
    "全部列出",
)


MEASUREMENT_PATTERN = re.compile(
    r"(?<![A-Za-z0-9])"
    r"\d+(?:\.\d+)?\s*"
    r"(?:k?v|ma|ka|a|w|kw|hz|khz|mhz|bps|kbps|mbps|ms|s|mm|cm|m|kg|℃|°c|%|路|通道|位|字节)"
    r"(?![A-Za-z0-9])",
    re.IGNORECASE,
)
HEX_LITERAL_PATTERN = re.compile(r"(?<![A-Za-z0-9])0x[0-9a-f]{1,8}(?![A-Za-z0-9])", re.IGNORECASE)
HEX_FRAME_PATTERN = re.compile(r"(?<![0-9A-Fa-f])(?:[0-9A-Fa-f]{2}[ \t]+){3,}[0-9A-Fa-f]{2}(?![0-9A-Fa-f])")
GENERIC_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{2,}")
GENERIC_CJK_PATTERN = re.compile(r"[\u4e00-\u9fff]{2,}")
GENERIC_STOPWORDS = {
    "what", "which", "where", "when", "how", "does", "this", "that",
    "the", "are", "is", "and", "for", "with", "from", "can", "please",
    "哪些", "什么", "如何", "怎么", "是否", "请问", "这个", "那个", "系统", "知识库",
}


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value or "").casefold()


def normalize_evidence_mode(value: str | None) -> str:
    """Normalize the optional domain policy without allowing unknown modes."""
    normalized = normalize_text(value or "")
    if normalized == "off":
        return "off"
    return "industrial_strict" if normalized == "industrial_strict" else "generic"


def canonical_identifier(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", unicodedata.normalize("NFKC", value or "")).upper()


def _runtime_dictionary() -> dict[str, Any] | None:
    try:
        return get_dictionary_store().runtime()
    except Exception:
        return None


def _runtime_dictionary_entries(dictionary_type: str) -> list[dict[str, Any]] | None:
    """Read enabled entries from the published business dictionary.

    A broken or unavailable configuration must not make evidence extraction
    fail.  The code-level defaults remain a compatibility fallback until an
    administrator publishes a valid dictionary version.
    """
    dictionary = _runtime_dictionary()
    if dictionary is None:
        return None
    return [
        item for item in dictionary.get("entries", [])
        if item.get("enabled", True) and item.get("dictionary_type") == dictionary_type
    ]


def _runtime_identifier_stops() -> tuple[set[str], tuple[str, ...]]:
    exact_entries = _runtime_dictionary_entries("stop_exact")
    prefix_entries = _runtime_dictionary_entries("stop_prefix")
    exact: set[str] = set()
    prefixes: list[str] = []
    for entry in exact_entries or []:
        for value in [entry.get("canonical_value"), *(entry.get("aliases") or [])]:
            normalized = canonical_identifier(str(value or ""))
            if normalized:
                exact.add(normalized)
    for entry in prefix_entries or []:
        for value in [entry.get("canonical_value"), *(entry.get("aliases") or [])]:
            normalized = canonical_identifier(str(value or ""))
            if normalized and normalized not in prefixes:
                prefixes.append(normalized)
    return (
        ({canonical_identifier(item) for item in IDENTIFIER_EXACT_STOP} if exact_entries is None else exact),
        (tuple(canonical_identifier(item) for item in IDENTIFIER_PREFIX_STOP) if prefix_entries is None else tuple(prefixes)),
    )


def _runtime_technical_subjects() -> dict[str, tuple[str, ...]]:
    entries = _runtime_dictionary_entries("technical_subject")
    if entries is None:
        return TECHNICAL_SUBJECTS
    result: dict[str, tuple[str, ...]] = {}
    for entry in entries:
        canonical = str(entry.get("canonical_value") or "").strip()
        if not canonical:
            continue
        aliases = [canonical, *(entry.get("aliases") or [])]
        result[canonical] = tuple(dict.fromkeys(str(item).strip() for item in aliases if str(item).strip()))
    return result


def _runtime_aspect_rules() -> dict[str, dict[str, tuple[str, ...]]]:
    entries = _runtime_dictionary_entries("aspect")
    if entries is None:
        return ASPECT_RULES
    result: dict[str, dict[str, tuple[str, ...]]] = {}
    for entry in entries:
        canonical = str(entry.get("canonical_value") or "").strip()
        if not canonical:
            continue
        query_terms = entry.get("query_terms") or entry.get("aliases") or [canonical]
        evidence_terms = entry.get("evidence_terms") or query_terms
        result[canonical] = {
            "query": tuple(str(item).strip() for item in query_terms if str(item).strip()),
            "evidence": tuple(str(item).strip() for item in evidence_terms if str(item).strip()),
        }
    return result


def _identifier_matches_allowed(identifier: str, allowed: set[str]) -> bool:
    """Accept a requested identifier or a slash-joined list of requested IDs."""
    canonical = canonical_identifier(identifier)
    if canonical in allowed:
        return True
    parts = [part for part in re.split(r"[/_,]", identifier) if part.strip()]
    return len(parts) > 1 and all(canonical_identifier(part) in allowed for part in parts)


def extract_identifiers(text: str) -> list[str]:
    """Extract product entities, excluding technical values and references.

    The lexical shape alone is not enough: ``FF00`` and ``AB12`` look like
    product names but are commonly Modbus data/address values.  Classification
    therefore combines token shape with its local semantic context.  Unknown
    product names such as ``FT-RS-9999`` remain entities and are still subject
    to the evidence gate.
    """
    result: list[str] = []
    seen: set[str] = set()
    normalized_text = unicodedata.normalize("NFKC", text or "")
    exact_stops, prefix_stops = _runtime_identifier_stops()
    for match in IDENTIFIER_PATTERN.finditer(normalized_text):
        raw = match.group(0).strip("-_/.")
        # A model may compact several requested objects as M1501/M1516 in a
        # table or sentence. Treat each side as an identifier so validation
        # can bind facts to the corresponding evidence independently.
        raw_items = [item.strip() for item in re.split(r"[/_,]", raw) if item.strip()]
        for raw_item in raw_items:
            canonical = canonical_identifier(raw_item)
            # E-xxxxxxxxxxxx is an internal evidence identifier, not a product
            # model. It must never be validated as a device identifier.
            if re.fullmatch(r"E-[0-9A-F]{8,}", raw_item, re.IGNORECASE):
                continue
            if _classify_token(raw_item, normalized_text, match.start(), match.end(), exact_stops, prefix_stops) != "entity":
                continue
            if not canonical or canonical in exact_stops:
                continue
            if canonical.startswith(prefix_stops):
                continue
            if re.fullmatch(r"V\d+(?:\d+)?", canonical):
                continue
            if canonical not in seen:
                seen.add(canonical)
                result.append(raw_item.upper())
    return result


def find_similar_identifiers(
    query: str,
    chunks: list[dict[str, Any]],
    *,
    min_similarity: float = 0.78,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Find likely model typos without treating near matches as evidence.

    Similar identifiers are only clarification candidates.  They never enter
    the verified chunk set and therefore cannot silently substitute one model
    for another in a factual answer.
    """
    requested = extract_identifiers(query)
    if not requested:
        return []

    candidates: list[str] = []
    seen_candidates: set[str] = set()
    for chunk in chunks:
        searchable = f"{chunk.get('document_name', '')}\n{chunk.get('content', '')}"
        for candidate in extract_identifiers(searchable):
            canonical = canonical_identifier(candidate)
            if canonical and canonical not in seen_candidates:
                seen_candidates.add(canonical)
                candidates.append(candidate)

    suggestions: list[dict[str, Any]] = []
    for target in requested:
        target_key = canonical_identifier(target)
        ranked: list[tuple[float, str]] = []
        for candidate in candidates:
            candidate_key = canonical_identifier(candidate)
            if candidate_key == target_key:
                continue
            similarity = SequenceMatcher(None, target_key, candidate_key).ratio()
            if similarity >= min_similarity:
                ranked.append((similarity, candidate))
        ranked.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
        for similarity, candidate in ranked[:limit]:
            suggestions.append({
                "requested": target,
                "candidate": candidate,
                "similarity": round(similarity, 4),
            })
    return suggestions[:limit]


def _looks_like_hex_value(raw_item: str, text: str, start: int, end: int) -> bool:
    """Return whether a model-like token is actually a hexadecimal value."""
    canonical = canonical_identifier(raw_item)
    if not re.fullmatch(r"[0-9A-F]{2,8}", canonical, re.IGNORECASE):
        return False
    if not re.search(r"[A-F]", canonical, re.IGNORECASE):
        return False

    context = normalize_text(text[max(0, start - 48): min(len(text), end + 48)])
    has_value_context = any(normalize_text(term) in context for term in HEX_VALUE_CONTEXT_TERMS)
    has_entity_context = any(normalize_text(term) in context for term in ENTITY_CONTEXT_TERMS)
    return has_value_context and not has_entity_context


def _classify_token(
    raw_item: str,
    text: str,
    start: int,
    end: int,
    exact_stops: set[str] | None = None,
    prefix_stops: tuple[str, ...] | None = None,
) -> str:
    """Classify a model-shaped token before it enters entity validation."""
    if re.fullmatch(r"E-[0-9A-F]{8,}", raw_item, re.IGNORECASE):
        return "internal_reference"

    if exact_stops is None or prefix_stops is None:
        exact_stops, prefix_stops = _runtime_identifier_stops()
    canonical = canonical_identifier(raw_item)
    if not canonical or canonical in exact_stops:
        return "technical_value"
    if canonical.startswith(prefix_stops) or re.fullmatch(r"V\d+(?:\d+)?", canonical):
        return "technical_value"

    if re.fullmatch(r"[0-9A-F]{2,8}", canonical, re.IGNORECASE):
        if _looks_like_hex_value(raw_item, text, start, end):
            return "hex_value"
        context = normalize_text(text[max(0, start - 48): min(len(text), end + 48)])
        # Without local value context, keep the token as an entity.  This is
        # important for legitimate models such as A100 or B100 that happen to
        # consist only of hexadecimal characters and digits.

    return "entity"


def extract_technical_subjects(text: str) -> list[str]:
    normalized = normalize_text(text)
    found: list[str] = []
    for subject, aliases in _runtime_technical_subjects().items():
        if any(normalize_text(alias) in normalized for alias in aliases):
            found.append(subject)
    # Prefer the more specific Modbus variants over a generic duplicate.
    return found


def extract_aspects(text: str) -> list[str]:
    normalized = f" {normalize_text(text)}"
    return [
        name
        for name, rule in _runtime_aspect_rules().items()
        if any(normalize_text(term) in normalized for term in rule["query"])
    ]


def query_requires_evidence(query: str) -> bool:
    """Conservative risk policy for enterprise factual answers.

    This is not an intent classifier.  It only decides whether an answer may be
    released without external evidence.
    """
    compact = normalize_text(query).strip()
    if not compact:
        return False
    if any(pattern in compact for pattern in TRANSFORMATION_PATTERNS):
        return False
    direct_text = compact.rstrip("!！。,.，?？")
    if any(pattern == direct_text for pattern in DIRECT_RESPONSE_PATTERNS):
        return False
    if len(compact) <= 60 and (
        "你能做什么" in compact
        or "能做些什么" in compact
        or ("介绍" in compact and "能力" in compact)
    ):
        return False
    if extract_identifiers(query) or extract_aspects(query) or extract_technical_subjects(query):
        return True
    return "?" in compact or "？" in compact or any(term in compact for term in FACT_QUESTION_PATTERNS)


def contextualize_query(
    history: list[dict[str, str]],
    query: str,
    context_summary: dict[str, Any] | None = None,
) -> str:
    """Resolve follow-up subjects from recent user turns and safe summary hints.

    Assistant messages are intentionally ignored because an earlier generated
    answer is conversational context, not trusted evidence.  A summary value
    is only a retrieval hint; it does not turn into verified evidence.
    """
    normalized = normalize_text(query)
    follow_up_markers = ("它", "该设备", "这个设备", "上述", "刚才", "前面", "请求帧", "响应报文", "设备地址")
    if not any(marker in normalized for marker in follow_up_markers):
        return query

    inherited: list[str] = []
    for message in reversed(history[-8:]):
        if message.get("role") != "user":
            continue
        inherited.extend(extract_identifiers(message.get("content", "")))
        if inherited:
            break
    if not inherited:
        for item in (context_summary or {}).get("candidate_entities") or []:
            value = str(item.get("value") or "").strip()
            if value:
                inherited.append(value)
    inherited = list(dict.fromkeys(inherited))
    if not inherited:
        return query
    return f"{query}\n[仅用于检索与证据校验的上下文对象：{'、'.join(inherited)}]"


def _identifier_in_text(identifier: str, text: str) -> bool:
    parts = re.findall(r"[A-Za-z]+|\d+[A-Za-z]*", identifier)
    if not parts:
        return False
    flexible = r"[\s\-_/\.]*".join(re.escape(part) for part in parts)
    return bool(re.search(rf"(?<![A-Za-z0-9]){flexible}(?![A-Za-z0-9])", text, re.IGNORECASE))


def _matching_aspects(text: str, requested: Iterable[str]) -> list[str]:
    normalized = normalize_text(text)
    rules = _runtime_aspect_rules()
    return [
        aspect
        for aspect in requested
        if any(normalize_text(term) in normalized for term in rules.get(aspect, {}).get("evidence", ()))
    ]


def _identifier_has_local_aspect(
    identifier: str,
    content: str,
    requested_aspects: Iterable[str],
    window: int = 320,
) -> bool:
    """Require a requested property near a model mention in mixed documents."""
    parts = re.findall(r"[A-Za-z]+|\d+[A-Za-z]*", identifier)
    if not parts:
        return False
    flexible = r"[\s\-_/\.]*".join(re.escape(part) for part in parts)
    for match in re.finditer(rf"(?<![A-Za-z0-9]){flexible}(?![A-Za-z0-9])", content, re.IGNORECASE):
        start = max(0, match.start() - window)
        end = min(len(content), match.end() + window)
        if _matching_aspects(content[start:end], requested_aspects):
            return True
    return False


def _evidence_id(chunk: dict[str, Any]) -> str:
    identity = "|".join(str(chunk.get(key) or "") for key in (
        "dataset_id",
        "document_id",
        "page",
        "content",
    ))
    return "E-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12].upper()


def assess_evidence(
    query: str,
    chunks: list[dict[str, Any]],
    *,
    min_similarity: float = 0.2,
    generic_min_similarity: float = 0.3,
    mode: str = "industrial_strict",
) -> dict[str, Any]:
    """Validate retrieval candidates and return only evidence-safe chunks.

    ``industrial_strict`` preserves field-level checks for the original
    industrial protocol assistant.  ``generic`` is the production default for
    mixed enterprise knowledge bases: it does not assume devices, protocols or
    fixed parameter names, and only applies generic relevance and citation
    gates.
    """
    mode = normalize_evidence_mode(mode)
    if mode == "off":
        return _assess_unrestricted_evidence(query, chunks)
    if mode == "generic":
        return _assess_generic_evidence(
            query,
            chunks,
            min_similarity=min_similarity,
            generic_min_similarity=generic_min_similarity,
        )

    identifiers = extract_identifiers(query)
    requested_aspects = extract_aspects(query)
    subjects = extract_technical_subjects(query)
    technical_subjects = _runtime_technical_subjects()
    verified: list[dict[str, Any]] = []
    covered_identifiers: set[str] = set()
    covered_aspects: set[str] = set()
    covered_subjects: set[str] = set()
    rejected_reasons: Counter[str] = Counter()
    low_similarity_count = 0

    for original in chunks:
        chunk = dict(original)
        content = str(chunk.get("content") or "")
        document_name = str(chunk.get("document_name") or "")
        searchable = f"{document_name}\n{content}"
        score = _safe_float(chunk.get("score"))

        document_identifiers = extract_identifiers(document_name)
        requested_in_document_name = [
            item for item in identifiers if _identifier_in_text(item, document_name)
        ]
        matched_identifiers = [item for item in identifiers if _identifier_in_text(item, searchable)]
        document_subject_mismatch = bool(
            identifiers
            and document_identifiers
            and not requested_in_document_name
            and matched_identifiers
        )
        if document_subject_mismatch:
            # A manual whose title clearly names another model may contain a
            # navigation menu or passing reference to the requested model.  It
            # is not accepted as that model's specification.
            matched_identifiers = []
        elif requested_aspects and not requested_in_document_name:
            matched_identifiers = [
                item
                for item in matched_identifiers
                if _identifier_has_local_aspect(item, content, requested_aspects)
            ]
        matched_subjects = [
            item
            for item in subjects
            if any(normalize_text(alias) in normalize_text(searchable) for alias in technical_subjects.get(item, (item,)))
        ]
        matched_aspects = _matching_aspects(searchable, requested_aspects)

        if identifiers and subjects:
            # Mixed comparison questions may legitimately need one product
            # manual plus separate protocol specifications.  Global coverage
            # below still requires every requested product identifier, so a
            # generic protocol chunk cannot substitute for a missing product.
            subject_ok = bool(matched_identifiers or matched_subjects)
        elif identifiers:
            subject_ok = bool(matched_identifiers)
        elif subjects:
            subject_ok = bool(matched_subjects)
        else:
            subject_ok = True
        aspect_ok = not requested_aspects or bool(matched_aspects)
        threshold = min_similarity if matched_identifiers else generic_min_similarity
        similarity_warning = score < threshold

        reasons: list[str] = []
        if not content.strip():
            reasons.append("empty_content")
        if not subject_ok:
            reasons.append("document_subject_mismatch" if document_subject_mismatch else "subject_mismatch")
        if not aspect_ok:
            reasons.append("aspect_mismatch")
        if reasons:
            rejected_reasons.update(reasons)
            continue

        if similarity_warning:
            low_similarity_count += 1

        evidence_id = _evidence_id(chunk)
        chunk.update({
            "evidence_id": str(chunk.get("evidence_id") or evidence_id),
            "matched_identifiers": matched_identifiers,
            "matched_subjects": matched_subjects,
            "matched_aspects": matched_aspects,
            "verified": True,
        })
        if similarity_warning:
            chunk["similarity_warning"] = True
            chunk["similarity_threshold"] = threshold
        verified.append(chunk)
        covered_identifiers.update(canonical_identifier(item) for item in matched_identifiers)
        covered_aspects.update(matched_aspects)
        covered_subjects.update(matched_subjects)

    # Retrieval score controls evidence ordering and diagnostics. Entity,
    # subject, and aspect checks remain the acceptance boundary.
    verified.sort(key=lambda item: _safe_float(item.get("score")), reverse=True)

    warnings: list[str] = []
    if low_similarity_count:
        warnings.append(
            f"{low_similarity_count} 个证据片段相似度低于配置阈值，"
            "因对象、主题和属性底线匹配已保留；相似度仅用于排序。"
        )

    requested_identifier_map = {canonical_identifier(item): item for item in identifiers}
    missing_identifiers = [
        display
        for canonical, display in requested_identifier_map.items()
        if canonical not in covered_identifiers
    ]
    missing_aspects = [item for item in requested_aspects if item not in covered_aspects]
    missing_subjects = [item for item in subjects if item not in covered_subjects]
    coverage_by_entity: dict[str, set[str]] = {canonical_identifier(item): set() for item in identifiers}
    for chunk in verified:
        for identifier in chunk.get("matched_identifiers") or []:
            coverage_by_entity.setdefault(canonical_identifier(identifier), set()).update(
                chunk.get("matched_aspects") or []
            )
    missing_entity_aspects = [
        {"entity": requested_identifier_map[canonical], "aspect": aspect}
        for canonical in requested_identifier_map
        for aspect in requested_aspects
        if aspect not in coverage_by_entity.get(canonical, set())
    ]
    missing_requirements = _missing_aspect_requirements(
        query,
        verified,
        identifiers,
        requested_aspects,
    )
    coverage_limited = any(pattern in normalize_text(query) for pattern in COMPLETE_COVERAGE_PATTERNS)
    conflicts = _detect_scalar_conflicts(verified, identifiers, requested_aspects)

    # A specific product identifier is a hard boundary.  Generic protocol text
    # cannot establish facts about a missing product.
    if identifiers and len(missing_identifiers) == len(identifiers):
        status = "not_found"
        verified = []
    elif not verified:
        status = "not_found"
    elif conflicts:
        status = "conflict"
    elif (
        missing_identifiers
        or missing_aspects
        or missing_subjects
        or missing_entity_aspects
        or missing_requirements
        or coverage_limited
    ):
        status = "partial"
    else:
        status = "supported"

    reason = _assessment_reason(
        status=status,
        verified_count=len(verified),
        missing_identifiers=missing_identifiers,
        missing_aspects=missing_aspects,
        missing_subjects=missing_subjects,
    )
    if coverage_limited:
        reason += "；Top-K 文本检索不能证明已覆盖系统全集"
    if missing_entity_aspects:
        pairs = "、".join(f"{item['entity']}→{item['aspect']}" for item in missing_entity_aspects[:8])
        reason += f"；对象与属性的联合证据不足：{pairs}"
    if missing_requirements:
        items = "、".join(
            f"{item.get('entity') or '问题'}→{item['requirement']}"
            for item in missing_requirements[:10]
        )
        reason += f"；细分参数证据不足：{items}"
    if conflicts:
        reason = f"发现 {len(conflicts)} 组相互冲突的标量事实，不能自动选择其中一个值"
    return {
        "status": status,
        "reason": reason,
        "query": query,
        "candidate_count": len(chunks),
        "verified_count": len(verified),
        "rejected_count": len(chunks) - len(verified),
        "requested_entities": identifiers,
        "covered_entities": [requested_identifier_map[item] for item in requested_identifier_map if item in covered_identifiers],
        "missing_entities": missing_identifiers,
        "requested_subjects": subjects,
        "covered_subjects": sorted(covered_subjects),
        "missing_subjects": missing_subjects,
        "requested_aspects": requested_aspects,
        "covered_aspects": sorted(covered_aspects),
        "missing_aspects": missing_aspects,
        "missing_entity_aspects": missing_entity_aspects,
        "missing_requirements": missing_requirements,
        "coverage_limited": coverage_limited,
        "conflicts": conflicts,
        "rejection_reasons": dict(rejected_reasons),
        "validation_mode": "industrial_strict",
        "non_blocking": False,
        "warnings": warnings,
        "chunks": verified,
    }


def _assess_unrestricted_evidence(
    query: str,
    chunks: list[dict[str, Any]],
) -> dict[str, Any]:
    """Pass RAGFlow candidates through without evidence/completeness gates."""
    provided: list[dict[str, Any]] = []
    for original in chunks:
        chunk = dict(original)
        chunk["evidence_id"] = str(chunk.get("evidence_id") or _evidence_id(chunk))
        chunk["verified"] = False
        chunk.setdefault("matched_identifiers", [])
        chunk.setdefault("matched_subjects", [])
        chunk.setdefault("matched_aspects", [])
        provided.append(chunk)

    requested_entities = extract_identifiers(query)
    return {
        "status": "unverified" if provided else "not_found",
        "reason": (
            "后端证据校验已关闭，RAGFlow 候选片段已原样提供给回答模型"
            if provided
            else "RAGFlow 本轮没有返回候选片段；后端证据校验已关闭"
        ),
        "query": query,
        "candidate_count": len(chunks),
        "provided_count": len(provided),
        "verified_count": 0,
        "rejected_count": 0,
        "requested_entities": requested_entities,
        "covered_entities": [],
        "missing_entities": [],
        "requested_subjects": [],
        "covered_subjects": [],
        "missing_subjects": [],
        "requested_aspects": [],
        "covered_aspects": [],
        "missing_aspects": [],
        "missing_entity_aspects": [],
        "missing_requirements": [],
        "coverage_limited": False,
        "conflicts": [],
        "rejection_reasons": {},
        "validation_mode": "off",
        "non_blocking": True,
        "warnings": ["后端证据底线和完整性约束已关闭"],
        "chunks": provided,
    }


def _generic_anchor_terms(text: str) -> tuple[list[str], list[str]]:
    """Extract domain-neutral reference anchors for candidate filtering."""
    ascii_terms = []
    seen_ascii: set[str] = set()
    for raw in GENERIC_TOKEN_PATTERN.findall(unicodedata.normalize("NFKC", text or "")):
        normalized = normalize_text(raw).strip("._/-")
        if normalized in GENERIC_STOPWORDS or len(normalized) < 3:
            continue
        if normalized not in seen_ascii:
            seen_ascii.add(normalized)
            ascii_terms.append(normalized)

    cjk_terms: list[str] = []
    seen_cjk: set[str] = set()
    for raw in GENERIC_CJK_PATTERN.findall(text or ""):
        for size in (4, 3, 2):
            for index in range(0, max(0, len(raw) - size + 1)):
                term = raw[index:index + size]
                if term in GENERIC_STOPWORDS or term in seen_cjk:
                    continue
                seen_cjk.add(term)
                cjk_terms.append(term)
    return ascii_terms, cjk_terms


def _generic_anchor_matches(query: str, searchable: str) -> bool:
    """Require at least one query anchor in a candidate when anchors exist."""
    ascii_terms, cjk_terms = _generic_anchor_terms(query)
    normalized_searchable = normalize_text(searchable)
    if any(term in normalized_searchable for term in ascii_terms):
        return True
    if cjk_terms:
        matches = sum(term in searchable for term in cjk_terms)
        return matches >= min(2, len(cjk_terms))
    return not ascii_terms


def _assess_generic_evidence(
    query: str,
    chunks: list[dict[str, Any]],
    *,
    min_similarity: float,
    generic_min_similarity: float,
) -> dict[str, Any]:
    """Generic evidence gate independent of any industry vocabulary.

    Retrieval similarity is a ranking signal, not a fact-proof signal.  A
    candidate with non-empty content and a matching requested entity is kept
    even when its normalized score is below the configured threshold.  The
    low score is surfaced as a warning so callers can still diagnose weak
    retrieval without losing usable evidence.
    """
    verified: list[dict[str, Any]] = []
    rejected_reasons: Counter[str] = Counter()
    threshold = max(0.0, _safe_float(generic_min_similarity or min_similarity))
    requested_identifiers = extract_identifiers(query)
    covered_identifiers: set[str] = set()
    low_similarity_count = 0

    for original in chunks:
        chunk = dict(original)
        content = str(chunk.get("content") or "")
        searchable = f"{chunk.get('document_name', '')}\n{content}"
        score = _safe_float(chunk.get("score"))
        matched_identifiers = [
            item for item in requested_identifiers
            if _identifier_in_text(item, searchable)
        ]
        similarity_warning = score < threshold
        reasons: list[str] = []
        if not content.strip():
            reasons.append("empty_content")
        if requested_identifiers and not matched_identifiers:
            reasons.append("entity_mismatch")
        if reasons:
            rejected_reasons.update(reasons)
            continue

        if similarity_warning:
            low_similarity_count += 1

        chunk.update({
            "evidence_id": str(chunk.get("evidence_id") or _evidence_id(chunk)),
            "matched_identifiers": matched_identifiers,
            "matched_subjects": [],
            "matched_aspects": [],
            "verified": True,
        })
        if similarity_warning:
            chunk["similarity_warning"] = True
            chunk["similarity_threshold"] = threshold
        verified.append(chunk)
        covered_identifiers.update(
            canonical_identifier(item) for item in matched_identifiers
        )

    # Keep the retriever's strongest candidates first.  The score now affects
    # ordering and diagnostics only; it is no longer an independent rejection
    # condition.
    verified.sort(key=lambda item: _safe_float(item.get("score")), reverse=True)

    warnings: list[str] = []
    if low_similarity_count:
        warnings.append(
            f"{low_similarity_count} 个证据片段相似度低于 {threshold:.2f}，"
            "因内容和对象底线匹配已保留；相似度仅用于排序。"
        )

    coverage_limited = any(pattern in normalize_text(query) for pattern in COMPLETE_COVERAGE_PATTERNS)
    requested_map = {canonical_identifier(item): item for item in requested_identifiers}
    missing_identifiers = [
        display
        for canonical, display in requested_map.items()
        if canonical not in covered_identifiers
    ]
    if requested_identifiers and not covered_identifiers:
        status = "not_found"
    elif missing_identifiers or coverage_limited:
        status = "partial"
    else:
        status = "supported" if verified else "not_found"
    return {
        "status": status,
        "reason": (
            f"{len(verified)} 个候选片段通过内容和对象校验；相似度仅用于排序"
            if status == "supported"
            else "仅部分证据通过内容和对象校验；相似度仅用于排序"
            if status == "partial"
            else "没有候选片段通过内容和对象校验"
        ),
        "query": query,
        "candidate_count": len(chunks),
        "verified_count": len(verified),
        "rejected_count": len(chunks) - len(verified),
        "requested_entities": requested_identifiers,
        "covered_entities": [
            requested_map[item]
            for item in requested_map
            if item in covered_identifiers
        ],
        "missing_entities": missing_identifiers,
        "requested_subjects": [],
        "covered_subjects": [],
        "missing_subjects": [],
        "requested_aspects": [],
        "covered_aspects": [],
        "missing_aspects": [],
        "missing_entity_aspects": [],
        "missing_requirements": [],
        "coverage_limited": coverage_limited,
        "conflicts": [],
        "rejection_reasons": dict(rejected_reasons),
        "validation_mode": "generic",
        "non_blocking": False,
        "warnings": warnings,
        "chunks": verified,
    }


def _assessment_reason(
    *,
    status: str,
    verified_count: int,
    missing_identifiers: list[str],
    missing_aspects: list[str],
    missing_subjects: list[str],
) -> str:
    if status == "supported":
        return f"{verified_count} 个候选片段通过实体、主题和属性校验；相似度仅用于排序"
    missing: list[str] = []
    if missing_identifiers:
        missing.append("对象：" + "、".join(missing_identifiers))
    if missing_subjects:
        missing.append("主题：" + "、".join(missing_subjects))
    if missing_aspects:
        missing.append("属性：" + "、".join(missing_aspects))
    if status == "partial":
        return "仅部分证据通过校验" + ("；缺少" + "；".join(missing) if missing else "")
    return "没有候选片段能够同时证明问题中的对象和所问属性" + ("；缺少" + "；".join(missing) if missing else "")


def validate_answer(
    answer: str,
    query: str,
    chunks: list[dict[str, Any]],
    cited_evidence_ids: Iterable[str] | None = None,
    require_evidence_ids: bool = True,
    mode: str = "industrial_strict",
) -> dict[str, Any]:
    """Check an answer against verified evidence.

    Generic mode deliberately validates only evidence binding.  It does not
    attempt to guess whether an arbitrary token is a model, address, version,
    person, policy number, or another domain-specific entity.  That keeps new
    knowledge-base domains usable without weakening the requirement that a
    factual answer cite evidence returned in this turn.
    """
    evidence_ids = {str(item.get("evidence_id")) for item in chunks if item.get("evidence_id")}
    cited = {str(item) for item in (cited_evidence_ids or []) if str(item).strip()}
    violations: list[str] = []
    invalid_ids = sorted(cited - evidence_ids)
    if invalid_ids:
        violations.append("模型引用了不存在的证据 ID：" + "、".join(invalid_ids))
    if require_evidence_ids and evidence_ids and not cited:
        violations.append("事实回答没有声明所使用的证据 ID")

    mode = normalize_evidence_mode(mode)
    if mode == "generic":
        requested_identifiers = extract_identifiers(query)
        query_identifiers = {
            canonical_identifier(item)
            for item in requested_identifiers
        }
        bound_chunks = [
            item for item in chunks
            if not cited or str(item.get("evidence_id") or "") in cited
        ]
        if query_identifiers:
            unsupported_answer_identifiers = [
                item
                for item in extract_identifiers(answer)
                if canonical_identifier(item) not in query_identifiers
            ]
            if unsupported_answer_identifiers:
                violations.append(
                    "回答引入了问题未指定的对象："
                    + "、".join(unsupported_answer_identifiers)
                )
        bound_text = "\n".join(str(item.get("content") or "") for item in bound_chunks)
        for value in _high_risk_values(answer):
            if not _value_supported(value, bound_text):
                violations.append(f"关键数值或报文缺少引用证据：{value}")
        violations.extend(_validate_entity_bound_values(answer, query, bound_chunks))
        if evidence_ids and not chunks:
            violations.append("回答没有绑定任何可用证据片段")
        if not str(answer or "").strip():
            violations.append("模型没有生成有效回答")
        violations = list(dict.fromkeys(violations))
        return {
            "passed": not violations,
            "violations": violations,
            "available_evidence_ids": sorted(evidence_ids),
            "used_evidence_ids": sorted(cited & evidence_ids) if cited else [],
        }

    # A document title is provenance metadata, not factual content. Keep it
    # available for entity binding below, but do not let values in a filename
    # (for example, “8路数字量输出”) satisfy a factual answer check.
    evidence_text = "\n".join(
        str(item.get("content") or "")
        for item in chunks
    )
    # User-provided values are questions or assumptions, not proof.  Otherwise
    # “这个设备是 16 路吗” could make an unsupported “16 路” answer pass.
    known_text = evidence_text

    query_identifier_set = {canonical_identifier(item) for item in extract_identifiers(query)}
    known_identifiers = {canonical_identifier(item) for item in extract_identifiers(evidence_text)}
    unsupported_identifiers = [
        item
        for item in extract_identifiers(answer)
        if canonical_identifier(item) not in known_identifiers
        and not _identifier_matches_allowed(item, query_identifier_set)
    ]
    if unsupported_identifiers:
        violations.append("回答出现证据中不存在的型号：" + "、".join(unsupported_identifiers))

    query_subjects = set(extract_technical_subjects(query))
    known_subjects = set(extract_technical_subjects(evidence_text))
    unsupported_subjects = [
        item
        for item in extract_technical_subjects(answer)
        if item not in known_subjects and item not in query_subjects
    ]
    if unsupported_subjects:
        violations.append("回答出现证据中不存在的技术主题：" + "、".join(unsupported_subjects))

    for display in _high_risk_values(answer):
        if not _value_supported(display, known_text):
            violations.append(f"关键数值或报文缺少证据：{display}")

    violations.extend(_validate_entity_bound_values(answer, query, chunks))

    # Preserve order while removing duplicate diagnostics.
    violations = list(dict.fromkeys(violations))
    return {
        "passed": not violations,
        "violations": violations,
        "available_evidence_ids": sorted(evidence_ids),
        "used_evidence_ids": sorted(cited & evidence_ids) if cited else sorted(evidence_ids),
    }


def _high_risk_values(text: str) -> list[str]:
    values: list[str] = []
    values.extend(match.group(0) for match in HEX_FRAME_PATTERN.finditer(text))
    values.extend(match.group(0) for match in HEX_LITERAL_PATTERN.finditer(text))
    values.extend(match.group(0) for match in MEASUREMENT_PATTERN.finditer(text))
    function_code_list = re.compile(
        r"(?:功能码|功能代码|命令码)\s*(?:为|是|包括|包含|支持|有|[:：])?\s*"
        r"((?:(?:0x)?[0-9a-f]{1,2}\s*(?:、|,|，|/|和|及|与)\s*)*"
        r"(?:0x)?[0-9a-f]{1,2})",
        re.IGNORECASE,
    )
    for match in function_code_list.finditer(text):
        values.extend(
            f"功能码 {item.group(0)}"
            for item in re.finditer(r"(?:0x)?[0-9a-f]{1,2}", match.group(1), re.IGNORECASE)
        )
    values.extend(
        match.group(0)
        for match in re.finditer(r"(?:0x)?[0-9a-f]{1,2}\s*号功能码", text, re.IGNORECASE)
    )
    for field, pattern in SCALAR_FACT_PATTERNS.items():
        values.extend(
            f"标量:{field}={match.group(1)}"
            for match in pattern.finditer(text)
        )
    return list(dict.fromkeys(item.strip() for item in values))


def _value_supported(value: str, known_text: str) -> bool:
    if value.startswith("标量:"):
        match = re.fullmatch(r"标量:([a-z_]+)=(\d+)", value)
        if not match:
            return False
        field, expected = match.groups()
        pattern = SCALAR_FACT_PATTERNS.get(field)
        if pattern is None:
            return False
        actual_values = {
            item.group(1).lstrip("0") or "0"
            for item in pattern.finditer(known_text)
        }
        return (expected.lstrip("0") or "0") in actual_values

    normalized_value = re.sub(r"\s+", "", normalize_text(value))
    normalized_known = re.sub(r"\s+", "", normalize_text(known_text))
    if normalized_value in normalized_known:
        return True

    # Treat 0x01 and a source expression such as “1 号功能码” as equivalent,
    # but do not accept an unrelated address byte as proof of a function code.
    if "功能码" in normalized_value or "功能代码" in normalized_value or "命令码" in normalized_value:
        code_match = re.search(r"(0x)?([0-9a-f]{1,2})", normalized_value, re.IGNORECASE)
        if not code_match:
            return False
        token = code_match.group(2)
        is_hex = bool(code_match.group(1)) or any(char in "abcdef" for char in token.casefold())
        number = int(token, 16 if is_hex else 10)
        return _function_code_supported(number, known_text)
    if normalized_value.startswith("0x"):
        try:
            number = int(normalized_value[2:], 16)
        except ValueError:
            return False
        return _function_code_supported(number, known_text)
    return False


def _function_code_supported(number: int, known_text: str) -> bool:
    source = normalize_text(known_text)
    decimal = str(number)
    hexadecimal = format(number, "x")
    plain_hex = rf"|0*{hexadecimal}" if any(char in "abcdef" for char in hexadecimal) else ""
    representation = rf"(?:0x0*{hexadecimal}|0*{decimal}{plain_hex})"
    patterns = (
        rf"0x0*{hexadecimal}(?![0-9a-f])",
        rf"(?:功能码|功能代码|命令码|function\s*code)[^。；\n]{{0,24}}{representation}(?![0-9a-f])",
        rf"{representation}\s*号功能码",
    )
    return any(re.search(pattern, source, re.IGNORECASE) for pattern in patterns)


def _validate_entity_bound_values(
    answer: str,
    query: str,
    chunks: list[dict[str, Any]],
) -> list[str]:
    """Prevent a value from one product being attached to another product."""
    query_identifiers = extract_identifiers(query)
    violations: list[str] = []
    segments = [item.strip() for item in re.split(r"[\n。；;]+", answer) if item.strip()]
    for segment in segments:
        segment_identifiers = extract_identifiers(segment)
        if not segment_identifiers and len(query_identifiers) == 1:
            segment_identifiers = query_identifiers
        values = _high_risk_values(segment)
        if not segment_identifiers or not values:
            continue
        for identifier in segment_identifiers:
            related_text = "\n".join(
                f"{item.get('document_name', '')}\n{item.get('content', '')}"
                for item in chunks
                if _identifier_in_text(
                    identifier,
                    f"{item.get('document_name', '')}\n{item.get('content', '')}",
                )
            )
            if not related_text:
                violations.append(f"回答中的对象 {identifier} 没有对应证据")
                continue
            for value in values:
                if not _value_supported(value, related_text):
                    violations.append(f"{identifier} 的关键值缺少对象级证据：{value}")
    return violations


def _detect_scalar_conflicts(
    chunks: list[dict[str, Any]],
    requested_identifiers: list[str],
    requested_aspects: list[str],
) -> list[dict[str, Any]]:
    """Detect explicit conflicts for a conservative set of scalar fields."""
    relevant_fields = {
        field
        for aspect in requested_aspects
        for field in SCALAR_FIELDS_BY_ASPECT.get(aspect, set())
    }
    if not requested_identifiers or not relevant_fields:
        return []
    facts: dict[tuple[str, str], dict[str, set[str]]] = {}
    for chunk in chunks:
        identity_text = f"{chunk.get('document_name', '')}\n{chunk.get('content', '')}"
        # Use the title and body to associate a chunk with the requested
        # object, but detect scalar facts only in the body. A PDF filename must
        # not become a fake conflicting evidence paragraph.
        searchable = str(chunk.get("content") or "")
        evidence_id = str(chunk.get("evidence_id") or _evidence_id(chunk))
        matched = [item for item in requested_identifiers if _identifier_in_text(item, identity_text)]
        for identifier in matched:
            for field, pattern in SCALAR_FACT_PATTERNS.items():
                if field not in relevant_fields:
                    continue
                for match in pattern.finditer(searchable):
                    value = match.group(1).lstrip("0") or "0"
                    by_value = facts.setdefault((canonical_identifier(identifier), field), {})
                    by_value.setdefault(value, set()).add(evidence_id)

    conflicts: list[dict[str, Any]] = []
    display_map = {canonical_identifier(item): item for item in requested_identifiers}
    for (identifier, field), by_value in facts.items():
        if len(by_value) <= 1:
            continue
        conflicts.append({
            "entity": display_map.get(identifier, identifier),
            "field": field,
            "values": sorted(by_value),
            "evidence_ids": sorted({item for ids in by_value.values() for item in ids}),
        })
    return conflicts


def _missing_aspect_requirements(
    query: str,
    chunks: list[dict[str, Any]],
    identifiers: list[str],
    requested_aspects: list[str],
) -> list[dict[str, str | None]]:
    """Check explicitly requested subfields, not just a broad aspect label."""
    normalized_query = normalize_text(query)
    missing: list[dict[str, str | None]] = []
    targets: list[str | None] = identifiers or [None]
    for aspect in requested_aspects:
        groups = ASPECT_REQUIREMENT_GROUPS.get(aspect, ())
        requested_groups = [
            (name, aliases)
            for name, aliases in groups
            if any(normalize_text(alias) in normalized_query for alias in aliases)
        ]
        for target in targets:
            related = "\n".join(
                f"{item.get('document_name', '')}\n{item.get('content', '')}"
                for item in chunks
                if target is None
                or _identifier_in_text(
                    target,
                    f"{item.get('document_name', '')}\n{item.get('content', '')}",
                )
            )
            normalized_related = normalize_text(related)
            for requirement, aliases in requested_groups:
                if not any(normalize_text(alias) in normalized_related for alias in aliases):
                    missing.append({"entity": target, "aspect": aspect, "requirement": requirement})
    return missing


def build_safe_answer(status: str, summary: dict[str, Any]) -> str:
    entities = summary.get("missing_entities") or summary.get("requested_entities") or []
    target = "、".join(str(item) for item in entities)
    candidate_count = int(summary.get("candidate_count") or 0)
    verified_count = int(summary.get("verified_count") or 0)

    if status == "retrieval_error":
        return "本次无法正常访问知识库，因此无法基于企业资料核实这个问题。请检查 RAGFlow 服务、API Key、知识库权限和网络连接后重试。"
    if status == "not_retrieved":
        return "这个问题涉及需要核实的知识事实，但本轮没有取得知识库证据。为避免给出无依据的参数或结论，系统已停止生成；请重试或检查 RAGFlow 工具调用配置。"
    if status == "answer_blocked":
        return "模型草稿中出现了知识库证据无法支持的型号、技术主题、数值或报文，系统已拦截该答案。请展开调用链查看校验原因，并补充可靠资料后重试。"
    if status == "conflict":
        return "知识库中检索到了相互冲突的可靠资料，系统无法安全地自动选择其中一个值。请展开证据片段核对文档版本、发布日期和适用型号，确认有效版本后再回答。"
    if status == "partial":
        covered = "、".join(str(item) for item in summary.get("covered_entities") or []) or "部分内容"
        missing = "、".join(str(item) for item in summary.get("missing_entities") or [])
        message = f"本次只获得了关于{covered}的部分可靠证据，尚不足以完成全部结论。"
        if missing:
            message += f"缺少对象：{missing}。"
        if summary.get("coverage_limited"):
            message += "当前是 Top-K 文本检索，不能据此声称已经覆盖系统中的全部记录。"
        return message + "为避免把不同对象或不完整资料混在一起，系统未输出未经完整验证的参数。"

    subject = f"“{target}”" if target else "该问题"
    message = (
        f"当前知识库没有检索到能够可靠支持{subject}及所问内容的证据，因此无法确认具体结论。"
        f"本次检索得到 {candidate_count} 个语义相似候选，其中 {verified_count} 个通过证据校验；"
        "系统不会使用其他型号、其他厂商或通用资料替代目标对象。"
    )
    similar = summary.get("similar_entities") or []
    if similar:
        first = similar[0]
        requested = str(first.get("requested") or target)
        candidate = str(first.get("candidate") or "")
        if requested and candidate:
            message += (
                f"\n\n当前知识库没有检索到 {requested}，但发现相似型号 "
                f"{candidate}。你是否想查询 {candidate}？"
            )
    return message


def public_evidence_summary(assessment: dict[str, Any], *, requires_evidence: bool = True) -> dict[str, Any]:
    """Return the stable, client-facing subset of an assessment."""
    keys = (
        "status",
        "reason",
        "candidate_count",
        "verified_count",
        "rejected_count",
        "requested_entities",
        "covered_entities",
        "missing_entities",
        "similar_entities",
        "requested_subjects",
        "covered_subjects",
        "missing_subjects",
        "requested_aspects",
        "covered_aspects",
        "missing_aspects",
        "missing_entity_aspects",
        "missing_requirements",
        "coverage_limited",
        "conflicts",
        "rejection_reasons",
        "provided_count",
        "validation_mode",
        "non_blocking",
        "warnings",
        "violations",
        "used_evidence_ids",
        "has_error",
        "errors",
    )
    result = {key: assessment.get(key) for key in keys if assessment.get(key) not in (None, [], {})}
    result["requires_evidence"] = requires_evidence
    result.setdefault("candidate_count", 0)
    result.setdefault("verified_count", 0)
    result.setdefault("rejected_count", 0)
    result.setdefault("violations", [])
    result.setdefault("warnings", [])
    result.setdefault("used_evidence_ids", [])
    return result


def _safe_float(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0
