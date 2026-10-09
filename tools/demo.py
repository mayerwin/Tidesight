#!/usr/bin/env python3
"""Generate a realistic demo dataset and open the dashboard on it.

    python tools/demo.py --out demo-data            # 60 days, about 4,000 visitors
    python server/python/devserver.py --config demo-data/tidesight.toml --no-auth --port 8790
    # then open http://127.0.0.1:8790/dashboard/

Traffic is synthetic but goes through the real collector (in process, with
simulated clock times) and the real enrichment jobs: pageviews with context,
engagement, scroll, clicks for heatmaps, rage and dead clicks, forms, Web
Vitals, errors, purchases, site search, returning visitors, campaigns, a few
bots, mouse movement, DOM snapshots and short session recordings. Geolocation
uses small generated databases (fake but public IP ranges).
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import math
import os
import random
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from wsgiref.util import setup_testing_defaults

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "server", "python"))
sys.path.insert(0, os.path.join(REPO, "server", "python", "tests"))

from mmdb_writer import write_database  # noqa: E402  (test helper: builds MaxMind DB files)
from tidesight import collect  # noqa: E402
from tidesight.collect import Collector  # noqa: E402
from tidesight.config import ConfigHolder, load  # noqa: E402
from tidesight.enrich import pipeline  # noqa: E402

SITE = "demo"
ORIGIN = "https://demo.tidesight.dev"
DAY = 86400000

CITIES = [  # (country, city, region code, region name, lat, lon, tz, weight)
    ("US", "New York", "NY", "New York", 40.71, -74.0, "America/New_York", 9),
    ("US", "San Francisco", "CA", "California", 37.77, -122.42, "America/Los_Angeles", 7),
    ("US", "Austin", "TX", "Texas", 30.27, -97.74, "America/Chicago", 4),
    ("US", "Chicago", "IL", "Illinois", 41.88, -87.63, "America/Chicago", 4),
    ("FR", "Paris", "IDF", "Île-de-France", 48.86, 2.35, "Europe/Paris", 9),
    ("FR", "Lyon", "ARA", "Auvergne-Rhône-Alpes", 45.76, 4.84, "Europe/Paris", 3),
    ("PF", "Papeete", "WIN", "Windward Islands", -17.54, -149.57, "Pacific/Tahiti", 3),
    ("GB", "London", "ENG", "England", 51.51, -0.13, "Europe/London", 7),
    ("GB", "Manchester", "ENG", "England", 53.48, -2.24, "Europe/London", 2),
    ("DE", "Berlin", "BE", "Berlin", 52.52, 13.4, "Europe/Berlin", 5),
    ("DE", "Munich", "BY", "Bavaria", 48.14, 11.58, "Europe/Berlin", 3),
    ("CA", "Toronto", "ON", "Ontario", 43.65, -79.38, "America/Toronto", 3),
    ("CA", "Montreal", "QC", "Quebec", 45.5, -73.57, "America/Toronto", 2),
    ("AU", "Sydney", "NSW", "New South Wales", -33.87, 151.21, "Australia/Sydney", 3),
    ("JP", "Tokyo", "13", "Tokyo", 35.68, 139.69, "Asia/Tokyo", 3),
    ("BR", "São Paulo", "SP", "São Paulo", -23.55, -46.63, "America/Sao_Paulo", 3),
    ("IN", "Bengaluru", "KA", "Karnataka", 12.97, 77.59, "Asia/Kolkata", 4),
    ("NL", "Amsterdam", "NH", "North Holland", 52.37, 4.9, "Europe/Amsterdam", 3),
    ("ES", "Madrid", "MD", "Madrid", 40.42, -3.7, "Europe/Madrid", 2),
    ("IT", "Milan", "25", "Lombardy", 45.46, 9.19, "Europe/Rome", 2),
    ("SE", "Stockholm", "AB", "Stockholm", 59.33, 18.07, "Europe/Stockholm", 2),
    ("SG", "Singapore", "01", "Central Singapore", 1.29, 103.85, "Asia/Singapore", 2),
    ("NZ", "Auckland", "AUK", "Auckland", -36.85, 174.76, "Pacific/Auckland", 1),
    ("MX", "Mexico City", "CMX", "Mexico City", 19.43, -99.13, "America/Mexico_City", 2),
]
COUNTRY_NAMES = {"US": "United States", "FR": "France", "PF": "French Polynesia", "GB": "United Kingdom", "DE": "Germany",
                 "CA": "Canada", "AU": "Australia", "JP": "Japan", "BR": "Brazil", "IN": "India", "NL": "Netherlands",
                 "ES": "Spain", "IT": "Italy", "SE": "Sweden", "SG": "Singapore", "NZ": "New Zealand", "MX": "Mexico"}
ISPS = ["Comcast Cable", "Orange", "Vodafone", "Deutsche Telekom", "Rogers", "Telstra", "NTT", "Vivo", "Jio", "KPN",
        "Telefonica", "TIM", "Telia", "Singtel", "Spark", "Telmex", "Free SAS", "Vini", "BT", "Bell Canada"]

DEVICES = [  # (weight, ua, uad or None, ctx overrides)
    (30, "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36",
     {"brands": [{"brand": "Google Chrome", "version": "129"}], "mobile": False, "platform": "Windows", "platformVersion": "15.0.0",
      "fullVersionList": [{"brand": "Google Chrome", "version": "129.0.6668.90"}]}, {"sw": 1920, "sh": 1080, "vw": 1536, "vh": 730, "dpr": 1.25, "tp": 0}),
    (8, "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36 Edg/129.0.0.0",
     {"brands": [{"brand": "Microsoft Edge", "version": "129"}], "mobile": False, "platform": "Windows", "platformVersion": "10.0.0",
      "fullVersionList": [{"brand": "Microsoft Edge", "version": "129.0.2792.65"}]}, {"sw": 1920, "sh": 1080, "vw": 1280, "vh": 680, "dpr": 1, "tp": 0}),
    (12, "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
     None, {"sw": 1512, "sh": 982, "vw": 1512, "vh": 860, "dpr": 2, "tp": 0}),
    (6, "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:131.0) Gecko/20100101 Firefox/131.0", None,
     {"sw": 2560, "sh": 1440, "vw": 1600, "vh": 900, "dpr": 1, "tp": 0}),
    (20, "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
     None, {"sw": 393, "sh": 852, "vw": 393, "vh": 659, "dpr": 3, "tp": 5}),
    (14, "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0.0.0 Mobile Safari/537.36",
     {"brands": [{"brand": "Google Chrome", "version": "129"}], "mobile": True, "platform": "Android", "platformVersion": "14.0.0",
      "model": "Pixel 8", "fullVersionList": [{"brand": "Google Chrome", "version": "129.0.6668.81"}]},
     {"sw": 412, "sh": 915, "vw": 412, "vh": 780, "dpr": 2.625, "tp": 5}),
    (4, "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/25.0 Chrome/121.0.0.0 Mobile Safari/537.36",
     None, {"sw": 384, "sh": 854, "vw": 384, "vh": 720, "dpr": 3, "tp": 5}),
    (4, "Mozilla/5.0 (iPad; CPU OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1",
     None, {"sw": 820, "sh": 1180, "vw": 820, "vh": 1100, "dpr": 2, "tp": 5}),
]

SOURCES = [  # (weight, landing referrer, query string to add, landing pages)
    (28, "", "", ["/", "/pricing", "/docs/getting-started"]),
    (22, "https://www.google.com/", "", ["/", "/blog/reading-the-tides", "/docs/getting-started", "/features"]),
    (4, "https://www.bing.com/", "", ["/", "/features"]),
    (3, "https://duckduckgo.com/", "", ["/", "/docs/api"]),
    (7, "", "utm_source=newsletter&utm_medium=email&utm_campaign=october-release", ["/blog/whats-new", "/pricing"]),
    (6, "https://www.google.com/", "gclid=EAIaIQobChMI&utm_source=google&utm_medium=cpc&utm_campaign=brand", ["/", "/pricing"]),
    (5, "https://t.co/abc123", "", ["/blog/self-hosting-analytics", "/"]),
    (4, "https://www.linkedin.com/", "", ["/about", "/blog/self-hosting-analytics"]),
    (3, "https://news.ycombinator.com/", "", ["/blog/self-hosting-analytics"]),
    (4, "https://chatgpt.com/", "utm_source=chatgpt.com", ["/docs/getting-started", "/"]),
    (2, "https://www.perplexity.ai/", "", ["/features"]),
    (3, "https://github.com/tidesight/tidesight", "", ["/docs/getting-started", "/docs/api"]),
    (2, "https://www.reddit.com/r/selfhosted/", "", ["/blog/self-hosting-analytics", "/"]),
    (1, "https://www.youtube.com/", "", ["/"]),
    (1, "https://www.reddit.com/r/selfhosted/", "", ["/docs/install"]),  # links to pages that no longer exist
    (1, "https://t.co/abc123", "", ["/blog/tides-explained"]),
]

PAGES = {
    "/": ("Tidesight — analytics that keeps every wave", ["/features", "/pricing", "/docs/getting-started", "/blog/reading-the-tides"]),
    "/features": ("Features · Tidesight", ["/pricing", "/docs/getting-started", "/"]),
    "/pricing": ("Pricing · Tidesight", ["/signup", "/features", "/docs/getting-started"]),
    "/signup": ("Create your account · Tidesight", ["/checkout", "/pricing"]),
    "/checkout": ("Checkout · Tidesight", ["/thank-you", "/pricing"]),
    "/thank-you": ("Welcome aboard · Tidesight", ["/docs/getting-started"]),
    "/docs/getting-started": ("Getting started · Docs", ["/docs/api", "/docs/tracker", "/pricing"]),
    "/docs/api": ("API reference · Docs", ["/docs/getting-started", "/docs/tracker"]),
    "/docs/tracker": ("Tracker · Docs", ["/docs/api", "/docs/getting-started"]),
    "/docs/search": ("Search · Docs", ["/docs/getting-started", "/docs/api"]),
    "/blog/reading-the-tides": ("Reading the tides · Blog", ["/", "/blog/self-hosting-analytics", "/pricing"]),
    "/blog/self-hosting-analytics": ("Self-hosting your analytics · Blog", ["/", "/pricing", "/docs/getting-started"]),
    "/blog/whats-new": ("What's new in October · Blog", ["/pricing", "/features"]),
    "/about": ("About · Tidesight", ["/", "/pricing"]),
}
NOT_FOUND = ("Page not found · Tidesight", ["/", "/docs/getting-started"])
BROKEN = {"/docs/install", "/blog/tides-explained", "/docs/configuration"}  # 404 pages (meta ts:status)

# Clickable elements per page: id, page box at 1280px (x, y, w, h), label, href
ELEMENTS = {
    "/": [("cta-hero", 480, 420, 220, 52, "Start free", "/signup"), ("nav-pricing", 860, 28, 70, 24, "Pricing", "/pricing"),
          ("nav-docs", 950, 28, 50, 24, "Docs", "/docs/getting-started"), ("hero-image", 140, 520, 1000, 420, "", None),
          ("feature-replay", 160, 1080, 300, 180, "Session replay", "/features")],
    "/pricing": [("plan-starter", 150, 520, 300, 48, "Choose Starter", "/signup"), ("plan-pro", 490, 520, 300, 48, "Choose Pro", "/signup"),
                 ("plan-team", 830, 520, 300, 48, "Contact sales", "mailto:sales@tidesight.dev"),
                 ("toggle-yearly", 560, 250, 160, 40, "Yearly billing", None), ("faq-1", 240, 900, 800, 56, "Can I self-host?", None),
                 ("faq-2", 240, 970, 800, 56, "Is there a free plan?", None)],
    "/signup": [("signup-submit", 440, 640, 400, 48, "Create account", None), ("terms-link", 500, 710, 120, 20, "Terms", "/about")],
    "/docs/getting-started": [("download-pdf", 900, 300, 200, 40, "Download PDF guide", "/files/tidesight-guide.pdf"),
                              ("code-copy", 860, 620, 80, 30, "Copy", None), ("next-api", 700, 1600, 220, 40, "Next: API", "/docs/api")],
}

FORM_FIELDS = [("email", "email"), ("name", "text"), ("company", "text"), ("password", "password")]


def weighted(rng: random.Random, items: List[Any], key=lambda x: x[0]) -> Any:
    total = sum(key(i) for i in items)
    r = rng.uniform(0, total)
    for i in items:
        r -= key(i)
        if r <= 0:
            return i
    return items[-1]


def build_geo(geo_dir: str) -> List[Tuple[str, Tuple]]:
    """Fake but public /16 ranges, one per city, in GeoLite2-style City and ASN databases."""
    os.makedirs(geo_dir, exist_ok=True)
    nets, city_recs, asn_recs = [], [], []
    for i, c in enumerate(CITIES):
        net = "5.%d.0.0/16" % (10 + i)
        nets.append((net, c))
        city_recs.append((net, {"city": {"names": {"en": c[1]}}, "continent": {"code": "EU"},
                                "country": {"iso_code": c[0], "names": {"en": COUNTRY_NAMES[c[0]]}},
                                "location": {"latitude": c[4], "longitude": c[5], "time_zone": c[6], "accuracy_radius": 20},
                                "subdivisions": [{"iso_code": c[2], "names": {"en": c[3]}}]}))
        asn_recs.append((net, {"autonomous_system_number": 3000 + i, "autonomous_system_organization": ISPS[i % len(ISPS)]}))
    city_recs.append(("52.0.0.0/11", {"country": {"iso_code": "US", "names": {"en": "United States"}},
                                      "location": {"latitude": 39.04, "longitude": -77.49, "time_zone": "America/New_York"}}))
    asn_recs.append(("52.0.0.0/11", {"autonomous_system_number": 16509, "autonomous_system_organization": "AMAZON-02"}))
    write_database(os.path.join(geo_dir, "GeoLite2-City.mmdb"), city_recs, database_type="GeoLite2-City", ipv4_aliases=True)
    write_database(os.path.join(geo_dir, "GeoLite2-ASN.mmdb"), asn_recs, database_type="GeoLite2-ASN")
    return nets


class Poster:
    def __init__(self, cfg) -> None:
        self.app = Collector(ConfigHolder(config=cfg))
        self.n = 0

    def _call(self, method: str, path: str, body: bytes, headers: Dict[str, str], query: str, ip: str, at: int) -> int:
        collect.now_ms = lambda: at
        environ: Dict[str, Any] = {}
        setup_testing_defaults(environ)
        environ.update({"REQUEST_METHOD": method, "PATH_INFO": path, "QUERY_STRING": query, "REMOTE_ADDR": ip,
                        "wsgi.input": io.BytesIO(body), "CONTENT_LENGTH": str(len(body)) if body else "",
                        "wsgi.url_scheme": "https"})
        for k, v in headers.items():
            key = k.upper().replace("-", "_")
            environ[key if key in ("CONTENT_TYPE", "CONTENT_LENGTH") else "HTTP_" + key] = v
        out: Dict[str, str] = {}
        b"".join(self.app(environ, lambda s, h, e=None: out.update(s=s)))
        self.n += 1
        return int(out["s"].split()[0])

    def batch(self, at: int, env: Dict[str, Any], ua: str, ip: str, lang: str) -> None:
        headers = {"Content-Type": "text/plain;charset=UTF-8", "Origin": ORIGIN, "User-Agent": ua, "Accept-Language": lang,
                   "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}
        self._call("POST", "/collect", json.dumps(env).encode(), headers, "", ip, at)

    def chunk(self, at: int, query: str, payload: Any, ua: str, ip: str) -> None:
        body = gzip.compress(json.dumps(payload).encode())
        self._call("POST", "/chunk", body, {"Origin": ORIGIN, "User-Agent": ua, "Content-Type": "text/plain"}, query, ip, at)

    def pixel(self, at: int, query: str, ua: str, ip: str, referer: str) -> None:
        self._call("GET", "/p.gif", b"", {"User-Agent": ua, "Referer": referer}, query, ip, at)


def rid(rng: random.Random, n: int = 16) -> str:
    return "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(n))


# ---- tiny rrweb documents for replays and heatmap snapshots ------------------------------------

def snapshot_dom(path: str, height: int) -> Dict[str, Any]:
    counter = [0]

    def nid() -> int:
        counter[0] += 1
        return counter[0]

    def text(t: str) -> Dict[str, Any]:
        return {"type": 3, "textContent": t, "id": nid()}

    def elem(tag: str, attrs: Optional[Dict[str, str]] = None, children: Optional[List] = None) -> Dict[str, Any]:
        return {"type": 2, "tagName": tag, "attributes": attrs or {}, "childNodes": children or [], "id": nid()}

    css = ("body{margin:0;font:16px/1.5 Georgia,serif;color:#0f2236;background:#f6f3ea}"
           "header{height:80px;background:#0f2236;color:#fff;display:flex;align-items:center;padding:0 100px;gap:24px}"
           "header a{color:#cfe6ff;text-decoration:none} .wrap{width:1080px;margin:0 auto;position:relative}"
           ".abs{position:absolute;display:flex;align-items:center;justify-content:center;border-radius:8px}"
           ".btn{background:#2a78d6;color:#fff;font-weight:600} .ghost{background:#e7eef7;color:#0f2236}"
           ".card{position:absolute;background:#fff;border:1px solid #d9d5c7;border-radius:14px}"
           "h1{position:absolute;left:40px;top:120px;font-size:46px;margin:0;width:1000px;text-align:center}")
    head = elem("head", {}, [elem("style", {"_cssText": css})])
    kids: List[Dict[str, Any]] = [elem("header", {}, [elem("strong", {}, [text("Tidesight")]),
                                                       elem("a", {"id": "nav-pricing", "href": "/pricing"}, [text("Pricing")]),
                                                       elem("a", {"id": "nav-docs", "href": "/docs/getting-started"}, [text("Docs")])])]
    title = PAGES.get(path, ("Page", []))[0].split(" · ")[0]
    inner: List[Dict[str, Any]] = [elem("h1", {}, [text(title)])]
    if path == "/pricing":
        for i, plan in enumerate(["Starter", "Pro", "Team"]):
            inner.append(elem("div", {"class": "card", "style": "left:%dpx;top:300px;width:300px;height:300px" % (50 + 340 * i)},
                              [elem("h2", {"style": "margin:24px"}, [text(plan)])]))
    for (eid, x, y, w, h, label, _href) in ELEMENTS.get(path, []):
        if eid.startswith("nav-"):
            continue
        cls = "abs btn" if eid.startswith(("plan", "cta", "signup", "next", "download")) else "abs ghost"
        inner.append(elem("a" if _href else "div", {"id": eid, "class": cls,
                                                     "style": "left:%dpx;top:%dpx;width:%dpx;height:%dpx" % (x - 100, y - 80, w, h)},
                          [text(label or " ")]))
    kids.append(elem("main", {"class": "wrap", "style": "height:%dpx" % height}, inner))
    body = elem("body", {}, kids)
    html = elem("html", {"lang": "en"}, [head, body])
    return {"type": 0, "childNodes": [{"type": 1, "name": "html", "publicId": "", "systemId": "", "id": nid()}, html], "id": nid()}


def node_ids(node: Dict[str, Any], out: Dict[str, int]) -> Dict[str, int]:
    if node.get("type") == 2 and node.get("attributes", {}).get("id"):
        out[node["attributes"]["id"]] = node["id"]
    for c in node.get("childNodes", []):
        node_ids(c, out)
    return out


def recording(rng: random.Random, t0: int, path: str, width: int, height: int, clicks: List[Tuple[str, int, int]]) -> List[Dict[str, Any]]:
    dom = snapshot_dom(path, 2000)
    ids = node_ids(dom, {})
    events: List[Dict[str, Any]] = [{"type": 4, "timestamp": t0, "data": {"href": ORIGIN + path, "width": width, "height": height}},
                                    {"type": 2, "timestamp": t0 + 5, "data": {"node": dom, "initialOffset": {"left": 0, "top": 0}}}]
    t = t0 + 300
    x, y = width / 2, height / 3
    for (eid, cx, cy) in clicks:
        for _ in range(rng.randint(4, 9)):
            x += (cx - x) * 0.35 + rng.uniform(-15, 15)
            y += (cy - y) * 0.35 + rng.uniform(-15, 15)
            t += rng.randint(60, 180)
            events.append({"type": 3, "timestamp": t, "data": {"source": 1, "positions": [{"x": int(x), "y": int(y), "id": 1, "timeOffset": 0}]}})
        t += 200
        events.append({"type": 3, "timestamp": t, "data": {"source": 2, "type": 2, "id": ids.get(eid, 1), "x": int(cx), "y": int(cy)}})
        if rng.random() < 0.5:
            t += 600
            events.append({"type": 3, "timestamp": t, "data": {"source": 3, "id": 1, "x": 0, "y": rng.randint(200, 900)}})
    return events


# ---- traffic -------------------------------------------------------------------------------------

def simulate(cfg, days: int, visitors: int, seed: int) -> Dict[str, int]:
    rng = random.Random(seed)
    nets = build_geo(cfg.enrich["geo_dir"])
    poster = Poster(cfg)
    end = int(time.time() * 1000)
    start = end - days * DAY
    stats = {"sessions": 0, "replays": 0}
    people = []
    for v in range(visitors):
        # Growth over the period, a newsletter spike 9 days before the end, weekday rhythm.
        while True:
            u = rng.random() ** 0.85
            at = start + int(u * days * DAY)
            d = (at - start) // DAY
            weekday = time.gmtime(at / 1000).tm_wday
            keep = (0.62 if weekday >= 5 else 1.0) * (2.4 if days - d in (9, 10) else 1.0)
            if rng.random() < keep / 2.4:
                break
        net, city = weighted(rng, nets, key=lambda n: n[1][7])
        ip = "5.%s.%d.%d" % (net.split(".")[1], rng.randint(0, 255), rng.randint(1, 254))
        device = weighted(rng, DEVICES)
        people.append({"vid": rid(rng, 20), "vt": at, "ip": ip, "city": city, "device": device, "at": at,
                       "lang": rng.choice(["en-US,en;q=0.9", "fr-FR,fr;q=0.9,en;q=0.8", "de-DE,de;q=0.9", "en-GB,en;q=0.9",
                                           "es-ES,es;q=0.9", "ja-JP,ja;q=0.9", "pt-BR,pt;q=0.9"]),
                       "uid": None})
    people.sort(key=lambda p: p["at"])
    for p in people:
        visits = 1 + (rng.random() < 0.32) + (rng.random() < 0.12) + (rng.random() < 0.05)
        at = p["at"]
        for sn in range(1, visits + 1):
            if at > end - 60000:
                break
            session(rng, poster, p, at, sn, stats)
            at += int(rng.uniform(0.6, 12) * DAY)
    # Bots and no-JS hits.
    for i in range(140):
        at = start + rng.randint(0, days * DAY)
        bot_ua = rng.choice(["Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
                             "Mozilla/5.0 (compatible; AhrefsBot/7.0; +http://ahrefs.com/robot/)",
                             "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.2; +https://openai.com/gptbot)"])
        poster.pixel(at, "site=demo&u=" + ORIGIN + rng.choice(list(PAGES)), bot_ua, "66.249.66.%d" % rng.randint(1, 250), ORIGIN + "/")
    for i in range(40):
        at = start + rng.randint(0, days * DAY)
        vid, sid = rid(rng, 20), rid(rng)
        pid = rid(rng, 12)
        env = {"v": 1, "site": SITE, "vid": vid, "sid": sid, "sent": at, "lib": "ts.js/0.1.0", "try": 0, "events": [
            {"t": "pageview", "id": rid(rng, 12), "ts": at, "pid": pid, "url": ORIGIN + "/pricing", "title": PAGES["/pricing"][0],
             "ref": "", "nav": "navigate", "sn": 1, "pn": 1, "ns": 1, "new": 1, "idsrc": "new"},
            {"t": "fp", "id": rid(rng, 12), "ts": at + 50, "pid": pid, "h": "headless", "fv": 1,
             "bot": {"wd": 1, "pl": 0, "lg": 1, "oz": 1, "hl": 1, "co": 0, "au": ["__pwInitScripts"]}}]}
        poster.batch(at, env, "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/129.0.0.0 Safari/537.36",
                     "52.%d.%d.%d" % (rng.randint(0, 31), rng.randint(0, 255), rng.randint(1, 254)), "en-US")
    stats["requests"] = poster.n
    return stats


def session(rng: random.Random, poster: Poster, p: Dict[str, Any], at: int, sn: int, stats: Dict[str, int]) -> None:
    weight, ua, uad, ctx_over = p["device"]
    country, city_name, *_rest = p["city"]
    tz = p["city"][6]
    sid = rid(rng)
    src = weighted(rng, SOURCES) if sn == 1 or rng.random() < 0.6 else (28, "", "", ["/", "/pricing", "/docs/getting-started"])
    _w, ref, qs, landings = src
    path = rng.choice(landings)
    mobile = ctx_over["vw"] < 700
    vw, vh = ctx_over["vw"], ctx_over["vh"]
    ctx = dict(ctx_over, cd=24, lang=p["lang"].split(",")[0], langs=[p["lang"].split(",")[0]], tz=tz, tzo=0, cs=rng.choice(["light", "light", "dark"]),
               rm=0, ct="no-preference", fc=0, touch=1 if mobile else 0, hc=rng.choice([4, 8, 8, 12]), dm=8,
               net={"type": "wifi", "eff": "4g", "dl": 10, "rtt": 50, "sd": 0}, ck=1, ls=1, pwa="browser", ifr=0, ua=ua)
    if uad:
        ctx["uad"] = uad
    events: List[Dict[str, Any]] = []
    t = at
    referrer = ref
    pages_n = max(1, min(7, int(rng.expovariate(0.55)) + 1))
    replay = rng.random() < 0.05 and not mobile
    rec_clicks: List[Tuple[str, int, int]] = []
    send_at = at
    first = True
    for i in range(pages_n):
        pid = rid(rng, 12)
        url = ORIGIN + path + (("?" + qs) if (first and qs) else "")
        if path == "/docs/search":
            url = ORIGIN + path + "?q=" + rng.choice(["webhooks", "export csv", "privacy mode", "spool", "geoip", "replay"])
        title = NOT_FOUND[0] if path in BROKEN else PAGES.get(path, ("Page", []))[0]
        pv = {"t": "pageview", "id": rid(rng, 12), "ts": t, "pid": pid, "url": url, "title": title, "ref": referrer,
              "nav": "navigate", "sn": sn, "pn": i + 1, "ctx": ctx}
        if path in BROKEN:
            pv["st"] = 404
        if first:
            pv["idsrc"] = "new" if sn == 1 else "cookie"
            if sn == 1:
                pv["new"] = 1
            pv["ns"] = 1
        events.append(pv)
        if first and sn == 1:
            events.append({"t": "fp", "id": rid(rng, 12), "ts": t + 80, "pid": pid, "h": "fp" + rid(rng, 10), "fv": 1,
                           "bot": {"wd": 0, "pl": 5, "lg": 2, "oz": 0, "hl": 0, "co": 1 if "Chrome" in ua else None}})
        # Web Vitals, worse on mobile and on the heavy pages.
        heavy = 1.6 if path in ("/", "/features") else 1.0
        lcp = rng.lognormvariate(math.log(1500 * heavy * (1.7 if mobile else 1.0)), 0.45)
        for name, val in (("LCP", lcp), ("FCP", lcp * rng.uniform(0.45, 0.8)), ("TTFB", rng.lognormvariate(math.log(330), 0.5)),
                          ("CLS", abs(rng.gauss(0.04 if path != "/blog/reading-the-tides" else 0.18, 0.05))),
                          ("INP", rng.lognormvariate(math.log(140 * (1.6 if mobile else 1)), 0.5))):
            good, poor = {"LCP": (2500, 4000), "FCP": (1800, 3000), "TTFB": (800, 1800), "CLS": (0.1, 0.25), "INP": (200, 500)}[name]
            rating = "good" if val <= good else "ni" if val <= poor else "poor"
            events.append({"t": "vital", "id": rid(rng, 12), "ts": t + 2000, "pid": pid, "url": url, "n": name,
                           "val": round(val, 4 if name == "CLS" else 0), "r": rating, "el": "img.hero" if name == "LCP" else None})
        events.append({"t": "perf", "id": rid(rng, 12), "ts": t + 2500, "pid": pid, "url": url, "dns": rng.randint(0, 40),
                       "tcp": rng.randint(10, 60), "tls": rng.randint(15, 70), "ttfb": int(rng.lognormvariate(math.log(330), 0.5)),
                       "dl": rng.randint(5, 80), "dom": int(lcp * 0.8), "dcl": int(lcp * 0.9), "load": int(lcp * 1.3),
                       "proto": "h2", "nt": "navigate"})
        dwell = int(rng.lognormvariate(math.log(28000 if not path.startswith("/blog") else 70000), 0.8))
        if pages_n == 1 and rng.random() < 0.55:  # quick bounces
            dwell = rng.randint(1200, 9000)
        depth = min(100, int(rng.betavariate(2, 1.6) * 100) + (10 if path.startswith("/blog") else 0))
        for mark in (25, 50, 75, 90, 100):
            if depth >= mark:
                events.append({"t": "scroll", "id": rid(rng, 12), "ts": t + int(dwell * mark / 140), "pid": pid, "url": url, "d": mark, "max": depth})
        # Clicks on the page's elements (heatmaps), with element-relative positions.
        for (eid, x, y, w, h, label, href) in ELEMENTS.get(path, []):
            if rng.random() < (0.35 if href else 0.12):
                ex, ey = min(0.98, max(0.02, rng.gauss(0.5, 0.18))), min(0.95, max(0.05, rng.gauss(0.5, 0.2)))
                scale = vw / 1280 if mobile else 1
                cx, cy = (x + ex * w) * scale, (y + ey * h) * scale
                lk = "dl" if href and href.endswith(".pdf") else "mail" if href and href.startswith("mailto:") else None
                ev = {"t": "click", "id": rid(rng, 12), "ts": t + rng.randint(2000, max(2100, dwell)), "pid": pid, "url": url,
                      "x": round(cx), "y": round(cy), "vx": round(cx), "vy": round(cy) % vh, "pw": vw, "ph": 2200, "vw": vw, "vh": vh,
                      "sel": "#" + eid, "tag": "a" if href else "div", "txt": label, "ex": round(ex, 3), "ey": round(ey, 3),
                      "ew": w, "eh": h, "btn": 0, "mod": 0}
                if href:
                    ev["href"] = href if href.startswith("mailto:") else ORIGIN + href
                if lk:
                    ev["lk"] = lk
                events.append(ev)
                rec_clicks.append((eid, int(cx), int(cy)))
        if path == "/pricing" and rng.random() < 0.06:
            events.append({"t": "rage", "id": rid(rng, 12), "ts": t + 4000, "pid": pid, "url": url, "sel": "#toggle-yearly", "n": rng.randint(3, 6),
                           "ms": 900, "x": 640, "y": 270})
        if path == "/pricing" and rng.random() < 0.08:
            events.append({"t": "dead", "id": rid(rng, 12), "ts": t + 5000, "pid": pid, "url": url, "sel": "#faq-2", "tag": "div", "x": 640, "y": 998})
        if path == "/checkout" and rng.random() < 0.09:
            events.append({"t": "error", "id": rid(rng, 12), "ts": t + 3000, "pid": pid, "url": url, "k": "error",
                           "msg": "TypeError: Cannot read properties of undefined (reading 'amount')",
                           "src": ORIGIN + "/assets/checkout.4f2a.js", "line": 212, "col": 33,
                           "stack": "TypeError: Cannot read properties of undefined (reading 'amount')\n    at computeTotal (checkout.4f2a.js:212:33)\n    at HTMLButtonElement.<anonymous> (checkout.4f2a.js:340:9)"})
        if rng.random() < 0.015:
            events.append({"t": "error", "id": rid(rng, 12), "ts": t + 1500, "pid": pid, "url": url, "k": "resource", "tag": "img",
                           "res": ORIGIN + "/img/wave-old.png", "msg": "Failed to load image"})
        if path == "/signup":
            events.append({"t": "form", "id": rid(rng, 12), "ts": t + 1500, "pid": pid, "url": url, "a": "start", "f": "signup"})
            touched = 0
            last = None
            give_up = rng.random() < 0.35
            for k, (fld, ft) in enumerate(FORM_FIELDS):
                if give_up and k >= rng.randint(1, 3):
                    break
                touched += 1
                last = fld
                events.append({"t": "form", "id": rid(rng, 12), "ts": t + 2000 + k * 4000, "pid": pid, "url": url, "a": "blur",
                               "f": "signup", "fld": fld, "ft": ft, "ms": int(rng.lognormvariate(math.log(3500 if ft != "password" else 6500), 0.5)), "filled": 1})
            events.append({"t": "form", "id": rid(rng, 12), "ts": t + 20000, "pid": pid, "url": url, "a": "abandon" if give_up else "submit",
                           "f": "signup", "n": touched, "last": last})
            if not give_up:
                events.append({"t": "event", "id": rid(rng, 12), "ts": t + 20100, "pid": pid, "url": url, "n": "signup",
                               "p": {"plan": rng.choice(["starter", "pro", "pro", "team"]), "method": rng.choice(["email", "github"])}})
                if rng.random() < 0.5:
                    p["uid"] = p["uid"] or "user-" + rid(rng, 6)
                    events.append({"t": "identify", "id": rid(rng, 12), "ts": t + 20200, "pid": pid, "url": url, "uid": p["uid"],
                                   "traits": {"plan": "pro"}})
        if path == "/thank-you":
            plan = rng.choice([("pro", 29.0), ("pro", 29.0), ("team", 99.0), ("starter", 9.0)])
            events.append({"t": "event", "id": rid(rng, 12), "ts": t + 1000, "pid": pid, "url": url, "n": "purchase", "val": plan[1],
                           "p": {"plan": plan[0], "currency": "EUR", "value": plan[1]}})
        if path == "/docs/getting-started" and rng.random() < 0.2:
            events.append({"t": "event", "id": rid(rng, 12), "ts": t + 9000, "pid": pid, "url": url, "n": "copy_snippet", "p": {"lang": "html"}})
        events.append({"t": "engagement", "id": rid(rng, 12), "ts": t + dwell, "pid": pid, "url": url, "ms": int(dwell * rng.uniform(0.55, 0.95)),
                       "tot": dwell, "why": "route" if i < pages_n - 1 else "hidden", "sd": depth})
        if replay:
            stats["replays"] += 1 if first else 0
        # Next page.
        nxt = NOT_FOUND[1] if path in BROKEN else PAGES.get(path, ("", ["/"]))[1]
        referrer = url
        t += dwell + rng.randint(500, 3000)
        if path == "/docs/api" and rng.random() < 0.06:
            path = "/docs/configuration"  # a broken internal link
        else:
            path = rng.choice(nxt + (["/docs/search"] if path.startswith("/docs") and rng.random() < 0.2 else []))
        first = False
    env = {"v": 1, "site": SITE, "vid": p["vid"], "vt": p["vt"], "sid": sid, "uid": p["uid"], "sent": t, "lib": "ts.js/0.1.0", "try": 0,
           "events": events}
    poster.batch(min(t, int(time.time() * 1000) - 1000), env, ua, p["ip"], p["lang"])
    stats["sessions"] += 1
    landing = events[0]["url"].split(ORIGIN, 1)[1].split("?")[0]
    if replay:
        rec = recording(rng, at, landing, vw, vh, rec_clicks[:4] or [("cta-hero", 590, 446)])
        poster.chunk(at + 15000, "site=demo&k=replay&vid=%s&sid=%s&pid=%s&seq=0&ts=%d&rec=%s" % (p["vid"], sid, events[0]["pid"], at, rid(rng, 8)),
                     rec, ua, p["ip"])
    if landing in ("/pricing", "/") and rng.random() < 0.25 and not mobile:
        pts = [[int(rng.gauss(640, 260)), int(abs(rng.gauss(500, 380))), k * 100] for k in range(120)]
        poster.chunk(at + 9000, "site=demo&k=mouse&vid=%s&sid=%s&pid=%s&seq=0&ts=%d&url=%s" % (p["vid"], sid, events[0]["pid"], at, ORIGIN + landing),
                     {"pw": vw, "ph": 2200, "vw": vw, "vh": vh, "pts": pts}, ua, p["ip"])


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="demo-data", help="folder for the demo config and data (default demo-data)")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--visitors", type=int, default=4000)
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args(argv)
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    cfg_path = os.path.join(out, "tidesight.toml")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        fh.write("""# Demo configuration written by tools/demo.py
[server]
data_dir = "data"
rate_limit_per_minute = 0
secret = "demo-secret"

