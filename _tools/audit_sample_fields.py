# -*- coding: utf-8 -*-
"""一次性取证：按课件「每条正式样本应有的 6 个字段」审计我们库里到底有没有。"""
import json
import os
import sqlite3

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "程序", "data", "msyt.db")
c = sqlite3.connect(DB)
cur = c.cursor()


def q1(sql, args=()):
    return cur.execute(sql, args).fetchone()[0]


out = {}

# ---------- 规模 ----------
out["threads_total"] = q1("select count(*) from threads")
out["threads_done"] = q1("select count(*) from threads where detail_state='done'")
out["threads_restricted"] = q1("select count(*) from threads where detail_state='restricted'")
out["posts_total"] = q1("select count(*) from posts")
out["posts_real"] = q1("select count(*) from posts where pid>0")

# ---------- 字段① 发布/显示时间 与 发现时间 ----------
out["posts_post_time_nonempty"] = q1("select count(*) from posts where pid>0 and post_time<>''")
out["posts_post_time_empty"] = q1("select count(*) from posts where pid>0 and (post_time is null or post_time='')")
out["posts_fetched_at_nonempty"] = q1("select count(*) from posts where pid>0 and fetched_at<>''")
out["threads_pub_time_nonempty"] = q1("select count(*) from threads where pub_time<>'' and pub_time is not null")
out["threads_first_seen_nonempty"] = q1("select count(*) from threads where first_seen<>'' and first_seen is not null")
out["threads_last_reply_nonempty"] = q1("select count(*) from threads where last_reply_time<>'' and last_reply_time is not null")

# ---------- 字段② 来源平台 / 版块 / 观察单位 ----------
out["src_dist"] = [list(r) for r in cur.execute(
    "select source,count(*) from thread_sources group by source order by 2 desc limit 12")]
out["src_total"] = q1("select count(*) from thread_sources")
out["threads_dept_nonempty"] = q1("select count(*) from threads where dept_name<>'' and dept_name is not null")
out["threads_category_nonempty"] = q1("select count(*) from threads where category<>'' and category is not null")
out["threads_state_nonempty"] = q1("select count(*) from threads where state<>'' and state is not null")
out["threads_mailbox_nonempty"] = q1("select count(*) from threads where mailbox_name<>'' and mailbox_name is not null")
out["threads_url_nonempty"] = q1("select count(*) from threads where url<>'' and url is not null")

# ---------- 字段③ 查询词/规则版本/候选池路径 ----------
out["fetch_log_kinds"] = [list(r) for r in cur.execute(
    "select kind,count(*) from fetch_log group by kind order by 2 desc limit 12")]
out["list_changes_kinds"] = [list(r) for r in cur.execute(
    "select kind,count(*) from list_changes group by kind order by 2 desc limit 12")]
out["list_forms_kinds"] = [list(r) for r in cur.execute(
    "select kind,count(*) from thread_list_forms group by kind order by 2 desc limit 12")]

# ---------- 字段④ 原文摘要/链接/互动 ----------
out["posts_content_nonempty"] = q1("select count(*) from posts where pid>0 and chars>0")
out["posts_images_rows"] = q1("select count(*) from posts where pid>0 and images<>'[]' and images is not null")
out["threads_replies_nonnull"] = q1("select count(*) from threads where replies is not null")
out["threads_views_nonnull"] = q1("select count(*) from threads where views is not null")
# 摘要字段是否存在
out["summary_col_exists"] = q1(
    "select count(*) from pragma_table_info('threads') where name like '%summary%'")
out["abstract_col_exists"] = q1(
    "select count(*) from pragma_table_info('threads') where name like '%abstract%'")
out["ai_col_exists"] = q1(
    "select count(*) from pragma_table_info('threads') where name like '%ai%' or name like '%review%' or name like '%verify%'")

# ---------- 字段⑤ 纳入/排除/待核验理由 ----------
out["threads_warn_nonempty"] = q1("select count(*) from threads where detail_warn<>'' and detail_warn is not null")
out["integrity_items"] = q1("select count(*) from integrity_items")
out["assist_conflicts"] = q1("select count(*) from assist_conflicts")
out["assist_conflicts_unresolved"] = q1("select count(*) from assist_conflicts where resolved=0")

# ---------- 关键词/词表：有没有 ----------
for kw in ("keyword", "keywords", "term", "terms", "vocab", "wordlist", "dict"):
    out["col_%s" % kw] = [
        [t[0], t[1]] for t in cur.execute(
            "select name,type from sqlite_master where type='table' and name not like 'sqlite_%'")
        if any(kw in c2[1].lower() for c2 in cur.execute(
            "pragma table_info('%s')" % t[0]))]
out["tables_with_keyword"] = [t[0] for t in cur.execute(
    "select name from sqlite_master where type='table' and name not like 'sqlite_%'")
    if any("keyword" in c2[1].lower() for c2 in cur.execute("pragma table_info('%s')" % t[0]))]

# ---------- 去重/互动 ----------
out["posts_dup_pid"] = q1("select count(*) from (select pid from posts group by pid having count(*)>1)")
out["threads_missing_head"] = q1(
    "select count(*) from threads where detail_state='done' and first_post_id is null")
out["threads_with_chars"] = q1("select count(*) from threads where detail_state='done' and chars>0")

print(json.dumps(out, ensure_ascii=False, indent=1))
