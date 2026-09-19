# -*- coding: utf-8 -*-
"""
LLM 接入层 · 统一协议 v6.0

结论依据（2026-08 调研）：
  OpenAI Chat Completions 是事实标准（95%+ 厂商兼容）。
  原生兼容：DeepSeek / 通义千问 / Kimi / 智谱 GLM / MiniMax / xAI / SiliconFlow
  需要适配器：Anthropic(Messages) / Gemini / Ollama / LM Studio / llama.cpp

四类真实差异（本模块逐一处理）：
  1. 思维链不在 content，而在 choices[0].delta.reasoning_content
  2. 流式 usage 需要显式开 stream_options={"include_usage": true}
  3. Kimi 的 finish_reason 枚举可能越界（出现非标准值）
  4. json_schema 支持度不一 => 降级链 json_schema -> json_object -> 提示词+正则

统一事件流（所有 provider 都归一化成这 5 种）：
  {"type":"start"}
  {"type":"reasoning","text":...}
  {"type":"delta","text":...}
  {"type":"usage","prompt_tokens":..,"completion_tokens":..,"total_tokens":..}
  {"type":"end","finish_reason":...}
"""
import json
import re
import threading
import time
import urllib.error
import urllib.request

# ============================================================ 实时进度播报
#
# 后台任务线程里挂一个 sink，LLM 层每跟模型交换一次数据就播报一次
# （发出请求 / 收到响应 / 用时多少 / 失败原因）。前端据此显示"还在干活"，
# 不必盯着一个不动的大进度条猜是否死机。
#
# 用线程局部变量：每个后台任务各播各的，互不串台。
# 播报本身失败绝不影响主流程（它只是给人看的）。

_tls = threading.local()


def set_progress_sink(fn):
    """挂进度回调 fn(msg, progress=None)；传 None 等于摘掉。"""
    _tls.sink = fn


def clear_progress_sink():
    _tls.sink = None


def note(msg, progress=None):
    """播报一条进度；没挂 sink 就静默跳过。"""
    fn = getattr(_tls, "sink", None)
    if fn is None:
        return
    try:
        fn(msg, progress)
    except Exception:                    # noqa: BLE001
        pass


def _brief_url(url):
    """只留主机名，别把完整接口地址（可能带 key）写进日志。"""
    try:
        host = url.split("://", 1)[-1].split("/", 1)[0]
        return host or url[:60]
    except Exception:                    # noqa: BLE001
        return ""


# ============================================================ 厂商预设

PROVIDERS = {
    "deepseek": {
        "name": "DeepSeek", "kind": "openai",
        "base_url": "https://api.deepseek.com",
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "reasoning_field": "reasoning_content",
    },
    "qwen": {
        "name": "通义千问", "kind": "openai",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen-max", "qwen-plus", "qwen-turbo"],
        "reasoning_field": "reasoning_content",
    },
    "kimi": {
        "name": "Kimi", "kind": "openai",
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
        "reasoning_field": None,
    },
    "zhipu": {
        "name": "智谱 GLM", "kind": "openai",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-plus", "glm-4", "glm-4-flash"],
        "reasoning_field": "reasoning_content",
    },
    "minimax": {
        "name": "MiniMax", "kind": "openai",
        "base_url": "https://api.minimaxi.com/v1",
        "models": ["abab6.5s-chat", "MiniMax-Text-01"],
        "reasoning_field": None,
    },
    "siliconflow": {
        "name": "SiliconFlow", "kind": "openai",
        "base_url": "https://api.siliconflow.cn/v1",
        "models": ["deepseek-ai/DeepSeek-V3", "Qwen/Qwen2.5-72B-Instruct"],
        "reasoning_field": "reasoning_content",
    },
    "openai": {
        "name": "OpenAI", "kind": "openai",
        "base_url": "https://api.openai.com/v1",
        "models": ["gpt-4o", "gpt-4o-mini"],
        "reasoning_field": None,
    },
    "xai": {
        "name": "xAI", "kind": "openai",
        "base_url": "https://api.x.ai/v1",
        "models": ["grok-2-latest", "grok-beta"],
        "reasoning_field": None,
    },
    "anthropic": {
        "name": "Anthropic Claude", "kind": "anthropic",
        "base_url": "https://api.anthropic.com/v1",
        "models": ["claude-sonnet-4-20250514", "claude-opus-4-20250514"],
        "reasoning_field": None,
    },
    "gemini": {
        "name": "Google Gemini", "kind": "gemini",
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "models": ["gemini-2.5-pro", "gemini-2.5-flash"],
        "reasoning_field": None,
    },
    "ollama": {
        "name": "Ollama（本地）", "kind": "ollama",
        "base_url": "http://localhost:11434",
        "models": ["qwen2.5:14b", "llama3.1:8b"],
        "reasoning_field": None,
    },
    "lmstudio": {
        "name": "LM Studio（本地）", "kind": "openai",
        "base_url": "http://localhost:1234/v1",
        "models": ["local-model"],
        "reasoning_field": None,
    },
    "custom": {
        "name": "自定义（OpenAI 兼容）", "kind": "openai",
        "base_url": "",
        "models": [],
        "reasoning_field": "reasoning_content",
    },
}

# 标准 finish_reason 白名单（Kimi 等可能越界，统一归并）
_STD_FINISH = {"stop", "length", "tool_calls", "content_filter", "function_call"}

# 输出被截断时预算翻倍的上限（推理模型的思维链会吃掉 max_tokens）
_MAX_TOKENS_CAP = 32768


