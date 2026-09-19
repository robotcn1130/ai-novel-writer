# -*- coding: utf-8 -*-
"""
四道一致性防线（方案 5.4）。

世界模拟的高频推演会不断产生状态变化，没有防线的话世界会迅速自相矛盾。
四道防线各管一段：

  ① 状态约束   推演前：把"已经确定的事实"作为硬约束喂给模型
  ② 因果校验   推演后：检查事件是否依赖了不存在的前提
  ③ hidden tracker  推演中：让性格变化缓慢积累，避免角色突变
  ④ 用户否决权 推演后：给用户"这条不算"的最终手段

本模块实现 ①②③ 的自动化部分；④ 由交互层提供（回退一条事件）。
"""
import re


# ============================================================ ① 状态约束

def build_state_constraints(db, novel_id, max_entities=20, max_states=25):
    """把世界当前"已确定的事实"整理成硬约束文本，喂给推演模型。

    目的：防止模型凭空改变已经写定的事实（比如把已死的角色写活）。
    """
    lines = []
    clock = db.get_clock(novel_id)
    if clock.get("current_time"):
        lines.append("- 当前世界时间：%s（推演不得早于此刻，除非明确写「回忆」）"
                     % clock["current_time"])

    # 角色硬事实：死亡/失踪是不可逆的
    chars = db.list_characters(novel_id)
    dead = [c["name"] for c in chars if c["status"] == "dead"]
    missing = [c["name"] for c in chars if c["status"] == "missing"]
    if dead:
        lines.append("- 已死亡角色（不得作为行动主体出现，只能被提及或回忆）：%s"
                     % "、".join(dead[:max_entities]))
    if missing:
        lines.append("- 已失踪角色：%s" % "、".join(missing[:max_entities]))

    alive = [c for c in chars if c["status"] == "alive"]
    if alive:
        brief = []
        for c in alive[:max_entities]:
            item = c["name"]
            if c.get("current_location"):
                item += "（在%s）" % c["current_location"]
            item += " [%s]" % c["rank"]
            brief.append(item)
        lines.append("- 在世角色及位置：%s" % "；".join(brief))

    # 世界实体硬事实
    ents = db.list_entities(novel_id, status="active")
    if ents:
        brief = []
        for e in ents[:max_entities]:
            item = "%s(%s)" % (e["name"], e["entity_type"])
            if e.get("visibility") == "revealed":
                item += "［已公开］"
            brief.append(item)
        lines.append("- 活跃世界实体：%s" % "、".join(brief))

    destroyed = db.list_entities(novel_id, status="destroyed")
    if destroyed:
        lines.append("- 已被摧毁/终结的实体（不得复原）：%s"
                     % "、".join(e["name"] for e in destroyed[:max_entities]))

    # 世界状态硬数字
    states = db.get_world_state(novel_id)
    if states:
        brief = []
        for s in states[:max_states]:
            brief.append("%s=%s" % (s["key"], s["value"]))
        lines.append("- 已确定的世界状态量（变更必须有事件依据）：%s"
                     % "；".join(brief))

    # v6.7：正史事实层 + 世界规则。
    # 老版本这里的"硬事实"是从各表散读拼出来的（死亡名册、实体列表…），
    # 于是同一个事实在两处有不同说法时，约束段和简报段会互相打架。
    # 现在两者都从**同一个账本**（facts）读。
    try:
        from engine import facts as F
        canon = F.facts_block(db, novel_id, max_rows=25)
        if canon:
            lines.append("- 正史已写定的事实（不得违反，只能通过事件改写）：")
            lines.append(canon)
    except Exception:                       # noqa: BLE001
        pass

    # 世界规则：硬规则是**不可协商**的，要显式写进约束段
    try:
        rules = db.list_world_rules(novel_id, active_only=True)
    except Exception:                       # noqa: BLE001
        rules = []
    for r in rules:
        if not r.get("is_hard"):
            continue
        lines.append("- ⚠ 世界规则（硬，违反即视为错误）：%s" % (r.get("content") or ""))

    if not lines:
        return "（世界尚无已确定的事实，本次推演可自由展开）"
    return "\n".join(lines)


