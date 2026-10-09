<?php
/**
 * Small helpers that reproduce the parts of the Python standard library the reference
 * collector relies on: str.strip(), slicing by code point, urllib.parse.parse_qs/unquote,
 * urlsplit (for the Referer origin), fnmatch, bounded gzip decompression, gzip.compress.
 *
 * Strings are handled as Python sees them under WSGI: request headers and the query
 * string are ISO-8859-1 decoded (PEP 3333), bodies are UTF-8. In PHP they are kept as
 * UTF-8 byte strings.
 */

namespace Tidesight;

final class Util
{
    /** First code point of every run of Unicode decimal digits (Python 3.10 / Unicode 13), for int(). */
    const DECIMAL_ZEROS = array(
        0x30, 0x660, 0x6F0, 0x7C0, 0x966, 0x9E6, 0xA66, 0xAE6, 0xB66, 0xBE6, 0xC66, 0xCE6, 0xD66, 0xDE6,
        0xE50, 0xED0, 0xF20, 0x1040, 0x1090, 0x17E0, 0x1810, 0x1946, 0x19D0, 0x1A80, 0x1A90, 0x1B50,
        0x1BB0, 0x1C40, 0x1C50, 0xA620, 0xA8D0, 0xA900, 0xA9D0, 0xA9F0, 0xAA50, 0xABF0, 0xFF10, 0x104A0,
        0x10D30, 0x11066, 0x110F0, 0x11136, 0x111D0, 0x112F0, 0x11450, 0x114D0, 0x11650, 0x116C0,
        0x11730, 0x118E0, 0x11950, 0x11C50, 0x11D50, 0x11DA0, 0x16A60, 0x16B50, 0x1D7CE, 0x1D7D8,
        0x1D7E2, 0x1D7EC, 0x1D7F6, 0x1E140, 0x1E2F0, 0x1E950, 0x1FBF0,
    );

    /** Valid UTF-8 runs, or one ill-formed "maximal subpart" (captured), as Python's decoder sees them. */
    const UTF8_SCAN = '/(?:[\x00-\x7F]|[\xC2-\xDF][\x80-\xBF]|\xE0[\xA0-\xBF][\x80-\xBF]|[\xE1-\xEC\xEE\xEF][\x80-\xBF]{2}'
        . '|\xED[\x80-\x9F][\x80-\xBF]|\xF0[\x90-\xBF][\x80-\xBF]{2}|[\xF1-\xF3][\x80-\xBF]{3}|\xF4[\x80-\x8F][\x80-\xBF]{2})++'
        . '|(\xE0[\xA0-\xBF]|[\xE1-\xEC\xEE\xEF][\x80-\xBF]|\xED[\x80-\x9F]|\xF0[\x90-\xBF][\x80-\xBF]?'
        . '|[\xF1-\xF3][\x80-\xBF]{1,2}|\xF4[\x80-\x8F][\x80-\xBF]?|[\x80-\xFF])/';

    /** @var array<string,string>|null */
    private static $latin1Map = null;
    /** @var array<string,string>|null */
    private static $latin1Lower = null;
    /** @var array<string,string> */
    private static $globCache = array();
    /** Set by the exact JSON parser when a string holds a lone UTF-16 surrogate (WTF-8 bytes). */
    public static $loneSurrogates = false;

    /** Milliseconds since the epoch, as int(time.time() * 1000). */
    public static function nowMs()
    {
        return (int) floor(microtime(true) * 1000);
    }

    /** realpath() where the host allows it, else false (the callers then treat the path as unresolved). */
    public static function realpath($path)
    {
        return function_exists('realpath') ? @realpath($path) : false;
    }

    /** umask($mask) where the host allows it; returns the previous mask, or null. umask(null) does nothing. */
    public static function umask($mask)
    {
        if ($mask === null || !function_exists('umask')) {
            return null;
        }
        return umask($mask);
    }

    /**
     * The umask for Tidesight's files: owner only (0077), except under mod_php, where PHP runs as the web
     * server's user and the data directory is shared with the cron account through a group (README.md,
     * "Permissions"): there the group keeps access (0007). Nobody else ever reads them.
     */
    public static function privateMask()
    {
        return PHP_SAPI === 'apache2handler' ? 0007 : 0077;
    }

