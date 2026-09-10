# Troubleshooting

Ordered by how often each one is the answer. Every number here was measured
on **2026-09-09** against `etsy.com`; where a symptom has more than one
cause, the section says how to tell them apart rather than listing guesses.

---

## Every page comes back exit 3 (blocked)

**Read the `t` value in the log first — it says whether money can help.**
Etsy sits behind DataDome, and its refusal is a ~1.5 KB shell carrying
DataDome's own JS object:

```
var dd={'rt':'c', ..., 't':'bv', 'host':'geo.captcha-delivery.com', ...}
```

| What the log says | What it means | What fixes it |
|---|---|---|
| `t=bv` | the address or the browser is banned. 2Captcha's own docs: the cookie a solve returns is **not accepted** | a different exit, or a different `--cdp-endpoint` pid. **Not** a captcha key |
| `t=fe` | a real, solvable slider | `--twocaptcha-key`, with a proxy (the task requires one) |
| `rt='i'`, no `t` | a device check still in progress | nothing — the engines wait it out, and it becomes one of the two above or clears itself |

**A local browser on your own address will almost certainly not work.**
Measured 2026-09-09:

```
curl + browser UA, hosting ASN            403  t=bv
headless Chromium, hosting ASN            403  t=bv
real Chrome (channel="chrome"), hosting    403  t=bv
headless Chromium via residential US       403  t=bv
curl via that SAME residential US exit     403  rt=i   (an interstitial, not a ban)
Scraping Browser API, fresh profile        200  64 listings
```

The fourth and fifth lines are the point: from one address, a plain HTTP
client got an interstitial and an automated browser got a hard ban. DataDome
scores the network first and the browser second, and an automated browser
fails the second test. **So a residential proxy alone is usually not enough
here** — which is the opposite of the sibling repos in this family.

**The fix that works is `--cdp-endpoint`** pointing at a 2Captcha Scraping
Browser session. On the measured runs it needed no captcha solve at all.

**If a fresh profile still refuses you, retry before you rotate.** A `pid`
warms up: one was measured refusing the first two requests of a session and
serving the third and everything after it in full, 1.3 MB and 64 listings.
The engines carry three block-retries for exactly this, separate from
`--retries`. If three do not clear it, that `pid` is burnt — use another, and
reuse a handful rather than minting one per run (they are capped per
account).

**How to confirm it is this and not something else.** Run with
`--dump-html page.html` and look at what arrived:

| What the dump contains | What it is |
|---|---|
| ~1.5 KB, `captcha-delivery.com`, `'t':'bv'` | a hard block; change exit or pid |
| ~1.5 KB, `captcha-delivery.com`, `/interstitial/` | a check in progress; the run should have waited — if it did not, file a bug |
| A real page with listings in it | not a block at all; see the empty-column section below |
| A real page with no listings | a page that HAS none — exit 4, not 3 |

## Exit 4 (zero products) on a URL that plainly has products in a browser

Two causes, and they are easy to tell apart.

**A page that genuinely has no listings.** A taxonomy hub, a search whose
filters exclude everything, or one page past the end of a listing. All three
answer HTTP 200 with a real page and nothing on it, and the run reports exit
4 because that is the honest answer — the request was served exactly as
asked. Open the URL yourself: if you see no grid, exit 4 is correct.

**The tile anchor moved.** If the URL plainly shows a grid in your browser
and the run still reports zero, the primary anchor
(`div[data-listing-id]`) is no longer matching. Confirm with
`--dump-html page.html` and grep it:

```bash
grep -c 'data-listing-id' page.html      # 129 on a healthy search page
grep -c 'application/ld+json' page.html  # 1 on a healthy search page
```

If the first number is 0 and the second is 1, the DOM path broke and the
JSON-LD fallback is carrying the run — which on a search page means about 8
rows instead of 64. That is a site change worth an issue, with the dump
attached.

---

## The row count is right but a column is empty

Check `price_source` first. It is in every row for exactly this reason.