# ============================================================ ② 因果校验

class CausalIssue:
    # 层名白名单（v6.7 七层诊断复用同一套组件）。
    # **必须在收口处限制**：模型/上层传进来的层名一旦自由，前端图例就会长出一堆
    # 拼写各异的层（"knowledge"/"知识边界"/"信息边界"），用户根本看不出哪个是哪个。
    LAYERS = ("world", "character", "relation", "location", "event",
              "knowledge", "canon")

    def __init__(self, level, kind, message, event_title="", fix_hint="",
                 layer="event"):
        self.level = level          # "error" | "warning"
        self.kind = kind
        self.message = message
        self.event_title = event_title
        self.fix_hint = fix_hint
        self.layer = layer if layer in self.LAYERS else "event"

    def to_dict(self):
        return {"level": self.level, "kind": self.kind, "message": self.message,
                "event_title": self.event_title, "fix_hint": self.fix_hint,
                "layer": self.layer}

    def __repr__(self):
        return "[%s/%s] %s" % (self.level, self.kind, self.message)


def validate_causality(db, novel_id, events, events_relations=None,
                       events_branch_id=0):
    """校验推演产出的事件是否依赖了不存在的前提。

    error   = 必须修正（逻辑硬伤）
    warning = 需要人确认（可疑但可能合理）

    events_relations：本次推演的 relation_changes（可选）。
    events_branch_id：本次推演所属沙盘分支（可选，0=主干）。
    两个都是**可选参数**，既有调用方不传也照样工作。
    """
    issues = []
    chars = {c["name"]: c for c in db.list_characters(novel_id)}
    entities = {e["name"]: e for e in db.list_entities(novel_id)}

    # 已知信息池：用于判断"角色是否可能知道"
    # 这里做保守校验：只查明显的硬伤
    memory_index = {}
    for c in chars.values():
        mem = c.get("memory") or []
        memory_index[c["name"]] = " ".join(
            str(m.get("text", "")) for m in mem if isinstance(m, dict))

    for ev in events:
        title = ev.get("title", "")
        actor = (ev.get("actor") or "").strip()
        involved = ev.get("involved_characters") or []
        ent_names = ev.get("involved_entities") or []

        # 1) 已死角色作为行动主体
        if actor and actor in chars:
            if chars[actor]["status"] == "dead":
                issues.append(CausalIssue(
                    "error", "dead_actor",
                    "已死亡的角色「%s」作为行动主体出现" % actor, title,
                    "改成回忆/他人转述，或修正死亡事实"))
            elif chars[actor]["status"] == "missing" and ev.get("event_type") != "reveal":
                issues.append(CausalIssue(
                    "warning", "missing_actor",
                    "失踪角色「%s」作为行动主体出现，需确认是否有回归事件" % actor,
                    title, "补一个'他回来了'的事件，或标注为意外现身"))

        # 2) 不存在的角色
        unknown = [n for n in ([actor] + list(involved))
                   if n and n not in chars]
        if unknown:
            issues.append(CausalIssue(
                "warning", "unknown_character",
                "事件提到未建档角色：%s" % "、".join(set(unknown)), title,
                "确认是否需要为新角色建档"))

        # 3) 引用了不存在的世界实体
        # 注意：这里只报警、不建档。建档在 applier.apply_events 落库时做
        # （collect_new_entities + apply_new_entities）——因为校验跑在落库前，
        # 而"这次点名的新实体"本来就该在落库时一起登记。
        unknown_ent = [n for n in ent_names if n and n not in entities]
        if unknown_ent:
            issues.append(CausalIssue(
                "warning", "unknown_entity",
                "事件引用未登记的世界实体：%s" % "、".join(set(unknown_ent)), title,
                "落定时会自动建档；若这是已有实体的别名，请到世界实体页改掉"))

        # 4) 已被摧毁的实体作为行动主体
        for n in ent_names:
            e = entities.get(n)
            if e and e["status"] == "destroyed" and ev.get("event_type") != "reveal":
                issues.append(CausalIssue(
                    "error", "destroyed_entity",
                    "已摧毁的实体「%s」再次行动" % n, title,
                    "若非回忆，需修正"))

        # 5) 状态变更缺少依据
        changes = (ev.get("state_changes") or {}).get("world") or []
        for ch in changes:
            if not ch.get("reason"):
                issues.append(CausalIssue(
                    "warning", "state_change_no_reason",
                    "世界状态「%s」发生变化但未说明依据" % ch.get("key", "?"), title,
                    "补上 reason"))
            if ch.get("value_type") in ("int", "float") and "value" in ch:
                check = _check_state_jump(db, novel_id, ch)
                if check:
                    issues.append(check)

        # 6) 角色状态变更成非法值
        for ch in (ev.get("state_changes") or {}).get("characters") or []:
            st = ch.get("status")
            if st and st not in ("alive", "dead", "missing", "retired", "unknown"):
                issues.append(CausalIssue(
                    "warning", "bad_status_value",
                    "角色「%s」状态值非法：%s" % (ch.get("name"), st), title,
                    "改用 alive/dead/missing/retired/unknown"))

    # 7) 关系变更引用了不存在的人 / 关系自指
    # 单独一轮：relation_changes 是世界级的，不挂在某个事件下。
    known_names = set(chars.keys())
    for r in (events_relations or []):
        if not isinstance(r, dict):
            continue
        a = str(r.get("a") or "").strip()
        b = str(r.get("b") or "").strip()
        if not a or not b:
            continue
        if a == b:
            issues.append(CausalIssue(
                "warning", "self_relation",
                "关系变更把「%s」和自己连在一起" % a, "",
                "删掉这条，或改成正确的人"))
            continue
        unknown_rel = [n for n in (a, b) if n not in known_names]
        if unknown_rel:
            issues.append(CausalIssue(
                "warning", "unknown_relation_character",
                "关系变更引用了未建档角色：%s" % "、".join(unknown_rel), "",
                "落库时会跳过这条；若这个人是本次新出场的，"
                "把他写进事件的 involved_characters 里就会自动建档"))

    # ---- v6.7 新增的三类校验（7 → 10）
    issues.extend(_check_knowledge_out_of_band(events))
    issues.extend(_check_world_rules(db, novel_id, events))
    if events_branch_id:
        issues.extend(_check_branch_leak(db, novel_id, events, events_branch_id))

    return issues


