<?php
/**
 * The Tidesight collector for PHP: the ingest endpoints of docs/protocol.md, mirroring
 * server/python/tidesight/collect.py handler by handler.
 *
 *   POST /collect      batch of events (JSON envelope, optionally gzip)
 *   POST /chunk        heavy data chunk (replay, mouse movement), stored unparsed
 *   GET  /p.gif        pixel hit
 *   GET  /config.js    site configuration for the tracker (also /config.json)
 *   GET  /health       liveness and storage check
 *   GET  /ts.js, /ts/* tracker files, when no web server serves them
 *
 * The request path validates, bounds and appends. It never enriches, never calls the
 * network, and stores malformed or unknown-site hits flagged instead of dropping them.
 */

namespace Tidesight;

/** The current HTTP request, seen the way a WSGI server would hand it to Python. */
final class Request
{
    /** @var array */
    public $server;
    /** @var string upper-case method */
    public $method;
    /** @var string route inside the collector, e.g. "/collect" */
    public $path;
    /** @var string request path as received (percent-decoded), for the "server" column */
    public $fullPath;
    /** @var string QUERY_STRING read as ISO-8859-1 (UTF-8 text) */
    public $query;

    public static function fromGlobals()
    {
        $r = new self();
        $s = $_SERVER;
        $r->server = $s;
        $method = isset($s['REQUEST_METHOD']) ? (string) $s['REQUEST_METHOD'] : 'GET';
        $r->method = Util::upperLatin1(Util::latin1($method)); // environ["REQUEST_METHOD"].upper(), stored as text
        $uri = isset($s['REQUEST_URI']) ? (string) $s['REQUEST_URI'] : '/';
        $q = strpos($uri, '?');
        $rawPath = $q === false ? $uri : substr($uri, 0, $q);
        if (preg_match('#\A[A-Za-z][A-Za-z0-9+.\-]*://[^/]*#', $rawPath, $m) === 1) { // absolute-form target
            $rawPath = (string) substr($rawPath, strlen($m[0]));
        }
        $decoded = rawurldecode($rawPath);
        $route = $decoded;
        if (PHP_SAPI !== 'cli-server') {
            // Apache/FastCGI: /base/collect rewritten to /base/index.php, or /base/index.php/collect.
            $pathInfo = isset($s['PATH_INFO']) ? (string) $s['PATH_INFO'] : '';
            if ($pathInfo !== '') {
                $route = $pathInfo;
            } else {
                $script = isset($s['SCRIPT_NAME']) ? str_replace('\\', '/', (string) $s['SCRIPT_NAME']) : '';
                $base = rtrim(str_replace('\\', '/', dirname($script)), '/');
                if ($script !== '' && $route === $script) {
                    $route = '/';
                } elseif ($base !== '' && strncmp($route, $base . '/', strlen($base) + 1) === 0) {
                    $route = (string) substr($route, strlen($base));
                }
            }
        }
        $r->path = $route === '' ? '/' : Util::latin1($route);
        $r->fullPath = Util::latin1($decoded);
        $r->query = Util::latin1(isset($s['QUERY_STRING']) ? (string) $s['QUERY_STRING'] : '');
        return $r;
    }

    /** A server variable as received (raw bytes), or null. */
    public function raw($key)
    {
        return isset($this->server[$key]) && is_scalar($this->server[$key]) ? (string) $this->server[$key] : null;
    }

    /** A server variable as Python's WSGI environ holds it (ISO-8859-1 text), or null. */
    public function text($key)
    {
        $v = $this->raw($key);
        return $v === null ? null : Util::latin1($v);
    }

    /** "https" or "http" (wsgiref's guess_scheme; REQUEST_SCHEME too). */
    public function scheme()
    {
        $https = $this->raw('HTTPS');
        if (in_array($https, array('yes', 'on', '1'), true) || $this->raw('REQUEST_SCHEME') === 'https') {
            return 'https';
        }
        return 'http';
    }

    /** collect.MAX_DECLARED_LENGTH: larger Content-Length values are recorded as this (2^53 - 1). */
    const MAX_DECLARED_LENGTH = 9007199254740991;

    /**
     * collect.read_body: at most $limit bytes. Returns array(body or null when too large,
     * declared/observed size), the declared length clamped to 0..2^53-1.
     */
    public function body($limit)
    {
        $declared = trim((string) $this->raw('CONTENT_LENGTH'));
        $length = preg_match('/\A[+-]?[0-9]+\z/', $declared) === 1 ? (int) $declared : 0; // saturates at PHP_INT_MAX
        $length = max(0, min(self::MAX_DECLARED_LENGTH, $length));
        if ($length > $limit) {
            return array(null, $length);
        }
        $body = @file_get_contents('php://input', false, null, 0, $limit + 1);
        if (!is_string($body)) {
            $body = '';
        }
        if (strlen($body) > $limit) {
            return array(null, $length > 0 ? $length : strlen($body));
        }
        return array($body, strlen($body));
    }
}

final class Response
{
    private static $sent = false;

    /** collect._respond: no body and no Content-Length for 204/304; HEAD sends headers only. */
    public static function send($code, array $headers, $body = '', $head = false)
    {
        if (self::$sent) {
            return;
        }
        self::$sent = true;
        $empty = $code === 204 || $code === 304;
        if (!headers_sent()) {
            if (function_exists('header_remove')) {
                @header_remove('X-Powered-By');
            }
            http_response_code($code);
            foreach ($headers as $h) {
                header($h[0] . ': ' . $h[1], false);
            }
            if (!$empty) {
                header('Content-Length: ' . strlen($body));
            }
        }
        if (!$head && !$empty && $body !== '') {
            echo $body;
        }
    }
}

final class Collector
{
    const GIF = "GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff\x21\xf9\x04\x01\x00\x00\x00\x00\x2c\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02\x44\x01\x00\x3b";

    const ACCEPT_CH = 'Sec-CH-UA, Sec-CH-UA-Mobile, Sec-CH-UA-Platform, Sec-CH-UA-Platform-Version, Sec-CH-UA-Model, Sec-CH-UA-Full-Version-List, Sec-CH-UA-Arch, Sec-CH-UA-Bitness, Sec-CH-UA-Form-Factors, Sec-CH-UA-WoW64, Viewport-Width, DPR, Device-Memory, RTT, Downlink, ECT, Sec-CH-Prefers-Color-Scheme, Sec-CH-Prefers-Reduced-Motion';

    const CHUNK_KINDS = array('replay', 'mouse', 'snapshot');
    /** The site feature each chunk kind needs (collect.CHUNK_FEATURES). */
    const CHUNK_FEATURES = array('replay' => 'replay', 'mouse' => 'mouse_moves', 'snapshot' => 'dom_snapshots');
    /** Bytes charged per stored hit: a row costs about that much, however small its JSON. */
    const HIT_COST = 256;
    /** Objects and arrays nested deeper than this make a body unreadable (bad_json). */
    const MAX_JSON_DEPTH = 64;
    const MAX_KEPT_COOKIES = 20;
    const MAX_HEADER_VALUE = 8192;
    const REDACTED = '[redacted]';
    const ALWAYS_SECRET = array('authorization', 'proxy-authorization');
    const SERVER_ENV_KEYS = array('SERVER_PROTOCOL', 'REQUEST_SCHEME', 'HTTPS', 'SSL_PROTOCOL', 'SSL_CIPHER', 'HTTP2',
        'H2PUSH', 'SERVER_NAME', 'SERVER_PORT', 'REMOTE_PORT', 'SERVER_SOFTWARE');

