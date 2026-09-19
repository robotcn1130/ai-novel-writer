// 技能库 v6.9 · 前端渲染桩测试
//
// 做法：把 index.html 的 <script> 与 app.js 一起在 vm 里跑，
// 给一个最小 DOM 桩，然后直接调那批技能库函数，断言产出的 HTML。
// 不启浏览器也能抓到「模板字符串跨行」「字段名写错」「undefined 漏到页面」这类问题。

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const APP = fs.readFileSync(path.join(ROOT, 'src_v6/webui/static/app.js'), 'utf8');
const HTML = fs.readFileSync(path.join(ROOT, 'src_v6/webui/static/index.html'), 'utf8');

// 从 index.html 抠出内联 script（工具函数都在里面）
const scripts = [...HTML.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const inline = scripts.join('\n');

const OK = [];
const FAIL = [];
function check(name, cond, extra) {
  (cond ? OK : FAIL).push(name);
  if (!cond) console.log('  FAIL: ' + name + (extra !== undefined ? ' | ' + extra : ''));
}

// ---------- 最小 DOM 桩 ----------
function makeEl(tag) {
  const el = {
    tagName: (tag || 'div').toUpperCase(),
    children: [], _html: '', textContent: '', value: '',
    style: {}, dataset: {}, className: '',
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    appendChild(c) { this.children.push(c); return c; },
    remove() {},
    addEventListener() {},
    querySelector() { return null; },
    querySelectorAll() { return []; },
  };
  Object.defineProperty(el, 'innerHTML', {
    get() { return this._html; },
    set(v) { this._html = String(v); },
  });
  return el;
}

const els = {};
['#modalTitle', '#modalBody', '#modalFoot', '#modal', '#mask', '#toast', '#v-skills',
 '#skPath', '#skErr', '#skErr2', '#skErr3', '#skGErr', '#skFile', '#skFileInfo',
 '#skNewName', '#skScope', '#skGenName', '#skGenScope', '#skGenBody', '#skAsName',
 '#skReErr', '#skBrief', '#skGenLog', '#skTabFolder', '#skTabFile',
 '#skPaneFolder', '#skPaneFile', '#skDelName'].forEach(k => { els[k] = makeEl(); });

const document = {
  querySelector: s => els[s] || null,
  querySelectorAll: () => [],
  createElement: makeEl,
  addEventListener() {},
  body: makeEl('body'),
};

const sandbox = {
  document,
  window: {},
  console,
  setTimeout: () => 0,
  clearTimeout() {},
  localStorage: { getItem: () => null, setItem() {}, removeItem() {} },
  fetch: () => Promise.reject(new Error('no net in test')),
  encodeURIComponent, decodeURIComponent, JSON, Math, String, Number, Array,
  Object, Boolean, Date, RegExp, Promise, Error, isNaN, parseInt, parseFloat,
  FileReader: function () { this.readAsDataURL = () => {}; },
};
sandbox.window = sandbox;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);

try {
  // `let/const` 声明不会挂到 sandbox 上（函数声明会）。用一段尾巴把需要的
  // 变量显式导出来，否则 typeof S 是 undefined。
  vm.runInContext(inline + '\n' + APP
    + '\n;globalThis.__S = (typeof S !== "undefined") ? S : null;',
    sandbox, { filename: 'bundle.js' });
} catch (e) {
  console.log('!! 脚本加载失败:', e.message);
  process.exit(1);
}

const G = sandbox;
check('脚本能整体加载', typeof G.renderSkills === 'function');
check('skActionsCard 存在', typeof G.skActionsCard === 'function');
check('skImportConfirm 存在', typeof G.skImportConfirm === 'function');
check('skGenPreview 存在', typeof G.skGenPreview === 'function');
check('skDetail 存在', typeof G.skDetail === 'function');
check('skRenameAsk 存在', typeof G.skRenameAsk === 'function');

// ---------- skInit 默认值 ----------
const k0 = G.skInit();
check('skInit 有 local/slots', Array.isArray(k0.local) && Array.isArray(k0.slots), Object.keys(k0));
check('skInit maxInject 有默认值', k0.maxInject === 8000, k0.maxInject);

// ---------- skActionsCard：两个新入口必须在 ----------
const k = G.skInit();
k.dirs = { user: 'C:/u/.workbuddy/skills', project: 'E:/p/.workbuddy/skills' };
const actions = G.skActionsCard(k);
check('actions 有「导入本地技能」', actions.includes('导入本地技能'), actions);
check('actions 有「让 AI 写一个」', actions.includes('让 AI 写一个'), actions);
check('actions 调 skImportAsk', actions.includes('skImportAsk()'));
check('actions 调 skGenAsk', actions.includes('skGenAsk()'));
check('actions 无 undefined 漏出', !actions.includes('undefined'), actions);

