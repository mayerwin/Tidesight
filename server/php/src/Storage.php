<?php
/**
 * Raw storage, identical to server/python/tidesight/storage.py:
 *
 * - SqliteStore appends to events.db (raw_requests + raw_hits) and replay.db (chunks), one
 *   short BEGIN IMMEDIATE transaction per request, with the per-key rate limit applied in
 *   the same transaction. The schema is created by the same numbered migrations (DDL copied
 *   verbatim, tracked with PRAGMA user_version), so either connector can open the other's files.
 * - SpoolStore appends one JSON line per record to <data_dir>/spool/<stream>/<day>/<host>-<pid>.jsonl,
 *   the format `python -m tidesight.jobs load-spool` imports.
 * - FallbackStore writes to SQLite and falls back to the spool when SQLite fails.
 *
 * Uses the SQLite3 extension, or PDO with pdo_sqlite when SQLite3 is missing.
 */

namespace Tidesight;

final class StorageError extends \RuntimeException
{
}

/** A value to bind as a BLOB. */
final class Blob
{
    /** @var string */
    public $bytes;

    public function __construct($bytes)
    {
        $this->bytes = $bytes;
    }
}

final class HitRecord
{
    public $seq;
    public $type = null;
    public $name = null;
    public $client_ts = null;
    public $event_id = null;
    public $visitor_id = null;
    public $session_id = null;
    public $page_id = null;
    public $user_id = null;
    public $url = null;
    /** @var string the event as JSON text */
    public $data;

    public function __construct($seq, $data)
    {
        $this->seq = $seq;
        $this->data = $data;
    }

    /** HitRecord.to_dict() as JSON (spool line). */
    public function toJson()
    {
        return '{"seq":' . $this->seq . ',"type":' . self::s($this->type) . ',"name":' . self::s($this->name)
            . ',"client_ts":' . self::i($this->client_ts) . ',"event_id":' . self::s($this->event_id)
            . ',"visitor_id":' . self::s($this->visitor_id) . ',"session_id":' . self::s($this->session_id)
            . ',"page_id":' . self::s($this->page_id) . ',"user_id":' . self::s($this->user_id)
            . ',"url":' . self::s($this->url) . ',"data":' . Json::encodeString($this->data) . '}';
    }

    public static function s($v)
    {
        return $v === null ? 'null' : Json::encodeString((string) $v);
    }

    public static function i($v)
    {
        return $v === null ? 'null' : (string) (int) $v;
    }
}

/** Everything stored about one request to /collect or /p.gif. JSON columns are kept as text. */
final class RequestRecord
{
    public $received_at;
    public $endpoint;
    public $method;
    public $site_id = null;
    public $site_status;
    public $ip = null;
    /** @var string JSON object text */
    public $headers = '{}';
    /** @var string|null JSON object text */
    public $cookies = null;
    public $query = null;
    public $content_type = null;
    public $body_bytes = 0;
    public $body_encoding = null;
    /** @var string|null JSON object text */
    public $envelope = null;
    /** @var string|null raw bytes */
    public $body = null;
    public $error = null;
    public $proto_v = null;
    /** @var string|null JSON object text */
    public $server = null;
    /** @var HitRecord[] */
    public $hits = array();

    public function siteOk()
    {
        return $this->site_status === 'ok' ? 1 : 0;
    }

    /** RequestRecord.to_dict() as JSON (spool line), same key order as Python. */
    public function toJson()
    {
        $hits = array();
        foreach ($this->hits as $h) {
            $hits[] = $h->toJson();
        }
        return '{"received_at":' . (int) $this->received_at . ',"endpoint":' . HitRecord::s($this->endpoint)
            . ',"method":' . HitRecord::s($this->method) . ',"site_id":' . HitRecord::s($this->site_id)
            . ',"site_status":' . HitRecord::s($this->site_status) . ',"ip":' . HitRecord::s($this->ip)
            . ',"headers":' . $this->headers . ',"cookies":' . ($this->cookies === null ? 'null' : $this->cookies)
            . ',"query":' . HitRecord::s($this->query) . ',"content_type":' . HitRecord::s($this->content_type)
            . ',"body_bytes":' . (int) $this->body_bytes . ',"body_encoding":' . HitRecord::s($this->body_encoding)
            . ',"envelope":' . ($this->envelope === null ? 'null' : $this->envelope)
            . ',"error":' . HitRecord::s($this->error) . ',"proto_v":' . HitRecord::i($this->proto_v)
            . ',"server":' . ($this->server === null ? 'null' : $this->server)
            . ',"body":' . ($this->body === null ? 'null' : '"' . base64_encode($this->body) . '"')
            . ',"hits":[' . implode(',', $hits) . ']}';
    }
}

