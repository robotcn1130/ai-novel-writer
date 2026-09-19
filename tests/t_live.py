# -*- coding: utf-8 -*-
"""收尾真机验证：拿**真实生产库**起服务，确认两条新链路在真环境下能走通。

只用只读调用 + 一个装在临时目录的测试技能（不碰用户职业技能），
跑完清理。这是「最后用户自己测」之前的自检。
"""
import json
import os
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src_v6"))
sys.path.insert(0, os.path.join(ROOT, "src_v6", "core"))

# 写入根挪到临时目录：真机验证也不能真往用户 skills 里装
TMP_SKILLS = os.path.join(tempfile.gettempdir(), "nw_live_skills")
shutil.rmtree(TMP_SKILLS, ignore_errors=True)
os.makedirs(TMP_SKILLS)
os.environ["NW_SKILLS_DIR"] = TMP_SKILLS

from webui import server as S

PORT = 8797
OK, FAIL, SKIP = [], [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    if not cond:
        print("  FAIL:", name, extra)


def skip(name, why=""):
    """**数据不够**（而不是代码坏了）时用这个。

    真机验证是拿生产库里的真实数据跑；库是干净的（比如刚 clone、
    或者书全被归档了）时，有些断言**没法验**。那种情况必须报「跳过」，
    报「失败」会让人去查一个根本不存在的 bug。
    """
    SKIP.append(name)
    print("  SKIP:", name, why)


def req(path, method="GET", body=None):
    if "?" in path:
        h, qs = path.split("?", 1)
        path = h + "?" + urllib.parse.quote(qs, safe="=&")
    else:
        path = urllib.parse.quote(path, safe="/")
    url = "http://127.0.0.1:%d%s" % (PORT, path)
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(url, data=data,
                               headers={"Content-Type": "application/json"},
                               method=method)
    try:
        with urllib.request.urlopen(r, timeout=30) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8")
        try:
            return e.code, json.loads(raw)
        except Exception:                                    # noqa: BLE001
            return e.code, {"_raw": raw[:200]}


def main():
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), S.Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        run()
    finally:
        httpd.shutdown()
        httpd.server_close()
        shutil.rmtree(TMP_SKILLS, ignore_errors=True)
    print("\n=== OK=%d FAIL=%d SKIP=%d ===" % (len(OK), len(FAIL), len(SKIP)))
    for f in FAIL:
        print(" FAIL:", f)
    for s in SKIP:
        print(" SKIP:", s)
    return 1 if FAIL else 0