// ---------- skLocalCard：引用小字与详情按钮 ----------
k.local = [
  { name: 'a-skill', dir: 'a-skill', scope: 'user', files: 3, body_chars: 1200,
    description: '说明 X', refs: [{ path: 'docs/x.md', line: 3 }] },
  { name: 'b-skill', dir: 'b-skill', scope: 'project', files: 1, body_chars: 9000,
    description: '', refs: [] },
];
const local = G.skLocalCard(k);
check('local 列出两个技能', local.includes('a-skill') && local.includes('b-skill'));
check('local 显示引用条数', local.includes('1 处在用它'), local.slice(0, 400));
check('local 有详情按钮', local.includes('skDetail('), local);
check('local 超长正文有提示',
      local.includes('超注入上限'), local);
check('local 空说明有兜底', local.includes('（没写说明）'));
check('local 无 undefined', !local.includes('undefined'));

// ---------- 导入确认弹层：BLOCK 命中要红框 + 危险按钮 ----------
const planBlock = {
  plan_id: 'p1', dir: '坏技能', file_count: 2,
  file_list: ['SKILL.md', 'run.sh'],
  meta: { description: '危险的' , body_chars: 100 },
  audit: { block: [{ file: 'SKILL.md', why: '管道执行远程脚本' }], warn: [] },
};
G.skImportConfirm(planBlock);
const bodyBlock = els['#modalBody'].innerHTML;
const footBlock = els['#modalFoot'].innerHTML;
check('确认弹层显示技能名', bodyBlock.includes('坏技能'));
check('BLOCK 时说明危险指令', bodyBlock.includes('这个技能里有危险指令'), bodyBlock.slice(0, 300));
check('BLOCK 时列出命中文件', bodyBlock.includes('SKILL.md') && bodyBlock.includes('管道执行远程脚本'));
check('BLOCK 时按钮变危险', footBlock.includes('我知道风险，仍然导入'), footBlock);
check('BLOCK 时 force=true', footBlock.includes('skImportCommit(true)'), footBlock);
check('确认弹层有改名框', bodyBlock.includes('id="skNewName"'), bodyBlock);
check('确认弹层有作用域选择', bodyBlock.includes('id="skScope"'));

// 无命中时：绿字 + 普通按钮
G.skImportConfirm({ plan_id: 'p2', dir: '好技能', file_count: 1, file_list: ['SKILL.md'],
  meta: { description: 'ok', body_chars: 10 }, audit: { block: [], warn: [] } });
const bodyOk = els['#modalBody'].innerHTML;
const footOk = els['#modalFoot'].innerHTML;
check('干净时说明审计通过', bodyOk.includes('审计通过'), bodyOk.slice(0, 300));
check('干净时按钮是普通确认', footOk.includes('确认导入') && !footOk.includes('我知道风险'), footOk);
check('干净时 force=false', footOk.includes('skImportCommit(false)'), footOk);

// WARN 命中
G.skImportConfirm({ plan_id: 'p3', dir: 'w', file_count: 1, file_list: ['SKILL.md'],
  meta: {}, audit: { block: [], warn: [{ file: 'SKILL.md', why: '会跑 subprocess' }] } });
const bodyWarn = els['#modalBody'].innerHTML;
check('WARN 时提示会执行脚本', bodyWarn.includes('会执行脚本或装包'), bodyWarn.slice(0, 300));
check('WARN 时列出命中原因', bodyWarn.includes('会跑 subprocess'));
check('WARN 不算 BLOCK（按钮仍普通）',
      els['#modalFoot'].innerHTML.includes('skImportCommit(false)'));

// ---------- 导入弹层本体 ----------
G.skImportAsk();
const askBody = els['#modalBody'].innerHTML;
check('导入弹层有文件夹页签', askBody.includes('skTabFolder'));
check('导入弹层有文件页签', askBody.includes('skTabFile'));
check('导入弹层有路径输入框', askBody.includes('id="skPath"'), askBody.slice(0, 400));
check('导入弹层有文件选择框', askBody.includes('id="skFile"'));
check('导入弹层提示三种格式', askBody.includes('zip') && askBody.includes('SKILL.md'));
check('导入弹层初始化 skI', G.__S && G.__S.skI && G.__S.skI.kind === 'folder',
      G.__S && G.__S.skI);

