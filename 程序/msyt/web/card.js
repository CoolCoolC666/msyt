/* ---------------- 舆情卡片（优化报告 P2-6，2026-11-12） ----------------
   ★ 三层字段（详见《舆情卡片分类标准-v1》）：
     一 确定性字段 —— 程序算，AI 改不了
     二 研判信号   —— 程序算的异常
     三 AI 生成     —— 概述 / 诉求焦点 / 诉求主体 / 情绪对抗性 / 涉稳要素 / 处置建议
     四 人工        —— 可改并留痕；改过的不被自动重新生成覆盖
   ★ 风险等级是**规则算**的（card.level_of 是纯函数），不是模型说的。
   ★ 时效：卡片带数据快照指纹；站方一动 → 标「已过时」并给重生成按钮。
   ★ 批量靠指纹幂等：已有卡片且指纹没变就跳过，不烧额度。 */

const CARD_LEVELS = ['提示', '关注', '预警', '紧急'];

/* ★ 信号的中文说法**不在前端存一份** —— 服务端 card.SIGNAL_CN 是单一出处，
   卡片里带上 signal_labels 直接用（两边各存一份迟早跑偏）。 */

/* ---------- 小工具 ---------- */
function cardLevelBadge(level) {
  const i = CARD_LEVELS.indexOf(level);
  return '<span class="ocard-lv lv' + (i < 0 ? 0 : i) + '">' + esc(level || '—') + '</span>';
}

function cardStopBtns() {
  return Array.prototype.slice.call(document.querySelectorAll('.card-stop'));
}

function setCardState(txt) {
  ['anno-card-state', 'card-batch-state'].forEach(function (id) {
    const el = document.getElementById(id);
    if (el) el.textContent = txt;
  });
}

/* 轮询只会往一个 stateEl 写状态；检索页与标注台各有一个 → 用个壳把两个都写 */
const cardStateEl = { set textContent(v) { setCardState(v); } };

/* ---------- 详情抽屉里的卡片 ---------- */
async function renderCard(tid) {
  const box = document.getElementById('dw-card');
  if (!box) return;
  box.innerHTML = '<div class="secttl">舆情卡片</div><div class="hint">读取中…</div>';
  let d;
  try {
    d = await api('/api/threads/' + encodeURIComponent(tid) + '/card');
  } catch (e) {
    box.innerHTML = '<div class="secttl">舆情卡片</div><div class="errbox">' + esc(e.message) + '</div>';
    return;
  }
  const c = d.card || {};
  if (!d.exists || !c.facts) {
    box.innerHTML = '<div class="secttl">舆情卡片</div>'
      + '<div class="hint">这条还没有卡片。生成后存到服务器；站方有更新时会提示「已过时」。</div>'
      + '<div class="row"><button class="primary" id="ocard-gen">生成舆情卡片</button>'
      + '<span class="hint">要调一次模型（消耗额度）</span></div>';
    const b = document.getElementById('ocard-gen');
    if (b) b.onclick = function () { genCard(tid, false); };
    return;
  }
  box.innerHTML = cardHtml(c);
  bindCard(tid, c);
}

