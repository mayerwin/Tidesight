// The live demo's boot, run before the dashboard: it starts Tidesight's web app in a worker (worker.js, Pyodide)
// and gives the dashboard a way to call it (window.tidesightDemo, read once by js/api.js). The overlay written in
// index.html by tools/build_site.py tells how the start goes, and goes away when the dashboard can be used.
(function () {
  'use strict';
  var STEPS = { python: 'Loading Python (WebAssembly)…', code: 'Loading Tidesight…', data: 'Loading the demo data…',
    start: 'Starting Tidesight…' };
  var waiting = new Map();
  var next = 1;
  var bytes = { code: [0, 0], data: [0, 0] };
  var worker;

  function $(id) { return document.getElementById(id); }
  function step(text, fraction) {
    var s = $('demo-boot-step'), f = $('demo-boot-fill');
    if (s && text) s.textContent = text;
    if (f && fraction !== undefined) f.style.width = Math.round(Math.max(0.04, Math.min(1, fraction)) * 100) + '%';
  }
  function finish() {
    var o = $('demo-boot');
    if (!o) return;
    o.classList.add('done');
    setTimeout(function () { o.remove(); }, 450);
  }
  function fail(message) {
    var o = $('demo-boot');
    if (!o) return;
    o.classList.add('failed');
    step('The demo could not start: ' + message + '. It needs a recent browser with WebAssembly and module workers.');
  }
  function settle(id, status, reason, headers, body) {
    var p = waiting.get(id);
    if (!p) return;
    waiting.delete(id);
    p(new Response(body, { status: status, statusText: reason || '', headers: headers || [] }));
  }

  try {
    worker = new Worker('worker.js', { type: 'module' });
  } catch (e) {
    document.addEventListener('DOMContentLoaded', function () { fail(e.message); });
    return;
  }
  worker.onmessage = function (e) {
    var m = e.data;
    if (m.id) return settle(m.id, m.status, m.reason, m.headers, m.body);
    if (m.ready) { step('Ready', 1); return finish(); }
    if (m.failed) return fail(m.failed);
    if (m.progress) {
      if (bytes[m.progress]) bytes[m.progress] = [m.done || 0, m.total || 0];
      var got = bytes.code[0] + bytes.data[0], all = bytes.code[1] + bytes.data[1];
      var label = STEPS[m.progress] || '';
      if (m.progress === 'data' && m.total) label = 'Loading the demo data… ' + (m.done / 1e6).toFixed(1) + ' / ' + (m.total / 1e6).toFixed(1) + ' MB';
      step(label, m.progress === 'start' ? 0.95 : all ? 0.1 + 0.8 * got / all : 0.08);
    }
  };
  worker.onerror = function (e) { fail((e && e.message) || 'the worker stopped'); };

  // What js/api.js calls instead of fetch(): the request goes to the worker, the answer comes back as a Response.
  window.tidesightDemo = {
    fetch: function (url, init) {
      init = init || {};
      return new Promise(function (resolve) {
        var id = next++;
        waiting.set(id, resolve);
        worker.postMessage({ id: id, url: String(url), method: String(init.method || 'GET').toUpperCase(),
          headers: init.headers || {}, body: init.body === undefined || init.body === null ? null : String(init.body) });
      });
    },
  };
})();