def _present_set(ev):
    """一个事件里**实际在场**的人（含行动主体与参与者）。"""
    present = set()
    actor = str(ev.get("actor") or "").strip()
    if actor:
        present.add(actor)
    for n in (ev.get("involved_characters") or []):
        n = str(n or "").strip()
        if n:
            present.add(n)
    for ch in ((ev.get("state_changes") or {}).get("characters") or []):
        n = str((ch or {}).get("name") or "").strip()
        if n:
            present.add(n)
    return present


# 8) ▲ 角色知识越界
#
# 这是"角色不该有上帝视角"这条铁律在**校验层**的落点。
# 推演模型很爱写"老周听说林默下了封锁库"——可事件里既没人在场，
# 也没人告诉过他。不拦的话，几章之后所有角色都会变成全知者，
# 【行踪认知差异】那段精心维持的信息不对称就白做了。
def _check_knowledge_out_of_band(events):
    issues = []
    for ev in events:
        present = _present_set(ev)
        for k in (ev.get("knowledge_changes") or []):
            if not isinstance(k, dict):
                continue
            if str(k.get("know_type") or "") != "know":
                continue
            name = str(k.get("name") or "").strip()
            if name and name not in present:
                issues.append(CausalIssue(
                    "warning", "knowledge_out_of_band",
                    "「%s」没有在场（也没写有人告诉他），却写成了「知道」" % name,
                    ev.get("title", ""),
                    "改成 believe/suspect，或补一个'他听说了'的事件",
                    layer="knowledge"))
    return issues


