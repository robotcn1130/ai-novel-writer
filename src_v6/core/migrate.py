# -*- coding: utf-8 -*-
"""
Schema v6.7 建库 / 迁移脚本

用法：
    python src_v6/core/migrate.py                 # 自动建库或增量升级
    python src_v6/core/migrate.py --check         # 仅体检
    python src_v6/core/migrate.py --db <path>     # 指定数据库

设计原则：
    1. schema_v6.sql 是唯一事实来源，本脚本只负责"把它应用到库上"。
    2. 幂等：反复执行结果一致，不产生重复数据。
    3. 破坏性操作前自动备份到 backup/。
    4. 不做 v4 -> v6 的数据迁移（用户已明确不需要兼容旧数据）。

v6.7 迁移流水线（机制不改，只加内容）：
    _backup()                    已有：sqlite3.backup() 到 backup/
      ↓
    _apply_schema()              9 张新表随 IF NOT EXISTS 建出
      ↓
    _add_columns()               加 38 个新列 + 重放 _LATE_INDEXES
      ↓
    _seed_defaults()             加 conflict_check 档位
      ↓
    _backfill_v67()              ★ 新增：把老数据接上新结构
      ↓
    _record_version(7)

**迁移时宁可不生成，也不能生成错的**（第 6 条）：老库的 events.structured 可能是
空/半截（v6.x 前期的截断抢救产物），自动投影成 facts 会造出**错误的正史**——
比没有正史更糟（第 20 章的推演会基于假事实）。所以只提供人工触发的重建入口。
"""
import argparse
import datetime
import json
import os
import shutil
import sqlite3
import sys

SCHEMA_VERSION = 7

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC_V6 = os.path.dirname(_HERE)                 # src_v6/
ROOT_DIR = os.path.dirname(_SRC_V6)              # 项目根
SCHEMA_FILE = os.path.join(_HERE, "schema_v6.sql")
DEFAULT_DB = os.path.join(ROOT_DIR, "sql", "novel.db")
BACKUP_DIR = os.path.join(ROOT_DIR, "backup")


# ---------------------------------------------------------------- 工具

def _split_statements(sql_text):
    """按分号切分 SQL 语句（字符级扫描，正确处理字符串字面量与触发器体）。

    规则：
      - 单引号字符串内的分号不分隔
      - CREATE TRIGGER ... BEGIN ... END; 整体视为一条语句
      - 双横线注释整行跳过
    """
    statements = []
    buf = []
    in_squote = False
    in_trigger = False
    pending = False          # 当前语句是否已判定为触发器
    i = 0
    n = len(sql_text)

    while i < n:
        ch = sql_text[i]

        # 行注释：不在字符串里且遇到 -- 就跳到行尾
        if not in_squote and ch == "-" and i + 1 < n and sql_text[i + 1] == "-":
            j = sql_text.find("\n", i)
            if j == -1:
                break
            i = j
            continue

        buf.append(ch)

        if ch == "'":
            # 处理 '' 转义
            if in_squote and i + 1 < n and sql_text[i + 1] == "'":
                buf.append(sql_text[i + 1])
                i += 2
                continue
            in_squote = not in_squote
            i += 1
            continue

        if not in_squote:
            # 判断触发器/虚拟表起始
            if not pending and ch == "\n":
                head = "".join(buf).strip().upper()
                if head.startswith("CREATE TRIGGER") and "BEGIN" in head:
                    in_trigger = True
                    pending = True
            if ch == ";" and not in_trigger:
                statements.append("".join(buf).strip())
                buf = []
                pending = False
                i += 1
                continue
            if in_trigger:
                tail = "".join(buf).rstrip().upper()
                if tail.endswith("END;") or tail == "END;":
                    statements.append("".join(buf).strip())
                    buf = []
                    in_trigger = False
                    pending = False
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return [s for s in statements if s and s != ";"]