/** One heavy-data chunk for replay.db, stored without parsing the payload. */
final class ChunkRecord
{
    public $received_at;
    public $kind;
    public $site_id = null;
    public $site_status;
    /** @var string bytes */
    public $payload;
    public $encoding;
    /** @var string JSON object text */
    public $meta = '{}';
    public $visitor_id = null;
    public $session_id = null;
    public $page_id = null;
    public $seq = null;
    public $client_ts = null;
    public $url = null;
    public $ip = null;
    public $error = null;

    public function siteOk()
    {
        return $this->site_status === 'ok' ? 1 : 0;
    }

    /** ChunkRecord.to_dict() as JSON (spool line). */
    public function toJson()
    {
        return '{"received_at":' . (int) $this->received_at . ',"kind":' . HitRecord::s($this->kind)
            . ',"site_id":' . HitRecord::s($this->site_id) . ',"site_status":' . HitRecord::s($this->site_status)
            . ',"payload":"' . base64_encode($this->payload) . '","encoding":' . HitRecord::s($this->encoding)
            . ',"meta":' . $this->meta . ',"visitor_id":' . HitRecord::s($this->visitor_id)
            . ',"session_id":' . HitRecord::s($this->session_id) . ',"page_id":' . HitRecord::s($this->page_id)
            . ',"seq":' . HitRecord::i($this->seq) . ',"client_ts":' . HitRecord::i($this->client_ts)
            . ',"url":' . HitRecord::s($this->url) . ',"ip":' . HitRecord::s($this->ip)
            . ',"error":' . HitRecord::s($this->error) . '}';
    }
}

// ---------------------------------------------------------------------------
// Database access (PDO SQLite with a persistent connection, or the SQLite3 extension)
// ---------------------------------------------------------------------------

abstract class Db
{
    /**
     * The driver to use: "pdo" (persistent connection) when pdo_sqlite is available, else "sqlite3".
     * Force one with the TIDESIGHT_SQLITE_DRIVER constant or environment variable.
     *
     * Why PDO first: only PDO can keep a connection open across requests, like the Python
     * collector keeps one per process. With a fresh connection per request, the closing
     * connection is usually the last one, so SQLite checkpoints the WAL into the database
     * (with syncs) and deletes it on every request.
     */
    public static function driver()
    {
        $forced = defined('TIDESIGHT_SQLITE_DRIVER') ? (string) constant('TIDESIGHT_SQLITE_DRIVER')
            : (string) getenv('TIDESIGHT_SQLITE_DRIVER');
        $pdo = class_exists('PDO') && in_array('sqlite', \PDO::getAvailableDrivers(), true);
        $sqlite3 = class_exists('SQLite3');
        if ($forced === 'sqlite3' && $sqlite3) {
            return 'sqlite3';
        }
        if ($pdo) {
            return 'pdo';
        }
        if ($sqlite3) {
            return 'sqlite3';
        }
        throw new StorageError('neither PDO SQLite nor the SQLite3 extension is available');
    }

    /** Open a database with Tidesight's pragmas (storage.connect). */
    public static function open($path, $journalMode, $synchronous, $busyTimeoutMs)
    {
        $dir = dirname($path);
        if (!is_dir($dir)) {
            @mkdir($dir, 0777, true);
        }
        $db = self::driver() === 'pdo' ? new PdoDb($path, $busyTimeoutMs) : new Sqlite3Db($path, $busyTimeoutMs);
        $db->exec('PRAGMA busy_timeout=' . (int) $busyTimeoutMs);
        $mode = $db->scalar('PRAGMA journal_mode=' . ($journalMode === 'wal' ? 'WAL' : 'DELETE'));
        if (strtolower((string) $mode) !== $journalMode) {
            Log::warning(sprintf('journal_mode %s requested for %s, got %s', $journalMode, basename($path), $mode));
        }
        $db->exec('PRAGMA synchronous=' . ($synchronous === 'full' ? 'FULL' : 'NORMAL'));
        return $db;
    }

