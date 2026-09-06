# Lenny's Jobs Canvas

Personal research canvas for [Lenny's Jobs](https://www.lennysjobs.com/) listings, powered by the public TrueUp search API — **no Clerk Bearer token**, **no per-job page scraping**.

Live (GitHub Pages): **https://cldev1.github.io/lennys-jobs-canvas/**

> Not affiliated with Lenny's Newsletter, Lenny's Jobs, or TrueUp. Snapshot data is for personal research; always verify openings on the source Apply link.

## What’s included

| Path | Purpose |
|------|---------|
| `scripts/fetch_lenny_jobs.py` | CLI to refresh `data/jobs.json` + `data/meta.json` |
| `data/jobs.json` | Slimmed job hits (Product-focused snapshot by default) |
| `data/meta.json` | Fetch metadata (query, count, timestamp) |
| `index.html` / `app.js` / `styles.css` | Static canvas: search, filters, sort, cards |

## API (guest `x-rc`, no Bearer)

```
POST https://arc.trueup.io/jobs/search
```

Headers of note:

- `Content-Type: application/json`
- `Origin` / `Referer`: `https://www.lennysjobs.com`
- `x-rc`: 16 hex chars — short-lived guest challenge derived from a **public client secret** shipped in TrueUp’s JS (not a user credential)

Body shape:

```json
[{"indexName":"job","params":{"query":"Product Manager","hitsPerPage":50,"page":0,"trueupRequestVersion":2,"trueupPartnerId":"lenny"}}]
```

Response: `{ "results": [{ "hits", "nbHits", "nbPages", "hitsPerPage" }] }`.

Hit fields used by the canvas include title, company, location, salary range, level, tags, apply `url`, trajectory score, etc. Full JD text is usually **not** in hits — we do not scrape apply pages.

The `x-rc` algorithm (HMAC-SHA1 TOTP-style counter over 30s windows, then HMAC-SHA256 truncated to 16 hex) is implemented in `scripts/fetch_lenny_jobs.py`. On HTTP 401 the script retries adjacent 30-second windows.

## Refresh data

```bash
# Recommended: partitioned Product Management category crawl (~full guest coverage)
python3 scripts/fetch_lenny_jobs.py --product-all

# Default: Product Manager query (API typically caps ~6 pages ≈ 300 hits per query)
python3 scripts/fetch_lenny_jobs.py

# Merge several Product-focused queries
python3 scripts/fetch_lenny_jobs.py \
  --queries "Product Manager,Senior Product Manager,Group Product Manager,Head of Product,Director of Product,Principal Product Manager,Staff Product Manager,VP of Product,Product Lead,Associate Product Manager" \
  --max-pages 10 --hits-per-page 50

# Single custom query
python3 scripts/fetch_lenny_jobs.py --query "Product" --max-pages 10 --hits-per-page 50

# All pages the API returns for a query (still subject to TrueUp depth cap)
python3 scripts/fetch_lenny_jobs.py --query "Product Manager" --all
```

Be polite: the guest `x-rc` endpoint rate-limits; the script backs off on 401 and retries adjacent 30s windows.

Then commit `data/jobs.json` and `data/meta.json`, push `main`, and sync the `gh-pages` branch.

## Local preview

```bash
cd /path/to/lennys-jobs-canvas
python3 -m http.server 8080
# open http://localhost:8080
```

## Visa / sponsorship

The guest search API does **not** expose visa or sponsorship fields. The canvas does not invent them — check each Apply link on the source posting.

## Extending the canvas

- Add filters in `app.js` (e.g. remote-only, AI-ready, valuation bands).
- Keep `KEEP_FIELDS` in the fetcher slim so `jobs.json` stays under ~5–8MB.
- Prefer a Product / PM query for the shipped snapshot; use `--all` only for offline research.

## GitHub Pages

This repo publishes from the **`gh-pages`** branch (root). After updating `main`, sync static assets + `data/` to `gh-pages`.

## License / attribution

Job data © respective employers / Lenny's Jobs / TrueUp. This repo is a personal tooling canvas — improve freely; do not misrepresent affiliation.