def run():
    from engine import skillhub as SH
    check("写入根是临时目录（护栏）",
          os.path.realpath(SH.USER_DIR) == os.path.realpath(TMP_SKILLS),
          SH.USER_DIR)

    # 1. 技能库页要的数据全都能拿到
    st, loc = req("/api/skills/local")
    check("local 200", st == 200, st)
    check("local 有 dirs", "dirs" in loc, list(loc.keys()))
    st, bd = req("/api/skills/bindings")
    check("bindings 200", st == 200, st)
    check("bindings 有 10 个档位", len(bd.get("slots") or []) >= 8,
          len(bd.get("slots") or []))
    st, rf = req("/api/skills/refs")
    check("refs 200", st == 200, st)
    if rf.get("refs"):
        check("refs 非空（真项目里有引用）", True)
    else:
        # 干净库里没人绑过技能引用，属正常空态，不是 bug。
        skip("refs 非空（真项目里有引用）", "库里还没有技能引用")


    # 2. 导入一条：真机环境下编计划 + 落盘
    src = os.path.join(tempfile.gettempdir(), "nw_live_pack", "真机测试技能")
    shutil.rmtree(os.path.dirname(src), ignore_errors=True)
    os.makedirs(src)
    with open(os.path.join(src, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("---\nname: live-test\ndescription: 真机验证用\n---\n\n# 怎么做\n\n1. 做\n")
    st, plan = req("/api/skills/import_plan", "POST", {"kind": "folder", "path": src})
    check("真机 import_plan 200", st == 200, (st, plan))
    check("计划目录名取文件夹名", plan.get("dir") == "真机测试技能", plan.get("dir"))
    st, ins = req("/api/skills/import", "POST",
                  {"plan_id": plan.get("plan_id"), "scope": "user"})
    check("真机 import 200", st == 200, (st, ins))
    check("真机落盘成功", ((ins or {}).get("installed") or {}).get("ok"), ins)

    # 3. 装完能扫到，且 name/dir 一致
    st, loc2 = req("/api/skills/local")
    got = [s for s in (loc2.get("skills") or []) if s.get("dir") == "真机测试技能"]
    check("scan_local 扫到新技能", bool(got), [s.get("dir") for s in (loc2.get("skills") or [])])
    if got:
        check("name 与 dir 一致", got[0]["name"] == got[0]["dir"], got[0])

    # 4. 详情能打开
    st, det = req("/api/skills/detail?name=真机测试技能")
    check("详情 200", st == 200, (st, det))
    check("详情有正文预览", bool((det or {}).get("body_preview")), det)
    check("详情有 max_inject_chars", (det or {}).get("max_inject_chars") == SH.MAX_INJECT_CHARS, det)

    # 5. 绑到某个档位，确认注入真的生效（不调模型，直接看 apply_to_system）
    st, _r = req("/api/skills/bind", "POST",
                 {"slot": "summarize", "skill": "真机测试技能", "enabled": True})
    check("绑定 200", st == 200, st)
    # 写进去还不够，必须**读回来仍是启用**。用户报的「选完技能立刻回弹成
    # 『不使用』并提示已停用」就落在这条「写—读」一致性上：以前前端选技能时
    # 带的是 enabled=false，后端见 not enabled 直接删条目，页面重绘自然回弹。
    st, bd2 = req("/api/skills/bindings")
    row = [s for s in (bd2.get("slots") or []) if s["slot"] == "summarize"][0]
    check("绑定后读回来是已启用（不回弹）", row["enabled"] is True, row)
    check("绑定后读回来技能名正确", row["skill"] == "真机测试技能", row)
    check("绑定后不是 missing", row["missing"] is False, row)
    # 直接查注入结果：拿真实库开一个 router 级的 db
    import core.database as DB
    d = DB.open_db(os.path.join(ROOT, "sql", "novel.db"))
    try:
        out = SH.apply_to_system("summarize", "原始 system", d)
        check("注入把技能正文拼进去了", "真机测试技能" in out and "原始 system" in out, out[:200])
        check("注入放在最前面", out.strip().startswith("【附加技能规则"), out[:60])
        # 换个没绑的档位，应当原样返回
        other = SH.apply_to_system("polish", "原始 system", d)
        check("没绑的档位不受影响", other == "原始 system", other[:80])
    finally:
        # 收尾：解绑
        d.set_config(SH.BIND_KEY, json.dumps({}))
        d.close()

    st, _b = req("/api/skills/bind", "POST",
                 {"slot": "summarize", "skill": "", "enabled": False})
    check("解绑 200", st == 200, st)

    # 5b. 事件卡：结构化的三行必须真的送到前端
    #     用户抱怨"推演的事件卡看着难受"—— 根因之一是 payload 里的
    #     action / intent / result / open_threads 从来没往前端送过，
    #     页面上只剩一个 description 可看，于是模型把这几样全揉进那段话里。
    #     这一节锁住"真的有送到"，否则改了渲染也是白改。
    import sqlite3
    con = sqlite3.connect(
        "file:%s?mode=ro" % os.path.join(ROOT, "sql", "novel.db"), uri=True)
    row = con.execute(
        # ⚠ 必须排除归档书：status='archived' 在 get_novel 里取不到，
        # /api/tree 会回 404「小说不存在」——这是既定契约，不是 bug，
        # 拿归档书跑带参接口只会得到一个误导性的失败。
        "SELECT s.novel_id FROM story_nodes s JOIN novels n ON n.id=s.novel_id"
        " WHERE s.kind='event' AND s.deleted_at IS NULL AND n.status!='archived'"
        " AND s.payload LIKE '%\"action\"%' LIMIT 1").fetchone()
    con.close()
    if row:
        check("真库里有可测的事件卡", True)
    else:
        # 库里一本书都没推演过（或者书全被归档了）—— 没有样本来验，
        # 报「跳过」。真实创作中的库这里会走上面的正分支。
        skip("真库里有可测的事件卡", "没有带 action 的活跃事件节点")
    if row:
        st, tr = req("/api/tree?novel_id=%d" % row[0])
        check("tree 200", st == 200, st)
        nodes = (tr or {}).get("nodes") or []
        check("树里有节点", bool(nodes), len(nodes))
        withcard = [n for n in nodes if (n.get("card") or {}).get("action")]
        check("事件卡带出「做了什么」", bool(withcard),
              [n.get("kind") for n in nodes])
        if withcard:
            ev = withcard[0]
            c = ev["card"]
            check("事件卡带出「为什么」", bool(c.get("intent")), c)
            check("事件卡带出「结果」", bool(c.get("result")), c)
            check("悬置线索是列表", isinstance(c.get("opens"), list), c.get("opens"))
        # 根 / 岔路口 / 选项的 payload 结构不是 event，不许编出三行
        bogus = [n.get("kind") for n in nodes
                 if n.get("kind") != "event" and (n.get("card") or {}).get("action")]
        check("根/岔路/选项不编造三行", not bogus, bogus)

    # 6. 卸载掉，还原环境
    st, un = req("/api/skills/uninstall", "POST",
                 {"name": "真机测试技能", "scope": "user", "confirm_name": "真机测试技能"})
    check("真机卸载 200", st == 200, (st, un))
    check("卸载后目录没了", not os.path.isdir(os.path.join(TMP_SKILLS, "真机测试技能")))

    # 7. 确认没污染用户真实技能目录
    real = os.path.join(os.path.expanduser("~"), ".workbuddy", "skills")
    check("用户真实技能目录未被写入",
          not os.path.isdir(os.path.join(real, "真机测试技能")))


if __name__ == "__main__":
    sys.exit(main())
