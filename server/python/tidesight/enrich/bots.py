"""Bot classification for Tidesight enrichment.

Bots are **flagged, never deleted**: this module only labels hits so the
dashboard can filter them. It combines several kinds of evidence:

* known User-Agent patterns: search crawlers, SEO tools, AI crawlers and
  agents, link previews, uptime monitors, HTTP tools and libraries,
  headless browsers and automation frameworks;
* an optional external list in the ``crawler-user-agents.json`` format
  (see :func:`load_crawler_list`);
* signals collected by the tracker (``navigator.webdriver``, automation
  globals, headless hints, ``window.chrome``...);
* request-header anomalies (a browser UA without ``Accept-Language`` or
  ``Sec-Fetch-*`` headers, a ``From`` header, a ``Signature-Agent`` header);
* the network origin: hosting, cloud and VPN autonomous systems.

The module is standard library only and deterministic, so the enrichment
job that calls it can be re-run at any time. Bump :data:`BOTS_VERSION`
whenever patterns or scoring change so stored results are recomputed.
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from typing import Any, Dict, Iterable, List, Optional, Pattern, Tuple

__all__ = [
    "BOTS_VERSION",
    "BOT_THRESHOLD",
    "KINDS",
    "HOSTING_ASNS",
    "CrawlerList",
    "match_ua",
    "is_hosting",
    "load_crawler_list",
    "classify",
]

BOTS_VERSION = 1

log = logging.getLogger(__name__)

# Bot kinds -----------------------------------------------------------------

CRAWLER = "crawler"        # search engines, SEO tools, archives, feed fetchers
AI = "ai"                  # AI training crawlers, AI search and AI agents
PREVIEW = "preview"        # link unfurlers (social networks, chat apps)
MONITOR = "monitor"        # uptime and synthetic monitoring
TOOL = "tool"              # HTTP libraries, CLIs, auditing and security tools
HEADLESS = "headless"      # headless browsers
AUTOMATION = "automation"  # browser automation (webdriver, Playwright...)
DATACENTER = "datacenter"  # no-JS hit from a hosting network
SUSPICIOUS = "suspicious"  # several weak signals, not conclusive on their own

KINDS = (CRAWLER, AI, PREVIEW, MONITOR, TOOL, HEADLESS, AUTOMATION, DATACENTER, SUSPICIOUS)

BOT_THRESHOLD = 0.7
SUSPICIOUS_THRESHOLD = 0.5

# A pattern table entry is (regex, name, kind). A name of None means "use
# the matched text as the name", which keeps alternations compact. Order
# matters: the first matching entry wins, so specific tokens come before
# the more general ones they contain (e.g. "Googlebot-Image" before
# "Googlebot", "Applebot-Extended" before "Applebot"). All patterns are
# matched case-insensitively.

_AI_PATTERNS: List[Tuple[str, Optional[str], str]] = [
    (r"GPTBot", "GPTBot", AI),
    (r"ChatGPT-User", "ChatGPT-User", AI),
    (r"OAI-SearchBot", "OAI-SearchBot", AI),
    (r"ChatGPT[ -]Agent", "ChatGPT Agent", AI),
    (r"ClaudeBot", "ClaudeBot", AI),
    (r"Claude-User", "Claude-User", AI),
    (r"Claude-SearchBot", "Claude-SearchBot", AI),
    (r"Claude-Web", "Claude-Web", AI),
    (r"anthropic-ai", "anthropic-ai", AI),
    (r"PerplexityBot", "PerplexityBot", AI),
    (r"Perplexity-User", "Perplexity-User", AI),
    (r"CCBot", "CCBot", AI),
    (r"Bytespider", "Bytespider", AI),
    (r"Google-Extended", "Google-Extended", AI),
    (r"Google-CloudVertexBot", "Google-CloudVertexBot", AI),
    (r"Google-NotebookLM", "Google-NotebookLM", AI),
    (r"DuplexWeb-Google", "DuplexWeb-Google", AI),
    (r"Applebot-Extended", "Applebot-Extended", AI),
    (r"Amazonbot", "Amazonbot", AI),
    (r"Amzn-SearchBot", "Amzn-SearchBot", AI),
    (r"Amzn-User", "Amzn-User", AI),
    (r"meta-externalagent", "meta-externalagent", AI),
    (r"meta-externalfetcher", "meta-externalfetcher", AI),
    (r"meta-webindexer", "meta-webindexer", AI),
    (r"FacebookBot", "FacebookBot", AI),
    (r"cohere-training-data-crawler", "cohere-training-data-crawler", AI),
    (r"cohere-ai", "cohere-ai", AI),
    (r"Diffbot", "Diffbot", AI),
    (r"YouBot", "YouBot", AI),
    (r"Timpibot", "Timpibot", AI),
    (r"ImagesiftBot", "ImagesiftBot", AI),
    (r"Omgilibot|\bomgili\b", "Omgilibot", AI),
    (r"Webzio-Extended", "Webzio-Extended", AI),
    (r"AI2Bot", "AI2Bot", AI),
    (r"PanguBot", "PanguBot", AI),
    (r"Kangaroo Bot", "Kangaroo Bot", AI),
    (r"img2dataset", "img2dataset", AI),
    (r"DuckAssistBot", "DuckAssistBot", AI),
    (r"MistralAI-User", "MistralAI-User", AI),
    (r"PhindBot", "PhindBot", AI),
    (r"iaskspider", "iAskSpider", AI),
    (r"Andibot", "Andibot", AI),
    (r"ChatGLM-Spider", "ChatGLM-Spider", AI),
    (r"LinerBot", "LinerBot", AI),
    (r"Cotoyogi", "Cotoyogi", AI),
]

_CRAWLER_PATTERNS: List[Tuple[str, Optional[str], str]] = [
    # Google (specific crawlers first: their tokens contain "Googlebot").
    (r"Googlebot-Image", "Googlebot-Image", CRAWLER),
    (r"Googlebot-Video", "Googlebot-Video", CRAWLER),
    (r"Googlebot-News", "Googlebot-News", CRAWLER),
    (r"Google-InspectionTool", "Google-InspectionTool", CRAWLER),
    (r"Storebot-Google", "Storebot-Google", CRAWLER),
    (r"AdsBot-Google", "AdsBot-Google", CRAWLER),
    (r"Mediapartners-Google", "Mediapartners-Google", CRAWLER),
    (r"APIs-Google", "APIs-Google", CRAWLER),
    (r"FeedFetcher-Google", "FeedFetcher-Google", CRAWLER),
    (r"Google-Site-Verification", "Google-Site-Verification", CRAWLER),
    (r"GoogleOther", "GoogleOther", CRAWLER),
    (r"Google-Safety", "Google-Safety", CRAWLER),
    (r"GoogleProducer", "GoogleProducer", CRAWLER),
    (r"Google Favicon", "Google Favicon", CRAWLER),
    (r"Google-AMPHTML", "Google-AMPHTML", CRAWLER),
    (r"Googlebot", "Googlebot", CRAWLER),
    # Microsoft / Bing.
    (r"bingbot", "Bingbot", CRAWLER),
    (r"msnbot", "msnbot", CRAWLER),
    (r"adidxbot", "AdIdxBot", CRAWLER),
    # Other search engines.
    (r"Yahoo! ?Slurp|\bSlurp\b", "Yahoo Slurp", CRAWLER),
    (r"DuckDuckGo-Favicons-Bot", "DuckDuckGo-Favicons-Bot", CRAWLER),
    (r"DuckDuckBot", "DuckDuckBot", CRAWLER),
    (r"Baiduspider", "Baiduspider", CRAWLER),
    (
        r"Yandex(?:Bot|Images|MobileBot|Accessibility ?Bot|Metrika|Direct(?:Dyn)?|Video(?:Parser)?"
        r"|Media|Favicons|Webmaster|RenderResourcesBot|ScreenshotBot|Additional|AdNet|Market"
        r"|Turbo|News|Blogs|Calendar|Sprav ?Bot|Tracker|Verticals|PagesChecker|ImageResizer"
        r"|ComBot|SearchShop|OntoDB)",
        "YandexBot",
        CRAWLER,
    ),
    (r"Applebot", "Applebot", CRAWLER),
    (r"SeznamBot", "SeznamBot", CRAWLER),
    (r"Sogou.{0,20}spider", "Sogou Spider", CRAWLER),
    (r"Exabot", "Exabot", CRAWLER),
    (r"PetalBot", "PetalBot", CRAWLER),
    (r"AspiegelBot", "AspiegelBot", CRAWLER),
    (r"Qwantify|Qwantbot|Qwant-news", "Qwantbot", CRAWLER),
    (r"MojeekBot", "MojeekBot", CRAWLER),
    (r"\bYeti/", "Naver Yeti", CRAWLER),
    (r"coccocbot", "Coccocbot", CRAWLER),
    (r"\bDaum(?:oa)?[ /]", "Daum", CRAWLER),
    (r"YisouSpider", "YisouSpider", CRAWLER),
    (r"360Spider|HaosouSpider", "360Spider", CRAWLER),
    (r"SeekportBot", "SeekportBot", CRAWLER),
    (r"TikTokSpider", "TikTokSpider", CRAWLER),
    (r"Mail\.RU_Bot", "Mail.RU_Bot", CRAWLER),
    (r"ia_archiver|archive\.org_bot|Wayback Save Page", "Internet Archive", CRAWLER),
    (r"\bNutch\b", "Apache Nutch", CRAWLER),
    (r"Heritrix", "Heritrix", CRAWLER),
    # SEO tools.
    (r"AhrefsBot|AhrefsSiteAudit", "AhrefsBot", CRAWLER),
    (r"SemrushBot|SiteAuditBot|SplitSignalBot", "SemrushBot", CRAWLER),
    (r"MJ12bot", "MJ12bot", CRAWLER),
    (r"DotBot", "DotBot", CRAWLER),
    (r"rogerbot", "rogerbot", CRAWLER),
    (r"Screaming Frog", "Screaming Frog SEO Spider", CRAWLER),
    (r"serpstatbot", "serpstatbot", CRAWLER),
    (r"BLEXBot", "BLEXBot", CRAWLER),
    (r"DataForSeoBot", "DataForSeoBot", CRAWLER),
    (r"SEOkicks", "SEOkicks", CRAWLER),
    (r"SeobilityBot", "SeobilityBot", CRAWLER),
    (r"SISTRIX", "SISTRIX", CRAWLER),
    (r"linkdexbot", "Linkdexbot", CRAWLER),
    (r"MegaIndex", "MegaIndex", CRAWLER),
    (r"Sitebulb", "Sitebulb", CRAWLER),
    (r"\bbotify\b", "Botify", CRAWLER),
    (r"OnCrawl", "OnCrawl", CRAWLER),
    (r"DeepCrawl", "DeepCrawl", CRAWLER),
    (r"ContentKing", "ContentKing", CRAWLER),
    (r"Siteimprove", "Siteimprove", CRAWLER),
    (r"\bspbot\b", "spbot", CRAWLER),
    (r"woorank", "WooRank", CRAWLER),
    (r"Barkrowler", "Barkrowler", CRAWLER),
    # Feed fetchers.
    (r"\b(?:Feedly|Feedbin|NewsBlur|Inoreader|FreshRSS|Miniflux|Tiny Tiny RSS|Feedspot)\b", None, CRAWLER),
]

_PREVIEW_PATTERNS: List[Tuple[str, Optional[str], str]] = [
    # iMessage announces itself with all three tokens at once.
    (r"facebookexternalhit.*Twitterbot|Twitterbot.*facebookexternalhit", "iMessage", PREVIEW),
    (r"facebookexternalhit", "facebookexternalhit", PREVIEW),
    (r"\bFacebot\b", "Facebot", PREVIEW),
    (r"TelegramBot", "TelegramBot", PREVIEW),   # before Twitterbot: "TelegramBot (like TwitterBot)"
    (r"Twitterbot", "Twitterbot", PREVIEW),
    (r"Slack-ImgProxy", "Slack-ImgProxy", PREVIEW),
    (r"Slackbot", "Slackbot", PREVIEW),
    (r"Discordbot", "Discordbot", PREVIEW),
    (r"\bWhatsApp/\d", "WhatsApp", PREVIEW),
    (r"LinkedInBot", "LinkedInBot", PREVIEW),
    (r"SkypeUriPreview", "SkypeUriPreview", PREVIEW),
    (r"Pinterestbot|Pinterest/0\.", "Pinterestbot", PREVIEW),
    (r"redditbot", "redditbot", PREVIEW),
    (r"Embedly", "Embedly", PREVIEW),
    (r"Iframely", "Iframely", PREVIEW),
    (r"vkShare", "vkShare", PREVIEW),
    (r"\bMastodon/\d", "Mastodon", PREVIEW),
    (
        r"\b(?:Pleroma|Akkoma|Misskey|Friendica|GoToSocial|Pixelfed|Lemmy|Mbin|Sharkey|Firefish"
        r"|Iceshrimp)/\d",
        None,
        PREVIEW,
    ),
    (r"Bluesky Cardyb|\bCardyb/", "Bluesky", PREVIEW),
    (r"Snap URL Preview", "Snapchat Preview", PREVIEW),
    (r"kakaotalk-scrap", "KakaoTalk Preview", PREVIEW),
    (r"\bTumblr/\d", "Tumblr", PREVIEW),
    (r"Quora Link Preview", "Quora Link Preview", PREVIEW),
    (r"FlipboardProxy|FlipboardRSS", "Flipboard", PREVIEW),
    (r"Yahoo Link Preview", "Yahoo Link Preview", PREVIEW),
    (r"Discourse(?: Forum)? Onebox", "Discourse Onebox", PREVIEW),
    (r"Synapse \(bot", "Matrix Synapse", PREVIEW),
    (r"bitlybot", "bitlybot", PREVIEW),
    (r"XING-contenttabreceiver", "XING", PREVIEW),
    (r"\bMicrolink\b", "Microlink", PREVIEW),
    (r"BingPreview", "BingPreview", PREVIEW),
    (r"MicrosoftPreview", "MicrosoftPreview", PREVIEW),
    (r"Google Web Preview|Google-PageRenderer", "Google Web Preview", PREVIEW),
]

_MONITOR_PATTERNS: List[Tuple[str, Optional[str], str]] = [
    (r"UptimeRobot", "UptimeRobot", MONITOR),
    (r"Pingdom", "Pingdom", MONITOR),
    (r"StatusCake", "StatusCake", MONITOR),
    (r"Site24x7", "Site24x7", MONITOR),
    (r"Better ?Uptime|Better ?Stack", "Better Stack", MONITOR),
    (r"Uptime-Kuma", "Uptime Kuma", MONITOR),
    (r"Datadog ?/? ?Synthetics", "Datadog Synthetics", MONITOR),
    (r"NewRelicPinger|NewRelicSynthetics", "New Relic Synthetics", MONITOR),
    (r"\bCheckly\b", "Checkly", MONITOR),
    (r"HetrixTools", "HetrixTools", MONITOR),
    (r"Freshping", "Freshping", MONITOR),
    (r"Zabbix", "Zabbix", MONITOR),
    (r"nagios|check_http/", "Nagios", MONITOR),
    (r"updown\.io", "updown.io", MONITOR),
    (r"OhDear", "Oh Dear", MONITOR),
    (r"Uptimebot|Uptime\.com", "Uptime.com", MONITOR),
    (r"Blackbox Exporter", "Prometheus Blackbox Exporter", MONITOR),
    (r"RuxitSynthetic", "Dynatrace Synthetic", MONITOR),
    (r"Catchpoint", "Catchpoint", MONITOR),
    (r"Jetpack by WordPress", "Jetpack Monitor", MONITOR),
    (r"ManageWP", "ManageWP", MONITOR),
    (r"MainWP", "MainWP", MONITOR),
    (r"GoogleStackdriverMonitoring", "Google Cloud Monitoring", MONITOR),
    (r"Amazon-Route53-Health-Check", "Route 53 Health Check", MONITOR),
    (r"ELB-HealthChecker", "ELB Health Checker", MONITOR),
    (r"kube-probe", "kube-probe", MONITOR),
    (r"Consul Health Check", "Consul Health Check", MONITOR),
    (r"GoogleHC/", "Google Health Check", MONITOR),
    (r"Azure Traffic Manager", "Azure Traffic Manager", MONITOR),
    (r"Cloudflare-Healthchecks|Cloudflare-Traffic-Manager", "Cloudflare Health Checks", MONITOR),
    (r"NodePing", "NodePing", MONITOR),
    (r"Uptrends", "Uptrends", MONITOR),
]

_HEADLESS_PATTERNS: List[Tuple[str, Optional[str], str]] = [
    (r"HeadlessChrome", "HeadlessChrome", HEADLESS),
    (r"PhantomJS", "PhantomJS", HEADLESS),
    (r"\bPuppeteer\b", "Puppeteer", HEADLESS),
    (r"SlimerJS", "SlimerJS", HEADLESS),
    (r"Zombie\.js", "Zombie.js", HEADLESS),
    (r"\bjsdom/", "jsdom", HEADLESS),
    (r"HtmlUnit", "HtmlUnit", HEADLESS),
    (r"\bCypress/", "Cypress", AUTOMATION),
    (r"\bPlaywright\b", "Playwright", AUTOMATION),
    (r"\bSelenium\b", "Selenium", AUTOMATION),
]

_TOOL_PATTERNS: List[Tuple[str, Optional[str], str]] = [
    # Auditing and testing services.
    (r"Chrome-Lighthouse|\bLighthouse/", "Lighthouse", TOOL),
    (r"Page ?Speed ?Insights|Google-PageSpeed", "PageSpeed Insights", TOOL),
    (r"GTmetrix", "GTmetrix", TOOL),
    (r"\bPTST/\d|WebPageTest", "WebPageTest", TOOL),
    (r"Google-Apps-Script", "Google Apps Script", TOOL),
    (r"GoogleDocs; apps-", "Google Docs", TOOL),
    (r"Google-Read-Aloud", "Google Read Aloud", TOOL),
    (r"W3C_Validator|W3C-checklink|W3C_CSS_Validator|Validator\.nu", "W3C Validator", TOOL),
    # Command-line clients and HTTP libraries.
    (r"\bcurl/\d", "curl", TOOL),
    (r"\bWget/", "Wget", TOOL),
    (r"python-requests/", "python-requests", TOOL),
    (r"Python-urllib/", "python-urllib", TOOL),
    (r"python-httpx/", "httpx", TOOL),
    (r"aiohttp/", "aiohttp", TOOL),
    (r"urllib3/", "urllib3", TOOL),
    (r"PycURL/", "PycURL", TOOL),
    (r"Scrapy/", "Scrapy", TOOL),
    (r"Go-http-client/", "Go-http-client", TOOL),
    (r"\bcolly\b", "Colly", TOOL),
    (r"(?:^|\s)Java/\d", "Java", TOOL),
    (r"Apache-HttpClient/", "Apache-HttpClient", TOOL),
    (r"Jakarta Commons-HttpClient", "Commons-HttpClient", TOOL),
    (r"\bokhttp/", "okhttp", TOOL),
    (r"\baxios/", "axios", TOOL),
    (r"node-fetch", "node-fetch", TOOL),
    (r"\bundici\b", "undici", TOOL),
    (r"^node$", "Node.js fetch", TOOL),
    (r"\bgot \(https://github\.com/sindresorhus/got\)", "got", TOOL),
    (r"node-superagent", "superagent", TOOL),
    (r"^Deno/", "Deno", TOOL),
    (r"^Bun/", "Bun", TOOL),
    (r"PostmanRuntime/", "Postman", TOOL),
    (r"\binsomnia/", "Insomnia", TOOL),
    (r"Thunder Client", "Thunder Client", TOOL),
    (r"HTTPie/", "HTTPie", TOOL),
    (r"libwww-perl", "libwww-perl", TOOL),
    (r"WWW-Mechanize|\bMechanize/", "Mechanize", TOOL),
    (r"GuzzleHttp/", "Guzzle", TOOL),
    (r"^PHP/|PHP-Curl-Class|Zend_Http_Client|Symfony HttpClient", "PHP", TOOL),
    (r"^WordPress/\d", "WordPress", TOOL),
    (r"^Ruby\b|Faraday v\d|rest-client/|Typhoeus", "Ruby", TOOL),
    (r"\bhttp\.rb/", "http.rb", TOOL),
    (r"^Dart/", "Dart", TOOL),
    (r"\breqwest/", "reqwest", TOOL),
    (r"^hyper/", "hyper", TOOL),
    (r"lua-resty-http", "lua-resty-http", TOOL),
    (r"WinHttp|Microsoft URL Control", "WinHTTP", TOOL),
    (r"HTTrack", "HTTrack", TOOL),
    (r"\bk6/\d", "k6", TOOL),
    (r"\bGatling\b", "Gatling", TOOL),
    # Scanners.
    (r"Nmap Scripting Engine", "Nmap", TOOL),
    (r"\bmasscan\b", "masscan", TOOL),
    (r"\bzgrab\b", "zgrab", TOOL),
    (r"\bNuclei\b", "Nuclei", TOOL),
    (r"\bNikto\b", "Nikto", TOOL),
    (r"\bsqlmap\b", "sqlmap", TOOL),
    (r"WPScan", "WPScan", TOOL),
    (r"CensysInspect", "CensysInspect", TOOL),
    (r"\bExpanse\b", "Expanse", TOOL),
    (r"NetcraftSurveyAgent", "Netcraft", TOOL),
    (r"InternetMeasurement", "InternetMeasurement", TOOL),
    (r"ModatScanner", "Modat", TOOL),
    # A bare "Mozilla/5.0" is what scanners and mail-privacy prefetchers send.
    (r"^Mozilla/\d\.\d$", "Bare Mozilla", TOOL),
]

# Order of the groups: AI first (their tokens are the most specific), then
# crawlers, previews (before tools: LinkedInBot embeds "Apache-HttpClient",
# Mastodon embeds "http.rb"), monitors, headless/automation, tools.
_PATTERN_TABLE = (
    _AI_PATTERNS + _CRAWLER_PATTERNS + _PREVIEW_PATTERNS + _MONITOR_PATTERNS
    + _HEADLESS_PATTERNS + _TOOL_PATTERNS
)

# Literal gate ----------------------------------------------------------------
# Running hundreds of regexes (or one huge alternation) over every UA costs
# about half a millisecond. Most patterns are plain text, so the gate keeps,
# per pattern, the literal each alternative cannot match without; only the
# patterns whose literal occurs in the UA are run. Patterns with groups or
# character classes are not analysed and always run.

_ESCAPE_RE = re.compile(r"\\.")
_REPEAT_RE = re.compile(r"\{\d*,?\d*\}")
_LITERAL_RUN_RE = re.compile(r"[A-Za-z0-9_-]{2,}")


def _required_literals(source: str) -> Optional[List[str]]:
    """One literal per top-level alternative, or None when not analysable."""
    # Escapes break literal runs; "{m,n}" may repeat zero times, like "?".
    text = _REPEAT_RE.sub("?", _ESCAPE_RE.sub("\x00", source))
    if any(ch in text for ch in "()[]"):
        return None
    literals = []
    for branch in text.split("|"):
        best = ""
        for run in _LITERAL_RUN_RE.finditer(branch):
            literal = run.group(0)
            if branch[run.end():run.end() + 1] in ("?", "*"):
                literal = literal[:-1]          # the last character is optional
            if len(literal) > len(best):
                best = literal
        if len(best) < 2:
            return None
        literals.append(best)
    return literals


class _LiteralGate:
    """Selects the patterns that can possibly match a given UA."""

    def __init__(self, patterns: Iterable[Any]) -> None:
        self.always: List[int] = []
        self.insensitive: Dict[str, List[int]] = {}
        self.sensitive: Dict[str, List[int]] = {}
        for index, regex in enumerate(patterns):
            try:
                literals = _required_literals(regex.pattern)
                ignore_case = bool(regex.flags & re.IGNORECASE)
            except (AttributeError, TypeError):
                literals, ignore_case = None, False
            if literals is None:
                self.always.append(index)
                continue
            table = self.insensitive if ignore_case else self.sensitive
            for literal in literals:
                table.setdefault(literal.lower() if ignore_case else literal, []).append(index)

    def candidates(self, ua: str) -> List[int]:
        """Indexes of the patterns worth running, in table order."""
        found = set(self.always)
        low = ua.lower()
        for literal, indexes in self.insensitive.items():
            if literal in low:
                found.update(indexes)
        for literal, indexes in self.sensitive.items():
            if literal in ua:
                found.update(indexes)
        return sorted(found)


_Compiled = Tuple[Pattern, Optional[str], str]


def _compile_table(table: Iterable[Tuple[str, Optional[str], str]]) -> List[_Compiled]:
    """Compile a pattern table, case-insensitively."""
    return [(re.compile(pattern, re.IGNORECASE), name, kind) for pattern, name, kind in table]


_SPECIFIC: List[_Compiled] = _compile_table(_PATTERN_TABLE)
_SPECIFIC_GATE = _LiteralGate(regex for regex, _, _ in _SPECIFIC)

# Generic fallbacks ------------------------------------------------------------

# Product tokens such as "FooBot/1.2" or "Example-Crawler".
_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]*")
# Tokens that contain "bot" but name real devices ("CUBOT X30", "CUBOT_NOTE_20").
_NOT_BOT_PREFIXES = ("cubot",)
_CRAWLER_WORDS = ("crawler", "crawling", "spider", "slurp", "archiver", "indexer")
_TOOL_WORDS = ("scraper", "scrapy", "fetcher", "httpclient", "http-client", "http_client",
               "checker", "linkcheck", "validator")
_MONITOR_WORDS = ("monitor", "monitoring", "healthcheck", "health-check")
# Every word the generic rules react to: a UA with none of them is skipped.
_GENERIC_HINTS = ("bot", "headless", "http") + _CRAWLER_WORDS + _TOOL_WORDS + _MONITOR_WORDS
_URL_IN_UA_RE = re.compile(r"\+\s?https?://", re.IGNORECASE)
_CONTACT_RE = re.compile(r"\+?\s?(?:https?://|www\.)[^\s;)]*|[\w.+-]+@[\w.-]+", re.IGNORECASE)
_COMPATIBLE_NAME_RE = re.compile(r"compatible;\s*([A-Za-z][\w.\- ]*?)\s*(?:/|;|\)|$)", re.IGNORECASE)
_FIRST_PRODUCT_RE = re.compile(r"^\s*([A-Za-z][\w.\-]*)/")


def _token_kind(token: str) -> Optional[str]:
    """Return the bot kind a single UA token suggests, if any."""
    low = token.lower()
    if low.startswith(_NOT_BOT_PREFIXES):
        return None
    if low.endswith(("bot", "bots")) or "bot-" in low or "bot_" in low:
        return CRAWLER
    if any(word in low for word in _CRAWLER_WORDS):
        return CRAWLER
    if any(word in low for word in _MONITOR_WORDS):
        return MONITOR
    if any(word in low for word in _TOOL_WORDS):
        return TOOL
    if "headless" in low:
        return HEADLESS
    return None


def _url_bot_name(ua: str) -> str:
    """Best-effort name for a UA that embeds a "+http://" contact URL."""
    match = _COMPATIBLE_NAME_RE.search(ua)
    if match and not match.group(1).upper().startswith("MSIE"):
        return match.group(1).strip()
    match = _FIRST_PRODUCT_RE.match(ua)
    if match and match.group(1).lower() != "mozilla":
        return match.group(1)
    return "Unknown bot"


@lru_cache(maxsize=8192)
def _match_generic(ua: str) -> Optional[Tuple[str, str]]:
    """Generic fallbacks for bots that no specific pattern knows."""
    if not any(ch.isalpha() for ch in ua):
        return ("Invalid User-Agent", TOOL)
    low = ua.lower()
    if not any(word in low for word in _GENERIC_HINTS):
        return None
    # Contact URLs and e-mail addresses ("+https://example.com/bot") say
    # nothing about the product name, so they are left out of the scan.
    for token_match in _TOKEN_RE.finditer(_CONTACT_RE.sub(" ", ua)):
        token = token_match.group(0).strip("._-")
        kind = _token_kind(token)
        if kind:
            return (token, kind)
    if _URL_IN_UA_RE.search(ua):
        return (_url_bot_name(ua), CRAWLER)
    return None


@lru_cache(maxsize=8192)
def _match_specific(ua: str) -> Optional[Tuple[str, str]]:
    """Match the built-in table of known bots (first entry in table order wins)."""
    for index in _SPECIFIC_GATE.candidates(ua):
        regex, name, kind = _SPECIFIC[index]
        match = regex.search(ua)
        if match:
            return (name or match.group(0).strip(), kind)
    return None


def _clean_ua(ua: Any) -> str:
    """Coerce a raw User-Agent value to a stripped string ("" if unusable)."""
    if isinstance(ua, bytes):
        ua = ua.decode("utf-8", "replace")
    if not isinstance(ua, str):
        return ""
    return ua.strip()[:2048]


def match_ua(ua: Optional[str]) -> Optional[Tuple[str, str]]:
    """Return ``(bot_name, bot_kind)`` when the User-Agent is a known bot.

    Only the UA string is considered: specific built-in patterns first, then
    generic fallbacks (``...bot``, ``crawler``, ``spider``, a ``+http://``
    contact URL...). An empty or missing UA returns None here; see
    :func:`classify` for the full decision.
    """
    text = _clean_ua(ua)
    if not text:
        return None
    return _match_specific(text) or _match_generic(text)


class CrawlerList(list):
    """Patterns of an external crawler list: a plain ``List[Tuple[Pattern, str]]``
    that also caches a literal gate, so matching it stays fast."""

    def gate(self) -> _LiteralGate:
        cached = self.__dict__.get("_gate")
        if cached is None or cached[0] != len(self):
            patterns = [item[0] if isinstance(item, tuple) and item else None for item in self]
            cached = (len(self), _LiteralGate(patterns))
            self.__dict__["_gate"] = cached
        return cached[1]


def _match_extra(ua: str, extra_patterns: Optional[List[Tuple[Pattern, str]]]) -> Optional[Tuple[str, str]]:
    """Match an external crawler list (its kind is always "crawler")."""
    if not extra_patterns:
        return None
    items = extra_patterns if isinstance(extra_patterns, list) else list(extra_patterns)
    if isinstance(items, CrawlerList):
        indexes: Iterable[int] = items.gate().candidates(ua)
    else:
        indexes = range(len(items))
    for index in indexes:
        try:
            regex, name = items[index]
            if regex.search(ua):
                return (name, CRAWLER)
        except (TypeError, ValueError, AttributeError):
            continue
    return None

# Hosting, cloud and VPN networks ---------------------------------------------

# Autonomous systems of clouds, hosting companies, CDNs/proxies and VPN
# providers. A hit from one of them is either automated or a visitor behind
# a VPN/relay, so on its own it is only "suspicious"; combined with a no-JS
# hit it becomes a "datacenter" bot. Extend this dict at runtime if needed.
HOSTING_ASNS: Dict[int, str] = {
    # Hyperscalers.
    16509: "Amazon AWS", 14618: "Amazon AWS", 8987: "Amazon AWS", 7224: "Amazon AWS",
    38895: "Amazon AWS",
    15169: "Google", 396982: "Google Cloud", 19527: "Google",
    8075: "Microsoft Azure", 8068: "Microsoft", 8069: "Microsoft", 8070: "Microsoft",
    8071: "Microsoft", 8072: "Microsoft", 8073: "Microsoft", 8074: "Microsoft",
    31898: "Oracle Cloud", 36351: "IBM Cloud (SoftLayer)",
    45102: "Alibaba Cloud", 37963: "Alibaba Cloud",
    132203: "Tencent Cloud", 45090: "Tencent Cloud",
    136907: "Huawei Cloud", 55990: "Huawei Cloud",
    # Developer clouds and VPS providers.
    14061: "DigitalOcean", 393406: "DigitalOcean", 394362: "DigitalOcean",
    200130: "DigitalOcean", 62567: "DigitalOcean",
    63949: "Linode (Akamai)", 20473: "Vultr (Choopa)", 16276: "OVHcloud",
    24940: "Hetzner", 213230: "Hetzner Cloud", 212317: "Hetzner Cloud",
    51167: "Contabo", 141995: "Contabo", 12876: "Scaleway", 202053: "UpCloud",
    36007: "Kamatera", 54825: "Equinix Metal", 396356: "Latitude.sh", 262287: "Maxihost",
    # CDNs, proxies and relays (WARP, iCloud Private Relay egress).
    13335: "Cloudflare", 209242: "Cloudflare (WARP)", 54113: "Fastly",
    20940: "Akamai", 16625: "Akamai", 36183: "Akamai (Private Relay)", 32787: "Akamai (Prolexic)",
    19551: "Imperva Incapsula", 199524: "G-Core Labs", 21859: "Zenlayer",
    # VPN-heavy and general hosting networks.
    9009: "M247", 60068: "Datacamp (CDN77)", 212238: "Datacamp",
    136787: "TEFINCOM (NordVPN)", 147049: "PacketHub (NordVPN)", 39351: "31173 Services (Mullvad)",
    60781: "Leaseweb", 28753: "Leaseweb", 16265: "Leaseweb", 30633: "Leaseweb",
    59253: "Leaseweb", 395954: "Leaseweb", 32613: "Leaseweb (iWeb)",
    47583: "Hostinger", 8560: "IONOS", 54290: "Hostwinds",
    26496: "GoDaddy", 398101: "GoDaddy", 30083: "GoDaddy", 44273: "Host Europe", 20773: "Host Europe",
    26347: "DreamHost", 46606: "Unified Layer (Bluehost, HostGator)",
    40676: "Psychz Networks", 8100: "QuadraNet", 36352: "ColoCrossing",
    22612: "Namecheap", 32244: "Liquid Web",
    19994: "Rackspace", 27357: "Rackspace", 33070: "Rackspace", 15395: "Rackspace",
    19318: "InterServer", 29802: "HIVELOCITY", 53667: "FranTech (BuyVM)",
    35916: "Multacom", 25820: "IT7 Networks", 13213: "UK2 Net",
    49981: "WorldStream", 50673: "Serverius", 62240: "Clouvider",
    49505: "Selectel", 9123: "Timeweb", 197695: "REG.RU", 197540: "netcup", 6724: "Strato",
    55286: "ServerMania", 46844: "Sharktech", 30058: "FDCservers", 23470: "ReliableSite",
    32097: "WholeSale Internet", 33387: "DataShack", 46562: "Performive", 63023: "GTHost",
    20857: "TransIP", 31034: "Aruba", 9370: "SAKURA Internet", 43350: "NForce Entertainment",
    202425: "IP Volume", 44477: "Stark Industries", 3223: "Voxility", 49544: "i3D.net",
    57169: "EDIS", 12182: "INAP", 13768: "Cogeco Peer 1", 32475: "SingleHop",
    15003: "Nobis Technology", 40244: "Turnkey Internet", 27715: "Locaweb",
    15817: "Mittwald", 34788: "all-inkl.com", 12824: "home.pl",
    # Internet-wide scanners.
    398324: "Censys",
}

# Networks whose names look like hosting but that serve residential users.
_NOT_HOSTING_ASNS = frozenset({
    35540,  # OVH Telecom (French ISP, same "OVH SAS" org name as OVHcloud)
    16591,  # Google Fiber
})

_HOSTING_ORG_RE = re.compile(
    r"hosting|webhost|\bhost(?:ed|er|s)?\b|server|cloud|data ?cent(?:er|re)|\bcolo\b|colocation"
    r"|\bvps\b|\bvds\b|dedicated|\bvpn\b|proxy|proxies|virtual private"
    r"|datacamp|digitalocean|linode|vultr|choopa|\bovh|hetzner|contabo|scaleway|leaseweb"
    r"|amazon|\baws\b|azure|microsoft|oracle|alibaba|aliyun|tencent|hwclouds|akamai|fastly"
    r"|\bm247\b|hostinger|ionos|godaddy|dreamhost|new-dream|unified ?layer|psychz|quadranet"
    r"|colocrossing|hostwinds|interserver|hivelocity|liquid ?web|rackspace|softlayer|namecheap"
    r"|kamatera|upcloud|netcup|\bstrato\b|g-core|gcore|selectel|timeweb|packethub|tefincom"
    r"|nforce|worldstream|serverius|clouvider|zenlayer|censys|equinix|sharktech|servermania"
    r"|frantech|ponynet|multacom|voxility|incapsula|imperva|stark industries",
    re.IGNORECASE,
)


def _as_int(value: Any) -> Optional[int]:
    """Parse an ASN given as 16509, "16509" or "AS16509"."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip().upper()
        if text.startswith("AS"):
            text = text[2:]
        if text.isdigit():
            return int(text)
    return None


