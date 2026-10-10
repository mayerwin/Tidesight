#!/usr/bin/env python3
"""Build the website (GitHub Pages): the landing page, its screenshots, and the live demo.

    cd site && npm ci && cd ..                  # Pyodide, pinned in site/package-lock.json
    python tools/build_site.py --out _site       # then serve _site/ with any static server
    python tools/build_site.py --out _site --no-demo

The live demo is Tidesight's own dashboard and Python web app, running in the visitor's browser: Pyodide (Python
compiled to WebAssembly) answers the dashboard's API calls from a web worker (site/demo), on demo data that
tools/demo.py makes at build time ([dashboard] demo: everything can be seen, nothing can be changed). Nothing is
hosted anywhere and nothing leaves the browser; the data is as fresh as the last build, which
.github/workflows/pages.yml runs every six hours. Every file is served from the site itself: no CDN at runtime.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from typing import List

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
SITE = os.path.join(REPO, "site")
DASHBOARD = os.path.join(REPO, "dashboard")
PYODIDE = os.path.join(SITE, "node_modules", "pyodide")
PYODIDE_FILES = ("pyodide.mjs", "pyodide.asm.mjs", "pyodide.asm.wasm", "python_stdlib.zip", "pyodide-lock.json")
PACKAGES = ("tzdata",)  # Pyodide packages the demo loads (time zones), copied next to the core files
KEEP_RAW_DAYS = 7       # raw payloads older than this are dropped from the demo data (visit timelines keep their steps)


def copytree(src: str, dst: str, skip: tuple = ()) -> None:
    for root, dirs, files in os.walk(src):
        dirs[:] = [d for d in dirs if d not in skip and d != "node_modules" and not d.startswith(".")]
        rel = os.path.relpath(root, src)
        os.makedirs(os.path.join(dst, rel), exist_ok=True)
        for name in files:
            if name in skip or name.startswith("."):
                continue
            shutil.copy2(os.path.join(root, name), os.path.join(dst, rel, name))


def landing(out: str) -> None:
    """The landing page, the screenshots (docs/images) and the fonts (the dashboard's, vendored)."""
    copytree(SITE, out, skip=("demo", "package.json", "package-lock.json", "README.md"))
    copytree(os.path.join(REPO, "docs", "images"), os.path.join(out, "images"))
    shutil.copy2(os.path.join(DASHBOARD, "favicon.svg"), os.path.join(out, "favicon.svg"))
    fonts = os.path.join(out, "fonts")
    os.makedirs(fonts, exist_ok=True)
    for name in os.listdir(os.path.join(DASHBOARD, "vendor", "fonts")):
        shutil.copy2(os.path.join(DASHBOARD, "vendor", "fonts", name), os.path.join(fonts, name))
    with open(os.path.join(out, ".nojekyll"), "w") as fh:
        fh.write("")


def pyodide(dest: str, cache: str) -> str:
    """Pyodide's core files and the packages the demo needs, checked against Pyodide's lock file."""
    if not os.path.isdir(PYODIDE):
        raise SystemExit("Pyodide is missing: run `npm ci` in site/ first")
    os.makedirs(dest, exist_ok=True)
    for name in PYODIDE_FILES:
        shutil.copy2(os.path.join(PYODIDE, name), os.path.join(dest, name))
    with open(os.path.join(PYODIDE, "package.json"), encoding="utf-8") as fh:
        version = json.load(fh)["version"]
    with open(os.path.join(PYODIDE, "pyodide-lock.json"), encoding="utf-8") as fh:
        lock = json.load(fh)
    os.makedirs(cache, exist_ok=True)
    for pkg in PACKAGES:
        info = lock["packages"][pkg]
        cached = os.path.join(cache, info["file_name"])
        if not os.path.exists(cached):  # once, at build time: the site serves its own copy
            url = "https://cdn.jsdelivr.net/pyodide/v%s/full/%s" % (version, info["file_name"])
            with urllib.request.urlopen(url, timeout=60) as res:
                data = res.read()
            with open(cached + ".part", "wb") as fh:
                fh.write(data)
            os.replace(cached + ".part", cached)
        with open(cached, "rb") as fh:
            if hashlib.sha256(fh.read()).hexdigest() != info["sha256"]:
                os.remove(cached)
                raise SystemExit("%s does not match Pyodide's lock file" % info["file_name"])
        shutil.copy2(cached, os.path.join(dest, info["file_name"]))
    with open(os.path.join(dest, "NOTICE.txt"), "w", encoding="utf-8") as fh:
        fh.write("Pyodide %s (https://github.com/pyodide/pyodide), Mozilla Public License 2.0: Python compiled to "
                 "WebAssembly, with CPython's standard library (Python Software Foundation License).\n"
                 "Packages: %s, from Pyodide's distribution (see pyodide-lock.json).\n" % (version, ", ".join(PACKAGES)))
    return version


def python_code(dest: str) -> None:
    """Tidesight's Python package and tools/demo.py, zipped for Pyodide (unpacked into /app)."""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        base = os.path.join(REPO, "server", "python")
        for root, dirs, files in os.walk(os.path.join(base, "tidesight")):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if name.endswith(".py"):
                    path = os.path.join(root, name)
                    z.write(path, os.path.relpath(path, base).replace(os.sep, "/"))
        z.write(os.path.join(REPO, "tools", "demo.py"), "demo.py")


def slim(data_dir: str, keep_days: int = KEEP_RAW_DAYS) -> None:
    """Smaller databases for a download. Reports are untouched; for visits older than `keep_days`, Web Vitals,
    timings, scroll, engagement and fingerprint steps leave the visit timelines (pages, clicks and events stay),
    and raw payloads keep no client context. Enrichment's bookkeeping goes too (the browser never enriches)."""
    cutoff = int((time.time() - keep_days * 86400) * 1000)
    light = ("vital", "perf", "scroll", "engagement", "fp")
    marks = ",".join("?" * len(light))
    conn = sqlite3.connect(os.path.join(data_dir, "events.db"))
    conn.execute("DELETE FROM raw_hits WHERE received_at < ? AND type IN (%s)" % marks, (cutoff,) + light)
    conn.execute("UPDATE raw_hits SET data = json_remove(data, '$.ctx') WHERE received_at < ? AND type = 'pageview'", (cutoff,))
    conn.execute("UPDATE raw_requests SET headers = '{}', envelope = '{}' WHERE received_at < ?", (cutoff,))
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    conn = sqlite3.connect(os.path.join(data_dir, "derived.db"))
    conn.execute("DELETE FROM hits WHERE ts < ? AND type IN (%s)" % marks, (cutoff,) + light)
    conn.execute("UPDATE hits SET extra = NULL WHERE ts < ?", (cutoff,))
    conn.execute("DELETE FROM event_ids")
    conn.commit()
    conn.execute("VACUUM")
    conn.close()
    for name in ("replay.db", "web.db"):
        conn = sqlite3.connect(os.path.join(data_dir, name))
        conn.execute("VACUUM")
        conn.close()


def demo_data(dest: str, visitors: int, seed: int) -> None:
    """tools/demo.py's three properties, read-only ([dashboard] demo), slimmed and packed for the browser."""
    work = tempfile.mkdtemp(prefix="tidesight-demo-")
    try:
        subprocess.run([sys.executable, os.path.join(REPO, "tools", "demo.py"), "--out", work, "--light", "--demo",
                        "--journal-mode", "delete", "--visitors", str(visitors), "--seed", str(seed),
                        "--public-url", "https://stats.example.com/"], check=True)
        data = os.path.join(work, "data")
        slim(data)
        with tarfile.open(dest, "w:gz", compresslevel=9) as tar:
            tar.add(os.path.join(work, "tidesight.toml"), "tidesight.toml")
            for name in sorted(os.listdir(data)):
                if name.endswith((".db", ".json")) or name == "geo":
                    tar.add(os.path.join(data, name), "data/" + name)
    finally:
        shutil.rmtree(work, ignore_errors=True)


BOOT = """  <link rel="stylesheet" href="boot.css">
  <script src="boot.js"></script>
"""
OVERLAY = """  <div id="demo-boot" class="demo-boot" role="status" aria-live="polite">
    <div class="demo-boot-card">
      <svg class="demo-boot-mark" viewBox="0 0 34 34" aria-hidden="true"><circle cx="17" cy="17" r="15.5" fill="none" stroke="#9fd1ff" stroke-opacity=".5"/><circle cx="17" cy="17" r="4" fill="#9fd1ff"/><path d="M3 22c3.5-3 7-3 10.5 0s7 3 10.5 0 7-3 7 0" fill="none" stroke="#e9eef3" stroke-width="1.6" stroke-linecap="round"/><path d="M5 26.5c3-2.4 6-2.4 9 0s6 2.4 9 0 4.5-2 6 0" fill="none" stroke="#9fd1ff" stroke-width="1.2" stroke-linecap="round" opacity=".7"/></svg>
      <p class="demo-boot-kicker">Tidesight · live demo</p>
      <h1>Starting Tidesight in your browser</h1>
      <div class="demo-boot-bar"><i id="demo-boot-fill"></i></div>
      <p id="demo-boot-step" class="demo-boot-step">Loading Python (WebAssembly)…</p>
      <p class="demo-boot-note">Tidesight's own dashboard and Python API, running on this page with made-up data for three websites. Nothing is sent anywhere. The first visit downloads about %(mb)s MB.</p>
    </div>
  </div>
"""


def demo(out: str, visitors: int, seed: int, cache: str) -> None:
    dest = os.path.join(out, "demo")
    os.makedirs(dest, exist_ok=True)
    for name in ("index.html", "app.css", "favicon.svg"):
        shutil.copy2(os.path.join(DASHBOARD, name), os.path.join(dest, name))
    for folder in ("js", "vendor"):
        copytree(os.path.join(DASHBOARD, folder), os.path.join(dest, folder))
    for name in ("boot.js", "boot.css", "worker.js", "bridge.py"):
        shutil.copy2(os.path.join(SITE, "demo", name), os.path.join(dest, name))
    version = pyodide(os.path.join(dest, "pyodide"), cache)
    python_code(os.path.join(dest, "tidesight.zip"))
    demo_data(os.path.join(dest, "demo-data.tar.gz"), visitors, seed)
    # What a first visit downloads: GitHub Pages compresses the WebAssembly and the scripts (about 38 % and 22 % of
    # their size), not the archives, which are compressed already.
    shrink = {"pyodide.asm.wasm": 0.38, "pyodide.asm.mjs": 0.22, "pyodide.mjs": 0.3, "pyodide-lock.json": 0.15}
    size = sum(os.path.getsize(os.path.join(dest, "pyodide", n)) * shrink.get(n, 1) for n in PYODIDE_FILES)
    size += sum(os.path.getsize(os.path.join(dest, n)) for n in ("tidesight.zip", "demo-data.tar.gz"))
    size += sum(os.path.getsize(os.path.join(dest, "pyodide", n)) for n in os.listdir(os.path.join(dest, "pyodide")) if n.endswith(".whl"))
    page = os.path.join(dest, "index.html")
    with open(page, encoding="utf-8") as fh:
        html = fh.read()
    html = html.replace("<title>Tidesight</title>", "<title>Tidesight · Live demo</title>\n"
                        "  <meta name=\"description\" content=\"Try Tidesight's dashboard on made-up data: it runs entirely in your browser.\">\n"
                        "  <meta name=\"robots\" content=\"noindex\">", 1)
    html = html.replace('  <script type="module" src="js/main.js"></script>\n', BOOT + '  <script type="module" src="js/main.js"></script>\n', 1)
    html = html.replace("<body>\n", "<body>\n" + OVERLAY % {"mb": max(1, round(size / 1e6))}, 1)
    if "boot.js" not in html or "demo-boot" not in html:
        raise SystemExit("dashboard/index.html changed: update tools/build_site.py")
    with open(page, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(html)
    print("live demo: Pyodide %s, about %.1f MB to download" % (version, size / 1e6))


def main(argv: List[str] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="_site")
    ap.add_argument("--no-demo", action="store_true", help="the landing page only")
    ap.add_argument("--visitors", type=int, default=650, help="the demo shop's visitors (tools/demo.py --visitors)")
    ap.add_argument("--seed", type=int, default=0, help="demo data seed (default: a new one each day)")
    args = ap.parse_args(argv)
    out = os.path.abspath(args.out)
    if os.path.isdir(out):
        shutil.rmtree(out)
    landing(out)
    if not args.no_demo:
        seed = args.seed or int(time.strftime("%Y%m%d"))
        demo(out, args.visitors, seed, os.path.join(SITE, ".cache"))
    print("built %s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
