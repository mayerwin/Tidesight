"""Pure-Python reader for MaxMind DB (``.mmdb``) files.

MaxMind DB is the binary format of the MaxMind GeoLite2/GeoIP2, DB-IP and
IPinfo databases (format spec 2.0: https://maxmind.github.io/MaxMind-DB/).
This module reads it with the standard library only, so geolocation works on
shared hosting where the ``maxminddb`` C extension cannot be installed.
:func:`open_reader` uses that extension instead when it is available, because
it is several times faster; both expose the same small interface
(``metadata``, ``get``, ``get_with_prefix_len``, ``close``).

Layout of a file::

    search tree | 16 zero bytes | data section | marker | metadata map

The decoder applies the spec's reader resource limits, so a corrupt or
hostile file cannot exhaust CPU, memory or the Python stack: one decode may
nest at most 512 levels (maps, arrays and pointers), produce at most 65,536
values and materialise at most 2 MiB of string and byte data. Anything
malformed raises :class:`InvalidDatabaseError`, never a partial result.

Decoded values are cached by data-section offset (an LRU bounded by entry
count and by payload size), which also memoises shared pointer targets such
as country maps. Callers always receive their own copies of maps and lists.
"""

from __future__ import annotations

import importlib
import ipaddress
import mmap
import os
import struct
from collections import OrderedDict
from typing import Any, Dict, List, Mapping, Optional, Protocol, Tuple, Union

__all__ = [
    "DEFAULT_CACHE_SIZE",
    "PURE_ENV_VAR",
    "ExtensionReader",
    "InvalidDatabaseError",
    "Metadata",
    "Reader",
    "ReaderLike",
    "open_reader",
    "read_metadata",
]

METADATA_MARKER = b"\xab\xcd\xefMaxMind.com"
METADATA_MAX_SIZE = 128 * 1024  # the metadata section, marker included
DATA_SECTION_SEPARATOR_SIZE = 16

DEFAULT_CACHE_SIZE = 4096
CACHE_MAX_PAYLOAD = 32 * 1024 * 1024  # string/bytes data held by one cache

# Reader resource limits (spec section "Reader Resource Limits").
MAX_DEPTH = 512
MAX_VALUES = 65536
MAX_PAYLOAD = 2 * 1024 * 1024

PURE_ENV_VAR = "TIDESIGHT_PURE_MMDB"

# Data section type numbers. Types 8 and above use the "extended" encoding.
TYPE_EXTENDED = 0
TYPE_POINTER = 1
TYPE_UTF8_STRING = 2
TYPE_DOUBLE = 3
TYPE_BYTES = 4
TYPE_UINT16 = 5
TYPE_UINT32 = 6
TYPE_MAP = 7
TYPE_INT32 = 8
TYPE_UINT64 = 9
TYPE_UINT128 = 10
TYPE_ARRAY = 11
TYPE_DATA_CACHE_CONTAINER = 12
TYPE_END_MARKER = 13
TYPE_BOOLEAN = 14
TYPE_FLOAT = 15

_TYPE_NAMES = {
    TYPE_POINTER: "pointer",
    TYPE_UTF8_STRING: "utf8_string",
    TYPE_DOUBLE: "double",
    TYPE_BYTES: "bytes",
    TYPE_UINT16: "uint16",
    TYPE_UINT32: "uint32",
    TYPE_MAP: "map",
    TYPE_INT32: "int32",
    TYPE_UINT64: "uint64",
    TYPE_UINT128: "uint128",
    TYPE_ARRAY: "array",
    TYPE_DATA_CACHE_CONTAINER: "data cache container",
    TYPE_END_MARKER: "end marker",
    TYPE_BOOLEAN: "boolean",
    TYPE_FLOAT: "float",
}
_UINT_SIZES = {TYPE_UINT16: 2, TYPE_UINT32: 4, TYPE_UINT64: 8, TYPE_UINT128: 16}

_UINT32 = struct.Struct(">I").unpack_from
_DOUBLE = struct.Struct(">d").unpack_from
_FLOAT = struct.Struct(">f").unpack_from

