"""Tests for tidesight.enrich.ua: User-Agent and Client Hints parsing.

The corpus runs against the built-in parser (TIDESIGHT_PURE_UA is set by an
autouse fixture) so results do not depend on whether uap-python happens to
be installed. The uap path has its own tests at the end.
"""

from __future__ import annotations

import sys
import types
from typing import Any, Dict

import pytest

from tidesight.enrich import ua as ua_mod

OUTPUT_KEYS = {
    "browser", "browser_version", "browser_major", "engine", "os", "os_version",
    "device_type", "device_brand", "device_model", "is_webview", "in_app",
    "is_bot_ua", "bot_name", "parser", "parser_version",
}


@pytest.fixture(autouse=True)
def builtin_parser(monkeypatch):
    """Force the built-in parser and start every test with a cold cache."""
    monkeypatch.setenv("TIDESIGHT_PURE_UA", "1")
    ua_mod.clear_cache()
    yield
    ua_mod.clear_cache()


def check(result: Dict[str, Any], expected: Dict[str, Any]) -> None:
    mismatches = {
        key: (result.get(key), value) for key, value in expected.items() if result.get(key) != value
    }
    assert not mismatches, "got != expected: %r\nfull result: %r" % (mismatches, result)


# Real-world User-Agent corpus ------------------------------------------------------------
# (id, user agent, expected subset of the output)

