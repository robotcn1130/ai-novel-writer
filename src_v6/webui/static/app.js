/* ============================================================
   v6.0 四视图渲染 · 世界 / 决策台 / 章节 / 设置
   ============================================================ */

/* ------------------------------------------------------------
   通用确认弹层

   不要用原生 confirm()：浏览器在用户连点几次后会「阻止此页面创建
   更多对话框」，此后 confirm 静默返回 false —— 表现为「点删除没
   任何反应」，且不报错、不留痕、极难自查。弹层里自带常驻错误区，
   失败时保持打开并把原因显示出来。
   ------------------------------------------------------------ */
let _confirmCb = null;

function confirmDialog(title, bodyHtml, okLabel, onOk) {
  _confirmCb = onOk;
  openModal(title, `${bodyHtml}
    <div id="confirmErr" class="small" style="display:none;background:var(--bad-soft);
      color:var(--bad);border-radius:8px;padding:8px 11px;margin-bottom:10px;
      word-break:break-all"></div>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn pri danger" id="confirmOk"
       onclick="runConfirm()">${esc(okLabel || '确认')}</button>`, true);
}

async function runConfirm() {
  const cb = _confirmCb;
  if (!cb) return;
  const btn = $('#confirmOk');
  if (btn) { btn.disabled = true; }
  try {
    await cb();
    closeModal();
  } catch (e) {
    const box = $('#confirmErr');
    if (box) { box.textContent = e.message; box.style.display = 'block'; }
    if (btn) { btn.disabled = false; }
  }
}


/* ------------------------------------------------------------
   视图一：世界
   ------------------------------------------------------------ */
/* 落定时停在岔路口留下的"待决点"——写正文前后都能回来定 */
function openDecisionsCard() {
  const ds = S.openDecisions || [];
  if (!ds.length) return '';
  return `
    <div class="card" style="margin-bottom:16px;border-color:#e7d5b2">
      <h3>待决岔路 <span class="spacer"></span>
        <span class="tag warn">${ds.length} 个还没定</span></h3>
      <div class="body">
        <p class="dim small" style="margin:0 0 12px">
          这几个岔路口是在推演沙盘上<b>落定时还没拍板</b>的，先给你留着了。
          它们会影响后面的推演和正文，定了就不会再变；
          也可以回推演沙盘上补选，效果一样。</p>
        ${ds.map(d => `
          <div style="padding:10px 0;border-bottom:1px solid var(--line-2)">
            <div style="font-weight:620;margin-bottom:4px">
              ${esc(d.title)}
              ${d.trigger_label ? `<span class="tag warn">${esc(d.trigger_label)}</span>` : ''}</div>
            <div class="small dim" style="margin-bottom:6px">${nl2br(d.situation || '')}</div>
            ${d.stakes ? `<div class="small dim" style="margin-bottom:8px">
              代价：${esc(d.stakes)}</div>` : ''}
            <div class="row wrap">
              ${(d.options || []).map((o, i) => `<button class="btn sm"
                onclick="pickOpenDecision(${d.id},${i})"
                title="${esc(o.description || '')}">${i + 1}. ${esc(o.label)}</button>`).join('')}
            </div>
          </div>`).join('')}
      </div>
    </div>`;
}

function pickOpenDecision(did, idx) {
  const d = (S.openDecisions || []).find(x => x.id === did) || {};
  const o = (d.options || [])[idx] || {};
  confirmDialog('定下这个岔路口？', `<p style="margin-top:0">
      <b>${esc(d.title || '')}</b></p>
    <p>你选的是：<b>${esc(o.label || '')}</b></p>
    <p class="dim small" style="margin-bottom:0">${nl2br(o.description || '')}
      ${o.consequence_hint ? '<br>走向：' + esc(o.consequence_hint) : ''}</p>
    <p class="dim small">定了之后，后面的推演和正文都会按这个走。</p>`,
    '就选它', async () => {
      try {
        await api('/api/decision/resolve', { method: 'POST',
          body: { id: did, option_index: idx } });
        toast('已定', 'ok');
        await refreshView('world'); render();
      } catch (e) { toast(e.message, 'err'); }
    });
}

/* ---------------------------------------------------------------- 正史账本（v6.7）
   用户要的："正史只有一个写入口，投影只有一个落库点，正文永远不能覆盖正史。"

   前端对应的三件事：
   1. 顶部状态条——tick / 正史事实数 / 规则数 / 待裁决候选 / 未裁决冲突，
      一眼看到"这世界的账本有多厚、有没有欠账"。
   2. 账本抽屉——正史事实 / 世界规则 / 待裁决候选 三个页签，
      事实可以手工作废、规则可以增删、候选可以采信或驳回。
   3. 一个**只读**的提醒：这里不提供"直接改世界状态"的入口，
      因为状态是正史的投影，要改就改正史（否则下次推演就把它盖回去）。
   ------------------------------------------------------------ */
const FACT_TYPE_LABEL = {
  identity: '身份', status: '存活状态', location: '位置', possession: '持有',
  relationship: '关系', ability: '能力', injury: '伤势', resource: '资源',
  world_rule: '世界规则', outcome: '结果', other: '其他',
};
const FACT_STATUS_LABEL = {
  canonical: '正史', derived: '推得', provisional: '暂定',
};
const SOURCE_KIND_LABEL = {
  sim: '推演', prose: '正文', user: '作者', init: '初始化',
};
const CLASSIFY_LABEL = {
  world: '世界事实', cognition: '角色认知', unverified: '未经证实',
  dream: '梦境', memory: '回忆', author_claim: '作者宣告',
};

function canonStatBar() {
  const c = S.canon;
  if (!c) return '';
  const tick = c.tick || 0;
  const conflicts = c.open_conflicts || 0;
  const cands = c.pending_candidates || 0;
  return `
    <div class="card" style="margin-bottom:16px;border-color:#d8e0ea">
      <div class="body" style="padding:11px 16px">
        <div class="row wrap" style="gap:14px;align-items:center">
          <span class="small"><b>第 ${tick} tick</b>
            <span class="dim">（${esc(c.display_time || '时间未设定')}）</span></span>
          <span class="small">正史事实 <b>${c.facts || 0}</b> 条</span>
          <span class="small">世界规则 <b>${c.rules || 0}</b> 条</span>
          <span class="small">知识条目 <b>${c.knowledge || 0}</b> 条</span>
          ${cands ? `<span class="tag warn">${cands} 条候选待裁决</span>` : ''}
          ${conflicts ? `<span class="tag bad">${conflicts} 处未裁决正文冲突</span>` : ''}
          <span class="spacer" style="flex:1"></span>
          <button class="btn sm" onclick="toggleCanon()">
            ${S.canonOpen ? '收起' : '打开'}正史账本</button>
          <button class="btn sm ghost" onclick="rebuildCanon()">重建账本</button>
        </div>
        <p class="dim small" style="margin:8px 0 0">
          <b>这里是全书的唯一事实来源。</b>世界状态是它的投影缓存；
          推演落定时写它，正文回流只会"提请裁决"、不会覆盖它。
          改设定请改这里——直接改世界状态页的值，下次推演就会被盖回去。</p>
      </div>
    </div>`;
}

function toggleCanon() {
  S.canonOpen = !S.canonOpen;
  try { localStorage.setItem('nw_canon_open', S.canonOpen ? '1' : '0'); } catch (e) {}
  render();
}

function canonLedgerCard() {
  if (!S.canonOpen || !S.canon) return '';
  const tab = S.canonTab || 'facts';
  const tabBtn = (k, label, n) => `<button class="btn sm ${tab === k ? 'pri' : ''}"
    onclick="setCanonTab('${k}')">${label}${n ? ' ' + n : ''}</button>`;
  let body = '';
  if (tab === 'facts') body = canonFactsHtml();
  else if (tab === 'rules') body = canonRulesHtml();
  else body = canonCandidatesHtml();
  return `
    <div class="card" style="margin-bottom:16px">
      <h3>正史账本 <span class="spacer"></span>
        <span class="dim small" style="font-weight:400">
          supersede 而非删除：旧事实永远留着，可回溯</span></h3>
      <div class="body">
        <div class="row wrap" style="margin-bottom:12px">
          ${tabBtn('facts', '正史事实', S.canonFacts.length)}
          ${tabBtn('rules', '世界规则', S.canonRules.length)}
          ${tabBtn('candidates', '待裁决', S.canonCandidates.length)}
          <span class="spacer" style="flex:1"></span>
          ${tab === 'facts' ? '<button class="btn sm" onclick="openFactEdit()">＋ 写一条事实</button>' : ''}
          ${tab === 'rules' ? '<button class="btn sm" onclick="openRuleEdit()">＋ 加一条规则</button>' : ''}
        </div>
        ${body}
      </div>
    </div>`;
}

function setCanonTab(k) { S.canonTab = k; render(); }

function canonFactsHtml() {
  if (!S.canonFacts.length) {
    return `<div class="empty">还没有正史事实。<br>
      推演落定、正文回流、或你手工写一条，都会进这里。</div>`;
  }
  return `<div class="body tight" style="padding:0">
    <table class="t"><thead><tr>
      <th>主体</th><th>内容</th><th>类型</th><th>来源</th><th>tick</th><th></th>
    </tr></thead><tbody>
    ${S.canonFacts.map(f => `<tr>
      <td><b>${esc(f.subject_name || '—')}</b>
        <div class="dim small">${esc(f.subject_type || '')}</div></td>
      <td><span class="mono">${esc(f.predicate || '')}</span>
        ${f.object_text ? ' = <b>' + esc(f.object_text) + '</b>' : ''}
        ${f.quote ? '<div class="dim small">「' + esc(f.quote) + '」</div>' : ''}</td>
      <td><span class="tag">${esc(FACT_TYPE_LABEL[f.fact_type] || f.fact_type || '')}</span>
        ${f.fact_status !== 'canonical'
          ? '<div><span class="tag warn">' +
            esc(FACT_STATUS_LABEL[f.fact_status] || f.fact_status) + '</span></div>' : ''}</td>
      <td class="small">${esc(SOURCE_KIND_LABEL[f.source_kind] || f.source_kind || '')}
        ${f.source_chapter ? '<div class="dim small">第' + f.source_chapter + '章</div>' : ''}</td>
      <td class="mono small">${f.valid_from_tick || 0}</td>
      <td style="text-align:right">
        <button class="btn sm ghost danger"
          onclick="revokeFact(${f.id})">作废</button></td>
    </tr>`).join('')}</tbody></table></div>`;
}

function canonRulesHtml() {
  if (!S.canonRules.length) {
    return `<div class="empty">还没有世界规则。<br>
      规则是"这个世界怎么运作"的硬约束，比如「死人不会复活」
      「异常实体无法被普通人直视」——推演时会被摆给模型当铁律。</div>`;
  }
  return `<ul class="list">${S.canonRules.map(r => `
    <li>
      <div class="main">
        <div class="title">${esc(r.content)}
          <span class="tag">${esc(r.scope || 'global')}</span>
          ${r.severity === 'error' ? '<span class="tag bad">硬规则</span>'
            : '<span class="tag warn">软规则</span>'}
          ${r.is_hard ? '<span class="tag bad">不可违反</span>' : ''}</div>
        ${r.subject_name ? '<div class="sub">限定对象：' + esc(r.subject_name) + '</div>' : ''}
        ${r.check_hint ? '<div class="sub dim">判据：' + esc(r.check_hint) + '</div>' : ''}
      </div>
      <div class="acts"><button class="btn sm ghost danger"
        onclick="delRule(${r.id})">删</button></div>
    </li>`).join('')}</ul>`;
}

function canonCandidatesHtml() {
  // 这段说明**始终要显示**，不能只放在空态里：
  // 「无冲突 ≠ 是真的」是这一层存在的理由，用户看不懂它就会一路点"确认为正史"，
  // 把角色的一场梦变成世界真相——那正是约束 2 要防的事。
  const why = `<p class="dim small" style="margin:0 0 10px">
    正文里写的"未经证实 / 梦境 / 回忆 / 作者宣告"会落到这里等你拍板，
    确认是真的才进正史——<b>"没跟已知的打架"不等于"是真的"</b>。
    它们**没跟正史比对过**，所以需要你判断。</p>`;
  if (!S.canonCandidates.length) {
    return why + `<div class="empty">当前没有待裁决的候选事实。</div>`;
  }
  return why + S.canonCandidates.map(c => `
    <div style="padding:11px 0;border-bottom:1px solid var(--line-2)">
      <div style="margin-bottom:5px">
        <span class="tag ${c.classification === 'memory' || c.classification === 'dream'
          ? 'warn' : ''}">${esc(CLASSIFY_LABEL[c.classification] || c.classification || '')}</span>
        <b>${esc(c.subject_name || '—')}</b>
        <span class="mono">${esc(c.predicate || '')}</span>
        ${c.object_text ? ' = ' + esc(c.object_text) : ''}
      </div>
      ${c.quote ? `<div class="small dim" style="margin-bottom:4px">「${esc(c.quote)}」</div>` : ''}
      <div class="small dim" style="margin-bottom:8px">
        来源：${esc(SOURCE_KIND_LABEL[c.source_kind] || c.source_kind || '')}
        ${c.source_chapter ? '第' + c.source_chapter + '章' : ''}
        ${c.provenance ? '｜' + esc(c.provenance) : ''}
        ${c.classify_reason ? '｜' + esc(c.classify_reason) : ''}</div>
      <div class="row wrap">
        <button class="btn sm pri"
          onclick="resolveCandidate(${c.id},'accept')">确认为正史</button>
        <button class="btn sm"
          onclick="resolveCandidate(${c.id},'reject')">驳回</button>
      </div>
    </div>`).join('');
}

async function revokeFact(id) {
  confirmDialog('作废这条正史事实？', `<p style="margin-top:0">
      作废不是删除——这条会标成"已撤销"，仍然留在账本里可回溯。</p>
    <p class="dim small" style="margin-bottom:0">
      注意：作废事实<b>不会</b>自动回滚已经投影出去的世界状态，
      也可能让后续章节变得"无依据"。作废后请检查世界状态页。</p>`,
    '就作废', async () => {
      await api('/api/canon/fact', { method: 'POST',
        body: { novel_id: S.novelId, op: 'revoke', fact_id: id } });
      toast('已作废', 'ok');
      await refreshView('world'); render();
    });
}

function openFactEdit() {
  openModal('写一条正史事实', `
    <p class="dim small" style="margin-top:0">
      主体 + 谓词 + 值，就是一条事实。比如 主体「老周」谓词「存活」值「false」。
      写进来就是正史，后续推演与正文都必须遵守它。</p>
    <div class="grid g2">
      ${field('主体类型', 'ft_subject_type', 'character')}
      ${field('主体名称', 'ft_subject_name', '')}
    </div>
    <div class="grid g2">
      ${field('谓词', 'ft_predicate', '')}
      ${field('值', 'ft_object_text', '')}
    </div>
    <div class="grid g2">
      ${field('类型', 'ft_fact_type', 'status')}
      ${field('值类型', 'ft_object_type', 'text')}
    </div>
    <p class="dim small" style="margin:8px 0 0;line-height:1.7">
      <b>状态类事实的键 = 主体名.谓词</b>，例：主体「异常实体」+ 谓词「剩余数量」
      → <span class="mono">异常实体.剩余数量</span>。<br>
      世界状态页显示的键就是这个形状。<b>键写歪了不会报错</b>，
      只会在状态页多出一条新记录、原来那条不动 —— 改之前先去状态页对一下键名。</p>
    <div id="factErr" class="small" style="display:none;background:var(--bad-soft);
      color:var(--bad);border-radius:8px;padding:8px 11px;margin-top:8px"></div>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn pri" id="factOk" onclick="doFactEdit()">写入正史</button>`);
}

async function doFactEdit() {
  const btn = $('#factOk');
  const err = $('#factErr');
  const v = id => ($('#' + id) && $('#' + id).value.trim()) || '';
  if (err) err.style.display = 'none';
  if (!v('ft_predicate')) {
    if (err) { err.textContent = '谓词不能为空'; err.style.display = 'block'; }
    return;
  }
  if (btn) btn.disabled = true;
  try {
    await api('/api/canon/fact', { method: 'POST', body: {
      novel_id: S.novelId, op: 'add',
      subject_type: v('ft_subject_type') || 'world',
      subject_name: v('ft_subject_name'),
      predicate: v('ft_predicate'),
      object_text: v('ft_object_text'),
      fact_type: v('ft_fact_type') || 'other',
      object_type: v('ft_object_type') || 'text',
    } });
    closeModal(); toast('已写入正史', 'ok');
    await refreshView('world'); render();
  } catch (e) {
    if (err) { err.textContent = e.message; err.style.display = 'block'; }
    if (btn) btn.disabled = false;
  }
}

function openRuleEdit() {
  openModal('加一条世界规则', `
    <p class="dim small" style="margin-top:0">
      规则是这个世界怎么运作的硬约束，推演时会被当成铁律摆给模型。
      写成一句陈述句：「死人不会复活」。</p>
    ${field('规则内容', 'rule_content', '')}
    <div class="grid g2">
      ${field('适用范围', 'rule_scope', 'global')}
      ${field('严重度（error＝不可违反）', 'rule_severity', 'error')}
    </div>
    ${field('校验判据（可选，给校验器看的可操作标准）', 'rule_hint', '',
            '例：正文出现已 death 角色开口说话 → 违反')}
    <div id="ruleErr" class="small" style="display:none;background:var(--bad-soft);
      color:var(--bad);border-radius:8px;padding:8px 11px;margin-top:8px"></div>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn pri" id="ruleOk" onclick="doRuleEdit()">加上</button>`);
}

async function doRuleEdit() {
  const btn = $('#ruleOk');
  const err = $('#ruleErr');
  const content = (($('#rule_content') || {}).value || '').trim();
  if (err) err.style.display = 'none';
  if (!content) {
    if (err) { err.textContent = '规则内容不能为空'; err.style.display = 'block'; }
    return;
  }
  if (btn) btn.disabled = true;
  try {
    await api('/api/canon/rules', { method: 'POST', body: {
      novel_id: S.novelId, content: content,
      scope: (($('#rule_scope') || {}).value || 'global').trim(),
      severity: (($('#rule_severity') || {}).value || 'error').trim(),
      check_hint: (($('#rule_hint') || {}).value || '').trim(),
    } });
    closeModal(); toast('规则已加上', 'ok');
    await refreshView('world'); render();
  } catch (e) {
    if (err) { err.textContent = e.message; err.style.display = 'block'; }
    if (btn) btn.disabled = false;
  }
}

async function delRule(id) {
  confirmDialog('删掉这条世界规则？', '<p style="margin-top:0">删掉后推演不再受它约束。</p>',
    '删掉', async () => {
      // HTTP 层没有 DELETE 方法（dispatch 只认 GET/POST），删走 op=delete
      await api('/api/canon/rules', { method: 'POST',
        body: { novel_id: S.novelId, op: 'delete', rule_id: id } });
      toast('已删除', 'ok');
      await refreshView('world'); render();
    });
}

async function resolveCandidate(id, action) {
  const c = (S.canonCandidates || []).find(x => x.id === id) || {};
  if (action === 'reject') {
    await api('/api/canon/candidate/resolve', { method: 'POST',
      body: { novel_id: S.novelId, candidate_id: id, action: 'reject' } })
      .then(() => toast('已驳回', 'ok'))
      .catch(e => toast(e.message, 'err'));
    await refreshView('world'); render();
    return;
  }
  confirmDialog('确认为正史？', `<p style="margin-top:0">
      <b>${esc(c.subject_name || '')}</b>
      <span class="mono">${esc(c.predicate || '')}</span>
      ${c.object_text ? ' = ' + esc(c.object_text) : ''}</p>
    ${c.quote ? `<p class="dim small">原文：「${esc(c.quote)}」</p>` : ''}
    <p class="dim small" style="margin-bottom:0">
      确认后它会进正史账本，后续推演与正文都必须遵守。
      ${c.classification === 'memory' || c.classification === 'dream'
        ? '<br><b>提醒</b>：这条的分类是"'
          + esc(CLASSIFY_LABEL[c.classification]) + '"，'
          + '如果它只是角色的回忆或梦境、并非客观发生的事，'
          + '不该确认为正史——应驳回，让它留在认知层。' : ''}</p>`,
    '确认为正史', async () => {
      await api('/api/canon/candidate/resolve', { method: 'POST',
        body: { novel_id: S.novelId, candidate_id: id, action: 'accept' } });
      toast('已进正史', 'ok');
      await refreshView('world'); render();
    });
}

async function rebuildCanon() {
  confirmDialog('从正史事件重建账本？', `<p style="margin-top:0">
      按已落定的 <b>events</b> 反推一遍正史事实。</p>
    <p class="dim small" style="margin-bottom:0">
      这条路是给<b>老库迁移后</b>用的：迁移时没有自动投影（老事件的结构化字段
      可能是空/半截，硬投影会造出<b>错误的正史</b>——比没有正史更糟）。
      重建出来的事实全部标成"推得"，可以整批撤销重来。<br>
      已经手工确认过的正史不会被覆盖。</p>`,
    '开始重建', async () => {
      closeModal();
      render();                       // 先把日志容器画出来，再往里写
      await runJob('/api/canon/rebuild', { novel_id: S.novelId },
        'canonLog', '重建正史账本', async () => {
          toast('重建完成', 'ok');
          await refreshView('world'); render();
        });
    });
}

function renderWorld() {
  const n = S.novel || {};
  const st = S.stats || {};
  const clock = S.clock || {};
  const cur = n.current_chapter || 0;

  const shapeLabel = k => (S.meta.shape_labels || {})[k] || k || '—';
  const modeLabel = k => (S.meta.modes || {})[k] || k || '—';

  const html = `
    <div class="stats" style="margin-bottom:16px">
      <div class="stat"><div class="k">世界时间</div>
        <div class="v" style="font-size:15px">${esc(clock.current_time || '未设定')}</div>
        <div class="k" style="margin-top:3px">推进 ${clock.total_ticks || 0} 次</div></div>
      <div class="stat"><div class="k">章节</div><div class="v">${cur}
        <small>章</small></div></div>
      <div class="stat"><div class="k">角色</div>
        <div class="v">${S.characters.length}</div></div>
      <div class="stat"><div class="k">世界实体</div>
        <div class="v">${S.entities.length}</div></div>
      <div class="stat"><div class="k">活跃线索</div><div class="v">${S.threads.length}</div>
        <div class="k" style="margin-top:3px">${(S.urgent || []).length
          ? '<span class="tag warn">' + (S.urgent || []).length + ' 条急需回收</span>'
          : '都在掌控中'}</div></div>
      <div class="stat"><div class="k">人机分工</div>
        <div class="v" style="font-size:14px">${esc(modeLabel(n.decision_mode))}</div></div>
    </div>

    ${canonStatBar()}
    ${canonLedgerCard()}
    <div id="canonLog" style="margin-bottom:16px"></div>

    <div class="grid g2" style="margin-bottom:16px">
      <div class="card">
        <h3>世界观 <span class="spacer"></span>
          <button class="btn sm" onclick="openNovelEdit()">编辑设定</button>
          <button class="btn sm ghost danger" onclick="openDeleteNovel()">删除</button></h3>
        <div class="body">
          <div class="kv">
            <span class="k">前提</span><span>${nl2br(n.premise || '（未设定）')}</span>
            <span class="k">核心张力</span><span>${nl2br(n.initial_tension || '（未设定）')}</span>
            <span class="k">题材</span><span>${esc(n.genre || '—')}　
              基调 ${esc(n.tone || '—')}</span>
            <span class="k">目标篇幅</span><span>每章约 ${n.target_words_per_chapter || 3000} 字</span>
          </div>
        </div>
      </div>

      <div class="card">
        <h3>推进世界 <span class="spacer"></span>
          <button class="btn sm" onclick="go('tree')">去推演</button></h3>
        <div class="body">
          <p class="dim small" style="margin:0 0 10px">
            推演在<b>「推演」页</b>的分支沙盘上做：一次推出一段剧情，遇到岔路口停下来让你挑，
            挑定一条线再「落定成正文」。这里显示的是已经落定的世界状态。</p>
          <div class="row wrap">
            <button class="btn pri" onclick="go('tree')">打开推演沙盘</button>
            <button class="btn" onclick="openTimeline()">时间线锚点</button>
          </div>
          <div id="advanceLog" style="margin-top:12px"></div>
        </div>
      </div>
    </div>

    ${openDecisionsCard()}

    <div class="grid g2" style="margin-bottom:16px">
      <div class="card">
        <h3>世界状态 <span class="spacer"></span>
          <button class="btn sm" onclick="openStateEdit()">＋ 状态</button></h3>
        <div class="body tight">
          ${S.state.length ? `<table class="t"><thead><tr>
            <th>分类</th><th>状态项</th><th>当前值</th><th></th></tr></thead><tbody>
            ${S.state.map(s => `<tr>
              <td><span class="tag">${esc(s.category || 'other')}</span></td>
              <td><b>${esc(s.key)}</b>${s.note
                ? '<div class="dim small">' + esc(s.note) + '</div>' : ''}</td>
              <td class="mono">${esc(fmtVal(s.value))}
                <span class="dim" style="font-size:11px">${esc(s.value_type || '')}</span></td>
              <td style="text-align:right"><button class="btn sm ghost"
                onclick="openStateHistory('${esc(s.key)}')">历史</button></td>
            </tr>`).join('')}</tbody></table>`
            : `<div class="empty">还没有世界状态量。<br>比如「异常实体.剩余数量 = 3」
               「公众知情度 = 12%」</div>`}
        </div>
      </div>

      <div class="card">
        <h3>世界实体 <span class="spacer"></span>
          <button class="btn sm" onclick="openEntityEdit()">＋ 实体</button></h3>
        <div class="body tight">
          ${S.entities.length ? `<ul class="list">${S.entities.map(e => `
            <li>
              <div class="main">
                <div class="title">${esc(e.name)}
                  <span class="tag">${esc(e.entity_type)}</span>
                  ${e.visibility === 'hidden' ? '<span class="tag warn">隐藏</span>' : ''}
                  ${e.status !== 'active' ? '<span class="tag bad">' + esc(e.status) + '</span>' : ''}
                  <span class="dim small">威胁 ${e.power_level}/5</span></div>
                <div class="sub">${esc(e.description || '（无描述）')}</div>
              </div>
              <div class="acts"><button class="btn sm ghost"
                onclick="openEntityEdit(${e.id})">改</button></div>
            </li>`).join('')}</ul>`
            : '<div class="empty">还没有世界实体（组织/地点/物品/现象）。</div>'}
        </div>
      </div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <h3>角色 <span class="spacer"></span>
        <span class="dim small" style="font-weight:400">托管模式决定推演时谁做决定</span>
        <button class="btn sm" onclick="setAllControl()">批量设定</button>
        <button class="btn sm" onclick="openCharEdit()">＋ 角色</button>
        <button class="btn sm" onclick="openCastFromWorld()">＋ 组建阵容</button></h3>
      <div class="body tight">
        ${S.characters.length ? `<ul class="list">${S.characters.map(c => `
          <li>
            <div class="main">
              <div class="title">${esc(c.name)}
                <span class="tag ${c.rank === 'A' ? 'accent' : ''}">${esc(c.rank)}级</span>
                ${c.role_tag ? '<span class="tag">' + esc(c.role_tag) + '</span>' : ''}
                ${ctrlTag(c.control_mode)}
                ${c.status !== 'alive' ? '<span class="tag bad">' + esc(c.status) + '</span>' : ''}
              </div>
              <div class="sub">
                ${c.current_location ? '位于 ' + esc(c.current_location) + ' ｜ ' : ''}
                ${c.personality ? esc(c.personality) : '（性格未定义）'}
              </div>
              ${(c.goals_long.length || c.goals_short.length) ? `<div class="sub"
                style="margin-top:4px">
                ${c.goals_long.map(g => '<span class="tag info">长期 ' + esc(g) + '</span>').join(' ')}
                ${c.goals_short.map(g => '<span class="tag ok">短期 ' + esc(g) + '</span>').join(' ')}
              </div>` : '<div class="sub dim">（暂无目标，可随事件临时生成）</div>'}
            </div>
            <div class="acts">
              <button class="btn sm ghost" onclick="openCharDetail(${c.id})">详情</button>
              <button class="btn sm ghost" onclick="openCharEdit(${c.id})">改</button>
            </div>
          </li>`).join('')}</ul>`
          : `<div class="empty">还没有角色。点上方「＋ 组建阵容」让 AI 出一套互相咬合的
              角色，或手动加一个——没有人带着目标进场，推演只会每轮即兴捏一批人。</div>`}
      </div>
    </div>

    <div class="card">
      <h3>活跃线索 <span class="spacer"></span>
        <button class="btn sm" onclick="openThreadEdit()">＋ 线索</button></h3>
      <div class="body tight">
        ${S.threads.length ? `<table class="t"><thead><tr>
          <th>线索</th><th>张力</th><th>埋于</th><th>计划回收</th><th></th>
          </tr></thead><tbody>
          ${S.threads.map(t => `<tr>
            <td><b>${esc(t.title)}</b>
              ${t.description ? '<div class="dim small">' + esc(t.description) + '</div>' : ''}</td>
            <td>${tensionBar(t.tension_level)}</td>
            <td>${t.planted_chapter ? '第' + t.planted_chapter + '章' : '—'}</td>
            <td>${t.target_chapter ? '第' + t.target_chapter + '章' : '未定'}</td>
            <td style="text-align:right">
              <button class="btn sm ghost" onclick="nudgeThread(${t.id},1)">+张力</button>
              <button class="btn sm ghost" onclick="resolveThread(${t.id})">回收</button></td>
          </tr>`).join('')}</tbody></table>`
          : '<div class="empty">没有活跃线索。线索会在推演中自动涌现，也可以手动埋。</div>'}
      </div>
    </div>

    ${worldMapCard()}

    ${worldRelationsCard()}
  `;
  $('#v-world').innerHTML = html;
}

/* ---------------------------------------------------------------- 角色地图
   用户要的："有剧情发生的空间就详细到什么房间，没有剧情发生的，
   可以模糊到某个建筑或者某个城市；别人都不知道这个人在什么具体位置。"

   所以这里做两件事：
   1. 按地点树分组摆人，精度决定这个人画在哪一级（房间 → 楼栋 → 城市）。
   2. 可以切到"某个角色的视角"，看**他以为**谁在哪（秘密行踪就在这显形）。

   位置不会凭空出现：树是推演/正文里写到哪长到哪，没写到的层级不编。 */
function worldMapCard() {
  const m = S.map;
  if (!m) return '';
  const viewer = m.viewer || 0;
  const chars = (S.characters || []).filter(c => c.status !== 'dead');
  const LV = { city: '城市', district: '街区', building: '建筑', floor: '楼层',
               room: '房间', site: '地点', other: '其他', unknown: '不明' };
  // 只有真有人的节点才画（空节点是地名簿，不是地图）
  const live = (m.all_nodes || []).filter(n => n.members.length || n.believed.length);
  const byParent = {};
  live.forEach(n => {
    const k = n.parent_id == null ? 0 : n.parent_id;
    (byParent[k] = byParent[k] || []).push(n);
  });
  const nameOf = id => {
    const n = (m.all_nodes || []).find(x => x.id === id);
    return n ? n.name : '';
  };
  // 自底向上拼一条"旧城区 · 本部 · 地下二层"的路径，给没有子节点的显示
  const trailOf = n => {
    const parts = [n.name];
    let p = n.parent_id, guard = 0;
    while (p != null && guard++ < 8) {
      parts.unshift(nameOf(p));
      const pn = (m.all_nodes || []).find(x => x.id === p);
      p = pn ? pn.parent_id : null;
    }
    return parts.join(' · ');
  };
  const chip = (c, belief) => `
    <span class="tag ${belief ? 'warn' : ''}"
      title="${esc(belief ? '（此认知可能已过时）' : '')}"
      style="margin:2px 4px 2px 0;display:inline-block">
      ${esc(c.name)}<span class="dim" style="font-size:10px;margin-left:3px">${
        esc(LV[c.precision] || c.precision || '')}</span></span>`;

  const blocks = [];
  const walk = (pid, depth) => {
    (byParent[pid] || []).sort((a, b) => a.id - b.id).forEach(n => {
      const kids = byParent[n.id] || [];
      const indent = depth ? 'margin-left:' + (depth * 16) + 'px;' : '';
      blocks.push(`
        <div style="${indent}padding:6px 0">
          <div><b>${esc(n.name)}</b>
            <span class="tag info">${esc(LV[n.level] || n.level || '地点')}</span>
            ${n.is_secret ? '<span class="tag bad">隐秘</span>' : ''}
            ${!kids.length && !viewer ? `<span class="dim small">${esc(trailOf(n))}</span>` : ''}
          </div>
          <div style="margin-top:3px">
            ${n.members.map(c => chip(c, false)).join('')}
            ${viewer ? n.believed.map(c => chip(c, true)).join('') : ''}
            ${!n.members.length && !n.believed.length
              ? '<span class="dim small">（无人）</span>' : ''}
          </div>
        </div>`);
      walk(n.id, depth + 1);
    });
  };
  walk(0, 0);

  const unknown = m.unknown || [];
  const loose = m.loose || [];
  return `
    <div class="card" style="margin-top:16px">
      <h3>角色地图 <span class="spacer"></span>
        <span class="dim small" style="font-weight:400">位置精度由剧情决定：演到哪写到哪</span>
        <select id="mapViewer" onchange="setMapViewer(this.value)"
          style="max-width:190px">
          <option value="0"${!viewer ? ' selected' : ''}>上帝视角（真实位置）</option>
          ${chars.map(c => `<option value="${c.id}"${viewer === c.id
            ? ' selected' : ''}>${esc(c.name)} 的视角</option>`).join('')}
        </select></h3>
      <div class="body tight">
        <p class="dim small" style="margin:0 0 8px">
          ${viewer
            ? `现在显示 <b>${esc(m.viewer_name)}</b> 掌握的行踪——他以为谁在哪。
               <span class="tag warn">黄标</span>是听说/推断的，未必是实情。`
            : `按地点分组显示每个人现在的真实位置。切到某个角色的视角，
               就能看到"他以为谁在哪"——秘密行踪正是靠这里露出马脚。`}
        </p>
        ${blocks.length ? blocks.join('')
          : `<div class="empty">地图还没有内容。<br>
              地点树随推演和正文生长：事件里写到「调查局本部 地下二层 封锁库」，
              树上就会长出这四级。没写到的位置不会编。</div>`}
        ${loose.length ? `
          <div style="margin-top:10px;border-top:1px dashed var(--line);padding-top:8px">
            <b>只知地点名，未接入地点树</b>
            <span class="dim small">（老数据只有文字位置，跑一次迁移会补上树）</span>
            <div style="margin-top:4px">
              ${loose.map(l => `<span class="tag" style="margin:2px 4px 0 0">
                ${esc(l.who)}<span class="dim small"> · ${esc(l.text)}</span></span>`
              ).join('')}
            </div>
          </div>` : ''}
        ${unknown.length ? `
          <div style="margin-top:10px;border-top:1px dashed var(--line);padding-top:8px">
            <b>行踪不明</b>
            <span class="dim small">（没演到具体位置，或本人刻意隐藏）</span>
            <div style="margin-top:4px">
              ${unknown.map(c => `<span class="tag" style="margin:2px 4px 0 0">
                ${esc(c.name)}</span>`).join('')}
            </div>
          </div>` : ''}
      </div>
    </div>`;
}

