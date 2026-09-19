# -*- coding: utf-8 -*-
"""
状态应用器：把 LLM 产出的结构化事件流翻译成数据库写入。

## v6.7 的核心变化：把「写」拆成「算」+「落」

改造前所有函数都是**边算边写**：

    def apply_state_changes(db, novel_id, event, chapter_ref=0):
        for ch in changes.get("world") or []:
            db.set_state_value(...)        # ← 直接写
        for ch in changes.get("characters") or []:
            db.update_character(...)       # ← 直接写

这导致**沙盘没法用**：一旦要算"走这条分支会怎样"，就得真的写进去。
于是"探索分支不污染世界状态"根本做不到。

改造后拆成两层：

    project_event() / project_events()   纯函数：算「这条事件改变了什么」，返回
                                         projection dict，**一个字节都不写库**
    apply_projection()                   唯一写入口：把 projection 落库

沙盘 `tree.expand()` 可以放心调 `project_events()` 而不污染正史。

## 三条铁律

1. **`apply_projection()` 是全项目唯一允许调
   `db.set_state_value(writer="applier.apply_projection")` 的地方**
   （约束 4）。其余任何写入 world_state 的代码都会撞上 `PermissionError`。
   连"把事实投影成状态"的 `facts.write_state_projection()` 也要转调这里。

2. **先建档再落状态变更**。`project_event` 查不到人的话，位置/状态/记忆会被
   整条丢掉（世界推演里的人物就成了走马灯），所以 `apply_projection` 的
   第一步永远是建角色/实体档。

3. **事实走 `facts.add_fact()`，认知走 `db.record_knowledge()`。**
   本模块**不直接拼 facts 的 INSERT**——枚举收口、supersede、来源记录
   都在 `facts.py` 里统一做（两条写入链路共用同一套收口）。
"""
import json


def _norm_enum(value, allowed, default, aliases=None):
    v = str(value or "").strip().lower()
    if v in allowed:
        return v
    if aliases and v in aliases:
        return aliases[v]
    return default


def _s(value, default=""):
    if value is None:
        return default
    return str(value).strip()


def _normalize_value(value, value_type):
    """把 LLM 给的 value 归一到可存储的字符串。"""
    if value_type == "json":
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if value_type == "bool":
        if isinstance(value, bool):
            return "1" if value else "0"
        return "1" if str(value).lower() in ("1", "true", "yes", "是") else "0"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _guess_type(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, (dict, list)):
        return "json"
    return "text"


def _category_to_fact_type(category):
    """world_state 的 category → facts.fact_type。

    只让 `resource/progress/outcome` 三类参与 state projection，
    所以这里必须映射过去；一律 'other' 会让状态事实在重放时消失。
    """
    try:
        from engine import facts as _F
        return _F.CATEGORY_TO_FACT_TYPE.get(_s(category) or "other", "other")
    except Exception:                       # noqa: BLE001
        return {"tension": "outcome", "resource": "resource", "threat": "outcome",
                "progress": "progress", "relation": "relationship"}.get(
                    _s(category), "other")


# ============================================================ 兼容壳（P0）
#
# 旧名保留：调用方（world/tree/prose 的老路径）还没全切过来时不会当场炸。
# 内部已改为「算 → 落」两段。日志打的是 Python 的 warnings，
# 不是 print——避免污染 CLI 的正常输出。

def apply_state_changes(db, novel_id, event, chapter_ref=0):
    """【兼容壳】应用单个事件的 state_changes。

    ⚠ 已废弃：内部改为 `project_event()` + `apply_projection()`。
    新代码请直接用这两个函数（沙盘里只能用 `project_event`）。
    """
    tick = _current_tick(db, novel_id)
    proj = project_event(db, novel_id, event, tick=tick, chapter_ref=chapter_ref)
    return apply_projection(db, novel_id, proj, tick=tick,
                            chapter_ref=chapter_ref)


def apply_events(db, novel_id, result, chapter_ref=0, window=8,
                 apply_state=True, apply_threads_flag=True, max_new=2,
                 max_new_entities=8):
    """【兼容壳】把一次推演结果整体落库。

    ⚠ 已废弃：内部改为 `project_events()` + `apply_projection()`。
    返回值结构与旧版一致，老调用方（返回摘要渲染）无需改动。
    """
    tick = _current_tick(db, novel_id)
    proj = project_events(db, novel_id, result, tick=tick,
                          chapter_ref=chapter_ref, window=window,
                          apply_state=apply_state,
                          apply_threads=apply_threads_flag,
                          max_new=max_new, max_new_entities=max_new_entities)
    return apply_projection(db, novel_id, proj, tick=tick,
                            chapter_ref=chapter_ref)


def _current_tick(db, novel_id):
    from engine import ticktime
    return ticktime.current_tick(db, novel_id)


# ============================================================ 算：projection
#
# projection 是三个新函数的**统一契约**。形状见设计 §5.3。
# 关键点：`events` / `facts` / `state` / `characters` / `knowledge` /
# `beliefs` / `threads` / `relations` / `new_characters` / `new_entities`
# / `traits` 十个槽，`apply_projection` 按固定顺序落库。

def _empty_projection():
    return {
        "event_ids": [], "events": [],
        "facts": [], "state": [], "characters": [], "knowledge": [],
        "beliefs": [], "threads": [], "relations": [],
        "new_characters": [], "new_entities": [], "traits": [],
    }


def project_events(db, novel_id, result, tick=0, chapter_ref=0, window=8,
                   apply_state=True, apply_threads=True, max_new=2,
                   max_new_entities=8, is_background=False):
    """把一次推演结果整体算成 projection。**纯数据，不写库。**

    `is_background=True`（`world.pulse()` 的背景动向）→ 事件落库时标
    `is_background=1`，正文装配与状态投影都会跳过它们。
    """
    events = (result or {}).get("events") or []
    proj = _empty_projection()

    # 建档名单：先算出来，让 apply_projection 第一步就建好——
    # 否则后面 project_event 查不到人，状态/位置会被整条丢掉。
    proj["new_characters"] = collect_new_characters(
        db, novel_id, events, max_new=max_new)
    proj["new_entities"] = collect_new_entities(
        db, novel_id, events, max_new=max_new_entities)

    world_time = _s((result or {}).get("world_time"))
    time_elapsed = _s((result or {}).get("time_elapsed"))

    for ev in events:
        sub = project_event(db, novel_id, ev, tick=tick,
                            chapter_ref=chapter_ref, world_time=world_time,
                            time_elapsed=time_elapsed,
                            is_background=is_background,
                            apply_state=apply_state, apply_threads=apply_threads,
                            window=window)
        _merge_projection(proj, sub)

    proj["relations"] = list((result or {}).get("relation_changes") or [])
    return proj


