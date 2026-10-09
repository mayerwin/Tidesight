"""FastCGI: the web app in a long-lived process on shared hosts (standard library only).

Dreamhost removed Passenger from its shared plans in 2024, but its Apache still runs FastCGI programs through
mod_fcgid, as many shared hosts do: a script named *.fcgi is started once and then answers request after
request, like a Passenger app, so a hit costs milliseconds instead of a new Python process. mod_fcgid hands the
program a listening socket as its standard input and sends each request over a new connection as FastCGI
records: the CGI variables (PARAMS), then the body (STDIN); the program answers with STDOUT records and an
END_REQUEST record.

run() serves a WSGI application that way (server/python/tidesight.fcgi is the entry point). Started any other way
(by a server that runs the file as a plain CGI script) it answers a single CGI request instead, so the same file
works everywhere. A process makes way for a fresh one by itself, after the request in progress, when its code
changes on disk (an update needs no restart), after MAX_REQUESTS requests, or when the web server asks it to stop.
"""

from __future__ import annotations

import hashlib
import logging
import os
import signal
import socket
import struct
import sys
import tempfile
import threading
import time
from typing import Any, BinaryIO, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

log = logging.getLogger("tidesight.fcgi")

# Record types and values of the FastCGI 1.0 specification.
BEGIN_REQUEST, ABORT_REQUEST, END_REQUEST, PARAMS, STDIN, STDOUT, STDERR, DATA = 1, 2, 3, 4, 5, 6, 7, 8
GET_VALUES, GET_VALUES_RESULT, UNKNOWN_TYPE = 9, 10, 11
RESPONDER = 1
KEEP_CONN = 1
REQUEST_COMPLETE, CANT_MPX_CONN, OVERLOADED, UNKNOWN_ROLE = 0, 1, 2, 3
HEADER = struct.Struct("!BBHHBx")  # version, type, request id, content length, padding length, reserved
MAX_CONTENT = 65535                # content bytes in one record

MAX_PARAMS = 256 * 1024             # CGI variables of one request (Apache sends a few KB)
BODY_IN_MEMORY = 1024 * 1024        # larger request bodies wait in a temporary file
MAX_BODY = 16 * 1024 * 1024         # body bytes kept (Tidesight refuses far smaller bodies; the rest is skipped)
MAX_REQUESTS = 10000                # then a fresh process takes over (a guard against slow leaks)
CHECK_SECONDS = 2.0                 # at most this often (between requests), look for changed code
IO_TIMEOUT = 120.0                  # a connection that stalls this long is dropped
VALUES = {b"FCGI_MAX_CONNS": b"1", b"FCGI_MAX_REQS": b"1", b"FCGI_MPXS_CONNS": b"0"}

WsgiApp = Callable[[Dict[str, Any], Callable[..., Any]], Iterable[bytes]]


class ProtocolError(Exception):
    """The web server sent something this program cannot follow: the connection is dropped."""


class ConnectionGone(Exception):
    """The web server's end of the connection went away, or stalled, while the answer was being sent."""


# -- records -------------------------------------------------------------------

def read_record(rfile: BinaryIO) -> Optional[Tuple[int, int, bytes]]:
    """(type, request id, content), or None when the web server closed the connection between records."""
    head = rfile.read(HEADER.size)
    if not head:
        return None
    if len(head) < HEADER.size:
        raise ProtocolError("connection closed inside a record header")
    version, rtype, rid, length, padding = HEADER.unpack(head)
    if version != 1:
        raise ProtocolError("FastCGI version %d" % version)
    data = rfile.read(length + padding) if length + padding else b""
    if len(data) < length + padding:
        raise ProtocolError("connection closed inside a record")
    return rtype, rid, data[:length]


def record(rtype: int, rid: int, content: bytes = b"") -> bytes:
    """One record (content of at most MAX_CONTENT bytes), padded to a multiple of 8 bytes."""
    padding = -len(content) % 8
    return HEADER.pack(1, rtype, rid, len(content), padding) + content + b"\0" * padding


def end_request(rid: int, protocol_status: int = REQUEST_COMPLETE, app_status: int = 0) -> bytes:
    return record(END_REQUEST, rid, struct.pack("!IB3x", app_status, protocol_status))


def decode_pairs(data: bytes) -> List[Tuple[bytes, bytes]]:
    """FastCGI name-value pairs: each length is one byte below 128, else four bytes with the top bit set."""
    pairs: List[Tuple[bytes, bytes]] = []
    i, end = 0, len(data)
    while i < end:
        lengths = []
        for _ in range(2):
            if i < end and data[i] < 128:
                lengths.append(data[i])
                i += 1
            elif i + 4 <= end:
                lengths.append(struct.unpack_from("!I", data, i)[0] & 0x7FFFFFFF)
                i += 4
            else:
                raise ProtocolError("truncated name-value pair")
        if i + lengths[0] + lengths[1] > end:
            raise ProtocolError("truncated name-value pair")
        pairs.append((data[i:i + lengths[0]], data[i + lengths[0]:i + lengths[0] + lengths[1]]))
        i += lengths[0] + lengths[1]
    return pairs