CORPUS = [
    # Desktop -----------------------------------------------------------------------------
    ("chrome-windows-reduced",
     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
     dict(browser="Chrome", browser_version="128.0.0.0", browser_major=128, engine="Blink", os="Windows",
          os_version="10", device_type="desktop", device_brand=None, is_webview=False, is_bot_ua=False)),
    ("edge-windows",
     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.2739.67",
     dict(browser="Edge", browser_version="128.0.2739.67", engine="Blink", os="Windows", device_type="desktop")),
    ("firefox-windows",
     "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0",
     dict(browser="Firefox", browser_version="131.0", browser_major=131, engine="Gecko", os="Windows",
          os_version="10", device_type="desktop")),
    ("opera-windows",
     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36 OPR/113.0.0.0",
     dict(browser="Opera", browser_major=113, engine="Blink", os="Windows")),
    ("yandex-windows",
     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 YaBrowser/24.7.0.0 Safari/537.36",
     dict(browser="Yandex Browser", browser_version="24.7.0.0", engine="Blink")),
    ("vivaldi-token",
     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/92.0.4515.131 Safari/537.36 Vivaldi/4.1.2369.21",
     dict(browser="Vivaldi", browser_version="4.1.2369.21")),
    ("whale",
     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Whale/3.26.244.21 Safari/537.36",
     dict(browser="Whale", browser_major=3)),
    ("coc-coc",
     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) coc_coc_browser/131.0.204 Chrome/125.0.6422.204 Safari/537.36",
     dict(browser="Coc Coc", browser_major=131)),
    ("ie11-windows7",
     "Mozilla/5.0 (Windows NT 6.1; WOW64; Trident/7.0; rv:11.0) like Gecko",
     dict(browser="Internet Explorer", browser_version="11.0", engine="Trident", os="Windows", os_version="7",
          device_type="desktop", is_bot_ua=False)),
    ("ie10-windows8",
     "Mozilla/5.0 (compatible; MSIE 10.0; Windows NT 6.2; Trident/6.0)",
     dict(browser="Internet Explorer", browser_version="10.0", engine="Trident", os_version="8", is_bot_ua=False)),
    ("edge-legacy",
     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/70.0.3538.102 Safari/537.36 Edge/18.19582",
     dict(browser="Edge", browser_version="18.19582", engine="EdgeHTML", os="Windows")),
    ("chrome-windows81",
     "Mozilla/5.0 (Windows NT 6.3; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Safari/537.36",
     dict(os="Windows", os_version="8.1")),
    ("firefox-windows-xp",
     "Mozilla/5.0 (Windows NT 5.1; rv:52.0) Gecko/20100101 Firefox/52.0",
     dict(browser="Firefox", os="Windows", os_version="XP")),
    ("opera-presto",
     "Opera/9.80 (Windows NT 6.1; WOW64) Presto/2.12.388 Version/12.18",
     dict(browser="Opera", browser_version="12.18", engine="Presto", os_version="7")),
    ("pale-moon",
     "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:102.0) Gecko/20100101 Goanna/6.7 Firefox/102.0 PaleMoon/33.3.1",
     dict(browser="Pale Moon", browser_version="33.3.1", engine="Gecko")),
    ("safari-macos",
     "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Safari/605.1.15",
     dict(browser="Safari", browser_version="18.0", engine="WebKit", os="macOS", os_version="10.15",
          device_type="desktop", device_brand="Apple", device_model="Mac", is_webview=False)),
    ("chrome-macos",
     "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
     dict(browser="Chrome", engine="Blink", os="macOS", os_version="10.15", device_type="desktop")),
    ("firefox-macos",
     "Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:131.0) Gecko/20100101 Firefox/131.0",
     dict(browser="Firefox", engine="Gecko", os="macOS", os_version="10.15")),
    ("safari-mojave",
     "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_14_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.2 Safari/605.1.15",
     dict(browser="Safari", os="macOS", os_version="10.14.6")),
    ("duckduckgo-macos",
     "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15 Ddg/17.5",
     dict(browser="DuckDuckGo", browser_version="17.5", os="macOS")),
    ("electron-slack",
     "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Slack/4.40.120 Chrome/128.0.6613.36 Electron/32.0.1 Safari/537.36 Sonic Slack_SSB/4.40.120",
     dict(browser="Electron", is_webview=True, is_bot_ua=False, os="macOS")),
    ("chrome-linux",
     "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
     dict(browser="Chrome", os="Linux", os_version=None, device_type="desktop")),
    ("firefox-ubuntu",
     "Mozilla/5.0 (X11; Ubuntu; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0",
     dict(browser="Firefox", os="Ubuntu", device_type="desktop")),
    ("firefox-fedora",
     "Mozilla/5.0 (X11; Fedora; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0",
     dict(os="Fedora", device_type="desktop")),
    ("firefox-freebsd",
     "Mozilla/5.0 (X11; FreeBSD amd64; rv:128.0) Gecko/20100101 Firefox/128.0",
     dict(os="FreeBSD", device_type="desktop")),
    ("chromeos-frozen",
     "Mozilla/5.0 (X11; CrOS x86_64 14541.0.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
     dict(browser="Chrome", os="ChromeOS", os_version=None, device_type="desktop")),
    # iOS and iPadOS ------------------------------------------------------------------------
    ("safari-iphone",
     "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
     dict(browser="Safari", browser_version="17.5", engine="WebKit", os="iOS", os_version="17.5.1",
          device_type="mobile", device_brand="Apple", device_model="iPhone", is_webview=False)),
    ("safari-iphone-ios26-frozen",
     "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/26.0 Mobile/15E148 Safari/604.1",
     dict(browser="Safari", browser_major=26, os="iOS", os_version="26.0")),
    ("safari-iphone-ios18",
     "Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1",
     dict(os="iOS", os_version="18.6")),
    ("chrome-ios",
     "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) CriOS/128.0.6613.98 Mobile/15E148 Safari/604.1",
     dict(browser="Chrome", browser_version="128.0.6613.98", engine="WebKit", os="iOS", os_version="17.5",
          device_type="mobile", is_webview=False)),
    ("firefox-ios",
     "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) FxiOS/129.0 Mobile/15E148 Safari/605.1.15",
     dict(browser="Firefox", browser_version="129.0", engine="WebKit", os="iOS")),
    ("edge-ios",
     "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 EdgiOS/128.0.2739.60 Mobile/15E148 Safari/605.1.15",
     dict(browser="Edge", browser_major=128, engine="WebKit", os="iOS")),
    ("opera-mini-ios",
     "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 OPiOS/16.0.15.124050 Mobile/15E148 Safari/9537.53",
     dict(browser="Opera Mini", browser_major=16, engine="WebKit", os="iOS")),
    ("duckduckgo-ios",
     "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 DuckDuckGo/7 Safari/605.1.15",
     dict(browser="DuckDuckGo", os="iOS", device_type="mobile")),
    ("safari-ipad",
     "Mozilla/5.0 (iPad; CPU OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1",
     dict(browser="Safari", os="iPadOS", os_version="17.5", device_type="tablet", device_brand="Apple",
          device_model="iPad")),
    ("safari-ipod",
     "Mozilla/5.0 (iPod touch; CPU iPhone OS 15_7 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.6 Mobile/15E148 Safari/604.1",
     dict(os="iOS", os_version="15.7", device_type="mobile", device_model="iPod touch")),
    ("ios-wkwebview",
     "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148",
     dict(browser="Safari WebView", is_webview=True, in_app=None, os="iOS", device_type="mobile")),
    # Android phones --------------------------------------------------------------------------
    ("chrome-android-samsung-full",
     "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Mobile Safari/537.36",
     dict(browser="Chrome", engine="Blink", os="Android", os_version="13", device_type="mobile",
          device_brand="Samsung", device_model="SM-S918B", is_webview=False)),
    ("chrome-android-reduced",
     "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36",
     dict(browser="Chrome", os="Android", os_version=None, device_type="mobile", device_brand=None,
          device_model=None)),
    ("chrome-android-reduced-tablet",
     "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
     dict(os="Android", device_type="tablet", device_model=None)),
    ("chrome-pixel",
     "Mozilla/5.0 (Linux; Android 14; Pixel 8 Pro) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.144 Mobile Safari/537.36",
     dict(os_version="14", device_brand="Google", device_model="Pixel 8 Pro", device_type="mobile")),
    ("samsung-internet",
     "Mozilla/5.0 (Linux; Android 14; SAMSUNG SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/26.0 Chrome/122.0.0.0 Mobile Safari/537.36",
     dict(browser="Samsung Internet", browser_version="26.0", device_brand="Samsung", device_model="SM-S928B",
          device_type="mobile")),
    ("firefox-android",
     "Mozilla/5.0 (Android 14; Mobile; rv:131.0) Gecko/131.0 Firefox/131.0",
     dict(browser="Firefox", engine="Gecko", os="Android", os_version="14", device_type="mobile",
          device_model=None)),
    ("edge-android",
     "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Mobile Safari/537.36 EdgA/128.0.0.0",
     dict(browser="Edge", os="Android", device_type="mobile")),
    ("opera-android",
     "Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.0 Mobile Safari/537.36 OPR/84.0.0.0",
     dict(browser="Opera", browser_major=84)),
    ("uc-browser-realme",
     "Mozilla/5.0 (Linux; U; Android 11; en-US; RMX2193 Build/RP1A.200720.011) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/100.0.4896.58 UCBrowser/13.4.0.1306 Mobile Safari/537.36",
     dict(browser="UC Browser", browser_version="13.4.0.1306", os_version="11", device_brand="Realme",
          device_model="RMX2193", is_webview=False)),
    ("miui-browser-xiaomi",
     "Mozilla/5.0 (Linux; U; Android 13; en-us; 2201116SG Build/TKQ1.221114.001) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/112.0.5615.136 Mobile Safari/537.36 XiaoMi/MiuiBrowser/14.10.1-gn",
     dict(browser="MIUI Browser", device_brand="Xiaomi", device_model="2201116SG", is_webview=False)),
    ("huawei-browser-harmonyos",
     "Mozilla/5.0 (Linux; Android 10; HarmonyOS; NOH-AN00; HMSCore 6.11.0.302) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/99.0.4844.88 HuaweiBrowser/14.0.0.320 Mobile Safari/537.36",
     dict(browser="Huawei Browser", os="HarmonyOS", device_brand="Huawei", device_model="NOH-AN00",
          device_type="mobile")),
    ("harmonyos-next",
     "Mozilla/5.0 (Phone; OpenHarmony 5.0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36 ArkWeb/4.1.6.1 Mobile HuaweiBrowser/5.0.4.300",
     dict(browser="Huawei Browser", engine="Blink", os="HarmonyOS", os_version="5.0", device_type="mobile",
          device_brand="Huawei")),
    ("yandex-android",
     "Mozilla/5.0 (Linux; arm_64; Android 14; SM-A546B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 YaBrowser/24.6.1.71.00 SA/3 Mobile Safari/537.36",
     dict(browser="Yandex Browser", device_brand="Samsung", device_model="SM-A546B", device_type="mobile")),
    ("duckduckgo-android",
     "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/128.0.0.0 Mobile DuckDuckGo/5 Safari/537.36",
     dict(browser="DuckDuckGo", os="Android", os_version="14", is_webview=False, device_type="mobile")),
    ("redmi",
     "Mozilla/5.0 (Linux; Android 12; Redmi Note 11) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/109.0.0.0 Mobile Safari/537.36",
     dict(device_brand="Xiaomi", device_model="Redmi Note 11")),
    ("poco",
     "Mozilla/5.0 (Linux; Android 13; POCO F5) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36",
     dict(device_brand="Xiaomi", device_model="POCO F5")),
    ("oneplus",
     "Mozilla/5.0 (Linux; Android 13; LE2123) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Mobile Safari/537.36",
     dict(device_brand="OnePlus", device_model="LE2123")),
    ("oppo",
     "Mozilla/5.0 (Linux; Android 13; CPH2581) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36",
     dict(device_brand="Oppo", device_model="CPH2581")),
    ("vivo",
     "Mozilla/5.0 (Linux; Android 13; V2302) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36",
     dict(device_brand="Vivo", device_model="V2302")),
    ("motorola",
     "Mozilla/5.0 (Linux; Android 12; moto g power (2022)) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/108.0.0.0 Mobile Safari/537.36",
     dict(device_brand="Motorola", device_model="moto g power (2022)", device_type="mobile")),
    ("nokia",
     "Mozilla/5.0 (Linux; Android 11; Nokia G20) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Mobile Safari/537.36",
     dict(device_brand="Nokia", device_model="Nokia G20")),
    ("sony-xperia",
     "Mozilla/5.0 (Linux; Android 13; XQ-CT54) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36",
     dict(device_brand="Sony", device_model="XQ-CT54")),
    ("lg-velvet",
     "Mozilla/5.0 (Linux; Android 10; LM-G900) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/107.0.0.0 Mobile Safari/537.36",
     dict(device_brand="LG", device_model="LM-G900")),
    ("huawei-p30",
     "Mozilla/5.0 (Linux; Android 10; ELE-L29) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Mobile Safari/537.36",
     dict(os="Android", device_brand="Huawei", device_model="ELE-L29")),
    ("honor",
     "Mozilla/5.0 (Linux; Android 13; HONOR X8a) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Mobile Safari/537.36",
     dict(device_brand="Honor", device_model="HONOR X8a")),
    ("cubot-is-not-a-bot",
     "Mozilla/5.0 (Linux; Android 10; CUBOT X30) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.6099.230 Mobile Safari/537.36",
     dict(is_bot_ua=False, bot_name=None, device_type="mobile", device_brand="Cubot", device_model="CUBOT X30")),
    ("android-stock-browser",
     "Mozilla/5.0 (Linux; U; Android 4.0.3; ko-kr; LG-L160L Build/IML74K) AppleWebKit/534.30 (KHTML, like Gecko) Version/4.0 Mobile Safari/534.30",
     dict(browser="Android Browser", browser_version="4.0", engine="WebKit", os="Android", os_version="4.0.3",
          device_brand="LG", device_model="LG-L160L", device_type="mobile")),
    ("android-webview-generic",
     "Mozilla/5.0 (Linux; Android 13; SM-A536B Build/TP1A.220624.014; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/118.0.5993.111 Mobile Safari/537.36",
     dict(browser="Android WebView", browser_version="118.0.5993.111", is_webview=True, in_app=None,
          device_brand="Samsung", device_model="SM-A536B", device_type="mobile")),
    ("opera-mini-j2me",
     "Opera/9.80 (J2ME/MIDP; Opera Mini/5.1.21214/28.2725; U; ru) Presto/2.8.119 Version/11.10",
     dict(browser="Opera Mini", browser_major=5, engine="Presto", device_type="mobile")),
    ("kaios",
     "Mozilla/5.0 (Mobile; LYF/F300B/LYF-F300B-001-01-15-130718-i;Android; rv:48.0) Gecko/48.0 Firefox/48.0 KAIOS/2.5",
     dict(browser="Firefox", engine="Gecko", os="KaiOS", os_version="2.5", device_type="mobile")),
    ("windows-phone",
     "Mozilla/5.0 (Windows Phone 10.0; Android 6.0.1; Microsoft; Lumia 950) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/52.0.2743.116 Mobile Safari/537.36 Edge/15.15063",
     dict(browser="Edge", engine="EdgeHTML", os="Windows Phone", os_version="10.0", device_type="mobile",
          device_brand="Microsoft", device_model="Lumia 950")),
    ("blackberry-bb10",
     "Mozilla/5.0 (BB10; Touch) AppleWebKit/537.35+ (KHTML, like Gecko) Version/10.3.3.2205 Mobile Safari/537.35+",
     dict(browser="BlackBerry Browser", os="BlackBerry OS", device_type="mobile", device_brand="BlackBerry")),
    # Tablets ------------------------------------------------------------------------------------
    ("kindle-fire-silk",
     "Mozilla/5.0 (Linux; Android 11; KFTRWI) AppleWebKit/537.36 (KHTML, like Gecko) Silk/128.3.1 like Chrome/128.0.6613.137 Safari/537.36",
     dict(browser="Silk", browser_version="128.3.1", os="Fire OS", os_version="8", device_type="tablet",
          device_brand="Amazon", device_model="KFTRWI")),
    ("lenovo-tab",
     "Mozilla/5.0 (Linux; Android 11; Lenovo TB-J606F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
     dict(device_type="tablet", device_brand="Lenovo", device_model="Lenovo TB-J606F")),
    ("pixel-tablet",
     "Mozilla/5.0 (Linux; Android 14; Pixel Tablet) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
     dict(device_type="tablet", device_brand="Google", device_model="Pixel Tablet")),
    ("galaxy-tab",
     "Mozilla/5.0 (Linux; Android 13; SM-X200) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
     dict(device_type="tablet", device_brand="Samsung", device_model="SM-X200")),
    ("samsung-internet-tablet",
     "Mozilla/5.0 (Linux; Android 14; SAMSUNG SM-X710) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/25.0 Chrome/121.0.0.0 Safari/537.36",
     dict(browser="Samsung Internet", device_type="tablet", device_model="SM-X710")),
    ("firefox-android-tablet",
     "Mozilla/5.0 (Android 13; Tablet; rv:128.0) Gecko/128.0 Firefox/128.0",
     dict(browser="Firefox", device_type="tablet")),
    # TVs ------------------------------------------------------------------------------------------
    ("samsung-tizen-tv",
     "Mozilla/5.0 (SMART-TV; LINUX; Tizen 6.5) AppleWebKit/537.36 (KHTML, like Gecko) 85.0.4183.93/6.5 TV Safari/537.36",
     dict(browser="Tizen Browser", engine="Blink", os="Tizen", os_version="6.5", device_type="tv",
          device_brand="Samsung")),
    ("lg-webos-tv",
     "Mozilla/5.0 (Web0S; Linux/SmartTV) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/94.0.4606.128 Safari/537.36 WebAppManager",
     dict(os="webOS", device_type="tv", device_brand="LG")),
    ("fire-tv",
     "Mozilla/5.0 (Linux; Android 9; AFTKA Build/PS7633.3445N; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/126.0.6478.188 Mobile Safari/537.36",
     dict(os="Fire OS", os_version="7", device_type="tv", device_brand="Amazon", device_model="AFTKA",
          browser="Android WebView")),
    ("chromecast",
     "Mozilla/5.0 (X11; Linux armv7l) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.5735.198 Safari/537.36 CrKey/1.56.500000",
     dict(os="Chromecast", device_type="tv", device_brand="Google", device_model="Chromecast")),
    ("roku",
     "Roku/DVP-12.5 (12.5.0.4174-56)",
     dict(os="Roku OS", os_version="12.5", device_type="tv", device_brand="Roku", is_bot_ua=False)),
    ("sony-bravia-android-tv",
     "Mozilla/5.0 (Linux; Android 10; BRAVIA 4K VH2 Build/QTG3.200305.006.S292; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/122.0.6261.119 Mobile Safari/537.36",
     dict(os="Android", device_type="tv", device_brand="Sony", device_model="BRAVIA 4K VH2")),
    ("hbbtv-panasonic",
     "HbbTV/1.5.1 (+DRM;Panasonic;SmartTV2019;3.022;4401-0003 0008-0000;SmartTV2019;)",
     dict(device_type="tv", device_brand="Panasonic", is_bot_ua=False)),
    ("apple-tv",
     "AppleCoreMedia/1.0.0.21J354 (Apple TV; U; CPU OS 17_0 like Mac OS X; en_us)",
     dict(os="tvOS", os_version="17.0", device_type="tv", device_brand="Apple", device_model="Apple TV")),
    # Consoles, XR, wearables, cars ------------------------------------------------------------------
    ("playstation-5",
     "Mozilla/5.0 (PlayStation; PlayStation 5/2.26) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.0 Safari/605.1.15",
     dict(os="PlayStation", os_version="2.26", device_type="console", device_brand="Sony",
          device_model="PlayStation 5")),
    ("playstation-4",
     "Mozilla/5.0 (PlayStation 4 11.00) AppleWebKit/605.1.15 (KHTML, like Gecko)",
     dict(os="PlayStation", os_version="11.00", device_type="console", device_model="PlayStation 4")),
    ("playstation-vita",
     "Mozilla/5.0 (PlayStation Vita 3.74) AppleWebKit/537.73 (KHTML, like Gecko) Silk/3.2",
     dict(os="PlayStation", os_version="3.74", device_type="console", device_model="PlayStation Vita",
          browser=None)),
    ("xbox-series-x",
     "Mozilla/5.0 (Windows NT 10.0; Win64; x64; Xbox; Xbox Series X) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/48.0.2564.82 Safari/537.36 Edge/20.02",
     dict(browser="Edge", engine="EdgeHTML", os="Xbox", device_type="console", device_brand="Microsoft",
          device_model="Xbox Series X")),
    ("nintendo-switch",
     "Mozilla/5.0 (Nintendo Switch; WifiWebAuthApplet) AppleWebKit/606.4 (KHTML, like Gecko) NF/6.0.1.15.4 NintendoBrowser/5.1.0.20393",
     dict(browser="Nintendo Browser", os="Nintendo", device_type="console", device_brand="Nintendo",
          device_model="Switch")),
    ("meta-quest-3",
     "Mozilla/5.0 (X11; Linux x86_64; Quest 3) AppleWebKit/537.36 (KHTML, like Gecko) OculusBrowser/35.0.0.0.32 SamsungBrowser/4.0 Chrome/126.0.6478.122 VR Safari/537.36",
     dict(browser="Meta Quest Browser", os="Horizon OS", device_type="wearable", device_brand="Meta",
          device_model="Quest 3")),
    ("galaxy-watch",
     "Mozilla/5.0 (Linux; Android 11; SAMSUNG SM-R890) AppleWebKit/537.36 (KHTML, like Gecko) SamsungBrowser/2.0 Chrome/84.0.4147.125 Mobile Safari/537.36",
     dict(os="Wear OS", device_type="wearable", device_brand="Samsung", device_model="SM-R890")),
    ("tesla",
     "Mozilla/5.0 (X11; GNU/Linux) AppleWebKit/537.36 (KHTML, like Gecko) Chromium/79.0.3945.130 Chrome/79.0.3945.130 Safari/537.36 Tesla/2024.20.9-ea2c8f8c4b5f",
     dict(browser="Chromium", os="Linux", device_type="car", device_brand="Tesla")),
    # In-app browsers ------------------------------------------------------------------------------
    ("facebook-android",
     "Mozilla/5.0 (Linux; Android 14; SM-S918B Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/126.0.6478.122 Mobile Safari/537.36 [FB_IAB/FB4A;FBAV/471.0.0.35.80;]",
     dict(in_app="Facebook", browser="Android WebView", browser_major=126, is_webview=True,
          device_brand="Samsung", device_model="SM-S918B", device_type="mobile")),
    ("facebook-ios",
     "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 [FBAN/FBIOS;FBAV/470.0.0.40.98;FBBV/628553946;FBDV/iPhone15,2;FBMD/iPhone;FBSN/iOS;FBSV/17.5.1;FBSS/3;FBID/phone;FBLC/en_US;FBOP/5;FBRV/629958312]",
     dict(in_app="Facebook", browser="Safari WebView", is_webview=True, os="iOS", os_version="17.5.1",
          device_type="mobile")),
    ("messenger-ios",
     "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 [FBAN/MessengerForiOS;FBAV/466.0.0.36.106;FBBV/620361183;FBDV/iPhone14,7;FBMD/iPhone;FBSN/iOS;FBSV/17.5;FBSS/3;FBCR/;FBID/phone;FBLC/en_US;FBOP/5]",
     dict(in_app="Messenger", browser="Safari WebView", is_webview=True)),
    ("messenger-android",
     "Mozilla/5.0 (Linux; Android 14; Pixel 7 Build/AP2A.240805.005; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/127.0.6533.103 Mobile Safari/537.36 [FB_IAB/MESSENGER;FBAV/469.0.0.43.109;]",
     dict(in_app="Messenger", browser="Android WebView", device_brand="Google", device_model="Pixel 7")),
    ("instagram-android",
     "Mozilla/5.0 (Linux; Android 14; SM-S918B Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/126.0.6478.71 Mobile Safari/537.36 Instagram 337.0.0.35.102 Android (34/14; 450dpi; 1080x2340; samsung; SM-S918B; dm3q; qcom; en_US; 621425578)",
     dict(in_app="Instagram", browser="Android WebView", is_webview=True, device_model="SM-S918B")),
    ("instagram-ios",
     "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Instagram 341.0.3.20.98 (iPhone15,3; iOS 17_5; en_US; en; scale=3.00; 1290x2796; 621640227; IABMV/1)",
     dict(in_app="Instagram", browser="Safari WebView", os="iOS", os_version="17.5")),
    ("threads-android",
     "Mozilla/5.0 (Linux; Android 14; SM-S918B Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/126.0.6478.134 Mobile Safari/537.36 Barcelona 339.0.0.37.110 Android (34/14; 450dpi; 1080x2340; samsung; SM-S918B; dm3q; qcom; en_US; 621950000)",
     dict(in_app="Threads", browser="Android WebView")),
    ("linkedin-ios",
     "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 [LinkedInApp]/9.29.5463",
     dict(in_app="LinkedIn", browser="Safari WebView", is_bot_ua=False)),
    ("tiktok-android",
     "Mozilla/5.0 (Linux; Android 14; SM-A546B Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/126.0.6478.71 Mobile Safari/537.36 trill_350402 JsSdk/1.0 NetType/WIFI Channel/googleplay AppName/trill app_version/35.4.2 ByteLocale/en ByteFullLocale/en Region/IN AppId/1180 Spark/1.5.8.6-bugfix AppVersion/35.4.2 BytedanceWebview/d8a21c6",
     dict(in_app="TikTok", browser="Android WebView", device_brand="Samsung")),
    ("tiktok-ios",
     "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 musical_ly_35.4.0 JsSdk/2.0 NetType/WIFI Channel/App Store ByteLocale/en Region/US RevealType/Dialog isDarkMode/0 WKWebView/1 BytedanceWebview/d8a21c6 FalconTag/",
     dict(in_app="TikTok", browser="Safari WebView")),
    ("snapchat-ios",
     "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Snapchat/13.2.0.36 (like Safari/8618.2.12.10.4, panda)",
     dict(in_app="Snapchat", browser="Safari WebView", is_webview=True)),
    ("pinterest-android",
     "Mozilla/5.0 (Linux; Android 14; SM-G991B Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/126.0.6478.134 Mobile Safari/537.36 [Pinterest/Android]",
     dict(in_app="Pinterest", browser="Android WebView", is_bot_ua=False)),
    ("wechat-android",
     "Mozilla/5.0 (Linux; Android 13; V2244A Build/TP1A.220624.014; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/116.0.0.0 Mobile Safari/537.36 XWEB/1160065 MMWEBSDK/20231202 MMWEBID/2247 MicroMessenger/8.0.47.2560(0x28002F30) WeChat/arm64 Weixin NetType/WIFI Language/zh_CN ABI/arm64",
     dict(in_app="WeChat", browser="Android WebView", device_brand="Vivo", device_model="V2244A")),
    ("line-ios",
     "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Safari Line/14.10.0",
     dict(in_app="Line", browser="Safari WebView")),
    ("telegram-android",
     "Mozilla/5.0 (Linux; Android 14; SM-S911B Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/127.0.6533.103 Mobile Safari/537.36 Telegram-Android/11.0.0 (Samsung SM-S911B; Android 14; SDK 34; HIGH)",
     dict(in_app="Telegram", browser="Android WebView", device_model="SM-S911B")),
    ("google-app-ios",
     "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) GSA/330.0.665236494 Mobile/15E148 Safari/604.1",
     dict(in_app="Google App", browser="Safari WebView", is_webview=True)),
    ("google-app-android",
     "Mozilla/5.0 (Linux; Android 14; Pixel 8 Build/AP2A.240705.005; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/126.0.6478.122 Mobile Safari/537.36 GSA/15.26.33.29.arm64",
     dict(in_app="Google App", browser="Android WebView", device_brand="Google", device_model="Pixel 8")),
    ("twitter-ios",
     "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Twitter for iPhone/10.4",
     dict(in_app="Twitter", browser="Safari WebView")),
    # Gmail's in-app token varies between app versions; this is its usual shape.
    ("gmail-ios",
     "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 Gmail/6.0.240428",
     dict(in_app="Gmail", browser="Safari WebView")),
    # Bots (UA patterns delegated to bots.py) -------------------------------------------------------
    ("googlebot-smartphone",
     "Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.6613.119 Mobile Safari/537.36 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
     dict(is_bot_ua=True, bot_name="Googlebot", device_type="bot", browser="Googlebot", browser_version="2.1",
          os="Android", device_brand=None, device_model=None)),
    ("bingbot",
     "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm) Chrome/116.0.1938.76 Safari/537.36",
     dict(is_bot_ua=True, bot_name="Bingbot", device_type="bot")),
    ("gptbot",
     "Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; GPTBot/1.1; +https://openai.com/gptbot",
     dict(is_bot_ua=True, bot_name="GPTBot", device_type="bot", browser_version="1.1")),
    ("curl",
     "curl/8.4.0",
     dict(is_bot_ua=True, bot_name="curl", browser="curl", browser_version="8.4.0", device_type="bot")),
    ("headless-chrome",
     "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/120.0.6099.109 Safari/537.36",
     dict(is_bot_ua=True, bot_name="HeadlessChrome", browser="HeadlessChrome", browser_major=120,
          device_type="bot", os="Linux")),
    ("lighthouse",
     "Mozilla/5.0 (Linux; Android 11; moto g power (2022)) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Mobile Safari/537.36 Chrome-Lighthouse",
     dict(is_bot_ua=True, bot_name="Lighthouse", device_type="bot")),
    ("facebookexternalhit",
     "facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)",
     dict(is_bot_ua=True, bot_name="facebookexternalhit", device_type="bot")),
]


