/* 民生e点通舆情采集器 —— 前端逻辑
   零依赖、零构建。原生 fetch + DOM 操作。
   注意：所有采集动作都走 /api/jobs/*（后台线程），页面只负责发起与轮询。 */

const $  = (s, r = document) => r.querySelector(s);
const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

/* ★ 令牌**同时**存 localStorage 与 sessionStorage。
   为什么存两份：浏览器的存储可能被清（隐私策略、换 profile、
   Electron 内置浏览器的临时 session）—— 实测踩过：标签页的令牌
   莫名其妙没了，页面退回登录态，而服务端其实认那个令牌。
   两份各留一份，清掉其中一份还能自动续上。 */
function saveToken(t) {
  try { localStorage.setItem('msyt_token', t); } catch (_) {}
  try { sessionStorage.setItem('msyt_token', t); } catch (_) {}
}
function loadToken() {
  try { const a = localStorage.getItem('msyt_token'); if (a) return a; } catch (_) {}
  try { const b = sessionStorage.getItem('msyt_token'); if (b) return b; } catch (_) {}
  return '';
}
function clearToken() {
  try { localStorage.removeItem('msyt_token'); } catch (_) {}
  try { sessionStorage.removeItem('msyt_token'); } catch (_) {}
}

const state = {
  meta: null,
  page: 1,
  total: 0,
  limit: 20,
  jobTimer: null,
  selJob: null,
  lastQ: '',
  token: loadToken(),
  me: null,
  es: null,
  esSeq: 0,
};

/* ------------------------------------------------------------------ 工具 */
async function api(path, opts = {}) {
  const headers = { 'Content-Type': 'application/json' };
  if (state.token) headers['Authorization'] = 'Bearer ' + state.token;
  const res = await fetch(path, { ...opts, headers: { ...headers, ...(opts.headers || {}) } });
  if (!res.ok) {
    let msg = res.statusText;
    let detail = null;
    try { detail = (await res.json()).detail; msg = detail || msg; } catch (_) {}
    if (res.status === 401) { showLogin(msg); }
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return res.json();
}

function toast(msg, bad = false) {
  const t = $('#toast');
  t.textContent = msg;
  t.className = 'toast on' + (bad ? ' bad' : '');
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { t.className = 'toast'; }, 3200);
}

const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({ '&': '&', '<': '<', '>': '>', '"': '"', "'": '&#39;' }[c]));

const num = n => (n ?? 0).toLocaleString('zh-CN');

function lightBadge(code, name) {
  const c = ['green', 'yellow', 'grey', 'warn'].includes(code) ? code : 'none';
  return `<span class="badge ${c}">${esc(name || code || '未点亮')}</span>`;
}

function stateBadge(s) {
  const m = { '已回复': 'green', '已受理': 'teal', '未受理': 'grey' };
  return `<span class="badge ${m[s] || 'none'}">${esc(s || '—')}</span>`;
}

function detailBadge(s) {
  const m = { 'done': 'done', 'pending': 'pending', 'failed': 'failed',
              'skipped': 'skipped', 'running': 'teal',
              // stale = 站方又回复了，本地正文过时，等着重采
              'stale': 'warn',
              // restricted = 站方限制：历史帖子仅限作者本人查看，正文不公开
              'restricted': 'warn' };
  const label = { stale: '待更新', restricted: '站方限制' };
  return `<span class="badge ${m[s] || 'none'}">${esc(label[s] || s || '—')}</span>`;
}

/* ------------------------------------------------------------------ 标签页 */
/* ★ 页签权限（2026-10-22 调整）
   管理员：全部
   成员  ：总览 / 检索与浏览 / 采集与协作 / 采集日志
           **完整性体检隐藏**（那是管理员对全库做质量自查的视角）
   界面层藏 + 接口层 require_admin，两道都做 —— 缺一道等于没锁。 */
const TABS_FOR_MEMBER = ['overview', 'threads', 'collect', 'log'];

function showSub(sub) {
  const target = sub || 'jobs';
  $$('#collect-subtabs button').forEach(b =>
    b.classList.toggle('active', b.dataset.sub === target));
  $$('#tab-collect .subpanel').forEach(p => p.classList.add('hidden'));
  const el = $('#sub-' + target);
  if (el) el.classList.remove('hidden');
  if (target === 'jobs') {
    ensurePickers(); loadJobs();
    // ★ 「采集作业」页顶上就是本机 agent 面板 + 「先起 Agent」引导卡，
    //   所以进这个页签也要开始轮询助手（否则状态徽章不更新）。
    pollHelper();
  }
  if (target === 'assist') {
    ensurePickers();
    loadAssistTasks();
    loadConflicts();
    loadOnline();
    pollHelper();
  }
  if (target === 'me') { renderMe(); }
}

$$('#collect-subtabs button').forEach(btn => {
  btn.onclick = () => showSub(btn.dataset.sub);
});

$$('#tabs button').forEach(btn => {
  btn.onclick = async () => {
    $$('#tabs button').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    $$('.panel').forEach(p => p.classList.add('hidden'));
    $('#tab-' + btn.dataset.tab).classList.remove('hidden');
    if (btn.dataset.tab === 'overview') loadOverview();
    if (btn.dataset.tab === 'threads') { if (!state.meta) await loadMeta(); loadThreads(); }
    if (btn.dataset.tab === 'collect') {
      if (!state.token) { showLogin('请先登录'); return; }
      if (!state.meta) await loadMeta();
      ensurePickers();
      showSub('jobs');
    }
    if (btn.dataset.tab === 'integrity') runIntegrity();
    if (btn.dataset.tab === 'log') loadLog();
  };
});

/* ------------------------------------------------------------------ 顶栏 */
async function loadHealth() {
  try {
    const h = await api('/api/health');
    $('#dot').className = 'dot ' + (h.ok ? 'ok' : 'bad');
    $('#dbtext').textContent = h.ok
      ? (h.db_path.split(/[\\/]/).pop() || 'msyt.db')
      : ('库异常：' + (h.error || ''));
    $('#m-threads').textContent = num(h.threads);
    $('#m-posts').textContent = num(h.posts);
  } catch (e) {
    $('#dot').className = 'dot bad';
    $('#dbtext').textContent = '后端未连接';
    return;
  }
  // ⚠ /api/stats 现在要登录 —— 未登录时**不要**把「后端未连接」显示出来，
  //   那会让登录页看起来像服务挂了。分开兜底。
  try {
    const st = await api('/api/stats');
    $('#m-done').textContent = num(st.detail_done);
    $('#m-pending').textContent = num((st.queue || {}).pending || 0);
  } catch (e) {
    $('#m-done').textContent = '–';
    $('#m-pending').textContent = '–';
  }
}

/* ------------------------------------------------------------------ 总览 */
function barChart(el, obj, color) {
  const items = Object.entries(obj || {}).sort((a, b) => b[1] - a[1]);
  if (!items.length) { el.innerHTML = '<p class="hint">暂无数据</p>'; return; }
  const max = Math.max(...items.map(x => x[1]));
  el.innerHTML = items.map(([k, v]) => `
    <div class="bar-row">
      <div class="name" title="${esc(k)}">${esc(k)}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${(v / max * 100).toFixed(1)}%${color ? ';background:' + color : ''}"></div></div>
      <div class="val">${num(v)}</div>
    </div>`).join('');
}

async function loadOverview() {
  let st;
  try {
    st = await api('/api/stats');
  } catch (e) {
    $('#ov-cards').innerHTML = `<div class="errbox">总览加载失败：${esc(e.message)}</div>`;
    return;
  }
  const q = st.queue || {};
  const cards = [
    ['帖子总数', num(st.threads), 'accent', '列表页已入库的帖'],
    ['楼层总数', num(st.posts), '', '详情页正文条目'],
    ['已采详情帖', num(st.detail_done), '', '通过完整性判据'],
    ['站方限制帖', num(st.detail_restricted || 0), (st.detail_restricted ? 'warn' : ''),
      '站方不公开正文，已标记不计入已采'],
    ['待采队列', num(q.pending || 0), 'accent', '排在队列里等采'],
    ['失败待重试', num(q.failed || 0), (q.failed ? 'danger' : ''), '需要 retry'],
    ['部门数', num(st.depts), '', '站方部门目录'],
    ['带告警帖', num(st.warned), (st.warned ? 'warn' : ''), '站方计数不自洽等'],
    ['站方自报留言', num((st.site_stats || {}).total_msgs || 0), '', '全站历史总量'],
  ];
  $('#ov-cards').innerHTML = cards.map(([l, n, c, h]) => `
    <div class="metric ${c}"><div class="n">${n}</div><div class="l">${esc(l)}</div>
    ${h ? `<div class="l" style="font-size:11px;opacity:.75">${esc(h)}</div>` : ''}</div>`).join('');

  barChart($('#ov-category'), st.by_category);
  barChart($('#ov-state'), st.by_state);
  barChart($('#ov-group'), st.by_group);
  barChart($('#ov-light'), st.by_light);
  barChart($('#ov-district'), st.by_district);

  const ss = st.site_stats || {};
  $('#ov-site').innerHTML = ss.total_msgs ? `
    <div class="k">采集时点</div><div class="v">${esc(ss.captured_at || '—')}</div>
    <div class="k">历史留言总量</div><div class="v">${num(ss.total_msgs)}</div>
    <div class="k">历史受理量</div><div class="v">${num(ss.total_accepted)}</div>
    <div class="k">今年留言</div><div class="v">${num(ss.this_year_msgs)}</div>
    <div class="k">上月留言</div><div class="v">${num(ss.last_month_msgs)}</div>`
    : '<p class="hint">还没采过站方统计脚本，去「采集作业」抓一次。</p>';

  const wm = st.watermarks || [];
  $('#ov-wm').innerHTML = wm.length ? `
    <thead><tr><th>采集源</th><th>名称</th><th>状态</th><th>已采页</th>
    <th>站方上限</th><th>累计行</th><th>最早</th><th>最近更新</th></tr></thead>
    <tbody>${wm.map(w => `<tr>
      <td class="nowrap">${esc(w.source)}</td>
      <td>${esc(w.name || '')}</td>
      <td><span class="badge ${w.status === 'done' ? 'done' : (w.status === 'in_progress' ? 'teal' : 'pending')}">${esc(w.status)}</span></td>
      <td class="num">${num(w.last_page)}</td>
      <td class="num">${num(w.max_page)}</td>
      <td class="num">${num(w.rows_total)}</td>
      <td class="nowrap">${w.oldest_ts ? new Date(w.oldest_ts * 1000).toLocaleDateString('zh-CN') : '—'}</td>
      <td class="nowrap sub2">${esc(w.updated_at || '')}</td>
    </tr>`).join('')}</tbody>`
    : '<tbody><tr><td class="hint">还没有采集源水位</td></tr></tbody>';
}

/* ------------------------------------------------------------------ 字典 */
async function loadMeta() {
  const m = await api('/api/meta');
  state.meta = m;
  const fill = (sel, items, label) => {
    const el = $(sel);
    if (!el) return;
    el.innerHTML = `<option value="">${label}</option>` +
      items.map(x => `<option value="${esc(x.code ?? x.typeid)}">${esc(x.name)}</option>`).join('');
  };
  fill('#f-category', m.categories, '全部类别');
  fill('#f-state', m.states, '全部状态');
  fill('#f-mailbox', m.mailboxes, '全部留言箱');
  fill('#f-light', m.lights, '全部时效灯');
  fill('#f-group', m.groups, '全部分组');
  fill('#f-district', m.districts, '全部区县市');
  fill('#f-dept', m.depts, '全部部门');
  initDeptDict();          // 部门字典：两个选择器共用
  pickers.forEach(renderPicker);
}

/* ------------------------------------------------------------------ 检索 */
function filters() {
  const g = id => { const e = $('#' + id); return e ? e.value : ''; };
  const p = new URLSearchParams();
  const q = g('f-q').trim();
  if (q) p.set('q', q);
  // ★★ 筛选器的 **DOM id 全都带 f- 前缀**（f-category / f-state / f-group …）。
  //   这里必须按 'f-' + 键 去取值。
  //   旧写法 g('category') 找的是 #category → 永远是 null → **所有下拉筛选
  //   被静默丢掉**，表现就是「下拉框选了条件、结果还是全量」（2026-10-24 用户实测）。
  ['scope', 'sort', 'category', 'state', 'mailbox', 'group', 'district',
   'dept', 'light'].forEach(k => {
    const v = g('f-' + k);
    if (v) p.set(k, v);
  });
  if (g('f-from')) p.set('date_from', g('f-from'));
  if (g('f-to')) p.set('date_to', g('f-to'));
  p.set('limit', state.limit);
  p.set('offset', (state.page - 1) * state.limit);
  return p;
}

