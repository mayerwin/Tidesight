"""Download and refresh the offline geolocation databases.

Meant to run from cron through the jobs CLI. Each helper downloads a
database, checks that it opens as a valid MaxMind DB, then atomically
replaces the previous copy: the file is written to a temporary name in the
destination folder and moved into place with ``os.replace``. Readers never
see a partial file, and a failed download leaves the old database in place.

- **MaxMind GeoLite2 / GeoIP2** need an account ID and a license key (free
  for GeoLite2). The databases must not be redistributed.
- **DB-IP Lite** needs no key. Its data is licensed CC BY 4.0, so pages that
  show it must credit DB-IP ("IP Geolocation by DB-IP", https://db-ip.com).

Standard library only (``urllib``); system proxy settings are honoured.
"""

from __future__ import annotations

import base64
import datetime
import functools
import gzip
import os
import posixpath
import re
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from typing import IO, Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import mmdb

__all__ = ["GeoUpdateError", "download_dbip", "download_maxmind"]

USER_AGENT = "tidesight-geo-update"
TIMEOUT_SECONDS = 60  # per connection attempt and per read, not for the whole download
CHUNK_SIZE = 1024 * 1024
MAX_DATABASE_SIZE = 2 * 1024 ** 3  # refuse anything larger once decompressed

MAXMIND_BASE_URL = "https://download.maxmind.com"
DBIP_BASE_URL = "https://download.db-ip.com/free"
DBIP_KINDS = ("city", "country", "asn")

_EDITION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_MONTH = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
# A few lookups exercise the search tree and data section of a new file.
_PROBE_ADDRESSES = ("1.1.1.1", "8.8.8.8", "81.2.69.160", "2001:4860:4860::8888", "2a01:cb00::1")

_MAXMIND_HINTS = {
    401: "check the MaxMind account ID and license key",
    403: "this account may not download this edition",
    404: "unknown edition",
    429: "MaxMind's daily download limit was reached; try again later",
}
_DBIP_HINTS = {429: "DB-IP is rate limiting downloads; try again later"}


class GeoUpdateError(RuntimeError):
    """One or more databases could not be downloaded or verified.

    Each database is attempted even when another fails; ``updated`` lists
    the files that were replaced successfully.
    """

    def __init__(self, message: str, updated: Optional[Sequence[str]] = None) -> None:
        super().__init__(message)
        self.updated: List[str] = list(updated or [])


def download_maxmind(
    dest_dir: str,
    account_id: str,
    license_key: str,
    editions: Sequence[str] = ("GeoLite2-City", "GeoLite2-ASN"),
    base_url: str = MAXMIND_BASE_URL,
) -> List[str]:
    """Download MaxMind editions into *dest_dir* as ``<edition>.mmdb``.

    Fetches ``{base_url}/geoip/databases/{edition}/download?suffix=tar.gz``
    with HTTP Basic auth and extracts the ``.mmdb`` from the archive. The
    credentials go to *base_url* only, never to the pre-signed storage URL
    MaxMind redirects to. Returns the paths that were replaced.
    """
    account, key = str(account_id).strip(), str(license_key).strip()
    if not account or not key:
        raise ValueError("MaxMind downloads need an account ID and a license key")
    names = _check_names(editions, _EDITION, "edition")
    base = _check_base_url(base_url)
    token = base64.b64encode(f"{account}:{key}".encode("utf-8")).decode("ascii")
    os.makedirs(dest_dir, exist_ok=True)
    updated: List[str] = []
    failures: List[Tuple[str, BaseException]] = []
    for edition in names:
        url = f"{base}/geoip/databases/{edition}/download?suffix=tar.gz"
        dest = os.path.join(dest_dir, f"{edition}.mmdb")
        try:
            with _open_url(url, authorization=f"Basic {token}") as response:
                _install(dest, functools.partial(_extract_mmdb, response, f"{edition}.mmdb"))
        except Exception as exc:  # report every edition, then raise once
            failures.append((f"{edition}: {_describe(exc, _MAXMIND_HINTS)}", exc))
        else:
            updated.append(dest)
    _raise_failures("MaxMind", failures, updated)
    return updated


def download_dbip(
    dest_dir: str,
    kinds: Sequence[str] = ("city", "asn"),
    month: Optional[str] = None,
    base_url: str = DBIP_BASE_URL,
) -> List[str]:
    """Download DB-IP Lite databases into *dest_dir* as ``dbip-<kind>-lite.mmdb``.

    Fetches ``{base_url}/dbip-{kind}-lite-{YYYY-MM}.mmdb.gz`` for the current
    UTC month, or the previous month while the current one is not published
    yet (HTTP 404). An explicit *month* ("YYYY-MM") is fetched as is, without
    fallback. *kinds* are among "city", "country" and "asn". Returns the
    paths that were replaced.
    """
    names = [kind.lower() for kind in _check_names(kinds, None, "kind")]
    unknown = [kind for kind in names if kind not in DBIP_KINDS]
    if unknown:
        raise ValueError(f"unknown DB-IP kind(s) {unknown}; expected some of {DBIP_KINDS}")
    if month is not None and not _MONTH.match(month):
        raise ValueError(f"month must look like 2026-10, not {month!r}")
    months = [month] if month is not None else _recent_months(_utc_today())
    base = _check_base_url(base_url)
    os.makedirs(dest_dir, exist_ok=True)
    updated: List[str] = []
    failures: List[Tuple[str, BaseException]] = []
    for kind in names:
        dest = os.path.join(dest_dir, f"dbip-{kind}-lite.mmdb")
        urls = [f"{base}/dbip-{kind}-lite-{release}.mmdb.gz" for release in months]
        try:
            with _open_first_published(urls) as response:
                _install(dest, functools.partial(_gunzip, response))
        except Exception as exc:  # report every kind, then raise once
            failures.append((f"dbip-{kind}-lite: {_describe(exc, _DBIP_HINTS)}", exc))
        else:
            updated.append(dest)
    _raise_failures("DB-IP", failures, updated)
    return updated


