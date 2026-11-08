/* ---------------- AI 初注（AI 初筛）· 优化报告 P2-5（2026-11-08）----------------
   ★ AI 只给**建议**，作数的是人工标注：采纳走正常提交路径（anno.submit）。
   ★ 密钥只存服务器，前端既拿不到也看不到（接口只回「有没有配」）。
   ★ 有疑点的建议（结论待核验 / 标签被丢弃）**必须人工确认**：
     批量采纳会自动跳过它们，单条采纳会弹二次确认并标出疑点。 */

/* ---------- 我的 API（存在「本人浏览器」里，组员与管理员都能用） ----------
   ★ 服务器上那份是**管理员的默认**（全组共用）；这份是**自己的**：
     存在 localStorage，费用各归各人，换模型不用管理员改服务器。
   ★ 这份**不发给服务器保存**：只在 AI 请求里带过去，用完就走
     （服务端不落盘、不入库、不写日志、不进任务参数）。
   ★ 边界：key 存在这台电脑的浏览器里 → 公用电脑别填。
*/
const AI_LOCAL_KEY = 'msyt_ai_local';

function getLocalAiCfg() {
  try {
    const c = JSON.parse(localStorage.getItem(AI_LOCAL_KEY) || 'null');
    if (!c || !c.api_key) return null;      // 没填 key = 不用自己的，走服务器默认
    return {
      protocol: c.protocol || 'openai',
      base_url: c.base_url || '',
      model: c.model || '',
      api_key: c.api_key || '',
      timeout: Number(c.timeout || 60),
    };
  } catch (e) { return null; }
}

/** 要带给服务器的**个人配置**；没有就返回 null（走服务器默认） */
function localAiPayload() { return getLocalAiCfg(); }

/* ---------- 领活范围筛选（与检索页同一套口径） ---------- */
function cloneFilterOptions() {
  const map = [['ab-category', 'cl-category'], ['ab-state', 'cl-state'],
    ['ab-group', 'cl-group'], ['ab-district', 'cl-district']];
  map.forEach(function (pair) {
    const s = document.getElementById(pair[0]);
    const d = document.getElementById(pair[1]);
    if (!s || !d) return;
    d.innerHTML = '';
    Array.prototype.forEach.call(s.options, function (o) {
      const n = document.createElement('option');
      n.value = o.value;
      n.textContent = o.textContent;
      d.appendChild(n);
    });
  });
}

/** 领活范围的下拉要有选项 → 先确保字典（meta）加载过，再克隆。
    ★ 以前是在脚本加载时克隆一次，那时检索页的字典还没填 → 四个下拉全是空的。 */
async function ensureAnnoFilters() {
  if (!state.meta) {
    try { await loadMeta(); } catch (e) { return; }
  }
  cloneFilterOptions();
}

/* ---------- AI 结果卡（标注台里显示） ---------- */
function aiCardHtml(a) {
  if (!a || !a.verdict) return '';
  const dims = (annoState.vocab && annoState.vocab.dims) || {};
  const tags = (a.tags || []).map(function (t) {
    return esc((dims[t.dim] || t.dim) + '：' + t.tag);
  }).join('，') || '（无）';
  const doubts = (a.doubts || []).length
    ? '<div class="hint" style="color:var(--warn)">⚠ <b>有疑点，需人工确认</b>：'
      + esc((a.doubts || []).join('；')) + '</div>'
    : '';
  const dropped = (a.dropped || []).length
    ? '<div class="hint">被丢弃的标签（不在候选清单里，没采纳）：'
      + esc((a.dropped || []).map(function (d) { return d.dim + '：' + d.tag; }).join('，'))
      + '</div>'
    : '';
  const adopted = a.status === 'adopted'
    ? '<div class="hint" style="color:var(--accent)">✓ 已采纳为人工标注</div>' : '';
  return '<div class="anno-ai" id="anno-ai-card">'
    + '<div class="secttl">AI 初注建议</div>'
    + '<div><b>建议结论：</b>' + esc(a.verdict || '（无）') + '</div>'
    + '<div><b>理由：</b>' + esc(a.reason || '（无）') + '</div>'
    + '<div><b>标签建议：</b>' + tags + '</div>'
    + doubts + dropped + adopted
    + '<div class="hint">模型 ' + esc(a.model || '—') + ' ｜ 协议 ' + esc(a.protocol || '—')
      + ' ｜ 提示词 ' + esc(a.prompt_version || '—') + ' ｜ 生成 ' + esc(a.updated_at || '—') + '</div>'
    + '<div class="row">'
    + '<button class="primary tiny" id="anno-ai-adopt">采纳为人工标注</button>'
    + '<span class="hint">采纳＝走正常提交路径（人工标签优先覆盖规则标签），事后可再改。</span>'
    + '</div></div>';
}