def encode_pairs(pairs: Iterable[Tuple[bytes, bytes]]) -> bytes:
    out = bytearray()
    for name, value in pairs:
        for length in (len(name), len(value)):
            out += bytes((length,)) if length < 128 else struct.pack("!I", length | 0x80000000)
        out += name + value
    return bytes(out)


def management_reply(rtype: int, content: bytes) -> bytes:
    """The answer to a record about the application rather than a request (request id 0)."""
    if rtype == GET_VALUES:
        asked = [name for name, _ in decode_pairs(content)]
        return record(GET_VALUES_RESULT, 0, encode_pairs((name, VALUES[name]) for name in asked if name in VALUES))
    return record(UNKNOWN_TYPE, 0, struct.pack("!B7x", rtype))


# -- requests ------------------------------------------------------------------

class Request:
    """One request as the web server sent it: its CGI variables and its whole body."""

    def __init__(self, rid: int, keep_conn: bool) -> None:
        self.id = rid
        self.keep_conn = keep_conn
        self.params = bytearray()
        self.body = tempfile.SpooledTemporaryFile(max_size=BODY_IN_MEMORY)
        self.body_size = 0

    def add_params(self, data: bytes) -> None:
        self.params += data
        if len(self.params) > MAX_PARAMS:
            raise ProtocolError("CGI variables over %d bytes" % MAX_PARAMS)

    def add_body(self, data: bytes) -> None:
        room = MAX_BODY - self.body_size
        if room > 0:
            self.body.write(data[:room])
        self.body_size += len(data)

    def environ(self) -> Dict[str, Any]:
        """The WSGI environ. Values are str holding the bytes as Latin-1, as PEP 3333 (and CGI under wsgiref) has it."""
        env: Dict[str, Any] = {name.decode("latin-1"): value.decode("latin-1")
                               for name, value in decode_pairs(bytes(self.params))}
        if self.body_size and not env.get("CONTENT_LENGTH"):  # a chunked upload: the length is known now
            env["CONTENT_LENGTH"] = str(min(self.body_size, MAX_BODY))
        https = (env.get("HTTPS", "").lower() in ("on", "1", "yes")
                 or env.get("REQUEST_SCHEME", "").lower() == "https")
        self.body.seek(0)
        env.update({"wsgi.version": (1, 0), "wsgi.url_scheme": "https" if https else "http",
                    "wsgi.input": self.body, "wsgi.errors": sys.stderr, "wsgi.multithread": False,
                    "wsgi.multiprocess": True, "wsgi.run_once": False, "wsgi.input_terminated": True})
        return env

    def close(self) -> None:
        self.body.close()


def read_request(rfile: BinaryIO, conn: socket.socket) -> Optional[Request]:
    """The next request on a connection, body included; None when the web server closed the connection."""
    req: Optional[Request] = None
    try:
        while True:
            rec = read_record(rfile)
            if rec is None:
                if req is not None:
                    raise ProtocolError("connection closed inside a request")
                return None
            rtype, rid, content = rec
            if rid == 0:
                conn.sendall(management_reply(rtype, content))
            elif rtype == BEGIN_REQUEST:
                if len(content) < 8:
                    raise ProtocolError("short BEGIN_REQUEST record")
                role, flags = struct.unpack_from("!HB", content)
                if req is not None:
                    conn.sendall(end_request(rid, CANT_MPX_CONN))  # one request at a time on a connection
                elif role != RESPONDER:
                    conn.sendall(end_request(rid, UNKNOWN_ROLE))
                else:
                    req = Request(rid, bool(flags & KEEP_CONN))
            elif req is None or rid != req.id:
                continue  # a request refused above
            elif rtype == PARAMS:
                req.add_params(content)
            elif rtype == STDIN:
                if not content:  # the end of the body: the request is complete
                    done, req = req, None
                    return done
                req.add_body(content)
            elif rtype == ABORT_REQUEST:  # the client left before the request was complete
                conn.sendall(end_request(rid))
                req.close()
                req = None
    finally:
        if req is not None:
            req.close()


# -- answers -------------------------------------------------------------------