def _open_url(url: str, authorization: Optional[str] = None) -> Any:
    """GET *url*; HTTP errors raise urllib.error.HTTPError."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    if authorization:
        # Unredirected headers are not copied to the request a redirect makes.
        request.add_unredirected_header("Authorization", authorization)
    return urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)


def _open_first_published(urls: Sequence[str]) -> Any:
    """Open the first URL that is not a 404."""
    for url in urls:
        try:
            return _open_url(url)
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code != 404:
                raise
    tried = ", ".join(posixpath.basename(urllib.parse.urlsplit(url).path) for url in urls)
    raise GeoUpdateError(f"not published (HTTP 404 for {tried})")


def _install(dest: str, write: Callable[[IO[bytes]], None]) -> None:
    """Write a new database with *write*, verify it, then move it over *dest*."""
    directory = os.path.dirname(os.path.abspath(dest))
    prefix = f".{os.path.basename(dest)}."
    handle, temp_path = tempfile.mkstemp(prefix=prefix, suffix=".part", dir=directory)
    try:
        with os.fdopen(handle, "wb") as out:
            write(out)
            out.flush()
            os.fsync(out.fileno())
        _verify(temp_path)
        os.chmod(temp_path, 0o644)
        os.replace(temp_path, dest)
    except BaseException:
        _remove(temp_path)
        raise


def _verify(path: str) -> None:
    """Raise GeoUpdateError unless *path* opens and answers lookups."""
    try:
        with mmdb.Reader(path, use_mmap=True, cache_size=0) as reader:
            for address in _PROBE_ADDRESSES:
                if ":" in address and reader.metadata.ip_version == 4:
                    continue
                reader.get(address)
    except mmdb.InvalidDatabaseError as exc:
        raise GeoUpdateError(f"the download is not a valid MaxMind DB: {exc}") from exc


def _extract_mmdb(source: IO[bytes], member_name: str, out: IO[bytes]) -> None:
    """Copy *member_name* out of a streamed .tar.gz without touching other paths."""
    wanted = member_name.lower()
    with tarfile.open(fileobj=source, mode="r|gz") as archive:
        for member in archive:
            if member.isfile() and posixpath.basename(member.name).lower() == wanted:
                extracted = archive.extractfile(member)
                if extracted is not None:
                    _copy_stream(extracted, out)
                    return
    raise GeoUpdateError(f"the archive does not contain {member_name}")


def _gunzip(source: IO[bytes], out: IO[bytes]) -> None:
    with gzip.GzipFile(fileobj=source, mode="rb") as stream:
        _copy_stream(stream, out)


def _copy_stream(source: IO[bytes], out: IO[bytes]) -> None:
    total = 0
    while True:
        chunk = source.read(CHUNK_SIZE)
        if not chunk:
            return
        total += len(chunk)
        if total > MAX_DATABASE_SIZE:
            raise GeoUpdateError(f"the database is larger than {MAX_DATABASE_SIZE} bytes")
        out.write(chunk)


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _describe(exc: BaseException, hints: Dict[int, str]) -> str:
    """A one-line, credential-free description of a download failure."""
    if isinstance(exc, urllib.error.HTTPError):
        hint = hints.get(exc.code)
        message = f"HTTP {exc.code} {exc.reason}"
        return f"{message} ({hint})" if hint else message
    if isinstance(exc, urllib.error.URLError):
        return f"cannot connect: {exc.reason}"
    return str(exc) or type(exc).__name__


def _raise_failures(source: str, failures: List[Tuple[str, BaseException]], updated: List[str]) -> None:
    if failures:
        details = "; ".join(message for message, _ in failures)
        raise GeoUpdateError(f"{source} update failed: {details}", updated) from failures[0][1]


def _check_names(values: Sequence[str], pattern: Optional["re.Pattern[str]"], what: str) -> List[str]:
    if isinstance(values, str):
        values = [values]
    names = [str(value).strip() for value in values]
    if not names:
        raise ValueError(f"no {what} to download")
    for name in names:
        if pattern is not None and not pattern.match(name):
            raise ValueError(f"invalid {what} {name!r}")
    return names


def _check_base_url(base_url: str) -> str:
    if urllib.parse.urlsplit(base_url).scheme not in ("http", "https"):
        raise ValueError(f"base_url must be an http(s) URL, not {base_url!r}")
    return base_url.rstrip("/")


def _utc_today() -> datetime.date:
    return datetime.datetime.now(datetime.timezone.utc).date()


def _recent_months(today: datetime.date) -> List[str]:
    """The current month and the one before, as "YYYY-MM"."""
    year, month = (today.year, today.month - 1) if today.month > 1 else (today.year - 1, 12)
    return [f"{today.year:04d}-{today.month:02d}", f"{year:04d}-{month:02d}"]