function setMapViewer(v) {
  S.mapViewer = parseInt(v, 10) || 0;
  loadMap().then(() => renderWorld()).catch(e => toast(e.message, 'err'));
}

function loadMap() {
  const nid = S.novelId || (S.novel || {}).id;
  if (!nid) { S.map = null; return Promise.resolve(); }
  const v = S.mapViewer || 0;
  return api(`/api/map?novel_id=${nid}${v ? '&viewer=' + v : ''}`)
    .then(d => { S.map = d; });
}

/* 人物关系网（世界页）。
   没有这一块时，关系表是死数据：AI 不知道谁认识谁，
   推演里陌生人一见面就像老友，正文里的态度也只能靠猜。
   双向关系是两条库记录，展示时按 (小 id, 大 id, 类型) 去重。 */
function worldRelationsCard() {
  const all = S.relations || [];
  // 归组口径必须与后端 db.group_relations 一致：双向关系在库里是两条
  // （A→B、B→A），单向只有一条。这里按 (min_id, max_id, type) 归组后，
  // 组内出现两个不同方向 = 双向，显示 ↔；只有一个方向 = 单向，显示 →
  // 并标注"对方不知情"——否则用户看不出这条关系其实只有一边。
  const groups = {};
  const order = [];
  all.forEach(r => {
    const k = [Math.min(r.character_a_id, r.character_b_id),
               Math.max(r.character_a_id, r.character_b_id),
               r.relation_type].join('|');
    if (!groups[k]) { groups[k] = []; order.push(k); }
    groups[k].push(r);
  });
  const rows = order.map(k => {
    const g = groups[k];
    const base = g.slice().sort((x, y) => x.id - y.id)[0];
    const dirs = {};
    g.forEach(r => { dirs[r.character_a_id + '>' + r.character_b_id] = 1; });
    return { r: base, mutual: Object.keys(dirs).length >= 2 };
  });
  const chars = S.characters || [];
  const canAdd = chars.length >= 2;
  return `
    <div class="card" style="margin-top:16px">
      <h3>人物关系网 <span class="spacer"></span>
        <span class="dim small" style="font-weight:400">推演与正文都靠它决定谁怎么对待谁</span>
        <button class="btn sm" onclick="openWorldRelationEdit(-1)"
          ${canAdd ? '' : 'disabled'}>＋ 关系</button></h3>
      <div class="body tight">
        ${rows.length ? `<table class="t"><thead><tr>
          <th>关系</th><th>类型</th><th>强度</th><th>状态</th><th></th>
          </tr></thead><tbody>
          ${rows.map(({ r, mutual }) => `<tr>
            <td><b>${esc(r.name_a || '?')}</b> ${mutual ? '↔' : '→'} <b>${esc(r.name_b || '?')}</b>
              ${mutual ? '' : '<span class="tag warn">单向 · 对方不知情</span>'}
              ${r.description ? '<div class="dim small">' + esc(r.description) + '</div>' : ''}</td>
            <td><span class="tag ${r.relation_type === 'enemy' ? 'bad'
              : r.relation_type === 'rival' ? 'warn' : 'info'}">${
              esc(REL_LABELS[r.relation_type] || r.relation_type)}</span></td>
            <td>${r.intensity}/5</td>
            <td><span class="tag ${r.status === 'broken' ? 'bad' : ''}">${
              r.status === 'broken' ? '已破裂' : r.status === 'evolved'
                ? '已变化' : '维持中'}</span></td>
            <td style="text-align:right;white-space:nowrap">
              <button class="btn sm ghost"
                onclick="openWorldRelationEdit(${r.id})">改</button>
              <button class="btn sm ghost"
                onclick="delWorldRelation(${r.id})">删</button></td>
          </tr>`).join('')}</tbody></table>`
          : `<div class="empty">还没有人物关系。<br>
              关系在「组建阵容」时会跟角色一起生成，推演中出现新的羁绊也会自动补进来。<br>
              也可以手动加——没有关系网，模型只能默认这些人互相都认识。</div>`}
      </div>
    </div>`;
}

