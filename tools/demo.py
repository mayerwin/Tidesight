#!/usr/bin/env python3
"""Demo data: three made-up websites and their visitors, sent through Tidesight's real collector and jobs.

    python tools/demo.py --out demo-data           # about a minute
    python server/python/devserver.py --config demo-data/tidesight.toml --no-auth --port 8790
    # then open http://127.0.0.1:8790/dashboard/

The properties, under .example (a domain no one can own):

    demo      Tidewater Supply Co.  a surf shop: collections, products, cart, checkout, purchases, accounts
    journal   Saltwater Journal     a magazine with a newsletter: long reads, search and social traffic, subscribers
    harbor    Harbor                a web app: pricing, sign-ups, documentation, a single-page app with user ids

Sixty days of traffic with growth, a weekly rhythm and a few remarkable days (a newsletter and an Instagram post,
a day on Hacker News, a launch on Product Hunt), returning visitors, campaigns, AI assistants, engagement and
scroll, clicks for heatmaps, rage and dead clicks, forms, Web Vitals, errors, site search, bots, recordings,
DOM snapshots, and the last half hour of visits for the Realtime page. With --team (the default), a few people,
annotations and saved segments too. Geolocation uses small generated databases (made-up, public IP ranges).

Options: --sites demo,journal (some of them), --visitors N (the shop's; the others in proportion), --light
(fewer events per page, as with sampled plugins: the in-browser live demo), --demo (a read-only dashboard open
to anyone, [dashboard] demo). The in-browser live demo (site/demo) imports this file too: live() adds the
visits of the last minutes.
"""

from __future__ import annotations

import argparse
import gzip
import io
import json
import math
import os
import random
import secrets
import sys
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from wsgiref.util import setup_testing_defaults

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, "server", "python"))

from tidesight import collect  # noqa: E402
from tidesight.collect import Collector  # noqa: E402
from tidesight.config import ConfigHolder, load  # noqa: E402

DAY = 86400000
MINUTE = 60000

# ---- the world: places, devices ----------------------------------------------------------------

CITIES = [  # (country, city, region code, region name, lat, lon, time zone, weight)
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
    ("PT", "Lisbon", "11", "Lisbon", 38.72, -9.14, "Europe/Lisbon", 3),
    ("PT", "Porto", "13", "Porto", 41.15, -8.61, "Europe/Lisbon", 2),
    ("FR", "Biarritz", "NAQ", "Nouvelle-Aquitaine", 43.48, -1.56, "Europe/Paris", 2),
    ("US", "Honolulu", "HI", "Hawaii", 21.31, -157.86, "Pacific/Honolulu", 2),
    ("ZA", "Cape Town", "WC", "Western Cape", -33.92, 18.42, "Africa/Johannesburg", 1),
    ("IE", "Dublin", "L", "Leinster", 53.35, -6.26, "Europe/Dublin", 2),
]
COUNTRY_NAMES = {"US": "United States", "FR": "France", "PF": "French Polynesia", "GB": "United Kingdom", "DE": "Germany",
                 "CA": "Canada", "AU": "Australia", "JP": "Japan", "BR": "Brazil", "IN": "India", "NL": "Netherlands",
                 "ES": "Spain", "IT": "Italy", "SE": "Sweden", "SG": "Singapore", "NZ": "New Zealand", "MX": "Mexico",
                 "PT": "Portugal", "ZA": "South Africa", "IE": "Ireland"}
ISPS = ["Comcast Cable", "Orange", "Vodafone", "Deutsche Telekom", "Rogers", "Telstra", "NTT", "Vivo", "Jio", "KPN",
        "Telefonica", "TIM", "Telia", "Singtel", "Spark", "Telmex", "Free SAS", "Vini", "BT", "Bell Canada", "MEO",
        "NOS", "Bouygues Telecom", "Spectrum", "Vodacom", "Eir"]
LANGS = {"US": "en-US,en;q=0.9", "GB": "en-GB,en;q=0.9", "IE": "en-IE,en;q=0.9", "CA": "en-CA,en;q=0.9",
         "AU": "en-AU,en;q=0.9", "NZ": "en-NZ,en;q=0.9", "SG": "en-SG,en;q=0.9", "IN": "en-IN,en;q=0.9",
         "ZA": "en-ZA,en;q=0.9", "FR": "fr-FR,fr;q=0.9,en;q=0.8", "PF": "fr-FR,fr;q=0.9", "DE": "de-DE,de;q=0.9,en;q=0.7",
         "JP": "ja-JP,ja;q=0.9", "BR": "pt-BR,pt;q=0.9", "PT": "pt-PT,pt;q=0.9,en;q=0.8", "NL": "nl-NL,nl;q=0.9,en;q=0.8",
         "ES": "es-ES,es;q=0.9", "IT": "it-IT,it;q=0.9", "SE": "sv-SE,sv;q=0.9,en;q=0.8", "MX": "es-MX,es;q=0.9"}

