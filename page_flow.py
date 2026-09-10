"""
page_flow.py
------------
Tokopedia's page-state, lazy-load and pagination policy, shared by all three
engines.

Why this module exists, when part of this family keeps each engine
self-contained: Tokopedia answers a listing request in four ways and three of
them want a different response.

    content     the grid painted — parse it
    empty       a real page with no products on it, and there are THREE
                distinct ways to get one: an exhausted `?page=N`, a search
                whose query matches nothing ("Oops, produk nggak
                ditemukan"), and a `/p/<slug>` discovery hub that has no grid
                on it at all. None of the three is a fault, none should be
                retried, and none should send anyone looking for a proxy
                problem.
    blocked     nothing arrived. On this site that is not a page — see
                "There is no block page" below.
    unknown     served by Tokopedia, no grid, no no-results message. Almost
                always "not painted yet", which is why it is the one state
                that retries.

Three copies of that triage across three engines would drift, and the drift
would be silent — one engine reporting exit 3 where its twin reports exit 4
on the same URL. The family already shares `output_writer.finish_run()` for
exactly this reason; this is the same argument applied to the decisions that
come before it.

There is no block page
----------------------
Every other site in this family answers a request it dislikes with
*something*: an Akamai reference, a DataDome shell, a branded 403. Tokopedia
accepts the connection, completes TLS, opens the HTTP/2 stream and resets it.
curl reports exit 92, Chromium reports `ERR_HTTP2_PROTOCOL_ERROR`, and there
is no markup at all.

So there is nothing to solve and nothing to detect in a page's content, and
two things follow. Detection is INVERTED — a served page is recognised by the
site's own asset host and its absence is the signal
(`product_parser.served_by_tokopedia`). And a bare navigation failure has to
be reported as `blocked` rather than crashing, because on this site that IS
what a block looks like. `RETRY_ON_BLOCKED` is nonetheless true: a Scraping
Browser profile was measured refusing early requests of a session and serving
later ones, and rotating to a different exit is exactly the response a
scored address deserves.

This module DOES contain a scroll loop
--------------------------------------
The opposite of one sibling repo, which deliberately has none. Tokopedia
paints nothing on the first response — measured `links=0` at round 0 on every
capture — and then hydrates 60 tiles at a time from client-side GraphQL. So
the scroll is not an optimisation, it is the only way any product is ever
seen.

The functions here are either pure or driven through small callables, so each
engine passes its own driver's primitives and keeps its browser plumbing to
itself:

    count(selector) -> int          how many elements match
    content() -> Optional[str]      current HTML, None if unavailable
    current_url() -> str            the URL the browser is on
    sleep(ms) -> None               the driver's own wait
    page_height() -> Optional[int]  document.body.scrollHeight
    scroll_to_bottom() -> None      scroll to that height

Deliberately no `evaluate(js)`: passing JavaScript from here would decide its
dialect for every driver, and they disagree — Playwright and pyppeteer take
`() => expr` while Selenium's `execute_script` takes a function body with an
explicit `return`. So the OPERATION is named and each engine spells it in its
own dialect.

Every value here is measured, the numbers are in the comments, and the
measurements are dated because Tokopedia's markup moves: the April 2026
prototype's field selectors were all gone by September.
"""

import logging
from typing import Callable, List, Optional
from urllib.parse import urlsplit

from product_parser import (detect_page_state, listing_kind, page_url,
                            page_number_from_url, paginates_by_url,
                            served_by_tokopedia, strip_tracking)

logger = logging.getLogger("page_flow")


# ---------------------------------------------------------------------------
# Readiness
# ---------------------------------------------------------------------------
# What "the page is ready" means, per mode. Both are containers rather than
# fields: the fields are wrapped in build-hash classes and the containers are
# the site's own `data-testid` / `data-ssr` markers, which have outlived them.
READY_SELECTOR_LISTING = (
    '[data-testid="divSRPContentProducts"] a[href], '
    '[data-ssr="productsCategoryL2/L3SSR"] a[data-testid="lnkProductContainer"]'
)
# A detail page's product name, which the site labels. Not the price: the
# price is a meta tag, present before anything paints, so waiting on it would
# resolve on a page whose body is still a shell.
READY_SELECTOR_PRODUCT = '[data-testid="lblPDPProductNameJumper"], h1'