_CONTAINERS = frozenset((dict, list))
_METADATA_FIELDS = (
    "node_count",
    "record_size",
    "ip_version",
    "database_type",
    "languages",
    "binary_format_major_version",
    "binary_format_minor_version",
    "build_epoch",
    "description",
)

Buffer = Union[bytes, mmap.mmap]
IPAddress = Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
IPInput = Union[str, ipaddress.IPv4Address, ipaddress.IPv6Address]
# (value, values counted, payload bytes, height below the entry's depth)
_CacheEntry = Tuple[Any, int, int, int]


class InvalidDatabaseError(RuntimeError):
    """The file is not a valid MaxMind DB, or the part being read is corrupt."""


class Metadata:
    """The metadata map stored at the end of a MaxMind DB file."""

    def __init__(self, values: Mapping[str, Any]) -> None:
        self.node_count = _required_int(values, "node_count")
        self.record_size = _required_int(values, "record_size")
        self.ip_version = _required_int(values, "ip_version")
        self.database_type = _required_str(values, "database_type")
        self.binary_format_major_version = _required_int(values, "binary_format_major_version")
        self.binary_format_minor_version = _required_int(values, "binary_format_minor_version")
        self.build_epoch = _required_int(values, "build_epoch")
        self.languages: List[str] = _string_list(values.get("languages"))
        self.description: Dict[str, str] = _string_map(values.get("description"))
        if self.binary_format_major_version != 2:
            raise InvalidDatabaseError(
                f"unsupported binary format major version {self.binary_format_major_version}"
            )
        if self.record_size not in (24, 28, 32):
            raise InvalidDatabaseError(f"unsupported record size {self.record_size}")
        if self.ip_version not in (4, 6):
            raise InvalidDatabaseError(f"unsupported IP version {self.ip_version}")

    @classmethod
    def from_object(cls, source: Any) -> "Metadata":
        """Build from any object with the same attributes (e.g. maxminddb's)."""
        values = {name: getattr(source, name, None) for name in _METADATA_FIELDS}
        return cls({name: value for name, value in values.items() if value is not None})

    @property
    def node_byte_size(self) -> int:
        """Size of one search tree node in bytes (two records)."""
        return self.record_size // 4

    @property
    def search_tree_size(self) -> int:
        """Size of the search tree in bytes."""
        return self.node_count * self.node_byte_size

    def as_dict(self) -> Dict[str, Any]:
        """The metadata as a plain dict (handy for comparisons and logs)."""
        return {name: getattr(self, name) for name in _METADATA_FIELDS}

    def __repr__(self) -> str:
        return (
            f"Metadata(database_type={self.database_type!r}, ip_version={self.ip_version}, "
            f"record_size={self.record_size}, node_count={self.node_count}, "
            f"build_epoch={self.build_epoch})"
        )


class ReaderLike(Protocol):
    """What :func:`open_reader` returns, whichever implementation backs it."""

    metadata: Metadata

    def get(self, ip: IPInput) -> Optional[Any]: ...

    def get_with_prefix_len(self, ip: IPInput) -> Tuple[Optional[Any], int]: ...

    def close(self) -> None: ...


