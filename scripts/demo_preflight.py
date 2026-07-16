"""Verify demo prerequisites without calling a paid model provider."""

import argparse
import os
import shutil
import sys
from dataclasses import dataclass
from urllib.error import URLError
from urllib.request import urlopen

from dotenv import load_dotenv

from app.matching import DEFAULT_OPENAI_MODEL
from app.observability import is_langfuse_enabled
from app.prompts import MATCH_PROMPT_VERSION
from app.retrieval import retrieve_candidate_evidence
from app.workflow import MATCH_WORKFLOW_VERSION
from evals.run_evals import load_cases


@dataclass(frozen=True)
class Check:
    label: str
    passed: bool
    detail: str
    required: bool = True


def _check_url(label: str, url: str, expected_text: str) -> Check:
    try:
        with urlopen(url, timeout=5) as response:  # noqa: S310 - explicit local/deploy URL
            body = response.read().decode("utf-8", errors="replace")
        passed = 200 <= response.status < 400 and expected_text in body
        detail = f"HTTP {response.status}"
    except (OSError, URLError) as exc:
        passed = False
        detail = str(exc)
    return Check(label, passed, detail)


def run_preflight(
    *,
    api_url: str | None = None,
    frontend_url: str | None = None,
    warm_embeddings: bool = True,
) -> list[Check]:
    load_dotenv()
    checks: list[Check] = []

    checks.append(
        Check(
            "Python",
            sys.version_info >= (3, 12),
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        )
    )
    auth_secret = os.getenv("CAREERPILOT_AUTH_SECRET", "")
    checks.append(
        Check(
            "Local provider encryption",
            len(auth_secret) >= 32
            and auth_secret != "replace-with-at-least-32-random-characters",
            "secret configured"
            if len(auth_secret) >= 32
            and auth_secret != "replace-with-at-least-32-random-characters"
            else "CAREERPILOT_AUTH_SECRET is missing or still a placeholder",
        )
    )
    codex_binary = os.getenv("CODEX_BINARY", "codex")
    checks.append(
        Check(
            "Codex runtime",
            shutil.which(codex_binary) is not None,
            shutil.which(codex_binary) or f"{codex_binary} not found",
        )
    )
    checks.append(
        Check(
            "Langfuse configuration",
            is_langfuse_enabled(),
            "enabled" if is_langfuse_enabled() else "optional keys not configured",
            required=False,
        )
    )

    cases = load_cases()
    categories = sorted({case.category for case in cases})
    checks.append(
        Check(
            "Evaluation dataset",
            len(cases) >= 3 and len(categories) == 3,
            f"{len(cases)} cases: {', '.join(categories)}",
        )
    )

    if warm_embeddings:
        try:
            profile = (
                "Python engineer who built RAG systems with Chroma and FastAPI. "
                + "backend " * 120
                + ". Finance analyst using spreadsheets and forecasting. "
                + "accounting " * 120
                + "."
            )
            evidence = retrieve_candidate_evidence(
                profile,
                "Python RAG vector database engineer",
                max_results=1,
            )
            passed = bool(evidence and "Python engineer" in evidence[0].text)
            detail = evidence[0].source_id if evidence else "no result"
        except Exception as exc:  # Preflight must report a useful failure, not a traceback.
            passed = False
            detail = f"{type(exc).__name__}: {exc}"
        checks.append(Check("Local MiniLM + Chroma", passed, detail))

    if api_url:
        checks.append(_check_url("FastAPI health", f"{api_url.rstrip('/')}/health", '"ok"'))
    if frontend_url:
        checks.append(
            _check_url(
                "Frontend",
                frontend_url.rstrip("/") or frontend_url,
                "CareerPilot",
            )
        )

    model = os.getenv("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL
    checks.append(
        Check(
            "Build Week model",
            model.startswith("gpt-5.6"),
            model,
        )
    )
    checks.append(
        Check(
            "Release versions",
            True,
            f"model={model}, prompt={MATCH_PROMPT_VERSION}, workflow={MATCH_WORKFLOW_VERSION}",
        )
    )
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", help="Optionally verify a running API URL.")
    parser.add_argument("--frontend-url", help="Optionally verify a running frontend URL.")
    parser.add_argument(
        "--skip-embedding-warmup",
        action="store_true",
        help="Skip the real local embedding check.",
    )
    args = parser.parse_args()

    checks = run_preflight(
        api_url=args.api_url,
        frontend_url=args.frontend_url,
        warm_embeddings=not args.skip_embedding_warmup,
    )
    for check in checks:
        status = "PASS" if check.passed else ("WARN" if not check.required else "FAIL")
        print(f"{status:4} {check.label}: {check.detail}")

    return 1 if any(check.required and not check.passed for check in checks) else 0


if __name__ == "__main__":
    raise SystemExit(main())