    /** Origin header syntax (collect._ORIGIN_RE), on raw bytes: Python's \s over ISO-8859-1 text. */
    const ORIGIN_RE = '/\A(?:null|[A-Za-z][A-Za-z0-9+.\-]*:\/\/[^\x09-\x0D\x1C-\x20\x85\xA0\/?#,]+)\z/';
    const ID_RE = '/\A[A-Za-z0-9_\-]{8,64}\z/';
    const STATIC_NAME_RE = '/\A[A-Za-z0-9][A-Za-z0-9._\-]*\z/';
    const STATIC_TYPES = array(
        '.js' => 'application/javascript; charset=utf-8',
        '.map' => 'application/json; charset=utf-8',
        '.css' => 'text/css; charset=utf-8',
        '.json' => 'application/json; charset=utf-8',
        '.txt' => 'text/plain; charset=utf-8',
    );

    /** @var string */
    private $entryDir;
    /** @var SqliteStore|SpoolStore|FallbackStore|null */
    private $store = null;
    /** @var float|int|false|null free bytes in the data directory's file system (null: not read yet) */
    private $diskFree = null;

    public function __construct($entryDir)
    {
        $this->entryDir = $entryDir;
    }

    // -- infrastructure ------------------------------------------------------

    private function store(Config $cfg)
    {
        if ($this->store === null) {
            $this->store = Storage::open($cfg);
        }
        return $this->store;
    }

    public function handle(Request $req)
    {
        try {
            $cfg = Config::load($this->entryDir);
        } catch (\Throwable $e) {
            // No data directory is known: the error budget's state goes next to the configuration file, if any.
            try {
                $stateDir = dirname(Config::findPath($this->entryDir));
            } catch (\Throwable $e2) {
                $stateDir = null;
            }
            ErrorLog::exception($stateDir, 'configuration unavailable', $e);
            Response::send(503, array(array('Content-Type', 'text/plain')), "configuration error\n");
            return;
        }
        $exposed = self::webRootExposure($cfg, $req->raw('DOCUMENT_ROOT'));
        if ($exposed !== null) {
            ErrorLog::error($cfg->dataDir, sprintf('refusing to run: %s is inside the web root %s, where its files could '
                . 'be downloaded (move it, or set [server] allow_data_in_web_root)', $exposed, $req->raw('DOCUMENT_ROOT')));
            Response::send(503, array(array('Content-Type', 'text/plain')), "configuration error\n");
            return;
        }
        $path = $req->path;
        $method = $req->method;
        try {
            if ($this->route($cfg, $req, $path, $method)) {
                return;
            }
        } catch (\Throwable $e) {
            ErrorLog::exception($cfg->dataDir, 'unhandled error on ' . $method . ' ' . $path, $e);
            if ($path === '/p.gif') {
                Response::send(200, self::gifHeaders(), self::GIF);
                return;
            }
            Response::send(500, array(array('Content-Type', 'text/plain')), "error\n");
            return;
        } finally {
            if ($this->store !== null) {
                $this->store->close();
                $this->store = null;
            }
        }
        Response::send(404, array(array('Content-Type', 'text/plain')), "not found\n");
    }

    /**
     * collect.web_root_exposure: which of the data directory, the configuration file and the log file lies
     * inside the web server's document root (where anyone could download the databases or the secret), or null.
     */
    private static function webRootExposure(Config $cfg, $documentRoot)
    {
        if ($documentRoot === null || $documentRoot === '' || !empty($cfg->server['allow_data_in_web_root'])) {
            return null;
        }
        $root = self::realPath($documentRoot);
        $prefix = rtrim($root, '/\\') . DIRECTORY_SEPARATOR;
        foreach (array('the data directory' => $cfg->dataDir, 'the configuration file' => $cfg->path,
                     'the log file' => (string) $cfg->server['log_file']) as $what => $path) {
            if ($path === null || $path === '') {
                continue;
            }
            $real = self::realPath($path);
            if ($real === $root || strncmp($real, $prefix, strlen($prefix)) === 0) {
                return $what . ' (' . $path . ')';
            }
        }
        return null;
    }

    /**
     * os.path.normcase(os.path.realpath(path)): symbolic links resolved as far as the path exists, the rest
     * appended (PHP's realpath() fails for a path that does not exist yet); case-folded on Windows.
     */
    private static function realPath($path)
    {
        $tail = array();
        $p = (string) $path;
        while (($real = Util::realpath($p)) === false) {
            $parent = dirname($p);
            if ($parent === $p || $parent === '.' || $parent === '') {
                $real = $p;
                break;
            }
            array_unshift($tail, basename($p));
            $p = $parent;
        }
        foreach ($tail as $part) {
            if ($part === '..') {
                $real = dirname($real);
            } elseif ($part !== '.' && $part !== '') {
                $real = rtrim($real, '/\\') . DIRECTORY_SEPARATOR . $part;
            }
        }
        return DIRECTORY_SEPARATOR === '\\' ? strtolower(str_replace('/', '\\', $real)) : $real;
    }

    /** Dispatch; false when nothing matched (404). */
    private function route(Config $cfg, Request $req, $path, $method)
    {
        if ($path === '/collect' || $path === '/chunk') {
            if ($method === 'POST') {
                if ($path === '/collect') {
                    $this->handleCollect($cfg, $req);
                } else {
                    $this->handleChunk($cfg, $req);
                }
            } elseif ($method === 'OPTIONS') {
                $this->handlePreflight($cfg, $req);
            } else {
                self::methodNotAllowed('POST, OPTIONS');
            }
            return true;
        }
        if ($path === '/p.gif') {
            $this->handlePixel($cfg, $req);
            return true;
        }
        if ($path === '/config.js' || $path === '/config.json') {
            $this->handleConfig($cfg, $req, $path === '/config.json');
            return true;
        }
        if ($path === '/health') {
            $this->handleHealth($cfg);
            return true;
        }
        if ($path === '/' && ($method === 'GET' || $method === 'HEAD')) {
            Response::send(200, array(array('Content-Type', 'text/plain; charset=utf-8')), "Tidesight\n",
                $method === 'HEAD');
            return true;
        }
        if (($method === 'GET' || $method === 'HEAD') && (strncmp($path, '/ts', 3) === 0 || $path === '/robots.txt')) {
            $this->handleStatic($cfg, $req, $path);
            return true;
        }
        return false;
    }

    // -- shared pieces ---------------------------------------------------------

    private function cors(Config $cfg, Request $req, $site)
    {
        $origin = $req->raw('HTTP_ORIGIN');
        if ($origin === null || $origin === '' || preg_match(self::ORIGIN_RE, $origin) !== 1) {
            return array();
        }
        if ($cfg->server['cors'] === 'allowed'
            && ($site === null || !Util::originAllowed($site['origins'], Util::latin1($origin)))) {
            return array(array('Vary', 'Origin'));
        }
        return array(array('Access-Control-Allow-Origin', $origin), array('Access-Control-Allow-Credentials', 'true'),
            array('Vary', 'Origin'));
    }