// ---------- AI 草稿预览：可编辑 + 改名 + 正文 ----------
k.maxInject = 8000;
G.skGenPreview({ dir: 'ai-skill', name: 'ai-skill', body_chars: 500,
  content: '---\nname: ai-skill\ndescription: 测试\n---\n\n正文\n',
  description: '测试' });
const genBody = els['#modalBody'].innerHTML;
const genFoot = els['#modalFoot'].innerHTML;
check('草稿弹层有可编辑 textarea', genBody.includes('id="skGenBody"'), genBody.slice(0, 300));
check('草稿弹层有技能名输入框', genBody.includes('id="skGenName"'));
check('草稿弹层有作用域', genBody.includes('id="skGenScope"'));
check('草稿弹层显示字数', genBody.includes('500 字'));
check('草稿弹层有保存按钮', genFoot.includes('skGenSave()'), genFoot);
check('草稿弹层有再写一版', genFoot.includes('skGenAgain()'), genFoot);
check('草稿正文被注入', genBody.includes('description: 测试'), genBody.slice(0, 600));

// 超长草稿要提示会被截断
G.skGenPreview({ dir: 'long', name: 'long', body_chars: 12000,
  content: '---\nname: long\n---\n\n' + 'x'.repeat(50) });
const genLong = els['#modalBody'].innerHTML;
check('超长草稿提示截断', genLong.includes('超过 8000 字注入上限'), genLong.slice(0, 400));

// ---------- 改名弹层 ----------
G.skRenameAsk(0, { name: 'dup' }, '「dup」已经存在了');
const reBody = els['#modalBody'].innerHTML;
const reFoot = els['#modalFoot'].innerHTML;
check('改名单层回显原因', reBody.includes('已经存在了'), reBody.slice(0, 300));
check('改名单层预填 -2', reBody.includes('value="dup-2"'), reBody.slice(0, 400));
check('改名单层按钮调 skRenameGo', reFoot.includes('skRenameGo(0)'), reFoot);

// ---------- AI 提问弹层 ----------
G.skGenAsk();
const gAsk = els['#modalBody'].innerHTML;
check('AI 弹层有需求输入框', gAsk.includes('skBrief'), gAsk.slice(0, 300));
check('AI 弹层说明不会直接落盘', gAsk.includes('不会直接落盘'), gAsk);
check('AI 弹层有示例', gAsk.includes('例如'), gAsk);

// ---------- 无 undefined 泄漏（全量扫一遍） ----------
[['local', local], ['actions', actions], ['导入弹层', askBody], ['确认弹层', bodyBlock],
 ['草稿弹层', genBody], ['改名弹层', reBody], ['AI 弹层', gAsk]]
  .forEach(([nm, html]) => {
    const bad = /\bundefined\b|\bNaN\b|\[object Object\]/.test(html);
    check('「' + nm + '」无 undefined/NaN', !bad, (html.match(/undefined|NaN|\[object Object\]/) || [])[0]);
  });

// ---------- 事件卡：画布摘要按句收 ----------
// 推演产出常常是一整段流水，按"字"切会把句子拦腰砍断（读到一半没了）。
check('treeHead 存在', typeof G.treeHead === 'function');
check('treeHead 短文本原样返回', G.treeHead('短句子。', 60) === '短句子。');

const HS = '黄昏，全家福便利店。郝算拿着一张没装订的临时用工登记表进来，'
  + '说市里在补录临时用工，签个字每月多一笔登记补贴。她要郑半帧在表上签字，'
  + '表是折着的，只露出一角。';
const h1 = G.treeHead(HS, 60);
check('treeHead 收在句末', h1.endsWith('。'), h1);
check('treeHead 不超限', h1.length <= 60, h1.length);
check('treeHead 有整句就不硬切', !h1.endsWith('…'), h1);
check('treeHead 至少两句（第一句太短不单独成行）',
      (h1.match(/。/g) || []).length >= 2, h1);

const h2 = G.treeHead('这是一个中间没有任何句号所以只能硬切的长句子' + '啊'.repeat(60), 30);
check('treeHead 单句超长时退回按字切', h2.length <= 31 && h2.endsWith('…'), h2);

// ---------- 事件卡：长描述先收起 ----------
check('treeDescHtml 存在', typeof G.treeDescHtml === 'function');
const dShort = G.treeDescHtml({ id: 1, description: '一句话。' });
check('短描述原样显示', dShort.includes('一句话。'), dShort);
check('短描述不给展开按钮', !dShort.includes('展开全文'), dShort);

