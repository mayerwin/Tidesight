"""Traffic-source attribution: campaign tags, referrers and GA4 channel groups.

``classify()`` turns the URL and referrer of a session's entry pageview into
the attribution Google Analytics 4 would report (source, medium, campaign and
default channel group), plus a source category that dashboards can group on.
Its building blocks are public too:

- ``parse_campaign()``: UTM tags (with Matomo/Piwik fallbacks), click
  identifiers and other ad parameters found in a URL;
- ``referrer_info()``: host, registrable domain, known-site name and category
  of a referrer, including ``android-app://`` referrers;
- ``source_category()``: the category of a source name or host;
- ``channel_group()``: GA4's default channel group rules;
- ``extract_search_query()``: the site-search term in a page URL;
- ``registrable_domain()``: ``www.google.co.uk`` -> ``google.co.uk``.

All functions are pure and never raise: malformed input gives the direct or
unknown result. Derived rows store ``SOURCES_VERSION``; bump it whenever any
output changes (a list update counts) so that history is recomputed.

Reference lists. Google publishes the source lists behind its channel rules
(``SOURCE_CATEGORY_SEARCH``, ``_SOCIAL``, ``_VIDEO`` and ``_SHOPPING``); they
are embedded verbatim in ``_GA4_SOURCE_CATEGORIES`` and every entry keeps
Google's category, except the few webmail hosts listed in ``_GA4_DEVIATIONS``.
A curated table on top adds what Google's lists lack (Threads, Bluesky,
Mastodon, Discord, newer search engines and marketplaces...), maps redirectors,
mobile hosts and country domains to one canonical source per site, and adds
two categories GA4 has no list for: ``email`` (webmail) and ``ai`` (AI
assistants).

Standard library only; Python 3.9+.
"""

from __future__ import annotations

import re
from typing import Any, Dict, FrozenSet, List, NamedTuple, Optional, Sequence, Tuple
from urllib.parse import unquote_plus, urlsplit

__all__ = [
    "CATEGORIES",
    "CHANNELS",
    "CLICK_IDS",
    "DEFAULT_SEARCH_PARAMS",
    "REF_PARAMS",
    "SOURCES_VERSION",
    "channel_group",
    "classify",
    "extract_search_query",
    "parse_campaign",
    "referrer_info",
    "registrable_domain",
    "source_category",
]

SOURCES_VERSION = 1  # bump whenever any output changes, list updates included

#: Source categories. The first four are GA4's; "email" (webmail) and "ai"
#: (AI assistants) are Tidesight's own and take part in no GA4 rule.
CATEGORIES: Tuple[str, ...] = ("search", "social", "video", "shopping", "email", "ai")

#: GA4 default channels, in their order of evaluation.
CHANNELS: Tuple[str, ...] = (
    "Direct",
    "Cross-network",
    "Paid Shopping",
    "Paid Search",
    "Paid Social",
    "Paid Video",
    "Display",
    "Paid Other",
    "Organic Shopping",
    "Organic Social",
    "Organic Video",
    "Organic Search",
    "AI Assistant",
    "Referral",
    "Email",
    "Affiliates",
    "Audio",
    "SMS",
    "Mobile Push Notifications",
    "Unassigned",
)

#: Site-search parameters, as in the ``search_params`` site setting.
DEFAULT_SEARCH_PARAMS: Tuple[str, ...] = ("q", "s", "search", "query", "keyword")

#: Referral-style parameters that ``ref_params`` can promote to a source.
REF_PARAMS: Tuple[str, ...] = ("ref", "source", "via")

_DIRECT = "(direct)"
_NONE = "(none)"
_NOT_SET = "(not set)"
_ORGANIC_CAMPAIGN = "(organic)"
_REFERRAL_CAMPAIGN = "(referral)"
_AI_MEDIUM = "ai-assistant"
_AI_CAMPAIGN = "(ai-assistant)"

_MAX_VALUE_LENGTH = 500
_MAX_EXTRA_PARAMS = 50
_MAX_SEARCH_QUERY = 200


# ---------------------------------------------------------------------------
# Click identifiers and campaign parameters
# ---------------------------------------------------------------------------


class _ClickRule(NamedTuple):
    """How a click identifier attributes a visit that has no campaign tags."""

    source: str
    medium: str
    strong: bool  # True: overrides the referrer, as GA4 auto-tagging does


# Order matters: the first identifier present is reported, so referrer-overriding
# identifiers come before the organic ones. Names are lower case because they
# match case-insensitively (Snapchat documents "ScCid").
_CLICK_RULES: Dict[str, _ClickRule] = {
    # Search and display advertising.
    "gclid": _ClickRule("google", "cpc", True),
    "gbraid": _ClickRule("google", "cpc", True),
    "wbraid": _ClickRule("google", "cpc", True),
    "dclid": _ClickRule("dv360", "cpm", True),
    "msclkid": _ClickRule("bing", "cpc", True),
    "yclid": _ClickRule("yandex", "cpc", True),
    # Social advertising.
    "ttclid": _ClickRule("tiktok", "cpc", True),
    "twclid": _ClickRule("twitter", "cpc", True),
    "li_fat_id": _ClickRule("linkedin", "cpc", True),
    "epik": _ClickRule("pinterest", "cpc", True),
    "sccid": _ClickRule("snapchat", "cpc", True),
    "rdt_cid": _ClickRule("reddit", "cpc", True),
    "ndclid": _ClickRule("nextdoor", "cpc", True),
    # Native advertising.
    "tblci": _ClickRule("taboola", "cpc", True),
    "dicbo": _ClickRule("outbrain", "cpc", True),
    # Affiliate networks.
    "irclickid": _ClickRule("impact", "affiliate", True),
    "cjevent": _ClickRule("cj", "affiliate", True),
    "awc": _ClickRule("awin", "affiliate", True),
    "sscid": _ClickRule("shareasale", "affiliate", True),
    "tduid": _ClickRule("tradedoubler", "affiliate", True),
    "admitad_uid": _ClickRule("admitad", "affiliate", True),
    # Email service providers.
    "mc_eid": _ClickRule("mailchimp", "email", True),
    "mc_cid": _ClickRule("mailchimp", "email", True),
    "_kx": _ClickRule("klaviyo", "email", True),
    "_hsenc": _ClickRule("hubspot", "email", True),
    "_hsmi": _ClickRule("hubspot", "email", True),
    "mkt_tok": _ClickRule("marketo", "email", True),
    # Added to organic links too: used only when there is no referrer at all.
    "fbclid": _ClickRule("facebook.com", "referral", False),
    "srsltid": _ClickRule("google", "organic", False),
    "ysclid": _ClickRule("yandex", "organic", False),
}

#: Click identifiers, in the order in which ``parse_campaign()`` looks for them.
CLICK_IDS: Tuple[str, ...] = tuple(_CLICK_RULES)

_CAMPAIGN_FIELDS: Tuple[str, ...] = (
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "utm_source_platform",
    "utm_creative_format",
    "utm_marketing_tactic",
)

# Matomo (and earlier Piwik) campaign parameters, used when the UTM one is absent.
_FIELD_ALIASES: Dict[str, Tuple[str, ...]] = {
    "utm_source": ("mtm_source", "matomo_source", "pk_source", "piwik_source"),
    "utm_medium": ("mtm_medium", "matomo_medium", "pk_medium", "piwik_medium"),
    "utm_campaign": (
        "mtm_campaign", "matomo_campaign", "mtm_cpn", "pk_campaign", "piwik_campaign", "pk_cpn",
    ),
    "utm_term": (
        "mtm_keyword", "matomo_keyword", "mtm_kwd", "pk_keyword", "piwik_keyword", "pk_kwd",
    ),
    "utm_content": ("mtm_content", "matomo_content", "pk_content", "piwik_content"),
    "utm_id": ("mtm_cid", "matomo_cid", "pk_cid", "piwik_cid"),
}

# Parameters that tag a campaign source (as opposed to referral-style ones).
_SOURCE_TAGS: FrozenSet[str] = frozenset(("utm_source",) + _FIELD_ALIASES["utm_source"])

# Campaign families also read from the hash fragment.
_CAMPAIGN_PREFIXES: Tuple[str, ...] = ("utm_", "mtm_", "matomo_", "pk_", "piwik_")

# Recognised tracking parameters reported in ``extra`` (besides the prefixes below).
_EXTRA_PARAMS: FrozenSet[str] = frozenset({
    # Google Ads, Merchant Center and the Google tag.
    "gad_source", "gad_campaignid", "gclsrc", "_gl",
    # Adobe Advertising and Search Ads 360.
    "s_kwcid", "ef_id", "s_cid",
    # Affiliate networks, besides their click identifiers.
    "irgwc", "cjdata", "ranmid", "raneaid", "ransiteid", "clickref",
    # Email tools, besides their click identifiers.
    "vero_id", "vero_conv", "ck_subscriber_id", "ml_subscriber", "ml_subscriber_hash",
    "elqtrackid", "elqtrack", "oly_enc_id", "oly_anon_id", "dm_i",
    # Referral-style parameters (see REF_PARAMS).
    "ref", "ref_src", "source", "via",
})
_EXTRA_PREFIXES: Tuple[str, ...] = _CAMPAIGN_PREFIXES + ("hsa_",)


# ---------------------------------------------------------------------------
# GA4 channel rules (manual traffic), https://support.google.com/analytics/answer/9756891
# ---------------------------------------------------------------------------

