from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests

LOG = logging.getLogger(__name__)

UMM_API = "https://ummapi.nordpoolgroup.com/messages"
UMM_UI = "https://umm.nordpoolgroup.com/#/messages"


@dataclass
class FetchMeta:
    source: str
    fetched_at: str
    status_code: int | None = None
    total_reported: int | None = None
    error: str | None = None


def _get_nested(obj: dict[str, Any], *paths: str) -> Any:
    for path in paths:
        cur: Any = obj
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur not in (None, "", [], {}):
            return cur
    return None


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, dict):
                candidate = (
                    item.get("name") or item.get("code") or item.get("value")
                    or item.get("areaName") or item.get("assetName")
                )
                parts.append(_text(candidate if candidate is not None else item))
            else:
                parts.append(_text(item))
        return ", ".join(p for p in parts if p)
    if isinstance(value, dict):
        candidate = value.get("name") or value.get("code") or value.get("value")
        if candidate is not None:
            return _text(candidate)
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _iso(value: Any) -> str:
    s = _text(value)
    if not s:
        return ""
    return s.replace("Z", "+00:00")


def _to_number(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("value") or value.get("amount") or value.get("quantity")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_number(obj: dict[str, Any], *paths: str) -> float | None:
    return _to_number(_get_nested(obj, *paths))


def _recursive_values(obj: Any, key_names: set[str]) -> list[Any]:
    """Collect values for matching keys anywhere in an API message.

    Nord Pool messages may contain segmented capacity profiles, so capacity fields
    are not guaranteed to be at the JSON root.
    """
    found: list[Any] = []
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key.lower() in key_names:
                found.append(value)
            found.extend(_recursive_values(value, key_names))
    elif isinstance(obj, list):
        for item in obj:
            found.extend(_recursive_values(item, key_names))
    return found


def _recursive_numbers(obj: Any, *key_names: str) -> list[float]:
    keys = {k.lower() for k in key_names}
    nums: list[float] = []
    for value in _recursive_values(obj, keys):
        n = _to_number(value)
        if n is not None:
            nums.append(n)
    return nums


def _capacity_summary(m: dict[str, Any]) -> tuple[float | None, float | None, float | None, float | None, list[dict[str, float | None]]]:
    """Return installed, available, unavailable, affected and capacity profile.

    Affected capacity is reported unavailable capacity where available. If the
    message only provides installed and available capacity, affected capacity is
    derived as installed - available. For segmented messages the maximum affected
    capacity is used as the headline value.
    """
    installed_values = _recursive_numbers(m, "installedCapacity", "installed_capacity")
    available_values = _recursive_numbers(m, "availableCapacity", "available_capacity")
    unavailable_values = _recursive_numbers(m, "unavailableCapacity", "unavailable_capacity")

    installed = max(installed_values) if installed_values else None
    available = min(available_values) if available_values else None
    unavailable = max(unavailable_values) if unavailable_values else None

    affected = unavailable
    if affected is None and installed is not None and available is not None:
        diff = installed - available
        if diff >= 0:
            affected = diff

    # Best-effort profile extraction: identify nested dicts containing capacity data.
    profile: list[dict[str, float | None]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            keys_lower = {str(k).lower(): k for k in node.keys()}
            has_capacity = any(k in keys_lower for k in {
                "installedcapacity", "availablecapacity", "unavailablecapacity",
                "installed_capacity", "available_capacity", "unavailable_capacity",
            })
            if has_capacity:
                inst = _to_number(node.get(keys_lower.get("installedcapacity", keys_lower.get("installed_capacity", ""))))
                avail = _to_number(node.get(keys_lower.get("availablecapacity", keys_lower.get("available_capacity", ""))))
                unavail = _to_number(node.get(keys_lower.get("unavailablecapacity", keys_lower.get("unavailable_capacity", ""))))
                aff = unavail
                if aff is None and inst is not None and avail is not None and inst >= avail:
                    aff = inst - avail
                profile.append({
                    "installed_capacity": inst,
                    "available_capacity": avail,
                    "unavailable_capacity": unavail,
                    "affected_capacity": aff,
                })
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(m)

    # Deduplicate identical profile rows while preserving order.
    unique_profile: list[dict[str, float | None]] = []
    seen: set[tuple[Any, ...]] = set()
    for row in profile:
        key = tuple(row.get(k) for k in (
            "installed_capacity", "available_capacity", "unavailable_capacity", "affected_capacity"
        ))
        if key not in seen:
            seen.add(key)
            unique_profile.append(row)

    return installed, available, unavailable, affected, unique_profile


def normalize_message(m: dict[str, Any]) -> dict[str, Any]:
    message_id = _text(_get_nested(m, "id", "messageId", "messageID", "ummId", "message.id"))
    version = _text(_get_nested(m, "version", "messageVersion", "revisionNumber"))

    asset = _get_nested(m, "assetName", "asset.name", "assets", "assetList", "productionUnit.name", "infrastructureName")
    area = _get_nested(m, "area", "areas", "biddingZone", "biddingZones", "location.area", "eventArea")
    participant = _get_nested(m, "marketParticipant", "marketParticipants", "participant", "publisher")
    fuel = _get_nested(m, "fuelType", "fuelTypes", "asset.fuelType", "productionUnit.fuelType")

    event_type = _text(_get_nested(m, "eventType", "messageType", "type", "event.type", "unavailabilityType"))
    status = _text(_get_nested(m, "eventStatus", "status", "messageStatus"))
    reason = _text(_get_nested(m, "reason", "reasonText", "remarks", "remark", "description", "messageText", "event.reason"))

    installed, available, unavailable, affected, capacity_profile = _capacity_summary(m)

    link = UMM_UI
    if message_id:
        link = f"https://umm.nordpoolgroup.com/#/messages/{message_id}"
        if version:
            link += f"/{version}"

    return {
        "message_id": message_id,
        "version": version,
        "publication_time": _iso(_get_nested(m, "publicationDate", "publicationTime", "published", "createdAt", "message.publicationDate")),
        "event_start": _iso(_get_nested(m, "eventStart", "eventStartDate", "startDate", "event.start", "eventPeriod.start")),
        "event_end": _iso(_get_nested(m, "eventStop", "eventEnd", "eventStopDate", "endDate", "event.end", "eventPeriod.end")),
        "status": status,
        "message_type": event_type,
        "market_participant": _text(participant),
        "asset_name": _text(asset),
        "area": _text(area),
        "fuel_type": _text(fuel),
        "installed_capacity": installed,
        "available_capacity": available,
        "unavailable_capacity": unavailable,
        "affected_capacity": affected,
        "capacity_profile": capacity_profile,
        "reason": reason,
        "source_url": link,
        "source": "Nord Pool UMM",
        "raw": m,
    }


def _extract_items(payload: Any) -> tuple[list[dict[str, Any]], int | None]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)], len(payload)
    if not isinstance(payload, dict):
        raise ValueError(f"Unexpected JSON root type: {type(payload).__name__}")

    items = payload.get("items")
    if items is None:
        items = payload.get("messages")
    if items is None:
        items = payload.get("data")
    if items is None and any(k in payload for k in ("id", "messageId", "ummId")):
        items = [payload]
    if not isinstance(items, list):
        raise ValueError(f"JSON does not contain a message list. Keys: {sorted(payload.keys())[:30]}")

    total = payload.get("total") or payload.get("totalCount") or payload.get("count")
    try:
        total = int(total) if total is not None else None
    except (TypeError, ValueError):
        total = None
    return [x for x in items if isinstance(x, dict)], total