/* ---------- 单条：AI 初注 ---------- */
async function runAiSuggest(tid) {
  const hint = document.getElementById('anno-ai-hint');
  if (hint) hint.textContent = '正在问模型，等一下…';
  try {
    const body = { tid: tid };
    const mine = localAiPayload();
    if (mine) body.ai = mine;          // ★ 用我自己的 key（存在我浏览器里）
    const r = await api('/api/anno/ai/suggest', {
      method: 'POST', body: JSON.stringify(body)
    });
    toast('AI 建议：' + (r.verdict || '（无结论）')
      + (r.needs_review ? '（有疑点，要人工确认）' : ''));
    await openAnno(tid);
  } catch (e) {
    toast(e.message, true);
    if (hint) hint.textContent = e.message;
  }
}

/* ---------- 单条：采纳（必须二次确认） ---------- */
async function adoptAi(tid) {
  const it = (annoState && annoState.item) || {};
  const a = it.ai || {};
  const dims = (annoState.vocab && annoState.vocab.dims) || {};
  const tags = (a.tags || []).map(function (t) {
    return (dims[t.dim] || t.dim) + '：' + t.tag;
  }).join('，') || '（无标签）';
  let msg = '把 AI 建议采纳为人工标注？\n\n'
    + '结论：' + (a.verdict || '（无）') + '\n'
    + '理由：' + (a.reason || '（无）') + '\n'
    + '标签：' + tags;
  if (a.needs_review) {
    msg += '\n\n⚠ 这条**有疑点**（' + ((a.doubts || []).join('；')) + '），请自己核对后再采纳。';
  }
  if (!confirm(msg)) return;
  try {
    await api('/api/anno/ai/adopt', {
      method: 'POST', body: JSON.stringify({ tid: tid })
    });
    toast('已采纳（走正常提交路径，人工标签已生效）');
    await loadAnnoQueue();
    await openAnno(tid);
  } catch (e) { toast(e.message, true); }
}

/* ---------- 批量：AI 初注（后台任务 + 逐条回显） ----------
   ★ 2026-11-08 用户反馈「这个进程不显示是比较尴尬的」：
     一条一次模型调用，20 条要跑好几分钟 → 同步跑就是页面看着像死了。
     现在走后台任务（复用采集任务那套 job 机制），逐条回显在下面的日志框里。 */
let aiJobId = '';

async function runAiBatch() {
  const q = (annoState.queue || []).filter(function (x) { return x.state !== 'done'; }).slice(0, 20);
  if (!q.length) { toast('队列里没有待标的', true); return; }
  if (!confirm('对当前队列前 ' + q.length + ' 条跑 AI 初注？\n会消耗 API 额度。')) return;
  const st = document.getElementById('anno-ai-state');
  if (st) st.textContent = '正在启动…';
  if (logAi) logAi.clear('正在启动 AI 初注…');
  try {
    const body = { tids: q.map(function (x) { return x.tid; }), limit: q.length };
    const mine = localAiPayload();
    if (mine) body.ai = mine;          // ★ 用我自己的 key（存在我浏览器里）
    const r = await api('/api/anno/ai/batch', {
      method: 'POST', body: JSON.stringify(body)
    });
    aiJobId = (r.job || {}).id || '';
    if (!aiJobId) throw new Error('没拿到任务号');
    const btn = document.getElementById('anno-ai-stop');
    if (btn) btn.classList.remove('hidden');
    await pollAiJob(aiJobId);
  } catch (e) {
    toast(e.message, true);
    if (st) st.textContent = e.message;
  }
}

