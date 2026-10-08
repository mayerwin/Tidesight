"""Tests for tidesight._toml, the standard-library TOML 1.0.0 fallback parser.

The parser must return exactly what tomllib/tomli return for every valid
document and raise TOMLDecodeError for every invalid one. The comparative
tests use tomli as the reference (or tomllib on Python 3.11+ when tomli is
not installed) and are skipped when neither is available; the other tests
check explicit expected values and need no reference.
"""

from __future__ import annotations

import datetime as dt
import importlib
import importlib.util
import io
import math
import pickle
import sys
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any, List

import pytest

from tidesight import _toml

EXAMPLE_CONFIG = Path(__file__).resolve().parents[3] / "config" / "tidesight.example.toml"


@pytest.fixture(scope="module")
def reference() -> ModuleType:
    """The reference implementation: tomli, or tomllib on Python 3.11+."""
    if sys.version_info >= (3, 11) and importlib.util.find_spec("tomli") is None:
        return importlib.import_module("tomllib")
    return pytest.importorskip("tomli")


def assert_identical(actual: Any, expected: Any, path: str = "document") -> None:
    """Assert equality with exact types, key order, NaN and -0.0, and UTC offsets.

    Plain ``==`` is not enough: it treats 1, 1.0 and True as equal, NaN as
    different from itself, and aware datetimes in different offsets as equal.
    """
    assert type(actual) is type(expected), f"{path}: {actual!r} != {expected!r}"
    if isinstance(expected, dict):
        assert list(actual) == list(expected), f"{path}: keys {list(actual)} != {list(expected)}"
        for key, value in expected.items():
            assert_identical(actual[key], value, f"{path}[{key!r}]")
    elif isinstance(expected, list):
        assert len(actual) == len(expected), f"{path}: {actual!r} != {expected!r}"
        for index, (item, expected_item) in enumerate(zip(actual, expected)):
            assert_identical(item, expected_item, f"{path}[{index}]")
    elif isinstance(expected, float):
        same_value = (math.isnan(actual) and math.isnan(expected)) or actual == expected
        same_sign = math.copysign(1.0, actual) == math.copysign(1.0, expected)
        assert same_value and same_sign, f"{path}: {actual!r} != {expected!r}"
    elif isinstance(expected, (dt.datetime, dt.time)):
        assert actual == expected, f"{path}: {actual!r} != {expected!r}"
        assert actual.utcoffset() == expected.utcoffset(), f"{path}: {actual!r} != {expected!r}"
    else:
        assert actual == expected, f"{path}: {actual!r} != {expected!r}"


# --- Valid documents ----------------------------------------------------------

TOML_README_EXAMPLE = """\
# This is a TOML document

title = "TOML Example"

[owner]
name = "Tom Preston-Werner"
dob = 1979-05-27T07:32:00-08:00

[database]
enabled = true
ports = [ 8000, 8001, 8002 ]
data = [ ["delta", "phi"], [3.14] ]
temp_targets = { cpu = 79.5, case = 72.0 }

[servers]

[servers.alpha]
ip = "10.0.0.1"
role = "frontend"

[servers.beta]
ip = "10.0.0.2"
role = "backend"
"""

