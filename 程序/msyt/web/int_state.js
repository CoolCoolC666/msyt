/* 体检页的**勾选态与分页状态**（纯逻辑，不碰 DOM）
   ============================================================
   为什么单独拆一个文件（2026-10-28）
   ------------------------------------------------------
   体检结果分页之后，「勾选态」不能再住在 DOM 里：
   翻页会重建表格 → 未渲染的那几十行**勾选就丢了**，
   「全选」会退化成「全选当前页 50 条」——语义直接塌掉。

   现在勾选态住在一个与 DOM 无关的 Set 里：
     · 翻页只换表格内容，Set 不动 → 跨页勾选天然保持
     · 「全选本类」从 /api/integrity/tids 拿**该类全部** tid → 语义正确
     · 纯函数、无 DOM → 可以在 Node 里直接单测（_tools/test_integrity_paging.py）

   ★ 口径：站方限制（restricted）**不可勾选** —— 重跑多少次都是同一个提示页，
     勾了只会让成员白跑（见 app.js 里那处注释）。 */
(function (root) {
  'use strict';

  /** 体检明细的四个类别（与后端 DB.INTEGRITY_KINDS 一致）
   *  ★ 2026-10-29 下线第五类 count_mismatch：
   *    它的唯一语义是「站方列表计数虚高导致的假少贴」，
   *    而那批帖的正文**本来就采全了、站方也照样公开**
   *    → 正确处置是**归为 done（正文齐全即已采）**，不再单列一栏。 */
  var KINDS = ['problems', 'growth', 'warnings', 'restricted'];
  /** 其中**可勾选**的三类（restricted 不可勾选） */
  var SELECTABLE = ['problems', 'growth', 'warnings'];

  function createState() {
    return {
      run: null,        // {run_id, checked, ok, counts}
      sel: new Set(),   // 选中的 tid（跨页保持）
      all: {},          // kind -> 该类**全部** tid（懒加载，来源 /api/integrity/tids）
      page: {},         // kind -> {page, pages, total}
      size: {},         // kind -> 每页条数
      loaded: {},       // kind -> bool（这一类是否已经拉过第一页）
    };
  }

  /** 某类的总条数（从摘要里读，没有就是 0） */
  function count(st, kind) {
    if (!st || !st.run || !st.run.counts) return 0;
    return st.run.counts[kind] || 0;
  }

  function isSelected(st, tid) {
    return !!st.sel.has(Number(tid));
  }

  /** 勾上/取消一批 tid */
  function setSelected(st, tids, on) {
    (tids || []).forEach(function (t) {
      var tid = Number(t);
      if (!tid) return;
      if (on) st.sel.add(tid); else st.sel.delete(tid);
    });
    return st.sel.size;
  }

  /** 全选/取消**某一类**（切换语义）。
   *  @param tids 该类的全部 tid；不给就用缓存的 st.all[kind]
   *  @return {on: 是否变成了全选, n: 该类条数}
   */
  function toggleAll(st, kind, tids) {
    var all = tids || st.all[kind] || [];
    var allOn = all.length > 0 && all.every(function (t) { return st.sel.has(Number(t)); });
    setSelected(st, all, !allOn);
    return { on: !allOn, n: all.length };
  }

  /** 取消全部选择 */
  function clear(st) {
    st.sel.clear();
    return 0;
  }

  /** 当前选中的 tid 数组（发给后端的就是它） */
  function selectedTids(st) {
    return Array.from(st.sel);
  }

  root.IntState = {
    KINDS: KINDS,
    SELECTABLE: SELECTABLE,
    createState: createState,
    count: count,
    isSelected: isSelected,
    setSelected: setSelected,
    toggleAll: toggleAll,
    clear: clear,
    selectedTids: selectedTids,
  };
})(typeof globalThis !== 'undefined' ? globalThis : this);
