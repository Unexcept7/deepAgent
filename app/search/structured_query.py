"""安全的结构化数据查询边界。

结构化数据不是文档切片的另一种写法。Agent 只提出逻辑查询计划，连接器
和企业侧网关共同负责数据集/字段白名单、行数上限、租户上下文和证据归属。
本模块不解析或拼接 SQL。
"""

from __future__ import annotations

import json
from typing import Any

from app.search.models import SearchHit, SearchRequest, StructuredQueryPlan


class StructuredQueryPolicyError(ValueError):
    """Raised when a logical query falls outside a connection/application scope."""


def _as_scope(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _name_set(value: Any) -> set[str] | None:
    if not isinstance(value, (list, tuple, set)):
        return None
    return {str(item).strip() for item in value if str(item).strip()}


def _field_map(value: Any) -> dict[str, set[str]] | None:
    if not isinstance(value, dict):
        return None
    result: dict[str, set[str]] = {}
    for dataset, fields in value.items():
        if isinstance(fields, (list, tuple, set)):
            result[str(dataset)] = {
                str(field).strip() for field in fields if str(field).strip()
            }
    return result


def merge_sql_scope(connection_config: dict[str, Any] | None, application_scope: dict[str, Any] | None = None) -> dict[str, Any]:
    """Merge application restrictions over a connection's default policy.

    An application scope may narrow a connection, never widen it. The actual
    intersection check is performed by ``validate_structured_queries``.
    """

    config = dict(connection_config or {})
    base_datasets = config.get("datasets")
    app_scope = _as_scope(application_scope)
    app_datasets = app_scope.get("datasets")
    base_definitions = _dataset_definitions(config)
    app_definitions = _dataset_definitions(app_scope)
    if app_datasets is not None:
        narrowed: dict[str, Any] = {}
        candidate_names = set(app_definitions)
        if base_datasets is not None:
            candidate_names &= set(base_definitions)
        for name in sorted(candidate_names):
            app_definition = app_definitions.get(name) or {}
            if base_datasets is not None and name not in base_definitions:
                continue
            base_definition = base_definitions.get(name) or {}
            if isinstance(base_definition, dict) and isinstance(app_definition, dict):
                base_fields = set(str(item) for item in base_definition.get("fields") or [])
                app_fields = set(str(item) for item in app_definition.get("fields") or [])
                fields = sorted(base_fields & app_fields) if base_fields and app_fields else sorted(base_fields or app_fields)
                narrowed[name] = {**base_definition, **app_definition}
                if fields:
                    narrowed[name]["fields"] = fields
            else:
                narrowed[name] = app_definition
        config["datasets"] = narrowed

    base_allowed_datasets = _name_set(config.get("allowed_datasets"))
    app_allowed_datasets = _name_set(app_scope.get("allowed_datasets"))
    if app_allowed_datasets is not None:
        config["allowed_datasets"] = sorted(
            app_allowed_datasets
            if base_allowed_datasets is None
            else app_allowed_datasets & base_allowed_datasets
        )

    base_allowed_fields = _field_map(config.get("allowed_fields"))
    app_allowed_fields = _field_map(app_scope.get("allowed_fields"))
    if app_allowed_fields is not None:
        if base_allowed_fields is None:
            config["allowed_fields"] = {
                dataset: sorted(fields)
                for dataset, fields in app_allowed_fields.items()
            }
        else:
            config["allowed_fields"] = {
                dataset: sorted(fields & base_allowed_fields.get(dataset, set()))
                for dataset, fields in app_allowed_fields.items()
                if dataset in base_allowed_fields
            }
    if "max_rows" in app_scope:
        try:
            config["max_rows"] = min(int(config.get("max_rows") or 200), int(app_scope["max_rows"]))
        except (TypeError, ValueError):
            raise StructuredQueryPolicyError("结构化数据范围的 max_rows 无效")
    if "allow_natural_query" in app_scope:
        # An application may turn an existing compatibility switch off, but
        # cannot turn it on when the connection did not explicitly publish it.
        config["allow_natural_query"] = bool(config.get("allow_natural_query", False) and app_scope["allow_natural_query"])
    return config


def _dataset_definitions(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    datasets = config.get("datasets")
    if isinstance(datasets, dict):
        return {
            str(name): definition if isinstance(definition, dict) else {}
            for name, definition in datasets.items()
            if str(name).strip()
        }
    if isinstance(datasets, list):
        return {str(name): {} for name in datasets if str(name).strip()}
    return {}


def _allowed_datasets(config: dict[str, Any]) -> set[str]:
    defined = set(_dataset_definitions(config))
    explicit = _name_set(config.get("allowed_datasets"))
    if explicit is not None:
        return explicit & defined if defined else explicit
    return defined


def _allowed_fields(config: dict[str, Any], dataset: str) -> set[str]:
    definition = _dataset_definitions(config).get(dataset) or {}
    definition_fields = {
        str(item).strip() for item in definition.get("fields") or [] if str(item).strip()
    }
    value = config.get("allowed_fields")
    explicit_fields: set[str] = set()
    explicit_present = False
    if isinstance(value, dict):
        if dataset in value:
            explicit_present = True
            fields = value.get(dataset) or []
            explicit_fields.update(str(item).strip() for item in fields if str(item).strip())
    elif isinstance(value, (list, tuple, set)):
        explicit_present = True
        explicit_fields.update(str(item).strip() for item in value if str(item).strip())
    if explicit_present and definition_fields:
        return explicit_fields & definition_fields
    if explicit_present:
        return explicit_fields
    return definition_fields


def validate_structured_queries(
    plans: list[StructuredQueryPlan],
    config: dict[str, Any] | None = None,
    *,
    require_allowlist: bool = False,
) -> list[StructuredQueryPlan]:
    """Validate plans against a connection/application logical-data policy."""

    config = dict(config or {})
    allowed_datasets = _allowed_datasets(config)
    if require_allowlist and not allowed_datasets:
        raise StructuredQueryPolicyError("生产 SQL 连接未配置数据集白名单")
    result: list[StructuredQueryPlan] = []
    for plan in plans:
        if allowed_datasets and plan.dataset not in allowed_datasets:
            raise StructuredQueryPolicyError(f"结构化数据集未授权：{plan.dataset}")
        fields = _allowed_fields(config, plan.dataset)
        if not fields:
            if require_allowlist:
                raise StructuredQueryPolicyError(f"数据集未配置字段白名单：{plan.dataset}")
            result.append(plan)
            continue
        requested = set(plan.select) | set(plan.group_by) | {item.field for item in plan.filters}
        requested.update(item.field for item in plan.order_by)
        requested.update(item.field for item in plan.aggregations if item.field != "*")
        unknown = sorted(item for item in requested if item not in fields)
        if unknown:
            raise StructuredQueryPolicyError(f"结构化查询字段未授权：{plan.dataset}.{unknown[0]}")
        result.append(plan)
    return result


def structured_query_payload(plans: list[StructuredQueryPlan]) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in plans]


def sql_request_payload(
    request: SearchRequest,
    plans: list[StructuredQueryPlan],
    *,
    max_rows: int = 100,
) -> dict[str, Any]:
    """Build the gateway contract without allowing caller-controlled identity."""

    limit = min(max(1, int(request.top_k)), max(1, min(int(max_rows), 200)))
    return {
        "query": request.query,
        "structured_queries": structured_query_payload(plans),
        "filters": request.filters,
        "limit": limit,
        "top_k": limit,
        "context": {
            "tenant_id": request.tenant_id,
            "user_id": request.user_id,
            "app_id": request.app_id,
        },
    }


def _updated_value(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if item.get(key) is not None:
            return item.get(key)
    return None


def normalize_sql_hits(
    payload: Any,
    *,
    source_name: str,
    evidence_prefix: str,
    scrubber=None,
) -> list[SearchHit]:
    """Normalize gateway rows while preserving stable row-level provenance."""

    if isinstance(payload, dict):
        items = payload.get("items") or payload.get("rows") or payload.get("hits") or []
        query_id = payload.get("query_id")
        data_as_of = payload.get("data_as_of")
        response_schema = payload.get("schema_version")
    else:
        items = payload
        query_id = None
        data_as_of = None
        response_schema = None
    if not isinstance(items, list):
        raise ValueError("SQL 搜索网关返回的 items 必须是数组")

    hits: list[SearchHit] = []
    for index, raw in enumerate(items):
        if not isinstance(raw, dict):
            continue
        item = scrubber(raw) if scrubber else raw
        if not isinstance(item, dict):
            continue
        fields = item.get("fields") if isinstance(item.get("fields"), dict) else None
        content_value = item.get("content") or item.get("summary")
        if content_value is None:
            content_value = fields if fields is not None else item
        evidence_id = str(item.get("evidence_id") or "").strip()
        row_key = str(item.get("row_key") or item.get("id") or "").strip()
        if not evidence_id:
            evidence_id = f"{evidence_prefix}:{row_key or index + 1}"
        metadata = {
            **item,
            "row_key": row_key or None,
            "query_id": item.get("query_id") or query_id,
            "data_as_of": item.get("data_as_of") or data_as_of,
            "schema_version": item.get("schema_version") or response_schema,
            "fields": fields,
        }
        hits.append(SearchHit(
            source_type="sql",
            source_name=str(item.get("source_name") or source_name),
            title=str(item.get("title") or item.get("name") or item.get("dataset") or "结构化记录"),
            content=str(content_value if isinstance(content_value, str) else json.dumps(content_value, ensure_ascii=False, default=str)),
            score=float(item.get("score") or 0.85),
            authority=float(item.get("authority") or 0.9),
            freshness=float(item.get("freshness") or 0.95),
            entity_match=float(item.get("entity_match") or 0.8),
            updated_at=_updated_value(item, "updated_at", "occurred_at", "created_at") or data_as_of,
            version=str(item.get("version") or "") or None,
            evidence_id=evidence_id,
            url=str(item.get("url") or item.get("source_url") or "") or None,
            metadata=metadata,
        ))
    return hits