/** 当前生效的筛选条件（给人看的「我现在在筛什么」）。 */
function activeFilters() {
  const g = id => { const e = $('#' + id); return e ? e.value : ''; };
  const names = {
    category: '类别', state: '状态', mailbox: '留言箱', group: '分组',
    district: '区县市', dept: '部门', light: '时效灯',
  };
  const out = [];
  for (const [k, label] of Object.entries(names)) {
    const el = $('#f-' + k);
    if (!el || !el.value) continue;
    const opt = el.options[el.selectedIndex];
    const txt = (opt && opt.textContent || el.value).trim();
    if (!txt || txt.startsWith('全部')) continue;
    out.push({ key: k, label, value: el.value, text: txt });
  }
  const d1 = g('f-from'), d2 = g('f-to');
  if (d1) out.push({ key: 'date_from', label: '起始', value: d1, text: d1 });
  if (d2) out.push({ key: 'date_to', label: '截止', value: d2, text: d2 });
  return out;
}

/** 按某个标签筛选（点表格里的类别 / 状态 / 时效灯徽章时用）。 */
function applyTagFilter(key, value) {
  const el = $('#f-' + key);
  if (!el) return;
  // 选项不存在就补一个（表里的值未必在字典里）
  let found = false;
  for (const o of el.options) { if (o.value === value) { found = true; break; } }
  if (!found) {
    const o = document.createElement('option');
    o.value = value; o.textContent = value;
    el.appendChild(o);
  }
  el.value = value;
  state.page = 1;
  loadThreads();
}

/** 把「当前生效的筛选条件」显示出来，点一下 chip 就能去掉那条。 */
function renderActiveFilters() {
  const box = $('#active-filters');
  if (!box) return;
  const list = activeFilters();
  if (!list.length) { box.innerHTML = ''; return; }
  box.innerHTML = '当前筛选：' + list.map(f =>
    `<span class="chip" data-key="${f.key}" title="点一下去掉这个条件">`
    + `${esc(f.label)}：${esc(f.text)} ✕</span>`).join('');
  $$('#active-filters .chip').forEach(c => {
    c.onclick = () => {
      const el = $('#f-' + c.dataset.key);
      if (el) el.value = '';
      state.page = 1;
      loadThreads();
    };
  });
}

async function loadThreads() {
  if (!state.meta) await loadMeta();
  let res;
  try {
    res = await api('/api/threads?' + filters().toString());
  } catch (e) {
    $('#res-info').innerHTML = `<span style="color:#b42318">检索失败：${esc(e.message)}</span>`;
    return;
  }
  state.total = res.total;
  const modeTxt = res.mode === 'fts' ? '全文索引 (FTS5)'
    : res.mode === 'like' ? 'LIKE 全表扫描（关键词不足 3 字，自动退回）'
    : '无关键词';
  $('#res-info').innerHTML =
    `命中 <b>${num(res.total)}</b> 条 · 检索方式 <b>${modeTxt}</b> · 耗时 ${res.took_ms} ms` +
    (res.matched_in && res.matched_in.length ? ` · 命中位置 ${res.matched_in.join('/')}` : '');

  const rows = res.items || [];
  $('#t-threads').innerHTML = `
    <thead><tr><th>TID</th><th>标题</th><th>部门</th><th>类别</th><th>状态</th>
    <th>时效灯</th><th>时间</th><th>回复</th><th>详情</th></tr></thead>
    <tbody>${rows.length ? rows.map(r => `
      <tr class="clickable" data-tid="${r.tid}">
        <td class="num sub2">${r.tid}</td>
        <td class="title"><a href="javascript:void 0">${esc(r.title)}</a>
          <div class="sub2">${esc(r.author || '')} · 浏览 ${num(r.views)}</div></td>
        <td><span class="tagfilter" data-tag="dept" data-val="${esc(r.dept_id ?? '')}"
              title="按这个部门筛选">${esc(r.dept_name || '')}</span>${r.dept_district ? `<div class="sub2">${esc(r.dept_district)}</div>` : ''}</td>
        <td class="nowrap"><span class="tagfilter" data-tag="category"
              data-val="${esc(r.category || '')}" title="按这个类别筛选">${esc(r.category || '')}</span></td>
        <td class="nowrap"><span class="tagfilter" data-tag="state"
              data-val="${esc(r.state || '')}" title="按这个受理状态筛选">${stateBadge(r.state)}</span></td>
        <td class="nowrap"><span class="tagfilter" data-tag="light"
              data-val="${esc(r.light_code || '')}" title="按这个时效灯筛选">${lightBadge(r.light_code, r.light_name)}</span></td>
        <td class="nowrap sub2">${esc(r.pub_time || '')}</td>
        <td class="num">${num(r.replies)}</td>
        <td class="nowrap">${detailBadge(r.detail_state)}</td>
      </tr>`).join('')
      : '<tr><td colspan="9" class="hint">没有命中的帖子</td></tr>'}</tbody>`;

  $$('#t-threads tr.clickable').forEach(tr => {
    tr.onclick = () => openThread(tr.dataset.tid);
  });
  // ★ 标签可点：点类别/状态/时效灯/部门 → 直接按它筛（别开详情抽屉）
  $$('#t-threads .tagfilter').forEach(el => {
    el.onclick = (ev) => {
      ev.stopPropagation();
      ev.preventDefault();
      const v = el.dataset.val || '';
      if (!v) return;
      applyTagFilter(el.dataset.tag, v);
    };
  });

  renderActiveFilters();

  const pages = Math.max(1, Math.ceil(res.total / state.limit));
  $('#pg-text').textContent = `第 ${state.page} / ${pages} 页`;
  $('#pg-first').disabled = $('#pg-prev').disabled = state.page <= 1;
  $('#pg-next').disabled = $('#pg-last').disabled = state.page >= pages;
}

function goto(p) { state.page = Math.max(1, p); loadThreads(); }
$('#btn-search').onclick = () => { state.page = 1; loadThreads(); };
$('#btn-reset').onclick = () => {
  ['f-q', 'f-from', 'f-to'].forEach(i => $('#' + i).value = '');
  ['f-scope', 'f-sort', 'f-category', 'f-state', 'f-mailbox', 'f-group',
   'f-district', 'f-dept', 'f-light'].forEach(i => {
    const el = $('#' + i);
    if (el) el.value = '';      // select 回落到第一项（全部 / 默认）
  });
  state.page = 1; loadThreads();
};
$('#f-q').addEventListener('keydown', e => { if (e.key === 'Enter') { state.page = 1; loadThreads(); } });

// ★ 下拉框一改就自动检索（用户改完条件还要再点一次「检索」很容易忘）
['f-category', 'f-state', 'f-mailbox', 'f-group', 'f-district', 'f-dept',
 'f-light'].forEach(id => {
  const el = $('#' + id);
  if (el) el.onchange = () => { state.page = 1; loadThreads(); };
});
['f-from', 'f-to'].forEach(id => {
  const el = $('#' + id);
  if (el) el.onchange = () => { state.page = 1; loadThreads(); };
});
$('#pg-first').onclick = () => goto(1);
$('#pg-prev').onclick = () => goto(state.page - 1);
$('#pg-next').onclick = () => goto(state.page + 1);
$('#pg-last').onclick = () => goto(Math.max(1, Math.ceil(state.total / state.limit)));
$('#pg-size').onchange = e => { state.limit = +e.target.value; state.page = 1; loadThreads(); };

/* ------------------------------------------------------------------ 详情抽屉 */
async function openThread(tid) {
  $('#dw-title').textContent = '载入中…';
  $('#dw-meta').textContent = '';
  $('#dw-body').innerHTML = '';
  // ★ 先去掉 wide：详情抽屉是普通宽度。
  //   协作任务详情那个抽屉才是宽的，不清掉会残留。
  $('#drawer').classList.remove('wide');
  $('#drawer').classList.add('on');
  $('#mask').classList.add('on');
  try {
    const d = await api('/api/threads/' + tid);
    const t = d.thread;
    $('#dw-title').textContent = t.title || '(无标题)';
    $('#dw-meta').innerHTML = [
      `TID ${t.tid}`,
      t.dept_name, t.dept_district, t.category, t.state,
      t.pub_time, `回复 ${num(t.replies)}`, `浏览 ${num(t.views)}`,
    ].filter(Boolean).map(esc).join(' · ');

    const bits = [];
    if (t.url) bits.push(`<a href="${esc(t.url)}" target="_blank" rel="noopener">原帖</a>`);
    bits.push(detailBadge(t.detail_state));
    if (t.detail_warn) bits.push(`<span class="badge warn">有告警</span>`);
    bits.push(`楼层 ${num(t.posts_n)} · 字数 ${num(t.chars)} · 页数 ${num(t.pages_total)}`);

    let html = `<div class="secttl">基本信息</div><div style="margin-bottom:12px">${bits.join(' &nbsp; ')}</div>`;

    if (t.detail_warn) html += `<div class="warnbox">${esc(t.detail_warn)}</div>`;

    if (d.flows && d.flows.length) {
      html += `<div class="secttl">受理流程</div><div class="flow">` +
        d.flows.map(f => `<div class="fs"><div class="fn">${esc(f.name)}</div>
          <div class="ft">${esc(f.time || '')}</div></div>`).join('') + '</div>';
    }

    if (d.pages && d.pages.length) {
      html += `<div class="secttl">分页与楼层锚点（每页 pid 集合）</div>
        <div class="tablewrap" style="margin-bottom:14px"><table>
        <thead><tr><th>页</th><th>楼层数</th><th>pid 集合</th><th>状态</th><th>抓取时间</th></tr></thead>
        <tbody>${d.pages.map(p => `<tr><td class="num">${p.page_no}</td>
          <td class="num">${num(p.n)}</td>
          <td class="sub2">${esc(p.pids || '')}</td>
          <td>${esc(p.status || '')}</td>
          <td class="sub2">${esc(p.fetched_at || '')}</td></tr>`).join('')}</tbody></table></div>`;
    }

    html += `<div class="secttl">楼层（${d.posts.length}）</div>`;
    if (!d.posts.length) {
      html += '<p class="hint">这篇还没有采到正文。去「采集作业」用定点 tid 采一次。</p>';
    } else {
      html += d.posts.map(p => {
        const fl = p.floor > 0 ? p.floor + '#' : '—';
        let imgs = '';
        if (p.images) {
          try {
            const arr = JSON.parse(p.images);
            if (Array.isArray(arr) && arr.length) {
              imgs = '<div class="imgs">' + arr.map(u =>
                `<a href="${esc(u)}" target="_blank" rel="noopener"><img loading="lazy" src="${esc(u)}"></a>`).join('') + '</div>';
            }
          } catch (_) {}
        }
        return `<div class="post">
          <div class="ph"><span class="fl">${esc(fl)}</span>
            <span>${esc(p.author || '')}</span>
            <span>${esc(p.post_time || '')}</span>
            <span>${num(p.chars)} 字</span>
            <span class="sub2">pid ${p.pid}</span></div>
          <div class="pc">${esc(p.content || '')}</div>${imgs}</div>`;
      }).join('');
    }
    $('#dw-body').innerHTML = html;
  } catch (e) {
    $('#dw-title').textContent = '载入失败';
    $('#dw-body').innerHTML = `<div class="errbox">${esc(e.message)}</div>`;
  }
}
$('#dw-close').onclick = () => { $('#drawer').classList.remove('on'); $('#mask').classList.remove('on'); };
$('#mask').onclick = () => { $('#drawer').classList.remove('on'); $('#mask').classList.remove('on'); };
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') { $('#drawer').classList.remove('on'); $('#mask').classList.remove('on'); }
});

/* ------------------------------------------------------------------ 部门选择器（可多实例）
   列表采集与详情采集**各有一个**，共用同一份部门字典，但选中集合各自独立；
   详情那边可以一键「沿用列表的选择」，实现「定点 tid 与列表走同一个源」。 */

const deptDict = { depts: [], byCode: {} };
const pickers = [];
/** 服务端能力探测：旧进程不认识新字段时必须降级，不能静默忽略 */
const serverCaps = { pending: false };
/**
 * 采集节点标识 = **当前登录者自己的节点号**。
 * 管理员本机 = "admin"；成员 = "member:xxx@example.com"。
 * ⚠ 不要再用写死的 'admin'：那样所有成员的任务都会挂在 admin 节点上，
 *   于是「同节点同类任务互斥」会把成员和管理员**互相挡住**，
 *   协作就变成了互相踢凳子。
 */
function myNode() {
  return (state.me && state.me.node_id) || 'admin';
}

function initDeptDict() {
  const m = state.meta;
  if (!m) return;
  deptDict.depts = (m.depts || []).map(d => ({
    typeid: d.typeid,
    name: d.name,
    groups: (d.group_code || '').split(',').filter(Boolean),
  }));
  deptDict.byCode = {};
  (m.groups || []).forEach(g => { deptDict.byCode[g.code] = []; });
  deptDict.depts.forEach(d => d.groups.forEach(c => {
    if (deptDict.byCode[c]) deptDict.byCode[c].push(d);
  }));
}