_PAID_MEDIUM = re.compile(r"^(.*cp.*|ppc|retargeting|paid.*)$")
_SHOPPING_CAMPAIGN = re.compile(r"^(.*(([^a-df-z]|^)shop|shopping).*)$")
_VIDEO_MEDIUM = re.compile(r"^(.*video.*)$")
_DISPLAY_MEDIUMS = frozenset({"display", "banner", "expandable", "interstitial", "cpm"})
_SOCIAL_MEDIUMS = frozenset(
    {"social", "social-network", "social-media", "sm", "social network", "social media"}
)
_REFERRAL_MEDIUMS = frozenset({"referral", "app", "link"})
_EMAIL_VALUES = frozenset({"email", "e-mail", "e_mail", "e mail"})
_GA4_CATEGORIES = frozenset({"search", "social", "video", "shopping"})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_campaign(url: Optional[str], *, ref_params: Sequence[str] = ()) -> Dict[str, Any]:
    """Campaign tags, click identifier and other ad parameters of a page URL.

    Returns a dict with:

    - ``utm_source``, ``utm_medium``, ``utm_campaign``, ``utm_term``,
      ``utm_content``, ``utm_id``, ``utm_source_platform``,
      ``utm_creative_format`` and ``utm_marketing_tactic``: the UTM value or,
      when the UTM parameter is absent, its Matomo/Piwik equivalent
      (``mtm_source``, ``pk_campaign``, ``pk_kwd``...). Matomo reads both
      families, so links tagged for Matomo keep working;
    - ``source_param``: the parameter that supplied ``utm_source``;
    - ``click_id_type`` and ``click_id``: the first identifier of
      ``CLICK_IDS`` present (in that order) and its value, kept exactly as
      received;
    - ``extra``: the other recognised tracking parameters, keyed by
      lower-cased name: ``gad_source``, ``gad_campaignid``, ``mtm_*``,
      ``pk_*``, non-standard ``utm_*``, ``hsa_*``, further click identifiers,
      ``ref``, ``via``, ``source``...

    Parameter names match case-insensitively. Values are URL-decoded (``+``
    is a space) and stripped, capped at 500 characters, and empty values
    count as absent; the first non-empty occurrence wins. The hash fragment
    is scanned for campaign tags too (``#/route?utm_source=x`` or
    ``#utm_source=x``, as some single-page apps produce), with the query
    string taking precedence.

    ``ref_params`` names referral-style parameters (see ``REF_PARAMS``) whose
    value becomes ``utm_source`` when no campaign source is tagged. It is off
    by default: Plausible, Fathom and Simple Analytics read ``?ref=``, but
    GA4 and Matomo ignore it, and many sites use these names for internal
    navigation, affiliate codes or Amazon-style tracking (``ref=sr_1_1``),
    which would pollute attribution. The raw values are always kept in
    ``extra``.
    """
    result = _empty_campaign()
    if not isinstance(url, str) or not url.strip():
        return result
    try:
        _fill_campaign(result, url.strip(), _param_names(ref_params))
    except Exception:  # pragma: no cover - defensive: attribution never fails a job
        return _empty_campaign()
    return result


def referrer_info(referrer: Optional[str]) -> Dict[str, Optional[str]]:
    """Host, registrable domain, known-site name and category of a referrer.

    >>> info = referrer_info("https://www.google.co.uk/search")
    >>> info["referrer_host"], info["referrer_domain"], info["name"], info["category"]
    ('www.google.co.uk', 'google.co.uk', 'google', 'search')

    ``category`` is one of ``CATEGORIES`` or None. Redirectors and mobile
    hosts resolve to their site (``l.facebook.com`` and ``lm.facebook.com``
    are ``facebook``, ``t.co`` is ``twitter``, ``lnkd.in`` is ``linkedin``),
    and brands are recognised under any country domain (``google.com.br``,
    ``yandex.kz``, ``amazon.de``). Subdomains such as ``mail.``, ``webmail.``
    or ``email.`` count as email even when the provider is unknown.

    For ``android-app://<package>/...`` referrers the host and domain are the
    package id, and known apps resolve to their site
    (``com.google.android.gm`` is ``gmail``, category ``email``). Referrers
    that are not http(s) or app URLs (``about:blank``, browser extensions,
    malformed text) give all-None values.
    """
    try:
        ref = _parse_referrer(referrer)
    except Exception:  # pragma: no cover - defensive
        ref = None
    if ref is None:
        return {"referrer_host": None, "referrer_domain": None, "name": None, "category": None}
    return {
        "referrer_host": ref.host,
        "referrer_domain": ref.domain,
        "name": _referrer_site_name(ref),
        "category": _referrer_category(ref),
    }


def source_category(source_or_host: Optional[str]) -> Optional[str]:
    """Category of a source name, host, URL or Android package id.

    Returns ``"search"``, ``"social"``, ``"video"``, ``"shopping"``,
    ``"email"``, ``"ai"`` or None. Names match exactly and case-insensitively,
    as in GA4 (``google``, ``fb``, ``ig``, ``IGShopping``); hosts also match
    their known subdomains and country domains (``m.facebook.com``,
    ``www.google.de``).
    """
    try:
        return _category_of(source_or_host)
    except Exception:  # pragma: no cover - defensive
        return None


def channel_group(
    source: Optional[str],
    medium: Optional[str],
    campaign: Optional[str] = None,
    source_category: Optional[str] = None,
) -> str:
    """GA4 default channel group of a source, medium and campaign.

    Implements GA4's rules for manually tagged traffic (Google Analytics Help
    article 9756891, as of 2026), evaluated in this order. Matching is
    case-insensitive and missing values read as ``(not set)``. "Paid medium"
    is ``^(.*cp.*|ppc|retargeting|paid.*)$`` and "shopping campaign" is
    ``^(.*(([^a-df-z]|^)shop|shopping).*)$``.

    1. Direct: source is ``(direct)`` and medium is ``(not set)`` or ``(none)``.
    2. Cross-network: campaign contains ``cross-network``.
    3. Paid Shopping: shopping source or shopping campaign, and paid medium.
    4. Paid Search: search source and paid medium.
    5. Paid Social: social source and paid medium.
    6. Paid Video: video source and paid medium.
    7. Display: medium is display, banner, expandable, interstitial or cpm.
    8. Paid Other: paid medium.
    9. Organic Shopping: shopping source or shopping campaign.
    10. Organic Social: social source, or medium is social, social-network,
        social-media, sm, "social network" or "social media".
    11. Organic Video: video source, or medium contains ``video``.
    12. Organic Search: search source, or medium is ``organic``.
    13. AI Assistant: medium is ``ai-assistant`` (added by GA4 in May 2026).
    14. Referral: medium is referral, app or link.
    15. Email: source or medium is email, e-mail, e_mail or "e mail".
    16. Affiliates: medium is ``affiliate``.
    17. Audio: medium is ``audio``.
    18. SMS: source or medium is ``sms``.
    19. Mobile Push Notifications: medium ends with ``push`` or contains
        ``mobile`` or ``notification``, or source is ``firebase``.
    20. Unassigned: anything else.

    ``source_category`` is the GA4 category of the source: ``search``,
    ``social``, ``video``, ``shopping`` (Google's ``SOURCE_CATEGORY_SEARCH``
    spelling is accepted too); other values such as ``email`` or ``ai`` take
    part in no rule. When it is None, it is derived from ``source`` with
    ``source_category()``. GA4's rules for Google Ads, DV360, CM360, SA360 and
    Merchant Center traffic need data from those products (network type,
    campaign type, creative format) and are not implemented.
    """
    try:
        return _channel_group(source, medium, campaign, source_category)
    except Exception:  # pragma: no cover - defensive
        return "Unassigned"


def classify(
    url: Optional[str],
    referrer: Optional[str],
    site_hosts: Sequence[str] = (),
    *,
    ref_params: Sequence[str] = (),
) -> Dict[str, Any]:
    """Attribute a session's entry pageview, as GA4 would.

    ``url`` is the page URL and ``referrer`` its ``document.referrer`` (or the
    ``Referer`` header). ``site_hosts`` lists other hosts that belong to the
    site, such as a checkout or another language domain; their subdomains
    count too. ``ref_params`` is passed on to ``parse_campaign()``.

    Returns ``source``, ``medium``, ``campaign``, ``term``, ``content``,
    ``campaign_id``, ``source_platform``, ``click_id_type``, ``click_id``,
    ``referrer_host``, ``referrer_domain``, ``source_category``,
    ``is_internal`` and ``channel`` (see ``channel_group()``).

    Precedence:

    1. **Campaign tags**, when ``utm_source``, ``utm_medium``,
       ``utm_campaign`` or ``utm_id`` (or a Matomo equivalent) is present.
       Source and medium are the lower-cased tags, ``(not set)`` when
       missing; campaign is the tag as written, or ``(not set)``. A source
       that is a known AI assistant with no medium gets medium
       ``ai-assistant``: ChatGPT appends ``?utm_source=chatgpt.com`` to its
       links, which GA4 leaves Unassigned. Tags beat click identifiers; GA4
       prefers ``gclid`` when the Ads account is linked, but without Google
       Ads data the tags carry the campaign names. ``campaign_id`` falls
       back to ``gad_campaignid``.
    2. **Click identifiers**, which override the referrer as GA4 auto-tagging
       does. ``gclid``, ``gbraid``, ``wbraid`` give google / cpc, and so do
       ``gad_source`` or ``gad_campaignid`` alone, which Google adds to every
       ad click and which survive when a browser strips ``gclid``.
       ``dclid`` gives dv360 / cpm (Display: GA4 names the platform DV360
       and Google's tagging guidance uses medium cpm). ``msclkid`` gives
       bing / cpc; ``yclid`` gives yandex / cpc, or yahoo / cpc when the
       referrer is Yahoo (Yahoo! JAPAN Ads use the same name). Social ad
       identifiers give the network / cpc (tiktok, twitter, linkedin,
       pinterest, snapchat, reddit, nextdoor); Taboola and Outbrain give
       Paid Other; affiliate-network identifiers give network / affiliate;
       email-tool identifiers give tool / email.
    3. **Referrer.** Internal referrers set ``is_internal`` and leave source,
       medium, campaign, ``source_category`` and channel None, so the
       sessions job keeps the session's previous attribution (GA4's last
       non-direct click) or falls back to direct. A search engine gives
       engine / organic, campaign ``(organic)``. An AI assistant gives its
       domain / ai-assistant, campaign ``(ai-assistant)``, which is how GA4
       feeds its AI Assistant channel. Any other referrer gives the site's
       canonical domain (``l.facebook.com`` -> ``facebook.com``, ``t.co`` ->
       ``x.com``) or the host without ``www.``, medium ``referral``, campaign
       ``(referral)``. Webmail stays Referral, as in GA4; its
       ``source_category`` of ``email`` lets dashboards regroup it. Android
       app referrers give the app's site, or the package id when unknown.
    4. **Organic identifiers**, only when there is no referrer at all.
       ``fbclid`` gives facebook.com / referral (Organic Social): Meta adds
       it to outbound links from organic Facebook and Instagram posts as
       well as from ads, so it never means paid by itself (paid Meta traffic
       needs UTM tags), and it survives copy and paste, so a real referrer
       wins. ``srsltid`` (Merchant Center, organic product results) gives
       google / organic; ``ysclid`` (Yandex organic results) gives
       yandex / organic.
    5. **Direct**: ``(direct)`` / ``(none)``, campaign ``(direct)``.

    ``is_internal`` is True when the referrer is the page's own host, shares
    its registrable domain (``www`` and apex, ``blog`` and ``shop``
    subdomains), or is one of ``site_hosts``. Platforms whose subdomains have
    different owners (``github.io``, ``netlify.app``, ``blogspot.com``...)
    are not treated as one site.
    """
    try:
        return _classify(url, referrer, site_hosts, ref_params)
    except Exception:  # pragma: no cover - defensive: attribution never fails a job
        return _classification(None, False, None, (_DIRECT, _NONE, _DIRECT))


