# -*- coding: utf-8 -*-
"""
v6.0 Web 管理台 · 零依赖标准库服务端。

设计约束（方案 11.7）：
  - 只用标准库（http.server / json / sqlite3），不引入任何第三方包
  - 每个请求新建数据库连接，请求结束即关闭（WAL + busy_timeout 已够用）
  - 长任务（推演 / 生成）一律放后台线程，前端轮询 /api/job/<id>
  - 长任务结果通过 job 表在内存里传递，进程重启即丢（可接受）

四个视图对应四组 API：
  世界视图  /api/novel  /api/world  /api/clock  /api/state  /api/entities
  推演树    /api/tree  /api/tree/*（原决策台，v6.1 起改为分支剧情树）
  章节视图  /api/chapters  /api/chapter/*  /api/events  /api/threads
  管理视图  /api/providers  /api/slots  /api/voice  /api/seeds  /api/completion

用法：
  python src_v6/webui/server.py               # 默认 8787
  python src_v6/webui/server.py --port 9000
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC_V6 = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_SRC_V6)
for _p in (_SRC_V6, os.path.join(_SRC_V6, "core")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core import database as DB          # noqa: E402
from llm import client as LLM            # noqa: E402
from llm.router import LLMRouter, SLOT_LABELS   # noqa: E402
from engine.tree import StaleEditError   # noqa: E402

DB_PATH = os.path.join(_ROOT, "sql", "novel.db")
STATIC_DIR = os.path.join(_HERE, "static")

MAX_BODY = 2 * 1024 * 1024


# ============================================================ 异常

class ApiError(Exception):
    def __init__(self, message, status=400, **extra):
        super().__init__(message)
        self.message = message
        self.status = status
        self.extra = extra


def _require(cond, message, status=400):
    if not cond:
        raise ApiError(message, status)


# ============================================================ 后台任务池

class JobPool:
    """极简任务池。job 结构：
    {id, kind, status(running|done|error), progress, log[], result, error,
     created_at, finished_at}
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._jobs = {}
        self._seq = 0

    def submit(self, kind, fn):
        with self._lock:
            self._seq += 1
            jid = "j%d" % self._seq
            job = {"id": jid, "kind": kind, "status": "running", "progress": 0,
                   "log": [], "result": None, "error": "",
                   "created_at": time.time(), "finished_at": None}
            self._jobs[jid] = job
        t = threading.Thread(target=self._run, args=(jid, fn), daemon=True)
        t.start()
        return jid

    def _run(self, jid, fn):
        job = self._jobs[jid]

        def emit(msg, progress=None):
            with self._lock:
                job["log"].append({"t": time.time(), "msg": str(msg)})
                if progress is not None:
                    job["progress"] = progress

        # LLM 层每跟模型交换一次数据都会往这里播报一条，
        # 前端轮询就能看到"正在请求 / 已收到 / 用时多少"，不必猜是不是死机
        LLM.set_progress_sink(emit)
        try:
            res = fn(emit)
            with self._lock:
                job["status"] = "done"
                job["result"] = res
                job["progress"] = 100
        except Exception as e:                      # noqa: BLE001
            with self._lock:
                job["status"] = "error"
                job["error"] = "%s: %s" % (type(e).__name__, e)
                job["log"].append({"t": time.time(),
                                   "msg": traceback.format_exc()[-1200:]})
        finally:
            LLM.clear_progress_sink()
            job["finished_at"] = time.time()

    def get(self, jid, since=0):
        with self._lock:
            job = self._jobs.get(jid)
            if not job:
                return None
            out = dict(job)
            out["log"] = job["log"][int(since):]
            return out

    def prune(self, older_than=3600):
        now = time.time()
        with self._lock:
            dead = [k for k, v in self._jobs.items()
                    if v["finished_at"] and now - v["finished_at"] > older_than]
            for k in dead:
                self._jobs.pop(k, None)


JOBS = JobPool()


# ============================================================ 路由表

ROUTES = []


def route(method, pattern):
    def deco(fn):
        ROUTES.append((method, re.compile("^" + pattern + "$"), fn))
        return fn
    return deco


def dispatch(method, path, query, body):
    """返回 (status, payload)。path 已去掉 query。"""
    allowed = []
    for m, rx, fn in ROUTES:
        mt = rx.match(path)
        if not mt:
            continue
        if m != method:
            allowed.append(m)
            continue
        params = {k: urllib.parse.unquote(v) for k, v in mt.groupdict().items()}
        out = fn(query, body, **params)
        # 处理函数可以只回 payload（默认 200），也可以回 (status, payload)
        # 表示非 200 的语义比如「需要用户二次确认」的 409。
        # 不认这个元组的话，payload 会变成 (409, {...}) 直接塞进 _send，
        # 既不是 dict 也不是 str，body 就写成了空——前端只看到 200 + 空。
        if isinstance(out, tuple) and len(out) == 2 and isinstance(out[0], int):
            return out[0], out[1]
        return 200, out
    if allowed:
        raise ApiError("方法不允许：%s（可用：%s）" % (method, "/".join(allowed)), 405)
    raise ApiError("接口不存在：%s" % path, 404)


# ============================================================ 通用工具

def _db():
    return DB.open_db(DB_PATH)


def _novel_id(q, body=None, default=0):
    v = q.get("novel_id") or (body or {}).get("novel_id")
    return int(v) if v else default


def _as_int(v, field="参数"):
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ApiError("%s 必须是整数：%r" % (field, v))


def _as_opt_int(v, field="参数", default=None):
    """可选整数：**缺省/空串返回 default，不报错**。

    踩过：列表接口（知识矩阵 / 候选事实 / 冲突列表）的过滤参数（character_id /
    chapter / branch_id）本来就是可选的——不传 = 看全部。用 `_as_int` 收口会把
    "不传"当成"传了 None"，直接在 400 上把整个列表页打掉，前端一打开就是报错。
    只在**用户确实传了一个非数字**时才抛，那才是真错。
    """
    if v is None or v == "" or v == "null":
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        raise ApiError("%s 必须是整数：%r" % (field, v))


def _flag(value, default=False):
    """解析 query/body 里的布尔。**参数缺省时返回 default**。

    不能直接用 core.database._truthy：它对 None 一律返回 False，
    于是 `active_only` 这种"默认开"的开关在参数缺省时会被当成关，
    "默认只给 active" 的语义静默失效。
    """
    if value is None or value == "":
        return bool(default)
    if isinstance(value, bool):
        return value
    s = str(value).strip().lower()
    if s in ("1", "true", "yes", "on", "y"):
        return True
    if s in ("0", "false", "no", "off", "n"):
        return False
    return bool(default)


def _router(db, novel_id=0):
    return LLMRouter(db, novel_id)


def _view_character(db, c):
    goals = db.list_goals(character_id=c["id"], status="active")
    gtype = {}
    for g in goals:
        gtype.setdefault(g["goal_type"], []).append(g)
    return {
        "id": c["id"], "name": c["name"], "alias": c.get("alias") or "",
        "rank": c["rank"], "role_tag": c.get("role_tag") or "",
        "status": c["status"], "control_mode": c.get("control_mode") or "ai",
        "current_location": c.get("current_location") or "",
        # v6.4 结构化位置：地图按 location_id 分组，精度决定画到哪一级
        "location_id": c.get("location_id"),
        "location_precision": c.get("location_precision") or "unknown",
        "location_updated_at": c.get("location_updated_at") or "",
        "personality": c.get("personality") or "",
        "background": c.get("background") or "",
        "speech_style": c.get("speech_style") or "",
        "decision_tendency": c.get("decision_tendency") or "",
        "tracker": c.get("tracker") or {},
        "memory": c.get("memory") or [],
        "goals_short": [g["content"] for g in gtype.get("short", [])],
        "goals_long": [g["content"] for g in gtype.get("long", [])],
        "goal_count": len(goals),
    }


def _view_decision(db, d):
    from decide.scheduler import TRIGGER_LABELS
    return {
        "id": d["id"], "title": d["title"], "situation": d["situation"],
        "stakes": d["stakes"], "trigger_type": d["trigger_type"],
        "trigger_label": TRIGGER_LABELS.get(d["trigger_type"], d["trigger_type"]),
        "actor_type": d["actor_type"], "actor_name": d["actor_name"],
        "chapter_ref": d["chapter_ref"] or 0,
        "options": d.get("options") or [],
        "allow_freeform": bool(d.get("allow_freeform")),
        "resolved": bool(d.get("resolved")),
        "chosen_option": d.get("chosen_option", -1),
        "chosen_content": d.get("chosen_content") or "",
        "chosen_by": d.get("chosen_by") or "",
        "impact": d.get("impact") or {},
        "note": d.get("note") or "",
        "needs_user": None,
    }


# ============================================================ 元信息

@route("GET", r"/api/meta")
def api_meta(q, b):
    from llm import client as LLM
    return {
        "novels": _db().list_novels(),
        "providers": LLM.list_providers(),
        "slots": SLOT_LABELS,
        "shapes": ["scene", "montage", "interlude", "aftermath", "ensemble",
                   "transition"],
        "shape_labels": {
            "scene": "场景（单一时空连续推进）",
            "montage": "蒙太奇（多场景快切）",
            "interlude": "间章（换个视角看同一件事）",
            "aftermath": "余波（事件之后的静默）",
            "ensemble": "群像（多条线并行）",
            "transition": "过渡（时空跨越）",
        },
        "modes": {"viewer": "观影（全 AI 决策）",
                  "director": "导演（主角归你）",
                  "tabletop": "跑团（全部你来定）"},
        "control_modes": {"ai": "AI 托管", "user": "你来做主",
                          "auto_delegate": "AI 代管（可随时收回）"},
        "voice_categories": {"sensory": "感官偏好", "syntax": "句式癖好",
                             "dialogue": "对话习惯", "taboo": "禁忌",
                             "motif": "反复出现的意象"},
        "seed_types": {"conflict": "冲突", "mystery": "谜团",
                       "opportunity": "机会", "disaster": "灾变",
                       "encounter": "相遇", "revelation": "揭示",
                       "other": "其他"},
        "roles": {"pov": "视角", "main": "主要", "supporting": "配角",
                  "cameo": "客串", "mentioned": "被提及"},
    }


@route("GET", r"/api/health")
def api_health(q, b):
    db = _db()
    return db.health()


@route("POST", r"/api/health/purge")
def api_purge_orphans(q, b):
    db = _db()
    res = db.purge_orphans()
    res["health"] = db.health()
    return res


# ============================================================ 小说 / 世界

@route("GET", r"/api/novels")
def api_novels(q, b):
    return {"novels": _db().list_novels()}


# v6.7 起新增 9 张表。老库不迁移的话，代码一查就是 "no such table"，
# 表现为整屏 traceback 而用户不知道该怎么办。这里把"还差几张表"算出来，
# 前端开机就能横幅提示，而不是等某个接口炸了才暴露。
_REQUIRED_TABLES = ("facts", "fact_sources", "fact_candidates", "fact_conflicts",
                    "character_knowledge", "branches", "world_rules",
                    "chapter_event_links", "world_snapshots")


