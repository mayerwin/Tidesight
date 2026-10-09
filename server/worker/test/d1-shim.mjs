/**
 * A Cloudflare D1 stand-in on node:sqlite, for running the Worker under Node (test/serve.mjs).
 *
 * Implements the subset of the D1 client API the Worker uses — prepare(sql).bind(...values) with first(),
 * all(), run(), raw(), and db.batch([...]) as one transaction — with D1's documented constraints, so code that
 * passes here does not rely on anything D1 refuses:
 *   - at most 100 bound parameters per statement, 100,000 bytes of SQL, 2,000,000 bytes per string or BLOB
 *   - bindable types: null, number, string, boolean (stored as 1/0), ArrayBuffer and views; not BigInt/undefined
 *   - no BEGIN/COMMIT/SAVEPOINT (use batch()), and only the PRAGMAs D1 documents
 *   - BLOB columns come back as arrays of numbers
 * https://developers.cloudflare.com/d1/worker-api/ and https://developers.cloudflare.com/d1/platform/limits/
 */

import { DatabaseSync } from "node:sqlite";

const MAX_PARAMS = 100;
const MAX_SQL_BYTES = 100000;
const MAX_VALUE_BYTES = 2000000;
const PRAGMAS = new Set(["table_list", "table_info", "table_xinfo", "index_list", "index_info", "index_xinfo",
  "quick_check", "foreign_key_check", "foreign_key_list", "case_sensitive_like", "ignore_check_constraints",
  "legacy_alter_table", "recursive_triggers", "reverse_unordered_selects", "foreign_keys", "defer_foreign_keys",
  "optimize"]);

export class D1Error extends Error {
  constructor(message, cause) {
    super(message, cause ? { cause } : undefined);
    this.name = "Error";
  }
}

function checkSql(sql) {
  if (typeof sql !== "string") throw new D1Error("D1_ERROR: query must be a string");
  if (Buffer.byteLength(sql) > MAX_SQL_BYTES) throw new D1Error("D1_ERROR: statement too long: SQLITE_TOOBIG");
  const body = sql.replace(/^(\s|--[^\n]*\n|\/\*[\s\S]*?\*\/)+/, "");
  if (/^(BEGIN|COMMIT|END|ROLLBACK|SAVEPOINT|RELEASE)\b/i.test(body)) {
    throw new D1Error("D1_ERROR: transactions are not supported; use batch(): SQLITE_AUTH");
  }
  const pragma = /^PRAGMA\s+(?:\w+\.)?(\w+)/i.exec(body);
  if (pragma && !PRAGMAS.has(pragma[1].toLowerCase())) throw new D1Error("D1_ERROR: not authorized: SQLITE_AUTH");
}

function toSqlite(value) {
  if (value === null) return null;
  switch (typeof value) {
    case "number":
      return value;
    case "boolean":
      return value ? 1 : 0;
    case "string":
      if (value.length * 3 > MAX_VALUE_BYTES && Buffer.byteLength(value) > MAX_VALUE_BYTES) {
        throw new D1Error("D1_ERROR: string or blob too big: SQLITE_TOOBIG");
      }
      return value;
    default:
      break;
  }
  if (value instanceof ArrayBuffer || ArrayBuffer.isView(value)) {
    const bytes = value instanceof ArrayBuffer ? new Uint8Array(value)
      : new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
    if (bytes.byteLength > MAX_VALUE_BYTES) throw new D1Error("D1_ERROR: string or blob too big: SQLITE_TOOBIG");
    return Uint8Array.from(bytes);
  }
  throw new D1Error(`D1_TYPE_ERROR: Type '${typeof value}' not supported for value '${String(value)}'`);
}

function fromSqlite(row) {
  const out = {};
  for (const [key, value] of Object.entries(row)) out[key] = value instanceof Uint8Array ? Array.from(value) : value;
  return out;
}

export class D1PreparedStatement {
  constructor(database, sql, params = []) {
    this.database = database;
    this.sql = sql;
    this.params = params;
  }