def project_event(db, novel_id, event, tick=0, chapter_ref=0, world_time="",
                  time_elapsed="", is_background=False, apply_state=True,
                  apply_threads=True, window=8):
    """把**一个事件**算成 projection。纯函数（只读 db 查名字/id）。

    只读的理由：算"这条事件改变了什么"需要把角色名解析成 id、
    把位置文本解析成地点树节点——这些都是**查询**，不是写入。
    """
    ev = dict(event or {})
    proj = _empty_projection()

    wt = _s(world_time) or _s(ev.get("world_time"))
    if not wt:
        wt = (db.get_clock(novel_id) or {}).get("current_time") or ""

    structured = {
        "actor": ev.get("actor", ""),
        "action": ev.get("action", ""),
        "intent": ev.get("intent", ""),
        "result": ev.get("result", ""),
        "state_changes": ev.get("state_changes") or {},
        "is_gradual": bool(ev.get("is_gradual")),
        "time_elapsed": time_elapsed,
    }
    # 事件本体（含 eid=0：真正写入时 apply_projection 才拿到 id，
    # 并把它回填进各条 fact/knowledge 的 source_event_id）
    proj["events"].append({
        "_src": ev,
        "title": ev.get("title", ""),
        "description": ev.get("description", ""),
        "event_type": ev.get("event_type", "plot"),
        "involved_characters": ev.get("involved_characters") or [],
        "involved_entities": ev.get("involved_entities") or [],
        "importance": ev.get("importance") or 3,
        "world_time": wt,
        "intent": ev.get("intent", ""),
        "structured": structured,
        "open_threads": ev.get("open_threads") or [],
        "tick": tick,
        "is_background": 1 if is_background else 0,
    })

    if not apply_state:
        return proj

    changes = ev.get("state_changes") or {}

    # ---- ① 世界状态量 → facts（再投影成 world_state）
    for ch in (changes.get("world") or []):
        if not isinstance(ch, dict):
            continue
        key = _s(ch.get("key"))
        if not key:
            continue
        vt = ch.get("value_type") or _guess_type(ch.get("value"))
        val = ch.get("value")
        subj, _, pred = key.partition(".")
        category = _s(ch.get("category")) or "other"
        proj["facts"].append({
            "op": "add",
            "subject_type": "world" if not pred else "entity",
            "subject_id": None,
            "subject_name": subj,
            "predicate": pred or key,
            "object_text": val if vt == "json" else _normalize_value(val, vt),
            "object_type": vt,
            # 用 category 映射 fact_type，而不是一律 'other'：
            # state projection 只认 resource/progress/outcome 三类，
            # 全标 other 的话这条状态在**重放/快照时会被跳过**，
            # 于是"回看第 30 章的世界"就少了一堆数字。
            "fact_type": _category_to_fact_type(category),
            "fact_status": "canonical",
            "confidence": 5,
            "classification": "world",
            "source_kind": "sim",
            "source_event_id": 0,
            "tick": tick,
            "quote": _s(ch.get("reason")),
        })
        proj["state"].append({
            "key": key, "value": val, "value_type": vt,
            "category": category,
            "reason": _s(ch.get("reason")) or ("来自事件：%s" % ev.get("title", "")),
        })

    # ---- ② 角色状态/位置
    for ch in (changes.get("characters") or []):
        if not isinstance(ch, dict):
            continue
        name = _s(ch.get("name"))
        if not name:
            continue
        row = db.get_character_by_name(novel_id, name)
        cid = row["id"] if row else None

        item = {"name": name, "character_id": cid, "patch": {},
                "location": "", "location_id": None,
                "location_precision": "", "location_secret": False,
                "world_time": wt, "memory": _s(ch.get("memory"))}

        status = _s(ch.get("status"))
        if status in ("alive", "dead", "missing", "retired", "unknown"):
            item["patch"]["status"] = status
            if cid:
                proj["facts"].append({
                    "op": "add", "subject_type": "character",
                    "subject_id": cid, "subject_name": name,
                    "predicate": "存活", "object_text": status,
                    "object_type": "text", "fact_type": "status",
                    "fact_status": "canonical", "confidence": 5,
                    "classification": "world", "source_kind": "sim",
                    "source_event_id": 0, "tick": tick,
                })

        loc_text = _s(ch.get("location"))
        if loc_text:
            lid, path_text = _resolve_location(db, novel_id, loc_text,
                                               chapter_ref=chapter_ref)
            precision = _guess_precision(
                db, lid, path_text or loc_text,
                explicit=ch.get("location_precision"))
            secret = (bool(ch.get("location_secret"))
                      or _location_is_secret(db, lid))
            item.update({
                "location": path_text or loc_text, "location_id": lid,
                "location_precision": precision, "location_secret": secret})
            if cid:
                proj["facts"].append({
                    "op": "add", "subject_type": "character",
                    "subject_id": cid, "subject_name": name,
                    "predicate": "位于", "object_text": path_text or loc_text,
                    "object_type": "ref", "object_ref_id": lid,
                    "fact_type": "location", "fact_status": "canonical",
                    "confidence": 5, "classification": "world",
                    "source_kind": "sim", "source_event_id": 0, "tick": tick,
                })
                proj["beliefs"].append({
                    "character_id": cid, "name": name, "secret": secret,
                    "location_id": lid, "location_text": path_text or loc_text,
                    "precision": precision, "world_time": wt,
                    "location_hint": path_text or loc_text,
                })
        proj["characters"].append(item)

    # ---- ③ 认知（模型显式产出：know/believe/suspect/assume）
    proj["knowledge"].extend(
        project_knowledge(db, novel_id, ev, tick=tick, chapter_ref=chapter_ref))

    # ---- ④ 悬置线索
    if apply_threads:
        proj["threads"].extend(_project_threads(db, novel_id, ev, chapter_ref,
                                                window))
    return proj


def project_facts(db, novel_id, event, tick=0, chapter_ref=0):
    """单独取一个事件产生的事实候选（供诊断/预检复用）。纯数据。"""
    return project_event(db, novel_id, event, tick=tick,
                         chapter_ref=chapter_ref)["facts"]


# 抽取器 → projection 的字段白名单。正文回流用它收口：
# 模型给的自由文本字段**不许直连**带 CHECK 的列（本项目的铁律），
# 能进 projection 的字段必须在这里列清。
_EXTRACT_FACT_TYPES = ("identity", "status", "location", "possession",
                       "relationship", "ability", "injury", "resource",
                       "outcome", "other")
_EXTRACT_SUBJECT_TYPES = ("character", "entity", "world")
_EXTRACT_CLASSES = ("world", "cognition", "unverified", "dream", "memory",
                    "author_claim")
_EXTRACT_KNOW_TYPES = ("know", "believe", "suspect", "assume", "memory", "dream")
_EXTRACT_KNOW_SOURCES = ("witnessed", "told", "inferred", "assumed", "read",
                         "public", "dream", "recalled")


