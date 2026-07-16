import re
from collections.abc import Callable, Sequence
from typing import Any
from uuid import uuid4

from app.schemas import EvidenceSnippet


Embedder = Callable[[Sequence[str]], Sequence[Sequence[float]]]

_BOUNDARY = re.compile(r"(?<=[.!?])\s+|\n+")


def _split_long_piece(piece: str, max_chars: int) -> list[str]:
    parts: list[str] = []
    remainder = piece.strip()
    while len(remainder) > max_chars:
        split_at = remainder.rfind(" ", 0, max_chars + 1)
        if split_at < max_chars // 2:
            split_at = max_chars
        parts.append(remainder[:split_at].strip())
        remainder = remainder[split_at:].strip()
    if remainder:
        parts.append(remainder)
    return parts


def chunk_candidate_profile(
    text: str,
    *,
    max_chars: int = 900,
    overlap_chars: int = 120,
) -> list[EvidenceSnippet]:
    """Split profile text into citation-friendly chunks without LangChain."""

    if max_chars < 200:
        raise ValueError("max_chars must be at least 200")
    if not 0 <= overlap_chars < max_chars:
        raise ValueError("overlap_chars must be between 0 and max_chars")

    pieces: list[str] = []
    for raw_piece in _BOUNDARY.split(text.strip()):
        if raw_piece := raw_piece.strip():
            pieces.extend(_split_long_piece(raw_piece, max_chars))

    if not pieces:
        return []

    chunk_texts: list[str] = []
    current = pieces[0]
    for piece in pieces[1:]:
        candidate = f"{current} {piece}"
        if len(candidate) <= max_chars:
            current = candidate
            continue

        chunk_texts.append(current)
        overlap = current[-overlap_chars:].lstrip() if overlap_chars else ""
        current = f"{overlap} {piece}".strip()
        if len(current) > max_chars:
            split_current = _split_long_piece(current, max_chars)
            chunk_texts.extend(split_current[:-1])
            current = split_current[-1]

    chunk_texts.append(current)
    return [
        EvidenceSnippet(source_id=f"candidate:{index:04d}", text=chunk)
        for index, chunk in enumerate(chunk_texts, start=1)
    ]


def _as_float_vectors(values: Sequence[Sequence[float]]) -> list[list[float]]:
    return [[float(value) for value in vector] for vector in values]


def _get_local_embedder() -> Embedder:
    from chromadb.utils.embedding_functions import DefaultEmbeddingFunction

    embedding_function = DefaultEmbeddingFunction()
    if embedding_function is None:
        raise RuntimeError("Chroma's local embedding function is unavailable")
    return embedding_function


def retrieve_candidate_evidence(
    candidate_profile: str,
    job_description: str,
    *,
    max_results: int = 6,
    embedder: Embedder | None = None,
    client: Any | None = None,
) -> list[EvidenceSnippet]:
    """Index one candidate profile in ephemeral Chroma and return relevant chunks."""

    if max_results < 1:
        raise ValueError("max_results must be at least 1")

    chunks = chunk_candidate_profile(candidate_profile)
    if not chunks:
        return []

    selected_embedder = embedder or _get_local_embedder()
    document_vectors = _as_float_vectors(
        selected_embedder([chunk.text for chunk in chunks])
    )
    query_vectors = _as_float_vectors(selected_embedder([job_description]))

    if client is None:
        import chromadb
        from chromadb.config import Settings

        client = chromadb.EphemeralClient(
            settings=Settings(anonymized_telemetry=False)
        )

    collection = client.create_collection(
        name=f"career_pilot_{uuid4().hex}",
        embedding_function=None,
        configuration={"hnsw": {"space": "cosine"}},
    )
    collection.add(
        ids=[chunk.source_id for chunk in chunks],
        documents=[chunk.text for chunk in chunks],
        embeddings=document_vectors,
    )
    result = collection.query(
        query_embeddings=query_vectors,
        n_results=min(max_results, len(chunks)),
        include=["documents", "distances"],
    )

    ids = result["ids"][0]
    documents = result["documents"][0] if result["documents"] else []
    return [
        EvidenceSnippet(source_id=source_id, text=document)
        for source_id, document in zip(ids, documents, strict=True)
    ]
