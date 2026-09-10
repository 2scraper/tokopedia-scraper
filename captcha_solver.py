"""
captcha_solver.py
------------------
Shared helper used by all three scrapers (Playwright / Selenium / Puppeteer).

Detection runs after EVERY page navigation in the main loop of all three
scrapers, regardless of what URL was requested (category hub, product page,
sign-in, checkout, anything) — this is deliberate, not scoped to any one
page. If Etsy renders a reCAPTCHA challenge anywhere — account, sign-in and
checkout flows are the usual places — this fires.

TWO VENDORS, TWO PATHS. Etsy's listing pages are gated by DataDome rather
than reCAPTCHA, so this module carries both:

  * reCAPTCHA, below — for the account, sign-in and checkout flows where Etsy
    does use it. Returns a TOKEN to inject into the page.
  * DataDome, further down (`solve_datadome`) — for a listing page's own
    challenge. A different task type, a different success signal: it returns
    a COOKIE, which the caller sets before reloading. It also REQUIRES a
    proxy, because the cookie is bound to the address that solved it.

Which state reaches which is decided in `product_parser.detect_page_state`
and `page_flow.STATE_POLICY`, not here — and the distinction is about money:
a `t=bv` DataDome page has nothing solvable on it, so no solve is bought for
one. See the header above `solve_datadome`.

Flow:
  1. Both detectors run and are reconciled (see reconcile_detections) to decide
     the variant: v3, v2-invisible or v2-checkbox. The parameters differ per
     variant and are not interchangeable — v3 params sent for a v2-invisible
     widget buy a token the site rejects.
  2. The challenge goes to 2captcha's API (v2 by default, legacy v1 as a
     one-shot fallback) with the parameters for that variant.
  3. The resulting token is injected into the page's `g-recaptcha-response`
     textarea and any bound callback is invoked.

Often none of this is needed. Over the Scraping Browser API,
`Captcha.setAutoSolve` can clear the challenge inside the browser before this
code gets a turn — treat `Captcha.solveFinished` as the success signal and keep
this path as the fallback rather than assuming every detection completes.
This module is the path for a browser you launched yourself.

No other captcha vendor is integrated (per spec: no competitors).
"""

from __future__ import annotations

import base64
import re
import time
import logging
from dataclasses import dataclass
from typing import Optional, Tuple
from urllib.parse import urljoin, urlparse, urlunparse, parse_qsl, urlencode

import requests

logger = logging.getLogger("captcha_solver")


# An API key must never reach a log, and the easiest way for one to get there
# is an exception message. `requests` puts the FULL URL — query string and all
# — into the text of HTTPError and of every connection error, so any endpoint
# that takes its key as a query parameter leaks it the moment something goes
# wrong. That is not hypothetical: a 400 from the fingerprint endpoint printed
# a live key to the terminal.
#
# Everything raised or logged from this module goes through here first. The
# endpoint and the status survive, because which call failed is the useful
# half and is not the secret.
_KEY_IN_TEXT_RE = re.compile(
    r"((?:client)?key|token|api[_-]?key)=([^&\s'\"]{6,})", re.IGNORECASE)

# A PROXY credential is the other secret this module handles, and it does not
# look like a query parameter: it rides in the userinfo of a URL
# (`scheme://login:password@host:port`). Since this module gained a path that
# takes a proxy — the DataDome solve, which cannot run without one — its
# errors can quote one, and an error that quotes a proxy URL prints the
# password.
#
# Found by a refusal message doing exactly that: a portless proxy was
# rejected with the whole URL in the text. The host and the port survive
# here, deliberately: WHICH exit failed is the useful half of that message
# and is not the secret.
#
# Matched GLOBALLY, not once. A masker that handles the first occurrence
# prints the password the other four times and still looks like it works.
_PROXY_CREDENTIALS_RE = re.compile(
    r"([a-z][a-z0-9+.\-]*://)[^\s/@\"']+:[^\s/@\"']+@", re.IGNORECASE)


def _redact(text) -> str:
    """Everything raised or logged from this module goes through here first.

    An EXCEPTION MESSAGE IS A LOG: `requests` puts the full URL, query string
    and all, into the text of HTTPError and of every connection error, so an
    endpoint that takes its key as a query parameter leaks it the moment
    anything goes wrong.
    """
    masked = _KEY_IN_TEXT_RE.sub(r"\1=***", str(text))
    return _PROXY_CREDENTIALS_RE.sub(r"\1***:***@", masked)

# 2captcha has two generations of solver API and both are live.
#
#   v2 (current, documented at https://2captcha.com/api-docs):
#       POST https://api.2captcha.com/createTask     {clientKey, task:{...}}
#       POST https://api.2captcha.com/getTaskResult  {clientKey, taskId}
#     JSON in, JSON out, typed task objects (RecaptchaV3TaskProxyless etc.).
#
#   v1 (legacy, still accepted):
#       POST https://2captcha.com/in.php   form-encoded, method=userrecaptcha
#       GET  https://2captcha.com/res.php  polling
#
# This module speaks v2 by default and keeps v1 as a fallback, because the
# original build was written against v1 and every live result recorded in the
# README came through it. Switch with solve_recaptcha(..., api_version="v1").
TWOCAPTCHA_API_V2 = "https://api.2captcha.com"
TWOCAPTCHA_CREATE_TASK_URL = f"{TWOCAPTCHA_API_V2}/createTask"
TWOCAPTCHA_GET_RESULT_URL = f"{TWOCAPTCHA_API_V2}/getTaskResult"
TWOCAPTCHA_BALANCE_URL = f"{TWOCAPTCHA_API_V2}/getBalance"

