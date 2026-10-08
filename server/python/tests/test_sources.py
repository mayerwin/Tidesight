"""Tests for tidesight.enrich.sources: campaign tags, referrers and GA4 channels."""

from __future__ import annotations

import itertools
import json

import pytest

from tidesight.enrich import sources
from tidesight.enrich.sources import (
    CATEGORIES,
    CHANNELS,
    CLICK_IDS,
    REF_PARAMS,
    SOURCES_VERSION,
    channel_group,
    classify,
    extract_search_query,
    parse_campaign,
    referrer_info,
    registrable_domain,
    source_category,
)

PAGE = "https://www.example.com/landing"

CLASSIFY_KEYS = {
    "source", "medium", "campaign", "term", "content", "campaign_id", "source_platform",
    "click_id_type", "click_id", "referrer_host", "referrer_domain", "source_category",
    "is_internal", "channel",
}
CAMPAIGN_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "utm_source_platform", "utm_creative_format", "utm_marketing_tactic", "source_param",
    "click_id_type", "click_id", "extra",
}

# Inputs no function may choke on.
WEIRD = [
    None, "", "   ", 0, 42, 3.5, b"https://www.google.com/", object(), ["https://x.com"],
    {"url": 1}, "%", "%%%", "%ZZ", "?&=&==&", "?=x&utm_source=%ZZ%", "#", "#?", "?#?#",
    "http://", "https://", "http://[::1", "http://[::1]:99999/", "//", "///",
    "javascript:alert(1)", "about:blank", "data:text/html,<b>hi</b>", "file:///etc/passwd",
    "chrome-extension://abc/x.html", "android-app://", "android-app:///", "ios-app://",
    "android-app://com.example/https/", "\x00\x01\x02", "https://exa mple.com/",
    "https://例え.jp/?q=日本", "http://user:pw@host.example.com:99999/?utm_source=x",
    "https://a..b/", "https://-.-/", "x" * 20000,
    "?" + "&".join("utm_x%d=v" % i for i in range(500)),
]


# ---------------------------------------------------------------------------
# channel_group: GA4 default channel group rules
# ---------------------------------------------------------------------------

CHANNEL_CASES = [
    # source, medium, campaign, source_category, expected
    # Direct
    ("(direct)", "(none)", None, None, "Direct"),
    ("(direct)", "(not set)", None, None, "Direct"),
    ("(Direct)", None, None, None, "Direct"),
    ("(direct)", "referral", None, None, "Referral"),
    ("(direct)", "(none)", "cross-network", None, "Direct"),
    # Cross-network
    ("google", "cpc", "PMax Cross-Network Q3", None, "Cross-network"),
    ("newsletter", "email", "cross-network-test", None, "Cross-network"),
    ("google", "cpc", "crossnetwork", None, "Paid Search"),
    # Paid Shopping
    ("amazon", "cpc", None, None, "Paid Shopping"),
    ("google", "cpc", "Shopping - Brand", None, "Paid Shopping"),
    ("facebook", "paid_social", "summer shop", None, "Paid Shopping"),
    ("newsletter", "ppc", "eshop-launch", None, "Paid Shopping"),
    ("acme", "cpc", "shop", None, "Paid Shopping"),
    ("acme", "cpc", "2shop", None, "Paid Shopping"),
    ("acme", "cpc", None, "shopping", "Paid Shopping"),
    ("acme", "cpc", None, "SOURCE_CATEGORY_SHOPPING", "Paid Shopping"),
    # Paid Search
    ("google", "cpc", None, None, "Paid Search"),
    ("bing", "PPC", None, None, "Paid Search"),
    ("duckduckgo.com", "paidsearch", None, None, "Paid Search"),
    ("yahoo", "retargeting", None, None, "Paid Search"),
    ("google", "cpc", "workshop-signups", None, "Paid Search"),
    ("google", "cpc", "Photoshop tutorials", None, "Paid Search"),
    ("google", "cpc", "bishop", None, "Paid Search"),
    ("acme", "cpc", None, "search", "Paid Search"),
    # Paid Social
    ("facebook", "cpc", None, None, "Paid Social"),
    ("m.facebook.com", "paid", None, None, "Paid Social"),
    ("meta", "paid_social", None, None, "Paid Social"),
    ("linkedin", "cpm", None, None, "Paid Social"),
    ("fb", "e-cpc", None, None, "Paid Social"),
    ("acme", "cpc", None, "SOURCE_CATEGORY_SOCIAL", "Paid Social"),
    # Paid Video
    ("youtube", "cpv", None, None, "Paid Video"),
    ("vimeo.com", "paid-video", None, None, "Paid Video"),
    ("acme", "cpc", None, "video", "Paid Video"),
    # Display
    ("dv360", "cpm", None, None, "Display"),
    ("newsletter", "banner", None, None, "Display"),
    ("acme", "Interstitial", None, None, "Display"),
    ("acme", "expandable", None, None, "Display"),
    ("x", "display", None, None, "Display"),
    ("spotify", "cpm", None, None, "Display"),
    # Paid Other
    ("taboola", "cpc", None, None, "Paid Other"),
    ("acme", "paid", None, None, "Paid Other"),
    ("acme", "socpost", None, None, "Paid Other"),
    ("newsletter", "cpa", "workshop", None, "Paid Other"),
    ("google", "cpc", None, "email", "Paid Other"),
    # Organic Shopping
    ("amazon.com", "referral", None, None, "Organic Shopping"),
    ("IGShopping", "social", None, None, "Organic Shopping"),
    ("newsletter", "email", "shop-the-look", None, "Organic Shopping"),
    ("facebook.com", "referral", "Workshopping ideas", None, "Organic Shopping"),
    ("Google Shopping", "(not set)", None, None, "Organic Shopping"),
    # Organic Social
    ("facebook.com", "referral", None, None, "Organic Social"),
    ("newsletter", "social-media", None, None, "Organic Social"),
    ("acme", "SM", None, None, "Organic Social"),
    ("acme", "social network", None, None, "Organic Social"),
    ("t.co", "(not set)", None, None, "Organic Social"),
    ("facebook", "retargeting_fb", None, None, "Organic Social"),
    ("acme", "referral", None, "SOURCE_CATEGORY_SOCIAL", "Organic Social"),
    # Organic Video
    ("youtube.com", "referral", None, None, "Organic Video"),
    ("acme", "video", None, None, "Organic Video"),
    ("acme", "organic_video", None, None, "Organic Video"),
    # Organic Search
    ("google", "organic", None, None, "Organic Search"),
    ("bing", "referral", None, None, "Organic Search"),
    ("acme", "organic", None, None, "Organic Search"),
    ("ntp.msn.com", "referral", None, None, "Organic Search"),
    ("bing", "ppc-brand", None, None, "Organic Search"),
    # AI Assistant
    ("chatgpt.com", "ai-assistant", "(ai-assistant)", None, "AI Assistant"),
    ("acme", "AI-Assistant", None, None, "AI Assistant"),
    ("chatgpt.com", "referral", None, None, "Referral"),
    ("chatgpt.com", "referral", None, "ai", "Referral"),
    # Referral
    ("example.org", "referral", None, None, "Referral"),
    ("acme", "app", None, None, "Referral"),
    ("acme", "link", None, None, "Referral"),
    ("mail.google.com", "referral", None, None, "Referral"),
    ("email", "referral", None, None, "Referral"),
    # Email
    ("newsletter", "email", None, None, "Email"),
    ("email", "(not set)", None, None, "Email"),
    ("acme", "E-Mail", None, None, "Email"),
    ("acme", "e_mail", None, None, "Email"),
    ("acme", "e mail", None, None, "Email"),
    ("acme", "emails", None, None, "Unassigned"),
    ("acme", "newsletter", None, None, "Unassigned"),
    # Affiliates
    ("awin", "affiliate", None, None, "Affiliates"),
    ("acme", "Affiliate", None, None, "Affiliates"),
    ("acme", "affiliates", None, None, "Unassigned"),
    # Audio
    ("spotify", "audio", None, None, "Audio"),
    ("acme", "AUDIO", None, None, "Audio"),
    # SMS
    ("sms", "(not set)", None, None, "SMS"),
    ("acme", "sms", None, None, "SMS"),
    ("acme", "SMS", None, None, "SMS"),
    # Mobile Push Notifications
    ("acme", "web-push", None, None, "Mobile Push Notifications"),
    ("acme", "push", None, None, "Mobile Push Notifications"),
    ("acme", "mobile_app", None, None, "Mobile Push Notifications"),
    ("acme", "notifications", None, None, "Mobile Push Notifications"),
    ("firebase", "(not set)", None, None, "Mobile Push Notifications"),
    ("acme", "pushed", None, None, "Unassigned"),
    # Unassigned
    ("acme", "(not set)", None, None, "Unassigned"),
    (None, None, None, None, "Unassigned"),
    ("(not set)", "(not set)", None, None, "Unassigned"),
    ("acme", "unpaid", None, None, "Unassigned"),
    ("acme", "prepaid", None, None, "Unassigned"),
    ("chatgpt.com", "(not set)", None, None, "Unassigned"),
]


