from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles


def mount_static_frontend(app: FastAPI, directory: Path | None) -> None:
    if directory is None:
        return
    app.mount(
        "/",
        StaticFiles(directory=str(directory), html=True, check_dir=True),
        name="career-companion-web",
    )