TWOCAPTCHA_IN_URL = "https://2captcha.com/in.php"
TWOCAPTCHA_RES_URL = "https://2captcha.com/res.php"

# v2 rejects an arbitrary minScore: the documented values are these three.
V3_ALLOWED_MIN_SCORES = (0.3, 0.7, 0.9)

@dataclass
class CaptchaChallenge:
    kind: str            # "recaptcha_v3" | "recaptcha_v2_invisible" | "recaptcha_v2"
    sitekey: str
    action: str = "verify"
    page_url: str = ""
    # How the challenge was found: "html" (static markup) or "runtime"
    # (read out of the live page's reCAPTCHA client config). Recorded
    # because the two paths can disagree about the version, and the
    # runtime one is authoritative when they do.
    source: str = "html"
    # Raw `size` from the reCAPTCHA client config, when available:
    # "invisible" for v2-invisible and for v3, absent for a v2 checkbox.
    size: Optional[str] = None

    @property
    def is_v3(self) -> bool:
        return self.kind == "recaptcha_v3"

    @property
    def is_invisible_v2(self) -> bool:
        return self.kind == "recaptcha_v2_invisible"


def detect_recaptcha_v3(html: str, page_url: str) -> Optional[CaptchaChallenge]:
    """Scan raw page HTML for a reCAPTCHA v3 challenge, in either of two
    real-world formats seen so far:

    1. An inline <script> calling grecaptcha.execute('SITEKEY', {action: '...'})
       directly — seen on Kohl's.
    2. A <captcha-widget> custom element carrying the config as HTML
       attributes (data-captcha-type="recaptcha" data-version="v3"
       data-sitekey="..." data-action="..."), with the actual execute()
       call happening inside a bundled JS file, never appearing as
       readable inline script text at all — confirmed live on the sibling
       farfetch-scraper repo's target,
       sign-up modal via DevTools inspection. A previous version of this
       function, which only checked for format 1, reported "no captcha"
       on this exact page despite one being genuinely present — caught by
       manually inspecting the DOM, not by the detector itself, which is
       exactly the gap this format-2 check closes.
    """
    if "recaptcha" not in html.lower():
        return None

    # Format 2 first — a structured HTML attribute match is more reliable
    # than the format-1 regex when both happen to be present.
    for widget_match in re.finditer(r"<captcha-widget\b([^>]*)>", html, re.IGNORECASE):
        attrs = widget_match.group(1)
        version_match = re.search(r'data-version=["\']v(\d)["\']', attrs)
        sitekey_match = re.search(r'data-sitekey=["\']([\w-]{20,})["\']', attrs)
        if version_match and version_match.group(1) == "3" and sitekey_match:
            action_match = re.search(r'data-action=["\']([\w_]+)["\']', attrs)
            action = action_match.group(1) if action_match and action_match.group(1) != "null" else "verify"
            return CaptchaChallenge(
                kind="recaptcha_v3",
                sitekey=sitekey_match.group(1),
                action=action,
                page_url=page_url,
            )

    if "grecaptcha" not in html:
        return None

    exec_match = re.search(
        r"grecaptcha\.execute\(\s*['\"]([\w-]{20,})['\"]\s*,\s*\{\s*action:\s*['\"]([\w_]+)['\"]",
        html,
    )
    if exec_match:
        return CaptchaChallenge(
            kind="recaptcha_v3",
            sitekey=exec_match.group(1),
            action=exec_match.group(2),
            page_url=page_url,
        )

    key_match = re.search(r"data-sitekey=['\"]([\w-]{20,})['\"]", html)
    if key_match and "grecaptcha.render" in html:
        return CaptchaChallenge(kind="recaptcha_v3", sitekey=key_match.group(1), page_url=page_url)

    return None


