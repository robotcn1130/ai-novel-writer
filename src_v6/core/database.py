# -*- coding: utf-8 -*-
"""
AI 小说创作系统 · 数据访问层 v6.7

设计要点：
  1. 世界模拟范式下，"世界状态"是第一等公民，所有读写围绕它展开。
  2. open-schema：角色/实体/事件的扩展字段存 JSON，缺字段自动补，不做 ALTER。
  3. 不做 v4 兼容；不提供追读力/三线占比/AI 痕迹检测相关方法。
  4. 每个方法尽量"短事务、明确语义"，复杂编排交给上层 engine。

v6.7 新增的三条硬规则（细则见 docs/v6.7_数据库与代码改造详细设计 §0.6）：

  ▲ 规则一（约束 1）：**世界真相与角色认知物理分离**。
     `facts` 表只接受 canonical/derived/provisional 三态，
     角色的 believe/suspect/assume/memory/dream 走 `record_knowledge()`。
     执行点：`engine/facts.add_fact()` 的第一行。

  ▲ 规则二（约束 4）：**`world_state` 的写权限在代码层收口**。
     唯一白名单是 `_STATE_WRITERS`，只有一个名字。
     世界页手改走 `set_state_value_manual()`（写 NULL=锁定，投影不覆盖），
     初始化/推演/正文回流一律走 fact 投影。
     "数据库字段存在，不代表所有模块都有写权限。"——权限在代码里，不在文档里。

  ▲ 规则三：模型自由文本**不许直连带 CHECK/INT 的列**。
     所有枚举列（含 v6.7 新增的 know_type / lifecycle / classification /
     conflict_type / scope）一律在下面"枚举收口区"归一，绝不抛异常。
"""
import json
import os
import re
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(os.path.dirname(_HERE))       # 项目根
DEFAULT_DB = os.path.join(ROOT_DIR, "sql", "novel.db")
NOVELS_DIR = os.path.join(ROOT_DIR, "novels")

_ILLEGAL_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# 模型档位常量（与 schema 中 model_presets.slot 对齐）
# 注意：加档位要**三处同步**——这里、migrate._seed_defaults 的 slots、
# llm/router.SLOT_LABELS；否则新功能一上线就是「档位未配置」。
SLOTS = ("world_sim", "character_decide", "option_gen", "prose_gen",
         "state_extract", "completion_judge", "summarize", "diagnose",
         # v6.7 语义级冲突判定（降级链指向 state_extract）。
         # 事实分类的 LLM 兜底**复用这一档位**，不再新增。
         "conflict_check",
         # v6.8 创建世界时按选定题材生成整份世界草案（降级链指向 world_sim）。
         # 只用在"创建世界"这一步，高频推演仍走 world_sim。
         "world_init")

# 决策触发类型
TRIGGER_TYPES = ("moral", "cost", "irreversible", "user_focus")
DECISION_MODES = ("viewer", "director", "tabletop")

# 章节形态
CHAPTER_SHAPES = ("scene", "montage", "interlude", "aftermath",
                  "ensemble", "transition")


# ============================================================ 工具函数