def projection_from_extract(db, novel_id, parsed, tick=0, chapter_ref=0):
    """把 `state_extract` 的 JSON **折成 projection**（纯数据，不写库）。

    这是 6.3.1 那条新链路的第一环：抽取 → 投影 → 分类 → 分流 → 比对 → 应用。
    之所以要这一步而不是像 v1.0 那样"抽到啥直接写啥"：

      · 投影是**可比的纯数据**，分类与冲突比对都只读它，不碰库；
      · 无冲突时整份投影一次性落库（一个 `apply_projection` 调用），
        有冲突时**一个字节都不落**——不会出现"改了一半的正史"。

    位置类事实的坐标**只在 `characters[]` 里给一次**：`apply_projection`
    第 6 步会顺带把 facts（位于）与认知（谁知道）一起同步。若这里再单独
    塞一条 location fact，同一次回流就会对同一主体写两遍，`uq_facts_active`
    会把第一条顶掉——来源就丢了。
    """
    proj = _empty_projection()
    parsed = parsed or {}
    _pid = novel_id

    def _resolve_subject(stype, name):
        """主语名 → id。返回 (subject_id, 收口后的 subject_type)。"""
        st = _norm_enum(stype, _EXTRACT_SUBJECT_TYPES, "character")
        if st == "character":
            row = db.get_character_by_name(_pid, name)
            return (row["id"] if row else None), st
        if st == "entity":
            row = db.find_entity(_pid, name)
            return (row["id"] if row else None), st
        return None, st

    # ---- ① 结构化事实（正文里新写定的事）
    for f in _as_dicts(parsed.get("facts")):
        subject = _s(f.get("subject"))
        predicate = _s(f.get("predicate"))
        if not (subject and predicate):
            continue
        stype = _norm_enum(f.get("subject_type"), _EXTRACT_SUBJECT_TYPES,
                           "character")
        sid, stype = _resolve_subject(stype, subject)
        item = {
            "op": "add",
            "subject_type": stype, "subject_id": sid, "subject_name": subject,
            "predicate": predicate,
            "object_text": _s(f.get("object")),
            "object_type": "text",
            "fact_type": _norm_enum(f.get("fact_type"), _EXTRACT_FACT_TYPES,
                                    "other"),
            "fact_status": "canonical",
            "confidence": _to_int_safe(f.get("confidence"), 5),
            # 模型先自标一遍；**它的标注不作数**，classify_projection 会重判
            "classification": _norm_enum(f.get("classification"),
                                         _EXTRACT_CLASSES, "world"),
            "source_kind": "prose",
            "source_event_id": None,
            "tick": int(tick or 0),
            "quote": _s(f.get("quote"))[:300],
            "provenance": _s(f.get("provenance"))[:120],
        }
        # cognition 类走"角色认知"出口，需要这些字段（6.3.1 路 A）
        if item["classification"] == "cognition":
            by = _s(f.get("belief_by")) or subject
            brow = db.get_character_by_name(_pid, by)
            item.update({
                "belief_by": by,
                "target_character_id": brow["id"] if brow else sid,
                "know_type": _norm_enum(f.get("know_type"),
                                        _EXTRACT_KNOW_TYPES, "believe"),
                "about_type": "fact",
                "about_id": None,
                "about_text": subject,
                "content": _s(f.get("object")) or predicate,
                "source": _norm_enum(f.get("know_source"),
                                     _EXTRACT_KNOW_SOURCES, "inferred"),
            })
        proj["facts"].append(item)

    # ---- ② world_state → 事实 + 状态投影
    for st in _as_dicts(parsed.get("world_state")):
        key = _s(st.get("key"))
        if not key:
            continue
        vt = _norm_enum(st.get("value_type"),
                        ("text", "int", "float", "bool", "json"), "") \
            or _guess_type(st.get("value"))
        val = st.get("value")
        subj, _, pred = key.partition(".")
        cat = _norm_enum(st.get("category"),
                         ("tension", "resource", "threat", "relation",
                          "progress", "other"), "other")
        proj["facts"].append({
            "op": "add",
            "subject_type": "world" if not pred else "entity",
            "subject_id": None, "subject_name": subj,
            "predicate": pred or key,
            "object_text": val if vt == "json" else _normalize_value(val, vt),
            "object_type": vt,
            "fact_type": _category_to_fact_type(cat),
            "fact_status": "canonical", "confidence": 5,
            "classification": "world", "source_kind": "prose",
            "source_event_id": None, "tick": int(tick or 0),
            "key": key,          # apply_projection 用它把 state 与 fact 对上
            "quote": _s(st.get("reason"))[:200],
            "provenance": "叙述者陈述",
        })
        proj["state"].append({
            "key": key, "value": val, "value_type": vt,
            "category": cat,
            "reason": _s(st.get("reason")) or ("第%s章正文回流" % chapter_ref),
        })

    # ---- ③ 角色状态 / 位置
    for c in _as_dicts(parsed.get("character_status")):
        name = _s(c.get("name"))
        if not name:
            continue
        row = db.get_character_by_name(_pid, name)
        cid = row["id"] if row else None
        patch = {}
        status = _s(c.get("status"))
        if status in ("alive", "dead", "missing", "retired", "unknown"):
            patch["status"] = status
            if cid:
                proj["facts"].append({
                    "op": "add", "subject_type": "character",
                    "subject_id": cid, "subject_name": name,
                    "predicate": "存活", "object_text": status,
                    "object_type": "text", "fact_type": "status",
                    "fact_status": "canonical", "confidence": 5,
                    "classification": "world", "source_kind": "prose",
                    "source_event_id": None, "tick": int(tick or 0),
                })
        loc_text = _s(c.get("location"))
        item = {"name": name, "character_id": cid, "patch": patch,
                "location": "", "location_id": None,
                "location_precision": "", "location_secret": False,
                "world_time": _clock_text(db, _pid), "memory": ""}
        if loc_text:
            # 与推演同一条链路（applier._resolve_location）：两处若各写一套，
            # 地图上就会长出"同一个地方两个节点"。
            lid, path_text = _resolve_location(db, _pid, loc_text,
                                               chapter_ref=chapter_ref)
            precision = _guess_precision(
                db, lid, path_text or loc_text,
                explicit=c.get("location_precision"))
            secret = (bool(c.get("location_secret"))
                      or _location_is_secret(db, lid))
            item.update({"location": path_text or loc_text, "location_id": lid,
                         "location_precision": precision,
                         "location_secret": secret})
            if cid:
                proj["facts"].append({
                    "op": "add", "subject_type": "character",
                    "subject_id": cid, "subject_name": name,
                    "predicate": "位于", "object_text": path_text or loc_text,
                    "object_type": "ref", "object_ref_id": lid,
                    "fact_type": "location", "fact_status": "canonical",
                    "confidence": 5, "classification": "world",
                    "source_kind": "prose", "source_event_id": None,
                    "tick": int(tick or 0),
                })
                if secret:
                    # 秘密移动 → 让 apply_projection 逐个观察者推"他还在老地方"
                    proj["beliefs"].append({
                        "character_id": cid, "name": name, "secret": True,
                        "location_id": lid,
                        "location_text": path_text or loc_text,
                        "precision": precision,
                        "world_time": item["world_time"],
                        "location_hint": path_text or loc_text,
                    })
        if patch or loc_text:
            proj["characters"].append(item)

    # ---- ④ 角色认知（两个来源：老字段 character_knowledge + 新字段 knowledge）
    for k in _as_dicts(parsed.get("knowledge")):
        name = _s(k.get("name"))
        content = _s(k.get("content"))
        if not (name and content):
            continue
        row = db.get_character_by_name(_pid, name)
        if not row:
            continue
        proj["knowledge"].append({
            "character_id": row["id"], "name": name,
            "know_type": _norm_enum(k.get("know_type"), _EXTRACT_KNOW_TYPES,
                                    "know"),
            "about_type": "fact",
            "about_id": None,
            "about_text": _s(k.get("about")),
            "content": content,
            "source": _norm_enum(k.get("source"), _EXTRACT_KNOW_SOURCES,
                                 "witnessed"),
            "confidence": 4,
            "learned_tick": int(tick or 0),
        })
    # 兼容老 schema：character_knowledge 只有 {name, knowledge}
    for k in _as_dicts(parsed.get("character_knowledge")):
        name = _s(k.get("name"))
        known = _s(k.get("knowledge"))
        if not (name and known):
            continue
        row = db.get_character_by_name(_pid, name)
        if not row:
            continue
        proj["knowledge"].append({
            "character_id": row["id"], "name": name,
            "know_type": "know", "about_type": "fact", "about_id": None,
            "about_text": "", "content": known, "source": "witnessed",
            "confidence": 4, "learned_tick": int(tick or 0),
            "also_memory": True,        # 老行为：同时进记忆流
        })

    # ---- ⑤ 线索
    for th in _as_dicts(parsed.get("new_threads")):
        title = _s(th.get("title"))
        if not title:
            continue
        proj["threads"].append({
            "title": title, "description": _s(th.get("description")),
            "tension": _to_int_safe(th.get("tension"), 3),
            "importance": "normal",
        })

    # ---- ⑥ 新出场角色
    for ch in _as_dicts(parsed.get("new_characters")):
        name = _s(ch.get("name"))
        if not name or db.get_character_by_name(_pid, name):
            continue
        if _s(ch.get("role_tag")) or _s(ch.get("first_impression")):
            proj["new_characters"].append(ch)
        else:
            proj["new_characters"].append({"name": name})

    # ---- ⑦ 已解决线索 / 模型自报的冲突（原样带着，由调用方处理）
    proj["resolved_threads"] = [kw for kw in (parsed.get("resolved_threads") or [])
                                if isinstance(kw, str) and kw.strip()]
    proj["model_conflicts"] = _as_dicts(parsed.get("conflicts"))
    return proj


def _as_dicts(rows):
    """把任意输入收成"只含 dict 的列表"（模型偶尔返回字符串数组）。"""
    out = []
    for r in (rows or []):
        if isinstance(r, dict):
            out.append(r)
        elif isinstance(r, str) and r.strip():
            out.append({"title": r.strip()})
    return out


