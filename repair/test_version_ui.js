const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const {execFileSync} = require('node:child_process');
const html = process.env.WBMIG_TEST_HTML ? fs.readFileSync(process.env.WBMIG_TEST_HTML, 'utf8') :
  execFileSync('python', ['-c', "import sys,pathlib,json;sys.path.insert(0,'repair');import build_repaired as b;d=pathlib.Path(json.loads(pathlib.Path('repair/build/build_report.json').read_text(encoding='utf-8'))['output']).read_bytes();sys.stdout.buffer.write(next(b.unpack(e) for e in b.archive(d)[3] if e['name']=='wb_ui.html'))"], {cwd:require('node:path').join(__dirname,'..')}).toString('utf8');
assert.match(html, /id="version-script"/);
const script = html.match(/<script id="version-script">([\s\S]*?)<\/script>/)[1];

// 版本切换必须在卡片标题行右侧，而不是压在说明文字下面
assert.match(html, /<div class="cardhead"><div class="htitle"><h3>本机（源机）的两个存储路径<\/h3>[\s\S]{0,600}?<div class="verhost" id="ver-host-src"><\/div><\/div>/);
assert.match(html, /<div class="cardhead"><div class="htitle"><h3>本机（现机）的两个存储路径<\/h3>[\s\S]{0,600}?<div class="verhost" id="ver-host-dst"><\/div><\/div>/);
// 路径卡片不再有整行说明文字（信息改由标题旁悬浮提示承担）
assert.equal(html.includes('识别错了点右侧锁图标'), false, '说明文字与切换控件语义重复');
// 只看到卡片自己的结束标签为止，避免跨到下一张卡
function cardOf(id) {
  const start = html.indexOf('data-od-id="' + id + '"');
  assert.ok(start > -1, '找不到卡片 ' + id);
  const end = html.indexOf('<div class="card', start);
  return html.slice(start, end > -1 ? end : start + 1500);
}
assert.equal(cardOf('path-card-src').includes('class="desc"'), false, '源机路径卡片不应再有 desc 行');
assert.equal(cardOf('path-card-dst').includes('class="desc"'), false, '现机路径卡片不应再有 desc 行');
// 三个帮助入口都在，且配了 tooltip 文本
assert.match(html, /aria-describedby="tip-src-path"/);
assert.match(html, /id="tip-src-path" role="tooltip"/);
assert.match(html, /aria-describedby="tip-dst-path"/);
assert.match(html, /aria-describedby="tip-pack"/);
assert.match(html, /id="tip-pack" role="tooltip"[\s\S]{0,300}?打包范围/);

function host() {
  return {innerHTML:'', set(markup){this.innerHTML=markup;}};
}