# How many matches mean "the grid rendered". Must be > 1: waiting for a
# single match resolves on an unrelated link long before the grid paints
# (§5). Tokopedia hydrates in batches of 60, so 4 is reached the moment the
# first batch lands and is nowhere near the batch boundary.
MIN_CARD_MATCHES = 4
MIN_CARD_MATCHES_PRODUCT = 1

# Tokopedia's first batch arrived between 5 and 13 seconds after
# domcontentloaded across six captures, over a remote browser on a
# residential exit. 45s leaves room for the slow tail without letting a
# genuinely dead page hold a worker.
CONTENT_TIMEOUT_MS = 45_000
CONTENT_TIMEOUT_MS_PRODUCT = 30_000


def ready_selector(mode: str) -> str:
    return READY_SELECTOR_PRODUCT if mode == "product" else READY_SELECTOR_LISTING


def min_matches(mode: str) -> int:
    return MIN_CARD_MATCHES_PRODUCT if mode == "product" else MIN_CARD_MATCHES


def content_timeout_ms(mode: str) -> int:
    return CONTENT_TIMEOUT_MS_PRODUCT if mode == "product" else CONTENT_TIMEOUT_MS


# ---------------------------------------------------------------------------
# The lazy-load scroll
# ---------------------------------------------------------------------------
# §8's rule, and every clause of it was earned somewhere in this family:
#
#   * Scroll to `document.body.scrollHeight`, not a fixed wheel distance. A
#     2400px wheel stopped three rounds short of the bottom of a 7600px grid
#     in the sibling repo and the trigger was never reached. Tokopedia's
#     search page grew 1606 -> 3730 -> 8157px across two rounds, so a fixed
#     distance would fall behind immediately.
#   * Require the count AND the height to hold still for THREE rounds, not
#     one: the next batch takes longer to arrive than a single pause.
#   * Stop when a round adds nothing new. On search that is the ONLY
#     termination condition available — see `pagination_is_addressable`.
SCROLL_ROUNDS_MAX = 24
SCROLL_PAUSE_MS = 2_200
SCROLL_STABLE_ROUNDS = 3


def wait_for_count(count: Callable[[str], int],
                   sleep: Callable[[int], None],
                   selector: str,
                   want: int,
                   timeout_ms: int,
                   poll_ms: int = 500) -> int:
    """Poll until `count(selector) > want`, or the timeout. Returns the count.

    A POLL rather than the driver's own wait-for-predicate, and this is not a
    style choice: **Tokopedia's Content-Security-Policy has no
    `unsafe-eval`.** Playwright's `wait_for_function` hands the browser a
    STRING to evaluate, so on a /search page it dies with

        EvalError: Evaluating a string as JavaScript violates the following
        Content Security Policy directive … 'strict-dynamic' … 'report-sample'

    which took a live run down with exit 1 — a crash, on the site's most
    obvious URL. Counting elements goes over CDP instead
    (`querySelectorAll` through the protocol, not through eval), so it works
    under any CSP and works identically in all three drivers.

    The count is returned rather than a bool so a caller can say how close it
    got, and a timeout is not an error: a listing with genuinely no products
    on it never reaches `want`, and that is exit 4 rather than a fault.
    """
    waited = 0
    found = 0
    while True:
        try:
            found = count(selector)
        except Exception as exc:                    # a driver-level fault
            logger.warning("could not count %r: %s", selector, exc)
            return found
        if found > want:
            return found
        if waited >= timeout_ms:
            return found
        sleep(poll_ms)
        waited += poll_ms


