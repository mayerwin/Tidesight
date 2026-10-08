"""Tests for tidesight.enrich.bots: UA patterns, hosting networks, scoring."""

from __future__ import annotations

import json
import logging
import re

import pytest

from tidesight.enrich import bots

CHROME = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/128.0.0.0 Safari/537.36")
CHROME_ANDROID = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/128.0.0.0 Mobile Safari/537.36")
FIREFOX = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:131.0) Gecko/20100101 Firefox/131.0"
SAFARI_17 = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) "
             "Version/17.5 Safari/605.1.15")
SAFARI_15 = SAFARI_17.replace("Version/17.5", "Version/15.6")
SAFARI_IOS = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
              "Version/17.5 Mobile/15E148 Safari/604.1")
GOOGLEBOT = "Mozilla/5.0 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)"

HUMAN_HEADERS = {
    "accept": "*/*",
    "accept-language": "en-GB,en;q=0.9",
    "accept-encoding": "gzip, deflate, br, zstd",
    "sec-fetch-mode": "no-cors",
    "sec-fetch-site": "cross-site",
    "sec-fetch-dest": "empty",
}
HUMAN_CLIENT = {
    "webdriver": False, "plugins": 5, "languages": 2, "outer_zero": False,
    "automation": [], "headless_ua": False, "chrome_obj": True,
}
COMCAST = 7922


def human(**overrides):
    """classify() arguments of an ordinary visitor, with overrides."""
    args = dict(user_agent=CHROME, headers=dict(HUMAN_HEADERS), client=dict(HUMAN_CLIENT),
                asn=COMCAST, as_org="COMCAST-7922", has_js=True)
    args.update(overrides)
    return args


# Known bot User-Agents (name, kind) --------------------------------------------------------