VALID_DOCUMENTS = [
    pytest.param("", id="empty"),
    pytest.param("  \t\n# only a comment\n\n   # and another\t\n", id="comments-only"),
    pytest.param("a = 1", id="no-trailing-newline"),
    pytest.param(
        '# full-line comment\nkey = "value"  # trailing comment\n'
        'other = "# not a comment" #\n#\nlast = 1#no space\n',
        id="comments",
    ),
    pytest.param(
        "a = 1 # caf\u00e9 \u2603 \U0001f600 \u0085\ttab\n", id="comment-unicode-and-tab"
    ),
    pytest.param("a=1\nb   =   2\nc\t=\t3\n\t d = 4\n", id="whitespace-around-equals"),
    pytest.param(
        "key = 1\nbare_key = 2\nbare-key = 3\n1234 = 4\n-_- = 5\nA_Z-09 = 6\n", id="bare-keys"
    ),
    pytest.param(
        "true = 1\nfalse = 2\ninf = 3\nnan = 4\n2001-02-03 = 5\n0x1F = 6\n", id="keyword-keys"
    ),
    pytest.param(
        r'''"127.0.0.1" = "value"
"character encoding" = "value"
"ʎǝʞ" = "value"
'key2' = "value"
'quoted "value"' = "value"
"" = "empty key"
'with \backslash' = 1
"\u0041\t\"" = 2
''',
        id="quoted-keys",
    ),
    pytest.param(
        'name = "Orange"\nphysical.color = "orange"\nphysical.shape = "round"\n'
        'site."google.com" = true\n',
        id="dotted-keys",
    ),
    pytest.param(
        'fruit.name = "banana"\nfruit. color = "yellow"\nfruit . flavor = "banana"\n'
        "a .\tb\t. c = 1\n'x'.\"y\".z = 2\n",
        id="dotted-keys-whitespace",
    ),
    pytest.param("3.14159 = 'pi'\n", id="float-like-dotted-key"),
    pytest.param(
        'apple.type = "fruit"\norange.type = "fruit"\napple.skin = "thin"\n'
        'orange.skin = "thick"\napple.color = "red"\norange.color = "orange"\n',
        id="dotted-keys-out-of-order",
    ),
    pytest.param("[a.b.c]\nx = 1\n[a]\nb.d = 2\n", id="dotted-key-into-implicit-table"),
    pytest.param('"" = 1\n[a]\n"" = 2\n["b".""]\nc = 3\n', id="empty-keys"),
    pytest.param(
        r'''plain = "I'm a string."
escapes = "\b\t\n\f\r\"\\"
unicode4 = "\u00e9\u00C9\u20AC"
unicode8 = "\U0001F600\U00000041\U0010FFFF"
nul = "\u0000"
del = "\u007F"
path = "C:\\Users\\nodejs\\templates"
non_ascii = "café ☃ 😀"
hash = "# not a comment"
empty = ""
'''
        + 'tab = "a\tb"\nc1_control = "a\u0085b"\n',
        id="basic-strings",
    ),
    pytest.param(
        r'''str1 = """
Roses are red
Violets are blue"""
str2 = """only the first newline is trimmed

"""
str3 = """\
       The quick brown \
       fox jumps over \
       the lazy dog.\
       """
str4 = """Here are two quotation marks: "". Simple enough."""
str5 = """Here are three quotation marks: ""\"."""
str6 = """Here are fifteen quotation marks: ""\"""\"""\"""\"""\"."""
str7 = """"This," she said, "is just a pointless statement.""""
str8 = """ends with two quotes"""""
str9 = """escapes \t \u00e9 \U0001F600 \\ \" still work"""
str10 = """a backslash before blank lines \


   joins"""
str11 = """\
"""
str12 = """"""
''',
        id="multiline-basic-strings",
    ),
    pytest.param(
        'a = """trailing whitespace after \\  \t \n   the backslash"""\n'
        'b = """\\\t\n\n  x"""\n'
        'c = """tab\tand \\\\ \\"quotes\\" """\n',
        id="line-ending-backslash-with-whitespace",
    ),
    pytest.param(
        r"""winpath = 'C:\Users\nodejs\templates'
winpath2 = '\\ServerX\admin$\system32\'
quoted = 'Tom "Dubs" Preston-Werner'
regex = '<\i\c*\s*>'
empty = ''
tab = '	'
""",
        id="literal-strings",
    ),
    pytest.param(
        r"""regex2 = '''I [dw]on't need \d{2} apples'''
lines = '''
The first newline is
trimmed in raw strings.
   All other whitespace
   is preserved.
'''
apos15 = "Here are fifteen apostrophes: '''''''''''''''"
str = ''''That,' she said, 'is still pointless.''''
inner = '''two '' apostrophes'''
end2 = '''ends with two'''''
empty = ''''''
"""
        + "quot15 = '''Here are fifteen quotation marks: " + '"' * 15 + "'''\n",
        id="multiline-literal-strings",
    ),
    pytest.param(
        "int1 = +99\nint2 = 42\nint3 = 0\nint4 = -17\nint5 = 1_000\nint6 = 5_349_221\n"
        "int7 = 53_49_221\nint8 = 1_2_3_4_5\nzero_pos = +0\nzero_neg = -0\n"
        "hex1 = 0xDEADBEEF\nhex2 = 0xdeadbeef\nhex3 = 0xdead_beef\nhex4 = 0x0\nhex5 = 0x00_0F\n"
        "oct1 = 0o01234567\noct2 = 0o755\nbin1 = 0b11010110\nbin2 = 0b1_0\nbin3 = 0b0\n"
        "max = 9223372036854775807\nmin = -9223372036854775808\n"
        "big = 123456789012345678901234567890\n",
        id="integers",
    ),
    pytest.param(
        "flt1 = +1.0\nflt2 = 3.1415\nflt3 = -0.01\nflt4 = 5e+22\nflt5 = 1e06\nflt6 = -2E-2\n"
        "flt7 = 6.626e-34\nflt8 = 224_617.445_991_228\nflt9 = 1e1_0\nflt10 = 3.0e-0_1\n"
        "zero = 0.0\npos_zero = +0.0\nneg_zero = -0.0\nexp_zero = 0e0\nneg_exp_zero = -0E-0\n"
        "fraction_zeros = 0.000_1\nexponent_zeros = 1e007\nhuge = 1e1000\ntiny = 1e-1000\n",
        id="floats",
    ),
    pytest.param(
        "a = inf\nb = +inf\nc = -inf\nd = nan\ne = +nan\nf = -nan\narr = [inf, -inf, nan]\n",
        id="special-floats",
    ),
    pytest.param("t = true\nf = false\narr = [true,false]\n", id="booleans"),
    pytest.param(
        "odt1 = 1979-05-27T07:32:00Z\nodt2 = 1979-05-27T00:32:00-07:00\n"
        "odt3 = 1979-05-27T00:32:00.999999-07:00\nodt4 = 1979-05-27 07:32:00Z\n"
        "odt5 = 1979-05-27t07:32:00z\nodt6 = 2021-01-01T00:00:00+05:45\n"
        "odt7 = 2021-01-01T00:00:00-00:00\nodt8 = 2021-01-01T00:00:00+00:00\n"
        "odt9 = 1979-05-27T07:32:00.5+23:59\nodt10 = 2000-02-29T12:00:00-12:30\n",
        id="offset-date-times",
    ),
    pytest.param(
        "ldt1 = 1979-05-27T07:32:00\nldt2 = 1979-05-27T00:32:00.999999\n"
        "ldt3 = 1979-05-27 07:32:00\nldt4 = 2000-02-29t23:59:59.1\n",
        id="local-date-times",
    ),
    pytest.param(
        "a = 1979-05-27T00:32:00.1Z\nb = 1979-05-27T00:32:00.123456789Z\n"
        "c = 1979-05-27T00:32:00.9999999\nd = 00:00:00.0000001\ne = 12:34:56.000100\n",
        id="fractional-seconds",
    ),
    pytest.param(
        "ld1 = 1979-05-27\nld2 = 2000-02-29\nld3 = 0001-01-01\nld4 = 9999-12-31\n"
        "lt1 = 07:32:00\nlt2 = 00:32:00.999999\nlt3 = 23:59:59\n"
        "date_then_comment = 1979-05-27 # a comment\n",
        id="local-dates-and-times",
    ),
    pytest.param(
        'integers = [ 1, 2, 3 ]\ncolors = [ "red", "yellow", "green" ]\n'
        "nested_arrays_of_ints = [ [ 1, 2 ], [3, 4, 5] ]\n"
        'nested_mixed_array = [ [ 1, 2 ], ["a", "b", "c"] ]\n'
        "string_array = [ \"all\", 'strings', \"\"\"are the same\"\"\", '''type''' ]\n"
        "numbers = [ 0.1, 0.2, 0.5, 1, 2, 5 ]\n"
        'contributors = [\n  "Foo Bar <foo@example.com>",\n'
        '  { name = "Baz Qux", email = "bazqux@example.com",'
        ' url = "https://example.com/bazqux" }\n]\n'
        'tricky = [ "]", "[", ",", "#" ]\n'
        "empty = []\nempty_ws = [ ]\nnested_empty = [[], [[]]]\n",
        id="arrays",
    ),
    pytest.param(
        "integers2 = [\n  1, 2, 3\n]\nintegers3 = [\n  1,\n  2, # this is ok\n]\n"
        "comments = [ # opening\n  # alone\n  1 # after a value\n  , # after a comma\n  2\n"
        "  # before the end\n]\ntrailing = [1,2,]\nwhitespace = [  \t1\t,\n\n2  ]\n"
        "empty_multiline = [\n\n]\n",
        id="multiline-arrays",
    ),
    pytest.param(
        'mixed = [1, 1.5, "s", true, 1979-05-27, 07:32:00, [1], {a = 1}, 1979-05-27T07:32:00Z]\n',
        id="mixed-type-array",
    ),
    pytest.param(
        'name = { first = "Tom", last = "Preston-Werner" }\npoint = { x = 1, y = 2 }\n'
        'animal = { type.name = "pug" }\nempty = {}\nempty_ws = {  }\n'
        "nested = { a = { b = { c = 1 } }, d = [ { e = 2 } ] }\n"
        "dotted = { a.b = 1, a.c = 2, \"q\".'r' = 3, a . d . e = 4 }\n"
        'multiline_values = { s = """\nx\ny""", arr = [\n 1, # a comment in the array\n 2,\n] }\n'
        "no_space={a=1,b=2}\n",
        id="inline-tables",
    ),
    pytest.param(
        '[table]\n\n[table-1]\nkey1 = "some string"\nkey2 = 123\n\n'
        '[table-2]\nkey1 = "another string"\nkey2 = 456\n\n'
        '[dog."tater.man"]\ntype.name = "pug"\n\n'
        "[a.b.c]\n[ d.e.f ]\n[ g .  h  . i ]\n[ j . \"ʞ\" . 'l' ]\n"
        "\t[indented]\n  x = 1\n[with-comment] # comment\n[true]\n[1.2]\n",
        id="tables",
    ),
    pytest.param(
        "[x.y.z.w]\na = 1\n[x]\nb = 2\n[x.y]\nc = 3\n", id="super-tables-after-sub-tables"
    ),
    pytest.param(
        '[fruit]\napple.color = "red"\napple.taste.sweet = true\n\n'
        "[fruit.apple.texture]\nsmooth = true\n\n[[fruit.apple.seeds]]\nsize = 1\n",
        id="sub-tables-of-dotted-key-tables",
    ),
    pytest.param("a.b.c = 1\n[a.b.d]\ne = 2\n[a.x]\n", id="sub-tables-of-root-dotted-keys"),
    pytest.param(
        '[[products]]\nname = "Hammer"\nsku = 738594937\n\n'
        "[[products]]  # empty table within the array\n\n"
        '[[products]]\nname = "Nail"\nsku = 284758393\n\ncolor = "gray"\n',
        id="arrays-of-tables",
    ),
    pytest.param(
        '[[fruits]]\nname = "apple"\n\n[fruits.physical]  # subtable\ncolor = "red"\n'
        'shape = "round"\n\n[[fruits.varieties]]  # nested array of tables\n'
        'name = "red delicious"\n\n[[fruits.varieties]]\nname = "granny smith"\n\n\n'
        '[[fruits]]\nname = "banana"\n\n[[fruits.varieties]]\nname = "plantain"\n',
        id="nested-arrays-of-tables",
    ),
    pytest.param(
        "[[a.b]]\nx = 1\n[[a.b]]\nx = 2\n[a]\ny = 3\n", id="super-table-of-array-of-tables"
    ),
    pytest.param(
        "[[a]]\nb.c = 1\nd = [1]\n[[a]]\nb.c = 2\nd = [2]\n[a.e]\nf = 3\n",
        id="array-of-tables-element-keys",
    ),
    pytest.param("[[a]]\n[a.b]\nx = 1\n[[a]]\n[a.b]\nx = 2\n", id="array-of-tables-sub-tables"),
    pytest.param("[[ a ]]\n[[ a]]\n[[a ]]\n[[ 'a' ]]\n", id="array-of-tables-header-spacing"),
    pytest.param("z = 1\na = 2\n[m]\nq = 3\nb = 4\n[c]\n[m2.z]\n[m2.a]\n", id="key-order"),
    pytest.param(
        'a = 1\r\nb = """\r\nline one\r\nline two\\\r\n   continued"""\r\n'
        "c = '''\r\nx\r\ny'''\r\n# comment\r\n[t]\r\nd = [\r\n  1,\r\n  2,\r\n]\r\n",
        id="crlf-line-endings",
    ),
    pytest.param(
        '"€" = "euro"\n"😀" = { "é" = "\\u00e9" }\n[ "ü" . \'ñ\' ]\nk = "日本語"\n',
        id="unicode",
    ),
    pytest.param(TOML_README_EXAMPLE, id="toml-readme-example"),
]


