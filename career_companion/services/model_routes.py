from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from career_companion.database import ModelRouteRecord, UsageRunRecord
from career_companion.schemas import ModelRoute
from career_companion.services.audit import record_audit

BALANCED_ROUTES = [
    ModelRoute(
        name="interactive",
        provider="openai-codex",
        model="gpt-5.4",
        reasoning_effort="medium",
        token_limit=8000,
        cost_budget_usd=0.75,
    ),
    ModelRoute(
        name="research",
        provider="openai-codex",
        model="gpt-5.4",
        reasoning_effort="medium",
        token_limit=12000,
        cost_budget_usd=1.0,
    ),
    ModelRoute(
        name="extraction",
        provider="openai-api",
        model="gpt-5.4-mini",
        reasoning_effort="low",
        token_limit=4000,
        cost_budget_usd=0.15,
    ),
    ModelRoute(
        name="tailoring",
        provider="openai-codex",
        model="gpt-5.4",
        reasoning_effort="high",
        token_limit=16000,
        cost_budget_usd=1.25,
    ),
    ModelRoute(
        name="evaluation",
        provider="openai-api",
        model="gpt-5.4-mini",
        reasoning_effort="medium",
        token_limit=6000,
        cost_budget_usd=0.25,
    ),
    ModelRoute(
        name="memory-review",
        provider="openai-api",
        model="gpt-5.4-mini",
        reasoning_effort="low",
        token_limit=2500,
        cost_budget_usd=0.08,
    ),
    ModelRoute(
        name="compression",
        provider="openai-api",
        model="gpt-5.4-mini",
        reasoning_effort="low",
        token_limit=2500,
        cost_budget_usd=0.08,
    ),
    ModelRoute(
        name="cron",
        provider="openai-api",
        model="gpt-5.4-mini",
        reasoning_effort="low",
        token_limit=4000,
        cost_budget_usd=0.15,
        scheduled=True,
    ),
]


def ensure_default_routes(session: Session) -> None:
    for route in BALANCED_ROUTES:
        if session.get(ModelRouteRecord, route.name):
            continue
        session.add(ModelRouteRecord(**route.model_dump()))


def upsert_route(session: Session, route: ModelRoute) -> ModelRouteRecord:
    if route.scheduled and route.fallback_policy != "none":
        raise ValueError("Scheduled routes must not use implicit fallbacks")
    if route.fallback_policy == "explicit" and not route.fallback_route:
        raise ValueError("Explicit fallback policy requires a fallback route")
    if route.fallback_policy != "explicit" and route.fallback_route:
        raise ValueError("A fallback route requires the explicit fallback policy")
    if route.fallback_route == route.name:
        raise ValueError("A model route cannot fall back to itself")
    if route.fallback_route and session.get(ModelRouteRecord, route.fallback_route) is None:
        raise ValueError("Fallback route does not exist")
    record = session.get(ModelRouteRecord, route.name)
    if record is None:
        record = ModelRouteRecord(**route.model_dump())
        session.add(record)
    else:
        for key, value in route.model_dump().items():
            setattr(record, key, value)
    record_audit(
        session,
        "model_route.updated",
        subject_type="model_route",
        subject_id=route.name,
        payload={"provider": route.provider, "model": route.model},
    )
    return record


def record_usage(session: Session, run: UsageRunRecord) -> UsageRunRecord:
    route = session.get(ModelRouteRecord, run.route)
    if not route:
        raise LookupError("Unknown model route")
    estimated_cost = float(run.estimated_cost_usd or 0)
    input_tokens = int(run.input_tokens or 0)
    output_tokens = int(run.output_tokens or 0)
    if estimated_cost < 0:
        raise ValueError("Estimated cost must not be negative")
    if input_tokens < 0 or output_tokens < 0:
        raise ValueError("Token counts must not be negative")
    if estimated_cost > route.cost_budget_usd:
        raise PermissionError("Run exceeds the route cost budget")
    if run.provider != route.provider or run.model != route.model:
        raise ValueError("Usage provider and model must match the selected route")
    run.estimated_cost_usd = estimated_cost
    run.input_tokens = input_tokens
    run.output_tokens = output_tokens
    session.add(run)
    return run


def daily_cost(session: Session) -> float:
    today = datetime.now(UTC).date()
    result = session.scalar(
        select(func.coalesce(func.sum(UsageRunRecord.estimated_cost_usd), 0.0)).where(
            func.date(UsageRunRecord.created_at) == today.isoformat()
        )
    )
    return float(result or 0)
