"""Test-only writer for MaxMind DB files (format spec 2.0).

Builds small databases for the reader and geo tests. It follows the spec
exactly, so a file it writes reads the same in Tidesight's reader and in the
official ``maxminddb`` package:

- a binary search tree over the given networks, with 24-, 28- or 32-bit
  records; in an IPv6 tree, IPv4 networks go under ``::/96``, and
  ``ipv4_aliases=True`` also points ``::ffff:0:0/96`` at that subtree as
  MaxMind's own databases do;
- 16 zero bytes, then the data section, where repeated values (map keys
  included) are written once and referenced by pointers;
- the metadata marker and the metadata map.

Python values map to types as follows: ``str`` utf8_string, ``float``
double, ``bytes`` bytes, ``bool`` boolean, ``dict`` map, ``list``/``tuple``
array, and ``int`` the smallest unsigned type that fits (uint32, uint64,
uint128) or int32 when negative. Wrap a value in :class:`Uint16`,
:class:`Uint32`, :class:`Uint64`, :class:`Uint128`, :class:`Int32` or
:class:`Float32` to pick its type. :class:`Raw` splices hand-made bytes into
the data section, and :class:`DataOffset` / :class:`RawRecord` put chosen
values into the search tree, to build broken files.
"""

from __future__ import annotations

import ipaddress
import struct
from collections import deque
from typing import Any, Dict, Hashable, Iterable, List, Optional, Sequence, Tuple

METADATA_MARKER = b"\xab\xcd\xefMaxMind.com"
SEPARATOR = bytes(16)

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


class Uint16(int):
    """Written as uint16 (type 5)."""


class Uint32(int):
    """Written as uint32 (type 6)."""


class Uint64(int):
    """Written as uint64 (type 9)."""


class Uint128(int):
    """Written as uint128 (type 10)."""


class Int32(int):
    """Written as int32 (type 8)."""


class Float32(float):
    """Written as a 4-byte float (type 15)."""


class Raw(bytes):
    """Bytes copied verbatim into the data section."""


class DataOffset(int):
    """A search tree record pointing at this data-section offset (no data written)."""


class RawRecord(int):
    """A search tree record with exactly this value (no data written)."""


_UINT_TYPES = {
    Uint16: (TYPE_UINT16, 2),
    Uint32: (TYPE_UINT32, 4),
    Uint64: (TYPE_UINT64, 8),
    Uint128: (TYPE_UINT128, 16),
}


def control(type_num: int, size: int) -> bytes:
    """Control byte(s) for a value of *type_num* whose size field is *size*."""
    if type_num <= 7:
        first, extended = type_num << 5, b""
    else:
        first, extended = 0, bytes([type_num - 7])
    if size < 29:
        return bytes([first | size]) + extended
    if size < 285:
        return bytes([first | 29]) + extended + bytes([size - 29])
    if size < 65821:
        return bytes([first | 30]) + extended + (size - 285).to_bytes(2, "big")
    if size < 65821 + (1 << 24):
        return bytes([first | 31]) + extended + (size - 65821).to_bytes(3, "big")
    raise ValueError(f"size {size} is too large")


def pointer(target: int, size: Optional[int] = None) -> bytes:
    """A pointer to data-section offset *target*; smallest encoding by default."""
    if size is None:
        size = 0 if target < 2048 else 1 if target < 526336 else 2 if target < 134744064 else 3
    if size == 0 and 0 <= target < 2048:
        return bytes([0x20 | (target >> 8), target & 0xFF])
    if size == 1 and 2048 <= target < 526336:
        value = target - 2048
        return bytes([0x28 | (value >> 16)]) + (value & 0xFFFF).to_bytes(2, "big")
    if size == 2 and 526336 <= target < 134744064:
        value = target - 526336
        return bytes([0x30 | (value >> 24)]) + (value & 0xFFFFFF).to_bytes(3, "big")
    if size == 3 and 0 <= target < 1 << 32:
        return bytes([0x38]) + target.to_bytes(4, "big")
    raise ValueError(f"cannot encode pointer {target} with size {size}")


def encode(value: Any) -> bytes:
    """One value encoded inline, without pointers."""
    writer = DataWriter(dedupe=False)
    writer.add(value)
    return bytes(writer.buffer)