    /** mkdir -p, the new directories private (0700, or 0770 under mod_php), whatever the process umask. */
    public static function mkdirPrivate($dir)
    {
        if ($dir === '' || is_dir($dir)) {
            return is_dir($dir);
        }
        $old = self::umask(self::privateMask());
        $ok = @mkdir($dir, 0777 & ~self::privateMask(), true);
        self::umask($old);
        return $ok || is_dir($dir);
    }

    /** UTC day of a millisecond timestamp, as time.strftime("%Y-%m-%d", time.gmtime(ms / 1000)). */
    public static function day($ms)
    {
        return gmdate('Y-m-d', (int) floor($ms / 1000));
    }

    /** Python's len() of a UTF-8 string: code points, not bytes. */
    public static function length($s)
    {
        $s = (string) $s;
        $n = strlen($s);
        if ($n === 0 || preg_match('/[\x80-\xFF]/', $s) !== 1) {
            return $n;
        }
        return $n - preg_match_all('/[\x80-\xBF]/', $s); // every byte but the continuation bytes starts a character
    }

    /** collect._kb: kilobytes, rounded up (a request of 1 byte uses 1 KB of a budget). */
    public static function kb($bytes)
    {
        $n = (int) $bytes;
        if ($n <= 0) {
            return 1;
        }
        return intdiv($n, 1024) + ($n % 1024 > 0 ? 1 : 0);
    }

    /**
     * A number read from SQLite: PDO before PHP 8.1 returns text, and an INTEGER that overflowed
     * in arithmetic comes back as a REAL. Compared as a number, never truncated.
     */
    public static function number($v)
    {
        if (is_int($v) || is_float($v)) {
            return $v;
        }
        return is_string($v) && is_numeric($v) ? $v + 0 : 0;
    }

