// Device fingerprint (fingerprint, with canvas_, webgl_, audio_ and font_fingerprint) and automation signals
// (bot_signals), sent as one `fp` event. Computed in idle time, one step per idle period, after every full page load;
// sent once per session and again when the result changes (remembered per tab in sessionStorage, key ts_fp).
// Fingerprints are evidence for identity stitching, never an identity on their own.
import { register, idle } from '../lib/dom.js';

const FV = 1; // algorithm version: bump whenever a component's computation changes
const AUTOMATION = ['__playwright__binding__', '__pwInitScripts', 'callPhantom', '_phantom', '__nightmare', 'domAutomation',
  'domAutomationController', 'Cypress', '__selenium_unwrapped', '__webdriver_evaluate', '__driver_evaluate',
  '__webdriver_script_fn', '__webdriver_unwrapped', '__driver_unwrapped', '__selenium_evaluate', '__fxdriver_evaluate',
  '__fxdriver_unwrapped', '_Selenium_IDE_Recorder', '_selenium', 'calledSelenium', '_WEBDRIVER_ELEM_CACHE',
  '__lastWatirAlert', '__lastWatirConfirm', '__lastWatirPrompt', 'webdriver'];
const FONTS = ['Arial Black', 'Arial Narrow', 'Bahnschrift', 'Calibri', 'Cambria', 'Cambria Math', 'Candara',
  'Comic Sans MS', 'Consolas', 'Constantia', 'Corbel', 'Courier New', 'Ebrima', 'Franklin Gothic Medium', 'Gabriola',
  'Gadugi', 'Georgia', 'HoloLens MDL2 Assets', 'Impact', 'Ink Free', 'Javanese Text', 'Leelawadee UI', 'Lucida Console',
  'Lucida Sans Unicode', 'Malgun Gothic', 'Marlett', 'Microsoft Himalaya', 'Microsoft JhengHei', 'Microsoft Sans Serif',
  'MS Gothic', 'MV Boli', 'Myanmar Text', 'Nirmala UI', 'Palatino Linotype', 'Segoe MDL2 Assets', 'Segoe Print',
  'Segoe Script', 'Segoe UI', 'Segoe UI Emoji', 'SimSun', 'Sitka Small', 'Sylfaen', 'Tahoma', 'Trebuchet MS', 'Verdana',
  'Yu Gothic', 'American Typewriter', 'Andale Mono', 'Avenir', 'Avenir Next', 'Baskerville', 'Chalkboard', 'Didot',
  'Futura', 'Geneva', 'Gill Sans', 'Helvetica Neue', 'Hoefler Text', 'Lucida Grande', 'Menlo', 'Monaco', 'Optima',
  'Palatino', 'Cantarell', 'DejaVu Sans', 'Liberation Sans', 'Noto Sans', 'Roboto', 'Ubuntu', 'Fira Sans'];
// WebGL getParameter() names hashed into gl.h (limits, bit depths, versions).
const GL = [0x846E, 0x846D, 0x0D55, 0x0D54, 0x0D56, 0x0D53, 0x8B4D, 0x851C, 0x8DFD, 0x84E8, 0x8872, 0x0D33, 0x8DFC,
  0x8869, 0x8B4C, 0x8DFB, 0x0D3A, 0x0D52, 0x1F01, 0x8B8C, 0x0D57, 0x1F00, 0x1F02];