@pytest.mark.parametrize("source,medium,campaign,category,expected", CHANNEL_CASES)
def test_channel_group(source, medium, campaign, category, expected):
    assert channel_group(source, medium, campaign, category) == expected


def test_channel_group_cases_cover_every_channel():
    covered = {case[-1] for case in CHANNEL_CASES}
    assert covered == set(CHANNELS)
    for channel in CHANNELS:
        assert sum(1 for case in CHANNEL_CASES if case[-1] == channel) >= 2, channel


def test_channel_group_keyword_arguments_and_empty_category():
    assert channel_group(source="acme", medium="cpc", source_category="search") == "Paid Search"
    assert channel_group("facebook", "referral", None, "") == "Organic Social"
    assert channel_group("facebook", "referral", source_category=None) == "Organic Social"


def test_channels_constant():
    assert len(CHANNELS) == 20 and len(set(CHANNELS)) == 20
    assert CHANNELS[0] == "Direct" and CHANNELS[-1] == "Unassigned"
    assert CHANNELS.index("Organic Search") < CHANNELS.index("AI Assistant") < CHANNELS.index(
        "Referral"
    )


@pytest.mark.parametrize("value", WEIRD)
def test_channel_group_always_returns_a_channel(value):
    assert channel_group(value, value, value, value) in CHANNELS
    assert channel_group("google", value, value) in CHANNELS
    assert channel_group(value, "cpc") in CHANNELS


# ---------------------------------------------------------------------------
# source_category
# ---------------------------------------------------------------------------

SOURCE_CATEGORY_CASES = [
    # Search
    ("google", "search"),
    ("Google", "search"),
    ("www.google.de", "search"),
    ("google.com.br", "search"),
    ("https://www.google.co.jp/search?q=x", "search"),
    ("google-play", "search"),
    ("news.google.com", "search"),
    ("bing", "search"),
    ("duckduckgo.com", "search"),
    ("search.brave.com", "search"),
    ("kagi.com", "search"),
    ("yandex.kz", "search"),
    ("ya.ru", "search"),
    ("uk.search.yahoo.com", "search"),
    ("ntp.msn.com", "search"),
    # Social
    ("facebook", "social"),
    ("fb", "social"),
    ("ig", "social"),
    ("meta", "social"),
    ("x", "social"),
    ("l.facebook.com", "social"),
    ("threads.com", "social"),
    ("bsky.app", "social"),
    ("mastodon.social", "social"),
    ("news.ycombinator.com", "social"),
    ("pinterest.co.uk", "social"),
    ("discord.gg", "social"),
    ("com.facebook.katana", "social"),
    # Video
    ("youtube", "video"),
    ("youtu.be", "video"),
    ("m.youtube.com", "video"),
    ("twitch.tv", "video"),
    # Shopping
    ("amazon.de", "shopping"),
    ("IGShopping", "shopping"),
    ("Google Shopping", "shopping"),
    ("ebay.com.au", "shopping"),
    # Email
    ("mail.google.com", "email"),
    ("gmail", "email"),
    ("outlook.live.com", "email"),
    ("mail.yandex.ru", "email"),
    ("webmail.example.net", "email"),
    ("newsletter", "email"),
    ("email", "email"),
    ("com.google.android.gm", "email"),
    # AI assistants
    ("chatgpt.com", "ai"),
    ("chatgpt", "ai"),
    ("chat.openai.com", "ai"),
    ("perplexity.ai", "ai"),
    ("gemini.google.com", "ai"),
    ("claude.ai", "ai"),
    ("copilot.microsoft.com", "ai"),
    ("edgeservices.bing.com", "ai"),
    ("yiyan.baidu.com", "ai"),
    # Unknown or uncategorised
    ("docs.google.com", None),
    ("accounts.google.com", None),
    ("example.com", None),
    ("newsletter2", None),
    ("aws.amazon.com", None),
    ("news.yahoo.com", None),
    ("user.blogspot.com", None),
    ("mail.ru", None),
    ("e mail", None),
    ("(direct)", None),
    ("(not set)", None),
    ("", None),
    (None, None),
    (42, None),
]


@pytest.mark.parametrize("value,expected", SOURCE_CATEGORY_CASES)
def test_source_category(value, expected):
    assert source_category(value) == expected


@pytest.mark.parametrize(
    "word",
    ["target", "signal", "teams", "kick", "otto", "allegro", "monica", "bard", "yep", "petal",
     "centrum", "consensus", "elicit", "blackbox", "manus", "ernie", "poe", "superhuman"],
)
def test_generic_words_are_not_source_names(word):
    # Site names double as utm_source names, and GA4 checks source lists before
    # Email: an everyday word must not turn utm_source=<word>&utm_medium=email
    # into a social, shopping or search visit.
    assert source_category(word) is None
    result = classify("%s?utm_source=%s&utm_medium=email" % (PAGE, word), None)
    assert result["channel"] == "Email"


def test_categories_constant_covers_every_site():
    assert CATEGORIES == ("search", "social", "video", "shopping", "email", "ai")
    for site in sources._SITES_BY_NAME.values():
        assert site.category in CATEGORIES or site.category is None, site


# ---------------------------------------------------------------------------
# referrer_info
# ---------------------------------------------------------------------------

