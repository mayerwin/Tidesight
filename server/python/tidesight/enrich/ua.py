"""User-Agent and Client Hints parsing for Tidesight enrichment.

:func:`parse` turns a raw ``User-Agent`` string, plus optional User-Agent
Client Hints, into browser, OS and device fields::

    browser, browser_version, browser_major, engine, os, os_version,
    device_type, device_brand, device_model, is_webview, in_app,
    is_bot_ua, bot_name, parser, parser_version

Unknown values are None. Naming follows GA4/Plausible: one family per
browser across platforms ("Chrome" covers desktop, Android and iOS;
"Safari" covers macOS and iOS), plus "Android WebView" and
"Safari WebView" for embedded browsers. ``device_type`` is one of
:data:`DEVICE_TYPES`; ``browser_major`` is an int.

Client Hints come from two places: request headers (``sec-ch-ua*``, raw
structured-header values, lowercase names) and the tracker's copy of
``navigator.userAgentData`` (``js_hints``). JS hints are high-entropy and
win when both exist. They fix what the frozen, "reduced" User-Agent hides:
Windows 11, the real macOS and Android versions, Android device models,
and Chromium browsers that look like Chrome (Brave, Opera GX...).

Two back ends exist. The built-in regex parser is always available. When
the optional ``ua_parser`` package (uap-python) is importable and the
``TIDESIGHT_PURE_UA`` environment variable is not set (or is 0/false/off),
uap provides the browser and OS families and versions instead. Device
brand/model keep the built-in spelling (uap only fills a missing brand),
and the same refinements (webviews, in-app browsers, TVs and consoles,
frozen versions, Client Hints, bots, device type) apply in both cases.
``parser_version`` identifies the back end and its version (library and
regex data), so an upgrade triggers a recompute. Bot detection reuses
:func:`tidesight.enrich.bots.match_ua`.
"""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import Any, Callable, Dict, List, NamedTuple, Optional, Tuple

from . import bots

__all__ = ["UA_VERSION", "DEVICE_TYPES", "parse", "current_parser_version", "clear_cache"]

UA_VERSION = 1  # bump when parsing output changes

DEVICE_TYPES = ("desktop", "mobile", "tablet", "tv", "console", "wearable", "car", "bot", "unknown")

_MAX_UA_LENGTH = 2048
_PURE_ENV = "TIDESIGHT_PURE_UA"


# Client Hints -------------------------------------------------------------------


class ClientHints(NamedTuple):
    """Normalised Client Hints (hashable, so parse results can be cached)."""

    brands: Tuple[Tuple[str, str], ...] = ()
    full_versions: Tuple[Tuple[str, str], ...] = ()
    mobile: Optional[bool] = None
    platform: Optional[str] = None
    platform_version: Optional[str] = None
    model: Optional[str] = None
    form_factors: Tuple[str, ...] = ()
    ua_full_version: Optional[str] = None
    max_touch_points: Optional[int] = None


_NO_HINTS = ClientHints()


def _sf_bare_item(value: str, i: int) -> Tuple[str, int]:
    """Read one structured-field item (quoted string or token) at ``i``."""
    n = len(value)
    while i < n and value[i] in " \t":
        i += 1
    if i < n and value[i] == '"':
        i += 1
        out: List[str] = []
        while i < n:
            ch = value[i]
            if ch == "\\" and i + 1 < n:
                out.append(value[i + 1])
                i += 2
                continue
            i += 1
            if ch == '"':
                break
            out.append(ch)
        return "".join(out), i
    start = i
    while i < n and value[i] not in ";,":
        i += 1
    return value[start:i].strip(), i


def _sf_list(value: Any) -> List[Tuple[str, Dict[str, str]]]:
    """Parse an RFC 8941 list such as ``"Chromium";v="128", "Not;A=Brand";v="24"``.

    Lenient: malformed input yields whatever items could be read. Quoted
    strings may contain ``;``, ``=`` and ``,`` (GREASE brands do).
    """
    if not isinstance(value, str):
        return []
    items: List[Tuple[str, Dict[str, str]]] = []
    i, n = 0, len(value)
    while i < n:
        while i < n and value[i] in " \t,":
            i += 1
        if i >= n:
            break
        item, i = _sf_bare_item(value, i)
        params: Dict[str, str] = {}
        while i < n and value[i] in " \t":
            i += 1
        while i < n and value[i] == ";":
            key_start = i + 1
            i = key_start
            while i < n and value[i] not in "=;,":
                i += 1
            key = value[key_start:i].strip().lower()
            param = ""
            if i < n and value[i] == "=":
                param, i = _sf_bare_item(value, i + 1)
            if key:
                params[key] = param
            while i < n and value[i] in " \t":
                i += 1
        while i < n and value[i] != ",":
            i += 1
        items.append((item, params))
    return items


def _sf_string(value: Any) -> Optional[str]:
    """A single structured-field string (``"Windows"``), quotes optional."""
    if not isinstance(value, str):
        return None
    items = _sf_list(value)
    text = items[0][0].strip() if items else ""
    return text or None


def _sf_bool(value: Any) -> Optional[bool]:
    """A structured-field boolean (``?1`` / ``?0``), leniently."""
    if not isinstance(value, str):
        return None
    text = value.strip().strip('"').lower()
    if text in ("?1", "1", "true"):
        return True
    if text in ("?0", "0", "false"):
        return False
    return None


def _clean_text(value: Any, limit: int = 200) -> Optional[str]:
    """A stripped, bounded string, or None for empty/non-string values."""
    if not isinstance(value, str):
        return None
    text = value.strip()[:limit]
    return text or None


def _header_brands(value: Any) -> Tuple[Tuple[str, str], ...]:
    return tuple(
        (brand.strip()[:100], params.get("v", "").strip()[:50])
        for brand, params in _sf_list(value)
        if brand.strip()
    )


