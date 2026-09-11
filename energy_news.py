from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
import re
from typing import Any
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

import requests


@dataclass
class NewsMeta:
    fetched_at: str
    sources_ok: int
    sources_total: int
    errors: list[str]


# Curated energy-specialist / high-authority sources.
# Google News RSS is used only as a discovery transport for sites without a stable public RSS URL.
SOURCES = [
    ("Reuters", "google", "site:reuters.com (energy OR electricity OR power OR gas OR LNG OR oil OR nuclear OR grid OR carbon) when:3d"),
    ("S&P Global Energy", "google", "site:spglobal.com/energy (power OR gas OR LNG OR oil OR energy transition) when:3d"),
    ("Energy Intelligence", "google", "site:energyintel.com (energy OR oil OR gas OR LNG OR power) when:3d"),
    ("Utility Dive", "rss", "https://www.utilitydive.com/feeds/news/"),
    ("IEA", "google", "site:iea.org/news (energy OR electricity OR gas OR oil OR renewables OR nuclear) when:7d"),
]

TOPIC_WORDS = {
    "Elekter & võrk": ("electricity", "power", "grid", "transmission", "interconnector", "utility"),
    "Gaas & LNG": ("gas", "lng", "pipeline", "storage"),
    "Nafta": ("oil", "crude", "opec", "refinery"),
    "Tuumaenergia": ("nuclear", "uranium", "reactor"),
    "Taastuvenergia": ("renewable", "wind", "solar", "hydro", "geothermal"),
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


def _feed(url: str, source: str, timeout=6) -> list[dict[str, Any]]:
    r = requests.get(url, timeout=timeout, headers={"User-Agent": "BalticPulse/15.7 energy-news"})
    r.raise_for_status()
    root = ET.fromstring(r.content)
    rows = []
    for item in root.findall(".//item")[:30]:
        title = _clean(item.findtext("title"))
        link = _clean(item.findtext("link"))
        summary = _clean(item.findtext("description"))
        published = _parse_date(item.findtext("pubDate"))
        # Google News titles often end in " - Source"; strip only the exact source suffix.
        if title.endswith(f" - {source}"):
            title = title[:-(len(source)+3)].strip()
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
    for source, kind, spec in SOURCES:
        try:
            if kind == "google":
                url = "https://news.google.com/rss/search?q=" + quote_plus(spec) + "&hl=en-US&gl=US&ceid=US:en"
            else:
                url = spec
            got = _feed(url, source)
            if got:
                ok += 1
                rows.extend(got)
            else:
                errors.append(f"{source}: empty feed")
        except Exception as exc:
            errors.append(f"{source}: {type(exc).__name__}: {exc}")

    # Deduplicate by normalized title, newest first.
    seen = set()
    unique = []
    for row in sorted(rows, key=lambda x: x.get("published_at") or "", reverse=True):
        key = re.sub(r"\W+", "", row["title"].lower())
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(row)

    # Relevance preference: Baltic/Nordic/Europe and market-moving terms, then recency.
    boost = ("europe", "eu ", "baltic", "nordic", "finland", "sweden", "estonia", "latvia", "lithuania",
             "electricity", "power", "grid", "gas", "lng", "oil", "nuclear", "carbon", "energy")
    for row in unique:
        txt = (row["title"] + " " + row["summary"]).lower()
        row["_score"] = sum(1 for w in boost if w in txt)
    unique.sort(key=lambda x: (x["_score"], x.get("published_at") or ""), reverse=True)
    for r in unique:
        r.pop("_score", None)

    return unique[:max_items], NewsMeta(
        fetched_at=datetime.now(timezone.utc).isoformat(),
        sources_ok=ok,
        sources_total=len(SOURCES),
        errors=errors,
    )
