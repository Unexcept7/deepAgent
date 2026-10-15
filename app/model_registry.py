"""Model profiles and deterministic stage-based model routing.

The registry deliberately stores only model metadata and environment-variable
names.  API keys stay in the process environment (or a future secret store),
while the router chooses a profile using stage, capability and task
complexity.  A missing fast/reasoning profile safely falls back to the current
balanced model, so enabling this registry does not break existing deployments.
"""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from threading import RLock
from typing import Any

from app.config import get_settings
from app.schemas.model_registry import (
    ModelProfileDefinitionInput,
    ModelRoutingRuleInput,
)


class ModelRegistryError(ValueError):
    """A client-correctable model registry error."""


MODEL_STAGE_CATALOG: list[dict[str, Any]] = [
    {
        "stage": "semantic_router",
        "name": "意图和实体识别",
        "description": "识别多个意图、实体、指代和检索查询，要求严格 JSON。",
        "required_capabilities": ["json"],
    },
    {
        "stage": "requirement_plan",
        "name": "需求规划",
        "description": "把一个问题拆成可独立检索和回答的需求项。",
        "required_capabilities": ["json"],
    },
    {
        "stage": "requirement_audit",
        "name": "需求覆盖审计",
        "description": "检查需求规划是否覆盖了用户原始问题。",
        "required_capabilities": ["json"],
    },
    {
        "stage": "claim_judge",
        "name": "证据裁判",
        "description": "逐条判断结论是否被证据支持、冲突或缺失。",
        "required_capabilities": ["json"],
    },
    {
        "stage": "answer",
        "name": "最终回答",
        "description": "根据经过校验的需求结果和证据组织最终答案。",
        "required_capabilities": [],
    },
    {
        "stage": "agent",
        "name": "Agent 总控",
        "description": "调用工具、执行只读检索并完成工作流编排。",
        "required_capabilities": ["tool_call"],
    },
    {
        "stage": "graph_extraction",
        "name": "图谱抽取",
        "description": "从文档 Chunk 中抽取可审核的实体和关系 JSON。",
        "required_capabilities": ["json"],
    },
]


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _default_profiles() -> list[dict[str, Any]]:
    """Return a safe compatibility set based on the existing Qwen config."""

    settings = get_settings()
    shared = {
        "provider": "DashScope / OpenAI 兼容",
        "base_url": "",
        "base_url_env": "DASHSCOPE_BASE_URL",
        "api_key_env": "DASHSCOPE_API_KEY",
        "temperature": 0.0,
        "max_tokens": 8192,
        "timeout_seconds": 60,
        "max_retries": int(settings.qwen_max_retries),
        "enable_thinking": False,
        "capabilities": ["json", "tool_call", "vision"],
        "cost_tier": "medium",
    }
    return [
        {
            **shared,
            "profile_id": "balanced",
            "name": "均衡模型（当前 Qwen）",
            "model": settings.qwen_model,
            "enabled": True,
            "description": "兼容现有项目配置，作为所有未配置模型的安全回退。",
        },
        {
            **shared,
            "profile_id": "fast",
            "name": "快速模型",
            "model": "请配置快速模型",
            "enabled": False,
            "cost_tier": "low",
            "description": "用于简单意图识别、摘要和低复杂度任务。",
        },
        {
            **shared,
            "profile_id": "reasoning",
            "name": "推理模型",
            "model": "请配置推理模型",
            "enabled": False,
            "cost_tier": "high",
            "description": "用于多意图、资料冲突、证据裁判和复杂比较。",
        },
        {
            **shared,
            "profile_id": "vision",
            "name": "多模态模型",
            "model": "请配置多模态模型",
            "enabled": False,
            "capabilities": ["json", "vision"],
            "description": "用于图片、截图和扫描资料理解。",
        },
    ]


