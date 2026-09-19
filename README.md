# AI 小说创作系统 v6 · 世界模拟

**不预设大纲。** 先初始化一个世界和几个角色，让世界按自己的逻辑往前跑，
到关键岔路停下来问你，最后把已经发生的事写成正文。

> 本仓库只放 **v6 能跑的部分**：`src_v6/` 程序、`tests/` 测试、启动脚本。
> 数据库首次启动自动创建；文档、论文、旧版归档均不在库内。
> 旧版（v4/v5，大纲驱动）已废弃，不再维护，也没有兼容义务。

---

## 为什么重写

大纲驱动的文本有稳定的**结构指纹**，检测器一眼能认出来：

1. 情节因果过于工整
2. 每章自闭环
3. 结尾靠"主角想通了"收束
4. 主题被叙述者直接讲出
5. 时间永远线性

**病根在结构里，表面润色救不回来。** 你可以把"就在这时""仿佛"全删掉，
把形容词全换成细节，但只要章节骨架还是「抛问题 → 解决 → 悟出道理」，
指纹就还在。

世界模拟天然破解这五条：多角色并行让因果必然杂乱；世界状态连续让章节
无法自闭环；章节以**世界事件**收尾而不是内心顿悟；推演只问"发生了什么"
从不问"这意味着什么"；蒙太奇与多线并行天然打破线性。

---

## 快速开始

### 0. 环境

**Python 3.10+，零第三方依赖** —— 全部用标准库
（`sqlite3` / `http.server` / `urllib` / `json` / `argparse`）。
**不需要 `pip install` 任何东西。**

### 1. 启动创作台

双击 **`启动创作台_v6.bat`**。它会：

1. 自动定位 Python（PATH → `py -3` → WorkBuddy 托管解释器）
2. `sql/novel.db` 不存在时自动建库（幂等，可重复跑）
3. 起 Web 服务并打开 <http://127.0.0.1:8787>

手动等价：

```bash
python src_v6/core/migrate.py                  # 建库（只有第一次需要）
python src_v6/webui/server.py --port 8787 --open
```

创作台共五个页签：**世界 / 推演 / 章节 / 设置 / 技能库**。

### 2. 配置 AI 服务商

进 **设置**：

- **AI 服务商**：DeepSeek / 通义 / Kimi / 智谱 / MiniMax / SiliconFlow /
  OpenAI / Anthropic / xAI / Gemini / Ollama / LM Studio / 自定义，填 Base URL 和 API Key
- **模型档位**：七个档位分别指定用哪个模型（见下表）

API Key 存在 `%APPDATA%/NovelWriter/secrets.json`，**不进数据库、不进版本库**。

### 3. 创建一个世界

推荐用种子文件（模板见 `novels/_seeds/异常现象调查局_v6世界.json`）：

```bash
python src_v6/cli.py world-init --file novels/_seeds/我的世界.json
```

种子只需要四样东西：

| 要素 | 说明 |
|------|------|
| 世界背景 `premise` | 这个世界有什么不寻常的设定 |
| 核心张力 `initial_tension` | 什么东西正在绷紧 |
| 世界状态量 `world_state` | 可量化的开局数字，如 `异常实体.剩余数量 = 4` |
| 角色 `characters` | 每人性格 + 短期/长期目标（**可以没有目标**，后面随事件生成） |

背景与角色**都可以由 AI 生成**——你只说"我想看一个关于 X 的故事"，
AI 生成种子给你改。创作台的「新世界」页内置热门题材卡片，可一键起手。

### 4. 推演世界

```bash
python src_v6/cli.py world-advance 1 --focus "调查局内部出现隐瞒"
```

一次推演做七件事：装配上下文 → 生成硬约束 → LLM 推演 → 因果校验 →
应用状态变化 → 建决策点 → 推进世界时钟。

**推演只产结构化事件流**（谁 / 做了什么 / 为什么 / 结果），不产文学文本。
这是成本命门：推演用中档模型高频跑，写作才用强模型低频跑。

一轮产出 **2~4 个事件**，并且：

- **岔路是碰撞的产物，不是节拍器。** 这段里真的撞出必须由人拍板的岔路才记
  `decision_needed`（一次最多 1 个），没有就留空 —— 这是正常的。
- **世界会动。** 一段里应当有人被卷进来（`new_characters`）
  或有人退场（`character_exits`），并有别处在做事的线（`event_type="world"`）。
  **一个角色从头活到尾、人一个不增不减，是不正常的。**