function cardHtml(c) {
  const f = c.facts || {}, a = c.ai || {};
  const j = c.judge || {}, adv = c.advice || {};
  const labels = c.signal_labels || {};
  const hits = Object.keys(labels);
  let h = '';
  if (c.stale) {
    h += '<div class="warnbox">⚠ ' + esc(c.stale_reason || '卡片已过时')
      + '（卡片快照 ' + esc(c.fingerprint || '') + ' → 现在 ' + esc(c.current_fingerprint || '') + '）</div>';
  }
  h += '<div class="ocard-head">' + cardLevelBadge(j.level)
    + '<span class="ocard-why">' + esc(j.why || '') + '</span>'
    + (c.edited_by ? '<span class="badge yellow">人工改过 · ' + esc(c.edited_by) + '</span>' : '')
    + '</div>';
  /* 第一层：站方事实（程序算，AI 改不了） */
  h += '<div class="ocard-facts"><span class="lbl">站方事实</span>'
    + '<span>类别 ' + esc(f.category || '—') + '</span>'
    + '<span>部门 ' + esc(f.dept_name || '—')
    + (f.extra_dept_names ? '（协办 ' + esc(f.extra_dept_names) + '）' : '') + '</span>'
    + '<span>状态 ' + esc(f.state || '（空）') + '</span>'
    + '<span>时效灯 ' + esc(f.light_name || f.light_code || '—') + '</span>'
    + '<span>楼层 ' + num(f.posts_n) + ' · 回复 ' + num(f.replies) + ' · 字数 ' + num(f.chars) + '</span>'
    + '<span>历时 ' + (f.elapsed_days === null || f.elapsed_days === undefined
        ? '—' : num(f.elapsed_days) + ' 天') + '</span>'
    + '<span>楼主追问 ' + num(f.ask_rounds) + ' 轮</span>'
    + '</div>';
  /* 第二层：研判信号 */
  h += '<div class="ocard-facts"><span class="lbl">研判信号</span>'
    + (hits.length ? hits.map(function (k) {
        return '<span class="ocard-sig">' + esc(labels[k] || k) + '</span>';
      }).join('') : '<span class="dim">没有命中</span>')
    + '</div>';
  /* 第三层：AI 生成 */
  h += '<div class="ocard-ai">'
    + '<div class="ocard-ai-row"><span class="lbl">概述</span><span>' + esc(a.summary || '（无）') + '</span></div>'
    + '<div class="ocard-ai-row"><span class="lbl">诉求焦点</span><span>' + esc(a.focus || '（无）') + '</span></div>'
    + '<div class="ocard-ai-row"><span class="lbl">诉求主体</span><span>' + esc(a.subject || '（无）') + '</span></div>'
    + '<div class="ocard-ai-row"><span class="lbl">情绪对抗性</span><span>' + esc(a.emotion || '（无）') + '</span></div>'
    + '<div class="ocard-ai-row"><span class="lbl">事件性质</span><span>' + esc(a.nature || '（无）') + '</span></div>'
    + '<div class="ocard-ai-row"><span class="lbl">涉稳要素</span><span>' + esc(a.stability || '（无）') + '</span></div>'
    + '<div class="ocard-ai-row"><span class="lbl">处置建议</span><span>' + esc(adv.advice || '（无）')
    + (adv.advice_reason ? '（' + esc(adv.advice_reason) + '）' : '') + '</span></div>';
  if (a.evidence) {
    h += '<div class="ocard-ai-row"><span class="lbl">原文依据</span><span class="dim">'
      + esc(a.evidence) + '</span></div>';
  }
  if (a.urgent) {
    h += '<div class="ocard-ai-row"><span class="lbl">紧急依据</span><span>'
      + esc(a.urgent_reason || '') + '</span></div>';
  }
  h += '</div>';
  const doubts = c.doubts || [];
  if (doubts.length) {
    h += '<div class="hint" style="color:var(--warn)">⚠ 有疑点（不阻塞显示，看的人自己判断）：'
      + esc(doubts.join('；')) + '</div>';
  }
  const dropped = a.dropped || [];
  if (dropped.length) {
    h += '<div class="hint">已丢弃的越界取值：' + esc(dropped.join('；')) + '</div>';
  }
  /* 第四层：人工修订 */
  h += '<details class="adv"><summary>人工修订（留痕；改过就不会被自动重新生成覆盖）</summary>'
    + '<div class="row"><span class="lbl">风险等级</span><select id="ocard-lv">'
    + CARD_LEVELS.map(function (lv) {
        return '<option value="' + esc(lv) + '"' + (lv === j.level ? ' selected' : '')
          + '>' + esc(lv) + '</option>';
      }).join('')
    + '</select><span class="lbl">处置建议</span><input id="ocard-advice" value="' + esc(adv.advice || '') + '">'
    + '<span class="lbl">建议理由</span><input id="ocard-advwhy" value="' + esc(adv.advice_reason || '') + '">'
    + '</div>'
    + '<div class="row"><span class="lbl">概述</span><input id="ocard-summary" value="' + esc(a.summary || '') + '">'
    + '</div>'
    + '<div class="row"><span class="lbl">人工备注</span><input id="ocard-note" value="' + esc(a.human_note || '') + '">'
    + '</div>'
    + '<div class="row"><button class="primary" id="ocard-save">保存修订</button>'
    + '<span class="hint">保存后会记下是谁改的</span></div></details>';
  h += '<div class="row" style="margin-top:10px">'
    + '<button class="ghost" id="ocard-regen">'
    + (c.edited_by ? '强制重生成（覆盖人工修订）' : '重新生成') + '</button>'
    + '<span class="hint">模型 ' + esc(c.model || '—') + ' ｜ 提示词 ' + esc(c.prompt_version || '—')
    + ' ｜ 首次生成 ' + esc(c.created_at || '—')
    + (c.updated_at && c.updated_at !== c.created_at
        ? ' ｜ 更新于 ' + esc(c.updated_at) : '')
    + (c.edited_at ? ' ｜ 人工改于 ' + esc(c.edited_at) : '') + '</span></div>';
  return h;
}

