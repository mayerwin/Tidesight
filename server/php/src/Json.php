<?php
/**
 * JSON exactly as the Python collector reads and writes it.
 *
 * Reading: json.loads(text, parse_constant=reject). PHP's json_decode is the fast path; the
 * exact parser (JsonParser.php, a port of CPython's scanner) takes over when json_decode
 * fails (to accept what Python accepts, e.g. lone surrogate escapes, and to reproduce its
 * error messages) and when the text holds integers that do not fit in 64 bits.
 *
 * Writing: json.dumps(value, ensure_ascii=False, separators=(",", ":")) for stored JSON,
 * and json.dumps(value, sort_keys=True) (ensure_ascii) for the site configuration. Floats
 * use Python's repr (shortest round-trip digits, "1e-05", "1e+16", "100.0").
 *
 * Values: JSON objects are stdClass (fast path) or JsonObject (exact parser), arrays are
 * PHP lists, integers beyond 64 bits are JsonBigInt. Maps built by the collector are
 * passed as stdClass so that an empty map is written "{}".
 */

namespace Tidesight;

/** A JSON object from the exact parser: an ordered map that allows any key. */
final class JsonObject
{
    /** @var array */
    public $m;

    public function __construct(array $m = array())
    {
        $this->m = $m;
    }
}

/** An integer literal that does not fit in 64 bits, kept as its decimal digits. */
final class JsonBigInt
{
    /** @var string */
    public $digits;

    public function __construct($digits)
    {
        $this->digits = $digits;
    }
}

final class Json
{
    /** ensure_ascii=False, no escaped slashes or line terminators (as Python), floats keep ".0". */
    const FLAGS = JSON_UNESCAPED_SLASHES | JSON_UNESCAPED_UNICODE | JSON_PRESERVE_ZERO_FRACTION
        | JSON_UNESCAPED_LINE_TERMINATORS;

    /** A JSON string token (skipped), or a float token PHP and Python write differently. */
    const FLOAT_FIX = '/"(?:[^"\\\\]++|\\\\.)*+"(*SKIP)(*FAIL)|-?[0-9]+(?:\.[0-9]+)?e[+-]?[0-9]+|-?[0-9]{17,}\.[0-9]+/';

    /** A JSON string token (skipped), or 19+ digits outside strings (a possible >64-bit integer). */
    const BIG_INT = '/"(?:[^"\\\\]++|\\\\.)*+"(*SKIP)(*FAIL)|[0-9]{19}/';

    /** @var bool|null whether json_encode() gives the shortest round-trip repr of floats */
    private static $shortest = null;

    public static function init()
    {
        if (self::$shortest === null) {
            if (function_exists('ini_set')) { // else the shortest repr is searched with sprintf
                @ini_set('serialize_precision', '-1');
            }
            self::$shortest = json_encode(0.1) === '0.1' && json_encode(1 / 3) === '0.3333333333333333';
        }
    }

    // -----------------------------------------------------------------------
    // Reading
    // -----------------------------------------------------------------------

    /**
     * json.loads(text, parse_constant=reject) on valid UTF-8 text (BOM already removed).
     * Returns array(value, error message or null, exact): `exact` is true when the value came
     * from the exact parser and must be written with Json::dumps($value, true).
     */
    public static function loads($text)
    {
        $value = json_decode($text, false, 512);
        if (json_last_error() !== JSON_ERROR_NONE) {
            return JsonParser::parse($text);
        }
        // Integers beyond 64 bits need the exact parser. The scan runs only on text that decoded, where every
        // string is terminated, so it is linear (on arbitrary text an unterminated string made it quadratic);
        // if PCRE still gives up (false), the exact parser decides.
        if (strlen($text) >= 19 && preg_match('/[0-9]{19}/', $text) === 1 && preg_match(self::BIG_INT, $text) !== 0) {
            $value = null; // free json_decode's tree before the exact parser builds its own
            return JsonParser::parse($text);
        }
        // collect._finite_float (the collector's parse_float): a number too large for a float makes the body
        // unreadable. Without 19-digit runs only an exponent of three digits or more can overflow.
        if (preg_match('/[eE][+-]?[0-9]{3}/', $text) === 1 && self::hasInfinite($value)) {
            return array(null, 'number out of range', false);
        }
        return array($value, null, false);
    }

