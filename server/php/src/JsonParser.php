<?php
/**
 * The exact JSON parser: a port of CPython 3.10's C scanner (Modules/_json.c) as used by
 * json.loads(text, parse_constant=reject) in the Python collector. It accepts exactly what
 * Python accepts and raises the same error messages ("Expecting value: line 1 column 2
 * (char 1)", ...), with positions counted in characters.
 *
 * Only used when PHP's json_decode cannot reproduce Python's result: on parse errors, for
 * lone surrogate escapes (kept as 3-byte WTF-8 sequences and written back as \udxxx), for
 * keys json_decode refuses as object properties, for nesting deeper than 512 levels and
 * for integers that do not fit in 64 bits (kept exactly as JsonBigInt).
 */

namespace Tidesight;

final class JsonDecodeError extends \Exception
{
}

final class JsonParser
{
    /**
     * Nesting at which Python raises RecursionError: the interpreter's limit (1000) minus the
     * frames already on the stack when the collector parses a body (measured on the dev server).
     */
    const MAX_DEPTH = 981;

    /** Characters that end a run of plain string content. */
    const STRING_STOPS = "\"\\\x00\x01\x02\x03\x04\x05\x06\x07\x08\x09\x0a\x0b\x0c\x0d\x0e\x0f\x10\x11\x12\x13\x14\x15\x16\x17\x18\x19\x1a\x1b\x1c\x1d\x1e\x1f";

    /** @var string */
    private $s;
    /** @var int */
    private $n;
    /** @var int */
    private $depth = 0;

    private function __construct($text)
    {
        $this->s = $text;
        $this->n = strlen($text);
    }

    /** Returns array(value, error message or null, true). */
    public static function parse($text)
    {
        $p = new self($text);
        try {
            return array($p->decode(), null, true);
        } catch (JsonDecodeError $e) {
            return array(null, $e->getMessage(), true);
        }
    }

    private function decode()
    {
        if (strncmp($this->s, "\xEF\xBB\xBF", 3) === 0) {
            throw $this->error('Unexpected UTF-8 BOM (decode using utf-8-sig)', 0);
        }
        $idx = $this->ws(0);
        list($value, $end) = $this->scanOnce($idx);
        $end = $this->ws($end);
        if ($end !== $this->n) {
            throw $this->error('Extra data', $end);
        }
        return $value;
    }

    private function ws($i)
    {
        return $i >= $this->n ? $i : $i + strspn($this->s, " \t\n\r", $i);
    }

    private function at($idx, $literal)
    {
        return substr($this->s, $idx, strlen($literal)) === $literal;
    }

    private function scanOnce($idx)
    {
        if ($idx >= $this->n) {
            throw $this->error('Expecting value', $idx);
        }
        switch ($this->s[$idx]) {
            case '"':
                return $this->scanString($idx + 1);
            case '{':
                if (++$this->depth > self::MAX_DEPTH) {
                    throw new JsonDecodeError('maximum recursion depth exceeded while decoding a JSON object from a unicode string');
                }
                $r = $this->parseObject($idx + 1);
                $this->depth--;
                return $r;
            case '[':
                if (++$this->depth > self::MAX_DEPTH) {
                    throw new JsonDecodeError('maximum recursion depth exceeded while decoding a JSON array from a unicode string');
                }
                $r = $this->parseArray($idx + 1);
                $this->depth--;
                return $r;
            case 'n':
                if ($this->at($idx, 'null')) {
                    return array(null, $idx + 4);
                }
                break;
            case 't':
                if ($this->at($idx, 'true')) {
                    return array(true, $idx + 4);
                }
                break;
            case 'f':
                if ($this->at($idx, 'false')) {
                    return array(false, $idx + 5);
                }
                break;
            case 'N':
                if ($this->at($idx, 'NaN')) {
                    throw new JsonDecodeError('invalid JSON constant NaN');
                }
                break;
            case 'I':
                if ($this->at($idx, 'Infinity')) {
                    throw new JsonDecodeError('invalid JSON constant Infinity');
                }
                break;
            case '-':
                if ($this->at($idx, '-Infinity')) {
                    throw new JsonDecodeError('invalid JSON constant -Infinity');
                }
                break;
        }
        return $this->matchNumber($idx);
    }

    private function parseObject($idx)
    {
        $s = $this->s;
        $last = $this->n - 1;
        $map = array();
        $idx = $this->ws($idx);
        if ($idx > $last || $s[$idx] !== '}') {
            while (true) {
                if ($idx > $last || $s[$idx] !== '"') {
                    throw $this->error('Expecting property name enclosed in double quotes', $idx);
                }
                list($key, $idx) = $this->scanString($idx + 1);
                $idx = $this->ws($idx);
                if ($idx > $last || $s[$idx] !== ':') {
                    throw $this->error("Expecting ':' delimiter", $idx);
                }
                $idx = $this->ws($idx + 1);
                list($value, $idx) = $this->scanOnce($idx);
                $map[$key] = $value;
                $idx = $this->ws($idx);
                if ($idx <= $last && $s[$idx] === '}') {
                    break;
                }
                if ($idx > $last || $s[$idx] !== ',') {
                    throw $this->error("Expecting ',' delimiter", $idx);
                }
                $idx = $this->ws($idx + 1);
            }
        }
        return array(new JsonObject($map), $idx + 1);
    }

    private function parseArray($idx)
    {
        $s = $this->s;
        $last = $this->n - 1;
        $list = array();
        $idx = $this->ws($idx);
        if ($idx > $last || $s[$idx] !== ']') {
            while (true) {
                list($value, $idx) = $this->scanOnce($idx);
                $list[] = $value;
                $idx = $this->ws($idx);
                if ($idx <= $last && $s[$idx] === ']') {
                    break;
                }
                if ($idx > $last || $s[$idx] !== ',') {
                    throw $this->error("Expecting ',' delimiter", $idx);
                }
                $idx = $this->ws($idx + 1);
            }
        }
        return array($list, $idx + 1);
    }

