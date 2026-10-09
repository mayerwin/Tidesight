/**
 * Node http ↔ Fetch API glue, shared by the Node connector (server.mjs) and the Worker test server
 * (server/worker/test/serve.mjs).
 */

const FETCH_FORBIDDEN_METHODS = new Set(["CONNECT", "TRACE", "TRACK"]);

/** The socket peer as REMOTE_ADDR: IPv4-mapped IPv6 (a dual-stack listener) is reported as plain IPv4. */
export function remoteAddress(req) {
  const addr = req.socket && req.socket.remoteAddress;
  if (!addr) return null;
  return addr.startsWith("::ffff:") && addr.includes(".") ? addr.slice(7) : addr;
}

/**
 * The request body as a pull-based web stream: nothing is read until the collector asks for it, so a request
 * refused on its Content-Length is never buffered. A client that disconnects mid-body ends the stream early;
 * the collector then sees a short body, as a WSGI server would hand it over.
 */
export function bodyStream(req) {
  return new ReadableStream({
    pull(controller) {
      return new Promise((resolve) => {
        const finish = () => {
          cleanup();
          controller.close();
          resolve();
        };
        const onReadable = () => {
          const chunk = req.read();
          if (chunk !== null) {
            cleanup();
            controller.enqueue(new Uint8Array(chunk.buffer, chunk.byteOffset, chunk.byteLength));
            resolve();
          }
        };
        const cleanup = () => {
          req.off("readable", onReadable);
          req.off("end", finish);
          req.off("close", finish);
          req.off("error", finish);
        };
        const chunk = req.read();
        if (chunk !== null) {
          controller.enqueue(new Uint8Array(chunk.buffer, chunk.byteOffset, chunk.byteLength));
          resolve();
          return;
        }
        if (req.readableEnded || req.destroyed) {
          controller.close();
          resolve();
          return;
        }
        req.on("readable", onReadable);
        req.once("end", finish);
        req.once("close", finish);
        req.once("error", finish);
      });
    },
  }, { highWaterMark: 0 });
}

/**
 * The request's path and query, for request.url. The URL is never built from what the client sends as Host or as
 * an absolute-form target: a client-supplied authority with credentials ("u:p@x") would make the Request
 * constructor throw, and the collector reads the raw target anyway.
 */
function pathAndQuery(target) {
  if (target.startsWith("/")) return target;
  try {
    const u = new URL(target);
    return u.pathname + u.search;
  } catch {
    return "/";
  }
}

/**
 * Build a Fetch Request from a Node IncomingMessage.
 * @param {object} [options]
 * @param {Array} [options.extraHeaders]  [name, value] pairs that replace any header of the same name
 * @param {string} [options.scheme]       scheme for request.url (default http)
 */
export function toFetchRequest(req, { extraHeaders = [], scheme = "http" } = {}) {
  const replaced = new Set(extraHeaders.map(([name]) => name.toLowerCase()));
  const headers = new Headers();
  const raw = req.rawHeaders;
  for (let i = 0; i + 1 < raw.length; i += 2) {
    if (replaced.has(raw[i].toLowerCase())) continue;
    try {
      headers.append(raw[i], raw[i + 1]);
    } catch {
      // a value the Fetch API refuses; the Node connector reads raw headers anyway
    }
  }
  for (const [name, value] of extraHeaders) headers.set(name, value);
  let url;
  try {
    url = new URL(pathAndQuery(req.url), `${scheme}://localhost`);
  } catch {
    url = new URL(`${scheme}://localhost/`);
  }
  const method = req.method;
  const hasBody = method !== "GET" && method !== "HEAD";
  return new Request(url, {
    method: FETCH_FORBIDDEN_METHODS.has(method) ? (hasBody ? "POST" : "GET") : method,
    headers,
    body: hasBody ? bodyStream(req) : null,
    duplex: "half",
  });
}

function drain(req, limit) {
  let seen = 0;
  req.on("error", () => {});
  req.on("data", (chunk) => {
    seen += chunk.length;
    if (seen > limit) req.socket.destroy();
  });
  req.resume();
}

// The bytes of a Response's body attached by the collector (core/collector.mjs RAW_BODY): sent as they are, so a
// cached tracker file is not copied for every response.
const RAW_BODY = Symbol.for("tidesight.rawBody");

/**
 * Write a Fetch Response to a Node ServerResponse. A request body the handler did not read is drained (up to
 * drainLimit bytes, then the connection is dropped) so the client still receives the response, unless the
 * response closes the connection (Connection: close), which then ends without reading it.
 */
export async function sendResponse(req, res, response, { drainLimit = 16 * 1024 * 1024 } = {}) {
  const headers = [];
  response.headers.forEach((value, name) => {
    if (name !== "set-cookie") headers.push(name, value);
  });
  for (const cookie of response.headers.getSetCookie()) headers.push("set-cookie", cookie);
  let body = null;
  if (response.body && req.method !== "HEAD") {
    body = response[RAW_BODY] instanceof Uint8Array ? response[RAW_BODY] : new Uint8Array(await response.arrayBuffer());
  }
  if (res.destroyed) return;
  res.writeHead(response.status, headers);
  res.end(body);
  if (req.complete) return;
  if (String(response.headers.get("connection")).toLowerCase() === "close") {
    res.once("finish", () => req.socket && req.socket.destroy());
  } else {
    drain(req, drainLimit);
  }
}

/** Last-resort error response. */
export function sendError(res, status = 500, text = "error\n") {
  if (res.headersSent) {
    res.destroy();
    return;
  }
  res.writeHead(status, { "content-type": "text/plain", "content-length": Buffer.byteLength(text) });
  res.end(text);
}
