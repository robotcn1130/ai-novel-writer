# -*- coding: utf-8 -*-
"""
章节诊断（v6.6 起，v6.7 扩为七层）：作者读出逻辑问题，但说不清在哪一层。

设计取舍：
  - **不猜层**。不是先让模型判断"这可能是哪层的问题"，而是把七层事实
    （世界/人物/关系/位置/知识边界/事件/正史）全部摊开，让它逐层比对、逐层排除。
    原因：作者说不清病根时，让模型猜层会把错误放大——猜错了后面全错。
  - **材料复用已有装配器**，不另写一套。`prose.build_world_facts_block`
    等函数已经被真实使用验证过，这里直接拿来用，只补正文侧缺的几层
    （关系网、位置与行踪认知、知识边界、正史账本）。
  - **只报能指出冲突的问题**。提示词里写死了这条纪律，避免模型为凑数
    编造"感觉不对"。

层序与层名的**唯一来源是 `prompts.DIAGNOSE_LAYERS`**（见下面 LAYER_ORDER）。
踩过的坑：这里曾自己写一份 5 层的表，而提示词已经改成 7 层——
注释里写着"唯一收口"，实际是两份，缺的两层在装配侧根本没给材料。

返回结构（给前端直接用）：
  {"issues": [...], "summary": ..., "suspected_root": ...,
   "materials": {"world": 条数, "characters": 条数, ...}}   # 让用户看到查了什么
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC_V6 = os.path.dirname(_HERE)
for _p in (_SRC_V6, os.path.join(_SRC_V6, "core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from engine import prompts as P                      # noqa: E402

LAYER_LABELS = {
    "world": "世界设定",
    "character": "人物设定",
    "relation": "人物关系",
    "location": "位置与认知",
    "knowledge": "知识边界",
    "event": "事件推理",
    "canon": "正史一致",
    "unknown": "未归层",
}
# 层序与层名**只有一个来源**：prompts.DIAGNOSE_LAYERS。
# 原先这里自己写了一份 5 层的表，而提示词/schema 已经改成 7 层——
# 结果就是注释里写着"唯一收口"、实际却有两份，缺的两层在引擎侧根本没装配。
# 现在从提示词那边派生，改层只需要改一处。
LAYER_ORDER = list(P.DIAGNOSE_LAYERS)

# ---- 材料预算 ----------------------------------------------------------
# 诊断是"比对事实"，不是"通读全书"。把三十万字的世界观整段塞进去，模型会
# 先被淹掉：预算被思维链吃掉，正文还没吐完就 finish=length，最后抢救出一个
# 残缺 JSON。所以这里给每层定硬上限，超出的走"摘要 + 折叠"。
# 实测（58k 字输入）栽过跟头，预算控制在 ~20k 字以内才稳。
MAX_EVENTS_FULL = 8        # 最多几条事件给全文（description/intent/result 全留）
MAX_EVENT_DESC = 240       # 事件正文/意图/结果各自的截断长度
MAX_CHARS_FULL = 10        # 人物层：最多几个角色给完整档案
MAX_CHAR_FIELD = 120       # 人物字段（性格/外貌…）的截断长度
MAX_GOALS_PER_CHAR = 3     # 每人最多几条目标
MAX_RELATIONS = 18         # 关系层最多几条
MAX_BELIEFS = 16           # 行踪认知最多几条
MAX_CHAPTER_TAIL = 6000    # 正文超过这个数才截；3000 字是常态，别切它

# 诊断的输出预算。**必须显式给**，不能让降级链自己翻倍：
# 诊断的输出是结构化结论（最多 8 条问题 × 几百字），6000 token 绰绰有余。
# 一旦放任翻倍，模型会拿多出来的预算去写"检查过程/复述材料"，
# 实测出现过输出 89,646 字节、finish=length 还停不下来的情况——
# 预算越大它写得越啰嗦，而正文永远排在最后，结果一个完整 JSON 都拿不到。
DIAGNOSE_BUDGET = 6000


def _clip(text, limit):
    """截断并显式标注，别让模型以为原文就这么短。"""
    s = str(text or "").strip()
    if len(s) <= limit:
        return s
    return s[:limit].rstrip() + "…（此处省略 %d 字）" % (len(s) - limit)


def _rel_mutual(db, novel_id):
    """关系方向标注：单向关系不能被画成对称，否则诊断会误判「两人都知情」。"""
    return db.group_relations(novel_id)


# ============================================================ 材料装配


def build_world_block(db, novel_id):
    """第 1 层：世界事实。复用正文侧的 facts 装配器，保证口径一致。"""
    from generate.prose import build_world_facts_block
    return build_world_facts_block(db, novel_id)


def build_characters_block(db, novel_id, names=None):
    """第 2 层：人物档案。names 为空时收全部在世角色（诊断场景要全量）。

    names 里的人（本章真正出场的）优先，给完整档案；其余只给一行身份，
    用来做"这个人根本不在场"判断——不需要他们的性格史。
    """
    rows = db.list_characters(novel_id)
    if not rows:
        return ""
    if names:
        want = set(names)
        head = [c for c in rows if c["name"] in want]
        tail = [c for c in rows if c["name"] not in want]
        rows = head + tail
    full, brief = rows[:MAX_CHARS_FULL], rows[MAX_CHARS_FULL:]
    L = []
    for c in full:
        head = "- %s" % c["name"]
        tags = []
        if c.get("role_tag"):
            tags.append(c["role_tag"])
        if c.get("rank"):
            tags.append("%s 级" % c["rank"])
        tags.append("状态：%s" % (c.get("status") or "alive"))
        if c.get("current_location"):
            tags.append("在 %s" % c["current_location"])
        head += "（%s）" % "、".join(tags)
        L.append(head)
        for key, label in (("personality", "性格"), ("speech_style", "说话方式"),
                           ("appearance", "外貌")):
            if c.get(key):
                L.append("    %s：%s" % (label, _clip(c[key], MAX_CHAR_FIELD)))
        goals = db.list_goals(character_id=c["id"], status="active") or []
        for g in goals[:MAX_GOALS_PER_CHAR]:
            tag = "长期" if g["goal_type"] == "long" else "短期"
            line = "    %s目标：%s" % (tag, _clip(g["content"], 80))
            if g.get("obstacle"):
                line += "（障碍：%s）" % _clip(g["obstacle"], 60)
            L.append(line)
        mem = c.get("memory") or []
        if mem:
            L.append("    他记得：" + "；".join(
                _clip(m.get("text", ""), 30) for m in mem[-2:]))
    if brief:
        L.append("")
        L.append("**其余角色**（本章未出场，只在需要判断「他怎么可能在场」时参考）：")
        for c in brief:
            L.append("  - %s（%s，在 %s）" % (
                c["name"], c.get("role_tag") or "配角",
                c.get("current_location") or "位置不明"))
    return "\n".join(L)


def build_relations_block(db, novel_id):
    """第 3 层：关系网。走 db.group_relations 以保证方向标注一致
    （双向 ↔ / 单向 →，单向时被指向方不得知情）。"""
    rels = db.list_relations(novel_id)
    if not rels:
        return ""
    labels = {"family": "亲属", "friend": "朋友", "enemy": "敌对", "lover": "恋慕",
              "colleague": "同僚", "mentor": "师徒", "rival": "竞争", "other": "其他"}
    L = []
    for g in db.group_relations(novel_id):
        r = g["record"]
        arrow = "↔" if g["mutual"] else "→"
        line = "  - %s %s %s：%s（强度%s）" % (
            g["a"], arrow, g["b"],
            labels.get(g["relation_type"], g["relation_type"]), r["intensity"])
        if r.get("description"):
            line += "——" + _clip(r["description"], 70)
        if not g["mutual"]:
            line += "［单向：%s 尚不知情］" % g["b"]
        st = r.get("status") or "active"
        if st != "active":
            line += "（关系已%s）" % {"broken": "破裂",
                                      "evolved": "变化"}.get(st, st)
        L.append(line)
        if len(L) >= MAX_RELATIONS:
            L.append("  …（关系较多，只列前 %d 条）" % MAX_RELATIONS)
            break
    return "\n".join(L)


def build_locations_block(db, novel_id):
    """第 4 层：位置与行踪认知。

    这是诊断里最容易被忽略、却最容易出逻辑错的一层：
    事实（他在哪）与认知（谁知道他在哪）是两码事。
    """
    L = []
    chars = db.list_characters(novel_id)
    fact = []
    for c in chars:
        if c.get("current_location"):
            fact.append("  - %s 在 %s" % (c["name"], c["current_location"]))
    if fact:
        L.append("**事实位置**（上帝视角）：")
        L.extend(fact)

    try:
        beliefs = db.list_location_beliefs(novel_id)
    except Exception:                                  # noqa: BLE001
        beliefs = []
    if beliefs:
        L.append("**行踪认知**（某人以为某人在哪——做决定时以此为准）：")
        for b in beliefs[:MAX_BELIEFS]:
            if b.get("stale"):
                continue
            L.append("  - %s 以为 %s 在 %s%s" % (
                b.get("observer_name") or "?", b.get("character_name") or "?",
                b.get("believed_location") or "某处",
                "（已过期）" if b.get("stale") else ""))
    if not L:
        return ""
    L.append("注意：**不知道对方行踪的人，不该直接找到对方**。")
    return "\n".join(L)


def build_events_block(db, novel_id, chapter):
    """第 6 层：本章该写的事件记录（正文取材于此）。

    取"最重的几条给全文、其余只给标题"：诊断要查的是**因果链**，
    而链条上真正承载因果的通常只有三五个转折点；把二十条都摊开写全文，
    只会在模型开始比对之前先把上下文吃光。
    """
    ids = chapter.get("source_event_ids") or []
    rows = []
    if ids:
        all_ev = {e["id"]: e for e in db.list_events(novel_id)}
        rows = [all_ev[i] for i in ids if i in all_ev]
    if not rows:
        # 没记来源就按章节号兜底
        num = chapter.get("chapter_number")
        rows = [e for e in db.list_events(novel_id) if e.get("chapter_ref") == num]
    if not rows:
        return ""
    # 重的排前面（importance 9/10 是主线转折，1/2 是过场）
    order = sorted(range(len(rows)),
                   key=lambda i: -int(rows[i].get("importance") or 5))
    L = []
    for n, i in enumerate(order, 1):
        ev = rows[i]
        if n <= MAX_EVENTS_FULL:
            L.append("%d. 【%s】%s（重要度 %s）" % (
                n, ev.get("event_type") or "plot", ev.get("title") or "",
                ev.get("importance") or 5))
            if ev.get("world_time"):
                L.append("   时间：%s" % ev["world_time"])
            if ev.get("description"):
                L.append("   发生了什么：%s" % _clip(ev["description"],
                                                    MAX_EVENT_DESC))
            if ev.get("intent"):
                L.append("   意图（这是「想做」，不是「做到」）：%s"
                         % _clip(ev["intent"], MAX_EVENT_DESC))
            if ev.get("result"):
                L.append("   结果：%s" % _clip(ev["result"], MAX_EVENT_DESC))
            if ev.get("involved_characters"):
                L.append("   涉及人物：%s" % "、".join(ev["involved_characters"]))
        else:
            L.append("%d. 【%s】%s（重要度 %s，略）" % (
                n, ev.get("event_type") or "plot", ev.get("title") or "",
                ev.get("importance") or 5))
    if len(rows) > MAX_EVENTS_FULL:
        L.append("（前 %d 条按重要度排序给了全文，其余只留标题。"
                 "若怀疑问题出在后面那几条，可直接说事件名。）" % MAX_EVENTS_FULL)
    return "\n".join(L)


# ---- 第 5 层 / 第 7 层（v6.7 加的两层，v6.8 才真正接上装配）--------------
#
# 这两层是 v6.7 加的，但当时只改了提示词与 schema，**装配侧漏了**。
# 那不是"少报两层"那么轻，而是更糟：`diagnose_user()` 在不传这两个 block 时
# 会把它们渲染成「（暂无认知记录）」「（暂无正史记录）」——**库里明明有数据，
# 也这么告诉模型**。模型于是会得出"没有正史约束"的结论，正史冲突就此漏过，
# 而且报告看上去一切正常。这正是本项目最怕的"静默失效"。

MAX_KNOWLEDGE = 24         # 认知层最多几条
MAX_KNOW_CONTENT = 90      # 认知内容截断
MAX_CANON = 30             # 正史层最多几条

_KNOW_LABEL = {
    "know": "知道", "believe": "相信", "suspect": "怀疑",
    "assume": "自以为", "memory": "回忆", "dream": "梦中所见",
}


def build_knowledge_block(db, novel_id, names=None):
    """第 5 层：角色认知（一人一版本）。

    这一层的价值不在"把认知列出来"，而在**与事实对照**：
    某人"以为"的和正史不一样是正常的（人会被骗）；
    但正文若把他的误解**当成事实**叙述，就是错的。
    所以 `is_false`（已知与事实不符）的条目一定要标出来——
    那正是"叙述者跟着一起错"的高发位置。

    本章出场的人优先给。认知是"一人一个版本"，全库的认知塞进来会立刻淹掉
    真正相关的几条，而诊断要的正是"这一章里这几个人各自以为事情是什么样"。
    """
    try:
        rows = db.list_knowledge(novel_id, limit=MAX_KNOWLEDGE * 4) or []
    except Exception:                                       # noqa: BLE001
        return ""
    if not rows:
        return ""
    cmap = {}
    try:
        for c in (db.list_characters(novel_id) or []):
            cmap[c.get("id")] = c.get("name") or ("#%s" % c.get("id"))
    except Exception:                                       # noqa: BLE001
        pass

    want = {n for n in (names or []) if n}
    onstage, others = [], []
    for r in rows:
        who = cmap.get(r.get("character_id")) or ("#%s" % r.get("character_id"))
        kt = _KNOW_LABEL.get((r.get("know_type") or "know"), "知道")
        about = (r.get("about_text") or r.get("about_type") or "").strip()
        content = _clip(r.get("content") or "", MAX_KNOW_CONTENT)
        if not content:
            continue
        line = "- %s｜%s" % (who, kt)
        if about:
            line += "（关于 %s）" % about
        line += "｜%s" % content
        if r.get("is_false"):
            line += "　⚠ 这条与正史不符（他信的是假的）"
        src = []
        if r.get("source"):
            src.append(str(r["source"]))
        if r.get("learned_chapter"):
            src.append("第 %s 章" % r["learned_chapter"])
        if src:
            line += "（%s）" % "·".join(src)
        (onstage if (not want or who in want) else others).append(line)

    L = onstage[:MAX_KNOWLEDGE]
    if len(L) < MAX_KNOWLEDGE and others:
        L += others[:MAX_KNOWLEDGE - len(L)]
    if want and not onstage:
        L.insert(0, "（本章出场角色暂无认知记录，以下是其他人物的）")
    if len(rows) > len(L):
        L.append("（共 %d 条认知，此处按「本章出场优先」给了 %d 条。"
                 "若怀疑问题出在别的角色身上，可直接点名。）" % (len(rows), len(L)))
    return "\n".join(L)


def build_canon_block(db, novel_id):
    """第 7 层：正史账本（已写定的事实）。

    只给 `canonical` / `derived`——`list_facts(active_only=True)` 已经就是这个口径。
    `provisional`（还没确认的）**不能拿来判冲突**：拿它比对会冤枉正文，
    而误报会让作者训练成"见到红色徽标就点忽略"，比漏报更伤。

    "正文补充正史没提的细节"是正常的（那是丰富），只有**不能同时为真**才算错。
    这条纪律写在提示词里，这里只负责把事实摆清楚。
    """
    try:
        from engine import facts as F
        rows = F.list_facts(db, novel_id, active_only=True, limit=MAX_CANON)
    except Exception:                                       # noqa: BLE001
        return ""
    if not rows:
        return ""
    L = []
    for f in rows:
        subj = (f.get("subject_name") or "").strip() or "(世界)"
        pred = (f.get("predicate") or "").strip()
        val = (f.get("object_text") or "").strip()
        if not pred and not val:
            continue
        line = "- %s｜%s" % (subj, pred)
        if val:
            line += "｜%s" % val
        if f.get("fact_status") == "derived":
            line += "（推得）"
        src = []
        if f.get("source_chapter"):
            src.append("第 %s 章" % f["source_chapter"])
        if f.get("source_kind"):
            src.append(str(f["source_kind"]))
        if src:
            line += "（%s）" % "·".join(src)
        L.append(line)
    if not L:
        return ""
    if len(rows) >= MAX_CANON:
        L.append("（正史共 %d 条以上，此处按最新给了 %d 条。）"
                 % (len(rows), MAX_CANON))
    return "\n".join(L)


def _chapter_text_for_diagnose(text):
    """正文给全文；只有**真的很长**时才截断，且截头留尾。

    为什么要"截头留尾"：逻辑断裂几乎总是发生在最新写的那几段，而开头
    已经被前几章和读者一起校验过了。但要小心阈值——单章 3000 字上下是
    常态（一章 ≈ 750 字 × 4 个事件），**别把常态当超长**，那会把模型
    本来该看到的上下文切掉一半，反而更容易误诊。
    """
    t = (text or "").strip()
    if len(t) <= MAX_CHAPTER_TAIL:
        return t, False
    return ("（本章正文共 %d 字，此处只给**最后 %d 字**——前面的部分已由前文"
            "校验过。若问题出在更早的段落，请在补充说明里点出来。）\n\n%s"
            % (len(t), MAX_CHAPTER_TAIL, t[-MAX_CHAPTER_TAIL:])), True


def assemble(db, novel_id, chapter, complaint=""):
    """装配七层材料。返回 (user_prompt, 材料条数统计, blocks)。"""
    from generate.prose import build_novel_block
    novel = db.get_novel(novel_id) or {}
    full_text = (chapter.get("content") or "").strip()
    text, trimmed = _chapter_text_for_diagnose(full_text)

    names = []
    for e in (db.list_events(novel_id) or []):
        if e.get("chapter_ref") == chapter.get("chapter_number"):
            if e.get("actor"):
                names.append(e["actor"])
            names.extend(e.get("involved_characters") or [])

    blocks = {
        "world": build_world_block(db, novel_id),
        "characters": build_characters_block(db, novel_id, names or None),
        "relations": build_relations_block(db, novel_id),
        "locations": build_locations_block(db, novel_id),
        # v6.8：这两层是 v6.7 加进提示词、却漏在装配里的。不补上的话，
        # 模型收到的材料里这两层恒为「（暂无…记录）」——库里明明有数据。
        "knowledge": build_knowledge_block(db, novel_id, names or None),
        "events": build_events_block(db, novel_id, chapter),
        "canon": build_canon_block(db, novel_id),
    }
    user = P.diagnose_user(
        novel_block=build_novel_block(novel),
        world_block=blocks["world"],
        characters_block=blocks["characters"],
        relations_block=blocks["relations"],
        locations_block=blocks["locations"],
        knowledge_block=blocks["knowledge"],
        events_block=blocks["events"],
        canon_block=blocks["canon"],
        chapter_text=text,
        complaint=complaint)
    stats = {k: (v.count("\n") + 1 if v else 0) for k, v in blocks.items()}
    stats["chapter_chars"] = len(full_text)
    stats["chapter_sent_chars"] = len(text)
    stats["chapter_trimmed"] = trimmed
    stats["prompt_chars"] = len(user)
    return user, stats, blocks


# ============================================================ 诊断

def diagnose(db, router, novel_id, chapter_number, complaint="", emit=None):
    """跑一次七层诊断。emit 用于播报进度。"""
    def say(msg, pct=None):
        if emit:
            emit(msg, pct)

    chapter = db.get_chapter(novel_id, int(chapter_number))
    if not chapter or not (chapter.get("content") or "").strip():
        raise ValueError("这一章还没有正文，没什么可诊断的。")

    say("收集七层事实（世界 / 人物 / 关系 / 位置 / 知识边界 / 事件 / 正史）…", 8)
    user, stats, blocks = assemble(db, novel_id, chapter, complaint)

    say("材料：世界 %d 行、人物 %d 行、关系 %d 条、位置 %d 行、事件 %d 条、"
        "正文 %d 字" % (stats["world"], stats["characters"], stats["relations"],
                       stats["locations"], stats["events"], stats["chapter_chars"]), 20)
    if stats.get("chapter_trimmed"):
        say("（正文较长，只送了后 %d 字——逻辑断裂基本都出在最新段落）"
            % stats["chapter_sent_chars"], 22)
    say("提示词共 %d 字，模型正在逐层比对"
        "（world → character → relation → location → event）…"
        % stats.get("prompt_chars", len(user)), 30)

    out = router.run_json("diagnose", system=P.DIAGNOSE_SYSTEM, user=user,
                          schema=P.DIAGNOSE_SCHEMA, temperature=0.3,
                          max_tokens=DIAGNOSE_BUDGET, cap=DIAGNOSE_BUDGET,
                          with_meta=True)
    parsed, raw, mode, salvaged = out
    if not isinstance(parsed, dict):
        raise ValueError("模型没有返回可解析的诊断结果。")

    if salvaged:
        say("⚠ 这次结果是截断后抢救出来的，字段可能不全。", 90)

    issues = parsed.get("issues") or []
    if not isinstance(issues, list):
        issues = []
    # 收口：层与严重度都用白名单，别让自由文本漏到前端
    clean = []
    for it in issues:
        if not isinstance(it, dict):
            continue
        layer = str(it.get("layer") or "unknown").strip().lower()
        level = str(it.get("level") or "warning").strip().lower()
        clean.append({
            "layer": layer if layer in LAYER_LABELS else "unknown",
            "layer_label": LAYER_LABELS.get(layer, LAYER_LABELS["unknown"]),
            "level": level if level in ("error", "warning") else "warning",
            "problem": str(it.get("problem") or "").strip(),
            "quote": str(it.get("quote") or "").strip(),
            "conflicts_with": str(it.get("conflicts_with") or "").strip(),
            "suggestion": str(it.get("suggestion") or "").strip(),
            "severity_note": str(it.get("severity_note") or "").strip(),
        })
    clean = [i for i in clean if i["problem"]]
    # 按层顺序排，前端好读
    clean.sort(key=lambda i: LAYER_ORDER.index(i["layer"])
               if i["layer"] in LAYER_ORDER else 99)

    root = str(parsed.get("suspected_root") or "unknown").strip().lower()
    say("诊断完成：%d 条问题。" % len(clean), 100)
    return {
        "ok": True,
        "chapter_number": int(chapter_number),
        "title": chapter.get("title") or "",
        "issues": clean,
        "error_count": sum(1 for i in clean if i["level"] == "error"),
        "warning_count": sum(1 for i in clean if i["level"] == "warning"),
        "summary": str(parsed.get("summary") or "").strip(),
        "verdict": str(parsed.get("verdict") or "").strip(),
        "suspected_root": root if root in LAYER_LABELS else "unknown",
        "suspected_root_label": LAYER_LABELS.get(root, LAYER_LABELS["unknown"]),
        "layers_checked": parsed.get("layers_checked") or [],
        "materials": stats,          # 让用户看到"到底查了什么"，而不是黑箱
        "salvaged": bool(salvaged),
    }


# ============================================================ 定向改稿

def revise(db, router, novel_id, chapter_number, issues=None, direction="",
           handle=None, emit=None):
    """按审查意见 / 作者方向改稿。**不落库**——只返回改后正文供预览。"""
    def say(msg, pct=None):
        if emit:
            emit(msg, pct)

    chapter = db.get_chapter(novel_id, int(chapter_number))
    if not chapter or not (chapter.get("content") or "").strip():
        raise ValueError("这一章还没有正文。")
    text = chapter["content"]

    if not issues and not direction:
        raise ValueError("没有给改稿依据：要么采纳审查意见，要么写一个修改方向。")

    say("装配改稿上下文（%d 条问题%s）…"
        % (len(issues or []), "、含作者方向" if direction else ""), 12)
    user = P.revise_user(text, issues=issues, direction=direction, handle=handle)

    words = len(text)
    # 改稿要吐**整章正文**，所以预算按字数走；但也要有下限兜住短章。
    # 上限不设 16384——那是给"重写"留的口子，这里只改几段，8000 足够，
    # 且能防住模型趁机把整章扩写一遍（那就不叫"只改有问题的段落"了）。
    budget = min(12000, max(2048, words * 2))
    say("模型正在改稿（只动有问题的段落）…", 25)
    out = router.run_json("prose_gen", system=P.REVISE_SYSTEM, user=user,
                          schema=P.REVISE_SCHEMA, temperature=0.6,
                          max_tokens=budget, cap=budget, with_meta=True)
    parsed, raw, mode, salvaged = out
    if not isinstance(parsed, dict):
        raise ValueError("模型没有返回可解析的改稿结果。")
    new_text = str(parsed.get("content") or "").strip()
    if not new_text:
        raise ValueError("模型返回了空正文，没有改动可应用。")

    changes = []
    for ch in (parsed.get("changes") or []):
        if not isinstance(ch, dict):
            continue
        changes.append({
            "before": str(ch.get("before") or "").strip(),
            "after": str(ch.get("after") or "").strip(),
            "why": str(ch.get("why") or "").strip(),
        })

    say("改稿完成：%d 处改动，%d 字 → %d 字。"
        % (len(changes), words, len(new_text)), 100)
    return {
        "ok": True,
        "chapter_number": int(chapter_number),
        "content": new_text,
        "original": text,
        "changes": changes,
        "note": str(parsed.get("note") or "").strip(),
        "word_count_before": words,
        "word_count_after": len(new_text),
        "salvaged": bool(salvaged),
    }