def extract_search_query(
    url: Optional[str], params: Sequence[str] = DEFAULT_SEARCH_PARAMS
) -> Optional[str]:
    """Site-search term in a page URL, or None.

    ``params`` are tried in order, case-insensitively, first in the query
    string and then in the hash fragment (``#/search?q=...``); the first
    non-empty value wins. It is URL-decoded, control characters become
    spaces, whitespace is collapsed and the result is cut to 200 characters.
    """
    if not isinstance(url, str) or not url.strip():
        return None
    try:
        query, fragment = _split_url(url.strip())
        pairs = _parse_params(query) + _parse_params(_fragment_params(fragment))
        for name in _param_names(params):
            for key, value in pairs:
                if key == name:
                    text = _normalise_search_term(value)
                    if text:
                        return text
    except Exception:  # pragma: no cover - defensive
        return None
    return None


def registrable_domain(host: Optional[str]) -> Optional[str]:
    """Registrable domain of a host: ``www.google.co.uk`` -> ``google.co.uk``.

    Uses a compact built-in list of public suffixes: second-level suffixes of
    country domains (``co.uk``, ``com.au``, ``co.jp``, ``com.br``...) and
    multi-tenant hosting platforms (``github.io``, ``blogspot.com``...). IP
    addresses, single-label hosts and public suffixes themselves are returned
    unchanged.
    """
    clean = _clean_host(host)
    if clean is None:
        return None
    if ":" in clean or _IPV4.fullmatch(clean) or "." not in clean:
        return clean
    labels = clean.split(".")
    if "" in labels:
        return clean
    suffix_length = _public_suffix_length(labels)
    if suffix_length >= len(labels):
        return clean
    return ".".join(labels[-(suffix_length + 1):])


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------

_Attribution = Tuple[str, str, str]  # source, medium, campaign


def _classify(url: Any, referrer: Any, site_hosts: Any, ref_params: Any) -> Dict[str, Any]:
    """Body of ``classify()``."""
    tags = parse_campaign(url, ref_params=ref_params)
    ref = _parse_referrer(referrer)
    internal = (
        ref is not None
        and not ref.is_app
        and _is_internal(ref.host, _host_of(url), _site_host_list(site_hosts))
    )
    attribution = _tag_attribution(tags) or _click_attribution(tags, ref, strong=True)
    if attribution is None and ref is not None and not internal:
        attribution = _referrer_attribution(ref)
    if attribution is None and not internal:
        attribution = _click_attribution(tags, ref, strong=False) or (_DIRECT, _NONE, _DIRECT)
    return _classification(tags, internal, ref, attribution)


def _classification(
    tags: Optional[Dict[str, Any]],
    internal: bool,
    ref: Optional[_Referrer],
    attribution: Optional[_Attribution],
) -> Dict[str, Any]:
    """Assemble the ``classify()`` result."""
    tags = tags or _empty_campaign()
    result: Dict[str, Any] = {
        "source": None,
        "medium": None,
        "campaign": None,
        "term": tags["utm_term"],
        "content": tags["utm_content"],
        "campaign_id": tags["utm_id"] or tags["extra"].get("gad_campaignid"),
        "source_platform": tags["utm_source_platform"],
        "click_id_type": tags["click_id_type"],
        "click_id": tags["click_id"],
        "referrer_host": ref.host if ref is not None else None,
        "referrer_domain": ref.domain if ref is not None else None,
        "source_category": None,
        "is_internal": bool(internal),
        "channel": None,
    }
    if attribution is not None:
        source, medium, campaign = attribution
        category = _category_of(source)
        result.update(
            source=source,
            medium=medium,
            campaign=campaign,
            source_category=category,
            channel=_channel_group(source, medium, campaign, category),
        )
    return result


def _tag_attribution(tags: Dict[str, Any]) -> Optional[_Attribution]:
    """Attribution from campaign tags, or None when the URL has none."""
    if not any(tags[field] for field in ("utm_source", "utm_medium", "utm_campaign", "utm_id")):
        return None
    source = (tags["utm_source"] or _NOT_SET).lower()
    campaign = tags["utm_campaign"]
    if tags["utm_medium"]:
        return source, tags["utm_medium"].lower(), campaign or _NOT_SET
    if _category_of(source) == "ai":
        return source, _AI_MEDIUM, campaign or _AI_CAMPAIGN
    if tags["source_param"] and tags["source_param"] not in _SOURCE_TAGS:
        # Promoted from ?ref= and friends: the visitor was referred by that source.
        return source, "referral", campaign or _REFERRAL_CAMPAIGN
    return source, _NOT_SET, campaign or _NOT_SET


def _click_attribution(
    tags: Dict[str, Any], ref: Optional[_Referrer], strong: bool
) -> Optional[_Attribution]:
    """Attribution from a click identifier of the requested strength."""
    click_type = tags["click_id_type"]
    rule = _CLICK_RULES.get(click_type) if click_type else None
    if not strong:
        if rule is None or rule.strong:
            return None
        campaign = _ORGANIC_CAMPAIGN if rule.medium == "organic" else _REFERRAL_CAMPAIGN
        return rule.source, rule.medium, campaign
    if rule is not None and rule.strong:
        source = rule.source
        if click_type == "yclid" and _referrer_site_name(ref) == "yahoo":
            source = "yahoo"  # Yahoo! JAPAN Ads also use yclid
        return source, rule.medium, _NOT_SET
    extra = tags["extra"]
    if extra.get("gad_source") or extra.get("gad_campaignid"):
        return "google", "cpc", _NOT_SET
    return None


def _referrer_attribution(ref: _Referrer) -> _Attribution:
    """Attribution from an external referrer."""
    site = ref.site
    if site is not None and site.category == "search":
        return site.name, "organic", _ORGANIC_CAMPAIGN
    if site is not None and site.canonical:
        source = site.canonical
    else:
        source = _strip_www(ref.host)
    if site is not None and site.category == "ai":
        return source, _AI_MEDIUM, _AI_CAMPAIGN
    return source, "referral", _REFERRAL_CAMPAIGN


def _is_internal(ref_host: str, page_host: Optional[str], site_hosts: Sequence[str]) -> bool:
    """Whether a referrer host belongs to the site of the page."""
    host = _strip_www(ref_host)
    if page_host:
        page = _strip_www(page_host)
        if host == page or registrable_domain(host) == registrable_domain(page):
            return True
    return any(host == known or host.endswith("." + known) for known in site_hosts)


def _site_host_list(site_hosts: Any) -> Tuple[str, ...]:
    """Normalise ``site_hosts``: hosts, URLs or ``*.example.com`` patterns."""
    if isinstance(site_hosts, str):
        site_hosts = (site_hosts,)
    try:
        items = list(site_hosts or ())
    except TypeError:
        return ()
    hosts: List[str] = []
    for item in items:
        if not isinstance(item, str):
            continue
        text = item.strip().lstrip("*").lstrip(".")
        host = _host_of(text) if ("://" in text or "/" in text) else _clean_host(text)
        if host:
            hosts.append(_strip_www(host))
    return tuple(hosts)