def is_hosting(asn: Optional[int], as_org: Optional[str]) -> bool:
    """True when the network belongs to a cloud, hosting, CDN or VPN provider.

    The ASN is checked against :data:`HOSTING_ASNS`; when it is unknown the
    organisation name is matched against hosting keywords and provider
    names. Residential networks with misleading names are excluded.
    """
    number = _as_int(asn)
    if number is not None:
        if number in _NOT_HOSTING_ASNS:
            return False
        if number in HOSTING_ASNS:
            return True
    if isinstance(as_org, str) and as_org.strip():
        return bool(_HOSTING_ORG_RE.search(as_org))
    return False


# External crawler list ---------------------------------------------------------

_REGEX_CLASS_RE = re.compile(r"\[([^\]])[^\]]*\]")


def _name_from_pattern(pattern: str) -> str:
    """Derive a readable bot name from a crawler-list regex."""
    text = pattern.split("|", 1)[0]
    text = _REGEX_CLASS_RE.sub(r"\1", text)          # "[wW]get" -> "wget"
    text = re.sub(r"\\[bBdDsSwW]", "", text)          # drop \b, \d...
    text = re.sub(r"\\(.)", r"\1", text)               # unescape "\/" etc.
    text = re.sub(r"[\^\$\(\)\*\+\?\{\}]|\.\*", "", text)
    text = text.strip(" /;:.-_")
    return text or pattern


