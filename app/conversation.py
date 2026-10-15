import copy
import json
import os
from collections import defaultdict
from datetime import datetime, timezone
from threading import Lock
from typing import Any

from dotenv import load_dotenv

load_dotenv()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _title_from_content(content: str) -> str:
    compact = " ".join(content.split())
    return compact[:28] + ("…" if len(compact) > 28 else "")


class ConversationAccessError(ValueError):
    """尝试从错误的 Agent 访问会话时抛出。"""


def _assert_conversation_access(
    meta: dict[str, Any] | None,
    app_id: str | None,
) -> None:
    """校验会话是否属于当前 Agent。

    没有 app_id 的旧会话只允许继续走旧的无作用域调用；一旦会话已经
    绑定 Agent，就必须带上同一个 app_id，避免跨 Agent 复用上下文。
    """
    if not meta:
        return
    owner = meta.get("app_id")
    if owner != app_id:
        if owner is None and app_id is not None:
            raise ConversationAccessError("会话尚未绑定当前 Agent")
        raise ConversationAccessError("会话属于其他 Agent")


def _conversation_meta(
    conversation_id: str,
    title: str,
    app_id: str | None,
    now: str,
) -> dict[str, Any]:
    meta: dict[str, Any] = {
        "conversation_id": conversation_id,
        "title": title,
        "created_at": now,
        "updated_at": now,
    }
    if app_id is not None:
        meta["app_id"] = app_id
    return meta