register('fp', (api) => {
  const fp = api.enabled('fingerprint'), bots = api.enabled('bot_signals');
  if (!fp && !bots) return;
  const w = window, d = document, n = w.navigator, P = w.performance;
  const H = (s) => api.hash(String(s)).toString(36), clock = () => (P && P.now ? P.now() : Date.now());
  const out = { fv: FV };
  let ms = 0;

  const bot = () => {
    // Globals automation tools define on window itself: a page element with that id (<a id="callPhantom">) is
    // no automation, so only own properties count, and never a DOM node.
    const own = (k) => { try { return Object.prototype.hasOwnProperty.call(w, k) && !(w[k] instanceof w.Node); } catch (e) { return false; } };
    const ua = n.userAgent || '', au = AUTOMATION.filter(own);
    Object.keys(d).concat(Object.keys(w)).forEach((k) => { if (/^\$?cdc_|^\$wdc_/.test(k)) au.push(k); });
    ['webdriver', 'selenium', 'driver'].forEach((a) => { if (d.documentElement.getAttribute(a) != null) au.push('@' + a); });
    return {
      wd: n.webdriver ? 1 : 0, pl: (n.plugins || []).length, lg: (n.languages || []).length,
      oz: !w.outerWidth && !w.outerHeight ? 1 : 0, au: au.length ? au : undefined,
      hl: /HeadlessChrome/.test(ua) ? 1 : 0, co: /Chrome\//.test(ua) ? (w.chrome ? 1 : 0) : undefined
    };
  };

  const misc = () => {
    const s = w.screen || {}, M = Math, f = new Float32Array(1), mm = (q) => { try { return w.matchMedia(q).matches; } catch (e) { return false; } };
    const list = (l, k) => H([].map.call(l || [], (p) => p[k]).join());
    let tz;
    try { tz = Intl.DateTimeFormat().resolvedOptions().timeZone; } catch (e) { /* old browser */ }
    f[0] = Infinity;
    f[0] -= f[0]; // NaN bit pattern: differs between CPU architectures
    return {
      pf: n.platform, vnd: n.vendor, pdf: n.pdfViewerEnabled == null ? undefined : n.pdfViewerEnabled ? 1 : 0,
      pl: list(n.plugins, 'name'), mt: list(n.mimeTypes, 'type'),
      mc: H([M.acos(0.123124234234234242), M.acosh(1e308), M.asin(0.123124234234234242), M.asinh(1), M.atanh(0.5),
        M.atan(0.5), M.sin(-1e300), M.sinh(1), M.cos(10.000000000123), M.cosh(1), M.tan(-1e300), M.tanh(1), M.exp(1),
        M.expm1(1), M.log1p(10), M.pow(M.PI, -100), M.cbrt(100)].join()),
      tz, lg: [].slice.call(n.languages || []).join(), sc: [s.width, s.height, s.colorDepth], hc: n.hardwareConcurrency,
      dm: n.deviceMemory, tp: n.maxTouchPoints, ar: new Uint8Array(f.buffer)[3], ps: n.productSub, os: n.oscpu,
      cg: ['rec2020', 'p3', 'srgb'].filter((g) => mm('(color-gamut:' + g + ')'))[0], hdr: mm('(dynamic-range:high)') ? 1 : 0
    };
  };

  // Text with an emoji in two fonts, a gradient and overlapping arcs; drawn twice to detect randomised canvases.
  const canvas = () => {
    const draw = () => {
      const c = d.createElement('canvas');
      c.width = 240;
      c.height = 80;
      const x = c.getContext('2d'), s = 'Cwm fjordbank gly 😃', g = x.createLinearGradient(0, 0, 240, 0);
      x.textBaseline = 'alphabetic';
      x.fillStyle = '#f60';
      x.fillRect(100, 1, 62, 20);
      x.fillStyle = '#069';
      x.font = '11pt "Times New Roman"';
      x.fillText(s, 2, 15);
      x.fillStyle = 'rgba(102,204,0,0.2)';
      x.font = '18pt Arial';
      x.fillText(s, 4, 45);
      g.addColorStop(0, '#f2f');
      g.addColorStop(0.5, '#2ff');
      g.addColorStop(1, '#ff2');
      x.globalCompositeOperation = 'multiply';
      [[150, 50], [175, 50], [162, 65]].forEach((p) => {
        x.fillStyle = g;
        x.beginPath();
        x.arc(p[0], p[1], 18, 0, Math.PI * 2, true);
        x.fill();
      });
      x.fillStyle = '#f9c';
      x.beginPath();
      x.arc(215, 40, 20, 0, Math.PI * 2, true);
      x.arc(215, 40, 8, 0, Math.PI * 2, true);
      x.fill('evenodd');
      return c.toDataURL();
    };
    const a = draw();
    if (a !== draw()) (out.x = out.x || {}).cn = 1; // noise added by the browser or an extension: not stable
    return H(a);
  };

  const webgl = () => {
    const c = d.createElement('canvas'), g = c.getContext('webgl') || c.getContext('experimental-webgl');
    if (!g) return;
    const get = (k) => { const v = g.getParameter(k); return v && typeof v === 'object' && v.length != null ? [].slice.call(v) : v; };
    const dbg = g.getExtension('WEBGL_debug_renderer_info'), an = g.getExtension('EXT_texture_filter_anisotropic');
    const prec = [];
    [0x8B31, 0x8B30].forEach((sh) => [0x8DF0, 0x8DF1, 0x8DF2, 0x8DF3, 0x8DF4, 0x8DF5].forEach((p) => {
      const f = g.getShaderPrecisionFormat(sh, p);
      prec.push(f && [f.rangeMin, f.rangeMax, f.precision]);
    }));
    const r = {
      v: get(dbg ? dbg.UNMASKED_VENDOR_WEBGL : 0x1F00), r: get(dbg ? dbg.UNMASKED_RENDERER_WEBGL : 0x1F01),
      h: H(JSON.stringify([GL.map(get), prec, g.getSupportedExtensions(), g.getContextAttributes(),
        an && get(an.MAX_TEXTURE_MAX_ANISOTROPY_EXT)]))
    };
    const lose = g.getExtension('WEBGL_lose_context');
    if (lose) lose.loseContext(); // free the context: browsers allow only a few
    return r;
  };

  // Installed fonts: text width in each font against the three generic fallbacks (canvas only, no DOM changes).
  const fonts = () => {
    const x = d.createElement('canvas').getContext('2d'), base = ['monospace', 'sans-serif', 'serif'];
    const m = (f) => { x.font = '72px ' + f; return x.measureText('mmMwWLliI0fiflO&1').width; };
    const ref = base.map(m), found = FONTS.filter((f) => base.some((b, i) => m('"' + f + '",' + b) !== ref[i]));
    return { h: H(found.join()), n: found.length };
  };

  // Audio: an oscillator through a compressor, rendered offline; the sum of a window of samples.
  const audio = (cb) => {
    const t = clock();
    let done = 0;
    const end = api.safe((v) => { if (!done) { done = 1; ms += clock() - t; cb(v); } });
    try {
      const AC = w.OfflineAudioContext || w.webkitOfflineAudioContext;
      if (!AC) return end();
      const ctx = new AC(1, 5000, 44100), osc = ctx.createOscillator(), comp = ctx.createDynamicsCompressor();
      osc.type = 'triangle';
      osc.frequency.value = 10000;
      [['threshold', -50], ['knee', 40], ['ratio', 12], ['attack', 0], ['release', 0.25]].forEach((p) => {
        if (comp[p[0]]) comp[p[0]].value = p[1];
      });
      osc.connect(comp);
      comp.connect(ctx.destination);
      osc.start(0);
      const got = api.safe((b) => { // runs from an event handler and a promise: must never throw
        const s = b && b.getChannelData(0);
        let sum = 0;
        for (let i = 4500; s && i < 5000; i++) sum += Math.abs(s[i]);
        end(s ? sum : undefined);
      });
      setTimeout(end, 1000); // rendering can stall (e.g. in background tabs)
      ctx.oncomplete = (e) => got(e.renderedBuffer);
      const p = ctx.startRendering();
      if (p && p.then) p.then(got, () => end());
    } catch (e) { end(); }
  };

  const steps = [];
  if (bots) steps.push(() => { out.bot = bot(); });
  if (fp) {
    steps.push(() => { out.x = misc(); });
    if (api.enabled('canvas_fingerprint')) steps.push(() => { out.c = canvas(); });
    if (api.enabled('webgl_fingerprint')) steps.push(() => { out.gl = webgl(); });
    if (api.enabled('font_fingerprint')) steps.push(() => { out.fo = fonts(); });
  }
  const finish = () => {
    if (fp) { const all = JSON.stringify([out.c, out.gl, out.au, out.fo, out.x]); out.h = H(all) + H('.' + all); }
    const key = api.sid() + '.' + H(JSON.stringify(out));
    let ss = null;
    try { ss = w.sessionStorage; if (ss.getItem('ts_fp') === key) return; } catch (e) { /* storage blocked */ }
    out.ms = Math.round(ms);
    api.send('fp', out);
    try { if (ss) ss.setItem('ts_fp', key); } catch (e) { /* storage blocked */ }
  };
  const next = () => {
    const step = steps.shift();
    if (step) {
      const t = clock();
      try { step(); } catch (e) { api.log(e); }
      ms += clock() - t;
      return idle(api.safe(next), 1000);
    }
    if (fp && api.enabled('audio_fingerprint')) audio((v) => { out.au = v; finish(); }); else finish();
  };
  idle(api.safe(next), 2000);
});