def load_crawler_list(path: str) -> List[Tuple[Pattern, str]]:
    """Load a crawler list in the ``crawler-user-agents.json`` format.

    The file is a JSON array of objects such as ``{"pattern": "Googlebot\\/",
    "url": ..., "instances": [...]}``; plain strings are accepted too.
    Patterns are matched case-sensitively, as the list's authors intend.
    The result is a :class:`CrawlerList` (a plain list that also caches a
    literal gate); pass it to :func:`classify` as ``extra_patterns`` and
    reuse it across calls. A missing or invalid file, and any invalid
    pattern, is logged and skipped: detection then relies on the built-in
    patterns.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError) as exc:
        log.warning("crawler list %r not loaded: %s", path, exc)
        return []
    if not isinstance(data, list):
        log.warning("crawler list %r not loaded: expected a JSON array", path)
        return []
    patterns = CrawlerList()
    for entry in data:
        source = entry.get("pattern") if isinstance(entry, dict) else entry
        if not isinstance(source, str) or not source:
            continue
        try:
            regex = re.compile(source)
        except re.error as exc:
            log.warning("crawler list %r: skipping invalid pattern %r: %s", path, source, exc)
            continue
        name = entry.get("name") if isinstance(entry, dict) else None
        patterns.append((regex, name if isinstance(name, str) and name else _name_from_pattern(source)))
    return patterns

# Classification ------------------------------------------------------------------

# Weights. A strong signal sets the kind and the base score; weak signals add
# up on top of it (the total is capped at 1.0). is_bot = score >= 0.7.
_W_KNOWN_UA = 1.0
_W_EMPTY_UA = 1.0
_W_SIGNATURE_AGENT = 1.0
_W_AUTOMATION_GLOBALS = 1.0
_W_WEBDRIVER = 0.95
_W_HEADLESS_HINT = 0.9
_W_DATACENTER_NO_JS = 0.8
_W_FROM_HEADER = 0.8
_W_HOSTING = 0.5
_W_NO_ACCEPT_LANGUAGE = 0.3
_W_NO_WINDOW_CHROME = 0.3
_W_OUTER_ZERO = 0.3
_W_NO_SEC_FETCH = 0.2
_W_NO_LANGUAGES = 0.2
_W_NO_PLUGINS = 0.1

_BROWSER_UA_RE = re.compile(r"^Mozilla/\d")
_CHROME_RE = re.compile(r"\bChrome/(\d+)")
_FIREFOX_RE = re.compile(r"\bFirefox/(\d+)")
_SAFARI_RE = re.compile(r"\bVersion/(\d+)[\w.]*(?: Mobile/\S+)? Safari/")
_IOS_RE = re.compile(r"iPhone|iPad|iPod|CriOS|FxiOS|EdgiOS|OPiOS")
_NOT_DESKTOP_RE = re.compile(r"Android|Mobile|CriOS|FxiOS|iPhone|iPad|iPod|; wv\)")
# Mail image proxies fetch on behalf of a person opening an e-mail.
_MAIL_PROXY_RE = re.compile(r"GoogleImageProxy|YahooMailProxy", re.IGNORECASE)
_URL_HOST_RE = re.compile(r"https?://([^/\"'\s;,]+)", re.IGNORECASE)

# Crawlers whose genuine traffic only comes from their owner's network.
_VERIFIED_ASNS: Dict[str, frozenset] = {
    "Googlebot": frozenset({15169}),
    "Bingbot": frozenset({8075}),
    "Applebot": frozenset({714, 6185}),
}

# Automation globals detected by the tracker -> framework name.
_AUTOMATION_GLOBALS = (
    ("__playwright", "Playwright"), ("__pw", "Playwright"), ("cypress", "Cypress"),
    ("callphantom", "PhantomJS"), ("_phantom", "PhantomJS"), ("__nightmare", "Nightmare"),
    ("domautomation", "Chrome automation"), ("cdc_", "ChromeDriver"), ("selenium", "Selenium"),
    ("webdriver", "Selenium"), ("__fxdriver", "Selenium"), ("__driver_", "Selenium"),
    ("puppeteer", "Puppeteer"),
)

_AI_AGENT_HOSTS = ("chatgpt.com", "openai.com", "anthropic.com", "claude.ai", "perplexity.ai")


class _Evidence:
    """Accumulates signals while classifying one hit."""

    def __init__(self) -> None:
        self.strong: List[Tuple[float, str, Optional[str]]] = []
        self.weak = 0.0
        self.reasons: List[str] = []

    def add_strong(self, weight: float, kind: str, name: Optional[str], reason: str) -> None:
        self.strong.append((weight, kind, name))
        self.reasons.append(reason)

    def add_weak(self, weight: float, reason: str) -> None:
        self.weak += weight
        self.reasons.append(reason)

    def note(self, reason: str) -> None:
        self.reasons.append(reason)

    def result(self, hosting: bool) -> Dict[str, Any]:
        top = max(self.strong, key=lambda signal: signal[0]) if self.strong else None
        score = round(min(1.0, (top[0] if top else 0.0) + self.weak), 2)
        if top:
            kind, name = top[1], top[2]
        else:
            kind = SUSPICIOUS if score >= SUSPICIOUS_THRESHOLD else None
            name = None
        return {
            "is_bot": score >= BOT_THRESHOLD,
            "bot_kind": kind,
            "bot_name": name,
            "score": score,
            "reasons": self.reasons,
            "is_hosting": hosting,
        }


def _lower_headers(headers: Any) -> Optional[Dict[str, str]]:
    """Header map with lowercase names and string values (None if absent)."""
    if not isinstance(headers, dict):
        return None
    result: Dict[str, str] = {}
    for key, value in headers.items():
        if key is None:
            continue
        if isinstance(value, (list, tuple)):
            value = ", ".join(str(item) for item in value)
        result[str(key).strip().lower()] = "" if value is None else str(value)
    return result


def _as_count(value: Any) -> Optional[int]:
    """A non-negative integer count, or None for missing/invalid values."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value) if value >= 0 else None


