# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/) as closely
as a CLI toolkit can. In practice that means: **a patch release fixes things**
— it does not promise that every flag and every default is frozen. Where a
patch changes behaviour an existing user would notice, the release notes lead
with it, so nobody discovers it from a bill or from a diff.

---

## [0.1.1] — 2026-09-10

A pre-publication audit against the family notes, section by section. Five
defects, all found by checking rather than by reading, all pinned by checks
now. Two of them made a documented feature not work at all.

> **If you copied `.env.example`, re-check what your run is actually using.**
> The placeholder check was a literal set, so it caught
> `your_2captcha_api_key_here` and MISSED the two credentialled URLs, which
> the file documents the way the vendor does — with the parts you fill in in
> braces. A copied example therefore read as CONFIGURED, and a run connected
> to `cb.2captcha.com` with the string `{login}-zone-…` as its username and
> got a 401 a long way from its cause. Any `{…}` left in a value now reads as
> unset, and `python3 env_config.py` says so by name.

> **`--fingerprint` never worked.** The engines' `--fp-tags` default was
> `Windows,Chrome,Desktop`, and the fingerprint API rejects it with HTTP 400
> — while `fingerprint_client.py`'s own `--tags` help has always said ONE
> OS-family tag, not a list. Measured against the live API: `Windows`
> succeeds; `Windows,Chrome,Desktop`, `Chrome` and `Desktop` each 400. Fixed
> and verified end to end. **This one was in all four sibling repos too**, and
> each has its own fix.

### Fixed

- A copied `.env.example` read as configured — see above.
- `--fingerprint` failed on every invocation — see above.
- **A no-results page could be reported as blocked.** `detect_page_state`
  checked the served-by-Tokopedia heuristic (two or more asset references)
  BEFORE the site's own "Oops, produk nggak ditemukan" sentence, so a minimal
  real page carrying one reference instead of the measured 3-7 came back as
  exit 3 — a proxy hunt for a correct answer. The unambiguous positive signal
  now comes first.
- **`RETRY_ON_BLOCKED` was a policy nothing enforced.** A constant with a
  paragraph of justification that no engine read: all three computed their
  budget from `BLOCK_RETRIES_WITHOUT_POOL` alone, so setting it False changed
  nothing. Now consulted by all three.
- **Half the CLI was undocumented.** Sixteen real flags appeared in no
  document — `--out`, `--delay`, `--retries`, `--proxy-file`,
  `--proxy-rotate`, `--twocaptcha-key`, `--headless`/`--headful` among them.
  The README now has every flag with its default, and the three engines' flag
  sets are pinned against the family contract and against each other in both
  directions, so a new divergence fails a check and closing a documented one
  does too.

### Removed

- `page_flow.page_bound` (never called, always returned its input) and
  `page_flow.page_number` (a wrapper that only delegated).

### Documented

- `proxy_pool.mask()` takes a bare URL, not a sentence: given one it returns
  `?://?` — the password is gone, which is the property that matters, but so
  is the host and port the log was written to show. Every call site passes the
  URL as its own argument for that reason, and the limitation is now pinned
  rather than half-guarded. `_mask_credentials()` is what handles arbitrary
  text, globally.
- Two README figures that legitimately vary between runs (`image_url` and
  `slug_id` coverage on a category run) are stated as ranges rather than as
  fractions that would go stale on the next run.

467 offline checks, all green.

---

## [0.1.0] — 2026-09-10

