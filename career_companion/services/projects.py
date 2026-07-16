from __future__ import annotations

import asyncio
import base64
import os
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlsplit

import httpx

from career_companion.schemas import ProfileProject, ProjectAnalysis

MAX_REPOSITORY_FILES = 3_000
MAX_MANIFEST_BYTES = 80_000
IGNORED_DIRECTORIES = {
    ".git",
    ".idea",
    ".mypy_cache",
    ".next",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    ".vscode",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "vendor",
}
MANIFEST_NAMES = {
    "cargo.toml",
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
    "gemfile",
    "go.mod",
    "package.json",
    "pom.xml",
    "pyproject.toml",
    "requirements.txt",
}
LANGUAGE_EXTENSIONS = {
    ".c": "C",
    ".cc": "C++",
    ".cpp": "C++",
    ".cs": "C#",
    ".css": "CSS",
    ".dart": "Dart",
    ".go": "Go",
    ".html": "HTML",
    ".java": "Java",
    ".js": "JavaScript",
    ".jsx": "JavaScript",
    ".kt": "Kotlin",
    ".php": "PHP",
    ".py": "Python",
    ".rb": "Ruby",
    ".rs": "Rust",
    ".scala": "Scala",
    ".sh": "Shell",
    ".sql": "SQL",
    ".swift": "Swift",
    ".ts": "TypeScript",
    ".tsx": "TypeScript",
    ".vue": "Vue",
}


async def analyze_project(project: ProfileProject) -> ProjectAnalysis:
    """Inspect repository structure without executing repository code."""

    if project.local_path.strip():
        path = Path(project.local_path).expanduser().resolve()
        return await asyncio.to_thread(_analyze_local_repository, project, path)
    if project.repository_url is not None:
        return await _analyze_github_repository(project)
    raise ValueError("Add a local codebase path or public GitHub repository URL first")


def _analyze_local_repository(project: ProfileProject, root: Path) -> ProjectAnalysis:
    if not root.exists():
        raise FileNotFoundError(f"Local codebase was not found: {root}")
    if not root.is_dir():
        raise ValueError("The local codebase path must point to a directory")

    files: list[str] = []
    manifest_text: list[str] = []
    for current_root, directories, filenames in os.walk(root, followlinks=False):
        directories[:] = sorted(
            directory
            for directory in directories
            if directory not in IGNORED_DIRECTORIES
            and (not directory.startswith(".") or directory == ".github")
            and not (Path(current_root) / directory).is_symlink()
        )
        for filename in sorted(filenames):
            candidate = Path(current_root) / filename
            if candidate.is_symlink() or not candidate.is_file():
                continue
            relative = candidate.relative_to(root).as_posix()
            files.append(relative)
            if filename.casefold() in MANIFEST_NAMES:
                try:
                    manifest_text.append(
                        candidate.read_text(encoding="utf-8", errors="replace")[
                            :MAX_MANIFEST_BYTES
                        ]
                    )
                except OSError:
                    pass
            if len(files) >= MAX_REPOSITORY_FILES:
                break
        if len(files) >= MAX_REPOSITORY_FILES:
            break

    return _build_analysis(
        project,
        source="local",
        repository_name=project.name.strip() or root.name,
        files=files,
        manifest_text="\n".join(manifest_text),
    )