def _is_desktop_chromium(ua: str) -> bool:
    """Desktop Chromium UA (where window.chrome and PDF plugins always exist)."""
    return bool(_CHROME_RE.search(ua)) and not _NOT_DESKTOP_RE.search(ua)


def _expects_sec_fetch(ua: str) -> bool:
    """True when a genuine browser with this UA always sends Sec-Fetch-* headers."""
    if _IOS_RE.search(ua):
        return False
    chrome = _CHROME_RE.search(ua)
    if chrome:
        return int(chrome.group(1)) >= 80
    firefox = _FIREFOX_RE.search(ua)
    if firefox:
        return int(firefox.group(1)) >= 90
    safari = _SAFARI_RE.search(ua)
    return bool(safari) and int(safari.group(1)) >= 17


def _automation_name(names: List[str]) -> Optional[str]:
    """Framework name for a list of detected automation globals."""
    for name in names:
        low = name.lower()
        for marker, framework in _AUTOMATION_GLOBALS:
            if marker in low:
                return framework
    return None


def _agent_host(value: str) -> str:
    """Host named by a Signature-Agent header (Web Bot Auth)."""
    match = _URL_HOST_RE.search(value)
    if match:
        return match.group(1).lower()
    return value.strip().strip("\"'").lower()[:100]


