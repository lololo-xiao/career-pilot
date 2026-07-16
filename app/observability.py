import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from dotenv import load_dotenv


_FALSE_VALUES = {"0", "false", "no", "off"}


def is_langfuse_enabled() -> bool:
    """Enable remote tracing only when credentials exist and it is not disabled."""

    load_dotenv()
    tracing_setting = os.getenv("LANGFUSE_TRACING_ENABLED", "true").casefold()
    if tracing_setting in _FALSE_VALUES:
        return False
    return bool(
        os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")
    )


@contextmanager
def trace_observation(
    name: str,
    *,
    as_type: str = "span",
    input: Any | None = None,
    metadata: dict[str, Any] | None = None,
    version: str | None = None,
) -> Iterator[Any | None]:
    """Create an optional Langfuse observation without burdening domain code."""

    if not is_langfuse_enabled():
        yield None
        return

    from langfuse import get_client

    langfuse = get_client()
    with langfuse.start_as_current_observation(
        name=name,
        as_type=as_type,
        input=input,
        metadata=metadata,
        version=version,
    ) as observation:
        yield observation


@contextmanager
def trace_attributes(
    *,
    metadata: dict[str, Any],
    version: str,
    tags: list[str],
    trace_name: str,
) -> Iterator[None]:
    """Propagate stable release metadata to all observations in one workflow."""

    if not is_langfuse_enabled():
        yield
        return

    from langfuse import propagate_attributes

    with propagate_attributes(
        metadata=metadata,
        version=version,
        tags=tags,
        trace_name=trace_name,
    ):
        yield


def update_observation(observation: Any | None, **values: Any) -> None:
    if observation is not None:
        observation.update(**values)
