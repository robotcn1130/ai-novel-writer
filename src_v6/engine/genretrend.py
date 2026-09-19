"""热门题材趋势：联网抓取 + 语义库合并 + 分级降级。

用法：
    import engine.genretrend as GT
    cards = GT.trends(audience="male")            # 用快照/内置，不联网
    cards = GT.trends(audience="male", refresh=True)   # 先联网抓，抓不到自动降级
    block = GT.genre_block("都市高武", hint="要有内鬼")

■ 为什么是"双源"而不是"全靠抓"

    只抓网页：源站改一次版，用户的创建流程就断了。
    只内置：数据永远停在打包那天，"热门"两个字名不副实。

    所以分两层：
      · **量化热度**（哪个题材在涨）—— 联网抓，抓得到就用最新的，按标签名覆盖内置值；
        抓不到就依次退到本地快照 → 内置基准值。**任何一层失败都不影响用户往下走。**
      · **题材语义**（这个世界该怎么搭）—— 手工整理，存在 data/genre_trends.json。
        标签名本身不含建模信息：模型看到"都市高武"三个字，不会知道要先定
        「武者等级体系」和「普通人是否知情」。这两条轴定不下来，世界就没法推演。

■ 为什么排序用"在读量"而不是"榜单名次"

    榜单名次每天变，而且不同榜单口径不同（月票是付费、在读是免费）。
    fanqiehub 的标签页给的是**每个标签的累计在读量**，口径统一、可横向比较，
    正好是"这个题材有多少人在看"的直接度量。名次会骗人，量级不会。

■ 失败形态（本项目最怕的那种，所以这里专门防）

    「静默返回空列表」= 用户点开创建世界，看到一片空白，不知道为什么。
    所以 trends() 永远返回一个 source 描述（live / cache / builtin + 原因 + 时间），
    前端把它显示出来——**宁可告诉用户"这是 9 月 18 日的快照"，也不要让他以为
    世界上只有这几个题材**。
"""
import io
import json
import os
import re
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC_V6 = os.path.dirname(_HERE)
ROOT_DIR = os.path.dirname(_SRC_V6)

DATA_PATH = os.path.join(_SRC_V6, "data", "genre_trends.json")
CACHE_DIR = os.path.join(ROOT_DIR, ".workbuddy", "cache")
CACHE_PATH = os.path.join(CACHE_DIR, "genre_trends_live.json")

# 抓取超时（秒）。宁可贵一点也别把用户的"创建世界"卡住——
# 这是交互路径上的同步调用，超过这个时间就当源站不可用。
FETCH_TIMEOUT = 8

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

_CACHE = {}          # {"data":..., "mtime":...}


# ============================================================ 内置语义库