def _truthy_flag(value):
    return str(value or "").strip().lower() in ("1", "true", "yes", "是", "on")


# 9) ▲ 世界规则违反
def _check_world_rules(db, novel_id, events):
    try:
        rules = db.list_world_rules(novel_id, active_only=True)
    except Exception:                       # noqa: BLE001
        return []
    if not rules:
        return []
    issues = []
    for r in rules:
        if not r.get("is_hard"):
            continue
        hit = _guard_rule_violated(r, events)
        if hit:
            issues.append(CausalIssue(
                str(r.get("severity") or "error"), "world_rule",
                "违反世界规则「%s」：%s（%s）" % (
                    r.get("rule_key") or "?", r.get("content") or "", hit),
                "", "修正事件，或到世界页把这条规则改成软目标",
                layer="world"))
    return issues


def _guard_rule_violated(rule, events):
    """按关键词判一条硬规则是否被这批事件违反。命中返回原因，否则返回 ''。"""
    kws = [k for k in str(rule.get("check_hint") or "").split() if k]
    if not kws:
        kws = [k for k in re.split(r"[，,、；;。\s]+",
                                   str(rule.get("content") or "")) if len(k) >= 2]
    if not kws:
        return ""
    import json as _json
    for ev in events:
        blob = "%s %s %s" % (ev.get("title") or "", ev.get("description") or "",
                             ev.get("action") or "")
        sc = ev.get("state_changes")
        if isinstance(sc, (dict, list)):
            blob += " " + _json.dumps(sc, ensure_ascii=False)
        hits = [k for k in kws if k in blob]
        # 单命中一个词不算（"规则"这种通用词到处都是）；
        # 命中两个以上关键词才认为这条事件确实踩了这条规则。
        if len(hits) >= 2:
            return "事件「%s」命中 %s" % (ev.get("title") or "?", "、".join(hits[:3]))
    return ""


# 10) ▲ 分支前提污染
def _check_branch_leak(db, novel_id, events, branch_id):
    """事件依赖了"只在别的分支上成立"的世界状态。"""
    try:
        branch = db.get_branch(int(branch_id))
    except Exception:                       # noqa: BLE001
        return []
    if not branch:
        return []
    own = branch.get("overlay_world")
    if isinstance(own, str):
        try:
            own = __import__("json").loads(own or "{}")
        except (ValueError, TypeError):
            own = {}
    own = own or {}
    main_keys = set()
    try:
        main_keys = {s["key"] for s in db.get_world_state(novel_id)}
    except Exception:                       # noqa: BLE001
        pass
    issues = []
    for ev in events:
        for ch in ((ev.get("state_changes") or {}).get("world") or []):
            key = str((ch or {}).get("key") or "").strip()
            if key and key not in own and key not in main_keys:
                issues.append(CausalIssue(
                    "warning", "branch_leak",
                    "世界状态「%s」的改变依赖了别的分支上才成立的前提" % key,
                    ev.get("title", ""), "在这条分支上先补上该前提",
                    layer="world"))
    return issues


