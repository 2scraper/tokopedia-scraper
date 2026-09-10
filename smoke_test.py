#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
smoke_test.py
--------------
Zero-network, zero-browser sanity check for tokopedia-scraper.

Run this FIRST, before touching a real browser or tokopedia.com, to confirm
the parsing, the output contract, the page-state policy and the shared engine
decisions still hold:

    python3 smoke_test.py        # exits non-zero if anything failed

One file of plain functions with inline fixtures — no pytest, no conftest, no
fixtures directory. `tests/test_smoke.py` wraps this as a single pytest test
so `pytest` works as an entry point without a second copy of the checks.

It must pass with NO engine library installed at all: every
`import playwright_scraper` / `puppeteer_scraper` / `selenium_scraper` is
guarded and the skip is recorded. CI's `engine-smoke` job installs each engine
in its own virtualenv and fails if the corresponding group reports a skip —
"skipped, engine absent" reads identically to a real import error, so the two
have to be told apart somewhere.

WHAT THE FIXTURES ARE
---------------------
Real captures, taken 2026-09-10 over the 2Captcha Scraping Browser API from an
Indonesian residential exit, trimmed to the grid container and whole tiles,
and then VERIFIED to parse identically to the untrimmed original — every
pinned field, every row, checked before they were committed.

Tiles are whole and each is here because it pins a specific behaviour, named
in a comment above it. `SEARCH` deliberately carries FOUR of the eight tiles
whose slug has no 19-digit tail, because that is the case a tail-derived `sku`
would silently null out on a tenth of every real run.

ASSERT VALUES, NOT COVERAGE. A column can be 100% populated and entirely
wrong: a sibling repo shipped a `review_count` of 445279961 on every row of
every mode because it stripped the digits out of an aria-label, while its
coverage check happily said 100%. So the checks below pin the expected price,
rating, sold count and title for named products.