# ---------------------------------------------------------------------------
# Runtime detection (added 2026-08-24 — the static-HTML detector below no
# longer matched the sibling farfetch-scraper repo's target)
# ---------------------------------------------------------------------------
#
# What changed there: the sign-up modal used to render
#   <captcha-widget data-version="v3" data-sitekey="6Leif..." ...>
# with the whole config in HTML attributes. Verified live on 2026-08-24, that
# element is GONE. The modal now loads
#   https://recaptcha.net/recaptcha/api.js?render=explicit
# and configures the widget entirely in JavaScript, into a
#   <div id="register-captcha" class="g-recaptcha">
# container. There is no `data-sitekey`, no inline `grecaptcha.execute(...)`,
# and no `grecaptcha.render` anywhere in the served HTML — all four things
# detect_recaptcha_v3() keys off are absent. It returns None on this page
# even though a real reCAPTCHA is present and active.
#
# The sitekey is still recoverable, but only at runtime, from two places
# that exist in the live page and not in its HTML:
#   1. window.___grecaptcha_cfg.clients — the reCAPTCHA API's own client
#      registry. Property names inside it are minified and change between
#      releases, so this walks the object looking for a value shaped like a
#      sitekey (^6L[\w-]{30,}$) rather than trusting any key name.
#   2. the reCAPTCHA iframe's `k=` query parameter — a stable, documented
#      part of the widget's URL, used as a cross-check and fallback.
#
# The same client config also carries `size`, which is what tells v3 apart
# from v2-invisible. That distinction is not cosmetic: 2captcha needs
# `version=v3` + `action` + `min_score` for one and `invisible=1` for the
# other, and sending the wrong one burns balance for a token that won't
# validate.
RECAPTCHA_DISCOVERY_JS = r"""
() => {
  const out = {found: false, sitekey: null, size: null, action: null,
               enterprise: false, containerId: null, hints: []};

  const SITEKEY_RE = /^6L[\w-]{30,}$/;

  // --- 1. the reCAPTCHA client registry -----------------------------------
  // Minified property names, so match on value shape, not key name.
  try {
    const clients = (window.___grecaptcha_cfg || {}).clients || null;
    if (clients) {
      for (const id of Object.keys(clients)) {
        const seen = new Set();
        const walk = (o, depth) => {
          if (!o || depth > 4 || seen.has(o)) return;
          if (typeof o === 'object') seen.add(o);
          for (const k of Object.keys(o)) {
            let v;
            try { v = o[k]; } catch (e) { continue; }
            if (typeof v === 'string') {
              if (!out.sitekey && SITEKEY_RE.test(v)) { out.sitekey = v; out.found = true; }
              else if (v === 'invisible' || v === 'normal' || v === 'compact') out.size = out.size || v;
            } else if (v && typeof v === 'object') {
              walk(v, depth + 1);
            }
          }
        };
        walk(clients[id], 0);
      }
      if (out.sitekey) out.hints.push('sitekey from ___grecaptcha_cfg');
    }
  } catch (e) { out.hints.push('cfg walk failed: ' + e.message); }

  // --- 2. the widget iframe's k= parameter --------------------------------
  try {
    for (const f of document.querySelectorAll('iframe[src*="recaptcha"]')) {
      const m = /[?&]k=([\w-]{20,})/.exec(f.getAttribute('src') || '');
      if (m) {
        if (!out.sitekey) { out.sitekey = m[1]; out.found = true; out.hints.push('sitekey from iframe k= param'); }
        else if (out.sitekey !== m[1]) out.hints.push('iframe k= disagrees with cfg: ' + m[1]);
        break;
      }
    }
  } catch (e) { out.hints.push('iframe scan failed: ' + e.message); }

  // --- 3. legacy paths, still checked in case they come back --------------
  try {
    const w = document.querySelector('captcha-widget[data-sitekey]');
    if (w) {
      out.found = true;
      out.sitekey = out.sitekey || w.getAttribute('data-sitekey');
      const dv = w.getAttribute('data-version');
      if (dv) out.hints.push('legacy captcha-widget data-version=' + dv);
      const da = w.getAttribute('data-action');
      if (da && da !== 'null') out.action = da;
    }
    const gr = document.querySelector('[data-sitekey]');
    if (gr && !out.sitekey) {
      out.sitekey = gr.getAttribute('data-sitekey'); out.found = !!out.sitekey;
      out.hints.push('sitekey from a data-sitekey attribute');
    }
  } catch (e) { out.hints.push('legacy scan failed: ' + e.message); }

  // --- 4. context -------------------------------------------------------
  try {
    const c = document.querySelector('.g-recaptcha[id], [id*="captcha"]');
    if (c) out.containerId = c.id || null;
    out.enterprise = !!(window.grecaptcha && window.grecaptcha.enterprise);
    out.badge = !!document.querySelector('.grecaptcha-badge');
    // A bframe iframe is the interactive challenge. v3 never shows one;
    // v2-invisible does when it decides to challenge.
    out.challengeFrame = !!document.querySelector('iframe[src*="bframe"]');
    out.scripts = [...document.querySelectorAll('script[src*="recaptcha"]')]
                    .map(s => (s.getAttribute('src') || '').split('?')[0]);
    // The api.js `render` parameter is the strongest v2-vs-v3 signal there
    // is, per Google's own docs: v3 loads api.js?render=<SITE_KEY>, while
    // v2 (checkbox and invisible alike) loads api.js?render=explicit.
    out.renderParam = null;
    for (const sc of document.querySelectorAll('script[src*="recaptcha"]')) {
      const m = /[?&]render=([^&]+)/.exec(sc.getAttribute('src') || '');
      if (m) { out.renderParam = decodeURIComponent(m[1]); break; }
    }
  } catch (e) { out.hints.push('context scan failed: ' + e.message); }

  return out;
}
"""