    abstract public function exec($sql);

    /** First column of the first row, or null. */
    abstract public function scalar($sql, array $params = array());

    /** Execute a statement (prepared once per connection); returns the last insert rowid. */
    abstract public function run($sql, array $params);

    abstract public function close();

    /** storage.migrate: apply pending migrations; safe when several processes race. */
    public function migrate(array $migrations)
    {
        $current = (int) $this->scalar('PRAGMA user_version');
        $last = $migrations[count($migrations) - 1];
        if ($current >= $last[0]) {
            return $current;
        }
        $this->exec('BEGIN IMMEDIATE');
        try {
            $current = (int) $this->scalar('PRAGMA user_version'); // re-check under the lock
            foreach ($migrations as $migration) {
                if ($migration[0] <= $current) {
                    continue;
                }
                foreach ($migration[1] as $sql) {
                    $this->exec($sql);
                }
                $this->exec('PRAGMA user_version=' . (int) $migration[0]);
                $current = $migration[0];
            }
            $this->exec('COMMIT');
        } catch (\Throwable $e) {
            $this->rollback();
            throw $e;
        }
        return $current;
    }

    public function rollback()
    {
        try {
            $this->exec('ROLLBACK');
        } catch (\Throwable $e) {
            // no transaction left to roll back
        }
    }
}

final class Sqlite3Db extends Db
{
    /** @var \SQLite3 */
    private $db;
    /** @var array<string,\SQLite3Stmt> */
    private $stmts = array();

    public function __construct($path, $busyTimeoutMs)
    {
        $this->db = new \SQLite3($path, SQLITE3_OPEN_READWRITE | SQLITE3_OPEN_CREATE);
        $this->db->enableExceptions(true);
        $this->db->busyTimeout((int) $busyTimeoutMs);
    }

    public function exec($sql)
    {
        $this->db->exec($sql);
    }

    public function scalar($sql, array $params = array())
    {
        if (!$params) {
            $v = $this->db->querySingle($sql);
            return $v === false ? null : $v;
        }
        $result = $this->stmt($sql, $params)->execute();
        $row = $result->fetchArray(SQLITE3_NUM);
        $result->finalize();
        return $row === false ? null : $row[0];
    }

    public function run($sql, array $params)
    {
        $result = $this->stmt($sql, $params)->execute();
        if ($result instanceof \SQLite3Result) {
            $result->finalize();
        }
        return $this->db->lastInsertRowID();
    }

    private function stmt($sql, array $params)
    {
        if (!isset($this->stmts[$sql])) {
            $this->stmts[$sql] = $this->db->prepare($sql);
        }
        $stmt = $this->stmts[$sql];
        $stmt->reset();
        $stmt->clear();
        $i = 1;
        foreach ($params as $v) {
            if ($v === null) {
                $stmt->bindValue($i, null, SQLITE3_NULL);
            } elseif (is_int($v)) {
                $stmt->bindValue($i, $v, SQLITE3_INTEGER);
            } elseif ($v instanceof Blob) {
                $stmt->bindValue($i, $v->bytes, SQLITE3_BLOB);
            } else {
                $stmt->bindValue($i, (string) $v, SQLITE3_TEXT);
            }
            $i++;
        }
        return $stmt;
    }

    public function close()
    {
        foreach ($this->stmts as $stmt) {
            @$stmt->close();
        }
        $this->stmts = array();
        @$this->db->close();
    }
}

/**
 * PDO SQLite with a persistent connection: it stays open in the PHP worker between requests
 * (PHP-FPM, mod_php, the built-in server), as the Python collector keeps one per process.
 * The persistent handle is keyed by the file's inode, so a replaced or restored database
 * file gets a new connection instead of writes to the old file.
 */