def _channel_group(source: Any, medium: Any, campaign: Any, category: Any) -> str:
    """Body of ``channel_group()``; see its docstring for the rules."""
    src = _dimension(source)
    med = _dimension(medium)
    camp = _dimension(campaign)
    cat = _ga4_category(category if category else _category_of(src))
    paid = _PAID_MEDIUM.match(med) is not None
    shopping_campaign = _SHOPPING_CAMPAIGN.match(camp) is not None

    if src == _DIRECT and med in (_NOT_SET, _NONE):
        return "Direct"
    if "cross-network" in camp:
        return "Cross-network"
    if paid:
        if cat == "shopping" or shopping_campaign:
            return "Paid Shopping"
        if cat == "search":
            return "Paid Search"
        if cat == "social":
            return "Paid Social"
        if cat == "video":
            return "Paid Video"
    if med in _DISPLAY_MEDIUMS:
        return "Display"
    if paid:
        return "Paid Other"
    if cat == "shopping" or shopping_campaign:
        return "Organic Shopping"
    if cat == "social" or med in _SOCIAL_MEDIUMS:
        return "Organic Social"
    if cat == "video" or _VIDEO_MEDIUM.match(med):
        return "Organic Video"
    if cat == "search" or med == "organic":
        return "Organic Search"
    if med == _AI_MEDIUM:
        return "AI Assistant"
    if med in _REFERRAL_MEDIUMS:
        return "Referral"
    if src in _EMAIL_VALUES or med in _EMAIL_VALUES:
        return "Email"
    if med == "affiliate":
        return "Affiliates"
    if med == "audio":
        return "Audio"
    if src == "sms" or med == "sms":
        return "SMS"
    if med.endswith("push") or "mobile" in med or "notification" in med or src == "firebase":
        return "Mobile Push Notifications"
    return "Unassigned"


def _dimension(value: Any) -> str:
    """A GA4 dimension value for matching: lower case, ``(not set)`` when empty."""
    if value is None:
        return _NOT_SET
    text = str(value).strip().lower()
    return text or _NOT_SET


def _ga4_category(value: Any) -> Optional[str]:
    """``search``/``social``/``video``/``shopping``, or None for anything else."""
    if not isinstance(value, str):
        return None
    key = value.strip().lower()
    if key.startswith("source_category_"):
        key = key[len("source_category_"):]
    return key if key in _GA4_CATEGORIES else None


# ---------------------------------------------------------------------------
# URL parameters
# ---------------------------------------------------------------------------


def _empty_campaign() -> Dict[str, Any]:
    """A ``parse_campaign()`` result with nothing found."""
    result: Dict[str, Any] = {field: None for field in _CAMPAIGN_FIELDS}
    result.update(source_param=None, click_id_type=None, click_id=None, extra={})
    return result


def _fill_campaign(result: Dict[str, Any], url: str, ref_params: Tuple[str, ...]) -> None:
    """Fill a ``parse_campaign()`` result from a URL."""
    query, fragment = _split_url(url)
    values = _first_values(_parse_params(query))
    for name, value in _first_values(_parse_params(_fragment_params(fragment))).items():
        if name.startswith(_CAMPAIGN_PREFIXES):
            values.setdefault(name, value)

    for field in _CAMPAIGN_FIELDS:
        for name in (field,) + _FIELD_ALIASES.get(field, ()):
            if name in values:
                result[field] = values[name]
                if field == "utm_source":
                    result["source_param"] = name
                break
    if result["utm_source"] is None:
        for name in ref_params:
            if name in values:
                result["utm_source"] = _referral_source(values[name])
                result["source_param"] = name
                break

    click_key = next((name for name in CLICK_IDS if name in values), None)
    if click_key is not None:
        result["click_id_type"] = click_key
        result["click_id"] = values[click_key]

    extra: Dict[str, str] = {}
    for name, value in values.items():
        if len(extra) >= _MAX_EXTRA_PARAMS:
            break
        if name in _CAMPAIGN_FIELDS or name == click_key:
            continue
        if (
            name in _EXTRA_PARAMS
            or name in _CLICK_RULES
            or name in ref_params
            or name.startswith(_EXTRA_PREFIXES)
        ):
            extra[name] = value
    result["extra"] = extra


def _split_url(url: str) -> Tuple[str, str]:
    """Query string and fragment of a URL (either may be empty)."""
    base, _, fragment = url.partition("#")
    _, _, query = base.partition("?")
    return query, fragment


def _fragment_params(fragment: str) -> str:
    """The parameter part of a fragment: after its first ``?``, or all of it."""
    _, question, after = fragment.partition("?")
    return after if question else fragment


def _parse_params(query: str) -> List[Tuple[str, str]]:
    """``(lower-cased name, decoded value)`` pairs of a query string, in order."""
    pairs: List[Tuple[str, str]] = []
    for part in query.split("&"):
        if not part:
            continue
        name, _, value = part.partition("=")
        key = unquote_plus(name, errors="replace").strip().lower()
        if key:
            pairs.append((key, unquote_plus(value, errors="replace")))
    return pairs


def _first_values(pairs: Sequence[Tuple[str, str]]) -> Dict[str, str]:
    """First non-empty, stripped and capped value of each parameter."""
    values: Dict[str, str] = {}
    for name, raw in pairs:
        if name in values:
            continue
        value = raw.strip()[:_MAX_VALUE_LENGTH].strip()
        if value:
            values[name] = value
    return values


def _param_names(names: Any) -> Tuple[str, ...]:
    """Lower-cased, non-empty parameter names from a sequence (or one string)."""
    if isinstance(names, str):
        names = (names,)
    try:
        items = list(names or ())
    except TypeError:
        return ()
    return tuple(n.strip().lower() for n in items if isinstance(n, str) and n.strip())


def _referral_source(value: str) -> str:
    """A ``?ref=`` value as a source: the host when it is a URL."""
    if "://" in value:
        host = _host_of(value)
        if host:
            return _strip_www(host)
    return value


def _normalise_search_term(value: str) -> Optional[str]:
    """Collapse whitespace and control characters, cap the length."""
    text = "".join(ch if ch.isprintable() else " " for ch in value)
    text = " ".join(text.split())[:_MAX_SEARCH_QUERY].strip()
    return text or None


# ---------------------------------------------------------------------------
# Hosts, referrers and known sites
# ---------------------------------------------------------------------------

_WWW_PREFIX = re.compile(r"www\d*\.")
_IPV4 = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}")
_BARE_HOST = re.compile(r"([a-z0-9-]+(?:\.[a-z0-9-]+)+)\.?(?::\d+)?(?:[/?#].*)?", re.I | re.S)
_APP_SCHEMES: Tuple[str, ...] = ("android-app:", "ios-app:")
_WEBMAIL_LABELS: FrozenSet[str] = frozenset(
    {"mail", "webmail", "email", "mailbox", "correo", "posta", "messagerie"}
)


class _Site(NamedTuple):
    """A known traffic source."""

    name: str  # normalised source name ("google", "facebook")
    category: Optional[str]  # one of CATEGORIES, or None
    canonical: Optional[str]  # canonical source domain ("facebook.com"); None for apps only


class _Referrer(NamedTuple):
    """A parsed referrer."""

    host: str  # web host, or the package id of an app referrer
    domain: str  # registrable domain, or the package id
    site: Optional[_Site]
    is_app: bool


def _clean_host(host: Any) -> Optional[str]:
    """Lower-case a host, dropping brackets, any port and a trailing dot."""
    if not isinstance(host, str):
        return None
    text = host.strip().lower()
    if text.startswith("["):  # IPv6 literal, possibly with a port
        text = text[1:].split("]", 1)[0]
    elif text.count(":") == 1:
        text = text.split(":", 1)[0]
    text = text.rstrip(".")
    if len(text) > 253 or text.count(".") > 126:  # not a DNS name (and matching it would cost O(labels^2))
        return None
    return text or None


def _strip_www(host: str) -> str:
    """Drop a leading ``www.`` (or ``www2.``...) unless nothing else would remain."""
    match = _WWW_PREFIX.match(host)
    if match and "." in host[match.end():]:
        return host[match.end():]
    return host


def _host_of(url: Any) -> Optional[str]:
    """Host of an http(s) URL or of a bare ``host/path``; None for anything else."""
    if not isinstance(url, str):
        return None
    text = url.strip()
    if text.startswith("//"):
        text = "http:" + text
    if "://" not in text:
        match = _BARE_HOST.fullmatch(text)
        return _clean_host(match.group(1)) if match else None
    try:
        parts = urlsplit(text)
        if parts.scheme.lower() not in ("http", "https"):
            return None
        return _clean_host(parts.hostname)
    except ValueError:
        return None


def _public_suffix_length(labels: Sequence[str]) -> int:
    """Number of trailing labels that form the public suffix (1 to 3)."""
    if len(labels) >= 3 and ".".join(labels[-3:]) in _PRIVATE_SUFFIXES:
        return 3
    if len(labels) >= 2:
        tld, second = labels[-1], labels[-2]
        if second + "." + tld in _PRIVATE_SUFFIXES:
            return 2
        if len(tld) == 2 and (
            second in _GENERIC_SECOND_LEVEL or second in _CCTLD_SECOND_LEVEL.get(tld, ())
        ):
            return 2
    return 1


def _match_host(host: str) -> Optional[_Site]:
    """The known site of a host; the most specific pattern wins."""
    clean = _strip_www(host)
    labels = clean.split(".")  # at most 127 (_clean_host refuses longer names), so this loop stays cheap
    brand = len(labels) - _public_suffix_length(labels) - 1 if len(labels) > 1 else -1
    for i in range(len(labels)):
        key = ".".join(labels[i:])
        if i == 0 and key in _HOST_EXACT:
            return _HOST_EXACT[key]
        if key in _DOMAIN_EXACT:
            return _DOMAIN_EXACT[key]
        if i <= brand:
            brand_key = ".".join(labels[i:brand + 1])
            if i == 0 and brand_key in _HOST_BRAND:
                return _HOST_BRAND[brand_key]
            if brand_key in _DOMAIN_BRAND:
                return _DOMAIN_BRAND[brand_key]
    return None


def _looks_like_webmail(host: str) -> bool:
    """``mail.example.com``, ``webmail.example.net``: an unknown webmail."""
    clean = _strip_www(host)
    first = clean.split(".", 1)[0]
    return first in _WEBMAIL_LABELS and registrable_domain(clean) != clean