KNOWN_BOTS = [
    # Search engines.
    (GOOGLEBOT, "Googlebot", "crawler"),
    ("Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/128.0.6613.119 Mobile Safari/537.36 (compatible; Googlebot/2.1; +http://www.google.com/bot.html)",
     "Googlebot", "crawler"),
    ("Googlebot-Image/1.0", "Googlebot-Image", "crawler"),
    ("Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/127.0.6533.119 Mobile Safari/537.36 (compatible; Google-InspectionTool/1.0;)",
     "Google-InspectionTool", "crawler"),
    ("Mozilla/5.0 (X11; Linux x86_64; Storebot-Google/1.0) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/79.0.3945.88 Safari/537.36", "Storebot-Google", "crawler"),
    ("AdsBot-Google (+http://www.google.com/adsbot.html)", "AdsBot-Google", "crawler"),
    ("Mozilla/5.0 (iPhone; CPU iPhone OS 14_7_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
     "Version/14.1.2 Mobile/15E148 Safari/604.1 (compatible; AdsBot-Google-Mobile; "
     "+http://www.google.com/mobile/adsbot.html)", "AdsBot-Google", "crawler"),
    ("Mediapartners-Google", "Mediapartners-Google", "crawler"),
    ("APIs-Google (+https://developers.google.com/webmasters/APIs-Google.html)", "APIs-Google", "crawler"),
    ("GoogleOther", "GoogleOther", "crawler"),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; bingbot/2.0; "
     "+http://www.bing.com/bingbot.htm) Chrome/116.0.1938.76 Safari/537.36", "Bingbot", "crawler"),
    ("msnbot/2.0b (+http://search.msn.com/msnbot.htm)", "msnbot", "crawler"),
    ("Mozilla/5.0 (compatible; Yahoo! Slurp; http://help.yahoo.com/help/us/ysearch/slurp)", "Yahoo Slurp",
     "crawler"),
    ("DuckDuckBot/1.1; (+http://duckduckgo.com/duckduckbot.html)", "DuckDuckBot", "crawler"),
    ("Mozilla/5.0 (compatible; Baiduspider/2.0; +http://www.baidu.com/search/spider.html)", "Baiduspider",
     "crawler"),
    ("Mozilla/5.0 (compatible; YandexBot/3.0; +http://yandex.com/bots)", "YandexBot", "crawler"),
    ("Mozilla/5.0 (compatible; YandexImages/3.0; +http://yandex.com/bots)", "YandexBot", "crawler"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_5) AppleWebKit/605.1.15 (KHTML, like Gecko) "
     "Version/13.1.1 Safari/605.1.15 (Applebot/0.1; +http://www.apple.com/go/applebot)", "Applebot", "crawler"),
    ("Mozilla/5.0 (compatible; SeznamBot/4.0; +https://o-seznam.cz/napoveda/vyhledavani/en/seznambot-crawler/)",
     "SeznamBot", "crawler"),
    ("Sogou web spider/4.0(+http://www.sogou.com/docs/help/webmasters.htm#07)", "Sogou Spider", "crawler"),
    ("Mozilla/5.0 (compatible; Exabot/3.0; +http://www.exabot.com/go/robot)", "Exabot", "crawler"),
    ("Mozilla/5.0 (Linux; Android 7.0;) AppleWebKit/537.36 (KHTML, like Gecko) Mobile Safari/537.36 "
     "(compatible; PetalBot;+https://webmaster.petalsearch.com/site/petalbot)", "PetalBot", "crawler"),
    ("Mozilla/5.0 (compatible; Qwantbot/1.0_4193332; +https://help.qwant.com/bot/)", "Qwantbot", "crawler"),
    ("Mozilla/5.0 (compatible; MojeekBot/0.11; +https://www.mojeek.com/bot.html)", "MojeekBot", "crawler"),
    ("Mozilla/5.0 (compatible; Yeti/1.1; +https://naver.me/spd)", "Naver Yeti", "crawler"),
    ("Mozilla/5.0 (compatible; coccocbot-web/1.0; +http://help.coccoc.com/searchengine)", "Coccocbot",
     "crawler"),
    # SEO tools.
    ("Mozilla/5.0 (compatible; AhrefsBot/7.0; +http://ahrefs.com/robot/)", "AhrefsBot", "crawler"),
    ("Mozilla/5.0 (compatible; SemrushBot/7~bl; +http://www.semrush.com/bot.html)", "SemrushBot", "crawler"),
    ("Mozilla/5.0 (compatible; MJ12bot/v1.4.8; http://mj12bot.com/)", "MJ12bot", "crawler"),
    ("Mozilla/5.0 (compatible; DotBot/1.2; +https://opensiteexplorer.org/dotbot; help@moz.com)", "DotBot",
     "crawler"),
    ("Screaming Frog SEO Spider/20.2", "Screaming Frog SEO Spider", "crawler"),
    ("Mozilla/5.0 (compatible; DataForSeoBot/1.0; +https://dataforseo.com/dataforseo-bot)", "DataForSeoBot",
     "crawler"),
    ("Mozilla/5.0 (compatible; BLEXBot/1.0; +https://help.seranking.com/submit-a-ticket)", "BLEXBot", "crawler"),
    ("serpstatbot/2.1 (advanced backlink tracking bot; https://serpstatbot.com/; abuse@serpstatbot.com)",
     "serpstatbot", "crawler"),
    # AI crawlers and agents.
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; GPTBot/1.1; +https://openai.com/gptbot",
     "GPTBot", "ai"),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ChatGPT-User/1.0; +https://openai.com/bot",
     "ChatGPT-User", "ai"),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; OAI-SearchBot/1.0; "
     "+https://openai.com/searchbot", "OAI-SearchBot", "ai"),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; ClaudeBot/1.0; +claudebot@anthropic.com)",
     "ClaudeBot", "ai"),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Claude-User/1.0; "
     "+Claude-User@anthropic.com)", "Claude-User", "ai"),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Claude-SearchBot/1.0; "
     "+Claude-SearchBot@anthropic.com)", "Claude-SearchBot", "ai"),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; PerplexityBot/1.0; "
     "+https://perplexity.ai/perplexitybot)", "PerplexityBot", "ai"),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Perplexity-User/1.0; "
     "+https://perplexity.ai/perplexity-user)", "Perplexity-User", "ai"),
    ("CCBot/2.0 (https://commoncrawl.org/faq/)", "CCBot", "ai"),
    ("Mozilla/5.0 (Linux; Android 5.0) AppleWebKit/537.36 (KHTML, like Gecko) Mobile Safari/537.36 "
     "(compatible; Bytespider; spider-feedback@bytedance.com)", "Bytespider", "ai"),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; Amazonbot/0.1; "
     "+https://developer.amazon.com/support/amazonbot) Chrome/119.0.6045.214 Safari/537.36", "Amazonbot", "ai"),
    ("meta-externalagent/1.1 (+https://developers.facebook.com/docs/sharing/webmasters/crawler)",
     "meta-externalagent", "ai"),
    ("Mozilla/5.0 (compatible; FacebookBot/1.0; +https://developers.facebook.com/docs/sharing/webmasters/"
     "facebookbot/)", "FacebookBot", "ai"),
    ("Mozilla/5.0 (compatible; Applebot-Extended/0.1; +http://www.apple.com/go/applebot)", "Applebot-Extended",
     "ai"),
    ("Mozilla/5.0 (X11; U; Linux x86_64; en-US) AppleWebKit/533.4 (KHTML, like Gecko) Chrome/5.0.375.99 "
     "Safari/533.4 Diffbot/0.1 (+http://www.diffbot.com)", "Diffbot", "ai"),
    ("Mozilla/5.0 (compatible; YouBot (+http://www.you.com))", "YouBot", "ai"),
    ("Timpibot/0.8 (+http://www.timpi.io)", "Timpibot", "ai"),
    ("Mozilla/5.0 (compatible; ImagesiftBot; +imagesift.com)", "ImagesiftBot", "ai"),
    ("DuckAssistBot/1.2; (+http://duckduckgo.com/duckassistbot.html)", "DuckAssistBot", "ai"),
    ("Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko; compatible; MistralAI-User/1.0; "
     "+https://docs.mistral.ai/robots)", "MistralAI-User", "ai"),
    # Link previews.
    ("facebookexternalhit/1.1 (+http://www.facebook.com/externalhit_uatext.php)", "facebookexternalhit",
     "preview"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_11_1) AppleWebKit/601.2.4 (KHTML, like Gecko) Version/9.0.1 "
     "Safari/601.2.4 facebookexternalhit/1.1 Facebot Twitterbot/1.0", "iMessage", "preview"),
    ("Twitterbot/1.0", "Twitterbot", "preview"),
    ("Slackbot-LinkExpanding 1.0 (+https://api.slack.com/robots)", "Slackbot", "preview"),
    ("Slack-ImgProxy (+https://api.slack.com/robots)", "Slack-ImgProxy", "preview"),
    ("Mozilla/5.0 (compatible; Discordbot/2.0; +https://discordapp.com)", "Discordbot", "preview"),
    ("TelegramBot (like TwitterBot)", "TelegramBot", "preview"),
    ("WhatsApp/2.23.20.0 A", "WhatsApp", "preview"),
    ("LinkedInBot/1.0 (compatible; Mozilla/5.0; Apache-HttpClient +http://www.linkedin.com)", "LinkedInBot",
     "preview"),
    ("Mozilla/5.0 (Windows NT 6.1; WOW64) SkypeUriPreview Preview/0.5", "SkypeUriPreview", "preview"),
    ("Mozilla/5.0 (compatible; Pinterestbot/1.0; +http://www.pinterest.com/bot.html)", "Pinterestbot",
     "preview"),
    ("Mozilla/5.0 (compatible; redditbot/1.0; +http://www.reddit.com/feedback)", "redditbot", "preview"),
    ("Mozilla/5.0 (compatible; Embedly/0.2; +http://support.embed.ly/)", "Embedly", "preview"),
    ("Iframely/1.3.1 (+https://iframely.com/docs/about)", "Iframely", "preview"),
    ("Mozilla/5.0 (compatible; vkShare; +http://vk.com/dev/Share)", "vkShare", "preview"),
    ("http.rb/5.1.1 (Mastodon/4.2.10; +https://mastodon.social/) Bot", "Mastodon", "preview"),
    ("Mozilla/5.0 (compatible; Bluesky Cardyb/1.1; +mailto:support@bsky.app)", "Bluesky", "preview"),
    ("Snap URL Preview Service; bot; snapchat; https://developers.snap.com/robots", "Snapchat Preview",
     "preview"),
    ("Mozilla/5.0 (Windows NT 6.1; WOW64) AppleWebKit/534+ (KHTML, like Gecko) BingPreview/1.0b",
     "BingPreview", "preview"),
    # Monitors.
    ("Mozilla/5.0+(compatible; UptimeRobot/2.0; http://www.uptimerobot.com/)", "UptimeRobot", "monitor"),
    ("Pingdom.com_bot_version_1.4_(http://www.pingdom.com/)", "Pingdom", "monitor"),
    (CHROME + " StatusCake", "StatusCake", "monitor"),
    ("Site24x7", "Site24x7", "monitor"),
    ("Better Uptime Bot Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/95.0.4638.69 Safari/537.36", "Better Stack", "monitor"),
    ("Uptime-Kuma/1.23.13", "Uptime Kuma", "monitor"),
    ("Datadog/Synthetics", "Datadog Synthetics", "monitor"),
    ("NewRelicPinger/1.0 (269833)", "New Relic Synthetics", "monitor"),
    ("Checkly/1.0 (https://www.checklyhq.com)", "Checkly", "monitor"),
    ("Mozilla/5.0 (compatible; HetrixTools Uptime Monitoring Bot. https://hetrix.tools/)", "HetrixTools",
     "monitor"),
    ("FreshpingBot/1.0 (+https://freshping.io/)", "Freshping", "monitor"),
    ("Zabbix", "Zabbix", "monitor"),
    ("check_http/v2.3.3 (nagios-plugins 2.3.3)", "Nagios", "monitor"),
    ("kube-probe/1.29", "kube-probe", "monitor"),
    ("ELB-HealthChecker/2.0", "ELB Health Checker", "monitor"),
    # Tools and libraries.
    ("curl/8.4.0", "curl", "tool"),
    ("Wget/1.21.4", "Wget", "tool"),
    ("python-requests/2.32.3", "python-requests", "tool"),
    ("Python-urllib/3.11", "python-urllib", "tool"),
    ("Python/3.12 aiohttp/3.9.5", "aiohttp", "tool"),
    ("python-httpx/0.27.0", "httpx", "tool"),
    ("Go-http-client/2.0", "Go-http-client", "tool"),
    ("Java/17.0.10", "Java", "tool"),
    ("Apache-HttpClient/4.5.14 (Java/17.0.10)", "Apache-HttpClient", "tool"),
    ("okhttp/4.12.0", "okhttp", "tool"),
    ("axios/1.7.2", "axios", "tool"),
    ("node-fetch/1.0 (+https://github.com/bitinn/node-fetch)", "node-fetch", "tool"),
    ("undici", "undici", "tool"),
    ("node", "Node.js fetch", "tool"),
    ("PostmanRuntime/7.39.0", "Postman", "tool"),
    ("insomnia/2023.5.8", "Insomnia", "tool"),
    ("HTTPie/3.2.2", "HTTPie", "tool"),
    ("libwww-perl/6.72", "libwww-perl", "tool"),
    ("Scrapy/2.11.2 (+https://scrapy.org)", "Scrapy", "tool"),
    ("GuzzleHttp/7", "Guzzle", "tool"),
    ("Ruby", "Ruby", "tool"),
    ("PHP/8.2", "PHP", "tool"),
    ("WordPress/6.6.1; https://example.com", "WordPress", "tool"),
    ("Mozilla/5.0 (Linux; Android 11; moto g power (2022)) AppleWebKit/537.36 (KHTML, like Gecko) "
     "Chrome/114.0.0.0 Mobile Safari/537.36 Chrome-Lighthouse", "Lighthouse", "tool"),
    (CHROME + " GTmetrix", "GTmetrix", "tool"),
    (CHROME + " PTST/240611.150223", "WebPageTest", "tool"),
    ("masscan/1.3 (https://github.com/robertdavidgraham/masscan)", "masscan", "tool"),
    ("Mozilla/5.0 (compatible; CensysInspect/1.1; +https://about.censys.io/)", "CensysInspect", "tool"),
    ("Mozilla/5.0 (compatible; Nmap Scripting Engine; https://nmap.org/book/nse.html)", "Nmap", "tool"),
    ("Mozilla/5.0", "Bare Mozilla", "tool"),
    # Headless browsers and automation.
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) HeadlessChrome/120.0.6099.109 "
     "Safari/537.36", "HeadlessChrome", "headless"),
    ("Mozilla/5.0 (Unknown; Linux x86_64) AppleWebKit/538.1 (KHTML, like Gecko) PhantomJS/2.1.1 Safari/538.1",
     "PhantomJS", "headless"),
    ("Mozilla/5.0 (linux) AppleWebKit/537.36 (KHTML, like Gecko) jsdom/24.1.0", "jsdom", "headless"),
    ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Cypress/13.13.0 "
     "Chrome/118.0.5993.159 Electron/27.3.10 Safari/537.36", "Cypress", "automation"),
]