@route("GET", r"/api/schema")
def api_schema(q, b):
    db = _db()
    try:
        # ★ db.query 返回的是 dict 列表（不是元组）→ 必须按列名取。
        #   写 r[0] 会抛 KeyError: 0，而 str(KeyError(0)) == "0"，
        #   报出来是个孤零零的 "0"，极难排查。
        have = {r["name"] for r in db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    except Exception as e:                           # noqa: BLE001
        return {"ok": False, "error": str(e)}
    missing = [t for t in _REQUIRED_TABLES if t not in have]
    try:
        from core import migrate as _M
        want = int(getattr(_M, "SCHEMA_VERSION", 7))
    except Exception:                                # noqa: BLE001
        want = 7
    cur = 0
    if "schema_migrations" in have:
        try:
            cur = int(db.scalar(
                "SELECT MAX(version) FROM schema_migrations", default=0) or 0)
        except Exception:                            # noqa: BLE001
            cur = 0
    return {"ok": not missing, "current_version": cur, "want_version": want,
            "missing_tables": missing, "table_count": len(have),
            "needs_migration": bool(missing),
            "command": "python src_v6/core/migrate.py" if missing else ""}


# ============================================================ 技能库（v6.9）
#
# 页面入口在「技能库」页签。这一组接口管四件事：看远程库有什么、
# 装到本地、卸掉、把某个技能绑到某个 AI 调用点上。
#
# 绑定的落点是 app_config 的 skills.bindings 键（JSON），**不新建表**——
# 新建表就要用户重跑 migrate.py，而没跑迁移的库一进来就是满屏
# "no such table"，等于功能没上线。
#
# 联网的两个（browse / install）走标准库 urllib，失败只回人话错误，
# 绝不抛异常打断页面：浏览失败最多这一栏空着，别的照用。

@route("GET", r"/api/skills/local")
def api_skills_local(q, b):
    from engine import skillhub as SH
    return {"ok": True, "skills": SH.scan_local(),
            "dirs": {"user": SH.USER_DIR, "project": SH.PROJECT_DIR}}


@route("GET", r"/api/skills/refs")
def api_skills_refs(q, b):
    """批量查「项目里哪里引用了各个技能」。

    单独一个接口是有意的：它要扫源码+docs，比别的都慢。
    前端把它和主数据分开拉、分开失败——引用信息读不到，
    技能库页照样能用（最多是列表里的引用小字不显示）。
    """
    from engine import skillhub as SH
    out = []
    for s in SH.scan_local():
        out.append({"name": s["name"], "dir": s["dir"],
                    "hits": SH.scan_references(s["name"])[:8]})
    return {"ok": True, "refs": out}


@route("GET", r"/api/skills/detail")
def api_skills_detail(q, b):
    """一个技能的全部信息：正文预览、绑到哪些调用点、项目里哪里引用了它。

    为什么要有这个接口：技能列表里只看得见名字和说明，用户点一下
    才发现「哦这个我绑了」，或者「原来提示词里早就点名用它」。
    这些信息分散在三处（SKILL.md / app_config / 源码），一次给全。
    """
    from engine import skillhub as SH
    db = _db()
    try:
        name = (q.get("name") or "").strip()
        _require(name, "缺少 name")
        sk = SH.find_skill(name)
        _require(sk, "本地没有这个技能：%s" % name, 404)
        body = SH.read_body(sk["path"])
        # 绑到哪些档位
        bd = SH.load_bindings(db)
        bound = []
        for s in SH.slot_catalog():
            cur = bd.get(s["slot"]) or {}
            if (cur.get("skill") or "") in (name, sk["dir"]):
                s["enabled"] = bool(cur.get("enabled"))
                bound.append(s)
        # 项目里哪里引用了它（两份历史：源码+文档，当前库的行）
        refs = SH.scan_references(name)
        db_refs = []
        try:
            db_refs = SH.db_reference_scanner(DB_PATH, name)
        except Exception:                                # noqa: BLE001
            db_refs = []
        return {"ok": True, "skill": sk, "body": body,
                "body_preview": body[:4000],
                "truncated": len(body) > 4000,
                "bound_slots": bound, "refs": refs, "db_refs": db_refs[:40],
                "max_inject_chars": SH.MAX_INJECT_CHARS}
    finally:
        db.close()


@route("POST", r"/api/skills/import_plan")
def api_skills_import_plan(q, b):
    """把「用户要导入的东西」编成计划，**先不落盘**。

    两种输入：
      folder — 本机一个文件夹的绝对路径（只读扫一遍）
      zip/md — 前端读成 base64 传来的文件内容

    为什么分两步：导入前必须让用户看清三件事——装成什么名字、
    几个文件、审计有没有命中。一键直装的代价是用户永远不知道
    自己往机器里放了什么。
    """
    from engine import skillhub as SH
    kind = (b.get("kind") or "folder").strip()
    if kind == "folder":
        path = (b.get("path") or "").strip().strip('"')
        _require(path, "请填写文件夹路径")
        r = SH.plan_folder(path)
    elif kind in ("zip", "md"):
        _require(b.get("data"), "请选择要导入的文件")
        r = SH.plan_zip(b.get("data"), kind=kind)
    else:
        raise ApiError("不认识的导入方式：%s" % kind)
    if not r.get("ok"):
        raise ApiError(r.get("error") or "解析失败")
    plan = _stash_plan(r)
    return {"ok": True, "plan_id": plan["id"], "dir": r["dir"],
            "renamed": r.get("renamed"),
            "meta": r["meta"], "audit": r["audit"],
            "file_list": sorted((r.get("files") or {}).keys())[:60],
            "file_count": len(r.get("files") or {})}


# 导入计划暂存区。计划里有文件内容，不能塞进 URL；做完就删。
# 用内存字典而不是落临时文件：服务重启计划就作废，符合"用户在页面上点两下"的用法。
_PLANS = {}
_PLAN_SEQ = [0]


def _stash_plan(plan):
    _PLAN_SEQ[0] += 1
    pid = "p%d" % _PLAN_SEQ[0]
    _PLANS[pid] = plan
    if len(_PLANS) > 20:                       # 只留最近 20 个，别长胖
        for k in sorted(_PLANS.keys(), key=lambda x: int(x[1:]))[:-20]:
            _PLANS.pop(k, None)
    plan["id"] = pid
    return plan


@route("POST", r"/api/skills/import")
def api_skills_import(q, b):
    """把计划真正落到磁盘。"""
    from engine import skillhub as SH
    db = _db()
    try:
        pid = (b.get("plan_id") or "").strip()
        plan = _PLANS.get(pid)
        _require(plan, "导入计划已过期（服务重启过？），请重新选一次文件", 404)
        new_dir = (b.get("dir") or "").strip()
        if new_dir:
            # 用户可以改名。改了就要把 SKILL.md 里的 name 一起改齐，
            # 否则界面显示 name、卸载按目录名，两边对不上。
            ok_name = SH._safe_display_name(new_dir)
            _require(ok_name, "名字里有非法字符（不能含 / \\ : * ? \" < > | ）")
            if ok_name != plan["dir"]:
                files = dict(plan.get("files") or {})
                txt = files.get("SKILL.md", b"").decode("utf-8", "replace")
                files["SKILL.md"] = SH._set_frontmatter_name(
                    txt, ok_name).encode("utf-8")
                plan = dict(plan)
                plan["files"] = files
                plan["dir"] = ok_name
        r = SH.commit_plan(plan, (b.get("scope") or "user").strip(),
                           force=bool(b.get("force")))
        if not r.get("ok"):
            out = {"error": r.get("error") or "导入失败"}
            if r.get("need_confirm"):
                out["need_confirm"] = True
                out["audit"] = r.get("audit") or {}
                # 审计命中不是 400 而是需要用户拍板：用 409 跟普通错误分开，
                # 前端好据此弹二次确认而不是弹报错
                return 409, out
            raise ApiError(out["error"])
        _PLANS.pop(pid, None)
        return {"ok": True, "installed": r, "skills": SH.scan_local()}
    finally:
        db.close()


@route("POST", r"/api/skills/gen")
def api_skills_gen(q, b):
    """让 AI 写一个技能。

    后台任务：模型要写几百上千字，同步等会把页面卡住。
    产出**只是草案**——用户看过、改过名字，再走 /api/skills/gen_save 落盘。
    """
    db = _db()
    from engine import skillhub as SH
    from engine import prompts as P
    brief = (b.get("brief") or "").strip()
    _require(brief, "先说说你要这个技能管什么")
    _require(len(brief) <= 2000, "需求写太长了，精简到 2000 字以内")
    # 生成也是"写文档"，用正文档位；世界推演档位温度高但不管文风。
    ok, why = _router(db, 0).is_ready("summarize")
    _require(ok, "档位「摘要」不可用：%s。请先到设置页配置模型。"
                 % (why or "未配置"))
    avoid = [s["name"] for s in SH.scan_local()]
    db.close()

    def work(emit):
        d = DB.open_db(DB_PATH)
        try:
            r = _router(d, 0)
            emit("正在按你的要求起草技能…", 20)
            # 生成型任务给明确 cap：不给会被降级链放到 32k，模型拿多出来的
            # 空间复述需求，最后仍是个被截断的 JSON。
            parsed, raw, mode = r.run_json(
                "summarize", schema=SH.gen_schema(),
                system=SH.gen_system(), user=SH.gen_user(brief, avoid),
                max_tokens=3072, cap=4096)
            _require(parsed, "模型没有返回可解析的技能内容，可以重试")
            emit("整理成 SKILL.md…", 80)
            out = SH.parse_gen_output(parsed)
            _require(out, "模型产出的内容不完整（缺正文），可以重试")
            out["brief"] = brief
            out["mode"] = mode
            emit("已起草技能「%s」，看一遍再保存。" % out["name"], 100)
            return out
        finally:
            d.close()

    return {"job_id": JOBS.submit("skill_gen", work)}


@route("POST", r"/api/skills/gen_save")
def api_skills_gen_save(q, b):
    """把 AI 草案（可能被用户改过）写到磁盘。"""
    from engine import skillhub as SH
    db = _db()
    try:
        name = (b.get("dir") or b.get("name") or "").strip()
        content = (b.get("content") or "").strip()
        _require(name and content, "缺少技能名或正文")
        _require(content.lstrip().startswith("---"),
                 "内容要从 frontmatter（---）开头，否则不是合法的 SKILL.md")
        plan = SH.plan_content(name, content)
        if not plan.get("ok"):
            raise ApiError(plan.get("error") or "保存失败")
        r = SH.commit_plan(plan, (b.get("scope") or "user").strip())
        if not r.get("ok"):
            raise ApiError(r.get("error") or "保存失败")
        return {"ok": True, "installed": r, "skills": SH.scan_local()}
    finally:
        db.close()


@route("GET", r"/api/skills/sources")
def api_skills_sources(q, b):
    from engine import skillhub as SH
    return {"ok": True, "sources": SH.list_sources()}


@route("POST", r"/api/skills/browse")
def api_skills_browse(q, b):
    from engine import skillhub as SH
    sid = (b.get("source") or b.get("source_id") or "").strip()
    _require(sid, "请先选一个来源库")
    r = SH.browse(sid)
    return {"ok": bool(r.get("ok")), "items": r.get("items") or [],
            "error": r.get("error") or "", "source": r.get("source") or {}}


@route("POST", r"/api/skills/install")
def api_skills_install(q, b):
    from engine import skillhub as SH
    sid = (b.get("source") or b.get("source_id") or "").strip()
    name = (b.get("name") or "").strip()
    _require(sid and name, "缺少来源或技能名")
    r = SH.install(sid, name, as_name=(b.get("as_name") or "").strip())
    if not r.get("ok"):
        # 重名不是错误、是「请改个名」：用 409 让前端弹改名而不是弹报错
        if r.get("name_taken"):
            return 409, {"error": r.get("error") or "重名",
                         "name_taken": True, "suggest": SH.suggest_name(name)}
        raise ApiError(r.get("error") or "安装失败")
    return {"ok": True, "installed": r, "skills": SH.scan_local(),
            # 审计告警要带回前端：技能里带脚本很常见，一律拒会误伤，
            # 但用户得知道这个技能会跑 subprocess / pip install。
            "warn": (r.get("audit") or {}).get("warn") or []}


@route("POST", r"/api/skills/uninstall")
def api_skills_uninstall(q, b):
    from engine import skillhub as SH
    db = _db()
    try:
        name = (b.get("name") or b.get("dir") or "").strip()
        scope = (b.get("scope") or "user").strip()
        confirm = (b.get("confirm_name") or "").strip()
        # 删目录是不可逆的：跟 purge 一个规矩，**必须把名字原样打一遍**。
        # 只回 200 却没真删（或删错）是本项目踩过的坑，所以这里宁严不松。
        _require(confirm == name and name, "确认名与技能名不一致，已取消")
        r = SH.uninstall(name, scope, db)
        if not r.get("ok"):
            raise ApiError(r.get("error") or "卸载失败")
        return {"ok": True, "skills": SH.scan_local(),
                "bindings": SH.load_bindings(db)}
    finally:
        db.close()


@route("GET", r"/api/skills/bindings")
def api_skills_bindings(q, b):
    from engine import skillhub as SH
    db = _db()
    try:
        cats = SH.slot_catalog()
        bd = SH.load_bindings(db)
        for c in cats:
            cur = bd.get(c["slot"]) or {}
            want = (cur.get("skill") or "").strip()
            sk = SH.find_skill(want) if want else None
            c["skill"] = want
            # 绑了但本地找不到 = 技能被卸了。这种"页面显示启用、模型其实
            # 没收到"的静默失效最坑，所以单独标 missing 让前端画红字。
            c["enabled"] = bool(cur.get("enabled")) and bool(sk)
            c["missing"] = bool(want) and not sk
            c["full_chars"] = (sk or {}).get("body_chars", 0)
            c["inject_chars"] = min(c["full_chars"], SH.MAX_INJECT_CHARS)
        return {"ok": True, "slots": cats, "skills": SH.scan_local(),
                "max_inject_chars": SH.MAX_INJECT_CHARS}
    finally:
        db.close()


@route("POST", r"/api/skills/bind")
def api_skills_bind(q, b):
    from engine import skillhub as SH
    db = _db()
    try:
        slot = (b.get("slot") or "").strip()
        skill = (b.get("skill") or "").strip()
        enabled = bool(b.get("enabled"))
        if enabled and skill and not SH.find_skill(skill):
            raise ApiError("本地没有这个技能：%s" % skill)
        try:
            d = SH.save_binding(db, slot, skill, enabled)
        except ValueError as e:
            raise ApiError(str(e))
        return {"ok": True, "bindings": d}
    finally:
        db.close()


@route("POST", r"/api/novels")
def api_create_novel(q, b):
    db = _db()
    _require((b.get("title") or "").strip(), "书名不能为空")
    nv = db.create_novel(
        title=b["title"], genre=b.get("genre", ""), tone=b.get("tone", ""),
        style=b.get("style", ""), premise=b.get("premise", ""),
        initial_tension=b.get("initial_tension", ""),
        decision_mode=b.get("decision_mode", "director"),
        completion_mode=b.get("completion_mode", "ai"),
        target_words_per_chapter=int(b.get("target_words_per_chapter") or 3000),
    )
    if b.get("world_time"):
        db.advance_clock(nv["id"], new_time=b["world_time"])
    # ★ 同 world_apply：初始状态走 facts.seed_state()。
    #   这里原先直接调 db.set_state_value()，而 v6.7 把它收成了只接受投影写入，
    #   于是"带初始世界状态建书"从 v6.7 起就**一直在抛 PermissionError**——
    #   旧版冒烟测试没覆盖这条组合（建书时不带状态），所以没被发现。
    seed_items = []
    skipped = []
    for st in (b.get("world_state") or []):
        if not isinstance(st, dict):
            continue
        key = str(st.get("key") or "").strip()
        if not key:
            continue
        if "." not in key:
            skipped.append(key)
            continue
        seed_items.append({"key": key, "value": st.get("value"),
                           "value_type": st.get("value_type") or "text",
                           "category": st.get("category") or "other"})
    if seed_items:
        from engine import facts as F
        F.seed_state(db, nv["id"], seed_items)
    return {"novel": db.get_novel(nv["id"]),
            "skipped_state": skipped}


@route("POST", r"/api/novel/cast")
def api_gen_cast(q, b):
    """为一个还没有角色的世界生成初始阵容草案（后台任务，不打库）。

    只返回草案，落库走 /api/novel/cast/apply——用户可以逐条改完再确认。
    """
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid and db.get_novel(nid), "小说不存在", 404)
    count = max(2, min(8, int(b.get("count") or 4)))
    focus = (b.get("focus") or "").strip()
    ok, why = _router(db, nid).is_ready("character_decide")
    _require(ok, "档位「角色决策」不可用：%s。请先到设置页配置模型。"
             % (why or "未配置"))
    db.close()

    def work(emit):
        d = DB.open_db(DB_PATH)
        try:
            from engine import prompts as P
            from engine.character import pick_protagonist
            r = _router(d, nid)
            nv = d.get_novel(nid) or {}
            emit("读取世界设定…", 10)
            user = P.cast_gen_user(
                title=nv.get("title") or "", genre=nv.get("genre") or "",
                tone=nv.get("tone") or "", premise=nv.get("premise") or "",
                initial_tension=nv.get("initial_tension") or "",
                count=count, focus=focus)
            emit("模型正在设计 %d 个角色、目标与关系网…" % count, 25)
            parsed, raw, mode = r.run_json(
                "character_decide", schema=P.CAST_GEN_SCHEMA,
                system=P.CAST_GEN_SYSTEM, user=user, max_tokens=4096)
            _require(parsed, "模型没有返回可解析的阵容。可以重试，或手动添加角色。")
            chars = [c for c in (parsed.get("characters") or [])
                     if str(c.get("name") or "").strip()]
            _require(chars, "模型返回的阵容是空的。可以重试，或手动添加角色。")
            existing = {c["name"] for c in d.list_characters(nid)}
            for c in chars:
                c["name"] = c["name"].strip()
                c["_exists"] = c["name"] in existing
            lead = pick_protagonist(chars)
            for c in chars:
                c["is_protagonist"] = (c["name"] == lead)

            # 关系网：只保留双方都在阵容里的，避免模型写出不存在的人。
            # 掉出去的那条要报给用户——静默丢掉会让"这俩人怎么没互动"变成谜。
            names = {c["name"] for c in chars}
            rels, dropped = [], []
            for r in (parsed.get("relations") or []):
                if not isinstance(r, dict):
                    continue
                a = str(r.get("a") or "").strip()
                b = str(r.get("b") or "").strip()
                if not a or not b or a == b:
                    continue
                if a in names and b in names:
                    r["a"], r["b"] = a, b
                    rels.append(r)
                else:
                    dropped.append("%s—%s" % (a, b))
            if not rels and len(chars) > 1:
                emit("⚠ 模型这次没给出关系网（relations 为空）。"
                     "可以重试，或落库后在角色页手动补。", 88)
            elif dropped:
                emit("⚠ 有 %d 条关系引用了阵容外的名字，已跳过：%s"
                     % (len(dropped), "、".join(dropped[:5])), 88)
            if existing:
                emit("注意：%d 个名字和已有角色重名，确认时会自动跳过。"
                     % len([c for c in chars if c["_exists"]]), 92)
            emit("拿到 %d 个角色、%d 条关系的草案，请逐条过一遍再确认。"
                 % (len(chars), len(rels)), 100)
            return {"characters": chars, "protagonist": lead,
                    "relations": rels, "dropped_relations": dropped,
                    "conflict_map": parsed.get("conflict_map") or "",
                    "mode": mode}
        finally:
            d.close()

    return {"job_id": JOBS.submit("cast_gen", work)}


@route("POST", r"/api/novel/cast/apply")
def api_apply_cast(q, b):
    """把阵容草案落库（同名角色跳过，主角按人机分工设托管模式）。

    relations 一并落库——人物关系网是推演的燃料，不能只有角色没有关系。
    """
    db = _db()
    nid = int(b.get("novel_id") or 0)
    nv = db.get_novel(nid)
    _require(nv, "小说不存在", 404)
    chars = [c for c in (b.get("characters") or [])
             if isinstance(c, dict) and str(c.get("name") or "").strip()]
    _require(chars, "阵容是空的。至少要有一个角色——没有角色的世界，"
                    "推演会每次即兴捏一批人，状态留不下来。")
    rels = [r for r in (b.get("relations") or []) if isinstance(r, dict)]
    from engine.character import apply_cast
    res = apply_cast(db, nid, chars, relations=rels,
                     decision_mode=(b.get("decision_mode")
                                    or nv.get("decision_mode") or "director"))
    res["characters"] = [_view_character(db, c)
                         for c in db.list_characters(nid)]
    res["all_relations"] = db.list_relations(nid)
    return res


# ============================================================ 题材趋势 · 创建世界（v6.8）
#
# 用户场景：
#   「创建世界的时候可以去网络上搜索热门小说类型、题材，然后根据这些数据让用户
#     选择生成哪一类，生成的时候用户可以增加一些提示内容。然后自动创建出这个
#     虚拟世界。」
#
# 三段式，每一段都不落库，最后一段才写：
#   ① GET  /api/genre/trends      —— 热门题材（可联网刷新）
#   ② POST /api/genre/world_draft —— 按选定题材生成世界草案（异步，不落库）
#   ③ POST /api/genre/world_apply —— 用户确认后才落库，接着走既有的组建阵容

@route("GET", r"/api/genre/trends")
def api_genre_trends(q, b):
    """热门题材列表。

    `refresh=1` 时才联网——列表每次刷新都联网会让"选题材"这个动作变慢，
    而热度一天变一次就够了。抓不到网不会失败，会降级到本地快照/内置基准，
    并把来源如实写在 `source` 里给前端显示。
    """
    from engine import genretrend as GT
    aud = (q.get("audience") or "").strip().lower()
    if aud not in ("male", "female"):
        aud = ""
    limit = _as_opt_int(q.get("limit"))
    if limit is not None and limit <= 0:
        limit = None
    try:
        r = GT.trends(audience=aud or None,
                      refresh=_flag(q.get("refresh"), default=False),
                      limit=limit)
    except Exception as e:                                  # noqa: BLE001
        # 题材库本身出问题也不能让"创建世界"进不去——退回空列表 + 说清原因，
        # 前端会显示"题材库不可用，你可以直接手填"。
        return {"genres": [], "source": {
            "kind": "error", "label": "题材库不可用",
            "note": "读取题材库失败：%s。可以自己填题材直接创建世界。"
                    % str(e)[:120], "at": "", "report": []},
            "audiences": _genre_audience_counts()}
    r["audiences"] = _genre_audience_counts()
    return r


