# Troubleshooting

Symptoms in the order you are likely to meet them. Every number was measured
on **2026-09-10** against `tokopedia.com`; where a symptom has more than one
cause, the causes are ordered by how often they were the answer.

---

## Nothing comes back at all — a timeout, or exit 3 with a 0-byte dump

**This is the normal failure on this site, and it is not a bug.**

Tokopedia does not refuse an address it has scored. It ignores it: the
connection is accepted, TLS completes, the HTTP/2 stream opens, and then it
is reset. No status code, no interstitial, no challenge, no error page.

    curl https://www.tokopedia.com/            000, stream reset (INTERNAL_ERROR)
    curl --http1.1 https://www.tokopedia.com/  000, timeout, 0 bytes
    headless Chromium via Playwright           net::ERR_HTTP2_PROTOCOL_ERROR

A real browser gets the same treatment as curl, so it is not a TLS or HTTP/2
fingerprint problem — it is the address.

**What fixes it: any residential exit. The country does not matter.** An
Indonesian exit (`country-id`) and a US one (`country-us`) both returned HTTP
200 and 95 products, with identical markup and zero price differences across
the 68 products both runs saw.

**What does NOT fix it:**

- **A 2Captcha solving key.** There is no challenge on the page, because
  there is no page. No challenge of any kind has been observed on this site.
- **`--retries`.** That budget is for transient faults. The engines have a
  separate block-retry budget, and on a scored address neither helps.
- **A datacentre proxy.** A GitHub Actions runner is one, which is why the
  canary in this repo skips rather than fails when it has no Scraping Browser
  endpoint.

**Read the byte count in the error line.** The engines write the debug dump
even when it is empty, because "0 bytes" is itself the diagnosis and a reader
who finds no file at all cannot tell that from a run that never got there.

    Tokopedia did not serve this request — 0 bytes, with no reference to the
    site's own asset host, saved to out_page1_debug.html.

A dump with bytes in it that still reports `not-served` means something
answered but it was not built out of Tokopedia's own assets — see the next
section.

---

## `--proxy` is set and every navigation times out

Known, reproducible, and not understood. The 2Captcha residential gateway
does reach an Indonesian exit — `36.77.157.93`, Bandung, Telkom Indonesia —
and plain `requests` fetches through it fine. But **every Chromium navigation
through it times out, including `shopee.co.id` as a control**, so it is the
browser-plus-proxy combination rather than anything to do with Tokopedia.

Until it is understood, use `--cdp-endpoint`. If you have a proxy Tokopedia
accepts from a browser, `--proxy` is wired and its credentials never reach
the browser's command line.

---

## `500 Internal Server Error` on connecting, exit 5

A Scraping Browser profile allows **one live connection**. Two runs against
the same `pid` give the second a 500, which the engine reports as exit 5
(remote API error) with that explanation.

Measured: two runs launched back to back against one pid — the second exited
5 even though the first had already printed its results, because the profile
had not been released yet. Leave ~60 seconds between runs on one pid, or give
each concurrent run its own.

`401 deny_no_user` is a different thing: the endpoint's login is no longer
recognised, which usually means the credential has expired. Every endpoint
inherited from this family's sibling repos was already returning it when this
repo was written.

---

## Exit 4 and zero rows — is it broken, or is the page empty?

There are **three** ways to get a page with no products on it, and none of
them is a fault.

**A `/p/<slug>` URL with one path segment is a discovery hub.** Banners,
brand strips and recommendation carousels, with no product grid on it. It
does carry ~39 product links from those carousels, so a scraper anchored on
the URL pattern alone would return 39 rows of filler and report success; this
one is scoped to the grid and returns zero. The engines warn about it by
name:

    /p/makanan-minuman is a /p/<slug> DISCOVERY HUB, not a listing…

The real listings are one or two levels down —
`/p/makanan-minuman/minuman/kopi-bubuk`. A product page's own breadcrumb
names its category's listing URL, and a `--mode product` run records it in
`category_url`.

**A search whose query matches nothing** answers 200 with the site's filter
rail, its footer and `Oops, produk nggak ditemukan` where the grid would be.

**One page past the end of a category listing** does the same.

All three are exit 4. If you expected products, check the URL kind first —
`listing_kind()` in `product_parser.py` is what the engines use, and a hub is
by far the most common mistake.

---

## The run says "shell … has not painted" and then works

That is the expected path on a search URL, not a warning to act on:

    Page 1 is a shell Tokopedia served but has not painted (610712 bytes,
    no grid) — waiting up to 45s for the grid rather than spending a retry.

