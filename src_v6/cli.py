# -*- coding: utf-8 -*-
"""
v6.0 命令行入口。

命令分组：
  世界：world-init  world-advance  world-state  clock
  角色：chars  add-char  goals  add-goal  control
  决策：decisions  options  resolve  auto-decide
  章节：write  chapters  save-chapter  versions  restore
  完结：completion  goals-world  add-world-goal
  其他：novels  new-novel  context  search  stats  providers  slots  doctor

用法示例：
  python src_v6/cli.py novels
  python src_v6/cli.py world-advance 1 --focus "调查局内部出现隐瞒"
  python src_v6/cli.py decisions 1
  python src_v6/cli.py write 1
"""
import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from core import database as DB

DB_PATH = os.path.join(_ROOT, "sql", "novel.db")

# 章节形态（与 schema.chapters.chapter_shape 的 CHECK 对齐）
CHAPTER_SHAPES = ("scene", "montage", "interlude", "aftermath", "ensemble",
                  "transition")


def _db(args):
    return DB.NovelDatabase(getattr(args, "db", None) or DB_PATH)


def _router(db, novel_id=0):
    from llm.router import LLMRouter
    return LLMRouter(db, novel_id)


def _out(obj, as_json=False):
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    elif isinstance(obj, str):
        print(obj)
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=2))


def _fail(msg, code=1):
    print("错误：%s" % msg, file=sys.stderr)
    return code


# ============================================================ 世界

def cmd_world_init(args):
    db = _db(args)
    if args.file:
        with open(args.file, "r", encoding="utf-8") as f:
            spec = json.load(f)
    else:
        spec = {}
    nv = db.create_novel(
        title=args.title or spec.get("title") or "未命名",
        genre=args.genre or spec.get("genre", ""),
        tone=args.tone or spec.get("tone", ""),
        style=args.style or spec.get("style", ""),
        premise=args.premise or spec.get("premise", ""),
        initial_tension=args.tension or spec.get("initial_tension", ""),
        decision_mode=args.mode or spec.get("decision_mode", "director"),
        completion_mode=spec.get("completion_mode", "ai"),
        target_words_per_chapter=int(spec.get("target_words_per_chapter") or 3000),
    )
    nid = nv["id"]
    if args.time:
        db.set_clock(nid, args.time)
    # ★ 约束 4：初始状态也是"世界真相"，要进正史账本 → 走 facts.seed_state()。
    #   直接 db.set_state_value() 会被 writer 白名单挡下（PermissionError），
    #   建书就半途失败。seed_state 额外把这些数字记成 source_kind='init' 的 fact，
    #   于是第 30 章回头还能查到"这个数字最初是多少"。
    from engine import facts as F
    _seeded = F.seed_state(db, nid, spec.get("world_state") or [])
    print("  初始状态：%d 条进账本，%d 条进世界状态"
          % (_seeded.get("facts", 0), _seeded.get("state", 0)))
    for ent in (spec.get("entities") or []):
        db.add_entity(nid, ent.get("entity_type", "other"), ent["name"],
                      ent.get("description", ""), power_level=ent.get("power_level", 3),
                      visibility=ent.get("visibility", "public"))
    for ch in (spec.get("characters") or []):
        cid = db.add_character(
            nid, ch["name"], rank=ch.get("rank", "C"),
            role_tag=ch.get("role_tag", ""), personality=ch.get("personality", ""),
            background=ch.get("background", ""), speech_style=ch.get("speech_style", ""),
            decision_tendency=ch.get("decision_tendency", ""),
            control_mode=ch.get("control_mode", "ai"),
            current_location=ch.get("location", ""))
        for g in (ch.get("goals") or []):
            if isinstance(g, str):
                db.add_goal(nid, cid, g, origin="init")
            else:
                db.add_goal(nid, cid, g["content"],
                            goal_type=g.get("goal_type", "short"),
                            motivation=g.get("motivation", ""),
                            obstacle=g.get("obstacle", ""),
                            priority=g.get("priority", 3), origin="init")
    for wg in (spec.get("world_goals") or []):
        db.add_world_goal(nid, wg["title"], goal_type=wg.get("goal_type", "world"),
                          description=wg.get("description", ""),
                          condition_expr=wg.get("condition_expr", ""),
                          is_primary=1 if wg.get("is_primary") else 0)
    print("已创建世界 #%s《%s》" % (nid, nv["title"]))
    print("  人机分工：%s ｜ 完结方式：%s" % (nv["decision_mode"], nv["completion_mode"]))
    print("  角色 %d 位 ｜ 实体 %d 个 ｜ 状态 %d 项"
          % (len(db.list_characters(nid)), len(db.list_entities(nid)),
             len(db.get_world_state(nid))))
    db.close()
    return 0