### 5. 在岔路上做决定

推演产出的决策点按**托管模式**分流：

| 模式 | 谁决定 |
|------|--------|
| `viewer` 观影 | 全 AI，你只看 |
| `director` 导演（**默认**） | 主角归你，其余 AI |
| `tabletop` 跑团 | 全部你定 |

新登场角色默认 AI 托管，章节开头可随时调整。四种触发类型：
道德两难 / 重大代价 / 不可逆行动 / 你关注的焦点。

```bash
python src_v6/cli.py decisions 1                     # 看有哪些待决
python src_v6/cli.py options 3 --novel-id 1          # 生成选项
python src_v6/cli.py resolve 3 --index 0             # 选某个选项
python src_v6/cli.py resolve 3 --free "他什么也不说"   # 自由输入
```

### 6. 写成正文

```bash
python src_v6/cli.py write 1
```

生成器拿到的素材：**事件记录**（含每个角色当时的意图，写手最需要这个）、
角色性格与目标、作者的写作癖好、**上一章结尾的原文**、
以及由事件性质**自动推断**的章节形态。

六种形态：`scene` 单场景 / `montage` 蒙太奇 / `interlude` 间章 /
`aftermath` 余波 / `ensemble` 群像 / `transition` 过渡。

**不强制每章都有钩子。** `aftermath` 章就该平淡。

### 7. 发布 = 确认这一章进世界 ★

这是本系统最重要的一条语义：

> **点「发布」不是改一个状态字段，而是作者在确认"这一章写下的内容成立"。**

所以发布会触发一次**正文回流**，把这一章的正文真正结算进世界：

```
抽取（这一章实际写了什么）
  → 折成 projection
  → 分类（认知 / 候选 / 世界）
  → 三分流落库
  → 与正史冲突比对（只比 classification="world" 的）
  → 无 error 才 apply_soft_projection
```

落库后世界页面的状态量、角色、认知、线索都会随之更新。

**两条必须守住的边界：**

1. **只有 `severity="error"` 的冲突才拦。** `warning`（"正文写了正史没有的新事"）
   只记不拦 —— 那是章节常态，全拦会让冲突卡片退化成骚扰。
2. **待选（还没成章）的事件不许被顺手带进世界。** 发布只结算**这一章**
   （`chapter_ref` 匹配的那些）。后面推演出来、用户还没选的事件一条都不能被带走。

回流花 LLM（几十秒），因此**幂等**：已确认过的章节再点发布直接跳过，
不重复花钱。正文改过之后要重跑，用「重新确认」（`force`）。

---

## 命令行全表

```bash
python src_v6/cli.py novels            # 小说列表
python src_v6/cli.py world-init        # 初始化世界（--file 从 JSON 种子）
python src_v6/cli.py world-advance 1   # 推进世界（--focus / --chapter / --dry-run）
python src_v6/cli.py world-state 1     # 世界状态
python src_v6/cli.py clock 1           # 世界时钟
python src_v6/cli.py chars 1           # 角色列表
python src_v6/cli.py goals 1           # 目标列表
python src_v6/cli.py decisions 1       # 待决岔路
python src_v6/cli.py options 3         # 为某个岔路生成选项
python src_v6/cli.py resolve 3         # 落定岔路
python src_v6/cli.py auto-decide 1     # 按托管模式自动处理待决点
python src_v6/cli.py write 1           # 生成章节
python src_v6/cli.py chapters 1        # 章节列表
python src_v6/cli.py completion 1      # 完结判定
python src_v6/cli.py context 1         # 完整创作上下文
python src_v6/cli.py search "铜扣"      # 全文检索
python src_v6/cli.py stats             # 统计
python src_v6/cli.py providers         # AI 配置总览
python src_v6/cli.py doctor            # 数据库体检
```

---

## 五层架构

```
交互层          Web 创作台（五视图） / CLI
   ↓
决策调度层 ★   什么时候停下来问人（三托管模式 × 四触发类型）
   ↓
世界引擎       推演：状态+目标 → 结构化事件流（四道一致性防线）
   ↓
角色代理层     每个角色按自己的性格与目标做决定
   ↓
内容生成层     把事件流写成文学正文
   ↑
数据层         40 表 / 3 视图 / 3 触发器（贯穿全部层级）
```

### 四道一致性防线

高频推演会不断产生状态变化，没有防线世界会迅速自相矛盾：