async function run() {
  const hosts = {'ver-host-src':host(), 'ver-host-dst':host()};
  const toasts = [], logs = [], saves = [], reloads = [], srcReloads = [];
  let reply = {ok:true, msg:'ok'};
  // 模拟「当前还留着上一个版本账号」的状态
  const TG = {loading:false, accounts:[{uid:'old-intl-uid', name:'国际版账号'}],
              uid:'old-intl-uid', tmp:'old-intl-uid', err:''};
  const SK = {dstScope:'old-scope', actions:{overwrite:true},
              cache:'C:\\Users\\demo\\.workbuddy', items:[{name:'old-skill', error:null}],
              picked:{'old-skill':true}, ready:true, error:''};
  // 模拟源机「打包范围」还留着上一个版本（国内版）的账号与会话统计
  const PK = {mode:'all', accounts:[{uid:'old-cn-uid', alive:12}], uid:'old-cn-uid',
              sessions:[{id:'s1'}], picked:{s1:true}, q:'abc',
              allItems:[{acc:{uid:'old-cn-uid'}, sessions:[{id:'s1'}]}], allTot:{n:12,b:3456},
              step:2, loading:false, allLoading:false, accErr:'', err:''};

  const context = vm.createContext({
    S:{cache:'C:\\Users\\demo\\.workbuddy', ws:'C:\\Users\\demo\\WorkBuddy'},
    editing:{src:{cache:false,ws:false},dst:{cache:false,ws:false}},
    TG, SK, PK, MODAL:{kind:'src'},
    closePick:()=>{context.MODAL.kind=''},
    resetVersionSkills:()=>{SK.dstScope='';SK.actions={};SK.cache=null;SK.items=[];SK.picked={}},
    setSt:()=>{},
    renderTargets:()=>{},
    loadTargets:()=>{reloads.push('accounts')},
    loadAccounts:()=>{srcReloads.push('accounts')},
    loadSkills:()=>{srcReloads.push('skills')},
    renderPick:()=>{},
    syncPackBtn:()=>{},
    busyOn:false,
    busy:on=>{context.busyOn=on},
    $:id => hosts[id] || null,
    api:(name,payload)=>{
      saves.push({name,payload});
      return Promise.resolve(Object.assign({ok:true,msg:'ok',cache:payload.cache,ws:payload.ws}, reply));
    },
    renderPaths:()=>{ Object.keys(hosts).forEach(k=>{ hosts[k].innerHTML=''; }); },
    toast:(m,bad)=>toasts.push({m,bad:!!bad}),
    logLine:m=>logs.push(m),
    Promise, console,
  });
  vm.runInContext(script, context);

  // 1) 当前版本判断
  assert.equal(context.verCurrent(), 'cn');
  context.S.cache = 'C:\\Users\\demo\\.workbuddy-ai';
  assert.equal(context.verCurrent(), 'intl');
  context.S.cache = 'D:\\我的资料\\wb缓存';
  assert.equal(context.verCurrent(), '', '自定义路径不归属任何版本');
  context.S.cache = 'C:\\Users\\demo\\.workbuddy';

  // 2) 渲染时写进标题行右侧的宿主，当前版本高亮且 aria-pressed 正确
  context.renderPaths();
  assert.match(hosts['ver-host-src'].innerHTML, /国内版/);
  assert.match(hosts['ver-host-src'].innerHTML, /国际版/);
  assert.match(hosts['ver-host-dst'].innerHTML, /国际版/);
  assert.equal((hosts['ver-host-src'].innerHTML.match(/class="vbtn on"/g) || []).length, 1, '只应高亮一个');
  assert.match(hosts['ver-host-src'].innerHTML, /data-ver="cn"[^>]*aria-pressed="true"[^>]*>国内版</);
  assert.match(hosts['ver-host-src'].innerHTML, /data-ver="intl"[^>]*aria-pressed="false"/);
  // 紧凑版：不再带长句提示，也不再有「WorkBuddy」前缀标签
  assert.equal(hosts['ver-host-src'].innerHTML.includes('两个路径会换成该版本的默认值'), false, '标题行放不下长句提示');
  assert.equal(hosts['ver-host-src'].innerHTML.includes('vlabel'), false, '版本切换前不再挂 WorkBuddy 文字标签');
  assert.match(hosts['ver-host-src'].innerHTML, /^<div class="verbar"><div class="verseg"/);

  // 3) 切到国际版：按 home 目录换成该版本默认值，并走现有的 paths/save
  const switching = context.switchVersion('intl');
  assert.equal(context.busyOn, true);
  context.switchVersion('intl');
  await switching;
  assert.equal(saves.length, 1);
  assert.equal(saves[0].name, 'paths/save');
  assert.equal(saves[0].payload.cache, 'C:\\Users\\demo\\.workbuddy-ai');
  assert.equal(saves[0].payload.ws, 'C:\\Users\\demo\\WorkBuddy AI');
  assert.equal(context.S.cache, 'C:\\Users\\demo\\.workbuddy-ai');
  assert.equal(context.S.ws, 'C:\\Users\\demo\\WorkBuddy AI');
  // 3b) 旧版本账号必须被清掉并重拉，否则「迁入到本机哪个账号」留着上一个版本的账号
  assert.notEqual(context.TG, TG);
  assert.equal(context.TG.accounts.length, 0, '切换后旧账号列表必须清空');
  assert.equal(context.TG.uid, '', '切换后旧 uid 必须清空');
  assert.equal(context.TG.err, '', '切换后旧错误提示必须清空');
  assert.equal(SK.dstScope, '', '切换后技能检查缓存必须失效');
  assert.equal(Object.keys(SK.actions).length, 0, '切换后技能处理动作必须失效');
  assert.deepEqual(reloads, ['accounts'], '切换后必须重新拉取本机账号');
  // 3c) 源机「打包范围」的账号/会话统计（PK）和「携带技能」列表（SK）也必须
  // 作废并重拉，否则会停留在本机上一个版本的数据（本次修复点）。
  assert.notEqual(context.PK, PK);
  assert.equal(context.PK.mode, 'all', '保留打包方式');
  assert.equal(context.PK.accounts.length, 0, '切换后源机打包范围账号必须清空');
  assert.equal(context.PK.uid, '', '切换后源机所选账号必须清空');
  assert.equal(context.PK.sessions.length, 0, '切换后源机会话清单必须清空');
  assert.equal(Object.keys(context.PK.picked).length, 0);
  assert.equal(context.PK.allItems.length, 0, '切换后全部打包预览必须清空');
  assert.equal(context.PK.allTot, null, '切换后账号/会话统计必须清空');
  assert.equal(context.MODAL.kind, '');
  assert.equal(context.busyOn, false);
  assert.equal(SK.cache, null, '切换后技能列表的 cache 指纹必须清空');
  assert.equal(SK.items.length, 0, '切换后技能列表必须清空');
  assert.deepEqual(srcReloads.sort(), ['accounts','skills'], '切换后必须重新拉源机账号与技能');

  // 4) 重复点击同一版本不再发请求
  saves.length = 0;
  context.switchVersion('intl');
  assert.equal(saves.length, 0);
  assert.match(toasts.at(-1).m, /当前已经是国际版/);

  // 5) 后端拒绝（目标版本没安装）时给出原因且不改状态
  const before = context.S.cache;
  reply = {ok:false, field:'cache', msg:'这里没有 workbuddy.db，不像是系统缓存目录'};
  context.switchVersion('cn');
  await new Promise(r => setImmediate(r));
  assert.equal(toasts.at(-1).bad, true);
  assert.match(toasts.at(-1).m, /workbuddy\.db/);
  assert.match(toasts.at(-1).m, /系统缓存目录/);
  assert.equal(context.S.cache, before, '失败时不能改动当前路径');

  // 6) 编辑态收起切换控件，避免误触
  context.editing.src.cache = true;
  context.renderPaths();
  assert.equal(hosts['ver-host-src'].innerHTML, '');
  assert.match(hosts['ver-host-dst'].innerHTML, /国内版/);

  console.log('Version switch: 版本判断、标题行落位、默认路径换算、paths/save、账号列表重拉、重复点击、失败回退、编辑态收起全部通过');
}