    /** scanstring_unicode: $end is the index just after the opening quote. */
    private function scanString($end)
    {
        $s = $this->s;
        $len = $this->n;
        $begin = $end - 1;
        $out = '';
        while (true) {
            $next = $end < $len ? $end + strcspn($s, self::STRING_STOPS, $end) : $len;
            if ($next >= $len) {
                throw $this->error('Unterminated string starting at', $begin);
            }
            $c = $s[$next];
            if ($c !== '"' && $c !== '\\') {
                throw $this->error('Invalid control character at', $next);
            }
            if ($next > $end) {
                $out .= substr($s, $end, $next - $end);
            }
            $next++;
            if ($c === '"') {
                $end = $next;
                break;
            }
            if ($next === $len) {
                throw $this->error('Unterminated string starting at', $begin);
            }
            $c = $s[$next];
            if ($c !== 'u') {
                $end = $next + 1;
                switch ($c) {
                    case '"':
                    case '\\':
                    case '/':
                        $out .= $c;
                        break;
                    case 'b':
                        $out .= "\x08";
                        break;
                    case 'f':
                        $out .= "\x0c";
                        break;
                    case 'n':
                        $out .= "\n";
                        break;
                    case 'r':
                        $out .= "\r";
                        break;
                    case 't':
                        $out .= "\t";
                        break;
                    default:
                        throw $this->error('Invalid \\escape', $end - 2);
                }
                continue;
            }
            $next++;
            $end = $next + 4;
            if ($end >= $len) {
                throw $this->error('Invalid \\uXXXX escape', $next - 1);
            }
            $hex = substr($s, $next, 4);
            if (strspn($hex, '0123456789abcdefABCDEF') !== 4) {
                throw $this->error('Invalid \\uXXXX escape', $end - 5);
            }
            $cp = (int) hexdec($hex);
            $next = $end;
            if ($cp >= 0xD800 && $cp <= 0xDBFF && $end + 6 < $len && $s[$next] === '\\' && $s[$next + 1] === 'u') {
                $end += 6;
                $hex2 = substr($s, $next + 2, 4);
                if (strspn($hex2, '0123456789abcdefABCDEF') !== 4) {
                    throw $this->error('Invalid \\uXXXX escape', $end - 5);
                }
                $c2 = (int) hexdec($hex2);
                if ($c2 >= 0xDC00 && $c2 <= 0xDFFF) {
                    $cp = 0x10000 + (($cp - 0xD800) << 10) + ($c2 - 0xDC00);
                } else {
                    $end -= 6;
                }
            }
            $out .= self::chr($cp);
        }
        return array($out, $end);
    }

    /** UTF-8 for a code point; a lone surrogate becomes its 3-byte (WTF-8) form. */
    private static function chr($cp)
    {
        if ($cp < 0x80) {
            return chr($cp);
        }
        if ($cp < 0x800) {
            return chr(0xC0 | ($cp >> 6)) . chr(0x80 | ($cp & 0x3F));
        }
        if ($cp < 0x10000) {
            if ($cp >= 0xD800 && $cp <= 0xDFFF) {
                Util::$loneSurrogates = true;
            }
            return chr(0xE0 | ($cp >> 12)) . chr(0x80 | (($cp >> 6) & 0x3F)) . chr(0x80 | ($cp & 0x3F));
        }
        return chr(0xF0 | ($cp >> 18)) . chr(0x80 | (($cp >> 12) & 0x3F)) . chr(0x80 | (($cp >> 6) & 0x3F))
            . chr(0x80 | ($cp & 0x3F));
    }

    /** _match_number_unicode. */
    private function matchNumber($start)
    {
        if (preg_match('/\G-?(?:0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?/', $this->s, $m, 0, $start) !== 1) {
            throw $this->error('Expecting value', $start);
        }
        $literal = $m[0];
        $next = $start + strlen($literal);
        if ((isset($m[1]) && $m[1] !== '') || (isset($m[2]) && $m[2] !== '')) {
            $f = (float) $literal;
            if (is_infinite($f)) { // collect._finite_float, the collector's parse_float: 1e999 makes the body unreadable
                throw new JsonDecodeError('number out of range');
            }
            return array($f, $next);
        }
        $negative = $literal[0] === '-';
        $digits = $negative ? substr($literal, 1) : $literal;
        if (strlen($digits) > 4300) {
            // Python 3.10.7+ refuses longer integer literals (the wording is 3.10/3.11's).
            throw new JsonDecodeError(sprintf('Exceeds the limit (4300) for integer string conversion: '
                . 'value has %d digits; use sys.set_int_max_str_digits() to increase the limit', strlen($digits)));
        }
        $int = Util::intFromDigits($negative, $digits);
        if ($int === null) {
            return array(new JsonBigInt($literal), $next);
        }
        return array($int, $next);
    }

    /** JSONDecodeError(msg, doc, pos): "msg: line L column C (char P)", positions in characters. */
    private function error($msg, $pos)
    {
        $prefix = (string) substr($this->s, 0, $pos);
        $char = $pos - preg_match_all('/[\x80-\xBF]/', $prefix);
        $line = substr_count($prefix, "\n") + 1;
        $nl = strrpos($prefix, "\n");
        if ($nl === false) {
            $col = $char + 1;
        } else {
            $col = $char - ($nl - preg_match_all('/[\x80-\xBF]/', substr($prefix, 0, $nl)));
        }
        return new JsonDecodeError(sprintf('%s: line %d column %d (char %d)', $msg, $line, $col, $char));
    }
}