@pytest.mark.parametrize("ua,expected", [(ua, exp) for _, ua, exp in CORPUS], ids=[c[0] for c in CORPUS])
def test_corpus(ua: str, expected: Dict[str, Any]) -> None:
    result = ua_mod.parse(ua)
    check(result, expected)
    assert result["parser"] == "builtin"
    assert result["parser_version"] == ua_mod.UA_VERSION


def test_corpus_is_large_and_unique() -> None:
    assert len(CORPUS) >= 80
    assert len({ua for _, ua, _ in CORPUS}) == len(CORPUS)


@pytest.mark.parametrize("ua", [c[1] for c in CORPUS], ids=[c[0] for c in CORPUS])
def test_output_shape(ua: str) -> None:
    result = ua_mod.parse(ua)
    assert set(result) == OUTPUT_KEYS
    assert result["device_type"] in ua_mod.DEVICE_TYPES
    assert isinstance(result["is_webview"], bool) and isinstance(result["is_bot_ua"], bool)
    if result["browser_version"] is not None and result["browser_major"] is not None:
        assert result["browser_version"].startswith(str(result["browser_major"]))


# Client Hints -------------------------------------------------------------------------------

CHROME_WIN = CORPUS[0][1]
CHROME_MAC = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/128.0.0.0 Safari/537.36")
SAFARI_MAC = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
              "Version/17.5 Safari/605.1.15")