| 防线 | 位置 | 管什么 |
|------|------|--------|
| ① 状态约束 | 推演前 | 把已确定的事实喂给模型，防止凭空改变（如把死人写活） |
| ② 因果校验 | 推演后 | 检查事件是否依赖了不存在的前提 |
| ③ hidden tracker | 推演中 | 性格变化缓慢积累，达到阈值才跃迁 —— **防角色突变** |
| ④ 用户否决权 | 推演后 | "这条不算" 的最终手段 |

### 五条硬约束（正史账本）

事件卡、正文、世界状态三者之间靠 `facts` / `fact_candidates` /
`fact_conflicts` / `location_beliefs` 记账。核心原则：

- **正史只有一个人口**：`tree.commit()` → `project_events()` → `apply_projection()`
  → `write_state_projection()` → `ticktime.advance()`，顺序敏感（新角色先建、关系最后写）。
- **认知分级**：角色知道什么、以为什么、听说什么，分 `know` / `believe` /
  `suspect` / `infer` 存放，不能互相冒充。
- **地点是状态不是设定**：人在自己家里走动不算改设定，只有跨地方才算冲突
  （按路径父级比较，`堂屋` vs `灶间` 同属一个家 → 不拦）。

### 七级模型档位

| 档位 | 用途 | 建议 |
|------|------|------|
| `world_sim` | 世界推演 | 中档 + 结构化输出（**成本命门，高频**） |
| `character_decide` | 角色决策 | 中档 |
| `option_gen` | 选项生成 | 强模型（影响体验，值得花钱） |
| `prose_gen` | 正文生成 | 强模型 |
| `state_extract` | 状态抽取（正文回流用） | 中/小模型 |
| `completion_judge` | 完结判定 | 强模型 |
| `summarize` | 摘要 | 小模型 |

> `state_extract` **没配就不会回流**。发布时若该档位未配置，会明确提示而不是静默失败。

### 技能库

「技能库」页可以把写作规范 / 检查清单之类的 Skill 包（文件夹、zip、
或 GitHub 仓库）导入，并**绑定到具体档位**，注入对应模型的 system prompt。
导入、绑定、卸载都在页面上完成；绑定的落点是 `app_config` 的
`skills.bindings` 键（JSON），不新建表。

### 三种完结条件

| 方式 | 判定 |
|------|------|
| `ai` | AI 三问：张力是否消解？目标是否达成/失败/失去意义？是否只是重复？ |
| `goal` | 结构化目标达成，如 `world.异常实体.剩余数量 == 0` |
| `both` | 两者兼用 |

条件表达式支持安全子集（**不用 `eval`**）：
`world.<key>`、`character.<名>.status/location/rank`、`thread.<标题>`、
`goal.<id>`、`count.goal.active`、`count.thread.active`。

---

## 目录结构

```
├── src_v6/                       # v6 全部代码
│   ├── core/
│   │   ├── schema_v6.sql         # 建表语句（单一事实来源）
│   │   ├── migrate.py            # 幂等迁移（自动备份、自动建目录）
│   │   └── database.py           # 数据访问层
│   ├── llm/
│   │   ├── client.py             # 13 家厂商统一协议
│   │   ├── secrets.py            # 密钥管理（不进 DB）
│   │   └── router.py             # 七档位路由
│   ├── engine/
│   │   ├── prompts.py            # 提示词库
│   │   ├── guard.py              # 四道防线
│   │   ├── applier.py            # 结构化 → 数据库
│   │   ├── facts.py              # 事实/认知/冲突记账
│   │   ├── world.py              # 世界推进引擎（心脏）
│   │   ├── tree.py               # 沙盘剧情树（推演 + 发布结算）
│   │   ├── ticktime.py           # 世界时钟
│   │   ├── character.py          # 角色代理层
│   │   ├── skillhub.py           # 技能库
│   │   ├── genretrend.py         # 热门题材
│   │   └── diagnose.py           # 章节诊断
│   ├── decide/scheduler.py       # 决策调度层
│   ├── generate/prose.py         # 内容生成层 + 正文回流
│   ├── webui/
│   │   ├── server.py             # 零依赖 Web 服务端
│   │   └── static/               # index.html + app.js
│   └── cli.py                    # 完整命令行
├── tests/                        # 测试（不需要联网 / API Key）
│   └── run_all.py                # 总入口
├── novels/_seeds/                # 世界种子 JSON（示例）
├── sql/novel.db                  # 运行时数据库（首次启动自动生成，不入库）
├── requirements.txt
└── 启动创作台_v6.bat              # Web 创作台启动器
```