def _genre_audience_counts():
    """三个页签各自的题材数（前端要显示"男频 16"这种）。"""
    try:
        from engine import genretrend as GT
        rows = list(GT.genres())
    except Exception:                                      # noqa: BLE001
        rows = []
    def n(a):
        return sum(1 for g in rows
                   if g.get("audience") == a or (a in ("male", "female")
                                                 and g.get("audience") == "both"))
    return [{"key": "", "label": "全部", "count": len(rows)},
            {"key": "male", "label": "男频", "count": n("male")},
            {"key": "female", "label": "女频", "count": n("female")}]


@route("POST", r"/api/genre/world_draft")
def api_genre_world_draft(q, b):
    """按选定题材生成整份世界草案（后台任务，**不落库**）。

    和 `/api/novel/cast` 一样只返回草案，用户在弹层里逐条改完，
    点"创建这个世界"才走 world_apply。
    """
    db = _db()
    from engine import genretrend as GT
    genre_key = (b.get("genre_key") or "").strip()
    genre_name = (b.get("genre") or "").strip()
    hint = (b.get("hint") or "").strip()
    # v6.8：本接口既服务于「新建作品」向导（此时还没有作品，nid=0 走全局档位），
    # 也服务于「给已有作品重新生成世界」（此时必须用该作品自己的档位）。
    # 早期这里写死 0，导致用户在作品里配了「世界构建」档位也不生效——
    # 路由只会读到全局行，而全局行的 model 往往是空的，于是静默降级到
    # 全局 world_sim，表现就是「总是用第一个模型」。
    nid = int(b.get("novel_id") or 0)
    _require(genre_key or genre_name, "请先选一个题材，或自己填一个题材名")

    g = GT.resolve_genre(genre_key or genre_name)
    # 来源标记：告诉前端这次是按热门题材生成，还是按用户自填的题材生成
    genre_source = "trend" if (genre_key and g) else (
        "trend_name" if g else "custom")

    block = ""
    if g:
        block = GT.genre_block(g, hint=hint)
        if not genre_name:
            genre_name = (g.get("name") or "").strip()

    ok, why = _router(db, nid).is_ready("world_init")
    _require(ok, "档位「世界构建」不可用：%s。请先到设置页配置模型。"
             % (why or "未配置"))
    db.close()

    def work(emit):
        d = DB.open_db(DB_PATH)
        try:
            from engine import prompts as P
            r = _router(d, nid)
            nv_hint = hint if not g else ""      # 已经在题材块里拼过一次，不重复
            emit("正在把题材落成世界…", 20)
            user = P.world_gen_user(genre_block=block, hint=nv_hint,
                                    audience=(g or {}).get("audience") or "")
            # 世界构建是**生成型**任务，给一个明确的输出预算就够；
            # 不给会被降级链放到 32k，模型拿多出来的空间复述题材介绍。
            parsed, raw, mode = r.run_json(
                "world_init", schema=P.WORLD_GEN_SCHEMA,
                system=P.WORLD_GEN_SYSTEM, user=user,
                max_tokens=3072, cap=4096)
            _require(parsed, "模型没有返回可解析的世界配置。可以重试，"
                             "或直接手填世界设定。")
            emit("整理世界状态与规则…", 80)
            draft = _normalize_world_draft(parsed, genre_name)
            # 题材 key 由这里回填（收口函数只管收口，不认得题材库）
            draft["genre_key"] = (g or {}).get("key") or ""
            emit("已生成《%s》的世界草案，请过一遍再创建。"
                 % (draft.get("title") or "未命名"), 100)
            draft["mode"] = mode
            draft["genre_source"] = genre_source
            return draft
        finally:
            d.close()

    return {"job_id": JOBS.submit("world_draft", work)}


def _normalize_world_draft(parsed, genre_name=""):
    """把模型给的世界草案收口成可直接落库的形状（纯数据，不写库）。

    **必须收口而不是直接转发**：模型自由文本会往带 CHECK/INT 的列里灌垃圾——
    这是本项目反复踩过的一条。这里只做三件事：
      · 世界状态的键必须是「主体.谓词」，否则**不静默丢弃**，记进 warnings
        让用户看见（静默丢键＝用户以后发现世界状态少了一条，找不到原因）
      · 值按字面量推断 value_type（数字就 int/float，否则 text）
      · 规则的 severity / world_state 的 value_type 收进枚举
    """
    warnings = []

    state = []
    for st in (parsed.get("world_state") or []):
        if not isinstance(st, dict):
            continue
        key = str(st.get("key") or "").strip()
        if not key:
            continue
        if "." not in key:
            # 不丢，但也不直接落——落下去推演读不出主体，等于一条死数据
            warnings.append("世界状态「%s」不是「主体.谓词」格式，已跳过。"
                            "推演靠主体和谓词定位状态量，缺了读不出来。" % key)
            continue
        raw_val = st.get("value")
        text = "" if raw_val is None else str(raw_val).strip()
        vt = str(st.get("value_type") or "").strip().lower()
        if vt not in ("int", "float", "text"):
            # 模型经常漏 value_type 或写 "number"；按字面量推更可靠
            if re.fullmatch(r"-?\d+", text):
                vt = "int"
            elif re.fullmatch(r"-?\d*\.\d+", text):
                vt = "float"
            else:
                vt = "text"
        value = raw_val
        if vt == "int":
            try:
                value = int(float(text))
            except (TypeError, ValueError):
                vt, value = "text", text
        elif vt == "float":
            try:
                value = float(text)
            except (TypeError, ValueError):
                vt, value = "text", text
        else:
            value = text
        state.append({"key": key, "value": value, "value_type": vt,
                      "category": str(st.get("category") or "other").strip()
                                  or "other",
                      "note": str(st.get("note") or "").strip()[:60]})

    if not state:
        warnings.append("模型没给出可用的世界状态。世界状态是推演的硬事实来源，"
                        "建议至少手工填一条（一行「主体.谓词 = 值」）。")

    rules = []
    for r in (parsed.get("rules") or []):
        if not isinstance(r, dict):
            continue
        content = str(r.get("content") or "").strip()
        if not content:
            continue
        sev = str(r.get("severity") or "").strip().lower()
        if sev not in ("error", "warning"):
            sev = "error"
        rules.append({"content": content[:120],
                      "scope": str(r.get("scope") or "global").strip()
                               or "global",
                      "severity": sev,
                      "check_hint": str(r.get("check_hint") or "").strip()[:80],
                      "is_hard": 1 if sev == "error" else 0})

    tags = []
    for t in (parsed.get("tags") or []):
        t = str(t or "").strip()
        if t and t not in tags:
            tags.append(t[:12])

    return {
        "title": str(parsed.get("title") or "").strip()[:30],
        "genre": str(parsed.get("genre") or genre_name or "").strip()[:40],
        "genre_key": "",                     # 由调用方回填
        "tone": str(parsed.get("tone") or "").strip()[:24],
        "premise": str(parsed.get("premise") or "").strip()[:1200],
        "initial_tension": str(parsed.get("initial_tension") or "").strip()[:600],
        "world_time": str(parsed.get("world_time") or "").strip()[:60],
        "tags": tags,
        "world_state": state,
        "rules": rules,
        "design_note": str(parsed.get("design_note") or "").strip()[:400],
        "warnings": warnings,
    }


@route("POST", r"/api/genre/world_apply")
def api_genre_world_apply(q, b):
    """把用户确认过的世界草案落库。

    为什么不让前端改成两次调用（先 /api/novels 再补状态和规则）：
    **状态与规则必须和书一起建成**。分两次的话，中间失败会留下一本
    "世界里什么都没有"的书——而世界状态为空时推演是能跑的（它会自己长出来），
    用户根本不会发现状态没写进去。
    """
    db = _db()
    title = (b.get("title") or "").strip()
    _require(title, "书名不能为空")
    nv = db.create_novel(
        title=title,
        genre=(b.get("genre") or "").strip(),
        tone=(b.get("tone") or "").strip(),
        premise=(b.get("premise") or "").strip(),
        initial_tension=(b.get("initial_tension") or "").strip(),
        decision_mode=(b.get("decision_mode") or "director").strip(),
        target_words_per_chapter=int(b.get("target_words_per_chapter") or 3000),
    )
    nid = nv["id"]
    applied = {"state": 0, "rules": 0, "skipped_state": []}

    if (b.get("world_time") or "").strip():
        db.advance_clock(nid, new_time=b["world_time"].strip())

    # ★ 初始世界状态必须走 facts.seed_state()，**不能直接 db.set_state_value()**。
    #   v6.7 的约束 4 把 world_state 收成了"只接受投影写入"（唯一写入口是
    #   applier.apply_projection），直接调就会抛 PermissionError。
    #   而且这不只是权限问题：初始状态**也是世界真相**，它得进正史账本，
    #   否则第 30 章回头看"这个数字最初是多少"就查不到了。
    seed_items = []
    for st in (b.get("world_state") or []):
        if not isinstance(st, dict):
            continue
        key = str(st.get("key") or "").strip()
        if not key:
            continue
        if "." not in key:
            # 建库路径也要挡：状态量是「主体.谓词」，缺主体推演读不出来。
            # 但**不静默丢**——把键报回前端让用户知道哪几条没进去。
            applied["skipped_state"].append(key)
            continue
        seed_items.append({"key": key, "value": st.get("value"),
                           "value_type": st.get("value_type") or "text",
                           "category": st.get("category") or "other"})
    if seed_items:
        try:
            from engine import facts as F
            r = F.seed_state(db, nid, seed_items)
            applied["state"] = r.get("state") or 0
            applied["facts"] = r.get("facts") or 0
        except Exception as e:                              # noqa: BLE001
            # 建库阶段的状态写入失败必须让用户知道——世界状态为空时推演
            # 照样能跑（它会自己长出来），静默失败用户永远发现不了。
            applied["state_error"] = "%s: %s" % (type(e).__name__, str(e)[:200])

    for r in (b.get("rules") or []):
        if not isinstance(r, dict):
            continue
        content = str(r.get("content") or "").strip()
        if not content:
            continue
        rid = db.add_world_rule(
            nid, content,
            scope=str(r.get("scope") or "global").strip() or "global",
            subject_name=str(r.get("subject_name") or "").strip(),
            is_hard=1 if r.get("is_hard", 1) else 0,
            severity=(str(r.get("severity") or "error").strip().lower()
                      if str(r.get("severity") or "").strip().lower()
                      in ("error", "warning") else "error"),
            check_hint=str(r.get("check_hint") or "").strip(),
            source="user")
        # 规则也是世界的硬事实——同步记进正史账本，否则推演看不到它。
        # 与 /api/canon/rules 走同一套收口，别在这里另写一份。
        try:
            from engine import facts as F
            F.register_rule_as_fact(db, nid, rid, content,
                                    tick=F._current_tick(db, nid))
        except Exception:                                  # noqa: BLE001
            pass
        applied["rules"] += 1

    # 题材标签落在 novel_attributes（open schema），供以后检索与分类
    tags = [str(t).strip() for t in (b.get("tags") or []) if str(t).strip()]
    if tags:
        db.set_attribute(nid, "genre_tags", "、".join(tags[:8]))
    if (b.get("genre_key") or "").strip():
        db.set_attribute(nid, "genre_key", b["genre_key"].strip())
    if (b.get("design_note") or "").strip():
        db.set_attribute(nid, "world_design_note",
                         b["design_note"].strip()[:400])

    return {"novel": db.get_novel(nid), "applied": applied}


@route("GET", r"/api/novel")
def api_novel(q, b):
    db = _db()
    nid = _novel_id(q)
    nv = db.get_novel(nid)
    _require(nv, "小说不存在", 404)
    return {"novel": nv,
            "clock": db.get_clock(nid),
            "stats": db.stats(nid),
            "setting": db.get_world(nid),
            "attributes": db.get_attributes(nid)}


@route("POST", r"/api/novel")
def api_update_novel(q, b):
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid, "缺少 novel_id")
    fields = {k: v for k, v in (b.get("fields") or {}).items()}
    nv = db.update_novel(nid, **fields)
    return {"novel": nv}


def _novel_snapshot(db, nid):
    """删除前留一份"这世界原来有什么"的账，返回给前端展示。"""
    try:
        st = db.stats(nid) or {}
    except Exception:
        st = {}
    return {"chapters": st.get("chapter_count") or 0,
            "words": st.get("total_words") or 0,
            "characters": st.get("character_count") or 0,
            "events": st.get("event_count") or 0,
            "threads": st.get("active_thread_count") or 0}


@route("GET", r"/api/novels/archived")
def api_archived_novels(q, b):
    return {"novels": _db().list_archived_novels()}


@route("POST", r"/api/novel/delete")
def api_delete_novel(q, b):
    """删除小说。

    mode = "archive"（默认）：归档，列表里隐藏，数据保留，可 /api/novel/restore 恢复。
    mode = "purge"：物理删除，级联清空全部子表，不可恢复。
                    必须带 confirm_title 且与书名完全一致，防误删长篇。
    """
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid, "缺少 novel_id")
    nv = db.get_novel(nid)
    _require(nv, "小说不存在", 404)
    mode = (b.get("mode") or "archive").strip()
    _require(mode in ("archive", "purge"), "mode 只能是 archive 或 purge")

    snap = _novel_snapshot(db, nid)
    title = (nv.get("title") or "").strip()
    backup = ""
    orphans = None

    if mode == "purge":
        want = (b.get("confirm_title") or "").strip()
        _require(want, "彻底删除需要输入书名确认")
        _require(want == title, "书名不一致（要删除的是「%s」），已取消" % title)
        # 尽力导出章节正文，删库前留一份可读的副本
        try:
            paths = db.export_all(nid) or []
            if paths:
                backup = "%d 章正文已导出到 novels/ 目录" % len(paths)
        except Exception:
            backup = ""
        db.delete_novel(nid)
        # 哨兵表（novel_id=0）之外，指向已删世界的残留行一并清扫
        try:
            orphans = db.purge_orphans().get("total")
        except Exception:
            orphans = None
    else:
        db.soft_delete_novel(nid)

    return {"deleted": nid, "mode": mode, "title": title, "snapshot": snap,
            "backup": backup, "orphans": orphans,
            "novels": db.list_novels(),
            "archived": db.list_archived_novels(),
            "health": db.health()}


@route("POST", r"/api/novel/restore")
def api_restore_novel(q, b):
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid, "缺少 novel_id")
    nv = db.restore_novel(nid)
    _require(nv, "小说不存在", 404)
    return {"novel": nv, "novels": db.list_novels(),
            "archived": db.list_archived_novels()}


@route("GET", r"/api/stats")
def api_stats(q, b):
    db = _db()
    st = db.stats(_novel_id(q))
    _require(st, "小说不存在", 404)
    return st


@route("GET", r"/api/clock")
def api_clock(q, b):
    db = _db()
    nid = _novel_id(q)
    return {"clock": db.get_clock(nid), "timeline": db.list_timeline(nid)}


@route("POST", r"/api/clock")
def api_set_clock(q, b):
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid, "缺少 novel_id")
    if not (b.get("current_time") or "").strip():
        raise ApiError("时间不能为空")
    db.advance_clock(nid, new_time=b["current_time"],
                     granularity=b.get("granularity"))
    return {"clock": db.get_clock(nid)}


@route("GET", r"/api/state")
def api_state(q, b):
    db = _db()
    nid = _novel_id(q)
    return {"state": db.get_world_state(nid, category=q.get("category") or None),
            "history": db.state_history(nid, q.get("key") or None, limit=60)}


@route("POST", r"/api/state")
def api_set_state(q, b):
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid, "缺少 novel_id")
    _require((b.get("key") or "").strip(), "状态名不能为空")
    # ★ 约束 4：这是"世界页手改"，不是投影写入 → 必须走 set_state_value_manual。
    #   直接调 db.set_state_value() 会被 writer 白名单挡下（PermissionError），
    #   于是"在世界页改个数字"整条路死掉。manual 版写 derived_from_fact_id=NULL，
    #   语义是"这是人定的值，后面的自动投影不许覆盖"（投影侧会把它计入 skipped）。
    db.set_state_value_manual(nid, b["key"], b.get("value"),
                              b.get("value_type", "text"),
                              b.get("category", "other"), b.get("note", ""),
                              int(b.get("chapter_ref") or 0),
                              log_reason=b.get("reason") or "手动修改")
    return {"state": db.get_world_state(nid)}


@route("POST", r"/api/state/delete")
def api_delete_state(q, b):
    db = _db()
    nid = int(b.get("novel_id") or 0)
    key = (b.get("key") or "").strip()
    _require(nid and key, "缺少 novel_id 或 key")
    db.execute("DELETE FROM world_state WHERE novel_id=? AND key=?", (nid, key))
    return {"state": db.get_world_state(nid)}