async function lateResponses() {
  const requests = [];
  const account = {uid:'shared-uid'};
  const context = vm.createContext({
    PK:{accounts:[account, {uid:'old-second'}], allItems:[]},
    TG:{accounts:[]},
    get:()=>new Promise((resolve,reject)=>{requests.push({resolve,reject})}),
    renderPick:()=>{}, renderTargets:()=>{}, setSt:()=>{}, toast:()=>{},
  });
  for (const name of ['loadAccounts','pickAccount','loadAllPreview','loadTargets']) {
    const start = html.indexOf('function ' + name + '(');
    vm.runInContext(html.slice(start, html.indexOf('\nfunction ', start)), context);
  }
  context.loadAccounts();
  context.pickAccount(account.uid);
  context.loadAllPreview();
  context.loadTargets();
  const source = {uid:account.uid, accounts:[account], sessions:[{id:'new-session'}], allItems:[]};
  const target = {accounts:[account]};
  context.PK = source;
  context.TG = target;
  requests[0].resolve({ok:true, accounts:[{uid:'old'}]});
  requests[1].resolve({ok:true, sessions:[{id:'old-session'}]});
  requests[2].resolve({ok:true, sessions:[]});
  requests[3].reject(new Error('Old request failed'));
  await new Promise(resolve=>setImmediate(resolve));
  assert.deepEqual(source, {uid:account.uid, accounts:[account], sessions:[{id:'new-session'}], allItems:[]});
  assert.deepEqual(target, {accounts:[account]});
  assert.equal(requests.length, 4, '已切换后不继续请求旧版全部打包预览');
}

run().then(lateResponses).catch(error=>{console.error(error);process.exitCode=1;});
