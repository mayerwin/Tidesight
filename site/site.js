// Tidesight's website: the tour's tabs, copy buttons, and the GitHub stars (asked once an hour, kept in this browser).
(function () {
  'use strict';

  // The tour: one screen at a time, arrow keys between the tabs.
  var tabs = Array.prototype.slice.call(document.querySelectorAll('.tour-tabs [role="tab"]'));
  function select(tab, focus) {
    tabs.forEach(function (t) {
      var on = t === tab;
      t.setAttribute('aria-selected', String(on));
      t.tabIndex = on ? 0 : -1;
      document.getElementById(t.getAttribute('aria-controls')).hidden = !on;
    });
    if (focus) tab.focus();
  }
  tabs.forEach(function (t, i) {
    t.tabIndex = i === 0 ? 0 : -1;
    t.addEventListener('click', function () { select(t); });
    t.addEventListener('keydown', function (e) {
      var d = { ArrowDown: 1, ArrowRight: 1, ArrowUp: -1, ArrowLeft: -1 }[e.key];
      if (!d) return;
      e.preventDefault();
      select(tabs[(i + d + tabs.length) % tabs.length], true);
    });
  });

  // Copy buttons next to the commands.
  document.querySelectorAll('.code .copy').forEach(function (b) {
    b.addEventListener('click', function () {
      var text = b.closest('.code').querySelector('code').textContent.replace(/\s+#.*$/gm, '');
      var done = function () { b.textContent = 'Copied'; setTimeout(function () { b.textContent = 'Copy'; }, 1600); };
      if (navigator.clipboard) navigator.clipboard.writeText(text).then(done, function () {});
    });
  });

  // Stars on GitHub, when there are some.
  var counts = document.querySelectorAll('[data-gh-stars]');
  if (!counts.length) return;
  var KEY = 'tidesight_site_stars';
  function show(n) {
    if (typeof n !== 'number' || n < 1) return;
    var text = n >= 1000 ? (n / 1000).toFixed(n >= 10000 ? 0 : 1) + 'k' : String(n);
    counts.forEach(function (c) { c.textContent = text; c.hidden = false; c.title = n + ' stars on GitHub'; });
  }
  var cached = null;
  try { cached = JSON.parse(localStorage.getItem(KEY)); } catch (e) { /* none */ }
  if (cached && typeof cached.n === 'number') show(cached.n);
  if (cached && Date.now() - cached.at < 3600000) return;
  fetch('https://api.github.com/repos/mayerwin/Tidesight', { headers: { Accept: 'application/vnd.github+json' } })
    .then(function (r) { return r.ok ? r.json() : null; })
    .then(function (repo) {
      if (!repo || typeof repo.stargazers_count !== 'number') return;
      show(repo.stargazers_count);
      try { localStorage.setItem(KEY, JSON.stringify({ n: repo.stargazers_count, at: Date.now() })); } catch (e) { /* private mode */ }
    })
    .catch(function () { /* offline, or GitHub's hourly allowance is used up: no count */ });
})();