  bind(...values) {
    if (values.length > MAX_PARAMS) throw new D1Error(`D1_ERROR: too many SQL variables (${values.length} > ${MAX_PARAMS})`);
    return new D1PreparedStatement(this.database, this.sql, values.map(toSqlite));
  }

  /** Run inside the caller's transaction (batch) or on its own (autocommit). */
  execute() {
    const started = performance.now();
    const stmt = this.database.statement(this.sql);
    let results = [];
    let changes = 0;
    let lastRowId = null;
    const totalChanges = () => Number(this.database.statement("SELECT total_changes() AS n").get().n);
    const before = totalChanges();
    const returnsRows = typeof stmt.columns === "function" ? stmt.columns().length > 0 // Node >= 22.16
      : /^\s*(SELECT|WITH|PRAGMA|VALUES)\b|\bRETURNING\b/i.test(this.sql);
    if (returnsRows) {
      results = stmt.all(...this.params).map(fromSqlite);
    } else {
      const info = stmt.run(...this.params);
      lastRowId = Number(info.lastInsertRowid);
    }
    changes = totalChanges() - before;
    if (lastRowId === null) lastRowId = Number(this.database.statement("SELECT last_insert_rowid() AS id").get().id);
    return {
      success: true,
      results,
      meta: { duration: performance.now() - started, changes, last_row_id: lastRowId, changed_db: changes > 0,
        rows_read: results.length, rows_written: changes, size_after: 0, served_by: "tidesight-d1-shim" },
    };
  }

  autocommit() {
    try {
      return this.execute();
    } catch (exc) {
      throw exc instanceof D1Error ? exc : new D1Error(`D1_ERROR: ${exc.message}`, exc);
    }
  }

  async first(column) {
    const row = this.autocommit().results[0];
    if (!row) return null;
    if (column === undefined) return row;
    if (!(column in row)) throw new D1Error(`D1_COLUMN_NOTFOUND: Column not found (${column})`);
    return row[column];
  }

  async all() {
    return this.autocommit();
  }

  async run() {
    return this.autocommit();
  }

  async raw({ columnNames = false } = {}) {
    const { results } = this.autocommit();
    const rows = results.map((r) => Object.values(r));
    return columnNames ? [results.length ? Object.keys(results[0]) : [], ...rows] : rows;
  }
}

export class D1Database {
  /** @param {string} file  SQLite file standing in for the D1 database */
  constructor(file, { busyTimeoutMs = 10000 } = {}) {
    this.db = new DatabaseSync(file);
    this.db.exec(`PRAGMA busy_timeout=${busyTimeoutMs}`);
    this.db.exec("PRAGMA journal_mode=WAL");
    this.statements = new Map();
  }

  statement(sql) {
    let stmt = this.statements.get(sql);
    if (!stmt) {
      stmt = this.db.prepare(sql);
      this.statements.set(sql, stmt);
    }
    return stmt;
  }

  prepare(sql) {
    checkSql(sql);
    return new D1PreparedStatement(this, sql);
  }

  /** Statements run sequentially in one transaction; any failure rolls the whole batch back. */
  async batch(statements) {
    if (!Array.isArray(statements) || !statements.every((s) => s instanceof D1PreparedStatement)) {
      throw new D1Error("D1_ERROR: batch() expects an array of prepared statements");
    }
    this.db.exec("BEGIN IMMEDIATE");
    try {
      const results = statements.map((s) => s.execute());
      this.db.exec("COMMIT");
      return results;
    } catch (exc) {
      try {
        this.db.exec("ROLLBACK");
      } catch {
        // keep the original error
      }
      throw exc instanceof D1Error ? exc : new D1Error(`D1_ERROR: ${exc.message}`, exc);
    }
  }

  async exec(sql) {
    const started = performance.now();
    const queries = sql.split("\n").filter((line) => line.trim());
    for (const query of queries) checkSql(query);
    try {
      this.db.exec(sql);
    } catch (exc) {
      throw new D1Error(`D1_EXEC_ERROR: ${exc.message}`, exc);
    }
    return { count: queries.length, duration: performance.now() - started };
  }

  close() {
    this.db.close();
  }
}