@route("GET", r"/api/entities")
def api_entities(q, b):
    db = _db()
    nid = _novel_id(q)
    rows = db.list_entities(nid, entity_type=q.get("entity_type") or None,
                            status=q.get("status") or None)
    return {"entities": rows}


@route("POST", r"/api/entities")
def api_add_entity(q, b):
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid and (b.get("name") or "").strip(), "缺少 novel_id 或名称")
    eid = db.add_entity(nid, b.get("entity_type", "other"), b["name"],
                        b.get("description", ""),
                        power_level=int(b.get("power_level") or 3),
                        visibility=b.get("visibility", "public"))
    return {"id": eid, "entities": db.list_entities(nid)}


@route("POST", r"/api/entities/update")
def api_update_entity(q, b):
    db = _db()
    eid = int(b.get("id") or 0)
    _require(eid, "缺少 id")
    fields = {k: v for k, v in (b.get("fields") or {}).items()}
    db.update_entity(eid, **fields)
    if b.get("attributes"):
        db.merge_entity_attributes(eid, b["attributes"])
    return {"entity": db.get_entity(eid)}


@route("GET", r"/api/locations/tree")
def api_location_tree(q, b):
    """地点树，供地图分组渲染。带 parent_id 的结构 + 层级。"""
    db = _db()
    nid = _novel_id(q)
    rows = db.list_locations(nid)
    return {"locations": [
        {"id": r["id"], "name": r["name"], "parent_id": r.get("parent_id"),
         "level": r.get("location_level") or "", "description": r.get("description") or "",
         "is_secret": int(r.get("is_secret") or 0),
         "attributes": r.get("attributes") or {}}
        for r in rows]}


@route("GET", r"/api/map")
def api_map(q, b):
    """世界地图：按地点树分组摆人。

    两种视角：
      - 默认（上帝视角）：用 characters.location_id（事实）——画"谁真的在哪"
      - viewer=<角色id>：改用 location_beliefs ——画"这个角色**以为**谁在哪"。
        用户要的正是这个：别人都不知道这个人在什么具体位置。

    精度由 location_precision 决定，前端据此决定把点画在楼栋还是房间。
    位置不是凭空来的：树随叙事生长，没演到的层级不编。
    """
    db = _db()
    nid = _novel_id(q)
    viewer = _as_int(q["viewer"], "viewer") if q.get("viewer") else None

    chars = {c["id"]: c for c in db.list_characters(nid)}

    # 节点表：**所有**地点都在（含空的），前端自己决定画不画——
    # 只有真装了人的节点才值得画，但父链必须完整，不然层级显示会断层。
    nodes = {}
    for r in db.list_locations(nid):
        nodes[r["id"]] = {"id": r["id"], "name": r["name"],
                          "parent_id": r.get("parent_id"),
                          "level": r.get("location_level") or "",
                          "is_secret": int(r.get("is_secret") or 0),
                          "members": [], "believed": []}

    def _item(cid, precision):
        c = chars.get(cid) or {}
        return {"id": cid, "name": c.get("name", "?"), "rank": c.get("rank", "C"),
                "precision": precision or "unknown",
                "status": c.get("status", "alive")}

    def _add(cid, lid, precision, is_belief):
        """挂到节点上；节点不存在（老数据只存了文本）就退回声明的文本。"""
        n = nodes.get(lid)
        if n is not None:
            n["believed" if is_belief else "members"].append(_item(cid, precision))
            return True
        return False

    loose = []           # 挂不上节点的：有文本没结构，别丢，单列出来
    unknown = []         # 完全不知道在哪
    if viewer:
        # 角色视角：他以为谁在哪。**先把他自己的位置放进来**——
        # 不然切到某个人视角，地图上反而没了他本人，看着像坏了。
        me = chars.get(viewer) or {}
        if me:
            if not _add(viewer, me.get("location_id"), me.get("location_precision"),
                        False):
                loose.append({"who": me.get("name", ""), "text":
                              me.get("current_location") or "行踪不明",
                              "self": True})
        seen = {viewer}
        for bm in db.list_location_beliefs(nid, observer_id=viewer):
            if bm.get("stale"):
                continue
            cid = bm["character_id"]
            if cid in seen:
                continue
            seen.add(cid)
            if _add(cid, bm.get("believed_location_id"), bm.get("precision"), True):
                continue
            loose.append({"who": (chars.get(cid) or {}).get("name", "?"),
                          "text": bm.get("believed_text") or "行踪不明",
                          "self": False})
        unknown = [_item(c["id"], "unknown") for c in chars.values()
                   if c["id"] not in seen and c["status"] == "alive"]
    else:
        for c in chars.values():
            lid = c.get("location_id")
            if lid and _add(c["id"], lid, c.get("location_precision"), False):
                continue
            # 没有结构位置：只给了文本的进 loose，什么都没有的进 unknown
            if c["status"] != "alive":
                continue
            if c.get("current_location"):
                loose.append({"who": c["name"], "text": c["current_location"],
                              "self": False})
            else:
                unknown.append(_item(c["id"], c.get("location_precision")))

    return {
        "viewer": viewer,
        "viewer_name": (chars.get(viewer) or {}).get("name", "") if viewer else "",
        "nodes": [n for n in nodes.values()
                  if n["members"] or n["believed"] or n["parent_id"] is None],
        "all_nodes": list(nodes.values()),
        "loose": loose,
        "unknown": unknown,
    }


@route("GET", r"/api/world-setting")
def api_world_setting(q, b):
    db = _db()
    nid = _novel_id(q)
    return {"setting": db.get_world(nid, q.get("category") or None),
            "attributes": db.get_attributes(nid)}


@route("POST", r"/api/world-setting")
def api_add_world_setting(q, b):
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid and (b.get("name") or "").strip(), "缺少 novel_id 或名称")
    wid = db.add_world(nid, b.get("category", "other"), b["name"],
                       b.get("content", ""), int(b.get("importance") or 3))
    return {"id": wid, "setting": db.get_world(nid)}


# ============================================================ 角色

@route("GET", r"/api/characters")
def api_characters(q, b):
    db = _db()
    nid = _novel_id(q)
    return {"characters": [_view_character(db, c)
                           for c in db.list_characters(nid, status=q.get("status") or None)]}


@route("GET", r"/api/character")
def api_character(q, b):
    db = _db()
    cid = _as_int(q.get("id"), "id")
    c = db.get_character(cid)
    _require(c, "角色不存在", 404)
    out = _view_character(db, c)
    out["goals"] = db.list_goals(character_id=cid)
    out["relations"] = db.list_relations(c["novel_id"], cid)
    out["identities"] = db.list_identities(c["novel_id"], cid)
    return {"character": out}


@route("POST", r"/api/characters")
def api_add_character(q, b):
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid and (b.get("name") or "").strip(), "缺少 novel_id 或姓名")
    cid = db.add_character(
        nid, b["name"], alias=b.get("alias", ""), rank=b.get("rank", "C"),
        role_tag=b.get("role_tag", ""), personality=b.get("personality", ""),
        background=b.get("background", ""), speech_style=b.get("speech_style", ""),
        decision_tendency=b.get("decision_tendency", ""),
        control_mode=b.get("control_mode", "ai"),
        current_location=b.get("current_location", ""))
    for g in (b.get("goals") or []):
        if isinstance(g, str):
            if g.strip():
                db.add_goal(nid, cid, g, origin="user_added")
        elif g.get("content"):
            db.add_goal(nid, cid, g["content"],
                        goal_type=g.get("goal_type", "short"),
                        motivation=g.get("motivation", ""),
                        obstacle=g.get("obstacle", ""),
                        priority=int(g.get("priority") or 3), origin="user_added")
    return {"id": cid, "characters": [_view_character(db, c)
                                      for c in db.list_characters(nid)]}


@route("GET", r"/api/relations")
def api_relations(q, b):
    """某个世界的全部人物关系（关系网）。"""
    db = _db()
    nid = _novel_id(q)
    cid = q.get("character_id")
    return {"relations": db.list_relations(nid,
                                           int(cid) if cid else None)}


@route("POST", r"/api/relations")
def api_add_relation(q, b):
    """手动加一条人物关系。

    支持按名字（a/b）或按 id（a_id/b_id）指定双方——前端两种场景都有。
    is_mutual 默认 true，会同时落反方向，否则另一边角色的提示词里看不到。
    """
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid, "缺少 novel_id")
    a_id, b_id = _resolve_relation_pair(db, nid, b)
    _require(a_id and b_id, "请指定关系的双方")
    _require(a_id != b_id, "不能给自己建关系")
    from engine.character import _RELATION_LABELS
    rtype = b.get("relation_type") or "other"
    if rtype not in _RELATION_LABELS:
        rtype = "other"
    desc = b.get("description") or ""
    intensity = int(b.get("intensity") or 3)
    db.add_relation(nid, a_id, b_id, relation_type=rtype, description=desc,
                    intensity=intensity)
    if b.get("is_mutual", True):
        db.add_relation(nid, b_id, a_id, relation_type=rtype, description=desc,
                        intensity=intensity)
    return {"relations": db.list_relations(nid)}


def _resolve_relation_pair(db, nid, b):
    """关系双方既能按 id 给，也能按名字给（前端两种都能用）。"""
    a_id = b.get("a_id")
    b_id = b.get("b_id")
    if not a_id and b.get("a"):
        row = db.get_character_by_name(nid, str(b["a"]).strip())
        a_id = row["id"] if row else None
    if not b_id and b.get("b"):
        row = db.get_character_by_name(nid, str(b["b"]).strip())
        b_id = row["id"] if row else None
    try:
        return (int(a_id) if a_id else None, int(b_id) if b_id else None)
    except (TypeError, ValueError):
        return None, None


@route("POST", r"/api/relations/update")
def api_update_relation(q, b):
    db = _db()
    rid = int(b.get("id") or 0)
    _require(rid, "缺少 id")
    fields = {k: v for k, v in (b.get("fields") or {}).items()}
    allowed = ("relation_type", "description", "intensity", "status",
               "updated_chapter")
    sets, vals = [], []
    for k in allowed:
        if k in fields:
            sets.append("%s=?" % k)
            vals.append(fields[k])
    _require(sets, "没有要改的字段")
    vals.append(rid)
    db.execute("UPDATE character_relations SET %s WHERE id=?" % ", ".join(sets),
               vals)
    rid_row = db.one("SELECT novel_id FROM character_relations WHERE id=?", (rid,))
    return {"relations": db.list_relations(rid_row["novel_id"] if rid_row else 0)}


@route("POST", r"/api/relations/delete")
def api_del_relation(q, b):
    db = _db()
    rid = int(b.get("id") or 0)
    _require(rid, "缺少 id")
    row = db.one("SELECT * FROM character_relations WHERE id=?", (rid,))
    if row:
        # 双向关系是两条记录，删一条时把反方向那条一起删掉，
        # 否则关系网会剩下一条"单向残影"，比不删更误导。
        db.execute(
            "DELETE FROM character_relations WHERE novel_id=? AND "
            "character_a_id=? AND character_b_id=? AND relation_type=? AND id<>?",
            (row["novel_id"], row["character_b_id"], row["character_a_id"],
             row["relation_type"], rid))
        db.execute("DELETE FROM character_relations WHERE id=?", (rid,))
    return {"ok": True, "relations": db.list_relations(row["novel_id"])
            if row else []}


@route("POST", r"/api/character/update")
def api_update_character(q, b):
    db = _db()
    cid = int(b.get("id") or 0)
    _require(cid, "缺少 id")
    fields = {k: v for k, v in (b.get("fields") or {}).items()}
    if "tracker" in fields and not isinstance(fields["tracker"], str):
        fields["tracker"] = json.dumps(fields["tracker"], ensure_ascii=False)
    db.update_character(cid, **fields)
    return {"character": _view_character(db, db.get_character(cid))}


@route("POST", r"/api/character/memory")
def api_character_memory(q, b):
    db = _db()
    cid = int(b.get("id") or 0)
    entry = (b.get("entry") or "").strip()
    _require(cid and entry, "缺少 id 或 entry")
    db.append_character_memory(cid, entry)
    return {"character": _view_character(db, db.get_character(cid))}


@route("POST", r"/api/character/die")
def api_character_die(q, b):
    db = _db()
    cid = int(b.get("id") or 0)
    _require(cid, "缺少 id")
    db.mark_character_dead(cid, int(b.get("chapter_ref") or 0), b.get("note", ""))
    return {"character": _view_character(db, db.get_character(cid))}


# ============================================================ 目标

@route("GET", r"/api/goals")
def api_goals(q, b):
    db = _db()
    return {"goals": db.list_goals(novel_id=_novel_id(q),
                                   goal_type=q.get("goal_type") or None,
                                   status=q.get("status") or None)}


@route("POST", r"/api/goals")
def api_add_goal(q, b):
    db = _db()
    nid = int(b.get("novel_id") or 0)
    content = (b.get("content") or "").strip()
    _require(nid and content, "缺少 novel_id 或目标内容")
    cid = b.get("character_id")
    if not cid and b.get("character_name"):
        row = db.get_character_by_name(nid, b["character_name"])
        cid = row["id"] if row else None
    _require(cid, "找不到对应角色")
    gid = db.add_goal(nid, int(cid), content,
                      goal_type=b.get("goal_type", "short"),
                      motivation=b.get("motivation", ""),
                      obstacle=b.get("obstacle", ""),
                      priority=int(b.get("priority") or 3),
                      origin="user_added")
    return {"id": gid, "goals": db.list_goals(novel_id=nid)}


@route("POST", r"/api/goals/update")
def api_update_goal(q, b):
    db = _db()
    gid = int(b.get("id") or 0)
    _require(gid, "缺少 id")
    fields = {k: v for k, v in (b.get("fields") or {}).items()}
    db.update_goal(gid, **fields)
    return {"ok": True}


@route("POST", r"/api/goals/evolve")
def api_evolve_goal(q, b):
    db = _db()
    gid = int(b.get("id") or 0)
    content = (b.get("content") or "").strip()
    _require(gid and content, "缺少 id 或新目标内容")
    db.evolve_goal(gid, content, b.get("motivation", ""),
                   b.get("priority") and int(b["priority"]))
    return {"ok": True}


# ============================================================ 线索 / 事件

@route("GET", r"/api/threads")
def api_threads(q, b):
    db = _db()
    nid = _novel_id(q)
    return {"threads": db.list_threads(nid, status=q.get("status") or "active",
                                       min_tension=int(q.get("min_tension") or 0)),
            "urgent": db.urgent_threads(nid, int(q.get("chapter") or 0))}


@route("POST", r"/api/threads")
def api_add_thread(q, b):
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid and (b.get("title") or "").strip(), "缺少 novel_id 或标题")
    tid = db.add_thread(nid, b["title"], b.get("description", ""),
                        planted_chapter=int(b.get("planted_chapter") or 0),
                        target_chapter=int(b.get("target_chapter") or 0),
                        foreshadowing_type=b.get("foreshadowing_type", "plot"),
                        importance=b.get("importance", "normal"),
                        origin="user",
                        tension_level=int(b.get("tension_level") or 3))
    return {"id": tid, "threads": db.list_threads(nid)}


@route("POST", r"/api/threads/resolve")
def api_resolve_thread(q, b):
    db = _db()
    tid = int(b.get("id") or 0)
    _require(tid, "缺少 id")
    db.resolve_thread(tid, int(b.get("chapter_ref") or 0), b.get("note", ""))
    return {"ok": True}


@route("POST", r"/api/threads/nudge")
def api_nudge_thread(q, b):
    db = _db()
    tid = int(b.get("id") or 0)
    _require(tid, "缺少 id")
    db.nudge_thread(tid, int(b.get("delta") or 1))
    return {"ok": True}


@route("GET", r"/api/events")
def api_events(q, b):
    db = _db()
    nid = _novel_id(q)
    chapter_ref = q.get("chapter_ref")
    rows = db.list_events(
        nid,
        chapter_ref=int(chapter_ref) if chapter_ref not in (None, "", "null") else None,
        event_type=q.get("event_type") or None,
        limit=int(q.get("limit") or 100))
    for r in rows:
        r["structured"] = r.get("structured") or {}
        r["open_threads"] = r.get("open_threads") or []
    return {"events": rows}


# ============================================================ 决策台

@route("GET", r"/api/decisions")
def api_decisions(q, b):
    db = _db()
    nid = _novel_id(q)
    resolved = q.get("resolved")
    rows = db.list_decisions(
        nid, resolved=(None if resolved in (None, "", "all")
                       else resolved in ("1", "true", "yes")),
        chapter_ref=int(q["chapter"]) if q.get("chapter") else None)
    out = []
    sc = None
    for d in rows:
        v = _view_decision(db, d)
        if not v["resolved"]:
            if sc is None:
                from decide.scheduler import DecisionScheduler
                sc = DecisionScheduler(db, None, nid)
            v["needs_user"] = sc.needs_user(d)
        out.append(v)
    return {"decisions": out}


