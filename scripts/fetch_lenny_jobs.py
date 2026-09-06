#!/usr/bin/env python3
"""
Fetch job listings from Lenny's Jobs via the TrueUp search API.

Uses the public client x-rc challenge (guest auth) — no Clerk Bearer token.
The HMAC secret is embedded in TrueUp's client JS; it is a public client
challenge, not a user credential.

Usage:
  python scripts/fetch_lenny_jobs.py
  python scripts/fetch_lenny_jobs.py --query "Product Manager" --max-pages 40
  python scripts/fetch_lenny_jobs.py --queries "Product Manager,Senior Product Manager,Head of Product"
  python scripts/fetch_lenny_jobs.py --all   # all pages for the query (API still caps depth)
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import struct
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

API_URL = "https://arc.trueup.io/jobs/search"
API_PATH = "/jobs/search"
# Public client challenge secret from TrueUp JS (not a user credential).
X_RC_SECRET = "aA>nDcM@KMQV4Fb#:0xpR%}k}#6fPTqo"
PARTNER_ID = "lenny"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
ORIGIN = "https://www.lennysjobs.com"
PAGE_DELAY_SEC = 0.7
KEEP_FIELDS = (
    "objectID",
    "job_id",
    "title",
    "company_name",
    "company_id",
    "location",
    "url",
    "level",
    "salary_range_min",
    "salary_range_max",
    "valuation",
    "business_description_short",
    "description_tags",
    "updated_at",
    "trajectory_score",
    "ai_ready",
    "sponsored",
    "ownership_type",
    "normalized_domain",
    "ats_job_ref",
)

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"


def to_bytes(s: str) -> bytes:
    return bytes(ord(c) & 0xFF for c in s)


def x_rc(path: str = API_PATH, now: float | None = None) -> str:
    if now is None:
        now = time.time()
    counter = int(now) // 30
    # HMAC-SHA1 over packed counter; key is RAW secret bytes (do NOT sha1 first).
    digest = hmac.new(
        to_bytes(X_RC_SECRET), struct.pack(">Q", counter), hashlib.sha1
    ).digest()
    offset = digest[-1] & 0x0F
    code = (
        f"{((digest[offset] & 0x7F) << 24 | (digest[offset + 1] & 0xFF) << 16 | (digest[offset + 2] & 0xFF) << 8 | (digest[offset + 3] & 0xFF)) % 1000000:06d}"
    )
    return hmac.new(to_bytes(code), path.encode(), hashlib.sha256).hexdigest()[:16]


def slim_hit(hit: dict) -> dict:
    out = {k: hit.get(k) for k in KEEP_FIELDS if k in hit}
    tags = out.get("description_tags")
    if isinstance(tags, list) and len(tags) > 12:
        out["description_tags"] = tags[:12]
    return out


def post_search(query: str, page: int, hits_per_page: int, counter_offset: int = 0) -> dict:
    body = [
        {
            "indexName": "job",
            "params": {
                "query": query,
                "hitsPerPage": hits_per_page,
                "page": page,
                "trueupRequestVersion": 2,
                "trueupPartnerId": PARTNER_ID,
            },
        }
    ]
    now = time.time() + (counter_offset * 30)
    payload = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        API_URL,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Origin": ORIGIN,
            "Referer": f"{ORIGIN}/",
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "x-rc": x_rc(now=now),
        },
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_page(query: str, page: int, hits_per_page: int) -> dict:
    """POST one page; retry adjacent 30s windows on HTTP 401; backoff on burst limits."""
    last_err: Exception | None = None
    for attempt in range(4):
        for offset in (0, -1, 1, -2, 2):
            try:
                return post_search(query, page, hits_per_page, counter_offset=offset)
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 401:
                    # drain body so connection can close cleanly
                    try:
                        e.read()
                    except Exception:
                        pass
                    continue
                raise
        # Likely rate-limit 401s — wait for a fresh TOTP window
        wait = 8 + attempt * 10
        print(f"    401 on all windows; sleeping {wait}s (attempt {attempt + 1}/4)", flush=True)
        time.sleep(wait)
    assert last_err is not None
    raise last_err


def fetch_query(
    query: str,
    hits_per_page: int,
    max_pages: int | None,
    seen: set[str],
) -> tuple[list[dict], dict]:
    hits_out: list[dict] = []
    page = 0
    meta: dict = {}

    while True:
        if max_pages is not None and page >= max_pages:
            break
        print(f"  [{query}] page {page} …", flush=True)
        data = fetch_page(query, page, hits_per_page)
        results = data.get("results") or []
        if not results:
            break
        block = results[0]
        raw_hits = block.get("hits") or []
        meta = {
            "nbHits": block.get("nbHits"),
            "nbPages": block.get("nbPages"),
            "hitsPerPage": block.get("hitsPerPage", hits_per_page),
        }
        if not raw_hits:
            break
        for h in raw_hits:
            oid = h.get("objectID") or h.get("job_id")
            if not oid or oid in seen:
                continue
            seen.add(oid)
            hits_out.append(slim_hit(h))
        nb_pages = meta.get("nbPages")
        page += 1
        if nb_pages is not None and page >= int(nb_pages):
            break
        time.sleep(PAGE_DELAY_SEC)

    return hits_out, meta


def parse_queries(args: argparse.Namespace) -> list[str]:
    if args.queries:
        return [q.strip() for q in args.queries.split(",") if q.strip()]
    return [args.query]


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Lenny's Jobs via TrueUp (x-rc guest auth)")
    parser.add_argument(
        "--query",
        default="Product Manager",
        help='Search query (default: "Product Manager")',
    )
    parser.add_argument(
        "--queries",
        default="",
        help="Comma-separated queries to merge/dedupe (overrides --query when set)",
    )
    parser.add_argument("--hits-per-page", type=int, default=50, dest="hits_per_page")
    parser.add_argument(
        "--max-pages",
        type=int,
        default=40,
        help="Cap pages per query (default: 40; API often caps ~6 pages ≈ 300 hits)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Fetch all pages the API returns for each query (ignore --max-pages)",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DATA_DIR,
        help="Output directory (default: data/)",
    )
    args = parser.parse_args()

    max_pages = None if args.all else args.max_pages
    queries = parse_queries(args)
    print(
        f"Fetching queries={queries!r} hitsPerPage={args.hits_per_page} max_pages={max_pages}"
    )

    all_hits: list[dict] = []
    seen: set[str] = set()
    per_query_meta: list[dict] = []

    fatal = None
    for i, q in enumerate(queries):
        if i:
            time.sleep(1.5)
        try:
            hits, api_meta = fetch_query(q, args.hits_per_page, max_pages, seen)
            all_hits.extend(hits)
            per_query_meta.append({"query": q, **api_meta, "added": len(hits)})
            print(f"  → +{len(hits)} unique (total {len(all_hits)})", flush=True)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:500]
            print(f"  ! HTTP {e.code} on query {q!r}: {body}", file=sys.stderr)
            per_query_meta.append({"query": q, "error": f"HTTP {e.code}", "added": 0})
            fatal = e
            time.sleep(20)
            continue
        except Exception as e:
            print(f"  ! Error on query {q!r}: {e}", file=sys.stderr)
            per_query_meta.append({"query": q, "error": str(e), "added": 0})
            fatal = e
            continue

    if not all_hits and fatal is not None:
        if isinstance(fatal, urllib.error.HTTPError):
            return 1
        print(f"Error: {fatal}", file=sys.stderr)
        return 1

    args.out_dir.mkdir(parents=True, exist_ok=True)
    jobs_path = args.out_dir / "jobs.json"
    meta_path = args.out_dir / "meta.json"

    fetched_at = datetime.now(timezone.utc).isoformat()
    primary = queries[0] if len(queries) == 1 else ", ".join(queries)
    meta = {
        "source": "Lenny's Jobs via TrueUp arc.trueup.io",
        "partner": PARTNER_ID,
        "query": primary,
        "queries": queries,
        "fetched_at": fetched_at,
        "count": len(all_hits),
        "per_query": per_query_meta,
        "hits_per_page": args.hits_per_page,
        "max_pages": max_pages,
        "all": bool(args.all),
        "note": "Personal research canvas. Data from Lenny's Jobs / TrueUp. Not affiliated. API typically caps ~6 pages per query.",
    }

    jobs_path.write_text(
        json.dumps(all_hits, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    size_mb = jobs_path.stat().st_size / (1024 * 1024)
    print(f"Wrote {len(all_hits)} jobs → {jobs_path} ({size_mb:.2f} MB)")
    print(f"Wrote meta → {meta_path}")
    if size_mb > 8:
        print(
            "WARNING: jobs.json is over 8MB; consider fewer --queries or tighter --max-pages",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