def fetch_umm_messages(limit: int = 1000, max_pages: int = 5, retries: int = 3) -> tuple[list[dict[str, Any]], FetchMeta]:
    session = requests.Session()
    session.headers.update({
        "Accept": "application/json",
        "User-Agent": "NordPool-UMM-Dashboard/1.1 (+https://github.com/)",
    })

    all_items: list[dict[str, Any]] = []
    total_reported: int | None = None
    last_status: int | None = None

    for page in range(max_pages):
        params = {
            "limit": limit,
            "skip": page * limit,
            "IncludeOutdated": "true",
        }
        response = None
        for attempt in range(retries):
            try:
                response = session.get(UMM_API, params=params, timeout=(5, 25))
                last_status = response.status_code
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    if attempt < retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                response.raise_for_status()
                break
            except requests.RequestException as exc:
                if attempt == retries - 1:
                    meta = FetchMeta(
                        source="Nord Pool UMM REST API",
                        fetched_at=datetime.now(timezone.utc).isoformat(),
                        status_code=last_status,
                        error=f"{type(exc).__name__}: {exc}",
                    )
                    return [], meta
                time.sleep(2 ** attempt)

        assert response is not None
        try:
            payload = response.json()
        except ValueError as exc:
            return [], FetchMeta(
                source="Nord Pool UMM REST API",
                fetched_at=datetime.now(timezone.utc).isoformat(),
                status_code=response.status_code,
                error=f"Malformed/non-JSON response: {exc}; body={response.text[:300]!r}",
            )

        try:
            items, total = _extract_items(payload)
        except ValueError as exc:
            return [], FetchMeta(
                source="Nord Pool UMM REST API",
                fetched_at=datetime.now(timezone.utc).isoformat(),
                status_code=response.status_code,
                error=str(exc),
            )

        total_reported = total_reported or total
        all_items.extend(items)
        if not items or len(items) < limit or (total_reported is not None and len(all_items) >= total_reported):
            break

    normalized = [normalize_message(x) for x in all_items]
    seen = set()
    unique = []
    for row in normalized:
        key = (row.get("message_id"), row.get("version"))
        if not key[0]:
            key = (json.dumps(row.get("raw", {}), sort_keys=True, ensure_ascii=False), "")
        if key in seen:
            continue
        seen.add(key)
        unique.append(row)

    return unique, FetchMeta(
        source="Nord Pool UMM REST API",
        fetched_at=datetime.now(timezone.utc).isoformat(),
        status_code=last_status,
        total_reported=total_reported,
    )


def save_snapshot(path: str | Path, rows: Iterable[dict[str, Any]], meta: FetchMeta) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"meta": meta.__dict__, "items": list(rows)}
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_snapshot(path: str | Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    return payload.get("items", []), payload.get("meta", {})