    /** Whether a decoded value holds an infinite float (from a literal like 1e999). Iterative. */
    private static function hasInfinite($value)
    {
        $stack = array($value);
        while ($stack) {
            $v = array_pop($stack);
            if (is_float($v)) {
                if (is_infinite($v)) {
                    return true;
                }
            } elseif (is_array($v)) {
                foreach ($v as $x) {
                    $stack[] = $x;
                }
            } elseif ($v instanceof \stdClass) {
                foreach (get_object_vars($v) as $x) {
                    $stack[] = $x;
                }
            }
        }
        return false;
    }

    public static function isObject($v)
    {
        return $v instanceof \stdClass || $v instanceof JsonObject;
    }

    /** dict.get(key) on a decoded JSON object. */
    public static function get($obj, $key)
    {
        if ($obj instanceof \stdClass) {
            return isset($obj->$key) ? $obj->$key : null;
        }
        if ($obj instanceof JsonObject) {
            return array_key_exists($key, $obj->m) ? $obj->m[$key] : null;
        }
        return null;
    }

    /**
     * collect._deeper_than: true when objects and arrays nest more than $limit levels (the top level
     * is 1). Iterative. $text, the JSON it came from, lets a document with too few brackets skip the walk.
     */
    public static function deeperThan($value, $limit, $text = null)
    {
        if ($text !== null && self::nesting($text, $limit) <= $limit) {
            return false; // the text never nests that deep: no walk
        }
        return self::deeper($value, 1, $limit);
    }

    /**
     * Whether a container at $depth holds one deeper than $limit. Recursion stops past $limit, so it is at
     * most $limit + 1 frames deep, and nothing is copied (the walk runs only when the text nests that deep;
     * the decoded value can still be shallower, as a repeated key keeps only its last value).
     */
    private static function deeper($v, $depth, $limit)
    {
        if ($depth > $limit) {
            return true;
        }
        foreach (($v instanceof JsonObject ? $v->m : $v) as $x) {
            if ((is_array($x) || $x instanceof \stdClass || $x instanceof JsonObject) && self::deeper($x, $depth + 1, $limit)) {
                return true;
            }
        }
        return false;
    }

    /**
     * The deepest nesting of objects and arrays in JSON text, strings skipped, counted up to $stop + 1.
     * Linear: strcspn jumps to the next bracket or quote.
     */
    public static function nesting($text, $stop)
    {
        $n = strlen($text);
        $i = 0;
        $depth = 0;
        $max = 0;
        while ($i < $n) {
            $i += strcspn($text, '"[]{}', $i);
            if ($i >= $n) {
                break;
            }
            $c = $text[$i];
            if ($c === '"') {
                $i++;
                while (true) {
                    $i += strcspn($text, "\"\\", $i);
                    if ($i >= $n) {
                        return $max; // an unterminated string: nothing more nests
                    }
                    if ($text[$i] === '\\') {
                        $i += 2;
                        continue;
                    }
                    $i++;
                    break;
                }
            } elseif ($c === '[' || $c === '{') {
                $depth++;
                if ($depth > $max) {
                    $max = $depth;
                    if ($max > $stop) {
                        return $max;
                    }
                }
                $i++;
            } else {
                $depth--;
                $i++;
            }
        }
        return $max;
    }

    /** A copy of a decoded JSON object without one key. */
    public static function without($obj, $key)
    {
        if ($obj instanceof JsonObject) {
            $m = $obj->m;
            unset($m[$key]);
            return new JsonObject($m);
        }
        $copy = clone $obj;
        unset($copy->$key);
        return $copy;
    }