# ============================================================ 异常

class LLMError(Exception):
    def __init__(self, message, status=None, provider="", raw=""):
        super().__init__(message)
        self.status = status
        self.provider = provider
        self.raw = raw


class LLMConfigError(LLMError):
    """配置错误：缺 key / 缺 base_url / 模型不存在。"""


# ============================================================ 消息构造

def build_messages(system="", user="", history=None, images=None):
    """构造 OpenAI 风格消息数组。history 为 [{"role","content"}]。"""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    if history:
        for h in history:
            role = h.get("role", "user")
            if role in ("user", "assistant", "system"):
                msgs.append({"role": role, "content": h.get("content", "")})
    if user:
        msgs.append({"role": "user", "content": user})
    return msgs


# ============================================================ HTTP 基础

def _payload_chars(messages):
    """payload 里的**正文字数**（不含 JSON 转义开销）。

    之前这里图省事报了 len(data)（UTF-8 字节数），日志写成"输入约 58338 字"，
    于是排查时一路往"材料太多"的方向找——其实真正的中文字只有一万出头。
    一个中文字 3 字节，`json.dumps` 默认还会把非 ASCII 转成 \\uXXXX（6 字符），
    两者叠加能把数字放大五倍。报错数字要是把排查带偏，比不报还糟。
    """
    total = 0
    for m in messages or []:
        c = m.get("content")
        if isinstance(c, str):
            total += len(c)
    return total


def _post_json(url, payload, headers, timeout=180):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    model = payload.get("model") or "?"
    note("→ 已发出请求：模型 %s（%s，输入约 %d 字，超时 %ds），等待模型响应…"
         % (model, _brief_url(url), _payload_chars(payload.get("messages")), timeout))
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            note("← 模型已返回：%s，收到 %d 字，用时 %.1fs"
                 % (resp.status, len(body), time.time() - t0))
            return resp.status, body
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        note("← 模型拒绝：HTTP %s（用时 %.1fs）" % (e.code, time.time() - t0))
        raise LLMError("HTTP %s: %s" % (e.code, body[:400]), status=e.code, raw=body)
    except urllib.error.URLError as e:
        note("← 网络错误：%s（用时 %.1fs）" % (e.reason, time.time() - t0))
        raise LLMError("网络错误：%s" % (e.reason,))
    except TimeoutError:
        note("← 请求超时：等了 %ss 还没回应（用时 %.1fs）" % (timeout, time.time() - t0))
        raise LLMError("请求超时（%ss）" % timeout)


def _post_stream(url, payload, headers, timeout=300):
    """流式 POST，逐行 yield 原始 SSE 文本行。"""
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "text/event-stream")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    model = payload.get("model") or "?"
    note("→ 已发出流式请求：模型 %s（%s，输入约 %d 字，超时 %ds），等待模型开口…"
         % (model, _brief_url(url), _payload_chars(payload.get("messages")), timeout))
    t0 = time.time()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        note("← 模型拒绝：HTTP %s（用时 %.1fs）" % (e.code, time.time() - t0))
        raise LLMError("HTTP %s: %s" % (e.code, body[:400]), status=e.code, raw=body)
    except urllib.error.URLError as e:
        note("← 网络错误：%s（用时 %.1fs）" % (e.reason, time.time() - t0))
        raise LLMError("网络错误：%s" % (e.reason,))

    note("← 已连上模型，开始接收输出…（用时 %.1fs）" % (time.time() - t0))

    def _gen():
        got = 0
        last = time.time()
        try:
            for raw in resp:
                got += len(raw)
                now = time.time()
                if now - last >= 1.5:     # 边收边报，别刷屏
                    last = now
                    note("… 正在接收模型输出（已 %d 字，用时 %.1fs）"
                         % (got, now - t0))
                yield raw.decode("utf-8", "replace")
        finally:
            note("← 流式输出结束：共 %d 字，用时 %.1fs" % (got, time.time() - t0))
            resp.close()
    return _gen()


# ============================================================ JSON 提取

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


def extract_json(text, with_meta=False):
    """从模型输出里稳健地抠出 JSON。

    策略：按"候选范围从大到小"依次尝试，且**每个候选先原样解析、
    再尝试修复尾随逗号**，避免出现"内层数组抢先命中"的误判。

    候选顺序：
      1. 整段
      2. 代码围栏内容
      3. 最早开始的 { ... } （优先，因为结构化输出通常是对象）
      4. **截断补全**：第一个开括号配不上时，丢掉最后一个没写完的元素再闭合
      5. 其余配平的 { ... } / [ ... ]（对象优先，长的优先）

    第 4 条是给推理模型准备的：思维链吃掉 max_tokens 预算时，正文会被
    拦腰截断（finish_reason=length）。能救回"已经写完的那几个元素"，
    比整体失败强得多——世界推演一次产出 3~6 个事件，丢最后一个远好过重跑。

    `with_meta=True` 时返回 (parsed, salvaged)：
      salvaged=True 表示这份结果**来自截断补全**（第 4 条），即原文本
      是坏的、我们只抢救出了前面几个元素。**调用方必须据此校验字段完整性**——
      否则会拿到"看着 JSON 合法、其实只有一个 title"的半条数据
      （真实事故：网关截断 + 抢救成功 = 半条事件流，下游还以为一切正常）。
    """
    if not text:
        return (None, False) if with_meta else None
    s = text.strip()
    for cand, salvaged in _candidates_with_meta(s):
        got = _try_load(cand)
        if got is not _MISS:
            return (got, salvaged) if with_meta else got
    return (None, False) if with_meta else None


