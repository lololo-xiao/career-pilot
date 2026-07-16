from collections.abc import Sequence

import pytest

from app.retrieval import chunk_candidate_profile, retrieve_candidate_evidence


def test_chunk_candidate_profile_preserves_order_and_limits() -> None:
    profile = " ".join(
        [
            "Built Python APIs and retrieval systems.",
            "Measured model quality with fixed evaluation sets.",
            "Created a React interface for internal users.",
            "Documented production incidents and their fixes.",
        ]
        * 4
    )

    chunks = chunk_candidate_profile(profile, max_chars=240, overlap_chars=30)

    assert len(chunks) > 1
    assert [chunk.source_id for chunk in chunks] == [
        f"candidate:{index:04d}" for index in range(1, len(chunks) + 1)
    ]
    assert all(1 <= len(chunk.text) <= 240 for chunk in chunks)
    assert chunks[0].text.startswith("Built Python APIs")


def test_retrieval_uses_chroma_with_injected_deterministic_embeddings() -> None:
    profile = " ".join(
        [
            "Python RAG retrieval engineering " + "backend " * 90 + ".",
            "React CSS design systems " + "frontend " * 90 + ".",
        ]
    )
    vocabulary = ("python", "rag", "react", "css")

    def embed(texts: Sequence[str]) -> list[list[float]]:
        return [
            [float(text.casefold().count(term)) for term in vocabulary]
            for text in texts
        ]

    results = retrieve_candidate_evidence(
        profile,
        "The role requires Python and RAG experience.",
        max_results=1,
        embedder=embed,
    )

    assert len(results) == 1
    assert results[0].source_id == "candidate:0001"
    assert "Python RAG retrieval engineering" in results[0].text


def test_retrieval_rejects_invalid_result_count() -> None:
    with pytest.raises(ValueError, match="at least 1"):
        retrieve_candidate_evidence(
            "Python engineering experience",
            "Looking for Python engineering experience",
            max_results=0,
        )