function openWorldRelationEdit(id) {
  const chars = (S.characters || []).filter(c => c.status !== 'dead');
  if (chars.length < 2) return toast('至少要有两个角色才能建关系', 'err');
  const r = id ? ((S.relations || []).find(x => x.id === id) || {}) : {};
  const opts = sel => chars.map(c =>
    `<option value="${c.id}"${sel === c.id ? ' selected' : ''}>${esc(c.name)}</option>`
  ).join('');
  openModal(id ? '编辑人物关系' : '添加人物关系', `
    <p class="dim small" style="margin-top:0">
      写清"他们之间有过什么、现在什么状态、芥蒂在哪"。
      推演时模型靠这段决定他们见面怎么对待彼此，空标签（"是同事"）没有用。</p>
    <div class="row">
      <div style="flex:1"><label class="f"><span>甲方</span>
        <select id="wrA">${opts(r.character_a_id)}</select></label></div>
      <div style="flex:1"><label class="f"><span>乙方</span>
        <select id="wrB">${opts(r.character_b_id || (chars[1] || {}).id)}</select></label></div>
    </div>
    <div class="row">
      <div style="flex:1"><label class="f"><span>关系类型</span>
        <select id="wrType">${Object.keys(REL_LABELS).map(k =>
          `<option value="${k}"${(r.relation_type || 'colleague') === k
            ? ' selected' : ''}>${REL_LABELS[k]}</option>`).join('')}</select></label></div>
      <div style="flex:none;width:110px"><label class="f"><span>强度 1-5</span>
        <input id="wrIntensity" type="number" min="1" max="5"
          value="${r.intensity || 3}"></label></div>
    </div>
    ${area('关系内容', 'wrDesc', r.description,
      '他们之间发生过什么、现在处什么状态、芥蒂/恩情在哪')}
    ${id ? '' : `<label class="f" style="display:flex;align-items:center;gap:8px">
      <input type="checkbox" id="wrMutual" style="width:auto" checked>
      <span>双向（取消＝单向：一方有这心思，另一方不知道）</span></label>`}`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn pri" onclick="saveWorldRelation(${id || 0})">保存</button>`);
}

async function saveWorldRelation(id) {
  const a = parseInt($('#wrA').value, 10);
  const b = parseInt($('#wrB').value, 10);
  if (!a || !b) return toast('请选择关系双方', 'err');
  if (a === b) return toast('不能给自己建关系', 'err');
  const fields = {
    relation_type: $('#wrType').value,
    intensity: parseInt($('#wrIntensity').value, 10) || 3,
    description: $('#wrDesc').value.trim(),
  };
  try {
    if (id) {
      const r = await api('/api/relations/update', { method: 'POST',
        body: { id, fields } });
      S.relations = r.relations;
    } else {
      const r = await api('/api/relations', { method: 'POST', body: {
        novel_id: S.novelId, a_id: a, b_id: b, ...fields,
        is_mutual: $('#wrMutual') ? $('#wrMutual').checked : true } });
      S.relations = r.relations;
    }
    closeModal(); render(); toast('已保存', 'ok');
  } catch (e) { toast(e.message, 'err'); }
}

async function delWorldRelation(id) {
  confirmDialog('删除这条关系',
    '<p style="margin:0">双向关系会连同反方向那条一起删掉。</p>',
    '删除', async () => {
      const r = await api('/api/relations/delete', { method: 'POST',
        body: { id } });
      S.relations = r.relations;
      render(); toast('已删除', 'ok');
    });
}

function fmtVal(v) {
  if (v === null || v === undefined) return '—';
  if (typeof v === 'object') return JSON.stringify(v);
  return String(v);
}
function tensionBar(lv) {
  lv = parseInt(lv, 10) || 0;
  const color = lv >= 5 ? 'var(--bad)' : lv >= 4 ? 'var(--warn)' : 'var(--ink-3)';
  return `<span class="mono" style="color:${color}">${'●'.repeat(Math.min(lv, 6))}${
    '<span class="dim">' + '○'.repeat(Math.max(0, 6 - lv)) + '</span>'}</span>`;
}
function ctrlTag(m) {
  if (m === 'user') return '<span class="tag accent">你来做主</span>';
  if (m === 'auto_delegate') return '<span class="tag info">AI 代管</span>';
  return '<span class="tag">AI 托管</span>';
}

/* 分支状态条（v6.7）
   沙盘上的节点现在带 *_delta：它们只是"在这条分支上累计的变化"，
   不写库、不影响正史。这条状态栏把这些 delta 的来源讲清楚——
   否则用户看到"沙盘里世界变了、正史却没变"，会以为是 bug。 */
function branchStatHtml() {
  const b = S.branch;
  if (!b) return '';
  const chk = b.check || {};
  const errs = (chk.errors || []).length;
  const warns = (chk.warnings || []).length;
  const n = b.overlay_count || 0;
  return `
    <div style="margin-top:12px;padding:9px 12px;border-radius:9px;
      background:var(--panel-2);border:1px solid var(--line-2)">
      <div class="row wrap small" style="gap:14px;align-items:center">
        <span>分支 <b>#${b.branch_id || 0}</b></span>
        <span class="dim">从第 <b>${b.base_tick || 0}</b> tick 起算</span>
        <span class="dim">累计改动 <b>${n}</b> 个状态量</span>
        <span class="dim">正史事实 <b>${b.canon_facts || 0}</b> 条</span>
        ${errs ? `<span class="tag bad">${errs} 处硬伤</span>` : ''}
        ${warns ? `<span class="tag warn">${warns} 条提醒</span>` : ''}
        ${!errs && !warns ? '<span class="tag ok">校验通过</span>' : ''}
      </div>
      <div class="dim small" style="margin-top:6px">
        沙盘上的状态变化只属于这条分支，<b>不会写进正史</b>——
        只有点「落定成正文」才会把它变成世界的真相。
      </div>
    </div>`;
}

function renderTree() {
  const host = $('#v-tree');
  host.innerHTML = `
    <div class="card" style="margin-bottom:14px">
      <h3>推演沙盘 <span class="spacer"></span>
        <span class="dim small" style="font-weight:400">
          一次推演长一段剧情；遇到岔路口会停下来等你选</span></h3>
      <div class="body">
        <div class="row wrap" style="margin-bottom:10px">
          <input id="treeFocus" type="text" style="flex:1;min-width:240px"
            placeholder="这次想往哪个方向推？（留空就顺着当前局势走）">
          <button class="btn pri" id="treeExpandBtn" onclick="treeExpand()">推演</button>
          <button class="btn" id="treeCommitBtn" onclick="treeCommit()">落定成正文</button>
          <button class="btn danger ghost" onclick="treeReset()">重开一棵树</button>
        </div>
        <div class="stats">
          <div class="stat"><div class="k">节点</div>
            <div class="v" id="treeStatNodes">0</div></div>
          <div class="stat"><div class="k">待落定事件</div>
            <div class="v" id="treeStatPending">0</div></div>
          <div class="stat"><div class="k">岔路口</div>
            <div class="v" id="treeStatDecisions">0</div></div>
          <div class="stat"><div class="k">沙盘字数</div>
            <div class="v" id="treeStatWords">0</div></div>
        </div>
        ${branchStatHtml()}
      </div>
      <div class="body" id="treeLog" style="padding-top:0"></div>
    </div>
    <div class="treesplit">
      <div class="treehost">
        <div class="treewrap" id="treeWrap"></div>
        <div class="treezoom">
          <button class="btn sm ghost" onclick="treeZoom(-1)" title="缩小">−</button>
          <span class="zl" id="treeZoom">100%</span>
          <button class="btn sm ghost" onclick="treeZoom(1)" title="放大">+</button>
          <button class="btn sm ghost" onclick="fitTree(1)"
            title="缩放到刚好装下整棵树">适应</button>
        </div>
        <div class="treehint">空白处按住拖动 · 滚轮缩放 · 双击空白适应窗口</div>
      </div>
      <div class="treedetail" id="treeDetail"></div>
    </div>`;
  bindTreePan();
  paintTree();
}

/* ------------------------------------------------------------
   视图二：推演树（分支剧情沙盘）

   和「世界」页的分工：世界页看已经落定的正史，这里在沙盘上试各种走法。
   一张图从上往下长，遇到岔路口停下来问你，你挑一条才继续往下长。
   最后挑定一条线「落定成正文」，它才变成章节页的素材。
   ------------------------------------------------------------ */

const TREE_W = 214;          // 节点宽
const TREE_GX = 26;          // 横向间隙
const TREE_GY = 78;          // 层间距
const TREE_PAD = 26;
const TREE_H = { root: 84, event: 104, decision: 128, option: 94 };
const KIND_LABEL = { root: '起点', event: '事件', decision: '岔路口', option: '分支' };
const KIND_ICON  = { root: '◆', event: '●', decision: '✦', option: '↳' };

function treeH(n) { return TREE_H[n.kind] || 104; }

/* ------------------------------------------------------------
   画布的平移与缩放

   图是 svg + 绝对定位方块拼的，所以直接给整块 canvas 挂 transform。
   视口状态放在 S.treeView 里（不随树数据重绘而丢），重绘后重新贴一次。
   ------------------------------------------------------------ */
function treeView() {
  if (!S.treeView) S.treeView = { x: 14, y: 14, s: 1, key: '' };
  return S.treeView;
}

function treeCanvas() {
  const wrap = $('#treeWrap');
  return wrap ? wrap.querySelector('.treecanvas') : null;
}

function applyTreeView() {
  const v = treeView();
  const z = $('#treeZoom');
  if (z) z.textContent = Math.round(v.s * 100) + '%';
  const c = treeCanvas();
  if (!c) return;
  c.style.transform = 'translate(' + v.x + 'px,' + v.y + 'px) scale(' + v.s + ')';
}

/* 缩放到整棵树刚好装进视口（只在装不下时才缩小，不倒着放大） */
function fitTree(manual) {
  const wrap = $('#treeWrap'), c = treeCanvas();
  if (!wrap || !c) return false;
  const w = parseFloat(c.style.width) || 0;
  const h = parseFloat(c.style.height) || 0;
  const vw = wrap.clientWidth, vh = wrap.clientHeight;
  if (!w || !h || !vw || !vh) return false;      // 页面还没显示出来，等下次
  const v = treeView();
  v.s = Math.max(0.22, Math.min(1, Math.min((vw - 40) / w, (vh - 40) / h)));
  v.x = Math.max(16, (vw - w * v.s) / 2);
  v.y = 18;
  applyTreeView();
  if (manual) toast('已适应窗口', 'ok');
  return true;
}

/* 树变了（推演多了一段、砍了分支）就重新取景，免得新节点长在视野外 */
function treeFitIfNeeded() {
  const t = S.tree || {};
  const key = S.novelId + ':' + (t.root || 0) + ':' + ((t.nodes || []).length);
  const v = treeView();
  if (v.key === key) return;
  if (fitTree()) v.key = key;
}

function treeZoom(dir) {
  const wrap = $('#treeWrap');
  const v = treeView();
  const cx = wrap ? wrap.clientWidth / 2 : 0;
  const cy = wrap ? wrap.clientHeight / 2 : 0;
  const next = Math.max(0.22, Math.min(2.5, v.s * (dir > 0 ? 1.2 : 1 / 1.2)));
  if (Math.abs(next - v.s) < 1e-4) return;
  const k = next / v.s;                          // 以视口中心为锚点
  v.x = cx - (cx - v.x) * k;
  v.y = cy - (cy - v.y) * k;
  v.s = next;
  applyTreeView();
}

/* 左键拖空白平移，滚轮以指针为锚点缩放，双击空白适应窗口。
   绑在 treeWrap 上；treeWrap 只在切换视图时重建，paintTree 只换它的内容。 */
function bindTreePan() {
  const wrap = $('#treeWrap');
  if (!wrap || wrap.dataset.panBound) return;
  wrap.dataset.panBound = '1';

  let sx = 0, sy = 0, ox = 0, oy = 0, down = false, moved = false, onNode = false;
  let hitId = 0;                                // 按下时踩到的节点 id

  wrap.addEventListener('pointerdown', e => {
    if (e.button !== 0) return;                  // 只认左键
    /* v6.8：treeWrap 内部**也可能有按钮**——树空着时那块引导文案里就有一个
       「开始推演」。下面这行 setPointerCapture 会把后续 pointer 事件全部改派到
       wrap 上，于是 pointerup 不再落在按钮上，浏览器就不合成 click，
       按钮的 inline onclick 永远不触发：表现是「点了没反应」，且不报错。

       同理，按钮上的拖动也不该被解读成"拖画布"。
       → 交互元素（button/input/select/textarea/a/[data-no-pan]）一律直接放行，
         既不捕获指针，也不标记 dragging。 */
    if (e.target.closest && e.target.closest(
        'button,input,select,textarea,a,[data-no-pan]')) return;

    down = true; moved = false;
    S.treeDragJustNow = false;                   // 新手势开始，清掉上次的拖动痕迹
    const nd = e.target.closest ? e.target.closest('.tnode') : null;
    onNode = !!nd;
    hitId = nd ? Number(nd.getAttribute('data-id') || 0) : 0;
    sx = e.clientX; sy = e.clientY;
    const v = treeView(); ox = v.x; oy = v.y;
    wrap.classList.add('dragging');
    try { wrap.setPointerCapture(e.pointerId); } catch (_) {}
  });

  wrap.addEventListener('pointermove', e => {
    if (!down) return;
    const dx = e.clientX - sx, dy = e.clientY - sy;
    if (!moved) {
      // 在节点上按下时留一点容差：想点选结果手抖几下，不算拖动
      if (Math.abs(dx) + Math.abs(dy) < (onNode ? 6 : 2)) return;
      moved = true;
    }
    const v = treeView();
    v.x = ox + dx; v.y = oy + dy;
    applyTreeView();
  });

  function endDrag(e) {
    if (!down) return;
    down = false;
    wrap.classList.remove('dragging');
    try { wrap.releasePointerCapture(e.pointerId); } catch (_) {}
    // 记下这次是不是"拖过"：随后的 click 无论落到容器还是卡片上，treeSelect 都会忽略它。
    // （指针捕获会把 click 改派到容器上，所以不能靠"click 只会在没拖过时发生"来推断。）
    S.treeDragJustNow = moved;
    if (!moved && hitId) treeSelect(hitId);       // 点选：自己判定，不等 click
    hitId = 0;
  }
  wrap.addEventListener('pointerup', endDrag);
  wrap.addEventListener('pointercancel', endDrag);

  wrap.addEventListener('wheel', e => {
    e.preventDefault();
    let d = e.deltaY;
    if (e.deltaMode === 1) d *= 16;              // 按行滚
    else if (e.deltaMode === 2) d *= 400;        // 按页滚
    const v = treeView();
    const next = Math.max(0.22, Math.min(2.5, v.s * Math.exp(-d * 0.0015)));
    if (Math.abs(next - v.s) < 1e-4) return;
    const r = wrap.getBoundingClientRect();
    const mx = e.clientX - r.left, my = e.clientY - r.top;
    const k = next / v.s;                        // 以鼠标位置为锚点，不跑偏
    v.x = mx - (mx - v.x) * k;
    v.y = my - (my - v.y) * k;
    v.s = next;
    applyTreeView();
  }, { passive: false });

  wrap.addEventListener('dblclick', e => {
    // 双击节点不算"双击空白"（指针捕获下 target 会变成 wrap，所以看 onNode）
    if (onNode) return;
    if (e.target.closest && e.target.closest('.tnode')) return;
    fitTree();
  });
}

/* 自上而下的 org-chart 布局：叶子依次占一个槽位，父节点居中于它的子节点之上 */
function treeLayout(nodes, rootId) {
  const kids = {}, byId = {};
  nodes.forEach(n => { byId[n.id] = n; kids[n.id] = []; });
  nodes.forEach(n => { if (n.parent_id && byId[n.parent_id]) kids[n.parent_id].push(n); });
  Object.keys(kids).forEach(k => kids[k].sort((a, b) => (a.seq - b.seq) || (a.id - b.id)));

  const lv = {};
  nodes.forEach(n => { (lv[n.depth] = lv[n.depth] || []).push(n); });
  const depths = Object.keys(lv).map(Number).sort((a, b) => a - b);
  const lvY = {};
  let acc = TREE_PAD;
  depths.forEach(d => {
    lvY[d] = acc;
    acc += Math.max.apply(null, lv[d].map(treeH)) + TREE_GY;
  });

  const sx = {};
  let slot = 0;
  (function walk(id) {
    const cs = kids[id] || [];
    if (!cs.length) { sx[id] = slot++; return; }
    cs.forEach(c => walk(c.id));
    const xs = cs.map(c => sx[c.id]);
    sx[id] = (Math.min.apply(null, xs) + Math.max.apply(null, xs)) / 2;
  })(rootId);

  let maxX = 0;
  const lvH = {};
  depths.forEach(d => { lvH[d] = Math.max.apply(null, lv[d].map(treeH)); });
  nodes.forEach(n => {
    if (sx[n.id] === undefined) sx[n.id] = slot++;
    maxX = Math.max(maxX, sx[n.id]);
    n._h = treeH(n);
    n._x = TREE_PAD + sx[n.id] * (TREE_W + TREE_GX);
    n._y = lvY[n.depth] + (lvH[n.depth] - n._h) / 2;
  });
  return {
    w: TREE_PAD * 2 + (maxX + 1) * (TREE_W + TREE_GX) - TREE_GX,
    h: acc - TREE_GY + TREE_PAD,
  };
}

function treeEdges(nodes, onPath) {
  const byId = {};
  nodes.forEach(n => { byId[n.id] = n; });
  return nodes.filter(n => n.parent_id && byId[n.parent_id]).map(n => {
    const p = byId[n.parent_id];
    const px = p._x + TREE_W / 2, py = p._y + p._h;
    const cx = n._x + TREE_W / 2, cy = n._y;
    const my = (py + cy) / 2;
    const live = onPath.has(n.id) && onPath.has(p.id);
    return `<path d="M${px} ${py} C${px} ${my} ${cx} ${my} ${cx} ${cy}"
      fill="none" stroke="${live ? 'var(--accent)' : 'var(--ink-3)'}"
      stroke-width="${live ? 2 : 1.3}" stroke-dasharray="${live ? 'none' : '4 4'}"
      opacity="${live ? .8 : .38}"/>`;
  }).join('');
}

/* 画布上的小方块只放得下一两行。按"字"切会把句子拦腰砍断（读到一半没了），
   所以先按句号凑，凑到六成就收在句末；整句凑不出来再退回按字切。 */
function treeHead(s, limit) {
  s = String(s || '').replace(/\s+/g, ' ').trim();
  if (s.length <= limit) return s;
  const parts = s.match(/[^。！？]+[。！？]+/g) || [];
  let out = '';
  for (const p of parts) {
    if (out && out.length + p.length > limit) break;
    out += p;
    if (out.length >= limit * 0.6) break;
  }
  if (out.length >= limit * 0.6) return out;
  return s.slice(0, limit).replace(/[，、；,]\s*$/, '') + '…';
}

/* 卡片上的描述。推演产出经常是一整段两百多字的流水（模型把「做了什么 / 想要什么 /
   结果」全揉进了 description），直接铺出来很压眼睛。长的一律先收起，
   想细看再展开 —— 不替用户删内容，但先给个能扫的骨架。 */
function treeDescHtml(n) {
  const d = String(n.description || '');
  if (!d) return '<div class="small dim">（这张卡没写描述）</div>';
  const LIMIT = 110;
  if (d.length <= LIMIT) {
    return `<div class="small" style="line-height:1.8">${nl2br(d)}</div>`;
  }
  const open = !!(S.treeDescOpen || {})[n.id];
  const shown = open ? d
    : d.slice(0, LIMIT).replace(/[，、；,]\s*$/, '') + '…';
  return `<div class="small" style="line-height:1.8">${nl2br(shown)}</div>
    <button class="btn sm ghost" style="padding:2px 6px;margin-top:3px"
      onclick="treeDescToggle(${n.id})">${
      open ? '收起' : '展开全文（' + d.length + ' 字）'}</button>`;
}

function treeDescToggle(id) {
  S.treeDescOpen = S.treeDescOpen || {};
  S.treeDescOpen[id] = !S.treeDescOpen[id];
  const n = (S.tree.nodes || []).find(x => x.id === id);
  if (n) paintTreeDetail(n);
}

/* 结构化三行。模型本来就把「做了什么 / 想要什么 / 结果 / 悬置线索」分开写了，
   只是以前没送到页面上 —— 于是它只能把这些塞进 description 里，写成一段流水。
   这三行比一整段散句好读得多，也不需要在卡片上复述一遍。 */
function treeFieldsHtml(n) {
  const c = n.card || {};
  const rows = [['做了什么', c.action], ['为什么', c.intent], ['结果', c.result]]
    .filter(r => String(r[1] || '').trim());
  const opens = (c.opens || []).filter(x => String(x || '').trim());
  if (!rows.length && !opens.length) return '';
  return '<dl class="efields">'
    + rows.map(r => `<dt>${esc(r[0])}</dt><dd>${esc(r[1])}</dd>`).join('')
    + (opens.length
        ? `<dt>悬置</dt><dd>${esc(opens.join(' · '))}</dd>` : '')
    + '</dl>';
}

function treeNodeHtml(n, onPath) {
  const cls = ['tnode', 'k-' + n.kind];
  if (onPath.has(n.id)) cls.push('onpath'); else cls.push('offpath');
  if (n.event_ref) cls.push('committed');
  if (n.kind === 'decision' && !n.event_ref) cls.push('pending');
  if (S.treeSel === n.id) cls.push('sel');
  const badge = n.kind === 'decision' ? '待你定'
    : n.kind === 'option' ? (n.status === 'explored' ? '看过' : '未走')
    : n.event_ref ? '已落定' : (onPath.has(n.id) ? '这条线上' : '未走');
  return `<div class="${cls.join(' ')}" style="left:${n._x}px;top:${n._y}px;
      width:${TREE_W}px;height:${n._h}px" data-id="${n.id}"
      onclick="treeSelect(${n.id})" title="${esc(n.title)}">
    <div class="tk"><span>${KIND_ICON[n.kind] || '●'}</span>
      <span>${KIND_LABEL[n.kind] || ''}</span>
      <span class="spacer" style="flex:1"></span>
      <span class="dim">${esc(badge)}</span></div>
    <div class="tt">${esc(n.title)}</div>
    <div class="td">${esc(treeHead(n.description || n.stakes || '', 60))}</div>
  </div>`;
}

function paintTree() {
  const wrap = $('#treeWrap');
  if (!wrap) return;
  const t = S.tree || {};
  const nodes = (t.nodes || []).slice();
  if (!nodes.length || !t.root) {
    wrap.innerHTML = `<div class="empty" style="padding:40px 20px">
      这棵树还空着。<br>
      点「推演」开始，AI 会从世界现状长出第一段剧情；
      遇到关键节点会停下来让你选，选完再往下长。
      <p><button class="btn pri" onclick="treeExpand()">开始推演</button></p>
      <p class="small dim">先去「世界」页把设定、角色、当前局势填好，推演会更有落点。</p>
    </div>`;
    paintTreeToolbar(t, null);
    paintTreeDetail(null);
    return;
  }
  const size = treeLayout(nodes, t.root);
  const onPath = new Set(t.path || []);
  const sel = nodes.find(n => n.id === S.treeSel) || null;
  wrap.innerHTML = `<div class="treecanvas" style="width:${size.w}px;height:${size.h}px">
    <svg width="${size.w}" height="${size.h}">${treeEdges(nodes, onPath)}</svg>
    ${nodes.map(n => treeNodeHtml(n, onPath)).join('')}
  </div>`;
  applyTreeView();
  treeFitIfNeeded();
  paintTreeToolbar(t, sel);
  paintTreeDetail(sel);
}

function paintTreeToolbar(t, sel) {
  const st = t.stats || {};
  const set = (id, v) => { const e = $(id); if (e) e.textContent = v; };
  set('#treeStatNodes', st.nodes || 0);
  set('#treeStatPending', t.pending_events || 0);
  set('#treeStatDecisions', st.decisions || 0);
  // event_words 在后端是 tree() 的顶层字段，不在 stats 里——只读 st 会永远显示 0
  set('#treeStatWords', t.event_words || st.event_words || 0);

  /* 「推演」按钮从哪开始长：选中了卡片就从那张卡片长。
     按钮自己把这件事说出来，省得你以为它是从最下面开始的。 */
  const ex = $('#treeExpandBtn');
  if (ex) {
    const nodes = t.nodes || [];
    const ok = sel && sel.kind !== 'decision'
      && !nodes.some(x => x.parent_id === sel.id);
    if (ok) {
      ex.textContent = '从选中的往下推';
      ex.title = `选中了「${sel.title}」——点这里就从它接着往下推演一段`;
    } else {
      ex.textContent = '推演';
      ex.title = '点卡片可以改成从那张卡片开始；不选就顺着当前这条线的末梢往下推';
    }
  }

  const btn = $('#treeCommitBtn');
  if (btn) {
    const n = t.pending_events || 0;
    const behind = t.behind_world || 0;
    btn.disabled = !t.can_commit;
    btn.textContent = n ? `落定成正文（${n}）` : '落定成正文';
    btn.title = !t.can_commit
      ? '这条线还没有新的推演结果可落——先点「推演」长一段'
      : (behind
        ? `⚠️ 这条线落在已写进展之前（后面还有 ${behind} 个已落定的事件），`
          + '落定会让世界时间往回接，和已有章节对不上'
        : `把这条线上还没落定的 ${n} 个事件写进世界（落定后就能去章节页写正文）`);
  }
}

function paintTreeDetail(n) {
  const box = $('#treeDetail');
  if (!box) return;
  if (!n) {
    box.innerHTML = `<div class="card"><h3>节点详情</h3><div class="body dim small">
      点图上任意一个方块，这里会显示它写了什么、以及能对它做什么。</div>
      <div class="body" style="border-top:1px solid var(--line-2)">
        <div class="small dim" style="line-height:2">
          <b>怎么看这张图</b><br>
          实线 = 你正在走的这条线；虚线 = 试过或还没走的分支。<br>
          橙色方块是<b>岔路口</b>，得你挑一条才算往下走。<br>
          蓝色方块是它的<b>分支</b>，点一下就会顺着它继续推演。
        </div>
      </div></div>`;
    return;
  }
  const t = S.tree || {};
  const onPath = new Set(t.path || []);
  const isTip = (t.path || []).slice(-1)[0] === n.id;
  const tOn = n.trigger_label || ({
    moral: '道德两难', cost: '重大代价',
    irreversible: '不可逆', user_focus: '你关注的焦点',
  })[n.trigger_type] || '';
  let acts = '';

  if (n.kind === 'root') {
    acts = `<button class="btn pri" onclick="treeExpand(${n.id})">从这里开始推演</button>
      <button class="btn danger ghost" onclick="treeReset()">重开一棵树</button>`;
  } else if (n.kind === 'decision') {
    const kids = (S.tree.nodes || [])
      .filter(x => x.parent_id === n.id && x.kind === 'option')
      .sort((a, b) => (a.seq - b.seq) || (a.id - b.id));
    acts = `<div class="small dim" style="margin-bottom:6px">${
      n.decision_ref
        ? '这个岔路口已经落定过了，但当时没选——现在补选一条，后面的推演会接着它长：'
        : '挑一条走：'}</div>` +
      (kids.length ? kids.map((o, i) => `
        <div class="opt" onclick="treeStep(${n.id},${i})">
          <div class="lb">${i + 1}. ${esc(o.title)}</div>
          <div class="ds">${esc(o.description || '')}</div>
          ${o.branch_hint ? '<div class="ch">走向：' + esc(o.branch_hint) + '</div>' : ''}
        </div>`).join('')
        : '<div class="small dim">这个岔路口没生成出选项。</div>') +
      `<div class="small dim" style="margin-top:8px">
        选中的那条会立刻往下推演；没选的留着，回头可以从这里另走。</div>`;
  } else if (n.kind === 'option') {
    const hasKids = (S.tree.nodes || []).some(x => x.parent_id === n.id);
    acts = hasKids
      ? `<button class="btn pri" onclick="treeJump(${n.id})">走这条线</button>
         <button class="btn" onclick="treeRebranch(${n.id})">从此处另推一条</button>`
      : `<button class="btn pri" onclick="treeExpand(${n.id})">顺着这条分支推演</button>`;
    acts += `<button class="btn danger ghost" onclick="treePrune(${n.id})">砍掉这条分支</button>`;
  } else {
    acts = (isTip
      ? `<button class="btn pri" onclick="treeExpand(${n.id})">接着往下推演</button>`
      : `<button class="btn" onclick="treeJump(${n.id})">走这条线</button>
         <button class="btn" onclick="treeRebranch(${n.id})">从这里另推一条</button>`);
    if (isTip && t.can_commit) {
      acts += `<button class="btn pri" onclick="treeCommit()">落定成正文</button>`;
    }
    if (!n.event_ref) {
      acts += `<button class="btn danger ghost" onclick="treePrune(${n.id})">
        砍掉这段线</button>`;
    }
  }

  /* 「改这张卡」两个入口：自己改 / 让 AI 改。
     落定过的卡不给改（正文已经按它写了），但**仍允许查下游衔接**——
     作者可能就是想确认一下这条线还立不立得住。 */
  const lockable = (n.kind !== 'root' && n.kind !== 'decision');
  if (lockable) {
    acts += `<button class="btn sm" onclick="treeOpenEdit(${n.id})"
      title="${n.event_ref
        ? '这段已落成正文，只能改主线以外的字段或去章节页改正文'
        : '自己动手改这张卡的字段'}">✎ 改这张卡</button>`;
    acts += `<button class="btn sm" onclick="treeOpenFix(${n.id})"
      title="用一句话说明哪里不对，让 AI 来改">🪄 说哪里不对，让 AI 改</button>`;
    acts += `<button class="btn sm ghost" onclick="treeVerify(${n.id})"
      title="检查这张卡下游的事件是否还接得上">🔍 查下游衔接</button>`;
  }

  box.innerHTML = `<div class="card">
    <h3>${KIND_ICON[n.kind] || '●'} ${esc(KIND_LABEL[n.kind] || '')}
      <span class="spacer"></span>
      <span class="dim small" style="font-weight:400">#${n.id}</span></h3>
    <div class="body">
      <div style="font-weight:620;margin-bottom:6px">${esc(n.title)}</div>
      ${treeDescHtml(n)}
      ${treeFieldsHtml(n)}
      ${n.stakes ? `<div class="small dim" style="margin-top:8px">
        <b>代价</b>：${esc(n.stakes)}</div>` : ''}
      ${n.kind === 'decision' ? treeForkOriginHtml(n) : ''}
      <div class="row wrap small dim" style="margin-top:10px;gap:8px">
        ${tOn ? `<span class="tag warn">${esc(tOn)}</span>` : ''}
        ${n.actor_name ? `<span class="tag">${esc(n.actor_name)}</span>` : ''}
        ${n.world_time ? `<span class="tag">${esc(n.world_time)}</span>` : ''}
        <span class="tag ${n.event_ref ? 'ok' : onPath.has(n.id) ? 'accent' : ''}"
          >${esc(n.status_label || n.status)}</span>
      </div>
      ${(n.involved_characters || []).length ? `<div class="small dim"
        style="margin-top:8px">涉及：${esc(n.involved_characters.join('、'))}</div>` : ''}
      <hr class="sep">
      <div class="row wrap">${acts}</div>
      <div id="treeEditBox"></div>
    </div>
  </div>`;

  // 详情重绘后把之前打开的编辑/校验面板还原回去（否则点一次推演就没了）
  restoreTreeCardPanel();
}

/* 岔路是"从哪条事件里长出来的"。
   旧版岔路口卡片只有一句"要不要签"，用户看不出它凭什么出现在这儿；
   v6.9 起模型必须给 reason 和 forks_from，这里把它摆到选项上方。 */
function treeForkOriginHtml(n) {
  const forks = n.forks_from || '';
  const reason = n.reason || '';
  if (!forks && !reason) return '';
  return `<div class="note" style="margin-top:8px;padding:8px 10px;
      border-left:3px solid var(--warn,#d98a00);background:rgba(217,138,0,.07);
      border-radius:4px">
    ${forks ? `<div class="small" style="margin-bottom:4px">
      <b>从</b> <span style="font-weight:600">${esc(forks)}</span> <b>逼出来</b></div>` : ''}
    ${reason ? `<div class="small dim" style="line-height:1.55">${esc(reason)}</div>` : ''}
  </div>`;
}


function treeSelect(id) {
  // 刚拖完画布落下的那次 click 不算点选（指针捕获下它可能落到任意地方）
  if (S.treeDragJustNow) { S.treeDragJustNow = false; return; }
  S.treeSel = id;
  paintTree();
  revealTreeDetail();
}

/* 选完把右侧详情带进视野：窄屏时详情会落到画布下面，
   不滚一下就会"点了像没反应"。（已经看得见的时候 nearest 不会乱滚） */
function revealTreeDetail() {
  const box = $('#treeDetail');
  if (!box) return;
  try { box.scrollIntoView({ block: 'nearest', behavior: 'smooth' }); } catch (_) {}
}

/* ============================================================
   改事件卡（v6.8）：自己改 / 让 AI 改 / 查下游衔接

   用户原话：「事件卡片有逻辑不对的地方，可以点击修改，也可以提交文字
   说明哪里不对，让AI修改。修改后需要校验后续事件是否和修改的内容
   上下衔接并且符合逻辑。」

   所以面板有三种形态，共用 #treeEditBox 这一个容器：
     edit  —— 字段表单（自己改）
     fix   —— 一段话说明 + 让 AI 改，改完自动带出衔接校验结果
     check —— 只查下游衔接
   面板状态存在 S._tcard 里，因为 paintTree 会整个重绘详情区。
   ============================================================ */

const TCARD_DOWN_LEVEL = { broken: '硬断裂', weak: '牵强' };
const TCARD_ASPECT = {
  causality: '因果', fact: '事实', knowledge: '认知',
  world_state: '世界状态', motive: '动机',
};

/* 字段名 → 界面上叫它什么。冲突提示要说清"是哪儿被改了"，
   而用户看到的是「做了什么」，不是 `action`。 */
const TCARD_FIELD_LABEL = {
  title: '标题', action: '做了什么', intent: '为什么', result: '结果',
  description: '连贯叙述', stakes: '代价 / 风险',
  actor_name: '行动主体', event_type: '事件类型', importance: '重要度',
};
function tcFieldLabel(k) { return TCARD_FIELD_LABEL[k] || k; }

async function treeOpenEdit(id) {
  S._tcard = { mode: 'edit', node: id, data: null, err: '' };
  renderTreeCardPanel(true);
}
async function treeOpenFix(id) {
  S._tcard = { mode: 'fix', node: id, data: null, err: '', check: null };
  renderTreeCardPanel(true);
}
async function treeVerify(id) {
  S._tcard = { mode: 'check', node: id, data: null, err: '', check: null };
  renderTreeCardPanel(true);
}

/* 面板重绘：详情区被 paintTree 重建后，把之前打开的面板还原回去 */
function restoreTreeCardPanel() {
  if (S._tcard && S._tcard.mode) renderTreeCardPanel(false);
}

function treeCloseCardPanel() {
  S._tcard = null;
  const b = $('#treeEditBox');
  if (b) b.innerHTML = '';
}

async function renderTreeCardPanel(fetchNow) {
  const st = S._tcard;
  const box = $('#treeEditBox');
  if (!st || !box) return;
  const id = st.node;

  if (fetchNow || !st.data) {
    box.innerHTML = `<div class="small dim" style="margin-top:10px">正在读取这张卡…</div>`;
    try {
      st.data = await api('/api/tree/card', {
        method: 'POST', body: { novel_id: S.novelId, node_id: id } });
      st.err = '';
    } catch (e) {
      st.err = e.message;
    }
  }
  if (st.err) {
    box.innerHTML = `<div class="tcerr">${esc(st.err)}</div>
      <div class="row" style="margin-top:8px">
        <button class="btn sm" onclick="treeCloseCardPanel()">关闭</button></div>`;
    return;
  }
  const d = st.data || {};
  const head = `<div class="tcwrap">`;

  if (st.mode === 'edit') {
    box.innerHTML = head + tcEditForm(d) + tcCheckHtml(st) + `</div>`;
    return;
  }
  if (st.mode === 'fix') {
    box.innerHTML = head + tcFixForm(d, st) + tcCheckHtml(st) + `</div>`;
    return;
  }
  box.innerHTML = head + tcCheckHtml(st) + `</div>`;
}

function tcEditForm(d) {
  if (d.locked) {
    return `<div class="tcnote">${esc(d.locked_reason || '这段已落定，不能改。')}</div>
      <div class="row" style="margin-top:8px">
        <button class="btn sm" onclick="treeCloseCardPanel()">关闭</button></div>`;
  }
  const f = (k, label, val, rows, hint) => `
    <label class="tcf">
      <span class="k">${label}${hint ? `<i>${hint}</i>` : ''}</span>
      ${rows
        ? `<textarea data-f="${k}" rows="${rows}">${esc(val || '')}</textarea>`
        : `<input data-f="${k}" value="${esc(val || '')}">`}
    </label>`;
  return `<div class="tcwrap-in">
    <div class="tctitle">✎ 改这张卡</div>
    ${f('title', '标题', d.title, 0, '一句话概括')}
    ${f('action', '做了什么', d.action, 2, '卡片第一行')}
    ${f('intent', '为什么', d.intent, 2, '卡片第二行')}
    ${f('result', '结果', d.result, 2, '卡片第三行')}
    <div class="tcrow">
      <label class="tcf half"><span class="k">行动主体</span>
        <input data-f="actor_name" value="${esc(d.actor_name || '')}"></label>
      <label class="tcf half"><span class="k">事件类型</span>
        <input data-f="event_type" value="${esc(d.event_type || '')}"></label>
    </div>
    ${f('description', '连贯叙述', d.description, 4, '给正文层当素材')}
    ${f('stakes', '代价 / 风险', d.stakes, 2, '')}
    <div class="row wrap" style="margin-top:10px">
      <button class="btn pri sm" onclick="treeSaveEdit(${d.id})">保存</button>
      <button class="btn sm" onclick="treeSaveEdit(${d.id}, true)">保存并查上下游</button>
      <button class="btn ghost sm" onclick="treeCloseCardPanel()">取消</button>
    </div>
    <div class="small dim" style="margin-top:7px;line-height:1.7">
      「做了什么 / 为什么 / 结果」是这张卡的主干，改了它们下游可能就对不上了——
      保存时建议勾上「保存并查上下游」。</div>
  </div>`;
}

function tcFixForm(d, st) {
  if (d.locked) {
    return `<div class="tcnote">${esc(d.locked_reason || '这段已落定，不能改。')}</div>
      <div class="row" style="margin-top:8px">
        <button class="btn sm" onclick="treeCloseCardPanel()">关闭</button></div>`;
  }
  return `<div class="tcwrap-in">
    <div class="tctitle">🪄 说哪里不对，让 AI 改</div>
    <div class="small dim" style="line-height:1.75;margin-bottom:8px">
      用大白话写哪儿不对就行，比如「他不该知道这件事，这时候他还没见过老周」、
      「结果是拿到钥匙，可是行动写的是去找人，前后对不上」。</div>
    <label class="tcf">
      <span class="k">哪里不对<i>必填</i></span>
      <textarea id="tcfComplaint" rows="4"
        placeholder="例：这张卡里他知道得太多了，这时候他还没见过老周"></textarea>
    </label>
    <label class="tcf">
      <span class="k">希望怎么改<i>可选，比上面的话更管用</i></span>
      <textarea id="tcfDirection" rows="2"
        placeholder="例：改成他是从别人嘴里听说的，而且只知道一半"></textarea>
    </label>
    <div class="row wrap" style="margin-top:10px">
      <button class="btn pri sm" onclick="treeRunFix(${d.id})">让 AI 改</button>
      <button class="btn ghost sm" onclick="treeCloseCardPanel()">取消</button>
    </div>
    <div class="small dim" style="margin-top:7px;line-height:1.7">
      AI 只会改你指出的那处，其余字段原样保留；改完会<b>自动检查下游事件</b>
      是否还接得上。</div>
    ${st.running ? `<div class="tcrun" id="tcfRun">${
      esc(st.runmsg || '正在改…')}</div>` : ''}
  </div>`;
}

function tcCheckHtml(st) {
  const c = st.check;
  if (!c) return '';
  if (c.error) {
    return `<div class="tcnote warn">衔接校验没跑完：${esc(c.error)}</div>`;
  }
  const issues = c.issues || [];
  const head = `<div class="tccheck">
    <div class="tctitle">🔍 下游衔接校验
      <span class="spacer" style="flex:1"></span>
      <span class="small dim">检查了 ${c.checked || 0} 条，有问题 ${issues.length} 条</span>
    </div>`;
  if (!issues.length) {
    return head + `<div class="tcok">✓ 下游还接得上。
      ${c.ok_summary ? esc(c.ok_summary) : ''}</div>
      <div class="row" style="margin-top:8px">
        <button class="btn sm" onclick="treeCloseCardPanel()">关闭</button></div></div>`;
  }
  const items = issues.map(it => `
    <div class="tcissue ${it.level === 'broken' ? 'bad' : 'warn'}">
      <div class="l1">
        <span class="lv">${esc(TCARD_DOWN_LEVEL[it.level] || it.level)}</span>
        <span class="asp">${esc(TCARD_ASPECT[it.aspect] || it.aspect)}</span>
        <span class="nm">#${it.node_id} ${esc(it.title || '')}</span>
      </div>
      <div class="rs">${esc(it.reason)}</div>
      ${it.quote ? `<div class="qt">「${esc(it.quote)}」</div>` : ''}
      ${it.fix_hint ? `<div class="fx">→ ${esc(it.fix_hint)}</div>` : ''}
      <div class="row" style="margin-top:6px">
        <button class="btn sm ghost" onclick="treeSelect(${it.node_id})">
          看这条</button>
      </div>
    </div>`).join('');
  return head + items + `
    <div class="small dim" style="margin-top:8px;line-height:1.7">
      ${c.ok_summary ? esc(c.ok_summary) + '<br>' : ''}
      上面每条都可以点「看这条」跳过去改；改完再查一次即可。</div>
    <div class="row" style="margin-top:8px">
      <button class="btn sm" onclick="treeVerify(${st.node})">再查一次</button>
      <button class="btn ghost sm" onclick="treeCloseCardPanel()">关闭</button>
    </div></div>`;
}

/* ------------------------------------------------------------ 动作 */

function tcCollectFields() {
  const box = $('#treeEditBox');
  if (!box) return {};
  const out = {};
  box.querySelectorAll('[data-f]').forEach(el => {
    out[el.getAttribute('data-f')] = el.value;
  });
  return out;
}

async function treeSaveEdit(id, alsoCheck) {
  const st = S._tcard || {};
  const d = st.data || {};
  const fields = tcCollectFields();
  // 乐观锁：把打开面板时看到的值一起发回去，别人改过就报冲突而不是静默覆盖
  const expected = {};
  ['title', 'action', 'intent', 'result', 'description', 'stakes',
   'actor_name', 'event_type'].forEach(k => { expected[k] = d[k] || ''; });
  try {
    const r = await api('/api/tree/edit', {
      method: 'POST',
      body: { novel_id: S.novelId, node_id: id, fields, expected } });
    if (r.tree) S.tree = r.tree;
    toast('已保存', 'ok');
    st.data = null;                     // 强制重新拉最新的
    paintTree();
    if (alsoCheck) await treeVerify(id);
    else renderTreeCardPanel(true);
  } catch (e) {
    const b = $('#treeEditBox');
    const d2 = e.data || {};
    /* 409：冲突。这时候最不该做的就是让他对着一个已经过期的表单反复提交——
       把最新的卡拉回来，让他看着新内容重新决定。顺便说清是哪个字段变了。 */
    if (e.status === 409 || d2.conflict) {
      if (b) b.insertAdjacentHTML('afterbegin',
        `<div class="tcerr">这张卡已经被改过了${
          d2.field ? `（「${esc(tcFieldLabel(d2.field))}」）` : ''
        }。<br>已经帮你换成最新内容，请确认后再保存。</div>`);
      st.data = null;                 // 丢掉手里的旧快照
      toast('内容已过期，已刷新为最新', 'err');
      renderTreeCardPanel(true);
      return;
    }
    if (b) b.insertAdjacentHTML('afterbegin',
      `<div class="tcerr">${esc(e.message)}</div>`);
  }
}

async function treeRunFix(id) {
  const st = S._tcard || {};
  const cmp = $('#tcfComplaint'), dir = $('#tcfDirection');
  const complaint = cmp ? cmp.value.trim() : '';
  if (!complaint) { toast('先写清楚哪里不对', 'err'); return; }
  st.running = true; st.runmsg = '正在把你的说明交给模型…';
  renderTreeCardPanel(false);
  const r = await runJob('/api/tree/ai_fix',
    { novel_id: S.novelId, node_id: id, complaint,
      direction: dir ? dir.value.trim() : '', check: true },
    'treeLog', '改这张卡', res => {
      S.tree = res.tree || S.tree;
      st.check = res.check || null;
      const fx = res.fix || {};
      st.data = null;
      st.running = false;
      st.mode = 'fix';
      if (fx.no_change) {
        toast('模型认为不用改——看看它怎么说', 'ok');
      } else {
        toast('已改：' + ((fx.changed || []).join('、') || '（无）'), 'ok');
      }
      paintTree();
      // 把模型的说明摆在面板最上面，别让它随重绘消失
      setTimeout(() => {
        const b = $('#treeEditBox');
        if (!b) return;
        const bits = [];
        if (fx.no_change) {
          bits.push(`<div class="tcnote">模型认为这张卡不用改。${
            fx.why ? esc(fx.why) : ''}</div>`);
        } else if (fx.why) {
          bits.push(`<div class="tcnote ok">改动说明：${esc(fx.why)}</div>`);
        }
        if (fx.note) {
          bits.push(`<div class="tcnote warn">需要你留意：${esc(fx.note)}</div>`);
        }
        if (bits.length) b.insertAdjacentHTML('afterbegin', bits.join(''));
      }, 30);
    });
  if (r === null) {
    st.running = false;
    renderTreeCardPanel(false);
  }
}


function treeRefreshTree(t) {
  if (t && t.tree) S.tree = t.tree;
  paintTree();
  const d = S.tree && S.tree.pending_decision;
  $('#tabDotTree') && $('#tabDotTree').classList.toggle('show', !!d);
}

async function treeExpand(nodeId, force) {
  const t = S.tree || {};
  const nodes = t.nodes || [];

  /* 没指定起点时：你点选了哪张卡片，就从那张卡片往下长。
     以前这里一律从"末梢"（最下面）开始，于是"选前面的卡片再点推演"
     看着毫无反应、内容还是接着最下面那条线——这正是会踩的坑。 */
  if (!nodeId && S.treeSel) {
    const sel = nodes.find(n => n.id === S.treeSel);
    if (sel && sel.kind === 'decision') {
      // 选中岔路口：它自己不是"一段剧情"，只能挑分支
      toast('这是个岔路口——先在右侧挑一条分支，再往下推', 'ok');
      return null;
    }
    if (sel) nodeId = sel.id;
  }

  if (nodeId) {
    const n = nodes.find(x => x.id === nodeId);
    if (n && n.kind === 'decision') {
      S.treeSel = nodeId; paintTree();
      toast('这是个岔路口——先在右侧挑一条分支，再往下推', 'ok');
      return null;
    }
    if (!force && nodes.some(x => x.parent_id === nodeId)) {
      S.treeSel = nodeId; paintTree();
      confirmDialog('从这张卡片重新岔一条？', `<p style="margin-top:0">
        「<b>${esc(n ? n.title : '')}</b>」后面已经推过一段了。<br>
        在这儿重新往下推，会长出<b>并列的一条新线</b>，原来那条留着，两条可以对着比。</p>
        <p class="dim small" style="margin:0">这次推演只会看到这张卡片为止的剧情——
        <b>它之后发生的事都不算数</b>，不会再接着最下面那条线写。</p>`,
        '重新岔一条', () => treeExpand(nodeId, true));
      return null;
    }
  } else {
    // 没有选中任何卡片：顺着当前这条线的末梢往下长（原来就有的保护）
    if (t.tail_kind === 'decision') {
      S.treeSel = t.leaf; paintTree();
      toast('这条线停在岔路口上——先挑一条分支，再往下推', 'ok');
      return null;
    }
    if (t.tail_kind === 'option'
        && nodes.some(x => x.parent_id === t.leaf)) {
      S.treeSel = t.leaf; paintTree();
      toast('这个分支已经推过了，点「从此处另推一条」才能再分岔', 'ok');
      return null;
    }
  }
  const body = { novel_id: S.novelId };
  if (nodeId) body.node_id = nodeId;
  const f = $('#treeFocus');
  if (f && f.value.trim()) body.focus = f.value.trim();
  const r = await runJob('/api/tree/expand', body, 'treeLog',
    (nodeId && nodeId !== t.leaf) ? '从这张卡片重新推演' : '推演', res => {
      S.tree = res.tree || S.tree;
      S.treeSel = res.decision ? res.decision.id : (res.leaf || null);
      paintTree();
      toast(treeChurnMsg(res), 'ok');
    });
  if (r === null) paintTree();
  return r;
}

// 推演结果说人话：事件几条、有没有人进出、撞没撞出岔路。
// 旧版只说"遇到岔路口了/推演完成"——世界有没有动、有没有人登场退场，
// 用户完全看不见，只能自己去别的页面翻。
function treeChurnMsg(res) {
  const n = (res.created || []).length;
  const bits = ['推演出 ' + n + ' 个事件'];
  const add = (res.new_characters || []).map(c => c.name);
  const out = (res.character_exits || []).map(c => c.name);
  if (add.length) bits.push('登场 ' + add.join('、'));
  if (out.length) bits.push('退场 ' + out.join('、'));
  if (res.relation_count) bits.push('新羁绊 ' + res.relation_count + ' 条');
  if (res.decision) bits.push('撞出岔路：' + res.decision.title);
  else bits.push('这段没分岔，世界按惯性走');
  return bits.join('；');
}

async function treeStep(decId, idx) {
  const body = { novel_id: S.novelId, node_id: decId, option_index: idx };
  const f = $('#treeFocus');
  if (f && f.value.trim()) body.focus = f.value.trim();
  const r = await runJob('/api/tree/step', body, 'treeLog', '顺着分支推演', t => {
    S.tree = t.tree || S.tree;
    S.treeSel = t.decision ? t.decision.id : (t.leaf || null);
    paintTree();
    toast(treeChurnMsg(t), 'ok');
  });
  if (r === null) paintTree();
  return r;
}

async function treeJump(nodeId) {
  try {
    const t = await api('/api/tree/jump', { method: 'POST',
      body: { novel_id: S.novelId, node_id: nodeId } });
    S.tree = t; S.treeSel = nodeId; paintTree();
    toast('已切到这条线', 'ok');
  } catch (e) { toast(e.message, 'err'); }
}

function treeRebranch(nodeId) {
  confirmDialog('从这儿另推一条？', `<p style="margin-top:0">
    会在<b>这个节点</b>下面再长出一段新的推演，原来那条继续保留，
    两条线可以对着比。</p>
    <p class="dim small" style="margin:0">
    这次推演只会看到这个节点为止的剧情——<b>它之后发生的事都不算数</b>。</p>`,
    '另推一条', () => treeExpand(nodeId, true));
}

function treePrune(nodeId) {
  confirmDialog('砍掉这段线？', `<p style="margin-top:0">
    这个节点以及它下面所有推演都会消失（正文里已经落定的不受影响）。</p>`,
    '砍掉', async () => {
      try {
        const t = await api('/api/tree/prune', { method: 'POST',
          body: { novel_id: S.novelId, node_id: nodeId } });
        S.tree = t; S.treeSel = null; paintTree();
        toast('已砍掉 ' + (t.removed || 0) + ' 个节点', 'ok');
      } catch (e) { toast(e.message, 'err'); }
    });
}

function treeReset() {
  confirmDialog('重开一棵树？', `<p style="margin-top:0">
    沙盘上的推演全部清空，从头再来。<br>
    <b>已经落定成正文的事件不受影响</b>，世界状态也不会回退。</p>`,
    '重开', async () => {
      try {
        const t = await api('/api/tree/reset', { method: 'POST',
          body: { novel_id: S.novelId } });
        S.tree = t; S.treeSel = null; paintTree();
        toast('沙盘已清空', 'ok');
      } catch (e) { toast(e.message, 'err'); }
    });
}

async function treeCommit() {
  const t = S.tree || {};
  const n = t.pending_events || 0;
  const opens = t.open_decisions || [];
  const behind = t.behind_world || 0;

  // v6.7：落定＝写正史，**必须先过预校验**（只读，不改一个字节）。
  // 有 error 就是硬伤，不能落——直接拦下并把问题摆出来。
  // 这一步不能省：正史一旦写进去，后面每一章都建立在它上面。
  let pre = null;
  try {
    pre = await api('/api/tree/precheck', { method: 'POST',
      body: { novel_id: S.novelId } });
  } catch (e) {
    // 预校验自己挂了（老库没迁移/接口异常）：不静默放行，让用户知道
    confirmDialog('预校验没能跑起来', `<p style="margin-top:0">
        落定前的校验接口报错：<b>${esc(e.message)}</b></p>
      <p class="dim small" style="margin-bottom:0">
        这意味着<b>这条线有没有硬伤是未知的</b>。继续落定会把未经校验的事件写进正史，
        后面很难查。建议先在设置页跑一次数据库迁移，再回来落定。</p>`,
      '仍然落定', () => doTreeCommit());
    return;
  }

  const errs = (pre && pre.errors) || [];
  const warns = (pre && pre.warnings) || [];
  if (errs.length) {
    openModal('这条线有硬伤，不能落定', `
      <p style="margin-top:0">预校验发现 <b>${errs.length}</b> 处硬伤。
        落定会把它们写成正史，所以先拦下来。</p>
      <div style="max-height:300px;overflow:auto">
        ${precheckListHtml(errs, 'bad')}
      </div>
      <p class="dim small" style="margin-bottom:0">
        处理办法：回沙盘上把出问题的节点改掉（删掉重推 / 换一条分支），再落定。</p>`,
      `<button class="btn" onclick="closeModal()">知道了</button>`);
    return;
  }

  confirmDialog('把这条线落定成正文？', `<p style="margin-top:0">
    这条线上 <b>${n}</b> 个还没落定的事件会写进世界，成为章节页的素材。</p>
    ${warns.length ? `<div style="margin:0 0 10px;padding:9px 11px;border-radius:8px;
      background:var(--warn-soft);border:1px solid var(--warn)">
      <div class="small" style="font-weight:620;margin-bottom:4px">
        预校验有 ${warns.length} 条提醒（不拦人，你自己判断）</div>
      ${precheckListHtml(warns, 'warn')}</div>` : ''}
    ${opens.length ? `<p class="small" style="margin:0 0 8px">
      ⚠️ 这条线停在岔路口「<b>${esc(opens[0].title)}</b>」上，你还没选。
      落定不受影响——这个岔路口会记成一条<b>待决点</b>留在世界里，之后随时能补定。</p>` : ''}
    ${behind ? `<p class="small" style="margin:0 0 8px;color:var(--warn)">
      ⚠️ 这是一条<b>回头推的线</b>：它后面还有 ${behind} 个已经写进正文的事件。
      落定会把世界时间接回这个位置，和已经写好的章节对不上（前面那些事件不会消失，
      但世界状态会被这条线覆盖）。想保留按时间往下走的版本，就别在这条线上落定。</p>` : ''}
    <p class="dim small" style="margin:0">
    落定之后世界状态会推进，<b>沙盘上没走的分支会被清掉</b>——
    它们建立在落定前的世界状态上，留着已经不准了。<br>
    落定过的部分没法撤回；要改请到章节页改正文。<br>
    这条线本身会留在沙盘上，可以接着往下推下一章。</p>`,
    '落定为正史', () => doTreeCommit());
}

function precheckListHtml(items, kind) {
  const color = kind === 'bad' ? 'var(--bad)' : 'var(--warn)';
  return `<ul style="margin:6px 0 0;padding-left:18px">${items.map(x => `
    <li class="small" style="margin-bottom:7px;color:${color}">
      ${x.event_title ? `<b>${esc(x.event_title)}</b>：` : ''}${esc(x.message || '')}
      ${x.fix_hint ? `<div class="dim" style="color:var(--ink-3)">建议：${esc(x.fix_hint)}</div>` : ''}
    </li>`).join('')}</ul>`;
}

async function doTreeCommit() {
  try {
    const t2 = await api('/api/tree/commit', { method: 'POST',
      body: { novel_id: S.novelId } });
    S.tree = t2; S.treeSel = null;
    // 落在岔路口上：顺手把它选中，右侧直接摆出分支，省一次点击
    const pd = t2.pending_decision;
    if (pd) S.treeSel = pd.id;
    paintTree();
    const c = t2.commit || {};
    toast(`已落定 ${c.events || 0} 个事件`, 'ok');
    treeAfterCommit(c);
  } catch (e) { toast(e.message, 'err'); }
}

/* 落定完把"下一步"摆到眼前——别让人落定了还不知道去哪写 */
function treeAfterCommit(c) {
  const log = $('#treeLog');
  if (!log) return;
  const pd = (S.tree || {}).pending_decision;
  log.innerHTML = `
    <div style="margin-top:12px;padding:12px 14px;border:1px solid var(--ok);
      background:var(--ok-soft);border-radius:10px">
      <div style="font-weight:620;margin-bottom:4px">✓ 已落定 ${c.events || 0} 个事件</div>
      <div class="small dim" style="line-height:1.7">
        它们已经写进世界，现在是<b>待成章素材</b>——到章节页就能写成一章正文。
        ${c.open_decisions ? `<br>有 ${c.open_decisions} 个岔路口你没定，
          已经记成待决点存在世界里（世界页能看到），写正文时或之后补定都行。` : ''}
      </div>
      <div class="row wrap" style="margin-top:10px">
        <button class="btn pri" onclick="go('chapter')">去章节页写这一章</button>
        <button class="btn" onclick="treeNextAfterCommit()">
          ${pd ? '先挑一条分支' : '接着推下一章'}</button>
      </div>
    </div>`;
}

/* 落定后想接着推：停在岔路口上得先选一条，别让「推演」按钮直接报错 */
function treeNextAfterCommit() {
  const pd = (S.tree || {}).pending_decision;
  if (pd) {
    S.treeSel = pd.id; paintTree();
    toast('先在这个岔路口挑一条分支，才好往下推', 'ok');
    return;
  }
  treeExpand();
}

/* ------------------------------------------------------------
   视图三：章节
   ------------------------------------------------------------ */
/* ---------------------------------------------------------------- 正文-正史冲突卡片（v6.7）
   核心口径：「正史不是正文说了算，而是正文负责呈现、正史负责裁决。」

   所以正文回流发现冲突时**不覆盖正史**，而是把它挂到这里等用户三选一。
   三个选项各自的后果不一样，弹层里必须写清楚——特别是
   「更新正史」会补一条 canon_revision 事件（正史变更必须有来源，
   否则第 20 章的推演会看到一条来路不明的变更，因果链断裂）。
   ------------------------------------------------------------ */
const CONFLICT_KIND_LABEL = {
  prose_contradicts_fact: '正文与正史矛盾',
  prose_invents_fact: '正文出现正史没有的事',
  knowledge_leak: '角色知道了不该知道的',
  rule_violation: '违反世界规则',
};

function conflictCard() {
  const cs = S.conflicts || [];
  if (!cs.length) return '';
  return `
    <div class="card" style="margin-bottom:16px;border-color:var(--bad)">
      <h3 style="color:var(--bad)">正文与正史冲突
        <span class="spacer"></span>
        <span class="tag bad">${cs.length} 处未裁决</span></h3>
      <div class="body">
        <p class="dim small" style="margin:0 0 12px">
          写出来的正文和已经写定的正史对不上。<b>正史没有被覆盖</b>——
          这几处先挂起，等你裁决。不裁决就继续往下写，后面会越写越乱。</p>
        ${cs.map(c => `
          <div style="padding:11px 0;border-bottom:1px solid var(--line-2)">
            <div style="margin-bottom:5px">
              <span class="tag bad">${esc(CONFLICT_KIND_LABEL[c.conflict_type]
                || c.conflict_type || '')}</span>
              ${c.chapter_number ? `<span class="dim small">
                第 ${c.chapter_number} 章</span>` : ''}
              <span class="dim small">${esc(c.severity || '')}</span>
            </div>
            <div class="small" style="margin-bottom:4px">
              <b>正文写的</b>：${esc(c.prose_statement || '')}</div>
            ${c.quote ? `<div class="small dim" style="margin-bottom:4px">
              原文：「${esc(c.quote)}」</div>` : ''}
            <div class="small" style="margin-bottom:8px">
              <b>正史写的</b>：${esc(c.canon_statement || '')}</div>
            ${c.detail ? `<div class="small dim" style="margin-bottom:8px">
              ${esc(c.detail)}</div>` : ''}
            <div class="row wrap">
              <button class="btn sm" onclick="resolveConflict(${c.id},'keep_canon')"
                title="以正史为准 → 去改正文">改正文</button>
              <button class="btn sm pri" onclick="resolveConflict(${c.id},'keep_prose')"
                title="以正文为准 → 正史改为正文说法">更新正史</button>
              <button class="btn sm ghost" onclick="resolveConflict(${c.id},'ignored')"
                title="记在案，不再提示">忽略</button>
            </div>
          </div>`).join('')}
      </div>
    </div>`;
}

function resolveConflict(id, action) {
  const c = (S.conflicts || []).find(x => x.id === id) || {};
  const head = `<p style="margin-top:0">${esc(c.prose_statement || '')}</p>`;
  if (action === 'keep_canon') {
    confirmDialog('以正史为准？', `${head}
      <p class="dim small">正史保持「${esc(c.canon_statement || '')}」不变。</p>
      <p class="dim small" style="margin-bottom:0">
        标记后建议去<b>章节页的诊断</b>跑一遍，按建议把正文改回正史的口径。
        这条冲突会从待裁决列表里消失，但记录仍然留在案。</p>`,
      '以正史为准', () => doResolveConflict(id, action));
    return;
  }
  if (action === 'keep_prose') {
    confirmDialog('以正文为准，改正史？', `${head}
      <p class="dim small" style="margin-bottom:0">
        正史会被改成正文的说法，旧事实标成"已被取代"（不删除，可回溯）。<br>
        同时会补一条 <b>canon_revision</b>（正史修正）事件，并写进本章事件流——
        正史的每一次变更都必须有来源，否则后面的推演会看到一条来路不明的改动，
        因果链就断了。<br>
        <b>只在正文写的才是你要的版本时才选它。</b></p>`,
      '更新正史', () => doResolveConflict(id, action));
    return;
  }
  confirmDialog('忽略这处冲突？', `${head}
    <p class="dim small" style="margin-bottom:0">
      只是不再提示，正史不会被改，正文也不会被改。
      以后想翻账还能在世界页的正史账本里看到。</p>`,
    '忽略', () => doResolveConflict(id, action));
}

async function doResolveConflict(id, action) {
  await api('/api/chapter/conflict/resolve', { method: 'POST',
    body: { novel_id: S.novelId, conflict_id: id, action: action } });
  toast('已裁决', 'ok');
  await refreshView('chapter'); render();
}

function renderChapter() {
  const cand = S.candidates || [];
  const plan = S.candPlan || {};
  const per = Math.max(1, plan.per_chapter || 4);
  // 每次进章节页都按推荐重置勾选：写完一章后列表变了，
  // 沿用旧勾选会指向已被别人用掉的事件。
  if (!Array.isArray(S.candSel)) {
    S.candSel = cand.slice(0, per).map(e => e.id);
  }
  const selSet = new Set(S.candSel);
  const sel = cand.filter(e => selSet.has(e.id));
  const selWords = sel.reduce((a, e) => a + String(e.description || '').length, 0);
  const allWords = cand.reduce((a, e) => a + String(e.description || '').length, 0);
  const left = cand.length - sel.length;
  const willMake = cand.length ? Math.ceil(cand.length / per) : 0;

  const html = `
    ${conflictCard()}
    <div class="card" id="writeNextCard" style="margin-bottom:16px">
      <h3>写下一章 <span class="spacer"></span>
        <span class="dim small" style="font-weight:400">
          勾选本章要写的事件——一章塞太多会写成流水账</span></h3>
      <div class="body">
        ${cand.length ? `
          <div class="row wrap" style="margin-bottom:10px">
            <span>待成章事件 <b>${cand.length}</b> 个</span>
            <span class="dim">·</span>
            <span class="dim small">共 ${allWords} 字素材，按每章 <b>${per}</b> 个算，
              还能写 <b>${willMake}</b> 章</span>
          </div>
          <div class="row wrap" style="margin-bottom:8px">
            <label class="small dim">本章写几个
              <input id="wrPer" type="number" min="1" max="${cand.length}"
                value="${sel.length}" style="width:72px;margin-left:6px"
                onchange="candSetPer(this.value)"></label>
            <button class="btn sm ghost" onclick="candSetPer(${per})">
              按推荐选前 ${Math.min(per, cand.length)} 个</button>
            <button class="btn sm ghost" onclick="candPickAll()">全选</button>
            <button class="btn sm ghost" onclick="candPickNone()">全不选</button>
            <span class="dim small" style="margin-left:auto">已选
              <b id="candCount">${sel.length}</b> 个 ·
              <b id="candWords">${selWords}</b> 字素材</span>
          </div>
          <div class="scroll" style="max-height:320px;border:1px solid var(--line);
            border-radius:9px;padding:4px 0;margin-bottom:12px">
            <ul class="list">${cand.map(e => {
              const on = selSet.has(e.id);
              return `<li id="cli${e.id}" style="${on ? '' : 'opacity:.45'}">
                <div style="padding-top:2px">
                  <input type="checkbox" id="cd${e.id}" ${on ? 'checked' : ''}
                    style="cursor:pointer;width:15px;height:15px"
                    onchange="candToggle(${e.id}, this.checked)">
                </div>
                <div class="main">
                  <div class="title">${esc(e.title || '')}
                    <span class="tag">${esc(e.event_type || '')}</span>
                    <span class="dim small">重要度 ${e.importance || 3}</span>
                    ${e.world_time ? '<span class="dim small">· '
                      + esc(e.world_time) + '</span>' : ''}</div>
                  <div class="sub">${esc((e.description || '').slice(0, 160))}</div>
                </div></li>`;
            }).join('')}</ul>
          </div>
          <div class="row wrap">
            <button class="btn pri" id="writeBtn" onclick="openWrite()"
              ${sel.length ? '' : 'disabled'}>生成第 ${S.curChapter + 1} 章
              （用 ${sel.length} 个事件）</button>
            <button class="btn" onclick="go('tree')">回到推演沙盘</button>
            <span class="dim small">没勾的 ${left} 个留在列表里，
              写完这一章接着写下一章。</span>
          </div>`
        : `<div class="empty" style="padding:26px">
            还没有待成章的事件。<br>
            先去<b>推演</b>页长一条剧情线，满意了就「落定成正文」。
            <p><button class="btn pri" onclick="go('tree')">
              去推演</button></p></div>`}
        <div id="writeLog" style="margin-top:14px"></div>
      </div>
    </div>

    <div class="row" style="margin-bottom:12px">
      <input type="text" id="searchBox" placeholder="搜索正文…（至少 3 个字）"
        value="${esc(S.searchKw)}" style="max-width:320px"
        onkeydown="if(event.key==='Enter')doSearch()">
      <button class="btn" onclick="doSearch()">搜索</button>
      <button class="btn" onclick="exportAll()">导出全部章节</button>
    </div>
    <div id="searchOut"></div>

    <div class="card">
      <h3>章节列表 <span class="spacer"></span>
        <span class="dim small" style="font-weight:400">
          共 ${S.chapters.length} 章 ·
          ${S.chapters.reduce((a, c) => a + (c.word_count || 0), 0)} 字</span>
        <button class="btn sm pri" onclick="focusWriteCard()">
          ${cand.length ? '＋ 写第 ' + (S.curChapter + 1) + ' 章' : '＋ 去推演'}</button></h3>
      <div class="body tight">
        ${S.chapters.length ? `<ul class="list">${S.chapters.map(c => `
          <li>
            <div class="main">
              <div class="title">第${c.chapter_number}章 ${esc(c.title || '（无题）')}
                <span class="tag">${esc((S.meta.shape_labels || {})[c.chapter_shape] || c.chapter_shape || '')}</span>
                ${c.status === 'published' ? '<span class="tag ok">已发布</span>'
                  : '<span class="tag">草稿</span>'}
                <span class="dim small">${c.word_count || 0} 字</span></div>
              <div class="sub">${esc((c.summary || c.content || '').slice(0, 110))}</div>
            </div>
            <div class="acts">
              <button class="btn sm" onclick="openChapter(${c.chapter_number})">阅读</button>
              <button class="btn sm ghost" onclick="openChapterHistory(${c.chapter_number})">版本</button>
            </div>
          </li>`).join('')}</ul>`
          : '<div class="empty">还没有章节。</div>'}
      </div>
    </div>
  `;
  $('#v-chapter').innerHTML = html;
}

/* ---- 待成章事件的勾选 ---- */
function candSync() {
  const cand = S.candidates || [];
  const selSet = new Set(S.candSel || []);
  let n = 0, w = 0;
  cand.forEach(e => {
    const on = selSet.has(e.id);
    const cb = document.getElementById('cd' + e.id);
    if (cb) cb.checked = on;
    const li = document.getElementById('cli' + e.id);
    if (li) li.style.opacity = on ? '' : '.45';
    if (on) { n++; w += String(e.description || '').length; }
  });
  const c1 = $('#candCount'); if (c1) c1.textContent = n;
  const c2 = $('#candWords'); if (c2) c2.textContent = w;
  const b = $('#writeBtn');
  if (b) {
    b.disabled = n === 0;
    b.textContent = `生成第 ${S.curChapter + 1} 章（用 ${n} 个事件）`;
  }
  const perEl = $('#wrPer'); if (perEl) perEl.value = n || 1;
  return n;
}

function candToggle(id, on) {
  const keep = new Set(S.candSel || []);
  if (on) keep.add(id); else keep.delete(id);
  // 按列表原顺序重排，避免勾选顺序影响"下一批"的取法
  S.candSel = (S.candidates || []).filter(e => keep.has(e.id)).map(e => e.id);
  candSync();
}

function candSetPer(v) {
  const cand = S.candidates || [];
  const n = Math.max(1, Math.min(cand.length || 1, Math.round(Number(v)) || 1));
  S.candSel = cand.slice(0, n).map(e => e.id);
  candSync();
}

function candPickAll() {
  S.candSel = (S.candidates || []).map(e => e.id);
  candSync();
}

function candPickNone() {
  S.candSel = [];
  candSync();
}

/* 章节列表右上角的「写下一章」：有素材就滚到写作卡片，没有就回推演页 */
function focusWriteCard() {
  if (!(S.candidates || []).length) { go('tree'); return; }
  const el = document.getElementById('writeNextCard');
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

/* 一章写完，把\"下一步\"摆在眼前——这是上版最坑的地方：
   写完一章只剩一句 toast，用户根本不知道下一章在哪 */
function showNextStep(res) {
  const log = $('#writeLog');
  if (!log) return;
  const left = res.remaining || 0;
  const per = (S.candPlan || {}).per_chapter || 4;
  const more = left ? Math.ceil(left / per) : 0;
  const ch = res.chapter || {};
  log.innerHTML = `
    <div style="margin-top:12px;padding:12px 14px;border:1px solid var(--ok);
      background:var(--ok-soft);border-radius:10px">
      <div style="font-weight:620;margin-bottom:4px">✓ 第 ${ch.chapter_number || ''} 章写好了
        （${res.word_count || 0} 字，用了 ${res.event_count || 0} 个事件）</div>
      <div class="small dim" style="line-height:1.7">
        ${left ? `还剩 <b>${left}</b> 个待成章事件，大约还能写 <b>${more}</b> 章。`
               : '待成章的事件都用完了——想继续就回推演页再推一段、落定成正文。'}
        ${res.truncated ? '<br>⚠️ 这章被模型的输出上限截断了，结尾可能没收住，'
          + '建议调低目标字数或到章节页重写。' : ''}
      </div>
      <div class="row wrap" style="margin-top:10px">
        <button class="btn" onclick="openChapter(${ch.chapter_number || 0})">读这一章</button>
        ${left ? `<button class="btn pri" onclick="openWrite()">
          接着写第 ${(ch.chapter_number || 0) + 1} 章</button>`
        : `<button class="btn pri" onclick="go('tree')">去推演下一段</button>`}
      </div>
    </div>`;
}

async function doSearch() {
  const kw = $('#searchBox').value.trim();
  S.searchKw = kw;
  if (!kw) { $('#searchOut').innerHTML = ''; return; }
  $('#searchOut').innerHTML = '<div class="log"><div class="l">搜索中…</div></div>';
  try {
    const r = await api('/api/search?novel_id=' + S.novelId +
      '&q=' + encodeURIComponent(kw));
    const rs = r.results || [];
    $('#searchOut').innerHTML = `<div class="card" style="margin-bottom:16px">
      <h3>搜索「${esc(kw)}」· ${rs.length} 条</h3>
      <div class="body tight">${rs.length ? `<ul class="list">${rs.map(x => `<li>
        <div class="main">
          <div class="title">第${x.chapter_number}章 ${esc(x.title || '')}</div>
          <div class="sub">${esc(x.snippet || '')}</div>
        </div>
        <div class="acts"><button class="btn sm"
          onclick="openChapter(${x.chapter_number})">打开</button></div>
      </li>`).join('')}</ul>` : '<div class="empty">没有找到</div>'}</div></div>`;
  } catch (e) {
    $('#searchOut').innerHTML = '<div class="log"><div class="l err">' + esc(e.message) + '</div></div>';
  }
}

function openWrite() {
  const shapes = S.meta.shape_labels || {};
  const cand = S.candidates || [];
  const selSet = new Set(S.candSel || []);
  const sel = cand.filter(e => selSet.has(e.id));
  if (!sel.length) { toast('先在列表里勾几个事件', 'err'); return; }
  const left = cand.length - sel.length;
  const per = (S.candPlan || {}).per_chapter || 4;
  openModal('生成第 ' + (S.curChapter + 1) + ' 章', `
    <div class="small" style="background:var(--panel-2);border:1px solid var(--line);
      border-radius:9px;padding:10px 12px;margin-bottom:12px;line-height:1.7">
      本章用 <b>${sel.length}</b> 个事件：
      ${sel.slice(0, 3).map(e => esc(e.title || '')).join('、')}
      ${sel.length > 3 ? '…等 ' + sel.length + ' 个' : ''}<br>
      <span class="dim">写完后还剩 <b>${left}</b> 个待成章事件，
      ${left ? `大约还能写 <b>${Math.ceil(left / per)}</b> 章。`
             : '要写下一章得先回推演页再落定一段。'}</span>
      ${sel.length > per ? '<br>⚠️ 比推荐的每章 ' + per + ' 个多，'
        + '每件事分到的篇幅会变薄、容易被写成流水账；'
        + '建议拆成 ' + Math.ceil(sel.length / per) + ' 章分别写。' : ''}
    </div>
    <label class="f"><span>叙事形态</span>
      <select id="wrShape">
        <option value="">（让 AI 根据事件自己判断）</option>
        ${Object.keys(shapes).map(k =>
          `<option value="${k}">${esc(shapes[k])}</option>`).join('')}
      </select></label>
    <label class="f"><span>目标字数</span>
      <input type="number" id="wrWords" value="${(S.novel || {}).target_words_per_chapter || 3000}"
        min="500" step="200"></label>
    <label class="f"><span>额外要求（可选）</span>
      <textarea id="wrNotes" rows="3"
        placeholder="例如：这一章多写环境与声音，少写内心"></textarea></label>
    <p class="dim small">生成通常需要 1-3 分钟，期间可以继续用其他功能。</p>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn pri" onclick="doWrite()">开始写</button>`);
}

async function doWrite() {
  const ids = S.candSel || [];
  if (!ids.length) { toast('一个事件都没勾', 'err'); return; }
  const body = {
    novel_id: S.novelId,
    chapter_number: S.curChapter + 1,
    shape: $('#wrShape').value || null,
    words: parseInt($('#wrWords').value, 10) || null,
    notes: $('#wrNotes').value,
    event_ids: ids,
  };
  closeModal();
  await runJob('/api/chapter/write', body, 'writeLog', '正文生成', async (res) => {
    await refreshView('chapter');
    render();
    showNextStep(res);
  });
}

/* ------------------------------------------------------------
   后台任务通用处理

   任务动辄几十秒，期间可能只跟模型交换一两次数据。所以除了服务端日志，
   前端自己走一个心跳：每 0.5 秒重画一次，秒数一直在动；每收到新日志就记一次
   活动时间，据此显示"最后活动多久前"。进度条上还有一道光一直在扫——哪怕进度
   值没变，也看得出程序还活着，不用猜是不是死机。
   ------------------------------------------------------------ */
async function runJob(path, body, logElId, label, onDone, existingJobId) {
  const el = document.getElementById(logElId);
  const t0 = Date.now();
  let lines = [{ t: '', msg: label + '已提交，等待模型…' }];
  let prog = 3, lastAt = Date.now(), finished = false;

  const isExchange = m => /次交换|已发出请求|已发出流式请求|开始流式生成/.test(m);
  const exchanges = () => lines.filter(l => isExchange(l.msg)).length;
  const secs = ms => Math.round(Math.max(0, ms) / 1000);

  const paint = () => {
    // 每次重新取一遍：切走再切回来时那个容器是新造的，别把日志留在废弃节点上
    const host = document.getElementById(logElId) || el;
    if (!host) return;
    const waited = secs(Date.now() - t0);
    const idle = secs(Date.now() - lastAt);
    const meta = finished
      ? '已完成'
      : `已运行 ${waited}s` + (lines.length > 1 ? ` · 最后活动 ${idle}s 前` : '');
    const stuck = !finished && idle >= 75;      // 长时间没动静：给个说法
    host.innerHTML = `
      <div class="jobmeta">
        ${finished ? '<span style="color:var(--ok)">✓</span>'
                   : '<span class="busy"></span>'}
        <span>${esc(label)}：${meta}</span>
        <span class="spacer" style="flex:1"></span>
        <span class="dim">与模型交换 ${exchanges()} 次</span>
      </div>
      <div class="bar${finished ? '' : ' run'}" style="margin-bottom:9px">
        <i style="width:${prog}%"></i></div>
      <div class="log">${lines.map(l =>
        `<div class="l${l.err ? ' err' : ''}"><span class="t">${l.t}</span> ${esc(l.msg)}</div>`
      ).join('')}</div>
      ${stuck ? `<div class="small" style="margin-top:8px;color:var(--warn);
        line-height:1.7">已经 ${idle}s 没有新动静了。推理模型长思考时就这样，
        可以再等等；若一直不动，多半是网络或网关卡住了，稍后重试即可。</div>` : ''}`;
    const lg = host.querySelector('.log');
    if (lg) lg.scrollTop = lg.scrollHeight;     // 最新一行始终可见
  };

  const timer = setInterval(paint, 500);
  const stop = () => { finished = true; clearInterval(timer); paint(); };

  paint();
  if (el) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  try {
    // existingJobId：任务已经在别处提交过了（比如 /api/chapter/status 发布时
    // 顺手就把 confirm_chapter 排进了队列），这里只要接上去看进度，不要重复提交。
    let jid = existingJobId;
    if (!jid) {
      const r = await api(path, { method: 'POST', body });
      jid = r.job_id;
    }
    let since = 0;
    for (;;) {
      await new Promise(s => setTimeout(s, 900));
      let j;
      try {
        j = (await api('/api/job?id=' + jid + '&since=' + since)).job;
      } catch (e) {
        lines.push({ t: '', msg: e.message, err: true }); stop(); break;
      }
      const fresh = j.log || [];
      if (fresh.length) {
        lastAt = Date.now();
        fresh.forEach(l => lines.push({
          t: new Date(l.t * 1000).toLocaleTimeString('zh-CN', { hour12: false }),
          msg: l.msg, err: /Traceback|Error/.test(l.msg) }));
        since += fresh.length;
        if (lines.length > 400) lines = lines.slice(-400);
        paint();
      }
      if (typeof j.progress === 'number') prog = j.progress;
      if (j.status === 'done') {
        prog = 100;
        lines.push({ t: '', msg: '完成。' });
        stop();
        if (onDone) await onDone(j.result);
        return j.result;
      }
      if (j.status === 'error') {
        prog = 100;
        lines.push({ t: '', msg: j.error, err: true });
        stop();
        toast(j.error, 'err');
        return null;
      }
    }
  } catch (e) {
    lines.push({ t: '', msg: e.message, err: true });
    stop();
    toast(e.message, 'err');
    return null;
  } finally {
    finished = true;
    clearInterval(timer);
  }
}

function openChapter(num) {
  api('/api/chapter?novel_id=' + S.novelId + '&chapter=' + num).then(r => {
    const c = r.chapter;
    const vs = r.versions || [];
    S.chapterView = c;
    const nEv = (c.source_event_ids || []).length;
    const isLast = c.chapter_number === S.curChapter;
    openModal(`第${c.chapter_number}章 ${c.title || ''}`, `
      <div class="row wrap small dim" style="margin-bottom:10px">
        <span class="tag">${esc((S.meta.shape_labels || {})[c.chapter_shape] || c.chapter_shape || '')}</span>
        <span>${c.word_count || 0} 字</span>
        ${nEv ? '<span>用了 ' + nEv + ' 个事件</span>' : ''}
        ${c.pov_character ? '<span>视角：' + esc(c.pov_character) + '</span>' : ''}
        ${c.ending_mode ? '<span>收尾：' + esc(c.ending_mode) + '</span>' : ''}
        ${c.world_time_start ? '<span>世界时间：' + esc(c.world_time_start)
          + ' → ' + esc(c.world_time_end || '') + '</span>' : ''}
      </div>
      ${c.summary ? `<p class="small dim" style="margin:0 0 12px">
        <b>摘要</b>：${esc(c.summary)}</p>` : ''}
      <div class="prose sm" id="chBody">${esc(c.content || '（本章还没有正文）')}</div>`,
      `<button class="btn" onclick="closeModal()">关闭</button>
       <input type="text" id="chTitle" placeholder="章节标题" value="${esc(c.title || '')}"
         style="width:190px">
       <button class="btn" onclick="saveChapterText(${c.chapter_number})">保存修改</button>
       <button class="btn" onclick="openChapterEdit(${c.chapter_number})">编辑正文</button>
       ${c.content ? `<button class="btn ghost"
         onclick="openDiagnose(${c.chapter_number})">报错诊断</button>` : ''}
       ${vs.length ? `<button class="btn ghost"
         onclick="openChapterHistory(${c.chapter_number})">版本 ${vs.length}</button>` : ''}
       <button class="btn" onclick="togglePublish(${c.chapter_number},'${c.status}')">
         ${c.status === 'published' ? '转为草稿' : '标记发布'}</button>
       ${isLast && nEv ? `<button class="btn ghost danger"
         onclick="releaseChapter(${c.chapter_number})">退回成素材</button>` : ''}`, true);
  }).catch(e => toast(e.message, 'err'));
}

/* ------------------------------------------------------------
   章节报错诊断（v6.6）

   场景：作者读出逻辑问题，但说不清是哪一层的错——世界设定？人物设定？
   事件推理？关系？位置？这里把五层事实全部摊开让模型逐层比对，
   而不是让作者自己猜层（猜错层后面全错）。

   两条后续路径：
     a) 采纳某几条建议 → 改稿
     b) 不采纳、自己写方向 → 也改稿
   改稿结果**先预览再应用**，应用时自动留历史版本。
   ------------------------------------------------------------ */
let _diag = null;          // 最近一次诊断结果
let _revise = null;        // 最近一次改稿结果

function openDiagnose(num) {
  _diag = null; _revise = null;
  openModal(`第 ${num} 章 · 报错诊断`, `
    <p class="small" style="margin:0 0 10px;line-height:1.8">
      说出你觉得哪里不对劲就行，不用判断是哪一层的问题。
      系统会把<b>世界设定 / 人物设定 / 人物关系 / 位置与认知 / 事件推理</b>
      五层事实摊开，逐层比对后告诉你矛盾出在哪、该怎么改。</p>
    <textarea id="diagComplaint" rows="4"
      placeholder="例如：林知行这时候不该知道江海股份的事，他和苏晚晴的关系也不该这么熟"
      style="width:100%"></textarea>
    <p class="small dim" style="margin:8px 0 0">留空也可以——系统会做全层体检。</p>
    <div id="diagLog" class="small" style="margin-top:12px"></div>
    <div id="diagResult"></div>`,
    `<button class="btn" onclick="closeModal()">关闭</button>
     <button class="btn pri" id="diagGo" onclick="runDiagnose(${num})">开始诊断</button>`,
    true);
}

async function runDiagnose(num) {
  const btn = $('#diagGo');
  if (btn) btn.disabled = true;
  const complaint = ($('#diagComplaint') || {}).value || '';
  try {
    const r = await runJob('/api/chapter/diagnose',
      { novel_id: S.novelId, chapter_number: num, complaint: complaint },
      'diagLog', '逻辑诊断', async (res) => {
        _diag = res;
        renderDiagResult(num);
      });
    return r;
  } catch (e) {
    const box = $('#diagResult');
    if (box) box.innerHTML = `<div class="small" style="color:var(--bad);
      margin-top:10px">诊断失败：${esc(e.message)}</div>`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderDiagResult(num) {
  const box = $('#diagResult');
  if (!box || !_diag) return;
  const d = _diag;
  if (!d.issues.length) {
    box.innerHTML = `<div style="margin-top:14px;padding:12px 14px;
      background:var(--ok-soft);border-radius:10px;line-height:1.8">
      <b>五层比对完毕，没有发现逻辑冲突。</b>
      <div class="small" style="margin-top:6px">${esc(d.summary || '')}</div>
      ${d.verdict ? `<div class="small dim" style="margin-top:6px">${esc(d.verdict)}</div>` : ''}
    </div>`;
    return;
  }
  const m = d.materials || {};
  // 只是**兜底**：正常情况下每条问题的层名用服务端给的 layer_label
  // （唯一来源是 prompts.DIAGNOSE_LAYERS → diagnose.LAYER_LABELS）。
  // 这里曾经只写到"事件层"就停了，后加的知识边界/正史两层落到 it.layer 原样输出。
  const layerIcon = { world: '世界设定', character: '人物设定', relation: '人物关系',
    location: '位置与认知', knowledge: '知识边界', event: '事件推理',
    canon: '正史一致', unknown: '未归层' };
  box.innerHTML = `
    <div style="margin-top:14px;border-top:1px solid var(--line);padding-top:12px">
      <div class="row wrap small dim" style="gap:10px;margin-bottom:10px">
        <span>共 <b style="color:var(--text)">${d.issues.length}</b> 条</span>
        ${d.error_count ? `<span style="color:var(--bad)">硬矛盾 ${d.error_count}</span>` : ''}
        ${d.warning_count ? `<span style="color:var(--warn)">存疑 ${d.warning_count}</span>` : ''}
        <span>根因层：<b>${esc(d.suspected_root_label)}</b></span>
        <span class="spacer" style="flex:1"></span>
        <span>已查：世界 ${m.world || 0} · 人物 ${m.characters || 0}
          · 关系 ${m.relations || 0} · 位置 ${m.locations || 0}
          · 知识边界 ${m.knowledge || 0} · 事件 ${m.events || 0}
          · 正史 ${m.canon || 0}</span>
      </div>
      ${d.salvaged ? `<div class="small" style="color:var(--warn);margin-bottom:8px">
        ⚠ 这次结果是截断后抢救出来的，字段可能不全，建议重跑一次。</div>` : ''}
      ${d.summary ? `<p class="small" style="margin:0 0 10px;line-height:1.8">
        <b>总体判断</b>：${esc(d.summary)}</p>` : ''}
      ${d.verdict ? `<p class="small dim" style="margin:0 0 10px;line-height:1.8">
        ${esc(d.verdict)}</p>` : ''}
      <div id="diagIssues">
      ${d.issues.map((it, i) => `
        <div style="border:1px solid var(--line);border-radius:10px;
          padding:11px 13px;margin-bottom:9px">
          <div class="row wrap" style="gap:8px;align-items:center">
            <input type="checkbox" class="diagPick" data-i="${i}" checked
              style="width:auto;margin:0">
            <span class="tag">${esc(it.layer_label || layerIcon[it.layer] || it.layer)}</span>
            <span class="tag" style="${it.level === 'error'
              ? 'background:var(--bad-soft);color:var(--bad)'
              : 'background:var(--warn-soft);color:var(--warn)'}">
              ${it.level === 'error' ? '硬矛盾' : '存疑'}</span>
            <span class="small" style="line-height:1.7">${esc(it.problem)}</span>
          </div>
          ${it.quote ? `<div class="small" style="margin-top:7px;padding-left:10px;
            border-left:2px solid var(--line);color:var(--ink-2);line-height:1.7">
            正文原句：${esc(it.quote)}</div>` : ''}
          ${it.conflicts_with ? `<div class="small" style="margin-top:5px;
            padding-left:10px;border-left:2px solid var(--warn);line-height:1.7">
            与库中事实冲突：${esc(it.conflicts_with)}</div>` : ''}
          ${it.suggestion ? `<div class="small" style="margin-top:5px;
            padding-left:10px;border-left:2px solid var(--ok);line-height:1.7">
            <b>建议</b>：${esc(it.suggestion)}</div>` : ''}
          <input type="text" class="diagHandle" data-i="${i}"
            placeholder="不采纳？在这里写你的处理意见，会覆盖上面的建议"
            style="margin-top:7px;width:100%">
        </div>`).join('')}
      </div>
      <label class="f" style="margin-top:6px"><span>额外修改方向（可选，会一并发给模型）</span>
        <textarea id="diagDirection" rows="2"
          placeholder="例如：整体保持，但把林知行写得再迟钝一点"></textarea></label>
      <div id="reviseBox"></div>
    </div>`;
  injectModalButton('diagReviseBtn', '按选中项改稿', () => startRevise(num));
}

async function startRevise(num) {
  if (!_diag) return;
  const picks = Array.from(document.querySelectorAll('.diagPick'));
  const issues = [], handle = [];
  picks.forEach(p => {
    if (!p.checked) return;
    const i = +p.dataset.i;
    issues.push(_diag.issues[i]);
    const h = document.querySelector(`.diagHandle[data-i="${i}"]`);
    handle.push(h ? h.value : '');
  });
  const dirEl = $('#diagDirection');
  const direction = dirEl ? dirEl.value : '';
  if (!issues.length && !direction.trim()) {
    toast('至少勾选一条问题，或者写一个修改方向', 'err');
    return;
  }
  const btn = $('#diagReviseBtn');
  if (btn) btn.disabled = true;
  try {
    _revise = await runJob('/api/chapter/revise',
      { novel_id: S.novelId, chapter_number: num, issues: issues,
        handle: handle, direction: direction },
      'diagLog', '按诊断改稿', async (res) => {
        _revise = res;
        renderReviseResult(num);
      });
  } catch (e) {
    const box = $('#reviseBox');
    if (box) box.innerHTML = `<div class="small" style="color:var(--bad);
      margin-top:10px">改稿失败：${esc(e.message)}</div>`;
  } finally {
    if (btn) btn.disabled = false;
  }
}

function renderReviseResult(num) {
  const box = $('#reviseBox');
  if (!box || !_revise) return;
  const r = _revise;
  box.innerHTML = `
    <div style="margin-top:14px;border-top:1px solid var(--line);padding-top:12px">
      <div class="row wrap small dim" style="gap:10px;margin-bottom:10px">
        <span>改动 <b style="color:var(--text)">${r.changes.length}</b> 处</span>
        <span>${r.word_count_before} 字 → ${r.word_count_after} 字</span>
        ${r.salvaged ? '<span style="color:var(--warn)">⚠ 结果不完整，建议重跑</span>' : ''}
      </div>
      ${r.note ? `<p class="small" style="margin:0 0 10px;line-height:1.8;
        color:var(--warn)">${esc(r.note)}</p>` : ''}
      ${r.changes.length ? `<div style="margin-bottom:10px">
        ${r.changes.map((c, i) => `
          <div class="small" style="margin-bottom:7px;padding:9px 11px;
            background:var(--panel-2);border-radius:8px;line-height:1.7">
            <div><b>${i + 1}.</b> ${esc(c.why)}</div>
            <div style="margin-top:4px;color:var(--bad)">− ${esc(c.before)}</div>
            <div style="color:var(--ok)">+ ${esc(c.after)}</div>
          </div>`).join('')}
      </div>` : ''}
      <div class="row" style="gap:8px;margin-bottom:8px">
        <button class="btn sm" onclick="toggleReviseView(${num})" id="reviseToggle">
          对照全文</button>
        <span class="small dim" style="line-height:2">改稿尚未写入，
          满意再点「应用到本章」</span>
      </div>
      <div id="reviseCompare" style="display:none"></div>
    </div>`;
  injectModalButton('diagApplyBtn', '应用到本章', () => applyRevision(num), true);
}

/* 往弹层底部插一个按钮（插在「关闭」之前）。
   注意弹层底部容器是 #modalFoot，不是给 body 加 DOM——body 在 openModal
   里被整体重置过。重复调用只插一次。 */
function injectModalButton(id, label, onclick, danger) {
  if (document.getElementById(id)) return;
  const foot = document.getElementById('modalFoot');
  if (!foot) return;
  const b = document.createElement('button');
  b.className = 'btn pri' + (danger ? ' danger' : '');
  b.id = id;
  b.textContent = label;
  b.onclick = onclick;
  const close = foot.querySelector('button');
  foot.insertBefore(b, close || null);
}

function toggleReviseView(num) {
  const el = $('#reviseCompare');
  if (!el || !_revise) return;
  if (el.style.display === 'none') {
    el.style.display = 'block';
    el.innerHTML = `
      <div class="small dim" style="margin-bottom:6px">左边是原稿，右边是改后。
        没问题的段落应当完全一致。</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">
        <div class="prose sm" style="max-height:300px;overflow:auto;
          padding:10px;background:var(--panel-2);border-radius:8px">
          ${esc(_revise.original)}</div>
        <div class="prose sm" style="max-height:300px;overflow:auto;
          padding:10px;background:var(--ok-soft);border-radius:8px">
          ${esc(_revise.content)}</div>
      </div>`;
    const t = $('#reviseToggle'); if (t) t.textContent = '收起对照';
  } else {
    el.style.display = 'none';
    const t = $('#reviseToggle'); if (t) t.textContent = '对照全文';
  }
}

function applyRevision(num) {
  if (!_revise) return;
  confirmDialog('把改稿写入第 ' + num + ' 章？', `<p style="margin-top:0">
    改后正文会被保存，并<b>自动留一个历史版本</b>——万一不满意，
    可以在「版本」里随时恢复回来。</p>
    <p class="dim small" style="margin:0">
    ${_revise.word_count_before} 字 → ${_revise.word_count_after} 字，
    ${_revise.changes.length} 处改动。</p>`,
    '写入', async () => {
      const r = await api('/api/chapter/apply_revision', { method: 'POST',
        body: { novel_id: S.novelId, chapter_number: num,
                content: _revise.content,
                change_note: '按诊断意见修稿' } });
      toast('已写入（' + r.word_count + ' 字），旧版留档为 v'
        + (r.version || '—'), 'ok');
      _diag = null; _revise = null;
      await refreshView('chapter'); render();
    });
}

/* 一章塞进了太多事件（写成流水账）时，把它退回成"待成章素材"再按章拆分重写。
   正文会先进版本表，删了也找得回。 */
function releaseChapter(num) {
  const c = S.chapterView || {};
  const nEv = (c.source_event_ids || []).length;
  confirmDialog('把第 ' + num + ' 章退回成素材？', `<p style="margin-top:0">
    这一章的正文会先存成一个<b>历史版本</b>（版本里找得回），然后：</p>
    <ul style="margin:0 0 10px 18px;line-height:1.8">
      <li>章节行删除，章节号让出来</li>
      <li>本章用到的 <b>${nEv || '这些'}</b> 个事件回到「待成章事件」</li>
      <li>写作进度回退到第 ${Math.max(0, num - 1)} 章</li>
    </ul>
    <p class="dim small" style="margin:0">
      适合"一章塞太多事件、每件事都没写开"，退回后按每章 ${(S.candPlan || {}).per_chapter || 4}
      个事件重新分批写。</p>`,
    '退回成素材', async () => {
      const r = await api('/api/chapter/release', { method: 'POST',
        body: { novel_id: S.novelId, chapter_number: num } });
      S.candSel = null;
      toast('已退回：释放 ' + r.released_events + ' 个事件，'
        + '正文留档为 v' + (r.archived_version || '—'), 'ok');
      await refreshView('chapter'); render();
      const log = $('#writeLog'); if (log) log.innerHTML = '';
    });
}

async function saveChapterText(num) {
  try {
    const r = await api('/api/chapter/save', { method: 'POST', body: {
      novel_id: S.novelId, chapter_number: num,
      title: $('#chTitle').value } });
    toast('已保存（' + r.word_count + ' 字）', 'ok');
  } catch (e) { toast(e.message, 'err'); }
}

function openChapterEdit(num) {
  api('/api/chapter?novel_id=' + S.novelId + '&chapter=' + num).then(r => {
    const c = r.chapter;
    openModal(`编辑第${c.chapter_number}章正文`, `
      <label class="f"><span>标题</span>
        <input type="text" id="edTitle" value="${esc(c.title || '')}"></label>
      <label class="f"><span>摘要</span>
        <input type="text" id="edSummary" value="${esc(c.summary || '')}"></label>
      <label class="f"><span>正文</span>
        <textarea id="edContent" rows="18">${esc(c.content || '')}</textarea></label>`,
      `<button class="btn" onclick="closeModal()">取消</button>
       <button class="btn pri" onclick="doSaveChapter(${num})">保存</button>`, true);
  });
}

async function doSaveChapter(num) {
  try {
    const r = await api('/api/chapter/save', { method: 'POST', body: {
      novel_id: S.novelId, chapter_number: num,
      title: $('#edTitle').value, summary: $('#edSummary').value,
      content: $('#edContent').value, change_note: '手动编辑' } });
    closeModal(); toast('已保存（' + r.word_count + ' 字）', 'ok');
    await refreshView('chapter'); render();
  } catch (e) { toast(e.message, 'err'); }
}

async function togglePublish(num, cur) {
  const toPublish = cur !== 'published';
  try {
    const r = await api('/api/chapter/status', { method: 'POST', body: {
      novel_id: S.novelId, chapter_number: num,
      status: toPublish ? 'published' : 'draft' } });
    closeModal();
    if (!toPublish) {
      toast('已转为草稿', 'ok');
      await refreshView('chapter'); render();
      return;
    }
    // 发布 = 确认这一章进世界，要跑一次正文回流（花 LLM，几十秒）。
    // 任务在 /api/chapter/status 里已经提交了，这里把 job_id 接过来看进度。
    await runJob('/api/chapter/status', {}, 'chapterLog', '确认进世界',
      res => {
        const c = res.confirm || {};
        if (c.applied) {
          const bits = [];
          if ((c.world_state || []).length) bits.push('世界状态 ' + c.world_state.length + ' 项');
          if ((c.characters || []).length) bits.push('角色 ' + c.characters.length + ' 个');
          if ((c.knowledge || []).length) bits.push('认知 ' + c.knowledge.length + ' 条');
          if ((c.new_characters || []).length) bits.push('新角色 ' + c.new_characters.length + ' 个');
          toast('已发布，并确认进世界' + (bits.length ? '：' + bits.join('、') : ''), 'ok');
        } else {
          toast('已发布。' + (c.skipped || '世界未更新。'), 'ok');
        }
      }, r.job_id);
    await refreshView('chapter'); render();
  } catch (e) { toast(e.message, 'err'); }
}

function openChapterHistory(num) {
  api('/api/chapter?novel_id=' + S.novelId + '&chapter=' + num).then(r => {
    const vs = r.versions || [];
    openModal(`第${num}章 · 历史版本`, `
      ${vs.length ? `<ul class="list">${vs.map(v => `<li>
        <div class="main">
          <div class="title">v${v.version}
            <span class="dim small">${v.word_count} 字</span></div>
          <div class="sub">${esc(v.change_note || '')} ·
            ${esc(v.created_at || '')}</div>
        </div>
        <div class="acts">
          <button class="btn sm" onclick="previewVersion(${num},${v.version})">看</button>
          <button class="btn sm" onclick="restoreVersion(${num},${v.version})">恢复</button>
        </div></li>`).join('')}</ul>`
      : '<div class="empty">还没有历史版本。每次保存正文时，旧稿会自动留档。</div>'}`,
      `<button class="btn" onclick="closeModal()">关闭</button>`, true);
  });
}

async function previewVersion(num, ver) {
  const r = await api('/api/chapter?novel_id=' + S.novelId + '&chapter=' + num);
  const v = (r.versions || []).find(x => x.version === ver);
  if (!v) return;
  openModal(`v${ver} 预览`, `<div class="prose sm">${esc(v.content || '')}</div>`,
    `<button class="btn" onclick="openChapterHistory(${num})">返回</button>
     <button class="btn pri" onclick="restoreVersion(${num},${ver})">恢复这一版</button>`, true);
}

function restoreVersion(num, ver) {
  confirmDialog('恢复这一版？', `
    <p style="margin-top:0">把第 ${num} 章恢复到 v${ver}。</p>
    <p class="dim small" style="margin:0">当前内容会先自动留一份档，随时还能切回来。</p>`,
    '恢复这一版', async () => {
      const r = await api('/api/chapter/restore', { method: 'POST', body: {
        novel_id: S.novelId, chapter_number: num, version: ver } });
      toast('已恢复到 v' + ver + '（' + r.word_count + ' 字）', 'ok');
      await refreshView('chapter'); render();
    });
}

async function exportAll() {
  try {
    const r = await api('/api/chapter/export', { method: 'POST',
      body: { novel_id: S.novelId } });
    toast('已导出到 novels/ 目录', 'ok');
  } catch (e) { toast(e.message, 'err'); }
}

/* ------------------------------------------------------------
   视图四：设置
   ------------------------------------------------------------ */
function renderManage(fromEmpty) {
  const provs = S.providers || [];
  const slots = S.slots || [];
  const h = S.health || {};
  const voice = S.voice || [];
  const seeds = S.seeds || [];
  const arch = S.archived || [];
  const n = S.novel || {};
  const st = S.stats || {};
  const vc = S.meta.voice_categories || {};
  const stypes = S.meta.seed_types || {};

  const htmlNoNovel = `
    <div class="card" style="margin-bottom:16px">
      <h3>AI 服务商</h3>
      <div class="body">
        <p class="dim small" style="margin-top:0">
          密钥保存在 <span class="mono">${esc(S.secretsPath || '')}</span>，
          不写进数据库。</p>
        ${provCards(provs, slots)}
      </div>
    </div>
    ${archivedCard()}
    <div class="card">
      <h3>数据库</h3>
      <div class="body">
        <div class="kv">
          <span class="k">位置</span><span class="mono">${esc(h.db || '')}</span>
          <span class="k">完整性</span><span>${esc(h.integrity || '—')}</span>
          <span class="k">外键断裂</span><span>${h.foreign_keys_broken || 0} 行</span>
          <span class="k">表数</span><span>${h.table_count || 0}</span>
          <span class="k">版本</span><span>${h.version || 0}</span>
        </div>
      </div>
    </div>`;

  if (fromEmpty || !S.novelId) { $('#v-manage').innerHTML = htmlNoNovel; return; }

  $('#v-manage').innerHTML = `
    ${provCards(provs, slots)}

    <div class="card" style="margin-bottom:16px">
      <h3>模型档位 <span class="spacer"></span>
        <span class="dim small" style="font-weight:400">
          不同任务用不同档次的模型，能显著省钱</span>
        <button class="btn sm" onclick="openBulkSlots()">批量配置</button></h3>
      <div class="body tight">
        <table class="t"><thead><tr>
          <th>档位</th><th>用途</th><th>服务商</th><th>模型</th><th>温度</th><th>状态</th>
          <th></th></tr></thead><tbody>
          ${slots.map(s => `<tr>
            <td><b>${esc(s.label)}</b><div class="dim small mono">${esc(s.slot)}</div></td>
            <td class="dim small">${esc(SLOT_DESC[s.slot] || '')}</td>
            <td class="small">${esc((provs.find(p => p.id === s.provider_id) || {}).name
              || '<未指定>')}</td>
            <td class="mono">${esc(s.model || '—')}</td>
            <td class="mono">${s.temperature}</td>
            <td>${s.ok ? '<span class="tag ok">就绪</span>'
              : '<span class="tag warn">' + esc(s.reason || '未配置') + '</span>'}</td>
            <td style="text-align:right"><button class="btn sm ghost"
              onclick="openSlotEdit('${s.slot}')">配置</button></td>
          </tr>`).join('')}</tbody></table>
      </div>
    </div>

    <div class="grid g2" style="margin-bottom:16px">
      <div class="card">
        <h3>作者癖好 <span class="spacer"></span>
          <button class="btn sm" onclick="openVoiceEdit()">＋ 条目</button></h3>
        <div class="body">
          <p class="dim small" style="margin-top:0">
            写正文时会注入这些偏好，用来固定你的个人语感。「禁忌」项对推演也有约束力。</p>
          ${voice.length ? Object.keys(vc).map(cat => {
            const items = voice.filter(v => v.category === cat);
            if (!items.length) return '';
            return `<div style="margin-bottom:10px">
              <div class="small dim" style="margin-bottom:5px">${esc(vc[cat])}</div>
              ${items.map(v => `<div class="row" style="align-items:flex-start;
                padding:5px 0;border-bottom:1px solid var(--line-2)">
                <div style="flex:1">
                  <span ${v.is_active ? '' : 'class="dim" style="text-decoration:line-through"'}>
                    ${esc(v.content)}</span>
                  ${v.example ? '<div class="dim small">例：' + esc(v.example) + '</div>' : ''}
                </div>
                <button class="btn sm ghost" onclick="toggleVoice(${v.id},${v.is_active ? 0 : 1})">
                  ${v.is_active ? '停用' : '启用'}</button>
                <button class="btn sm ghost danger" onclick="delVoice(${v.id})">删</button>
              </div>`).join('')}</div>`;
          }).join('') : `<div class="empty">还没有条目。比如「不写『他松了口气』」
            「对话里少用感叹号」</div>`}
        </div>
      </div>

      <div class="card">
        <h3>事件种子 <span class="spacer"></span>
          <button class="btn sm" onclick="openSeedEdit()">＋ 种子</button></h3>
        <div class="body">
          <p class="dim small" style="margin-top:0">
            种子是「条件满足时可以长成事件」的胚芽，给世界提供自发的动力。</p>
          ${seeds.length ? seeds.map(s => `<div class="row"
            style="align-items:flex-start;padding:6px 0;border-bottom:1px solid var(--line-2)">
            <div style="flex:1">
              <div><b>${esc(s.name)}</b>
                <span class="tag">${esc(stypes[s.seed_type] || s.seed_type)}</span>
                <span class="dim small">强度 ${s.intensity}/5 · 已用 ${s.used_count} 次</span>
                ${s.is_active ? '' : '<span class="tag bad">已停用</span>'}</div>
              <div class="dim small">${esc(s.description || '')}</div>
            </div></div>`).join('')
            : '<div class="empty">还没有种子。</div>'}
        </div>
      </div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <h3>完结条件 <span class="spacer"></span>
        <span class="dim small" style="font-weight:400">
          当前方式：${esc({ai: 'AI 自动判定', goal: '达成设定目标', both: '两者都要'
            }[S.completionMode] || S.completionMode || '—')}</span>
        <button class="btn sm" onclick="openCompGoalEdit()">＋ 目标</button>
        <button class="btn sm" onclick="openCompModeEdit()">改方式</button></h3>
      <div class="body tight">
        ${(S.compGoals || []).length ? `<table class="t"><thead><tr>
          <th>目标</th><th>类型</th><th>达成条件</th><th>状态</th><th></th>
          </tr></thead><tbody>
          ${S.compGoals.map(g => `<tr>
            <td><b>${esc(g.title)}</b>${g.is_primary
              ? ' <span class="tag accent">主要</span>' : ''}
              ${g.description ? '<div class="dim small">' + esc(g.description) + '</div>' : ''}</td>
            <td><span class="tag">${esc(g.goal_type)}</span></td>
            <td class="mono small">${esc(g.condition_expr || '（无，靠 AI 判断）')}</td>
            <td>${goalStatus(g.status)}</td>
            <td style="text-align:right">
              <button class="btn sm ghost"
                onclick="markGoal(${g.id},'achieved')">标记达成</button></td>
          </tr>`).join('')}</tbody></table>`
          : `<div class="empty">还没有设定完结条件。可以是世界线目标，
            也可以是某个角色的目标。<br>
            <span class="mono small">例：world.异常实体.剩余数量 == 0</span></div>`}
      </div>
      <div class="body" style="border-top:1px solid var(--line-2)">
        <button class="btn pri" onclick="checkCompletion()">现在判定一次是否该完结</button>
        <div id="compOut" style="margin-top:12px"></div>
      </div>
    </div>

    ${novelManageCard()}
    ${archivedCard()}

    <div class="card">
      <h3>数据库</h3>
      <div class="body">
        <div class="kv">
          <span class="k">位置</span><span class="mono">${esc(h.db || '')}</span>
          <span class="k">完整性</span><span>${esc(h.integrity || '—')}</span>
          <span class="k">外键断裂</span><span>${h.foreign_keys_broken || 0} 行</span>
          <span class="k">表数</span><span>${h.table_count || 0}</span>
          <span class="k">版本</span><span>${h.version || 0}</span>
        </div>
        <hr class="sep">
        <div class="row">
          <button class="btn" onclick="runHealth()">重新体检</button>
          <button class="btn" onclick="purgeOrphans()">清理残留数据</button>
          <span class="dim small">残留数据 = 指向已删世界的孤儿行</span>
        </div>
      </div>
    </div>
  `;
}

/* ---- 设置：世界管理卡片（当前世界 + 归档箱） ---- */
function novelManageCard() {
  const n = S.novel || {};
  const st = S.stats || {};
  return `
    <div class="card" style="margin-bottom:16px">
      <h3>世界管理 <span class="spacer"></span>
        <span class="dim small" style="font-weight:400">当前世界</span>
        <button class="btn sm" onclick="openNovelEdit()">编辑设定</button>
        <button class="btn sm ghost" onclick="exportAll()">导出章节</button>
        <button class="btn sm ghost danger" onclick="openDeleteNovel()">删除这个世界</button></h3>
      <div class="body">
        <div class="kv">
          <span class="k">书名</span><span><b>${esc(n.title || '')}</b></span>
          <span class="k">进度</span><span>${st.chapter_count || 0} 章 ·
            ${(st.total_words || 0).toLocaleString('zh-CN')} 字 ·
            ${st.character_count || 0} 角色 · ${st.event_count || 0} 事件</span>
          <span class="k">世界时间</span><span>${esc((S.clock || {}).current_time || '未设定')}</span>
        </div>
        <p class="dim small" style="margin:10px 0 0">
          删除有两条路：<b>归档</b>只是从列表里藏起来，数据原样留在库里、随时可以恢复；
          <b>彻底删除</b>会把章节、角色、事件一并清空，删前系统会尽力把正文导出到
          <span class="mono">novels/</span> 目录留底。</p>
      </div>
    </div>`;
}

function archivedCard() {
  const arch = S.archived || [];
  if (!arch.length) return '';
  return `
    <div class="card" style="margin-bottom:16px">
      <h3>归档箱 <span class="spacer"></span>
        <span class="dim small" style="font-weight:400">
          ${arch.length} 个世界 · 数据都还在库里</span></h3>
      <div class="body">
        ${arch.map(a => `<div class="row"
          style="padding:7px 0;border-bottom:1px solid var(--line-2)">
          <div style="flex:1;min-width:0"><b>${esc(a.title)}</b>
            <div class="dim small">${a.chapter_count || 0} 章 ·
              ${(a.total_words || 0).toLocaleString('zh-CN')} 字 ·
              归档于 ${esc((a.deleted_at || '').slice(0, 16))}</div></div>
          <div class="acts">
            <button class="btn sm ghost" onclick="restoreNovel(${a.id})">恢复</button>
            <button class="btn sm ghost danger"
              onclick="openDeleteNovel(${a.id})">彻底删除</button>
          </div>
        </div>`).join('')}
      </div>
    </div>`;
}

// 档位说明。键必须与后端 llm/router.py 的 SLOT_LABELS 完全一致——
// 批量配置界面遍历的是这里的 key，少一个档位用户就永远配不到它
// （曾经只列了 7 个，漏掉 diagnose / conflict_check / world_init）。
const SLOT_DESC = {
  world_sim: '推演世界发生了什么（高频，成本命门）',
  character_decide: '角色自己拿主意',
  option_gen: '给用户生成可选分支',
  prose_gen: '把事件写成正文（最值得花钱）',
  state_extract: '从文本里抽状态变化',
  completion_judge: '判断是否该完结',
  summarize: '写章节摘要',
  diagnose: '诊断正文与设定的逻辑冲突',
  conflict_check: '正文与正史冲突时的语义级兜底判定',
  world_init: '按选定题材生成整份世界草案（新建世界）',
};

function provCards(provs, slots) {
  // 同一 Base URL 配了多条 = 多半是重复添加，指出来免得用户以为"越加越多"
  const urlCount = {};
  provs.forEach(p => {
    const u = (p.base_url || '').replace(/\/+$/, '');
    if (u) urlCount[u] = (urlCount[u] || 0) + 1;
  });
  return `
  <div class="card" style="margin-bottom:16px">
    <h3>AI 服务商 <span class="spacer"></span>
      <span class="dim small" style="font-weight:400">
        密钥存于 ${esc(S.secretsPath || '')}，不进数据库</span>
      <button class="btn sm" onclick="openProviderEdit()">＋ 服务商</button></h3>
    <div class="body">
      ${provs.length ? provs.map(p => {
        const u = (p.base_url || '').replace(/\/+$/, '');
        const dup = u && urlCount[u] > 1;
        return `<div class="row"
        style="align-items:flex-start;padding:8px 0;border-bottom:1px solid var(--line-2)">
        <div style="flex:1;min-width:0">
          <div><b>${esc(p.name)}</b> <span class="tag">${esc(p.kind)}</span>
            ${p.preset_key ? '<span class="tag">' + esc(p.preset_key) + '</span>' : ''}
            ${p.has_key ? '<span class="tag ok">已配密钥</span>'
              : (p.kind === 'ollama' ? '' : '<span class="tag warn">缺密钥</span>')}
            ${(p.models || []).length
              ? '<span class="tag">' + p.models.length + ' 个模型</span>'
              : '<span class="tag warn">未登记模型</span>'}
            ${dup ? '<span class="tag warn">与其它条同址</span>' : ''}</div>
          <div class="dim small mono">${esc(p.base_url || '')}</div>
          ${(p.models || []).length ? `<div class="dim small mono"
            style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
            ${esc(p.models.slice(0, 6).join(' · '))}${
              p.models.length > 6 ? ' …' : ''}</div>` : ''}
        </div>
        <div class="acts">
          <button class="btn sm" onclick="testProvider(${p.id})">测试</button>
          <button class="btn sm ghost" onclick="openProviderEdit(${p.id})">改</button>
          <button class="btn sm ghost danger" onclick="openDeleteProvider(${p.id})">删</button>
        </div>
      </div>`; }).join('')
        : '<div class="empty">还没有配置服务商。<br>支持 DeepSeek / 通义 / Kimi / 智谱 / OpenAI / Claude / Gemini / Ollama 等 13 家。</div>'}
    </div>
  </div>`;
}

function goalStatus(s) {
  const m = { active: '<span class="tag">进行中</span>',
    achieved: '<span class="tag ok">已达成</span>',
    failed: '<span class="tag bad">已失败</span>',
    abandoned: '<span class="tag">已放弃</span>' };
  return m[s] || s;
}

/* ---- 设置：动作 ---- */
async function runHealth() {
  try { S.health = await api('/api/health');
    toast(S.health.ok ? '体检正常' : '发现问题', S.health.ok ? 'ok' : 'err');
    render();
  } catch (e) { toast(e.message, 'err'); }
}
async function purgeOrphans() {
  try {
    const r = await api('/api/health/purge', { method: 'POST', body: {} });
    S.health = r.health;
    toast('清理了 ' + r.total + ' 行残留', 'ok'); render();
  } catch (e) { toast(e.message, 'err'); }
}
async function toggleVoice(id, v) {
  try { await api('/api/voice/toggle', { method: 'POST', body: { id, is_active: !!v } });
    await loadManage(); render();
  } catch (e) { toast(e.message, 'err'); }
}
function delVoice(id) {
  const v = (S.voice || []).find(x => x.id === id) || {};
  confirmDialog('删除这条作者癖好', `
    <p style="margin-top:0">${esc(v.content || '')}</p>
    <p class="dim small" style="margin:0">删掉后，写正文时不会再注入这条偏好。</p>`,
    '确认删除', async () => {
      await api('/api/voice/delete', { method: 'POST', body: { id } });
      await loadManage(); render();
      toast('已删除', 'ok');
    });
}
async function markGoal(id, status) {
  try { await api('/api/completion/goal/update', { method: 'POST',
    body: { id, fields: { status } } });
    await loadManage(); render(); toast('已更新', 'ok');
  } catch (e) { toast(e.message, 'err'); }
}
async function checkCompletion() {
  const el = $('#compOut');
  el.innerHTML = '<div class="log"><div class="l">正在判定…</div></div>';
  try {
    const r = await api('/api/completion/check', { method: 'POST',
      body: { novel_id: S.novelId } });
    const v = r.result.ai_verdict || {};
    el.innerHTML = `<div class="opt" style="cursor:default">
      <div class="lb">${r.result.should_complete
        ? '<span class="tag ok">可以完结</span>' : '<span class="tag">尚未到终点</span>'}
        <span class="dim small">方式：${esc(r.result.mode)}</span></div>
      <div class="ds">${esc(r.result.reason || '')}</div>
      ${v.q1_tension ? `<div class="ch">张力：${esc(v.q1_tension)}</div>
        <div class="ch">目标：${esc(v.q2_goals || '')}</div>
        <div class="ch">重复感：${esc(v.q3_repetition || '')}</div>
        <div class="ch">置信度：${esc(v.confidence || '')}</div>` : ''}
      ${v.error ? '<div class="ch" style="color:var(--bad)">' + esc(v.error) + '</div>' : ''}
    </div>`;
  } catch (e) {
    el.innerHTML = '<div class="log"><div class="l err">' + esc(e.message) + '</div></div>';
  }
}

/* ============================================================
   各类编辑弹层
   ============================================================ */
function field(label, id, val, ph) {
  return `<label class="f"><span>${label}</span>
    <input type="text" id="${id}" value="${esc(val || '')}"
      placeholder="${esc(ph || '')}"></label>`;
}
function area(label, id, val, ph, hint) {
  return `<label class="f"><span>${label}</span>
    <textarea id="${id}" rows="3" placeholder="${esc(ph || '')}">${esc(val || '')}</textarea>
    ${hint ? `<span class="small dim" style="font-weight:400">${esc(hint)}</span>` : ''}</label>`;
}

/* ---- 新建 / 编辑世界 ---- */

/* 顶栏世界下拉的统一填充入口。
   boot / 新建 / 删除 / 进设置页都走这里，避免多处各写一份导致显示口径不一致。 */
function fillNovelSelect() {
  const sel = $('#novelSel');
  if (!sel) return;
  sel.innerHTML = (S.novels || []).length
    ? S.novels.map(n => `<option value="${n.id}">${esc(n.title)}（${
        n.chapter_count || 0}章 · ${n.pending_decision_count || 0}待决）</option>`).join('')
    : '<option value="0">（还没有世界）</option>';
  if (S.novelId) sel.value = String(S.novelId);
}

/* 切换/删除世界时清掉上一部作品的缓存，避免串味 */
function resetNovelCache() {
  ['novel', 'stats', 'clock', 'state', 'entities', 'characters', 'threads',
   'goals', 'decisions', 'chapters', 'voice', 'seeds', 'compGoals',
   'completion', 'urgent', 'candidates', 'casting', 'events', 'relations',
   'timeline'
  ].forEach(k => { S[k] = Array.isArray(S[k]) ? [] : null; });
  S.searchKw = '';
  S.curChapter = 0;
}

/* ============================================================ 创建世界（v6.8）
 *
 * 流程（三步，只有最后一步落库）：
 *   ① 从热门题材里挑一个（可联网刷新）→ 或自己填题材
 *   ② 写上额外要求 → 点「AI 生成整个世界」→ 草案回填进下面的表单（全部可改）
 *   ③ 「下一步：组建阵容」才真正建库（走 /api/genre/world_apply）
 *
 * 两条设计纪律：
 *
 * 1. **AI 生成的是"草案"，不是"成品"。** 回填之后所有字段都能改，
 *    用户改完再创建。直接建库再让他去改世界页，等于把"确认"这一步跳过了。
 *
 * 2. **弹层每次重绘前必须 nwSnapshot()。** 点页签、选题材、联网刷新都会重绘
 *    modalBody，而 innerHTML 一换，用户敲的字就没了。这本项目已经因为
 *    "重绘丢状态"踩过好几次（见 MEMORY 的展示层铁律），新建世界又是最长的一张表单。
 */

function nwBlankForm() {
  return { title: '', genre: '', tone: '', premise: '', tension: '',
           state: '', time: '', mode: 'director',
           tags: [], rules: [], designNote: '', genreKey: '' };
}

/* 把当前 DOM 里的值收进 S.nwForm。**任何重绘前都要先调它。** */
function nwSnapshot() {
  if (!S.nwForm) S.nwForm = nwBlankForm();
  const f = S.nwForm;
  const val = id => { const el = $('#' + id); return el ? el.value : null; };
  [['nwTitle', 'title'], ['nwGenre', 'genre'], ['nwTone', 'tone'],
   ['nwPremise', 'premise'], ['nwTension', 'tension'],
   ['nwState', 'state'], ['nwTime', 'time'], ['nwMode', 'mode'],
  ].forEach(([id, k]) => { const v = val(id); if (v !== null) f[k] = v; });
  const h = val('nwHint');
  if (h !== null) S.nwHint = h;
  // 规则是逐条 DOM 节点，按 sh 里的条数读回来
  (f.rules || []).forEach((r, i) => {
    const c = val('nwRule_c' + i), s = val('nwRule_s' + i), v = val('nwRule_v' + i);
    if (c !== null) r.content = c;
    if (s !== null) r.scope = s;
    if (v !== null) r.severity = v;
  });
  return f;
}

function openNewNovel() {
  if (!S.nwForm) S.nwForm = nwBlankForm();
  openModal('创建新世界（第 1 步 / 共 2 步）',
    '<div id="nwBody"></div><div id="nwJobLog"></div>',
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn" id="nwGenBtn" onclick="genWorldDraft()">AI 生成整个世界</button>
     <button class="btn pri" onclick="doNewNovel()">下一步：组建阵容</button>`,
    true);
  paintNewNovel();
  if (!S.nwGenre.list.length) loadGenreTrends(false);
}

function paintNewNovel() {
  const host = $('#nwBody');
  if (!host) return;
  const f = S.nwForm || nwBlankForm();
  const G = S.nwGenre;
  const sel = (G.list || []).find(g => g.key === G.sel) || null;
  const draft = S.nwDraft;

  host.innerHTML = `
    <div class="card" style="margin-bottom:16px">
      <h3>① 挑一个题材<span class="spacer"></span>
        <span class="dim small" style="font-weight:400">数据来自网络热门榜单</span>
      </h3>
      <div class="body">${nwGenreHtml()}${sel ? nwGenreDetailHtml(sel) : ''}</div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <h3>② 补充你的要求（可选）</h3>
      <div class="body">
        <p class="dim small" style="margin-top:0">
          想让 AI 在这个题材里偏成什么样，写在这里。它会**优先于**题材的通用套路。</p>
        ${area('创作提示', 'nwHint', S.nwHint,
          '例如：主角是个不想上班的法医／要有一个内部叛徒／不要超能力／基调要冷',
          '现在先选一个题材，再点「AI 生成整个世界」')}
        ${draft ? `<div class="small" style="margin-top:10px;background:var(--ok-soft);
            color:var(--ok);border-radius:8px;padding:9px 12px;line-height:1.7">
          ✓ 草案已回填到下面。**所有字段都能改**，改完点「下一步」才真正建库。
          ${draft.design_note ? '<br>AI 的说明：' + esc(draft.design_note) : ''}
        </div>` : ''}
        ${(draft && (draft.warnings || []).length) ? `<div class="small"
            style="margin-top:8px;background:var(--warn-soft);color:var(--warn);
            border-radius:8px;padding:9px 12px;line-height:1.7">
          AI 给的东西有几处需要你留意：<br>
          ${draft.warnings.map(w => '· ' + esc(w)).join('<br>')}
        </div>` : ''}
      </div>
    </div>

    <div class="card">
      <h3>③ 世界设定<span class="spacer"></span>
        <span class="dim small" style="font-weight:400">手填也行，不必用 AI</span>
      </h3>
      <div class="body">
        <p class="dim small" style="margin-top:0">
          世界先定下来，下一步再定谁在里面。<br>
          之所以要先有人：推演是「角色为了目标去行动」，世界里没有人，
          模型每轮都会即兴捏一批人，捏完不留档，下一轮又是另一批。</p>
        ${field('书名', 'nwTitle', f.title, '例如：异常现象调查局')}
        <div class="grid g2">
          <div>${field('题材', 'nwGenre', f.genre, '悬疑 / 科幻 / 玄幻')}</div>
          <div>${field('基调', 'nwTone', f.tone, '冷峻 / 诙谐 / 悲悯')}</div>
        </div>
        ${area('世界前提', 'nwPremise', f.premise,
          '这个世界是什么样，正在发生什么')}
        ${area('核心张力', 'nwTension', f.tension,
          '什么东西在持续拉扯，让故事有动力')}
        ${area('初始世界状态（可选，一行一条）', 'nwState', f.state,
          '例如：异常实体.剩余数量 = 7\n调查局.剩余编制 = 12\n每行「主体.谓词 = 值」，推演会拿它当硬事实')}
        ${nwRulesHtml(f)}
        <div class="grid g2">
          <div>${field('起始世界时间', 'nwTime', f.time, '第三纪元 214 年 霜月 1 日')}</div>
          <div><label class="f"><span>人机分工</span>
            <select id="nwMode">
              <option value="director"${f.mode === 'director' ? ' selected' : ''}>导演 — 主角归你，其余 AI</option>
              <option value="viewer"${f.mode === 'viewer' ? ' selected' : ''}>观影 — 全 AI 决策，你只看</option>
              <option value="tabletop"${f.mode === 'tabletop' ? ' selected' : ''}>跑团 — 全部你来定</option>
            </select></label></div>
        </div>
      </div>
    </div>`;
}

/* 世界规则逐条编辑。
   为什么不做成一个 textarea 一行一条：规则除了内容还有范围与严重度，
   挤进一行就得自定义分隔符，用户写错一次就静默丢一条。
   一条规则一个 DOM 节点，项目里这条铁律是有原因的。 */
function nwRulesHtml(f) {
  const rows = (f.rules || []).map((r, i) => `
    <div class="row" style="margin-bottom:7px;align-items:flex-start">
      <div style="flex:1">
        <input type="text" id="nwRule_c${i}" value="${esc(r.content || '')}"
          placeholder="规则内容，如：死人不会复活">
      </div>
      <div style="width:130px">
        <input type="text" id="nwRule_s${i}" value="${esc(r.scope || 'global')}"
          placeholder="适用范围"></div>
      <div style="width:104px">
        <select id="nwRule_v${i}">
          <option value="error"${r.severity !== 'warning' ? ' selected' : ''}>error</option>
          <option value="warning"${r.severity === 'warning' ? ' selected' : ''}>warning</option>
        </select></div>
      <button class="btn ghost" title="删掉这条"
        onclick="nwDelRule(${i})">×</button>
    </div>`).join('');
  return `<div style="margin-bottom:12px">
    <div class="row" style="margin-bottom:6px">
      <span class="small dim" style="flex:1">世界规则（可选）——这个世界的物理定律，
        推演时会当铁律摆给模型。写成可判定的陈述句。</span>
      <button class="btn sm" onclick="nwAddRule()">＋ 加一条</button>
    </div>
    ${rows || '<div class="small dim" style="padding:6px 0">还没有规则。</div>'}
  </div>`;
}

function nwAddRule() {
  nwSnapshot();
  if (!S.nwForm.rules) S.nwForm.rules = [];
  S.nwForm.rules.push({ content: '', scope: 'global', severity: 'error' });
  paintNewNovel();
}

function nwDelRule(i) {
  nwSnapshot();
  (S.nwForm.rules || []).splice(i, 1);
  paintNewNovel();
}

/* ---- 题材区 ---- */

function nwGenreHtml() {
  const G = S.nwGenre;
  if (G.loading) {
    return `<div class="empty">正在读取热门题材…</div>`;
  }
  if (G.err) {
    return `<div class="small" style="background:var(--warn-soft);color:var(--warn);
      border-radius:8px;padding:10px 12px;line-height:1.7">
      ${esc(G.err)}<br>不影响使用——你可以在下面直接自己填题材。</div>`;
  }
  if (!G.list.length) {
    return `<div class="empty">没有读到题材列表。可以直接在下面自己填题材。</div>`;
  }
  const tabs = (G.audiences || []).map(a => `
    <button class="btn${G.audience === a.key ? ' pri' : ''}"
      onclick="setGenreAudience('${a.key}')">${esc(a.label)}${
      a.key ? ' ' + (a.count || 0) : ''}</button>`).join('');
  const src = G.source || {};
  const cls = { live: 'ok', cache: 'warn', builtin: '', error: 'bad' }[src.kind] || '';
  const list = (G.list || []).filter(g => !G.audience
    || g.audience === G.audience || g.audience === 'both');
  return `
    <div class="gtabs" style="margin-bottom:9px">
      ${tabs}
      <span style="flex:1"></span>
      <button class="btn sm" onclick="loadGenreTrends(true)"
        ${G.refreshing ? 'disabled' : ''}>${G.refreshing ? '正在联网…' : '⟳ 联网刷新'}</button>
    </div>
    <div class="gsrc" style="margin-bottom:9px">
      <span class="tag ${cls}">${esc(src.label || '—')}</span>
      ${src.at ? '数据时间 ' + esc(src.at) + '　' : ''}
      ${esc(src.note || '')}
      ${(src.report || []).filter(r => !r.ok).map(r =>
        '<br>· ' + esc(r.name) + '：' + esc(r.error || '失败')).join('')}
    </div>
    <div class="ggrid">
      ${list.map(g => `
        <div class="opt gcard${G.sel === g.key ? ' chosen' : ''}"
             onclick="pickGenre('${esc(g.key)}')">
          <div class="nm">${esc(g.name)}
            ${G.sel === g.key ? '<span class="tag ok">已选</span>' : ''}</div>
          <div class="ht">
            <span>${esc(g.heat_label)}</span>
            <span class="dim">${g.heat_is_live
              ? '实时' : '内置'}</span>
            <span class="dim">${(g.platforms || []).join('·')}</span>
          </div>
          <div class="heatbar${g.heat_is_live ? '' : ' base'}">
            <i style="width:${g.heat_pct}%"></i></div>
        </div>`).join('')}
    </div>`;
}

function nwGenreDetailHtml(g) {
  const li = (arr, cls) => (arr || []).filter(Boolean)
    .map(x => `<li${cls ? ' class="' + cls + '"' : ''}>${esc(x)}</li>`).join('');
  return `<div class="gdetail">
    <div class="row" style="margin-bottom:8px">
      <b style="font-size:14px">${esc(g.name)}</b>
      ${g.heat_is_live
        ? `<span class="tag ok">实时 ${esc(g.heat_label)}</span>
           <span class="dim small">取自标签「${esc(g.heat_tag)}」</span>`
        : `<span class="tag">内置基准 ${esc(g.heat_label)}</span>`}
      <span style="flex:1"></span>
      <span class="dim small">${(g.tropes || []).length} 个套路 ·
        ${(g.opening_hooks || []).length} 个钩子</span>
    </div>
    ${g.why_hot ? `<div class="small" style="color:var(--ink-2);margin-bottom:10px">
      为什么现在有人在看：${esc(g.why_hot)}</div>` : ''}
    ${g.heat_note ? `<div class="gsrc" style="margin-bottom:10px">
      热度说明：${esc(g.heat_note)}</div>` : ''}
    <div class="grid g2">
      <div>
        <h4>这个题材必须先定死的几条轴</h4>
        <ul>${li(g.world_axes)}</ul>
        <h4>读者认的套路</h4>
        <ul>${li(g.tropes)}</ul>
      </div>
      <div>
        <h4>开篇钩子参考</h4>
        <ul>${li(g.opening_hooks)}</ul>
        <h4>该避免的坑</h4>
        <ul>${li(g.pitfalls, 'warn')}</ul>
      </div>
    </div>
    ${(g.examples || []).length ? `<div class="gsrc">同类作品：${
      g.examples.map(esc).join('、')}</div>` : ''}
  </div>`;
}

function setGenreAudience(k) {
  nwSnapshot();
  S.nwGenre.audience = k || '';
  paintNewNovel();
}

function pickGenre(key) {
  nwSnapshot();
  const g = (S.nwGenre.list || []).find(x => x.key === key);
  if (!g) return;
  S.nwGenre.sel = (S.nwGenre.sel === key) ? null : key;
  const f = S.nwForm;
  if (S.nwGenre.sel) {
    // 题材名直接填进去（用户仍可改）；题材是必填的，不能等他手打
    f.genre = g.name;
    f.genreKey = g.key;
    if (!f.tone) f.tone = (g.audience === 'female' ? '细腻' : '冷峻');
  }
  paintNewNovel();
}

async function loadGenreTrends(refresh) {
  const G = S.nwGenre;
  if (G.loading || G.refreshing) return;
  if (refresh) {
    nwSnapshot();
    G.refreshing = true;
    G.err = '';
    paintNewNovel();
  } else {
    G.loading = true;
    paintNewNovel();
  }
  try {
    const q = refresh ? '?refresh=1' : '';
    const r = await api('/api/genre/trends' + q);
    G.list = r.genres || [];
    G.source = r.source || null;
    G.audiences = r.audiences || [];
    if (!G.list.length && !G.err) {
      G.err = (r.source && r.source.note) || '没有读到题材列表。';
    }
  } catch (e) {
    G.err = '读取热门题材失败：' + e.message;
  } finally {
    G.loading = false;
    G.refreshing = false;
    paintNewNovel();
  }
}

/* ---- 生成世界草案 ---- */

async function genWorldDraft() {
  nwSnapshot();
  const f = S.nwForm;
  const sel = (S.nwGenre.list || []).find(g => g.key === S.nwGenre.sel) || null;
  // 用户可能选了题材又手改过题材名——以表单里的文本为准
  const genreName = (f.genre || '').trim();
  if (!genreName && !S.nwGenre.sel) {
    return toast('先挑一个题材，或在「题材」里自己填一个', 'err');
  }
  const btn = $('#nwGenBtn');
  const label = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '正在生成…'; }
  // runJob 失败时**返回 null 而不是抛异常**（它内部已经 toast 过了）。
  // 所以这里必须按返回值判断并把按钮恢复——不然失败一次之后按钮永远是
  // disabled，用户再点毫无反应，正是本项目最怕的那种失败形态。
  const restore = () => { if (btn) { btn.disabled = false; btn.textContent = label; } };
  try {
    // 把当前作品 id 一并带上：新建作品时 S.novelId 为空 → 传 0，后端走全局档位；
    // 给已有作品重新生成世界时必须传该作品 id，否则「世界构建」档位不会生效
    // （后端会读到全局行并静默降级到全局 world_sim，表现为"总是用第一个模型"）。
    const r = await runJob('/api/genre/world_draft',
      { genre_key: S.nwGenre.sel || '', genre: genreName, hint: S.nwHint || '',
        novel_id: S.novelId || 0 },
      'nwJobLog', '世界构建');
    if (!r) return restore();          // 失败：runJob 已经报过错了
    nwApplyDraft(r);
    restore();
  } catch (e) {
    toast(e.message, 'err');
    restore();
  }
}

/* 把草案回填进表单。**全部可改**——这一步只是省去用户打字，不是替他做决定。 */
function nwApplyDraft(d) {
  if (!d) return;
  const f = S.nwForm || (S.nwForm = nwBlankForm());
  if (d.title) f.title = d.title;
  if (d.genre) f.genre = d.genre;
  if (d.tone) f.tone = d.tone;
  if (d.premise) f.premise = d.premise;
  if (d.initial_tension) f.tension = d.initial_tension;
  if (d.world_time) f.time = d.world_time;
  // 世界状态：折回「键 = 值」的一行一条（表单里就是这个形态）
  if ((d.world_state || []).length) {
    f.state = d.world_state
      .map(s => `${s.key} = ${s.value}`).join('\n');
  }
  if ((d.rules || []).length) {
    f.rules = d.rules.map(r => ({
      content: r.content || '', scope: r.scope || 'global',
      severity: r.severity === 'warning' ? 'warning' : 'error',
      check_hint: r.check_hint || '', is_hard: r.is_hard ? 1 : 0,
    }));
  }
  f.tags = d.tags || [];
  f.designNote = d.design_note || '';
  f.genreKey = d.genre_key || f.genreKey || '';
  S.nwDraft = d;
  paintNewNovel();
  toast('世界草案已生成，请过一遍再创建', 'ok');
}

/* ---- 落库（走 world_apply；建书 + 状态 + 规则一次写完）---- */

async function doNewNovel() {
  nwSnapshot();
  const f = S.nwForm;
  const title = (f.title || '').trim();
  if (!title) return toast('书名不能为空', 'err');

  // 初始世界状态：每行「主体.谓词 = 值」。这是用户唯一能"预先定死"世界数字的地方——
  // 不填也能跑（推演会自己长出来），但填了推演第一轮就有硬约束可用。
  const worldState = [];
  const badKeys = [];
  (f.state || '').split('\n').forEach(line => {
    const s = line.trim();
    if (!s || s.indexOf('=') < 0) return;
    const i = s.indexOf('=');
    const key = s.slice(0, i).trim();
    const raw = s.slice(i + 1).trim();
    if (!key || !raw) return;
    let value = raw, value_type = 'text';
    if (/^-?\d+$/.test(raw)) { value = parseInt(raw, 10); value_type = 'int'; }
    else if (/^-?\d*\.\d+$/.test(raw)) { value = parseFloat(raw); value_type = 'float'; }
    worldState.push({ key, value, value_type, category: 'other' });
    // 键必须带主体。世界状态是「主体.谓词 → 值」，缺主体推演读不出来。
    // 这里不拦（用户可能就想先随便记一笔），但要告诉他哪几条不对。
    if (key.indexOf('.') < 0) badKeys.push(key);
  });

  const rules = (f.rules || [])
    .filter(r => (r.content || '').trim())
    .map(r => ({ content: r.content.trim(), scope: r.scope || 'global',
                 severity: r.severity === 'warning' ? 'warning' : 'error',
                 check_hint: r.check_hint || '',
                 is_hard: r.severity === 'warning' ? 0 : 1 }));

  const body = {
    title, genre: (f.genre || '').trim(), tone: (f.tone || '').trim(),
    premise: (f.premise || '').trim(),
    initial_tension: (f.tension || '').trim(),
    world_time: (f.time || '').trim(), decision_mode: f.mode || 'director',
    world_state: worldState, rules,
    tags: f.tags || [], genre_key: f.genreKey || '',
    design_note: f.designNote || '',
  };
  const btn = $('#modalFoot .btn.pri');
  if (btn) btn.disabled = true;
  try {
    const r = await api('/api/genre/world_apply', { method: 'POST', body });
    S.novels = (await api('/api/novels')).novels;
    S.novelId = r.novel.id;
    localStorage.setItem('nw_novel', String(S.novelId));
    resetNovelCache();
    fillNovelSelect();
    $('.tab[data-v="world"]').click();
    await refreshView('world');
    render();
    const ap = r.applied || {};
    if (ap.skipped_state && ap.skipped_state.length) {
      toast('已创建《' + title + '》，但有 ' + ap.skipped_state.length
        + ' 条世界状态因缺少主体被跳过', 'warn');
    } else {
      toast('已创建《' + title + '》：' + (ap.state || 0) + ' 条状态、'
        + (ap.rules || 0) + ' 条规则', 'ok');
    }
    S.nwForm = null; S.nwHint = ''; S.nwDraft = null;
    S.castDraft = [];
    S.castMeta = { fromNew: true };
    openCastStep();
  } catch (e) {
    toast(e.message, 'err');
    if (btn) btn.disabled = false;
  }
}

/* ---- 创建世界 · 第 2 步：初始阵容 ----

   没有人就没有推演：WorldEngine 的简报在角色表为空时连【角色】段都不拼，
   但提示词又强制要求事件有 actor/intent/result，模型只能当场编人；
   编出来的人不落库，于是下一轮又是另一批。
   所以第一次推演之前，先把这套阵容落库。 */

function openCastStep() {
  if (!S.castDraft) S.castDraft = [];
  if (!S.castMeta) S.castMeta = {};
  const list = S.castDraft;
  const cfg = S.castMeta.cfg || { count: 4, focus: '' };
  const meta = S.castMeta;

  const html = `
    <p class="dim small" style="margin-top:0">
      主角（<span class="tag info">主角</span>）在人机分工下由你拍板，其余交给 AI。<br>
      目标和关系都要具体——推演时模型是照着「谁要什么」和「谁跟谁什么关系」让角色互相撞的。
      没有关系网，AI 会默认这些人互相都认识，陌生人开口就像老友。</p>
    ${meta.conflict_map ? `<div class="opt" style="cursor:default">
      <div class="lb">这套阵容的冲突结构</div>
      <div class="ds">${esc(meta.conflict_map)}</div></div>` : ''}
    <div class="row" style="margin:12px 0">
      <label class="f" style="flex:none;width:92px"><span>人数</span>
        <select id="castCount">${[3, 4, 5, 6, 7].map(n =>
          `<option value="${n}"${cfg.count === n ? ' selected' : ''}>${n}</option>`)
          .join('')}</select></label>
      <div style="flex:1">${field('额外要求（可选）', 'castFocus', cfg.focus,
        '例如：要有一个内部叛徒 / 不要超能力')}</div>
    </div>
    <div class="row" style="margin-bottom:12px">
      <button class="btn pri" onclick="doCastGen()">让 AI 组建阵容</button>
      <button class="btn" onclick="openCastEdit(${list.length})">＋ 手动添加</button>
      <span class="dim small">已定 ${list.length} 人 · ${(meta.relations || []).length} 条关系</span>
    </div>
    <div id="castLog"></div>
    <ul class="list" style="margin-top:12px">${list.length ? list.map((c, i) => `
      <li>
        <div class="main">
          <div class="title">${esc(c.name)}
            <span class="tag ${c.rank === 'A' ? 'accent' : ''}">${esc(c.rank || 'C')}级</span>
            ${c.role_tag ? '<span class="tag">' + esc(c.role_tag) + '</span>' : ''}
            ${c.is_protagonist ? '<span class="tag info">主角</span>' : ''}
            ${c._exists ? '<span class="tag warn">已建档</span>' : ''}</div>
          <div class="sub">
            ${c.location ? '位于 ' + esc(c.location) + ' ｜ ' : ''}
            ${c.personality ? esc(c.personality) : '（性格未定义）'}</div>
          ${(c.goals || []).length ? `<div class="sub" style="margin-top:4px">
            ${(c.goals || []).map(g => `<span class="tag ${
              g.goal_type === 'long' ? 'info' : 'ok'}">${
              g.goal_type === 'long' ? '长期' : '短期'} ${esc(g.content)}</span>`).join(' ')}
          </div>` : '<div class="sub dim">（没有目标，推演时他不会被推动）</div>'}
        </div>
        <div class="acts">
          <button class="btn sm ghost" onclick="openCastEdit(${i})">改</button>
          <button class="btn sm ghost" onclick="delCast(${i})">删</button>
        </div>
      </li>`).join('') : '<div class="empty">还没有角色。点「让 AI 组建阵容」，或自己加一个。</div>'}
    </ul>
    ${castRelationsHtml()}`;

  const foot = `
    <button class="btn" onclick="skipCast()">先不建，直接进世界</button>
    <button class="btn pri" onclick="applyCast()"
      ${list.length ? '' : 'disabled'}>确认 ${list.length} 人${(meta.relations || []).length
        ? '、' + (meta.relations || []).length + ' 条关系' : ''}并进入世界</button>`;
  openModal('初始阵容（第 2 步 / 共 2 步）', html, foot, true);
}

