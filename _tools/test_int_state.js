/* test_int_state.js —— 体检页「勾选态」状态模块的单测（Node，无 DOM）
   ===============================================================================
   为什么单独有这一个文件：
     体检结果分页之后，「勾选态」不能再住在 DOM 里（翻页重建表格会把没渲染的
     那些勾选丢掉，「全选」会退化成「全选当前页」）。
     勾选态现在住在一个与 DOM 无关的 Set 里（int_state.js）→ 纯逻辑、可直接单测。

   两条要验的东西：
     ① 正向：全选语义 = 该类**全部**；跨页勾选保持；取消逻辑
     ② 反向：把实现改坏（模拟旧 bug：只选前 50 条），
        同一批断言**必须报红** —— 否则这条闸是空的

   用法：node _tools/test_int_state.js
*/
'use strict';

const fs = require('fs');
const path = require('path');
const os = require('os');

const SRC = path.join(__dirname, '..', '程序', 'msyt', 'web', 'int_state.js');
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), 'intstate-'));

let passed = 0;
let failed = 0;

function check(name, ok, extra) {
  if (ok) { passed++; console.log('  [PASS] ' + name); }
  else { failed++; console.log('  [FAIL] ' + name + (extra ? '  —— ' + extra : '')); }
}

/* 加载一个模块副本（并把全局的 IntState 换掉） */
function loadFrom(srcText, tag) {
  const p = path.join(TMP, 'int_state_' + tag + '.js');
  fs.writeFileSync(p, srcText, 'utf8');
  delete globalThis.IntState;
  require(p);
  return globalThis.IntState;
}

/* 同一批断言：既能验真实实现，也能验「坏实现」 */
function suite(S) {
  const out = [];
  const add = (name, ok, extra) => out.push({ name: name, ok: ok, extra: extra || '' });

  // ① 全选语义：选的是**该类全部**（1200 条），不是当前页那 50 条
  const st = S.createState();
  const all = [];
  for (let i = 1; i <= 1200; i++) all.push(i);
  const r1 = S.toggleAll(st, 'problems', all);
  add('全选本类 → 选中该类全部 1200 条',
      st.sel.size === 1200, '实际 ' + st.sel.size);
  add('全选返回的条数 = 该类总数', r1.n === 1200, '实际 ' + r1.n);
  add('全选后 isSelected 为真', S.isSelected(st, 7) === true, '');
  add('未选中的不在集合里', S.isSelected(st, 99999) === false, '');

  // ② 跨页勾选保持：第 1 页勾 3 条 → 翻到第 2 页勾 2 条 → 回第 1 页仍是 5 条
  const st2 = S.createState();
  let page = 1;
  const pageItems = p => (p === 1 ? [101, 102, 103] : [201, 202, 203, 204, 205]);
  S.setSelected(st2, pageItems(page).slice(0, 3), true);   // 第 1 页勾 3 条
  const afterP1 = st2.sel.size;
  page = 2;                                                 // ★ 翻页
  S.setSelected(st2, pageItems(page).slice(0, 2), true);   // 第 2 页勾 2 条
  add('翻页后勾选没有被清掉（第 1 页的 3 条还在）',
      S.isSelected(st2, 101) && S.isSelected(st2, 102) && S.isSelected(st2, 103),
      '');
  add('第 2 页勾的 2 条也算上（共 5 条）', st2.sel.size === 5, '实际 ' + st2.sel.size);
  add('第 1 页勾完时是 3 条', afterP1 === 3, '实际 ' + afterP1);
  page = 1;                                                 // ★ 翻回第 1 页
  add('★ 翻回第 1 页，原来的 3 条仍是勾选态',
      S.isSelected(st2, 101) && S.isSelected(st2, 103), '');
  add('翻回后总数仍是 5 条', st2.sel.size === 5, '实际 ' + st2.sel.size);

  // ③ 取消：再点一次「全选」应变成取消
  const r2 = S.toggleAll(st2, 'problems', [101, 102, 103, 201, 202]);
  add('再点一次「全选」= 取消（全选是切换语义）', st2.sel.size === 0,
      '实际 ' + st2.sel.size);
  add('取消时返回 on=false', r2.on === false, '');

  // ④ 站方限制不可勾选
  add('★ restricted 不在可勾选的三类里',
      S.SELECTABLE.indexOf('restricted') === -1
      && S.SELECTABLE.length === 3, JSON.stringify(S.SELECTABLE));
  // ★ 2026-10-29 下线第五类 count_mismatch（站方计数缺陷）→
  //   那批帖的正文**本来就采全了**（实测 25 条逐层对比），正确处置是
  //   **归为 done（正文齐全即已采）**，不再单列一栏。
  add('★ 已下线的 count_mismatch 不在类别里',
      S.KINDS.indexOf('count_mismatch') === -1, JSON.stringify(S.KINDS));
  add('四个类别齐全',
      S.KINDS.length === 4
      && S.KINDS.indexOf('restricted') >= 0
      && S.KINDS.indexOf('problems') >= 0
      && S.KINDS.indexOf('growth') >= 0
      && S.KINDS.indexOf('warnings') >= 0,
      JSON.stringify(S.KINDS));

  // ⑤ 导出与清空
  const st3 = S.createState();
  S.setSelected(st3, [1, 2, 3], true);
  const ids = S.selectedTids(st3);
  add('selectedTids 导出 3 个 tid',
      ids.length === 3 && ids.indexOf(2) >= 0, JSON.stringify(ids));
  S.clear(st3);
  add('clear 之后清空', st3.sel.size === 0, '实际 ' + st3.sel.size);

  // ⑥ 单条取消
  S.setSelected(st3, [1, 2, 3], true);
  S.setSelected(st3, [2], false);
  add('单条取消只删那一条', st3.sel.size === 2, '实际 ' + st3.sel.size);

  return out;
}

const realSrc = fs.readFileSync(SRC, 'utf8');

console.log('=== ① 正向：真实实现必须全过 ===');
const Real = loadFrom(realSrc, 'real');
let allOk = true;
for (const a of suite(Real)) {
  check(a.name, a.ok, a.extra);
  if (!a.ok) allOk = false;
}

console.log('\n=== ② 反向：把实现改坏，同一批断言必须报红 ===');
/* 坏实现 = 旧 bug 的翻版：勾选只认「当前页」，也就是最多 50 条 */
const broken = realSrc.replace(
  'if (on) st.sel.add(tid); else st.sel.delete(tid);',
  'if (on) { if (st.sel.size < 50) st.sel.add(tid); } else st.sel.delete(tid);');
if (broken === realSrc) {
  check('反向：能定位到要改坏的那一行', false, '源码里没找到目标行');
} else {
  const Bad = loadFrom(broken, 'broken');
  const res = suite(Bad);
  const bad = res.filter(a => !a.ok);
  check('★ 改坏后断言确实报红（闸不是空的）', bad.length > 0,
        '一条都没报红 → 闸是空的');
  check('★ 报红的正是「全选语义」那条',
        bad.some(a => a.name.indexOf('1200') >= 0),
        JSON.stringify(bad.map(a => a.name).slice(0, 3)));
  console.log('     改坏后报红的断言：');
  bad.forEach(a => console.log('       - ' + a.name + (a.extra ? '  (' + a.extra + ')' : '')));
}

console.log('\n共 ' + (passed + failed) + ' 项 · 通过 ' + passed + ' · 失败 ' + failed);
process.exit(failed ? 1 : 0);
