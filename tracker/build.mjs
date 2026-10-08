// Builds the tracker into build/ (committed, deployed as-is):
//   build/ts.js               core (classic script)
//   build/ts/<plugin>.js      each plugin, bundled with its shared helpers, loaded on demand by the core
//   build/ts.all.js           core + every plugin except replay, in one file, for sites that prefer one request
//   build/ts/rrweb-record.js  vendored rrweb recorder (MIT), lazy-loaded by the replay plugin
import { build, transform } from 'esbuild';
import { gzipSync } from 'node:zlib';
import { copyFileSync, existsSync, mkdirSync, readFileSync, readdirSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(fileURLToPath(import.meta.url));
const src = join(root, 'src');
const out = join(root, 'build');
// The original target was 4 KB; the core carries context, identity restoration, SPA routing,
// engagement and offline retry, and lands around 6 KB. This is the hard limit.
const CORE_BUDGET = 6 * 1024;
const common = { minify: true, target: 'es2019', legalComments: 'inline', charset: 'utf8' };

mkdirSync(join(out, 'ts'), { recursive: true });

const sizes = [];
function write(path, code) {
  writeFileSync(path, code);
  sizes.push([path.slice(root.length + 1).replace(/\\/g, '/'), Buffer.byteLength(code), gzipSync(code, { level: 9 }).length]);
}
async function bundle(stdinOrEntry) {
  const opts = { ...common, bundle: true, format: 'iife', write: false };
  if (typeof stdinOrEntry === 'string') opts.entryPoints = [stdinOrEntry];
  else opts.stdin = stdinOrEntry;
  const result = await build(opts);
  return result.outputFiles[0].text;
}

const coreSrc = readFileSync(join(src, 'core.js'), 'utf8');
write(join(out, 'ts.js'), (await transform(coreSrc, common)).code);

const pluginDir = join(src, 'plugins');
const plugins = existsSync(pluginDir) ? readdirSync(pluginDir).filter((f) => f.endsWith('.js')).sort() : [];
for (const file of plugins) write(join(out, 'ts', file), await bundle(join(pluginDir, file)));

const imports = ['./core.js', ...plugins.filter((f) => f !== 'replay.js').map((f) => './plugins/' + f)];
write(join(out, 'ts.all.js'), await bundle({
  contents: imports.map((p) => `import ${JSON.stringify(p)};`).join('\n'), resolveDir: src, loader: 'js'
}));

// Vendored rrweb recorder: refreshed from node_modules when present, otherwise the committed copy is kept.
const rrweb = join(root, 'node_modules', '@rrweb', 'record', 'dist', 'record.umd.min.cjs');
if (existsSync(rrweb)) {
  const pkg = JSON.parse(readFileSync(join(root, 'node_modules', '@rrweb', 'record', 'package.json'), 'utf8'));
  const banner = `/*! @rrweb/record ${pkg.version} | MIT License | https://github.com/rrweb-io/rrweb */\n`;
  write(join(out, 'ts', 'rrweb-record.js'), banner + readFileSync(rrweb, 'utf8'));
  const lic = join(root, 'node_modules', '@rrweb', 'record', 'LICENSE');
  if (existsSync(lic)) copyFileSync(lic, join(out, 'ts', 'rrweb-LICENSE.txt'));
}

const pad = (s, n) => String(s).padStart(n);
console.log('file'.padEnd(28) + pad('bytes', 9) + pad('gzip', 9));
for (const [name, raw, gz] of sizes) console.log(name.padEnd(28) + pad(raw, 9) + pad(gz, 9));
const coreGz = sizes.find((s) => s[0] === 'build/ts.js')[2];
if (coreGz > CORE_BUDGET) {
  console.error(`core is ${coreGz} bytes gzipped, over the ${CORE_BUDGET} byte budget`);
  process.exit(1);
}