def _default_routing() -> list[dict[str, Any]]:
    return [
        {
            "stage": "semantic_router",
            "name": "意图和实体识别",
            "simple_profile": "fast",
            "normal_profile": "balanced",
            "complex_profile": "reasoning",
            "fallback_profile": "balanced",
            "required_capabilities": ["json"],
            "enabled": True,
        },
        {
            "stage": "requirement_plan",
            "name": "需求规划",
            "simple_profile": "fast",
            "normal_profile": "balanced",
            "complex_profile": "reasoning",
            "fallback_profile": "balanced",
            "required_capabilities": ["json"],
            "enabled": True,
        },
        {
            "stage": "requirement_audit",
            "name": "需求覆盖审计",
            "simple_profile": "fast",
            "normal_profile": "balanced",
            "complex_profile": "reasoning",
            "fallback_profile": "balanced",
            "required_capabilities": ["json"],
            "enabled": True,
        },
        {
            "stage": "claim_judge",
            "name": "证据裁判",
            "simple_profile": "balanced",
            "normal_profile": "reasoning",
            "complex_profile": "reasoning",
            "fallback_profile": "balanced",
            "required_capabilities": ["json"],
            "enabled": True,
        },
        {
            "stage": "answer",
            "name": "最终回答",
            "simple_profile": "balanced",
            "normal_profile": "balanced",
            "complex_profile": "reasoning",
            "fallback_profile": "balanced",
            "required_capabilities": [],
            "enabled": True,
        },
        {
            "stage": "agent",
            "name": "Agent 总控",
            "simple_profile": "balanced",
            "normal_profile": "balanced",
            "complex_profile": "reasoning",
            "fallback_profile": "balanced",
            "required_capabilities": ["tool_call"],
            "enabled": True,
        },
        {
            "stage": "graph_extraction",
            "name": "图谱抽取",
            "simple_profile": "balanced",
            "normal_profile": "reasoning",
            "complex_profile": "reasoning",
            "fallback_profile": "balanced",
            "required_capabilities": ["json"],
            "enabled": True,
        },
    ]


def _placeholder_model(value: str) -> bool:
    text = str(value or "").strip().lower()
    return not text or text.startswith("请配置") or text in {"未配置", "待配置"}


def _profile_base_url(profile: dict[str, Any]) -> str:
    direct = str(profile.get("base_url") or "").strip()
    if direct:
        return direct.rstrip("/")
    env_name = str(profile.get("base_url_env") or "").strip()
    if env_name:
        value = os.getenv(env_name, "").strip()
        if value:
            return value.rstrip("/")
        if env_name == "DASHSCOPE_BASE_URL":
            return str(getattr(get_settings(), "dashscope_base_url", "") or "").strip().rstrip("/")
    return ""


def _profile_api_key(profile: dict[str, Any]) -> str:
    env_name = str(profile.get("api_key_env") or "").strip()
    if not env_name:
        return ""
    value = os.getenv(env_name, "").strip()
    if value:
        return value
    if env_name == "DASHSCOPE_API_KEY":
        return str(getattr(get_settings(), "dashscope_api_key", "") or "").strip()
    return ""


def _api_key_required(profile: dict[str, Any]) -> bool:
    provider = str(profile.get("provider") or "").lower()
    return bool(profile.get("api_key_env")) and not any(item in provider for item in ("ollama", "local", "本地"))


def _capability_label(value: str) -> str:
    return {
        "json": "JSON 输出",
        "tool_call": "工具调用",
        "vision": "图片理解",
        "long_context": "长上下文",
        "audio": "音频理解",
        "video": "视频理解",
    }.get(value, value)


def _profile_diagnostics(
    profile: dict[str, Any],
    required_capabilities: list[str] | None = None,
) -> list[dict[str, str]]:
    """Explain every reason a model profile cannot be used.

    This is intentionally secret-safe: it reports environment-variable names
    and missing fields, never the value of a URL credential or API key.
    """

    issues: list[dict[str, str]] = []

    def add(code: str, field: str, message: str) -> None:
        issues.append({"code": code, "field": field, "message": message})

    profile_id = str(profile.get("profile_id") or "未命名")
    if not bool(profile.get("enabled", True)):
        add("disabled", "enabled", "模型档案未启用，请勾选“启用此模型档案”并保存。")

    model_name = str(profile.get("model") or "").strip()
    if _placeholder_model(model_name):
        add("model_missing", "model", f"模型名称未配置，请填写真实模型 ID（当前档案：{profile_id}）。")

    base_url = _profile_base_url(profile)
    if not base_url:
        env_name = str(profile.get("base_url_env") or "").strip()
        if env_name:
            add("base_url_missing", "base_url_env", f"没有读取到环境变量 {env_name} 的值，请确认 .env 或系统环境变量已配置；修改环境变量后需要重启服务。")
        else:
            add("base_url_missing", "base_url", "Base URL 为空，请填写直接地址或配置 Base URL 环境变量名。")

    if _api_key_required(profile) and not _profile_api_key(profile):
        env_name = str(profile.get("api_key_env") or "").strip()
        add("api_key_missing", "api_key_env", f"没有读取到 API Key 环境变量 {env_name} 的值，请确认 .env 或系统环境变量已配置；真实 Key 不要填写到模型中心。")

    required = {str(item).strip().lower() for item in (required_capabilities or []) if str(item).strip()}
    capabilities = {str(item).strip().lower() for item in (profile.get("capabilities") or [])}
    missing = sorted(required - capabilities)
    if missing:
        labels = "、".join(f"{_capability_label(item)}（{item}）" for item in missing)
        add("capability_missing", "capabilities", f"模型能力不足，缺少：{labels}；当前能力为：{capability_label_text(capabilities)}。")
    return issues


