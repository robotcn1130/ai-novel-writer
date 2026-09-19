# -*- coding: utf-8 -*-
"""
tick 时间层（v6.7）：世界时间的**唯一可比较形态**。

为什么要有这一层（设计 §0.5）：

方案原本写的是「LLM 负责把自然语言时间解析成 tick，Python 负责比较、加减、排序」。
前半句要**改口径**——这是虚构历法（"第三纪元 214 年 霜月 12 日 黄昏"），
让模型把它解析成整数是**不可靠**的：同一句话两次调用可能给出 9127 和 9131。
而"过了多久"是可解析的（"一个下午"、"三天"、"转眼半年"）。

所以定案是：

| 谁 | 产出什么 | 可靠性 |
|---|---|---|
| LLM（推演时） | `tick_delta`：**整数增量**（一场戏=1，一天≈1，一周≈7） | 高（只需数个数） |
| Python（本模块） | `absolute_tick += tick_delta`；比较、排序、到期判定 | 100%（整数运算） |
| 用户（初始化时） | 给出起始 `absolute_tick`（默认 0） | — |
| 规则表兜底（`parse_delta`） | "三天"→3，用于手工设置时间/时间线锚点，**不调 LLM** | 中 |

好处：`world.expire_due_anchors()` 从「让模型判断哪条约定到期了」
（每个 tick 多花一次 LLM 调用，且**同一输入两次结果可能不同**）
退化成一条 SQL，结果确定。

铁律：**`world_clock.current_time` 是只读展示文本，任何比较逻辑不得读它。**
要比较一律读 `absolute_tick`。
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC_V6 = os.path.dirname(_HERE)
for _p in (_SRC_V6, os.path.join(_SRC_V6, "core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# 1 tick 代表什么（与 world_clock.tick_unit 同词表）
TICK_UNITS = ("scene", "day", "stage")
DEFAULT_UNIT_NOTE = "1 场戏"


# ---------------------------------------------------------------- 读

def current_tick(db, novel_id):
    """当前世界 tick。缺行/脏值一律返回 0（不抛异常）。"""
    try:
        row = db.one("SELECT absolute_tick FROM world_clock WHERE novel_id=?",
                     (novel_id,))
    except Exception:                       # noqa: BLE001
        return 0
    if not row:
        return 0
    try:
        return int(row.get("absolute_tick") or 0)
    except (TypeError, ValueError):
        return 0


def display_time(db, novel_id):
    """世界时间的**展示文本**。纯展示，不做任何比较。"""
    try:
        row = db.one("SELECT current_time FROM world_clock WHERE novel_id=?",
                     (novel_id,))
    except Exception:                       # noqa: BLE001
        return ""
    return str((row or {}).get("current_time") or "")


def unit_note(db, novel_id):
    """tick 单位的展示文案（"1 tick = 一场戏"）。"""
    try:
        row = db.one("SELECT tick_unit_note, tick_unit FROM world_clock "
                     "WHERE novel_id=?", (novel_id,))
    except Exception:                       # noqa: BLE001
        return DEFAULT_UNIT_NOTE
    row = row or {}
    note = str(row.get("tick_unit_note") or "").strip()
    if note:
        return note
    unit = str(row.get("tick_unit") or "scene")
    return {"scene": DEFAULT_UNIT_NOTE, "day": "1 tick = 一天",
            "stage": "1 tick = 一个阶段"}.get(unit, DEFAULT_UNIT_NOTE)


# ---------------------------------------------------------------- 写

def advance(db, novel_id, delta=1, new_time=None, chapter_ref=None,
            reason="时间推进", event_id=None):
    """推进世界时间。

    同步维护三个字段，缺一不可：
      · absolute_tick —— 权威值，所有比较读它
      · total_ticks   —— **只读别名**。老代码还在读它，不同步更新的话
                         那些地方会永远看到 0（"世界时间页显示推了 0 次"）。
      · current_time  —— 展示文本。**只在调用方给了新文本时才改**：
                         传 None 表示"这次模型没给新时间描述"，那就保留原文并
                         标记 display_note，**绝不用 tick 反推文字**
                         （反推出来的"第 9127 tick"不是这个世界的人说的话）。

    返回新的 absolute_tick。
    """
    tick = current_tick(db, novel_id)
    try:
        delta = int(delta)
    except (TypeError, ValueError):
        delta = 1
    if delta < 0:
        delta = 0
    new_tick = tick + delta

    sets = ["absolute_tick = ?", "total_ticks = ?",
            "updated_at = datetime('now','localtime')"]
    vals = [new_tick, new_tick]
    if new_time is not None and str(new_time).strip():
        sets.append("current_time = ?")
        vals.append(str(new_time).strip())
        sets.append("display_note = ''")
        note_text = ""
    else:
        note_text = "（时间未更新）"
        sets.append("display_note = ?")
        vals.append(note_text)
    if chapter_ref is not None:
        sets.append("last_chapter = ?")
        vals.append(int(chapter_ref))
    vals.append(novel_id)

    try:
        db.get_clock(novel_id)          # 确保行存在
        with db.tx() as con:
            con.execute("UPDATE world_clock SET %s WHERE novel_id=?"
                        % ", ".join(sets), vals)
            con.execute(
                "INSERT INTO world_state_log(novel_id, key, old_value, new_value, "
                "reason, chapter_ref, event_id, tick) VALUES (?,?,?,?,?,?,?,?)",
                (novel_id, "__tick__", str(tick), str(new_tick),
                 str(reason or "时间推进"), int(chapter_ref or 0),
                 event_id, new_tick))
    except Exception:                       # noqa: BLE001
        pass
    return new_tick


def set_tick(db, novel_id, tick, new_time=None):
    """直接设定当前 tick（世界初始化 / 用户手改起始时间）。不计入推进次数。"""
    try:
        tick = int(tick)
    except (TypeError, ValueError):
        tick = 0
    try:
        db.get_clock(novel_id)
        sets = ["absolute_tick = ?", "total_ticks = ?",
                "updated_at = datetime('now','localtime')"]
        vals = [tick, tick]
        if new_time is not None and str(new_time).strip():
            sets.append("current_time = ?")
            vals.append(str(new_time).strip())
        vals.append(novel_id)
        with db.tx() as con:
            con.execute("UPDATE world_clock SET %s WHERE novel_id=?"
                        % ", ".join(sets), vals)
    except Exception:                       # noqa: BLE001
        pass
    return tick


# ---------------------------------------------------------------- 文本 → 增量（兜底）

# 中文数字
_CN_DIGITS = {"零": 0, "〇": 0, "一": 1, "壹": 1, "二": 2, "两": 2, "贰": 2,
              "三": 3, "叁": 3, "四": 4, "肆": 4, "五": 5, "伍": 5,
              "六": 6, "陆": 6, "七": 7, "柒": 7, "八": 8, "捌": 8,
              "九": 9, "玖": 9}
# ⚠ "十"/"拾" **不能放进 _CN_DIGITS**。放进去的话 `if ch in _CN_DIGITS` 会先命中，
#   专门处理十位的那个分支就成了死代码 —— "十二" 会被算成 2、"二十五" 算成 5。
_CN_TEN = ("十", "拾")

# 单位 → tick 系数。
# 口径与提示词里给模型的那段保持一致（"三天≈3，一周≈7，一个月≈30"），
# 否则模型给的整数和规则表兜底算出来的会打架。
_UNIT_FACTORS = (
    ("年", 365), ("载", 365), ("岁", 365),
    ("月", 30), ("个月", 30),
    ("周", 7), ("星期", 7), ("礼拜", 7), ("旬", 10),
    ("天", 1), ("日", 1), ("夜", 1), ("晚", 1), ("昼夜", 1),
    ("时辰", 1), ("小时", 1), ("刻", 1), ("盏茶", 1), ("炷香", 1),
    ("场", 1), ("幕", 1), ("段", 1), ("会", 1),
)

# 语气上的"过了挺久"：这些词后面带时间单位时，跨度 ×2
_VAGUE_LONG = ("转眼", "一晃", "不知不觉", "弹指", "眨眼", "倏忽")
# 极短的表述：一律 1（至少推进一场戏）
_TINY = ("片刻", "须臾", "instant", "一会儿", "俄顷", "少顷", "半晌")
# 量词/前缀填充词：出现在数字与单位之间或之前，**按子串清掉**（不看位置）。
# "经过了三天" / "大约三天" / "两个月" / "一年半" 里的这些字都不是数字。
_FILLER_WORDS = ("经过", "过了", "大约", "将近", "整整", "足有", "已有",
                 "不到", "个", "了")


def _cn_to_int(text):
    """把 "三" / "十二" / "二十五" / "半" 转成整数。识别不了返回 None。

    实现按 "X十Y" 拆（中文里 0–99 只有这一种结构）：
      "十二" → 十在首位 → 高位默认 1、低位 2 → 12
      "二十五" → 高位 2、低位 5 → 25
      "十" → 10
    """
    s = str(text or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s in ("半", "半刻", "半晌"):
        return 1
    if s == "几":
        return 1

    tens = [i for i, ch in enumerate(s) if ch in _CN_TEN]
    if tens:
        idx = tens[0]
        hi = _cn_digits_only(s[:idx])
        if hi is None:
            hi = 1                       # "十二" 的十位没写数字，按 1 算
        lo = _cn_digits_only(s[idx + 1:]) if s[idx + 1:] else 0
        if lo is None:
            return None
        return hi * 10 + lo
    return _cn_digits_only(s)


def _cn_digits_only(s):
    """只含数字字符的串 → 整数（"二五" → 25）。空串返回 None。"""
    s = str(s or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    val, hit = 0, False
    for ch in s:
        if ch not in _CN_DIGITS:
            return None
        val = val * 10 + _CN_DIGITS[ch]
        hit = True
    return val if hit else None


def parse_delta(text, unit="scene"):
    """规则表：把"三天""一个下午""转眼半年"换算成整数 tick。**不调 LLM**。

    这是**兜底**：主路径是推演时模型直接给 `tick_delta` 整数。
    本函数用于手工设置时间、时间线锚点换算，以及模型没给增量时的保底。

    返回 >=1 的整数。
    """
    s = str(text or "").strip()
    if not s:
        return 1
    if any(t in s for t in _TINY):
        return 1

    for word, factor in _UNIT_FACTORS:
        idx = s.find(word)
        if idx < 0:
            continue
        head = s[:idx].strip()
        # 去掉填充词："经过了三天" → "三"；"大约三天" → "三"；"两个月" → "两"。
        #
        # ⚠ 两个坑都踩过：
        #   1. 原来写 `head.endswith(junk)` —— 这些词在 head 的**开头**，
        #      永远命中不了，于是 parse_delta("过了三天") 返回 1（最小步长），
        #      时间推进被悄悄压扁。
        #   2. 改成 startswith 也不够："两个月" 的 head 是 "两个"，
        #      填充词 "个" 夹在中间。所以这里按**子串**清掉，不看位置。
        for junk in _FILLER_WORDS:
            head = head.replace(junk, "")
        head = head.strip().strip("的").strip()
        if head in ("半",):                     # "半个月" → 15，不是 30
            v = max(1, int(factor / 2))
        else:
            n = _cn_to_int(head)
            if n is None or n <= 0:
                n = 1
            v = n * factor
        if any(t in s for t in _VAGUE_LONG):
            v = v * 2
        return max(1, int(v))

    # 纯数字："3" / "3.5"
    try:
        v = float(s)
        return max(1, int(round(v)))
    except (TypeError, ValueError):
        pass
    # 识别不出：保守推进一场戏（宁少不多）
    return 1


def parse_span_to_tick(clock_row, text):
    """处理"三天后"这类相对表述。

    返回 `(delta, remaining)`：delta 是推进的 tick 数；remaining 是
    文本里**没能解释清楚**的剩余部分（留给调用方决定是否展示）。
    第一版几乎总是 remaining=''——保留这个返回值是为了日后支持
    "三天后的黄昏"这种混合表述。
    """
    delta = parse_delta(text)
    return delta, ""


def delta_from(parsed, default=1):
    """从推演结果里取 tick_delta。模型没给 / 给了脏值 → default。"""
    raw = (parsed or {}).get("tick_delta")
    try:
        v = int(float(str(raw).strip()))
    except (TypeError, ValueError):
        return int(default)
    if v < 1:
        return int(default)
    return min(v, 3650)          # 一次推演最多推 10 年，再多必是模型犯浑


# ---------------------------------------------------------------- 提示词用

def tick_block(db, novel_id, base_tick=None):
    """给模型看的一行世界时间。

    base_tick 非 None（沙盘分支推演）时附一句"该分支位于第 N tick 的分叉点"——
    不写这句，模型会拿主线最新时间去算分支上的"过了多久"。
    """
    tick = current_tick(db, novel_id) if base_tick is None else int(base_tick)
    text = display_time(db, novel_id) or "尚未设定"
    line = "【世界时间】%s（第 %d tick；%s）" % (text, tick, unit_note(db, novel_id))
    if base_tick is not None:
        line += "\n> 这段推演位于第 %d tick 的分叉点，时间从那一刻往后走。" % int(base_tick)
    return line


def describe_tick(db, novel_id, tick):
    """反查某个 tick 对应的展示文本。

    取最近的 location_history / events 的 world_time；都没有就老实说"第 N tick"
    ——**不要**用 tick 编一个虚构历法的日期出来。
    """
    try:
        tick = int(tick)
    except (TypeError, ValueError):
        return ""
    row = db.one(
        "SELECT world_time FROM events WHERE novel_id=? AND tick<=? "
        "AND world_time IS NOT NULL AND world_time<>'' ORDER BY tick DESC, id DESC "
        "LIMIT 1", (novel_id, tick))
    if row and row.get("world_time"):
        return str(row["world_time"])
    return "第 %d tick" % tick


def tick_line(db, novel_id, limit=6):
    """待触发锚点的倒计时文案（供简报使用）。"""
    try:
        rows = db.query(
            "SELECT time_anchor, countdown_name, absolute_tick, description "
            "FROM timeline WHERE novel_id=? AND status='pending' "
            "AND absolute_tick>0 ORDER BY absolute_tick LIMIT ?",
            (novel_id, int(limit)))
    except Exception:                       # noqa: BLE001
        return []
    cur = current_tick(db, novel_id)
    out = []
    for r in rows:
        left = int(r.get("absolute_tick") or 0) - cur
        label = str(r.get("time_anchor") or "")
        if r.get("countdown_name"):
            label += "（%s）" % r["countdown_name"]
        if left > 0:
            label += "（还有 %d tick 到期）" % left
        out.append(label)
    return out