class DataWriter:
    """Builds a data section in which identical values are written once."""

    def __init__(self, dedupe: bool = True, pointer_size: Optional[int] = None) -> None:
        self.buffer = bytearray()
        self.dedupe = dedupe
        self.pointer_size = pointer_size
        self._offsets: Dict[Hashable, int] = {}

    def add(self, value: Any) -> int:
        """Store *value*, or find an identical stored one; return its offset."""
        key = _key(value) if self.dedupe else None
        if key is not None and key in self._offsets:
            return self._offsets[key]
        offset = len(self.buffer)
        self._write_inline(value)
        if key is not None:
            self._offsets[key] = offset
        return offset

    def _write(self, value: Any) -> None:
        key = _key(value) if self.dedupe else None
        if key is not None and key in self._offsets:
            self.buffer += pointer(self._offsets[key], self.pointer_size)
            return
        offset = len(self.buffer)
        self._write_inline(value)
        if key is not None:
            self._offsets[key] = offset

    def _write_inline(self, value: Any) -> None:
        kind = type(value)
        if kind is Raw:
            self.buffer += value
        elif kind is dict:
            self.buffer += control(TYPE_MAP, len(value))
            for key, item in value.items():
                if not isinstance(key, str):
                    raise TypeError(f"map keys must be strings, not {key!r}")
                self._write(key)
                self._write(item)
        elif kind in (list, tuple):
            self.buffer += control(TYPE_ARRAY, len(value))
            for item in value:
                self._write(item)
        else:
            type_num, size, payload = _scalar(value)
            self.buffer += control(type_num, size) + payload


def build_database(
    networks: Iterable[Tuple[str, Any]],
    *,
    ip_version: int = 6,
    record_size: int = 28,
    database_type: str = "Tidesight-Test",
    languages: Sequence[str] = ("en",),
    description: Optional[Dict[str, str]] = None,
    build_epoch: int = 1_700_000_000,
    dedupe: bool = True,
    pointer_size: Optional[int] = None,
    data_prefix: Sequence[Any] = (),
    ipv4_aliases: bool = False,
    metadata: Optional[Dict[str, Any]] = None,
    metadata_dedupe: bool = False,
    raw_metadata: Optional[bytes] = None,
) -> bytes:
    """Return the bytes of a database mapping each network to its value.

    Later networks override earlier ones where they overlap. *data_prefix*
    values are written at the start of the data section (to push records to
    large offsets). *metadata* overrides metadata keys (None removes one);
    *raw_metadata* replaces the encoded metadata map altogether.
    """
    if ip_version not in (4, 6):
        raise ValueError("ip_version must be 4 or 6")
    width = 32 if ip_version == 4 else 128
    root = _Node()
    for network, value in networks:
        bits, prefix_len = _network_bits(network, ip_version)
        _insert(root, bits, prefix_len, width, _Leaf(value))
    if ipv4_aliases and ip_version == 6:
        _alias_ipv4(root)
    nodes = _number(root)
    node_count = len(nodes)

    data = DataWriter(dedupe=dedupe, pointer_size=pointer_size)
    for value in data_prefix:
        data.add(value)
    leaf_offsets: Dict[int, int] = {}
    for node in nodes:
        for child in node.children:
            if isinstance(child, _Leaf) and id(child) not in leaf_offsets:
                if not isinstance(child.value, (DataOffset, RawRecord)):
                    leaf_offsets[id(child)] = data.add(child.value)

    index = {id(node): number for number, node in enumerate(nodes)}

    def record(child: Any) -> int:
        if child is None:
            return node_count
        if isinstance(child, _Node):
            return index[id(child)]
        if isinstance(child.value, RawRecord):
            return int(child.value)
        if isinstance(child.value, DataOffset):
            return node_count + 16 + int(child.value)
        return node_count + 16 + leaf_offsets[id(child)]

    tree = b"".join(
        _node_bytes(record(node.children[0]), record(node.children[1]), record_size) for node in nodes
    )
    if raw_metadata is None:
        meta: Dict[str, Any] = {
            "binary_format_major_version": Uint16(2),
            "binary_format_minor_version": Uint16(0),
            "build_epoch": Uint64(build_epoch),
            "database_type": database_type,
            "description": dict(description if description is not None else {"en": "Test database"}),
            "ip_version": Uint16(ip_version),
            "languages": list(languages),
            "node_count": Uint32(node_count),
            "record_size": Uint16(record_size),
        }
        for key, value in (metadata or {}).items():
            if value is None:
                meta.pop(key, None)
            else:
                meta[key] = value
        meta_writer = DataWriter(dedupe=metadata_dedupe)
        meta_writer.add(meta)
        raw_metadata = bytes(meta_writer.buffer)
    return tree + SEPARATOR + bytes(data.buffer) + METADATA_MARKER + raw_metadata


def write_database(path: str, networks: Iterable[Tuple[str, Any]], **options: Any) -> str:
    """Write :func:`build_database` output to *path*; return *path*."""
    content = build_database(networks, **options)
    with open(path, "wb") as handle:
        handle.write(content)
    return path


class _Node:
    __slots__ = ("children",)

    def __init__(self) -> None:
        self.children: List[Any] = [None, None]


class _Leaf:
    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value


def _network_bits(network: str, ip_version: int) -> Tuple[int, int]:
    """The network as an integer of the tree's width, and its prefix length."""
    parsed = ipaddress.ip_network(network)
    if parsed.version == 6 and ip_version == 4:
        raise ValueError(f"{network} does not fit in an IPv4 tree")
    offset = 96 if parsed.version == 4 and ip_version == 6 else 0
    return int(parsed.network_address), parsed.prefixlen + offset