    /** Locale-independent ASCII lower-casing. */
    public static function lowerAscii($s)
    {
        return strtr($s, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz');
    }

    /** Raw bytes read as ISO-8859-1 (as a WSGI server hands headers to Python), returned as UTF-8. */
    public static function latin1($s)
    {
        if ($s === '' || preg_match('/[\x80-\xFF]/', $s) !== 1) {
            return $s;
        }
        if (self::$latin1Map === null) {
            $map = array();
            for ($i = 0x80; $i <= 0xFF; $i++) {
                $map[chr($i)] = chr(0xC0 | ($i >> 6)) . chr(0x80 | ($i & 0x3F));
            }
            self::$latin1Map = $map;
        }
        return strtr($s, self::$latin1Map);
    }

    /** Python's str.lower() for text that came from ISO-8859-1 (ASCII and Latin-1 letters). */
    public static function lowerLatin($s)
    {
        $s = self::lowerAscii($s);
        if (strpos($s, "\xC3") === false) {
            return $s;
        }
        if (self::$latin1Lower === null) {
            $map = array();
            for ($c = 0xC0; $c <= 0xDE; $c++) {
                if ($c !== 0xD7) { // U+00D7 is the multiplication sign
                    $map["\xC3" . chr(0x80 | ($c & 0x3F))] = "\xC3" . chr(0x80 | (($c + 0x20) & 0x3F));
                }
            }
            self::$latin1Lower = $map;
        }
        return strtr($s, self::$latin1Lower);
    }

    public static function isUtf8($s)
    {
        return $s === '' || preg_match('//u', $s) === 1;
    }

    /** bytes.decode("utf-8", "replace"): each ill-formed maximal subpart becomes U+FFFD. */
    public static function utf8Replace($b)
    {
        if (self::isUtf8($b)) {
            return $b;
        }
        return preg_replace_callback(self::UTF8_SCAN, function ($m) {
            return isset($m[1]) && $m[1] !== '' ? "\xEF\xBF\xBD" : $m[0];
        }, $b);
    }

    /**
     * Valid UTF-8 for a text column. Lone surrogates (only produced by the exact JSON parser,
     * as 3-byte WTF-8 sequences) become U+FFFD, one per code point, so lengths are unchanged.
     */
    public static function textSafe($s)
    {
        if (self::isUtf8($s)) {
            return $s;
        }
        $s = preg_replace('/\xED[\xA0-\xBF][\x80-\xBF]/', "\xEF\xBF\xBD", $s);
        return self::utf8Replace($s);
    }

    /**
     * Python's str.strip() on UTF-8 text, in linear time: the leading and trailing characters are compared
     * with the UTF-8 encodings of Python's whitespace (1 to 3 bytes), with no regular expression (an
     * unanchored "[ws]+\z" search is quadratic on long runs of whitespace without PCRE's JIT).
     */
    public static function pyStrip($s)
    {
        $n = strlen($s);
        if ($n === 0) {
            return $s;
        }
        $start = 0;
        while ($start < $n) {
            $w = self::spaceAt($s, $start, $n);
            if ($w === 0) {
                break;
            }
            $start += $w;
        }
        $end = $n;
        while ($end > $start) {
            if (strpos(self::SPACE1, $s[$end - 1]) !== false) {
                $end--;
            } elseif ($end - 2 >= $start && isset(self::$space2[substr($s, $end - 2, 2)])) {
                $end -= 2;
            } elseif ($end - 3 >= $start && isset(self::$space3[substr($s, $end - 3, 3)])) {
                $end -= 3;
            } else {
                break;
            }
        }
        return $start === 0 && $end === $n ? $s : (string) substr($s, $start, $end - $start);
    }

    /** One-byte whitespace of Python's str.isspace() (no NUL: strpos would find it in no string). */
    const SPACE1 = "\x09\x0A\x0B\x0C\x0D\x1C\x1D\x1E\x1F\x20";
    /** @var array<string,bool> two- and three-byte UTF-8 encodings of Python's whitespace */
    private static $space2 = array("\xC2\x85" => true, "\xC2\xA0" => true);
    private static $space3 = array("\xE1\x9A\x80" => true, "\xE2\x80\x80" => true, "\xE2\x80\x81" => true,
        "\xE2\x80\x82" => true, "\xE2\x80\x83" => true, "\xE2\x80\x84" => true, "\xE2\x80\x85" => true,
        "\xE2\x80\x86" => true, "\xE2\x80\x87" => true, "\xE2\x80\x88" => true, "\xE2\x80\x89" => true,
        "\xE2\x80\x8A" => true, "\xE2\x80\xA8" => true, "\xE2\x80\xA9" => true, "\xE2\x80\xAF" => true,
        "\xE2\x81\x9F" => true, "\xE3\x80\x80" => true);

    /** The length of the whitespace character at $i, or 0. */
    private static function spaceAt($s, $i, $n)
    {
        if (strpos(self::SPACE1, $s[$i]) !== false) {
            return 1;
        }
        if ($i + 2 <= $n && isset(self::$space2[substr($s, $i, 2)])) {
            return 2;
        }
        if ($i + 3 <= $n && isset(self::$space3[substr($s, $i, 3)])) {
            return 3;
        }
        return 0;
    }

    /**
     * Python's str.upper() of ISO-8859-1 text held as UTF-8 (a request method as a WSGI server hands it
     * over): a-z and à-þ (but ÷) to their capitals, µ to Μ, ß to SS, ÿ to Ÿ.
     */
    public static function upperLatin1($s)
    {
        $s = strtr($s, 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ');
        if (preg_match('/[\x80-\xFF]/', $s) !== 1) {
            return $s;
        }
        $map = array("\xC2\xB5" => "\xCE\x9C", "\xC3\x9F" => 'SS', "\xC3\xBF" => "\xC5\xB8");
        for ($c = 0xE0; $c <= 0xFE; $c++) {
            if ($c !== 0xF7) {
                $map["\xC3" . chr(0x80 | ($c & 0x3F))] = "\xC3" . chr(0x80 | (($c - 0x20) & 0x3F));
            }
        }
        return strtr($s, $map);
    }

    /**
     * (parse_qs(text, keep_blank_values=True).get(name) or …)[0] for the first of $names present, else "":
     * the field of a form-encoded body, found without building the whole map (with PHP's unseeded string
     * hash, a map of attacker-chosen names can be made to cost quadratic time).
     */
    public static function firstFormValue($text, array $names)
    {
        $found = array();
        foreach (explode('&', $text) as $nv) {
            if ($nv === '') {
                continue;
            }
            $eq = strpos($nv, '=');
            $name = self::unquote(str_replace('+', ' ', $eq === false ? $nv : substr($nv, 0, $eq)));
            $rank = array_search($name, $names, true);
            if ($rank === false || isset($found[$rank])) {
                continue;
            }
            $found[$rank] = $eq === false ? '' : self::unquote(str_replace('+', ' ', (string) substr($nv, $eq + 1)));
            if ($rank === 0) {
                break; // the first name wins, at its first occurrence
            }
        }
        ksort($found);
        return $found ? reset($found) : '';
    }

    /** Python's s[:n] on a UTF-8 string (code points, not bytes). */
    public static function cut($s, $n)
    {
        if (strlen($s) <= $n) {
            return $s;
        }
        if (preg_match('/\A.{0,' . (int) $n . '}/su', $s, $m) === 1) {
            return $m[0];
        }
        return self::cut(self::textSafe($s), $n);
    }

    /** collect._str: a bounded string column value, or null. */
    public static function str($value, $maxLen)
    {
        if (is_string($value)) {
            if ($value === '') {
                return null;
            }
            if (self::$loneSurrogates) {
                $value = self::textSafe($value);
            }
            return self::cut($value, $maxLen);
        }
        if (is_int($value)) {
            return self::cut((string) $value, $maxLen);
        }
        if (is_float($value)) {
            return self::cut(Json::pyStrFloat($value), $maxLen);
        }
        if ($value instanceof JsonBigInt) {
            return self::cut($value->digits, $maxLen);
        }
        return null;
    }

    /** collect._int: a signed 64-bit integer column value, or null. */
    public static function toInt($value)
    {
        if (is_int($value)) {
            return $value;
        }
        if (is_float($value)) {
            if (is_nan($value) || abs($value) >= 9.2e18) {
                return null;
            }
            return (int) $value;
        }
        if (!is_string($value) || $value === '') {
            return null; // bool, null, containers, big integers (outside int64 by construction)
        }
        $t = self::pyStrip($value);
        if (preg_match('/\A(-*)([0-9]+)\z/', $t, $m) !== 1) {
            if (preg_match('/[^\x00-\x7F]/', $t) !== 1) {
                return null;
            }
            $m = self::unicodeDigits($t);
            if ($m === null) {
                return null;
            }
        }
        if (strlen($m[1]) > 1) {
            return null; // "--5": isdigit() after lstrip("-") but int() refuses it
        }
        return self::intFromDigits($m[1] === '-', $m[2]);
    }

    /** Decimal digits (ASCII) and a sign as int64, or null when out of range (Python's range check). */
    public static function intFromDigits($negative, $digits)
    {
        $digits = ltrim($digits, '0');
        if ($digits === '') {
            return 0;
        }
        $len = strlen($digits);
        if ($len > 19) {
            return null;
        }
        if ($len === 19) {
            $limit = $negative ? '9223372036854775808' : '9223372036854775807';
            if (strcmp($digits, $limit) > 0) {
                return null;
            }
            if ($negative && $digits === '9223372036854775808') {
                return PHP_INT_MIN;
            }
        }
        $v = (int) $digits;
        return $negative ? -$v : $v;
    }

    /** "-*" + Unicode decimal digits (str.isdigit() + int()), mapped to ASCII; null otherwise. */
    private static function unicodeDigits($t)
    {
        if (preg_match('/\A(-*)(.+)\z/su', $t, $m) !== 1 || !self::isUtf8($m[2])) {
            return null;
        }
        $out = '';
        foreach (preg_split('//u', $m[2], -1, PREG_SPLIT_NO_EMPTY) as $ch) {
            $cp = self::codePoint($ch);
            $digit = null;
            foreach (self::DECIMAL_ZEROS as $zero) {
                if ($cp >= $zero && $cp <= $zero + 9) {
                    $digit = $cp - $zero;
                    break;
                }
            }
            if ($digit === null) {
                return null;
            }
            $out .= chr(48 + $digit);
        }
        return array($t, $m[1], $out);
    }

    /** Code point of one UTF-8 encoded character. */
    public static function codePoint($ch)
    {
        $b = array_values(unpack('C*', $ch));
        $n = count($b);
        if ($n === 1) {
            return $b[0];
        }
        if ($n === 2) {
            return (($b[0] & 0x1F) << 6) | ($b[1] & 0x3F);
        }
        if ($n === 3) {
            return (($b[0] & 0x0F) << 12) | (($b[1] & 0x3F) << 6) | ($b[2] & 0x3F);
        }
        return (($b[0] & 0x07) << 18) | (($b[1] & 0x3F) << 12) | (($b[2] & 0x3F) << 6) | ($b[3] & 0x3F);
    }

    // -----------------------------------------------------------------------
    // urllib.parse
    // -----------------------------------------------------------------------

    /**
     * urllib.parse.parse_qs(qs, keep_blank_values): name => list of values, in order of first
     * appearance. Numeric names become integer array keys; cast keys back with (string).
     */
    public static function parseQs($qs, $keepBlank)
    {
        $out = array();
        if ($qs === '' || $qs === null) {
            return $out;
        }
        foreach (explode('&', $qs) as $nv) {
            if ($nv === '') {
                continue;
            }
            $eq = strpos($nv, '=');
            if ($eq === false) {
                if (!$keepBlank) {
                    continue;
                }
                $name = $nv;
                $value = '';
            } else {
                $name = substr($nv, 0, $eq);
                $value = (string) substr($nv, $eq + 1);
            }
            if ($value !== '' || $keepBlank) {
                $name = self::unquote(str_replace('+', ' ', $name));
                $value = self::unquote(str_replace('+', ' ', $value));
                $out[$name][] = $value;
            }
        }
        return $out;
    }

    /** {name: first value} from parse_qs output. */
    public static function firstValues(array $parsed)
    {
        $out = array();
        foreach ($parsed as $k => $values) {
            $out[$k] = $values[0];
        }
        return $out;
    }

    /** urllib.parse.unquote (UTF-8, errors="replace"), applied to each ASCII run like Python. */
    public static function unquote($s)
    {
        if (strpos($s, '%') === false) {
            return $s;
        }
        return preg_replace_callback('/[\x00-\x7F]+/', function ($m) {
            $run = $m[0];
            if (strpos($run, '%') === false) {
                return $run;
            }
            $bytes = preg_replace_callback('/%([0-9A-Fa-f]{2})/', function ($h) {
                return chr((int) hexdec($h[1]));
            }, $run);
            return Util::utf8Replace($bytes);
        }, $s);
    }

    /**
     * collect.origin_of: "scheme://netloc" (lower-cased) of a URL, or null. Mirrors
     * urllib.parse.urlsplit of Python 3.10 for the parts used.
     */
    public static function originOf($url)
    {
        if ($url === null || $url === '') {
            return null;
        }
        $url = str_replace(array("\t", "\r", "\n"), '', $url);
        $scheme = '';
        $netloc = '';
        $i = strpos($url, ':');
        if ($i !== false && $i > 0) {
            $candidate = substr($url, 0, $i);
            $ok = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+-.';
            if (strspn($candidate, $ok) === strlen($candidate)) {
                $scheme = self::lowerAscii($candidate);
                $url = (string) substr($url, $i + 1);
            }
        }
        if (strncmp($url, '//', 2) === 0) {
            $rest = (string) substr($url, 2);
            $netloc = substr($rest, 0, strcspn($rest, '/?#'));
            $open = strpos($netloc, '[') !== false;
            $close = strpos($netloc, ']') !== false;
            if ($open !== $close) {
                return null; // ValueError("Invalid IPv6 URL")
            }
        }
        if ($scheme === '' || $netloc === '') {
            return null;
        }
        return $scheme . '://' . self::lowerLatin($netloc);
    }

    /** config.normalize_origin. */
    public static function normalizeOrigin($origin)
    {
        $o = rtrim(self::lowerLatin(self::pyStrip($origin)), '/');
        if (strncmp($o, 'https://', 8) === 0 && substr($o, -4) === ':443') {
            $o = substr($o, 0, -4);
        } elseif (strncmp($o, 'http://', 7) === 0 && substr($o, -3) === ':80') {
            $o = substr($o, 0, -3);
        }
        return $o;
    }

    /** SiteConfig.origin_allowed. $origins are already normalised (resolved config). */
    public static function originAllowed(array $origins, $origin)
    {
        if (!$origins) {
            return true;
        }
        if ($origin === null || $origin === '') {
            return false;
        }
        $o = self::normalizeOrigin($origin);
        foreach ($origins as $pattern) {
            $pattern = (string) $pattern;
            if ($pattern === '*' || $pattern === $o) {
                return true;
            }
            if (strpos($pattern, '*') !== false && self::fnmatch($o, $pattern)) {
                return true;
            }
        }
        return false;
    }

    // -----------------------------------------------------------------------
    // fnmatch.fnmatchcase
    // -----------------------------------------------------------------------

    public static function fnmatch($name, $pattern)
    {
        if (!isset(self::$globCache[$pattern])) {
            self::$globCache[$pattern] = self::translateGlob($pattern);
        }
        return preg_match(self::$globCache[$pattern], $name) === 1;
    }

    /** A port of fnmatch.translate (Python 3.10) producing a PCRE pattern. */
    public static function translateGlob($pat)
    {
        $chars = self::isUtf8($pat) ? preg_split('//u', $pat, -1, PREG_SPLIT_NO_EMPTY) : str_split($pat);
        if ($chars === false) {
            $chars = array();
        }
        $n = count($chars);
        $star = new \stdClass();
        $res = array();
        $i = 0;
        while ($i < $n) {
            $c = $chars[$i];
            $i++;
            if ($c === '*') {
                if (!$res || end($res) !== $star) {
                    $res[] = $star;
                }
            } elseif ($c === '?') {
                $res[] = '.';
            } elseif ($c === '[') {
                $j = $i;
                if ($j < $n && $chars[$j] === '!') {
                    $j++;
                }
                if ($j < $n && $chars[$j] === ']') {
                    $j++;
                }
                while ($j < $n && $chars[$j] !== ']') {
                    $j++;
                }
                if ($j >= $n) {
                    $res[] = '\\[';
                } else {
                    $stuffChars = array_slice($chars, $i, $j - $i);
                    if (!in_array('-', $stuffChars, true)) {
                        $stuff = self::classEscape(implode('', $stuffChars), false);
                    } else {
                        $chunks = array();
                        $k = ($chars[$i] === '!') ? $i + 2 : $i + 1;
                        while (true) {
                            $k = self::findChar($chars, '-', $k, $j);
                            if ($k < 0) {
                                break;
                            }
                            $chunks[] = array_slice($chars, $i, $k - $i);
                            $i = $k + 1;
                            $k = $k + 3;
                        }
                        $chunk = array_slice($chars, $i, $j - $i);
                        if ($chunk) {
                            $chunks[] = $chunk;
                        } else {
                            $chunks[count($chunks) - 1][] = '-';
                        }
                        // Remove empty ranges -- invalid in RE.
                        for ($k = count($chunks) - 1; $k > 0; $k--) {
                            $prev = $chunks[$k - 1];
                            $cur = $chunks[$k];
                            if ($prev && $cur && self::codePoint($prev[count($prev) - 1]) > self::codePoint($cur[0])) {
                                $chunks[$k - 1] = array_merge(array_slice($prev, 0, -1), array_slice($cur, 1));
                                array_splice($chunks, $k, 1);
                            }
                        }
                        $parts = array();
                        foreach ($chunks as $chunk) {
                            $parts[] = self::classEscape(implode('', $chunk), true);
                        }
                        $stuff = implode('-', $parts);
                    }
                    $i = $j + 1;
                    if ($stuff === '') {
                        $res[] = '(?!)';
                    } elseif ($stuff === '!') {
                        $res[] = '.';
                    } else {
                        if ($stuff[0] === '!') {
                            $stuff = '^' . substr($stuff, 1);
                        } elseif ($stuff[0] === '^' || $stuff[0] === '[') {
                            $stuff = '\\' . $stuff;
                        }
                        $res[] = '[' . $stuff . ']';
                    }
                }
            } else {
                $res[] = preg_quote($c, '/');
            }
        }
        // Stars: ".*" at the end; elsewhere a minimal match that never backtracks (as Python does).
        $out = '';
        $i = 0;
        $n = count($res);
        while ($i < $n && $res[$i] !== $star) {
            $out .= $res[$i];
            $i++;
        }
        while ($i < $n) {
            $i++; // the star
            if ($i === $n) {
                $out .= '.*';
                break;
            }
            $fixed = '';
            while ($i < $n && $res[$i] !== $star) {
                $fixed .= $res[$i];
                $i++;
            }
            if ($i === $n) {
                $out .= '.*' . $fixed;
            } else {
                $out .= '(?>.*?' . $fixed . ')';
            }
        }
        return '/\A(?:' . $out . ')\z/s' . (self::isUtf8($pat) ? 'u' : '');
    }

    /** Escape a character-class body the way fnmatch.translate does, made safe for PCRE. */
    private static function classEscape($s, $escapeHyphen)
    {
        $s = str_replace('\\', '\\\\', $s);
        if ($escapeHyphen) {
            $s = str_replace('-', '\\-', $s);
        }
        // Python escapes set operators; PCRE also needs "[" (POSIX classes) and the delimiter escaped.
        return preg_replace('/([&~|\[\/])/', '\\\\$1', $s);
    }

    private static function findChar(array $chars, $needle, $from, $to)
    {
        for ($k = $from; $k < $to; $k++) {
            if ($chars[$k] === $needle) {
                return $k;
            }
        }
        return -1;
    }

    // -----------------------------------------------------------------------
    // gzip
    // -----------------------------------------------------------------------

    /**
     * collect.gunzip_bounded: decompress one gzip member, refusing to produce more than
     * $maxOut bytes. Returns array(data, null) or array(null, error message), with the same
     * messages as Python's zlib (except that deflate-stream errors all read "data error").
     */
    public static function gunzipBounded($body, $maxOut)
    {
        $zerr = 'Error -3 while decompressing data: ';
        if (!function_exists('inflate_init')) {
            return array(null, 'zlib extension unavailable');
        }
        $n = strlen($body);
        // Header (RFC 1952), checked in zlib's order.
        if ($n < 2) {
            return array(null, 'truncated gzip data');
        }
        if ($body[0] !== "\x1f" || $body[1] !== "\x8b") {
            return array(null, $zerr . 'incorrect header check');
        }
        if ($n < 4) {
            return array(null, 'truncated gzip data');
        }
        if (ord($body[2]) !== 8) {
            return array(null, $zerr . 'unknown compression method');
        }
        $flags = ord($body[3]);
        if ($flags & 0xE0) {
            return array(null, $zerr . 'unknown header flags set');
        }
        $pos = 10; // ID1 ID2 CM FLG MTIME(4) XFL OS
        if ($n < $pos) {
            return array(null, 'truncated gzip data');
        }
        if ($flags & 0x04) { // FEXTRA
            if ($n < $pos + 2) {
                return array(null, 'truncated gzip data');
            }
            $xlen = ord($body[$pos]) | (ord($body[$pos + 1]) << 8);
            $pos += 2 + $xlen;
            if ($n < $pos) {
                return array(null, 'truncated gzip data');
            }
        }
        foreach (array(0x08, 0x10) as $flag) { // FNAME, FCOMMENT: zero-terminated
            if ($flags & $flag) {
                $end = strpos($body, "\0", $pos);
                if ($end === false) {
                    return array(null, 'truncated gzip data');
                }
                $pos = $end + 1;
            }
        }
        if ($flags & 0x02) { // FHCRC
            if ($n < $pos + 2) {
                return array(null, 'truncated gzip data');
            }
            $want = ord($body[$pos]) | (ord($body[$pos + 1]) << 8);
            if ((crc32(substr($body, 0, $pos)) & 0xFFFF) !== $want) {
                return array(null, $zerr . 'header crc mismatch');
            }
            $pos += 2;
        }
        // Raw deflate data, fed in small slices so a "zip bomb" cannot allocate much beyond the limit.
        $ctx = inflate_init(ZLIB_ENCODING_RAW);
        if ($ctx === false) {
            return array(null, 'zlib inflate_init failed');
        }
        $out = '';
        $start = $pos;
        $ended = false;
        while ($pos < $n) {
            $piece = substr($body, $pos, 1024);
            $chunk = @inflate_add($ctx, $piece, ZLIB_SYNC_FLUSH);
            if ($chunk === false) {
                return array(null, $zerr . 'data error');
            }
            $out .= $chunk;
            if (strlen($out) > $maxOut) {
                return array(null, sprintf('decompressed body exceeds %d bytes', $maxOut));
            }
            $pos += strlen($piece);
            if (inflate_get_status($ctx) === ZLIB_STREAM_END) {
                $ended = true;
                break;
            }
        }
        if (!$ended) {
            return array(null, 'truncated gzip data');
        }
        // Trailer: CRC32 then ISIZE, little-endian.
        $t = $start + inflate_get_read_len($ctx);
        if ($n - $t < 4) {
            return array(null, 'truncated gzip data');
        }
        $crc = unpack('V', substr($body, $t, 4));
        if (($crc[1] & 0xFFFFFFFF) !== (crc32($out) & 0xFFFFFFFF)) {
            return array(null, $zerr . 'incorrect data check');
        }
        if ($n - $t < 8) {
            return array(null, 'truncated gzip data');
        }
        $size = unpack('V', substr($body, $t + 4, 4));
        if (($size[1] & 0xFFFFFFFF) !== (strlen($out) & 0xFFFFFFFF)) {
            return array(null, $zerr . 'incorrect length check');
        }
        return array($out, null);
    }

    /**
     * gzip.compress(data, compresslevel=6) as Python writes it: no file name, current mtime,
     * XFL 0, OS 255, deflate at level 6 with zlib's default memory level.
     * Returns null when zlib is unavailable.
     */
    public static function gzipCompress($data)
    {
        if (function_exists('deflate_init')) {
            $ctx = deflate_init(ZLIB_ENCODING_RAW, array('level' => 6, 'memory' => 8, 'window' => 15));
            if ($ctx !== false) {
                $deflated = deflate_add($ctx, $data, ZLIB_FINISH);
                if ($deflated !== false) {
                    return "\x1f\x8b\x08\x00" . pack('V', time()) . "\x00\xff" . $deflated
                        . pack('V', crc32($data) & 0xFFFFFFFF) . pack('V', strlen($data) & 0xFFFFFFFF);
                }
            }
        }
        if (function_exists('gzencode')) {
            $out = gzencode($data, 6);
            return $out === false ? null : $out;
        }
        return null;
    }
}

/**
 * Errors of the request path with a budget, as collect.ErrorLog: full details for the first 5 errors of
 * each minute, then only a count, logged when the first error of a later minute arrives. A flood of failing
 * requests on shared hosting must not fill the account's disk with log lines.
 *
 * PHP keeps nothing between requests (under CGI each request is a new process), so the state lives in a
 * tiny file in the data directory, ".error-log" ("<minute> <count>"), read and updated under an exclusive
 * lock. When the file cannot be used, every error is logged, as without a budget.
 */
final class ErrorLog
{
    const PER_MINUTE = 5;
    const FILE = '.error-log';

    public static function exception($dataDir, $message, $e)
    {
        if (self::admit($dataDir)) {
            Log::exception($message, $e);
        }
    }

    public static function error($dataDir, $message)
    {
        if (self::admit($dataDir)) {
            Log::error($message);
        }
    }

    /** A warning that a flood of requests would repeat (Python logs those once per process): the same budget. */
    public static function warning($dataDir, $message)
    {
        if (self::admit($dataDir)) {
            Log::warning($message);
        }
    }

    /** Whether to log this error in full; first logs how many went unlogged in the minute of the last ones. */
    private static function admit($dataDir)
    {
        if ($dataDir === null || $dataDir === '' || !@is_dir($dataDir)) {
            return true;
        }
        if (!function_exists('flock')) {
            return true;
        }
        $fh = @fopen(rtrim($dataDir, '/\\') . DIRECTORY_SEPARATOR . self::FILE, 'c+');
        if ($fh === false) {
            return true;
        }
        $unlogged = 0;
        try {
            if (!@flock($fh, LOCK_EX)) {
                return true;
            }
            $state = explode(' ', trim((string) @stream_get_contents($fh)), 2);
            $minute = intdiv(time(), 60);
            $last = (int) $state[0];
            $count = isset($state[1]) ? (int) $state[1] : 0;
            if ($last !== $minute) {
                if ($count > self::PER_MINUTE) {
                    $unlogged = $count - self::PER_MINUTE;
                }
                $count = 0;
            }
            $count++;
            @ftruncate($fh, 0);
            @rewind($fh);
            @fwrite($fh, $minute . ' ' . $count);
            @fflush($fh);
            @flock($fh, LOCK_UN);
        } finally {
            @fclose($fh);
        }
        if ($unlogged > 0) {
            Log::error(sprintf('%d more errors in the previous minute (not logged)', $unlogged));
        }
        return $count <= self::PER_MINUTE;
    }
}

/** Error log in the format of the Python collector's logging setup. */
final class Log
{
    /** @var string|null */
    public static $file = null;

    public static function error($message)
    {
        self::write('ERROR', $message);
    }

    public static function warning($message)
    {
        self::write('WARNING', $message);
    }

    public static function exception($message, $e)
    {
        self::write('ERROR', $message . ': ' . get_class($e) . ': ' . $e->getMessage()
            . ' (' . basename($e->getFile()) . ':' . $e->getLine() . ')');
    }

    private static function write($level, $message)
    {
        $now = microtime(true);
        $line = gmdate('Y-m-d H:i:s', (int) $now) . sprintf(',%03d', (int) (($now - floor($now)) * 1000))
            . ' tidesight.php ' . $level . ' ' . str_replace(array("\r", "\n"), ' ', $message);
        if (self::$file) {
            if (@file_put_contents(self::$file, $line . "\n", FILE_APPEND | LOCK_EX) !== false) {
                return;
            }
        }
        if (function_exists('error_log')) { // disabled on some hosts: the line is then lost, the request is not
            @error_log($line);
        }
    }
}