REFERRER_CASES = [
    # referrer, host, domain, name, category
    ("https://www.google.co.uk/", "www.google.co.uk", "google.co.uk", "google", "search"),
    ("https://www.google.com.au/search?q=x", "www.google.com.au", "google.com.au", "google",
     "search"),
    ("https://google.de", "google.de", "google.de", "google", "search"),
    ("https://images.google.fr/", "images.google.fr", "google.fr", "google", "search"),
    ("https://news.google.com/", "news.google.com", "google.com", "google", "search"),
    ("https://play.google.com/store", "play.google.com", "google.com", "google-play", "search"),
    ("https://docs.google.com/document/d/1", "docs.google.com", "google.com", None, None),
    ("https://mail.google.com/mail/u/0/", "mail.google.com", "google.com", "gmail", "email"),
    ("https://gemini.google.com/app", "gemini.google.com", "google.com", "gemini", "ai"),
    ("https://www.bing.com/", "www.bing.com", "bing.com", "bing", "search"),
    ("https://cn.bing.com/", "cn.bing.com", "bing.com", "bing", "search"),
    ("https://html.duckduckgo.com/html/", "html.duckduckgo.com", "duckduckgo.com", "duckduckgo",
     "search"),
    ("https://yandex.com.tr/", "yandex.com.tr", "yandex.com.tr", "yandex", "search"),
    ("https://ya.ru/", "ya.ru", "ya.ru", "yandex", "search"),
    ("https://uk.search.yahoo.com/", "uk.search.yahoo.com", "yahoo.com", "yahoo", "search"),
    ("https://search.yahoo.co.jp/", "search.yahoo.co.jp", "yahoo.co.jp", "yahoo", "search"),
    ("https://news.yahoo.com/", "news.yahoo.com", "yahoo.com", None, None),
    ("https://www.baidu.com/link?url=x", "www.baidu.com", "baidu.com", "baidu", "search"),
    ("https://search.naver.com/", "search.naver.com", "naver.com", "naver", "search"),
    ("https://blog.naver.com/x", "blog.naver.com", "naver.com", "blog.naver.com", "social"),
    ("https://search.brave.com/", "search.brave.com", "brave.com", "brave", "search"),
    ("https://www.ecosia.org/", "www.ecosia.org", "ecosia.org", "ecosia", "search"),
    ("https://www.startpage.com/", "www.startpage.com", "startpage.com", "startpage", "search"),
    ("https://kagi.com/", "kagi.com", "kagi.com", "kagi", "search"),
    ("https://www.seznam.cz/", "www.seznam.cz", "seznam.cz", "seznam", "search"),
    ("https://l.facebook.com/l.php?u=x", "l.facebook.com", "facebook.com", "facebook", "social"),
    ("https://lm.facebook.com/", "lm.facebook.com", "facebook.com", "facebook", "social"),
    ("http://m.facebook.com/", "m.facebook.com", "facebook.com", "facebook", "social"),
    ("https://t.co/abc", "t.co", "t.co", "twitter", "social"),
    ("https://lnkd.in/abc", "lnkd.in", "lnkd.in", "linkedin", "social"),
    ("https://out.reddit.com/t3_x", "out.reddit.com", "reddit.com", "reddit", "social"),
    ("https://youtu.be/xyz", "youtu.be", "youtu.be", "youtube", "video"),
    ("https://www.youtube.com/redirect?q=x", "www.youtube.com", "youtube.com", "youtube", "video"),
    ("https://news.ycombinator.com/", "news.ycombinator.com", "ycombinator.com", "hackernews",
     "social"),
    ("https://chatgpt.com/", "chatgpt.com", "chatgpt.com", "chatgpt", "ai"),
    ("https://edgeservices.bing.com/edgesvc/chat", "edgeservices.bing.com", "bing.com", "copilot",
     "ai"),
    ("https://chat.deepseek.com/", "chat.deepseek.com", "deepseek.com", "deepseek", "ai"),
    ("https://outlook.live.com/mail/0/", "outlook.live.com", "live.com", "outlook", "email"),
    ("https://mail.yahoo.co.jp/", "mail.yahoo.co.jp", "yahoo.co.jp", "yahoo-mail", "email"),
    ("https://deref-gmx.net/mail/client/x", "deref-gmx.net", "deref-gmx.net", "gmx", "email"),
    ("https://webmail.example.net/", "webmail.example.net", "example.net", None, "email"),
    ("https://www.amazon.co.uk/dp/1", "www.amazon.co.uk", "amazon.co.uk", "amazon", "shopping"),
    ("https://aws.amazon.com/", "aws.amazon.com", "amazon.com", "aws", None),
    ("https://alice.github.io/post", "alice.github.io", "alice.github.io", None, None),
    ("https://myblog.blogspot.com/", "myblog.blogspot.com", "myblog.blogspot.com", None, None),
    ("https://blogspot.com/", "blogspot.com", "blogspot.com", "blogspot.com", "social"),
    ("https://example.com:8443/path", "example.com", "example.com", None, None),
    ("HTTPS://WWW.GOOGLE.COM/", "www.google.com", "google.com", "google", "search"),
    ("https://www.google.com./", "www.google.com", "google.com", "google", "search"),
    ("//www.google.com/", "www.google.com", "google.com", "google", "search"),
    ("www.google.com/search", "www.google.com", "google.com", "google", "search"),
    ("http://192.168.1.20:8080/admin", "192.168.1.20", "192.168.1.20", None, None),
    ("https://[2001:db8::1]/", "2001:db8::1", "2001:db8::1", None, None),
    # Android and iOS app referrers
    ("android-app://com.google.android.gm/", "com.google.android.gm", "com.google.android.gm",
     "gmail", "email"),
    ("android-app://com.google.android.googlequicksearchbox/https/www.google.com",
     "com.google.android.googlequicksearchbox", "com.google.android.googlequicksearchbox",
     "google", "search"),
    ("android-app://com.linkedin.android/", "com.linkedin.android", "com.linkedin.android",
     "linkedin", "social"),
    ("android-app://com.slack/", "com.slack", "com.slack", "slack", "social"),
    ("android-app://org.telegram.messenger/", "org.telegram.messenger", "org.telegram.messenger",
     "telegram", "social"),
    ("android-app://com.Facebook.Katana", "com.facebook.katana", "com.facebook.katana",
     "facebook", "social"),
    ("android-app://com.openai.chatgpt/", "com.openai.chatgpt", "com.openai.chatgpt", "chatgpt",
     "ai"),
    ("android-app://com.example.unknown/", "com.example.unknown", "com.example.unknown", None,
     None),
    ("android-app://com.example.browser/https/duckduckgo.com/", "com.example.browser",
     "com.example.browser", "duckduckgo", "search"),
    ("ios-app://1234567/https/www.bing.com/", "1234567", "1234567", "bing", "search"),
]


@pytest.mark.parametrize("referrer,host,domain,name,category", REFERRER_CASES)
def test_referrer_info(referrer, host, domain, name, category):
    assert referrer_info(referrer) == {
        "referrer_host": host,
        "referrer_domain": domain,
        "name": name,
        "category": category,
    }


@pytest.mark.parametrize(
    "referrer",
    [
        None, "", "   ", "about:blank", "javascript:void(0)", "chrome-extension://abc/popup.html",
        "file:///C:/page.html", "data:text/html,hi", "http://", "https://", "not a url",
        "android-app://", "http://[::1", 42, b"https://www.google.com/",
    ],
)
def test_referrer_info_without_usable_host(referrer):
    assert referrer_info(referrer) == {
        "referrer_host": None, "referrer_domain": None, "name": None, "category": None,
    }


