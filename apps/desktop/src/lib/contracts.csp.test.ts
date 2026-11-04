// @vitest-environment node
import { execFileSync } from 'node:child_process';
import { mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from 'vite';
import { expect, it } from 'vitest';

it('imports the bundled runtime client and validates every fixture without dynamic code generation', async () => {
  const directory = await mkdtemp(join(tmpdir(), 'threadrelay-csp-'));
  try {
    // Validate the generator's checked-in artifacts before exercising the renderer.
    execFileSync(process.execPath, [resolve('../../scripts/generate-contracts.mjs'), '--check'], {
      encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'],
    });
    // Bundle the actual renderer client, including dependencies, as production does.
    const result = await build({
      configFile: false,
      logLevel: 'silent',
      build: { write: false, minify: false, lib: {
        entry: resolve('src/lib/contracts.ts'), formats: ['es'], fileName: 'contracts',
      } },
    });
    const outputs = Array.isArray(result) ? result : [result];
    const chunks = outputs.flatMap((item) => 'output' in item ? item.output : []);
    const chunk = chunks.find((item) => item.type === 'chunk');
    if (!chunk || chunk.type !== 'chunk') throw new Error('Missing client bundle');
    const modulePath = join(directory, 'contracts.mjs');
    await writeFile(modulePath, chunk.code);
    const runner = `
      import assert from 'node:assert/strict';
      import { readFileSync } from 'node:fs';
      const { validateContract, isCommand, isEvent } = await import(process.argv[1]);
      const fixtures = JSON.parse(readFileSync(process.argv[2], 'utf8'));
      for (const item of fixtures.valid) assert.equal(validateContract(item.contract, item.value), true, item.name);
      for (const item of fixtures.invalid) assert.equal(validateContract(item.contract, item.value), false, item.name);
      assert.equal(validateContract('NotAContract', {}), false);
      assert.equal(isCommand({ version: 1, id: 'csp', type: 'QueryState', payload: {} }), true);
      assert.equal(isEvent({ version: 1, id: 'csp', type: 'Error', payload: { code: 'unsupported', message: 'Unavailable' } }), true);
      console.log('CSP-compatible client: all shared fixtures validated');
    `;
    const output = execFileSync(process.execPath, [
      '--disallow-code-generation-from-strings', '--input-type=module', '--eval', runner,
      pathToFileURL(modulePath).href, resolve('../../contracts/fixtures.json'),
    ], { encoding: 'utf8', stdio: ['ignore', 'pipe', 'pipe'] });
    expect(output.trim()).toBe('CSP-compatible client: all shared fixtures validated');
  } finally { await rm(directory, { recursive: true, force: true }); }
}, 20_000);