def _read_schema():
    with open(SCHEMA_FILE, "r", encoding="utf-8") as f:
        return f.read()


def _backup(db_path):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    name = os.path.basename(db_path).replace(".db", "")
    dst = os.path.join(BACKUP_DIR, "%s_v6pre_%s.db.bak" % (name, stamp))
    shutil.copy2(db_path, dst)
    return dst


def _connect(db_path):
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    return con


# ---------------------------------------------------------------- 迁移步骤

def _current_version(con):
    try:
        row = con.execute(
            "SELECT MAX(version) FROM schema_migrations"
        ).fetchone()
        return row[0] or 0
    except sqlite3.OperationalError:
        return 0


def _apply_schema(con, verbose=True):
    """应用整个 schema。全部使用 IF NOT EXISTS，天然幂等。"""
    text = _read_schema()
    stmts = _split_statements(text)
    ok = 0
    deferred = 0
    for stmt in stmts:
        try:
            con.execute(stmt)
            ok += 1
        except sqlite3.OperationalError as exc:
            msg = str(exc)
            # 视图/触发器已存在时忽略
            if "already exists" in msg:
                continue
            # 增量升级的排序难题：schema 文件按"最终形态"写，于是
            # CREATE INDEX ... ON t(new_col) 会排在 _add_columns 补列之前执行，
            # 老库此时还没有 new_col -> "no such column"。
            # 这类语句不是错的，只是来早了：跳过，等补完列再由
            # _add_columns 末尾统一重放（见 _LATE_INDEXES）。
            if "no such column" in msg and stmt.upper().lstrip().startswith(
                    "CREATE INDEX"):
                deferred += 1
                continue
            raise RuntimeError("执行失败：%s\n--- SQL ---\n%s" % (msg, stmt[:400]))
    con.commit()
    if verbose:
        print("  [schema] 应用 %d 条语句%s" % (
            ok, "（%d 条索引等补列后重放）" % deferred if deferred else ""))
    return ok


# 依赖"后来才补上的列"的索引。_apply_schema 跑的时候老库还没这些列，
# 必须等 _add_columns 补完再建，否则老库升级直接崩在建索引上。
_LATE_INDEXES = [
    # (索引名, 建索引 SQL)
    ("idx_world_entities_parent",
     "CREATE INDEX IF NOT EXISTS idx_world_entities_parent "
     "ON world_entities(novel_id, parent_id)"),

    # ---- v6.7 新增（都依赖本版新补的列）----
    ("idx_events_tick",
     "CREATE INDEX IF NOT EXISTS idx_events_tick "
     "ON events(novel_id, tick)"),
    ("idx_events_branch",
     "CREATE INDEX IF NOT EXISTS idx_events_branch "
     "ON events(novel_id, branch_id)"),
    ("idx_story_nodes_branch",
     "CREATE INDEX IF NOT EXISTS idx_story_nodes_branch "
     "ON story_nodes(novel_id, branch_id, status)"),
    ("idx_timeline_tick",
     "CREATE INDEX IF NOT EXISTS idx_timeline_tick "
     "ON timeline(novel_id, status, absolute_tick)"),
    # 幂等重放：schema_v6.sql 里也有（新库一步到位），这里保证"老库补完列之后"也建上
    ("uq_facts_active",
     "CREATE UNIQUE INDEX IF NOT EXISTS uq_facts_active "
     "ON facts(novel_id, subject_type, subject_id, predicate) "
     "WHERE status='active' AND subject_id IS NOT NULL"),
]