def load():
    """读内置题材语义库（带 mtime 缓存，改了文件会自动重读）。"""
    try:
        mt = os.path.getmtime(DATA_PATH)
    except OSError:
        mt = 0
    hit = _CACHE.get("data")
    if hit is not None and _CACHE.get("mtime") == mt:
        return hit
    try:
        with io.open(DATA_PATH, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        # 数据文件坏了也要能起服务：给一个空壳，上层会报"题材库不可用"。
        data = {"genres": [], "audience_map": {}, "live_sources": [],
                "_load_error": "题材库文件无法解析"}
    _CACHE["data"] = data
    _CACHE["mtime"] = mt
    return data


def genres():
    return list(load().get("genres") or [])


def genre_by_key(key):
    k = _s(key)
    for g in genres():
        if g.get("key") == k:
            return g
    return None


def genre_by_name(name):
    n = _s(name)
    if not n:
        return None
    for g in genres():
        if g.get("name") == n or n in (g.get("aliases") or []):
            return g
    # 再退一步：模糊（用户可能是从别处复制来的名字）
    for g in genres():
        gn = _s(g.get("name"))
        if gn and (gn in n or n in gn):
            return g
    return None


def resolve_genre(value):
    """`value` 可以是 key，也可以是名字。拿不到就返回 None。"""
    v = _s(value)
    if not v:
        return None
    g = genre_by_key(v)
    if g:
        return g
    return genre_by_name(v)


# ============================================================ 读数解析

def parse_heat_number(num, unit=""):
    """把 (3.0, '亿') / (7788.5, '万') 折成统一的「万」。

    统一口径是排序的前提：不折算的话，"3.0亿"和"7788.5万"直接比大小会得出
    完全相反的结论。
    """
    try:
        v = float(num)
    except (TypeError, ValueError):
        return 0.0
    u = _s(unit)
    if u == "亿":
        return v * 10000.0
    if u == "千":
        return v * 0.1
    return v          # 万 / 空


_TAG_HEAT_RE = re.compile(
    r">([^<>]{1,14})</[a-zA-Z0-9]+>\s*<[^>]*>\s*"
    r"([0-9]+(?:\.[0-9]+)?)\s*([亿万])")


def parse_tags_heat(html):
    """从 HTML 里抓「标签名 + 读数」。

    页面结构是 `<span>穿越</span> … <span>3.0亿</span>`。
    这里**不做完整 HTML 解析**（不值得为一次抓取引依赖），
    只认这一种相邻结构——源站要是改版，正则失配会表现为"抓到 0 条"，
    上层据此降级到快照，不会当成"世界上没有热门题材"。
    """
    out = []
    if not html:
        return out
    for m in _TAG_HEAT_RE.finditer(html):
        tag = _s(m.group(1))
        if not tag or len(tag) > 14:
            continue
        # 标签名必须是正常的中文/字母数字词，滤掉标签栏里的导航词
        if not re.fullmatch(r"[\u4e00-\u9fa5A-Za-z0-9·\-]+", tag):
            continue
        out.append({"tag": tag, "heat": parse_heat_number(m.group(2), m.group(3))})
    return out


# ============================================================ 联网抓取

def _fetch(url, timeout=FETCH_TIMEOUT):
    """取一个 URL 的文本。失败抛异常，由调用方接住（不在这里吞）。"""
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept-Language": "zh-CN,zh;q=0.9",
        "Accept": "text/html,application/xhtml+xml",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return raw.decode("utf-8", "replace")


def fetch_live(timeout=FETCH_TIMEOUT):
    """按 live_sources 抓一遍。返回 (heat_by_tag, report)。

    **永不抛异常**：每个源的失败都记进 report，全失败时 heat_by_tag 为空字典。
    这是刻意的——抓不到网不该让"创建世界"这个动作失败。
    """
    heat, report = {}, []
    for src in (load().get("live_sources") or []):
        if not src.get("enabled", True):
            continue
        name = _s(src.get("name")) or _s(src.get("key"))
        t0 = time.time()
        try:
            html = _fetch(_s(src.get("url")), timeout=timeout)
            rows = parse_tags_heat(html)
            if not rows:
                report.append({"key": src.get("key"), "name": name, "ok": False,
                               "error": "页面结构变了，没抓到任何标签读数"})
                continue
            for r in rows:
                # 同一个标签多源命中时取较大值：源站口径不同，取大不取小
                # 是"这个题材确实有人在看"的保守判断。
                prev = heat.get(r["tag"])
                if prev is None or r["heat"] > prev:
                    heat[r["tag"]] = r["heat"]
            report.append({"key": src.get("key"), "name": name, "ok": True,
                           "count": len(rows),
                           "seconds": round(time.time() - t0, 2)})
        except Exception as e:
            report.append({"key": src.get("key"), "name": name, "ok": False,
                           "error": "%s: %s" % (type(e).__name__, str(e)[:120])})
    return heat, report


def _write_cache(heat, report):
    try:
        if not os.path.isdir(CACHE_DIR):
            os.makedirs(CACHE_DIR)
        with io.open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                       "heat": heat, "report": report},
                      f, ensure_ascii=False, indent=1)
        return True
    except Exception:
        return False


def _read_cache():
    try:
        with io.open(CACHE_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d.get("heat"), dict) and d["heat"]:
            return d
    except Exception:
        pass
    return None


