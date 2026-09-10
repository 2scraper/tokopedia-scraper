# Contributing

Bug reports, site-change reports and pull requests are all welcome. This file
covers the few things specific to a scraper, which are not the usual ones.

## Before you open anything

Run the offline suite. It needs no network, no browser and no API key, and takes
about a second:

```bash
pip install -r requirements.txt
python3 smoke_test.py
```

It prints its own check count, and lists any group it had to skip because an
engine library is absent.

**The suite must pass with no engine installed at all.** CI installs only
`beautifulsoup4` and `requests`, so any import of `playwright_scraper`,
`puppeteer_scraper` or `selenium_scraper` in a test has to sit inside
`try/except ImportError` with the skip recorded. This is easy to get wrong
locally, where you almost certainly have an engine installed and an unguarded
import passes.

If the suite fails on a clean clone, that is itself the bug — say so.

## Never commit a credential

`.env` is in `.gitignore`. Keep it there.

The scrapers mask `user:pass@` in their own log lines, but three things are **not**
masked: raw HTML dumps, the Scraper API's `x-debug` response header, and your
shell history. Before pasting any output into an issue or a PR, replace keys,
proxy passwords and full `ws://user:pass@host:9222` endpoints with `***`.

CI fails the build if something that looks like a credential is committed. That
check is a backstop, not a review — a leaked key has to be rotated whether or
not the check caught it.

## Reporting a site change

Etsy changing its markup is the normal way this stops working, and it
has its own issue template. The detail that saves the most time is which of
the two paths broke, because on THIS site they run in the opposite order from
the rest of the family:

1. **The DOM tiles** — `div[data-listing-id]`, which is Etsy's own attribute
   and the PRIMARY path here. Etsy's JSON-LD covers only 8 of the 64
   listings a search page renders, so a structured-data-first parser would
   silently drop most of a page.
2. **JSON-LD** — the `ItemList` on a listing page, or the `Product` on a
   detail page. Used to ENRICH and cross-check: it supplies `in_stock`, half
   the `brand` coverage, and the confirmation behind `price_source`.
3. **The `/listing/<id>` URL pattern**, which logs a warning when it runs,
   because it means no tile matched at all and every column except `url` and
   `sku` is now read out of whatever markup surrounded the link.

A third thing can break without any path failing: the **join** between the
tiles and the structured data. When it breaks, the row count and the prices
stay healthy while `in_stock` and part of `brand` quietly empty out — so
every run logs its structured-price confirmation share per page and warns
below a floor set PER PAGE KIND (search 8%, category 70%, shop 80%; the
achievable share differs by a factor of eight between them). If you are
reporting a change, that percentage and the page kind are the numbers to
include.

`--dump-html PATH` writes the exact bytes the parser was given, on success as
well as failure, and a run that finds nothing writes a dump and a screenshot
next to the output on its own.

## Before this repository goes public

One item cannot be undone later, so it belongs on a checklist rather than in
someone's head. **A commit on top cannot reach what a published tag and a
merged PR's refs already hold** — those stay attached to the PR and cannot be
deleted from it. Afterwards, only a fresh repository removes anything.

```bash
python3 .github/ci_checks.py --history-check
```

That applies the same credential rules CI enforces to **every blob that has
ever existed**, not just the working tree. It is deliberately not part of
`--all` and not run by CI: it shells out to git once per object, and a dirty
history needs a decision, not a red check on every push.

Then the rest of the presentation, in the order that matters:

1. `python3 smoke_test.py` green, and the canary dispatched at least once —
   including its SKIP branch, which is what runs when the
   `TOKOPEDIA_CDP_ENDPOINT` secret is absent. The canary has **no schedule**: a
   Scraping Browser credential on this account does not survive a day, so it
   runs on demand with a fresh secret rather than painting a badge that has
   tested nothing. Put a schedule back the day a long-lived credential
   exists.
2. The repo description, homepage and topics set (see the family notes on
   what those should say).
3. Only then the row in the org profile README — and check it with an
   ANONYMOUS request rather than your own logged-in browser. A row pointing
   at a private repo is a 404 for every visitor, which costs more trust than
   the missing row.

## Pull requests

**Add a test for the behaviour you are changing.** `smoke_test.py` is a single
file of plain functions with inline HTML/JSON fixtures — no pytest, no
conftest, no fixtures directory. Copy the nearest existing check and edit it.