def _add_columns(con, verbose=True):
    """给已存在的表补列（IF NOT EXISTS 不会改已存在的表）。

    这是"增量升级"的核心：新版本加了列，老库需要 ALTER 补上。
    每条都是幂等的——已有该列就跳过。

    注意：这里的列声明**不带 CHECK**（ALTER TABLE ADD COLUMN 能加 CHECK，
    但新库走的是 schema_v6.sql 的 CREATE TABLE、老库走这里，两边会不一致）。
    取舍：v6 的铁律是"模型自由文本不许直连带 CHECK 的列"，
    枚举一律在 database.py 收口，所以老库少一个 CHECK 不构成风险；
    反过来若只在老库加 CHECK，才会出现"新库能写、老库不能写"的分裂。
    """
    wanted = [
        # (表, 列, 类型与默认值)
        # 注意：ALTER TABLE ADD COLUMN 的默认值必须是常量，
        # 所以这里不能用 datetime('now')，带了会报
        # "Cannot add a column with non-constant default"。
        ("providers", "preset_key", "TEXT NOT NULL DEFAULT ''"),
        ("providers", "models", "TEXT NOT NULL DEFAULT '[]'"),
        ("providers", "updated_at", "TEXT NOT NULL DEFAULT ''"),
        # v6.4 地点树：世界实体可以挂在父地点下（旧城区 → 本部 → 地下二层 → 封锁库）
        ("world_entities", "parent_id", "INTEGER"),
        ("world_entities", "location_level", "TEXT NOT NULL DEFAULT ''"),
        ("world_entities", "is_secret", "INTEGER NOT NULL DEFAULT 0"),
        # v6.4 角色位置的**结构化**表达（current_location 那串文本保留给展示用）
        ("characters", "location_id", "INTEGER"),
        ("characters", "location_precision", "TEXT NOT NULL DEFAULT 'unknown'"),
        ("characters", "location_updated_at", "TEXT NOT NULL DEFAULT ''"),

        # ---- v6.7 双层时间 ----
        ("world_clock", "absolute_tick", "INTEGER NOT NULL DEFAULT 0"),
        ("world_clock", "tick_unit", "TEXT NOT NULL DEFAULT 'scene'"),
        ("world_clock", "tick_unit_note", "TEXT NOT NULL DEFAULT ''"),
        # ---- v6.7 正史事件 ----
        ("events", "canonical", "INTEGER NOT NULL DEFAULT 1"),
        ("events", "immutable", "INTEGER NOT NULL DEFAULT 1"),
        ("events", "branch_id", "INTEGER NOT NULL DEFAULT 0"),
        ("events", "tick", "INTEGER NOT NULL DEFAULT 0"),
        ("events", "is_background", "INTEGER NOT NULL DEFAULT 0"),
        # ---- v6.7 沙盘分支 ----
        ("story_nodes", "branch_id", "INTEGER NOT NULL DEFAULT 0"),
        ("story_nodes", "state_delta", "TEXT NOT NULL DEFAULT '{}'"),
        ("story_nodes", "fact_delta", "TEXT NOT NULL DEFAULT '[]'"),
        ("story_nodes", "knowledge_delta", "TEXT NOT NULL DEFAULT '[]'"),
        ("story_nodes", "delta_applied", "INTEGER NOT NULL DEFAULT 0"),
        ("story_nodes", "tick", "INTEGER NOT NULL DEFAULT 0"),
        # ---- v6.7 状态投影 ----
        ("world_state", "derived_from_fact_id", "INTEGER"),
        ("world_state", "projected_at_tick", "INTEGER NOT NULL DEFAULT 0"),
        ("world_state_log", "event_id", "INTEGER"),
        ("world_state_log", "fact_id", "INTEGER"),
        ("world_state_log", "tick", "INTEGER NOT NULL DEFAULT 0"),
        # ---- v6.7 时间线锚点 tick 化 ----
        ("timeline", "absolute_tick", "INTEGER NOT NULL DEFAULT 0"),
        ("timeline", "tick_span", "INTEGER NOT NULL DEFAULT 0"),
        # ---- v6.7 章节 ----
        ("chapters", "tick_start", "INTEGER NOT NULL DEFAULT 0"),
        ("chapters", "tick_end", "INTEGER NOT NULL DEFAULT 0"),
        ("chapters", "fact_conflict_count", "INTEGER NOT NULL DEFAULT 0"),
        ("chapters", "verified", "INTEGER NOT NULL DEFAULT 0"),
        # ---- v6.7 目标生命周期 ----
        ("character_goals", "lifecycle", "TEXT NOT NULL DEFAULT 'active'"),
        ("character_goals", "success_condition", "TEXT NOT NULL DEFAULT ''"),
        ("character_goals", "fail_condition", "TEXT NOT NULL DEFAULT ''"),
        ("character_goals", "last_reviewed_chapter", "INTEGER NOT NULL DEFAULT 0"),
        # ---- v6.7 位置认知并入知识层 ----
        ("location_beliefs", "know_type", "TEXT NOT NULL DEFAULT 'believe'"),
        ("location_beliefs", "confidence", "INTEGER NOT NULL DEFAULT 3"),
        ("location_beliefs", "source", "TEXT NOT NULL DEFAULT 'witnessed'"),
        ("location_beliefs", "knowledge_id", "INTEGER"),
        # ---- v6.7 零星 ----
        ("location_history", "tick", "INTEGER NOT NULL DEFAULT 0"),
        ("world_settings", "rule_level", "TEXT NOT NULL DEFAULT ''"),
        ("decision_points", "branch_id", "INTEGER NOT NULL DEFAULT 0"),
        ("decision_points", "node_id", "INTEGER"),
        ("characters", "knowledge_tick", "INTEGER NOT NULL DEFAULT 0"),
    ]
    added = 0
    for table, col, decl in wanted:
        try:
            cols = [r[1] for r in con.execute("PRAGMA table_info(%s)" % table)]
        except sqlite3.OperationalError:
            continue                      # 表还不存在，交给 _apply_schema 建
        if not cols:
            continue
        if col in cols:
            continue
        con.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, decl))
        added += 1
    if added:
        # 补列后回填 updated_at 的空值
        try:
            con.execute(
                "UPDATE providers SET updated_at=datetime('now','localtime') "
                "WHERE updated_at IS NULL OR updated_at=''")
        except sqlite3.OperationalError:
            pass
        con.commit()
    # 补完列再重放"来早了"的索引（_apply_schema 阶段它们还建不了）。
    # 每条独立 try：某个索引依赖的表/列真不存在时不该拖垮整个迁移。
    late = 0
    for name, sql in _LATE_INDEXES:
        try:
            con.execute(sql)
            late += 1
        except sqlite3.OperationalError:
            continue
    if late:
        con.commit()
    if verbose and added:
        print("  [alter] 补列 %d 个" % added)
    return added


