# -*- coding: utf-8 -*-
"""
技能库（Skills Hub）：看远程库 → 装到本地 → 绑到 AI 调用点 → 运行时注入。

为什么单列一个模块：
  Agent Skills 本质就是「一个含 SKILL.md 的文件夹」，装它只需要拷文件。
  难的是后半段——**装完不等于生效**。要让 skill 真的约束模型，必须把它
  的正文拼进那次调用的 system 提示词里，否则页面上看得见、模型看不见。
  所以本模块同时管「目录」和「注入」两件事。

四个字段的约定：
  name        技能名（SKILL.md frontmatter 的 name，缺省用目录名）
  dir         磁盘上的目录名（安装/卸载按这个来，**不按 name**，
              因为 name 可能带中文或跟目录名不一致）
  scope       user=用户级 ~/.workbuddy/skills，project=本项目的 .workbuddy/skills
  slot        AI 调用点档位（见 llm.router.SLOT_LABELS）

绑定为什么存 app_config 而不是新建表：
  绑定只有「档位 → 技能名」这十几条，是配置不是业务数据。新建表要改
  schema_v6.sql 并让用户重跑 migrate.py，而**用户不跑迁移的功能等于没上线**。
  app_config 本来就是 KV 配置表（get_config/set_config 已收口），
  单键存 JSON 即可，零迁移成本。

联网一律走标准库 urllib（本项目零第三方依赖），失败只返回人话错误，
**绝不抛异常打断页面**——浏览失败最多是这一栏空着。
"""
import base64
import binascii
import json
import os
import re
import shutil
import sqlite3
import tempfile
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.dirname(_HERE)
_ROOT = os.path.dirname(_SRC)
# 项目源码根。长这样写是为了**不依赖任何模块级副作用**：
# 就算 _SRC 在某些部署形态下取错，后面也都是靠"文件存不存在"兜底，不会炸。
_SRC_FALLBACKS = (_SRC, os.path.join(_ROOT, "src_v6"))

USER_DIR = os.path.join(os.path.expanduser("~"), ".workbuddy", "skills")
PROJECT_DIR = os.path.join(_ROOT, ".workbuddy", "skills")

# 测试护栏：设了 NW_SKILLS_DIR 就把**写入根**整体挪走。
# 为什么要有这个：测试要试导入/卸载，而它们都是真删真写。曾经因为测试
# 直接改了模块常量、服务端却拿着另一个模块对象，结果把测试技能装进了
# 用户真实的 ~/.workbuddy/skills。给一个环境变量当唯一开关，比让每个
# 测试自己记得打补丁可靠。
_OVERRIDE = os.environ.get("NW_SKILLS_DIR")
if _OVERRIDE:
    USER_DIR = os.path.abspath(_OVERRIDE)

BIND_KEY = "skills.bindings"

# 注入上限。技能正文是白给的提示词，会实打实占 prompt token；
# 有些 SKILL.md 连示例带检查表能写上万字，全塞进去正文生成就没地方了。
MAX_INJECT_CHARS = 8000

# 导入上限。一个技能包就是几个文本文件，给足 8M；
# 超了多半是误传了模型或数据集，不是技能。
MAX_ZIP_BYTES = 8 * 1024 * 1024
MAX_IMPORT_FILES = 200
MAX_IMPORT_FILE_BYTES = 1024 * 1024

_UA = {"User-Agent": "novel-writer-skillhub/1.0"}
_TIMEOUT = 25
_MAX_FILES = 40          # 单个技能最多下多少个文件
_MAX_FILE_BYTES = 400 * 1024


# ============================================================ 来源目录

SOURCES = [
    {"id": "deepseek", "name": "DeepSeek 官方 · deepseek-harness",
     "repo": "deepseek-ai/deepseek-harness", "branch": "master",
     "path": ".agents/skills", "kind": "dir",
     "note": "MIT。约 12 个官方技能。部分是给它自家 TS 仓库写的（VitePress、gh stack 之类），"
             "装之前先看一眼说明，跟本项目对不上就别装。"},
    {"id": "anthropic", "name": "Anthropic 官方 · skills",
     "repo": "anthropics/skills", "branch": "main",
     # 仓库根下面是 spec/template/skills 三类目录，**技能只在 skills/ 里**。
     # 写空串会列出 spec、template，用户点进去全是空条目。
     "path": "skills", "kind": "dir",
     "note": "Agent Skills 这个开放标准的原始仓库，偏通用（文档、表格、演示稿）。"},
    {"id": "humanizer", "name": "Humanizer-zh · 中文去 AI 味",
     "repo": "op7418/Humanizer-zh", "branch": "main",
     "path": "", "kind": "file",
     "files": ["SKILL.md", "README.md", "LICENSE"], "skill": "humanizer-zh",
     "note": "12.5k star、MIT。中文 29 种 AI 写作特征的检测与修法，对口正文去味终检。"},
]


def list_sources():
    """来源清单 + 每个来源是否已装到本地（给前端画徽标用）。"""
    have = {s["dir"] for s in scan_local()}
    out = []
    for s in SOURCES:
        item = dict(s)
        item["installed"] = (s.get("skill") or "") in have if s["kind"] == "file" else False
        out.append(item)
    return out


def _source(sid):
    for s in SOURCES:
        if s["id"] == sid:
            return s
    return None


# ============================================================ SKILL.md 解析

