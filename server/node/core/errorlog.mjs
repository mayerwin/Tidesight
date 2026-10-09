/**
 * collect.ErrorLog (runtime-neutral): full stack traces for the first few errors of each minute, then only a count,
 * so that a flood of failing requests does not become a flood of log lines on a small host's disk. The count is
 * logged as "N more errors in the previous minute (not logged)" when the first error of a later minute arrives.
 */
export class ErrorLog {
  /**
   * @param {{error: Function, warning?: Function}} log  a logger: error(msg, exc) writes msg and exc's stack
   * @param {number} [perMinute]  errors logged in full per clock minute
   * @param {() => number} [clock]  wall clock in ms (Python: time.time())
   */
  constructor(log, perMinute = 5, clock = Date.now) {
    this.log = log;
    this.perMinute = perMinute;
    this.clock = clock;
    this.minute = 0;
    this.count = 0;
  }

  /** Log msg (and exc's stack) at `level` while this minute's allowance lasts; count it otherwise. */
  exception(msg, exc = undefined, level = "error") {
    const minute = Math.floor(this.clock() / 60000);
    if (minute !== this.minute) {
      if (this.count > this.perMinute) {
        this.log.error(`${this.count - this.perMinute} more errors in the previous minute (not logged)`);
      }
      this.minute = minute;
      this.count = 0;
    }
    this.count += 1;
    if (this.count > this.perMinute) return;
    if (level === "warning" && this.log.warning) this.log.warning(msg);
    else this.log.error(msg, exc);
  }
}