def scroll_until_settled(count: Callable[[str], int],
                         page_height: Callable[[], Optional[int]],
                         scroll_to_bottom: Callable[[], None],
                         sleep: Callable[[int], None],
                         selector: str = READY_SELECTOR_LISTING,
                         rounds: int = SCROLL_ROUNDS_MAX,
                         pause_ms: int = SCROLL_PAUSE_MS,
                         stable_rounds: int = SCROLL_STABLE_ROUNDS) -> dict:
    """Scroll a listing until it stops growing. Returns what happened.

    Pure policy: every browser operation arrives as a callable, so this runs
    identically under all three drivers and is testable with the browser
    stubbed out.

    The returned dict is meant for the sidecar. `settled` False means the
    round budget ran out with the page still growing — the run is PARTIAL and
    must say so, because a listing that was still loading when we stopped is
    not an exhausted one.
    """
    seen_counts: List[int] = []
    stable = 0
    prev = None
    for i in range(rounds):
        sleep(pause_ms)
        try:
            found = count(selector)
        except Exception as exc:                    # a driver-level fault
            logger.warning("scroll round %d could not count: %s", i, exc)
            break
        height = page_height()
        seen_counts.append(found)
        same = prev is not None and found == prev[0] and height == prev[1]
        stable = stable + 1 if same else 0
        logger.info("scroll round %d: %d cards, height %s, stable %d",
                    i, found, height, stable)
        prev = (found, height)
        if stable >= stable_rounds and found >= MIN_CARD_MATCHES:
            return {"settled": True, "rounds": i + 1, "cards": found,
                    "height": height, "counts": seen_counts}
        scroll_to_bottom()
    return {"settled": False, "rounds": len(seen_counts),
            "cards": prev[0] if prev else 0,
            "height": prev[1] if prev else None, "counts": seen_counts}


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------
def classify(html: Optional[str], status: Optional[int] = None,
             url: str = "") -> str:
    """The page's state, in one place, for all three engines.

    `status` is OPTIONAL, and that default is load-bearing rather than
    tidy. Playwright hands back a response object with a status on it;
    pyppeteer and Selenium do not expose one at the point this is called, so
    they pass only the markup. When `status` was required positionally both
    of those engines crashed with `TypeError` on their FIRST fetch — and
    that was invisible to import, to `--help`, to `compileall`, to the AST
    undefined-name walk and to 426 green offline checks, because none of
    them calls a function the way a live run does.

    `test_engines` now checks every `page_flow.*` call in every engine
    against this module's real signatures, which is the general form of that
    bug.
    """
    if html is None:
        # No markup reached us at all. On this site that is what a refusal
        # looks like, so it is `blocked` rather than a crash.
        return "blocked"
    return detect_page_state(html, status, url)


# The retry/solve/blocked decision as DATA rather than as three copies of an
# if-chain, so an engine cannot quietly disagree with its twins about whether
# a page is worth retrying or worth paying for.
#
#   retry    fetch it again — a different exit if there is a pool
#   solve    spend money on a captcha here
#   blocked  contributes to exit 3
#   parse    hand the html to the parser
STATE_POLICY = {
    "content":   {"retry": False, "solve": False, "blocked": False, "parse": True},
    # An exhausted page, a no-match query, a discovery hub. A real answer.
    "empty":     {"retry": False, "solve": False, "blocked": False, "parse": True},
    # Nothing arrived. No page to solve, but a different exit is worth a try.
    "blocked":   {"retry": True,  "solve": False, "blocked": True,  "parse": False},
    # Never observed on this site. If a bot manager is ever switched on, this
    # is the state that would pay for it — and `--solve-captcha when-blocked`
    # still gates the spend on there being no products on the page.
    "challenge": {"retry": True,  "solve": True,  "blocked": True,  "parse": False},
    # Served by Tokopedia, no grid, no no-results message. Almost always
    # "the GraphQL response has not landed yet", which a retry fixes and a
    # solve cannot.
    "unknown":   {"retry": True,  "solve": False, "blocked": False, "parse": False},
}


