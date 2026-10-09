<?php
/**
 * Raw storage, identical to server/python/tidesight/storage.py:
 *
 * - SqliteStore appends to events.db (raw_requests + raw_hits) and replay.db (chunks), one
 *   short BEGIN IMMEDIATE transaction per request, with the allowances (rate_limits) and the
 *   site's daily quota checked in the same transaction. The schema is created by the same
 *   numbered migrations (DDL copied verbatim, tracked with PRAGMA user_version), so either
 *   connector can open the other's files.
 * - SpoolStore appends one JSON line per record to <data_dir>/spool/<stream>/<day>/<host>-<pid>.jsonl,
 *   the format `python -m tidesight.jobs load-spool` imports. Its allowances are per process.
 * - FallbackStore writes to SQLite and falls back to the spool when SQLite fails.
 *
 * Uses PDO with pdo_sqlite, or the SQLite3 extension when PDO SQLite is missing.
 */

namespace Tidesight;

final class StorageError extends \RuntimeException
{
}

/** storage.Budget: a per-minute allowance, counted in rate_limits in the same transaction as the write. */
final class Budget
{
    /** @var string "r:<client>" (requests), "kb:<client>" (kilobytes), "f:<client>" and "*f" (flagged requests) */
    public $key;
    /** @var int what this request consumes: 1, or its size in KB */
    public $amount;
    /** @var int per clock minute; 0 = unlimited */
    public $limit;
    /** @var string the counters name recorded when the allowance is exceeded */
    public $counter;

    public function __construct($key, $amount, $limit, $counter)
    {
        $this->key = (string) $key;
        $this->amount = (int) $amount;
        $this->limit = (int) $limit;
        $this->counter = (string) $counter;
    }
}

