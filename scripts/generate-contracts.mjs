// Schema is authoritative. Run from any cwd; --check detects stale generated clients.
import { readFile, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
const require = createRequire(new URL('../apps/desktop/package.json', import.meta.url));
const { compile } = require('json-schema-to-typescript');
const Ajv = require('ajv');
const addFormats = require('ajv-formats');
const standaloneCode = require('ajv/dist/standalone');
const root = new URL('../', import.meta.url);
const source = await readFile(new URL('contracts/protocol.schema.json', root), 'utf8');
const schema = JSON.parse(source);
// Expose each definition through a synthetic root for type generation only.
// json-schema-to-typescript cannot resolve root-$ref aliases with unreachableDefinitions.
const typeSchema = structuredClone(schema);
delete typeSchema.$ref;
// Conditional abstention/final-ID rules stay enforced by runtime validators; the generator
// otherwise invents an open index signature for this conditional-only allOf.
for (const name of ['TopicResult', 'Delivery', 'TelegramAttempt', 'AutoRoutePreview']) {
  delete typeSchema.definitions[name].allOf;
}
typeSchema.type = 'object';
typeSchema.properties = Object.fromEntries(Object.keys(schema.definitions).map((name) => [name, { $ref: '#/definitions/' + name }]));
typeSchema.required = Object.keys(schema.definitions);
typeSchema.additionalProperties = false;
const types = await compile(typeSchema, 'ThreadRelayProtocol', {
  bannerComment: '/* Generated from contracts/protocol.schema.json. Do not edit. */',
  unreachableDefinitions: true,
  additionalProperties: false,
});
// Compilation belongs exclusively in this build-time generator. The renderer must
// import ordinary functions because Tauri deliberately forbids unsafe-eval.
const ajv = new Ajv({ strict: false, allErrors: false, validateFormats: true,
  code: { source: true, esm: true, lines: true } });
addFormats(ajv);
ajv.addSchema(schema);
const refs = Object.fromEntries(Object.keys(schema.definitions).map((name) => [
  name, schema.$id + '#/definitions/' + name,
]));
let standalone = standaloneCode(ajv, refs);
// Ajv emits these two CommonJS helper references even for ESM standalone output.
// Convert only known helpers to static imports; future helpers fail generation
// rather than introducing a renderer require/eval fallback.
standalone = standalone
  .replaceAll('require("ajv/dist/runtime/ucs2length").default', 'ucs2length')
  .replaceAll('require("ajv-formats/dist/formats").fullFormats', 'fullFormats')
  .replaceAll('require("ajv/dist/runtime/equal").default', 'equal');
if (/\brequire\s*\(/.test(standalone)) throw new Error('Unsupported standalone validator helper');
const validators = `/* Generated from contracts/protocol.schema.json. Do not edit. */
import ucs2lengthRuntime from 'ajv/dist/runtime/ucs2length.js';
import { fullFormats } from 'ajv-formats/dist/formats.js';
import equalRuntime from 'ajv/dist/runtime/equal.js';
const equal = equalRuntime.default ?? equalRuntime;
const ucs2length = ucs2lengthRuntime.default ?? ucs2lengthRuntime;
` + standalone;
const declarations = '/* Generated standalone validator signatures. Do not edit. */\n' +
  Object.keys(schema.definitions).map((name) => `export function ${name}(value: unknown): boolean;`).join('\n') + '\n';
const runtime = `
import * as compiledValidators from './contracts.validators.mjs';

// Types alone do not validate untrusted JSON. These functions were compiled at
// build time, with no renderer code generation, coercion or default mutation.
const validators: Readonly<Record<string, (value: unknown) => boolean>> = compiledValidators;
export function validateContract(name: string, value: unknown): boolean {
  try { return Object.hasOwn(validators, name) && validators[name](value) === true; }
  catch { return false; }
}
export function isCommand(value: unknown): value is Command { return validateContract('Command', value); }
export function isEvent(value: unknown): value is Event { return validateContract('Event', value); }
`;
const outputs = new Map([
  ['apps/desktop/src/lib/contracts.ts', types + runtime],
  ['apps/desktop/src/lib/contracts.validators.mjs', validators],
  ['apps/desktop/src/lib/contracts.validators.d.mts', declarations],
  ['worker/src/threadrelay/protocol.schema.json', source],
]);
let stale = false;
for (const [path, content] of outputs) {
  const target = new URL(path, root);
  if (process.argv.includes('--check')) {
    const actual = await readFile(target, 'utf8').catch(() => '');
    if (actual !== content) { console.error(`Stale generated contract: ${path}`); stale = true; }
  } else { await writeFile(target, content); }
}
if (stale) process.exitCode = 1;
