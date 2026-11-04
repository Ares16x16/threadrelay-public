/* Copyright (c) Microsoft Corporation and ThreadRelay contributors.
 * Playwright dispatcher/bootstrap adaptations are Apache-2.0. No independent
 * browser launch, network endpoint, renderer argument, or global monkeypatch.
 */
'use strict';
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const MAX = 4 * 1024 * 1024;
const fail = () => process.exit(2);
process.on('uncaughtException', fail);
process.on('unhandledRejection', fail);
process.stdout.on('error', fail);
process.stdin.on('error', fail);
for (const name of Object.keys(process.env)) {
  if (/^(DEBUG|DEBUGP|PWDEBUG|INTERNAL_PW_GREENLET_DEBUG|PLAYWRIGHT_|NODE_|DYLD_|LD_)/.test(name) || /_PROXY$/i.test(name)) fail();
}
if (process.versions.node !== '24.22.1') fail();
const packageRoot = path.resolve(__dirname, '../playwright/package');
const corePath = path.join(packageRoot, 'lib/coreBundle.js');
const digest = crypto.createHash('sha256').update(fs.readFileSync(corePath)).digest('hex');
if (digest !== '829521b67ed7be90c7d6d6098ff39f2241562d47e4ad7ccab13710924a56001d') fail();
const packageInfo = JSON.parse(fs.readFileSync(path.join(packageRoot, 'package.json'), 'utf8'));
if (packageInfo.version !== '1.64.0-beta-1791568591000') fail();
const { DispatcherConnection, RootDispatcher, PlaywrightDispatcher, createPlaywright, nullProgress } = require(corePath).server;
function send(kind, object) {
  const data = Buffer.from(JSON.stringify(object), 'utf8');
  if (!data.length || data.length > MAX || process.stdout.writableLength > MAX * 2) fail();
  const header = Buffer.allocUnsafe(5); header[0] = kind; header.writeUInt32LE(data.length, 1);
  process.stdout.write(header); process.stdout.write(data);
}
const cdp = {
  onmessage: undefined, onclose: undefined,
  send: object => send(2, object),
  close: async () => { if (cdp.onclose) cdp.onclose(); }
};
const connection = new DispatcherConnection();
new RootDispatcher(connection, async (scope, {sdkLanguage}) => {
  if (sdkLanguage !== 'python') throw new Error('unsupported binding');
  const playwright = createPlaywright({sdkLanguage:'python', isClientCollocatedWithServer:true});
  const browser = await playwright.chromium.connectOverCDP(nullProgress, {transport:cdp});
  return new PlaywrightDispatcher(scope, playwright, {preLaunchedBrowser:browser, sharedBrowser:true, denyLaunch:true});
});
connection.onmessage = message => send(1, message);
let pending = Buffer.alloc(0);
process.stdin.on('data', chunk => {
  pending = Buffer.concat([pending, chunk]);
  if (pending.length > MAX + 65541) fail();
  while (pending.length >= 5) {
    const kind = pending[0], size = pending.readUInt32LE(1);
    if ((kind !== 1 && kind !== 2) || !size || size > MAX) fail();
    if (pending.length < size + 5) break;
    const value = JSON.parse(pending.subarray(5, size + 5).toString('utf8'));
    pending = pending.subarray(size + 5);
    if (!value || Array.isArray(value) || typeof value !== 'object') fail();
    if (kind === 1) Promise.resolve(connection.dispatch(value)).catch(fail);
    else if (cdp.onmessage) cdp.onmessage(value);
    else fail();
  }
});
process.stdin.on('end', fail);
