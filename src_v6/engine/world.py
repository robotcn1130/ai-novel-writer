# -*- coding: utf-8 -*-
"""
世界推进引擎：心脏。

一次 tick（推进）的流程（方案 5.3）：
  1. 装配上下文（世界状态 + 角色目标 + 活跃线索）
  2. 生成硬约束（防线①）
  3. 调 LLM 推演 -> 结构化事件流
  4. 因果校验（防线②）
  5. 应用状态变化（含 hidden tracker，防线③）
  6. 处理 decision_needed -> 建决策点
  7. 推进世界时钟

设计原则：
  - 引擎只产"发生了什么"，不产文学文本
  - 单次 tick 的产出可被用户整体否决（防线④）
  - 每一步都可单独调用，便于测试与调试
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC_V6 = os.path.dirname(_HERE)
for _p in (_SRC_V6, os.path.join(_SRC_V6, "core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from engine import prompts as P
from engine import guard
from engine import applier
from engine import facts as F
from engine import ticktime
from llm import client as LLM

# 元层事件（canon_revision）不进任何"给模型看的事件流"（约束 3）。
# 与 database.META_EVENT_TYPES 同源；这里加兜底是为了 world 能独立 import。
try:
    from core.database import META_EVENT_TYPES
except Exception:                           # noqa: BLE001
    META_EVENT_TYPES = ("canon_revision",)

# 关系类型的中文标签（简报与校验提示共用）
_REL_LABEL = {"family": "亲属", "friend": "朋友", "enemy": "敌对", "lover": "恋慕",
              "colleague": "同僚", "mentor": "师徒", "rival": "竞争",
              "other": "其他"}

MAX_EVENTS_PER_TICK = 6

# 一条事件要能落库/落沙盘，这些字段必须有实际内容。
# 依据 WORLD_SIM_SCHEMA 的 required —— 网关截断时，超出这个集合的字段
# （state_changes / open_threads / importance…）缺了无所谓，但缺了这几个
# 的事件根本没法用：树上是有标题没内容的一格，拿去写正文更是写不出来。
EVENT_REQUIRED_FIELDS = ("title", "description", "actor", "action",
                         "intent", "result")


def split_events(parsed):
    """把解析结果里的事件按"字段齐不齐"分成 usable / dropped 两堆。

    返回 {"usable": [ev, ...], "dropped": [(ev, [缺的字段名]), ...]}

    为什么要分级：网关把 JSON 拦腰截断时，`extract_json` 的"截断补全"能在
    原文坏掉的情况下救回前面的元素 —— 但**救回来的元素自己可能是半个**
    （模型才写了个 title，description 还没写完就被掐了）。
    过去只看"events 数组非空"就放行，会把半条事件落进库：树上显示一个
    有标题没内容的事件，拿去写正文更是没法用。
    现在缺字段的当场丢掉并在日志里说清，一条都不齐就重发。

    真机案例：deepseek-flash 走 tokenhub 网关，json_schema 被 400 拒绝降级到
    json_object，返回体被截断（finish_reason 却报正常结束），events[0] 只剩
    title —— 过去这一步会「顺利落库」。
    """
    raw_events = (parsed or {}).get("events") or []
    usable, dropped = [], []
    for ev in raw_events:
        if not isinstance(ev, dict):
            dropped.append((ev, ["（不是对象）"]))
            continue
        miss = [f for f in EVENT_REQUIRED_FIELDS
                if not str(ev.get(f) or "").strip()]
        if miss:
            dropped.append((ev, miss))
        else:
            usable.append(ev)
    return {"usable": usable, "dropped": dropped}


# 解析失败时给用户看的排查方向（比"未产出有效事件流"这种话有用得多）
_PARSE_HINT = (
    "常见原因：\n"
    "  ① 用的是推理模型（deepseek-flash / deepseek-reasoner 等），思维链会占满"
    "输出预算，正文被拦腰截断 —— 系统已自动加倍预算重试，仍失败就把该档位"
    "换回普通对话模型；\n"
    "  ② 该网关在 prompt_only 降级下不保证 JSON 格式，偶发乱写 —— 重试一次通常就好；\n"
    "  ③ 输出确实太长，调小本次推演的事件数。")


class EngineError(Exception):
    pass


class WorldEngine:
    """世界推进引擎。db 为 NovelDatabase，router 为 LLMRouter。"""

    def __init__(self, db, router, novel_id):
        self.db = db
        self.router = router
        self.novel_id = novel_id

    # ------------------------------------------------ 上下文

    def _ctx_brief(self, focus_chars=None, max_events=8, upto_event_id=None,
                   branch_id=0, upto_tick=None):
        """组装推演用的世界简报（紧凑文本，不是全量 dump）。

        upto_event_id：
          None（默认）—— 简报代表"这部作品目前的最新面貌"，用于顺着末梢往下推。
          整数      —— 只看到这条线上的这个事件为止，用于"回到前面某张卡片重新岔开"。
                       后面那些已经落定的进展会从【刚发生的事】里切掉；世界时间/状态/
                       角色现状仍是全局最新的，所以会明确标注"仅供背景参考"——
                       不标注的话，模型会把它们当成"这条线上已经发生"，于是内容
                       还是接着最下面那条线写。

        v6.7 新增两个维度：
          branch_id  ≠ 0（沙盘分支）—— 【正史事实】给的是**分叉点当时为真的事实**
                       （`as_of_tick`），并追加【本分支已改变的事】。
                       若分支推演读到分支之后才成立的 facts，现象就还是那句
                       "我从前面的卡片重推，出来的内容还是从最下面来的"。
          upto_tick  非空 —— 显式指定"只看到第 N tick 为止"。默认 None 时
                       由 branch_id 去 `branches.base_tick` 取，取不到就退化成
                       旧行为（**默认 None = 老行为，这条不能改回去**）。
        """
        db, nid = self.db, self.novel_id
        n = db.get_novel(nid)
        if not n:
            raise EngineError("小说不存在：%s" % nid)
        rewind = upto_event_id is not None

        # 沙盘分支：从 branches.base_tick 取分叉点时间（取不到就不切）
        branch = None
        if branch_id:
            try:
                branch = db.get_branch(int(branch_id))
            except Exception:                   # noqa: BLE001
                branch = None
        if upto_tick is None and branch:
            upto_tick = int(branch.get("base_tick") or 0) or None
        base_tick = int(upto_tick) if upto_tick else None
        if base_tick is not None:
            rewind = True

        bg = "（整部作品目前的最新面貌，仅供背景参考）" if rewind else ""
        L = []
        L.append("【作品】%s ｜ 题材：%s ｜ 基调：%s" % (
            n["title"], n.get("genre") or "未定", n.get("tone") or "未定"))
        L.append("【前提】%s" % (n.get("premise") or "未设定"))
        L.append("【核心张力】%s" % (n.get("initial_tension") or "未设定"))

        # 世界时间：**带 tick**。没有 tick，模型算不出"过了多久"。
        L.append(ticktime.tick_block(db, nid, base_tick=base_tick))

        # 正史事实（v6.7 新增）：这一段替代原来零散的"世界状态 + 死伤名册"。
        # 模型需要的是**真正的事实层**：有主语、有时效、有来源，
        # 而不是一堆没有主语的 key=value。
        canon = F.facts_block(db, nid, max_rows=30, as_of_tick=base_tick)
        if canon:
            L.append("【正史事实%s】" % bg)
            L.append(canon)

        # 世界规则（P1）：把硬约束显式喂给模型，而不是只靠 guard 的自然语言
        try:
            rules = db.list_world_rules(nid, active_only=True)
        except Exception:                       # noqa: BLE001
            rules = []
        if rules:
            L.append("【世界规则】" + "；".join(
                ("⚠ " if r.get("is_hard") else "") + str(r.get("content") or "")
                for r in rules[:8]))

        # 世界状态：**投影缓存**。标注清楚"不要直接改"，并说明它只是
        # 【正史事实】的一份派生视图——否则模型会把它当成第二个真相源。
        states = db.get_world_state(nid)
        if states:
            L.append("【世界状态·投影值%s】（由正史事实派生，仅供参照，不要当成"
                     "可以随意改的设定）" % bg + "；".join(
                "%s=%s" % (s["key"], s["value"]) for s in states[:20]))

        # 分支沙盘：这条分支上已经改变的事
        if branch:
            self._append_branch_overlay(L, branch)

        # 角色（带目标）
        chars = db.list_characters(nid, status="alive")
        goals = db.list_goals(novel_id=nid, status="active")
        by_char = {}
        for g in goals:
            by_char.setdefault(g["character_id"], []).append(g)
        if chars:
            L.append("【角色%s】" % bg)
            for c in chars[:12]:
                gs = by_char.get(c["id"]) or []
                gt = "；".join(
                    ("长期" if g["goal_type"] == "long" else "短期") + "：" + g["content"]
                    + ("（障碍：%s）" % g["obstacle"] if g.get("obstacle") else "")
                    for g in gs) or "无明确目标"
                line = "  - %s[%s]" % (c["name"], c["rank"])
                if c.get("current_location"):
                    line += " 位于%s" % c["current_location"]
                line += " ｜ 目标：%s" % gt
                if c.get("decision_tendency"):
                    line += " ｜ 倾向：%s" % c["decision_tendency"]
                L.append(line)
                if c.get("personality"):
                    L.append("      性格：%s" % c["personality"])
                # 他知道什么（v6.7）：角色的行动依据是认知，不是事实。
                # 只给 2 条，多了白占 token。
                knows = self._knowledge_digest(db, nid, c["id"], limit=2)
                if knows:
                    L.append("      他所知道的：%s" % knows)

        # 人物关系网
        # 没有这一段，模型会默认"这些人互相都认识、都能直接对话"——
        # 陌生人开口就像老友，人物动机也缺了"他在意谁、防着谁"这层。
        #
        # 方向必须标出来（用 db.group_relations 统一归组）：双向关系落两条
        # （A→B、B→A），显示成「A ↔ B」；单向关系只有一条，显示成
        # 「A → B（B 不知情）」。若单向也画成 ↔，模型会以为双方心知肚明。
        groups = db.group_relations(nid)
        if groups:
            L.append("【人物关系】")
            for g in groups[:24]:
                r = g["record"]
                arrow = "↔" if g["mutual"] else "→"
                extra = "" if g["mutual"] else "（%s 不知情）" % g["b"]
                L.append("  - %s %s %s%s：%s（强度%s，%s）%s" % (
                    g["a"], arrow, g["b"], extra,
                    _REL_LABEL.get(g["relation_type"], g["relation_type"]),
                    r["intensity"], r["status"],
                    ("：" + r["description"]) if r.get("description") else ""))

        # 行踪认知（只有"秘密行踪"才有账）
        #
        # 上面【角色】段里的"位于X"是**事实**（上帝视角）。角色做决定靠的是
        # 他**以为**的事——若只给事实，模型会让毫不知情的人径直走向秘密地点
        # 找人，信息不对称当场破功。这里只摆"事实与某项认知不符"的条目：
        # 一致的不用说（说了只会白占 token），不一致的才是剧情张力所在。
        self._append_belief_divergence(L, db, nid)

        # 活跃线索
        threads = db.list_threads(nid, status="active")
        if threads:
            L.append("【未解决的事】")
            for t in threads[:10]:
                L.append("  - %s（张力%s，埋于第%s章）%s" % (
                    t["title"], t["tension_level"], t["planted_chapter"],
                    ("：%s" % t["description"]) if t.get("description") else ""))

        # 世界实体
        ents = db.list_entities(nid, status="active")
        if ents:
            L.append("【世界实体】" + "、".join(
                "%s(%s)" % (e["name"], e["entity_type"]) for e in ents[:15]))

        # 近期事件
        # 重推（upto_event_id 非 None）时只看到"这条线的岔口"为止——后面已经落定的
        # 进展在这条线上还没发生，摆给模型看它就会顺着写下去。
        #
        # v6.7 两条过滤，缺一不可：
        #   · is_background=1 的背景动向：它们"真的发生过"，但不是主线剧情；
        #     混进来会让模型把远处的战争当成主角刚经历的事。
        #   · canon_revision 元层事件：那是作者改设定，不是故事里的事。
        #     漏掉它，模型会写出「作者修正了设定」这种正文（约束 3）。
        _no_meta = (" AND event_type NOT IN (%s)"
                    % ", ".join("'%s'" % t for t in META_EVENT_TYPES))
        if upto_event_id is None:
            recent = db.query(
                "SELECT title, description, world_time FROM events "
                "WHERE novel_id=? AND deleted_at IS NULL AND is_background=0"
                + _no_meta +
                " ORDER BY id DESC LIMIT ?",
                (nid, int(max_events)))
            recent_label = "【刚发生的事】"
        else:
            up = int(upto_event_id or 0)
            recent = db.query(
                "SELECT title, description, world_time FROM events "
                "WHERE novel_id=? AND deleted_at IS NULL AND is_background=0"
                + _no_meta +
                " AND id<=? ORDER BY id DESC LIMIT ?", (nid, up, int(max_events))
            ) if up > 0 else []
            recent_label = "【这条线上、到这个岔口为止已经写进正文的事】"
        if recent:
            recent.reverse()
            L.append(recent_label)
            for e in recent:
                L.append("  - %s：%s" % (e["title"], (e["description"] or "")[:80]))

        # 背景动向（P1）：方案 §11 —— "即使主角第 70 章才知道第 10 章发生的战争，
        # 系统仍然知道这场战争在第 10 章已经真实发生"。
        # 改造前 pulse() 只写 timeline（那是"待触发锚点"，不是"已发生的事"），
        # 于是第 70 章的推演根本看不到第 10 章的战争，蝴蝶效应是假的。
        bgs = db.query(
            "SELECT title, description, tick FROM events WHERE novel_id=? "
            "AND deleted_at IS NULL AND is_background=1 "
            "ORDER BY tick DESC, id DESC LIMIT 5", (nid,))
        if bgs:
            L.append("【背景动向】（与主线无关的地方上确实发生的事，"
                     "角色未必知道；需要时可以让它撞回主线）")
            for e in bgs:
                L.append("  - [第%s tick] %s：%s" % (
                    e.get("tick") or 0, e["title"],
                    (e["description"] or "")[:70]))

        # 时间线锚点
        tl = ticktime.tick_line(db, nid, limit=5)
        if tl:
            L.append("【待触发的时间线】" + "；".join(tl))

        # 作者声音：全类别注入。
        #
        # 踩过的坑：这里原来只取 category="taboo"。用户把"人物对话别太专业""描述
        # 用日常口语"这类要求填在「句法习惯 / 对白习惯」里，推演就完全看不到，
        # 于是正文不专业、推演出来的事件标题却全是行话术语——正文侧和引擎侧
        # 用的是两套口径。现在按类别分组全给，措辞要求（含"不要术语"）才能
        # 同时约束推演和正文。
        voice = db.list_voice(nid)
        if voice:
            by_cat = {}
            for v in voice:
                by_cat.setdefault(v["category"], []).append(v)
            # 顺序：先给"措辞/口吻"类，再给"绝对不写"类——最要紧的排前面，
            # 免得被截断丢掉。
            labels = (("syntax", "句法习惯"), ("dialogue", "对白习惯"),
                      ("sensory", "感官偏好"), ("motif", "反复出现的意象"),
                      ("taboo", "绝对不写"))
            for cat, label in labels:
                items = by_cat.get(cat) or []
                if not items:
                    continue
                L.append("【叙事要求·%s】" % label + "；".join(
                    v["content"] for v in items[:5]))

        return "\n".join(L)

    @staticmethod
    def _append_belief_divergence(L, db, nid):
        """把"某人以为他在哪 ≠ 他真在哪"的条目摆出来。

        判断"不一致"用位置节点比对：believed_location_id 与目标的
        location_id 不同，或者认知连节点都没有（只知"行踪不明"），
        都算分歧。精度低于 building 的分歧不值得报——"有人以为他在旧城区"
        这种粗粒度认知跟事实差不离，写进提示词只会让模型瞎紧张。
        """
        try:
            beliefs = db.list_location_beliefs(nid)
        except Exception:
            return
        if not beliefs:
            return
        chars = {c["id"]: c for c in db.list_characters(nid)}
        lines = []
        for b in beliefs:
            if b.get("stale"):
                continue
            tgt = chars.get(b["character_id"])
            obs = chars.get(b["observer_id"])
            if not (tgt and obs):
                continue
            real_id = tgt.get("location_id")
            if real_id and b.get("believed_location_id") == real_id:
                continue                     # 认知跟事实一致，没什么好说的
            if not real_id and not b.get("believed_location_id"):
                continue
            believed = b.get("believed_text") or "行踪不明"
            real = tgt.get("current_location") or "行踪不明"
            lines.append("  - %s 以为 %s 在「%s」，实际在「%s」" % (
                obs["name"], tgt["name"], believed, real))
        if lines:
            L.append("【行踪认知差异】" +
                     "（做决定时以各自的认知为准，别让不知情者直接找到对方）")
            L.extend(lines[:12])

        # v6.7：认知与事实不符的**通用**段（不止位置）。
        # 方案 §10「把记忆升级为知识状态」——角色的 believe/suspect/assume
        # 不进 facts，但必须在简报里出现，否则模型会让他们表现得全知。
        try:
            klines = []
            for k in db.list_knowledge(nid, know_type="suspect"):
                klines.append("  - %s 怀疑：%s" % (
                    k.get("character_name") or "?", (k.get("content") or "")[:50]))
            for k in db.list_knowledge(nid, know_type="assume"):
                klines.append("  - %s 暂且假定：%s" % (
                    k.get("character_name") or "?", (k.get("content") or "")[:50]))
            if klines:
                L.append("【他们心里怎么想】（未必是事实，但角色会照它行事）")
                L.extend(klines[:8])
        except Exception:                       # noqa: BLE001
            pass

    @staticmethod
    def _knowledge_digest(db, nid, character_id, limit=2):
        """一个角色"知道的事"摘要（供【角色】段用）。

        角色的行动依据是**认知**不是事实。不给这段，模型会让他们全知全能。
        """
        try:
            rows = db.list_knowledge(nid, character_id=character_id,
                                     know_type="know", limit=limit)
        except Exception:                       # noqa: BLE001
            return ""
        return "；".join((r.get("content") or "")[:40] for r in rows if r.get("content"))

    @staticmethod
    def _append_branch_overlay(L, branch):
        """把这条沙盘分支上已经改变的事摆出来（分支推演专用）。

        没有这一段，模型不知道"这条分支跟主干有什么不一样"，
        于是它给出的推演跟主干一模一样——沙盘就白做了。
        """
        try:
            import json as _json
            world = branch.get("overlay_world")
            if isinstance(world, str):
                world = _json.loads(world or "{}")
            if isinstance(world, dict) and world:
                L.append("【本分支已改变的事】（只在沙盘里成立，尚未落定）")
                for k, v in list(world.items())[:10]:
                    val = v.get("to") if isinstance(v, dict) else v
                    L.append("  - %s → %s" % (k, val))
        except Exception:                       # noqa: BLE001
            pass

    def canonical_block(self, as_of_tick=None, max_rows=30):
        """公开入口：正史事实块（诊断层、正文装配、沙盘都复用）。"""
        return F.facts_block(self.db, self.novel_id, max_rows=max_rows,
                             as_of_tick=as_of_tick)

    def state_projection(self, upto_tick=None):
        """公开入口：把正史事实投影成 `{key: value}`（**不落库**）。"""
        return F.project_state(self.db, self.novel_id, upto_tick=upto_tick)

    # ------------------------------------------------ 时间推进

    def brief(self, **kw):
        """公开入口：世界简报。剧情树引擎复用同一套上下文装配。"""
        return self._ctx_brief(**kw)

    def advance(self, focus="", extra_constraints="", dry_run=False,
                chapter_ref=0, apply=True):
        """推进一次世界。

        参数：
          focus             本次推演希望聚焦的方向（可空）
          extra_constraints 额外硬约束（用户临时加的规则）
          dry_run           只推演不落库
          chapter_ref       归属章节

        返回 {
          'result': 原始结构化结果,
          'check': 因果校验报告,
          'applied': 落库摘要（dry_run 时为空）,
          'decision_point_ids': [...],
          'context_brief': 本次用的简报,
        }
        """
        if not self.db.get_novel(self.novel_id):
            raise EngineError("小说不存在")

        brief = self._ctx_brief()
        # 防线①：硬约束
        constraints = guard.build_state_constraints(self.db, self.novel_id)
        merged = constraints
        if extra_constraints:
            merged = constraints + "\n\n## 本次额外约束\n" + extra_constraints

        # 推演
        parsed, raw, mode, salvaged = self.router.run_json(
            "world_sim",
            schema=P.WORLD_SIM_SCHEMA,
            system=P.WORLD_SIM_SYSTEM,
            user=P.world_sim_user(brief, merged, focus),
            max_tokens=8192,
            with_meta=True,
        )
        # 先分级再落库：截断抢救回来的半条事件不能进正史（详见 split_events）
        got = split_events(parsed)
        if not got["usable"]:
            LLM.note("⚠ 这次输出的事件都不完整，自动重发一次…")
            parsed, raw, mode, salvaged = self.router.run_json(
                "world_sim", schema=P.WORLD_SIM_SCHEMA,
                system=P.WORLD_SIM_SYSTEM,
                user=P.world_sim_user(brief, merged, focus), max_tokens=8192,
                with_meta=True)
            got = split_events(parsed)

        if not got["usable"]:
            raise EngineError(
                "推演未产出有效事件流（解析模式：%s）。\n%s\n原始输出片段：%s"
                % (mode, _PARSE_HINT, (raw or "")[:300]))

        if got["dropped"]:
            LLM.note("⚠ 这次有 %d 条事件没写完（缺 %s），已丢弃、只保留完整的 %d 条。"
                     % (len(got["dropped"]),
                        "、".join(sorted({f for _, miss in got["dropped"]
                                          for f in miss})),
                        len(got["usable"])))
        elif salvaged:
            LLM.note("⚠ 模型的输出在中途被截断，最后一个没写完的事件已丢弃；"
                     "这次只落了 %d 条（解析模式 %s）。" % (len(got["usable"]), mode))

        events = got["usable"][:MAX_EVENTS_PER_TICK]
        parsed["events"] = events

        # 防线②：因果校验
        check = guard.full_check(self.db, self.novel_id, events,
                                 chapter_ref=chapter_ref,
                                 relations=parsed.get("relation_changes") or [])

        out = {
            "result": parsed,
            "check": check,
            "applied": {},
            "decision_point_ids": [],
            "context_brief": brief,
            "parse_mode": mode,
            "raw": raw if not parsed else "",
        }
        if dry_run:
            return out

        # 防线②b：世界规则校验（v6.7 新增）。
        # 因果校验看的是"这件事说不说得通"，规则校验看的是"这件事违不违反世界设定"。
        rules = {"ok": True, "blocked": False, "errors": [], "warnings": []}
        try:
            rules = guard.rule_check(self.db, self.novel_id, events)
        except AttributeError:
            pass                                # guard 还没升级时不阻塞
        except Exception as e:                  # noqa: BLE001
            LLM.note("⚠ 世界规则校验异常（已跳过）：%s" % e)
        out["rule_check"] = rules

        out["projection"] = None

        # 有 error 级问题时默认不落库，交由调用方决定
        if not check["ok"] or rules.get("blocked"):
            out["blocked"] = True
            reasons = []
            if not check["ok"]:
                reasons.append("因果校验发现 %d 处硬伤" % len(check["errors"]))
            if rules.get("blocked"):
                reasons.append("违反世界规则 %d 处" % len(rules.get("errors") or []))
            out["blocked_reason"] = "；".join(reasons) + "，未落库"
            return out

        # 落库：**算（投影）→ 落（唯一写入口）**
        if apply:
            tick_now = ticktime.current_tick(self.db, self.novel_id)
            delta = ticktime.delta_from(parsed, default=1)
            new_tick = tick_now + delta

            # ③ 投影（纯函数）——沙盘走的就是这一步，所以这里也能预演
            proj = applier.project_events(
                self.db, self.novel_id, parsed, tick=new_tick,
                chapter_ref=chapter_ref)
            out["projection_new_tick"] = new_tick
            out["tick_delta"] = delta

            # ④ 落库（唯一写入口）
            out["applied"] = applier.apply_projection(
                self.db, self.novel_id, proj, tick=new_tick,
                chapter_ref=chapter_ref)

            # 决策点
            out["decision_point_ids"] = self._create_decisions(
                parsed.get("decision_needed") or [], chapter_ref=chapter_ref)

            # ⑤ 时间推进改为 tick 运算（展示文本只在模型给了新描述时更新）
            ticktime.advance(self.db, self.novel_id, delta=delta,
                             new_time=parsed.get("world_time") or None,
                             chapter_ref=chapter_ref)
            out["tick"] = new_tick
            out["world_time"] = ticktime.display_time(self.db, self.novel_id)

            # ⑥ 状态投影（facts → world_state 缓存）
            try:
                out["state_projection"] = F.write_state_projection(
                    self.db, self.novel_id)
            except Exception as e:              # noqa: BLE001
                out["state_projection"] = {"updated": 0, "skipped": 0}
                LLM.note("⚠ 状态投影异常（不影响事件落库）：%s" % e)

            # 时间线锚点：把"将来会到期"的约定记下来，后续简报会摆给模型，
            # 让"日子往前走"这件事真的会撞回剧情。（提取失败不影响推演）
            try:
                out["timeline_anchors"] = self.extract_timeline_anchors(
                    events, chapter_ref=chapter_ref)
                out["anchors_triggered"] = self.expire_due_anchors(
                    events, chapter_ref=chapter_ref)
            except Exception as e:                  # noqa: BLE001
                out["timeline_anchors"] = []
                out["anchors_triggered"] = []
                LLM.note("⚠ 时间线锚点处理异常（已跳过）：%s" % e)
        return out

    def _create_decisions(self, needed, chapter_ref=0):
        """把 decision_needed 落成决策点（选项留空，由决策调度层补）。"""
        ids = []
        for d in needed:
            actor_name = d.get("actor_name") or ""
            actor_id = None
            row = self.db.get_character_by_name(self.novel_id, actor_name)
            if row:
                actor_id = row["id"]
            did = self.db.add_decision(
                self.novel_id,
                title=d.get("title") or "待定决策",
                situation=d.get("situation") or "",
                stakes=d.get("stakes") or "",
                trigger_type=d.get("trigger_type") or "moral",
                actor_type="character" if actor_id else "world",
                actor_id=actor_id,
                actor_name=actor_name,
                chapter_ref=chapter_ref,
                note=d.get("reason") or "",
            )
            ids.append(did)
        return ids

    # ------------------------------------------------ 分支推演

    def simulate_decision(self, decision_id, choice_text, choice_label="",
                          rollback=False):
        """推演"如果角色选了 X，会怎样"。

        rollback=True 时不落库，仅返回推演预览（用于让用户先看后果）。
        """
        d = self.db.get_decision(decision_id)
        if not d:
            raise EngineError("决策点不存在：%s" % decision_id)

        brief = self._ctx_brief()
        choice_desc = choice_label or choice_text
        focus = ("角色「%s」做出了这样的选择：%s。请推演这个选择直接引发的后果，"
                 "以及它如何改变了后续的局势。不要替读者总结意义。"
                 % (d["actor_name"] or "某人", choice_desc))

        constraints = guard.build_state_constraints(self.db, self.novel_id)
        constraints += ("\n\n## 已确定的选择（不可更改）\n%s" % choice_desc)

        parsed, raw, mode = self.router.run_json(
            "world_sim", schema=P.WORLD_SIM_SCHEMA, system=P.WORLD_SIM_SYSTEM,
            user=P.world_sim_user(brief, constraints, focus), max_tokens=8192)

        if not parsed or not parsed.get("events"):
            raise EngineError("分支推演未产出一致结果：%s" % (raw or "")[:200])

        events = parsed["events"][:MAX_EVENTS_PER_TICK]
        parsed["events"] = events
        check = guard.full_check(self.db, self.novel_id, events,
                                 relations=parsed.get("relation_changes") or [])

        out = {"result": parsed, "check": check, "applied": {},
               "dry_run": bool(rollback), "decision_id": decision_id}

        if not rollback and check["ok"]:
            out["applied"] = applier.apply_events(
                self.db, self.novel_id, parsed,
                chapter_ref=d.get("chapter_ref") or 0)
            self.db.advance_clock(self.novel_id,
                                  new_time=parsed.get("world_time") or None, ticks=1)
        return out

    # ------------------------------------------------ 世界脉搏 / 时间线锚点

    def pulse(self, time_span="", focus="", dry_run=False, chapter_ref=0,
              max_events=3):
        """推演"时间自身带来的变化"——与主角无缘的那部分蝴蝶效应。

        踩过的坑（真实断裂）：这个项目原来**没有任何"时间自然演化"的机制**。
        每次推演都是"角色为了目标行动"，世界时间往前走一格，但世界本身是静止的：
        没有远处的势力在动、没有与主线无关的地方新闻、没有"日子一天天过去"
        带来的背景变化。用户问的"与剧情无缘的那些变化"就是这里缺的。

        与 advance() 的区别：
          advance()  角色主动行动 —— 主角能不能做成事
          pulse()    世界被动演化 —— 时间自己改变了什么

        产出的事件 importance 一律低、默认不进正史：
        **只写进 timeline 表当锚点候选，等 advance() 时作为背景喂给模型**。
        这样背景变化不会污染正史事件流，又确实"存在"，日后能撞回主线。
        """
        db, nid = self.db, self.novel_id
        brief = self._ctx_brief()
        parsed, raw, mode = self.router.run_json(
            "world_sim",
            schema=P.WORLD_PULSE_SCHEMA,
            system=P.WORLD_PULSE_SYSTEM,
            user=P.world_pulse_user(brief, time_span, focus),
            max_tokens=4096,
        )
        events = split_events(parsed)["usable"] if parsed else []
        events = events[:max_events]
        out = {"result": parsed, "events": events, "parse_mode": mode,
               "raw": raw if not events else "", "applied": {},
               "anchors": []}
        if dry_run:
            return out

        # 背景事件要"真的发生过"（方案 §11 原话）：
        # 「即使主角第 70 章才知道第 10 章发生的战争，系统仍然知道这场战争
        #   在第 10 章已经真实发生」。
        #
        # 改造前只写 timeline——那是"待触发锚点"，不是"已发生的事"，
        # 于是第 70 章的推演**看不到**第 10 章的战争，蝴蝶效应是假的。
        # 现在它进 events（is_background=1，不污染主线装配），
        # 【背景动向】段会把它摆给模型；同时继续写 timeline 锚点，
        # 因为"战争带来的余波还会继续"这层信息仍有价值。
        tick_now = ticktime.current_tick(db, nid)
        delta = ticktime.delta_from(parsed, default=1) if parsed else 1
        new_tick = tick_now + delta

        # 时间口径：world_time 在 WORLD_PULSE_SCHEMA 里是**顶层字段**，不在
        # 单个事件里。三级回退：事件自带 → 顶层 → 世界时钟。
        head_time = str(parsed.get("world_time") or "") if parsed else ""
        fallback_time = head_time or (db.get_clock(nid).get("current_time") or "")

        if events:
            proj = applier.project_events(
                db, nid, {"events": events, "world_time": head_time},
                tick=new_tick, chapter_ref=chapter_ref, is_background=True,
                apply_threads=False)
            # 背景事件投影出的事实一律 derived —— 它们不是作者裁定的正史，
            # 是"世界自己在动"，用户在第一页能一眼分清（可整批撤销）。
            for f in (proj.get("facts") or []):
                f["fact_status"] = "derived"
                f["classified_by"] = "python"
            applied = applier.apply_projection(
                db, nid, proj, tick=new_tick, chapter_ref=chapter_ref,
                is_background=True, link_chapter=False)
            # 给事件回填 tick（apply_projection 已写 canonical/immutable/branch_id）
            for eid in applied.get("event_ids") or []:
                try:
                    db.execute("UPDATE events SET is_background=1, tick=? "
                               "WHERE id=?", (new_tick, eid))
                except Exception:               # noqa: BLE001
                    pass
            out["event_ids"] = applied.get("event_ids") or []
            out["tick"] = new_tick

        with db.tx():
            for ev in events:
                title = ev.get("title") or ""
                if not title:
                    continue
                desc = ev.get("description") or ""
                impact = ev.get("impact_on_main") or ""
                if impact and impact not in desc:
                    desc = (desc + "｜对主线的影响：" + impact).strip("｜")
                aid = db.add_timeline(
                    nid, time_anchor=ev.get("world_time") or fallback_time or title,
                    countdown_name=ev.get("actor") or "",
                    countdown_value="",
                    description=desc,
                    time_span=time_span,
                    absolute_tick=new_tick,
                )
                out["anchors"].append(aid)

        # 背景事件推进世界时间（不然"第 10 章的战争"永远挂在同一个 tick 上）
        if events and not dry_run:
            ticktime.advance(db, nid, delta=delta, new_time=head_time or None,
                             chapter_ref=chapter_ref, reason="世界自然演化")
        out["applied"] = {"anchors": out["anchors"],
                          "events": out.get("event_ids") or []}
        return out

    def extract_timeline_anchors(self, events, chapter_ref=0):
        """从已推演出的事件里提取"将来会到期"的约定，落成时间线锚点。

        这也是原来断掉的一环：add_timeline 除了定义处**没有任何调用方**，
        timeline 表永远是空的，所以 _ctx_brief 里的【待触发的时间线】段
        从来没出现过内容。
        """
        db, nid = self.db, self.novel_id
        if not events:
            return []
        block = "\n".join(
            "%d.【%s】%s ｜ %s ｜ 结果：%s" % (
                i, ev.get("event_type") or "plot", ev.get("title") or "",
                ev.get("description") or "", ev.get("result") or "")
            for i, ev in enumerate(events, 1))
        try:
            parsed, raw, mode = self.router.run_json(
                "world_sim", schema=P.TIMELINE_GEN_SCHEMA,
                system=P.TIMELINE_GEN_SYSTEM,
                user=P.timeline_gen_user(self._ctx_brief(), block),
                max_tokens=2048)
        except Exception as e:                      # noqa: BLE001
            LLM.note("⚠ 时间线锚点提取失败（不影响推演）：%s" % e)
            return []
        anchors = (parsed or {}).get("anchors") or []
        created = []
        cur = ticktime.current_tick(db, nid)
        with db.tx():
            for a in anchors:
                anchor = str(a.get("time_anchor") or "").strip()
                if not anchor:
                    continue
                # 同名 pending 锚点不重复建
                dup = db.one(
                    "SELECT id FROM timeline WHERE novel_id=? AND time_anchor=? "
                    "AND status='pending'", (nid, anchor))
                if dup:
                    continue
                # v6.7：把"还有多久到期"换成一个**绝对 tick**。
                # 模型给的 time_span / countdown_value 是自由文本（"三天后"、
                # "一周"），走规则表换算成整数——**不调 LLM**。
                # 换算不出就留 0，该锚点退回"让模型判断"的兼容路径。
                span_text = str(a.get("time_span") or a.get("countdown_value") or "")
                span = ticktime.parse_delta(span_text) if span_text else 0
                created.append(db.add_timeline(
                    nid, time_anchor=anchor,
                    countdown_name=str(a.get("countdown_name") or ""),
                    countdown_value=str(a.get("countdown_value") or ""),
                    description=str(a.get("description") or ""),
                    time_span=str(a.get("time_span") or ""),
                    absolute_tick=(cur + span) if span else 0,
                    tick_span=span,
                ))
        if created:
            LLM.note("✓ 记下 %d 个将来会到期的约定（世界页可见）" % len(created))
        return created

    def expire_due_anchors(self, events, chapter_ref=0):
        """推演之后：把已经到期的／已经应验的时间线锚点标成 triggered。

        v6.7 口径（这是 tick 化最直接的收益）：
          ① **有绝对 tick 的锚点走一条 SQL**——
             `WHERE status='pending' AND absolute_tick>0 AND absolute_tick<=?`
             结果**确定、零 LLM 调用**。老做法是"让模型判断哪条约定到期了"，
             同一输入两次调用可能给出不同答案，每个 tick 还白花一次调用。
          ② `absolute_tick=0` 的老锚点（换算不出，或迁移前的数据）
             继续走模型判断的兼容路径——**不能一刀切丢掉**，
             否则老作品里挂着的约定永远不会应验。

        返回被标记的锚点 id 列表。
        """
        db, nid = self.db, self.novel_id
        hit = []
        cur = ticktime.current_tick(db, nid)

        # ① 确定性路径：tick 比较
        try:
            rows = db.query(
                "SELECT id FROM timeline WHERE novel_id=? AND status='pending' "
                "AND absolute_tick>0 AND absolute_tick<=?",
                (nid, cur))
            with db.tx():
                for r in rows:
                    db.execute(
                        "UPDATE timeline SET status='triggered', "
                        "trigger_chapter=? WHERE id=? AND status='pending'",
                        (int(chapter_ref or 0), r["id"]))
                    hit.append(r["id"])
        except Exception as e:                  # noqa: BLE001
            LLM.note("⚠ tick 到期判定异常（已跳过）：%s" % e)

        # ② 兼容路径：没有 tick 的老锚点仍靠模型判断
        legacy = [t for t in db.list_timeline(nid, status="pending")
                  if not t.get("absolute_tick")]
        if not legacy or not events:
            if hit:
                LLM.note("✓ %d 个时间线约定到期/应验了" % len(hit))
            return hit

        block = "\n".join(
            "%d.【%s】%s" % (i, ev.get("title") or "",
                            (ev.get("description") or "")[:100])
            for i, ev in enumerate(events, 1))
        anchors = "\n".join(
            "%d. 时间锚点「%s」｜%s" % (i, t.get("time_anchor") or "",
                                      t.get("description") or "")
            for i, t in enumerate(legacy, 1))
        prompt = ("下面左边是本次推演出的事件，右边是之前挂着的"
                  "「将来会到期的约定」。请判断哪些约定**已经在这批事件里应验/"
                  "触发/作废了**。只返回应验的锚点序号，没有就给空数组。\n\n"
                  "## 本次事件\n%s\n\n## 挂着的时间线锚点\n%s" % (block, anchors))
        try:
            parsed, raw, mode = self.router.run_json(
                "world_sim",
                schema={"type": "object", "properties": {
                    "triggered": {"type": "array", "items": {"type": "integer"}}},
                    "required": ["triggered"]},
                system="你判断小说里的时间线约定是否已经应验。只输出序号数组，"
                       "用 JSON 对象：{\"triggered\": [1,3]}。",
                user=prompt, max_tokens=512)
        except Exception:                            # noqa: BLE001
            return hit
        idxs = (parsed or {}).get("triggered") or []
        with db.tx():
            for i in idxs:
                try:
                    i = int(i)
                except (TypeError, ValueError):
                    continue
                if 1 <= i <= len(legacy):
                    tid = legacy[i - 1]["id"]
                    db.execute(
                        "UPDATE timeline SET status='triggered', trigger_chapter=? "
                        "WHERE id=?", (int(chapter_ref or 0), tid))
                    hit.append(tid)
        if hit:
            LLM.note("✓ %d 个时间线约定应验了" % len(hit))
        return hit

    # ------------------------------------------------ 完结判定

    def check_completion(self, force=False):
        """三种完结条件（方案 8）：
        1. 用户设定的结构化目标达成
        2. AI 三问判定
        3. 两者兼用（completion_mode='both'）
        """
        db, nid = self.db, self.novel_id
        n = db.get_novel(nid)
        mode = n.get("completion_mode") or "ai"
        result = {"mode": mode, "goal_hits": [], "ai_verdict": None,
                  "should_complete": False, "reason": ""}

        # --- 结构化目标
        goals = db.list_world_goals(nid, status="active")
        for g in goals:
            hit = evaluate_condition(db, nid, g.get("condition_expr") or "")
            if hit:
                result["goal_hits"].append({
                    "id": g["id"], "title": g["title"],
                    "condition": g["condition_expr"]})
                db.update_world_goal(g["id"], status="achieved",
                                     progress=100,
                                     achieved_chapter=n.get("current_chapter") or 0)

        # --- AI 三问
        if mode in ("ai", "both"):
            try:
                result["ai_verdict"] = self._ai_verdict()
            except Exception as e:
                result["ai_verdict"] = {"error": str(e)}

        ai_yes = bool((result["ai_verdict"] or {}).get("should_complete"))
        goal_yes = bool(result["goal_hits"])

        if mode == "ai":
            result["should_complete"] = ai_yes
            result["reason"] = "AI 判定" if ai_yes else "AI 判定尚未到终点"
        elif mode == "goal":
            result["should_complete"] = goal_yes
            result["reason"] = "达成 %d 个设定目标" % len(result["goal_hits"]) \
                if goal_yes else "设定目标尚未达成"
        else:  # both
            result["should_complete"] = ai_yes and goal_yes
            result["reason"] = "目标达成且 AI 认可" if result["should_complete"] \
                else "需「目标达成」与「AI 判定」同时满足"
        return result

    def _ai_verdict(self):
        db, nid = self.db, self.novel_id
        states = db.get_world_state(nid)
        state_brief = "；".join("%s=%s" % (s["key"], s["value"]) for s in states[:25]) \
            or "（无状态量）"
        goals = db.list_world_goals(nid, status="active")
        goal_lines = ["- [%s] %s%s" % (
            g["goal_type"], g["title"],
            ("（条件：%s）" % g["condition_expr"]) if g["condition_expr"] else "")
            for g in goals]
        cgoals = db.list_goals(novel_id=nid, status="active")
        for g in cgoals[:15]:
            goal_lines.append("- [角色-%s] %s（进度 %s%%）" % (
                g.get("character_name") or "?", g["content"], g["progress"]))
        goals_brief = "\n".join(goal_lines) or "（无目标）"

        recent = db.query(
            "SELECT title, description FROM events WHERE novel_id=? "
            "AND deleted_at IS NULL ORDER BY id DESC LIMIT 15", (nid,))
        recent.reverse()
        recent_brief = "\n".join("- %s：%s" % (e["title"], (e["description"] or "")[:70])
                                 for e in recent) or "（无事件）"

        parsed, raw, mode = self.router.run_json(
            "completion_judge", schema=P.COMPLETION_JUDGE_SCHEMA,
            system=P.COMPLETION_JUDGE_SYSTEM,
            user=P.completion_judge_user(state_brief, goals_brief, recent_brief),
            max_tokens=2048)
        return parsed


# ============================================================ 条件表达式求值

def evaluate_condition(db, novel_id, expr):
    """求值结构化完结条件。

    支持形式（安全子集，不用 eval）：
      world.<状态key> == <值>
      world.<key> >= <数字>   <=  >  <  !=
      character.<角色名>.<状态key> == <值>
      thread.<线索标题> == resolved
      goal.<目标id> == achieved
    """
    if not expr or not expr.strip():
        return False
    text = expr.strip()
    for op in ("==", "!=", ">=", "<=", ">", "<"):
        idx = text.find(op)
        if idx > 0:
            left = text[:idx].strip()
            right = text[idx + len(op):].strip()
            return _compare(db, novel_id, left, op, right)
    # 无运算符：视为"存在性 / 布尔真"
    return bool(_resolve_operand(db, novel_id, text))


def _unquote(s):
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        return s[1:-1]
    return s


def _resolve_operand(db, novel_id, token):
    """解析左值。返回 (found, value) 语义上直接返回值或 None。"""
    t = _unquote(token)
    parts = t.split(".")

    if parts[0] == "world" and len(parts) >= 2:
        key = ".".join(parts[1:])
        val = db.get_state_value(novel_id, key)
        if val is not None:
            return val
        # 兼容键名带点号但被 split 的情况：先查整串
        return db.get_state_value(novel_id, t[len("world."):])

    if parts[0] == "character" and len(parts) >= 3:
        cname = parts[1]
        ckey = ".".join(parts[2:])
        row = db.get_character_by_name(novel_id, cname)
        if not row:
            return None
        mapping = {
            "status": row.get("status"),
            "rank": row.get("rank"),
            "location": row.get("current_location"),
        }
        if ckey in mapping:
            return mapping[ckey]
        attrs = row.get("attributes") or {}
        return attrs.get(ckey)

    if parts[0] == "thread" and len(parts) >= 2:
        title = t[len("thread."):]
        row = db.one(
            "SELECT status FROM foreshadowing WHERE novel_id=? AND title=? "
            "ORDER BY id DESC LIMIT 1", (novel_id, title))
        return row["status"] if row else None

    if parts[0] == "goal" and len(parts) >= 2:
        gid = parts[1]
        if str(gid).isdigit():
            row = db.one("SELECT status FROM world_goals WHERE id=?", (int(gid),))
            return row["status"] if row else None

    if parts[0] == "fact" and len(parts) >= 3:
        # v6.7：允许按正史事实写完结条件，例如
        #   fact.老周.存活 == false
        # 这是"完结条件"从"世界状态"升级到"正史账本"的那一步——
        # world_state 只是投影缓存，用它写完结条件会在手工锁定时永远不触发。
        subj = parts[1]
        pred = ".".join(parts[2:])
        # 按**名字**查，不能只按谓词查（那会撞上同谓词的其他主体）。
        for stype in ("character", "entity"):
            row = db.one(
                "SELECT * FROM facts WHERE novel_id=? AND subject_type=? "
                "AND subject_name=? AND predicate=? AND status='active' "
                "ORDER BY id DESC LIMIT 1", (novel_id, stype, subj, pred))
            if row:
                return row.get("object_text")
        return None

    if parts[0] == "count":
        # count.goal.active / count.thread.active 等便捷统计
        if len(parts) >= 3 and parts[1] == "goal":
            col = "status='active'" if parts[2] == "active" else "1=1"
            return db.scalar(
                "SELECT COUNT(*) FROM character_goals WHERE novel_id=? AND %s"
                % col, (novel_id,), default=0)
        if len(parts) >= 3 and parts[1] == "thread":
            col = "status='active'" if parts[2] == "active" else "1=1"
            return db.scalar(
                "SELECT COUNT(*) FROM foreshadowing WHERE novel_id=? AND %s"
                % col, (novel_id,), default=0)
    return None


def _coerce(right):
    r = _unquote(right)
    low = r.lower()
    if low in ("true", "yes", "是"):
        return True
    if low in ("false", "no", "否"):
        return False
    if low in ("null", "none"):
        return None
    try:
        if "." in r:
            return float(r)
        return int(r)
    except ValueError:
        return r


def _compare(db, novel_id, left, op, right):
    lv = _resolve_operand(db, novel_id, left)
    rv = _coerce(right)
    if lv is None:
        # 左值不存在：只有 != 和 == 空值有意义
        if op == "==":
            return rv is None or rv == ""
        if op == "!=":
            return not (rv is None or rv == "")
        return False
    # 布尔/字符串比较
    if isinstance(rv, bool) or isinstance(lv, bool):
        lb = lv if isinstance(lv, bool) else str(lv).lower() in ("1", "true", "yes", "是")
        rb = rv if isinstance(rv, bool) else str(rv).lower() in ("1", "true", "yes", "是")
        return {"==": lb == rb, "!=": lb != rb}.get(op, False)
    # 数字比较
    try:
        ln, rn = float(lv), float(rv)
        return {"==": ln == rn, "!=": ln != rn, ">=": ln >= rn,
                "<=": ln <= rn, ">": ln > rn, "<": ln < rn}[op]
    except (TypeError, ValueError):
        ls, rs = str(lv), str(rv)
        return {"==": ls == rs, "!=": ls != rs, ">=": ls >= rs,
                "<=": ls <= rs, ">": ls > rs, "<": ls < rs}.get(op, False)
