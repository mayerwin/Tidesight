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
        'max_decompressed_kb' => 4096,
        'max_events_per_request' => 500,
        'rate_limit_per_minute' => 600,
        'rate_limit_key' => 'remote_addr',
        'cors' => 'reflect',
        'accept_ch' => true,
        'public_url' => '',
        'secret' => '',
        'static_dir' => '',
        'dashboard_dir' => '',
        'store_raw_body' => false,
        'spool_fallback' => true,
        'log_file' => '',
    );

    const HEADERS_DEFAULTS = array(
        'secret_patterns' => array('*token*', '*key*', '*secret*', '*password*'),
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

    /** The config file to use: TIDESIGHT_CONFIG (constant or environment), else tidesight.json nearby. */
    public static function findPath($entryDir)
    {
        if (defined('TIDESIGHT_CONFIG') && constant('TIDESIGHT_CONFIG')) {
            return (string) constant('TIDESIGHT_CONFIG');
        }
        $env = getenv('TIDESIGHT_CONFIG');
        if ($env !== false && $env !== '') {
            return $env;
        }
        foreach (array('TIDESIGHT_CONFIG', 'REDIRECT_TIDESIGHT_CONFIG') as $key) {
            if (!empty($_SERVER[$key]) && is_string($_SERVER[$key])) {
                return $_SERVER[$key];
            }
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
            foreach ($raw['sites'] as $id => $site) {
                if (is_array($site)) {
                    $this->sites[(string) $id] = self::siteDoc($site, $this->privacy);
                }
            }
        }
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
                     'max_events_per_request', 'rate_limit_per_minute') as $key) {
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

    private static function siteDoc(array $site, array $privacy)
    {
        return array(
            'name' => isset($site['name']) ? $site['name'] : '',
            'origins' => isset($site['origins']) && is_array($site['origins']) ? array_values($site['origins']) : array(),
            'server_cookie_domain' => isset($site['server_cookie_domain']) ? (string) $site['server_cookie_domain'] : '',
            'cookie_days' => isset($site['cookie_days']) ? $site['cookie_days'] : 400,
            'privacy' => self::merge($privacy, isset($site['privacy']) ? $site['privacy'] : array()),
        );
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
        $value = bin2hex(random_bytes(32));
        @mkdir($this->dataDir, 0777, true);
        $fh = @fopen($path, 'x');
        if ($fh === false) { // another process created it first
            $again = @file_get_contents($path);
            if ($again !== false && trim($again) !== '') {
                return $this->secret = trim($again);
            }
            Log::warning('could not persist the generated secret in the data directory');
            return $this->secret = $value;
        }
        fwrite($fh, $value);
        fclose($fh);
        @chmod($path, 0600);
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