    private function baseHeaders(Config $cfg)
    {
        $headers = array(array('Cache-Control', 'no-store'), array('X-Robots-Tag', 'noindex, nofollow'));
        if ($cfg->server['accept_ch']) {
            $headers[] = array('Accept-CH', self::ACCEPT_CH);
        }
        return $headers;
    }

    private function siteStatus(Config $cfg, $siteId, Request $req, $requireOrigin)
    {
        if ($siteId === null || $siteId === '') {
            return array('missing_site', null);
        }
        $site = $cfg->site($siteId);
        if ($site === null) {
            return array('unknown_site', null);
        }
        $origin = $req->text('HTTP_ORIGIN');
        if ($origin === null || $origin === '') {
            $origin = Util::originOf($req->text('HTTP_REFERER'));
        }
        if ($origin !== null && $origin !== '') {
            return array(Util::originAllowed($site['origins'], $origin) ? 'ok' : 'origin_mismatch', $site);
        }
        if ($site['origins'] && $requireOrigin) {
            return array('no_origin', $site);
        }
        return array('ok', $site);
    }

    private function privacyDrop(Config $cfg, $site, Request $req)
    {
        $privacy = $site !== null ? $site['privacy'] : $cfg->privacy;
        if (!empty($privacy['honour_dnt']) && Util::pyStrip((string) $req->text('HTTP_DNT')) === '1') {
            return 'dropped_dnt';
        }
        if (!empty($privacy['honour_gpc']) && Util::pyStrip((string) $req->text('HTTP_SEC_GPC')) === '1') {
            return 'dropped_gpc';
        }
        return null;
    }

    /**
     * collect.Collector._identity: what is kept about the requester: the address (anonymised in privacy
     * mode) and the headers. A Referer can hold the full page URL (beacons, the pixel): its secrets are
     * redacted with the site's redact_params and redact_paths (Redact), as the tracker does for its URLs.
     */
    private function identity(Config $cfg, $site, Request $req, array $headers)
    {
        $privacy = $site !== null ? $site['privacy'] : $cfg->privacy;
        $ip = $req->text('REMOTE_ADDR');
        if ($ip === '') {
            $ip = null;
        }
        $hash = !empty($privacy['hash_ip']);
        $truncate = !empty($privacy['truncate_ip']);
        if ($hash || $truncate) {
            $secret = $cfg->secret();
            $ip = Ip::anonymize($ip, $hash, $truncate, $secret);
            $headers = Ip::anonymizeHeaders($headers, $hash, $truncate, $secret);
        }
        if (isset($headers['referer'])) {
            list($names, $paths) = $cfg->redactPatterns($site);
            $headers['referer'] = Redact::url($headers['referer'], $names, $paths);
        }
        return array($ip, $headers);
    }

    /**
     * collect.Collector._rate_key: the client's identity for the allowances (Ip::clientKey), else "-".
     * The connecting address, or the address the rate_limit_key header gives (its last value: the one
     * the nearest proxy appended), believed only when the connection comes from one of
     * rate_limit_proxies and the value is an IP address: anyone else could invent a new identity per
     * request, or borrow a victim's.
     */
    private function clientKey(Config $cfg, Request $req)
    {
        $remote = $req->text('REMOTE_ADDR');
        if ($remote === '') {
            $remote = null;
        }
        $keyName = (string) $cfg->server['rate_limit_key'];
        $value = null;
        if ($keyName !== 'remote_addr' && $remote !== null && Ip::inNetworks($remote, $cfg->server['rate_limit_proxies'])) {
            $var = 'HTTP_' . strtr(str_replace('-', '_', $keyName), 'abcdefghijklmnopqrstuvwxyz', 'ABCDEFGHIJKLMNOPQRSTUVWXYZ');
            $header = $req->text($var);
            if ($header !== null && $header !== '') {
                $values = explode(',', $header);
                $candidate = Util::cut(Util::pyStrip($values[count($values) - 1]), 64);
                if (Ip::address($candidate) !== null) {
                    $value = $candidate;
                }
            }
        }
        $key = Ip::clientKey($value !== null ? $value : $remote, $cfg->server['rate_limit_ipv6_prefix']);
        return $key === null ? '-' : $key;
    }

    /**
     * collect.header_bytes: what a request's stored headers (as stored: redacted, anonymised) and kept
     * cookies weigh, names and values, in characters.
     */
    private static function headerBytes(array $headers, $cookies)
    {
        $n = 0;
        foreach ($headers as $k => $v) {
            $n += Util::length((string) $k) + Util::length((string) $v);
        }
        if ($cookies !== null) {
            foreach ($cookies['kept'] as $k => $v) {
                $n += Util::length((string) $k) + Util::length((string) $v);
            }
        }
        return $n;
    }

    /**
     * collect.Collector._limits: what this request may consume (docs/configuration.md, "Limits and
     * abuse protection"). Per client and minute: requests and kilobytes (separate allowances for
     * /chunk); requests that would only be stored flagged have their own, smaller allowance, per
     * client and for all clients together; a configured site has a daily quota of stored hits.
     */
    private function limits(Config $cfg, Request $req, $site, $chunk, $size, $flagged, $hits = 0)
    {
        $s = $cfg->server;
        $client = $this->clientId($cfg, $req);
        $counter = $chunk ? 'chunk_rate_limited' : 'rate_limited';
        // Every key carries a prefix, so no client value (a header can say anything) can alias another row.
        $limits = new Limits();
        $limits->budgets[] = new Budget('r:' . $client, 1,
            $chunk ? $s['chunk_rate_limit_per_minute'] : $s['rate_limit_per_minute'], $counter);
        $limits->budgets[] = new Budget('kb:' . $client, Util::kb($size),
            $chunk ? $s['chunk_kb_per_minute'] : $s['rate_limit_kb_per_minute'], $counter);
        if ($flagged) {
            $limits->budgets[] = new Budget('f:' . $client, 1, $s['flagged_per_minute'], 'flagged_limited');
            $limits->budgets[] = new Budget('*f', 1, $s['flagged_global_per_minute'], 'flagged_limited');
        }
        $counted = $site !== null && !$flagged && !$chunk;
        $limits->site = $site !== null ? (string) $site['id'] : '';
        $limits->quota = $counted ? max(0, (int) $site['max_hits_per_day']) : 0;
        $limits->hits = $counted ? (int) $hits : 0;
        return $limits;
    }

    /**
     * collect.Collector._disk_low: true when the file system of the data directory (or of its
     * nearest existing parent) has less than min_free_disk_mb free. Read once per request (Python
     * caches it for 10 s per process; statvfs is cheap). Unknown free space never refuses a write.
     */
    private function diskLow(Config $cfg)
    {
        $need = (int) $cfg->server['min_free_disk_mb'];
        if ($need <= 0) {
            return false;
        }
        if ($this->diskFree === null) {
            $path = $cfg->dataDir !== '' ? $cfg->dataDir : '.';
            while (!@file_exists($path) && dirname($path) !== $path) {
                $path = dirname($path);
            }
            $free = function_exists('disk_free_space') ? @disk_free_space($path) : false;
            $this->diskFree = is_float($free) || is_int($free) ? $free : false;
        }
        $low = $this->diskFree !== false && $this->diskFree < $need * 1048576;
        if ($low && mt_rand(0, 99) === 0) { // Python logs this at most once a minute per process
            ErrorLog::warning($cfg->dataDir, sprintf('less than %d MB free in %s: refusing writes (503)', $need, $cfg->dataDir));
        }
        return $low;
    }