class Output:
    """The answer to one request, as STDOUT records (sent as it grows, so long answers stream)."""

    def __init__(self, conn: socket.socket, rid: int) -> None:
        self.conn = conn
        self.rid = rid
        self.buf = bytearray()

    def write(self, data: bytes) -> None:
        self.buf += data
        if len(self.buf) >= MAX_CONTENT:
            self.flush()

    def flush(self, tail: bytes = b"") -> None:
        data = bytes(self.buf)
        self.buf.clear()
        out = b"".join(record(STDOUT, self.rid, data[i:i + MAX_CONTENT]) for i in range(0, len(data), MAX_CONTENT))
        try:
            self.conn.sendall(out + tail)
        except OSError as exc:  # includes timeouts
            raise ConnectionGone(str(exc) or exc.__class__.__name__)

    def end(self) -> None:
        self.flush(record(STDOUT, self.rid) + end_request(self.rid))


def response_head(status: str, headers: Sequence[Tuple[str, str]]) -> bytes:
    """The CGI header block of an answer. A line break inside a value would forge headers: refused."""
    lines = ["Status: " + status] + ["%s: %s" % (name, value) for name, value in headers]
    if any("\r" in line or "\n" in line for line in lines):
        raise ValueError("line break in a response header")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


class Response:
    """WSGI's start_response() and write() for one request."""

    def __init__(self, out: Output) -> None:
        self.out = out
        self.status: Optional[str] = None
        self.headers: List[Tuple[str, str]] = []
        self.sent = False

    def start_response(self, status: str, headers: List[Tuple[str, str]], exc_info: Any = None) -> Callable[[bytes], None]:
        if exc_info:
            try:
                if self.sent:
                    raise exc_info[1].with_traceback(exc_info[2])
            finally:
                exc_info = None
        elif self.status is not None:
            raise AssertionError("start_response() called twice")
        self.status, self.headers = status, list(headers)
        return self.write

    def write(self, data: bytes) -> None:
        if self.status is None:
            raise AssertionError("write() before start_response()")
        if not self.sent:
            head = response_head(self.status, self.headers)
            self.sent = True
            self.out.write(head)
        if data:
            self.out.write(data)


def handle(app: WsgiApp, req: Request, conn: socket.socket) -> None:
    """Run the application for one request and send its answer (500 when it fails before answering)."""
    env = req.environ()
    out = Output(conn, req.id)
    resp = Response(out)
    try:
        result = app(env, resp.start_response)
        try:
            for data in result:
                if data:
                    resp.write(data)
            if not resp.sent:
                resp.write(b"")
        finally:
            close = getattr(result, "close", None)
            if close is not None:
                close()
    except ConnectionGone:
        raise
    except Exception:
        log.exception("%s %s failed", env.get("REQUEST_METHOD", "?"), env.get("REQUEST_URI") or env.get("PATH_INFO", ""))
        if not resp.sent:  # nothing sent yet: a plain 500 (the details are in the log, not in the answer)
            out.buf.clear()
            out.write(response_head("500 Internal Server Error", [("Content-Type", "text/plain; charset=utf-8"),
                                                                 ("Cache-Control", "no-store")]))
            out.write(b"Internal server error\n")
        # else: the answer is cut short; END_REQUEST still closes it
    out.end()


def serve_connection(conn: socket.socket, app: WsgiApp) -> int:
    """Answer the requests the web server sends on one connection (mod_fcgid sends one). Returns how many."""
    served = 0
    rfile = conn.makefile("rb")
    try:
        while True:
            req = read_request(rfile, conn)
            if req is None:
                break
            try:
                handle(app, req, conn)
            finally:
                req.close()
            served += 1
            if not req.keep_conn:
                break
    except (ProtocolError, ConnectionGone, OSError) as exc:  # drop this connection, go on with the next one
        log.warning("FastCGI connection dropped: %s", exc)
    finally:
        rfile.close()
    return served


# -- the process ---------------------------------------------------------------

class CodeWatch:
    """Tells when the code of this process changed on disk: the .py files of the package (and of `dirs`) and the
    given files. Any difference counts, an older time too (copying tools may keep the original times)."""

    def __init__(self, files: Sequence[str] = (), dirs: Sequence[str] = ()) -> None:
        self.files = [os.path.abspath(f) for f in files]
        self.dirs = [os.path.abspath(d) for d in dirs] or [os.path.dirname(os.path.abspath(__file__))]
        self.checked = time.monotonic()
        self.signature = self._signature()

    def _signature(self) -> str:
        paths = list(self.files)
        for top in self.dirs:
            for root, dirnames, filenames in os.walk(top):
                dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
                paths.extend(os.path.join(root, f) for f in sorted(filenames) if f.endswith(".py"))
        h = hashlib.sha256()
        for p in paths:
            try:
                st = os.stat(p)
                h.update(("%s\0%d\0%d\n" % (p, st.st_mtime_ns, st.st_size)).encode("utf-8", "surrogateescape"))
            except OSError:
                h.update(("%s\0-\n" % p).encode("utf-8", "surrogateescape"))
        return h.hexdigest()

    def changed(self, every: float = CHECK_SECONDS) -> bool:
        now = time.monotonic()
        if now - self.checked < every:
            return False
        self.checked = now
        return self._signature() != self.signature