---

## 数据库（40 表 / 3 视图 / 3 触发器）

| 分组 | 表 |
|------|-----|
| 世界 | `novels` `novel_attributes` `world_settings` `world_state` `world_state_log` `world_entities` `world_rules` `world_snapshots` `world_clock` `timeline` |
| 角色 | `characters` `character_goals` `character_relations` `character_identities` `character_knowledge` |
| 事件 | `events` `event_seeds` `story_nodes` `branches` |
| 事实 | `facts` `fact_candidates` `fact_conflicts` `fact_sources` |
| 地点 | `location_history` `location_beliefs` |
| 线索 | `foreshadowing`（`origin`: system / user / ai_emerged） |
| 决策 | `decision_points` |
| 章节 | `chapters` `chapter_versions` `chapter_casting` `chapter_event_links` `chapters_fts` |
| 完结 | `world_goals` |
| 声音 | `author_voice` |
| 配置 | `app_config` `providers` `model_presets` `llm_usage` `prompts` `schema_migrations` |

**视图**：`v_novel_overview` `v_active_threads` `v_pending_decisions`
**触发器**：`chapters_fts_ai` / `_au` / `_ad`（章节增改删时自动同步检索索引）

**全文检索**用 FTS5 独立副本表（`chapters_fts.chapter_ref` = `chapters.id` + 触发器同步）。
**不要改回 external content 模式**（本库曾触发 malformed）。
trigram 查询词需 ≥3 字，不足时应用层自动回退 LIKE。

---

## 维护

**改结构的唯一正确姿势**：改 `src_v6/core/schema_v6.sql` → 跑 `migrate.py`。
**禁止手写 `ALTER` 直接改库。**

```bash
python src_v6/core/migrate.py     # 迁移（幂等，自动备份到 backup/）
python src_v6/cli.py doctor       # 数据库体检
python src_v6/cli.py providers    # AI 配置总览
python src_v6/cli.py context 1    # 完整创作上下文
```

---

## 测试

```bash
python tests/run_all.py
```

十套，合计 **360+ 项断言**，**不需要联网也不需要 API Key**：

| 套件 | 验什么 |
|------|--------|
| `t_regress.py` | 全站只读接口回归（`dispatch` 是所有路由的必经处） |
| `t_import_http.py` | 技能导入链路（文件夹/zip/md、改名、二次确认、卸载） |
| `t_gen.py` | AI 生成链路收口（喂脏数据，验证产出一定能装） |
| `t_render.js` | 前端渲染桩（模板字符串、字段名、`undefined` 泄漏） |
| `t_bind.js` | 绑定交互（复选框与下拉联动；"选技能被当成停用"的死锁） |
| `t_prompt.py` | 推演提示词关键约束（事件卡形态，防静默回退） |
| `t_tree.py` | 剧情树引擎（游标类型、人事变动通道） |
| `t_confirm.py` | **发布=确认进世界**（幂等 / 档位 / 硬冲突 / 异常） |
| `t_know.py` | 认知边界 `has_knowledge`（SQL 字面 `%` 撞格式化） |
| `t_live.py` | 真机（真实库 + 临时技能目录，验注入真生效） |

约定：全部指向**副本库 / 临时目录**，不碰 `sql/novel.db`，不写用户真实技能目录。
`t_live.py` 需要库里有真实数据，数据不够时相关断言报 `SKIP`（不算失败）。

改到对应模块时**必须先跑**：

| 改动 | 必跑 |
|---|---|
| `story_nodes` / `tree.py` | `tests/t_tree.py` |
| 提示词 / schema | `tests/t_prompt.py` |
| 正文回流 / 发布 / `confirm_chapter` | `tests/t_confirm.py` |
| `has_knowledge` / 认知 / 地点事实 / 新角色建档 | `tests/t_know.py` |

前端另有语法检查：

```bash
node --check src_v6/webui/static/app.js
```

---

## 已知边界

- **单机单用户**。Web 服务默认绑 `127.0.0.1`，无鉴权，**不要暴露到公网**。
- **桌面打包未做**。选型是 Nuitka + pywebview（~30-50MB），尚未实施。
  当前以 `.bat` 启动本地服务 + 浏览器访问的形式使用。
- **插件生态未做**（内置能力 / 官方插件 / 兼容层 / 第三方准入），代码尚未实现。
- 旧版（v4/v5）与本版**数据库结构不兼容**，不做迁移。

---

## 许可证

MIT License
