# `_tools/` —— 验收脚本与工具

这个目录里是**开发期用过的脚本**。它们分三类，用途完全不同，
但**都请保留**（尤其 `test_*.py`）—— 有人曾把三个验收脚本当临时文件清掉，
结果下次回归直接报「文件不存在」，被误判成「测试失败」。

---

## 一、`test_*.py` —— 回归验收资产（**最重要，别删**）

每个都能独立运行，是改完代码后的回归防线。

| 脚本 | 守的是什么 |
|---|---|
| `test_increment.py` | 两级增量判据、回复数增长识别、重跑 / 清除 |
| `test_beta_permissions.py` | 权限矩阵：界面层 + 接口层两道、成员 403 |
| `test_gap_retry.py` / `test_gap_retry_e2e.py` | 「少贴」判据：尾部少 → 补采；中间缺 → 只记软警告 |
| `test_restricted.py` | 站方限制帖：标记 + 写入正文 + 不计入已采详情帖 |
| `test_task_detail.py` / `test_task_cancel.py` | 协作任务详情 / 移除（已完成的不废弃） |
| `test_pool.py` | 协作池：先新后旧、退活、回收 |
| `test_write_lock.py` | 并发写库：共享写锁 + 读事务升级为写 |
| `test_submit_retry.py` / `test_agent_linkloss.py` | 回传重试、失联容忍 |
| `test_pack_credential.py` / `test_bundle_collab.py` | 启动包凭证链、协作包内容 |
| `test_auto_update.py` | 助手自更新（自举） |
| `test_member_e2e.py` / `test_public_e2e.py` / `test_local_collect.py` | 成员端 / 公网 / 本机采集端到端 |
| `test_cmd_encoding.py` | **`.cmd` 纯 ASCII + CRLF**（中文入 cmd 会被 cmd.exe 咬坏） |
| `test_link_tolerance.py` | 链路容错 |
| `test_list_changes.py` / `test_conflicts_paging.py` / `test_drift.py` | 列表变更留痕、冲突分页、漂移检测 |
| `test_no_agent_prompt.py` / `test_agent_first_prompt.py` | agent 首轮提示 |
| `test_password_echo.py` / `test_member_side_net.py` | 密码不回显、成员侧网络 |

跑法：

```powershell
$env:PYTHONIOENCODING='utf-8'
python _tools\test_increment.py
```

---

## 二、通用工具

| 脚本 | 作用 |
|---|---|
| `md2docx.py` | Markdown → Word（自包含，只依赖 python-docx） |
| `rebuild_report_docx.py` | 重建两份报告的 docx（**改完 md 必须跑**；WPS 占用时加 `wait 120`） |
| `verify_report_docx.py` | 交付前**回读验收** docx（不看源码，只看产物） |
| `clean_test_residue.py` | 清测试残留数据（tid ≥ 9 号段；**默认 dry-run**，加 `--apply` 才删） |
| `clean_queue_orphans.py` | 清队列孤儿活（threads 已终态、队列还 pending）；**默认 dry-run**，`--apply` 才写库 |
| `recycle_bin.py` | 把临时文件送**系统回收站**（本机删除纪律：只走可恢复通道） |
| `sync_member_bundle.py` | 走**同一个** `/api/agent/bundle.zip` 端点同步成员端包 |
| `set_member_pw.py` / `reset_member_pw.py` / `add_member.py` | 成员账号管理 |
| `_qa_accounts.py` | 验收用测试账号的建 / 删 |

---

## 三、一次性诊断脚本（`probe_*` / `check_*` / `diag_*` / `dump_*` / `fix_*`）

排查具体问题时写的一次性脚本，**留着是为了可追溯**，不是常驻依赖。
每个脚本头部都写明当时在查什么问题。

---

## 四、发布流程工具（`_prepare_release.py` / `_verify_release2.py` / `_scan_secrets.py`）

把项目**脱敏复制**到独立的发布目录，并做独立复查。

⚠ 这些脚本**本身不进入发布包** —— 它们写着真实标识的字面量当检查清单用，
留在仓库里自己就成了泄露源。

> 教训：「替换过」≠「没有了」。第一轮复查脚本有两个致命漏洞：
> ① `.docx` 是 zip，直接当文本读**读不到正文**（两份报告的域名与成员邮箱全在里面）；
> ② allowlist 判断粒度错了 —— 同一文件里出现任一允许项就**整份文件跳过不检查**。
> 两个都修掉之后才算真的干净。
