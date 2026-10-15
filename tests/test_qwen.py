import os

import pytest

from app.agents.model import create_qwen_model


@pytest.mark.skipif(os.getenv("RUN_LIVE_QWEN") != "1", reason="live Qwen test disabled by default")
def test_live_qwen():
    response = create_qwen_model().invoke("用一句话解释 Modbus TCP。")
    assert response.content
