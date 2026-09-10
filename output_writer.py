"""
output_writer.py
-----------------
Shared row models + JSON/CSV writers used by all three scrapers.

Two modes, one row shape
------------------------
    --mode listing   a search grid or a category listing -> Product
    --mode product   one /{shop}/{slug} detail page -> Product, with the
                     trailing detail-only fields populated

Both modes yield the SAME class, because on Tokopedia a detail page is not a
different kind of object from a tile — it is the same product described more
fully. So there is no second dataclass here (a sibling repo needs one for
reviews; this one does not), and `diff_runs.py` can compare a listing run
against a product run on the columns both populate.

There is deliberately no `--mode shop`. A Tokopedia shop front is its own
application shell with its own markup, none of which has been captured or
measured, and a mode that ships untested would be worse than a mode that is
absent. `product_parser.shop_metadata` reads the seller's facts off a detail
page for the sidecar, which is what a run covering one product can honestly
say about its seller.

`Product` keeps the family's first sixteen columns in the family's order,
with the Tokopedia-specific ones appended after `price_source`, so a consumer
written against another repo in this family still reads the prefix unchanged.

Everything below is row-class-agnostic: pass `row_cls` so an empty CSV still
gets the right header for the mode that produced it.
"""

import csv
import json
from dataclasses import dataclass, asdict, field, fields
from datetime import datetime, timezone
from typing import Optional, List, Set, Sequence, Any, Type


# The hostname a row came from. Tokopedia is ONE storefront — measured by
# fetching the same URL from an Indonesian and a US exit and getting
# identical markup, identical `<html lang="id">` and identical IDR prices —
# so this column is `tokopedia.com` on every row of every run. It is kept
# because the family's schema has it in this position and consumers read the
# columns by name across repos.
SOURCE_DEFAULT = "tokopedia.com"