A **category** listing server-renders its grid container, so it is content
from the first response. A **search** grid arrives with a client-side GraphQL
response, so at `domcontentloaded` the page is a 600 KB shell with
`divSRPLazyProductWrapper` placeholders and nothing else. Classified naively
that is "unknown", "unknown" retries, and the first live search run of this
engine fetched the page twice, scrolled not at all and reported 0 rows with
exit 4. It now waits instead.

---

## `image_url` is null on most rows

**Expected, and the alternative is worse.** Tokopedia lazy-loads tile images:
a tile that has not scrolled into view carries a placeholder — an SVG under
`/obj/tokopedia-web-sg/zeus_v2/` on a search page (55 of 95 tiles), a `data:`
URI on a category page (30 of 60). Reading `src` blindly gives a column that
is 100% populated and half wrong.

So a product image is recognised positively — the tile's own product `<img>`,
on the site's image CDN, carrying the `~tplv-` transform marker every real
product image has and no icon does — and everything else is null. Measured
36-43% populated on live runs. Scroll further and more rows fill in.

A search tile carries three to seven `<img>` elements (the product, a rating
star, a glyph, usually a shop badge, sometimes a ribbon and a video
thumbnail), so a naive "first CDN-hosted image" read returns the **shop
badge** on most rows.

---

## `slug_id` is null on about a third of rows, and `product_id` on all of them

Both are correct on a listing run.

Most product URLs end in a 19-digit tail, and **4 of 40 do not** — two carry
a short hex suffix instead, all four ordinary organic products with identical
tile markup. `slug_id` carries that tail where it exists.

**The tail is not the product id.** The id Tokopedia's own app deep links use
is `103490518624` for a product whose URL tail is `1731177319241910164`, and
only a detail page states it. `product_id` is populated by `--mode product`
and null on a listing run, along with `shop_id`, `condition`,
`weight_grams`, `stock_max`, `listed_at`, `category_url`, `review_count` and
`in_stock`.

`sku` is the URL **path** — always present, stable, what the site's own
canonical uses, and what a listing row and a detail row join on.

---

## `rating`, `sold`, `original_price` and `discount_pct` are null on every row

Check which page kind the run used. A **category** tile prints none of them —
0 of 60 on every capture — while a **search** tile prints all four. That is a
property of the page kind, not a parsing failure.

The columns stay because a search grid and `--mode product` do populate them.
The sidecar's `mode` says which run you are reading, and `diff_runs.py`
refuses to compare two runs of different modes.

---

## `sold` disagrees between two runs of the same product

Check `sold_is_floor`.

A tile prints `100rb+ terjual` — *rb* is *ribu*, a thousand, and the `+`
means the site rounded down. The same product's own page states `countSold`
207785. So a listing row's `sold` is a **lower bound** and a product row's is
exact, and the two are not comparable. That is what the flag is for, and why
`diff_runs.py` will not diff a listing run against a product run.

---

## `price_source`

| Value | Means | When it is a problem |
|---|---|---|
| `dom` | The rendered tile. **The only possible value on a listing page** — there is no structured data on one to confirm against. | Never on a listing run. On a product run it means the Apollo cache was missing. |
| `meta+apollo` | `--mode product`: the price from the page's own `product:price:amount` meta, everything else from its `window.__cache` Apollo blob. | Nothing. |
| `meta` | `--mode product` where the Apollo cache was absent. | `product_id`, `shop_id`, the exact `sold` and the review count will all be null. |

**There is no `jsonld` value, and that is measured.** A Tokopedia listing
page carries zero `application/ld+json`, zero `__NEXT_DATA__`, zero
React-flight payload and zero Apollo state, across six captures. Any
confirmation threshold copied from a sibling repo would fail every run.

---

## A discount of 90%+ — is the parser confusing the two prices?

Almost certainly not. One captured tile prices a 1 kg coffee at Rp3.653
against a Rp117.000 was-price, and the site's own badge says 97%. Sellers set
both numbers.

Two things guard against the real confusion:

- The discount is **computed** from the two prices, never read off the badge.
  Tokopedia only prints a badge above some threshold — two tiles carry a
  strike at 7% and 8% off with no badge at all — so computing recovers 88
  rows of 95 where reading recovers 86. Where both exist they agreed on all
  86.
- `discount_pct` is `None`, never zero or negative, when the figures are not
  what they were taken for. A row with an `original_price` at or below its
  `price` is what a second KIND of struck-through price would produce, and
  both the suite and the canary assert there are none.

---

## The parser used to work and now returns empty columns

**Class names are content hashes on this site.**
`<span class="+tnoqZhn89+NHUA43BpiJg==">` is the product title today and will
be something else after the next deploy. Nothing in this repo anchors on one.