    private function write(Config $cfg, $kind, $rec, Limits $limits)
    {
        try {
            $store = $this->store($cfg);
            return $kind === 'chunk' ? $store->writeChunk($rec, $limits) : $store->writeRequest($rec, $limits);
        } catch (StorageError $e) {
            throw $e;
        } catch (\Throwable $e) { // no spool fallback configured (or a function the host disabled): still a 503
            throw new StorageError('could not store the record: ' . $e->getMessage(), 0, $e);
        }
    }

    /**
     * collect.Collector._client_id: the client in allowance keys, its key hashed with the secret as
     * privacy mode hashes addresses ("h:" + 16 hex digits of HMAC-SHA256), so the rate_limits table (and
     * its backups) never holds an address, whatever the privacy settings.
     */
    private function clientId(Config $cfg, Request $req)
    {
        return Ip::hash($this->clientKey($cfg, $req), $cfg->secret());
    }

    /**
     * collect.Collector._refused_early: a client already over its request or kilobyte allowance this
     * minute is refused before its body is decompressed, parsed or compressed (the costly part of a
     * request): charged (r: 1, then kb: $size) and counted as any refusal, under $site (the configured
     * site the query string names). Never refuses because the check itself failed.
     */
    private function refusedEarly(Config $cfg, Request $req, $site, $chunk, $size)
    {
        $s = $cfg->server;
        $client = $this->clientId($cfg, $req);
        $caps = array();
        $r = (int) ($chunk ? $s['chunk_rate_limit_per_minute'] : $s['rate_limit_per_minute']);
        $kb = (int) ($chunk ? $s['chunk_kb_per_minute'] : $s['rate_limit_kb_per_minute']);
        if ($r > 0) {
            $caps['r:' . $client] = $r;
        }
        if ($kb > 0) {
            $caps['kb:' . $client] = $kb;
        }
        if (!$caps || $this->diskLow($cfg)) { // low disk: the normal path answers 503 without touching the database
            return false;
        }
        $stream = $chunk ? 'chunks' : 'events';
        $now = Util::nowMs();
        try {
            $store = $this->store($cfg);
            $used = $store->usage($stream, array_keys($caps), $now);
            $over = false;
            foreach ($caps as $key => $cap) {
                $over = $over || (isset($used[$key]) && $used[$key] >= $cap);
            }
            if (!$over) {
                return false;
            }
            $all = $this->limits($cfg, $req, $site, $chunk, $size, false);
            $limits = new Limits();
            $limits->budgets = $all->budgets;
            $limits->site = $all->site;
            $store->refuse($stream, $limits, $now);
        } catch (\Throwable $e) {
            ErrorLog::exception($cfg->dataDir, 'could not check allowances', $e);
            return false;
        }
        return true;
    }

    /**
     * collect.Collector._count: count a request that stores nothing else (a DNT/GPC drop, an oversize
     * chunk), under a configured site only (never an id from the request), and only within the client's
     * request allowance (r:<client>, rate_limit_per_minute, in events.db): a flood of them must not
     * become a flood of writes.
     */
    private function count(Config $cfg, $site, $name, Request $req)
    {
        $limits = new Limits();
        $limits->budgets[] = new Budget('r:' . $this->clientId($cfg, $req), 1, $cfg->server['rate_limit_per_minute'], '');
        $limits->site = $site !== null ? (string) $site['id'] : '';
        try {
            $this->store($cfg)->recordCounter(gmdate('Y-m-d'), $limits->site, $name, 1, $limits);
        } catch (\Throwable $e) {
            ErrorLog::exception($cfg->dataDir, 'could not record counter ' . $name, $e);
        }
    }

    /** collect.extract_headers: all request headers (lower-case names), secrets redacted, plus our cookies. */
    private function extractHeaders(Config $cfg, Request $req)
    {
        $hcfg = $cfg->headers;
        $secretPatterns = array();
        foreach ((array) $hcfg['secret_patterns'] as $p) {
            $secretPatterns[] = Util::lowerLatin((string) $p);
        }
        $drop = array();
        foreach ((array) $hcfg['drop'] as $d) {
            $drop[Util::lowerLatin((string) $d)] = true;
        }
        $cookiePatterns = (array) $hcfg['cookie_names'];
        $headers = array();
        $cookies = null;
        foreach ($req->server as $key => $value) {
            $key = (string) $key;
            if (strncmp($key, 'HTTP_', 5) === 0) {
                $name = Util::lowerLatin(Util::latin1(str_replace('_', '-', (string) substr($key, 5))));
            } elseif ($key === 'CONTENT_TYPE' || $key === 'CONTENT_LENGTH') {
                if (!is_string($value) || $value === '') {
                    continue;
                }
                $name = $key === 'CONTENT_TYPE' ? 'content-type' : 'content-length';
            } else {
                continue;
            }
            if (!is_string($value) || isset($drop[$name])) {
                continue;
            }
            if ($name === 'cookie') {
                $kept = array();
                $others = 0;
                foreach (self::parseCookieHeader($value) as $pair) {
                    $cname = Util::latin1($pair[0]);
                    $match = false;
                    // The dashboard's sign-in cookie (ts_login) is never kept, whatever cookie_names says.
                    if ($cname !== 'ts_login' && count($kept) < self::MAX_KEPT_COOKIES) { // further matching cookies count as others
                        foreach ($cookiePatterns as $p) {
                            if (Util::fnmatch($cname, (string) $p)) {
                                $match = true;
                                break;
                            }
                        }
                    }
                    if ($match) {
                        // A comma is not a cookie character: past it is another Cookie header the server merged in.
                        $v = $pair[1];
                        $comma = strpos($v, ',');
                        if ($comma !== false) {
                            $v = rtrim(substr($v, 0, $comma), "\x09\x0A\x0B\x0C\x0D\x1C\x1D\x1E\x1F\x20\x85\xA0");
                        }
                        $kept[$cname] = Util::latin1(substr($v, 0, 512));
                    } else {
                        $others++;
                    }
                }
                $cookies = array('kept' => $kept, 'others' => $others);
                continue;
            }
            if (in_array($name, self::ALWAYS_SECRET, true) || self::anyMatch($name, $secretPatterns)) {
                $headers[$name] = self::REDACTED;
                continue;
            }
            $headers[$name] = Util::latin1(substr($value, 0, self::MAX_HEADER_VALUE));
        }
        // Apache hides Authorization from PHP unless .htaccess passes it on; still record that it was sent.
        if (!isset($headers['authorization']) && !isset($drop['authorization'])
            && ($req->raw('REDIRECT_HTTP_AUTHORIZATION') || $req->raw('PHP_AUTH_USER') !== null
                || $req->raw('PHP_AUTH_DIGEST') !== null)) {
            $headers['authorization'] = self::REDACTED;
        }
        return array($headers, $cookies);
    }

