# -*- coding: utf-8 -*-
"""
内容生成层：把结构化事件流写成文学正文。

关键设计（方案 7）：
  - 生成器拿到的输入里包含"角色内心的动机"——这是世界模拟范式
    相比大纲驱动的最大优势：写手知道每个人为什么这么做
  - 章节形态由事件性质自然决定，**不强制每章都有钩子**
  - 收尾方式是"世界事件收尾"而不是"主角想通了"
  - 只推演"发生了什么"，不总结"意味着什么"
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC_V6 = os.path.dirname(_HERE)
for _p in (_SRC_V6, os.path.join(_SRC_V6, "core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _imp(value, default=3):
    """宽容取"分量"：事件可能来自库（int），也可能来自沙盘 payload（模型自由文本）。"""
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def _s(value, default=""):
    """宽松转字符串：None → default，其余 strip。模型返回值一律经它。"""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


# ============================================================ 提示词

PROSE_SYSTEM = """你是一个写网文的小说作者。你要把一组已经发生的事实写成正文。

你拿到的不是情节大纲，而是**事件记录**——谁在什么时间做了什么、为什么、结果如何。
你的任务是让读者通过场景与行动感受到这些事，而不是被通知。

【第一章 · 最重要的一条：详略分级】
**读者要看的是矛盾推进，不是一个人一天的行踪记录。**
你写的是小说，不是监控录像。主角起床、出门、坐地铁、吃饭、洗碗、上楼——
这些如果没有矛盾发生，**一句都不要写**，或者一句带过。

你收到的每条事件后面标了【本章戏份】，照它分配笔墨：
- **主戏**：本章的高光。要写透——写进具体场景、具体动作、具体对话，
  可以给到 800–1500 字。全文的力气主要花在这里。
- **过场**：只用来串场与交代位移。**一到两句，不超过 60 字**。
  绝对不许为过场事件写环境、写动作分解、写心理过程。
- **背景**：只在本章需要时提一句，不单独成段。

【第二章 · 拍点纪律】
每一段都必须**至少推进以下之一**：矛盾、关系、信息差、情绪转折。
只是"又过了一段时间""他又做了个动作"的段落，属于注水，**删掉**。

自检：随手抽掉任意一段，如果后面的剧情照样成立，这段就是水，删掉。

【第三章 · 对白配额】
**本章对白至少要占正文的 35%。**
理由很实在：对白压缩篇幅、自带张力、是网文的骨架。大段动作分解与环境描写
是最容易拖垮节奏的东西。
规则：
- 能让人物说出来的，就不要用叙述交代。
- 对白要带信息差——误解、隐瞒、答非所问、突然转话题，至少出现两次。
- 禁止纯对白：每段对白要带上说话人的动作或神态，但**只带一个**，别铺垫三句。

【第四章 · 注水黑名单（严禁）】
以下写法一律禁止，见到就删：
1. **动作分解**：把一个动作拆成三句写。
   反例：「他伸手拿起杯子。手指扣住杯壁。把杯子举到嘴边。」
   正例：「他喝了口水。」
2. **等价拍点连拍**：连续三段都只是"时间在走"，矛盾没动。这是流水账的头号特征。
3. **可被脑补的基础流程**：进门脱鞋、打开电脑、手机解锁、走到楼下、掏出钥匙——
   读者自己能补全的，别写。
4. **环境清单**：一段里连写三个以上环境物件（窗帘、台灯、闹钟、水杯……），
   除非其中某件物件正在起作用**且**推动当下矛盾。
5. **重复情绪**：同一个情绪反复拍。写过了就够了。
6. **无冲突的位移**：从 A 到 B 的路上没发生任何事，就不写这条路。

【第五章 · 基本写法】
1. 用场景、动作、对话、感官细节呈现。不要写"他意识到""他明白过来"这类告知式句子。
2. 凡是事件记录里写了"意图"的，用角色的行为让读者自己推断出这个意图，不要直接讲。
3. 不要把事件的"意义"写出来。不要总结主题。不要在结尾点题。
4. 保持视角一致。如果指定了视角人物，只写他能感知到的东西。
5. 时间不要跳。场景之间如果需要跨度，用一句过渡带过，不要逐日记录。
6. 不要每章都收束干净。有些事就该悬着。
7. 长短句交替，不要连续 5 句以上都是短句；段落长度也要有变化。

绝对禁止：
- "就在这时" "与此同时" "下一刻" "仿佛" "似乎" "不由得" "不禁" 这类套路连接词
- 结尾用"他终于明白了……" "这一切意味着……" 收束
- 让人物说出自己的性格设定
- 为了让本章完整而硬塞一个高潮
- 专业术语、行业行话堆砌（写角色做什么、看到什么、结果如何，
  不要写"他完成了分时预演""量价出现背离"这类名词点题式表述）。
  作者若在「作者的写作癖好」里对措辞另有规定，以那条规定为准。

