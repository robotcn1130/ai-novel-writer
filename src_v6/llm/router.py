# -*- coding: utf-8 -*-
"""
任务路由层：把"任务档位"翻译成"具体模型调用"。

档位与任务的对应（方案 10.3）：
  world_sim        世界推演        中档模型 + 结构化输出（高频，成本命门）
  character_decide 角色决策        中档模型
  option_gen       选项生成        强模型（影响体验，值得花钱）
  prose_gen        正文生成        强模型
  state_extract    状态抽取        中/小模型
  completion_judge 完结判定        强模型
  summarize        摘要            小模型

设计要点：
  - 单一入口 LLMRouter.run(slot, ...)，上层永不知道具体厂商
  - 每次调用自动记 llm_usage
  - 支持全局默认档位 + 小说级覆盖
  - 密钥从 secrets.json 按 ref 取，不进数据库
"""
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC_V6 = os.path.dirname(_HERE)
if _SRC_V6 not in sys.path:
    sys.path.insert(0, _SRC_V6)

from llm import client as LLM
from llm import secrets as SEC

SLOT_LABELS = {
    "world_sim": "世界推演",
    "character_decide": "角色决策",
    "option_gen": "选项生成",
    "prose_gen": "正文生成",
    "state_extract": "状态抽取",
    "completion_judge": "完结判定",
    "summarize": "摘要",
    "diagnose": "逻辑诊断",
    # v6.7：语义级冲突兜底 + 事实分类兜底。
    # 加档位要**三处同步**（database.SLOTS / migrate._seed_defaults / 这里），
    # 少一处新功能一上线就是"档位未配置"。
    "conflict_check": "冲突检查",
    # v6.8：创建世界时按选定题材生成整份世界草案。
    "world_init": "世界构建",
}


class RouterConfigError(Exception):
    pass


