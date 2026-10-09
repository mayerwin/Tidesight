<?php
/**
 * URL redaction, identical to server/python/tidesight/redact.py (docs/connectors.md, "URL redaction"):
 * secrets in URLs (login links, reset tokens, signed URLs…) become "redacted". The collector applies it
 * to the Referer header it stores, which a browser may fill with the full page URL. Linear time in the URL
 * (parameter values are looked into at most MAX_NESTING levels deep): no regular expression is built from
 * the patterns.
 *
 * Strings are UTF-8 (header values read as ISO-8859-1, as Python sees them). Every delimiter is ASCII and
 * UTF-8 is self-synchronising, so byte positions here correspond to Python's character positions; only the
 * name-length limit counts characters.
 */

namespace Tidesight;

final class Redact
{
    const REDACTED = 'redacted';
    const MAX_NAME = 256;
    /** How deep parameter values are looked into (a URL in a URL in a URL). */
    const MAX_NESTING = 2;
    const SCHEMES = array('http', 'https', 'ws', 'wss');

    /** @var array<string,string[]> each pattern, folded and split on "*" (bounded: patterns come from the config) */
    private static $pieces = array();

    /** redact.fold: ASCII lower case (other characters unchanged, so positions stay the same). */
    public static function fold($s)
    {
        return strtr($s, 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz');
    }

    /**
     * redact.glob_spans: where each `*` of the glob matched, as array(start, end) byte positions in
     * $text, when the glob (only `*` is special; ASCII case-insensitive) matches the whole text; else null.
     * The pieces between stars are taken at their first occurrence, left to right: linear in the text.
     */
    public static function globSpans($pattern, $text)
    {
        if (!isset(self::$pieces[$pattern])) {
            if (count(self::$pieces) > 1000) {
                self::$pieces = array();
            }
            self::$pieces[$pattern] = explode('*', self::fold($pattern));
        }
        $parts = self::$pieces[$pattern];
        $text = self::fold($text);
        $n = count($parts);
        if ($n === 1) {
            return $parts[0] === $text ? array() : null;
        }
        $head = $parts[0];
        $tail = $parts[$n - 1];
        $end = strlen($text) - strlen($tail);
        if ($end < strlen($head) || strncmp($text, $head, strlen($head)) !== 0
            || ($tail !== '' && substr($text, -strlen($tail)) !== $tail)) {
            return null;
        }
        $spans = array();
        $pos = strlen($head);
        for ($i = 1; $i < $n - 1; $i++) {
            $piece = $parts[$i];
            if ($piece === '') {
                $at = $pos; // str.find of an empty string: where the search starts
            } else {
                // text.find(piece, pos, end): the leftmost occurrence from pos is the only one that can end by end.
                $at = strpos($text, $piece, $pos);
                if ($at === false || $at + strlen($piece) > $end) {
                    return null;
                }
            }
            $spans[] = array($pos, $at);
            $pos = $at + strlen($piece);
        }
        $spans[] = array($pos, $end);
        return $spans;
    }

    public static function globMatch($pattern, $text)
    {
        return self::globSpans($pattern, $text) !== null;
    }

    /** redact.patterns: the strings of a configured pattern list (anything else is ignored). */
    public static function patterns($value)
    {
        $out = array();
        if (is_array($value)) {
            foreach ($value as $p) {
                if (is_string($p)) {
                    $out[] = $p;
                }
            }
        }
        return $out;
    }

    /**
     * redact.decode_name: a parameter name (or value) as decodeURIComponent(name.replace(/\+/g, " "))
     * reads it, or unchanged when that would fail (a `%` without two hex digits after it, or a run of
     * escapes that is not UTF-8 by itself).
     */
    public static function decodeName($name)
    {
        $plus = str_replace('+', ' ', $name);
        if (strpos($plus, '%') === false) {
            return $plus;
        }
        $out = '';
        $i = 0;
        $n = strlen($plus);
        while ($i < $n) {
            if ($plus[$i] !== '%') {
                $j = strpos($plus, '%', $i);
                $j = $j === false ? $n : $j;
                $out .= substr($plus, $i, $j - $i);
                $i = $j;
                continue;
            }
            $run = '';
            while ($i < $n && $plus[$i] === '%') {
                $pair = (string) substr($plus, $i + 1, 2);
                if (strlen($pair) < 2 || strspn($pair, '0123456789abcdefABCDEF') !== 2) {
                    return $name;
                }
                $run .= chr((int) hexdec($pair));
                $i += 3;
            }
            if (preg_match('//u', $run) !== 1) { // Python's strict UTF-8: no surrogates, overlongs or > U+10FFFF
                return $name;
            }
            $out .= $run;
        }
        return $out;
    }

    /** redact._secret_inside: whether a parameter value, decoded, is a URL or a query these rules would change. */
    private static function secretInside($value, array $names, array $paths, $depth)
    {
        $inner = self::decodeName($value);
        if (self::redact($inner, $names, $paths, $depth) !== $inner) {
            return true;
        }
        return strpos($inner, '?') === false && strpos($inner, '#') === false && strpos($inner, '=') !== false
            && self::redactPairs($inner, '&', $names, $paths, $depth) !== $inner;
    }

    private static function redactPairs($s, $sep, array $names, array $paths, $depth)
    {
        $out = array();
        foreach (explode($sep, $s) as $pair) {
            $i = strpos($pair, '=');
            if ($i !== false && $i >= 1) {
                $raw = substr($pair, 0, $i);
                if ($names && (Util::length($raw) > self::MAX_NAME || self::anyMatch($names, self::decodeName($raw)))) {
                    $pair = substr($pair, 0, $i + 1) . self::REDACTED;
                } elseif ($depth < self::MAX_NESTING
                    && self::secretInside((string) substr($pair, $i + 1), $names, $paths, $depth + 1)) {
                    $pair = substr($pair, 0, $i + 1) . self::REDACTED;
                }
            }
            $out[] = $pair;
        }
        return implode($sep, $out);
    }

    private static function anyMatch(array $patterns, $text)
    {
        foreach ($patterns as $p) {
            if (self::globMatch($p, $text)) {
                return true;
            }
        }
        return false;
    }

    private static function redactPath($path, array $names, array $paths, $depth)
    {
        if (strpos($path, ';') !== false) {
            $segments = array();
            foreach (explode('/', $path) as $seg) {
                $i = strpos($seg, ';');
                $segments[] = $i === false ? $seg
                    : substr($seg, 0, $i + 1) . self::redactPairs((string) substr($seg, $i + 1), ';', $names, $paths, $depth);
            }
            $path = implode('/', $segments);
        }
        foreach ($paths as $pattern) {
            $spans = self::globSpans($pattern, $path);
            if ($spans !== null) {
                foreach (array_reverse($spans) as $span) {
                    if ($span[1] > $span[0]) {
                        $path = substr($path, 0, $span[0]) . self::REDACTED . substr($path, $span[1]);
                    }
                }
                break;
            }
        }
        return $path;
    }

    private static function redactFragment($frag, array $names, array $paths, $depth)
    {
        $q = strpos($frag, '?');
        $head = $q === false ? $frag : substr($frag, 0, $q);
        if (strncmp($head, '/', 1) === 0 || strncmp($head, '!/', 2) === 0) {
            $k = strncmp($head, '!', 1) === 0 ? 1 : 0;
            $head = substr($head, 0, $k) . self::redactPath((string) substr($head, $k), $names, $paths, $depth);
        } elseif ($q === false) {
            $eq = strpos($frag, '=');
            return $eq !== false && $eq > 0 ? self::redactPairs($frag, '&', $names, $paths, $depth) : $frag;
        }
        if ($q === false) {
            return $head;
        }
        return $head . '?' . self::redactPairs((string) substr($frag, $q + 1), '&', $names, $paths, $depth);
    }

    /** redact._redact. */
    private static function redact($url, array $names, array $paths, $depth)
    {
        $h = strpos($url, '#');
        $main = $h === false ? $url : substr($url, 0, $h);
        $q = strpos($main, '?');
        $base = $q === false ? $main : substr($main, 0, $q);
        $c = strpos($base, ':');
        $start = -1;
        if ($c !== false && $c > 0 && preg_match('/\A[A-Za-z][A-Za-z0-9+.\-]*\z/', substr($base, 0, $c)) === 1) {
            if (!in_array(self::fold(substr($base, 0, $c)), self::SCHEMES, true)) {
                return $url;
            }
            if (substr($base, $c, 3) === '://') {
                $start = $c + 3;
            }
        } elseif (strncmp($base, '//', 2) === 0) {
            $start = 2;
        }
        $origin = '';
        $path = $base;
        if ($start >= 0) {
            $j = strpos($base, '/', $start);
            if ($j === false) {
                $origin = $base;
                $path = '';
            } else {
                $origin = substr($base, 0, $j);
                $path = substr($base, $j);
            }
            $at = strrpos($origin, '@');
            if ($at !== false && $at > $start) {
                $origin = substr($origin, 0, $start) . self::REDACTED . substr($origin, $at);
            }
        }
        $out = $origin . self::redactPath($path, $names, $paths, $depth);
        if ($q !== false) {
            $out .= '?' . self::redactPairs((string) substr($main, $q + 1), '&', $names, $paths, $depth);
        }
        if ($h !== false) {
            $out .= '#' . self::redactFragment((string) substr($url, $h + 1), $names, $paths, $depth);
        }
        return $out;
    }

    /** redact.redact_url: the URL with the secrets the patterns describe replaced. */
    public static function url($url, array $names, array $paths)
    {
        if (!is_string($url) || $url === '' || (!$names && !$paths)) {
            return $url;
        }
        return self::redact($url, $names, $paths, 0);
    }
}
