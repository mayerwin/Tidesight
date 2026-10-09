<?php
/**
 * The resolved configuration written by `python -m tidesight.jobs config-json`
 * (Config.resolved(): format "tidesight-resolved-config/1"). Defaults and per-site
 * layering are already applied by Python; this class only reads the result.
 */

namespace Tidesight;

final class ConfigError extends \RuntimeException
{
}

final class Config
{
    const FORMAT = 'tidesight-resolved-config/1';

    /** config.SERVER_DEFAULTS (used only for keys missing from a hand-written file). */
    const SERVER_DEFAULTS = array(
        'data_dir' => '',
        'storage_mode' => 'sqlite',
        'journal_mode' => 'wal',
        'synchronous' => 'normal',
        'busy_timeout_ms' => 10000,
        'max_payload_kb' => 64,
        'max_chunk_kb' => 1024,
        'max_decompressed_kb' => 256,
        'max_events_per_request' => 200,
        'rate_limit_per_minute' => 600,
        'rate_limit_kb_per_minute' => 2048,
        'chunk_rate_limit_per_minute' => 120,
        'chunk_kb_per_minute' => 8192,
        'flagged_per_minute' => 30,
        'flagged_global_per_minute' => 300,
        'rate_limit_key' => 'remote_addr',
        'rate_limit_proxies' => array(), // CIDRs ("cloudflare" is expanded by Python in the resolved JSON)
        'rate_limit_ipv6_prefix' => 64,
        'min_free_disk_mb' => 512,
        'cors' => 'reflect',
        'accept_ch' => true,
        'public_url' => '',
        'secret' => '',
        'static_dir' => '',
        'dashboard_dir' => '',
        'store_raw_body' => false,
        'spool_fallback' => true,
        'allow_data_in_web_root' => false,
        'log_file' => '',
    );

    const HEADERS_DEFAULTS = array(
        'secret_patterns' => array('*token*', '*key*', '*secret*', '*password*', '*auth*', '*session*', '*jwt*',
            '*assertion*', '*credential*', '*signature*', '*cookie*'),
        'drop' => array(),
        'cookie_names' => array('ts_*'),
    );

    const PRIVACY_DEFAULTS = array(
        'hash_ip' => false,
        'truncate_ip' => false,
        'honour_dnt' => false,
        'honour_gpc' => false,
        'drop_fingerprint' => false,
    );

    /** config.SITE_DEFAULTS["max_hits_per_day"]: no daily quota unless configured. */
    const MAX_HITS_PER_DAY = 0;

    /** @var string */
    public $path;
    /** @var string */
    private $text;
    /** @var array */
    public $server;
    /** @var array */
    public $headers;
    /** @var array */
    public $privacy;
    /** @var array<string,array> */
    public $sites;
    /** @var string */
    public $dataDir;
    /** @var string|null */
    private $secret = null;
    /** @var object|null */
    private $doc = null;
    /** @var array array(redact_params, redact_paths) of the default site (unknown sites) */
    private $defaultRedact = array(array(), array());

    /** The config file to use: TIDESIGHT_CONFIG (constant or environment), else tidesight.json nearby. */
    public static function findPath($entryDir)
    {
        if (defined('TIDESIGHT_CONFIG') && constant('TIDESIGHT_CONFIG')) {
            return (string) constant('TIDESIGHT_CONFIG');
        }
        $env = function_exists('getenv') ? getenv('TIDESIGHT_CONFIG') : false; // $_SERVER below otherwise
        if ($env !== false && $env !== '') {
            return $env;
        }
        foreach (array('TIDESIGHT_CONFIG', 'REDIRECT_TIDESIGHT_CONFIG') as $key) {
            if (!empty($_SERVER[$key]) && is_string($_SERVER[$key])) {
                return $_SERVER[$key];
            }
        }
        if (!empty($_ENV['TIDESIGHT_CONFIG']) && is_string($_ENV['TIDESIGHT_CONFIG'])) {
            return $_ENV['TIDESIGHT_CONFIG']; // where getenv() is disabled
        }
        foreach (array($entryDir, dirname(__DIR__)) as $dir) {
            $candidate = rtrim((string) $dir, '/\\') . DIRECTORY_SEPARATOR . 'tidesight.json';
            if (is_file($candidate)) {
                return $candidate;
            }
        }
        throw new ConfigError('no configuration: set TIDESIGHT_CONFIG or put tidesight.json next to index.php');
    }