/** 轮询后台任务，把逐条进度回显出来 */
async function pollAiJob(jid) {
  const st = document.getElementById('anno-ai-state');
  for (;;) {
    const j = await api('/api/jobs/' + encodeURIComponent(jid) + '?tail=200');
    const lines = (j.logs || []).map(function (x) {
      const t = new Date((x.t || 0) * 1000).toLocaleTimeString('zh-CN');
      return '<div class="stream-line"><span class="t">' + t + '</span>'
        + '<span class="m">' + esc(x.m || '') + '</span></div>';
    });
    const head = j.last_progress ? '<b>' + esc(j.last_progress) + '</b>' : '';
    if (logAi) logAi.set(lines, head);
    if (st && j.state === 'running') st.textContent = j.last_progress || '运行中…';
    if (j.state !== 'running') {
      const res = j.result || {};
      const msg = 'AI 初注结束：成功 ' + (res.ok || 0) + ' 条'
        + (res.failed ? '，失败 ' + res.failed + ' 条' : '')
        + (res.stopped ? '（已停手：' + res.stopped + '）' : '')
        + (j.state === 'failed' ? '（任务出错：' + (j.error || '') + '）' : '');
      toast(msg);
      if (st) st.textContent = msg;
      const btn = document.getElementById('anno-ai-stop');
      if (btn) btn.classList.add('hidden');
      await loadAnnoQueue();
      return;
    }
    await new Promise(function (r) { setTimeout(r, 1500); });
  }
}

/** 进标注台时：接上**还在跑**的 AI 初注任务。
    ★ 为什么要这个（2026-11-08 用户提「进程不显示」）：
      任务在服务器上跑着，页面刷新 / 切走再回来，进度不能丢。
      顺带也让「批量初注」不依赖这一页一直开着。 */
async function resumeAiJob() {
  try {
    const r = await api('/api/jobs');
    const jobs = (r.items || []).filter(function (j) { return j.kind === 'ai_batch'; });
    if (!jobs.length) return;
    const j = jobs[0];                 // 后端按开始时间倒序 → 第一个就是最新的
    if (j.state !== 'running') return;
    aiJobId = j.id;
    const btn = document.getElementById('anno-ai-stop');
    if (btn) btn.classList.remove('hidden');
    const st = document.getElementById('anno-ai-state');
    if (st) st.textContent = j.last_progress || '运行中…';
    await pollAiJob(j.id);
  } catch (e) { /* 没有在跑的任务就算了，不打扰 */ }
}

async function stopAiJob() {
  if (!aiJobId) return;
  try {
    await api('/api/jobs/' + encodeURIComponent(aiJobId) + '/stop', { method: 'POST' });
    toast('已请求停止（这一条跑完就停）');
  } catch (e) { toast(e.message, true); }
}

/* ---------- 批量：采纳 AI 建议（疑点自动跳过） ---------- */
async function adoptAiBatch() {
  const q = (annoState.queue || []).filter(function (x) { return x.state !== 'done'; });
  const tids = q.map(function (x) { return x.tid; });
  if (!tids.length) { toast('队列里没有待标的', true); return; }
  if (!confirm('批量采纳队列里「有 AI 建议且无疑点」的条目？\n'
    + '有疑点的会自动跳过（要人工逐条确认）。')) return;
  try {
    const r = await api('/api/anno/ai/adopt-batch', {
      method: 'POST', body: JSON.stringify({ tids: tids })
    });
    const msg = '已采纳 ' + r.adopted + ' 条，跳过 ' + r.skipped_n + ' 条'
      + (r.skipped_n ? '（' + (r.skipped || []).slice(0, 3)
        .map(function (s) { return s.tid + '：' + s.why; }).join('；') + '）' : '');
    toast(msg);
    const st = document.getElementById('anno-ai-state');
    if (st) st.textContent = msg;
    await loadAnnoQueue();
  } catch (e) { toast(e.message, true); }
}

