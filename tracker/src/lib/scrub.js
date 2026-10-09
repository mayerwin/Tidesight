// Text that may carry secrets (error messages and stacks, link and button text), bundled into the plugins that
// send such text. URLs and name=value pairs in it get the redaction of every URL the tracker sends (api.scrub).

// Every http(s) or ws(s) URL in s redacted (a stack frame's trailing :line:col is kept), then every name=value
// whose name is a secret parameter (redact_params), e.g. "failed: access_token=…".
export function scrubText(api, s) {
  if (typeof s !== 'string' || !api.scrub) return s;
  // One pass: a URL is taken whole (its own parameters are its business), anything else as name=value pairs.
  return (s.length > 20000 ? s.slice(0, 20000) : s).replace(/\b(?:https?|wss?):\/\/[^\s"'<>()]+|([\w.%[\]-]{1,256})=([^\s&;,'"()<>]+)/gi, (m, k, v) => {
    if (k === undefined) {
      const lc = /(?::\d+){1,2}$/.exec(m), end = lc ? m.length - lc[0].length : m.length;
      return api.scrub(m.slice(0, end)) + m.slice(end);
    }
    return api.scrub('?' + m) !== '?' + m ? k + '=redacted' : m;
  });
}

// What was thrown or rejected: an Error's name and message; for anything else its type (and a string name or
// message it may carry), never the value itself: a rejected object may hold tokens or personal data.
export function errorReason(r) {
  if (r && typeof r === 'object') {
    const m = r.message, n = r.name;
    return typeof m === 'string' ? (typeof n === 'string' && n !== 'Error' ? n + ': ' : '') + m
      : Object.prototype.toString.call(r).slice(8, -1) + (typeof n === 'string' ? ' ' + n : '');
  }
  return typeof r === 'symbol' ? 'Symbol' : String(r);
}
