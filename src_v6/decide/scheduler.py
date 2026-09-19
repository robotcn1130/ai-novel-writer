# -*- coding: utf-8 -*-
"""
决策调度层：决定"什么时候停下来问人"。

三种托管模式（方案 6.2）：
  viewer    观影模式：全 AI 决策，用户只看
  director  导演模式（默认）：主角归用户，其余 AI
  tabletop  跑团模式：全用户决策

决策点四种触发（方案 6.1）：
  moral         道德两难
  cost          重大代价
  irreversible  不可逆行动
  user_focus    用户特别关注的角色/线索

选项三原则（方案 6.3）：
  - 必须标后果倾向（不剧透，只给方向感）
  - 至少 3 个实质选项，且不能有明显最优解
  - 永远允许自由输入
"""
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC_V6 = os.path.dirname(_HERE)
for _p in (_SRC_V6, os.path.join(_SRC_V6, "core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from engine import prompts as P

TRIGGER_LABELS = {
    "moral": "道德两难",
    "cost": "重大代价",
    "irreversible": "不可逆行动",
    "user_focus": "你关注的焦点",
}
MODE_LABELS = {
    "viewer": "观影（全 AI 决策）",
    "director": "导演（主角归你）",
    "tabletop": "跑团（全部你来定）",
}


class DecisionScheduler:
    """决策点的生成、托管判定与落定。"""

    def __init__(self, db, router, novel_id):
        self.db = db
        self.router = router
        self.novel_id = novel_id

    # ------------------------------------------------ 托管判定

    def needs_user(self, decision):
        """这个决策点是否应该停下来问用户。"""
        novel = self.db.get_novel(self.novel_id)
        mode = (novel or {}).get("decision_mode") or "director"

        if mode == "viewer":
            return False
        if mode == "tabletop":
            return True

        # director：主角归用户
        actor_name = decision.get("actor_name") or ""
        char = self.db.get_character_by_name(self.novel_id, actor_name)
        if char and char.get("control_mode") == "user":
            return True
        if char and char.get("control_mode") == "ai":
            return False
        # 没有对应角色（世界级决策）时，让用户决定
        return char is None and decision.get("actor_type") == "world"

    def set_character_control(self, character_id, control_mode):
        """切换某角色的托管模式。"""
        if control_mode not in ("ai", "user", "auto_delegate"):
            raise ValueError("control_mode 非法：%s" % control_mode)
        self.db.update_character(character_id, control_mode=control_mode)
        return self.db.get_character(character_id)

    def bulk_set_control(self, control_mode, character_ids=None):
        """批量设定托管（用于章节开头的选角环节）。"""
        chars = self.db.list_characters(self.novel_id, status="alive")
        if character_ids:
            chars = [c for c in chars if c["id"] in character_ids]
        for c in chars:
            self.db.update_character(c["id"], control_mode=control_mode)
        return len(chars)

    # ------------------------------------------------ 选项生成

    def generate_options(self, decision_id, count_hint=3):
        """为决策点生成选项。"""
        d = self.db.get_decision(decision_id)
        if not d:
            raise ValueError("决策点不存在：%s" % decision_id)

        actor_block = d["actor_name"] or "某个角色"
        char = self.db.get_character_by_name(self.novel_id, d["actor_name"]) \
            if d["actor_name"] else None
        if char:
            actor_block = "%s（%s；%s）" % (
                char["name"], char.get("role_tag") or "无身份",
                char.get("personality") or "性格未定义")
            goals = self.db.list_goals(character_id=char["id"], status="active")
            if goals:
                actor_block += "\n他的目标：" + "；".join(g["content"] for g in goals[:3])

        parsed, raw, mode = self.router.run_json(
            "option_gen", schema=P.OPTION_GEN_SCHEMA,
            system=P.OPTION_GEN_SYSTEM,
            user=P.option_gen_user(d["situation"], actor_block, d["stakes"]),
            max_tokens=2048)

        options = (parsed or {}).get("options") or []
        if len(options) < 3:
            # 保底：至少给 3 个可用的
            options = _fallback_options(d)
        options = [_normalize_option(o) for o in options[:5]]

        self.db.execute(
            "UPDATE decision_points SET options=?, allow_freeform=1 WHERE id=?",
            (json.dumps(options, ensure_ascii=False), decision_id))
        return options

    # ------------------------------------------------ 落定

    def resolve(self, decision_id, option_index=None, freeform="",
                by="user"):
        """落定一个决策点。

        option_index 为 None 且 freeform 非空 -> 自由输入
        """
        d = self.db.get_decision(decision_id)
        if not d:
            raise ValueError("决策点不存在：%s" % decision_id)
        opts = d.get("options") or []

        chosen_content = ""
        if option_index is not None:
            if not (0 <= int(option_index) < len(opts)):
                raise ValueError("选项下标越界：%s（共 %d 个）"
                                 % (option_index, len(opts)))
            o = opts[int(option_index)]
            chosen_content = "%s：%s" % (o.get("label", ""), o.get("description", ""))
            option_index = int(option_index)
        else:
            if not freeform.strip():
                raise ValueError("必须选择一个选项或输入自定义内容")
            chosen_content = freeform.strip()
            option_index = -1

        return self.db.resolve_decision(
            decision_id, chosen_option=option_index,
            chosen_content=chosen_content, chosen_by=by)

    def ai_decide(self, decision_id):
        """AI 托管：让角色自己选（观影模式 / 非主角决策点）。"""
        d = self.db.get_decision(decision_id)
        if not d:
            raise ValueError("决策点不存在")
        opts = d.get("options") or []
        if not opts:
            opts = self.generate_options(decision_id)
            d = self.db.get_decision(decision_id)
            opts = d.get("options") or []

        char = self.db.get_character_by_name(self.novel_id, d["actor_name"]) \
            if d["actor_name"] else None
        actor_block = "某角色"
        if char:
            actor_block = char["name"]
            if char.get("personality"):
                actor_block += "（%s）" % char["personality"]
            if char.get("decision_tendency"):
                actor_block += "，倾向于%s" % char["decision_tendency"]

        option_text = "\n".join(
            "%d. %s —— %s（%s）" % (i, o.get("label", ""), o.get("description", ""),
                                    o.get("consequence_hint", ""))
            for i, o in enumerate(opts))

        schema = {
            "type": "object",
            "properties": {
                "choice_index": {"type": "integer"},
                "reasoning": {"type": "string"}
            },
            "required": ["choice_index", "reasoning"]
        }
        parsed, raw, mode = self.router.run_json(
            "character_decide", schema=schema,
            system="你为小说角色做选择。只选**这个人真的会选的**，不要选最合理的。",
            user="## 谁在决定\n%s\n\n## 局面\n%s\n\n## 代价\n%s\n\n"
                 "## 可选项\n%s\n\n请选择并说明理由。"
                 % (actor_block, d["situation"], d["stakes"], option_text),
            max_tokens=1024)

        if not parsed:
            # 静默选 0 号选项会造成"AI 做了个看不出理由的决定"，宁可报错让人看见
            raise ValueError(
                "AI 没能给出选择（解析模式：%s）。原始输出：%s"
                % (mode, (raw or "")[:200]))
        idx = parsed.get("choice_index", 0)
        try:
            idx = int(idx)
        except (TypeError, ValueError):
            idx = 0
        if not (0 <= idx < len(opts)):
            idx = 0
        return self.db.resolve_decision(
            decision_id, chosen_option=idx,
            chosen_content="（AI 决策）%s：%s" % (
                opts[idx].get("label", ""), opts[idx].get("description", "")),
            chosen_by="ai_auto",
            impact={"reasoning": parsed.get("reasoning", "")})

    # ------------------------------------------------ 批量处理

    def process_pending(self, auto_resolve_ai=True, generate_options=True):
        """处理当前所有待决决策点。

        返回 {'needs_user': [...], 'ai_resolved': [...], 'failed': [...]}
        """
        out = {"needs_user": [], "ai_resolved": [], "failed": []}
        for item in self.db.pending_decisions(self.novel_id):
            did = item["id"]
            try:
                if generate_options:
                    d = self.db.get_decision(did)
                    if not (d.get("options") or []):
                        self.generate_options(did)
                d = self.db.get_decision(did)
                if self.needs_user(d):
                    out["needs_user"].append(_decision_view(d))
                elif auto_resolve_ai:
                    res = self.ai_decide(did)
                    out["ai_resolved"].append(_decision_view(res))
                else:
                    out["needs_user"].append(_decision_view(d))
            except Exception as e:
                out["failed"].append({"id": did, "error": str(e)})
        return out

    # ------------------------------------------------ 触发判定

    def trigger_from_events(self, events):
        """从事件流里识别值得停下来问人的岔路（补充 LLM 的 decision_needed）。

        规则（方案 6.1）：只有当事件同时具备"高重要度"和"不可逆性"信号时才触发，
        避免什么都问用户。
        """
        found = []
        for ev in events:
            try:
                importance = int(float(str(ev.get("importance") or 3).strip()))
            except (TypeError, ValueError):
                importance = 3
            etype = ev.get("event_type")
            intent = (ev.get("intent") or "")
            result = (ev.get("result") or "")
            text = intent + result

            if importance < 4:
                # 低重要度不打扰用户
                if not _mentions_user_focus_in(self.db, self.novel_id, text):
                    continue

            trigger = None
            if etype == "decision" or _has_irreversible(text):
                trigger = "irreversible"
            elif _has_cost(text):
                trigger = "cost"
            elif _mentions_user_focus_in(self.db, self.novel_id, text):
                trigger = "user_focus"

            if trigger:
                found.append({
                    "trigger_type": trigger,
                    "actor_name": ev.get("actor") or "",
                    "title": ev.get("title") or "需要决断",
                    "situation": ev.get("description") or "",
                    "stakes": result[:200],
                })
        return found


# ============================================================ 辅助

_IRREVERSIBLE_WORDS = ("毁掉", "杀死", "永久", "不可逆", "彻底", "出卖", "背叛",
                       "销毁", "泄漏", "暴露", "断交", "辞职", "处决")
_COST_WORDS = ("代价", "失去", "牺牲", "放弃", "交换", "以……换", "赌上",
               "付出", "透支", "抵押")


def _has_irreversible(text):
    return any(w in text for w in _IRREVERSIBLE_WORDS)


def _has_cost(text):
    return any(w in text for w in _COST_WORDS)


def _mentions_user_focus_in(db, novel_id, text):
    """用户关注的焦点：托管模式为 user 的角色名，或用户手动标记的线索。"""
    if not text:
        return False
    chars = db.list_characters(novel_id, status="alive")
    focus = [c["name"] for c in chars if c.get("control_mode") == "user"]
    attrs = db.get_attributes(novel_id)
    if attrs.get("focus_threads"):
        focus.extend([t.strip() for t in attrs["focus_threads"].split(",") if t.strip()])
    return any(f and f in text for f in focus)


def _normalize_option(o):
    return {
        "label": str(o.get("label") or "").strip()[:24],
        "description": str(o.get("description") or "").strip(),
        "consequence_hint": str(o.get("consequence_hint") or "").strip(),
        "tendency": str(o.get("tendency") or "").strip(),
    }


def _fallback_options(d):
    """LLM 选项生成失败时的保底三选项。"""
    return [
        {"label": "直接面对", "description": "正面处理这件事，不回避",
         "consequence_hint": "后果立刻显现，但有主动权", "tendency": "果断"},
        {"label": "暂时搁置", "description": "先放着，观察局势再定",
         "consequence_hint": "赢得时间，但问题会发酵", "tendency": "稳妥"},
        {"label": "另寻他路", "description": "绕过眼前的局面，从别处入手",
         "consequence_hint": "规避风险，但可能错失关键信息", "tendency": "迂回"},
    ]


def _decision_view(d):
    """给 UI 的决策视图。"""
    return {
        "id": d["id"],
        "title": d["title"],
        "situation": d["situation"],
        "stakes": d["stakes"],
        "actor_name": d["actor_name"],
        "trigger_type": d["trigger_type"],
        "trigger_label": TRIGGER_LABELS.get(d["trigger_type"], d["trigger_type"]),
        "options": d.get("options") or [],
        "allow_freeform": bool(d.get("allow_freeform")),
        "resolved": bool(d.get("resolved")),
        "chosen_content": d.get("chosen_content") or "",
        "chosen_by": d.get("chosen_by") or "",
        "chapter_ref": d.get("chapter_ref") or 0,
    }


def mode_label(mode):
    return MODE_LABELS.get(mode, mode)