/** 建一个选择器实例 */
function createPicker(rootId, opts) {
  const root = document.getElementById(rootId);
  if (!root) return null;
  const P = {
    root,
    sel: new Set(),
    search: '',
    listEl: root.querySelector('.picker-list'),
    countEl: root.querySelector('.dp-count'),
    searchEl: root.querySelector('.dp-search'),
    onChange: (opts && opts.onChange) || null,
  };
  pickers.push(P);

  P.searchEl.addEventListener('input', e => {
    P.search = e.target.value;
    renderPicker(P);
  });

  root.querySelectorAll('[data-pick]').forEach(btn => {
    btn.onclick = () => {
      const kind = btn.dataset.pick;
      const total = deptDict.depts.length;
      if (kind === 'copy') {
        const src = pickers.find(x => x.root.id === 'picker-list');
        if (!src) return;
        P.sel = new Set(src.sel);
        toast(`已沿用列表采集的选择（${P.sel.size} 个部门）`);
      } else if (kind === 'all') {
        // 可切换：已全选就取消，没全选就全选（否则点了没反应 = "坏了"）
        const allOn = total > 0 && P.sel.size === total;
        if (allOn) { P.sel.clear(); toast('已取消全选'); }
        else { deptDict.depts.forEach(d => P.sel.add(d.typeid)); toast(`已全选 ${total} 个部门`); }
      } else if (kind === 'none') {
        P.sel.clear(); toast('已清空选择');
      } else if (kind === 'invert') {
        deptDict.depts.forEach(d => {
          if (P.sel.has(d.typeid)) P.sel.delete(d.typeid);
          else P.sel.add(d.typeid);
        });
        toast(`已反选，当前 ${P.sel.size} 个`);
      }
      renderPicker(P);
    };
  });
  return P;
}

/** 渲染分组勾选列表（带搜索过滤） */
function renderPicker(P) {
  const q = P.search.trim().toLowerCase();
  const groups = (state.meta && state.meta.groups) || [];
  const html = groups.map(g => {
    const all = deptDict.byCode[g.code] || [];
    const list = q ? all.filter(d => d.name.toLowerCase().includes(q)) : all;
    if (!list.length) return '';
    const selIn = list.filter(d => P.sel.has(d.typeid)).length;
    const cnt = q ? `${list.length} / ${all.length}` : `${all.length}`;
    return `<div class="pg" data-g="${esc(g.code)}">
      <label class="pg-h">
        <input type="checkbox" class="pg-all" ${list.length && selIn === list.length ? 'checked' : ''}>
        ${esc(g.name)} <span class="dim">(${cnt})</span>
      </label>
      <div class="pg-items">${list.map(d => `
        <label class="pg-i ${P.sel.has(d.typeid) ? 'on' : ''}">
          <input type="checkbox" data-tid="${esc(d.typeid)}" ${P.sel.has(d.typeid) ? 'checked' : ''}>
          ${esc(d.name)} <span class="tid">${esc(d.typeid)}</span>
        </label>`).join('')}</div>
    </div>`;
  }).join('');

  // ⚠ 重建 innerHTML 会把滚动位置重置到 0 —— 用户在列表中间点一下按钮，
  //    界面直接跳回顶部，勾选的变化根本来不及看，**看起来就像"没生效"**。
  const keepTop = P.listEl.scrollTop;
  P.listEl.innerHTML = html || '<p class="hint" style="padding:12px 14px">没有匹配的部门</p>';
  P.listEl.scrollTop = keepTop;

  P.listEl.querySelectorAll('.pg-i input[data-tid]').forEach(cb => {
    cb.onchange = () => {
      const t = cb.dataset.tid;
      if (cb.checked) P.sel.add(t); else P.sel.delete(t);
      renderPicker(P);
    };
  });
  P.listEl.querySelectorAll('.pg-all').forEach(cb => {
    cb.onchange = () => {
      const g = cb.closest('.pg').dataset.g;
      const all = deptDict.byCode[g] || [];
      const q2 = P.search.trim().toLowerCase();
      const list = q2 ? all.filter(d => d.name.toLowerCase().includes(q2)) : all;
      list.forEach(d => {
        if (cb.checked) P.sel.add(d.typeid); else P.sel.delete(d.typeid);
      });
      renderPicker(P);
    };
  });
  updatePickerCount(P);
}

function updatePickerCount(P) {
  const total = deptDict.depts.length;
  const n = P.sel.size;
  P.countEl.textContent = `已选 ${n} / ${total}`;
  const allBtn = P.root.querySelector('[data-pick="all"]');
  if (allBtn) {
    const allOn = total > 0 && n === total;
    allBtn.textContent = allOn ? '取消全选' : '全选';
    allBtn.classList.toggle('btn-primary', allOn);
  }
  if (P.onChange) P.onChange(P);
}

/** 列表采集的工作量估算 */
function updateListEstimate(P) {
  const pages = +$('#j-pages').value || 0;
  const cap = pages > 0 ? pages : 500;              // 站方分页顶 500
  const srcs = P.sel.size + ($('#dp-global').checked ? 1 : 0);
  const maxReq = srcs * cap;
  const hours = maxReq * 1.0 / 3600;
  const el = $('#dp-estimate');
  if (!srcs) {
    el.textContent = '先在上面选采集源（可点「全选」）。';
    el.className = 'hint';
    return;
  }
  let txt = `工作量估算：${srcs} 条采集源 × 每源最多 ${cap} 页 → 最坏 `
    + `${maxReq.toLocaleString()} 次请求`;
  if (hours >= 1) txt += `，按 1.0 秒/次约 ${hours.toFixed(1)} 小时`;
  else txt += `，约 ${Math.round(maxReq * 1.0)} 秒`;
  txt += '。中途可随时「停止」，进度会落库、可续采。';
  el.textContent = txt;
  el.className = 'hint' + (maxReq > 5000 ? ' danger-est' : (maxReq > 1000 ? ' warn-est' : ''));
}

/** 详情采集的待采帖数统计（走 /api/pending，服务端没重启就降级提示） */
let _ddTimer = null;
function updateDetailEstimate(P) {
  const el = $('#dd-estimate');
  const ids = Array.from(P.sel);
  if (!serverCaps.pending) {
    el.textContent = '⚠ 服务端尚未重启到支持「按部门采详情」的版本，'
      + '当前只能直接填 tid。（等正在跑的采集结束后重启即可启用）';
    el.className = 'hint warn-est';
    return;
  }
  if (!ids.length) {
    el.textContent = '选部门 → 采这些部门下所有还没采详情的帖；也可直接填 tid。';
    el.className = 'hint';
    return;
  }
  el.textContent = '正在统计待采帖数…';
  el.className = 'hint';
  // 防抖：搜索框每敲一个字都会重绘，不防抖就是每字一次全表统计
  clearTimeout(_ddTimer);
  _ddTimer = setTimeout(async () => {
    try {
      const r = await api('/api/pending?dept_ids=' + ids.join(','));
      const limit = +$('#j-limit').value || 0;
      const willDo = limit > 0 ? Math.min(limit, r.pending) : r.pending;
      el.textContent = `所选 ${ids.length} 个部门下有 ${r.pending.toLocaleString()} 帖还没采详情`
        + (limit > 0 ? `，本次最多采 ${willDo.toLocaleString()} 帖` : '，本次将全采')
        + '；每帖 N×2 次请求（N=页数），先旧后新。'
        + (r.total_pending > r.pending
           ? `（全库还有 ${r.total_pending.toLocaleString()} 帖待采）` : '');
      el.className = 'hint';
    } catch (e) {
      el.textContent = '（统计待采帖数失败：' + e.message + '）';
      el.className = 'hint';
    }
  }, 400);
}

/** 探测服务端是否支持新功能（旧进程会 404 → 降级，绝不静默忽略） */
function _verAtLeast(v, want) {
  const a = String(v || '0').split('.').map(x => parseInt(x, 10) || 0);
  const b = String(want).split('.').map(x => parseInt(x, 10) || 0);
  for (let i = 0; i < 3; i++) {
    if ((a[i] || 0) > (b[i] || 0)) return true;
    if ((a[i] || 0) < (b[i] || 0)) return false;
  }
  return true;
}

async function probeServerCaps() {
  // 先读版本号（永远 200，不会在控制台留 404 噪音）
  let ver = '';
  try { ver = (await api('/api/health')).version || ''; } catch (e) { /* 后面兜底 */ }
  if (ver && !_verAtLeast(ver, '1.0.2')) {
    serverCaps.pending = false;
  } else {
    // 版本说支持，再实打实验一次（万一端点被回退）
    // ⚠ /api/pending 现在要登录 —— 没登录就验不成，此时先按"不支持"处理，
    //   登录后（afterLogin）会再探一次并恢复。
    try {
      await api('/api/pending?dept_ids=');
      serverCaps.pending = true;
    } catch (e) {
      serverCaps.pending = false;
    }
  }
  if (!serverCaps.pending) {
    const dp = document.getElementById('picker-detail');
    if (dp) {
      dp.querySelectorAll('[data-pick]').forEach(b => { b.disabled = true; });
      const s = dp.querySelector('.dp-search');
      if (s) s.disabled = true;
    }
  } else {
    const dp = document.getElementById('picker-detail');
    if (dp) {
      dp.querySelectorAll('[data-pick]').forEach(b => { b.disabled = false; });
      const s = dp.querySelector('.dp-search');
      if (s) s.disabled = false;
    }
  }
}

/* ------------------------------------------------------------------ 采集作业 */
/** 建选择器实例（只建一次） */
function ensurePickers() {
  if (pickers.length) { pickers.forEach(renderPicker); return; }
  createPicker('picker-list', { onChange: updateListEstimate });
  createPicker('picker-detail', { onChange: updateDetailEstimate });
  createPicker('picker-assist', { onChange: () => {} });
  pickers.forEach(renderPicker);
}

async function startJob(path, body, label) {  try {
    const r = await api(path, { method: 'POST', body: JSON.stringify(body || {}) });
    toast(`${label}已开始：${r.job.id}`);
    state.selJob = r.job.id;
    loadJobs();
    pollJobs();
  } catch (e) { toast(`${label}失败：${e.message}`, true); }
}

$('#btn-taxonomy').onclick = () => startJob('/api/jobs/taxonomy', {}, '分类体系抓取');
/* ---------------- 本机采集（成员专用） ----------------
   ★ 为什么成员的采集必须发生在**自己的机器**上
     ① 站方**不提供 CORS** → 浏览器里的网页不能直接采站方，
        成员的采集只能跑在自己机器上的程序里；
     ② 更要紧的：在服务器上采 → 站方看到的流量**全部来自管理员那一个 IP**，
        「多个分散的普通用户」这个 Beta 的核心前提就落空了。
        （实测：成员在网页点一次「开始采集」，dept:555 在管理员的 IP 上
         从 58 页一路刷到 500 页。）
   所以成员的「开始采集」**改为转交本机 agent**：网页 → 本地助手(8766) → agent
   → 在成员机器上采 → 回传服务器。 */
function isMemberView() {
  return !!(state.me && state.me.role !== 'admin');
}

/** 把一次采集转交给本机 agent（经由本地助手 127.0.0.1:8766）。 */
async function startLocalCollect(payload, label) {
  // ★ 先探一下助手在不在（2026-10-23 加）：
  //   组员最容易犯的错就是「没起 Agent 就点开始采集」。
  //   直接发 /start 会得到一个含糊的失败；先探一次可以把话说清楚。
  try {
    const st = await helperCall('/status');
    if (!st || !st.ok) {
      helperMsg('本机助手没响应 —— ‹请先起 Agent›', true);
      toast('还没起 Agent：先点上面的「下载成员端启动包」，'
        + '解压后双击「启动助手.cmd」，再点「启动本机 agent」', true);
      setAgentFirstState(null);
      return;
    }
    if (!st.running) {
      helperMsg('助手在，但本机 Agent 没在跑 —— ‹请先点「启动本机 agent」›', true);
      toast('助手已在运行，但还没起 Agent —— 点上面的「启动本机 agent」', true);
      setAgentFirstState(st);
      return;
    }
  } catch (e) {
    helperMsg('没检测到本机助手 —— ‹请先起 Agent›', true);
    toast('还没起 Agent：先双击包里的「启动助手.cmd」；若助手窗口**已经在跑**却仍提示没检测到，多半是 Edge 拦了「本地网络访问」——点地址栏**左侧**那个图标 → 选「允许此网站访问你的本地网络」；还不行就双击包里的「自检.cmd」，它会直接告诉你哪一环不通', true);
    setAgentFirstState(null);
    return;
  }
  helperMsg(`正在呼叫本机 agent（${label}）…`);
  try {
    const r = await helperCall('/start',
      Object.assign({ token: state.token }, payload));
    if (r && r.ok) {
      helperMsg(r.already
        ? '本机 agent 已经在跑了（先停掉再换目标）'
        : `本机 agent 已开始${label}`);
      toast(`已在本机启动${label}`);
      pollHelper();
    } else {
      helperMsg((r && r.error) || '助手拒绝了', true);
    }
  } catch (e) {
    // ★ 助手没起：明确告诉组员**先起 Agent**，别只说一句「没检测到」
    //   （用户 2026-10-23：「组员网站运行这个记得请组员先起Agent」）
    helperMsg('没检测到本机助手 —— ‹请先起 Agent›', true);
    toast('还没起 Agent：先双击包里的「启动助手.cmd」；若助手窗口**已经在跑**却仍提示没检测到，多半是 Edge 拦了「本地网络访问」——点地址栏**左侧**那个图标 → 选「允许此网站访问你的本地网络」；还不行就双击包里的「自检.cmd」，它会直接告诉你哪一环不通', true);
    setAgentFirstState(null);
  }
}

