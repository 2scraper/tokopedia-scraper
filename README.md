# tokopedia-scraper

Tokopedia product scraper — search grids, category listings and product pages
— with three interchangeable browser engines, JSON/CSV output and a
run-metadata sidecar.

[![release](https://img.shields.io/github/v/release/2scraper/tokopedia-scraper?sort=semver)](https://github.com/2scraper/tokopedia-scraper/releases)
[![tests](https://github.com/2scraper/tokopedia-scraper/actions/workflows/tests.yml/badge.svg)](https://github.com/2scraper/tokopedia-scraper/actions/workflows/tests.yml)
[![canary (on demand)](https://github.com/2scraper/tokopedia-scraper/actions/workflows/canary.yml/badge.svg)](https://github.com/2scraper/tokopedia-scraper/actions/workflows/canary.yml)
![Python](https://img.shields.io/badge/python-3.9%2B-blue?logo=python&logoColor=white)
![licence](https://img.shields.io/badge/licence-MIT-green)
![engines](https://img.shields.io/badge/engines-Playwright%20%7C%20Selenium%20%7C%20Puppeteer-blueviolet)
![access](https://img.shields.io/badge/needs-a%20residential%20exit-orange)

---

## Read this before anything else: Tokopedia answers nothing at all

Every number and date below is measured. Where something is a guess it says
so and carries no number.

**Tokopedia does not refuse a bad address. It ignores it.** Measured
2026-09-10 from a residential German address: the connection is accepted,
TLS completes, the HTTP/2 stream opens, and then it is reset. No status code,
no interstitial, no challenge, no error page — nothing.

    curl https://www.tokopedia.com/            000, HTTP/2 stream reset (INTERNAL_ERROR)
    curl --http1.1 https://www.tokopedia.com/  000, timeout, 0 bytes
    curl http://www.tokopedia.com/             000, timeout, 0 bytes
    headless Chromium via Playwright           net::ERR_HTTP2_PROTOCOL_ERROR

A real browser gets exactly the same treatment as curl, so it is not a TLS or
HTTP/2 fingerprint problem — it is the address. For contrast, from the same
machine in the same minute: `www.etsy.com` 403, `www.mediamarkt.de` 403,
`www.blibli.com` 403 (an Indonesian competitor), `shopee.co.id` **200**.
A 403 is a refusal the site took the trouble to send; Tokopedia sends nothing.
The CDN is Akamai (`www.tokopedia.com` → `…edgesuite.net` → `…akamai.net`).

**What clears it: any residential exit. The country does not matter.**

|  | `country-id` | `country-us` |
|---|---|---|
| exit | 182.8.226.144, Yogyakarta, Telkomsel | 24.14.32.4, Aurora IL, Comcast |
| status | 200 | 200 |
| products in the grid | 95 | 95 |
| `<html lang>` | `id` | `id` |
| currency on tiles | IDR (`Rp`) | IDR (`Rp`) |
| price differences across the 68 products both runs saw | — | **0** |

So **there is no geo-redirect and no per-country storefront**: one site, one
language, one currency, whatever exit you arrive from. That is why there is no
`--country` flag — it could only disagree with the URL.

### What this means for your run

* **A 2Captcha solving key does nothing about a block here.** There is no
  challenge on the page, because there is no page. No challenge of any kind
  has been observed on this site. The key is still what `--fingerprint` uses.
* **`--cdp-endpoint` (the Scraping Browser API) is the path that works.**
  Every measurement in this README was taken through it.
* **`--proxy` is not currently usable from a browser, and that is unexplained
  rather than excused.** The 2Captcha residential gateway does reach an
  Indonesian exit (`36.77.157.93`, Bandung, Telkom Indonesia) and plain
  `requests` goes through it — but every Chromium navigation through it times
  out, including `shopee.co.id` as a control. So it is the
  browser-plus-proxy combination and not Tokopedia. Until it is understood,
  this README does not recommend it.
* **The remote session drops sometimes.** Three of six captures had their
  target closed mid-scroll and succeeded on the next attempt. The engines
  treat that as a transient fault, not a block.
* **A Scraping Browser profile allows ONE live connection.** Two runs against
  the same `pid` give the second a 500 (`profile_locked`, exit 5). Reuse pids
  across runs, not within them.

---

## What a healthy run looks like

Measured 2026-09-10, `country-id`, one profile:

    python playwright_scraper.py \
        --url "https://www.tokopedia.com/p/makanan-minuman/minuman/kopi-bubuk" \
        --pages 2

    2 pages, 119 rows after dedupe, exit 0
    price       119/119   (100%)
    title       119/119
    brand       119/119
    currency    119/119   all IDR
    image_url    46-49/119  (~40%)  <- correct, see below
    slug_id      76-78/119  (~65%)  <- correct, see below
    scroll settled on both pages after 5 rounds, 60 cards each

Those last two are ranges rather than exact fractions on purpose: both vary
between runs of the same command. `image_url` depends on how far the lazy
loader got, and `slug_id` on which products the listing happened to return —
so a fixed fraction here would be a number that goes stale on the next run,
and the repo's own numbers should not do that.

And a search grid, same day, same profile:

    python playwright_scraper.py --url "https://www.tokopedia.com/search?st=product&q=kopi"

    155 rows, exit 0, status complete
    price          155/155  (100%)   title  155/155   brand 155/155
    currency       155/155  all IDR  sold   155/155   shop_location 155/155
    rating         153/155  (98%)    <- two products have no reviews yet
    original_price 119/155  (76%)    <- the rest are not discounted
    image_url       57/155  (36%)    <- correct, see below
    slug_id        113/155  (72%)    <- correct, see below
    prices ranged Rp500 to Rp626.000; 0 rows had a was-price at or below
    their price
    the scroll settled after 6 rounds: 15 -> 35 -> 155 cards, 12283px

A search page holds more than a category page because it keeps hydrating
while you scroll: 60 at a time, and the run stops when a round adds nothing
new. Three of those numbers look like defects and are not.

### `image_url` on ~40% of rows is CORRECT

Tokopedia lazy-loads tile images. A tile that has not scrolled into view
carries a **placeholder** in `src` — 55 of 95 search tiles held an SVG under
`/obj/tokopedia-web-sg/zeus_v2/`, and 30 of 60 category tiles held a `data:`
URI. Reading `src` blindly gives a column that is 100% populated and half
wrong, which is the worst of the three options.

So a product image is recognised **positively** — the tile's own product
`<img>`, on the site's image CDN, with the `~tplv-` transform marker every
real product image has and no icon does — and everything else is `null`.
Scroll further and more rows fill in. A search tile carries three to seven
`<img>` elements (the product, a rating star, a glyph, usually a shop badge,
sometimes a ribbon and a video thumbnail), so taking the first CDN-hosted one
returns the **shop badge** for most rows.

### `slug_id` on ~66% of rows is CORRECT, and it is not the product id

Most product URLs end in a 19-digit tail:

    /zayn-snack-448/kopi-hitam-bubuk-robusta-…-kopi-susu-1731177319241910164

It is tempting to make that the `sku`. **It is not the product id and it is
not always there.** The id Tokopedia's own app deep links use is
`103490518624` for that same product — a completely different number, which
only a detail page states. And 4 of 40 listing URLs carry no tail at all (two
carry a short hex suffix instead), all four ordinary organic products with
byte-identical tile markup.

So `sku` is the **URL path** — always present, stable, what the site's own
canonical uses, and what a listing row and a detail row join on. `slug_id`
carries the tail where there is one, named for what it is; `product_id`
carries the real id, in `--mode product`.

---

## Install

One engine, not three. Playwright and pyppeteer declare mutually
unsatisfiable pins (`pyee` <12 vs ≥13), and pyppeteer and selenium collide on
`urllib3` (<2.0 vs ≥2.6). All of them do run side by side in practice because
neither library touches the incompatible part, but `pip check` reports the
conflict and pip may resolve it by downgrading something you wanted.

```bash
pip install -r requirements.txt -r requirements-playwright.txt
playwright install chromium        # not needed with --cdp-endpoint
```

Swap in `requirements-selenium.txt` or `requirements-puppeteer.txt` for the
other engines, in a virtualenv of their own.

---

## Credentials go in `.env`, never on the command line

A secret in `argv` is readable by anything that can run `ps` and lands in
your shell history, so none of these has a flag you are expected to type.

```bash
cp .env.example .env      # then fill in what you use
python3 env_config.py     # prints what was picked up, WITHOUT any secret
```

Precedence, highest first: **explicit flag → exported environment variable →
`.env` → default.** A `.env` never overrides something you typed, and an
already-exported variable (a CI secret, direnv, your shell profile) is never
clobbered by a file you forgot to delete.

`.env.example` documents exactly the variables the code reads —
`smoke_test.py` asserts the two sets are equal in both directions.

---

## Run

```bash
# a category listing, three pages, JSON and CSV
python playwright_scraper.py \
    --url "https://www.tokopedia.com/p/makanan-minuman/minuman/kopi-bubuk" \
    --pages 3 --format both

# a search grid (one infinitely scrolling page — see Pagination)
python playwright_scraper.py \
    --url "https://www.tokopedia.com/search?st=product&q=kopi"

# one product, with the site's real id, the exact sold count and the rest
python playwright_scraper.py --mode product \
    --url "https://www.tokopedia.com/zayn-snack-448/kopi-hitam-bubuk-robusta-original-berat-1-kg-coffee-kualitas-premium-pahitnya-pas-cocok-untuk-kopi-susu-1731177319241910164"
```

### Every flag

Defaults are `playwright_scraper.py`'s. The three engines agree on
everything in the family's flag contract — and on exit codes, run status and
whether a run spends money — but they are not flag-identical, and the
differences are real rather than oversights:

| | difference |
|---|---|
| pyppeteer | no `--fingerprint` / `--fp-country` / `--fp-tags` / `--locale`; adds `--chromium-path`. `--concurrency` is accepted for parity and ignored. |
| Selenium | no `--locale`; adds `--no-sandbox` and `--disable-dev-shm-usage`. `--concurrency` is accepted for parity and ignored. `--cdp-endpoint` and an authenticated `--proxy` do not work at all — see below. |

| Flag | Default | What it does |
|---|---|---|
| `--url` | — | The page to read. Required unless `TOKOPEDIA_URL` is set. |
| `--mode` | `listing` | `listing` or `product`. No shop mode — see Modes. |
| `--category` | from the URL | Label for the `category` column. |
| `--pages` | `1` | Listing pages. On a SEARCH url this scrolls further rather than fetching more URLs, because a search has no per-page addresses. |
| `--delay` | `2.0` | Seconds between pages. |
| `--concurrency` | `1` | Parallel workers, each with its own browser and its own proxy exit. **Refused above 1 for a search URL**, with the reason. |
| `--retries` | `3` | Attempts per page LOAD. An empty page is never retried — it is a correct answer. |
| `--retry-delay` | `2.0` | Seconds before the first retry, doubling after. |
| `--format` | `both` | `json`, `csv` or `both`. |
| `--out` | `tokopedia_products` | Output prefix: `<out>.json`, `<out>.csv`, `<out>.meta.json`. |
| `--locale` | `id-ID` | What the browser claims. Does **not** change the language or the currency — this site serves `lang="id"` and IDR to everyone. |
| `--proxy` | — | One proxy URL. Credentials never reach the browser's command line. See the access section before relying on it here. |
| `--proxy-file` | — | A pool, one URL per line. What actually spreads a run's volume. |
| `--proxy-rotate` | `per-run` | `per-run`, `per-page` or `on-block`. |
| `--proxy-shuffle` | off | Randomise the pool order at start-up. |
| `--proxy-block-retries` | `2` | Exits to try when a page comes back blocked. Without a pool the engine re-fetches once instead. |
| `--twocaptcha-key` | — | Prefer `TWOCAPTCHA_KEY` in `.env`; a key in `argv` is readable by `ps`. |
| `--captcha-api` | `v2` | `v2` (createTask) or `v1` (the legacy in.php/res.php pair). |
| `--solve-captcha` | `when-blocked` | `when-blocked` counts product links before paying; `always` solves on any detection. Neither helps with a refusal here, and no challenge has ever been observed on this site. |
| `--min-score` | `0.7` | reCAPTCHA v3 score to request (`0.3`, `0.7` or `0.9` — the API takes only these). |
| `--cdp-endpoint` | — | Connect to a running browser, e.g. the Scraping Browser API. **The path that works on this site.** Prefer `TOKOPEDIA_CDP_ENDPOINT`. |
| `--fingerprint` | off | Fetch and apply a 2Captcha fingerprint. Ignored with `--cdp-endpoint`: the remote browser brings its own. |
| `--fp-country` / `--fp-tags` | — | Narrow which fingerprint. `--fp-tags` takes ONE OS-family tag, not a list. |
| `--allow-empty` | off | Write output files even when 0 rows were found. Off by default so a bad run cannot replace last night's good data. |
| `--dump-html` | — | Save the snapshot the parser was given — **on success too**, because a run can return the right count with a field silently unpopulated. |
| `--headless` / `--headful` | headless | Ignored with `--cdp-endpoint`. |

Deliberately absent: **`--country`** (one storefront, so it could only
disagree with the URL) and **`--mode shop`** (unmeasured markup).

### Which engine can actually reach Tokopedia

| | Playwright | pyppeteer | Selenium |
|---|---|---|---|
| local Chromium | yes | yes | yes |
| `--cdp-endpoint` with credentials | **yes** | **yes** | **no** |
| `--proxy` with credentials | yes¹ | yes¹ | **no** |
| `--concurrency` above 1 | yes | no² | no² |

1. See the access section — a browser through the 2Captcha residential
   gateway did not work in testing, for reasons not yet understood. The flag
   is wired and the credentials never reach the browser's command line.
2. Accepted for flag parity and ignored; parallel page fetching lives in
   `playwright_scraper.py`.

**Selenium cannot use an authenticated remote CDP endpoint**, and that is not
a bug in this repo. Playwright's `connect_over_cdp` and pyppeteer's
`browserWSEndpoint` take a full `ws://user:pass@host:port` and authenticate
on the WebSocket upgrade; chromedriver's `debuggerAddress` takes a bare
`host:port` with nowhere to put a password. **Its `--proxy-server` cannot
authenticate either** — credentials are stripped and a warning is printed
rather than letting you believe a `user:pass` URL is doing something.

**pyppeteer is effectively unmaintained** and its own README points at
Playwright.

### Modes

    --mode listing   (default)  a search grid or a category listing
    --mode product              one /{shop}/{slug} page

There is deliberately **no shop mode**. A Tokopedia shop front is a different
application shell whose markup has not been measured here, and a mode that
ships untested is worse than one that is absent.

---

## Pagination: two page kinds, two conventions, and one of them has none

This is the part most likely to be "fixed" wrongly.

| | search | category listing |
|---|---|---|
| URL | `/search?st=product&q=…` | `/p/<cat>/<sub>[/<subsub>]` |
| pagination | **infinite scroll only** | **`?page=N` works** |
| `--concurrency` | refused, with the reason | allowed |

`?page=2` on a **category** listing returns page 2: 66 products on page 1, 61
on page 2, overlapping by 3 — and those three are the "cheaper products"
carousel that appears on every page, which is why the parser is scoped to the
grid rather than to the document.

`?page=2` on a **search** URL does not paginate. It **empties the result
set**: HTTP 200, `Oops, produk nggak ditemukan`, no grid, no prices. A
scraper that built `?page=N` unconditionally would fetch that, find no new
`sku`, conclude the listing was exhausted, and report a **complete** run
holding page 1. A fully scrolled search page also publishes no `page=` href,
no `link[rel=next]` and no next button — every button on it is a filter or a
sort.

So a search listing terminates on **data**: a scroll round that adds no new
product path ends it. And `--concurrency` above 1 is refused for a search
URL, with that reason, because page 5 of an infinitely scrolling listing has
no address — workers would each re-fetch page 1 from a different exit.

**The site publishes no result total.** Its search header looks like it does:

    "Menampilkan 1 - 60 barang dari total  untuk "kopi""
    ("Showing 1 - 60 items of total ___ for "kopi"")

and the total is empty on every capture. The range it does print is not a
count of loaded rows either — after one scroll it claimed 61-180 while the
DOM held 95 tiles. It is recorded verbatim in the sidecar and trusted for
nothing.

---

## Output

`<out>.json`, `<out>.csv` and `<out>.meta.json`. The first sixteen columns
are the 2scraper family's, in the family's order, so a consumer written
against a sibling repo reads them unchanged:

    source scraped_at url sku title brand price currency original_price
    discount_pct rating review_count in_stock image_url category price_source

then Tokopedia's own:

    page position shop_slug shop_location sold sold_is_floor slug_id
    product_id shop_id condition weight_grams stock_max listed_at category_url

`sample_output.json` and `sample_output.csv` are cut from a real run.

**`sold` is a floor on a listing row and exact on a product row**, and
`sold_is_floor` is what says which. A tile prints `100rb+ terjual` — *rb* is
*ribu*, a thousand — for a product whose own page states `countSold` 207785.
Without that flag one column would silently mean two things, and a diff
between a listing run and a product run would report every row as changed.

**A run that finds nothing writes nothing.** Last night's good output is not
replaced with `[]`; `--allow-empty` is the opt-out. A failed run writes no
sidecar either, because a `"failed"` sidecar beside good data would
contradict it. An empty CSV still carries its header.

### Exit codes

    0  ok            3  blocked                5  remote API error
    1  crash         4  zero products          6  partial
    2  bad usage

**Blocked ≠ empty ≠ partial**, and on this site there are three distinct ways
to get a legitimately empty page: a `/p/<slug>` discovery hub, a search whose
query matches nothing, and one page past the end of a category listing. All
three are exit 4. Exit 3 means the site sent nothing at all.

---

## Traps that look like bugs

**A `/p/<slug>` URL returns zero products, and that is correct.** With ONE
path segment it is a *discovery hub* — banners, brand strips and
recommendation carousels, with no product grid on it. It does carry ~39
product links from those carousels, so a scraper anchored on the URL pattern
alone would return 39 rows of filler and report success. Scoped to the grid
it returns zero, which is exit 4. The real listings are one or two levels
down: `/p/makanan-minuman/minuman/kopi-bubuk`. A product page's own
breadcrumb names its category's listing URL, and the run records it in
`category_url`.

**A category listing has no rating, no sold count and no was-price on any
row.** 0 of 60 tiles carry any of them — that page kind does not print them.
The columns stay because `--mode product` and a search grid do populate them;
the sidecar's `mode` is what tells a consumer which run they are reading, and
`diff_runs.py` refuses to compare two runs of different modes.

**Discounts of 90%+ are real, and they are the seller's own arithmetic.** One
captured tile prices a 1 kg coffee at Rp3.653 against a Rp117.000 was-price —
the site's own badge says 97%. The discount is computed from the two prices,
never read off the badge, because Tokopedia only prints the badge above some
threshold: two tiles carry a strike at 7% and 8% off with no badge at all, so
computing recovers 88 rows of 95 where reading recovers 86. Where both exist
they agreed on all 86.

**A product with no reviews shows no rating**, and then its sold count stands
alone in the tile text (`… Rp30.000 5 terjual …`). One such tile in 190. The
parser will not read that 5 as a five-star rating.

**There is no structured data on a listing page.** Zero
`application/ld+json`, zero `__NEXT_DATA__`, zero React-flight payload, zero
Apollo state, across six captures. The grid is fetched by client-side GraphQL
and exists only in the rendered DOM, so `price_source` is `dom` on every
listing row and there is nothing to cross-check a price against. A **detail**
page is different: it carries a `window.__cache` Apollo blob, and that is
where `--mode product` gets the real product id, the exact sold count, the
review count, the condition, the weight and the category's own URL.

**Class names are content hashes**
(`<span class="+tnoqZhn89+NHUA43BpiJg==">` is the product title) and they
change on every deploy. This repo's own April 2026 prototype anchored on
`data-testid="linkProductName"`, `linkProductPrice`, `linkProductShopName`
and friends — **every one of those is gone**, zero occurrences on either page
kind five months later. What survived is the URL pattern, three container
`data-testid`s and the utility class `flip`. So the reads are text-order based
inside a structurally scoped tile; see the top of `product_parser.py`.

**The canonical URL carries UTM parameters.** A detail page's own
`<link rel="canonical">` arrives with `?utm_source=google&utm_medium=organic
&utm_campaign=pdp`, and a listing anchor arrives with an `extParam` carrying
the search id. Both are stripped before a URL becomes a row's `url`, or the
join between a listing row and a detail row fails on every row.

**The site's Content-Security-Policy has no `unsafe-eval`.** This is a trap
for anyone extending the engines: Playwright's `wait_for_function` hands the
browser a *string* to evaluate, and on a `/search` page that raises
`EvalError` and takes the run down. Every readiness wait here polls
`querySelectorAll` over CDP instead. `page.evaluate` with a real function is
fine.

---

## What the paid products buy here

Four separately-billed 2Captcha products sit behind one key.

**The Scraping Browser API** is what makes this work at all, given that a
plain address gets no response. `--cdp-endpoint` takes:

    ws://{login}-zone-scraping_browser-country-{cc}-pid-{profileId}:{password}@cb.2captcha.com:9222

`country-` picks the exit country — which on this site affects **getting in**
and nothing about what comes back. `pid-` is a profile with persistent
cookies, **one live connection each** and capped per account: reuse pids
rather than minting one per run, and expect a 500 (`profile_locked`, exit 5)
while another run still holds one.

**Proxies** would be the cheaper path and are not currently usable from a
browser here — see the access section.

**Captcha solving** buys nothing on this site today. No challenge of any kind
has been observed, and a refusal is not a page. The path is wired up because
a bot manager can be switched on between deploys, capped at one solve per
page so a speculative path cannot become a bill, and `--solve-captcha
when-blocked` (the default) counts product links before spending anything.

**Fingerprints** (`--fingerprint`) use the same key on a separate
subscription. Ignored with `--cdp-endpoint`: the remote browser brings its
own, and stacking a second creates a contradiction rather than better cover.

---

## Comparing two runs

```bash
python diff_runs.py old.json new.json --fail-on-change
```

Diffs by `sku` and refuses to compare runs that are not both `complete` — a
partial run's unfetched pages would otherwise read as delisted products. It
also refuses a pair whose **modes** differ, because `sold` means a floor on
one side and an exact figure on the other. A price difference that comes with
a `price_source` difference is reported as `source_changed` rather than
`changed`, and `--fail-on-change` ignores it: it says something about our own
two snapshots, not about Tokopedia.

---

## Tests

```bash
python3 smoke_test.py     # 400+ offline checks, no network, no browser
pytest                    # the same checks, wrapped as one test
```

It passes with no engine library installed at all; the skips are reported and
CI fails on an unexpected one. Fixtures are cut from the real captures and
verified to parse identically to the untrimmed original, and the checks pin
**values** — the expected price, rating, sold count and title for named
products — because a column can be 100% populated and entirely wrong.

The canary (`canary.yml`) is `workflow_dispatch` only. It needs a Scraping
Browser endpoint, and the credential in one does not survive a day, so a
nightly run would go red every night once it expired — and a check that is
always red teaches everyone to ignore checks. When the secret is absent it
**skips** with a notice rather than failing.

---

## Not supported

* Shop fronts (`--mode shop`) — see Modes.
* Reviews. A product's review list is fetched by a separate GraphQL call and
  has not been measured here.
* Tokopedia's GraphQL API. This drives a browser; `gql.tokopedia.com` is
  refused with that reason.
* Anything behind a login: the seller dashboard, cart, checkout.
* Sponsored tiles are not labelled in the output. Zero `topads` markers
  appeared across all six captures and every anchor carried `src=search`, so
  there is nothing measured to label — that is absence of evidence, not
  evidence of absence, and a grid that has them should be captured before a
  column is added.

## Licence and scope

MIT. Open-source, no account required to read the code or run the tests.
Point it at public listing pages, respect the site's terms, and keep your
request rate reasonable.