@pytest.mark.parametrize(
    "tld", ["com", "co.uk", "de", "fr", "com.au", "co.jp", "co.in", "com.br", "com.mx", "co.za",
            "com.tr", "ca", "es", "it", "nl", "pl", "ru", "com.sg", "co.kr", "com.ar", "ch"],
)
def test_google_country_domains_are_google_search(tld):
    info = referrer_info("https://www.google.%s/" % tld)
    assert (info["name"], info["category"], info["referrer_domain"]) == (
        "google", "search", "google." + tld,
    )


# ---------------------------------------------------------------------------
# parse_campaign
# ---------------------------------------------------------------------------


def test_parse_campaign_reads_every_utm_field():
    url = (
        "https://www.example.com/?utm_source=Google&utm_medium=CPC&utm_campaign=Spring%20Sale"
        "&utm_term=running+shoes&utm_content=ad-1&utm_id=123"
        "&utm_source_platform=Search%20Ads%20360&utm_creative_format=video"
        "&utm_marketing_tactic=prospecting"
    )
    result = parse_campaign(url)
    assert set(result) == CAMPAIGN_KEYS
    assert result["utm_source"] == "Google"  # values keep their case here
    assert result["utm_medium"] == "CPC"
    assert result["utm_campaign"] == "Spring Sale"
    assert result["utm_term"] == "running shoes"
    assert result["utm_content"] == "ad-1"
    assert result["utm_id"] == "123"
    assert result["utm_source_platform"] == "Search Ads 360"
    assert result["utm_creative_format"] == "video"
    assert result["utm_marketing_tactic"] == "prospecting"
    assert result["source_param"] == "utm_source"
    assert result["click_id_type"] is None and result["click_id"] is None
    assert result["extra"] == {}


@pytest.mark.parametrize(
    "url,field,expected",
    [
        ("/?UTM_SOURCE=News&Utm_Medium=Email", "utm_source", "News"),
        ("/?UTM_SOURCE=News&Utm_Medium=Email", "utm_medium", "Email"),
        ("/?utm_campaign=c%2B%2B+tips", "utm_campaign", "c++ tips"),
        ("/?utm_campaign=caf%C3%A9", "utm_campaign", "café"),
        ("/?utm_campaign=%20%20padded%20%20", "utm_campaign", "padded"),
        ("/?utm_source=a&utm_source=b", "utm_source", "a"),
        ("/?utm_source=&utm_source=%20&utm_source=real", "utm_source", "real"),
        ("/?utm_source=", "utm_source", None),
        ("/?utm_source", "utm_source", None),
        ("/?utm_campaign=100%25", "utm_campaign", "100%"),
        ("/?utm_campaign=bad%ZZescape", "utm_campaign", "bad%ZZescape"),
        ("https://x.com/#/landing?utm_source=fb&utm_medium=social", "utm_source", "fb"),
        ("https://x.com/#/landing?utm_source=fb&utm_medium=social", "utm_medium", "social"),
        ("https://x.com/#utm_campaign=hash-only", "utm_campaign", "hash-only"),
        ("https://x.com/#!/page?utm_term=bang", "utm_term", "bang"),
        ("https://x.com/?utm_source=query#utm_source=fragment", "utm_source", "query"),
        ("https://x.com/?utm_source=q#utm_medium=frag", "utm_medium", "frag"),
        ("http://[::1?utm_source=still-parsed", "utm_source", "still-parsed"),
        ("utm_source=no-question-mark", "utm_source", None),
    ],
)
def test_parse_campaign_names_values_and_fragments(url, field, expected):
    assert parse_campaign(url)[field] == expected


def test_parse_campaign_caps_long_values():
    result = parse_campaign("/?utm_campaign=" + "a" * 2000)
    assert result["utm_campaign"] == "a" * 500


@pytest.mark.parametrize(
    "url,expected",
    [
        (
            "/?mtm_source=Newsletter&mtm_medium=email&mtm_campaign=Spring&mtm_kwd=boots"
            "&mtm_content=hero&mtm_cid=9",
            {"utm_source": "Newsletter", "utm_medium": "email", "utm_campaign": "Spring",
             "utm_term": "boots", "utm_content": "hero", "utm_id": "9",
             "source_param": "mtm_source"},
        ),
        (
            "/?pk_campaign=Spring&pk_kwd=boots&pk_source=old&pk_medium=banner",
            {"utm_source": "old", "utm_medium": "banner", "utm_campaign": "Spring",
             "utm_term": "boots", "source_param": "pk_source"},
        ),
        (
            "/?matomo_campaign=M&piwik_keyword=K&pk_cpn=ignored",
            {"utm_campaign": "M", "utm_term": "K"},
        ),
        (
            "/?mtm_source=matomo&utm_source=utm&mtm_campaign=only-matomo",
            {"utm_source": "utm", "source_param": "utm_source", "utm_campaign": "only-matomo"},
        ),
        ("https://x.com/#pk_campaign=in-hash", {"utm_campaign": "in-hash"}),
    ],
)
def test_parse_campaign_matomo_and_piwik_fallbacks(url, expected):
    result = parse_campaign(url)
    for field, value in expected.items():
        assert result[field] == value, field


def test_parse_campaign_reports_matomo_parameters_in_extra():
    result = parse_campaign("/?mtm_campaign=Spring&mtm_group=g1&mtm_placement=sidebar")
    assert result["extra"] == {"mtm_campaign": "Spring", "mtm_group": "g1",
                               "mtm_placement": "sidebar"}


@pytest.mark.parametrize("click_id", CLICK_IDS)
def test_parse_campaign_finds_every_click_id(click_id):
    result = parse_campaign("https://x.com/?a=1&%s=Val-UE_123&b=2" % click_id)
    assert result["click_id_type"] == click_id
    assert result["click_id"] == "Val-UE_123"  # the value is never lower-cased


@pytest.mark.parametrize(
    "url,click_type,click_value",
    [
        ("/?GCLID=AbC", "gclid", "AbC"),
        ("/?ScCid=snap", "sccid", "snap"),
        ("/?fbclid=f&gclid=g", "gclid", "g"),
        ("/?srsltid=s&msclkid=m", "msclkid", "m"),
        ("/?gclid=&fbclid=f", "fbclid", "f"),
        ("/?mc_cid=c&mc_eid=e", "mc_eid", "e"),
        ("https://x.com/#gclid=in-fragment", None, None),
        ("/?page=2", None, None),
    ],
)
def test_parse_campaign_click_id_precedence(url, click_type, click_value):
    result = parse_campaign(url)
    assert (result["click_id_type"], result["click_id"]) == (click_type, click_value)


def test_parse_campaign_extra_parameters():
    url = (
        "/?gclid=g&fbclid=f&gad_source=1&gad_campaignid=777&gclsrc=aw.ds&utm_adgroup=a1"
        "&hsa_net=adwords&ref=producthunt&via=friend&source=homepage&page=2&q=shoes&mc_cid=c"
    )
    assert parse_campaign(url)["extra"] == {
        "fbclid": "f",
        "gad_source": "1",
        "gad_campaignid": "777",
        "gclsrc": "aw.ds",
        "utm_adgroup": "a1",
        "hsa_net": "adwords",
        "ref": "producthunt",
        "via": "friend",
        "source": "homepage",
        "mc_cid": "c",
    }