| Value | Meaning | If this is unexpected |
|---|---|---|
| `dom` | The rendered tile only. **The NORMAL case on this site** — Etsy publishes JSON-LD for 8 of the 64 listings a search page renders, so most rows have nothing to be confirmed against. | Nothing. 163 of 184 rows on a live search run. |
| `jsonld+dom` | The tile and Etsy's own structured data agreed. The trustworthy read. | Nothing. ~12% of a search page, ~88% of a category page. |
| `jsonld` | Structured data only; no price was found in the tile. | The tile's price node has moved. `original_price` comes from the tile ONLY, so it will be empty. |

Every run prints DOM-confirmation coverage per page and warns below 90%, so a
tile-markup change shows up in the log rather than as a quietly emptier
output.

**A LOW confirmation share is normal on a search page and NOT a fault.**
Etsy publishes JSON-LD for only a fraction of what it renders, and the
fraction depends on the page kind:

```
search page     8 of 64 listings   ->  ~12% is HEALTHY
category page  61 of 65 listings   ->  ~88%
shop front     36 of 40 listings   ->  ~92%
```

The engines log the share with the page kind beside it and warn only below a
floor set for THAT kind. A search run reporting 12% is working exactly as
designed; a *category* run reporting 12% means the join between tiles and
structured data has broken, which empties `in_stock` and part of `brand`
while the row count and the prices stay perfectly healthy.

**Columns that are empty on purpose**, so you do not go looking:

- **`rating` and `review_count` are null on EVERY listing row.** Etsy
  publishes no per-listing rating on a listing page — 0 of 105 JSON-LD
  product nodes across a search page, a category page and a shop front. The
  stars you see on a tile are the **shop's**, and they are in `shop_rating` /
  `shop_review_count`. Use `--mode product` for a listing's own rating.
- `description`, `images`, `material`, `gtin`, `free_shipping` and
  `ships_from` are null on every listing row: only a detail page publishes
  them. Use `--mode product`.
- `gtin` is null on almost every row even in product mode. Most handmade
  listings have no barcode.
- `original_price` and `discount_pct` are null unless the tile shows a
  strikethrough — 49 of 184 rows on a live run.
- `price_max` is null unless the listing has variations AND Etsy published an
  `AggregateOffer` for it. The tile prints only the low end.
- `is_ad` is null on a shop front: a seller's own catalogue carries no ads,
  and the tiles there have no `ls` parameter at all.

`--dump-html PATH` writes the exact bytes the parser was given, on success as
well as failure, which is the only way to tell a parsing bug from a snapshot
taken too early.

---

## A product shows a negative or absurd discount

It should not, and if it does, that is a bug worth reporting with the sku.

Unlike its sibling repos, Etsy renders exactly ONE kind of struck-through
price — the was-price — so there is no second node to confuse it with and no
`lowest_price_30d` column here. What can still go wrong is reading the sale
price and the strikethrough out of the wrong nodes, because **both live
inside one container** along with the discount badge:

```
"Sale-Preis 35,87 € 35,87 € 59,79 € Ursprünglicher Preis 59,79 € (40% Rabatt)"
```

Reading "the first price" out of that text is a coin flip. This parser reads
the current price from the container with the strikethrough and promotion
subtrees removed, and the was-price from the strikethrough alone — and
`discount_pct` is computed from `price` and `original_price` only. If you see
a discount at or below zero, the two got crossed somewhere.

---

## "Blocked by turnstile" over `--cdp-endpoint`, on a page that clearly loaded

Fixed here, and worth knowing if you write your own detector.

The Scraping Browser API's auto-solve extension injects its own captcha
hunters into every page it loads:

```html
<script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/content/captcha/turnstile/hunter.js"
        data-ts-input="cf-turnstile-response"></script>
```

