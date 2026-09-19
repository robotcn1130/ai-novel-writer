// 技能库 · 绑定交互测试
//
// 钉死一个真实发生过的死锁：复选框和下拉各自提交，用户顺表格从左往右操作
// （先选技能、再勾开关）时，选技能那一步带着 enabled=false 提交，后端
// save_binding 见「not enabled」把整条绑定删掉 → 提示「已停用」→ 恢复成
// 「不使用」；反过来先勾开关又被守卫弹回。两条路都不通 = 启用不了任何技能。
//
// 做法同 t_render.js：index.html 内联 script + app.js 一起丢进 vm，
// 给最小 DOM 桩，mock 掉 api / loadSkills / render / toast，然后直接调
// skillsBindRow 断言「发了什么请求」。

const fs = require('fs');
const path = require('path');
const vm = require('vm');

const ROOT = path.resolve(__dirname, '..');
const APP = fs.readFileSync(path.join(ROOT, 'src_v6/webui/static/app.js'), 'utf8');
const HTML = fs.readFileSync(path.join(ROOT, 'src_v6/webui/static/index.html'), 'utf8');
const scripts = [...HTML.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
const inline = scripts.join('\n');

const OK = [];
const FAIL = [];
function check(name, cond, extra) {
  (cond ? OK : FAIL).push(name);
  if (!cond) console.log('  FAIL: ' + name + (extra !== undefined ? ' | ' + extra : ''));
}

function makeEl(tag) {
  const el = {
    tagName: (tag || 'div').toUpperCase(),
    children: [], _html: '', textContent: '', value: '', checked: false,
    style: {}, dataset: {}, className: '',
    classList: { add() {}, remove() {}, toggle() {}, contains: () => false },
    appendChild(c) { this.children.push(c); return c; },
    remove() {}, addEventListener() {},
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
['#modalTitle', '#modalBody', '#modalFoot', '#modal', '#mask', '#toast',
 '#v-skills'].forEach(k => { els[k] = makeEl(); });

const document = {
  querySelector: s => els[s] || null,
  querySelectorAll: () => [],
  createElement: makeEl,
  addEventListener() {},
  body: makeEl('body'),
};

const sandbox = {
  document, window: {}, console,
  setTimeout: () => 0, clearTimeout() {},
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
  vm.runInContext(inline + '\n' + APP
    + '\n;globalThis.__S = (typeof S !== "undefined") ? S : null;',
    sandbox, { filename: 'bundle.js' });
} catch (e) {
  console.log('!! 脚本加载失败:', e.message);
  process.exit(1);
}

const G = sandbox;

// ---------- mock 掉网络与重绘 ----------
// 只记绑定这一个接口：页面 boot() 是异步跑的（/api/schema、/api/meta），
// 会把无关请求混进 calls，让「提交了几次」这类断言变成时序噪声。
const calls = [];
const toasts = [];
G.api = (p, o) => {
  if (p === '/api/skills/bind') calls.push({ p, body: (o || {}).body });
  return Promise.resolve({ ok: true });
};
G.loadSkills = async () => {};
G.render = () => {};
G.refreshView = async () => {};
G.toast = (m, kind) => { toasts.push({ m, kind }); };
function reset() { calls.length = 0; toasts.length = 0; }
// 取第 n 次绑定请求的 body。没发请求时给空对象，让断言正常判 FAIL
// 而不是 "Cannot read properties of undefined" 把整轮测试打断。
function sent(n) { return (calls[n] || {}).body || {}; }

// ---------- 素材：世界构建这一行 ----------
const k = G.skInit();
function slotState(o) {
  k.slots = [Object.assign({
    slot: 'world_init', label: '世界构建', hint: '按题材生成世界草案',
    skill: '', enabled: false, missing: false,
    full_chars: 0, inject_chars: 0,
  }, o || {})];
}
k.local = [
  { name: 'my-skill', dir: 'my-skill', scope: 'user', files: 1, body_chars: 100 },
  { name: 'p-skill', dir: 'p-skill', scope: 'project', files: 1, body_chars: 200 },
];
k.maxInject = 8000;
k.keepSel = {};

// ---------- 1. 渲染：两个控件必须带上触发源 ----------
slotState();
const html0 = G.skBindCard(k);
check('开关 onchange 带 on 来源', html0.includes("skillsBindRow(0,'on')"),
      html0.slice(0, 900));
check('下拉 onchange 带 sel 来源', html0.includes("skillsBindRow(0,'sel')"),
      html0.slice(0, 900));
check('渲染无 undefined 漏出', !html0.includes('undefined'));
check('未启用时不显示「没启用」小字（因为还没选）',
      !html0.includes('但没启用'));

// ---------- 2. 复现用户操作：先在拉里选技能（开关此时是关的）----------
(async () => {
  reset();
  slotState();
  els['#skOn_0'] = makeEl('input'); els['#skOn_0'].checked = false;
  els['#skSel_0'] = makeEl('select'); els['#skSel_0'].value = 'my-skill';

  await G.skillsBindRow(0, 'sel');
  check('★ 选技能后提交的是「启用」而非「停用」',
        calls.length === 1 && sent(0).enabled === true,
        JSON.stringify(calls));
  check('提交的技能名正确', sent(0).skill === 'my-skill');
  check('提交的调用点正确', sent(0).slot === 'world_init');
  check('开关被自动勾上（用户不用再点一次）',
        els['#skOn_0'].checked === true);
  check('提示是「已启用」而非「已停用」',
        toasts.some(t => /已启用/.test(t.m)) && !toasts.some(t => /已停用/.test(t.m)),
        JSON.stringify(toasts));
  check('提示带档位名', toasts.some(t => t.m.includes('世界构建')), JSON.stringify(toasts));

  // ---------- 3. 换绑：已启用状态下改选另一个技能 ----------
  reset();
  slotState({ skill: 'my-skill', enabled: true, full_chars: 100, inject_chars: 100 });
  els['#skOn_0'].checked = true;
  els['#skSel_0'].value = 'p-skill';
  await G.skillsBindRow(0, 'sel');
  check('换绑也是「启用」', sent(0).enabled === true && sent(0).skill === 'p-skill',
        JSON.stringify(calls));

  // ---------- 4. 下拉选回「不使用」= 停用 ----------
  reset();
  slotState({ skill: 'my-skill', enabled: true, full_chars: 100, inject_chars: 100 });
  els['#skOn_0'].checked = true;
  els['#skSel_0'].value = '';
  await G.skillsBindRow(0, 'sel');
  check('选「不使用」提交停用', sent(0).enabled === false, JSON.stringify(calls));
  check('停用会同步取消勾选', els['#skOn_0'].checked === false);
  check('选「不使用」不留回显', !k.keepSel.world_init, JSON.stringify(k.keepSel));

  // ---------- 5. 取消勾选：停用但保留下拉回显 ----------
  reset();
  slotState({ skill: 'my-skill', enabled: true, full_chars: 100, inject_chars: 100 });
  els['#skOn_0'].checked = false;
  els['#skSel_0'].value = 'my-skill';
  await G.skillsBindRow(0, 'on');
  check('取消勾选 = 停用', sent(0).enabled === false, JSON.stringify(calls));
  check('停用后记住下拉选择', k.keepSel.world_init === 'my-skill',
        JSON.stringify(k.keepSel));

  // 后端此时已把条目删掉，重绘要能把选择回显出来
  slotState();                       // skill='' enabled=false，模拟后端返回
  const html1 = G.skBindCard(k);
  check('停用后下拉仍回显上次选的技能',
        html1.includes('value="my-skill" selected') || html1.includes('selected>my-skill'),
        html1.slice(0, 1200));
  check('停用但已选时给出「没启用」提示', html1.includes('但没启用'),
        html1.slice(0, 1200));

  // ---------- 6. 只勾开关不选技能：拦下且不写库 ----------
  reset();
  slotState();
  k.keepSel = {};
  els['#skOn_0'].checked = true;
  els['#skSel_0'].value = '';
  await G.skillsBindRow(0, 'on');
  check('没选技能就勾开关会被拦下（不发请求）', calls.length === 0,
        JSON.stringify(calls));
  check('拦下时给出提示', toasts.some(t => /先选一个技能/.test(t.m)),
        JSON.stringify(toasts));
  check('拦下时开关弹回', els['#skOn_0'].checked === false);

  // ---------- 7. 技能被卸载（missing）：不该说「勾上开关才生效」 ----------
  slotState({ skill: 'gone', enabled: false, missing: true });
  k.keepSel = {};
  const html2 = G.skBindCard(k);
  check('missing 时提示已不在本地', html2.includes('已不在本地'), html2.slice(0, 1200));
  check('missing 时不说「勾上开关才生效」', !html2.includes('但没启用'));

  // ---------- 汇总 ----------
  console.log('\n=== OK=' + OK.length + ' FAIL=' + FAIL.length + ' ===');
  FAIL.forEach(f => console.log(' FAIL: ' + f));
  process.exit(FAIL.length ? 1 : 0);
})();
