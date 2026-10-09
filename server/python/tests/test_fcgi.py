"""The FastCGI runner (tidesight/fcgi.py), driven the way Apache's mod_fcgid drives it: a listening socket, a
connection per request, FastCGI records both ways. Also the entry script, run as CGI and (POSIX) as FastCGI."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

import pytest

from conftest import BASE_TOML, envelope
from tidesight import fcgi
from tidesight.config import loads
from tidesight.wsgi import create_app

ENTRY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tidesight.fcgi")


# -- playing the web server --------------------------------------------------------

def request_records(rid: int, params: Dict[str, str], body: bytes = b"", keep: bool = False,
                    role: int = fcgi.RESPONDER) -> bytes:
    out = fcgi.record(fcgi.BEGIN_REQUEST, rid, struct.pack("!HB5x", role, fcgi.KEEP_CONN if keep else 0))
    pairs = fcgi.encode_pairs((k.encode("latin-1"), v.encode("latin-1")) for k, v in params.items())
    for stream, data in ((fcgi.PARAMS, pairs), (fcgi.STDIN, body)):
        for i in range(0, len(data), fcgi.MAX_CONTENT):
            out += fcgi.record(stream, rid, data[i:i + fcgi.MAX_CONTENT])
        out += fcgi.record(stream, rid)
    return out


def cgi_params(method: str = "GET", path: str = "/", query: str = "", body: bytes = b"",
               extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    p = {"GATEWAY_INTERFACE": "CGI/1.1", "REQUEST_METHOD": method, "SCRIPT_NAME": "/tidesight.fcgi",
         "PATH_INFO": path, "QUERY_STRING": query, "REQUEST_URI": path + ("?" + query if query else ""),
         "SERVER_NAME": "stats.example.com", "SERVER_PORT": "443", "SERVER_PROTOCOL": "HTTP/1.1",
         "HTTPS": "on", "REMOTE_ADDR": "203.0.113.9", "HTTP_HOST": "stats.example.com"}
    if body:
        p["CONTENT_LENGTH"] = str(len(body))
    p.update(extra or {})
    return p


class Answer:
    def __init__(self, stdout: bytes, protocol_status: int, records: int) -> None:
        head, _, self.body = stdout.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n") if head else []
        self.headers = {k.lower(): v.strip() for k, _, v in (line.partition(":") for line in lines)}
        self.code = int(self.headers.get("status", "0 ?").split()[0])
        self.protocol_status = protocol_status
        self.records = records

    def json(self) -> Any:
        return json.loads(self.body.decode("utf-8"))


def read_answer(rfile: Any, rid: int = 1) -> Answer:
    stdout, n = bytearray(), 0
    while True:
        rec = fcgi.read_record(rfile)
        assert rec is not None, "connection closed before END_REQUEST"
        rtype, got_rid, content = rec
        assert got_rid == rid
        if rtype == fcgi.STDOUT:
            stdout += content
            n += 1 if content else 0
        elif rtype == fcgi.END_REQUEST:
            return Answer(bytes(stdout), struct.unpack("!IB3x", content)[1], n)


class Server:
    """fcgi.serve() in a thread, on a TCP socket (AF_UNIX is not on every test machine)."""

    def __init__(self, app: Any, **kw: Any) -> None:
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.addr = self.sock.getsockname()
        self.served: Optional[int] = None
        self.thread = threading.Thread(target=self._run, args=(app, kw), daemon=True)
        self.thread.start()

    def _run(self, app: Any, kw: Dict[str, Any]) -> None:
        self.served = fcgi.serve(self.sock, lambda: app, **kw)

    def connect(self) -> socket.socket:
        return socket.create_connection(self.addr, timeout=20)

    def request(self, method: str = "GET", path: str = "/", body: bytes = b"", **kw: Any) -> Answer:
        with self.connect() as c:
            c.sendall(request_records(1, cgi_params(method, path, body=body, **kw), body))
            return read_answer(c.makefile("rb"))

    def stop(self) -> None:
        self.sock.close()
        self.thread.join(5)


@pytest.fixture
def fastcgi():
    servers: List[Server] = []

    def start(app: Any, **kw: Any) -> Server:
        s = Server(app, **kw)
        servers.append(s)
        return s

    yield start
    for s in servers:
        s.stop()


def echo_app(environ: Dict[str, Any], start_response: Any) -> List[bytes]:
    body = environ["wsgi.input"].read()
    keys = ("REQUEST_METHOD", "PATH_INFO", "QUERY_STRING", "CONTENT_LENGTH", "wsgi.url_scheme", "wsgi.run_once",
            "wsgi.multiprocess", "HTTP_X_NAME")
    out = {k: environ.get(k) for k in keys}
    out["sha256"] = hashlib.sha256(body).hexdigest()
    out["size"] = len(body)
    start_response("200 OK", [("Content-Type", "application/json"), ("X-Test", "yes")])
    return [json.dumps(out).encode()]


# -- the protocol --------------------------------------------------------------------

def test_name_value_pairs_round_trip():
    pairs = [(b"", b""), (b"A", b"x" * 127), (b"B" * 128, b"y" * 128), (b"C", b"z" * 70000)]
    assert fcgi.decode_pairs(fcgi.encode_pairs(pairs)) == pairs
    with pytest.raises(fcgi.ProtocolError):
        fcgi.decode_pairs(fcgi.encode_pairs([(b"NAME", b"value")])[:-1])
    with pytest.raises(fcgi.ProtocolError):
        fcgi.decode_pairs(b"\x80\x00")  # a four-byte length cut short


def test_get_request(fastcgi):
    s = fastcgi(echo_app)
    a = s.request("GET", "/a/b", query="x=1", extra={"HTTP_X_NAME": "café".encode("utf-8").decode("latin-1")})
    assert a.code == 200 and a.headers["x-test"] == "yes" and a.protocol_status == fcgi.REQUEST_COMPLETE
    out = a.json()
    assert out["REQUEST_METHOD"] == "GET" and out["PATH_INFO"] == "/a/b" and out["QUERY_STRING"] == "x=1"
    assert out["wsgi.url_scheme"] == "https" and out["wsgi.run_once"] is False and out["wsgi.multiprocess"] is True
    # Header bytes arrive as Latin-1 text (PEP 3333): encode back and decode as UTF-8 to read them.
    assert out["HTTP_X_NAME"].encode("latin-1").decode("utf-8") == "café"
    assert out["size"] == 0


def test_large_bodies_span_records_and_spill_to_disk(fastcgi):
    s = fastcgi(echo_app)
    for size in (65535, 65536, 300000, fcgi.BODY_IN_MEMORY + 5000):
        body = os.urandom(size)
        out = s.request("POST", "/collect", body=body).json()
        assert out["size"] == size and out["sha256"] == hashlib.sha256(body).hexdigest()
        assert out["CONTENT_LENGTH"] == str(size)


def test_chunked_upload_gets_a_length(fastcgi):
    s = fastcgi(echo_app)
    with s.connect() as c:
        params = cgi_params("POST", "/collect")  # no CONTENT_LENGTH: the client sent the body in chunks
        c.sendall(request_records(1, params, b"hello"))
        out = read_answer(c.makefile("rb")).json()
    assert out["CONTENT_LENGTH"] == "5" and out["size"] == 5


def test_long_answer_streams_in_records(fastcgi):
    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "application/octet-stream")])
        return (bytes([i]) * 100000 for i in range(3))

    a = fastcgi(app).request()
    assert a.code == 200 and a.body == b"".join(bytes([i]) * 100000 for i in range(3))
    assert a.records >= 5  # sent as it came, in records of at most 64 KB


def test_failing_app_answers_500_and_the_process_goes_on(fastcgi, caplog):
    calls = []

    def app(environ, start_response):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("boom")
        start_response("200 OK", [("Content-Type", "text/plain")])
        return [b"fine"]

    s = fastcgi(app)
    a = s.request()
    assert a.code == 500 and b"boom" not in a.body and a.protocol_status == fcgi.REQUEST_COMPLETE
    assert "boom" in caplog.text
    assert s.request().body == b"fine"


def test_failure_after_the_headers_cuts_the_answer(fastcgi):
    def app(environ, start_response):
        start_response("200 OK", [("Content-Type", "text/plain")])
        yield b"first part"
        raise RuntimeError("late")

    a = fastcgi(app).request()
    assert a.code == 200 and a.body == b"first part" and a.protocol_status == fcgi.REQUEST_COMPLETE


def test_line_breaks_in_headers_are_refused(fastcgi):
    def app(environ, start_response):
        start_response("200 OK", [("X-Bad", "a\r\nSet-Cookie: x=1")])
        return [b"body"]

    a = fastcgi(app).request()
    assert a.code == 500 and "set-cookie" not in a.headers


def test_empty_answer_still_has_headers(fastcgi):
    def app(environ, start_response):
        start_response("204 No Content", [("X-Empty", "1")])
        return []

    a = fastcgi(app).request()
    assert a.code == 204 and a.headers["x-empty"] == "1" and a.body == b""


def test_keep_conn_serves_several_requests_on_one_connection(fastcgi):
    s = fastcgi(echo_app)
    with s.connect() as c:
        rfile = c.makefile("rb")
        for rid, path in ((1, "/one"), (2, "/two")):
            c.sendall(request_records(rid, cgi_params(path=path), keep=True))
            assert read_answer(rfile, rid).json()["PATH_INFO"] == path


def test_management_records(fastcgi):
    s = fastcgi(echo_app)
    with s.connect() as c:
        rfile = c.makefile("rb")
        asked = fcgi.encode_pairs([(b"FCGI_MPXS_CONNS", b""), (b"FCGI_MAX_REQS", b""), (b"OTHER", b"")])
        c.sendall(fcgi.record(fcgi.GET_VALUES, 0, asked) + fcgi.record(99, 0, b"x" * 8))
        rtype, rid, content = fcgi.read_record(rfile)
        assert (rtype, rid) == (fcgi.GET_VALUES_RESULT, 0)
        assert dict(fcgi.decode_pairs(content)) == {b"FCGI_MPXS_CONNS": b"0", b"FCGI_MAX_REQS": b"1"}
        rtype, rid, content = fcgi.read_record(rfile)
        assert (rtype, rid, content[0]) == (fcgi.UNKNOWN_TYPE, 0, 99)
        c.sendall(request_records(1, cgi_params(path="/after")))  # requests still work on that connection
        assert read_answer(rfile).json()["PATH_INFO"] == "/after"


def test_other_roles_and_multiplexing_are_refused(fastcgi):
    s = fastcgi(echo_app)
    with s.connect() as c:
        rfile = c.makefile("rb")
        c.sendall(fcgi.record(fcgi.BEGIN_REQUEST, 5, struct.pack("!HB5x", 2, fcgi.KEEP_CONN)))  # AUTHORIZER
        rtype, rid, content = fcgi.read_record(rfile)
        assert (rtype, rid, struct.unpack("!IB3x", content)[1]) == (fcgi.END_REQUEST, 5, fcgi.UNKNOWN_ROLE)
        first = request_records(1, cgi_params(path="/first"))
        cut = first.index(fcgi.record(fcgi.STDIN, 1))  # request 1 is still incomplete when request 2 begins
        c.sendall(first[:cut] + fcgi.record(fcgi.BEGIN_REQUEST, 2, struct.pack("!HB5x", fcgi.RESPONDER, 0)))
        rtype, rid, content = fcgi.read_record(rfile)
        assert (rtype, rid, struct.unpack("!IB3x", content)[1]) == (fcgi.END_REQUEST, 2, fcgi.CANT_MPX_CONN)
        c.sendall(first[cut:])
        assert read_answer(rfile).json()["PATH_INFO"] == "/first"


def test_aborted_request_is_not_run(fastcgi):
    runs = []

    def app(environ, start_response):
        runs.append(environ["PATH_INFO"])
        return echo_app(environ, start_response)

    s = fastcgi(app)
    with s.connect() as c:
        rfile = c.makefile("rb")
        first = request_records(1, cgi_params(path="/aborted"), keep=True)
        c.sendall(first[:first.index(fcgi.record(fcgi.STDIN, 1))] + fcgi.record(fcgi.ABORT_REQUEST, 1))
        rtype, rid, _ = fcgi.read_record(rfile)
        assert (rtype, rid) == (fcgi.END_REQUEST, 1)
        c.sendall(request_records(2, cgi_params(path="/next")))
        assert read_answer(rfile, 2).json()["PATH_INFO"] == "/next"
    assert runs == ["/next"]


def test_broken_connections_do_not_stop_the_process(fastcgi):
    s = fastcgi(echo_app)
    with s.connect() as c:
        c.sendall(b"\x01\x01\x00")  # half a record header, then gone
    with s.connect() as c:
        c.sendall(b"\x02" + b"\x00" * 7)  # FastCGI version 2
    with s.connect() as c:
        c.sendall(request_records(1, cgi_params())[:20])
    assert s.request(path="/still-here").json()["PATH_INFO"] == "/still-here"


def test_load_failure_answers_500_and_retries():
    attempts = []

    def load():
        attempts.append(1)
        if len(attempts) == 1:
            raise SyntaxError("half-copied update")
        return echo_app

    lazy = fcgi.LazyApp(load)
    seen = []
    body = lazy({"wsgi.input": None}, lambda status, headers: seen.append(status))
    assert seen == ["500 Internal Server Error"] and b"error log" in b"".join(body)
    env = {"wsgi.input": io.BytesIO(b""), "PATH_INFO": "/ok"}
    lazy(env, lambda status, headers: seen.append(status))
    assert seen[-1] == "200 OK" and len(attempts) == 2


# -- the process ---------------------------------------------------------------------

def test_stops_after_max_requests(fastcgi):
    s = fastcgi(echo_app, max_requests=2)
    s.request()
    s.request()
    s.thread.join(5)
    assert s.served == 2 and not s.thread.is_alive()


def test_changed_code_retires_the_process(fastcgi, tmp_path):
    watched = tmp_path / "app.py"
    watched.write_text("x = 1\n")

    def app(environ, start_response):
        if environ["PATH_INFO"] == "/during-update":  # an update lands while this request runs
            watched.write_text("x = 22\n")
        return echo_app(environ, start_response)

    s = fastcgi(app, watch=[str(watched)], check_seconds=0, watch_dirs=[str(tmp_path)])
    s.request(path="/before")
    assert s.request(path="/during-update").json()["PATH_INFO"] == "/during-update"  # still answered
    s.thread.join(5)
    assert s.served == 2 and not s.thread.is_alive()


def test_idle_process_stops_when_the_code_changes(fastcgi, tmp_path):
    watched = tmp_path / "app.py"
    watched.write_text("x = 1\n")
    s = fastcgi(echo_app, watch=[str(watched)], check_seconds=0, watch_dirs=[str(tmp_path)])
    s.request()
    watched.write_text("x = 22\n")  # no request comes: the process notices by itself, within a second or two
    s.thread.join(10)
    assert s.served == 1 and not s.thread.is_alive()


def test_code_watch_sees_new_and_older_files(tmp_path):
    (tmp_path / "a.py").write_text("a = 1\n")
    w = fcgi.CodeWatch(dirs=[str(tmp_path)])
    assert not w.changed(0)
    os.utime(tmp_path / "a.py", (1, 1))  # an older time (copied with its original date) is a change too
    assert w.changed(0)
    w2 = fcgi.CodeWatch(dirs=[str(tmp_path)])
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "a.cpython.pyc").write_text("compiled")
    (tmp_path / "notes.txt").write_text("not code")
    assert not w2.changed(0)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.py").write_text("b = 1\n")
    assert w2.changed(0)


# -- the Tidesight app through FastCGI -----------------------------------------------

@pytest.fixture
def world(tmp_path):
    data_dir = str(tmp_path / "data").replace("\\", "/")
    text = BASE_TOML.format(data_dir=data_dir, rate=600, storage_mode="sqlite", journal_mode="wal")
    path = tmp_path / "tidesight.toml"
    path.write_text(text, encoding="utf-8")
    return loads(text, str(path))


def test_tidesight_app_answers_and_stores_hits(fastcgi, world):
    s = fastcgi(create_app(config=world))
    health = s.request("GET", "/health")
    assert health.code == 200 and health.json()["status"] == "ok"
    body = gzip.compress(json.dumps(envelope()).encode())
    hit = s.request("POST", "/collect", body=body, extra={"CONTENT_TYPE": "text/plain;charset=UTF-8",
                                                          "HTTP_ORIGIN": "https://example.com",
                                                          "HTTP_USER_AGENT": "Mozilla/5.0 Chrome/129.0"})
    assert hit.code == 204
    conn = sqlite3.connect(os.path.join(world.data_dir, "events.db"))
    try:
        assert conn.execute("SELECT COUNT(*) FROM raw_hits").fetchone()[0] == 1
        assert conn.execute("SELECT body_encoding FROM raw_requests").fetchone()[0] == "gzip"
    finally:
        conn.close()
    page = s.request("GET", "/dashboard/")  # no user yet: the page says how to add one
    assert page.code == 403 and b"user set NAME" in page.body


def _entry_env(world: Any, **extra: str) -> Dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith(("HTTP_", "REQUEST_", "PATH_INFO", "QUERY_"))}
    env.update({"TIDESIGHT_CONFIG": world.path, "PYTHONDONTWRITEBYTECODE": "1"})
    env.update(extra)
    return env


def test_entry_script_answers_as_cgi(world):
    env = _entry_env(world, GATEWAY_INTERFACE="CGI/1.1", REQUEST_METHOD="GET", PATH_INFO="/health",
                     SCRIPT_NAME="/tidesight.fcgi", QUERY_STRING="", SERVER_NAME="stats.example.com",
                     SERVER_PORT="443", SERVER_PROTOCOL="HTTP/1.1", REMOTE_ADDR="203.0.113.9")
    out = subprocess.run([sys.executable, ENTRY], env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr.decode()
    head, _, body = out.stdout.replace(b"\r\n", b"\n").partition(b"\n\n")
    assert b"Status: 200" in head and json.loads(body.decode())["status"] == "ok"


def test_entry_script_explains_itself_in_a_shell(world):
    out = subprocess.run([sys.executable, ENTRY], env=_entry_env(world), stdin=subprocess.DEVNULL,
                         capture_output=True, timeout=60)
    assert out.returncode == 2 and b"entry point" in out.stderr and not out.stdout


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX") or os.name == "nt", reason="needs POSIX sockets as stdin")
def test_entry_script_as_a_fastcgi_child(world, tmp_path):
    """As mod_fcgid runs it: a listening Unix socket as standard input; SIGTERM when idle stops it at once."""
    path = str(tmp_path / "fcgi.sock")
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(path)
    listener.listen(8)
    proc = subprocess.Popen([sys.executable, ENTRY], stdin=listener.fileno(), env=_entry_env(world),
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    listener.close()
    try:
        for _ in range(3):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as c:
                c.settimeout(30)
                c.connect(path)
                c.sendall(request_records(1, cgi_params("GET", "/health")))
                a = read_answer(c.makefile("rb"))
            assert a.code == 200 and a.json()["status"] == "ok"
        assert proc.poll() is None  # one process answered all of them
        time.sleep(0.2)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
        proc.stderr.close()