/* 关系网区块：AI 起草 + 手动增删。
   这一块是"没有角色关系"问题的第一道补丁——创建世界时就得有关系，
   否则第一次推演起模型就在猜谁认识谁，错的关系会一路被当成事实继承。 */
const REL_LABELS = { family: '亲属', friend: '朋友', enemy: '敌对',
                     lover: '恋慕', colleague: '同僚', mentor: '师徒',
                     rival: '竞争', other: '其他' };

function castRelationsHtml() {
  const rels = (S.castMeta && S.castMeta.relations) || [];
  const dropped = (S.castMeta && S.castMeta.relationsDropped) || [];
  const names = (S.castDraft || []).map(c => (c.name || '').trim()).filter(Boolean);
  return `
    <div class="card" style="margin-top:14px">
      <h3>人物关系网 <span class="spacer"></span>
        <button class="btn sm" onclick="openRelationEdit(-1)"
          ${names.length >= 2 ? '' : 'disabled'}>＋ 关系</button></h3>
      <div class="body tight">
        ${rels.length ? `<ul class="list">${rels.map((r, i) => `
          <li>
            <div class="main">
              <div class="title">${esc(r.a)} ${r.is_mutual === false ? '→' : '↔'} ${esc(r.b)}
                <span class="tag ${r.relation_type === 'enemy' ? 'bad'
                  : r.relation_type === 'rival' ? 'warn' : 'info'}">${
                  esc(REL_LABELS[r.relation_type] || r.relation_type)}</span>
                <span class="dim small">强度 ${r.intensity || 3}/5</span>
                ${r.is_mutual === false ? '<span class="tag warn">单向 · 对方不知情</span>' : ''}
                ${r.change === 'broken' ? '<span class="tag bad">已破裂</span>' : ''}
                ${r.change === 'new' ? '<span class="tag ok">新建立</span>' : ''}</div>
              <div class="sub">${esc(r.description || '（无描述）')}</div>
            </div>
            <div class="acts">
              <button class="btn sm ghost" onclick="openRelationEdit(${i})">改</button>
              <button class="btn sm ghost" onclick="delCastRelation(${i})">删</button>
            </div>
          </li>`).join('')}</ul>`
          : `<div class="empty">还没有关系。<br>
              点「让 AI 组建阵容」会连带生成，或点右上「＋ 关系」手动加。
              <br>关系网是推演的燃料：没有它，模型不知道谁跟谁熟、谁防着谁。</div>`}
        ${dropped.length ? `<div class="small" style="margin-top:8px;color:var(--warn)">
          ⚠ 有 ${dropped.length} 条关系引用了阵容外的名字，已跳过：${
            esc(dropped.slice(0, 5).join('、'))}</div>` : ''}
      </div>
    </div>`;
}

