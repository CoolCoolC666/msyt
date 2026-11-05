/* 闸 + 预览生成：分布卡的 SVG 版式（2026-11-06 用户定的版式）
   —— **名称 + 数字在上，条形图在数字下方**。

   ★ 用**从 app.js 里抽出来的真函数**渲染，不是抄一份副本 ——
     抄的副本过了闸，真页面照样可能坏。

   ★ 顺带生成一张预览页（真 style.css + 真函数），
     用来肉眼验收版式：程序/msyt/web/_preview_bars.html

   跑法：node _tools/test_bar_layout.js
*/
'use strict';
const fs = require('fs');
const path = require('path');

const WEB = path.join(__dirname, '..', '程序', 'msyt', 'web');
const APP = path.join(WEB, 'app.js');
const FAILED = [];

function check(cond, msg) {
  console.log((cond ? '  [PASS] ' : '  [FAIL] ') + msg);
  if (!cond) FAILED.push(msg);
}

/* ---------- 从 app.js 抽真函数 ---------- */
function extractConst(src, name) {
  // ★ 定义可能**跨行**（esc 就是两行），而且里面**含分号**（'&#39;'）→
  //   不能拿「第一个分号」当结尾，改用「分号 + 换行」收尾。
  const at = src.search(new RegExp('^const ' + name + ' = ', 'm'));
  if (at < 0) throw new Error('app.js 里找不到 const ' + name);
  const end = src.indexOf(';\n', at);
  if (end < 0) throw new Error('const ' + name + ' 找不到结尾');
  return src.slice(at, end + 1);
}
function extractFunction(src, name) {
  const start = src.indexOf('function ' + name + '(');
  if (start < 0) throw new Error('app.js 里找不到 function ' + name);
  let depth = 0, i = src.indexOf('{', start);
  for (let j = i; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}') { depth--; if (depth === 0) return src.slice(start, j + 1); }
  }
  throw new Error('function ' + name + ' 括号不配对');
}

const src = fs.readFileSync(APP, 'utf-8');
const code = [
  extractConst(src, 'esc'),
  extractConst(src, 'num'),
  extractFunction(src, 'barChart'),
].join('\n');
const api = new Function(code + '\nreturn { esc: esc, num: num, barChart: barChart };')();

/* ---------- 渲染 ---------- */
const DATA = {
  '帖子类别分布': { '投诉': 250005, '咨询': 215134, '建议': 50210, '询问': 30976,
    '其他': 30616, '求助': 20220, '市民巡访': 6347, '点赞': 3520, '': 6 },
  '受理状态分布': { '已回复': 581441, '（站方未标）': 15451, '已受理': 8416, '未受理': 1726 },
  '部门分组': { 'district': 362280, 'func': 266789, 'enterprise': 68246, 'other': 59957 },
  '时效灯': { 'green': 443279, 'yellow': 80414, 'grey': 46028, 'warn': 19765,
    'none': 17546, '（未点亮）': 2 },
  '区县市': { '海曙区': 49995, '鄞州区': 49991, '江北区': 40097, '北仑区': 35198,
    '奉化区': 29884, '慈溪市': 24611, '镇海区': 23887, '余姚市': 18958,
    '象山县': 9705, '宁海县': 8477 },
};
const HINT = '口径：站方给的分类（只统计已采到详情的帖）。';

function render(obj) {
  const el = { innerHTML: '' };
  api.barChart(el, obj);
  return el.innerHTML;
}

/* ---------- ① 版式：数字在上、条形在下 ---------- */
console.log('=== ① 版式（名称+数字在上，条形图在数字下方）===');
const html = render(DATA['帖子类别分布']);
check(html.indexOf('class="bar-row2"') >= 0, '用的是新的 bar-row2 结构');
check(html.indexOf('class="bar-track"') < 0, '旧的 bar-track 结构已经不用了');
const rows = html.split('<div class="bar-row2">').slice(1);
check(rows.length === Object.keys(DATA['帖子类别分布']).length,
  '条目数对得上（' + rows.length + '）');
let orderOk = true, widthOk = true, valOk = true;
const cat = DATA['帖子类别分布'];
const max = Math.max.apply(null, Object.values(cat));
// ★ 必须按 **barChart 里的排序**（值降序）逐条对，否则宽度会错配
const sorted = Object.entries(cat).sort((a, b) => b[1] - a[1]);
sorted.forEach(function (pair, idx) {
  const name = pair[0], v = pair[1];
  const row = rows[idx] || '';
  if (row.indexOf('class="hd"') > row.indexOf('class="bar-svg"')) orderOk = false;
  if (row.indexOf('width="' + (v / max * 100).toFixed(2) + '"') < 0) widthOk = false;
  if (row.indexOf(api.num(v)) < 0) valOk = false;
});
check(orderOk, '每一条里 **数字在上、SVG 条形在下**');
check(widthOk, '条形宽度 = 该条 / 本组最大值（viewBox 0-100 归一）');
check(valOk, '数字用的是 num() 格式（千分位）');

/* ---------- ② SVG 本身 ---------- */
console.log('\n=== ② SVG 条形 ===');
check(html.indexOf('<svg class="bar-svg"') >= 0, '条形是 <svg>（零依赖、离线可跑）');
check(html.indexOf('preserveAspectRatio="none"') >= 0, '非等比缩放（宽度自适应）');
check(html.indexOf('<rect') >= 0, '条形用 <rect> 画');
check(html.indexOf('rx=') < 0, '不用 rect 的 rx（会被拉成椭圆；圆角交给 CSS）');
check(html.indexOf('style="fill:') >= 0, '填色走 style（支持 CSS 变量）');
const css = fs.readFileSync(path.join(WEB, 'style.css'), 'utf-8');
check(/\.bar-svg\s*\{[^}]*border-radius/.test(css), 'CSS 给 svg 加了圆角');
check(/\.bar-svg\s*\{[^}]*overflow:\s*hidden/.test(css), 'CSS 裁掉溢出（圆角才生效）');
check(css.indexOf('.bar-row2 .hd') >= 0, '有 bar-row2 的表头行样式');
check(css.indexOf('.bar-track') < 0 && css.indexOf('.bar-fill') < 0,
  '旧的 CSS 死代码删干净了');

/* ---------- ③ 内容安全 ---------- */
console.log('\n=== ③ 内容安全 ===');
const bad = render({ '<img onerror=x>': 1, '正常': 2 });
check(bad.indexOf('<img') < 0, '名称里的标签被转义（XSS 防护还在）');
check(bad.indexOf('&lt;img') >= 0, '转义后仍可读');

/* ---------- ④ 生成预览页 ---------- */
console.log('\n=== ④ 生成预览页 ===');
const cards = Object.entries(DATA).map(([title, obj]) =>
  '<div class="card"><h3>' + title + '</h3><p class="hint">' + HINT + '</p>'
  + '<div class="bars">' + render(obj) + '</div></div>').join('\n');
const page = '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
  + '<title>分布卡版式预览</title>'
  + '<link rel="stylesheet" href="/static/style.css?v=preview">'
  + '</head><body style="background:var(--bg);padding:24px">'
  + '<div class="grid2">' + cards + '</div></body></html>';
const out = path.join(WEB, '_preview_bars.html');
fs.writeFileSync(out, page, 'utf-8');
check(fs.existsSync(out), '预览页已生成：程序/msyt/web/_preview_bars.html');

console.log('\n' + '='.repeat(62));
if (FAILED.length) {
  console.log('失败 ' + FAILED.length + ' 项：');
  FAILED.forEach(f => console.log('  - ' + f));
  process.exit(1);
}
console.log('全部通过');