@route("POST", r"/api/decision/options")
def api_decision_options(q, b):
    """为决策点生成选项（同步，约 3-10 秒）。"""
    db = _db()
    did = int(b.get("id") or 0)
    _require(did, "缺少 id")
    d = db.get_decision(did)
    _require(d, "决策点不存在", 404)
    from decide.scheduler import DecisionScheduler
    sc = DecisionScheduler(db, _router(db, d["novel_id"]), d["novel_id"])
    opts = sc.generate_options(did)
    return {"options": opts,
            "decision": _view_decision(db, db.get_decision(did))}


@route("POST", r"/api/decision/resolve")
def api_decision_resolve(q, b):
    db = _db()
    did = int(b.get("id") or 0)
    _require(did, "缺少 id")
    d = db.get_decision(did)
    _require(d, "决策点不存在", 404)
    from decide.scheduler import DecisionScheduler
    sc = DecisionScheduler(db, _router(db, d["novel_id"]), d["novel_id"])
    idx = b.get("option_index")
    if idx is not None and idx != "":
        res = sc.resolve(did, option_index=int(idx), by=b.get("by") or "user")
    else:
        res = sc.resolve(did, option_index=None,
                         freeform=b.get("freeform", ""),
                         by=b.get("by") or "user")
    return {"decision": _view_decision(db, res)}


@route("POST", r"/api/decision/ai-decide")
def api_decision_ai(q, b):
    db = _db()
    did = int(b.get("id") or 0)
    _require(did, "缺少 id")
    d = db.get_decision(did)
    _require(d, "决策点不存在", 404)
    from decide.scheduler import DecisionScheduler
    sc = DecisionScheduler(db, _router(db, d["novel_id"]), d["novel_id"])
    res = sc.ai_decide(did)
    return {"decision": _view_decision(db, res)}


@route("POST", r"/api/decision/simulate")
def api_decision_simulate(q, b):
    """分支预演：只看后果，不落库。"""
    db = _db()
    did = int(b.get("id") or 0)
    _require(did, "缺少 id")
    d = db.get_decision(did)
    _require(d, "决策点不存在", 404)
    from engine.world import WorldEngine
    eng = WorldEngine(db, _router(db, d["novel_id"]), d["novel_id"])
    out = eng.simulate_decision(did, b.get("choice") or "",
                                choice_label=b.get("label") or "", rollback=True)
    return {"events": out["result"].get("events") or [],
            "check": out["check"],
            "world_time": out["result"].get("world_time") or ""}


@route("POST", r"/api/control")
def api_control(q, b):
    """设定角色的托管模式（单个或全部）。"""
    db = _db()
    nid = int(b.get("novel_id") or 0)
    mode = b.get("mode") or ""
    _require(nid, "缺少 novel_id")
    _require(mode in ("ai", "user", "auto_delegate"), "托管模式非法：%s" % mode)
    from decide.scheduler import DecisionScheduler
    sc = DecisionScheduler(db, None, nid)
    if b.get("character_id"):
        sc.set_character_control(int(b["character_id"]), mode)
        n = 1
    elif b.get("character_name"):
        row = db.get_character_by_name(nid, b["character_name"])
        _require(row, "角色不存在：%s" % b["character_name"])
        sc.set_character_control(row["id"], mode)
        n = 1
    else:
        n = sc.bulk_set_control(mode)
    return {"changed": n,
            "characters": [_view_character(db, c)
                           for c in db.list_characters(nid)]}


# ============================================================ 剧情树（推演沙盘）

def _tree(db, nid):
    from engine.tree import StoryTree
    return StoryTree(db, _router(db, nid), nid)


@route("GET", r"/api/tree")
def api_tree(q, b):
    db = _db()
    nid = _novel_id(q)
    _require(nid, "缺少 novel_id")
    _require(db.get_novel(nid), "小说不存在", 404)
    st = _tree(db, nid)
    out = st.tree()
    out["novel"] = {"id": nid, "title": (db.get_novel(nid) or {}).get("title")}
    return out


def _tree_job(kind, nid, fn):
    """剧情树的推演一律放后台：一次 world_sim 可能跑几十秒。"""
    def work(emit):
        d = DB.open_db(DB_PATH)
        try:
            st = _tree(d, nid)
            emit("装配世界简报与这条线上的既有剧情…", 8)
            out = fn(st, emit)
            emit("整理剧情树…", 92)
            out["tree"] = st.tree()
            return out
        finally:
            d.close()
    return JOBS.submit(kind, work)


@route("POST", r"/api/tree/expand")
def api_tree_expand(q, b):
    """从某个节点往下推演一段（到关键节点就停）。"""
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid, "缺少 novel_id")
    node_id = int(b.get("node_id") or 0) or None
    focus = b.get("focus") or ""
    cons = b.get("constraints") or ""

    def fn(st, emit):
        emit("向模型请求推演（一段剧情）…", 35)
        try:
            return st.expand(node_id=node_id, focus=focus, constraints=cons)
        except Exception as e:             # noqa: BLE001
            raise ApiError(str(e), 502)
    return {"job_id": _tree_job("tree_expand", nid, fn)}


@route("POST", r"/api/tree/step")
def api_tree_step(q, b):
    """在岔路口挑一条 + 立刻往下推一段（前端一次点击走完两步）。"""
    db = _db()
    nid = int(b.get("novel_id") or 0)
    did = int(b.get("node_id") or 0)
    _require(nid, "缺少 novel_id")
    _require(did, "缺少 node_id（岔路口节点）")
    idx = b.get("option_index")
    _require(idx is not None and idx != "", "缺少 option_index")
    focus = b.get("focus") or ""

    def fn(st, emit):
        emit("记下你的选择…", 12)
        emit("顺着这条分支往下推演…", 35)
        try:
            return st.choose_and_expand(did, int(idx), focus=focus)
        except Exception as e:             # noqa: BLE001
            raise ApiError(str(e), 502)
    return {"job_id": _tree_job("tree_step", nid, fn)}


@route("POST", r"/api/tree/jump")
def api_tree_jump(q, b):
    """把工作线切到某个节点：从这条线继续 / 回头看看别的分支。"""
    db = _db()
    nid = int(b.get("novel_id") or 0)
    node_id = int(b.get("node_id") or 0)
    _require(nid, "缺少 novel_id")
    _require(node_id, "缺少 node_id")
    st = _tree(db, nid)
    try:
        leaf = st.goto(node_id)
    except Exception as e:                 # noqa: BLE001
        raise ApiError(str(e), 400)
    out = st.tree()
    out["leaf"] = leaf
    return out


@route("POST", r"/api/tree/prune")
def api_tree_prune(q, b):
    """砍掉某节点及其子树，重新推演这条路。"""
    db = _db()
    nid = int(b.get("novel_id") or 0)
    node_id = int(b.get("node_id") or 0)
    _require(nid, "缺少 novel_id")
    _require(node_id, "缺少 node_id")
    st = _tree(db, nid)
    try:
        removed = st.prune(node_id)
    except Exception as e:                 # noqa: BLE001
        raise ApiError(str(e), 400)
    out = st.tree()
    out["removed"] = removed
    return out


@route("POST", r"/api/tree/reset")
def api_tree_reset(q, b):
    """清空整棵树，重新开始（不动已经落定的正文事件）。"""
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid, "缺少 novel_id")
    _require(db.get_novel(nid), "小说不存在", 404)
    st = _tree(db, nid)
    info = st.reset()
    out = st.tree()
    out["reset"] = info
    return out


@route("POST", r"/api/tree/commit")
def api_tree_commit(q, b):
    """把选定路径落成正文事件（进入章节页的"待成章"池）。"""
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid, "缺少 novel_id")
    node_id = int(b.get("node_id") or 0) or None
    st = _tree(db, nid)
    try:
        res = st.commit(node_id)
    except Exception as e:                 # noqa: BLE001
        raise ApiError(str(e), 400)
    out = st.tree()
    out["commit"] = res
    out["candidates"] = db.list_events(nid, chapter_ref=0)
    return out


# ------------------------------------------------------------ 改事件卡（v6.8）

@route("POST", r"/api/tree/card")
def api_tree_card(q, b):
    """取一张卡的可编辑视图（含"能不能改"的说明）。

    前端点开编辑面板时调这个，而不是复用 tree() 里的节点视图：
    节点视图的 card 只有三行，这里还要给出 importance / event_type 等
    可改字段、以及 locked 状态。
    """
    db = _db()
    nid = int(b.get("novel_id") or 0)
    node_id = int(b.get("node_id") or 0)
    _require(nid, "缺少 novel_id")
    _require(node_id, "缺少 node_id")
    st = _tree(db, nid)
    node = db.get_story_node(node_id)
    _require(node and int(node.get("novel_id") or 0) == nid,
             "节点不存在或不属于这部作品", 404)
    out = st.changeable_card(node)
    out["downstream"] = [
        {"id": n["id"], "title": n.get("title") or "",
         "status": n.get("status") or ""}
        for n in st.downstream_of(node_id)]
    return out


@route("POST", r"/api/tree/edit")
def api_tree_edit(q, b):
    """人工直接改卡片的若干字段。同步返回，不调模型，所以不用后台任务。

    冲突回 **409**，其余内容问题回 400。前端得能分开：
      400 = 你填的不对 → 把提示挂在对应输入框上；
      409 = 卡被改过了 → 重新拉一遍这张卡，让人在最新内容上再决定。
    两者混在一起的话，前端只能一律弹一句 toast，用户不知道该改哪里。
    """
    db = _db()
    nid = int(b.get("novel_id") or 0)
    node_id = int(b.get("node_id") or 0)
    _require(nid, "缺少 novel_id")
    _require(node_id, "缺少 node_id")
    fields = b.get("fields") or {}
    _require(isinstance(fields, dict) and fields, "没有要改的内容")
    st = _tree(db, nid)
    try:
        card = st.edit_node(node_id, fields, expected=b.get("expected") or None)
    except StaleEditError as e:
        # 注意是 **extra 而不是 extra={...}：ApiError 签名是 (message, status, **extra)
        raise ApiError(str(e), 409, conflict=True, field=e.field,
                       mine=e.mine, theirs=e.theirs)
    except Exception as e:                 # noqa: BLE001
        raise ApiError(str(e), 400)
    out = st.tree()
    out["card"] = card
    return out


@route("POST", r"/api/tree/ai_fix")
def api_tree_ai_fix(q, b):
    """按文字说明让 AI 改这张卡（后台任务：要调模型）。

    改完**顺手跑一次下游衔接校验**，把结果一起返回——用户的原话是
    「修改后需要校验后续事件是否和修改的内容上下衔接并且符合逻辑」，
    分成两步让他再点一次按钮没有必要。
    校验失败不否决改卡：卡已经改好了，把校验的错误如实带回去就行。
    """
    db = _db()
    nid = int(b.get("novel_id") or 0)
    node_id = int(b.get("node_id") or 0)
    _require(nid, "缺少 novel_id")
    _require(node_id, "缺少 node_id")
    complaint = (b.get("complaint") or "").strip()
    _require(complaint, "请先写清楚哪里不对")
    direction = (b.get("direction") or "").strip()
    want_check = b.get("check", True) is not False

    def fn(st, emit):
        emit("把你的说明交给模型，改这张卡…", 20)
        res = st.ai_fix_node(node_id, complaint, direction=direction)
        if res.get("no_change"):
            emit("模型认为不用改（已在结果里说明）…", 70)
        else:
            emit("已改：%s" % "、".join(res.get("changed") or []), 60)
        out = {"fix": res}
        if want_check:
            emit("检查后续事件是否还接得上…", 78)
            try:
                out["check"] = st.check_downstream(node_id)
            except Exception as e:         # noqa: BLE001
                # 校验挂了不代表改卡失败：如实说，别把已经改好的结果一起吞掉
                out["check"] = {"issues": [], "checked": 0, "affected": 0,
                                "error": str(e),
                                "ok_summary": "衔接校验没能跑完：%s" % e}
        return out
    return {"job_id": _tree_job("tree_ai_fix", nid, fn)}


@route("POST", r"/api/tree/verify")
def api_tree_verify(q, b):
    """单独校验某张卡的下游衔接（改完后想再查一次，或手改完想查）。"""
    db = _db()
    nid = int(b.get("novel_id") or 0)
    node_id = int(b.get("node_id") or 0)
    _require(nid, "缺少 novel_id")
    _require(node_id, "缺少 node_id")

    def fn(st, emit):
        emit("收集这张卡下游的事件…", 25)
        emit("逐条比对因果、事实、认知与世界状态…", 55)
        return {"check": st.check_downstream(node_id)}
    return {"job_id": _tree_job("tree_verify", nid, fn)}



# ============================================================ 世界推演（后台任务）

@route("POST", r"/api/advance")
def api_advance(q, b):
    """启动一次世界推演（后台任务）。返回 job_id。"""
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid, "缺少 novel_id")
    _require(db.get_novel(nid), "小说不存在", 404)
    focus = b.get("focus") or ""
    constraints = b.get("constraints") or ""
    chapter_ref = int(b.get("chapter_ref") or 0)

    def work(emit):
        d = DB.open_db(DB_PATH)
        try:
            from engine.world import WorldEngine, EngineError
            emit("装配世界简报…", 5)
            eng = WorldEngine(d, _router(d, nid), nid)
            emit("向模型请求推演…", 20)
            try:
                out = eng.advance(focus=focus, extra_constraints=constraints,
                                  chapter_ref=chapter_ref)
            except EngineError as e:
                raise ApiError(str(e), 502)
            if out.get("blocked"):
                return {"blocked": True,
                        "reason": out.get("blocked_reason") or "校验未通过",
                        "check": out["check"],
                        "events": out["result"].get("events") or []}
            emit("因果校验通过，整理结构…", 80)
            res = {
                "blocked": False,
                "world_time": out.get("world_time") or "",
                "story_time": out["result"].get("world_time") or "",
                "events": out["result"].get("events") or [],
                "state_changes": out["result"].get("state_changes") or {},
                "open_threads": out["result"].get("open_threads") or [],
                "narrative_note": out["result"].get("narrative_note") or "",
                "check": out["check"],
                "applied": out.get("applied") or {},
                "decision_point_ids": out.get("decision_point_ids") or [],
                "parse_mode": out.get("parse_mode") or "",
            }
            return res
        finally:
            d.close()

    jid = JOBS.submit("world_advance", work)
    return {"job_id": jid}


@route("POST", r"/api/pulse")
def api_pulse(q, b):
    """时间自然演化：推演"与主角无关、但时间自己在改变的事"（后台任务）。

    产出不落正史事件表，只落时间线锚点（timeline），
    后续 advance 的简报会摆给模型看——这就是"蝴蝶效应"的入口。
    """
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid, "缺少 novel_id")
    _require(db.get_novel(nid), "小说不存在", 404)
    time_span = b.get("time_span") or ""
    focus = b.get("focus") or ""

    def work(emit):
        d = DB.open_db(DB_PATH)
        try:
            from engine.world import WorldEngine, EngineError
            emit("装配世界简报…", 10)
            eng = WorldEngine(d, _router(d, nid), nid)
            emit("推演世界自身的动静…", 30)
            try:
                out = eng.pulse(time_span=time_span, focus=focus)
            except EngineError as e:
                raise ApiError(str(e), 502)
            emit("整理背景变化…", 85)
            return {
                "events": out.get("events") or [],
                "anchors": out.get("anchors") or [],
                "parse_mode": out.get("parse_mode") or "",
                "raw": out.get("raw") or "",
            }
        finally:
            d.close()

    return {"job_id": JOBS.submit("world_pulse", work)}


@route("GET", r"/api/timeline")
def api_timeline(q, b):
    db = _db()
    nid = _novel_id(q)
    return {"timeline": db.list_timeline(nid, status=q.get("status") or None)}


@route("POST", r"/api/timeline/status")
def api_timeline_status(q, b):
    """把时间线锚点标成 triggered / expired / cancelled。"""
    db = _db()
    tid = int(b.get("id") or 0)
    _require(tid, "缺少 id")
    st = (b.get("status") or "").strip()
    _require(st in ("pending", "triggered", "expired", "cancelled"),
             "状态非法")
    db.execute("UPDATE timeline SET status=? WHERE id=?", (st, tid))
    return {"ok": True}


@route("POST", r"/api/timeline")
def api_add_timeline(q, b):
    """手动加一个时间线锚点（例如用户自己安排的"三天后的审判"）。"""
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid, "缺少 novel_id")
    anchor = (b.get("time_anchor") or "").strip()
    _require(anchor, "时间锚点不能为空")
    tid = db.add_timeline(
        nid, time_anchor=anchor,
        countdown_name=b.get("countdown_name") or "",
        countdown_value=b.get("countdown_value") or "",
        description=b.get("description") or "",
        time_span=b.get("time_span") or "")
    return {"id": tid}