so `cf-turnstile` appears in the markup of a perfectly good category grid.
The first live run of this scraper reported exit 3 on a 1.8 MB page holding
the full catalogue for exactly this reason. This repo now strips
`chrome-extension://` and `moz-extension://` scripts before looking for
challenge markers, and never treats a marker as blocking when products have
already rendered.

If you are seeing this from another tool, that is where to look.

---

## HTTP 500 from the Scraping Browser endpoint

A profile (`pid-`) allows **one live connection at a time**. A 500 usually
means another run still holds it. Wait for that run to finish, or use a
different `pid` in the endpoint URL.

This is also why `--concurrency` is refused with `--cdp-endpoint`: N workers
would collide on one profile. Several `pid`s, one run each, is the way.

---

## Selenium: `--cdp-endpoint` or `--proxy` does not work

Both are real limits of the driver, not of this code, and both are refused or
warned about rather than silently failing:

- **An authenticated CDP endpoint is impossible.** Playwright's
  `connect_over_cdp` and pyppeteer's `browserWSEndpoint` take a full
  `ws://user:pass@host:port` and authenticate on the WebSocket upgrade.
  chromedriver's `debuggerAddress` takes a bare `host:port` with nowhere to
  put a password.
- **An authenticated proxy is impossible.** `--proxy-server=` accepts no
  credentials and there is no equivalent of pyppeteer's `page.authenticate`.
  This repo strips the credentials and warns. On Etsy that means the
  proxy will not authenticate and every page will be a 403 — so use the
  Playwright or pyppeteer engine when your exits need a password.

---

## A run stopped early and reported `partial` (exit 6)

The sidecar says which pages failed, by number:

```json
{"status": "partial", "stop_reason": "blocked_datadome-bv",
 "pages_requested": 20, "pages_completed": 7, "pages_failed": [8]}
```

`pages_completed` alone is not enough once pages can be fetched
concurrently — page 8 can fail while 9 and 10 succeed — which is why the list
is there. `diff_runs.py` refuses to compare a partial run against anything,
because its un-fetched pages would read as delisted products.

If `stop_reason` is a block partway through a long run, you are probably
burning one address too fast. Spread it: `--proxy-file` with more exits, or a
larger `--delay`.

---

## pyppeteer prints a traceback AFTER a successful run

Looks like this, after the output has already been written:

```
[+] Saved 74 products -> shop.json
[+] Wrote run metadata -> shop.meta.json (status=complete)
Exception ignored in: <coroutine object Connection._recv_loop at 0x...>
...
RuntimeError: Event loop is closed
```

**The run succeeded.** Check the exit code — it is 0 — and check the output
files, which are already on disk. This is pyppeteer's websocket coroutine
being collected at interpreter shutdown, printed by CPython's own garbage
collector rather than by anything in this repo.

Everything that CAN be suppressed is: the engine cancels pyppeteer's pending
tasks before stopping its loop, and its loop exception handler swallows the
teardown messages (`Target closed`, `Connection closed`, `Task was destroyed
but it is pending`, `No session with given id`). After those, ERROR-level
output on a successful run is zero. What is left arrives after the loop is
gone and after the exit code is decided, so nothing in the process is still
listening — suppressing it would mean installing a global unraisable-exception
hook, which would also swallow real bugs. That trade is worse than the noise.

`playwright_scraper.py` does not do this, and it is the recommended engine on
this site anyway.

---

## `pip check` complains after installing two engines

Expected. playwright and pyppeteer pin incompatible `pyee` versions, and
pyppeteer and selenium collide on `urllib3`. They do run side by side in
practice because neither library touches the incompatible part, but pip may
resolve the conflict by downgrading something you wanted. Use a virtualenv
per engine.

---

## Something else

`python3 smoke_test.py` runs 339 checks with no network, no browser and no
credentials. If it passes and a live run still misbehaves, the problem is in
the fetch rather than the parse — which narrows it to the exit address, the
engine, or the URL. If it fails, the message names the check.

Open an issue with: the exact command (with credentials removed), the log,
the `.meta.json` sidecar, and — if you can — the `--dump-html` output.
