"""Compatibility factory for the consolidated CareerPilot FastAPI application.

Provider authentication and browser sessions are owned by :mod:`app.main`. This
module remains so the transferred CLI and third-party imports keep working while
there is only one HTTP application.
"""

from fastapi import FastAPI


def create_app(*_args, **_kwargs) -> FastAPI:
    from app.main import app

    return app