def _to_int_safe(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _clock_text(db, novel_id):
    try:
        return (db.get_clock(novel_id) or {}).get("current_time") or ""
    except Exception:                       # noqa: BLE001
        return ""


def project_knowledge(db, novel_id, event, tick=0, chapter_ref=0):
    """谁在场 → 谁 KNOW 了；不在场 → 只能 BELIEVE/SUSPECT。

    准则是**信息边界**：角色只能知道"他目睹的"或"别人告诉他的"。
    没有这两条，他连"怀疑"都不该有（怀疑也需要线索）。

    两段来源：
      1. Python 推的：本事件在场者 → `know`（他确实在场，这不需要模型告诉）
      2. 模型给的 `knowledge_changes`：BELIEVE/SUSPECT/ASSUME
         **Python 只做校验，不替模型编认知**——
         不在场却写成 `know` 的，降级为 `believe`（他不可能知道）。
    """
    out = []
    actor = _s(event.get("actor"))
    present = set()
    if actor:
        present.add(actor)
    for n in (event.get("involved_characters") or []):
        n = _s(n)
        if n:
            present.add(n)
    for ch in ((event.get("state_changes") or {}).get("characters") or []):
        n = _s((ch or {}).get("name"))
        if n:
            present.add(n)

    title = _s(event.get("title"))
    summary = "%s：%s" % (title, _s(event.get("result")))

    # ---- ① 在场者 → know（信息边界的下限）
    for name in present:
        row = db.get_character_by_name(novel_id, name)
        if not row:
            continue
        out.append({
            "character_id": row["id"], "character_name": name,
            "know_type": "know", "about_type": "event",
            "about_text": title, "content": summary[:120],
            "source": "witnessed", "confidence": 5,
            "learned_tick": tick, "learned_chapter": chapter_ref,
            "classification": "cognition",
        })

    # ---- ② 模型显式给的认知变化
    for k in (event.get("knowledge_changes") or []):
        if isinstance(k, str):
            k = {"name": k, "know_type": "know", "content": k}
        name = _s(k.get("name"))
        content = _s(k.get("content"))
        if not (name and content):
            continue
        row = db.get_character_by_name(novel_id, name)
        if not row:
            continue
        ktype = _norm_enum(k.get("know_type"),
                           ("know", "believe", "suspect", "assume",
                            "memory", "dream"), "believe")
        source = _norm_enum(k.get("source"),
                            ("witnessed", "told", "inferred", "assumed",
                             "read", "public", "dream", "recalled"), "")
        if ktype == "know" and name not in present:
            # 他不在场，也没人说他被告知 → 不可能"知道"。
            # 降级而不是丢弃：这条信息仍是剧情（他多半是听说的）。
            ktype = "believe"
            if not source:
                source = "told"
        if not source:
            source = {"know": "witnessed", "believe": "told",
                      "suspect": "inferred", "assume": "assumed",
                      "memory": "recalled", "dream": "dream"}.get(ktype, "told")
        out.append({
            "character_id": row["id"], "character_name": name,
            "know_type": ktype,
            "about_type": _norm_enum(
                k.get("about_type"),
                ("character", "entity", "event", "location", "fact", "world"),
                "fact"),
            "about_text": _s(k.get("about")) or title,
            "content": content[:200], "source": source,
            "confidence": k.get("confidence") or 3,
            "learned_tick": tick, "learned_chapter": chapter_ref,
            "classification": "cognition",
        })
    return out


def project_beliefs(db, novel_id, event, tick=0, chapter_ref=0):
    """位置认知（knowledge 的 location 子类，落库时会双写 location_beliefs）。"""
    out = []
    for k in ((event.get("state_changes") or {}).get("characters") or []):
        name = _s((k or {}).get("name"))
        if not name:
            continue
        row = db.get_character_by_name(novel_id, name)
        if not row:
            continue
        for b in (k.get("beliefs") or []):
            observer = _s((b or {}).get("observer"))
            if not observer:
                continue
            orow = db.get_character_by_name(novel_id, observer)
            if not orow:
                continue
            out.append({
                "character_id": row["id"], "observer_id": orow["id"],
                "believed_location_id": b.get("location_id"),
                "believed_text": _s(b.get("text")) or "行踪不明",
                "precision": _s(b.get("precision")) or "unknown",
                "chapter_ref": chapter_ref,
            })
    return out


def _project_threads(db, novel_id, event, chapter_ref, window):
    """把 open_threads 算成待落库的线索条目。"""
    out = []
    for th in (event.get("open_threads") or []):
        if isinstance(th, str):
            th = {"title": th}
        title = _s(th.get("title"))
        if not title:
            continue
        out.append({
            "title": title, "description": th.get("description") or "",
            "window": th.get("window") or window,
            "tension": th.get("tension") or 3,
            "importance": th.get("importance") or "normal",
        })
    return out


def _merge_projection(dst, src):
    for k, v in (src or {}).items():
        if k in ("event_ids",) or v is None:
            continue
        if isinstance(v, list):
            dst.setdefault(k, []).extend(v)
    return dst


# ============================================================ 落：唯一写入口

def apply_projection(db, novel_id, projection, tick=0, chapter_ref=0,
                     branch_id=0, is_background=False, link_chapter=True):
    """把 projection 落库。**全项目唯一允许写 world_state 的地方。**

    写入顺序**不能改**（顺序错了会出半截数据）：

        1. 新角色建档          先建档——否则后面查不到人，状态全丢
        2. 新实体建档
        3. 事件落库            canonical=1 immutable=1；拿到 id 回填 source_event_id
        4. 事实落库            facts.add_fact（内部自动 supersede 旧条）
        5. 状态投影            set_state_value(writer=...) ← 约束 4 的白名单
        6. 角色状态/位置
        7. 认知落库            db.record_knowledge（内部双写 location_beliefs）
        8. 线索
        9. 性格 tracker
       10. 关系                必须最后——要等新角色建好档

    `link_chapter`：正文路径落库时要建 chapter_event_links；沙盘落定由
    `tree.commit()` 自己指定章号，这里可以关掉。
    """
    from engine import facts as F
    from engine import guard

    projection = projection or {}
    summary = {"event_ids": [], "facts": 0, "state": 0, "state_skipped": 0,
               "knowledge": 0, "beliefs": 0, "threads": 0, "relations": 0,
               "characters": 0, "new_characters": [], "new_entities": [],
               "traits": [], "conflicts": []}

    with db.tx():
        # ---- 1. 新角色建档
        # ⚠ `new_characters` 里装的**是 dict**（`collect_new_characters` 产出的
        #   `{"name":..., "role_tag":..., "first_impression":...}`），不是名字字符串。
        #   旧代码写成 `[{"name": name}]`，于是 dict 被整个塞进 name 字段，
        #   落库后 `characters.name` 变成 "{'name': '林母', 'role_tag': ...}"
        #   ——2026-09-19 实测小说 78 的 110 号角色就是这么坏的（role_tag /
        #   first_impression 也一起丢了）。
        #   这里两种形态都收：dict 直接用，裸字符串补成 {"name": s}。
        new_raw = projection.get("new_characters") or []
        norm_new = []
        for item in new_raw:
            if isinstance(item, dict):
                nm = _s(item.get("name"))
                if nm:
                    norm_new.append(item)
            elif _s(item):
                norm_new.append({"name": _s(item)})
        for item in norm_new:
            ids = apply_new_characters(db, novel_id, [item],
                                       chapter_ref=chapter_ref, max_new=1)
            summary["new_characters"].extend(ids)

        # ---- 2. 新实体建档
        ents = [e for e in (projection.get("new_entities") or [])
                if isinstance(e, dict)]
        if ents:
            summary["new_entities"] = apply_new_entities(
                db, novel_id, ents, chapter_ref=chapter_ref)

        # ---- 3. 事件落库（拿 id 回填）
        ev_ids = []
        for item in (projection.get("events") or []):
            bg = int(item.get("is_background") or (1 if is_background else 0))
            eid = db.add_event(
                novel_id,
                title=item.get("title", ""),
                description=item.get("description", ""),
                chapter_ref=chapter_ref,
                event_type=item.get("event_type", "plot"),
                involved_characters=item.get("involved_characters") or [],
                involved_entities=item.get("involved_entities") or [],
                importance=item.get("importance") or 3,
                world_time=item.get("world_time", ""),
                intent=item.get("intent", ""),
                structured=item.get("structured") or {},
                open_threads=item.get("open_threads") or [],
            )
            ev_ids.append(eid)
            if bg:
                try:
                    db.execute("UPDATE events SET is_background=1, "
                               "canonical=1, immutable=1, branch_id=?, tick=? "
                               "WHERE id=?",
                               (int(branch_id or 0), int(tick or 0), eid))
                except Exception:           # noqa: BLE001
                    pass
            else:
                try:
                    db.execute("UPDATE events SET canonical=1, immutable=1, "
                               "branch_id=?, tick=? WHERE id=?",
                               (int(branch_id or 0), int(tick or 0), eid))
                except Exception:           # noqa: BLE001
                    pass
            # 回填：每条 fact/knowledge 的 source_event_id 认领这个事件
            for bucket in ("facts", "knowledge"):
                for row in (projection.get(bucket) or []):
                    if row.get("source_event_id") in (0, None):
                        row["source_event_id"] = eid
        summary["event_ids"] = ev_ids
        first_eid = ev_ids[0] if ev_ids else None

        # ---- 4. 事实落库（唯一入口是 facts.add_fact）
        key_to_fact = {}
        for f in (projection.get("facts") or []):
            if _s(f.get("op")) == "supersede":
                old = F.find_fact(
                    db, novel_id, _s(f.get("subject_type")) or "world",
                    f.get("subject_id"), _s(f.get("predicate")),
                    subject_name=_s(f.get("subject_name")))
                if old:
                    F.supersede_fact(db, old["id"], by_fact_id=None,
                                     reason=_s(f.get("reason")),
                                     tick=int(tick or 0))
                continue
            fid = F.add_fact(
                db, novel_id,
                subject_type=f.get("subject_type") or "world",
                subject_id=f.get("subject_id"),
                subject_name=f.get("subject_name", ""),
                predicate=f.get("predicate", ""),
                object_text=f.get("object_text", ""),
                object_type=f.get("object_type") or "text",
                object_ref_id=f.get("object_ref_id"),
                fact_type=f.get("fact_type") or "other",
                tick=int(f.get("tick") or tick or 0),
                source_event_id=f.get("source_event_id") or first_eid,
                source_chapter=chapter_ref,
                source_kind=f.get("source_kind") or "sim",
                fact_status=f.get("fact_status") or "canonical",
                confidence=f.get("confidence") or 5,
                classified_by=f.get("classified_by") or "sim",
                note=f.get("note", ""), quote=f.get("quote", ""))
            if fid:
                summary["facts"] += 1
                if f.get("key"):
                    key_to_fact[_s(f["key"])] = fid
                elif f.get("subject_name") and f.get("predicate"):
                    key_to_fact["%s.%s" % (f["subject_name"],
                                           f["predicate"])] = fid

        # ---- 5. 状态投影（本项目唯一有权调 set_state_value 的位置）
        for st in (projection.get("state") or []):
            key = _s((st or {}).get("key"))
            if not key:
                continue
            vt = st.get("value_type") or _guess_type(st.get("value"))
            val = st.get("value")
            fid = st.get("fact_id") or key_to_fact.get(key) or 0
            before = db.one("SELECT value FROM world_state WHERE novel_id=? "
                            "AND key=?", (novel_id, key))
            try:
                db.set_state_value(
                    novel_id, key,
                    val if vt == "json" else _normalize_value(val, vt),
                    value_type=vt, category=st.get("category") or "other",
                    note=st.get("reason") or "", chapter_ref=chapter_ref,
                    log_reason=st.get("reason") or "由正史事实投影",
                    writer="applier.apply_projection",
                    derived_from_fact_id=fid, projected_at_tick=int(tick or 0),
                    event_id=first_eid, fact_id=fid, tick=int(tick or 0))
            except PermissionError:         # 不该发生；发生即代码腐化
                raise
            after = db.one("SELECT value FROM world_state WHERE novel_id=? "
                           "AND key=?", (novel_id, key))
            if before and before.get("value") == (after or {}).get("value"):
                summary["state_skipped"] += 1
            else:
                summary["state"] += 1

        # ---- 6. 角色状态 / 位置
        for ch in (projection.get("characters") or []):
            cid = ch.get("character_id")
            if not cid:
                row = db.get_character_by_name(novel_id, _s(ch.get("name")))
                cid = row["id"] if row else None
                ch["character_id"] = cid
            if not cid:
                continue
            patch = dict(ch.get("patch") or {})
            if patch:
                db.update_character(cid, **patch)
            if ch.get("location"):
                db.update_character_location(
                    cid, location_id=ch.get("location_id"),
                    location_text=ch.get("location"),
                    precision=ch.get("location_precision"),
                    world_time=ch.get("world_time") or "",
                    chapter_ref=chapter_ref, event_id=first_eid,
                    is_secret=bool(ch.get("location_secret")))
                # 事实写完接着同步"谁知道"（秘密行踪才写认知）
                sync_beliefs_after_move(
                    db, novel_id, cid, _s(ch.get("name")),
                    bool(ch.get("location_secret")), chapter_ref=chapter_ref,
                    world_time=ch.get("world_time") or "",
                    location_hint=ch.get("location") or "",
                    write_beliefs=False)
            if ch.get("memory"):
                db.append_character_memory(cid, {
                    "chapter": chapter_ref, "text": ch["memory"],
                    "kind": "event", "tick": int(tick or 0)})
            summary["characters"] += 1

        # ---- 7. 认知落库
        for k in (projection.get("knowledge") or []):
            cid = k.get("character_id")
            if not cid or not _s(k.get("content")):
                continue
            kid = db.record_knowledge(
                novel_id, cid, k["content"],
                know_type=k.get("know_type") or "know",
                about_type=k.get("about_type") or "fact",
                about_id=k.get("about_id"),
                about_text=k.get("about_text", ""),
                source=k.get("source") or "witnessed",
                confidence=k.get("confidence") or 3,
                learned_event_id=k.get("source_event_id") or first_eid,
                learned_chapter=chapter_ref,
                learned_tick=int(k.get("learned_tick") or tick or 0))
            if kid:
                summary["knowledge"] += 1
            # 兼容老行为：正文回流抽到的"学到的事"同时进角色记忆流
            # （记忆流是给提示词用的紧凑摘要，与认知表职责不同，不重复）
            if k.get("also_memory") and _s(k.get("content")):
                db.append_character_memory(cid, {
                    "chapter": chapter_ref, "text": _s(k.get("content")),
                    "kind": "learned", "tick": int(tick or 0)})

        # ---- 7b. 位置认知（双写 knowledge + location_beliefs）
        #
        # 两种输入形状都要收：
        #   a) 带 observer_id —— "观察者 O 以为目标在 X"（project_beliefs 产出）
        #   b) 只带 character_id —— "某人偷偷移动了"，需要按观察者逐个推
        #      （project_event 产出）。**不能跳过它**：跳过就等于"秘密行踪"
        #      这件事从认知层凭空消失，第 20 章推演会以为谁都知道他在哪。
        for b in (projection.get("beliefs") or []):
            if b.get("observer_id"):
                db.set_location_belief(
                    novel_id, b["character_id"], b["observer_id"],
                    believed_location_id=b.get("believed_location_id"),
                    believed_text=b.get("believed_text") or "行踪不明",
                    precision=b.get("precision") or "unknown",
                    known_since=b.get("known_since") or "",
                    chapter_ref=chapter_ref)
                summary["beliefs"] += 1
                continue
            if b.get("character_id") and b.get("secret"):
                # 秘密移动：把"他还在老地方"的旧认知推给不知情的人。
                summary["beliefs"] += _belief_for_unaware(
                    db, novel_id, b["character_id"], _s(b.get("name")),
                    chapter_ref, b.get("world_time") or "",
                    b.get("location_hint") or b.get("location_text") or "")

        # ---- 8. 线索
        for th in (projection.get("threads") or []):
            n = apply_threads(db, novel_id, {"open_threads": [th]}, chapter_ref)
            summary["threads"] += len(n)

        # ---- 9. 性格 tracker
        for item in (projection.get("events") or []):
            summary["traits"].extend(guard.apply_trait_deltas(
                db, novel_id, item.get("_src") or item, threshold=30,
                chapter_ref=chapter_ref))

        # ---- 10. 关系（必须最后：要等新角色建好档）
        rel = projection.get("relations") or []
        if rel:
            summary["relations"] = apply_relations(db, novel_id, rel,
                                                   chapter_ref=chapter_ref)

        # ---- 章↔事件关联
        if link_chapter and chapter_ref and ev_ids:
            try:
                db.link_chapter_events(novel_id, chapter_ref, ev_ids,
                                       link_type="source")
            except Exception:               # noqa: BLE001
                pass
    return summary


def apply_soft_projection(db, novel_id, projection, tick=0, chapter_ref=0):
    """正文回流专用：**无冲突才应用**。

    内部转调 `apply_projection`——自己不写 `set_state_value`，
    否则约束 4 的白名单就白做了。
    """
    return apply_projection(db, novel_id, projection, tick=tick,
                            chapter_ref=chapter_ref)


# ============================================================ 分类（约束 2）

# Python 规则先判（免费、确定、可解释）；LLM 只兜底判不了的。
_PROVENANCE_RULES = (
    ("memory", ("想起", "记得", "回忆", "当年", "记得那时", "记起", "回想",
                "往事", "曾经")),
    ("dream", ("梦", "梦见", "梦中", "梦境", "幻觉")),
    ("author_claim", ("据说", "听说", "传闻", "他说", "她声称", "他声称",
                      "声称", "据传", "有人说", "据称")),
    ("cognition", ("他认为", "她觉得", "他觉得", "他怀疑", "他以为",
                   "她认为", "她怀疑", "她以为", "怀疑", "以为", "猜测",
                   "推测", "判断是")),
)

# 过去时表述 + 无事件来源 → 多半是回忆（"三年前杀过一个人"）
_PAST_MARKERS = ("曾", "曾经", "过", "当年", "那时", "多年前", "三年前",
                 "很久以前", "小时候")


def classify_projection(db, novel_id, projection, chapter_ref=0,
                        use_llm=True):
    """给 projection 里每条候选事实打 `classification`。**就地修改并返回。**

    三分流（约束 2 的核心）：

        cognition / memory / dream      → 不进 facts，走别的出口
        unverified / author_claim       → fact_candidates(pending)
        world                           → 才走冲突比对

    为什么必须有这一步：「**"无冲突"不等于"是真的"**，只说明没跟已知的打架」。
    正文写「李四想起三年前杀过人」，正史从没记过 → 永远"无冲突" → 直接入库。
    """
    facts_list = [f for f in (projection.get("facts") or [])
                  if isinstance(f, dict)]
    uncertain_idx = []
    for i, f in enumerate(facts_list):
        c, reason = _classify_by_rules(f)
        f["classification"] = c
        f["classify_reason"] = reason
        f["classified_by"] = "python"
        if c == "world" and not reason:
            uncertain_idx.append(i)

    if use_llm and uncertain_idx:
        llm_map = _classify_by_llm(db, novel_id, facts_list, uncertain_idx)
        for i, (c, reason) in llm_map.items():
            facts_list[i]["classification"] = c
            facts_list[i]["classify_reason"] = reason
            facts_list[i]["classified_by"] = "llm"
    return projection


def _classify_by_rules(f):
    """第一级：纯字符串/时态判断。命中即返回 (分类, 理由)。"""
    prov = _s(f.get("provenance"))
    quote = _s(f.get("quote"))
    blob = "%s %s" % (prov, quote)
    for cls, kws in _PROVENANCE_RULES:
        for kw in kws:
            if kw in blob:
                return cls, "来源表述含「%s」" % kw
    # 抽取器直接标了"谁相信" → 认知，且对象已知
    if _s(f.get("belief_by")):
        return "cognition", "标注了相信者「%s」" % _s(f.get("belief_by"))
    # 过去时 + 无事件来源 → 回忆（这是最危险的一类：
    # 正史从没记过，比对永远"无冲突"，v1.0 会直接入库）
    pred = _s(f.get("predicate"))
    if (any(m in pred for m in _PAST_MARKERS)
            and not f.get("source_event_id")):
        return "memory", "过去时表述且无事件来源"
    if f.get("source_event_id"):
        return "world", ""
    # 判不了：交给第二级
    return "world", ""


_CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array", "maxItems": 12,
            "items": {
                "type": "object",
                "properties": {
                    "idx": {"type": "integer"},
                    "classification": {
                        "enum": ["world", "cognition", "unverified",
                                 "dream", "memory", "author_claim"]},
                    "reason": {"type": "string", "maxLength": 40},
                },
                "required": ["idx", "classification", "reason"],
            },
        }
    },
}