/* ---------- 批量通过（待复核，管理员） ---------- */
async function reviewBatch(approved) {
  const picks = document.querySelectorAll('#t-anno-pending .anno-pick');
  const tids = [];
  picks.forEach(function (c) { if (c.checked) tids.push(Number(c.value)); });
  if (!tids.length) { toast('先勾选要处理的标注', true); return; }
  let note = '';
  if (!approved) {
    note = prompt('批量打回意见（必填）—— 打回后人工标签失效，导出回落到规则标签：') || '';
    if (!note) return;
  } else if (!confirm('批量通过 ' + tids.length + ' 条标注？')) return;
  try {
    const r = await api('/api/anno/review/batch', {
      method: 'POST',
      body: JSON.stringify({ tids: tids, approved: approved, note: note })
    });
    const msg = (approved ? '已通过 ' : '已打回 ') + r.ok + ' 条'
      + (r.skipped_n ? '，失败 ' + r.skipped_n + ' 条' : '');
    toast(msg);
    const st = document.getElementById('anno-review-state');
    if (st) st.textContent = msg;
    loadAnnoPending();
  } catch (e) { toast(e.message, true); }
}

/* ---------- AI 设置（管理员；密钥只存服务器，不回显） ---------- */
async function loadAiConfig() {
  const card = document.getElementById('ai-config-card');
  const localCard = document.getElementById('ai-local-card');
  const isAdmin = !!(state.me && state.me.role === 'admin');
  // ★ 我的 API（存在本人浏览器里）：**组员与管理员都能用** —— AI 功能要在组员侧生效，
  //   就得让组员自己配一份；服务器那份是管理员的默认。
  if (localCard) { localCard.classList.remove('hidden'); renderLocalAiState(); }
  if (!card) return;
  if (!isAdmin) { card.classList.add('hidden'); return; }
  card.classList.remove('hidden');
  try {
    const c = await api('/api/ai/config');
    document.getElementById('ai-protocol').value = c.protocol || 'openai';
    document.getElementById('ai-base').value = c.base_url || '';
    document.getElementById('ai-model').value = c.model || '';
    document.getElementById('ai-timeout').value = c.timeout || 60;
    document.getElementById('ai-enabled').checked = !!c.enabled;
    const st = document.getElementById('ai-state');
    if (st) {
      st.textContent = '状态：' + (c.enabled ? '已启用' : '已停用')
        + ' ｜ 密钥：' + (c.has_key ? '已配置（不回显）' : '未配置')
        + ' ｜ 配置文件：' + (c.config_path || '') + '（只在这台服务器上）';
    }
  } catch (e) {
    // ★ 拉配置失败也要把卡留着并说清原因：
    //   以前这里 catch 里直接把这张卡藏了，管理员永远看不到（密钥没法配）。
    const st = document.getElementById('ai-state');
    if (st) st.textContent = '读取配置失败：' + (e.message || e);
  }
}

async function saveAiConfig() {
  try {
    await api('/api/ai/config', {
      method: 'POST',
      body: JSON.stringify({
        protocol: (document.getElementById('ai-protocol') || {}).value,
        base_url: (document.getElementById('ai-base') || {}).value,
        model: (document.getElementById('ai-model') || {}).value,
        api_key: (document.getElementById('ai-key') || {}).value,
        timeout: Number((document.getElementById('ai-timeout') || {}).value || 60),
        enabled: (document.getElementById('ai-enabled') || {}).checked
      })
    });
    const key = document.getElementById('ai-key');
    if (key) key.value = '';
    toast('AI 设置已保存（密钥只在本服务器，不回显、不进发布包）');
    loadAiConfig();
  } catch (e) { toast(e.message, true); }
}

/* ---------- 我的 API（存本机浏览器） ---------- */
function renderLocalAiState() {
  const st = document.getElementById('ai-local-state');
  if (!st) return;
  const c = getLocalAiCfg();
  st.textContent = c
    ? ('我的 API：已配置（' + (c.model || '未填模型名') + ' ｜ 协议 ' + c.protocol
       + '）｜ 只存在这台电脑的浏览器里，不进服务器')
    : '我的 API：未配置（不填就用服务器上管理员的默认配置）';
}

