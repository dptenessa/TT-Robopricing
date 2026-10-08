#!/usr/bin/env python3
"""Incremental RSS -> SQLite event catalogue with optional local Qwen enrichment."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import re
import sqlite3
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from urllib.parse import urlparse

VERSION = "3.0-db-first"
CACHE_VERSION = "policy-v2"


def clean(value: object) -> str:
    value = html.unescape(str(value or ""))
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", value)).strip()


def fetch(url: str, timeout: int) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "T-Travel Event Radar/3.0"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def load_sources(path: Path) -> list[dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [x for x in data if x.get("enabled", True) and x.get("url")]


def child_text(node: ET.Element, names: tuple[str, ...]) -> str:
    for child in node.iter():
        if child is not node and child.tag.rsplit("}", 1)[-1].lower() in names and child.text:
            return clean(child.text)
    return ""


def parse_feed(data: bytes, feed_url: str) -> list[dict[str, str]]:
    root = ET.fromstring(data)
    source = urlparse(feed_url).netloc or feed_url
    result = []
    items = [n for n in root.iter() if n.tag.rsplit("}", 1)[-1].lower() in {"item", "entry"}]
    for item in items:
        title = child_text(item, ("title",))
        link = child_text(item, ("link",))
        if not link:
            for node in item.iter():
                if node.tag.rsplit("}", 1)[-1].lower() == "link" and node.get("href"):
                    link = node.get("href", "")
                    break
        if not title or not link:
            continue
        description = child_text(item, ("description", "summary", "content", "encoded"))
        item_id = hashlib.sha1((title.lower() + "|" + link.lower()).encode()).hexdigest()[:12]
        result.append({"event_id": item_id, "event_name": title, "description": description,
                       "source_url": link, "source": source})
    return result


def find_qwen(ollama_url: str) -> str | None:
    try:
        with urllib.request.urlopen(ollama_url.rstrip("/") + "/api/tags", timeout=2) as response:
            models = json.loads(response.read().decode("utf-8")).get("models", [])
        for model in models:
            name = str(model.get("name", ""))
            if name.lower().startswith("qwen"):
                return name
    except (OSError, urllib.error.URLError, json.JSONDecodeError):
        return None
    return None


def as_text(value: object) -> str:
    if isinstance(value, list):
        return "; ".join(str(x) for x in value)
    return "" if value is None else str(value).strip()


def valid_iso_date(value: object) -> str:
    text = as_text(value)
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return ""


def qwen_enrich(item: dict[str, str], model: str, ollama_url: str, cache_dir: Path) -> dict[str, str]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_key = item["source_url"] + "|" + CACHE_VERSION
    cache_file = cache_dir / (hashlib.sha256(cache_key.encode()).hexdigest()[:24] + ".json")
    if cache_file.exists():
        return json.loads(cache_file.read_text(encoding="utf-8"))
    fields = ["occasion_event", "occasion_type", "country", "destination", "region", "start_date", "end_date",
              "recurrence", "origin_markets", "estimated_total_participants", "estimated_foreign_visitors",
              "estimated_addressable_travellers", "typical_stay_length", "typical_stopovers", "main_traveller_segment",
              "interests_motivation", "connectivity_requirements", "recommended_product", "recommended_duration",
              "campaign_start", "campaign_end", "landing_page_recommendation", "seo_page_or_existing_page",
              "opportunity_score", "priority", "confidence", "notes", "rss_international_share",
              "rss_estimated_relevant_travellers", "rss_connectivity_relevance", "content_classification"]
    prompt = ("Return ONLY a JSON object with exactly these keys: " + ", ".join(fields) + ". Analyze this RSS item "
              "for a strict T-Travel travel-demand catalogue. Use empty strings when unknown; never invent numbers. "
              "start_date and end_date must be YYYY-MM-DD. If it is not a dated event, leave start_date empty. "
              "Only use priority values Activate, Prepare, Monitor, or Exclude. Exclude institutional meetings, "
              "policy/regulation news, airline/airport industry news, generic travel guides, awards, and articles "
              "without a real travel occasion. Activate means substantial international travel demand and clear "
              "mobile-data potential; Prepare means a credible campaign opportunity but requiring validation; "
              "Monitor means niche, small, uncertain, or mainly B2B; Exclude means not commercially useful for "
              "selling travel eSIMs. Use only these recommended products: S, M, L, Unlimited. S is email/light use; "
              "M is social media; L is video but not Netflix/streaming; Unlimited is up to 3 GB per day. "
              "Landing page rules: use an existing country page with an event or seasonal module for a single-country "
              "event; recommend a dedicated page only for a genuinely multinational event. SEO page should be "
              "Existing destination pages unless the event is multinational, where it may be a dedicated page. "
              "Score opportunity from 0 to 100 using international travel scale, addressable travellers, connectivity "
              "need, destination clarity, confidence, and strategic campaign value; do not score generic article value. "
              "Item: " + json.dumps(item, ensure_ascii=False))
    payload = json.dumps({"model": model, "prompt": prompt, "stream": False, "format": "json",
                          "options": {"temperature": 0}}).encode("utf-8")
    request = urllib.request.Request(ollama_url.rstrip("/") + "/api/generate", data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=300) as response:
        outer = json.loads(response.read().decode("utf-8"))
    result = json.loads(outer.get("response", "{}"))
    result = {key: as_text(result.get(key, "")) for key in fields}
    result["start_date"] = valid_iso_date(result["start_date"])
    result["end_date"] = valid_iso_date(result["end_date"])
    if result["recommended_product"] not in {"S", "M", "L", "Unlimited"}:
        result["recommended_product"] = ""
    if result["priority"] not in {"Activate", "Prepare", "Monitor", "Exclude"}:
        result["priority"] = "Monitor"
    cache_file.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return result


def get_columns(db: sqlite3.Connection) -> list[str]:
    columns = [row[1] for row in db.execute('PRAGMA table_info("events")')]
    if not columns:
        raise RuntimeError("events.db has no events table. Run migrate_to_db.py first.")
    required = {"Occasion_ID", "Occasion_Event", "Start_Date", "Source_URL"}
    missing = sorted(required - set(columns))
    if missing:
        raise RuntimeError("events table is missing columns: " + ", ".join(missing))
    return columns


def main() -> int:
    parser = argparse.ArgumentParser(description="Daily DB-first RSS event collector")
    parser.add_argument("--database", default="events.db")
    parser.add_argument("--sources", type=Path, default=Path("sources.json"))
    parser.add_argument("--ollama-url", default="http://127.0.0.1:11434")
    parser.add_argument("--limit", type=int, default=0, help="Maximum new items sent to Qwen")
    parser.add_argument("--no-ai", action="store_true")
    parser.add_argument("--timeout", type=int, default=20)
    args = parser.parse_args()
    today = date.today().isoformat()
    db = sqlite3.connect(args.database)
    columns = get_columns(db)
    existing = db.execute('SELECT * FROM events').fetchall()
    url_index = columns.index("Source_URL")
    existing_urls = {str(row[url_index] or "").strip() for row in existing}
    model = None if args.no_ai else find_qwen(args.ollama_url)
    print(f"T-Travel Event Radar {VERSION}")
    print("Qwen:", model or "unavailable; new undated RSS items will be skipped")
    candidates = []
    for source in load_sources(args.sources):
        try:
            for item in parse_feed(fetch(source["url"], args.timeout), source["url"]):
                if item["source_url"] not in existing_urls:
                    candidates.append(item)
                    existing_urls.add(item["source_url"])
            print("OK ", source.get("name", source["url"]))
        except Exception as exc:
            print("ERR", source.get("name", source["url"]), exc)
    to_process = candidates if not args.limit else candidates[:args.limit]
    model_cache = Path("cache/master_ai_daily")
    existing_ids = {
        str(row[columns.index("Occasion_ID")] or "").strip()
        for row in existing
    }
    next_id, added = len(existing) + 1, 0
    mapping = {
        "occasion_type": "Occasion_Type", "country": "Country", "destination": "Destination", "region": "Region",
        "recurrence": "Recurrence", "origin_markets": "Origin_Markets", "estimated_total_participants": "Estimated_Total_Participants",
        "estimated_foreign_visitors": "Estimated_Foreign_Visitors", "estimated_addressable_travellers": "Estimated_Addressable_Travellers",
        "typical_stay_length": "Typical_Stay_Length", "typical_stopovers": "Typical_Stopovers", "main_traveller_segment": "Main_Traveller_Segment",
        "interests_motivation": "Interests_Motivation", "connectivity_requirements": "Connectivity_Requirements", "recommended_product": "Recommended_Product",
        "recommended_duration": "Recommended_Duration", "campaign_start": "Campaign_Start", "campaign_end": "Campaign_End",
        "landing_page_recommendation": "Landing_Page_Recommendation", "seo_page_or_existing_page": "SEO_Page_or_Existing_Page",
        "opportunity_score": "Opportunity_Score", "priority": "Priority", "confidence": "Confidence", "rss_international_share": "RSS_International_Share",
        "rss_estimated_relevant_travellers": "RSS_Estimated_Relevant_Travellers", "rss_connectivity_relevance": "RSS_Connectivity_Relevance",
        "content_classification": "Content_Classification",
    }
    for index, item in enumerate(to_process, 1):
        if not model:
            continue
        try:
            ai = qwen_enrich(item, model, args.ollama_url, model_cache)
            start_date = ai.get("start_date", "")
            if not start_date:
                print(f"SKIP {item['event_name']} [no Start Date]")
                continue
            while f"RSS{next_id:04d}" in existing_ids:
                next_id += 1
            row = {column: "" for column in columns}
            row.update({"Occasion_ID": f"RSS{next_id:04d}", "Occasion_Event": ai.get("occasion_event") or item["event_name"],
                        "Start_Date": start_date, "End_Date": ai.get("end_date", "") or start_date,
                        "Source_URL": item["source_url"], "Source_Type": "RSS", "Last_Researched": today,
                        "Notes": ai.get("notes", "") or item["description"], "first_seen": today, "last_seen": today, "times_seen": "1"})
            for source_key, db_key in mapping.items():
                if db_key in row and ai.get(source_key):
                    row[db_key] = ai[source_key]
            quoted = ",".join(f'"{column}"' for column in columns)
            placeholders = ",".join("?" for _ in columns)
            db.execute(f'INSERT INTO events ({quoted}) VALUES ({placeholders})', [row[column] for column in columns])
            existing_ids.add(row["Occasion_ID"])
            next_id += 1
            added += 1
            print(f"AI {index}/{len(to_process)} {item['event_name']} [added]")
        except Exception as exc:
            print(f"AI ERR {item['event_name']}: {exc}")
    db.commit()
    total = db.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    db.close()
    print(f"Added {added} new dated RSS events; database now contains {total} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