def detect_recaptcha_in_page(evaluate, page_url: str = "") -> Optional[CaptchaChallenge]:
    """Detect a reCAPTCHA by inspecting the LIVE page, not its HTML.

    `evaluate` is a callable that runs RECAPTCHA_DISCOVERY_JS in the page and
    returns the resulting dict — i.e. `page.evaluate` under Playwright,
    `driver.execute_script` under Selenium (wrap it so the arrow function is
    invoked), or an awaited `page.evaluate` under pyppeteer.

    Version inference, and why it is deliberately conservative:
      * `size == "invisible"` PLUS an interactive bframe iframe present is
        the signature of **v2-invisible**, not v3 — v3 never renders a
        challenge frame. This is what that site looked like as of
        2026-08-24, on the same sitekey that its old markup explicitly
        labelled `data-version="v3"`.
      * `size == "invisible"` with no challenge frame is treated as v3,
        which is v3's normal appearance (badge only).
      * a `normal`/`compact` size is a v2 checkbox.
    When the two signals disagree the ambiguity is recorded in the returned
    challenge's `kind` and in the log, rather than silently guessing — pick
    the wrong one and 2captcha returns a token the site rejects.
    """
    try:
        info = evaluate(RECAPTCHA_DISCOVERY_JS)
    except Exception as e:  # noqa: BLE001 - any engine's evaluate can raise
        logger.debug("In-page reCAPTCHA discovery failed: %s", e)
        return None

    if not info or not info.get("found") or not info.get("sitekey"):
        return None

    size = info.get("size")
    render = info.get("renderParam")

    # Classification, strongest signal first.
    #
    # 1. api.js's `render` parameter. Per Google's docs, v3 loads
    #    api.js?render=<SITE_KEY> while v2 — checkbox and invisible alike —
    #    loads api.js?render=explicit. v3 has no `size` concept and never
    #    renders a challenge iframe at all.
    #      https://developers.google.com/recaptcha/docs/v3
    #      https://developers.google.com/recaptcha/docs/invisible
    #    So render=explicit rules v3 out outright, and render=<the sitekey>
    #    confirms it outright.
    # 2. `size`, which only exists for v2: invisible / normal / compact.
    # 3. presence of a bframe (interactive challenge) iframe — v2 only.
    if render and render != "explicit" and render == info.get("sitekey"):
        kind = "recaptcha_v3"
    elif render == "explicit":
        kind = "recaptcha_v2_invisible" if size == "invisible" else "recaptcha_v2"
    elif size in ("normal", "compact"):
        kind = "recaptcha_v2"
    elif info.get("challengeFrame") and size == "invisible":
        kind = "recaptcha_v2_invisible"
    else:
        kind = "recaptcha_v3"

    hints = info.get("hints") or []
    logger.info("reCAPTCHA found at runtime: kind=%s sitekey=%s size=%s render=%s "
                "challengeFrame=%s container=%s hints=%s",
                kind, info["sitekey"], size, render, info.get("challengeFrame"),
                info.get("containerId"), "; ".join(hints))
    if info.get("enterprise"):
        logger.warning("This is a reCAPTCHA ENTERPRISE widget — 2captcha needs its "
                       "enterprise method, which this project does not implement.")

    return CaptchaChallenge(
        kind=kind,
        sitekey=info["sitekey"],
        action=info.get("action") or "verify",
        page_url=page_url,
        source="runtime",
        size=size,
    )


def reconcile_detections(html_challenge: Optional[CaptchaChallenge],
                          runtime_challenge: Optional[CaptchaChallenge]
                          ) -> Optional[CaptchaChallenge]:
    """Pick between the two detectors when both find something.

    They can disagree on the SAME page, and on the site this was written
    against they did. A capture
    taken through the Scraping Browser on 2026-08-24 contains, simultaneously:

      * `<captcha-widget data-version="v3" data-sitekey="6Leif..."
         data-action="null">` — the site's own wrapper element, asserting v3
      * `recaptcha/api.js?render=explicit`, a client registered with
        `size: "invisible"`, and a `bframe` challenge iframe — the documented
        **v2-invisible** signature

    Both cannot be true. `data-version` is an attribute on the site's own
    component: it says what their code believes. `render=explicit` + `size` +
    the challenge frame describe the Google loader that is actually on the
    page, which is what Google enforces and therefore what 2captcha has to
    match. So the runtime reading wins, and the disagreement is logged rather
    than quietly resolved.

    Supporting detail: `data-action="null"` in that markup means there is no
    action string. v3 scores partly on the action; a real v3 integration
    passes one. An empty action fits a v2-invisible widget wearing a stale
    v3 label better than it fits working v3.

    (Also worth knowing: the same modal served to a European residential IP
    the same day had NO `<captcha-widget>` element at all — just the
    `render=explicit` loader. That site served more than one variant of this
    modal, so neither detector alone is enough.)
    """
    if runtime_challenge and not html_challenge:
        return runtime_challenge
    if html_challenge and not runtime_challenge:
        return html_challenge
    if not html_challenge and not runtime_challenge:
        return None

    if html_challenge.kind != runtime_challenge.kind:
        logger.warning(
            "Detectors disagree on this page: static markup says %s (from the "
            "site's own data-version), the live loader says %s (render/size/"
            "challenge-frame). Trusting the loader — that's what Google "
            "enforces and what 2captcha has to match.",
            html_challenge.kind, runtime_challenge.kind)
        # Keep the action if the static markup had a real one; the runtime
        # path often can't see it.
        if html_challenge.action and html_challenge.action != "verify":
            runtime_challenge.action = html_challenge.action
    return runtime_challenge


