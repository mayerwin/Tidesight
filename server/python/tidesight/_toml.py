"""Fallback TOML parser for Pythons that have neither ``tomllib`` nor ``tomli``.

Tidesight reads its configuration with :mod:`tomllib` (Python 3.11+) or the
``tomli`` package when one of them is importable. Shared hosting often runs
an older Python with neither, so this module provides a parser written with
the standard library only and with the same interface::

    load(fp, /, *, parse_float=float) -> dict   # fp is opened in binary mode
    loads(s, /, *, parse_float=float) -> dict
    TOMLDecodeError                           # a ValueError subclass

It implements TOML 1.0.0 (https://toml.io/en/v1.0.0) completely and returns
what :mod:`tomllib` returns: ``dict`` and ``list`` containers, ``str``,
``int``, ``bool``, ``float`` (or whatever *parse_float* returns; it receives
the literal text without underscores, such as ``"-1e10"`` or ``"+inf"``) and
``datetime.datetime``, ``datetime.date`` and ``datetime.time`` objects.
Offset date-times get a fixed :class:`datetime.timezone`, and fractional
seconds are truncated to microseconds. CRLF line endings are accepted; inside
multi-line strings they become LF.

The additions of TOML 1.1 (newlines and a trailing comma in inline tables,
the ``\\e`` and ``\\xHH`` escapes, times without seconds) are rejected, as
they are by :mod:`tomllib` up to Python 3.14.

Two deliberate differences concern input that :mod:`tomllib` cannot parse
either: arrays or inline tables nested too deeply for the interpreter's
recursion limit, and integers longer than Python's integer-string conversion
limit, raise :class:`TOMLDecodeError` (with a location) instead of
:class:`RecursionError` or a plain :class:`ValueError`.
"""

from __future__ import annotations

import re
import string
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, BinaryIO, Callable, Dict, List, Optional, Set, Tuple, Union

__all__ = ["TOMLDecodeError", "load", "loads"]

ParseFloat = Callable[[str], Any]
Key = Tuple[str, ...]
Table = Dict[str, Any]


class TOMLDecodeError(ValueError):
    """Raised when a document is not valid TOML.

    The message ends with the location, e.g. ``Invalid value (at line 3,
    column 9)``. As with :mod:`tomllib` in Python 3.14, the details are also
    available as attributes:

    * ``msg``: the message without the location;
    * ``doc``: the document being parsed (with CRLF folded to LF);
    * ``pos``: the index in ``doc`` where the problem was found;
    * ``lineno`` and ``colno``: the 1-based line and column of ``pos``.
    """

    def __init__(self, msg: str, doc: str, pos: int) -> None:
        lineno = doc.count("\n", 0, pos) + 1
        colno = pos - doc.rfind("\n", 0, pos)
        where = f"line {lineno}, column {colno}"
        if pos >= len(doc):
            where = f"end of document, {where}"
        super().__init__(f"{msg} (at {where})")
        self.msg = msg
        self.doc = doc
        self.pos = pos
        self.lineno = lineno
        self.colno = colno

    def __reduce__(self) -> Tuple[Any, ...]:
        return self.__class__, (self.msg, self.doc, self.pos)


def load(fp: BinaryIO, /, *, parse_float: ParseFloat = float) -> Dict[str, Any]:
    """Parse a TOML document from a file object opened in binary mode."""
    data = fp.read()
    try:
        text = data.decode()
    except AttributeError:
        raise TypeError(
            "File must be opened in binary mode, e.g. use `open('foo.toml', 'rb')`"
        ) from None
    return loads(text, parse_float=parse_float)


def loads(s: str, /, *, parse_float: ParseFloat = float) -> Dict[str, Any]:
    """Parse a TOML document from a string."""
    if not isinstance(s, str):
        raise TypeError(f"Expected str object, not '{type(s).__qualname__}'")
    # CRLF is a newline wherever LF is. Folding it first keeps the parser
    # simple and, as in tomllib, turns CRLF into LF inside multi-line strings.
    src = s.replace("\r\n", "\n")
    parser = _Parser(src, _checked_parse_float(parse_float))
    try:
        return parser.parse()
    except RecursionError:
        raise TOMLDecodeError(
            "Arrays or inline tables are nested too deeply", src, parser.pos
        ) from None