_MISS = object()


def _try_load(cand):
    """对一个候选串尝试两种解析：原样、修复尾随逗号。"""
    for attempt in (cand, re.sub(r",(\s*[}\]])", r"\1", cand)):
        try:
            return json.loads(attempt)
        except (ValueError, TypeError):
            continue
    return _MISS


def _candidates(s):
    """产出 JSON 候选串（只有串，兼容旧调用）。"""
    return [c for c, _ in _candidates_with_meta(s)]


def _candidates_with_meta(s):
    """产出 (候选串, 是否来自截断补全) 列表，优先级由高到低。

    关键：对象候选必须排在数组候选之前，否则
    '{"events":[{...}],}' 会因为外层尾逗号失败、而内层数组恰好合法，
    导致返回内层数组而不是整个对象。
    """
    out = [(s, False)]

    m = _FENCE_RE.search(s)
    if m:
        inner = m.group(1).strip()
        if inner and inner != s:
            out.append((inner, False))

    # 收集所有"配平"的括号跨度（限量扫描，避免超长文本上 O(n^2)）
    spans = {"{": [], "[": []}
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        i = s.find(open_ch)
        guard = 0
        while i != -1 and guard < 80:
            guard += 1
            j = _match_close(s, i, open_ch, close_ch)
            if j != -1:
                spans[open_ch].append((i, j))
            i = s.find(open_ch, i + 1)

    # 最外层容器 = 文本里最早出现的那个括号。
    # 配平就直接用它；配不上说明被截断，先试着把"已写完的部分"补全。
    # （必须先处理最外层：若先去捡里面那些配平的小对象，截断时就会
    #   返回一个内层片段，看着成功、其实丢了整份数据。）
    outer = None
    for open_ch in ("{", "["):
        i = s.find(open_ch)
        if i != -1 and (outer is None or i < outer[1]):
            outer = (open_ch, i)
    used = None
    if outer is not None:
        open_ch, i = outer
        j = next((j for a, j in spans[open_ch] if a == i), -1)
        if j != -1:
            out.append((s[i:j + 1], False))
            used = (open_ch, i)
        else:
            salv = _salvage_text(s[i:])
            if salv:
                # ★ 这一条是"抢救"来的：原文坏掉，只保住了前面的元素
                out.append((salv, True))

    # 其余配平跨度：对象组优先，组内长的优先
    # （正文 JSON 通常比提示词里的示例片段更长）
    for open_ch in ("{", "["):
        group = [s[a:j + 1] for a, j in spans[open_ch] if (open_ch, a) != used]
        group.sort(key=len, reverse=True)
        out.extend((g, False) for g in group)
    return out


def _salvage_text(t):
    """把被截断的 JSON 补成一个可解析的串。

    关键是想清楚"截到哪儿"：模型被 max_tokens 掐断时，断点总在**最后一个
    数组元素内部**，所以不能简单地"最后一个逗号处切断"——那会留下一个
    只有半个字段的元素（例如只有 title、没有 description），下游照样报错。

    做法：记住"最后一个完整数组元素的收尾位置"，从那里断开再补闭括号。
    没有这样的位置时才退化成在逗号处切断。救不回来返回 None。
    """
    if not t:
        return None
    stack = []
    cuts = []
    boundary = -1
    boundary_stack = ()
    in_str = False
    esc = False
    for k, ch in enumerate(t):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            if stack:
                stack.pop()
                # 刚闭合的是一个数组元素 => 这里是可以安全断开的地方
                if stack and stack[-1] == "[":
                    boundary, boundary_stack = k, tuple(stack)
                elif not stack:
                    return None          # 已经配平，不需要补
        elif ch == "," and stack:
            cuts.append((k, tuple(stack)))

    def _close(body, st):
        return body + "".join("}" if c == "{" else "]" for c in reversed(st))

    if boundary != -1:
        cand = _close(t[:boundary + 1], boundary_stack)
        if _try_load(cand) is not _MISS:
            return cand
    for k, st in reversed(cuts[-500:]):
        cand = _close(t[:k], st)
        if _try_load(cand) is not _MISS:
            return cand
    return None


def _match_close(s, start, open_ch, close_ch):
    """从 start 处的开括号出发，忽略字符串内的括号，找到配对闭括号位置。"""
    depth = 0
    in_str = False
    esc = False
    for k in range(start, len(s)):
        ch = s[k]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return k
    return -1


def normalize_finish(reason):
    """Kimi 等厂商可能返回非标准 finish_reason，统一归并。"""
    if not reason:
        return "stop"
    r = str(reason).lower()
    if r in _STD_FINISH:
        return r
    if "length" in r or "max" in r:
        return "length"
    if "tool" in r or "function" in r:
        return "tool_calls"
    if "filter" in r or "safety" in r:
        return "content_filter"
    return "stop"


# 各家控制台经常直接给"完整接口地址"，用户粘进来就成了双重路径。
# 这里统一剥掉已知的端点后缀，只留 base。
_ENDPOINT_SUFFIXES = (
    "/chat/completions", "/completions", "/messages", "/api/chat", "/api/generate",
    "/generatecontent", "/streamgeneratecontent",
)
# Gemini 的路径形如 .../v1beta/models/<model>:generateContent
_GEMINI_PATH_RE = re.compile(
    r"/models/[^/]+:(generate|streamgenerate)content$", re.I)