const LONGD = '很久以前发生过一件事，'.repeat(12) + '。';
const dLong = G.treeDescHtml({ id: 9001, description: LONGD });
check('长描述默认收起', dLong.includes('展开全文'), dLong.slice(0, 120));
check('长描述标出全文字数', dLong.includes('（' + LONGD.length + ' 字）'), dLong.slice(0, 120));
check('长描述以省略号结尾', dLong.includes('…'), dLong.slice(0, 200));
check('长描述不吐出全文', !dLong.includes(LONGD), dLong.slice(0, 200));

check('空描述给占位', G.treeDescHtml({ id: 2, description: '' }).includes('没写描述'));

// 展开 → 状态翻转 + 全文出现
G.__S.tree = { path: [], nodes: [{ id: 9001, kind: 'event', description: LONGD,
  title: 'x', card: {}, involved_characters: [] }], can_commit: false };
G.__S.treeDescOpen = {};
G.treeDescToggle(9001);
check('toggle 翻转成展开', G.__S.treeDescOpen[9001] === true, G.__S.treeDescOpen);

// ---------- 事件卡：结构化三行 ----------
check('treeFieldsHtml 存在', typeof G.treeFieldsHtml === 'function');
const fe = G.treeFieldsHtml({ card: { action: '去了店里', intent: '拿签字',
  result: '没拿到', opens: ['第九行还空着', '他还不知道'] } });
check('渲染「做了什么」', fe.includes('做了什么') && fe.includes('去了店里'), fe);
check('渲染「为什么」', fe.includes('为什么') && fe.includes('拿签字'), fe);
check('渲染「结果」', fe.includes('结果') && fe.includes('没拿到'), fe);
check('渲染悬置线索', fe.includes('悬置') && fe.includes('第九行还空着 · 他还不知道'), fe);
check('空 card 不渲染任何东西', G.treeFieldsHtml({}) === '');
check('缺 opens 不炸', G.treeFieldsHtml({ card: { result: 'R' } }).includes('R'));

// ---------- 详情卡整体：三行摆上来 + description 收起 ----------
// 这正是用户抱怨的那个场景：一段 200 多字的流水铺满整张卡。
els['#treeDetail'] = makeEl();
const NODE = {
  id: 9001, parent_id: 9000, kind: 'event', title: '调度局的女科员上门要签字',
  description: LONGD, event_type: 'conflict', importance: 4, actor_name: '郝算',
  trigger_type: '', trigger_label: '', stakes: '', status: 'explored',
  status_label: '看过', branch_hint: '', world_time: '霜月十五',
  involved_characters: ['郝算', '郑半帧'], involved_entities: [],
  event_ref: null, branch_id: 0, tick: 2, has_delta: false, delta_keys: [],
  state_delta: {}, fact_count: 0, knowledge_count: 0, delta_applied: false,
  card: { action: '带着登记表到便利店，让她在第九行签字',
          intent: '赶在审计前补齐所缺的人头签字',
          result: '对方没签，用"身份证没带"拖住',
          opens: ['第九行还空着'] },
};
G.__S.tree = { path: [9001], nodes: [NODE], can_commit: false };
G.__S.treeDescOpen = {};
G.paintTreeDetail(NODE);
const dc = els['#treeDetail'].innerHTML;
check('详情卡有结构化三行', dc.includes('efields'), dc.slice(0, 200));
check('详情卡显示做了什么', dc.includes('带着登记表到便利店'), dc.slice(0, 400));
check('详情卡显示结果', dc.includes('对方没签'), dc.slice(0, 400));
check('详情卡提示悬置', dc.includes('第九行还空着'), dc.slice(0, 500));
check('详情卡把长描述收起来', dc.includes('展开全文'), dc.slice(0, 300));
check('详情卡不铺满原文', !dc.includes(LONGD), dc.slice(0, 300));
check('详情卡无 undefined/NaN', !/\bundefined\b|\bNaN\b|\[object Object\]/.test(dc),
      (dc.match(/undefined|NaN|\[object Object\]/) || [])[0]);

// 展开之后全文才出现
G.treeDescToggle(9001);
const dc2 = els['#treeDetail'].innerHTML;
check('展开后吐全文', dc2.includes(LONGD), dc2.slice(0, 120));
check('展开后按钮变收起', dc2.includes('收起'), dc2.slice(0, 200));

console.log('\n=== OK=' + OK.length + ' FAIL=' + FAIL.length + ' ===');
FAIL.forEach(f => console.log(' FAIL: ' + f));
process.exit(FAIL.length ? 1 : 0);