# --- Invalid documents (rejected by TOML 1.0.0 and 1.1.0 alike) ----------------

INVALID_DOCUMENTS = [
    # Statements and keys
    pytest.param("a = ", id="missing-value"),
    pytest.param("a = # comment", id="missing-value-before-comment"),
    pytest.param("= 1", id="missing-key"),
    pytest.param("a 1", id="missing-equals"),
    pytest.param("a", id="key-without-value"),
    pytest.param("a = 1 b = 2", id="two-pairs-on-one-line"),
    pytest.param('a = "x" b = 1', id="pair-after-string"),
    pytest.param("a b = 1", id="space-in-bare-key"),
    pytest.param("é = 1", id="non-ascii-bare-key"),
    pytest.param("a/b = 1", id="invalid-bare-key-character"),
    pytest.param("a..b = 1", id="empty-dotted-key-part"),
    pytest.param("a. = 1", id="trailing-dot-in-key"),
    pytest.param(".a = 1", id="leading-dot-in-key"),
    pytest.param('"""a""" = 1', id="multiline-string-key"),
    pytest.param('"a\nb" = 1', id="newline-in-quoted-key"),
    pytest.param("\ufeffa = 1", id="byte-order-mark"),
    # Strings
    pytest.param('a = "abc', id="unterminated-basic-string"),
    pytest.param("a = 'abc", id="unterminated-literal-string"),
    pytest.param('a = """abc', id="unterminated-multiline-basic-string"),
    pytest.param("a = '''abc", id="unterminated-multiline-literal-string"),
    pytest.param('a = "ab\ncd"', id="newline-in-basic-string"),
    pytest.param("a = 'ab\ncd'", id="newline-in-literal-string"),
    pytest.param(r'a = "\q"', id="unknown-escape"),
    pytest.param(r'a = "\ "', id="escaped-space"),
    pytest.param(r'a = "\u12"', id="short-unicode-escape"),
    pytest.param(r'a = "\u12G4"', id="non-hex-unicode-escape"),
    pytest.param(r'a = "\u+123"', id="signed-unicode-escape"),
    pytest.param(r'a = "\uD800"', id="surrogate-escape"),
    pytest.param(r'a = "\U00110000"', id="out-of-range-escape"),
    pytest.param('a = """x\\  y"""', id="backslash-whitespace-not-ending-line"),
    pytest.param('a = """x""""""', id="six-closing-quotes"),
    pytest.param("a = '''x''''''", id="six-closing-apostrophes"),
    pytest.param('a = "\x00"', id="null-in-basic-string"),
    pytest.param("a = '\x7f'", id="delete-in-literal-string"),
    pytest.param('a = """\x0b"""', id="vertical-tab-in-multiline-string"),
    pytest.param("a = '''x\ry'''", id="lone-cr-in-multiline-string"),
    pytest.param("a = 1\r", id="lone-cr-at-end-of-line"),
    pytest.param("# bell \x07\na = 1", id="control-character-in-comment"),
    pytest.param("a = 1 # \x7f", id="delete-in-comment"),
    pytest.param("# x\ry\n", id="lone-cr-in-comment"),
    # Integers
    pytest.param("a = 01", id="leading-zero"),
    pytest.param("a = +01", id="signed-leading-zero"),
    pytest.param("a = 1__000", id="double-underscore"),
    pytest.param("a = _1", id="leading-underscore"),
    pytest.param("a = 1_", id="trailing-underscore"),
    pytest.param("a = 0x_1", id="underscore-after-prefix"),
    pytest.param("a = 0x", id="empty-hex"),
    pytest.param("a = 0X1F", id="uppercase-prefix"),
    pytest.param("a = +0x1F", id="signed-hex"),
    pytest.param("a = -0o7", id="negative-octal"),
    pytest.param("a = 0b102", id="bad-binary-digit"),
    pytest.param("a = 0o8", id="bad-octal-digit"),
    pytest.param("a = --1", id="double-sign"),
    # Floats
    pytest.param("a = 1.", id="trailing-dot"),
    pytest.param("a = .5", id="leading-dot"),
    pytest.param("a = 1.e5", id="dot-before-exponent"),
    pytest.param("a = 1e", id="empty-exponent"),
    pytest.param("a = 1._0", id="underscore-after-dot"),
    pytest.param("a = 3.14.15", id="two-dots"),
    pytest.param("a = 03.14", id="float-leading-zero"),
    pytest.param("a = Inf", id="capitalised-inf"),
    pytest.param("a = NaN", id="capitalised-nan"),
    pytest.param("a = infinity", id="infinity-word"),
    # Booleans
    pytest.param("a = True", id="capitalised-true"),
    pytest.param("a = fals", id="truncated-false"),
    # Dates and times
    pytest.param("a = 1979-02-29", id="not-a-leap-year"),
    pytest.param("a = 1979-13-01", id="month-13"),
    pytest.param("a = 1979-05-32", id="day-32"),
    pytest.param("a = 0000-01-01", id="year-zero"),
    pytest.param("a = 1979-05-27T24:00:00", id="hour-24"),
    pytest.param("a = 07:32:60", id="second-60"),
    pytest.param("a = 1979-05-27T07:32:00+24:00", id="offset-hour-24"),
    pytest.param("a = 1979-05-27T07:32:00+0700", id="offset-without-colon"),
    pytest.param("a = 07:32:00.", id="dot-without-fraction"),
    pytest.param("a = 1979-05-27T", id="separator-without-time"),
    pytest.param("a = 1979-5-27", id="single-digit-month"),
    pytest.param("a = 1979-05-27T07:32:00Zx", id="text-after-date-time"),
    pytest.param("a = 07:32:00Z", id="offset-on-local-time"),
    # Arrays
    pytest.param("a = [1, 2", id="unclosed-array"),
    pytest.param("a = [1 2]", id="array-missing-comma"),
    pytest.param("a = [,]", id="array-only-comma"),
    pytest.param("a = [1,,2]", id="array-double-comma"),
    pytest.param("a = [,1]", id="array-leading-comma"),
    # Inline tables
    pytest.param("a = {b = 1", id="unclosed-inline-table"),
    pytest.param("a = {b = 1 c = 2}", id="inline-table-missing-comma"),
    pytest.param("a = {b = 1,, c = 2}", id="inline-table-double-comma"),
    pytest.param("a = {,}", id="inline-table-only-comma"),
    pytest.param("a = {b = 1, b = 2}", id="inline-table-duplicate-key"),
    pytest.param("a = {b.c = 1, b.c = 2}", id="inline-table-duplicate-dotted-key"),
    pytest.param("a = {b = {c = 1}, b.d = 2}", id="inline-table-extends-nested-inline-table"),
    pytest.param("a = {b = 1, b.c = 2}", id="inline-table-dotted-key-through-value"),
    pytest.param("a = {b = 1}\na.c = 2", id="dotted-key-extends-inline-table"),
    pytest.param("a = {}\n[a]", id="header-redefines-inline-table"),
    pytest.param("a = {b = {}}\n[a.b.c]", id="header-inside-inline-table"),
    pytest.param("a = {b = []}\n[[a.b]]", id="array-of-tables-inside-inline-table"),
    # Redefinitions
    pytest.param("a = 1\na = 2", id="duplicate-key"),
    pytest.param('a = 1\n"a" = 2', id="duplicate-quoted-key"),
    pytest.param('a = 1\n"\\u0061" = 2', id="duplicate-escaped-key"),
    pytest.param("a = 1\na.b = 2", id="dotted-key-through-value"),
    pytest.param("a.b = 1\na = 2", id="value-over-dotted-key-table"),
    pytest.param("a.b = 1\na.b.c = 2", id="dotted-key-through-nested-value"),
    pytest.param("[a]\n[a]", id="duplicate-table"),
    pytest.param('[a]\n["a"]', id="duplicate-table-quoted"),
    pytest.param("[a.b]\n[a]\n[a]", id="duplicate-super-table"),
    pytest.param("[a]\nb = 1\n[a.b]", id="table-over-value"),
    pytest.param("a = 1\n[a.b]", id="table-through-value"),
    pytest.param(
        '[fruit]\napple.color = "red"\n[fruit.apple]', id="header-redefines-dotted-table"
    ),
    pytest.param(
        "[fruit]\napple.taste.sweet = true\n[fruit.apple.taste]",
        id="header-redefines-nested-dotted-table",
    ),
    pytest.param("a.b = 1\n[a]", id="header-redefines-root-dotted-table"),
    pytest.param("[a.b]\nx = 1\n[a]\nb.y = 2", id="dotted-key-extends-defined-table"),
    pytest.param("[a.b.c]\nz = 9\n[a]\nb.c.t = 1", id="dotted-key-extends-nested-defined-table"),
    pytest.param("[a]\n[[a]]", id="array-of-tables-over-table"),
    pytest.param("[[a]]\n[a]", id="table-over-array-of-tables"),
    pytest.param("a = []\n[[a]]", id="array-of-tables-over-static-array"),
    pytest.param("a = [1]\n[a]", id="table-over-static-array"),
    pytest.param("a = [{b = 1}]\n[a.c]", id="sub-table-of-static-array"),
    pytest.param("[[a.b]]\n[[a]]", id="array-of-tables-over-implicit-table"),
    pytest.param("[[a]]\nb = []\n[[a.b]]", id="array-of-tables-over-element-array"),
    pytest.param("[[a.b]]\n[a]\nb.c = 1", id="dotted-key-into-array-of-tables"),
    pytest.param(
        "[[fruit]]\nname = 'apple'\n[[fruit.variety]]\nname = 'red delicious'\n"
        "[fruit.variety]\nname = 'granny smith'",
        id="table-over-nested-array-of-tables",
    ),
    # Headers
    pytest.param("[]", id="empty-table-header"),
    pytest.param("[[]]", id="empty-array-of-tables-header"),
    pytest.param("[a", id="unclosed-table-header"),
    pytest.param("[[a]", id="unclosed-array-of-tables-header"),
    pytest.param("[[a] ]", id="split-closing-brackets"),
    pytest.param("[ [a]]", id="split-opening-brackets"),
    pytest.param("[a]]", id="extra-closing-bracket"),
    pytest.param("[a.]", id="trailing-dot-in-header"),
    pytest.param("[a] b = 1", id="pair-after-header"),
    pytest.param("[a\n]", id="newline-in-header"),
    pytest.param("[a = 1]", id="value-in-header"),
]

