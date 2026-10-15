import pytest
from pydantic import ValidationError

from app import main
from app.conversation import MemoryConversationStore
from app.multimodal import multimodal_content
from app.schemas.query import QueryRequest


IMAGE = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB"


def _result():
    return {
        "answer": "已分析图片。",
        "tool_calls": [],
        "citations": [],
        "retrieval_hits": [],
        "evidence": {"status": "not_found", "candidate_count": 0},
        "claims": [],
        "requirements": [],
        "results": [],
        "coverage": None,
        "suggestions": [],
        "trace": {"route": "test", "steps": []},
    }


def test_query_request_accepts_bounded_image_attachment():
    request = QueryRequest(
        query="请分析这张设备照片",
        attachments=[{
            "name": "设备铭牌.png",
            "mime_type": "image/png",
            "data_url": IMAGE,
            "size_bytes": 128,
        }],
    )

    assert request.attachments[0].mime_type == "image/png"
    assert request.attachments[0].data_url == IMAGE


def test_query_request_rejects_non_image_and_too_many_attachments():
    with pytest.raises(ValidationError):
        QueryRequest(
            query="读取文件",
            attachments=[{
                "name": "manual.pdf",
                "mime_type": "application/pdf",
                "url": "https://example.com/manual.pdf",
            }],
        )

    with pytest.raises(ValidationError):
        QueryRequest(
            query="分析图片",
            attachments=[{
                "name": f"{index}.png",
                "mime_type": "image/png",
                "data_url": IMAGE,
            } for index in range(5)],
        )


def test_multimodal_content_uses_openai_image_blocks():
    content = multimodal_content(
        "识别设备型号",
        [{"name": "铭牌.png", "mime_type": "image/png", "data_url": IMAGE}],
    )

    assert content[0] == {"type": "text", "text": "识别设备型号"}
    assert content[1]["type"] == "image_url"
    assert content[1]["image_url"]["url"] == IMAGE


def test_chat_passes_images_to_agent_and_persists_them(monkeypatch):
    monkeypatch.setattr(main, "conversation_store", MemoryConversationStore())
    captured = {}

    def fake_run_agent(history, query, **kwargs):
        captured["history"] = history
        captured["query"] = query
        captured["attachments"] = kwargs.get("attachments")
        return _result()

    monkeypatch.setattr(main, "run_agent", fake_run_agent)
    response = main.chat(QueryRequest(
        query="这张图里的设备是什么？",
        attachments=[{
            "name": "铭牌.png",
            "mime_type": "image/png",
            "data_url": IMAGE,
        }],
    ))

    assert response.answer == "已分析图片。"
    assert captured["attachments"][0]["data_url"] == IMAGE
    stored = main.conversation_store.get(response.conversation_id)
    assert stored[0]["role"] == "user"
    assert stored[0]["attachments"][0]["name"] == "铭牌.png"

