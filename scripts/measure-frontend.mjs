// Cloud source characterization only. Not native startup/render/model speed.
import fs from 'node:fs';
import path from 'node:path';
import zlib from 'node:zlib';
import { performance } from 'node:perf_hooks';
import { pathToFileURL, fileURLToPath } from 'node:url';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const source = path.join(root, 'apps/desktop/src/lib/contracts.validators.mjs');
const sizes = file => { const bytes = fs.readFileSync(file); return { bytes: bytes.length, gzip_bytes: zlib.gzipSync(bytes).length }; };
const start = performance.now();
const validators = await import(pathToFileURL(source).href);
const importMs = performance.now() - start;
const samples = [];
const command = { version: 1, id: 'synthetic-measurement', type: 'QueryState', payload: {} };
for (let batch = 0; batch < 20; batch++) {
  const began = performance.now();
  for (let i = 0; i < 1000; i++) {
    if (!validators.Command(command) || validators.Command({ ...command, extra: true })) throw new Error('Validation regression');
  }
  samples.push((performance.now() - began) / 2000);
}
samples.sort((a, b) => a - b);
const assets = path.join(root, 'apps/desktop/dist/assets');
console.log(JSON.stringify({ scope: 'Linux cloud Node synthetic source characterization; not target UI performance', node: process.version,
  validators_source: sizes(source), validator_initial_import_ms: importMs,
  mixed_valid_invalid_query_validation_ms_per_operation: { median_batch: samples[10], p95_batch: samples[18], batches: 20, operations_per_batch: 2000 },
  built_javascript: fs.readdirSync(assets).filter(name => name.endsWith('.js')).map(name => ({ name, ...sizes(path.join(assets, name)) })) }, null, 2));