def normalize_base_url(url, *extra_suffixes):
    """把"完整接口地址"还原成 base URL。

    用户从控制台复制地址时，很常见的是拿到
        https://host/v1/chat/completions
    而适配器内部还会再接一次 /chat/completions，于是打成
        https://host/v1/chat/completions/chat/completions  -> 404

    这个函数把已知的端点后缀剥掉，避免双重路径。
    """
    u = (url or "").strip().rstrip("/")
    if not u:
        return ""
    # Gemini 整段路径（含模型名）直接截到 /v1beta
    m = _GEMINI_PATH_RE.search(u)
    if m:
        return u[: m.start()]
    low = u.lower()
    for suf in tuple(_ENDPOINT_SUFFIXES) + tuple(extra_suffixes):
        if low.endswith(suf.lower()):
            u = u[: len(u) - len(suf)].rstrip("/")
            low = u.lower()
            break
    return u


# ============================================================ 适配器基类

class BaseAdapter:
    kind = "openai"

    def __init__(self, base_url="", api_key="", model="", provider_key="custom"):
        self.base_url = normalize_base_url(base_url)
        self.api_key = api_key or ""
        self.model = model
        self.provider_key = provider_key
        preset = PROVIDERS.get(provider_key, {})
        if not self.base_url:
            self.base_url = preset.get("base_url", "")
        self.reasoning_field = preset.get("reasoning_field") or "reasoning_content"

    # ----- 子类实现
    def _endpoint(self):
        raise NotImplementedError

    def _headers(self):
        raise NotImplementedError

    def _payload(self, messages, temperature, max_tokens, stream, json_mode):
        raise NotImplementedError

    def _parse_delta(self, chunk):
        """返回 (reasoning_text, content_text, finish_reason)"""
        raise NotImplementedError

    def _parse_full(self, data):
        raise NotImplementedError

    # ----- 通用能力

    def chat(self, messages, temperature=0.8, max_tokens=4096, timeout=180,
             no_think=False):
        payload = self._payload(messages, temperature, max_tokens, False, False)
        if no_think:
            _apply_no_think(payload)
        status, body = _post_json(self._endpoint(), payload, self._headers(), timeout)
        try:
            data = json.loads(body)
        except ValueError:
            raise LLMError("响应不是合法 JSON：%s" % body[:300], raw=body,
                           provider=self.provider_key)
        return self._parse_full(data)

    def stream(self, messages, temperature=0.8, max_tokens=4096, timeout=300):
        """产出统一事件流。"""
        payload = self._payload(messages, temperature, max_tokens, True, False)
        yield {"type": "start", "model": self.model, "provider": self.provider_key}
        usage = None
        finish = "stop"
        for line in _post_stream(self._endpoint(), payload, self._headers(), timeout):
            line = line.strip()
            if not line or line.startswith(":"):
                continue
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if chunk == "[DONE]":
                break
            try:
                data = json.loads(chunk)
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("usage"):
                u = data["usage"]
                usage = {
                    "prompt_tokens": u.get("prompt_tokens", 0) or 0,
                    "completion_tokens": u.get("completion_tokens", 0) or 0,
                    "total_tokens": u.get("total_tokens", 0) or 0,
                }
            r, c, f = self._parse_delta(data)
            if f:
                finish = normalize_finish(f)
            if r:
                yield {"type": "reasoning", "text": r}
            if c:
                yield {"type": "delta", "text": c}
        if usage:
            yield {"type": "usage", **usage}
        yield {"type": "end", "finish_reason": finish}

    def chat_json(self, messages, schema=None, temperature=0.2, max_tokens=4096,
                  timeout=180, retries=2, cap=None):
        """带降级链的结构化输出。

        降级顺序：json_schema（若给 schema）-> json_object -> 提示词约束
        返回 (parsed_obj, raw_text, mode_used)

        `cap`：输出预算的**硬上限**。默认 None = 沿用老行为（被截断就一路翻倍
        到 _MAX_TOKENS_CAP）。调"结论型"任务时应当显式给 cap——诊断的输出是
        最多 8 条结构化问题，翻倍只会让模型把多出来的预算拿去复述材料、
        写检查过程，越写越长反而永远填不满，白烧时间和 token。

        两个现实约束（都真踩过）：
          · json_object 模式下 messages 里**必须出现 "json" 字样**，
            否则部分网关直接 400（腾讯 tokenhub 报 400001）。所以走该模式前
            统一注入格式说明 —— 我们的提示词全是中文，天生不含这个词。
          · 推理模型（deepseek-flash / deepseek-reasoner 等）的思维链会吃掉
            max_tokens 预算，表现为 finish_reason=length、正文被截断甚至为空。
            这时加倍预算重试，别拿半个 JSON 当成功。
        """
        attempts = [("json_schema", schema), ("json_object", schema),
                    ("prompt_only", schema)]

        ceiling = min(int(cap), _MAX_TOKENS_CAP) if cap else _MAX_TOKENS_CAP

        last_raw = ""
        last_err = None
        best_partial = None          # 截断但已能解析：留作所有模式都失败后的兜底
        best_salvaged = False
        floor = max_tokens           # 某模式试出"预算不够"后，后面的模式直接从这儿起步
        tries = 0                    # 与模型的"交换次数"，播报给用户看
        dead_modes = set()           # 网关明确说"不支持这种 response_format"的模式
        woke_by_call = set()         # 靠"关掉思维链"救回来的模式，别重复关
        for mode, sch in attempts:
            if mode in dead_modes:
                continue
            budget = min(floor, ceiling)
            retried_nothink = False
            for _ in range(max(1, retries)):
                tries += 1
                note("第 %d 次交换：结构化输出 · 模式 %s · 预算 %d tokens"
                     % (tries, mode, budget))
                try:
                    if mode == "prompt_only":
                        out = self.chat(
                            _inject_json_instruction(messages, schema),
                            temperature, budget, timeout,
                            no_think=(mode in woke_by_call))
                    else:
                        out = self._chat_with_format(
                            messages, temperature, budget, timeout, mode, sch,
                            no_think=(mode in woke_by_call))
                    raw = out.get("content") or ""
                    truncated = (out.get("finish_reason") == "length")
                    if raw:
                        last_raw = raw
                        parsed, salvaged = extract_json(raw, with_meta=True)
                        if parsed is not None:
                            # 抢救来的结果**不算成功**：原文是坏的，只保住了
                            # 前面几个元素。当作 partial 存着兜底，继续试下一种模式。
                            if not truncated and not salvaged:
                                note("✓ 第 %d 次交换解析成功（模式 %s，正文 %d 字）"
                                     % (tries, mode, len(raw)))
                                return parsed, raw, mode, False
                            if best_partial is None:
                                best_partial = (parsed, raw, mode)
                                best_salvaged = salvaged
                            if salvaged and not truncated:
                                note("⚠ 第 %d 次交换：JSON 不完整，只抢救出部分内容"
                                     "（可能丢掉最后一个没写完的事件），继续尝试…"
                                     % tries)
                    if not raw or truncated:
                        if budget >= ceiling:
                            # 预算已到顶（可能是 _MAX_TOKENS_CAP，也可能是调用方
                            # 给的 cap），加倍是条死路。**唯一真能解的**是让模型
                            # 少说废话：推理模型的思维链会连着正文一起吃掉预算，
                            # 关掉它换来的空间是实打实的（实测 8192 预算下
                            # reasoning 占 627 token，近三成）。
                            if (not retried_nothink and mode not in woke_by_call
                                    and _think_hint_available(messages, schema)):
                                retried_nothink = True
                                woke_by_call.add(mode)
                                note("⚠ 输出被%s，预算已到顶 %d tokens——改用「关思考」"
                                     "重试一次（推理模型的思维链会连着正文一起吃预算）…"
                                     % ("截断" if truncated else "置空", ceiling))
                                continue
                            note("⚠ 输出被%s，预算已到顶 %d tokens，换下一种模式…"
                                 % ("截断" if truncated else "置空", ceiling))
                            break        # 同一模式已无手段可用，别空转
                        budget = min(budget * 2, ceiling)
                        floor = budget
                        note("⚠ 输出被%s，预算翻倍到 %d tokens 重试…"
                             % ("截断（finish=length）" if truncated else "置空",
                                budget))
                        continue
                except LLMError as e:
                    last_err = e
                    # 格式不被支持时直接降级，不重试。
                    # 注意：死模式的判定必须在**写日志之前**做——否则日志会
                    # 写"降级到 json_schema 重试"，而 json_schema 正是刚被
                    # 网关拒掉的那个（先改集合再取名字，就取到了自己）。
                    if e.status in (400, 404, 422) and mode != "prompt_only":
                        rejected = _is_format_reject(e)
                        nxt = _next_mode(mode, dead_modes)
                        if rejected:
                            dead_modes.add(mode)
                            nxt = _next_mode(mode, dead_modes)
                        note("⚠ 该模型不支持 %s 模式（HTTP %s），降级到 %s 重试…"
                             % (mode, e.status, nxt))
                        note("  注：降级后 schema 改由提示词约束（提示词会变长），"
                             "模型对字段的遵守度可能下降——若反复出现内容不完整，"
                             "把该档位换成原生支持 json_schema 的模型更稳。")
                        break
                    note("⚠ 第 %d 次交换失败：%s" % (tries, str(e)[:160]))
                    continue
        if best_partial is not None:
            note("△ 所有模式都没拿到完整输出，先用已解析的那部分（模式 %s）"
                 "——可能少了最后一个没写完的事件。" % best_partial[2])
            return best_partial[0], best_partial[1], best_partial[2], best_salvaged
        # 抛错前先摘掉"格式不支持"这类**与内容无关**的错误，否则用户看到的是
        # "This response_format type is unavailable"，会以为是配置坏了。
        if last_err and not last_raw:
            raise _downgrade_err(last_err, dead_modes, last_raw)
        return None, last_raw, "failed", False

    def _chat_with_format(self, messages, temperature, max_tokens, timeout,
                          mode, schema, no_think=False):
        """带 response_format 的结构化请求。

        注意：json_object 模式要求 messages 里出现 "json" 字样（OpenAI 规范，
        腾讯 tokenhub 严格执行），所以这里**先统一注入含 JSON 字样的格式说明**，
        再挂 response_format。顺带把 schema 也写进提示词，提高字段命中率。
        """
        msgs = _inject_json_instruction(messages, schema)
        payload = self._payload(msgs, temperature, max_tokens, False, True)
        if no_think:
            _apply_no_think(payload)
        if mode == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "out", "schema": schema, "strict": False},
            }
        elif mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        status, body = _post_json(self._endpoint(), payload, self._headers(), timeout)
        data = json.loads(body)
        return self._parse_full(data)