def test_parse_campaign_bounds_extra():
    url = "/?" + "&".join("utm_custom%d=v" % i for i in range(300))
    assert len(parse_campaign(url)["extra"]) == 50


def test_ref_params_are_off_by_default():
    result = parse_campaign("/?ref=producthunt")
    assert result["utm_source"] is None and result["source_param"] is None
    assert result["extra"] == {"ref": "producthunt"}


@pytest.mark.parametrize(
    "url,ref_params,source,param",
    [
        ("/?ref=producthunt", REF_PARAMS, "producthunt", "ref"),
        ("/?via=Friend&ref=second", REF_PARAMS, "second", "ref"),
        ("/?source=newsletter", REF_PARAMS, "newsletter", "source"),
        ("/?REF=upper", ["ref"], "upper", "ref"),
        ("/?ref=x", "ref", "x", "ref"),
        ("/?ref=https://www.producthunt.com/posts/x", REF_PARAMS, "producthunt.com", "ref"),
        ("/?utm_source=tagged&ref=ignored", REF_PARAMS, "tagged", "utm_source"),
        ("/?mtm_source=matomo&ref=ignored", REF_PARAMS, "matomo", "mtm_source"),
        ("/?ref=", REF_PARAMS, None, None),
        ("/?ref=x", ["via"], None, None),
        ("/?ref=x", [None, 3, "", "  "], None, None),
    ],
)
def test_ref_params_promote_referral_parameters(url, ref_params, source, param):
    result = parse_campaign(url, ref_params=ref_params)
    assert (result["utm_source"], result["source_param"]) == (source, param)


@pytest.mark.parametrize("value", WEIRD)
def test_parse_campaign_never_raises(value):
    result = parse_campaign(value)
    assert set(result) == CAMPAIGN_KEYS
    assert isinstance(result["extra"], dict)
    parse_campaign(value, ref_params=value)


# ---------------------------------------------------------------------------
# classify: campaign tags
# ---------------------------------------------------------------------------


def test_classify_result_keys_and_types():
    result = classify(PAGE + "?utm_source=x", "https://www.google.com/")
    assert set(result) == CLASSIFY_KEYS
    assert isinstance(result["is_internal"], bool)
    json.dumps(result)


@pytest.mark.parametrize(
    "query,source,medium,campaign,channel",
    [
        ("utm_source=Newsletter&utm_medium=Email&utm_campaign=Spring+Sale", "newsletter",
         "email", "Spring Sale", "Email"),
        ("utm_source=Google&utm_medium=CPC&utm_campaign=Brand", "google", "cpc", "Brand",
         "Paid Search"),
        ("utm_source=facebook&utm_medium=paid_social", "facebook", "paid_social", "(not set)",
         "Paid Social"),
        ("utm_source=facebook", "facebook", "(not set)", "(not set)", "Organic Social"),
        ("utm_medium=cpc&utm_campaign=x", "(not set)", "cpc", "x", "Paid Other"),
        ("utm_campaign=only", "(not set)", "(not set)", "only", "Unassigned"),
        ("utm_id=42", "(not set)", "(not set)", "(not set)", "Unassigned"),
        ("utm_source=bing&utm_medium=cpc&utm_campaign=Shopping+Feed", "bing", "cpc",
         "Shopping Feed", "Paid Shopping"),
        ("utm_source=google&utm_medium=cpc&utm_campaign=PMax+cross-network", "google", "cpc",
         "PMax cross-network", "Cross-network"),
        ("pk_source=Newsletter&pk_medium=email&pk_campaign=Spring", "newsletter", "email",
         "Spring", "Email"),
        ("utm_source=chatgpt.com", "chatgpt.com", "ai-assistant", "(ai-assistant)",
         "AI Assistant"),
        ("utm_source=perplexity&utm_campaign=launch", "perplexity", "ai-assistant", "launch",
         "AI Assistant"),
        ("utm_source=chatgpt.com&utm_medium=referral", "chatgpt.com", "referral", "(not set)",
         "Referral"),
        ("UTM_SOURCE=Twitter&UTM_MEDIUM=Social", "twitter", "social", "(not set)",
         "Organic Social"),
    ],
)
def test_classify_campaign_tags(query, source, medium, campaign, channel):
    result = classify(PAGE + "?" + query, "https://www.google.com/")
    assert (result["source"], result["medium"], result["campaign"], result["channel"]) == (
        source, medium, campaign, channel,
    )


def test_classify_tags_win_over_click_ids_and_referrer():
    result = classify(
        PAGE + "?utm_source=newsletter&utm_medium=email&gclid=Abc", "https://www.bing.com/"
    )
    assert (result["source"], result["medium"], result["channel"]) == (
        "newsletter", "email", "Email",
    )
    assert (result["click_id_type"], result["click_id"]) == ("gclid", "Abc")
    assert result["referrer_host"] == "www.bing.com"


def test_classify_tags_in_hash_fragment():
    result = classify("https://app.example.com/#/promo?utm_source=fb&utm_medium=paid", None)
    assert (result["source"], result["medium"], result["channel"]) == ("fb", "paid", "Paid Social")


def test_classify_passes_through_campaign_details():
    result = classify(
        PAGE + "?utm_source=g&utm_medium=cpc&utm_term=Boots&utm_content=Ad+1&utm_id=99"
        "&utm_source_platform=SA360",
        None,
    )
    assert (result["term"], result["content"], result["campaign_id"],
            result["source_platform"]) == ("Boots", "Ad 1", "99", "SA360")


def test_classify_content_alone_is_not_a_campaign():
    result = classify(PAGE + "?utm_content=hero-banner", "https://www.google.com/")
    assert (result["source"], result["medium"]) == ("google", "organic")
    assert result["content"] == "hero-banner"


def test_classify_campaign_id_falls_back_to_gad_campaignid():
    assert classify(PAGE + "?gad_campaignid=777", None)["campaign_id"] == "777"
    assert classify(PAGE + "?utm_id=1&gad_campaignid=777", None)["campaign_id"] == "1"


def test_classify_ref_params():
    url = PAGE + "?ref=producthunt"
    off = classify(url, None)
    assert (off["source"], off["channel"]) == ("(direct)", "Direct")
    on = classify(url, None, ref_params=REF_PARAMS)
    assert (on["source"], on["medium"], on["campaign"], on["channel"]) == (
        "producthunt", "referral", "(referral)", "Organic Social",
    )
    tagged = classify(PAGE + "?ref=Acme&utm_medium=email", None, ref_params=REF_PARAMS)
    assert (tagged["source"], tagged["medium"], tagged["channel"]) == ("acme", "email", "Email")


# ---------------------------------------------------------------------------
# classify: click identifiers
# ---------------------------------------------------------------------------