@dataclass
class Product:
    source: str = SOURCE_DEFAULT
    scraped_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    url: str = ""
    # The product's `/{shop-slug}/{product-slug}` path, and NOT the 19-digit
    # tail most slugs end in.
    #
    # That tail is tempting and wrong. The site's own id for this product is
    # `pdpBasicInfo.productID` in a detail page's Apollo cache —
    # 103490518624 where the URL tail is 1731177319241910164 — and 4 of 40
    # listing URLs carry no tail at all (two carry a short hex suffix
    # instead), all four ordinary organic products with identical markup. So
    # a tail-derived sku would have been a different number than the site's,
    # null on a tenth of every run, and nobody would have noticed either.
    #
    # The path is always present, is what the site's own canonical uses, and
    # is what a listing row and a detail row join on. `product_id` below
    # carries the real numeric id where a page states it.
    sku: Optional[str] = None
    title: Optional[str] = None
    # The SHOP. On a marketplace of small sellers the shop IS the brand, and
    # Tokopedia publishes no manufacturer field anywhere, so this column
    # carries the seller's display name instead of being null on every row.
    # Read from the tile on a search page (95/95), from the badge image's alt
    # on a category tile, and from `pdpBasicInfo.shopName` on a detail page;
    # falls back to the slug in the URL, which names the same seller.
    brand: Optional[str] = None
    price: Optional[float] = None
    # IDR, and on this site that is a fact rather than a guess: one
    # storefront, one currency, and the two exit countries tested returned
    # zero price differences across the 68 products both saw. Still null
    # rather than defaulted when no price was found — a row with no price has
    # no currency either.
    currency: Optional[str] = None
    # The was-price, from the tile's strike node. 88 of 95 search tiles carry
    # one; a CATEGORY tile carries none at all (0 of 60), which is a property
    # of that page kind and not a parsing failure. Null in --mode product: a
    # detail page has no strikethrough of its own and the "similar products"
    # carousel's strikes belong to other products.
    original_price: Optional[float] = None
    # Computed from the two prices, never read off the printed badge.
    # Tokopedia only prints the badge above some threshold — two tiles carry
    # a strike at 7% and 8% off with no badge — so computing recovers 88 rows
    # where reading recovers 86, and where both exist they agreed on all 86.
    discount_pct: Optional[float] = None
    # The PRODUCT's rating, which is what a Tokopedia tile prints — worth
    # stating because a sibling repo's tile stars are the SHOP's, and folding
    # the two together would make one column mean different things per site.
    # 95/95 on a search page, 0/60 on a category page (that tile kind prints
    # none at all), and `pdpBasicInfo.stats.rating` on a detail page.
    rating: Optional[float] = None
    # Null on every listing row and populated in --mode product, from
    # `stats.countReview`. A tile prints the sold count, never the review
    # count.
    review_count: Optional[int] = None
    # Only a detail page says: `status == "ACTIVE"` with a non-zero
    # `maxOrder`. A tile does not state availability at all, so this is null
    # on a listing run rather than assumed true.
    in_stock: Optional[bool] = None
    # Sparse ON PURPOSE, and this is the trap on this site.
    #
    # Tokopedia lazy-loads tile images, so a tile below the fold carries a
    # PLACEHOLDER in `src`: 55 of 95 search tiles held an SVG under
    # `/zeus_v2/` and 30 of 60 category tiles held a `data:` URI. Reading
    # `src` blindly gives a column that is 100% populated and half wrong, so
    # a real image is recognised positively by its host and everything else
    # is null. Scroll further and more rows fill in.
    image_url: Optional[str] = None
    category: Optional[str] = None
    # Where `price` came from:
    #   "dom"          the rendered tile. The ONLY case on a listing page —
    #                  there is no structured data on one to confirm against,
    #                  which is measured rather than assumed (0 JSON-LD, 0
    #                  __NEXT_DATA__, 0 Apollo state on six captures).
    #   "meta+apollo"  --mode product: the price from the page's own
    #                  `product:price:amount` meta, everything else from its
    #                  Apollo cache.
    #   "meta"         --mode product where the Apollo cache was absent.
    # diff_runs.py reports a price change that comes with a price_source
    # change as `source_changed`, not `changed`: that says something about
    # our own two snapshots, not about Tokopedia.
    price_source: Optional[str] = None

    # ---- Tokopedia-specific, appended so the family prefix stays stable ----
    # Which listing page this row came from (1-based) and its position in
    # that page as the site ordered it. Without `page`, `position` is
    # ambiguous — it restarts at 1 on every page. Both null in --mode
    # product, where there is no page.
    page: Optional[int] = None
    position: Optional[int] = None
    # The seller's URL slug, from the row's own path. Always present, and
    # kept beside `brand` because a shop's display name can change and its
    # slug is what the URL commits to.
    shop_slug: Optional[str] = None
    # The seller's city, which a search tile prints as the second of its two
    # `span.flip` nodes ("Kab. Magetan", "Jakarta Timur"). 95/95 on search,
    # absent on a category tile and on a detail page.
    shop_location: Optional[str] = None
    # Units sold. On a tile this is a FLOOR the site has rounded down —
    # "100rb+ terjual" is 100_000 for a product whose detail page states
    # 207785 — and `sold_is_floor` is what says which kind of number this
    # is. Without that flag one column would silently mean two things and a
    # diff between a listing run and a product run would report every row as
    # changed.
    sold: Optional[int] = None
    sold_is_floor: Optional[bool] = None
    # The 19-digit tail on the product slug, where the slug has one (36 of
    # 40 measured). Named for what it is so a future edit cannot mistake it
    # for the product id, which it is not.
    slug_id: Optional[str] = None
    # ---- populated by --mode product only; null on a listing run ----
    # The site's OWN product id, from `pdpBasicInfo.productID`. The number
    # Tokopedia's app deep links use (`product/103490518624`) and the only
    # id that is stable across a slug rename.
    product_id: Optional[str] = None
    shop_id: Optional[str] = None
    # "New" or "Used", from `pdpBasicInfo.condition`. A marketplace field
    # with no equivalent in the sibling repos, and the reason a price of
    # 30_000 IDR for a phone is not necessarily wrong.
    condition: Optional[str] = None
    # Shipping weight normalised to grams. The site states a number and a
    # unit separately (`weight: 1`, `weightUnit: "KILOGRAM"`); a column
    # mixing the two units would be a number nobody can compare.
    weight_grams: Optional[int] = None
    # `maxOrder` — the largest quantity the seller will accept in one order.
    # A ceiling on available stock rather than the stock level, which
    # Tokopedia does not publish, so it is named for the field it is.
    stock_max: Optional[int] = None
    # `createdAt` — when the seller listed it. Genuinely useful for spotting
    # a relisted product whose price history restarts.
    listed_at: Optional[str] = None
    # The site's own category listing URL for this product, from
    # `pdpCategory.breadcrumbURL`. Worth a column because it is the address
    # a listing run should be pointed at to track this product's
    # competitors, and it is not derivable from anything else in the row.
    category_url: Optional[str] = None