This repo's own April 2026 prototype anchored on
`data-testid="linkProductName"`, `linkProductPrice`, `linkProductShopName`
and friends — **every one of those is gone**, zero occurrences on either page
kind five months later. What survived is the URL pattern, three container
`data-testid`s and the utility class `flip`.

So if a column empties, look in this order:

1. **The grid container.** `[data-testid="divSRPContentProducts"]` on a
   search page, `[data-ssr="productsCategoryL2/L3SSR"]` on a category
   listing. If this moves, the run reports 0 rows and exit 4 — loud.
2. **The tile marker.** `[data-testid="imgLeg-c"]` on a search page (one per
   tile), `[data-testid="divProductWrapper"]` inside
   `a[data-testid="lnkProductContainer"]` on a category listing.
3. **The reading ORDER inside the tile**, which is what the field reads rest
   on: badge, title, price, was-price, rating, sold, shop, location. If
   Tokopedia reorders a tile, `title` and the prices are what break.
4. **`span.flip`**, which is the shop name and the shop's city, in that
   order, exactly two per search tile.

`--dump-html` writes the snapshot the parser was given, on success too. A run
can return the right row count with a field silently unpopulated, and then
the exact bytes are the only way to tell a parsing bug from a too-early
snapshot.

---

## `--concurrency 4` is refused

On a search URL, by design:

    a Tokopedia search listing has no per-page addresses — it is one
    infinitely scrolling page, and ?page=N on a search URL returns an EMPTY
    result set rather than page N. Workers would each re-fetch the same
    page. Use a category listing URL (/p/<cat>/<sub>/<subsub>), which does
    paginate with ?page=N, or run with --concurrency 1.

A category listing accepts it. Note it is also refused with
`--cdp-endpoint`: a Scraping Browser profile allows one live connection, so
workers collide with `profile_locked`. Use several `pid`s, one run each.

---

## `pytest` or `python3 smoke_test.py` fails after an edit

The suite pins **values**, not coverage — the expected price, rating, sold
count and title for named products — because a column can be 100% populated
and entirely wrong. A failure names the product and the field.

Two checks are worth knowing about before you edit an engine:

- **Every `page_flow.*` and `product_parser.*` call in every engine is bound
  against the real signature.** This is the general form of a bug the first
  live run found: `classify(html, status, url)` took `status` positionally
  while two of the three engines called it as `classify(html, url=…)`, and
  both crashed on their first fetch — invisible to import, `--help`,
  `compileall`, the AST undefined-name walk and 400+ green checks.
- **The readiness wait must not evaluate a string.** Tokopedia's
  Content-Security-Policy has no `unsafe-eval`, so Playwright's
  `wait_for_function` raises `EvalError` on a `/search` page and takes the
  run down. Poll `querySelectorAll` over CDP instead; `page.evaluate` with a
  real function is fine.

---

## pyppeteer prints a wall of asyncio tracebacks after a successful run

Cosmetic, and after exit 0:

    Exception ignored in: <coroutine object …close_connection …>
    RuntimeError: Event loop is closed
    RuntimeWarning: coroutine 'WebSocketCommonProtocol.write_close_frame'
                    was never awaited

pyppeteer's WebSocket teardown races the interpreter shutdown. The engine
installs an asyncio exception handler that silences the ones it can reach;
these last few are emitted by the interpreter itself, after the loop is
already gone, and there is nowhere left to catch them. The run succeeded —
check the exit code and the output files, both of which are unaffected.
Swallowing them by muting the interpreter's own warnings would hide real
faults of the same shape, which is why they are documented here instead. **pyppeteer is effectively unmaintained** and
its own README points at Playwright.

---

## Selenium cannot reach the site at all

`selenium_scraper.py` cannot use an authenticated remote CDP endpoint, and
that is a limitation of chromedriver rather than of this repo: Playwright's
`connect_over_cdp` and pyppeteer's `browserWSEndpoint` take a full
`ws://user:pass@host:port` and authenticate on the WebSocket upgrade, while
chromedriver's `debuggerAddress` takes a bare `host:port` with nowhere to put
a password. Its `--proxy-server` cannot authenticate either — credentials are
stripped and a warning printed.

So on this site, where a plain address gets no response, Selenium needs a
local browser on an exit Tokopedia already accepts. It is kept for parity and
correctness, not because it is the way in.

---

## Where to look next

- `product_parser.py`'s module docstring — what is and is not readable on
  each page kind, and why the reads are text-order based.
- `page_flow.py`'s module docstring — the page-state policy, the scroll rule
  and the pagination split, all as data.
- The README's access section — every access measurement with its date.
- `CHANGELOG.md` — what changed and what it breaks.