def capability_label_text(values: set[str]) -> str:
    return "、".join(_capability_label(item) for item in sorted(values)) or "未声明"


def _profile_ready(profile: dict[str, Any], required_capabilities: list[str] | None = None) -> bool:
    return not _profile_diagnostics(profile, required_capabilities)


class ModelRegistry:
    def __init__(self, path: str | Path | None = None) -> None:
        configured_path = path or getattr(get_settings(), "model_registry_store_path", None)
        self.path = Path(configured_path) if configured_path else Path(__file__).parent / "data" / "model_profiles.json"
        self._lock = RLock()

    def _empty_document(self) -> dict[str, Any]:
        now = _now()
        return {
            "schema_version": 1,
            "version": 1,
            "updated_at": now,
            "profiles": [{**item, "version": 1, "updated_at": now} for item in _default_profiles()],
            "routing": _default_routing(),
        }

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            document = self._empty_document()
            self._write(document)
            return document
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ModelRegistryError(f"模型配置文件无法读取：{exc}") from exc
        if not isinstance(document, dict) or not isinstance(document.get("profiles"), list):
            raise ModelRegistryError("模型配置文件格式无效")
        document.setdefault("schema_version", 1)
        document.setdefault("version", 1)
        document.setdefault("routing", _default_routing())
        return document

    def _write(self, document: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        document["updated_at"] = _now()
        fd, temp_name = tempfile.mkstemp(prefix="model-profiles-", suffix=".json", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(document, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    @staticmethod
    def _find(document: dict[str, Any], profile_id: str) -> dict[str, Any]:
        for profile in document.get("profiles", []):
            if str(profile.get("profile_id") or "") == profile_id:
                return profile
        raise ModelRegistryError(f"模型配置不存在：{profile_id}")

    @staticmethod
    def _public(profile: dict[str, Any]) -> dict[str, Any]:
        item = copy.deepcopy(profile)
        item["resolved_base_url"] = _profile_base_url(profile)
        item["api_key_configured"] = bool(_profile_api_key(profile))
        item["issues"] = _profile_diagnostics(profile)
        item["ready"] = not item["issues"]
        item["status"] = "已配置" if item["ready"] else "已停用" if not profile.get("enabled", True) else "待配置"
        return item

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            document = self._read()
            routing = copy.deepcopy(document.get("routing") or _default_routing())
            return {
                "schema_version": document.get("schema_version", 1),
                "version": document.get("version", 1),
                "updated_at": document.get("updated_at"),
                "profiles": [self._public(item) for item in document.get("profiles", [])],
                "routing": routing,
                "stages": copy.deepcopy(MODEL_STAGE_CATALOG),
            }

    def profile(self, profile_id: str) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._find(self._read(), profile_id))

    def profiles(self) -> list[dict[str, Any]]:
        return self.snapshot()["profiles"]

    def diagnose(self, profile_id: str, required_capabilities: list[str] | None = None) -> list[dict[str, str]]:
        with self._lock:
            return _profile_diagnostics(self._find(self._read(), profile_id), required_capabilities)

    def routing(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._read().get("routing") or _default_routing())

    def upsert_profile(self, request: ModelProfileDefinitionInput) -> dict[str, Any]:
        values = request.model_dump(mode="json")
        profile_id = values["profile_id"]
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,79}", profile_id):
            raise ModelRegistryError("模型 ID 只能包含字母、数字、下划线和短横线")
        with self._lock:
            document = self._read()
            existing = next((item for item in document["profiles"] if item.get("profile_id") == profile_id), None)
            now = _now()
            if existing is None:
                values.update({"version": 1, "created_at": now, "updated_at": now})
                document["profiles"].append(values)
            else:
                version = int(existing.get("version") or 1) + 1
                values.update({"version": version, "created_at": existing.get("created_at") or now, "updated_at": now})
                existing.clear()
                existing.update(values)
            document["version"] = int(document.get("version") or 1) + 1
            self._write(document)
            return self._public(self._find(document, profile_id))

    def update_routing(self, rules: list[ModelRoutingRuleInput]) -> dict[str, Any]:
        values = [item.model_dump(mode="json") for item in rules]
        stages = {str(item["stage"]) for item in values}
        known_stages = {str(item["stage"]) for item in MODEL_STAGE_CATALOG}
        unknown = stages - known_stages
        if unknown:
            raise ModelRegistryError(f"未知模型阶段：{sorted(unknown)[0]}")
        if len(stages) != len(values):
            raise ModelRegistryError("同一个模型阶段不能重复配置")
        with self._lock:
            document = self._read()
            document["routing"] = values
            document["version"] = int(document.get("version") or 1) + 1
            self._write(document)
            return self.snapshot()

    def resolve(self, profile_id: str, required_capabilities: list[str] | None = None) -> dict[str, Any]:
        profile = self.profile(profile_id)
        issues = _profile_diagnostics(profile, required_capabilities)
        if issues:
            detail = "；".join(item["message"] for item in issues)
            raise ModelRegistryError(f"模型档案“{profile_id}”不可用：{detail}")
        return profile

    def select(
        self,
        stage: str,
        *,
        query: str = "",
        attachments: list[dict[str, Any]] | None = None,
        complexity: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        complexity_info = complexity or classify_task_complexity(query, attachments)
        level = str(complexity_info.get("level") or "normal")
        rule = next((item for item in self.routing() if str(item.get("stage")) == stage), None)
        if not rule or rule.get("enabled") is False:
            rule = next((item for item in _default_routing() if item["stage"] == stage), None)
        if not rule:
            raise ModelRegistryError(f"没有找到模型阶段：{stage}")
        required = list(rule.get("required_capabilities") or [])
        if attachments and "vision" not in required:
            required.append("vision")
        requested_id = str(rule.get(f"{level}_profile") or rule.get("normal_profile") or "balanced")
        fallback_id = str(rule.get("fallback_profile") or "balanced")
        candidate_ids = [requested_id, fallback_id, "balanced"]
        selected = None
        used_fallback = False
        for candidate_id in dict.fromkeys(candidate_ids):
            try:
                profile = self.profile(candidate_id)
            except ModelRegistryError:
                continue
            if _profile_ready(profile, required):
                selected = profile
                used_fallback = candidate_id != requested_id
                break
        if selected is None:
            # Keep the error actionable.  The caller can still choose the
            # legacy global Qwen client when the registry is not configured.
            try:
                selected = self.profile(fallback_id)
            except ModelRegistryError:
                selected = {"profile_id": fallback_id, "model": "", "provider": ""}
            used_fallback = fallback_id != requested_id
        return {
            "stage": stage,
            "stage_name": next((item["name"] for item in MODEL_STAGE_CATALOG if item["stage"] == stage), stage),
            "complexity": complexity_info,
            "requested_profile_id": requested_id,
            "profile_id": selected.get("profile_id"),
            "profile_name": selected.get("name"),
            "model": selected.get("model"),
            "provider": selected.get("provider"),
            "required_capabilities": required,
            "fallback_used": used_fallback,
            "reason": "复杂度路由未命中可用档案，已回退" if used_fallback else "按阶段和任务复杂度选择",
        }


def classify_task_complexity(query: str, attachments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Score obvious complexity signals without adding another model call."""

    text = str(query or "").strip()
    score = 0
    signals: list[str] = []
    if len(text) >= 260:
        score += 1
        signals.append("问题较长")
    if len(text) >= 700:
        score += 1
        signals.append("上下文较长")
    question_count = text.count("?") + text.count("？")
    if question_count >= 2 or re.search(r"分别|各自|同时|并且|以及|比较|对比|差异|冲突|为什么.*以及", text):
        score += 1
        signals.append("包含多个子问题或比较关系")
    entity_candidates = set(re.findall(r"\b[A-Za-z]{1,12}[-_]?[A-Za-z0-9]{2,}\b", text))
    if len(entity_candidates) >= 2:
        score += 1
        signals.append("包含多个对象或编号")
    if attachments:
        score += 2
        signals.append("包含图片或多模态附件")
    if re.search(r"证据|依据|来源|页码|引用|校验|审计|无法确认|冲突", text):
        score += 1
        signals.append("需要证据判断")
    level = "complex" if score >= 4 else "normal" if score >= 2 else "simple"
    return {"level": level, "score": score, "signals": signals}


def plan_model_routing(
    query: str,
    *,
    attachments: list[dict[str, Any]] | None = None,
    stages: list[str] | None = None,
) -> dict[str, Any]:
    registry = get_model_registry()
    complexity = classify_task_complexity(query, attachments)
    stage_names = stages or [str(item["stage"]) for item in MODEL_STAGE_CATALOG]
    return {
        "complexity": complexity,
        "stages": {
            stage: registry.select(stage, query=query, attachments=attachments, complexity=complexity)
            for stage in stage_names
        },
    }


@lru_cache(maxsize=1)
def get_model_registry() -> ModelRegistry:
    return ModelRegistry()