async def _analyze_github_repository(project: ProfileProject) -> ProjectAnalysis:
    owner, repository = _github_coordinates(str(project.repository_url))
    api_root = f"https://api.github.com/repos/{quote(owner)}/{quote(repository)}"
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "CareerPilot-repository-review",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    try:
        async with httpx.AsyncClient(
            headers=headers,
            timeout=15,
            trust_env=False,
            follow_redirects=False,
        ) as client:
            metadata_response = await client.get(api_root)
            metadata_response.raise_for_status()
            metadata = metadata_response.json()
            if metadata.get("private"):
                raise ValueError(
                    "Private GitHub repositories require a local checkout for analysis"
                )
            default_branch = str(metadata.get("default_branch") or "main")
            tree_response = await client.get(
                f"{api_root}/git/trees/{quote(default_branch, safe='')}?recursive=1"
            )
            tree_response.raise_for_status()
            tree = tree_response.json()
            files = [
                str(item.get("path"))
                for item in tree.get("tree", [])
                if item.get("type") == "blob" and item.get("path")
            ][:MAX_REPOSITORY_FILES]
            manifest_text = await _github_manifest_text(client, api_root, files)
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            raise ValueError(
                "The GitHub repository was not found or is not public"
            ) from exc
        if exc.response.status_code == 403:
            raise ValueError(
                "GitHub temporarily refused the repository scan; try a local checkout"
            ) from exc
        raise ValueError("GitHub could not provide the repository contents") from exc
    except httpx.HTTPError as exc:
        raise RuntimeError("The GitHub repository could not be reached") from exc

    return _build_analysis(
        project,
        source="github",
        repository_name=str(metadata.get("full_name") or f"{owner}/{repository}"),
        files=files,
        manifest_text=manifest_text,
    )


async def _github_manifest_text(
    client: httpx.AsyncClient, api_root: str, files: list[str]
) -> str:
    selected = [
        path for path in files if Path(path).name.casefold() in MANIFEST_NAMES
    ][:8]
    contents: list[str] = []
    for path in selected:
        response = await client.get(f"{api_root}/contents/{quote(path, safe='/')}")
        if response.status_code != 200:
            continue
        payload = response.json()
        encoded = payload.get("content")
        if payload.get("encoding") != "base64" or not isinstance(encoded, str):
            continue
        try:
            decoded = base64.b64decode(encoded, validate=False)
        except (ValueError, TypeError):
            continue
        contents.append(
            decoded[:MAX_MANIFEST_BYTES].decode("utf-8", errors="replace")
        )
    return "\n".join(contents)


def _github_coordinates(value: str) -> tuple[str, str]:
    parts = urlsplit(value)
    path_parts = [part for part in parts.path.strip("/").split("/") if part]
    if (
        parts.scheme != "https"
        or (parts.hostname or "").casefold() not in {"github.com", "www.github.com"}
        or parts.username
        or parts.password
        or len(path_parts) != 2
    ):
        raise ValueError("Use a repository URL like https://github.com/owner/repository")
    owner = path_parts[0]
    repository = path_parts[1].removesuffix(".git")
    if not owner or not repository:
        raise ValueError("The GitHub repository URL is incomplete")
    return owner, repository


def _build_analysis(
    project: ProfileProject,
    *,
    source: Literal["local", "github"],
    repository_name: str,
    files: list[str],
    manifest_text: str,
) -> ProjectAnalysis:
    normalized_files = [path.replace("\\", "/") for path in files]
    languages = _primary_languages(normalized_files)
    technologies = _technologies(normalized_files, manifest_text)
    display_name = project.name.strip() or repository_name
    suggestions = _improvement_suggestions(normalized_files)
    questions = _interview_questions(display_name, languages, technologies)
    stack = ", ".join(technologies[:4] or languages[:4]) or "an unclassified stack"
    summary = (
        f"Read-only structure scan found {len(normalized_files)} files and identified "
        f"{stack}. No repository code was executed."
    )
    return ProjectAnalysis(
        source=source,
        repository_name=repository_name,
        analyzed_at=datetime.now(UTC),
        file_count=len(normalized_files),
        primary_languages=languages,
        technologies=technologies,
        notable_files=_notable_files(normalized_files),
        improvement_suggestions=suggestions,
        interview_questions=questions,
        summary=summary,
    )


def _primary_languages(files: list[str]) -> list[str]:
    counts = Counter(
        language
        for path in files
        if (language := LANGUAGE_EXTENSIONS.get(Path(path).suffix.casefold()))
    )
    return [name for name, _ in counts.most_common(6)]


