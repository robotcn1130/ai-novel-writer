# -*- coding: utf-8 -*-
"""技能库 v6.9 测试总入口。

    python tests/run_all.py

十套（只有「真机验证」需要真实数据，其它都能在干净库上跑）：
  t_regress.py     全站只读接口回归（dispatch 是所有路由的必经处，必须常跑）
  t_import_http.py 导入链路（文件夹/zip/md、改名、二次确认、卸载）
  t_gen.py         AI 生成链路的收口（喂脏数据，验证产出一定能装）
  t_render.js      前端渲染桩（模板字符串、字段名、undefined 泄漏）
  t_bind.js        绑定交互（复选框与下拉联动；「选技能被当成停用」的死锁）
  t_prompt.py      世界推演提示词的关键约束（事件卡形态，防静默回退）
  t_tree.py        剧情树引擎回归（游标类型、人事变动通道；不需要 LLM）
  t_confirm.py     发布=确认进世界（幂等/档位/硬冲突/异常；不需要 LLM）
  t_know.py        认知边界 has_knowledge（SQL 字面 % 撞格式化；不需要 LLM）
  t_live.py        真机（真实库 + 临时技能目录，验注入真生效）
                   库里没数据时相关断言报 SKIP，不算失败。

约定（跟项目里其它测试一致）：
  · 一律指向**副本库/临时目录**，不碰 sql/novel.db，不进项目 .workbuddy/skills
  · 自己起独立端口的 HTTP 服务，跑完关掉释放端口
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HERE = os.path.dirname(os.path.abspath(__file__))

PY = sys.executable
# 用托管 Node，别依赖用户机器上的 node
NODE = os.environ.get("NW_NODE") or (
    r"C:\Users\robot\.workbuddy\binaries\node\versions\22.22.2-3\node.exe")

SUITES = [
    ("全站接口回归", [PY, os.path.join(HERE, "t_regress.py")]),
    ("导入链路", [PY, os.path.join(HERE, "t_import_http.py")]),
    ("AI 生成收口", [PY, os.path.join(HERE, "t_gen.py")]),
    ("前端渲染桩", [NODE, os.path.join(HERE, "t_render.js")]),
    ("绑定交互", [NODE, os.path.join(HERE, "t_bind.js")]),
    ("推演提示词", [PY, os.path.join(HERE, "t_prompt.py")]),
    ("剧情树引擎", [PY, os.path.join(HERE, "t_tree.py")]),
    ("发布确认进世界", [PY, os.path.join(HERE, "t_confirm.py")]),
    ("认知边界", [PY, os.path.join(HERE, "t_know.py")]),
    # 放最后：它开的是**真实生产库**（只读 + 临时技能目录），
    # 需要真数据才有意义，所以不跟前面几套共用桩数据。
    ("真机验证", [PY, os.path.join(HERE, "t_live.py")]),
]


def main():
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    results = []
    for name, cmd in SUITES:
        if not os.path.isfile(cmd[1]):
            results.append((name, "跳过（文件不在）", ""))
            continue
        if cmd[0] == NODE and not os.path.isfile(NODE):
            results.append((name, "跳过（没找到 node）", ""))
            continue
        print("\n" + "=" * 60)
        print("  " + name)
        print("=" * 60)
        p = subprocess.run(cmd, cwd=ROOT, env=env,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace")
        out = (p.stdout or "") + (p.stderr or "")
        tail = [ln for ln in out.splitlines()
                if ln.startswith("=== OK=") or ln.startswith(" FAIL:")
                or ln.startswith(" SKIP:")]
        print("\n".join(tail) if tail else out[-500:])
        results.append((name, "通过" if p.returncode == 0 else "失败",
                        " / ".join(x.strip() for x in tail if x.startswith("==="))))

    print("\n" + "=" * 60)
    print("  汇总")
    print("=" * 60)
    for name, verdict, summary in results:
        print("  %-16s %-6s %s" % (name, verdict, summary))
    bad = [r for r in results if r[1] == "失败"]
    print("\n%s" % ("全部通过。" if not bad else "有 %d 套失败。" % len(bad)))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