/** 成员侧把「列表采集」的参数转成 agent 能懂的源描述。 */
function localSourcesFromPicker() {
  const P = pickers.find(x => x.root.id === 'picker-list');
  const deptIds = P ? Array.from(P.sel) : [];
  const manual = ($('#j-sources').value || '').trim();
  const parts = [];
  if (manual) parts.push(manual);
  if (deptIds.length) parts.push('dept:' + deptIds.join(','));
  if ($('#dp-global').checked) parts.push('global');
  return parts.join(',');
}

$('#btn-list').onclick = () => {
  const P = pickers.find(x => x.root.id === 'picker-list');
  const deptIds = P ? Array.from(P.sel) : [];
  const pages = +$('#j-pages').value || 0;
  const cap = pages > 0 ? pages : 500;
  const srcs = deptIds.length + ($('#dp-global').checked ? 1 : 0);
  const maxReq = srcs * cap;
  if (!srcs && !$('#j-sources').value.trim()) {
    toast('先选采集源（点「全选」或勾选部门）', true); return;
  }
  if (maxReq > 5000) {
    const h = (maxReq * 1.0 / 3600).toFixed(1);
    if (!confirm(`即将启动 ${srcs} 条采集源，最坏 ${maxReq.toLocaleString()} 次请求`
      + `（按 1.0 秒/次约 ${h} 小时）。\n中途可随时点「停止」，进度会落库、可续采。\n\n继续？`)) return;
  }
  // ★ 成员：转交本机 agent（不能��管理员的机器去刷站方）
  if (isMemberView()) {
    const spec = localSourcesFromPicker();
    if (!spec) { toast('先选采集源（点「全选」或勾选部门）', true); return; }
    return startLocalCollect(
      { sources: spec, pages: Math.min(cap, 50) },
      '列表采集');
  }
  startJob('/api/jobs/list', {
    sources: $('#j-sources').value.trim(),
    dept_ids: deptIds,
    use_global: $('#dp-global').checked,
    pages: pages,
    stop_after: +$('#j-stopafter').value || 0,
    restart: $('#j-restart').checked,
    taxonomy: $('#j-taxonomy').checked,
    node: myNode(),
  }, '列表采集');
};
$('#dp-global').onchange = () => {
  const P = pickers.find(x => x.root.id === 'picker-list');
  if (P) updateListEstimate(P);
};
$('#j-pages').oninput = () => {
  const P = pickers.find(x => x.root.id === 'picker-list');
  if (P) updateListEstimate(P);
};
$('#btn-detail').onclick = () => {
  const tids = ($('#j-tid').value || '').split(/[,\s]+/).filter(Boolean)
    .map(Number).filter(Boolean);
  const P = pickers.find(x => x.root.id === 'picker-detail');
  const deptIds = serverCaps.pending && P ? Array.from(P.sel) : [];
  // ★ 成员：定点补采（填了 tid）→ 转交本机 agent；
  //   「按部门把该部门下还没采详情的帖都补齐」需要服务端调度，
  //   走**协作采集**领活那条路（本机 agent 不自己挑活）。
  if (isMemberView()) {
    if (tids.length) {
      return startLocalCollect({ tids: tids.join(',') }, '详情采集（定点补采）');
    }
    if (deptIds.length) {
      toast('「按部门补齐」请用「协作采集」领活 —— 那里会按池子把活派给你', true);
      return;
    }
    toast('先填 tid（定点补采），或到「协作采集」领活', true);
    return;
  }
  if (!tids.length && !deptIds.length) {
    toast('先填 tid 或按部门选（两者可叠加）', true);
    return;
  }
  if (!tids.length && deptIds.length) {
    const n = deptIds.length;
    if (!confirm(`将按 ${n} 个部门采集详情（这些部门下所有还没采详情的帖），先旧后新。\n`
      + `每帖 N×2 次请求，可能跑很久；中途可随时「停止」，进度会落库、可续采。\n\n继续？`)) return;
  }
  startJob('/api/jobs/detail', {
    limit: +$('#j-limit').value || 0,
    retry_failed: $('#j-retry').checked,
    tids: tids,
    dept_ids: deptIds,
    node: myNode(),
  }, '详情采集');
};
$('#j-limit').oninput = () => {
  const P = pickers.find(x => x.root.id === 'picker-detail');
  if (P) updateDetailEstimate(P);
};
$('#btn-export').onclick = async () => {
  try {
    const r = await api('/api/export', {
      method: 'POST',
      body: JSON.stringify({ out: $('#j-out').value || 'threads.csv' }),
    });
    $('#export-msg').innerHTML = `<div class="okbox">已导出 ${num(r.rows)} 条 → <code>${esc(r.path)}</code></div>`;
    toast(`导出完成：${r.rows} 条`);
  } catch (e) { toast('导出失败：' + e.message, true); }
};

const KIND_CN = { taxonomy: '分类体系', list: '列表采集', detail: '详情采集' };

async function loadJobs() {
  const r = await api('/api/jobs');
  const items = r.items || [];
  if (!items.length) {
    $('#job-list').innerHTML = '<div class="empty">还没有任务。上面挑一个开始。</div>';
    return;
  }
  $('#job-list').innerHTML = items.map(j => {
    const st = { running: 'teal', done: 'done', failed: 'failed', stopped: 'pending' }[j.state] || 'none';
    const p = j.kind === 'list'
      ? `源 ${j.params.sources}｜最多 ${j.params.pages || '不限'} 页`
      : j.kind === 'detail'
        ? `最多 ${j.params.limit || '不限'} 帖${j.params.tids?.length ? '｜定点 ' + j.params.tids.join(',') : ''}`
        : '部门目录 + 区县市 + 站方统计';
    return `<div class="job ${state.selJob === j.id ? 'sel' : ''}" data-jid="${j.id}">
      <span class="kind">${esc(KIND_CN[j.kind] || j.kind)}</span>
      <span class="badge ${st}">${esc(j.state)}</span>
      <span class="node">${esc(j.node || 'admin')}</span>
      <span class="p">${esc(p)}</span>
      <span class="st">${j.elapsed}s · ${j.log_count} 行日志</span>
      ${j.state === 'running'
        ? `<button class="tiny danger" data-stop="${j.id}">停止</button>` : ''}
    </div>`;
  }).join('');

  $$('#job-list .job').forEach(el => {
    el.onclick = ev => {
      if (ev.target.dataset.stop) {
        ev.stopPropagation();
        api(`/api/jobs/${ev.target.dataset.stop}/stop`, { method: 'POST' })
          .then(() => { toast('已请求停止'); loadJobs(); })
          .catch(e => toast(e.message, true));
        return;
      }
      state.selJob = el.dataset.jid;
      loadJobs();
    };
  });

  if (state.selJob) showJobLog(state.selJob);
}

