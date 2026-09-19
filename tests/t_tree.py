# -*- coding: utf-8 -*-
"""剧情树引擎的单元回归（不需要 LLM）。

真实事故（必须钉死）：
  `_rounds_since_decision` 早期版本把整行 dict 赋给游标
  （`cur = row.get("parent_id")` 误写成 `cur = row`），第二轮
  `get_story_node(dict)` 直接抛：
      sqlite3.ProgrammingError: Error binding parameter 1: type 'dict' is not supported
  用户在点「推演」时看到的就是这个 502。所以这里用**真实库**造一条
  event→event→decision 的链，确保游标每次都是整数 id。
  测试床自动挑选：优先复用一本已有剧情树的小说，都没有时临时新建一个
  （跑完删除），**不写死 novel_id** —— 写死会在用户清理数据库后静默跳过。

同时也覆盖 v6.9 新增的人事变动通道 `_apply_churn` 的健壮性：
  · 垃圾输入（None / 字符串）必须静默返回空结果，不能把推演整个搞崩
  · 已在档的角色不重复建档（幂等）
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "src_v6")
for _p in (SRC, os.path.join(SRC, "core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    if not cond:
        print("  FAIL:", name, extra)


def main():
    from core import database as DBmod
    from engine.tree import StoryTree

    db = DBmod.open_db()

    # 选一本**已经有剧情树**的小说当测试床。
    # 早期版本写死 novel_id=77；用户一旦清理数据库，这里就直接
    # `OK=0 FAIL=0` 静默跳过 —— 看着像通过，其实守卫完全没跑。
    # 所以改为：优先复用真实数据，一本都没有时现建一个一次性的。
    nid, scratch = None, False
    for n in (db.list_novels() or []):
        if db.list_story_nodes(n["id"]):
            nid = n["id"]
            break
    if nid is None:
        nid = db.create_novel("__剧情树回归_临时世界__")["id"]
        scratch = True
    print("  测试床：小说 %s%s" % (nid, "（临时新建）" if scratch else ""))

    root = db.story_root(nid) or {}
    rootid = root.get("id")
    if rootid is None:
        rootid = db.add_story_node(nid, kind="root", title="__回归_根__",
                                   description="测试", event_type="plot")

    tree = StoryTree(db, None, nid)
    made = []
    try:
        # ---- 1. 造一条 event → event → decision 的链 ----
        pid = rootid
        for i in range(2):
            pid = db.add_story_node(
                nid, kind="event", parent_id=pid,
                title="__回归_事件%d__" % (i + 1),
                description="测试", event_type="plot")
            made.append(pid)
        did = db.add_story_node(nid, kind="decision", parent_id=pid,
                                title="__回归_岔路__",
                                description="测试", event_type="decision")
        made.append(did)

        # ---- 2. 不能崩：这就是当初 502 的根因 ----
        try:
            n = tree._rounds_since_decision(did)
            check("_rounds_since_decision 沿 parent_id 走不崩", True)
        except Exception as e:                 # noqa: BLE001
            check("_rounds_since_decision 沿 parent_id 走不崩", False, repr(e))
            n = None

        # ---- 3. 数得对（注意：起点自己也算一段）----
        check("起点是岔路 → 0", tree._rounds_since_decision(did) == 0,
              tree._rounds_since_decision(did))
        # 链：root(444) → made[0](event) → made[1](event) → did(decision)
        # 从 made[1] 往上：自己 + made[0] = 2 个事件，再往上是 root（不计）
        check("从第2个事件往上 → 2", tree._rounds_since_decision(made[1]) == 2,
              tree._rounds_since_decision(made[1]))
        # 从 made[0] 往上：只有自己 = 1 个事件，再往上是 root（不计）
        check("从第1个事件往上 → 1（父是 root，root 不计）",
              tree._rounds_since_decision(made[0]) == 1,
              tree._rounds_since_decision(made[0]))
        check("起点是 root → 0", tree._rounds_since_decision(rootid) == 0,
              tree._rounds_since_decision(rootid))

        # ---- 4. 游标类型守卫：parent_id 必须是整数或 None ----
        row = db.get_story_node(made[0])
        check("parent_id 不是 dict",
              not isinstance(row.get("parent_id"), dict),
              type(row.get("parent_id")).__name__)

        # ---- 5. 边界输入 ----
        check("node_id=0 → 0", tree._rounds_since_decision(0) == 0)
        check("node_id=None → 0", tree._rounds_since_decision(None) == 0)

        # ---- 6. 压力文案三档，且不该逼模型造岔路 ----
        check("0 段 → 无提示", tree._decision_pressure(0) == "")
        check("3 段 → 弱提示", "留意" in tree._decision_pressure(3))
        check("6 段 → 强提示", "正面撞上" in tree._decision_pressure(6))
        for k in (0, 3, 6, 10):
            t = tree._decision_pressure(k)
            check("压力文案（%d段）不含「必须」" % k, "必须" not in t, t)

        # ---- 7. _apply_churn 对垃圾输入要静默 ----
        check("parsed=None → 空结果",
              tree._apply_churn(None) == {"new": [], "exited": []})
        check("parsed='x' → 空结果",
              tree._apply_churn("x") == {"new": [], "exited": []})
        check("parsed=[1] → 空结果",
              tree._apply_churn([1]) == {"new": [], "exited": []})

        # ---- 8. 已在档的角色不能被重复建（幂等）----
        existing = (db.list_characters(nid) or [{}])[0].get("name")
        if existing:
            before = db.scalar(
                "SELECT COUNT(*) FROM characters WHERE novel_id=?", (nid,))
            tree._apply_churn({"new_characters": [
                {"name": existing, "role_tag": "x", "goal": "y"}]})
            after = db.scalar(
                "SELECT COUNT(*) FROM characters WHERE novel_id=?", (nid,))
            check("已在档角色不重复建档", before == after,
                  "%s -> %s" % (before, after))

        # ---- 9. 退场枚举守卫：非法状态要忽略 ----
        if existing:
            st0 = (db.get_character_by_name(nid, existing) or {}).get("status")
            tree._apply_churn({"character_exits": [
                {"name": existing, "status": "banana", "reason": "x"}]})
            st1 = (db.get_character_by_name(nid, existing) or {}).get("status")
            check("非法退场状态被忽略", st0 == st1, "%s -> %s" % (st0, st1))

    finally:
        # 先删造出来的节点；临时世界再连根拔掉（FK 级联清子表）。
        if made:
            with db.tx():
                for i in made:
                    db.execute("DELETE FROM story_nodes WHERE id=?", (i,))
        if scratch:
            db.delete_novel(nid)

    print("")
    print("=== OK=%d FAIL=%d ===" % (len(OK), len(FAIL)))
    for f in FAIL:
        print(" FAIL:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
