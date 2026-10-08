// Loads the tracker from the collector origin. ?site=, ?bundle= and ?debug select variants.
(function () {
  var p = new URLSearchParams(location.search);
  var me = document.currentScript;
  var site = p.get('site') || (me && me.getAttribute('data-site')) || 'core';
  window.tidesight = window.tidesight || function () { (tidesight.q = tidesight.q || []).push(arguments); };
  var s = document.createElement('script');
  s.async = true;
  s.src = 'http://127.0.0.1:8787/' + (p.get('bundle') || 'ts.js');
  s.setAttribute('data-site', site);
  if (p.has('debug')) s.setAttribute('data-debug', '');
  document.head.appendChild(s);
})();
