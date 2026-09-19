# -*- coding: utf-8 -*-
"""世界推演提示词的关键约束（防静默回退）。

为什么单独测提示词：事件卡"看着难受"不是渲染问题，是**提示词没说清楚
description 该写成什么样** —— 模型就按"写一段场景"来写，把「做了什么 /
想要什么 / 结果 / 悬置线索」全揉进一段两百多字的流水里。
这类约束一旦在后续改提示词时被顺手删掉，没有任何测试会红，
问题要到下次推演才暴露。所以在这里钉住。

只读断言，不调模型、不写库。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src_v6"))
sys.path.insert(0, os.path.join(ROOT, "src_v6", "core"))

from engine import prompts as P                                    # noqa: E402

OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    if not cond:
        print("  FAIL:", name, extra)


def main():
    sysmsg = P.WORLD_SIM_SYSTEM
    schema = P.WORLD_SIM_SCHEMA
    ev = schema["properties"]["events"]["items"]["properties"]

    # ---------- 1. 事件卡写法：五条硬约束必须在 ----------
    check("有「事件卡怎么写」小节", "事件卡怎么写" in sysmsg)
    check("限定一个事件=一次不可再分的行动",
          "一次不可再分的行动" in sysmsg, sysmsg[-200:])
    check("要求拆卡而不是把整场戏压进一张",
          "不要" in sysmsg and "压进一张卡" in sysmsg)
    check("限定单卡单视角", "一张卡只写一个视角" in sysmsg)
    check("禁止解释世界机制", "不解释世界的运转机制" in sysmsg)
    check("description 给了字数上限", "80 字以内" in sysmsg)
    check("禁止每张卡重新铺场", "别每张卡都重新铺一遍场" in sysmsg)

    # ---------- 2. 「绝对不要写」清单里也有机制解说 ----------
    block = sysmsg.split("绝对不要写：", 1)[-1].split("术语纪律", 1)[0]
    check("禁写清单含机制解说", "讲解设定怎么运转" in block, block[:200])
    check("禁写清单仍含主题总结", "这一切意味着" in block)

    # ---------- 3. schema 里把字段的分工写清楚 ----------
    check("description 有 doc", bool(ev["description"].get("description")))
    dd = ev["description"]["description"]
    check("description doc 说明不复述 title", "不要复述 title" in dd, dd)
    check("description doc 说明单视角", "一个视角" in dd, dd)
    ml = ev["description"].get("maxLength")
    check("description 有 maxLength", isinstance(ml, int) and ml > 0, ml)
    check("maxLength 不宽到形同虚设", isinstance(ml, int) and ml <= 200, ml)

    for f, kw in (("title", "钩子"), ("action", "做了什么"),
                  ("intent", "想达成什么"), ("result", "直接后果")):
        d = (ev.get(f) or {}).get("description") or ""
        check("schema「%s」有说明" % f, kw in d, d[:80])

    check("result 说明里点明「另一方的反应写这里」",
          "另一方的反应" in (ev["result"].get("description") or ""),
          ev["result"].get("description"))

    # ---------- 4. 原有铁律没被顺手删掉 ----------
    for kw in ("信息边界", "tick_delta", "不要为了让情节好看而硬凑巧合",
               "术语纪律"):
        check("原有约束仍在：%s" % kw, kw in sysmsg)

    # ---------- 5. 用户消息装配没被带坏 ----------
    u = P.world_sim_user("【世界简报】", "【约束】", "【焦点】")
    check("world_sim_user 三参数仍可用",
          "【世界简报】" in u and "【约束】" in u and "【焦点】" in u, u[:80])
    check("硬约束在 system 里，不在 user 里",
          "事件卡怎么写" not in u and "事件卡怎么写" in sysmsg)

    # ---------- 6. v6.9 岔路是"碰撞的产物"，不是节拍器 ----------
    # 旧文案"如果出现……就立刻停下"把模型逼成了"写 1 条事件就交 1 个岔路"，
    # 实测每次 expand 只出 1 条事件、岔路永远挂链尾。这几条防止它被改回去。
    check("不再有「立刻停下」的指令",
          "立刻停下" not in sysmsg, sysmsg[:0])
    check("岔路明确说成碰撞的产物",
          "碰撞的产物" in sysmsg and "不是节拍器" in sysmsg)
    check("允许一段推演没有岔路",
          "不要每段都冒岔路" in sysmsg)
    check("要求先有事件后有岔路",
          "先有事件，后有岔路" in sysmsg)

    dn = schema["properties"]["decision_needed"]
    check("decision_needed 一次最多 1 个", dn.get("maxItems") == 1, dn.get("maxItems"))
    check("decision_needed 说明里点明宁缺勿滥",
          "宁缺勿滥" in (dn.get("description") or ""))
    check("岔路必填 reason",
          "reason" in dn["items"]["required"], dn["items"]["required"])
    check("岔路必填 stakes",
          "stakes" in dn["items"]["required"], dn["items"]["required"])
    check("岔路有 forks_from（哪条事件逼出来的）",
          "forks_from" in dn["items"]["properties"])

    # ---------- 7. v6.9 这是一个"世界"，不是固定场景的情景剧 ----------
    check("要求写别人的线（主角之外）",
          'event_type="world"' in sysmsg)
    check("要求世界至少动一个状态量",
          "state_changes.world" in sysmsg)
    check("点明「人一个不增不减是不正常的」",
          "一个不增不减，是不正常的" in sysmsg)

    nc = schema["properties"]["new_characters"]
    ce = schema["properties"]["character_exits"]
    check("schema 有 new_characters 通道", bool(nc))
    check("schema 有 character_exits 通道", bool(ce))
    check("新角色必填 goal（防止工具人）",
          "goal" in nc["items"]["required"], nc["items"]["required"])
    check("新角色 goal 的说明点明「他自己想要什么」",
          "他自己想要什么" in nc["items"]["properties"]["goal"]["description"])
    check("退场状态取值受限",
          set(ce["items"]["properties"]["status"]["enum"])
          == {"dead", "missing", "retired"},
          ce["items"]["properties"]["status"]["enum"])

    # 关系：新羁绊必须记，空着是例外
    check("relation_changes 点明新羁绊必须写",
          "只要有一段新关系产生" in sysmsg)

    print("")
    print("=== OK=%d FAIL=%d ===" % (len(OK), len(FAIL)))
    for f in FAIL:
        print(" FAIL:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