Five properties in this repo exist because they were once absent and cost real
time. Tests pin all five, so a PR that breaks one will fail rather than
silently regress:

- **`rating` is the LISTING's and `shop_rating` is the SELLER's.** The stars
  printed on a tile are the shop's — every seller with more than one listing
  on a captured page showed the same rating and count on all of them, 12
  shops across two page kinds. A listing's own rating exists only on its
  detail page, where two listings of one shop report 825 and 375 reviews
  while their shop reports 16,679. Folding them into one column would make it
  mean different things in different modes.
- **`is_ad` comes from the tile link's `ls` parameter, never from the label.**
  Etsy writes "Anzeige" on a German page and "Ad from shop" on an English
  one, so a text marker catches one locale and silently lets paid placements
  through on every other. The parameter agreed with the label 280 times out of
  280 across four captures.
- **A `t=bv` DataDome page is NOT handed to the solver.** The cookie a solve
  returns for one is rejected, so paying for it spends money to learn
  nothing. Only `t=fe` is solvable. `page_flow.STATE_POLICY` holds that as
  data so the three engines cannot disagree about it.
- **A run that finds nothing writes nothing.** It must not replace a good output
  file with `[]`. `--allow-empty` is the opt-out.
- **Exit codes are a contract**, not decoration: `0` ok, `1` crash, `2` bad
  usage, `3` blocked (the 403 refusal, or a challenge), `4` zero rows —
  including a hub category, which is a correct answer — `5` remote API error,
  `6` partial. A pipeline branches on these.
- **An EMPTY page is never retried and never counted as blocked.** A hub
  category has no product grid and one page past the end of a listing has no
  products; both are correct answers to the question that was asked.
  Retrying them spends the user's budget re-confirming the same answer, and
  rotating the exit blames an address for the URL it was given.
  `page_flow.STATE_POLICY` holds that for all three engines so they cannot
  disagree about it.
- **A challenge marker is not a block when products have already rendered.**
  This one has bitten already: the Scraping Browser API's auto-solve
  extension injects `cf-turnstile` into every page it loads, and the first
  live run reported exit 3 on a 1.8 MB page holding the full catalogue.
  Extension-injected scripts are stripped before markers are looked for.
- **A sku already written by an earlier page of the same run is dropped, not
  duplicated.** Etsy's own pagination does not repeat (0 skus shared
  across three consecutive pages, measured), so this guards against a
  re-fetch rather than against the site — and a non-zero drop count in a run
  log is worth looking at rather than routine. See `dedupe_by_key` in
  `output_writer.py`.

There is also a naming check: certain phrases are banned repo-wide and the suite
fails naming them. If it trips, read the message — the phrase is wrong for a
reason, not merely unfashionable.

### Style

- **Match the file you are editing.** No formatter is enforced.
- **Comments explain *why*.** What the code does is visible; why it does it that
  way, especially where the obvious version is wrong, is not.
- **A timeout on every remote call.** Every browser library used here has needed
  an explicit timeout its own API does not provide, and each has needed its own
  route out of the runtime — reporting a timeout is not the same as exiting on
  one. If you add a call to a remote browser or API, bound it.
- **Fail loudly.** A function that returns an empty list on error, or logs
  success without checking that the thing it wanted actually happened, is the
  single most common bug class in this codebase's history. A selector that
  matches the *wrong* element is worse than one that matches nothing, because
  the second one tells you.

### If your change needs a live run

Most do not — the suite covers the parser, the writers, the captcha classifier
and the CLI contract against inline fixtures. If yours genuinely needs
etsy.com, say in the PR what you ran, against which storefront, from
which exit country, and what you got — including the price coverage and
DOM-confirmation percentages the run prints. Note that a run from a
datacentre address will be refused outright, so "it returned nothing" from a
VPS is not a finding. Product counts differ by category and by URL, so a bare
"worked for me" is not reproducible.

Do not add anything that submits the registration form. This project
deliberately never does, and a captcha token proved valid by creating a real
account is not a result worth having.

## Scope

This repo scrapes **public pages** on Etsy: category listings, search
results and product pages, exactly as an anonymous visitor is served them.
Out of scope: anything behind a login, anything that submits a form, and
anything that defeats a protection rather than passing it the way an ordinary
browser does.

## Licence

MIT. By opening a pull request you agree your contribution ships under it.