def _v2_task_for(challenge: CaptchaChallenge, min_score: float) -> dict:
    """Build the API-v2 `task` object for a challenge.

    Task type per variant, from https://2captcha.com/api-docs:
      * v3           -> RecaptchaV3TaskProxyless, with pageAction + minScore
      * v2 invisible -> RecaptchaV2TaskProxyless with isInvisible: true
      * v2 checkbox  -> RecaptchaV2TaskProxyless

    The `*Proxyless` types let 2captcha use its OWN IP pool, which is the
    right trade for reCAPTCHA: a v2/v3 token is not bound to the address that
    produced it, so the solve and the submission may leave from different
    places. The non-proxyless variants (RecaptchaV2Task) exist for the cases
    where that is not true, and they are not wired up here.
    """
    if challenge.is_v3:
        # minScore is not free-form: 0.3 / 0.7 / 0.9 are the documented values.
        score = min(V3_ALLOWED_MIN_SCORES,
                    key=lambda allowed: abs(allowed - min_score))
        if score != min_score:
            logger.info("minScore %.2f is not one of %s — using %.1f.",
                        min_score, V3_ALLOWED_MIN_SCORES, score)
        task = {
            "type": "RecaptchaV3TaskProxyless",
            "websiteURL": challenge.page_url,
            "websiteKey": challenge.sitekey,
            "minScore": score,
        }
        # v3 scores partly on the action, so send it when it's a real one.
        # "verify" is this module's placeholder for "the page didn't say".
        if challenge.action and challenge.action != "verify":
            task["pageAction"] = challenge.action
        return task

    task = {
        "type": "RecaptchaV2TaskProxyless",
        "websiteURL": challenge.page_url,
        "websiteKey": challenge.sitekey,
    }
    if challenge.is_invisible_v2:
        task["isInvisible"] = True
    return task


def _solve_with_2captcha_v2(api_key: str, challenge: CaptchaChallenge,
                             min_score: float = 0.7, poll_interval: int = 5,
                             max_wait: int = 180) -> str:
    """Solve via API v2: createTask, then poll getTaskResult."""
    task = _v2_task_for(challenge, min_score)
    logger.info("createTask: %s (sitekey=%s)", task["type"], challenge.sitekey)

    created = requests.post(TWOCAPTCHA_CREATE_TASK_URL,
                            json={"clientKey": api_key, "task": task},
                            timeout=30)
    created.raise_for_status()
    payload = created.json()
    if payload.get("errorId"):
        raise RuntimeError(
            f"createTask failed: {payload.get('errorCode')} — "
            f"{payload.get('errorDescription')}")
    task_id = payload["taskId"]

    waited = 0
    while waited < max_wait:
        time.sleep(poll_interval)
        waited += poll_interval
        got = requests.post(TWOCAPTCHA_GET_RESULT_URL,
                            json={"clientKey": api_key, "taskId": task_id},
                            timeout=30)
        got.raise_for_status()
        result = got.json()
        if result.get("errorId"):
            raise RuntimeError(
                f"getTaskResult failed: {result.get('errorCode')} — "
                f"{result.get('errorDescription')}")
        if result.get("status") == "ready":
            solution = result.get("solution") or {}
            # v2 returns the same string under both names.
            token = solution.get("gRecaptchaResponse") or solution.get("token")
            if not token:
                raise RuntimeError(f"task ready but no token in solution: {solution}")
            logger.info("2captcha solved %s in ~%ds.", challenge.kind, waited)
            return token
        # status == "processing"

    raise TimeoutError(f"2captcha did not return a token within {max_wait}s")


def get_balance(api_key: str) -> float:
    """Account balance via API v2 — handy for a preflight check."""
    r = requests.post(TWOCAPTCHA_BALANCE_URL, json={"clientKey": api_key}, timeout=30)
    r.raise_for_status()
    d = r.json()
    if d.get("errorId"):
        raise RuntimeError(f"getBalance failed: {d.get('errorCode')}")
    return float(d["balance"])


def _solve_with_2captcha_v1(api_key: str, challenge: CaptchaChallenge,
                             min_score: float = 0.7, poll_interval: int = 5,
                             max_wait: int = 120) -> str:
    """Legacy API v1: submit to in.php, poll res.php.

    Kept as a fallback because every live result recorded in this project's
    README came through this path. Prefer v2 for new work.

    The parameters differ per variant and are NOT interchangeable — send v3
    parameters for a v2-invisible widget and you pay for a token the site
    then rejects:
      * v3            -> version=v3, action=..., min_score=...
      * v2 invisible  -> invisible=1, no action, no min_score
      * v2 checkbox   -> neither
    """
    payload = {
        "key": api_key,
        "method": "userrecaptcha",
        "googlekey": challenge.sitekey,
        "pageurl": challenge.page_url,
        "json": 1,
    }
    if challenge.is_v3:
        payload.update({"version": "v3", "action": challenge.action, "min_score": min_score})
    elif challenge.is_invisible_v2:
        payload["invisible"] = 1

    logger.info("Submitting to 2captcha as %s (sitekey=%s)", challenge.kind, challenge.sitekey)
    submit = requests.post(TWOCAPTCHA_IN_URL, data=payload, timeout=30)
    submit.raise_for_status()
    payload = submit.json()
    if payload.get("status") != 1:
        raise RuntimeError(f"2captcha submit error: {payload.get('request')}")

    task_id = payload["request"]
    waited = 0
    while waited < max_wait:
        time.sleep(poll_interval)
        waited += poll_interval
        try:
            # The v1 result endpoint takes the key as a QUERY parameter, so a
            # connection error here would otherwise put it in the log.
            result = requests.get(TWOCAPTCHA_RES_URL, params={
                "key": api_key, "action": "get", "id": task_id, "json": 1,
            }, timeout=30).json()
        except requests.RequestException as exc:
            raise RuntimeError("2captcha polling request failed: %s"
                               % _redact(exc)) from None
        if result.get("status") == 1:
            logger.info("2captcha.com solved the reCAPTCHA v3 challenge.")
            return result["request"]
        if result.get("request") != "CAPCHA_NOT_READY":
            raise RuntimeError(f"2captcha polling error: {result.get('request')}")

    raise TimeoutError("2captcha.com did not return a token in time")