final class PdoDb extends Db
{
    /** @var \PDO|null */
    private $db;
    /** @var array<string,\PDOStatement> */
    private $stmts = array();
    /** @var bool */
    private $inTransaction = false;
    /** @var PdoDb[] connections of this request, rolled back at shutdown if a fatal error interrupted them */
    private static $live = array();

    public function __construct($path, $busyTimeoutMs)
    {
        $inode = is_file($path) ? @fileinode($path) : false;
        $this->db = new \PDO('sqlite:' . $path, 'tidesight-' . ($inode === false ? 'new' : $inode), null, array(
            \PDO::ATTR_ERRMODE => \PDO::ERRMODE_EXCEPTION,
            \PDO::ATTR_TIMEOUT => max(1, (int) ceil($busyTimeoutMs / 1000)),
            \PDO::ATTR_PERSISTENT => true,
        ));
        // A request that died mid-transaction (fatal error) may have left one open on this handle.
        try {
            $this->db->exec('ROLLBACK');
        } catch (\PDOException $e) {
            // "cannot rollback - no transaction is active": the normal case
        }
        if (!self::$live) {
            register_shutdown_function(array(__CLASS__, 'shutdown'));
        }
        self::$live[] = $this;
    }

    /** Roll back what a fatal error left open, so the persistent handle is clean for the next request. */
    public static function shutdown()
    {
        foreach (self::$live as $db) {
            if ($db->inTransaction && $db->db !== null) {
                try {
                    $db->db->exec('ROLLBACK');
                } catch (\Throwable $e) {
                    // nothing more can be done here
                }
            }
        }
        self::$live = array();
    }

    public function exec($sql)
    {
        $this->db->exec($sql);
        if ($sql === 'BEGIN IMMEDIATE') {
            $this->inTransaction = true;
        } elseif ($sql === 'COMMIT' || $sql === 'ROLLBACK') {
            $this->inTransaction = false;
        }
    }

    public function rollback()
    {
        parent::rollback();
        $this->inTransaction = false;
    }

    public function scalar($sql, array $params = array())
    {
        $stmt = $params ? $this->stmt($sql, $params) : $this->db->query($sql);
        if ($params) {
            $stmt->execute();
        }
        $v = $stmt->fetchColumn();
        $stmt->closeCursor();
        return $v === false ? null : $v;
    }

    public function run($sql, array $params)
    {
        $stmt = $this->stmt($sql, $params);
        $stmt->execute();
        $stmt->closeCursor();
        return (int) $this->db->lastInsertId();
    }

    private function stmt($sql, array $params)
    {
        if (!isset($this->stmts[$sql])) {
            $this->stmts[$sql] = $this->db->prepare($sql);
        }
        $stmt = $this->stmts[$sql];
        $i = 1;
        foreach ($params as $v) {
            if ($v === null) {
                $stmt->bindValue($i, null, \PDO::PARAM_NULL);
            } elseif (is_int($v)) {
                $stmt->bindValue($i, $v, \PDO::PARAM_INT);
            } elseif ($v instanceof Blob) {
                $stmt->bindValue($i, $v->bytes, \PDO::PARAM_LOB);
            } else {
                $stmt->bindValue($i, (string) $v, \PDO::PARAM_STR);
            }
            $i++;
        }
        return $stmt;
    }

    public function close()
    {
        $this->stmts = array();
        $this->db = null;
    }
}

// ---------------------------------------------------------------------------
// Stores
// ---------------------------------------------------------------------------

final class Storage
{
    const STORED = 'stored';
    const RATE_LIMITED = 'rate_limited';
    const SPOOLED = 'spooled';

