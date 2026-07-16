from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from urllib.parse import urlsplit

from pypdf import PdfReader
from pypdf.errors import PdfReadError


@dataclass
class RenderReport:
    pdf_path: str
    pages: int
    text_characters: int
    links: int
    engine: str
    attempts: int
    valid: bool
    errors: list[str]
    warnings: list[str]


_DANGEROUS_TEX_COMMANDS = re.compile(
    r"(?:\\(?:write18|input|include|openin|openout|read|catcode|csname)\b"
    r"|\\usepackage\s*\{shellesc\})",
    re.IGNORECASE,
)
_GRAPHIC = re.compile(r"\\includegraphics(?:\[[^\]]*\])?\{([^}]+)\}")
_SAFE_ASSET_SUFFIXES = {".jpeg", ".jpg", ".pdf", ".png"}
_LOG_ERROR_MARKERS = (
    "missing character:",
    "fontspec error",
    "font not found",
    "unable to load picture",
    "file not found",
)


def render_one_page(
    tex_path: Path,
    max_attempts: int = 3,
    *,
    engine_executable: str = "tectonic",
) -> RenderReport:
    if not tex_path.is_file():
        raise FileNotFoundError(tex_path)
    if not 1 <= max_attempts <= 3:
        raise ValueError("Rendering is limited to one through three attempts")
    preflight_errors = validate_tex_source(tex_path)
    if preflight_errors:
        return _failed_report(tex_path, attempts=0, errors=preflight_errors)
    engine = shutil.which(engine_executable)
    if not engine:
        raise RuntimeError("Tectonic is required. Run career-companion doctor for details.")
    output_dir = tex_path.parent
    errors: list[str] = []
    attempts = 0
    for attempts in range(1, max_attempts + 1):
        result = subprocess.run(
            [engine, "--keep-logs", "--outdir", str(output_dir), str(tex_path.name)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
            cwd=output_dir,
            env={**os.environ, "TECTONIC_UNTRUSTED_MODE": "1"},
        )
        if result.returncode != 0:
            errors.append(_short_error(result.stdout + "\n" + result.stderr))
            continue
        pdf_path = tex_path.with_suffix(".pdf")
        log_errors, log_warnings = _log_diagnostics(tex_path.with_suffix(".log"))
        report = validate_pdf(
            pdf_path,
            engine="tectonic",
            attempts=attempts,
            additional_errors=log_errors,
            warnings=log_warnings,
        )
        if report.valid:
            return report
        errors.extend(report.errors)
    pdf_path = tex_path.with_suffix(".pdf")
    if pdf_path.exists():
        report = validate_pdf(pdf_path, engine="tectonic", attempts=attempts)
        report.errors = list(dict.fromkeys(errors or report.errors))
        report.valid = not report.errors
        return report
    return _failed_report(
        tex_path,
        attempts=attempts,
        errors=list(dict.fromkeys(errors)) or ["No PDF was produced"],
    )


def validate_tex_source(tex_path: Path) -> list[str]:
    if tex_path.stat().st_size > 2 * 1024 * 1024:
        return ["TeX source exceeds the 2 MB safety limit"]
    try:
        source = tex_path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return ["TeX source must be UTF-8"]
    errors: list[str] = []
    if _DANGEROUS_TEX_COMMANDS.search(source):
        errors.append("TeX source contains a prohibited file or execution command")
    root = tex_path.parent.resolve()
    for raw_asset in _GRAPHIC.findall(source):
        candidate = Path(raw_asset.strip())
        if candidate.is_absolute() or ".." in candidate.parts:
            errors.append(f"Asset path must stay inside the artifact folder: {raw_asset}")
            continue
        resolved = (root / candidate).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            errors.append(f"Asset path escapes the artifact folder: {raw_asset}")
            continue
        if resolved.suffix.casefold() not in _SAFE_ASSET_SUFFIXES:
            errors.append(f"Unsupported asset type: {raw_asset}")
        elif not resolved.is_file():
            errors.append(f"Missing asset: {raw_asset}")
    return errors


def validate_pdf(
    pdf_path: Path,
    *,
    engine: str,
    attempts: int,
    additional_errors: list[str] | None = None,
    warnings: list[str] | None = None,
) -> RenderReport:
    errors = list(additional_errors or [])
    report_warnings = list(warnings or [])
    try:
        reader = PdfReader(str(pdf_path), strict=True)
    except (OSError, PdfReadError, ValueError) as exc:
        return _failed_report(
            pdf_path.with_suffix(".tex"),
            attempts=attempts,
            errors=errors + [f"PDF could not be read: {exc}"],
            engine=engine,
            pdf_path=pdf_path,
            warnings=report_warnings,
        )
    pages = len(reader.pages)
    if pages != 1:
        errors.append(f"Expected one page, found {pages}")
    try:
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
    except (PdfReadError, ValueError, TypeError) as exc:
        text = ""
        errors.append(f"PDF text extraction failed: {exc}")
    if len(text.strip()) < 100:
        errors.append("Extracted PDF text is unexpectedly short")
    links = 0
    for page in reader.pages:
        annotations = page.get("/Annots", [])
        for item in annotations:
            annotation = item.get_object()
            if annotation.get("/Subtype") != "/Link":
                continue
            links += 1
            action = annotation.get("/A")
            action = action.get_object() if hasattr(action, "get_object") else action
            if not action or action.get("/S") != "/URI":
                continue
            target = str(action.get("/URI", ""))
            if urlsplit(target).scheme.casefold() not in {"http", "https", "mailto"}:
                errors.append(f"PDF contains an unsafe external link: {target[:120]}")
    return RenderReport(
        pdf_path=str(pdf_path),
        pages=pages,
        text_characters=len(text),
        links=links,
        engine=engine,
        attempts=attempts,
        valid=not errors,
        errors=list(dict.fromkeys(errors)),
        warnings=report_warnings,
    )


def report_json(report: RenderReport) -> dict:
    return asdict(report)


def _short_error(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return " | ".join(lines[-8:])[:2000] or "Tectonic failed without output"


def _log_diagnostics(log_path: Path) -> tuple[list[str], list[str]]:
    if not log_path.is_file():
        return [], ["Tectonic did not preserve a build log"]
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return [], [f"Tectonic log could not be read: {exc}"]
    errors: list[str] = []
    warnings: list[str] = []
    for line in lines:
        normalized = line.strip()
        lowered = normalized.casefold()
        if normalized and any(marker in lowered for marker in _LOG_ERROR_MARKERS):
            errors.append(normalized[:500])
        elif normalized and "warning" in lowered:
            warnings.append(normalized[:500])
    return list(dict.fromkeys(errors)), list(dict.fromkeys(warnings))[:20]


def _failed_report(
    tex_path: Path,
    *,
    attempts: int,
    errors: list[str],
    engine: str = "tectonic",
    pdf_path: Path | None = None,
    warnings: list[str] | None = None,
) -> RenderReport:
    return RenderReport(
        pdf_path=str(pdf_path or tex_path.with_suffix(".pdf")),
        pages=0,
        text_characters=0,
        links=0,
        engine=engine,
        attempts=attempts,
        valid=False,
        errors=errors,
        warnings=list(warnings or []),
    )