@pytest.mark.parametrize("ua,name,kind", KNOWN_BOTS, ids=["%s-%d" % (k[1], i) for i, k in enumerate(KNOWN_BOTS)])
def test_match_ua_known_bots(ua: str, name: str, kind: str) -> None:
    assert bots.match_ua(ua) == (name, kind)


@pytest.mark.parametrize("ua,name,kind", KNOWN_BOTS, ids=["%s-%d" % (k[1], i) for i, k in enumerate(KNOWN_BOTS)])
def test_classify_known_bots(ua: str, name: str, kind: str) -> None:
    result = bots.classify(ua, headers=dict(HUMAN_HEADERS), asn=COMCAST, has_js=True)
    assert result["is_bot"] is True
    assert (result["bot_name"], result["bot_kind"]) == (name, kind)
    assert result["score"] == 1.0
    assert "ua:%s" % name in result["reasons"]


def test_every_kind_is_covered() -> None:
    covered = {kind for _, _, kind in KNOWN_BOTS}
    assert covered >= {"crawler", "ai", "preview", "monitor", "tool", "headless", "automation"}
    assert len(KNOWN_BOTS) >= 100


# Real browsers and apps must not match -----------------------------------------------------

NOT_BOTS = [
    CHROME, CHROME_ANDROID, FIREFOX, SAFARI_17, SAFARI_IOS,
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 "
    "Safari/537.36 Edg/128.0.2739.67",
    "Mozilla/5.0 (compatible; MSIE 10.0; Windows NT 6.2; Trident/6.0)",
    "Mozilla/5.0 (Windows NT 6.1; WOW64; Trident/7.0; rv:11.0) like Gecko",
    "Opera/9.80 (Windows NT 6.1; WOW64) Presto/2.12.388 Version/12.18",
    "Mozilla/5.0 (Linux; Android 14; SAMSUNG SM-S928B) AppleWebKit/537.36 (KHTML, like Gecko) "
    "SamsungBrowser/26.0 Chrome/122.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 10; CUBOT X30) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.6099.230 Mobile Safari/537.36",
    "Mozilla/5.0 (Linux; Android 11; CUBOT_NOTE_20) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.6099.230 Mobile Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Mobile/15E148 Instagram 341.0.3.20.98 (iPhone15,3; iOS 17_5; en_US; en; scale=3.00; 1290x2796; 621640227)",
    "Mozilla/5.0 (Linux; Android 14; SM-S918B Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Version/4.0 Chrome/126.0.6478.122 Mobile Safari/537.36 [FB_IAB/FB4A;FBAV/471.0.0.35.80;]",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Mobile/15E148 [LinkedInApp]/9.29.5463",
    "Mozilla/5.0 (Linux; Android 14; SM-G991B Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Version/4.0 Chrome/126.0.6478.134 Mobile Safari/537.36 [Pinterest/Android]",
    "Mozilla/5.0 (Linux; Android 14; SM-S911B Build/UP1A.231005.007; wv) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Version/4.0 Chrome/127.0.6533.103 Mobile Safari/537.36 Telegram-Android/11.0.0 (Samsung SM-S911B; "
    "Android 14; SDK 34; HIGH)",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Mobile/15E148 Snapchat/13.2.0.36 (like Safari/8618.2.12.10.4, panda)",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Mobile/15E148 Twitter for iPhone/10.4",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Slack/4.40.120 "
    "Chrome/128.0.6613.36 Electron/32.0.1 Safari/537.36 Sonic Slack_SSB/4.40.120",
    "Mozilla/5.0 (X11; GNU/Linux) AppleWebKit/537.36 (KHTML, like Gecko) Chromium/79.0.3945.130 "
    "Chrome/79.0.3945.130 Safari/537.36 Tesla/2024.20.9-ea2c8f8c4b5f",
    "Mozilla/5.0 (X11; Linux x86_64; Quest 3) AppleWebKit/537.36 (KHTML, like Gecko) OculusBrowser/35.0.0.0.32 "
    "SamsungBrowser/4.0 Chrome/126.0.6478.122 VR Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; Xbox; Xbox Series X) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/48.0.2564.82 Safari/537.36 Edge/20.02",
    "Mozilla/5.0 (PlayStation; PlayStation 5/2.26) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/13.0 "
    "Safari/605.1.15",
    "Roku/DVP-12.5 (12.5.0.4174-56)",
    "HbbTV/1.5.1 (+DRM;Panasonic;SmartTV2019;3.022;4401-0003 0008-0000;SmartTV2019;)",
    "Dalvik/2.1.0 (Linux; U; Android 14; SM-S918B Build/UP1A.231005.007)",
    "Mozilla/5.0 (Windows NT 5.1; rv:11.0) Gecko Firefox/11.0 (via ggpht.com GoogleImageProxy)",
]


