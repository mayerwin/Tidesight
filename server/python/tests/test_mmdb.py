"""Tests for the pure-Python MaxMind DB reader (tidesight.enrich.mmdb).

Fixture databases come from tests/mmdb_writer.py. When the official
``maxminddb`` package is installed, extra tests check that it reads the same
files identically, which validates the writer as well as the reader.
"""

from __future__ import annotations

import importlib
import ipaddress
import math
import os
import random
import sys
import types
from typing import Any, Dict, List, Tuple

import pytest

from mmdb_writer import (
    METADATA_MARKER,
    TYPE_ARRAY,
    TYPE_BOOLEAN,
    TYPE_BYTES,
    TYPE_DATA_CACHE_CONTAINER,
    TYPE_DOUBLE,
    TYPE_END_MARKER,
    TYPE_FLOAT,
    TYPE_INT32,
    TYPE_MAP,
    TYPE_UINT16,
    TYPE_UINT32,
    TYPE_UINT64,
    TYPE_UINT128,
    TYPE_UTF8_STRING,
    DataOffset,
    Float32,
    Int32,
    Raw,
    RawRecord,
    Uint16,
    Uint32,
    Uint64,
    Uint128,
    build_database,
    control,
    encode,
    pointer,
    write_database,
)
from tidesight.enrich import mmdb

ALL_TYPES: Dict[str, Any] = {
    "utf8_string": "unicode! ☯ - ♫",
    "empty_string": "",
    "double": 42.123456,
    "negative_double": -1.5e-300,
    "infinity": math.inf,
    "bytes": b"\x00\x00\x00*",
    "empty_bytes": b"",
    "uint16": Uint16(65535),
    "uint16_zero": Uint16(0),
    "uint32": Uint32(4294967295),
    "uint32_small": Uint32(268435456),
    "int32_min": Int32(-2147483648),
    "int32_max": Int32(2147483647),
    "int32_minus_one": Int32(-1),
    "int32_one_byte": Int32(255),
    "int32_three_bytes": Int32(0x800000),
    "int32_zero": Int32(0),
    "uint64": Uint64(2**64 - 1),
    "uint64_mid": Uint64(1152921504606846976),
    "uint128": Uint128(2**128 - 1),
    "uint128_mid": Uint128(1329227995784915872903807060280344576),
    "uint128_zero": Uint128(0),
    "map": {"mapX": {"arrayX": [7, 8, 9], "utf8_stringX": "hello"}},
    "empty_map": {},
    "array": [1, 2, 3],
    "empty_array": [],
    "nested_arrays": [[], [[]], [1, [2, [3]]]],
    "boolean_true": True,
    "boolean_false": False,
    "float": Float32(1.5),
    "float_negative": Float32(-0.25),
    "auto_uint64": 2**40,
    "auto_uint128": 2**100,
    "auto_int32": -5,
}


def make_reader(tmp_path, networks, name: str = "test.mmdb", **options: Any) -> mmdb.Reader:
    reader_options = {key: options.pop(key) for key in ("use_mmap", "cache_size") if key in options}
    path = write_database(str(tmp_path / name), networks, **options)
    return mmdb.Reader(path, **reader_options)


def write_raw(tmp_path, content: bytes, name: str = "raw.mmdb") -> str:
    path = tmp_path / name
    path.write_bytes(content)
    return str(path)


def data_section(content: bytes, reader: mmdb.Reader) -> bytes:
    start = reader.metadata.search_tree_size + 16
    return content[start : content.rindex(METADATA_MARKER)]


# --- data types -----------------------------------------------------------


@pytest.mark.parametrize("dedupe", [True, False])
def test_every_data_type_round_trips(tmp_path, dedupe):
    with make_reader(tmp_path, [("1.2.3.0/24", ALL_TYPES)], dedupe=dedupe) as reader:
        record = reader.get("1.2.3.4")
    assert record == ALL_TYPES
    assert type(record["uint16"]) is int and type(record["uint128"]) is int
    assert type(record["bytes"]) is bytes
    assert type(record["float"]) is float and record["float"] == 1.5
    assert record["boolean_true"] is True and record["boolean_false"] is False
    assert record["int32_three_bytes"] == 0x800000  # shorter than 4 bytes: positive


def test_nan_and_negative_zero(tmp_path):
    value = {"nan": math.nan, "neg_zero": -0.0, "float_nan": Float32(math.nan)}
    with make_reader(tmp_path, [("1.0.0.0/8", value)]) as reader:
        record = reader.get("1.1.1.1")
    assert math.isnan(record["nan"]) and math.isnan(record["float_nan"])
    assert math.copysign(1.0, record["neg_zero"]) == -1.0