class MemoryConversationStore:
    """开发环境回退存储。进程重启后会话会清空。"""

    def __init__(self, max_messages: int = 12, max_conversations: int = 10):
        self._data: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._meta: dict[str, dict[str, Any]] = {}
        self._context_summaries: dict[str, dict[str, Any]] = {}
        self._order: dict[str, int] = {}
        self._counter = 0
        self._lock = Lock()
        self.max_messages = max_messages
        self.max_conversations = max_conversations

    def create(
        self,
        conversation_id: str,
        title: str = "新对话",
        app_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            now = _now_iso()
            meta = self._meta.get(conversation_id)
            if meta is None:
                meta = _conversation_meta(conversation_id, title, app_id, now)
                self._meta[conversation_id] = meta
                self._data[conversation_id] = []
            else:
                _assert_conversation_access(meta, app_id)
            self._counter += 1
            self._order[conversation_id] = self._counter
            self._prune_locked()
            return dict(meta)

    def list_conversations(self, app_id: str | None = None) -> list[dict[str, Any]]:
        with self._lock:
            ordered = sorted(
                self._meta.values(),
                key=lambda item: self._order.get(item["conversation_id"], 0),
                reverse=True,
            )
            if app_id is not None:
                ordered = [item for item in ordered if item.get("app_id") == app_id]
            return [
                dict(item)
                for item in ordered[:self.max_conversations]
            ]

    def get_metadata(
        self,
        conversation_id: str,
        app_id: str | None = None,
    ) -> dict[str, Any] | None:
        with self._lock:
            meta = self._meta.get(conversation_id)
            _assert_conversation_access(meta, app_id)
            return dict(meta) if meta else None

    def get(
        self,
        conversation_id: str,
        app_id: str | None = None,
    ) -> list[dict[str, Any]]:
        with self._lock:
            _assert_conversation_access(self._meta.get(conversation_id), app_id)
            return list(self._data.get(conversation_id, []))

    def get_context_summary(
        self,
        conversation_id: str,
        app_id: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            _assert_conversation_access(self._meta.get(conversation_id), app_id)
            return copy.deepcopy(self._context_summaries.get(conversation_id) or {})

    def save_context_summary(
        self,
        conversation_id: str,
        summary: dict[str, Any],
        app_id: str | None = None,
    ) -> None:
        with self._lock:
            _assert_conversation_access(self._meta.get(conversation_id), app_id)
            self._context_summaries[conversation_id] = copy.deepcopy(summary or {})

    def append(
        self,
        conversation_id: str,
        role: str,
        content: str,
        app_id: str | None = None,
        **metadata: Any,
    ) -> None:
        with self._lock:
            now = _now_iso()
            meta = self._meta.get(conversation_id)
            if meta is None:
                meta = _conversation_meta(conversation_id, "新对话", app_id, now)
                self._meta[conversation_id] = meta
            else:
                _assert_conversation_access(meta, app_id)
            if role == "user" and meta["title"] == "新对话":
                meta["title"] = _title_from_content(content)
            meta["updated_at"] = now
            self._counter += 1
            self._order[conversation_id] = self._counter

            message = {"role": role, "content": content}
            message.update({key: value for key, value in metadata.items() if value is not None})
            messages = self._data[conversation_id]
            messages.append(message)
            self._data[conversation_id] = messages[-self.max_messages:]
            self._prune_locked()

    def touch(
        self,
        conversation_id: str,
        app_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Refresh the conversation activity time without adding a message."""
        with self._lock:
            meta = self._meta.get(conversation_id)
            if meta is None:
                return None
            _assert_conversation_access(meta, app_id)
            meta["updated_at"] = _now_iso()
            self._counter += 1
            self._order[conversation_id] = self._counter
            self._prune_locked()
            return dict(meta)

    def clear(self, conversation_id: str, app_id: str | None = None) -> None:
        with self._lock:
            _assert_conversation_access(self._meta.get(conversation_id), app_id)
            self._data.pop(conversation_id, None)
            self._meta.pop(conversation_id, None)
            self._order.pop(conversation_id, None)
            self._context_summaries.pop(conversation_id, None)

    def bind_legacy(self, app_id: str) -> int:
        """将隔离改造前的无归属会话迁移到默认 Agent。"""
        with self._lock:
            count = 0
            for meta in self._meta.values():
                if meta.get("app_id") is None:
                    meta["app_id"] = app_id
                    count += 1
            return count

    def _prune_locked(self) -> None:
        ordered = sorted(
            self._meta.values(),
            key=lambda item: self._order.get(item["conversation_id"], 0),
            reverse=True,
        )
        for item in ordered[self.max_conversations:]:
            conversation_id = item["conversation_id"]
            self._meta.pop(conversation_id, None)
            self._data.pop(conversation_id, None)
            self._order.pop(conversation_id, None)
            self._context_summaries.pop(conversation_id, None)


class RedisConversationStore:
    """Redis 会话存储，保存最近 10 个对话及每个对话的短期上下文。"""

    INDEX_KEY = "deepagent:conversations"

    def __init__(
        self,
        url: str,
        max_messages: int = 12,
        ttl: int = 86400,
        max_conversations: int = 10,
    ):
        try:
            import redis
        except ImportError as exc:
            raise RuntimeError("Redis 模式需要先安装 redis：python -m pip install redis") from exc
        self.client = redis.Redis.from_url(url, decode_responses=True)
        self.client.ping()
        self.max_messages = max_messages
        self.ttl = ttl
        self.max_conversations = max_conversations

    def _messages_key(self, conversation_id: str) -> str:
        return f"deepagent:conversation:{conversation_id}"

    def _meta_key(self, conversation_id: str) -> str:
        return f"deepagent:conversation_meta:{conversation_id}"

    def _context_summary_key(self, conversation_id: str) -> str:
        return f"deepagent:conversation_context:{conversation_id}"

    def create(
        self,
        conversation_id: str,
        title: str = "新对话",
        app_id: str | None = None,
    ) -> dict[str, Any]:
        existing = self.client.hgetall(self._meta_key(conversation_id))
        now = _now_iso()
        if existing:
            _assert_conversation_access(existing, app_id)
            meta = dict(existing)
        else:
            meta = _conversation_meta(conversation_id, title, app_id, now)
        order = self.client.incr(f"{self.INDEX_KEY}:sequence")
        pipeline = self.client.pipeline()
        pipeline.hset(self._meta_key(conversation_id), mapping=meta)
        pipeline.expire(self._meta_key(conversation_id), self.ttl)
        pipeline.zadd(self.INDEX_KEY, {conversation_id: order})
        pipeline.execute()
        self._prune()
        return meta

    def list_conversations(self, app_id: str | None = None) -> list[dict[str, Any]]:
        # 先取完整索引再按 app_id 过滤，否则其他 Agent 的最新会话可能
        # 占满前 max_conversations 个位置，导致当前 Agent 看不到自己的历史。
        conversation_ids = self.client.zrevrange(self.INDEX_KEY, 0, -1)
        result: list[dict[str, Any]] = []
        stale: list[str] = []
        for conversation_id in conversation_ids:
            meta = self.client.hgetall(self._meta_key(conversation_id))
            if meta:
                if app_id is None or meta.get("app_id") == app_id:
                    result.append(meta)
                    if len(result) >= self.max_conversations:
                        break
            else:
                stale.append(conversation_id)
        if stale:
            self.client.zrem(self.INDEX_KEY, *stale)
        return result

    def get_metadata(
        self,
        conversation_id: str,
        app_id: str | None = None,
    ) -> dict[str, Any] | None:
        meta = self.client.hgetall(self._meta_key(conversation_id))
        if not meta:
            return None
        _assert_conversation_access(meta, app_id)
        return meta

    def get(
        self,
        conversation_id: str,
        app_id: str | None = None,
    ) -> list[dict[str, Any]]:
        self.get_metadata(conversation_id, app_id=app_id)
        raw = self.client.get(self._messages_key(conversation_id))
        return json.loads(raw) if raw else []

    def get_context_summary(
        self,
        conversation_id: str,
        app_id: str | None = None,
    ) -> dict[str, Any]:
        self.get_metadata(conversation_id, app_id=app_id)
        raw = self.client.get(self._context_summary_key(conversation_id))
        if not raw:
            return {}
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return value if isinstance(value, dict) else {}

    def save_context_summary(
        self,
        conversation_id: str,
        summary: dict[str, Any],
        app_id: str | None = None,
    ) -> None:
        self.get_metadata(conversation_id, app_id=app_id)
        self.client.setex(
            self._context_summary_key(conversation_id),
            self.ttl,
            json.dumps(summary or {}, ensure_ascii=False),
        )

    def append(
        self,
        conversation_id: str,
        role: str,
        content: str,
        app_id: str | None = None,
        **metadata: Any,
    ) -> None:
        meta = self.get_metadata(conversation_id, app_id=app_id)
        if not meta:
            meta = self.create(conversation_id, app_id=app_id)
        if role == "user" and meta.get("title") == "新对话":
            meta["title"] = _title_from_content(content)
        meta["updated_at"] = _now_iso()

        message = {"role": role, "content": content}
        message.update({key: value for key, value in metadata.items() if value is not None})
        messages = self.get(conversation_id, app_id=app_id)
        messages.append(message)

        pipeline = self.client.pipeline()
        pipeline.setex(
            self._messages_key(conversation_id),
            self.ttl,
            json.dumps(messages[-self.max_messages:], ensure_ascii=False),
        )
        pipeline.hset(self._meta_key(conversation_id), mapping=meta)
        pipeline.expire(self._meta_key(conversation_id), self.ttl)
        order = self.client.incr(f"{self.INDEX_KEY}:sequence")
        pipeline.zadd(self.INDEX_KEY, {conversation_id: order})
        pipeline.execute()
        self._prune()

    def touch(
        self,
        conversation_id: str,
        app_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Refresh the conversation activity time without adding a message."""
        meta = self.get_metadata(conversation_id, app_id=app_id)
        if not meta:
            return None
        now = _now_iso()
        meta["updated_at"] = now
        order = self.client.incr(f"{self.INDEX_KEY}:sequence")
        pipeline = self.client.pipeline()
        pipeline.hset(self._meta_key(conversation_id), "updated_at", now)
        pipeline.expire(self._meta_key(conversation_id), self.ttl)
        pipeline.expire(self._messages_key(conversation_id), self.ttl)
        pipeline.expire(self._context_summary_key(conversation_id), self.ttl)
        pipeline.zadd(self.INDEX_KEY, {conversation_id: order})
        pipeline.execute()
        self._prune()
        return meta

    def clear(self, conversation_id: str, app_id: str | None = None) -> None:
        self.get_metadata(conversation_id, app_id=app_id)
        pipeline = self.client.pipeline()
        pipeline.delete(self._messages_key(conversation_id), self._meta_key(conversation_id))
        pipeline.delete(self._context_summary_key(conversation_id))
        pipeline.zrem(self.INDEX_KEY, conversation_id)
        pipeline.execute()

    def bind_legacy(self, app_id: str) -> int:
        """将隔离改造前的无归属 Redis 会话迁移到默认 Agent。"""
        count = 0
        conversation_ids = self.client.zrange(self.INDEX_KEY, 0, -1)
        pipeline = self.client.pipeline()
        for conversation_id in conversation_ids:
            meta = self.client.hgetall(self._meta_key(conversation_id))
            if meta and meta.get("app_id") is None:
                pipeline.hset(self._meta_key(conversation_id), "app_id", app_id)
                pipeline.expire(self._meta_key(conversation_id), self.ttl)
                count += 1
        if count:
            pipeline.execute()
        return count

    def _prune(self) -> None:
        stale_ids = self.client.zrevrange(self.INDEX_KEY, self.max_conversations, -1)
        if not stale_ids:
            return
        pipeline = self.client.pipeline()
        for conversation_id in stale_ids:
            pipeline.delete(
                self._messages_key(conversation_id),
                self._meta_key(conversation_id),
                self._context_summary_key(conversation_id),
            )
        pipeline.zrem(self.INDEX_KEY, *stale_ids)
        pipeline.execute()


def _create_store():
    max_messages = int(os.getenv("CONTEXT_MAX_MESSAGES", "12"))
    max_conversations = int(os.getenv("MAX_CONVERSATIONS", "10"))
    ttl = int(os.getenv("CONVERSATION_TTL_SECONDS", "86400"))
    redis_url = os.getenv("REDIS_URL", "").strip()
    if redis_url:
        try:
            return RedisConversationStore(redis_url, max_messages, ttl, max_conversations)
        except Exception as exc:
            print(f"Redis unavailable, using memory store: {exc}")
    return MemoryConversationStore(max_messages, max_conversations)


conversation_store = _create_store()
try:
    # 旧版本只有一个企业知识助手，迁移后仍归属于这个默认 Agent；
    # 新会话不会再走这条路径。
    conversation_store.bind_legacy(
        os.getenv("LEGACY_CONVERSATION_APP_ID", "enterprise-knowledge-assistant")
    )
except Exception as exc:
    print(f"Unable to migrate legacy conversations: {exc}")
# 兼容原有测试和调用方。
ConversationStore = MemoryConversationStore