def _seed_defaults(con, verbose=True):
    """写入默认配置（幂等）。"""
    defaults = [
        ("llm.default_provider", ""),
        ("llm.stream", "1"),
        ("sim.max_events_per_tick", "6"),
        ("sim.auto_advance_limit", "20"),
        ("ui.theme", "light"),
        ("ui.page_size", "30"),
        # ---- v6.7 ----
        ("ui.fact_panel_open", "0"),        # 第一页「正史账本」抽屉默认是否展开
        ("sim.conflict_autoskip", "0"),     # 冲突是否自动忽略（0=必须人工裁决）
        # ▲ 别改成 1。改了就退回"正文说了算"，第 2 条硬约束当场失效。
        ("sim.classify_autoskip", "0"),
        ("sim.tick_unit_note", "1 场戏"),
    ]
    for k, v in defaults:
        con.execute(
            "INSERT OR IGNORE INTO app_config(key, value) VALUES (?, ?)", (k, v)
        )

    # 默认模型档位（novel_id=0 为全局默认，api_key 未配置时仅占位）
    slots = [
        ("world_sim",        "", 0.9, 4096),
        ("character_decide", "", 0.8, 2048),
        ("option_gen",       "", 1.0, 2048),
        ("prose_gen",        "", 1.0, 8192),
        ("state_extract",    "", 0.2, 4096),
        ("completion_judge", "", 0.3, 2048),
        ("summarize",        "", 0.3, 2048),
        # v6.6 章节诊断：低温（要的是稳定比对，不是发挥）
        ("diagnose",         "", 0.3, 8192),
        # v6.7 语义级冲突判定（Python 结构化比对漏掉的情形：自由文本事实、
        # 语义相反的表述）。同时被"事实分类兜底"复用，不再新增档位。
        ("conflict_check",   "", 0.2, 2048),
        # v6.8 创建世界时按选定题材生成整份世界草案（降级链指向 world_sim）。
        ("world_init",       "", 0.9, 4096),
    ]
    for slot, model, temp, mt in slots:
        con.execute(
            "INSERT OR IGNORE INTO model_presets"
            "(novel_id, slot, provider_id, model, temperature, max_tokens) "
            "VALUES (0, ?, NULL, ?, ?, ?)",
            (slot, model, temp, mt),
        )
    con.commit()
    if verbose:
        print("  [seed] 默认配置与模型档位已就绪")