def parse_skill_md(text):
    """拆 frontmatter 与正文。

    只认最外层那对 `---`。description 常见写法是 `description: |` 后接
    缩进多行，这里把后续缩进行并回上一个键，够用就行——**不要为了解析
    YAML 引入第三方依赖**，本模块要能单独 import。
    """
    text = text or ""
    if not text.lstrip().startswith("---"):
        return {}, text.strip()
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() in ("---", "..."):
            end = i
            break
    if end is None:
        return {}, text.strip()
    meta = {}
    key = None
    for ln in lines[1:end]:
        m = re.match(r"^([A-Za-z_][\w-]*):\s*(.*)$", ln)
        if m:
            key = m.group(1)
            val = m.group(2).strip()
            # `description: |` 的 `|` 是 YAML 多行标记，不是内容。
            # 不清掉的话列表里每条说明都以「| 去除文本中…」开头，很脏。
            meta[key] = "" if val in ("|", ">", "|-", ">-", "|+", ">+") else val
        elif key and ln[:1] in (" ", "\t"):
            meta[key] = (meta[key] + " " + ln.strip()).strip()
    return meta, "\n".join(lines[end + 1:]).strip()


# ============================================================ 本地扫描

def scan_local():
    """扫用户级 + 项目级两个目录，返回已装技能列表。"""
    out = []
    for scope, base in (("user", USER_DIR), ("project", PROJECT_DIR)):
        if not os.path.isdir(base):
            continue
        for dname in sorted(os.listdir(base)):
            d = os.path.join(base, dname)
            if not os.path.isdir(d):
                continue
            p = os.path.join(d, "SKILL.md")
            if not os.path.isfile(p):
                continue
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            meta, body = parse_skill_md(text)
            n = 0
            for _r, _d, fs in os.walk(d):
                n += len(fs)
            out.append({
                "name": (meta.get("name") or dname).strip() or dname,
                "dir": dname, "scope": scope, "path": p,
                "description": (meta.get("description") or "")[:400],
                "body_chars": len(body), "files": n,
            })
    return out


def find_skill(name):
    """按技能名找本地技能（用户级优先），找不到返回 None。

    只在两个 skills 目录里找，**不递归扫仓库**：同名 SKILL.md 在别处
    出现（比如备份、别人的示例）不算「已安装」。
    """
    target = (name or "").strip()
    if not target:
        return None
    for s in scan_local():
        if s["name"] == target or s["dir"] == target:
            return s
    return None


def with_refs(skills, root=None):
    """给每个技能补上「项目里哪里引用了它」。列表页要用。"""
    out = []
    for s in skills:
        item = dict(s)
        item["refs"] = scan_references(s["name"], root=root)[:8]
        out.append(item)
    return out


# ============================================================ 联网

def _fetch(url, timeout=_TIMEOUT):
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _gh_contents(repo, path, branch):
    base = "https://api.github.com/repos/%s/contents" % repo
    url = base + ("/" + path.strip("/") if path.strip("/") else "") + "?ref=" + branch
    return json.loads(_fetch(url).decode("utf-8", "replace"))


def browse(source_id):
    """列出某个远程库里有哪些技能。**失败不抛异常**，返回 ok=False + 人话原因。"""
    src = _source(source_id)
    if not src:
        return {"ok": False, "error": "没有这个来源：%s" % source_id, "items": []}
    have = {s["dir"] for s in scan_local()} | {s["name"] for s in scan_local()}
    try:
        if src["kind"] == "file":
            nm = src.get("skill") or src["repo"].split("/")[-1]
            return {"ok": True, "source": src, "items": [{
                "name": nm, "kind": "file",
                "description": src.get("note", ""),
                "installed": nm in have,
            }]}
        data = _gh_contents(src["repo"], src.get("path", ""), src.get("branch", "main"))
        if isinstance(data, dict):
            data = [data]
        items = []
        for it in data:
            if it.get("type") != "dir":
                continue
            items.append({"name": it.get("name") or "", "kind": "dir",
                          "description": "", "installed": it.get("name") in have})
        return {"ok": True, "source": src, "items": items}
    except urllib.error.HTTPError as e:
        # 403 基本都是匿名配额用完了：说明白，别让用户以为是网络坏了
        why = ("GitHub 匿名接口配额用完了（HTTP 403），过一会儿再试"
               if e.code == 403 else "GitHub 返回 HTTP %s" % e.code)
        return {"ok": False, "error": why, "items": []}
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "error": "连不上 GitHub：%s" % e, "items": []}


def _collect_files(repo, branch, path, depth=0, out=None, budget=None):
    """递归收集一个目录下的文件（最多 3 层 / _MAX_FILES 个）。"""
    if out is None:
        out = []
    if depth > 3 or len(out) >= _MAX_FILES:
        return out
    try:
        data = _gh_contents(repo, path, branch)
    except Exception:                                        # noqa: BLE001
        return out
    if isinstance(data, dict):
        data = [data]
    for it in data:
        if len(out) >= _MAX_FILES:
            break
        if it.get("type") == "file":
            if (it.get("size") or 0) <= _MAX_FILE_BYTES:
                out.append((it.get("path") or "", it.get("download_url") or ""))
        elif it.get("type") == "dir":
            _collect_files(repo, branch, it.get("path") or "", depth + 1, out)
    return out


# ============================================================ 安全审计