function delCastRelation(i) {
  (S.castMeta.relations || []).splice(i, 1);
  openCastStep();
}

function openRelationEdit(i) {
  const rels = (S.castMeta && S.castMeta.relations) || [];
  const isNew = (i < 0);
  const r = isNew ? {} : (rels[i] || {});
  const names = (S.castDraft || []).map(c => (c.name || '').trim()).filter(Boolean);
  const opts = sel => names.map(n =>
    `<option${sel === n ? ' selected' : ''}>${esc(n)}</option>`).join('');
  openModal(isNew ? '添加人物关系' : '编辑人物关系', `
    <p class="dim small" style="margin-top:0">
      写清"他们之间有过什么、现在什么状态、芥蒂在哪"——推演和写正文都靠这段来决定
      他们见面时的态度。写"他们是同事"这种空标签没有用。</p>
    <div class="row">
      <div style="flex:1"><label class="f"><span>甲方</span>
        <select id="relA">${opts(r.a)}</select></label></div>
      <div style="flex:1"><label class="f"><span>乙方</span>
        <select id="relB">${opts(r.b || (names[1] || ''))}</select></label></div>
    </div>
    <div class="row">
      <div style="flex:1"><label class="f"><span>关系类型</span>
        <select id="relType">${Object.keys(REL_LABELS).map(k =>
          `<option value="${k}"${(r.relation_type || 'colleague') === k
            ? ' selected' : ''}>${REL_LABELS[k]}</option>`).join('')}</select></label></div>
      <div style="flex:none;width:110px"><label class="f"><span>强度 1-5</span>
        <input id="relIntensity" type="number" min="1" max="5"
          value="${r.intensity || 3}"></label></div>
    </div>
    ${area('关系内容', 'relDesc', r.description,
      '他们之间发生过什么、现在处什么状态、芥蒂/恩情在哪')}
    <label class="f" style="display:flex;align-items:center;gap:8px">
      <input type="checkbox" id="relMutual" style="width:auto"
        ${r.is_mutual === false ? '' : 'checked'}>
      <span>双向关系（取消勾选＝单向：一方有这心思，另一方不知道）</span></label>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn pri" onclick="saveRelationEdit(${i})">保存</button>`);
}

