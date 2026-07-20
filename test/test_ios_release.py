from __future__ import annotations

from pathlib import Path

import pytest

from scripts import ios_release


def test_major_version_parses_node_and_xcode_output() -> None:
    assert ios_release._major_version("v24.18.0\n", r"^v?(\d+)") == 24
    assert ios_release._major_version("Xcode 26.6\nBuild version 17G86\n", r"^Xcode\s+(\d+)") == 26
    assert ios_release._major_version("not a version", r"^v?(\d+)") is None


def test_dotenv_files_are_hidden_and_restored_after_success(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    local = frontend / ".env.local"
    production = frontend / ".env.production"
    example = frontend / ".env.example"
    local.write_text("LOCAL_SECRET=one\n")
    production.write_text("PRODUCTION_SECRET=two\n")
    example.write_text("SAFE_EXAMPLE=\n")

    with ios_release._without_frontend_dotenv(frontend):
        assert not local.exists()
        assert not production.exists()
        assert example.read_text() == "SAFE_EXAMPLE=\n"

    assert local.read_text() == "LOCAL_SECRET=one\n"
    assert production.read_text() == "PRODUCTION_SECRET=two\n"
    assert not list(tmp_path.glob(".careerpilot-ios-env-*"))


def test_dotenv_files_are_restored_after_build_failure(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    local = frontend / ".env.local"
    local.write_text("LOCAL_SECRET=still-here\n")

    with pytest.raises(RuntimeError, match="simulated build failure"):
        with ios_release._without_frontend_dotenv(frontend):
            raise RuntimeError("simulated build failure")

    assert local.read_text() == "LOCAL_SECRET=still-here\n"
    assert not list(tmp_path.glob(".careerpilot-ios-env-*"))


def test_dotenv_symlink_is_rejected_without_moving_target(tmp_path: Path) -> None:
    frontend = tmp_path / "frontend"
    frontend.mkdir()
    target = tmp_path / "secret"
    target.write_text("SECRET=value\n")
    local = frontend / ".env.local"
    try:
        local.symlink_to(target)
    except OSError:
        pytest.skip("symlinks are unavailable on this test platform")

    with pytest.raises(ios_release.IOSReleaseError, match="non-regular"):
        with ios_release._without_frontend_dotenv(frontend):
            pass

    assert local.is_symlink()
    assert target.read_text() == "SECRET=value\n"