ANDROID_REDUCED = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/128.0.0.0 Mobile Safari/537.36")
ANDROID_REDUCED_TABLET = ANDROID_REDUCED.replace(" Mobile Safari", " Safari")
CHROME_BRANDS = '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"'


def windows_headers(platform_version: str) -> Dict[str, str]:
    return {
        "sec-ch-ua": CHROME_BRANDS,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-ch-ua-platform-version": '"%s"' % platform_version,
    }


@pytest.mark.parametrize("platform_version,expected", [
    ("15.0.0", "11"), ("13.0.0", "11"), ("19.0.0", "11"), ("10.0.0", "10"), ("1.0.0", "10"),
    ("0.1.0", "7"), ("0.2.0", "8"), ("0.3.0", "8.1"),
])
def test_windows_version_from_header_hints(platform_version: str, expected: str) -> None:
    result = ua_mod.parse(CHROME_WIN, header_hints=windows_headers(platform_version))
    check(result, dict(os="Windows", os_version=expected, browser="Chrome", device_type="desktop"))


def test_windows_without_hints_stays_10() -> None:
    assert ua_mod.parse(CHROME_WIN)["os_version"] == "10"


def test_unknown_windows_hint_keeps_ua_version() -> None:
    result = ua_mod.parse(CHROME_WIN, header_hints=windows_headers("0.0.0"))
    assert result["os_version"] == "10"