function saveLocalAiCfg() {
  const cfg = {
    protocol: (document.getElementById('ai-l-protocol') || {}).value || 'openai',
    base_url: ((document.getElementById('ai-l-base') || {}).value || '').trim(),
    model: ((document.getElementById('ai-l-model') || {}).value || '').trim(),
    api_key: ((document.getElementById('ai-l-key') || {}).value || '').trim(),
    timeout: Number((document.getElementById('ai-l-timeout') || {}).value || 60),
  };
  if (!cfg.api_key) { toast('api_key 不能空（想用服务器默认就别填这一栏）', true); return; }
  if (!cfg.base_url || !cfg.model) { toast('base_url 和模型名都要填', true); return; }
  try {
    localStorage.setItem(AI_LOCAL_KEY, JSON.stringify(cfg));
    const key = document.getElementById('ai-l-key');
    if (key) key.value = '';
    toast('已保存到这台电脑的浏览器（不发给服务器保存）');
    renderLocalAiState();
  } catch (e) { toast('存不进浏览器：' + (e.message || e), true); }
}

function clearLocalAiCfg() {
  try { localStorage.removeItem(AI_LOCAL_KEY); } catch (e) { /* 忽略 */ }
  ['ai-l-base', 'ai-l-model', 'ai-l-key'].forEach(function (id) {
    const el = document.getElementById(id);
    if (el) el.value = '';
  });
  toast('已清除本机浏览器里的 API 配置');
  renderLocalAiState();
}

/* ---------- 检索页点进来的详情里，也要能看到 AI 标注 ---------- */
async function appendAiToDrawer(tid) {
  const box = document.getElementById('dw-body');
  if (!box) return;
  try {
    const a = await api('/api/anno/ai/' + encodeURIComponent(tid));
    if (!a || !a.verdict) return;
    const dims = { relation: '关系', process: '性质·过程', impact: '影响' };
    const tags = (a.tags || []).map(function (t) {
      return esc((dims[t.dim] || t.dim) + '：' + t.tag);
    }).join('，') || '（无）';
    const doubts = (a.doubts || []).length
      ? '<div class="hint" style="color:var(--warn)">⚠ 有疑点，需人工确认：'
        + esc((a.doubts || []).join('；')) + '</div>'
      : '';
    const el = document.createElement('div');
    el.className = 'anno-ai';
    el.innerHTML = '<div class="secttl">AI 初注建议</div>'
      + '<div><b>建议结论：</b>' + esc(a.verdict) + '</div>'
      + '<div><b>理由：</b>' + esc(a.reason || '（无）') + '</div>'
      + '<div><b>标签建议：</b>' + tags + '</div>'
      + doubts
      + '<div class="hint">模型 ' + esc(a.model || '—') + ' ｜ 生成 ' + esc(a.updated_at || '—')
      + ' ｜ 状态 ' + esc(a.status || '—') + '（AI 只是建议，作数的是人工标注）</div>';
    box.appendChild(el);
  } catch (e) { /* 没建议就算了，不打扰 */ }
}

/* ---------- 初始化 ---------- */
// ★ AI 初注的进度日志（复用采集任务那套日志视图：分页 + 只在变了才重绘）
const logAi = makeLogView('#anno-ai-log', { empty: '（还没有日志）' });

(function initAiUi() {
  cloneFilterOptions();
  const bind = function (id, fn) {
    const el = document.getElementById(id);
    if (el) el.onclick = fn;
  };
  bind('anno-ai-batch', runAiBatch);
  bind('anno-ai-adopt-all', adoptAiBatch);
  bind('anno-review-all', function () { reviewBatch(true); });
  bind('ai-save', saveAiConfig);
  bind('anno-ai-stop', stopAiJob);
  bind('ai-l-save', saveLocalAiCfg);
  bind('ai-l-clear', clearLocalAiCfg);
  const pickAll = document.getElementById('anno-pick-all');
  if (pickAll) {
    pickAll.onchange = function () {
      document.querySelectorAll('#t-anno-pending .anno-pick').forEach(function (c) {
        c.checked = pickAll.checked;
      });
    };
  }
})();