    private static function anyMatch($name, array $patterns)
    {
        foreach ($patterns as $p) {
            if (Util::fnmatch($name, $p)) {
                return true;
            }
        }
        return false;
    }

    /** collect.parse_cookie_header on raw bytes (Python's strip() over ISO-8859-1 text). */
    private static function parseCookieHeader($value)
    {
        $ws = "\x09\x0A\x0B\x0C\x0D\x1C\x1D\x1E\x1F\x20\x85\xA0";
        $out = array();
        foreach (explode(';', $value) as $part) {
            $eq = strpos($part, '=');
            if ($eq !== false) {
                $out[] = array(trim(substr($part, 0, $eq), $ws), trim((string) substr($part, $eq + 1), $ws));
            } else {
                $p = trim($part, $ws);
                if ($p !== '') {
                    $out[] = array($p, '');
                }
            }
        }
        return $out;
    }

    private function serverInfo(Request $req)
    {
        $info = array('connector' => 'php', 'version' => VERSION, 'scheme' => $req->scheme(),
            'path' => Util::cut($req->fullPath, 512));
        foreach (self::SERVER_ENV_KEYS as $key) {
            $v = $req->raw($key);
            if ($v !== null && $v !== '') {
                $info[strtolower($key)] = Util::cut(Util::latin1($v), 200);
            }
        }
        $info['php'] = PHP_VERSION;
        $info['sapi'] = PHP_SAPI;
        return Json::encodeExact(new JsonObject($info));
    }

    private static function headersJson(array $headers)
    {
        return Json::encodeExact(new JsonObject($headers));
    }

    private static function cookiesJson($cookies)
    {
        if ($cookies === null) {
            return null;
        }
        return Json::encodeExact(new JsonObject(array('kept' => new JsonObject($cookies['kept']),
            'others' => $cookies['others'])));
    }

    // -- /collect --------------------------------------------------------------

    private function handlePreflight(Config $cfg, Request $req)
    {
        $headers = array_merge($this->cors($cfg, $req, null), array(
            array('Access-Control-Allow-Methods', 'POST, GET, OPTIONS'),
            array('Access-Control-Allow-Headers', 'Content-Type, Content-Encoding'),
            array('Access-Control-Max-Age', '86400'),
            array('Cache-Control', 'no-store'),
        ));
        Response::send(204, $headers);
    }

    private function handleCollect(Config $cfg, Request $req)
    {
        $received = Util::nowMs();
        $s = $cfg->server;
        $query = $req->query;
        $qs = Util::parseQs($query, true);
        $qsSite = isset($qs['site']) ? $qs['site'][0] : null;
        $contentType = $req->text('CONTENT_TYPE');
        if ($contentType === '') {
            $contentType = null;
        }
        list($headers, $cookies) = $this->extractHeaders($cfg, $req);
        list($body, $size) = $req->body((int) $s['max_payload_kb'] * 1024);
        $queryBytes = strlen((string) $req->raw('QUERY_STRING')); // Python's len() of the ISO-8859-1 text
        if ($body !== null) {
            $qSite = $cfg->site(Util::str($qsSite, 100));
            if ($this->refusedEarly($cfg, $req, $qSite, false, $size + $queryBytes)) {
                Response::send(429, array_merge($this->baseHeaders($cfg), $this->cors($cfg, $req, $qSite),
                    array(array('Retry-After', '60'))));
                return;
            }
        }

        $rec = new RequestRecord();
        $rec->received_at = $received;
        $rec->endpoint = 'collect';
        $rec->method = 'POST';
        $rec->site_status = 'invalid';
        $rec->cookies = self::cookiesJson($cookies);
        $rec->query = $query === '' ? null : $query;
        $rec->content_type = $contentType;
        $rec->body_bytes = $size;
        $rec->server = $this->serverInfo($req);
        $code = 204;
        $envelope = null;
        $exact = false;
        $storedSize = $size; // what this request would store, for the per-minute KB budget

        if ($body === null) {
            $rec->error = 'too_large';
            $rec->site_status = 'too_large';
            $rec->site_id = Util::str($qsSite, 100);
            $code = 413;
            $storedSize = (int) $s['max_payload_kb'] * 1024 + 1; // what was read, not what the request claims
        } else {
            list($envelope, $error, $encoding, $exact, $dataBytes) = $this->parseEnvelope($body, $req, $s, $contentType);
            $rec->body_encoding = $encoding;
            $storedSize = max($size, $dataBytes);
            if ($error !== null) {
                $rec->error = $error;
                $rec->body = $body;
                $code = 400;
            } elseif ($s['store_raw_body']) {
                $rec->body = $body;
            }
        }

        if ($envelope !== null) {
            $siteId = Util::str(Json::get($envelope, 'site'), 100);
            if ($siteId === null) {
                $siteId = Util::str($qsSite, 100);
            }
            $rec->site_id = $siteId;
            list($rec->site_status, $site) = $this->siteStatus($cfg, $siteId, $req, true);
        } else {
            if ($rec->site_id === null) {
                $rec->site_id = Util::str($qsSite, 100);
            }
            $site = $cfg->site($rec->site_id);
        }
        $cors = $this->cors($cfg, $req, $site);
        $dropped = $this->privacyDrop($cfg, $site, $req);
        if ($dropped !== null) {
            $this->count($cfg, $site, $dropped, $req);
            Response::send(204, array_merge($this->baseHeaders($cfg), $cors));
            return;
        }
        if ($this->diskLow($cfg)) {
            Response::send(503, array_merge($this->baseHeaders($cfg), $cors, array(array('Retry-After', '300'))));
            return;
        }
        list($rec->ip, $headers) = $this->identity($cfg, $site, $req, $headers);
        $rec->headers = self::headersJson($headers);

        if ($envelope !== null) {
            list($hits, $errors) = self::buildHits($envelope, (int) $s['max_events_per_request'], $site, $exact);
            $rec->hits = $hits;
            $rec->proto_v = Util::toInt(Json::get($envelope, 'v'));
            $rec->envelope = Json::dumps(Json::without($envelope, 'events'), $exact);
            if ($errors) {
                $rec->error = implode(',', $errors);
                $tooMany = false;
                foreach ($errors as $e) {
                    $tooMany = $tooMany || strncmp($e, 'too_many_events', 15) === 0;
                }
                if ($tooMany || in_array('no_events', $errors, true) || in_array('not_an_object', $errors, true)) {
                    $code = 400;
                }
                if ($tooMany) {
                    $rec->body = $body; // the events not stored as hits stay in the raw body
                }
            }
        }

        $flagged = Storage::isFlagged($rec); // a batch with a few invalid events still has good hits: normal allowances
        // What the request stores: its body, its hits as serialized (re-serialized numbers can be longer than
        // sent: 9e15 becomes 9000000000000000.0), at least HIT_COST a hit, its query string and its headers.
        $storedHits = 0;
        foreach ($rec->hits as $h) {
            $storedHits += Util::length($h->data);
        }
        $charged = max($storedSize, self::HIT_COST * count($rec->hits), $storedHits) + $queryBytes
            + self::headerBytes($headers, $cookies);
        $limits = $this->limits($cfg, $req, $site, false, $charged, $flagged, count($rec->hits));
        try {
            $outcome = $this->write($cfg, 'request', $rec, $limits);
        } catch (StorageError $e) {
            ErrorLog::exception($cfg->dataDir, 'storage unavailable', $e);
            Response::send(503, array_merge($this->baseHeaders($cfg), $cors, array(array('Retry-After', '60'))));
            return;
        }
        $out = array_merge($this->baseHeaders($cfg), $cors);
        if ($outcome === Storage::RATE_LIMITED) {
            if ($code === 204) { // 400 and 413 stand: retrying cannot help
                $code = 429;
                $out[] = array('Retry-After', '60');
            }
            Response::send($code, $out);
            return;
        }
        if ($code === 204 && $site !== null && $rec->site_status === 'ok' && $envelope !== null) {
            $cookie = $this->serverCookie($site, $envelope, $req);
            if ($cookie !== null) {
                $out[] = array('Set-Cookie', $cookie);
            }
        }
        Response::send($code, $out);
    }