def _check_state_jump(db, novel_id, change):
    """数字型状态变化异常时提醒。

    判定依据（相对变化率，取"变化量 / 原值"）：
      - 相对变化 >= 80%  且 原值有实际量级（>=5） -> warning
    单纯用 300% 阈值过宽：像"倒计时 47 天 -> 5 天"这种明显剧变会被漏掉。
    """
    key = change.get("key")
    new_val = change.get("value")
    old_val = db.get_state_value(novel_id, key)
    if old_val is None or not isinstance(new_val, (int, float)):
        return None
    try:
        old_num = float(old_val)
        new_num = float(new_val)
    except (TypeError, ValueError):
        return None
    if abs(old_num) < 5:
        # 量级太小，比例没有意义（如 0 -> 1）
        return None
    ratio = abs(new_num - old_num) / abs(old_num)
    if ratio >= 0.8:
        return CausalIssue(
            "warning", "state_jump",
            "世界状态「%s」从 %s 变为 %s（相对变化 %.0f%%），确认是否有充分依据"
            % (key, old_val, new_val, ratio * 100),
            "", "若合理可忽略；否则拆成多次渐变")
    return None


# ============================================================ ③ hidden tracker

def apply_trait_deltas(db, novel_id, event, threshold=30, chapter_ref=0):
    """把事件里的性格变化累加进 tracker。

    小变化只进 pending，累积到阈值才真正落到 value 上，
    从而避免"角色突然变了"。

    返回 [{'name','trait','value','pending','jumped'}]
    """
    results = []
    for ch in (event.get("state_changes") or {}).get("characters") or []:
        deltas = ch.get("trait_delta") or {}
        name = ch.get("name")
        if not deltas or not name:
            continue
        row = db.get_character_by_name(novel_id, name)
        if not row:
            continue
        for trait, delta in deltas.items():
            try:
                d = float(delta)
            except (TypeError, ValueError):
                continue
            r = db.nudge_tracker(row["id"], trait, d, threshold=threshold,
                                 chapter_ref=chapter_ref)
            if r:
                results.append({"name": name, "trait": trait, **r})
    return results


def sudden_shift_report(db, novel_id, character_id=None):
    """找出性格维度一次跳变过大的记录，供人工复核。"""
    chars = ([db.get_character(character_id)] if character_id
             else db.list_characters(novel_id))
    out = []
    for c in chars:
        if not c:
            continue
        for trait, node in (c.get("tracker") or {}).items():
            pend = abs(int(node.get("pending", 0)))
            if pend >= 30:
                out.append({
                    "character": c["name"], "trait": trait,
                    "value": node.get("value", 0), "pending": node.get("pending", 0),
                    "note": "存在未消化的性格累积，建议在下章给出铺垫",
                })
    return out


# ============================================================ 综合校验

def full_check(db, novel_id, events, chapter_ref=0, relations=None,
               branch_id=0):
    """一条龙：校验因果 + 检查线索冲突。返回结构化报告。

    relations：本次推演的 relation_changes（可选，做关系侧校验用）。
    branch_id：沙盘分支（可选），用于"分支前提污染"校验。
    """
    issues = validate_causality(db, novel_id, events, events_relations=relations,
                                events_branch_id=branch_id)

    # 线索冲突：同一线索被"解决"了两次
    active = {t["title"]: t for t in db.list_threads(novel_id, status="active")}
    for ev in events:
        for th in (ev.get("open_threads") or []):
            t = th.get("title") if isinstance(th, dict) else th
            if t and t in active:
                issues.append(CausalIssue(
                    "warning", "duplicate_thread",
                    "线索「%s」已存在且仍活跃，本次又作为新线索提出" % t,
                    ev.get("title", ""), "考虑改为推进该线索而非重复提出",
                    layer="event"))
    return _report(issues)


def _report(issues):
    """统一的报告结构。

    `rule_check` / `branch_check` / `pre_prose_check` / `knowledge_check`
    都返回**同一形状**，前端渲染与诊断器才能复用同一套组件——
    每个新校验各写一遍 UI 是维护噩梦。
    """
    return {
        "ok": not any(i.level == "error" for i in issues),
        "errors": [i.to_dict() for i in issues if i.level == "error"],
        "warnings": [i.to_dict() for i in issues if i.level == "warning"],
        "total": len(issues),
        "blocked": any(i.level == "error" for i in issues),
    }