@pytest.mark.parametrize("ua", NOT_BOTS)
def test_real_browsers_are_not_bots(ua: str) -> None:
    assert bots.match_ua(ua) is None
    result = bots.classify(ua, headers=dict(HUMAN_HEADERS), asn=COMCAST, has_js=True)
    assert result["is_bot"] is False and result["bot_kind"] is None


# Generic fallbacks ---------------------------------------------------------------------------------


@pytest.mark.parametrize("ua,expected", [
    ("Mozilla/5.0 (compatible; ExampleBot/1.0)", ("ExampleBot", "crawler")),
    ("SuperCrawler 2.0", ("SuperCrawler", "crawler")),
    ("acme-spider/3.1", ("acme-spider", "crawler")),
    ("my-site-monitor/1.0", ("my-site-monitor", "monitor")),
    ("LinkChecker/10.2", ("LinkChecker", "tool")),
    ("Mozilla/5.0 (compatible; Zorg/1.0; +https://zorg.example/bot)", ("Zorg", "crawler")),
    ("Zorg/1.0 (+https://zorg.example/about)", ("Zorg", "crawler")),
    ("1234567", ("Invalid User-Agent", "tool")),
    ("-", ("Invalid User-Agent", "tool")),
])
def test_generic_fallbacks(ua: str, expected) -> None:
    assert bots.match_ua(ua) == expected
    reasons = bots.classify(ua)["reasons"]
    assert reasons == ["ua_generic:%s" % expected[0]] or reasons == ["ua:%s" % expected[0]]