def solve_recaptcha(challenge: CaptchaChallenge, twocaptcha_api_key: Optional[str],
                     api_version: str = "v2", min_score: float = 0.7) -> str:
    """Public entry point: solve `challenge` through 2captcha and return the token.

    There used to be a `use_antidetect` branch here, behind a CLI flag of the
    same name, that POSTed to a hardcoded local "solve-captcha" endpoint.
    It was removed before publishing: that endpoint was a placeholder for a
    product that does not exist under that name, so the flag could not work for
    anyone who set it, and the real "the browser solves it for you" path is
    `Captcha.setAutoSolve` over the Scraping Browser API. A flag that cannot
    succeed is worse than a missing feature — it reads as an option.
    """
    if not twocaptcha_api_key:
        raise RuntimeError(
            "A captcha was detected but no 2captcha API key was given. Pass "
            "--twocaptcha-key, or set TWOCAPTCHA_KEY. Over the Scraping Browser "
            "API you may not need either: Captcha.setAutoSolve can clear it "
            "inside the browser."
        )
    solver = _solve_with_2captcha_v1 if api_version == "v1" else _solve_with_2captcha_v2
    return solver(twocaptcha_api_key, challenge, min_score=min_score)


# ---------------------------------------------------------------------------
# DataDome — the challenge this site actually uses on its listing pages
# ---------------------------------------------------------------------------
# A different vendor, a different task type, and a different success signal
# from reCAPTCHA, so it gets its own path rather than being bent into the one
# above. What comes back is not a token to inject into a form: it is a
# COOKIE, and the caller sets it and reloads.
#
# WHAT IS AND IS NOT WORTH PAYING FOR — this is the whole reason the state
# machine distinguishes two kinds of refusal. DataDome's own JS object on the
# block page carries a `t` field, and 2Captcha's documentation is explicit:
#
#     "The value of t must be equal to fe. If t=bv, it means that your ip is
#      banned by the captcha and you need to change the ip address."
#
# So a `t=bv` page has nothing solvable on it and a solve bought for one
# returns a cookie the site rejects. `product_parser.detect_page_state`
# reports those as "blocked" and `page_flow.STATE_POLICY` does not solve
# them; only `t=fe` reaches this module. Measured 2026-09-09: two `t=fe`
# pages, two ERROR_CAPTCHA_UNSOLVABLE, and the access path that works needed
# no solve at all — which is why this is documented as a fallback rather than
# sold as the answer.
#
# THE PROXY IS MANDATORY, and not as a formality. DataDome binds the cookie
# it issues to the IP and the user agent that solved the challenge, so the
# solve has to happen through the same exit the browser is using. 2Captcha's
# API takes that as `proxyType`/`proxyAddress`/`proxyPort` and refuses the
# task without them. The practical consequence is stated plainly in
# `solve_datadome`: over `--cdp-endpoint` there IS no proxy of ours to pass —
# the remote browser owns its exit and does not tell us what it is — so this
# path cannot run there, and saying so beats sending a task that cannot work.
TWOCAPTCHA_DATADOME_TASK = "DataDomeSliderTask"


class CaptchaUnsolvable(RuntimeError):
    """2Captcha reported the challenge as unsolvable from this exit.

    Distinct from a generic failure because it calls for a DIFFERENT action:
    the documented remedy is to change the exit address, not to try again
    from the same one. The engines let their block-retry loop rotate on it
    rather than burning solves against an address the vendor has already
    refused.
    """


@dataclass
class DataDomeChallenge:
    """Everything `DataDomeSliderTask` needs, and nothing it does not.

    `captcha_url` is the challenge iframe's `src` VERBATIM — it carries the
    `cid`, `hash` and `e` values the solve is scoped to, and a re-encoded or
    HTML-escaped copy is a different string. `product_parser`'s
    `datadome_captcha_url` unescapes `&amp;` for exactly this reason.

    `user_agent` is the BROWSER'S OWN, read at runtime rather than written as
    a literal: the cookie is bound to it, so a UA that does not match the
    browser that will present the cookie produces one the site rejects.
    """
    captcha_url: str
    page_url: str
    user_agent: str

    @property
    def t(self) -> Optional[str]:
        """The `t` parameter of the challenge URL: "fe", "bv" or None."""
        m = re.search(r"[?&]t=(\w+)", self.captcha_url or "")
        return m.group(1) if m else None

    @property
    def is_solvable(self) -> bool:
        return self.t == "fe"


def datadome_proxy_fields(proxy_url: Optional[str]) -> dict:
    """`proxyType`/`Address`/`Port`/`Login`/`Password` from a proxy URL.

    Returns `{}` for no proxy, so the caller can tell "none configured" from
    "configured but unusable" — the first is a refusal with an explanation
    and the second is a bug worth raising.

    Note the SCHEME matters to the API, which accepts http, socks4 and
    socks5. The 2Captcha residential gateway was measured answering SOCKS5
    only on the ports tested, so a socks5 URL is a legitimate value here even
    though Chromium itself cannot authenticate one — the solve does not run in
    Chromium.
    """
    if not proxy_url:
        return {}
    parts = urlparse(proxy_url if "//" in proxy_url else "http://" + proxy_url)
    if not parts.hostname or not parts.port:
        raise ValueError(
            "a DataDome solve needs a proxy with a host AND a port; "
            f"{_redact(proxy_url)} has %s"
            % ("no port" if parts.hostname else "neither"))
    scheme = (parts.scheme or "http").lower()
    if scheme in ("socks5h", "socks5"):
        scheme = "socks5"
    elif scheme not in ("http", "https", "socks4"):
        raise ValueError(
            f"proxy scheme {scheme!r} is not one 2Captcha accepts "
            f"(http, socks4, socks5)")
    fields = {
        "proxyType": "http" if scheme == "https" else scheme,
        "proxyAddress": parts.hostname,
        "proxyPort": parts.port,
    }
    if parts.username:
        fields["proxyLogin"] = parts.username
    if parts.password:
        fields["proxyPassword"] = parts.password
    return fields


