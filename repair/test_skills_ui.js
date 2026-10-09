const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {execFileSync} = require('node:child_process');
const html = execFileSync('python', ['-c', "import sys,pathlib,json;sys.path.insert(0,'repair');import build_repaired as b;d=pathlib.Path(json.loads(pathlib.Path('repair/build/build_report.json').read_text(encoding='utf-8'))['output']).read_bytes();sys.stdout.buffer.write(next(b.unpack(e) for e in b.archive(d)[3] if e['name']=='wb_ui.html'))"], {cwd:require('node:path').join(__dirname,'..')}).toString('utf8');
assert.match(html, /id="skills-src"/);
assert.match(html, /id="skills-dst"/);
const script = html.match(/<script id="skills-script">([\s\S]*?)<\/script>/)[1];

async function run() {
  let reply = {ok:true, skills:[{name:'alpha',description:'A',bytes:100},{name:'beta',description:'B',bytes:200}]};
  const elements = {};
  const requests = [];
  let applied = 0, checks = 0;
  const $ = id => elements[id] ||= {value:'',textContent:'',innerHTML:'',hidden:false,querySelectorAll:()=>[]};
  $('pkg-v').textContent = 'package';
  const context = vm.createContext({$, PK:{mode:'sel'}, S:{cache:'source'}, TG:{uid:'target'}, busyOn:false,
    api:(name,payload)=>{requests.push({name,payload});return Promise.resolve(reply);},
    go:()=>{},chooseScope:m=>context.PK.mode=m,renderPaths:()=>{},busy:on=>context.busyOn=on,
    doApply:()=>{applied++;},doCheck:()=>{checks++;},toast:()=>{},
    hsize:n=>n+' B',esc:v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),
    Promise,console});
  vm.runInContext(script,context);
  assert.equal(context.SK.mode,'none');
  context.chooseScope('all');
  assert.equal(context.SK.mode,'all');
  await context.loadSkills();
  assert.match($('skill-list').innerHTML,/alpha/);
  context.skillModeChanged('custom');
  context.skillToggle('alpha',true);
  await context.api('collect',{dest:'destination'});
  assert.equal(requests.at(-1).payload.skill_mode,'custom');
  assert.deepEqual(Array.from(requests.at(-1).payload.skill_names),['alpha']);
  assert.match($('skill-summary').textContent,/100 B/);
  context.skillSetAll(true);
  assert.match($('skill-summary').textContent,/300 B/);
  context.skillSetAll(false);
  await context.api('collect',{dest:'destination'});
  assert.deepEqual(Array.from(requests.at(-1).payload.skill_names),[]);
  reply={ok:true,skills:[{name:'alpha',description:'A',bytes:100},
    {name:'broken',description:'',bytes:0,error:'失效链接：broken'}]};
  await context.loadSkills();
  context.skillSetAll(true);
  await context.api('collect',{dest:'destination'});
  assert.deepEqual(Array.from(requests.at(-1).payload.skill_names),['alpha'],'unavailable skills cannot be selected');
  assert.match($('skill-list').innerHTML,/disabled/);
  assert.equal(($('skill-list').innerHTML+$('skill-summary').textContent).split('失效链接：broken').length-1,1);
  context.TG.uid='';
  reply={ok:true,target_uid:'target',skills:[{name:'alpha',status:'conflict',bytes:100,local_bytes:50},
    {name:'beta',status:'same',bytes:200,local_bytes:200},{name:'gamma',status:'new',bytes:10}]};
  await context.api('check',{pkg:'package'});
  context.TG.uid=reply.target_uid; // The original doCheck also updates the chosen account.
  assert.equal(context.SK.dstScope,context.skillContext(),'automatic target account must not invalidate the check');
  assert.equal(context.SK.actions.alpha,'skip');
  assert.match($('skills-dst').innerHTML,/覆盖/);
  context.skillSetAction('alpha','overwrite');
  await context.api('apply',{pkg:'package'});
  assert.equal(requests.at(-1).payload.skill_actions.alpha,'overwrite');
  context.doApply();
  assert.equal(applied,1);
  $('pkg-v').textContent='other-package';
  context.doApply();
  assert.equal(applied,1,'changed package requires another check');
  assert.equal(checks,1);
  context.S.cache='other-source';
  reply={ok:false,text:'unreadable skills'};
  await context.loadSkills();
  const count=requests.length;
  const result=await context.api('collect',{dest:'destination'});
  assert.equal(result.ok,false);
  assert.equal(requests.length,count,'do not pack a stale custom skill selection');
  assert.equal(($('skill-list').innerHTML+$('skill-summary').textContent).split('unreadable skills').length-1,1,'global error is shown once');
  console.log('Skill UI: selection, sizes, conflicts and stale state checks passed');
}
run().catch(error=>{console.error(error);process.exitCode=1;});