@pytest.mark.parametrize("value", [None, "", "   ", 42, ["curl/8"]])
def test_match_ua_ignores_missing_values(value) -> None:
    assert bots.match_ua(value) is None


def test_match_ua_accepts_bytes() -> None:
    assert bots.match_ua(b"curl/8.0.1") == ("curl", "tool")


# Hosting networks -----------------------------------------------------------------------------------


@pytest.mark.parametrize("asn,org", [
    (16509, None), ("AS16509", None), ("14061", None), (14061, "DIGITALOCEAN-ASN"), (396982, None),
    (13335, "CLOUDFLARENET"), (24940, "Hetzner Online GmbH"), (None, "DigitalOcean, LLC"),
    (None, "Hetzner Online GmbH"), (None, "OVH SAS"), (None, "Some Random Hosting Ltd"),
    (None, "M247 Europe SRL"), (None, "Datacamp Limited"), (None, "Private VPN Services Inc"),
    (None, "Acme Cloud Services"), (None, "AMAZON-02"), (99999999, "Example Data Center GmbH"),
])
def test_is_hosting_true(asn, org) -> None:
    assert bots.is_hosting(asn, org) is True


@pytest.mark.parametrize("asn,org", [
    (COMCAST, "COMCAST-7922"), (3320, "Deutsche Telekom AG"), (3215, "Orange S.A."),
    (None, "Telefonica Colombia"), (None, "Colombia Movil"), (None, "1&1 Versatel Deutschland GmbH"),
    (None, "Leibniz-Rechenzentrum"), (35540, "OVH SAS"), (16591, "GOOGLE-FIBER"),
    (None, None), (None, ""), (True, None), ("garbage", 5), (-1, None),
])
def test_is_hosting_false(asn, org) -> None:
    assert bots.is_hosting(asn, org) is False


def test_hosting_list_is_substantial() -> None:
    assert len(bots.HOSTING_ASNS) >= 100
    assert all(isinstance(asn, int) and asn > 0 for asn in bots.HOSTING_ASNS)


# External crawler list ---------------------------------------------------------------------------------


@pytest.fixture
def crawler_list(tmp_path):
    entries = [
        {"pattern": "Googlebot\\/", "url": "http://www.google.com/bot.html", "instances": ["Googlebot/2.1"]},
        {"pattern": "[wW]get", "instances": ["Wget/1.21"]},
        {"pattern": "bad[regex"},
        "SomeBot",
        {"pattern": ""},
        {"name": "no pattern"},
        {"pattern": "Zorglub-Fetch", "name": "Zorglub"},
    ]
    path = tmp_path / "crawler-user-agents.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return str(path)