# The cookie 2Captcha returns arrives as a Set-Cookie-style string:
#
#     datadome=<value>; Max-Age=31536000; Domain=.etsy.com; Path=/; ...
#
# Only the name and the value are used. The attributes are re-derived by the
# caller, because a `Domain` the API guessed is not necessarily the domain the
# browser is on — and a cookie set on the wrong domain is silently ignored,
# which would look exactly like a solve that did not work.
_DD_COOKIE_RE = re.compile(r"\s*([A-Za-z0-9_\-]+)\s*=\s*([^;]+)")


def parse_datadome_cookie(raw: str) -> Tuple[str, str]:
    """(name, value) out of the API's cookie string.

    Raises rather than returning a half-answer: a cookie whose name could not
    be read is not one a caller can set, and continuing would produce a
    reload that silently changes nothing.
    """
    m = _DD_COOKIE_RE.match(raw or "")
    if not m:
        raise RuntimeError(
            "the solve returned no readable cookie (got %r)" % (raw or "")[:80])
    return m.group(1), m.group(2).strip()


def solve_datadome(challenge: DataDomeChallenge, api_key: Optional[str],
                   proxy_url: Optional[str], attempts: int = 3,
                   poll_interval: int = 6, max_wait: int = 240
                   ) -> Tuple[str, str]:
    """Solve a DataDome challenge; return the (name, value) to set as a cookie.

    TWO THINGS THE API'S OWN ANSWER DOES NOT TELL YOU, both measured:

      * `status: "ready"` is not "the challenge was solved". A task built
        from a FABRICATED captchaUrl — invented `cid` and `hash`, a challenge
        that never existed — came back ready, with a cookie, and was billed
        at the normal rate. So the only evidence that a solve worked is the
        page loading afterwards, which is why this function returns a cookie
        for the caller to VERIFY rather than a boolean.
      * `ip` in the result is the REQUESTER's address, not the proxy exit.
        It reported this machine's own egress on every task, including ones
        that passed a residential proxy. It is not usable as a check that the
        solve left from the same exit as the browser.

    `attempts` covers TRANSIENT failures — a task that could not be created,
    a poll that errored, a solve that timed out. It deliberately does NOT
    cover `ERROR_CAPTCHA_UNSOLVABLE`, which is raised as `CaptchaUnsolvable`
    on the first occurrence: the vendor's documented remedy for that is a
    different exit, so retrying the same one would spend money to be told the
    same thing. Rotating is the caller's job and its loop already knows how.
    """
    if not api_key:
        raise RuntimeError(
            "A DataDome challenge was detected but no 2captcha API key was "
            "given. Pass --twocaptcha-key, or set TWOCAPTCHA_KEY.")
    if not challenge.is_solvable:
        # Refused here as well as in the state machine, because this function
        # is reachable from anywhere and a wasted solve is a real cost.
        raise CaptchaUnsolvable(
            "this challenge is t=%s, not t=fe: DataDome says the address or "
            "the browser is banned, and the cookie a solve returns is not "
            "accepted. Change the exit (or the --cdp-endpoint pid) instead of "
            "paying for it." % (challenge.t or "unstated"))

    proxy_fields = datadome_proxy_fields(proxy_url)
    if not proxy_fields:
        raise RuntimeError(
            "A DataDome solve needs a proxy: the cookie it issues is bound to "
            "the IP that solved the challenge, so the solve has to leave from "
            "the same exit as the browser, and 2Captcha's API requires the "
            "fields. Pass --proxy (or TOKOPEDIA_PROXY).\n"
            "Over --cdp-endpoint there is no proxy of ours to pass — the "
            "remote browser owns its exit and does not disclose it — so this "
            "path cannot run there. Retry the profile or use a different pid; "
            "on the measured runs the Scraping Browser needed no solve at all.")

    task = dict(proxy_fields,
                type=TWOCAPTCHA_DATADOME_TASK,
                websiteURL=challenge.page_url,
                captchaUrl=challenge.captcha_url,
                userAgent=challenge.user_agent)

    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            logger.info("createTask: %s (attempt %d/%d, exit %s)",
                        TWOCAPTCHA_DATADOME_TASK, attempt, attempts,
                        proxy_fields["proxyAddress"])
            created = requests.post(TWOCAPTCHA_CREATE_TASK_URL,
                                    json={"clientKey": api_key, "task": task},
                                    timeout=30)
            created.raise_for_status()
            payload = created.json()
            if payload.get("errorId"):
                _raise_for_datadome_error(payload)
            task_id = payload["taskId"]

            waited = 0
            while waited < max_wait:
                time.sleep(poll_interval)
                waited += poll_interval
                got = requests.post(TWOCAPTCHA_GET_RESULT_URL,
                                    json={"clientKey": api_key,
                                          "taskId": task_id},
                                    timeout=30)
                got.raise_for_status()
                result = got.json()
                if result.get("errorId"):
                    _raise_for_datadome_error(result)
                if result.get("status") == "ready":
                    solution = result.get("solution") or {}
                    name, value = parse_datadome_cookie(solution.get("cookie"))
                    # "the API returned a cookie", NOT "the challenge is
                    # solved". Those are different claims and only the first
                    # one is ours to make: a task was measured returning
                    # `status: ready` with a billable cookie for a
                    # captchaUrl whose cid and hash were FABRICATED. So a
                    # ready result says nothing about whether the site will
                    # accept what it sold us — the reload the caller does
                    # next is what decides that, and it is the only thing
                    # worth calling success.
                    logger.info("2captcha returned a %s cookie after ~%ds "
                                "(cost %s). Whether Etsy accepts it is "
                                "decided by the reload, not by this.",
                                name, waited, result.get("cost", "?"))
                    return name, value
            raise TimeoutError(
                "2captcha did not return a DataDome cookie within %ds"
                % max_wait)
        except CaptchaUnsolvable:
            raise
        except Exception as e:  # noqa: BLE001 — retried below, or re-raised
            last_error = e
            logger.warning("DataDome solve attempt %d/%d failed: %s",
                           attempt, attempts, _redact(e))
            if attempt == attempts:
                raise RuntimeError(
                    "every DataDome solve attempt failed (%d/%d); last error: "
                    "%s" % (attempts, attempts, _redact(last_error))) from None
    raise RuntimeError("unreachable")  # pragma: no cover


