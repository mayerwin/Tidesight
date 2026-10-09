// Refreshes dashboard/vendor/ (committed) from npm packages, so deployment needs no Node and no CDN:
//   vendor/fonts/          Fraunces (titles), IBM Plex Sans (UI and data), IBM Plex Mono (code) + OFL licences
//   vendor/rrweb-player/   session replay player (MIT)
//   vendor/world.json      country outlines (Natural Earth via world-atlas, ISC) projected to Equal Earth SVG paths
import { copyFileSync, mkdirSync, readFileSync, writeFileSync, existsSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';

const root = dirname(fileURLToPath(import.meta.url));
const nm = join(root, 'node_modules');
const out = join(root, 'vendor');
const require = createRequire(import.meta.url);
mkdirSync(join(out, 'fonts'), { recursive: true });
mkdirSync(join(out, 'rrweb-player'), { recursive: true });

const copy = (from, to) => { copyFileSync(join(nm, from), join(out, to)); console.log('vendor/' + to); };

// Fonts
copy('@fontsource-variable/fraunces/files/fraunces-latin-standard-normal.woff2', 'fonts/fraunces-latin.woff2');
copy('@fontsource-variable/fraunces/files/fraunces-latin-standard-italic.woff2', 'fonts/fraunces-latin-italic.woff2');
copy('@fontsource-variable/ibm-plex-sans/files/ibm-plex-sans-latin-wght-normal.woff2', 'fonts/plex-sans-latin.woff2');
copy('@fontsource-variable/ibm-plex-sans/files/ibm-plex-sans-latin-ext-wght-normal.woff2', 'fonts/plex-sans-latin-ext.woff2');
copy('@fontsource-variable/ibm-plex-sans/files/ibm-plex-sans-latin-wght-italic.woff2', 'fonts/plex-sans-latin-italic.woff2');
copy('@fontsource/ibm-plex-mono/files/ibm-plex-mono-latin-400-normal.woff2', 'fonts/plex-mono-latin.woff2');
copy('@fontsource-variable/fraunces/LICENSE', 'fonts/Fraunces-OFL.txt');
copy('@fontsource-variable/ibm-plex-sans/LICENSE', 'fonts/IBMPlex-OFL.txt');

// Session replay player
copy('rrweb-player/dist/rrweb-player.umd.min.cjs', 'rrweb-player/rrweb-player.js');
copy('rrweb-player/dist/style.min.css', 'rrweb-player/rrweb-player.css');
// rrweb-player ships no licence file; it lives in the rrweb monorepo, whose LICENSE (vendored with the tracker's
// recorder, copied verbatim from the rrweb@2.1.7 tag) covers it.
const lic = ['rrweb-player/LICENSE', 'rrweb-player/LICENSE.md'].find((p) => existsSync(join(nm, p)));
if (lic) copy(lic, 'rrweb-player/LICENSE.txt');
else {
  copyFileSync(join(root, '..', 'tracker', 'build', 'ts', 'rrweb-LICENSE.txt'), join(out, 'rrweb-player', 'LICENSE.txt'));
  console.log('vendor/rrweb-player/LICENSE.txt (from tracker/build/ts/rrweb-LICENSE.txt)');
}

// World map: Equal Earth projection, viewBox 0 0 1000 487, paths keyed by ISO 3166-1 alpha-2.
const { feature } = require('topojson-client');
const countries = require('i18n-iso-countries');
const topo = JSON.parse(readFileSync(join(nm, 'world-atlas/countries-110m.json'), 'utf8'));
const A1 = 1.340264, A2 = -0.081106, A3 = 0.000893, A4 = 0.003796, M = Math.sqrt(3) / 2;
const XMAX = 2.70663, YMAX = 1.31734, SCALE = 1000 / (2 * XMAX), HEIGHT = Math.round(2 * YMAX * SCALE);
function project(lon, lat) {
  const l = (lon * Math.PI) / 180, p = (lat * Math.PI) / 180;
  const t = Math.asin(M * Math.sin(p)), t2 = t * t, t6 = t2 * t2 * t2;
  const x = (2 * Math.sqrt(3) * l * Math.cos(t)) / (3 * (A1 + 3 * A2 * t2 + t6 * (7 * A3 + 9 * A4 * t2)));
  const y = t * (A1 + A2 * t2 + t6 * (A3 + A4 * t2));
  return [Math.round((x + XMAX) * SCALE * 10) / 10, Math.round((YMAX - y) * SCALE * 10) / 10];
}
function ring(coords) {
  let d = '', last = '';
  for (const [lon, lat] of coords) {
    const [x, y] = project(lon, lat);
    const pt = x + ',' + y;
    if (pt === last) continue;
    d += (d ? 'L' : 'M') + pt;
    last = pt;
  }
  return d + 'Z';
}
const geo = feature(topo, topo.objects.countries);
const result = { viewBox: `0 0 1000 ${HEIGHT}`, projection: 'equal-earth', source: 'Natural Earth via world-atlas 2.0.2', countries: {} };
for (const f of geo.features) {
  const id = f.id ? countries.numericToAlpha2(f.id) : null;
  const key = id || (f.properties.name === 'Kosovo' ? 'XK' : null);
  if (!key) continue;
  const polys = f.geometry.type === 'Polygon' ? [f.geometry.coordinates] : f.geometry.coordinates;
  const d = polys.map((poly) => poly.map(ring).join('')).join('');
  result.countries[key] = { n: f.properties.name, d };
}
writeFileSync(join(out, 'world.json'), JSON.stringify(result));
console.log('vendor/world.json (' + Object.keys(result.countries).length + ' countries)');
