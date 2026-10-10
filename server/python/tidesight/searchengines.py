"""Search engines: what people searched for on Google and Bing before they came (docs/search-engines.md).

Google Search Console and Bing Webmaster Tools are the only places that know the queries behind organic visits.
Their accounts are connected once, in the dashboard (data_dir/search-engines.json: a Google service account's key
file, a Bing API key; this account only, never shown again), and each property names its Search Console property
(`search_console`) and its Bing site (`bing_site`). `jobs.py cron` imports their figures into search.db, a database
of its own: they are raw data from elsewhere, which derived.db (rebuilt at will) must not hold.

  - Google: one row per day, query and page (clicks, impressions, average position), and per day, country and
    device for the totals (they include the queries Google keeps to itself). 16 months of history on the first
    import, newest days first; days stay "fresh" for GOOGLE_FINAL_AFTER days and are fetched again meanwhile.
  - Bing: its query and page statistics (weekly buckets) and its daily totals, refreshed twice a day.

Every call has a timeout; failures are recorded per property and engine, and retried later (cron never fails for
them). Standard library only: Google's token is a JSON Web Token signed with the key (rs256.py).
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import sqlite3
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote, urlencode, urlsplit

from . import __version__, properties, rs256
from .config import Config
from .enrich.timeutil import local_day
from .storage import connect, migrate

log = logging.getLogger("tidesight.search")

FILE = "search-engines.json"
DB = "search.db"
ENGINES = ("google", "bing")
LABELS = {"google": "Google", "bing": "Bing"}
SETTINGS = {"google": "search_console", "bing": "bing_site"}  # the property setting that names each engine's site
SOURCES = {"google": "google", "bing": "bing"}  # the session source of their organic visits (enrich/sources.py)

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_API = "https://www.googleapis.com/webmasters/v3/"
GOOGLE_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"
BING_API = "https://ssl.bing.com/webmaster/api.svc/json/"
PACIFIC = "America/Los_Angeles"  # Search Console's days
GOOGLE_HISTORY_DAYS = 486        # Search Console keeps 16 months
GOOGLE_FINAL_AFTER = 4           # days after which Google's figures for a day no longer change
GOOGLE_REFRESH_MS = 6 * 3600 * 1000
BING_REFRESH_MS = 12 * 3600 * 1000
ROW_LIMIT = 25000                # Search Console's largest page of rows
MAX_ROWS_PER_DAY = 100000        # per site and day (Search Console gives at most 50,000 a day anyway)
MAX_RESPONSE = 64 * 1024 * 1024
TIMEOUT = 60.0
RETRY_SOON_MS = 10 * 60000       # after a network, rate or server error
RETRY_LATER_MS = 60 * 60000      # after a refusal (a key, a permission): or as soon as the settings change
MAX_TEXT = 2000

SEARCH_MIGRATIONS: List[Tuple[int, List[str]]] = [
    (1, [
        # One row per day and query and page (Bing: per query, or per page, the other one '').
        """CREATE TABLE IF NOT EXISTS search_rows (
            site_id     TEXT NOT NULL,
            engine      TEXT NOT NULL,
            day         TEXT NOT NULL,
            query       TEXT NOT NULL,
            page        TEXT NOT NULL,
            clicks      INTEGER NOT NULL,
            impressions INTEGER NOT NULL,
            position    REAL
        )""",
        "CREATE INDEX IF NOT EXISTS search_rows_site_day ON search_rows(site_id, engine, day)",
        # Totals per day (Google: per country and device too), hidden queries included.
        """CREATE TABLE IF NOT EXISTS search_totals (
            site_id     TEXT NOT NULL,
            engine      TEXT NOT NULL,
            day         TEXT NOT NULL,
            country     TEXT NOT NULL,
            device      TEXT NOT NULL,
            clicks      INTEGER NOT NULL,
            impressions INTEGER NOT NULL,
            position    REAL
        )""",
        "CREATE INDEX IF NOT EXISTS search_totals_site_day ON search_totals(site_id, engine, day)",
        # Days imported (Google), and whether their figures are final.
        """CREATE TABLE IF NOT EXISTS search_days (
            site_id    TEXT NOT NULL,
            engine     TEXT NOT NULL,
            day        TEXT NOT NULL,
            fetched_at INTEGER NOT NULL,
            final      INTEGER NOT NULL,
            rows       INTEGER NOT NULL,
            PRIMARY KEY (site_id, engine, day)
        ) WITHOUT ROWID""",
        # The import of each property and engine: its target (the engine's site), the last run, the last error.
        """CREATE TABLE IF NOT EXISTS search_state (
            site_id  TEXT NOT NULL,
            engine   TEXT NOT NULL,
            target   TEXT NOT NULL,
            last_run INTEGER,
            last_ok  INTEGER,
            error    TEXT,
            error_at INTEGER,
            retry_at INTEGER,
            PRIMARY KEY (site_id, engine)
        ) WITHOUT ROWID""",
    ]),
]

# ISO 3166 alpha-3 (Search Console's country codes, lower case) to alpha-2 (Tidesight's).
_ALPHA3 = dict((p[:3], p[3:]) for p in (
    "abwAW afgAF agoAO aiaAI alaAX albAL andAD areAE argAR armAM asmAS ataAQ atfTF atgAG ausAU autAT azeAZ bdiBI "
    "belBE benBJ besBQ bfaBF bgdBD bgrBG bhrBH bhsBS bihBA blmBL blrBY blzBZ bmuBM bolBO braBR brbBB brnBN btnBT "
    "bvtBV bwaBW cafCF canCA cckCC cheCH chlCL chnCN civCI cmrCM codCD cogCG cokCK colCO comKM cpvCV criCR cubCU "
    "cuwCW cxrCX cymKY cypCY czeCZ deuDE djiDJ dmaDM dnkDK domDO dzaDZ ecuEC egyEG eriER eshEH espES estEE ethET "
    "finFI fjiFJ flkFK fraFR froFO fsmFM gabGA gbrGB geoGE ggyGG ghaGH gibGI ginGN glpGP gmbGM gnbGW gnqGQ grcGR "
    "grdGD grlGL gtmGT gufGF gumGU guyGY hkgHK hmdHM hndHN hrvHR htiHT hunHU idnID imnIM indIN iotIO irlIE irnIR "
    "irqIQ islIS isrIL itaIT jamJM jeyJE jorJO jpnJP kazKZ kenKE kgzKG khmKH kirKI knaKN korKR kwtKW laoLA lbnLB "
    "lbrLR lbyLY lcaLC lieLI lkaLK lsoLS ltuLT luxLU lvaLV macMO mafMF marMA mcoMC mdaMD mdgMG mdvMV mexMX mhlMH "
    "mkdMK mliML mltMT mmrMM mneME mngMN mnpMP mozMZ mrtMR msrMS mtqMQ musMU mwiMW mysMY mytYT namNA nclNC nerNE "
    "nfkNF ngaNG nicNI niuNU nldNL norNO nplNP nruNR nzlNZ omnOM pakPK panPA pcnPN perPE phlPH plwPW pngPG polPL "
    "priPR prkKP prtPT pryPY psePS pyfPF qatQA reuRE rouRO rusRU rwaRW sauSA sdnSD senSN sgpSG sgsGS shnSH sjmSJ "
    "slbSB sleSL slvSV smrSM somSO spmPM srbRS ssdSS stpST surSR svkSK svnSI sweSE swzSZ sxmSX sycSC syrSY tcaTC "
    "tcdTD tgoTG thaTH tjkTJ tklTK tkmTM tlsTL tonTO ttoTT tunTN turTR tuvTV twnTW tzaTZ ugaUG ukrUA umiUM uryUY "
    "usaUS uzbUZ vatVA vctVC venVE vgbVG virVI vnmVN vutVU wlfWF wsmWS xkxXK yemYE zafZA zmbZM zweZW").split())
_GOOGLE_SITE_RE = re.compile(r"sc-domain:[a-z0-9.-]{1,253}|https?://[^\s/?#]{1,253}/[^\s?#]{0,200}")
_BING_SITE_RE = re.compile(r"https?://[^\s/?#]{1,253}/?[^\s?#]{0,200}")
_BING_KEY_RE = re.compile(r"[A-Za-z0-9_-]{16,200}")
_BING_DATE_RE = re.compile(r"/Date\((-?\d{1,15})([+-]\d{4})?\)/")


class SearchEngineError(Exception):
    """A search engine refused or could not be reached. kind: config (what was given is not usable), auth,
    permission, setup (something to do at the engine), rate, server, network (it may work later)."""

    def __init__(self, message: str, kind: str = "error", status: Optional[int] = None) -> None:
        super().__init__(message)
        self.kind = kind
        self.status = status

    @property
    def transient(self) -> bool:
        return self.kind in ("rate", "server", "network")


def _now_ms() -> int:
    return int(time.time() * 1000)


def _clip(text: Any, n: int = 300) -> str:
    s = " ".join(str(text).split())
    return s if len(s) <= n else s[:n - 1] + "…"


# ---------------------------------------------------------------------------
# HTTP (replaced in tests)
# ---------------------------------------------------------------------------

def _http(method: str, url: str, data: Optional[bytes] = None, headers: Optional[Dict[str, str]] = None,
          timeout: float = TIMEOUT) -> Tuple[int, bytes]:
    """(status, body). Error messages never repeat the URL: Bing's carries the API key."""
    import ssl  # here: the in-browser live demo (Pyodide) has no ssl module, and never calls out
    host = urlsplit(url).hostname or "?"
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=dict(headers or {}, **{"User-Agent": "Tidesight/%s" % __version__}))
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ssl.create_default_context()) as res:
            body = res.read(MAX_RESPONSE + 1)
            if len(body) > MAX_RESPONSE:
                raise SearchEngineError("%s sent more than %d MB" % (host, MAX_RESPONSE // 2 ** 20), "server")
            return res.status, body
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, exc.read(1024 * 1024)
        except (OSError, http.client.HTTPException):
            return exc.code, b""
    except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as exc:
        reason = getattr(exc, "reason", None) or exc.__class__.__name__
        raise SearchEngineError("could not reach %s (%s)" % (host, _clip(reason, 120)), "network")


http_request: Callable[..., Tuple[int, bytes]] = _http


def _json(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8")) if raw else {}
    except (ValueError, UnicodeDecodeError, RecursionError):
        return {}


# ---------------------------------------------------------------------------
# Google Search Console
# ---------------------------------------------------------------------------

def _google_error(status: int, doc: Any, prefix: str, email: str = "") -> SearchEngineError:
    msg = ""
    if isinstance(doc, dict):
        err = doc.get("error")
        if isinstance(err, dict):
            msg = str(err.get("message") or "")
        elif isinstance(err, str):
            msg = "%s%s" % (err, ": %s" % doc["error_description"] if doc.get("error_description") else "")
    msg = _clip(msg or "HTTP %d" % status)
    kind = "rate" if status == 429 else "server" if status >= 500 else "auth"
    hint = ""
    if "has not been used in project" in msg or "is disabled" in msg or "SERVICE_DISABLED" in msg:
        kind, hint = "setup", " Enable the Google Search Console API in the key's Google Cloud project."
    elif "sufficient permission" in msg or status == 403:
        kind = "permission"
        hint = (" In Search Console, add %s as a user of this property." % email) if email else ""
    elif "invalid_grant" in msg:
        hint = " The key may have been deleted or disabled in Google Cloud: create a new one."
    return SearchEngineError("%s: %s%s" % (prefix, msg, hint), kind, status)


class Google:
    """Search Console, as a service account (read-only scope)."""

    def __init__(self, cred: Dict[str, Any]) -> None:
        try:
            self.key = rs256.load_private_key(cred.get("private_key") or "")
        except rs256.KeyFormatError as exc:
            raise SearchEngineError("the key file's private key cannot be used (%s)" % exc, "config")
        self.email = str(cred.get("client_email") or "")
        self.key_id = str(cred.get("private_key_id") or "")
        self._token: Optional[str] = None
        self._expires = 0.0

    def token(self) -> str:
        now = time.time()
        if self._token and now < self._expires - 60:
            return self._token
        claims = {"iss": self.email, "scope": GOOGLE_SCOPE, "aud": GOOGLE_TOKEN_URL, "iat": int(now),
                  "exp": int(now) + 3600}
        assertion = rs256.jwt(self.key, claims, {"kid": self.key_id} if self.key_id else {})
        body = urlencode({"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion})
        status, raw = http_request("POST", GOOGLE_TOKEN_URL, body.encode("ascii"),
                                   {"Content-Type": "application/x-www-form-urlencoded"})
        doc = _json(raw)
        if status != 200 or not isinstance(doc, dict) or not isinstance(doc.get("access_token"), str):
            raise _google_error(status, doc, "Google refused the key")
        self._token = doc["access_token"]
        try:
            self._expires = now + float(doc.get("expires_in") or 3600)
        except (TypeError, ValueError):
            self._expires = now + 3600
        return self._token

    def _call(self, method: str, path: str, body: Any = None) -> Dict[str, Any]:
        headers = {"Authorization": "Bearer " + self.token(), "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        status, raw = http_request(method, GOOGLE_API + path, data, headers)
        doc = _json(raw)
        if status != 200 or not isinstance(doc, dict):
            raise _google_error(status, doc, "Search Console refused", self.email)
        return doc

    def sites(self) -> List[Dict[str, Any]]:
        """The Search Console properties this account may read."""
        entries = self._call("GET", "sites").get("siteEntry") or []
        out = [{"url": e["siteUrl"], "permission": e.get("permissionLevel")} for e in entries
               if isinstance(e, dict) and isinstance(e.get("siteUrl"), str)
               and e.get("permissionLevel") != "siteUnverifiedUser"]
        return sorted(out, key=lambda s: (not s["url"].startswith("sc-domain:"), s["url"]))

    def query(self, site: str, body: Dict[str, Any]) -> List[Dict[str, Any]]:
        rows = self._call("POST", "sites/%s/searchAnalytics/query" % quote(site, safe=""), body).get("rows")
        return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []

    def all_rows(self, site: str, body: Dict[str, Any], limit: int = MAX_ROWS_PER_DAY) -> List[Dict[str, Any]]:
        """Every row of a query, page after page."""
        out: List[Dict[str, Any]] = []
        while len(out) < limit:
            page = self.query(site, dict(body, rowLimit=ROW_LIMIT, startRow=len(out)))
            out.extend(page)
            if len(page) < ROW_LIMIT:
                break
        return out[:limit]


def check_google_key(value: Any) -> Dict[str, Any]:
    """The parts of a service account's key file Tidesight keeps (refused with a reason a person can act on)."""
    if isinstance(value, str):
        if len(value) > 50000:
            raise SearchEngineError("this file is too large to be a key file", "config")
        try:
            value = json.loads(value)
        except ValueError:
            raise SearchEngineError("this is not a JSON key file: paste the whole file Google downloaded", "config")
    if not isinstance(value, dict):
        raise SearchEngineError("this is not a JSON key file: paste the whole file Google downloaded", "config")
    if "installed" in value or "web" in value:
        raise SearchEngineError("this is an OAuth client's file, not a service account key: create a key on the "
                                "service account's Keys tab (step 4)", "config")
    if value.get("type") != "service_account":
        raise SearchEngineError("this key file is not a service account's (its type is %r)" % value.get("type"), "config")
    email = value.get("client_email")
    if not isinstance(email, str) or "@" not in email or len(email) > 320:
        raise SearchEngineError("the key file has no client_email", "config")
    pem = value.get("private_key")
    if not isinstance(pem, str) or len(pem) > 20000:
        raise SearchEngineError("the key file has no private_key", "config")
    try:
        rs256.load_private_key(pem)
    except rs256.KeyFormatError as exc:
        raise SearchEngineError("the key file's private key cannot be used (%s)" % exc, "config")
    return {"client_email": email, "private_key": pem, "private_key_id": str(value.get("private_key_id") or "")[:100],
            "project_id": str(value.get("project_id") or "")[:100]}


# ---------------------------------------------------------------------------
# Bing Webmaster Tools
# ---------------------------------------------------------------------------

class Bing:
    """Bing Webmaster Tools' JSON API, with the account's API key."""

    def __init__(self, cred: Dict[str, Any]) -> None:
        self.key = str(cred.get("api_key") or "")

    def call(self, method: str, **params: str) -> List[Dict[str, Any]]:
        url = BING_API + method + "?" + urlencode(dict(params, apikey=self.key))
        status, raw = http_request("GET", url, None, {"Accept": "application/json"})
        doc = _json(raw)
        if status != 200:
            msg = doc.get("Message") or doc.get("message") if isinstance(doc, dict) else None
            msg = _clip(msg or "HTTP %d" % status)
            kind = "rate" if status == 429 else "server" if status >= 500 else "auth"
            if "InvalidApiKey" in msg or "api key" in msg.lower():
                kind, msg = "auth", msg + ". Copy the key again from Bing Webmaster Tools (Settings, API access)."
            elif "NotAuthorized" in msg or "not verified" in msg.lower():
                kind = "permission"
            raise SearchEngineError("Bing refused: " + msg, kind, status)
        d = doc.get("d") if isinstance(doc, dict) else None
        return [x for x in d if isinstance(x, dict)] if isinstance(d, list) else []

    def sites(self) -> List[Dict[str, Any]]:
        out = [{"url": s["Url"], "permission": "verified" if s.get("IsVerified") else "not verified"}
               for s in self.call("GetUserSites") if isinstance(s.get("Url"), str)]
        return sorted(out, key=lambda s: s["url"])


def check_bing_key(value: Any) -> Dict[str, Any]:
    key = value.strip() if isinstance(value, str) else ""
    if not _BING_KEY_RE.fullmatch(key):
        raise SearchEngineError("this is not a Bing API key (letters and digits, from Settings, API access)", "config")
    return {"api_key": key}


def bing_day(value: Any) -> Optional[str]:
    """The day of Bing's "/Date(1399100400000-0700)/" (the offset says which day it means)."""
    m = _BING_DATE_RE.fullmatch(str(value or ""))
    if not m:
        return None
    ms = int(m.group(1))
    if m.group(2):
        sign = 1 if m.group(2)[0] == "+" else -1
        ms += sign * (int(m.group(2)[1:3]) * 60 + int(m.group(2)[3:5])) * 60000
    try:
        return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d")
    except (OverflowError, OSError, ValueError):
        return None


def client(engine: str, cred: Dict[str, Any]) -> Any:
    return Google(cred) if engine == "google" else Bing(cred)


# ---------------------------------------------------------------------------
# The accounts (data_dir/search-engines.json)
# ---------------------------------------------------------------------------

def path(data_dir: str) -> str:
    return os.path.join(data_dir, FILE)


def credentials(data_dir: str) -> Dict[str, Dict[str, Any]]:
    """The connected accounts ({} when none; a file that cannot be read is reported and counts as none)."""
    try:
        table = properties.read_json_table(path(data_dir), "engines", SearchEngineError)
    except SearchEngineError as exc:
        log.error("%s", exc)
        return {}
    return {k: v for k, v in table.items() if k in ENGINES and isinstance(v, dict)}


def save_credentials(data_dir: str, change: Callable[[Dict[str, Dict[str, Any]]], Any]) -> Any:
    """Change the accounts under a lock, written atomically, readable by this account only."""
    with properties.locked(data_dir, name=".search-engines.lock"):
        try:
            table = properties.read_json_table(path(data_dir), "engines", SearchEngineError)
        except SearchEngineError as exc:
            raise SearchEngineError("%s: fix or delete it first" % exc, "config")
        result = change(table)
        properties.write_json(path(data_dir), {"format": "tidesight-search-engines/1",
                                               "updated_at": _now_ms(), "engines": table})
        return result


def describe(creds: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """What the dashboard may show of the accounts: never a key."""
    out: Dict[str, Any] = {}
    g = creds.get("google")
    out["google"] = None if not g else {
        "email": g.get("client_email"), "project_id": g.get("project_id") or None,
        "added_at": g.get("added_at"), "added_by": g.get("added_by"), "demo": bool(g.get("demo"))}
    b = creds.get("bing")
    key = str((b or {}).get("api_key") or "")
    out["bing"] = None if not b else {
        "key_hint": ("…" + key[-4:]) if len(key) >= 12 else "…", "added_at": b.get("added_at"),
        "added_by": b.get("added_by"), "demo": bool(b.get("demo"))}
    return out


def connect_engine(cfg: Config, engine: str, value: Any, user: str) -> Dict[str, Any]:
    """Check an account with the engine (a key that works, the sites it sees), then keep it. Returns its sites."""
    if engine == "google":
        cred = check_google_key(value)
    elif engine == "bing":
        cred = check_bing_key(value)
    else:
        raise SearchEngineError("engine is google or bing", "config")
    sites = client(engine, cred).sites()  # refused keys stop here, before anything is saved
    cred.update(added_at=_now_ms(), added_by=user)

    def put(table: Dict[str, Dict[str, Any]]) -> None:
        table[engine] = cred

    save_credentials(cfg.data_dir, put)
    clear_retries(cfg, engine)
    return {"sites": sites}


def disconnect_engine(cfg: Config, engine: str) -> bool:
    def drop(table: Dict[str, Dict[str, Any]]) -> bool:
        return table.pop(engine, None) is not None

    return bool(save_credentials(cfg.data_dir, drop))


def engine_sites(cfg: Config, engine: str) -> List[Dict[str, Any]]:
    cred = credentials(cfg.data_dir).get(engine)
    if not cred:
        raise SearchEngineError("%s is not connected" % LABELS.get(engine, engine), "config")
    if cred.get("demo"):  # tools/demo.py's made-up account: the sites its properties name
        return [{"url": s.settings[SETTINGS[engine]], "permission": "siteFullUser"} for s in cfg.sites.values()
                if s.settings.get(SETTINGS[engine])]
    return client(engine, cred).sites()


def check_target(engine: str, value: Any) -> str:
    """A property's Search Console property or Bing site, as the engine names it ('' unlinks)."""
    if value in (None, ""):
        return ""
    v = value.strip() if isinstance(value, str) else ""
    if engine == "google":
        if v.startswith("sc-domain:"):
            v = v.lower()
        if not _GOOGLE_SITE_RE.fullmatch(v):
            raise SearchEngineError("a Search Console property is sc-domain:example.com or https://www.example.com/ "
                                    "(as Search Console lists it)", "config")
    elif not _BING_SITE_RE.fullmatch(v):
        raise SearchEngineError("a Bing site is its address, e.g. https://www.example.com/", "config")
    return v


def targets(cfg: Config, site_id: Optional[str] = None) -> List[Tuple[str, str, str]]:
    """(property, engine, the engine's site) for every link set on the properties."""
    out = []
    for sid, site in sorted(cfg.sites.items()):
        if site_id and sid != site_id:
            continue
        for engine in ENGINES:
            target = str(site.settings.get(SETTINGS[engine]) or "")
            if target:
                out.append((sid, engine, target))
    return out


# ---------------------------------------------------------------------------
# search.db
# ---------------------------------------------------------------------------

def db_path(data_dir: str) -> str:
    return os.path.join(data_dir, DB)


def open_db(cfg: Config) -> sqlite3.Connection:
    s = cfg.server
    conn = connect(db_path(cfg.data_dir), s["journal_mode"], s["synchronous"], max(30000, s["busy_timeout_ms"]))
    migrate(conn, SEARCH_MIGRATIONS)
    conn.row_factory = sqlite3.Row
    return conn


def open_readonly(data_dir: str) -> Optional[sqlite3.Connection]:
    p = db_path(data_dir)
    if not os.path.exists(p):
        return None
    conn = connect(p, readonly=True)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("SELECT 1 FROM search_state LIMIT 1")
    except sqlite3.Error:  # created a moment ago, not migrated yet
        conn.close()
        return None
    return conn


def _state(conn: sqlite3.Connection, site_id: str, engine: str) -> Dict[str, Any]:
    row = conn.execute("SELECT * FROM search_state WHERE site_id = ? AND engine = ?", (site_id, engine)).fetchone()
    return dict(row) if row else {}


def _set_state(conn: sqlite3.Connection, site_id: str, engine: str, target: str, ok: bool,
               error: Optional[SearchEngineError] = None) -> None:
    now = _now_ms()
    st = _state(conn, site_id, engine)
    if ok:
        values = (now, now, None, None, None)
    else:
        assert error is not None
        values = (now, st.get("last_ok"), str(error), now, now + (RETRY_SOON_MS if error.transient else RETRY_LATER_MS))
    conn.execute("INSERT OR REPLACE INTO search_state(site_id, engine, target, last_run, last_ok, error, error_at, "
                 "retry_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (site_id, engine, target) + values)


def _reset(conn: sqlite3.Connection, site_id: str, engine: str, target: str) -> None:
    """A property now linked to another site of the engine: the figures of the previous one go."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        for table in ("search_rows", "search_totals", "search_days"):
            conn.execute("DELETE FROM %s WHERE site_id = ? AND engine = ?" % table, (site_id, engine))
        conn.execute("INSERT OR REPLACE INTO search_state(site_id, engine, target) VALUES (?, ?, ?)",
                     (site_id, engine, target))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def clear_retries(cfg: Config, engine: Optional[str] = None, site_id: Optional[str] = None) -> None:
    """Settings changed (a key, a link): the next cron run tries again at once."""
    if not os.path.exists(db_path(cfg.data_dir)):
        return
    conn = open_db(cfg)
    try:
        sql, args = "UPDATE search_state SET retry_at = NULL WHERE 1 = 1", []  # type: Tuple[str, List[Any]]
        if engine:
            sql, args = sql + " AND engine = ?", args + [engine]
        if site_id:
            sql, args = sql + " AND site_id = ?", args + [site_id]
        conn.execute(sql, args)
    finally:
        conn.close()


def status(cfg: Config, site_id: str) -> Dict[str, Any]:
    """Each engine's import for a property: when it last ran, the error if any, the days it holds."""
    out: Dict[str, Any] = {}
    conn = open_readonly(cfg.data_dir)
    if conn is None:
        return out
    try:
        for engine in ENGINES:
            st = _state(conn, site_id, engine)
            span = conn.execute("SELECT MIN(day), MAX(day), COUNT(DISTINCT day) FROM search_totals WHERE site_id = ? "
                                "AND engine = ?", (site_id, engine)).fetchone()
            if not st and not span[2]:
                continue
            out[engine] = {"target": st.get("target"), "last_run": st.get("last_run"), "last_ok": st.get("last_ok"),
                           "error": st.get("error"), "retry_at": st.get("retry_at"),
                           "first_day": span[0], "last_day": span[1], "days": span[2]}
            if engine == "google":
                todo = conn.execute("SELECT COUNT(*) FROM search_days WHERE site_id = ? AND engine = 'google'",
                                    (site_id,)).fetchone()[0]
                out[engine]["days_checked"] = todo
                out[engine]["history_days"] = GOOGLE_HISTORY_DAYS
    finally:
        conn.close()
    return out


# ---------------------------------------------------------------------------
# Imports (jobs.py cron)
# ---------------------------------------------------------------------------

def run(cfg: Config, max_seconds: float = 30.0, site_id: Optional[str] = None, engine: Optional[str] = None,
        force: bool = False) -> Dict[str, Any]:
    """Import what is due for every linked property, within a time budget (the rest at the next run)."""
    from .enrich.db import JobLock
    creds = credentials(cfg.data_dir)
    jobs = [t for t in targets(cfg, site_id) if t[1] in creds and not creds[t[1]].get("demo")
            and (engine is None or t[1] == engine)]
    if not jobs:
        return {}
    lock = JobLock(cfg.data_dir, "search")
    if not lock.acquire():
        return {"skipped": "another search engine import is running"}
    deadline = time.monotonic() + max_seconds
    out: Dict[str, Any] = {}
    clients: Dict[str, Any] = {}
    try:
        conn = open_db(cfg)
        try:
            for sid, eng, target in jobs:
                if time.monotonic() > deadline:
                    break
                st = _state(conn, sid, eng)
                if not force and st.get("retry_at") and st["retry_at"] > _now_ms():
                    continue
                if st.get("target") != target:  # new, or linked to another site of the engine
                    _reset(conn, sid, eng, target)
                try:
                    if eng not in clients:
                        clients[eng] = client(eng, creds[eng])
                    if eng == "google":
                        done = _import_google(conn, clients[eng], sid, target, deadline, force)
                    else:
                        done = _import_bing(conn, clients[eng], sid, target, force)
                    if done is not None:
                        _set_state(conn, sid, eng, target, ok=True)
                        out["%s:%s" % (sid, eng)] = done
                except SearchEngineError as exc:
                    _set_state(conn, sid, eng, target, ok=False, error=exc)
                    out["%s:%s" % (sid, eng)] = {"error": str(exc)}
                    log.warning("search engine import %s:%s: %s", sid, eng, exc)
        finally:
            conn.close()
    finally:
        lock.release()
    return out


def _days_back(today: str, n: int) -> List[str]:
    d0 = datetime.strptime(today, "%Y-%m-%d").date()
    return [(d0 - timedelta(days=i)).isoformat() for i in range(n)]


def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if value == value and abs(value) < 1e15 else None


def _google_row(r: Dict[str, Any], n_keys: int) -> Optional[Tuple[List[str], int, int, Optional[float]]]:
    keys = r.get("keys")
    if not isinstance(keys, list) or len(keys) != n_keys or not all(isinstance(k, str) for k in keys):
        return None
    clicks, impressions = _num(r.get("clicks")), _num(r.get("impressions"))
    if clicks is None or impressions is None:
        return None
    position = _num(r.get("position"))
    return [k[:MAX_TEXT] for k in keys], int(clicks), int(impressions), position if position and position > 0 else None


def _import_google(conn: sqlite3.Connection, g: Google, site_id: str, target: str, deadline: float,
                   force: bool) -> Optional[Dict[str, Any]]:
    """Days not imported yet, newest first, and the recent days again while Google still changes them."""
    now = _now_ms()
    today = local_day(now, PACIFIC)
    days = _days_back(today, GOOGLE_HISTORY_DAYS)
    final_before = days[GOOGLE_FINAL_AFTER] if len(days) > GOOGLE_FINAL_AFTER else today
    seen = {r["day"]: (r["fetched_at"], r["final"]) for r in conn.execute(
        "SELECT day, fetched_at, final FROM search_days WHERE site_id = ? AND engine = 'google'", (site_id,))}
    need = [d for d in days if d not in seen
            or (not seen[d][1] and (force or now - seen[d][0] > GOOGLE_REFRESH_MS))]
    if not need:
        return None
    fetched, rows_in = 0, 0
    new = [d for d in need if d not in seen]
    if new:
        # One call says which of the new days have data at all: an empty day costs nothing more.
        with_data = set()
        for r in g.all_rows(target, {"startDate": min(new), "endDate": max(new), "dimensions": ["date"],
                                      "type": "web", "dataState": "all"}):
            row = _google_row(r, 1)
            if row:
                with_data.add(row[0][0])
        empty = [d for d in new if d not in with_data]
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.executemany("INSERT OR REPLACE INTO search_days(site_id, engine, day, fetched_at, final, rows) "
                             "VALUES (?, 'google', ?, ?, ?, 0)",
                             [(site_id, d, now, 1 if d <= final_before else 0) for d in empty])
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        need = [d for d in need if d not in empty]
    for day in need:  # newest first
        if time.monotonic() > deadline:
            break
        rows_in += _google_day(conn, g, site_id, target, day, day <= final_before)
        fetched += 1
    left = len(need) - fetched
    return {"days": fetched, "rows": rows_in, **({"days_left": left} if left else {})}


def _google_day(conn: sqlite3.Connection, g: Google, site_id: str, target: str, day: str, final: bool) -> int:
    body = {"startDate": day, "endDate": day, "type": "web", "dataState": "all"}
    rows = [x for x in (_google_row(r, 2) for r in g.all_rows(target, dict(body, dimensions=["query", "page"]))) if x]
    totals = [x for x in (_google_row(r, 2) for r in g.all_rows(target, dict(body, dimensions=["country", "device"])))
              if x]
    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute("DELETE FROM search_rows WHERE site_id = ? AND engine = 'google' AND day = ?", (site_id, day))
        conn.execute("DELETE FROM search_totals WHERE site_id = ? AND engine = 'google' AND day = ?", (site_id, day))
        conn.executemany("INSERT INTO search_rows(site_id, engine, day, query, page, clicks, impressions, position) "
                         "VALUES (?, 'google', ?, ?, ?, ?, ?, ?)",
                         [(site_id, day, k[0], k[1], c, i, p) for k, c, i, p in rows])
        conn.executemany("INSERT INTO search_totals(site_id, engine, day, country, device, clicks, impressions, "
                         "position) VALUES (?, 'google', ?, ?, ?, ?, ?, ?)",
                         [(site_id, day, _ALPHA3.get(k[0].lower(), ""), k[1].lower()[:20], c, i, p)
                          for k, c, i, p in totals])
        conn.execute("INSERT OR REPLACE INTO search_days(site_id, engine, day, fetched_at, final, rows) "
                     "VALUES (?, 'google', ?, ?, ?, ?)", (site_id, day, _now_ms(), 1 if final else 0, len(rows)))
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return len(rows)


def _bing_rows(items: Iterable[Dict[str, Any]]) -> Dict[Tuple[str, str], List[float]]:
    """{(day, query or page): [clicks, impressions, position × impressions, impressions with a position]}."""
    out: Dict[Tuple[str, str], List[float]] = {}
    for it in items:
        day, key = bing_day(it.get("Date")), it.get("Query")
        clicks, impressions = _num(it.get("Clicks")), _num(it.get("Impressions"))
        if not day or not isinstance(key, str) or not key or clicks is None or impressions is None:
            continue
        acc = out.setdefault((day, key[:MAX_TEXT]), [0.0, 0.0, 0.0, 0.0])
        acc[0] += clicks
        acc[1] += impressions
        position = _num(it.get("AvgImpressionPosition"))
        if position is not None and position > 0 and impressions > 0:
            acc[2] += position * impressions
            acc[3] += impressions
    return out


def _import_bing(conn: sqlite3.Connection, b: Bing, site_id: str, target: str,
                 force: bool) -> Optional[Dict[str, Any]]:
    """Bing gives everything it has in each call: the days it returns replace those days."""
    st = _state(conn, site_id, "bing")
    if not force and st.get("last_ok") and _now_ms() - st["last_ok"] < BING_REFRESH_MS:
        return None
    queries = _bing_rows(b.call("GetQueryStats", siteUrl=target))
    pages = _bing_rows(b.call("GetPageStats", siteUrl=target))
    traffic: Dict[str, List[float]] = {}
    for it in b.call("GetRankAndTrafficStats", siteUrl=target):
        day = bing_day(it.get("Date"))
        clicks, impressions = _num(it.get("Clicks")), _num(it.get("Impressions"))
        if day and clicks is not None and impressions is not None:
            acc = traffic.setdefault(day, [0.0, 0.0])
            acc[0] += clicks
            acc[1] += impressions

    def position(acc: List[float]) -> Optional[float]:
        return round(acc[2] / acc[3], 2) if acc[3] else None

    conn.execute("BEGIN IMMEDIATE")
    try:
        for day in sorted({d for d, _ in queries}):
            conn.execute("DELETE FROM search_rows WHERE site_id = ? AND engine = 'bing' AND day = ? AND query != ''",
                         (site_id, day))
        for day in sorted({d for d, _ in pages}):
            conn.execute("DELETE FROM search_rows WHERE site_id = ? AND engine = 'bing' AND day = ? AND query = ''",
                         (site_id, day))
        for day in sorted(traffic):
            conn.execute("DELETE FROM search_totals WHERE site_id = ? AND engine = 'bing' AND day = ?", (site_id, day))
        conn.executemany("INSERT INTO search_rows(site_id, engine, day, query, page, clicks, impressions, position) "
                         "VALUES (?, 'bing', ?, ?, '', ?, ?, ?)",
                         [(site_id, d, q, int(a[0]), int(a[1]), position(a)) for (d, q), a in queries.items()])
        conn.executemany("INSERT INTO search_rows(site_id, engine, day, query, page, clicks, impressions, position) "
                         "VALUES (?, 'bing', ?, '', ?, ?, ?, ?)",
                         [(site_id, d, p, int(a[0]), int(a[1]), position(a)) for (d, p), a in pages.items()])
        conn.executemany("INSERT INTO search_totals(site_id, engine, day, country, device, clicks, impressions, "
                         "position) VALUES (?, 'bing', ?, '', '', ?, ?, NULL)",
                         [(site_id, d, int(a[0]), int(a[1])) for d, a in traffic.items()])
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return {"queries": len(queries), "pages": len(pages), "days": len(traffic)}


# ---------------------------------------------------------------------------
# Reports (api.py)
# ---------------------------------------------------------------------------

_METRICS = ("SUM(clicks) AS clicks, SUM(impressions) AS impressions, SUM(position * impressions) AS pos_w, "
            "SUM(CASE WHEN position IS NOT NULL THEN impressions ELSE 0 END) AS pos_n")
TABS = {"queries": ("search_rows", "query"), "pages": ("search_rows", "page"),
        "countries": ("search_totals", "country"), "devices": ("search_totals", "device")}


def _finish(row: Any) -> Dict[str, Any]:
    clicks, impressions = int(row["clicks"] or 0), int(row["impressions"] or 0)
    pos = row["pos_w"] / row["pos_n"] if row["pos_n"] else None
    return {"clicks": clicks, "impressions": impressions,
            "ctr": round(clicks / impressions, 4) if impressions else None,
            "position": round(pos, 1) if pos is not None else None}


def _where(site_id: str, engine: str, start: str, end: str, query: Optional[str],
           page: Optional[str]) -> Tuple[str, List[Any]]:
    sql = "site_id = ? AND engine = ? AND day BETWEEN ? AND ?"
    args: List[Any] = [site_id, engine, start, end]
    if query is not None:
        sql, args = sql + " AND query = ?", args + [query]
    if page is not None:
        sql, args = sql + " AND page = ?", args + [page]
    return sql, args


def totals(conn: sqlite3.Connection, site_id: str, engine: str, start: str, end: str, query: Optional[str] = None,
           page: Optional[str] = None) -> Dict[str, Any]:
    """Clicks, impressions, CTR and average position of a period: from the engine's totals (hidden queries
    included), or from its rows for one query or page."""
    if query is None and page is None:
        where, args = _where(site_id, engine, start, end, None, None)
        row = conn.execute("SELECT %s, COUNT(*) AS n FROM search_totals WHERE %s" % (_METRICS, where), args).fetchone()
        if row["n"]:
            out = _finish(row)
            if out["position"] is None:  # Bing's totals have none: its queries' average
                pos = conn.execute("SELECT %s FROM search_rows WHERE %s AND query != ''" % (_METRICS, where), args).fetchone()
                out["position"] = _finish(pos)["position"]
            return out
    where, args = _where(site_id, engine, start, end, query, page)
    side = " AND query != ''" if page is None else ""
    return _finish(conn.execute("SELECT %s FROM search_rows WHERE %s%s" % (_METRICS, where, side), args).fetchone())


def series(conn: sqlite3.Connection, site_id: str, engine: str, start: str, end: str, query: Optional[str] = None,
           page: Optional[str] = None) -> Dict[str, Dict[str, Any]]:
    """The same figures per day: {day: figures}."""
    table, side = "search_rows", (" AND query != ''" if page is None else "")
    where, args = _where(site_id, engine, start, end, query, page)
    if query is None and page is None and conn.execute(
            "SELECT 1 FROM search_totals WHERE %s LIMIT 1" % where, args).fetchone():
        table, side = "search_totals", ""
    return {r["day"]: _finish(r) for r in conn.execute(
        "SELECT day, %s FROM %s WHERE %s%s GROUP BY day" % (_METRICS, table, where, side), args)}


def rows(conn: sqlite3.Connection, site_id: str, engine: str, start: str, end: str, tab: str,
         query: Optional[str] = None, page: Optional[str] = None, limit: int = 1000,
         values: Optional[Sequence[str]] = None) -> Tuple[List[Dict[str, Any]], int]:
    """(rows of a tab, sorted by clicks then impressions; how many there are). `values`: only these."""
    table, col = TABS[tab]
    if table == "search_totals":
        where, args = _where(site_id, engine, start, end, None, None)
    else:
        where, args = _where(site_id, engine, start, end, query, page)
    where += " AND %s != ''" % col
    out: List[Dict[str, Any]] = []
    if values is not None:
        vals = list(values)
        for i in range(0, len(vals), 500):
            part = vals[i:i + 500]
            out += [dict(_finish(r), value=r["value"]) for r in conn.execute(
                "SELECT %s AS value, %s FROM %s WHERE %s AND %s IN (%s) GROUP BY %s"
                % (col, _METRICS, table, where, col, ",".join("?" * len(part)), col), args + part)]
        return out, len(out)
    total = conn.execute("SELECT COUNT(DISTINCT %s) FROM %s WHERE %s" % (col, table, where), args).fetchone()[0]
    out = [dict(_finish(r), value=r["value"]) for r in conn.execute(
        "SELECT %s AS value, %s FROM %s WHERE %s GROUP BY %s ORDER BY clicks DESC, impressions DESC, value LIMIT ?"
        % (col, _METRICS, table, where, col), args + [limit])]
    return out, total


def coverage(conn: sqlite3.Connection, site_id: str, engine: str, start: str, end: str,
             total_clicks: int) -> Optional[float]:
    """The share of clicks whose query is known (Google keeps rare queries to itself)."""
    if not total_clicks:
        return None
    where, args = _where(site_id, engine, start, end, None, None)
    known = conn.execute("SELECT SUM(clicks) FROM search_rows WHERE %s AND query != '' AND page != ''" % where,
                         args).fetchone()[0]
    if engine == "bing":
        known = conn.execute("SELECT SUM(clicks) FROM search_rows WHERE %s AND query != ''" % where, args).fetchone()[0]
    return round(min(1.0, (known or 0) / total_clicks), 4)


def span(conn: sqlite3.Connection, site_id: str, engine: str) -> Tuple[Optional[str], Optional[str]]:
    row = conn.execute("SELECT MIN(day), MAX(day) FROM search_totals WHERE site_id = ? AND engine = ?",
                       (site_id, engine)).fetchone()
    if row[0] is None:
        row = conn.execute("SELECT MIN(day), MAX(day) FROM search_rows WHERE site_id = ? AND engine = ?",
                           (site_id, engine)).fetchone()
    return row[0], row[1]


def page_path(url: str) -> str:
    """The path of a page's address (how Tidesight's own reports name it)."""
    try:
        return urlsplit(url).path or "/"
    except ValueError:
        return "/"