    public static function load($entryDir)
    {
        $path = self::findPath($entryDir);
        $text = @file_get_contents($path);
        if ($text === false) {
            throw new ConfigError('cannot read the configuration file');
        }
        return new self($text, $path);
    }

    public function __construct($text, $path)
    {
        if (strncmp($text, "\xEF\xBB\xBF", 3) === 0) {
            $text = substr($text, 3);
        }
        $raw = json_decode($text, true, 512);
        if (!is_array($raw)) {
            throw new ConfigError('the configuration is not valid JSON: ' . json_last_error_msg());
        }
        if (!isset($raw['format']) || $raw['format'] !== self::FORMAT) {
            throw new ConfigError('unsupported configuration format (expected ' . self::FORMAT
                . '; generate it with `python -m tidesight.jobs config-json`)');
        }
        $this->path = $path;
        $this->text = $text;
        $this->server = self::merge(self::SERVER_DEFAULTS, isset($raw['server']) ? $raw['server'] : array());
        $this->headers = self::merge(self::HEADERS_DEFAULTS, isset($raw['headers']) ? $raw['headers'] : array());
        $this->privacy = self::merge(self::PRIVACY_DEFAULTS, isset($raw['privacy']) ? $raw['privacy'] : array());
        $this->sites = array();
        if (isset($raw['sites']) && is_array($raw['sites'])) {
            $public = isset($raw['public']) && is_array($raw['public']) ? $raw['public'] : array();
            foreach ($raw['sites'] as $id => $site) {
                if (is_array($site)) {
                    $this->sites[(string) $id] = self::siteDoc((string) $id, $site, $this->privacy,
                        isset($public[$id]) && is_array($public[$id]) ? $public[$id] : array());
                }
            }
        }
        $this->defaultRedact = self::redactPatternsOf(
            isset($raw['default_site']) && is_array($raw['default_site']) ? $raw['default_site'] : array(),
            isset($raw['public_default']) && is_array($raw['public_default']) ? $raw['public_default'] : array());
        $dataDir = (string) $this->server['data_dir'];
        if ($dataDir === '') {
            $dataDir = 'data';
        }
        if (!self::isAbsolute($dataDir)) { // relative paths are relative to the config file, as in Python
            $dataDir = dirname($path) . DIRECTORY_SEPARATOR . $dataDir;
        }
        $this->dataDir = $dataDir;
        $this->server['data_dir'] = $dataDir;
        foreach (array('busy_timeout_ms', 'max_payload_kb', 'max_chunk_kb', 'max_decompressed_kb',
                     'max_events_per_request', 'rate_limit_per_minute', 'rate_limit_kb_per_minute',
                     'chunk_rate_limit_per_minute', 'chunk_kb_per_minute', 'flagged_per_minute',
                     'flagged_global_per_minute', 'rate_limit_ipv6_prefix', 'min_free_disk_mb') as $key) {
            $this->server[$key] = (int) $this->server[$key];
        }
        if ($this->server['log_file']) {
            Log::$file = (string) $this->server['log_file'];
        }
    }

    private static function merge(array $defaults, $values)
    {
        if (!is_array($values)) {
            return $defaults;
        }
        foreach ($values as $k => $v) {
            $defaults[$k] = $v;
        }
        return $defaults;
    }

    /**
     * One site of Config.resolved(). A file written before sites carried `features` takes them
     * from the site's public configuration, which has always held the same map.
     */
    private static function siteDoc($id, array $site, array $privacy, array $public)
    {
        if (isset($site['features']) && is_array($site['features'])) {
            $features = $site['features'];
        } else {
            $features = isset($public['features']) && is_array($public['features']) ? $public['features'] : array();
        }
        return array(
            'id' => $id,
            'name' => isset($site['name']) ? $site['name'] : '',
            'origins' => isset($site['origins']) && is_array($site['origins']) ? array_values($site['origins']) : array(),
            'server_cookie_domain' => isset($site['server_cookie_domain']) ? (string) $site['server_cookie_domain'] : '',
            'cookie_days' => isset($site['cookie_days']) ? $site['cookie_days'] : 400,
            'privacy' => self::merge($privacy, isset($site['privacy']) ? $site['privacy'] : array()),
            'features' => $features,
            'max_hits_per_day' => isset($site['max_hits_per_day']) ? (int) $site['max_hits_per_day'] : self::MAX_HITS_PER_DAY,
            'redact' => self::redactPatternsOf($site, $public),
        );
    }