@pytest.mark.parametrize(
    "length", [0, 1, 28, 29, 30, 284, 285, 286, 65820, 65821, 65822, 70000]
)
def test_size_encodings(tmp_path, length):
    text = "x" * length
    blob = bytes(range(256)) * (length // 256) + bytes(length % 256)
    value = {"text": text, "blob": blob}
    with make_reader(tmp_path, [("1.0.0.0/8", value)]) as reader:
        assert reader.get("1.0.0.1") == value


def test_large_maps_and_arrays(tmp_path):
    # Containers of 65,821+ elements would exceed the 65,536-value limit.
    value = {
        "map": {f"key{index}": index for index in range(300)},
        "array": list(range(60000)),
    }
    with make_reader(tmp_path, [("1.0.0.0/8", value)]) as reader:
        assert reader.get("1.0.0.1") == value


def test_hand_encoded_values_from_the_spec(tmp_path):
    # Bytes written by hand, independent of the writer's encoder.
    value = Raw(
        b"\xe4"  # map, 4 pairs
        b"\x43abc" b"\x43def"  # "abc": "def" (string, size 3)
        b"\x41n" b"\x02\x02\x01\x00"  # "n": uint64 (extended type 9) 0x0100
        b"\x41i" b"\x04\x01\xff\xff\xff\xfe"  # "i": int32 -2
        b"\x41a" b"\x02\x04" b"\x01\x07" b"\x00\x07"  # "a": array (type 11) [true, false] (type 14)
    )
    with make_reader(tmp_path, [("1.0.0.0/8", value)], dedupe=False) as reader:
        assert reader.get("1.0.0.1") == {"abc": "def", "n": 256, "i": -2, "a": [True, False]}


# --- pointers -------------------------------------------------------------


def test_deduplicated_values_use_pointers(tmp_path):
    shared = {"names": {"en": "Shared", "fr": "Partage"}, "code": "XX"}
    networks = [(f"{n}.0.0.0/8", {"shared": shared, "n": n}) for n in range(1, 20)]
    with_pointers = build_database(networks)
    without = build_database(networks, dedupe=False)
    assert len(with_pointers) < len(without)
    path = write_raw(tmp_path, with_pointers)
    with mmdb.Reader(path) as reader:
        for n in range(1, 20):
            assert reader.get(f"{n}.1.2.3") == {"shared": shared, "n": n}


@pytest.mark.parametrize(
    ("padding", "pointer_class"),
    [(0, 0x20), (3000, 0x28), (600_000, 0x30)],
    ids=["size0", "size1", "size2"],
)
def test_pointer_sizes_zero_to_two(tmp_path, padding, pointer_class):
    shared = "shared-value"
    networks = [("1.0.0.0/8", {"a": shared}), ("2.0.0.0/8", {"b": shared})]
    content = build_database(networks, data_prefix=[b"\x00" * padding] if padding else [])
    path = write_raw(tmp_path, content)
    with mmdb.Reader(path) as reader:
        assert reader.get("1.1.1.1") == {"a": shared}
        assert reader.get("2.2.2.2") == {"b": shared}
        data = data_section(content, reader)
    target = data.index(encode(shared))
    encoded = pointer(target)
    assert encoded[0] & 0xF8 == pointer_class
    assert encoded in data


def test_pointer_size_three(tmp_path):
    value = {"names": {"en": "x"}, "other": {"names": {"en": "x"}}, "list": ["x", "x"]}
    content = build_database([("1.0.0.0/8", value), ("2.0.0.0/8", value)], pointer_size=3)
    assert bytes([0x38]) in content
    path = write_raw(tmp_path, content)
    with mmdb.Reader(path) as reader:
        assert reader.get("1.2.3.4") == value
        assert reader.get("2.3.4.5") == value


@pytest.mark.parametrize(
    ("encoded", "target"),
    [
        (b"\x27\xff", 2047),  # size 0: 11 bits
        (b"\x2f\xff\xff", 2048 + 524287),  # size 1: 19 bits + 2048
        (b"\x30\x00\x00\x00", 526336),  # size 2: 27 bits + 526336
        (b"\x37\xff\xff\xff", 526336 + 134217727),
        (b"\x3f\xff\xff\xff\xff", 0xFFFFFFFF),  # size 3: 32 bits, no offset
        (b"\x38\x00\x00\x00\x10", 16),
    ],
)
def test_pointer_arithmetic(encoded, target):
    # A tiny section whose pointer goes past the end reports the target.
    decoder = mmdb._Decoder(encoded, 0, len(encoded), 0)
    with pytest.raises(mmdb.InvalidDatabaseError, match=f"pointer to offset {target} "):
        decoder.decode(0)


def test_pointer_to_pointer_is_invalid(tmp_path):
    # The record sits at data offset 0 and is a pointer to offset 0.
    with make_reader(tmp_path, [("1.0.0.0/8", Raw(pointer(0)))]) as reader:
        with pytest.raises(mmdb.InvalidDatabaseError, match="another pointer"):
            reader.get("1.1.1.1")


def test_top_level_pointer_is_followed(tmp_path):
    target = encode("value")
    networks = [("1.0.0.0/8", DataOffset(len(target)))]
    content = build_database(networks, data_prefix=[Raw(target + pointer(0))])
    with mmdb.Reader(write_raw(tmp_path, content)) as reader:
        assert reader.get("1.2.3.4") == "value"


# --- search tree ----------------------------------------------------------


NETWORKS_V4 = [
    ("1.2.3.0/24", {"name": "a"}),
    ("1.2.4.0/22", {"name": "b"}),
    ("128.0.0.0/1", {"name": "upper half"}),
    ("10.0.0.1/32", {"name": "host"}),
]
NETWORKS_V6 = NETWORKS_V4 + [
    ("2a01:cb00::/32", {"name": "v6 /32"}),
    ("2a01:cb00:1234::/48", {"name": "v6 /48"}),
    ("2c0f:f000::1/128", {"name": "v6 host"}),
]
EXPECTED_V4: List[Tuple[str, Any, int]] = [
    ("1.2.3.4", {"name": "a"}, 24),
    ("1.2.3.0", {"name": "a"}, 24),
    ("1.2.3.255", {"name": "a"}, 24),
    ("1.2.4.0", {"name": "b"}, 22),
    ("1.2.7.255", {"name": "b"}, 22),
    ("1.2.8.0", None, 21),
    ("1.2.2.255", None, 24),
    ("128.0.0.0", {"name": "upper half"}, 1),
    ("255.255.255.255", {"name": "upper half"}, 1),
    ("10.0.0.1", {"name": "host"}, 32),
    ("10.0.0.2", None, 31),
    ("64.0.0.1", None, 2),
]
EXPECTED_V6: List[Tuple[str, Any, int]] = [
    ("2a01:cb00::1", {"name": "v6 /32"}, 36),  # the /48 inside splits the /32
    ("2a01:cb00:8000::", {"name": "v6 /32"}, 33),
    ("2a01:cb00:1234:5678::1", {"name": "v6 /48"}, 48),
    ("2a01:cb01::", None, 32),
    ("2c0f:f000::1", {"name": "v6 host"}, 128),
    ("2c0f:f000::2", None, 127),
    ("::1.2.3.4", {"name": "a"}, 120),  # the IPv4 subtree, reached as IPv6
    ("ffff::", None, 1),
]


@pytest.mark.parametrize("record_size", [24, 28, 32])
@pytest.mark.parametrize("use_mmap", [False, True])
def test_ipv4_tree(tmp_path, record_size, use_mmap):
    with make_reader(
        tmp_path, NETWORKS_V4, ip_version=4, record_size=record_size, use_mmap=use_mmap
    ) as reader:
        assert reader.metadata.ip_version == 4
        for ip, record, prefix_len in EXPECTED_V4:
            assert reader.get_with_prefix_len(ip) == (record, prefix_len), ip
            assert reader.get(ip) == record
        with pytest.raises(ValueError, match="IPv6 address in an IPv4-only database"):
            reader.get("2a01:cb00::1")


@pytest.mark.parametrize("record_size", [24, 28, 32])
@pytest.mark.parametrize("use_mmap", [False, True])
def test_ipv6_tree(tmp_path, record_size, use_mmap):
    with make_reader(
        tmp_path, NETWORKS_V6, ip_version=6, record_size=record_size, use_mmap=use_mmap
    ) as reader:
        # IPv4 lookups start at the ::/96 node and report IPv4 prefix lengths.
        for ip, record, prefix_len in EXPECTED_V4 + EXPECTED_V6:
            assert reader.get_with_prefix_len(ip) == (record, prefix_len), ip


@pytest.mark.parametrize("record_size", [28, 32])
def test_record_values_above_24_bits(tmp_path, record_size):
    # A 16 MiB value pushes every record past 2**24, so the 28-bit high
    # nibbles and the 4th byte of 32-bit records matter.
    networks = [("1.0.0.0/8", {"name": "one"}), ("2.0.0.0/8", {"name": "two"})]
    content = build_database(
        networks, record_size=record_size, data_prefix=[bytes(16_800_000)], dedupe=False
    )
    with mmdb.Reader(write_raw(tmp_path, content)) as reader:
        assert reader.get("1.2.3.4") == {"name": "one"}
        assert reader.get("2.3.4.5") == {"name": "two"}
        assert reader.get("3.4.5.6") is None
    with pytest.raises(ValueError, match="too large"):
        build_database(networks, record_size=24, data_prefix=[bytes(16_800_000)], dedupe=False)


def test_ipv4_mapped_addresses_follow_aliases(tmp_path):
    with make_reader(tmp_path, NETWORKS_V4, ipv4_aliases=True) as reader:
        assert reader.get_with_prefix_len("::ffff:1.2.3.4") == ({"name": "a"}, 120)
        assert reader.get_with_prefix_len("1.2.3.4") == ({"name": "a"}, 24)
    with make_reader(tmp_path, NETWORKS_V4, name="plain.mmdb") as reader:
        assert reader.get("::ffff:1.2.3.4") is None


def test_ipv6_tree_without_ipv4_subtree(tmp_path):
    with make_reader(tmp_path, [("2a01:cb00::/32", {"x": 1})]) as reader:
        assert reader.get_with_prefix_len("1.2.3.4") == (None, 0)
        assert reader.get("2a01:cb00::1") == {"x": 1}


def test_whole_address_space_record(tmp_path):
    with make_reader(tmp_path, [("::/0", {"all": True})]) as reader:
        assert reader.get_with_prefix_len("1.2.3.4") == ({"all": True}, 0)
        assert reader.get_with_prefix_len("2a01::1") == ({"all": True}, 1)


def test_empty_database(tmp_path):
    with make_reader(tmp_path, []) as reader:
        assert reader.metadata.node_count == 1
        assert reader.get_with_prefix_len("1.2.3.4") == (None, 0)
        assert reader.get_with_prefix_len("2a01::1") == (None, 1)


def test_later_networks_override_earlier_ones(tmp_path):
    networks = [("1.0.0.0/8", {"v": "wide"}), ("1.2.0.0/16", {"v": "narrow"})]
    with make_reader(tmp_path, networks) as reader:
        assert reader.get_with_prefix_len("1.2.3.4") == ({"v": "narrow"}, 16)
        assert reader.get_with_prefix_len("1.3.0.0") == ({"v": "wide"}, 16)
        assert reader.get_with_prefix_len("1.0.0.0") == ({"v": "wide"}, 15)


@pytest.mark.parametrize(
    "ip", ["", "abc", "1.2.3", "1.2.3.4.5", "256.0.0.1", "1.2.3.4/24", " 1.2.3.4", "::g", "1::2::3"]
)
def test_invalid_address_strings_raise_value_error(tmp_path, ip):
    with make_reader(tmp_path, NETWORKS_V4) as reader:
        with pytest.raises(ValueError):
            reader.get(ip)


@pytest.mark.parametrize("ip", [123, None, b"1.2.3.4", 1.5])
def test_non_string_addresses_raise_type_error(tmp_path, ip):
    with make_reader(tmp_path, NETWORKS_V4) as reader:
        with pytest.raises(TypeError):
            reader.get(ip)


def test_ipaddress_objects_are_accepted(tmp_path):
    with make_reader(tmp_path, NETWORKS_V6) as reader:
        assert reader.get(ipaddress.ip_address("1.2.3.4")) == {"name": "a"}
        assert reader.get(ipaddress.ip_address("2a01:cb00::1")) == {"name": "v6 /32"}


# --- metadata -------------------------------------------------------------


def test_metadata(tmp_path):
    with make_reader(
        tmp_path,
        NETWORKS_V6,
        record_size=32,
        database_type="GeoLite2-City",
        languages=["en", "fr"],
        description={"en": "A test", "fr": "Un test"},
        build_epoch=1790818572,
    ) as reader:
        metadata = reader.metadata
    assert metadata.database_type == "GeoLite2-City"
    assert metadata.ip_version == 6
    assert metadata.record_size == 32
    assert metadata.node_byte_size == 8
    assert metadata.languages == ["en", "fr"]
    assert metadata.description == {"en": "A test", "fr": "Un test"}
    assert metadata.binary_format_major_version == 2
    assert metadata.binary_format_minor_version == 0
    assert metadata.build_epoch == 1790818572
    assert metadata.search_tree_size == metadata.node_count * 8
    assert metadata.as_dict()["node_count"] == metadata.node_count
    assert "GeoLite2-City" in repr(metadata)


def test_optional_metadata_keys(tmp_path):
    with make_reader(tmp_path, NETWORKS_V4, metadata={"languages": None, "description": None}) as reader:
        assert reader.metadata.languages == []
        assert reader.metadata.description == {}


def test_read_metadata_matches_reader(tmp_path):
    path = write_database(str(tmp_path / "a.mmdb"), NETWORKS_V6, build_epoch=123)
    with mmdb.Reader(path) as reader:
        assert mmdb.read_metadata(path).as_dict() == reader.metadata.as_dict()


def test_marker_inside_the_data_section(tmp_path):
    # The metadata starts at the *last* marker in the file.
    with make_reader(tmp_path, [("1.0.0.0/8", {"blob": METADATA_MARKER * 3})]) as reader:
        assert reader.get("1.1.1.1") == {"blob": METADATA_MARKER * 3}


# --- corrupt and truncated files -------------------------------------------


def corrupt_cases() -> List[Tuple[str, bytes]]:
    valid = build_database(NETWORKS_V6)
    marker = valid.rindex(METADATA_MARKER)
    return [
        ("empty", b""),
        ("garbage", os.urandom(4096)),
        ("first half", valid[: len(valid) // 2]),
        ("metadata cut", valid[:-3]),
        ("tree cut", valid[:20] + valid[marker:]),
        ("marker only", METADATA_MARKER),
        ("metadata not a map", build_database(NETWORKS_V4, raw_metadata=encode("not a map"))),
        ("no node_count", build_database(NETWORKS_V4, metadata={"node_count": None})),
        ("no build_epoch", build_database(NETWORKS_V4, metadata={"build_epoch": None})),
        ("record size 20", build_database(NETWORKS_V4, metadata={"record_size": Uint16(20)})),
        ("ip version 5", build_database(NETWORKS_V4, metadata={"ip_version": Uint16(5)})),
        ("format 3", build_database(NETWORKS_V4, metadata={"binary_format_major_version": Uint16(3)})),
        ("node_count too big", build_database(NETWORKS_V4, metadata={"node_count": Uint32(10**6)})),
        ("marker too far", valid + bytes(129 * 1024)),
        ("bad metadata utf-8", valid[: marker + 14] + b"\xe1" + b"\x5d\x00" + b"\xff" * 30),
    ]


@pytest.mark.parametrize(("name", "content"), corrupt_cases(), ids=[c[0] for c in corrupt_cases()])
@pytest.mark.parametrize("use_mmap", [False, True])
def test_corrupt_files_raise_invalid_database_error(tmp_path, name, content, use_mmap):
    path = write_raw(tmp_path, content)
    with pytest.raises(mmdb.InvalidDatabaseError):
        mmdb.Reader(path, use_mmap=use_mmap)


@pytest.mark.parametrize(("name", "content"), corrupt_cases(), ids=[c[0] for c in corrupt_cases()])
def test_read_metadata_rejects_corrupt_files(tmp_path, name, content):
    with pytest.raises(mmdb.InvalidDatabaseError):
        mmdb.read_metadata(write_raw(tmp_path, content))


def test_missing_file_raises_os_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        mmdb.Reader(str(tmp_path / "missing.mmdb"))


@pytest.mark.parametrize(
    ("value", "message"),
    [
        (Raw(control(TYPE_UTF8_STRING, 2) + b"\xff\xfe"), "corrupt data"),
        (Raw(control(TYPE_DOUBLE, 4) + b"\x00" * 4), "double of 4 bytes"),
        (Raw(control(TYPE_FLOAT, 8) + b"\x00" * 8), "float of 8 bytes"),
        (Raw(control(TYPE_BOOLEAN, 2)), "boolean"),
        (Raw(control(TYPE_UINT16, 3) + b"\x00" * 3), "uint16 of 3 bytes"),
        (Raw(control(TYPE_UINT32, 5) + b"\x00" * 5), "uint32 of 5 bytes"),
        (Raw(control(TYPE_INT32, 5) + b"\x00" * 5), "int32 of 5 bytes"),
        (Raw(control(TYPE_UINT64, 9) + b"\x00" * 9), "uint64 of 9 bytes"),
        (Raw(control(TYPE_UINT128, 17) + b"\x00" * 17), "uint128 of 17 bytes"),
        (Raw(b"\x00\x00"), "extended type number 7"),
        (Raw(b"\x00\x09"), "extended type number 16"),
        (Raw(control(TYPE_DATA_CACHE_CONTAINER, 0)), "data cache container"),
        (Raw(control(TYPE_END_MARKER, 0)), "end marker"),
        (Raw(control(TYPE_MAP, 1) + encode(Uint16(1)) + encode("x")), "map key"),
        (Raw(control(TYPE_MAP, 1) + encode("a") + pointer(0)), "nested deeper"),  # a cycle
        (Raw(control(TYPE_UTF8_STRING, 100) + b"abc"), "past the end"),
        (Raw(control(TYPE_BYTES, 4)), "past the end"),
        (Raw(control(TYPE_ARRAY, 3) + encode(1)), "past the end"),
        (Raw(bytes([0x40 | 30])), "truncated size"),  # size field 30, no size bytes
        (Raw(b"\x29"), "truncated pointer"),
    ],
)
def test_corrupt_data_raises_invalid_database_error(tmp_path, value, message):
    # Each value is the only data in the file, at data offset 0.
    with make_reader(tmp_path, [("1.0.0.0/8", value)], dedupe=False) as reader:
        with pytest.raises(mmdb.InvalidDatabaseError, match=message):
            reader.get("1.2.3.4")
        assert reader.get("2.0.0.0") is None  # the rest of the file still works


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (DataOffset(10**6), "search tree is corrupt"),  # past the data section
        (RawRecord(0), "Invalid node in search tree"),  # a node below a /32
    ],
)
def test_corrupt_search_tree(tmp_path, record, message):
    networks = [("1.0.0.0/8", {"ok": True}), ("2.2.2.2/32", record)]
    with make_reader(tmp_path, networks, ip_version=4) as reader:
        assert reader.get("1.1.1.1") == {"ok": True}
        with pytest.raises(mmdb.InvalidDatabaseError, match=message):
            reader.get("2.2.2.2")


def test_record_pointing_into_the_separator(tmp_path):
    with make_reader(tmp_path, [("1.0.0.0/8", DataOffset(-5))], ip_version=4) as reader:
        with pytest.raises(mmdb.InvalidDatabaseError, match="search tree is corrupt"):
            reader.get("1.1.1.1")


# --- resource limits ------------------------------------------------------


def nested_arrays(depth: int) -> Raw:
    return Raw(control(TYPE_ARRAY, 1) * depth + encode(True))


def unwrap(value: Any) -> Tuple[Any, int]:
    levels = 0
    while isinstance(value, list):
        value, levels = value[0], levels + 1
    return value, levels


def test_depth_limit_for_nested_containers(tmp_path):
    with make_reader(tmp_path, [("1.0.0.0/8", nested_arrays(512))], dedupe=False) as reader:
        assert unwrap(reader.get("1.1.1.1")) == (True, 512)
    with make_reader(tmp_path, [("1.0.0.0/8", nested_arrays(513))], name="deep.mmdb") as reader:
        with pytest.raises(mmdb.InvalidDatabaseError, match="deeper than 512"):
            reader.get("1.1.1.1")


def pointer_chain(links: int, extra_array: bool) -> Raw:
    """Arrays each holding a pointer to the next: two depth levels per link."""
    link_size = len(control(TYPE_ARRAY, 1)) + len(pointer(0, size=3))
    content = b""
    for link in range(links):
        content += control(TYPE_ARRAY, 1) + pointer(link_size * (link + 1), size=3)
    tail = encode(True)
    return Raw(content + (control(TYPE_ARRAY, 1) + tail if extra_array else tail))


def test_depth_limit_counts_pointers(tmp_path):
    with make_reader(tmp_path, [("1.0.0.0/8", pointer_chain(256, False))], dedupe=False) as reader:
        assert unwrap(reader.get("1.1.1.1")) == (True, 256)
    with make_reader(
        tmp_path, [("1.0.0.0/8", pointer_chain(256, True))], name="deep.mmdb", dedupe=False
    ) as reader:
        with pytest.raises(mmdb.InvalidDatabaseError, match="deeper than 512"):
            reader.get("1.1.1.1")


@pytest.mark.parametrize("dedupe", [True, False])
def test_value_count_limit(tmp_path, dedupe):
    # The array itself plus its elements: 65,536 values is the limit.
    with make_reader(tmp_path, [("1.0.0.0/8", [True] * 65535)], dedupe=dedupe) as reader:
        assert len(reader.get("1.1.1.1")) == 65535
    with make_reader(tmp_path, [("1.0.0.0/8", [True] * 65536)], name="over.mmdb", dedupe=dedupe) as reader:
        with pytest.raises(mmdb.InvalidDatabaseError, match="more than 65536 values"):
            reader.get("1.1.1.1")


def test_value_count_includes_map_keys(tmp_path):
    accepted = {f"k{index}": True for index in range(32767)}  # 1 + 2 * 32767 = 65535
    rejected = {f"k{index}": True for index in range(32768)}  # 65537
    with make_reader(tmp_path, [("1.0.0.0/8", accepted)]) as reader:
        assert len(reader.get("1.1.1.1")) == 32767
    with make_reader(tmp_path, [("1.0.0.0/8", rejected)], name="over.mmdb") as reader:
        with pytest.raises(mmdb.InvalidDatabaseError, match="values"):
            reader.get("1.1.1.1")


def test_payload_limit(tmp_path):
    with make_reader(tmp_path, [("1.0.0.0/8", "a" * (2 * 1024 * 1024))]) as reader:
        assert len(reader.get("1.1.1.1")) == 2 * 1024 * 1024
    with make_reader(tmp_path, [("1.0.0.0/8", b"a" * (2 * 1024 * 1024 + 1))], name="b.mmdb") as reader:
        with pytest.raises(mmdb.InvalidDatabaseError, match="bytes of strings"):
            reader.get("1.1.1.1")


def test_payload_limit_counts_every_pointer_occurrence(tmp_path):
    shared = "s" * 65536
    with make_reader(tmp_path, [("1.0.0.0/8", [shared] * 32)]) as reader:
        record = reader.get("1.1.1.1")
        assert len(record) == 32 and record[31] == shared
    with make_reader(tmp_path, [("1.0.0.0/8", [shared] * 33)], name="over.mmdb") as reader:
        with pytest.raises(mmdb.InvalidDatabaseError, match="bytes of strings"):
            reader.get("1.1.1.1")


def test_pointer_fan_out_is_rejected_quickly(tmp_path):
    # Level k is an array of two pointers to level k - 1: 2**40 leaves.
    levels = [encode("leaf")]
    offsets = [0]
    for _ in range(40):
        offsets.append(offsets[-1] + len(levels[-1]))
        target = offsets[-2]
        levels.append(control(TYPE_ARRAY, 2) + pointer(target, size=3) * 2)
    content = build_database([("1.0.0.0/8", DataOffset(offsets[-1]))], data_prefix=[Raw(b"".join(levels))])
    with mmdb.Reader(write_raw(tmp_path, content)) as reader:
        with pytest.raises(mmdb.InvalidDatabaseError, match="values"):
            reader.get("1.1.1.1")


def test_metadata_is_bounded_too(tmp_path):
    padding = "p" * 65536
    content = build_database(NETWORKS_V4, metadata={"padding": [padding] * 40}, metadata_dedupe=True)
    with pytest.raises(mmdb.InvalidDatabaseError, match="bytes of strings"):
        mmdb.Reader(write_raw(tmp_path, content))


# --- caching and lifecycle ---------------------------------------------------


def test_returned_records_are_independent_copies(tmp_path):
    shared = {"names": {"en": "Shared"}}
    networks = [("1.0.0.0/8", {"country": shared, "list": [1, [2]]}), ("2.0.0.0/8", {"country": shared})]
    with make_reader(tmp_path, networks) as reader:
        first = reader.get("1.1.1.1")
        first["country"]["names"]["en"] = "changed"
        first["list"][1].append(3)
        assert reader.get("1.1.1.1") == {"country": shared, "list": [1, [2]]}
        assert reader.get("2.2.2.2") == {"country": {"names": {"en": "Shared"}}}


def test_cache_is_bounded(tmp_path):
    networks = [(f"{n}.0.0.0/8", {"n": n, "names": {"en": "x"}}) for n in range(1, 100)]
    with make_reader(tmp_path, networks, cache_size=8) as reader:
        for _ in range(2):
            for n in range(1, 100):
                assert reader.get(f"{n}.1.1.1")["n"] == n
        assert 0 < len(reader._decoder._cache) <= 8


def test_cache_can_be_disabled(tmp_path):
    with make_reader(tmp_path, NETWORKS_V4, cache_size=0) as reader:
        assert reader.get("1.2.3.4") == {"name": "a"}
        assert reader.get("1.2.3.4") == {"name": "a"}
        assert len(reader._decoder._cache) == 0
    with pytest.raises(ValueError):
        mmdb.Reader(write_database(str(tmp_path / "x.mmdb"), NETWORKS_V4), cache_size=-1)


@pytest.mark.parametrize("use_mmap", [False, True])
def test_close(tmp_path, use_mmap):
    reader = make_reader(tmp_path, NETWORKS_V4, use_mmap=use_mmap)
    assert not reader.closed and "open" in repr(reader)
    reader.close()
    reader.close()  # idempotent
    assert reader.closed and "closed" in repr(reader)
    with pytest.raises(ValueError, match="closed"):
        reader.get("1.2.3.4")


def test_mmap_reader_releases_the_file(tmp_path):
    path = write_database(str(tmp_path / "a.mmdb"), NETWORKS_V4)
    with mmdb.Reader(path, use_mmap=True) as reader:
        assert reader.get("1.2.3.4") == {"name": "a"}
    os.replace(write_database(str(tmp_path / "b.mmdb"), NETWORKS_V6), path)  # fails on Windows if still mapped


# --- open_reader ------------------------------------------------------------


class FakeExtensionReader:
    def __init__(self, path: str) -> None:
        self.inner = mmdb.Reader(path)
        self.closed = False

    def metadata(self) -> Any:
        return types.SimpleNamespace(**self.inner.metadata.as_dict())

    def get(self, ip: str) -> Any:
        if ip == "6.6.6.6":
            raise FakeInvalidDatabaseError("broken")
        return self.inner.get(ip)

    def get_with_prefix_len(self, ip: str) -> Any:
        if ip == "6.6.6.6":
            raise FakeInvalidDatabaseError("broken")
        return self.inner.get_with_prefix_len(ip)

    def close(self) -> None:
        self.closed = True
        self.inner.close()


class FakeInvalidDatabaseError(RuntimeError):
    pass


def fake_maxminddb(has_extension: bool = True) -> types.ModuleType:
    module = types.ModuleType("maxminddb")
    module.MODE_MMAP_EXT = 1
    module.InvalidDatabaseError = FakeInvalidDatabaseError
    opened: List[FakeExtensionReader] = []

    def open_database(path: str, mode: int = 0) -> FakeExtensionReader:
        assert mode == 1
        if not has_extension:
            raise ValueError("MODE_MMAP_EXT requires the maxminddb.extension module to be available")
        if not os.path.getsize(path):
            raise FakeInvalidDatabaseError("Error opening database file")
        opened.append(FakeExtensionReader(path))
        return opened[-1]

    module.open_database = open_database
    module.opened = opened
    return module


def test_open_reader_without_maxminddb_is_pure(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "maxminddb", None)  # import fails
    monkeypatch.delenv(mmdb.PURE_ENV_VAR, raising=False)
    path = write_database(str(tmp_path / "a.mmdb"), NETWORKS_V4)
    with mmdb.open_reader(path) as reader:
        assert isinstance(reader, mmdb.Reader)


def test_open_reader_prefers_the_extension(tmp_path, monkeypatch):
    module = fake_maxminddb()
    monkeypatch.setitem(sys.modules, "maxminddb", module)
    monkeypatch.delenv(mmdb.PURE_ENV_VAR, raising=False)
    path = write_database(str(tmp_path / "a.mmdb"), NETWORKS_V6, build_epoch=99)
    reader = mmdb.open_reader(path)
    assert isinstance(reader, mmdb.ExtensionReader)
    assert isinstance(reader.metadata, mmdb.Metadata)
    assert reader.metadata.build_epoch == 99
    assert reader.get("1.2.3.4") == {"name": "a"}
    assert reader.get_with_prefix_len("2a01:cb00::1") == ({"name": "v6 /32"}, 36)
    with pytest.raises(mmdb.InvalidDatabaseError, match="broken"):
        reader.get("6.6.6.6")
    with pytest.raises(mmdb.InvalidDatabaseError):
        reader.get_with_prefix_len("6.6.6.6")
    reader.close()
    assert module.opened[0].closed
    with pytest.raises(mmdb.InvalidDatabaseError):
        mmdb.open_reader(write_raw(tmp_path, b"", name="empty.mmdb"))


@pytest.mark.parametrize(("value", "pure"), [("1", True), ("yes", True), ("0", False), ("", False)])
def test_open_reader_environment_switch(tmp_path, monkeypatch, value, pure):
    monkeypatch.setitem(sys.modules, "maxminddb", fake_maxminddb())
    monkeypatch.setenv(mmdb.PURE_ENV_VAR, value)
    with mmdb.open_reader(write_database(str(tmp_path / "a.mmdb"), NETWORKS_V4)) as reader:
        assert isinstance(reader, mmdb.Reader) is pure


def test_open_reader_without_the_c_extension_is_pure(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "maxminddb", fake_maxminddb(has_extension=False))
    monkeypatch.delenv(mmdb.PURE_ENV_VAR, raising=False)
    with mmdb.open_reader(write_database(str(tmp_path / "a.mmdb"), NETWORKS_V4)) as reader:
        assert isinstance(reader, mmdb.Reader)


# --- agreement with the official maxminddb package (when installed) -----------


def random_addresses(count: int, seed: int) -> List[str]:
    rng = random.Random(seed)
    addresses = []
    for _ in range(count):
        if rng.random() < 0.5:
            addresses.append(str(ipaddress.IPv4Address(rng.getrandbits(32))))
        else:
            addresses.append(str(ipaddress.IPv6Address(rng.getrandbits(128))))
    return addresses


def random_networks(values: Dict[str, Any]) -> List[Tuple[str, Any]]:
    rng = random.Random(7)
    networks = []
    for index in range(300):
        prefix = rng.randint(8, 30)
        network = ipaddress.ip_network((rng.getrandbits(32) >> (32 - prefix) << (32 - prefix), prefix))
        networks.append((str(network), {"i": index, "names": {"en": f"n{index % 17}"}, "v": values}))
    for index in range(200):
        prefix = rng.randint(16, 64)
        value = rng.getrandbits(128) >> (128 - prefix) << (128 - prefix)
        network = ipaddress.ip_network((value, prefix))
        networks.append((str(network), {"i": 1000 + index, "loc": [rng.random(), rng.random()]}))
    return networks


def addresses_for(networks: List[Tuple[str, Any]], seed: int) -> List[str]:
    """Random addresses plus the first and last address of every network."""
    addresses = random_addresses(400, seed)
    for network, _ in networks:
        parsed = ipaddress.ip_network(network)
        addresses += [str(parsed.network_address), str(parsed.broadcast_address)]
    return addresses


def fixture_databases(tmp_path, networks: List[Tuple[str, Any]]) -> List[str]:
    paths = []
    v4_networks = [network for network in networks if ":" not in network[0]]
    for record_size in (24, 28, 32):
        for aliases in (False, True):
            path = str(tmp_path / f"db-{record_size}-{aliases}.mmdb")
            paths.append(write_database(path, networks, record_size=record_size, ipv4_aliases=aliases))
        path = str(tmp_path / f"v4-{record_size}.mmdb")
        paths.append(write_database(path, v4_networks, ip_version=4, record_size=record_size))
    return paths


def has_c_extension() -> bool:
    try:
        importlib.import_module("maxminddb.extension")
    except ImportError:
        return False
    return True


def assert_same_as_official(maxminddb: Any, mode: int, paths: List[str], addresses: List[str]) -> None:
    for path in paths:
        with mmdb.Reader(path) as ours:
            theirs = maxminddb.open_database(path, mode)
            try:
                metadata = theirs.metadata()
                expected = {name: getattr(metadata, name) for name in ours.metadata.as_dict()}
                assert ours.metadata.as_dict() == expected
                for ip in addresses:
                    if ":" in ip and ours.metadata.ip_version == 4:
                        continue
                    assert ours.get_with_prefix_len(ip) == theirs.get_with_prefix_len(ip), (path, ip)
            finally:
                theirs.close()


def test_agrees_with_official_pure_python_reader(tmp_path):
    maxminddb = pytest.importorskip("maxminddb")
    networks = random_networks(ALL_TYPES)
    paths = fixture_databases(tmp_path, networks)
    assert_same_as_official(maxminddb, maxminddb.MODE_MEMORY, paths, addresses_for(networks, 1))


def test_agrees_with_official_c_extension(tmp_path):
    maxminddb = pytest.importorskip("maxminddb")
    if not has_c_extension():
        pytest.skip("maxminddb C extension not available")
    # The extension returns uint32 values >= 2**31 as negative numbers on
    # Windows, where a C long has 32 bits; that one value is left out.
    networks = random_networks(dict(ALL_TYPES, uint32=Uint32(2**31 - 1)))
    paths = fixture_databases(tmp_path, networks)
    assert_same_as_official(maxminddb, maxminddb.MODE_MMAP_EXT, paths, addresses_for(networks, 2))


def test_open_reader_uses_the_real_extension_when_installed(tmp_path, monkeypatch):
    pytest.importorskip("maxminddb")
    if not has_c_extension():
        pytest.skip("maxminddb C extension not available")
    monkeypatch.delenv(mmdb.PURE_ENV_VAR, raising=False)
    path = write_database(str(tmp_path / "a.mmdb"), NETWORKS_V6)
    with mmdb.open_reader(path) as fast, mmdb.Reader(path) as pure:
        assert isinstance(fast, mmdb.ExtensionReader)
        assert fast.metadata.as_dict() == pure.metadata.as_dict()
        for ip, _, _ in EXPECTED_V4 + EXPECTED_V6:
            assert fast.get_with_prefix_len(ip) == pure.get_with_prefix_len(ip)
