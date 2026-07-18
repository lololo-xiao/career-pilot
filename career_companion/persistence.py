from __future__ import annotations

import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from career_companion.database import (
    Base,
    build_engine,
    ensure_application_job_claims,
)
from career_companion.paths import CompanionPaths
from career_companion.services.model_routes import ensure_default_routes

_factory_lock = threading.Lock()
_factories: dict[Path, sessionmaker[Session]] = {}


def session_factory_for(paths: CompanionPaths) -> sessionmaker[Session]:
    """Return a cached, initialized factory for one account-scoped database."""

    database = paths.database.resolve()
    factory = _factories.get(database)
    if factory is not None:
        return factory
    with _factory_lock:
        factory = _factories.get(database)
        if factory is not None:
            return factory
        engine = build_engine(paths)
        Base.metadata.create_all(engine)
        ensure_application_job_claims(engine)
        factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
        with factory() as session:
            ensure_default_routes(session)
            session.commit()
        _factories[database] = factory
        return factory


@contextmanager
def account_session(paths: CompanionPaths) -> Generator[Session, None, None]:
    """Commit one operational unit of work, rolling back on any failure."""

    with session_factory_for(paths)() as session:
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise


def clear_factory_cache() -> None:
    """Dispose cached engines. Intended for tests and controlled shutdown."""

    with _factory_lock:
        for factory in _factories.values():
            bind = factory.kw.get("bind")
            if bind is not None:
                bind.dispose()
        _factories.clear()