    /**
     * Decompress, decode and parse. Returns array(envelope or null, error or null, encoding, exact,
     * size of the decoded bytes: the decompressed body, or the body as received when it is not gzip
     * or does not decompress).
     */
    private function parseEnvelope($body, Request $req, array $s, $contentType)
    {
        $encoding = 'identity';
        $data = $body;
        $contentEncoding = Util::lowerLatin((string) $req->text('HTTP_CONTENT_ENCODING'));
        if (strncmp($body, "\x1f\x8b", 2) === 0 || strpos($contentEncoding, 'gzip') !== false) {
            $encoding = 'gzip';
            list($data, $err) = Util::gunzipBounded($body, (int) $s['max_decompressed_kb'] * 1024);
            if ($err !== null) {
                return array(null, 'bad_gzip: ' . $err, $encoding, false, strlen($body));
            }
        } elseif ($contentEncoding !== '' && $contentEncoding !== 'identity') {
            $enc = Util::cut($contentEncoding, 40);
            return array(null, 'unsupported_encoding: ' . $enc, $enc, false, strlen($body));
        }
        $bytes = strlen($data);
        if ($data === '') {
            return array(null, 'empty_body', $encoding, false, 0);
        }
        if (!Util::isUtf8($data)) {
            return array(null, 'bad_utf8', $encoding, false, $bytes);
        }
        $text = strncmp($data, "\xEF\xBB\xBF", 3) === 0 ? (string) substr($data, 3) : $data;
        if ($contentType !== null && strpos(Util::lowerLatin($contentType), 'application/x-www-form-urlencoded') !== false) {
            $text = Util::firstFormValue($text, array('d', 'data')); // (form.get("d") or form.get("data") or [""])[0]
        }
        if (strlen($data) > 1048576) {
            // A large body decodes into many PHP values: make room, sized by the configuration (the largest
            // body it admits), never by what a request claims or sends.
            $cap = max((int) $s['max_payload_kb'], (int) $s['max_decompressed_kb']) * 1024;
            self::ensureMemory(64 * $cap + 64 * 1048576);
        }
        list($obj, $error, $exact) = Json::loads($text);
        if ($error !== null) {
            return array(null, 'bad_json: ' . Util::cut($error, 120), $encoding, false, $bytes);
        }
        if (!Json::isObject($obj)) {
            return array(null, 'not_an_object', $encoding, false, $bytes);
        }
        if (Json::deeperThan($obj, self::MAX_JSON_DEPTH, $text)) { // nothing the tracker sends nests deeper than a few levels
            return array(null, 'bad_json: nested deeper than ' . self::MAX_JSON_DEPTH . ' levels', $encoding, false, $bytes);
        }
        if (Util::$loneSurrogates) {
            $obj = Json::replaceSurrogates($obj);
        }
        return array($obj, null, $encoding, $exact, $bytes);
    }

    private static function ensureMemory($bytes)
    {
        if (!function_exists('ini_get') || !function_exists('ini_set')) {
            return;
        }
        $limit = trim((string) ini_get('memory_limit'));
        if ($limit === '' || $limit === '-1') {
            return;
        }
        $n = (int) $limit;
        $unit = strtolower(substr($limit, -1));
        if ($unit === 'g') {
            $n *= 1073741824;
        } elseif ($unit === 'm') {
            $n *= 1048576;
        } elseif ($unit === 'k') {
            $n *= 1024;
        }
        if ($n < $bytes) {
            @ini_set('memory_limit', (string) $bytes);
        }
    }

    private function serverCookie(array $site, $envelope, Request $req)
    {
        $domain = (string) $site['server_cookie_domain'];
        $vid = Json::get($envelope, 'vid');
        if ($domain === '' || !is_string($vid) || preg_match(self::ID_RE, $vid) !== 1) {
            return null;
        }
        $vt = Util::toInt(Json::get($envelope, 'vt'));
        $value = $vt !== null && $vt > 0 ? $vid . '.' . $vt : $vid;
        if (preg_match('/\A\.?[A-Za-z0-9.\-]+\z/', $domain) !== 1) {
            return null;
        }
        $https = Util::lowerAscii((string) $req->raw('HTTPS'));
        $secure = $req->scheme() === 'https' || $https === 'on' || $https === '1';
        $parts = array('ts_svid=' . $value, 'Domain=' . $domain, 'Path=/',
            'Max-Age=' . ((int) $site['cookie_days'] * 86400), 'SameSite=Lax');
        if ($secure) {
            $parts[] = 'Secure';
        }
        return implode('; ', $parts);
    }

    /**
     * collect.build_hits: split an envelope into hit records with typed columns. At most $maxEvents
     * become hits: one request must not turn into an unbounded number of rows. The caller keeps the
     * raw body of a longer batch, flagged "too_many_events:<count>", so nothing is lost.
     */
    public static function buildHits($envelope, $maxEvents, $site, $exact)
    {
        $errors = array();
        $v = Json::get($envelope, 'v');
        if (!is_int($v) && !($v instanceof JsonBigInt)) {
            $errors[] = 'bad_version';
        }
        $events = Json::get($envelope, 'events');
        if (!is_array($events)) {
            $errors[] = 'no_events';
            return array(array(), $errors);
        }
        if (count($events) > $maxEvents) {
            $errors[] = 'too_many_events:' . count($events);
            $events = array_slice($events, 0, $maxEvents); // events[:max_events], a negative limit included
        }
        $dropFp = $site !== null && !empty($site['privacy']['drop_fingerprint']);
        $vid = Util::str(Json::get($envelope, 'vid'), 64);
        $sid = Util::str(Json::get($envelope, 'sid'), 64);
        $uid = Util::str(Json::get($envelope, 'uid'), 200);
        $hits = array();
        $bad = 0;
        foreach ($events as $seq => $ev) {
            if (!Json::isObject($ev)) {
                $bad++;
                $h = new HitRecord($seq, Json::dumps($ev, $exact));
                $h->type = '_invalid';
                $h->visitor_id = $vid;
                $h->session_id = $sid;
                $h->user_id = $uid;
                $hits[] = $h;
                continue;
            }
            $etype = Util::str(Json::get($ev, 't'), 32);
            if ($etype === null) {
                $bad++;
                $etype = '_invalid';
            }
            if ($dropFp && $etype === 'fp') {
                continue;
            }
            $h = new HitRecord($seq, Json::dumps($ev, $exact));
            $h->type = $etype;
            $n = Json::get($ev, 'n');
            $h->name = is_string($n) ? Util::str($n, 200) : null; // only a string n is a name (rage/form/fp use a count)
            $h->client_ts = Util::toInt(Json::get($ev, 'ts'));
            $h->event_id = Util::str(Json::get($ev, 'id'), 64);
            $x = Util::str(Json::get($ev, 'vid'), 64);
            $h->visitor_id = $x !== null ? $x : $vid;
            $x = Util::str(Json::get($ev, 'sid'), 64);
            $h->session_id = $x !== null ? $x : $sid;
            $h->page_id = Util::str(Json::get($ev, 'pid'), 64);
            $x = Util::str(Json::get($ev, 'uid'), 200);
            $h->user_id = $x !== null ? $x : $uid;
            $h->url = Util::str(Json::get($ev, 'url'), 2048);
            $hits[] = $h;
        }
        if ($bad) {
            $errors[] = 'bad_events:' . $bad;
        }
        return array($hits, $errors);
    }

