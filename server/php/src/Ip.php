<?php
/**
 * IP address anonymisation, identical to server/python/tidesight/anonymize.py: the same
 * address syntax (Python's ipaddress module: strict IPv4 without leading zeros, IPv6 with
 * one "::" and an optional IPv4 tail), the same /24 and /48 truncation, the same text form
 * of the result, and the same keyed hash ('h:' + 16 hex chars of HMAC-SHA256).
 */

namespace Tidesight;

final class Ip
{
    const HASH_PREFIX = 'h:';

    /** Request headers that can carry client IP addresses (anonymize.IP_HEADERS, same order). */
    const HEADERS = array(
        'x-forwarded-for', 'x-real-ip', 'forwarded', 'cf-connecting-ip', 'true-client-ip',
        'x-client-ip', 'x-cluster-client-ip', 'fastly-client-ip', 'x-original-forwarded-for',
        'x-forwarded', 'forwarded-for', 'x-appengine-user-ip', 'cf-connecting-ipv6',
    );

    /** Candidate IPv4/IPv6 tokens inside header values (anonymize._IP_TOKEN). */
    const TOKEN = '/\[?([0-9A-Fa-f:.]{2,45}(?:%[0-9A-Za-z]+)?)\]?/';

    /** IPv4 as 4 octets, or null (ipaddress.IPv4Address rules). */
    public static function parseV4($s)
    {
        if ($s === '' || strpos($s, '/') !== false) {
            return null;
        }
        $octets = explode('.', $s);
        if (count($octets) !== 4) {
            return null;
        }
        $out = array();
        foreach ($octets as $o) {
            if ($o === '' || preg_match('/\A[0-9]+\z/', $o) !== 1 || strlen($o) > 3) {
                return null;
            }
            if ($o !== '0' && $o[0] === '0') {
                return null; // leading zeros are not permitted
            }
            $v = (int) $o;
            if ($v > 255) {
                return null;
            }
            $out[] = $v;
        }
        return $out;
    }

    /** IPv6 as 8 hextets, or null (ipaddress.IPv6Address._ip_int_from_string rules). */
    public static function parseV6($s)
    {
        if ($s === '' || strpos($s, '/') !== false) {
            return null;
        }
        $pct = strpos($s, '%');
        if ($pct !== false) { // scope id: "addr%zone", the zone must be non-empty and contain no '%'
            $zone = (string) substr($s, $pct + 1);
            if ($zone === '' || strpos($zone, '%') !== false) {
                return null;
            }
            $s = substr($s, 0, $pct);
            if ($s === '') {
                return null;
            }
        }
        $parts = explode(':', $s);
        if (count($parts) < 3) {
            return null;
        }
        $last = $parts[count($parts) - 1];
        if (strpos($last, '.') !== false) {
            $v4 = self::parseV4(array_pop($parts));
            if ($v4 === null) {
                return null;
            }
            $parts[] = dechex(($v4[0] << 8) | $v4[1]);
            $parts[] = dechex(($v4[2] << 8) | $v4[3]);
        }
        $count = count($parts);
        if ($count > 9) {
            return null;
        }
        $skip = null;
        for ($i = 1; $i < $count - 1; $i++) {
            if ($parts[$i] === '') {
                if ($skip !== null) {
                    return null; // at most one '::'
                }
                $skip = $i;
            }
        }
        if ($skip !== null) {
            $hi = $skip;
            $lo = $count - $skip - 1;
            if ($parts[0] === '') {
                $hi--;
                if ($hi) {
                    return null;
                }
            }
            if ($parts[$count - 1] === '') {
                $lo--;
                if ($lo) {
                    return null;
                }
            }
            $skipped = 8 - ($hi + $lo);
            if ($skipped < 1) {
                return null;
            }
        } else {
            if ($count !== 8 || $parts[0] === '' || $parts[$count - 1] === '') {
                return null;
            }
            $hi = $count;
            $lo = 0;
            $skipped = 0;
        }
        $out = array();
        for ($i = 0; $i < $hi; $i++) {
            $h = self::hextet($parts[$i]);
            if ($h === null) {
                return null;
            }
            $out[] = $h;
        }
        for ($i = 0; $i < $skipped; $i++) {
            $out[] = 0;
        }
        for ($i = $count - $lo; $i < $count; $i++) {
            $h = self::hextet($parts[$i]);
            if ($h === null) {
                return null;
            }
            $out[] = $h;
        }
        return $out;
    }

    private static function hextet($s)
    {
        if ($s === '' || strlen($s) > 4 || preg_match('/\A[0-9A-Fa-f]+\z/', $s) !== 1) {
            return null;
        }
        return (int) hexdec($s);
    }