def should_retry(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["retry"]


def should_solve(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["solve"]


def counts_as_blocked(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["blocked"]


def should_parse(state: str) -> bool:
    return STATE_POLICY.get(state, STATE_POLICY["unknown"])["parse"]


def is_unpainted(state: str, html: Optional[str]) -> bool:
    """Whether this page is "not painted yet" rather than actually wrong.

    This distinction is load-bearing and the first live run of the search
    path is what found it. §8 says missing content is one of three things —
    not painted, lazy-loaded, or a different page served to this session —
    and the two Tokopedia page kinds differ in WHICH:

      * A CATEGORY listing server-renders its grid container
        (`data-ssr="productsCategoryL2/L3SSR"` is in the first response), so
        it classifies as "content" immediately and only the tiles inside it
        need the scroll.
      * A SEARCH grid does not. `divSRPContentProducts` arrives with the
        client-side GraphQL response, so at `domcontentloaded` the page is a
        shell with `divSRPLazyProductWrapper` placeholders and nothing else.

    Classified naively, the second is "unknown", and "unknown" retries — so
    the first live search run fetched the page twice, waited for nothing,
    scrolled not at all and reported 0 rows with exit 4. A page Tokopedia
    plainly served, with no no-results message on it, is waiting to paint;
    the answer is to WAIT, not to spend a retry and not to rotate the exit.
    """
    return state == "unknown" and bool(html) and served_by_tokopedia(html)


# A blocked page is worth retrying, and this is measured rather than hopeful:
# a Scraping Browser profile refused the first requests of a session and
# served everything after them, and three of six captures had their remote
# target closed mid-run and succeeded on the next attempt. Rotating to
# another exit is the right response to a scored address.
RETRY_ON_BLOCKED = True
# Without a pool there is only one address to try, so retrying it mostly
# spends time. One extra attempt, because the observed failure is often the
# session rather than the address.
BLOCK_RETRIES_WITHOUT_POOL = 1

# Never spend more than this on one page, whatever the retry budget says. No
# challenge has ever been observed on this site, so any solve here is
# speculative and the cap keeps a speculative path from becoming a bill.
SOLVES_PER_PAGE = 1


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------
# The most important thing in this file, and the one a reader is most likely
# to "fix" wrongly.
#
# A category listing paginates with `?page=N`: page 1 of
# /p/makanan-minuman/minuman/kopi-bubuk gave 66 products, `?page=2` gave 61,
# and they overlapped by exactly 3 — the "cheaper products" carousel that
# appears on both, which is why the parser is grid-scoped.
#
# **Search does not paginate at all.** A fully scrolled search page publishes
# no `href` containing `page=`, no `link[rel=next]` and no next-page button:
# every button on it is a filter or a sort. And `?page=2` on a search URL
# does not paginate, it EMPTIES the result set — 200, no grid, "Oops, produk
# nggak ditemukan".
#
# That last part is the trap. A `page_url()` used unconditionally would fetch
# that empty page, find no new sku, conclude the listing was exhausted, and
# report a COMPLETE run holding page 1 — §7's silent-single-page failure
# arriving by a different route. So an engine must ask
# `pagination_is_addressable` BEFORE it plans page URLs, and fall back to
# scrolling one long page when the answer is no.
NEXT_PAGE_SELECTOR: List[str] = [
    # Ordered most-durable first, per §5 — standards-based signals before
    # build artefacts. NONE of these has ever matched on Tokopedia: the list
    # is here so that if the site ever grows real pagination markup the
    # engines pick it up, and every entry is honestly marked as unobserved.
    'link[rel="next"]',            # unobserved: 0 occurrences on all captures
    'a[rel="next"]',               # unobserved
    '[data-testid="btnNextPage"]', # unobserved; named after the site's own
                                   # btn* convention, so it is the shape a
                                   # real one would take
]


def next_page_selector(page_num: int = 1) -> str:
    return ", ".join(NEXT_PAGE_SELECTOR)


def pagination_is_addressable(page1_url: str,
                              advertised_hrefs: Optional[List[str]] = None) -> bool:
    """Whether page N of this listing can be fetched without fetching N-1.

    Two layers, and the URL kind decides which applies:

      * A category listing: `?page=N` is the convention and it works. If the
        page ALSO advertises next-page hrefs, they are checked for agreement
        — a cursor or a token the convention cannot reproduce means the
        chain has to be followed link by link.
      * A search: no. There is nothing to construct and nothing advertised.

    An engine that gets False here must fetch one long page and scroll it,
    must not plan page URLs, and must not run workers concurrently.
    """
    if not paginates_by_url(page1_url):
        return False
    if not advertised_hrefs:
        # No advertised link and a working convention: constructing is all
        # there is, and it was verified against a real page 2.
        return True
    built = page_url(page1_url, 2)
    if built is None:
        return False
    want = strip_tracking(built)
    return any(strip_tracking(h) == want for h in advertised_hrefs if h)


def pagination_agrees(current_url: str, page_num: int,
                      advertised_hrefs: Optional[List[str]] = None) -> bool:
    """Whether the site's own next-page link matches what we would build."""
    if not advertised_hrefs:
        return True
    built = page_url(current_url, page_num + 1)
    if built is None:
        return False
    want = strip_tracking(built)
    return any(strip_tracking(h) == want for h in advertised_hrefs if h)


def next_page_candidates(current_url: str,
                         advertised_hrefs: Optional[List[str]] = None) -> List[str]:
    """Addresses worth trying for the next page, best first."""
    out: List[str] = []
    built = page_url(current_url, page_number_from_url(current_url) + 1)
    if built:
        out.append(built)
    for href in advertised_hrefs or []:
        if href and _same_listing(current_url, href) and href not in out:
            out.append(href)
    return out


def _same_listing(current_url: str, candidate: str) -> bool:
    """Whether a candidate href is another page of THIS listing.

    Guards against following a link out of the listing — a "cheaper
    products" card, a related category, a promo banner — which would silently
    replace the run's subject.
    """
    a, b = urlsplit(current_url), urlsplit(candidate)
    if b.netloc and a.netloc and b.netloc != a.netloc:
        return False
    return a.path.rstrip("/") == b.path.rstrip("/")


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------
def concurrency_limit(url: str) -> Optional[int]:
    """The most workers this URL can usefully be given, or None for no limit.

    1 for anything that does not paginate by URL, and the reason is
    structural rather than cautious: a worker cannot be handed "page 5"
    because page 5 has no address. Reaching the 300th tile means scrolling
    past the first 299 in one session, so there is nothing to parallelise and
    N workers would simply fetch page 1 N times from N addresses.

    Refuse it with that reason rather than accepting it silently — a flag
    that appears to work and does nothing is worse than one that says no.
    """
    return None if paginates_by_url(url) else 1


def concurrency_refusal(url: str) -> Optional[str]:
    """Why --concurrency cannot be honoured for this URL, or None if it can."""
    if concurrency_limit(url) != 1:
        return None
    kind = listing_kind(url)
    if kind == "search":
        what = ("a Tokopedia search listing has no per-page addresses — it is "
                "one infinitely scrolling page, and ?page=N on a search URL "
                "returns an EMPTY result set rather than page N")
    elif kind == "hub":
        what = ("a /p/<slug> URL is a discovery hub with no product grid on "
                "it, so there are no pages to divide")
    else:
        what = ("this URL does not paginate by address, so page N cannot be "
                "fetched without fetching N-1 first")
    return (what + ". Workers would each re-fetch the same page. Use a "
            "category listing URL (/p/<cat>/<sub>/<subsub>), which does "
            "paginate with ?page=N, or run with --concurrency 1.")


def comparable(url: str) -> str:
    """A URL reduced to what identifies the page, for dedupe and comparison."""
    return strip_tracking(url)