# Valid in TOML 1.1 (and in tomli 2.4+), but not in TOML 1.0.0, which this
# parser implements like tomllib up to Python 3.14.
TOML_1_1_ONLY = [
    pytest.param("a = {b = 1,}", id="inline-table-trailing-comma"),
    pytest.param("a = {\n}", id="inline-table-newline"),
    pytest.param("a = {b = 1,\nc = 2}", id="inline-table-multiline"),
    pytest.param("a = {b = 1, # comment\nc = 2}", id="inline-table-comment"),
    pytest.param(r'a = "\e"', id="escape-e"),
    pytest.param(r'a = "\x41"', id="escape-x"),
    pytest.param("a = 07:32", id="local-time-without-seconds"),
    pytest.param("a = 1979-05-27T07:32Z", id="date-time-without-seconds"),
    pytest.param("a = 1979-05-27 07:32", id="local-date-time-without-seconds"),
]


@pytest.mark.parametrize("doc", VALID_DOCUMENTS)
def test_valid_document_parses(doc: str) -> None:
    assert isinstance(_toml.loads(doc), dict)


@pytest.mark.parametrize("doc", VALID_DOCUMENTS)
def test_valid_document_matches_reference(doc: str, reference: ModuleType) -> None:
    assert_identical(_toml.loads(doc), reference.loads(doc))


