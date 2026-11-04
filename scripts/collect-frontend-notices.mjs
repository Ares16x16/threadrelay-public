// Installed, lock-bound production dependency closure. Reads metadata/notices;
// does not import dependency code. Includes QR encoder transitives, even when
// tree-shaking may omit some runtime code. No redistribution legal guarantee.
import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import { fileURLToPath } from 'node:url';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const app = path.join(root, 'apps/desktop');
const output = path.join(root, 'packaging/notices/frontend');
const sha = data => crypto.createHash('sha256').update(data).digest('hex');
const lock = fs.readFileSync(path.join(app, 'pnpm-lock.yaml'));
const lockText = lock.toString();
function resolvePackage(name, from) {
  if (!/^(@[a-z0-9._-]+\/)?[a-z0-9._-]+$/i.test(name)) throw new Error('Invalid dependency name');
  for (let dir = from; ; dir = path.dirname(dir)) {
    const candidate = path.join(dir, 'node_modules', name);
    if (fs.existsSync(path.join(candidate, 'package.json'))) return fs.realpathSync(candidate);
    if (path.dirname(dir) === dir) throw new Error(`Installed dependency missing: ${name}`);
  }
}
const pending = Object.keys(JSON.parse(fs.readFileSync(path.join(app, 'package.json'))).dependencies).map(name => [name, app]);
const records = new Map();
while (pending.length) {
  const [name, from] = pending.pop();
  const directory = resolvePackage(name, from);
  const metadataBytes = fs.readFileSync(path.join(directory, 'package.json'));
  const meta = JSON.parse(metadataBytes);
  const id = `${meta.name}@${meta.version}`;
  if (records.has(id)) continue;
  if (meta.name !== name || ![`${id}:`, `'${id}':`, `"${id}":`].some(key => lockText.includes(key))) throw new Error(`Lock mismatch: ${id}`);
  const noticeNames = fs.readdirSync(directory).filter(name => /^(licen[cs]e|copying|notice)([-_.]|$)/i.test(name));
  if (!noticeNames.length) throw new Error(`License text missing: ${id}`);
  const destination = path.join(output, id.replaceAll('/', '__'));
  fs.mkdirSync(destination, { recursive: true });
  const notices = [];
  for (const name of noticeNames) {
    const source = path.join(directory, name);
    if (!fs.lstatSync(source).isFile()) continue;
    const bytes = fs.readFileSync(source);
    const outputName = name.replace(/\.(?:md|markdown)$/i, '.txt');
    fs.writeFileSync(path.join(destination, outputName), bytes);
    notices.push({ path: path.relative(output, path.join(destination, outputName)).replaceAll('\\', '/'), sha256: sha(bytes) });
  }
  if (!notices.length) throw new Error(`License text unavailable: ${id}`);
  records.set(id, { name: meta.name, version: meta.version, license: meta.license ?? 'review required', package_metadata_sha256: sha(metadataBytes), notices });
  for (const dependency of Object.keys(meta.dependencies ?? {})) pending.push([dependency, directory]);
}
const packages = [...records.values()].sort((a, b) => `${a.name}@${a.version}`.localeCompare(`${b.name}@${b.version}`));
fs.writeFileSync(path.join(output, 'sbom.json'), JSON.stringify({ schema_version: 1, scope: 'installed frontend production dependency closure; not complete native SBOM', lock_sha256: sha(lock), packages }, null, 2) + '\n');
fs.writeFileSync(path.join(output, 'THIRD_PARTY_NOTICES.txt'), '# Frontend third-party notices\n\nGenerated from the pinned installed production dependency closure. Full text is in each listed package directory. Includes qrcode encoder and all installed transitive dependencies. Build-time tree-shaking may omit code; this conservative list is not a complete native-package SBOM.\n\n' + packages.map(p => `- ${p.name} ${p.version}: ${typeof p.license === 'string' ? p.license : JSON.stringify(p.license)}`).join('\n') + '\n');
console.log(JSON.stringify({ packages: packages.length, output: path.relative(root, output), lock_sha256: sha(lock) }));