# BLOCK：命中就**拒绝安装**并回滚。这些是「执行即失控」的模式。
_BLOCK = [
    (r"curl\s+[^\n|]*\|\s*(?:ba)?sh", "curl 管道直接喂给 shell"),
    (r"wget\s+[^\n|]*\|\s*(?:ba)?sh", "wget 管道直接喂给 shell"),
    (r"rm\s+-rf\s+/(?:\s|$)", "rm -rf / 删根目录"),
    (r"rm\s+-rf\s+~", "rm -rf 删家目录"),
    (r":\s*\(\s*\)\s*\{", "fork 炸弹"),
    (r"\bmkfs\b", "格式化磁盘"),
    (r"chmod\s+-R\s+777", "递归放开全部权限"),
    (r"Invoke-Expression", "PowerShell 执行任意字符串"),
    (r"powershell\s+-e", "PowerShell 加密命令"),
    (r"Set-ExecutionPolicy", "改 PowerShell 执行策略"),
    (r"/dev/tcp/", "用 bash 建网络连接"),
]
# WARN：装，但把证据摆出来给用户看。技能里带脚本很常见，一律拒会误伤。
_WARN = [
    (r"os\.system", "Python 执行系统命令"),
    (r"subprocess", "Python 起子进程"),
    (r"\beval\s*\(", "eval 执行字符串"),
    (r"\bexec\s*\(", "exec 执行字符串"),
    (r"base64\s+-d", "base64 解码（常见于隐藏载荷）"),
    (r"\bpip\s+install", "装 Python 包"),
    (r"\bnpm\s+install", "装 npm 包"),
]
_TEXT_EXT = (".md", ".py", ".sh", ".js", ".ts", ".json", ".yaml", ".yml", ".txt", ".ps1")


def audit_dir(root):
    """扫目录下所有文本文件，返回 (block_hits, warn_hits)。"""
    block, warn = [], []
    for r, _d, fs in os.walk(root):
        for fn in fs:
            p = os.path.join(r, fn)
            if not fn.lower().endswith(_TEXT_EXT):
                continue
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    txt = f.read()
            except OSError:
                continue
            rel = os.path.relpath(p, root).replace("\\", "/")
            for pat, why in _BLOCK:
                if re.search(pat, txt, re.I):
                    block.append({"file": rel, "why": why})
            for pat, why in _WARN:
                if re.search(pat, txt, re.I):
                    warn.append({"file": rel, "why": why})
    return block, warn


# ============================================================ 安装 / 卸载

def _safe_dirname(name):
    """目录名白名单。绝不允许 `..`、路径分隔符——卸载要 rmtree，这是闸门。"""
    n = (name or "").strip()
    if not n or n in (".", ".."):
        return ""
    if "/" in n or "\\" in n or ":" in n:
        return ""
    if not re.match(r"^[A-Za-z0-9._-]+$", n):
        return ""
    return n


def _unique_dir(base, prefix):
    """找一个没被占用的目录名。重名就 -2、-3 往后排，**不覆盖已有的**。"""
    n = 1
    while True:
        tail = "" if n == 1 else "-%d" % n
        cand = base + tail
        if not os.path.exists(os.path.join(prefix, cand)):
            return cand
        n += 1
        if n > 99:
            import time
            return "%s-%d" % (base, int(time.time()))


def _now_ms():
    import time
    return time.time()


def _safe_display_name(name):
    """导入技能时的**显示名**：比目录名宽松，允许中文。

    白名单分开是有意的：
      · `_safe_dirname` 管**磁盘目录名**——它后面要拼进路径、要 rmtree，
        必须死守 ASCII 白名单（这是安全闸门）；
      · 这里是用户给自己技能起的名字，中文完全合理，
        只挡路径分隔符和控制字符（它们能改写路径或搞坏界面）。
    """
    n = (name or "").strip()
    if not n or n in (".", ".."):
        return ""
    if any(c in n for c in '/\\:*?"<>|'):
        return ""
    if any(ord(c) < 32 for c in n):
        return ""
    return n[:60]


# ============================================================ 导入用户自己的技能

def plan_folder(folder):
    """把本地一个文件夹编成安装计划（只读，不落任何东西）。

    文件夹名（目录名）就是技能名——**用户想叫什么就叫什么**，
    不像远程安装那样被仓库目录名绑死。重名自动 -2 往后排。
    """
    folder = os.path.abspath(folder or "")
    if not os.path.isdir(folder):
        return {"ok": False, "error": "没有这个文件夹：%s" % folder}
    if not os.path.isfile(os.path.join(folder, "SKILL.md")):
        return {"ok": False,
                "error": "这个文件夹里没有 SKILL.md，不是技能包：%s" % folder}
    files = {}
    for r, _d, fs in os.walk(folder):
        for fn in sorted(fs):
            src = os.path.join(r, fn)
            rel = os.path.relpath(src, folder).replace("\\", "/")
            if rel.startswith(".") or "/." in rel:
                continue
            try:
                if os.path.getsize(src) > MAX_IMPORT_FILE_BYTES:
                    continue
                with open(src, "rb") as f:
                    files[rel] = f.read()
            except OSError:
                continue
            if len(files) >= MAX_IMPORT_FILES:
                break
    if not files:
        return {"ok": False, "error": "文件夹里没有可导入的文件"}
    base = _safe_display_name(os.path.basename(folder)) or "skill"
    dname = _unique_dir(base, USER_DIR)
    # frontmatter 的 name **一律跟着最终目录名走**，不只是重名时。
    # 理由：用户导入时选的文件夹名就是他要的技能名。留着原来的
    # `name: my-skill` 会出现「目录叫我的技能包、列表显示 my-skill」，
    # 用户按界面上看到的名字去卸装/绑定，跟磁盘上那个目录对不上。
    text = files.get("SKILL.md", b"").decode("utf-8", "replace")
    files["SKILL.md"] = _set_frontmatter_name(text, dname).encode("utf-8")
    return {"ok": True, "dir": dname, "files": files,
            # renamed 让前端能明说「原来叫 X，因为重名改成了 Y」。
            # 静默改名最坑：用户回去找自己的技能，按原名字怎么都找不到。
            "renamed": None if dname == base else {"from": base, "to": dname},
            "meta": _plan_meta(files, dname),
            "audit": _plan_audit(files)}