CLICK_CASES = [
    # parameter, source, medium, channel
    ("gclid", "google", "cpc", "Paid Search"),
    ("gbraid", "google", "cpc", "Paid Search"),
    ("wbraid", "google", "cpc", "Paid Search"),
    ("dclid", "dv360", "cpm", "Display"),
    ("msclkid", "bing", "cpc", "Paid Search"),
    ("yclid", "yandex", "cpc", "Paid Search"),
    ("ttclid", "tiktok", "cpc", "Paid Social"),
    ("twclid", "twitter", "cpc", "Paid Social"),
    ("li_fat_id", "linkedin", "cpc", "Paid Social"),
    ("epik", "pinterest", "cpc", "Paid Social"),
    ("ScCid", "snapchat", "cpc", "Paid Social"),
    ("rdt_cid", "reddit", "cpc", "Paid Social"),
    ("ndclid", "nextdoor", "cpc", "Paid Social"),
    ("tblci", "taboola", "cpc", "Paid Other"),
    ("dicbo", "outbrain", "cpc", "Paid Other"),
    ("irclickid", "impact", "affiliate", "Affiliates"),
    ("cjevent", "cj", "affiliate", "Affiliates"),
    ("awc", "awin", "affiliate", "Affiliates"),
    ("sscid", "shareasale", "affiliate", "Affiliates"),
    ("tduid", "tradedoubler", "affiliate", "Affiliates"),
    ("admitad_uid", "admitad", "affiliate", "Affiliates"),
    ("mc_eid", "mailchimp", "email", "Email"),
    ("mc_cid", "mailchimp", "email", "Email"),
    ("_kx", "klaviyo", "email", "Email"),
    ("_hsenc", "hubspot", "email", "Email"),
    ("_hsmi", "hubspot", "email", "Email"),
    ("mkt_tok", "marketo", "email", "Email"),
    ("fbclid", "facebook.com", "referral", "Organic Social"),
    ("srsltid", "google", "organic", "Organic Search"),
    ("ysclid", "yandex", "organic", "Organic Search"),
]


def test_click_cases_cover_every_click_id():
    assert {case[0].lower() for case in CLICK_CASES} == set(CLICK_IDS)


@pytest.mark.parametrize("param,source,medium,channel", CLICK_CASES)
def test_classify_click_ids_without_referrer(param, source, medium, channel):
    result = classify("%s?%s=CkId-42" % (PAGE, param), None)
    assert (result["source"], result["medium"], result["channel"]) == (source, medium, channel)
    assert (result["click_id_type"], result["click_id"]) == (param.lower(), "CkId-42")
    assert result["is_internal"] is False


@pytest.mark.parametrize(
    "query,referrer,source,medium",
    [
        ("gclid=x", "https://www.facebook.com/", "google", "cpc"),
        ("gclid=x", "https://www.google.com/", "google", "cpc"),
        ("msclkid=x", "https://duckduckgo.com/", "bing", "cpc"),
        ("ttclid=x", "https://www.tiktok.com/", "tiktok", "cpc"),
        ("_kx=x", "https://mail.google.com/", "klaviyo", "email"),
        ("yclid=123", "https://search.yahoo.co.jp/", "yahoo", "cpc"),
        ("yclid=123", "https://yandex.ru/", "yandex", "cpc"),
        ("gad_source=1", "https://www.google.com/", "google", "cpc"),
        ("gad_source=1&fbclid=f", None, "google", "cpc"),
        ("gad_campaignid=777", None, "google", "cpc"),
    ],
)
def test_classify_paid_click_ids_override_the_referrer(query, referrer, source, medium):
    result = classify(PAGE + "?" + query, referrer)
    assert (result["source"], result["medium"]) == (source, medium)


@pytest.mark.parametrize(
    "query,referrer,source,medium,channel",
    [
        ("fbclid=x", "https://www.reddit.com/", "reddit.com", "referral", "Organic Social"),
        ("fbclid=x", "https://l.instagram.com/", "instagram.com", "referral", "Organic Social"),
        ("fbclid=x", "https://news.example.org/", "news.example.org", "referral", "Referral"),
        ("srsltid=x", "https://www.bing.com/", "bing", "organic", "Organic Search"),
        ("ysclid=x", "https://www.google.ru/", "google", "organic", "Organic Search"),
    ],
)
def test_classify_organic_click_ids_yield_to_a_referrer(query, referrer, source, medium, channel):
    result = classify(PAGE + "?" + query, referrer)
    assert (result["source"], result["medium"], result["channel"]) == (source, medium, channel)


def test_classify_fbclid_is_never_paid():
    for referrer in (None, "https://m.facebook.com/", "https://l.instagram.com/"):
        result = classify(PAGE + "?fbclid=IwAR0abc", referrer)
        assert result["medium"] == "referral"
        assert result["channel"] == "Organic Social"


def test_classify_click_ids_with_internal_referrer():
    paid = classify(PAGE + "?gclid=x", "https://www.example.com/cart")
    assert (paid["source"], paid["medium"], paid["is_internal"]) == ("google", "cpc", True)
    organic = classify(PAGE + "?fbclid=x", "https://www.example.com/cart")
    assert organic["is_internal"] is True
    assert organic["source"] is None and organic["channel"] is None


# ---------------------------------------------------------------------------
# classify: referrers
# ---------------------------------------------------------------------------