class LazyApp:
    """The application, loaded on first use. When loading fails (an update half copied, a broken install),
    each request answers 500 and tries again: a process serving an error never needs a restart to recover."""

    def __init__(self, load: Callable[[], WsgiApp]) -> None:
        self.load = load
        self.app: Optional[WsgiApp] = None

    def __call__(self, environ: Dict[str, Any], start_response: Callable[..., Any]) -> Iterable[bytes]:
        if self.app is None:
            try:
                self.app = self.load()
            except Exception:
                log.exception("the Tidesight web app could not be loaded")
                start_response("500 Internal Server Error", [("Content-Type", "text/plain; charset=utf-8"),
                                                             ("Cache-Control", "no-store")])
                return [b"Tidesight could not start: the web server's error log says why.\n"]
        return self.app(environ, start_response)


class _Stopped(Exception):
    pass


class _Stopper:
    """Stopping on a signal (mod_fcgid sends SIGTERM to an idle process it retires, or on a restart): at once
    when idle, else after the request in progress."""

    SIGNALS = ("SIGTERM", "SIGINT", "SIGHUP", "SIGUSR1")

    def __init__(self) -> None:
        self.idle = False
        self.requested = False
        self.previous: Dict[int, Any] = {}

    def __enter__(self) -> "_Stopper":
        if threading.current_thread() is threading.main_thread():  # signals reach the main thread only
            for name in self.SIGNALS:
                sig = getattr(signal, name, None)
                if sig is not None:
                    self.previous[sig] = signal.signal(sig, self._handler)
        return self

    def __exit__(self, *exc: Any) -> None:
        for sig, handler in self.previous.items():
            signal.signal(sig, handler)

    def _handler(self, signum: int, frame: Any) -> None:
        self.requested = True
        if self.idle:
            raise _Stopped()


def serve(sock: socket.socket, load: Callable[[], WsgiApp], watch: Sequence[str] = (),
          max_requests: int = MAX_REQUESTS, check_seconds: float = CHECK_SECONDS,
          watch_dirs: Sequence[str] = ()) -> int:
    """Answer requests arriving on a listening socket until it is time to stop. Returns the number answered."""
    app = LazyApp(load)
    code = CodeWatch(watch, watch_dirs)
    served = 0
    # Idle, wake up now and then to look for new code too: the next request then finds a fresh process.
    sock.settimeout(max(check_seconds, 1.0))
    with _Stopper() as stopper:
        try:
            while not stopper.requested:
                stopper.idle = True
                try:
                    conn, _ = sock.accept()
                except socket.timeout:
                    if code.changed(0):
                        log.info("code changed: idle FastCGI process %d stops", os.getpid())
                        break
                    continue
                except OSError as exc:  # the socket was closed, or the process is out of descriptors
                    log.warning("FastCGI accept failed, stopping: %s", exc)
                    break
                finally:
                    stopper.idle = False
                with conn:
                    conn.settimeout(IO_TIMEOUT)
                    served += serve_connection(conn, app)
                if served >= max_requests:
                    log.info("FastCGI process retiring after %d requests", served)
                    break
                if code.changed(check_seconds):
                    log.info("code changed: FastCGI process %d makes way for a fresh one", os.getpid())
                    break
        except _Stopped:
            pass
    return served


def listening_socket() -> Optional[socket.socket]:
    """The listening socket a FastCGI server passes as standard input, or None (CGI, or a shell)."""
    try:
        sock = socket.socket(fileno=os.dup(0))
    except (OSError, ValueError):  # standard input is not a socket (or is closed)
        return None
    try:
        sock.getpeername()
    except OSError:  # not connected: a listening socket, so this is FastCGI
        return sock
    sock.close()  # a connected socket: some servers run CGI that way
    return None


def run(load: Callable[[], WsgiApp], watch: Sequence[str] = ()) -> None:
    """Serve the application loaded by `load` as a FastCGI process, or answer one CGI request."""
    sock = listening_socket()
    if sock is None:
        if "GATEWAY_INTERFACE" not in os.environ:
            sys.stderr.write("This is the web server's entry point for Tidesight (FastCGI, or CGI): the web server runs "
                             "it. To check the installation, run: python3 -m tidesight.jobs status\n")
            raise SystemExit(2)
        from wsgiref.handlers import CGIHandler
        CGIHandler().run(LazyApp(load))
        return
    with sock:
        serve(sock, load, watch=watch)