def cmd_world_advance(args):
    db = _db(args)
    from engine.world import WorldEngine, EngineError
    try:
        eng = WorldEngine(db, _router(db, args.novel_id), args.novel_id)
        out = eng.advance(focus=args.focus or "", extra_constraints=args.constraints or "",
                          dry_run=args.dry_run, chapter_ref=args.chapter or 0)
    except EngineError as e:
        db.close()
        return _fail(str(e))
    if out.get("blocked"):
        print("⚠ 推演被拦截：%s" % out["blocked_reason"])
        for i in out["check"]["errors"]:
            print("  · [%s] %s" % (i["kind"], i["message"]))
        db.close()
        return 2
    print("推演完成 ｜ 世界时间：%s" % out.get("world_time") or "")
    for e in out["result"]["events"]:
        print("  · [%s] %s" % (e.get("event_type"), e.get("title")))
        print("      %s" % (e.get("description") or "")[:100])
    if out["check"]["warnings"]:
        print("  校验提醒 %d 条：" % len(out["check"]["warnings"]))
        for w in out["check"]["warnings"][:5]:
            print("    - %s" % w["message"])
    if out["decision_point_ids"]:
        print("  产生决策点：#%s" % ", #".join(str(i) for i in out["decision_point_ids"]))
    if out.get("applied"):
        print("  落库事件 %d 个 ｜ 新增线索 %d 条"
              % (len(out["applied"]["event_ids"]), len(out["applied"]["threads"])))
    db.close()
    return 0


def cmd_world_state(args):
    db = _db(args)
    for s in db.get_world_state(args.novel_id):
        print("  [%s] %s = %s%s" % (s["category"], s["key"], s["value"],
                                    ("  # %s" % s["note"]) if s["note"] else ""))
    db.close()
    return 0


def cmd_clock(args):
    db = _db(args)
    c = db.get_clock(args.novel_id)
    print("世界时间：%s" % (c["current_time"] or "（未设定）"))
    print("已推进：%s 次 ｜ 粒度：%s ｜ 上次产出章：%s"
          % (c["total_ticks"], c["granularity"], c["last_chapter"]))
    db.close()
    return 0


# ============================================================ 角色

def cmd_chars(args):
    db = _db(args)
    for c in db.list_characters(args.novel_id):
        goals = db.list_goals(character_id=c["id"], status="active")
        gt = "；".join("%s:%s" % ("长" if g["goal_type"] == "long" else "短", g["content"])
                       for g in goals) or "无目标"
        print("  #%s [%s] %s ｜ %s ｜ 托管:%s ｜ 位置:%s"
              % (c["id"], c["rank"], c["name"], c["status"],
                 c["control_mode"], c["current_location"] or "-"))
        print("      目标：%s" % gt)
        if c.get("decision_tendency"):
            print("      倾向：%s" % c["decision_tendency"])
    db.close()
    return 0


def cmd_add_char(args):
    db = _db(args)
    cid = db.add_character(
        args.novel_id, args.name, rank=args.rank, role_tag=args.role or "",
        personality=args.personality or "", background=args.background or "",
        speech_style=args.speech or "", decision_tendency=args.tendency or "",
        control_mode=args.control, current_location=args.location or "")
    if args.goal:
        for g in args.goal:
            parts = g.split(":", 1)
            gt = parts[0] if parts[0] in ("short", "long") else "short"
            content = parts[1] if len(parts) > 1 else g
            db.add_goal(args.novel_id, cid, content, goal_type=gt, origin="user_added")
    print("已创建角色 #%s %s（托管：%s）" % (cid, args.name, args.control))
    db.close()
    return 0


def cmd_goals(args):
    db = _db(args)
    rows = db.list_goals(novel_id=args.novel_id, status=args.status or None)
    for g in rows:
        print("  #%s [%s/%s] %s ｜ 优先级%s ｜ 进度%s%% ｜ %s"
              % (g["id"], g["character_name"], g["goal_type"], g["content"],
                 g["priority"], g["progress"], g["status"]))
        if g.get("motivation"):
            print("      动机：%s" % g["motivation"])
    db.close()
    return 0