def _is_format_reject(e):
    """判断这个 400 是"网关不支持这种 response_format"，还是别的参数错。

    分不清的后果很实际：真·格式不支持被当成参数错会反复重试浪费调用；
    真·参数错被当成格式不支持则会被悄悄吞掉，用户只看到一句
    "This response_format type is unavailable"（其实模型根本没毛病）。
    """
    blob = ("%s %s" % (str(e), getattr(e, "raw", "") or "")).lower()
    for pat in ("response_format", "json_schema", "json object",
                "structured output", "response format"):
        if pat in blob:
            return True
    return False


def _downgrade_err(e, dead_modes=None, raw=""):
    """把"格式不支持"这类错误换成人能看懂的话，并说清为什么彻底失败。

    原始报文（tokenhub）是英文 + 中文两行，还夹着 request body 提示，
    直接抛到 UI 上，用户会以为是 API Key 或模型名写错了。
    更坏的情况是 last_err 恰好是那条无关的 "response_format unavailable"，
    它会把**真正的原因**（比如"所有模式都被思维链吃光、一个完整 JSON 都没拿到"）
    整条盖掉——所以这里要能同时说两件事。
    """
    parts = []
    if dead_modes:
        parts.append("该网关在本次调用中拒收了结构化输出（%s 不可用，已自动降级）"
                     % "、".join(sorted(dead_modes)))
    if raw.strip():
        parts.append("模型有返回内容但一个完整 JSON 都没能解析出来"
                     "（多半是推理模型的思维链把输出预算吃光、正文被截断）")
    else:
        parts.append("所有可用模式都没拿到任何输出")
    if not _is_format_reject(e):
        head = "结构化输出失败：" + "；".join(parts)
    else:
        head = "结构化输出失败：" + "；".join(parts)
    return LLMError(
        "%s。最后一次失败原因：%s" % (head, str(e)[:220]),
        status=e.status, provider=e.provider, raw=e.raw)