    /** ipaddress.ip_address(): array('v' => 4|6, 'a' => octets|hextets) or null. */
    public static function parse($s)
    {
        $v4 = self::parseV4($s);
        if ($v4 !== null) {
            return array('v' => 4, 'a' => $v4);
        }
        $v6 = self::parseV6($s);
        return $v6 === null ? null : array('v' => 6, 'a' => $v6);
    }

    /** str(IPv6Address): lower-case hextets, longest run of zeros (2+) compressed. */
    public static function formatV6(array $h)
    {
        $hex = array();
        foreach ($h as $x) {
            $hex[] = dechex($x);
        }
        $bestStart = -1;
        $bestLen = 0;
        $start = -1;
        $len = 0;
        foreach ($hex as $i => $x) {
            if ($x === '0') {
                $len++;
                if ($start === -1) {
                    $start = $i;
                }
                if ($len > $bestLen) {
                    $bestLen = $len;
                    $bestStart = $start;
                }
            } else {
                $len = 0;
                $start = -1;
            }
        }
        if ($bestLen > 1) {
            $end = $bestStart + $bestLen;
            if ($end === count($hex)) {
                $hex[] = '';
            }
            array_splice($hex, $bestStart, $bestLen, array(''));
            if ($bestStart === 0) {
                array_unshift($hex, '');
            }
        }
        return implode(':', $hex);
    }

    /** anonymize.truncate_ip: the /24 of an IPv4 address or the /48 of an IPv6 address. */
    public static function truncate($ip)
    {
        $parts = explode('%', $ip, 2);
        $addr = self::parse($parts[0]);
        if ($addr === null) {
            return $ip;
        }
        $a = $addr['a'];
        if ($addr['v'] === 6 && $a[0] === 0 && $a[1] === 0 && $a[2] === 0 && $a[3] === 0 && $a[4] === 0
            && $a[5] === 0xFFFF) {
            $addr = array('v' => 4, 'a' => array($a[6] >> 8, $a[6] & 0xFF, $a[7] >> 8, $a[7] & 0xFF));
            $a = $addr['a'];
        }
        if ($addr['v'] === 4) {
            return $a[0] . '.' . $a[1] . '.' . $a[2] . '.0';
        }
        return self::formatV6(array($a[0], $a[1], $a[2], 0, 0, 0, 0, 0));
    }

    /** anonymize.hash_ip. */
    public static function hash($ip, $secret)
    {
        if (strncmp($ip, self::HASH_PREFIX, 2) === 0) {
            return $ip;
        }
        return self::HASH_PREFIX . substr(hash_hmac('sha256', $ip, $secret), 0, 16);
    }

    /** anonymize.anonymize_ip: truncate first, then hash. */
    public static function anonymize($ip, $doHash, $doTruncate, $secret)
    {
        if ($ip === null || $ip === '') {
            return $ip;
        }
        $out = $ip;
        if ($doTruncate) {
            $out = self::truncate($out);
        }
        if ($doHash) {
            $out = self::hash($out, $secret);
        }
        return $out;
    }

    private static function isIp($token)
    {
        $parts = explode('%', $token, 2);
        return self::parse($parts[0]) !== null;
    }

    /** anonymize.anonymize_header_value: replace every IP address inside a header value. */
    public static function anonymizeHeaderValue($value, $doHash, $doTruncate, $secret)
    {
        $out = preg_replace_callback(self::TOKEN, function ($m) use ($doHash, $doTruncate, $secret) {
            $token = $m[1];
            $candidate = $token;
            $port = '';
            if (substr_count($candidate, ':') === 1 && strpos($candidate, '.') !== false) {
                list($candidate, $port) = explode(':', $candidate, 2);
                $port = ':' . $port;
            }
            if (!Ip::isIpPublic($candidate)) {
                return $m[0];
            }
            $new = Ip::anonymize($candidate, $doHash, $doTruncate, $secret);
            if ($new === null || $new === '') {
                $new = $candidate;
            }
            return str_replace($token, $new . $port, $m[0]);
        }, $value);
        return $out === null ? $value : $out;
    }

    /** @internal for the callback above (PHP 7.4 closures cannot call private statics by name). */
    public static function isIpPublic($token)
    {
        return self::isIp($token);
    }

    /** anonymize.anonymize_headers: a copy with addresses in IP-bearing headers anonymised. */
    public static function anonymizeHeaders(array $headers, $doHash, $doTruncate, $secret)
    {
        if (!$doHash && !$doTruncate) {
            return $headers;
        }
        foreach (self::HEADERS as $name) {
            if (isset($headers[$name]) && is_string($headers[$name])) {
                $headers[$name] = self::anonymizeHeaderValue($headers[$name], $doHash, $doTruncate, $secret);
            }
        }
        return $headers;
    }
}