def cmd_add_goal(args):
    db = _db(args)
    c = db.get_character_by_name(args.novel_id, args.character)
    if not c:
        db.close()
        return _fail("角色不存在：%s" % args.character)
    gid = db.add_goal(args.novel_id, c["id"], args.content, goal_type=args.type,
                      motivation=args.motivation or "", obstacle=args.obstacle or "",
                      priority=args.priority, origin="user_added")
    print("已为目标 #%s 建档" % gid)
    db.close()
    return 0


def cmd_control(args):
    db = _db(args)
    from decide.scheduler import DecisionScheduler
    sc = DecisionScheduler(db, None, args.novel_id)
    if args.character:
        c = db.get_character_by_name(args.novel_id, args.character)
        if not c:
            db.close()
            return _fail("角色不存在：%s" % args.character)
        sc.set_character_control(c["id"], args.mode)
        print("%s 的托管模式已改为 %s" % (args.character, args.mode))
    else:
        n = sc.bulk_set_control(args.mode)
        print("%d 位角色的托管模式已改为 %s" % (n, args.mode))
    db.close()
    return 0


# ============================================================ 决策

def cmd_decisions(args):
    db = _db(args)
    rows = db.list_decisions(args.novel_id)
    if not rows:
        print("（无决策点）")
    for d in rows:
        st = "已决（%s）" % d["chosen_by"] if d["resolved"] else "待决"
        print("  #%s [%s] %s ｜ 主体:%s ｜ %s"
              % (d["id"], d["trigger_type"], d["title"], d["actor_name"], st))
        print("      局面：%s" % d["situation"])
        if d["stakes"]:
            print("      代价：%s" % d["stakes"])
        for i, o in enumerate(d.get("options") or []):
            mark = "✔" if d["resolved"] and d["chosen_option"] == i else " "
            print("      %s %d. %s — %s（%s）"
                  % (mark, i, o.get("label"), o.get("description"),
                     o.get("consequence_hint")))
        if d["resolved"]:
            print("      结果：%s" % d["chosen_content"])
    db.close()
    return 0


def cmd_options(args):
    db = _db(args)
    from decide.scheduler import DecisionScheduler
    r = _router(db, args.novel_id)
    sc = DecisionScheduler(db, r, args.novel_id)
    opts = sc.generate_options(args.decision_id)
    d = db.get_decision(args.decision_id)
    print("【%s】%s" % (d["title"], d["situation"]))
    for i, o in enumerate(opts):
        print("  %d. %s — %s" % (i, o["label"], o["description"]))
        print("      后果倾向：%s ｜ 倾向：%s" % (o["consequence_hint"], o["tendency"]))
    db.close()
    return 0


def cmd_resolve(args):
    db = _db(args)
    from decide.scheduler import DecisionScheduler
    sc = DecisionScheduler(db, _router(db, args.novel_id), args.novel_id)
    try:
        if args.free:
            res = sc.resolve(args.decision_id, option_index=None, freeform=args.free)
        else:
            res = sc.resolve(args.decision_id, option_index=args.index)
    except ValueError as e:
        db.close()
        return _fail(str(e))
    print("决策已落定：%s" % res["chosen_content"])
    db.close()
    return 0


def cmd_auto_decide(args):
    db = _db(args)
    from decide.scheduler import DecisionScheduler
    sc = DecisionScheduler(db, _router(db, args.novel_id), args.novel_id)
    out = sc.process_pending()
    for d in out["needs_user"]:
        print("【需要你决定】#%s %s（%s）" % (d["id"], d["title"], d["trigger_label"]))
        for i, o in enumerate(d["options"]):
            print("   %d. %s — %s" % (i, o["label"], o["consequence_hint"]))
    for d in out["ai_resolved"]:
        print("【AI 已决定】#%s %s -> %s" % (d["id"], d["title"], d["chosen_content"]))
    for f in out["failed"]:
        print("【失败】#%s %s" % (f["id"], f["error"]))
    db.close()
    return 0


# ============================================================ 章节

def cmd_write(args):
    db = _db(args)
    from generate.prose import ProseGenerator
    pg = ProseGenerator(db, _router(db, args.novel_id), args.novel_id)
    try:
        res = pg.write_chapter(chapter_number=args.chapter, words=args.words,
                               shape=args.shape, extra=args.notes or "",
                               summary=args.summary)
    except ValueError as e:
        db.close()
        return _fail(str(e))
    print("已生成第 %s 章《%s》" % (res["chapter"]["chapter_number"],
                                res["chapter"]["title"]))
    print("  字数：%s ｜ 形态：%s ｜ 视角：%s ｜ 收尾：%s ｜ 事件数：%s"
          % (res["word_count"], res["shape"], res["pov"],
             res["ending_mode"], res["event_count"]))
    if res.get("remaining"):
        print("  还剩 %s 个待成章事件，再跑一次 write 就是下一章。"
              % res["remaining"])
    elif res.get("truncated"):
        print("  ⚠ 本章被输出上限截断，结尾可能没收住，建议调低 --words 重写。")
    path = db.export_chapter_file(args.novel_id, res["chapter"]["chapter_number"])
    if path:
        print("  已导出：%s" % path)
    db.close()
    return 0