# Row classes by --mode, so an engine maps its mode to a schema in one place.
# Both modes are Product here; the mapping exists so adding a mode later is a
# one-line change rather than a search for every place that assumed Product.
ROW_CLASS_BY_MODE = {"listing": Product, "product": Product}

# Modes whose rows are one-per-sku, and therefore safe to dedupe on `sku` and
# to hand to diff_runs.py. Both of this repo's modes qualify: a listing page
# names each product once, and a product page IS one product.
UNIQUE_BY_SKU_MODES = ("listing", "product")


def dedupe_by_key(rows: Sequence[Any], seen: Set[str], key: str = "sku") -> List[Any]:
    """Drop rows whose key already appeared earlier in this same run.

    `seen` is mutated in place, so callers thread the same set across pages —
    a stale or repeating next-page link then re-parses a page without
    duplicating its rows into the final output. On Tokopedia this DOES fire
    on healthy runs: page 1 and page 2 of one category listing shared
    exactly 3 products, all three from the "cheaper products" carousel that
    appears on every page of a listing. So a small non-zero drop count here
    is expected and a large one is not.

    A row with no key is always kept: there is nothing to check a duplicate
    against, and dropping it would be a silent data loss rather than a
    duplicate removal.

    Both of this repo's modes are one row per `sku`, so `key` is never
    overridden here — the parameter exists because the rest of the family
    shares this function and one of them needs it.
    """
    fresh = []
    for r in rows:
        val = getattr(r, key, None)
        if val is None or val not in seen:
            if val is not None:
                seen.add(val)
            fresh.append(r)
    return fresh


# Kept under its old name: the engines and smoke tests in this family all
# call it, and a listing run does dedupe by sku.
def dedupe_by_sku(rows: Sequence[Any], seen: Set[str]) -> List[Any]:
    return dedupe_by_key(rows, seen, key="sku")


# CSV cannot hold a list. Joining with " | " keeps the cell readable in a
# spreadsheet and round-trippable by splitting on the same separator; the
# JSON output keeps the real list, so nothing is lost for a consumer that
# wants structure. `repr()` of a Python list (the default if this is not
# handled) is neither readable nor parseable by anything but Python.
LIST_CSV_SEPARATOR = " | "


def _csv_value(v: Any) -> Any:
    if isinstance(v, (list, tuple)):
        return LIST_CSV_SEPARATOR.join(str(x) for x in v)
    return v


