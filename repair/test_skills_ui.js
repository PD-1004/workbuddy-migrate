const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {execFileSync} = require('node:child_process');
const html = process.env.WBMIG_TEST_HTML ? fs.readFileSync(process.env.WBMIG_TEST_HTML, 'utf8') :
  execFileSync('python', ['-c', "import sys,pathlib,json;sys.path.insert(0,'repair');import build_repaired as b;d=pathlib.Path(json.loads(pathlib.Path('repair/build/build_report.json').read_text(encoding='utf-8'))['output']).read_bytes();sys.stdout.buffer.write(next(b.unpack(e) for e in b.archive(d)[3] if e['name']=='wb_ui.html'))"], {cwd:require('node:path').join(__dirname,'..')}).toString('utf8');
assert.match(html, /id="skills-src"/);
assert.match(html, /id="skills-dst"/);
assert.match(html, /id="skill-mask"/);
// 「打包范围」「携带技能」是两个结构相同的垂直字段（标签在上、控件在下），不再并排
assert.match(html, /<div class="prow vfield selrow" id="scope-row">\s*<span class="plabel">打包范围<\/span>/);
assert.match(html, /<div class="prow vfield" id="skills-src"><\/div>\s*<div id="dest-row">/);
assert.equal(html.includes('prow dual'), false, '不应残留旧的并排布局');
assert.equal(html.includes('id="scope-field"'), false, '不应残留旧的 field 包裹');
const script = html.match(/<script id="skills-script">([\s\S]*?)<\/script>/)[1];

function fakeElement(id) {
  const classes = new Set();
  return {
    id, value:'', textContent:'', innerHTML:'', hidden:false, dataset:{}, onchange:null,
    classList:{add:c=>classes.add(c), remove:c=>classes.delete(c), contains:c=>classes.has(c), toggle:(c,on)=>{on?classes.add(c):classes.delete(c)}},
    querySelectorAll:()=>[],
  };
}

