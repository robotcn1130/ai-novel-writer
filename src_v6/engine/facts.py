# -*- coding: utf-8 -*-
"""
Canonical Fact Ledger（正史账本）+ State Projection（状态投影）。

这个模块是 v6.7 改造的地基。它替掉了原来"世界状态可以到处写"的做法：

    events（不可变正史）
       │  投影（纯函数，可预演）
       ▼
    facts（正史账本，**只 supersede，从不 DELETE**）
       │  投影
       ▼
    world_state（只读缓存）· character_knowledge（认知，另一条独立链路）

## 三条不可破的规则

1. **`facts` 只接受世界真相三态**（`canonical` / `derived` / `provisional`）。
   角色的 believe/suspect/assume/memory/dream **一律不得进本表**——
   走 `db.record_knowledge()`；抽出来但定不了性的走 `db.add_fact_candidate()`。
   执行点就在 `add_fact()` 的第一行（约束 1）。

2. **只有 `world_state` 是投影缓存，facts 才是事实**。
   `world_state` 的写权限收口在 `db.set_state_value(writer=...)`，
   全项目只有 `applier.apply_projection()` 拿得到这个权限。
   本模块 **不直接调 set_state_value**——`write_state_projection()` 内部转调
   `applier.apply_projection()`（延迟 import，避免 facts ↔ applier 循环依赖）。

3. **只有"用状态量表达才自然"的事实类型才投影进 `world_state`**：
   `resource` / `progress` / `outcome`。
   位置、身份、关系分别由 `characters.location_id`、`character_identities`、
   `character_relations` 承载，**不做双重表达**——这正是"多处状态源打架"的病根。

## 与 character_knowledge 的分工（写进两个模块的 docstring）

> `facts` 回答「世界是什么样」，`character_knowledge` 回答「**谁**以为世界是什么样」。
> 二者**可以互相矛盾**——`is_fake` 标记的那种矛盾不是错误，是剧情。

## 演进示例（为什么"从不 DELETE"）

    第 3 章：老周 存活（active，source_event=12）
    第 8 章：老周 存活 → superseded_by=88        ← 不删！"读者以为他死了、
              老周 已死亡（active，source_event=88）   第 20 章发现没死"因此可查
    第 20 章：老周 已死亡 → superseded_by=214
              老周 重伤未死（active，source_event=214）
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC_V6 = os.path.dirname(_HERE)
for _p in (_SRC_V6, os.path.join(_SRC_V6, "core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# ---- 收口用的枚举 ----
# **从 core.database 引**，不在这里另写一份：这些元组一一对应 schema 里的 CHECK，
# 两处各写一份的话，改了 schema 而漏改这里就是 IntegrityError（本项目踩过）。
try:
    from core.database import (                    # noqa: F401
        FACT_STATUSES, FACT_TYPES, SOURCE_KINDS, CLASSIFIED_BY, CLASSIFICATIONS,
    )
except Exception:                                  # noqa: BLE001
    FACT_STATUSES = ("canonical", "derived", "provisional")
    FACT_TYPES = ("identity", "status", "location", "possession", "relationship",
                  "ability", "injury", "resource", "world_rule", "outcome",
                  "other")
    SOURCE_KINDS = ("sim", "prose", "user", "init")
    CLASSIFIED_BY = ("sim", "python", "llm", "user")
    CLASSIFICATIONS = ("world", "cognition", "unverified", "dream", "memory",
                       "author_claim")

OBJECT_TYPES = ("text", "int", "float", "bool", "json", "ref")

# 只有这三类事实"用状态量表达才自然"，才投影进 world_state
PROJECTABLE_TYPES = ("resource", "progress", "outcome")

# 状态类别 → fact_type（世界页手改 / 初始化落 fact 时用）
CATEGORY_TO_FACT_TYPE = {
    "tension": "outcome", "resource": "resource", "threat": "outcome",
    "progress": "progress", "relation": "relationship",
    # ★ "other" 必须映射成**可投影**的 outcome，不能原样映射成 "other"。
    #   "other" 不在 PROJECTABLE_TYPES 里 → 写进账本的事实永远进不了 world_state。
    #   而 "other" 恰恰是推演 JSON 与建书表单里**最常见的默认分类**，
    #   于是"推演改了个状态、世界页看不到"会变成常态（不报错、不崩，就是没有）。
    #   outcome 是"可投影里的兜底"，且 `_fact_type_to_category("outcome") == "other"`，
    #   这样来回映射能闭合：category other → fact_type outcome → category other。
    "other": "outcome",
}

# 手工锁定行（world_state.derived_from_fact_id）：
#   NULL  = 用户手工改过 → **投影不覆盖**（改完就丢的老 bug 就是这里修的）
#   0     = 自动来源，具体 fact 未知（迁移期的老行）
#   >0    = 由这条 fact 投影而来
# 注：锁定不是不可逆的，世界页提供「恢复自动投影」（db.unlock_state_value）。
MANUAL = None
AUTO_UNKNOWN = 0

# P1 常量：每 500 个 canonical event 打一次快照。**第一版只是声明，不实现。**
SNAPSHOT_INTERVAL = 500


# ============================================================ 工具

def _s(value, default=""):
    if value is None:
        return default
    return str(value).strip()


def _i(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def fact_key(fact):
    """状态投影用的 key：`subject_name.predicate`。

    例：subject_name='异常实体' + predicate='剩余数量' → `异常实体.剩余数量`
    这个规则**必须确定**，否则投影出来的 key 与老数据对不上
    （老库里的 key 就是这个形状）。
    """
    subj = _s((fact or {}).get("subject_name"))
    pred = _s((fact or {}).get("predicate"))
    if not subj:
        subj = _s((fact or {}).get("subject_type")) or "world"
    return "%s.%s" % (subj, pred) if pred else subj


def _norm_enum(value, allowed, default, aliases=None):
    """宽容枚举收口（模型自由文本不许直连带 CHECK 的列）。"""
    v = _s(value).lower()
    if v in allowed:
        return v
    if aliases and v in aliases:
        return aliases[v]
    return default


_FACT_TYPE_ALIAS = {
    "存活": "status", "状态": "status", "生死": "status",
    "位置": "location", "地点": "location", "行踪": "location",
    "身份": "identity", "职业": "identity",
    "持有": "possession", "物品": "possession", "所有物": "possession",
    "关系": "relationship", "羁绊": "relationship",
    "能力": "ability", "技能": "ability",
    "伤": "injury", "伤势": "injury", "受伤": "injury",
    "资源": "resource", "数量": "resource",
    "规则": "world_rule", "法则": "world_rule",
    "结果": "outcome", "结局": "outcome", "进展": "progress",
}
_OBJECT_TYPE_ALIAS = {"number": "float", "数值": "float", "数字": "float",
                      "整数": "int", "布尔": "bool", "文本": "text"}


# ============================================================ 写：唯一入口

def add_fact(db, novel_id, subject_type="world", subject_id=None,
             subject_name="", predicate="", object_text="", object_type="text",
             object_ref_id=None, fact_type="other", tick=0,
             source_event_id=None, source_chapter=0, source_kind="sim",
             fact_status="canonical", confidence=5, classified_by="sim",
             note="", quote=""):
    """写入一条正史事实。**这是 facts 的唯一写入口。**

    行为：
      1. 【约束 1 的执行点】`fact_status` 必须是世界真相三态之一，否则抛 ValueError。
         角色的 believe/suspect/assume/memory/dream 在**类型层面**就进不来。
      2. 查同键 active fact：
         · 存在且值不同 → `supersede_fact()` 旧条，再插新条（**不删旧条**）
         · 存在且值相同 → 不插重复行，只补一条来源记录
      3. 返回值：fact id（值相同的情形返回已有行的 id）。

    参数：
      tick            该事实从哪个 tick 起成立（valid_from_tick）
      source_kind     sim / prose / user / init —— 谁说的这条
      classified_by   谁把它判成 world 的（sim/python/llm/user）—— 约束 2 的可追溯性
    """
    # ---- 约束 1：唯一执行点
    if fact_status not in FACT_STATUSES:
        raise ValueError(
            "角色认知/记忆/梦境/未证实的内容不得写入 facts"
            "（fact_status=%r 非法；合法值只有 %s）。"
            "请改走 db.record_knowledge() 或 db.add_fact_candidate()。"
            % (fact_status, "、".join(FACT_STATUSES)))

    subject_type = _norm_enum(subject_type, ("character", "entity", "world"),
                              "world")
    object_type = _norm_enum(object_type, OBJECT_TYPES, "text", _OBJECT_TYPE_ALIAS)
    fact_type = _norm_enum(fact_type, FACT_TYPES, "other", _FACT_TYPE_ALIAS)
    source_kind = _norm_enum(source_kind, SOURCE_KINDS, "sim")
    classified_by = _norm_enum(classified_by, CLASSIFIED_BY, "python")
    try:
        confidence = int(confidence)
    except (TypeError, ValueError):
        confidence = 5
    confidence = min(5, max(1, confidence))
    tick = _i(tick, 0)
    subject_name = _s(subject_name)
    predicate = _s(predicate)
    object_text = "" if object_text is None else str(object_text)

    if not predicate:
        # 没有谓词的"事实"是无意义的（投影不出 key，也没法比对冲突）。
        # 不抛异常（会打断整批推演），但也绝不落库。
        return 0

    if subject_type != "world" and not subject_id:
        row = _find_subject_id(db, novel_id, subject_type, subject_name)
        subject_id = row

    with db.tx():
        exist = find_fact(db, novel_id, subject_type, subject_id, predicate,
                          subject_name=subject_name)
        if exist:
            if str(exist.get("object_text") or "") == object_text:
                add_fact_source(db, exist["id"], source_kind,
                                source_event_id or 0, source_chapter, quote)
                return exist["id"]
            # ⚠ **必须先 supersede，再 insert**。
            #   `uq_facts_active` 是 partial unique（WHERE status='active'），
            #   旧行还 active 时插新行会直接撞唯一索引 —— 这一步反了就是
            #   "IntegrityError: UNIQUE constraint failed"，一条都写不进去。
            #   （踩过：先 insert 后 supersede，整条事实链断在这里。）
            supersede_fact(db, exist["id"], by_fact_id=None,
                           reason="被新事实取代（%s）" % (source_kind or "sim"),
                           tick=tick)
            fid = _insert_fact(
                db, novel_id, subject_type, subject_id, subject_name, predicate,
                object_text, object_type, object_ref_id, fact_type, tick,
                source_event_id, source_chapter, source_kind, fact_status,
                confidence, classified_by, note)
            if fid:
                # 回填指向关系（supersede 时还不知道新 id）
                db.execute("UPDATE facts SET superseded_by=? WHERE id=?",
                           (fid, exist["id"]))
            add_fact_source(db, fid, source_kind, source_event_id or 0,
                            source_chapter, quote)
            return fid
        fid = _insert_fact(
            db, novel_id, subject_type, subject_id, subject_name, predicate,
            object_text, object_type, object_ref_id, fact_type, tick,
            source_event_id, source_chapter, source_kind, fact_status,
            confidence, classified_by, note)
        add_fact_source(db, fid, source_kind, source_event_id or 0,
                        source_chapter, quote)
        return fid


def _find_subject_id(db, novel_id, subject_type, subject_name):
    """按名字补出 subject_id（模型只给名字是常态）。"""
    if not subject_name:
        return None
    try:
        if subject_type == "character":
            row = db.get_character_by_name(novel_id, subject_name)
            return row["id"] if row else None
        if subject_type == "entity":
            row = db.find_entity(novel_id, subject_name)
            return row["id"] if row else None
    except Exception:                       # noqa: BLE001
        return None
    return None


def _insert_fact(db, novel_id, subject_type, subject_id, subject_name, predicate,
                 object_text, object_type, object_ref_id, fact_type, tick,
                 source_event_id, source_chapter, source_kind, fact_status,
                 confidence, classified_by, note):
    return db.execute(
        "INSERT INTO facts(novel_id, subject_type, subject_id, subject_name, "
        "predicate, object_text, object_type, object_ref_id, fact_type, "
        "valid_from_tick, source_event_id, source_chapter, source_kind, "
        "fact_status, confidence, classified_by, note) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (novel_id, subject_type, subject_id, subject_name, predicate,
         object_text, object_type, object_ref_id, fact_type, _i(tick),
         source_event_id, _i(source_chapter), source_kind, fact_status,
         confidence, classified_by, _s(note)))


def add_fact_source(db, fact_id, kind="sim", event_id=0, chapter_number=0,
                    quote=""):
    """补一条来源记录（一条事实可以由多个来源共同支撑）。"""
    if not fact_id:
        return 0
    try:
        return db.execute(
            "INSERT OR IGNORE INTO fact_sources(fact_id, event_id, "
            "chapter_number, kind, quote) VALUES (?,?,?,?,?)",
            (int(fact_id), _i(event_id), _i(chapter_number),
             _norm_enum(kind, SOURCE_KINDS, "sim"), _s(quote)[:400]))
    except Exception:                       # noqa: BLE001
        return 0


def supersede_fact(db, fact_id, by_fact_id=None, reason="", tick=0):
    """把旧事实标记为被取代。**绝不 DELETE**——正史要可回溯。

    对"读者以为他死了，第 20 章发现没死"这类跨越几十章的翻转，全靠这一条。
    """
    if not fact_id:
        return 0
    return db.execute(
        "UPDATE facts SET status='superseded', superseded_by=?, "
        "supersede_reason=?, valid_to_tick=COALESCE(valid_to_tick, ?) "
        "WHERE id=? AND status='active'",
        (by_fact_id, _s(reason)[:200], _i(tick), int(fact_id)))


def revoke_fact(db, fact_id, reason=""):
    """用户手工作废一条事实（"这条不算"）。"""
    if not fact_id:
        return 0
    return db.execute(
        "UPDATE facts SET status='revoked', supersede_reason=? WHERE id=?",
        (_s(reason)[:200], int(fact_id)))


# ============================================================ 读

def list_facts(db, novel_id, subject_type=None, subject_id=None,
               subject_name=None, predicate=None, fact_type=None,
               active_only=True, as_of_tick=None, status=None, limit=None):
    """查事实。

    `as_of_tick` 非空时返回"**那个时刻为真**的事实"
    （valid_from_tick<=t AND (valid_to_tick IS NULL OR valid_to_tick>t)）
    —— 这是回答"第 10 章的时候世界是什么样"的唯一正确办法。
    沙盘分支推演读分叉点的世界，靠的就是它。
    """
    # `quote`（原始出处那句话）不在 facts 表里，它在 fact_sources。
    # 这里用相关子查询取**最新一条非空引文**带上，而不是让前端再打一次接口：
    # 账本列表本来就要展示"这条事实是从哪句话得出来的"，
    # 前端拿不到就只会渲染出一片空白（不报错、不崩，就是那一格没有）。
    sql = ("SELECT f.*, (SELECT s.quote FROM fact_sources s "
           "WHERE s.fact_id = f.id AND s.quote <> '' "
           "ORDER BY s.id DESC LIMIT 1) AS quote "
           "FROM facts f WHERE novel_id=?")
    params = [novel_id]
    if active_only and not status and as_of_tick is None:
        sql += " AND status='active'"
    if status:
        sql += " AND status=?"
        params.append(status)
    if subject_type:
        sql += " AND subject_type=?"
        params.append(subject_type)
    if subject_id is not None:
        sql += " AND subject_id=?"
        params.append(int(subject_id))
    if subject_name:
        sql += " AND subject_name=?"
        params.append(subject_name)
    if predicate:
        sql += " AND predicate=?"
        params.append(predicate)
    if fact_type:
        sql += " AND fact_type=?"
        params.append(fact_type)
    if as_of_tick is not None:
        t = _i(as_of_tick)
        sql += (" AND fact_status IN ('canonical','derived') "
                "AND valid_from_tick<=? "
                "AND (valid_to_tick IS NULL OR valid_to_tick>?) "
                "AND status IN ('active','superseded')")
        params.extend([t, t])
    sql += " ORDER BY fact_type, subject_name, predicate, id"
    if limit:
        sql += " LIMIT ?"
        params.append(int(limit))
    return db.query(sql, params)


def find_fact(db, novel_id, subject_type, subject_id, predicate,
              active_only=True, subject_name=None):
    """查单条（同主体+同谓词）。这是加事实前必查的一步。

    三条路径，**都不能跨主体命中**：

    | 输入 | 查法 | 为什么 |
    |---|---|---|
    | 有 `subject_id` | 按 id 查 | 精确 |
    | 有 id 但库里没这条 | 再加一轮**同名**查 | 那条事实入库时主体还没建档，`subject_id` 落成了 NULL |
    | 具名主体却没 id | 按 `subject_name` 查 | 只能按名字认人 |

    ⚠ **绝不能"只按 predicate 查"兜底**。踩过：给"林默 存活"查同键时命中
    "老周 存活"，后果是双重的——
      · 冲突检查把一个人的状态报成另一个人的矛盾；
      · `add_fact` 更糟，它会把**老周**那条 supersede 掉。
    一个人改状态、另一个人的正史被顶掉，是最难查的一类脏数据。

    ⚠ 也**不能**在具名主体没给 id 时退化成 `subject_id IS NULL` —— 那样永远
    查不到（事实存的是名字），等于冲突检测静默失效。
    """
    name = _s(subject_name)
    if subject_id:
        row = _find_fact_row(db, novel_id, subject_type, predicate,
                             subject_id=int(subject_id),
                             active_only=active_only)
        if row or not name:
            return row
        # 兜底：这条事实入库时主体还没建档，subject_id 是 NULL
        return _find_fact_row(db, novel_id, subject_type, predicate,
                              subject_name=name, active_only=active_only)
    if subject_type != "world" and name:
        return _find_fact_row(db, novel_id, subject_type, predicate,
                              subject_name=name, active_only=active_only)
    # 世界级事实：没有主体 id，也没有主体名
    return _find_fact_row(db, novel_id, subject_type, predicate,
                          active_only=active_only)


def _find_fact_row(db, novel_id, subject_type, predicate, subject_id=None,
                   subject_name=None, active_only=True):
    sql = ("SELECT * FROM facts WHERE novel_id=? AND subject_type=? "
           "AND predicate=?")
    params = [novel_id, subject_type, predicate]
    if subject_id is not None:
        sql += " AND subject_id=?"
        params.append(int(subject_id))
    elif subject_name:
        sql += " AND subject_name=?"
        params.append(subject_name)
    else:
        sql += " AND subject_id IS NULL"
    if active_only:
        sql += " AND status='active'"
    sql += " ORDER BY id DESC LIMIT 1"
    return db.one(sql, params)


def count_facts(db, novel_id, fact_type=None):
    sql = "SELECT COUNT(*) FROM facts WHERE novel_id=? AND status='active'"
    params = [novel_id]
    if fact_type:
        sql += " AND fact_type=?"
        params.append(fact_type)
    return int(db.scalar(sql, params, default=0) or 0)


def summary(db, novel_id):
    """第一页状态条用：正史账本的一句话概况。"""
    return {
        "facts": count_facts(db, novel_id),
        "rules": int(db.scalar(
            "SELECT COUNT(*) FROM world_rules WHERE novel_id=? AND is_active=1",
            (novel_id,), default=0) or 0),
        "knowledge": int(db.scalar(
            "SELECT COUNT(*) FROM character_knowledge WHERE novel_id=? "
            "AND status='active'", (novel_id,), default=0) or 0),
        "pending_candidates": int(db.scalar(
            "SELECT COUNT(*) FROM fact_candidates WHERE novel_id=? "
            "AND status='pending'", (novel_id,), default=0) or 0),
        "open_conflicts": int(db.scalar(
            "SELECT COUNT(*) FROM fact_conflicts WHERE novel_id=? "
            "AND status='open'", (novel_id,), default=0) or 0),
    }


def facts_block(db, novel_id, max_rows=30, group_by_type=True, as_of_tick=None,
                only_types=None):
    """给提示词用的【正史事实】段。

    这一段替代原来零散的"世界状态 + 死伤名册"：模型需要的是**真正的事实层**，
    而不是一堆 key=value（后者没有主语、没有时效、没有来源）。

    `as_of_tick` 非空 = 分支推演：给的是**分叉点当时为真的事实**，不是最新事实。
    """
    rows = [f for f in list_facts(db, novel_id, as_of_tick=as_of_tick,
                                  limit=max_rows * 2)
            if f.get("fact_status") in ("canonical", "derived")]
    if only_types:
        rows = [f for f in rows if f.get("fact_type") in only_types]
    if not rows:
        return ""
    label = {"identity": "身份", "status": "状态", "location": "位置",
             "possession": "持有", "relationship": "关系", "ability": "能力",
             "injury": "伤势", "resource": "资源", "world_rule": "世界规则",
             "outcome": "结果", "other": "其他"}
    lines = []
    if not group_by_type:
        for f in rows[:max_rows]:
            lines.append("  - %s %s" % (_subject(f), _statement(f)))
        return "\n".join(lines)
    by_type = {}
    for f in rows:
        by_type.setdefault(f.get("fact_type") or "other", []).append(f)
    order = [t for t in FACT_TYPES if t in by_type]
    for t in order:
        items = by_type[t]
        lines.append("  <%s>" % label.get(t, t))
        for f in items[:max_rows]:
            lines.append("    - %s %s" % (_subject(f), _statement(f)))
        if len(items) > max_rows:
            lines.append("    - …（同类还有 %d 条）" % (len(items) - max_rows))
    return "\n".join(lines)


def _subject(f):
    st = f.get("subject_type")
    name = _s(f.get("subject_name")) or ("世界" if st == "world" else "?")
    return name


def _statement(f):
    pred = _s(f.get("predicate"))
    val = _s(f.get("object_text"))
    if f.get("object_type") == "bool":
        val = {"1": "是", "true": "是", "0": "否", "false": "否"}.get(
            val.lower(), val)
    return "%s：%s" % (pred, val)


def fact_view(f):
    """给前端的一条事实视图（带上"这是谁说的/谁判的"）。"""
    return {
        "id": f.get("id"), "subject_type": f.get("subject_type"),
        "subject_id": f.get("subject_id"),
        "subject_name": _s(f.get("subject_name")),
        "predicate": _s(f.get("predicate")),
        "object_text": _s(f.get("object_text")),
        "object_type": f.get("object_type"),
        "fact_type": f.get("fact_type"),
        "fact_status": f.get("fact_status"),
        "confidence": f.get("confidence"),
        "classified_by": f.get("classified_by"),
        "status": f.get("status"),
        "valid_from_tick": f.get("valid_from_tick"),
        "valid_to_tick": f.get("valid_to_tick"),
        "source_kind": f.get("source_kind"),
        "source_event_id": f.get("source_event_id"),
        "source_chapter": f.get("source_chapter"),
        # 原始出处那句话（来自 fact_sources，见 list_facts 的 join）
        "quote": _s(f.get("quote")),
        "supersede_reason": _s(f.get("supersede_reason")),
        "superseded_by": f.get("superseded_by"),
        "note": _s(f.get("note")),
        "created_at": f.get("created_at"),
        "key": fact_key(f),
        "statement": _statement(f),
    }


# ============================================================ 投影

def project_state(db, novel_id, upto_tick=None, facts_rows=None):
    """State Projection：把可投影的 active facts 折成 `{key: value}`（纯函数）。

    只有 `fact_type in ('resource','progress','outcome')` 参与——位置/身份/关系
    分别由 characters / character_identities / character_relations 承载，
    在这里再表达一遍就会出现两个真相源。

    返回 `{key: {"value":…, "value_type":…, "category":…, "fact_id":…}}`。
    """
    rows = facts_rows if facts_rows is not None else list_facts(
        db, novel_id, active_only=True)
    out = {}
    for f in rows:
        if f.get("fact_status") not in ("canonical", "derived"):
            continue                       # provisional 只做展示，不进缓存
        if f.get("fact_type") not in PROJECTABLE_TYPES:
            continue
        key = fact_key(f)
        if not key:
            continue
        out[key] = {
            "value": f.get("object_text"),
            "value_type": f.get("object_type") or "text",
            "category": _fact_type_to_category(f.get("fact_type")),
            "fact_id": f.get("id"),
            "tick": _i(f.get("valid_from_tick")),
            "subject_name": _s(f.get("subject_name")),
            "predicate": _s(f.get("predicate")),
        }
    if upto_tick is not None:
        out = {k: v for k, v in out.items() if _i(v.get("tick")) <= _i(upto_tick)}
    return out


def _fact_type_to_category(fact_type):
    return {"resource": "resource", "progress": "progress",
            "outcome": "other"}.get(fact_type, "other")


def write_state_projection(db, novel_id, upto_tick=None):
    """把 `project_state()` 的结果写进 `world_state` 缓存。

    **不直接调 `db.set_state_value`**——那需要 `writer` 白名单，
    而白名单里只有一个名字（`applier.apply_projection`）。这里转调
    `applier.apply_projection()`（延迟 import，避免板块间循环依赖），
    于是"唯一落库点"这条铁律仍然成立。

    返回 `{"updated": n, "skipped": m}`——skipped 是被手工锁定的行
    （`derived_from_fact_id IS NULL`，用户在世界上手改过的值）。
    """
    from engine import applier
    proj = project_state(db, novel_id, upto_tick=upto_tick)
    if not proj:
        return {"updated": 0, "skipped": 0}
    tick = _i(upto_tick) if upto_tick is not None else _current_tick(db, novel_id)
    state = []
    for key, v in proj.items():
        state.append({
            "key": key, "value": v.get("value"), "value_type": v.get("value_type"),
            "category": v.get("category"), "reason": "由正史事实投影",
            "fact_id": v.get("fact_id"),
        })
    res = applier.apply_projection(db, novel_id, {"state": state}, tick=tick)
    return {"updated": int((res or {}).get("state") or 0),
            "skipped": int((res or {}).get("state_skipped") or 0)}


def _current_tick(db, novel_id):
    from engine import ticktime
    return ticktime.current_tick(db, novel_id)


def _clock_text(db, novel_id):
    """世界时间的展示文本（自由文本，只用于展示，不参与比较）。"""
    try:
        return (db.get_clock(novel_id) or {}).get("current_time") or ""
    except Exception:                       # noqa: BLE001
        return ""


def diff_projection(base, projected):
    """沙盘算 Δ：`{"key": {"from":…, "to":…}}`（纯函数）。"""
    base = base or {}
    projected = projected or {}

    def _val(node):
        if isinstance(node, dict) and "value" in node:
            return node["value"]
        return node

    out = {}
    for key, val in projected.items():
        new_v = _val(val)
        if key not in base:
            out[key] = {"from": None, "to": new_v}
        elif str(_val(base[key])) != str(new_v):
            out[key] = {"from": _val(base[key]), "to": new_v}
    for key in base:
        if key not in projected:
            out[key] = {"from": _val(base[key]), "to": None}
    return out


def apply_delta(base, delta):
    """沙盘叠加：`Base + Δ`（纯函数）。"""
    out = dict(base or {})
    for key, ch in (delta or {}).items():
        if isinstance(ch, dict) and "to" in ch:
            if ch["to"] is None:
                out.pop(key, None)
            else:
                out[key] = ch["to"]
        else:
            out[key] = ch
    return out


def facts_as_delta(before, after):
    """两份事实快照的差（沙盘与 commit 都用它）。

    before/after 是 `{key: value}` 形状（`project_state()` 的输出或其简化版）。
    """
    return diff_projection(before, after)


def merge_overlay(overlay_list):
    """把一条分支上所有节点的 state_delta 依次叠加，得到累计 overlay。"""
    acc = {}
    for d in (overlay_list or []):
        acc = apply_delta(acc, d)
    return acc


# ============================================================ 迁移重建（人工触发）

def rebuild_from_events(db, novel_id, upto_event_id=None, dry_run=False):
    """由正史事件重建 facts。**只由用户手工触发，迁移不自动跑。**

    为什么迁移不自动做（这是本设计里最需要坚持的一点）：
      老库的 `events.structured` 可能是空/半截（v6.x 前期的截断抢救产物），
      强行投影会造出**错误的正史**——比没有正史更糟
      （第 20 章的推演会基于一条假事实往下演）。

    重建出来的全部标 `fact_status='derived'`、`classified_by='python'`，
    可以整批删掉重来（见 `clear_derived`）。
    """
    rows = db.query(
        "SELECT * FROM events WHERE novel_id=? AND deleted_at IS NULL "
        + ("AND id<=? " % _i(upto_event_id) if upto_event_id else "")
        + "ORDER BY id", (novel_id,))
    made, skipped = 0, 0
    proj = {"facts": []}
    for ev in rows:
        structured = ev.get("structured")
        if isinstance(structured, str):
            try:
                structured = json.loads(structured or "{}")
            except (ValueError, TypeError):
                structured = {}
        changes = (structured or {}).get("state_changes") or {}
        world = changes.get("world") or []
        chars = changes.get("characters") or []
        if not world and not chars:
            skipped += 1
            continue
        for ch in world:
            key = _s(ch.get("key"))
            if not key:
                continue
            subj, _, pred = key.partition(".")
            proj["facts"].append({
                "subject_type": "world" if not pred else "entity",
                "subject_name": subj, "predicate": pred or key,
                "object_text": ch.get("value"), "fact_type": "other",
                "source_event_id": ev["id"], "fact_status": "derived",
                "classified_by": "python"})
        for ch in chars:
            name = _s(ch.get("name"))
            if not name:
                continue
            if ch.get("status"):
                proj["facts"].append({
                    "subject_type": "character", "subject_name": name,
                    "predicate": "存活", "object_text": ch["status"],
                    "fact_type": "status", "source_event_id": ev["id"],
                    "fact_status": "derived", "classified_by": "python"})
            if ch.get("location"):
                proj["facts"].append({
                    "subject_type": "character", "subject_name": name,
                    "predicate": "位于", "object_text": ch["location"],
                    "fact_type": "location", "source_event_id": ev["id"],
                    "fact_status": "derived", "classified_by": "python"})
    if dry_run:
        return {"facts": len(proj["facts"]), "skipped": skipped,
                "dry_run": True}
    from engine import applier
    res = applier.apply_projection(
        db, novel_id, proj, tick=_current_tick(db, novel_id), chapter_ref=0)
    made = int((res or {}).get("facts") or 0)
    return {"facts": made, "skipped": skipped}


def clear_derived(db, novel_id):
    """把重建出来的 derived 事实整批删掉（重来一次的入口）。

    只删 `fact_status='derived' AND source_kind='init'` 之外的重建产物：
    判定标准是 `fact_status='derived'`（推演/正文产生的事实永远是 canonical）。
    """
    n = db.scalar("SELECT COUNT(*) FROM facts WHERE novel_id=? "
                  "AND fact_status='derived'", (novel_id,), default=0)
    db.execute("DELETE FROM facts WHERE novel_id=? AND fact_status='derived'",
               (novel_id,))
    return int(n or 0)


def seed_state(db, novel_id, items, chapter_ref=0, tick=0):
    """世界初始化：把设定里的初始状态量写成 facts（source_kind='init'）。

    老路径是直接 `db.set_state_value(...)`（世界初始化时批量写状态）。
    现在改走 facts —— 因为**初始状态也是世界真相**，它必须进账本，
    否则第 30 章回头看"这个数字最初是多少"就查不到了。
    返回 `{"facts": n, "state": m}`。
    """
    n = 0
    for st in (items or []):
        key = _s((st or {}).get("key"))
        if not key:
            continue
        subj, _, pred = key.partition(".")
        try:
            fid = add_fact(
                db, novel_id,
                subject_type="world" if not pred else "entity",
                subject_name=subj, predicate=pred or key,
                object_text=st.get("value"),
                object_type=st.get("value_type") or "text",
                fact_type=CATEGORY_TO_FACT_TYPE.get(
                    _s(st.get("category")) or "other", "other"),
                tick=tick, source_chapter=chapter_ref, source_kind="init",
                fact_status="canonical", confidence=5,
                classified_by="user", note="世界初始化")
        except ValueError:
            continue
        if fid:
            n += 1
    res = write_state_projection(db, novel_id)
    return {"facts": n, "state": res.get("updated", 0)}


def register_rule_as_fact(db, novel_id, rule_id, content, tick=0):
    """把一条 world_rule 也记进正史账本（规则是世界的硬事实）。"""
    return add_fact(db, novel_id, subject_type="world",
                    subject_name=_s(_rule_subject(db, novel_id, rule_id)),
                    predicate="世界规则", object_text=content,
                    fact_type="world_rule", tick=tick, source_kind="user",
                    fact_status="canonical", classified_by="user",
                    note="world_rules#%s" % rule_id)


def _rule_subject(db, novel_id, rule_id):
    row = db.one("SELECT subject_name FROM world_rules WHERE id=?",
                 (_i(rule_id),))
    return (row or {}).get("subject_name") or "世界"


def check_projection_conflicts(db, novel_id, projection, chapter_number=0):
    """把"正文抽出来的事实"与"正史事实"逐条比。**大部分冲突不需要 LLM。**

    ▲ 只比 `classification='world'` 的条目：cognition 类根本不进 facts，
    谈不上冲突（这正是约束 1"严格分离"带来的简化）。

    返回冲突列表（不写库——写库在 `applier._enqueue_conflicts`）。
    """
    conflicts = []
    for f in ((projection or {}).get("facts") or []):
        if _s(f.get("classification") or "world") != "world":
            continue
        if _s(f.get("op")) == "supersede":
            continue
        subject_type = _norm_enum(f.get("subject_type"),
                                  ("character", "entity", "world"), "world")
        subject_id = f.get("subject_id") or _find_subject_id(
            db, novel_id, subject_type, _s(f.get("subject_name")))
        predicate = _s(f.get("predicate"))
        if not predicate:
            continue
        canon = find_fact(db, novel_id, subject_type, subject_id, predicate,
                          subject_name=_s(f.get("subject_name")))
        quote = _s(f.get("quote") or f.get("provenance") or f.get("prose_quote"))
        if canon is None:
            conflicts.append(_mk_conflict(
                "prose_invents_fact", chapter_number, None,
                _s(f.get("subject_name")), quote,
                "%s：%s" % (predicate, _s(f.get("object_text"))),
                "正文写了一件正史里没有的事，待裁决", severity="warning"))
        elif str(canon.get("object_text") or "") != _s(f.get("object_text")):
            # ▲ 「位于」是**会变的**位置属性，不是永恒正史。
            #   一个人在堂屋说完话转身进灶间，这是正常的叙事推进；
            #   若把它按字符串相等去硬比，**几乎每一章都会被拦下**
            #   （2026-09-19 实测：78 号第 1 章 11 条冲突里 2 条全是这个）。
            #   规则：正史位置与正文位置**同属一条地点路径**时（一个包含另一个，
            #   或本来就同前缀），只记不拦 —— 那只是"他从堂屋走到了院子里"。
            #   真正跨地方的矛盾（正史在樟溪镇、正文在临安）仍然拦。
            if _s(f.get("fact_type")) == "location" or predicate == "位于":
                sev = ("warning" if _same_place_path(
                    str(canon.get("object_text") or ""),
                    _s(f.get("object_text"))) else "error")
            else:
                sev = "error"
            conflicts.append(_mk_conflict(
                "prose_contradicts_fact", chapter_number, canon["id"],
                _s(f.get("subject_name")), quote,
                "%s：%s" % (predicate, _s(canon.get("object_text"))),
                "正文写的与正史冲突（正文 %r / 正史 %r）"
                % (_s(f.get("object_text")), _s(canon.get("object_text"))),
                severity=sev))
    # 认知泄漏：正文让某人"知道"了他没有渠道知道的事
    for k in ((projection or {}).get("knowledge") or []):
        if _s(k.get("know_type")) != "know":
            continue
        cid = k.get("character_id")
        about = _s(k.get("about_text"))
        if not (cid and about):
            continue
        if not db.has_knowledge(novel_id, cid, about):
            conflicts.append(_mk_conflict(
                "knowledge_leak", chapter_number, None,
                _s(k.get("character_name")), _s(k.get("content")),
                about, "他没有渠道知道这件事", severity="error"))
    return conflicts


def _same_place_path(a, b):
    """两个地点文本是否**同一个地方（或其内部）**。

    判定：把地点路径按层级切开，看**父级路径是否一致** —— 只比到
    "两者较短的那个的上一级"。这样同一栋房子里的两个房间算同一个地方，
    跨城镇才算真矛盾。

    | 正史 | 正文 | 结果 | 为什么 |
    |---|---|---|---|
    | 樟溪镇·沈家·堂屋 | 樟溪镇·沈家·灶间 | True | 同一家的两个房间 |
    | 樟溪镇·沈家·堂屋 | 樟溪镇·沈家·院子（劈柴） | True | 同上 |
    | 樟溪镇·沈家 | 樟溪镇·沈家·东屋 | True | 正史粗、正文细，是更精确 |
    | 樟溪镇·沈家·堂屋 | 樟溪镇 | True | 正文粗、正史细，地点没跑 |
    | 樟溪镇·沈家 | 临安·古玩城 | False | 跨地方，真矛盾 |
    | 临安 | 樟溪镇 | False | 同上 |

    ⚠ 关键：**不能直接比整条路径**。"堂屋" vs "灶间" 整条比是不等的，
    但它们其实是同一个家的两个房间 —— 正文让角色从堂屋走到灶间，
    是正常的叙事推进，不是改设定。2026-09-19 实测：78 号第 1 章被拦下的
    2 条 error 全是这一类。
    """
    def segs(s):
        s = _s(s)
        for ch in ("·", "/", ">", "\\", "、", ","):
            s = s.replace(ch, ".")
        return [p for p in (x.strip() for x in s.split(".")) if p]

    sa, sb = segs(a), segs(b)
    if not sa or not sb:
        return False
    if sa == sb:
        return True
    # 比父级：留出较短那条的最后一段（它可能是更细的房间名）
    n = min(len(sa), len(sb))
    if n == 1:
        # 两边都只到第一级：只有完全相同才算同一地方
        return sa[:1] == sb[:1]
    return sa[:n - 1] == sb[:n - 1]


def _mk_conflict(ctype, chapter_number, fact_id, subject_name, quote,
                 fact_statement, detail, severity="error", detected_by="python"):
    return {
        "conflict_type": ctype, "chapter_number": _i(chapter_number),
        "fact_id": fact_id, "subject_name": _s(subject_name),
        "prose_quote": _s(quote)[:300],
        "fact_statement": _s(fact_statement)[:300],
        "detail": _s(detail)[:300], "severity": severity,
        "detected_by": detected_by,
    }


# ============================================================ 冲突裁决

def resolve_conflict(db, novel_id, conflict_id, action, note="", tick=None):
    """裁决一条正文↔正史冲突（设计 §7.3 的三个按钮）。

    | action | 做什么 | 结果 |
    |---|---|---|
    | `keep_canon` | 只改冲突状态，**不改正史** | 本章 `verified=0`，等用户去改正文 |
    | `keep_prose` | 正文这条升级为正史 + 补一条 `canon_revision` 事件 | 正史被改，且**事件流完整** |
    | `ignored`    | 记录在案 | 本章 `verified=1` |

    ⚠ **`keep_prose` 必须补事件**（这条在 v1.0 就定了，不要省）：
    方案原话是「正史不是正文说了算，而是正文负责呈现、Canonical State 负责裁决」。
    只改 fact 不补事件，`facts` 就出现了一条**无来源**的变更——第 20 章的推演
    会看到一个来路不明的"李四死了"，因果链断在那里。

    ⚠ 补的事件是 `canon_revision`，**不是 `reveal`**（约束 3）：
    reveal = 剧情里揭示秘密；这里可能是"正文发现了正史的错误"、
    "作者主动改设定"、"作者确认新增事实入正史"——三者都不是 reveal。
    它还必须带 `source_kind='user'`，一眼看出是作者动作而非剧情推进。
    """
    row = db.one("SELECT * FROM fact_conflicts WHERE id=?", (int(conflict_id),))
    if not row:
        return {"ok": False, "error": "冲突记录不存在"}
    action = _s(action)
    if action not in ("keep_canon", "keep_prose", "ignored"):
        return {"ok": False, "error": "action 必须是 keep_canon / keep_prose / ignored"}
    ch = _i(row.get("chapter_number"))
    note = _s(note)
    subject = _s(row.get("subject_name"))
    quote = _s(row.get("prose_quote"))
    statement = _s(row.get("fact_statement"))
    old_fact_id = row.get("fact_id")

    out = {"ok": True, "action": action, "chapter_number": ch,
           "conflict_id": int(conflict_id)}

    if action == "keep_prose":
        # 正文那条事实 → 正史。predicate 从 fact_statement 里取（"谓词：值"）
        pred = statement.split("：", 1)[0].strip() or statement
        val = statement.split("：", 1)[1].strip() if "：" in statement else ""
        if not val:
            # fact_statement 可能是"谓词：正文值"而正文值更可靠，退回用引文
            val = quote
        stype = "character" if subject else "world"
        sid = _find_subject_id(db, novel_id, stype, subject) if subject else None
        fid = add_fact(
            db, novel_id, subject_type=stype, subject_id=sid,
            subject_name=subject, predicate=pred, object_text=val,
            fact_type="other", tick=_i(tick) if tick is not None
            else _current_tick(db, novel_id),
            source_chapter=ch, source_kind="prose",
            fact_status="canonical", confidence=5, classified_by="user",
            note="按作者裁决采纳正文（冲突 #%s）" % conflict_id, quote=quote)
        out["fact_id"] = fid
        if old_fact_id and fid:
            out["superseded"] = supersede_fact(
                db, old_fact_id, by_fact_id=fid,
                reason="作者裁决：以正文为准（冲突 #%s）" % conflict_id,
                tick=_i(tick) if tick is not None else _current_tick(db, novel_id))
        # 补一条元层事件，让"正史被改"这件事在事件流里有来源
        eid = db.add_event(
            novel_id,
            title="正史修正：%s" % (statement or subject or "一条事实"),
            description=("作者裁决采纳正文。原正史：%s；正文：%s"
                         % (statement or "（无）", quote or "（无）")),
            chapter_ref=ch, event_type="canon_revision",
            involved_characters=[subject] if subject else [],
            importance=3,
            world_time=_clock_text(db, novel_id),
            structured={"source_kind": "user", "conflict_id": int(conflict_id),
                        "fact_id": fid, "superseded_fact_id": old_fact_id,
                        "action": "keep_prose"},
            tick=_i(tick) if tick is not None else _current_tick(db, novel_id),
            is_background=0)
        out["event_id"] = eid
        # 状态量若因此变了，让投影跟上（走唯一落库点）
        try:
            write_state_projection(db, novel_id)
        except Exception:                   # noqa: BLE001
            pass

    db.resolve_conflict(int(conflict_id), action, note=note)
    # 该章的未裁决数重算（徽标与 verified 由它统一维护）
    try:
        out["open_left"] = db.refresh_chapter_conflict_count(novel_id, ch)
    except Exception:                       # noqa: BLE001
        pass
    return out


def auto_resolve_conflicts(db, novel_id, chapter_number, action="ignored",
                           note="批量忽略"):
    """把某一章的未裁决冲突一次性处理掉（前端「全部忽略」用）。"""
    rows = db.list_conflicts(novel_id, status="open",
                             chapter_number=chapter_number)
    n = 0
    for r in rows:
        try:
            res = resolve_conflict(db, novel_id, r["id"], action, note=note)
            if res.get("ok"):
                n += 1
        except Exception:                   # noqa: BLE001
            continue
    return n


# ============================================================ 快照预留（约束 5）

def load_latest_snapshot(db, novel_id, at_tick, upto_event_id=None):
    """返回 at_tick 之前最近的快照。

    ⚠ **第一版永远返回 None**（约束 5：只定签名，不写实现）。
    P1 才把它改成真实查询——那时所有调用方**不需要改**，
    因为 `replay_from()` 的两条路径已经是共用代码。
    """
    return None


def save_snapshot(db, novel_id, upto_event_id, at_tick):
    """⚠ P1 才实现。第一版显式不写：调用即返回 False。"""
    return False


def invalidate_snapshots(db, novel_id, from_tick):
    """用户手工订正历史事实时，把 from_tick 之后的快照批量作废。

    ⚠ P1 才用得上（现在表是空的），但规则先定好：
    `supersede` 只影响 at_tick 之后的快照；手工订正（canon_revision + user）
    要把 `at_tick >= 修正点` 的全部置 is_valid=0。
    """
    try:
        return db.execute(
            "UPDATE world_snapshots SET is_valid=0 WHERE novel_id=? "
            "AND at_tick>=?", (novel_id, _i(from_tick)))
    except Exception:                       # noqa: BLE001
        return 0


def replay_from(db, novel_id, at_tick, upto_event_id=None):
    """从快照（若有）或从事件 1 开始 replay 到目标位置。

    第一版 `snapshot` 分支恒为 `None`，**两种情形的后续逻辑共用一条代码路径**
    —— 这是约束 5 的全部意义：日后启用快照时，不需要改任何调用方。
    """
    snap = load_latest_snapshot(db, novel_id, at_tick)      # 恒为 None
    start_event = _i((snap or {}).get("upto_event_id")) if snap else 0
    rows = db.query(
        "SELECT * FROM events WHERE novel_id=? AND deleted_at IS NULL "
        "AND canonical=1 AND id>? AND tick<=? ORDER BY id",
        (novel_id, start_event, _i(at_tick)))
    facts_seen = {}
    if snap:
        for f in ((snap.get("payload") or {}).get("facts") or []):
            facts_seen[fact_key(f)] = f
    for ev in rows:
        structured = ev.get("structured")
        if isinstance(structured, str):
            try:
                structured = json.loads(structured or "{}")
            except (ValueError, TypeError):
                structured = {}
        for ch in ((structured or {}).get("state_changes") or {}).get("world") or []:
            key = _s(ch.get("key"))
            if key:
                facts_seen[key] = {"object_text": ch.get("value")}
    return {"at_tick": _i(at_tick), "from_event": start_event,
            "events_replayed": len(rows), "state": facts_seen,
            "used_snapshot": bool(snap)}
