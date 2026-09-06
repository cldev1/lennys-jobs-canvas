#!/usr/bin/env python3
"""
Fetch job listings from Lenny's Jobs via the TrueUp search API.

Uses the public client x-rc challenge (guest auth) — no Clerk Bearer token.
The HMAC secret is embedded in TrueUp's client JS; it is a public client
challenge, not a user credential.

Usage:
  python scripts/fetch_lenny_jobs.py
  python scripts/fetch_lenny_jobs.py --query "Product Manager" --max-pages 40
  python scripts/fetch_lenny_jobs.py --queries "Product Manager,Senior Product Manager"
  python scripts/fetch_lenny_jobs.py --all
  python scripts/fetch_lenny_jobs.py --product-all   # partitioned PM category crawl
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import re
import struct
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

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
PAGE_DELAY_SEC = 0.75
PARTITION_DELAY_SEC = 0.55
GUEST_HIT_CAP = 300  # ~6 pages × 50
REMOTE_RE = re.compile(r"remote", re.I)
REMOTE_LOCATION_FACETS = (
    "🌎 Remote",
    "🇺🇸 United States (remote)",
)

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

PM_CATEGORY = "job_categories_lvl0:Product Management"
# Prefer location then customer_type (binary) before multi-valued themes
# to keep leaf count manageable and reduce theme-gap misses.
SPLIT_FACETS = (
    "level",
    "company_stage",
    "job_locations_combined",
    "customer_type",
    "themes",
)

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"


def to_bytes(s: str) -> bytes:
    return bytes(ord(c) & 0xFF for c in s)


def x_rc(path: str = API_PATH, now: float | None = None) -> str:
    if now is None:
        now = time.time()
    counter = int(now) // 30
    digest = hmac.new(
        to_bytes(X_RC_SECRET), struct.pack(">Q", counter), hashlib.sha1
    ).digest()
    offset = digest[-1] & 0x0F
    code = (
        f"{((digest[offset] & 0x7F) << 24 | (digest[offset + 1] & 0xFF) << 16 | (digest[offset + 2] & 0xFF) << 8 | (digest[offset + 3] & 0xFF)) % 1000000:06d}"
    )
    return hmac.new(to_bytes(code), path.encode(), hashlib.sha256).hexdigest()[:16]


def is_remote_job(hit: dict) -> bool:
    loc = hit.get("location") or ""
    if REMOTE_RE.search(str(loc)):
        return True
    # Some hits expose combined location facets as a list
    combined = hit.get("job_locations_combined")
    if isinstance(combined, list):
        for v in combined:
            if v in REMOTE_LOCATION_FACETS or REMOTE_RE.search(str(v)):
                return True
    elif isinstance(combined, str) and (
        combined in REMOTE_LOCATION_FACETS or REMOTE_RE.search(combined)
    ):
        return True
    return False


def slim_hit(hit: dict, *, force_remote: bool | None = None) -> dict:
    out = {k: hit.get(k) for k in KEEP_FIELDS if k in hit}
    tags = out.get("description_tags")
    if isinstance(tags, list) and len(tags) > 12:
        out["description_tags"] = tags[:12]
    if force_remote is True:
        out["remote"] = True
    else:
        out["remote"] = is_remote_job(hit)
    return out


def post_search(
    *,
    query: str = "",
    page: int = 0,
    hits_per_page: int = 50,
    facet_filters: list[list[str]] | None = None,
    facets: list[str] | None = None,
    counter_offset: int = 0,
) -> dict:
    params: dict[str, Any] = {
        "query": query,
        "hitsPerPage": hits_per_page,
        "page": page,
        "trueupRequestVersion": 2,
        "trueupPartnerId": PARTNER_ID,
    }
    if facet_filters is not None:
        params["facetFilters"] = facet_filters
    if facets is not None:
        params["facets"] = facets
        params["maxValuesPerFacet"] = 100
    body = [{"indexName": "job", "params": params}]
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


def fetch_page(
    *,
    query: str = "",
    page: int = 0,
    hits_per_page: int = 50,
    facet_filters: list[list[str]] | None = None,
    facets: list[str] | None = None,
) -> dict:
    """POST one page; retry adjacent 30s windows on HTTP 401; backoff on bursts."""
    last_err: Exception | None = None
    for attempt in range(6):
        for offset in (0, -1, 1, -2, 2):
            try:
                return post_search(
                    query=query,
                    page=page,
                    hits_per_page=hits_per_page,
                    facet_filters=facet_filters,
                    facets=facets,
                    counter_offset=offset,
                )
            except urllib.error.HTTPError as e:
                last_err = e
                if e.code == 401:
                    try:
                        e.read()
                    except Exception:
                        pass
                    continue
                # 429 / 5xx — backoff then retry
                if e.code in (429, 500, 502, 503, 504):
                    try:
                        e.read()
                    except Exception:
                        pass
                    break
                raise
        wait = 8 + attempt * 12
        print(
            f"    401/burst on all windows; sleeping {wait}s (attempt {attempt + 1}/6)",
            flush=True,
        )
        time.sleep(wait)
    assert last_err is not None
    raise last_err


def result_block(data: dict) -> dict:
    results = data.get("results") or []
    if not results:
        return {}
    return results[0] or {}


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
        data = fetch_page(query=query, page=page, hits_per_page=hits_per_page)
        block = result_block(data)
        if not block:
            break
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
        # Guest depth often hard-caps ~6 pages even when nbPages claims more
        if page >= 6 and hits_per_page >= 50:
            # still honor API nbPages if smaller; otherwise stop at guest cap
            if nb_pages is None or int(nb_pages) > 6:
                # continue only if we still get hits; loop handles empty
                pass
        time.sleep(PAGE_DELAY_SEC)

    return hits_out, meta


def facet_label(filters: list[list[str]]) -> str:
    parts = []
    for group in filters:
        if len(group) == 1:
            parts.append(group[0])
        else:
            parts.append("{" + " | ".join(group) + "}")
    return " AND ".join(parts) if parts else "(none)"


def discover_facet_values(
    facet_filters: list[list[str]], facet: str
) -> list[tuple[str, int]]:
    data = fetch_page(
        query="",
        page=0,
        hits_per_page=0,
        facet_filters=facet_filters,
        facets=[facet],
    )
    block = result_block(data)
    vals = (block.get("facets") or {}).get(facet) or {}
    return sorted(vals.items(), key=lambda kv: (-kv[1], kv[0]))


def paginate_partition(
    facet_filters: list[list[str]],
    hits_per_page: int,
    seen: set[str],
    *,
    force_remote: bool | None = None,
    label: str = "",
) -> tuple[list[dict], dict]:
    hits_out: list[dict] = []
    page = 0
    meta: dict = {"nbHits": 0, "nbPages": 0, "fetched": 0, "added": 0}

    while True:
        print(f"  [{label or facet_label(facet_filters)}] page {page} …", flush=True)
        data = fetch_page(
            query="",
            page=page,
            hits_per_page=hits_per_page,
            facet_filters=facet_filters,
        )
        block = result_block(data)
        if not block:
            break
        raw_hits = block.get("hits") or []
        meta["nbHits"] = block.get("nbHits", meta["nbHits"])
        meta["nbPages"] = block.get("nbPages", meta["nbPages"])
        if not raw_hits:
            break
        for h in raw_hits:
            oid = h.get("objectID") or h.get("job_id")
            meta["fetched"] = int(meta.get("fetched") or 0) + 1
            if not oid or oid in seen:
                continue
            seen.add(oid)
            hits_out.append(slim_hit(h, force_remote=force_remote))
        page += 1
        nb_pages = block.get("nbPages")
        # Guest API typically serves at most ~6 pages (300 hits)
        hard_cap_pages = max(1, (GUEST_HIT_CAP + hits_per_page - 1) // hits_per_page)
        if nb_pages is not None and page >= int(nb_pages):
            break
        if page >= hard_cap_pages:
            break
        time.sleep(PAGE_DELAY_SEC)

    meta["added"] = len(hits_out)
    meta["pages_fetched"] = page
    return hits_out, meta


def next_split_facet(used: set[str]) -> str | None:
    for f in SPLIT_FACETS:
        if f not in used:
            return f
    return None


def used_facets_from_filters(facet_filters: list[list[str]]) -> set[str]:
    used: set[str] = set()
    for group in facet_filters:
        for item in group:
            if ":" in item:
                used.add(item.split(":", 1)[0])
    return used


def build_partitions(
    base_filters: list[list[str]],
    *,
    primary_axes: tuple[str, ...] = ("level", "company_stage"),
) -> list[dict]:
    """
    Build leaf partitions under base_filters.
    Primary split: cartesian product of level × company_stage (when available).
    Leaves with nbHits > GUEST_HIT_CAP are further split by remaining facets.
    """
    leaves: list[dict] = []

    # Probe primary axis values
    axis_values: dict[str, list[str]] = {}
    for axis in primary_axes:
        time.sleep(PARTITION_DELAY_SEC)
        pairs = discover_facet_values(base_filters, axis)
        axis_values[axis] = [k for k, _ in pairs]
        print(f"  facet {axis}: {len(axis_values[axis])} values", flush=True)

    # Cartesian primary partitions (skip empty axes)
    active_axes = [a for a in primary_axes if axis_values.get(a)]
    if not active_axes:
        return _expand_leaf(base_filters, leaves)

    from itertools import product

    combos = list(product(*[[(a, v) for v in axis_values[a]] for a in active_axes]))
    print(f"  primary combos: {len(combos)}", flush=True)

    for combo in combos:
        ff = [list(g) for g in base_filters]  # deep-ish copy of groups
        for axis, val in combo:
            ff.append([f"{axis}:{val}"])
        time.sleep(PARTITION_DELAY_SEC)
        data = fetch_page(query="", page=0, hits_per_page=0, facet_filters=ff)
        nb = int(result_block(data).get("nbHits") or 0)
        label = facet_label(ff)
        if nb <= 0:
            continue
        if nb <= GUEST_HIT_CAP:
            leaves.append({"facetFilters": ff, "nbHits": nb, "label": label})
            print(f"  leaf OK ({nb}): {label}", flush=True)
        else:
            print(f"  split ({nb}): {label}", flush=True)
            _split_recursive(ff, nb, leaves)
    return leaves


def _expand_leaf(facet_filters: list[list[str]], leaves: list[dict]) -> list[dict]:
    data = fetch_page(query="", page=0, hits_per_page=0, facet_filters=facet_filters)
    nb = int(result_block(data).get("nbHits") or 0)
    if nb > 0:
        if nb <= GUEST_HIT_CAP:
            leaves.append(
                {
                    "facetFilters": facet_filters,
                    "nbHits": nb,
                    "label": facet_label(facet_filters),
                }
            )
        else:
            _split_recursive(facet_filters, nb, leaves)
    return leaves


def _append_truncated(facet_filters: list[list[str]], nb_hits: int, leaves: list[dict]) -> None:
    leaves.append(
        {
            "facetFilters": facet_filters,
            "nbHits": nb_hits,
            "label": facet_label(facet_filters),
            "truncated": True,
        }
    )
    print(
        f"  ! truncated leaf ({nb_hits} > {GUEST_HIT_CAP}): {facet_label(facet_filters)}",
        flush=True,
    )


def _split_recursive(
    facet_filters: list[list[str]],
    nb_hits: int,
    leaves: list[dict],
) -> None:
    """Try remaining SPLIT_FACETS; skip sparse facets that barely cover the parent."""
    used = used_facets_from_filters(facet_filters)

    for facet in SPLIT_FACETS:
        if facet in used:
            continue
        time.sleep(PARTITION_DELAY_SEC)
        pairs = discover_facet_values(facet_filters, facet)
        if not pairs:
            print(f"  skip facet {facet}: no values", flush=True)
            continue

        children: list[tuple[str, int, list[list[str]]]] = []
        for val, _count in pairs:
            child_ff = [list(g) for g in facet_filters] + [[f"{facet}:{val}"]]
            time.sleep(PARTITION_DELAY_SEC)
            data = fetch_page(
                query="", page=0, hits_per_page=0, facet_filters=child_ff
            )
            child_nb = int(result_block(data).get("nbHits") or 0)
            if child_nb > 0:
                children.append((val, child_nb, child_ff))

        if not children:
            print(f"  skip facet {facet}: all child probes empty", flush=True)
            continue

        sum_child = sum(c[1] for c in children)
        max_child = max(c[1] for c in children)
        # Sparse facets (e.g. customer_type) often cover << parent — skip them.
        # Multi-valued facets (themes, locations) often sum_child >> nb_hits; that's OK.
        if sum_child < nb_hits * 0.55:
            print(
                f"  skip facet {facet}: weak coverage {sum_child}/{nb_hits}",
                flush=True,
            )
            continue
        # Facet must actually shrink the largest bucket (or already be within cap).
        if max_child > GUEST_HIT_CAP and max_child >= nb_hits * 0.92:
            print(
                f"  skip facet {facet}: does not shrink (max={max_child} of {nb_hits})",
                flush=True,
            )
            continue

        print(
            f"  using facet {facet}: {len(children)} children, "
            f"sum={sum_child}, max={max_child} (parent {nb_hits})",
            flush=True,
        )
        for _val, child_nb, child_ff in children:
            label = facet_label(child_ff)
            if child_nb <= GUEST_HIT_CAP:
                leaves.append(
                    {"facetFilters": child_ff, "nbHits": child_nb, "label": label}
                )
                print(f"  leaf OK ({child_nb}): {label}", flush=True)
            else:
                print(f"  split ({child_nb}): {label}", flush=True)
                _split_recursive(child_ff, child_nb, leaves)
        return

    _append_truncated(facet_filters, nb_hits, leaves)


def fetch_product_all(hits_per_page: int, seen: set[str]) -> tuple[list[dict], dict]:
    """Partitioned crawl of Product Management category (~full guest coverage)."""
    base = [[PM_CATEGORY]]
    print("Probing Product Management category nbHits…", flush=True)
    data = fetch_page(
        query="",
        page=0,
        hits_per_page=0,
        facet_filters=base,
        facets=list(SPLIT_FACETS),
    )
    block = result_block(data)
    category_nb = int(block.get("nbHits") or 0)
    print(f"  category nbHits={category_nb}", flush=True)

    # Also record PM query nbHits for coverage meta
    time.sleep(PARTITION_DELAY_SEC)
    qdata = fetch_page(query="Product Manager", page=0, hits_per_page=0)
    query_nb = int(result_block(qdata).get("nbHits") or 0)
    print(f"  'Product Manager' query nbHits={query_nb}", flush=True)

    print("Building partitions (level × company_stage, then deepen)…", flush=True)
    leaves = build_partitions(base)
    print(f"  total leaf partitions: {len(leaves)}", flush=True)

    all_hits: list[dict] = []
    partition_stats: list[dict] = []

    for i, leaf in enumerate(leaves):
        ff = leaf["facetFilters"]
        label = leaf["label"]
        force_remote = None
        # If partition is scoped to a remote location facet, force remote=True
        for group in ff:
            for item in group:
                if item.startswith("job_locations_combined:"):
                    loc_val = item.split(":", 1)[1]
                    if loc_val in REMOTE_LOCATION_FACETS or REMOTE_RE.search(loc_val):
                        force_remote = True
        if i:
            time.sleep(PARTITION_DELAY_SEC)
        try:
            hits, meta = paginate_partition(
                ff,
                hits_per_page,
                seen,
                force_remote=force_remote,
                label=f"{i + 1}/{len(leaves)} {label}",
            )
            all_hits.extend(hits)
            partition_stats.append(
                {
                    "label": label,
                    "nbHits": leaf.get("nbHits"),
                    "truncated": bool(leaf.get("truncated")),
                    "added": meta.get("added", 0),
                    "fetched": meta.get("fetched", 0),
                    "pages_fetched": meta.get("pages_fetched", 0),
                }
            )
            print(
                f"  → +{meta.get('added', 0)} unique (total {len(seen)}) "
                f"[leaf nbHits={leaf.get('nbHits')}]",
                flush=True,
            )
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:300]
            print(f"  ! HTTP {e.code} on {label}: {body}", file=sys.stderr)
            partition_stats.append(
                {
                    "label": label,
                    "nbHits": leaf.get("nbHits"),
                    "error": f"HTTP {e.code}",
                    "added": 0,
                }
            )
            time.sleep(20)
        except Exception as e:
            print(f"  ! Error on {label}: {e}", file=sys.stderr)
            partition_stats.append(
                {
                    "label": label,
                    "nbHits": leaf.get("nbHits"),
                    "error": str(e),
                    "added": 0,
                }
            )

    remote_count = sum(1 for h in all_hits if h.get("remote"))
    meta_out = {
        "mode": "product-all",
        "category": "Product Management",
        "category_nbHits": category_nb,
        "product_manager_query_nbHits": query_nb,
        "leaf_partitions": len(leaves),
        "truncated_partitions": sum(1 for p in partition_stats if p.get("truncated")),
        "partition_stats": partition_stats,
        "remote_count": remote_count,
        "coverage_vs_category": (
            round(len(all_hits) / category_nb, 4) if category_nb else None
        ),
        "coverage_vs_pm_query": (
            round(len(all_hits) / query_nb, 4) if query_nb else None
        ),
    }
    return all_hits, meta_out


def parse_queries(args: argparse.Namespace) -> list[str]:
    if args.queries:
        return [q.strip() for q in args.queries.split(",") if q.strip()]
    return [args.query]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Fetch Lenny's Jobs via TrueUp (x-rc guest auth)"
    )
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
        "--product-all",
        action="store_true",
        dest="product_all",
        help=(
            "Partitioned crawl of Product Management category: split by "
            "level × company_stage, then job_locations_combined / themes / "
            "customer_type until each leaf is within the guest ~300-hit cap"
        ),
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=DATA_DIR,
        help="Output directory (default: data/)",
    )
    args = parser.parse_args()

    all_hits: list[dict] = []
    seen: set[str] = set()
    per_query_meta: list[dict] = []
    product_meta: dict | None = None
    fatal = None

    if args.product_all:
        print(
            f"Fetching --product-all hitsPerPage={args.hits_per_page} "
            f"(guest cap ~{GUEST_HIT_CAP}/partition)"
        )
        try:
            all_hits, product_meta = fetch_product_all(args.hits_per_page, seen)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")[:500]
            print(f"HTTP {e.code}: {body}", file=sys.stderr)
            return 1
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            return 1
        queries = ["(product-all / Product Management)"]
        max_pages = None
    else:
        max_pages = None if args.all else args.max_pages
        queries = parse_queries(args)
        print(
            f"Fetching queries={queries!r} hitsPerPage={args.hits_per_page} "
            f"max_pages={max_pages}"
        )

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
    remote_count = sum(1 for h in all_hits if h.get("remote"))
    meta: dict[str, Any] = {
        "source": "Lenny's Jobs via TrueUp arc.trueup.io",
        "partner": PARTNER_ID,
        "query": primary,
        "queries": queries,
        "fetched_at": fetched_at,
        "count": len(all_hits),
        "remote_count": remote_count,
        "hits_per_page": args.hits_per_page,
        "max_pages": max_pages,
        "all": bool(args.all),
        "product_all": bool(args.product_all),
        "note": (
            "Personal research canvas. Data from Lenny's Jobs / TrueUp. Not affiliated. "
            "Guest API caps ~6 pages (~300 hits) per query/filter combo; "
            "--product-all partitions by facets to approach full PM coverage. "
            "Visa/sponsorship is not exposed by this API — do not invent it."
        ),
    }
    if product_meta:
        meta.update(product_meta)
    else:
        meta["per_query"] = per_query_meta

    jobs_path.write_text(
        json.dumps(all_hits, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    meta_path.write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")

    size_mb = jobs_path.stat().st_size / (1024 * 1024)
    print(f"Wrote {len(all_hits)} jobs → {jobs_path} ({size_mb:.2f} MB)")
    print(f"  remote={remote_count}")
    if product_meta:
        print(
            f"  coverage vs category {product_meta.get('category_nbHits')}: "
            f"{product_meta.get('coverage_vs_category')}"
        )
        print(
            f"  coverage vs PM query {product_meta.get('product_manager_query_nbHits')}: "
            f"{product_meta.get('coverage_vs_pm_query')}"
        )
    print(f"Wrote meta → {meta_path}")
    if size_mb > 12:
        print(
            "WARNING: jobs.json is over 12MB; consider slimming KEEP_FIELDS",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