def test_load_crawler_list(crawler_list, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="tidesight.enrich.bots"):
        patterns = bots.load_crawler_list(crawler_list)
    assert [name for _, name in patterns] == ["Googlebot", "wget", "SomeBot", "Zorglub"]
    assert all(isinstance(regex, re.Pattern) for regex, _ in patterns)
    assert any("bad[regex" in record.getMessage() for record in caplog.records)


def test_crawler_list_patterns_are_case_sensitive(crawler_list) -> None:
    patterns = dict((name, regex) for regex, name in bots.load_crawler_list(crawler_list))
    assert patterns["wget"].search("Wget/1.21") and patterns["wget"].search("wget/1.21")
    assert not patterns["Zorglub"].search("zorglub-fetch")


@pytest.mark.parametrize("content", ["{not json", '{"pattern": "x"}', "42", ""])
def test_load_crawler_list_invalid_files(tmp_path, caplog, content: str) -> None:
    path = tmp_path / "list.json"
    path.write_text(content, encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="tidesight.enrich.bots"):
        assert bots.load_crawler_list(str(path)) == []
    assert caplog.records


def test_load_crawler_list_missing_file(tmp_path, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger="tidesight.enrich.bots"):
        assert bots.load_crawler_list(str(tmp_path / "missing.json")) == []
    assert caplog.records


def test_classify_uses_extra_patterns(crawler_list) -> None:
    extra = bots.load_crawler_list(crawler_list)
    ua = "Zorglub-Fetch/2.0 (example)"
    assert bots.classify(ua)["is_bot"] is False
    result = bots.classify(ua, extra_patterns=extra)
    assert (result["is_bot"], result["bot_kind"], result["bot_name"]) == (True, "crawler", "Zorglub")
    assert result["reasons"] == ["ua_list:Zorglub"]


def test_builtin_patterns_win_over_extra_list(crawler_list) -> None:
    result = bots.classify(GOOGLEBOT, extra_patterns=bots.load_crawler_list(crawler_list))
    assert result["reasons"][0] == "ua:Googlebot"


def test_malformed_extra_patterns_are_ignored() -> None:
    result = bots.classify(CHROME, headers=dict(HUMAN_HEADERS),
                           extra_patterns=[("not a pattern", "x"), None, (re.compile("Nope"),)])
    assert result["is_bot"] is False


# classify(): kinds and scoring ---------------------------------------------------------------------


def test_human_baseline() -> None:
    result = bots.classify(**human())
    assert result == {"is_bot": False, "bot_kind": None, "bot_name": None, "score": 0.0, "reasons": [],
                      "is_hosting": False}


@pytest.mark.parametrize("ua", [None, "", "   ", 12345, b""])
def test_empty_ua_is_a_tool(ua) -> None:
    result = bots.classify(**human(user_agent=ua))
    assert (result["is_bot"], result["bot_kind"], result["score"]) == (True, "tool", 1.0)
    assert result["reasons"][0] == "empty_ua"


def test_webdriver_is_automation() -> None:
    result = bots.classify(**human(client=dict(HUMAN_CLIENT, webdriver=True)))
    assert (result["is_bot"], result["bot_kind"], result["score"]) == (True, "automation", 0.95)
    assert result["reasons"] == ["webdriver"]


@pytest.mark.parametrize("found,name", [
    (["__playwright__binding__", "__pwInitScripts"], "Playwright"),
    (["$cdc_asdjflasutopfhvcZLmcfl_"], "ChromeDriver"),
    (["Cypress"], "Cypress"),
    (["callPhantom", "_phantom"], "PhantomJS"),
    (["__nightmare"], "Nightmare"),
    (["domAutomation"], "Chrome automation"),
    (["__selenium_unwrapped"], "Selenium"),
    (["__webdriver_evaluate"], "Selenium"),
    (["webdriver_script_fn"], "Selenium"),
    (["someNewFrameworkGlobal"], None),
])
def test_automation_globals(found, name) -> None:
    result = bots.classify(**human(client=dict(HUMAN_CLIENT, automation=found)))
    assert (result["is_bot"], result["bot_kind"], result["bot_name"]) == (True, "automation", name)
    assert result["reasons"] == ["automation:%s" % ",".join(found)]


def test_headless_hint_from_tracker() -> None:
    result = bots.classify(**human(client=dict(HUMAN_CLIENT, headless_ua=True)))
    assert (result["is_bot"], result["bot_kind"], result["bot_name"]) == (True, "headless", "HeadlessChrome")


def test_datacenter_without_js() -> None:
    result = bots.classify(**human(asn=16509, as_org="AMAZON-02", has_js=False))
    assert (result["is_bot"], result["bot_kind"], result["score"]) == (True, "datacenter", 0.8)
    assert result["is_hosting"] is True
    assert result["reasons"] == ["hosting_asn:16509", "no_js"]


@pytest.mark.parametrize("has_js", [True, None])
def test_hosting_with_js_is_only_suspicious(has_js) -> None:
    result = bots.classify(**human(asn=16509, as_org="AMAZON-02", has_js=has_js))
    assert (result["is_bot"], result["bot_kind"], result["score"]) == (False, "suspicious", 0.5)
    assert result["is_hosting"] is True
    assert result["reasons"] == ["hosting_asn:16509"]


