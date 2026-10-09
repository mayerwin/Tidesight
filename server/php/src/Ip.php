<?php
/**
 * IP address anonymisation, identical to server/python/tidesight/anonymize.py: the same
 * address syntax (Python's ipaddress module: strict IPv4 without leading zeros, IPv6 with
 * one "::" and an optional IPv4 tail), the same /24 and /48 truncation, the same text form
 * of the result, and the same keyed hash ('h:' + 16 hex chars of HMAC-SHA256). Also the
 * rate-limit client key of collect.client_key.
 */

namespace Tidesight;

final class Ip
{
    const HASH_PREFIX = 'h:';

    /** Request headers that can carry client IP addresses (anonymize.IP_HEADERS, same order). */
    const HEADERS = array(
        'x-forwarded-for', 'x-real-ip', 'forwarded', 'cf-connecting-ip', 'true-client-ip',
        'x-client-ip', 'x-cluster-client-ip', 'fastly-client-ip', 'x-original-forwarded-for',
        'x-forwarded', 'forwarded-for', 'x-appengine-user-ip', 'cf-connecting-ipv6', 'via',
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

    /** collect._V4_PORT_RE and _V6_PORT_RE: an address with a port ("1.2.3.4:5678", "[2001:db8::1]:443", "[::1]"). */
    const V4_PORT = '/\A([0-9]{1,3}(?:\.[0-9]{1,3}){3}):[0-9]{1,5}\z/';
    const V6_PORT = '/\A\[([0-9A-Fa-f:.]+)\](?::[0-9]{1,5})?\z/';

    /** anonymize.IP_HEADER_PARTS: a header whose name contains one of these may carry addresses. */
    const HEADER_PARTS = array('ip', 'forwarded', 'client', 'addr');

    /** collect._address: the IP address in a value, a port ignored, as parse() gives it; null if it is none. */
    public static function address($value)
    {
        $v = Util::pyStrip((string) $value);
        if (preg_match(self::V6_PORT, $v, $m) === 1 || preg_match(self::V4_PORT, $v, $m) === 1) {
            $v = $m[1];
        }
        return self::parse($v);
    }

    /**
     * enrich.ip.in_networks($ip, networks($cidrs)): whether an address (surrounding quotes, brackets, a
     * port or a zone allowed; an IPv4-mapped address taken as its IPv4) lies in one of the networks,
     * each read as ipaddress.ip_network(cidr, strict=False). Invalid entries are skipped.
     */
    public static function inNetworks($ip, $cidrs)
    {
        $addr = self::parseLoose($ip);
        if ($addr === null || !is_array($cidrs)) {
            return false;
        }
        foreach ($cidrs as $cidr) {
            $net = is_string($cidr) ? self::network($cidr) : null;
            if ($net !== null && $net['v'] === $addr['v'] && self::sameNetwork($addr['a'], $net['a'], $net['p'])) {
                return true;
            }
        }
        return false;
    }

    /** enrich.ip._parse. */
    private static function parseLoose($ip)
    {
        if ($ip === null || $ip === '') {
            return null;
        }
        $ip = trim(Util::pyStrip((string) $ip), '"');
        $close = strpos($ip, ']');
        if (strncmp($ip, '[', 1) === 0 && $close !== false) { // [v6]:port
            $ip = (string) substr($ip, 1, $close - 1);
        } elseif (substr_count($ip, ':') === 1 && strpos($ip, '.') !== false) { // v4:port
            $ip = (string) strstr($ip, ':', true);
        }
        $parts = explode('%', $ip, 2);
        $addr = self::parse($parts[0]);
        if ($addr !== null && $addr['v'] === 6) {
            $a = $addr['a'];
            if ($a[0] === 0 && $a[1] === 0 && $a[2] === 0 && $a[3] === 0 && $a[4] === 0 && $a[5] === 0xFFFF) {
                return array('v' => 4, 'a' => array($a[6] >> 8, $a[6] & 0xFF, $a[7] >> 8, $a[7] & 0xFF));
            }
        }
        return $addr;
    }

    /** ipaddress.ip_network($cidr.strip(), strict=False): array('v', 'a' => parts, 'p' => prefix length) or null. */
    private static function network($cidr)
    {
        $parts = explode('/', Util::pyStrip($cidr));
        if (count($parts) > 2) {
            return null;
        }
        $v4 = self::parseV4($parts[0]);
        if ($v4 !== null) {
            $p = count($parts) === 1 ? 32 : self::prefixLength($parts[1], 32);
            if ($p === null) { // IPv4 also takes a netmask or hostmask: 255.255.255.0, 0.0.0.255
                $mask = self::parseV4($parts[1]);
                if ($mask !== null) {
                    $n = ($mask[0] << 24) | ($mask[1] << 16) | ($mask[2] << 8) | $mask[3];
                    $p = self::contiguous($n);
                    if ($p === null) {
                        $p = self::contiguous($n ^ 0xFFFFFFFF);
                    }
                }
            }
            return $p === null ? null : array('v' => 4, 'a' => $v4, 'p' => $p);
        }
        $v6 = self::parseV6($parts[0]);
        if ($v6 !== null) {
            $p = count($parts) === 1 ? 128 : self::prefixLength($parts[1], 128);
            return $p === null ? null : array('v' => 6, 'a' => $v6, 'p' => $p);
        }
        return null;
    }

    /** A prefix length written in ASCII digits, 0..$max (leading zeros allowed), else null. */
    private static function prefixLength($s, $max)
    {
        if (preg_match('/\A[0-9]+\z/', $s) !== 1) {
            return null;
        }
        $p = (int) $s;
        return $p <= $max ? $p : null;
    }

    /** The prefix length of a 32-bit netmask made of leading ones then zeros, else null. */
    private static function contiguous($n)
    {
        for ($p = 0; $p <= 32; $p++) {
            if ($n === ((0xFFFFFFFF << (32 - $p)) & 0xFFFFFFFF)) {
                return $p;
            }
        }
        return null;
    }

    /** Whether two addresses (octets or hextets) share their first $prefix bits. */
    private static function sameNetwork(array $a, array $b, $prefix)
    {
        $bits = count($a) === 4 ? 8 : 16;
        $all = (1 << $bits) - 1;
        foreach ($a as $i => $x) {
            $keep = $prefix - $bits * $i;
            if ($keep <= 0) {
                break;
            }
            $mask = $keep >= $bits ? $all : ($all << ($bits - $keep)) & $all;
            if (($x & $mask) !== ($b[$i] & $mask)) {
                return false;
            }
        }
        return true;
    }

    /**
     * anonymize.ip_bearing: whether a header (lower-case name) may carry client addresses: the known
     * ones, and any name containing "ip", "forwarded", "client" or "address".
     */
    public static function ipBearing($name)
    {
        if (in_array($name, self::HEADERS, true)) {
            return true;
        }
        foreach (self::HEADER_PARTS as $part) {
            if (strpos($name, $part) !== false) {
                return true;
            }
        }
        return false;
    }

    /**
     * collect.client_key: the rate-limit identity of a client. A port is ignored. An IPv4 address as is
     * (an IPv4-mapped IPv6 address as its IPv4), another IPv6 address as its network for $ipv6Prefix
     * (clamped to 16..128), written as str(ipaddress.IPv6Network((int(addr), prefix), strict=False)),
     * e.g. "2001:db8:1:2::/64": one host can use a whole /64. Anything else (a custom key header) is
     * kept as its first 64 characters. Null for an empty value.
     *
     * Values are ISO-8859-1 text (as UTF-8), where Python's \d matches only 0-9.
     */
    public static function clientKey($value, $ipv6Prefix = 64)
    {
        if ($value === null || $value === '') {
            return null;
        }
        $addr = self::address($value);
        if ($addr === null) {
            return Util::cut($value, 64);
        }
        $a = $addr['a'];
        if ($addr['v'] === 4) {
            return implode('.', $a);
        }
        if ($a[0] === 0 && $a[1] === 0 && $a[2] === 0 && $a[3] === 0 && $a[4] === 0 && $a[5] === 0xFFFF) {
            return ($a[6] >> 8) . '.' . ($a[6] & 0xFF) . '.' . ($a[7] >> 8) . '.' . ($a[7] & 0xFF);
        }
        $prefix = max(16, min(128, (int) $ipv6Prefix));
        $network = array();
        foreach ($a as $i => $hextet) {
            $bits = $prefix - 16 * $i; // bits of this hextet inside the prefix
            if ($bits >= 16) {
                $network[] = $hextet;
            } elseif ($bits <= 0) {
                $network[] = 0;
            } else {
                $network[] = $hextet & ((0xFFFF << (16 - $bits)) & 0xFFFF);
            }
        }
        return self::formatV6($network) . '/' . $prefix;
    }

    /**
     * anonymize.truncate_ip: the /24 of an IPv4 address or the /48 of an IPv6 address (the /40 of a 6to4
     * address, 2002::/16, whose next 32 bits are an IPv4 address: its /24, as for IPv4).
     */
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
        if ($a[0] === 0x2002) { // 6to4 (IPv6Address.sixtofour)
            return self::formatV6(array($a[0], $a[1], $a[2] & 0xFF00, 0, 0, 0, 0, 0));
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
        foreach ($headers as $name => $value) {
            if (is_string($value) && self::ipBearing((string) $name)) {
                $headers[$name] = self::anonymizeHeaderValue($value, $doHash, $doTruncate, $secret);
            }
        }
        return $headers;
    }
}