def _parse_referrer(referrer: Any) -> Optional[_Referrer]:
    """Parse a web or app referrer; None when it carries no usable host."""
    if not isinstance(referrer, str):
        return None
    text = referrer.strip()
    lowered = text.lower()
    for scheme in _APP_SCHEMES:
        if lowered.startswith(scheme):
            return _parse_app_referrer(text[len(scheme):])
    host = _host_of(text)
    if host is None:
        return None
    return _Referrer(host, registrable_domain(host) or host, _match_host(host), False)


def _parse_app_referrer(rest: str) -> Optional[_Referrer]:
    """``android-app://<package>[/<scheme>/<host>/<path>]``, after the scheme."""
    segments = rest.lstrip("/").split("/")
    package = segments[0].split("?", 1)[0].split("#", 1)[0].strip().lower()
    if not package:
        return None
    site = _APPS.get(package)
    if site is None and len(segments) > 2 and segments[1].lower() in ("http", "https"):
        embedded = _clean_host(segments[2])
        site = _match_host(embedded) if embedded else None
    return _Referrer(package, package, site, True)


def _referrer_site_name(ref: Optional[_Referrer]) -> Optional[str]:
    """Name of the known site of a referrer, if any."""
    return ref.site.name if ref is not None and ref.site is not None else None


def _referrer_category(ref: _Referrer) -> Optional[str]:
    """Category of a parsed referrer."""
    if ref.site is not None:
        return ref.site.category
    if not ref.is_app and _looks_like_webmail(ref.host):
        return "email"
    return None


def _category_of(value: Any) -> Optional[str]:
    """Body of ``source_category()``."""
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text or text.startswith("("):
        return None
    if text in _NAMES:
        return _NAMES[text]
    app = _APPS.get(text)
    if app is not None:
        return app.category
    host = _host_of(text)
    if host is None:
        return None
    site = _match_host(host)
    if site is not None:
        return site.category
    return "email" if _looks_like_webmail(host) else None


# ---------------------------------------------------------------------------
# Reference data: public suffixes
# ---------------------------------------------------------------------------

# Second-level labels that form a public suffix under any two-letter country
# domain: co.uk, com.au, org.br, gob.mx, ac.jp...
_GENERIC_SECOND_LEVEL: FrozenSet[str] = frozenset(
    {"com", "net", "org", "edu", "gov", "mil", "ac", "co", "gob"}
)

# Further second-level suffixes, per country domain.
_CCTLD_SECOND_LEVEL: Dict[str, FrozenSet[str]] = {
    tld: frozenset(labels.split())
    for tld, labels in {
        "ar": "int tur",
        "at": "gv or",
        "au": "asn id",
        "br": "art blog eco emp eng ind inf jor log nom rec seg srv tur vet wiki",
        "es": "nom",
        "fr": "asso gouv nom presse tm",
        "hk": "idv",
        "id": "biz go my or sch web",
        "il": "muni",
        "in": "firm gen ind res",
        "jp": "ad ed go gr lg ne or",
        "ke": "go ne or sc",
        "kr": "es go hs kg ms ne or pe re sc",
        "nz": "cri geek gen govt health iwi kiwi maori parliament school",
        "pl": "biz info",
        "th": "go in or",
        "tr": "av bel biz gen info tv web",
        "tw": "idv",
        "tz": "go ne or sc",
        "ua": "in",
        "ug": "go ne or sc",
        "uk": "ltd me nhs plc police sch",
        "za": "web",
    }.items()
}

# Hosting platforms whose subdomains belong to different owners; most come from
# the private section of the Public Suffix List.
_PRIVATE_SUFFIXES: FrozenSet[str] = frozenset("""
    000webhostapp.com appspot.com azurestaticapps.net azurewebsites.net bitbucket.io
    blogspot.com carrd.co cloudfront.net deno.dev elasticbeanstalk.com firebaseapp.com
    fly.dev github.io gitlab.io glitch.me herokuapp.com myshopify.com neocities.org
    netlify.app ngrok-free.app ngrok.io notion.site onrender.com pages.dev
    pythonanywhere.com readthedocs.io repl.co s3.amazonaws.com streamlit.app surge.sh
    translate.goog vercel.app web.app webflow.io wixsite.com workers.dev
""".split())


# ---------------------------------------------------------------------------
# Reference data: known sites
# ---------------------------------------------------------------------------