def test_hosting_detected_from_org_name() -> None:
    result = bots.classify(**human(asn=None, as_org="Hetzner Online GmbH", has_js=False))
    assert result["bot_kind"] == "datacenter"
    assert result["reasons"] == ["hosting_org", "no_js"]


def test_no_js_from_residential_network_is_human() -> None:
    result = bots.classify(**human(has_js=False))
    assert result["is_bot"] is False and result["score"] == 0.0


def test_missing_accept_language_raises_score() -> None:
    headers = {k: v for k, v in HUMAN_HEADERS.items() if k != "accept-language"}
    result = bots.classify(**human(headers=headers))
    assert (result["is_bot"], result["score"], result["reasons"]) == (False, 0.3, ["no_accept_language"])


def test_missing_sec_fetch_on_modern_browser() -> None:
    headers = {k: v for k, v in HUMAN_HEADERS.items() if not k.startswith("sec-fetch")}
    result = bots.classify(**human(headers=headers))
    assert (result["score"], result["reasons"]) == (0.2, ["no_sec_fetch"])


@pytest.mark.parametrize("ua,expected", [(SAFARI_17, True), (SAFARI_15, False), (SAFARI_IOS, False),
                                         (FIREFOX, True), (CHROME_ANDROID, True)])
def test_sec_fetch_expectation_per_browser(ua: str, expected: bool) -> None:
    headers = {"accept-language": "fr"}
    result = bots.classify(ua, headers=headers)
    assert ("no_sec_fetch" in result["reasons"]) is expected


def test_empty_headers_are_suspicious_but_not_a_bot() -> None:
    result = bots.classify(CHROME, headers={})
    assert (result["is_bot"], result["bot_kind"], result["score"]) == (False, "suspicious", 0.5)
    assert result["reasons"] == ["no_accept_language", "no_sec_fetch"]


def test_unknown_headers_skip_header_checks() -> None:
    assert bots.classify(CHROME, headers=None)["score"] == 0.0


def test_hosting_plus_missing_language_crosses_threshold() -> None:
    headers = {k: v for k, v in HUMAN_HEADERS.items() if k != "accept-language"}
    result = bots.classify(**human(headers=headers, asn=14061, as_org="DIGITALOCEAN-ASN"))
    assert (result["is_bot"], result["bot_kind"], result["score"]) == (True, "suspicious", 0.8)


def test_headless_fingerprint_signals_add_up() -> None:
    client = dict(HUMAN_CLIENT, chrome_obj=False, outer_zero=True, languages=0)
    result = bots.classify(**human(client=client))
    assert (result["is_bot"], result["bot_kind"], result["score"]) == (True, "suspicious", 0.8)
    assert set(result["reasons"]) == {"no_window_chrome", "outer_zero", "no_languages"}


def test_chrome_without_window_chrome() -> None:
    result = bots.classify(**human(client=dict(HUMAN_CLIENT, chrome_obj=False)))
    assert (result["is_bot"], result["score"], result["reasons"]) == (False, 0.3, ["no_window_chrome"])


def test_no_plugins_on_desktop_chrome_is_a_slight_signal() -> None:
    result = bots.classify(**human(client=dict(HUMAN_CLIENT, plugins=0)))
    assert (result["score"], result["reasons"]) == (0.1, ["no_plugins"])


@pytest.mark.parametrize("ua", [CHROME_ANDROID, FIREFOX, SAFARI_17])
def test_chrome_checks_only_apply_to_desktop_chromium(ua: str) -> None:
    client = dict(HUMAN_CLIENT, chrome_obj=False, plugins=0)
    result = bots.classify(**human(user_agent=ua, client=client))
    assert result["score"] == 0.0


def test_unverified_googlebot_is_still_a_crawler() -> None:
    result = bots.classify(GOOGLEBOT, asn=16509, as_org="AMAZON-02", has_js=False)
    assert (result["is_bot"], result["bot_kind"], result["bot_name"]) == (True, "crawler", "Googlebot")
    assert result["reasons"] == ["ua:Googlebot", "unverified_asn:Googlebot", "hosting_asn:16509"]


def test_verified_googlebot() -> None:
    result = bots.classify(GOOGLEBOT, asn=15169, as_org="GOOGLE", has_js=False)
    assert result["reasons"] == ["ua:Googlebot", "hosting_asn:15169"]
    assert result["is_hosting"] is True and result["bot_kind"] == "crawler"


def test_mail_image_proxy_is_not_a_datacenter_bot() -> None:
    ua = "Mozilla/5.0 (Windows NT 5.1; rv:11.0) Gecko Firefox/11.0 (via ggpht.com GoogleImageProxy)"
    result = bots.classify(ua, asn=15169, as_org="GOOGLE", has_js=False)
    assert (result["is_bot"], result["bot_kind"]) == (False, None)
    assert result["reasons"] == ["hosting_asn:15169", "mail_proxy"]


def test_from_header_marks_a_crawler() -> None:
    headers = dict(HUMAN_HEADERS, **{"from": "googlebot(at)googlebot.com"})
    result = bots.classify(**human(headers=headers))
    assert (result["is_bot"], result["bot_kind"], result["score"]) == (True, "crawler", 0.8)
    assert result["reasons"] == ["from_header"]