def test_js_hints_win_over_header_hints() -> None:
    js = {"platform": "Windows", "platformVersion": "15.0.0", "mobile": False}
    result = ua_mod.parse(CHROME_WIN, header_hints=windows_headers("10.0.0"), js_hints=js)
    assert result["os_version"] == "11"


@pytest.mark.parametrize("hints,expected", [
    ({"js_hints": {"platform": "macOS", "platformVersion": "14.6.1"}}, "14.6.1"),
    ({"js_hints": {"platform": "macOS", "platformVersion": "15.0.0"}}, "15.0"),
    ({"header_hints": {"sec-ch-ua-platform": '"macOS"', "sec-ch-ua-platform-version": '"26.0.1"'}}, "26.0.1"),
])
def test_macos_version_from_hints(hints: Dict[str, Any], expected: str) -> None:
    result = ua_mod.parse(CHROME_MAC, **hints)
    check(result, dict(os="macOS", os_version=expected, device_type="desktop"))


def test_android_model_and_version_from_header_hints() -> None:
    headers = {
        "sec-ch-ua": CHROME_BRANDS,
        "sec-ch-ua-mobile": "?1",
        "sec-ch-ua-platform": '"Android"',
        "sec-ch-ua-platform-version": '"14.0.0"',
        "sec-ch-ua-model": '"Pixel 7"',
    }
    result = ua_mod.parse(ANDROID_REDUCED, header_hints=headers)
    check(result, dict(os="Android", os_version="14", device_model="Pixel 7", device_brand="Google",
                       device_type="mobile", browser="Chrome"))