    // -----------------------------------------------------------------------
    // Writing
    // -----------------------------------------------------------------------

    /** json.dumps(v, ensure_ascii=False, separators=(",", ":")). */
    public static function dumps($v, $exact = false)
    {
        if (!$exact) {
            $s = json_encode($v, self::FLAGS, 4096);
            if ($s !== false) {
                if (preg_match('/[0-9]e|[0-9]{17}\./', $s) !== 1) {
                    return $s;
                }
                $fixed = preg_replace_callback(self::FLOAT_FIX, function ($m) {
                    return Json::pyFloatToken($m[0]);
                }, $s);
                return $fixed === null ? $s : $fixed;
            }
        }
        return self::encodeExact($v);
    }

    /** The recursive encoder: floats as Python (Infinity/NaN included), big integers, lone surrogates. */
    public static function encodeExact($v)
    {
        if (is_string($v)) {
            return self::encodeString($v);
        }
        if (is_int($v)) {
            return (string) $v;
        }
        if (is_float($v)) {
            return self::pyFloatRepr($v);
        }
        if ($v === null) {
            return 'null';
        }
        if (is_bool($v)) {
            return $v ? 'true' : 'false';
        }
        if (is_array($v)) {
            $parts = array();
            foreach ($v as $x) {
                $parts[] = self::encodeExact($x);
            }
            return '[' . implode(',', $parts) . ']';
        }
        if ($v instanceof JsonBigInt) {
            return $v->digits;
        }
        $map = $v instanceof JsonObject ? $v->m : (is_object($v) ? get_object_vars($v) : array());
        $parts = array();
        foreach ($map as $k => $x) {
            $parts[] = self::encodeString((string) $k) . ':' . self::encodeExact($x);
        }
        return '{' . implode(',', $parts) . '}';
    }

    /** A JSON string as Python writes it with ensure_ascii=False (a lone surrogate as U+FFFD). */
    public static function encodeString($s)
    {
        $j = json_encode($s, self::FLAGS);
        if ($j !== false) {
            return $j;
        }
        return (string) json_encode(Util::textSafe($s), self::FLAGS);
    }

    /**
     * Lone surrogates become U+FFFD in every string, keys included, as in the Python collector:
     * JSON can carry them (an escape such as \ud83d, from text cut in the middle of an emoji) but
     * UTF-8 cannot. Only the exact parser produces them (as WTF-8), and it sets Util::$loneSurrogates.
     * A key that becomes equal to an earlier one replaces its value, keeping its position (a dict).
     */
    public static function replaceSurrogates($v)
    {
        if (is_string($v)) {
            return Util::textSafe($v);
        }
        if (is_array($v)) {
            return array_map(array(__CLASS__, 'replaceSurrogates'), $v);
        }
        if ($v instanceof JsonObject) {
            $m = array();
            foreach ($v->m as $k => $x) {
                $m[Util::textSafe((string) $k)] = self::replaceSurrogates($x);
            }
            return new JsonObject($m);
        }
        return $v;
    }