def cmd_chapters(args):
    db = _db(args)
    for c in db.list_chapters(args.novel_id):
        print("  第%s章 《%s》 ｜ %s 字 ｜ %s ｜ 形态:%s"
              % (c["chapter_number"], c["title"], c["word_count"],
                 c["status"], c["chapter_shape"]))
        if c["summary"]:
            print("      摘要：%s" % c["summary"])
    db.close()
    return 0


def cmd_versions(args):
    db = _db(args)
    for v in db.list_versions(args.novel_id, args.chapter):
        print("  v%s ｜ %s 字 ｜ %s ｜ %s"
              % (v["version"], v["word_count"], v["change_note"], v["created_at"]))
    db.close()
    return 0


def cmd_restore(args):
    db = _db(args)
    res = db.restore_version(args.novel_id, args.chapter, args.version)
    if not res:
        db.close()
        return _fail("版本不存在")
    print("已恢复到 v%s（当前字数 %s）" % (args.version, res["word_count"]))
    db.close()
    return 0


# ============================================================ 完结

def cmd_completion(args):
    db = _db(args)
    from engine.world import WorldEngine
    eng = WorldEngine(db, _router(db, args.novel_id), args.novel_id)
    try:
        res = eng.check_completion()
    except Exception as e:
        db.close()
        return _fail(str(e))
    print("完结判定方式：%s" % res["mode"])
    print("结论：%s" % ("可以完结" if res["should_complete"] else "尚未到终点"))
    print("说明：%s" % res["reason"])
    for h in res["goal_hits"]:
        print("  ✔ 目标达成：#%s %s（%s）" % (h["id"], h["title"], h["condition"]))
    v = res.get("ai_verdict") or {}
    if v and not v.get("error"):
        print("  AI 三问：")
        print("    张力：%s" % v.get("q1_tension"))
        print("    目标：%s" % v.get("q2_goals"))
        print("    重复：%s" % v.get("q3_repetition"))
        print("    置信度：%s" % v.get("confidence"))
    db.close()
    return 0


def cmd_add_world_goal(args):
    db = _db(args)
    cid = None
    if args.character:
        c = db.get_character_by_name(args.novel_id, args.character)
        cid = c["id"] if c else None
    gid = db.add_world_goal(args.novel_id, args.title, goal_type=args.type,
                            description=args.description or "",
                            condition_expr=args.condition or "",
                            character_id=cid,
                            is_primary=1 if args.primary else 0)
    print("已创建完结目标 #%s" % gid)
    if args.condition:
        print("  条件：%s" % args.condition)
    db.close()
    return 0


# ============================================================ 其他

def cmd_novels(args):
    db = _db(args)
    rows = db.list_novels()
    if not rows:
        print("（还没有小说。用 world-init 创建世界）")
    for n in rows:
        print("  #%s《%s》[%s] ｜ 第%s章 ｜ %s字 ｜ 角色%s 实体%s 事件%s 线索%s 待决%s"
              % (n["id"], n["title"], n["status"], n["current_chapter"],
                 n["total_words"], n["character_count"], n["entity_count"],
                 n["event_count"], n["open_thread_count"],
                 n["pending_decision_count"]))
    db.close()
    return 0


def cmd_context(args):
    db = _db(args)
    md = db.context_markdown(args.novel_id, target_chapter=args.chapter)
    print(md)
    db.close()
    return 0


def cmd_search(args):
    db = _db(args)
    rows = db.search(args.novel_id, args.keyword)
    if not rows:
        print("（无结果）")
    for r in rows:
        print("  第%s章《%s》" % (r["chapter_number"], r["title"]))
        if r.get("snippet"):
            print("      ...%s..." % r["snippet"])
    db.close()
    return 0