def _backfill_locations(con, verbose=True):
    """把老数据里 current_location 那串自由文本补成结构化位置（v6.4）。

    老库里角色的位置只有 "调查局本部 茶水间" 这样的文本，没有 location_id。
    不补的话，升级后地图上所有人都是"行踪不明"——数据明明在，只是没接上。

    只在 location_id 为空时补（幂等）：补过一次就跳过，不会覆盖新写的结构位置。
    """
    try:
        rows = con.execute(
            "SELECT id, novel_id, current_location FROM characters "
            "WHERE (location_id IS NULL OR location_id=0) "
            "AND current_location IS NOT NULL AND current_location != '' "
            "AND deleted_at IS NULL").fetchall()
    except sqlite3.OperationalError:
        return 0                           # 老库还没有这一列，交给 _add_columns
    if not rows:
        return 0

    import re as _re
    split_re = _re.compile(r"[ \u3000·／/>＞\-—]+")
    vague = {"某处", "某地", "不明", "未知", "行踪不明", "远方", "远处",
             "他处", "elsewhere", "unknown", "somewhere"}
    level_kw = (
        ("city", ("市", "城", "都", "州", "郡", "国", "王国", "帝国")),
        ("district", ("区", "街区", "巷", "街", "坊", "镇", "乡", "郊", "港")),
        ("building", ("本部", "总部", "大楼", "大厦", "楼", "馆", "院", "所",
                      "站", "基地", "庄园", "宅", "府", "庙", "寺", "观", "塔",
                      "仓", "厂", "校", "学园", "医院", "议会", "总局", "分局")),
        ("floor", ("层", "楼", "地下", "顶层", "天台", "阁楼", "地下室")),
        ("room", ("室", "间", "房", "厅", "堂", "库", "牢", "狱", "店", "铺",
                  "台", "洗手间", "厕所", "走廊", "办公室", "会议室")),
        ("site", ("遗址", "废墟", "坟", "墓", "洞穴", "洞", "林", "场", "广场",
                  "码头", "野外", "荒原", "沙漠", "森林")),
    )

    def guess_level(name, last=False):
        s = str(name or "").strip()
        if not s or s in vague:
            return "other"
        for lv, kws in level_kw:
            for kw in kws:
                if kw in s:
                    return lv
        return "room" if last else "other"

    def ensure_path(novel_id, text):
        parts = [p.strip() for p in split_re.split(str(text or ""))
                 if p.strip() and p.strip() not in vague]
        if not parts:
            return None
        parent = None
        for i, seg in enumerate(parts):
            lv = guess_level(seg, last=(i == len(parts) - 1))
            row = con.execute(
                "SELECT id FROM world_entities WHERE novel_id=? "
                "AND entity_type='location' AND name=? AND parent_id IS ?",
                (novel_id, seg, parent)).fetchone()
            if row:
                lid = row[0]
            else:
                # parent_id 必须一起写进去。漏了它，每个新节点都会落在顶层，
                # "本部 → 茶水间" 就断成两个平级地名（真实踩过）。
                cur = con.execute(
                    "INSERT INTO world_entities(novel_id, entity_type, name, "
                    "description, parent_id, location_level, is_secret) "
                    "VALUES (?,'location',?,'',?,?,0)",
                    (novel_id, seg, parent, lv))
                lid = cur.lastrowid
            parent = lid
        return parent

    done = 0
    for cid, nid, text in rows:
        lid = ensure_path(nid, text)
        if not lid:
            continue
        node = con.execute(
            "SELECT location_level FROM world_entities WHERE id=?", (lid,)).fetchone()
        lv = (node[0] if node else "") or ""
        precision = lv if lv and lv != "other" else guess_level(text, last=True)
        if precision == "other":
            precision = "unknown"
        con.execute(
            "UPDATE characters SET location_id=?, location_precision=? WHERE id=?",
            (lid, precision, cid))
        done += 1
    con.commit()
    if verbose and done:
        print("  [backfill] 回填 %d 个角色的结构化位置" % done)
    return done