REFERRER_CLASSIFY_CASES = [
    # referrer, source, medium, campaign, channel, source_category
    ("https://www.google.co.uk/", "google", "organic", "(organic)", "Organic Search", "search"),
    ("https://www.google.com.br/search?q=x", "google", "organic", "(organic)", "Organic Search",
     "search"),
    ("https://images.google.de/", "google", "organic", "(organic)", "Organic Search", "search"),
    ("https://www.bing.com/", "bing", "organic", "(organic)", "Organic Search", "search"),
    ("https://duckduckgo.com/", "duckduckgo", "organic", "(organic)", "Organic Search", "search"),
    ("https://yandex.ru/", "yandex", "organic", "(organic)", "Organic Search", "search"),
    ("https://search.yahoo.co.jp/", "yahoo", "organic", "(organic)", "Organic Search", "search"),
    ("https://www.baidu.com/", "baidu", "organic", "(organic)", "Organic Search", "search"),
    ("https://search.naver.com/", "naver", "organic", "(organic)", "Organic Search", "search"),
    ("https://search.brave.com/", "brave", "organic", "(organic)", "Organic Search", "search"),
    ("https://www.ecosia.org/", "ecosia", "organic", "(organic)", "Organic Search", "search"),
    ("https://kagi.com/", "kagi", "organic", "(organic)", "Organic Search", "search"),
    ("https://www.startpage.com/", "startpage", "organic", "(organic)", "Organic Search",
     "search"),
    ("https://www.qwant.com/", "qwant", "organic", "(organic)", "Organic Search", "search"),
    ("https://l.facebook.com/l.php", "facebook.com", "referral", "(referral)", "Organic Social",
     "social"),
    ("https://lm.facebook.com/", "facebook.com", "referral", "(referral)", "Organic Social",
     "social"),
    ("https://m.facebook.com/", "facebook.com", "referral", "(referral)", "Organic Social",
     "social"),
    ("https://t.co/xyz", "x.com", "referral", "(referral)", "Organic Social", "social"),
    ("https://twitter.com/", "x.com", "referral", "(referral)", "Organic Social", "social"),
    ("https://lnkd.in/abc", "linkedin.com", "referral", "(referral)", "Organic Social", "social"),
    ("https://out.reddit.com/", "reddit.com", "referral", "(referral)", "Organic Social",
     "social"),
    ("https://old.reddit.com/", "reddit.com", "referral", "(referral)", "Organic Social",
     "social"),
    ("https://l.instagram.com/", "instagram.com", "referral", "(referral)", "Organic Social",
     "social"),
    ("https://www.pinterest.co.uk/", "pinterest.com", "referral", "(referral)", "Organic Social",
     "social"),
    ("https://news.ycombinator.com/item?id=1", "news.ycombinator.com", "referral", "(referral)",
     "Organic Social", "social"),
    ("https://bsky.app/profile/x", "bsky.app", "referral", "(referral)", "Organic Social",
     "social"),
    ("https://youtu.be/xyz", "youtube.com", "referral", "(referral)", "Organic Video", "video"),
    ("https://m.youtube.com/", "youtube.com", "referral", "(referral)", "Organic Video", "video"),
    ("https://www.amazon.de/dp/1", "amazon.com", "referral", "(referral)", "Organic Shopping",
     "shopping"),
    ("https://chatgpt.com/", "chatgpt.com", "ai-assistant", "(ai-assistant)", "AI Assistant",
     "ai"),
    ("https://chat.openai.com/", "chatgpt.com", "ai-assistant", "(ai-assistant)",
     "AI Assistant", "ai"),
    ("https://www.perplexity.ai/", "perplexity.ai", "ai-assistant", "(ai-assistant)",
     "AI Assistant", "ai"),
    ("https://gemini.google.com/app", "gemini.google.com", "ai-assistant", "(ai-assistant)",
     "AI Assistant", "ai"),
    ("https://claude.ai/chat/1", "claude.ai", "ai-assistant", "(ai-assistant)", "AI Assistant",
     "ai"),
    ("https://edgeservices.bing.com/", "copilot.microsoft.com", "ai-assistant",
     "(ai-assistant)", "AI Assistant", "ai"),
    ("https://mail.google.com/", "mail.google.com", "referral", "(referral)", "Referral", "email"),
    ("https://webmail.example.net/", "webmail.example.net", "referral", "(referral)",
     "Referral", "email"),
    ("https://www.example.org/blog", "example.org", "referral", "(referral)", "Referral", None),
    ("https://blog.example.org/", "blog.example.org", "referral", "(referral)", "Referral", None),
    ("https://docs.google.com/", "docs.google.com", "referral", "(referral)", "Referral", None),
    ("android-app://com.google.android.gm/", "mail.google.com", "referral", "(referral)",
     "Referral", "email"),
    ("android-app://com.google.android.googlequicksearchbox/https/www.google.com", "google",
     "organic", "(organic)", "Organic Search", "search"),
    ("android-app://com.linkedin.android/", "linkedin.com", "referral", "(referral)",
     "Organic Social", "social"),
    ("android-app://org.telegram.messenger/", "telegram.org", "referral", "(referral)",
     "Organic Social", "social"),
    ("android-app://com.openai.chatgpt/", "chatgpt.com", "ai-assistant", "(ai-assistant)",
     "AI Assistant", "ai"),
    ("android-app://com.samsung.android.email.provider/", "com.samsung.android.email.provider",
     "referral", "(referral)", "Referral", "email"),
    ("android-app://com.example.unknown/", "com.example.unknown", "referral", "(referral)",
     "Referral", None),
]


@pytest.mark.parametrize(
    "referrer,source,medium,campaign,channel,category", REFERRER_CLASSIFY_CASES
)
def test_classify_referrers(referrer, source, medium, campaign, channel, category):
    result = classify(PAGE, referrer)
    assert (result["source"], result["medium"], result["campaign"], result["channel"],
            result["source_category"]) == (source, medium, campaign, channel, category)
    assert result["is_internal"] is False


def test_classify_reports_referrer_host_and_domain():
    result = classify(PAGE, "https://www.google.co.uk/search?q=x")
    assert (result["referrer_host"], result["referrer_domain"]) == (
        "www.google.co.uk", "google.co.uk",
    )


@pytest.mark.parametrize(
    "referrer",
    [None, "", "about:blank", "chrome-extension://abc/x.html", "file:///C:/x.html", "not a url"],
)
def test_classify_direct(referrer):
    result = classify(PAGE, referrer)
    assert (result["source"], result["medium"], result["campaign"], result["channel"]) == (
        "(direct)", "(none)", "(direct)", "Direct",
    )
    assert result["source_category"] is None and result["is_internal"] is False
    assert result["referrer_host"] is None


# ---------------------------------------------------------------------------
# classify: internal referrers
# ---------------------------------------------------------------------------

INTERNAL_CASES = [
    # page url, referrer, site_hosts, expected is_internal
    ("https://www.example.com/a", "https://www.example.com/b", (), True),
    ("https://example.com/a", "https://www.example.com/b", (), True),
    ("https://www.example.com/a", "https://example.com/b", (), True),
    ("https://shop.example.com/", "https://blog.example.com/", (), True),
    ("https://example.com/", "https://a.b.example.com/", (), True),
    ("https://EXAMPLE.com/", "https://www.Example.COM:8443/", (), True),
    ("https://example.co.uk/", "https://www.example.co.uk/", (), True),
    ("https://example.com/", "https://example.org/", (), False),
    ("https://example.com/", "https://notexample.com/", (), False),
    ("https://example.co.uk/", "https://other.co.uk/", (), False),
    ("https://alice.github.io/", "https://bob.github.io/", (), False),
    ("https://shop.myshopify.com/", "https://other.myshopify.com/", (), False),
    ("https://example.com/", "https://checkout.payments.net/", ("payments.net",), True),
    ("https://example.com/", "https://payments.net/", ["https://www.payments.net/pay"], True),
    ("https://example.com/", "https://eu.payments.net/", ["*.payments.net"], True),
    ("https://example.com/", "https://eu.payments.net/", "payments.net", True),
    ("https://example.com/", "https://payments.net.evil.com/", ("payments.net",), False),
    ("https://example.com/", "https://www.example.fr/", ("example.fr", "example.de"), True),
    (None, "https://www.example.com/", ("example.com",), True),
    (None, "https://www.example.com/", (), False),
    ("/relative/path", "https://www.example.com/", ("www.example.com",), True),
    ("http://localhost:3000/", "http://localhost:3000/other", (), True),
    ("https://example.com/", "http://192.168.0.1/", ("192.168.0.1",), True),
    ("https://example.com/", "android-app://com.example/", ("com.example",), False),
]


@pytest.mark.parametrize("url,referrer,site_hosts,expected", INTERNAL_CASES)
def test_classify_internal_referrers(url, referrer, site_hosts, expected):
    result = classify(url, referrer, site_hosts)
    assert result["is_internal"] is expected
    if expected:
        for key in ("source", "medium", "campaign", "source_category", "channel"):
            assert result[key] is None, key
    else:
        assert result["channel"] in CHANNELS


def test_classify_internal_referrer_keeps_campaign_tags():
    result = classify(PAGE + "?utm_source=banner&utm_medium=internal",
                      "https://www.example.com/home")
    assert result["is_internal"] is True
    assert (result["source"], result["medium"], result["channel"]) == (
        "banner", "internal", "Unassigned",
    )


# ---------------------------------------------------------------------------
# classify: robustness
# ---------------------------------------------------------------------------


