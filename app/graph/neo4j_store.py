"""Neo4j 图谱存储和受控查询。

设计边界：

* Cypher 模板全部由服务端固定，用户和大模型只能提供参数；
* 节点统一使用 ``Entity`` 标签，实体类型保存在 ``entity_type`` 属性中，
  避免把外部输入拼接进标签或关系类型；
* 关系统一使用 ``RELATED`` 类型，业务关系名保存在 ``type`` 属性中，
  这样关系名称同样不会形成 Cypher 注入点；
* 写入使用幂等 ``MERGE``，并保留来源文档、页码、Chunk 和版本等证据字段。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from app.config import Settings, get_settings


class GraphStoreError(RuntimeError):
    """图谱配置、连接或数据校验错误。"""


TEXT_SEARCH_CYPHER = """
MATCH (n:Entity)-[r:RELATED]-(m:Entity)
WHERE (
    $query = ''
    OR toLower(coalesce(n.name, n.code, n.id, '')) CONTAINS toLower($query)
    OR toLower(coalesce(m.name, m.code, m.id, '')) CONTAINS toLower($query)
    OR toLower(coalesce(r.type, '')) CONTAINS toLower($query)
)
AND ($entity_id IS NULL OR n.id = $entity_id OR m.id = $entity_id)
AND ($device_id IS NULL OR n.id = $device_id OR m.id = $device_id
     OR n.device_id = $device_id OR m.device_id = $device_id)
AND ($alarm_code IS NULL OR n.code = $alarm_code OR m.code = $alarm_code)
AND ($fault_code IS NULL OR n.code = $fault_code OR m.code = $fault_code)
AND ($relation_type IS NULL OR r.type = $relation_type)
AND ($tenant_id IS NULL OR coalesce(n.tenant_id, '') = $tenant_id
     OR coalesce(m.tenant_id, '') = $tenant_id)
RETURN n, r, m
ORDER BY coalesce(n.mention_count, 0) + coalesce(m.mention_count, 0) DESC
LIMIT $limit
"""


ENTITY_RELATION_SEARCH_CYPHER = """
MATCH (n:Entity)-[r:RELATED]-(m:Entity)
WHERE (
    toLower(coalesce(n.name, '')) = toLower($entity)
    OR toLower(coalesce(n.id, '')) = toLower($entity)
    OR toLower(coalesce(m.name, '')) = toLower($entity)
    OR toLower(coalesce(m.id, '')) = toLower($entity)
)
AND ($tenant_id IS NULL OR coalesce(n.tenant_id, '') = $tenant_id
     OR coalesce(m.tenant_id, '') = $tenant_id)
AND (
    size($attribute_terms) = 0
    OR any(term IN $attribute_terms WHERE
        toLower(coalesce(n.name, '')) CONTAINS toLower(term)
        OR toLower(coalesce(n.code, '')) CONTAINS toLower(term)
        OR toLower(coalesce(n.id, '')) CONTAINS toLower(term)
        OR toLower(coalesce(m.name, '')) CONTAINS toLower(term)
        OR toLower(coalesce(m.code, '')) CONTAINS toLower(term)
        OR toLower(coalesce(m.id, '')) CONTAINS toLower(term)
        OR toLower(coalesce(r.type, '')) CONTAINS toLower(term)
        OR toLower(coalesce(r.excerpt, '')) CONTAINS toLower(term)
        OR toLower(coalesce(r.source_excerpt, '')) CONTAINS toLower(term)
        OR toLower(coalesce(r.evidence_records, '')) CONTAINS toLower(term)
    )
)
AND (
    size($attribute_terms) = 0
    OR NOT (toLower(coalesce(r.type, '')) IN [
        '包含', '组成', '包括', '拥有', '涉及', '关联',
        'contains', 'has', 'includes'
    ])
    OR any(term IN $attribute_terms WHERE
        toLower(coalesce(n.name, '')) CONTAINS toLower(term)
        OR toLower(coalesce(n.code, '')) CONTAINS toLower(term)
        OR toLower(coalesce(n.id, '')) CONTAINS toLower(term)
        OR toLower(coalesce(m.name, '')) CONTAINS toLower(term)
        OR toLower(coalesce(m.code, '')) CONTAINS toLower(term)
        OR toLower(coalesce(m.id, '')) CONTAINS toLower(term)
    )
)
AND (
    size($relation_types) = 0
    OR size($attribute_terms) > 0
    OR any(relation_type IN $relation_types WHERE
        toLower(coalesce(r.type, '')) CONTAINS toLower(relation_type)
    )
)
RETURN n, r, m
ORDER BY
    CASE
        WHEN toLower(coalesce(n.name, '')) = toLower($entity) THEN 0
        WHEN toLower(coalesce(m.name, '')) = toLower($entity) THEN 1
        ELSE 2
    END