    /** storage.EVENTS_MIGRATIONS, verbatim. */
    public static function eventsMigrations()
    {
        return array(
            array(1, array(
                "CREATE TABLE IF NOT EXISTS raw_requests (\n"
                . "            id            INTEGER PRIMARY KEY,\n"
                . "            received_at   INTEGER NOT NULL,\n"
                . "            site_id       TEXT,\n"
                . "            site_status   TEXT NOT NULL,\n"
                . "            endpoint      TEXT NOT NULL,\n"
                . "            method        TEXT NOT NULL,\n"
                . "            ip            TEXT,\n"
                . "            headers       TEXT NOT NULL,\n"
                . "            cookies       TEXT,\n"
                . "            query         TEXT,\n"
                . "            content_type  TEXT,\n"
                . "            body_bytes    INTEGER NOT NULL DEFAULT 0,\n"
                . "            body_encoding TEXT,\n"
                . "            envelope      TEXT,\n"
                . "            body          BLOB,\n"
                . "            error         TEXT,\n"
                . "            proto_v       INTEGER,\n"
                . "            event_count   INTEGER NOT NULL DEFAULT 0,\n"
                . "            server        TEXT\n"
                . "        )",
                "CREATE INDEX IF NOT EXISTS raw_requests_time ON raw_requests(received_at)",
                "CREATE TABLE IF NOT EXISTS raw_hits (\n"
                . "            id          INTEGER PRIMARY KEY,\n"
                . "            request_id  INTEGER NOT NULL,\n"
                . "            seq         INTEGER NOT NULL,\n"
                . "            received_at INTEGER NOT NULL,\n"
                . "            site_id     TEXT,\n"
                . "            site_ok     INTEGER NOT NULL,\n"
                . "            type        TEXT,\n"
                . "            name        TEXT,\n"
                . "            client_ts   INTEGER,\n"
                . "            event_id    TEXT,\n"
                . "            visitor_id  TEXT,\n"
                . "            session_id  TEXT,\n"
                . "            page_id     TEXT,\n"
                . "            user_id     TEXT,\n"
                . "            url         TEXT,\n"
                . "            data        TEXT NOT NULL\n"
                . "        )",
                "CREATE INDEX IF NOT EXISTS raw_hits_site_time ON raw_hits(site_id, received_at)",
                "CREATE INDEX IF NOT EXISTS raw_hits_request ON raw_hits(request_id)",
                "CREATE TABLE IF NOT EXISTS rate_limits (\n"
                . "            key    TEXT NOT NULL,\n"
                . "            minute INTEGER NOT NULL,\n"
                . "            hits   INTEGER NOT NULL DEFAULT 0,\n"
                . "            PRIMARY KEY (key, minute)\n"
                . "        ) WITHOUT ROWID",
                "CREATE TABLE IF NOT EXISTS counters (\n"
                . "            day     TEXT NOT NULL,\n"
                . "            site_id TEXT NOT NULL,\n"
                . "            name    TEXT NOT NULL,\n"
                . "            n       INTEGER NOT NULL DEFAULT 0,\n"
                . "            PRIMARY KEY (day, site_id, name)\n"
                . "        ) WITHOUT ROWID",
                "CREATE TABLE IF NOT EXISTS spool_progress (\n"
                . "            path   TEXT PRIMARY KEY,\n"
                . "            offset INTEGER NOT NULL,\n"
                . "            done   INTEGER NOT NULL DEFAULT 0\n"
                . "        )",
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)",
            )),
            // Privacy annotations: the geolocation derived before an IP was anonymised (JSON {provider: record}).
            array(2, array(
                "ALTER TABLE raw_requests ADD COLUMN geo TEXT",
            )),
        );
    }

    /** storage.REPLAY_MIGRATIONS, verbatim. */
    public static function replayMigrations()
    {
        return array(
            array(1, array(
                "CREATE TABLE IF NOT EXISTS chunks (\n"
                . "            id          INTEGER PRIMARY KEY,\n"
                . "            received_at INTEGER NOT NULL,\n"
                . "            site_id     TEXT,\n"
                . "            site_ok     INTEGER NOT NULL,\n"
                . "            kind        TEXT NOT NULL,\n"
                . "            visitor_id  TEXT,\n"
                . "            session_id  TEXT,\n"
                . "            page_id     TEXT,\n"
                . "            seq         INTEGER,\n"
                . "            client_ts   INTEGER,\n"
                . "            url         TEXT,\n"
                . "            ip          TEXT,\n"
                . "            meta        TEXT NOT NULL,\n"
                . "            encoding    TEXT NOT NULL,\n"
                . "            payload     BLOB NOT NULL,\n"
                . "            error       TEXT\n"
                . "        )",
                "CREATE INDEX IF NOT EXISTS chunks_session ON chunks(site_id, session_id, page_id, seq)",
                "CREATE INDEX IF NOT EXISTS chunks_time ON chunks(received_at)",
                "CREATE TABLE IF NOT EXISTS rate_limits (\n"
                . "            key    TEXT NOT NULL,\n"
                . "            minute INTEGER NOT NULL,\n"
                . "            hits   INTEGER NOT NULL DEFAULT 0,\n"
                . "            PRIMARY KEY (key, minute)\n"
                . "        ) WITHOUT ROWID",
                "CREATE TABLE IF NOT EXISTS spool_progress (\n"
                . "            path   TEXT PRIMARY KEY,\n"
                . "            offset INTEGER NOT NULL,\n"
                . "            done   INTEGER NOT NULL DEFAULT 0\n"
                . "        )",
            )),
        );
    }

    /** storage.open_store: the store described by the [server] config. */
    public static function open(Config $cfg)
    {
        $s = $cfg->server;
        if ($s['storage_mode'] === 'spool') {
            return new SpoolStore($cfg->dataDir);
        }
        $primary = new SqliteStore($cfg->dataDir, (string) $s['journal_mode'], (string) $s['synchronous'],
            (int) $s['busy_timeout_ms']);
        if ($s['spool_fallback']) {
            return new FallbackStore($primary, new SpoolStore($cfg->dataDir));
        }
        return $primary;
    }
}