def test_android_model_from_js_hints() -> None:
    js = {"platform": "Android", "platformVersion": "13.0.0", "model": "SM-S918B", "mobile": True}
    result = ua_mod.parse(ANDROID_REDUCED, js_hints=js)
    check(result, dict(os_version="13", device_brand="Samsung", device_model="SM-S918B", device_type="mobile"))


def test_empty_model_hint_is_ignored() -> None:
    result = ua_mod.parse(CHROME_WIN, header_hints={"sec-ch-ua-model": '""'})
    assert result["device_model"] is None


def test_full_version_list_adds_precision() -> None:
    headers = {
        "sec-ch-ua": CHROME_BRANDS,
        "sec-ch-ua-full-version-list":
            '"Chromium";v="128.0.6613.120", "Not;A=Brand";v="24.0.0.0", "Google Chrome";v="128.0.6613.120"',
    }
    result = ua_mod.parse(CHROME_WIN, header_hints=headers)
    check(result, dict(browser="Chrome", browser_version="128.0.6613.120", browser_major=128))


def test_ua_full_version_fallback() -> None:
    result = ua_mod.parse(CHROME_WIN, js_hints={"uaFullVersion": "128.0.6613.84"})
    assert result["browser_version"] == "128.0.6613.84"


@pytest.mark.parametrize("brands,expected", [
    ([("Brave", "128"), ("Chromium", "128"), ("Not;A=Brand", "24")], "Brave"),
    ([("Not)A;Brand", "99"), ("Opera GX", "113"), ("Chromium", "127")], "Opera GX"),
    ([("Chromium", "128"), ("Not_A Brand", "8")], "Chromium"),
    ([(" Not A;Brand", "99"), ("Chromium", "96"), ("Google Chrome", "96")], "Chrome"),
    ([("Not/A)Brand", "8"), ("Chromium", "126"), ("Microsoft Edge", "126")], "Edge"),
    ([("Avast Secure Browser", "127"), ("Chromium", "127"), ("Not.A/Brand", "24")], "Avast Secure Browser"),
])
def test_browser_from_brands(brands, expected) -> None:
    js = {"brands": [{"brand": b, "version": v} for b, v in brands]}
    assert ua_mod.parse(CHROME_WIN, js_hints=js)["browser"] == expected
    header = ", ".join('"%s";v="%s"' % (b, v) for b, v in brands)
    assert ua_mod.parse(CHROME_WIN, header_hints={"sec-ch-ua": header})["browser"] == expected


def test_opera_gx_brand_refines_opera() -> None:
    ua = CORPUS[3][1]
    result = ua_mod.parse(ua, header_hints={"sec-ch-ua": '"Opera GX";v="113", "Chromium";v="127", "Not)A;Brand";v="99"'})
    assert result["browser"] == "Opera GX"


def test_android_webview_brand() -> None:
    js = {"brands": [{"brand": "Android WebView", "version": "128"}, {"brand": "Chromium", "version": "128"}]}
    result = ua_mod.parse(ANDROID_REDUCED, js_hints=js)
    check(result, dict(browser="Android WebView", is_webview=True))


def test_edge_webview2_brand() -> None:
    headers = {"sec-ch-ua": '"Microsoft Edge WebView2";v="128", "Microsoft Edge";v="128", '
                            '"Chromium";v="128", "Not;A=Brand";v="24"'}
    result = ua_mod.parse(CORPUS[1][1], header_hints=headers)
    check(result, dict(browser="Edge WebView2", is_webview=True))