_CLASSIFY_SYSTEM = """你在判断一句话属于哪一类。只输出结论，不要解释过程。

- world        ：世界里真实发生/成立的事（有事件来源的此刻变化）
- cognition    ：某个角色认为/怀疑/以为的事（认知，未必为真）
- memory       ：角色回忆的往事（可能记错，正史未必记过）
- dream        ：梦里/幻觉里的事（没发生过）
- author_claim ：转述、传闻、某人声称（真假未定）
- unverified   ：说不清，先挂起

判据只有一条：**这件事是"世界真的发生了"，还是"某个人脑子里的东西"？**"""

# 三条纪律（决定它会不会变成第二个"诊断超预算"的坑）：
#   1. reason 限 40 字——不限就会写小作文
#   2. 只送存疑条目、最多 12 条——第一级能判的绝不浪费一次调用
#   3. 模型只允许在 6 个值里选，不允许自由文本
_CLASSIFY_BUDGET = 1024
# 纪律 2：只送存疑条目，最多 12 条。第一级能判的绝不浪费一次调用。
_CLASSIFY_MAX_ITEMS = 12


def _classify_by_llm(db, novel_id, facts_list, idx_list):
    """第二级：LLM 兜底。**失败一律退回 world**（保持系统可用）。"""
    try:
        from llm import router
        ok, _why = router.is_ready("conflict_check")
        if not ok:
            return {}
        picked = idx_list[:_CLASSIFY_MAX_ITEMS]
        lines = []
        for n, i in enumerate(picked):
            f = facts_list[i]
            lines.append("%d. %s %s：%s（来源：%s）" % (
                n, _s(f.get("subject_name")), _s(f.get("predicate")),
                _s(f.get("object_text")), _s(f.get("provenance")) or "未标注"))
        parsed, _raw, _mode = router.run_json(
            "conflict_check", schema=_CLASSIFY_SCHEMA,
            system=_CLASSIFY_SYSTEM,
            user="判断下面每条属于哪一类：\n" + "\n".join(lines),
            max_tokens=_CLASSIFY_BUDGET)
        out = {}
        for item in ((parsed or {}).get("items") or []):
            try:
                n = int(item.get("idx"))
            except (TypeError, ValueError):
                continue
            if 0 <= n < len(picked):
                out[picked[n]] = (_norm_enum(
                    item.get("classification"),
                    ("world", "cognition", "unverified", "dream", "memory",
                     "author_claim"), "world"),
                    _s(item.get("reason"))[:40])
        return out
    except Exception:                       # noqa: BLE001
        return {}