def test_classify_never_raises():
    for url, referrer in itertools.product(WEIRD, repeat=2):
        result = classify(url, referrer, site_hosts=referrer, ref_params=url)
        assert set(result) == CLASSIFY_KEYS
        assert result["channel"] in CHANNELS or result["is_internal"]


@pytest.mark.parametrize("value", WEIRD)
def test_classify_garbage_is_direct(value):
    result = classify(value, value)
    tags = parse_campaign(value)
    tagged = any(tags[f] for f in ("utm_source", "utm_medium", "utm_campaign", "utm_id"))
    if result["referrer_host"] is None and result["click_id"] is None and not tagged:
        assert (result["source"], result["channel"]) == ("(direct)", "Direct")


def test_classify_keeps_undecodable_tags_verbatim():
    result = classify("/?utm_source=%ZZ%&utm_medium=Bad%", None)
    assert (result["source"], result["medium"], result["channel"]) == (
        "%zz%", "bad%", "Unassigned",
    )


def test_classify_is_deterministic():
    args = (PAGE + "?utm_source=X&gclid=1", "https://l.facebook.com/")
    assert classify(*args) == classify(*args)


# ---------------------------------------------------------------------------
# extract_search_query
# ---------------------------------------------------------------------------

SEARCH_CASES = [
    ("https://x.com/search?q=running+shoes", "running shoes"),
    ("https://x.com/?s=hello%20world", "hello world"),
    ("https://x.com/?Q=Case+Kept", "Case Kept"),
    ("https://x.com/?q=&s=fallback", "fallback"),
    ("https://x.com/?q=%20%20&search=x", "x"),
    ("https://x.com/?s=first&q=second", "second"),
    ("https://x.com/?keyword=a+b+++c", "a b c"),
    ("https://x.com/?q=a%0Ab%09c%0D%0Ad", "a b c d"),
    ("https://x.com/?q=nul%00byte", "nul byte"),
    ("https://x.com/?q=a%C2%A0b", "a b"),
    ("https://x.com/?q=%E2%82%AC+100", "€ 100"),
    ("https://x.com/?q=%ZZ", "%ZZ"),
    ("https://x.com/?q=" + "a" * 300, "a" * 200),
    ("https://x.com/?q=" + "a" * 199 + "+b", "a" * 199),
    ("https://x.com/#/search?query=spa", "spa"),
    ("https://x.com/?q=real#/search?q=hash", "real"),
    ("https://x.com/?page=2", None),
    ("https://x.com/", None),
    ("https://x.com/?q", None),
    ("", None),
    (None, None),
    (42, None),
]


@pytest.mark.parametrize("url,expected", SEARCH_CASES)
def test_extract_search_query(url, expected):
    assert extract_search_query(url) == expected


@pytest.mark.parametrize(
    "params,expected",
    [
        (("term",), "boots"),
        ("term", "boots"),
        (["TERM"], "boots"),
        (("q", "term"), "boots"),
        ((), None),
        ([None, 3, "", "term"], "boots"),
        (None, None),
    ],
)
def test_extract_search_query_custom_params(params, expected):
    assert extract_search_query("https://x.com/?term=boots", params=params) == expected


@pytest.mark.parametrize("value", WEIRD)
def test_extract_search_query_never_raises(value):
    result = extract_search_query(value, params=value)
    assert result is None or isinstance(result, str)


# ---------------------------------------------------------------------------
# registrable_domain
# ---------------------------------------------------------------------------

DOMAIN_CASES = [
    ("www.google.co.uk", "google.co.uk"),
    ("google.com", "google.com"),
    ("a.b.example.com", "example.com"),
    ("WWW.Example.COM.", "example.com"),
    ("shop.example.com.au", "example.com.au"),
    ("news.bbc.co.uk", "bbc.co.uk"),
    ("foo.co.jp", "foo.co.jp"),
    ("x.y.ne.jp", "y.ne.jp"),
    ("www.google.com.co", "google.com.co"),
    ("t.co", "t.co"),
    ("alice.github.io", "alice.github.io"),
    ("www.alice.github.io", "alice.github.io"),
    ("bucket.s3.amazonaws.com", "bucket.s3.amazonaws.com"),
    ("example.com:8080", "example.com"),
    ("co.uk", "co.uk"),
    ("localhost", "localhost"),
    ("192.168.0.1", "192.168.0.1"),
    ("[::1]", "::1"),
    ("2001:db8::1", "2001:db8::1"),
    ("a..b", "a..b"),
    ("", None),
    (None, None),
    (5, None),
]


@pytest.mark.parametrize("host,expected", DOMAIN_CASES)
def test_registrable_domain(host, expected):
    assert registrable_domain(host) == expected


# ---------------------------------------------------------------------------
# Reference data integrity
# ---------------------------------------------------------------------------


def test_no_registration_conflicts():
    assert sources._CONFLICTS == []


def test_every_site_canonical_domain_resolves_to_the_site():
    for name, site in sources._SITES_BY_NAME.items():
        if site.canonical:
            assert referrer_info("https://%s/" % site.canonical)["name"] == name


def test_every_android_app_resolves():
    for package, site in sources._APPS.items():
        info = referrer_info("android-app://%s/" % package)
        assert (info["name"], info["category"]) == (site.name, site.category), package


@pytest.mark.parametrize("category", ["search", "social", "video", "shopping"])
def test_google_published_lists_keep_their_categories(category):
    mismatches = []
    for entry in sources._GA4_SOURCE_CATEGORIES[category].split(","):
        entry = entry.strip()
        if not entry:
            continue
        expected = sources._GA4_DEVIATIONS.get(entry.lower(), category)
        if source_category(entry) != expected:
            mismatches.append((entry, source_category(entry)))
        if "." in entry and referrer_info("https://%s/" % entry)["category"] != expected:
            mismatches.append(("referrer " + entry, referrer_info("https://" + entry)))
    assert mismatches == []


def test_ga4_deviations_are_webmail_hosts_from_the_search_list():
    search = {e.strip() for e in sources._GA4_SOURCE_CATEGORIES["search"].split(",")}
    for host, category in sources._GA4_DEVIATIONS.items():
        assert host in search
        assert category == "email"
        assert classify(PAGE, "https://%s/" % host)["channel"] == "Referral"


def test_click_ids_constant():
    assert len(CLICK_IDS) == len(set(CLICK_IDS))
    assert all(name == name.lower() for name in CLICK_IDS)
    assert CLICK_IDS[:3] == ("gclid", "gbraid", "wbraid")
    for required in ("dclid", "msclkid", "fbclid", "ttclid", "twclid", "li_fat_id", "yclid",
                     "epik", "rdt_cid", "sccid", "irclickid", "mc_eid", "mc_cid", "_kx",
                     "srsltid"):
        assert required in CLICK_IDS
    # Identifiers that override the referrer come before the organic ones.
    strong = [sources._CLICK_RULES[name].strong for name in CLICK_IDS]
    assert strong == sorted(strong, reverse=True)


def test_sources_version():
    assert isinstance(SOURCES_VERSION, int) and SOURCES_VERSION >= 1


@pytest.mark.parametrize("value", WEIRD)
def test_public_helpers_never_raise(value):
    info = referrer_info(value)
    assert set(info) == {"referrer_host", "referrer_domain", "name", "category"}
    assert source_category(value) in CATEGORIES + (None,)
    result = registrable_domain(value)
    assert result is None or isinstance(result, str)