[enrich]
geo_dir = "data/geo"

[sites.demo]
name = "Tidewater Supply Co."
origins = ["%s"]
timezone = "Europe/Paris"
public = true
public_metrics = ["visitors", "pageviews", "pages", "sources", "countries", "devices", "realtime"]
features.replay = true
replay.sample_rate = 0.05
features.mouse_moves = true
features.dom_snapshots = true
""" % ORIGIN)
    cfg = load(cfg_path)
    data_dir = cfg.data_dir
    for name in ("events.db", "replay.db", "derived.db"):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(os.path.join(data_dir, name + suffix))
            except OSError:
                pass
    t0 = time.time()
    stats = simulate(cfg, args.days, args.visitors, args.seed)
    # DOM snapshots of the main pages (heatmap backgrounds).
    poster = Poster(cfg)
    now = int(time.time() * 1000)
    for path in ("/", "/pricing", "/signup", "/docs/getting-started"):
        dom = snapshot_dom(path, 2200)
        poster.chunk(now - 3600000, "site=demo&k=snapshot&vid=snapvisitor0001&sid=snapsession01&pid=snap%s&seq=0&ts=%d&url=%s"
                     % (path.replace("/", "_"), now, ORIGIN + path),
                     [{"type": 4, "timestamp": now, "data": {"href": ORIGIN + path, "width": 1280, "height": 2200}},
                      {"type": 2, "timestamp": now + 1, "data": {"node": dom, "initialOffset": {"left": 0, "top": 0}}}], "demo", "5.10.0.1")
    collect.now_ms = lambda: int(time.time() * 1000)
    print("posted %(sessions)d visits (%(requests)d requests), %(replays)d recordings" % stats, flush=True)
    result = pipeline.run(cfg)
    print("enrichment: %d hits, %d sessions in %.1f s" % (result["hits"], result["sessions"], time.time() - t0))
    print("\nNext:\n  python server/python/devserver.py --config %s --no-auth --port 8790\n  open http://127.0.0.1:8790/dashboard/"
          % os.path.relpath(cfg_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