function saveRelationEdit(i) {
  const a = $('#relA').value.trim();
  const b = $('#relB').value.trim();
  if (!a || !b) return toast('请选择关系双方', 'err');
  if (a === b) return toast('不能给自己建关系', 'err');
  const entry = {
    a, b, relation_type: $('#relType').value,
    intensity: parseInt($('#relIntensity').value, 10) || 3,
    description: $('#relDesc').value.trim(),
    is_mutual: $('#relMutual').checked,
  };
  const rels = S.castMeta.relations || (S.castMeta.relations = []);
  // 同类型同方向视为同一条，覆盖而不是堆两条
  const dup = rels.findIndex(r => r.a === a && r.b === b
    && r.relation_type === entry.relation_type);
  if (dup >= 0 && dup !== i) {
    rels[dup] = entry;
  } else if (i < 0) {
    rels.push(entry);
  } else {
    rels[i] = entry;
  }
  closeModal();
  openCastStep();
}

async function doCastGen() {
  S.castMeta.cfg = {
    count: parseInt($('#castCount').value, 10) || 4,
    focus: $('#castFocus').value,
  };
  const body = { novel_id: S.novelId, count: S.castMeta.cfg.count,
    focus: S.castMeta.cfg.focus };
  await runJob('/api/novel/cast', body, 'castLog', '阵容生成', async (r) => {
    if (!r) return;
    // 手动加的人保留，AI 草稿覆盖上来
    const manual = (S.castDraft || []).filter(c => c._manual);
    const got = (r.characters || []).slice();
    manual.forEach(m => {
      if (!got.some(c => c.name === m.name)) got.push(m);
    });
    S.castDraft = got;
    S.castMeta.protagonist = r.protagonist;
    S.castMeta.conflict_map = r.conflict_map;
    // 关系网：跟着草案一起走，落库时一并提交。
    // 手动加的人不参与 AI 关系，所以每次生成都覆盖（不是合并）——
    // 合并会让上一轮已经不存在的角色关系留在列表里。
    S.castMeta.relations = (r.relations || []).slice();
    S.castMeta.relationsDropped = r.dropped_relations || [];
    openCastStep();
    const nRel = S.castMeta.relations.length;
    toast('AI 给出 ' + (r.characters || []).length + ' 个角色、' +
      nRel + ' 条关系，逐条过一遍再确认',
      nRel ? 'ok' : 'warn');
  });
}

function delCast(i) {
  S.castDraft.splice(i, 1);
  openCastStep();
}

function openCastEdit(i) {
  const isNew = (i >= S.castDraft.length);
  const c = isNew ? {} : (S.castDraft[i] || {});
  const ranks = ['A', 'B', 'C', 'D', 'E'];
  const goals = c.goals || [];
  const long = (goals.find(g => g.goal_type === 'long') || {}).content || '';
  const short = (goals.find(g => g.goal_type !== 'long') || {}).content || '';
  openModal(isNew ? '添加角色' : '编辑角色', `
    <div class="row">
      <div style="flex:2">${field('姓名', 'ceName', c.name)}</div>
      <div style="flex:1"><label class="f"><span>等级</span>
        <select id="ceRank">${ranks.map(r =>
          `<option${(c.rank || 'C') === r ? ' selected' : ''}>${r}</option>`).join('')}
        </select></label></div>
    </div>
    <div class="row">
      <div style="flex:1">${field('身份', 'ceRole', c.role_tag, '调查员 / 局长…')}</div>
      <div style="flex:1">${field('位置', 'ceLoc', c.location)}</div>
    </div>
    ${area('性格', 'cePersonality', c.personality, '他会怎么做，而不只是形容词')}
    ${area('背景', 'ceBg', c.background)}
    <div class="row">
      <div style="flex:1">${field('说话方式', 'ceSpeech', c.speech_style, '短句、爱用反问…')}</div>
      <div style="flex:1">${field('决策倾向', 'ceTendency', c.decision_tendency,
        '先观察再动手')}</div>
    </div>
    ${field('长期目标（可空）', 'ceLong', long, '他要的最终结果，和谁冲突')}
    ${field('短期目标（可空）', 'ceShort', short, '眼下这一步想拿到什么')}
    <label class="f"><span>主角</span>
      <select id="ceLead">
        <option value="1"${c.is_protagonist ? ' selected' : ''}>是</option>
        <option value="0"${!c.is_protagonist ? ' selected' : ''}>否</option>
      </select></label>`,
    `<button class="btn" onclick="openCastStep()">返回</button>
     <button class="btn pri" onclick="doCastEdit(${i}, ${isNew ? 1 : 0})">保存</button>`, true);
}

function doCastEdit(i, isNew) {
  const name = $('#ceName').value.trim();
  if (!name) return toast('姓名不能为空', 'err');
  const old = isNew ? {} : (S.castDraft[i] || {});
  const keep = (content, type) => {
    if (!content) return null;
    const hit = (old.goals || []).find(g => (g.content || '').trim() === content);
    return hit ? Object.assign({}, hit, { goal_type: type }) : { content, goal_type: type };
  };
  const goals = [keep($('#ceLong').value.trim(), 'long'),
                 keep($('#ceShort').value.trim(), 'short')].filter(Boolean);
  const entry = Object.assign({}, old, {
    name, rank: $('#ceRank').value, role_tag: $('#ceRole').value,
    location: $('#ceLoc').value, personality: $('#cePersonality').value,
    background: $('#ceBg').value, speech_style: $('#ceSpeech').value,
    decision_tendency: $('#ceTendency').value,
    is_protagonist: $('#ceLead').value === '1', goals,
  });
  if (isNew) entry._manual = true;
  if (entry.is_protagonist) S.castDraft.forEach(c => { c.is_protagonist = false; });
  if (isNew) S.castDraft.push(entry); else S.castDraft[i] = entry;
  openCastStep();
}

async function applyCast() {
  const chars = (S.castDraft || []).filter(c => (c.name || '').trim());
  if (!chars.length) {
    return toast('至少要留一个角色——没有角色的世界，推演会每轮即兴捏一批人。', 'err');
  }
  // 关系网跟着角色一起落库。只提交双方都还在名单里的关系——
  // 用户可能刚删掉一个角色，留着那条关系会在落库时被静默跳过（白填）。
  const alive = new Set(chars.map(c => (c.name || '').trim()));
  const rels = ((S.castMeta && S.castMeta.relations) || [])
    .filter(r => alive.has(r.a) && alive.has(r.b) && r.a !== r.b);
  const dropped = ((S.castMeta && S.castMeta.relations) || []).length - rels.length;
  try {
    const r = await api('/api/novel/cast/apply', { method: 'POST',
      body: { novel_id: S.novelId, characters: chars, relations: rels } });
    const fromNew = !!(S.castMeta || {}).fromNew;
    S.castDraft = null; S.castMeta = null;
    closeModal();
    let msg = '已建档 ' + (r.created || []).length + ' 个角色';
    if (r.relations) msg += '、' + r.relations + ' 条人物关系';
    if (r.protagonist) msg += '，主角是 ' + r.protagonist;
    if (dropped > 0) msg += '（' + dropped + ' 条关系因角色被删而略过）';
    toast(msg, 'ok');
    await refreshView('world'); render();
    if (fromNew) go('world');
  } catch (e) { toast(e.message, 'err'); }
}

function skipCast() {
  const fromNew = !!(S.castMeta || {}).fromNew;
  S.castDraft = null; S.castMeta = null;
  closeModal();
  if (fromNew) go('world');
  toast('没有角色，推演会每轮即兴捏人且不留档。世界页随时可以再组建阵容。', 'err');
}

/* 世界页手动进来组建阵容（不是创建世界流程，跳过时留在原地） */
function openCastFromWorld() {
  if (!S.novelId) return toast('先选一个世界', 'err');
  S.castDraft = [];
  S.castMeta = { fromNew: false };
  openCastStep();
}

