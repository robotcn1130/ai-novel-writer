# -*- coding: utf-8 -*-
"""回归：dispatch 改动后，全站只读接口是否都还正常。

这次改的是**所有路由的必经处**（dispatch 现在认 (status, payload) 元组），
所以必须把主要 GET 接口全打一遍，确认没有一个被改坏。

只跑 GET（只读），不碰会写库的接口。
"""
import json
import os
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src_v6"))
sys.path.insert(0, os.path.join(ROOT, "src_v6", "core"))

from webui import server as S

PORT = 8798
OK, FAIL = [], []


def check(name, cond, extra=""):
    (OK if cond else FAIL).append(name)
    if not cond:
        print("  FAIL:", name, extra)


def get(path):
    url = "http://127.0.0.1:%d%s" % (PORT, urllib.parse.quote(path, safe="/?=&"))
    try:
        with urllib.request.urlopen(url, timeout=25) as r:
            raw = r.read().decode("utf-8")
            return r.status, raw
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:                                   # noqa: BLE001
        return -1, str(e)


# 只读接口清单。全都是 GET，改了 dispatch 之后应当行为不变。
ROUTES = [
    "/api/meta",
    "/api/health",
    "/api/novels",
    "/api/schema",
    "/api/skills/local",
    "/api/skills/sources",
    "/api/skills/refs",
    "/api/skills/bindings",
    "/api/providers",
    "/api/slots",
]

# 需要书的接口。**参数名是 novel_id，不是 id** —— `_novel_id()` 只认
# novel_id，传 id 会静默取到 0，再回一个误导性的「小说不存在」。
PARAM_ROUTES = [
    "/api/novel?novel_id={nid}",
    "/api/stats?novel_id={nid}",
    "/api/clock?novel_id={nid}",
    "/api/state?novel_id={nid}",
    "/api/entities?novel_id={nid}",
    "/api/characters?novel_id={nid}",
    "/api/relations?novel_id={nid}",
    "/api/goals?novel_id={nid}",
    "/api/threads?novel_id={nid}",
    "/api/events?novel_id={nid}",
    "/api/tree?novel_id={nid}",
    "/api/chapters?novel_id={nid}",
    "/api/canon/summary?novel_id={nid}",
]


def main():
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), S.Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        run()
    finally:
        httpd.shutdown()
        httpd.server_close()
    print("\n=== OK=%d FAIL=%d ===" % (len(OK), len(FAIL)))
    for f in FAIL:
        print(" FAIL:", f)
    return 1 if FAIL else 0


def run():
    for p in ROUTES:
        st, raw = get(p)
        check("%s → 200" % p, st == 200, (st, raw[:160]))
        try:
            d = json.loads(raw)
            check("%s 回 JSON 对象" % p, isinstance(d, dict), type(d).__name__)
            # 元组泄漏的话，raw 会像 [409, {...}] 或干脆是空的
            check("%s 不是元组/数组" % p, not isinstance(d, list), raw[:120])
        except Exception as e:                               # noqa: BLE001
            check("%s JSON 可解析" % p, False, str(e) + " | " + raw[:120])

    # 取一本书来跑带参接口
    st, raw = get("/api/novels")
    nid = None
    if st == 200:
        try:
            ns = json.loads(raw).get("novels") or []
            # 挑一本非归档的：归档书 get_novel 取不到是**既定契约**，
            # 拿归档书跑会得到假失败（本项目踩过这个坑，见 MEMORY）。
            live = [x for x in ns if (x.get("status") or "") != "archived"]
            if live:
                nid = live[0].get("id")
        except Exception:                                    # noqa: BLE001
            pass
    if not nid:
        print("  （库里没有小说，跳过带参接口）")
    else:
        for tpl in PARAM_ROUTES:
            p = tpl.format(nid=nid)
            st, raw = get(p)
            check("%s → 200" % p, st == 200, (st, raw[:160]))
            try:
                d = json.loads(raw)
                check("%s 不是元组/数组" % p, not isinstance(d, list), raw[:120])
            except Exception as e:                           # noqa: BLE001
                check("%s JSON 可解析" % p, False, str(e) + " | " + raw[:120])

    # 404 / 405 仍走 ApiError 分支
    st, raw = get("/api/没有这个接口")
    check("未知接口 → 404", st == 404, st)
    check("404 body 是 JSON", '"error"' in raw, raw[:120])

    # 静态资源仍能出
    st, raw = get("/")
    check("首页 → 200", st == 200, st)
    check("首页含 <html", "<html" in raw.lower(), raw[:80])
    st, raw = get("/app.js")
    check("app.js → 200", st == 200, st)
    check("app.js 内容像 JS", "function" in raw, raw[:80])


if __name__ == "__main__":
    sys.exit(main())