def dispatch_by_classification(db, novel_id, projection, chapter_ref=0):
    """按分类把条目分到三个出口。返回 `(to_facts, to_knowledge, to_candidates)`。

    - `world`                          → facts（走冲突比对）
    - `cognition`                      → character_knowledge（绕过比对）
    - `memory/dream/unverified/author_claim` → fact_candidates(pending)
    """
    to_facts, to_knowledge, to_candidates = [], [], []
    for f in (projection.get("facts") or []):
        cls = _s(f.get("classification") or "world")
        if cls == "world":
            to_facts.append(f)
        elif cls == "cognition":
            to_knowledge.append(f)
        else:
            to_candidates.append(f)
    return to_facts, to_knowledge, to_candidates


def store_candidates(db, novel_id, items, chapter_ref=0, source_kind="prose"):
    """把"定不了性"的条目挂到 `fact_candidates`（等用户裁决）。"""
    n = 0
    for f in (items or []):
        try:
            db.add_fact_candidate(
                novel_id,
                subject_name=_s(f.get("subject_name")),
                predicate=_s(f.get("predicate")),
                object_text=_s(f.get("object_text")),
                fact_type=f.get("fact_type") or "other",
                classification=_s(f.get("classification")) or "unverified",
                classified_by="llm" if _s(f.get("classified_by")) == "llm"
                else "python",
                classify_reason=_s(f.get("classify_reason")),
                confidence=f.get("confidence") or 3,
                quote=_s(f.get("quote")),
                provenance=_s(f.get("provenance")),
                source_kind=source_kind, source_chapter=chapter_ref,
                source_event_id=f.get("source_event_id"),
                subject_type=f.get("subject_type") or "character",
                subject_id=f.get("subject_id"),
                target_character_id=f.get("target_character_id"))
            n += 1
        except Exception:                   # noqa: BLE001
            continue
    return n


# ============================================================ 冲突比对

def check_projection_conflicts(db, novel_id, projection, chapter_number=0):
    """把"正文抽出来的事实"与"正史事实"逐条比。**大部分冲突不需要 LLM。**

    只比 `classification='world'` 的条目——cognition 类根本不进 facts，
    谈不上冲突（这正是约束 1"严格分离"带来的简化）。
    """
    from engine import facts as F
    return F.check_projection_conflicts(db, novel_id, projection,
                                        chapter_number=chapter_number)


# ============================================================ 地点解析

def _resolve_location(db, novel_id, text, chapter_ref=0):
    """把自由文本位置解析成地点树节点。解析不动就降级为 (None, 原文)。

    降级是**刻意**的：模型偶尔会写"某处""远方"这种根本不成树的位置，
    让整轮推演因此报错是不划算的——地图上少一个人，总好过世界崩掉。
    """
    try:
        lid, path_text = db.ensure_location_path(novel_id, text,
                                                chapter_ref=chapter_ref)
        return lid, path_text
    except Exception:                       # noqa: BLE001
        return None, str(text or "").strip()


def _guess_precision(db, location_id, text, explicit=None):
    """位置精度：剧情演到哪一级就记到哪一级。

    优先用模型显式声明的；没声明就按地点树节点的实际层级推断，
    再退回按文本猜。精度决定地图上这个点画在"城市"还是"某间屋"。
    """
    from core.database import _LOCATION_PRECISIONS, _guess_location_level
    if explicit and str(explicit) in _LOCATION_PRECISIONS:
        return str(explicit)
    if location_id:
        node = db.get_entity(location_id)
        level = (node or {}).get("location_level") or ""
        if level and level != "other":
            return level
    return _guess_location_level(str(text or ""), last=True)