The only thing changed inside a fixture is the CDN's expiring image signature
(`?lk3s=…&x-expires=…&x-signature=…`), replaced with `?SIGNATURE-SCRUBBED`.
It identifies no person and no session, but it is per-impression material a
fresh capture brings with it, and §10's rule is to scrub before committing
rather than to argue about which material is harmless. `image_url` is
therefore asserted by SHAPE, and every other field byte-for-byte.
`test_no_capture_leaks` guards the next capture with PATTERNS rather than
these literals.
"""

import io
import ast
import csv
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import threading
import builtins
from contextlib import redirect_stdout
from dataclasses import asdict as dataclasses_asdict, fields

import captcha_solver
from captcha_solver import (CaptchaChallenge, detect_recaptcha_v3,
                            reconcile_detections, _v2_task_for, _redact)
from diff_runs import diff_products
import env_config
import page_flow
from output_writer import (Product, save, finish_run, write_csv,
                           dedupe_by_key, dedupe_by_sku, run_meta,
                           ROW_CLASS_BY_MODE, UNIQUE_BY_SKU_MODES,
                           EXIT_BLOCKED, EXIT_NO_PRODUCTS, EXIT_PARTIAL,
                           EXIT_API_ERROR, COMPLETE_STOP_REASONS,
                           LIST_CSV_SEPARATOR)
import product_parser
from product_parser import (parse_products, parse_product_page, shop_metadata,
                            page_url, paginates_by_url, category_from_url,
                            listing_kind, site_host, is_supported_host,
                            host_currency, locale_of, shop_from_url,
                            slug_tail_id, LOCALE_CURRENCY, HOSTS, CURRENCY,
                            detect_page_state, detect_bot_challenge,
                            served_by_tokopedia, is_no_results,
                            page_number_from_url, total_results, total_pages,
                            search_header, sku_from_url, product_path,
                            unsupported_reason, strip_tracking, prices_in,
                            rating_in, sold_in, SELECTORS)
from proxy_pool import (ProxyPool, mask, to_playwright, split_credentials,
                        parse_proxy_line)

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

_failures = []


def check(label, condition):
    """Print and record one check. Returns the condition, so callers can
    accumulate with `ok &= check(...)`."""
    if condition:
        print("  PASS  %s" % label)
    else:
        print("  FAIL  %s" % label)
        _failures.append(label)
    return bool(condition)


def group(title):
    print("\n== %s" % title)


def _raises(fn):
    """True if `fn()` raises. Used where refusing is the correct behaviour."""
    try:
        fn()
    except Exception:
        return True
    return False


def page(*fragments):
    """Wrap fragments in a minimal document, as the engines hand it over.

    The asset-host reference is not decoration. Tokopedia sends an address it
    has scored NOTHING — no status code, no interstitial, no marker — so
    `detect_page_state` is inverted: it recognises a page the site really
    served by the site's OWN asset host and treats the absence of one as the
    block signal. A bare `<html><body>` wrapper would therefore classify
    every hand-built fixture below as "blocked".
    """
    return ('<html lang="id"><body>'
            '<img src="https://images.tokopedia.net/img/x.png"/>'
            '<link rel="stylesheet" '
            'href="https://assets.tokopedia-static.net/x.css">'
            '%s</body></html>' % "".join(fragments))

# ---------------------------------------------------------------------------
# FIXTURES
# ---------------------------------------------------------------------------
# Cut from the real captures in ../captures/, trimmed to the grid container
# and whole tiles, and verified to parse identically to the untrimmed
# original before being committed. See the module docstring for what is and
# is not scrubbed.
#
# SEARCH — 8 tiles from /search?st=product&q=kopi, chosen for what each pins:
#   zayn-snack-448      a 93.75% discount: strike price, badge, 100rb+ sold
#   bakulkopistoree     the "Hemat s.d 8% Pakai Bonus" promo line in the tile
#   kopi-tungku         a title containing a literal "100%" AND a badge
#   arutalacoffee       a slug ending in a short hex suffix, no 19-digit tail
#   nestle-indonesia    no tail at all
#   kapalapistore       no tail at all
#   wingsofficial       no tail at all
#   shopshabira         the only tile with ONE price and no badge
#
# CATEGORY — 5 tiles from /p/makanan-minuman/minuman/kopi-bubuk: a different
# markup (divProductWrapper inside a[data-testid=lnkProductContainer]), the
# "Tambah ke Wishlist" button label inside the anchor, and no rating, sold
# count or was-price on any of them.
#
# DETAIL — one /{shop}/{slug} page reduced to its price metas, its h1, its
# canonical (UTM tail and all) and the four Apollo nodes the parser reads.

SEARCH_FIXTURE = '<!doctype html><html lang="id"><head><title>Jual kopi | Tokopedia</title><link rel="stylesheet" href="https://assets.tokopedia-static.net/x.css"><link rel="preconnect" href="https://images.tokopedia.net"></head><body><div class="css-q72k9b" data-testid="dSRPSearchInfo"><span>Menampilkan 61 - 180 barang dari total  untuk </span><strong>"kopi"</strong></div><div data-testid="divSRPContentProducts" data-ssr="contentProductsSRPSSR"><a class="Ui5-B4CDAk4Cv-cjLm4o0g== XeGJAOdlJaxl4+UD3zEJLg==" data-theme="default" href="https://www.tokopedia.com/zayn-snack-448/kopi-hitam-bubuk-robusta-original-berat-1-kg-coffee-kualitas-premium-pahitnya-pas-cocok-untuk-kopi-susu-1731177319241910164?extParam=ivf%3Dfalse%26keyword%3Dkopi%26search_id%3D20260910092803B83AAC23B952872697TE%26src%3Dsearch"><div class="gG1uA844gIiB2+C3QWiaKA=="><div class="_2BUZ1gvKHeLMf12T+cRKig=="><div class="ODO+GeAyQ9sdtJOCc1-faA=="><div class="loWbMM9lKTafPiUjqt9UWA== responsive lKpZCmwW6pa5aDKlZEpG8A==" data-testid="imgLeg-c"><span class="AdDkPTZUkNLBgVUauBHDdQ== responsive" data-testid="imgLeg-sb" style="padding-top: 100%;"></span><img alt="product-image" class="uMlDjJuJiuswTkGYpYCBBA== wSt-NCwsL186UdS6D-IpAg==" crossorigin="anonymous" decoding="async" src="https://p16-images-sign-sg.tokopedia-static.net/tos-alisg-i-aphluv4xwc-sg/ed86f8de7b0643f7a572939c0d8de763~tplv-aphluv4xwc-white-pad-v1:200:200.webp?SIGNATURE-SCRUBBED&amp;ect=4g"/></div><div class="aCRo0qnx2GsSF-ZjudD6Kw=="><span class="_7UCYdN8MrOTwg0MKcGu8zg==" style="background: rgb(249, 77, 99); color: rgb(255, 255, 255);">94%</span><span class="eZJYyupiigJIsjSthitkBg==" style="background: rgb(179, 29, 64);"></span></div></div></div><div class="y-oybT3IAd310DVdH3OwVg=="><div class="SzILjt4fxHUFNVT48ZPhHA=="><span class="+tnoqZhn89+NHUA43BpiJg==">Kopi Hitam Bubuk Robusta Original Berat 1 Kg - Coffee Kualitas Premium - Pahitnya pas cocok untuk kopi Susu</span></div><div class="pBlp2erqYHW+p5z-rsznAA=="><div class="rJTRB7icxB2aB4uO48TY0Q==" style="background: rgb(255, 245, 246); border: 1px solid rgb(255, 178, 194); opacity: 1;"><img alt="Rp7.500" class="-ZC0w8XNeK8Zgh8i3v4zkg==" src="https://p16-images-comn-sg.tokopedia-static.net/tos-alisg-i-zr7vqa5nfb-sg/img/ic_promo-discount.png~tplv-zr7vqa5nfb-image.image"/><span class="YZHqvX+8TVU2YltRC9S+oA==" style="color: rgb(249, 77, 99);">Rp7.500</span></div><div class="e48Kml5BRW9dq8Mopwgv7w=="><span class="hC1B8wTAoPszbEZj80w6Qw==">Rp120.000</span></div></div><div class="IuuhpNDMo+epg35dWPvTPA=="><span class="HCDPz5z44v-6ZX0vHDLMwQ==" style="color: rgb(255, 127, 23);">Bisa COD</span></div><div class="c7W9YYbRQuC29+GfsfRTEA=="><div class="_8BRsFZmjhjt-Zm-1rWcg2w=="><div class="pVUAhlsLSAwMvawIXOQ+fg=="><img alt="rating" class="+gFwr+QKBmB9gJvF845AkA==" src="https://lf-web-assets.tokopedia-static.net/obj/tokopedia-web-sg/zeus_v2/2e112467.svg"/></div><span class="_2NfJxPu4JC-55aCJ8bEsyw==">4.7</span></div><span class="tI9OTJG54HO9rEV+sMvRrw=="></span><span class="u6SfjDD2WiBlNW7zHmzRhQ==">100rb+ terjual</span></div><div class="ljZNQLe6R-7wAWexijt7lA=="><div class="NIOOv+ZjkRVGAkcL5Xf4Xw=="><img alt="shop badge" class="_63XCSQLAF1hJMidX4AzSdQ==" src="https://p16-images-comn-sg.tokopedia-static.net/tos-alisg-i-zr7vqa5nfb-sg/img/goldmerchant/pm_activation/badge/Power%20Merchant%20Pro.png~tplv-zr7vqa5nfb-image.image"/></div><div class="_1yoE8Ml3qwvn-r+EZ5hlbA=="><span class="si3CNdiG8AR0EaXvf6bFbQ== gxi+fsEljOjqhjSKqjE+sw== flip">Zayn Snack</span><span class="gxi+fsEljOjqhjSKqjE+sw== flip">Kab. Magetan</span></div></div><div class="_7RLZ8SfSf2-4gSgj-c5VEQ=="><button class="bJ+-0sJibWmMqDYLiidq+Q==" type="button"><img alt="three dots" class="bbUkCNRkldSGoN+pt0TdOA==" src="https://lf-web-assets.tokopedia-static.net/obj/tokopedia-web-sg/zeus_v2/ae78c469.svg"/></button></div></div></div><div></div></a>\n<a class="Ui5-B4CDAk4Cv-cjLm4o0g== XeGJAOdlJaxl4+UD3zEJLg==" data-theme="default" href="https://www.tokopedia.com/bakulkopistoree/promo-1kg-kopi-bubuk-hitam-robusta-mantap-coffee-1730878203764639452?extParam=ivf%3Dfalse%26keyword%3Dkopi%26search_id%3D20260910092803B83AAC23B952872697TE%26src%3Dsearch"><div class="gG1uA844gIiB2+C3QWiaKA=="><div class="_2BUZ1gvKHeLMf12T+cRKig=="><div class="ODO+GeAyQ9sdtJOCc1-faA=="><div class="loWbMM9lKTafPiUjqt9UWA== responsive lKpZCmwW6pa5aDKlZEpG8A==" data-testid="imgLeg-c"><span class="AdDkPTZUkNLBgVUauBHDdQ== responsive" data-testid="imgLeg-sb" style="padding-top: 100%;"></span><img alt="product-image" class="uMlDjJuJiuswTkGYpYCBBA== wSt-NCwsL186UdS6D-IpAg==" crossorigin="anonymous" decoding="async" src="https://p16-images-sign-sg.tokopedia-static.net/tos-alisg-i-aphluv4xwc-sg/5e6f5440ca314a4ab4d9eefc90332fb6~tplv-aphluv4xwc-white-pad-v1:200:200.webp?SIGNATURE-SCRUBBED&amp;ect=4g"/></div><div class="aCRo0qnx2GsSF-ZjudD6Kw=="><span class="_7UCYdN8MrOTwg0MKcGu8zg==" style="background: rgb(249, 77, 99); color: rgb(255, 255, 255);">40%</span><span class="eZJYyupiigJIsjSthitkBg==" style="background: rgb(179, 29, 64);"></span></div></div></div><div class="y-oybT3IAd310DVdH3OwVg=="><div class="SzILjt4fxHUFNVT48ZPhHA=="><span class="+tnoqZhn89+NHUA43BpiJg==">PROMO!!! 1kg Kopi Bubuk Hitam Robusta / Mantap Coffee</span></div><div class="pBlp2erqYHW+p5z-rsznAA=="><div class="urMOIDHH7I0Iy1Dv2oFaNw== HJhoi0tEIlowsgSNDNWVXg==">Rp11.900</div><div class="e48Kml5BRW9dq8Mopwgv7w=="><span class="hC1B8wTAoPszbEZj80w6Qw==">Rp19.999</span></div></div><div class="IuuhpNDMo+epg35dWPvTPA=="><span class="HCDPz5z44v-6ZX0vHDLMwQ==" style="color: rgb(255, 127, 23);">Hemat s.d  8% Pakai Bonus</span></div><div class="c7W9YYbRQuC29+GfsfRTEA=="><div class="_8BRsFZmjhjt-Zm-1rWcg2w=="><div class="pVUAhlsLSAwMvawIXOQ+fg=="><img alt="rating" class="+gFwr+QKBmB9gJvF845AkA==" src="https://lf-web-assets.tokopedia-static.net/obj/tokopedia-web-sg/zeus_v2/2e112467.svg"/></div><span class="_2NfJxPu4JC-55aCJ8bEsyw==">4.7</span></div><span class="tI9OTJG54HO9rEV+sMvRrw=="></span><span class="u6SfjDD2WiBlNW7zHmzRhQ==">50rb+ terjual</span></div><div class="ljZNQLe6R-7wAWexijt7lA=="><div class="_1yoE8Ml3qwvn-r+EZ5hlbA=="><span class="si3CNdiG8AR0EaXvf6bFbQ== gxi+fsEljOjqhjSKqjE+sw== flip">BakulKopiStoree</span><span class="gxi+fsEljOjqhjSKqjE+sw== flip">Surabaya</span></div></div><div class="_7RLZ8SfSf2-4gSgj-c5VEQ=="><button class="bJ+-0sJibWmMqDYLiidq+Q==" type="button"><img alt="three dots" class="bbUkCNRkldSGoN+pt0TdOA==" src="https://lf-web-assets.tokopedia-static.net/obj/tokopedia-web-sg/zeus_v2/ae78c469.svg"/></button></div></div></div><div></div></a>\n<a class="Ui5-B4CDAk4Cv-cjLm4o0g== XeGJAOdlJaxl4+UD3zEJLg==" data-theme="default" href="https://www.tokopedia.com/kopi-tungku/kopi-bubuk-hitam-robusta-original-berat-1kg-kualitas-premium-biji-berkualitas-100-higienis-diolah-secara-tradisional-1733227682292597917?extParam=ivf%3Dfalse%26keyword%3Dkopi%26search_id%3D20260910092803B83AAC23B952872697TE%26src%3Dsearch"><div class="gG1uA844gIiB2+C3QWiaKA=="><div class="_2BUZ1gvKHeLMf12T+cRKig=="><div class="ODO+GeAyQ9sdtJOCc1-faA=="><div class="loWbMM9lKTafPiUjqt9UWA== responsive lKpZCmwW6pa5aDKlZEpG8A==" data-testid="imgLeg-c"><span class="AdDkPTZUkNLBgVUauBHDdQ== responsive" data-testid="imgLeg-sb" style="padding-top: 100%;"></span><img alt="product-image" class="uMlDjJuJiuswTkGYpYCBBA== wSt-NCwsL186UdS6D-IpAg==" crossorigin="anonymous" decoding="async" src="https://p16-images-sign-sg.tokopedia-static.net/tos-alisg-i-aphluv4xwc-sg/2774d991f6a7464f8a9bcf6d02db0ad2~tplv-aphluv4xwc-white-pad-v1:200:200.webp?SIGNATURE-SCRUBBED&amp;ect=4g"/></div><div class="aCRo0qnx2GsSF-ZjudD6Kw=="><span class="_7UCYdN8MrOTwg0MKcGu8zg==" style="background: rgb(249, 77, 99); color: rgb(255, 255, 255);">97%</span><span class="eZJYyupiigJIsjSthitkBg==" style="background: rgb(179, 29, 64);"></span></div></div></div><div class="y-oybT3IAd310DVdH3OwVg=="><div class="SzILjt4fxHUFNVT48ZPhHA=="><span class="+tnoqZhn89+NHUA43BpiJg==">Kopi Bubuk Hitam Robusta Original Berat 1kg Kualitas Premium Biji Berkualitas 100% Higienis Diolah Secara Tradisional</span></div><div class="pBlp2erqYHW+p5z-rsznAA=="><div class="rJTRB7icxB2aB4uO48TY0Q==" style="background: rgb(255, 245, 246); border: 1px solid rgb(255, 178, 194); opacity: 1;"><img alt="Rp3.653" class="-ZC0w8XNeK8Zgh8i3v4zkg==" src="https://p16-images-comn-sg.tokopedia-static.net/tos-alisg-i-zr7vqa5nfb-sg/img/ic_promo-discount.png~tplv-zr7vqa5nfb-image.image"/><span class="YZHqvX+8TVU2YltRC9S+oA==" style="color: rgb(249, 77, 99);">Rp3.653</span></div><div class="e48Kml5BRW9dq8Mopwgv7w=="><span class="hC1B8wTAoPszbEZj80w6Qw==">Rp117.000</span></div></div><div class="IuuhpNDMo+epg35dWPvTPA=="><span class="HCDPz5z44v-6ZX0vHDLMwQ==" style="color: rgb(255, 127, 23);">Hemat s.d  8% Pakai Bonus</span></div><div class="c7W9YYbRQuC29+GfsfRTEA=="><div class="_8BRsFZmjhjt-Zm-1rWcg2w=="><div class="pVUAhlsLSAwMvawIXOQ+fg=="><img alt="rating" class="+gFwr+QKBmB9gJvF845AkA==" src="https://lf-web-assets.tokopedia-static.net/obj/tokopedia-web-sg/zeus_v2/2e112467.svg"/></div><span class="_2NfJxPu4JC-55aCJ8bEsyw==">4.8</span></div><span class="tI9OTJG54HO9rEV+sMvRrw=="></span><span class="u6SfjDD2WiBlNW7zHmzRhQ==">40+ terjual</span></div><div class="ljZNQLe6R-7wAWexijt7lA=="><div class="_1yoE8Ml3qwvn-r+EZ5hlbA=="><span class="si3CNdiG8AR0EaXvf6bFbQ== gxi+fsEljOjqhjSKqjE+sw== flip">KOPI TUNGKU</span><span class="gxi+fsEljOjqhjSKqjE+sw== flip">Kab. Kediri</span></div></div><div class="_7RLZ8SfSf2-4gSgj-c5VEQ=="><button class="bJ+-0sJibWmMqDYLiidq+Q==" type="button"><img alt="three dots" class="bbUkCNRkldSGoN+pt0TdOA==" src="https://lf-web-assets.tokopedia-static.net/obj/tokopedia-web-sg/zeus_v2/ae78c469.svg"/></button></div></div></div><div></div></a>\n<a class="Ui5-B4CDAk4Cv-cjLm4o0g== XeGJAOdlJaxl4+UD3zEJLg==" data-theme="default" href="https://www.tokopedia.com/arutalacoffee/arutala-kopi-arabika-gayo-arabica-coffee-200-gram-biji-c69d?extParam=ivf%3Dtrue%26keyword%3Dkopi%26search_id%3D20260910092803B83AAC23B952872697TE%26src%3Dsearch"><div class="gG1uA844gIiB2+C3QWiaKA=="><div class="_2BUZ1gvKHeLMf12T+cRKig=="><div class="ODO+GeAyQ9sdtJOCc1-faA=="><div class="loWbMM9lKTafPiUjqt9UWA== responsive lKpZCmwW6pa5aDKlZEpG8A==" data-testid="imgLeg-c"><span class="AdDkPTZUkNLBgVUauBHDdQ== responsive" data-testid="imgLeg-sb" style="padding-top: 100%;"></span><img alt="product-image" class="uMlDjJuJiuswTkGYpYCBBA== wSt-NCwsL186UdS6D-IpAg==" crossorigin="anonymous" decoding="async" src="https://p16-images-sign-sg.tokopedia-static.net/tos-alisg-i-aphluv4xwc-sg/5de05935e96740d893d872a53986bcfc~tplv-aphluv4xwc-white-pad-v1:200:200.webp?SIGNATURE-SCRUBBED&amp;ect=4g"/></div><div class="aCRo0qnx2GsSF-ZjudD6Kw=="><span class="_7UCYdN8MrOTwg0MKcGu8zg==" style="background: rgb(249, 77, 99); color: rgb(255, 255, 255);">69%</span><span class="eZJYyupiigJIsjSthitkBg==" style="background: rgb(179, 29, 64);"></span></div><div class="vUM1Mtt5FD5f2ad+VtC30Q=="><div class="rWbbZsc94CZ-FK2s5cTbzw=="><img alt="video sneakpeek" class="Z1iNQj-E+naR9TsjifaxZw==" src="https://lf-web-assets.tokopedia-static.net/obj/tokopedia-web-sg/zeus_v2/9212ba62.svg"/></div></div><div class="Vb-1u63D1aLQI1KGQraT0g=="><img alt="Beli Lokal" class="HfQw3mC2iBQQM8Ha9S0kjw== aKzJOAwYnwTN9NKZLa51bg==" src="https://p16-images-comn-sg.tokopedia-static.net/tos-alisg-i-zr7vqa5nfb-sg/img/vMWOpN/2025/10/16/1e33b6b7-d4bb-4aed-a1af-ebb46043c369.png~tplv-zr7vqa5nfb-resize-jpeg:700:0.png"/></div></div></div><div class="y-oybT3IAd310DVdH3OwVg=="><div class="SzILjt4fxHUFNVT48ZPhHA=="><span class="+tnoqZhn89+NHUA43BpiJg==">ARUTALA Kopi Arabika Gayo Arabica Coffee 200 gram</span></div><div class="pBlp2erqYHW+p5z-rsznAA=="><div class="rJTRB7icxB2aB4uO48TY0Q==" style="background: rgb(255, 245, 246); border: 1px solid rgb(255, 178, 194); opacity: 1;"><img alt="Rp39.900" class="-ZC0w8XNeK8Zgh8i3v4zkg==" src="https://p16-images-comn-sg.tokopedia-static.net/tos-alisg-i-zr7vqa5nfb-sg/img/ic_promo-discount.png~tplv-zr7vqa5nfb-image.image"/><span class="YZHqvX+8TVU2YltRC9S+oA==" style="color: rgb(249, 77, 99);">Rp39.900</span></div><div class="e48Kml5BRW9dq8Mopwgv7w=="><span class="hC1B8wTAoPszbEZj80w6Qw==">Rp127.000</span></div></div><div class="IuuhpNDMo+epg35dWPvTPA=="><span class="HCDPz5z44v-6ZX0vHDLMwQ==" style="color: rgb(255, 127, 23);">Hemat s.d  8% Pakai Bonus</span></div><div class="c7W9YYbRQuC29+GfsfRTEA=="><div class="_8BRsFZmjhjt-Zm-1rWcg2w=="><div class="pVUAhlsLSAwMvawIXOQ+fg=="><img alt="rating" class="+gFwr+QKBmB9gJvF845AkA==" src="https://lf-web-assets.tokopedia-static.net/obj/tokopedia-web-sg/zeus_v2/2e112467.svg"/></div><span class="_2NfJxPu4JC-55aCJ8bEsyw==">4.9</span></div><span class="tI9OTJG54HO9rEV+sMvRrw=="></span><span class="u6SfjDD2WiBlNW7zHmzRhQ==">10rb+ terjual</span></div><div class="ljZNQLe6R-7wAWexijt7lA=="><div class="NIOOv+ZjkRVGAkcL5Xf4Xw=="><img alt="shop badge" class="_63XCSQLAF1hJMidX4AzSdQ==" src="https://p16-images-comn-sg.tokopedia-static.net/tos-alisg-i-zr7vqa5nfb-sg/img/official_store/badge_os.png~tplv-zr7vqa5nfb-image.image"/></div><div class="_1yoE8Ml3qwvn-r+EZ5hlbA=="><span class="si3CNdiG8AR0EaXvf6bFbQ== gxi+fsEljOjqhjSKqjE+sw== flip">Arutala Coffee</span><span class="gxi+fsEljOjqhjSKqjE+sw== flip">Tangerang</span></div></div><div class="_7RLZ8SfSf2-4gSgj-c5VEQ=="><button class="bJ+-0sJibWmMqDYLiidq+Q==" type="button"><img alt="three dots" class="bbUkCNRkldSGoN+pt0TdOA==" src="https://lf-web-assets.tokopedia-static.net/obj/tokopedia-web-sg/zeus_v2/ae78c469.svg"/></button></div></div></div><div></div></a>\n<a class="Ui5-B4CDAk4Cv-cjLm4o0g== XeGJAOdlJaxl4+UD3zEJLg==" data-theme="default" href="https://www.tokopedia.com/nestle-indonesia/nescafe-gold-kopi-instan-kopi-hitam-50g-jar?extParam=ivf%3Dfalse%26keyword%3Dkopi%26search_id%3D20260910092803B83AAC23B952872697TE%26src%3Dsearch"><div class="gG1uA844gIiB2+C3QWiaKA=="><div class="_2BUZ1gvKHeLMf12T+cRKig=="><div class="ODO+GeAyQ9sdtJOCc1-faA=="><div class="loWbMM9lKTafPiUjqt9UWA== responsive lKpZCmwW6pa5aDKlZEpG8A==" data-testid="imgLeg-c"><span class="AdDkPTZUkNLBgVUauBHDdQ== responsive" data-testid="imgLeg-sb" style="padding-top: 100%;"></span><img alt="product-image" class="uMlDjJuJiuswTkGYpYCBBA== wSt-NCwsL186UdS6D-IpAg==" crossorigin="anonymous" decoding="async" src="https://p16-images-common-sign-sg.tokopedia-static.net/tos-maliva-i-o3syd03w52-us/57be73ada2b347d5b7ac3f82f75b39bb~tplv-o3syd03w52-resize-jpeg:200:200.webp?SIGNATURE-SCRUBBED&amp;ect=4g"/></div><div class="aCRo0qnx2GsSF-ZjudD6Kw=="><span class="_7UCYdN8MrOTwg0MKcGu8zg==" style="background: rgb(249, 77, 99); color: rgb(255, 255, 255);">65%</span><span class="eZJYyupiigJIsjSthitkBg==" style="background: rgb(179, 29, 64);"></span></div></div></div><div class="y-oybT3IAd310DVdH3OwVg=="><div class="SzILjt4fxHUFNVT48ZPhHA=="><span class="+tnoqZhn89+NHUA43BpiJg==">NESCAFE GOLD Kopi Instan Kopi Hitam Jar 50g</span></div><div class="pBlp2erqYHW+p5z-rsznAA=="><div class="rJTRB7icxB2aB4uO48TY0Q==" style="background: rgb(255, 245, 246); border: 1px solid rgb(255, 178, 194); opacity: 1;"><img alt="Rp45.610" class="-ZC0w8XNeK8Zgh8i3v4zkg==" src="https://p16-images-comn-sg.tokopedia-static.net/tos-alisg-i-zr7vqa5nfb-sg/img/ic_promo-discount.png~tplv-zr7vqa5nfb-image.image"/><span class="YZHqvX+8TVU2YltRC9S+oA==" style="color: rgb(249, 77, 99);">Rp45.610</span></div><div class="e48Kml5BRW9dq8Mopwgv7w=="><span class="hC1B8wTAoPszbEZj80w6Qw==">Rp129.530</span></div></div><div class="IuuhpNDMo+epg35dWPvTPA=="><span class="HCDPz5z44v-6ZX0vHDLMwQ==" style="color: rgb(255, 127, 23);">Hemat s.d  8% Pakai Bonus</span></div><div class="c7W9YYbRQuC29+GfsfRTEA=="><div class="_8BRsFZmjhjt-Zm-1rWcg2w=="><div class="pVUAhlsLSAwMvawIXOQ+fg=="><img alt="rating" class="+gFwr+QKBmB9gJvF845AkA==" src="https://lf-web-assets.tokopedia-static.net/obj/tokopedia-web-sg/zeus_v2/2e112467.svg"/></div><span class="_2NfJxPu4JC-55aCJ8bEsyw==">5.0</span></div><span class="tI9OTJG54HO9rEV+sMvRrw=="></span><span class="u6SfjDD2WiBlNW7zHmzRhQ==">7rb+ terjual</span></div><div class="ljZNQLe6R-7wAWexijt7lA=="><div class="NIOOv+ZjkRVGAkcL5Xf4Xw=="><img alt="shop badge" class="_63XCSQLAF1hJMidX4AzSdQ==" src="https://p16-images-comn-sg.tokopedia-static.net/tos-alisg-i-zr7vqa5nfb-sg/img/official_store/badge_os.png~tplv-zr7vqa5nfb-image.image"/></div><div class="_1yoE8Ml3qwvn-r+EZ5hlbA=="><span class="si3CNdiG8AR0EaXvf6bFbQ== gxi+fsEljOjqhjSKqjE+sw== flip">Nestle Indonesia</span><span class="gxi+fsEljOjqhjSKqjE+sw== flip">Tangerang</span></div></div><div class="_7RLZ8SfSf2-4gSgj-c5VEQ=="><button class="bJ+-0sJibWmMqDYLiidq+Q==" type="button"><img alt="three dots" class="bbUkCNRkldSGoN+pt0TdOA==" src="https://lf-web-assets.tokopedia-static.net/obj/tokopedia-web-sg/zeus_v2/ae78c469.svg"/></button></div></div></div><div></div></a>\n<a class="Ui5-B4CDAk4Cv-cjLm4o0g== XeGJAOdlJaxl4+UD3zEJLg==" data-theme="default" href="https://www.tokopedia.com/kapalapistore/kapal-api-special-mix-1-bag-20-x-23-gr-minuman-kopi-bubuk?extParam=ivf%3Dfalse%26keyword%3Dkopi%26search_id%3D20260910092803B83AAC23B952872697TE%26src%3Dsearch"><div class="gG1uA844gIiB2+C3QWiaKA=="><div class="_2BUZ1gvKHeLMf12T+cRKig=="><div class="ODO+GeAyQ9sdtJOCc1-faA=="><div class="loWbMM9lKTafPiUjqt9UWA== responsive lKpZCmwW6pa5aDKlZEpG8A==" data-testid="imgLeg-c"><span class="AdDkPTZUkNLBgVUauBHDdQ== responsive" data-testid="imgLeg-sb" style="padding-top: 100%;"></span><img alt="product-image" class="uMlDjJuJiuswTkGYpYCBBA== wSt-NCwsL186UdS6D-IpAg==" crossorigin="anonymous" decoding="async" src="https://p16-images-sign-sg.tokopedia-static.net/tos-alisg-i-aphluv4xwc-sg/img/VqbcmM/2025/3/18/732f4637-fc0a-494e-843a-5582852c29f1.jpg~tplv-aphluv4xwc-white-pad-v1:200:200.webp?SIGNATURE-SCRUBBED&amp;ect=4g"/></div><div class="aCRo0qnx2GsSF-ZjudD6Kw=="><span class="_7UCYdN8MrOTwg0MKcGu8zg==" style="background: rgb(249, 77, 99); color: rgb(255, 255, 255);">83%</span><span class="eZJYyupiigJIsjSthitkBg==" style="background: rgb(179, 29, 64);"></span></div><div class="Vb-1u63D1aLQI1KGQraT0g=="><img alt="Beli Lokal" class="HfQw3mC2iBQQM8Ha9S0kjw== aKzJOAwYnwTN9NKZLa51bg==" src="https://p16-images-comn-sg.tokopedia-static.net/tos-alisg-i-zr7vqa5nfb-sg/img/vMWOpN/2025/10/16/1e33b6b7-d4bb-4aed-a1af-ebb46043c369.png~tplv-zr7vqa5nfb-resize-jpeg:700:0.png"/></div></div></div><div class="y-oybT3IAd310DVdH3OwVg=="><div class="SzILjt4fxHUFNVT48ZPhHA=="><span class="+tnoqZhn89+NHUA43BpiJg==">KAPAL API Special Mix 1 Bag (20 x 23 gr) - Minuman Kopi Bubuk</span></div><div class="pBlp2erqYHW+p5z-rsznAA=="><div class="rJTRB7icxB2aB4uO48TY0Q==" style="background: rgb(255, 245, 246); border: 1px solid rgb(255, 178, 194); opacity: 1;"><img alt="Rp8.600" class="-ZC0w8XNeK8Zgh8i3v4zkg==" src="https://p16-images-comn-sg.tokopedia-static.net/tos-alisg-i-zr7vqa5nfb-sg/img/ic_promo-discount.png~tplv-zr7vqa5nfb-image.image"/><span class="YZHqvX+8TVU2YltRC9S+oA==" style="color: rgb(249, 77, 99);">Rp8.600</span></div><div class="e48Kml5BRW9dq8Mopwgv7w=="><span class="hC1B8wTAoPszbEZj80w6Qw==">Rp52.100</span></div></div><div class="c7W9YYbRQuC29+GfsfRTEA=="><div class="_8BRsFZmjhjt-Zm-1rWcg2w=="><div class="pVUAhlsLSAwMvawIXOQ+fg=="><img alt="rating" class="+gFwr+QKBmB9gJvF845AkA==" src="https://lf-web-assets.tokopedia-static.net/obj/tokopedia-web-sg/zeus_v2/2e112467.svg"/></div><span class="_2NfJxPu4JC-55aCJ8bEsyw==">5.0</span></div><span class="tI9OTJG54HO9rEV+sMvRrw=="></span><span class="u6SfjDD2WiBlNW7zHmzRhQ==">10rb+ terjual</span></div><div class="ljZNQLe6R-7wAWexijt7lA=="><div class="NIOOv+ZjkRVGAkcL5Xf4Xw=="><img alt="shop badge" class="_63XCSQLAF1hJMidX4AzSdQ==" src="https://p16-images-comn-sg.tokopedia-static.net/tos-alisg-i-zr7vqa5nfb-sg/img/official_store/badge_os.png~tplv-zr7vqa5nfb-image.image"/></div><div class="_1yoE8Ml3qwvn-r+EZ5hlbA=="><span class="si3CNdiG8AR0EaXvf6bFbQ== gxi+fsEljOjqhjSKqjE+sw== flip">Kapal Api Store</span><span class="gxi+fsEljOjqhjSKqjE+sw== flip">Jakarta Barat</span></div></div><div class="_7RLZ8SfSf2-4gSgj-c5VEQ=="><button class="bJ+-0sJibWmMqDYLiidq+Q==" type="button"><img alt="three dots" class="bbUkCNRkldSGoN+pt0TdOA==" src="https://lf-web-assets.tokopedia-static.net/obj/tokopedia-web-sg/zeus_v2/ae78c469.svg"/></button></div></div></div><div></div></a>\n<a class="Ui5-B4CDAk4Cv-cjLm4o0g== XeGJAOdlJaxl4+UD3zEJLg==" data-theme="default" href="https://www.tokopedia.com/wingsofficial/top-coffee-kopi-instan-gula-aren-22-gr-x-6-pcs?extParam=ivf%3Dfalse%26keyword%3Dkopi%26search_id%3D20260910092803B83AAC23B952872697TE%26src%3Dsearch%26whid%3D7386608"><div class="gG1uA844gIiB2+C3QWiaKA=="><div class="_2BUZ1gvKHeLMf12T+cRKig=="><div class="ODO+GeAyQ9sdtJOCc1-faA=="><div class="loWbMM9lKTafPiUjqt9UWA== responsive lKpZCmwW6pa5aDKlZEpG8A==" data-testid="imgLeg-c"><span class="AdDkPTZUkNLBgVUauBHDdQ== responsive" data-testid="imgLeg-sb" style="padding-top: 100%;"></span><img alt="product-image" class="uMlDjJuJiuswTkGYpYCBBA== wSt-NCwsL186UdS6D-IpAg==" crossorigin="anonymous" decoding="async" src="https://p16-images-sign-sg.tokopedia-static.net/tos-alisg-i-aphluv4xwc-sg/5be95e4684af4df4967f2cbaceca82d5~tplv-aphluv4xwc-white-pad-v1:200:200.webp?SIGNATURE-SCRUBBED&amp;ect=4g"/></div><div class="aCRo0qnx2GsSF-ZjudD6Kw=="><span class="_7UCYdN8MrOTwg0MKcGu8zg==" style="background: rgb(249, 77, 99); color: rgb(255, 255, 255);">27%</span><span class="eZJYyupiigJIsjSthitkBg==" style="background: rgb(179, 29, 64);"></span></div><div class="Vb-1u63D1aLQI1KGQraT0g=="><img alt="Beli Lokal" class="HfQw3mC2iBQQM8Ha9S0kjw== aKzJOAwYnwTN9NKZLa51bg==" src="https://p16-images-comn-sg.tokopedia-static.net/tos-alisg-i-zr7vqa5nfb-sg/img/vMWOpN/2025/10/16/1e33b6b7-d4bb-4aed-a1af-ebb46043c369.png~tplv-zr7vqa5nfb-resize-jpeg:700:0.png"/></div></div></div><div class="y-oybT3IAd310DVdH3OwVg=="><div class="SzILjt4fxHUFNVT48ZPhHA=="><span class="+tnoqZhn89+NHUA43BpiJg==">TOP Coffee Kopi Instan Gula Aren 22g isi 9pcs</span></div><div class="pBlp2erqYHW+p5z-rsznAA=="><div class="urMOIDHH7I0Iy1Dv2oFaNw== HJhoi0tEIlowsgSNDNWVXg==">Rp18.200</div><div class="e48Kml5BRW9dq8Mopwgv7w=="><span class="hC1B8wTAoPszbEZj80w6Qw==">Rp24.900</span></div></div><div class="IuuhpNDMo+epg35dWPvTPA=="><span class="HCDPz5z44v-6ZX0vHDLMwQ==" style="color: rgb(255, 127, 23);">Hemat s.d  8% Pakai Bonus</span></div><div class="c7W9YYbRQuC29+GfsfRTEA=="><div class="_8BRsFZmjhjt-Zm-1rWcg2w=="><div class="pVUAhlsLSAwMvawIXOQ+fg=="><img alt="rating" class="+gFwr+QKBmB9gJvF845AkA==" src="https://lf-web-assets.tokopedia-static.net/obj/tokopedia-web-sg/zeus_v2/2e112467.svg"/></div><span class="_2NfJxPu4JC-55aCJ8bEsyw==">5.0</span></div><span class="tI9OTJG54HO9rEV+sMvRrw=="></span><span class="u6SfjDD2WiBlNW7zHmzRhQ==">10rb+ terjual</span></div><div class="ljZNQLe6R-7wAWexijt7lA=="><div class="NIOOv+ZjkRVGAkcL5Xf4Xw=="><img alt="shop badge" class="_63XCSQLAF1hJMidX4AzSdQ==" src="https://p16-images-comn-sg.tokopedia-static.net/tos-alisg-i-zr7vqa5nfb-sg/img/official_store/badge_os.png~tplv-zr7vqa5nfb-image.image"/></div><div class="_1yoE8Ml3qwvn-r+EZ5hlbA=="><span class="si3CNdiG8AR0EaXvf6bFbQ== gxi+fsEljOjqhjSKqjE+sw== flip">Wings Indonesia</span><span class="gxi+fsEljOjqhjSKqjE+sw== flip">Kab. Sidoarjo</span></div></div><div class="_7RLZ8SfSf2-4gSgj-c5VEQ=="><button class="bJ+-0sJibWmMqDYLiidq+Q==" type="button"><img alt="three dots" class="bbUkCNRkldSGoN+pt0TdOA==" src="https://lf-web-assets.tokopedia-static.net/obj/tokopedia-web-sg/zeus_v2/ae78c469.svg"/></button></div></div></div><div></div></a>\n<a class="Ui5-B4CDAk4Cv-cjLm4o0g== XeGJAOdlJaxl4+UD3zEJLg==" data-theme="default" href="https://www.tokopedia.com/shopshabira/kopi-good-day-mocacinno-merah-exp-terbaru-coffee-3-in-1-1729604349080143827?extParam=ivf%3Dfalse%26keyword%3Dkopi%26search_id%3D20260910092803B83AAC23B952872697TE%26src%3Dsearch"><div class="gG1uA844gIiB2+C3QWiaKA=="><div class="_2BUZ1gvKHeLMf12T+cRKig=="><div class="ODO+GeAyQ9sdtJOCc1-faA=="><div class="loWbMM9lKTafPiUjqt9UWA== responsive lKpZCmwW6pa5aDKlZEpG8A==" data-testid="imgLeg-c"><span class="AdDkPTZUkNLBgVUauBHDdQ== responsive" data-testid="imgLeg-sb" style="padding-top: 100%;"></span><img alt="product-image" class="uMlDjJuJiuswTkGYpYCBBA== wSt-NCwsL186UdS6D-IpAg==" crossorigin="anonymous" decoding="async" src="https://p16-images-sign-sg.tokopedia-static.net/tos-alisg-i-aphluv4xwc-sg/e7c2886f31754084ba2fec036f7738d4~tplv-aphluv4xwc-white-pad-v1:200:200.webp?SIGNATURE-SCRUBBED&amp;ect=4g"/></div></div></div><div class="y-oybT3IAd310DVdH3OwVg=="><div class="SzILjt4fxHUFNVT48ZPhHA=="><span class="+tnoqZhn89+NHUA43BpiJg==">kopi good day mocacinno merah exp terbaru Coffee 3 in 1</span></div><div class="pBlp2erqYHW+p5z-rsznAA=="><div class="urMOIDHH7I0Iy1Dv2oFaNw==">Rp20.999</div></div><div class="IuuhpNDMo+epg35dWPvTPA=="><span class="HCDPz5z44v-6ZX0vHDLMwQ==" style="color: rgb(255, 127, 23);">Bisa COD</span></div><div class="c7W9YYbRQuC29+GfsfRTEA=="><div class="_8BRsFZmjhjt-Zm-1rWcg2w=="><div class="pVUAhlsLSAwMvawIXOQ+fg=="><img alt="rating" class="+gFwr+QKBmB9gJvF845AkA==" src="https://lf-web-assets.tokopedia-static.net/obj/tokopedia-web-sg/zeus_v2/2e112467.svg"/></div><span class="_2NfJxPu4JC-55aCJ8bEsyw==">5.0</span></div><span class="tI9OTJG54HO9rEV+sMvRrw=="></span><span class="u6SfjDD2WiBlNW7zHmzRhQ==">9rb+ terjual</span></div><div class="ljZNQLe6R-7wAWexijt7lA=="><div class="NIOOv+ZjkRVGAkcL5Xf4Xw=="><img alt="shop badge" class="_63XCSQLAF1hJMidX4AzSdQ==" src="https://p16-images-comn-sg.tokopedia-static.net/tos-alisg-i-zr7vqa5nfb-sg/img/goldmerchant/pm_activation/badge/Power%20Merchant%20Pro.png~tplv-zr7vqa5nfb-image.image"/></div><div class="_1yoE8Ml3qwvn-r+EZ5hlbA=="><span class="si3CNdiG8AR0EaXvf6bFbQ== gxi+fsEljOjqhjSKqjE+sw== flip">ShopShabira</span><span class="gxi+fsEljOjqhjSKqjE+sw== flip">Kab. Bekasi</span></div></div><div class="_7RLZ8SfSf2-4gSgj-c5VEQ=="><button class="bJ+-0sJibWmMqDYLiidq+Q==" type="button"><img alt="three dots" class="bbUkCNRkldSGoN+pt0TdOA==" src="https://lf-web-assets.tokopedia-static.net/obj/tokopedia-web-sg/zeus_v2/ae78c469.svg"/></button></div></div></div><div></div></a></div></body></html>'

CATEGORY_FIXTURE = '<!doctype html><html lang="id"><head><title>Kopi Bubuk Pilihan Terlengkap &amp; Produk Terbaru - Harga Terbaik | Tokopedia</title><link rel="stylesheet" href="https://assets.tokopedia-static.net/x.css"><link rel="preconnect" href="https://images.tokopedia.net"></head><body><div data-ssr="productsCategoryL2/L3SSR"><a class="css-54k5sq" data-testid="lnkProductContainer" href="https://www.tokopedia.com/delifru/premix-powder-coffee-jelly-arnav-1-kg-bubuk-premix-jeli-kopi-premium-1729950312039418010?extParam=ivf%3Dfalse%26search_id%3D202609100933515B8ADD2A5D98B12CA3T2"><div class="css-ds2sdp"><div class="wishlistButton"><div class="css-h7xut0 e1nksy3g0"><div class="css-1i26qzt"><span>Tambah ke Wishlist</span></div></div><span class="css-1gfa5on"><span class="css-gu7dmw"></span><span class="css-kzst4z"></span><div class="css-ckbit5" data-testid="btnWishlistClickable"></div></span></div></div><div class="css-16vw0vn" data-testid="divProductWrapper"><div class="css-79elbk"><div class="css-377m5r"><div class="css-b4oy9w"><img alt="Premix Powder Coffee Jelly Arnav 1 Kg - Bubuk Premix Jeli Kopi Premium" class="success fade" src="https://p19-images-common-sign-sg.tokopedia-static.net/tos-maliva-i-o3syd03w52-us/92cbb44b99284a9882d60f6cc0aada8a~tplv-o3syd03w52-resize-jpeg:200:200.jpeg?SIGNATURE-SCRUBBED" title=""/></div></div></div><div class="css-11s9vse"><span class="css-20kt3o">Premix Powder Coffee Jelly Arnav 1 Kg - Bubuk Premix Jeli Kopi Premium</span><div><div class="css-1m96yvy"><span class="css-o5uqvq">Rp109.435</span></div></div><div class="css-tpww51"><div class="css-1hy7m5k"><img alt="Delifru Utama Indonesia" src="https://p16-images-comn-sg.tokopedia-static.net/tos-alisg-i-zr7vqa5nfb-sg/img/official_store/badge_os.png~tplv-zr7vqa5nfb-image.image"/></div><div class="css-vbihp9"><span class="css-ywdpwd hidden disable-trans"></span><span class="css-ywdpwd disable-trans">Delifru Utama Indonesia</span></div></div></div></div></a>\n<a class="css-54k5sq" data-testid="lnkProductContainer" href="https://www.tokopedia.com/arutalacoffee/arutala-drip-filter-bag-coffee-arabika-bali-kintamani-5-drip-bag-1729558783519656210?extParam=ivf%3Dtrue%26search_id%3D202609100933515B8ADD2A5D98B12CA3T2"><div class="css-ds2sdp"><div class="wishlistButton"><div class="css-h7xut0 e1nksy3g0"><div class="css-1i26qzt"><span>Tambah ke Wishlist</span></div></div><span class="css-1gfa5on"><span class="css-gu7dmw"></span><span class="css-kzst4z"></span><div class="css-ckbit5" data-testid="btnWishlistClickable"></div></span></div></div><div class="css-16vw0vn" data-testid="divProductWrapper"><div class="css-79elbk"><div class="css-377m5r"><div class="css-b4oy9w"><img alt="Arutala Drip/Filter Bag coffee : Arabika Bali Kintamani - 5 Drip Bag" class="success fade" src="https://p16-images-sign-sg.tokopedia-static.net/tos-alisg-i-aphluv4xwc-sg/88ab2f05f9e24dfe8877fe7b3651fa9b~tplv-aphluv4xwc-white-pad-v1:200:200.jpeg?SIGNATURE-SCRUBBED" title=""/></div></div></div><div class="css-11s9vse"><span class="css-20kt3o">Arutala Drip/Filter Bag coffee : Arabika Bali Kintamani - 5 Drip Bag</span><div><div class="css-1m96yvy"><span class="css-o5uqvq">Rp9.900</span></div></div><div class="css-tpww51"><div class="css-1hy7m5k"><img alt="Arutala Coffee" src="https://p16-images-comn-sg.tokopedia-static.net/tos-alisg-i-zr7vqa5nfb-sg/img/official_store/badge_os.png~tplv-zr7vqa5nfb-image.image"/></div><div class="css-vbihp9"><span class="css-ywdpwd">Tangerang</span><span class="css-ywdpwd">Arutala Coffee</span></div></div></div></div></a>\n<a class="css-54k5sq" data-testid="lnkProductContainer" href="https://www.tokopedia.com/keraton-gallery-indonesia/kopi-vmax-rasa-coklat-nikmat-bikin-semangat-bpom-bisa-cod-1733275462096618970?extParam=ivf%3Dfalse%26search_id%3D202609100933515B8ADD2A5D98B12CA3T2"><div class="css-ds2sdp"><div class="wishlistButton"><div class="css-h7xut0 e1nksy3g0"><div class="css-1i26qzt"><span>Tambah ke Wishlist</span></div></div><span class="css-1gfa5on"><span class="css-gu7dmw"></span><span class="css-kzst4z"></span><div class="css-ckbit5" data-testid="btnWishlistClickable"></div></span></div></div><div class="css-16vw0vn" data-testid="divProductWrapper"><div class="css-79elbk"><div class="css-377m5r"><div class="css-b4oy9w"><img alt="Kopi Vmax Rasa Coklat Nikmat Bikin Semangat BPOM Bisa COD" class="success fade" src="https://p16-images-sign-sg.tokopedia-static.net/tos-alisg-i-aphluv4xwc-sg/33a168db35d245b6b8a00e89043a1826~tplv-aphluv4xwc-white-pad-v1:200:200.jpeg?SIGNATURE-SCRUBBED" title=""/></div></div></div><div class="css-11s9vse"><span class="css-20kt3o">Kopi Vmax Rasa Coklat Nikmat Bikin Semangat BPOM Bisa COD</span><div><div class="css-1m96yvy"><span class="css-o5uqvq">Rp100.000</span></div></div><div class="css-tpww51"><div class="css-1hy7m5k"><img alt="Keraton Gallery Indonesia" src="https://p16-images-comn-sg.tokopedia-static.net/tos-alisg-i-zr7vqa5nfb-sg/img/official_store/badge_os.png~tplv-zr7vqa5nfb-image.image"/></div><div class="css-vbihp9"><span class="css-ywdpwd">Jakarta Timur</span><span class="css-ywdpwd">Keraton Gallery Indonesia</span></div></div></div></div></a>\n<a class="css-54k5sq" data-testid="lnkProductContainer" href="https://www.tokopedia.com/luwakkoffie/kopi-luwak-white-koffie-original-bag-18x20gr-twin-pack?extParam=ivf%3Dfalse%26search_id%3D202609100933515B8ADD2A5D98B12CA3T2%26whid%3D21002572"><div class="css-ds2sdp"><div class="wishlistButton"><div class="css-h7xut0 e1nksy3g0"><div class="css-1i26qzt"><span>Tambah ke Wishlist</span></div></div><span class="css-1gfa5on"><span class="css-gu7dmw"></span><span class="css-kzst4z"></span><div class="css-ckbit5" data-testid="btnWishlistClickable"></div></span></div></div><div class="css-16vw0vn" data-testid="divProductWrapper"><div class="css-79elbk"><div class="css-377m5r"><div class="css-15r6kwb"><img alt="Kopi Luwak White Koffie Original Bag 18x20gr Twin Pack" class="fade" src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAHgAAAB4CAQAAACTbf5ZAAAAiklEQVR42u3PMQEAAAwCoNm/9Cr4Cw3IjYmwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsHDrASUpAHnJhbktAAAAAElFTkSuQmCC" title=""/></div></div></div><div class="css-11s9vse"><span class="css-20kt3o">Kopi Luwak White Koffie Original Bag 18x20gr Twin Pack</span><div><div class="css-1m96yvy"><span class="css-o5uqvq">Rp47.060</span></div></div><div class="css-tpww51"><div class="css-1hy7m5k"><img alt="Kopi Luwak Shop" src="https://p16-images-comn-sg.tokopedia-static.net/tos-alisg-i-zr7vqa5nfb-sg/img/official_store/badge_os.png~tplv-zr7vqa5nfb-image.image"/></div><div class="css-vbihp9"><span class="css-ywdpwd">Surabaya</span><span class="css-ywdpwd">Kopi Luwak Shop</span></div></div></div></div></a>\n<a class="css-54k5sq" data-testid="lnkProductContainer" href="https://www.tokopedia.com/luwakkoffie/kopi-luwak-white-koffie-collagen-bag-5x25gr-triple-pack?extParam=ivf%3Dfalse%26search_id%3D202609100933515B8ADD2A5D98B12CA3T2%26whid%3D18978536"><div class="css-ds2sdp"><div class="wishlistButton"><div class="css-h7xut0 e1nksy3g0"><div class="css-1i26qzt"><span>Tambah ke Wishlist</span></div></div><span class="css-1gfa5on"><span class="css-gu7dmw"></span><span class="css-kzst4z"></span><div class="css-ckbit5" data-testid="btnWishlistClickable"></div></span></div></div><div class="css-16vw0vn" data-testid="divProductWrapper"><div class="css-79elbk"><div class="css-377m5r"><div class="css-15r6kwb"><img alt="Kopi Luwak White Koffie Collagen Bag 5x25gr Triple Pack" class="fade" src="data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAHgAAAB4CAQAAACTbf5ZAAAAiklEQVR42u3PMQEAAAwCoNm/9Cr4Cw3IjYmwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsLCwsHDrASUpAHnJhbktAAAAAElFTkSuQmCC" title=""/></div></div></div><div class="css-11s9vse"><span class="css-20kt3o">Kopi Luwak White Koffie Collagen Bag 5x25gr Triple Pack</span><div><div class="css-1m96yvy"><span class="css-o5uqvq">Rp10.760</span></div></div><div class="css-tpww51"><div class="css-1hy7m5k"><img alt="Kopi Luwak Shop" src="https://p16-images-comn-sg.tokopedia-static.net/tos-alisg-i-zr7vqa5nfb-sg/img/official_store/badge_os.png~tplv-zr7vqa5nfb-image.image"/></div><div class="css-vbihp9"><span class="css-ywdpwd">Surabaya</span><span class="css-ywdpwd">Kopi Luwak Shop</span></div></div></div></div></a></div></body></html>'

DETAIL_FIXTURE = '<!doctype html><html lang="id"><head><title>Promo Kopi Hitam Bubuk Robusta Original Berat 1 Kg - Coffee Kualitas Premium - Pahitnya pas cocok untuk kopi Susu - Kab. Magetan - Zayn Snack | Tokopedia</title><meta content="7500" data-rh="true" property="product:price:amount"/><meta content="Rp" data-rh="true" property="product:price:currency"/><meta content="Promo Kopi Hitam Bubuk Robusta Original Berat 1 Kg - Coffee Kualitas Premium - Pahitnya pas cocok untuk kopi Susu di Zayn Snack | Tokopedia" data-rh="true" property="og:title"/><link data-rh="true" href="https://www.tokopedia.com/zayn-snack-448/kopi-hitam-bubuk-robusta-original-berat-1-kg-coffee-kualitas-premium-pahitnya-pas-cocok-untuk-kopi-susu-1731177319241910164?utm_source=google&amp;utm_medium=organic&amp;utm_campaign=pdp" rel="canonical"/><link rel="stylesheet" href="https://assets.tokopedia-static.net/x.css"><link rel="preconnect" href="https://images.tokopedia.net"></head><body><span data-testid="lblPDPProductNameJumper">Kopi Hitam Bubuk Robusta Original Berat 1 Kg - Coffee Kualitas Premium - Pahitnya pas cocok untuk kopi Susu</span><script type="text/javascript">window.__isBot="false";window.__cache={"pdpBasicInfo103490518624": {"alias": "kopi-hitam-bubuk-robusta-original-berat-1-kg-coffee-kualitas-premium-pahitnya-pas-cocok-untuk-kopi-susu-1731177319241910164", "createdAt": "2025-04-16T17:41:57+07:00", "isQA": false, "productID": "103490518624", "shopID": "7494836721885547412", "shopName": "Zayn Snack", "minOrder": 1, "maxOrder": 6795, "weight": 1, "weightUnit": "KILOGRAM", "condition": "NEW", "status": "ACTIVE", "url": "https://www.tokopedia.com/zayn-snack-448/kopi-hitam-bubuk-robusta-original-berat-1-kg-coffee-kualitas-premium-pahitnya-pas-cocok-untuk-kopi-susu-1731177319241910164", "needPrescription": false, "catalogID": "0", "isLeasing": false, "isBlacklisted": false, "isTokoNow": false, "defaultMediaURL": "https://p19-images-sign-sg.tokopedia-static.net/tos-alisg-i-aphluv4xwc-sg/ed86f8de7b0643f7a572939c0d8de763~tplv-aphluv4xwc-resize-jpeg:700:0.jpeg?SIGNATURE-SCRUBBED", "menu": {"type": "id", "generated": false, "id": "pdpMenu0", "typename": "pdpMenu"}, "blacklistMessage": {"type": "id", "generated": true, "id": "$pdpBasicInfo103490518624.blacklistMessage", "typename": "pdpBlacklistMessage"}, "category": {"type": "id", "generated": false, "id": "pdpCategory2787", "typename": "pdpCategory"}, "txStats": {"type": "id", "generated": true, "id": "$pdpBasicInfo103490518624.txStats", "typename": "pdpTxStats"}, "stats": {"type": "id", "generated": true, "id": "$pdpBasicInfo103490518624.stats", "typename": "pdpStats"}, "ttsPID": "1731177319241910164", "ttsSKUID": "1731177362079123348", "ttsShopID": "7494836721885547412", "isAggregatedWithTTS": true, "__typename": "pdpBasicInfo"}, "$pdpBasicInfo103490518624.stats": {"countView": "0", "countReview": "3220", "countTalk": "0", "rating": 4.7, "__typename": "pdpStats"}, "$pdpBasicInfo103490518624.txStats": {"transactionSuccess": "0", "transactionReject": "0", "countSold": "207785", "paymentVerified": "0", "itemSoldFmt": "100 rb+", "__typename": "pdpTxStats"}, "pdpCategory2787": {"id": "2787", "name": "Kopi Bubuk", "title": "", "breadcrumbURL": "https://www.tokopedia.com/p/makanan-minuman/minuman/kopi-bubuk", "isAdult": false, "isKyc": false, "minAge": 0, "detail": [{"type": "id", "generated": false, "id": "pdpCategoryDetail35", "typename": "pdpCategoryDetail"}, {"type": "id", "generated": false, "id": "pdpCategoryDetail1173", "typename": "pdpCategoryDetail"}, {"type": "id", "generated": false, "id": "pdpCategoryDetail2787", "typename": "pdpCategoryDetail"}], "ttsID": "700612", "ttsDetail": [{"type": "id", "generated": false, "id": "pdpCategoryDetail700437", "typename": "pdpCategoryDetail"}, {"type": "id", "generated": false, "id": "pdpCategoryDetail914824", "typename": "pdpCategoryDetail"}, {"type": "id", "generated": false, "id": "pdpCategoryDetail700612", "typename": "pdpCategoryDetail"}], "__typename": "pdpCategory"}};window.__PAGE_TYPE__="productdetailpage-desktop";</script></body></html>'



# A fingerprint in the shape the API actually returns, trimmed to the keys
# this repo reads. Cut from a real `format=chromium` response; the id and the
# exact pixel values are the only things changed, and only so that nothing
# here looks like a specific machine.
#
# Not named with the `_FIXTURE` suffix on purpose: it is a JSON response from
# the 2Captcha API, not a captured Tokopedia page, so it has no business in
# the committed-capture privacy scan.
FIX_FINGERPRINT = {
    "id": 1000000,
    "country": "ID",
    "userAgent": {
        "userAgent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/146.0.0.0 Safari/537.36"),
        "platform": "Windows",
        "mobile": False,
    },
    "intl": {
        "contentLocale": "id-ID",
        "languages": ["id-ID", "id", "en-US", "en"],
        "timeZone": "Asia/Jakarta",
    },
    "screen": {"width": 1920, "height": 1080,
               "outerWidth": 1920, "outerHeight": 992,
               "deviceScaleFactor": 1},
}

# Short names for the checks below; the `_FIXTURE` suffix is what
# test_no_capture_leaks collects by, so both spellings exist on purpose.
SEARCH = SEARCH_FIXTURE
CATEGORY = CATEGORY_FIXTURE
DETAIL = DETAIL_FIXTURE

# Wording the build fails on. The original 2scraper brief and this repo's own
# April 2026 prototype use several of these — they predate the naming — so
# the scan is what stops one being pasted back in.
BANNED_PHRASES = (
    "cloud browser",
    "antidetect browser",
    "anti-detect browser",
    "2scraper Antidetect Browser",
    "gate.2prx.com",
    "2prx.com",
)

# Flags that were removed and must stay removed. Scoped to the ENGINES:
# `--country` is banned on a scraper for this site — one storefront, so the
# flag could only disagree with the URL — and legitimate on
# fingerprint_client.py, where it picks a fingerprint locale.
REMOVED_ENGINE_FLAGS = ("--antidetect", "--country")
ENGINE_FILES = ("playwright_scraper.py", "puppeteer_scraper.py",
                "selenium_scraper.py")

SEARCH_URL = "https://www.tokopedia.com/search?st=product&q=kopi"
CATEGORY_URL = ("https://www.tokopedia.com/p/makanan-minuman/minuman/"
                "kopi-bubuk")
DETAIL_URL = ("https://www.tokopedia.com/zayn-snack-448/kopi-hitam-bubuk-"
              "robusta-original-berat-1-kg-coffee-kualitas-premium-pahitnya-"
              "pas-cocok-untuk-kopi-susu-1731177319241910164")
HUB_URL = "https://www.tokopedia.com/p/makanan-minuman"


def _by_sku(rows):
    return {r.sku: r for r in rows}


def test_price_parsing():
    group("price parsing")
    ok = True

    # Tokopedia writes IDR with dot grouping and no minor unit: 74 distinct
    # prices across the captures, not one with a comma. The family's rule —
    # exactly three trailing digits means a thousands grouping — is what
    # makes these right, and `Rp1.800` is the case that would break under a
    # decimal-point reading.
    for text, want in [("Rp7.500", [7500.0]),
                       ("Rp1.800", [1800.0]),
                       ("Rp19.999", [19999.0]),
                       ("Rp180.800", [180800.0]),
                       ("Rp1.331.127", [1331127.0]),
                       ("Rp460", [460.0])]:
        ok &= check("%s parses as %s" % (text, want[0]),
                    prices_in(text) == want)

    # The other two grouping conventions and the no-break spaces, because a
    # rendered page uses a no-break variant so the number does not wrap and
    # missing them parses "1 234" as 234. Kept even though no Tokopedia price
    # has been seen written this way: "IDR never has cents" is a claim six
    # captures do not license.
    ok &= check("a comma decimal is read as one", prices_in("Rp7.500,25") == [7500.25])
    ok &= check("NBSP grouping is not read as 800", prices_in("Rp180 800") == [180800.0])
    ok &= check("narrow NBSP grouping too", prices_in("Rp180 800") == [180800.0])

    # THE PROMO LINE. 29 of 95 search tiles carry "Hemat s.d  8% Pakai
    # Bonus" INSIDE the tile, after the prices. It is not the discount, and
    # its percentage is not the badge's. This is MediaMarkt's instalment line
    # in a different costume.
    promo = ("97% Kopi Bubuk Hitam Robusta Rp3.653 Rp117.000 "
             "Hemat s.d  8% Pakai Bonus 4.8 40+ terjual KOPI TUNGKU")
    ok &= check("the promo line does not add a third price",
                prices_in(promo) == [3653.0, 117000.0])

    # Percentages come out BEFORE prices are matched, not after: a rejected
    # match has still consumed the currency symbol, so filtering afterwards
    # loses the real price too.
    ok &= check("a leading discount badge is not read as a price",
                prices_in("94% Rp7.500 Rp120.000") == [7500.0, 120000.0])
    ok &= check("a percentage inside a title is not read as a price",
                prices_in("Biji Berkualitas 100% Higienis Rp3.653")
                == [3653.0])

    # A title may legitimately contain "%", and stripping a LEADING
    # percentage off the tile TEXT would eat it. The parser removes the image
    # container structurally instead — see product_parser._without_image_block.
    rows = parse_products(SEARCH, SEARCH_URL, page=1)
    tungku = _by_sku(rows)["/kopi-tungku/kopi-bubuk-hitam-robusta-original-"
                           "berat-1kg-kualitas-premium-biji-berkualitas-100-"
                           "higienis-diolah-secara-tradisional-"
                           "1733227682292597917"]
    ok &= check("a title keeps its own '100%'", "100% Higienis" in tungku.title)
    ok &= check("...and its badge is not in the title",
                not tungku.title.startswith("97"))
    return ok


def test_sold_and_rating():
    group("the sold count and the rating")
    ok = True

    # "rb" is ribu (thousand), "jt" is juta (million), and the trailing "+"
    # means the site rounded DOWN. So a tile's figure is a FLOOR, which is
    # why `sold_is_floor` exists: the same product's detail page states
    # countSold 207785 against the tile's "100rb+".
    for text, want, floor in [("100rb+ terjual", 100000, True),
                              ("2rb+ terjual", 2000, True),
                              ("1,5jt terjual", 1500000, True),
                              ("40+ terjual", 40, True),
                              ("5 terjual", 5, False)]:
        got = sold_in(text)
        ok &= check("%r -> %s (floor=%s)" % (text, want, floor),
                    got == (want, floor))
    ok &= check("no sold count is (None, None)", sold_in("no numbers here")
                == (None, None))

    # THE ONE THAT MATTERS. A product with no reviews prints no rating, and
    # then its sold count stands alone:
    #
    #     "KOPI SAIYO : 100% Robusta Rp30.000 5 terjual Galeri UMKM …"
    #
    # A looser pattern reads that 5 as a five-star rating on a product that
    # has never been rated. One such tile in 190.
    ok &= check("a lone sold count is not read as a rating",
                rating_in("KOPI SAIYO Rp30.000 5 terjual Galeri UMKM") is None)
    ok &= check("...while its sold count is still read",
                sold_in("KOPI SAIYO Rp30.000 5 terjual") == (5, False))
    ok &= check("a rating before a sold count is read",
                rating_in("Rp7.500 4.7 100rb+ terjual") == 4.7)
    ok &= check("a 5.0 rating is read", rating_in("Rp1 5.0 10rb+ terjual") == 5.0)
    return ok


def test_listing_values():
    group("listing rows, with the VALUES pinned")
    ok = True
    rows = parse_products(SEARCH, SEARCH_URL, page=2)
    ok &= check("the search fixture yields its 8 tiles", len(rows) == 8)
    by = _by_sku(rows)

    # A heavily discounted tile: the discount is COMPUTED from the two
    # prices, never read off the printed badge. Tokopedia's badge said 94%
    # and 120000 -> 7500 is 93.75%.
    zayn = by["/zayn-snack-448/kopi-hitam-bubuk-robusta-original-berat-1-kg-"
              "coffee-kualitas-premium-pahitnya-pas-cocok-untuk-kopi-susu-"
              "1731177319241910164"]
    ok &= check("its title is the product name", zayn.title.startswith(
        "Kopi Hitam Bubuk Robusta Original Berat 1 Kg"))
    ok &= check("its price is 7500", zayn.price == 7500.0)
    ok &= check("its was-price is 120000", zayn.original_price == 120000.0)
    ok &= check("its discount is computed, not read (93.75)",
                zayn.discount_pct == 93.75)
    ok &= check("its rating is 4.7", zayn.rating == 4.7)
    ok &= check("its sold count is a FLOOR of 100000",
                (zayn.sold, zayn.sold_is_floor) == (100000, True))
    ok &= check("its shop is named", zayn.brand == "Zayn Snack")
    ok &= check("its shop city is read from the second span.flip",
                zayn.shop_location == "Kab. Magetan")
    ok &= check("its currency is IDR", zayn.currency == "IDR")
    ok &= check("its price_source is dom — there is nothing else on this "
                "page", zayn.price_source == "dom")
    ok &= check("the page number it was told is carried", zayn.page == 2)
    ok &= check("its shop slug comes from the URL",
                zayn.shop_slug == "zayn-snack-448")
    ok &= check("its slug tail is recorded as a slug tail, not as an id",
                zayn.slug_id == "1731177319241910164")

    # THE CASE A TAIL-DERIVED SKU WOULD LOSE. Four of these eight tiles have
    # no 19-digit tail at all, and they are ordinary organic products with
    # identical markup — so `sku` is the URL PATH and `slug_id` is None here
    # rather than the row being empty.
    tailless = [r for r in rows if r.slug_id is None]
    ok &= check("4 of the 8 fixture tiles have no slug tail",
                len(tailless) == 4)
    ok &= check("...and every one of them still has a sku",
                all(r.sku and r.sku.count("/") == 2 for r in tailless))
    ok &= check("...and a price", all(r.price for r in tailless))
    nestle = by["/nestle-indonesia/nescafe-gold-kopi-instan-kopi-hitam-50g-jar"]
    ok &= check("a tailless row's price is right (45610)",
                nestle.price == 45610.0)

    # The ONE undiscounted tile: a single price, no badge, and
    # `original_price`/`discount_pct` null rather than 0.
    single = by["/shopshabira/kopi-good-day-mocacinno-merah-exp-terbaru-"
                "coffee-3-in-1-1729604349080143827"]
    ok &= check("an undiscounted tile has one price", single.price == 20999.0)
    ok &= check("...no was-price", single.original_price is None)
    ok &= check("...and no discount, not a zero", single.discount_pct is None)

    # THE CANARY ASSERTION §4 asks for, and it costs one line: no row may
    # carry a was-price at or below its price. That is what a second KIND of
    # struck-through price (an EU Omnibus 30-day low, say) would produce.
    ok &= check("no row has an original_price at or below its price",
                not [r for r in rows if r.original_price is not None
                     and r.price is not None
                     and r.original_price <= r.price])
    ok &= check("every discount is between 0 and 100 exclusive",
                all(0 < r.discount_pct < 100 for r in rows
                    if r.discount_pct is not None))

    # `image_url` is SPARSE ON PURPOSE. Tokopedia lazy-loads tile images, so
    # a tile below the fold carries a placeholder — 55 of 95 search tiles
    # held an SVG under /obj/tokopedia-web-sg/zeus_v2/ and 30 of 60 category
    # tiles a data: URI. Reading `src` blindly gives a column that is 100%
    # populated and half wrong.
    for r in rows:
        if r.image_url is None:
            continue
        ok &= check("%s's image is a real product image, not an icon or a "
                    "placeholder" % r.shop_slug,
                    "~tplv-" in r.image_url
                    and "/img/" not in r.image_url
                    and "/obj/" not in r.image_url
                    and not r.image_url.lower().endswith(".svg"))
    return ok


def test_category_listing_is_a_different_page():
    group("the category listing: a second markup and a second pagination")
    ok = True
    rows = parse_products(CATEGORY, CATEGORY_URL, page=1)
    ok &= check("the category fixture yields its 5 tiles", len(rows) == 5)
    by = _by_sku(rows)

    first = by["/delifru/premix-powder-coffee-jelly-arnav-1-kg-bubuk-premix-"
               "jeli-kopi-premium-1729950312039418010"]
    ok &= check("its title is the product name",
                first.title == "Premix Powder Coffee Jelly Arnav 1 Kg - "
                               "Bubuk Premix Jeli Kopi Premium")
    ok &= check("its price is 109435", first.price == 109435.0)
    ok &= check("its shop comes from the badge image's alt",
                first.brand == "Delifru Utama Indonesia")
    ok &= check("the 'Tambah ke Wishlist' button label is not in the title",
                "Wishlist" not in first.title)

    # A category tile prints FEWER fields than a search tile, and that is a
    # property of the page kind rather than a parsing failure: 0 of 60 carry
    # a rating, a sold count, a was-price or a discount badge. The columns
    # stay (they are populated by the other mode and the other page kind) and
    # `mode` in the sidecar is what tells a consumer which run this was.
    ok &= check("no category row has a rating", all(r.rating is None for r in rows))
    ok &= check("no category row has a sold count", all(r.sold is None for r in rows))
    ok &= check("no category row has a was-price",
                all(r.original_price is None for r in rows))
    ok &= check("every category row still has a price and a title",
                all(r.price and r.title for r in rows))
    ok &= check("...and a shop", all(r.brand for r in rows))
    return ok


def test_the_hub_yields_nothing():
    group("a /p/<slug> hub is not a listing")
    ok = True
    # A discovery hub answers 200 with banners and recommendation carousels
    # and no grid — but it DOES carry ~39 product links from those carousels.
    # A parser anchored on the URL pattern alone returns 39 rows of filler
    # and reports success; scoped to the grid it returns zero, which is exit
    # 4 and correct.
    hub = page(
        '<div data-testid="divDiscoLihatSemua">'
        '<a href="https://www.tokopedia.com/kyfood/kylafood-cimol-spesial-'
        'isi-keju-1729382371065562361">Kylafood Cimol Rp25.000</a>'
        '<a href="https://www.tokopedia.com/nestle-indonesia/nestle-'
        'carnation-krimer-kental-manis-488g">Carnation Rp18.900</a>'
        '</div>')
    ok &= check("a hub is recognised from its URL",
                listing_kind(HUB_URL) == "hub")
    ok &= check("carousel links on a hub yield NO rows",
                parse_products(hub, HUB_URL) == [])
    ok &= check("...and the hub is 'empty', not 'unknown' — so the engines "
                "report exit 4 rather than retrying it",
                detect_page_state(hub, 200, HUB_URL) == "empty")
    # The same links inside a real grid container DO yield rows, so the
    # difference above is the scoping and not the URL pattern.
    ok &= check("the same links inside a grid container are read",
                len(parse_products(CATEGORY, CATEGORY_URL)) == 5)
    return ok


def test_product_detail():
    group("the detail page's Apollo cache")
    ok = True
    row = parse_product_page(DETAIL, DETAIL_URL)
    ok &= check("a detail page yields a row", row is not None)
    if row is None:
        return False

    # THE REAL PRODUCT ID, and it is not the URL tail. The tail is
    # 1731177319241910164; the id Tokopedia's own app deep links use is
    # 103490518624. Anything that treated the tail as the id would have been
    # reading a different number entirely and would never have noticed.
    ok &= check("product_id is the site's own id (103490518624)",
                row.product_id == "103490518624")
    ok &= check("...and the URL tail is kept apart from it",
                row.slug_id == "1731177319241910164")
    ok &= check("slug_tail_id agrees", slug_tail_id(DETAIL_URL)
                == "1731177319241910164")

    # The title comes from the h1, not from og:title — which carries a
    # "Promo " prefix the product's name does not have and a
    # " di {shop} | Tokopedia" suffix.
    ok &= check("the title is the product name, with no Promo prefix",
                row.title == "Kopi Hitam Bubuk Robusta Original Berat 1 Kg - "
                             "Coffee Kualitas Premium - Pahitnya pas cocok "
                             "untuk kopi Susu")
    ok &= check("...and no ' | Tokopedia' suffix", "Tokopedia" not in row.title)

    ok &= check("the price comes from product:price:amount (7500)",
                row.price == 7500.0)
    # "Rp" is a SYMBOL, not an ISO 4217 code. §4's tier-2 rule is a written
    # code checked against an allowlist of real codes; this goes through a
    # symbol table instead.
    ok &= check("its currency maps Rp -> IDR", row.currency == "IDR")
    ok &= check("price_source names both sources",
                row.price_source == "meta+apollo")

    # EXACT here, where a tile's is a floor.
    ok &= check("countSold is exact (207785)", row.sold == 207785)
    ok &= check("...and says it is not a floor", row.sold_is_floor is False)
    ok &= check("the review count is read (3220)", row.review_count == 3220)
    ok &= check("the rating is read (4.7)", row.rating == 4.7)

    ok &= check("the shop is named and identified", (row.brand, row.shop_id)
                == ("Zayn Snack", "7494836721885547412"))
    ok &= check("the seller's city is read from the <title>",
                row.shop_location == "Kab. Magetan")
    ok &= check("the category is the site's own name", row.category == "Kopi Bubuk")
    ok &= check("the category's own listing URL is recorded",
                row.category_url == "https://www.tokopedia.com/p/"
                                    "makanan-minuman/minuman/kopi-bubuk")
    ok &= check("the condition is read", row.condition == "New")
    # The site states a number and a unit separately (weight 1, weightUnit
    # KILOGRAM); a column mixing grams with kilograms is a number nobody can
    # compare.
    ok &= check("the weight is normalised to grams", row.weight_grams == 1000)
    ok &= check("maxOrder is recorded as stock_max", row.stock_max == 6795)
    ok &= check("in_stock is derived from status + maxOrder",
                row.in_stock is True)
    ok &= check("the listing date is recorded",
                row.listed_at == "2025-04-16T17:41:57+07:00")

    # A detail page carries no strikethrough of its own, and the "similar
    # products" carousel's prices belong to other products.
    ok &= check("no was-price is invented on a detail page",
                row.original_price is None and row.discount_pct is None)

    # THE CANONICAL CARRIES UTM PARAMETERS, so it is not a clean key. Strip
    # the query or the join between a listing row and a detail row fails on
    # every row.
    ok &= check("the row url has no UTM tail", "utm_" not in row.url)
    ok &= check("its sku is the /{shop}/{slug} path",
                row.sku == product_path(DETAIL_URL))
    ok &= check("a listing row for the same product would join on it",
                row.sku.startswith("/zayn-snack-448/"))

    # A listing page is not a product page, and saying so is what stops a
    # --mode product run against the wrong URL inventing a row.
    ok &= check("a listing page fed to parse_product_page is None",
                parse_product_page(SEARCH, SEARCH_URL) is None)

    facts = shop_metadata(DETAIL, DETAIL_URL)
    ok &= check("shop_metadata reads the seller for the sidecar",
                facts == {"shop_id": "7494836721885547412",
                          "shop_name": "Zayn Snack",
                          "shop_slug": "zayn-snack-448"})
    return ok


def test_urls():
    group("URLs, ids and hosts")
    ok = True
    ok &= check("the supported hosts are the storefront's",
                set(HOSTS) == {"tokopedia.com", "www.tokopedia.com"})
    ok &= check("www. is stripped from the host",
                site_host(SEARCH_URL) == "tokopedia.com")
    ok &= check("the storefront is supported", is_supported_host(SEARCH_URL))
    ok &= check("there is one currency and it is IDR",
                (CURRENCY, host_currency(SEARCH_URL), LOCALE_CURRENCY)
                == ("IDR", "IDR", {"id": "IDR"}))
    ok &= check("there is one locale", locale_of(SEARCH_URL) == "id")

    # Refuse a look-alike host WITH THE REASON. "is not a Tokopedia site"
    # would be false for a Tokopedia subdomain that simply is not a
    # storefront, and sends the reader hunting for a typo.
    for host, word in [("seller.tokopedia.com", "seller dashboard"),
                       ("gql.tokopedia.com", "GraphQL"),
                       ("images.tokopedia.net", "image CDN"),
                       ("mitra.tokopedia.com", "Mitra")]:
        why = unsupported_reason("https://%s/x" % host)
        ok &= check("%s is refused, and the reason names it" % host,
                    why is not None and word.lower() in why.lower())
    ok &= check("another marketplace is refused",
                not is_supported_host("https://shopee.co.id/x"))
    ok &= check("...and the reason does not claim it is a subdomain",
                "subdomain" not in
                (unsupported_reason("https://shopee.co.id/x") or ""))

    # Page kinds.
    for url, kind in [(SEARCH_URL, "search"),
                      (CATEGORY_URL, "category"),
                      (HUB_URL, "hub"),
                      ("https://www.tokopedia.com/p/makanan-minuman/minuman",
                       "category"),
                      (DETAIL_URL, "product")]:
        ok &= check("%s is a %s page" % (url.split("tokopedia.com")[1][:40],
                                         kind),
                    listing_kind(url) == kind)

    # A junk two-segment path under one of the site's own routes must not
    # read as a product: that is how a sizing guide or a promo card steals a
    # real product's row (§4).
    for junk in ["/search/kopi", "/p/makanan-minuman", "/promo/harbolnas",
                 "/help/article", "/about/us", "/discovery/x"]:
        ok &= check("%s is not a product path" % junk,
                    product_path("https://www.tokopedia.com" + junk) is None)
    ok &= check("a real product path is one",
                product_path(DETAIL_URL) is not None)
    ok &= check("a three-segment path is not a product",
                product_path("https://www.tokopedia.com/a/b/c") is None)
    ok &= check("another host's path is not a product",
                product_path("https://shopee.co.id/shop/item") is None)

    ok &= check("sku_from_url is the path",
                sku_from_url(DETAIL_URL) == product_path(DETAIL_URL))
    ok &= check("shop_from_url is the first segment",
                shop_from_url(DETAIL_URL) == "zayn-snack-448")

    # Tracking parameters are stripped before a URL becomes a row's `url` or
    # is compared to a canonical. A listing anchor arrives with an extParam
    # carrying the search id; a detail canonical arrives with a UTM triple.
    dirty = (DETAIL_URL + "?extParam=ivf%3Dfalse%26keyword%3Dkopi"
             "&utm_source=google&utm_campaign=pdp")
    ok &= check("tracking parameters are stripped",
                strip_tracking(dirty) == DETAIL_URL)
    ok &= check("...and a real parameter is kept",
                strip_tracking(CATEGORY_URL + "?page=3&ob=5")
                == CATEGORY_URL + "?page=3&ob=5")

    # The category label.
    ok &= check("a category URL names its own category",
                category_from_url(CATEGORY_URL)
                == "makanan-minuman/minuman/kopi-bubuk")
    ok &= check("a search URL has no category from its path",
                category_from_url(SEARCH_URL) is None)
    return ok


def test_pagination():
    group("pagination — two page kinds, two conventions")
    ok = True

    # A CATEGORY listing paginates with ?page=N, verified against a real
    # page 2: 66 products on page 1, 61 on page 2, overlapping by 3 — the
    # "cheaper products" carousel that appears on both.
    ok &= check("a category listing paginates by URL",
                paginates_by_url(CATEGORY_URL))
    ok &= check("page 2 of a category is ?page=2",
                page_url(CATEGORY_URL, 2) == CATEGORY_URL + "?page=2")
    ok &= check("an existing page is REPLACED, not duplicated",
                page_url(CATEGORY_URL + "?page=7", 2)
                == CATEGORY_URL + "?page=2")
    ok &= check("other parameters survive",
                "ob=5" in page_url(CATEGORY_URL + "?ob=5", 3))
    ok &= check("page 1 is the URL itself", page_url(CATEGORY_URL, 1)
                == CATEGORY_URL)
    ok &= check("the page number is read back",
                page_number_from_url(CATEGORY_URL + "?page=4") == 4)
    ok &= check("no page parameter means page 1",
                page_number_from_url(CATEGORY_URL) == 1)

    # A SEARCH DOES NOT PAGINATE AT ALL, and this is the check that would
    # have caught the family's most expensive bug. `?page=2` on a search URL
    # does not advance it, it EMPTIES the result set — "Oops, produk nggak
    # ditemukan", no grid, no prices. A page_url() used unconditionally
    # would fetch that, find no new sku, and report a COMPLETE run holding
    # page 1.
    ok &= check("a search does NOT paginate by URL",
                not paginates_by_url(SEARCH_URL))
    ok &= check("...so page_url refuses to invent an address for it",
                page_url(SEARCH_URL, 2) is None)
    ok &= check("...and page_flow says it is not addressable",
                not page_flow.pagination_is_addressable(SEARCH_URL))
    ok &= check("a category listing IS addressable",
                page_flow.pagination_is_addressable(CATEGORY_URL))
    ok &= check("a hub is not addressable either",
                not page_flow.pagination_is_addressable(HUB_URL))

    # Concurrency follows from that, and is REFUSED with the reason rather
    # than accepted silently: page 5 of an infinitely scrolling listing has
    # no address, so workers would each re-fetch page 1.
    ok &= check("concurrency is capped at 1 for a search",
                page_flow.concurrency_limit(SEARCH_URL) == 1)
    ok &= check("...with a reason that says why",
                "no per-page addresses"
                in (page_flow.concurrency_refusal(SEARCH_URL) or ""))
    ok &= check("...and the reason points at the URL that does work",
                "/p/<cat>"
                in (page_flow.concurrency_refusal(SEARCH_URL) or ""))
    ok &= check("a category listing is not capped",
                page_flow.concurrency_limit(CATEGORY_URL) is None)
    ok &= check("...and is not refused",
                page_flow.concurrency_refusal(CATEGORY_URL) is None)
    ok &= check("a hub's refusal names the hub, not the search",
                "discovery hub"
                in (page_flow.concurrency_refusal(HUB_URL) or ""))

    # The site publishes NO result total and no page count. Its search header
    # looks like it does — "Menampilkan 61 - 180 barang dari total  untuk
    # "kopi"" — with the total EMPTY, and the range it prints ran ahead of
    # the DOM (180 claimed, 95 present). Recorded verbatim, trusted for
    # nothing.
    ok &= check("no result total is invented", total_results(SEARCH, 8) is None)
    ok &= check("no page count is invented", total_pages(SEARCH) is None)
    hdr = search_header(SEARCH)
    ok &= check("the header is recorded verbatim",
                hdr is not None and "Menampilkan" in hdr
                and "dari total" in hdr)
    ok &= check("a category page has no such header",
                search_header(CATEGORY) is None)

    # Following a link out of the listing would silently replace the run's
    # subject — a "cheaper products" card, a related category, a banner.
    ok &= check("a next-page candidate from another listing is dropped",
                page_flow.next_page_candidates(
                    CATEGORY_URL,
                    ["https://www.tokopedia.com/p/elektronik/tv?page=2"])
                == [CATEGORY_URL + "?page=2"])
    return ok


def test_page_state():
    group("page state — and there is no block page")
    ok = True

    ok &= check("a painted search page is content",
                detect_page_state(SEARCH, 200, SEARCH_URL) == "content")
    ok &= check("a painted category page is content",
                detect_page_state(CATEGORY, 200, CATEGORY_URL) == "content")
    ok &= check("a detail page is content",
                detect_page_state(DETAIL, 200, DETAIL_URL) == "content")

    # The empty-result page is a SUCCESS, not a block: Tokopedia answers 200
    # with its filter rail, its footer and this sentence where the grid would
    # be. Seen on ?page=2 of a search URL and on any query with no matches.
    empty = page('<div>Produk Toko <p>Oops, produk nggak ditemukan</p>'
                 '<span>Coba kata kunci lain atau cek produk rekomendasi di '
                 'bawah.</span></div>')
    ok &= check("the no-results page is recognised", is_no_results(empty))
    ok &= check("...and classified as empty, not blocked",
                detect_page_state(empty, 200, SEARCH_URL + "&page=2")
                == "empty")

    # DETECTION IS INVERTED ON THIS SITE. Tokopedia sends an address it has
    # scored NOTHING — no status code, no interstitial, no vendor marker — so
    # a served page is recognised by the site's OWN asset host and the
    # absence of one is the signal.
    ok &= check("a real page references the site's own assets",
                served_by_tokopedia(SEARCH)
                and served_by_tokopedia(CATEGORY)
                and served_by_tokopedia(DETAIL))
    ok &= check("...including the empty-result page",
                served_by_tokopedia(empty))
    foreign = '<html><body><h1>Access Denied</h1></body></html>'
    ok &= check("a page built out of nothing of the site's is not served",
                not served_by_tokopedia(foreign))
    ok &= check("...and classifies as blocked",
                detect_page_state(foreign, 200, SEARCH_URL) == "blocked")
    ok &= check("no markup at all is blocked",
                detect_page_state("", None, SEARCH_URL) == "blocked")
    ok &= check("a non-200 is blocked",
                detect_page_state(SEARCH, 403, SEARCH_URL) == "blocked")
    ok &= check("page_flow maps a failed navigation to blocked",
                page_flow.classify(None, None, SEARCH_URL) == "blocked")

    # CHROMIUM'S OWN ERROR PAGE, which is what a run actually gets when the
    # navigation fails rather than the request being refused. A live Selenium
    # run through an unauthenticatable proxy produced 187,799 bytes of it —
    # Chromium's built-in error styling, `<title>www.tokopedia.com</title>`,
    # and not one reference to the site's own assets.
    #
    # This is the case that vindicates the inverted detection: the page
    # carries no vendor marker and no error text a marker list would know,
    # and its title is the SITE'S OWN HOSTNAME, so a title check would call
    # it a real page. Only "was this built out of Tokopedia's assets?"
    # answers correctly.
    chrome_error = (
        '<html><head><title>www.tokopedia.com</title>'
        '<style>/* Copyright 2017 The Chromium Authors */ '
        'body { --background-color: #fff; --error-code-color: var(--google'
        '-gray-700); }</style></head><body>'
        '<div id="main-frame-error"><span>ERR_PROXY_CONNECTION_FAILED</span>'
        '</div></body></html>')
    ok &= check("Chromium's own error page is not 'served by Tokopedia'",
                not served_by_tokopedia(chrome_error))
    ok &= check("...and classifies as blocked despite naming the site in its "
                "title", detect_page_state(chrome_error, None, SEARCH_URL)
                == "blocked")
    ok &= check("...and carries no marker a vendor list would have caught",
                detect_bot_challenge(chrome_error) is None)

    # "NOT PAINTED YET" IS NOT A FAULT, and telling it apart from one is what
    # the first live search run got wrong. A search grid arrives with the
    # client-side GraphQL response, so at domcontentloaded the page is a
    # served shell with no grid in it — classified naively that is "unknown",
    # "unknown" retries, and the run fetched the page twice, scrolled not at
    # all and reported 0 rows with exit 4.
    shell = page('<div data-testid="divSRPLazyProductWrapper"></div>'
                 '<div data-testid="divSearchFilter"></div>')
    shell_state = detect_page_state(shell, 200, SEARCH_URL)
    ok &= check("an unpainted search shell is 'unknown'",
                shell_state == "unknown")
    ok &= check("...and page_flow calls it unpainted, so the engines WAIT",
                page_flow.is_unpainted(shell_state, shell))
    ok &= check("a page that was never served is not 'unpainted'",
                not page_flow.is_unpainted("blocked", foreign))
    ok &= check("an empty-result page is not 'unpainted' either",
                not page_flow.is_unpainted("empty", empty))
    ok &= check("a painted page is not 'unpainted'",
                not page_flow.is_unpainted("content", SEARCH))

    # The challenge markers stay broad (different geos surface different
    # challenges) but `cf-turnstile` is deliberately NOT among them: the
    # Scraping Browser's auto-solve extension injects a cf-turnstile hunter
    # into every page it loads, so it would fire on perfectly good pages
    # fetched over --cdp-endpoint. mediamarkt-scraper's first live run
    # reported exit 3 on a 1.8 MB page holding the full catalogue for
    # exactly that reason.
    ok &= check("cf-turnstile is not a marker on this site",
                not any("turnstile" in m.lower()
                        for m in product_parser.BOT_CHALLENGE_MARKERS))
    injected = page(
        '<script src="chrome-extension://kjmkgkdkpedkejedfhmfcenooemhbpbo/'
        'content/captcha/turnstile/hunter.js" '
        'data-ts-input="cf-turnstile-response"></script>')
    ok &= check("an extension-injected script is stripped before scanning",
                detect_bot_challenge(injected) is None)
    ok &= check("a real vendor marker is still found",
                detect_bot_challenge(page('<div class="g-recaptcha"></div>'))
                is not None)
    ok &= check("a good page reports no challenge",
                detect_bot_challenge(SEARCH, SEARCH_URL) is None)

    # A MARKER THAT MATCHES EVERY PAGE IS WORSE THAN NO MARKER. Tokopedia is
    # fronted by Akamai, so its own performance-monitoring script references
    # `…clientnsv4-s.akamaihd.net` on every page it serves — and a live run
    # of a /p/<slug> hub reported "Blocked by akamai" and exit 3 on a 191 KB
    # page the site had plainly served. The bare vendor name is gone;
    # Akamai's actual REFUSAL strings stay.
    ok &= check("the bare string 'akamai' is not a marker",
                "akamai" not in {m.lower() for m
                                 in product_parser.BOT_CHALLENGE_MARKERS})
    ok &= check("a page referencing akamaihd.net is not a challenge",
                detect_bot_challenge(page(
                    '<script>var a="rwpc42axfxuzq2vcqjxq-f-5a6518927-'
                    'clientnsv4-s.akamaihd.net";</script>')) is None)
    ok &= check("...while Akamai's own refusal page still is",
                detect_bot_challenge(
                    "Request unsuccessful. Reference #18.2f Access Denied")
                == "Request unsuccessful")
    return ok


def test_page_flow():
    group("page_flow policy is shared, and is DATA")
    ok = True

    # The retry/solve/blocked decision as data rather than as three copies of
    # an if-chain, so an engine cannot quietly disagree with its twins about
    # whether a page is worth retrying or worth paying for.
    for state, retry, solve, blocked, parse in [
            ("content", False, False, False, True),
            ("empty", False, False, False, True),
            ("blocked", True, False, True, False),
            ("challenge", True, True, True, False),
            ("unknown", True, False, False, False)]:
        ok &= check("%s: retry=%s solve=%s blocked=%s parse=%s"
                    % (state, retry, solve, blocked, parse),
                    (page_flow.should_retry(state),
                     page_flow.should_solve(state),
                     page_flow.counts_as_blocked(state),
                     page_flow.should_parse(state))
                    == (retry, solve, blocked, parse))
    ok &= check("an unrecognised state falls back to 'unknown', not to "
                "'content'", page_flow.should_retry("nonsense-state"))

    # An EMPTY page is a correct answer, so retrying it would spend the
    # user's budget re-confirming the same right answer and rotating the
    # exit would blame an address for the URL it was given.
    ok &= check("an empty page is never retried",
                not page_flow.should_retry("empty"))
    ok &= check("...and never counts as blocked",
                not page_flow.counts_as_blocked("empty"))
    # No challenge has ever been observed on this site, so the spend is
    # capped even though the path is wired up.
    ok &= check("at most one solve is bought per page",
                page_flow.SOLVES_PER_PAGE == 1)

    ok &= check("readiness needs more than one match",
                page_flow.min_matches("listing") > 1)
    ok &= check("a detail page needs only its name",
                page_flow.min_matches("product") == 1)
    ok &= check("the listing anchor is a grid container, not a class hash",
                "data-testid" in page_flow.ready_selector("listing")
                and "==" not in page_flow.ready_selector("listing"))
    ok &= check("every remote wait is bounded",
                page_flow.content_timeout_ms("listing") > 0
                and page_flow.content_timeout_ms("product") > 0)

    # THE SCROLL, driven with the browser stubbed out. §8's rule: scroll to
    # document.body.scrollHeight, and require the count AND the height to
    # hold still for THREE rounds — one is not enough, because the next batch
    # takes longer to arrive than a single pause.
    steps = iter([(0, 1606), (60, 4035), (60, 8157), (95, 8157),
                  (95, 8157), (95, 8157), (95, 8157)])
    state = {"cur": (0, 1606), "scrolls": 0}

    def _count(_sel):
        return state["cur"][0]

    def _height():
        return state["cur"][1]

    def _scroll():
        state["scrolls"] += 1

    def _sleep(_ms):
        try:
            state["cur"] = next(steps)
        except StopIteration:
            pass

    res = page_flow.scroll_until_settled(_count, _height, _scroll, _sleep,
                                         pause_ms=0)
    ok &= check("the scroll settles once the count and height hold still",
                res["settled"] is True)
    ok &= check("...on the row count it reached", res["cards"] == 95)
    ok &= check("...after three stable rounds and not one",
                res["rounds"] == 7)
    # It scrolls after every round that has not yet settled — six of the
    # seven — and not after the one that does. A scroll on the settled round
    # would fetch a batch the run then throws away.
    ok &= check("...and it scrolled once per unsettled round, not after the "
                "settled one", state["scrolls"] == 6)

    # A page still growing when the budget runs out is PARTIAL, not
    # exhausted, and saying so is what stops a consumer reading the missing
    # tail as delisted products.
    growing = {"n": 0}

    def _grow_count(_sel):
        return growing["n"]

    def _grow_height():
        return 1000 + growing["n"] * 40

    def _grow_sleep(_ms):
        growing["n"] += 20

    res2 = page_flow.scroll_until_settled(_grow_count, _grow_height,
                                          lambda: None, _grow_sleep,
                                          rounds=5, pause_ms=0)
    ok &= check("a still-growing page is reported as NOT settled",
                res2["settled"] is False)
    ok &= check("...with the rounds it spent", res2["rounds"] == 5)

    # A driver fault mid-scroll ends the scroll instead of the run: the
    # remote target was observed closing mid-scroll on three of six
    # captures.
    def _boom(_sel):
        raise RuntimeError("Target page, context or browser has been closed")

    res3 = page_flow.scroll_until_settled(_boom, lambda: None, lambda: None,
                                          lambda _ms: None, pause_ms=0)
    ok &= check("a driver fault mid-scroll does not raise",
                res3["settled"] is False)

    # No JavaScript crosses the page_flow boundary. Selenium's
    # execute_script takes a function BODY with an explicit `return` while
    # Playwright and pyppeteer take `() => expr`, so a shared module passing
    # JS would quietly acquire one driver's dialect.
    #
    # Scoped to STRING LITERALS in page_flow's code rather than to its whole
    # source: the module docstring names both dialects on purpose, to say why
    # this rule exists, and a scan over the raw text would fail on the
    # explanation of the very rule it is enforcing.
    _tree = ast.parse(inspect.getsource(page_flow))
    _docstrings = set()
    for _n in ast.walk(_tree):
        if isinstance(_n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                           ast.ClassDef)):
            _d = ast.get_docstring(_n, clean=False)
            if _d:
                _docstrings.add(_d)
    literals = [n.value for n in ast.walk(_tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)
                and n.value not in _docstrings]
    for js in ("() =>", "document.querySelectorAll", "window.scrollTo",
               "return document.", "querySelector"):
        ok &= check("no page_flow string literal contains %r" % js,
                    not [s for s in literals if js in s])

    # Pagination markup entries are ordered most-durable first (§5):
    # standards-based signals before build artefacts.
    sel = page_flow.NEXT_PAGE_SELECTOR
    ok &= check("the pagination selectors lead with link[rel=next]",
                sel and "rel=" in sel[0])
    ok &= check("...and a data-testid comes after it",
                all("data-testid" not in s for s in sel[:2]))

    # THE READINESS WAIT MUST NOT EVALUATE A STRING. Tokopedia's
    # Content-Security-Policy has no `unsafe-eval`, so Playwright's
    # wait_for_function — which hands the browser a string to evaluate —
    # dies with EvalError on a /search page and took a live run down with
    # exit 1. The shared poll counts elements over CDP instead.
    got = {"n": 0}

    def _slow_count(_sel):
        got["n"] += 1
        return 0 if got["n"] < 4 else 9

    ticks = {"n": 0}

    def _tick(_ms):
        ticks["n"] += 1

    found = page_flow.wait_for_count(_slow_count, _tick, "sel", 4,
                                     timeout_ms=10_000, poll_ms=100)
    ok &= check("wait_for_count polls until the count clears the threshold",
                found == 9)
    ok &= check("...sleeping between polls rather than spinning",
                ticks["n"] == 3)
    # A timeout is NOT an error: a listing with genuinely no products on it
    # never reaches the threshold, and that is exit 4 rather than a fault.
    found = page_flow.wait_for_count(lambda _s: 0, lambda _ms: None, "sel", 4,
                                     timeout_ms=300, poll_ms=100)
    ok &= check("a page that never paints returns its count, not an error",
                found == 0)

    for name in ("playwright_scraper", "puppeteer_scraper"):
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        calls = [ln for ln in src.split("\n")
                 if ("wait_for_function(" in ln or "waitForFunction(" in ln)
                 and not ln.strip().startswith("#")]
        ok &= check("%s calls no wait-for-function (the site's CSP forbids "
                    "evaluating a string)" % name, not calls)
    return ok


# ---------------------------------------------------------------------------
# The output contract
# ---------------------------------------------------------------------------
def test_output_contract():
    group("the output contract shared across this scraper family")
    ok = True
    names = [f.name for f in fields(Product)]
    # The family prefix, byte-identical and in order, so a consumer written
    # against another repo in this family reads the first sixteen columns
    # unchanged. Site-specific columns go AFTER it.
    family_prefix = ["source", "scraped_at", "url", "sku", "title", "brand",
                     "price", "currency", "original_price", "discount_pct",
                     "rating", "review_count", "in_stock", "image_url",
                     "category", "price_source"]
    ok &= check("the family field prefix is present and in order",
                names[:len(family_prefix)] == family_prefix)
    ok &= check("Tokopedia's own columns come after it, in order",
                names[len(family_prefix):] ==
                ["page", "position", "shop_slug", "shop_location", "sold",
                 "sold_is_floor", "slug_id", "product_id", "shop_id",
                 "condition", "weight_grams", "stock_max", "listed_at",
                 "category_url"])
    ok &= check("both modes map to a row class, and there is no shop mode",
                ROW_CLASS_BY_MODE == {"listing": Product, "product": Product})
    # A column that is null on every row of every run should not exist (§9).
    # These four are null on every LISTING row and populated in --mode
    # product, which is a different thing and is why they are kept — pinned
    # so that removing them needs a measurement rather than a hunch.
    ok &= check("the detail-only columns are declared",
                {"product_id", "shop_id", "condition", "weight_grams",
                 "stock_max", "listed_at", "category_url"} <= set(names))
    ok &= check("both modes are one row per sku",
                set(UNIQUE_BY_SKU_MODES) == {"listing", "product"})

    ok &= check("the exit codes are the family's",
                (EXIT_BLOCKED, EXIT_NO_PRODUCTS, EXIT_PARTIAL) == (3, 4, 6))
    ok &= check("an exhausted listing counts as complete",
                "no_new_products" in COMPLETE_STOP_REASONS
                and "pagination_exhausted" in COMPLETE_STOP_REASONS)
    ok &= check("a single-page mode is complete by construction",
                "single_page_mode" in COMPLETE_STOP_REASONS)

    # No defaulted currency anywhere: a row that could not establish one says
    # None rather than claiming EUR, which would be wrong for the four
    # non-euro country sites.
    ok &= check("Product defaults currency to None, not a guess",
                Product().currency is None)
    ok &= check("Product defaults price_source to None",
                Product().price_source is None)
    return ok


def test_writers():
    group("writers, dedupe and the refusal to overwrite good data")
    ok = True
    rows = [Product(sku="1", url="u1", price=1.0),
            Product(sku="2", url="u2", price=2.0)]
    with tempfile.TemporaryDirectory() as d:
        prefix = os.path.join(d, "out")

        # A run that finds nothing writes NOTHING: a consumer cannot tell an
        # empty category from a failed run, and the failure destroys the last
        # known good data.
        save(rows, prefix, "json", allow_empty=False)
        ok &= check("a good run writes its output",
                    os.path.exists(prefix + ".json"))
        before = open(prefix + ".json").read()
        save([], prefix, "json", allow_empty=False)
        ok &= check("an empty run does NOT overwrite the previous good output",
                    open(prefix + ".json").read() == before)
        save([], prefix, "json", allow_empty=True)
        ok &= check("--allow-empty is the opt-out and does overwrite",
                    json.load(open(prefix + ".json")) == [])

        # An empty CSV still carries its header, so a consumer reads a table
        # with no rows instead of failing on a zero-byte file.
        csv_path = os.path.join(d, "empty.csv")
        write_csv([], csv_path, row_cls=Product)
        header = open(csv_path).read().strip().split("\n")[0]
        ok &= check("an empty CSV still carries its header",
                    header.split(",")[:4] == ["source", "scraped_at", "url", "sku"])

        # A list column has to survive CSV without becoming a Python repr.
        #
        # NO Tokopedia column is a list — the site publishes no image gallery
        # and no attribute list this repo reads — so this is checked with a
        # local row class rather than with Product. The joining is kept in
        # write_csv because it is generic and because a future column may
        # need it; pinning the CURRENT behaviour is what stops it being
        # deleted as dead or reappearing as a repr() by accident.
        ok &= check("no Product column is a list today",
                    not [f for f in fields(Product)
                         if "List" in str(f.type)])

        from dataclasses import dataclass as _dataclass
        from typing import List as _List, Optional as _Optional

        @_dataclass
        class _WithList:
            sku: _Optional[str] = None
            things: _Optional[_List[str]] = None

        csv_path = os.path.join(d, "list.csv")
        write_csv([_WithList(sku="1", things=["a", "b"])], csv_path,
                  row_cls=_WithList)
        body = open(csv_path).read()
        ok &= check("a list column is joined, not repr()d in CSV",
                    ("a" + LIST_CSV_SEPARATOR + "b") in body and "['a'" not in body)

    seen = set()
    ok &= check("dedupe drops a repeated sku",
                len(dedupe_by_sku([Product(sku="a"), Product(sku="a")], seen)) == 1)
    # A row with no key is always KEPT: there is nothing to check a duplicate
    # against, and dropping it is a silent data loss rather than a dedupe.
    ok &= check("a row with no sku is kept, not dropped",
                len(dedupe_by_key([Product(sku=None), Product(sku=None)],
                                  set())) == 2)

    meta = run_meta("complete", "completed", 3, 3, "u", "u", 36,
                    pages_failed=[], mode="listing", source="tokopedia.com")
    ok &= check("the sidecar records status, mode and source",
                meta["status"] == "complete" and meta["mode"] == "listing"
                and meta["source"] == "tokopedia.com")
    # A count stops being a description once a page can fail while later ones
    # succeed, so the sidecar names WHICH pages failed.
    meta = run_meta("partial", "blocked", 5, 3, "u", "u", 12,
                    pages_failed=[2, 4], mode="listing", source="tokopedia.com")
    ok &= check("the sidecar names which pages failed, by number",
                meta["pages_failed"] == [2, 4])
    return ok


def test_finish_run():
    group("finish_run: the exit codes all three engines must agree on")
    ok = True
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "run")
        rows = [Product(sku="1", url="u")]

        code = finish_run(rows, p, "json", False, blocked=False,
                          stop_reason="completed", pages_requested=1,
                          pages_completed=1, pages_failed=[], mode="listing",
                          source="tokopedia.com", start_url="u", final_url="u")
        ok &= check("a complete run exits 0", code == 0)

        code = finish_run([], p + "b", "json", False, blocked=True,
                          stop_reason="blocked_no-response",
                          pages_requested=1, pages_completed=0,
                          pages_failed=[1], mode="listing",
                          source="tokopedia.com", start_url="u", final_url="u")
        ok &= check("a blocked run exits 3, not 4", code == EXIT_BLOCKED)
        # A FAILED run writes no sidecar: `save` leaves the previous good
        # output in place, and a "failed" sidecar beside good data would
        # contradict it.
        ok &= check("a failed run writes no sidecar beside older good data",
                    not os.path.exists(p + "b.meta.json"))

        code = finish_run([], p + "c", "json", False, blocked=False,
                          stop_reason="completed", pages_requested=1,
                          pages_completed=1, pages_failed=[], mode="listing",
                          source="tokopedia.com", start_url="u", final_url="u")
        ok &= check("a genuinely empty result exits 4, not 3",
                    code == EXIT_NO_PRODUCTS)

        code = finish_run(rows, p + "d", "json", False, blocked=False,
                          stop_reason="page_load_timeout", pages_requested=5,
                          pages_completed=2, pages_failed=[3], mode="listing",
                          source="tokopedia.com", start_url="u", final_url="u")
        ok &= check("a run with data that stopped early exits 6 (partial)",
                    code == EXIT_PARTIAL)
        ok &= check("a partial run still writes what it got",
                    os.path.exists(p + "d.json"))
    return ok


def test_diff():
    group("diff_runs")
    ok = True
    # `price_source` on this site is "dom" for a listing row and
    # "meta+apollo" for a detail row — there is no structured price on a
    # listing page to confirm against — so the source-change case uses those
    # two values rather than the sibling repos' jsonld pair.
    old = [{"sku": "1", "price": 10.0, "price_source": "dom"},
           {"sku": "2", "price": 20.0, "price_source": "dom"},
           {"sku": "3", "price": 30.0, "price_source": "dom"}]
    new = [{"sku": "1", "price": 11.0, "price_source": "dom"},
           {"sku": "3", "price": 30.5, "price_source": "meta+apollo"},
           {"sku": "4", "price": 40.0, "price_source": "dom"}]
    d = diff_products(old, new)
    ok &= check("a real price move is reported as changed",
                any(c["sku"] == "1" for c in d["changed"]))
    ok &= check("a delisted product is reported as removed",
                [r["sku"] for r in d["removed"]] == ["2"])
    ok &= check("a new product is reported as added",
                [r["sku"] for r in d["added"]] == ["4"])
    # A price difference that comes with a price_source difference says
    # something about OUR two snapshots, not about the shop.
    ok &= check("a price move with a source change is not 'changed'",
                not any(c["sku"] == "3" for c in d["changed"]))
    ok &= check("...it is reported separately as source_changed",
                any(c["sku"] == "3" for c in d.get("source_changed", [])))

    # `sold` and `sold_is_floor` are BOTH tracked, and the pair is the
    # point. Without the flag a `sold` change is unreadable: a tile's figure
    # is a floor the site rounded down (100rb+ = 100_000) while a product
    # page's is exact (207785 for that same product), so diffing a listing
    # run against a product run would report a jump of 107,785 that is not a
    # single sale.
    tracked = __import__("diff_runs").TRACKED_FIELDS
    ok &= check("sold and sold_is_floor are both tracked",
                "sold" in tracked and "sold_is_floor" in tracked)
    # And the sibling repo's from-price columns are NOT tracked, because they
    # do not exist here: a Tokopedia tile prints one price, not a range.
    # Pinned so that adding one is a decision.
    ok &= check("no from-price columns are tracked (this site has none)",
                not {"price_is_from", "price_max"} & set(tracked))
    ok &= check("...and Product does not declare them either",
                not {"price_is_from", "price_max"}
                & {f.name for f in fields(Product)})
    return ok


def test_pyppeteer_teardown_noise():
    group("pyppeteer teardown noise is suppressed, and its limit is pinned")
    ok = True
    try:
        import puppeteer_scraper as pyp
    except ImportError:
        return check("pyppeteer engine present (skipped: library absent)", True)

    handler = pyp._AsyncBridge._on_loop_exception.__func__ if hasattr(
        pyp._AsyncBridge._on_loop_exception, "__func__") else pyp._AsyncBridge._on_loop_exception

    class _Loop:
        def __init__(self): self.passed_through = []
        def default_exception_handler(self, context):
            self.passed_through.append(context)

    # Each of these arrives on a run that SUCCEEDED, after the output is
    # written, and four tracebacks under a healthy run is how a reader learns
    # to ignore the log.
    swallowed = [
        {"message": "Task was destroyed but it is pending"},
        {"message": "Future exception was never retrieved",
         "exception": RuntimeError("Protocol error (Target.sendMessageToTarget): "
                                   "No session with given id")},
        {"exception": RuntimeError("Target closed")},
        {"exception": RuntimeError("Connection closed")},
        {"message": "Event loop is closed"},
    ]
    for context in swallowed:
        loop = _Loop()
        handler(loop, context)
        label = (context.get("message") or str(context.get("exception")))[:44]
        ok &= check("teardown noise suppressed: %s" % label,
                    not loop.passed_through)

    # A REAL error must still get through, or the suppression has become a
    # blindfold.
    loop = _Loop()
    handler(loop, {"exception": ValueError("something actually went wrong")})
    ok &= check("a real exception is NOT swallowed", len(loop.passed_through) == 1)

    # The handler reads BOTH fields. It used to read `exception or message`,
    # which meant a context carrying both never had its message inspected —
    # so the asyncio-worded ones kept printing after they were "handled".
    src = inspect.getsource(handler)
    ok &= check("the handler inspects the message as well as the exception",
                'for k in ("exception", "message")' in src)

    # PINNED LIMITATION, not a guard: `Exception ignored in: <coroutine
    # object Connection._recv_loop>` is printed by CPython's garbage
    # collector at interpreter shutdown, after the loop is gone and after the
    # exit code is decided. No loop handler can reach it, and catching it
    # would mean a global unraisable hook that swallows real bugs too. It is
    # documented in TROUBLESHOOTING.md instead; this check makes sure that
    # documentation stays there.
    doc = open(os.path.join(REPO_ROOT, "TROUBLESHOOTING.md"),
               encoding="utf-8").read()
    ok &= check("the shutdown-time traceback is documented rather than hidden",
                "Exception ignored in" in doc and "The run succeeded" in doc)
    return ok


def test_canary_separates_access_from_defect():
    group("the canary fails on defects and only WARNS on access conditions")
    ok = True
    wf_path = os.path.join(REPO_ROOT, ".github", "workflows", "canary.yml")
    wf = open(wf_path, encoding="utf-8").read()

    # The data checks must not run on a blocked or refused run: there is no
    # output file, and a missing file would fail for the wrong reason.
    ok &= check("the data checks are gated on the run having got in",
                "steps.verdict.outputs.tested == 'true'" in wf)

    # Extract the real interpret-the-exit-code script and run it under bash
    # for every code, rather than asserting on the YAML text. What matters is
    # whether the JOB FAILS, and only running it answers that.
    try:
        start = wf.index('          set -e\n          code=')
        end = wf.index('          echo "tested=$tested" >> "$GITHUB_OUTPUT"')
        end += len('          echo "tested=$tested" >> "$GITHUB_OUTPUT"')
    except ValueError:
        return check("the canary's exit-code script could be located", False)
    script = "\n".join(line[10:] if line.startswith(" " * 10) else line
                        for line in wf[start:end].splitlines())

    # WHY EACH CODE LANDS WHERE IT DOES:
    #   0  got in and parsed         -> pass, and the assertions then run
    #   3  blocked before parsing    -> ACCESS. Measured intermittent per
    #      profile on this site, so a daily red badge would be noise.
    #   5  endpoint refused          -> ACCESS, and the commonest cause is an
    #      EXPIRED SECRET. A credential is not forever; failing on it paints
    #      the badge red every day until someone notices.
    #   6  partial                   -> ACCESS, usually a mid-run block.
    #   1  crashed                   -> DEFECT.
    #   2  bad arguments             -> DEFECT (in the workflow itself).
    #   4  served a page, ZERO rows  -> DEFECT, and precisely the regression
    #      this canary exists to catch: the tile anchor moved.
    expected = {0: "pass", 3: "warn", 5: "warn", 6: "warn",
                1: "fail", 2: "fail", 4: "fail", 99: "fail"}
    for code, want in sorted(expected.items()):
        body = script.replace('code="${{ steps.run.outputs.exit_code }}"',
                              'code="%d"' % code)
        with tempfile.TemporaryDirectory() as td:
            out_file = os.path.join(td, "gh_output")
            summary = os.path.join(td, "gh_summary")
            open(out_file, "w").close()
            open(summary, "w").close()
            done = subprocess.run(
                ["bash", "-c", body], capture_output=True, text=True,
                env=dict(os.environ, GITHUB_OUTPUT=out_file,
                         GITHUB_STEP_SUMMARY=summary))
            failed = done.returncode != 0
            warned = "::warning::" in done.stdout
            errored = "::error::" in done.stdout
            wrote_summary = bool(open(summary, encoding="utf-8").read().strip())
            tested = "tested=true" in open(out_file, encoding="utf-8").read()

        if want == "pass":
            got = not failed and not warned and not errored and tested
        elif want == "warn":
            # A warning must NOT read as a pass: it also has to say in the
            # step summary that nothing was actually tested, and it must not
            # claim `tested`.
            got = not failed and warned and wrote_summary and not tested
        else:
            got = failed and errored
        ok &= check("exit %-2d is treated as %s" % (code, want), got)

    ok &= check("the reason access is not a defect is written down",
                "ACCESS CONDITIONS ARE NOT DEFECTS" in wf)

    # NO SCHEDULE, and the reason has to travel with the decision. A daily
    # cron against a credential that does not survive a day gives either a
    # permanently red badge or a permanently green one that tested nothing —
    # and the green is worse, because it reads as "the parser still works".
    # Restoring the cron is a legitimate change the day a long-lived
    # credential exists; this check makes it a decision rather than a habit.
    ok &= check("the canary has no cron schedule",
                not re.search(r"^\s*-\s*cron:", wf, re.M))
    ok &= check("it is dispatchable by hand", "workflow_dispatch:" in wf)
    ok &= check("and the reason the schedule is off is written down",
                "does not survive a day" in wf)
    ok &= check("the expired-secret case is named",
                "expired" in wf.lower() and "refresh" in wf.lower())
    return ok


def test_ci_checks_is_actually_wired_up():
    group("the repo's own checks are RUN, and still catch a real secret")
    ok = True
    script = os.path.join(REPO_ROOT, ".github", "ci_checks.py")
    ok &= check("ci_checks.py exists", os.path.exists(script))
    if not os.path.exists(script):
        return ok

    # IT HAS TO BE INVOKED BY A WORKFLOW. It was not — for the whole of
    # v0.1.0 it sat there implementing three checks that nothing ran, while a
    # second, LOOSER copy of one of them lived inline in tests.yml. Dead code
    # that looks load-bearing is worse than no code, and this is the check
    # that keeps it alive.
    wf_dir = os.path.join(REPO_ROOT, ".github", "workflows")
    workflows = "\n".join(
        open(os.path.join(wf_dir, f), encoding="utf-8").read()
        for f in sorted(os.listdir(wf_dir)) if f.endswith((".yml", ".yaml")))
    ok &= check("a workflow runs ci_checks.py", "ci_checks.py" in workflows)
    ok &= check("the secret check specifically is run",
                "--secret-check" in workflows or "--all" in workflows)

    # AND IT PASSES ON THIS REPO. A check that is always red teaches everyone
    # to ignore checks; this one WAS red, on six documented placeholders.
    done = subprocess.run([sys.executable, script, "--all"],
                          cwd=REPO_ROOT, capture_output=True, text=True)
    ok &= check("ci_checks.py --all passes on this repo (exit %d)" % done.returncode,
                done.returncode == 0)
    if done.returncode != 0:
        print("        " + (done.stdout or done.stderr).strip()[-400:])

    # AND IT STILL CATCHES A REAL ONE. Loosening an allowlist until the check
    # passes is the failure mode here, so both directions are asserted: a
    # planted CDP endpoint, a planted 32-hex key and a planted http proxy URL
    # must all be found. The http one matters most — the inline grep this
    # replaced covered only ws:// and would have missed a committed proxy.
    planted = os.path.join(REPO_ROOT, "_secret_probe_delete_me.py")
    # The key is ASSEMBLED rather than written as a literal, because a
    # 32-character hex string sitting in this file is exactly what the check
    # under test flags — and it did, on the first run of this test. The file
    # it writes still gets the whole thing, which is what the probe needs.
    planted_key = "3f8a1c9e4b7d2065" + "af13ce88b409d752"
    try:
        with open(planted, "w", encoding="utf-8") as f:
            f.write(
                'CDP = "ws://acct-zone-scraping_browser-pid-x:'
                'S3cretPassw0rd@cb.2captcha.com:9222"\n'
                'KEY = "%s"\n'
                'PROXY = "http://acct-zone-custom:S3cretPassw0rd'
                '@na.proxy.2captcha.com:2334"\n' % planted_key)
        caught = subprocess.run([sys.executable, script, "--secret-check"],
                                cwd=REPO_ROOT, capture_output=True, text=True)
        out = caught.stdout + caught.stderr
        ok &= check("a planted secret fails the check", caught.returncode != 0)
        ok &= check("the planted ws:// CDP endpoint is named",
                    "_secret_probe_delete_me.py:1" in out)
        ok &= check("the planted 32-hex key is named",
                    "_secret_probe_delete_me.py:2" in out)
        ok &= check("the planted http:// PROXY url is named (the grep this "
                    "replaced missed those)",
                    "_secret_probe_delete_me.py:3" in out)
    finally:
        # Never leave it behind: a test that mutates the working tree is its
        # own defect, and this one would plant a fake secret.
        if os.path.exists(planted):
            os.remove(planted)
    ok &= check("the probe file is cleaned up", not os.path.exists(planted))

    # The pre-publication scan: the same rules over every blob that has EVER
    # existed. A later commit cannot remove what a published tag and a merged
    # PR's refs already hold, so this has to be runnable BEFORE the repo goes
    # public — and it has to be findable, which a check makes it.
    hist = subprocess.run([sys.executable, script, "--history-check"],
                          cwd=REPO_ROOT, capture_output=True, text=True)
    ok &= check("--history-check runs and this history is clean",
                hist.returncode == 0)
    ok &= check("it says how many objects it looked at",
                "ever existed" in hist.stdout)
    # NOT in --all, on purpose: it shells out to git once per object, and a
    # dirty history needs a decision rather than a red check on every push.
    every = subprocess.run([sys.executable, script, "--all"],
                           cwd=REPO_ROOT, capture_output=True, text=True)
    ok &= check("--all deliberately excludes the history scan",
                "history check" not in every.stdout)
    return ok


def test_no_capture_leaks():
    group("no credentials or personal data in the committed fixtures")
    ok = True
    # Collected by SUFFIX, which is how this file names its fixtures. An
    # earlier version asked for a "FIX_" PREFIX, matched nothing, and every
    # check below passed against an empty string — 150 KB of committed real
    # captures went unexamined while twelve checks reported green. The
    # non-empty assertion underneath is the actual fix: a corpus check that
    # can silently scan nothing is worse than no corpus check at all.
    names = [k for k, v in sorted(globals().items())
             if k.endswith("_FIXTURE") and isinstance(v, str)]
    fixtures = "\n".join(globals()[k] for k in names)
    ok &= check("the privacy checks below have fixtures to scan "
                "(%d fixtures, %d chars)" % (len(names), len(fixtures)),
                len(names) >= 3 and len(fixtures) > 30000)
    # Guarded with PATTERNS rather than with the literals a previous capture
    # happened to contain, so the NEXT capture is checked too. MediaMarkt's
    # pages embed a front-end configuration blob — a Sentry DSN, a Woosmap
    # public key, a store-code JWT — none of which is needed to test a
    # parser, and none of which belongs in a public repository.
    patterns = {
        "a JWT": r"eyJ[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{10,}",
        "an access token": r"(?:access|auth|bearer)[_\-]?[Tt]oken\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{12,}",
        "an API key": r"(?:api|public|secret|private)[_\-]?[Kk]ey\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{12,}",
        "a Sentry DSN": r"https://[0-9a-f]{16,}@[\w.]*ingest",
        "a session id": r"session[_\-]?[Ii]d\"?\s*[:=]\s*\"?[A-Za-z0-9._\-]{8,}",
        "an email address": r"[\w.+-]+@[\w-]+\.[a-z]{2,}",
        "a proxy credential": r"://[^\s/@\"]+:[^\s/@\"]+@",
    }
    for label, pattern in patterns.items():
        hits = re.findall(pattern, fixtures)
        ok &= check("no %s in the fixtures" % label, not hits)

    # The SITE's OWN per-impression material, which a fresh capture brings with
    # it: a click-tracking key, its checksum, and the logging key that ties an
    # impression to a session. Anonymous and expired, and still not something
    # to commit — and a 40-character hex-ish blob in a public repo reads as a
    # credential to every scanner that looks, including this repo's own CI
    # grep. Matched as PATTERNS rather than as the values one capture
    # happened to hold, so the NEXT capture is checked too.
    site_session = {
        "a click-tracking key": r"click_key=(?!PLACEHOLDER)[A-Za-z0-9%.-]{12,}",
        "a click checksum": r"click_sum=(?!PLACEHOLDER)[A-Za-z0-9]{6,}",
        "an impression logging key":
            r'data-logging-key="(?!PLACEHOLDER)[A-Za-z0-9:-]{12,}"',
        "a content-source token":
            r"content_source=(?!PLACEHOLDER)[A-Za-z0-9%.-]{12,}",
        "a DataDome session blob": r"'(?:cid|hsh|e|cookie)':'(?!PLACEHOLDER)[^']{16,}'",
    }
    for label, pattern in site_session.items():
        hits = re.findall(pattern, fixtures)
        ok &= check("no %s in the fixtures (scrub a new capture before "
                    "committing it)" % label, not hits)

    # The repo-wide grep CI runs, applied here too so a failure is local.
    # Asked of GIT, not of the filesystem. A developer's own `.env` beside
    # the scripts is EXPECTED — it is how the local runs get their key — and
    # `.gitignore` is what keeps it out of the repo. Checking for the file's
    # existence made this red on every machine that had ever run the scraper
    # for real, which is the machine most likely to be running the suite.
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", ".env"],
        cwd=REPO_ROOT, capture_output=True, text=True).returncode == 0
    ok &= check("no .env file is tracked by git", not tracked)
    return ok


def test_wording():
    group("wording and removed flags")
    ok = True
    # Asked of GIT, so the scan reaches the workflows and the issue
    # templates under .github/ — eight shipped files that an os.listdir of
    # the repo ROOT silently missed, including the four a contributor is
    # most likely to paste marketing wording into. Untracked scratch files
    # and .pytest_cache/ are excluded for free by asking git.
    listed = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT,
                            capture_output=True, text=True)
    if listed.returncode == 0 and listed.stdout.strip():
        shipped = [f for f in listed.stdout.split("\n")
                   if f.endswith((".py", ".md", ".txt", ".toml", ".yml", ".yaml"))
                   and os.path.basename(f) != os.path.basename(__file__)]
    else:  # not a git checkout (a release tarball): fall back to the root
        shipped = [f for f in os.listdir(REPO_ROOT)
                   if f.endswith((".py", ".md", ".txt", ".toml", ".yml", ".yaml"))
                   and f != os.path.basename(__file__)]
    ok &= check("the wording scan reaches beyond the repo root",
                any(os.sep in f or "/" in f for f in shipped))
    for phrase in BANNED_PHRASES:
        offenders = []
        for f in shipped:
            try:
                text = open(os.path.join(REPO_ROOT, f), encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            if phrase.lower() in text.lower():
                offenders.append(f)
        ok &= check("no shipped file says %r" % phrase, not offenders)

    for flag in REMOVED_ENGINE_FLAGS:
        offenders = []
        for f in ENGINE_FILES:
            path = os.path.join(REPO_ROOT, f)
            if not os.path.exists(path):
                continue
            text = open(path, encoding="utf-8").read()
            # A prose mention explaining why the flag does NOT exist is fine
            # and is worth keeping; an argparse registration is not.
            if ('add_argument("%s"' % flag) in text or \
                    ("add_argument('%s'" % flag) in text:
                offenders.append(f)
        ok &= check("no engine registers the removed flag %s" % flag,
                    not offenders)

    # The product this repo integrates with, named correctly.
    readme = os.path.join(REPO_ROOT, "README.md")
    if os.path.exists(readme):
        text = open(readme, encoding="utf-8").read()
        ok &= check("the README names the Scraping Browser API",
                    "Scraping Browser API" in text)
        ok &= check("the README does not name a competitor",
                    not re.search(r"brightdata|oxylabs|smartproxy|zyte|scraperapi\.com",
                                  text, re.IGNORECASE))
    return ok


def test_fingerprint_application():
    group("a fingerprint is applied as the fingerprint describes it")
    ok = True
    import fingerprint_client as fpc

    ua = fpc.fingerprint_user_agent(FIX_FINGERPRINT)
    # The UA used to be read from `userAgent.value`, a key the API returns in
    # NEITHER format. So --fingerprint silently set no user agent at all and
    # the browser kept its own: a German fingerprint's screen and locale
    # wearing a local Chromium's UA, which is precisely the identity mismatch
    # the flag exists to avoid.
    ok &= check("the user agent is found in the shape the API returns",
                ua and ua.startswith("Mozilla/5.0 (Windows NT 10.0"))
    ok &= check("the `raw` format's ua key is understood too",
                fpc.fingerprint_user_agent({"data": {"ua": "UA/1.0"}}) == "UA/1.0")
    ok &= check("a fingerprint with no user agent yields None, not a crash",
                fpc.fingerprint_user_agent({"country": "DE"}) is None)

    kw = fpc.playwright_context_kwargs(FIX_FINGERPRINT)
    ok &= check("the context carries the fingerprint's user agent",
                kw.get("user_agent") == ua)
    # `locale` used to be built as f"en-{country}", giving "en-ID" for an
    # Indonesian fingerprint. An English-speaking visitor in Indonesia is
    # possible, but it is not what this fingerprint describes, and a locale
    # that contradicts the rest of the identity is the mismatch again. This
    # was one of the six defects a sibling repo inherited from copied core
    # and never ran (§16).
    ok &= check("the locale is the fingerprint's own, not en-<country>",
                kw.get("locale") == "id-ID")
    ok &= check("the timezone is carried, so the browser cannot contradict it",
                kw.get("timezone_id") == "Asia/Jakarta")
    # A viewport exactly equal to the screen is itself a signal, and the
    # fingerprint states its own window size rather than needing one guessed.
    ok &= check("the viewport is the fingerprint's window, not its screen",
                kw.get("viewport") == {"width": 1920, "height": 992}
                and kw.get("screen") == {"width": 1920, "height": 1080})

    # Falling back sensibly when a field is absent, rather than dropping it.
    bare = fpc.playwright_context_kwargs({"country": "FR", "screen":
                                          {"width": 1280, "height": 800}})
    ok &= check("a fingerprint with no intl block still gets a locale",
                bare.get("locale") == "en-FR")
    ok &= check("...and a window smaller than the screen",
                bare["viewport"]["height"] < bare["screen"]["height"])
    ok &= check("a fingerprint with nothing usable yields no kwargs",
                fpc.playwright_context_kwargs({}) == {})

    # Every key this produces must be one Playwright's new_context accepts;
    # an unknown one is a TypeError at launch, on the paid path, at runtime.
    accepted = {"user_agent", "viewport", "screen", "locale", "timezone_id",
                "geolocation", "permissions", "extra_http_headers",
                "device_scale_factor", "is_mobile", "has_touch", "color_scheme"}
    ok &= check("every context kwarg is one Playwright accepts",
                set(kw) <= accepted)
    return ok


def test_credentials_never_reach_a_log():
    group("an API key never reaches a log or an exception message")
    ok = True
    import fingerprint_client as fpc
    import captcha_solver as cs

    # requests puts the FULL URL — query string included — into the text of
    # HTTPError and of every connection error. Both of these modules have an
    # endpoint that takes the key as a query parameter, so an error there
    # echoed a live key to the terminal. It did, once, on a real call.
    # An obviously fake key, and NOT a real one even a revoked one: a
    # 32-hex string in a public repo reads as a live credential to every
    # scanner that looks, including this repo's own CI grep. The word
    # "example" in the name is what tells that grep this line is a fixture.
    example_key = "0123456789abcdef0123456789abcdef"
    for name, module in (("fingerprint_client", fpc), ("captcha_solver", cs)):
        redacted = module._redact(
            "400 Client Error: Bad Request for url: "
            "https://api.2captcha.com/fingerprint/random?format=chromium&"
            "key=%s" % example_key)
        ok &= check("%s redacts a key out of an error message" % name,
                    example_key not in redacted)
        ok &= check("...and keeps the endpoint, which is the useful half",
                    "api.2captcha.com/fingerprint/random" in redacted)
        ok &= check("%s redacts clientKey too" % name,
                    example_key not in module._redact("clientKey=%s" % example_key))
        ok &= check("%s leaves ordinary text alone" % name,
                    module._redact("upstream status 403") == "upstream status 403")
    return ok


def test_concurrent_dispatch(skips):
    group("concurrent page dispatch (threads, stop event, accounting)")
    ok = True
    try:
        import playwright_scraper as eng
    except ImportError as e:
        skips.append("concurrent dispatch (%s)" % e)
        return ok

    # The thread fan-out is the one part of --concurrency that the rest of
    # this suite does not reach, and it is not reachable from a live run in
    # every environment either: page 1 is always fetched alone and decides
    # whether the rest may be addressed, so a blocked page 1 means the
    # workers never start. Driven here with the browser stubbed out, which
    # leaves exactly the concurrency logic under test.
    original = (eng.sync_playwright, eng._BrowserSession, eng._fetch_one_page)

    class Args:
        delay = 0
        mode = "listing"
        out = "x"

    def run(specs, concurrency, rows_for_page, die_on=()):
        fetched, lock = [], threading.Lock()

        def fake_fetch(session, args, pool, page_num, url):
            with lock:
                fetched.append(page_num)
            if page_num in die_on:
                raise RuntimeError("worker blew up on page %d" % page_num)
            outcome = eng.PageOutcome(page_num=page_num, url=url)
            outcome.products = rows_for_page(page_num)
            return outcome

        eng.sync_playwright = lambda: _FakePlaywright()
        eng._BrowserSession = lambda pw, args, pool, **kw: _FakeSession(pool)
        eng._fetch_one_page = fake_fetch
        try:
            results, unattempted, exhausted = eng._fetch_pages_concurrently(
                Args(), None, specs, concurrency)
        finally:
            (eng.sync_playwright, eng._BrowserSession,
             eng._fetch_one_page) = original
        return fetched, results, unattempted, exhausted

    # 1. Every page fetched exactly once, whatever the worker count.
    specs = [(n, "u%d" % n) for n in range(2, 12)]
    fetched, results, unattempted, exhausted = run(
        specs, 4, lambda n: ["row"])
    ok &= check("every queued page is fetched exactly once",
                sorted(fetched) == [n for n, _ in specs])
    ok &= check("every page produces an outcome",
                sorted(o.page_num for o in results) == [n for n, _ in specs])
    ok &= check("nothing is left unattempted when the listing does not end",
                unattempted == [] and not exhausted)

    # 2. Results arrive in whatever order the threads finish, which is
    #    exactly why the caller merges by page number instead of by arrival.
    #    Sorting them must reconstruct the page order.
    ok &= check("outcomes can be put back into page order",
                [o.page_num for o in sorted(results, key=lambda o: o.page_num)]
                == [n for n, _ in specs])

    # 3. The stop event. Asking for 50 pages of a listing that ends at page 5
    #    must not fetch 45 empty ones: workers check the event before taking
    #    more work, so at most (concurrency - 1) extra are already in flight.
    specs = [(n, "u%d" % n) for n in range(2, 51)]
    fetched, results, unattempted, exhausted = run(
        specs, 3, lambda n: [] if n >= 5 else ["row"])
    ok &= check("the end of the listing stops dispatch", exhausted)
    ok &= check("an exhausted listing costs at most (concurrency-1) extra "
                "fetches (%d fetched of 49 queued)" % len(fetched),
                len(fetched) <= 4 + 3)
    ok &= check("the pages never tried are reported, not counted as failed",
                unattempted and all(o.ok for o in results))
    ok &= check("unattempted pages are reported in order",
                unattempted == sorted(unattempted))

    # 4. A worker that dies must not hang the run, and must not swallow the
    #    pages its siblings did fetch.
    specs = [(n, "u%d" % n) for n in range(2, 8)]
    fetched, results, unattempted, exhausted = run(
        specs, 3, lambda n: ["row"], die_on={3})
    ok &= check("a worker that raises does not hang the run",
                len(results) + len(unattempted) + 1 >= len(specs))
    ok &= check("the pages other workers fetched still come back",
                any(o.page_num != 3 for o in results))
    return ok


def test_no_undefined_names():
    group("no engine references a name that does not exist")
    ok = True
    # This exists because of a bug that got all the way to a live run.
    # puppeteer_scraper.py called `detect_page_state(...)` on a line reached
    # only while fetching a page, after the import of that name had been
    # removed. The module imported fine, `--help` worked, `compileall`
    # passed, the whole offline suite passed and CI was green — and the
    # engine died with NameError on its first real page.
    #
    # Byte-compiling proves a file PARSES. It says nothing about whether the
    # names in it resolve, and the paths where they do not are exactly the
    # ones an offline suite cannot execute.
    for name in sorted(f for f in os.listdir(REPO_ROOT) if f.endswith(".py")):
        missing = _undefined_names(os.path.join(REPO_ROOT, name))
        detail = ", ".join("%s (line %d)" % (k, v[0])
                           for k, v in sorted(missing.items()))
        ok &= check("%s references no undefined name%s"
                    % (name, ": " + detail if missing else ""), not missing)
    return ok


def test_dockerfile_copies_what_it_runs():
    group("the Docker image contains every module its entrypoint imports")
    ok = True
    path = os.path.join(REPO_ROOT, "Dockerfile")
    if not os.path.exists(path):
        return check("Dockerfile exists", False)

    # The Dockerfile COPYs an explicit list rather than the whole directory,
    # which is right — the image should not carry the test suite, the
    # fixtures or a stray .env. The cost is that the list can fall behind the
    # imports, and NOTHING else in this repo would notice: CI never builds
    # the image, so a missing module ships and the container dies with
    # ModuleNotFoundError on every invocation, `--help` included.
    #
    # That is not hypothetical. `proxy_pool.py` was missing from this list,
    # and playwright_scraper.py imports it at module level.
    raw = open(path, encoding="utf-8").read()
    joined = re.sub(r"\\\n\s*", " ", raw)          # fold line continuations
    copied = set()
    for line in joined.splitlines():
        if line.startswith("COPY "):
            copied.update(tok for tok in line.split() if tok.endswith(".py"))

    entrypoint = None
    m = re.search(r'ENTRYPOINT\s*\[([^\]]*)\]', joined)
    if m:
        parts = [x.strip().strip('"\'') for x in m.group(1).split(",")]
        entrypoint = next((x for x in parts if x.endswith(".py")), None)
    ok &= check("the Dockerfile names a Python entrypoint", bool(entrypoint))
    if not entrypoint:
        return False
    ok &= check("the entrypoint itself is copied into the image",
                entrypoint in copied)

    # Every LOCAL module the entrypoint reaches, transitively.
    local = {f[:-3] for f in os.listdir(REPO_ROOT) if f.endswith(".py")}

    def reached(module, seen=None):
        seen = seen if seen is not None else set()
        if module in seen:
            return seen
        seen.add(module)
        tree = ast.parse(open(os.path.join(REPO_ROOT, module + ".py"),
                              encoding="utf-8").read())
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name in local:
                    reached(name, seen)
        return seen

    needed = reached(entrypoint[:-3])
    missing = sorted(m + ".py" for m in needed if (m + ".py") not in copied)
    ok &= check("every module the entrypoint imports is COPYed (%s)"
                % (", ".join(missing) if missing else "none missing"),
                not missing)

    # The other direction is a warning, not a failure: diff_runs.py is copied
    # deliberately as a companion tool even though the engine never imports
    # it. But anything copied must at least still EXIST.
    gone = sorted(f for f in copied
                  if not os.path.exists(os.path.join(REPO_ROOT, f)))
    ok &= check("the Dockerfile copies no file that has been deleted (%s)"
                % (", ".join(gone) if gone else "none"), not gone)
    return ok


def test_sample_output():
    group("sample_output is cut from a real run")
    ok = True
    path = os.path.join(REPO_ROOT, "sample_output.json")
    if not os.path.exists(path):
        return check("sample_output.json exists", False)
    rows = json.load(open(path, encoding="utf-8"))
    ok &= check("the sample has rows", len(rows) > 0)
    names = [f.name for f in fields(Product)]
    ok &= check("its columns match the Product schema exactly",
                all(set(r) == set(names) for r in rows))
    text = json.dumps(rows, ensure_ascii=False)
    ok &= check("the sample carries no fabrication markers",
                not re.search(r"example\.com|lorem ipsum|FIXME|TODO|XXXX",
                              text, re.IGNORECASE))
    # `sku` is the /{shop}/{slug} path, not the 19-digit URL tail — see the
    # README. Pinned here too, because a sample whose sku changed shape would
    # be the first visible sign of that decision being reversed.
    ok &= check("every sample row's sku is its /{shop}/{slug} path",
                all(re.fullmatch(r"/[^/]+/[^/]+", r.get("sku") or "")
                    for r in rows))
    ok &= check("every sample row names the storefront it came from",
                all((r.get("source") or "") in HOSTS for r in rows))
    ok &= check("every sample row's URL is on the storefront",
                all((r.get("url") or "").startswith(
                    "https://www.tokopedia.com/") for r in rows))
    ok &= check("no sample URL carries a tracking tail",
                not [r for r in rows
                     if "utm_" in (r.get("url") or "")
                     or "extParam" in (r.get("url") or "")])
    # A sample cut from ONE page kind would hide half the schema: a category
    # tile prints no rating, no sold count and no was-price, and only a
    # product page states the site's own id. So the sample has to span them,
    # or a reader judges the output by its sparsest or its fullest rows alone.
    ok &= check("the sample spans both modes",
                any(r.get("product_id") for r in rows)
                and any(r.get("page") for r in rows))
    ok &= check("the sample shows a discounted row and an undiscounted one",
                any(r.get("original_price") for r in rows)
                and any(r.get("original_price") is None for r in rows))
    ok &= check("the sample shows a row with a slug tail and one without",
                any(r.get("slug_id") for r in rows)
                and any(r.get("slug_id") is None for r in rows))
    ok &= check("the sample shows sold as a floor and as an exact figure",
                any(r.get("sold_is_floor") is True for r in rows)
                and any(r.get("sold_is_floor") is False for r in rows))
    # The sample is what a reader judges the output by, so it has to show the
    # provenance column doing its job rather than a column of nulls.
    ok &= check("the sample shows a real price_source",
                all(r.get("price_source") in ("dom", "meta", "meta+apollo")
                    for r in rows))

    csv_path = os.path.join(REPO_ROOT, "sample_output.csv")
    if os.path.exists(csv_path):
        header = open(csv_path, encoding="utf-8").read().split("\n")[0]
        ok &= check("the sample CSV header matches the schema",
                    header.strip().split(",") == names)
    return ok


def test_captcha():
    group("captcha detection and reconciliation")
    ok = True
    from captcha_solver import CaptchaChallenge

    # Format 2: the site's own wrapper element carries the config as
    # attributes, with the execute() call inside a bundled file that never
    # appears as readable inline script.
    widget = ('<captcha-widget data-captcha-type="recaptcha" data-version="v3" '
              'data-sitekey="6LcABCDEFGHIJKLMNOPQRSTUVWXYZ0123" '
              'data-action="submit"></captcha-widget>')
    c = detect_recaptcha_v3(widget, "https://www.tokopedia.com/")
    ok &= check("a captcha-widget declaring v3 is detected",
                c is not None and c.kind == "recaptcha_v3")

    # A sitekey is at least 20 characters; a short string next to
    # data-sitekey is not one, and treating it as one would send a malformed
    # task to the API and bill for the answer.
    ok &= check("a too-short sitekey is not accepted as a challenge",
                detect_recaptcha_v3('<div data-sitekey="short" '
                                    'class="g-recaptcha"></div>',
                                    "https://www.tokopedia.com/") is None)
    ok &= check("a page with no reCAPTCHA at all is not a challenge",
                detect_recaptcha_v3(SEARCH, SEARCH_URL) is None)

    # THE LOADER WINS. A site's own wrapper can declare v3 while the Google
    # loader it actually ships is the v2-invisible signature
    # (render=explicit, size=invisible, a bframe challenge iframe). v3
    # parameters sent for a v2-invisible widget buy a token the site
    # rejects — so the runtime reading is authoritative and the two
    # detectors are reconciled rather than short-circuited.
    static_v3 = CaptchaChallenge(kind="recaptcha_v3", sitekey="6LcABC" + "X" * 20,
                                 action="submit", source="html")
    runtime_v2 = CaptchaChallenge(kind="recaptcha_v2_invisible",
                                  sitekey="6LcABC" + "X" * 20,
                                  source="runtime", size="invisible")
    merged = reconcile_detections(static_v3, runtime_v2)
    ok &= check("when the detectors disagree, the live loader wins",
                merged is not None and merged.kind == "recaptcha_v2_invisible")
    ok &= check("...and the real action from the static markup is kept",
                merged.action == "submit")
    ok &= check("one detector alone is still used when only it fires",
                reconcile_detections(static_v3, None) is static_v3
                and reconcile_detections(None, runtime_v2) is runtime_v2)
    ok &= check("neither firing means no challenge",
                reconcile_detections(None, None) is None)

    # Deliberately absent: no solver for a first-party image captcha. This
    # site has no such page — measured 2026-09-10, its refusal is not a page
    # at all: the HTTP/2 stream is reset and nothing arrives — so a solver
    # for one would be dead code that looks load-bearing. Pinned so that
    # reintroducing it is a decision rather than a drift.
    import captcha_solver
    ok &= check("no first-party image-captcha solver was ported",
                not [n for n in dir(captcha_solver)
                     if "image" in n.lower() and "captcha" in n.lower()])
    # ...while the DETECTORS stay broad, which is the family's standing
    # policy: which challenge a visitor meets depends on the exit country and
    # on what the address has been doing.
    # The marker set is deliberately SHORT on this site, and shorter than
    # the sibling repos'. None of these has ever been observed on Tokopedia —
    # a refused request is not a page at all — so every entry is a guess
    # about a bot manager that might be switched on between deploys, and a
    # long list of guesses is not better than a short one.
    #
    # `cf-turnstile` is specifically EXCLUDED: the Scraping Browser's
    # auto-solve extension injects a cf-turnstile hunter into every page it
    # loads, so it would fire on good pages fetched over --cdp-endpoint.
    # Pinned in both directions so a broadening edit is a decision.
    markers = {m.lower() for m in product_parser.BOT_CHALLENGE_MARKERS}
    ok &= check("detection covers the vendors a page could carry",
                {"g-recaptcha", "hcaptcha.com", "request unsuccessful"}
                <= markers)
    ok &= check("...and does NOT include cf-turnstile, which our own "
                "extension injects",
                not [m for m in markers if "turnstile" in m])
    return ok


def test_env_config():
    group("env_config")
    ok = True
    ok &= check("the env keys are this site's, not another repo's",
                set(env_config.ENV_KEYS) ==
                {"TWOCAPTCHA_KEY", "TOKOPEDIA_CDP_ENDPOINT",
                 "TOKOPEDIA_PROXY", "TOKOPEDIA_URL"})

    # .env.example must document exactly the variables the code reads, in
    # both directions. It drifts otherwise, and a documented-but-unread
    # variable is worse than an undocumented one.
    example = os.path.join(REPO_ROOT, ".env.example")
    documented = set()
    if os.path.exists(example):
        for line in open(example, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                documented.add(line.split("=", 1)[0].strip())
    ok &= check(".env.example documents exactly the variables the code reads",
                documented == set(env_config.ENV_KEYS))

    # A variable mapped onto a flag with a non-empty default would be
    # silently inert, because the loader only fills UNSET values: a setting
    # that looks configurable and is not.
    ok &= check("no env variable is mapped onto --out (it has a default)",
                "out" not in env_config.ENV_KEYS.values())

    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, ".env")
        with open(path, "w", encoding="utf-8") as f:
            f.write("TWOCAPTCHA_KEY=fromfile\n")
            f.write("TOKOPEDIA_URL=https://www.tokopedia.com/search?q=x\n")
            f.write("NOT_A_REAL_KEY=1\n")

        class A:
            twocaptcha_key = None
            url = None
            cdp_endpoint = None
            proxy = None

        a = A()
        env_config.load_env(path)
        env_config.apply(a, quiet=True)
        ok &= check("a value in .env fills an unset flag",
                    a.twocaptcha_key == "fromfile")

        b = A()
        b.twocaptcha_key = "fromflag"
        env_config.apply(b, quiet=True)
        # A .env must never override something the caller typed.
        ok &= check("an explicit flag beats .env", b.twocaptcha_key == "fromflag")
        # A typo is REPORTED rather than silently ignored.
        ok &= check("an unrecognised variable in .env is reported",
                    "NOT_A_REAL_KEY" in env_config.unknown_keys(path))
    return ok


def test_proxy_pool():
    group("proxy_pool: credentials never reach argv or logs")
    ok = True
    url = "http://user:secret@eu.proxy.2captcha.com:2334"
    masked = mask(url)
    ok &= check("credentials are masked in logs", "secret" not in masked)
    # The host and port are KEPT: which exit a run used is the point of the
    # log and is not the secret.
    ok &= check("...but the host and port survive masking",
                "eu.proxy.2captcha.com:2334" in masked)

    pw = to_playwright(url)
    # A `--proxy-server=` value becomes part of the browser's command line,
    # readable by anything that can run `ps`. The credentials go through the
    # driver's own fields instead.
    ok &= check("the server string handed to the browser has no credentials",
                "secret" not in pw["server"])
    ok &= check("credentials go through the driver's own fields",
                pw["username"] == "user" and pw["password"] == "secret")

    scrubbed, creds = split_credentials(url)
    ok &= check("split_credentials separates the two",
                scrubbed == "http://eu.proxy.2captcha.com:2334"
                and creds == ("user", "secret"))

    pool = ProxyPool(["http://a:1", "http://b:2", "http://c:3"])
    ok &= check("a pool reports its size", len(pool) == 3)
    first = pool.current
    pool.advance("test")
    ok &= check("advancing moves to another exit", pool.current != first)
    # `.proxies` hands back a COPY, so a worker building its own pool from it
    # cannot mutate the parent's list. Two threads sharing one mutable list
    # is the bug that makes concurrency stop being worth it.
    copy = pool.proxies
    copy.append("http://d:4")
    ok &= check("the pool hands out a copy of its exits, not the list itself",
                len(pool) == 3)

    # Workers start on DIFFERENT exits, each with its own pool object, so no
    # thread needs a lock: the concurrency is safe by construction rather
    # than by discipline. Tested through the engine's own helper, because
    # that is where the offset actually lives.
    try:
        import playwright_scraper
    except ImportError:
        playwright_scraper = None
    if playwright_scraper is not None:
        exits = [playwright_scraper._worker_pool(pool, i).current
                 for i in range(3)]
        ok &= check("three workers start on three different exits",
                    len(set(exits)) == 3)
        ok &= check("a worker with no pool gets none",
                    playwright_scraper._worker_pool(None, 0) is None)

    # A pool of one is legal and must not rotate itself into an index error.
    one = ProxyPool(["http://only:1"])
    one.advance("nowhere else to go")
    ok &= check("a single-exit pool survives a rotation",
                one.current == "http://only:1")
    ok &= check("an empty pool is refused rather than silently accepted",
                _raises(lambda: ProxyPool([])))

    # This used to assert that "http://host:port:login:pass" — a line from a
    # proxy LIST FILE — "is understood", checking only that parse_proxy_line
    # did not reject it. It returned the string unchanged, so the check
    # passed; the value was never usable, and it blew up several calls later.
    # A test that asserts a function did not complain is not a test that its
    # answer was right.
    #
    # A proxy LIST FILE line pasted where a proxy URL belongs. This is the
    # mistake a new user makes — the file format is
    # scheme://host:port:login:password and the flag wants
    # http://login:password@host:port — and it reached a real CI run.
    #
    # It used to sail through parse_proxy_line (which never looked at the
    # port) and blow up much later inside to_playwright as an uncaught
    # ValueError: exit 1, a crash, where it should be exit 2, bad usage. And
    # the traceback printed the login AND the password into a public CI log.
    from proxy_pool import ProxyError
    pasted = ("http://eu.proxy.2captcha.com:2334:"
              "SOMELOGIN-zone-custom-region-de:SOMEPASSWORD")
    raised = None
    try:
        parse_proxy_line(pasted, source="TOKOPEDIA_PROXY")
    except ProxyError as exc:
        raised = str(exc)
    ok &= check("a proxy-list line pasted as a URL is refused, not crashed on",
                raised is not None)
    ok &= check("...and the refusal says what the value should look like",
                raised is not None and "login:password@host:port" in raised)
    ok &= check("...and neither the login nor the password is in the message",
                raised is not None
                and "SOMEPASSWORD" not in raised and "SOMELOGIN" not in raised)

    # mask() is the last thing standing between a password and a log, and it
    # is called precisely when the value is already wrong. It read
    # `parsed.port`, which urlparse computes lazily and which RAISES on a
    # malformed authority — so the masker blew up on exactly the input that
    # most needed masking. A masker that raises is worse than a vague one.
    ok &= check("mask() does not raise on a malformed URL",
                "SOMEPASSWORD" not in mask(pasted))
    for junk in ("::::", "not a url", "http://", "://x", ""):
        try:
            mask(junk)
            raised_here = False
        except Exception:
            raised_here = True
        ok &= check("mask(%r) does not raise" % junk, not raised_here)
    ok &= check("mask() still keeps host and port on a good URL",
                mask("http://u:p@h.example:8080") == "http://***:***@h.example:8080")
    return ok


# The three engines. Playwright is primary; the other two exist for parity
# and are demoted in priority, not in correctness — all three must agree on
# exit codes, run status, and whether a run crashes or spends money.
_ENGINE_MODULES = ("playwright_scraper", "puppeteer_scraper",
                   "selenium_scraper")


# Stand-ins for a real browser session, so the concurrency machinery can be
# driven with the browser stubbed out. A live run cannot always reach it:
# page 1 is fetched alone and decides whether the rest may be addressed, so a
# blocked page 1 means the workers never start.
class _FakeSession:
    """Stands in for a _BrowserSession: opened, closed, carries a pool."""

    def __init__(self, pool=None):
        self.pool = pool
        self.closed = False

    def open(self):
        return self

    def close(self):
        self.closed = True


class _FakePlaywright:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


STATE_POLICY_NAMES = ("content", "empty", "blocked", "challenge", "unknown")


def test_engines(skips):
    group("engines: all three must behave identically")
    ok = True
    loaded = {}
    for name in _ENGINE_MODULES:
        try:
            loaded[name] = __import__(name)
        except ImportError as e:
            # Reported, never swallowed: "skipped, engine absent" reads
            # exactly like a passing run, and CI's engine-smoke job fails if
            # this list is non-empty.
            skips.append("%s (%s)" % (name, e))

    for name, mod in loaded.items():
        ok &= check("%s exposes scrape() and parse_args()" % name,
                    hasattr(mod, "scrape") and hasattr(mod, "parse_args"))
        # The engines must reach the shared policy rather than carry copies.
        src = inspect.getsource(mod)
        ok &= check("%s takes its readiness policy from page_flow" % name,
                    "page_flow.ready_selector" in src)
        ok &= check("%s takes its state policy from page_flow" % name,
                    "page_flow.should_retry" in src or "page_flow.classify" in src)
        # The scroll POLICY lives in page_flow and the DRIVER primitives
        # live here — the opposite of a sibling repo, which has no scroll at
        # all. An engine that grew its own loop would drift on the settle
        # rule, so the shared call is asserted and a local copy is banned.
        ok &= check("%s drives the shared scroll rather than its own" % name,
                    "page_flow.scroll_until_settled" in src)
        ok &= check("%s names the scroll operations, and passes no JS to "
                    "page_flow" % name,
                    "page_height" in src and "scroll_to_bottom" in src)
        # "Not painted yet" is not a fault, and every engine has to make that
        # distinction the same way — the first live search run of the
        # Playwright engine reported 0 rows and exit 4 because it did not.
        ok &= check("%s waits for an unpainted page instead of retrying it"
                    % name, "page_flow.is_unpainted" in src)
        # Credentials never reach a log, in any engine.
        ok &= check("%s masks credentials globally, not just once" % name,
                    "pass@" not in mod._mask_credentials(
                        "a ws://user:pass@h:1/ b ws://user:pass@h:1/"))
        ok &= check("%s refuses a host that is not Tokopedia" % name,
                    "is_supported_host" in src)
        # The same modes in every engine — a mode one engine offers and
        # another does not is the drift page_flow.py and finish_run() exist
        # to prevent, one level up. There are exactly two here, and no shop
        # mode: a Tokopedia shop front is a different application shell whose
        # markup has not been measured, and a mode that ships untested is
        # worse than one that is absent.
        ok &= check("%s offers exactly the listing and product modes" % name,
                    '"listing", "product"]' in src)
        ok &= check("%s has no shop mode" % name, '"shop"' not in src)

    # EVERY page_flow CALL IN EVERY ENGINE, CHECKED AGAINST THE REAL
    # SIGNATURE. This is the general form of a bug the first live run of the
    # pyppeteer engine found: `classify(html, status, url)` took `status`
    # positionally, and two of the three engines called it as
    # `classify(html, url=…)` because they have no response object to read a
    # status from. Both crashed with TypeError on their FIRST fetch — and
    # that was invisible to import, to --help, to compileall, to the AST
    # undefined-name walk and to 400+ green offline checks, because none of
    # those calls a function the way a live run does.
    #
    # An offline suite cannot execute a fetch. It CAN bind every call's
    # arguments to the callee's signature, which is the same check the
    # interpreter does at the moment of the call, minus the browser.
    import inspect as _inspect
    for name in _ENGINE_MODULES:
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        bad = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            if not (isinstance(fn, ast.Attribute)
                    and isinstance(fn.value, ast.Name)
                    and fn.value.id == "page_flow"):
                continue
            target = getattr(page_flow, fn.attr, None)
            if not callable(target):
                bad.append("%s: page_flow has no %s()" % (name, fn.attr))
                continue
            try:
                sig = _inspect.signature(target)
            except (TypeError, ValueError):
                continue
            # Bind PLACEHOLDERS, not values: this checks arity and keyword
            # names, which is what drifts. `*args` in the call (none today)
            # would make the binding unknowable, so it is skipped rather
            # than guessed at.
            if any(isinstance(a, ast.Starred) for a in node.args) or \
                    any(k.arg is None for k in node.keywords):
                continue
            try:
                sig.bind(*[object()] * len(node.args),
                         **{k.arg: object() for k in node.keywords})
            except TypeError as exc:
                bad.append("%s:%d page_flow.%s(...) — %s"
                           % (name, node.lineno, fn.attr, exc))
        ok &= check("every page_flow call in %s matches its signature" % name,
                    not bad)
        for line in bad:
            print("        %s" % line)

    # The same check for product_parser, which the engines call as often.
    for name in _ENGINE_MODULES:
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "product_parser":
                imported.update(a.asname or a.name for a in node.names)
        bad = []
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in imported):
                continue
            target = getattr(product_parser, node.func.id, None)
            if not callable(target):
                continue
            if any(isinstance(a, ast.Starred) for a in node.args) or \
                    any(k.arg is None for k in node.keywords):
                continue
            try:
                _inspect.signature(target).bind(
                    *[object()] * len(node.args),
                    **{k.arg: object() for k in node.keywords})
            except TypeError as exc:
                bad.append("%s:%d %s(...) — %s"
                           % (name, node.lineno, node.func.id, exc))
        ok &= check("every product_parser call in %s matches its signature"
                    % name, not bad)
        for line in bad:
            print("        %s" % line)

    # And the two-argument call itself, pinned: `status` must stay optional,
    # because two of the three engines have no status to pass.
    ok &= check("page_flow.classify works with no status, as two engines "
                "call it",
                page_flow.classify("<html>x</html>",
                                   url=SEARCH_URL) in STATE_POLICY_NAMES)

    # For "it must pass with no engine installed" to mean anything, each
    # engine has to import its driver at MODULE level — otherwise the module
    # imports cleanly with the library absent, the group never skips, and the
    # CI job that exists to catch that cannot. This drifts back silently, so
    # it is asserted rather than trusted.
    driver_imports = {"playwright_scraper": "playwright",
                      "puppeteer_scraper": "pyppeteer",
                      "selenium_scraper": "selenium"}
    for name, lib in driver_imports.items():
        path = os.path.join(REPO_ROOT, name + ".py")
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding="utf-8").read())
        top_level = set()
        for node in tree.body:
            if isinstance(node, ast.Import):
                top_level.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top_level.add(node.module.split(".")[0])
        ok &= check("%s imports %s at module level, so an absent library skips"
                    % (name, lib), lib in top_level)
    return ok


def _raises_type(fn, exc_type) -> bool:
    """True if `fn()` raises exactly `exc_type` (or a subclass)."""
    try:
        fn()
    except exc_type:
        return True
    except Exception:  # noqa: BLE001 — a different type is a failed check
        return False
    return False


def _no_secret_in(fn, secret: str) -> bool:
    """True if `fn()` raises and the secret is absent from the message."""
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        return secret not in str(e)
    return False


# Names Python provides that are not imports and not assignments.
_MODULE_DUNDERS = {"__file__", "__name__", "__doc__", "__package__",
                   "__spec__", "__loader__", "__builtins__", "__debug__"}


def _undefined_names(path):
    """Names loaded in `path` that are never imported, defined or assigned.

    A deliberately coarse approximation — it pools every binding in the file
    rather than tracking scopes, so it under-reports and never invents a
    problem. That is the right trade here: this exists to catch a name that
    is nowhere at all, and a false positive would be worse than a miss.
    """
    tree = ast.parse(open(path, encoding="utf-8").read())
    bound = set(dir(builtins)) | _MODULE_DUNDERS
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            bound |= {(a.asname or a.name.split(".")[0]) for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            bound |= {(a.asname or a.name) for a in node.names}
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                               ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.Global):
            bound |= set(node.names)
    missing = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) \
                and node.id not in bound:
            missing.setdefault(node.id, []).append(node.lineno)
    return missing


def main() -> int:
    ok = True
    # Checks that could not run because an optional engine library is absent.
    # Reported at the end: a suite that silently skips part of itself and
    # still says "all passed" is the same defect as code that reports success
    # without checking that what it wanted actually happened.
    skips = []

    ok &= test_price_parsing()
    ok &= test_sold_and_rating()
    ok &= test_listing_values()
    ok &= test_category_listing_is_a_different_page()
    ok &= test_the_hub_yields_nothing()
    ok &= test_product_detail()
    ok &= test_urls()
    ok &= test_pagination()
    ok &= test_page_state()
    ok &= test_page_flow()
    ok &= test_output_contract()
    ok &= test_writers()
    ok &= test_finish_run()
    ok &= test_diff()
    ok &= test_captcha()
    ok &= test_env_config()
    ok &= test_proxy_pool()
    ok &= test_engines(skips)
    ok &= test_pyppeteer_teardown_noise()
    ok &= test_canary_separates_access_from_defect()
    ok &= test_ci_checks_is_actually_wired_up()
    ok &= test_no_capture_leaks()
    ok &= test_wording()
    ok &= test_fingerprint_application()
    ok &= test_credentials_never_reach_a_log()
    ok &= test_concurrent_dispatch(skips)
    ok &= test_no_undefined_names()
    ok &= test_dockerfile_copies_what_it_runs()
    ok &= test_sample_output()

    print()
    if _failures:
        print("%d check(s) FAILED:" % len(_failures))
        for f in _failures:
            print("  - %s" % f)
    if skips:
        print("%d engine group(s) SKIPPED — an optional engine library is "
              "absent. CI's engine-smoke job installs all three and fails if "
              "this list is non-empty, because a skip reads exactly like a "
              "passing run:" % len(skips))
        for s in skips:
            print("  - %s" % s)
    print("smoke_test: %s" % ("OK" if ok else "FAILED"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