    // -- /chunk ----------------------------------------------------------------

    private function handleChunk(Config $cfg, Request $req)
    {
        $received = Util::nowMs();
        $s = $cfg->server;
        $qs = Util::firstValues(Util::parseQs($req->query, true));
        $siteId = Util::str(isset($qs['site']) ? $qs['site'] : null, 100);
        list($siteStatus, $site) = $this->siteStatus($cfg, $siteId, $req, true);
        $dropped = $this->privacyDrop($cfg, $site, $req);
        $cors = $this->cors($cfg, $req, $site);
        if ($dropped !== null) {
            $this->count($cfg, $site, $dropped, $req);
            Response::send(204, array_merge($this->baseHeaders($cfg), $cors));
            return;
        }
        list($body, $size) = $req->body((int) $s['max_chunk_kb'] * 1024);
        if ($body === null) {
            $this->count($cfg, $site, 'chunk_too_large', $req);
            Response::send(413, array_merge($this->baseHeaders($cfg), $cors));
            return;
        }
        if ($this->diskLow($cfg)) {
            Response::send(503, array_merge($this->baseHeaders($cfg), $cors, array(array('Retry-After', '300'))));
            return;
        }
        if ($this->refusedEarly($cfg, $req, $site, true, $size + strlen((string) $req->raw('QUERY_STRING')))) {
            Response::send(429, array_merge($this->baseHeaders($cfg), $cors, array(array('Retry-After', '60'))));
            return;
        }
        $k = isset($qs['k']) ? $qs['k'] : null;
        $kind = in_array($k, self::CHUNK_KINDS, true) ? $k : '_invalid';
        $error = $kind !== '_invalid' ? null : 'unknown_kind';
        if ($body === '') {
            $error = 'empty_body';
        }
        $needs = self::CHUNK_FEATURES;
        if ($site !== null && $siteStatus === 'ok' && isset($needs[$kind]) && empty($site['features'][$needs[$kind]])) {
            $siteStatus = 'feature_off'; // the site does not record this kind: stored flagged, within its allowance
        }
        $sentGzip = strncmp($body, "\x1f\x8b", 2) === 0;
        $payload = $body;
        $encoding = 'gzip';
        if (!$sentGzip) {
            $payload = Util::gzipCompress($body);
            if ($payload === null) { // no zlib: keep the bytes as sent
                $payload = $body;
                $encoding = 'identity';
            }
        }
        list($headers, $unusedCookies) = $this->extractHeaders($cfg, $req);
        list($ip, $headers) = $this->identity($cfg, $site, $req, $headers);
        $meta = $qs;
        $meta['bytes'] = $size;
        $meta['sent_encoding'] = $sentGzip ? 'gzip' : 'identity';
        foreach (array('user-agent', 'origin', 'referer') as $h) {
            if (isset($headers[$h])) {
                $meta[$h] = $headers[$h];
            }
        }
        $rec = new ChunkRecord();
        $rec->received_at = $received;
        $rec->kind = $kind;
        $rec->site_id = $siteId;
        $rec->site_status = $siteStatus;
        $rec->payload = $payload;
        $rec->encoding = $encoding;
        $rec->meta = Json::encodeExact(new JsonObject($meta));
        $rec->visitor_id = Util::str(isset($qs['vid']) ? $qs['vid'] : null, 64);
        $rec->session_id = Util::str(isset($qs['sid']) ? $qs['sid'] : null, 64);
        $rec->page_id = Util::str(isset($qs['pid']) ? $qs['pid'] : null, 64);
        $rec->seq = Util::toInt(isset($qs['seq']) ? $qs['seq'] : null);
        $rec->client_ts = Util::toInt(isset($qs['ts']) ? $qs['ts'] : null);
        $rec->url = Util::str(isset($qs['url']) ? $qs['url'] : null, 2048);
        $rec->ip = $ip;
        $rec->error = $error;
        // Charged: the body received, the query string (its raw length) and the headers kept in meta.
        $charged = $size + strlen((string) $req->raw('QUERY_STRING'));
        foreach (array('user-agent', 'origin', 'referer') as $h) {
            if (isset($meta[$h])) {
                $charged += strlen($h) + Util::length((string) $meta[$h]);
            }
        }
        $limits = $this->limits($cfg, $req, $site, true, $charged, $siteStatus !== 'ok' || $error !== null);
        try {
            $outcome = $this->write($cfg, 'chunk', $rec, $limits);
        } catch (StorageError $e) {
            ErrorLog::exception($cfg->dataDir, 'storage unavailable', $e);
            Response::send(503, array_merge($this->baseHeaders($cfg), $cors, array(array('Retry-After', '60'))));
            return;
        }
        if ($outcome === Storage::RATE_LIMITED && $error === null) { // a 400 stands: retrying cannot help
            Response::send(429, array_merge($this->baseHeaders($cfg), $cors, array(array('Retry-After', '60'))));
            return;
        }
        Response::send($error !== null ? 400 : 204, array_merge($this->baseHeaders($cfg), $cors));
    }

    // -- /p.gif ------------------------------------------------------------------