@route("POST", r"/api/timeline/delete")
def api_del_timeline(q, b):
    db = _db()
    tid = int(b.get("id") or 0)
    _require(tid, "缺少 id")
    db.execute("DELETE FROM timeline WHERE id=?", (tid,))
    return {"ok": True}


# ============================================================ 章节

@route("GET", r"/api/chapters")
def api_chapters(q, b):
    db = _db()
    nid = _novel_id(q)
    rows = db.list_chapters(nid, status=q.get("status") or None)
    out = []
    for c in rows:
        out.append({k: c[k] for k in (
            "id", "chapter_number", "title", "summary", "word_count", "status",
            "pov_character", "chapter_shape", "world_time_start", "world_time_end",
            "ending_mode", "viewpoint", "location", "created_at", "updated_at")
            if k in c.keys()})
    return {"chapters": out, "current_chapter": (db.get_novel(nid) or {}).get("current_chapter") or 0}


@route("GET", r"/api/chapter")
def api_chapter(q, b):
    db = _db()
    nid = _novel_id(q)
    num = _as_int(q.get("chapter"), "chapter")
    ch = db.get_chapter(nid, num)
    _require(ch, "章节不存在", 404)
    return {"chapter": ch,
            "versions": db.list_versions(nid, num),
            "casting": db.list_casting(nid, num),
            "events": db.list_events(nid, chapter_ref=num)}


@route("GET", r"/api/chapter/candidates")
def api_chapter_candidates(q, b):
    """下一章可以写哪些事件（尚未归章的），以及"还能写几章"。"""
    db = _db()
    nid = _novel_id(q)
    rows = [e for e in db.list_events(nid, chapter_ref=0)
            if not e.get("chapter_ref")]
    novel = db.get_novel(nid) or {}
    words = int(novel.get("target_words_per_chapter") or 3000)
    from generate.prose import plan_chapters
    plan = plan_chapters(len(rows), words)
    return {"events": rows,
            "characters": [_view_character(db, c)
                           for c in db.list_characters(nid, status="alive")],
            "current_chapter": novel.get("current_chapter") or 0,
            "target_words_per_chapter": words,
            # 一章装几个事件（按目标字数推出来的），前端默认按它预选
            "suggested_limit": plan["per_chapter"],
            # 这些素材还够写几章
            "planned_chapters": plan["chapters"]}


@route("POST", r"/api/chapter/write")
def api_write_chapter(q, b):
    """生成一章（后台任务，可流式查看进度日志）。"""
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid, "缺少 novel_id")
    _require(db.get_novel(nid), "小说不存在", 404)
    chapter_number = b.get("chapter_number") and int(b["chapter_number"]) or None
    shape = b.get("shape") or None
    words = b.get("words") and int(b["words"]) or None
    extra = b.get("notes") or ""
    summary = b.get("summary") or None
    # 本章写哪些事件：前端勾选的优先；没勾就按 limit 取一批；all=True 才是全都要
    event_ids = [int(i) for i in (b.get("event_ids") or []) if str(i).strip()]
    limit = int(b["limit"]) if (b.get("limit") and str(b["limit"]).isdigit()) else None
    if b.get("all"):
        limit = 9999

    def work(emit):
        d = DB.open_db(DB_PATH)
        try:
            from generate.prose import ProseGenerator, events_per_chapter
            pg = ProseGenerator(d, _router(d, nid), nid)
            novel = d.get_novel(nid) or {}
            cnum = chapter_number or ((novel.get("current_chapter") or 0) + 1)
            emit("收集待成章的事件…", 5)
            pool = [e for e in d.list_events(nid, chapter_ref=0)
                    if not e.get("chapter_ref")]
            if not pool:
                pool = [e for e in d.list_events(nid) if not e.get("chapter_ref")]
            _require(pool, "没有待成章的事件。请到「推演」页落定一条剧情线。")
            if event_ids:
                want = set(event_ids)
                picked = [e for e in pool if e["id"] in want]
                _require(picked, "选中的事件不在待成章列表里（可能已被别的章用掉），"
                                 "刷新页面重新选。")
            else:
                per = limit or events_per_chapter(
                    words or novel.get("target_words_per_chapter"))
                picked = pool[:max(1, int(per))]
            left = max(0, len(pool) - len(picked))
            emit("本章用 %d 个事件（待成章共 %d 个，写完后还剩 %d 个）…"
                 % (len(picked), len(pool), left), 12)
            emit("装配写作上下文…", 18)
            emit("模型正在写第 %s 章（通常 1-3 分钟）…" % cnum, 25)
            res = pg.write_chapter(chapter_number=cnum, events=picked, shape=shape,
                                   words=words, summary=summary, extra=extra)
            res["remaining"] = left
            res["pending_before"] = len(pool)
            emit("已落库（%s 字），导出文本…" % res.get("word_count"), 90)
            path = d.export_chapter_file(nid, cnum)
            res["exported"] = path or ""
            ex = res.get("extracted") or {}
            if ex.get("error"):
                emit("正文回流失败（不影响本章）：%s" % ex["error"], 94)
            elif ex:
                emit("正文回流：世界状态 %d 项、角色状态 %d 项、新认知 %d 条、"
                     "新线索 %d 条、回收线索 %d 条、新角色 %d 个。"
                     % (len(ex.get("world_state") or []),
                        len(ex.get("characters") or []),
                        len(ex.get("knowledge") or []),
                        len(ex.get("new_threads") or []),
                        len(ex.get("resolved_threads") or []),
                        len(ex.get("new_characters") or [])), 94)
            else:
                emit("未做正文回流（状态抽取档位未配置，或模型没给出可解析结果）。"
                     "世界状态不会因此丢，只是这一章的变化要等下次推演才进库。", 94)
            if left:
                emit("还剩 %d 个待成章事件，可以接着写第 %s 章。" % (left, cnum + 1), 97)
            else:
                emit("待成章的事件用完了——想继续就回推演页再推一段。", 97)
            return res
        finally:
            d.close()

    jid = JOBS.submit("write_chapter", work)
    return {"job_id": jid}


@route("POST", r"/api/chapter/release")
def api_chapter_release(q, b):
    """把一章退回成"待成章素材"（正文留档在版本里）。

    场景：一章被塞进太多事件写成了流水账，想拆成几章重写。
    """
    db = _db()
    nid = int(b.get("novel_id") or 0)
    num = int(b.get("chapter_number") or 0)
    _require(nid and num, "缺少 novel_id 或 chapter_number")
    novel = db.get_novel(nid)
    _require(novel, "小说不存在", 404)
    last = int(novel.get("current_chapter") or 0)
    # 只允许退最后一章：退回中间章会让后面已生成的章号错位，
    # 那批事件在时间上也接不回原来的位置。
    _require(num == last,
             "只能退回最后一章（当前最后一章是第 %d 章）。"
             "想改中间章请直接编辑它的正文。" % last, 400)
    res = db.release_chapter(nid, num, note=b.get("note") or "")
    _require(res, "第 %d 章不存在" % num, 404)
    return res


@route("POST", r"/api/chapter/save")
def api_save_chapter(q, b):
    """保存（或覆盖）章节正文，自动留档版本。"""
    db = _db()
    nid = int(b.get("novel_id") or 0)
    num = int(b.get("chapter_number") or 0)
    _require(nid and num, "缺少 novel_id 或 chapter_number")
    res = db.save_chapter(
        nid, num, title=b.get("title") or "", content=b.get("content") or "",
        summary=b.get("summary") or "", status=b.get("status") or "draft",
        change_note=b.get("change_note") or "手动编辑")
    return res


@route("POST", r"/api/chapter/status")
def api_chapter_status(q, b):
    """改章节状态。**变为 published 时把这一章确认进世界。**

    语义（用户明确要求）：点击发布 = 作者确认这一章的内容成立，
    所以这一刻就要让世界状态因为这些内容而更新——不能只翻个 status 字段
    就完事（那样世界页面永远停在推演时的那本旧账上）。

    实现上先执行状态迁移（用户点了一次，就得先落下去），
    再跑 confirm_chapter 做正文回流。回流要花一次 LLM 调用，
    所以放后台任务；回流的成败在状态里体现为 verified 字段，
    前端刷新后能看到"已确认进世界 / 未确认"。
    """
    db = _db()
    nid = int(b.get("novel_id") or 0)
    num = int(b.get("chapter_number") or 0)
    _require(nid and num, "缺少参数")
    status = b.get("status") or "draft"
    # force=1 时，即使这一章已经回流过也重跑一次（正文改过之后用）
    force = _flag(b.get("force"), default=False)
    db.update_chapter_status(nid, num, status)

    if status != "published":
        return {"ok": True, "confirm": None}

    from generate.prose import ProseGenerator

    def work(emit):
        d = DB.open_db(DB_PATH)
        try:
            emit("确认第 %s 章的内容…" % num, 20)
            pg = ProseGenerator(d, _router(d, nid), nid)
            emit("把正文里写出来的变化补进世界…（通常 20-60 秒）", 45)
            res = pg.confirm_chapter(num, force=force)
            if res is None:
                emit("章节不存在", 100)
                return {"confirm": None}
            if res.get("applied"):
                ex = res.get("extracted") or {}
                emit("世界已更新：状态 %d 项、角色 %d 个、认知 %d 条、"
                     "新线索 %d 条、回收 %d 条、新角色 %d 个。"
                     % (len(res.get("world_state") or []),
                        len(res.get("characters") or []),
                        len(res.get("knowledge") or []),
                        len(res.get("new_threads") or []),
                        len(res.get("resolved_threads") or []),
                        len(res.get("new_characters") or [])), 95)
                if ex.get("pending"):
                    emit("另有 %d 条拿不准的说法挂到「待裁决」了。" % ex["pending"], 97)
            else:
                hard = len(res.get("conflicts") or [])
                allc = res.get("all_conflicts") or hard
                extra = ""
                if allc > hard:
                    extra = "（另 %d 条是「正文写了正史没有的新事」，只记不拦）" % (allc - hard)
                emit((res.get("skipped") or "世界未更新。") + extra, 95)
            return {"confirm": res}
        finally:
            d.close()

    return {"ok": True, "job_id": JOBS.submit("confirm_chapter", work)}


@route("POST", r"/api/chapter/export")
def api_chapter_export(q, b):
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid, "缺少 novel_id")
    if b.get("chapter_number"):
        path = db.export_chapter_file(nid, int(b["chapter_number"]))
    else:
        path = db.export_all(nid)
    return {"path": path or ""}


@route("POST", r"/api/chapter/diagnose")
def api_chapter_diagnose(q, b):
    """五层逻辑诊断（后台任务，可看进度日志）。

    作者觉得"哪里不对"但说不清在哪一层时用这个：把世界/人物/关系/位置/事件
    五层事实全部摊开，让模型逐层比对。**只读，不改任何数据。**
    """
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid, "缺少 novel_id")
    _require(db.get_novel(nid), "小说不存在", 404)
    num = int(b.get("chapter_number") or 0)
    _require(num, "缺少 chapter_number")
    complaint = (b.get("complaint") or "").strip()

    def work(emit):
        d = DB.open_db(DB_PATH)
        try:
            from engine import diagnose as DG
            res = DG.diagnose(d, _router(d, nid), nid, num,
                              complaint=complaint, emit=emit)
            return res
        finally:
            d.close()

    return {"job_id": JOBS.submit("chapter_diagnose", work)}


@route("POST", r"/api/chapter/revise")
def api_chapter_revise(q, b):
    """按审查意见或作者自己的方向改稿（后台任务）。

    **不落库**——返回改后正文供前端做对照预览，作者点「应用」才写库
    （写库走 /api/chapter/apply_revision，那里会自动留历史版本）。
    """
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid, "缺少 novel_id")
    _require(db.get_novel(nid), "小说不存在", 404)
    num = int(b.get("chapter_number") or 0)
    _require(num, "缺少 chapter_number")

    issues = b.get("issues") or []
    if not isinstance(issues, list):
        issues = []
    # 只把诊断需要的字段带下去，别把前端的展示字段混进提示词
    kept = []
    for it in issues:
        if not isinstance(it, dict):
            continue
        kept.append({k: str(it.get(k) or "") for k in
                     ("layer", "level", "problem", "quote",
                      "conflicts_with", "suggestion")})
    handle = [str(x or "") for x in (b.get("handle") or [])]
    direction = (b.get("direction") or "").strip()
    _require(kept or direction,
             "没有给改稿依据：要么勾选要采纳的问题，要么写一个修改方向。")

    def work(emit):
        d = DB.open_db(DB_PATH)
        try:
            from engine import diagnose as DG
            return DG.revise(d, _router(d, nid), nid, num, issues=kept,
                             direction=direction, handle=handle, emit=emit)
        finally:
            d.close()

    return {"job_id": JOBS.submit("chapter_revise", work)}


@route("POST", r"/api/chapter/apply_revision")
def api_chapter_apply_revision(q, b):
    """把预览通过的改稿写进库。**自动留历史版本**（改坏了能回滚）。"""
    db = _db()
    nid = int(b.get("novel_id") or 0)
    num = int(b.get("chapter_number") or 0)
    content = b.get("content") or ""
    _require(nid and num, "缺少参数")
    _require(str(content).strip(), "改后正文是空的，不能写入。")
    cur = db.get_chapter(nid, num)
    _require(cur, "章节不存在", 404)
    res = db.save_chapter(
        nid, num, title=cur.get("title") or "", content=content,
        summary=cur.get("summary") or "", status=cur.get("status") or "draft",
        change_note=b.get("change_note") or "按诊断意见修稿")
    # 正文变了，把新出现的变化回流进库（失败不阻塞——稿子已经存好了）
    extracted = None
    try:
        from generate.prose import extract_and_apply_state
        extracted = extract_and_apply_state(db, _router(db, nid), nid,
                                            content, num)
    except Exception as e:                              # noqa: BLE001
        extracted = {"error": "%s: %s" % (type(e).__name__, e)}
    res["extracted"] = extracted
    return res


@route("POST", r"/api/chapter/restore")
def api_chapter_restore(q, b):
    db = _db()
    nid = int(b.get("novel_id") or 0)
    num = int(b.get("chapter_number") or 0)
    ver = int(b.get("version") or 0)
    _require(nid and num and ver, "缺少参数")
    res = db.restore_version(nid, num, ver)
    _require(res, "版本不存在", 404)
    return res


@route("GET", r"/api/casting")
def api_casting(q, b):
    db = _db()
    nid = _novel_id(q)
    num = _as_int(q.get("chapter"), "chapter")
    return {"casting": db.list_casting(nid, num)}


@route("POST", r"/api/casting")
def api_set_casting(q, b):
    db = _db()
    nid = int(b.get("novel_id") or 0)
    num = int(b.get("chapter_number") or 0)
    _require(nid and num, "缺少参数")
    db.set_casting(nid, num, b.get("entries") or [])
    return {"casting": db.list_casting(nid, num)}


# ============================================================ 完结

@route("GET", r"/api/completion")
def api_completion_goals(q, b):
    db = _db()
    nid = _novel_id(q)
    nv = db.get_novel(nid) or {}
    return {"goals": db.list_world_goals(nid),
            "completion_mode": nv.get("completion_mode") or "ai"}


@route("POST", r"/api/completion/goal")
def api_add_completion_goal(q, b):
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid and (b.get("title") or "").strip(), "缺少 novel_id 或标题")
    cid = None
    if b.get("character_name"):
        row = db.get_character_by_name(nid, b["character_name"])
        cid = row["id"] if row else None
    gid = db.add_world_goal(nid, b["title"],
                            goal_type=b.get("goal_type", "world"),
                            description=b.get("description", ""),
                            condition_expr=b.get("condition_expr", ""),
                            character_id=cid,
                            is_primary=1 if b.get("is_primary") else 0)
    return {"id": gid, "goals": db.list_world_goals(nid)}


@route("POST", r"/api/completion/check")
def api_completion_check(q, b):
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid, "缺少 novel_id")
    from engine.world import WorldEngine
    eng = WorldEngine(db, _router(db, nid), nid)
    res = eng.check_completion()
    return {"result": res, "goals": db.list_world_goals(nid)}


@route("POST", r"/api/completion/goal/update")
def api_update_completion_goal(q, b):
    db = _db()
    gid = int(b.get("id") or 0)
    _require(gid, "缺少 id")
    db.update_world_goal(gid, **(b.get("fields") or {}))
    return {"ok": True}


# ============================================================ 作者声音 / 种子

@route("GET", r"/api/voice")
def api_voice(q, b):
    db = _db()
    nid = _novel_id(q)
    return {"voice": db.list_voice(nid, q.get("category") or None,
                                   active_only=q.get("all") != "1")}


