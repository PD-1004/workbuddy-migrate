// Exercise visibility changes against the HTML actually packed in the EXE.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {execFileSync} = require('node:child_process');
const root = path.join(__dirname, '..');
const html = execFileSync('python', ['-c', "import sys,pathlib;sys.path.insert(0,'repair');import build_repaired as b;d=next(pathlib.Path('修复版').glob('*.exe')).read_bytes();sys.stdout.buffer.write(next(b.unpack(e) for e in b.archive(d)[3] if e['name']=='wb_ui.html'))"], {cwd:root}).toString('utf8');
const buttonMarkup = html.match(/<button[^>]*id="update-button"[^>]*>/)[0];
const script = fs.readFileSync(path.join(__dirname, 'update_ui.html'), 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];

async function run() {
  let response;
  const elements = {};
  const document = {getElementById(id) {
    if (!elements[id]) {
      const classes = new Set();
      elements[id] = {hidden:id === 'update-button' && /\bhidden\b/.test(buttonMarkup),
        classList:{add:c=>classes.add(c), remove:c=>classes.delete(c), contains:c=>classes.has(c)}};
    }
    return elements[id];
  }};
  const context = vm.createContext({document, busyOn:false, setTimeout:()=>{},
    get:()=>response instanceof Error ? Promise.reject(response) : Promise.resolve(response),
    toast:()=>{}, window:{open:()=>{}}});
  vm.runInContext(script, context);
  const button = document.getElementById('update-button');
  assert.equal(button.hidden, true, 'hidden before the first update check');
  async function check(value) {
    response = value;
    context.checkUpdate(false);
    await new Promise(resolve=>setImmediate(resolve));
  }
  await check({ok:true, available:false, current:'1.0.1'});
  assert.equal(button.hidden, true, 'latest version has no update entry');
  await check({ok:true, available:true, current:'1.0.1', version:'1.0.2', notes:'test'});
  assert.equal(button.hidden, false, 'show only after a newer version is confirmed');
  assert.equal(document.getElementById('update-mask').classList.contains('on'), true);
  context.closeUpdate();
  assert.equal(button.hidden, false, 'dismissing the popup keeps the available update entry');
  await check({ok:true, available:false, current:'1.0.1'});
  assert.equal(button.hidden, true, 'hide again if the update is no longer available');
  await check({ok:false, available:false, current:'1.0.1'});
  assert.equal(button.hidden, true, 'failed checks do not show the entry');
  await check(new Error('offline'));
  assert.equal(button.hidden, true, 'network errors do not show the entry');
  console.log('Update visibility: all checks passed');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