def heat_map(refresh=False, timeout=FETCH_TIMEOUT):
    """拿"当前该用哪份热度"，并说明它的来源。

    返回 (heat_by_tag, source)：
      source = {"kind": "live"|"cache"|"builtin", "label":…, "note":…, "at":…, "report":[…]}

    refresh=True 时才联网；否则直接用本地快照/内置值——列表页每次刷新都联网
    会让"选题材"这个动作变慢，而热度一天变一次就够了。
    """
    if refresh:
        heat, report = fetch_live(timeout=timeout)
        if heat:
            _write_cache(heat, report)
            ok = [r for r in report if r.get("ok")]
            return heat, {
                "kind": "live", "label": "联网实时",
                "at": time.strftime("%Y-%m-%d %H:%M"),
                "note": "已从 %d 个源抓到 %d 个标签的在读量。" % (len(ok), len(heat)),
                "report": report}
        # 抓不到：把失败原因如实带出去，别假装成功
        why = "；".join("%s（%s）" % (r.get("name"), r.get("error"))
                        for r in report if not r.get("ok")) or "没有启用的抓取源"
        cached = _read_cache()
        if cached:
            return cached["heat"], {
                "kind": "cache", "label": "本地快照",
                "at": cached.get("fetched_at") or "",
                "note": "联网刷新失败，用的是上次抓到的数据。原因：%s" % why,
                "report": report}
        return {}, {"kind": "builtin", "label": "内置基准",
                    "at": load().get("updated_at") or "",
                    "note": "联网刷新失败，用的是内置基准热度。原因：%s" % why,
                    "report": report}

    cached = _read_cache()
    if cached:
        return cached["heat"], {
            "kind": "cache", "label": "本地快照",
            "at": cached.get("fetched_at") or "",
            "note": "上次联网抓到的数据。点「联网刷新」可以更新。",
            "report": []}
    return {}, {"kind": "builtin", "label": "内置基准",
                "at": load().get("updated_at") or "",
                "note": "内置基准热度。点「联网刷新」可以拉取当前在读量。",
                "report": []}


# ============================================================ 热度归属

# 泛标签黑名单：这些词横跨多个题材，**不能代表任何一个具体题材**。
# 踩过的坑：原先用"标签名是题材名的一部分"做模糊匹配，结果
#   都市重生 → 命中「重生」1.1 亿
#   都市灵异 → 命中「都市」7788 万
# 两个题材因此虚高到榜首，排序彻底失真——而且**不报错**，
# 用户只会以为"'都市重生'原来这么火"。所以这里显式拒绝，
# 让误配表现为"没有实时数据"（退回内置基准值）而不是一个错的数字。
GENERIC_TAGS = {
    "穿越", "系统", "重生", "都市", "爽文", "无敌", "多女主", "无女主", "单女主",
    "日常", "直播", "开局", "同人", "空间", "魂穿", "轻松", "恋爱", "求生",
    "末日", "异能", "打脸", "双洁", "慢热", "反派", "学霸", "天才", "1v1",
    "位面", "女强", "宗门", "热血", "校花", "大佬", "搞笑", "搞笑轻松",
    "男频衍生", "衍生", "动漫衍生", "架空", "玄幻", "仙侠", "历史",
    "现代言情", "古代言情", "幻想言情",
}


def _heat_for(genre, heat_by_tag):
    """把一个题材映射到某个标签的在读量上。

    只认**显式声明**的 `live_tag`（外加"题材名与标签完全同名"这一条），
    **不做模糊匹配**——模糊匹配看起来更聪明，实际会把泛标签的数字栽到
    具体题材头上。名字对不上就返回 None，让调用方退回内置基准值：
    一个来源不明的数字比"没有实时数据"危险得多。

    返回 (heat, tag)；没有实时数据时返回 (None, "")。
    """
    if not heat_by_tag:
        return None, ""
    for tag in (_s(genre.get("live_tag")), _s(genre.get("name"))):
        if not tag or tag in GENERIC_TAGS:
            continue
        if tag in heat_by_tag:
            return heat_by_tag[tag], tag
    return None, ""


# ============================================================ 对外：卡片列表