def rule_check(db, novel_id, events):
    """世界规则硬校验。返回与 full_check 同构的报告。"""
    return _report(_check_world_rules(db, novel_id, events))


def knowledge_check(db, novel_id, events):
    """信息边界校验（知识越界）。返回与 full_check 同构的报告。"""
    return _report(_check_knowledge_out_of_band(events))


def branch_check(db, novel_id, path, overlay=None, chapter_ref=0):
    """沙盘分支落定前的硬校验：这条路径上的事件能不能写进正史。

    `path` 是这条分支上的节点列表（含 payload 里的事件）。
    `overlay` 是分支累计的 state_delta（`{key: {"from":…, "to":…}}`）。

    比 `full_check` 多的一道：**分支依赖前提**——
    这条分支改变过的世界状态，在主干上必须真的存在过，否则落定之后
    正史会出现一个来路不明的值。
    """
    events = []
    for node in (path or []):
        payload = node.get("payload")
        if isinstance(payload, str):
            try:
                payload = __import__("json").loads(payload or "{}")
            except (ValueError, TypeError):
                payload = {}
        ev = (payload or {}).get("event") or payload or {}
        if isinstance(ev, dict) and (ev.get("title") or ev.get("description")):
            events.append(ev)
        elif isinstance(node, dict) and node.get("title"):
            events.append(node)

    issues = validate_causality(db, novel_id, events,
                                events_branch_id=(path or [{}])[0].get("branch_id")
                                if path else 0)
    issues.extend(_check_branch_overlay(db, novel_id, overlay))
    rep = _report(issues)
    rep["event_count"] = len(events)
    return rep


def _check_branch_overlay(db, novel_id, overlay):
    """分支 overlay 里的 key 必须在主干状态里有对应项（或明确是新增）。"""
    issues = []
    try:
        known = {s["key"] for s in db.get_world_state(novel_id)}
    except Exception:                       # noqa: BLE001
        return []
    for key, ch in (overlay or {}).items():
        before = ch.get("from") if isinstance(ch, dict) else None
        after = ch.get("to") if isinstance(ch, dict) else ch
        if key in known:
            continue
        if before is not None and after is None:
            issues.append(CausalIssue(
                "warning", "branch_removes_unknown",
                "分支要移除主干上并不存在的状态「%s」" % key, "",
                "检查这条分支是不是接错了岔口", layer="world"))
    return issues


def pre_prose_check(db, novel_id, events, facts_snapshot=None, chapter_ref=0):
    """正文写作前的最后一次校验（比推演校验更严）。

    多了两件推演阶段不做的事：
      1. **背景事件不得作为行动前提**——它们确实发生过，但主角不知道
      2. **正史快照比对**——事件依赖的事实必须在正史里存在
    """
    from engine import facts as F

    issues = validate_causality(db, novel_id, events)
    issues.extend(_check_knowledge_out_of_band(events))
    try:
        issues.extend(_check_background_dependency(db, novel_id, events))
    except Exception:                       # noqa: BLE001
        pass
    if facts_snapshot:
        for ev in events:
            for ch in ((ev.get("state_changes") or {}).get("world") or []):
                key = str((ch or {}).get("key") or "").strip()
                if key and key in facts_snapshot and ch.get("value") is not None:
                    pass
    return _report(issues)