final class SqliteStore
{
    public $name = 'sqlite';
    private $dataDir;
    private $journalMode;
    private $synchronous;
    private $busyTimeoutMs;
    /** @var array<string,Db> */
    private $conns = array();

    public function __construct($dataDir, $journalMode = 'wal', $synchronous = 'normal', $busyTimeoutMs = 10000)
    {
        $this->dataDir = $dataDir;
        $this->journalMode = $journalMode;
        $this->synchronous = $synchronous;
        $this->busyTimeoutMs = $busyTimeoutMs;
    }

    private function conn($which)
    {
        if (!isset($this->conns[$which])) {
            $path = $this->dataDir . DIRECTORY_SEPARATOR . ($which === 'events' ? 'events.db' : 'replay.db');
            $db = Db::open($path, $this->journalMode, $this->synchronous, $this->busyTimeoutMs);
            try {
                $db->migrate($which === 'events' ? Storage::eventsMigrations() : Storage::replayMigrations());
            } catch (\Throwable $e) {
                $db->close();
                throw $e;
            }
            $this->conns[$which] = $db;
        }
        return $this->conns[$which];
    }

    private function discard($which)
    {
        if (isset($this->conns[$which])) {
            $this->conns[$which]->close();
            unset($this->conns[$which]);
        }
    }

    public function close()
    {
        foreach ($this->conns as $db) {
            $db->close();
        }
        $this->conns = array();
    }

    /** Count this request; true when the key exceeded $limit requests this minute. */
    private function rateLimited(Db $db, $key, $limit, $nowMs)
    {
        if ($key === null || $key === '' || $limit <= 0) {
            return false;
        }
        $minute = intdiv($nowMs, 60000);
        $db->run('INSERT OR IGNORE INTO rate_limits(key, minute, hits) VALUES (?, ?, 0)', array($key, $minute));
        $db->run('UPDATE rate_limits SET hits = hits + 1 WHERE key = ? AND minute = ?', array($key, $minute));
        $hits = (int) $db->scalar('SELECT hits FROM rate_limits WHERE key = ? AND minute = ?', array($key, $minute));
        // Python prunes at most every 10 minutes per process; PHP keeps no state between
        // requests, so prune on about one request in a hundred.
        if (mt_rand(0, 99) === 0) {
            $db->run('DELETE FROM rate_limits WHERE minute < ?', array($minute - 60));
        }
        return $hits > $limit;
    }

    public static function count(Db $db, $day, $siteId, $name, $n = 1)
    {
        $site = $siteId === null ? '' : (string) $siteId;
        $db->run('INSERT OR IGNORE INTO counters(day, site_id, name, n) VALUES (?, ?, ?, 0)', array($day, $site, $name));
        $db->run('UPDATE counters SET n = n + ? WHERE day = ? AND site_id = ? AND name = ?',
            array((int) $n, $day, $site, $name));
    }