function openNovelEdit() {
  const n = S.novel || {};
  openModal('编辑世界设定', `
    ${field('书名', 'neTitle', n.title)}
    <div class="row">
      <div style="flex:1">${field('题材', 'neGenre', n.genre)}</div>
      <div style="flex:1">${field('基调', 'neTone', n.tone)}</div>
    </div>
    ${area('世界前提', 'nePremise', n.premise)}
    ${area('核心张力', 'neTension', n.initial_tension)}
    ${area('故事梗概', 'neSynopsis', n.synopsis)}`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn pri" onclick="doNovelEdit()">保存</button>`, true);
}
async function doNovelEdit() {
  try {
    await api('/api/novel', { method: 'POST', body: { novel_id: S.novelId,
      fields: { title: $('#neTitle').value, genre: $('#neGenre').value,
        tone: $('#neTone').value, premise: $('#nePremise').value,
        initial_tension: $('#neTension').value, synopsis: $('#neSynopsis').value } } });
    closeModal(); toast('已保存', 'ok');
    S.novels = (await api('/api/novels')).novels;
    await refreshView('world'); render();
  } catch (e) { toast(e.message, 'err'); }
}

/* ---- 归档 / 彻底删除世界 ---- */
let _delTarget = { id: 0, title: '', fromArch: false };

function openDeleteNovel(id) {
  const fromArch = !!id;
  const src = fromArch ? ((S.archived || []).find(a => a.id === id) || {})
                       : (S.novel || {});
  const title = String(src.title || '').trim();
  const tid = id || S.novelId;
  if (!tid) return toast('没有可删除的世界', 'err');
  const st = fromArch ? null : (S.stats || {});
  _delTarget = { id: tid, title, fromArch };

  openModal(fromArch ? '彻底删除归档世界' : '删除「' + title + '」', `
    <p style="margin-top:0">
      目标：<b>${esc(title)}</b>
      ${st ? `<span class="dim small">· ${st.chapter_count || 0} 章 ·
        ${(st.total_words || 0).toLocaleString('zh-CN')} 字 ·
        ${st.character_count || 0} 个角色</span>` : ''}</p>
    ${fromArch ? `
      <div class="card" style="margin:0 0 12px;background:var(--bad-soft);
        border-color:transparent"><div class="body">
        <div style="color:var(--bad);font-weight:600;margin-bottom:4px">
          这个世界已经在归档箱里了</div>
        <div class="dim small" style="margin:0">
          归档箱里只提供彻底删除：章节正文、角色、事件、状态记录会一并清空，
          无法撤销。想留着就先「取消」，回到「设置 → 归档箱」点恢复。</div>
      </div></div>` : `
      <div class="grid g2" style="margin-bottom:12px">
        <div class="card" style="margin:0;background:var(--warn-soft);
          border-color:transparent"><div class="body">
          <div style="color:var(--warn);font-weight:600;margin-bottom:4px">归档（推荐）</div>
          <div class="dim small" style="margin:0">
            从世界列表里藏起来，数据原样留在库里。以后在「设置 → 归档箱」
            点恢复就能接着写。</div>
        </div></div>
        <div class="card" style="margin:0;background:var(--bad-soft);
          border-color:transparent"><div class="body">
          <div style="color:var(--bad);font-weight:600;margin-bottom:4px">彻底删除</div>
          <div class="dim small" style="margin:0">
            章节、角色、事件全部清除，不可撤销。删之前会自动把已写正文导出到
            <span class="mono">novels/</span> 目录留底。</div>
        </div></div>
      </div>`}
    <label class="f"><span>要彻底删除，请照抄书名
      <span class="mono">${esc(title)}</span></span>
      <input type="text" id="delTitle" placeholder="${esc(title)}"
        autocomplete="off"></label>
    <div id="delErr" class="small" style="display:none;background:var(--bad-soft);
      color:var(--bad);border-radius:8px;padding:8px 11px;margin-bottom:10px;
      word-break:break-all"></div>`,
    `${fromArch ? '' : '<button class="btn" onclick="doDeleteNovel(\'archive\')">归档</button>'}
     <button class="btn" onclick="closeModal()">取消</button>
     <button class="btn pri danger" id="delBtn"
       onclick="doDeleteNovel('purge')">彻底删除</button>`, true);
  setTimeout(() => { const i = $('#delTitle'); if (i) i.focus(); }, 30);
}

async function doDeleteNovel(mode) {
  const err = $('#delErr');
  const fail = m => { if (err) { err.textContent = m; err.style.display = 'block'; } };
  if (err) err.style.display = 'none';
  const typed = (($('#delTitle') || {}).value || '').trim();
  if (mode === 'purge' && typed !== _delTarget.title) {
    return fail('书名不一致。要删除的是「' + _delTarget.title +
      '」，请把它原样抄一遍（含标点）。');
  }
  const btn = $('#delBtn');
  if (btn) { btn.disabled = true; btn.textContent = '处理中…'; }
  try {
    const r = await api('/api/novel/delete', { method: 'POST', body: {
      novel_id: _delTarget.id, mode, confirm_title: typed } });
    closeModal();
    toast(mode === 'archive'
      ? '已归档「' + r.title + '」，可在 设置 → 归档箱 里恢复'
      : '已彻底删除「' + r.title + '」（' + ((r.snapshot || {}).chapters || 0) +
        ' 章）' + (r.backup ? '，' + r.backup : ''), 'ok');
    await applyNovelListChange(r);
  } catch (e) {
    if (btn) { btn.disabled = false; btn.textContent = '彻底删除'; }
    fail(e.message);
  }
}

async function restoreNovel(id) {
  try {
    const r = await api('/api/novel/restore', { method: 'POST',
      body: { novel_id: id } });
    toast('已恢复「' + ((r.novel || {}).title || '') + '」', 'ok');
    S.archived = r.archived || [];
    S.novels = r.novels || [];
    fillNovelSelect();
    render();
  } catch (e) { toast(e.message, 'err'); }
}

/* 删除/恢复之后同步列表与顶栏；当前世界没了就自动切到下一个，全没了就回到空状态 */
async function applyNovelListChange(r) {
  S.novels = r.novels || [];
  S.archived = r.archived || [];
  if (r.health) S.health = r.health;
  if (!S.novels.some(n => n.id === S.novelId)) {
    S.novelId = (S.novels[0] || {}).id || 0;
    if (S.novelId) localStorage.setItem('nw_novel', String(S.novelId));
    else localStorage.removeItem('nw_novel');
    resetNovelCache();
  }
  fillNovelSelect();
  if (!S.novelId) { renderNoNovel(); return; }
  await refreshView(S.view);
  render();
}

/* ---- 推进世界 ---- */
function openAdvance() {
  openModal('推进世界时间线', `
    <p class="dim small" style="margin-top:0">
      AI 会站在所有角色的位置上，推演接下来一段时间里世界发生了什么。
      只产出事件，不写正文。</p>
    ${area('聚焦方向（可选）', 'adFocus', '',
      '例如：调查局内部开始互相猜疑 / 让叶清和与林默的线交汇')}
    ${area('额外硬约束（可选）', 'adConstraints', '',
      '例如：本段不要出现新的反派 / 陈默不能离开本市')}
    <label class="f"><span>归属章节（可选）</span>
      <input type="number" id="adChapter" value="0" min="0"></label>
    <p class="dim small">推演一般 20-60 秒。若校验发现硬伤，会拒绝落库并列出原因。</p>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn pri" onclick="doAdvance()">开始推演</button>`);
}

async function doAdvance() {
  const body = { novel_id: S.novelId, focus: $('#adFocus').value,
    constraints: $('#adConstraints').value,
    chapter_ref: parseInt($('#adChapter').value, 10) || 0 };
  closeModal();
  const res = await runJob('/api/advance', body, 'advanceLog', '世界推演',
    async (r) => {
      await refreshView('world');
      render();
      if (r && r.blocked) {
        toast('推演被拦截：' + r.reason, 'err');
      } else if (r) {
        toast('推演出 ' + (r.events || []).length + ' 个事件', 'ok');
        if ((r.decision_point_ids || []).length) {
          toast('产生 ' + r.decision_point_ids.length + ' 个决策点，去决策台看看',
            'ok');
        }
      }
    });
  if (res && !res.blocked) showAdvanceResult(res);
}

function showAdvanceResult(r) {
  const evs = r.events || [];
  const el = document.getElementById('advanceLog');
  if (!el) return;
  el.innerHTML = `
    <div class="small dim" style="margin-bottom:8px">
      世界时间推进到 <b>${esc(r.world_time || '—')}</b></div>
    ${evs.map(e => `<div class="opt" style="cursor:default">
      <div class="lb">${esc(e.title || '')}
        <span class="tag">${esc(e.event_type || '')}</span>
        <span class="dim small">${esc(e.location || '')}</span></div>
      <div class="ds">${esc(e.description || '')}</div>
      ${e.intent ? '<div class="ch">（角色的意图不会被直接写进正文）</div>' : ''}
    </div>`).join('')}
    ${(r.open_threads || []).length ? `<div class="small" style="margin-top:8px">
      <b>新涌现的线索</b>：${(r.open_threads || []).map(t =>
        '<span class="tag warn">' + esc(t.title || t) + '</span>').join(' ')}</div>` : ''}
    ${(r.check && r.check.warnings || []).length ? `<div class="small dim"
      style="margin-top:8px">校验提醒：
      ${(r.check.warnings || []).map(w => esc(w.message)).join('；')}</div>` : ''}
  `;
}

/* ---- 状态 / 实体 / 角色 / 线索 ---- */
function openStateEdit() {
  openModal('设置世界状态', `
    <p class="dim small" style="margin-top:0">
      状态量是世界模型的核心。AI 推演时会读它，也会改它。</p>
    ${field('状态名', 'stKey', '', '例如：异常实体.剩余数量')}
    <div class="row">
      <div style="flex:1">${field('当前值', 'stVal', '')}</div>
      <div style="flex:1"><label class="f"><span>类型</span>
        <select id="stType"><option value="text">文本</option>
          <option value="int">整数</option><option value="float">小数</option>
          <option value="bool">真假</option><option value="json">JSON</option>
        </select></label></div>
    </div>
    <div class="row">
      <div style="flex:1">${field('分类', 'stCat', 'other', 'status / relation / resource…')}</div>
      <div style="flex:2">${field('备注', 'stNote', '')}</div>
    </div>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn pri" onclick="doStateEdit()">保存</button>`);
}
async function doStateEdit() {
  const key = $('#stKey').value.trim();
  if (!key) return toast('状态名不能为空', 'err');
  let val = $('#stVal').value;
  const t = $('#stType').value;
  if (t === 'int') val = parseInt(val, 10) || 0;
  else if (t === 'float') val = parseFloat(val) || 0;
  else if (t === 'bool') val = ['1', 'true', '是', 'yes'].includes(val.toLowerCase());
  else if (t === 'json') { try { val = JSON.parse(val || '{}'); }
    catch (e) { return toast('JSON 格式不对', 'err'); } }
  try {
    await api('/api/state', { method: 'POST', body: { novel_id: S.novelId,
      key, value: val, value_type: t, category: $('#stCat').value,
      note: $('#stNote').value, reason: '手动设定' } });
    closeModal(); toast('已保存', 'ok');
    await refreshView('world'); render();
  } catch (e) { toast(e.message, 'err'); }
}

async function openStateHistory(key) {
  try {
    const r = await api('/api/state?novel_id=' + S.novelId +
      '&key=' + encodeURIComponent(key));
    const hs = r.history || [];
    openModal(`「${key}」的变化历史`, `
      ${hs.length ? `<table class="t"><thead><tr>
        <th>时间</th><th>值</th><th>原因</th></tr></thead><tbody>
        ${hs.map(h => `<tr><td class="small dim">${esc(h.created_at || '')}</td>
          <td class="mono">${esc(fmtVal(h.value ?? h.new_value))}</td>
          <td class="small">${esc(h.reason || h.log_reason || '')}</td></tr>`).join('')}
      </tbody></table>` : '<div class="empty">还没有变更记录</div>'}`,
      `<button class="btn" onclick="closeModal()">关闭</button>`, true);
  } catch (e) { toast(e.message, 'err'); }
}

function openEntityEdit(id) {
  const e = id ? (S.entities.find(x => x.id === id) || {}) : {};
  const types = { faction: '势力', location: '地点', organization: '组织',
    item: '物品', concept: '概念', phenomenon: '现象', other: '其他' };
  openModal(id ? '编辑世界实体' : '新增世界实体', `
    ${field('名称', 'enName', e.name)}
    <label class="f"><span>类型</span><select id="enType">
      ${Object.keys(types).map(k => `<option value="${k}"${
        e.entity_type === k ? ' selected' : ''}>${types[k]}</option>`).join('')}
    </select></label>
    ${area('描述', 'enDesc', e.description)}
    <div class="row">
      <div style="flex:1"><label class="f"><span>威胁/重要度 1-5</span>
        <input type="number" id="enPower" min="1" max="5"
          value="${e.power_level || 3}"></label></div>
      <div style="flex:1"><label class="f"><span>可见性</span>
        <select id="enVis">
          <option value="public"${e.visibility === 'public' ? ' selected' : ''}>公开</option>
          <option value="hidden"${e.visibility === 'hidden' ? ' selected' : ''}>隐藏</option>
          <option value="revealed"${e.visibility === 'revealed' ? ' selected' : ''}>已揭示</option>
        </select></label></div>
    </div>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn pri" onclick="doEntityEdit(${id || 0})">保存</button>`);
}
async function doEntityEdit(id) {
  try {
    const b = { novel_id: S.novelId, name: $('#enName').value,
      entity_type: $('#enType').value, description: $('#enDesc').value,
      power_level: parseInt($('#enPower').value, 10) || 3,
      visibility: $('#enVis').value };
    if (id) await api('/api/entities/update', { method: 'POST',
      body: { id, fields: { name: b.name, entity_type: b.entity_type,
        description: b.description, power_level: b.power_level,
        visibility: b.visibility } } });
    else await api('/api/entities', { method: 'POST', body: b });
    closeModal(); toast('已保存', 'ok');
    await refreshView('world'); render();
  } catch (e) { toast(e.message, 'err'); }
}

function openCharEdit(id) {
  const c = id ? (S.characters.find(x => x.id === id) || {}) : {};
  const ranks = ['A', 'B', 'C', 'D', 'E'];
  openModal(id ? '编辑角色' : '新增角色', `
    <div class="row">
      <div style="flex:2">${field('姓名', 'chName', c.name)}</div>
      <div style="flex:1"><label class="f"><span>等级</span>
        <select id="chRank">${ranks.map(r =>
          `<option${c.rank === r ? ' selected' : ''}>${r}</option>`).join('')}
        </select></label></div>
    </div>
    <div class="row">
      <div style="flex:1">${field('身份', 'chRole', c.role_tag, '调查员 / 局长…')}</div>
      <div style="flex:1">${field('位置', 'chLoc', c.current_location)}</div>
    </div>
    ${area('性格', 'chPersonality', c.personality, '谨慎、嘴硬心软、反应快…')}
    ${area('背景', 'chBg', c.background)}
    <div class="row">
      <div style="flex:1">${field('说话方式', 'chSpeech', c.speech_style,
        '短句、爱用反问…')}</div>
      <div style="flex:1">${field('决策倾向', 'chTendency', c.decision_tendency,
        '先观察再动手')}</div>
    </div>
    <label class="f"><span>托管模式</span>
      <select id="chCtrl">
        <option value="ai"${c.control_mode === 'ai' ? ' selected' : ''}>AI 托管</option>
        <option value="user"${c.control_mode === 'user' ? ' selected' : ''}>你来做主</option>
        <option value="auto_delegate"${c.control_mode === 'auto_delegate' ? ' selected' : ''}>AI 代管</option>
      </select></label>
    ${!id ? `
      <hr class="sep">
      <p class="dim small" style="margin-top:0">初始目标可以留空，后面随事件临时生成。</p>
      ${field('长期目标（可空）', 'chLong', '')}
      ${field('短期目标（可空）', 'chShort', '')}` : ''}`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn pri" onclick="doCharEdit(${id || 0})">保存</button>`, true);
}

async function doCharEdit(id) {
  const name = $('#chName').value.trim();
  if (!name) return toast('姓名不能为空', 'err');
  const fields = { name, rank: $('#chRank').value, role_tag: $('#chRole').value,
    current_location: $('#chLoc').value, personality: $('#chPersonality').value,
    background: $('#chBg').value, speech_style: $('#chSpeech').value,
    decision_tendency: $('#chTendency').value, control_mode: $('#chCtrl').value };
  try {
    if (id) await api('/api/character/update', { method: 'POST',
      body: { id, fields } });
    else {
      const goals = [];
      if ($('#chLong') && $('#chLong').value.trim())
        goals.push({ content: $('#chLong').value.trim(), goal_type: 'long' });
      if ($('#chShort') && $('#chShort').value.trim())
        goals.push({ content: $('#chShort').value.trim(), goal_type: 'short' });
      await api('/api/characters', { method: 'POST',
        body: Object.assign({ novel_id: S.novelId, goals }, fields) });
    }
    closeModal(); toast('已保存', 'ok');
    await refreshView(S.view); render();
  } catch (e) { toast(e.message, 'err'); }
}

function openCharDetail(id) {
  api('/api/character?id=' + id).then(r => {
    const c = r.character;
    openModal(c.name, `
      <div class="row wrap small" style="margin-bottom:12px">
        <span class="tag">${esc(c.rank)}级</span>
        ${c.role_tag ? '<span class="tag">' + esc(c.role_tag) + '</span>' : ''}
        ${ctrlTag(c.control_mode)}
        <span class="tag ${c.status === 'alive' ? 'ok' : 'bad'}">${esc(c.status)}</span>
      </div>
      <div class="kv" style="margin-bottom:14px">
        <span class="k">位置</span><span>${esc(c.current_location || '—')}</span>
        <span class="k">性格</span><span>${esc(c.personality || '—')}</span>
        <span class="k">说话方式</span><span>${esc(c.speech_style || '—')}</span>
        <span class="k">决策倾向</span><span>${esc(c.decision_tendency || '—')}</span>
        <span class="k">背景</span><span>${nl2br(c.background || '—')}</span>
      </div>
      <h4 style="margin:14px 0 6px;font-size:13px;color:var(--ink-2)">目标</h4>
      ${(c.goals || []).length ? (c.goals || []).map(g => `<div style="padding:5px 0;
        border-bottom:1px solid var(--line-2)">
        <span class="tag ${g.goal_type === 'long' ? 'info' : 'ok'}">
          ${g.goal_type === 'long' ? '长期' : '短期'}</span>
        ${esc(g.content)}
        <span class="dim small">进度 ${g.progress}% · ${esc(g.status)}</span>
        ${g.obstacle ? '<div class="dim small">障碍：' + esc(g.obstacle) + '</div>' : ''}
      </div>`).join('') : '<div class="dim small">（暂无目标）</div>'}
      <div class="row" style="margin-top:10px">
        <button class="btn sm" onclick="openGoalEdit(${id})">＋ 加目标</button>
      </div>
      <h4 style="margin:14px 0 6px;font-size:13px;color:var(--ink-2)">
        角色记忆（只记他知道的）</h4>
      ${(c.memory || []).length ? (c.memory || []).map(m => `<div class="small"
        style="padding:3px 0">· ${esc(typeof m === 'string' ? m : JSON.stringify(m))}</div>`).join('')
        : '<div class="dim small">（还没有记忆）</div>'}
      <div class="row" style="margin-top:8px">
        <input type="text" id="memText" placeholder="补记一条他知道的事…">
        <button class="btn sm" onclick="addMemory(${id})">记下</button>
      </div>
      ${Object.keys(c.tracker || {}).length ? `
        <h4 style="margin:14px 0 6px;font-size:13px;color:var(--ink-2)">
          内部倾向累积（防角色突变）</h4>
        ${Object.entries(c.tracker).map(([k, v]) => `<div class="small">
          ${esc(k)}：<span class="mono">${esc(JSON.stringify(v))}</span></div>`).join('')}` : ''}`,
      `<button class="btn" onclick="closeModal()">关闭</button>
       <button class="btn" onclick="openCharEdit(${id})">编辑</button>
       <button class="btn danger" onclick="killChar(${id})">标记死亡</button>`, true);
  });
}

async function addMemory(id) {
  const t = $('#memText').value.trim();
  if (!t) return;
  try {
    await api('/api/character/memory', { method: 'POST', body: { id, entry: t } });
    toast('已记下', 'ok'); openCharDetail(id);
  } catch (e) { toast(e.message, 'err'); }
}
function killChar(id) {
  const c = (S.characters || []).find(x => x.id === id) || {};
  confirmDialog('标记角色已死亡', `
    <p style="margin-top:0">把「<b>${esc(c.name || '')}</b>」标记为已死亡。</p>
    <p class="dim small" style="margin:0">推演时他会从可用角色里移除，
      但仍会留在角色表里，之前的戏份不会丢。</p>`,
    '确认标记', async () => {
      await api('/api/character/die', { method: 'POST', body: { id } });
      toast('已标记', 'ok');
      await refreshView(S.view); render();
    });
}

function openGoalEdit(cid) {
  const c = S.characters.find(x => x.id === cid) || {};
  openModal('给「' + c.name + '」加一个目标', `
    ${area('目标内容', 'goContent', '', '例如：查清父亲失踪那年的档案编号')}
    <label class="f"><span>类型</span>
      <select id="goType"><option value="short">短期</option>
        <option value="long">长期</option></select></label>
    ${field('动机（可选）', 'goMotivation', '', '他为什么想要这个')}
    ${field('障碍（可选）', 'goObstacle', '', '什么挡着他')}
    <label class="f"><span>优先级 1-5</span>
      <input type="number" id="goPriority" min="1" max="5" value="3"></label>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn pri" onclick="doGoalEdit(${cid})">添加</button>`);
}
async function doGoalEdit(cid) {
  const content = $('#goContent').value.trim();
  if (!content) return toast('目标不能为空', 'err');
  try {
    await api('/api/goals', { method: 'POST', body: { novel_id: S.novelId,
      character_id: cid, content, goal_type: $('#goType').value,
      motivation: $('#goMotivation').value, obstacle: $('#goObstacle').value,
      priority: parseInt($('#goPriority').value, 10) || 3 } });
    closeModal(); toast('已添加', 'ok'); openCharDetail(cid);
  } catch (e) { toast(e.message, 'err'); }
}

function openThreadEdit() {
  openModal('埋一条线索', `
    <p class="dim small" style="margin-top:0">
      线索也可以让 AI 在推演中自动涌现，这里用于手动埋设。</p>
    ${field('线索标题', 'thTitle', '', '例如：档案室第 7 排少了一份卷宗')}
    ${area('描述', 'thDesc', '')}
    <div class="row">
      <div style="flex:1">${field('埋于第几章', 'thPlanted', '0')}</div>
      <div style="flex:1">${field('计划第几章回收', 'thTarget', '0')}</div>
      <div style="flex:1"><label class="f"><span>张力 1-6</span>
        <input type="number" id="thTension" min="1" max="6" value="3"></label></div>
    </div>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn pri" onclick="doThreadEdit()">埋下</button>`);
}
async function doThreadEdit() {
  const title = $('#thTitle').value.trim();
  if (!title) return toast('标题不能为空', 'err');
  try {
    await api('/api/threads', { method: 'POST', body: { novel_id: S.novelId,
      title, description: $('#thDesc').value,
      planted_chapter: parseInt($('#thPlanted').value, 10) || 0,
      target_chapter: parseInt($('#thTarget').value, 10) || 0,
      tension_level: parseInt($('#thTension').value, 10) || 3 } });
    closeModal(); toast('已埋下', 'ok');
    await refreshView('world'); render();
  } catch (e) { toast(e.message, 'err'); }
}
async function nudgeThread(id, d) {
  try { await api('/api/threads/nudge', { method: 'POST', body: { id, delta: d } });
    await refreshView('world'); render();
  } catch (e) { toast(e.message, 'err'); }
}
function resolveThread(id) {
  const t = (S.threads || []).find(x => x.id === id) || {};
  confirmDialog('回收这条线索', `
    <p style="margin-top:0"><b>${esc(t.title || '')}</b></p>
    <label class="f"><span>在哪一章回收？</span>
      <input type="number" id="thCh" min="1"
        value="${(S.novel || {}).current_chapter || 1}"></label>
    <p class="dim small" style="margin:0">这里填的章节号会记成线索的回收章，
      用来判断有没有拖着不收。</p>`,
    '确认回收', async () => {
      const num = parseInt(($('#thCh') || {}).value, 10) || 0;
      await api('/api/threads/resolve', { method: 'POST',
        body: { id, chapter_ref: num } });
      toast('已回收', 'ok');
      await refreshView('world'); render();
    });
}

function openTimeline() {
  const tl = S.timeline || [];
  const labels = { pending: '待触发', triggered: '已触发', expired: '已过期',
                   cancelled: '已取消' };
  openModal('时间线锚点', `
    <p class="dim small" style="margin-top:0">
      锚点是"挂到未来某个时点上的约定/期限"：推演时提到的「三天后的听证会」
      「霜月十五的祭典」会自动记在这里，推演下一轮就会把它们摆给模型看——
      世界往前走，这些账迟早要还。你也可以手动加。</p>
    ${tl.length ? `<table class="t"><thead><tr><th>时间</th><th>要发生的事</th>
      <th>状态</th><th></th></tr></thead><tbody>${tl.map(t => `<tr>
      <td class="mono">${esc(t.time_anchor || '')}
        ${t.time_span ? '<div class="dim small">' + esc(t.time_span) + '</div>' : ''}</td>
      <td>${esc(t.description || '')}
        ${t.countdown_name ? '<div class="dim small">来自：' + esc(t.countdown_name) + '</div>' : ''}</td>
      <td><span class="tag ${t.status === 'pending' ? '' : 'info'}">${
        esc(labels[t.status] || t.status || '')}</span></td>
      <td style="text-align:right;white-space:nowrap">
        ${t.status === 'pending' ? `<button class="btn sm" onclick="setTimelineStatus(${t.id},'triggered')">已到期</button>` : ''}
        <button class="btn sm ghost" onclick="delTimeline(${t.id})">删</button></td>
      </tr>`).join('')}</tbody></table>`
      : '<div class="empty">还没有时间线锚点。<br>推演后会从这里长出来，也可以手动加一个。</div>'}
    <div class="row" style="margin-top:14px">
      <button class="btn pri" onclick="doPulse()">让世界自己动一动</button>
      <button class="btn" onclick="addTimelineManual()">＋ 手动加锚点</button>
    </div>
    <div id="pulseLog"></div>`,
    `<button class="btn" onclick="closeModal()">关闭</button>`, true);
}

async function setTimelineStatus(id, status) {
  try {
    await api('/api/timeline/status', { method: 'POST', body: { id, status } });
    S.timeline = (await api('/api/timeline?novel_id=' + S.novelId)).timeline;
    render(); openTimeline();
  } catch (e) { toast(e.message, 'err'); }
}

async function delTimeline(id) {
  confirmDialog('删除锚点', '<p style="margin:0">删掉后这条约定就不再提醒推演了。</p>',
    '删除', async () => {
      await api('/api/timeline/delete', { method: 'POST', body: { id } });
      S.timeline = (await api('/api/timeline?novel_id=' + S.novelId)).timeline;
      render(); openTimeline();
    });
}

function addTimelineManual() {
  openModal('手动添加时间线锚点', `
    <p class="dim small" style="margin-top:0">
      你安排好的"到点必发生"的事。推演时会把它当作既定事实。</p>
    ${field('时间锚点', 'tlAnchor', '', '例如：霜月十五 / 三天后 / 月末清算日')}
    <div class="row">
      <div style="flex:1">${field('距现在还有多久（可选）', 'tlSpan', '', '如：还剩 3 天')}</div>
      <div style="flex:1">${field('来源/相关方（可选）', 'tlName', '', '如：调查局')}</div>
    </div>
    ${area('到期会发生什么', 'tlDesc', '', '写清楚时间一到，世界会变成什么样')}`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn pri" onclick="doAddTimeline()">添加</button>`);
}

async function doAddTimeline() {
  const time_anchor = $('#tlAnchor').value.trim();
  if (!time_anchor) return toast('时间锚点不能为空', 'err');
  try {
    await api('/api/timeline', { method: 'POST', body: {
      novel_id: S.novelId, time_anchor,
      time_span: $('#tlSpan').value, countdown_name: $('#tlName').value,
      description: $('#tlDesc').value } });
    S.timeline = (await api('/api/timeline?novel_id=' + S.novelId)).timeline;
    toast('已添加', 'ok'); render(); openTimeline();
  } catch (e) { toast(e.message, 'err'); }
}

/* 世界脉搏：推演"与主角无关、时间自己在改变的事"。
   这是"蝴蝶效应"的入口——远处的动静现在无关，日后可能撞进主线。
   产出不落正史，只落成时间线锚点。 */
async function doPulse() {
  const span = S.clock && S.clock.current_time
    ? '从世界时间「' + S.clock.current_time + '」往后的一段（一天到数日）'
    : '接下来的一段日子';
  await runJob('/api/pulse',
    { novel_id: S.novelId, time_span: span },
    'pulseLog', '世界脉搏',
    async (res) => {
      S.timeline = (await api('/api/timeline?novel_id=' + S.novelId)).timeline;
      const n = ((res && res.events) || []).length;
      if (!n) {
        toast('这段时间里世界没什么值得记录的变化——这本身是正常的', 'ok');
      } else {
        toast('记下 ' + n + ' 处背景变化', 'ok');
      }
      render();
      // 日志跑完了，把面板重开一遍让新锚点立刻可见
      openTimeline();
    });
}

/* ---- 托管 ---- */
async function setControl(cid, mode) {
  try { await api('/api/control', { method: 'POST',
    body: { novel_id: S.novelId, mode, character_id: cid } });
    S.characters = (await api('/api/characters?novel_id=' + S.novelId)).characters;
    render(); toast('已更新', 'ok');
  } catch (e) { toast(e.message, 'err'); }
}
function setAllControl() {
  confirmDialog('全部交给 AI 托管', `
    <p style="margin-top:0">把所有角色都设为 AI 托管。</p>
    <p class="dim small" style="margin:0">之后推演不会再停下来问你要怎么选，
      适合"我只想看故事"的用法。单个角色之后还能单独改回来。</p>`,
    '确认全部托管', async () => {
      await api('/api/control', { method: 'POST',
        body: { novel_id: S.novelId, mode: 'ai' } });
      toast('全部改为 AI 托管', 'ok');
      await refreshView(S.view); render();
    });
}

/* ---- 设置类 ---- */
function openProviderEdit(id) {
  const p = id ? (S.providers.find(x => x.id === id) || {}) : {};
  const presets = S.meta.providers || [];
  openModal(id ? '编辑服务商' : '添加 AI 服务商', `
    <label class="f"><span>选择厂商预设（自动填 Base URL 与常用模型）</span>
      <select id="pvPreset" onchange="applyPreset()">
        <option value="">— 自定义 —</option>
        ${presets.map(x => `<option value="${x.key}"
          data-url="${esc(x.base_url)}" data-kind="${esc(x.kind)}"
          data-models="${esc((x.models || []).join(','))}"
          ${(p.preset_key || '') === x.key ? ' selected' : ''}>
          ${esc(x.name)}</option>`).join('')}
      </select></label>
    ${field('名称', 'pvName', p.name, 'deepseek / 我的中转站')}
    <div class="row">
      <div style="flex:2">${field('Base URL', 'pvUrl', p.base_url,
        'https://api.deepseek.com')}</div>
      <div style="flex:1"><label class="f"><span>协议</span>
        <select id="pvKind">
          <option value="openai"${p.kind === 'openai' || !p.kind ? ' selected' : ''}>OpenAI 兼容</option>
          <option value="anthropic"${p.kind === 'anthropic' ? ' selected' : ''}>Anthropic</option>
          <option value="gemini"${p.kind === 'gemini' ? ' selected' : ''}>Gemini</option>
          <option value="ollama"${p.kind === 'ollama' ? ' selected' : ''}>Ollama（本地）</option>
        </select></label></div>
    </div>
    <label class="f"><span>API Key${id ? '（留空表示不修改）' : ''}</span>
      <input type="password" id="pvKey" placeholder="${p.has_key
        ? '已配置 ' + esc(p.key_masked) : 'sk-…'}"></label>
    <label class="f"><span>模型名（可填多个，用英文逗号分隔）</span>
      <input type="text" id="pvModel" value="${esc((p.models || []).join(', '))}"
        placeholder="deepseek/deepseek-flash, glm-5"></label>
    <div id="pvErr" class="small" style="display:none;background:var(--bad-soft);
      color:var(--bad);border-radius:8px;padding:8px 11px;margin-bottom:10px;
      word-break:break-all"></div>
    <p class="dim small">密钥只写入本地 secrets.json，不会存进数据库。</p>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn" onclick="testProviderForm(${id || 0})">测试连接</button>
     <button class="btn pri" onclick="doProviderEdit(${id || 0})">保存</button>`, true);
  const sel = $('#pvPreset');
  if (sel && !(p.preset_key || '')) {
    // 没存过预设键的老记录：按名称回填，方便用户直接测试
    const guess = (p.name || '').toLowerCase();
    if (presets.some(x => x.key === guess)) sel.value = guess;
  }
}

function applyPreset() {
  const o = $('#pvPreset').selectedOptions[0];
  if (!o || !o.value) return;
  $('#pvName').value = o.value;
  $('#pvUrl').value = o.dataset.url || '';
  $('#pvKind').value = o.dataset.kind || 'openai';
  const ms = (o.dataset.models || '').split(',').filter(Boolean);
  if (ms.length) $('#pvModel').value = ms.join(', ');
}

/* 收集表单上的服务商信息，供保存/测试复用 */
function readProviderForm(id) {
  const cur = id ? (S.providers.find(x => x.id === id) || {}) : {};
  return {
    name: ($('#pvName').value || '').trim(),
    base_url: ($('#pvUrl').value || '').trim(),
    kind: $('#pvKind').value,
    preset_key: $('#pvPreset').value || '',
    api_key: $('#pvKey').value || '',
    models: ($('#pvModel').value || '')
      .split(',').map(s => s.trim()).filter(Boolean),
    provider_id: id || 0,
    api_key_ref: cur.api_key_ref || '',
  };
}

async function testProviderForm(id) {
  const f = readProviderForm(id);
  if (!f.name) return toast('先填名称', 'err');
  if (!f.models.length) return toast('先填模型名，例如 deepseek/deepseek-flash', 'err');
  toast('正在测试 ' + f.name + '…');
  const box = $('#pvErr');
  if (box) box.style.display = 'none';
  try {
    // model 取第一个：测试连通性只要一个模型够了。
    // 同时带上 models 数组，后端两个字段都认。
    const r = await api('/api/providers/test',
      { method: 'POST', body: Object.assign({}, f, { model: f.models[0] }) });
    if (r.ok) toast('连通正常 · ' + (r.latency_ms || '?') + 'ms · ' +
      (r.model || '') + ' ' + (r.sample || ''), 'ok');
    else {
      const msg = r.error || '未知错误';
      if (box) {
        box.style.display = 'block';
        box.textContent = '测试失败：' + msg + (r.hint ? '\n' + r.hint : '');
      }
      toast('失败：' + msg, 'err');
    }
  } catch (e) {
    const msg = (e && e.message) ? e.message : '未知错误';
    if (box) { box.style.display = 'block'; box.textContent = '测试失败：' + msg; }
    toast('失败：' + msg, 'err');
  }
}

async function doProviderEdit(id) {
  const f = readProviderForm(id);
  if (!f.name) return toast('名称不能为空', 'err');
  try {
    const body = { name: f.name, base_url: f.base_url, kind: f.kind,
      preset_key: f.preset_key, models: f.models };
    if (f.api_key) body.api_key = f.api_key;
    if (f.api_key_ref) body.api_key_ref = f.api_key_ref;
    if (id) body.id = id;
    const r = await api('/api/providers', { method: 'POST', body });
    closeModal(); toast('已保存', 'ok');
    await loadManage(); render();
    // 模型清单为空时提醒一句，避免"能填 Key 不能填模型"再犯
    if (!(r.models || []).length) {
      toast('提示：这个服务商还没有模型名，去档位里也要填一次', 'err');
    }
  } catch (e) {
    // 保存失败时不要关弹层，把服务端的原话完整留在界面里
    const box = $('#pvErr');
    if (box) {
      box.style.display = 'block';
      box.textContent = '保存失败：' + (e && e.message ? e.message : '未知错误');
    }
    toast('保存失败：' + (e && e.message ? e.message : '未知错误'), 'err');
  }
}

async function testProvider(id) {
  const p = S.providers.find(x => x.id === id) || {};
  toast('正在测试 ' + p.name + '…');
  try {
    const r = await api('/api/providers/test', { method: 'POST',
      body: { provider_id: id, name: p.name, base_url: p.base_url,
        key_masked: p.key_masked } });
    if (r.ok) toast('连通正常 · ' + (r.latency_ms || '?') + 'ms · ' +
      (r.model || ''), 'ok');
    else toast('失败：' + (r.error || '未知错误'), 'err');
  } catch (e) { toast(e.message, 'err'); }
}

function openDeleteProvider(id) {
  const p = (S.providers || []).find(x => x.id === id) || {};
  const used = (S.slots || []).filter(s => s.provider_id === id);
  confirmDialog('删除服务商', `
    <p style="margin-top:0">目标：<b>${esc(p.name || '')}</b>
      <span class="dim small mono">${esc(p.base_url || '')}</span></p>
    ${used.length ? `<div class="card" style="margin:0 0 10px;background:var(--bad-soft);
      border-color:transparent"><div class="body">
      <div style="color:var(--bad);font-weight:600;margin-bottom:4px">
        还有 ${used.length} 个档位在用它</div>
      <div class="dim small" style="margin:0">${used.map(s => esc(s.label)).join('、')}
        —— 删除后这些档位会变成未配置，需要重新指定服务商和模型。</div>
      </div></div>`
      : '<p class="dim small" style="margin:0 0 10px">当前没有档位在用它。</p>'}
    <p class="dim small" style="margin:0">密钥文件（secrets.json）里的条目会保留，
      不会动你的 Key。</p>`,
    '确认删除', async () => {
      await api('/api/providers/delete', { method: 'POST', body: { id } });
      await loadManage(); render();
      toast('已删除服务商「' + (p.name || '') + '」', 'ok');
    });
}

function openSlotEdit(slot) {
  const s = (S.slots || []).find(x => x.slot === slot) || {};
  const provs = S.providers || [];
  openModal('配置档位 · ' + s.label, `
    <p class="dim small" style="margin-top:0">${esc(SLOT_DESC[slot] || '')}</p>
    <label class="f"><span>服务商</span>
      <select id="slProv" onchange="onSlotProvChange()">
        <option value="">（未指定）</option>
        ${provs.map(p => `<option value="${p.id}"${s.provider_id === p.id
          ? ' selected' : ''}>${esc(p.name)}</option>`).join('')}
      </select></label>
    <label class="f"><span>模型名</span>
      <input type="text" id="slModel" list="slModelList" value="${esc(s.model || '')}"
        placeholder="deepseek-chat">
      <datalist id="slModelList">
        ${slotModelOptions(s.provider_id, s.model)}
      </datalist></label>
    <div class="row" style="margin:-4px 0 10px">
      <span class="dim small" id="slModelHint">${slotModelHint(s.provider_id)}</span>
    </div>
    <div class="row">
      <div style="flex:1"><label class="f"><span>温度</span>
        <input type="number" id="slTemp" step="0.1" min="0" max="2"
          value="${s.temperature || 0.8}"></label></div>
      <div style="flex:1"><label class="f"><span>最大输出 token</span>
        <input type="number" id="slMax" step="512" min="256"
          value="${s.max_tokens || 4096}"></label></div>
    </div>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn pri" onclick="doSlotEdit('${slot}')">保存</button>`);
}

/* 某服务商的模型清单（去重，当前值也并进去方便改） */
function modelsOf(pid, extra) {
  const p = (S.providers || []).find(x => x.id === pid);
  const out = (p && p.models) ? p.models.slice() : [];
  if (extra && out.indexOf(extra) < 0) out.unshift(extra);
  return out;
}
function slotModelOptions(pid, cur) {
  const ms = modelsOf(pid, cur);
  return ms.map(m => `<option value="${esc(m)}"></option>`).join('');
}
function slotModelHint(pid) {
  const ms = modelsOf(pid, '');
  if (!pid) return '先在「服务商」里选一个；没有可选项就直接手填模型名。';
  if (!ms.length) return '这个服务商还没登记模型名：直接手填一个，或去服务商里点「拉取模型列表」。';
  return '该服务商登记了 ' + ms.length + ' 个模型，输入框会提示候选。';
}
function onSlotProvChange() {
  const pid = parseInt($('#slProv').value, 10) || 0;
  const dl = $('#slModelList');
  if (dl) dl.innerHTML = slotModelOptions(pid, '').replace(/<option /g, '<option ');
  const hint = $('#slModelHint');
  if (hint) hint.textContent = slotModelHint(pid);
  const inp = $('#slModel');
  const ms = modelsOf(pid, '');
  if (inp && !inp.value.trim() && ms.length) inp.value = ms[0];
}
async function doSlotEdit(slot) {
  try {
    await api('/api/slots', { method: 'POST', body: { slot, novel_id: S.novelId,
      provider_id: parseInt($('#slProv').value, 10) || null,
      model: $('#slModel').value.trim(),
      temperature: parseFloat($('#slTemp').value) || 0.8,
      max_tokens: parseInt($('#slMax').value, 10) || 4096 } });
    closeModal(); toast('已保存', 'ok');
    await loadManage(); render();
  } catch (e) { toast(e.message, 'err'); }
}

function openBulkSlots() {
  const provs = S.providers || [];
  if (!provs.length) return toast('先添加一个服务商', 'err');
  // 优先用服务商登记的真实模型，其次厂商预设，最后才是通用猜测
  const first = provs[0];
  const pool = modelsOf(first.id, '');
  const pick = i => pool[i] || pool[0] || '';
  openModal('批量配置档位', `
    <p class="dim small" style="margin-top:0">
      把一套模型铺到全部档位。之后可以单独微调。</p>
    <label class="f"><span>服务商</span>
      <select id="bkProv" onchange="onBulkProvChange()">${provs.map(p =>
        `<option value="${p.id}">${esc(p.name)}</option>`).join('')}</select></label>
    <p class="dim small" id="bkHint" style="margin-top:-4px">${bulkHint(first.id)}</p>
    <hr class="sep">
    ${Object.keys(SLOT_DESC).map(slot => `<label class="f">
      <span>${esc((S.slots.find(x => x.slot === slot) || {}).label || slot)} —
        ${esc(SLOT_DESC[slot])}</span>
      <input type="text" list="bkModelList" id="bk_${slot}" value="${
        (S.slots.find(x => x.slot === slot) || {}).model || ''}"
        placeholder="${esc(pick(0))}"></label>`).join('')}
    <datalist id="bkModelList">
      ${pool.map(m => `<option value="${esc(m)}"></option>`).join('')}
    </datalist>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn pri" onclick="doBulkSlots()">全部应用</button>`, true);
}
function bulkHint(pid) {
  const ms = modelsOf(pid, '');
  return ms.length
    ? '该服务商登记了 ' + ms.length + ' 个模型，输入框会提示候选。'
    : '该服务商没有登记模型，请直接手填模型名（每个档位可不同）。';
}
function onBulkProvChange() {
  const pid = parseInt($('#bkProv').value, 10) || 0;
  const dl = $('#bkModelList');
  if (dl) dl.innerHTML = modelsOf(pid, '')
    .map(m => `<option value="${esc(m)}"></option>`).join('');
  const hint = $('#bkHint');
  if (hint) hint.textContent = bulkHint(pid);
  const ms = modelsOf(pid, '');
  if (ms.length) {
    Object.keys(SLOT_DESC).forEach(slot => {
      const el = $('#bk_' + slot);
      if (el && !el.value.trim()) el.value = ms[0];
    });
  }
}
async function doBulkSlots() {
  const pid = parseInt($('#bkProv').value, 10);
  const mapping = {};
  Object.keys(SLOT_DESC).forEach(slot => {
    const v = ($('#bk_' + slot).value || '').trim();
    if (v) mapping[slot] = v;
  });
  if (!Object.keys(mapping).length) return toast('至少填一个模型名', 'err');
  try {
    await api('/api/slots/bulk', { method: 'POST',
      body: { provider_id: pid, novel_id: S.novelId, mapping } });
    closeModal(); toast('已配置 ' + Object.keys(mapping).length + ' 个档位', 'ok');
    await loadManage(); render();
  } catch (e) { toast(e.message, 'err'); }
}

function openVoiceEdit() {
  const cats = S.meta.voice_categories || {};
  openModal('添加作者癖好', `
    <label class="f"><span>类别</span><select id="voCat">
      ${Object.keys(cats).map(k => `<option value="${k}">${cats[k]}</option>`).join('')}
    </select></label>
    ${area('内容', 'voContent', '', '例如：不写「他松了口气」这类套话')}
    ${field('例子（可选）', 'voExample', '')}
    <label class="f"><span>权重 1-5</span>
      <input type="number" id="voWeight" min="1" max="5" value="3"></label>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn pri" onclick="doVoiceEdit()">添加</button>`);
}
async function doVoiceEdit() {
  const content = $('#voContent').value.trim();
  if (!content) return toast('内容不能为空', 'err');
  try {
    await api('/api/voice', { method: 'POST', body: { novel_id: S.novelId,
      category: $('#voCat').value, content, example: $('#voExample').value,
      weight: parseInt($('#voWeight').value, 10) || 3 } });
    closeModal(); toast('已添加', 'ok');
    await loadManage(); render();
  } catch (e) { toast(e.message, 'err'); }
}

function openSeedEdit() {
  const st = S.meta.seed_types || {};
  openModal('添加事件种子', `
    <p class="dim small" style="margin-top:0">
      种子是「条件满足时可以长成事件」的胚芽，给世界提供自发动力。</p>
    ${field('名称', 'seName', '', '例如：第二具尸体的出现')}
    <label class="f"><span>类型</span><select id="seType">
      ${Object.keys(st).map(k => `<option value="${k}">${st[k]}</option>`).join('')}
    </select></label>
    ${area('描述', 'seDesc', '')}
    <label class="f"><span>强度 1-5</span>
      <input type="number" id="seIntensity" min="1" max="5" value="3"></label>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn pri" onclick="doSeedEdit()">添加</button>`);
}
async function doSeedEdit() {
  const name = $('#seName').value.trim();
  if (!name) return toast('名称不能为空', 'err');
  try {
    await api('/api/seeds', { method: 'POST', body: { novel_id: S.novelId,
      name, seed_type: $('#seType').value, description: $('#seDesc').value,
      intensity: parseInt($('#seIntensity').value, 10) || 3 } });
    closeModal(); toast('已添加', 'ok');
    await loadManage(); render();
  } catch (e) { toast(e.message, 'err'); }
}

function openCompGoalEdit() {
  openModal('设定完结条件', `
    <p class="dim small" style="margin-top:0">
      条件表达式支持 <span class="mono">world.&lt;状态名&gt;</span>、
      <span class="mono">character.&lt;角色名&gt;.&lt;属性&gt;</span>、
      <span class="mono">thread.&lt;线索标题&gt;</span>、
      <span class="mono">count.goal.active</span> 等形式。</p>
    ${field('目标标题', 'cgTitle', '', '例如：异常实体全部收容')}
    <label class="f"><span>类型</span><select id="cgType">
      <option value="world">世界线目标</option>
      <option value="character">角色目标</option>
      <option value="ai_judged">交给 AI 判断</option>
    </select></label>
    ${field('达成条件', 'cgCond', '', 'world.异常实体.剩余数量 == 0')}
    ${area('描述（可选）', 'cgDesc', '')}
    <label class="f"><span>
      <input type="checkbox" id="cgPrimary" style="width:auto"> 设为主要目标</span></label>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn pri" onclick="doCompGoalEdit()">添加</button>`);
}
async function doCompGoalEdit() {
  const title = $('#cgTitle').value.trim();
  if (!title) return toast('标题不能为空', 'err');
  try {
    await api('/api/completion/goal', { method: 'POST', body: {
      novel_id: S.novelId, title, goal_type: $('#cgType').value,
      condition_expr: $('#cgCond').value, description: $('#cgDesc').value,
      is_primary: $('#cgPrimary').checked } });
    closeModal(); toast('已添加', 'ok');
    await loadManage(); render();
  } catch (e) { toast(e.message, 'err'); }
}

function openCompModeEdit() {
  openModal('完结判定方式', `
    <label class="f"><span>方式</span><select id="cmMode">
      <option value="ai"${S.completionMode === 'ai' ? ' selected' : ''}>
        AI 自动判定（问三个问题）</option>
      <option value="goal"${S.completionMode === 'goal' ? ' selected' : ''}>
        达成设定目标即完结</option>
      <option value="both"${S.completionMode === 'both' ? ' selected' : ''}>
        两者都要满足</option>
    </select></label>
    <p class="dim small">AI 三问：张力还在吗？主要目标了结了吗？是不是在自我重复？</p>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn pri" onclick="doCompModeEdit()">保存</button>`);
}
async function doCompModeEdit() {
  try {
    await api('/api/novel', { method: 'POST', body: { novel_id: S.novelId,
      fields: { completion_mode: $('#cmMode').value } } });
    closeModal(); toast('已保存', 'ok');
    await loadManage(); render();
  } catch (e) { toast(e.message, 'err'); }
}

/* ============================================================
   视图五：技能库（v6.9）

   三块：远程库（选库 → 加载 → 装） / 本地已装（卸） /
   AI 调用点绑定（哪个档位用哪个技能）。

   为什么要有这一页：技能装在磁盘上，只有对话侧的 AI 会自动匹配；
   Web 控制台不会去扫 skills 目录。要让技能约束**页面里跑的那些模型调用**，
   必须显式绑定——绑完由 llm/router.py 把 SKILL.md 正文拼进 system 提示词。
   ============================================================ */
function skInit() {
  if (!S.skills) {
    S.skills = { local: [], sources: [], slots: [], dirs: {},
      browse: null, browseSrc: '', loading: false, err: '', maxInject: 8000,
      // 停用后仍回显的下拉选择：{slot: 技能名}
      keepSel: {} };
  }
  return S.skills;
}

async function loadSkills() {
  const k = skInit();
  k.loading = true;
  try {
    const [loc, src, bd] = await Promise.all([
      api('/api/skills/local'),
      api('/api/skills/sources'),
      api('/api/skills/bindings'),
    ]);
    k.local = loc.skills || [];
    k.dirs = loc.dirs || {};
    k.sources = src.sources || [];
    k.slots = bd.slots || [];
    k.maxInject = bd.max_inject_chars || 8000;
    k.err = '';
    // 每个技能补上「项目里哪里引用了它」。单独一趟、单独失败：
    // 这是锦上添花的信息，读不到不该让整个技能库页打不开。
    try {
      const refs = await api('/api/skills/refs');
      const m = {};
      (refs.refs || []).forEach(r => { m[r.name] = r.hits || []; });
      k.local.forEach(s => { s.refs = m[s.name] || []; });
    } catch (e) {
      k.local.forEach(s => { if (!s.refs) s.refs = []; });
    }
  } catch (e) {
    k.err = e.message || '加载失败';
  } finally {
    k.loading = false;
  }
}

function renderSkills() {
  const k = skInit();
  const host = $('#v-skills');
  if (!host) return;
  if (k.loading && !k.local.length && !k.slots.length) {
    host.innerHTML = '<div class="card"><div class="empty">'
      + '<span class="busy"></span> 正在扫描技能目录…</div></div>';
    return;
  }
  host.innerHTML =
    (k.err ? `<div class="card" style="border-color:#e9d5d3;margin-bottom:16px">
       <div class="body" style="color:var(--bad)">加载失败：${esc(k.err)}</div>
     </div>` : '')
    + skActionsCard(k) + skRemoteCard(k) + skLocalCard(k) + skBindCard(k);
}

/* ---- 卡片零：自己造技能 ---- */
function skActionsCard(k) {
  return `
  <div class="card" style="margin-bottom:16px">
    <h3>自己做技能 <span class="spacer"></span>
      <span class="tag">技能就是一份 SKILL.md</span></h3>
    <div class="body">
      <p class="dim small" style="margin-top:0">
        写好的技能放进 <span class="mono">${esc((k.dirs || {}).user || '')}</span>
        就生效。两种做法：</p>
      <div class="row wrap">
        <button class="btn pri" onclick="skImportAsk()">导入本地技能</button>
        <button class="btn" onclick="skGenAsk()">让 AI 写一个</button>
        <span class="dim small" style="flex:1">导入支持文件夹、zip、单个 SKILL.md；
          AI 写的会先给你过一遍再保存。</span>
      </div>
    </div>
  </div>`;
}

/* ---- 卡片一：远程库 ---- */
function skRemoteCard(k) {
  const srcs = k.sources || [];
  if (!k.browseSrc && srcs.length) k.browseSrc = srcs[0].id;
  const cur = srcs.find(s => s.id === k.browseSrc) || {};
  const b = k.browse || {};
  const items = b.items || [];
  return `
  <div class="card" style="margin-bottom:16px">
    <h3>远程技能库 <span class="spacer"></span>
      <span class="tag info">Agent Skills 开放标准</span></h3>
    <div class="body">
      <div class="row wrap" style="margin-bottom:10px">
        <select id="skSrc" style="max-width:340px" onchange="skillsBrowse()">
          ${srcs.map(s => `<option value="${esc(s.id)}"${s.id === k.browseSrc
            ? ' selected' : ''}>${esc(s.name)}</option>`).join('')}
        </select>
        <button class="btn" onclick="skillsBrowse()">重新加载</button>
        <span class="dim small" style="flex:1">${esc(cur.note || '')}</span>
      </div>
      ${b.loading ? '<div class="empty"><span class="busy"></span> 正在连 GitHub…</div>'
        : b.error ? `<div class="empty" style="color:var(--bad)">
            ${esc(b.error)}<br><span class="dim small">换一个库或稍后再试；
            别的页签不受影响。</span></div>`
        : !items.length ? '<div class="empty">选好库后点「重新加载」看看里面有什么。</div>'
        : `<ul class="list" style="border:1px solid var(--line);border-radius:9px">
            ${items.map((it, i) => `
              <li><div class="main">
                <div class="title">${esc(it.name)}
                  ${it.installed ? '<span class="tag ok">已安装</span>' : ''}
                  <span class="tag">${esc(it.kind === 'file' ? '单文件' : '目录')}</span>
                </div>
                <div class="sub">${esc(it.description || '（这个库没给说明）')}</div>
              </div>
              <div class="acts">
                <button class="btn sm${it.installed ? '' : ' pri'}"
                  ${it.installed ? 'disabled' : ''}
                  onclick="skillsInstall(${i})">${it.installed ? '已装' : '安装'}</button>
              </div></li>`).join('')}
          </ul>`}
    </div>
  </div>`;
}

/* ---- 卡片二：本地已装 ---- */
function skLocalCard(k) {
  const ls = k.local || [];
  return `
  <div class="card" style="margin-bottom:16px">
    <h3>已安装到本地 <span class="spacer"></span>
      <span class="tag">${ls.length} 个</span></h3>
    <div class="body">
      <p class="dim small" style="margin-top:0">
        用户级 <span class="mono">${esc((k.dirs || {}).user || '')}</span> ·
        项目级 <span class="mono">${esc((k.dirs || {}).project || '')}</span>
        ——装在这里的技能，对话里的 AI 会自动匹配，页面里的调用则要靠下面那张表绑定。</p>
      ${!ls.length ? '<div class="empty">一个都没装。上面选个库装一个试试。</div>'
        : `<ul class="list" style="border:1px solid var(--line);border-radius:9px">
            ${ls.map((s, i) => `
              <li><div class="main">
                <div class="title">${esc(s.name)}
                  <span class="tag ${s.scope === 'user' ? 'info' : 'accent'}">${
                    s.scope === 'user' ? '用户级' : '项目级'}</span>
                  <span class="tag">${s.files} 文件</span>
                  <span class="tag${s.body_chars > k.maxInject ? ' warn' : ''}">正文 ${
                    s.body_chars} 字${s.body_chars > k.maxInject
                      ? '（超注入上限，绑定后会被截断）' : ''}</span>
                </div>
                <div class="sub">${esc(s.description || '（没写说明）')}
                  ${(s.refs || []).length ? `<br><span class="dim">
                    项目里 ${s.refs.length} 处在用它：
                    <span class="mono">${esc(s.refs[0].path)}</span>${
                      s.refs.length > 1 ? ' 等' : ''}</span>` : ''}</div>
              </div>
              <div class="acts">
                <button class="btn sm" onclick="skDetail('${esc(s.dir)}', '${
                  esc(s.scope)}')">详情</button>
                <button class="btn sm danger" onclick="skillsUninstallAsk(${i})">卸载</button>
              </div></li>`).join('')}
          </ul>`}
    </div>
  </div>`;
}

/* ---- 卡片三：AI 调用点绑定 ---- */
function skBindCard(k) {
  const slots = k.slots || [];
  const ls = k.local || [];
  // 停用后仍保留的下拉选择（见 skillsBindRow 的 keepSel 说明）
  const keep = k.keepSel || {};
  return `
  <div class="card">
    <h3>AI 调用点 · 技能绑定 <span class="spacer"></span>
      <span class="tag ${slots.filter(s => s.enabled).length ? 'ok' : ''}">${
        slots.filter(s => s.enabled).length} / ${slots.length} 已启用</span></h3>
    <div class="body">
      <p class="dim small" style="margin-top:0">
        系统里每一处调模型的地方都列在这儿。给某个档位勾上技能后，
        那次调用的 system 提示词前面会拼上技能正文——
        <b>不绑定就不会生效</b>，装了也只是躺在磁盘上。
        改动即时保存，不用点保存按钮。</p>
      ${!slots.length ? '<div class="empty">没读到档位清单。</div>' : `
      <table class="t">
        <tr><th style="width:150px">调用点</th><th style="width:34%">干什么用</th>
          <th style="width:80px">用技能</th><th>选哪个技能</th>
          <th style="width:150px">每次注入</th></tr>
        ${slots.map((s, i) => {
          // 已停用时下拉仍回显上次选的技能，用户下次想再启用不用重新找
          const cur = s.skill || keep[s.slot] || '';
          return `
          <tr>
            <td><b>${esc(s.label)}</b><br>
              <span class="mono dim small">${esc(s.slot)}</span></td>
            <td class="small dim">${esc(s.hint)}</td>
            <td><input type="checkbox" id="skOn_${i}"${s.enabled ? ' checked' : ''}
              ${ls.length ? '' : ' disabled'} onchange="skillsBindRow(${i},'on')"></td>
            <td><select id="skSel_${i}" onchange="skillsBindRow(${i},'sel')"
              ${s.enabled ? '' : ' style="opacity:.5"'}>
              <option value="">— 不使用 —</option>
              ${ls.map(x => `<option value="${esc(x.name)}"${
                cur === x.name ? ' selected' : ''}>${esc(x.name)}${
                x.scope === 'project' ? '（项目级）' : ''}</option>`).join('')}
            </select>
            ${s.missing ? `<div class="small" style="color:var(--bad)">
              技能「${esc(s.skill)}」已不在本地，这次调用不会注入</div>` : ''}
            ${!s.enabled && cur && !s.missing ? `<div class="small dim">
              已选「${esc(cur)}」但没启用，勾上左边的开关才生效</div>` : ''}</td>
            <td class="small">${s.enabled
              ? (s.full_chars > k.maxInject
                  ? `<span class="tag warn">${s.inject_chars} 字（原文 ${s.full_chars}，已截断）</span>`
                  : `<span class="tag ok">${s.inject_chars} 字</span>`)
              : '<span class="dim">—</span>'}</td>
          </tr>`;
        }).join('')}
      </table>`}
      ${!ls.length ? '<p class="dim small" style="margin:10px 0 0">' +
        '本地还没有技能，先到上面装一个。</p>' : ''}
    </div>
  </div>`;
}

/* 下拉换库即加载：用户说的「选哪个库，然后加载里面的 skills」就是这个动作 */
async function skillsBrowse() {
  const k = skInit();
  const sel = $('#skSrc');
  if (sel) k.browseSrc = sel.value;
  const sid = k.browseSrc || (k.sources[0] || {}).id;
  if (!sid) return;
  k.browseSrc = sid;
  k.browse = { loading: true, items: [], error: '' };
  render();
  try {
    const r = await api('/api/skills/browse', { method: 'POST', body: { source: sid } });
    k.browse = { loading: false, items: r.items || [], error: r.error || '' };
  } catch (e) {
    k.browse = { loading: false, items: [], error: e.message || '加载失败' };
  }
  render();
}

async function skillsInstall(i, asName) {
  const k = skInit();
  const it = ((k.browse || {}).items || [])[i];
  if (!it) return;
  toast('正在下载 ' + it.name + '…');
  const body = { source: k.browseSrc, name: it.name };
  if (asName) body.as_name = asName;
  let r = null;
  try {
    r = await api('/api/skills/install', { method: 'POST', body });
  } catch (e) {
    // 重名不是错误，是「请改个名」。服务端用 409 + name_taken 区分，
    // 这里弹改名框而不是弹报错——否则用户只能自己去翻目录看占没占。
    const msg = e.message || '';
    if (/已经存在|同名目录|重名/.test(msg)) return skRenameAsk(i, it, msg);
    toast(msg || '安装失败', 'err');
    return;
  }
  const warn = r.warn || [];
  toast('已安装：' + ((r.installed || {}).dir || it.name), 'ok');
  await loadSkills();
  await skillsBrowse();
  render();
  if (warn.length) {
    toast('这个技能带脚本行为：' + warn.slice(0, 3)
      .map(w => w.why).join('、'), 'warn');
  }
}

function skRenameAsk(i, it, why) {
  const guess = it.name + '-2';
  openModal('这个名字已经占了', `
    <p style="margin-top:0">${esc(why)}</p>
    <p class="dim small">换个名字再装，原来的不会被覆盖。</p>
    <label class="f"><span>装成什么名字</span>
      <input type="text" id="skAsName" value="${esc(guess)}"></label>
    <div id="skReErr" class="small" style="display:none;background:var(--bad-soft);
      color:var(--bad);border-radius:8px;padding:8px 11px;
      word-break:break-all"></div>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn pri" onclick="skRenameGo(${i})">用这个名字装</button>`);
}

async function skRenameGo(i) {
  const nm = (($('#skAsName') || {}).value || '').trim();
  const err = $('#skReErr');
  if (!nm) return toast('名字不能为空', 'err');
  if (err) err.style.display = 'none';
  closeModal();
  await skillsInstall(i, nm);
}

function skillsUninstallAsk(i) {
  const k = skInit();
  const s = (k.local || [])[i];
  if (!s) return;
  confirmDialog('卸载技能', `
    <p style="margin-top:0">目标：<b>${esc(s.name)}</b></p>
    <p class="dim small" style="margin:0 0 10px">
      这会删除整个目录 <span class="mono">${esc(s.dir)}</span>
      （${s.scope === 'user' ? '用户级' : '项目级'}，共 ${s.files} 个文件），不可恢复。</p>
    <label class="f"><span>请输入目录名 <span class="mono">${esc(s.dir)}</span> 确认删除</span>
      <input type="text" id="skDelName" placeholder="${esc(s.dir)}"></label>`,
    '确认删除', async () => {
      const v = (($('#skDelName') || {}).value || '').trim();
      if (v !== s.dir) throw new Error('输入的名字不一致，已取消');
      await api('/api/skills/uninstall',
        { method: 'POST', body: { name: s.dir, scope: s.scope, confirm_name: v } });
      await loadSkills();
      render();
      toast('已卸载 ' + s.dir, 'ok');
    });
}

/* 一行一个绑定，改动立刻写库。
   不设「保存」按钮：这类表格式配置最容易出的就是「改了没保存」，
   而它又不像新建世界那样需要反复试，即时保存没有副作用。

   ⚠ 复选框和下拉是**一对**语义（选哪个 + 开不开），但两者都会触发提交。
   曾经两个控件都直接提交，于是用户顺着表格从左往右操作（先选技能、再勾开关）时，
   第一步选的技能就带着 enabled=false 提交了 —— 后端 save_binding 见
   「not enabled」把整条绑定删掉，前端弹「已停用：世界构建」并把整行恢复成
   「不使用」；反过来先勾开关又会被守卫拦下并弹回。
   **两条路都走不通 ＝ 这个功能根本启用不了任何技能。**
   现在按触发源判断意图：在下拉里做了选择就是明确表态，选了技能即启用、
   选「不使用」即停用；开关只负责「用/不用当前选的这个」。 */
async function skillsBindRow(i, src) {
  const k = skInit();
  const s = (k.slots || [])[i];
  if (!s) return;
  const on = $('#skOn_' + i);
  const sel = $('#skSel_' + i);
  if (!on || !sel) return;
  const skill = sel.value || '';
  let enabled;
  if (src === 'sel') {
    // 在下拉里选了一个具体技能 = 用户要用它，别让他再回来勾一次开关
    enabled = !!skill;
  } else {
    enabled = !!on.checked;
    if (enabled && !skill) {
      on.checked = false;
      return toast('先选一个技能，再打开开关', 'err');
    }
  }
  if (on.checked !== enabled) on.checked = enabled;
  // 停用不抹掉下拉的选择：下次想再启用不用重新找。
  // 记在前端状态里而不是库里 —— 库里的语义是「没启用就不留条目」，
  // 不为了一个界面回显去改绑定的存储契约。
  k.keepSel = k.keepSel || {};
  if (enabled) delete k.keepSel[s.slot];
  else if (skill) k.keepSel[s.slot] = skill;
  else delete k.keepSel[s.slot];
  try {
    await api('/api/skills/bind',
      { method: 'POST', body: { slot: s.slot, skill, enabled } });
    toast(enabled ? '已启用：' + s.label + ' → ' + skill : '已停用：' + s.label, 'ok');
  } catch (e) {
    toast(e.message || '保存失败', 'err');
  }
  await loadSkills();
  render();
}


/* ============================================================
   技能库 · 导入 / AI 生成 / 详情
   ============================================================ */

/* 导入分三步：选 → 看清是什么 → 落盘。
   不一键直装的理由：装了以后用户很难知道里面有什么，
   而这一步之前他会往机器里放一个来源不明的文件夹。 */
function skImportAsk() {
  openModal('导入本地技能', `
    <p class="dim small" style="margin-top:0">
      技能就是一个含 <span class="mono">SKILL.md</span> 的文件夹。
      三种都能导：整个文件夹、zip 压缩包、单个 SKILL.md。</p>

    <div class="gtabs" style="margin-bottom:12px">
      <button class="btn on" id="skTabFolder" onclick="skImportTab('folder')">文件夹</button>
      <button class="btn" id="skTabFile" onclick="skImportTab('file')">zip / md 文件</button>
    </div>

    <div id="skPaneFolder">
      <label class="f"><span>文件夹绝对路径（该文件夹里要有 SKILL.md）</span>
        <input type="text" id="skPath" placeholder="E:\\我的技能\\去AI味"></label>
      <p class="dim small" style="margin-top:-4px">
        名字默认取文件夹名，下一步可以改。（服务跑在哪台机器上，就读哪台机器的路径）</p>
    </div>

    <div id="skPaneFile" style="display:none">
      <label class="f"><span>选择文件（.zip 或 .md，单文件不超过 8MB）</span>
        <input type="file" id="skFile" accept=".zip,.md,.txt"
          onchange="skPickFile()"></label>
      <div id="skFileInfo" class="dim small"></div>
    </div>

    <div id="skErr" class="small" style="display:none;background:var(--bad-soft);
      color:var(--bad);border-radius:8px;padding:8px 11px;margin-top:12px;
      word-break:break-all"></div>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn pri" onclick="skImportPlan()">下一步：看一下要装什么</button>`,
    true);
  S.skI = { kind: 'folder', data: '', fname: '', plan: null };
}

function skImportTab(t) {
  const k = S.skI || (S.skI = {});
  k.kind = t;
  const f = $('#skTabFolder'), g = $('#skTabFile');
  if (f) f.className = 'btn' + (t === 'folder' ? ' on' : '');
  if (g) g.className = 'btn' + (t === 'file' ? ' on' : '');
  const pf = $('#skPaneFolder'), pfile = $('#skPaneFile');
  if (pf) pf.style.display = (t === 'folder') ? '' : 'none';
  if (pfile) pfile.style.display = (t === 'file') ? '' : 'none';
}

function skPickFile() {
  const inp = $('#skFile');
  const box = $('#skFileInfo');
  const f = inp && inp.files && inp.files[0];
  if (!f) { if (box) box.textContent = ''; return; }
  if (f.size > 8 * 1024 * 1024) {
    if (box) box.textContent = '这个文件 ' + Math.round(f.size / 1024 / 1024)
      + 'MB，超过 8MB，多半不是技能包。';
    return;
  }
  const rd = new FileReader();
  rd.onload = () => {
    const b64 = String(rd.result).split(',')[1] || '';
    const k = S.skI || (S.skI = {});
    k.data = b64;
    k.fname = f.name;
    k.kind = /\.zip$/i.test(f.name) ? 'zip' : 'md';
    if (box) box.textContent = '已选：' + f.name + '（'
      + Math.round(f.size / 1024) + ' KB，按 '
      + (k.kind === 'zip' ? 'zip 压缩包' : '单个 SKILL.md') + ' 解析）';
  };
  rd.readAsDataURL(f);
}

async function skImportPlan() {
  const k = S.skI || (S.skI = {});
  const err = $('#skErr');
  if (err) err.style.display = 'none';
  const body = { kind: k.kind };
  if (k.kind === 'folder') {
    body.path = (($('#skPath') || {}).value || '').trim();
    if (!body.path) return toast('先填文件夹路径', 'err');
  } else {
    if (!k.data) return toast('先选一个文件', 'err');
    body.data = k.data;
  }
  try {
    const r = await api('/api/skills/import_plan',
      { method: 'POST', body: Object.assign(body, { kind: k.kind }) });
    k.plan = r;
    skImportConfirm(r);
  } catch (e) {
    if (err) { err.textContent = e.message; err.style.display = 'block'; }
    else toast(e.message, 'err');
  }
}

/* 第二步：把「装成什么 / 几个文件 / 审计命中」全摊开让用户看 */
function skImportConfirm(r) {
  const au = r.audit || {};
  const warn = au.warn || [], block = au.block || [];
  const k = S.skI || (S.skI = {});
  k.scope = 'user';
  openModal('确认导入', `
    <div class="kv" style="margin-bottom:12px">
      <span class="k">技能名</span><span><b>${esc(r.dir)}</b></span>
      <span class="k">说明</span><span>${esc((r.meta || {}).description
        || '（SKILL.md 里没写 description）')}</span>
      <span class="k">文件</span><span>${r.file_count} 个 · 正文 ${
        (r.meta || {}).body_chars || 0} 字</span>
      <span class="k">装到</span><span class="mono">${
        esc(k.scope === 'project' ? (S.skills.dirs || {}).project
            : (S.skills.dirs || {}).user)}</span>
    </div>

    <label class="f"><span>技能名（可以改，中文也行）</span>
      <input type="text" id="skNewName" value="${esc(r.dir)}"></label>

    <label class="f"><span>装到哪一级</span>
      <select id="skScope" onchange="skScopeChange()">
        <option value="user">用户级 —— 所有项目都能用</option>
        <option value="project">项目级 —— 只在本项目生效</option>
      </select></label>

    <details style="margin-bottom:10px">
      <summary class="dim small" style="cursor:pointer">
        看文件清单（${r.file_count} 个）</summary>
      <div class="mono small" style="margin-top:6px;line-height:1.7;color:var(--ink-2)">
        ${(r.file_list || []).map(f => esc(f)).join('<br>')}
        ${r.file_count > (r.file_list || []).length ? '<br>…' : ''}</div>
    </details>

    ${block.length ? `<div class="card" style="margin:0 0 10px;background:var(--bad-soft);
      border-color:transparent"><div class="body">
      <div style="color:var(--bad);font-weight:600;margin-bottom:4px">
        这个技能里有危险指令</div>
      <div class="small" style="color:var(--bad)">${block.slice(0, 5).map(h =>
        esc(h.file) + ' — ' + esc(h.why)).join('<br>')}</div>
      <div class="dim small" style="margin-top:6px">
        这是你自己的文件，你可以确认后继续导入；但请先确认你信任它的来源。</div>
      </div></div>` : ''}
    ${warn.length ? `<div class="card" style="margin:0 0 10px;background:var(--warn-soft);
      border-color:transparent"><div class="body">
      <div style="color:var(--warn);font-weight:600;margin-bottom:4px">
        它会执行脚本或装包（不是错，但你要知道）</div>
      <div class="small" style="color:var(--warn)">${warn.slice(0, 5).map(h =>
        esc(h.file) + ' — ' + esc(h.why)).join('<br>')}</div>
      </div></div>` : ''}
    ${!block.length && !warn.length
      ? '<p class="dim small" style="margin:0">审计通过：没有危险指令，也没有脚本调用。</p>'
      : ''}
    <div id="skErr2" class="small" style="display:none;background:var(--bad-soft);
      color:var(--bad);border-radius:8px;padding:8px 11px;margin-top:10px;
      word-break:break-all"></div>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn${block.length ? ' danger' : ' pri'}"
       onclick="skImportCommit(${block.length ? 'true' : 'false'})">${
       block.length ? '我知道风险，仍然导入' : '确认导入'}</button>`, true);
}

function skScopeChange() {
  const el = $('#skScope');
  const k = S.skI || (S.skI = {});
  k.scope = el ? el.value : 'user';
}

async function skImportCommit(force) {
  const k = S.skI || (S.skI = {});
  const dir = (($('#skNewName') || {}).value || '').trim();
  const err = $('#skErr2');
  if (err) err.style.display = 'none';
  try {
    const r = await api('/api/skills/import', { method: 'POST',
      body: { plan_id: k.plan.plan_id, dir, scope: k.scope, force: !!force } });
    closeModal();
    await loadSkills(); render();
    toast('已导入技能「' + ((r.installed || {}).dir || dir) + '」', 'ok');
  } catch (e) {
    if (err) { err.textContent = e.message; err.style.display = 'block'; }
    else toast(e.message, 'err');
  }
}

/* ---- 让 AI 写 ---- */
function skGenAsk() {
  S.skG = null;
  openModal('让 AI 写一个技能', `
    <p class="dim small" style="margin-top:0">
      说清「这个技能该在什么时候管什么」，AI 会照 Agent Skill 的格式
      写出一份 SKILL.md。写出来先给你看，你改完再保存——不会直接落盘。</p>
    ${area('你要什么技能', 'skBrief',
      '', '例如：每一章写完后，检查有没有 AI 腔——排比堆砌、'
        + '「不禁」「仿佛」用太滥、每段都恰好三句。命中就列出来让我改。',
      5)}
    <p class="dim small" style="margin-top:-4px">
      越具体越好：写清触发时机、要检查什么、命中了怎么办。
      「写得生动一点」这种没法验证的说法，AI 也只能给你没法验证的条目。</p>
    <div id="skGErr" class="small" style="display:none;background:var(--bad-soft);
      color:var(--bad);border-radius:8px;padding:8px 11px;margin-top:10px;
      word-break:break-all"></div>`,
    `<button class="btn" onclick="closeModal()">取消</button>
     <button class="btn pri" onclick="skGenRun()">开始写</button>`, true);
}

async function skGenRun() {
  const brief = (($('#skBrief') || {}).value || '').trim();
  const err = $('#skGErr');
  if (err) err.style.display = 'none';
  if (!brief) return toast('先说说你要这个技能管什么', 'err');
  // 生成要跑模型，几十秒起步。换成日志面板让用户看得见在动。
  openModal('AI 正在写技能', `
    <p class="dim small" style="margin-top:0">模型在起草，几十秒的样子。</p>
    <div id="skGenLog"></div>`, '', true);
  await runJob('/api/skills/gen', { brief }, 'skGenLog', '起草技能',
    async (res) => {
      if (res) { S.skG = res; skGenPreview(res); }
    });
}

/* 看草稿 → 改名 → 保存 */
function skGenPreview(g) {
  const k = skInit();
  openModal('AI 草稿 · 看过再保存', `
    <p class="dim small" style="margin-top:0">
      下面就是完整的 SKILL.md，可以直接改。保存前不会动磁盘。</p>
    <div class="row wrap" style="margin-bottom:10px">
      <div style="flex:1"><label class="f" style="margin:0">
        <span>技能名</span>
        <input type="text" id="skGenName" value="${esc(g.dir || g.name)}"></label></div>
      <div style="width:200px"><label class="f" style="margin:0">
        <span>装到哪一级</span>
        <select id="skGenScope">
          <option value="user">用户级</option>
          <option value="project">项目级</option>
        </select></label></div>
    </div>
    <label class="f"><span>SKILL.md 全文（${g.body_chars || 0} 字${
      (g.body_chars || 0) > k.maxInject
        ? '，超过 ' + k.maxInject + ' 字注入上限，绑定后会被截断' : ''}）</span>
      <textarea id="skGenBody" rows="18" style="font-family:var(--mono);
        font-size:12.5px;line-height:1.7">${esc(g.content)}</textarea></label>
    <p class="dim small" style="margin:0">
      提示：<span class="mono">name</span> 与 <span class="mono">description</span>
      在开头两行之间，是技能被认出来的依据，别删。</p>
    <div id="skErr3" class="small" style="display:none;background:var(--bad-soft);
      color:var(--bad);border-radius:8px;padding:8px 11px;margin-top:10px;
      word-break:break-all"></div>`,
    `<button class="btn" onclick="closeModal()">不保存</button>
     <button class="btn" onclick="skGenAgain()">再写一版</button>
     <button class="btn pri" onclick="skGenSave()">保存到本地</button>`, true);
}

function skGenAgain() {
  closeModal();
  skGenAsk();
}

async function skGenSave() {
  const name = (($('#skGenName') || {}).value || '').trim();
  const content = (($('#skGenBody') || {}).value || '');
  const scope = (($('#skGenScope') || {}).value) || 'user';
  const err = $('#skErr3');
  if (err) err.style.display = 'none';
  if (!name) return toast('技能名不能为空', 'err');
  try {
    const r = await api('/api/skills/gen_save',
      { method: 'POST', body: { dir: name, content, scope } });
    closeModal();
    await loadSkills(); render();
    toast('已保存技能「' + ((r.installed || {}).dir || name) + '」', 'ok');
  } catch (e) {
    // 保存失败不关弹层：用户手改的内容还在 textarea 里，关了就白写了
    if (err) { err.textContent = e.message; err.style.display = 'block'; }
    else toast(e.message, 'err');
  }
}

/* ---- 技能详情 ---- */
async function skDetail(dir, scope) {
  let d;
  try {
    d = await api('/api/skills/detail?name=' + encodeURIComponent(dir));
  } catch (e) { return toast(e.message, 'err'); }
  const s = d.skill || {};
  const bs = d.bound_slots || [];
  openModal('技能详情 · ' + s.name, `
    <div class="kv" style="margin-bottom:12px">
      <span class="k">目录</span><span class="mono">${esc(s.dir)}（${
        s.scope === 'user' ? '用户级' : '项目级'}）</span>
      <span class="k">说明</span><span>${esc(s.description || '（没写）')}</span>
      <span class="k">正文</span><span>${s.body_chars} 字 · 注入时最多 ${
        d.max_inject_chars} 字</span>
      <span class="k">路径</span><span class="mono small">${esc(s.path)}</span>
    </div>

    <h4 style="margin:0 0 6px;font-size:13px">绑到哪些调用点</h4>
    ${!bs.length ? '<p class="dim small" style="margin:0 0 12px">还没绑到任何调用点。'
        + '装了不绑，页面里的模型调用收不到它——到下面「AI 调用点」那张表里勾一下。</p>'
      : `<ul class="list" style="border:1px solid var(--line);border-radius:9px;
          margin-bottom:12px">${bs.map(x => `<li><div class="main">
          <div class="title">${esc(x.label)} <span class="tag ${
            x.enabled ? 'ok' : ''}">${x.enabled ? '已启用' : '已停用'}</span></div>
          <div class="sub">${esc(x.hint)}</div></div></li>`).join('')}</ul>`}

    <h4 style="margin:0 0 6px;font-size:13px">项目里哪里引用了它</h4>
    ${!(d.refs || []).length ? '<p class="dim small" style="margin:0 0 12px">'
        + '源码和文档里都没提到它——说明只在被绑定的档位上起作用。</p>'
      : `<ul class="list" style="border:1px solid var(--line);border-radius:9px;
          margin-bottom:12px">${(d.refs || []).slice(0, 12).map(r => `<li>
          <div class="main"><div class="title mono" style="font-size:12.5px">
            ${esc(r.path)}:${r.line}</div>
          <div class="sub mono">${esc(r.text)}</div></div></li>`).join('')}</ul>`}

    ${(d.db_refs || []).length ? `
      <h4 style="margin:0 0 6px;font-size:13px">库里的记录</h4>
      <p class="dim small" style="margin:0 0 12px">
        这些行里的文本提到了它（可能是历史书、历史版本留下的）：<br>
        <span class="mono">${d.db_refs.slice(0, 10)
          .map(r => esc(r.table + '#' + (r.id == null ? '?' : r.id)
            + (r.novel_id ? '（书 ' + r.novel_id + '）' : ''))).join('、')}</span></p>`
      : ''}

    <h4 style="margin:0 0 6px;font-size:13px">正文预览</h4>
    <div class="prose sm" style="max-height:280px;overflow:auto;
      border:1px solid var(--line);border-radius:9px;padding:12px 14px;
      background:var(--panel-2)">${esc(d.body_preview || '（空）')}${
      d.truncated ? '\n\n…（后面还有，完整内容见 SKILL.md）' : ''}</div>`,
    `<button class="btn" onclick="closeModal()">关闭</button>`, true);
}


/* ============================================================
   启动
   ============================================================ */
boot();
