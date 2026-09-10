import {build} from 'esbuild';
import {readFile, writeFile, readdir} from 'node:fs/promises';
import {fileURLToPath} from 'node:url';
import path from 'node:path';
const root = path.dirname(fileURLToPath(import.meta.url));
for (const [entry, globalName] of [['editor', 'PerchEditor'], ['formatter', 'PerchFormatter']]) {
  await build({absWorkingDir: root, entryPoints: [entry === 'formatter' ? 'formatter-worker.js' : 'editor.js'], outfile: `../src/perch/static/${entry}.min.js`,
    bundle: true, minify: true, format: 'iife', globalName, target: ['safari15', 'chrome100'], legalComments: 'none'});
}
const lock = JSON.parse(await readFile(path.join(root, 'package-lock.json'), 'utf8'));
const licenses = [];
for (const [location, pkg] of Object.entries(lock.packages)) {
  if (!location || pkg.dev) continue;
  const folder = path.join(root, location);
  const license = (await readdir(folder)).find(name => /^licen[sc]e(?:\.md|\.txt)?$/i.test(name));
  if (!license) throw new Error(`Missing license: ${location}`);
  licenses.push(`${location.replace('node_modules/', '')} ${pkg.version}\n${await readFile(path.join(folder, license), 'utf8')}`);
}
await writeFile(path.join(root, '../src/perch/static/licenses/editor.txt'), licenses.join('\n\n---\n\n'));