@pytest.mark.parametrize("doc", INVALID_DOCUMENTS)
def test_invalid_document_is_rejected(doc: str) -> None:
    with pytest.raises(_toml.TOMLDecodeError):
        _toml.loads(doc)


@pytest.mark.parametrize("doc", INVALID_DOCUMENTS)
def test_invalid_document_is_rejected_by_reference(doc: str, reference: ModuleType) -> None:
    with pytest.raises(reference.TOMLDecodeError):
        reference.loads(doc)


@pytest.mark.parametrize("doc", TOML_1_1_ONLY)
def test_toml_1_1_syntax_is_rejected(doc: str) -> None:
    with pytest.raises(_toml.TOMLDecodeError):
        _toml.loads(doc)


def test_corpus_sizes() -> None:
    assert len(VALID_DOCUMENTS) >= 40
    assert len(INVALID_DOCUMENTS) >= 40


# --- The project's example configuration ---------------------------------------


def test_example_config_parses() -> None:
    with EXAMPLE_CONFIG.open("rb") as fp:
        config = _toml.load(fp)
    assert isinstance(config, dict) and config


def test_example_config_matches_reference(reference: ModuleType) -> None:
    with EXAMPLE_CONFIG.open("rb") as fp:
        ours = _toml.load(fp)
    with EXAMPLE_CONFIG.open("rb") as fp:
        theirs = reference.load(fp)
    assert_identical(ours, theirs)