    /**
     * json.dumps(v, ensure_ascii=True, sort_keys=$sortKeys, separators=($itemSep, $keySep)),
     * used for the public site configuration (config.js, its "cv" hash) and /health.
     */
    public static function dumpsAscii($v, $sortKeys, $itemSep = ', ', $keySep = ': ')
    {
        if (is_string($v)) {
            $j = json_encode($v, JSON_UNESCAPED_SLASHES);
            if ($j === false) {
                $j = (string) json_encode(Util::textSafe($v), JSON_UNESCAPED_SLASHES);
            }
            return str_replace("\x7f", '\\u007f', $j);
        }
        if (is_int($v)) {
            return (string) $v;
        }
        if (is_float($v)) {
            return self::pyFloatRepr($v);
        }
        if ($v === null) {
            return 'null';
        }
        if (is_bool($v)) {
            return $v ? 'true' : 'false';
        }
        if (is_array($v)) {
            $parts = array();
            foreach ($v as $x) {
                $parts[] = self::dumpsAscii($x, $sortKeys, $itemSep, $keySep);
            }
            return '[' . implode($itemSep, $parts) . ']';
        }
        if ($v instanceof JsonBigInt) {
            return $v->digits;
        }
        $map = $v instanceof JsonObject ? $v->m : (is_object($v) ? get_object_vars($v) : array());
        if ($sortKeys) {
            ksort($map, SORT_STRING);
        }
        $parts = array();
        foreach ($map as $k => $x) {
            $parts[] = self::dumpsAscii((string) $k, false) . $keySep . self::dumpsAscii($x, $sortKeys, $itemSep, $keySep);
        }
        return '{' . implode($itemSep, $parts) . '}';
    }

    // -----------------------------------------------------------------------
    // Floats
    // -----------------------------------------------------------------------

    /** float.__repr__ as json.dumps writes it ("Infinity", "-Infinity", "NaN" for non-finite values). */
    public static function pyFloatRepr($f)
    {
        if (is_nan($f)) {
            return 'NaN';
        }
        if (is_infinite($f)) {
            return $f > 0 ? 'Infinity' : '-Infinity';
        }
        return self::pyFloatToken(self::shortest($f));
    }

    /** str(float): as repr, but "inf", "-inf", "nan" for non-finite values. */
    public static function pyStrFloat($f)
    {
        if (is_nan($f)) {
            return 'nan';
        }
        if (is_infinite($f)) {
            return $f > 0 ? 'inf' : '-inf';
        }
        return self::pyFloatToken(self::shortest($f));
    }

    /** Shortest decimal text that reads back as the same double. */
    private static function shortest($f)
    {
        if (self::$shortest === null) {
            self::init();
        }
        if (self::$shortest) {
            $s = json_encode($f, JSON_PRESERVE_ZERO_FRACTION);
            if ($s !== false) {
                return $s;
            }
        }
        for ($p = 0; $p < 17; $p++) {
            $s = sprintf('%.' . $p . 'e', $f);
            if ((float) $s === $f) {
                return $s;
            }
        }
        return sprintf('%.16e', $f);
    }

    /** Re-format a decimal float token (any notation) the way Python's repr() writes it. */
    public static function pyFloatToken($tok)
    {
        if (preg_match('/\A(-?)([0-9]+)(?:\.([0-9]*))?(?:[eE]([+-]?[0-9]+))?\z/', $tok, $m) !== 1) {
            return $tok;
        }
        $sign = $m[1];
        $digits = $m[2] . (isset($m[3]) ? $m[3] : '');
        $decpt = strlen($m[2]) + (isset($m[4]) && $m[4] !== '' ? (int) $m[4] : 0);
        $stripped = ltrim($digits, '0');
        $decpt -= strlen($digits) - strlen($stripped);
        $digits = rtrim($stripped, '0');
        if ($digits === '') {
            return $sign . '0.0';
        }
        $nd = strlen($digits);
        if ($decpt <= -4 || $decpt > 16) {
            $exp = $decpt - 1;
            $mant = $nd > 1 ? $digits[0] . '.' . substr($digits, 1) : $digits;
            return $sign . $mant . 'e' . ($exp < 0 ? '-' : '+') . str_pad((string) abs($exp), 2, '0', STR_PAD_LEFT);
        }
        if ($decpt <= 0) {
            return $sign . '0.' . str_repeat('0', -$decpt) . $digits;
        }
        if ($decpt >= $nd) {
            return $sign . $digits . str_repeat('0', $decpt - $nd) . '.0';
        }
        return $sign . substr($digits, 0, $decpt) . '.' . substr($digits, $decpt);
    }
}
