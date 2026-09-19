# -*- coding: utf-8 -*-
"""
剧情树引擎：推演的分支探索层（沙盘）。

和 WorldEngine 的分工（成本命门的延伸）：
  WorldEngine.advance()   一次推演 -> 直接落进 events（正史）
  StoryTree.expand()      一次推演 -> 只长在 story_nodes 上（沙盘）

为什么要有沙盘：
  如果推演直接落库，"看看另一条路会怎样"就变成了不可逆的污染——
  你得先删掉刚刚写进去的事件，世界状态也已经动过了。
  有了这一层，同一个局面下可以并行试多条路，只有挑定的那条才写进正史。

树的形状（前端画成自上而下的思维导图）：
    root
     └─ event ─ event ─ event
                         └─ decision ─┬─ option A ─ event ─ …
                                      ├─ option B   （未展开：不花 token）
                                      └─ option C   （未展开）

约定：
  1. 一次 expand 从某个节点往下长一段（≤ MAX_EVENTS_PER_EXPAND 个事件），
     遇到关键节点（decision_needed）就**提前停下**，交给用户拍板。
  2. 关键节点下自动挂出 3-5 个选项子节点，但**不预先推演**——
     用户点哪个才推哪个。这是省 token 的关键。
  3. "当前在哪条线上" 用 novel_attributes['story_leaf'] 记录，
     路径 = 从 leaf 顺着 parent_id 往上走。不额外存冗余状态。
  4. commit() 才把选定路径写进 events；写完后其它分支一并清掉，
     因为它们建立在旧的世界状态上，留着会误导。
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
from engine import applier
from engine import facts as F
from engine import ticktime
from engine.world import WorldEngine, split_events
from llm.client import note


def _int_or(value, default=3):
    """宽容取整：模型给 '高' / '5 分' / None 都不能让推演整段失败。"""
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default

# 一次推演最多长几个事件（比 WorldEngine 的 6 少，因为要留出"停下来问"的余地）
MAX_EVENTS_PER_EXPAND = 4
# 关键节点下最多挂几个选项
MAX_OPTIONS = 5
# 送进提示词的路径事件上限（太多会稀释最近发生的事）
PATH_BRIEF_LIMIT = 10

LEAF_ATTR_KEY = "story_leaf"

STATUS_LABELS = {
    "idea": "未走",
    "open": "等你选",
    "explored": "看过",
    "dead": "已弃",
    "committed": "已落定",
}


class StoryTreeError(Exception):
    pass


class StaleEditError(StoryTreeError):
    """乐观锁冲突：卡在你看过之后被别人（或另一次 AI 改卡）动过了。

    单独分一个类型，是因为它跟"你填的内容不对"完全是两回事：
      · 内容不对 → 该把错误提示挂在那个输入框上，让人改了再提交；
      · 这里冲突 → 该把这张卡**重新拉一遍**，让人在最新内容上重新决定。
    上层（webui）据此回 409 而不是 400，前端才分得清。
    """

    def __init__(self, message, field=None, mine=None, theirs=None):
        super().__init__(message)
        self.field = field
        self.mine = mine      # 前端提交时看到的值
        self.theirs = theirs  # 现在库里的值


class StoryTree:
    """剧情树的生长、走位与落定。db 为 NovelDatabase，router 为 LLMRouter。"""

    def __init__(self, db, router, novel_id):
        self.db = db
        self.router = router
        self.novel_id = novel_id

    # ================================================ 位置：当前在哪条线上

    def leaf_id(self):
        attrs = self.db.get_attributes(self.novel_id)
        raw = attrs.get(LEAF_ATTR_KEY) or ""
        try:
            nid = int(raw)
        except (TypeError, ValueError):
            return 0
        node = self.db.get_story_node(nid)
        return node["id"] if node else 0

    def set_leaf(self, node_id):
        self.db.set_attribute(self.novel_id, LEAF_ATTR_KEY, str(int(node_id)))
        return int(node_id)

    def path_nodes(self, node_id=None):
        """从根到 node 的节点列表（含两端）。node 为空则用当前叶。"""
        node_id = node_id or self.leaf_id()
        if not node_id:
            return []
        out = []
        seen = set()
        cur = self.db.get_story_node(node_id)
        while cur and cur["id"] not in seen:
            seen.add(cur["id"])
            out.append(cur)
            cur = self.db.get_story_node(cur["parent_id"]) if cur["parent_id"] else None
        out.reverse()
        return out

    def path_ids(self, node_id=None):
        return [n["id"] for n in self.path_nodes(node_id)]

    def committed_upto(self, path):
        """这条线上最后一个已经落进正文的事件 id（0 = 这条线还没落定过）。"""
        up = 0
        for n in path:
            if n.get("event_ref"):
                up = max(up, int(n["event_ref"]))
        return up

    def committed_tick(self, path):
        """这条线上最后一个已落定事件的世界 tick（0 = 还没落定过）。

        与 `committed_upto()` 并列存在：一个给"事件序号"，一个给"时间"。
        **两个维度都要**：只比事件 id 判断不出"跨了多久"，
        而且沙盘上的节点 tick 是累加的（`_node_tick()`），
        落定时要拿它跟当时的绝对 tick 对账。
        """
        t = 0
        for n in path:
            if n.get("event_ref"):
                t = max(t, int(n.get("tick") or 0))
        return t

    def beyond_world(self, path):
        """这条线之后还有多少已经写进正文的事件（>0 说明它落在进展之前）。"""
        up = self.committed_upto(path)
        try:
            n = self.db.scalar(
                "SELECT COUNT(*) FROM events WHERE novel_id=? "
                "AND deleted_at IS NULL AND id>?", (self.novel_id, up))
        except Exception:                  # noqa: BLE001
            return 0
        return int(n or 0)

    def behind_world_by_tick(self, path):
        """tick 维度的"落在进展之前"判定。

        事件 id 维度的 `beyond_world()` 有个盲区：**同一章里写了多少事件**
        不取决于时间——第 8 章可能只写了 1 个事件却跨了 30 tick。
        这里补上时间维度，两者取并。
        """
        t = self.committed_tick(path)
        if not t:
            return 0
        try:
            cur = ticktime.current_tick(self.db, self.novel_id)
        except Exception:                  # noqa: BLE001
            return 0
        return max(0, int(cur) - int(t)) if t else 0

    def behind_world(self, path):
        """综合判定：事件维度或 tick 维度任一成立就算"落在进展之前"。"""
        return (self.beyond_world(path) > 0
                or self.behind_world_by_tick(path) > 0)

    # ================================================ 分支（v6.7）

    def trunk_branch(self):
        """主干分支（root_node_id 为空的那条，`branch_id=0` 的语义实体）。

        为什么主干也要有行：`branches` 表要能回答"这个作品的沙盘一共有几条线、
        各自从哪儿岔开"。主干没有行的话，第一条分支的 `parent_branch`
        就指向了一个不存在的 id，前端画分支树会断在这里。
        """
        row = self.db.one(
            "SELECT * FROM branches WHERE novel_id=? AND parent_branch=0 "
            "AND root_node_id IS NULL ORDER BY id LIMIT 1", (self.novel_id,))
        if row:
            return row
        bid = self.db.add_branch(self.novel_id, root_node_id=None,
                                 parent_branch=0, base_event_id=0,
                                 base_tick=0, label="主干", status="active")
        return self.db.get_branch(bid)

    def _node_branch(self, node_id):
        node = self.db.get_story_node(node_id) if node_id else None
        return int((node or {}).get("branch_id") or 0)

    def _ensure_branch(self, node_id, parent_branch=0, label="",
                       base_event_id=0, base_tick=0):
        """为某个 option 节点建/取一条分支。**幂等**。

        `uq_branches_root` 保证同一个 option 节点只会有一条分支——
        用户反复点同一个选项不该长出好几条一模一样的线。
        """
        exist = self.db.find_branch_by_root(self.novel_id, node_id)
        if exist:
            return exist["id"]
        bid = self.db.add_branch(
            self.novel_id, root_node_id=int(node_id or 0) or None,
            parent_branch=int(parent_branch or 0),
            base_event_id=int(base_event_id or 0),
            base_tick=int(base_tick or 0), label=label or "", status="open")
        if node_id:
            self.db.update_story_node(int(node_id), branch_id=int(bid))
        return bid

    def branch_overlay(self, branch_id):
        """这条分支累计的世界状态改变 `{key: {"from":…, "to":…}}`。

        读缓存（`branches.overlay_state`）而不是每次遍历节点：
        沙盘可能有一百多个节点，每次推演都重算一遍是纯浪费。
        """
        if not branch_id:
            return {}
        b = self.db.get_branch(branch_id) or {}
        raw = b.get("overlay_state")
        if isinstance(raw, str):
            try:
                raw = json.loads(raw or "{}")
            except (ValueError, TypeError):
                raw = {}
        return raw or {}

    def _bump_branch_overlay(self, branch_id, delta, proj):
        """把这次的净改变累加进分支缓存。"""
        if not branch_id:
            return
        acc = self.branch_overlay(branch_id)
        acc = F.apply_delta(acc, delta)
        b = self.db.get_branch(branch_id) or {}
        facts_acc = b.get("overlay_facts") or []
        if isinstance(facts_acc, str):
            try:
                facts_acc = json.loads(facts_acc or "[]")
            except (ValueError, TypeError):
                facts_acc = []
        know_acc = b.get("overlay_knowledge") or []
        if isinstance(know_acc, str):
            try:
                know_acc = json.loads(know_acc or "[]")
            except (ValueError, TypeError):
                know_acc = []
        facts_acc = list(facts_acc) + list((proj or {}).get("facts") or [])
        know_acc = list(know_acc) + list((proj or {}).get("knowledge") or [])
        self.db.update_branch(
            branch_id, overlay_state=acc, overlay_facts=facts_acc[-200:],
            overlay_knowledge=know_acc[-200:],
            node_count=int(b.get("node_count") or 0) + 1,
            status="active")

    def _branch_base_state(self, branch_id):
        """分叉点的世界状态：主干投影 + 本分支已有 overlay。

        这就是方案 §5 的 `Base + ΔA1 + ΔA2 + …`。
        """
        base = {}
        try:
            for s in self.db.get_world_state(self.novel_id):
                base[s["key"]] = s["value"]
        except Exception:                       # noqa: BLE001
            base = {}
        return F.apply_delta(base, self.branch_overlay(branch_id))

    def _branch_base_tick(self, branch_id):
        if not branch_id:
            return ticktime.current_tick(self.db, self.novel_id)
        b = self.db.get_branch(branch_id) or {}
        return int(b.get("base_tick") or 0) or ticktime.current_tick(
            self.db, self.novel_id)

    def _node_tick(self, node_id):
        """节点上的累计 tick（沙盘里时间只往前，不回退）。

        取不到就用分叉点的 tick——**不要回退到世界最新 tick**：
        那会让"从中间重推"的分支时间突然跳到未来
        （v6.5 铁律：重推只改沙盘，世界状态/时钟不回退）。
        """
        if not node_id:
            return ticktime.current_tick(self.db, self.novel_id)
        node = self.db.get_story_node(node_id)
        if node and node.get("tick"):
            return int(node["tick"])
        return self._branch_base_tick(self._node_branch(node_id))

    def branch_status(self, branch_id):
        """「查看分支状态」：overlay + 节点 + 校验报告。"""
        b = self.db.get_branch(branch_id)
        if not b:
            raise StoryTreeError("分支不存在：%s" % branch_id)
        nodes = self.db.list_story_nodes(self.novel_id, branch_id=int(branch_id))
        overlay = self.branch_overlay(branch_id)
        from engine import guard
        events = []
        for n in nodes:
            if n["kind"] != "event":
                continue
            ev = (n.get("payload") or {}).get("event") or {}
            if isinstance(ev, dict) and ev.get("title"):
                events.append(ev)
        check = guard.branch_check(self.db, self.novel_id, nodes, overlay)
        return {
            "branch": b,
            "nodes": [_node_view(n) for n in nodes],
            "overlay": overlay,
            "overlay_count": len(overlay),
            "check": check,
            "base_tick": int(b.get("base_tick") or 0),
            "base_event_id": int(b.get("base_event_id") or 0),
        }

    # ================================================ 根

    def ensure_root(self):
        root = self.db.story_root(self.novel_id)
        if root:
            if not self.leaf_id():
                self.set_leaf(root["id"])
            self.trunk_branch()                 # 主干行必须存在（见 trunk_branch）
            return root
        n = self.db.get_novel(self.novel_id) or {}
        rid = self.db.add_story_node(
            self.novel_id, kind="root",
            title=n.get("title") or "故事起点",
            description=n.get("premise") or "（还没写前提）",
            event_type="world", importance=5,
            payload={"premise": n.get("premise") or "",
                     "initial_tension": n.get("initial_tension") or "",
                     "genre": n.get("genre") or "",
                     "tone": n.get("tone") or ""},
            status="explored",
            # 根节点绑定当前 tick：它是"分叉点 = 世界起点"的那条线
            tick=ticktime.current_tick(self.db, self.novel_id))
        self.set_leaf(rid)
        self.trunk_branch()
        return self.db.get_story_node(rid)

    # ================================================ 视图（给前端做布局）

    def tree(self):
        """整棵树 + 当前路径 + 可执行动作。前端拿到就能画，不需要二次查询。"""
        nodes = self.db.list_story_nodes(self.novel_id)
        root = next((n for n in nodes if n["kind"] == "root"), None)
        if not nodes:
            return {"root": None, "nodes": [], "leaf": 0, "path": [],
                    "pending_decision": None, "can_commit": False,
                    "committed_events": 0, "event_words": 0, "stats": {}}

        leaf = self.leaf_id()
        if not leaf or not any(n["id"] == leaf for n in nodes):
            # 叶丢了（被删了之类）：退到最深的节点，别让前端空转
            leaf = max(nodes, key=lambda n: (n["depth"], n["id"]))["id"]
            self.set_leaf(leaf)

        path = set(self.path_ids(leaf))
        by_id = {n["id"]: n for n in nodes}
        for n in nodes:
            n["on_path"] = n["id"] in path
            n["status_label"] = STATUS_LABELS.get(n["status"], n["status"])
            # 事件卡的结构化三行。主树视图返回的是**原始行**（不是 _node_view），
            # 所以这里得单独挂一次 —— 漏了它页面上就只剩 description 可看，
            # 模型只好把三行揉进那段话里（就是"卡片看着难受"的由来）。
            n["card"] = _card(n.get("payload"))
            if n["parent_id"] and n["parent_id"] in by_id:
                n["parent_kind"] = by_id[n["parent_id"]]["kind"]
            # delta 徽标：节点卡片右下角那颗"这条分支改变了什么"的小标
            n["has_delta"] = bool(n.get("state_delta"))
            n["delta_keys"] = list((n.get("state_delta") or {}).keys())[:4]
            n["delta_applied"] = bool(n.get("delta_applied"))

        # 路径上还没拍板的岔路口 / 还没落定的事件
        tail = self.path_nodes(leaf)
        open_dec = []
        for n in tail:
            if n["kind"] != "decision":
                continue
            kids = [c for c in nodes if c["parent_id"] == n["id"]]
            if not any(c["id"] in path for c in kids):
                open_dec.append(n)
        pending = open_dec[0] if open_dec else None
        undone = [n for n in tail
                  if n["kind"] == "event" and not n.get("event_ref")]
        tail_kind = tail[-1]["kind"] if tail else ""

        ev_words = sum(len(n.get("description") or "") for n in nodes
                       if n["kind"] == "event")
        # 当前线所属分支 + 本条线的 net overlay（前端侧栏展示"这条线改变了什么"）
        branch_id = self._node_branch(leaf)
        branch = self.db.get_branch(branch_id) if branch_id else None
        return {
            "root": root["id"] if root else None,
            "nodes": nodes,
            "leaf": leaf,
            "path": [n["id"] for n in tail],
            "pending_decision": pending,
            "tail_kind": tail_kind,
            # 只要这条线上还有没写进世界的事件，随时可以收手去写正文——
            # 停在岔路口也放行，那个岔路口会落成一条"待决点"留着
            "can_commit": bool(undone),
            "pending_events": len(undone),
            # >0 = 这条线落在"已经写进正文的进展"之前。落定它会让世界时间往回接，
            # 与已写好的章节打架——前端据此在落定前给出明确警告。
            "behind_world": self.beyond_world(tail),
            "behind_world_by_tick": self.behind_world_by_tick(tail),
            "open_decisions": [_node_view(n) for n in open_dec],
            "committed_events": sum(1 for n in nodes if n.get("event_ref")),
            # v6.7：分支信息（前端侧栏 / delta 徽标 / "查看分支状态"入口）
            "branch_id": branch_id,
            "branch": ({
                "id": branch.get("id"), "label": branch.get("label") or "",
                "base_tick": branch.get("base_tick") or 0,
                "base_event_id": branch.get("base_event_id") or 0,
                "status": branch.get("status"),
                "parent_branch": branch.get("parent_branch") or 0,
            } if branch else None),
            "overlay": self.branch_overlay(branch_id) if branch_id else {},
            "branches": len(self.db.list_branches(self.novel_id)),
            "current_tick": ticktime.current_tick(self.db, self.novel_id),
            "tick_unit_note": ticktime.unit_note(self.db, self.novel_id),
            "event_words": ev_words,
            "stats": {
                "nodes": len(nodes),
                "events": sum(1 for n in nodes if n["kind"] == "event"),
                "decisions": sum(1 for n in nodes if n["kind"] == "decision"),
                "depth": max((n["depth"] for n in nodes), default=0),
                "branches": len(self.db.list_branches(self.novel_id)),
            },
        }

    # ================================================ 上下文装配

    def _context_for_path(self, path, upto_event_id=None, branch_id=0):
        """世界简报 + 这条沙盘线上已经确定的事（还没落库的那些）。

        upto_event_id 非 None = 从这条线的中间重新岔开：简报里的世界时间/状态/角色
        现状会被标成"仅供参考"。【刚发生的事】也只切到这个岔口为止。

        v6.7：`branch_id` 非 0 时把分叉点时间一并传给简报，
        让【正史事实】给的是**分叉点当时为真的事实**——
        否则分支推演会读到分支之后才成立的 facts，
        现象就还是那句"我从前面的卡片重推，出来的内容还是从最下面来的"。
        """
        eng = WorldEngine(self.db, self.router, self.novel_id)
        parts = [eng.brief(upto_event_id=upto_event_id, branch_id=branch_id)]

        events = [n for n in path if n["kind"] == "event" and not n.get("event_ref")]
        if events:
            parts.append(
                "\n## 这条线已经推演出来、已经确定发生的事（还没写成正文）\n" +
                "\n".join("  - %s：%s" % (n["title"], (n["description"] or "")[:110])
                          for n in events[-PATH_BRIEF_LIMIT:]))

        picks = [n for n in path if n["kind"] == "option"]
        if picks:
            parts.append(
                "\n## 这条线上已经拍板的选择（不可更改，后续必须承接它的后果）\n" +
                "\n".join("  - %s：%s" % (n["title"], n["description"] or "")
                          for n in picks[-6:]))

        done = [n for n in path if n["kind"] == "event" and n.get("event_ref")]
        if done:
            parts.append("\n## 这条线里已经写成正文的部分（共 %d 个事件，最近一个：%s）"
                         % (len(done), done[-1]["title"]))
        return "\n".join(parts)

    def _rewind_note(self, node, branching):
        """从"作品最新进展之前"接续推演时补的一段说明。

        不写这段，模型会拿着简报里"最新的世界"顺着最下面那条线往下写——现象就是
        "我从前面那张卡片重推，出来的内容还是从最下面来的"。
        """
        at = (node.get("world_time") or "").strip()
        head = (
            "**注意：这次是从这条线的中间岔开，不是接着最新进展往下写。**\n"
            "起点是「%s」%s——在这里重新往下推，长出一条与已有分支**并列**的新线。"
            % (node["title"], ("（%s）" % at) if at else "")
            if branching else
            "**注意：这条线落在整部作品最新进展之前，不要接着最新进展往下写。**\n"
            "这条线目前走到「%s」%s，从这里接着往下推。"
            % (node["title"], ("（%s）" % at) if at else "")
        )
        return head + (
            "\n- 这条线上真正发生过的事，只有上面「这条线已经推演出来、已经确定发生的事」"
            "里列的那些；\n"
            "- 上面简报里的世界状态、世界时间、角色现状是**整部作品目前的最新面貌**，"
            "其中有一部分发生在这个起点**之后**——它们在这条线上**还没有发生**，"
            "不要承接、不要引用、不要假设它们已经存在；\n"
            "- 时间接着%s往后走，不要跳到更晚的时间点。"
            % (("「%s」" % at) if at else "起点那一刻"))

    def _actor_block(self, actor_name):
        if not actor_name:
            return "世界本身（不涉及具体角色的局面变化）"
        char = self.db.get_character_by_name(self.novel_id, actor_name)
        if not char:
            return "%s（尚未建档的角色）" % actor_name
        block = "%s（%s；%s）" % (char["name"], char.get("role_tag") or "无身份",
                                 char.get("personality") or "性格未定义")
        goals = self.db.list_goals(character_id=char["id"], status="active")
        if goals:
            block += "\n他的目标：" + "；".join(g["content"] for g in goals[:3])
        if char.get("decision_tendency"):
            block += "\n他的行事倾向：%s" % char["decision_tendency"]
        return block

    # ================================================ 生长

    def expand(self, node_id=None, focus="", constraints="", max_events=None):
        """从某个节点往下推演一段。返回 {'created': [...], 'decision': {...}}"""
        self.ensure_root()
        node_id = node_id or self.leaf_id()
        node = self.db.get_story_node(node_id)
        if not node:
            raise StoryTreeError("节点不存在：%s" % node_id)
        if node["kind"] == "decision":
            raise StoryTreeError(
                "这是个岔路口，先挑一条分支再往下推。\n岔路：%s" % node["title"])
        if node["kind"] == "option" and self.db.story_children(node_id):
            raise StoryTreeError(
                "这个分支已经推演过了。要从这儿另起一条线，先点「从此处重推」。")

        path = self.path_nodes(node_id)
        branching = self.leaf_id() not in (0, node_id)
        # 这条线是不是落在"作品最新进展"之前？——是的话，简报必须按线切。
        # 只按"起点不是末梢"判断是不够的：用「走这条线」切回前面时，末梢就是它自己，
        # 但后面已经落定的章节仍然摆在那儿，模型照样接着最下面写。
        # v6.7：判定合并了 tick 维度（见 behind_world）。
        behind = self.behind_world(path)
        rewind = branching or behind
        branch_id = self._node_branch(node_id)
        brief = self._context_for_path(
            path, upto_event_id=(self.committed_upto(path) if rewind else None),
            branch_id=branch_id)

        from engine import guard
        cons = guard.build_state_constraints(self.db, self.novel_id)
        if constraints:
            cons = cons + "\n\n## 本次额外约束\n" + constraints

        limit = int(max_events or MAX_EVENTS_PER_EXPAND)
        head = focus or "顺着当前局势往下推。"
        if rewind:
            head = self._rewind_note(node, branching) + "\n\n" + head
        # ▲ v6.9：不再"一嗅到岔路就停下"。
        # 旧文案"如果出现……就立刻停下写进 decision_needed"把模型逼成了
        # 「写 1 个事件 → 交 1 个岔路」的节拍器——实测每次 expand 只产出 1 条
        # 事件，岔路永远挂在链尾，树长成了事件/岔路交替的梯子。
        # 现在改成"先把这段推完，再看这段里有没有真的撞出岔路"：
        # 事件数优先，岔路是有则加分、无则正常。
        since = self._rounds_since_decision(node_id)
        prompt_focus = (
            "%s\n\n## 重要\n"
            "这次推演 %d 个事件以内的一段（尽量写满，别只写一条就收）。\n"
            "推完之后**回头看这段里有没有真的撞出必须由人拍板的岔路**：\n"
            "- 有，就写进 decision_needed，一次最多 1 个，并把逼出它的事件的标题"
            "填进 forks_from；\n"
            "- 没有，decision_needed 就给空数组 —— 这是正常的，不要为了交差硬造。\n"
            "%s\n"
            "另外：至少要有 1 条别人在别处做事的线（event_type=\"world\"），"
            "至少 1 条 state_changes.world，"
            "这一段里要有人被卷进来（new_characters）或有人退场（character_exits）。"
            % (head, limit, self._decision_pressure(since)))

        try:
            parsed, raw, mode, salvaged = self.router.run_json(
                "world_sim", schema=P.WORLD_SIM_SCHEMA, system=P.WORLD_SIM_SYSTEM,
                user=P.world_sim_user(brief, cons, prompt_focus), max_tokens=8192,
                with_meta=True)
        except Exception as e:            # noqa: BLE001
            raise StoryTreeError("推演失败：%s" % e)

        # ── 先分级，再落库 ────────────────────────────────────────────
        # 网关把 JSON 拦腰截断时，extract_json 的"截断补全"会抢救出一个
        # **合法但其实不全**的对象（真实事故：events[0] 只剩一个 title，
        # 别的字段全没了，下游却以为一切正常）。
        # 所以这里按"一条事件的必填字段是否齐"分成两堆：
        #   usable   字段齐的 → 照常落库
        #   dropped  缺字段的 → 丢掉，只在日志里说一声
        # 一条都不齐 → 判定这次输出是垃圾，自动重发一次；再不行才报错。
        got = split_events(parsed)
        if not got["usable"]:
            note("⚠ 这次输出的事件都不完整，自动重发一次…")
            try:
                parsed, raw, mode, salvaged = self.router.run_json(
                    "world_sim", schema=P.WORLD_SIM_SCHEMA,
                    system=P.WORLD_SIM_SYSTEM,
                    user=P.world_sim_user(brief, cons, prompt_focus),
                    max_tokens=8192, with_meta=True)
            except Exception as e:        # noqa: BLE001
                raise StoryTreeError("推演失败（重发时出错）：%s" % e)
            got = split_events(parsed)

        if not got["usable"]:
            raise StoryTreeError(
                "这次推演没产出有效事件流（解析模式：%s）。\n"
                "多半是推理模型的思维链吃满了输出预算，或该网关在降级模式下乱写——"
                "重试一次通常就好。\n原始输出片段：%s" % (mode, (raw or "")[:200]))

        if got["dropped"]:
            note("⚠ 这次有 %d 条事件没写完（缺 %s），已丢弃、只保留完整的 %d 条。"
                 % (len(got["dropped"]),
                    "、".join(sorted({f for _, miss in got["dropped"]
                                      for f in miss})),
                    len(got["usable"])))
        elif salvaged:
            # 截断抢救：写到最后一条被掐了，半截的那条已被解析器切掉，
            # 所以 dropped 是空的 —— 数据干净，但确实少了一条，得说出来。
            note("⚠ 模型的输出在中途被截断，最后一个没写完的事件已丢弃；"
                 "这次只落了 %d 条（解析模式 %s）。" % (len(got["usable"]), mode))

        events = got["usable"][:limit]
        world_time = parsed.get("world_time") or ""
        time_elapsed = parsed.get("time_elapsed") or ""
        # tick 增量：模型只给整数（"过了几个 tick"），Python 负责累加。
        # 让模型把虚构历法解析成绝对时间是**不可靠**的（同一句两次调用
        # 可能给出 9127 和 9131），但"过了多久"是可解析的。
        tick_delta = ticktime.delta_from(parsed, default=1)

        parent = node_id
        created = []
        for ev in events:
            # ① 先算这一条事件对世界做了什么——**纯函数，一个字节都不写库**。
            #    这就是沙盘能用的根本原因：算"走这条分支会怎样"不必真的写进去。
            node_tick = self._node_tick(parent) + tick_delta
            proj = applier.project_event(
                self.db, self.novel_id, ev, tick=node_tick, chapter_ref=0)
            # ② 与"Base + 本分支已有 overlay"对比，取净改变
            before = self._branch_base_state(branch_id)
            after = F.apply_delta(before, {
                st["key"]: st.get("value")
                for st in (proj.get("state") or []) if st.get("key")})
            delta = F.diff_projection(before, after)

            nid = self.db.add_story_node(
                self.novel_id, kind="event", parent_id=parent,
                title=ev.get("title") or "（未命名事件）",
                description=ev.get("description") or "",
                event_type=(ev.get("event_type") or "plot"),
                importance=_int_or(ev.get("importance"), 3),
                world_time=world_time,
                involved_characters=ev.get("involved_characters") or [],
                involved_entities=ev.get("involved_entities") or [],
                # payload 存完整结构化事件 + 本次推演的时间信息，commit 时原样回放
                payload={"event": ev, "world_time": world_time,
                         "time_elapsed": time_elapsed,
                         "tick_delta": tick_delta},
                status="idea",
                branch_id=branch_id, tick=node_tick,
                state_delta=delta,
                fact_delta=proj.get("facts") or [],
                knowledge_delta=proj.get("knowledge") or [],
                note=("来自推演（解析模式 %s）" % mode))
            parent = nid
            created.append(self.db.get_story_node(nid))
            # ③ 更新分支的累计 overlay 缓存（+ 节点数）
            self._bump_branch_overlay(branch_id, delta, proj)

        # ▲ v6.9：人事变动（新人登场 / 有人退场 / 新羁绊）。
        # 必须在事件落库之后做——新角色的建档要先于他被引用。
        churn = self._apply_churn(parsed)

        decisions = parsed.get("decision_needed") or []
        dec_node = None
        if decisions:
            dec_node = self._make_decision(parent, decisions, brief)
            self.set_leaf(dec_node["id"])
        else:
            self.set_leaf(parent)

        # 标记：走到的这条线上的节点算"已走"，其余仍为"未走"
        self._mark_path()

        return {
            "created": [_node_view(n) for n in created],
            "decision": _node_view(dec_node) if dec_node else None,
            "world_time": world_time,
            "time_elapsed": time_elapsed,
            "leftover_decisions": max(0, len(decisions) - 1),
            "parse_mode": mode,
            "leaf": self.leaf_id(),
            "new_characters": churn.get("new") or [],
            "character_exits": churn.get("exited") or [],
            "relation_count": churn.get("relations") or 0,
        }

    def _rounds_since_decision(self, node_id):
        """从这条线往上数：连着几个事件没有岔路了。

        用来给模型一点"压力"——不是逼它造岔路，而是在它已经好几段没分岔时
        提醒它"该看看有没有撞出东西了"。世界走顺了就该让矛盾自己浮上来。
        """
        n = 0
        cur = int(node_id or 0) or None
        seen = 0
        while cur and seen < 30:
            seen += 1
            row = self.db.get_story_node(cur)
            if not row:
                break
            if row["kind"] == "decision":
                break
            if row["kind"] == "event":
                n += 1
            # parent_id 是**节点的父亲 id**，必须取出来再往上走；
            # 早期版本误把整行 row 赋给 cur，下一轮 get_story_node(row)
            # 就会撞上 "Error binding parameter 1: type 'dict' is not supported"。
            cur = int(row.get("parent_id") or 0) or None
        return n

    def _decision_pressure(self, since):
        """按"已经几段没分岔"给出不同强度的提示语。"""
        if since >= 6:
            return ("（这条线已经连着 %d 个事件没有岔路了。回头看这段推演里"
                    "有没有人的目标已经正面撞上——如果撞上了，这次就该提一个；"
                    "确实没撞上也没关系，世界可以继续按惯性走。）" % since)
        if since >= 3:
            return ("（这条线已经 %d 个事件没分岔了，留意有没有该浮出来的矛盾。）"
                    % since)
        return ""

    def _apply_churn(self, parsed):
        """把 new_characters / character_exits 落库，返回本次的人事变动摘要。

        ▲ v6.9：这是"这是一个世界，不是情景剧"的落点。
        实测小说 77 推了 12 个事件、4 个角色一个没变、relation_changes 0/12——
        因为**没有任何通道**能让模型交出"谁被卷进来了""谁退场了"。
        state_changes.characters 只写"已经建档的人的状态"，模型想不到要往里塞新人；
        relation_changes 只记"关系变化"，也不负责建档。
        于是世界退化成固定四人组的情景剧。

        现在补两个显式通道，让"人的进出"有地方可写：
          new_characters   → 建角色档案（用模型给的目标，避免工具人）
          character_exits  → 更新 status（dead/missing/retired）
        """
        out = {"new": [], "exited": []}
        if not isinstance(parsed, dict):
            return out

        for ch in (parsed.get("new_characters") or []):
            if not isinstance(ch, dict):
                continue
            name = str(ch.get("name") or "").strip()
            if not name:
                continue
            # 已经在档的不重复建——模型有时会把老人写进 new_characters
            if self.db.get_character_by_name(self.novel_id, name):
                continue
            try:
                cid = self.db.add_character(
                    self.novel_id, name=name,
                    role_tag=str(ch.get("role_tag") or "").strip(),
                    personality=str(ch.get("personality") or "").strip(),
                    status="alive", control_mode="ai")
            except Exception as e:         # noqa: BLE001
                note("⚠ 新角色「%s」建档失败：%s" % (name, e))
                continue
            goal = str(ch.get("goal") or "").strip()
            if goal:
                try:
                    self.db.add_goal(self.novel_id, cid, goal, priority=3,
                                     origin="event_driven")
                except Exception:          # noqa: BLE001
                    pass
            out["new"].append({"id": cid, "name": name,
                               "role_tag": ch.get("role_tag") or "",
                               "goal": goal})
            note("＋ 新人登场：%s（%s）" % (name, ch.get("role_tag") or "身份未定"))

        for ch in (parsed.get("character_exits") or []):
            if not isinstance(ch, dict):
                continue
            name = str(ch.get("name") or "").strip()
            status = str(ch.get("status") or "").strip()
            if not name or status not in ("dead", "missing", "retired"):
                continue
            row = self.db.get_character_by_name(self.novel_id, name)
            if not row:
                continue
            if row.get("status") == status:
                continue
            try:
                self.db.update_character(row["id"], status=status)
            except Exception as e:         # noqa: BLE001
                note("⚠ 角色「%s」退场登记失败：%s" % (name, e))
                continue
            out["exited"].append({"id": row["id"], "name": name,
                                  "status": status,
                                  "reason": ch.get("reason") or ""})
            note("－ 有人退场：%s（%s）" % (name, status))

        # 关系落库：新羁绊必须记下来，否则下次推演这俩人又变陌生人。
        # （旧代码只在 commit 时才写 relations，沙盘里存的 payload 是断的）
        rels = parsed.get("relation_changes") or []
        if rels:
            out["relations"] = len(rels)

        return out

    def _make_decision(self, parent_id, decisions, brief):
        """把推演里冒出来的第一个岔路节点化，并挂上选项（选项不预推演）。

        ▲ v6.9：多了两件必须留住的东西——
          reason      "为什么这里要人拍板"（旧代码直接 note=d.get("reason")，
                      而模型从来不给，于是 note 全是空的，前端一片空白）
          forks_from  "是哪条事件逼出这个岔路的"（让人看得出岔路从事件里长出来，
                      而不是凭空挂在链尾）
        """
        d = decisions[0]
        actor_name = d.get("actor_name") or ""
        trigger = d.get("trigger_type") or "moral"
        reason = str(d.get("reason") or "").strip()
        forks = str(d.get("forks_from") or "").strip()
        stakes = str(d.get("stakes") or "").strip()

        # note 用来看"这个岔路是怎么来的"，按信息量拼：
        note_bits = []
        if forks:
            note_bits.append("由「%s」逼出来" % forks)
        if reason:
            note_bits.append(reason)
        note_text = "：".join(note_bits)

        did = self.db.add_story_node(
            self.novel_id, kind="decision", parent_id=parent_id,
            title=d.get("title") or "岔路口",
            description=d.get("situation") or "",
            event_type="decision", importance=5,
            actor_name=actor_name, trigger_type=trigger,
            stakes=stakes,
            payload={"raw": d, "forks_from": forks, "reason": reason},
            status="open",
            note=note_text)

        options = self._gen_options(d, brief)
        for i, o in enumerate(options):
            self.db.add_story_node(
                self.novel_id, kind="option", parent_id=did,
                title=o.get("label") or ("选项 %d" % (i + 1)),
                description=o.get("description") or "",
                event_type="decision", importance=4,
                actor_name=actor_name, trigger_type=trigger,
                payload=o, status="idea",
                branch_hint=o.get("consequence_hint") or "")
        return self.db.get_story_node(did)

    def _gen_options(self, decision, brief):
        """复用 option_gen 档位生成岔路选项；失败时退到保底三选项。"""
        situation = decision.get("situation") or decision.get("title") or ""
        stakes = decision.get("stakes") or ""
        try:
            parsed, raw, mode = self.router.run_json(
                "option_gen", schema=P.OPTION_GEN_SCHEMA,
                system=P.OPTION_GEN_SYSTEM,
                user=P.option_gen_user(situation,
                                       self._actor_block(decision.get("actor_name") or ""),
                                       stakes),
                max_tokens=2048)
        except Exception:                  # noqa: BLE001
            parsed = None
        opts = (parsed or {}).get("options") or []
        opts = [_normalize_option(o) for o in opts][:MAX_OPTIONS]
        opts = [o for o in opts if o["label"]]
        if len(opts) < 3:
            opts = _fallback_options()
        return opts

    # ================================================ 走位

    def choose(self, decision_id, option_index):
        """在岔路口挑一条。返回被选中的 option 节点。"""
        node = self.db.get_story_node(decision_id)
        if not node or node["kind"] != "decision":
            raise StoryTreeError("这不是一个岔路口：%s" % decision_id)
        kids = [c for c in self.db.story_children(decision_id)
                if c["kind"] == "option"]
        idx = int(option_index)
        if not (0 <= idx < len(kids)):
            raise StoryTreeError("选项下标越界：%s（共 %d 个）" % (idx, len(kids)))
        picked = kids[idx]
        # 同一个岔路口只保留一条活线：其它选项标记为"看过"
        for i, k in enumerate(kids):
            self.db.update_story_node(
                k["id"], status=("explored" if i == idx else "idea"))
        # ▲ v6.7：建一条分支，入口就是这个 option 节点。
        #   分支要记下"从哪个 tick / 哪个事件之后岔开"——落定时按 tick 累加，
        #   推演时按 as_of_tick 取"那一刻为真的事实"。
        parent_branch = self._node_branch(decision_id)
        base_tick = self._node_tick(decision_id)
        base_event = self.committed_upto(self.path_nodes(picked["id"]))
        branch_id = self._ensure_branch(
            picked["id"], parent_branch=parent_branch,
            label=picked["title"], base_event_id=base_event,
            base_tick=base_tick)
        self.set_leaf(picked["id"])
        self._mark_path()
        self._settle_leftover_decision(node, idx, picked)
        out = self.db.get_story_node(picked["id"])
        out["branch_id"] = branch_id
        return out

    def _settle_leftover_decision(self, node, idx, picked):
        """上次落定时没定的岔路口，这次在树上补选了，把那条待决点补上。"""
        ref = node.get("decision_ref")
        if not ref:
            return
        try:
            d = self.db.get_decision(ref)
        except Exception:                  # noqa: BLE001
            return
        if not d or d.get("resolved"):
            return
        self.db.resolve_decision(
            ref, chosen_option=int(idx),
            chosen_content="（你在剧情树上补定的）%s：%s"
                           % (picked["title"], picked["description"]),
            chosen_by="user",
            impact={"via": "story_tree", "late_pick": True})

    def choose_and_expand(self, decision_id, option_index, focus="",
                          constraints=""):
        """挑一条 + 立刻往下推一段。前端一次点击走完这两步。"""
        picked = self.choose(decision_id, option_index)
        out = self.expand(node_id=picked["id"], focus=focus,
                          constraints=constraints)
        out["chosen"] = _node_view(picked)
        return out

    def goto(self, node_id):
        """把工作线切到某个节点（"从这条线继续"）。"""
        node = self.db.get_story_node(node_id)
        if not node:
            raise StoryTreeError("节点不存在：%s" % node_id)
        self.set_leaf(node_id)
        self._mark_path()
        return self.leaf_id()

    def _mark_path(self):
        """路径上的事件/选项标 explored，路径外的保持 idea（灰着，等回头）。"""
        leaf = self.leaf_id()
        if not leaf:
            return
        on = set(self.path_ids(leaf))
        for n in self.db.list_story_nodes(self.novel_id):
            if n["kind"] not in ("event", "option"):
                continue
            if n.get("event_ref"):
                continue
            want = "explored" if n["id"] in on else "idea"
            if n["status"] != want:
                self.db.update_story_node(n["id"], status=want)

    def prune(self, node_id):
        """砍掉某个节点及其子树（重新推演这条路）。"""
        node = self.db.get_story_node(node_id)
        if not node:
            raise StoryTreeError("节点不存在：%s" % node_id)
        if node["kind"] == "root":
            raise StoryTreeError("根节点不能砍——要重来请用「重开一棵树」。")
        if node.get("event_ref"):
            raise StoryTreeError("这条线已经落成正文了，不能砍。")
        n = self.db.delete_story_subtree(node_id)
        parent = node["parent_id"]
        if self.leaf_id() == node_id or not self.db.get_story_node(self.leaf_id() or 0):
            self.set_leaf(parent or self.ensure_root()["id"])
        self._mark_path()
        return n

    def reset(self):
        """清空整棵树，重新开始（不碰已经落定的正文事件）。"""
        n = self.db.clear_story_tree(self.novel_id)
        self.db.set_attribute(self.novel_id, LEAF_ATTR_KEY, "")
        root = self.ensure_root()
        return {"deleted": n, "root": root["id"]}

    # ================================================ 改卡片与下游衔接校验（v6.8）

    # 只有这几个字段可以直接手改。**必须与 prompts.E_CARD_FIELDS 一致**：
    # 后端接口按这个白名单过滤，prompts 按它告诉模型有哪些字段，
    # 前端按它渲染表单——三处少改一处，要么改不动要么看不见。
    EDITABLE_FIELDS = ("title", "description", "stakes", "event_type",
                       "actor_name", "importance")

    def changeable_card(self, node):
        """把节点摊成"可编辑视图"：卡片三行 + 可直接改的字段。

        卡片三行（action/intent/result）住在 payload.event 里，不在列上；
        手改它们必须**回写进 payload**，否则下次 _node_view 一读又变回去了。
        """
        payload = node.get("payload") or {}
        ev = payload.get("event") if isinstance(payload, dict) else None
        ev = ev if isinstance(ev, dict) else {}
        return {
            "id": node["id"],
            "kind": node["kind"],
            "title": node.get("title") or "",
            "description": node.get("description") or "",
            "stakes": node.get("stakes") or "",
            "event_type": node.get("event_type") or "",
            "actor_name": node.get("actor_name") or "",
            "importance": node.get("importance") or 3,
            "action": str(ev.get("action") or ""),
            "intent": str(ev.get("intent") or ""),
            "result": str(ev.get("result") or ""),
            "opens": [str(t.get("title") if isinstance(t, dict) else t)
                      for t in (ev.get("open_threads") or [])],
            # 落定过的不让再改内容：正文已经按它写了，改了会和章节对不上
            "locked": bool(node.get("event_ref")),
            "locked_reason": ("这段已经落成正文了，改了会和已写的章节对不上。"
                              "要调整请去章节页改正文。"
                              if node.get("event_ref") else ""),
        }

    def edit_node(self, node_id, fields, expected=None):
        """人工直接改卡片。`fields` 只认白名单内的键。

        `expected` 是前端发回来的**原值**（乐观锁）：只校验它认得的键，
        值不一致就报冲突——否则两个页面同时开着改，后保存的会静默覆盖前者。
        """
        node = self.db.get_story_node(node_id)
        if not node:
            raise StoryTreeError("节点不存在：%s" % node_id)
        if node.get("event_ref"):
            raise StoryTreeError(
                "这段已经落成正文了，不能直接改——改了会和已写的章节对不上。\n"
                "要调整请到章节页改正文，或者把这段线砍掉重推。")

        cur = self.changeable_card(node)
        # 乐观锁：只比对前端明确回传的那些键
        if expected:
            conflicts = []
            for k, was in expected.items():
                if k in ("action", "intent", "result", "opens"):
                    now = cur.get(k)
                    if isinstance(now, list):
                        now = " · ".join(now)
                    was_s = was if isinstance(was, str) else " · ".join(was or [])
                else:
                    now = cur.get(k)
                    was_s = was
                if str(now if now is not None else "") != str(was_s or ""):
                    conflicts.append(k)
            if conflicts:
                bad = conflicts[0]
                _n = cur.get(bad)
                if isinstance(_n, list):
                    _n = " · ".join(_n)
                _w = expected.get(bad)
                if isinstance(_w, list):
                    _w = " · ".join(_w)
                raise StaleEditError(
                    "这张卡在你编辑期间已经被改过了（%s）。"
                    "刷新一下看最新内容，再决定怎么改。"
                    % "、".join(conflicts),
                    field=bad, mine=_w, theirs=_n)

        col_fields, payload_fields = {}, {}
        for k in ("title", "description", "stakes", "event_type", "actor_name",
                  "importance"):
            if k in (fields or {}) and fields[k] is not None:
                v = fields[k]
                col_fields[k] = int(v) if k == "importance" else str(v).strip()
        for k in ("action", "intent", "result"):
            if k in (fields or {}) and fields[k] is not None:
                payload_fields[k] = str(fields[k]).strip()

        # payload（卡片三行 + last_change）统一在下面一次写，见 _payload_with_change
        if not col_fields and not payload_fields:
            raise StoryTreeError("没有要改的内容。")

        # 记一笔"改之前长什么样"。check_downstream 要拿它对照"这次改了什么"——
        # 没有这个，模型只能看到改后的样子，判断不出哪些下游是**因为这次改动**
        # 才不成立的。存精简版（只留关键行），别把整份 payload 复制一遍。
        #
        # 注意：必须把 last_change **并进 col_fields["payload"]** 再一起写。
        # 先单独 update 一次 payload、再 UPDATE ... payload=? 的话，后一次会把
        # 前一次盖掉（col_fields 里的 payload 是改前的快照，不含 last_change），
        # last_change 就静默丢了。
        col_fields["payload"] = self._payload_with_change(
            node, extra=(payload_fields or None))

        col_fields["status"] = "idea"      # 改过了就不再是"已走过"的状态
        self.db.update_story_node(node_id, **col_fields)
        self._mark_path()
        return self.changeable_card(self.db.get_story_node(node_id))

    def _payload_with_change(self, node, extra=None):
        """构造新的 payload：应用 extra 字段 + 记下改前摘要（last_change）。

        `node` 必须是**改写前**的快照（库里可能已经是新的了）。
        """
        old = self.changeable_card(node)
        lines = []
        for k, label in (("title", "标题"), ("action", "做了什么"),
                         ("intent", "为什么"), ("result", "结果"),
                         ("stakes", "代价"), ("description", "叙述"),
                         ("actor_name", "行动主体")):
            v = str(old.get(k) or "").strip()
            if v:
                lines.append("%s：%s" % (label, v[:160]))

        payload = dict(node.get("payload") or {})
        if extra:
            ev = payload.get("event")
            ev = dict(ev) if isinstance(ev, dict) else {}
            ev.update(extra)
            payload["event"] = ev
        payload["last_change"] = "\n".join(lines)
        return payload

    def ai_fix_node(self, node_id, complaint, direction=""):
        """照作者的文字说明，让模型改这一张卡。

        返回 {'card': 改后的可编辑视图, 'changed': [...], 'why':..., 'note':...,
              'before': 改前的可编辑视图}
        """
        node = self.db.get_story_node(node_id)
        if not node:
            raise StoryTreeError("节点不存在：%s" % node_id)
        if node.get("event_ref"):
            raise StoryTreeError(
                "这段已经落成正文了，不能靠 AI 改——改了会和已写的章节对不上。\n"
                "要调整请到章节页改正文，或者把这段线砍掉重推。")
        complaint = (complaint or "").strip()
        if not complaint:
            raise StoryTreeError("请先写清楚哪里不对（一句话就行）。")

        before = self.changeable_card(node)
        ctx = self._node_repair_context(node_id)

        try:
            parsed, raw, mode = self.router.run_json(
                "diagnose", schema=P.EVENT_FIX_SCHEMA,
                system=P.EVENT_FIX_SYSTEM,
                user=P.event_fix_user(
                    card=before, complaint=complaint,
                    direction=(direction or "").strip(), context=ctx),
                temperature=0.4, max_tokens=2048, cap=4096)
        except Exception as e:             # noqa: BLE001
            raise StoryTreeError("AI 修正失败：%s" % e)

        # v6.9：模型只回**改动项**（patch），不再回整张卡。
        # 让它把整张卡回一遍的话，没动过的字段也得照抄（description 动辄几百字），
        # 输出量翻几倍，实测会撞上输出上限被截断（finish=length）——
        # 已经改好的东西反而跟着一起丢。只回 patch 既省预算，也堵住了
        # "顺手把别的字段也改了"这条路。
        #
        # 注意「patch 不存在」和「patch 是空的」是两回事：
        #   patch={} 是模型明确说"没找到要改的"（合理结论，走 no_change）；
        #   连 patch 键都没有才是输出不符合约定（旧式 card 输出算这一类）。
        # 一开始没分开，结果模型老老实实回个空 patch 反而被当成错误抛出去。
        got_patch = (parsed or {}).get("patch")
        if isinstance(got_patch, dict):
            patch = got_patch                    # 空 dict 也收下，下面按 no_change 处理
        else:
            # 兼容旧式输出（整张卡）：万一模型还是回了 card，也认，别白跑一趟
            legacy = (parsed or {}).get("card")
            if isinstance(legacy, dict) and legacy:
                patch = legacy
            else:
                raise StoryTreeError(
                    "模型没有返回可用的改动（解析模式 %s）。重试一次通常就好。\n"
                    "原始输出片段：%s" % (mode, (raw or "")[:200]))

        declared = [c for c in ((parsed or {}).get("changed") or [])
                    if c in P.E_CARD_FIELDS]

        # 哪些字段真该写回？`changed` 是模型**自报**的，patch 是它**实际给出**的，
        # 两者都可能不准：
        #   · patch 里有、changed 没申报 → 它确实给了内容（漏报），收下，
        #     别把作者要的改动白丢；
        #   · patch 里没有、changed 却申报了 → 没有内容可写，跳过。
        # 所以取 patch 的键 ∩ 声明的键，再并上"给了内容但漏报"的键。
        # 只认 E_CARD_FIELDS 白名单，防止模型塞进无关字段。
        keys = [k for k in P.E_CARD_FIELDS if k in patch]
        changed = sorted(set(keys))

        fields = {}
        for k in changed:
            v = patch.get(k)
            if v is None:
                continue
            fields[k] = str(v).strip()
        if not fields:
            # 模型说什么都没改：不落库，如实告诉作者（可能是它认为原样就是对的）
            return {"card": before, "before": before, "changed": [],
                    "why": (parsed or {}).get("why") or "",
                    "note": (parsed or {}).get("note") or "",
                    "no_change": True, "mode": mode,
                    "declared": declared}

        after = self.edit_node(node_id, fields)
        return {"card": after, "before": before, "changed": sorted(fields),
                "why": (parsed or {}).get("why") or "",
                "note": (parsed or {}).get("note") or "",
                "no_change": False, "mode": mode}

    def _node_repair_context(self, node_id, max_up=3):
        """给修理用的上下文：这条线在这张卡之前发生了什么。

        **不给下游**——下游要看是通过 check_downstream 单独查的。
        把上下游一起塞进"改卡"的提示词，模型会为了迁就下游而不敢真改，
        最后改成个四不像。
        """
        try:
            path = self.path_nodes(node_id)
        except Exception:                  # noqa: BLE001
            path = []
        ups = [n for n in path if n["id"] != node_id][-max_up:]
        L = []
        if ups:
            L.append("这条线在这张卡之前，刚刚发生的是：")
            for n in ups:
                L.append("· %s" % (n.get("title") or ""))
                d = (n.get("description") or "").strip()
                if d:
                    L.append("  %s" % d[:160])
        node = self.db.get_story_node(node_id) or {}
        if node.get("world_time"):
            L.append("世界时间：%s" % node["world_time"])
        # 涉及角色带上一点档案，避免模型把人物写偏
        names = list(node.get("involved_characters") or [])
        if node.get("actor_name"):
            names.insert(0, node["actor_name"])
        if names:
            try:
                from engine import diagnose as DG
                L.append("\n涉及角色的档案（改的时候不要写偏）：")
                L.append(DG.build_characters_block(
                    self.db, self.novel_id, names[:6]))
            except Exception:             # noqa: BLE001
                pass
        return "\n".join(L)

    def downstream_of(self, node_id, limit=12):
        """从某节点往后、沿**当前这条线**的下游事件。

        只取路径上的（path_ids），不取整棵子树：作者改一张卡时关心的是
        "我这条线还接不接得上"，把旁边那些没走的分支也拉进来只会干扰判断。
        """
        try:
            path = self.path_nodes()
        except Exception:                  # noqa: BLE001
            path = []
        ids = [n["id"] for n in path]
        if node_id not in ids:
            # 改的这张卡不在当前工作线上（在别的分支）：就往下取它的后继
            out, cur, seen = [], node_id, set()
            while cur and len(out) < limit:
                kids = [x for x in self.db.story_children(cur)
                        if x["kind"] in ("event", "option")]
                if not kids:
                    break
                cur = sorted(kids, key=lambda x: (x["seq"], x["id"]))[0]["id"]
                if cur in seen:
                    break
                seen.add(cur)
                n = self.db.get_story_node(cur)
                if n:
                    out.append(n)
            return out
        after = ids[ids.index(node_id) + 1:]
        out = []
        for nid in after:
            n = self.db.get_story_node(nid)
            if n and n["kind"] in ("event", "option"):
                out.append(n)
            if len(out) >= limit:
                break
        return out

    def check_downstream(self, node_id):
        """校验下游事件是否还与这张卡（改后的）衔接、逻辑是否还成立。

        返回 {'issues': [...], 'ok_summary':..., 'checked': n, 'affected': n}
        """
        node = self.db.get_story_node(node_id)
        if not node:
            raise StoryTreeError("节点不存在：%s" % node_id)
        card = self.changeable_card(node)
        downs = self.downstream_of(node_id)
        if not downs:
            return {"issues": [], "checked": 0, "affected": 0,
                    "ok_summary": "这张卡后面还没有事件，不存在衔接问题。"}

        items = []
        for n in downs:
            items.append({
                "id": n["id"],
                "title": n.get("title") or "",
                "description": (n.get("description") or "")[:300],
                "card": _card(n.get("payload")),
            })
        # 改前的内容：从节点的 note 里取不到历史，就用 payload 里存的
        # last_change（edit_node / ai_fix_node 落库时记一笔）
        before = (node.get("payload") or {}).get("last_change") or ""

        world_ctx = ""
        try:
            from engine import diagnose as DG
            world_ctx = DG.build_world_block(self.db, self.novel_id)
        except Exception:                  # noqa: BLE001
            pass

        try:
            parsed, raw, mode = self.router.run_json(
                "diagnose", schema=P.CARD_CHAIN_SCHEMA,
                system=P.CARD_CHAIN_SYSTEM,
                user=P.card_chain_user(card=card, before=before,
                                       downstream=items,
                                       world_context=world_ctx),
                temperature=0.3, max_tokens=3072, cap=4096)
        except Exception as e:             # noqa: BLE001
            raise StoryTreeError("衔接校验失败：%s" % e)

        parsed = parsed or {}
        valid = {n["id"] for n in downs}
        issues = []
        for it in (parsed.get("issues") or []):
            try:
                nid = int(it.get("node_id") or 0)
            except (TypeError, ValueError):
                continue
            # 模型偶尔会编一个不存在的 id：丢掉，别让前端拿它去查节点
            if nid not in valid:
                continue
            lvl = it.get("level") if it.get("level") in ("broken", "weak") else "weak"
            issues.append({
                "node_id": nid,
                "title": str(it.get("title") or "").strip()[:60],
                "level": lvl,
                "aspect": it.get("aspect") or "causality",
                "reason": str(it.get("reason") or "").strip()[:160],
                "quote": str(it.get("quote") or "").strip()[:160],
                "fix_hint": str(it.get("fix_hint") or "").strip()[:220],
            })
        issues.sort(key=lambda x: (x["level"] != "broken",))
        return {
            "issues": issues,
            "checked": len(downs),
            "affected": len(issues),
            "ok_summary": str(parsed.get("ok_summary") or "").strip()[:200],
            "mode": mode,
        }

    # ================================================ 落定成正文

    def pre_validate(self, path, overlay):
        """落定前的分支校验。返回与 `guard.full_check` 同构的报告。

        三件事一起查：
          causal —— 因果校验（死人在行动、状态跳变…）
          rules  —— 世界规则（用户在世界上写下的硬约束）
          branch —— 沙盘特有（分支前提污染、overlay 移除不存在的东西）
        """
        from engine import guard

        events = []
        for n in path:
            if n["kind"] != "event" or n.get("event_ref"):
                continue
            ev = (n.get("payload") or {}).get("event") or {}
            if isinstance(ev, dict) and ev:
                events.append(ev)
        causal = guard.full_check(self.db, self.novel_id, events,
                                  branch_id=self._node_branch(
                                      path[-1]["id"] if path else 0))
        rules = guard.rule_check(self.db, self.novel_id, events)
        branch = guard.branch_check(self.db, self.novel_id, path, overlay)
        ok = bool(causal.get("ok") and rules.get("ok") and branch.get("ok"))
        return {
            "ok": ok,
            "causal": causal, "rules": rules, "branch": branch,
            "errors": (causal.get("errors") or []) + (rules.get("errors") or [])
            + (branch.get("errors") or []),
            "warnings": (causal.get("warnings") or [])
            + (rules.get("warnings") or []) + (branch.get("warnings") or []),
        }

    def precheck(self, node_id=None):
        """落定前的预校验（**只读**，不改一个字节）。

        「落定为正史」按钮**先调它**：有 error 时前端弹确认框（不能落）；
        只有 warning 时把 warning 列给用户，由用户决定要不要落。

        比 `pre_validate` 多三件事：
          · 自己解析 path / overlay（调用方只给 node_id）
          · 附 `behind_world`（这条线是否落在已写进正文的进展之前）
          · 附 `pending_events`（有几个事件会被落进正史）
        """
        node_id = node_id or self.leaf_id()
        path = self.path_nodes(node_id)
        if not path:
            return {"ok": False, "errors": [{
                "level": "error", "kind": "empty_path",
                "message": "这条线还是空的，没什么可落。先点「推演」长一段。",
                "event_title": "", "fix_hint": ""}],
                "warnings": [], "total": 1, "behind_world": 0,
                "pending_events": 0, "branch_id": 0}
        branch_id = self._node_branch(node_id)
        overlay = self.branch_overlay(branch_id)
        rep = self.pre_validate(path, overlay)
        undone = [n for n in path
                  if n["kind"] == "event" and not n.get("event_ref")]
        rep.update({
            "node_id": node_id,
            "branch_id": branch_id,
            "overlay_count": len(overlay),
            "overlay": overlay,
            "pending_events": len(undone),
            "committed_upto": self.committed_upto(path),
            "behind_world": self.beyond_world(path),
            "total": len(rep.get("errors") or []) + len(rep.get("warnings") or []),
        })
        return rep

    def commit(self, node_id=None):
        """把 root->node 这条线写进 events，成为"待成章事件"。

        ▲ v6.7：**这是全项目唯一允许写正史的地方。**

        改造前它直接调 `applier.apply_events()`（边算边写），于是"先看看这条线
        合不合理再落"根本做不到——校验只能跑在写之后。现在：

            ① pre_validate()  先校验，有硬伤**直接拒绝**（抛 StoryTreeError）
            ② project_events() 算（纯函数）
            ③ apply_projection() 落（唯一写入口）
            ④ write_state_projection() 更新状态缓存
            ⑤ tick 推进（不再用自由文本的 advance_clock）

        只写还没落库的部分，所以可以分多次落定（先落前几章，再接着往下推）。
        停在岔路口/分支上也允许落定——用户想先写这一章就走人时不该被拦住：
        那个没拍板的岔路口会落成一条"待决点"留在库里，随时可以回来定。
        """
        node_id = node_id or self.leaf_id()
        path = self.path_nodes(node_id)
        if not path:
            raise StoryTreeError("这条线还是空的，没什么可落。")

        all_nodes = self.db.list_story_nodes(self.novel_id)
        path_ids = {x["id"] for x in path}
        branch_id = self._node_branch(node_id)

        # 1) 路径上还没拍板的岔路口：不拦人，记下来改成待决点
        open_decisions = []
        for n in path:
            if n["kind"] != "decision":
                continue
            kids = [c for c in all_nodes if c["parent_id"] == n["id"]]
            if not any(c["id"] in path_ids for c in kids):
                open_decisions.append(n)
        open_ids = {n["id"] for n in open_decisions}

        # 2) 收集还没落库的事件
        pending = [n for n in path if n["kind"] == "event" and not n.get("event_ref")]
        pending_decisions = [n for n in path
                             if n["kind"] == "decision" and not n.get("decision_ref")]
        if not pending:
            raise StoryTreeError(
                "这条线还没有新的推演结果可落。先点「推演」长一段，"
                "再落定成正文。")

        # 3) ▲ 落定前先校验（方案 P0：先阻止错误再写正文）
        overlay = self.branch_overlay(branch_id)
        check = self.pre_validate(path, overlay)
        if not check["ok"]:
            detail = "\n".join("  · %s" % e.get("message", "")
                               for e in (check.get("errors") or [])[:5])
            raise StoryTreeError(
                "这条线有 %d 处硬伤，不能落为正史：\n%s"
                % (len(check.get("errors") or []), detail))

        events = []
        for n in pending:
            ev = dict((n.get("payload") or {}).get("event") or {})
            ev.setdefault("title", n["title"])
            ev.setdefault("description", n["description"])
            ev.setdefault("event_type", n["event_type"])
            ev.setdefault("importance", n["importance"])
            if n.get("involved_characters"):
                ev.setdefault("involved_characters", n["involved_characters"])
            if n.get("involved_entities"):
                ev.setdefault("involved_entities", n["involved_entities"])
            events.append(ev)

        last_time = ""
        for n in reversed(path):
            if n["kind"] == "event" and n.get("world_time"):
                last_time = n["world_time"]
                break
        time_elapsed = ((pending[-1].get("payload") or {}).get("time_elapsed")
                        or "")

        # 4) ▲ tick 累加：从分叉点往后走
        base_tick = self._branch_base_tick(node_id)
        delta_sum = 0
        for n in pending:
            delta_sum += int((n.get("payload") or {}).get("tick_delta") or 1)
        tick = int(base_tick) + max(1, delta_sum)

        # 5) ▲ 投影（纯函数）→ 落库（唯一写入口）
        proj = applier.project_events(
            self.db, self.novel_id,
            {"events": events, "world_time": last_time,
             "time_elapsed": time_elapsed},
            tick=tick, chapter_ref=0)
        applied = applier.apply_projection(
            self.db, self.novel_id, proj, tick=tick, chapter_ref=0,
            branch_id=branch_id, link_chapter=False)

        eids = applied.get("event_ids") or []
        for n, eid in zip(pending, eids):
            # delta_applied=1：这条节点上的 Δ 已经真的写进正史了
            self.db.update_story_node(n["id"], status="committed",
                                      event_ref=eid, delta_applied=1)

        # 6) ▲ State Projection：facts → world_state 缓存
        try:
            F.write_state_projection(self.db, self.novel_id)
        except Exception:                  # noqa: BLE001
            pass

        # 7) 岔路口落成决策点：选过的记成历史，没选的留成待决点
        for n in pending_decisions:
            did = self._commit_decision(n, path, all_nodes,
                                        resolved=n["id"] not in open_ids,
                                        branch_id=branch_id)
            self.db.update_story_node(n["id"], decision_ref=did)
        for n in path:
            if n["kind"] == "decision" and n.get("decision_ref"):
                self.db.update_story_node(n["id"], status="committed")

        # 8) ▲ tick 推进
        try:
            ticktime.advance(self.db, self.novel_id,
                             delta=max(1, delta_sum),
                             new_time=last_time or None,
                             chapter_ref=0, reason="沙盘落定为正史")
        except Exception:                  # noqa: BLE001
            pass

        # 9) ▲ 分支标记为已落定；其余分支作废（世界已经变了）
        if branch_id:
            try:
                self.db.update_branch(branch_id, status="committed")
            except Exception:              # noqa: BLE001
                pass
        keep = {n["id"] for n in path}
        # 例外：落在岔路口上时，它的分支是"接下来怎么走"的入口，得留着
        for n in open_decisions:
            keep.update(c["id"] for c in all_nodes if c["parent_id"] == n["id"])
        dropped = [n for n in self.db.list_story_nodes(self.novel_id)
                   if n["id"] not in keep]
        # 只砍"被丢弃那片森林的根"，子孙靠外键级联跟着走
        for n in [x for x in dropped if x["parent_id"] not in keep]:
            self.db.delete_story_subtree(n["id"])
        # 分支行不删（留作回溯），只标 discarded
        self.trunk_branch()                 # 主干永远活着
        try:
            self.db.discard_branches(self.novel_id, keep_id=branch_id)
        except Exception:                  # noqa: BLE001
            pass
        if branch_id:
            try:
                self.db.update_branch(branch_id, status="committed")
            except Exception:              # noqa: BLE001
                pass

        self.set_leaf(node_id)
        self._mark_path()

        return {
            "events": len(eids),
            "event_ids": eids,
            "decisions": len(pending_decisions),
            "open_decisions": len(open_decisions),
            "open_decision_titles": [n["title"] for n in open_decisions],
            "world_time": ticktime.display_time(self.db, self.novel_id),
            "tick": tick,
            "branch_id": branch_id,
            "facts": len(proj.get("facts") or []),
            "knowledge": len(proj.get("knowledge") or []),
            "dropped_branches": len(dropped),
            "check": {"errors": len(check.get("errors") or []),
                      "warnings": len(check.get("warnings") or [])},
            "applied": {"threads": len(applied.get("threads") or []),
                        "traits": len(applied.get("traits") or [])},
        }

    def _commit_decision(self, node, path, all_nodes, resolved=True,
                         branch_id=0):
        kids = [c for c in all_nodes
                if c["parent_id"] == node["id"] and c["kind"] == "option"]
        kids.sort(key=lambda c: (c["seq"], c["id"]))
        on_path = {n["id"] for n in path}
        picked = next((c for c in kids if c["id"] in on_path), None)
        idx = kids.index(picked) if picked in kids else -1
        content = "%s：%s" % (picked["title"], picked["description"]) if picked \
            else "（未选择）"
        actor = self.db.get_character_by_name(self.novel_id, node["actor_name"]) \
            if node["actor_name"] else None
        note = node.get("note") or ""
        did = self.db.add_decision(
            self.novel_id,
            title=node["title"], situation=node["description"],
            stakes=node["stakes"], trigger_type=node["trigger_type"] or "moral",
            actor_type="character" if actor else "world",
            actor_id=actor["id"] if actor else None,
            actor_name=node["actor_name"],
            options=[{"label": c["title"], "description": c["description"],
                      "consequence_hint": c["branch_hint"], "tendency": ""}
                     for c in kids],
            chapter_ref=0,
            note=(note + "｜" if note else "") +
                 ("" if resolved else "推演落定时这个岔路口还没定，留待后续处理。"),
            branch_id=branch_id or self._node_branch(node["id"]),
            node_id=node["id"])
        if not resolved:
            # 用户先收手去写正文了：留成未决点，别替他拍板
            return did
        # 岔路已经在树上拍过板了，这里落成"已决定"的历史记录（供正文与检查清单追溯）
        self.db.resolve_decision(
            did, chosen_option=max(0, idx),
            chosen_content="（你在剧情树上定的）%s" % content,
            chosen_by="user",
            impact={"via": "story_tree", "options_considered": len(kids)})
        return did


# ============================================================ 视图 / 工具

def _card(event):
    """事件卡的结构化三行：做了什么 / 想要什么 / 结果。

    事件卡上真正好读的是这三行，它们本来就在 payload.event 里 —— 但过去
    `_node_view` 压根没往前面送，前端页面上只剩一个 description 可看。
    模型知道 description 是唯一出口，就把这三行揉成一段两百多字的散句，
    一个句号接一个逗号地铺下来，卡片自然"看着难受"。

    decision / option / root 的 payload 结构和 event 不一样（分别是
    {"raw": ...} / 选项对象 / 世界前提），这里一律宽容取不到就返回空，
    不要为了一致性去给它们编字段。"""
    ev = (event or {}).get("event") or {}
    if not isinstance(ev, dict):
        return {}
    opens = []
    for t in (ev.get("open_threads") or [])[:3]:
        if isinstance(t, dict) and str(t.get("title") or "").strip():
            opens.append(str(t["title"]).strip())
        elif isinstance(t, str) and t.strip():
            opens.append(t.strip())
    return {
        "action": str(ev.get("action") or "").strip(),
        "intent": str(ev.get("intent") or "").strip(),
        "result": str(ev.get("result") or "").strip(),
        "opens": opens,
    }


def _node_view(n):
    if not n:
        return None
    return {
        "id": n["id"], "parent_id": n["parent_id"], "depth": n["depth"],
        "seq": n["seq"], "kind": n["kind"], "title": n["title"],
        "description": n["description"], "event_type": n["event_type"],
        "card": _card(n.get("payload")),
        "importance": n["importance"], "actor_name": n["actor_name"],
        "trigger_type": n["trigger_type"], "stakes": n["stakes"],
        "status": n["status"],
        "status_label": STATUS_LABELS.get(n["status"], n["status"]),
        # ▲ v6.9：岔路的 note 现在承载"这条岔路是怎么长出来的"
        # （由「<forks_from>」逼出来：<reason>）。以前 reason 是空的、
        # note 也没暴露给前端，于是岔路口卡片只有一句"要不要签"，
        # 用户看不出它凭什么出现在这儿。
        "note": n.get("note") or "",
        "forks_from": (n.get("payload") or {}).get("forks_from") or "",
        "reason": (n.get("payload") or {}).get("reason") or "",
        "branch_hint": n.get("branch_hint") or "",
        "world_time": n.get("world_time") or "",
        "involved_characters": n.get("involved_characters") or [],
        "involved_entities": n.get("involved_entities") or [],
        "event_ref": n.get("event_ref"),
        # v6.7：分支与 Δ
        "branch_id": n.get("branch_id") or 0,
        "tick": n.get("tick") or 0,
        "has_delta": bool(n.get("state_delta")),
        "delta_keys": list((n.get("state_delta") or {}).keys())[:6],
        "state_delta": n.get("state_delta") or {},
        "fact_count": len(n.get("fact_delta") or []),
        "knowledge_count": len(n.get("knowledge_delta") or []),
        "delta_applied": bool(n.get("delta_applied")),
    }


def _normalize_option(o):
    return {
        "label": str(o.get("label") or "").strip()[:24],
        "description": str(o.get("description") or "").strip(),
        "consequence_hint": str(o.get("consequence_hint") or "").strip(),
        "tendency": str(o.get("tendency") or "").strip(),
    }


def _fallback_options():
    """LLM 选项生成失败时的保底三选项（和决策台保持同一套口径）。"""
    return [
        {"label": "直接面对", "description": "正面处理这件事，不回避",
         "consequence_hint": "后果立刻显现，但有主动权", "tendency": "果断"},
        {"label": "暂时搁置", "description": "先放着，观察局势再定",
         "consequence_hint": "赢得时间，但问题会发酵", "tendency": "稳妥"},
        {"label": "另寻他路", "description": "绕过眼前的局面，从别处入手",
         "consequence_hint": "规避风险，但可能错失关键信息", "tendency": "迂回"},
    ]
