# -*- coding: utf-8 -*-
"""认知边界（character_knowledge）回归。

钉死 2026-09-19 暴露的格式化 bug：

    db.has_knowledge() 里 SQL 用了 `'%' || about_text || '%'`，
    同时又用 `% marks` 做字符串格式化 —— SQL 里的**字面百分号**
    被 `%` 运算符当成占位符，而我们只补了 marks 一个参数，于是

        TypeError: not enough arguments for format string

    这个函数平时很少被走到（只有"信息边界校验"才调），一旦走到就必炸。
    它藏在 `facts.check_projection_conflicts` 里，**正文回流**一跑就撞上，
    表现成"发布了但世界没更新"——排查成本极高。

不碰生产库：只用内存库建最小表。
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src_v6"))

from core import database as DBmod          # noqa: E402

OK = 0
BAD = 0


def check(label, got, want):
    global OK, BAD
    if got == want:
        OK += 1
        print("  ok   %-52s %r" % (label, got))
    else:
        BAD += 1
        print("  FAIL %-52s got=%r want=%r" % (label, got, want))


def main():
    # open_db 要求文件已存在（它不做 create），所以拿真实库的**副本**当底座，
    # 不碰 sql/novel.db。表结构直接复用，不再自己 CREATE TABLE。
    real = os.path.join(ROOT, "sql", "novel.db")
    if not os.path.isfile(real):
        print("!! 找不到 %s，跳过" % real)
        return 0
    tmp = tempfile.mkdtemp(prefix="t_know_")
    path = os.path.join(tmp, "novel.db")
    shutil.copy2(real, path)
    db = DBmod.open_db(path)
    try:
        # ---------------------------------------------------------- 造桩数据
        # 用一个不存在的小说 id（9900），绝不碰真实数据
        NID = 9900
        db.execute("INSERT INTO novels(id,title) VALUES(?,?)", (NID, "__桩_知识测试__"))
        db.execute("INSERT INTO characters(novel_id,name) VALUES(?,?)", (NID, "桩甲"))
        db.execute("INSERT INTO characters(novel_id,name) VALUES(?,?)", (NID, "桩乙"))
        c1 = db.one("SELECT id FROM characters WHERE novel_id=? AND name=?",
                    (NID, "桩甲"))["id"]
        c2 = db.one("SELECT id FROM characters WHERE novel_id=? AND name=?",
                    (NID, "桩乙"))["id"]

        db.execute("INSERT INTO character_knowledge(novel_id,character_id,content,"
                   "about_text,about_type,know_type,status,source,confidence) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (NID, c1, "他知道铜扣缺了一道", "铜扣", "fact",
                    "know", "active", "told", 4))
        db.execute("INSERT INTO character_knowledge(novel_id,character_id,content,"
                   "about_text,about_type,know_type,status,source,confidence) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (NID, c1, "他以为林晚走了", "林晚", "fact",
                    "believe", "active", "inferred", 3))

        # -------------------------------------------------- ★ 核心：不许炸
        # 这一句在修复前会抛 TypeError: not enough arguments for format string
        try:
            db.has_knowledge(NID, c1, "铜扣")
            check("[格式] 单元素 allow_types 不抛异常", True, True)
        except Exception as e:                       # noqa: BLE001
            check("[格式] 单元素 allow_types 不抛异常 (%s)" % type(e).__name__,
                  False, True)

        for n in (2, 3, 4, 5):
            try:
                db.has_knowledge(NID, c1, "林晚",
                                 allow_types=tuple("x" * n))
                check("[格式] %d 个 allow_types 不抛异常" % n, True, True)
            except Exception as e:                   # noqa: BLE001
                check("[格式] %d 个 allow_types 不抛异常 (%s)"
                      % (n, type(e).__name__), False, True)

        # -------------------------------------------------- 语义：方向取宽
        check("[语义] 命中 about_text -> True", db.has_knowledge(NID, c1, "铜扣"), True)
        check("[语义] 命中 content 里的词 -> True",
              db.has_knowledge(NID, c1, "缺了一道"), True)
        check("[语义] 不相关 -> False",
              db.has_knowledge(NID, c1, "完全无关的词"), False)
        check("[语义] 空文本 -> True（不拦）",
              db.has_knowledge(NID, c1, ""), True)

        # 默认 allow_types=('know',) 只看 know，不该看见 believe 那条
        check("[语义] 默认只认 know -> 看不见 believe",
              db.has_knowledge(NID, c1, "林晚"), False)
        check("[语义] 放开 believe 就看得见",
              db.has_knowledge(NID, c1, "林晚",
                               allow_types=("know", "believe")), True)

        # -------------------------------------------------- 跨角色 / 跨书隔离
        check("其它 book 看不到 -> False",
              db.has_knowledge(9901, c1, "铜扣"), False)
        db.execute("UPDATE character_knowledge SET status='refuted' "
                   "WHERE novel_id=? AND character_id=? AND about_text=?",
                   (NID, c1, "铜扣"))
        check("作废的认知不算 -> False",
              db.has_knowledge(NID, c1, "铜扣"), False)

        # -------------------------------------------------- 地点：只在同一处才不拦
        from engine.facts import _same_place_path as same
        check("同家两屋 -> True", same("樟溪镇 · 沈家 · 堂屋", "樟溪镇 · 沈家 · 灶间"), True)
        check("同家院子 -> True", same("樟溪镇 · 沈家 · 堂屋", "樟溪镇 · 沈家 · 院子（劈柴）"), True)
        check("正史粗正文细 -> True", same("樟溪镇 · 沈家", "樟溪镇 · 沈家 · 东屋"), True)
        check("正文粗正史细 -> True", same("樟溪镇 · 沈家 · 堂屋", "樟溪镇"), True)
        check("跨地方 -> False", same("樟溪镇 · 沈家", "临安 · 古玩城"), False)
        check("不同家 -> False", same("樟溪镇 · 沈家 · 堂屋", "樟溪镇 · 谢家 · 堂屋"), False)
        check("空值 -> False", same("", "樟溪镇"), False)

        # -------------------------------------------------- SQL 里不该再有裸 '%'
        # 防回归：把 `'%'` 直接写进格式化串里就会再炸一次。
        src = open(os.path.join(ROOT, "src_v6", "core", "database.py"),
                   encoding="utf-8").read()
        i = src.find("def has_knowledge")
        body = src[i:i + 2200]
        check("源码里有防炸说明",
              "@PCT@" in body or "not enough arguments" in body, True)

    finally:
        try:
            db.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    print("\n=== OK=%d BAD=%d" % (OK, BAD))
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