def _js_brands(value: Any) -> Tuple[Tuple[str, str], ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    result = []
    for entry in value[:20]:
        if isinstance(entry, dict):
            brand = _clean_text(entry.get("brand"), 100)
            version = entry.get("version")
            if brand:
                result.append((brand, str(version).strip()[:50] if version is not None else ""))
    return tuple(result)


def _from_headers(headers: Any) -> Dict[str, Any]:
    """Client Hints fields from request headers (lowercase names)."""
    if not isinstance(headers, dict):
        return {}
    h = {str(k).strip().lower(): v for k, v in headers.items() if isinstance(v, str)}
    form_factors = h.get("sec-ch-ua-form-factors", h.get("sec-ch-ua-form-factor"))
    return {
        "brands": _header_brands(h.get("sec-ch-ua")),
        "full_versions": _header_brands(h.get("sec-ch-ua-full-version-list")),
        "mobile": _sf_bool(h.get("sec-ch-ua-mobile")),
        "platform": _sf_string(h.get("sec-ch-ua-platform")),
        "platform_version": _sf_string(h.get("sec-ch-ua-platform-version")),
        "model": _sf_string(h.get("sec-ch-ua-model")),
        "form_factors": tuple(item for item, _ in _sf_list(form_factors) if item),
        "ua_full_version": _sf_string(h.get("sec-ch-ua-full-version")),
    }


def _js_strings(value: Any) -> Tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        return ()
    return tuple(item.strip() for item in value[:20] if isinstance(item, str) and item.strip())


def _js_number(value: Any) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _from_js(js: Any) -> Dict[str, Any]:
    """Client Hints fields from the tracker's navigator.userAgentData copy."""
    if not isinstance(js, dict):
        return {}
    mobile = js.get("mobile")
    return {
        "brands": _js_brands(js.get("brands")),
        "full_versions": _js_brands(js.get("fullVersionList")),
        "mobile": mobile if isinstance(mobile, bool) else None,
        "platform": _clean_text(js.get("platform")),
        "platform_version": _clean_text(js.get("platformVersion")),
        "model": _clean_text(js.get("model")),
        "form_factors": _js_strings(js.get("formFactors")),
        "ua_full_version": _clean_text(js.get("uaFullVersion")),
        "max_touch_points": _js_number(js.get("max_touch_points", js.get("maxTouchPoints"))),
    }


def _merge_hints(header_hints: Any, js_hints: Any) -> ClientHints:
    """Merge both sources; a non-empty JS value wins over the header value."""
    merged: Dict[str, Any] = {}
    for source in (_from_headers(header_hints), _from_js(js_hints)):
        for key, value in source.items():
            if value is not None and value != () and value != "":
                merged[key] = value
    return ClientHints(**merged) if merged else _NO_HINTS


# Parse state ----------------------------------------------------------------------


class _Draft:
    """Mutable parse state shared by the detection steps."""

    def __init__(self) -> None:
        self.browser: Optional[str] = None
        self.browser_version: Optional[str] = None
        self.engine: Optional[str] = None
        self.os: Optional[str] = None
        self.os_version: Optional[str] = None
        self.device_type: Optional[str] = None      # guess from ordinary UA tokens
        self.special_type: Optional[str] = None     # tv/console/wearable/car from explicit tokens
        self.device_brand: Optional[str] = None
        self.device_model: Optional[str] = None
        self.is_webview = False
        self.in_app: Optional[str] = None
        self.is_bot_ua = False
        self.bot_name: Optional[str] = None
        self.reduced_android = False   # "Android 10; K": version and model frozen
        self.macintosh = False         # desktop-class Apple UA (could be an iPad)
        self.ipad_desktop = False      # iPad requesting the desktop site


def _dotted(version: Optional[str]) -> Optional[str]:
    """``17_5_1`` -> ``17.5.1``."""
    return version.replace("_", ".") if version else None


def _strip_zero_tail(version: Optional[str]) -> Optional[str]:
    """``14.0.0`` -> ``14``; ``12.1.0`` -> ``12.1``."""
    if not version:
        return None
    parts = version.split(".")
    while len(parts) > 1 and parts[-1] in ("0", "00", ""):
        parts.pop()
    return ".".join(parts)


def _major(version: Optional[str]) -> Optional[int]:
    """First numeric component of a version string, as an int."""
    match = re.match(r"\s*(\d+)", version or "")
    return int(match.group(1)) if match else None


# Operating systems ------------------------------------------------------------------

_WINDOWS_NT = {
    "10.0": "10", "6.4": "10", "6.3": "8.1", "6.2": "8", "6.1": "7", "6.0": "Vista",
    "5.2": "XP", "5.1": "XP", "5.01": "2000", "5.0": "2000",
}
# Fire OS releases by the Android version they are built on.
_FIRE_OS_BY_ANDROID = {11: "8", 9: "7", 7: "6", 5: "5"}
# A family the UA names precisely, versus the generic family a hint reports.
_SUBFAMILY_OF = {
    "Fire OS": "Android", "HarmonyOS": "Android", "Wear OS": "Android", "Horizon OS": "Android",
    "Xbox": "Windows", "Ubuntu": "Linux", "Fedora": "Linux", "Debian": "Linux",
    "Linux Mint": "Linux", "Arch Linux": "Linux", "openSUSE": "Linux", "CentOS": "Linux",
    "Tizen": "Linux", "webOS": "Linux", "Chromecast": "Linux",
}
_APPLE_MOBILE_OS = ("iOS", "iPadOS")
_ANDROID_LIKE_OS = ("Android", "Fire OS", "HarmonyOS", "Wear OS", "Horizon OS")

_XBOX_RE = re.compile(r"\bXbox(?:; (Xbox[^;)]*))?")
_PLAYSTATION_RE = re.compile(r"PlayStation(?:; PlayStation)? (\d|Vita|Portable)(?:[ /]([\d.]+))?", re.I)
_NINTENDO_RE = re.compile(r"Nintendo (Switch|WiiU|Wii U|Wii|New 3DS|3DS|DSi|DS)\b")
_WINDOWS_PHONE_RE = re.compile(r"Windows Phone(?: OS)? ([\d.]+)")
_WINDOWS_NT_RE = re.compile(r"Windows NT ([\d.]+)")
_WINDOWS_RE = re.compile(r"\bWin(?:dows|32|64|98|95|9x|NT)\b")
_OPENHARMONY_RE = re.compile(r"OpenHarmony ([\d.]+)")
_HARMONY_RE = re.compile(r"HarmonyOS(?:[ /]([\d.]+))?")
_HARMONY_CLASS_RE = re.compile(r"^Mozilla/[\d.]+ \((Phone|Tablet|PC|Watch|TV)[;)]")
_KAIOS_RE = re.compile(r"KAIOS(?:/([\d.]+))?", re.I)
_TIZEN_RE = re.compile(r"\bTizen[ /]?([\d.]+)?")
_WEBOS_RE = re.compile(r"Web0S|webOS|hpwOS")
_ROKU_RE = re.compile(r"\bRoku\b(?:/DVP-([\d.]+))?")
_CRKEY_RE = re.compile(r"\bCrKey/")
_APPLE_TV_RE = re.compile(r"Apple ?TV|\btvOS\b")
_TVOS_VERSION_RE = re.compile(r"(?:tvOS|CPU OS)[ /](\d+(?:[_.]\d+)*)|AppleTV\d+,\d+/(\d+(?:\.\d+)*)")
_WATCHOS_RE = re.compile(r"Watch ?OS[ /](\d+(?:[_.]\d+)*)", re.I)
_QUEST_RE = re.compile(r"OculusBrowser|\b(Quest(?: Pro| \d[sS]?)?)\b")
_SILK_RE = re.compile(r"\bSilk/")
_ANDROID_RE = re.compile(r"Android(?:[ /-]?(\d+(?:\.\d+)*))?")
_REDUCED_ANDROID_RE = re.compile(r"Android 10; K\)")
_IOS_DEVICE_RE = re.compile(r"\b(iPhone|iPad|iPod)\b")
_IOS_VERSION_RE = re.compile(r"(?:CPU (?:iPhone )?OS|iPhone OS|\biOS)[ /](\d+(?:[_.]\d+)*)")
_MAC_RE = re.compile(r"Macintosh|Mac OS X")
_MAC_VERSION_RE = re.compile(r"Mac OS X ?(\d+(?:[_.]\d+)*)")
_CROS_RE = re.compile(r"\bCrOS \S+ ([\d.]+)")
_FROZEN_CROS = "14541.0.0"
_LINUX_DISTROS = (
    ("Ubuntu", "Ubuntu"), ("Fedora", "Fedora"), ("Debian", "Debian"), ("Linux Mint", "Linux Mint"),
    ("Arch Linux", "Arch Linux"), ("openSUSE", "openSUSE"), ("CentOS", "CentOS"),
)
_BSD_RE = re.compile(r"\b(FreeBSD|OpenBSD|NetBSD)\b")
_BLACKBERRY_RE = re.compile(r"BlackBerry|\bBB10\b|RIM Tablet OS")
_SYMBIAN_RE = re.compile(r"Symbian|SymbOS|Series ?60")
_WEAR_OS_RE = re.compile(r"Wear ?OS|Pixel Watch|Galaxy Watch|\bSM-R\d{3}|TicWatch", re.I)


def _set_device(d: _Draft, brand: Optional[str], model: Optional[str], kind: Optional[str]) -> None:
    d.device_brand, d.device_model = brand, model
    if kind in ("tv", "console", "wearable", "car"):
        d.special_type = kind
    elif kind:
        d.device_type = kind


def _detect_console_os(ua: str, d: _Draft) -> bool:
    """Game consoles (checked first: their UAs embed other platforms)."""
    match = _XBOX_RE.search(ua)
    if match:
        d.os = "Xbox"
        _set_device(d, "Microsoft", (match.group(1) or "Xbox").strip(), "console")
        return True
    match = _PLAYSTATION_RE.search(ua)
    if match:
        d.os, d.os_version = "PlayStation", match.group(2)
        _set_device(d, "Sony", "PlayStation %s" % match.group(1).title(), "console")
        return True
    match = _NINTENDO_RE.search(ua)
    if match:
        d.os = "Nintendo"
        _set_device(d, "Nintendo", match.group(1).replace("WiiU", "Wii U"), "console")
        return True
    return False


def _detect_windows(ua: str, d: _Draft) -> bool:
    match = _WINDOWS_PHONE_RE.search(ua)
    if match:
        d.os, d.os_version, d.device_type = "Windows Phone", match.group(1), "mobile"
        return True
    match = _WINDOWS_NT_RE.search(ua)
    if match:
        d.os, d.os_version, d.device_type = "Windows", _WINDOWS_NT.get(match.group(1)), "desktop"
        return True
    if _WINDOWS_RE.search(ua):
        d.os, d.device_type = "Windows", "desktop"
        return True
    return False


def _detect_embedded_os(ua: str, d: _Draft) -> bool:
    """HarmonyOS, KaiOS, TV and wearable platforms."""
    match = _OPENHARMONY_RE.search(ua) or _HARMONY_RE.search(ua)
    if match:
        d.os, d.os_version = "HarmonyOS", match.group(1)
        form = _HARMONY_CLASS_RE.match(ua)
        if form:
            d.device_type = {"Phone": "mobile", "Tablet": "tablet", "PC": "desktop"}.get(form.group(1))
            d.special_type = {"Watch": "wearable", "TV": "tv"}.get(form.group(1))
        return True
    match = _KAIOS_RE.search(ua)
    if match:
        d.os, d.os_version, d.device_type = "KaiOS", match.group(1), "mobile"
        return True
    match = _TIZEN_RE.search(ua)
    if match:
        d.os, d.os_version = "Tizen", match.group(1)
        d.device_brand = "Samsung"
        d.device_type = "mobile"
        if re.search(r"\bSM-R\d|Gear", ua):
            d.special_type = "wearable"
        return True
    if _WEBOS_RE.search(ua):
        d.os, d.device_brand = "webOS", "LG"
        return True
    match = _ROKU_RE.search(ua)
    if match:
        d.os, d.os_version = "Roku OS", match.group(1)
        _set_device(d, "Roku", None, "tv")
        return True
    if _CRKEY_RE.search(ua) and "Android" not in ua:
        d.os = "Chromecast"
        _set_device(d, "Google", "Chromecast", "tv")
        return True
    if _APPLE_TV_RE.search(ua):
        match = _TVOS_VERSION_RE.search(ua)
        d.os = "tvOS"
        d.os_version = _dotted(match.group(1) or match.group(2)) if match else None
        _set_device(d, "Apple", "Apple TV", "tv")
        return True
    match = _WATCHOS_RE.search(ua)
    if match:
        d.os, d.os_version = "watchOS", _dotted(match.group(1))
        _set_device(d, "Apple", "Apple Watch", "wearable")
        return True
    match = _QUEST_RE.search(ua)
    if match and "Android" not in ua:
        d.os = "Horizon OS"
        _set_device(d, "Meta", match.group(1) or "Quest", "wearable")
        return True
    return False


def _detect_android(ua: str, d: _Draft) -> bool:
    is_silk = bool(_SILK_RE.search(ua))
    match = _ANDROID_RE.search(ua)
    if not match and not is_silk:
        return False
    version = _strip_zero_tail(match.group(1)) if match else None
    if _REDUCED_ANDROID_RE.search(ua):
        d.reduced_android, version = True, None
    d.os, d.os_version = "Android", version
    model = _android_model(ua)
    if is_silk or (model and re.match(r"(?:KF[A-Z]{2,4}|AFT[A-Z0-9]+)$", model)):
        d.os = "Fire OS"
        d.os_version = _FIRE_OS_BY_ANDROID.get(_major(version) or -1)
    elif _WEAR_OS_RE.search(ua):
        d.os, d.os_version, d.special_type = "Wear OS", None, "wearable"
    return True


def _detect_apple(ua: str, d: _Draft) -> bool:
    match = _IOS_DEVICE_RE.search(ua)
    if match:
        kind = match.group(1)
        version_match = _IOS_VERSION_RE.search(ua)
        d.os_version = _dotted(version_match.group(1)) if version_match else None
        if kind == "iPad":
            d.os = "iPadOS" if (_major(d.os_version) or 13) >= 13 else "iOS"
            _set_device(d, "Apple", "iPad", "tablet")
        else:
            d.os = "iOS"
            _set_device(d, "Apple", "iPhone" if kind == "iPhone" else "iPod touch", "mobile")
        return True
    if _MAC_RE.search(ua):
        d.os, d.macintosh = "macOS", True
        version = _MAC_VERSION_RE.search(ua)
        d.os_version = _dotted(version.group(1)) if version else None
        _set_device(d, "Apple", "Mac", "desktop")
        return True
    return False


def _detect_desktop_unix(ua: str, d: _Draft) -> bool:
    match = _CROS_RE.search(ua)
    if match:
        d.os, d.device_type = "ChromeOS", "desktop"
        d.os_version = None if match.group(1) == _FROZEN_CROS else match.group(1)
        return True
    for token, name in _LINUX_DISTROS:
        if token in ua:
            d.os, d.device_type = name, "desktop"
            return True
    match = _BSD_RE.search(ua)
    if match:
        d.os, d.device_type = match.group(1), "desktop"
        return True
    if "Linux" in ua or "X11" in ua:
        d.os, d.device_type = "Linux", "desktop"
        return True
    return False


def _detect_os(ua: str, d: _Draft) -> None:
    """Fill os/os_version (and the device facts the OS implies)."""
    if _detect_console_os(ua, d) or _detect_windows(ua, d) or _detect_embedded_os(ua, d):
        return
    if _BLACKBERRY_RE.search(ua):
        d.os, d.device_type, d.device_brand = "BlackBerry OS", "mobile", "BlackBerry"
        return
    if _detect_android(ua, d) or _detect_apple(ua, d) or _detect_desktop_unix(ua, d):
        return
    if _SYMBIAN_RE.search(ua):
        d.os, d.device_type = "Symbian", "mobile"


# Devices --------------------------------------------------------------------------

# Brand names, keyed by the lowercase word a model or UA starts with.
_BRAND_WORDS = {
    "samsung": "Samsung", "galaxy": "Samsung", "google": "Google", "pixel": "Google",
    "nexus": "Google", "xiaomi": "Xiaomi", "redmi": "Xiaomi", "poco": "Xiaomi", "mi": "Xiaomi",
    "huawei": "Huawei", "honor": "Honor", "oneplus": "OnePlus", "oppo": "Oppo", "realme": "Realme",
    "vivo": "Vivo", "iqoo": "Vivo", "motorola": "Motorola", "moto": "Motorola", "nokia": "Nokia",
    "hmd": "HMD", "sony": "Sony", "xperia": "Sony", "lg": "LG", "lenovo": "Lenovo", "asus": "Asus",
    "htc": "HTC", "zte": "ZTE", "nubia": "ZTE", "tecno": "Tecno", "infinix": "Infinix", "itel": "itel",
    "meizu": "Meizu", "alcatel": "Alcatel", "tcl": "TCL", "blackview": "Blackview", "doogee": "Doogee",
    "ulefone": "Ulefone", "oukitel": "Oukitel", "cubot": "Cubot", "nothing": "Nothing",
    "fairphone": "Fairphone", "sharp": "Sharp", "kyocera": "Kyocera", "fujitsu": "Fujitsu",
    "panasonic": "Panasonic", "hisense": "Hisense", "wiko": "Wiko", "umidigi": "Umidigi",
    "gionee": "Gionee", "lava": "Lava", "micromax": "Micromax", "crosscall": "Crosscall",
    "teclast": "Teclast", "chuwi": "Chuwi", "amazon": "Amazon", "kindle": "Amazon",
    "blackberry": "BlackBerry", "microsoft": "Microsoft", "apple": "Apple",
}

# Model-code patterns for brands whose models rarely carry the brand name.
_MODEL_CODES = [
    (re.compile(r"^(?:SAMSUNG[ -])?(?:SM-|GT-|SCH-|SGH-|SHV-|SC-\d|SCV\d)", re.I), "Samsung"),
    (re.compile(r"^(?:M\d{4}[A-Z]\d{1,2}[A-Z]{1,3}|2\d{3}[0-9A-Z]{4,7})$"), "Xiaomi"),
    (re.compile(r"^[A-Z]{3}-(?:AL|AN|BX|CL|DL|L|LX|N|NX|TL|TN|U|W)\d{1,2}[A-Z]?$"), "Huawei"),
    (re.compile(r"^(?:GM|HD|IN|KB|LE|NE|BE|DE|AC|EB)\d{4}$"), "OnePlus"),
    (re.compile(r"^(?:CPH\d{4}|PH[A-Z]\d{3}|PE[A-Z]{2}\d{2})$"), "Oppo"),
    (re.compile(r"^RMX\d{4}$"), "Realme"),
    (re.compile(r"^(?:V\d{4}[A-Z]{0,2}|I\d{4})$"), "Vivo"),
    (re.compile(r"^XT\d{4}"), "Motorola"),
    (re.compile(r"^TA-\d{4}$"), "Nokia"),
    (re.compile(r"^(?:XQ-[A-Z]{2}\d{2}|SO-\d{2}[A-Z]|SOG\d{2}|SOV\d{2}|[CDEFGHJ]\d{4})$"), "Sony"),
    (re.compile(r"^(?:LG[- ]|LM-[A-Z]\d{3})"), "LG"),
    (re.compile(r"^(?:KF[A-Z]{2,4}|AFT[A-Z0-9]+)$"), "Amazon"),
    (re.compile(r"^TB-?[A-Z0-9]{3,}"), "Lenovo"),
    (re.compile(r"^(?:ASUS_|ZS\d{3}KL)"), "Asus"),
    (re.compile(r"^FP\d$"), "Fairphone"),
]

_TABLET_MODEL_RE = re.compile(
    r"^(?:SM-[TXP]\d|KF[A-Z]{2,4}$|Pixel Tablet|TB-?[A-Z0-9]|Lenovo TB|Nexus (?:7|9|10)\b)"
    r"|\b(?:Tab|Pad|MatePad|MediaPad)\b",
    re.I,
)
_MODEL_SKIP_RE = re.compile(
    r"^(?:U|I|N|wv|K|Mobile|Tablet|HarmonyOS|Linux|arm(?:64|_64)?|x86(?:_64)?|aarch64)$"
    r"|^[a-z]{2}(?:[-_][a-zA-Z]{2,4})?$|^rv:|^HMSCore|^Build/|^Android",
    re.I,
)
# The segment may hold one nested group, as in "moto g power (2022)".
_ANDROID_SEGMENT_RE = re.compile(r"Android[ /-]?[\d.]*;\s*((?:[^()]|\([^()]*\))*)\)")

# Case-sensitive on purpose: "AFT..." (Fire TV models) must not match "after".
_TV_RE = re.compile(
    r"SMART-TV|Smart-TV|SmartTV|Smart TV|\bHbbTV|BRAVIA|\bAFT[A-Z0-9]+\b|CrKey/|Android ?TV"
    r"|Google ?TV|NetCast|Web0S|webOS\.TV|NETTV|VIDAA|SmartCast|\bViera\b|\bTV Safari\b"
    r"|Opera TV|PhilipsTV"
)
_TV_BRANDS = [
    (re.compile(r"Samsung|Tizen|SMART-TV", re.I), "Samsung"),
    (re.compile(r"Web0S|webOS|NetCast|\bLGE?\b", re.I), "LG"),
    (re.compile(r"BRAVIA|\bSony\b", re.I), "Sony"),
    (re.compile(r"Philips|NETTV", re.I), "Philips"),
    (re.compile(r"VIDAA|Hisense", re.I), "Hisense"),
    (re.compile(r"VIZIO|SmartCast", re.I), "Vizio"),
    (re.compile(r"Panasonic|Viera", re.I), "Panasonic"),
    (re.compile(r"\bTCL\b"), "TCL"),
    (re.compile(r"AQUOS|\bSharp\b", re.I), "Sharp"),
    (re.compile(r"Toshiba|TSBNetTV", re.I), "Toshiba"),
    (re.compile(r"\bAFT[A-Z0-9]+\b"), "Amazon"),
    (re.compile(r"CrKey|Chromecast|Google ?TV", re.I), "Google"),
    (re.compile(r"\bXiaomi\b|MiTV|Mi TV", re.I), "Xiaomi"),
]
_CAR_RE = re.compile(r"\bTesla/|QtCarBrowser|Android Automotive|\bAAOS\b")
_WINDOWS_PHONE_DEVICE_RE = re.compile(r"\b(NOKIA|Microsoft|HTC|SAMSUNG|LG|Acer|Alcatel)\s*;\s*([^;)]+)")


def _android_model(ua: str) -> Optional[str]:
    """Device model from the Android UA segment (None for reduced UAs)."""
    match = _ANDROID_SEGMENT_RE.search(ua)
    if not match:
        return None
    for part in match.group(1).split(";"):
        candidate = re.sub(r"\s*Build/.*$", "", part.strip()).strip()
        if candidate and not _MODEL_SKIP_RE.search(candidate):
            candidate = re.sub(r"^SAMSUNG[ -]", "", candidate, flags=re.I).strip()
            return candidate[:60] or None
    return None


def _brand_for_model(model: Optional[str]) -> Optional[str]:
    """Brand of a device model string, from its first word or its code."""
    if not model:
        return None
    first = re.split(r"[\s_\-/]", model.strip(), maxsplit=1)[0].lower()
    if first in _BRAND_WORDS:
        return _BRAND_WORDS[first]
    for regex, brand in _MODEL_CODES:
        if regex.search(model):
            return brand
    return None


def _detect_tv(ua: str, d: _Draft) -> None:
    if not _TV_RE.search(ua):
        return
    d.special_type = "tv"
    if not d.device_brand:
        for regex, brand in _TV_BRANDS:
            if regex.search(ua):
                d.device_brand = brand
                break
    if d.device_brand == "Sony" and d.device_model is None:
        match = re.search(r"\b(BRAVIA[^;)]*?)(?:\s+Build/|[;)])", ua)
        d.device_model = match.group(1).strip() if match else None


def _detect_device(ua: str, d: _Draft) -> None:
    """Brand, model and type from UA tokens (after the OS is known)."""
    if _CAR_RE.search(ua):
        d.special_type = "car"
        if "Tesla" in ua:
            d.device_brand = "Tesla"
    if d.os == "Windows Phone":
        match = _WINDOWS_PHONE_DEVICE_RE.search(ua)
        if match:
            d.device_brand = _BRAND_WORDS.get(match.group(1).lower(), match.group(1))
            d.device_model = match.group(2).strip()
    elif d.os in _ANDROID_LIKE_OS or (d.os is None and "Android" in ua):
        model = _android_model(ua)
        if model:
            d.device_model = model
            d.device_brand = _brand_for_model(model) or d.device_brand
        if d.os == "Fire OS":
            d.device_brand = "Amazon"
        elif d.os == "HarmonyOS" and d.device_brand is None:
            d.device_brand = "Huawei"
        if d.device_type is None:
            tablet = (model and _TABLET_MODEL_RE.search(model)) or "Tablet" in ua
            d.device_type = "tablet" if tablet or "Mobile" not in ua else "mobile"
    elif "Kindle/" in ua:
        _set_device(d, "Amazon", "Kindle", "tablet")
    _detect_tv(ua, d)
    if d.device_type is None and re.search(r"\bMobi(?:le)?\b|Opera Mini|\bJ2ME\b|MIDP", ua):
        d.device_type = "mobile"


# Browsers ---------------------------------------------------------------------------

# Ordered: a browser built on Chromium or WebView carries its own token next
# to "Chrome/", so the specific tokens are tried before the generic ones.
_BROWSER_TOKENS = [
    (r"\bOculusBrowser/(\d[\w.]*)", "Meta Quest Browser"),
    (r"\bOpera Mini/(\d[\w.]*)", "Opera Mini"),
    (r"\bOPiOS/(\d[\w.]*)", "Opera Mini"),       # Opera Mini for iOS
    (r"\bOPT/(\d[\w.]*)", "Opera"),              # Opera (Touch) for iOS
    (r"\bOPR/(\d[\w.]*)", "Opera"),
    (r"\bEdg(?:e|A|iOS)?/(\d[\w.]*)", "Edge"),
    (r"\bSamsungBrowser/(\d[\w.]*)", "Samsung Internet"),
    (r"\bYa(?:Search)?Browser/(\d[\w.]*)", "Yandex Browser"),
    (r"\b(?:UCBrowser|UBrowser|UCTurbo)/(\d[\w.]*)", "UC Browser"),
    (r"\bHuaweiBrowser/(\d[\w.]*)", "Huawei Browser"),
    (r"\b(?:XiaoMi/)?(?:Miui|HyperOS)Browser/(\d[\w.]*)", "MIUI Browser"),
    (r"\b(?:Ddg|DuckDuckGo)/(\d[\w.]*)", "DuckDuckGo"),
    (r"\bSilk/(\d[\w.]*)", "Silk"),
    (r"\bVivaldi/(\d[\w.]*)", "Vivaldi"),
    (r"\bBrave(?: Chrome)?/(\d[\w.]*)", "Brave"),
    (r"\bWhale/(\d[\w.]*)", "Whale"),
    (r"\bcoc_coc_browser/(\d[\w.]*)", "Coc Coc"),
    (r"\bM?QQBrowser/(\d[\w.]*)", "QQ Browser"),
    (r"\bMaxthon[ /](\d[\w.]*)", "Maxthon"),
    (r"\bPuffin/(\d[\w.]*)", "Puffin"),
    (r"\bNintendoBrowser/(\d[\w.]*)", "Nintendo Browser"),
    (r"\b(?:Focus|Klar)/(\d[\w.]*)", "Firefox Focus"),
    (r"\bFxiOS/(\d[\w.]*)", "Firefox"),
    (r"\bCriOS/(\d[\w.]*)", "Chrome"),
    (r"\bElectron/(\d[\w.]*)", "Electron"),
    (r"\bHeadlessChrome/(\d[\w.]*)", "HeadlessChrome"),
    (r"\bPaleMoon/(\d[\w.]*)", "Pale Moon"),
    (r"\bWaterfox/(\d[\w.]*)", "Waterfox"),
    (r"\bSeaMonkey/(\d[\w.]*)", "SeaMonkey"),
]
_BROWSER_TOKEN_RES = [(re.compile(pattern), name) for pattern, name in _BROWSER_TOKENS]

_UCWEB_RE = re.compile(r"\bUCWEB")
_PRESTO_RE = re.compile(r"^Opera/|\bPresto/")
_OPERA_VERSION_RE = re.compile(r"\bOpera[ /](\d[\w.]*)")
_MSIE_RE = re.compile(r"\bMSIE (\d[\w.]*)")
_TRIDENT_RV_RE = re.compile(r"Trident/\d.*?\brv:(\d[\w.]*)")
_WEBVIEW_RE = re.compile(r"; wv\)|\bVersion/4\.0 Chrome/")
_CHROME_RE = re.compile(r"\bChrome/(\d[\w.]*)")
_CHROMIUM_RE = re.compile(r"\bChromium/(\d[\w.]*)")
_FIREFOX_RE = re.compile(r"\bFirefox/(\d[\w.]*)")
_SAFARI_VERSION_RE = re.compile(r"\bVersion/(\d[\w.]*)(?:\s+Mobile/\S+)?\s+Safari/")
_VERSION_RE = re.compile(r"\bVersion/(\d[\w.]*)")

_IOS_WEBVIEW = "Safari WebView"
_ANDROID_WEBVIEW = "Android WebView"
# Families that only say "some Chromium/WebKit build": hints or in-app
# detection may name the real product.
_GENERIC_BROWSERS = frozenset({None, "Chrome", "Chromium", "Safari", _ANDROID_WEBVIEW, _IOS_WEBVIEW,
                               "Android Browser"})


def _detect_browser(ua: str, d: _Draft) -> None:
    """Fill browser/browser_version (and is_webview for embedded engines)."""
    for regex, name in _BROWSER_TOKEN_RES:
        match = regex.search(ua)
        # The PlayStation Vita's NetFront browser also says "Silk/3.2".
        if match and not (name == "Silk" and d.os == "PlayStation"):
            d.browser, d.browser_version = name, match.group(1)
            if name == "Electron":
                d.is_webview = True
            return
    if _UCWEB_RE.search(ua):
        d.browser = "UC Browser"
        return
    if _PRESTO_RE.search(ua):
        # Presto Opera froze "Opera/9.80"; the real version is in Version/.
        match = _VERSION_RE.search(ua) or _OPERA_VERSION_RE.search(ua)
        d.browser, d.browser_version = "Opera", match.group(1) if match else None
        return
    match = _MSIE_RE.search(ua) or _TRIDENT_RV_RE.search(ua)
    if match:
        d.browser, d.browser_version = "Internet Explorer", match.group(1)
        return
    chrome = _CHROME_RE.search(ua)
    if chrome and d.os in _ANDROID_LIKE_OS and _WEBVIEW_RE.search(ua):
        d.browser, d.browser_version, d.is_webview = _ANDROID_WEBVIEW, chrome.group(1), True
        return
    match = _CHROMIUM_RE.search(ua) or chrome
    if match:
        d.browser = "Chromium" if match.re is _CHROMIUM_RE else "Chrome"
        d.browser_version = match.group(1)
        return
    match = _FIREFOX_RE.search(ua)
    if match:
        d.browser, d.browser_version = "Firefox", match.group(1)
        return
    _detect_webkit_browser(ua, d)


def _detect_webkit_browser(ua: str, d: _Draft) -> None:
    """Safari, Apple WebViews and the other WebKit browsers."""
    safari = _SAFARI_VERSION_RE.search(ua)
    if d.os == "BlackBerry OS":
        version = _VERSION_RE.search(ua)
        d.browser, d.browser_version = "BlackBerry Browser", version.group(1) if version else None
    elif d.os == "Tizen" and d.special_type == "tv":
        d.browser = "Tizen Browser"
    elif d.os in _ANDROID_LIKE_OS and _VERSION_RE.search(ua) and "Safari/" in ua:
        d.browser, d.browser_version = "Android Browser", _VERSION_RE.search(ua).group(1)
    elif safari:
        d.browser, d.browser_version = "Safari", safari.group(1)
    elif d.os in _APPLE_MOBILE_OS or (d.os == "macOS" and "AppleWebKit/" in ua and "Safari/" not in ua):
        # WKWebView inside an app: no Version/ token (and often no Safari/).
        d.browser, d.is_webview = _IOS_WEBVIEW, True


def _detect_engine(ua: str, d: _Draft) -> None:
    if "Trident/" in ua or _MSIE_RE.search(ua):
        d.engine = "Trident"
    elif re.search(r"\bEdge/\d", ua):
        d.engine = "EdgeHTML"
    elif _PRESTO_RE.search(ua):
        d.engine = "Presto"
    elif d.os in _APPLE_MOBILE_OS or d.os in ("tvOS", "watchOS"):
        d.engine = "WebKit"
    elif re.search(r"\brv:[\d.]+\)\s*Gecko/|\bGoanna/", ua) or ("Gecko/" in ua and "like Gecko" not in ua):
        d.engine = "Gecko"
    elif re.search(r"AppleWebKit/537\.36\b|\bArkWeb/", ua):
        d.engine = "Blink"   # every Blink build reports the frozen WebKit 537.36
    elif "AppleWebKit/" in ua:
        d.engine = "WebKit"


# In-app browsers ----------------------------------------------------------------------

_IN_APP_RULES = [
    (r"FBAN/Messenger|FB_IAB/MESSENGER|\bMessenger(?:Lite)?ForiOS|\bOrca-Android", "Messenger", re.I),
    (r"\bBarcelona \d", "Threads", 0),
    (r"\bInstagram[ /]\d", "Instagram", re.I),
    (r"\bFBAN/|\bFB_IAB/|\bFBAV/|\bFBIOS\b|\bFB4A\b", "Facebook", 0),
    (r"LinkedInApp", "LinkedIn", 0),
    (r"\baweme[_/]\d|AppName/aweme", "Douyin", 0),
    (r"musical_ly|\btrill_\d|AppName/(?:musical_ly|trill)|\bTikTok[ /]\d|BytedanceWebview", "TikTok", 0),
    (r"Twitter for iP(?:hone|ad)|TwitterAndroid|\bTwitter/\d", "Twitter", 0),
    (r"\bSnapchat/\d", "Snapchat", 0),
    (r"\[Pinterest/(?:Android|iOS)\]|Pinterest for (?:Android|iOS)", "Pinterest", 0),
    (r"\bMicroMessenger/", "WeChat", 0),
    (r"\bLine/\d", "Line", 0),
    (r"\bTelegram-Android/|\bTelegram/\d", "Telegram", 0),
    (r"\bKAKAOTALK\b", "KakaoTalk", re.I),
    (r"\bNAVER\(inapp", "Naver", 0),
    (r"\bbaiduboxapp/", "Baidu App", 0),
    (r"__weibo__|\bWeibo\b", "Weibo", 0),
    (r"\bAlipayClient/", "Alipay", 0),
    (r"\bDingTalk/", "DingTalk", 0),
    (r"\bZalo", "Zalo", 0),
    (r"\bQQ/\d", "QQ", 0),
    (r"\bGSA/\d", "Google App", 0),
    (r"\bGmail/\d", "Gmail", re.I),
]
_IN_APP_RES = [(re.compile(pattern, flags), name) for pattern, name, flags in _IN_APP_RULES]


def _detect_in_app(ua: str, d: _Draft) -> None:
    for regex, name in _IN_APP_RES:
        if regex.search(ua):
            d.in_app = name
            return


# Optional uap-python back end ---------------------------------------------------------

_UapResult = Tuple[Optional[str], Optional[str], Optional[str], Optional[str], Optional[str], Optional[str]]


class _UapBackend(NamedTuple):
    parse: Callable[[str], _UapResult]
    version: str


_UAP_STATE: Dict[str, Any] = {"loaded": False, "backend": None}

_UAP_BROWSERS = {
    "chrome mobile": "Chrome", "chrome mobile ios": "Chrome", "chrome mobile webview": _ANDROID_WEBVIEW,
    "mobile safari": "Safari", "mobile safari ui/wkwebview": _IOS_WEBVIEW, "firefox ios": "Firefox",
    "firefox mobile": "Firefox", "firefox beta": "Firefox", "edge mobile": "Edge", "ie": "Internet Explorer",
    "ie mobile": "Internet Explorer", "opera mobile": "Opera", "miuibrowser": "MIUI Browser",
    "amazon silk": "Silk", "android": "Android Browser", "duckduckgo mobile": "DuckDuckGo",
    "qq browser mobile": "QQ Browser", "oculus browser": "Meta Quest Browser",
}
# uap reports some in-app browsers as the browser family.
_UAP_IN_APP = {
    "facebook": "Facebook", "facebook messenger": "Messenger", "instagram": "Instagram",
    "google": "Google App", "line": "Line", "wechat": "WeChat", "snapchat": "Snapchat",
    "pinterest": "Pinterest", "linkedin": "LinkedIn", "twitter": "Twitter", "tiktok": "TikTok",
}
_UAP_OS = {"mac os x": "macOS", "chrome os": "ChromeOS", "symbian os": "Symbian"}
# Placeholder brands ("Generic_Android_Tablet"...) and models uap reports.
_UAP_NO_BRAND = ("generic", "spider", "other", "unknown")
_UAP_NO_MODEL = frozenset({"k", "other", "smartphone", "tablet", "desktop", "feature phone", "spider",
                           "browser", "touch"})
# OS families the UA names more precisely than uap does.
_UA_SPECIFIC_OS = frozenset({
    "Xbox", "PlayStation", "Nintendo", "Fire OS", "HarmonyOS", "KaiOS", "Wear OS", "tvOS",
    "watchOS", "webOS", "Tizen", "Roku OS", "Chromecast", "iPadOS", "Horizon OS",
})


def _join_version(*parts: Any) -> Optional[str]:
    numbers = []
    for part in parts:
        if part is None or part == "":
            break
        numbers.append(str(part))
    return ".".join(numbers) or None


def _uap_modern(module: Any) -> Callable[[str], _UapResult]:
    """Adapter for uap-python >= 1.0 (``ua_parser.parse``)."""

    def run(ua: str) -> _UapResult:
        result = module.parse(ua)
        agent, system, device = result.user_agent, result.os, result.device
        return (
            agent.family if agent else None,
            _join_version(agent.major, agent.minor, agent.patch, agent.patch_minor) if agent else None,
            system.family if system else None,
            _join_version(system.major, system.minor, system.patch, system.patch_minor) if system else None,
            device.brand if device else None,
            device.model if device else None,
        )

    return run


def _uap_legacy(module: Any) -> Callable[[str], _UapResult]:
    """Adapter for uap-python 0.x (``user_agent_parser.Parse``)."""

    def run(ua: str) -> _UapResult:
        result = module.Parse(ua)
        agent, system, device = result.get("user_agent", {}), result.get("os", {}), result.get("device", {})
        return (
            agent.get("family"),
            _join_version(agent.get("major"), agent.get("minor"), agent.get("patch")),
            system.get("family"),
            _join_version(system.get("major"), system.get("minor"), system.get("patch"),
                          system.get("patch_minor")),
            device.get("brand"),
            device.get("model"),
        )

    return run


def _uap_version(module: Any) -> str:
    """Library version plus regex-data version, so either change is noticed."""
    try:
        from importlib import metadata
    except ImportError:  # pragma: no cover - Python < 3.8
        metadata = None  # type: ignore[assignment]
    parts: List[str] = []
    for dist in ("ua-parser", "ua-parser-builtins"):
        try:
            parts.append(metadata.version(dist))  # type: ignore[union-attr]
        except Exception:
            continue
    if not parts:
        raw = getattr(module, "VERSION", None) or getattr(module, "__version__", None)
        parts.append(".".join(str(p) for p in raw) if isinstance(raw, tuple) else str(raw or "unknown"))
    return "-".join(parts)


def _load_uap() -> Optional[_UapBackend]:
    """Import uap-python once; None when it is missing or unusable."""
    if _UAP_STATE["loaded"]:
        return _UAP_STATE["backend"]
    _UAP_STATE["loaded"] = True
    try:
        import ua_parser  # type: ignore[import-not-found]

        if callable(getattr(ua_parser, "parse", None)):
            runner = _uap_modern(ua_parser)
        else:
            from ua_parser import user_agent_parser  # type: ignore[import-not-found]

            runner = _uap_legacy(user_agent_parser)
        _UAP_STATE["backend"] = _UapBackend(runner, _uap_version(ua_parser))
    except Exception:
        _UAP_STATE["backend"] = None
    return _UAP_STATE["backend"]


def _active_backend() -> Optional[_UapBackend]:
    """The uap back end unless TIDESIGHT_PURE_UA asks for the built-in parser."""
    if os.environ.get(_PURE_ENV, "").strip().lower() not in ("", "0", "false", "no", "off"):
        return None
    return _load_uap()


def _apply_uap(ua: str, d: _Draft, backend: _UapBackend) -> bool:
    """Overlay uap's browser and OS families and versions on the built-in draft.

    The built-in rules keep the last word where uap is known to be weaker:
    TV and console browsers, in-app browsers (named by the WebView they
    use), and device brand/model, which stay as the UA spells them so both
    back ends name devices the same way; uap only fills a missing brand.
    """
    try:
        family, version, os_family, os_version, brand, model = backend.parse(ua)
    except Exception:
        return False
    if family and family != "Other" and d.special_type not in ("tv", "console"):
        key = family.lower()
        if key in _UAP_IN_APP or d.in_app:
            d.in_app = d.in_app or _UAP_IN_APP[key]
        else:
            name = _UAP_BROWSERS.get(key, re.sub(r" Mobile$", "", family))
            d.browser, d.browser_version = name, version
            if name in (_ANDROID_WEBVIEW, _IOS_WEBVIEW):
                d.is_webview = True
    if os_family and os_family != "Other" and d.os not in _UA_SPECIFIC_OS:
        match = re.match(r"Windows (\d[\d.]*|XP|Vista)$", os_family)
        if match:
            d.os, d.os_version = "Windows", match.group(1)
        else:
            d.os, d.os_version = _UAP_OS.get(os_family.lower(), os_family), os_version
        if d.os == "Android":
            d.os_version = _strip_zero_tail(d.os_version)
    usable_brand = bool(brand) and not brand.lower().startswith(_UAP_NO_BRAND)
    if usable_brand and d.device_brand is None and d.special_type is None:
        d.device_brand = _BRAND_WORDS.get(brand.lower(), brand)
        d.device_model = model if model and model.lower() not in _UAP_NO_MODEL else None
    return True


# Refinements shared by both back ends --------------------------------------------------


def _apply_frozen_versions(d: _Draft) -> None:
    """Undo what frozen/reduced User-Agents claim, without claiming more."""
    if d.os == "macOS" and d.os_version and d.os_version.startswith("10.15"):
        # Safari, Chrome and Firefox all freeze macOS at 10.15(.7) since macOS 11,
        # so the UA only tells "10.15 or later".
        d.os_version = "10.15"
    elif d.os == "ChromeOS" and d.os_version == _FROZEN_CROS:
        d.os_version = None
    elif d.os == "Android" and d.reduced_android:
        d.os_version = None          # "Android 10; K" is sent by every Android version
        if d.device_model == "K":
            d.device_model = None
    elif d.os in _APPLE_MOBILE_OS and d.browser == "Safari":
        # Since iOS 26, Safari freezes the OS in the UA (e.g. "18_6") while
        # Version/ follows the real release, which uses the same number.
        safari, system = _major(d.browser_version), _major(d.os_version)
        if safari and safari >= 26 and (system is None or safari > system):
            d.os_version = ".".join((d.browser_version or "").split(".")[:2])


def _apply_in_app(ua: str, d: _Draft) -> None:
    """In-app browsers are WebViews; name the engine family, not the app."""
    if not d.in_app:
        return
    d.is_webview = True
    if d.browser not in _GENERIC_BROWSERS:
        return
    if d.os in _APPLE_MOBILE_OS:
        d.browser, d.browser_version = _IOS_WEBVIEW, None
    elif d.os in _ANDROID_LIKE_OS:
        chrome = _CHROME_RE.search(ua)
        d.browser, d.browser_version = _ANDROID_WEBVIEW, chrome.group(1) if chrome else None


# GREASE brands: "Not;A=Brand", "Not)A;Brand", "Not_A Brand", " Not A;Brand"...
_GREASE_RE = re.compile(r"^[^a-z0-9]*not[^a-z0-9]*a[^a-z0-9]*brand[^a-z0-9]*$", re.I)
_BRAND_NAMES = {
    "google chrome": "Chrome", "chromium": "Chromium", "microsoft edge": "Edge", "opera": "Opera",
    "opera gx": "Opera GX", "opera air": "Opera Air", "brave": "Brave", "android webview": _ANDROID_WEBVIEW,
    "microsoft edge webview2": "Edge WebView2", "samsung internet": "Samsung Internet",
    "yandex": "Yandex Browser", "yabrowser": "Yandex Browser", "duckduckgo": "DuckDuckGo",
    "headlesschrome": "HeadlessChrome", "whale": "Whale", "naver whale": "Whale", "vivaldi": "Vivaldi",
    "huawei browser": "Huawei Browser", "huaweibrowser": "Huawei Browser", "coc coc": "Coc Coc",
}
_PLATFORM_OS = {
    "windows": "Windows", "macos": "macOS", "android": "Android", "chrome os": "ChromeOS",
    "chromeos": "ChromeOS", "chromium os": "ChromeOS", "linux": "Linux", "ios": "iOS",
}
_FORM_FACTOR_TYPES = (
    ("automotive", "car"), ("watch", "wearable"), ("xr", "wearable"), ("tablet", "tablet"),
    ("eink", "tablet"), ("mobile", "mobile"), ("desktop", "desktop"),
)


_CHROMIUM_FAMILIES = frozenset({"Chrome", "Chromium", "Edge", "Brave", "Opera", "Opera GX"})


def _brand_choice(hints: ClientHints) -> Optional[Tuple[str, str]]:
    """The browser named by the brand list, with its (full) version."""
    # Genuine brands always carry a numeric version; this also drops garbage.
    entries = [
        (b, v) for b, v in (hints.full_versions or hints.brands)
        if not _GREASE_RE.match(b) and re.search(r"[A-Za-z]", b) and re.match(r"\d", v)
    ]
    if not entries:
        return None
    names = {brand.lower(): version for brand, version in entries}
    webview2 = "microsoft edge webview2" in names
    generic = {"chromium", "google chrome", "android webview", "microsoft edge webview2", "headlesschrome"}
    if webview2:
        generic.add("microsoft edge")
    for brand, version in entries:
        if brand.lower() not in generic:
            return _BRAND_NAMES.get(brand.lower(), brand), version
    for key in ("headlesschrome", "microsoft edge webview2", "android webview", "google chrome", "chromium"):
        if key in names:
            return _BRAND_NAMES[key], names[key]
    return None


def _more_precise(new: str, old: Optional[str]) -> bool:
    """True when ``new`` adds detail to ``old`` (same major, e.g. reduced UA)."""
    if not new:
        return False
    if not old:
        return True
    if _major(new) != _major(old):
        return False
    return sum(1 for p in new.split(".") if p.strip("0")) > sum(1 for p in old.split(".") if p.strip("0"))


def _apply_hint_browser(d: _Draft, hints: ClientHints) -> None:
    choice = _brand_choice(hints)
    if choice is not None:
        name, version = choice
        if d.engine is None or (d.engine == "WebKit" and d.os not in _APPLE_MOBILE_OS):
            d.engine = "Blink"   # only Chromium implements UA Client Hints
        if name in (_ANDROID_WEBVIEW, "Edge WebView2"):
            d.is_webview = True
            if d.browser in ("Chrome", "Chromium", "Edge", None):
                d.browser, d.browser_version = name, version or d.browser_version
        elif (d.browser in ("Chrome", "Chromium", None) and name != d.browser) or (
            d.browser and name.startswith(d.browser + " ")
        ):
            d.browser, d.browser_version = name, version or d.browser_version
        if d.browser == name and version and _more_precise(version, d.browser_version):
            d.browser_version = version
    # Deprecated single full version (navigator.userAgentData.uaFullVersion).
    full = hints.ua_full_version
    if full and d.browser in _CHROMIUM_FAMILIES and _more_precise(full, d.browser_version):
        d.browser_version = full


def _windows_from_hint(platform_version: str) -> Optional[str]:
    match = re.match(r"(\d+)(?:\.(\d+))?", platform_version)
    if not match:
        return None
    major, minor = int(match.group(1)), int(match.group(2) or 0)
    if major >= 13:
        return "11"
    if 1 <= major <= 10:
        return "10"
    if major == 0:
        return {1: "7", 2: "8", 3: "8.1"}.get(minor)
    return None


def _os_version_from_hint(os_name: str, platform_version: Optional[str]) -> Optional[str]:
    if not platform_version or not re.match(r"\d", platform_version):
        return None
    if os_name == "Windows":
        return _windows_from_hint(platform_version)
    if os_name in ("Android", "macOS", "iOS"):
        version = _strip_zero_tail(platform_version) if os_name == "Android" else platform_version
        if os_name != "Android" and version and version.count(".") >= 2 and version.endswith(".0"):
            version = version[:-2]
        return version
    if os_name == "ChromeOS":
        return platform_version
    return None


def _apply_hint_os(d: _Draft, hints: ClientHints) -> None:
    hint_os = _PLATFORM_OS.get((hints.platform or "").strip().lower())
    if hint_os is None:
        return
    if d.os != hint_os and _SUBFAMILY_OF.get(d.os or "") != hint_os:
        d.os, d.os_version = hint_os, None
    if d.os == hint_os:
        version = _os_version_from_hint(hint_os, hints.platform_version)
        if version:
            d.os_version = version


def _apply_hints(d: _Draft, hints: ClientHints) -> None:
    """Client Hints refinements (browser, OS, model, iPadOS detection)."""
    if hints == _NO_HINTS:
        return
    _apply_hint_browser(d, hints)
    _apply_hint_os(d, hints)
    if hints.model and d.special_type is None:
        d.device_model = hints.model[:60]
        d.device_brand = _brand_for_model(hints.model) or d.device_brand
    touch = hints.max_touch_points
    if d.macintosh and d.os == "macOS" and touch is not None and touch > 1:
        # iPadOS Safari requests desktop sites with a Mac UA; Macs have no touch screen.
        # Safari's Version/ matches the iPadOS release it ships with.
        d.os, d.ipad_desktop, d.engine = "iPadOS", True, "WebKit"
        safari = d.browser_version if d.browser == "Safari" else None
        d.os_version = ".".join(safari.split(".")[:2]) if safari else None
        d.device_brand, d.device_model = "Apple", "iPad"


def _bot_version(ua: str, name: str) -> Optional[str]:
    match = re.search(r"(?i)\b%s[/ ]v?(\d+(?:\.\d+)*)" % re.escape(name), ua)
    return match.group(1) if match else None


def _apply_bot(ua: str, d: _Draft) -> None:
    """Known bots: the bot is the "browser" and the device type is 'bot'."""
    match = bots.match_ua(ua)
    if match is None:
        return
    d.is_bot_ua, d.bot_name = True, match[0]
    d.browser, d.browser_version = match[0], _bot_version(ua, match[0])
    d.device_brand = d.device_model = None


def _device_type(d: _Draft, hints: ClientHints) -> str:
    if d.is_bot_ua:
        return "bot"
    if d.special_type:
        return d.special_type
    factors = {factor.strip().lower() for factor in hints.form_factors}
    for factor, kind in _FORM_FACTOR_TYPES:
        if factor in factors:
            return kind
    if d.ipad_desktop:
        return "tablet"
    if hints.mobile is True:
        return "mobile"
    if d.device_type:
        return d.device_type
    if hints.mobile is False and d.os in ("Windows", "macOS", "Linux", "ChromeOS"):
        return "desktop"
    return "unknown"


# Public API -------------------------------------------------------------------------------


def _builtin_draft(ua: str) -> _Draft:
    d = _Draft()
    if not ua:
        return d
    _detect_os(ua, d)
    _detect_device(ua, d)
    _detect_browser(ua, d)
    _detect_engine(ua, d)
    _detect_in_app(ua, d)
    return d


def _version_stamp(backend: Optional[_UapBackend]) -> Any:
    return "%d+uap-%s" % (UA_VERSION, backend.version) if backend else UA_VERSION


@lru_cache(maxsize=8192)
def _parse_cached(ua: str, hints: ClientHints, use_uap: bool) -> Tuple[Tuple[str, Any], ...]:
    d = _builtin_draft(ua)
    backend = _load_uap() if use_uap else None
    used_uap = bool(ua) and backend is not None and _apply_uap(ua, d, backend)
    _apply_frozen_versions(d)
    _apply_in_app(ua, d)
    _apply_hints(d, hints)
    _apply_bot(ua, d)
    result = (
        ("browser", d.browser),
        ("browser_version", d.browser_version),
        ("browser_major", _major(d.browser_version)),
        ("engine", d.engine),
        ("os", d.os),
        ("os_version", d.os_version),
        ("device_type", _device_type(d, hints)),
        ("device_brand", d.device_brand),
        ("device_model", d.device_model),
        ("is_webview", d.is_webview),
        ("in_app", d.in_app),
        ("is_bot_ua", d.is_bot_ua),
        ("bot_name", d.bot_name),
        ("parser", "uap" if used_uap else "builtin"),
        ("parser_version", _version_stamp(backend if used_uap else None)),
    )
    return result


def parse(
    ua: Optional[str],
    header_hints: Optional[Dict[str, str]] = None,
    js_hints: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Parse a User-Agent plus optional Client Hints into browser/OS/device fields.

    ``header_hints`` are request headers with lowercase names and raw values
    (``sec-ch-ua``, ``sec-ch-ua-mobile``, ``sec-ch-ua-platform``,
    ``sec-ch-ua-platform-version``, ``sec-ch-ua-model``,
    ``sec-ch-ua-full-version-list``, ``sec-ch-ua-form-factors``...); other
    headers are ignored. ``js_hints`` is the tracker's copy of
    ``navigator.userAgentData`` (``brands``, ``mobile``, ``platform``,
    ``platformVersion``, ``model``, ``fullVersionList``, ``formFactors``,
    ``uaFullVersion``...) plus an optional ``max_touch_points``; it wins over
    the headers. Never raises: garbage in gives None fields out.
    """
    if isinstance(ua, bytes):
        ua = ua.decode("utf-8", "replace")
    text = ua.strip()[:_MAX_UA_LENGTH] if isinstance(ua, str) else ""
    hints = _merge_hints(header_hints, js_hints)
    return dict(_parse_cached(text, hints, _active_backend() is not None))


def current_parser_version() -> Any:
    """The ``parser_version`` that :func:`parse` stamps with the active back end."""
    return _version_stamp(_active_backend())


def clear_cache() -> None:
    """Forget cached results and re-detect the optional uap back end."""
    _parse_cached.cache_clear()
    _UAP_STATE["loaded"] = False
    _UAP_STATE["backend"] = None