def _backfill_v67(con, verbose=True):
    """把老数据接上 v6.7 的新结构。**全部幂等**（跑第二遍什么都不做）。

    自动做的只有 5 件（都满足"结构清晰、可安全转换、错了能撤"）：
      0. world_state 老行标 derived_from_fact_id=0（＝来自自动投影）。
         **不做这一步，老世界的状态会从此再也不被推演更新**——
         新列在老行上是 NULL，而 NULL 的语义是"手工值，投影跳过"。
         详见下面对应代码块的注释。
      1. world_clock.absolute_tick ← total_ticks（语义等价，直接搬）
         + tick_unit 与老的 granularity 对齐
      2. location_beliefs → character_knowledge（about_type='location'），回填 knowledge_id
      3. chapters.source_event_ids(JSON) → chapter_event_links（无损展开）
      4. app_config 的 v6.7 默认项与 conflict_check 档位（在 _seed_defaults 里）

    **刻意不做**的两件（见模块 docstring 与设计 §1.5）：
      · timeline.absolute_tick：老锚点的时间是自由文本（"霜月十五"），
        无法可靠换算成 tick。留 0，继续走老的"模型判断"路径（兼容期）。
      · 老 events → facts：老事件的 structured 可能是空/半截，
        强行投影会造出错误的正史。改由用户在「正史账本」里手工点一次重建。
    """
    did = []

    # ---- 0. **老 world_state 行全部标成"来自自动投影"（derived_from_fact_id=0）**
    #
    # 这一步是设计 §1.3.4 的必要补充（设计原文只说了 NULL=手工行、投影跳过）。
    # 若不做这一步：迁移新增的列在老行上是 NULL，而 NULL 的语义是"手工值，投影不覆盖"
    # → 老世界的世界状态**从此再也不会被推演更新**（全部被当成手工锁定的值）。
    # 老行本来就没有保护（"世界状态页改完就丢"就是这个 bug），所以这里统一标成
    # 自动来源（0 = 自动投影，具体 fact 未知），把 NULL 留给**迁移之后**用户的手改。
    try:
        n = con.execute(
            "UPDATE world_state SET derived_from_fact_id=0 "
            "WHERE derived_from_fact_id IS NULL").rowcount
        if n:
            did.append("世界状态标注自动来源 %d 行" % n)
            con.commit()
    except sqlite3.OperationalError:
        pass

    # ---- 1. tick
    try:
        n = con.execute(
            "UPDATE world_clock SET absolute_tick=total_ticks "
            "WHERE (absolute_tick IS NULL OR absolute_tick=0) AND total_ticks>0"
        ).rowcount
        if n:
            did.append("absolute_tick 回填 %d 行" % n)
        n = con.execute(
            "UPDATE world_clock SET tick_unit=granularity "
            "WHERE granularity IN ('scene','day','stage') "
            "AND (tick_unit IS NULL OR tick_unit='' OR tick_unit<>granularity)"
        ).rowcount
        if n:
            did.append("tick_unit 对齐 %d 行" % n)
        con.commit()
    except sqlite3.OperationalError:
        pass

    # ---- 2. 位置认知 → 知识层
    try:
        rows = con.execute(
            "SELECT b.id, b.novel_id, b.character_id, b.observer_id, "
            "       b.believed_location_id, b.believed_text, b.precision, "
            "       b.known_since, b.chapter_ref, b.stale, "
            "       tgt.name AS target_name, obs.name AS observer_name "
            "  FROM location_beliefs b "
            "  LEFT JOIN characters tgt ON tgt.id = b.character_id "
            "  LEFT JOIN characters obs ON obs.id = b.observer_id "
            " WHERE b.knowledge_id IS NULL").fetchall()
        made = 0
        for (bid, nid, cid, oid, lid, btext, prec, since, chref, stale,
             tname, oname) in rows:
            # character_knowledge.character_id = **相信的那个人**（观察者），
            # 不是被观察者。这一条写反了，"谁以为谁在哪"就整个反过来
            # （展示层会画出"该蒙在鼓里的人在读别人"的反结论）。
            content = "%s 以为 %s 在「%s」" % (
                oname or "某人", tname or "某人", btext or "行踪不明")
            cur = con.execute(
                "INSERT INTO character_knowledge(novel_id, character_id, know_type, "
                "about_type, about_id, about_text, content, source, confidence, "
                "learned_tick, learned_chapter, status) "
                "VALUES (?, ?, 'believe', 'location', ?, ?, ?, 'told', 3, 0, ?, ?)",
                (nid, oid, lid, btext or "", content, int(chref or 0),
                 "refuted" if stale else "active"))
            con.execute("UPDATE location_beliefs SET knowledge_id=? WHERE id=?",
                        (cur.lastrowid, bid))
            made += 1
        if made:
            con.commit()
            did.append("位置认知迁入知识层 %d 条" % made)
    except sqlite3.OperationalError:
        pass

    # ---- 3. 章节→事件映射
    try:
        rows = con.execute(
            "SELECT novel_id, chapter_number, source_event_ids FROM chapters "
            "WHERE source_event_ids IS NOT NULL AND source_event_ids NOT IN ('', '[]')"
        ).fetchall()
        made = 0
        for nid, chno, raw in rows:
            try:
                ids = json.loads(raw) if isinstance(raw, str) else (raw or [])
            except (ValueError, TypeError):
                continue
            if not isinstance(ids, list):
                continue
            for seq, eid in enumerate(ids):
                try:
                    eid = int(eid)
                except (TypeError, ValueError):
                    continue
                cur = con.execute(
                    "INSERT OR IGNORE INTO chapter_event_links"
                    "(novel_id, chapter_number, event_id, seq, link_type) "
                    "VALUES (?,?,?,?, 'source')",
                    (nid, int(chno), eid, seq))
                made += cur.rowcount
        if made:
            con.commit()
            did.append("章节↔事件映射 %d 条" % made)
    except sqlite3.OperationalError:
        pass

    if verbose and did:
        print("  [backfill v6.7] " + "；".join(did))
    return did