def _ua_evidence(
    ev: _Evidence,
    ua: str,
    asn: Any,
    extra_patterns: Optional[List[Tuple[Pattern, str]]],
) -> Optional[Tuple[str, str]]:
    """User-Agent patterns: built-in table, external list, generic fallbacks."""
    if not ua:
        ev.add_strong(_W_EMPTY_UA, TOOL, None, "empty_ua")
        return None
    match = _match_specific(ua)
    reason = "ua"
    if match is None:
        match = _match_extra(ua, extra_patterns)
        reason = "ua_list"
    if match is None:
        match = _match_generic(ua)
        reason = "ua_generic"
    if match is None:
        return None
    name, kind = match
    ev.add_strong(_W_KNOWN_UA, kind, name, "%s:%s" % (reason, name))
    allowed = _VERIFIED_ASNS.get(name)
    number = _as_int(asn)
    if allowed is not None and number is not None and number not in allowed:
        ev.note("unverified_asn:%s" % name)
    return match


def _header_evidence(
    ev: _Evidence, ua: str, ua_bot: Optional[Tuple[str, str]], headers: Optional[Dict[str, str]]
) -> None:
    """Request-header signals (skipped when headers are unknown)."""
    if headers is None:
        return
    agent = headers.get("signature-agent")
    if agent:
        host = _agent_host(agent)
        kind = AI if any(host.endswith(known) for known in _AI_AGENT_HOSTS) else AUTOMATION
        ev.add_strong(_W_SIGNATURE_AGENT, kind, host or None, "signature_agent:%s" % host)
    sender = headers.get("from", "")
    if "@" in sender or "(at)" in sender.lower():
        ev.add_strong(_W_FROM_HEADER, CRAWLER, None, "from_header")
    if ua_bot is not None or not _BROWSER_UA_RE.match(ua):
        return
    if not headers.get("accept-language", "").strip():
        ev.add_weak(_W_NO_ACCEPT_LANGUAGE, "no_accept_language")
    if _expects_sec_fetch(ua) and not any(key.startswith("sec-fetch-") for key in headers):
        ev.add_weak(_W_NO_SEC_FETCH, "no_sec_fetch")