def _insert(root: _Node, bits: int, prefix_len: int, width: int, leaf: Any) -> None:
    """Make the subtree for (*bits*, *prefix_len*) answer *leaf*."""
    if prefix_len == 0:
        root.children = [leaf, leaf]
        return
    node = root
    for depth in range(prefix_len - 1):
        bit = (bits >> (width - 1 - depth)) & 1
        child = node.children[bit]
        if not isinstance(child, _Node):
            replacement = _Node()
            if child is not None:  # split a wider network around the new one
                replacement.children = [child, child]
            node.children[bit] = replacement
            child = replacement
        node = child
    node.children[(bits >> (width - prefix_len)) & 1] = leaf


def _alias_ipv4(root: _Node) -> None:
    """Point ::ffff:0:0/96 at the IPv4 subtree (::/96), as MaxMind does."""
    node: Any = root
    for _ in range(96):
        if not isinstance(node, _Node):
            return
        node = node.children[0]
    if isinstance(node, _Node):
        mapped = int(ipaddress.IPv6Address("::ffff:0:0"))
        _insert(root, mapped, 96, 128, node)


def _number(root: _Node) -> List[_Node]:
    """Nodes in breadth-first order (node 0 is the root); shared nodes once."""
    nodes: List[_Node] = []
    seen = set()
    queue = deque([root])
    while queue:
        node = queue.popleft()
        if id(node) in seen:
            continue
        seen.add(id(node))
        nodes.append(node)
        queue.extend(child for child in node.children if isinstance(child, _Node))
    return nodes


def _node_bytes(left: int, right: int, record_size: int) -> bytes:
    limit = 1 << record_size
    if not (0 <= left < limit and 0 <= right < limit):
        raise ValueError(f"record value too large for {record_size}-bit records")
    if record_size == 24:
        return left.to_bytes(3, "big") + right.to_bytes(3, "big")
    if record_size == 28:
        middle = ((left >> 24) << 4) | (right >> 24)
        return (left & 0xFFFFFF).to_bytes(3, "big") + bytes([middle]) + (right & 0xFFFFFF).to_bytes(3, "big")
    if record_size == 32:
        return left.to_bytes(4, "big") + right.to_bytes(4, "big")
    raise ValueError(f"unsupported record size {record_size}")


def _scalar(value: Any) -> Tuple[int, int, bytes]:
    """(type number, size field, payload) for a non-container value."""
    kind = type(value)
    if kind is bool:
        return TYPE_BOOLEAN, int(value), b""
    if kind is str:
        data = value.encode("utf-8")
        return TYPE_UTF8_STRING, len(data), data
    if kind is bytes:
        return TYPE_BYTES, len(value), value
    if kind is float:
        return TYPE_DOUBLE, 8, struct.pack(">d", value)
    if kind is Float32:
        return TYPE_FLOAT, 4, struct.pack(">f", value)
    if kind is Int32 or (kind is int and value < 0):
        data = _int32(int(value))
        return TYPE_INT32, len(data), data
    if kind in _UINT_TYPES:
        type_num, max_size = _UINT_TYPES[kind]
        data = _uint(int(value), max_size)
        return type_num, len(data), data
    if kind is int:
        for type_num, max_size in ((TYPE_UINT32, 4), (TYPE_UINT64, 8), (TYPE_UINT128, 16)):
            if value < 1 << (8 * max_size):
                return type_num, (value.bit_length() + 7) // 8, _uint(value, max_size)
        raise ValueError(f"{value} does not fit in 128 bits")
    raise TypeError(f"cannot encode {kind.__name__} values")


def _uint(value: int, max_size: int) -> bytes:
    if not 0 <= value < 1 << (8 * max_size):
        raise ValueError(f"{value} does not fit in {max_size} unsigned bytes")
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def _int32(value: int) -> bytes:
    """Negative values use all 4 bytes (two's complement); others are minimal."""
    if not -(1 << 31) <= value < 1 << 31:
        raise ValueError(f"{value} does not fit in an int32")
    if value < 0:
        return (value & 0xFFFFFFFF).to_bytes(4, "big")
    return value.to_bytes((value.bit_length() + 7) // 8, "big")


def _key(value: Any) -> Optional[Hashable]:
    """A hashable identity of *value* and its types (None: never share it)."""
    kind = type(value)
    if kind is Raw:
        return None
    if kind is dict:
        items = []
        for key, item in value.items():
            item_key = _key(item)
            if item_key is None:
                return None
            items.append((key, item_key))
        return ("map", tuple(items))
    if kind in (list, tuple):
        elements = tuple(_key(item) for item in value)
        return None if any(element is None for element in elements) else ("array", elements)
    if kind is float:
        return ("double", struct.pack(">d", value))
    if kind is Float32:
        return ("float", struct.pack(">f", value))
    return (kind.__name__, value)