# --- Explicit expected values (no reference needed) ---------------------------


def test_strings_and_numbers() -> None:
    doc = _toml.loads(
        r'''
basic = "tab\there \"quoted\" \u00e9 \U0001F600"
literal = 'C:\no\escapes'
multi = """
first newline trimmed \
    joined"""
'''
        + r"""
multi_literal = '''
raw \n'''
int = -1_000
hex = 0xff
oct = 0o17
bin = 0b101
float = 6.02e23
neg_zero = -0.0
neg_inf = -inf
yes = true
"""
    )
    assert_identical(
        doc,
        {
            "basic": 'tab\there "quoted" \u00e9 \U0001f600',
            "literal": "C:\\no\\escapes",
            "multi": "first newline trimmed joined",
            "multi_literal": "raw \\n",
            "int": -1000,
            "hex": 255,
            "oct": 15,
            "bin": 5,
            "float": 6.02e23,
            "neg_zero": -0.0,
            "neg_inf": float("-inf"),
            "yes": True,
        },
    )


def test_dates_and_times() -> None:
    doc = _toml.loads(
        "odt = 1979-05-27T00:32:00.999999-07:00\n"
        "utc = 1979-05-27 07:32:00z\n"
        "ldt = 1979-05-27T07:32:00.1234567\n"
        "ld = 1979-05-27\n"
        "lt = 07:32:00.5\n"
    )
    assert_identical(
        doc,
        {
            "odt": dt.datetime(
                1979, 5, 27, 0, 32, 0, 999999, tzinfo=dt.timezone(dt.timedelta(hours=-7))
            ),
            "utc": dt.datetime(1979, 5, 27, 7, 32, tzinfo=dt.timezone.utc),
            "ldt": dt.datetime(1979, 5, 27, 7, 32, 0, 123456),
            "ld": dt.date(1979, 5, 27),
            "lt": dt.time(7, 32, 0, 500000),
        },
    )
    assert doc["ldt"].tzinfo is None and doc["lt"].tzinfo is None