First release as a member of the [2scraper](https://github.com/2scraper)
family. The repository previously held an April 2026 prototype; **nothing of
its behaviour is preserved** — see "Replaced, not extended" below before
upgrading anything that consumed its output.

### What it does

Scrapes Tokopedia search grids, category listings and product pages through
Playwright, Selenium or pyppeteer, with JSON/CSV output, a run-metadata
sidecar and the family's exit-code contract.

Everything in the README is measured, dated and taken through the 2Captcha
Scraping Browser API on 2026-09-10. The headline numbers: a two-page category
run gives 119 rows at 100% price coverage; a search grid gives 155 rows at
100% price, 98% rating and 100% sold-count coverage.

### Replaced, not extended

The April prototype and this release share no data contract. If anything
consumed the old output, it needs rewriting rather than adjusting:

- **Prices are numbers.** `price` was the string `"Rp17.999.000"`; it is now
  `17999000.0`, with `currency` as the ISO code `IDR`. `discount` was
  `"18%"`; `discount_pct` is now a number computed from the two prices.
- **`sold` is parsed.** It was the site's own abbreviation `"2rb+"`; it is
  now `2000` with `sold_is_floor: true` beside it, because *rb* is *ribu* and
  the `+` means the site rounded down.
- **There are exit codes and a sidecar.** 0 ok · 1 crash · 2 bad usage ·
  3 blocked · 4 zero products · 5 remote API error · 6 partial, plus
  `<out>.meta.json` per run. A run that finds nothing now writes nothing,
  rather than replacing good output with an empty file.
- **The 2Captcha key no longer travels in a query string.** The prototype
  called the v1 `res.php?key=…` endpoints, which put the key into the text of
  every `HTTPError`. All calls now carry it in a body or a header.
- **`--mode shop` does not exist**, and neither does `--country`. Tokopedia
  is one storefront — measured identical from an Indonesian and a US exit —
  so a country flag could only disagree with the URL.
- **The prototype's 20 `/p/<slug>` "categories" were all hub pages.** They
  have no product grid on them; the real listings are one or two levels down.
  A run against a hub now reports 0 rows and exit 4 rather than returning
  recommendation-carousel filler.
- **Its field selectors no longer exist.** `linkProductName`,
  `linkProductPrice`, `linkProductShopName` and the rest: zero occurrences on
  either page kind five months later.

### Four defects the first live runs found

Every one was in new code, every one was invisible to a green offline suite,
and every one is pinned by a check now. This is §16's rule earning itself:
running the code beats reading it, and running EVERY engine beats running the
primary one.

- **The readiness wait crashed on `/search`.** Tokopedia's
  Content-Security-Policy has no `unsafe-eval`, and Playwright's
  `wait_for_function` hands the browser a string to evaluate — so the site's
  most obvious URL died with `EvalError` and exit 1. Every readiness wait now
  polls `querySelectorAll` over CDP instead.
- **Two of the three engines crashed on their FIRST fetch.**
  `page_flow.classify(html, status, url)` took `status` positionally, while
  pyppeteer and Selenium call it as `classify(html, url=…)` because neither
  exposes a response status at that point. `TypeError`, immediately — and
  invisible to import, `--help`, `compileall`, the AST undefined-name walk
  and 400+ green assertions, because none of those calls a function the way a
  live run does. `status` is now optional, and the suite binds every
  `page_flow.*` and `product_parser.*` call in every engine against the real
  signature. That check was verified by reverting the fix: it names all six
  call sites.
- **A `/p/<slug>` hub reported exit 3.** The bare string `akamai` was in the
  challenge-marker list, and Tokopedia — which is fronted by Akamai — names
  `…clientnsv4-s.akamaihd.net` in its own performance script on every page it
  serves. A marker that matches every page of the site it guards is worse
  than no marker; it is gone, Akamai's actual refusal strings stay, and the
  vendor check now only consults markers for a state the policy had already
  given up on.
- **`page` was 1 on every row of a two-page run**, which made `position`
  ambiguous: a row from page 2 claimed the same position as one from page 1.
  The page number is threaded into the parser in all three engines.

### Inherited code that could never run here, removed

- **The DataDome solver** — roughly 240 lines: the slider task, the cookie
  parsing, the mandatory-proxy fields. Tokopedia has no such page, and an
  address it has scored gets no response at all rather than an interstitial,
  so none of it could ever fire. A paid code path that looks load-bearing and
  cannot run is worse than no code. The absence is pinned by a check.
- **`price_is_from` and `price_max`** from the row schema, and from
  `diff_runs`' tracked fields. A Tokopedia tile prints one price, not a
  range. `sold` and `sold_is_floor` are tracked in their place, as a pair:
  without the flag a `sold` change is unreadable, since a tile's figure is a
  floor and a product page's is exact.

### Known limitations, measured

- **A residential exit is required.** Tokopedia answers an address it has
  scored with nothing at all: no status code, no interstitial, no challenge.
  The country does not matter.
- **`--proxy` is not usable from a browser here.** The 2Captcha residential
  gateway reaches an Indonesian exit and plain `requests` goes through it, but
  every Chromium navigation through it times out — including a control site,
  so it is the browser-plus-proxy combination rather than Tokopedia. Not yet
  understood, and therefore not recommended.
- **A search listing has no per-page addresses.** `?page=2` on a search URL
  empties the result set rather than advancing it, so `--concurrency` above 1
  is refused for one, with that reason. A category listing paginates
  normally.
- **`image_url` is populated on about 37% of listing rows.** Tokopedia
  lazy-loads tile images and this parser reports `null` rather than the
  placeholder a tile below the fold carries.
- **No challenge of any kind has been observed on this site**, so a solving
  key buys nothing here today. The path is wired up and capped at one solve
  per page.
- **The Scraper API path returns about 5 products where a browser engine
  returns 60.** Measured: 200, 416,939 bytes, $0.0005, 5 rows. Tokopedia
  hydrates its grid from client-side GraphQL only as the page is scrolled, so
  a single browserless fetch sees the first paint and nothing after it. It
  cannot read a full listing by construction.