    public function writeRequest(RequestRecord $rec, $rateKey = null, $rateLimit = 0)
    {
        try {
            $db = $this->conn('events');
            $db->exec('BEGIN IMMEDIATE');
            try {
                if ($this->rateLimited($db, $rateKey, $rateLimit, $rec->received_at)) {
                    self::count($db, Util::day($rec->received_at), $rec->site_id, 'rate_limited');
                    $db->exec('COMMIT');
                    return Storage::RATE_LIMITED;
                }
                $requestId = self::insertRequest($db, $rec);
                self::insertHits($db, $requestId, $rec);
                $db->exec('COMMIT');
            } catch (\Throwable $e) {
                $db->rollback();
                throw $e;
            }
        } catch (\Throwable $e) {
            $this->discard('events');
            throw $e;
        }
        return Storage::STORED;
    }

    public static function insertRequest(Db $db, RequestRecord $rec)
    {
        return $db->run(
            'INSERT INTO raw_requests(received_at, site_id, site_status, endpoint, method, ip, headers, '
            . 'cookies, query, content_type, body_bytes, body_encoding, envelope, body, error, proto_v, '
            . 'event_count, server) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            array((int) $rec->received_at, $rec->site_id, $rec->site_status, $rec->endpoint, $rec->method, $rec->ip,
                $rec->headers === null ? '{}' : $rec->headers, $rec->cookies, $rec->query, $rec->content_type,
                (int) $rec->body_bytes, $rec->body_encoding, $rec->envelope,
                $rec->body === null ? null : new Blob($rec->body), $rec->error, $rec->proto_v,
                count($rec->hits), $rec->server));
    }

    public static function insertHits(Db $db, $requestId, RequestRecord $rec)
    {
        $siteOk = $rec->siteOk();
        foreach ($rec->hits as $h) {
            $db->run(
                'INSERT INTO raw_hits(request_id, seq, received_at, site_id, site_ok, type, name, client_ts, '
                . 'event_id, visitor_id, session_id, page_id, user_id, url, data) '
                . 'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                array((int) $requestId, (int) $h->seq, (int) $rec->received_at, $rec->site_id, $siteOk, $h->type,
                    $h->name, $h->client_ts, $h->event_id, $h->visitor_id, $h->session_id, $h->page_id,
                    $h->user_id, $h->url, $h->data));
        }
    }

    public function writeChunk(ChunkRecord $rec, $rateKey = null, $rateLimit = 0)
    {
        try {
            $db = $this->conn('replay');
            $db->exec('BEGIN IMMEDIATE');
            try {
                if ($this->rateLimited($db, $rateKey, $rateLimit, $rec->received_at)) {
                    $db->exec('COMMIT');
                    return Storage::RATE_LIMITED;
                }
                $db->run(
                    'INSERT INTO chunks(received_at, site_id, site_ok, kind, visitor_id, session_id, page_id, seq, '
                    . 'client_ts, url, ip, meta, encoding, payload, error) '
                    . 'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                    array((int) $rec->received_at, $rec->site_id, $rec->siteOk(), $rec->kind, $rec->visitor_id,
                        $rec->session_id, $rec->page_id, $rec->seq, $rec->client_ts, $rec->url, $rec->ip,
                        $rec->meta === null ? '{}' : $rec->meta, $rec->encoding, new Blob($rec->payload),
                        $rec->error));
                $db->exec('COMMIT');
            } catch (\Throwable $e) {
                $db->rollback();
                throw $e;
            }
        } catch (\Throwable $e) {
            $this->discard('replay');
            throw $e;
        }
        return Storage::STORED;
    }

    public function recordCounter($day, $siteId, $name, $n = 1)
    {
        $db = $this->conn('events');
        $db->exec('BEGIN IMMEDIATE');
        try {
            self::count($db, $day, $siteId, $name, $n);
            $db->exec('COMMIT');
        } catch (\Throwable $e) {
            $db->rollback();
            $this->discard('events');
            throw $e;
        }
    }

    public function healthy()
    {
        try {
            $this->conn('events')->scalar('SELECT 1 FROM raw_requests LIMIT 1');
            return is_writable($this->dataDir);
        } catch (\Throwable $e) {
            $this->discard('events');
            return false;
        }
    }
}