def _clean(text):
    """统一清洗：None -> ''，去首尾空白，压缩连续空行。"""
    if text is None:
        return ""
    s = str(text).replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _truthy(value):
    """宽容布尔判定：模型常给 "true" / "是" / 1 / True 混着来。

    口径与 _cast_state 的 bool 分支、server.py 的路由参数解析保持一致，
    别再各写一套（写两遍就会出现"这边算真、那边算假"的分裂）。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in ("1", "true", "yes", "y", "是", "真")


def _sanitize_filename(name):
    s = _ILLEGAL_CHARS.sub("_", str(name or "")).strip().strip(".")
    return s or "untitled"


def _count_words(text):
    """中文字数口径：与旧系统保持一致，按字符数计。"""
    return len(_clean(text))


def _jloads(raw, default):
    """宽容 JSON 解析：失败返回 default，避免脏数据炸掉调用方。"""
    if raw is None or raw == "":
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


def _jdumps(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def json_string_reader(col):
    """流式读一个 JSON 对象里的所有字符串值。

    SQLite 没有 json_each 之外的好办法把「一个 JSON 对象里的全部字符串」
    拉成行，而本项目的列类型是混合的：一个字段可能是 `"a"`、可能是
    `["a","b"]`、可能是 `{"k":"v"}`，还可能是 `[{"k":"v"}]`。
    与其在 SQL 里写一堆 json_type 分支，不如把行取回来在 Python 里走一遍——
    这里只对**一个字段**的文本用 json.JSONDecoder.raw_decode 扫，比正则可靠。

    用途：导入技能包后，去**项目自己的库**里查有没有哪本书引用过它。
    """
    def reader(raw):
        if raw is None or raw == "":
            return
        s = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)
        if col not in s:
            return                       # 关键字都不在，省掉解析开销
        dec = json.JSONDecoder()
        i, n = 0, len(s)
        while i < n:
            ch = s[i]
            if ch == '"':
                try:
                    v, j = dec.raw_decode(s, i)
                except ValueError:
                    i += 1
                    continue
                if isinstance(v, str):
                    yield v
                i = j
            else:
                i += 1
    return reader


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ============================================================ 枚举收口
# schema 里带 CHECK 的列（rank / event_type / category / importance / goal_type …）
# 有一半的值是**模型给的自由文本**。模型很爱写 "S"、"主角"、"情绪"、"medium"、
# "long_term"——这些看起来合理，撞上 CHECK 就是 IntegrityError → 整个请求 500，
# 而且多行写入会留下"半截阵容"。所以在写库这最后一站统一收口：
# 能认的认（别名 / 大小写 / 前缀），认不出的一律落到安全默认值，绝不抛异常。

RANKS = ("A", "B", "C", "D", "E")
GOAL_TYPES = ("short", "long")
EVENT_TYPES = ("plot", "character", "world", "conflict", "reveal",
               "decision", "transition", "other",
               # v6.7：**作者的元层操作**（正史修正），不是剧情内发生的事。
               # 与 reveal 的区别在"谁产生 / 该不该进正文装配"：
               #   reveal        → 剧情推进，必须写进正文
               #   canon_revision→ 设定被改，**必须被 prose.build_events_block()
               #                   过滤掉**（否则第 20 章会写一段"作者修正了设定"）
               "canon_revision")
CHARACTER_STATUSES = ("alive", "dead", "missing", "retired", "unknown")
CONTROL_MODES = ("ai", "user", "auto_delegate")
STATE_TYPES = ("text", "int", "float", "bool", "json")
STATE_CATEGORIES = ("tension", "resource", "threat", "relation", "progress", "other")
THREAD_IMPORTANCE = ("critical", "high", "normal", "low")

# ---- v6.7 新增枚举（每一项都对应 schema 里的一个 CHECK）----
# 列在这里的理由只有一个：**这些列的值有一半来自模型自由文本**，
# 不在写库前归一，撞上 CHECK 就是 IntegrityError → 整个请求 500。
FACT_STATUSES = ("canonical", "derived", "provisional")
KNOW_TYPES = ("know", "believe", "suspect", "assume", "memory", "dream")
KNOW_SOURCES = ("witnessed", "told", "inferred", "assumed", "read", "public",
                "dream", "recalled")
KNOW_ABOUT_TYPES = ("character", "entity", "event", "location", "fact", "world")
KNOW_STATUSES = ("active", "confirmed", "refuted", "revoked")
GOAL_LIFECYCLES = ("dormant", "active", "at_risk", "achieved", "failed",
                   "transformed")
CLASSIFICATIONS = ("world", "cognition", "unverified", "dream", "memory",
                   "author_claim")
CONFLICT_TYPES = ("prose_contradicts_fact", "prose_invents_fact",
                  "fact_missing_in_prose", "knowledge_leak")
CONFLICT_STATUSES = ("open", "keep_prose", "keep_canon", "ignored")
RULE_SCOPES = ("global", "character", "entity", "location", "faction")
RULE_SEVERITIES = ("error", "warning")
BRANCH_STATUSES = ("open", "active", "committed", "discarded")
CANDIDATE_STATUSES = ("pending", "accepted", "rejected", "converted")
# facts.fact_type / fact_candidates.fact_type / world_rules 共用同一套取值。
# **唯一事实来源在这里**（收口区），engine/facts.py 从本模块 import，
# 两处各写一份的话，schema 改了 CHECK 而代码没跟上就是 IntegrityError。
FACT_TYPES = ("identity", "status", "location", "possession", "relationship",
              "ability", "injury", "resource", "world_rule", "outcome", "other")
# 一条事实"是谁说的"。init=世界初始化，sim=推演，prose=正文回流，user=作者手改。
SOURCE_KINDS = ("sim", "prose", "user", "init")
CLASSIFIED_BY = ("sim", "python", "llm", "user")
# 元层事件：**作者对设定的操作**，不是故事里发生的事。
# 它必须被"装配给模型的每一处事件流"过滤掉 —— 否则第 20 章会写出一段
# "作者修正了设定"（约束 3 的落点）。见 prose.build_events_block / world._ctx_brief。
META_EVENT_TYPES = ("canon_revision",)

_RANK_ALIAS = {
    "S": "A", "SS": "A", "SSS": "A", "SR": "A", "R": "B",
    "主角": "A", "核心": "A", "主要": "A", "重要": "A", "关键": "A", "一线": "A",
    "重要配角": "B", "次主角": "B", "二线": "B",
    "配角": "C", "三线": "C", "普通": "C",
    "次要": "D", "四线": "D",
    "龙套": "E", "路人": "E", "背景": "E", "群众": "E",
}
_EVENT_TYPE_ALIAS = {
    "过渡": "transition", "转场": "transition", "衔接": "transition",
    "揭示": "reveal", "真相": "reveal", "伏笔": "reveal",
    "冲突": "conflict", "对抗": "conflict", "矛盾": "conflict",
    "情节": "plot", "剧情": "plot", "主线": "plot",
    "人物": "character", "角色": "character", "成长": "character",
    "世界": "world", "环境": "world", "设定": "world",
    "决定": "decision", "决策": "decision", "选择": "decision",
    "其他": "other", "其它": "other",
    # v6.7：正史修正的别名一律收口到 canon_revision。
    # 只收别名、不新增枚举值——否则库里会同时躺着 canon_revision 与
    # canon_update 两个近义值，查询与展示都要写两遍。
    "正史修正": "canon_revision", "正史更新": "canon_revision",
    "正史变更": "canon_revision", "正史修订": "canon_revision",
    "设定修正": "canon_revision", "作者修正": "canon_revision",
    "canon_update": "canon_revision", "canon_revise": "canon_revision",
    "canon revision": "canon_revision",
}
_KNOW_TYPE_ALIAS = {
    "知道": "know", "确认": "know", "亲眼": "know", "目睹": "know",
    "相信": "believe", "认为": "believe", "以为": "believe",
    "怀疑": "suspect", "猜疑": "suspect",
    "假设": "assume", "假定": "assume", "暂时": "assume",
    "回忆": "memory", "记得": "memory", "想起": "memory", "记忆": "memory",
    "梦": "dream", "梦见": "dream", "梦境": "dream", "梦中": "dream",
}
_KNOW_SOURCE_ALIAS = {
    "亲眼": "witnessed", "目睹": "witnessed", "亲历": "witnessed",
    "听说": "told", "转述": "told", "被告知": "told",
    "推断": "inferred", "推测": "inferred",
    "假定": "assumed", "假设": "assumed",
    "阅读": "read", "读到": "read", "看过": "read",
    "公开": "public", "众所周知": "public",
    "梦": "dream", "梦境": "dream",
    "回忆": "recalled", "想起": "recalled", "记得": "recalled",
}
_CLASSIFY_ALIAS = {
    "世界": "world", "真相": "world", "事实": "world",
    "认知": "cognition", "角色认知": "cognition", "误会": "cognition",
    "未证实": "unverified", "存疑": "unverified", "不确定": "unverified",
    "梦": "dream", "梦境": "dream",
    "回忆": "memory", "记忆": "memory", "不可靠叙述": "memory",
    "声称": "author_claim", "转述": "author_claim", "据说": "author_claim",
    "对话": "author_claim",
}
_LIFECYCLE_ALIAS = {
    "休眠": "dormant", "潜伏": "dormant",
    "进行中": "active", "活跃": "active",
    "受阻": "at_risk", "危险": "at_risk", "岌岌可危": "at_risk",
    "达成": "achieved", "完成": "achieved",
    "失败": "failed",
    "转化": "transformed", "演变": "transformed",
}
_CONFLICT_TYPE_ALIAS = {
    "与正史冲突": "prose_contradicts_fact", "矛盾": "prose_contradicts_fact",
    "正文新增": "prose_invents_fact", "正史没有": "prose_invents_fact",
    "漏写": "fact_missing_in_prose", "未写到": "fact_missing_in_prose",
    "认知泄漏": "knowledge_leak", "信息越界": "knowledge_leak",
}
_RULE_SCOPE_ALIAS = {
    "全局": "global", "世界": "global",
    "角色": "character", "人物": "character",
    "实体": "entity", "组织": "entity",
    "地点": "location", "地方": "location",
    "势力": "faction", "阵营": "faction",
}
_STATE_TYPE_ALIAS = {
    "number": "float", "数值": "float", "数字": "float",
    "整数": "int", "整数型": "int",
    "小数": "float", "浮点": "float", "浮点数": "float",
    "布尔": "bool", "是否": "bool", "boolean": "bool",
    "对象": "json", "数组": "json", "列表": "json", "结构": "json",
    "object": "json", "array": "json",
    "字符串": "text", "文本": "text", "string": "text",
}
_STATE_CATEGORY_ALIAS = {
    "情绪": "other", "情感": "other", "氛围": "other",
    "关系": "relation", "人情": "relation", "人际": "relation",
    "紧张": "tension", "压力": "tension", "局势": "tension",
    "资源": "resource", "物资": "resource", "钱": "resource",
    "威胁": "threat", "危险": "threat", "敌人": "threat",
    "进度": "progress", "进展": "progress",
    "其他": "other", "其它": "other",
}
_THREAD_IMP_ALIAS = {
    "medium": "normal", "一般": "normal", "中": "normal", "普通": "normal",
    "normal": "normal",
    "高": "high", "重要": "high", "high": "high",
    "关键": "critical", "致命": "critical", "critical": "critical",
    "低": "low", "次要": "low", "low": "low",
}
_GOAL_TYPE_ALIAS = {
    "长期": "long", "远期": "long", "long_term": "long", "longterm": "long",
    "短期": "short", "近期": "short", "short_term": "short", "shortterm": "short",
}
_CONTROL_ALIAS = {
    "用户": "user", "人工": "user", "手动": "user",
    "自动": "ai", "托管": "ai", "机器": "ai",
    "委托": "auto_delegate", "自动委托": "auto_delegate",
}
_IMPORTANCE_WORDS = {
    "critical": 5, "urgent": 5, "关键": 5, "致命": 5, "极高": 5, "最高": 5,
    "high": 4, "重要": 4, "高": 4, "较高": 4,
    "normal": 3, "medium": 3, "中": 3, "普通": 3, "一般": 3,
    "low": 2, "低": 2, "次要": 2, "较低": 2,
    "trivial": 1, "很低": 1, "最低": 1,
}

# ---------------------------------------------------------------- 地点树（v6.4）
#
# 起因（用户）："空间地图能不能做成——有剧情发生的空间就详细到什么房间，
# 没有剧情发生的，可以模糊到某个建筑或者某个城市。"
#
# 所以位置是**分级**的，精度由"剧情演到哪"决定：
#   旧城区(district) → 调查局本部(building) → 地下二层(floor) → 封锁库(room)
# 没演到的层级不编。current_location 那句自由文本保留原始措辞，
# location_id 指向树上的节点，两者并存。
_LOCATION_LEVELS = ("", "city", "district", "building", "floor", "room",
                    "site", "other")

# 角色位置精度比地点层级多一个 "unknown"：
# characters.location_precision 的默认值就是 unknown（"还不知道他在哪"）。
# 地点节点自身不需要 unknown —— 一个真实存在的节点怎么可能层级未知？
# 两个枚举长得像但是两码事，别合并（合了 _norm_enum 会把 unknown 当非法值
# 收口成 ""，角色的"位置不明"就静默变成"空字符串"）。
_LOCATION_PRECISIONS = ("unknown", "city", "district", "building", "floor",
                        "room", "site", "other")

# 自由文本切成层级的分隔符。模型写"本部 茶水间""本部·茶水间""本部/茶水间"
# 都有，全半角空格、中点、斜杠、箭头一起收。
_LOCATION_SPLIT_RE = re.compile(r"[ \u3000·／/>＞\-—]+")

# 层级猜测：从"名"反推它大概是城市/建筑/房间哪一级。
# 猜错不算错——只是给地图分组用，真正的父子关系由解析时的顺序决定。
_LEVEL_KEYWORDS = (
    ("city", ("市", "城", "都", "州", "郡", "国", "王国", "帝国")),
    ("district", ("区", "街区", "巷", "街", "坊", "镇", "乡", "郊", "港")),
    ("building", ("本部", "总部", "大楼", "大厦", "楼", "馆", "院", "所", "站",
                  "基地", "庄园", "宅", "府", "庙", "寺", "观", "塔", "仓",
                  "厂", "校", "学园", "医院", "议会", "总局", "分局")),
    ("floor", ("层", "楼", "地下", "顶层", "天台", "阁楼", "地下室")),
    ("room", ("室", "间", "房", "厅", "堂", "库", "牢", "狱", "店", "铺",
              "台", "间室", "洗手间", "厕所", "走廊", "办公室", "会议室")),
    ("site", ("遗址", "废墟", "坟", "墓", "洞穴", "洞", "林", "场", "广场",
              "码头", "野外", "荒原", "沙漠", "森林")),
)

# "故意不说的位置"。这些词不该在树上变成节点名——它们的语义就是
# "没有可指认的地点"，落成 other 后地图上显示为"行踪不明"。
_VAGUE_LOCATIONS = frozenset((
    "某处", "某地", "某处地方", "不明", "未知", "未知处", "行踪不明",
    "远方", "远处", "别处", "他处", "各地", "四处", "不明地点",
    "unknown", "elsewhere", "somewhere", "nowhere",
))


def _guess_location_level(name, last=False):
    """按名字猜地点的层级；猜不出按位置给默认（末段多半是房间，首段可能是城市）。

    "某处/远方/不知何处" 这类词是有意义的：它表达了"叙事故意不告诉你位置"。
    判成 room 会让地图上凭空多出一间叫"某处"的屋子，所以统一归 other——
    other 在地图上就是"位置不明"，那正是它该有的样子。
    """
    s = _clean(name)
    if not s:
        return "other"
    if s in _VAGUE_LOCATIONS:
        return "other"
    for level, kws in _LEVEL_KEYWORDS:
        for kw in kws:
            if kw in s:
                return level
    return "room" if last else "other"


def _norm_enum(value, allowed, default, alias=None):
    """把自由文本收进枚举：原值 → 小写/去空白 → 别名 → 前缀匹配 → 默认值。"""
    s = _clean(value)
    if not s:
        return default
    if s in allowed:
        return s
    low = s.lower().replace(" ", "_").replace("-", "_")
    if low in allowed:
        return low
    for table in (alias or {},):
        if s in table:
            return table[s]
        if low in table:
            return table[low]
    for a in allowed:                       # "long_term" / "A级" / "PLOT型"
        if low.startswith(a):
            return a
    return default


def _norm_rank(value, default="C"):
    s = _clean(value)
    if s.upper() in RANKS:
        return s.upper()
    if s in _RANK_ALIAS:
        return _RANK_ALIAS[s]
    for ch in s.upper():                    # "A级" / "a-配角"
        if ch in RANKS:
            return ch
    return default


def _to_int(value, default, lo=None, hi=None):
    """宽容取整：'5' / '5.0' / 5 / '高' / '' 都不抛异常。"""
    if isinstance(value, str) and value.strip().lower() in _IMPORTANCE_WORDS:
        n = _IMPORTANCE_WORDS[value.strip().lower()]
    else:
        try:
            n = int(float(str(value).strip()))
        except (TypeError, ValueError):
            n = default
    if lo is not None:
        n = max(lo, n)
    if hi is not None:
        n = min(hi, n)
    return n


def _norm_importance(value, default=3):
    return _to_int(value, default, 1, 5)


# ---- v6.7 新增收口（一一对应上面新增的枚举元组）----

def _norm_know_type(value):
    return _norm_enum(value, KNOW_TYPES, "know", _KNOW_TYPE_ALIAS)


def _norm_know_source(value):
    return _norm_enum(value, KNOW_SOURCES, "witnessed", _KNOW_SOURCE_ALIAS)


def _norm_lifecycle(value):
    return _norm_enum(value, GOAL_LIFECYCLES, "active", _LIFECYCLE_ALIAS)


def _norm_classification(value):
    # 默认 'unverified' 而**不是** 'world'：判不准时宁可挂起等裁决。
    # 反过来（默认 world）等于"拿不准就写进正史"，正是约束 2 要堵的洞。
    return _norm_enum(value, CLASSIFICATIONS, "unverified", _CLASSIFY_ALIAS)


def _norm_conflict_type(value):
    return _norm_enum(value, CONFLICT_TYPES, "prose_invents_fact",
                      _CONFLICT_TYPE_ALIAS)


def _norm_rule_scope(value):
    return _norm_enum(value, RULE_SCOPES, "global", _RULE_SCOPE_ALIAS)


# `world_state` 的**唯一**写权限白名单（约束 4）。
# 只有一个名字，且这个名字只出现在 applier.apply_projection 里。
# facts.write_state_projection() 不直接调 set_state_value——它转调 apply_projection，
# 于是"唯一落库点"这条铁律没有第二个例外。
_STATE_WRITERS = ("applier.apply_projection",)


# ============================================================ 主类

class NovelDatabase:
    """v6.7 数据访问层。"""

    def __init__(self, db_path=DEFAULT_DB, auto_migrate=False):
        self.db_path = os.path.abspath(db_path)
        if auto_migrate:
            from migrate import migrate as _mig
            _mig(self.db_path, verbose=False)
        if not os.path.exists(self.db_path):
            raise FileNotFoundError(
                "数据库不存在：%s\n请先运行：python src_v6/core/migrate.py" % self.db_path
            )
        self._conn = None
        self._connect()

    # -------------------------------------------------- 连接管理

    def _connect(self):
        con = sqlite3.connect(self.db_path, timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("PRAGMA busy_timeout = 15000")
        self._conn = con
        return con

    @property
    def conn(self):
        if self._conn is None:
            self._connect()
        return self._conn

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

    @contextmanager
    def tx(self):
        """显式事务：异常回滚。**支持嵌套**。

        一律用 SAVEPOINT，而不是 BEGIN/commit：
          - 嵌套时内层若 commit()，会把外层没写完的东西一起提交，外层再抛异常
            就回滚不掉了——"半截阵容 / 半截事件批次"就是这么来的；
          - SAVEPOINT 不依赖 sqlite3 模块的隐式 BEGIN 时机，最外层 RELEASE
            才真正提交，语义简单可控。
        """
        con = self.conn
        con.execute("SAVEPOINT nw_sp")
        try:
            yield con
            con.execute("RELEASE nw_sp")
        except Exception:
            try:
                con.execute("ROLLBACK TO nw_sp")
                con.execute("RELEASE nw_sp")
            except sqlite3.Error:
                con.rollback()          # savepoint 都没了，退回整库回滚
            raise

    # -------------------------------------------------- 基础查询原语

    def query(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def one(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None

    def scalar(self, sql, params=(), default=None):
        cur = self.conn.execute(sql, params)
        row = cur.fetchone()
        if row is None:
            return default
        val = row[0]
        return default if val is None else val

    def execute(self, sql, params=()):
        """执行写操作并提交，返回 lastrowid。"""
        with self.tx() as con:
            cur = con.execute(sql, params)
            return cur.lastrowid

    # ================================================== 小说 / 世界总览

    def list_novels(self, include_archived=True):
        cond = "" if include_archived else "WHERE status != 'archived' "
        return self.query(
            "SELECT * FROM v_novel_overview " + cond.replace("status", "n.status")
            if False else
            "SELECT n.*, "
            "(SELECT COUNT(*) FROM chapters c WHERE c.novel_id=n.id AND c.deleted_at IS NULL) AS chapter_count, "
            "(SELECT COALESCE(SUM(c.word_count),0) FROM chapters c WHERE c.novel_id=n.id AND c.deleted_at IS NULL) AS total_words, "
            "(SELECT COUNT(*) FROM characters ch WHERE ch.novel_id=n.id AND ch.deleted_at IS NULL) AS character_count, "
            "(SELECT COUNT(*) FROM world_entities e WHERE e.novel_id=n.id AND e.deleted_at IS NULL) AS entity_count, "
            "(SELECT COUNT(*) FROM events ev WHERE ev.novel_id=n.id AND ev.deleted_at IS NULL) AS event_count, "
            "(SELECT COUNT(*) FROM foreshadowing f WHERE f.novel_id=n.id AND f.status='active') AS open_thread_count, "
            "(SELECT COUNT(*) FROM decision_points d WHERE d.novel_id=n.id AND d.resolved=0) AS pending_decision_count "
            "FROM novels n WHERE n.deleted_at IS NULL ORDER BY n.updated_at DESC"
        )

    def get_novel(self, novel_id):
        return self.one("SELECT * FROM novels WHERE id=? AND deleted_at IS NULL",
                        (novel_id,))

    def create_novel(self, title, genre="", theme="", style="", tone="",
                     premise="", initial_tension="", synopsis="",
                     target_chapters=0, target_words_per_chapter=3000,
                     decision_mode="director", completion_mode="ai"):
        """创建小说并初始化世界时钟（v6：不再有 strand_tracker 初始化）。"""
        with self.tx() as con:
            cur = con.execute(
                "INSERT INTO novels(title, genre, theme, style, tone, premise, "
                "initial_tension, synopsis, target_chapters, target_words_per_chapter, "
                "decision_mode, completion_mode, status) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?, 'draft')",
                (title.strip(), genre, theme, style, tone, premise,
                 initial_tension, synopsis, target_chapters,
                 target_words_per_chapter, decision_mode, completion_mode),
            )
            novel_id = cur.lastrowid
            # 世界时钟初始化：时间为空，等待用户设定或首次推演生成
            con.execute(
                "INSERT INTO world_clock(novel_id, current_time, granularity) "
                "VALUES (?, '', 'scene')", (novel_id,)
            )
        return self.get_novel(novel_id)

    def update_novel(self, novel_id, **fields):
        allowed = {"title", "genre", "theme", "style", "tone", "premise",
                   "initial_tension", "synopsis", "current_chapter",
                   "target_chapters", "target_words_per_chapter", "status",
                   "decision_mode", "completion_mode", "auto_advance"}
        sets, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append("%s=?" % k)
                vals.append(v)
        if not sets:
            return self.get_novel(novel_id)
        sets.append("updated_at=?")
        vals.extend([_now(), novel_id])
        with self.tx() as con:
            con.execute("UPDATE novels SET %s WHERE id=?" % ", ".join(sets), vals)
        return self.get_novel(novel_id)

    def soft_delete_novel(self, novel_id):
        """归档：从所有列表里隐藏，但数据完整保留，可 restore_novel 恢复。"""
        self.execute(
            "UPDATE novels SET deleted_at=?, status='archived' WHERE id=?",
            (_now(), novel_id),
        )

    def restore_novel(self, novel_id):
        """把归档的小说放回列表。状态统一回到 draft（归档前的状态没有单独记账）。"""
        self.execute(
            "UPDATE novels SET deleted_at=NULL, status='draft' WHERE id=?",
            (novel_id,),
        )
        return self.get_novel(novel_id)

    def list_archived_novels(self):
        """归档箱：deleted_at 非空的小说（含章节数，便于判断是不是重要作品）。"""
        return self.query(
            "SELECT n.id, n.title, n.status, n.updated_at, n.deleted_at, "
            "(SELECT COUNT(*) FROM chapters c WHERE c.novel_id=n.id) AS chapter_count, "
            "(SELECT COALESCE(SUM(c.word_count),0) FROM chapters c "
            " WHERE c.novel_id=n.id) AS total_words "
            "FROM novels n WHERE n.deleted_at IS NOT NULL "
            "ORDER BY n.deleted_at DESC"
        )

    def delete_novel(self, novel_id):
        """物理删除（依赖 ON DELETE CASCADE 清理所有子表）。"""
        self.execute("DELETE FROM novels WHERE id=?", (novel_id,))

    def purge_orphans(self):
        """清理所有"novel_id 指向不存在小说"的残留行。

        正常流程下列级 ON DELETE CASCADE 会处理干净；但以下情况会漏：
          - 早期版本直接裸 SQL 删过 novels
          - 用外部工具（DB Browser 等）关掉 FK 后删除
        这些残留会污染统计与体检，所以提供一键清理。
        注意：novel_id = 0 是"全局哨兵行"（如 model_presets 的全局默认档位），
        不属于孤儿，必须保留。
        """
        con = self.conn
        tables = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")]
        targets = []
        for t in tables:
            cols = [c[1] for c in con.execute("PRAGMA table_info(%s)" % t)]
            if "novel_id" in cols:
                targets.append(t)

        removed = {}
        with self.tx() as c:
            for t in targets:
                cur = c.execute(
                    "DELETE FROM %s WHERE novel_id IS NOT NULL AND novel_id != 0 "
                    "AND novel_id NOT IN (SELECT id FROM novels)" % t)
                if cur.rowcount:
                    removed[t] = cur.rowcount
        return {"removed": removed, "total": sum(removed.values()),
                "broken_left": len(con.execute(
                    "PRAGMA foreign_key_check").fetchall())}

    # ================================================== 属性 / 世界设定

    def get_attributes(self, novel_id):
        rows = self.query(
            "SELECT key, value FROM novel_attributes WHERE novel_id=? ORDER BY key",
            (novel_id,),
        )
        return {r["key"]: r["value"] for r in rows}

    def set_attribute(self, novel_id, key, value):
        self.execute(
            "INSERT INTO novel_attributes(novel_id, key, value) VALUES (?,?,?) "
            "ON CONFLICT(novel_id, key) DO UPDATE SET value=excluded.value, "
            "updated_at=datetime('now','localtime')",
            (novel_id, key, _clean(value)),
        )

    def get_world(self, novel_id, category=None):
        if category:
            return self.query(
                "SELECT * FROM world_settings WHERE novel_id=? AND category=? "
                "ORDER BY importance DESC, id", (novel_id, category))
        return self.query(
            "SELECT * FROM world_settings WHERE novel_id=? "
            "ORDER BY category, importance DESC, id", (novel_id,))

    def add_world(self, novel_id, category, name, content, importance=3):
        return self.execute(
            "INSERT INTO world_settings(novel_id, category, name, content, importance) "
            "VALUES (?,?,?,?,?)",
            (novel_id, category, _clean(name), _clean(content), int(importance)),
        )

    # ================================================== 世界时钟

    def get_clock(self, novel_id):
        row = self.one("SELECT * FROM world_clock WHERE novel_id=?", (novel_id,))
        if not row:
            self.execute(
                "INSERT INTO world_clock(novel_id, current_time, granularity) "
                "VALUES (?, '', 'scene')", (novel_id,))
            row = self.one("SELECT * FROM world_clock WHERE novel_id=?", (novel_id,))
        return row

    def set_clock(self, novel_id, current_time, granularity=None):
        """设定世界起始时间。

        与 advance_clock 的区别：**不计入推进次数**。
        世界初始化时设时间属于"给定起点"，不是"推演了一次"，
        否则 total_ticks 会从一开始就是 1，统计口径失真。
        """
        self.get_clock(novel_id)     # 确保行存在
        sets = ["current_time = ?", "updated_at = ?"]
        vals = [_clean(current_time), _now()]
        if granularity:
            sets.append("granularity = ?")
            vals.append(granularity)
        vals.append(novel_id)
        with self.tx() as con:
            con.execute("UPDATE world_clock SET %s WHERE novel_id=?" % ", ".join(sets), vals)
        return self.get_clock(novel_id)

    def advance_clock(self, novel_id, new_time=None, ticks=1, granularity=None,
                      chapter_ref=None):
        clock = self.get_clock(novel_id)
        sets = ["total_ticks = total_ticks + ?", "updated_at = ?"]
        vals = [int(ticks), _now()]
        if new_time is not None:
            sets.append("current_time = ?")
            vals.append(_clean(new_time))
        if granularity:
            sets.append("granularity = ?")
            vals.append(granularity)
        if chapter_ref is not None:
            sets.append("last_chapter = ?")
            vals.append(int(chapter_ref))
        vals.append(novel_id)
        with self.tx() as con:
            con.execute("UPDATE world_clock SET %s WHERE novel_id=?" % ", ".join(sets), vals)
        return self.get_clock(novel_id)

    # ================================================== 世界实体

    def list_entities(self, novel_id, entity_type=None, status=None,
                      include_deleted=False):
        sql = "SELECT * FROM world_entities WHERE novel_id=?"
        params = [novel_id]
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        if entity_type:
            sql += " AND entity_type=?"
            params.append(entity_type)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY power_level DESC, id"
        rows = self.query(sql, params)
        for r in rows:
            r["attributes"] = _jloads(r.get("attributes"), {})
        return rows

    def get_entity(self, entity_id):
        row = self.one("SELECT * FROM world_entities WHERE id=?", (entity_id,))
        if row:
            row["attributes"] = _jloads(row.get("attributes"), {})
        return row

    def find_entity(self, novel_id, name, entity_type=None):
        if entity_type:
            return self.one(
                "SELECT * FROM world_entities WHERE novel_id=? AND entity_type=? AND name=?",
                (novel_id, entity_type, name))
        return self.one(
            "SELECT * FROM world_entities WHERE novel_id=? AND name=?",
            (novel_id, name))

    def add_entity(self, novel_id, entity_type, name, description="",
                   status="active", power_level=3, visibility="public",
                   attributes=None, first_appear_chapter=0,
                   parent_id=None, location_level="", is_secret=False):
        # v6.4：地点型实体可以带父子关系与层级。非 location 实体这两个字段
        # 只是被忽略（保持 NULL / ''），不会污染 entity 的语义。
        if entity_type != "location":
            parent_id, location_level, is_secret = None, "", False
        if location_level not in _LOCATION_LEVELS:
            location_level = ""
        return self.execute(
            "INSERT INTO world_entities(novel_id, entity_type, name, description, "
            "status, power_level, visibility, attributes, first_appear_chapter, "
            "parent_id, location_level, is_secret) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (novel_id, entity_type, _clean(name), _clean(description), status,
             int(power_level), visibility, _jdumps(attributes or {}),
             int(first_appear_chapter),
             int(parent_id) if parent_id else None,
             location_level, 1 if is_secret else 0),
        )

    def update_entity(self, entity_id, **fields):
        allowed = {"name", "description", "status", "power_level", "visibility",
                   "first_appear_chapter", "entity_type",
                   # v6.4 地点树：实体可以挂在父地点下，并标明自己是哪一级
                   "parent_id", "location_level", "is_secret"}
        sets, vals = [], []
        for k, v in fields.items():
            if k == "attributes":
                sets.append("attributes=?")
                vals.append(_jdumps(v) if not isinstance(v, str) else v)
            elif k in allowed:
                # 枚举列收口：模型/正文回流都会往这写自由文本
                if k == "location_level":
                    v = _norm_enum(v, _LOCATION_LEVELS, "other")
                elif k == "is_secret":
                    v = 1 if _truthy(v) else 0
                sets.append("%s=?" % k)
                vals.append(v)
        if not sets:
            return self.get_entity(entity_id)
        sets.append("updated_at=?")
        vals.extend([_now(), entity_id])
        with self.tx() as con:
            con.execute("UPDATE world_entities SET %s WHERE id=?" % ", ".join(sets), vals)
        return self.get_entity(entity_id)

    def merge_entity_attributes(self, entity_id, patch):
        """open-schema：字段按需生长，不存在则新增。"""
        ent = self.get_entity(entity_id)
        if not ent:
            return None
        attrs = ent.get("attributes") or {}
        attrs.update(patch or {})
        return self.update_entity(entity_id, attributes=attrs)

    # ------------------------------------------------ 地点树（v6.4）

    def add_location(self, novel_id, name, parent_id=None, level="other",
                     description="", is_secret=False, first_appear_chapter=0):
        """建一个地点节点。已经存在同名地点时返回它（幂等）。"""
        if level not in _LOCATION_LEVELS:
            level = "other"
        exist = self.one(
            "SELECT * FROM world_entities WHERE novel_id=? AND entity_type='location' "
            "AND name=?", (novel_id, _clean(name)))
        if exist:
            # 补挂父节点：先建的"茶水间"当时可能还不知道它属于本部
            patch = {}
            if parent_id and not exist.get("parent_id"):
                patch["parent_id"] = int(parent_id)
            if level != "other" and (exist.get("location_level") or "") in ("", "other"):
                patch["location_level"] = level
            if is_secret and not exist.get("is_secret"):
                patch["is_secret"] = 1
            if patch:
                self.update_entity(exist["id"], **patch)
            return exist["id"]
        return self.execute(
            "INSERT INTO world_entities(novel_id, entity_type, name, description, "
            "parent_id, location_level, is_secret, first_appear_chapter) "
            "VALUES (?,'location',?,?,?,?,?,?)",
            (novel_id, _clean(name), _clean(description),
             int(parent_id) if parent_id else None, level,
             1 if is_secret else 0, int(first_appear_chapter)),
        )

    def list_locations(self, novel_id, include_deleted=False):
        return self.list_entities(novel_id, entity_type="location",
                                  include_deleted=include_deleted)

    def location_children(self, novel_id, parent_id=None):
        if parent_id:
            return self.query(
                "SELECT * FROM world_entities WHERE novel_id=? AND entity_type='location' "
                "AND parent_id=? AND deleted_at IS NULL ORDER BY id",
                (novel_id, int(parent_id)))
        return self.query(
            "SELECT * FROM world_entities WHERE novel_id=? AND entity_type='location' "
            "AND parent_id IS NULL AND deleted_at IS NULL ORDER BY id", (novel_id,))

    def location_path(self, location_id, max_depth=8):
        """从根到该节点的路径（含自身）。带环保护——parent_id 是自引用，
        脏数据（A 的父是 B、B 的父是 A）会让上溯死循环。"""
        out, seen, cur = [], set(), location_id
        while cur and len(out) < max_depth and cur not in seen:
            seen.add(cur)
            row = self.get_entity(cur)
            if not row:
                break
            out.append(row)
            cur = row.get("parent_id")
        out.reverse()
        return out

    def location_path_text(self, location_id, sep=" · "):
        return sep.join(r["name"] for r in self.location_path(location_id))

    def ensure_location_path(self, novel_id, text, chapter_ref=0):
        """把"调查局本部 茶水间"这类自由文本解析成地点树，返回叶节点 id。

        分隔符按常见写法切：空格、中点、斜杠、大于号、全角空格。
        逐段向下找/建节点——这就是"地点树随叙事生长"的落点：
        模型只在事件里写一句"本部的茶水间"，树自己长出 本部 → 茶水间 两级。

        解析不出（整串为空）时返回 (None, "")。
        返回 (leaf_id, path_text)。
        """
        raw = str(text or "").strip()
        if not raw:
            return None, ""
        # 整串就是"某处/行踪不明"时，别在树上捏一个同名节点：
        # 位置不明是**信息**，不是**地点**。返回 (None, 原文)，
        # 调用方会只更新展示文本、精度记 other。
        if raw in _VAGUE_LOCATIONS:
            return None, raw
        parts = [p.strip() for p in _LOCATION_SPLIT_RE.split(raw) if p.strip()]
        if not parts:
            return None, ""
        # 逐段过滤掉"某处"这类虚词：不能因为它们污染父子链
        parts = [p for p in parts if p not in _VAGUE_LOCATIONS]
        if not parts:
            return None, raw
        # "本部 茶水间" 这种写法里第一段可能带地名后缀，交给 level 猜测
        parent = None
        for i, seg in enumerate(parts):
            level = _guess_location_level(seg, last=(i == len(parts) - 1))
            found = self.one(
                "SELECT * FROM world_entities WHERE novel_id=? AND entity_type='location' "
                "AND name=? AND parent_id IS ?",
                (novel_id, seg, parent))
            if found:
                lid = found["id"]
            else:
                lid = self.add_location(novel_id, seg, parent_id=parent,
                                        level=level, first_appear_chapter=chapter_ref)
            parent = lid
        return parent, self.location_path_text(parent)

    def update_character_location(self, character_id, location_id=None,
                                  location_text=None, precision=None,
                                  world_time="", chapter_ref=0,
                                  event_id=None, is_secret=False,
                                  record_history=True, novel_id=None):
        """更新角色位置：结构化 id + 展示文本 + 精度，并记一条轨迹。

        location_id 为空时只更新文本（降级路径：解析不出地点树也不该报错，
        地图上显示不了这个人，但正文和简报照样能用那句文本）。
        """
        ch = self.get_character(character_id)
        if not ch:
            return None
        nid = novel_id or ch["novel_id"]
        patch = {}
        if location_text is not None:
            patch["current_location"] = _clean(location_text)
        if location_id is not None:
            patch["location_id"] = int(location_id)
        if precision:
            patch["location_precision"] = precision
        if world_time:
            patch["location_updated_at"] = world_time
        if patch:
            self.update_character(character_id, **patch)
        if record_history and (location_text or location_id):
            self.execute(
                "INSERT INTO location_history(novel_id, character_id, location_id, "
                "location_text, precision, world_time, chapter_ref, event_id, is_secret) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (nid, character_id, int(location_id) if location_id else None,
                 _clean(location_text or ""), precision or "unknown",
                 _clean(world_time or ""), int(chapter_ref),
                 int(event_id) if event_id else None, 1 if is_secret else 0))
        return self.get_character(character_id)

    def list_location_history(self, novel_id, character_id=None, limit=40):
        sql = "SELECT * FROM location_history WHERE novel_id=?"
        params = [novel_id]
        if character_id:
            sql += " AND character_id=?"
            params.append(character_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return self.query(sql, params)

    # ------------------------------------------------ 行踪认知（v6.4）

    def set_location_belief(self, novel_id, character_id, observer_id,
                            believed_location_id=None, believed_text="",
                            precision="unknown", known_since="",
                            chapter_ref=0):
        """记下"观察者以为目标在哪"。同一对 (目标,观察者) 只留一条最新认知。"""
        return self.execute(
            "INSERT INTO location_beliefs(novel_id, character_id, observer_id, "
            "believed_location_id, believed_text, precision, known_since, "
            "chapter_ref, stale) VALUES (?,?,?,?,?,?,?,?,0) "
            "ON CONFLICT(novel_id, character_id, observer_id) DO UPDATE SET "
            "believed_location_id=excluded.believed_location_id, "
            "believed_text=excluded.believed_text, "
            "precision=excluded.precision, "
            "known_since=excluded.known_since, "
            "chapter_ref=excluded.chapter_ref, stale=0",
            (novel_id, int(character_id), int(observer_id),
             int(believed_location_id) if believed_location_id else None,
             _clean(believed_text), precision, _clean(known_since),
             int(chapter_ref)))

    def list_location_beliefs(self, novel_id, character_id=None, observer_id=None):
        sql = ("SELECT b.*, c.name AS character_name, o.name AS observer_name "
               "FROM location_beliefs b "
               "LEFT JOIN characters c ON c.id = b.character_id "
               "LEFT JOIN characters o ON o.id = b.observer_id "
               "WHERE b.novel_id=?")
        params = [novel_id]
        if character_id:
            sql += " AND b.character_id=?"
            params.append(int(character_id))
        if observer_id:
            sql += " AND b.observer_id=?"
            params.append(int(observer_id))
        sql += " ORDER BY b.id"
        return self.query(sql, params)

    def mark_beliefs_stale(self, novel_id, character_id):
        """目标露过面之后，所有"以为他在别处"的旧认知就该作废。"""
        return self.execute(
            "UPDATE location_beliefs SET stale=1 WHERE novel_id=? AND character_id=?",
            (novel_id, int(character_id)))

    def clear_location_beliefs(self, novel_id, character_id=None):
        if character_id:
            return self.execute(
                "DELETE FROM location_beliefs WHERE novel_id=? AND character_id=?",
                (novel_id, int(character_id)))
        return self.execute("DELETE FROM location_beliefs WHERE novel_id=?", (novel_id,))

    # ================================================== 世界状态

    def get_world_state(self, novel_id, category=None):
        sql = "SELECT * FROM world_state WHERE novel_id=?"
        params = [novel_id]
        if category:
            sql += " AND category=?"
            params.append(category)
        sql += " ORDER BY category, key"
        rows = self.query(sql, params)
        for r in rows:
            # 一律走 _cast_state：模型会把 "34%"、"第十二名" 这类文本
            # 标成 value_type=int，早先这里直接 int(value) 就抛 ValueError，
            # 而它是世界页/推演硬约束/context 的必经之路 —— 一行脏数据能把
            # 整个世界卡死（推演直接 500）。类型只是提示，值才是事实。
            r["parsed"] = self._cast_state(r)
        return rows

    def get_state_value(self, novel_id, key, default=None):
        row = self.one(
            "SELECT * FROM world_state WHERE novel_id=? AND key=?", (novel_id, key))
        if not row:
            return default
        return self._cast_state(row)

    @staticmethod
    def _cast_state(row):
        vt, val = row["value_type"], row["value"]
        if vt == "int":
            try:
                return int(val or 0)
            except ValueError:
                return 0
        if vt == "float":
            try:
                return float(val or 0)
            except ValueError:
                return 0.0
        if vt == "bool":
            return str(val).lower() in ("1", "true", "yes")
        if vt == "json":
            return _jloads(val, None)
        return val

    def set_state_value(self, novel_id, key, value, value_type="text",
                        category="other", note="", chapter_ref=0, log_reason="",
                        *, writer="", derived_from_fact_id=0,
                        projected_at_tick=0, event_id=None, fact_id=None,
                        tick=0):
        """写入/更新世界状态（**投影写入**）。自动记录变更日志。

        ▲ v6.7 约束 4：本方法只接受**投影**写入。
          `writer` 必须等于 `_STATE_WRITERS` 里唯一那个名字
          （`applier.apply_projection`）。这不是文档约定，是代码闸门——
          第一个撞上 PermissionError 的调用方就是违规代码。

          需要改一个值怎么办？
            · 世界页手改 → `set_state_value_manual()`（写 NULL，投影不覆盖）
            · 推演/正文产生的值 → 写一条 fact，让它投影下来
            · 初始化 → `facts.seed_state()`

        ▲ 手工锁定行不覆盖：`derived_from_fact_id IS NULL` 的行是用户手改过的，
          投影一律跳过（"世界状态页改完就丢"的根因就在这里修掉）。
        """
        if writer not in _STATE_WRITERS:
            raise PermissionError(
                "world_state 只接受投影写入（%s），调用方 %r 无权写入。\n"
                "如果你确实需要改一个值：请写一条 fact（facts.add_fact），"
                "让它投影下来；或走世界页的手工编辑"
                "（db.set_state_value_manual，derived_from_fact_id 留 NULL 锁定）。"
                % (_STATE_WRITERS[0], writer))

        old = self.one(
            "SELECT * FROM world_state WHERE novel_id=? AND key=?", (novel_id, key))
        if old is not None and old.get("derived_from_fact_id") is None:
            # 手工锁定：不覆盖。返回现值，让调用方知道"这次没写进去"。
            return self._cast_state(old)
        old_value = old["value"] if old else ""
        # 枚举收口：category / value_type 都可能是模型给的自由文本
        value_type = _norm_enum(value_type, STATE_TYPES, "text", _STATE_TYPE_ALIAS)
        category = _norm_enum(category, STATE_CATEGORIES, "other",
                              _STATE_CATEGORY_ALIAS)
        str_value = _jdumps(value) if value_type == "json" else str(value)
        # 类型与值对不上时以**值**为准：模型经常把 "34%"、"第十二名"、
        # "2008-02-25" 这类文本标成 int。真按 int 存下去，读取端一 int()
        # 就炸（见 get_world_state）。这里先降级，别把地雷埋进库。
        if value_type in ("int", "float"):
            try:
                int(str_value) if value_type == "int" else float(str_value)
            except (TypeError, ValueError):
                try:
                    float(str_value)
                    value_type = "float"
                except (TypeError, ValueError):
                    value_type = "text"
        try:
            tick = int(tick or projected_at_tick or 0)
        except (TypeError, ValueError):
            tick = 0
        with self.tx() as con:
            con.execute(
                "INSERT INTO world_state(novel_id, key, value, value_type, category, "
                "note, chapter_ref, derived_from_fact_id, projected_at_tick) "
                "VALUES (?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(novel_id, key) DO UPDATE SET value=excluded.value, "
                "value_type=excluded.value_type, category=excluded.category, "
                "note=excluded.note, chapter_ref=excluded.chapter_ref, "
                "derived_from_fact_id=excluded.derived_from_fact_id, "
                "projected_at_tick=excluded.projected_at_tick, "
                "updated_at=datetime('now','localtime')",
                (novel_id, key, str_value, value_type, category, _clean(note),
                 int(chapter_ref), derived_from_fact_id, tick),
            )
            if str(old_value) != str_value:
                con.execute(
                    "INSERT INTO world_state_log(novel_id, key, old_value, new_value, "
                    "reason, chapter_ref, event_id, fact_id, tick) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (novel_id, key, str(old_value), str_value,
                     _clean(log_reason) or "state update", int(chapter_ref),
                     event_id, fact_id if fact_id is not None
                     else derived_from_fact_id, tick),
                )
        return self.get_state_value(novel_id, key)

    def set_state_value_manual(self, novel_id, key, value, value_type="text",
                              category="other", note="", chapter_ref=0,
                              log_reason=""):
        """世界页的**手工编辑**入口（约束 4 的"另一条路"）。

        与投影写入的关键差别：`derived_from_fact_id` 留 **NULL**。
        NULL 的语义是"这是人定的值，自动投影不许覆盖"——
        用户在世界上手改过的值，不该被下一章推演冲掉。
        （老行为正好相反：后写覆盖先写、无痕，于是"改完就丢"。）

        锁定不是不可逆的：世界页对锁定的行显示"已锁定"，并给一个
        「恢复自动投影」按钮（`unlock_state_value`）。
        """
        old = self.one(
            "SELECT * FROM world_state WHERE novel_id=? AND key=?", (novel_id, key))
        old_value = old["value"] if old else ""
        value_type = _norm_enum(value_type, STATE_TYPES, "text", _STATE_TYPE_ALIAS)
        category = _norm_enum(category, STATE_CATEGORIES, "other",
                              _STATE_CATEGORY_ALIAS)
        str_value = _jdumps(value) if value_type == "json" else str(value)
        try:
            tick = int(self.scalar(
                "SELECT absolute_tick FROM world_clock WHERE novel_id=?",
                (novel_id,), default=0) or 0)
        except Exception:                       # noqa: BLE001
            tick = 0
        with self.tx() as con:
            con.execute(
                "INSERT INTO world_state(novel_id, key, value, value_type, category, "
                "note, chapter_ref, derived_from_fact_id, projected_at_tick) "
                "VALUES (?,?,?,?,?,?,?,NULL,?) "
                "ON CONFLICT(novel_id, key) DO UPDATE SET value=excluded.value, "
                "value_type=excluded.value_type, category=excluded.category, "
                "note=excluded.note, chapter_ref=excluded.chapter_ref, "
                "derived_from_fact_id=NULL, projected_at_tick=excluded.projected_at_tick, "
                "updated_at=datetime('now','localtime')",
                (novel_id, key, str_value, value_type, category,
                 _clean(note), int(chapter_ref), tick))
            if str(old_value) != str_value:
                con.execute(
                    "INSERT INTO world_state_log(novel_id, key, old_value, new_value, "
                    "reason, chapter_ref, tick) VALUES (?,?,?,?,?,?,?)",
                    (novel_id, key, str(old_value), str_value,
                     _clean(log_reason) or "世界页手工设定", int(chapter_ref), tick))
        return self.get_state_value(novel_id, key)

    def unlock_state_value(self, novel_id, key):
        """解除锁定：把 `derived_from_fact_id` 从 NULL 改回 0（＝接受自动投影）。

        没有这一步，锁定就是一个**只能进不能出**的状态——用户手改一次之后
        这个 key 永远不再跟随推演，而界面上又看不出原因。
        """
        return self.execute(
            "UPDATE world_state SET derived_from_fact_id=0 "
            "WHERE novel_id=? AND key=? AND derived_from_fact_id IS NULL",
            (novel_id, key))

    def is_state_locked(self, novel_id, key):
        row = self.one("SELECT derived_from_fact_id FROM world_state "
                       "WHERE novel_id=? AND key=?", (novel_id, key))
        return bool(row) and row.get("derived_from_fact_id") is None

    def state_history(self, novel_id, key=None, limit=100):
        sql = "SELECT * FROM world_state_log WHERE novel_id=?"
        params = [novel_id]
        if key:
            sql += " AND key=?"
            params.append(key)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return self.query(sql, params)

    # ================================================== 角色

    def list_characters(self, novel_id, rank=None, status=None,
                        include_deleted=False):
        sql = "SELECT * FROM characters WHERE novel_id=?"
        params = [novel_id]
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        if rank:
            sql += " AND rank=?"
            params.append(rank)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY rank, id"
        rows = self.query(sql, params)
        for r in rows:
            r["attributes"] = _jloads(r.get("attributes"), {})
            r["memory"] = _jloads(r.get("memory"), [])
            r["tracker"] = _jloads(r.get("tracker"), {})
        return rows

    def get_character(self, character_id):
        row = self.one("SELECT * FROM characters WHERE id=?", (character_id,))
        if row:
            row["attributes"] = _jloads(row.get("attributes"), {})
            row["memory"] = _jloads(row.get("memory"), [])
            row["tracker"] = _jloads(row.get("tracker"), {})
        return row

    def get_character_by_name(self, novel_id, name):
        row = self.one(
            "SELECT * FROM characters WHERE novel_id=? AND name=? AND deleted_at IS NULL",
            (novel_id, name))
        if row:
            row["attributes"] = _jloads(row.get("attributes"), {})
            row["memory"] = _jloads(row.get("memory"), [])
            row["tracker"] = _jloads(row.get("tracker"), {})
        return row

    def add_character(self, novel_id, name, alias="", rank="C", role_tag="",
                      personality="", background="", speech_style="",
                      appearance="", status="alive", current_location="",
                      decision_tendency="", control_mode="ai",
                      attributes=None, first_appear_chapter=0):
        # 枚举收口：rank 是模型最爱乱写的列（"S" / "主角" / "A级"）
        rank = _norm_rank(rank, "C")
        status = _norm_enum(status, CHARACTER_STATUSES, "alive")
        control_mode = _norm_enum(control_mode, CONTROL_MODES, "ai", _CONTROL_ALIAS)
        return self.execute(
            "INSERT INTO characters(novel_id, name, alias, rank, role_tag, personality, "
            "background, speech_style, appearance, status, current_location, "
            "decision_tendency, control_mode, attributes, first_appear_chapter) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (novel_id, _clean(name), alias, rank, role_tag, personality, background,
             speech_style, appearance, status, current_location, decision_tendency,
             control_mode, _jdumps(attributes or {}), int(first_appear_chapter)),
        )

    def update_character(self, character_id, **fields):
        allowed = {"name", "alias", "rank", "role_tag", "personality", "background",
                   "speech_style", "appearance", "status", "current_location",
                   "decision_tendency", "control_mode", "first_appear_chapter",
                   # v6.4 结构化位置：current_location 只给展示，
                   # 能不能回答"这栋楼里都有谁"全看这三个字段
                   "location_id", "location_precision", "location_updated_at"}
        sets, vals = [], []
        for k, v in fields.items():
            if k in ("attributes", "memory", "tracker"):
                sets.append("%s=?" % k)
                vals.append(_jdumps(v) if not isinstance(v, str) else v)
            elif k in allowed:
                # 枚举列同样收口：模型/正文回流都会往这写自由文本
                if k == "rank":
                    v = _norm_rank(v, "C")
                elif k == "status":
                    v = _norm_enum(v, CHARACTER_STATUSES, "alive")
                elif k == "control_mode":
                    v = _norm_enum(v, CONTROL_MODES, "ai", _CONTROL_ALIAS)
                elif k == "location_precision":
                    v = _norm_enum(v, _LOCATION_PRECISIONS, "unknown")
                sets.append("%s=?" % k)
                vals.append(v)
        if not sets:
            return self.get_character(character_id)
        sets.append("updated_at=?")
        vals.extend([_now(), character_id])
        with self.tx() as con:
            con.execute("UPDATE characters SET %s WHERE id=?" % ", ".join(sets), vals)
        return self.get_character(character_id)

    def append_character_memory(self, character_id, entry, max_items=60):
        """open-schema 记忆流：该角色亲历的关键事件，防止"知道不该知道的"。"""
        ch = self.get_character(character_id)
        if not ch:
            return None
        mem = ch.get("memory") or []
        mem.append(entry if isinstance(entry, dict) else {"text": str(entry)})
        if len(mem) > max_items:
            mem = mem[-max_items:]
        return self.update_character(character_id, memory=mem)

    def nudge_tracker(self, character_id, trait_key, delta, threshold=30,
                      chapter_ref=0):
        """hidden tracker：弱信号累积。

        返回值 {"value":..., "pending":..., "jumped": bool}
        jumped=True 表示累积越过了阈值，调用方应据此触发一次"可见的性格跃迁"。
        """
        ch = self.get_character(character_id)
        if not ch:
            return None
        tracker = ch.get("tracker") or {}
        node = tracker.get(trait_key) or {"value": 0, "pending": 0,
                                          "last_update_chapter": 0}
        node["pending"] = int(node.get("pending", 0) + delta)
        jumped = False
        if abs(node["pending"]) >= threshold:
            node["value"] = max(0, min(100, int(node.get("value", 0) + node["pending"])))
            node["pending"] = 0
            jumped = True
        node["last_update_chapter"] = int(chapter_ref)
        tracker[trait_key] = node
        self.update_character(character_id, tracker=tracker)
        return {"value": node["value"], "pending": node["pending"], "jumped": jumped}

    def mark_character_dead(self, character_id, chapter_ref=0, note=""):
        ch = self.get_character(character_id)
        if ch:
            self.append_character_memory(
                character_id,
                {"chapter": chapter_ref, "text": note or "角色死亡",
                 "kind": "status_change"})
        return self.update_character(character_id, status="dead")

    # ================================================== 角色目标

    def list_goals(self, character_id=None, novel_id=None, goal_type=None,
                   status="active"):
        sql = "SELECT g.*, c.name AS character_name FROM character_goals g " \
              "LEFT JOIN characters c ON c.id = g.character_id WHERE 1=1"
        params = []
        if character_id:
            sql += " AND g.character_id=?"
            params.append(character_id)
        if novel_id:
            sql += " AND g.novel_id=?"
            params.append(novel_id)
        if goal_type:
            sql += " AND g.goal_type=?"
            params.append(goal_type)
        if status:
            sql += " AND g.status=?"
            params.append(status)
        sql += " ORDER BY g.priority DESC, g.id"
        return self.query(sql, params)

    def add_goal(self, novel_id, character_id, content, goal_type="short",
                 motivation="", obstacle="", priority=3, origin="init",
                 created_chapter=0):
        goal_type = _norm_enum(goal_type, GOAL_TYPES, "short", _GOAL_TYPE_ALIAS)
        origin = _norm_enum(origin, ("init", "ai_generated", "user_added",
                                     "event_driven"), "init")
        priority = _to_int(priority, 3, 1, 5)
        return self.execute(
            "INSERT INTO character_goals(novel_id, character_id, goal_type, content, "
            "motivation, obstacle, priority, origin, created_chapter) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (novel_id, character_id, goal_type, _clean(content), motivation,
             obstacle, int(priority), origin, int(created_chapter)),
        )

    def update_goal(self, goal_id, **fields):
        allowed = {"content", "motivation", "obstacle", "priority", "progress",
                   "status", "settled_chapter", "goal_type", "evolved_from"}
        sets, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                if k == "goal_type":
                    v = _norm_enum(v, GOAL_TYPES, "short", _GOAL_TYPE_ALIAS)
                elif k == "priority":
                    v = _to_int(v, 3, 1, 5)
                sets.append("%s=?" % k)
                vals.append(v)
        if not sets:
            return self.one("SELECT * FROM character_goals WHERE id=?", (goal_id,))
        vals.append(goal_id)
        with self.tx() as con:
            con.execute("UPDATE character_goals SET %s WHERE id=?" % ", ".join(sets), vals)
        return self.one("SELECT * FROM character_goals WHERE id=?", (goal_id,))

    def evolve_goal(self, goal_id, new_content, new_motivation="", new_priority=None):
        """目标演化：旧目标标记 evolved，新目标记录 evolved_from。"""
        old = self.one("SELECT * FROM character_goals WHERE id=?", (goal_id,))
        if not old:
            return None
        self.update_goal(goal_id, status="evolved")
        new_id = self.add_goal(
            old["novel_id"], old["character_id"], new_content,
            goal_type=old["goal_type"], motivation=new_motivation,
            priority=new_priority if new_priority is not None else old["priority"],
            origin="event_driven", created_chapter=old["created_chapter"],
        )
        self.update_goal(new_id, evolved_from=goal_id)
        return self.one("SELECT * FROM character_goals WHERE id=?", (new_id,))

    # ================================================== 角色其他

    def list_relations(self, novel_id, character_id=None):
        sql = ("SELECT r.*, a.name AS name_a, b.name AS name_b "
               "FROM character_relations r "
               "LEFT JOIN characters a ON a.id = r.character_a_id "
               "LEFT JOIN characters b ON b.id = r.character_b_id "
               "WHERE r.novel_id=?")
        params = [novel_id]
        if character_id:
            sql += " AND (r.character_a_id=? OR r.character_b_id=?)"
            params.extend([character_id, character_id])
        sql += " ORDER BY r.intensity DESC, r.id"
        return self.query(sql, params)

    def group_relations(self, novel_id, character_id=None):
        """把关系按"同一对人 + 同一类型"归组，标出是双向还是单向。

        为什么必须归组：双向关系在库里是**两条**记录（A→B 与 B→A，见
        engine.character.apply_relations 的说明），直接遍历会把一对人的
        同一段关系打印两遍；而单向关系只有一条。展示层需要区分这两种，
        否则会出现两个后果：
          1. 双向的被重复展示，模型以为有两条不同的关系；
          2. 单向的被当成对称关系展示，该蒙在鼓里的人变得心知肚明。
        这三处展示（world._ctx_brief / character._relations_block /
        prose.build_relations_block）原来各写一套去重逻辑，其中两处漏了
        方向。统一收口到这里。

        返回 [{'a_id','b_id','a','b','relation_type','mutual',
               'record','records'}]，按 intensity 降序（沿用 list_relations
        的排序，即保持"最要紧的关系排前面"）。
        """
        groups = {}
        order = []
        for r in self.list_relations(novel_id, character_id=character_id):
            key = (tuple(sorted((r["character_a_id"], r["character_b_id"]))),
                   r["relation_type"])
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(r)
        out = []
        for key in order:
            records = groups[key]
            # 以 intensity / id 最小的那条作为代表（list_relations 已按
            # intensity DESC 排序，这里反过来取最"原始"的那条做展示基准）
            base = sorted(records, key=lambda x: x["id"])[0]
            out.append({
                "a_id": base["character_a_id"], "b_id": base["character_b_id"],
                "a": base.get("name_a") or "", "b": base.get("name_b") or "",
                "relation_type": base["relation_type"],
                # 两条 = 双向；一条 = 单向（对方不知情）
                "mutual": len({(x["character_a_id"], x["character_b_id"])
                               for x in records}) >= 2,
                "record": base, "records": records,
            })
        return out

    def add_relation(self, novel_id, a_id, b_id, relation_type="other",
                     description="", intensity=3, updated_chapter=0,
                     status="active"):
        # status 收口：模型/调用方爱给"破裂""已断"这类中文，
        # CHECK 会直接抛 → 一行脏数据废掉整份关系网。
        if status not in ("active", "broken", "evolved"):
            status = "broken" if str(status) in ("破裂", "已断", "断了", "反目") \
                else "active"
        return self.execute(
            "INSERT INTO character_relations(novel_id, character_a_id, character_b_id, "
            "relation_type, description, intensity, status, updated_chapter) "
            "VALUES (?,?,?,?,?,?,?,?) "
            "ON CONFLICT(novel_id, character_a_id, character_b_id, relation_type) "
            "DO UPDATE SET description=excluded.description, "
            "intensity=excluded.intensity, status=excluded.status, "
            "updated_chapter=excluded.updated_chapter",
            (novel_id, a_id, b_id, relation_type, _clean(description),
             int(intensity), status, int(updated_chapter)),
        )

    def list_identities(self, novel_id, character_id=None):
        sql = "SELECT i.*, c.name AS character_name FROM character_identities i " \
              "LEFT JOIN characters c ON c.id = i.character_id WHERE i.novel_id=?"
        params = [novel_id]
        if character_id:
            sql += " AND i.character_id=?"
            params.append(character_id)
        sql += " ORDER BY i.id"
        rows = self.query(sql, params)
        for r in rows:
            r["known_by"] = _jloads(r.get("known_by"), [])
        return rows

    def add_identity(self, novel_id, character_id, identity_name, description="",
                     is_secret=0, known_by=None, from_chapter=0, to_chapter=0):
        return self.execute(
            "INSERT INTO character_identities(novel_id, character_id, identity_name, "
            "description, is_secret, known_by, from_chapter, to_chapter) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (novel_id, character_id, _clean(identity_name), _clean(description),
             int(is_secret), _jdumps(known_by or []), int(from_chapter),
             int(to_chapter)),
        )

    # ================================================== 事件流

    def list_events(self, novel_id, chapter_ref=None, event_type=None,
                    limit=None, include_deleted=False, is_background=None,
                    canonical_only=False, for_prose=False, upto_tick=None):
        """查事件。

        v6.7 新增三个过滤维度（正文装配与推演各自需要不同的切片）：
          `is_background`  None=全部 / 0=只主线 / 1=只背景动向
          `canonical_only` 只取正史事件
          `for_prose`      True 时**排除 `canon_revision`** ——
                           它不是故事里发生的事，写进正文会出现
                           "作者修正了设定"这种元层叙述（约束 3 的落地）。
        """
        sql = "SELECT * FROM events WHERE novel_id=?"
        params = [novel_id]
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        if canonical_only:
            sql += " AND canonical=1"
        if for_prose:
            sql += " AND event_type <> 'canon_revision'"
        if is_background is not None:
            sql += " AND is_background=?"
            params.append(1 if _truthy(is_background) else 0)
        if chapter_ref is not None:
            sql += " AND chapter_ref=?"
            params.append(int(chapter_ref))
        if event_type:
            sql += " AND event_type=?"
            params.append(event_type)
        if upto_tick is not None:
            sql += " AND tick<=?"
            params.append(int(upto_tick))
        sql += " ORDER BY id"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        rows = self.query(sql, params)
        for r in rows:
            r["structured"] = _jloads(r.get("structured"), {})
            r["open_threads"] = _jloads(r.get("open_threads"), [])
            r["involved_characters"] = _jloads(r.get("involved_characters"), [])
            r["involved_entities"] = _jloads(r.get("involved_entities"), [])
        return rows

    def add_event(self, novel_id, title, description="", chapter_ref=0,
                  event_type="plot", involved_characters=None,
                  involved_entities=None, consequences="", importance=3,
                  world_time="", intent="", structured=None, open_threads=None,
                  decision_id=None, sequence=0, canonical=1, immutable=1,
                  branch_id=0, tick=0, is_background=0):
        event_type = _norm_enum(event_type, EVENT_TYPES, "other", _EVENT_TYPE_ALIAS)
        importance = _norm_importance(importance, 3)
        return self.execute(
            "INSERT INTO events(novel_id, chapter_ref, sequence, title, description, "
            "event_type, involved_characters, involved_entities, consequences, "
            "importance, world_time, intent, structured, open_threads, decision_id, "
            "canonical, immutable, branch_id, tick, is_background) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (novel_id, int(chapter_ref), int(sequence), _clean(title),
             _clean(description), event_type,
             _jdumps(involved_characters or []), _jdumps(involved_entities or []),
             _clean(consequences), int(importance), world_time, _clean(intent),
             _jdumps(structured or {}), _jdumps(open_threads or []), decision_id,
             int(_truthy(canonical)), int(_truthy(immutable)),
             int(branch_id or 0), int(tick or 0),
             int(_truthy(is_background))),
        )

    def add_events_bulk(self, novel_id, events):
        """批量写入结构化事件流，返回 id 列表。"""
        ids = []
        with self.tx() as con:
            for idx, ev in enumerate(events):
                cur = con.execute(
                    "INSERT INTO events(novel_id, chapter_ref, sequence, title, "
                    "description, event_type, involved_characters, involved_entities, "
                    "consequences, importance, world_time, intent, structured, "
                    "open_threads, decision_id, canonical, immutable, branch_id, "
                    "tick, is_background) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (novel_id, int(ev.get("chapter_ref", 0)), int(ev.get("sequence", idx)),
                     _clean(ev.get("title", "")), _clean(ev.get("description", "")),
                     _norm_enum(ev.get("event_type"), EVENT_TYPES, "plot",
                                _EVENT_TYPE_ALIAS),
                     _jdumps(ev.get("involved_characters") or []),
                     _jdumps(ev.get("involved_entities") or []),
                     _clean(ev.get("consequences", "")),
                     _norm_importance(ev.get("importance"), 3),
                     ev.get("world_time", ""),
                     _clean(ev.get("intent", "")),
                     _jdumps(ev.get("structured") or {}),
                     _jdumps(ev.get("open_threads") or []),
                     ev.get("decision_id"),
                     int(_truthy(ev.get("canonical", 1))),
                     int(_truthy(ev.get("immutable", 1))),
                     int(ev.get("branch_id") or 0), int(ev.get("tick") or 0),
                     int(_truthy(ev.get("is_background")))),
                )
                ids.append(cur.lastrowid)
        return ids

    def update_event(self, event_id, **fields):
        """改一条事件。

        ▲ v6.7 `immutable=1` 的执行点：**只允许改 `deleted_at` 与 `note`
          两类字段**，其他字段一律抛 ValueError。
          "正史被随手改掉"在代码层就不可能发生——否决一条事件走 `deleted_at`
          （guard.revert_event），不是改它的内容。
        """
        row = self.one("SELECT * FROM events WHERE id=?", (event_id,))
        if not row:
            return None
        allowed = {"deleted_at", "note"}
        if not _truthy(row.get("immutable", 1)):
            allowed |= {"title", "description", "event_type", "importance",
                        "consequences", "intent", "structured"}
        bad = [k for k in fields if k not in allowed]
        if bad:
            raise ValueError(
                "事件 #%s 是不可变正史（immutable=1），不能修改字段：%s。\n"
                "如果这条不该算数：用 guard.revert_event() 作废它"
                "（记 deleted_at，保留审计痕迹）；"
                "如果正史需要订正：写一条 canon_revision 事件 + 一条新 fact。"
                % (event_id, "、".join(bad)))
        sets, params = [], []
        for k, v in fields.items():
            sets.append("%s=?" % k)
            params.append(_jdumps(v) if k == "structured" else v)
        params.append(event_id)
        self.execute("UPDATE events SET %s WHERE id=?" % ", ".join(sets), params)
        return self.one("SELECT * FROM events WHERE id=?", (event_id,))

    def attach_events_to_chapter(self, event_ids, chapter_ref):
        if not event_ids:
            return 0
        marks = ",".join("?" * len(event_ids))
        with self.tx() as con:
            cur = con.execute(
                "UPDATE events SET chapter_ref=? WHERE id IN (%s)" % marks,
                [int(chapter_ref)] + [int(i) for i in event_ids])
            return cur.rowcount

    # ================================================== 悬置线索

    def list_threads(self, novel_id, status="active", min_tension=0):
        sql = "SELECT * FROM foreshadowing WHERE novel_id=?"
        params = [novel_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        if min_tension:
            sql += " AND tension_level>=?"
            params.append(int(min_tension))
        sql += " ORDER BY tension_level DESC, importance DESC, id"
        return self.query(sql, params)

    def add_thread(self, novel_id, title, description="", planted_chapter=0,
                   target_chapter=0, foreshadowing_type="plot",
                   importance="normal", origin="system", tension_level=3):
        importance = _norm_enum(importance, THREAD_IMPORTANCE, "normal",
                                _THREAD_IMP_ALIAS)
        origin = _norm_enum(origin, ("system", "user", "ai_emerged"), "system")
        tension_level = _to_int(tension_level, 3, 1, 5)
        return self.execute(
            "INSERT INTO foreshadowing(novel_id, title, description, planted_chapter, "
            "target_chapter, foreshadowing_type, importance, origin, tension_level) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (novel_id, _clean(title), _clean(description), int(planted_chapter),
             int(target_chapter), foreshadowing_type, importance, origin,
             int(tension_level)),
        )

    def resolve_thread(self, thread_id, chapter_ref, note=""):
        return self.execute(
            "UPDATE foreshadowing SET status='resolved', resolved_chapter=?, "
            "description = CASE WHEN ?='' THEN description "
            "ELSE description || char(10) || ? END WHERE id=?",
            (int(chapter_ref), note, note, thread_id),
        )

    def nudge_thread(self, thread_id, delta=1):
        return self.execute(
            "UPDATE foreshadowing SET tension_level = "
            "MAX(1, MIN(5, tension_level + ?)) WHERE id=?",
            (int(delta), thread_id),
        )

    def urgent_threads(self, novel_id, current_chapter, window=5):
        """target_chapter - current <= window 视为急需回收。"""
        rows = self.query(
            "SELECT * FROM foreshadowing WHERE novel_id=? AND status='active' "
            "AND target_chapter > 0 AND target_chapter - ? <= ? "
            "ORDER BY target_chapter ASC",
            (novel_id, int(current_chapter), int(window)))
        return rows

    # ================================================== 时间线锚点

    def list_timeline(self, novel_id, status=None):
        sql = "SELECT * FROM timeline WHERE novel_id=?"
        params = [novel_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY id"
        return self.query(sql, params)

    def add_timeline(self, novel_id, time_anchor, countdown_name="",
                     countdown_value="", description="", time_span=""):
        return self.execute(
            "INSERT INTO timeline(novel_id, time_anchor, time_span, countdown_name, "
            "countdown_value, description) VALUES (?,?,?,?,?,?)",
            (novel_id, time_anchor, time_span, countdown_name, countdown_value,
             _clean(description)),
        )

    # ================================================== 决策点

    def list_decisions(self, novel_id, resolved=None, chapter_ref=None):
        sql = "SELECT * FROM decision_points WHERE novel_id=?"
        params = [novel_id]
        if resolved is not None:
            sql += " AND resolved=?"
            params.append(1 if resolved else 0)
        if chapter_ref is not None:
            sql += " AND chapter_ref=?"
            params.append(int(chapter_ref))
        sql += " ORDER BY id"
        rows = self.query(sql, params)
        for r in rows:
            r["options"] = _jloads(r.get("options"), [])
            r["impact"] = _jloads(r.get("impact"), {})
        return rows

    def get_decision(self, decision_id):
        row = self.one("SELECT * FROM decision_points WHERE id=?", (decision_id,))
        if row:
            row["options"] = _jloads(row.get("options"), [])
            row["impact"] = _jloads(row.get("impact"), {})
        return row

    def add_decision(self, novel_id, title, situation="", stakes="",
                     trigger_type="moral", actor_type="character", actor_id=None,
                     actor_name="", options=None, allow_freeform=1,
                     chapter_ref=0, note="", branch_id=0, node_id=None):
        return self.execute(
            "INSERT INTO decision_points(novel_id, chapter_ref, trigger_type, title, "
            "situation, stakes, actor_type, actor_id, actor_name, options, "
            "allow_freeform, note, branch_id, node_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (novel_id, int(chapter_ref), trigger_type, _clean(title),
             _clean(situation), _clean(stakes), actor_type, actor_id,
             _clean(actor_name), _jdumps(options or []), 1 if allow_freeform else 0,
             _clean(note), int(branch_id or 0), node_id),
        )

    def resolve_decision(self, decision_id, chosen_option=-1, chosen_content="",
                         chosen_by="user", impact=None):
        if chosen_by not in ("user", "ai_auto", "timeout_default"):
            raise ValueError("chosen_by 必须是 user / ai_auto / timeout_default")
        with self.tx() as con:
            con.execute(
                "UPDATE decision_points SET chosen_option=?, chosen_content=?, "
                "chosen_by=?, resolved=1, resolved_at=?, impact=? WHERE id=?",
                (int(chosen_option), _clean(chosen_content), chosen_by, _now(),
                 _jdumps(impact or {}), decision_id),
            )
        return self.get_decision(decision_id)

    def pending_decisions(self, novel_id=None, limit=None):
        sql = "SELECT * FROM v_pending_decisions WHERE 1=1"
        params = []
        if novel_id:
            sql += " AND novel_id=?"
            params.append(novel_id)
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return self.query(sql, params)

    # ================================================== 剧情树（推演沙盘）

    _STORY_JSON_FIELDS = ("involved_characters", "involved_entities", "payload",
                          "state_delta", "fact_delta", "knowledge_delta")
    _STORY_JSON_DEFAULTS = {"involved_characters": [], "involved_entities": [],
                            "payload": {}, "state_delta": {}, "fact_delta": [],
                            "knowledge_delta": []}

    def _story_row(self, row):
        if not row:
            return row
        for f in self._STORY_JSON_FIELDS:
            row[f] = _jloads(row.get(f), self._STORY_JSON_DEFAULTS.get(f, []))
        return row

    def list_story_nodes(self, novel_id, include_deleted=False, branch_id=None):
        """整棵树的节点（一次取全，前端自己做布局）。

        `branch_id` 非空时只取该分支的节点（"查看分支状态"用）。
        """
        sql = "SELECT * FROM story_nodes WHERE novel_id=?"
        params = [novel_id]
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        if branch_id is not None:
            sql += " AND branch_id=?"
            params.append(int(branch_id))
        sql += " ORDER BY depth, seq, id"
        return [self._story_row(r) for r in self.query(sql, tuple(params))]

    def get_story_node(self, node_id):
        return self._story_row(
            self.one("SELECT * FROM story_nodes WHERE id=?", (node_id,)))

    def story_children(self, node_id):
        return [self._story_row(r) for r in
                self.query("SELECT * FROM story_nodes WHERE parent_id=? "
                           "AND deleted_at IS NULL ORDER BY seq, id", (node_id,))]

    def story_root(self, novel_id):
        return self._story_row(self.one(
            "SELECT * FROM story_nodes WHERE novel_id=? AND kind='root' "
            "AND deleted_at IS NULL ORDER BY id LIMIT 1", (novel_id,)))

    def add_story_node(self, novel_id, kind="event", title="", description="",
                       parent_id=None, event_type="plot", importance=3,
                       actor_name="", trigger_type="", stakes="", world_time="",
                       involved_characters=None, involved_entities=None,
                       payload=None, status=None, branch_hint="", note="",
                       branch_id=0, tick=0, state_delta=None, fact_delta=None,
                       knowledge_delta=None, delta_applied=0):
        if kind not in ("root", "event", "decision", "option"):
            raise ValueError("kind 非法：%s" % kind)
        if status is None:
            status = "open" if kind == "decision" else "idea"
        parent = self.get_story_node(parent_id) if parent_id else None
        depth = (parent["depth"] + 1) if parent else 0
        # 分支默认继承父节点：否则同一个分支上的节点会散在 branch_id=0 上，
        # "这条分支上都有哪些节点"就查不全。
        if parent and not branch_id:
            branch_id = parent.get("branch_id") or 0
        seq = self.scalar(
            "SELECT COALESCE(MAX(seq), -1) + 1 FROM story_nodes WHERE "
            "novel_id=? AND " + ("parent_id=?" if parent_id else "parent_id IS NULL"),
            ((novel_id, parent_id) if parent_id else (novel_id,)), default=0)
        return self.execute(
            "INSERT INTO story_nodes(novel_id, parent_id, depth, seq, kind, title, "
            "description, event_type, importance, actor_name, trigger_type, stakes, "
            "world_time, involved_characters, involved_entities, payload, status, "
            "branch_hint, note, branch_id, tick, state_delta, fact_delta, "
            "knowledge_delta, delta_applied) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (novel_id, parent_id, depth, int(seq), kind, _clean(title),
             _clean(description), event_type, int(importance), _clean(actor_name),
             trigger_type, _clean(stakes), world_time,
             _jdumps(involved_characters or []), _jdumps(involved_entities or []),
             _jdumps(payload or {}), status, _clean(branch_hint), _clean(note),
             int(branch_id or 0), int(tick or 0), _jdumps(state_delta or {}),
             _jdumps(fact_delta or []), _jdumps(knowledge_delta or []),
             int(_truthy(delta_applied))),
        )

    def update_story_node(self, node_id, **fields):
        allowed = {"title", "description", "event_type", "importance", "actor_name",
                   "trigger_type", "stakes", "world_time", "status", "branch_hint",
                   "note", "decision_ref", "event_ref", "payload",
                   "involved_characters", "involved_entities", "parent_id",
                   "branch_id", "tick", "state_delta", "fact_delta",
                   "knowledge_delta", "delta_applied"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            sets.append("%s=?" % k)
            if k in self._STORY_JSON_FIELDS:
                params.append(_jdumps(
                    v if v is not None
                    else self._STORY_JSON_DEFAULTS.get(k, [])))
            else:
                params.append(v)
        if not sets:
            return self.get_story_node(node_id)
        params.append(node_id)
        self.execute("UPDATE story_nodes SET %s WHERE id=?" % ", ".join(sets),
                     params)
        return self.get_story_node(node_id)

    # ------------------------------------------------ 分支（v6.7）

    def add_branch(self, novel_id, root_node_id=None, parent_branch=0,
                   base_event_id=0, base_tick=0, label="", status="open"):
        return self.execute(
            "INSERT INTO branches(novel_id, root_node_id, parent_branch, "
            "base_event_id, base_tick, label, status) VALUES (?,?,?,?,?,?,?)",
            (novel_id, root_node_id, int(parent_branch or 0),
             int(base_event_id or 0), int(base_tick or 0), _clean(label),
             _norm_enum(status, BRANCH_STATUSES, "open")))

    def get_branch(self, branch_id):
        row = self.one("SELECT * FROM branches WHERE id=?", (int(branch_id or 0),))
        return self._branch_row(row)

    def find_branch_by_root(self, novel_id, root_node_id):
        row = self.one("SELECT * FROM branches WHERE novel_id=? AND root_node_id=?",
                       (novel_id, int(root_node_id or 0)))
        return self._branch_row(row)

    @staticmethod
    def _branch_row(row):
        if not row:
            return row
        for f, default in (("overlay_state", {}), ("overlay_facts", []),
                           ("overlay_knowledge", [])):
            row[f] = _jloads(row.get(f), default)
        return row

    def list_branches(self, novel_id, status=None):
        sql = "SELECT * FROM branches WHERE novel_id=?"
        params = [novel_id]
        if status:
            if isinstance(status, (list, tuple)):
                marks = ",".join("?" * len(status))
                sql += " AND status IN (%s)" % marks
                params.extend(status)
            else:
                sql += " AND status=?"
                params.append(status)
        sql += " ORDER BY id"
        return [self._branch_row(r) for r in self.query(sql, tuple(params))]

    def update_branch(self, branch_id, **fields):
        allowed = {"root_node_id", "parent_branch", "base_event_id", "base_tick",
                   "label", "status", "overlay_state", "overlay_facts",
                   "overlay_knowledge", "node_count", "discarded_at"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            sets.append("%s=?" % k)
            if k in ("overlay_state", "overlay_facts", "overlay_knowledge"):
                params.append(_jdumps(
                    v if v is not None else ({} if k == "overlay_state" else [])))
            else:
                params.append(v)
        if not sets:
            return self.get_branch(branch_id)
        params.append(int(branch_id))
        self.execute("UPDATE branches SET %s WHERE id=?" % ", ".join(sets), params)
        return self.get_branch(branch_id)

    def discard_branches(self, novel_id, keep_id=0):
        """落定/清沙盘之后，把其它分支标记为 discarded（不删行，便于回溯）。"""
        return self.execute(
            "UPDATE branches SET status='discarded', "
            "discarded_at=datetime('now','localtime') "
            "WHERE novel_id=? AND id<>? AND status IN ('open','active')",
            (novel_id, int(keep_id or 0)))

    def story_subtree_ids(self, node_id):
        """节点自身 + 所有后代（含已软删的，用于彻底清）。"""
        rows = self.query(
            "WITH RECURSIVE sub(id) AS ("
            "  SELECT id FROM story_nodes WHERE id=?"
            "  UNION ALL"
            "  SELECT s.id FROM story_nodes s JOIN sub ON s.parent_id = sub.id"
            ") SELECT id FROM sub", (node_id,))
        return [r["id"] for r in rows]

    def delete_story_subtree(self, node_id):
        ids = self.story_subtree_ids(node_id)
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        with self.tx() as con:
            con.execute("DELETE FROM story_nodes WHERE id IN (%s)" % marks, ids)
        return len(ids)

    def clear_story_tree(self, novel_id):
        n = self.scalar("SELECT COUNT(*) FROM story_nodes WHERE novel_id=?",
                        (novel_id,), default=0)
        self.execute("DELETE FROM story_nodes WHERE novel_id=?", (novel_id,))
        return int(n)

    # ================================================== 认知层（v6.7 约束 1）

    def record_knowledge(self, novel_id, character_id, content,
                         character_name="", know_type="know", about_type="fact",
                         about_id=None, about_text="", source="witnessed",
                         confidence=3, is_false=0, contradicts_fact_id=None,
                         learned_event_id=None, learned_chapter=0,
                         learned_tick=0, status="active"):
        """记录一条**角色认知**。

        这是「角色的 believe/suspect/assume/memory/dream」的**唯一去处**。
        它们永远不得进 `facts`（那是世界真相），执行点在
        `engine/facts.add_fact()` 的第一行。

        `character_id` 是**相信的那个人**。这个方向写反了，"谁以为谁在哪"
        整段就会反过来——展示层会画出"该蒙在鼓里的人在读别人"的反结论
        （v6.4 在 location_beliefs 上踩过一模一样的坑）。
        """
        if not character_id and character_name:
            row = self.get_character_by_name(novel_id, character_name)
            character_id = row["id"] if row else None
        if not character_id:
            return 0
        try:
            confidence = min(5, max(1, int(confidence)))
        except (TypeError, ValueError):
            confidence = 3
        kid = self.execute(
            "INSERT INTO character_knowledge(novel_id, character_id, know_type, "
            "about_type, about_id, about_text, content, source, confidence, "
            "is_false, contradicts_fact_id, learned_event_id, learned_chapter, "
            "learned_tick, status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (novel_id, int(character_id),
             _norm_know_type(know_type),
             _norm_enum(about_type, KNOW_ABOUT_TYPES, "fact"),
             about_id, _clean(about_text), _clean(content),
             _norm_know_source(source), confidence,
             1 if _truthy(is_false) else 0, contradicts_fact_id,
             learned_event_id, int(learned_chapter or 0), int(learned_tick or 0),
             _norm_enum(status, KNOW_STATUSES, "active")))
        # 顺手更新"上次更新认知的 tick"：角色提示词需要判断
        # "他这份认知是什么时候的"（三天前的消息和此刻看到的不是一回事）
        try:
            self.execute("UPDATE characters SET knowledge_tick=?, "
                         "updated_at=datetime('now','localtime') WHERE id=?",
                         (int(learned_tick or 0), int(character_id)))
        except sqlite3.Error:
            pass
        return kid

    def list_knowledge(self, novel_id, character_id=None, know_type=None,
                       about_type=None, status="active", limit=None):
        sql = "SELECT * FROM character_knowledge WHERE novel_id=?"
        params = [novel_id]
        if character_id:
            sql += " AND character_id=?"
            params.append(int(character_id))
        if know_type:
            if isinstance(know_type, (list, tuple)):
                marks = ",".join("?" * len(know_type))
                sql += " AND know_type IN (%s)" % marks
                params.extend(know_type)
            else:
                sql += " AND know_type=?"
                params.append(know_type)
        if about_type:
            sql += " AND about_type=?"
            params.append(about_type)
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY id DESC"
        if limit:
            sql += " LIMIT ?"
            params.append(int(limit))
        return self.query(sql, tuple(params))

    def has_knowledge(self, novel_id, character_id, about_text,
                      allow_types=("know",)):
        """他有没有渠道知道这件事（信息边界校验用）。

        判定走"认知对象或内容里提到它"的模糊匹配——**方向取宽**：
        宁可漏报一次泄漏，也不要冤枉一段合法叙述（误报会把作者训练成
        "见到红色徽标就点忽略"）。
        """
        text = _clean(about_text)
        if not text:
            return True
        marks = ",".join("?" * len(allow_types))
        # ⚠ 这里**不能**用 `... IN (%s) ... '%' || about_text || '%%' ..." % marks`：
        #   SQL 里的 `'%'` 是字面百分号，`%` 运算符会把它当成格式占位符，
        #   而我们要补的参数只有 marks 一个 → TypeError: not enough arguments
        #   for format string。（2026-09-19 发布回流时才暴露：has_knowledge
        #   平时很少被走到，一旦走到就必炸。）
        #   解法：把 `%` 在格式化后再拼进去，格式化阶段只留 `%s`。
        sql = (
            "SELECT id FROM character_knowledge WHERE novel_id=? AND character_id=? "
            "AND status IN ('active','confirmed') AND know_type IN (%s) "
            "AND (about_text LIKE ? OR content LIKE ? "
            "OR ? LIKE @PCT@ || about_text || @PCT@) LIMIT 1" % marks
        ).replace("@PCT@", "'%'")
        row = self.one(
            sql,
            tuple([novel_id, int(character_id)] + list(allow_types)
                  + ["%" + text + "%", "%" + text + "%", text]))
        return bool(row)

    def refute_knowledge(self, knowledge_id, note=""):
        """作废一条认知（"他后来知道真相了"）。"""
        return self.execute(
            "UPDATE character_knowledge SET status='refuted' WHERE id=?",
            (int(knowledge_id),))

    def knowledge_matrix(self, novel_id, limit=80):
        """知识矩阵：按角色 / 按对象两个视角（第一页的展示单元）。"""
        rows = self.query(
            "SELECT k.*, c.name AS character_name FROM character_knowledge k "
            "LEFT JOIN characters c ON c.id=k.character_id "
            "WHERE k.novel_id=? AND k.status IN ('active','confirmed') "
            "ORDER BY k.character_id, k.id DESC LIMIT ?", (novel_id, int(limit)))
        by_char, by_about = {}, {}
        for r in rows:
            by_char.setdefault(r.get("character_name") or "?", []).append({
                "id": r["id"], "know_type": r["know_type"],
                "about_text": r.get("about_text") or "",
                "content": r.get("content") or "",
                "source": r.get("source"), "is_false": r.get("is_false"),
            })
            key = "%s|%s" % (r.get("about_type"), r.get("about_text") or "")
            by_about.setdefault(key, []).append({
                "id": r["id"], "character_name": r.get("character_name") or "?",
                "know_type": r["know_type"], "content": r.get("content") or "",
            })
        return {"by_character": by_char, "by_about": by_about}

    # ================================================== 待裁决事实缓冲区（v6.7 约束 2）

    def add_fact_candidate(self, novel_id, subject_name="", predicate="",
                           object_text="", fact_type="other",
                           classification="unverified", classified_by="python",
                           classify_reason="", confidence=3, quote="",
                           provenance="", source_kind="prose",
                           source_chapter=0, source_event_id=None,
                           subject_type="character", subject_id=None,
                           target_character_id=None):
        """把"抽取出来但定不了性"的条目接住。

        这张表的存在就是为了堵住这条洞：**「无冲突」不等于「是真的」**。
        正文写「李四突然想起，三年前自己杀过一个人」——正史从没记过这件事，
        比对结果永远是"无冲突"，v1.0 会直接把它写进 facts。
        """
        return self.execute(
            "INSERT INTO fact_candidates(novel_id, source_kind, source_event_id, "
            "source_chapter, quote, provenance, subject_type, subject_id, "
            "subject_name, predicate, object_text, fact_type, classification, "
            "classified_by, classify_reason, confidence, target_character_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (novel_id, _norm_enum(source_kind, ("prose", "sim", "user"), "prose"),
             source_event_id, int(source_chapter or 0), _clean(quote)[:500],
             _clean(provenance)[:120],
             _norm_enum(subject_type, ("character", "entity", "world"), "character"),
             subject_id, _clean(subject_name), _clean(predicate),
             _clean(object_text), _norm_enum(fact_type, FACT_TYPES, "other"),
             _norm_classification(classification),
             _norm_enum(classified_by, ("python", "llm", "user"), "python"),
             _clean(classify_reason)[:200],
             _to_int(confidence, 3, 1, 5),
             target_character_id))

    def list_candidates(self, novel_id, status="pending", classification=None,
                        chapter_number=None, limit=100):
        sql = "SELECT * FROM fact_candidates WHERE novel_id=?"
        params = [novel_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        if classification:
            sql += " AND classification=?"
            params.append(classification)
        if chapter_number:
            sql += " AND source_chapter=?"
            params.append(int(chapter_number))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return self.query(sql, tuple(params))

    def resolve_candidate(self, candidate_id, action="reject", note="",
                          classification=None):
        """裁决一条候选事实。

        action='accept' → 按**分类**落到正确的出口：
            cognition  → character_knowledge（谁信的归谁）
            world      → facts（此时它才是世界真相）
            其他       → 一律不得进 facts，改为保留在候选区
                          （想让它成真，先把分类改成 world）
        action='reject' → status='rejected'
        """
        row = self.one("SELECT * FROM fact_candidates WHERE id=?",
                       (int(candidate_id),))
        if not row:
            return {"ok": False, "error": "候选不存在"}
        cls = _norm_classification(classification or row.get("classification"))
        if action != "accept":
            self.execute(
                "UPDATE fact_candidates SET status='rejected', resolution_note=?, "
                "resolved_at=datetime('now','localtime') WHERE id=?",
                (_clean(note), int(candidate_id)))
            return {"ok": True, "action": "rejected"}

        novel_id = row["novel_id"]
        if cls == "cognition":
            kid = self.record_knowledge(
                novel_id,
                row.get("target_character_id") or row.get("subject_id"),
                row.get("object_text") or row.get("quote") or "",
                character_name=row.get("subject_name") or "",
                know_type="believe", about_type="fact",
                about_text=row.get("predicate") or "",
                source="inferred", confidence=row.get("confidence") or 3,
                learned_chapter=row.get("source_chapter") or 0)
            self.execute(
                "UPDATE fact_candidates SET status='accepted', resolution_note=?, "
                "resolved_at=datetime('now','localtime') WHERE id=?",
                (_clean(note), int(candidate_id)))
            return {"ok": True, "action": "knowledge", "knowledge_id": kid}

        if cls != "world":
            # 回忆/梦境/转述/定不了性 —— **明确拒绝写进 facts**。
            # 这是约束 2 的最后一道防线：走到这里还想入库，只能先把分类改成 world。
            return {"ok": False,
                    "error": "分类为 %s 的候选不能直接成为正史；"
                             "若确认它是真相，请先把分类改成 world" % cls}

        from engine import facts as F
        fid = F.add_fact(
            self, novel_id,
            subject_type=row.get("subject_type") or "character",
            subject_id=row.get("subject_id"), subject_name=row.get("subject_name"),
            predicate=row.get("predicate") or "", object_text=row.get("object_text"),
            fact_type=row.get("fact_type") or "other",
            tick=int(self.scalar("SELECT absolute_tick FROM world_clock "
                                 "WHERE novel_id=?", (novel_id,), default=0) or 0),
            source_chapter=row.get("source_chapter") or 0, source_kind="prose",
            fact_status="canonical", confidence=row.get("confidence") or 5,
            classified_by="user", note="由待裁决事实接受（#%s）" % candidate_id,
            quote=row.get("quote") or "")
        self.execute(
            "UPDATE fact_candidates SET status='accepted', resolved_fact_id=?, "
            "resolution_note=?, resolved_at=datetime('now','localtime') WHERE id=?",
            (fid, _clean(note), int(candidate_id)))
        return {"ok": True, "action": "fact", "fact_id": fid}

    def reclassify_candidate(self, candidate_id, classification,
                             reason="", by="user"):
        """改分类。**分类不是判决，是可修改的过程状态。**

        判错了（把"角色回忆"判成 world，或把真的判成 memory）都靠这一步纠正。
        """
        return self.execute(
            "UPDATE fact_candidates SET classification=?, classified_by=?, "
            "classify_reason=? WHERE id=?",
            (_norm_classification(classification),
             _norm_enum(by, ("python", "llm", "user"), "user"),
             _clean(reason)[:200], int(candidate_id)))

    # ================================================== 冲突队列（v6.7）

    def add_conflict(self, novel_id, chapter_number, conflict_type,
                     subject_name="", prose_quote="", fact_statement="",
                     detail="", severity="error", detected_by="python",
                     fact_id=None):
        return self.execute(
            "INSERT INTO fact_conflicts(novel_id, chapter_number, conflict_type, "
            "fact_id, subject_name, prose_quote, fact_statement, detail, severity, "
            "detected_by) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (novel_id, int(chapter_number or 0),
             _norm_conflict_type(conflict_type), fact_id, _clean(subject_name),
             _clean(prose_quote)[:500], _clean(fact_statement)[:500],
             _clean(detail)[:500],
             _norm_enum(severity, RULE_SEVERITIES, "error"),
             _norm_enum(detected_by, ("python", "llm"), "python")))

    def list_conflicts(self, novel_id, status="open", chapter_number=None,
                       limit=100):
        sql = "SELECT * FROM fact_conflicts WHERE novel_id=?"
        params = [novel_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        if chapter_number:
            sql += " AND chapter_number=?"
            params.append(int(chapter_number))
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        return self.query(sql, tuple(params))

    def resolve_conflict(self, conflict_id, status, note=""):
        status = _norm_enum(status, CONFLICT_STATUSES, "ignored")
        if status == "open":
            raise ValueError("裁决结果不能是 open")
        return self.execute(
            "UPDATE fact_conflicts SET status=?, resolution_note=?, "
            "resolved_at=datetime('now','localtime') WHERE id=?",
            (status, _clean(note), int(conflict_id)))

    def refresh_chapter_conflict_count(self, novel_id, chapter_number):
        """把未裁决冲突数写回 chapters（列表页出徽标，避免 N+1 查询）。"""
        n = int(self.scalar(
            "SELECT COUNT(*) FROM fact_conflicts WHERE novel_id=? "
            "AND chapter_number=? AND status='open'",
            (novel_id, int(chapter_number)), default=0) or 0)
        self.execute(
            "UPDATE chapters SET fact_conflict_count=?, verified=? "
            "WHERE novel_id=? AND chapter_number=?",
            (n, 0 if n else 1, novel_id, int(chapter_number)))
        return n

    # ================================================== 章节↔事件（v6.7）

    def link_chapter_events(self, novel_id, chapter_number, event_ids,
                            link_type="source"):
        link_type = _norm_enum(link_type, ("source", "mention", "conflict"),
                               "source")
        n = 0
        with self.tx() as con:
            for seq, eid in enumerate(event_ids or []):
                try:
                    eid = int(eid)
                except (TypeError, ValueError):
                    continue
                n += con.execute(
                    "INSERT OR IGNORE INTO chapter_event_links(novel_id, "
                    "chapter_number, event_id, seq, link_type) VALUES (?,?,?,?,?)",
                    (novel_id, int(chapter_number), eid, seq, link_type)).rowcount
        return n

    def unlink_chapter_events(self, novel_id, chapter_number, link_type=None):
        sql = "DELETE FROM chapter_event_links WHERE novel_id=? AND chapter_number=?"
        params = [novel_id, int(chapter_number)]
        if link_type:
            sql += " AND link_type=?"
            params.append(link_type)
        return self.execute(sql, params)

    def list_chapter_event_links(self, novel_id, chapter_number=None,
                                event_id=None):
        sql = "SELECT * FROM chapter_event_links WHERE novel_id=?"
        params = [novel_id]
        if chapter_number is not None:
            sql += " AND chapter_number=?"
            params.append(int(chapter_number))
        if event_id is not None:
            sql += " AND event_id=?"
            params.append(int(event_id))
        sql += " ORDER BY chapter_number, seq, id"
        return self.query(sql, tuple(params))

    def chapters_writing_event(self, novel_id, event_id):
        """这个正史事件被哪些章写过。（防同一事件被两章重复写成正文）"""
        return self.query(
            "SELECT chapter_number, link_type FROM chapter_event_links "
            "WHERE novel_id=? AND event_id=? ORDER BY chapter_number",
            (novel_id, int(event_id)))

    # ================================================== 世界规则（v6.7）

    def list_world_rules(self, novel_id, active_only=True, scope=None):
        sql = "SELECT * FROM world_rules WHERE novel_id=?"
        params = [novel_id]
        if active_only:
            sql += " AND is_active=1"
        if scope:
            sql += " AND scope=?"
            params.append(scope)
        sql += " ORDER BY is_hard DESC, severity, id"
        return self.query(sql, tuple(params))

    def add_world_rule(self, novel_id, content, rule_key="", scope="global",
                       subject_name="", is_hard=1, severity="error",
                       check_hint="", source="user", created_chapter=0):
        content = _clean(content)
        if not content:
            return 0
        key = _clean(rule_key) or content[:24]
        exist = self.one("SELECT id FROM world_rules WHERE novel_id=? AND rule_key=?",
                         (novel_id, key))
        if exist:
            return exist["id"]
        return self.execute(
            "INSERT INTO world_rules(novel_id, rule_key, content, scope, "
            "subject_name, is_hard, severity, check_hint, source, "
            "created_chapter) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (novel_id, key, content, _norm_rule_scope(scope),
             _clean(subject_name), 1 if _truthy(is_hard) else 0,
             _norm_enum(severity, RULE_SEVERITIES, "error"),
             _clean(check_hint),
             _norm_enum(source, ("user", "llm_extracted", "system"), "user"),
             int(created_chapter or 0)))

    def update_world_rule(self, rule_id, **fields):
        allowed = {"rule_key", "content", "scope", "subject_name", "is_hard",
                   "severity", "check_hint", "source", "is_active"}
        sets, params = [], []
        for k, v in fields.items():
            if k not in allowed:
                continue
            sets.append("%s=?" % k)
            if k == "scope":
                v = _norm_rule_scope(v)
            elif k == "severity":
                v = _norm_enum(v, RULE_SEVERITIES, "error")
            elif k in ("is_hard", "is_active"):
                v = 1 if _truthy(v) else 0
            params.append(v)
        if not sets:
            return 0
        params.append(int(rule_id))
        return self.execute("UPDATE world_rules SET %s WHERE id=?"
                            % ", ".join(sets), params)

    def delete_world_rule(self, rule_id):
        return self.execute("DELETE FROM world_rules WHERE id=?", (int(rule_id),))

    # ================================================== 目标生命周期（v6.7）

    def set_goal_lifecycle(self, goal_id, lifecycle, chapter_ref=None):
        """改目标生命周期，并**同步**派生 `status`。

        两列不一致是必然的（谁都会忘了改另一列），所以收口在一个函数里：
            achieved → status=achieved / failed → failed / transformed → evolved
        其它过程态（dormant/active/at_risk）不动 status。
        """
        lc = _norm_lifecycle(lifecycle)
        sets = ["lifecycle=?"]
        params = [lc]
        derived = {"achieved": "achieved", "failed": "failed",
                   "transformed": "evolved"}.get(lc)
        if derived:
            sets.append("status=?")
            params.append(derived)
            sets.append("settled_chapter=?")
            params.append(int(chapter_ref or 0))
        if chapter_ref is not None:
            sets.append("last_reviewed_chapter=?")
            params.append(int(chapter_ref))
        params.append(int(goal_id))
        return self.execute("UPDATE character_goals SET %s WHERE id=?"
                            % ", ".join(sets), params)

    # ================================================== 章节

    def list_chapters(self, novel_id, status=None, include_deleted=False):
        sql = "SELECT * FROM chapters WHERE novel_id=?"
        params = [novel_id]
        if not include_deleted:
            sql += " AND deleted_at IS NULL"
        if status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY chapter_number"
        rows = self.query(sql, params)
        for r in rows:
            r["source_event_ids"] = _jloads(r.get("source_event_ids"), [])
        return rows

    def get_chapter(self, novel_id, chapter_number):
        row = self.one(
            "SELECT * FROM chapters WHERE novel_id=? AND chapter_number=?",
            (novel_id, int(chapter_number)))
        if row:
            row["source_event_ids"] = _jloads(row.get("source_event_ids"), [])
        return row

    def save_chapter(self, novel_id, chapter_number, title="", content="",
                     summary="", status="draft", pov_character="", viewpoint="",
                     location="", chapter_shape=None, world_time_start=None,
                     world_time_end=None, source_event_ids=None, ending_mode=None,
                     change_note="", keep_version=True):
        """保存章节：自动留版本、更新字数、同步 novels.current_chapter。

        关于"结构化元数据"（形态 / 世界时间 / 来源事件 / 收尾方式）：
        这些字段与正文无关，重复保存正文时不应被抹掉。因此约定：
          - 传 None（默认） => 保留原值（新建时取 schema 默认值）
          - 传具体值        => 覆盖
        只有 title/content/summary/status 这些"正文属性"才允许被空值覆盖。

        返回 {'chapter': dict, 'version': int, 'word_count': int}
        """
        content = _clean(content)
        wc = _count_words(content)
        cnum = int(chapter_number)
        existing = self.get_chapter(novel_id, cnum)

        # 元数据：None 表示"沿用旧值"
        def _meta(key, incoming, default):
            if incoming is not None:
                return incoming
            if existing:
                return existing.get(key) if existing.get(key) is not None else default
            return default

        chapter_shape = _meta("chapter_shape", chapter_shape, "scene")
        world_time_start = _meta("world_time_start", world_time_start, "")
        world_time_end = _meta("world_time_end", world_time_end, "")
        ending_mode = _meta("ending_mode", ending_mode, "")
        source_event_ids = _meta("source_event_ids", source_event_ids, [])

        # 正文属性：传空字符串同样视为"沿用旧值"。
        # 理由：调用方只想改正文时（如 Web 的自动保存、局部重写），
        # 不该因为没传 title/summary 就把章节名和摘要清空。
        def _text(key, incoming):
            if incoming or not existing:
                return incoming
            return existing.get(key) or ""

        title = _text("title", title)
        summary = _text("summary", summary)
        pov_character = _text("pov_character", pov_character)
        viewpoint = _text("viewpoint", viewpoint)
        location = _text("location", location)

        with self.tx() as con:
            if existing:
                # 留档：先把"即将被覆盖的当前内容"存成一个版本。
                # 版本号从 1 开始，v1 永远是本章最早的稿子。
                if keep_version and existing.get("content"):
                    prev_ver = con.execute(
                        "SELECT COALESCE(MAX(version),0) FROM chapter_versions "
                        "WHERE novel_id=? AND chapter_number=?", (novel_id, cnum)
                    ).fetchone()[0]
                    con.execute(
                        "INSERT INTO chapter_versions(novel_id, chapter_number, version, "
                        "title, content, word_count, change_note) VALUES (?,?,?,?,?,?,?)",
                        (novel_id, cnum, prev_ver + 1,
                         existing.get("title") or "", existing.get("content") or "",
                         existing.get("word_count") or 0,
                         _clean(change_note) or "自动留档（保存前）"),
                    )
                elif not keep_version and not con.execute(
                        "SELECT 1 FROM chapter_versions WHERE novel_id=? "
                        "AND chapter_number=? LIMIT 1", (novel_id, cnum)
                ).fetchone():
                    # 明确要求不留档、且本章还没有任何版本时，
                    # 把"原始首稿"补记成 v1，保证历史不丢起点。
                    if existing.get("content"):
                        con.execute(
                            "INSERT INTO chapter_versions(novel_id, chapter_number, "
                            "version, title, content, word_count, change_note) "
                            "VALUES (?,?,?,?,?,?,?)",
                            (novel_id, cnum, 1, existing.get("title") or "",
                             existing.get("content") or "",
                             existing.get("word_count") or 0, "首稿"),
                        )
                con.execute(
                    "UPDATE chapters SET title=?, content=?, summary=?, status=?, "
                    "word_count=?, pov_character=?, viewpoint=?, location=?, "
                    "chapter_shape=?, world_time_start=?, world_time_end=?, "
                    "source_event_ids=?, ending_mode=?, updated_at=? "
                    "WHERE novel_id=? AND chapter_number=?",
                    (_clean(title), content, _clean(summary), status, wc,
                     pov_character, viewpoint, location, chapter_shape,
                     world_time_start, world_time_end,
                     _jdumps(source_event_ids or []), ending_mode, _now(),
                     novel_id, cnum),
                )
                version = con.execute(
                    "SELECT COALESCE(MAX(version),0) FROM chapter_versions "
                    "WHERE novel_id=? AND chapter_number=?", (novel_id, cnum)
                ).fetchone()[0]
            else:
                con.execute(
                    "INSERT INTO chapters(novel_id, chapter_number, title, content, "
                    "summary, status, word_count, pov_character, viewpoint, location, "
                    "chapter_shape, world_time_start, world_time_end, source_event_ids, "
                    "ending_mode) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (novel_id, cnum, _clean(title), content, _clean(summary), status,
                     wc, pov_character, viewpoint, location, chapter_shape,
                     world_time_start, world_time_end,
                     _jdumps(source_event_ids or []), ending_mode),
                )
                version = 0

            # 推进指针
            con.execute(
                "UPDATE novels SET current_chapter = MAX(current_chapter, ?), "
                "updated_at=? WHERE id=?", (cnum, _now(), novel_id))

        return {"chapter": self.get_chapter(novel_id, cnum), "version": version,
                "word_count": wc}

    def update_chapter_status(self, novel_id, chapter_number, status):
        return self.execute(
            "UPDATE chapters SET status=?, updated_at=? WHERE novel_id=? "
            "AND chapter_number=?", (status, _now(), novel_id, int(chapter_number)))

    def release_chapter(self, novel_id, chapter_number, note="退回成素材前自动留档"):
        """把一章退回成"未成章"：正文留档、事件释放、章节行删掉。

        用途：一章被塞进太多事件（比如落定 20 个事件一次写完），写成流水账，
        想拆成几章重写。

        三个要点，都是踩过的坑：
          - 正文先补记进 chapter_versions，删了也找得回；
          - chapters 行**物理删除**。`UNIQUE(novel_id, chapter_number)` 连软删行
            也算占用，留个 deleted_at 会让"重写第 N 章"直接撞唯一约束；
          - 事件 `chapter_ref` 归 0，`novels.current_chapter` 回退到现存最大章号
            （否则指针还停在已删的章上，下一章号会跳）。

        返回 {'released_events','archived_version','current_chapter','title'}；
        章节不存在返回 None。
        """
        row = self.get_chapter(novel_id, chapter_number)
        if not row:
            return None
        cnum = int(chapter_number)
        with self.tx() as con:
            ver = con.execute(
                "SELECT COALESCE(MAX(version),0) FROM chapter_versions "
                "WHERE novel_id=? AND chapter_number=?", (novel_id, cnum)
            ).fetchone()[0]
            if row.get("content"):
                con.execute(
                    "INSERT INTO chapter_versions(novel_id, chapter_number, version, "
                    "title, content, word_count, change_note) VALUES (?,?,?,?,?,?,?)",
                    (novel_id, cnum, ver + 1, row.get("title") or "",
                     row.get("content") or "", row.get("word_count") or 0,
                     _clean(note) or "退回成素材"))
                archived = ver + 1
            else:
                archived = 0
            cur = con.execute(
                "UPDATE events SET chapter_ref=0 WHERE novel_id=? AND chapter_ref=?",
                (novel_id, cnum))
            released = cur.rowcount
            # chapters_fts 有 AFTER DELETE 触发器，不用手动清索引
            con.execute("DELETE FROM chapters WHERE novel_id=? AND chapter_number=?",
                        (novel_id, cnum))
            left = con.execute(
                "SELECT COALESCE(MAX(chapter_number),0) FROM chapters WHERE novel_id=?",
                (novel_id,)).fetchone()[0]
            con.execute("UPDATE novels SET current_chapter=?, updated_at=? WHERE id=?",
                        (int(left or 0), _now(), novel_id))
        return {"released_events": released, "archived_version": archived,
                "current_chapter": int(left or 0), "title": row.get("title") or ""}

    def list_versions(self, novel_id, chapter_number):
        return self.query(
            "SELECT id, novel_id, chapter_number, version, title, word_count, "
            "change_note, created_at FROM chapter_versions WHERE novel_id=? "
            "AND chapter_number=? ORDER BY version DESC",
            (novel_id, int(chapter_number)))

    def restore_version(self, novel_id, chapter_number, version):
        """从历史版本恢复正文。

        语义要点：
          - 恢复本身**不新增版本**（keep_version=False），否则反复恢复会不断
            复制出重复版本，把历史淹没。
          - 版本表只留档正文相关字段，恢复时必须保留当前章节的结构化元数据
            （选角 / 世界时间 / 来源事件 / 形态），否则会静默丢数据。
          - 恢复前会把"当前内容"补记成一个版本（见 save_chapter 的
            keep_version=False 分支），保证恢复操作本身可回退。
        """
        row = self.one(
            "SELECT * FROM chapter_versions WHERE novel_id=? AND chapter_number=? "
            "AND version=?", (novel_id, int(chapter_number), int(version)))
        if not row:
            return None
        cur = self.get_chapter(novel_id, chapter_number) or {}
        res = self.save_chapter(
            novel_id, chapter_number, title=row["title"], content=row["content"],
            summary=cur.get("summary") or "",
            status=cur.get("status") or "draft",
            pov_character=cur.get("pov_character") or "",
            viewpoint=cur.get("viewpoint") or "",
            location=cur.get("location") or "",
            chapter_shape=cur.get("chapter_shape") or "scene",
            world_time_start=cur.get("world_time_start") or "",
            world_time_end=cur.get("world_time_end") or "",
            source_event_ids=cur.get("source_event_ids") or [],
            ending_mode=cur.get("ending_mode") or "",
            change_note="恢复前的自动留档", keep_version=False)
        res["restored_from"] = int(version)
        return res

    def export_chapter_file(self, novel_id, chapter_number):
        """导出单章为 markdown 到 novels/{书名}/chapter_XXX.md"""
        novel = self.get_novel(novel_id)
        ch = self.get_chapter(novel_id, chapter_number)
        if not novel or not ch:
            return None
        folder = os.path.join(NOVELS_DIR, _sanitize_filename(novel["title"]))
        os.makedirs(folder, exist_ok=True)
        fname = "chapter_%03d.md" % int(chapter_number)
        path = os.path.join(folder, fname)
        body = "# 第%s章 %s\n\n%s\n" % (
            int(chapter_number), ch.get("title") or "", ch.get("content") or "")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        return path

    def export_all(self, novel_id):
        novel = self.get_novel(novel_id)
        if not novel:
            return []
        paths = []
        for ch in self.list_chapters(novel_id):
            if ch.get("content"):
                p = self.export_chapter_file(novel_id, ch["chapter_number"])
                if p:
                    paths.append(p)
        return paths

    # ================================================== 章节选角

    def list_casting(self, novel_id, chapter_number):
        rows = self.query(
            "SELECT * FROM chapter_casting WHERE novel_id=? AND chapter_number=? "
            "ORDER BY CASE role_in_chapter WHEN 'pov' THEN 0 WHEN 'main' THEN 1 "
            "WHEN 'supporting' THEN 2 WHEN 'cameo' THEN 3 ELSE 4 END, id",
            (novel_id, int(chapter_number)))
        return rows

    def set_casting(self, novel_id, chapter_number, entries):
        """整体替换某章选角。entries: [{'character_id','character_name',
        'role_in_chapter','control_mode','is_new_character','note'}]"""
        cnum = int(chapter_number)
        with self.tx() as con:
            con.execute(
                "DELETE FROM chapter_casting WHERE novel_id=? AND chapter_number=?",
                (novel_id, cnum))
            for e in entries:
                con.execute(
                    "INSERT INTO chapter_casting(novel_id, chapter_number, character_id, "
                    "character_name, role_in_chapter, control_mode, is_new_character, "
                    "note) VALUES (?,?,?,?,?,?,?,?)",
                    (novel_id, cnum, e.get("character_id"), _clean(e.get("character_name", "")),
                     e.get("role_in_chapter", "supporting"),
                     e.get("control_mode", "ai"),
                     1 if e.get("is_new_character") else 0, _clean(e.get("note", ""))),
                )
        return self.list_casting(novel_id, cnum)

    def add_casting(self, novel_id, chapter_number, character_name,
                    role_in_chapter="supporting", control_mode="ai",
                    is_new_character=False, character_id=None, note=""):
        return self.execute(
            "INSERT INTO chapter_casting(novel_id, chapter_number, character_id, "
            "character_name, role_in_chapter, control_mode, is_new_character, note) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (novel_id, int(chapter_number), character_id, _clean(character_name),
             role_in_chapter, control_mode, 1 if is_new_character else 0, _clean(note)),
        )

    # ================================================== 作者声音

    def list_voice(self, novel_id, category=None, active_only=True):
        sql = "SELECT * FROM author_voice WHERE novel_id=?"
        params = [novel_id]
        if category:
            sql += " AND category=?"
            params.append(category)
        if active_only:
            sql += " AND is_active=1"
        sql += " ORDER BY category, weight DESC, id"
        return self.query(sql, params)

    def add_voice(self, novel_id, category, content, example="", weight=3):
        return self.execute(
            "INSERT INTO author_voice(novel_id, category, content, example, weight) "
            "VALUES (?,?,?,?,?)",
            (novel_id, category, _clean(content), _clean(example), int(weight)),
        )

    def toggle_voice(self, voice_id, is_active):
        return self.execute(
            "UPDATE author_voice SET is_active=? WHERE id=?",
            (1 if is_active else 0, voice_id))

    def delete_voice(self, voice_id):
        return self.execute("DELETE FROM author_voice WHERE id=?", (voice_id,))

    # ================================================== 完结目标

    def list_world_goals(self, novel_id, status=None, goal_type=None):
        sql = ("SELECT g.*, c.name AS character_name FROM world_goals g "
               "LEFT JOIN characters c ON c.id = g.character_id WHERE g.novel_id=?")
        params = [novel_id]
        if status:
            sql += " AND g.status=?"
            params.append(status)
        if goal_type:
            sql += " AND g.goal_type=?"
            params.append(goal_type)
        sql += " ORDER BY g.is_primary DESC, g.id"
        return self.query(sql, params)

    def add_world_goal(self, novel_id, title, goal_type="world",
                       description="", condition_expr="", character_id=None,
                       is_primary=0):
        return self.execute(
            "INSERT INTO world_goals(novel_id, goal_type, title, description, "
            "condition_expr, character_id, is_primary) VALUES (?,?,?,?,?,?,?)",
            (novel_id, goal_type, _clean(title), _clean(description),
             _clean(condition_expr), character_id, 1 if is_primary else 0),
        )

    def update_world_goal(self, goal_id, **fields):
        allowed = {"title", "description", "condition_expr", "progress", "status",
                   "achieved_chapter", "is_primary", "goal_type"}
        sets, vals = [], []
        for k, v in fields.items():
            if k in allowed:
                sets.append("%s=?" % k)
                vals.append(v)
        if not sets:
            return None
        vals.append(goal_id)
        with self.tx() as con:
            con.execute("UPDATE world_goals SET %s WHERE id=?" % ", ".join(sets), vals)
        return self.one("SELECT * FROM world_goals WHERE id=?", (goal_id,))

    # ================================================== 事件种子

    def list_seeds(self, novel_id, seed_type=None, active_only=True):
        sql = "SELECT * FROM event_seeds WHERE novel_id=?"
        params = [novel_id]
        if seed_type:
            sql += " AND seed_type=?"
            params.append(seed_type)
        if active_only:
            sql += " AND is_active=1"
        sql += " ORDER BY intensity DESC, id"
        rows = self.query(sql, params)
        for r in rows:
            r["trigger_condition"] = _jloads(r.get("trigger_condition"), {})
            r["skeleton"] = _jloads(r.get("skeleton"), {})
            r["tags"] = _jloads(r.get("tags"), [])
        return rows

    def add_seed(self, novel_id, name, seed_type="conflict", description="",
                 trigger_condition=None, skeleton=None, tags=None, intensity=3):
        return self.execute(
            "INSERT INTO event_seeds(novel_id, seed_type, name, description, "
            "trigger_condition, skeleton, tags, intensity) VALUES (?,?,?,?,?,?,?,?)",
            (novel_id, seed_type, _clean(name), _clean(description),
             _jdumps(trigger_condition or {}), _jdumps(skeleton or {}),
             _jdumps(tags or []), int(intensity)),
        )

    def mark_seed_used(self, seed_id, chapter_ref):
        return self.execute(
            "UPDATE event_seeds SET used_count = used_count + 1, "
            "last_used_chapter=? WHERE id=?", (int(chapter_ref), seed_id))

    # ================================================== 配置 / 模型

    def get_config(self, key, default=""):
        row = self.one("SELECT value FROM app_config WHERE key=?", (key,))
        return row["value"] if row else default

    def set_config(self, key, value):
        return self.execute(
            "INSERT INTO app_config(key, value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=datetime('now','localtime')", (key, str(value)))

    def list_providers(self, active_only=False):
        sql = "SELECT * FROM providers"
        if active_only:
            sql += " WHERE is_active=1"
        rows = self.query(sql + " ORDER BY id")
        for r in rows:
            r["models"] = _jloads(r.get("models"), [])
        return rows

    def add_provider(self, name, kind="openai", base_url="", api_key_ref="",
                     note="", preset_key="", models=None, provider_id=None):
        """新增或更新服务商。

        models 是可选模型清单（list[str]），存成 JSON 数组。
        传 None 表示"不改动已有清单"，传 [] 表示"明确清空"。

        provider_id 非空时按主键 UPDATE（此时允许改名，不会因为名称变化
        而新插一条记录）；为空时按 name 走 upsert。
        """
        fields = {
            "kind": kind, "base_url": base_url,
            "api_key_ref": api_key_ref, "note": _clean(note),
            "preset_key": preset_key or "",
        }
        if models is not None:
            if isinstance(models, str):
                models = [m.strip() for m in models.split(",") if m.strip()]
            fields["models"] = _jdumps(list(models))

        # ---- 按主键更新（编辑已有记录，允许改名）----
        if provider_id:
            fields["name"] = _clean(name)
            assignments = ", ".join("%s=?" % c for c in fields)
            params = list(fields.values()) + [provider_id]
            return self.execute(
                "UPDATE providers SET %s, "
                "updated_at=datetime('now','localtime') WHERE id=?" % assignments,
                params)

        # ---- 按名称 upsert（新增）----
        cols = ["name"] + list(fields.keys())
        marks = ",".join("?" * len(cols))
        updates = ", ".join("%s=excluded.%s" % (c, c) for c in fields)
        return self.execute(
            "INSERT INTO providers(%s) VALUES (%s) "
            "ON CONFLICT(name) DO UPDATE SET %s, "
            "updated_at=datetime('now','localtime')" % (", ".join(cols), marks, updates),
            [_clean(name)] + list(fields.values()))

    def set_provider_models(self, provider_id, models):
        """单独更新某个服务商的模型清单。"""
        if isinstance(models, str):
            models = [m.strip() for m in models.split(",") if m.strip()]
        return self.execute(
            "UPDATE providers SET models=?, updated_at=datetime('now','localtime') "
            "WHERE id=?", (_jdumps(list(models)), provider_id))

    def get_provider(self, provider_id):
        row = self.one("SELECT * FROM providers WHERE id=?", (provider_id,))
        if row:
            row["models"] = _jloads(row.get("models"), [])
        return row

    def get_model_preset(self, slot, novel_id=0):
        """先查小说级，再回退全局默认。"""
        row = self.one(
            "SELECT * FROM model_presets WHERE novel_id=? AND slot=?", (novel_id, slot))
        if not row and novel_id:
            row = self.one(
                "SELECT * FROM model_presets WHERE novel_id=0 AND slot=?", (slot,))
        if row:
            row["extra"] = _jloads(row.get("extra"), {})
        return row

    def set_model_preset(self, slot, model, provider_id=None, temperature=0.8,
                         max_tokens=4096, novel_id=0, extra=None):
        return self.execute(
            "INSERT INTO model_presets(novel_id, slot, provider_id, model, temperature, "
            "max_tokens, extra) VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(novel_id, slot) DO UPDATE SET provider_id=excluded.provider_id, "
            "model=excluded.model, temperature=excluded.temperature, "
            "max_tokens=excluded.max_tokens, extra=excluded.extra, "
            "updated_at=datetime('now','localtime')",
            (novel_id, slot, provider_id, model, float(temperature),
             int(max_tokens), _jdumps(extra or {})))

    def log_usage(self, novel_id, slot, provider, model, prompt_tokens=0,
                  completion_tokens=0, latency_ms=0, success=True, error=""):
        total = int(prompt_tokens) + int(completion_tokens)
        return self.execute(
            "INSERT INTO llm_usage(novel_id, slot, provider, model, prompt_tokens, "
            "completion_tokens, total_tokens, latency_ms, success, error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (novel_id, slot, provider, model, int(prompt_tokens),
             int(completion_tokens), total, int(latency_ms), 1 if success else 0,
             _clean(error)))

    def usage_summary(self, novel_id=0):
        row = self.one(
            "SELECT COUNT(*) AS calls, COALESCE(SUM(total_tokens),0) AS tokens, "
            "COALESCE(SUM(CASE WHEN success=0 THEN 1 ELSE 0 END),0) AS failures "
            "FROM llm_usage WHERE novel_id=?", (novel_id,))
        by_slot = self.query(
            "SELECT slot, COUNT(*) AS calls, COALESCE(SUM(total_tokens),0) AS tokens "
            "FROM llm_usage WHERE novel_id=? GROUP BY slot ORDER BY tokens DESC",
            (novel_id,))
        return {"total": row, "by_slot": by_slot}

    # ================================================== 提示词

    def list_prompts(self, novel_id=0, category=None):
        sql = "SELECT * FROM prompts WHERE (novel_id=? OR novel_id=0) AND is_active=1"
        params = [novel_id]
        if category:
            sql += " AND category=?"
            params.append(category)
        return self.query(sql + " ORDER BY id", params)

    def save_prompt(self, novel_id, name, content, category="general"):
        ver = self.scalar(
            "SELECT COALESCE(MAX(version),0)+1 FROM prompts WHERE novel_id=? AND name=?",
            (novel_id, name), default=1)
        return self.execute(
            "INSERT INTO prompts(novel_id, name, category, content, version) "
            "VALUES (?,?,?,?,?)",
            (novel_id, name, category, _clean(content), ver))

    # ================================================== 全文检索

    def search(self, novel_id, keyword, limit=20):
        """中文检索：trigram 要求 >=3 字，不足时回退 LIKE。"""
        kw = _clean(keyword)
        if not kw:
            return []
        if len(kw) >= 3:
            try:
                rows = self.query(
                    "SELECT c.id AS chapter_id, c.chapter_number, c.title, "
                    "snippet(chapters_fts, 3, '<<', '>>', '…', 12) AS snippet, "
                    "bm25(chapters_fts) AS score "
                    "FROM chapters_fts JOIN chapters c ON c.id = chapters_fts.chapter_ref "
                    "WHERE chapters_fts MATCH ? AND chapters_fts.novel_id=? "
                    "AND c.deleted_at IS NULL ORDER BY score LIMIT ?",
                    ('"%s"' % kw, novel_id, int(limit)))
                if rows:
                    return rows
            except sqlite3.OperationalError:
                pass
        # LIKE 回退
        like = "%" + kw + "%"
        return self.query(
            "SELECT id AS chapter_id, chapter_number, title, "
            "substr(content, MAX(1, instr(content, ?) - 20), 60) AS snippet "
            "FROM chapters WHERE novel_id=? AND deleted_at IS NULL "
            "AND (content LIKE ? OR title LIKE ?) ORDER BY chapter_number LIMIT ?",
            (kw, novel_id, like, like, int(limit)))

    # ================================================== 上下文装配

    def build_context(self, novel_id, target_chapter=None, recent_events=12,
                      tail_chars=800):
        """为"推演下一段"或"生成下一章"装配上下文。

        v6 相比 v4 的差异：
          - 世界状态、世界时钟、活跃线索成为核心，不再有追读力/三线占比
          - 给上一章结尾的原文（tail_chars），而不是摘要
          - 角色只带与该章相关的目标，避免上下文爆炸
        """
        novel = self.get_novel(novel_id)
        if not novel:
            return None
        cur = novel.get("current_chapter") or 0
        target = target_chapter or (cur + 1)

        chapters = self.list_chapters(novel_id)
        prev = None
        for ch in chapters:
            if ch["chapter_number"] < target and ch.get("content"):
                prev = ch
            if ch["chapter_number"] >= target:
                break

        tail = ""
        if prev and prev.get("content"):
            tail = prev["content"][-int(tail_chars):]

        chars = self.list_characters(novel_id)
        goals = self.list_goals(novel_id=novel_id, status="active")
        goals_by_char = {}
        for g in goals:
            goals_by_char.setdefault(g["character_id"], []).append(g)

        recent = self.query(
            "SELECT * FROM events WHERE novel_id=? AND deleted_at IS NULL "
            "ORDER BY id DESC LIMIT ?", (novel_id, int(recent_events)))
        recent.reverse()
        for r in recent:
            r["structured"] = _jloads(r.get("structured"), {})
            r["open_threads"] = _jloads(r.get("open_threads"), [])

        ctx = {
            "novel": novel,
            "target_chapter": target,
            "world_clock": self.get_clock(novel_id),
            "world_state": self.get_world_state(novel_id),
            "world_entities": self.list_entities(novel_id, status="active"),
            "characters": chars,
            "goals_by_character": goals_by_char,
            "relations": self.list_relations(novel_id),
            "threads_active": self.list_threads(novel_id, status="active"),
            "threads_urgent": self.urgent_threads(novel_id, cur, window=5),
            "recent_events": recent,
            "timeline": self.list_timeline(novel_id, status="pending"),
            "pending_decisions": self.pending_decisions(novel_id),
            "prev_chapter": prev,
            "prev_tail": tail,
            "voice": self.list_voice(novel_id),
            "world_settings": self.get_world(novel_id),
        }
        return ctx

    def context_markdown(self, novel_id, target_chapter=None, **kw):
        """把上下文渲染成可读的 markdown，供人工/AI 审阅。"""
        ctx = self.build_context(novel_id, target_chapter=target_chapter, **kw)
        if not ctx:
            return "# 上下文不存在\n"
        n = ctx["novel"]
        L = []
        L.append("# 创作上下文 · %s" % n["title"])
        L.append("")
        L.append("## 一、世界基础")
        L.append("- 题材：%s ｜ 基调：%s ｜ 文风：%s" % (
            n.get("genre") or "-", n.get("tone") or "-", n.get("style") or "-"))
        L.append("- 前提：%s" % (n.get("premise") or "-"))
        L.append("- 初始张力：%s" % (n.get("initial_tension") or "-"))
        L.append("- 人机分工：%s ｜ 完结方式：%s" % (
            n.get("decision_mode"), n.get("completion_mode")))
        L.append("")

        clock = ctx["world_clock"]
        L.append("## 二、世界时钟")
        L.append("- 当前时间：%s" % (clock.get("current_time") or "（未设定）"))
        L.append("- 已推进步数：%s ｜ 粒度：%s" % (
            clock.get("total_ticks"), clock.get("granularity")))
        L.append("")

        if ctx["world_state"]:
            L.append("## 三、世界状态")
            for s in ctx["world_state"]:
                L.append("- [%s] %s = %s%s" % (
                    s["category"], s["key"], s["value"],
                    ("（%s）" % s["note"]) if s.get("note") else ""))
            L.append("")

        if ctx["world_entities"]:
            L.append("## 四、世界实体")
            for e in ctx["world_entities"]:
                L.append("- (%s) %s：%s" % (
                    e["entity_type"], e["name"], (e.get("description") or "")[:60]))
            L.append("")

        L.append("## 五、角色（%d 位）" % len(ctx["characters"]))
        for c in ctx["characters"]:
            gs = ctx["goals_by_character"].get(c["id"]) or []
            gt = "；".join("%s目标：%s" % (
                "长期" if g["goal_type"] == "long" else "短期", g["content"])
                for g in gs) or "（暂无目标，可由事件临时生成）"
            L.append("- [%s] %s ｜ 状态 %s ｜ 托管 %s" % (
                c["rank"], c["name"], c["status"], c["control_mode"]))
            L.append("    - 目标：%s" % gt)
            if c.get("decision_tendency"):
                L.append("    - 决策倾向：%s" % c["decision_tendency"])
            mem = c.get("memory") or []
            if mem:
                last = mem[-2:]
                L.append("    - 近期记忆：%s" % "；".join(
                    str(m.get("text", m)) for m in last))
        L.append("")

        if ctx["threads_active"]:
            L.append("## 六、活跃线索")
            for t in ctx["threads_active"]:
                L.append("- [张力 %s/%s] %s（第 %s 章埋，目标第 %s 章）" % (
                    t["tension_level"], t["importance"], t["title"],
                    t["planted_chapter"], t["target_chapter"]))
            L.append("")
        if ctx["threads_urgent"]:
            L.append("### ⚠ 急需回收")
            for t in ctx["threads_urgent"]:
                L.append("- %s（目标第 %s 章）" % (t["title"], t["target_chapter"]))
            L.append("")

        if ctx["pending_decisions"]:
            L.append("## 七、待决策")
            for d in ctx["pending_decisions"]:
                L.append("- #%s [%s] %s ｜ 主体：%s" % (
                    d["id"], d["trigger_type"], d["title"], d["actor_name"]))
            L.append("")

        if ctx["recent_events"]:
            L.append("## 八、近期事件")
            for e in ctx["recent_events"]:
                L.append("- [%s] %s：%s" % (
                    e["event_type"], e["title"], (e.get("description") or "")[:70]))
                for th in (e.get("open_threads") or []):
                    L.append("    - 悬置线索：%s" % (
                        th.get("title") if isinstance(th, dict) else th))
            L.append("")

        if ctx["timeline"]:
            L.append("## 九、时间线锚点")
            for t in ctx["timeline"]:
                L.append("- %s%s" % (t["time_anchor"],
                                     ("（倒计时：%s）" % t["countdown_name"])
                                     if t["countdown_name"] else ""))
            L.append("")

        if ctx["world_settings"]:
            L.append("## 十、世界设定")
            for w in ctx["world_settings"]:
                L.append("- [%s] %s：%s" % (
                    w["category"], w["name"], (w.get("content") or "")[:70]))
            L.append("")

        if ctx["voice"]:
            L.append("## 十一、作者声音")
            for v in ctx["voice"]:
                L.append("- [%s] %s" % (v["category"], v["content"]))
            L.append("")

        if ctx["prev_chapter"]:
            L.append("## 十二、上一章结尾（原文，供承接）")
            L.append("> 第 %s 章《%s》" % (
                ctx["prev_chapter"]["chapter_number"], ctx["prev_chapter"]["title"]))
            L.append("")
            L.append(ctx["prev_tail"])
            L.append("")
        return "\n".join(L)

    # ================================================== 统计与体检

    def stats(self, novel_id):
        novel = self.get_novel(novel_id)
        if not novel:
            return None
        chapters = self.list_chapters(novel_id)
        published = [c for c in chapters if c["status"] == "published"]
        chars = self.list_characters(novel_id)
        return {
            "novel": novel,
            "chapter_count": len(chapters),
            "published_count": len(published),
            "total_words": sum(c.get("word_count") or 0 for c in chapters),
            "avg_words": int(
                sum(c.get("word_count") or 0 for c in chapters) / len(chapters)
            ) if chapters else 0,
            "character_count": len(chars),
            "alive_count": len([c for c in chars if c["status"] == "alive"]),
            "entity_count": len(self.list_entities(novel_id)),
            "event_count": self.scalar(
                "SELECT COUNT(*) FROM events WHERE novel_id=? AND deleted_at IS NULL",
                (novel_id,), default=0),
            "active_thread_count": len(self.list_threads(novel_id, status="active")),
            "urgent_thread_count": len(
                self.urgent_threads(novel_id, novel.get("current_chapter") or 0)),
            "pending_decision_count": len(self.pending_decisions(novel_id)),
            "goal_count": len(self.list_goals(novel_id=novel_id, status="active")),
            "world_state_count": self.scalar(
                "SELECT COUNT(*) FROM world_state WHERE novel_id=?",
                (novel_id,), default=0),
            "clock": self.get_clock(novel_id),
            "usage": self.usage_summary(novel_id),
        }

    def health(self):
        con = self.conn
        integrity = con.execute("PRAGMA integrity_check").fetchone()[0]
        fk_broken = len(con.execute("PRAGMA foreign_key_check").fetchall())
        tables = self.scalar(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'", default=0)
        return {
            "ok": integrity == "ok" and fk_broken == 0,
            "db": self.db_path,
            "integrity": integrity,
            "foreign_keys_broken": fk_broken,
            "table_count": tables,
            "version": self.scalar(
                "SELECT MAX(version) FROM schema_migrations", default=0),
        }


# ============================================================ 便捷入口

_DB_SINGLETON = None


def get_db(db_path=DEFAULT_DB):
    """获取默认连接（进程内复用）。Web 端请用 open_db 每请求新建。"""
    global _DB_SINGLETON
    if _DB_SINGLETON is None or _DB_SINGLETON.db_path != os.path.abspath(db_path):
        _DB_SINGLETON = NovelDatabase(db_path)
    return _DB_SINGLETON


def open_db(db_path=DEFAULT_DB):
    """新建连接（Web 请求级）。"""
    return NovelDatabase(db_path)


def ensure_db(db_path=DEFAULT_DB):
    """确保数据库存在，不存在则建库。"""
    if not os.path.exists(db_path):
        import sys
        sys.path.insert(0, _HERE)
        from migrate import migrate
        migrate(db_path, verbose=False)
    return db_path