LIMIT $limit
"""


PATH_SEARCH_CYPHER = """
MATCH p=(start:Entity)-[:RELATED*1..4]-(target:Entity)
WHERE start.id = $start_id
  AND ($target_type IS NULL OR target.entity_type = $target_type)
  AND ($tenant_id IS NULL OR coalesce(start.tenant_id, '') = $tenant_id
       OR coalesce(target.tenant_id, '') = $tenant_id)
RETURN p
LIMIT $limit
"""


UPSERT_NODES_CYPHER = """
UNWIND $nodes AS item
MERGE (n:Entity {id: item.id})
SET n.name = item.name,
    n.entity_type = item.entity_type,
    n.tenant_id = item.tenant_id,
    n.updated_at = $updated_at
SET n += item.properties
"""


UPSERT_RELATIONS_CYPHER = """
UNWIND $relations AS item
MATCH (source:Entity {id: item.source_id})
MATCH (target:Entity {id: item.target_id})
MERGE (source)-[r:RELATED {type: item.type}]->(target)
SET r.updated_at = $updated_at
SET r += item.properties
"""


class Neo4jGraphStore:
    """可选 Neo4j 存储。

    驱动采用懒加载，避免未安装驱动或未配置 Neo4j 时影响应用启动。
    """

    _RESERVED_NODE_PROPERTIES = {"id", "name", "entity_type", "tenant_id", "updated_at"}
    _RESERVED_RELATION_PROPERTIES = {"type", "updated_at"}

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._driver: Any | None = None

    @property
    def configured(self) -> bool:
        provider = str(getattr(self.settings, "graph_provider", "neo4j") or "neo4j").strip().lower()
        return provider == "neo4j" and all(
            str(getattr(self.settings, field, "") or "").strip()
            for field in ("neo4j_uri", "neo4j_username", "neo4j_password")
        )

    def _get_driver(self):
        if not self.configured:
            raise GraphStoreError("Neo4j 未配置，请设置 GRAPH_PROVIDER、NEO4J_URI、NEO4J_USERNAME 和 NEO4J_PASSWORD")
        if self._driver is not None:
            return self._driver
        try:
            from neo4j import GraphDatabase
        except ImportError as exc:
            raise GraphStoreError("Neo4j 连接器需要安装 neo4j 包") from exc
        self._driver = GraphDatabase.driver(
            str(self.settings.neo4j_uri),
            auth=(str(self.settings.neo4j_username), str(self.settings.neo4j_password)),
        )
        return self._driver

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def health(self) -> dict[str, Any]:
        """返回配置和连通性状态，不泄露 URI、用户名或密码。"""
        if not self.configured:
            return {"configured": False, "available": False, "status": "not_configured"}
        try:
            self._get_driver().verify_connectivity()
            return {"configured": True, "available": True, "status": "ok"}
        except Exception as exc:
            return {"configured": True, "available": False, "status": "error", "error": str(exc)}

    @staticmethod
    def _node_value(node: Any) -> dict[str, Any]:
        return dict(node or {})

    @staticmethod
    def _relation_value(relation: Any) -> dict[str, Any]:
        return dict(relation or {})

    def search(
        self,
        query: str,
        *,
        limit: int = 12,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        """执行固定模板的邻接关系检索。"""
        filters = filters or {}
        params = {
            "query": str(query or "").strip(),
            "entity_id": self._optional_filter(filters, "entity_id"),
            "device_id": self._optional_filter(filters, "device_id"),
            "alarm_code": self._optional_filter(filters, "alarm_code"),
            "fault_code": self._optional_filter(filters, "fault_code"),
            "relation_type": self._optional_filter(filters, "relation_type"),
            "tenant_id": self._optional_filter(filters, "tenant_id"),
            "limit": max(1, min(int(limit), 50)),
        }
        driver = self._get_driver()
        with driver.session() as session:
            # Neo4j Python 驱动的第一个位置参数也叫 query；把参数字典
            # 作为第二个位置参数传入，避免 ``query=`` 与 Cypher 中的
            # ``$query`` 发生 Python 方法签名冲突。
            records = session.run(TEXT_SEARCH_CYPHER, params)
            return [
                {
                    "left": self._node_value(record["n"]),
                    "right": self._node_value(record["m"]),
                    "relation": self._relation_value(record["r"]),
                }
                for record in records
            ]

    def search_entity_neighbors(
        self,
        entity: str,
        *,
        limit: int = 12,
        filters: dict[str, Any] | None = None,
        attribute_terms: list[str] | None = None,
        relation_types: list[str] | None = None,
    ) -> list[dict[str, Any]]:
        """按实体值查询邻接关系，不把完整自然语言问题当作关键词。

        这是语义路由检索计划的图谱入口。实体值作为参数传入固定
        Cypher 模板，模型或客户端永远不能提交任意 Cypher。
        """
        filters = filters or {}
        entity = str(entity or "").strip()
        if not entity:
            return []
        params = {
            "entity": entity,
            "tenant_id": self._optional_filter(filters, "tenant_id"),
            "attribute_terms": [
                str(item).strip()[:80]
                for item in (attribute_terms or [])
                if str(item).strip()
            ][:40],
            "relation_types": [
                str(item).strip()[:80]
                for item in (relation_types or [])
                if str(item).strip()
            ][:20],
            "limit": max(1, min(int(limit), 50)),
        }
        driver = self._get_driver()
        with driver.session() as session:
            records = session.run(ENTITY_RELATION_SEARCH_CYPHER, params)
            return [
                {
                    "left": self._node_value(record["n"]),
                    "right": self._node_value(record["m"]),
                    "relation": self._relation_value(record["r"]),
                }
                for record in records
            ]

    def find_paths(
        self,
        start_id: str,
        *,
        target_type: str | None = None,
        tenant_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """查询指定实体四跳以内的关系路径。"""
        if not str(start_id or "").strip():
            raise GraphStoreError("start_id 不能为空")
        driver = self._get_driver()
        with driver.session() as session:
            records = session.run(
                PATH_SEARCH_CYPHER,
                start_id=str(start_id).strip(),
                target_type=str(target_type).strip() if target_type else None,
                tenant_id=str(tenant_id).strip() if tenant_id else None,
                limit=max(1, min(int(limit), 50)),
            )
            paths: list[dict[str, Any]] = []
            for record in records:
                path = record["p"]
                nodes = [self._node_value(node) for node in path.nodes]
                relationships = [self._relation_value(rel) for rel in path.relationships]
                paths.append({"nodes": nodes, "relations": relationships})
            return paths

    def upsert(self, nodes: list[dict[str, Any]], relations: list[dict[str, Any]]) -> dict[str, int]:
        """幂等写入实体和关系，供受保护的导入任务调用。"""
        if len(nodes) > 5000 or len(relations) > 10000:
            raise GraphStoreError("单次图谱导入超过上限：最多 5000 个实体和 10000 条关系")
        normalized_nodes = [self._normalize_node(item) for item in nodes]
        normalized_relations = [self._normalize_relation(item) for item in relations]
        node_ids = {item["id"] for item in normalized_nodes}
        if not node_ids:
            raise GraphStoreError("至少需要一个实体")
        missing = {
            item["source_id"] for item in normalized_relations
        } | {item["target_id"] for item in normalized_relations}
        # 允许关系连接已存在节点，但阻止明显的批次内错误；数据库 MATCH
        # 仍会保证不存在的端点不会被自动创建。
        if not missing.issubset(node_ids):
            existing_ids = self._existing_ids(missing - node_ids)
            unresolved = sorted((missing - node_ids) - existing_ids)
            if unresolved:
                raise GraphStoreError(f"关系端点不存在：{', '.join(unresolved[:10])}")

        updated_at = datetime.now(UTC).isoformat()
        driver = self._get_driver()
        with driver.session() as session:
            tx = session.begin_transaction()
            try:
                tx.run(UPSERT_NODES_CYPHER, nodes=normalized_nodes, updated_at=updated_at)
                if normalized_relations:
                    tx.run(UPSERT_RELATIONS_CYPHER, relations=normalized_relations, updated_at=updated_at)
                tx.commit()
            except Exception:
                tx.rollback()
                raise
        return {"nodes_upserted": len(normalized_nodes), "relations_upserted": len(normalized_relations)}

    def _existing_ids(self, ids: set[str]) -> set[str]:
        if not ids:
            return set()
        driver = self._get_driver()
        with driver.session() as session:
            records = session.run("MATCH (n:Entity) WHERE n.id IN $ids RETURN n.id AS id", ids=sorted(ids))
            return {str(record["id"]) for record in records if record["id"]}

    @staticmethod
    def _optional_filter(filters: dict[str, Any], key: str) -> str | None:
        value = filters.get(key)
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    @classmethod
    def _normalize_node(cls, item: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise GraphStoreError("实体必须是对象")
        node_id = str(item.get("id") or "").strip()
        name = str(item.get("name") or node_id).strip()
        entity_type = str(item.get("type") or item.get("entity_type") or "Entity").strip()
        if not node_id or not name or not entity_type:
            raise GraphStoreError("实体必须包含 id、name 和 type")
        raw_properties = item.get("properties") or {}
        if not isinstance(raw_properties, dict):
            raise GraphStoreError(f"实体 {node_id} 的 properties 必须是对象")
        properties = {
            str(key): cls._property_value(value)
            for key, value in raw_properties.items()
            if str(key) not in cls._RESERVED_NODE_PROPERTIES
        }
        return {
            "id": node_id,
            "name": name,
            "entity_type": entity_type,
            "tenant_id": str(item.get("tenant_id") or "").strip() or None,
            "properties": properties,
        }

    @classmethod
    def _normalize_relation(cls, item: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(item, dict):
            raise GraphStoreError("关系必须是对象")
        source_id = str(item.get("source_id") or item.get("from") or "").strip()
        target_id = str(item.get("target_id") or item.get("to") or "").strip()
        relation_type = str(item.get("type") or item.get("relation") or "").strip()
        if not source_id or not target_id or not relation_type:
            raise GraphStoreError("关系必须包含 source_id、target_id 和 type")
        raw_properties = item.get("properties") or {}
        if not isinstance(raw_properties, dict):
            raise GraphStoreError(f"关系 {source_id}->{target_id} 的 properties 必须是对象")
        properties = {
            str(key): cls._property_value(value)
            for key, value in raw_properties.items()
            if str(key) not in cls._RESERVED_RELATION_PROPERTIES
        }
        return {
            "source_id": source_id,
            "target_id": target_id,
            "type": relation_type,
            "properties": properties,
        }

    @staticmethod
    def _property_value(value: Any) -> Any:
        """将 JSON 值转换为 Neo4j 可存储的属性类型。

        Neo4j 属性不能直接保存嵌套对象；证据元数据中的嵌套对象序列化为
        JSON 字符串，避免导入任务在运行到单条异常记录时整体失败。
        """
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False, default=str)
        if isinstance(value, (list, tuple)):
            return [Neo4jGraphStore._property_value(item) for item in value]
        if isinstance(value, (str, int, float, bool)) or value is None:
            return value
        return str(value)