class Reader:
    """Reads a MaxMind DB file with the standard library only.

    The whole file is read into memory, or memory-mapped with
    ``use_mmap=True`` (less private memory, but on Windows the file cannot be
    replaced while it is open). A Reader is not thread-safe: use one per
    thread.
    """

    def __init__(
        self,
        path: Union[str, "os.PathLike[str]"],
        *,
        use_mmap: bool = False,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        if cache_size < 0:
            raise ValueError("cache_size must not be negative")
        self.path = os.fspath(path)
        buffer = _load(self.path, use_mmap)
        try:
            marker = _find_marker(buffer, self.path)
            self.metadata = _decode_metadata(buffer, marker, self.path)
            data_start = _data_section_start(self.metadata, marker, self.path)
        except BaseException:
            if isinstance(buffer, mmap.mmap):
                buffer.close()
            raise
        self._buffer: Optional[Buffer] = buffer
        self._node_count = self.metadata.node_count
        self._record_size = self.metadata.record_size
        self._ip_version = self.metadata.ip_version
        self._data_start = data_start
        self._data_end = marker
        self._decoder: Optional[_Decoder] = _Decoder(buffer, data_start, marker, cache_size)
        self._ipv4_start = 0
        if self._ip_version == 6:
            # IPv4 addresses live under ::/96; walk its 96 zero bits once.
            self._ipv4_start = self._walk(0, 96, 0)[0]

    @property
    def closed(self) -> bool:
        """True once :meth:`close` has been called."""
        return self._buffer is None

    def get(self, ip: IPInput) -> Optional[Any]:
        """Return the record for *ip*, or None when the database has none."""
        return self.get_with_prefix_len(ip)[0]

    def get_with_prefix_len(self, ip: IPInput) -> Tuple[Optional[Any], int]:
        """Return ``(record, prefix_len)`` for *ip*.

        ``prefix_len`` is the length of the network the answer applies to, in
        bits of the queried address family: an IPv4 lookup in an IPv6 tree
        reports an IPv4 prefix (0-32), as the official readers do. Invalid
        address strings raise ValueError, and so does an IPv6 address in an
        IPv4-only database.
        """
        record, prefix_len = self._find(ip)
        if record == self._node_count:
            return None, prefix_len
        return _copy(self._resolve(record)), prefix_len

    def close(self) -> None:
        """Release the file contents. Lookups afterwards raise ValueError."""
        buffer = self._buffer
        self._buffer = None
        self._decoder = None
        if isinstance(buffer, mmap.mmap):
            buffer.close()

    def __enter__(self) -> "Reader":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "closed" if self.closed else "open"
        return f"<Reader {self.path!r} {self.metadata.database_type!r} {state}>"

    def _find(self, ip: IPInput) -> Tuple[int, int]:
        """Walk the search tree; return the final record value and depth."""
        if self._buffer is None:
            raise ValueError("Attempt to read from a closed MaxMind DB.")
        address = _parse_ip(ip)
        if address.version == 6:
            if self._ip_version == 4:
                raise ValueError(
                    f"Error looking up {ip}. You attempted to look up an IPv6 address "
                    "in an IPv4-only database."
                )
            record, depth = self._walk(int(address), 128, 0)
        else:
            record, depth = self._walk(int(address), 32, self._ipv4_start)
        if record < self._node_count:
            raise InvalidDatabaseError("Invalid node in search tree")
        return record, depth

    def _walk(self, number: int, bit_count: int, node: int) -> Tuple[int, int]:
        """Follow the bits of *number* from *node* until leaving the tree."""
        buffer = self._buffer
        node_count = self._node_count
        record_size = self._record_size
        mask = 1 << (bit_count - 1)
        depth = 0
        # Each branch reads 4 bytes big-endian; 24-bit records drop the last
        # byte (still inside the tree or the 16-byte separator).
        if record_size == 24:
            while depth < bit_count and node < node_count:
                offset = node * 6 + 3 if number & mask else node * 6
                node = _UINT32(buffer, offset)[0] >> 8
                mask >>= 1
                depth += 1
        elif record_size == 28:
            while depth < bit_count and node < node_count:
                if number & mask:
                    node = _UINT32(buffer, node * 7 + 3)[0] & 0x0FFFFFFF
                else:
                    value = _UINT32(buffer, node * 7)[0]
                    node = ((value & 0xF0) << 20) | (value >> 8)
                mask >>= 1
                depth += 1
        else:
            while depth < bit_count and node < node_count:
                offset = node * 8 + 4 if number & mask else node * 8
                node = _UINT32(buffer, offset)[0]
                mask >>= 1
                depth += 1
        return node, depth

    def _resolve(self, record: int) -> Any:
        """Decode the data a search tree record points to (internal object)."""
        offset = self._data_start + record - self._node_count - DATA_SECTION_SEPARATOR_SIZE
        if not self._data_start <= offset < self._data_end:
            raise InvalidDatabaseError("The MaxMind DB file's search tree is corrupt")
        decoder = self._decoder
        if decoder is None:  # pragma: no cover - closed between _find and here
            raise ValueError("Attempt to read from a closed MaxMind DB.")
        return decoder.decode(offset)


class ExtensionReader:
    """Wraps a ``maxminddb`` C-extension reader in this module's interface.

    ``metadata`` becomes an attribute holding a :class:`Metadata`, and the
    package's own InvalidDatabaseError is re-raised as this module's.
    """

    def __init__(self, reader: Any, path: str, invalid_error: Any) -> None:
        self.path = path
        self._reader = reader
        self._invalid_error = invalid_error
        try:
            self.metadata = Metadata.from_object(reader.metadata())
        except BaseException:
            reader.close()
            raise

    def get(self, ip: IPInput) -> Optional[Any]:
        """Return the record for *ip*, or None when the database has none."""
        try:
            return self._reader.get(ip)
        except self._invalid_error as exc:
            raise InvalidDatabaseError(str(exc)) from exc

    def get_with_prefix_len(self, ip: IPInput) -> Tuple[Optional[Any], int]:
        """Return ``(record, prefix_len)`` for *ip* (see Reader)."""
        try:
            return self._reader.get_with_prefix_len(ip)
        except self._invalid_error as exc:
            raise InvalidDatabaseError(str(exc)) from exc

    def close(self) -> None:
        """Close the underlying reader."""
        self._reader.close()

    def __enter__(self) -> "ExtensionReader":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<ExtensionReader {self.path!r} {self.metadata.database_type!r}>"


def open_reader(path: Union[str, "os.PathLike[str]"], *, use_mmap: bool = False) -> ReaderLike:
    """Open a database with the fastest reader available.

    Uses the ``maxminddb`` package when its C extension is installed, unless
    the TIDESIGHT_PURE_MMDB environment variable is set (to anything but
    "", "0", "false", "no" or "off"); otherwise returns the pure-Python
    :class:`Reader`. Both expose ``.metadata`` (a :class:`Metadata`),
    ``.get(ip)``, ``.get_with_prefix_len(ip)`` and ``.close()``, and both
    raise :class:`InvalidDatabaseError` for corrupt files.
    """
    if not _pure_forced():
        reader = _open_extension(os.fspath(path))
        if reader is not None:
            return reader
    return Reader(path, use_mmap=use_mmap)


def read_metadata(path: Union[str, "os.PathLike[str]"]) -> Metadata:
    """Read only the metadata of a database (reads at most the last 128 KiB).

    Also checks that the search tree fits in the file, so a truncated file
    fails here rather than at the first lookup.
    """
    path = os.fspath(path)
    with open(path, "rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        start = max(0, size - METADATA_MAX_SIZE)
        handle.seek(start)
        tail = handle.read()
    marker = _find_marker(tail, path)
    metadata = _decode_metadata(tail, marker, path)
    _data_section_start(metadata, start + marker, path)
    return metadata


class _Decoder:
    """Decodes values from one section (data or metadata) of a buffer.

    ``base`` is the offset pointers are relative to and ``end`` where the
    section stops. Decoded entries are cached by absolute offset; cached
    objects are shared internally and must only leave through :func:`_copy`.
    """

    def __init__(self, buffer: Buffer, base: int, end: int, cache_size: int) -> None:
        self._buffer = buffer
        self._base = base
        self._end = end
        self._cache: "OrderedDict[int, _CacheEntry]" = OrderedDict()
        self._cache_size = cache_size
        self._cache_payload = 0
        # Resource accounting for the decode operation in progress.
        self._values = 0
        self._payload = 0
        self._deepest = 0

    def decode(self, offset: int) -> Any:
        """Decode the data entry at absolute *offset* as one bounded operation."""
        entry = self._cache.get(offset)
        if entry is not None:
            self._cache.move_to_end(offset)
            return entry[0]
        self._values = self._payload = self._deepest = 0
        try:
            value = self._decode(offset, 0)[0]
        except InvalidDatabaseError:
            raise
        except (IndexError, ValueError, OverflowError, RecursionError, struct.error) as exc:
            raise InvalidDatabaseError(f"corrupt data at offset {offset}: {exc}") from exc
        self._remember(offset, (value, self._values, self._payload, self._deepest))
        return value

    def _decode(self, offset: int, depth: int) -> Tuple[Any, int]:
        """Decode one value at *offset*; return it and the offset after it.

        Pointers are followed inline (one stack frame per level), so the
        depth limit is reached long before Python's recursion limit.
        """
        buffer = self._buffer
        end = self._end
        if offset >= end:
            raise InvalidDatabaseError(f"offset {offset} is past the end of the section")
        ctrl = buffer[offset]
        offset += 1
        type_num = ctrl >> 5

        if type_num == TYPE_POINTER:
            pointer_size = (ctrl >> 3) & 0x3
            if pointer_size == 0:
                pointer = ((ctrl & 0x7) << 8) | buffer[offset]
            elif pointer_size == 1:
                pointer = (((ctrl & 0x7) << 16) | (buffer[offset] << 8) | buffer[offset + 1]) + 2048
            elif pointer_size == 2:
                pointer = (
                    ((ctrl & 0x7) << 24)
                    | (buffer[offset] << 16)
                    | (buffer[offset + 1] << 8)
                    | buffer[offset + 2]
                ) + 526336
            else:
                pointer = _UINT32(buffer, offset)[0]
            offset += pointer_size + 1
            if offset > end:
                raise InvalidDatabaseError("truncated pointer")
            depth += 1
            if depth > MAX_DEPTH:
                raise InvalidDatabaseError(f"data nested deeper than {MAX_DEPTH} levels")
            target = self._base + pointer
            entry = self._cache.get(target)
            if entry is not None:
                self._cache.move_to_end(target)
                self._charge(entry, depth)
                return entry[0], offset
            if target >= end:
                raise InvalidDatabaseError(f"pointer to offset {target} is past the end of the section")
            if buffer[target] >> 5 == TYPE_POINTER:
                raise InvalidDatabaseError("pointer points to another pointer")
            values, payload, deepest = self._values, self._payload, self._deepest
            self._deepest = depth
            value = self._decode(target, depth)[0]
            self._remember(
                target,
                (value, self._values - values, self._payload - payload, self._deepest - depth),
            )
            if deepest > self._deepest:
                self._deepest = deepest
            return value, offset

        values = self._values + 1
        if values > MAX_VALUES:
            raise InvalidDatabaseError(f"data entry decodes to more than {MAX_VALUES} values")
        self._values = values

        if type_num == TYPE_EXTENDED:
            if offset >= end:
                raise InvalidDatabaseError("truncated extended type")
            type_num = 7 + buffer[offset]
            offset += 1
            if type_num < 8 or type_num > 15:
                raise InvalidDatabaseError(f"invalid extended type number {type_num}")

        size = ctrl & 0x1F
        if size >= 29:
            if size == 29:
                size = 29 + buffer[offset]
                offset += 1
            elif size == 30:
                size = 285 + ((buffer[offset] << 8) | buffer[offset + 1])
                offset += 2
            else:
                size = 65821 + (
                    (buffer[offset] << 16) | (buffer[offset + 1] << 8) | buffer[offset + 2]
                )
                offset += 3
            if offset > end:
                raise InvalidDatabaseError("truncated size")

        if type_num == TYPE_UTF8_STRING or type_num == TYPE_BYTES:
            new_offset = offset + size
            if new_offset > end:
                raise InvalidDatabaseError(f"{_TYPE_NAMES[type_num]} runs past the end of the section")
            payload = self._payload + size
            if payload > MAX_PAYLOAD:
                raise InvalidDatabaseError(f"data entry holds more than {MAX_PAYLOAD} bytes of strings")
            self._payload = payload
            if type_num == TYPE_UTF8_STRING:
                return buffer[offset:new_offset].decode("utf-8"), new_offset
            return bytes(buffer[offset:new_offset]), new_offset

        if type_num == TYPE_MAP or type_num == TYPE_ARRAY:
            depth += 1
            if depth > MAX_DEPTH:
                raise InvalidDatabaseError(f"data nested deeper than {MAX_DEPTH} levels")
            if depth > self._deepest:
                self._deepest = depth
            decode = self._decode
            if type_num == TYPE_ARRAY:
                items = []
                for _ in range(size):
                    item, offset = decode(offset, depth)
                    items.append(item)
                return items, offset
            result: Dict[str, Any] = {}
            for _ in range(size):
                key, offset = decode(offset, depth)
                if type(key) is not str:
                    raise InvalidDatabaseError("map key is not a UTF-8 string")
                result[key], offset = decode(offset, depth)
            return result, offset

        max_size = _UINT_SIZES.get(type_num)
        if max_size is not None or type_num == TYPE_INT32:
            limit = 4 if max_size is None else max_size
            if size > limit:
                raise InvalidDatabaseError(f"{_TYPE_NAMES[type_num]} of {size} bytes")
            new_offset = offset + size
            if new_offset > end:
                raise InvalidDatabaseError(f"{_TYPE_NAMES[type_num]} runs past the end of the section")
            # A signed int32 is negative only when stored in all 4 bytes.
            signed = type_num == TYPE_INT32 and size == 4
            return int.from_bytes(buffer[offset:new_offset], "big", signed=signed), new_offset

        if type_num == TYPE_DOUBLE or type_num == TYPE_FLOAT:
            expected = 8 if type_num == TYPE_DOUBLE else 4
            if size != expected:
                raise InvalidDatabaseError(f"{_TYPE_NAMES[type_num]} of {size} bytes")
            if offset + size > end:
                raise InvalidDatabaseError(f"{_TYPE_NAMES[type_num]} runs past the end of the section")
            unpack = _DOUBLE if type_num == TYPE_DOUBLE else _FLOAT
            return unpack(buffer, offset)[0], offset + size

        if type_num == TYPE_BOOLEAN:
            if size > 1:
                raise InvalidDatabaseError(f"boolean with value {size}")
            return size == 1, offset

        # Types 12 and 13 are deprecated; the spec says readers need not
        # support them, and no data entry may consist of one.
        raise InvalidDatabaseError(f"unexpected {_TYPE_NAMES[type_num]} (type {type_num}) in data")

    def _charge(self, entry: _CacheEntry, depth: int) -> None:
        """Account for reusing a memoised entry as if it had been decoded."""
        values = self._values + entry[1]
        if values > MAX_VALUES:
            raise InvalidDatabaseError(f"data entry decodes to more than {MAX_VALUES} values")
        payload = self._payload + entry[2]
        if payload > MAX_PAYLOAD:
            raise InvalidDatabaseError(f"data entry holds more than {MAX_PAYLOAD} bytes of strings")
        deepest = depth + entry[3]
        if deepest > MAX_DEPTH:
            raise InvalidDatabaseError(f"data nested deeper than {MAX_DEPTH} levels")
        self._values = values
        self._payload = payload
        if deepest > self._deepest:
            self._deepest = deepest

    def _remember(self, offset: int, entry: _CacheEntry) -> None:
        """Cache *entry*, evicting the least recently used entries if needed."""
        if not self._cache_size:
            return
        cache = self._cache
        previous = cache.pop(offset, None)
        if previous is not None:
            self._cache_payload -= previous[2]
        cache[offset] = entry
        self._cache_payload += entry[2]
        while len(cache) > self._cache_size or self._cache_payload > CACHE_MAX_PAYLOAD:
            evicted = cache.popitem(last=False)[1]
            self._cache_payload -= evicted[2]


def _copy(value: Any) -> Any:
    """Return *value* with fresh maps and lists; immutable leaves are shared.

    Plain loops rather than comprehensions: before Python 3.12 each
    comprehension adds a stack frame, and data may nest 512 levels deep.
    """
    if type(value) is dict:
        copied: Dict[str, Any] = {}
        for key, item in value.items():
            copied[key] = _copy(item) if type(item) in _CONTAINERS else item
        return copied
    if type(value) is list:
        items = []
        for item in value:
            items.append(_copy(item) if type(item) in _CONTAINERS else item)
        return items
    return value


def _parse_ip(ip: IPInput) -> IPAddress:
    """Parse an address string (ValueError if invalid) or pass an address through."""
    if isinstance(ip, str):
        if ":" in ip:
            return ipaddress.IPv6Address(ip)
        return ipaddress.IPv4Address(ip)
    if isinstance(ip, (ipaddress.IPv4Address, ipaddress.IPv6Address)):
        return ip
    raise TypeError(f"expected an IP address string or ipaddress object, not {type(ip).__name__}")


def _load(path: str, use_mmap: bool) -> Buffer:
    """Read the file into memory, or map it read-only."""
    with open(path, "rb") as handle:
        if not use_mmap:
            return handle.read()
        if os.fstat(handle.fileno()).st_size == 0:
            raise InvalidDatabaseError(
                f"Error opening database file ({path}). Is this a valid MaxMind DB file?"
            )
        return mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)


def _find_marker(buffer: Buffer, path: str) -> int:
    """Offset of the last metadata marker, searched within the last 128 KiB."""
    start = max(0, len(buffer) - METADATA_MAX_SIZE)
    index = buffer.rfind(METADATA_MARKER, start)
    if index < 0:
        raise InvalidDatabaseError(
            f"Error opening database file ({path}). Is this a valid MaxMind DB file?"
        )
    return index


def _decode_metadata(buffer: Buffer, marker: int, path: str) -> Metadata:
    """Decode the metadata map that follows the marker at *marker*."""
    start = marker + len(METADATA_MARKER)
    decoder = _Decoder(buffer, start, len(buffer), cache_size=0)
    try:
        values = decoder.decode(start)
    except InvalidDatabaseError as exc:
        raise InvalidDatabaseError(f"Error reading metadata in database file ({path}): {exc}") from exc
    if not isinstance(values, dict):
        raise InvalidDatabaseError(f"Error reading metadata in database file ({path}): not a map")
    try:
        return Metadata(values)
    except InvalidDatabaseError as exc:
        raise InvalidDatabaseError(f"Invalid metadata in database file ({path}): {exc}") from exc


def _data_section_start(metadata: Metadata, marker: int, path: str) -> int:
    """Where the data section starts; checks the search tree fits before *marker*."""
    data_start = metadata.search_tree_size + DATA_SECTION_SEPARATOR_SIZE
    if data_start > marker:
        raise InvalidDatabaseError(
            f"Invalid database file ({path}): the search tree ({metadata.node_count} nodes) "
            "does not fit in the file"
        )
    return data_start


def _required_int(values: Mapping[str, Any], key: str) -> int:
    value = values.get(key)
    if type(value) is not int:
        raise InvalidDatabaseError(f"metadata key {key!r} is missing or not an integer")
    return value


def _required_str(values: Mapping[str, Any], key: str) -> str:
    value = values.get(key)
    if not isinstance(value, str):
        raise InvalidDatabaseError(f"metadata key {key!r} is missing or not a string")
    return value


def _string_list(value: Any) -> List[str]:
    if not isinstance(value, (list, tuple)):
        return []
    return [item for item in value if isinstance(item, str)]


def _string_map(value: Any) -> Dict[str, str]:
    if not isinstance(value, Mapping):
        return {}
    return {key: item for key, item in value.items() if isinstance(key, str) and isinstance(item, str)}


def _pure_forced() -> bool:
    value = os.environ.get(PURE_ENV_VAR, "").strip().lower()
    return value not in ("", "0", "false", "no", "off")


def _open_extension(path: str) -> Optional[ExtensionReader]:
    """Open *path* with the maxminddb C extension, or None if it is unavailable."""
    try:
        module = importlib.import_module("maxminddb")
    except ImportError:
        return None
    open_database = getattr(module, "open_database", None)
    mode = getattr(module, "MODE_MMAP_EXT", None)
    invalid_error = getattr(module, "InvalidDatabaseError", None)
    if open_database is None or mode is None or invalid_error is None:
        return None
    try:
        reader = open_database(path, mode)
    except invalid_error as exc:
        raise InvalidDatabaseError(str(exc)) from exc
    except ValueError:
        return None  # a build without the C extension
    if not hasattr(reader, "get_with_prefix_len"):  # maxminddb < 2.0
        reader.close()
        return None
    return ExtensionReader(reader, path, invalid_error)