@pytest.mark.parametrize("value,kind,name", [
    ('"https://chatgpt.com"', "ai", "chatgpt.com"),
    ('sig1="https://chatgpt.com"', "ai", "chatgpt.com"),
    ('"https://agent.example.net"', "automation", "agent.example.net"),
])
def test_signature_agent_header(value: str, kind: str, name: str) -> None:
    headers = dict(HUMAN_HEADERS, **{"signature-agent": value})
    result = bots.classify(**human(headers=headers))
    assert (result["is_bot"], result["bot_kind"], result["bot_name"]) == (True, kind, name)
    assert result["reasons"] == ["signature_agent:%s" % name]


def test_header_names_and_list_values_are_normalised() -> None:
    headers = {"Accept-Language": ["en", "fr"], "Sec-Fetch-Mode": "cors"}
    assert bots.classify(CHROME, headers=headers)["score"] == 0.0


def test_strongest_signal_sets_the_kind() -> None:
    client = dict(HUMAN_CLIENT, webdriver=True)
    result = bots.classify(**human(user_agent=GOOGLEBOT, client=client))
    assert (result["bot_kind"], result["bot_name"]) == ("crawler", "Googlebot")
    # A Googlebot UA from a residential network is noted as unverified.
    assert result["reasons"] == ["ua:Googlebot", "unverified_asn:Googlebot", "webdriver"]


@pytest.mark.parametrize("kwargs", [
    dict(user_agent=12345, headers="nope", client=["x"], asn="garbage", as_org=5, has_js="yes"),
    dict(user_agent=CHROME, headers={None: "x", "accept-language": None}, client={"plugins": "many",
         "languages": True, "automation": "not-a-list", "webdriver": "true"}),
    dict(user_agent="\x00\xff" * 50, client={"plugins": -3, "languages": 2.5}),
    dict(user_agent="A" * 50000),
])
def test_garbage_inputs_never_raise(kwargs) -> None:
    result = bots.classify(**kwargs)
    assert set(result) == {"is_bot", "bot_kind", "bot_name", "score", "reasons", "is_hosting"}
    assert 0.0 <= result["score"] <= 1.0


def test_scores_are_bounded_and_consistent() -> None:
    cases = [bots.classify(ua, headers={}, client={"chrome_obj": False, "outer_zero": True, "languages": 0,
                                                     "plugins": 0, "webdriver": True}, asn=16509,
                           has_js=False) for ua, _, _ in KNOWN_BOTS] + [bots.classify(ua) for ua in NOT_BOTS]
    for result in cases:
        assert 0.0 <= result["score"] <= 1.0
        assert result["is_bot"] is (result["score"] >= bots.BOT_THRESHOLD)
        assert result["bot_kind"] is None or result["bot_kind"] in bots.KINDS


def test_classify_is_deterministic() -> None:
    args = human(asn=16509, headers={}, client=dict(HUMAN_CLIENT, chrome_obj=False))
    assert bots.classify(**args) == bots.classify(**args)


def test_version_constant() -> None:
    assert isinstance(bots.BOTS_VERSION, int) and bots.BOTS_VERSION >= 1


# Literal gate (performance shortcut) must never change results -------------------------------------


def brute_force_specific(ua: str):
    for regex, name, kind in bots._SPECIFIC:
        match = regex.search(ua)
        if match:
            return (name or match.group(0).strip(), kind)
    return None


GATE_SAMPLES = [ua for ua, _, _ in KNOWN_BOTS] + NOT_BOTS + [
    "Mozilla/5.0 (compatible; YandexMetrika/2.0)", "Feedbin feed-id:1 - 2 subscribers",
    "Misskey/2024.5.0 (https://misskey.example)", "Agent Java/21", "Yahoo!Slurp", "BetterUptime",
    "Datadog Synthetics", "Sogou inst spider/4.0", "ChatGPT Agent", "Discourse Forum Onebox v3",
]


@pytest.mark.parametrize("ua", GATE_SAMPLES)
def test_gate_agrees_with_brute_force(ua: str) -> None:
    assert bots._match_specific(ua) == brute_force_specific(ua)


@pytest.mark.parametrize("source,expected", [
    ("Googlebot-Image", ["Googlebot-Image"]),
    (r"Yahoo! ?Slurp|\bSlurp\b", ["Yahoo", "Slurp"]),
    (r"Datadog ?/? ?Synthetics", ["Synthetics"]),
    (r"Sogou.{0,20}spider", ["spider"]),
    (r"\bk6/\d", ["k6"]),
    (r"ab{0,3}", None),
    (r"Bots?", ["Bot"]),
    (r"(?:^|\s)Java/\d", None),
    (r"ChatGPT[ -]Agent", None),
    (r"x|Googlebot", None),
])
def test_required_literals(source: str, expected) -> None:
    assert bots._required_literals(source) == expected


def test_crawler_list_gate_agrees_with_plain_list(crawler_list) -> None:
    gated = bots.load_crawler_list(crawler_list)
    assert isinstance(gated, bots.CrawlerList) and isinstance(gated, list)
    plain = list(gated)
    for ua in ["Zorglub-Fetch/2.0", "Wget/1.21", "wget", "Googlebot/2.1", "SomeBot", CHROME, ""]:
        assert bots._match_extra(ua, gated) == bots._match_extra(ua, plain)