def cmd_stats(args):
    db = _db(args)
    st = db.stats(args.novel_id)
    if not st:
        db.close()
        return _fail("小说不存在")
    n = st["novel"]
    print("《%s》" % n["title"])
    print("  状态 %s ｜ 章节 %s（已发 %s）｜ 总字数 %s ｜ 平均 %s 字/章"
          % (n["status"], st["chapter_count"], st["published_count"],
             st["total_words"], st["avg_words"]))
    print("  角色 %s（在世 %s）｜ 实体 %s ｜ 事件 %s"
          % (st["character_count"], st["alive_count"], st["entity_count"],
             st["event_count"]))
    print("  线索 %s（急需回收 %s）｜ 待决 %s ｜ 目标 %s ｜ 状态量 %s"
          % (st["active_thread_count"], st["urgent_thread_count"],
             st["pending_decision_count"], st["goal_count"],
             st["world_state_count"]))
    c = st["clock"]
    print("  世界时间：%s（推进 %s 次）" % (c["current_time"] or "未设定", c["total_ticks"]))
    u = st["usage"]["total"]
    print("  AI 调用：%s 次 ｜ token %s ｜ 失败 %s"
          % (u["calls"], u["tokens"], u["failures"]))
    db.close()
    return 0


def cmd_providers(args):
    db = _db(args)
    from llm import client as LLM, secrets as SEC
    from llm.router import SLOT_LABELS
    print("== 已配置的服务商 ==")
    rows = db.list_providers()
    if not rows:
        print("  （无。请用 Web 台或写程序配置）")
    for p in rows:
        masked = ""
        refs = {r["ref"]: r["masked"] for r in SEC.list_key_refs()}
        if p.get("api_key_ref"):
            masked = refs.get(p["api_key_ref"], "（未设置）")
        ms = p.get("models") or []
        print("  #%s %s [%s] %s ｜ key:%s ｜ 模型%s"
              % (p["id"], p["name"], p["kind"], p["base_url"],
                 masked or "-",
                 ("%d 个（%s）" % (len(ms), ", ".join(ms[:3])))
                 if ms else "未登记"))
    print("\n== 可用厂商预设 ==")
    for pr in LLM.list_providers():
        print("  %-14s %-10s %s" % (pr["key"], pr["kind"], pr["base_url"]))
    print("\n== 模型档位 ==")
    from llm.router import LLMRouter
    rt = LLMRouter(db, args.novel_id if hasattr(args, "novel_id") else 0)
    for slot, info in rt.ready_slots().items():
        mark = "✔" if info["ok"] else "✘"
        preset = db.get_model_preset(slot, args.novel_id if hasattr(args, "novel_id") else 0)
        print("  %s %-18s %-8s %s" % (mark, info["label"], slot,
                                      (preset or {}).get("model") or "（未指定）"))
    db.close()
    return 0


def cmd_doctor(args):
    db = _db(args)
    h = db.health()
    print("== 体检 ==")
    print("  库：%s" % h["db"])
    print("  完整性：%s ｜ 外键断裂：%s" % (h["integrity"], h["foreign_keys_broken"]))
    print("  表数：%s ｜ schema 版本：%s" % (h["table_count"], h["version"]))
    print("  总体：%s" % ("正常" if h["ok"] else "异常"))
    db.close()
    return 0


# ============================================================ 解析