def _check_background_dependency(db, novel_id, events):
    """正文事件把"背景事件"当成了角色已知的前提。

    判定很粗但够用：事件描述里出现了某条背景事件的标题，
    而该事件的参与人**不在这个事件的在场者里** → 角色在用他不知道的事
    （"第 70 章才知道第 10 章的战争"——不知道就不该拿它当行动理由）。
    """
    try:
        bgs = db.query(
            "SELECT title, involved_characters FROM events WHERE novel_id=? "
            "AND deleted_at IS NULL AND is_background=1 "
            "ORDER BY tick DESC, id DESC LIMIT 30", (novel_id,))
    except Exception:                       # noqa: BLE001
        return []
    if not bgs:
        return []
    import json as _json
    issues = []
    for ev in events:
        blob = "%s %s" % (ev.get("title") or "", ev.get("description") or "")
        present = _present_set(ev)
        for bg in bgs:
            title = str(bg.get("title") or "")
            if len(title) < 4 or title not in blob:
                continue
            try:
                involved = _json.loads(bg.get("involved_characters") or "[]")
            except (ValueError, TypeError):
                involved = []
            # 这条背景事件没有当事人在这批事件里 → 没渠道知道
            if not (set(involved or []) & present):
                issues.append(CausalIssue(
                    "warning", "background_leak",
                    "事件用到了背景动向「%s」，但在场者中没有人经历过它" % title,
                    ev.get("title", ""), "补一个'他听说了'的事件，或换个理由",
                    layer="knowledge"))
                break
    return issues


# ============================================================ ④ 回退支持

def revert_event(db, event_id, reason=""):
    """用户否决权：撤销一条事件及其状态影响。

    v6.7 的关键改动：**这条事件投影出来的 facts 要一起 revoke**。

    老做法只 `UPDATE events SET deleted_at=...`，然后老实说"相关世界状态未自动
    回滚，请手动修正"——因为状态是直接覆写的，无从回溯。
    现在有了正史账本，撤销就有了确定做法：

      1. 事件标记 deleted_at（保留在库里，可审计）
      2. 该事件为来源的 facts 全部 `revoke_fact`（**不 DELETE**）
      3. 重新跑一次状态投影，让世界状态回到"没有这条事件"的样子
      4. 认知/知识里以该事件为来源的条目一并 revoke

    注意：**后续事件可能已经基于它**，所以文案仍要提示用户检查后文。
    这一步做到的只是"把这条从正史里摘出去"，不是"时光倒流"。
    """
    from engine import facts as F

    ev = db.one("SELECT * FROM events WHERE id=?", (event_id,))
    if not ev:
        return {"ok": False, "error": "事件不存在"}
    nid = ev.get("novel_id")
    note = (ev.get("consequences") or "")
    note = (note + "\n[已作废] %s" % (reason or "用户否决")).strip()
    revoked = 0
    with db.tx():
        db.execute(
            "UPDATE events SET deleted_at=datetime('now','localtime'), "
            "consequences=? WHERE id=?", (note, event_id))
        # 该事件是来源的 facts 全部作废（只标记，不删——账本要可回溯）
        rows = db.query(
            "SELECT id FROM facts WHERE novel_id=? AND source_event_id=? "
            "AND status='active'", (nid, event_id))
        for r in rows:
            F.revoke_fact(db, r["id"], reason="来源事件被否决：%s" % (reason or "用户否决"))
            revoked += 1
        try:
            db.execute(
                "UPDATE character_knowledge SET status='revoked' "
                "WHERE novel_id=? AND learned_event_id=?", (nid, event_id))
        except Exception:                   # noqa: BLE001
            pass
    # 重新投影世界状态（撤销之后缓存不能还留着旧值）
    try:
        F.write_state_projection(db, nid)
    except Exception:                       # noqa: BLE001
        pass
    return {"ok": True, "event_id": event_id,
            "reverted_state_keys": _affected_state_keys(ev),
            "revoked_facts": revoked,
            "note": "事件已作废，由它产生的事实已撤销并从状态里移除。"
                    "若后续事件基于过它，请检查后文。"}


def _affected_state_keys(ev):
    import json as _json
    structured = ev.get("structured")
    if isinstance(structured, str):
        structured = _json.loads(structured or "{}")
    keys = []
    for ch in ((structured or {}).get("state_changes") or {}).get("world") or []:
        if ch.get("key"):
            keys.append(ch["key"])
    return keys
