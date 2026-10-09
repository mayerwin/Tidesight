<?php
/**
 * Tidesight PHP connector: bootstrap.
 *
 * The PHP connector implements the wire protocol (docs/protocol.md) and writes
 * exactly the same SQLite schema and rows as the reference Python collector
 * (server/python/tidesight/collect.py + storage.py), so the Python enrichment
 * jobs and the dashboard work on its data unchanged. It is checked by the shared
 * conformance suite (server/python/tests/test_conformance.py).
 *
 * Entry points:
 *   index.php   Apache / shared hosting (with .htaccess)
 *   router.php  PHP's built-in server:  php -S 127.0.0.1:8080 server/php/router.php
 *
 * Configuration: the resolved JSON written by `python -m tidesight.jobs config-json`,
 * found through the TIDESIGHT_CONFIG constant or environment variable, or a
 * tidesight.json next to the entry script. See README.md.
 *
 * Requirements: PHP 7.4+ (64-bit), the SQLite3 extension (or PDO with pdo_sqlite),
 * zlib, json, pcre. No Composer dependencies.
 */

namespace Tidesight;

/** Connector version: the protocol/schema generation it implements (same as the Python package). */
const VERSION = '0.1.0';

/** Class name => file under src/ (loaded on first use, so rarely used code costs nothing). */
const CLASS_FILES = array(
    'Tidesight\\Util' => 'Util.php',
    'Tidesight\\Log' => 'Util.php',
    'Tidesight\\ErrorLog' => 'Util.php',
    'Tidesight\\Ip' => 'Ip.php',
    'Tidesight\\Redact' => 'Redact.php',
    'Tidesight\\Json' => 'Json.php',
    'Tidesight\\JsonObject' => 'Json.php',
    'Tidesight\\JsonBigInt' => 'Json.php',
    'Tidesight\\JsonParser' => 'JsonParser.php',
    'Tidesight\\JsonDecodeError' => 'JsonParser.php',
    'Tidesight\\Config' => 'Config.php',
    'Tidesight\\ConfigError' => 'Config.php',
    'Tidesight\\HitRecord' => 'Storage.php',
    'Tidesight\\RequestRecord' => 'Storage.php',
    'Tidesight\\ChunkRecord' => 'Storage.php',
    'Tidesight\\StorageError' => 'Storage.php',
    'Tidesight\\Budget' => 'Storage.php',
    'Tidesight\\Limits' => 'Storage.php',
    'Tidesight\\ProcessBudgets' => 'Storage.php',
    'Tidesight\\Blob' => 'Storage.php',
    'Tidesight\\Db' => 'Storage.php',
    'Tidesight\\Sqlite3Db' => 'Storage.php',
    'Tidesight\\PdoDb' => 'Storage.php',
    'Tidesight\\SqliteStore' => 'Storage.php',
    'Tidesight\\SpoolStore' => 'Storage.php',
    'Tidesight\\FallbackStore' => 'Storage.php',
    'Tidesight\\Storage' => 'Storage.php',
    'Tidesight\\Request' => 'Collector.php',
    'Tidesight\\Response' => 'Collector.php',
    'Tidesight\\Collector' => 'Collector.php',
);

spl_autoload_register(function ($class) {
    $files = CLASS_FILES;
    if (isset($files[$class])) {
        require_once __DIR__ . '/src/' . $files[$class];
    }
});

/**
 * Handle the current HTTP request.
 *
 * @param string|null $entryDir directory of the entry script (index.php / router.php), where a
 *                              tidesight.json may live; defaults to this file's directory.
 */
function run($entryDir = null)
{
    if (function_exists('header_remove')) {
        @header_remove('X-Powered-By');
    }
    // The collector must never print PHP diagnostics into a response, and must finish
    // its write even when the browser goes away (sendBeacon does not wait). Hosts disable
    // some of these functions: each is optional.
    if (function_exists('ini_set')) {
        @ini_set('display_errors', '0');
        @ini_set('html_errors', '0');
        @ini_set('default_mimetype', '');
        @ini_set('default_charset', ''); // send Content-Type exactly as given (PHP appends a charset to text/*)
    }
    if (function_exists('ignore_user_abort')) {
        @ignore_user_abort(true);
    }
    // Files and directories the collector creates are private (PHP restores the umask after the request).
    Util::umask(Util::privateMask());
    $unsupported = unsupportedRuntime();
    if ($unsupported !== null) {
        try { // every request repeats this: the error budget, its state next to the configuration file if any
            $stateDir = dirname(Config::findPath($entryDir === null ? __DIR__ : $entryDir));
        } catch (\Throwable $e) {
            $stateDir = null;
        }
        ErrorLog::error($stateDir, 'refusing to run: ' . $unsupported);
        Response::send(503, array(array('Content-Type', 'text/plain')), "configuration error\n");
        return;
    }
    Json::init();
    $collector = new Collector($entryDir === null ? __DIR__ : $entryDir);
    $collector->handle(Request::fromGlobals());
}

/** Why this PHP cannot run the collector correctly, or null. */
function unsupportedRuntime()
{
    if (PHP_INT_SIZE < 8) {
        return '32-bit PHP: timestamps in milliseconds need 64-bit integers';
    }
    if (function_exists('ini_get') && (int) ini_get('mbstring.func_overload') !== 0) {
        return 'mbstring.func_overload is on: byte strings would be read as characters';
    }
    return null;
}