def test_brand_does_not_override_specific_ua_browser() -> None:
    samsung = [c[1] for c in CORPUS if c[0] == "samsung-internet"][0]
    result = ua_mod.parse(samsung, header_hints={"sec-ch-ua": CHROME_BRANDS})
    assert result["browser"] == "Samsung Internet"


@pytest.mark.parametrize("value", [
    '"Chromium";v="128", "Not;A=Brand";v="24", "Google Chrome";v="128"',
    '"Google Chrome";v="128", "Not=A?Brand";v="8", "Chromium";v="128"',
    '"Not:A-Brand";v="99", "Google Chrome";v="128", "Chromium";v="128"',
    '"Google Chrome" ; v="128" ,"Chromium";v="128"',
])
def test_grease_variants_are_ignored(value: str) -> None:
    assert ua_mod.parse(None, header_hints={"sec-ch-ua": value})["browser"] == "Chrome"


def test_ipados_desktop_mode_from_touch_points() -> None:
    result = ua_mod.parse(SAFARI_MAC, js_hints={"max_touch_points": 5})
    check(result, dict(os="iPadOS", os_version="17.5", device_type="tablet", device_brand="Apple",
                       device_model="iPad", browser="Safari", engine="WebKit"))


@pytest.mark.parametrize("touch", [0, 1, None])
def test_real_mac_is_not_an_ipad(touch) -> None:
    result = ua_mod.parse(SAFARI_MAC, js_hints={"max_touch_points": touch})
    check(result, dict(os="macOS", os_version="10.15", device_type="desktop", device_model="Mac"))


def test_ipados_accepts_camel_case_touch_key() -> None:
    assert ua_mod.parse(SAFARI_MAC, js_hints={"maxTouchPoints": 5})["device_type"] == "tablet"


@pytest.mark.parametrize("factors,expected", [
    (["Tablet"], "tablet"), (["Mobile"], "mobile"), (["Desktop"], "desktop"),
    (["Watch"], "wearable"), (["Automotive"], "car"), (["XR"], "wearable"), (["EInk"], "tablet"),
])
def test_form_factors(factors, expected) -> None:
    assert ua_mod.parse(ANDROID_REDUCED_TABLET, js_hints={"formFactors": factors})["device_type"] == expected


def test_form_factors_header() -> None:
    result = ua_mod.parse(ANDROID_REDUCED, header_hints={"sec-ch-ua-form-factors": '"Tablet"'})
    assert result["device_type"] == "tablet"


def test_mobile_hint() -> None:
    headers = {"sec-ch-ua": CHROME_BRANDS, "sec-ch-ua-mobile": "?1", "sec-ch-ua-platform": '"Android"'}
    result = ua_mod.parse("", header_hints=headers)
    check(result, dict(browser="Chrome", os="Android", device_type="mobile", engine="Blink"))


def test_platform_hint_corrects_desktop_mode_android() -> None:
    linux = CORPUS[[c[0] for c in CORPUS].index("chrome-linux")][1]
    result = ua_mod.parse(linux, js_hints={"platform": "Android", "platformVersion": "14.0.0", "mobile": False})
    check(result, dict(os="Android", os_version="14"))


def test_platform_hint_keeps_specific_subfamily() -> None:
    ubuntu = [c[1] for c in CORPUS if c[0] == "firefox-ubuntu"][0]
    assert ua_mod.parse(ubuntu, js_hints={"platform": "Linux"})["os"] == "Ubuntu"
    xbox = [c[1] for c in CORPUS if c[0] == "xbox-series-x"][0]
    result = ua_mod.parse(xbox, header_hints={"sec-ch-ua-platform": '"Windows"'})
    check(result, dict(os="Xbox", device_type="console"))


def test_chromeos_version_from_hint() -> None:
    cros = [c[1] for c in CORPUS if c[0] == "chromeos-frozen"][0]
    result = ua_mod.parse(cros, js_hints={"platform": "Chrome OS", "platformVersion": "16002.44.0"})
    check(result, dict(os="ChromeOS", os_version="16002.44.0"))


def test_header_names_are_case_insensitive() -> None:
    headers = {"Sec-CH-UA-Platform": '"Windows"', "Sec-CH-UA-Platform-Version": '"15.0.0"'}
    assert ua_mod.parse(CHROME_WIN, header_hints=headers)["os_version"] == "11"


@pytest.mark.parametrize("headers", [
    {"sec-ch-ua": '"unterminated', "sec-ch-ua-mobile": "maybe", "sec-ch-ua-platform": "Windows"},
    {"sec-ch-ua": ";;;,,,===", "sec-ch-ua-platform-version": "not a version"},
    {"sec-ch-ua": 42, "sec-ch-ua-model": None},
    "not a dict",
    None,
])
def test_malformed_header_hints(headers) -> None:
    result = ua_mod.parse(CHROME_WIN, header_hints=headers)
    assert result["browser"] == "Chrome" and result["os"] == "Windows"


def test_unquoted_platform_is_tolerated() -> None:
    headers = {"sec-ch-ua-platform": "Windows", "sec-ch-ua-platform-version": "15.0.0"}
    assert ua_mod.parse(CHROME_WIN, header_hints=headers)["os_version"] == "11"


@pytest.mark.parametrize("js", [
    {"brands": "nope", "mobile": "yes", "platformVersion": 15, "formFactors": "Tablet"},
    {"brands": [None, 3, {"brand": None}, {"version": "1"}], "fullVersionList": {}},
    {"max_touch_points": "five", "model": ["x"]},
    ["not", "a", "dict"],
])
def test_malformed_js_hints(js) -> None:
    result = ua_mod.parse(CHROME_WIN, js_hints=js)
    check(result, dict(browser="Chrome", os="Windows", os_version="10", device_type="desktop"))


# Robustness and caching --------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "   ", 12345, 3.5, ["Mozilla"], {"ua": 1}])
def test_missing_or_non_string_ua(value) -> None:
    result = ua_mod.parse(value)
    assert set(result) == OUTPUT_KEYS
    check(result, dict(browser=None, os=None, device_type="unknown", is_bot_ua=False, is_webview=False,
                       parser="builtin", parser_version=ua_mod.UA_VERSION))


@pytest.mark.parametrize("value", [
    "\x00\x01\x02 garbage \xff",
    "(((((((((((((",
    "Mozilla/5.0 (" + ";" * 500,
    "A" * 20000,
    "Mozilla/5.0 (Linux; Android 99999999999999999999; ) Chrome/999999999999999999999.0",
])
def test_garbage_ua_never_raises(value: str) -> None:
    result = ua_mod.parse(value)
    assert set(result) == OUTPUT_KEYS
    assert result["device_type"] in ua_mod.DEVICE_TYPES