async function showJobLog(jid) {
  try {
    const j = await api('/api/jobs/' + jid + '?tail=400');
    const box = $('#job-log');
    const head = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
        <b>${esc(KIND_CN[j.kind] || j.kind)} · ${esc(j.id)}</b>
        <span class="dim">${esc(j.state)} · 用时 ${j.elapsed}s${j.error ? ' · ' + esc(j.error) : ''}</span>
      </div>`;
    const lines = (j.logs || []).map(l => {
      const t = new Date(l.t * 1000).toLocaleTimeString('zh-CN');
      let cls = '';
      if (/✗|失败|不一致|错误/.test(l.m)) cls = 'bad';
      else if (/✓|完成|结束|命中/.test(l.m)) cls = 'ok';
      return `<span class="t">${t}</span><span class="${cls}">${esc(l.m)}</span>`;
    }).join('\n');
    const extra = j.result
      ? `<div class="okbox" style="margin:10px 0 0">结果：${esc(JSON.stringify(j.result))}</div>` : '';
    box.innerHTML = head + (lines || '<span class="dim">（还没有日志）</span>') + extra;
    box.scrollTop = box.scrollHeight;
  } catch (e) { /* 任务可能已被清理 */ }
}

function pollJobs() {
  if (state.jobTimer) clearInterval(state.jobTimer);
  state.jobTimer = setInterval(async () => {
    try {
      const r = await api('/api/jobs');
      const running = (r.items || []).filter(j => j.state === 'running');
      loadJobs();
      loadHealth();
      if (!running.length) { clearInterval(state.jobTimer); state.jobTimer = null; }
    } catch (_) {}
  }, 2000);
}

/* ------------------------------------------------------------------ 体检 */
/* 管理员专属页签。成员点不到（页签隐藏 + 接口 require_admin，两道都做）。 */
let INT_PICKS = [];        // 体检结果里可勾选的帖子
function intSelTids() {
  return $$('#integrity-out input[data-tid]:checked').map(cb => +cb.dataset.tid);
}
function intSelInfo() {
  const n = intSelTids().length;
  const el = $('#int-sel-info');
  if (el) el.textContent = n ? `已选 ${n} 帖` : '';
}
function intSetSel(tids, on) {
  const set = new Set(tids);
  $$('#integrity-out input[data-tid]').forEach(cb => {
    if (set.has(+cb.dataset.tid)) cb.checked = !!on;
  });
  intSelInfo();
}

async function runIntegrity() {
  const out = $('#integrity-out');
  if (!out) return;
  out.innerHTML = '<p class="hint">体检中…</p>';
  try {
    const r = await api('/api/integrity');
    let lc = { items: [], counts: {} };
    try { lc = await api('/api/list-changes?limit=200'); } catch (e) { /* 留痕表可能还没建 */ }
    let html = `<div class="okbox">检查 ${r.checked} 帖 · 通过 ${r.ok} 帖 · 问题 ${r.problems.length} 条`
      + ` · 软警告 ${r.warnings.length} 条 · 已识别增量 ${(r.growth || []).length} 条`
      + ` · 站方限制 ${(r.restricted || []).length} 条</div>`;

    const row = (tid, posts, pages, extra) =>
      `<tr><td><input type="checkbox" data-tid="${tid}"></td>`
      + `<td class="num">${tid}</td><td class="num">${posts ?? '—'}</td>`
      + (pages === null ? '' : `<td class="num">${pages ?? '—'}</td>`)
      + `<td>${extra}</td></tr>`;

    if (r.problems.length) {
      html += '<h4 style="margin:14px 0 6px">硬问题（要重跑或清除）</h4>'
        + '<div class="tablewrap"><table><thead><tr><th></th><th>TID</th><th>楼层</th>'
        + '<th>页</th><th>问题</th></tr></thead><tbody>'
        + r.problems.map(p => row(p.tid, p.posts, p.pages,
            esc(p.problems.join('；')))).join('')
        + '</tbody></table></div>';
    }
    if ((r.growth || []).length) {
      html += '<h4 style="margin:14px 0 6px">已识别的增量（站方又回复了 / 少贴 → 已标记待重采）</h4>'
        + '<div class="tablewrap"><table><thead><tr><th></th><th>TID</th><th>本地楼层</th>'
        + '<th>站方称</th><th>说明</th></tr></thead><tbody>'
        + r.growth.map(g => row(g.tid, g.posts, g.expect,
            esc(g.reason))).join('')
        + '</tbody></table></div>';
    }
    if (r.warnings.length) {
      html += '<h4 style="margin:14px 0 6px">软警告（站方计数问题 · 不算漏采）</h4>'
        + '<div class="tablewrap"><table><thead><tr><th></th><th>TID</th><th>本地楼层</th>'
        + '<th>说明</th></tr></thead><tbody>'
        + r.warnings.map(w => row(w.tid, w.posts, null,
            esc((w.warnings || []).join('；')))).join('')
        + '</tbody></table></div>'
        + '<p class="hint">软警告分两种：<b>楼层号中间缺</b>（站方删楼/置顶）——'
        + '那楼已经不存在了，<b>补采也没用</b>，不自动重采；'
        + '<b>站方称 N 楼而我们实收 M 楼、但楼层号连续</b>（尾部少）——'
        + '那是站方还有楼我们没采到，<b>已按增量自动安排补采</b>，会出现在上面那一栏。'
        + '</p>';
    }
    if ((r.restricted || []).length) {
      // ★ 站方限制：**不能勾选重跑/清除** —— 重跑多少次都是同一个提示页，
      //   勾了只会让成员白跑。单独列出来，让人知道这些帖为什么采不到。
      html += '<h4 style="margin:14px 0 6px">站方限制（正文不公开 · 已标记 · 不计入已采详情帖）</h4>'
        + '<div class="tablewrap"><table><thead><tr><th>TID</th><th>说明</th>'
        + '</tr></thead><tbody>'
        + r.restricted.map(g =>
          `<tr><td class="num">${g.tid}</td><td>${esc(g.reason || '')}</td></tr>`).join('')
        + '</tbody></table></div>'
        + '<p class="hint">这些帖的详情页在站方只给「提示信息」页（历史帖子仅限作者本人查看），'
        + '属于<b>站方的可见性策略</b>，不是漏采 —— 重跑不会变，因此不提供勾选。</p>';
    }
    if ((lc.items || []).length) {
      // ★ 列表阶段的变更留痕：站方什么时候受理的、什么时候回的、标题改没改
      const KIND = {
        new: '新帖', state: '受理状态变了', replies: '回复数变了', title: '标题改了',
      };
      // 说明写短一点（表头已有解释），别把列挤成竖排
      const WHY = {
        new: '首次出现', state: '受理流程随之变化，详情待重采',
        replies: '站方追加了楼层', title: '站方改了标题',
      };
      const rows = (lc.items || []).slice(0, 80).map(x => `<tr>
        <td class="sub2 nowrap">${esc(x.ts || '')}</td>
        <td class="sub2 nowrap">${esc(x.source || '')}</td>
        <td class="num nowrap">${esc(x.tid)}</td>
        <td class="nowrap">${esc(KIND[x.kind] || x.kind || '')}</td>
        <td class="sub2">${esc((x.old_val || '空').slice(0, 22))}</td>
        <td class="sub2">${esc((x.new_val || '空').slice(0, 22))}</td>
        <td class="sub2">${esc(WHY[x.kind] || '')}</td>
      </tr>`).join('');
      const c = lc.counts || {};
      html += '<h4 style="margin:14px 0 6px">列表阶段的变化（站方动过哪些帖）</h4>'
        + '<p class="hint">列表采集是<b>覆盖式</b>写入的 —— 旧值一被盖掉就再也查不到了。'
        + '这张表把「覆盖前那一瞬间」存下来，于是能回答：'
        + '这帖站方<b>什么时候受理的</b>、标题什么时候改的、这一趟多出了哪些新帖。<br>'
        + `当前留痕：新帖 ${c.new || 0} · 受理状态变了 ${c.state || 0} · `
        + `回复数变了 ${c.replies || 0} · 标题改了 ${c.title || 0}`
        + '</p>'
        + '<div class="tablewrap wide"><table><thead><tr><th>时间</th><th>采集源</th><th>TID</th>'
        + '<th>变化</th><th>旧值</th><th>新值</th><th>说明</th></tr></thead><tbody>'
        + rows + '</tbody></table></div>';
    }
    if (!r.problems.length && !r.warnings.length && !(r.growth || []).length
        && !(r.restricted || []).length && !(lc.items || []).length) {
      html += '<div class="okbox">全部干净。</div>';
    }
    out.innerHTML = html;
    $$('#integrity-out input[data-tid]').forEach(cb => {
      cb.onchange = intSelInfo;
    });
    intSelInfo();
    INT_PICKS = {
      problems: r.problems.map(p => p.tid),
      growth: (r.growth || []).map(g => g.tid),
      warnings: r.warnings.map(w => w.tid),
    };
  } catch (e) {
    out.innerHTML = `<div class="errbox">体检失败：${esc(e.message)}</div>`;
  }
}
$('#btn-integrity').onclick = runIntegrity;

$('#btn-int-sel-problem').onclick = () => { intSetSel(INT_PICKS.problems || [], true); };
$('#btn-int-sel-growth').onclick = () => { intSetSel(INT_PICKS.growth || [], true); };
$('#btn-int-sel-warn').onclick = () => { intSetSel(INT_PICKS.warnings || [], true); };
$('#btn-int-sel-all').onclick = () => {
  const all = $$('#integrity-out input[data-tid]');
  if (!all.length) {
    // ★ 之前这里什么都不做 → 点了像坏了。现在给明确提示。
    toast('当前没有可勾选的帖子（问题 / 增量 / 软警告都是空的）', true);
    return;
  }
  const allOn = all.every(cb => cb.checked);
  all.forEach(cb => { cb.checked = !allOn; });
  intSelInfo();
  toast(allOn ? '已取消全选' : `已全选 ${all.length} 帖`);
};
$('#btn-int-clear-sel').onclick = () => {
  $$('#integrity-out input[data-tid]').forEach(cb => { cb.checked = false; });
  intSelInfo();
};

$('#btn-int-retry').onclick = async () => {
  const tids = intSelTids();
  if (!tids.length) { toast('先勾选要重跑的帖子', true); return; }
  if (!confirm(`把 ${tids.length} 帖丢回队列重新采集？\n`
    + `（数据保留；重采到的新楼层会自动合并进来）`)) return;
  try {
    const r = await api('/api/integrity/retry', {
      method: 'POST', body: JSON.stringify({ tids }),
    });
    toast(`已重跑 ${r.reset} 帖 —— 启动详情采集任务即可重采`);
    runIntegrity();
  } catch (e) { toast(e.message, true); }
};

$('#btn-int-purge').onclick = async () => {
  const tids = intSelTids();
  if (!tids.length) { toast('先勾选要清除的帖子', true); return; }
  if (!confirm(`⚠ 清除 ${tids.length} 帖的详情？\n\n`
    + `这会**删掉**这些帖的全部楼层正文、分页凭据与受理流程，\n`
    + `清空后下一次采集从零开始。\n\n确定要清除吗？`)) return;
  try {
    const r = await api('/api/integrity/purge', {
      method: 'POST', body: JSON.stringify({ tids }),
    });
    toast(`已清除 ${r.purged} 帖（删了 ${r.posts} 个楼层、${r.pages} 页凭据、`
      + `${r.flows} 条流程）`);
    runIntegrity();
  } catch (e) { toast(e.message, true); }
};

/* ------------------------------------------------------------------ 日志 */
async function loadLog() {
  const ph = $('#log-phase').value;
  const r = await api('/api/fetchlog?limit=500' + (ph ? '&phase=' + ph : ''));
  const items = r.items || [];
  $('#t-log').innerHTML = `
    <thead><tr><th>时间</th><th>阶段</th><th>对象</th><th>类型</th><th>结果</th><th>说明</th></tr></thead>
    <tbody>${items.length ? items.map(x => `<tr>
      <td class="nowrap sub2">${esc(x.ts || '')}</td>
      <td>${esc(x.phase || '')}</td>
      <td class="sub2">${esc(x.target || '')}</td>
      <td class="sub2">${esc(x.kind || '')}</td>
      <td>${x.ok ? '<span class="badge done">ok</span>' : '<span class="badge failed">fail</span>'}</td>
      <td class="sub2">${esc(x.msg || '')}</td></tr>`).join('')
      : '<tr><td colspan="6" class="hint">暂无日志</td></tr>'}</tbody>`;
}
$('#btn-log-refresh').onclick = loadLog;
$('#log-phase').onchange = loadLog;

/* ------------------------------------------------------------------ 登录 / 协作（Beta） */
function showLogin(msg) {
  $('#login').classList.remove('hidden');
  if (msg) $('#lg-err').textContent = msg;
}
function hideLogin() {
  $('#login').classList.add('hidden');
  $('#lg-err').textContent = '';
}

$('#login-form').onsubmit = async (ev) => {
  ev.preventDefault();
  $('#lg-err').textContent = '';
  const btn = $('#lg-btn');
  btn.disabled = true; btn.textContent = '登录中…';
  try {
    const d = await api('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email: $('#lg-email').value.trim(), password: $('#lg-pw').value }),
    });
    state.token = d.token || '';
    state.me = d.member || {};
    saveToken(state.token);
    hideLogin();
    toast('登录成功 · 节点 ' + (state.me.node_id || ''));
    await afterLogin();
  } catch (e) {
    $('#lg-err').textContent = e.message;
  } finally {
    btn.disabled = false; btn.textContent = '登录';
  }
};

/* ---------------- 注册（自助） ---------------- */
$('#lg-to-reg').onclick = (e) => {
  e.preventDefault();
  $('#login-form').classList.add('hidden');
  $('#reg-form').classList.remove('hidden');
  $('#rg-err').textContent = '';
};
$('#rg-to-login').onclick = (e) => {
  e.preventDefault();
  $('#reg-form').classList.add('hidden');
  $('#login-form').classList.remove('hidden');
  $('#lg-err').textContent = '';
};
$('#reg-form').onsubmit = async (ev) => {
  ev.preventDefault();
  $('#rg-err').textContent = '';
  const pw = $('#rg-pw').value, pw2 = $('#rg-pw2').value;
  if (pw !== pw2) { $('#rg-err').textContent = '两次输入的密码不一样'; return; }
  const btn = $('#rg-btn');
  btn.disabled = true; btn.textContent = '注册中…';
  try {
    const d = await api('/api/auth/register', {
      method: 'POST',
      body: JSON.stringify({ email: $('#rg-email').value.trim(), password: pw }),
    });
    toast(d.message || '注册成功');
    $('#reg-form').classList.add('hidden');
    $('#login-form').classList.remove('hidden');
    $('#lg-email').value = $('#rg-email').value.trim();
    $('#lg-pw').value = pw;
    $('#lg-err').textContent = d.message || '注册成功，可以登录了';
  } catch (e) {
    $('#rg-err').textContent = e.message;
  } finally {
    btn.disabled = false; btn.textContent = '注册';
  }
};

/* 登录页：告诉管理员自己的账号是哪个（否则会试 admin@example.com 登不进去） */
(async () => {
  try {
    const c = await api('/api/auth/config');
    $('#lg-tip').innerHTML =
      `管理员账号：<code>${esc(c.admin_email)}</code>`
      + (c.allow_domains ? `　（允许域名：${esc(c.allow_domains)}）` : '');
  } catch (e) { /* 后端还没重启就先不显示 */ }
})();

/* 退出登录：成员也要能自己退出（不然换人用电脑时登录状态还挂着） */
function doLogout() {
  clearToken();
  state.token = ''; state.me = null;
  if (state.es) { state.es.close(); state.es = null; }
  if (_helperTimer) { clearInterval(_helperTimer); _helperTimer = null; }
  $('#btn-pw-top').classList.add('hidden');
  $('#btn-logout-top').classList.add('hidden');
  // 退出后回到登录页，并把页签恢复成默认的「总览」
  $$('#tabs button').forEach((b, i) => b.classList.toggle('active', i === 0));
  $$('.panel').forEach(p => p.classList.add('hidden'));
  $('#tab-overview').classList.remove('hidden');
  showLogin();
  toast('已退出登录');
}

function renderMe() {
  const isAdmin = (state.me && state.me.role) === 'admin';
  const node = myNode();
  $('#node-badge').textContent = node;
  $('#me-info').innerHTML = `
    <div class="k">邮箱</div><div class="v">${esc(state.me.email || '')}</div>
    <div class="k">角色</div><div class="v">${esc(state.me.role || '')}</div>
    <div class="k">节点号</div><div class="v"><code>${esc(node)}</code></div>
    <div class="k">单次配额</div><div class="v">不限 <span class="dim">（在线即可）</span></div>`;
  const srv = location.origin;
  const cmd = `python -m msyt agent --server ${srv} --email ${state.me.email} --password 你的密码`;
  $('#agent-cmd').value = cmd;
  $('#btn-copy-cmd').onclick = () => {
    navigator.clipboard?.writeText(cmd);
    toast('已复制到剪贴板');
  };
  $('#btn-logout').onclick = doLogout;
  $('#btn-logout-top').onclick = doLogout;
  $('#assist-member-tip').innerHTML =
    `领活<b>不限配额</b>：想领多少领多少（一次领 <b>${$('#as-limit').value || 50}</b> 帖）。`
    + `领完请在自己机器上跑上面的 agent 命令；`
    + `若中途<b>与服务器失联</b>，agent 会<b>自动停止</b>，不会继续白采。`;
  $('#pw-who').textContent = `账号：${state.me.email || ''}　·　节点：${node}`;
  return isAdmin;
}

async function afterLogin() {
  const isAdmin = renderMe();

  // ---- 界面层：按角色裁掉不该看的页签与卡片 ----
  $$('#tabs button').forEach(b => {
    const tab = b.dataset.tab;
    const visible = isAdmin || TABS_FOR_MEMBER.includes(tab);
    b.style.display = visible ? '' : 'none';
  });
  $('#btn-pw-top').classList.remove('hidden');
  $('#btn-logout-top').classList.remove('hidden');
  $('#assist-admin').style.display = isAdmin ? '' : 'none';
  $('#assist-member').style.display = isAdmin ? 'none' : '';
  $('#members-card').style.display = isAdmin ? '' : 'none';
  $('#conflicts-card').style.display = isAdmin ? '' : 'none';
  // ★ 成员**不能**在服务器上跑采集（站方不提供 CORS，且那样站方看到的
  //   流量全来自管理员那一个 IP）。所以成员的「开始采集」改为**转交本机 agent**，
  //   「分类体系抓取」是权威库的基础设施 → 管理员专属。
  $('#btn-taxonomy').style.display = isAdmin ? '' : 'none';
  // ★ 「先起 Agent」引导卡：**只给成员看**（2026-10-23 用户要求）。
  //   管理员在服务器上采（服务器就是自己的机器），不需要这层提醒；
  //   成员必须在自己的电脑上起 agent，所以这张卡要摆在「采集作业」最前面。
  $('#agent-first-card').style.display = isAdmin ? 'none' : '';
  $('#btn-list').title = isAdmin
    ? '在**这台机器**上采集（管理员端）'
    : '在**你自己的电脑**上采集（需要本机 agent）';
  $('#btn-detail').title = $('#btn-list').title;

  if (isAdmin) await loadMembers();
  await loadAssistTasks();
  startStream();
  // 登录后重新探一次能力（未登录时 /api/pending 探不通，详情选择器会被误禁）
  probeServerCaps().then(() => {
    const P = pickers.find(x => x.root.id === 'picker-detail');
    if (P) renderPicker(P);
  });
}

/* ---------------- 成员名单（仅管理员） ---------------- */
async function loadMembers() {
  const d = await api('/api/members');
  $('#t-members').innerHTML = `
    <thead><tr><th>邮箱</th><th>角色</th><th>节点号</th><th>状态</th><th>在线</th>
    <th>单次配额</th><th>最近登录</th><th>备注</th><th>操作</th></tr></thead>
    <tbody>${(d.items || []).map(m => `<tr>
      <td>${esc(m.email)}</td>
      <td>${esc(m.role)}</td>
      <td class="sub2">${esc(m.node_id || '')}</td>
      <td><span class="badge ${m.status === 'active' ? 'done' : (m.status === 'pending' ? 'teal' : 'failed')}">${esc(m.status)}</span></td>
      <td>${m.online ? '<span class="badge done">在线</span>' : '<span class="badge none">离线</span>'}</td>
      <td class="num">不限</td>
      <td class="sub2">${esc(m.last_login || '')}</td>
      <td class="sub2">${esc(m.note || '')}</td>
      <td class="nowrap">
        ${m.status === 'pending' ? `<button class="tiny" data-approve="${m.id}">通过</button> ` : ''}
        <button class="tiny" data-toggle="${m.id}">${m.status === 'active' ? '停用' : '启用'}</button>
        <button class="tiny" data-reset="${m.id}">重置密码</button>
        <button class="tiny danger" data-del="${m.id}">删除</button>
      </td>
    </tr>`).join('')}</tbody>`;

  const act = async (path, body, ok) => {
    try {
      const r = await api(path, { method: 'POST', body: JSON.stringify(body || {}) });
      toast(ok(r));
      loadMembers();
    } catch (e) { toast(e.message, true); }
  };

  $$('#t-members [data-approve]').forEach(b => b.onclick = () =>
    act(`/api/members/${b.dataset.approve}/approve`, {}, r => `已通过 ${r.email}`));
  $$('#t-members [data-toggle]').forEach(b => b.onclick = () =>
    act(`/api/members/${b.dataset.toggle}/toggle`, {}, r => `${r.email} → ${r.status}`));
  // ★ 不再有「配额」按钮：Beta 期间**不限配额**，在线即可。
  //   留一个能改配额的按钮反而会让人以为改了有用 —— 不如直接去掉。
  $$('#t-members [data-reset]').forEach(b => b.onclick = async () => {
    const r = await api(`/api/members/${b.dataset.reset}/reset-password`,
      { method: 'POST', body: JSON.stringify({}) });
    toast(`${r.email} 新密码：${r.password}（只显示这一次）`);
    toast(r.message || '提醒对方登录后自行改密码', false);
    loadMembers();
  });
  $$('#t-members [data-del]').forEach(b => b.onclick = async () => {
    if (!confirm('确定删除这个成员？')) return;
    try {
      const r = await api(`/api/members/${b.dataset.del}`, { method: 'DELETE' });
      toast(`已删除 ${r.email}`);
      loadMembers();
    } catch (e) { toast(e.message, true); }
  });
}
$('#btn-mb-add').onclick = async () => {
  const email = $('#mb-email').value.trim();
  if (!email) { toast('先填邮箱', true); return; }
  try {
    const r = await api('/api/members', {
      method: 'POST',
      body: JSON.stringify({ email, password: $('#mb-pw').value, note: $('#mb-note').value }),
    });
    toast(`已添加 ${r.email}`);
    if (r.password) toast(`初始密码：${r.password}（只显示这一次）`);
    $('#mb-email').value = ''; $('#mb-pw').value = ''; $('#mb-note').value = '';
    loadMembers();
  } catch (e) { toast(e.message, true); }
};

/* ---------------- 改密码（正式表单） ----------------
   ★ 原来是 prompt() 两次 —— 那不是界面，是凑合：
     弹的是浏览器原生框，没有强度提示、没有确认校验、输了错还会丢内容。
   ★ 更要紧的是「**重置密码要能自行修改**」：
     管理员重置成员密码只是给一个能登进去的临时口令，
     真正的密码必须由本人自己定 —— 否则管理员知道的密码就是唯一能用的密码。
     所以这个表单是**每个登录者**（成员也包括）都能用的全局入口。 */

function pwScore(pw) {
  let s = 0;
  if ((pw || '').length >= 6) s++;
  if ((pw || '').length >= 10) s++;
  if (/[a-z]/.test(pw) && /[A-Z]/.test(pw)) s++;
  if (/\d/.test(pw)) s++;
  if (/[^\w\s]/.test(pw)) s++;
  return s;                       // 0–5
}

function updateStrength() {
  const pw = $('#pw-new').value || '';
  const s = pwScore(pw);
  const names = ['太弱', '太弱', '弱', '一般', '较强', '很强'];
  const bar = $('#pw-bar');
  const txt = $('#pw-strength');
  if (!pw) {
    bar.style.width = '0%';
    bar.className = '';
    txt.textContent = '强度：—';
    return;
  }
  const pct = Math.min(100, s * 20);
  bar.style.width = pct + '%';
  bar.className = s <= 2 ? 'weak' : (s <= 3 ? 'mid' : 'strong');
  txt.textContent = `强度：${names[s]}${pw.length < 6 ? '（至少 6 位）' : ''}`;
}

function openPwModal() {
  $('#pw-who').textContent =
    `账号：${(state.me && state.me.email) || '—'}　·　节点：${myNode()}`;
  $('#pw-old').value = '';
  $('#pw-new').value = '';
  $('#pw-new2').value = '';
  $('#pw-err').textContent = '';
  updateStrength();
  $('#modal-pw').classList.add('on');
  setTimeout(() => $('#pw-old').focus(), 40);
}

function closePwModal() {
  $('#modal-pw').classList.remove('on');
}

$('#btn-pw-change').onclick = openPwModal;
$('#btn-pw-change2').onclick = openPwModal;
$('#btn-pw-top').onclick = openPwModal;
$('#pw-cancel').onclick = closePwModal;
$('#modal-pw').addEventListener('click', (ev) => {
  if (ev.target === $('#modal-pw')) closePwModal();
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape' && $('#modal-pw').classList.contains('on')) closePwModal();
});
$('#pw-new').addEventListener('input', updateStrength);
$$('.pw-eye').forEach(b => b.onclick = () => {
  const el = $('#' + b.dataset.eye);
  const show = el.type === 'password';
  el.type = show ? 'text' : 'password';
  b.textContent = show ? '隐藏' : '显示';
});

$('#pw-form').onsubmit = async (ev) => {
  ev.preventDefault();
  const err = $('#pw-err');
  err.textContent = '';
  const oldp = $('#pw-old').value;
  const np = $('#pw-new').value;
  const np2 = $('#pw-new2').value;
  if (np.length < 6) { err.textContent = '新密码至少 6 位'; return; }
  if (np !== np2) { err.textContent = '两次输入的新密码不一致'; return; }
  if (np === oldp) { err.textContent = '新密码不能和当前密码一样'; return; }
  const btn = $('#pw-submit');
  btn.disabled = true; btn.textContent = '提交中…';
  try {
    await api('/api/auth/password', {
      method: 'POST',
      body: JSON.stringify({
        old_password: oldp, new_password: np, confirm_password: np2,
      }),
    });
    closePwModal();
    toast('密码已修改 —— 请牢记新密码');
    $('#pw-state').textContent = '最近一次修改：刚刚（' +
      new Date().toLocaleString('zh-CN') + '）';
  } catch (e) {
    err.textContent = e.message;
  } finally {
    btn.disabled = false; btn.textContent = '确认修改';
  }
};

/* ---------------- 成员端工具：下载启动包 / 一键启动本机 agent ----------------
   ★ 为什么要有「下载启动包」这条路：
     成员的机器上没有 msyt 这个包，`python -m msyt agent …` 直接报
     `No module named msyt`。所以**不能**只给一条命令，
     得把「网站把命令传给组员、以下载的形式、组员运行开启」这条链打通。
   ★ 网址是动态的：启动包由服务端**实时生成**，`--server` 用的是
     下载者此刻访问的地址（本地 127.0.0.1 还是公网域名，自动对上）。 */

const HELPER_URL = 'http://127.0.0.1:8766';

$('#btn-bundle').onclick = async () => {
  const msg = $('#bundle-msg');
  msg.textContent = '正在生成…';
  try {
    const res = await fetch('/api/agent/bundle.zip', {
      headers: { 'Authorization': 'Bearer ' + state.token },
    });
    if (!res.ok) {
      let t = res.statusText;
      try { t = (await res.json()).detail || t; } catch (_) {}
      throw new Error(t);
    }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'msyt-成员端启动包.zip';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    msg.innerHTML = '已下载 → 解压后双击 <b>启动采集.cmd</b>，'
      + '装完依赖后按提示输入<b>邮箱和密码</b>即可开跑。';
    toast('启动包已下载');
  } catch (e) {
    msg.innerHTML = `<span style="color:#b42318">下载失败：${esc(e.message)}</span>`;
  }
};

/* 本机助手（127.0.0.1:8766）：网页 → 成员设备的那条路 */
async function helperCall(path, body) {
  const ctl = new AbortController();
  // ★ 超时 4 秒 → 10 秒（2026-10-23 晚上组员实测）：
  //   Edge 访问本机要走「私有网络访问(PNA)」预检 + 本地网络权限检查，
  //   这一步可能就要好几秒 —— 4 秒太紧，
  //   助手明明在跑，网页却报「没检测到本机助手」（实测踩到）。
  const t = setTimeout(() => ctl.abort(), 10000);
  try {
    const res = await fetch(HELPER_URL + path, {
      method: body ? 'POST' : 'GET',
      headers: { 'Content-Type': 'application/json' },
      body: body ? JSON.stringify(body) : undefined,
      signal: ctl.signal,
    });
    return await res.json();
  } finally { clearTimeout(t); }
}

function helperMsg(txt, bad) {
  const el = $('#helper-msg');
  // ★ 注意：这里用 esc() 转义，所以传进来的 markdown 星号会**原样显示**
  //   （实测：「‹请先起 Agent›」变成满屏星号）。
  //   要强调就用 <b> 标签 —— 但 txt 会被转义，<b> 也会变字面量。
  //   所以：把「要强调的部分」用 ‹› 包起来，这里替换成 <b>…</b>。
  const html = esc(txt).replace(/‹([^›]*)›/g, '<b>$1</b>');
  el.innerHTML = bad ? `<span style="color:#b42318">${html}</span>` : html;
}

$('#btn-helper-start').onclick = async () => {
  helperMsg('正在呼叫本机助手…');
  try {
    // 把当前网页的 JWT 递过去 —— 助手不用问密码，也不用把密码落到磁盘
    const r = await helperCall('/start', { token: state.token });
    if (r && r.ok) {
      helperMsg(r.already ? '本机 agent 已经在跑了' : '本机 agent 已启动');
      toast('本机 agent 已启动');
      pollHelper();
    } else {
      helperMsg((r && r.error) || '助手拒绝了', true);
    }
  } catch (e) {
    helperMsg('没检测到本机助手 —— 还没起 Agent：先双击包里的「启动助手.cmd」；若助手窗口**已经在跑**却仍提示没检测到，多半是 Edge 拦了「本地网络访问」——点地址栏**左侧**那个图标 → 选「允许此网站访问你的本地网络」；还不行就双击包里的「自检.cmd」，它会直接告诉你哪一环不通', true);
  }
};

$('#btn-helper-stop').onclick = async () => {
  try {
    await helperCall('/stop', {});
    helperMsg('已要求本机 agent 停止');
  } catch (e) {
    helperMsg('没检测到本机助手', true);
  }
};

let _helperTimer = null;
let _helperFails = 0;
async function pollHelper() {
  if (_helperTimer) clearInterval(_helperTimer);
  const box = $('#helper-log');
  // ★ 第一次**立即**探一次（2026-10-23 改）：
  //   原来只有 setInterval(3000)，用户进页面要等 3 秒才看到「本机 Agent 运行中」，
  //   徽章一直停在「正在探测…」—— 而这 3 秒正是他判断「能不能开工」的时候。
  const once = async () => {
    let r;
    try {
      r = await helperCall('/status');
      _helperFails = 0;
    } catch (e) {
      // 助手没开：连续失败几次就别再戳了（免得控制台一堆红）
      _helperFails++;
      setAgentFirstState(null);
      if (_helperFails >= 3) {
        clearInterval(_helperTimer);
        _helperTimer = null;
        box.innerHTML = '<p class="hint"><b>没检测到本机助手（127.0.0.1:8766）</b><br>'
          + '① 双击包里的「启动助手.cmd」<br>'
          + '② 助手窗口已经在跑却还报这个 → 多半是 <b>Edge 拦了「本地网络访问」</b>：'
          + '点地址栏<b>左侧</b>那个图标 → 选「允许此网站访问你的本地网络」<br>'
          + '③ 还不行 → 双击包里的「自检.cmd」，它会直接告诉你哪一环不通</p>';
      }
      return;
    }
    if (!r || !r.ok) return;
    const lines = (r.log || []).map(l => {
      const t = new Date(l.t * 1000).toLocaleTimeString('zh-CN');
      return `<span class="t">${t}</span><span>${esc(l.m)}</span>`;
    });
    // ★ 从 agent 的日志里数出「领了多少 / 完成了多少 / 最后活动时间」
    //   —— 光有一句「运行中 27s」没法确认到底采没采。
    //   优先用助手**累计**的计数（stat_*）；老助手没有这些字段时
    //   再退回从可见日志窗口里正则数（会随窗口滚动，不准但有个数）。
    const hasStat = typeof r.stat_done === 'number';
    const all = (r.log || []).map(l => l.m).join('\n');
    const picked = hasStat ? r.stat_picked
      : (all.match(/领走 (\d+) 帖/g) || []).reduce((s, m) => s + (+m.replace(/\D+/g, '')), 0);
    const done = hasStat ? r.stat_done : (all.match(/tid=\d+ ✓/g) || []).length;
    const failed = hasStat ? r.stat_failed : (all.match(/✗/g) || []).length;
    const restricted = hasStat ? r.stat_restricted : 0;
    const last = (r.log || []).length ? r.log[r.log.length - 1].t : 0;
    const idle = last ? Math.round((Date.now() / 1000) - last) : null;
    // ★ agent 自报状态（2026-10-23 加）
    //   agent 会在日志里打 `[[state]] idle` / `[[state]] collecting`，
    //   助手把它摘出来放进 /status。有了它，网页就能区分
    //   「**在等活**」（池子被占满/被领完）与「**卡住了**」——
    //   这两种情况以前混成一条「长时间没动静」，用户完全没法判断。
    const astate = r.agent_state || '';
    const anote = r.agent_state_note || '';
    let head;
    if (!r.running) {
      head = '<div class="warnbox">本机 agent <b>未运行</b>'
        + (r.log && r.log.length ? '（最后活动：' + ago(idle) + '）' : '') + '</div>';
    } else if (astate === 'idle') {
      head = '<div class="warnbox">本机 agent 在运行，但<b>池子里没有可领的活</b>'
        + (anote ? '（' + esc(anote) + '）' : '')
        + '<br>这是<b>正常等待</b>，不是卡住 —— 等别人回传，或等超时的活自动退回池子。</div>';
    } else if (astate === 'collecting') {
      head = '<div class="okbox">本机 agent 运行中（pid ' + r.pid + '）· 已运行 '
        + r.uptime + 's · <b>已领 ' + picked + ' 帖 · 已完成 ' + done + ' 帖</b>'
        + (restricted ? ' · 站方限制 ' + restricted : '')
        + (failed ? ' · 失败 ' + failed : '')
        + (idle !== null ? ' · 最后活动 ' + ago(idle) + '：' : '　')
        + (r.log && r.log.length ? esc(r.log[r.log.length - 1].m) : '') + '</div>';
    } else if (idle !== null && idle > 180) {
      head = '<div class="warnbox">本机 agent 显示运行中，但<b>已经 '
        + idle + ' 秒没有新动静</b> —— 可能卡住了（看下面的日志最后一行）'
        + '<br>本轮已领 ' + picked + ' 帖 · 已完成 ' + done + ' 帖</div>';
    } else {
      head = '<div class="okbox">本机 agent 运行中（pid ' + r.pid + '）· 已运行 '
        + r.uptime + 's · <b>已领 ' + picked + ' 帖 · 已完成 ' + done + ' 帖</b>'
        + (restricted ? ' · 站方限制 ' + restricted : '')
        + (failed ? ' · 失败 ' + failed : '')
        + (idle !== null ? ' · 最后活动 ' + ago(idle) + '：' : '　')
        + (r.log && r.log.length ? esc(r.log[r.log.length - 1].m) : '') + '</div>';
    }
    box.innerHTML = head + (lines.length ? lines.join('\n')
      : '<span class="dim">（还没有日志）</span>');
    box.scrollTop = box.scrollHeight;
    // ★ 同步「先起 Agent」引导卡上的状态徽章（2026-10-23 加）
    setAgentFirstState(r, picked, done);
  };
  _helperTimer = setInterval(once, 3000);
  once();          // ★ 立即探一次，别让用户对着「正在探测…」干等 3 秒
}

/* 「先起 Agent」引导卡的状态徽章。
   ★ 为什么单独做一张引导卡（用户 2026-10-23：「组员网站运行这个记得请组员先起Agent」）：
     组员进「采集作业」看到的是一整屏服务器端采集的配置，
     不知道采集其实发生在**自己的电脑**上。
     这张卡把「为什么」和「怎么做」摆在最前面，状态徽章实时告诉他能不能开工。 */
function setAgentFirstState(r, picked, done) {
  const el = $('#agent-first-state');
  if (!el) return;
  if (!r || !r.ok) {
    el.innerHTML = '<span style="color:#b42318">● 本机 Agent 未运行</span>'
      + '　—— 请按下面的步骤启动后再点「开始采集」';
    return;
  }
  if (!r.running) {
    el.innerHTML = '<span style="color:#b42318">● 助手已启动，但本机 Agent 没在跑</span>'
      + '　—— 点上面的「启动本机 agent」';
    return;
  }
  const astate = r.agent_state || '';
  const tip = astate === 'idle'
    ? '（在等活，池子里暂时没有可领的）'
    : (astate === 'collecting' ? '（正在采）' : '');
  el.innerHTML = '<span style="color:#0a7d55">● 本机 Agent 运行中</span>'
    + (picked !== undefined ? `　已领 ${picked} 帖 · 已完成 ${done} 帖` : '')
    + `　${tip}　—— 可以开始采集了`;
}

/* ---------------- 协作任务 ---------------- */
/** 把秒数说成人话：「刚刚 / 3 分钟前 / 2 小时前」 */
function ago(sec) {
  if (sec === null || sec === undefined) return '—';
  if (sec < 60) return `${sec} 秒前`;
  if (sec < 3600) return `${Math.floor(sec / 60)} 分钟前`;
  if (sec < 86400) return `${Math.floor(sec / 3600)} 小时前`;
  return `${Math.floor(sec / 86400)} 天前`;
}

async function loadAssistTasks() {
  const d = await api('/api/assist/tasks');
  const items = d.items || [];
  const isAdmin = !!(state.me && state.me.role === 'admin');
  $('#t-assist').innerHTML = `
    <thead><tr><th>编号</th><th>标题</th><th>状态</th><th>进度</th>
    <th>进行中</th><th>还剩</th><th>站方限制</th><th>各节点（完成/已领/受限）</th>
    <th>最近回传</th><th>创建时间</th>${isAdmin ? '<th>操作</th>' : ''}</tr></thead>
    <tbody>${items.length ? items.map(t => {
      const pct = t.total ? Math.round(t.done * 100 / t.total) : 0;
      // ★ 「站方限制」单列一栏：正文不公开的帖不是失败，是**采不到**。
      //   不单列的话，「完成 0 / 失败 500」看着像程序坏了。
      const restricted = t.restricted || 0;
      const nodes = Object.entries(t.by_node_detail || {}).map(([n, v]) =>
        `<span class="node-chip" title="已完成 ${v.done} / 已领 ${v.taken} / 失败 ${v.failed} / 站方限制 ${v.restricted || 0}">
           ${esc(n)} <b>${v.done}/${v.total}</b>${v.restricted ? ` <i class="dim">限${v.restricted}</i>` : ''}</span>`).join(' ') || '—';
      // ★ 撤销：已完成的**保留**（正文已在库里），未完成的作废
      let op = '';
      if (isAdmin) {
        if (t.can_cancel) {
          op = `<button class="tiny" data-cancel="${esc(t.code)}">撤销</button>`;
        } else if (t.can_restore) {
          op = `<button class="tiny" data-restore="${esc(t.code)}">恢复</button>`;
        }
        // ★ 移除：只有**没有正在被采的活**时才能做
        //   （有人在采时移除，他的回传会落到空处 → 先撤销）
        if (!t.inflight) {
          op += ` <button class="tiny" data-remove="${esc(t.code)}">移除</button>`;
        }
      }
      return `<tr class="clickable" data-open="${esc(t.code)}" title="点开看这个任务的详情">
        <td><b>${esc(t.code)}</b></td>
        <td>${esc(t.title || '')}</td>
        <td><span class="badge ${t.state === 'done' ? 'done'
          : (t.state === 'active' ? 'teal'
            : (t.state === 'cancelled' ? 'warn' : 'pending'))}"
          >${esc(t.state === 'cancelled' ? '已撤销' : t.state)}</span></td>
        <td class="num">${t.done}/${t.total}
          <span class="bar-mini"><i style="width:${pct}%"></i></span></td>
        <td class="num">${t.inflight || 0}</td>
        <td class="num">${t.pending || 0}</td>
        <td class="num" title="站方对历史帖子只给提示页，正文不公开（非失败）">${restricted}</td>
        <td class="sub2">${nodes}</td>
        <td class="sub2">${esc(t.last_activity || '—')}</td>
        <td class="sub2">${esc(t.created_at || '')}</td>
        ${isAdmin ? `<td class="nowrap">${op || '<span class="dim">—</span>'}</td>` : ''}
      </tr>`;
    }).join('') : `<tr><td colspan="${isAdmin ? 11 : 10}" class="hint">还没有协作任务</td></tr>`}</tbody>`;
  // ---- 点行 → 看这个任务的详情（成员也能看）----
  $$('#t-assist tr[data-open]').forEach(tr => {
    tr.onclick = (ev) => {
      if (ev.target.closest('button')) return;   // 点在按钮上就别开详情
      openTask(tr.dataset.open);
    };
  });
  // ---- 撤销 / 恢复 ----
  $$('#t-assist [data-cancel]').forEach(b => b.onclick = () => {
    const code = b.dataset.cancel;
    const t = items.find(x => x.code === code) || {};
    const done = t.done || 0;
    const undone = (t.pending || 0) + (t.inflight || 0);
    if (!confirm(`撤销协作任务 ${code}？\n\n`
      + `· 这个任务**不再派活**；\n`
      + `· 已完成的 ${done} 帖**保留**（正文已经在权威库里，不会被废弃）；\n`
      + `· 未完成的 ${undone} 帖作废（只是标记，不删数据，之后还能「恢复」）。\n\n继续？`)) return;
    api('/api/assist/cancel', { method: 'POST', body: JSON.stringify({ code: code }) })
      .then(r => {
        toast(`已撤销 ${code}（已完成的 ${r.kept} 帖保留，未完成的 ${r.cancelled} 帖作废）`);
        loadAssistTasks();
      }).catch(e => toast(e.message, true));
  });
  $$('#t-assist [data-restore]').forEach(b => b.onclick = () => {
    const code = b.dataset.restore;
    if (!confirm(`恢复协作任务 ${code}？\n\n`
      + `作废的活会**重新放回池子**，成员可以继续领。\n`
      + `已完成的那些不受影响（本来就保留着）。\n\n继续？`)) return;
    api('/api/assist/restore', { method: 'POST', body: JSON.stringify({ code: code }) })
      .then(r => {
        toast(`已恢复 ${code}（${r.restored} 帖重新放回池子）`);
        loadAssistTasks();
      }).catch(e => toast(e.message, true));
  });
  // ---- 移除（不可恢复；已采到的正文**保留**）----
  $$('#t-assist [data-remove]').forEach(b => b.onclick = () => {
    const code = b.dataset.remove;
    const t = items.find(x => x.code === code) || {};
    if (!confirm(`把协作任务 ${code} **从列表里移除**？\n\n`
      + `· 这个任务不会再出现在列表里（**不可恢复**）；\n`
      + `· 已采到的 ${t.done || 0} 帖正文**保留**在权威库里，不会被删；\n`
      + `· 还没干的 ${t.pending || 0} 帖随任务一起作废；\n`
      + `· 合并痕迹（谁覆盖了什么）保留，仍可追责。\n\n继续？`)) return;
    api('/api/assist/task/' + encodeURIComponent(code), { method: 'DELETE' })
      .then(r => {
        toast(`已移除 ${code}（已完成的 ${r.done_kept} 帖正文仍在库里）`);
        loadAssistTasks();
      }).catch(e => toast(e.message, true));
  });
}

/* ---------------- 协作任务详情（抽屉） ----------------
   任务表只给一行汇总；「到底哪几帖采到了、哪几帖还欠着、哪几帖被站方限制了」
   只有看明细才知道 —— 成员也要能看（协作的意义就在这）。 */

/* 节点号形如 member:test@example.com —— 表里只显示邮箱部分，
   完整值放进 title（悬停可见），免得一列把表撑得没法看。 */
function shortNode(n) {
  const s = String(n || '');
  if (!s) return '—';
  const at = s.indexOf('@');
  const mail = at > 0 ? s.slice(at) : '';
  const local = at > 0 ? s.slice(0, at).replace(/^member:/, '') : s.replace(/^member:/, '');
  return mail ? local + mail : local;
}

async function openTask(code) {
  $('#dw-title').textContent = '载入中…';
  $('#dw-meta').textContent = '';
  $('#dw-body').innerHTML = '';
  $('#drawer').classList.add('on', 'wide');   // ★ 宽抽屉：7 列的明细表要放得下
  $('#mask').classList.add('on');
  try {
    const d = await api('/api/assist/task/' + encodeURIComponent(code) + '?limit=300');
    const t = d.task || {};
    $('#dw-title').textContent = `协作任务 ${t.code}` + (t.title ? ` · ${t.title}` : '');
    $('#dw-meta').innerHTML = [
      `状态 ${t.state === 'cancelled' ? '已撤销' : t.state}`,
      `进度 ${num(t.done)}/${num(t.total)}`,
      `进行中 ${num(t.inflight)}`, `待领 ${num(t.pending)}`,
      `失败 ${num(t.failed)}`, `站方限制 ${num(t.restricted)}`,
      `创建 ${esc(t.created_at || '—')}`,
    ].join(' · ');

    const nodes = Object.entries(t.by_node_detail || {}).map(([n, v]) =>
      `<span class="node-chip">${esc(n)} 完成 ${v.done} / 已领 ${v.taken}`
      + `${v.failed ? ` / 失败 ${v.failed}` : ''}`
      + `${v.restricted ? ` / 站方限制 ${v.restricted}` : ''}</span>`).join(' ') || '—';

    const ST = {
      done: '已完成', taken: '进行中', pending: '待领', failed: '失败',
      restricted: '站方限制', cancelled: '已作废',
    };
    const rows = (d.items || []).map(x => {
      const st = ST[x.state] || x.state;
      const cls = x.state === 'done' ? 'done'
        : (x.state === 'failed' ? 'failed'
          : (x.state === 'restricted' ? 'warn' : 'pending'));
      return `<tr>
        <td class="num">${esc(x.tid)}</td>
        <td class="sub2 col-title">${esc((x.title || '').slice(0, 40) || '—')}</td>
        <td><span class="badge ${cls}">${esc(st)}</span></td>
        <td class="sub2 col-node" title="${esc(x.node || '')}">${esc(shortNode(x.node))}</td>
        <td class="num">${num(x.posts || 0)}</td>
        <td class="num">${num(x.chars || 0)}</td>
        <td class="sub2 col-time">${esc(x.done_at || '')}</td>
      </tr>`;
    }).join('');

    let html = `<div class="secttl">概要</div>
      <div style="margin-bottom:12px">各节点贡献：${nodes}</div>`;
    html += `<div class="secttl">这个任务里的帖子（共 ${num(d.total)} 条，`
      + `显示 ${num((d.items || []).length)} 条）</div>
      <div class="tablewrap wide"><table>
        <thead><tr><th>TID</th><th class="col-title">标题</th><th>状态</th><th>采的节点</th>
        <th>楼层</th><th>字数</th><th>完成时间</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="7" class="hint">这个任务还没有帖子</td></tr>'}</tbody>
      </table></div>`;
    $('#dw-body').innerHTML = html;
  } catch (e) {
    $('#dw-title').textContent = '载入失败';
    $('#dw-body').innerHTML = `<div class="errbox">${esc(e.message)}</div>`;
  }
}

/* ---------------- 在线节点（谁在采、有没有卡住） ---------------- */
async function loadOnline() {
  let items = [];
  try { items = (await api('/api/assist/online')).items || []; } catch (e) { return; }
  const rows = items.filter(x => x.email !== 'qa-admin@example.com');
  $('#t-online').innerHTML = `
    <thead><tr><th>节点</th><th>成员</th><th>在线</th><th>状态</th><th>最后心跳</th>
    <th>已完成</th><th>进行中</th><th>失败</th><th>站方限制</th><th>最近回传</th></tr></thead>
    <tbody>${rows.length ? rows.map(x => `
      <tr>
        <td class="sub2"><code>${esc(x.node)}</code></td>
        <td>${esc(x.email)}</td>
        <td>${x.online
            ? '<span class="badge done">在线</span>'
            : `<span class="badge none">${x.seconds_ago === null ? '未接入' : '离线'}</span>`}</td>
        <td>${x.agent_state === 'collecting'
            ? '<span class="badge done">在采</span>'
            : x.agent_state === 'idle'
              ? '<span class="badge warn" title="' + esc(x.agent_note || '') + '">在等活</span>'
              : '<span class="dim">—</span>'}</td>
        <td class="sub2">${x.seconds_ago === null ? '—' : ago(x.seconds_ago)}</td>
        <td class="num">${x.done}</td>
        <td class="num">${x.taken}</td>
        <td class="num">${x.failed}</td>
        <td class="num" title="站方限制：正文不公开，不算成员的失败">${x.restricted || 0}</td>
        <td class="sub2">${esc(x.last_activity || '—')}</td>
      </tr>`).join('')
      : '<tr><td colspan="10" class="hint">还没有成员接入</td></tr>'}</tbody>`;
}

$('#btn-assist-create').onclick = async () => {
  const P = pickers.find(x => x.root.id === 'picker-assist');
  const ids = P ? Array.from(P.sel) : [];
  if (!ids.length) { toast('先选部门', true); return; }
  try {
    const d = await api('/api/assist/task', {
      method: 'POST',
      body: JSON.stringify({ title: $('#as-title').value, dept_ids: ids, total: +$('#as-total').value || 50 }),
    });
    toast(`已发布协作任务 ${d.task.code}（${d.task.total} 帖）`);
    loadAssistTasks();
  } catch (e) { toast(e.message, true); }
};

/* ---------------- 合并冲突记录 ----------------
   「不跳过、采取并覆盖」这条规则，必须**看得见**：
   哪些帖子被覆盖了、本地有多少、成员传了多少、最终按谁处理。

   ★ 2026-10-23 加：翻页 + 折叠
     这张表会越积越多（每条回传都留一行），一次铺开能顶掉好几屏，
     反而让人找不到要看的东西 → 每页 20 条 + 翻页条 + 可收起。 */
const CF_PAGE_SIZE = 20;
const cfState = { page: 0, total: 0, collapsed: false };

async function loadConflicts(page) {
  const box = $('#t-conflicts');
  if (!box) return;
  if (page !== undefined) cfState.page = Math.max(0, page);
  const offset = cfState.page * CF_PAGE_SIZE;
  try {
    const d = await api('/api/assist/conflicts?limit=' + CF_PAGE_SIZE
      + '&offset=' + offset);
    const items = d.items || [];
    cfState.total = Number(d.total || 0);
    const kindCn = {
      overwritten: '覆盖（成员版替换本地版）',
      merged_new: '新增（增量：站方追加的回复）',
      empty: '空回传',
      local_done: '本地已完整（旧规则遗留）',
      dup_pid: '楼层重复（旧规则遗留）',
      content_diff: '内容不一致（旧规则遗留）',
    };
    box.innerHTML = `
      <thead><tr><th>时间</th><th>任务</th><th>TID</th><th>来源节点</th><th>类型</th>
      <th>说明</th><th>合并前</th><th>成员回传</th></tr></thead>
      <tbody>${items.length ? items.map(c => `<tr>
        <td class="nowrap sub2">${esc(c.created_at || '')}</td>
        <td><b>${esc(c.task_code || '')}</b></td>
        <td class="num">${esc(c.tid)}</td>
        <td class="node" title="${esc(c.node || '')}">${esc(c.node || '')}</td>
        <td class="nowrap">${esc(kindCn[c.kind] || c.kind)}</td>
        <td class="txt" title="${esc(c.reason || '')}">${esc(c.reason || '')}</td>
        <td class="txt txt2" title="${esc(c.local_val || '')}">${esc(c.local_val || '')}</td>
        <td class="txt txt2" title="${esc(c.remote_val || '')}">${esc(c.remote_val || '')}</td>
      </tr>`).join('')
        : '<tr><td colspan="8" class="hint">还没有冲突记录</td></tr>'}</tbody>`;
    updateConflictsBar();
  } catch (e) {
    box.innerHTML = `<tbody><tr><td colspan="8" class="hint">载入失败：${esc(e.message)}</td></tr></tbody>`;
  }
}

function updateConflictsBar() {
  const pages = Math.max(1, Math.ceil(cfState.total / CF_PAGE_SIZE));
  // 删记录后当前页可能越界 → 回到最后一页
  if (cfState.page >= pages) cfState.page = pages - 1;
  const pg = $('#cf-page');
  if (pg) pg.textContent = `第 ${cfState.page + 1} / ${pages} 页（共 ${cfState.total} 条）`;
  const cnt = $('#cf-count');
  if (cnt) cnt.textContent = cfState.total ? `共 ${cfState.total} 条` : '暂无记录';
  const prev = $('#cf-prev'), next = $('#cf-next');
  if (prev) prev.disabled = cfState.page <= 0;
  if (next) next.disabled = cfState.page >= pages - 1;
}

/* 折叠：把表体收起来，只留一条「翻页折叠条」 */
function toggleConflicts() {
  cfState.collapsed = !cfState.collapsed;
  const wrap = $('#conflicts-card .tablewrap');
  const btn = $('#cf-toggle');
  if (wrap) wrap.classList.toggle('hidden', cfState.collapsed);
  if (btn) btn.textContent = cfState.collapsed ? '展开' : '收起';
}

$('#btn-conflicts-refresh').onclick = () => loadConflicts(0);
$('#cf-prev').onclick = () => loadConflicts(cfState.page - 1);
$('#cf-next').onclick = () => loadConflicts(cfState.page + 1);
$('#cf-toggle').onclick = toggleConflicts;

$('#btn-assist-pick').onclick = async () => {
  try {
    const d = await api('/api/assist/pick', {
      method: 'POST',
      body: JSON.stringify({ limit: +$('#as-limit').value || 50 }),
    });
    const n = (d.picks || []).length;
    toast(n ? `领到 ${n} 帖，请在自己机器上跑 agent` : '暂时没有可领的活');
  } catch (e) { toast(e.message, true); }
};

/* ---------------- SSE 实时日志 ---------------- */
function startStream() {
  if (state.es) { state.es.close(); state.es = null; }
  // ⚠ EventSource **不能带自定义请求头**，所以 token 只能放查询串
  //   （服务端只在 /api/stream 这个端点开这个口子，其他端点仍只认 Authorization 头）
  const url = '/api/stream?since=' + state.esSeq + '&token=' + encodeURIComponent(state.token);
  const es = new EventSource(url);
  state.es = es;
  es.onopen = () => { $('#st-status').textContent = '已连接'; };
  es.onerror = () => { $('#st-status').textContent = '连接中断，重连中…'; };
  es.onmessage = (ev) => {
    let r;
    try { r = JSON.parse(ev.data); } catch (_) { return; }
    state.esSeq = Math.max(state.esSeq, r.seq || 0);
    if (!$('#st-all').checked && state.me && state.me.role !== 'admin'
        && r.node !== state.me.node_id) return;
    appendStream(r);
  };
}

function appendStream(r) {
  const box = $('#stream-log');
  if (box.dataset.empty === '1') { box.innerHTML = ''; box.dataset.empty = '0'; }
  const line = document.createElement('div');
  line.className = 'stream-line';
  const t = new Date(r.t * 1000).toLocaleTimeString('zh-CN');
  line.innerHTML = `<span class="t">${t}</span>`
    + `<span class="node">${esc(r.node)}</span>`
    + (r.task ? `<span class="task">${esc(r.task)}</span>` : '')
    + `<span class="m ${esc(r.level || 'info')}">${esc(r.m)}</span>`;
  box.appendChild(line);
  while (box.children.length > 600) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
}
$('#btn-st-clear').onclick = () => {
  const box = $('#stream-log');
  box.innerHTML = '<p class="hint">等待日志…</p>';
  box.dataset.empty = '1';
};
$('#st-all').onchange = () => { /* 过滤在 onmessage 里做 */ };

/* ------------------------------------------------------------------ 启动 */
window.addEventListener('unhandledrejection', ev => {
  const m = (ev.reason && ev.reason.message) || String(ev.reason || '');
  toast('出错了：' + m, true);
});
loadHealth();
if (state.token) { loadMeta().catch(() => {}); }
probeServerCaps();
ensurePickers();
$('#stream-log').dataset.empty = '1';

// 恢复登录态（localStorage 里的 token）；没有就弹登录框
(async () => {
  if (!state.token) { showLogin(); return; }
  try {
    state.me = await api('/api/auth/me');
    hideLogin();
    await afterLogin();
  } catch (e) {
    clearToken();
    state.token = '';
    showLogin();
  }
})();
/* ⚠️ 总览是默认激活的页签，但页签的加载只在**点击**时触发 ——
   启动时必须自己调一次，否则首屏是空的（实测踩过）。 */
loadOverview();
setInterval(loadHealth, 15000);
setInterval(() => {
  // 有采集任务在跑时，总览数字会变，跟着刷
  if (state.jobTimer) loadOverview();
}, 5000);
