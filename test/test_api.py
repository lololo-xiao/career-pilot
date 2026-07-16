import pytest
from fastapi.testclient import TestClient

from app.auth import AuthStore, AuthenticatedAccount, ProviderConnection
from app.main import app, get_matcher, get_store, require_current_account
from app.matching import MatchConfigurationError, MatchProviderError
from app.schemas import MatchRequest, MatchResponse


client = TestClient(app)

MATCH_REQUEST = {
    "candidate_profile": (
        "Python AI engineer with experience in RAG and LLM evaluation."
    ),
    "job_description": (
        "We are looking for an AI engineer with Python, Azure and Kubernetes."
    ),
}


def fake_matcher(request: MatchRequest) -> MatchResponse:
    return MatchResponse.model_validate(
        {
            "score": 5,
            "summary": "Python is supported, while Azure and Kubernetes are missing.",
            "matched_skills": [
                {
                    "skill": "Python",
                    "explanation": "Python directly satisfies a stated requirement.",
                    "evidence": [
                        {
                            "source_id": "candidate_profile",
                            "quote": "Python AI engineer",
                        }
                    ],
                }
            ],
            "adjacent_skills": [],
            "missing_skills": [
                {
                    "skill": "Azure",
                    "importance": "unspecified",
                    "explanation": "The candidate profile has no Azure evidence.",
                },
                {
                    "skill": "Kubernetes",
                    "importance": "unspecified",
                    "explanation": "The candidate profile has no Kubernetes evidence.",
                },
            ],
            "important_requirements": [
                {
                    "requirement": "Python",
                    "importance": "unspecified",
                    "evidence_quote": "Python, Azure and Kubernetes",
                }
            ],
            "preparation_actions": [
                {
                    "priority": 1,
                    "action": "Learn Kubernetes fundamentals.",
                    "rationale": "It is an uncovered job requirement.",
                    "addresses": ["Kubernetes"],
                }
            ],
            "unsupported_claim_warnings": [
                {
                    "claim": "Production Kubernetes experience",
                    "reason": "The candidate profile contains no Kubernetes evidence.",
                }
            ],
        }
    )


@pytest.fixture(autouse=True)
def replace_provider_matcher(tmp_path):
    store = AuthStore(tmp_path / "auth.db", "t" * 48)
    account = AuthenticatedAccount(
        user_id="test-user",
        email="person@example.com",
        display_name="Person",
        identity_method="local",
        active_provider="api_key",
        provider_connection=ProviderConnection(
            provider="api_key",
            credential=b"sk-test-key-with-enough-characters",
            plan_type="usage-based",
        ),
    )
    client.cookies.clear()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[require_current_account] = lambda: account
    app.dependency_overrides[get_matcher] = lambda: fake_matcher
    yield
    client.cookies.clear()
    app.dependency_overrides.clear()


def test_create_match_uses_network_free_dependency() -> None:
    response = client.post("/match", json=MATCH_REQUEST)

    assert response.status_code == 200
    payload = response.json()
    assert payload["score"] == 5
    assert payload["matched_skills"][0]["skill"] == "Python"
    assert payload["missing_skills"][0]["skill"] == "Azure"
    assert payload["unsupported_claim_warnings"]


def test_rejects_short_input() -> None:
    response = client.post(
        "/match",
        json={"candidate_profile": "Python", "job_description": "AI"},
    )

    assert response.status_code == 422


def test_health() -> None:
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_match_requires_provider_connection_without_override() -> None:
    app.dependency_overrides.pop(get_matcher)
    app.dependency_overrides.pop(require_current_account)

    response = client.post("/match", json=MATCH_REQUEST)

    assert response.status_code == 409
    assert response.json() == {
        "detail": "Choose an AI connection in Settings before running a match"
    }


def test_parse_text_cv() -> None:
    response = client.post(
        "/parse-cv",
        files={
            "file": (
                "candidate.txt",
                b"Python engineer with RAG and FastAPI experience.",
                "text/plain",
            )
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["filename"] == "candidate.txt"
    assert payload["file_type"] == "text"
    assert payload["text"] == "Python engineer with RAG and FastAPI experience."


def test_parse_cv_rejects_unsupported_extension() -> None:
    response = client.post(
        "/parse-cv",
        files={"file": ("candidate.png", b"not a CV", "image/png")},
    )

    assert response.status_code == 415
    assert "PDF, DOCX, and TXT" in response.json()["detail"]


def test_local_frontend_cors_preflight() -> None:
    response = client.options(
        "/match",
        headers={
            "Origin": "http://localhost:3000",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (MatchConfigurationError("OPENAI_API_KEY is not configured"), 503),
        (MatchProviderError("The model provider request failed"), 502),
    ],
)
def test_match_errors_become_safe_http_responses(
    error: Exception,
    expected_status: int,
) -> None:
    def failing_matcher(_: MatchRequest) -> MatchResponse:
        raise error

    app.dependency_overrides[get_matcher] = lambda: failing_matcher

    response = client.post("/match", json=MATCH_REQUEST)

    assert response.status_code == expected_status
    assert response.json() == {"detail": str(error)}