def _record_version(con, note="", verbose=True):
    row = con.execute(
        "SELECT 1 FROM schema_migrations WHERE version = ?", (SCHEMA_VERSION,)
    ).fetchone()
    if not row:
        con.execute(
            "INSERT INTO schema_migrations(version, note) VALUES (?, ?)",
            (SCHEMA_VERSION, note or "v6.0 世界模拟范式初始建库"),
        )
        con.commit()
    if verbose:
        print("  [version] 记录 schema version = %d" % SCHEMA_VERSION)


def _rebuild_fts(con, verbose=True):
    """重建 FTS 索引（独立副本表模式）。"""
    try:
        con.execute("INSERT INTO chapters_fts(chapters_fts) VALUES('rebuild')")
        con.commit()
    except sqlite3.OperationalError:
        # 独立副本表不支持 rebuild 指令，手工同步
        con.execute("DELETE FROM chapters_fts")
        con.execute(
            "INSERT INTO chapters_fts(chapter_ref, novel_id, title, content) "
            "SELECT id, novel_id, title, content FROM chapters WHERE deleted_at IS NULL"
        )
        con.commit()
    if verbose:
        n = con.execute("SELECT COUNT(*) FROM chapters_fts").fetchone()[0]
        print("  [fts] chapters_fts 现有 %d 行" % n)