def trends(audience=None, refresh=False, limit=None, timeout=FETCH_TIMEOUT):
    """题材卡片列表（按热度降序）。

    `audience` 取值 male / female / both / None(全部)。
    "both" 的题材在 male 和 female 两个页签下都出现——它们本来就是通用赛道。
    """
    heat_by_tag, source = heat_map(refresh=refresh, timeout=timeout)
    aud = _s(audience)
    rows = []
    for g in genres():
        ga = _s(g.get("audience")) or "both"
        if aud and aud in ("male", "female") and ga not in (aud, "both"):
            continue
        live_heat, live_tag = _heat_for(g, heat_by_tag)
        effective = live_heat if live_heat else float(g.get("heat") or 0)
        rows.append({
            "key": g.get("key"), "name": _s(g.get("name")),
            "audience": ga, "platforms": list(g.get("platforms") or []),
            "heat": round(effective, 1),
            "heat_label": heat_label(effective),
            "heat_is_live": live_heat is not None,
            "heat_tag": live_tag,
            "heat_note": _s(g.get("heat_note")),
            "base_heat": g.get("heat"),
            "why_hot": _s(g.get("why_hot")),
            "world_axes": list(g.get("world_axes") or []),
            "tropes": list(g.get("tropes") or []),
            "protagonists": list(g.get("protagonists") or []),
            "opening_hooks": list(g.get("opening_hooks") or []),
            "world_state_seeds": list(g.get("world_state_seeds") or []),
            "pitfalls": list(g.get("pitfalls") or []),
            "examples": list(g.get("examples") or []),
        })
    rows.sort(key=lambda x: -(x["heat"] or 0))
    if limit:
        rows = rows[:int(limit)]

    # 相对热度（0-100）：画热度条用。**必须以本次返回集合的最大值为基准**，
    # 否则筛了"女频"之后，条子会集体缩短到看不出差别。
    top = max([r["heat"] for r in rows] or [0]) or 1
    for r in rows:
        r["heat_pct"] = int(round(r["heat"] / top * 100))

    return {"genres": rows, "source": source}


def heat_label(heat):
    """把「万」转成好读的中文量级。"""
    try:
        v = float(heat)
    except (TypeError, ValueError):
        return ""
    if v >= 10000:
        return "%.1f 亿" % (v / 10000.0)
    return "%.0f 万" % v


# ============================================================ 对外：提示词材料

def genre_block(genre, hint="", include_siblings=3):
    """把选中题材渲染成模型能用的材料。

    `include_siblings`：顺带告诉模型"同平台还有哪几个题材在火"，
    因为 2026 的爆款公式是**跨界混搭**（借流量的壳，装硬核的馅）——
    模型知道邻赛道是什么，才可能给出有辨识度的方案，而不是"又一个纯类型"。
    """
    g = resolve_genre(genre) if not isinstance(genre, dict) else genre
    if not g:
        return ""
    L = ["## 选定题材：%s" % _s(g.get("name"))]
    aud = {"male": "男频", "female": "女频", "both": "男频女频通用"}.get(
        _s(g.get("audience")), "通用")
    L.append("受众：%s　主要平台：%s" % (aud, "、".join(g.get("platforms") or []) or "不限"))
    if g.get("why_hot"):
        L.append("为什么这个题材现在有人在看：%s" % _s(g.get("why_hot")))

    def _sec(title, items, note=""):
        items = [x for x in (items or []) if _s(x)]
        if not items:
            return
        L.append("\n### %s" % title)
        if note:
            L.append("（%s）" % note)
        for x in items:
            L.append("- %s" % _s(x))

    _sec("这个世界必须先定死的几条轴", g.get("world_axes"),
         "这几条定不下来，世界就没法推演。请在世界前提里交代清楚，"
         "并在初始世界状态里给出可计算的数字。")
    _sec("这个题材里读者认的套路", g.get("tropes"),
         "选一到两个用，不要全部堆上——堆满了就是同质化。")
    _sec("主角原型", g.get("protagonists"))
    _sec("开篇钩子参考", g.get("opening_hooks"),
         "这是给写手用的，不必写进世界设定。")
    _sec("该避免的坑", g.get("pitfalls"))

    if include_siblings:
        same = [x for x in genres()
                if x.get("key") != g.get("key")
                and _s(x.get("audience")) in (_s(g.get("audience")), "both")]
        same.sort(key=lambda x: -(x.get("heat") or 0))
        if same:
            L.append("\n### 同期在火的邻接题材（供混搭参考）")
            L.append("、".join("%s（%s）" % (_s(x.get("name")),
                                            heat_label(x.get("heat")))
                              for x in same[:include_siblings]))

    if _s(hint):
        L.append("\n## 作者的额外要求（优先级高于上面的套路）\n%s" % _s(hint))
    return "\n".join(L)


def _s(v):
    return "" if v is None else str(v).strip()
