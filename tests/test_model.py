from app.agents.model import create_qwen_model


def test_qwen_model_is_cached():
    assert create_qwen_model() is create_qwen_model()