def test_bytes_ua_is_decoded() -> None:
    assert ua_mod.parse(CHROME_WIN.encode("utf-8"))["browser"] == "Chrome"


def test_results_are_independent_copies() -> None:
    first = ua_mod.parse(CHROME_WIN)
    first["browser"] = "tampered"
    assert ua_mod.parse(CHROME_WIN)["browser"] == "Chrome"


def test_parse_is_deterministic_and_cached() -> None:
    hints = windows_headers("15.0.0")
    assert ua_mod.parse(CHROME_WIN, header_hints=hints) == ua_mod.parse(CHROME_WIN, header_hints=dict(hints))
    info = ua_mod._parse_cached.cache_info()
    assert info.hits >= 1


# Back-end selection ----------------------------------------------------------------------------


def make_fake_uap(monkeypatch, *, legacy: bool = False, broken: bool = False) -> None:
    """Install a stand-in ua_parser module (no third-party code needed)."""
    module = types.ModuleType("ua_parser")
    module.VERSION = (9, 9, 9)

    def agent(family, major=None, minor=None):
        return types.SimpleNamespace(family=family, major=major, minor=minor, patch=None, patch_minor=None)

    def modern_parse(ua: str):
        if broken:
            raise RuntimeError("boom")
        return types.SimpleNamespace(
            user_agent=agent("Chrome Mobile", "128", "0"),
            os=agent("Android", "14"),
            device=types.SimpleNamespace(family="Pixel 8", brand="Google", model="Pixel 8"),
        )

    if legacy:
        legacy_module = types.ModuleType("ua_parser.user_agent_parser")
        legacy_module.Parse = lambda ua: {
            "user_agent": {"family": "Firefox", "major": "131", "minor": "0", "patch": None},
            "os": {"family": "Windows", "major": "10", "minor": None, "patch": None, "patch_minor": None},
            "device": {"family": "Other", "brand": None, "model": None},
        }
        module.user_agent_parser = legacy_module
        monkeypatch.setitem(sys.modules, "ua_parser.user_agent_parser", legacy_module)
    else:
        module.parse = modern_parse
    monkeypatch.setitem(sys.modules, "ua_parser", module)
    monkeypatch.delenv("TIDESIGHT_PURE_UA", raising=False)
    ua_mod.clear_cache()


def test_pure_env_forces_builtin_even_with_uap(monkeypatch) -> None:
    make_fake_uap(monkeypatch)
    monkeypatch.setenv("TIDESIGHT_PURE_UA", "1")
    ua_mod.clear_cache()
    result = ua_mod.parse(ANDROID_REDUCED)
    check(result, dict(parser="builtin", parser_version=ua_mod.UA_VERSION, device_model=None))
    assert ua_mod.current_parser_version() == ua_mod.UA_VERSION


@pytest.mark.parametrize("value", ["", "0", "false", "off"])
def test_pure_env_falsy_values_do_not_force_builtin(monkeypatch, value: str) -> None:
    make_fake_uap(monkeypatch)
    monkeypatch.setenv("TIDESIGHT_PURE_UA", value)
    assert ua_mod.parse(ANDROID_REDUCED)["parser"] == "uap"


def test_fake_uap_modern_api_is_used_and_refined(monkeypatch) -> None:
    make_fake_uap(monkeypatch)
    result = ua_mod.parse(ANDROID_REDUCED)
    check(result, dict(parser="uap", browser="Chrome", browser_version="128.0", os="Android",
                       os_version=None, device_brand="Google", device_model="Pixel 8", device_type="mobile"))
    assert str(result["parser_version"]).startswith("%d+uap-" % ua_mod.UA_VERSION)
    assert ua_mod.current_parser_version() == result["parser_version"]


def test_fake_uap_legacy_api_is_used(monkeypatch) -> None:
    make_fake_uap(monkeypatch, legacy=True)
    result = ua_mod.parse(CORPUS[2][1])
    check(result, dict(parser="uap", browser="Firefox", browser_version="131.0", os="Windows", os_version="10"))


def test_fake_uap_failure_falls_back_to_builtin(monkeypatch) -> None:
    make_fake_uap(monkeypatch, broken=True)
    result = ua_mod.parse(CHROME_WIN)
    check(result, dict(parser="builtin", parser_version=ua_mod.UA_VERSION, browser="Chrome"))


def test_fake_uap_still_gets_hints_and_bots(monkeypatch) -> None:
    make_fake_uap(monkeypatch)
    headers = {"sec-ch-ua-platform": '"Android"', "sec-ch-ua-platform-version": '"15.0.0"',
               "sec-ch-ua-model": '"Pixel 9"'}
    check(ua_mod.parse(ANDROID_REDUCED, header_hints=headers), dict(os_version="15", device_model="Pixel 9"))
    bot = [c[1] for c in CORPUS if c[0] == "googlebot-smartphone"][0]
    check(ua_mod.parse(bot), dict(is_bot_ua=True, device_type="bot", bot_name="Googlebot"))


REAL_UAP_SAMPLE = [
    "chrome-windows-reduced", "edge-windows", "firefox-windows", "safari-macos", "chrome-macos",
    "safari-iphone", "safari-ipad", "chrome-android-samsung-full", "samsung-internet", "firefox-android",
    "facebook-android", "facebook-ios", "instagram-ios", "telegram-android", "googlebot-smartphone", "curl",
    "playstation-4", "playstation-5", "playstation-vita", "xbox-series-x", "chrome-android-reduced",
    "chrome-android-reduced-tablet", "galaxy-tab", "kindle-fire-silk", "samsung-tizen-tv",
    "windows-phone", "uc-browser-realme", "huawei-browser-harmonyos", "opera-mini-ios", "nokia",
]


@pytest.mark.parametrize("case_id", REAL_UAP_SAMPLE)
def test_real_uap_agrees_on_key_fields(monkeypatch, case_id: str) -> None:
    pytest.importorskip("ua_parser")
    ua = [c[1] for c in CORPUS if c[0] == case_id][0]
    builtin = ua_mod.parse(ua)
    monkeypatch.delenv("TIDESIGHT_PURE_UA", raising=False)
    ua_mod.clear_cache()
    result = ua_mod.parse(ua)
    assert result["parser"] == "uap"
    assert str(result["parser_version"]).startswith("%d+uap-" % ua_mod.UA_VERSION)
    keys = ["os", "device_type", "device_brand", "device_model", "is_bot_ua", "is_webview", "in_app",
            "engine", "browser"]
    for key in keys:
        assert result[key] == builtin[key], (key, result, builtin)
