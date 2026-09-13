from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import pandas as pd


@dataclass(frozen=True)
class SourceHealth:
    name: str
    state: str            # LIVE | SNAPSHOT | MIRROR | STALE | UNAVAILABLE
    displayable: bool
    direct: bool
    observed_at: Any = None
    checked_at: Any = None
    age_minutes: float | None = None
    max_age_minutes: float | None = None
    detail: str = ""

    @property
    def icon(self) -> str:
        return {
            "LIVE": "🟢",
            "SNAPSHOT": "🟡",
            "MIRROR": "🟡",
            "STALE": "🟠",
            "UNAVAILABLE": "🔴",
        }.get(self.state, "⚪")


def _ts(value):
    if value is None:
        return None
    try:
        t = pd.Timestamp(value)
        if pd.isna(t):
            return None
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        return t.tz_convert("UTC")
    except Exception:
        return None


def age_minutes(value) -> float | None:
    t = _ts(value)
    if t is None:
        return None
    return max(0.0, (pd.Timestamp.now(tz="UTC") - t).total_seconds() / 60.0)


def assess(
    name: str,
    *,
    has_data: bool,
    direct_ok: bool,
    observed_at=None,
    max_age_minutes: float | None = None,
    mode: str = "direct",          # direct | snapshot | mirror
    checked_at=None,
    detail: str = "",
) -> SourceHealth:
    age = age_minutes(observed_at)

    if not has_data:
        return SourceHealth(
            name=name, state="UNAVAILABLE", displayable=False,
            direct=(mode == "direct"), observed_at=observed_at,
            checked_at=checked_at, age_minutes=age,
            max_age_minutes=max_age_minutes, detail=detail or "andmed puuduvad",
        )

    if max_age_minutes is not None and age is not None and age > max_age_minutes:
        return SourceHealth(
            name=name, state="STALE", displayable=False,
            direct=(mode == "direct"), observed_at=observed_at,
            checked_at=checked_at, age_minutes=age,
            max_age_minutes=max_age_minutes, detail=detail or "andmed on vananenud",
        )

    if mode == "direct":
        if not direct_ok:
            return SourceHealth(
                name=name, state="UNAVAILABLE", displayable=False,
                direct=True, observed_at=observed_at, checked_at=checked_at,
                age_minutes=age, max_age_minutes=max_age_minutes,
                detail=detail or "otseühendus ebaõnnestus",
            )
        state = "LIVE"
    elif mode == "snapshot":
        state = "SNAPSHOT"
    elif mode == "mirror":
        state = "MIRROR"
    else:
        state = "UNAVAILABLE"

    return SourceHealth(
        name=name, state=state, displayable=True,
        direct=(mode == "direct"), observed_at=observed_at,
        checked_at=checked_at, age_minutes=age,
        max_age_minutes=max_age_minutes, detail=detail,
    )


def mask(value, health: SourceHealth):
    return value if health.displayable else None


def human_age(minutes: float | None) -> str:
    if minutes is None:
        return "aeg teadmata"
    mins = int(minutes)
    if mins < 60:
        return f"{mins} min"
    if mins < 1440:
        return f"{mins // 60} h {mins % 60} min"
    return f"{mins // 1440} p {((mins % 1440) // 60)} h"