def _location_is_secret(db, location_id):
    if not location_id:
        return False
    node = db.get_entity(location_id)
    return bool((node or {}).get("is_secret"))


# ---------------------------------------------------------------- 谁知道他在哪（v6.4）
#
# 用户的要求："别人都不知道这个人在什么具体位置，且这个位置可以更模糊。"
#
# 落地的关键是**事实与认知分开**：
#   characters.current_location / location_id = 事实（上帝视角，画他真在哪）
#   character_knowledge (about_type='location') = 某人以为他在哪
#   location_beliefs                          = 同一份认知的旧表（双写，兼容）
#
# 早先只有事实层，于是模型会理直气壮地让老周径直奔向封锁库去找林默——
# 可老周压根不该知道林默下了封锁库。混成一个字段，信息不对称必然破功。
#
# 只记**秘密行踪**（用户选定）：位置本身不隐秘时没必要存 O(n²) 的账——
# 公开场合所有人默认都知道。所以这里只在 is_secret 时写认知。

_KNOW_LOCATION_DEFAULT = "行踪不明"


def _belief_for_unaware(db, novel_id, char_id, char_name, chapter_ref,
                        world_time="", location_hint=""):
    """把一个"偷偷溜走"的人的认知，按现有认知逐条推给不知情的观察者。

    推法很简单，但顺序很重要：
      1. 谁**本来**以为他在哪（已有认知）—— 保持原样，别覆盖。
      2. 没有认知记录的人，退到"上一次公开露面的位置"（location_history 里
         最后一条非 secret 的记录）。
      3. 再没有，就是"行踪不明"（precision=unknown）。
    这就是"位置可以更模糊"：知道得越少，给得越粗。

    第 3 步**绝不能**拿新位置当兜底。踩过：林默刚偷偷下到封锁库，
    他没有公开露面记录，于是兜底写成了"老周以为林默在封锁库"——
    秘密行踪当场泄漏成公开信息，这个字段就白做了。
    location_hint 只用来做**粗化**参考（取它的父级），不是直接照抄。

    v6.7：**双写** character_knowledge（新主表）+ location_beliefs（旧表）。
    """
    known = {}
    for b in db.list_location_beliefs(novel_id, character_id=char_id):
        known[b["observer_id"]] = b
    for k in db.list_knowledge(novel_id, know_type="believe",
                               about_type="location"):
        pass                                    # 认知表只读校验，不重建已知集

    hist = db.list_location_history(novel_id, character_id=char_id, limit=20)
    last_public = None
    for h in hist:
        if not h.get("is_secret"):
            last_public = h
            break

    written = 0
    for o in db.list_characters(novel_id):
        oid = o["id"]
        if oid == char_id or oid in known:
            continue
        if last_public:
            text = last_public.get("location_text") or ""
            lid = last_public.get("location_id")
            prec = last_public.get("precision") or "unknown"
            since = last_public.get("world_time") or world_time
        else:
            # 从没人见过他在哪：对观察者来说这人的位置就是"不知道"。
            # **不要**把新位置写进去——那是秘密行踪本身，写上就等于广播。
            coarse, cid_ = _coarsen(db, location_hint)
            text = coarse or _KNOW_LOCATION_DEFAULT
            lid = cid_
            prec = "building" if cid_ else "unknown"
            since = world_time
        db.set_location_belief(
            novel_id, char_id, oid, believed_location_id=lid,
            believed_text=text, precision=prec, known_since=since,
            chapter_ref=chapter_ref)
        _record_location_belief_knowledge(
            db, novel_id, oid, char_name, text, chapter_ref, since)
        written += 1
    return written


def _record_location_belief_knowledge(db, novel_id, observer_id, target_name,
                                      text, chapter_ref, since=""):
    """把位置认知也写进 character_knowledge（约束 1：认知只在这一张表）。"""
    try:
        db.record_knowledge(
            novel_id, observer_id,
            "%s 在「%s」" % (target_name or "某人", text or _KNOW_LOCATION_DEFAULT),
            know_type="believe", about_type="location",
            about_text=target_name, source="told",
            confidence=2, learned_chapter=chapter_ref,
            learned_tick=0)
    except Exception:                       # noqa: BLE001
        pass


def _coarsen(db, location_text, sep=" · "):
    """把精确位置粗化到上一层（拿掉最后一段），返回 (文本, 节点id)。

    "调查局本部 · 地下二层 · 封锁库" → "调查局本部 · 地下二层"。
    这是给"不知道具体在哪、但知道人在哪个范围"的观察者的说法。
    """
    parts = [p for p in str(location_text or "").split(sep) if p.strip()]
    if len(parts) < 2:
        return "", None
    return sep.join(parts[:-1]), None


def _reveal_location(db, novel_id, char_id, chapter_ref=0):
    """目标公开露面：所有"以为他在别处"的旧认知作废，大家重新跟他同步。

    不标记 stale 的话，地图会一直挂着"老周以为林默在档案室"这种陈年错账，
    越堆越多，最后没人敢信。

    v6.7：同步把 character_knowledge 里对应的 believe 置为 refuted。
    """
    stale = db.mark_beliefs_stale(novel_id, char_id)
    # 同步作废 character_knowledge 里"他以为 XX 在哪"的旧认知。
    # 这里按 about_text（人名）匹配而不是拼 id 进 content——
    # 认知内容里没有 id，拿 id 去 LIKE 永远匹配不上（写了等于没写）。
    try:
        row = db.one("SELECT name FROM characters WHERE id=?", (int(char_id),))
        target = _s((row or {}).get("name"))
        if target:
            for k in db.list_knowledge(novel_id, know_type="believe",
                                       about_type="location"):
                if _s(k.get("about_text")) == target:
                    db.refute_knowledge(k["id"], note="目标公开露面，认知失效")
    except Exception:                       # noqa: BLE001
        pass
    return stale


def sync_beliefs_after_move(db, novel_id, char_id, char_name, was_secret,
                            chapter_ref=0, world_time="", location_hint="",
                            write_beliefs=True):
    """一次位置变更之后同步认知层。返回写了几条。

    `write_beliefs=False`：由 `apply_projection` 调用时用——
    那时 location_beliefs 已由 projection["beliefs"] 统一落库，
    这里只做"公开露面 → 旧认知作废"的作废动作，避免写两遍。
    """
    if was_secret:
        if not write_beliefs:
            return 0
        return _belief_for_unaware(db, novel_id, char_id, char_name,
                                   chapter_ref, world_time, location_hint)
    return _reveal_location(db, novel_id, char_id, chapter_ref)


# ============================================================ 线索 / 建档

def apply_threads(db, novel_id, event, chapter_ref=0, default_window=8):
    """把事件里的 open_threads 落成悬置线索（自动涌现）。"""
    created = []
    for th in event.get("open_threads") or []:
        if isinstance(th, str):
            th = {"title": th}
        title = str(th.get("title") or "").strip()
        if not title:
            continue
        existing = db.one(
            "SELECT id FROM foreshadowing WHERE novel_id=? AND title=? "
            "AND status='active'", (novel_id, title))
        if existing:
            db.nudge_thread(existing["id"], delta=1)
            continue
        tid = db.add_thread(
            novel_id, title, th.get("description") or "",
            planted_chapter=chapter_ref,
            target_chapter=chapter_ref + int(th.get("window") or default_window),
            origin="ai_emerged",
            tension_level=th.get("tension") or 3,      # 收口交给 db（_to_int）
            importance=th.get("importance") or "normal",
        )
        created.append(tid)
    return created


def apply_new_characters(db, novel_id, characters, chapter_ref=0, max_new=2):
    """为新出场角色建档。规则：每章新增 ≤2（方案 4.3）。"""
    created = []
    for ch in (characters or [])[:max_new]:
        # 防御：调用方传进来的必须是 dict。曾经有调用点把整条 dict 当名字传，
        # `str({...})` 一路写进 name 字段（见 apply_soft_projection 里的注释）。
        # 这里硬挡一道，宁可少建一个角色，也不让脏名字进库。
        if not isinstance(ch, dict):
            continue
        raw = ch.get("name")
        if not isinstance(raw, str):
            continue
        name = raw.strip()
        if not name:
            continue
        if db.get_character_by_name(novel_id, name):
            continue
        cid = db.add_character(
            novel_id, name,
            rank=ch.get("rank") or "C",
            role_tag=ch.get("role_tag") or "",
            personality=ch.get("first_impression") or ch.get("personality") or "",
            control_mode="ai",
            first_appear_chapter=chapter_ref,
        )
        created.append(cid)
    return created