# ---------------------------------------------------------------- 对外接口

def migrate(db_path=DEFAULT_DB, do_backup=True, verbose=True):
    brand_new = not os.path.exists(db_path)
    if verbose:
        print("== Schema v6.7 建库/升级 ==")
        print("  目标库：%s" % db_path)
        print("  状态：%s" % ("全新创建" if brand_new else "增量升级"))

    if not brand_new and do_backup:
        bak = _backup(db_path)
        if verbose:
            print("  备份：%s" % bak)

    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    con = _connect(db_path)
    try:
        before = _current_version(con)
        _apply_schema(con, verbose)
        _add_columns(con, verbose)
        _seed_defaults(con, verbose)
        _backfill_locations(con, verbose)
        _backfill_v67(con, verbose)
        _rebuild_fts(con, verbose)
        _record_version(con, note="v6.7 正史层与分支状态", verbose=verbose)
        after = _current_version(con)
        if verbose:
            print("  版本：%d -> %d" % (before, after))
            print("== 完成 ==")
        return {"ok": True, "db": db_path, "brand_new": brand_new,
                "version_before": before, "version_after": after}
    finally:
        con.close()


def health_check(db_path=DEFAULT_DB, verbose=True):
    if not os.path.exists(db_path):
        return {"ok": False, "error": "数据库不存在：%s" % db_path}
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    result = {"ok": True, "db": db_path}
    try:
        result["integrity"] = con.execute("PRAGMA integrity_check").fetchone()[0]
        result["foreign_keys_broken"] = len(
            con.execute("PRAGMA foreign_key_check").fetchall()
        )
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )]
        result["table_count"] = len(tables)
        result["tables"] = tables
        result["version"] = _current_version(con)
        result["novels"] = con.execute(
            "SELECT id, title, status, current_chapter FROM novels "
            "WHERE deleted_at IS NULL ORDER BY id"
        ).fetchall()
        result["novels"] = [dict(r) for r in result["novels"]]
    finally:
        con.close()
    if verbose:
        print("== 体检：%s ==" % db_path)
        print("  integrity      : %s" % result.get("integrity"))
        print("  外键断裂       : %s" % result.get("foreign_keys_broken"))
        print("  表数量         : %s" % result.get("table_count"))
        print("  schema version : %s" % result.get("version"))
        for nv in result.get("novels", []):
            print("  #%s %s [%s] 第 %s 章" % (
                nv["id"], nv["title"], nv["status"], nv["current_chapter"]))
    return result


def main():
    ap = argparse.ArgumentParser(description="Schema v6.7 建库/升级")
    ap.add_argument("--db", default=DEFAULT_DB, help="数据库路径")
    ap.add_argument("--no-backup", action="store_true", help="跳过备份")
    ap.add_argument("--check", action="store_true", help="仅体检，不修改")
    args = ap.parse_args()

    if args.check:
        health_check(args.db)
        return 0
    migrate(args.db, do_backup=not args.no_backup)
    health_check(args.db)
    return 0


if __name__ == "__main__":
    sys.exit(main())