    private function handlePixel(Config $cfg, Request $req)
    {
        $received = Util::nowMs();
        $method = $req->method;
        $query = $req->query;
        $parsed = Util::parseQs($query, true);
        $data = array();
        foreach ($parsed as $k => $values) {
            $data[$k] = count($values) === 1 ? $values[0] : $values;
        }
        $first = Util::firstValues($parsed);
        $siteId = Util::str(isset($first['site']) ? $first['site'] : null, 100);
        list($siteStatus, $site) = $this->siteStatus($cfg, $siteId, $req, false);
        if ($this->privacyDrop($cfg, $site, $req) !== null) {
            Response::send(200, self::gifHeaders(), self::GIF, $method === 'HEAD');
            return;
        }
        list($headers, $cookies) = $this->extractHeaders($cfg, $req);
        list($ip, $headers) = $this->identity($cfg, $site, $req, $headers);
        $url = null; // u, url, else the stored (redacted) Referer
        foreach (array(isset($first['u']) ? $first['u'] : null, isset($first['url']) ? $first['url'] : null,
                     isset($headers['referer']) ? $headers['referer'] : null) as $candidate) {
            if ($candidate !== null && $candidate !== '') {
                $url = $candidate;
                break;
            }
        }
        $hit = new HitRecord(0, Json::encodeExact(new JsonObject($data)));
        $type = Util::str(isset($first['t']) ? $first['t'] : null, 32);
        $hit->type = $type !== null ? $type : 'pageview';
        $hit->name = Util::str(isset($first['n']) ? $first['n'] : null, 200);
        $hit->event_id = Util::str(isset($first['id']) ? $first['id'] : null, 64);
        $hit->visitor_id = Util::str(isset($first['vid']) ? $first['vid'] : null, 64);
        $hit->session_id = Util::str(isset($first['sid']) ? $first['sid'] : null, 64);
        $hit->user_id = Util::str(isset($first['uid']) ? $first['uid'] : null, 200);
        $hit->url = Util::str($url, 2048);
        $hit->client_ts = Util::toInt(isset($first['ts']) ? $first['ts'] : null);
        $rec = new RequestRecord();
        $rec->received_at = $received;
        $rec->endpoint = 'pixel';
        $rec->method = $method;
        $rec->site_id = $siteId;
        $rec->site_status = $siteStatus;
        $rec->ip = $ip;
        $rec->headers = self::headersJson($headers);
        $rec->cookies = self::cookiesJson($cookies);
        $rec->query = $query === '' ? null : $query;
        $rec->server = $this->serverInfo($req);
        $rec->hits = array($hit);
        if (!$this->diskLow($cfg)) { // the pixel always answers its GIF, stored or not
            // Charged: the query string as received (Python's len() of the ISO-8859-1 text), at least a hit's
            // cost, plus the stored headers and kept cookies.
            $charged = max(strlen((string) $req->raw('QUERY_STRING')), self::HIT_COST)
                + self::headerBytes($headers, $cookies);
            $limits = $this->limits($cfg, $req, $site, false, $charged, Storage::isFlagged($rec), 1);
            try {
                $this->write($cfg, 'request', $rec, $limits);
            } catch (StorageError $e) {
                ErrorLog::exception($cfg->dataDir, 'storage unavailable (pixel)', $e);
            }
        }
        Response::send(200, self::gifHeaders(), self::GIF, $method === 'HEAD');
    }

    // -- /config.js --------------------------------------------------------------

    private function handleConfig(Config $cfg, Request $req, $asJson)
    {
        $method = $req->method;
        if ($method !== 'GET' && $method !== 'HEAD') {
            self::methodNotAllowed('GET, HEAD');
            return;
        }
        $qs = Util::parseQs($req->query, false);
        $siteId = Util::str(isset($qs['site']) ? $qs['site'][0] : '', 100);
        $public = $cfg->publicConfig($siteId === null ? '' : $siteId);
        $etag = '"' . $public->cv . '"';
        $headers = array(array('Cache-Control', 'public, max-age=300'), array('ETag', $etag),
            array('Access-Control-Allow-Origin', '*'), array('X-Content-Type-Options', 'nosniff'),
            array('X-Robots-Tag', 'noindex, nofollow'));
        if ($req->raw('HTTP_IF_NONE_MATCH') === $etag) {
            Response::send(304, $headers);
            return;
        }
        $body = Json::dumpsAscii($public, true, ',', ':');
        if ($asJson) {
            $headers[] = array('Content-Type', 'application/json; charset=utf-8');
            $payload = $body;
        } else {
            $headers[] = array('Content-Type', 'application/javascript; charset=utf-8');
            // Queues the configuration for the tracker, or hands it over if the tracker runs already. Never
            // throws, even if markup on the page made window.tidesight an element (<a id="tidesight">).
            $payload = '(typeof window.tidesight=="function"?window.tidesight:window.tidesight=function(){'
                . '(tidesight.q=tidesight.q||[]).push(arguments)})("_config",' . $body . ");\n";
        }
        Response::send(200, $headers, $payload, $method === 'HEAD');
    }

    // -- /health -----------------------------------------------------------------

    private function handleHealth(Config $cfg)
    {
        $ok = (bool) $this->store($cfg)->healthy();
        $body = Json::dumpsAscii(new JsonObject(array('status' => $ok ? 'ok' : 'error', 'version' => VERSION,
            'storage' => (string) $cfg->server['storage_mode'], 'time' => Util::nowMs())), false);
        Response::send($ok ? 200 : 503, array(array('Content-Type', 'application/json'),
            array('Cache-Control', 'no-store')), $body);
    }

    // -- static tracker files ------------------------------------------------------

    private function handleStatic(Config $cfg, Request $req, $path)
    {
        $notFound = array(array('Content-Type', 'text/plain'));
        if ($path === '/robots.txt') {
            Response::send(200, array(array('Content-Type', 'text/plain')), "User-agent: *\nDisallow: /\n");
            return;
        }
        // Only from [server] static_dir: a default relative to this code would point outside the deployment
        // (on shared hosting, possibly into another account's directory). The web server serves them otherwise.
        $root = (string) $cfg->server['static_dir'];
        if ($root === '') {
            Response::send(404, $notFound, "not found\n");
            return;
        }
        $parts = array();
        foreach (explode('/', $path) as $p) {
            if ($p !== '') {
                $parts[] = $p;
            }
        }
        $valid = $parts && count($parts) <= 2;
        foreach ($parts as $p) {
            $valid = $valid && preg_match(self::STATIC_NAME_RE, $p) === 1;
        }
        if (!$valid || (count($parts) === 2 && $parts[0] !== 'ts')) {
            Response::send(404, $notFound, "not found\n");
            return;
        }
        $last = $parts[count($parts) - 1];
        $dot = strrpos($last, '.');
        $ext = $dot === false ? '' : Util::lowerAscii(substr($last, $dot));
        $file = $root . DIRECTORY_SEPARATOR . implode(DIRECTORY_SEPARATOR, $parts);
        $types = self::STATIC_TYPES;
        $realRoot = Util::realpath($root);
        $realFile = isset($types[$ext]) && is_file($file) ? Util::realpath($file) : false;
        $inside = $realRoot !== false && $realFile !== false
            && strncmp($realFile, rtrim($realRoot, '/\\') . DIRECTORY_SEPARATOR, strlen(rtrim($realRoot, '/\\')) + 1) === 0;
        if (!$inside) { // also a symbolic link out of the root
            Response::send(404, $notFound, "not found\n");
            return;
        }
        $content = (string) file_get_contents($file);
        $versioned = strpos($req->query, 'v=') !== false;
        Response::send(200, array(array('Content-Type', $types[$ext]), array('Access-Control-Allow-Origin', '*'),
            array('X-Content-Type-Options', 'nosniff'),
            array('Cache-Control', $versioned ? 'public, max-age=31536000, immutable' : 'public, max-age=3600')),
            $content, $req->method === 'HEAD');
    }

    // -- plumbing ----------------------------------------------------------------

    private static function gifHeaders()
    {
        return array(array('Content-Type', 'image/gif'), array('Cache-Control', 'no-store, max-age=0'),
            array('X-Robots-Tag', 'noindex, nofollow'), array('Access-Control-Allow-Origin', '*'));
    }

    private static function methodNotAllowed($allow)
    {
        Response::send(405, array(array('Allow', $allow), array('Content-Type', 'text/plain')), "method not allowed\n");
    }
}