def _event_character_names(ev):
    """一个事件里提到的所有角色名（含 actor / 参与人 / 状态变更对象）。"""
    names = []
    actor = str(ev.get("actor") or "").strip()
    if actor:
        names.append(actor)
    for n in (ev.get("involved_characters") or []):
        if isinstance(n, str) and n.strip():
            names.append(n.strip())
    for ch in ((ev.get("state_changes") or {}).get("characters") or []):
        n = str(ch.get("name") or "").strip()
        if n:
            names.append(n)
    return names


def collect_new_characters(db, novel_id, events, max_new=2):
    """挑出"事件里出场但尚未建档"的角色名，保持出场顺序、去重、限量。

    先建档再落状态变更：否则 apply_state_changes 查不到人，
    位置/状态/记忆会被整条丢掉（世界推演里的人物就成了走马灯）。
    """
    out = []
    seen = set()
    for ev in events:
        for name in _event_character_names(ev):
            if name in seen:
                continue
            seen.add(name)
            if db.get_character_by_name(novel_id, name):
                continue
            out.append(name)
            if len(out) >= max_new:
                return out
    return out


# ============================================================ 世界实体登记
#
# 踩过的坑（真实断裂）：apply_events 一直只把 involved_entities 塞进
# events 表的 JSON 列，**从来没有调用过 db.add_entity**。结果世界实体页
# 永远是空的，guard.build_state_constraints 里的"活跃世界实体"硬约束也
# 从来没生效过（约束段永远为空）——同一处断裂同时废掉了问题 3 和问题 4。
#
# 现在补齐：模型在事件里点名过的实体，推演落库时自动建档（幂等，同名不重复）。

_ENTITY_TYPES = ("faction", "location", "organization", "item", "concept",
                 "phenomenon", "other")

# 按名字/描述关键词猜类型——模型经常不给 entity_type，全塞 other 会让
# 世界实体页变成一堆无分类条目。
_ENTITY_TYPE_HINTS = (
    ("location", ("城", "镇", "村", "山", "河", "海", "岛", "楼", "塔", "馆",
                  "站", "街", "巷", "区", "院", "室", "厅", "门", "谷", "港",
                  "码头", "广场", "遗址", "坟", "墓", "洞穴", "森林", "沙漠")),
    ("organization", ("公司", "集团", "委员会", "协会", "机构", "事务所",
                      "事务所", "部", "局", "队", "组", "会", "社", "帮", "盟",
                      "教会", "军队", "警察", "公会", "大学", "学院", "医院")),
    ("faction", ("派", "党", "阵营", "势力", "家族", "氏族", "世家", "宗", "教派")),
    ("item", ("刀", "剑", "枪", "符", "印", "戒", "钟", "镜", "书", "卷", "册",
              "钥匙", "档案", "信", "药", "瓶", "匣", "盒", "石", "珠", "玉",
              "珠", "器", "杖", "弓", "铠", "袍")),
    ("phenomenon", ("现象", "异变", "灾", "潮", "波", "雾", "蚀", "裂", "污染",
                    "异常", "事件", "诅咒")),
    ("concept", ("规则", "法则", "约定", "契约", "制度", "秩序", "序列", "体系",
                 "等级", "协议", "定理", "守恒")),
)

# 这个名字后缀强烈意味着"某个机构/组织"，优先级要压过 location 的关键词。
#
# 踩过的坑：`_ENTITY_TYPE_HINTS` 是**按表顺序**返回第一个命中的类型，
# 而 location 排在 organization 前面。于是"异常现象调查局"先命中 location
# 的"局"，被判成地点；"XX局""XX部""XX委员会"这类专名全都错分类。
# 这里把机构后缀单独拎出来，匹配到就直接返回 organization。
_ORG_SUFFIXES = ("调查局", "管理局", "委员会", "事务所", "研究院", "实验室",
                 "有限公司", "集团", "公会", "协会", "学会", "议会", "部",
                 "局", "厅", "署", "科", "处", "司", "行", "队", "帮", "盟",
                 "会", "社", "党", "派", "院", "所", "馆", "站")


def guess_entity_type(name, description=""):
    """按名字猜实体类型。猜不出返回 'other'。

    宁可猜错也好过全落 other：世界实体页按类型分组展示，全 other 等于没分类。
    """
    text = "%s%s" % (name or "", description or "")
    # 先判机构：专名以机构后缀结尾时，location 的关键词会抢跑（"调查局"里的
    # "局"同时在两边），必须先看这里。但排除掉本身就是地点名词的情况——
    # "博物馆/图书馆"命中 _ORG_SUFFIXES 的"馆"，实际是地点。
    if name and any(str(name).rstrip().endswith(s) for s in _ORG_SUFFIXES):
        if not any(name.endswith(s) for s in ("博物馆", "图书馆", "美术馆",
                                              "大使馆", "领事馆", "车站",
                                              "地铁站", "火车站")):
            return "organization"
    for etype, kws in _ENTITY_TYPE_HINTS:
        for kw in kws:
            if kw in text:
                return etype
    return "other"


def _event_entity_names(ev):
    """一个事件里提到的所有世界实体名（含 involved_entities 与 state_changes 备注）。"""
    names = []
    for n in (ev.get("involved_entities") or []):
        if isinstance(n, str) and n.strip():
            names.append(n.strip())
    sc = ev.get("state_changes") or {}
    for ch in (sc.get("world") or []):
        key = str(ch.get("key") or "")
        if "." in key:
            head = key.split(".", 1)[0].strip()
            # 只收看起来像专名的（长度 2-12 且不含空白），避免把 "天气" 这类
            # 普通概念全建进来
            if 2 <= len(head) <= 12 and " " not in head:
                names.append(head)
    return names


def collect_new_entities(db, novel_id, events, max_new=8):
    """挑出"事件里点名但尚未建档"的世界实体名，保序去重限量。

    比角色宽（max_new=8）：角色每章限 2 是叙事纪律，实体是布景，
    漏建就永远补不回来。
    """
    known = set()
    for e in db.list_entities(novel_id, include_deleted=True):
        known.add(str(e["name"]).strip())

    out = []
    seen = set()
    for ev in events:
        for name in _event_entity_names(ev):
            if name in seen or name in known:
                continue
            seen.add(name)
            desc = ""
            if name in str(ev.get("description") or ""):
                desc = str(ev.get("description"))[:120]
            out.append({"name": name, "description": desc,
                        "chapter_ref": None, "event_title": ev.get("title", "")})
            if len(out) >= max_new:
                return out
    return out


def apply_new_entities(db, novel_id, entities, chapter_ref=0):
    """为事件里点名的世界实体建档。返回新建的 id 列表。"""
    created = []
    for ent in (entities or []):
        name = str(ent.get("name") or "").strip()
        if not name:
            continue
        if db.find_entity(novel_id, name):
            continue
        etype = ent.get("entity_type") or guess_entity_type(
            name, ent.get("description") or "")
        eid = db.add_entity(
            novel_id, entity_type=etype, name=name,
            description=ent.get("description") or "",
            status="active", power_level=ent.get("power_level") or 3,
            visibility=ent.get("visibility") or "public",
            first_appear_chapter=int(chapter_ref or 0))
        created.append({"id": eid, "name": name, "entity_type": etype})
    return created


def apply_relations(db, novel_id, relations, chapter_ref=0):
    """把推演产出的 relation_changes 落库（新羁绊/关系变化/破裂）。

    关系数据缺失的表现是"素不相识的人一见面就像老友"——模型只能猜，
    而它猜的默认值是"都认识"。所以推演里产生的关系必须落库，
    下次推演的【人物关系】段才会把它摆出来。

    实际写入复用 character.apply_relations（统一的双向/枚举/幂等收口），
    这里只做一层转接，避免两套关系写入逻辑各自跑偏。
    """
    if not relations:
        return 0
    from engine.character import apply_relations as _apply
    return _apply(db, novel_id, relations, chapter_ref=chapter_ref)


def build_result_from_events(events, world_time="", time_elapsed="",
                             decision_needed=None, relation_changes=None):
    """把手工构造的事件列表包装成与世界推演同构的结果结构。"""
    return {
        "world_time": world_time,
        "time_elapsed": time_elapsed,
        "events": events,
        "decision_needed": decision_needed or [],
        "relation_changes": relation_changes or [],
    }


def summarize_result(result):
    """给 UI 用的一句话摘要。"""
    events = result.get("events") or []
    return "推演出 %d 个事件，%d 处需要决策" % (
        len(events), len(result.get("decision_needed") or []))