def _think_hint_available(messages, schema):
    """能不能靠"关思考"再搏一次。

    只在 chat_json 的降级链里用：预算已到顶、输出还在被思维链吃光时，
    关掉推理是唯一还能腾出空间的手段。有网关认不出这个字段，
    那就别浪费一次调用（它只会把无关字段忽略掉，等于白跑）。
    """
    return True


def _apply_no_think(payload):
    """就地关掉思维链（尽力而为，字段认不出的网关会忽略它，不会报错）。

    为什么值得做：deepseek-flash 这类推理模型的 reasoning 与正文**共用**
    max_tokens 预算。实测一次五层诊断，8192 预算里 reasoning 独占 627 token，
    输出一长就必然 finish=length，而正文永远排在思维链后面——等于先用
    将近三成预算写草稿，剩下的才用来交付。
    """
    payload["thinking"] = {"type": "disabled"}
    payload["enable_thinking"] = False
    payload["reasoning_effort"] = "none"
    payload["chat_template_kwargs"] = {"thinking": False}


def _next_mode(mode, dead=None):
    """降级链里的下一种模式，用于日志里说清楚"降到哪去了"。

    注意死模式要跳过：不然日志会写"降级到 json_schema 重试"，
    而 json_schema 恰恰是刚被网关拒掉的那个，用户看了会以为代码坏了。
    """
    chain = ["json_schema", "json_object", "prompt_only"]
    dead = dead or set()
    try:
        i = chain.index(mode)
    except ValueError:
        return "下一种模式"
    for nxt in chain[i + 1:]:
        if nxt not in dead:
            return nxt
    return "兜底提示词"


def _inject_json_instruction(messages, schema):
    """把"只要 JSON"的格式要求写进系统提示。

    两种模式都用它：
      · prompt_only  —— 唯一的格式约束手段
      · json_object / json_schema —— 不是为了约束，而是为了满足 OpenAI 规范
        「messages 里必须出现 json 字样」，否则网关拒收（腾讯 tokenhub 400001）。
    注意：hint 里必须保留 "JSON" 这个词，别顺手改成纯中文描述。
    """
    hint = "\n\n【输出格式】只输出一个合法的 JSON 对象，不要任何解释、不要 markdown 围栏。"
    if schema:
        hint += "\n必须符合以下 JSON Schema：\n" + json.dumps(schema, ensure_ascii=False)
    out = []
    done = False
    for m in messages:
        if m.get("role") == "system" and not done:
            out.append({"role": "system", "content": m["content"] + hint})
            done = True
        else:
            out.append(m)
    if not done:
        out.insert(0, {"role": "system", "content": hint.strip()})
    return out


# ============================================================ OpenAI 兼容

class OpenAIAdapter(BaseAdapter):
    kind = "openai"

    def _endpoint(self):
        return normalize_base_url(self.base_url) + "/chat/completions"

    def _headers(self):
        h = {"Authorization": "Bearer %s" % self.api_key} if self.api_key else {}
        return h

    def _payload(self, messages, temperature, max_tokens, stream, json_mode):
        p = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": stream,
        }
        if stream:
            # 关键：不显式开，多数厂商不下发 usage
            p["stream_options"] = {"include_usage": True}
        if json_mode:
            p["response_format"] = {"type": "json_object"}
        return p

    def _parse_delta(self, data):
        choices = data.get("choices") or []
        if not choices:
            return None, None, None
        ch = choices[0]
        delta = ch.get("delta") or {}
        # 思维链：不同厂商放不同字段，逐个探
        reasoning = None
        for key in ("reasoning_content", "reasoning", "thinking"):
            if delta.get(key):
                reasoning = delta[key]
                break
        content = delta.get("content")
        finish = ch.get("finish_reason")
        return reasoning, content, finish

    def _parse_full(self, data):
        choices = data.get("choices") or []
        if not choices:
            raise LLMError("响应缺少 choices：%s" % json.dumps(data, ensure_ascii=False)[:300])
        msg = choices[0].get("message") or {}
        reasoning = msg.get("reasoning_content") or msg.get("reasoning") or ""
        return {
            "content": msg.get("content") or "",
            "reasoning": reasoning,
            "finish_reason": normalize_finish(choices[0].get("finish_reason")),
            "usage": data.get("usage") or {},
            "model": data.get("model") or self.model,
        }


