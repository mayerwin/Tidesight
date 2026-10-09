/** Minimal logging in the Python collector's format: "2026-10-08 12:34:56,789 tidesight.collect ERROR message". */

import fs from "node:fs";

function asctime(d = new Date()) {
  const p = (n, w = 2) => String(n).padStart(w, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
    `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())},${p(d.getMilliseconds(), 3)}`;
}

/** A logger writing to `file` (appended) or to stderr when no file is given or it cannot be opened. */
export function createLogger(file = "") {
  let fd = null;
  if (file) {
    try {
      fd = fs.openSync(file, "a");
    } catch (exc) {
      process.stderr.write(`${asctime()} tidesight WARNING cannot open log file ${file}: ${exc.message}\n`);
    }
  }
  const write = (name, level, msg, exc) => {
    let line = `${asctime()} ${name} ${level} ${msg}\n`;
    if (exc) line += `${(exc && exc.stack) || exc}\n`;
    if (fd !== null) {
      try {
        fs.writeSync(fd, line);
        return;
      } catch {
        // fall back to stderr
      }
    }
    process.stderr.write(line);
  };
  const child = (name) => ({
    info: (msg) => write(name, "INFO", msg),
    warning: (msg) => write(name, "WARNING", msg),
    error: (msg, exc) => write(name, "ERROR", msg, exc),
    child,
  });
  return child("tidesight");
}