def write_json(rows: Sequence[Any], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([asdict(r) for r in rows], f, ensure_ascii=False, indent=2)


def write_csv(rows: Sequence[Any], path: str, row_cls: Type = Product) -> None:
    # An empty result still gets the header row. A zero-byte file makes a
    # consumer fail on read (no columns to parse) instead of reading a valid
    # table with zero rows — and "an empty result is still a well-formed
    # result" is the same principle as `save` refusing to overwrite good data.
    #
    # The header comes from `row_cls`, not from the first row, so an empty
    # run still writes the columns of the mode that produced it.
    fieldnames = [f.name for f in fields(row_cls)]
    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            writer.writerow({k: _csv_value(v) for k, v in asdict(r).items()})


# Exit code used when a run completes but produced nothing. Distinct from 1
# (crash) so a caller can tell "ran, found nothing" from "blew up".
EXIT_NO_PRODUCTS = 4

# Exit code for a run blocked by a bot-check/challenge page before parsing
# even started — distinct from EXIT_NO_PRODUCTS so a caller can tell "the
# search genuinely matched nothing" from "something stood between us and the
# content". See product_parser.detect_bot_challenge.
#
# On Tokopedia this code specifically does NOT cover the three ways to get a
# real page with no products on it: a `/p/<slug>` discovery hub, which
# answers 200 with banners and carousels and no grid; a search whose query
# matches nothing ("Oops, produk nggak ditemukan"); and one page past the
# end of a category listing. All three are EXIT_NO_PRODUCTS — the request
# was served exactly as asked and simply has no products on it. Reporting
# any of them as blocked would send a user hunting for a proxy problem that
# does not exist.
#
# What EXIT_BLOCKED means here is unusually literal: Tokopedia sends an
# address it has scored NOTHING at all. No status code, no interstitial, no
# vendor marker — the HTTP/2 stream is reset and the run sees a connection
# error rather than a page.
EXIT_BLOCKED = 3

# Exit code for a run that gathered SOME rows and then stopped early — a
# page-load timeout, a 503 throttle, or a challenge on page 3 of 10. The
# output file is still written (throwing away three good pages would be
# worse), but it is not a complete picture, and a consumer that cannot tell
# the difference will read the pages that were never fetched as products that
# disappeared from the catalogue. See write_run_meta.
# A REMOTE service failed — the Scraping Browser refusing the connection
# (`profile_locked` is the common one: a profile allows a single live
# connection), or the Scraper API answering an error. Distinct from 1 (a
# crash in this code) and from 2 (bad usage) because it means "try again, or
# use a different profile", not "there is a bug here". Defined once, here,
# because the browser engines and scraper_api_client.py both return it and
# two definitions of the same code is exactly how a family's exit contract
# drifts.
EXIT_API_ERROR = 5

EXIT_PARTIAL = 6


def write_run_meta(out_prefix: str, meta: dict) -> str:
    """Write a run-metadata sidecar next to the output, return its path.

    Deliberately a separate `<out>.meta.json` rather than columns on every
    row: this describes the RUN, not the product, and repeating it across
    every row would both bloat the output and change the schema every
    consumer of this project already parses.

    diff_runs.py reads it to refuse a comparison between runs that are not
    both complete, and between runs of different `mode`.
    """
    path = f"{out_prefix}.meta.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    print(f"[+] Wrote run metadata -> {path} (status={meta.get('status')})")
    return path


def run_meta(status: str, stop_reason: str, pages_requested: int,
             pages_completed: int, start_url: str, final_url: str,
             products: int, pages_failed: Optional[List[int]] = None,
             mode: str = "listing", source: str = SOURCE_DEFAULT,
             extra: Optional[dict] = None) -> dict:
    """Build the metadata dict for a finished run.

    `status` is the field a consumer branches on:
      complete — every requested page was fetched, or the site's own
                 pagination genuinely ran out (nothing more existed to get)
      partial  — rows were gathered, then the run stopped early
      failed   — nothing was gathered at all

    `mode` and `source` are recorded because `mode` is not implied by the
    repo: the same output prefix can hold a listing run or a product run,
    and those populate different columns — `sold` is a FLOOR on a listing
    row and exact on a product row, so diffing one against the other would
    report every row as changed. diff_runs.py refuses a pair whose modes or
    sources differ. `source` is `tokopedia.com` on every row of every run
    here, since the site has one storefront and one currency; it is kept
    because consumers read these columns by name across the family.

    `extra` carries facts about the run that are not about any single row.
    `--mode shop` uses it for the SELLER's own name, location, rating and
    review count: a run covers exactly one shop, so those belong to the run
    rather than repeated down a column, and the shop's review count (16679
    on the captured seller) is a different number from its listings' own
    (827 on one of them) — putting them in one column would make the schema
    lie.

    `pages_failed` lists the pages that did not yield data, by number.
    `pages_completed` alone was enough only while pages were fetched strictly
    in order, where "3 of 10 completed" could only mean 1-2-3: a count is not
    a description once pages can be fetched independently and page 3 can fail
    while 4 and 5 succeed. Recording the numbers keeps the sidecar honest
    about WHICH part of the catalogue is missing, not just how much.
    """
    meta = {
        "source": source,
        "mode": mode,
        "status": status,
        "stop_reason": stop_reason,
        "pages_requested": pages_requested,
        "pages_completed": pages_completed,
        "pages_failed": pages_failed or [],
        "products": products,
        "start_url": start_url,
        "final_url": final_url,
        "finished_at": datetime.now(timezone.utc).isoformat(),
    }
    if extra:
        # Merged rather than nested under a key, so a consumer reads
        # `shop_rating` at the top level beside `products`. Run fields win a
        # name collision: a caller cannot accidentally overwrite `status`.
        meta.update({k: v for k, v in extra.items() if k not in meta})
    return meta


def save(rows: Sequence[Any], out_prefix: str, fmt: str,
         allow_empty: bool = False, row_cls: Type = Product) -> int:
    """Write JSON/CSV and return a process exit code.

    Returns 0 when rows were written, EXIT_NO_PRODUCTS when there were none.
    Callers are expected to exit with it.

    On zero rows, nothing is written at all unless `allow_empty`. Two reasons,
    and a live run demonstrated both. A page-load timeout produced
    `Saved 0 products -> out.json` and exit 0: a two-byte `[]` that a
    consuming pipeline reads as a successful run with no stock. Worse, if the
    file already held a good result from an earlier run, that result is now
    gone — the failure destroyed the last known good data. So an empty result
    leaves the previous file intact and says why.

    `allow_empty=True` is for the legitimate case: a filter that genuinely
    matches nothing, where an empty file is the answer.
    """
    if not rows and not allow_empty:
        print(f"[!] 0 products — refusing to write {out_prefix}.json/.csv, so an "
              f"earlier good result isn't overwritten with an empty one. "
              f"Pass --allow-empty if an empty result is the expected answer.")
        return EXIT_NO_PRODUCTS

    if fmt in ("json", "both"):
        write_json(rows, f"{out_prefix}.json")
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.json")
    if fmt in ("csv", "both"):
        write_csv(rows, f"{out_prefix}.csv", row_cls=row_cls)
        print(f"[+] Saved {len(rows)} products -> {out_prefix}.csv")
    return 0 if rows else EXIT_NO_PRODUCTS


# Stop reasons that mean the run saw everything there was to see. Anything
# else ended the page loop early, so the result is only a partial view.
#
# "no_new_products" belongs here and "pagination_exhausted" is kept for the
# engines that still stop on a missing next-link: the first is a property of
# the DATA (a page contributed nothing not already seen, so the listing is
# over), while the second is a property of a CSS SELECTOR and is therefore
# the weaker signal — a renamed attribute looks identical to a short
# catalogue. On Tokopedia that ordering is not a preference, it is the only
# thing that works: the site publishes NO `link[rel=next]` and no numbered
# anchors anywhere, a CATEGORY listing is addressable by `?page=N`, and a
# SEARCH is not addressable at all — `?page=2` there returns an empty result
# set rather than page 2. So "no new products" is the one termination
# condition available on a search. See page_flow.pagination_is_addressable.
#
# "single_page_mode" is complete by construction: --mode product reads one
# page because one page is all there is.
COMPLETE_STOP_REASONS = ("completed", "pagination_exhausted", "no_new_products",
                         "single_page_mode")


def finish_run(rows: Sequence[Any], out_prefix: str, fmt: str,
               allow_empty: bool, *, blocked: bool, stop_reason: str,
               pages_requested: int, pages_completed: int,
               start_url: str, final_url: str,
               pages_failed: Optional[List[int]] = None,
               mode: str = "listing", source: str = SOURCE_DEFAULT,
               extra: Optional[dict] = None) -> int:
    """Write output + the run-metadata sidecar; return the exit code.

    Shared by all three browser engines so the status/exit-code mapping
    cannot drift between them.

    The metadata sidecar is written ONLY when the row file was written.
    Otherwise a failed run would leave a "status": "failed" sidecar next to
    the previous run's still-intact good output (which `save` deliberately
    does not overwrite) — the two files would contradict each other, and
    diff_runs.py would refuse to compare data that is in fact fine.
    """
    complete = stop_reason in COMPLETE_STOP_REASONS
    row_cls = ROW_CLASS_BY_MODE.get(mode, Product)
    rc = save(rows, out_prefix, fmt, allow_empty=allow_empty, row_cls=row_cls)
    wrote_output = bool(rows) or allow_empty

    if wrote_output:
        status = "complete" if (rows and complete) else (
            "partial" if rows else "failed")
        write_run_meta(out_prefix, run_meta(
            status=status, stop_reason=stop_reason,
            pages_requested=pages_requested, pages_completed=pages_completed,
            pages_failed=pages_failed, mode=mode, source=source,
            start_url=start_url, final_url=final_url, products=len(rows),
            extra=extra))

    if not rows:
        # Nothing gathered at all: a challenge outranks "empty result",
        # because it says something stood between the run and the content.
        return EXIT_BLOCKED if blocked else rc
    if not complete:
        print(f"[!] Partial run: stopped after {pages_completed} of "
              f"{pages_requested} page(s) ({stop_reason}). The output holds "
              f"what was gathered, but it is NOT a complete view — see "
              f"{out_prefix}.meta.json.")
        return EXIT_PARTIAL
    return rc
