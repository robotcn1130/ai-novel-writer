# -*- coding: utf-8 -*-
"""技能库 v6.9 · AI 生成链路的收口测试。

模型产出是不可信的：这里专门喂脏数据，验证 parse_gen_output 一定产出
一份「能装、能认、名字跟目录一致」的 SKILL.md。
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src_v6"))
sys.path.insert(0, os.path.join(ROOT, "src_v6", "core"))

USER_SKILLS = os.path.join(tempfile.gettempdir(), "nw_gen_skills")
os.makedirs(USER_SKILLS, exist_ok=True)
# 在 import 之前设环境变量：skillhub 加载时读它决定写入根
os.environ["NW_SKILLS_DIR"] = USER_SKILLS

from engine import skillhub as SH
SH.USER_DIR = USER_SKILLS
assert SH.USER_DIR == USER_SKILLS

OK = []
FAIL = []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    if not cond:
        print("  FAIL:", name, extra)


def main():
    # 护栏：写入根一旦不是临时目录，立刻停下——
    # 测试会真删真写，跑错地方就是把用户装好的技能删了。
    real = os.path.join(os.path.expanduser("~"), ".workbuddy", "skills")
    assert os.path.realpath(SH.USER_DIR) != os.path.realpath(real), \
        "测试写入了用户真实技能目录，已中止"

    # ---- 1. 正常产出 ----
    r = SH.parse_gen_output({
        "name": "prose-deai-taste",
        "description": "检查 AI 腔",
        "content": "---\nname: prose-deai-taste\ndescription: 检查 AI 腔\n---\n\n正文内容\n",
    })
    check("正常产出 ok", bool(r and r.get("ok")), r)
    check("name==dir", r["name"] == r["dir"], r)
    meta, body = SH.parse_skill_md(r["content"])
    check("frontmatter 可解析", bool(meta), meta)
    check("name 与 dir 一致", meta.get("name") == r["dir"], meta)
    check("body_chars 是正文字符数", r["body_chars"] == len(body), (r["body_chars"], len(body)))
    check("body 里不含 frontmatter", "name:" not in body, body[:60])

    # ---- 2. name 脏：大写、空格、下划线 ----
    r = SH.parse_gen_output({"name": "My Skill_V2", "description": "x", "content": "正文\n"})
    check("脏 name 被规范化", r["name"] == "my-skill-v2", r["name"])
    check("脏 name 无空格/大写", r["name"] == r["name"].lower() and " " not in r["name"], r["name"])

    # ---- 3. 无 name ----
    r = SH.parse_gen_output({"name": "", "description": "x", "content": "正文\n"})
    check("空 name 兜底 ai-skill", r["name"] == "ai-skill", r["name"])

    # ---- 4. 无 frontmatter：必须自动补 ----
    r = SH.parse_gen_output({"name": "auto-fm", "description": "自动补", "content": "只有正文\n"})
    check("自动补 frontmatter", r["content"].lstrip().startswith("---"), r["content"][:60])
    meta, _b = SH.parse_skill_md(r["content"])
    check("补出来的 frontmatter 有 name", meta.get("name") == "auto-fm", meta)
    check("补出来的有 description", meta.get("description") == "自动补", meta)

    # ---- 5. frontmatter 里的 name 与 dir 不符：必须被改写 ----
    r = SH.parse_gen_output({"name": "right-name", "description": "x",
                             "content": "---\nname: wrong-name\ndescription: x\n---\n\n正文\n"})
    meta, _b = SH.parse_skill_md(r["content"])
    check("不符的 name 被改齐", meta.get("name") == r["dir"] == "right-name", meta)

    # ---- 6. 无正文：拒绝 ----
    check("空正文返回 None", SH.parse_gen_output({"name": "a", "content": ""}) is None)
    check("非 dict 返回 None", SH.parse_gen_output("not a dict") is None)
    check("None 返回 None", SH.parse_gen_output(None) is None)

    # ---- 7. 缺 description 时补上 ----
    r = SH.parse_gen_output({"name": "no-desc", "description": "补的说明",
                             "content": "---\nname: no-desc\n---\n\n正文\n"})
    meta, _b = SH.parse_skill_md(r["content"])
    check("缺 description 被补上", meta.get("description") == "补的说明", meta)

    # ---- 8. 重名：dir 自动加后缀且 frontmatter 跟着改 ----
    os.makedirs(os.path.join(USER_SKILLS, "dup-skill"), exist_ok=True)
    r = SH.parse_gen_output({"name": "dup-skill", "description": "x", "content": "正文\n"})
    check("重名 dir 加后缀", r["dir"] == "dup-skill-2", r["dir"])
    meta, _b = SH.parse_skill_md(r["content"])
    check("重名后 frontmatter 跟着改", meta.get("name") == "dup-skill-2", meta)
    # 界面上给的是 name，保存时前端拿它当 dir，两边必须一致
    check("name 与 dir 一致（重名时）", r["name"] == r["dir"], r)

    # ---- 9. gen_user 提示词带上「别重名」的已有技能 ----
    u = SH.gen_user("要一个查错别字的技能", ["existing-a", "existing-b"])
    check("gen_user 含需求原文", "查错别字" in u, u[:120])
    check("gen_user 列出已有技能名", "existing-a" in u and "existing-b" in u, u[:400])

    # ---- 10. gen_schema 是个能用的 JSON Schema ----
    sc = SH.gen_schema()
    check("schema 有 properties", isinstance(sc.get("properties"), dict), sc)
    check("schema 要求 name/description/content",
          {"name", "description", "content"} <= set(sc["properties"]), list(sc["properties"]))

    # ---- 11. gen_system 明确禁止写脚本 ----
    gs = SH.gen_system()
    check("gen_system 要求 frontmatter", "frontmatter" in gs, gs[:200])
    check("gen_system 禁止脚本", "脚本" in gs and "Markdown" in gs, gs[:400])

    # ---- 12. 生成产出的内容真能装（端到端闭合）----
    r = SH.parse_gen_output({"name": "e2e-skill", "description": "端到端",
                             "content": "---\nname: e2e-skill\ndescription: 端到端\n---\n\n# 怎么做\n\n1. 做\n"})
    plan = SH.plan_content(r["dir"], r["content"])
    check("生成的草案能编成计划", plan.get("ok"), plan)
    out = SH.commit_plan(plan, "user")
    check("生成的草案能落盘", out.get("ok"), out)
    got = [s for s in SH.scan_local() if s["dir"] == "e2e-skill"]
    check("落盘后 scan_local 能扫到", bool(got), got)
    if got:
        check("扫到的 name 与 dir 一致", got[0]["name"] == got[0]["dir"], got[0])

    print("\n=== OK=%d FAIL=%d ===" % (len(OK), len(FAIL)))
    for f in FAIL:
        print(" FAIL:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