class LLMRouter:
    """按档位路由 LLM 调用。db 为 NovelDatabase 实例（可空，空则不记用量）。"""

    def __init__(self, db=None, novel_id=0):
        self.db = db
        self.novel_id = novel_id
        self._cache = {}

    # ------------------------------------------------ 配置解析

    # v6.6：新增档位在老库上可能是"行存在但 model 为空"（_seed_defaults 只插占位），
    # 也可能是整行都不在（用户没跑 migrate）。两种情况都要降级，
    # 否则用户一点新功能就报「档位未配置」——新功能等于没上线。
    #
    # 判定写 `not (preset or {}).get("model")`，**不能写 `if not preset`**：
    # migrate 之后行是存在的、只是 model 空着，用 `not preset` 判就漏了降级。
    # 元组是**两元素**（备用档位, 目标温度）。
    FALLBACKS = {
        "diagnose": ("prose_gen", 0.3),
        # v6.7：冲突判定与事实分类都要"稳定比对"而非"发挥"，温度压到 0.2。
        # 降级到 state_extract 而不是 prose_gen：抽取器与判定器是同一类活
        # （读文本出结构化结论），拿正文档位去干这个会写得像散文。
        "conflict_check": ("state_extract", 0.2),
        # v6.8：世界构建与日常推演是同一类活（从设定长出世界），
        # 温度也一致（0.9，要发散，不要保守复述）。
        "world_init": ("world_sim", 0.9),
    }

    def _resolve(self, slot):
        """解析档位 -> (provider_row, model, temperature, max_tokens, extra)"""
        if self.db is None:
            raise RouterConfigError("未绑定数据库，无法解析模型档位")
        preset = self.db.get_model_preset(slot, novel_id=self.novel_id)
        if slot in self.FALLBACKS and not (preset or {}).get("model"):
            # 降级：借用备用档位的 provider/model，温度按本档位的意图重设
            # （诊断要低温稳定，不能跟着正文档位的 1.0 一起发散）
            alt, want_temp = self.FALLBACKS[slot]
            alt_preset = self.db.get_model_preset(alt, novel_id=self.novel_id)
            if (alt_preset or {}).get("model"):
                preset = dict(alt_preset)
                preset["temperature"] = want_temp
                preset.pop("max_tokens", None)   # 让它走本档位声明的默认
        if not preset:
            raise RouterConfigError("档位未配置：%s" % slot)
        model = (preset.get("model") or "").strip()
        if not model:
            raise RouterConfigError(
                "档位「%s」尚未指定模型，请先在设置页配置" % SLOT_LABELS.get(slot, slot))
        provider_row = None
        if preset.get("provider_id"):
            rows = [p for p in self.db.list_providers() if p["id"] == preset["provider_id"]]
            provider_row = rows[0] if rows else None
        if provider_row is None:
            providers = self.db.list_providers(active_only=True)
            if not providers:
                raise RouterConfigError("尚未添加任何 AI 服务商")
            provider_row = providers[0]
        return (provider_row, model, preset.get("temperature", 0.8),
                preset.get("max_tokens", 4096), preset.get("extra") or {})

    def _client(self, slot):
        provider_row, model, temp, mt, extra = self._resolve(slot)
        cache_key = (provider_row["id"], model)
        if cache_key in self._cache:
            cli = self._cache[cache_key]
        else:
            api_key = SEC.get_key(provider_row.get("api_key_ref") or "")
            cli = LLM.create_client(
                provider_key=self._preset_key(provider_row),
                base_url=provider_row.get("base_url") or "",
                api_key=api_key,
                model=model,
                kind=provider_row.get("kind") or "openai",
            )
            self._cache[cache_key] = cli
        return cli, model, temp, mt, extra, provider_row

    def _with_skill(self, slot, system):
        """把该档位绑定的技能正文拼到 system 前面（技能库 v6.9）。

        为什么在这里：这是所有档位调用唯一的必经处，改一处三个方法都生效。
        为什么**延迟导入**：engine 下那些模块反过来 import 本模块，
        顶层 import 就成了环。

        为什么失败一律静默返回原文：技能是增强不是依赖。技能被卸了、
        文件坏了、库没迁移，最坏的结果应该是「这次调用跟以前一样」，
        而不是整个推演崩掉。
        """
        if self.db is None:
            return system
        try:
            from engine import skillhub as SH
            return SH.apply_to_system(slot, system, self.db)
        except Exception:                                    # noqa: BLE001
            return system

    @staticmethod
    def _preset_key(provider_row):
        """服务商 -> 厂商预设键。

        优先用记录里的 preset_key；否则按名称猜；都没有就 custom。
        （旧代码这里写死 "custom"，导致每个服务商都走 custom 分支，
          思维链字段、JSON 降级链等厂商特性全部失效。）
        """
        pk = (provider_row.get("preset_key") or "").strip()
        if pk and pk in LLM.PROVIDERS:
            return pk
        name = (provider_row.get("name") or "").strip().lower()
        if name in LLM.PROVIDERS:
            return name
        return "custom"

    def is_ready(self, slot):
        """档位是否可用（不实际发请求）。"""
        try:
            provider_row, model, *_ = self._resolve(slot)
        except RouterConfigError:
            return False, "档位未配置"
        if provider_row.get("kind") != "ollama":
            if not SEC.get_key(provider_row.get("api_key_ref") or ""):
                return False, "缺少 API Key"
        return True, ""

    def ready_slots(self):
        out = {}
        for s in SLOT_LABELS:
            ok, reason = self.is_ready(s)
            out[s] = {"ok": ok, "reason": reason, "label": SLOT_LABELS[s]}
        return out

    # ------------------------------------------------ 调用

    def run(self, slot, messages=None, system="", user="", history=None,
            temperature=None, max_tokens=None, timeout=180, retries=1):
        """普通调用，返回 {'content','reasoning','usage',...}"""
        cli, model, temp, mt, extra, provider_row = self._client(slot)
        msgs = messages or LLM.build_messages(self._with_skill(slot, system), user, history)
        if temperature is None:
            temperature = temp
        if max_tokens is None:
            max_tokens = mt

        last_err = None
        label = SLOT_LABELS.get(slot, slot)
        for attempt in range(max(1, retries + 1)):
            LLM.note("【%s】调用模型 %s（第 %d 次尝试）"
                     % (label, model, attempt + 1))
            t0 = time.time()
            try:
                out = cli.chat(msgs, temperature=temperature, max_tokens=max_tokens,
                               timeout=timeout)
                self._log(slot, provider_row, model, out.get("usage") or {},
                          int((time.time() - t0) * 1000), True, "")
                out["slot"] = slot
                return out
            except LLM.LLMError as e:
                last_err = e
                self._log(slot, provider_row, model, {},
                          int((time.time() - t0) * 1000), False, str(e)[:200])
                if e.status in (401, 403):
                    LLM.note("【%s】密钥被拒（HTTP %s），不再重试" % (label, e.status))
                    break
                if attempt < retries:
                    LLM.note("【%s】第 %d 次失败，重试…" % (label, attempt + 1))
        raise last_err

    def run_json(self, slot, schema=None, messages=None, system="", user="",
                 history=None, temperature=None, max_tokens=None, timeout=180,
                 retries=2, with_meta=False, cap=None):
        """结构化输出调用，自动走降级链。

        默认返回 (parsed, raw, mode)；`with_meta=True` 时返回
        (parsed, raw, mode, salvaged)——salvaged=True 表示 raw 是被截断后
        「抢救」出来的，parser 只保住了前面几个元素，**调用方必须自己校验
        数据完整性**（见 engine.world.split_events）。

        `cap` 是输出预算的硬上限（见 client.chat_json）。结论型任务
        （诊断、判定）该给：**别让降级链把预算翻倍到 32k**——那只会让模型
        拿多出来的空间去复述材料，越写越长，最后仍是一个截断的 JSON。
        """
        cli, model, temp, mt, extra, provider_row = self._client(slot)
        msgs = messages or LLM.build_messages(self._with_skill(slot, system), user, history)
        if temperature is None:
            temperature = temp
        if max_tokens is None:
            max_tokens = mt
        if cap is not None and max_tokens > cap:
            max_tokens = cap
        t0 = time.time()
        LLM.note("【%s】调用模型 %s（结构化输出）" % (SLOT_LABELS.get(slot, slot), model))
        try:
            parsed, raw, mode, salvaged = cli.chat_json(
                msgs, schema=schema, temperature=temperature,
                max_tokens=max_tokens, timeout=timeout, retries=retries, cap=cap)
            usage = {"total_tokens": len(raw) // 2} if raw else {}
            self._log(slot, provider_row, model, usage,
                      int((time.time() - t0) * 1000), True, "")
            if with_meta:
                return parsed, raw, mode, salvaged
            return parsed, raw, mode
        except LLM.LLMError as e:
            self._log(slot, provider_row, model, {},
                      int((time.time() - t0) * 1000), False, str(e)[:200])
            raise

    def stream(self, slot, messages=None, system="", user="", history=None,
               temperature=None, max_tokens=None, timeout=300):
        """流式调用，产出统一事件流，结束后自动补 usage 记录。"""
        cli, model, temp, mt, extra, provider_row = self._client(slot)
        msgs = messages or LLM.build_messages(self._with_skill(slot, system), user, history)
        if temperature is None:
            temperature = temp
        if max_tokens is None:
            max_tokens = mt
        t0 = time.time()
        usage = {}
        failed = None
        LLM.note("【%s】开始流式生成（模型 %s）" % (SLOT_LABELS.get(slot, slot), model))
        try:
            for ev in cli.stream(msgs, temperature=temperature,
                                 max_tokens=max_tokens, timeout=timeout):
                if ev.get("type") == "usage":
                    usage = ev
                yield ev
        except LLM.LLMError as e:
            failed = e
            raise
        finally:
            LLM.note("【%s】流式生成结束，用时 %.1fs"
                     % (SLOT_LABELS.get(slot, slot), time.time() - t0))
            self._log(slot, provider_row, model, usage,
                      int((time.time() - t0) * 1000), failed is None,
                      str(failed)[:200] if failed else "")

    # ------------------------------------------------ 用量记账

    def _log(self, slot, provider_row, model, usage, latency_ms, success, error):
        if self.db is None:
            return
        try:
            self.db.log_usage(
                self.novel_id, slot, provider_row.get("name") or "",
                model, usage.get("prompt_tokens", 0),
                usage.get("completion_tokens", 0), latency_ms, success, error)
        except Exception:
            pass  # 记账失败绝不影响主流程