# One site per line:  name  category  token...
#   category  search | social | video | shopping | email | ai | - (known, no category)
#   example.com    the domain and all its subdomains
#   =example.com   that host only ("www." is ignored)
#   brand.*        the brand under any public suffix (google.co.uk, amazon.de...);
#                  =brand.* for the bare host only, sub.brand.* for a subdomain
#   @alias         an extra source name (utm_source value) with the site's category
# The first host token is the canonical source domain (".*" reads ".com"). The
# site name is also a source name, so it must not be an everyday word: GA4 checks
# source lists before Email, and utm_source=target&utm_medium=email must stay
# Email (hence "target.com", "microsoft-teams"). The most specific token wins:
# news.google.* beats =google.*, mail.yandex.* beats yandex.*.
_SITES = """
# --- Search engines (GA4 SOURCE_CATEGORY_SEARCH, plus engines it lacks) ---
google            search    =google.* =images.google.* =scholar.google.* =maps.google.*
                            =books.google.* =video.google.* =encrypted.google.*
                            =ipv4.google.* =ipv6.google.* news.google.* lens.google.com
                            search.google.com
google-play       search    play.google.com
bing              search    bing.com
yahoo             search    =yahoo.* search.yahoo.*
baidu             search    baidu.com
yandex            search    yandex.* ya.ru
duckduckgo        search    duckduckgo.com
ecosia            search    ecosia.org
qwant             search    qwant.com
startpage         search    startpage.com startpage.nl ixquick.com
brave             search    search.brave.com
kagi              search    kagi.com
mojeek            search    mojeek.com mojeek.co.uk
swisscows         search    swisscows.com swisscows.ch
metager           search    metager.org metager.de
presearch         search    presearch.com presearch.org
yep.com           search    yep.com
naver             search    =naver.com =m.naver.com search.naver.com
daum              search    =daum.net =m.daum.net search.daum.net
seznam            search    =seznam.cz search.seznam.cz firmy.cz
sogou             search    sogou.com
so.com            search    so.com 360.cn haosou.com
shenma            search    sm.cn
coccoc            search    coccoc.com
petalsearch       search    petalsearch.com
aol               search    search.aol.* suche.aol.de recherche.aol.fr
ask               search    ask.com
dogpile           search    dogpile.com
lycos             search    lycos.com
excite            search    excite.com
webcrawler        search    webcrawler.com
info.com          search    info.com
msn               search    =msn.com ntp.msn.com
rambler           search    =rambler.ru nova.rambler.ru
go.mail.ru        search    go.mail.ru
onet              search    =onet.pl szukaj.onet.pl
centrum.cz        search    =centrum.cz search.centrum.cz
startsiden        search    startsiden.no
kvasir            search    kvasir.no
najdi             search    najdi.si
exalead           search    exalead.com
ukr               search    search.ukr.net secureurl.ukr.net
tut.by            search    =tut.by
docomo            search    search.smt.docomo.ne.jp
auone             search    search.auone.jp
biglobe           search    =biglobe.ne.jp =biglobe.co.jp search.biglobe.ne.jp
rakuten           search    =rakuten.co.jp websearch.rakuten.co.jp

# --- AI assistants (GA4 sends them to its AI Assistant channel; its list is unpublished) ---
chatgpt           ai        chatgpt.com chat.openai.com searchgpt.com @openai
perplexity        ai        perplexity.ai perplexity.com pplx.ai
claude            ai        claude.ai claude.com
gemini            ai        gemini.google.com bard.google.com aistudio.google.com gemini.google
notebooklm        ai        notebooklm.google.com notebooklm.google
copilot           ai        copilot.microsoft.com copilot.cloud.microsoft edgeservices.bing.com
                            sydney.bing.com
deepseek          ai        deepseek.com
grok              ai        grok.com
meta-ai           ai        meta.ai
mistral           ai        chat.mistral.ai @lechat
you.com           ai        you.com
phind             ai        phind.com
poe.com           ai        poe.com
character.ai      ai        character.ai
pi.ai             ai        pi.ai
kimi              ai        kimi.com kimi.ai kimi.moonshot.cn
qwen              ai        chat.qwen.ai qwen.ai tongyi.com tongyi.aliyun.com
                            qianwen.aliyun.com @tongyi
ernie-bot         ai        yiyan.baidu.com @yiyan
doubao            ai        doubao.com
yuanbao           ai        yuanbao.tencent.com
chatglm           ai        chatglm.cn chat.z.ai
duck.ai           ai        duck.ai
genspark          ai        genspark.ai
felo.ai           ai        felo.ai
iask.ai           ai        iask.ai
andisearch        ai        andisearch.com
consensus.app     ai        consensus.app
elicit.com        ai        elicit.com elicit.org
openrouter        ai        openrouter.ai
lmarena           ai        lmarena.ai
monica.im         ai        monica.im
manus.im          ai        manus.im
blackbox.ai       ai        blackbox.ai
t3.chat           ai        t3.chat

# --- Social networks, messaging and communities (GA4 SOURCE_CATEGORY_SOCIAL, plus newer ones) ---
facebook          social    facebook.com fb.com fb.me fb.watch @meta
messenger         social    messenger.com m.me
instagram         social    instagram.com instagr.am ig.me
threads           social    threads.com threads.net
whatsapp          social    whatsapp.com wa.me l.wl.co
twitter           social    x.com twitter.com t.co @x
linkedin          social    linkedin.com lnkd.in
pinterest         social    pinterest.* pin.it
reddit            social    reddit.com redd.it reddit.app.link
tiktok            social    tiktok.com
douyin            social    douyin.com
snapchat          social    snapchat.com
tumblr            social    =tumblr.com t.umblr.com
quora             social    quora.com
discord           social    discord.com discordapp.com discord.gg
slack             social    slack.com slack-redir.net
microsoft-teams   social    teams.microsoft.com teams.live.com teams.cloud.microsoft
telegram          social    telegram.org t.me telegram.me
signal-messenger  social    signal.org signal.me
viber             social    viber.com
line              social    line.me lin.ee
kakao             social    kakao.com kakaocorp.com
wechat            social    wechat.com weixin.qq.com @weixin
qzone             social    qzone.qq.com
weibo             social    weibo.com weibo.cn t.cn
douban            social    douban.com
zhihu             social    zhihu.com
xiaohongshu       social    xiaohongshu.com xhslink.com @rednote
vk                social    vk.com vk.ru vk.me vkontakte.ru @vkontakte
ok.ru             social    ok.ru odnoklassniki.ru odnoklassniki.ua @odnoklassniki
dzen              social    dzen.ru
mastodon          social    mastodon.social mastodon.online mastodon.world mstdn.social mas.to
                            fosstodon.org hachyderm.io infosec.exchange techhub.social
                            universeodon.com mstdn.jp pawoo.net joinmastodon.org
bluesky           social    bsky.app @bsky
lemmy             social    lemmy.world lemmy.ml
nextdoor          social    nextdoor.*
hackernews        social    news.ycombinator.com
lobsters          social    lobste.rs
producthunt       social    producthunt.com
medium            social    =medium.com link.medium.com
substack          social    =substack.com open.substack.com
flipboard         social    flipboard.com flip.it
yelp              social    yelp.*
tripadvisor       social    tripadvisor.*
trustpilot        social    trustpilot.*
glassdoor         social    glassdoor.*
skype             social    skype.com
hootsuite         social    hootsuite.com ow.ly
buffer            social    buffer.com buff.ly
dlvr.it           social    dlvr.it
linktree          social    linktr.ee
feedly            social    feedly.com
inoreader         social    inoreader.com
behance           social    behance.net
dribbble          social    dribbble.com
google-messages   social    messages.google.com
google-chat       social    chat.google.com
google-groups     social    groups.google.com googlegroups.com
google-sites      social    sites.google.com
google-plus       social    plus.google.com plus.url.google.com
samsung-messages  social

# --- Video platforms (GA4 SOURCE_CATEGORY_VIDEO, plus newer ones) ---
youtube           video     youtube.com youtu.be youtube-nocookie.com
vimeo             video     vimeo.com
dailymotion       video     dailymotion.com dai.ly
twitch            video     twitch.tv
bilibili          video     bilibili.com bilibili.tv b23.tv
rumble            video     rumble.com
odysee            video     odysee.com
kick.com          video     kick.com
loom              video     loom.com
wistia            video     wistia.com wistia.net
vidyard           video     vidyard.com
ted               video     ted.com
netflix           video     netflix.com
hulu              video     hulu.com
disneyplus        video     disneyplus.com
primevideo        video     primevideo.com
iqiyi             video     iqiyi.com iq.com
youku             video     youku.com

# --- Shopping (GA4 SOURCE_CATEGORY_SHOPPING, plus major marketplaces) ---
amazon            shopping  amazon.* amzn.to amzn.eu amzn.asia a.co amazon-adsystem.com
aws               -         aws.amazon.com
amazon-seller     -         sellercentral.amazon.* sellercentral-europe.amazon.com
amazon-developer  -         developer.amazon.com
amazon-ads        -         advertising.amazon.com
ebay              shopping  ebay.*
etsy              shopping  etsy.com etsy.me
aliexpress        shopping  aliexpress.com aliexpress.us aliexpress.ru
alibaba           shopping  alibaba.com
walmart           shopping  walmart.*
target.com        shopping  target.com
bestbuy           shopping  bestbuy.com bestbuy.ca
temu              shopping  temu.com
shein             shopping  shein.com
mercadolibre      shopping  mercadolibre.* mercadolivre.com.br
shopify           shopping  shopify.com shop.app
stripe            shopping  stripe.com
google-shopping   shopping  shopping.google.*
naver-shopping    shopping  shopping.naver.com
yahoo-shopping    shopping  shopping.yahoo.*
yandex-market     shopping  market.yandex.*
idealo            shopping  idealo.*
pricerunner       shopping  pricerunner.*
prisjakt          shopping  prisjakt.nu pricespy.co.uk pricespy.co.nz
kelkoo            shopping  kelkoo.*
zalando           shopping  zalando.*
otto.de           shopping  otto.de
allegro.pl        shopping  allegro.pl
bol.com           shopping  bol.com
cdiscount         shopping  cdiscount.com
flipkart          shopping  flipkart.com
taobao            shopping  taobao.com tmall.com
jd.com            shopping  jd.com
coupang           shopping  coupang.com
mercari           shopping  mercari.com
shopee            shopping  shopee.*
lazada            shopping  lazada.*
trendyol          shopping  trendyol.com
ozon              shopping  ozon.ru
wildberries       shopping  wildberries.ru

# --- Webmail and email tools (no GA4 list: GA4 counts webmail visits as Referral) ---
gmail             email     mail.google.com inbox.google.com
outlook           email     outlook.live.com outlook.office.com outlook.office365.com
                            outlook.cloud.microsoft =outlook.com mail.live.com
                            safelinks.protection.outlook.com @hotmail
yahoo-mail        email     mail.yahoo.* @ymail
aol-mail          email     mail.aol.com
proton-mail       email     mail.proton.me mail.protonmail.com @protonmail
zoho-mail         email     mail.zoho.*
icloud-mail       email     =icloud.com
yandex-mail       email     mail.yandex.*
mailru-mail       email     e.mail.ru
rambler-mail      email     mail.rambler.ru
seznam-email      email     email.seznam.cz
gmx               email     gmx.* deref-gmx.net deref-gmx.com
webde-mail        email     navigator.web.de 3c.web.de deref-web.de deref-web-02.de
mail.com          email     mail.com deref-mail.com
libero-mail       email     mail.libero.it
naver-mail        email     mail.naver.com
daum-mail         email     mail.daum.net
qq-mail           email     mail.qq.com wx.mail.qq.com exmail.qq.com
netease-mail      email     mail.163.com mail.126.com mail.yeah.net
sina-mail         email     mail.sina.com.cn
tuta              email     tuta.com tutanota.com
fastmail          email     fastmail.com
hey-mail          email     app.hey.com
superhuman-mail   email     mail.superhuman.com
mailchimp         email     mailchi.mp campaign-archive.com
samsung-email     email
k9-mail           email
edison-mail       email
spark-mail        email
newsletter        email     @email @e-mail @e_mail @klaviyo @sendgrid @brevo @sendinblue
                            @mailerlite @convertkit @campaignmonitor @constantcontact
                            @activecampaign @beehiiv
"""

# Android referrers (android-app://<package>/...): package id, then site name.
_ANDROID_APPS = """
com.google.android.googlequicksearchbox         google
com.google.android.apps.searchlite              google
com.google.android.apps.magazines               google
com.google.android.apps.maps                    google
com.android.vending                             google-play
com.microsoft.bing                              bing
com.baidu.searchbox                             baidu
ru.yandex.searchplugin                          yandex
com.nhn.android.search                          naver
net.daum.android.daum                           daum
com.duckduckgo.mobile.android                   duckduckgo
com.ecosia.android                              ecosia
com.google.android.apps.bard                    gemini
com.google.android.apps.labs.language.tailwind  notebooklm
com.openai.chatgpt                              chatgpt
ai.perplexity.app.android                       perplexity
com.anthropic.claude                            claude
com.microsoft.copilot                           copilot
com.deepseek.chat                               deepseek
ai.x.grok                                       grok
com.facebook.katana                             facebook
com.facebook.lite                               facebook
com.facebook.orca                               messenger
com.facebook.mlite                              messenger
com.instagram.android                           instagram
com.instagram.barcelona                         threads
com.whatsapp                                    whatsapp
com.whatsapp.w4b                                whatsapp
com.twitter.android                             twitter
com.linkedin.android                            linkedin
com.pinterest                                   pinterest
com.reddit.frontpage                            reddit
com.zhiliaoapp.musically                        tiktok
com.ss.android.ugc.trill                        tiktok
com.ss.android.ugc.aweme                        douyin
com.snapchat.android                            snapchat
org.telegram.messenger                          telegram
org.telegram.messenger.web                      telegram
org.thunderdog.challegram                       telegram
com.slack                                       slack
com.discord                                     discord
com.microsoft.teams                             microsoft-teams
com.viber.voip                                  viber
jp.naver.line.android                           line
com.kakao.talk                                  kakao
com.tencent.mm                                  wechat
com.sina.weibo                                  weibo
com.zhihu.android                               zhihu
com.xingin.xhs                                  xiaohongshu
com.vkontakte.android                           vk
ru.ok.android                                   ok.ru
com.quora.android                               quora
com.tumblr                                      tumblr
xyz.blueskyweb.app                              bluesky
org.joinmastodon.android                        mastodon
com.medium.reader                               medium
flipboard.app                                   flipboard
com.nextdoor                                    nextdoor
com.google.android.apps.messaging               google-messages
com.samsung.android.messaging                   samsung-messages
com.google.android.apps.dynamite                google-chat
com.google.android.youtube                      youtube
com.google.android.apps.youtube.music           youtube
com.google.android.apps.youtube.kids            youtube
tv.twitch.android.app                           twitch
com.vimeo.android.videoapp                      vimeo
tv.danmaku.bili                                 bilibili
com.amazon.mshop.android.shopping               amazon
com.ebay.mobile                                 ebay
com.etsy.android                                etsy
com.alibaba.aliexpresshd                        aliexpress
com.einnovation.temu                            temu
com.zzkko                                       shein
com.google.android.gm                           gmail
com.google.android.gm.lite                      gmail
com.microsoft.office.outlook                    outlook
com.yahoo.mobile.client.android.mail            yahoo-mail
ch.protonmail.android                           proton-mail
com.zoho.mail                                   zoho-mail
ru.yandex.mail                                  yandex-mail
ru.mail.mailapp                                 mailru-mail
de.tutao.tutanota                               tuta
com.samsung.android.email.provider              samsung-email
com.fsck.k9                                     k9-mail
com.easilydo.mail                               edison-mail
com.readdle.spark                               spark-mail
"""