def _checked_parse_float(parse_float: ParseFloat) -> ParseFloat:
    """Wrap *parse_float* so that, as in tomllib, it cannot return containers."""
    if parse_float is float:
        return float

    def checked(text: str) -> Any:
        value = parse_float(text)
        if isinstance(value, (dict, list)):
            raise ValueError("parse_float must not return dicts or lists")
        return value

    return checked


# --- Lexical rules -----------------------------------------------------------

_WHITESPACE = re.compile(r"[ \t]*")
_WHITESPACE_AND_NEWLINES = re.compile(r"[ \t\n]*")
_BARE_KEY = re.compile(r"[A-Za-z0-9_-]+")
_KEY_START = frozenset(string.ascii_letters + string.digits + "_-\"'")

# Runs of characters that need no special handling. Control characters other
# than tab (and newline, in multi-line strings) are not allowed in strings or
# comments; neither is a lone carriage return, as CRLF is already folded.
_BASIC_STRING_RUN = re.compile(r'[^"\\\x00-\x08\x0a-\x1f\x7f]*')
_MULTILINE_BASIC_STRING_RUN = re.compile(r'[^"\\\x00-\x08\x0b-\x1f\x7f]*')
_LITERAL_STRING_RUN = re.compile(r"[^'\x00-\x08\x0a-\x1f\x7f]*")
_MULTILINE_LITERAL_FORBIDDEN = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")
_COMMENT_FORBIDDEN = re.compile(r"[\x00-\x08\x0a-\x1f\x7f]")

_ESCAPES = {"b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r", '"': '"', "\\": "\\"}
_HEX_DIGITS = frozenset(string.hexdigits)

_NUMBER = re.compile(
    r"""
      0x[0-9A-Fa-f](?:_?[0-9A-Fa-f])*          # hexadecimal integer
    | 0o[0-7](?:_?[0-7])*                      # octal integer
    | 0b[01](?:_?[01])*                        # binary integer
    | [+-]?(?:0|[1-9](?:_?[0-9])*)             # decimal integer, or integer part
      (?P<fraction>\.[0-9](?:_?[0-9])*)?       # of a float with a fraction
      (?P<exponent>[eE][+-]?[0-9](?:_?[0-9])*)?  # and/or an exponent
    """,
    re.VERBOSE,
)
_SPECIAL_FLOAT = re.compile(r"[+-]?(?:inf|nan)")

# HH:MM:SS with optional fractional seconds (any number of digits).
_TIME = r"([01][0-9]|2[0-3]):([0-5][0-9]):([0-5][0-9])(?:\.([0-9]+))?"
_LOCAL_TIME = re.compile(_TIME)
_DATE_TIME = re.compile(
    r"([0-9]{4})-(0[1-9]|1[0-2])-(0[1-9]|[12][0-9]|3[01])"  # full date
    r"(?:[Tt ]" + _TIME  # optional time, after T, t or a space
    + r"(?:([Zz])|([+-])([01][0-9]|2[0-3]):([0-5][0-9]))?)?"  # optional offset
)

# How a table created by the document's structure (not an inline table) was
# made, which decides how it may still be extended.
_IMPLICIT = 0  # parent of a [header]; may still be defined by its own header
_DOTTED = 1  # created or extended by dotted keys of the current section
_DEFINED = 2  # defined by a header, or by dotted keys of an earlier section