@route("POST", r"/api/voice")
def api_add_voice(q, b):
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid and (b.get("content") or "").strip(), "缺少 novel_id 或内容")
    vid = db.add_voice(nid, b.get("category", "syntax"), b["content"],
                       b.get("example", ""), int(b.get("weight") or 3))
    return {"id": vid, "voice": db.list_voice(nid, active_only=False)}


@route("POST", r"/api/voice/toggle")
def api_toggle_voice(q, b):
    db = _db()
    db.toggle_voice(int(b.get("id") or 0), bool(b.get("is_active")))
    return {"ok": True}


@route("POST", r"/api/voice/delete")
def api_delete_voice(q, b):
    db = _db()
    db.delete_voice(int(b.get("id") or 0))
    return {"ok": True}


@route("GET", r"/api/seeds")
def api_seeds(q, b):
    db = _db()
    nid = _novel_id(q)
    return {"seeds": db.list_seeds(nid, q.get("seed_type") or None,
                                   active_only=q.get("all") != "1")}


@route("POST", r"/api/seeds")
def api_add_seed(q, b):
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid and (b.get("name") or "").strip(), "缺少 novel_id 或名称")
    sid = db.add_seed(nid, b["name"], seed_type=b.get("seed_type", "conflict"),
                      description=b.get("description", ""),
                      trigger_condition=b.get("trigger_condition") or {},
                      skeleton=b.get("skeleton") or {},
                      tags=b.get("tags") or [],
                      intensity=int(b.get("intensity") or 3))
    return {"id": sid, "seeds": db.list_seeds(nid, active_only=False)}


@route("POST", r"/api/seeds/use")
def api_use_seed(q, b):
    db = _db()
    db.mark_seed_used(int(b.get("id") or 0), int(b.get("chapter_ref") or 0))
    return {"ok": True}


# ============================================================ 检索 / 上下文

@route("GET", r"/api/search")
def api_search(q, b):
    db = _db()
    kw = (q.get("q") or "").strip()
    if not kw:
        return {"results": [], "keyword": ""}
    return {"results": db.search(_novel_id(q), kw, int(q.get("limit") or 20)),
            "keyword": kw}


@route("GET", r"/api/context")
def api_context(q, b):
    db = _db()
    nid = _novel_id(q)
    target = int(q["chapter"]) if q.get("chapter") else None
    return {"markdown": db.context_markdown(nid, target_chapter=target)}


# ============================================================ 配置：服务商 / 档位

def _decorate_providers(rows):
    """给服务商行补上 has_key / key_masked。

    保存和列表两个接口必须用同一套装饰，否则前端拿到的字段时有时无，
    会出现"明明存了密钥却显示缺密钥"这类假故障。
    """
    from llm import secrets as SEC
    refs = {r["ref"]: r["masked"] for r in SEC.list_key_refs()}
    for p in rows:
        p["key_masked"] = refs.get(p.get("api_key_ref") or "", "")
        p["has_key"] = bool(refs.get(p.get("api_key_ref") or ""))
    return rows


@route("GET", r"/api/providers")
def api_providers(q, b):
    from llm import secrets as SEC
    db = _db()
    rows = _decorate_providers(db.list_providers())
    return {"providers": rows, "secrets_path": SEC.secrets_path(),
            "key_refs": SEC.list_key_refs()}


@route("POST", r"/api/providers")
def api_save_provider(q, b):
    from llm import secrets as SEC
    db = _db()
    name = (b.get("name") or "").strip()
    _require(name, "服务商名称不能为空")

    # 编辑已有记录：按主键更新（允许改名，不会另插一条）
    pid = int(b.get("id") or 0)
    old = db.get_provider(pid) if pid else None

    if not old and not pid:
        # 没带 id 却填了跟已有服务商完全相同的名字：前端本该走编辑，
        # 说明前端状态过期了。与其静默 upsert 覆盖掉那条记录，不如明说。
        same = [p for p in db.list_providers() if p["name"] == name]
        if same:
            pid = same[0]["id"]
            old = same[0]

    if old:
        # 优先沿用原 key 引用；用户在弹层里显式改了引用才换
        ref = (b.get("api_key_ref") or "").strip() or (old.get("api_key_ref") or "") \
            or ("provider:%s" % name.lower())
    else:
        ref = (b.get("api_key_ref") or "").strip() or ("provider:%s" % name.lower())

    if b.get("api_key"):
        SEC.set_key(ref, b["api_key"])

    models = b.get("models")
    if models is None and b.get("preset_key"):
        # 选了厂商预设又没手工填模型：用预设的清单打底
        from llm import client as LLM
        models = list((LLM.PROVIDERS.get(b["preset_key"]) or {}).get("models") or [])

    try:
        db.add_provider(name, kind=b.get("kind", "openai"),
                        base_url=b.get("base_url", ""), api_key_ref=ref,
                        note=b.get("note", ""),
                        preset_key=b.get("preset_key", ""), models=models,
                        provider_id=pid or None)
    except Exception as e:                            # noqa: BLE001
        # 改名撞上另一个同名服务商：明确告知，而不是甩 500
        if "UNIQUE" in str(e).upper():
            raise ApiError("已存在名为「%s」的服务商，换个名字，"
                           "或直接编辑那一条。" % name)
        raise

    # 改名后把档位里指向旧名字的记录一起对齐（按 id 对齐，天然幂等）
    rows = _decorate_providers(db.list_providers())
    if pid:
        saved = [p for p in rows if p["id"] == pid]
    else:
        saved = [p for p in rows if p["name"] == name]
    return {"providers": rows, "key_ref": ref,
            "id": saved[0]["id"] if saved else None,
            "models": (saved[0].get("models") if saved else []) or []}



@route("POST", r"/api/providers/delete")
def api_delete_provider(q, b):
    db = _db()
    pid = int(b.get("id") or 0)
    _require(pid, "缺少 id")
    db.execute("DELETE FROM providers WHERE id=?", (pid,))
    return {"ok": True}


@route("POST", r"/api/providers/test")
def api_test_provider(q, b):
    """连通性自检。

    模型解析优先级（修复"填了 Key 却测不过"）：
      1. 请求显式带 model（前端下拉/输入框）
      2. 服务商记录里存的 models[0]
      3. 厂商预设的 models[0]
      4. 明确报错，而不是拿空模型去发请求
    """
    from llm import client as LLM
    from llm import secrets as SEC
    db = _db()
    name = (b.get("name") or "").strip()
    base_url = b.get("base_url") or ""
    model = (b.get("model") or "").strip()
    ref = b.get("api_key_ref") or ""
    key = b.get("api_key") or ""
    kind = (b.get("kind") or "").strip()
    prov = None

    # 前端表单传的是 models（数组，逗号分隔输入框解析而来），
    # 而单模型字段叫 model。两者都要认，否则用户填的模型名会被当成没填，
    # 直接落到"没有可用的模型名"的死路（曾真实踩过）。
    if not model:
        ms = b.get("models")
        if isinstance(ms, str):
            ms = [m.strip() for m in ms.split(",") if m.strip()]
        if isinstance(ms, (list, tuple)) and ms:
            model = str(ms[0]).strip()

    if b.get("provider_id"):
        prov = db.get_provider(int(b["provider_id"]))
        if prov:
            name = name or prov.get("name") or ""
            base_url = base_url or prov.get("base_url") or ""
            kind = kind or prov.get("kind") or "openai"
            ref = ref or prov.get("api_key_ref") or ""
            if not model:
                model = (prov.get("models") or [""])[0] if prov.get("models") else ""
    if not key and ref:
        key = SEC.get_key(ref)

    # ---- 模型兜底：服务商清单 -> 厂商预设 ----
    preset_key = (b.get("preset_key") or (prov or {}).get("preset_key") or "").strip()
    if not preset_key:
        preset_key = name.lower() if name else ""
    if preset_key not in LLM.PROVIDERS:
        preset_key = "custom" if not name else preset_key
        if preset_key not in LLM.PROVIDERS:
            preset_key = "custom"
    if not model:
        preset_models = (LLM.PROVIDERS.get(preset_key) or {}).get("models") or []
        model = preset_models[0] if preset_models else ""
    if not model:
        return {"ok": False,
                "error": "没有可用的模型名。请在「模型名」输入框里填一个，"
                         "例如 deepseek/deepseek-flash。",
                "hint": "聚合网关（腾讯 tokenhub、硅基流动等）的模型名通常要带"
                        "厂商前缀，写成 deepseek/deepseek-flash 这样；"
                        "不要在厂商官网找裸名，裸名会报 400004。"}

    if preset_key == "custom":
        if not base_url:
            return {"ok": False, "error": "缺少 Base URL，无法测试。"}
        return LLM.test_connection("custom", base_url, key, model)
    return LLM.test_connection(preset_key, base_url or
                               LLM.PROVIDERS[preset_key]["base_url"], key, model)


@route("GET", r"/api/slots")
def api_slots(q, b):
    db = _db()
    nid = _novel_id(q)
    rt = LLMRouter(db, nid)
    ready = rt.ready_slots()
    out = []
    for slot, label in SLOT_LABELS.items():
        p = db.get_model_preset(slot, novel_id=nid)
        out.append({
            "slot": slot, "label": label,
            "provider_id": (p or {}).get("provider_id"),
            "model": (p or {}).get("model") or "",
            "temperature": (p or {}).get("temperature", 0.8),
            "max_tokens": (p or {}).get("max_tokens", 4096),
            "ok": ready[slot]["ok"], "reason": ready[slot]["reason"],
            "overridden": bool(p and p.get("novel_id") == nid and nid),
        })
    return {"slots": out, "providers": db.list_providers()}


@route("POST", r"/api/slots")
def api_set_slot(q, b):
    db = _db()
    slot = b.get("slot") or ""
    _require(slot in SLOT_LABELS, "未知档位：%s" % slot)
    nid = int(b.get("novel_id") or 0)
    db.set_model_preset(slot, b.get("model") or "",
                        provider_id=b.get("provider_id") and int(b["provider_id"]) or None,
                        temperature=float(b.get("temperature") or 0.8),
                        max_tokens=int(b.get("max_tokens") or 4096),
                        novel_id=nid)
    return {"ok": True}


@route("POST", r"/api/slots/bulk")
def api_set_slots_bulk(q, b):
    """一键配置：把某个厂商的一套模型铺到全部档位。"""
    db = _db()
    pid = int(b.get("provider_id") or 0)
    nid = int(b.get("novel_id") or 0)
    mapping = b.get("mapping") or {}
    _require(pid, "缺少 provider_id")
    _require(mapping, "缺少 mapping（档位->模型）")
    for slot, model in mapping.items():
        if slot in SLOT_LABELS and model:
            db.set_model_preset(slot, model, provider_id=pid, novel_id=nid)
    return {"ok": True, "configured": len(mapping)}


@route("GET", r"/api/usage")
def api_usage(q, b):
    db = _db()
    return db.usage_summary(_novel_id(q))


# ============================================================ 任务

@route("GET", r"/api/job")
def api_job(q, b):
    jid = (q.get("id") or "").strip()
    _require(jid, "缺少 id")
    job = JOBS.get(jid, since=int(q.get("since") or 0))
    _require(job, "任务不存在", 404)
    return {"job": job, "next_since": (int(q.get("since") or 0) +
                                       len(job["log"]))}


@route("POST", r"/api/job/prune")
def api_job_prune(q, b):
    JOBS.prune()
    return {"ok": True}


# ============================================================ 正史账本（v6.7）
#
# 第一页「世界控制台」的四个新入口。读接口一律**只读**——
# 唯一能改正史的是 tree.commit() 与冲突裁决，不是这里。

def _canon_bundle(db, nid):
    """正史相关的公共零件（facts 模块 + ticktime），延迟 import。"""
    from engine import facts as F
    from engine import ticktime as T
    return F, T


@route("GET", r"/api/canon/summary")
def api_canon_summary(q, b):
    """第一页顶部状态条：tick / 时间 / 事实数 / 规则数 / 待决点 / 冲突 / 候选。

    **一次查询全拿到**（不 N+1）：前端状态条每 30 秒刷一次。
    """
    db = _db()
    nid = _novel_id(q)
    _require(nid, "缺少 novel_id")
    F, T = _canon_bundle(db, nid)
    s = F.summary(db, nid)
    s.update({
        "novel_id": nid,
        "tick": T.current_tick(db, nid),
        "display_time": T.display_time(db, nid),
        "tick_unit_note": T.unit_note(db, nid),
        "pending_decisions": int(db.scalar(
            "SELECT COUNT(*) FROM decision_points WHERE novel_id=? "
            "AND resolved_at IS NULL", (nid,), default=0) or 0),
        "unverified_chapters": int(db.scalar(
            "SELECT COUNT(*) FROM chapters WHERE novel_id=? AND "
            "deleted_at IS NULL AND verified=0", (nid,), default=0) or 0),
        "entities": int(db.scalar(
            "SELECT COUNT(*) FROM world_entities WHERE novel_id=? "
            "AND deleted_at IS NULL", (nid,), default=0) or 0),
        "characters": int(db.scalar(
            "SELECT COUNT(*) FROM characters WHERE novel_id=?",
            (nid,), default=0) or 0),
    })
    return {"summary": s}


@route("GET", r"/api/canon/facts")
def api_canon_facts(q, b):
    """正史账本列表。支持按主体/类型/时刻过滤，默认只给 active。"""
    db = _db()
    nid = _novel_id(q)
    _require(nid, "缺少 novel_id")
    F, T = _canon_bundle(db, nid)
    as_of = q.get("as_of_tick")
    rows = F.list_facts(
        db, nid,
        subject_type=(q.get("subject_type") or "").strip() or None,
        subject_name=(q.get("subject") or q.get("subject_name") or "").strip()
        or None,
        predicate=(q.get("predicate") or "").strip() or None,
        fact_type=(q.get("fact_type") or "").strip() or None,
        active_only=_flag(q.get("active_only"), default=True),
        status=(q.get("status") or "").strip() or None,
        as_of_tick=int(as_of) if as_of else None,
        limit=int(q.get("limit") or 300))
    return {"facts": [F.fact_view(r) for r in rows],
            "total": len(rows),
            "as_of_tick": int(as_of) if as_of else None,
            "tick": T.current_tick(db, nid)}


@route("POST", r"/api/canon/fact")
def api_canon_fact_write(q, b):
    """手工新增 / 作废一条正史事实（第一页正史账本抽屉里的两个按钮）。"""
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid, "缺少 novel_id")
    F, T = _canon_bundle(db, nid)
    op = (b.get("op") or "add").strip()
    if op == "revoke":
        fid = _as_int(b.get("fact_id"), "fact_id")
        _require(fid, "缺少 fact_id")
        n = F.revoke_fact(db, fid, reason=b.get("reason") or "作者手工作废")
        return {"ok": True, "revoked": n}
    if op == "unlock":
        # 世界状态被手工锁定 → 恢复自动投影
        key = (b.get("key") or "").strip()
        _require(key, "缺少 key")
        return {"ok": True, "unlocked": db.unlock_state_value(nid, key)}
    pred = (b.get("predicate") or "").strip()
    _require(pred, "缺少 predicate")
    fid = F.add_fact(
        db, nid,
        subject_type=b.get("subject_type") or "world",
        subject_id=b.get("subject_id"),
        subject_name=b.get("subject_name") or b.get("subject") or "",
        predicate=pred,
        object_text=b.get("object_text") if b.get("object_text") is not None else "",
        object_type=b.get("object_type") or "text",
        fact_type=b.get("fact_type") or "other",
        tick=int(b.get("tick") or T.current_tick(db, nid)),
        source_kind="user", fact_status="canonical", confidence=5,
        classified_by="user", note=b.get("note") or "作者手工写入",
        quote=b.get("quote") or "")
    _require(fid, "写入失败（谓词为空？）")
    # 状态类事实可能改变投影，让缓存跟上（转调唯一落库点）
    try:
        F.write_state_projection(db, nid)
    except Exception:                       # noqa: BLE001
        pass
    return {"ok": True, "fact_id": fid}