/** storage.Limits: everything a write must fit in. The first budget exceeded refuses the record. */
final class Limits
{
    /** @var Budget[] checked in order */
    public $budgets = array();
    /** @var string counters site_id: a configured site, or "" (never an id taken from the request) */
    public $site = '';
    /** @var int the site's stored hits per UTC day; 0 = unlimited */
    public $quota = 0;
    /** @var int hits this request adds (counted per site and day as the counter "hits") */
    public $hits = 0;
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
            : (function_exists('getenv') ? (string) getenv('TIDESIGHT_SQLITE_DRIVER') : '');
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
        Util::mkdirPrivate($dir);
        $db = self::driver() === 'pdo' ? new PdoDb($path, $busyTimeoutMs) : new Sqlite3Db($path, $busyTimeoutMs);
        $db->exec('PRAGMA busy_timeout=' . (int) $busyTimeoutMs);
        $mode = $db->scalar('PRAGMA journal_mode=' . ($journalMode === 'wal' ? 'WAL' : 'DELETE'));
        if (strtolower((string) $mode) !== $journalMode) { // per connection: per request with the SQLite3 driver
            ErrorLog::error($dir, sprintf('journal_mode %s requested for %s, got %s', $journalMode, basename($path), $mode));
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
        $inode = is_file($path) && function_exists('fileinode') ? @fileinode($path) : false;
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
        if (!self::$live && function_exists('register_shutdown_function')) {
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
            // Old allowance minutes are deleted by minute: without an index, a flood's rows made that a full scan.
            array(3, array(
                "CREATE INDEX IF NOT EXISTS rate_limits_minute ON rate_limits(minute)",
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
            array(2, array(
                "CREATE INDEX IF NOT EXISTS rate_limits_minute ON rate_limits(minute)",
            )),
        );
    }

    /**
     * storage.is_flagged: a request stored only as a flagged record (wrong or unknown site or origin,
     * unreadable, no events or too many). A batch with a few invalid events or a bad version still
     * holds good hits and is not flagged; a pixel is flagged only by its site status.
     */
    public static function isFlagged(RequestRecord $rec)
    {
        if ($rec->site_status !== 'ok') {
            return true;
        }
        if ($rec->endpoint === 'pixel') {
            return false;
        }
        if ($rec->envelope === null) {
            return true;
        }
        foreach (explode(',', (string) $rec->error) as $e) {
            if ($e === 'no_events' || strncmp($e, 'too_many_events', 15) === 0) {
                return true;
            }
        }
        return false;
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

    /**
     * storage.SQLiteStore._over_budget: count this request against its budgets; the counter name of
     * the first one exceeded, else null. A refused request still counts in the budgets it reached.
     */
    private function overBudget(Db $db, Limits $limits, $nowMs)
    {
        $minute = intdiv((int) $nowMs, 60000);
        $refused = null;
        foreach ($limits->budgets as $b) {
            if ($b->key === '' || $b->limit <= 0) {
                continue;
            }
            $db->run('INSERT OR IGNORE INTO rate_limits(key, minute, hits) VALUES (?, ?, 0)', array($b->key, $minute));
            $db->run('UPDATE rate_limits SET hits = hits + ? WHERE key = ? AND minute = ?',
                array($b->amount, $b->key, $minute));
            $used = Util::number($db->scalar('SELECT hits FROM rate_limits WHERE key = ? AND minute = ?',
                array($b->key, $minute)));
            if ($used > $b->limit) {
                $refused = $b->counter;
                break;
            }
        }
        // Python prunes at most once a minute per process; PHP keeps no state between requests, so
        // prune on about one request in a hundred. A bounded batch (the index on minute finds it), and a
        // failure (an old SQLite without row values) never costs the write.
        if (mt_rand(0, 99) === 0) {
            try {
                $db->run('DELETE FROM rate_limits WHERE (key, minute) IN (SELECT key, minute FROM rate_limits '
                    . 'WHERE minute < ? LIMIT 20000)', array($minute - 60));
            } catch (\Throwable $e) {
                // the old minutes stay for another time
            }
        }
        return $refused;
    }

    /** storage.SQLiteStore._over_quota: true when this request's hits would exceed the site's daily quota. */
    private static function overQuota(Db $db, Limits $limits, $day)
    {
        if ($limits->quota <= 0 || $limits->hits <= 0) {
            return false;
        }
        $n = $db->scalar("SELECT n FROM counters WHERE day = ? AND site_id = ? AND name = 'hits'",
            array($day, $limits->site));
        return Util::number($n) + $limits->hits > $limits->quota;
    }

    public static function count(Db $db, $day, $siteId, $name, $n = 1)
    {
        $site = $siteId === null ? '' : (string) $siteId;
        $db->run('INSERT OR IGNORE INTO counters(day, site_id, name, n) VALUES (?, ?, ?, 0)', array($day, $site, $name));
        $db->run('UPDATE counters SET n = n + ? WHERE day = ? AND site_id = ? AND name = ?',
            array((int) $n, $day, $site, $name));
    }

    public function writeRequest(RequestRecord $rec, Limits $limits)
    {
        $day = Util::day($rec->received_at);
        try {
            $db = $this->conn('events');
            $db->exec('BEGIN IMMEDIATE');
            try {
                $refused = $this->overBudget($db, $limits, $rec->received_at);
                if ($refused === null && self::overQuota($db, $limits, $day)) {
                    $refused = 'over_quota';
                }
                if ($refused !== null) {
                    self::count($db, $day, $limits->site, $refused);
                    $db->exec('COMMIT');
                    return Storage::RATE_LIMITED;
                }
                $requestId = self::insertRequest($db, $rec);
                self::insertHits($db, $requestId, $rec);
                if ($limits->site !== '' && $limits->hits > 0) {
                    self::count($db, $day, $limits->site, 'hits', $limits->hits);
                }
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

    public function writeChunk(ChunkRecord $rec, Limits $limits)
    {
        try {
            $db = $this->conn('replay');
            $db->exec('BEGIN IMMEDIATE');
            try {
                $refused = $this->overBudget($db, $limits, $rec->received_at);
                if ($refused === null) {
                    $db->run(
                        'INSERT INTO chunks(received_at, site_id, site_ok, kind, visitor_id, session_id, page_id, seq, '
                        . 'client_ts, url, ip, meta, encoding, payload, error) '
                        . 'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
                        array((int) $rec->received_at, $rec->site_id, $rec->siteOk(), $rec->kind, $rec->visitor_id,
                            $rec->session_id, $rec->page_id, $rec->seq, $rec->client_ts, $rec->url, $rec->ip,
                            $rec->meta === null ? '{}' : $rec->meta, $rec->encoding, new Blob($rec->payload),
                            $rec->error));
                }
                $db->exec('COMMIT');
            } catch (\Throwable $e) {
                $db->rollback();
                throw $e;
            }
        } catch (\Throwable $e) {
            $this->discard('replay');
            throw $e;
        }
        if ($refused !== null) { // counters live in events.db: a separate, best-effort write
            try {
                $this->recordCounter(Util::day($rec->received_at), $limits->site, $refused);
            } catch (\Throwable $e) {
                ErrorLog::error($this->dataDir, 'could not count a refused chunk');
            }
            return Storage::RATE_LIMITED;
        }
        return Storage::STORED;
    }

    /**
     * storage.SQLiteStore.usage: what the current minute has used of these allowance keys, in the
     * stream's database ("events" or "chunks"). Read only: no lock is taken.
     */
    public function usage($stream, array $keys, $nowMs)
    {
        $which = $stream === 'events' ? 'events' : 'replay';
        $out = array();
        try {
            $db = $this->conn($which);
            foreach ($keys as $k) {
                $n = $db->scalar('SELECT hits FROM rate_limits WHERE minute = ? AND key = ?',
                    array(intdiv((int) $nowMs, 60000), (string) $k));
                if ($n !== null) {
                    $out[(string) $k] = Util::number($n);
                }
            }
        } catch (\Throwable $e) {
            $this->discard($which);
            throw $e;
        }
        return $out;
    }

    /**
     * storage.SQLiteStore.refuse: charge a request refused before its body was parsed (its client was
     * already over an allowance) and count the refusal; the counter name, or null when the allowance had
     * just been renewed (a new minute).
     */
    public function refuse($stream, Limits $limits, $nowMs)
    {
        $which = $stream === 'events' ? 'events' : 'replay';
        try {
            $db = $this->conn($which);
            $db->exec('BEGIN IMMEDIATE');
            try {
                $refused = $this->overBudget($db, $limits, $nowMs);
                if ($refused !== null && $which === 'events') {
                    self::count($db, Util::day($nowMs), $limits->site, $refused);
                }
                $db->exec('COMMIT');
            } catch (\Throwable $e) {
                $db->rollback();
                throw $e;
            }
        } catch (\Throwable $e) {
            $this->discard($which);
            throw $e;
        }
        if ($refused !== null && $which === 'replay') { // counters live in events.db
            try {
                $this->recordCounter(Util::day($nowMs), $limits->site, $refused);
            } catch (\Throwable $e) {
                ErrorLog::error($this->dataDir, 'could not count a refused chunk');
            }
        }
        return $refused;
    }

    /**
     * Count something; with $limits (a Limits), only while its budgets hold. The budgets are consumed
     * either way, and a refusal is not itself counted.
     */
    public function recordCounter($day, $siteId, $name, $n = 1, $limits = null)
    {
        $db = $this->conn('events');
        $db->exec('BEGIN IMMEDIATE');
        try {
            if ($this->overBudget($db, $limits instanceof Limits ? $limits : new Limits(), Util::nowMs()) === null) {
                self::count($db, $day, $siteId, $name, $n);
            }
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
 * The spool store's per-process state, like the dicts of storage.SpoolStore (approximate when
 * several processes serve requests): the allowances of the current minute, the daily quotas, and
 * refusal counts not yet written.
 *
 * PHP forgets its variables after each request, but a persistent PDO connection lives on in its
 * worker process (PHP-FPM, mod_php, the built-in server), and with it an in-memory SQLite
 * database: the counts are kept there. Without pdo_sqlite, or under plain CGI (a new process per
 * request), nothing is counted and no allowance applies in spool mode.
 */
final class ProcessBudgets
{
    /** The file that holds the counts where a process lives for one request only (plain CGI). */
    const CGI_FILE = 'spool-allowances.db';

    /** @var array<string,\PDO|false> per data directory, for the current request */
    private static $dbs = array();
    /** @var array<string,bool> data directories whose counts live in CGI_FILE (shared by processes) */
    private static $shared = array();

    /**
     * Whether this process serves more than one request (PHP-FPM, mod_php, FastCGI, the built-in server):
     * not under plain CGI or suPHP, where php-cgi starts for each request (FastCGI sets FCGI_ROLE).
     */
    private static function persistent()
    {
        if (PHP_SAPI === 'cgi') {
            return false;
        }
        return PHP_SAPI !== 'cgi-fcgi' || isset($_SERVER['FCGI_ROLE'])
            || (function_exists('getenv') && getenv('FCGI_ROLE') !== false);
    }

    /** The database of a data directory's spool allowances, or false. */
    private static function db($dataDir)
    {
        if (array_key_exists($dataDir, self::$dbs)) {
            return self::$dbs[$dataDir];
        }
        self::$dbs[$dataDir] = false;
        if (!class_exists('PDO') || !in_array('sqlite', \PDO::getAvailableDrivers(), true)) {
            return false;
        }
        try {
            if (self::persistent()) {
                // In memory, on a persistent handle: it lives as long as the worker, as Python's dicts do.
                // The user name only keys the handle: one database per data directory.
                $db = new \PDO('sqlite::memory:', 'tidesight-budgets-' . md5($dataDir), null, array(
                    \PDO::ATTR_ERRMODE => \PDO::ERRMODE_EXCEPTION,
                    \PDO::ATTR_PERSISTENT => true,
                ));
            } else {
                // Plain CGI: nothing outlives the request, so the counts go to a small file every process
                // shares (best effort: disposable data, no syncs; a failure lets the record through).
                Util::mkdirPrivate($dataDir);
                $old = Util::umask(Util::privateMask());
                $db = new \PDO('sqlite:' . $dataDir . DIRECTORY_SEPARATOR . self::CGI_FILE, null, null, array(
                    \PDO::ATTR_ERRMODE => \PDO::ERRMODE_EXCEPTION,
                    \PDO::ATTR_TIMEOUT => 5,
                ));
                Util::umask($old);
                $db->exec('PRAGMA busy_timeout=5000');
                $db->exec('PRAGMA synchronous=OFF');
                self::$shared[$dataDir] = true;
            }
            if ((int) $db->query('PRAGMA user_version')->fetchColumn() !== 1) {
                $db->exec('CREATE TABLE IF NOT EXISTS rate (key TEXT NOT NULL, minute INTEGER NOT NULL, '
                    . 'n INTEGER NOT NULL, PRIMARY KEY (key, minute)) WITHOUT ROWID');
                $db->exec('CREATE TABLE IF NOT EXISTS quota (site TEXT NOT NULL, day TEXT NOT NULL, '
                    . 'n INTEGER NOT NULL, PRIMARY KEY (site, day)) WITHOUT ROWID');
                $db->exec('CREATE TABLE IF NOT EXISTS refusals (day TEXT NOT NULL, site TEXT NOT NULL, '
                    . 'name TEXT NOT NULL, n INTEGER NOT NULL, UNIQUE (day, site, name))');
                $db->exec('CREATE TABLE IF NOT EXISTS state (k TEXT PRIMARY KEY, v TEXT)');
                $db->exec('PRAGMA user_version = 1');
            }
            self::$dbs[$dataDir] = $db;
        } catch (\Throwable $e) {
            ErrorLog::warning($dataDir, 'spool allowances unavailable: ' . $e->getMessage());
        }
        return self::$dbs[$dataDir];
    }

    /**
     * Count a spooled record against its budgets and, with $quotaDay, its site's daily quota; the
     * counter name of the first one exceeded, else null. Counting that fails never refuses a record.
     * $stream ("events" for requests and counters, "chunks") keeps the allowances of each stream
     * apart, as those of events.db and replay.db are.
     */
    public static function over($dataDir, Limits $limits, $nowMs, $stream, $quotaDay)
    {
        $db = self::db($dataDir);
        if ($db === false) {
            return null;
        }
        try {
            return self::atomically($dataDir, $db, function () use ($db, $limits, $nowMs, $stream, $quotaDay) {
                return ProcessBudgets::charge($db, $limits, $nowMs, $stream, $quotaDay);
            });
        } catch (\Throwable $e) {
            ErrorLog::error($dataDir, 'spool allowances: ' . $e->getMessage());
        }
        return null;
    }

    /** this minute's use of the allowance keys in a stream (storage.SpoolStore.usage). */
    public static function usage($dataDir, $stream, array $keys, $nowMs)
    {
        $db = self::db($dataDir);
        $out = array();
        if ($db === false) {
            return $out;
        }
        try {
            foreach ($keys as $k) {
                $n = self::run($db, 'SELECT n FROM rate WHERE key = ? AND minute = ?',
                    array($stream . '|' . $k, intdiv((int) $nowMs, 60000)))->fetchColumn();
                if ($n !== false && $n !== null) {
                    $out[(string) $k] = Util::number($n);
                }
            }
        } catch (\Throwable $e) {
            ErrorLog::error($dataDir, 'spool allowances: ' . $e->getMessage());
        }
        return $out;
    }

    /** $fn() in an IMMEDIATE transaction when the counts are shared by processes (the CGI file). */
    private static function atomically($dataDir, \PDO $db, $fn)
    {
        if (empty(self::$shared[$dataDir])) {
            return $fn();
        }
        $db->exec('BEGIN IMMEDIATE');
        try {
            $out = $fn();
            $db->exec('COMMIT');
            return $out;
        } catch (\Throwable $e) {
            try {
                $db->exec('ROLLBACK');
            } catch (\Throwable $e2) {
                // no transaction left
            }
            throw $e;
        }
    }

    /** @internal the counting of over(), for its closure */
    public static function charge(\PDO $db, Limits $limits, $nowMs, $stream, $quotaDay)
    {
        $minute = intdiv((int) $nowMs, 60000);
        if (self::state($db, 'minute') !== (string) $minute) { // only the current minute is ever read
            self::run($db, 'DELETE FROM rate WHERE minute < ?', array($minute));
            self::setState($db, 'minute', (string) $minute);
        }
        foreach ($limits->budgets as $b) {
            if ($b->key === '' || $b->limit <= 0) {
                continue;
            }
            $key = $stream . '|' . $b->key; // a fixed stream name first: no budget key can reach another stream
            self::run($db, 'INSERT OR IGNORE INTO rate(key, minute, n) VALUES (?, ?, 0)', array($key, $minute));
            self::run($db, 'UPDATE rate SET n = n + ? WHERE key = ? AND minute = ?',
                array($b->amount, $key, $minute));
            $used = Util::number(self::run($db, 'SELECT n FROM rate WHERE key = ? AND minute = ?',
                array($key, $minute))->fetchColumn());
            if ($used > $b->limit) {
                return $b->counter;
            }
        }
        if ($quotaDay !== null && $limits->quota > 0 && $limits->hits > 0) {
            $n = Util::number(self::run($db, 'SELECT n FROM quota WHERE site = ? AND day = ?',
                array($limits->site, $quotaDay))->fetchColumn());
            if ($n + $limits->hits > $limits->quota) {
                return 'over_quota';
            }
            if (self::state($db, 'day') !== $quotaDay) { // only the current day is ever read
                self::run($db, 'DELETE FROM quota WHERE day <> ?', array($quotaDay));
                self::setState($db, 'day', $quotaDay);
            }
            self::run($db, 'INSERT OR IGNORE INTO quota(site, day, n) VALUES (?, ?, 0)',
                array($limits->site, $quotaDay));
            self::run($db, 'UPDATE quota SET n = n + ? WHERE site = ? AND day = ?',
                array($limits->hits, $limits->site, $quotaDay));
        }
        return null;
    }

    /**
     * Count a refusal (storage.SpoolStore._refused). Refusals are summed per day, site and counter
     * and handed back for writing at most once a minute per process (or once more than 1000 are
     * pending), so a refused flood does not grow the spool line by line. Returns the counts to write
     * now, each array(day, site, name, n).
     */
    public static function refused($dataDir, $day, $site, $name)
    {
        $db = self::db($dataDir);
        if ($db !== false) {
            try {
                return self::atomically($dataDir, $db, function () use ($db, $day, $site, $name) {
                    return ProcessBudgets::addRefusal($db, $day, $site, $name);
                });
            } catch (\Throwable $e) {
                ErrorLog::error($dataDir, 'spool allowances: ' . $e->getMessage());
            }
        }
        return array(array($day, $site, $name, 1));
    }

    /** @internal the counting of refused(), for its closure */
    public static function addRefusal(\PDO $db, $day, $site, $name)
    {
        self::run($db, 'INSERT OR IGNORE INTO refusals(day, site, name, n) VALUES (?, ?, ?, 0)', array($day, $site, $name));
        self::run($db, 'UPDATE refusals SET n = n + 1 WHERE day = ? AND site = ? AND name = ?', array($day, $site, $name));
        return self::pending($db);
    }

    /**
     * The refusal counts due for writing, when this request used the per-process state. Python also
     * writes them when its store is closed; a PHP worker gives no such moment, so a request that
     * spools a minute after the last write does it, and counts still pending when a worker exits are lost.
     */
    public static function due($dataDir)
    {
        if (empty(self::$dbs[$dataDir])) {
            return array();
        }
        $db = self::$dbs[$dataDir];
        try {
            return self::atomically($dataDir, $db, function () use ($db) {
                return ProcessBudgets::duePending($db);
            });
        } catch (\Throwable $e) {
            ErrorLog::error($dataDir, 'spool allowances: ' . $e->getMessage());
            return array();
        }
    }

    /** @internal pending(), for the closure of due() */
    public static function duePending(\PDO $db)
    {
        return self::pending($db);
    }

    /** storage.SpoolStore._flush_refusals, when due: every pending count, which is then forgotten. */
    private static function pending(\PDO $db)
    {
        $count = (int) self::run($db, 'SELECT COUNT(*) FROM refusals', array())->fetchColumn();
        if ($count === 0) {
            return array();
        }
        $now = microtime(true);
        $last = self::state($db, 'flushed');
        if ($count <= 1000 && $last !== null && $now - (float) $last < 60) {
            return array();
        }
        $rows = array();
        foreach (self::run($db, 'SELECT day, site, name, n FROM refusals ORDER BY rowid', array())
                     ->fetchAll(\PDO::FETCH_NUM) as $r) {
            $rows[] = array((string) $r[0], (string) $r[1], (string) $r[2], (int) $r[3]);
        }
        self::run($db, 'DELETE FROM refusals', array());
        self::setState($db, 'flushed', sprintf('%.6F', $now));
        return $rows;
    }

    private static function state(\PDO $db, $k)
    {
        $v = self::run($db, 'SELECT v FROM state WHERE k = ?', array($k))->fetchColumn();
        return $v === false || $v === null ? null : (string) $v;
    }

    private static function setState(\PDO $db, $k, $v)
    {
        self::run($db, 'INSERT OR REPLACE INTO state(k, v) VALUES (?, ?)', array($k, (string) $v));
    }

    private static function run(\PDO $db, $sql, array $params)
    {
        $stmt = $db->prepare($sql);
        foreach (array_values($params) as $i => $v) {
            $stmt->bindValue($i + 1, $v, is_int($v) ? \PDO::PARAM_INT : \PDO::PARAM_STR);
        }
        $stmt->execute();
        return $stmt;
    }
}

/**
 * Appends one JSON line per record to a per-process file (storage.SpoolStore).
 * Layout: <data_dir>/spool/events/<YYYY-MM-DD>/<host>-<pid>.jsonl and the same under chunks/.
 * Allowances and quotas are per process (ProcessBudgets); refusals are counted in counter lines
 * written summed, at most once a minute per process.
 */
final class SpoolStore
{
    public $name = 'spool';
    private $dataDir;
    private $root;
    private $host;

    public function __construct($dataDir)
    {
        $this->dataDir = (string) $dataDir;
        $this->root = $dataDir . DIRECTORY_SEPARATOR . 'spool';
    }

    /** "<host>-<pid>": the per-process file name (gethostname and getmypid may be disabled by the host). */
    private function processName()
    {
        if ($this->host === null) {
            $host = function_exists('gethostname') ? @gethostname() : false;
            if (!is_string($host) || $host === '') {
                $host = function_exists('php_uname') ? @php_uname('n') : '';
            }
            $parts = explode('.', is_string($host) ? $host : '');
            $this->host = preg_match('/\A[A-Za-z0-9_-]+\z/', $parts[0]) === 1 ? $parts[0] : 'host';
        }
        $pid = function_exists('getmypid') ? @getmypid() : false;
        return $this->host . '-' . (is_int($pid) && $pid > 0 ? $pid : 'php');
    }

    private function append($stream, $ms, $line)
    {
        $dir = $this->root . DIRECTORY_SEPARATOR . $stream . DIRECTORY_SEPARATOR . Util::day($ms);
        if (!Util::mkdirPrivate($dir)) {
            throw new StorageError('cannot create the spool directory');
        }
        $path = $dir . DIRECTORY_SEPARATOR . $this->processName() . '.jsonl';
        // One write of a whole line, under an exclusive lock (threaded SAPIs share a pid), created private.
        $old = Util::umask(Util::privateMask());
        $ok = @file_put_contents($path, $line . "\n", FILE_APPEND | LOCK_EX);
        Util::umask($old);
        if ($ok === false) {
            throw new StorageError('cannot append to the spool');
        }
    }

    public function writeRequest(RequestRecord $rec, Limits $limits)
    {
        $day = Util::day($rec->received_at);
        $refused = ProcessBudgets::over($this->dataDir, $limits, $rec->received_at, 'events', $day);
        if ($refused !== null) {
            $this->writeCounts(ProcessBudgets::refused($this->dataDir, $day, $limits->site, $refused));
            return Storage::RATE_LIMITED;
        }
        $this->append('events', $rec->received_at, '{"k":"request","rec":' . $rec->toJson() . '}');
        return Storage::STORED;
    }

    public function writeChunk(ChunkRecord $rec, Limits $limits)
    {
        $refused = ProcessBudgets::over($this->dataDir, $limits, $rec->received_at, 'chunks', null);
        if ($refused !== null) {
            $this->writeCounts(ProcessBudgets::refused($this->dataDir, Util::day($rec->received_at), $limits->site,
                $refused));
            return Storage::RATE_LIMITED;
        }
        $this->append('chunks', $rec->received_at, '{"k":"chunk","rec":' . $rec->toJson() . '}');
        return Storage::STORED;
    }

    public function dataDir()
    {
        return $this->dataDir;
    }

    /** storage.SpoolStore.usage: this minute's use of the allowance keys, in the per-process counts of the stream. */
    public function usage($stream, array $keys, $nowMs)
    {
        return ProcessBudgets::usage($this->dataDir, $stream, $keys, $nowMs);
    }

    /** storage.SpoolStore.refuse: charge a request refused before parsing; refusals are summed as usual. */
    public function refuse($stream, Limits $limits, $nowMs)
    {
        $refused = ProcessBudgets::over($this->dataDir, $limits, $nowMs, $stream, null);
        if ($refused !== null) {
            $this->writeCounts(ProcessBudgets::refused($this->dataDir, Util::day($nowMs), $limits->site, $refused));
        }
        return $refused;
    }

    /** Count something; with $limits (a Limits), only while its budgets hold (those of the events stream). */
    public function recordCounter($day, $siteId, $name, $n = 1, $limits = null)
    {
        if ($limits instanceof Limits
            && ProcessBudgets::over($this->dataDir, $limits, Util::nowMs(), 'events', null) !== null) {
            return;
        }
        $this->append('events', Util::nowMs(), self::counterLine($day, $siteId, $name, $n));
    }

    /** @param array $rows array(day, site, name, n) each, from ProcessBudgets */
    private function writeCounts(array $rows)
    {
        foreach ($rows as $r) {
            $this->append('events', Util::nowMs(), self::counterLine($r[0], $r[1], $r[2], $r[3]));
        }
    }

    private static function counterLine($day, $siteId, $name, $n)
    {
        return '{"k":"counter","day":' . HitRecord::s($day) . ',"site_id":' . HitRecord::s($siteId)
            . ',"name":' . HitRecord::s($name) . ',"n":' . (int) $n . '}';
    }

    public function healthy()
    {
        Util::mkdirPrivate($this->root);
        return is_dir($this->root) && is_writable($this->root);
    }

    /** End of the request: write the refusal counts that came due (see ProcessBudgets::due). Never throws. */
    public function close()
    {
        try {
            $this->writeCounts(ProcessBudgets::due($this->dataDir));
        } catch (\Throwable $e) {
            ErrorLog::warning($this->dataDir, 'could not write refusal counts: ' . $e->getMessage());
        }
    }
}

/** SQLite first; on a database error, append to the spool instead (never lose a hit). */
final class FallbackStore
{
    /** After SQLite failed, the spool alone for this long: no request waits for a busy database again. */
    const BREAK_SECONDS = 30;
    /** The marker of a failure, in the data directory (its mtime): shared by every process. */
    const BREAK_FILE = '.sqlite-down';

    public $name;
    private $primary;
    private $spool;

    public function __construct(SqliteStore $primary, SpoolStore $spool)
    {
        $this->primary = $primary;
        $this->spool = $spool;
        $this->name = $primary->name;
    }

    private function breakFile()
    {
        return $this->spool->dataDir() . DIRECTORY_SEPARATOR . self::BREAK_FILE;
    }

    /**
     * storage.FallbackStore._try: array(true, result) from SQLite, or array(false, null, error) when it
     * failed or failed less than BREAK_SECONDS ago (the caller then uses the spool). Python keeps that
     * moment per process; PHP keeps nothing between requests, so it is the mtime of a marker file.
     */
    private function attempt($method, array $args)
    {
        $marker = $this->breakFile();
        $failed = @filemtime($marker);
        if ($failed !== false && time() - $failed < self::BREAK_SECONDS && time() >= $failed) {
            return array(false, null, null);
        }
        try {
            return array(true, call_user_func_array(array($this->primary, $method), $args), null);
        } catch (\Throwable $e) {
            $old = Util::umask(Util::privateMask());
            if (function_exists('touch')) {
                @touch($marker);
            } else {
                @file_put_contents($marker, '');
            }
            Util::umask($old);
            ErrorLog::error($this->spool->dataDir(),
                sprintf('SQLite failed (%s); using the spool for %d s', $e->getMessage(), self::BREAK_SECONDS));
            return array(false, null, $e);
        }
    }

    private function write($method, array $args)
    {
        list($ok, $outcome, $e) = $this->attempt($method, $args);
        if ($ok) {
            return $outcome;
        }
        try {
            $outcome = call_user_func_array(array($this->spool, $method), $args);
        } catch (\Throwable $e2) {
            throw new StorageError(sprintf('could not store the record: %s / %s',
                $e !== null ? $e->getMessage() : 'SQLite unavailable', $e2->getMessage()));
        }
        return $outcome === Storage::STORED ? Storage::SPOOLED : $outcome; // RATE_LIMITED: only a counter was written
    }

    public function writeRequest(RequestRecord $rec, Limits $limits)
    {
        return $this->write('writeRequest', array($rec, $limits));
    }

    public function writeChunk(ChunkRecord $rec, Limits $limits)
    {
        return $this->write('writeChunk', array($rec, $limits));
    }

    public function recordCounter($day, $siteId, $name, $n = 1, $limits = null)
    {
        list($ok) = $this->attempt('recordCounter', array($day, $siteId, $name, $n, $limits));
        if (!$ok) {
            $this->spool->recordCounter($day, $siteId, $name, $n, $limits);
        }
    }

    public function usage($stream, array $keys, $nowMs)
    {
        list($ok, $used) = $this->attempt('usage', array($stream, $keys, $nowMs));
        return $ok ? $used : $this->spool->usage($stream, $keys, $nowMs);
    }

    public function refuse($stream, Limits $limits, $nowMs)
    {
        list($ok, $refused) = $this->attempt('refuse', array($stream, $limits, $nowMs));
        return $ok ? $refused : $this->spool->refuse($stream, $limits, $nowMs);
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