class _Parser:
    """Recursive-descent parser over one TOML document (with LF newlines)."""

    def __init__(self, src: str, parse_float: ParseFloat) -> None:
        self.src = src
        self.pos = 0
        self.parse_float = parse_float
        self.root: Table = {}
        # Tables made by headers and dotted keys, by id(), with their kind.
        # Any other dict is an inline table, which can never be extended.
        # Every object recorded here lives in the document, so ids are unique.
        self.kinds: Dict[int, int] = {id(self.root): _DEFINED}
        # Arrays made by [[header]]; any other list is a static array.
        self.table_arrays: Set[int] = set()
        # Tables created or extended by dotted keys since the last header.
        self.dotted: List[Table] = []
        # The table receiving the current section's key/value pairs.
        self.section: Table = self.root
        self.section_key: Key = ()

    # --- Document structure --------------------------------------------------

    def parse(self) -> Table:
        """Parse the whole document, one statement (line) at a time."""
        src = self.src
        size = len(src)
        while True:
            self.skip_whitespace()
            if self.pos >= size:
                return self.root
            char = src[self.pos]
            if char == "[":
                self.header()
            elif char in _KEY_START:
                self.key_value()
            elif char != "#" and char != "\n":
                raise self.error(
                    f"Expected a key, a table header or a comment; found {_describe(char)}"
                )
            self.skip_whitespace()
            self.skip_comment()
            if self.pos < size:
                if src[self.pos] != "\n":
                    raise self.error(
                        f"Expected a comment or the end of the line; "
                        f"found {_describe(src[self.pos])}"
                    )
                self.pos += 1

    def key_value(self) -> None:
        """Parse ``key = value`` and store it in the current section."""
        start = self.pos
        key = self.key()
        value = self.assigned_value()
        table = self.section
        for index in range(len(key) - 1):
            table = self.dotted_table(table, key, index, start)
        if key[-1] in table:
            path = _format_key(self.section_key + key)
            raise self.error(f"Key {path} is already defined", start)
        table[key[-1]] = value

    def dotted_table(self, parent: Table, key: Key, index: int, start: int) -> Table:
        """Return the table that the dotted *key* names at *index*.

        Dotted keys create the tables they name, or extend ones that they
        created earlier in the same section or that only exist as the parent
        of a header. Other tables, inline tables and arrays are off-limits.
        """
        name = key[index]
        if name not in parent:
            child: Table = {}
            parent[name] = child
            self.kinds[id(child)] = _DOTTED
            self.dotted.append(child)
            return child
        child = parent[name]
        kind = self.kinds.get(id(child)) if isinstance(child, dict) else None
        if kind == _DOTTED:
            return child
        if kind == _IMPLICIT:
            self.kinds[id(child)] = _DOTTED
            self.dotted.append(child)
            return child
        raise self.conflict(
            "set key",
            self.section_key + key,
            self.section_key + key[: index + 1],
            child,
            start,
        )

    def header(self) -> None:
        """Parse a ``[table]`` or ``[[array.of.tables]]`` header; enter its section."""
        # Tables made by dotted keys in the previous section are now complete.
        for table in self.dotted:
            self.kinds[id(table)] = _DEFINED
        self.dotted.clear()

        src = self.src
        start = self.pos
        is_array = src.startswith("[[", start)
        self.pos += 2 if is_array else 1
        self.skip_whitespace()
        key = self.key()
        closing = "]]" if is_array else "]"
        if not src.startswith(closing, self.pos):
            raise self.error(
                f"Expected '{closing}' at the end of the header; "
                f"found {_describe(src[self.pos : self.pos + 1])}"
            )
        self.pos += len(closing)

        action = "define array of tables" if is_array else "define table"
        parent = self.root
        for index in range(len(key) - 1):
            parent = self.header_parent(parent, key, index, action, start)
        if is_array:
            self.section = self.new_array_element(parent, key, start)
        else:
            self.section = self.new_table(parent, key, start)
        self.section_key = key

    def header_parent(
        self, parent: Table, key: Key, index: int, action: str, start: int
    ) -> Table:
        """Return the table that the header *key* passes through at *index*."""
        name = key[index]
        if name not in parent:
            child: Table = {}
            parent[name] = child
            self.kinds[id(child)] = _IMPLICIT
            return child
        child = parent[name]
        if isinstance(child, dict) and id(child) in self.kinds:
            return child
        if isinstance(child, list) and id(child) in self.table_arrays:
            return child[-1]  # the most recent element of an array of tables
        raise self.conflict(action, key, key[: index + 1], child, start)

    def new_table(self, parent: Table, key: Key, start: int) -> Table:
        """Define the table of a ``[key]`` header."""
        name = key[-1]
        if name not in parent:
            table: Table = {}
            parent[name] = table
        else:
            table = parent[name]
            if not (isinstance(table, dict) and self.kinds.get(id(table)) == _IMPLICIT):
                raise self.conflict("define table", key, key, table, start)
        self.kinds[id(table)] = _DEFINED
        return table

    def new_array_element(self, parent: Table, key: Key, start: int) -> Table:
        """Append a new table to the array of a ``[[key]]`` header."""
        name = key[-1]
        if name not in parent:
            array: List[Table] = []
            parent[name] = array
            self.table_arrays.add(id(array))
        else:
            array = parent[name]
            if not (isinstance(array, list) and id(array) in self.table_arrays):
                raise self.conflict("define array of tables", key, key, array, start)
        table: Table = {}
        self.kinds[id(table)] = _DEFINED
        array.append(table)
        return table

    def conflict(
        self, action: str, target: Key, culprit: Key, existing: Any, start: int
    ) -> TOMLDecodeError:
        """Build the error for an *action* on *target* blocked by *culprit*."""
        if isinstance(existing, dict):
            what = "a table" if id(existing) in self.kinds else "an inline table"
        elif isinstance(existing, list):
            what = "an array of tables" if id(existing) in self.table_arrays else "an array"
        else:
            what = "a value"
        subject = "it" if culprit == target else _format_key(culprit)
        return self.error(
            f"Cannot {action} {_format_key(target)}: {subject} is already defined as {what}",
            start,
        )

    # --- Keys -----------------------------------------------------------------

    def key(self) -> Key:
        """Parse a simple or dotted key, with the whitespace around its dots."""
        parts = [self.key_part()]
        self.skip_whitespace()
        while self.src.startswith(".", self.pos):
            self.pos += 1
            self.skip_whitespace()
            parts.append(self.key_part())
            self.skip_whitespace()
        return tuple(parts)

    def key_part(self) -> str:
        """Parse a bare, basic-string or literal-string key."""
        match = _BARE_KEY.match(self.src, self.pos)
        if match:
            self.pos = match.end()
            return match.group()
        char = self.src[self.pos : self.pos + 1]
        if char == '"':
            return self.basic_string()
        if char == "'":
            return self.literal_string()
        raise self.error(f"Expected a key; found {_describe(char)}")

    def assigned_value(self) -> Any:
        """Parse the ``= value`` part of a key/value pair."""
        if not self.src.startswith("=", self.pos):
            found = _describe(self.src[self.pos : self.pos + 1])
            raise self.error(f"Expected '=' after the key; found {found}")
        self.pos += 1
        self.skip_whitespace()
        return self.value()

    # --- Values ---------------------------------------------------------------

    def value(self) -> Any:
        """Parse any value."""
        src = self.src
        pos = self.pos
        char = src[pos : pos + 1]
        if char == '"':
            if src.startswith('"""', pos):
                return self.multiline_basic_string()
            return self.basic_string()
        if char == "'":
            if src.startswith("'''", pos):
                return self.multiline_literal_string()
            return self.literal_string()
        if char == "[":
            return self.array()
        if char == "{":
            return self.inline_table()
        if char == "t" and src.startswith("true", pos):
            self.pos += 4
            return True
        if char == "f" and src.startswith("false", pos):
            self.pos += 5
            return False
        return self.number_or_date()

    def number_or_date(self) -> Any:
        """Parse an integer, a float, or a date and/or time."""
        src = self.src
        start = self.pos
        first = src[start : start + 1]
        if "0" <= first <= "9":
            match = _DATE_TIME.match(src, start)
            if match:
                self.pos = match.end()
                return self.date_time(match, start)
            match = _LOCAL_TIME.match(src, start)
            if match:
                self.pos = match.end()
                hour, minute, second, fraction = match.groups()
                return time(int(hour), int(minute), int(second), _microseconds(fraction))
        match = _NUMBER.match(src, start)
        if match:
            self.pos = match.end()
            text = match.group()
            if match.group("fraction") or match.group("exponent"):
                return self.parse_float(text.replace("_", ""))
            try:
                return int(text, 0)  # also handles the 0x/0o/0b prefixes and "_"
            except ValueError:  # more digits than sys.get_int_max_str_digits()
                raise self.error("Integer is too large to convert", start) from None
        match = _SPECIAL_FLOAT.match(src, start)
        if match:
            self.pos = match.end()
            return self.parse_float(match.group())
        if first in ("", "\n", "#", ",", "]", "}"):
            raise self.error(f"Expected a value; found {_describe(first)}")
        raise self.error("Invalid value")

    def date_time(self, match: re.Match[str], start: int) -> Union[datetime, date]:
        """Build a local date, local date-time or offset date-time."""
        year, month, day, hour, minute, second, fraction = match.groups()[:7]
        zulu, sign, offset_hour, offset_minute = match.groups()[7:]
        try:
            if hour is None:
                return date(int(year), int(month), int(day))
            tz: Optional[tzinfo] = None
            if zulu:
                tz = timezone.utc
            elif sign:
                offset = timedelta(hours=int(offset_hour), minutes=int(offset_minute))
                tz = timezone(-offset if sign == "-" else offset)
            return datetime(
                int(year),
                int(month),
                int(day),
                int(hour),
                int(minute),
                int(second),
                _microseconds(fraction),
                tzinfo=tz,
            )
        except ValueError as exc:  # e.g. February 30
            raise self.error(f"Invalid date: {exc}", start) from None

    def array(self) -> List[Any]:
        """Parse ``[...]``; newlines and comments may appear between elements."""
        self.pos += 1
        items: List[Any] = []
        while True:
            self.skip_array_space()
            if self.src.startswith("]", self.pos):
                self.pos += 1
                return items
            items.append(self.value())
            self.skip_array_space()
            char = self.src[self.pos : self.pos + 1]
            if char == "]":
                self.pos += 1
                return items
            if char != ",":
                raise self.error(f"Expected ',' or ']' in array; found {_describe(char)}")
            self.pos += 1

    def inline_table(self) -> Table:
        """Parse ``{...}``: one line, no trailing comma, immutable once closed."""
        self.pos += 1
        table: Table = {}
        # Sub-tables made by dotted keys in this inline table. Any other dict
        # found here is a nested inline table, which cannot be extended.
        dotted: Set[int] = set()
        self.skip_whitespace()
        if self.src.startswith("}", self.pos):
            self.pos += 1
            return table
        while True:
            start = self.pos
            key = self.key()
            value = self.assigned_value()
            target = table
            for index, name in enumerate(key[:-1]):
                if name not in target:
                    target[name] = {}
                    dotted.add(id(target[name]))
                child = target[name]
                if not (isinstance(child, dict) and id(child) in dotted):
                    raise self.conflict("set key", key, key[: index + 1], child, start)
                target = child
            if key[-1] in target:
                raise self.error(
                    f"Key {_format_key(key)} is already defined in this inline table", start
                )
            target[key[-1]] = value
            self.skip_whitespace()
            char = self.src[self.pos : self.pos + 1]
            if char == "}":
                self.pos += 1
                return table
            if char != ",":
                raise self.error(f"Expected ',' or '}}' in inline table; found {_describe(char)}")
            self.pos += 1
            self.skip_whitespace()

    # --- Strings --------------------------------------------------------------

    def basic_string(self) -> str:
        """Parse a single-line ``"basic string"`` with escapes."""
        src = self.src
        pos = self.pos + 1
        chunks: List[str] = []
        while True:
            end = _BASIC_STRING_RUN.match(src, pos).end()
            chunks.append(src[pos:end])
            char = src[end : end + 1]
            if char == '"':
                self.pos = end + 1
                return "".join(chunks)
            if char != "\\":
                raise self.string_error(char, end)
            text, pos = self.escape(end)
            chunks.append(text)

    def multiline_basic_string(self) -> str:
        """Parse a ``\"\"\"multi-line basic string\"\"\"``."""
        src = self.src
        pos = self.pos + 3
        if src.startswith("\n", pos):
            pos += 1  # a newline right after the opening delimiter is trimmed
        chunks: List[str] = []
        while True:
            end = _MULTILINE_BASIC_STRING_RUN.match(src, pos).end()
            chunks.append(src[pos:end])
            char = src[end : end + 1]
            if char == '"':
                if src.startswith('"""', end):
                    # One or two quotes may directly precede the delimiter.
                    pos = end + 3
                    extra = _count_extra_quotes(src, pos, '"')
                    chunks.append('"' * extra)
                    self.pos = pos + extra
                    return "".join(chunks)
                chunks.append('"')
                pos = end + 1
            elif char == "\\":
                after = _WHITESPACE.match(src, end + 1).end()
                if src.startswith("\n", after):
                    # A line-ending backslash trims all whitespace and newlines
                    # up to the next other character.
                    pos = _WHITESPACE_AND_NEWLINES.match(src, after).end()
                elif after > end + 1:
                    raise self.error("A backslash followed by whitespace must end the line", end)
                else:
                    text, pos = self.escape(end)
                    chunks.append(text)
            else:
                raise self.string_error(char, end)

    def literal_string(self) -> str:
        """Parse a single-line ``'literal string'``."""
        src = self.src
        start = self.pos + 1
        end = _LITERAL_STRING_RUN.match(src, start).end()
        if not src.startswith("'", end):
            raise self.string_error(src[end : end + 1], end)
        self.pos = end + 1
        return src[start:end]

    def multiline_literal_string(self) -> str:
        """Parse a ``'''multi-line literal string'''``."""
        src = self.src
        start = self.pos + 3
        if src.startswith("\n", start):
            start += 1  # a newline right after the opening delimiter is trimmed
        end = src.find("'''", start)
        if end < 0:
            raise self.error("Unterminated multi-line string", len(src))
        forbidden = _MULTILINE_LITERAL_FORBIDDEN.search(src, start, end)
        if forbidden:
            raise self.string_error(forbidden.group(), forbidden.start())
        # One or two apostrophes may directly precede the delimiter.
        pos = end + 3
        extra = _count_extra_quotes(src, pos, "'")
        self.pos = pos + extra
        return src[start:end] + "'" * extra

    def escape(self, pos: int) -> Tuple[str, int]:
        """Decode the escape sequence whose backslash is at *pos*.

        Returns the decoded text and the position after the sequence.
        """
        src = self.src
        code = src[pos + 1 : pos + 2]
        if code in _ESCAPES:
            return _ESCAPES[code], pos + 2
        if code == "u" or code == "U":
            size = 4 if code == "u" else 8
            digits = src[pos + 2 : pos + 2 + size]
            if len(digits) != size or not _HEX_DIGITS.issuperset(digits):
                raise self.error(f"Expected {size} hexadecimal digits after '\\{code}'", pos)
            codepoint = int(digits, 16)
            if codepoint > 0x10FFFF or 0xD800 <= codepoint <= 0xDFFF:
                raise self.error(f"'\\{code}{digits}' is not a Unicode scalar value", pos)
            return chr(codepoint), pos + 2 + size
        sequence = "\\" + code
        raise self.error(f"Invalid escape sequence {sequence!r}", pos)

    def string_error(self, char: str, pos: int) -> TOMLDecodeError:
        """Build the error for *char*, which stopped a string at *pos*."""
        if not char:
            return self.error("Unterminated string", pos)
        if char == "\n":
            return self.error("Unterminated string (only multi-line strings span lines)", pos)
        return self.error(f"Invalid control character U+{ord(char):04X} in string", pos)

    # --- Whitespace and comments -----------------------------------------------

    def skip_whitespace(self) -> None:
        """Skip spaces and tabs."""
        self.pos = _WHITESPACE.match(self.src, self.pos).end()

    def skip_comment(self) -> None:
        """Skip a comment, if one starts here, up to (not including) the newline."""
        src = self.src
        if not src.startswith("#", self.pos):
            return
        end = src.find("\n", self.pos)
        if end < 0:
            end = len(src)
        forbidden = _COMMENT_FORBIDDEN.search(src, self.pos, end)
        if forbidden:
            raise self.error(
                f"Invalid control character U+{ord(forbidden.group()):04X} in comment",
                forbidden.start(),
            )
        self.pos = end

    def skip_array_space(self) -> None:
        """Skip whitespace, newlines and comments between array elements."""
        while True:
            self.pos = _WHITESPACE_AND_NEWLINES.match(self.src, self.pos).end()
            if not self.src.startswith("#", self.pos):
                return
            self.skip_comment()

    def error(self, message: str, pos: Optional[int] = None) -> TOMLDecodeError:
        """Build a :class:`TOMLDecodeError` at *pos* (default: the current position)."""
        return TOMLDecodeError(message, self.src, self.pos if pos is None else pos)


def _count_extra_quotes(src: str, pos: int, quote: str) -> int:
    """Count the quotes (at most two) at *pos*, right after a closing delimiter.

    A multi-line string may end with one or two quote characters of its own
    kind: in ``'''x'''''`` the content is ``x''``. The delimiter found first
    is therefore shifted by the quotes that follow it.
    """
    if src.startswith(quote * 2, pos):
        return 2
    return 1 if src.startswith(quote, pos) else 0


def _microseconds(fraction: Optional[str]) -> int:
    """Convert fractional-second digits to microseconds, truncating extra digits."""
    return int(fraction[:6].ljust(6, "0")) if fraction else 0


def _format_key(key: Key) -> str:
    """Render a key path for error messages, quoting parts that are not bare."""
    return ".".join(part if _BARE_KEY.fullmatch(part) else repr(part) for part in key)


def _describe(char: str) -> str:
    """Describe the character found where something else was expected."""
    if not char:
        return "end of document"
    if char == "\n":
        return "end of line"
    return repr(char)
