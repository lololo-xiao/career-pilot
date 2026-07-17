from __future__ import annotations

from pathlib import Path

import pytest

import career_companion.web as web


@pytest.fixture
def checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path]:
    repository = tmp_path / "career-pilot"
    package = repository / "career_companion"
    source = repository / "frontend" / "out"
    bundled = package / "web"
    package.mkdir(parents=True)
    (repository / "frontend").mkdir()
    (repository / "frontend" / "package.json").write_text("{}", encoding="utf-8")
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "career-pilot"\n', encoding="utf-8"
    )
    monkeypatch.setattr(web, "__file__", str(package / "web.py"))
    monkeypatch.delenv("CAREER_COMPANION_WEB_DIR", raising=False)
    return source, bundled, repository


def _build(directory: Path, marker: str) -> Path:
    directory.mkdir(parents=True)
    (directory / "index.html").write_text(marker, encoding="utf-8")
    return directory.resolve()


def test_source_export_wins_over_bundled_assets(checkout: tuple[Path, Path, Path]) -> None:
    source, bundled, _repository = checkout
    _build(bundled, "stale bundled UI")
    expected = _build(source, "fresh source UI")

    assert web.frontend_build_directory() == expected


def test_explicit_web_directory_wins_over_automatic_candidates(
    checkout: tuple[Path, Path, Path],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source, bundled, _repository = checkout
    _build(source, "source UI")
    _build(bundled, "bundled UI")
    explicit = _build(tmp_path / "operator-build", "operator UI")
    monkeypatch.setenv("CAREER_COMPANION_WEB_DIR", str(explicit))

    assert web.frontend_build_directory() == explicit


def test_bundled_assets_are_the_installed_distribution_fallback(
    checkout: tuple[Path, Path, Path],
) -> None:
    _source, bundled, repository = checkout
    (repository / "pyproject.toml").unlink()
    expected = _build(bundled, "bundled UI")

    assert web.frontend_build_directory() == expected


def test_unrelated_installed_frontend_cannot_shadow_bundled_assets(
    checkout: tuple[Path, Path, Path],
) -> None:
    source, bundled, repository = checkout
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "unrelated-package"\n', encoding="utf-8"
    )
    _build(source, "unrelated UI")
    expected = _build(bundled, "bundled CareerPilot UI")

    assert web.frontend_build_directory() == expected


def test_source_checkout_without_export_falls_back_to_bundled_assets(
    checkout: tuple[Path, Path, Path],
) -> None:
    _source, bundled, _repository = checkout
    expected = _build(bundled, "bundled UI")

    assert web.frontend_build_directory() == expected


def test_invalid_candidates_return_none(checkout: tuple[Path, Path, Path]) -> None:
    source, bundled, _repository = checkout
    source.mkdir(parents=True)
    bundled.mkdir(parents=True)

    assert web.frontend_build_directory() is None