def _client_evidence(ev: _Evidence, ua: str, signals: Dict[str, Any]) -> None:
    """Signals collected by the tracker in the browser."""
    if signals.get("webdriver") is True:
        ev.add_strong(_W_WEBDRIVER, AUTOMATION, None, "webdriver")
    found = signals.get("automation")
    if isinstance(found, (list, tuple)):
        names = [str(item) for item in found if item][:10]
        if names:
            ev.add_strong(_W_AUTOMATION_GLOBALS, AUTOMATION, _automation_name(names),
                          "automation:%s" % ",".join(names))
    if signals.get("headless_ua") is True:
        ev.add_strong(_W_HEADLESS_HINT, HEADLESS, "HeadlessChrome", "headless_ua")
    if signals.get("outer_zero") is True:
        ev.add_weak(_W_OUTER_ZERO, "outer_zero")
    if _as_count(signals.get("languages")) == 0:
        ev.add_weak(_W_NO_LANGUAGES, "no_languages")
    # Only desktop Chromium is checked: WebViews and some mobile builds
    # legitimately lack window.chrome and plugins.
    if _is_desktop_chromium(ua):
        if signals.get("chrome_obj") is False:
            ev.add_weak(_W_NO_WINDOW_CHROME, "no_window_chrome")
        if _as_count(signals.get("plugins")) == 0:
            ev.add_weak(_W_NO_PLUGINS, "no_plugins")


