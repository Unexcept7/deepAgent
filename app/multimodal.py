"""Shared validation and message helpers for controlled multimodal queries.

The workbench intentionally accepts images only in the chat composer.  Office
documents and PDFs remain governed by the RAGFlow knowledge-base ingestion
path; accepting arbitrary file bytes here would bypass that lifecycle.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse


MAX_ATTACHMENTS = 4
MAX_ATTACHMENT_SOURCE_CHARS = 8_000_000
MAX_TOTAL_ATTACHMENT_SOURCE_CHARS = 24_000_000


def _as_dict(item: Any) -> dict[str, Any]:
    if hasattr(item, "model_dump"):
        return dict(item.model_dump(mode="json"))
    if isinstance(item, dict):
        return dict(item)
    raise ValueError("图片附件格式无效")


def _valid_http_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def normalize_attachments(raw: Any) -> list[dict[str, Any]]:
    """Return a small, provider-neutral attachment payload.

    This is deliberately duplicated at runtime in addition to Pydantic request
    validation because the agent is also called directly by workflows/tests.
    """
    if not raw:
        return []
    if not isinstance(raw, (list, tuple)):
        raise ValueError("图片附件必须是数组")
    if len(raw) > MAX_ATTACHMENTS:
        raise ValueError(f"一次最多上传 {MAX_ATTACHMENTS} 张图片")

    normalized: list[dict[str, Any]] = []
    total_chars = 0
    for item in raw:
        payload = _as_dict(item)
        mime_type = str(payload.get("mime_type") or "").strip().lower()
        name = str(payload.get("name") or "图片").strip()[:160] or "图片"
        data_url = str(payload.get("data_url") or "").strip()
        url = str(payload.get("url") or "").strip()
        if not mime_type.startswith("image/"):
            raise ValueError("工作台暂只支持图片附件")
        if bool(data_url) == bool(url):
            raise ValueError("图片附件必须且只能提供 data_url 或 url")
        source = data_url or url
        if len(source) > MAX_ATTACHMENT_SOURCE_CHARS:
            raise ValueError("图片附件过大，请压缩后重试")
        if data_url and not data_url.lower().startswith("data:image/"):
            raise ValueError("图片 data_url 格式无效")
        if url and not _valid_http_url(url):
            raise ValueError("图片 URL 只允许使用 http 或 https")
        total_chars += len(source)
        if total_chars > MAX_TOTAL_ATTACHMENT_SOURCE_CHARS:
            raise ValueError("本次图片总大小过大，请减少图片数量或压缩图片")

        clean: dict[str, Any] = {
            "name": name,
            "mime_type": mime_type,
        }
        if payload.get("size_bytes") is not None:
            try:
                clean["size_bytes"] = max(0, int(payload["size_bytes"]))
            except (TypeError, ValueError) as exc:
                raise ValueError("图片附件大小字段无效") from exc
        if data_url:
            clean["data_url"] = data_url
        else:
            clean["url"] = url
        normalized.append(clean)
    return normalized


def multimodal_content(text: str, attachments: Any = None) -> str | list[dict[str, Any]]:
    """Build OpenAI-compatible text/image content for LangChain models."""
    normalized = normalize_attachments(attachments)
    if not normalized:
        return str(text or "")
    content: list[dict[str, Any]] = [{"type": "text", "text": str(text or "")}]
    for item in normalized:
        source = item.get("data_url") or item.get("url")
        content.append({
            "type": "image_url",
            "image_url": {"url": source},
        })
    return content


def attachment_metadata(raw: Any) -> list[dict[str, Any]]:
    """Return normalized attachments safe to persist in conversation metadata."""
    return normalize_attachments(raw)
