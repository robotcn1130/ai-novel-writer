# -*- coding: utf-8 -*-
"""「发布 = 确认进世界」链路回归（conform_chapter）。

背景（用户明确要求）：
    **点击发布 = 作者确认这一章的内容成立**，所以这一刻就要让世界状态
    因为这些内容而更新。旧版只翻了个 status 字段，世界页面永远停在推演
    时的那本旧账上——这是本次要钉死的回归点。

不需要 LLM：extract_and_apply_state 全程打桩，只验 confirm_chapter 的
**决策与映射**（幂等、档位未配、硬冲突拦截、异常不炸穿、字段名对得上）。

约定：指向**真实库的副本**，绝不写 sql/novel.db。
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src_v6"))
sys.path.insert(0, ROOT)

from core import database as DBmod          # noqa: E402
import generate.prose as prose              # noqa: E402

REAL_DB = os.path.join(ROOT, "sql", "novel.db")

OK = 0
BAD = 0


def check(label, got, want):
    global OK, BAD
    if got == want:
        OK += 1
        print("  ok   %-48s %r" % (label, got))
    else:
        BAD += 1
        print("  FAIL %-48s got=%r want=%r" % (label, got, want))


class FakeRouter:
    """可控 router 桩。calls 记录抽取被调用了几次。"""

    def __init__(self, ready=True):
        self._ready = ready
        self.calls = 0

    def is_ready(self, slot):
        return (self._ready, "" if self._ready else "未配置")

    def run_json(self, *a, **k):
        return {}, "", "stub"


def main():
    tmp = tempfile.mkdtemp(prefix="t_confirm_")
    copy = os.path.join(tmp, "novel.db")
    if os.path.isfile(REAL_DB):
        shutil.copy2(REAL_DB, copy)
    else:
        # 干净 clone 里 sql/novel.db 还没生成：现建一个空库当底子。
        # 早先这里直接 return 0 跳过，连 `=== OK=` 都不打 ——
        # 总入口只看到「通过」，其实一条断言没跑。
        print("  （没找到真实库，用临时空库）")
        DBmod.ensure_db(copy)

    db = DBmod.open_db(copy)
    ORIG = prose.extract_and_apply_state
    try:
        # 挑一个真有章节的小说当靶子；**一本都没有就现造一个**。
        # 干净库上走 `return 0` 会变成「零断言的通过」，比失败更危险。
        rows = db.query("SELECT novel_id, chapter_number FROM chapters "
                        "ORDER BY novel_id, chapter_number LIMIT 1")
        if rows:
            nid = int(rows[0]["novel_id"])
        else:
            nid = db.create_novel("__发布确认回归_临时世界__")["id"]
            print("  （副本库里没有章节，已新建临时世界 %s）" % nid)
        probe = 99001                       # 专门造的桩章号，不碰真数据

        def gen(router):
            return prose.ProseGenerator(db, router, nid)

        # ---------------------------------------------------- 1 章节不存在
        check("章节不存在 -> None", gen(FakeRouter()).confirm_chapter(999999), None)
        check("章号 0 -> None", gen(FakeRouter()).confirm_chapter(0), None)

        # 造一个桩章
        db.execute("INSERT INTO chapters(novel_id, chapter_number, title, content,"
                   " word_count, status) VALUES(?,?,?,?,?,?)",
                   (nid, probe, "__桩_确认章__", "", 0, "draft"))

        # ---------------------------------------------------- 2 空正文
        out = gen(FakeRouter()).confirm_chapter(probe)
        check("空正文 -> applied False", out["applied"], False)
        check("空正文 -> skipped 有说法", bool(out["skipped"]), True)

        # 填上正文
        db.execute("UPDATE chapters SET content=?, word_count=? "
                   "WHERE novel_id=? AND chapter_number=?",
                   ("正文" * 80, 160, nid, probe))

        # ---------------------------------------------------- 3 幂等
        db.execute("UPDATE chapters SET verified=1 WHERE novel_id=? "
                   "AND chapter_number=?", (nid, probe))
        r = FakeRouter()
        out = gen(r).confirm_chapter(probe)
        check("[幂等] 已确认 -> applied True", out["applied"], True)
        check("[幂等] 已确认 -> already True", out["already"], True)
        check("[幂等] 已确认 -> 不重复抽正文", r.calls, 0)
        check("[幂等] 已确认 -> 提示可用重新确认",
              "重新确认" in (out["skipped"] or ""), True)
        # force=True 必须能穿透幂等，否则正文改过就没法回流
        db.execute("UPDATE chapters SET verified=0 WHERE novel_id=? "
                   "AND chapter_number=?", (nid, probe))

        # ---------------------------------------------------- 4 档位未配
        out = gen(FakeRouter(ready=False)).confirm_chapter(probe)
        check("[档位] 未配 -> applied False", out["applied"], False)
        check("[档位] 未配 -> 点名 state_extract",
              "state_extract" in (out["skipped"] or ""), True)

        # ---------------------------------------------------- 5 正常回流
        def fake_ok(db_, router_, novel_id, text, chapter_ref=0, **k):
            return {"applied": True, "conflicts": [], "pending": 3,
                    "world_state": ["铜价", "风声"],
                    "characters": ["林晚"],
                    "knowledge": [{"name": "林晚", "knowledge": "她知道扣子的事"}],
                    "new_threads": ["尾款"], "resolved_threads": ["旧账"],
                    "new_characters": ["老周"]}

        prose.extract_and_apply_state = fake_ok
        out = gen(FakeRouter()).confirm_chapter(probe)
        check("[正常] applied True", out["applied"], True)
        check("[正常] 世界状态映射", out["world_state"], ["铜价", "风声"])
        check("[正常] 人物映射", out["characters"], ["林晚"])
        check("[正常] 认知映射", len(out["knowledge"]), 1)
        check("[正常] 新角色映射", out["new_characters"], ["老周"])
        check("[正常] 待裁决条数", out["pending"], 3)
        check("[正常] 已回收线索", out["resolved_threads"], ["旧账"])
        check("[正常] skipped 为空", out["skipped"], "")

        # ---------------------------------------------------- 6 硬冲突拦截
        def fake_hard(db_, router_, novel_id, text, chapter_ref=0, **k):
            return {"applied": False, "conflicts": [{"severity": "error"}] * 2,
                    "pending": 0, "world_state": [], "characters": [],
                    "knowledge": [], "new_threads": [], "resolved_threads": [],
                    "new_characters": []}

        prose.extract_and_apply_state = fake_hard
        out = gen(FakeRouter()).confirm_chapter(probe)
        check("[冲突] applied False", out["applied"], False)
        check("[冲突] 冲突条数映射", len(out["conflicts"]), 2)
        check("[冲突] 引导去裁决", "裁决" in (out["skipped"] or ""), True)

        # ---------------------------------------------------- 7 抽不出结果
        prose.extract_and_apply_state = \
            lambda *a, **k: None
        out = gen(FakeRouter()).confirm_chapter(probe)
        check("[空抽取] applied False", out["applied"], False)
        check("[空抽取] 引导重试",
              "重新确认" in (out["skipped"] or ""), True)

        # ---------------------------------------------------- 8 抽取抛异常
        def boom(*a, **k):
            raise RuntimeError("网关 502")

        prose.extract_and_apply_state = boom
        out = gen(FakeRouter()).confirm_chapter(probe)
        check("[异常] 不炸穿调用方", out["applied"], False)
        check("[异常] 错误写进 skipped",
              "RuntimeError" in (out["skipped"] or ""), True)

    finally:
        prose.extract_and_apply_state = ORIG
        db.close()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n=== OK=%d BAD=%d" % (OK, BAD))
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