def _network_evidence(
    ev: _Evidence,
    ua: str,
    hosting: bool,
    asn: Any,
    has_js: Optional[bool],
    ua_bot: Optional[Tuple[str, str]],
) -> None:
    """Hosting/VPN networks: a no-JS hit from one is a datacenter bot."""
    if not hosting:
        return
    number = _as_int(asn)
    label = "hosting_asn:%d" % number if number is not None and number in HOSTING_ASNS else "hosting_org"
    if _MAIL_PROXY_RE.search(ua):
        ev.note(label)
        ev.note("mail_proxy")
    elif ua_bot is not None:
        ev.note(label)
    elif has_js is False:
        ev.note(label)
        ev.add_strong(_W_DATACENTER_NO_JS, DATACENTER, None, "no_js")
    else:
        ev.add_weak(_W_HOSTING, label)


def classify(
    user_agent: Optional[str],
    headers: Optional[Dict[str, str]] = None,
    client: Optional[Dict[str, Any]] = None,
    asn: Optional[int] = None,
    as_org: Optional[str] = None,
    has_js: Optional[bool] = None,
    extra_patterns: Optional[List[Tuple[Pattern, str]]] = None,
) -> Dict[str, Any]:
    """Classify one hit as bot or human.

    Returns ``{"is_bot", "bot_kind", "bot_name", "score", "reasons",
    "is_hosting"}``. ``score`` is in [0, 1] and ``is_bot`` is
    ``score >= 0.7``. ``bot_kind`` is set for every strong signal and is
    "suspicious" when only weak signals reach 0.5; ``reasons`` lists every
    contributing signal. Missing inputs simply contribute nothing.
    """
    ev = _Evidence()
    ua = _clean_ua(user_agent)
    hosting = is_hosting(asn, as_org)
    signals = client if isinstance(client, dict) else {}
    ua_bot = _ua_evidence(ev, ua, asn, extra_patterns)
    _header_evidence(ev, ua, ua_bot, _lower_headers(headers))
    _client_evidence(ev, ua, signals)
    _network_evidence(ev, ua, hosting, asn, has_js, ua_bot)
    return ev.result(hosting)