DEVICES = [  # (weight, user agent, userAgentData or None, context)
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
BOT_UAS = ["Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
           "Mozilla/5.0 (compatible; AhrefsBot/7.0; +http://ahrefs.com/robot/)",
           "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; GPTBot/1.2; +https://openai.com/gptbot)",
           "Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)"]
HEADLESS_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/129.0.0.0 Safari/537.36"


def weighted(rng: random.Random, items: Sequence[Any], key: Callable[[Any], float] = lambda x: x[0]) -> Any:
    total = sum(key(i) for i in items)
    r = rng.uniform(0, total)
    for i in items:
        r -= key(i)
        if r <= 0:
            return i
    return items[-1]


def rid(rng: random.Random, n: int = 16) -> str:
    return "".join(rng.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(n))


def network(i: int) -> str:
    """The made-up /16 of CITIES[i]: the generated geo databases place it there."""
    return "5.%d.0.0/16" % (10 + i)


def build_geo(geo_dir: str) -> None:
    """GeoLite2-style City and ASN databases for the made-up ranges (and a hosting range for the bots)."""
    sys.path.insert(0, os.path.join(REPO, "server", "python", "tests"))
    from mmdb_writer import write_database  # test helper: builds MaxMind DB files

    os.makedirs(geo_dir, exist_ok=True)
    city_recs, asn_recs = [], []
    for i, c in enumerate(CITIES):
        net = network(i)
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


# ---- made-up pages: blocks at fixed places (1280 px wide), drawn for heatmaps and recordings --------
# A block: (id or "", x, y, w, h, kind, text, href, click weight). Clicks land on blocks in proportion to their
# weight, at a place inside them; the DOM snapshot draws every block where it is, so heatmaps line up.

Block = Tuple[str, int, int, int, int, str, str, Optional[str], float]


def B(bid: str, x: int, y: int, w: int, h: int, kind: str, text: str = "", href: Optional[str] = None,
      weight: float = 0.0) -> Block:
    return (bid, x, y, w, h, kind, text, href, weight)


class Page:
    def __init__(self, title: str, nxt: Sequence[Any], blocks: Optional[List[Block]] = None, height: int = 1800,
                 dwell: float = 30.0, kind: str = "page") -> None:
        self.title, self.blocks, self.height, self.dwell, self.kind = title, blocks or [], height, dwell, kind
        self.next = [(n, 1.0) if isinstance(n, str) else n for n in nxt]


class Source:
    def __init__(self, weight: float, ref: str, query: str, landings: Sequence[str], bounce: float = 0.4) -> None:
        self.weight, self.ref, self.query, self.landings, self.bounce = weight, ref, query, list(landings), bounce


def header(brand: str, links: Sequence[Tuple[str, str]], cta: Optional[Tuple[str, str, str]] = None) -> List[Block]:
    out = [B("", 0, 0, 1280, 72, "bar"), B("logo", 64, 22, 200, 28, "logo", brand, "/", 0.6)]
    x = 340
    for bid, label_href in zip(("nav-1", "nav-2", "nav-3", "nav-4", "nav-5"), links):
        label, href = label_href
        out.append(B(bid, x, 26, 12 + 9 * len(label), 22, "nav", label, href, 1.2))
        x += 40 + 9 * len(label)
    if cta:
        out.append(B(cta[0], 1060, 16, 156, 40, "btn", cta[1], cta[2], 1.4))
    return out


# ---- the properties --------------------------------------------------------------------------------

class Site:
    """A made-up website: its pages, where its visitors come from, and what they do there."""

    id = ""
    name = ""
    origin = ""
    timezone = "UTC"
    share = 1.0                    # visitors, relative to the shop's
    busy = 1.0                     # visits of the last half hour (Realtime), relative to the shop's
    countries: Dict[str, float] = {}
    pages: Dict[str, Page] = {}
    sources: List[Source] = []
    goals: List[Dict[str, str]] = []
    settings: Dict[str, Any] = {}
    spikes: List[Tuple[int, float, Optional[Source], str]] = []  # (days before the end, factor, source, annotation)
    notes: List[Tuple[int, str]] = []                             # other annotations: (days before the end, text)
    returns = (0.30, 0.10, 0.04)                                  # chances of a 2nd, 3rd, 4th visit
    return_days = (0.6, 12.0)
    broken: Dict[str, str] = {}                                   # paths answering 404, and their titles
    search_path = "/search"
    searches: List[str] = []
    errors: List[Tuple[str, float, Dict[str, Any]]] = []         # (path prefix, chance per page, error)
    rage: List[Tuple[str, str, float]] = []                       # (path prefix, element id, chance per page)
    dead: List[Tuple[str, str, float]] = []
    recorded = 0.05                                               # recordings: a share of desktop visits
    mouse = ("/",)                                                # pages with mouse movement chunks

    def table(self) -> Dict[str, Any]:
        """The property as properties.json holds it."""
        return dict({"name": self.name, "origins": [self.origin], "timezone": self.timezone, "goals": self.goals},
                    **self.settings)

    def place_weight(self, city: Tuple) -> float:
        return city[7] * self.countries.get(city[0], 1.0)

    def page(self, path: str) -> Page:
        return self.pages.get(path) or Page(self.broken.get(path, "Page not found"), ["/"])

    # What a visitor does on a page, beyond pageviews, engagement, scroll and clicks: events, forms, and where
    # they go next (None: the page's usual links).
    def on_page(self, v: "Visit", path: str, t: int, dwell: int) -> Optional[str]:
        return None


class Shop(Site):
    id = "demo"
    name = "Tidewater Supply Co."
    origin = "https://tidewater.example"
    timezone = "Europe/Paris"
    share, busy = 1.0, 1.0
    countries = {"FR": 2.2, "PT": 1.8, "ES": 1.6, "GB": 1.3, "IE": 1.4, "DE": 1.1, "US": 0.9, "AU": 1.2, "IN": 0.3, "JP": 0.6}
    goals = [{"name": "Viewed a product", "type": "page", "match": "/products/*"},
             {"name": "Added to cart", "type": "event", "match": "add_to_cart"},
             {"name": "Purchase", "type": "event", "match": "purchase"}]
    settings = {"features": {"replay": True, "mouse_moves": True, "dom_snapshots": True}, "replay": {"sample_rate": 0.05},
                "public": True, "public_metrics": ["visitors", "pageviews", "pages", "sources", "countries", "devices", "realtime"]}
    returns = (0.32, 0.12, 0.05)
    searches = ["hooded wetsuit", "5/4", "booties", "wax", "leash", "kids wetsuit", "sunscreen", "fins", "poncho", "4/3 chest zip"]
    broken = {"/products/winter-sale-wetsuit": "Page not found · Tidewater", "/collections/sale": "Page not found · Tidewater"}
    mouse = ("/", "/products/coldwater-5-4-hooded")

    PRODUCTS = {  # path: (name, price, category)
        "/products/coldwater-5-4-hooded": ("Coldwater 5/4 Hooded Wetsuit", 289.0, "Wetsuits"),
        "/products/summit-4-3-chest-zip": ("Summit 4/3 Chest-Zip Wetsuit", 239.0, "Wetsuits"),
        "/products/shorty-2mm": ("Shorty 2 mm Spring Suit", 119.0, "Wetsuits"),
        "/products/driftwood-9-2-longboard": ("Driftwood 9'2\" Longboard", 749.0, "Surfboards"),
        "/products/swallowtail-5-10-fish": ("Swallowtail 5'10\" Fish", 629.0, "Surfboards"),
        "/products/reef-safe-sunscreen": ("Reef-Safe Mineral Sunscreen", 18.0, "Accessories"),
        "/products/tide-watch": ("Tide Watch, Steel", 179.0, "Accessories"),
        "/products/neoprene-booties": ("Neoprene Booties 5 mm", 49.0, "Accessories"),
    }

    def __init__(self) -> None:
        nav = [("Wetsuits", "/collections/wetsuits"), ("Surfboards", "/collections/surfboards"),
               ("Accessories", "/collections/accessories"), ("Journal", "/journal/winter-wetsuit-guide")]
        top = header("TIDEWATER", nav) + [B("nav-search", 1010, 22, 150, 30, "input", "Search", None, 0.8),
                                          B("nav-cart", 1176, 22, 44, 30, "btn2", "Cart", "/cart", 1.0)]
        self.pages = {
            "/": Page("Tidewater Supply Co. — Surf gear for cold water", [("/collections/wetsuits", 5), ("/collections/surfboards", 3),
                      ("/collections/accessories", 2), ("/products/coldwater-5-4-hooded", 2), ("/journal/winter-wetsuit-guide", 1)],
                      top + [B("hero", 0, 72, 1280, 520, "hero"), B("", 120, 210, 640, 120, "h1x", "Built for cold water."),
                             B("", 120, 340, 560, 60, "lead", "Wetsuits, boards and the small things that make a dawn session."),
                             B("cta-shop", 120, 430, 200, 52, "btn", "Shop wetsuits", "/collections/wetsuits", 9),
                             B("cta-boards", 336, 430, 190, 52, "btn2l", "Explore boards", "/collections/surfboards", 4),
                             B("", 64, 640, 600, 40, "h2", "Shop by category"),
                             B("cat-wetsuits", 64, 700, 368, 320, "img", "Wetsuits", "/collections/wetsuits", 5),
                             B("cat-boards", 456, 700, 368, 320, "img2", "Surfboards", "/collections/surfboards", 3),
                             B("cat-accessories", 848, 700, 368, 320, "img3", "Accessories", "/collections/accessories", 2),
                             B("", 64, 1080, 1152, 220, "card"), B("", 104, 1120, 600, 40, "h2", "Swell alerts, new drops, no spam."),
                             B("news-email", 104, 1190, 380, 48, "input", "you@example.com", None, 1.5),
                             B("news-submit", 500, 1190, 150, 48, "btn", "Subscribe", None, 2.5)],
                      height=1400, dwell=25),
            "/collections/wetsuits": self._collection("Wetsuits", ["/products/coldwater-5-4-hooded", "/products/summit-4-3-chest-zip",
                                                                   "/products/shorty-2mm", "/products/neoprene-booties"], top),
            "/collections/surfboards": self._collection("Surfboards", ["/products/driftwood-9-2-longboard",
                                                                       "/products/swallowtail-5-10-fish"], top),
            "/collections/accessories": self._collection("Accessories", ["/products/reef-safe-sunscreen", "/products/tide-watch",
                                                                         "/products/neoprene-booties"], top),
            "/cart": Page("Your cart · Tidewater", [("/checkout", 6), ("/collections/wetsuits", 1)], top + [
                B("", 64, 110, 400, 44, "h1", "Your cart"), B("", 64, 180, 760, 140, "card"), B("", 84, 200, 100, 100, "img"),
                B("", 204, 214, 400, 26, "h3", "Coldwater 5/4 Hooded Wetsuit"), B("", 204, 248, 200, 22, "note", "Size M · Black"),
                B("cart-remove", 700, 214, 100, 24, "link", "Remove", None, 1.0), B("", 864, 180, 352, 300, "card"),
                B("", 884, 204, 200, 24, "h3", "Summary"), B("coupon", 884, 300, 200, 44, "input", "Gift card or code", None, 1.6),
                B("coupon-apply", 1096, 300, 100, 44, "btn2", "Apply", None, 1.8),
                B("checkout", 884, 400, 312, 52, "btn", "Check out", "/checkout", 9)], height=900, dwell=30),
            "/checkout": Page("Checkout · Tidewater", [("/checkout/thank-you", 6), ("/cart", 1)], top + [
                B("", 64, 110, 400, 44, "h1", "Checkout"), B("", 64, 180, 700, 520, "card"),
                B("f-email", 90, 210, 640, 46, "input", "Email", None, 1.5), B("f-name", 90, 276, 640, 46, "input", "Full name", None, 1.2),
                B("f-address", 90, 342, 640, 46, "input", "Address", None, 1.2), B("f-postcode", 90, 408, 300, 46, "input", "Postcode", None, 1),
                B("pay", 90, 600, 640, 54, "btn", "Pay now", None, 8)], height=900, dwell=70),
            "/checkout/thank-you": Page("Thank you for your order · Tidewater", [("/", 2), ("/account/register", 3)], dwell=20),
            "/account/register": Page("Create an account · Tidewater", [("/", 1)], dwell=40),
            "/journal/winter-wetsuit-guide": Page("How to choose a winter wetsuit · The Tidewater Journal",
                                                  [("/products/coldwater-5-4-hooded", 3), ("/collections/wetsuits", 2)], dwell=95),
            "/journal/reading-a-surf-forecast": Page("Reading a surf forecast · The Tidewater Journal", [("/", 1), ("/collections/surfboards", 1)],
                                                     dwell=110),
            "/pages/size-guide": Page("Size guide · Tidewater", [("/products/coldwater-5-4-hooded", 2), ("/collections/wetsuits", 1)], dwell=35),
            "/pages/shipping": Page("Shipping and returns · Tidewater", [("/cart", 1), ("/", 1)], dwell=30),
            "/search": Page("Search · Tidewater", [("/products/coldwater-5-4-hooded", 2), ("/products/neoprene-booties", 1),
                                                   ("/collections/accessories", 1)], dwell=15),
        }
        for path, (name, price, category) in self.PRODUCTS.items():
            self.pages[path] = self._product(name, price, category, top)
        self.sources = [
            Source(25, "", "", ["/", "/collections/wetsuits", "/cart"], 0.35),
            Source(24, "https://www.google.com/", "", ["/", "/collections/wetsuits", "/products/coldwater-5-4-hooded",
                                                       "/journal/winter-wetsuit-guide", "/products/reef-safe-sunscreen"], 0.45),
            Source(3, "https://www.bing.com/", "", ["/", "/collections/wetsuits"], 0.45),
            Source(2, "https://duckduckgo.com/", "", ["/", "/products/tide-watch"], 0.45),
            Source(8, "https://l.instagram.com/", "utm_source=instagram&utm_medium=social&utm_campaign=autumn-drop",
                   ["/products/coldwater-5-4-hooded", "/", "/products/shorty-2mm"], 0.55),
            Source(3, "https://m.facebook.com/", "", ["/", "/collections/surfboards"], 0.55),
            Source(3, "https://www.pinterest.com/", "", ["/journal/winter-wetsuit-guide", "/products/driftwood-9-2-longboard"], 0.6),
            Source(6, "", "utm_source=newsletter&utm_medium=email&utm_campaign=autumn-drop", ["/collections/wetsuits", "/products/summit-4-3-chest-zip"], 0.3),
            Source(7, "https://www.google.com/", "gclid=EAIaIQobChMI&utm_source=google&utm_medium=cpc&utm_campaign=shopping-wetsuits",
                   ["/products/coldwater-5-4-hooded", "/products/summit-4-3-chest-zip", "/collections/wetsuits"], 0.5),
            Source(3, "https://chatgpt.com/", "utm_source=chatgpt.com", ["/journal/winter-wetsuit-guide", "/products/coldwater-5-4-hooded"], 0.4),
            Source(1, "https://www.perplexity.ai/", "", ["/journal/reading-a-surf-forecast"], 0.4),
            Source(2, "https://www.reddit.com/r/surfing/", "", ["/journal/winter-wetsuit-guide", "/journal/reading-a-surf-forecast"], 0.55),
            Source(2, "https://www.surfline.com/", "", ["/journal/reading-a-surf-forecast", "/collections/surfboards"], 0.5),
            Source(1, "https://www.youtube.com/", "", ["/journal/winter-wetsuit-guide"], 0.6),
            Source(1, "https://l.instagram.com/", "", ["/products/winter-sale-wetsuit", "/collections/sale"], 0.8),  # old links
        ]
        self.spikes = [(12, 2.3, Source(1, "", "utm_source=newsletter&utm_medium=email&utm_campaign=autumn-drop",
                                        ["/collections/wetsuits", "/products/coldwater-5-4-hooded"], 0.3), "Autumn drop: newsletter and Instagram")]
        self.notes = [(34, "Product photos resized (faster pages)")]
        self.errors = [
            ("/checkout", 0.07, {"k": "error", "msg": "TypeError: Cannot read properties of undefined (reading 'shippingRate')",
                                 "src": self.origin + "/assets/checkout.8c1e.js", "line": 214, "col": 31,
                                 "stack": "TypeError: Cannot read properties of undefined (reading 'shippingRate')\n"
                                          "    at quote (checkout.8c1e.js:214:31)\n    at HTMLFormElement.<anonymous> (checkout.8c1e.js:402:7)"}),
            ("/", 0.012, {"k": "resource", "tag": "img", "res": self.origin + "/img/lookbook/autumn-hero.webp", "msg": "Failed to load image"}),
            ("/products/", 0.02, {"k": "error", "msg": "ReferenceError: reviewsWidget is not defined", "src": self.origin + "/assets/product.31aa.js",
                                  "line": 88, "col": 5, "stack": "ReferenceError: reviewsWidget is not defined\n    at product.31aa.js:88:5"}),
            ("/cart", 0.03, {"k": "rejection", "msg": "NetworkError when attempting to fetch resource.", "src": self.origin + "/assets/cart.77b0.js",
                             "line": 0, "col": 0, "stack": ""}),
        ]
        self.rage = [("/products/", "size-guide", 0.05), ("/cart", "coupon-apply", 0.04)]
        self.dead = [("/products/", "gallery-main", 0.07), ("/", "hero", 0.03)]

    def _collection(self, title: str, products: List[str], top: List[Block]) -> Page:
        blocks = top + [B("", 64, 110, 600, 50, "h1", title), B("filter-size", 64, 180, 140, 38, "btn2", "Size", None, 1.5),
                        B("filter-thickness", 216, 180, 160, 38, "btn2", "Thickness", None, 1.2),
                        B("sort", 1056, 180, 160, 38, "btn2", "Sort: Featured", None, 0.8)]
        for i, path in enumerate(products):
            x, y = 64 + (i % 3) * 392, 250 + (i // 3) * 470
            name, price, _category = self.PRODUCTS[path]
            blocks += [B("card-%d" % (i + 1), x, y, 368, 360, "img%d" % (i % 3 + 1), "", path, 6 - i),
                       B("", x, y + 376, 368, 26, "h3", name), B("", x, y + 408, 120, 24, "price", "€%d" % price)]
        return Page(title + " · Tidewater Supply Co.", [(p, 6 - i) for i, p in enumerate(products)] + [("/search", 1)],
                    blocks, height=250 + ((len(products) + 2) // 3) * 470 + 200, dwell=28)

    def _product(self, name: str, price: float, category: str, top: List[Block]) -> Page:
        blocks = top + [B("", 64, 96, 400, 20, "note", "Home / %s / %s" % (category, name)),
                        B("gallery-main", 64, 130, 600, 600, "img", "", None, 2.5)]
        for i in range(4):
            blocks.append(B("thumb-%d" % (i + 1), 64 + i * 152, 746, 140, 100, "img%d" % (i % 3 + 1), "", None, 1.6 - i * 0.3))
        blocks += [B("", 720, 130, 496, 80, "h1", name), B("", 720, 226, 160, 34, "pricexl", "€%d" % price),
                   B("", 720, 270, 200, 22, "stars", "★★★★★  128 reviews"), B("", 720, 320, 200, 22, "label", "Size"),
                   B("size-s", 720, 350, 72, 44, "chip", "S", None, 2.2), B("size-m", 804, 350, 72, 44, "chip", "M", None, 4.5),
                   B("size-l", 888, 350, 72, 44, "chip", "L", None, 3.4), B("size-xl", 972, 350, 72, 44, "chip", "XL", None, 1.5),
                   B("size-guide", 1060, 360, 110, 24, "link", "Size guide", None, 2.2),
                   B("add-to-cart", 720, 430, 496, 56, "btn", "Add to cart", None, 8),
                   B("", 720, 500, 496, 22, "note", "Free shipping over €100 · Free returns for 60 days"),
                   B("tab-details", 720, 560, 120, 36, "tab", "Details", None, 1.0),
                   B("tab-reviews", 852, 560, 120, 36, "tab", "Reviews", None, 1.8),
                   B("tab-shipping", 984, 560, 120, 36, "tab", "Shipping", None, 0.8),
                   B("", 720, 612, 496, 160, "p", "Glued and blind-stitched seams, a thermal lining down to the knees and a hood that stays "
                                                  "put when the sets roll in. Made for winter water, from 8 to 12 °C.")]
        return Page(name + " · Tidewater", [("/cart", 3), ("/collections/" + category.lower(), 2), ("/pages/size-guide", 1),
                                            ("/pages/shipping", 1)],
                    blocks, height=1500, dwell=55, kind="product")

    def on_page(self, v: "Visit", path: str, t: int, dwell: int) -> Optional[str]:
        rng = v.rng
        if path in self.PRODUCTS and rng.random() < 0.19:
            name, price, _ = self.PRODUCTS[path]
            size = rng.choice(["S", "M", "M", "L", "L", "XL"])
            v.event(t + int(dwell * 0.7), "event", n="add_to_cart", val=price, p={"product": name, "size": size, "price": price})
            v.cart.append(price)
            return "/cart"
        if path == "/checkout":
            done = v.form(t + 1500, "checkout", [("email", "email"), ("name", "text"), ("address", "text"), ("postcode", "text"),
                                                 ("country", "select-one")], abandon=0.3)
            if done and v.cart:
                return "/checkout/thank-you"
            return "/cart" if rng.random() < 0.3 else ""
        if path == "/checkout/thank-you":
            total = round(sum(v.cart) or rng.choice([49.0, 119.0, 289.0]), 2)
            v.event(t + 800, "event", n="purchase", val=total, p={"value": total, "currency": "EUR", "items": max(1, len(v.cart))})
            v.cart = []
            if rng.random() < 0.5:
                v.identify(t + 900, "cust-" + rid(rng, 6), {"customer": True})
        if path == "/account/register":
            if v.form(t + 1200, "register", [("email", "email"), ("name", "text"), ("password", "password")], abandon=0.35):
                v.event(t + 20000, "event", n="signup", p={"method": rng.choice(["email", "email", "google"])})
                v.identify(t + 20100, "cust-" + rid(rng, 6), {"customer": False})
        if path == "/" and rng.random() < 0.03:
            if v.form(t + 4000, "newsletter", [("email", "email")], abandon=0.2):
                v.event(t + 9000, "event", n="newsletter_signup", p={"placement": "home"})
        return None


class Journal(Site):
    id = "journal"
    name = "Saltwater Journal"
    origin = "https://saltwater-journal.example"
    timezone = "Europe/Lisbon"
    share, busy = 0.7, 0.6
    countries = {"US": 1.5, "GB": 1.4, "AU": 1.6, "PT": 1.8, "IE": 1.3, "NZ": 1.6, "ZA": 1.8, "FR": 0.9, "IN": 0.6, "JP": 0.5}
    goals = [{"name": "Read a story", "type": "page", "match": "/stories/*"},
             {"name": "Subscribed", "type": "event", "match": "subscribe"}]
    settings = {"features": {"dom_snapshots": True}}
    returns = (0.38, 0.22, 0.12)
    return_days = (3.0, 9.0)
    searches = ["tide tables", "wetsuit thickness", "portugal", "beginner", "forecast", "plastic", "longboard"]
    broken = {"/stories/tides-explained": "Page not found · Saltwater Journal"}
    recorded = 0.0
    mouse = ()

    STORIES = {
        "/stories/the-perfect-wave-forecast": ("The perfect wave forecast does not exist", "#204060,#6aa5c9"),
        "/stories/reading-the-tides": ("Reading the tides, a beginner's guide", "#2a4d55,#9cc9c2"),
        "/stories/cold-water-confidence": ("Cold-water confidence", "#1e2d3d,#7f9bb3"),
        "/stories/surf-trips-portugal": ("Ten days, one van, the coast of Portugal", "#b06a3b,#f2c38f"),
        "/stories/plastic-free-surfing": ("Can surfing go plastic-free?", "#2f5d3a,#a7d3a1"),
        "/stories/the-shaper": ("The last shaper in town", "#6b4f3a,#d8bfa0"),
    }

    def __init__(self) -> None:
        nav = [("Stories", "/"), ("Travel", "/topics/travel"), ("Craft", "/topics/craft"), ("Newsletter", "/newsletter")]
        top = header("Saltwater Journal", nav, ("nav-subscribe", "Subscribe", "/newsletter"))
        stories = list(self.STORIES)
        home = top + [B("", 64, 110, 900, 60, "h1x", "Stories from the edge of the sea")]
        for i, path in enumerate(stories[:6]):
            x, y = 64 + (i % 3) * 392, 210 + (i // 3) * 430
            home += [B("story-%d" % (i + 1), x, y, 368, 240, "img%d" % (i % 3 + 1), "", path, 6 - i * 0.8),
                     B("", x, y + 256, 368, 56, "h3", self.STORIES[path][0])]
        self.pages = {"/": Page("Saltwater Journal — stories from the edge of the sea", [(p, 6 - i) for i, p in enumerate(stories)] +
                                [("/newsletter", 1)], home, height=1200, dwell=25),
                      "/topics/travel": Page("Travel · Saltwater Journal", [("/stories/surf-trips-portugal", 3), ("/", 1)], dwell=20),
                      "/topics/craft": Page("Craft · Saltwater Journal", [("/stories/the-shaper", 3), ("/", 1)], dwell=20),
                      "/newsletter": Page("The newsletter · Saltwater Journal", [("/", 1)], dwell=30),
                      "/about": Page("About · Saltwater Journal", [("/", 1)], dwell=30),
                      "/search": Page("Search · Saltwater Journal", [(p, 1) for p in stories], dwell=12)}
        for path, (title, _grad) in self.STORIES.items():
            self.pages[path] = Page(title + " · Saltwater Journal", [(p, 1) for p in stories if p != path] + [("/newsletter", 1)],
                                    top + [B("", 240, 120, 800, 120, "h1x", title), B("", 240, 260, 400, 22, "note", "8 min read · Stories"),
                                           B("cover", 0, 310, 1280, 480, "hero"),
                                           B("", 240, 830, 800, 260, "p", "Long-form text of the story."),
                                           B("share-x", 240, 1120, 90, 36, "btn2", "Share", None, 1.2),
                                           B("subscribe-inline", 240, 1200, 800, 160, "card"),
                                           B("sub-email", 270, 1270, 480, 48, "input", "Your email", None, 2.0),
                                           B("sub-submit", 766, 1270, 240, 48, "btn", "Subscribe", None, 3.0),
                                           B("related-1", 240, 1420, 380, 220, "img2", "", stories[0], 2.0),
                                           B("related-2", 660, 1420, 380, 220, "img3", "", stories[1], 1.6)],
                                    height=1700, dwell=150, kind="story")
        self.sources = [
            Source(36, "https://www.google.com/", "", stories + ["/"], 0.55),
            Source(14, "", "", ["/", "/stories/the-perfect-wave-forecast"], 0.4),
            Source(10, "", "utm_source=newsletter&utm_medium=email&utm_campaign=issue-42", stories[:3], 0.3),
            Source(6, "https://www.reddit.com/r/surfing/", "", ["/stories/reading-the-tides", "/stories/cold-water-confidence"], 0.6),
            Source(5, "https://t.co/k3x9", "", ["/stories/surf-trips-portugal", "/stories/plastic-free-surfing"], 0.6),
            Source(3, "https://m.facebook.com/", "", ["/stories/surf-trips-portugal"], 0.6),
            Source(4, "https://www.pinterest.com/", "", ["/stories/surf-trips-portugal", "/stories/reading-the-tides"], 0.65),
            Source(3, "https://feedly.com/", "", stories[:4], 0.4),
            Source(2, "https://flipboard.com/", "", ["/stories/the-shaper"], 0.6),
            Source(3, "https://www.bing.com/", "", stories[:3], 0.55),
            Source(3, "https://chatgpt.com/", "utm_source=chatgpt.com", ["/stories/reading-the-tides", "/stories/cold-water-confidence"], 0.5),
            Source(1, "https://www.perplexity.ai/", "", ["/stories/the-perfect-wave-forecast"], 0.5),
            Source(1, "https://news.ycombinator.com/", "", ["/stories/the-perfect-wave-forecast"], 0.7),
            Source(1, "https://www.google.com/", "", ["/stories/tides-explained"], 0.8),  # an old address
        ]
        self.spikes = [(6, 3.4, Source(1, "https://news.ycombinator.com/", "", ["/stories/the-perfect-wave-forecast"], 0.7),
                        "On the Hacker News front page")]
        self.notes = [(20, "Newsletter issue #42")]
        self.errors = [("/stories/", 0.015, {"k": "error", "msg": "ResizeObserver loop completed with undelivered notifications.",
                                             "src": self.origin + "/", "line": 0, "col": 0, "stack": ""}),
                       ("/", 0.01, {"k": "resource", "tag": "img", "res": self.origin + "/img/covers/old-cover.jpg", "msg": "Failed to load image"})]
        self.dead = [("/stories/", "cover", 0.05)]

    def on_page(self, v: "Visit", path: str, t: int, dwell: int) -> Optional[str]:
        rng = v.rng
        story = path.startswith("/stories/")
        if (story and rng.random() < 0.035) or (path == "/newsletter" and rng.random() < 0.45):
            if v.form(t + int(dwell * 0.8), "subscribe", [("email", "email")], abandon=0.25):
                v.event(t + int(dwell * 0.8) + 4000, "event", n="subscribe", p={"placement": "story" if story else "newsletter page"})
        if story and rng.random() < 0.04:
            v.event(t + int(dwell * 0.9), "event", n="share", p={"network": rng.choice(["x", "facebook", "link", "whatsapp"])})
        return None


class Harbor(Site):
    id = "harbor"
    name = "Harbor"
    origin = "https://harbor-app.example"
    timezone = "America/New_York"
    share, busy = 0.45, 0.5
    countries = {"US": 2.0, "CA": 1.6, "GB": 1.4, "DE": 1.2, "IN": 1.5, "NL": 1.2, "AU": 1.0, "FR": 0.7, "PF": 0.1, "PT": 0.6}
    goals = [{"name": "Saw pricing", "type": "page", "match": "/pricing"},
             {"name": "Signed up", "type": "event", "match": "signup"},
             {"name": "Invited a teammate", "type": "event", "match": "invite_sent"}]
    settings = {"features": {"replay": True, "mouse_moves": True, "dom_snapshots": True}, "replay": {"sample_rate": 0.05}}
    returns = (0.45, 0.35, 0.25)
    return_days = (0.8, 6.0)
    search_path = "/docs/search"
    searches = ["webhooks", "sso", "api keys", "export", "slack", "rate limits", "macros", "gdpr"]
    broken = {"/docs/v1/auth": "Not found · Harbor docs"}
    mouse = ("/pricing", "/")

    def __init__(self) -> None:
        nav = [("Product", "/features"), ("Pricing", "/pricing"), ("Customers", "/customers"), ("Docs", "/docs")]
        top = header("harbor", nav, ("nav-signup", "Start free", "/signup"))
        self.pages = {
            "/": Page("Harbor — the calm inbox for support teams", [("/pricing", 4), ("/features", 3), ("/signup", 2), ("/customers", 1)],
                      top + [B("hero", 0, 72, 1280, 560, "hero"), B("", 160, 180, 960, 130, "h1x", "Support that feels calm, for your team and your customers."),
                             B("cta-start", 440, 380, 190, 54, "btn", "Start free", "/signup", 8),
                             B("cta-demo", 650, 380, 190, 54, "btn2l", "Book a demo", None, 3),
                             B("", 64, 690, 1152, 40, "h2", "Trusted by teams who answer thousands of messages a week"),
                             B("feature-inbox", 64, 780, 368, 260, "img", "Shared inbox", "/features", 2),
                             B("feature-macros", 456, 780, 368, 260, "img2", "Macros", "/features", 1.4),
                             B("feature-reports", 848, 780, 368, 260, "img3", "Reports", "/features", 1.2)], height=1300, dwell=30),
            "/features": Page("Features · Harbor", [("/pricing", 4), ("/signup", 2), ("/docs", 1)], dwell=45),
            "/customers": Page("Customers · Harbor", [("/pricing", 2), ("/signup", 1)], dwell=40),
            "/pricing": Page("Pricing · Harbor", [("/signup", 5), ("/features", 1), ("/docs", 1)], top + [
                B("", 240, 120, 800, 60, "h1x", "Simple pricing, per agent"),
                B("toggle-yearly", 560, 200, 160, 40, "btn2", "Yearly billing", None, 2.2),
                B("plan-starter", 150, 290, 300, 360, "card"), B("", 180, 320, 200, 30, "h2", "Starter"), B("", 180, 360, 200, 40, "pricexl", "$12"),
                B("choose-starter", 180, 580, 240, 48, "btn2", "Choose Starter", "/signup", 4),
                B("plan-pro", 490, 270, 300, 400, "card"), B("", 520, 300, 200, 30, "h2", "Pro"), B("", 520, 340, 200, 40, "pricexl", "$29"),
                B("choose-pro", 520, 600, 240, 48, "btn", "Choose Pro", "/signup", 7),
                B("plan-team", 830, 290, 300, 360, "card"), B("", 860, 320, 200, 30, "h2", "Enterprise"), B("", 860, 360, 200, 40, "pricexl", "Let's talk"),
                B("contact-sales", 860, 580, 240, 48, "btn2", "Contact sales", None, 2.5),
                B("faq-1", 240, 760, 800, 56, "faq", "Is there a free trial?", None, 1.6),
                B("faq-2", 240, 830, 800, 56, "faq", "Can I change plans later?", None, 1.2),
                B("faq-3", 240, 900, 800, 56, "faq", "Do you offer discounts for nonprofits?", None, 0.8)], height=1100, dwell=50),
            "/signup": Page("Create your account · Harbor", [("/app/inbox", 6), ("/pricing", 1)], top + [
                B("", 440, 120, 400, 50, "h1", "Create your account"), B("", 420, 190, 440, 470, "card"),
                B("su-email", 450, 220, 380, 46, "input", "Work email", None, 1.6), B("su-name", 450, 286, 380, 46, "input", "Full name", None, 1.2),
                B("su-company", 450, 352, 380, 46, "input", "Company", None, 1.0), B("su-password", 450, 418, 380, 46, "input", "Password", None, 1.2),
                B("su-submit", 450, 500, 380, 52, "btn", "Create account", None, 7),
                B("su-google", 450, 570, 380, 46, "btn2", "Continue with Google", None, 3)], height=800, dwell=60),
            "/login": Page("Sign in · Harbor", [("/app/inbox", 1)], dwell=15),
            "/docs": Page("Documentation · Harbor", [("/docs/getting-started", 3), ("/docs/integrations", 2), ("/docs/api", 2), ("/docs/search", 1)], dwell=30),
            "/docs/getting-started": Page("Getting started · Harbor docs", [("/docs/integrations", 2), ("/docs/api", 1), ("/signup", 1)], dwell=90),
            "/docs/integrations": Page("Integrations · Harbor docs", [("/docs/api", 1), ("/docs", 1)], dwell=70),
            "/docs/api": Page("API reference · Harbor docs", [("/docs/getting-started", 1), ("/docs/search", 1)], dwell=120),
            "/docs/search": Page("Search · Harbor docs", [("/docs/api", 2), ("/docs/integrations", 1)], dwell=12),
            "/blog/calm-support": Page("What calm support looks like · Harbor blog", [("/", 2), ("/pricing", 1)], dwell=110),
            "/changelog": Page("Changelog · Harbor", [("/app/inbox", 2), ("/docs", 1)], dwell=40),
            "/app/inbox": Page("Inbox · Harbor", [("/app/inbox/conversation", 6), ("/app/reports", 2), ("/app/settings/team", 1)], top[:2] + [
                B("", 0, 72, 240, 1000, "side"), B("app-new", 24, 96, 192, 44, "btn", "New message", None, 3),
                B("app-mine", 24, 160, 192, 32, "nav", "Assigned to me", None, 2), B("app-all", 24, 200, 192, 32, "nav", "All open", None, 2),
                B("conv-1", 264, 96, 980, 72, "card", "Refund for order #4821", None, 5), B("conv-2", 264, 180, 980, 72, "card", "Can't log in on mobile", None, 4),
                B("conv-3", 264, 264, 980, 72, "card", "Invoice address change", None, 3), B("conv-4", 264, 348, 980, 72, "card", "Feature request: dark mode", None, 2)],
                height=1000, dwell=60, kind="app"),
            "/app/inbox/conversation": Page("Conversation · Harbor", [("/app/inbox", 6), ("/app/reports", 1)], dwell=95, kind="app"),
            "/app/reports": Page("Reports · Harbor", [("/app/inbox", 3)], dwell=60, kind="app"),
            "/app/settings/team": Page("Team settings · Harbor", [("/app/inbox", 2)], dwell=45, kind="app"),
        }
        self.sources = [
            Source(22, "https://www.google.com/", "", ["/", "/pricing", "/docs/getting-started", "/blog/calm-support", "/docs/api"], 0.45),
            Source(26, "", "", ["/", "/app/inbox", "/app/inbox", "/login"], 0.2),
            Source(4, "https://www.g2.com/", "", ["/", "/pricing"], 0.4),
            Source(3, "https://www.producthunt.com/", "utm_source=producthunt", ["/"], 0.5),
            Source(5, "https://www.linkedin.com/", "", ["/blog/calm-support", "/"], 0.55),
            Source(2, "https://www.capterra.com/", "", ["/pricing"], 0.4),
            Source(5, "https://www.google.com/", "gclid=Cj0KCQjw&utm_source=google&utm_medium=cpc&utm_campaign=brand-us", ["/", "/pricing"], 0.45),
            Source(4, "https://chatgpt.com/", "utm_source=chatgpt.com", ["/docs/getting-started", "/", "/pricing"], 0.4),
            Source(1, "https://claude.ai/", "", ["/docs/api"], 0.4),
            Source(3, "https://github.com/", "", ["/docs/api", "/docs/integrations"], 0.5),
            Source(4, "", "utm_source=newsletter&utm_medium=email&utm_campaign=changelog-october", ["/changelog"], 0.35),
            Source(1, "https://news.ycombinator.com/", "", ["/blog/calm-support"], 0.6),
            Source(1, "https://www.google.com/", "", ["/docs/v1/auth"], 0.8),
        ]
        self.spikes = [(15, 2.6, Source(1, "https://www.producthunt.com/", "utm_source=producthunt", ["/", "/pricing"], 0.45),
                        "Launched on Product Hunt")]
        self.notes = [(41, "New pricing page")]
        self.errors = [
            ("/app/", 0.02, {"k": "error", "msg": "TypeError: Cannot read properties of null (reading 'focus')",
                             "src": self.origin + "/app/assets/inbox.5d2c.js", "line": 1203, "col": 17,
                             "stack": "TypeError: Cannot read properties of null (reading 'focus')\n    at Composer.open (inbox.5d2c.js:1203:17)"}),
            ("/app/", 0.008, {"k": "error", "msg": "ChunkLoadError: Loading chunk 412 failed.", "src": self.origin + "/app/assets/runtime.a1.js",
                              "line": 1, "col": 3021, "stack": "ChunkLoadError: Loading chunk 412 failed."}),
            ("/docs/", 0.01, {"k": "resource", "tag": "script", "res": self.origin + "/docs/assets/search-index.json", "msg": "Failed to load script"}),
        ]
        self.rage = [("/pricing", "toggle-yearly", 0.05)]
        self.dead = [("/pricing", "faq-3", 0.06), ("/", "feature-inbox", 0.04)]

    def on_page(self, v: "Visit", path: str, t: int, dwell: int) -> Optional[str]:
        rng = v.rng
        if path == "/signup":
            if v.form(t + 1500, "signup", [("email", "email"), ("name", "text"), ("company", "text"), ("password", "password")], abandon=0.35):
                plan = rng.choice(["starter", "pro", "pro", "enterprise-trial"])
                v.event(t + 20000, "event", n="signup", p={"plan": plan, "method": rng.choice(["email", "google", "google"])})
                v.identify(t + 20100, "u_" + rid(rng, 8), {"plan": plan})
                return "/app/inbox"
            return ""
        if path == "/pricing" and rng.random() < 0.05:
            v.form(t + 3000, "contact-sales", [("email", "email"), ("company", "text"), ("seats", "number"), ("message", "textarea")], abandon=0.5)
        if path.startswith("/app/"):
            if not v.person.get("uid"):
                v.identify(t + 300, "u_" + rid(rng, 8), {"plan": rng.choice(["starter", "pro"])})
            if path == "/app/inbox/conversation" and rng.random() < 0.6:
                v.event(t + int(dwell * 0.8), "event", n="conversation_closed", p={"channel": rng.choice(["email", "chat", "email"])})
            if path == "/app/settings/team" and rng.random() < 0.5:
                v.event(t + int(dwell * 0.6), "event", n="invite_sent", p={"seats": rng.choice([1, 1, 2, 3, 5])})
            if path == "/app/reports" and rng.random() < 0.15:
                v.event(t + int(dwell * 0.6), "event", n="export_report", p={"format": rng.choice(["csv", "pdf"])})
        return None


SITES: Dict[str, Callable[[], Site]] = {"demo": Shop, "journal": Journal, "harbor": Harbor}


# ---- one visit -----------------------------------------------------------------------------------

class Poster:
    """Sends requests to the collector in process, at simulated clock times."""

    def __init__(self, cfg: Any) -> None:
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

    def batch(self, site: Site, at: int, env: Dict[str, Any], ua: str, ip: str, lang: str) -> None:
        headers = {"Content-Type": "text/plain;charset=UTF-8", "Origin": site.origin, "User-Agent": ua, "Accept-Language": lang,
                   "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}
        self._call("POST", "/collect", json.dumps(env).encode(), headers, "", ip, at)

    def chunk(self, site: Site, at: int, query: str, payload: Any, ua: str, ip: str) -> None:
        body = gzip.compress(json.dumps(payload).encode())
        self._call("POST", "/chunk", body, {"Origin": site.origin, "User-Agent": ua, "Content-Type": "text/plain"}, query, ip, at)

    def pixel(self, at: int, query: str, ua: str, ip: str, referer: str) -> None:
        self._call("GET", "/p.gif", b"", {"User-Agent": ua, "Referer": referer}, query, ip, at)


class Visit:
    """The events of one visit, built page by page."""

    def __init__(self, site: Site, person: Dict[str, Any], rng: random.Random, light: bool) -> None:
        self.site, self.person, self.rng, self.light = site, person, rng, light
        self.events: List[Dict[str, Any]] = []
        self.cart: List[float] = []
        self.pid = ""
        self.url = ""

    def event(self, ts: int, kind: str, **fields: Any) -> None:
        self.events.append(dict({"t": kind, "id": rid(self.rng, 12), "ts": ts, "pid": self.pid, "url": self.url}, **fields))

    def identify(self, ts: int, uid: str, traits: Dict[str, Any]) -> None:
        if not self.person.get("uid"):
            self.person["uid"] = uid
        self.event(ts, "identify", uid=self.person["uid"], traits=traits)

    def form(self, t: int, name: str, fields: List[Tuple[str, str]], abandon: float) -> bool:
        """A form filled in field by field (never with values): submitted, or abandoned on the way."""
        rng = self.rng
        self.event(t, "form", a="start", f=name, fld=fields[0][0])
        give_up = rng.random() < abandon
        stop = rng.randint(1, max(1, len(fields) - 1)) if give_up else len(fields)
        last = None
        for k, (fld, ft) in enumerate(fields[:stop]):
            last = fld
            slow = {"password": 6500, "textarea": 15000, "select-one": 1800}.get(ft, 3500)
            self.event(t + 2000 + k * 4000, "form", a="blur", f=name, fld=fld, ft=ft,
                       ms=int(rng.lognormvariate(math.log(slow), 0.5)), filled=1)
        if not give_up and rng.random() < 0.12:  # a typo caught by the browser's validation
            self.event(t + 2500 + stop * 4000, "form", a="invalid", f=name, fld=fields[0][0], ft=fields[0][1], why="typeMismatch")
        self.event(t + 3000 + stop * 4000, "form", a="abandon" if give_up else "submit", f=name, n=stop,
                   **({"last": last} if give_up else {"ms": 3000 + stop * 4000}))
        return not give_up


def context(person: Dict[str, Any]) -> Dict[str, Any]:
    _w, ua, uad, base = person["device"]
    lang = person["lang"].split(",")[0]
    mobile = base["vw"] < 700
    ctx = dict(base, cd=24, lang=lang, langs=[lang], tz=person["city"][6], tzo=0, cs=person["scheme"], rm=0, ct="no-preference",
               fc=0, touch=1 if mobile else 0, hc=person["cores"], dm=8, net={"type": "wifi", "eff": "4g", "dl": 10, "rtt": 50, "sd": 0},
               ck=1, ls=1, pwa="browser", ifr=0, ua=ua)
    if uad:
        ctx["uad"] = uad
    return ctx


def new_person(site: Site, rng: random.Random, at: int) -> Dict[str, Any]:
    i, city = weighted(rng, list(enumerate(CITIES)), key=lambda ic: site.place_weight(ic[1]))
    ip = "5.%d.%d.%d" % (10 + i, rng.randint(0, 255), rng.randint(1, 254))
    return {"vid": rid(rng, 20), "vt": at, "ip": ip, "city": city, "device": weighted(rng, DEVICES), "at": at,
            "lang": LANGS.get(city[0], "en-US,en;q=0.9"), "uid": None, "scheme": rng.choice(["light", "light", "dark"]),
            "cores": rng.choice([4, 8, 8, 12])}


def visit(site: Site, poster: Poster, person: Dict[str, Any], at: int, sn: int, rng: random.Random, light: bool,
          stats: Dict[str, int], source: Optional[Source] = None, now: Optional[int] = None) -> None:
    """One visit: pages with their context, engagement, scroll, clicks, Web Vitals and the site's own events, sent
    as one batch at its end; or, given `now` (the last minutes, for Realtime), as they happen, never after `now`."""
    _w, ua, _uad, base = person["device"]
    mobile = base["vw"] < 700
    vw, vh = base["vw"], base["vh"]
    ctx = context(person)
    sid = rid(rng)
    if source is None:
        returning = sn > 1 and rng.random() < 0.55
        source = site.sources[0] if returning and site.id != "harbor" else weighted(rng, site.sources, key=lambda s: s.weight)
        if returning and site.id == "harbor" and person.get("uid"):  # a customer goes straight to the app
            source = Source(1, "", "", ["/app/inbox"], 0.1)
    path = rng.choice(source.landings)
    n_pages = 1 if rng.random() < source.bounce else min(9, 2 + int(rng.expovariate(0.6)))
    v = Visit(site, person, rng, light)
    t = at
    referrer, first = source.ref, True
    recorded = rng.random() < site.recorded and not mobile and bool(site.page(path).blocks)
    rec_clicks: List[Tuple[str, int, int]] = []
    prev_path = ""
    for i in range(n_pages):
        page = site.page(path)
        v.pid = rid(rng, 12)
        query = ""
        if first and source.query:
            query = "?" + source.query
        if path == site.search_path:
            query = "?q=" + rng.choice(site.searches).replace(" ", "+")
        v.url = site.origin + path + query
        spa = site.id == "harbor" and path.startswith("/app") and prev_path.startswith("/app")
        pv: Dict[str, Any] = {"t": "pageview", "id": rid(rng, 12), "ts": t, "pid": v.pid, "url": v.url, "title": page.title,
                              "ref": referrer if not spa else site.origin + prev_path, "nav": "spa" if spa else "navigate",
                              "sn": sn, "pn": i + 1, "ctx": ctx}
        if path in site.broken or path not in site.pages:
            pv["st"] = 404
        if first:
            pv["idsrc"] = "new" if sn == 1 else "cookie"
            pv["ns"] = 1
            if sn == 1:
                pv["new"] = 1
        v.events.append(pv)
        if first and sn == 1:
            v.event(t + 80, "fp", h="fp" + rid(rng, 10), fv=1, bot={"wd": 0, "pl": 5, "lg": 2, "oz": 0, "hl": 0, "co": 1 if "Chrome" in ua else None})
        if not spa and (not light or rng.random() < 0.35):
            vitals(site, v, t, path, mobile, at)
        dwell = int(rng.lognormvariate(math.log(page.dwell * 1000), 0.75))
        if n_pages == 1 and rng.random() < 0.5:
            dwell = rng.randint(1500, 9000)
        depth = min(100, int(rng.betavariate(2, 1.5) * 100) + (12 if page.kind == "story" else 0))
        marks = [m for m in (25, 50, 75, 90, 100) if depth >= m]
        for m in (marks[-1:] if light else marks):
            v.event(t + int(dwell * m / 140), "scroll", d=m, max=depth, ph=page.height, vh=vh)
        clickable = [b for b in page.blocks if b[8] > 0]
        for _ in range(rng.choice([0, 0, 1, 1, 1, 2, 2, 3]) if clickable else 0):
            b = weighted(rng, clickable, key=lambda blk: blk[8])
            bid, x, y, w, h, _kind, label, href = b[:8]
            ex, ey = min(0.97, max(0.03, rng.gauss(0.5, 0.17))), min(0.95, max(0.05, rng.gauss(0.5, 0.2)))
            scale = vw / 1280 if mobile else 1
            cx, cy = (x + ex * w) * scale, (y + ey * h) * scale
            ev = {"x": round(cx), "y": round(cy), "vx": round(cx), "vy": round(cy) % vh, "pw": vw, "ph": page.height, "vw": vw, "vh": vh,
                  "sel": "#" + bid, "tag": "a" if href else "button" if _kind in ("btn", "btn2", "btn2l", "chip", "tab") else "div",
                  "txt": label[:40], "ex": round(ex, 3), "ey": round(ey, 3), "ew": w, "eh": h, "btn": 0}
            if href:
                ev["href"] = site.origin + href if href.startswith("/") else href
            v.event(t + rng.randint(1500, max(1600, dwell)), "click", **ev)
            rec_clicks.append((bid, int(cx), int(cy)))
        for prefix, bid, p in site.rage:
            if path.startswith(prefix) and rng.random() < p:
                b = next((blk for blk in page.blocks if blk[0] == bid), None)
                if b:
                    v.event(t + 4000, "rage", sel="#" + bid, n=rng.randint(3, 7), ms=rng.randint(500, 1400), x=b[1] + b[3] // 2, y=b[2] + b[4] // 2)
        for prefix, bid, p in site.dead:
            if path.startswith(prefix) and rng.random() < p:
                b = next((blk for blk in page.blocks if blk[0] == bid), None)
                if b:
                    v.event(t + 5000, "dead", sel="#" + bid, tag="div", x=b[1] + b[3] // 2, y=b[2] + b[4] // 3)
        for prefix, p, err in site.errors:
            if path.startswith(prefix) and rng.random() < p:
                v.event(t + rng.randint(800, 6000), "error", **err)
        if path == site.search_path:
            pass  # the query is in the URL: Tidesight reads it from there
        nxt = site.on_page(v, path, t, dwell)
        v.event(t + dwell, "engagement", ms=int(dwell * rng.uniform(0.55, 0.95)), tot=dwell, why="route" if i < n_pages - 1 else "hidden", sd=depth)
        if nxt == "":
            break
        referrer = v.url
        prev_path = path
        t += dwell + rng.randint(500, 3000)
        if nxt:
            path = nxt
        else:
            options = page.next or [("/", 1.0)]
            path = weighted(rng, options, key=lambda o: o[1])[0]
        first = False
        if now is not None and t > now:
            break
    if now is not None:
        as_they_happen(site, poster, v, person, sid, now)
    else:
        env = {"v": 1, "site": site.id, "vid": person["vid"], "vt": person["vt"], "sid": sid, "uid": person.get("uid"),
               "sent": t, "lib": "ts.js/0.1.0", "try": 0, "events": v.events}
        poster.batch(site, min(t, int(time.time() * 1000) - 1000), env, ua, person["ip"], person["lang"])
    stats["sessions"] = stats.get("sessions", 0) + 1
    landing = v.events[0]["url"].split(site.origin, 1)[1].split("?")[0]
    if recorded and now is None:
        stats["replays"] = stats.get("replays", 0) + 1
        rec = recording(site, rng, at, landing, vw, vh, rec_clicks[:5])
        poster.chunk(site, at + 15000, "site=%s&k=replay&vid=%s&sid=%s&pid=%s&seq=0&ts=%d&rec=%s"
                     % (site.id, person["vid"], sid, v.events[0]["pid"], at, rid(rng, 8)), rec, ua, person["ip"])
    if landing in site.mouse and rng.random() < 0.25 and not mobile and now is None:
        pts = [[int(rng.gauss(640, 260)), int(abs(rng.gauss(520, 360))), k * 100] for k in range(120)]
        poster.chunk(site, at + 9000, "site=%s&k=mouse&vid=%s&sid=%s&pid=%s&seq=0&ts=%d&url=%s"
                     % (site.id, person["vid"], sid, v.events[0]["pid"], at, site.origin + landing),
                     {"pw": vw, "ph": site.page(landing).height, "vw": vw, "vh": vh, "pts": pts}, ua, person["ip"])


def as_they_happen(site: Site, poster: Poster, v: Visit, person: Dict[str, Any], sid: str, now: int) -> None:
    """Send the visit's events that happened before `now` as the tracker does: a batch every few seconds."""
    _w, ua, _uad, _base = person["device"]
    events = sorted((e for e in v.events if e["ts"] <= now), key=lambda e: e["ts"])
    while events:
        cut = next((k for k, e in enumerate(events) if e["ts"] > events[0]["ts"] + 8000), len(events))
        group, events = events[:cut], events[cut:]
        at = min(group[-1]["ts"] + 300, now)
        poster.batch(site, at, {"v": 1, "site": site.id, "vid": person["vid"], "vt": person["vt"], "sid": sid,
                                "uid": person.get("uid"), "sent": at, "lib": "ts.js/0.1.0", "try": 0, "events": group},
                     ua, person["ip"], person["lang"])


def vitals(site: Site, v: Visit, t: int, path: str, mobile: bool, at: int) -> None:
    rng = v.rng
    page = site.page(path)
    heavy = 1.5 if page.kind in ("product",) or path == "/" else 1.0
    if site.id == "demo" and page.kind == "product" and at < time.time() * 1000 - 34 * DAY:
        heavy *= 1.45  # before the product photos were resized
    lcp = rng.lognormvariate(math.log(1500 * heavy * (1.7 if mobile else 1.0)), 0.42)
    for name, val in (("LCP", lcp), ("FCP", lcp * rng.uniform(0.45, 0.8)), ("TTFB", rng.lognormvariate(math.log(320), 0.5)),
                      ("CLS", abs(rng.gauss(0.03 if page.kind != "story" else 0.12, 0.05))),
                      ("INP", rng.lognormvariate(math.log((170 if page.kind == "app" else 120) * (1.6 if mobile else 1)), 0.5))):
        good, poor = {"LCP": (2500, 4000), "FCP": (1800, 3000), "TTFB": (800, 1800), "CLS": (0.1, 0.25), "INP": (200, 500)}[name]
        rating = "good" if val <= good else "ni" if val <= poor else "poor"
        v.event(t + 2000, "vital", n=name, val=round(val, 4 if name == "CLS" else 0), r=rating,
                el="#gallery-main" if name == "LCP" and page.kind == "product" else None)
    v.event(t + 2500, "perf", dns=rng.randint(0, 40), tcp=rng.randint(10, 60), tls=rng.randint(15, 70),
            ttfb=int(rng.lognormvariate(math.log(320), 0.5)), dl=rng.randint(5, 80), dom=int(lcp * 0.8), dcl=int(lcp * 0.9),
            load=int(lcp * 1.3), proto="h2", nt="navigate")


# ---- DOM snapshots and recordings (rrweb's serialized form) -----------------------------------------

THEMES = {  # per property: page background, ink, accent, fonts, hero gradient
    "demo": ("#f6f3ec", "#13212e", "#0f4c5c", "Georgia, 'Times New Roman', serif", "-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif",
             "linear-gradient(120deg, #0f2a3d 0%, #1f5f7a 55%, #8cc7c4 100%)"),
    "journal": ("#fbf8f1", "#1b1b18", "#b4532a", "Georgia, 'Times New Roman', serif", "Georgia, 'Times New Roman', serif",
                "linear-gradient(160deg, #24435a 0%, #5d8fa8 50%, #e6c79c 100%)"),
    "harbor": ("#f7f8fb", "#141a2b", "#4f46e5", "-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif",
               "-apple-system, 'Segoe UI', Helvetica, Arial, sans-serif", "linear-gradient(135deg, #eef0ff 0%, #dfe4ff 50%, #f4f0ff 100%)"),
}


def page_css(site: Site) -> str:
    bg, ink, accent, display, body, hero = THEMES[site.id]
    light_hero = site.id == "harbor"
    return ("*{box-sizing:border-box}body{margin:0;background:%(bg)s;color:%(ink)s;font:15px/1.5 %(body)s}"
            ".page{position:relative;width:1280px;margin:0 auto}.b{position:absolute;overflow:hidden}"
            ".bar{background:#fff;border-bottom:1px solid #e3e0d8}.logo{font:700 20px/28px %(display)s;letter-spacing:.12em;color:%(ink)s}"
            "a{color:inherit;text-decoration:none}.nav{font-size:15px;line-height:22px;color:%(ink)s;opacity:.8}"
            ".hero{background:%(hero)s}.h1x{font:600 54px/1.08 %(display)s;color:%(heroink)s;letter-spacing:-.01em}"
            ".lead{font-size:20px;line-height:1.45;color:%(heroink)s;opacity:.85}"
            ".h1{font:600 34px/1.15 %(display)s}.h2{font:600 24px/1.3 %(display)s}.h3{font:600 17px/1.35 %(body)s}"
            ".p{font-size:16px;line-height:1.65;color:#4a4f57}.note{font-size:13px;color:#7b7f86}.label{font-size:13px;font-weight:600}"
            ".price{font-weight:700}.pricexl{font:700 30px/1.1 %(body)s}.stars{color:#d79b1d;font-size:14px}"
            ".btn{background:%(accent)s;color:#fff;border-radius:10px;font-weight:600;display:flex;align-items:center;justify-content:center}"
            ".btn2,.btn2l{border:1.5px solid currentColor;border-radius:10px;font-weight:600;display:flex;align-items:center;justify-content:center}"
            ".btn2{color:%(ink)s}.btn2l{color:%(heroink)s}"
            ".chip{border:1.5px solid #c9c4b8;border-radius:10px;display:flex;align-items:center;justify-content:center;font-weight:600;background:#fff}"
            ".tab{border-bottom:2px solid #d6d1c5;display:flex;align-items:center;justify-content:center;font-weight:600}"
            ".link{text-decoration:underline;font-size:14px}.input{background:#fff;border:1px solid #cfcabd;border-radius:9px;color:#9a9890;"
            "padding:0 14px;display:flex;align-items:center}.card{background:#fff;border:1px solid #e2ded3;border-radius:16px;"
            "box-shadow:0 10px 30px -18px rgba(0,0,0,.25);padding:20px 22px;font-weight:600}"
            ".faq{border-bottom:1px solid #dcd8cc;display:flex;align-items:center;font-weight:600}"
            ".side{background:#fff;border-right:1px solid #e3e0d8}"
            ".img,.img1{background:linear-gradient(150deg,#1d3557,#457b9d 60%%,#a8dadc);border-radius:14px}"
            ".img2{background:linear-gradient(150deg,#6d4c2f,#c49a6c 60%%,#efd9b4);border-radius:14px}"
            ".img3{background:linear-gradient(150deg,#264653,#2a9d8f 60%%,#e9c46a);border-radius:14px}"
            ".img,.img1,.img2,.img3{color:#fff;font:600 22px/1.2 %(display)s;padding:18px}"
            % {"bg": bg, "ink": ink, "accent": accent, "display": display, "body": body, "hero": hero,
               "heroink": ink if light_hero else "#fff"})


def snapshot_dom(site: Site, path: str) -> Dict[str, Any]:
    """The page as rrweb serializes a document: every block where it is."""
    counter = [0]

    def nid() -> int:
        counter[0] += 1
        return counter[0]

    def text(t: str) -> Dict[str, Any]:
        return {"type": 3, "textContent": t, "id": nid()}

    def elem(tag: str, attrs: Optional[Dict[str, str]] = None, children: Optional[List] = None) -> Dict[str, Any]:
        return {"type": 2, "tagName": tag, "attributes": attrs or {}, "childNodes": children or [], "id": nid()}

    page = site.page(path)
    head = elem("head", {}, [elem("meta", {"charset": "utf-8"}), elem("title", {}, [text(page.title)]),
                             elem("style", {"_cssText": page_css(site)})])
    kids = []
    for (bid, x, y, w, h, kind, label, href, _weight) in page.blocks:
        attrs = {"class": "b " + kind, "style": "left:%dpx;top:%dpx;width:%dpx;height:%dpx" % (x, y, w, h)}
        if bid:
            attrs["id"] = bid
        if href:
            attrs["href"] = site.origin + href if href.startswith("/") else href
        kids.append(elem("a" if href else "div", attrs, [text(label)] if label else []))
    body = elem("body", {}, [elem("div", {"class": "page", "style": "height:%dpx" % page.height}, kids)])
    html = elem("html", {"lang": "en"}, [head, body])
    return {"type": 0, "childNodes": [{"type": 1, "name": "html", "publicId": "", "systemId": "", "id": nid()}, html], "id": nid()}


def node_ids(node: Dict[str, Any], out: Dict[str, int]) -> Dict[str, int]:
    if node.get("type") == 2 and node.get("attributes", {}).get("id"):
        out[node["attributes"]["id"]] = node["id"]
    for c in node.get("childNodes", []):
        node_ids(c, out)
    return out


def recording(site: Site, rng: random.Random, t0: int, path: str, width: int, height: int,
              clicks: List[Tuple[str, int, int]]) -> List[Dict[str, Any]]:
    """A short rrweb recording: the page, the pointer wandering to each click, a scroll now and then."""
    dom = snapshot_dom(site, path)
    ids = node_ids(dom, {})
    if not clicks:
        b = max((blk for blk in site.page(path).blocks if blk[8] > 0), key=lambda blk: blk[8], default=None)
        clicks = [(b[0], b[1] + b[3] // 2, b[2] + b[4] // 2)] if b else []
    events: List[Dict[str, Any]] = [{"type": 4, "timestamp": t0, "data": {"href": site.origin + path, "width": width, "height": height}},
                                    {"type": 2, "timestamp": t0 + 5, "data": {"node": dom, "initialOffset": {"left": 0, "top": 0}}}]
    t, x, y, scroll = t0 + 400, width / 2, height / 3, 0
    for (eid, cx, cy) in clicks:
        if cy - scroll > height - 80 or cy < scroll:  # scroll the element into view first
            scroll = max(0, int(cy - height / 2))
            t += rng.randint(300, 700)
            events.append({"type": 3, "timestamp": t, "data": {"source": 3, "id": 1, "x": 0, "y": scroll}})
        for _ in range(rng.randint(5, 10)):
            x += (cx - x) * 0.33 + rng.uniform(-14, 14)
            y += (cy - scroll - y) * 0.33 + rng.uniform(-14, 14)
            t += rng.randint(50, 160)
            events.append({"type": 3, "timestamp": t, "data": {"source": 1, "positions": [{"x": int(x), "y": int(y), "id": 1, "timeOffset": 0}]}})
        t += 220
        events.append({"type": 3, "timestamp": t, "data": {"source": 2, "type": 2, "id": ids.get(eid, 1), "x": int(cx), "y": int(cy - scroll)}})
        t += rng.randint(600, 1600)
    return events


# ---- traffic ----------------------------------------------------------------------------------------

def simulate(cfg: Any, sites: List[Site], days: int, visitors: int, seed: int, light: bool) -> Dict[str, int]:
    rng = random.Random(seed)
    poster = Poster(cfg)
    end = int(time.time() * 1000)
    start = end - days * DAY
    stats: Dict[str, int] = {"sessions": 0, "replays": 0}
    for site in sites:
        srng = random.Random("%s-%s" % (seed, site.id))
        people = []
        for _ in range(int(visitors * site.share)):
            while True:  # growth over the period, a weekly rhythm, the site's remarkable days
                u = srng.random() ** 0.82
                at = start + int(u * days * DAY)
                d = days - (at - start) // DAY
                weekday = time.gmtime(at / 1000).tm_wday
                keep = 0.62 if weekday >= 5 and site.id != "journal" else 1.0
                spike = next((s for s in site.spikes if d == s[0]), None)
                if spike:
                    keep *= spike[1]
                if srng.random() < keep / 3.6:
                    break
            p = new_person(site, srng, at)
            p["spike"] = spike[2] if spike and srng.random() < 0.7 else None
            people.append(p)
        people.sort(key=lambda p: p["at"])
        for p in people:
            n = 1 + sum(srng.random() < r for r in site.returns)
            at = p["at"]
            for sn in range(1, n + 1):
                if at > end - 45 * MINUTE:
                    break
                visit(site, poster, p, at, sn, srng, light, stats, source=p["spike"] if sn == 1 else None)
                at += int(srng.uniform(*site.return_days) * DAY)
        bots(site, poster, srng, start, end, days)
        snapshots(site, poster)
    stats["requests"] = poster.n
    collect.now_ms = lambda: int(time.time() * 1000)
    return stats


def bots(site: Site, poster: Poster, rng: random.Random, start: int, end: int, days: int) -> None:
    """Crawlers fetching the pixel, and headless browsers running the tracker: flagged as bots, kept out of reports."""
    paths = list(site.pages)
    for _ in range(int(days * 2.5 * site.share)):
        at = start + rng.randint(0, days * DAY)
        poster.pixel(at, "site=%s&u=%s" % (site.id, site.origin + rng.choice(paths)), rng.choice(BOT_UAS),
                     "66.249.66.%d" % rng.randint(1, 250), site.origin + "/")
    for _ in range(int(days * 0.7 * site.share)):
        at = start + rng.randint(0, days * DAY)
        pid = rid(rng, 12)
        env = {"v": 1, "site": site.id, "vid": rid(rng, 20), "sid": rid(rng), "sent": at, "lib": "ts.js/0.1.0", "try": 0, "events": [
            {"t": "pageview", "id": rid(rng, 12), "ts": at, "pid": pid, "url": site.origin + rng.choice(paths), "title": site.name,
             "ref": "", "nav": "navigate", "sn": 1, "pn": 1, "ns": 1, "new": 1, "idsrc": "new"},
            {"t": "fp", "id": rid(rng, 12), "ts": at + 50, "pid": pid, "h": "headless", "fv": 1,
             "bot": {"wd": 1, "pl": 0, "lg": 1, "oz": 1, "hl": 1, "co": 0, "au": ["__pwInitScripts"]}}]}
        poster.batch(site, at, env, HEADLESS_UA, "52.%d.%d.%d" % (rng.randint(0, 31), rng.randint(0, 255), rng.randint(1, 254)), "en-US")


def snapshots(site: Site, poster: Poster) -> None:
    """DOM snapshots of the pages that have a layout (heatmap backgrounds)."""
    now = int(time.time() * 1000)
    for path, page in site.pages.items():
        if not page.blocks:
            continue
        dom = snapshot_dom(site, path)
        poster.chunk(site, now - 3600000, "site=%s&k=snapshot&vid=snapvisitor0001&sid=snapsession01&pid=snap%s&seq=0&ts=%d&url=%s"
                     % (site.id, path.replace("/", "_"), now, site.origin + path),
                     [{"type": 4, "timestamp": now, "data": {"href": site.origin + path, "width": 1280, "height": page.height}},
                      {"type": 2, "timestamp": now + 1, "data": {"node": dom, "initialOffset": {"left": 0, "top": 0}}}], "demo", "5.10.0.1")


def live(cfg: Any, now: Optional[int] = None, minutes: int = 30, seed: Optional[int] = None,
         sites: Optional[List[str]] = None, scale: float = 1.0) -> int:
    """Visits of the last `minutes`, sent page by page and never after `now`: the Realtime page has visitors. Only
    raw hits: reports change at the next enrichment. Returns the number of visits."""
    now = now or int(time.time() * 1000)
    rng = random.Random(seed if seed is not None else now)
    poster = Poster(cfg)
    stats: Dict[str, int] = {}
    count = 0
    for sid in sites or [s for s in SITES if cfg.site(s) is not None]:
        site = SITES[sid]()
        expected = minutes * 1.4 * site.busy * scale
        n = int(expected) + (rng.random() < expected - int(expected))
        for _ in range(n):
            at = now - int(rng.random() * minutes * MINUTE)
            visit(site, poster, new_person(site, rng, at), at, 1, rng, True, stats, now=now)
            count += 1
    collect.now_ms = lambda: int(time.time() * 1000)
    return count


# ---- the dashboard side: people, annotations, segments ---------------------------------------------

TEAM = [("maya", "admin", 0.2), ("noah", "admin", 2.5), ("lea", "viewer", 0.9), ("sam", "viewer", 6.0), ("ines", "viewer", None)]


def team(cfg: Any, sites: List[Site]) -> None:
    """A few people (with passwords no one knows), sign-ins, an invitation not accepted yet, annotations on the
    remarkable days, saved segments."""
    from tidesight import users
    from tidesight.auth import hash_password
    from tidesight.webdb import WebDB

    now = int(time.time() * 1000)

    def change(table: Dict[str, Dict[str, Any]]) -> None:
        for name, role, _seen in TEAM:
            table[name] = {"hash": hash_password(secrets.token_urlsafe(18), iterations=1000) if _seen is not None else None,
                           "role": role, "created_at": now - 50 * DAY, "created_by": None if name == "maya" else "maya"}

    users.update(cfg.data_dir, change)
    web = WebDB(cfg.data_dir)
    conn = web._conn()
    try:
        for name, _role, seen in TEAM:
            if seen is not None:
                conn.execute("INSERT OR REPLACE INTO sign_ins(user, at) VALUES (?, ?)", (name, now - int(seen * DAY)))
    finally:
        conn.close()
    web.create_link("ines", "invite", "maya", 7 * 24)
    for site in sites:
        for days_before, text in [(s[0], s[3]) for s in site.spikes] + site.notes:
            day = time.strftime("%Y-%m-%d", time.gmtime((now - days_before * DAY) / 1000))
            web.add_annotation(site.id, day, text, "maya")
    web.add_segment("demo", "Mobile visitors", [{"dim": "device", "op": "is", "values": ["mobile"]}], "maya")
    web.add_segment("demo", "From the newsletter", [{"dim": "channel", "op": "is", "values": ["Email"]}], "maya")
    web.add_segment(None, "Visitors in France", [{"dim": "country", "op": "is", "values": ["FR"]}], "noah")


# ---- main --------------------------------------------------------------------------------------------

CONFIG = """# Demo configuration written by tools/demo.py (its properties are in data/properties.json)
[server]
data_dir = "data"
rate_limit_per_minute = 0
secret = "demo-secret"
journal_mode = "%(journal)s"
%(public_url)s
[enrich]
geo_dir = "data/geo"
%(dashboard)s"""


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="demo-data", help="folder for the demo configuration and data (default demo-data)")
    ap.add_argument("--days", type=int, default=60)
    ap.add_argument("--visitors", type=int, default=1500, help="the shop's visitors; the other properties in proportion")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--sites", default=",".join(SITES), help="properties to make, among %s" % ", ".join(SITES))
    ap.add_argument("--light", action="store_true", help="fewer events per page (Web Vitals and scroll as if sampled)")
    ap.add_argument("--team", action=argparse.BooleanOptionalAction, default=True,
                    help="people, annotations and saved segments (default: yes)")
    ap.add_argument("--demo", action="store_true", help="a read-only dashboard open to anyone ([dashboard] demo = true)")
    ap.add_argument("--journal-mode", default="wal", choices=["wal", "delete"], help="SQLite journal mode of the databases")
    ap.add_argument("--public-url", default="", help="[server] public_url, shown in tracking codes (e.g. https://stats.example.com/)")
    args = ap.parse_args(argv)
    wanted = [s.strip() for s in args.sites.split(",") if s.strip()]
    unknown = [s for s in wanted if s not in SITES]
    if unknown or not wanted:
        ap.error("unknown properties: %s (choose among %s)" % (", ".join(unknown) or "none", ", ".join(SITES)))
    sites = [SITES[s]() for s in wanted]
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)
    cfg_path = os.path.join(out, "tidesight.toml")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        fh.write(CONFIG % {"journal": args.journal_mode, "dashboard": "\n[dashboard]\ndemo = true\n" if args.demo else "",
                           "public_url": 'public_url = "%s"\n' % args.public_url if args.public_url else ""})
    data_dir = os.path.join(out, "data")
    for name in ("events.db", "replay.db", "derived.db", "web.db", "properties.json", "users.json", "update.json"):
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(os.path.join(data_dir, name + suffix))
            except OSError:
                pass
    os.makedirs(data_dir, exist_ok=True)
    from tidesight import properties
    properties.save_tables(data_dir, {s.id: s.table() for s in sites})
    build_geo(os.path.join(data_dir, "geo"))
    cfg = load(cfg_path)
    t0 = time.time()
    stats = simulate(cfg, sites, args.days, args.visitors, args.seed, args.light)
    print("posted %(sessions)d visits (%(requests)d requests), %(replays)d recordings" % stats, flush=True)
    from tidesight.enrich import pipeline
    result = pipeline.run(cfg)
    print("enrichment: %d hits, %d sessions in %.1f s" % (result["hits"], result["sessions"], time.time() - t0))
    if args.team:
        team(cfg, sites)
    n = live(cfg, sites=[s.id for s in sites], seed=args.seed)
    print("the last half hour: %d visits (Realtime)" % n)
    print("\nNext:\n  python server/python/devserver.py --config %s --no-auth --port 8790\n  open http://127.0.0.1:8790/dashboard/"
          % os.path.relpath(cfg_path))
    return 0


if __name__ == "__main__":
    sys.exit(main())