def plan_zip(data_b64, kind="zip"):
    """把一个 zip / 单文件（base64）编成安装计划。

    zip 里可能有两种布局：技能文件就在根，或者外面套一层文件夹。
    两种都认——用户从 GitHub 下下来的压缩包通常是后者。
    """
    try:
        raw = base64.b64decode(data_b64 or "", validate=False)
    except (binascii.Error, ValueError) as e:
        return {"ok": False, "error": "文件内容不是合法 base64：%s" % e}
    if not raw:
        return {"ok": False, "error": "文件是空的"}
    if len(raw) > MAX_ZIP_BYTES:
        return {"ok": False, "error": "文件超过 %d MB，不像技能包"
                                      % (MAX_ZIP_BYTES // 1024 // 1024)}

    if kind == "md":
        text = raw.decode("utf-8", "replace")
        if not text.lstrip().startswith("---"):
            return {"ok": False,
                    "error": "这不是 SKILL.md：文件要以 `---` 开头的 frontmatter 起头。"
                             "（完整技能包是带 SKILL.md 的文件夹或 zip）"}
        meta, _body = parse_skill_md(text)
        nm = _safe_display_name((meta.get("name") or "").strip()) or "imported-skill"
        dname = _unique_dir(nm, USER_DIR)
        files = {"SKILL.md": raw}
        if dname != nm:
            files["SKILL.md"] = _set_frontmatter_name(text, dname).encode("utf-8")
        return {"ok": True, "dir": dname, "files": files,
                "meta": _plan_meta(files, dname),
                "audit": _plan_audit(files)}

    import zipfile
    tmp = tempfile.mkdtemp(prefix="nw-skillzip-")
    try:
        try:
            zf = zipfile.ZipFile(__import__("io").BytesIO(raw))
        except Exception as e:                               # noqa: BLE001
            return {"ok": False, "error": "不是有效的 zip：%s" % e}
        extracted = 0
        for info in zf.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            # ★ 路径穿越闸门：zip 里写 ../../ 就能把文件放到任意位置
            if name.startswith("/") or ".." in name.split("/"):
                return {"ok": False,
                        "error": "压缩包里有非法路径（%s），已拒绝" % name}
            if info.file_size > MAX_IMPORT_FILE_BYTES:
                continue
            if extracted >= MAX_IMPORT_FILES:
                break
            dst = os.path.join(tmp, *name.split("/"))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            with zf.open(info) as src, open(dst, "wb") as out:
                out.write(src.read())
            extracted += 1
        root = _find_skill_root(tmp)
        if not root:
            return {"ok": False,
                    "error": "压缩包里没找到 SKILL.md，不是技能包"}
        return plan_folder(root)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _find_skill_root(tmp):
    """找 SKILL.md 所在的那一层。根目录下有就是根，否则往下两层找。

    ★ `best` 必须在 `if` **之前**初始化：Python 里只要函数内有赋值，
    整个函数内它就是局部变量，写在 if 里会 UnboundLocalError。
    """
    if os.path.isfile(os.path.join(tmp, "SKILL.md")):
        return tmp
    best = None
    for r, ds, fs in os.walk(tmp):
        ds[:] = [d for d in ds if not d.startswith(".")]
        if "SKILL.md" in fs:
            depth = r[len(tmp):].count(os.sep)
            if depth <= 2 and (best is None or depth < best[0]):
                best = (depth, r)
    return best[1] if best else None


def _set_frontmatter_name(text, dname):
    """把 SKILL.md 里的 name 改写成目录名。

    为什么必须改：技能清单里认的是 frontmatter 的 name，而安装出来的目录
    名可能带了 -2 后缀。不改就会出现「目录叫 a-2、界面显示 a」，
    卸载时按目录名删、绑定按 name 找，两边对不上。
    """
    meta, body = parse_skill_md(text)
    if not meta:
        return "---\nname: %s\n---\n\n%s" % (dname, text.strip())
    if (meta.get("name") or "").strip() == dname:
        return text
    out, done = [], False
    for ln in text.splitlines():
        if not done and re.match(r"^name\s*:", ln):
            out.append("name: %s" % dname)
            done = True
        else:
            out.append(ln)
    return "\n".join(out)


def plan_content(name, content):
    """把一份现成的 SKILL.md 文本编成安装计划（AI 草案保存走这条路）。

    名字统一走 `_safe_display_name`——模型写出来的名字可能是
    `MySkill_V2` 这种，用户也可能想改成中文，两种都收。
    """
    nm = _safe_display_name(name) or "ai-skill"
    dname = _unique_dir(nm, USER_DIR)
    content = _set_frontmatter_name(content, dname)
    files = {"SKILL.md": content.encode("utf-8")}
    return {"ok": True, "dir": dname, "files": files,
            "meta": _plan_meta(files, dname),
            "audit": _plan_audit(files)}


def suggest_name(name, scope="user"):
    """重名时给个候选名。前端拿它预填改名框，省得用户自己数到 -3。"""
    base = _safe_display_name(name) or "skill"
    return _unique_dir(base, USER_DIR if scope == "user" else PROJECT_DIR)


def _plan_meta(files, dname):
    text = files.get("SKILL.md", b"").decode("utf-8", "replace")
    meta, body = parse_skill_md(text)
    return {"dir": dname, "name": (meta.get("name") or dname),
            "description": (meta.get("description") or "")[:400],
            "body_chars": len(body), "files": len(files)}


def _plan_audit(files):
    """对计划里的文件做与 install 同一套审计。

    为什么不能等落地再审：这一步的结果要**先给用户看**，
    用户看过 BLOCK 证据后还能自己决定要不要导入（自己的文件自己负责）。
    """
    block, warn = [], []
    for rel, data in files.items():
        if not rel.lower().endswith(_TEXT_EXT):
            continue
        txt = data.decode("utf-8", "replace")
        for pat, why in _BLOCK:
            if re.search(pat, txt, re.I):
                block.append({"file": rel, "why": why})
        for pat, why in _WARN:
            if re.search(pat, txt, re.I):
                warn.append({"file": rel, "why": why})
    return {"block": block, "warn": warn}


def commit_plan(plan, scope="user", force=False):
    """把计划落到磁盘。scope=user（用户级）/ project（项目级）。

    目录名走 `_safe_display_name` 而不是 `_safe_dirname`：这是**用户自己
    导入的技能**，中文名完全合理。真正的闸门在后面——每个相对路径都夹
    `..` 判断和 realpath 前缀校验，出不了目标目录。
    """
    if not plan or not plan.get("ok"):
        return {"ok": False, "error": "没有可用的安装计划"}
    dname = _safe_display_name(plan.get("dir") or "")
    if not dname:
        return {"ok": False, "error": "目录名不合法：%s" % plan.get("dir")}
    base = USER_DIR if scope == "user" else PROJECT_DIR
    dest = os.path.join(base, dname)
    if os.path.exists(dest):
        return {"ok": False,
                "error": "「%s」已经存在了，换个名字再导" % dname}
    files = plan.get("files") or {}
    if not files.get("SKILL.md"):
        return {"ok": False, "error": "缺 SKILL.md，不能导入"}
    # 最后一道：不管前面谁改过目录名（plan 阶段自动 -2、服务端用户改名），
    # 落盘前都以最终 dname 为准重写一次 frontmatter。
    # 这里是唯一知道 dir 会不会变的地方，放这儿才兜得住。
    files = dict(files)
    files["SKILL.md"] = _set_frontmatter_name(
        files["SKILL.md"].decode("utf-8", "replace"), dname).encode("utf-8")
    audit = plan.get("audit") or {}
    if audit.get("block") and not force:
        why = "；".join("%s（%s）" % (h["file"], h["why"])
                       for h in audit["block"][:3])
        return {"ok": False, "need_confirm": True,
                "error": "安全审计发现问题，需要确认：%s" % why,
                "audit": audit}
    try:
        os.makedirs(dest, exist_ok=True)
        for rel, data in files.items():
            rel = rel.replace("\\", "/")
            if ".." in rel.split("/"):
                continue
            out = os.path.realpath(os.path.join(dest, *rel.split("/")))
            if not out.startswith(os.path.realpath(dest)):
                continue
            os.makedirs(os.path.dirname(out), exist_ok=True)
            with open(out, "wb") as f:
                f.write(data)
    except Exception as e:                                   # noqa: BLE001
        shutil.rmtree(dest, ignore_errors=True)
        return {"ok": False, "error": "写入失败：%s（已清理）" % e}
    if not os.path.isfile(os.path.join(dest, "SKILL.md")):
        shutil.rmtree(dest, ignore_errors=True)
        return {"ok": False, "error": "SKILL.md 没写成功（已清理）"}
    return {"ok": True, "dir": dname, "path": dest,
            "files": len(files), "scope": scope, "audit": audit}


# ============================================================ AI 写技能

# 生成出来的技能只允许写这几个文件。**白名单不是洁癖**：模型要是给出
# run.sh、install.py，那就是一个能执行任意命令的技能被一键装进用户机器。
# 要带脚本的技能，让用户自己导文件夹。
GEN_ALLOWED = (".md", ".txt")


def gen_system():
    return (
        "你在为一个 AI 长篇小说创作系统写「技能」（Agent Skill）。"
        "技能就是一份 Markdown，写清某个环节该怎么做。\n\n"
        "硬要求：\n"
        "1. 开头必须是 YAML frontmatter，只有两行：\n"
        "---\n"
        "name: <英文小写短横线连字符，如 prose-deai-taste>\n"
        "description: <一句话说清什么时候用得上，中文>\n"
        "---\n"
        "2. 正文用中文，结构是「什么时候用 / 怎么做 / 检查清单 / 反例」。\n"
        "3. **可执行、可检查**：多写「必须」「不许」和能一眼对照的具体条目，"
        "不要写「要生动」「要有感染力」这类没法验证的空话。\n"
        "4. 只写 Markdown，不要写任何脚本、命令或代码文件。\n"
        "5. 篇幅 400–1200 字，够用就行，不要注水。\n\n"
        "只输出一个 JSON 对象，不要别的内容。"
    )


def gen_schema():
    return {
        "type": "object",
        "properties": {
            "name": {"type": "string",
                     "description": "英文短横线技能名，如 prose-deai-taste"},
            "description": {"type": "string",
                            "description": "一句话说明什么时候该用这个技能"},
            "content": {"type": "string",
                        "description": "完整 SKILL.md 内容，含 frontmatter"},
        },
        "required": ["name", "description", "content"],
    }


def parse_gen_output(parsed):
    """把模型产出收口成一份可直接预览的 SKILL.md。

    **不能直接落盘**：模型会把 name 写成带空格/大写/中文，或者干脆漏掉
    frontmatter。落盘前必须过这里——这正是本项目「模型自由文本不直连
    结构化字段」那条铁律。
    """
    if not isinstance(parsed, dict):
        return None
    name = _safe_dirname((parsed.get("name") or "").strip().lower()
                         .replace(" ", "-").replace("_", "-"))
    desc = (parsed.get("description") or "").strip()
    content = (parsed.get("content") or "").strip()
    if not content:
        return None
    if not name:
        name = "ai-skill"
    meta, body = parse_skill_md(content)
    if not meta:
        # 模型没给 frontmatter：自己补一个，别让用户拿到一个装不上的文件
        content = "---\nname: %s\ndescription: %s\n---\n\n%s" % (
            name, desc or "（未写说明）", content)
        meta, body = parse_skill_md(content)
    if (meta.get("name") or "").strip() != name:
        content = _set_frontmatter_name(content, name)
        meta, body = parse_skill_md(content)
    if not (meta.get("description") or "").strip():
        content = _set_frontmatter_desc(content, desc)
        meta, body = parse_skill_md(content)
    dirname = _unique_dir(name, USER_DIR)
    if dirname != name:
        content = _set_frontmatter_name(content, dirname)
    return {"ok": True, "dir": dirname, "name": dirname,
            "description": (meta.get("description") or "")[:400],
            "content": content, "body_chars": len(body)}


def _set_frontmatter_desc(text, desc):
    """改写 frontmatter 里的 description。

    ⚠ 原来只处理「已经有一行 description:」的情况，模型/用户给的 SKILL.md
    只有 name 时会**静默返回原文**——技能装上去看着正常，列表里却没有说明，
    用户完全不知道发生了什么。现在没有这行就插一行。

    插入位置在 frontmatter 块内部（第一个 `---` 与下一个 `---` 之间），
    优先跟在 name 后面；没有 name 就插在块首。
    """
    if not (desc or "").strip():
        return text
    one = desc.replace("\n", " ").strip()
    lines = text.splitlines()
    # 找 frontmatter 块的边界。没有块就没法安全插——硬插会把正文顶掉，
    # 那种情况交给调用方的 parse_gen_output 去补整块 frontmatter。
    if not lines or lines[0].strip() != "---":
        return text
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return text
    # 块内已有 description：原地替换
    for i in range(1, end):
        if re.match(r"^description\s*:", lines[i]):
            lines[i] = "description: %s" % one
            return "\n".join(lines)
    # 块内没有：跟在 name 后面，没有 name 就插在块首
    at = 1
    for i in range(1, end):
        if re.match(r"^name\s*:", lines[i]):
            at = i + 1
            break
    lines.insert(at, "description: %s" % one)
    return "\n".join(lines)


def gen_user(brief, avoid_names):
    lines = ["【要什么】", brief.strip()]
    if avoid_names:
        lines += ["", "【别重名】这几个技能名已经存在，换一个：",
                  "、".join(avoid_names[:40])]
    lines += ["", "现在按要求输出 JSON。"]
    return "\n".join(lines)


# ============================================================ 项目内引用扫描

# 项目的 AI 提示词写在 Python 源码里，不落库。所以查「哪些地方引用了这个技能」
# 得扫源码 + 文档。**docs 必须算进来**：文档里写明「第⑤步用 xt-webnovel-writing」
# 那也是一种真实使用位置，漏掉它用户会以为这个技能没在用。
_SRC_EXTS = (".py", ".md", ".json", ".js", ".html", ".txt")
_SRC_MAX_BYTES = 2 * 1024 * 1024
_REF_DIRS = ("src_v6", "docs")


def scan_references(token, root=None):
    """查项目里哪些位置出现了 `token`。返回 [{path, line, text}]。

    用途：技能不是被绑定才用得上的——项目里还有**代码写死的技能引用**
    （提示词里点名、文档里写明分工）。光看绑定表会漏掉它们，
    用户会以为这个技能没在用。

    扫 `src_v6/` 和 `docs/` 两块；`root` 传了就只扫那一个目录。
    """
    token = (token or "").strip()
    if not token or len(token) < 2:
        return []
    if root:
        roots = [root]
    else:
        roots = [os.path.join(_ROOT, d) for d in _REF_DIRS]
        roots = [p for p in roots if os.path.isdir(p)]
        if not roots:
            roots = [p for p in _SRC_FALLBACKS if os.path.isdir(p)]
    hits = []
    for base in roots:
        _scan_one(base, token, hits)
        if len(hits) >= 60:
            break
    return hits


def _scan_one(root, token, hits):
    for r, ds, fs in os.walk(root):
        ds[:] = [d for d in ds
                 if d not in ("__pycache__", ".git", "node_modules", ".workbuddy")]
        for fn in fs:
            if not fn.lower().endswith(_SRC_EXTS):
                continue
            p = os.path.join(r, fn)
            try:
                if os.path.getsize(p) > _SRC_MAX_BYTES:
                    continue
                with open(p, encoding="utf-8", errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if token in line:
                            hits.append({
                                "path": os.path.relpath(p, _ROOT).replace("\\", "/"),
                                "line": i,
                                "text": line.strip()[:160],
                            })
                            if len(hits) >= 60:
                                return
            except OSError:
                continue


# ------------------------------------------------------------
# 历史书 / 历史版本的技能引用（导技能时列给用户看）

_REF_SKIP = ("sqlite_sequence", "schema_migrations", "_fts", "fts_")


def db_reference_scanner(db_path, token):
    """直接在库里找哪些行引用了这个技能，返回 [{table, id, novel_id}]。

    为什么直读 sqlite3 而不走 NovelDatabase：这是**只读的考古查询**，
    要遍历所有表的文本列，用 ORM 反而更绕。而且它要能在一个库里
    查「另一套 schema」的历史书（导入技能包时经常要翻老备份）。
    """
    out = []
    token = (token or "").strip()
    if not token or not os.path.isfile(db_path):
        return out
    con = sqlite3.connect("file:%s?mode=ro" % _uri_path(db_path), uri=True)
    try:
        con.text_factory = lambda b: b.decode("utf-8", "replace")
        cur = con.cursor()
        tables = [r[0] for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        for t in tables:
            if any(s in t for s in _REF_SKIP):
                continue
            try:
                info = cur.execute("PRAGMA table_info(%s)" % _qi(t)).fetchall()
            except sqlite3.Error:
                continue
            cols = [c[1] for c in info if (c[2] or "").upper() in
                    ("TEXT", "VARCHAR", "CHAR", "CLOB", "")]
            if not cols:
                continue
            has_id = any(c[1] == "id" for c in info)
            has_nv = any(c[1] == "novel_id" for c in info)
            sel = ",".join(['"%s"' % c for c in cols])
            try:
                rows = cur.execute("SELECT %s FROM %s LIMIT 20000"
                                   % (sel, _qi(t))).fetchall()
            except sqlite3.Error:
                continue
            for row in rows:
                joined = " ".join(x for x in row if isinstance(x, str))
                if token not in joined:
                    continue
                rec = {"table": t}
                if has_id:
                    try:
                        rec["id"] = row[cols.index("id")] if "id" in cols else None
                    except (ValueError, IndexError):
                        pass
                if has_nv and "novel_id" in cols:
                    try:
                        rec["novel_id"] = row[cols.index("novel_id")]
                    except (ValueError, IndexError):
                        pass
                out.append(rec)
                if len(out) >= 100:
                    return out
        return out
    except sqlite3.Error:
        return out
    finally:
        con.close()


def _uri_path(p):
    """Windows 路径要转成 file: URI 能认的形式（反斜杠会破坏 URI）。"""
    p = os.path.abspath(p).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p
    return p.replace("?", "%3f").replace("#", "%23")


def _qi(name):
    return '"%s"' % str(name).replace('"', '""')


def install(source_id, name, as_name=""):
    """从远程库装一个技能到用户级目录。成功返回 {ok,dir,path,files,audit}。

    `as_name` 是本地目录名。默认用来源里的名字；重名时前端会带上
    一个新名字再调一次——**不要在这里自动改名**，装了哪个目录必须
    和界面上显示、以及用户预期的一致。
    """
    src = _source(source_id)
    if not src:
        return {"ok": False, "error": "没有这个来源：%s" % source_id}
    src_name = _safe_dirname(name)
    if not src_name:
        return {"ok": False, "error": "技能名不合法：%s" % name}
    dest_name = _safe_dirname(as_name) if as_name else src_name
    if not dest_name:
        return {"ok": False, "error": "目录名不合法：%s" % as_name}
    dest = os.path.join(USER_DIR, dest_name)
    if os.path.exists(dest):
        return {"ok": False, "error": "本地已经有同名目录 %s，先卸载再装"
                                      % dest_name,
                "name_taken": True}

    repo, branch = src["repo"], src.get("branch", "main")
    try:
        if src["kind"] == "file":
            files = [(fn, "https://raw.githubusercontent.com/%s/%s/%s"
                      % (repo, branch, fn)) for fn in src.get("files", ["SKILL.md"])]
        else:
            # 取文件按**来源目录名**，安装按**目标目录名**——两者可能不同
            sub = "/".join(x for x in (src.get("path", ""), src_name) if x)
            files = _collect_files(repo, branch, sub)
            if not files:
                return {"ok": False, "error": "这个目录下没找到文件：%s" % sub}
    except Exception as e:                                   # noqa: BLE001
        return {"ok": False, "error": "列目录失败：%s" % e}

    os.makedirs(dest, exist_ok=True)
    n = 0
    has_md = False
    try:
        for rel, url in files:
            if not url:
                continue
            raw = _fetch(url, timeout=40)
            rel = rel.replace("\\", "/")
            # 去掉来源侧的前缀目录，只保留技能内部相对路径
            base = (src.get("path", "") + "/" + src_name) if src["kind"] == "dir" else ""
            if base and rel.startswith(base + "/"):
                rel = rel[len(base) + 1:]
            elif src["kind"] == "file":
                rel = os.path.basename(rel)
            out = os.path.join(dest, *rel.split("/"))
            safe = os.path.realpath(out)
            if not safe.startswith(os.path.realpath(dest)):
                continue                                     # 路径穿越，跳过
            os.makedirs(os.path.dirname(safe), exist_ok=True)
            with open(safe, "wb") as f:
                f.write(raw)
            if rel == "SKILL.md":
                has_md = True
            n += 1
    except Exception as e:                                   # noqa: BLE001
        shutil.rmtree(dest, ignore_errors=True)
        return {"ok": False, "error": "下载中断：%s（已清理，没留半个目录）" % e}

    if not has_md and not os.path.isfile(os.path.join(dest, "SKILL.md")):
        shutil.rmtree(dest, ignore_errors=True)
        return {"ok": False, "error": "这个条目里没有 SKILL.md，不是技能（已清理）"}

    # 目标目录名跟来源不同时，把 frontmatter 的 name 一起改掉，
    # 否则界面显示 name、卸载按目录名，两边对不上
    if dest_name != src_name:
        p = os.path.join(dest, "SKILL.md")
        try:
            with open(p, encoding="utf-8", errors="replace") as f:
                txt = f.read()
            with open(p, "w", encoding="utf-8") as f:
                f.write(_set_frontmatter_name(txt, dest_name))
        except OSError:
            pass

    block, warn = audit_dir(dest)
    if block:
        shutil.rmtree(dest, ignore_errors=True)
        why = "；".join("%s（%s）" % (h["file"], h["why"]) for h in block[:3])
        return {"ok": False, "error": "安全审计没过，已拒绝安装并清理：%s" % why,
                "audit": {"block": block, "warn": warn}}
    return {"ok": True, "dir": dest_name, "path": dest, "files": n,
            "audit": {"block": [], "warn": warn}}


def uninstall(name, scope="user", db=None):
    """卸载一个技能。**只删校验过的那个目录**，绝不碰 skills 根。

    名字走 `_safe_display_name`（允许中文，用户导入的技能可能就叫中文名），
    但删之前三道校验一道不少：不许路径分隔符、必须是已存在目录、
    目录里必须有 SKILL.md。
    """
    dname = _safe_display_name(name)
    if not dname:
        return {"ok": False, "error": "技能名不合法：%s" % name}
    base = USER_DIR if scope == "user" else PROJECT_DIR
    dest = os.path.realpath(os.path.join(base, dname))
    # ★ 双保险：realpath 之后必须还在 base 底下。就算 _safe_display_name
    #   哪天被人改松了，这一条也拦得住 `..` 逃逸。
    if os.path.dirname(dest) != os.path.realpath(base):
        return {"ok": False, "error": "这个路径不在技能目录里，不敢删：%s" % dest}
    if not os.path.isdir(dest):
        return {"ok": False, "error": "没找到这个目录：%s" % dest}
    if not os.path.isfile(os.path.join(dest, "SKILL.md")):
        return {"ok": False, "error": "这个目录里没有 SKILL.md，不敢删：%s" % dest}
    shutil.rmtree(dest)
    # 绑定要跟着清：技能没了却还绑着，注入时静默失效——
    # 「页面显示已启用、模型其实没收到」，这正是本项目最怕的那类坑。
    if db is not None:
        try:
            d = load_bindings(db)
            dead = [k for k, v in d.items() if (v or {}).get("skill") in (dname, name)]
            for k in dead:
                d.pop(k, None)
            if dead:
                db.set_config(BIND_KEY, json.dumps(d, ensure_ascii=False))
        except Exception:                                    # noqa: BLE001
            pass
    return {"ok": True, "dir": dname}


# ============================================================ 绑定

def slot_catalog():
    """AI 调用点清单（档位 + 用途说明）。延迟导入 router，避免循环依赖。"""
    from llm.router import SLOT_LABELS
    hints = {
        "world_sim": "推演世界时间线、长出事件",
        "character_decide": "角色在岔路口做决定",
        "option_gen": "给岔路口生成 3-5 个选项",
        "prose_gen": "把事件流写成章节正文",
        "state_extract": "从正文抽取世界状态变化",
        "completion_judge": "判定作品是否可以完结",
        "summarize": "生成摘要与提要",
        "diagnose": "章节五层逻辑诊断",
        "conflict_check": "正史冲突判定与事实分类",
        "world_init": "按题材一键生成世界草案",
    }
    return [{"slot": s, "label": SLOT_LABELS.get(s, s),
             "hint": hints.get(s, "")} for s in SLOT_LABELS]


def load_bindings(db):
    if db is None:
        return {}
    raw = ""
    try:
        raw = db.get_config(BIND_KEY, "")
    except Exception:                                        # noqa: BLE001
        return {}
    if not raw:
        return {}
    try:
        d = json.loads(raw)
    except Exception:                                        # noqa: BLE001
        return {}
    return d if isinstance(d, dict) else {}


def save_binding(db, slot, skill, enabled):
    """写一条绑定。enabled=False 或不选技能 = 删掉这条（不留空壳）。"""
    from llm.router import SLOT_LABELS
    if slot not in SLOT_LABELS:
        raise ValueError("没有这个调用点：%s" % slot)
    d = load_bindings(db)
    if not enabled or not skill:
        d.pop(slot, None)
    else:
        d[slot] = {"skill": skill, "enabled": True}
    db.set_config(BIND_KEY, json.dumps(d, ensure_ascii=False))
    return d


# ============================================================ 运行时注入

_BODY_CACHE = {}


def read_body(path):
    """读 SKILL.md 正文（去掉 frontmatter），按 mtime 缓存。"""
    try:
        mt = os.path.getmtime(path)
    except OSError:
        return ""
    hit = _BODY_CACHE.get(path)
    if hit and hit[0] == mt:
        return hit[1]
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return ""
    _meta, body = parse_skill_md(text)
    _BODY_CACHE[path] = (mt, body)
    return body


def apply_to_system(slot, system, db=None):
    """把该调用点绑定的技能正文拼进 system 提示词。

    放最前面：技能里写的是「你必须怎么做」的硬约束，压在任务描述之前，
    模型才不会把它当成可忽略的参考。注入失败一律静默返回原文——
    **技能是增强，不是依赖**，技能没了模型该怎么跑还怎么跑。
    """
    if db is None:
        return system
    try:
        b = (load_bindings(db) or {}).get(slot) or {}
    except Exception:                                        # noqa: BLE001
        return system
    if not b.get("enabled"):
        return system
    sk = find_skill(b.get("skill") or "")
    if not sk:
        return system
    body = read_body(sk["path"])
    if not body:
        return system
    if len(body) > MAX_INJECT_CHARS:
        body = body[:MAX_INJECT_CHARS] + "\n…（技能正文过长，已截断到 %d 字）" % MAX_INJECT_CHARS
    head = "【附加技能规则 · %s】\n%s" % (sk["name"], body)
    return (head + "\n\n---\n\n" + (system or "")).strip()