【重要】你只写"发生了什么"，不写"这说明了什么"。
世界还在继续，这一章不是终点。"""


def prose_user(novel_block, events_block, characters_block, voice_block="",
               words=3000, shape="scene", pov="", tail="", extra="",
               facts_block="", relations_block="", canon_block="",
               knowledge_block=""):
    L = ["## 作品基调\n%s" % novel_block]
    if pov:
        L.append("\n## 视角\n本章视角人物：%s。只写他能感知到的。" % pov)
    # v6.7：正史账本（facts）与 POV 知识块。
    #   canon_block      —— 世界真相（不得与之矛盾）
    #   knowledge_block  —— 视角人物**以为**的事（他据此行动，哪怕以为错了）
    # 两者的区别就是这套改造的核心：**"世界是什么样"和"他以为世界是什么样"
    # 是两件事**。只给前者，正文里人人都是上帝视角；只给后者，正文会写飞。
    if canon_block:
        L.append("\n## 正史事实（已经写定的世界真相，本章**不得与之矛盾**）\n%s"
                 % canon_block)
    if knowledge_block:
        L.append("\n## 视角人物此刻知道 / 相信的事\n%s" % knowledge_block)
    if facts_block:
        L.append("\n## 已经写定的世界事实（本章不得与之矛盾）\n%s" % facts_block)
    L.append("\n## 本次要写的事件（这些是已经发生的事实，必须全部写到）\n%s"
             % events_block)
    if characters_block:
        L.append("\n## 相关角色（他们的内心动机是你最该利用的东西）\n%s"
                 % characters_block)
    if relations_block:
        L.append("\n## 人物关系（决定了他们怎么对待彼此）\n%s" % relations_block)
    if voice_block:
        L.append("\n## 作者的写作癖好（务必遵守）\n%s" % voice_block)
    if tail:
        L.append("\n## 上一章的结尾原文（本章需自然承接，不要重复它的内容）\n%s"
                 % tail)
    L.append("\n## 本章形态\n%s" % _shape_desc(shape))
    if extra:
        L.append("\n## 额外要求\n%s" % extra)
    L.append("\n## 篇幅与笔墨分配（务必照此执行）\n"
             "总篇幅约 %d 字。**但不要平均用力**：\n"
             "- 标了【主戏】的事件：写透，允许 800–1500 字/件，本章的力气主要花在这里。\n"
             "- 标了【过场】的事件：一到两句、不超过 60 字/件，只交代位移与结果，"
             "不许写环境、不许分解动作。\n"
             "- 标了【背景】的事件：需要时带一句即可。\n"
             "写完请自查：如果删掉某一段，后面的剧情照样成立——那就删掉它。" % words)
    # v6.7：自检从"指标达标"改成"目标导向"。
    # 老版本写的是"对白占全文比例是否达到 35%？不够就加对话、砍描写"——
    # 这是**为指标服务**，真机表现是硬塞对白、静场戏被写坏。
    # 那部分的收益（A/B 实测对白 22%→53%）来自"对白承担节奏"这个观念，
    # 不是来自那个百分比本身。所以保留观念、去掉考核。
    L.append("\n## 交付前自检（必须逐条对照）\n"
             "1. 这一章的信息密度够吗？有没有把一个动作拆成三句写？\n"
             "2. 有没有连写三段只推进时间、没有任何矛盾发生？\n"
             "3. 【主戏】是不是本章最长的部分？过场有没有超过 60 字？\n"
             "4. 视角人物有没有说出他**不可能知道**的事？（他只知道自己那段"
             "「知道/相信」里列的东西）\n"
             "注：对白是节奏的骨架，尽量让它承担主要篇幅；但**不要为了凑比例硬塞"
             "对白**——该静场的地方就静场。")
    L.append("\n请开始写正文。直接输出正文，不要写标题，不要写任何说明。")
    return "\n".join(L)


SHAPE_DESC = {
    "scene": "单场景深写。把镜头对准一个地点一段时间，写透。",
    "montage": "蒙太奇。多个短场景并置，用跳跃呈现时间的推移或局面的变化。"
               "场景之间不要过渡句，直接切。",
    "interlude": "间章。写一个次要视角或一段别处的动静，"
                 "与主线形成对照，不必推进主要矛盾。",
    "aftermath": "余波。写一件大事之后各人的反应与消化。"
                 "重点是「之后」，不是「事件本身」。",
    "ensemble": "群像。多个角色各有镜头，呈现同一局面在不同人眼里的样子。",
    "transition": "过渡。承接前后，交代必要的位移与状态变化，篇幅可短。",
}


def _shape_desc(shape):
    return SHAPE_DESC.get(shape, SHAPE_DESC["scene"])


# 根据事件特征推断章节形态（方案 7.3）
def infer_shape(events):
    """由事件性质推断章节形态，而不是预先规定。"""
    if not events:
        return "scene"
    types = [e.get("event_type") for e in events]
    count = len(events)
    actors = set()
    for e in events:
        actors.add(e.get("actor") or "")
        for n in (e.get("involved_characters") or []):
            actors.add(n)
    actors.discard("")

    if count >= 5 and len(actors) >= 4:
        return "ensemble"
    if count >= 4 and any(e.get("is_gradual") for e in events):
        return "montage"
    if types and all(t in ("reveal", "character") for t in types) and count <= 2:
        return "scene"
    if any(t == "transition" for t in types) and count <= 2:
        return "transition"
    if count >= 3 and all(_imp(e.get("importance")) <= 2 for e in events):
        return "aftermath"
    return "scene"


def infer_ending_mode(events):
    """推断章节收尾方式。默认"以世界事件收尾"，而不是"主角想通了"。"""
    if not events:
        return "world_event"
    last = events[-1]
    if last.get("open_threads"):
        return "open_thread"
    if last.get("event_type") == "reveal":
        return "revelation"
    if last.get("event_type") == "conflict":
        return "conflict_peak"
    return "world_event"


# ============================================================ 分批（一章装几个事件）

# 一件事要写开，至少需要这么多字（含动作、对白、感官细节）。
# 低于这个密度，事件就只能被"通知"，写成流水账。
WORDS_PER_EVENT = 750
MIN_EVENTS_PER_CHAPTER = 2
MAX_EVENTS_PER_CHAPTER = 8


def drop_meta_events(events):
    """剔除元层事件（`canon_revision`）与背景事件。

    元层事件 = 作者对设定的操作，**不是剧情**。它必须被每一处"装配给模型的
    事件流"过滤掉：正文装配、推演简报、【刚发生的事】。
    约束 3 的原话：漏掉过滤，第 20 章会写出「作者修正了设定」这种正文。

    背景事件（`is_background=1`）是 `world.pulse()` 产出的世界动向，
    它进事件流（供推演看见），但**不该被当成一章的取材**。
    """
    try:
        from core.database import META_EVENT_TYPES
    except Exception:                       # noqa: BLE001
        META_EVENT_TYPES = ("canon_revision",)
    out = []
    for e in (events or []):
        if not isinstance(e, dict):
            continue
        if (e.get("event_type") or "") in META_EVENT_TYPES:
            continue
        if _imp(e.get("is_background") or 0, 0):
            continue
        out.append(e)
    return out


def events_per_chapter(words):
    """按目标字数推荐"一章写几个事件"。

    这是被真实数据教育出来的：落定 20 个事件后一次性写成第 1 章，
    结果 3860 字写了 20 件事，每件事不到 200 字——全成了梗概。

    v6.7：返回值从"硬下限"降级为**默认建议**。
    `MIN_EVENTS_PER_CHAPTER` 不再强制兜底——有些章节本来就只有 1 个事件
    （一个长场景就能撑一章），硬凑到 2 个只会写出注水段落。
    真正的前端入口允许用户指定 `limit`/`all`。
    """
    w = int(words or 3000)
    per = int(round(w / float(WORDS_PER_EVENT)))
    if per < 1:
        per = 1
    return min(MAX_EVENTS_PER_CHAPTER, per)


def plan_chapters(n_pending, words=None):
    """待成章事件能写几章。"""
    per = events_per_chapter(words)
    n = int(n_pending or 0)
    return {"per_chapter": per, "chapters": (n + per - 1) // per if n else 0}


# ============================================================ 素材装配

def build_world_facts_block(db, novel_id, events=None):
    """把"已经写定的世界事实"整理成正文侧的硬约束。

    踩过的坑（真实断裂）：正文提示词原来只有「作品基调 / 视角 / 事件 / 角色 /
    写作癖好」，**没有任何世界状态、没有时间、没有死伤名册**。于是模型写正文时
    只知道"这几件事发生了"，不知道"倒计时还剩 3""老张已经死了""这地方上周就炸了"，
    前后逻辑只能靠它自己猜——这就是"章节内容和推演/前文对不上"的根因。

    注意：只**注入事实**，不做因果校验（校验在推演侧由 guard 做）。正文侧要的是
    "写的时候别写错"，不是"写之前先拦下来"。
    """
    L = []
    clock = db.get_clock(novel_id)
    if clock.get("current_time"):
        L.append("当前世界时间：%s（本章事件均发生在此刻或之后，不得倒回）"
                 % clock["current_time"])

    states = db.get_world_state(novel_id)
    if states:
        L.append("已经写定的世界状态量（正文不得与之矛盾）：\n" + "\n".join(
            "  - %s = %s" % (s["key"], s["value"]) for s in states[:25]))

    # 死伤名册：最容易写错的地方
    chars = db.list_characters(novel_id)
    dead = [c["name"] for c in chars if c["status"] == "dead"]
    missing = [c["name"] for c in chars if c["status"] == "missing"]
    retired = [c["name"] for c in chars if c["status"] == "retired"]
    if dead:
        L.append("已死亡的角色（只能被提及/回忆/出现在他人讲述里，"
                 "绝对不能在场景中开口行动）：%s" % "、".join(dead))
    if missing:
        L.append("已失踪的角色（出场必须有明确理由）：%s" % "、".join(missing))
    if retired:
        L.append("已退场的角色：%s" % "、".join(retired))

    ents = db.list_entities(novel_id, status="active")
    if ents:
        L.append("活跃世界实体：%s" % "、".join(
            "%s(%s)" % (e["name"], e["entity_type"]) for e in ents[:20]))
    destroyed = db.list_entities(novel_id, status="destroyed")
    if destroyed:
        L.append("已摧毁/终结的实体（不得复原）：%s"
                 % "、".join(e["name"] for e in destroyed[:20]))

    threads = db.list_threads(novel_id, status="active")
    if threads:
        L.append("尚未解决的旧账（本章可以推进，但不要凭空结清）：%s"
                 % "；".join(t["title"] for t in threads[:10]))

    if not L:
        return ""
    return "\n".join(L)


def build_relations_block(db, novel_id, names=None, max_rel=18):
    """本章出场人物之间的关系——写手最需要的"这俩人该怎么对待彼此"。

    没有这一段，正文里角色之间就只能靠猜：陌生人一见面就熟络，
    或者明明该有芥蒂的两个人客客气气。只收与本章人物相关的关系；
    names 为空时全收（审稿场景）。
    """
    rels = db.list_relations(novel_id)
    if not rels:
        return ""
    want = set(names or [])
    labels = {"family": "亲属", "friend": "朋友", "enemy": "敌对", "lover": "恋慕",
              "colleague": "同僚", "mentor": "师徒", "rival": "竞争",
              "other": "其他"}
    # 归组交给 db.group_relations（三处展示统一收口）：两条 = 双向，显示 ↔；
    # 一条 = 单向，显示 → 并标注"对方不知情"。单向若画成对称，正文会把
    # "一个人单方面提防"写成"两人心照不宣"。另外单向关系的 description
    # 是从发起方视角写的，不能直接摊给被指向的人看，所以这里转述成中性说法。
    lines = []
    for g in db.group_relations(novel_id):
        r = g["record"]
        na, nb = g["a"], g["b"]
        # 只保留与本章人物相关的（names 为空则全要）
        if want and not ({na, nb} & want):
            continue
        arrow = "↔" if g["mutual"] else "→"
        desc = ""
        if r.get("description"):
            desc = ("——" + (r["description"] if g["mutual"]
                            else "（%s 一方的心态，%s 尚不知情）" % (na, nb)))
        status = r.get("status") or "active"
        st = "" if status == "active" else "（已%s）" % {
            "broken": "破裂", "evolved": "变化"}.get(status, status)
        lines.append("  - %s %s %s：%s（强度%s）%s%s" % (
            na, arrow, nb, labels.get(g["relation_type"], g["relation_type"]),
            r["intensity"], desc, st))
        if len(lines) >= max_rel:
            break
    if not lines:
        return ""
    return ("以下是这些人物之间**已经存在**的关系，"
            "请据此安排他们的态度与对话：\n" + "\n".join(lines)
            + "\n注意：关系好的不会互相提防，有芥蒂的不会掏心掏肺；"
              "标了 → 的是单向关系，箭头右边的人并不知情，"
              "不要把他写成心里有数；"
              "不在关系表里的两个人，本章之前可能素不相识——"
              "不要写得像多年老友。")


# ============================================================ 戏份分级
#
# 病根（真实反馈）：正文拿到的只是一份"事件清单"，每条事件里都塞满了
# 可拍的细节（零点刷手机 / 催收电话念数字 / 卫生间下单 / 楼上拖鞋声 /
# 洗碗 / 数钱 / 晾衣绳……）。**事件描述里写了 20 个细节，模型就会老实拍 20 个**，
# 于是全文变成"记录一切的摄像头"，节奏全被无关紧要的东西吃掉。
#
# 修法：在事件装配时就标出**这条该花多少笔墨**，模型才有力气分配的概念。

ROLE_MAIN = "main"        # 主戏：本章高光，写透
ROLE_PASS = "passing"     # 过场：一句话带过
ROLE_BACK = "background"  # 背景：只在本章需要时提一句

_ROLE_LABEL = {
    ROLE_MAIN: "主戏 —— 本章高光，必须写透（具体场景/动作/对话，可给 800–1500 字）",
    ROLE_PASS: "过场 —— 只用于串场，**一到两句、不超过 60 字**，禁止展开环境与动作分解",
    ROLE_BACK: "背景 —— 只在本章需要时提一句，不要单独成段",
}

# 天然是主戏的事件类型：矛盾、抉择、揭示、人物关系变化
_MAIN_TYPES = ("conflict", "decision", "reveal", "character", "turning", "climax")
# 天然是过场的类型
_PASS_TYPES = ("transition",)


def event_role(ev, max_importance=None, total_events=None):
    """判定一条事件在本章里该占多少笔墨。

    规则（按优先级）：
      1. 类型是冲突/抉择/揭示/人物 → 主戏
      2. 类型是过渡 → 过场
      3. importance >= 4 → 主戏；importance <= 2 → 过场
      4. 兜底：一条事件撑起整章（本章只有 2 条）→ 主戏；否则过场

    max_importance / total_events 由 build_events_block 传入，
    用于处理"本章全是低分量事件，但总得有一个主角"的情形。
    """
    et = str(ev.get("event_type") or "").strip().lower()
    imp = _imp(ev.get("importance"))
    if et in _MAIN_TYPES:
        return ROLE_MAIN
    if et in _PASS_TYPES:
        return ROLE_PASS
    if imp >= 4:
        return ROLE_MAIN
    if imp <= 2:
        return ROLE_PASS
    # imp == 3：如果它是本章分量最重的，仍按主戏写
    if max_importance is not None and imp >= max_importance and (total_events or 0) <= 3:
        return ROLE_MAIN
    return ROLE_BACK


def role_plan(events):
    """本章的笔墨分配计划——直接摆给模型看。

    返回 (摘要行, {序号: 角色})。摘要行形如：
      「本章 4 件事：主戏 2 件（第 1、3 条）／过场 2 件（第 2、4 条）。
        主戏写透，过场总共不超过 150 字。」
    """
    if not events:
        return "", {}
    imps = [_imp(e.get("importance")) for e in events]
    mx = max(imps) if imps else 3
    n = len(events)
    roles = {}
    for i, ev in enumerate(events, 1):
        roles[i] = event_role(ev, max_importance=mx, total_events=n)
    mains = [i for i, r in roles.items() if r == ROLE_MAIN]
    passes = [i for i, r in roles.items() if r == ROLE_PASS]
    backs = [i for i, r in roles.items() if r == ROLE_BACK]
    # 保底：一条主戏都没有时，把分量最重的那条提为主戏，否则全章没有重心
    if not mains and events:
        top = imps.index(mx) + 1
        roles[top] = ROLE_MAIN
        mains = [top]
        passes = [i for i, r in roles.items() if r == ROLE_PASS]
        backs = [i for i, r in roles.items() if r == ROLE_BACK]

    parts = []
    if mains:
        parts.append("【主戏】第 %s 条 —— 写透，是本章的力气所在"
                     % "、".join(str(i) for i in mains))
    if passes:
        parts.append("【过场】第 %s 条 —— 总共不超过 %d 字，一笔带过"
                     % ("、".join(str(i) for i in passes), 60 * len(passes)))
    if backs:
        parts.append("【背景】第 %s 条 —— 需要时提一句" % "、".join(str(i) for i in backs))
    return "本章共 %d 件事，笔墨分配如下：" % n + "；".join(parts) + "。", roles


def build_events_block(events):
    """把事件渲染成"给写手的事实清单"，并标出每条该花多少笔墨。

    ⚠ **必须过滤元层事件**（`canon_revision`）：它是"作者改了设定"，
    不是故事里发生的事。漏掉这一句，第 20 章就会写出
    「作者修正了设定：李四并非死亡」这种正文（约束 3 的原话）。
    """
    events = drop_meta_events(events)
    plan_line, roles = role_plan(events)
    L = []
    if plan_line:
        L.append("【笔墨分配】" + plan_line)
        L.append("")
    for i, ev in enumerate(events, 1):
        role = roles.get(i, ROLE_BACK)
        L.append("%d. 【%s · %s】%s" % (
            i, ev.get("event_type") or "plot", _ROLE_LABEL.get(role, role),
            ev.get("title") or ""))
        if ev.get("world_time"):
            L.append("   时间：%s" % ev["world_time"])
        if ev.get("description"):
            L.append("   发生了什么：%s" % ev["description"])
        if ev.get("actor"):
            L.append("   行动者：%s" % ev["actor"])
        if ev.get("action"):
            L.append("   做了什么：%s" % ev["action"])
        if ev.get("intent"):
            L.append("   为什么（意图，不要直接写出来）：%s" % ev["intent"])
        if ev.get("result"):
            L.append("   结果：%s" % ev["result"])
        if ev.get("involved_characters"):
            L.append("   涉及人物：%s" % "、".join(ev["involved_characters"]))
        if ev.get("involved_entities"):
            L.append("   涉及实体：%s" % "、".join(ev["involved_entities"]))
        for th in (ev.get("open_threads") or []):
            t = th.get("title") if isinstance(th, dict) else th
            if t:
                L.append("   ⚠ 本章留下的未解问题：%s" % t)
    return "\n".join(L)


def build_characters_block(db, novel_id, names):
    """装配出场角色的"内心资料"——写手最需要的就是这个。"""
    L = []
    for name in names:
        c = db.get_character_by_name(novel_id, name)
        if not c:
            L.append("- %s：（尚未建档）" % name)
            continue
        L.append("- %s（%s）" % (c["name"], c.get("role_tag") or "身份未定"))
        if c.get("personality"):
            L.append("    性格：%s" % c["personality"])
        if c.get("speech_style"):
            L.append("    说话方式：%s" % c["speech_style"])
        if c.get("appearance"):
            L.append("    外貌：%s" % c["appearance"])
        goals = db.list_goals(character_id=c["id"], status="active")
        for g in goals:
            tag = "长期" if g["goal_type"] == "long" else "短期"
            line = "    %s目标：%s" % (tag, g["content"])
            if g.get("motivation"):
                line += "（因为 %s）" % g["motivation"]
            if g.get("obstacle"):
                line += "（障碍：%s）" % g["obstacle"]
            L.append(line)
        mem = c.get("memory") or []
        if mem:
            L.append("    他记得：" + "；".join(
                str(m.get("text", ""))[:40] for m in mem[-3:]))
        # v6.7：他此刻**知道**什么。
        # 角色档案里的"性格/目标"决定他"会怎么做"，
        # 而"他知道什么"决定他"能不能这么做"——写手两样都需要。
        try:
            knows = db.list_knowledge(novel_id, character_id=c["id"],
                                      know_type="know", limit=3)
            if knows:
                L.append("    他此刻知道：" + "；".join(
                    (k.get("content") or "")[:40] for k in knows
                    if k.get("content")))
        except Exception:                       # noqa: BLE001
            pass
    return "\n".join(L)


def build_voice_block(db, novel_id):
    """作者声音档案。"""
    rows = db.list_voice(novel_id)
    if not rows:
        return ""
    by_cat = {}
    for r in rows:
        by_cat.setdefault(r["category"], []).append(r)
    labels = {"sensory": "感官偏好", "syntax": "句法习惯", "dialogue": "对白习惯",
              "taboo": "绝对不写", "motif": "反复出现的意象"}
    L = []
    for cat, label in labels.items():
        items = by_cat.get(cat)
        if not items:
            continue
        L.append("%s：" % label)
        for it in items:
            line = "  - %s" % it["content"]
            if it.get("example"):
                line += "（例：%s）" % it["example"]
            L.append(line)
    return "\n".join(L)


def build_novel_block(novel):
    L = ["题材：%s" % (novel.get("genre") or "未定"),
         "基调：%s" % (novel.get("tone") or "未定")]
    if novel.get("style"):
        L.append("文风：%s" % novel["style"])
    if novel.get("theme"):
        L.append("主题倾向：%s（不要直接写出来，让它在事件里自然显现）"
                 % novel["theme"])
    if novel.get("premise"):
        L.append("世界前提：%s" % novel["premise"])
    return "\n".join(L)


# ============================================================ 正文回流

def _known_characters_block(db, novel_id):
    rows = db.list_characters(novel_id)
    if not rows:
        return "（库里还没有任何角色）"
    return "\n".join(
        "- %s%s" % (c["name"], ("（%s）" % c["role_tag"]) if c.get("role_tag") else "")
        for c in rows)


def _known_threads_block(db, novel_id):
    rows = db.list_threads(novel_id, status="active")
    if not rows:
        return "（当前没有活跃线索）"
    return "\n".join("- %s" % t["title"] for t in rows)


# 能进 `fact_candidates` 的字段白名单。模型给的自由文本**不许直连**带 CHECK
# 的列（本项目铁律），落库前一律经这张名单过滤 + 在 db 层收口。
_CANDIDATE_FIELDS = ("subject_type", "subject_id", "subject_name", "predicate",
                     "object_text", "fact_type", "classification",
                     "classified_by", "classify_reason", "confidence",
                     "quote", "provenance", "source_event_id",
                     "target_character_id")


def _canon_facts_block(db, novel_id):
    """把【已写定的正史事实】摆给抽取器。拿不到就退化为空串（绝不抛）。"""
    try:
        from engine import facts as F
        return F.facts_block(db, novel_id, max_rows=30)
    except Exception:                       # noqa: BLE001
        return ""


def _brief(proj):
    """给 UI 的 projection 摘要。**只报条数**，不搬内容（回传体积可控）。"""
    proj = proj or {}
    return {
        "events": len(proj.get("events") or []),
        "facts": len(proj.get("facts") or []),
        "state": len(proj.get("state") or []),
        "knowledge": len(proj.get("knowledge") or []),
        "threads": len(proj.get("threads") or []),
        "characters": len(proj.get("characters") or []),
        "new_characters": len(proj.get("new_characters") or []),
    }


def _enqueue_conflicts(db, novel_id, chapter_ref, conflicts):
    """把冲突写进 `fact_conflicts`（等用户在第三页裁决）。返回写了几条。"""
    n = 0
    for c in (conflicts or []):
        try:
            db.add_conflict(
                novel_id, chapter_ref,
                conflict_type=c.get("conflict_type") or "prose_invents_fact",
                subject_name=c.get("subject_name") or "",
                prose_quote=c.get("prose_quote") or "",
                fact_statement=c.get("fact_statement") or "",
                detail=c.get("detail") or "",
                severity=c.get("severity") or "error",
                detected_by=c.get("detected_by") or "python",
                fact_id=c.get("fact_id"))
            n += 1
        except Exception:                   # noqa: BLE001
            continue
    return n


def extract_and_apply_state(db, router, novel_id, chapter_text, chapter_ref=0,
                            *, dry_run=False, from_projection=None):
    """把正文里**确实写出来**的变化回流进库（方案 6.5 · v6.7 重写）。

    推演写的是"应该发生什么"，落笔成正文时会有出入。这一层把正文里真正
    发生的补回库里，否则后面越写越偏，推演还在用推演当时那本旧账。

    v6.7 的链路（**顺序不能改**，见设计 6.3.1）：

        抽取 → 折成 projection（纯数据）→ ▲分类 → 按分类三分流
              → 冲突比对（只比 world 类）→ 无 error 才落库

    与旧版的根本差别：旧版是「抽到啥就直接写啥」。**"无冲突"不等于"是真的"**
    ——它只说明"没跟已知的打架"。正文写「李四想起三年前杀过人」，正史从没记过
    这件事，比对结果永远是"无冲突"。所以必须先问"这是什么性质的话"。

    `dry_run=True` 只算不写（用于预览）；`from_projection` 可传一份已算好的
    投影复用它（供「事实检查」重跑时避免重复抽一次）。

    返回一份可展示的摘要；档位没配或模型没给出可解析结果时返回 None。
    """
    from engine import prompts as P
    from engine import applier
    from engine import ticktime

    text = (chapter_text or "").strip()
    if not text:
        return None
    ok, _why = router.is_ready("state_extract")
    if not ok:
        return None

    tick = 0
    try:
        tick = ticktime.current_tick(db, novel_id)
    except Exception:                       # noqa: BLE001
        tick = 0

    mode = "reuse"
    if from_projection is not None:
        proj = from_projection
    else:
        parsed, raw, mode = router.run_json(
            "state_extract", schema=P.STATE_EXTRACT_SCHEMA,
            system=P.STATE_EXTRACT_SYSTEM,
            user=P.state_extract_user(text, _known_characters_block(db, novel_id),
                                      _known_threads_block(db, novel_id),
                                      _canon_facts_block(db, novel_id)),
            max_tokens=4096)
        if not parsed:
            return None
        # ① 抽取结果先折成 projection —— **纯数据，一个字节都没落库**
        proj = applier.projection_from_extract(db, novel_id, parsed,
                                               tick=tick,
                                               chapter_ref=chapter_ref)

    # ▲ ② 【约束 2】Fact Classification
    #     先问"这是什么性质的话"，再问"它跟正史打不打架"。
    #     没有这一步，"李四想起三年前杀过人"会一路无阻地进 facts。
    proj = applier.classify_projection(db, novel_id, proj,
                                       chapter_ref=chapter_ref)
    to_facts, to_knowledge, to_candidates = \
        applier.dispatch_by_classification(db, novel_id, proj,
                                           chapter_ref=chapter_ref)

    out = {"mode": mode, "applied": False, "conflicts": [], "dry_run": bool(dry_run),
           "cognition": len(to_knowledge), "pending": len(to_candidates),
           "world_facts": len(to_facts),
           "world_state": [], "characters": [], "knowledge": [],
           "locations": [], "new_threads": [], "resolved_threads": [],
           "new_characters": [], "projection": _brief(proj)}

    if dry_run:
        return out

    # ---- ③ 路 A · cognition：直接进 character_knowledge，**绕过比对**
    #      它根本不进 facts，谈不上冲突（这正是约束 1"严格分离"带来的简化）
    for f in to_knowledge:
        cid = f.get("target_character_id") or f.get("subject_id")
        if not cid or not _s(f.get("content")):
            continue
        try:
            db.record_knowledge(
                novel_id, cid, _s(f.get("content")),
                know_type=_s(f.get("know_type")) or "believe",
                about_type=_s(f.get("about_type")) or "fact",
                about_id=f.get("about_id"),
                about_text=_s(f.get("about_text")),
                source=_s(f.get("source")) or "inferred",
                confidence=f.get("confidence") or 3,
                learned_chapter=chapter_ref, learned_tick=tick)
            out["knowledge"].append({"name": _s(f.get("belief_by")),
                                     "knowledge": _s(f.get("content"))})
        except Exception:                   # noqa: BLE001
            continue

    # ---- ③ 路 B · memory / dream / unverified / author_claim → 挂起等裁决。
    #      **不碰 facts**，第一页出「待裁决 N 条」灰徽标（可看可不看）
    stored = 0
    if to_candidates:
        stored = applier.store_candidates(
            db, novel_id, to_candidates, chapter_ref=chapter_ref,
            source_kind="prose")

    # ---- ④ 路 C · world：只有这一支允许继续走比对与落地
    if to_facts:
        world_proj = dict(proj)
        world_proj["facts"] = to_facts
        conflicts = applier.check_projection_conflicts(
            db, novel_id, world_proj, chapter_number=chapter_ref)
        # 模型自己报的冲突也一并入队（它是"读得懂语义"的那一路）
        for mc in (proj.get("model_conflicts") or []):
            conflicts.append({
                "conflict_type": "prose_contradicts_fact",
                "chapter_number": chapter_ref, "fact_id": None,
                "subject_name": "", "prose_quote": _s(mc.get("quote")),
                "fact_statement": _s(mc.get("fact")),
                "detail": _s(mc.get("detail")), "severity": "warning",
                "detected_by": "llm"})

        # ⚠ **只有 error 级才拦**（设计 §14 风险表：warning 级只记不拦）。
        #   不这么分级的话，每章都会因为"正文写了正史里没有的新事"被拦下——
        #   那是绝大多数章节的常态，冲突卡片立刻退化成每章一次的骚扰。
        hard = [c for c in conflicts if c.get("severity") == "error"]
        if conflicts:
            out["queued_conflicts"] = _enqueue_conflicts(
                db, novel_id, chapter_ref, conflicts)
        if hard:
            # 只回传**拦下来的**那几条（error 级），不是全部——
            # 全部里混着一堆 warning（"正文写了正史没有的新事"，那是章节常态），
            # 把 11 条一起报出去，用户看到"11 条硬冲突"会以为整章崩了。
            out["conflicts"] = hard
            out["all_conflicts"] = len(conflicts)
            db.execute(
                "UPDATE chapters SET fact_conflict_count=?, verified=0 "
                "WHERE novel_id=? AND chapter_number=?",
                (len(hard), novel_id, chapter_ref))
            return out

    # ---- ⑤ 无 error 冲突：照常应用（用户无感，行为与旧版一致）
    world_proj = dict(proj)
    world_proj["facts"] = to_facts
    summary = applier.apply_soft_projection(db, novel_id, world_proj,
                                            tick=tick,
                                            chapter_ref=chapter_ref)
    db.execute("UPDATE chapters SET verified=1, fact_conflict_count=0 "
               "WHERE novel_id=? AND chapter_number=?",
               (novel_id, chapter_ref))

    out["applied"] = True
    out["applied_summary"] = summary
    out["world_state"] = [st.get("key") for st in (proj.get("state") or [])]
    out["characters"] = [c.get("name") for c in (proj.get("characters") or [])]
    out["new_threads"] = [t.get("title") for t in (proj.get("threads") or [])]
    out["new_characters"] = summary.get("new_characters") or []
    out["locations"] = [
        {"name": c.get("name"), "text": c.get("location"),
         "location_id": c.get("location_id")}
        for c in (proj.get("characters") or []) if c.get("location")]

    # ---- ⑥ 已解决线索（按标题关键词匹配活跃线索；不进 projection 管线）
    active = db.list_threads(novel_id, status="active")
    for kw in (proj.get("resolved_threads") or []):
        kw = (kw or "").strip()
        if not kw:
            continue
        for t in active:
            if kw in t["title"] or t["title"] in kw:
                db.resolve_thread(t["id"], chapter_ref,
                                  note="第%s章正文回流" % chapter_ref)
                out["resolved_threads"].append(t["title"])
                break

    out["candidates_stored"] = stored
    return out


# ============================================================ 生成器

class ProseGenerator:
    """把事件写成正文。"""

    def __init__(self, db, router, novel_id):
        self.db = db
        self.router = router
        self.novel_id = novel_id

    def confirm_chapter(self, chapter_number, *, force=False):
        """▲ 作者「发布」一章 → 把这一章确认进世界。

        语义（用户明确要求）：**点击发布 = 作者确认这一章的内容成立**，
        所以这一刻就要让世界状态因为这些内容而更新。

        和 write_chapter 里那次自动回流的区别：
          · write_chapter 的回流是"顺手做"，失败/没配档位就算了，
            世界停在推演时的那本旧账上；
          · confirm_chapter 是"作者拍板"，要把正文里**真正写出来**的变化
            补进 world_state / characters / knowledge / threads / facts。

        幂等：已回流过的章（verified=1）默认不再重复跑——
        重跑一次要花一次 LLM 抽取的钱，而且第二次抽取结果可能与第一次不同，
        会把作者刚确认过的状态又改一遍。要强制重跑传 force=True。

        返回一份可展示的摘要；章节不存在返回 None。
        """
        db = self.db
        cnum = int(chapter_number or 0)
        if not cnum:
            return None
        row = db.get_chapter(self.novel_id, cnum)
        if not row:
            return None

        text = (row.get("content") or "").strip()
        out = {"chapter_number": cnum, "title": row.get("title") or "",
               "word_count": row.get("word_count") or len(text),
               "already": bool(row.get("verified")),
               "applied": False, "skipped": "", "extracted": None,
               "world_state": [], "characters": [], "knowledge": [],
               "new_threads": [], "resolved_threads": [],
               "new_characters": [], "conflicts": [], "pending": 0,
               "all_conflicts": 0}

        if not text:
            out["skipped"] = "这一章还没有正文，没什么可确认的。"
            return out

        # 已回流过：不重复花 token，直接把现状报回去
        if out["already"] and not force:
            out["skipped"] = ("这一章的内容此前已经确认进世界了（无需重复确认）。"
                              "如果正文后来改过、想让世界跟着改，用「重新确认」。")
            out["applied"] = True
            return out

        # 档位没配：说清楚，别让作者以为"世界怎么还是没动"
        ok, why = (True, "")
        try:
            ok, why = self.router.is_ready("state_extract")
        except Exception:                       # noqa: BLE001
            ok = False
        if not ok:
            out["skipped"] = ("「状态抽取」档位没配置，无法把这一章的内容确认进世界。"
                              "请到「模型档位」里给 state_extract 选一个模型。")
            return out

        try:
            ex = extract_and_apply_state(
                db, self.router, self.novel_id, text, cnum)
        except Exception as e:                  # noqa: BLE001
            out["skipped"] = "正文回流失败：%s: %s" % (type(e).__name__, e)
            return out

        if not ex:
            out["skipped"] = ("模型这次没给出可解析的状态抽取结果，"
                              "世界状态没变。可以再点一次「重新确认」。")
            return out

        out["extracted"] = ex
        out["applied"] = bool(ex.get("applied"))
        out["world_state"] = ex.get("world_state") or []
        out["characters"] = ex.get("characters") or []
        out["knowledge"] = ex.get("knowledge") or []
        out["new_threads"] = ex.get("new_threads") or []
        out["resolved_threads"] = ex.get("resolved_threads") or []
        out["new_characters"] = ex.get("new_characters") or []
        out["conflicts"] = ex.get("conflicts") or []
        out["all_conflicts"] = ex.get("all_conflicts") or len(out["conflicts"])
        out["pending"] = ex.get("pending") or 0

        if not out["applied"]:
            # 有 error 级冲突被拦下：状态**故意没落**，让作者先裁决
            n = len(out["conflicts"])
            out["skipped"] = ("正文与正史有 %d 处硬冲突，"
                              "状态**没有**落库——请先到「事实冲突」里裁决。"
                              % n)
        return out

    def generate(self, events, shape=None, pov="", words=None, extra="",
                 include_tail=True, stream=False):
        """生成正文。

        events 可以是事件 dict 列表，也可以是从库里的 events 行（结构兼容）。
        """
        novel = self.db.get_novel(self.novel_id)
        if not novel:
            raise ValueError("小说不存在：%s" % self.novel_id)

        if not events:
            raise ValueError("没有事件可写——请先推进世界")

        shape = shape or infer_shape(events)
        words = words or int(novel.get("target_words_per_chapter") or 3000)

        names = []
        for ev in events:
            actor = ev.get("actor")
            if actor:
                names.append(actor)
            names.extend(ev.get("involved_characters") or [])
        # 去重保序
        seen = set()
        uniq = []
        for n in names:
            if n and n not in seen:
                seen.add(n)
                uniq.append(n)

        if not pov:
            # 默认取重要角色中的最高位
            chars = self.db.list_characters(self.novel_id, status="alive")
            rank_order = {"A": 0, "B": 1, "C": 2, "D": 3, "E": 4}
            cands = [c for c in chars if c["name"] in seen]
            cands.sort(key=lambda c: rank_order.get(c["rank"], 9))
            pov = cands[0]["name"] if cands else (uniq[0] if uniq else "")

        tail = ""
        if include_tail:
            prev = self.db.get_chapter(self.novel_id, novel["current_chapter"])
            if prev and prev.get("content"):
                tail = prev["content"][-800:]

        user = prose_user(
            novel_block=build_novel_block(novel),
            events_block=build_events_block(events),
            characters_block=build_characters_block(self.db, self.novel_id, uniq),
            voice_block=build_voice_block(self.db, self.novel_id),
            facts_block=build_world_facts_block(self.db, self.novel_id, events),
            relations_block=build_relations_block(self.db, self.novel_id, uniq),
            words=words, shape=shape, pov=pov, tail=tail, extra=extra)

        # 预算：中文按每字 ~1 token 估，留出余量。
        # 注意推理模型（deepseek-flash / reasoner 等）的思维链也从这个预算里扣，
        # 所以正文为空时不要直接落库，先翻倍重试。
        budget = min(16384, max(1024, words * 3))

        if stream:
            return self.router.stream("prose_gen", system=PROSE_SYSTEM, user=user,
                                      max_tokens=budget)
        out = self.router.run("prose_gen", system=PROSE_SYSTEM, user=user,
                              max_tokens=budget)
        content = str(out.get("content") or "").strip()
        if not content and (out.get("reasoning") or "") and budget < 16384:
            out = self.router.run("prose_gen", system=PROSE_SYSTEM, user=user,
                                  max_tokens=min(32768, budget * 2))
            content = str(out.get("content") or "").strip()
        if not content:
            raise ValueError(
                "模型没有返回正文（finish_reason=%s）。若用的是推理模型"
                "（deepseek-flash / deepseek-reasoner 之类），思维链会吃掉输出"
                "预算，正文会被截断成空；请换非推理模型，或调低目标字数。"
                % out.get("finish_reason"))
        return {
            "content": content,
            "reasoning": out.get("reasoning") or "",
            "shape": shape,
            "pov": pov,
            "ending_mode": infer_ending_mode(events),
            "characters": uniq,
            # 被 max_tokens 掐断：正文可用但结尾不完整，交给调用方决定是否重写
            "truncated": (out.get("finish_reason") == "length"),
        }

    def regenerate_with_notes(self, events, notes, original="", **kw):
        """带修改意见重写（用于"这一段再改改"）。"""
        extra = "## 修改意见（必须落实）\n%s" % notes
        if original:
            extra += "\n\n## 原稿（在此基础上修改，保留其中好的部分）\n%s" % original
        return self.generate(events, extra=extra, **kw)

    def write_chapter(self, chapter_number=None, events=None, shape=None,
                      words=None, summary=None, extra="", stream=False,
                      event_ids=None, limit=None, extract=True):
        """生成并保存一章。

        events / event_ids 都不给时，自动取"尚未成章的事件"——**但要分批**。
        一次把 20 个事件塞进一章，每件事只剩百来字，写出来就是流水账；
        默认按目标字数推荐一批（events_per_chapter），剩下的留给下一章。

        extract=True 时，落库后把正文里实际写出来的变化回流进库（见
        extract_and_apply_state）——推演与正文之间靠它对齐。
        """
        novel = self.db.get_novel(self.novel_id)
        cnum = chapter_number or ((novel.get("current_chapter") or 0) + 1)
        words = words or int(novel.get("target_words_per_chapter") or 3000)

        remaining = 0
        if events is None:
            pool = [e for e in self.db.list_events(self.novel_id, chapter_ref=0)
                    if not e.get("chapter_ref")]
            if not pool:
                # 退而取最近未归章的事件
                pool = [e for e in self.db.list_events(self.novel_id)
                        if not e.get("chapter_ref")]
            # 元层事件（canon_revision）与背景动向不是一章的取材
            pool = drop_meta_events(pool)
            if event_ids:
                want = [int(i) for i in event_ids]
                by_id = {e["id"]: e for e in pool}
                events = [by_id[i] for i in want if i in by_id]
                if not events:
                    raise ValueError(
                        "选中的事件不在待成章列表里（可能已被别的章用掉）。"
                        "刷新页面重新选。")
            else:
                n = int(limit) if limit else events_per_chapter(words)
                events = pool[:max(1, n)]
            remaining = max(0, len(pool) - len(events))

        if not events:
            raise ValueError("没有待成章的事件。请先推演并「落定成正文」。")

        if stream:
            return {"stream": self.generate(events, shape, words=words,
                                            extra=extra, stream=True),
                    "chapter_number": cnum, "events": events,
                    "remaining": remaining}

        gen = self.generate(events, shape=shape, words=words, extra=extra)
        content = gen["content"]
        title = self._make_title(gen, events)

        if summary is None:
            summary = "；".join((e.get("title") or "") for e in events[:3])

        saved = self.db.save_chapter(
            self.novel_id, cnum, title=title, content=content,
            summary=summary, pov_character=gen["pov"],
            chapter_shape=gen["shape"], ending_mode=gen["ending_mode"],
            world_time_start=(events[0].get("world_time") or ""),
            world_time_end=(events[-1].get("world_time") or ""),
            source_event_ids=[e["id"] for e in events if e.get("id")],
            change_note="由事件流生成")

        # 事件归章
        ids = [e["id"] for e in events if e.get("id")]
        if ids:
            self.db.attach_events_to_chapter(ids, cnum)

        # 正文回流：把正文里确实写出来的变化补回库里
        # （档位没配 / 解析失败都不算错误，最多是这章的状态变化晚一步进库）
        extracted = None
        if extract:
            try:
                extracted = extract_and_apply_state(
                    self.db, self.router, self.novel_id, content, cnum)
            except Exception as e:                  # noqa: BLE001
                extracted = {"error": "%s: %s" % (type(e).__name__, e)}

        return {"chapter": saved["chapter"], "version": saved["version"],
                "word_count": saved["word_count"], "shape": gen["shape"],
                "pov": gen["pov"], "ending_mode": gen["ending_mode"],
                "event_count": len(events), "remaining": remaining,
                "truncated": gen.get("truncated", False),
                "extracted": extracted}

    def _make_title(self, gen, events):
        """标题优先用最有分量的事件标题，避免"第X章"这种空标题。"""
        if not events:
            return ""
        best = max(events, key=lambda e: _imp(e.get("importance")))
        t = str(best.get("title") or "").strip()
        return t[:20]

    def suggest_summary(self, content):
        """用便宜模型给已写的正文做摘要。"""
        parsed, raw, mode = self.router.run(
            "summarize", system="你为小说章节写不超过 120 字的摘要。"
                                "只写发生了什么，不写这意味着什么。",
            user=content[:6000], max_tokens=512)
        return (raw or "").strip()
