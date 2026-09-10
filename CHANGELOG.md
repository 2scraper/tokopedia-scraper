# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/) as closely
as a CLI toolkit can. In practice that means: **a patch release fixes things**
— it does not promise that every flag and every default is frozen. Where a
patch changes behaviour an existing user would notice, the release notes lead
with it, so nobody discovers it from a bill or from a diff.

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

### Two defects the first live runs found

Both were in new code, both invisible to the offline suite, and both are
pinned by checks now.

- **The readiness wait crashed on `/search`.** Tokopedia's
  Content-Security-Policy has no `unsafe-eval`, and Playwright's
  `wait_for_function` hands the browser a string to evaluate — so the site's
  most obvious URL died with `EvalError` and exit 1. Every readiness wait now
  polls `querySelectorAll` over CDP instead.
- **A `/p/<slug>` hub reported exit 3.** The bare string `akamai` was in the
  challenge-marker list, and Tokopedia — which is fronted by Akamai — names
  `…clientnsv4-s.akamaihd.net` in its own performance script on every page it
  serves. A marker that matches every page of the site it guards is worse
  than no marker; it is gone, Akamai's actual refusal strings stay, and the
  vendor check now only refines the reason for a state the policy had already
  given up on.

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