def build_parser():
    ap = argparse.ArgumentParser(prog="novel", description="AI 小说创作系统 v6.0")
    ap.add_argument("--db", help="数据库路径", default=None)
    sub = ap.add_subparsers(dest="cmd")

    def add(name, fn, **kw):
        p = sub.add_parser(name, **kw)
        p.set_defaults(func=fn)
        return p

    # 世界
    p = add("world-init", cmd_world_init, help="初始化世界")
    p.add_argument("--title"); p.add_argument("--genre"); p.add_argument("--tone")
    p.add_argument("--style"); p.add_argument("--premise"); p.add_argument("--tension")
    p.add_argument("--time", help="世界起始时间")
    p.add_argument("--mode", choices=["viewer", "director", "tabletop"])
    p.add_argument("--file", help="从 JSON 规格文件初始化")

    p = add("world-advance", cmd_world_advance, help="推进世界")
    p.add_argument("novel_id", type=int)
    p.add_argument("--focus", help="聚焦方向")
    p.add_argument("--constraints", help="额外约束")
    p.add_argument("--chapter", type=int, help="归属章节")
    p.add_argument("--dry-run", action="store_true", help="只推演不落库")

    p = add("world-state", cmd_world_state, help="查看世界状态")
    p.add_argument("novel_id", type=int)

    p = add("clock", cmd_clock, help="查看世界时钟")
    p.add_argument("novel_id", type=int)

    # 角色
    p = add("chars", cmd_chars, help="角色列表")
    p.add_argument("novel_id", type=int)

    p = add("add-char", cmd_add_char, help="新增角色")
    p.add_argument("novel_id", type=int); p.add_argument("name")
    p.add_argument("--rank", default="C", choices=list("ABCDE"))
    p.add_argument("--role"); p.add_argument("--personality"); p.add_argument("--background")
    p.add_argument("--speech"); p.add_argument("--tendency"); p.add_argument("--location")
    p.add_argument("--control", default="ai", choices=["ai", "user", "auto_delegate"])
    p.add_argument("--goal", action="append", help="short:目标 或 long:目标，可多次")

    p = add("goals", cmd_goals, help="目标列表")
    p.add_argument("novel_id", type=int); p.add_argument("--status")

    p = add("add-goal", cmd_add_goal, help="新增角色目标")
    p.add_argument("novel_id", type=int); p.add_argument("character"); p.add_argument("content")
    p.add_argument("--type", default="short", choices=["short", "long"])
    p.add_argument("--motivation"); p.add_argument("--obstacle")
    p.add_argument("--priority", type=int, default=3)

    p = add("control", cmd_control, help="设定托管模式")
    p.add_argument("novel_id", type=int); p.add_argument("mode",
                                                        choices=["ai", "user", "auto_delegate"])
    p.add_argument("--character", help="不指定则批量设定全部")

    # 决策
    p = add("decisions", cmd_decisions, help="决策点列表")
    p.add_argument("novel_id", type=int)

    p = add("options", cmd_options, help="为决策点生成选项")
    p.add_argument("decision_id", type=int)
    p.add_argument("--novel-id", type=int, default=0)

    p = add("resolve", cmd_resolve, help="落定决策")
    p.add_argument("decision_id", type=int); p.add_argument("--index", type=int)
    p.add_argument("--free", help="自由输入内容")
    p.add_argument("--novel-id", type=int, default=0)

    p = add("auto-decide", cmd_auto_decide, help="自动处理待决点")
    p.add_argument("novel_id", type=int)

    # 章节
    p = add("write", cmd_write, help="生成章节")
    p.add_argument("novel_id", type=int); p.add_argument("--chapter", type=int)
    p.add_argument("--words", type=int); p.add_argument("--notes")
    p.add_argument("--summary")
    p.add_argument("--shape", choices=CHAPTER_SHAPES)

    p = add("chapters", cmd_chapters, help="章节列表")
    p.add_argument("novel_id", type=int)

    p = add("versions", cmd_versions, help="章节版本")
    p.add_argument("novel_id", type=int); p.add_argument("chapter", type=int)

    p = add("restore", cmd_restore, help="恢复章节版本")
    p.add_argument("novel_id", type=int); p.add_argument("chapter", type=int)
    p.add_argument("version", type=int)

    # 完结
    p = add("completion", cmd_completion, help="完结判定")
    p.add_argument("novel_id", type=int)

    p = add("add-world-goal", cmd_add_world_goal, help="新增完结目标")
    p.add_argument("novel_id", type=int); p.add_argument("title")
    p.add_argument("--type", default="world", choices=["world", "character", "ai_judged"])
    p.add_argument("--description"); p.add_argument("--condition")
    p.add_argument("--character"); p.add_argument("--primary", action="store_true")

    # 其他
    add("novels", cmd_novels, help="小说列表")
    p = add("context", cmd_context, help="输出创作上下文")
    p.add_argument("novel_id", type=int); p.add_argument("--chapter", type=int)
    p = add("search", cmd_search, help="全文检索")
    p.add_argument("novel_id", type=int); p.add_argument("keyword")
    p = add("stats", cmd_stats, help="统计")
    p.add_argument("novel_id", type=int)
    p = add("providers", cmd_providers, help="AI 配置总览")
    p.add_argument("--novel-id", type=int, default=0)
    add("doctor", cmd_doctor, help="数据库体检")
    return ap


def main():
    ap = build_parser()
    args = ap.parse_args()
    if not getattr(args, "cmd", None):
        ap.print_help()
        return 0
    try:
        return args.func(args)
    except FileNotFoundError as e:
        return _fail(str(e))
    except Exception as e:
        import traceback
        if os.environ.get("NOVEL_DEBUG"):
            traceback.print_exc()
        return _fail("%s: %s" % (type(e).__name__, e))


if __name__ == "__main__":
    sys.exit(main())