@route("GET", r"/api/canon/knowledge")
def api_canon_knowledge(q, b):
    """知识矩阵：行=角色，列=认知对象，格=KNOW/BELIEVE/SUSPECT/ASSUME/—。"""
    db = _db()
    nid = _novel_id(q)
    _require(nid, "缺少 novel_id")
    rows = db.list_knowledge(
        nid, character_id=_as_opt_int(q.get("character_id"), "character_id"),
        know_type=(q.get("know_type") or "").strip() or None,
        about_type=(q.get("about_type") or "").strip() or None,
        status=(q.get("status") or "active").strip() or None,
        limit=int(q.get("limit") or 500))
    names = {c["id"]: c["name"] for c in db.list_characters(nid)}
    items = []
    for r in rows:
        items.append({
            "id": r["id"], "character_id": r["character_id"],
            "character_name": names.get(r["character_id"]) or "",
            "know_type": r.get("know_type"), "about_type": r.get("about_type"),
            "about_id": r.get("about_id"), "about_text": r.get("about_text") or "",
            "content": r.get("content") or "", "source": r.get("source"),
            "confidence": r.get("confidence"), "is_false": r.get("is_false"),
            "status": r.get("status"),
            "learned_chapter": r.get("learned_chapter"),
            "learned_tick": r.get("learned_tick"),
        })
    # 矩阵视图：按角色分组的紧凑结构（前端直接画格）
    matrix = {}
    for it in items:
        matrix.setdefault(it["character_name"] or "（未知）", []).append({
            "about": it["about_text"] or it["content"][:20],
            "know_type": it["know_type"], "content": it["content"]})
    return {"knowledge": items, "matrix": matrix, "total": len(items)}


@route("GET", r"/api/canon/rules")
def api_canon_rules(q, b):
    db = _db()
    nid = _novel_id(q)
    _require(nid, "缺少 novel_id")
    return {"rules": db.list_world_rules(
        nid, active_only=_flag(q.get("active_only"), default=True),
        scope=(q.get("scope") or "").strip() or None)}


@route("POST", r"/api/canon/rules")
def api_canon_rules_write(q, b):
    """世界规则增 / 改 / 删。删用 op=delete（HTTP 层没有 DELETE 方法）。"""
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid, "缺少 novel_id")
    op = (b.get("op") or "add").strip()
    if op == "delete":
        rid = _as_int(b.get("rule_id"), "rule_id")
        _require(rid, "缺少 rule_id")
        return {"ok": True, "deleted": db.delete_world_rule(rid)}
    if op == "update":
        rid = _as_int(b.get("rule_id"), "rule_id")
        _require(rid, "缺少 rule_id")
        fields = {k: b[k] for k in ("content", "scope", "subject_name",
                                    "is_hard", "severity", "check_hint",
                                    "is_active") if k in b}
        return {"ok": True, "updated": db.update_world_rule(rid, **fields)}
    content = (b.get("content") or "").strip()
    _require(content, "规则内容不能为空")
    rid = db.add_world_rule(
        nid, content,
        rule_key=b.get("rule_key") or "",
        scope=b.get("scope") or "global",
        subject_name=b.get("subject_name") or "",
        is_hard=b.get("is_hard", 1),
        severity=b.get("severity") or "error",
        check_hint=b.get("check_hint") or "",
        source="user")
    # 规则也是"世界的硬事实"——同步记进正史账本，否则推演看不到它
    try:
        from engine import facts as F
        F.register_rule_as_fact(db, nid, rid, content,
                               tick=F._current_tick(db, nid))
    except Exception:                       # noqa: BLE001
        pass
    return {"ok": True, "rule_id": rid}


@route("POST", r"/api/canon/rebuild")
def api_canon_rebuild(q, b):
    """由正史事件重建 facts。**只在迁移后手工点一次。**

    为什么不自动做：老库的 events.structured 可能是空/半截（v6.x 前期截断
    抢救的产物），强行投影会造出**错误的正史**——比没有正史更糟。
    重建出来的全部标 derived，出错可整批撤销（op=clear）。
    """
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid, "缺少 novel_id")
    from engine import facts as F
    if (b.get("op") or "rebuild").strip() == "clear":
        return {"ok": True, "cleared": F.clear_derived(db, nid)}
    if _flag(b.get("dry_run"), default=False):
        return {"dry_run": F.rebuild_from_events(db, nid, dry_run=True)}

    def work(emit):
        emit("读取已落定事件…")
        res = F.rebuild_from_events(db, nid)
        emit("重建完成：%d 条事实（跳过 %d 个无结构化数据的事件）"
             % (res.get("facts", 0), res.get("skipped", 0)))
        res["projection"] = F.write_state_projection(db, nid)
        return res

    return {"job_id": JOBS.submit("canon_rebuild", work)}


@route("GET", r"/api/canon/candidates")
def api_canon_candidates(q, b):
    """待裁决候选事实（第一页第四个抽屉）。默认只给 pending。"""
    db = _db()
    nid = _novel_id(q)
    _require(nid, "缺少 novel_id")
    rows = db.list_candidates(
        nid, status=(q.get("status") or "pending").strip() or None,
        classification=(q.get("classification") or "").strip() or None,
        chapter_number=_as_opt_int(q.get("chapter"), "chapter"),
        limit=int(q.get("limit") or 100))
    names = {c["id"]: c["name"] for c in db.list_characters(nid)}
    items = []
    for r in rows:
        items.append({
            "id": r["id"], "source_kind": r.get("source_kind"),
            "source_chapter": r.get("source_chapter"),
            "quote": r.get("quote") or "", "provenance": r.get("provenance") or "",
            "subject_type": r.get("subject_type"),
            "subject_name": r.get("subject_name") or "",
            "predicate": r.get("predicate") or "",
            "object_text": r.get("object_text") or "",
            "fact_type": r.get("fact_type"),
            "classification": r.get("classification"),
            "classified_by": r.get("classified_by"),
            "classify_reason": r.get("classify_reason") or "",
            "confidence": r.get("confidence"),
            "status": r.get("status"),
            "target_character_name": names.get(r.get("target_character_id")) or "",
            "statement": "%s %s：%s" % (r.get("subject_name") or "",
                                        r.get("predicate") or "",
                                        r.get("object_text") or ""),
        })
    return {"candidates": items, "total": len(items)}


@route("POST", r"/api/canon/candidate/resolve")
def api_canon_candidate_resolve(q, b):
    """裁决一条候选事实：接受（按分类落到正确出口）/ 否决 / 改分类。"""
    db = _db()
    cid = _as_int(b.get("candidate_id"), "candidate_id")
    _require(cid, "缺少 candidate_id")
    action = (b.get("action") or "reject").strip()
    return db.resolve_candidate(cid, action=action, note=b.get("note") or "",
                                classification=b.get("classification") or None)


@route("POST", r"/api/canon/candidate/resolve_all")
def api_canon_candidate_resolve_all(q, b):
    """一键「全部标为已阅」（候选区必须是灰的、可忽略的，不能变成负担）。"""
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid, "缺少 novel_id")
    rows = db.list_candidates(nid, status="pending", limit=500)
    n = 0
    for r in rows:
        try:
            db.resolve_candidate(r["id"], action="reject",
                                 note=b.get("note") or "批量标为已阅")
            n += 1
        except Exception:                   # noqa: BLE001
            continue
    return {"ok": True, "resolved": n}


# ============================================================ 沙盘分支与落定（v6.7）

@route("POST", r"/api/tree/branch")
def api_tree_branch(q, b):
    """查看某个分支的状态：base tick + 累计 overlay + 校验报告（只读）。"""
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid, "缺少 novel_id")
    from engine import tree as _tree
    from engine import facts as F
    from engine import ticktime as T
    t = _tree.StoryTree(db, _router(db, nid), nid)
    branch_id = _as_opt_int(b.get("branch_id"), "branch_id", 0)
    if not branch_id:
        branch_id = t._node_branch(t.leaf_id() or 0)
    try:
        st = t.branch_status(branch_id)
    except Exception as e:                          # noqa: BLE001
        st = {"error": "%s: %s" % (type(e).__name__, e)}
    return {
        "branch_id": branch_id,
        "current_node": t.leaf_id(),
        "branches": [
            {"id": x["id"], "label": x.get("label") or "",
             "status": x.get("status"), "base_tick": x.get("base_tick"),
             "root_node_id": x.get("root_node_id")}
            for x in db.list_branches(nid)],
        "overlay": st.get("overlay") or {},
        "overlay_count": st.get("overlay_count") or 0,
        "check": st.get("check") or {},
        "base_tick": st.get("base_tick") or 0,
        "base_event_id": st.get("base_event_id") or 0,
        "node_count": len(st.get("nodes") or []),
        "tick": T.current_tick(db, nid),
        "canon_facts": F.count_facts(db, nid),
    }


@route("POST", r"/api/tree/precheck")
def api_tree_precheck(q, b):
    """落定前的预校验（只读，不改一个字节）。

    与 commit 的分工：「落定为正史」按钮**先调它**，有 error 时弹确认框
    （不能落）；只有 warning 时列给用户看，由用户决定要不要落。
    """
    db = _db()
    nid = int(b.get("novel_id") or 0)
    _require(nid, "缺少 novel_id")
    from engine import tree as _tree
    t = _tree.StoryTree(db, _router(db, nid), nid)
    node_id = _as_opt_int(b.get("node_id"), "node_id")
    rep = t.precheck(node_id)
    _require(isinstance(rep, dict), "预校验返回异常")
    rep["can_commit"] = not (rep.get("errors") or [])
    return rep


# ============================================================ 章节事实检查与冲突裁决

@route("POST", r"/api/chapter/fact_check")
def api_chapter_fact_check(q, b):
    """对已有章节重跑一次事实检查（用户手改过正文之后用）。"""
    db = _db()
    nid = int(b.get("novel_id") or 0)
    num = _as_int(b.get("chapter_number"), "chapter_number")
    _require(nid and num, "缺少参数")
    ch = db.get_chapter(nid, num)
    _require(ch, "章节不存在", 404)
    content = (b.get("content") or ch.get("content") or "")
    _require(str(content).strip(), "章节正文是空的")

    def work(emit):
        emit("抽取正文里的事实…")
        from generate.prose import extract_and_apply_state
        emit("与正史逐条比对…")
        res = extract_and_apply_state(db, _router(db, nid), nid, content, num)
        if not res:
            return {"error": "抽取档位未配置或没有可解析结果", "conflicts": []}
        emit("完成：%d 处未裁决冲突" % len(res.get("conflicts") or []))
        return res

    return {"job_id": JOBS.submit("chapter_fact_check", work)}


@route("POST", r"/api/chapter/conflict/resolve")
def api_chapter_conflict_resolve(q, b):
    """裁决一条冲突：keep_canon / keep_prose / ignored（设计 §7.3）。"""
    db = _db()
    cid = _as_int(b.get("conflict_id"), "conflict_id")
    _require(cid, "缺少 conflict_id")
    row = db.one("SELECT novel_id FROM fact_conflicts WHERE id=?", (cid,))
    _require(row, "冲突记录不存在", 404)
    nid = int(row["novel_id"])
    from engine import facts as F
    from engine import ticktime as T
    return F.resolve_conflict(db, nid, cid,
                              action=(b.get("action") or "").strip(),
                              note=b.get("note") or "",
                              tick=T.current_tick(db, nid))


@route("GET", r"/api/chapter/conflicts")
def api_chapter_conflicts(q, b):
    """某一章（或全书）的冲突列表，供红色徽标与冲突卡片渲染。"""
    db = _db()
    nid = _novel_id(q)
    _require(nid, "缺少 novel_id")
    rows = db.list_conflicts(
        nid, status=(q.get("status") or "open").strip() or None,
        chapter_number=_as_opt_int(q.get("chapter"), "chapter"),
        limit=int(q.get("limit") or 100))
    return {"conflicts": rows, "total": len(rows)}


# ============================================================ HTTP Handler

_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".png": "image/png",
}


class Handler(BaseHTTPRequestHandler):
    server_version = "NovelWriter/6.0"
    protocol_version = "HTTP/1.1"

    # ---------- 输出

    def _send(self, status, payload, content_type="application/json; charset=utf-8"):
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        elif isinstance(payload, str):
            body = payload.encode("utf-8")
        else:
            body = payload
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_static(self, path):
        """静态资源。

        规则：
          /                  -> static/index.html
          /app.js            -> static/app.js（裸路径也认，方便手写引用）
          /static/app.js     -> static/app.js
          无扩展名的未知路径 -> static/index.html（前端路由兜底）
          有扩展名的未知路径 -> 404
        """
        if path.startswith("/static/"):
            rel = path[len("/static/"):]
        elif path == "/" or path == "/index.html":
            rel = "index.html"
        else:
            rel = path.lstrip("/")
        rel = rel or "index.html"
        if ".." in rel or rel.startswith("/") or ":" in rel:
            return self._send(403, {"error": "非法路径"})

        full = os.path.join(STATIC_DIR, rel.replace("/", os.sep))
        if not os.path.isfile(full):
            if "." not in os.path.basename(rel):
                # 前端路由兜底：交给 index.html 自己处理
                full = os.path.join(STATIC_DIR, "index.html")
            if not os.path.isfile(full):
                return self._send(404, {"error": "文件不存在：%s" % rel})
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as f:
            data = f.read()
        return self._send(200, data, _MIME.get(ext, "application/octet-stream"))

    # ---------- 请求

    def _handle(self, method):
        parsed = urllib.parse.urlparse(self.path)
        # HTTP 请求行按 RFC 是 latin-1 解码进来的，而实际发出的（curl、手粘地址栏、
        # 某些 HTTP 客户端）往往是**未百分号编码的 UTF-8 字节**。不做修复的话，
        # `predicate=天气` 会变成一串乱码，`parse_qs` 再 replace 掉，
        # 于是「按中文过滤」**静默返回 0 条**——不是报错，是什么都查不到。
        # 这正是本项目最怕的失败形态（数据没错，展示层自己丢条件）。
        # 修法：把 latin-1 字符串按字节还原再按 utf-8 解码（纯 ASCII 时是恒等变换，
        # 所以对正常请求零影响）。
        raw_query = parsed.query
        if any(ord(ch) > 127 for ch in raw_query):
            try:
                raw_query = raw_query.encode("latin-1").decode("utf-8")
            except (UnicodeDecodeError, UnicodeEncodeError):
                pass                                  # 修不了就用原串，至少不炸
        path = urllib.parse.unquote(parsed.path)
        query = {k: v[0] for k, v in
                 urllib.parse.parse_qs(raw_query).items()}
        body = {}
        if method == "POST":
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY:
                return self._send(413, {"error": "请求体过大"})
            raw = self.rfile.read(length) if length else b""
            if raw:
                try:
                    body = json.loads(raw.decode("utf-8"))
                except ValueError as e:
                    return self._send(400, {"error": "请求体不是合法 JSON：%s" % e})
                if not isinstance(body, dict):
                    return self._send(400, {"error": "请求体必须是 JSON 对象"})
        if not path.startswith("/api/"):
            if method != "GET":
                return self._send(405, {"error": "静态资源只支持 GET"})
            return self._serve_static(path)
        try:
            status, payload = dispatch(method, path, query, body)
            return self._send(status, payload)
        except ApiError as e:
            out = {"error": e.message}
            out.update(e.extra)
            return self._send(e.status, out)
        except sqlite3.OperationalError as e:
            # 缺表/缺列几乎只有一个原因：**库没跟着代码迁移**。
            # 默认行为是甩一屏 traceback 给前端——用户看不懂，也不知道该干什么。
            # 这里认出这个特定形态，回一句人话 + 该执行的命令。
            tb = traceback.format_exc()
            sys.stderr.write(tb)
            msg = str(e)
            out = {"error": "%s: %s" % (type(e).__name__, msg),
                   "traceback": tb[-1500:]}
            if "no such table" in msg or "no such column" in msg:
                out["needs_migration"] = True
                out["hint"] = ("数据库结构与当前代码不匹配。"
                               "请先执行迁移：python src_v6/core/migrate.py"
                               "（自动备份，可重复执行；迁移前会再备一份）")
            return self._send(500, out)
        except Exception as e:                       # noqa: BLE001
            tb = traceback.format_exc()
            sys.stderr.write(tb)
            return self._send(500, {"error": "%s: %s" % (type(e).__name__, e),
                                    "traceback": tb[-1500:]})

    def do_GET(self):                                # noqa: N802
        self._handle("GET")

    def do_POST(self):                               # noqa: N802
        self._handle("POST")

    def do_OPTIONS(self):                            # noqa: N802
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, fmt, *args):
        if os.environ.get("NOVEL_WEB_VERBOSE"):
            sys.stderr.write("[web] %s\n" % (fmt % args))


def serve(port=8787, host="127.0.0.1", open_browser=False, db_path=None):
    global DB_PATH
    if db_path:
        DB_PATH = os.path.abspath(db_path)
    DB.ensure_db(DB_PATH)
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    url = "http://%s:%d/" % (host, port)
    print("=" * 62)
    print("  AI 小说创作系统 v6.0 · 世界模拟范式")
    print("  数据库：%s" % DB_PATH)
    print("  管理台：%s" % url)
    print("  按 Ctrl+C 停止")
    print("=" * 62)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        httpd.server_close()


def main():
    ap = argparse.ArgumentParser(description="AI 小说创作系统 v6.0 Web 管理台")
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--db", default=None)
    ap.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    args = ap.parse_args()
    serve(port=args.port, host=args.host, open_browser=args.open,
          db_path=args.db)


if __name__ == "__main__":
    main()