function bindCard(tid, c) {
  const regen = document.getElementById('ocard-regen');
  if (regen) {
    regen.onclick = function () {
      if (c.edited_by && !confirm('这条卡片人工改过（' + c.edited_by
        + '）。\n强制重生成会覆盖人工修订，确定？')) return;
      genCard(tid, true);
    };
  }
  const save = document.getElementById('ocard-save');
  if (save) save.onclick = function () { saveCard(tid); };
}

async function genCard(tid, force) {
  const box = document.getElementById('dw-card');
  if (box) {
    box.innerHTML = '<div class="secttl">舆情卡片</div>'
      + '<div class="hint">生成中…（要调一次模型，等几秒到几十秒）</div>';
  }
  try {
    const body = { tid: tid, force: !!force };
    const mine = localAiPayload();
    if (mine) body.ai = mine;      /* 用我自己的 key（只在这一请求里用） */
    const c = await api('/api/anno/card/generate', {
      method: 'POST', body: JSON.stringify(body),
    });
    toast(c.skipped ? (c.reason || '已有卡片且没过时 → 跳过（没烧额度）')
          : '卡片已生成并保存到服务器');
    await renderCard(tid);
  } catch (e) {
    if (box) {
      box.innerHTML = '<div class="secttl">舆情卡片</div><div class="errbox">'
        + esc(e.message) + '</div>';
    }
    toast(e.message, true);
  }
}

async function saveCard(tid) {
  const val = function (id) {
    const el = document.getElementById(id);
    return el ? el.value : '';
  };
  const patch = {
    level: val('ocard-lv'),
    advice: val('ocard-advice'),
    advice_reason: val('ocard-advwhy'),
    summary: val('ocard-summary'),
    note: val('ocard-note'),
  };
  try {
    await api('/api/anno/card/' + encodeURIComponent(tid) + '/edit', {
      method: 'POST', body: JSON.stringify({ patch: patch }),
    });
    toast('已保存（留痕）');
    await renderCard(tid);
  } catch (e) { toast(e.message, true); }
}

/* ---------- 批量 ---------- */
function currentPageTids() {
  return Array.prototype.map.call(
    document.querySelectorAll('#t-threads tbody tr.clickable'),
    function (tr) { return parseInt(tr.getAttribute('data-tid'), 10); }
  ).filter(function (x) { return !isNaN(x); });
}

function queueTids() {
  const q = (typeof annoState !== 'undefined' && annoState.queue) ? annoState.queue : [];
  return q.map(function (x) { return x.tid; });
}