def test_tables_and_arrays_of_tables() -> None:
    doc = _toml.loads(
        "title = 'x'\n"
        "[server]\nport = 8080\nlimits.max = 5\n"
        "[[sites]]\nname = 'a'\n[sites.features]\nreplay = true\n"
        "[[sites]]\nname = 'b'\nfeatures = { replay = false }\n"
        "[x.y.z]\n[x]\nw = 1\n"
    )
    assert_identical(
        doc,
        {
            "title": "x",
            "server": {"port": 8080, "limits": {"max": 5}},
            "sites": [
                {"name": "a", "features": {"replay": True}},
                {"name": "b", "features": {"replay": False}},
            ],
            "x": {"y": {"z": {}}, "w": 1},
        },
    )


def test_crlf_becomes_lf_in_multiline_strings() -> None:
    doc = _toml.loads("a = \"\"\"\r\none\r\ntwo\"\"\"\r\nb = '''x\r\ny'''\r\n")
    assert doc == {"a": "one\ntwo", "b": "x\ny"}


def test_moderate_nesting() -> None:
    depth = 100
    arrays = "[" * depth + "]" * depth
    tables = "{c = " * depth + "1" + "}" * depth
    doc = _toml.loads(f"a = {arrays}\nb = {tables}\n")
    value: Any = doc["a"]
    for _ in range(depth - 1):
        assert len(value) == 1
        value = value[0]
    assert value == []
    table: Any = doc["b"]
    for _ in range(depth):
        table = table["c"]
    assert table == 1


