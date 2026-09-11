from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
import re
from typing import Any
import xml.etree.ElementTree as ET

import requests


@dataclass
class NewsMeta:
    fetched_at: str
    sources_ok: int
    sources_total: int
    errors: list[str]


# Direct publisher feeds. These do not depend on Google News.
SOURCES = [
    ("Utility Dive", "https://www.utilitydive.com/feeds/news/"),
    ("pv magazine", "https://www.pv-magazine.com/feed/"),
    ("Energy Storage News", "https://www.energy-storage.news/feed/"),
    ("Canary Media", "https://www.canarymedia.com/articles.rss"),
    ("Renewable Energy World", "https://www.renewableenergyworld.com/feed/"),
]

TOPIC_WORDS = {
    "Elekter & võrk": ("electricity", "power", "grid", "transmission", "interconnector", "utility", "capacity"),
    "Gaas & LNG": ("gas", "lng", "pipeline", "storage"),
    "Nafta": ("oil", "crude", "opec", "refinery"),
    "Tuumaenergia": ("nuclear", "uranium", "reactor"),
    "Taastuvenergia": ("renewable", "wind", "solar", "hydro", "geothermal"),
    "Salvestus": ("battery", "storage", "bess"),
    "CO₂ & kliimapoliitika": ("carbon", "emission", "ets", "climate"),
    "Energiapoliitika": ("policy", "regulation", "sanction", "security", "market"),
}


def _clean(s: str | None) -> str:
    s = unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _topic(title: str, summary: str) -> str:
    text = f"{title} {summary}".lower()
    for topic, words in TOPIC_WORDS.items():
        if any(w in text for w in words):
            return topic
    return "Energia"


def _parse_date(s: str | None):
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        try:
            return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            return None


def _feed(url: str, source: str, timeout=8) -> list[dict[str, Any]]:
    r = requests.get(
        url,
        timeout=(4, timeout),
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; BalticPulse/15.7.3; energy-news)",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*",
        },
    )
    r.raise_for_status()
    root = ET.fromstring(r.content)
    rows: list[dict[str, Any]] = []

    # RSS 2.0
    for item in root.findall(".//item")[:30]:
        title = _clean(item.findtext("title"))
        link = _clean(item.findtext("link"))
        summary = _clean(item.findtext("description"))
        published = _parse_date(item.findtext("pubDate"))
        if title and link:
            rows.append({
                "source": source,
                "published_at": published.isoformat() if published else "",
                "title": title,
                "summary": summary[:500],
                "topic": _topic(title, summary),
                "url": link,
            })

    # Atom fallback
    if not rows:
        ns = {"a": "http://www.w3.org/2005/Atom"}
        for entry in root.findall(".//a:entry", ns)[:30]:
            title = _clean(entry.findtext("a:title", default="", namespaces=ns))
            summary = _clean(
                entry.findtext("a:summary", default="", namespaces=ns)
                or entry.findtext("a:content", default="", namespaces=ns)
            )
            published = _parse_date(
                entry.findtext("a:published", default="", namespaces=ns)
                or entry.findtext("a:updated", default="", namespaces=ns)
            )
            link = ""
            for link_el in entry.findall("a:link", ns):
                href = link_el.attrib.get("href", "")
                rel = link_el.attrib.get("rel", "alternate")
                if href and rel in ("alternate", ""):
                    link = href
                    break
            if title and link:
                rows.append({
                    "source": source,
                    "published_at": published.isoformat() if published else "",
                    "title": title,
                    "summary": summary[:500],
                    "topic": _topic(title, summary),
                    "url": link,
                })
    return rows


def fetch_energy_news(max_items=24):
    rows = []
    errors = []
    ok = 0
    for source, url in SOURCES:
        try:
            got = _feed(url, source)
            if got:
                ok += 1
                rows.extend(got)
            else:
                errors.append(f"{source}: feed oli tühi")
        except Exception as exc:
            errors.append(f"{source}: {type(exc).__name__}: {exc}")

    seen = set()
    unique = []
    for row in sorted(rows, key=lambda x: x.get("published_at") or "", reverse=True):
        key = re.sub(r"\W+", "", row["title"].lower())
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(row)

    # Prefer regionally relevant / system-relevant stories, then recency.
    boosts = (
        "europe", "european", "baltic", "nordic", "finland", "sweden",
        "estonia", "latvia", "lithuania", "electricity", "power", "grid",
        "gas", "lng", "oil", "nuclear", "carbon", "storage", "energy",
    )
    for row in unique:
        txt = (row["title"] + " " + row["summary"]).lower()
        row["_score"] = sum(1 for w in boosts if w in txt)
    unique.sort(key=lambda x: (x["_score"], x.get("published_at") or ""), reverse=True)
    for row in unique:
        row.pop("_score", None)

    return unique[:max_items], NewsMeta(
        fetched_at=datetime.now(timezone.utc).isoformat(),
        sources_ok=ok,
        sources_total=len(SOURCES),
        errors=errors,
    )