# ============================================================ Anthropic

class AnthropicAdapter(BaseAdapter):
    kind = "anthropic"

    def _endpoint(self):
        return normalize_base_url(self.base_url, "/messages") + "/messages"

    def _headers(self):
        return {
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

    def _split_system(self, messages):
        system = ""
        rest = []
        for m in messages:
            if m.get("role") == "system":
                system = (system + "\n" + m["content"]).strip()
            else:
                rest.append({"role": m["role"], "content": m["content"]})
        return system, rest

    def _payload(self, messages, temperature, max_tokens, stream, json_mode):
        system, rest = self._split_system(messages)
        p = {
            "model": self.model,
            "messages": rest,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "stream": stream,
        }
        if system:
            p["system"] = system
        return p

    def _parse_delta(self, data):
        t = data.get("type")
        if t == "content_block_delta":
            d = data.get("delta") or {}
            if d.get("type") == "thinking_delta":
                return d.get("thinking"), None, None
            if d.get("type") == "text_delta":
                return None, d.get("text"), None
        if t == "message_delta":
            sr = (data.get("delta") or {}).get("stop_reason")
            return None, None, sr
        return None, None, None

    def _parse_full(self, data):
        blocks = data.get("content") or []
        text, reasoning = "", ""
        for b in blocks:
            if b.get("type") == "text":
                text += b.get("text") or ""
            elif b.get("type") == "thinking":
                reasoning += b.get("thinking") or ""
        u = data.get("usage") or {}
        return {
            "content": text,
            "reasoning": reasoning,
            "finish_reason": normalize_finish(data.get("stop_reason")),
            "usage": {
                "prompt_tokens": u.get("input_tokens", 0),
                "completion_tokens": u.get("output_tokens", 0),
                "total_tokens": (u.get("input_tokens", 0) or 0) + (u.get("output_tokens", 0) or 0),
            },
            "model": data.get("model") or self.model,
        }


# ============================================================ Gemini

class GeminiAdapter(BaseAdapter):
    kind = "gemini"

    def _endpoint(self):
        base = normalize_base_url(self.base_url, ":generatecontent")
        if not base.lower().rstrip("/").endswith("/v1beta"):
            base = base.rstrip("/") + "/v1beta"
        return "%s/models/%s:generateContent" % (base, self.model)

    def _headers(self):
        return {"x-goog-api-key": self.api_key} if self.api_key else {}

    def _payload(self, messages, temperature, max_tokens, stream, json_mode):
        system, rest = AnthropicAdapter._split_system(self, messages)
        contents = []
        for m in rest:
            role = "model" if m["role"] == "assistant" else "user"
            contents.append({"role": role, "parts": [{"text": m["content"]}]})
        p = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system:
            p["systemInstruction"] = {"parts": [{"text": system}]}
        if json_mode:
            p["generationConfig"]["responseMimeType"] = "application/json"
        return p

    def _parse_delta(self, data):
        cands = data.get("candidates") or []
        if not cands:
            return None, None, None
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text") or "" for p in parts)
        fr = cands[0].get("finishReason")
        return None, text or None, fr

    def _parse_full(self, data):
        cands = data.get("candidates") or []
        if not cands:
            raise LLMError("Gemini 响应缺少 candidates")
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text") or "" for p in parts)
        um = data.get("usageMetadata") or {}
        return {
            "content": text,
            "reasoning": "",
            "finish_reason": normalize_finish(cands[0].get("finishReason")),
            "usage": {
                "prompt_tokens": um.get("promptTokenCount", 0),
                "completion_tokens": um.get("candidatesTokenCount", 0),
                "total_tokens": um.get("totalTokenCount", 0),
            },
            "model": self.model,
        }


# ============================================================ Ollama

class OllamaAdapter(BaseAdapter):
    kind = "ollama"

    def _endpoint(self):
        return normalize_base_url(self.base_url, "/api/chat") + "/api/chat"

    def _headers(self):
        return {}

    def _payload(self, messages, temperature, max_tokens, stream, json_mode):
        p = {
            "model": self.model,
            "messages": messages,
            "stream": stream,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        if json_mode:
            p["format"] = "json"
        return p

    def _parse_delta(self, data):
        msg = data.get("message") or {}
        done = data.get("done")
        return None, msg.get("content"), ("stop" if done else None)

    def _parse_full(self, data):
        msg = data.get("message") or {}
        return {
            "content": msg.get("content") or "",
            "reasoning": msg.get("thinking") or "",
            "finish_reason": normalize_finish(data.get("done_reason") or "stop"),
            "usage": {
                "prompt_tokens": data.get("prompt_eval_count", 0) or 0,
                "completion_tokens": data.get("eval_count", 0) or 0,
                "total_tokens": (data.get("prompt_eval_count", 0) or 0)
                                + (data.get("eval_count", 0) or 0),
            },
            "model": data.get("model") or self.model,
        }

    def stream(self, messages, temperature=0.8, max_tokens=4096, timeout=300):
        """Ollama 是 NDJSON，不是 SSE，需要覆写流式解析。"""
        payload = self._payload(messages, temperature, max_tokens, True, False)
        yield {"type": "start", "model": self.model, "provider": self.provider_key}
        usage = None
        for line in _post_stream(self._endpoint(), payload, self._headers(), timeout):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue
            _, content, fin = self._parse_delta(data)
            if content:
                yield {"type": "delta", "text": content}
            if data.get("done"):
                usage = {
                    "prompt_tokens": data.get("prompt_eval_count", 0) or 0,
                    "completion_tokens": data.get("eval_count", 0) or 0,
                    "total_tokens": (data.get("prompt_eval_count", 0) or 0)
                                    + (data.get("eval_count", 0) or 0),
                }
                break
        if usage:
            yield {"type": "usage", **usage}
        yield {"type": "end", "finish_reason": "stop"}


# ============================================================ 工厂

_ADAPTERS = {
    "openai": OpenAIAdapter,
    "anthropic": AnthropicAdapter,
    "gemini": GeminiAdapter,
    "ollama": OllamaAdapter,
}


def create_client(provider_key="custom", base_url="", api_key="", model="",
                  kind=None):
    """根据 provider 预设或显式 kind 创建客户端。"""
    preset = PROVIDERS.get(provider_key)
    if preset is None:
        preset = PROVIDERS["custom"]
    if kind is None:
        kind = preset.get("kind", "openai")
    cls = _ADAPTERS.get(kind, OpenAIAdapter)
    if not base_url:
        base_url = preset.get("base_url", "")
    if not model and preset.get("models"):
        model = preset["models"][0]
    if kind != "ollama" and not api_key:
        raise LLMConfigError("缺少 API Key（provider=%s）" % provider_key,
                             provider=provider_key)
    if not base_url:
        raise LLMConfigError("缺少 Base URL（provider=%s）" % provider_key,
                             provider=provider_key)
    return cls(base_url=base_url, api_key=api_key, model=model,
               provider_key=provider_key)


def list_providers():
    """返回给 UI 用的厂商清单。"""
    out = []
    for k, v in PROVIDERS.items():
        out.append({
            "key": k, "name": v["name"], "kind": v["kind"],
            "base_url": v["base_url"], "models": v["models"],
            "needs_key": v["kind"] != "ollama",
        })
    return out


def _friendly_test_error(err_text, status, base_url, model):
    """把网关的生硬报错翻译成人话，并给出可执行的下一步。

    腾讯 tokenhub、各家网关的报错格式都不一样，这里只做最常见的几种。
    """
    t = (err_text or "")
    low = t.lower()

    if status == 404:
        return ("接口路径不对（404）。多数情况是 Base URL 多带了 /chat/completions："
                "只要填到 /v1 这一层就行，系统会自动补端点。")
    if status == 401 or status == 403 or "invalid api key" in low \
            or "unauthorized" in low or "authentication" in low:
        return "密钥无效或没有该模型的权限（%s）。请核对 API Key 是否复制完整。" % (status or "401")
    if "does not exist" in low and "model" in low or "model_not_found" in low \
            or "400004" in t or "no such model" in low:
        return ("模型名「%s」在这个服务端不存在。注意有些网关要求带厂商前缀，"
                "比如填 deepseek/deepseek-flash 而不是 deepseek-flash。"
                "模型名以服务商控制台的清单为准。" % model)
    if "insufficient" in low or "quota" in low or "balance" in low \
            or "billing" in low or "arrears" in low:
        return "账号额度不足或欠费。请去服务商控制台充值后重试。"
    if "rate limit" in low or status == 429:
        return "触发限流（429）。稍等片刻再试，或降低并发。"
    if status in (502, 503, 504) or "upstream connect failed" in low \
            or "bad gateway" in low or "service unavailable" in low:
        if "积极拒绝" in t or "refused" in low or "10061" in t:
            return ("连不上目标地址（%s）。本地模型请先启动服务"
                    "（Ollama 默认 http://localhost:11434）；"
                    "远程地址检查 Base URL 是否写对。" % (status or 502))
        return "网关或上游服务暂时不可用（%s）。稍后重试。" % (status or 502)
    if "timeout" in low or "timed out" in low:
        return "请求超时。检查 Base URL 是否可访问、网络是否需要代理。"
    if "name or service not known" in low or "getaddrinfo" in low \
            or "connection refused" in low or "max retries" in low \
            or "无法连接" in t or "目标计算机积极拒绝" in t:
        return ("连不上这个地址。检查 Base URL 拼写；本地模型（Ollama/LM Studio）"
                "请确认服务已经启动。")
    if "stream" in low and "json" in low:
        return "服务端返回的流式数据无法解析。试试换一个模型，或关闭流式。"
    return ""


def test_connection(provider_key, base_url="", api_key="", model=""):
    """连通性自检：发一个极短请求。"""
    try:
        client = create_client(provider_key, base_url, api_key, model)
    except LLMConfigError as e:
        return {"ok": False, "error": str(e),
                "hint": "配置不完整，检查 Base URL 与 API Key 是否都填了。"}
    t0 = time.time()
    try:
        out = client.chat([{"role": "user", "content": "回复：ok"}],
                          temperature=0, max_tokens=8, timeout=30)
        return {
            "ok": True,
            "latency_ms": int((time.time() - t0) * 1000),
            "model": out.get("model"),
            "sample": (out.get("content") or "")[:40],
        }
    except LLMError as e:
        raw = str(e)
        return {"ok": False, "error": raw, "status": e.status,
                "hint": _friendly_test_error(raw, e.status, base_url, model)}
