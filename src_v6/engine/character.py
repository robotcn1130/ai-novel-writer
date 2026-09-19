# -*- coding: utf-8 -*-
"""
角色代理层：让每个角色按自己的目标与性格行动。

核心机制（借鉴 EvolvingWorld）：
  - 角色决策的输入是"他自己的目标 + 他掌握的信息"，不是全局真相
    -> 这天然产生"信息差"，而信息差是真实感的主要来源
  - 性格变化走 hidden tracker 累积，不突变（见 engine.guard）
  - 记忆流决定"他知道什么"，防止角色知道不该知道的
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC_V6 = os.path.dirname(_HERE)
for _p in (_SRC_V6, os.path.join(_SRC_V6, "core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from engine import prompts as P


class CharacterAgent:
    """单个角色的决策代理。"""

    def __init__(self, db, router, novel_id, character):
        self.db = db
        self.router = router
        self.novel_id = novel_id
        self.character = character

    # ------------------------------------------------ 资料块

    def _char_block(self):
        c = self.character
        L = ["姓名：%s%s" % (c["name"],
                            ("（%s）" % c["alias"]) if c.get("alias") else "")]
        if c.get("role_tag"):
            L.append("身份：%s" % c["role_tag"])
        if c.get("rank"):
            L.append("在故事中的分量：%s" % c["rank"])
        if c.get("personality"):
            L.append("性格：%s" % c["personality"])
        if c.get("background"):
            L.append("背景：%s" % c["background"])
        if c.get("speech_style"):
            L.append("说话方式：%s" % c["speech_style"])
        if c.get("decision_tendency"):
            L.append("决策倾向：%s" % c["decision_tendency"])
        if c.get("current_location"):
            L.append("当前所在：%s" % c["current_location"])

        # tracker：已内化的性格维度 + 正在累积的
        tracker = c.get("tracker") or {}
        if tracker:
            L.append("性格状态：")
            for k, v in tracker.items():
                pend = int(v.get("pending", 0))
                note = ""
                if pend:
                    note = "（正在累积%s，尚未定型）" % (
                        "上升" if pend > 0 else "下降")
                L.append("  - %s：%s/100%s" % (k, v.get("value", 0), note))

        # 目标
        goals = self.db.list_goals(character_id=c["id"], status="active")
        if goals:
            L.append("当前目标：")
            for g in goals:
                tag = "长期" if g["goal_type"] == "long" else "短期"
                line = "  - [%s][优先级%s] %s" % (tag, g["priority"], g["content"])
                if g.get("motivation"):
                    line += "（因为：%s）" % g["motivation"]
                if g.get("obstacle"):
                    line += "（障碍：%s）" % g["obstacle"]
                L.append(line)
        else:
            L.append("当前目标：没有明确目标（可依本能与处境行事）")

        return "\n".join(L)

    def _knowledge_block(self, max_items=25):
        """他掌握的信息 = 自己的记忆流 + 世界公开事实。

        注意：这里刻意不给他"全局真相"，制造信息差。
        """
        L = []
        mem = self.character.get("memory") or []
        if mem:
            L.append("你亲历/知道的事：")
            for m in mem[-max_items:]:
                if isinstance(m, dict):
                    L.append("  - %s" % m.get("text", ""))
                else:
                    L.append("  - %s" % m)
        else:
            L.append("你还没有积累起关于这件事的记忆。")

        # 公开可见的世界事实（visibility=public 的实体）
        public = self.db.query(
            "SELECT name, description FROM world_entities WHERE novel_id=? "
            "AND status='active' AND visibility='public' AND deleted_at IS NULL "
            "LIMIT 10", (self.novel_id,))
        if public:
            L.append("众所周知的事：")
            for e in public:
                L.append("  - %s：%s" % (e["name"], (e["description"] or "")[:60]))

        # 已公开的线索
        threads = self.db.list_threads(self.novel_id, status="active")
        if threads:
            L.append("你注意到但还没弄清的事：")
            for t in threads[:6]:
                L.append("  - %s" % t["title"])
        return "\n".join(L)

    def _relations_block(self):
        """"你与这些人的关系"——喂给角色自己做决策用。

        踩过的坑（方向失明）：list_relations(character_id=X) 的 WHERE 是
        `a_id=? OR b_id=?`，所以一条「沈砚 →（单向）白鹤楼」的记录，
        白鹤楼查自己的关系时**也会命中**（因为他的 id 出现在 b_id 上）。
        原来这里只算"对面是谁"、不看方向，于是白鹤楼会读到
        「沈砚：rival（强度3）」——他会以为自己跟沈砚是竞争关系，
        而真相是他压根不知道沈砚在查他。方向一丢，单向关系的戏剧价值
        就反过来了：该蒙在鼓里的人变得心知肚明。

        现在按方向分开说：
          - 以我为主语（a_id == 我）→「你对 TA」，关系是我的态度；
          - 以我为宾语（b_id == 我）→「TA 对你」，且要提醒"你可能并不知情"。

        还有一层更隐蔽的坑：**描述也是从 a 的视角写的**。
        落库时 `description` 是模型站在 A 的立场写的（"沈砚在查他，
        白鹤楼毫无察觉"）——这句话直接摆给白鹤楼，等于当面告诉他有人在查他，
        "不知情"的前提当场破功。所以被指向的那一方（b_id == 我）只给
        类型/强度/状态这类**客观事实**，描述一律 `filtered` 掉，
        换成"你暂时还说不出对方为什么这样对你"。
        双向关系（落了两条）不存在这个问题：我这边的记录描述本来就是
        以我为视角写的，照常展示。

        双向关系会落两条（见 apply_relations），两边各读到一条正向表述，
        不会重复也不会拧巴。
        """
        me = self.character["id"]
        rels = self.db.list_relations(self.novel_id, character_id=me)
        if not rels:
            return ""
        # 哪些"对方→我"的配对是双向的（我这边也存在一条主动记录）？
        # 双向时我早就知道对方的态度，描述可以照给；单向时我是被动方，
        # 描述必须屏蔽（见上文 docstring 的视角泄漏说明）。
        my_pairs = {(r["character_b_id"], r["relation_type"])
                    for r in rels if r["character_a_id"] == me}
        mine, theirs = [], []
        for r in rels:
            out = bool(r["character_a_id"] == me)
            other = r["name_b"] if out else r["name_a"]
            if not other:
                continue
            status = r.get("status") or "active"
            tail = "" if status == "active" else "（已%s）" % (
                {"broken": "破裂", "evolved": "变化"}.get(status, status))
            if out:
                desc = ("，%s" % r["description"]) if r.get("description") else ""
            elif (r["character_a_id"], r["relation_type"]) in my_pairs:
                # 双向：我这边也有一条记录，说明我清楚这段关系
                desc = ("，%s" % r["description"]) if r.get("description") else ""
            else:
                desc = "，你暂时还说不出对方为什么这样对你"
            line = "  - %s：%s（强度%s）%s%s" % (
                other, _RELATION_LABELS.get(r["relation_type"],
                                            r["relation_type"]),
                r["intensity"], desc, tail)
            (mine if out else theirs).append(line)
        L = []
        if mine:
            L.append("你对这些人的关系（这是你的态度与立场）：")
            L.extend(mine)
        if theirs:
            L.append("这些人对你的关系（**你未必知情**——这些态度是从对方那边"
                     "产生的，除非你自己已经察觉，否则不要表现出你知道）：")
            L.extend(theirs)
        return "\n".join(L)

    # ------------------------------------------------ 决策

    def decide(self, situation, constraints=""):
        """让角色自己做决定，返回结构化结果。"""
        blocks = [self._char_block()]
        rel = self._relations_block()
        if rel:
            blocks.append(rel)
        char_block = "\n\n".join(blocks)

        parsed, raw, mode = self.router.run_json(
            "character_decide",
            schema=P.CHARACTER_DECIDE_SCHEMA,
            system=P.CHARACTER_DECIDE_SYSTEM,
            user=P.character_decide_user(
                char_block, situation, self._knowledge_block(), constraints),
            max_tokens=2048,
        )
        if not parsed:
            return {"action": "", "reasoning": "决策解析失败：%s" % (raw or "")[:150],
                    "intent": "", "_failed": True}
        parsed["_name"] = self.character["name"]
        return parsed

    def may_act(self, situation_prompt="", min_importance=3):
        """判断这个角色在当前局面下是否"会主动做点什么"。

        用于避免每章把所有角色都推一遍（那既费钱又不真实）。
        """
        goals = self.db.list_goals(character_id=self.character["id"], status="active")
        if not goals:
            return {"will_act": False, "reason": "无目标，缺乏行动驱动"}
        top = max(int(g["priority"]) for g in goals)
        if top < min_importance:
            return {"will_act": False, "reason": "目标优先级不足"}
        return {"will_act": True, "reason": "有优先级 %s 的目标在推动" % top}


# ============================================================ 批量决策

class CharacterDirector:
    """协调多个角色的决策，并处理"谁先动"的顺序问题。"""

    def __init__(self, db, router, novel_id):
        self.db = db
        self.router = router
        self.novel_id = novel_id

    def agents_for(self, character_ids=None, include_ai=True):
        """选取参与本轮决策的角色。"""
        chars = self.db.list_characters(self.novel_id, status="alive")
        if character_ids:
            chars = [c for c in chars if c["id"] in character_ids]
        if not include_ai:
            chars = [c for c in chars if c.get("control_mode") != "ai"]
        return [CharacterAgent(self.db, self.router, self.novel_id, c) for c in chars]

    def decide_all(self, situations, by_priority=True):
        """让多个角色各自决策。

        situations: {character_id 或 name: 该角色面对的局面描述}
        返回 [{'name', 'action', 'reasoning', 'intent', ...}]
        """
        agents = self.agents_for(list(situations.keys()) if situations else None)
        if by_priority:
            agents.sort(
                key=lambda a: -max([int(g["priority"]) for g in
                                    self.db.list_goals(character_id=a.character["id"],
                                                       status="active")] or [0]))
        out = []
        for a in agents:
            key = a.character["id"]
            sit = situations.get(key) or situations.get(a.character["name"]) or ""
            if not sit:
                continue
            out.append(a.decide(sit))
        return out

    def should_ai_decide(self, character_row):
        """是否需要 AI 代为决策（用户托管的角色跳过）。"""
        mode = character_row.get("control_mode") or "ai"
        return mode != "user"


# ============================================================ 新角色

def create_character_from_scene(db, router, novel_id, name, context_text,
                                chapter_ref=0):
    """章节中新出现的人物，让 AI 依据出场场景建档（方案 6.5）。

    返回新建的角色 id（若已存在则返回现有 id）。
    """
    existing = db.get_character_by_name(novel_id, name)
    if existing:
        return existing["id"]

    schema = {
        "type": "object",
        "properties": {
            "role_tag": {"type": "string"},
            "personality": {"type": "string"},
            "decision_tendency": {"type": "string"},
            "rank_hint": {"type": "string", "enum": ["B", "C", "D", "E"]},
            "speech_style": {"type": "string"},
            "probable_goal": {"type": "string"}
        },
        "required": ["role_tag", "personality"]
    }
    parsed, raw, mode = router.run_json(
        "state_extract", schema=schema,
        system="你根据一个人在小说中的出场片段，为他建立最简档案。"
               "只根据片段里表现出的信息推断，不要编造背景。",
        user="## 出场片段\n%s\n\n## 人物\n%s\n\n请给出他的档案。" % (
            context_text[:3000], name),
        max_tokens=1024)

    if not parsed:
        cid = db.add_character(novel_id, name, rank="C",
                               first_appear_chapter=chapter_ref)
        return cid

    cid = db.add_character(
        novel_id, name,
        rank=parsed.get("rank_hint") or "C",
        role_tag=parsed.get("role_tag") or "",
        personality=parsed.get("personality") or "",
        decision_tendency=parsed.get("decision_tendency") or "",
        speech_style=parsed.get("speech_style") or "",
        first_appear_chapter=chapter_ref,
    )
    if parsed.get("probable_goal"):
        db.add_goal(novel_id, cid, parsed["probable_goal"], goal_type="short",
                    origin="ai_generated", created_chapter=chapter_ref)
    return cid


# ============================================================ 初始阵容落库

_RANK_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}


def _control_for(decision_mode, is_protagonist):
    """人机分工 -> 角色托管模式。

    director  主角归用户，其余 AI（这是 director 模式的锚点）
    tabletop  全部归用户
    viewer    全部 AI
    """
    if decision_mode == "viewer":
        return "ai"
    if decision_mode == "tabletop":
        return "user"
    return "user" if is_protagonist else "ai"


def pick_protagonist(characters):
    """从阵容草案里挑一个主角：优先看 is_protagonist，否则取分量最高者。"""
    chars = [c for c in (characters or [])
             if isinstance(c, dict) and str(c.get("name") or "").strip()]
    if not chars:
        return ""
    for c in chars:
        if c.get("is_protagonist"):
            return str(c["name"]).strip()
    # 比分量之前必须先把 rank 收口：模型最爱给主角写 "S"，而 _RANK_ORDER 只有
    # A~E，直接 get("S", 9) 会落到兜底 9 —— 主角就被判给写 "A" 的配角了。
    # 用 db 层同一套词表，两处口径永远不会漂。
    from core import database as DB
    best = min(chars, key=lambda c: _RANK_ORDER.get(
        DB._norm_rank(c.get("rank"), "C"), 9))
    return str(best["name"]).strip()


_RELATION_LABELS = {
    "family": "亲属", "friend": "朋友", "enemy": "敌对", "lover": "恋慕",
    "colleague": "同僚", "mentor": "师徒", "rival": "竞争", "other": "其他",
}


def apply_relations(db, novel_id, relations, chapter_ref=0):
    """把结构化关系落库。

    踩过的坑（真实断裂）：character_relations 表建了，db.add_relation 也写了，
    CharacterAgent._relations_block() 还会把关系拼进"角色自己做决定"的提示词
    —— 但**全项目没有任何一处调用 add_relation**。结果就是关系段永远是空的，
    AI 决策时默认"这些人互相都认识、都能直接对话"，于是推演里陌生人开口就像老友，
    人物动机也缺少"他在意谁、防着谁"这层。

    这里负责补上写入。双向默认落**两条**（A→B 与 B→A），
    因为 `list_relations(character_id=...)` 是单向查询：
    只落一条时，B 的提示词里就看不到 A 了，仍是半个关系网。
    is_mutual=False 的单向关系只落 A→B 那一条，保留不对称。

    幂等靠 UNIQUE(novel_id, a_id, b_id, relation_type) + ON CONFLICT 覆盖。
    返回落库的关系条数。
    """
    by_name = {}
    for c in db.list_characters(novel_id):
        by_name[str(c["name"]).strip()] = c["id"]

    n = 0
    with db.tx():
        for r in (relations or []):
            if not isinstance(r, dict):
                continue
            a_name = str(r.get("a") or "").strip()
            b_name = str(r.get("b") or "").strip()
            if not a_name or not b_name or a_name == b_name:
                continue
            a_id, b_id = by_name.get(a_name), by_name.get(b_name)
            # 有一次推演里模型写了个没建档的名字：跳过而不是抛，
            # 一条脏关系不该把整份阵容回滚掉。
            if not a_id or not b_id:
                continue
            rtype = r.get("relation_type") or "other"
            if rtype not in _RELATION_LABELS:
                rtype = "other"
            desc = str(r.get("description") or "")
            intensity = r.get("intensity") or 3
            # change=broken 时把关系标成破裂（关系还在，但已经不是原来的样子）
            change = str(r.get("change") or "").strip()
            status = "broken" if change == "broken" else (
                "evolved" if change == "evolved" else "active")
            db.add_relation(novel_id, a_id, b_id, relation_type=rtype,
                            description=desc, intensity=intensity,
                            updated_chapter=chapter_ref, status=status)
            n += 1
            # 双向（默认）：补一条反方向，否则另一边的角色看不到这段关系
            if r.get("is_mutual", True):
                db.add_relation(novel_id, b_id, a_id, relation_type=rtype,
                                description=desc, intensity=intensity,
                                updated_chapter=chapter_ref, status=status)
                n += 1
    return n


def collect_dangling_relations(db, novel_id, relations):
    """找出引用了"没建档角色"的关系，返回缺的名字列表。

    用于把模型写错的名字报给用户（而不是静默丢掉），
    用户可以在阵容步骤里补上这个人，再重放一次。
    """
    known = {str(c["name"]).strip() for c in db.list_characters(novel_id)}
    missing = []
    for r in (relations or []):
        if not isinstance(r, dict):
            continue
        for k in ("a", "b"):
            nm = str(r.get(k) or "").strip()
            if nm and nm not in known and nm not in missing:
                missing.append(nm)
    return missing


def apply_cast(db, novel_id, characters, decision_mode="director",
               chapter_ref=0, origin="init", relations=None):
    """把一份角色阵容落库（创建世界 / 手动补充阵容用）。

    幂等：同名角色已存在就跳过，不会重复建档，也不会重复加目标。
    relations 给出时会顺手把关系网落库（见 apply_relations）。
    返回 {'created': [...], 'skipped': [...], 'protagonist': '名字',
          'relations': 落库条数, 'dangling_relations': [...]}
    """
    chars = [c for c in (characters or [])
             if isinstance(c, dict) and str(c.get("name") or "").strip()]
    protagonist = pick_protagonist(chars)
    created, skipped = [], []

    # 整份阵容一次落库：中途出错整体回滚，不留"建了一半"的阵容
    # （rank / goal_type / priority 这类枚举列由 db 写入端统一收口，
    #  见 core/database.py 的 _norm_rank / _norm_enum / _to_int）
    with db.tx():
        for ch in chars:
            name = str(ch.get("name") or "").strip()
            is_lead = (name == protagonist)
            existing = db.get_character_by_name(novel_id, name)
            if existing:
                # 已建档：只保证主角的托管模式正确，不再重复加目标
                if is_lead and (existing.get("control_mode") or "ai") != "user" \
                        and decision_mode != "viewer":
                    db.update_character(existing["id"], control_mode="user")
                skipped.append(name)
                continue
            cid = db.add_character(
                novel_id, name,
                rank=ch.get("rank") or "C",
                role_tag=ch.get("role_tag") or "",
                personality=ch.get("personality") or "",
                background=ch.get("background") or "",
                speech_style=ch.get("speech_style") or "",
                decision_tendency=ch.get("decision_tendency") or "",
                control_mode=_control_for(decision_mode, is_lead),
                current_location=ch.get("location") or "",
                first_appear_chapter=chapter_ref)
            for g in (ch.get("goals") or []):
                if isinstance(g, str):
                    if g.strip():
                        db.add_goal(novel_id, cid, g, origin=origin)
                    continue
                if not str(g.get("content") or "").strip():
                    continue
                db.add_goal(novel_id, cid, g["content"],
                            goal_type=g.get("goal_type", "short"),
                            motivation=g.get("motivation", ""),
                            obstacle=g.get("obstacle", ""),
                            priority=g.get("priority") or 3,
                            origin=origin, created_chapter=chapter_ref)
            created.append(cid)

    # 关系网必须等角色都建完再落（要靠名字查 id）
    rel_count = 0
    if relations:
        rel_count = apply_relations(db, novel_id, relations, chapter_ref=chapter_ref)

    return {"created": created, "skipped": skipped, "protagonist": protagonist,
            "relations": rel_count,
            "dangling_relations": collect_dangling_relations(
                db, novel_id, relations or [])}
