"""企业知识图谱适配层。

RAGFlow 负责文档解析和证据检索；本包负责可选的 Neo4j 企业关系图谱。
图谱未配置时，应用仍可使用 RAGFlow 和其他搜索源。
"""

from app.graph.neo4j_store import Neo4jGraphStore

__all__ = ["Neo4jGraphStore"]