def _technologies(files: list[str], manifest_text: str) -> list[str]:
    paths = "\n".join(files).casefold()
    manifests = manifest_text.casefold()
    detected: list[str] = []

    def add(name: str, condition: bool) -> None:
        if condition and name not in detected:
            detected.append(name)

    add("Python", "pyproject.toml" in paths or "requirements.txt" in paths)
    add("Node.js", "package.json" in paths)
    add("TypeScript", "tsconfig.json" in paths or any(path.endswith((".ts", ".tsx")) for path in files))
    add("React", '"react"' in manifests or any(path.endswith((".jsx", ".tsx")) for path in files))
    add("Next.js", "next.config." in paths or '"next"' in manifests)
    add("FastAPI", "fastapi" in manifests)
    add("Django", "django" in manifests)
    add("Flask", "flask" in manifests)
    add("SQLAlchemy", "sqlalchemy" in manifests)
    add("OpenAI", "openai" in manifests)
    add("LangGraph", "langgraph" in manifests)
    add("Docker", "dockerfile" in paths or "compose.y" in paths)
    add("Kubernetes", "/k8s/" in f"/{paths}" or "/kubernetes/" in f"/{paths}")
    add("Terraform", any(path.endswith(".tf") for path in files))
    add("Go", "go.mod" in paths)
    add("Rust", "cargo.toml" in paths)
    add("Java", "pom.xml" in paths or "build.gradle" in paths)
    return detected[:10]


def _improvement_suggestions(files: list[str]) -> list[str]:
    lowered = [path.casefold() for path in files]
    names = {Path(path).name for path in lowered}
    suggestions: list[str] = []
    if not any(Path(path).name.startswith("readme") for path in lowered):
        suggestions.append(
            "Add a README with the problem, architecture, setup steps, and one measurable outcome."
        )
    if not any(
        path.startswith(("test/", "tests/", "spec/"))
        or "/test/" in path
        or "/tests/" in path
        or Path(path).name.startswith("test_")
        for path in lowered
    ):
        suggestions.append(
            "Add focused tests around the most important behavior and one failure path."
        )
    if not any(path.startswith(".github/workflows/") for path in lowered):
        suggestions.append(
            "Add a CI check that runs formatting, static checks, and the test suite."
        )
    if not any(name.startswith("license") for name in names):
        suggestions.append("Clarify the repository license before presenting it publicly.")
    if not any(path.startswith(("docs/", "doc/")) for path in lowered):
        suggestions.append(
            "Document one key design decision and the trade-off behind it."
        )
    if len(suggestions) < 3:
        suggestions.append(
            "Add a short performance or reliability note with a reproducible measurement."
        )
    return suggestions[:5]


def _interview_questions(
    project_name: str, languages: list[str], technologies: list[str]
) -> list[str]:
    questions = [
        f"Walk me through the architecture of {project_name} and the boundary you found hardest to design.",
        f"What trade-off in {project_name} would you revisit if usage grew by 10×?",
        f"How did you verify that {project_name} solved the intended problem, and what evidence would you collect next?",
    ]
    if technologies:
        questions.append(
            f"Why did you choose {technologies[0]} for this project, and what alternative did you reject?"
        )
    elif languages:
        questions.append(
            f"Why was {languages[0]} a good fit for this project, and where did it create friction?"
        )
    questions.append(
        "Describe a defect or production risk you discovered here and how you reduced it."
    )
    return questions


def _notable_files(files: list[str]) -> list[str]:
    def priority(path: str) -> tuple[int, str]:
        lowered = path.casefold()
        name = Path(lowered).name
        if name.startswith("readme"):
            return 0, lowered
        if name in MANIFEST_NAMES or name in {"tsconfig.json", "cargo.lock", "uv.lock"}:
            return 1, lowered
        if lowered.startswith(".github/workflows/"):
            return 2, lowered
        if name.startswith("dockerfile"):
            return 3, lowered
        if "test" in lowered:
            return 4, lowered
        return 5, lowered

    return sorted(files, key=priority)[:12]