async function run() {
  let reply = {ok:true, skills:[{name:'alpha',description:'A',bytes:100},{name:'beta',description:'B',bytes:200}]};
  const elements = {};
  const requests = [];
  let applied = 0, checks = 0;
  const $ = id => elements[id] ||= fakeElement(id);
  $('pkg-v').textContent = 'package';
  const context = vm.createContext({$, PK:{mode:'sel'}, S:{cache:'source'}, TG:{uid:'target'}, busyOn:false,
    api:(name,payload)=>{requests.push({name,payload});return Promise.resolve(reply);},
    go:()=>{},chooseScope:m=>context.PK.mode=m,renderPaths:()=>{},busy:on=>context.busyOn=on,
    doApply:()=>{applied++;},doCheck:()=>{checks++;},toast:()=>{},
    hsize:n=>n+' B',esc:v=>String(v).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'),
    Promise,console});
  vm.runInContext(script,context);

  // 1) 默认不携带
  assert.equal(context.SK.mode,'none');
  assert.equal($('skill-pick-btn').hidden,true);
  assert.match($('skill-summary').textContent,/不携带技能/);

  // 2) 携带全部技能：只给一行摘要，不渲染、不搜索技能列表
  context.chooseScope('all');
  assert.equal(context.SK.mode,'all');
  await context.loadSkills();
  assert.match($('skill-summary').textContent,/携带全部技能：2 个/);
  assert.match($('skill-summary').textContent,/300 B/);
  assert.equal($('skill-list').innerHTML,'','"全部技能"不应渲染列表');
  assert.equal($('skill-pick-btn').hidden,true,'"全部技能"不显示选择按钮');
  await context.api('collect',{dest:'destination'});
  assert.equal(requests.at(-1).payload.skill_mode,'all');

  // 3) 自定义技能：面板只留入口，选择与搜索都在弹窗里
  context.skillModeChanged('custom');
  assert.equal($('skill-pick-btn').hidden,false);
  assert.equal($('skill-list').innerHTML,'','未打开弹窗时不渲染列表');
  assert.equal($('skill-summary').textContent.includes('已选 0 / 2'),true);

  context.openSkillPicker();
  assert.equal($('skill-mask').classList.contains('on'),true);
  assert.match($('skill-list').innerHTML,/alpha/);
  assert.match($('skill-list').innerHTML,/beta/);
  context.SK.q='bet';
  context.renderSkillPicker();
  assert.match($('skill-list').innerHTML,/beta/);
  assert.equal($('skill-list').innerHTML.includes('alpha'),false,'搜索应过滤列表');
  context.SK.q='';
  context.renderSkillPicker();
  context.skillPickToggle('alpha',true);
  assert.match($('skill-sum').textContent,/已选 1 \/ 2/);
  context.confirmSkillPicker();
  assert.equal($('skill-mask').classList.contains('on'),false);
  assert.equal(context.SK.picked.alpha,true);
  await context.api('collect',{dest:'destination'});
  assert.deepEqual(Array.from(requests.at(-1).payload.skill_names),['alpha']);
  assert.match($('skill-summary').textContent,/100 B/);

  // 4) 弹窗内全选 / 清空
  context.openSkillPicker();
  context.skillPickAll(true);
  assert.match($('skill-sum').textContent,/已选 2 \/ 2 个 · 300 B/);
  context.confirmSkillPicker();
  assert.match($('skill-summary').textContent,/300 B/);
  context.openSkillPicker();
  context.skillPickAll(false);
  context.confirmSkillPicker();
  await context.api('collect',{dest:'destination'});
  assert.deepEqual(Array.from(requests.at(-1).payload.skill_names),[]);

  // 5) 取消不提交草稿
  context.openSkillPicker();
  context.skillPickToggle('beta',true);
  context.closeSkillPicker();
  assert.deepEqual(Object.keys(context.SK.picked).filter(k=>context.SK.picked[k]),[],'取消后不保留草稿');

  // 6) 不可用技能不可选，错误只出现一次
  reply={ok:true,skills:[{name:'alpha',description:'A',bytes:100},
    {name:'broken',description:'',bytes:0,error:'失效链接：broken'}]};
  await context.loadSkills();
  context.openSkillPicker();
  context.skillPickAll(true);
  context.confirmSkillPicker();
  await context.api('collect',{dest:'destination'});
  assert.deepEqual(Array.from(requests.at(-1).payload.skill_names),['alpha'],'unavailable skills cannot be selected');
  context.openSkillPicker();
  assert.match($('skill-list').innerHTML,/disabled/);
  const seen=($('skill-list').innerHTML+$('skill-summary').textContent+$('skill-sum').textContent).split('失效链接：broken').length-1;
  assert.ok(seen<=2,'不可用原因不应重复堆叠');
  context.closeSkillPicker();

  // 7) 现机冲突处理保持不变
  context.TG.uid='';
  reply={ok:true,target_uid:'target',skills:[{name:'alpha',status:'conflict',bytes:100,local_bytes:50},
    {name:'beta',status:'same',bytes:200,local_bytes:200},{name:'gamma',status:'new',bytes:10}]};
  await context.api('check',{pkg:'package'});
  context.TG.uid=reply.target_uid;
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

  // 8) 技能读取失败时不打包过期选择
  context.S.cache='other-source';
  reply={ok:false,text:'unreadable skills'};
  await context.loadSkills();
  const count=requests.length;
  const result=await context.api('collect',{dest:'destination'});
  assert.equal(result.ok,false);
  assert.equal(requests.length,count,'do not pack a stale custom skill selection');
  assert.equal($('skill-summary').textContent.split('unreadable skills').length-1,1,'读取失败原因要直接显示在面板摘要里');

  // 9) 只改工作空间路径也必须让已有 check 结果失效（否则技能按旧目标导入）
  context.S.cache='source';
  context.S.ws='workspace-A';
  context.TG.uid='target';
  reply={ok:true,skills:[],target_uid:'target'};
  await context.api('check',{pkg:'package'});
  assert.equal(context.SK.dstScope,context.skillContext(),'check 后应记录当前目标');
  context.S.ws='workspace-B';  // 只改工作空间，cache/uid/pkg 都没变
  assert.notEqual(context.skillContext(),context.SK.dstScope,'改工作空间后目标标识必须变化');
  const beforeApply=requests.length;
  const stale=await context.api('apply',{pkg:'package'});
  assert.equal(stale.ok,false,'目标已变化时必须拒绝沿用旧检查结果的 apply');
  assert.equal(requests.length,beforeApply,'失效后不应把 apply 请求发出去');

  // 切回原路径时，第一次读取的迟到结果也不能覆盖最新清单。
  let finishOld;
  reply=new Promise(resolve=>{finishOld=resolve});
  const oldRequest=context.loadSkills();
  context.PK={mode:'sel'};
  context.resetVersionSkills();
  assert.equal(Object.keys(context.SK.picked).length,0);
  reply={ok:true,skills:[{name:'current',bytes:1}]};
  await context.loadSkills();
  finishOld({ok:true,skills:[{name:'outdated',bytes:1}]});
  await oldRequest;
  assert.equal(context.SK.items.map(item=>item.name).join(),'current');
  console.log('Skill UI: all/custom 分流、弹窗搜索、草稿提交、冲突与过期态、目标变更失效全部通过');
}

run().catch(error=>{console.error(error);process.exitCode=1;});
