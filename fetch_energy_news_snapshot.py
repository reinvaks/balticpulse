from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from pathlib import Path
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

import requests

OUT = Path("data/energy_news.json")

# Curated high-authority / energy-specialist sources.
# Google News RSS is used as discovery transport so publisher-specific
# RSS/Cloudflare restrictions do not affect BalticPulse itself.
QUERIES = [
    ("Reuters", 'site:reuters.com (energy OR electricity OR power OR grid OR gas OR LNG OR oil OR nuclear OR carbon) when:3d'),
    ("S&P Global Energy", 'site:spglobal.com/energy (electricity OR power OR gas OR LNG OR oil OR nuclear OR carbon) when:3d'),
    ("IEA", 'site:iea.org/news (energy OR electricity OR gas OR oil OR renewables OR nuclear OR grids) when:7d'),
    ("Utility Dive", 'site:utilitydive.com (electricity OR utilities OR grid OR power OR nuclear OR storage) when:5d'),
    ("Energy Intelligence", 'site:energyintel.com (energy OR oil OR gas OR LNG OR power) when:5d'),
]

TOPICS = {
    "Elekter & võrk": ("electricity", "power", "grid", "transmission", "interconnector", "utility", "capacity"),
    "Gaas & LNG": ("gas", "lng", "pipeline", "storage"),
    "Nafta": ("oil", "crude", "opec", "refinery"),
    "Tuumaenergia": ("nuclear", "uranium", "reactor"),
    "Taastuvenergia": ("renewable", "wind", "solar", "hydro", "geothermal"),
    "Salvestus": ("battery", "storage", "bess"),
    "CO₂ & kliimapoliitika": ("carbon", "emission", "ets", "climate"),
    "Energiapoliitika": ("policy", "regulation", "sanction", "security", "market"),
}

def clean(s: str | None) -> str:
    s = unescape(s or "")
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", s).strip()

def topic(title: str, summary: str) -> str:
    text = f"{title} {summary}".lower()
    for name, words in TOPICS.items():
        if any(w in text for w in words):
            return name
    return "Energia"

def parsedate(s: str | None):
    if not s:
        return None
    try:
        dt = parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None

def fetch_query(source: str, query: str) -> list[dict]:
    url = (
        "https://news.google.com/rss/search?q="
        + quote_plus(query)
        + "&hl=en-US&gl=US&ceid=US:en"
    )
    r = requests.get(
        url,
        timeout=(5, 20),
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; BalticPulseNews/1.0)",
            "Accept": "application/rss+xml, application/xml, text/xml, */*",
        },
    )
    r.raise_for_status()

    root = ET.fromstring(r.content)
    rows = []
    for item in root.findall(".//item")[:40]:
        title = clean(item.findtext("title"))
        link = clean(item.findtext("link"))
        summary = clean(item.findtext("description"))
        pub = parsedate(item.findtext("pubDate"))

        # Google News commonly appends " - Publisher".
        suffix = f" - {source}"
        if title.endswith(suffix):
            title = title[:-len(suffix)].strip()

        if title and link:
            rows.append({
                "source": source,
                "published_at": pub.isoformat() if pub else "",
                "title": title,
                "summary": summary[:500],
                "topic": topic(title, summary),
                "url": link,
            })
    return rows

def main():
    rows = []
    errors = []
    ok = 0

    for source, query in QUERIES:
        try:
            got = fetch_query(source, query)
            if got:
                ok += 1
                rows.extend(got)
            else:
                errors.append(f"{source}: no stories")
        except Exception as exc:
            errors.append(f"{source}: {type(exc).__name__}: {exc}")

    # Deduplicate.
    seen = set()
    unique = []
    for row in sorted(rows, key=lambda x: x.get("published_at") or "", reverse=True):
        key = re.sub(r"\W+", "", row["title"].lower())
        if not key or key in seen:
            continue
        seen.add(key)
        unique.append(row)

    # Relevance ranking while keeping freshness as tie-breaker.
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

    previous = {}
    if OUT.exists():
        try:
            previous = json.loads(OUT.read_text(encoding="utf-8"))
        except Exception:
            previous = {}

    # Never replace a working snapshot with a completely empty failed run.
    if not unique and previous.get("items"):
        raise SystemExit("News refresh returned no items; preserving previous snapshot")

    payload = {
        "schema_version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "sources_ok": ok,
        "sources_total": len(QUERIES),
        "errors": errors,
        "items": unique[:40],
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(payload['items'])} stories to {OUT}; sources {ok}/{len(QUERIES)}")

if __name__ == "__main__":
    main()