    /**
     * array(redact_params, redact_paths) as redact.patterns() reads them: from the site's document when
     * it has them, else from its public document (which has always carried the site's settings).
     */
    private static function redactPatternsOf(array $site, array $public)
    {
        $out = array();
        foreach (array('redact_params', 'redact_paths') as $key) {
            $value = array_key_exists($key, $site) ? $site[$key] : (isset($public[$key]) ? $public[$key] : array());
            $out[] = Redact::patterns($value);
        }
        return $out;
    }

    /** The URL redaction patterns of a site (Config::site()), or of the default site for null. */
    public function redactPatterns($site)
    {
        return $site !== null && isset($site['redact']) ? $site['redact'] : $this->defaultRedact;
    }

    private static function isAbsolute($p)
    {
        return $p !== '' && ($p[0] === '/' || $p[0] === '\\' || preg_match('/\A[A-Za-z]:[\\\\\/]/', $p) === 1);
    }

    /** Config.site(): the resolved site, or null when the id is empty or unknown. */
    public function site($siteId)
    {
        if ($siteId === null || $siteId === '') {
            return null;
        }
        return isset($this->sites[$siteId]) ? $this->sites[$siteId] : null;
    }

    /** The deployment secret: from the config, else the one Python keeps in <data_dir>/.secret. */
    public function secret()
    {
        if ($this->secret !== null) {
            return $this->secret;
        }
        if ((string) $this->server['secret'] !== '') {
            return $this->secret = (string) $this->server['secret'];
        }
        $path = $this->dataDir . DIRECTORY_SEPARATOR . '.secret';
        $value = @file_get_contents($path);
        if ($value !== false && trim($value) !== '') {
            return $this->secret = trim($value);
        }
        $bytes = function_exists('random_bytes') ? random_bytes(32)
            : (function_exists('openssl_random_pseudo_bytes') ? (string) openssl_random_pseudo_bytes(32) : '');
        if (strlen($bytes) !== 32 && function_exists('random_int')) { // the same CSPRNG, where only random_bytes is disabled
            $bytes = '';
            for ($i = 0; $i < 32; $i++) {
                $bytes .= chr(random_int(0, 255));
            }
        }
        if (strlen($bytes) !== 32) {
            throw new ConfigError('no secure random source to generate the secret: set [server] secret');
        }
        $value = bin2hex($bytes);
        Util::mkdirPrivate($this->dataDir);
        $old = Util::umask(Util::privateMask()); // created private at once, with no window before a chmod
        $fh = @fopen($path, 'x');
        Util::umask($old);
        if ($fh === false) { // another process created it first
            $again = @file_get_contents($path);
            if ($again !== false && trim($again) !== '') {
                return $this->secret = trim($again);
            }
            ErrorLog::warning($this->dataDir, 'could not persist the generated secret in ' . $this->dataDir);
            return $this->secret = $value;
        }
        if ($old === null && function_exists('chmod')) { // no umask(): private afterwards, as the next best thing
            @chmod($path, 0666 & ~Util::privateMask());
        }
        fwrite($fh, $value);
        fclose($fh);
        return $this->secret = $value;
    }

    /**
     * Config.public_site_config(site_id) as an object (for exact re-serialisation): the
     * precomputed entry for a known site, else the defaults with this site id and its "cv".
     */
    public function publicConfig($siteId)
    {
        if ($this->doc === null) {
            $this->doc = json_decode($this->text, false, 512);
        }
        $doc = $this->doc;
        if ($siteId !== '' && isset($this->sites[$siteId]) && isset($doc->public) && is_object($doc->public)
            && property_exists($doc->public, $siteId)) {
            return $doc->public->$siteId;
        }
        if (!isset($doc->public_default) || !is_object($doc->public_default)) {
            throw new ConfigError('the configuration has no public_default');
        }
        $cfg = clone $doc->public_default;
        if ($siteId === '' && isset($cfg->cv)) {
            return $cfg;
        }
        unset($cfg->cv);
        $cfg->site = $siteId;
        $cfg->known = false;
        $cfg->cv = substr(hash('sha256', Json::dumpsAscii($cfg, true)), 0, 12);
        return $cfg;
    }
}