# Google's source-category list for the GA4 default channel group, verbatim
# ("GA4 Source Categories", linked from Analytics Help article 9756891; the
# version Google still links to in 2026 dates from February 2023). Entries
# without a dot are source names, the others hosts. Entries not covered by
# _SITES become host-only sites of their own.
_GA4_SOURCE_CATEGORIES: Dict[str, str] = {
    "search": """
        360.cn, alice, aol, ar.search.yahoo.com, ask, at.search.yahoo.com, au.search.yahoo.com,
        auone, avg, babylon, baidu, biglobe, biglobe.co.jp, biglobe.ne.jp, bing,
        br.search.yahoo.com, ca.search.yahoo.com, centrum.cz, ch.search.yahoo.com,
        cl.search.yahoo.com, cn.bing.com, cnn, co.search.yahoo.com, comcast, conduit, daum,
        daum.net, de.search.yahoo.com, dk.search.yahoo.com, dogpile, dogpile.com, duckduckgo,
        ecosia.org, email.seznam.cz, eniro, es.search.yahoo.com, espanol.search.yahoo.com,
        exalead.com, excite.com, fi.search.yahoo.com, firmy.cz, fr.search.yahoo.com, globo,
        go.mail.ru, google, google-play, hk.search.yahoo.com, id.search.yahoo.com,
        in.search.yahoo.com, incredimail, it.search.yahoo.com, kvasir, lens.google.com,
        lite.qwant.com, lycos, m.baidu.com, m.naver.com, m.search.naver.com, m.sogou.com,
        mail.rambler.ru, mail.yandex.ru, malaysia.search.yahoo.com, msn, msn.com,
        mx.search.yahoo.com, najdi, naver, naver.com, news.google.com, nl.search.yahoo.com,
        no.search.yahoo.com, ntp.msn.com, nz.search.yahoo.com, onet, onet.pl, pe.search.yahoo.com,
        ph.search.yahoo.com, pl.search.yahoo.com, play.google.com, qwant, qwant.com, rakuten,
        rakuten.co.jp, rambler, rambler.ru, se.search.yahoo.com, search-results, search.aol.co.uk,
        search.aol.com, search.google.com, search.smt.docomo.ne.jp, search.ukr.net,
        secureurl.ukr.net, seznam, seznam.cz, sg.search.yahoo.com, so.com, sogou, sogou.com,
        sp-web.search.auone.jp, startsiden, startsiden.no, suche.aol.de, terra, th.search.yahoo.com,
        tr.search.yahoo.com, tut.by, tw.search.yahoo.com, uk.search.yahoo.com, ukr,
        us.search.yahoo.com, virgilio, vn.search.yahoo.com, wap.sogou.com, webmaster.yandex.ru,
        websearch.rakuten.co.jp, yahoo, yahoo.co.jp, yahoo.com, yandex, yandex.by, yandex.com,
        yandex.com.tr, yandex.fr, yandex.kz, yandex.ru, yandex.ua, yandex.uz, zen.yandex.ru
    """,
    "social": """
        43things, 43things.com, 51.com, 5ch.net, Hatena, ImageShack, academia.edu, activerain,
        activerain.com, activeworlds, activeworlds.com, addthis, addthis.com, airg.ca,
        allnurses.com, allrecipes.com, alumniclass, alumniclass.com, ameba.jp, ameblo.jp,
        americantowns, americantowns.com, amp.reddit.com, ancestry.com, anobii, anobii.com,
        answerbag, answerbag.com, answers.yahoo.com, aolanswers, aolanswers.com, apps.facebook.com,
        ar.pinterest.com, artstation.com, askubuntu, askubuntu.com, asmallworld.com, athlinks,
        athlinks.com, away.vk.com, awe.sm, b.hatena.ne.jp, baby-gaga, baby-gaga.com, babyblog.ru,
        badoo, badoo.com, bebo, bebo.com, beforeitsnews, beforeitsnews.com, bharatstudent,
        bharatstudent.com, biip.no, biswap.org, bit.ly, blackcareernetwork.com, blackplanet,
        blackplanet.com, blip.fm, blog.com, blog.feedspot.com, blog.goo.ne.jp, blog.naver.com,
        blog.yahoo.co.jp, blogg.no, bloggang.com, blogger, blogger.com, blogher, blogher.com,
        bloglines, bloglines.com, blogs.com, blogsome, blogsome.com, blogspot, blogspot.com,
        blogster, blogster.com, blurtit, blurtit.com, bookmarks.yahoo.co.jp, bookmarks.yahoo.com,
        br.pinterest.com, brightkite, brightkite.com, brizzly, brizzly.com, business.facebook.com,
        buzzfeed, buzzfeed.com, buzznet, buzznet.com, cafe.naver.com, cafemom, cafemom.com,
        camospace, camospace.com, canalblog.com, care.com, care2, care2.com, caringbridge.org,
        catster, catster.com, cbnt.io, cellufun, cellufun.com, centerblog.net, chat.zalo.me,
        chegg.com, chicagonow, chicagonow.com, chiebukuro.yahoo.co.jp, classmates, classmates.com,
        classquest, classquest.com, co.pinterest.com, cocolog-nifty, cocolog-nifty.com,
        copainsdavant.linternaute.com, couchsurfing.org, cozycot, cozycot.com, cross.tv,
        crunchyroll, crunchyroll.com, cyworld, cyworld.com, cz.pinterest.com, d.hatena.ne.jp,
        dailystrength.org, deluxe.com, deviantart, deviantart.com, dianping, dianping.com, digg,
        digg.com, diigo, diigo.com, discover.hubpages.com, disqus, disqus.com, dogster, dogster.com,
        dol2day, dol2day.com, doostang, doostang.com, dopplr, dopplr.com, douban, douban.com,
        draft.blogger.com, draugiem.lv, drugs-forum, drugs-forum.com, dzone, dzone.com,
        edublogs.org, elftown, elftown.com, epicurious.com, everforo.com, exblog.jp, extole,
        extole.com, facebook, facebook.com, faceparty, faceparty.com, fandom.com, fanpop,
        fanpop.com, fark, fark.com, fb, fb.me, fc2, fc2.com, feedspot, feministing, feministing.com,
        filmaffinity, filmaffinity.com, flickr, flickr.com, flipboard, flipboard.com, folkdirect,
        folkdirect.com, foodservice, foodservice.com, forums.androidcentral.com,
        forums.crackberry.com, forums.imore.com, forums.nexopia.com, forums.webosnation.com,
        forums.wpcentral.com, fotki, fotki.com, fotolog, fotolog.com, foursquare, foursquare.com,
        free.facebook.com, friendfeed, friendfeed.com, fruehstueckstreff.org, fubar, fubar.com,
        gaiaonline, gaiaonline.com, gamerdna, gamerdna.com, gather.com, geni.com, getpocket.com,
        glassboard, glassboard.com, glassdoor, glassdoor.com, godtube, godtube.com, goldenline.pl,
        goldstar, goldstar.com, goo.gl, gooblog, goodreads, goodreads.com, google+,
        googlegroups.com, googleplus, govloop, govloop.com, gowalla, gowalla.com, gree.jp,
        groups.google.com, gulli.com, gutefrage.net, habbo, habbo.com, hi5, hi5.com, hootsuite,
        hootsuite.com, houzz, houzz.com, hoverspot, hoverspot.com, hr.com, hu.pinterest.com,
        hubculture, hubculture.com, hubpages.com, hyves.net, hyves.nl, ibibo, ibibo.com,
        id.pinterest.com, identi.ca, ig, imageshack.com, imageshack.us, imvu, imvu.com,
        in.pinterest.com, insanejournal, insanejournal.com, instagram, instagram.com, instapaper,
        instapaper.com, internations.org, interpals.net, intherooms, intherooms.com,
        irc-galleria.net, is.gd, italki, italki.com, jammerdirect, jammerdirect.com, jappy.com,
        jappy.de, kaboodle.com, kakao, kakao.com, kakaocorp.com, kaneva, kaneva.com, kin.naver.com,
        l.facebook.com, l.instagram.com, l.messenger.com, last.fm, librarything, librarything.com,
        lifestream.aol.com, line, line.me, linkedin, linkedin.com, listal, listal.com, listography,
        listography.com, livedoor.com, livedoorblog, livejournal, livejournal.com, lm.facebook.com,
        lnkd.in, m.blog.naver.com, m.cafe.naver.com, m.facebook.com, m.kin.naver.com, m.vk.com,
        m.yelp.com, mbga.jp, medium.com, meetin.org, meetup, meetup.com, meinvz.net, meneame.net,
        menuism.com, messages.google.com, messages.yahoo.co.jp, messenger, messenger.com, mix.com,
        mixi.jp, mobile.facebook.com, mocospace, mocospace.com, mouthshut, mouthshut.com,
        movabletype, movabletype.com, mubi, mubi.com, my.opera.com, myanimelist.net, myheritage,
        myheritage.com, mylife, mylife.com, mymodernmet, mymodernmet.com, myspace, myspace.com,
        netvibes, netvibes.com, news.ycombinator.com, newsshowcase, nexopia, ngopost.org, niconico,
        nicovideo.jp, nightlifelink, nightlifelink.com, ning, ning.com, nl.pinterest.com,
        odnoklassniki.ru, odnoklassniki.ua, okwave.jp, old.reddit.com, oneworldgroup.org,
        onstartups, onstartups.com, opendiary, opendiary.com, oshiete.goo.ne.jp, out.reddit.com,
        over-blog.com, overblog.com, paper.li, partyflock.nl, photobucket, photobucket.com,
        pinboard, pinboard.in, pingsta, pingsta.com, pinterest, pinterest.at, pinterest.ca,
        pinterest.ch, pinterest.cl, pinterest.co.kr, pinterest.co.uk, pinterest.com,
        pinterest.com.au, pinterest.com.mx, pinterest.de, pinterest.es, pinterest.fr, pinterest.it,
        pinterest.jp, pinterest.nz, pinterest.ph, pinterest.pt, pinterest.ru, pinterest.se,
        pixiv.net, pl.pinterest.com, playahead.se, plurk, plurk.com, plus.google.com,
        plus.url.google.com, pocket.co, posterous, posterous.com, pro.homeadvisor.com,
        pulse.yahoo.com, qapacity, qapacity.com, quechup, quechup.com, quora, quora.com,
        qzone.qq.com, ravelry, ravelry.com, reddit, reddit.com, redux, redux.com, renren,
        renren.com, researchgate.net, reunion, reunion.com, reverbnation, reverbnation.com, rtl.de,
        ryze, ryze.com, salespider, salespider.com, scoop.it, screenrant, screenrant.com, scribd,
        scribd.com, scvngr, scvngr.com, secondlife, secondlife.com, serverfault, serverfault.com,
        shareit, sharethis, sharethis.com, shvoong.com, sites.google.com, skype, skyrock,
        skyrock.com, slashdot.org, slideshare.net, smartnews.com, snapchat, snapchat.com, social,
        sociallife.com.br, socialvibe, socialvibe.com, spaces.live.com, spoke, spoke.com, spruz,
        spruz.com, ssense.com, stackapps, stackapps.com, stackexchange, stackexchange.com,
        stackoverflow, stackoverflow.com, stardoll.com, stickam, stickam.com, studivz.net,
        suomi24.fi, superuser, superuser.com, sweeva, sweeva.com, t.co, t.me, tagged, tagged.com,
        taggedmail, taggedmail.com, talkbiznow, talkbiznow.com, taringa.net, techmeme, techmeme.com,
        tencent, tencent.com, tiktok, tiktok.com, tinyurl, tinyurl.com, toolbox, toolbox.com,
        touch.facebook.com, tr.pinterest.com, travellerspoint, travellerspoint.com, tripadvisor,
        tripadvisor.com, trombi, trombi.com, trustpilot, tudou, tudou.com, tuenti, tuenti.com,
        tumblr, tumblr.com, tweetdeck, tweetdeck.com, twitter, twitter.com, twoo.com, typepad,
        typepad.com, unblog.fr, urbanspoon.com, ushareit.com, ushi.cn, vampirefreaks,
        vampirefreaks.com, vampirerave, vampirerave.com, vg.no, video.ibm.com, vk.com, vkontakte.ru,
        wakoopa, wakoopa.com, wattpad, wattpad.com, web.facebook.com, web.skype.com, webshots,
        webshots.com, wechat, wechat.com, weebly, weebly.com, weibo, weibo.com, wer-weiss-was.de,
        weread, weread.com, whatsapp, whatsapp.com, wiki.answers.com, wikihow.com, wikitravel.org,
        woot.com, wordpress, wordpress.com, wordpress.org, xanga, xanga.com, xing, xing.com,
        yahoo-mbga.jp, yammer, yammer.com, yelp, yelp.co.uk, yelp.com, youroom.in, za.pinterest.com,
        zalo, zoo.gr, zooppa, zooppa.com
    """,
    "video": """
        blog.twitch.tv, crackle, crackle.com, curiositystream, curiositystream.com, d.tube,
        dailymotion, dailymotion.com, dashboard.twitch.tv, disneyplus, disneyplus.com,
        fast.wistia.net, help.hulu.com, help.netflix.com, hulu, hulu.com, id.twitch.tv, iq.com,
        iqiyi, iqiyi.com, jobs.netflix.com, justin.tv, m.twitch.tv, m.youtube.com,
        music.youtube.com, netflix, netflix.com, player.twitch.tv, player.vimeo.com, ted, ted.com,
        twitch, twitch.tv, utreon, utreon.com, veoh, veoh.com, viadeo.journaldunet.com, vimeo,
        vimeo.com, wistia, wistia.com, youku, youku.com, youtube, youtube.com
    """,
    "shopping": """
        Google Shopping, IGShopping, aax-us-east.amazon-adsystem.com, aax.amazon-adsystem.com,
        alibaba, alibaba.com, amazon, amazon.co.uk, amazon.com, apps.shopify.com,
        checkout.shopify.com, checkout.stripe.com, cr.shopping.naver.com, cr2.shopping.naver.com,
        ebay, ebay.co.uk, ebay.com, ebay.com.au, ebay.de, etsy, etsy.com, m.alibaba.com,
        m.shopping.naver.com, mercadolibre, mercadolibre.com, mercadolibre.com.ar,
        mercadolibre.com.mx, message.alibaba.com, msearch.shopping.naver.com, nl.shopping.net,
        no.shopping.net, offer.alibaba.com, one.walmart.com, order.shopping.yahoo.co.jp,
        partners.shopify.com, s3.amazonaws.com, se.shopping.net, shop.app, shopify, shopify.com,
        shopping.naver.com, shopping.yahoo.co.jp, shopping.yahoo.com, shopzilla, shopzilla.com,
        simplycodes.com, store.shopping.yahoo.co.jp, stripe, stripe.com, uk.shopping.net, walmart,
        walmart.com
    """,
}