/**
 * Appends one JSON line per record to a per-process file (storage.SpoolStore).
 * Layout: <data_dir>/spool/events/<YYYY-MM-DD>/<host>-<pid>.jsonl and the same under chunks/.
 * PHP keeps no memory between requests, so this mode applies no rate limit.
 */
final class SpoolStore
{
    public $name = 'spool';
    private $root;
    private $host;

    public function __construct($dataDir)
    {
        $this->root = $dataDir . DIRECTORY_SEPARATOR . 'spool';
        $host = gethostname();
        $parts = explode('.', $host === false ? '' : $host);
        $this->host = $parts[0] !== '' ? $parts[0] : 'host';
    }

    private function append($stream, $ms, $line)
    {
        $dir = $this->root . DIRECTORY_SEPARATOR . $stream . DIRECTORY_SEPARATOR . Util::day($ms);
        if (!is_dir($dir) && !@mkdir($dir, 0777, true) && !is_dir($dir)) {
            throw new StorageError('cannot create the spool directory');
        }
        $path = $dir . DIRECTORY_SEPARATOR . $this->host . '-' . getmypid() . '.jsonl';
        $new = !file_exists($path);
        // One write of a whole line, under an exclusive lock (threaded SAPIs share a pid).
        if (@file_put_contents($path, $line . "\n", FILE_APPEND | LOCK_EX) === false) {
            throw new StorageError('cannot append to the spool');
        }
        if ($new) {
            @chmod($path, 0600);
        }
    }

    public function writeRequest(RequestRecord $rec, $rateKey = null, $rateLimit = 0)
    {
        $this->append('events', $rec->received_at, '{"k":"request","rec":' . $rec->toJson() . '}');
        return Storage::STORED;
    }

    public function writeChunk(ChunkRecord $rec, $rateKey = null, $rateLimit = 0)
    {
        $this->append('chunks', $rec->received_at, '{"k":"chunk","rec":' . $rec->toJson() . '}');
        return Storage::STORED;
    }

    public function recordCounter($day, $siteId, $name, $n = 1)
    {
        $this->append('events', Util::nowMs(), '{"k":"counter","day":' . HitRecord::s($day) . ',"site_id":'
            . HitRecord::s($siteId) . ',"name":' . HitRecord::s($name) . ',"n":' . (int) $n . '}');
    }

    public function healthy()
    {
        if (!is_dir($this->root)) {
            @mkdir($this->root, 0777, true);
        }
        return is_dir($this->root) && is_writable($this->root);
    }

    public function close()
    {
    }
}

/** SQLite first; on a database error, append to the spool instead (never lose a hit). */
final class FallbackStore
{
    public $name;
    private $primary;
    private $spool;

    public function __construct(SqliteStore $primary, SpoolStore $spool)
    {
        $this->primary = $primary;
        $this->spool = $spool;
        $this->name = $primary->name;
    }

    private function call($method, array $args)
    {
        try {
            return call_user_func_array(array($this->primary, $method), $args);
        } catch (\Throwable $e) {
            Log::error(sprintf('SQLite write failed (%s); writing to the spool instead', $e->getMessage()));
            try {
                call_user_func_array(array($this->spool, $method), $args);
                return Storage::SPOOLED;
            } catch (\Throwable $e2) {
                throw new StorageError(sprintf('could not store the record: %s / %s', $e->getMessage(), $e2->getMessage()));
            }
        }
    }

    public function writeRequest(RequestRecord $rec, $rateKey = null, $rateLimit = 0)
    {
        return $this->call('writeRequest', array($rec, $rateKey, $rateLimit));
    }

    public function writeChunk(ChunkRecord $rec, $rateKey = null, $rateLimit = 0)
    {
        return $this->call('writeChunk', array($rec, $rateKey, $rateLimit));
    }

    public function recordCounter($day, $siteId, $name, $n = 1)
    {
        try {
            $this->primary->recordCounter($day, $siteId, $name, $n);
        } catch (\Throwable $e) {
            $this->spool->recordCounter($day, $siteId, $name, $n);
        }
    }

    public function healthy()
    {
        return $this->primary->healthy() || $this->spool->healthy();
    }

    public function close()
    {
        $this->primary->close();
        $this->spool->close();
    }
}
