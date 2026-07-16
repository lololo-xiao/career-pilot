from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.companion import (
    CompanionProviderError,
    build_companion_prompt,
    chat_with_companion,
)
from app.main import app, get_companion
from app.schemas import CompanionChatRequest, CompanionChatResponse


client = TestClient(app)

CHAT_REQUEST = {
    "message": "What should I focus on first?",
    "conversation": [
        {"role": "assistant", "content": "Let's work through this together."}
    ],
    "candidate_profile": "Python engineer with hands-on RAG delivery experience.",
    "job_description": "Seeking an AI engineer with Python, RAG, and Kubernetes.",
}


def fake_companion(_: CompanionChatRequest) -> CompanionChatResponse:
    return CompanionChatResponse(
        message="Let's start with the Kubernetes gap because the role names it directly.",
        suggested_prompts=["Build me a one-week plan", "Review my strongest evidence"],
    )


@pytest.fixture(autouse=True)
def replace_companion():
    app.dependency_overrides[get_companion] = lambda: fake_companion
    yield
    app.dependency_overrides.clear()


def test_companion_endpoint_returns_bounded_reply() -> None:
    response = client.post("/companion/chat", json=CHAT_REQUEST)

    assert response.status_code == 200
    assert "Kubernetes gap" in response.json()["message"]
    assert len(response.json()["suggested_prompts"]) == 2


def test_companion_endpoint_maps_provider_errors() -> None:
    def fail(_: CompanionChatRequest) -> CompanionChatResponse:
        raise CompanionProviderError("Pilot could not reach the model provider")

    app.dependency_overrides[get_companion] = lambda: fail
    response = client.post("/companion/chat", json=CHAT_REQUEST)

    assert response.status_code == 502
    assert response.json()["detail"] == "Pilot could not reach the model provider"


def test_companion_prompt_keeps_context_in_named_json_fields() -> None:
    request = CompanionChatRequest.model_validate(CHAT_REQUEST).model_copy(
        update={"session_id": "00000000-0000-0000-0000-000000000001"}
    )
    prompt = build_companion_prompt(request)

    assert '"source_session": "00000000-0000-0000-0000-000000000001"' in prompt
    assert '"candidate_profile"' in prompt
    assert '"latest_user_message": "What should I focus on first?"' in prompt


def test_openai_companion_uses_strict_schema_without_network() -> None:
    expected = CompanionChatResponse(
        message="We have a solid Python base. Next, let's make the RAG evidence sharper.",
        suggested_prompts=["Show me how", "Run a fit check"],
    )

    class FakeCompletions:
        def create(self, **kwargs):
            assert kwargs["response_format"]["json_schema"]["strict"] is True
            assert kwargs["model"] == "test-model"
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            refusal=None,
                            content=expected.model_dump_json(),
                        )
                    )
                ]
            )

    fake_client = SimpleNamespace(
        chat=SimpleNamespace(completions=FakeCompletions())
    )
    result = chat_with_companion(
        CompanionChatRequest.model_validate(CHAT_REQUEST),
        client=fake_client,
        model="test-model",
    )

    assert result == expected