# 2Captcha's own words for "this will not solve from here". Kept as a set
# rather than a substring match so a new code is reported verbatim instead of
# being silently swallowed into the retry loop.
_UNSOLVABLE_CODES = frozenset((
    "ERROR_CAPTCHA_UNSOLVABLE",
    "ERROR_PROXY_CONNECT_REFUSED",
    "ERROR_PROXY_CONNECT_TIMEOUT",
    "ERROR_PROXY_READ_TIMEOUT",
    "ERROR_PROXY_BANNED",
    "ERROR_IP_BLOCKED",
))


def _raise_for_datadome_error(payload: dict) -> None:
    """Turn an API error payload into the right exception type.

    The distinction matters to the caller: `CaptchaUnsolvable` means rotate,
    anything else means retry. Everything raised here goes through `_redact`,
    because an error description can quote the request that produced it.
    """
    code = payload.get("errorCode") or ""
    text = _redact("%s — %s" % (code, payload.get("errorDescription")))
    if code in _UNSOLVABLE_CODES:
        raise CaptchaUnsolvable(
            "%s. 2Captcha's documented remedy is a different exit address, "
            "so no further solves are attempted from this one." % text)
    raise RuntimeError(text)


INJECT_TOKEN_JS = """
(token) => {
  let el = document.getElementById('g-recaptcha-response');
  if (!el) {
    el = document.createElement('textarea');
    el.id = 'g-recaptcha-response';
    el.name = 'g-recaptcha-response';
    el.style.display = 'none';
    document.body.appendChild(el);
  }
  el.value = token;
  el.innerHTML = token;
  try {
    if (window.___grecaptcha_cfg && window.___grecaptcha_cfg.clients) {
      Object.values(window.___grecaptcha_cfg.clients).forEach((client) => {
        Object.values(client).forEach((prop) => {
          if (prop && typeof prop === 'object') {
            Object.values(prop).forEach((cb) => {
              if (typeof cb === 'function') { try { cb(token); } catch (e) {} }
            });
          }
        });
      });
    }
  } catch (e) { /* best effort, non-fatal */ }
  return true;
}
"""


# Kept as an alias: this function used to be reCAPTCHA-v3-only, and the three
# scrapers plus both modal diagnostics import it under the old name. The
# solver now branches on challenge.kind, so the name is a misnomer — the
# alias exists so older call sites keep working rather than to encourage it.
solve_recaptcha_v3 = solve_recaptcha


# ===========================================================================
# What is deliberately NOT here
# ===========================================================================
# The sibling repo in this family carries a whole second solver for its
# site's OWN first-party image captcha ("Enter the characters you see below",
# a JPEG of distorted text and a GET form). Roughly 190 lines of it, and none
# of it is ported here, because Etsy has no such page.
#
# What Etsy does instead is answer HTTP 403 with a DataDome shell, whose
# error page — no challenge, no form, nothing a solver can answer. Captured
# 2026-09-09 from a datacentre exit on five hosts (.de, .es, .nl, .pl and
# mediaworld.it): zero captcha markers of any kind, zero forms, zero images
# to read. The response to that page is a different exit address, not a
# solve, and product_parser.detect_page_state reports it as "blocked" rather
# than "captcha" precisely so no solve is attempted.
#
# The reCAPTCHA / hCaptcha / Turnstile machinery above IS kept, and that is a
# deliberate asymmetry rather than an inconsistency. Detection stays broad
# because which challenge a visitor meets depends on the exit country and on
# what the address has been doing — a narrow list is how a challenge gets
# reported as an empty page months later. A solver for a challenge this site
# has never been observed to serve is dead code; a DETECTOR for one is cheap
# insurance. See the family note in CLAUDE.md.