def test_excessive_nesting_raises_decode_error() -> None:
    depth = sys.getrecursionlimit() * 2
    with pytest.raises(_toml.TOMLDecodeError, match="nested too deeply"):
        _toml.loads("a = " + "[" * depth + "]" * depth)


@pytest.mark.skipif(
    not hasattr(sys, "get_int_max_str_digits"), reason="no integer-string conversion limit"
)
def test_integer_beyond_conversion_limit_raises_decode_error() -> None:
    digits = sys.get_int_max_str_digits() + 1  # type: ignore[attr-defined]
    with pytest.raises(_toml.TOMLDecodeError, match="too large"):
        _toml.loads("a = " + "1" * digits)


# --- parse_float -----------------------------------------------------------------


def test_parse_float_receives_the_literal_without_underscores() -> None:
    seen: List[str] = []

    def record(text: str) -> Decimal:
        seen.append(text)
        return Decimal(text)

    doc = _toml.loads(
        "a = 1_000.000_1\nb = -inf\nc = +nan\nd = 1e-0_5\ne = 3\nf = [0.5]", parse_float=record
    )
    assert seen == ["1000.0001", "-inf", "+nan", "1e-05", "0.5"]
    assert doc["a"] == Decimal("1000.0001") and doc["f"] == [Decimal("0.5")]
    assert type(doc["e"]) is int


@pytest.mark.parametrize("result", [{}, []], ids=["dict", "list"])
def test_parse_float_must_not_return_containers(result: Any) -> None:
    with pytest.raises(ValueError, match="parse_float"):
        _toml.loads("a = 1.5", parse_float=lambda text: result)


def test_parse_float_returning_none_is_still_a_value() -> None:
    assert _toml.loads("a = 1.5", parse_float=lambda text: None) == {"a": None}
    with pytest.raises(_toml.TOMLDecodeError):
        _toml.loads("a = 1.5\na.b = 1", parse_float=lambda text: None)
    with pytest.raises(_toml.TOMLDecodeError):
        _toml.loads("a = 1.5\n[a]", parse_float=lambda text: None)


# --- load() and loads() arguments ---------------------------------------------------


def test_load_reads_a_binary_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_bytes('name = "Café ☕"\r\n[server]\r\nport = 8080\r\n'.encode("utf-8"))
    with path.open("rb") as fp:
        assert _toml.load(fp) == {"name": "Café ☕", "server": {"port": 8080}}


def test_load_reads_in_memory_bytes() -> None:
    assert _toml.load(io.BytesIO(b"a = 1.5"), parse_float=Decimal) == {"a": Decimal("1.5")}


def test_load_rejects_a_text_mode_file(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    path.write_text("a = 1\n", encoding="utf-8")
    with path.open("r", encoding="utf-8") as fp:
        with pytest.raises(TypeError, match="binary mode"):
            _toml.load(fp)  # type: ignore[arg-type]


def test_load_rejects_invalid_utf8() -> None:
    with pytest.raises(UnicodeDecodeError):
        _toml.load(io.BytesIO(b"a = '\xff'"))


def test_loads_rejects_bytes() -> None:
    with pytest.raises(TypeError, match="Expected str object, not 'bytes'"):
        _toml.loads(b"a = 1")  # type: ignore[arg-type]


# --- Errors -----------------------------------------------------------------------


def test_error_reports_line_and_column() -> None:
    with pytest.raises(_toml.TOMLDecodeError) as info:
        _toml.loads("a = 1\nb = 2\nc = @\n")
    error = info.value
    assert (error.lineno, error.colno, error.pos) == (3, 5, 16)
    assert str(error) == f"{error.msg} (at line 3, column 5)"
    assert error.doc == "a = 1\nb = 2\nc = @\n"


def test_error_at_end_of_document_still_has_line_and_column() -> None:
    with pytest.raises(_toml.TOMLDecodeError, match=r"end of document, line 2, column 6"):
        _toml.loads('a = 1\nb = "')


def test_error_names_the_redefined_table() -> None:
    with pytest.raises(_toml.TOMLDecodeError, match=r"table a\.'b c'.*already defined.*line 2"):
        _toml.loads("[a.'b c']\n[a.\"b c\"]\n")


def test_decode_error_is_a_picklable_value_error() -> None:
    with pytest.raises(_toml.TOMLDecodeError) as info:
        _toml.loads("x = [1,\n2,,]")
    error = info.value
    assert isinstance(error, ValueError)
    clone = pickle.loads(pickle.dumps(error))
    assert str(clone) == str(error)
    assert (clone.msg, clone.pos, clone.lineno, clone.colno) == (
        error.msg,
        error.pos,
        error.lineno,
        error.colno,
    )