# Entries of Google's list filed under another category on purpose: webmail
# hosts that GA4 counts as search engines. A click in an inbox is not a search.
_GA4_DEVIATIONS: Dict[str, str] = {
    "email.seznam.cz": "email",
    "mail.rambler.ru": "email",
    "mail.yandex.ru": "email",
}


# ---------------------------------------------------------------------------
# Registries, built once at import time
# ---------------------------------------------------------------------------

_HOST_EXACT: Dict[str, _Site] = {}  # =example.com
_DOMAIN_EXACT: Dict[str, _Site] = {}  # example.com and its subdomains
_HOST_BRAND: Dict[str, _Site] = {}  # =brand.*
_DOMAIN_BRAND: Dict[str, _Site] = {}  # brand.* and sub.brand.*
_NAMES: Dict[str, Optional[str]] = {}  # source name -> category
_SITES_BY_NAME: Dict[str, _Site] = {}
_APPS: Dict[str, _Site] = {}  # Android package id -> site
_CONFLICTS: List[str] = []  # clashing registrations; the tests keep this empty


def _table_lines(table: str) -> List[List[str]]:
    """Tokens of a whitespace table; indented lines continue the previous row."""
    rows: List[List[str]] = []
    for raw in table.splitlines():
        line = raw.split("#", 1)[0]
        if not line.strip():
            continue
        if line[0].isspace() and rows:
            rows[-1].extend(line.split())
        else:
            rows.append(line.split())
    return rows


def _add_name(name: str, category: Optional[str]) -> None:
    """Register a source name, keeping the first category it was given."""
    key = name.lower()
    if key in _NAMES and _NAMES[key] != category:
        _CONFLICTS.append(f"name {key!r}: {_NAMES[key]} vs {category}")
        return
    _NAMES[key] = category


def _add_pattern(token: str, site: _Site) -> None:
    """Register one host token of the site table."""
    host_only = token.startswith("=")
    pattern = token.lstrip("=").lower()
    if pattern.endswith(".*"):
        key, table = pattern[:-2], (_HOST_BRAND if host_only else _DOMAIN_BRAND)
    else:
        key, table = pattern, (_HOST_EXACT if host_only else _DOMAIN_EXACT)
    if key in table and table[key] != site:
        _CONFLICTS.append(f"host {token!r}: {table[key].name} vs {site.name}")
        return
    table[key] = site


def _canonical(token: str) -> str:
    """Canonical source domain of a host token: ``=google.*`` -> ``google.com``."""
    pattern = token.lstrip("=").lower()
    return pattern[:-2] + ".com" if pattern.endswith(".*") else pattern


def _load_sites() -> None:
    """Register the curated site table."""
    for name, category, *tokens in _table_lines(_SITES):
        hosts = [token for token in tokens if not token.startswith("@")]
        site = _Site(name, None if category == "-" else category,
                     _canonical(hosts[0]) if hosts else None)
        if name in _SITES_BY_NAME:
            _CONFLICTS.append(f"site {name!r} defined twice")
        _SITES_BY_NAME[name] = site
        _add_name(name, site.category)
        for token in tokens:
            if token.startswith("@"):
                _add_name(token[1:], site.category)
            else:
                _add_pattern(token, site)


def _load_ga4_lists() -> None:
    """Register Google's lists: names as-is, uncovered hosts as host-only sites."""
    for category, block in _GA4_SOURCE_CATEGORIES.items():
        for entry in block.split(","):
            key = entry.strip().lower()
            if not key:
                continue
            if "." not in key:
                _NAMES.setdefault(key, category)
            elif _match_host(key) is None:
                host = _strip_www(key)
                _HOST_EXACT[host] = _Site(host, category, host)


def _load_apps() -> None:
    """Register the Android package table."""
    for package, name in _table_lines(_ANDROID_APPS):
        site = _SITES_BY_NAME.get(name)
        if site is None:
            _CONFLICTS.append(f"app {package!r}: unknown site {name!r}")
            continue
        _APPS[package.lower()] = site


_load_sites()
_load_ga4_lists()
_load_apps()