function cardPollOpts() {
  return {
    view: logCard,
    stateEl: cardStateEl,
    stopBtn: 'anno-card-stop',
    doneMsg: function (res, j) {
      return '舆情卡片批量结束：成功 ' + (res.ok || 0) + '，跳过 ' + (res.skipped || 0)
        + '，失败 ' + (res.failed || 0)
        + (res.stopped ? '（已停手：' + res.stopped + '）' : '')
        + (j.state === 'failed' ? '（任务出错：' + (j.error || '') + '）' : '');
    },
    onDone: function () {
      cardStopBtns().forEach(function (b) { b.classList.add('hidden'); });
      loadCardStats();
    },
  };
}

async function runCardBatch(tids, from) {
  const list = tids.slice(0, 20);
  if (!list.length) { toast('没有可生成的帖子', true); return; }
  if (!confirm('给这 ' + list.length + ' 条（' + from + '）生成舆情卡片？\n'
    + '每条调一次模型，会消耗额度；已有卡片且没过时的会跳过。')) return;
  if (logCard) logCard.clear('正在启动…');
  try {
    const body = { tids: list, limit: list.length };
    const mine = localAiPayload();
    if (mine) body.ai = mine;
    const r = await api('/api/jobs/card-batch', {
      method: 'POST', body: JSON.stringify(body),
    });
    cardJobId = (r.job || {}).id || '';
    if (!cardJobId) throw new Error('没拿到任务号');
    cardStopBtns().forEach(function (b) { b.classList.remove('hidden'); });
    await pollJob(cardJobId, cardPollOpts());
  } catch (e) { toast(e.message, true); }
}

/* 进标注台时：接上**还在跑**的卡片批量任务（刷新不丢进度） */
async function resumeCardJob() {
  try {
    const r = await api('/api/jobs');
    const jobs = (r.items || []).filter(function (j) { return j.kind === 'card_batch'; });
    if (!jobs.length) return;
    const j = jobs[0];                 /* 后端按开始时间倒序 → 第一个就是最新的 */
    if (j.state !== 'running') return;
    cardJobId = j.id;
    cardStopBtns().forEach(function (b) { b.classList.remove('hidden'); });
    setCardState(j.last_progress || '运行中…');
    await pollJob(j.id, cardPollOpts());
  } catch (e) { /* 没有在跑的任务就算了，不打扰 */ }
}

async function stopCardJob() {
  if (!cardJobId) return;
  try {
    await api('/api/jobs/' + encodeURIComponent(cardJobId) + '/stop', { method: 'POST' });
    toast('已请求停止（这一条跑完就停）');
  } catch (e) { toast(e.message, true); }
}

async function loadCardStats() {
  const el = document.getElementById('anno-card-stats');
  if (!el) return;
  try {
    const s = await api('/api/anno/card/stats');
    const lv = s.by_level || {};
    el.textContent = '已有卡片 ' + num(s.cards) + ' 条（已采正文 ' + num(s.detail_done)
      + ' 条）'
      + ' ｜ 紧急 ' + (lv['紧急'] || 0) + ' · 预警 ' + (lv['预警'] || 0)
      + ' · 关注 ' + (lv['关注'] || 0) + ' · 提示 ' + (lv['提示'] || 0)
      + ' ｜ 人工改过 ' + num(s.edited) + ' 条';
  } catch (e) { /* 不打扰 */ }
}

/* ---------- 初始化 ---------- */
let cardJobId = '';
const logCard = makeLogView(['#anno-card-log', '#card-log'], { empty: '（还没有日志）' });

(function initCardUi() {
  const bind = function (id, fn) {
    const el = document.getElementById(id);
    if (el) el.onclick = fn;
  };
  bind('btn-card-batch', function () { runCardBatch(currentPageTids(), '当前页'); });
  bind('card-stop', stopCardJob);
  bind('anno-card-batch', function () { runCardBatch(queueTids(), '当前队列'); });
  bind('anno-card-stop', stopCardJob);
})();
