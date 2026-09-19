-- ============================================================
-- AI 小说创作系统 · Schema v6.7
-- 范式：Simulation-Based Narrative（世界模拟驱动）
-- 与 v4 的根本差别：
--   1. 世界状态是第一等公民（world_state / world_clock / world_entities）
--   2. 角色有独立目标表（character_goals），且目标可为空
--   3. 章节来自自然成章，不再有 chapter_outlines 驱动
--   4. 决策点是一等公民（decision_points），人机分工可配置
--   5. 悬置线索 open_threads 自动涌现，不同于预埋伏笔
--   6. 不要追读力指标 / 三线占比 / AI 痕迹检测（已废弃）
-- 兼容性：不兼容 v4 旧库。旧库已归档为 sql/legacy_v4.db。
-- 变更流程：改本文件 → 运行 python src_v6/core/migrate.py
--
-- v6.7 新增（正史层）：状态从"多处手工写入"收窄成"单一投影管线"
--   events(不可变正史) → facts(正史账本，只 supersede 不 DELETE) → world_state(只读缓存)
--   · 唯一写正史的入口 = StoryTree.commit()
--   · 唯一落投影的函数 = applier.apply_projection()
--   · 世界真相(facts) 与 角色认知(character_knowledge) **物理分离**，永不互相污染
--   · 正文回流：**先分类，再比对**——"无冲突"不等于"是真的"
--   · world_state 写权限在代码层收口（db.set_state_value 的 writer 白名单）
-- ============================================================

PRAGMA foreign_keys = ON;

-- ============================================================
-- 第一节 基础元信息
-- ============================================================

CREATE TABLE IF NOT EXISTS schema_migrations (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    version      INTEGER NOT NULL,
    applied_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    note         TEXT    NOT NULL DEFAULT ''
);

-- 全局键值配置（应用级，不属于某本小说）
CREATE TABLE IF NOT EXISTS app_config (
    key          TEXT PRIMARY KEY,
    value        TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
);

-- ============================================================
-- 第二节 小说 / 世界总览
-- ============================================================

CREATE TABLE IF NOT EXISTS novels (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    title                TEXT    NOT NULL,
    genre                TEXT    NOT NULL DEFAULT '',          -- 题材
    theme                TEXT    NOT NULL DEFAULT '',          -- 主题（可为空，不强制）
    style                TEXT    NOT NULL DEFAULT '',          -- 文风
    tone                 TEXT    NOT NULL DEFAULT '',          -- 基调
    -- v6: 不再写"全书主线情节"，只记"初始张力"（世界启动时的核心矛盾，允许后续漂移）
    initial_tension      TEXT    NOT NULL DEFAULT '',
    premise              TEXT    NOT NULL DEFAULT '',          -- 一句话设定（世界基础背景）
    synopsis             TEXT    NOT NULL DEFAULT '',          -- 简介（可由系统自动回填）
    current_chapter      INTEGER NOT NULL DEFAULT 0,
    target_chapters      INTEGER NOT NULL DEFAULT 0,           -- 0 = 不设上限
    target_words_per_chapter INTEGER NOT NULL DEFAULT 3000,
    -- v6 新增：世界模拟运行参数
    decision_mode        TEXT    NOT NULL DEFAULT 'director'
                         CHECK (decision_mode IN ('viewer', 'director', 'tabletop')),
    -- viewer=全 AI 决策 / director=主角归用户（默认）/ tabletop=全用户决策
    auto_advance         INTEGER NOT NULL DEFAULT 0,           -- 是否允许连续自动推进
    completion_mode      TEXT    NOT NULL DEFAULT 'ai'
                         CHECK (completion_mode IN ('ai', 'goal', 'both')),
    status               TEXT    NOT NULL DEFAULT 'draft'
                         CHECK (status IN ('draft', 'writing', 'completed', 'paused', 'archived')),
    deleted_at           TEXT,
    created_at           TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at           TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_novels_status ON novels(status, deleted_at);

-- 小说级任意属性（open-schema 兜底区）
CREATE TABLE IF NOT EXISTS novel_attributes (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id     INTEGER NOT NULL,
    key          TEXT    NOT NULL,
    value        TEXT    NOT NULL DEFAULT '',
    updated_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (novel_id, key),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);

-- 世界设定条目（分类归档，用户可自由增补）
CREATE TABLE IF NOT EXISTS world_settings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id     INTEGER NOT NULL,
    category     TEXT    NOT NULL DEFAULT 'other',
    -- geography/time/rule/faction/resource/power_system/custom/other
    name         TEXT    NOT NULL DEFAULT '',
    content      TEXT    NOT NULL DEFAULT '',
    importance   INTEGER NOT NULL DEFAULT 3,                   -- 1-5
    -- v6.7：标 hard 的设定在初始化时会**同时写一条 world_rules**，
    -- 从而进入 guard 的逐条校验（原来只是一坨给模型看的自由文本，违反了没人知道）。
    rule_level   TEXT    NOT NULL DEFAULT '',                  -- '' / 'hard' / 'soft'
    created_at   TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_world_settings_novel
    ON world_settings(novel_id, category);

-- ============================================================
-- 第三节 世界状态机（v6 新增核心）
-- ============================================================

-- 世界时钟：模拟推进的时间轴
CREATE TABLE IF NOT EXISTS world_clock (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id       INTEGER NOT NULL,
    -- v6.7 语义重定义：current_time 是**只读展示文本**（display_time）。
    -- 它是虚构历法（"第三纪元 214 年 霜月 12 日 黄昏"），任何比较/排序/到期
    -- 判定都不得读它——读它就等于让字符串比大小。要比较一律读 absolute_tick。
    current_time   TEXT    NOT NULL DEFAULT '',
    display_note   TEXT    NOT NULL DEFAULT '',
    total_ticks    INTEGER NOT NULL DEFAULT 0,                 -- absolute_tick 的**只读别名**（老代码仍读它）
    granularity    TEXT    NOT NULL DEFAULT 'scene'
                   CHECK (granularity IN ('scene', 'day', 'stage')),
    -- v6.7 双层时间：tick 是唯一可比较的世界时间。
    -- 为什么不让 LLM 直接给绝对 tick：同一句"霜月十五"两次调用可能给出 9127 和 9131。
    -- LLM 只出增量（tick_delta，数个数就行），累加交给 Python（整数运算，结果确定）。
    absolute_tick  INTEGER NOT NULL DEFAULT 0,
    tick_unit      TEXT    NOT NULL DEFAULT 'scene'
                   CHECK (tick_unit IN ('scene', 'day', 'stage')),
    tick_unit_note TEXT    NOT NULL DEFAULT '',                -- 展示文案（"1 tick = 一场戏"）
    last_chapter   INTEGER NOT NULL DEFAULT 0,                 -- 上次推进产出到第几章
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (novel_id),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);

-- 世界实体：势力 / 地点 / 组织 / 物件 / 概念
-- 用 open-schema attributes 允许字段随剧情生长
CREATE TABLE IF NOT EXISTS world_entities (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id       INTEGER NOT NULL,
    entity_type    TEXT    NOT NULL DEFAULT 'other'
                   CHECK (entity_type IN ('faction', 'location', 'organization',
                                          'item', 'concept', 'phenomenon', 'other')),
    name           TEXT    NOT NULL,
    description    TEXT    NOT NULL DEFAULT '',
    status         TEXT    NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active', 'latent', 'destroyed', 'unknown', 'resolved')),
    power_level    INTEGER NOT NULL DEFAULT 3,                 -- 1-5 强度/影响力
    visibility     TEXT    NOT NULL DEFAULT 'public'
                   CHECK (visibility IN ('public', 'hidden', 'revealed')),
    -- v6.4 地点树：location 类型实体可以挂在父地点下面
    -- （旧城区 → 调查局本部 → 地下二层 → 封锁库）。
    -- 不再用 "本部 茶水间" 这种空格拼接的伪层级：那种写法既不能校验，
    -- 也没法回答"这两间屋在同一层吗"。非 location 实体一律留空。
    parent_id      INTEGER,                                    -- 自引用；NULL = 顶层
    location_level TEXT    NOT NULL DEFAULT ''
                   CHECK (location_level IN ('', 'city', 'district', 'building',
                                             'floor', 'room', 'site', 'other')),
    -- v6.4：这处地点本身是否隐秘（封锁库、密室、秘密据点）。
    -- 秘密地点里发生的事，对不知情者连"他进了这栋楼"都不给。
    is_secret      INTEGER NOT NULL DEFAULT 0,
    attributes     TEXT    NOT NULL DEFAULT '{}',              -- JSON open-schema
    first_appear_chapter INTEGER NOT NULL DEFAULT 0,
    deleted_at     TEXT,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (novel_id, entity_type, name),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_world_entities_novel
    ON world_entities(novel_id, entity_type, status);

-- 地点树按父节点查子节点（地图渲染、算"这栋楼里都有谁"都要用）。
-- 注意：老库升级时 parent_id 由 migrate.py 的 _add_columns 后补，
-- 而本文件是在补列**之前**执行的，所以这条索引在 migrate 里也会补建一次
-- （见 _add_columns 末尾）。此处保留是为了全新库一步到位。
CREATE INDEX IF NOT EXISTS idx_world_entities_parent
    ON world_entities(novel_id, parent_id);

-- 世界状态快照：按 key 存储可比较的状态量
-- 例：key='异常实体.剩余数量' value='7' value_type='int'
--
-- v6.7 语义重定义：本表从"事实的存储处"降级为 **facts 的只读投影缓存**。
--   derived_from_fact_id 非空 → 自动投影写进来的，可被下一次投影覆盖；
--   derived_from_fact_id 为空 → **用户在世界页手改的值，投影一律跳过**。
-- 最后一行修掉的是老问题："世界状态页改完就丢"——手工值与推演值混在一起，
-- 下一章推演一覆盖就没了。现在手工值有豁免权。
CREATE TABLE IF NOT EXISTS world_state (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id       INTEGER NOT NULL,
    key            TEXT    NOT NULL,
    value          TEXT    NOT NULL DEFAULT '',
    value_type     TEXT    NOT NULL DEFAULT 'text'
                   CHECK (value_type IN ('text', 'int', 'float', 'bool', 'json')),
    category       TEXT    NOT NULL DEFAULT 'other'
                   CHECK (category IN ('tension', 'resource', 'threat',
                                       'relation', 'progress', 'other')),
    note           TEXT    NOT NULL DEFAULT '',
    chapter_ref    INTEGER NOT NULL DEFAULT 0,                 -- 该值最新的来源章
    derived_from_fact_id INTEGER,                              -- 由哪条 fact 投影而来（NULL=手工设置，豁免覆盖）
    projected_at_tick    INTEGER NOT NULL DEFAULT 0,           -- 投影发生在哪个 tick
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (novel_id, key),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_world_state_novel
    ON world_state(novel_id, category);

-- 状态变更日志：为"因果校验"和"回溯"提供依据
CREATE TABLE IF NOT EXISTS world_state_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id       INTEGER NOT NULL,
    key            TEXT    NOT NULL,
    old_value      TEXT    NOT NULL DEFAULT '',
    new_value      TEXT    NOT NULL DEFAULT '',
    reason         TEXT    NOT NULL DEFAULT '',
    chapter_ref    INTEGER NOT NULL DEFAULT 0,
    -- v6.7：状态变更可追到具体事件 / 具体 fact / 具体 tick。
    -- 没有这三列，"这个数字是谁改的"只能靠翻推演日志。
    event_id       INTEGER,
    fact_id        INTEGER,
    tick           INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_world_state_log_novel
    ON world_state_log(novel_id, key, id);

-- ============================================================
-- 第四节 角色
-- ============================================================

CREATE TABLE IF NOT EXISTS characters (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id       INTEGER NOT NULL,
    name           TEXT    NOT NULL,
    alias          TEXT    NOT NULL DEFAULT '',                -- 别名/称谓
    rank           TEXT    NOT NULL DEFAULT 'C'
                   CHECK (rank IN ('A', 'B', 'C', 'D', 'E')),
    role_tag       TEXT    NOT NULL DEFAULT '',                -- 职业/身份标签（自由文本）
    personality    TEXT    NOT NULL DEFAULT '',
    background     TEXT    NOT NULL DEFAULT '',
    speech_style   TEXT    NOT NULL DEFAULT '',
    appearance     TEXT    NOT NULL DEFAULT '',
    -- 角色当前状态
    status         TEXT    NOT NULL DEFAULT 'alive'
                   CHECK (status IN ('alive', 'dead', 'missing', 'retired', 'unknown')),
    -- current_location：给人看的展示文本（"调查局本部 档案室"）。
    -- 保留它是因为老数据、CLI 输出、正文提示词都在用，且它是"位置被说出来时
    -- 的原始措辞"，有叙事价值（"封锁库"和"地下二层那间不许进的门"是同一处地方，
    -- 但后者是角色口中的说法）。
    current_location TEXT  NOT NULL DEFAULT '',
    -- v6.4：结构化位置。指向 world_entities 里的 location 节点。
    -- 有 location_id 才能回答"这栋楼里都有谁""这两人在不在一层"，
    -- 光靠 current_location 那串自由文本做不到（名字写法一变就对不上）。
    location_id    INTEGER,                                    -- 指向 world_entities
    -- 位置精度：剧情演到哪一级就记到哪一级。没演到的部分不编。
    location_precision TEXT NOT NULL DEFAULT 'unknown'
                   CHECK (location_precision IN ('unknown', 'city', 'district',
                                                 'building', 'floor', 'room', 'site',
                                                 'other')),
    location_updated_at TEXT NOT NULL DEFAULT '',              -- 最后一次确认位置的世界时间
    -- v6 新增：决策倾向（供 AI 托管该角色时的性格锚）
    decision_tendency TEXT NOT NULL DEFAULT '',
    -- v6 新增：记忆流（JSON 数组，该角色亲历的关键事件摘要，防止"角色知道了他不该知道的"）
    --
    -- v6.7 分工（必须写清，否则两套会打架）：
    --   memory              = 粗粒度、只增不查的历史流水，给正文/角色决策当"氛围上下文"；
    --                         **不再被任何校验逻辑读取**
    --   character_knowledge = 可查询、可作废、可判对错的认知状态，**唯一允许用于逻辑校验**
    memory         TEXT    NOT NULL DEFAULT '[]',
    -- v6.7：上次更新认知时的 tick
    knowledge_tick INTEGER NOT NULL DEFAULT 0,
    -- v6 新增：托管模式（是否由用户决策）
    control_mode   TEXT    NOT NULL DEFAULT 'ai'
                   CHECK (control_mode IN ('ai', 'user', 'auto_delegate')),
    -- v6 新增：hidden tracker（微弱信号累积，防角色突变）
    -- 结构：{"<trait_key>": {"value": 0-100, "pending": 0-100, "last_update_chapter": N}}
    tracker        TEXT    NOT NULL DEFAULT '{}',
    attributes     TEXT    NOT NULL DEFAULT '{}',              -- JSON open-schema
    first_appear_chapter INTEGER NOT NULL DEFAULT 0,
    deleted_at     TEXT,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (novel_id, name),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_characters_novel
    ON characters(novel_id, rank, status);

-- 角色目标（v6 新增）：短期 / 长期，均可为空
CREATE TABLE IF NOT EXISTS character_goals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id       INTEGER NOT NULL,
    character_id   INTEGER NOT NULL,
    goal_type      TEXT    NOT NULL DEFAULT 'short'
                   CHECK (goal_type IN ('short', 'long')),
    content        TEXT    NOT NULL,
    motivation     TEXT    NOT NULL DEFAULT '',                -- 动机（为什么想要）
    obstacle       TEXT    NOT NULL DEFAULT '',                -- 当前障碍
    priority       INTEGER NOT NULL DEFAULT 3,                 -- 1-5
    progress       INTEGER NOT NULL DEFAULT 0,                 -- 0-100
    status         TEXT    NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active', 'achieved', 'failed',
                                     'abandoned', 'evolved')),
    -- v6.7 生命周期：比 status 更细的过程态。
    -- 终态直接驱动 status（achieved→achieved / failed→failed / transformed→evolved），
    -- 收口写在 db.set_goal_lifecycle()，**不要在两处各改一次**（改了必然不一致）。
    lifecycle      TEXT    NOT NULL DEFAULT 'active'
                   CHECK (lifecycle IN ('dormant', 'active', 'at_risk',
                                        'achieved', 'failed', 'transformed')),
    success_condition TEXT NOT NULL DEFAULT '',                -- 什么情况算达成（供 AI 复核，不靠"感觉"）
    fail_condition    TEXT NOT NULL DEFAULT '',                -- 什么情况算失败
    last_reviewed_chapter INTEGER NOT NULL DEFAULT 0,          -- 上次被引擎复核是哪一章
    evolved_from   INTEGER,                                    -- 由哪个目标演化而来
    origin         TEXT    NOT NULL DEFAULT 'init'
                   CHECK (origin IN ('init', 'ai_generated', 'user_added', 'event_driven')),
    created_chapter INTEGER NOT NULL DEFAULT 0,
    settled_chapter INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE,
    FOREIGN KEY (character_id) REFERENCES characters(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_character_goals_char
    ON character_goals(character_id, goal_type, status);

-- 角色关系
CREATE TABLE IF NOT EXISTS character_relations (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id       INTEGER NOT NULL,
    character_a_id INTEGER NOT NULL,
    character_b_id INTEGER NOT NULL,
    relation_type  TEXT    NOT NULL DEFAULT 'other'
                   CHECK (relation_type IN ('family', 'friend', 'enemy', 'lover',
                                            'colleague', 'mentor', 'rival', 'other')),
    description    TEXT    NOT NULL DEFAULT '',
    intensity      INTEGER NOT NULL DEFAULT 3,                 -- 1-5
    status         TEXT    NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active', 'broken', 'evolved')),
    updated_chapter INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (novel_id, character_a_id, character_b_id, relation_type),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE,
    FOREIGN KEY (character_a_id) REFERENCES characters(id) ON DELETE CASCADE,
    FOREIGN KEY (character_b_id) REFERENCES characters(id) ON DELETE CASCADE
);

-- 角色身份（多身份/伪装/职位变迁）
CREATE TABLE IF NOT EXISTS character_identities (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id       INTEGER NOT NULL,
    character_id   INTEGER NOT NULL,
    identity_name  TEXT    NOT NULL,
    description    TEXT    NOT NULL DEFAULT '',
    is_secret      INTEGER NOT NULL DEFAULT 0,
    known_by       TEXT    NOT NULL DEFAULT '[]',              -- JSON 角色 id 数组（谁知道）
    from_chapter   INTEGER NOT NULL DEFAULT 0,
    to_chapter     INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE,
    FOREIGN KEY (character_id) REFERENCES characters(id) ON DELETE CASCADE
);

-- v6.4 行踪认知：某人此刻"以为"目标在哪。
--
-- 为什么不能只有 characters.current_location：
--   current_location 是**事实**（上帝视角），但角色做决定靠的是**他以为的事**。
--   林默悄悄下了封锁库，事实是他在地下二层，可老周根本不知道——
--   老周此刻若是按"林默在档案室"去找人，那才是合逻辑的行动。
--   把两者混成一个字段，AI 就会让老周径直走向封锁库（"他知道他不该知道的"）。
--
-- 只记**秘密行踪**（用户选定）：位置本身不隐秘时，所有人都默认知道，
-- 没必要给每对人存一行（那是 O(n²) 的账，且推演会变慢）。
-- 所以本表只在 (目标位置 is_secret=1) 或 (事件声明 location_secret=1) 时才写。
CREATE TABLE IF NOT EXISTS location_beliefs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id       INTEGER NOT NULL,
    -- 被观察的人
    character_id   INTEGER NOT NULL,
    -- 观察者（谁这么以为）
    observer_id    INTEGER NOT NULL,
    -- 观察者以为他所在的地点节点（可能比真实位置粗一级）
    believed_location_id INTEGER,
    believed_text  TEXT    NOT NULL DEFAULT '',      -- 观察者能说出的说法
    precision      TEXT    NOT NULL DEFAULT 'unknown'
                   CHECK (precision IN ('unknown', 'city', 'district', 'building',
                                        'floor', 'room', 'site', 'other')),
    -- 何时形成的这条认知（世界时间 / 章节），以及是否已被证伪
    known_since    TEXT    NOT NULL DEFAULT '',
    chapter_ref    INTEGER NOT NULL DEFAULT 0,
    stale          INTEGER NOT NULL DEFAULT 0,       -- 1 = 后来被推翻/已过期
    -- v6.7 定位重定义：本表降级为 character_knowledge 中 about_type='location'
    -- 的**专用快速索引**（唯一权威是 character_knowledge，knowledge_id 反向指回去）。
    -- 为什么还留着：地图渲染与【行踪认知差异】要按 UNIQUE(novel_id, character_id,
    -- observer_id) 秒查"谁以为谁在哪"，走通用表要多两个 JOIN 且没有唯一约束。
    -- 双写收口在 db.set_location_belief()，**任何地方不得直接 INSERT 本表**（迁移回填除外）。
    know_type      TEXT    NOT NULL DEFAULT 'believe'
                   CHECK (know_type IN ('know', 'believe', 'suspect', 'assume')),
    confidence     INTEGER NOT NULL DEFAULT 3,
    source         TEXT    NOT NULL DEFAULT 'witnessed',
    knowledge_id   INTEGER,                          -- 对应的 character_knowledge.id
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (novel_id, character_id, observer_id),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE,
    FOREIGN KEY (character_id) REFERENCES characters(id) ON DELETE CASCADE,
    FOREIGN KEY (observer_id) REFERENCES characters(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_location_beliefs_novel
    ON location_beliefs(novel_id, character_id);

-- 位置轨迹：谁在何时去了哪（地图的"最近动向"，也是回看"他怎么走过来的"）
CREATE TABLE IF NOT EXISTS location_history (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id       INTEGER NOT NULL,
    character_id   INTEGER NOT NULL,
    location_id    INTEGER,
    location_text  TEXT    NOT NULL DEFAULT '',
    precision      TEXT    NOT NULL DEFAULT 'unknown',
    world_time     TEXT    NOT NULL DEFAULT '',
    -- v6.7：脱离自由文本的可比较时间（world_time 只做展示）
    tick           INTEGER NOT NULL DEFAULT 0,
    chapter_ref    INTEGER NOT NULL DEFAULT 0,
    event_id       INTEGER,
    is_secret      INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE,
    FOREIGN KEY (character_id) REFERENCES characters(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_location_history_char
    ON location_history(novel_id, character_id, id DESC);

-- ============================================================
-- 第五节 事件流（v6 核心）
-- ============================================================
--
-- v6.7 定义：events 是 **Canonical Events（不可变正史）**。
--   · 唯一写入口 = StoryTree.commit()（世界级推进走 advance()，产出标 branch_id=0）
--   · canonical=1 表示这是正史；沙盘事件**不进这张表**（只活在 story_nodes）
--   · immutable=1 表示禁止 UPDATE 正文性字段——"正史被随手改掉"在代码层不可能发生
--   · 否决一条事件走 deleted_at（revert_event），不是改内容
--   · is_background=1 = pulse() 产出的背景事件：**真的发生过**，
--     但正文装配时要能单独过滤（第 70 章可以引用第 10 章的战争）

CREATE TABLE IF NOT EXISTS events (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id       INTEGER NOT NULL,
    chapter_ref    INTEGER NOT NULL DEFAULT 0,                 -- 归属章节（0 = 尚未成章）
    sequence       INTEGER NOT NULL DEFAULT 0,                 -- 世界推进内的序号
    title          TEXT    NOT NULL DEFAULT '',
    description    TEXT    NOT NULL DEFAULT '',
    -- v6.7：新增 canon_revision = **作者的元层操作**（正史修正），不是剧情内发生的事。
    -- 与 reveal（剧情内揭示）的区别在"谁产生 / 该不该进正文装配"：
    --   reveal        → 剧情推进，必须写进正文
    --   canon_revision→ 设定被改，**必须被 prose.build_events_block() 过滤掉**，
    --                   否则第 20 章会莫名写一段"作者修正了设定"
    -- 它只由用户动作产生（source_kind='user'），模型不得自产。
    event_type     TEXT    NOT NULL DEFAULT 'plot'
                   CHECK (event_type IN ('plot', 'character', 'world', 'conflict',
                                         'reveal', 'decision', 'transition',
                                         'canon_revision', 'other')),
    involved_characters TEXT NOT NULL DEFAULT '[]',            -- JSON 名字数组
    involved_entities   TEXT NOT NULL DEFAULT '[]',            -- JSON 世界实体名数组
    consequences   TEXT    NOT NULL DEFAULT '',
    importance     INTEGER NOT NULL DEFAULT 3,                 -- 1-5
    -- v6 新增 5 列
    world_time     TEXT    NOT NULL DEFAULT '',                -- 该事件发生的世界时间（展示）
    intent         TEXT    NOT NULL DEFAULT '',                -- 行动者的意图（actor 想干什么）
    structured     TEXT    NOT NULL DEFAULT '{}',              -- JSON 完整结构化事件流
    open_threads   TEXT    NOT NULL DEFAULT '[]',              -- JSON 自动涌现的悬置线索
    decision_id    INTEGER,                                    -- 若由决策点产生，关联 decision_points.id
    -- v6.7 新增 5 列
    canonical      INTEGER NOT NULL DEFAULT 1,                 -- 1=正史事件
    immutable      INTEGER NOT NULL DEFAULT 1,                 -- 1=禁止改正文性字段
    branch_id      INTEGER NOT NULL DEFAULT 0,                 -- 由哪条沙盘分支落定而来（0=世界级推进）
    tick           INTEGER NOT NULL DEFAULT 0,                 -- 发生的 absolute_tick
    is_background  INTEGER NOT NULL DEFAULT 0,                 -- 1=pulse() 产出的背景事件
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    deleted_at     TEXT,
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_events_novel
    ON events(novel_id, chapter_ref, id);
CREATE INDEX IF NOT EXISTS idx_events_type
    ON events(novel_id, event_type, importance);

-- 悬置线索（v6 重新定位：自动涌现 > 预埋伏笔）
CREATE TABLE IF NOT EXISTS foreshadowing (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id       INTEGER NOT NULL,
    title          TEXT    NOT NULL DEFAULT '',
    description    TEXT    NOT NULL DEFAULT '',
    planted_chapter INTEGER NOT NULL DEFAULT 0,
    target_chapter  INTEGER NOT NULL DEFAULT 0,
    resolved_chapter INTEGER NOT NULL DEFAULT 0,
    status         TEXT    NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active', 'resolved', 'abandoned', 'dormant')),
    foreshadowing_type TEXT NOT NULL DEFAULT 'plot',
    importance     TEXT    NOT NULL DEFAULT 'normal'
                   CHECK (importance IN ('critical', 'high', 'normal', 'low')),
    -- v6 新增 2 列
    origin         TEXT    NOT NULL DEFAULT 'system'
                   CHECK (origin IN ('system', 'user', 'ai_emerged')),
    tension_level  INTEGER NOT NULL DEFAULT 3,                 -- 1-5 读者/角色被牵引的程度
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_foreshadowing_novel
    ON foreshadowing(novel_id, status, tension_level);

-- 世界内时间线锚点（倒计时/大事件表）
CREATE TABLE IF NOT EXISTS timeline (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id       INTEGER NOT NULL,
    time_anchor    TEXT    NOT NULL DEFAULT '',
    time_span      TEXT    NOT NULL DEFAULT '',
    countdown_name TEXT    NOT NULL DEFAULT '',
    countdown_value TEXT   NOT NULL DEFAULT '',
    description    TEXT    NOT NULL DEFAULT '',
    -- v6.7：新增 expired_auto——由 tick 比较自动置为过期，与人工置的 expired 区分开
    status         TEXT    NOT NULL DEFAULT 'pending'
                   CHECK (status IN ('pending', 'triggered', 'expired',
                                     'expired_auto', 'cancelled')),
    -- v6.7 tick 化：到期判定从"让模型判断哪条约定到期了"（多一次 LLM 调用、
    -- 且同一输入两次结果可能不同）退化成一条 SQL：
    --   WHERE status='pending' AND absolute_tick>0 AND absolute_tick<=?
    -- tick_span 比绝对 tick 更可读（"从埋到爆隔了多久"）。
    -- absolute_tick=0 的锚点（老数据/无法换算）继续走老的模型判断路径（兼容期）。
    absolute_tick  INTEGER NOT NULL DEFAULT 0,
    tick_span      INTEGER NOT NULL DEFAULT 0,
    trigger_chapter INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);

-- ============================================================
-- 第六节 决策点（v6 新增核心）
-- ============================================================

CREATE TABLE IF NOT EXISTS decision_points (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id       INTEGER NOT NULL,
    chapter_ref    INTEGER NOT NULL DEFAULT 0,
    -- 触发类型：四种
    trigger_type   TEXT    NOT NULL DEFAULT 'moral'
                   CHECK (trigger_type IN ('moral', 'cost', 'irreversible', 'user_focus')),
    title          TEXT    NOT NULL DEFAULT '',
    situation      TEXT    NOT NULL DEFAULT '',                -- 局面描述
    stakes         TEXT    NOT NULL DEFAULT '',                -- 代价/风险
    -- 决策主体：角色 id（0 = 群体/世界级决策）
    actor_type     TEXT    NOT NULL DEFAULT 'character'
                   CHECK (actor_type IN ('character', 'world', 'group')),
    actor_id       INTEGER,
    actor_name     TEXT    NOT NULL DEFAULT '',
    -- 选项（JSON 数组）：[{"label":"...","description":"...","consequence_hint":"...","tendency":"..."}]
    options        TEXT    NOT NULL DEFAULT '[]',
    allow_freeform INTEGER NOT NULL DEFAULT 1,
    -- 决策结果
    chosen_option  INTEGER NOT NULL DEFAULT -1,                -- 选项下标，-1 = 未决
    chosen_content TEXT    NOT NULL DEFAULT '',                -- 自由输入内容
    chosen_by      TEXT    NOT NULL DEFAULT ''
                   CHECK (chosen_by IN ('', 'user', 'ai_auto', 'timeout_default')),
    resolved       INTEGER NOT NULL DEFAULT 0,
    resolved_at    TEXT,
    -- 决策造成的状态影响（JSON）
    impact         TEXT    NOT NULL DEFAULT '{}',
    note           TEXT    NOT NULL DEFAULT '',
    -- v6.7：这个决策点是哪条分支 / 哪个沙盘节点落下来的（便于从决策点回溯沙盘）
    branch_id      INTEGER NOT NULL DEFAULT 0,
    node_id        INTEGER,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_decision_points_novel
    ON decision_points(novel_id, resolved, chapter_ref);

-- ============================================================
-- 第六节之二 剧情树（v6.1 新增：推演的分支探索层）
-- ============================================================
-- 定位：events 是"正史"，story_nodes 是"沙盘"。
-- 推演先在树上长，用户在分叉处挑一条继续长，别的分支留着回头再探；
-- 只有用户点「落定成正文」时，选定路径才写进 events（commit）。
-- 好处：探索分支不会污染世界状态，同一局面下可以并行试多条路。
--
-- v6.7 新增 delta 族（方案第五节的 `Base + ΔA1 + ΔA2 + …`）：
--   expand() 里对每条事件调 applier.project_event()（**纯函数，不写库**），
--   与「Base State + 本分支已有 overlay」比对得到净改变，落成 delta。
--   于是沙盘上每个节点都能回答"如果走这条路，世界会变成什么样"，
--   而正史一行未动。delta 是**权威**，branches.overlay_* 只是缓存。

CREATE TABLE IF NOT EXISTS story_nodes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id      INTEGER NOT NULL,
    parent_id     INTEGER,                        -- NULL = 树根
    depth         INTEGER NOT NULL DEFAULT 0,     -- 根为 0
    seq           INTEGER NOT NULL DEFAULT 0,     -- 同级顺序
    kind          TEXT    NOT NULL DEFAULT 'event'
                  CHECK (kind IN ('root', 'event', 'decision', 'option')),
    title         TEXT    NOT NULL DEFAULT '',
    description   TEXT    NOT NULL DEFAULT '',
    event_type    TEXT    NOT NULL DEFAULT 'plot',
    importance    INTEGER NOT NULL DEFAULT 3,
    actor_name    TEXT    NOT NULL DEFAULT '',    -- 决策主体（world = 世界级）
    trigger_type  TEXT    NOT NULL DEFAULT '',
    stakes        TEXT    NOT NULL DEFAULT '',
    world_time    TEXT    NOT NULL DEFAULT '',
    involved_characters TEXT NOT NULL DEFAULT '[]',
    involved_entities   TEXT NOT NULL DEFAULT '[]',
    payload       TEXT    NOT NULL DEFAULT '{}',  -- 完整结构化事件 / 选项对象
    status        TEXT    NOT NULL DEFAULT 'idea'
                  CHECK (status IN ('idea', 'open', 'explored', 'dead',
                                    'committed')),
    branch_hint   TEXT    NOT NULL DEFAULT '',    -- 该分支的走向提示（不剧透）
    -- v6.7 分支与净改变
    branch_id     INTEGER NOT NULL DEFAULT 0,     -- 所属分支（0=主干）
    state_delta   TEXT    NOT NULL DEFAULT '{}',  -- 本节点对世界状态的净改变 JSON
    fact_delta    TEXT    NOT NULL DEFAULT '[]',  -- 本节点新增/推翻的事实 JSON 数组
    knowledge_delta TEXT  NOT NULL DEFAULT '[]',  -- 本节点改变了谁的认知 JSON 数组
    delta_applied INTEGER NOT NULL DEFAULT 0,     -- delta 是否已落进正史（commit 后置 1）
    tick          INTEGER NOT NULL DEFAULT 0,     -- 本节点所在的世界 tick
    decision_ref  INTEGER,                        -- commit 后对应的 decision_points.id
    event_ref     INTEGER,                        -- commit 后对应的 events.id
    note          TEXT    NOT NULL DEFAULT '',
    created_at    TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    deleted_at    TEXT,
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_id) REFERENCES story_nodes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_story_nodes_novel
    ON story_nodes(novel_id, parent_id, seq);
CREATE INDEX IF NOT EXISTS idx_story_nodes_kind
    ON story_nodes(novel_id, kind, status);

-- ============================================================
-- 第七节 章节与生成
-- ============================================================

CREATE TABLE IF NOT EXISTS chapters (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id       INTEGER NOT NULL,
    chapter_number INTEGER NOT NULL,
    title          TEXT    NOT NULL DEFAULT '',
    content        TEXT    NOT NULL DEFAULT '',
    summary        TEXT    NOT NULL DEFAULT '',
    status         TEXT    NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('planned', 'draft', 'published', 'discarded')),
    word_count     INTEGER NOT NULL DEFAULT 0,
    pov_character  TEXT    NOT NULL DEFAULT '',
    viewpoint      TEXT    NOT NULL DEFAULT '',
    location       TEXT    NOT NULL DEFAULT '',
    -- v6 新增：章节形态（不强制每章有钩子）
    chapter_shape  TEXT    NOT NULL DEFAULT 'scene'
                   CHECK (chapter_shape IN ('scene', 'montage', 'interlude',
                                            'aftermath', 'ensemble', 'transition')),
    -- v6 新增：世界时间区间（world_time_* 只做展示）
    world_time_start TEXT  NOT NULL DEFAULT '',
    world_time_end   TEXT  NOT NULL DEFAULT '',
    -- v6.7：可比较的 tick 区间（原 world_time_* 保留给展示）
    tick_start     INTEGER NOT NULL DEFAULT 0,
    tick_end       INTEGER NOT NULL DEFAULT 0,
    -- v6.7 事实检查：未裁决冲突数在列表页出徽标用（避免 N+1 查询）
    fact_conflict_count INTEGER NOT NULL DEFAULT 0,
    verified       INTEGER NOT NULL DEFAULT 0,     -- 1=已通过事实检查
    -- v6 新增：本章生成所依据的事件 id（JSON 数组）
    source_event_ids TEXT  NOT NULL DEFAULT '[]',
    -- v6 新增：章节收尾方式（世界事件收尾 > 主角想通收尾）
    ending_mode    TEXT    NOT NULL DEFAULT '',
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    deleted_at     TEXT,
    UNIQUE (novel_id, chapter_number),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chapters_novel
    ON chapters(novel_id, chapter_number);

CREATE TABLE IF NOT EXISTS chapter_versions (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id       INTEGER NOT NULL,
    chapter_number INTEGER NOT NULL,
    version        INTEGER NOT NULL DEFAULT 1,
    title          TEXT    NOT NULL DEFAULT '',
    content        TEXT    NOT NULL DEFAULT '',
    word_count     INTEGER NOT NULL DEFAULT 0,
    change_note    TEXT    NOT NULL DEFAULT '',
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (novel_id, chapter_number, version),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);

-- 章节选角（v6 新增）：本章有哪些角色出场、各自托管模式
CREATE TABLE IF NOT EXISTS chapter_casting (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id       INTEGER NOT NULL,
    chapter_number INTEGER NOT NULL,
    character_id   INTEGER,
    character_name TEXT    NOT NULL DEFAULT '',
    role_in_chapter TEXT   NOT NULL DEFAULT 'supporting'
                   CHECK (role_in_chapter IN ('pov', 'main', 'supporting',
                                              'cameo', 'mentioned')),
    control_mode   TEXT    NOT NULL DEFAULT 'ai'
                   CHECK (control_mode IN ('ai', 'user', 'auto_delegate')),
    is_new_character INTEGER NOT NULL DEFAULT 0,
    note           TEXT    NOT NULL DEFAULT '',
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE,
    FOREIGN KEY (character_id) REFERENCES characters(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_chapter_casting_novel
    ON chapter_casting(novel_id, chapter_number);

-- 作者声音档案（v6 新增）：写作者的癖好，用于让 AI 文本更像"这个人写的"
CREATE TABLE IF NOT EXISTS author_voice (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id       INTEGER NOT NULL,
    category       TEXT    NOT NULL DEFAULT 'syntax'
                   CHECK (category IN ('sensory', 'syntax', 'dialogue',
                                       'taboo', 'motif')),
    -- sensory=感官偏好 / syntax=句法习惯 / dialogue=对白习惯 /
    -- taboo=绝不写的 / motif=反复出现的意象
    content        TEXT    NOT NULL,
    example        TEXT    NOT NULL DEFAULT '',                -- 正例/反例
    weight         INTEGER NOT NULL DEFAULT 3,                 -- 1-5 重要度
    is_active      INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_author_voice_novel
    ON author_voice(novel_id, category, is_active);

-- ============================================================
-- 第八节 完结系统（v6 新增）
-- ============================================================

CREATE TABLE IF NOT EXISTS world_goals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id       INTEGER NOT NULL,
    goal_type      TEXT    NOT NULL DEFAULT 'world'
                   CHECK (goal_type IN ('world', 'character', 'ai_judged')),
    title          TEXT    NOT NULL DEFAULT '',
    description    TEXT    NOT NULL DEFAULT '',
    -- 结构化条件表达式，例：world.异常实体.剩余数量 == 0
    condition_expr TEXT    NOT NULL DEFAULT '',
    character_id   INTEGER,
    progress       INTEGER NOT NULL DEFAULT 0,                 -- 0-100
    is_primary     INTEGER NOT NULL DEFAULT 0,
    status         TEXT    NOT NULL DEFAULT 'active'
                   CHECK (status IN ('active', 'achieved', 'failed', 'abandoned')),
    achieved_chapter INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE,
    FOREIGN KEY (character_id) REFERENCES characters(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_world_goals_novel
    ON world_goals(novel_id, status, goal_type);

-- ============================================================
-- 第九节 事件种子库（原 case_library 升级）
-- ============================================================

CREATE TABLE IF NOT EXISTS event_seeds (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id       INTEGER NOT NULL,
    seed_type      TEXT    NOT NULL DEFAULT 'conflict'
                   CHECK (seed_type IN ('conflict', 'mystery', 'opportunity',
                                        'disaster', 'encounter', 'revelation', 'other')),
    name           TEXT    NOT NULL DEFAULT '',
    description    TEXT    NOT NULL DEFAULT '',
    -- 触发条件（JSON）：{"min_chapter":10,"world_state":{...},"characters":[...]}
    trigger_condition TEXT NOT NULL DEFAULT '{}',
    -- 事件骨架（JSON）：可含 actor/action/intent/result 模板
    skeleton       TEXT    NOT NULL DEFAULT '{}',
    tags           TEXT    NOT NULL DEFAULT '[]',
    intensity      INTEGER NOT NULL DEFAULT 3,
    used_count     INTEGER NOT NULL DEFAULT 0,
    last_used_chapter INTEGER NOT NULL DEFAULT 0,
    is_active      INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_event_seeds_novel
    ON event_seeds(novel_id, seed_type, is_active);

-- ============================================================
-- 第十节 提示词模板
-- ============================================================

CREATE TABLE IF NOT EXISTS prompts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id       INTEGER NOT NULL DEFAULT 0,                 -- 0 = 全局模板
    name           TEXT    NOT NULL,
    category       TEXT    NOT NULL DEFAULT 'general',
    content        TEXT    NOT NULL DEFAULT '',
    version        INTEGER NOT NULL DEFAULT 1,
    is_active      INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (novel_id, name, version)
);

-- ============================================================
-- 第十一节 AI 接入配置（v6 新增）
-- ============================================================

CREATE TABLE IF NOT EXISTS providers (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    name           TEXT    NOT NULL,                           -- 展示名
    kind           TEXT    NOT NULL DEFAULT 'openai'
                   CHECK (kind IN ('openai', 'anthropic', 'gemini', 'ollama')),
    base_url       TEXT    NOT NULL DEFAULT '',
    api_key_ref    TEXT    NOT NULL DEFAULT '',                -- 指向 secrets.json 的键名，不存明文
    preset_key     TEXT    NOT NULL DEFAULT '',                -- 厂商预设键（deepseek/qwen/…），空=自定义
    models         TEXT    NOT NULL DEFAULT '[]',              -- 可选模型清单 JSON 数组（测试与下拉共用）
    is_active      INTEGER NOT NULL DEFAULT 1,
    note           TEXT    NOT NULL DEFAULT '',
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (name)
);

-- 模型档位预设：把"任务"映射到"模型"
-- 约定：novel_id = 0 表示"全局默认档位"，不做外键约束（否则无法插入哨兵行）
CREATE TABLE IF NOT EXISTS model_presets (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id       INTEGER NOT NULL DEFAULT 0,
    slot           TEXT    NOT NULL,
    -- slot: world_sim / character_decide / option_gen / prose_gen
    --       / state_extract / completion_judge / summarize
    provider_id    INTEGER,
    model          TEXT    NOT NULL DEFAULT '',
    temperature    REAL    NOT NULL DEFAULT 0.8,
    max_tokens     INTEGER NOT NULL DEFAULT 4096,
    extra          TEXT    NOT NULL DEFAULT '{}',
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (novel_id, slot),
    FOREIGN KEY (provider_id) REFERENCES providers(id) ON DELETE SET NULL
);

-- 调用用量记录（成本控制）
CREATE TABLE IF NOT EXISTS llm_usage (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id       INTEGER NOT NULL DEFAULT 0,
    slot           TEXT    NOT NULL DEFAULT '',
    provider       TEXT    NOT NULL DEFAULT '',
    model          TEXT    NOT NULL DEFAULT '',
    prompt_tokens  INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens   INTEGER NOT NULL DEFAULT 0,
    latency_ms     INTEGER NOT NULL DEFAULT 0,
    success        INTEGER NOT NULL DEFAULT 1,
    error          TEXT    NOT NULL DEFAULT '',
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime'))
);

CREATE INDEX IF NOT EXISTS idx_llm_usage_novel
    ON llm_usage(novel_id, created_at);

-- ============================================================
-- 第十二节 全文检索（FTS5 独立副本表）
-- 注意：本库曾触发 malformed，禁止改回 external content 模式
-- ============================================================

CREATE VIRTUAL TABLE IF NOT EXISTS chapters_fts USING fts5(
    chapter_ref UNINDEXED,
    novel_id UNINDEXED,
    title,
    content,
    tokenize='trigram'
);

CREATE TRIGGER IF NOT EXISTS chapters_fts_ai AFTER INSERT ON chapters BEGIN
    INSERT INTO chapters_fts(chapter_ref, novel_id, title, content)
    VALUES (new.id, new.novel_id, new.title, new.content);
END;

CREATE TRIGGER IF NOT EXISTS chapters_fts_ad AFTER DELETE ON chapters BEGIN
    DELETE FROM chapters_fts WHERE chapter_ref = old.id;
END;

CREATE TRIGGER IF NOT EXISTS chapters_fts_au AFTER UPDATE ON chapters BEGIN
    DELETE FROM chapters_fts WHERE chapter_ref = old.id;
    INSERT INTO chapters_fts(chapter_ref, novel_id, title, content)
    VALUES (new.id, new.novel_id, new.title, new.content);
END;

-- ============================================================
-- 第十三节 视图（只读便捷查询）
-- ============================================================

CREATE VIEW IF NOT EXISTS v_novel_overview AS
SELECT n.id                       AS novel_id,
       n.title,
       n.status,
       n.genre,
       n.initial_tension,
       n.decision_mode,
       n.completion_mode,
       n.current_chapter,
       (SELECT COUNT(*) FROM chapters c
         WHERE c.novel_id = n.id AND c.deleted_at IS NULL)              AS chapter_count,
       (SELECT COALESCE(SUM(c.word_count), 0) FROM chapters c
         WHERE c.novel_id = n.id AND c.deleted_at IS NULL)              AS total_words,
       (SELECT COUNT(*) FROM characters ch
         WHERE ch.novel_id = n.id AND ch.deleted_at IS NULL)            AS character_count,
       (SELECT COUNT(*) FROM world_entities e
         WHERE e.novel_id = n.id AND e.deleted_at IS NULL)              AS entity_count,
       (SELECT COUNT(*) FROM events ev
         WHERE ev.novel_id = n.id AND ev.deleted_at IS NULL)            AS event_count,
       (SELECT COUNT(*) FROM foreshadowing f
         WHERE f.novel_id = n.id AND f.status = 'active')               AS open_thread_count,
       (SELECT COUNT(*) FROM decision_points d
         WHERE d.novel_id = n.id AND d.resolved = 0)                    AS pending_decision_count,
       n.updated_at
  FROM novels n
 WHERE n.deleted_at IS NULL;

-- 待决策视图
CREATE VIEW IF NOT EXISTS v_pending_decisions AS
SELECT d.id,
       d.novel_id,
       d.chapter_ref,
       d.trigger_type,
       d.title,
       d.actor_name,
       d.created_at,
       n.title AS novel_title
  FROM decision_points d
  JOIN novels n ON n.id = d.novel_id
 WHERE d.resolved = 0
 ORDER BY d.id ASC;

-- 活跃线索视图（按张力降序）
CREATE VIEW IF NOT EXISTS v_active_threads AS
SELECT f.id,
       f.novel_id,
       f.title,
       f.description,
       f.planted_chapter,
       f.target_chapter,
       f.tension_level,
       f.importance,
       f.origin,
       (f.target_chapter - COALESCE(
           (SELECT current_chapter FROM novels WHERE id = f.novel_id), 0)
       ) AS chapters_left
  FROM foreshadowing f
 WHERE f.status = 'active'
 ORDER BY f.tension_level DESC, f.importance DESC;

-- ============================================================
-- 第十四节 正史账本与认知层（v6.7 新增核心）
-- ============================================================
-- 五条硬约束的落地位置（与 docs/v6.7_数据库与代码改造详细设计 §0.6 一一对应）：
--   ① 世界真相(facts) 与 角色认知(character_knowledge) **物理分离** —— 不靠 confidence 区分
--   ② 先分类再比对：fact_candidates 接住"定不了性"的条目（"无冲突"不等于"是真的"）
--   ③ event_type 用 canon_revision，不复用 reveal
--   ④ world_state 写权限在代码层收口（见 db.set_state_value 的 writer 白名单）
--   ⑤ world_snapshots 只建表、只定接口，第一版不写实现

-- ------------------------------------------------------------
-- 14.1 facts —— Canonical Fact Ledger（本次改造的地基）
-- 与 world_state 的分工：world_state 只有"此刻为真"的一格，
-- 且没有主语、没有时效、没有溯源、没有历史；facts 补的正是这四样。
-- 演进示例：老周 存活 → superseded_by=88（第8章爆炸）→ 老周 已死亡 → superseded_by=214
--          → 老周 重伤未死（"读者以为他死了、第20章发现没死"因此可查）
CREATE TABLE IF NOT EXISTS facts (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id         INTEGER NOT NULL,

    -- ① 断言的主语
    subject_type     TEXT    NOT NULL DEFAULT 'world'
                     CHECK (subject_type IN ('character', 'entity', 'world')),
    subject_id       INTEGER,                          -- character/entity 的 id；world 为 NULL
    subject_name     TEXT    NOT NULL DEFAULT '',      -- 冗余，展示与提示词用（角色删档也读得出）

    -- ② 断言本身（主-谓-宾三元组，而不是自由文本）
    predicate        TEXT    NOT NULL DEFAULT '',      -- 存活 / 位于 / 持有 / 已摧毁 / 阵营 / 金额 …
    object_text      TEXT    NOT NULL DEFAULT '',      -- 宾语/值（"封锁库" / "0" / "7"）
    object_type      TEXT    NOT NULL DEFAULT 'text'
                     CHECK (object_type IN ('text', 'int', 'float', 'bool', 'json', 'ref')),
    object_ref_id    INTEGER,                          -- object_type='ref' 时指向 entities/characters

    -- ③ 分类（决定投影到 world_state 的哪一格 / 展示分组）
    fact_type        TEXT    NOT NULL DEFAULT 'other'
                     CHECK (fact_type IN ('identity', 'status', 'location', 'possession',
                                          'relationship', 'ability', 'injury', 'resource',
                                          'world_rule', 'outcome', 'other')),

    -- ④ 有效期：事实从哪个 tick 起成立，到哪个 tick 失效（NULL=仍有效）
    valid_from_tick  INTEGER NOT NULL DEFAULT 0,
    valid_to_tick    INTEGER,

    -- ⑤ 溯源
    source_event_id  INTEGER,
    source_chapter   INTEGER NOT NULL DEFAULT 0,
    source_kind      TEXT    NOT NULL DEFAULT 'sim'
                     CHECK (source_kind IN ('sim', 'prose', 'user', 'init')),

    -- ⑥ 状态机：**只 supersede，从不 DELETE**
    status           TEXT    NOT NULL DEFAULT 'active'
                     CHECK (status IN ('active', 'superseded', 'revoked', 'pending')),
    superseded_by    INTEGER,                          -- 被哪条新 fact 取代（自引用）
    supersede_reason TEXT    NOT NULL DEFAULT '',

    -- ⑦ 真值等级 —— **只接受世界真相三态**（约束 1 的执行点）
    --    角色的 believe/suspect/assume/memory/dream 一律不得出现在本表，
    --    走 character_knowledge.know_type；抽出来但定不了性的走 fact_candidates。
    --    CHECK 只放三值这件事本身就是约束：写入方拿不到第四个选项。
    fact_status      TEXT    NOT NULL DEFAULT 'canonical'
                     CHECK (fact_status IN ('canonical', 'derived', 'provisional')),

    -- ⑧ 溯源确信度 1-5，与 fact_status **正交**
    --    （derived + 5 = 高可信推导；canonical + 3 = 作者口头确认但没写进正文）
    confidence       INTEGER NOT NULL DEFAULT 5
                     CHECK (confidence BETWEEN 1 AND 5),

    -- ⑨ 这一条是谁判成 world 的（约束 2 的可追溯性）
    classified_by    TEXT    NOT NULL DEFAULT 'sim'
                     CHECK (classified_by IN ('sim', 'python', 'llm', 'user')),

    note             TEXT    NOT NULL DEFAULT '',
    created_at       TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE,
    FOREIGN KEY (source_event_id) REFERENCES events(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_facts_novel
    ON facts(novel_id, status, fact_type);
CREATE INDEX IF NOT EXISTS idx_facts_subject
    ON facts(novel_id, subject_type, subject_id, predicate);
-- 「同一主体+同一谓词，同时只能有一条 active 断言」。
-- 注意：SQLite 的 UNIQUE 把 NULL 视作彼此不同，所以 world 级事实（subject_id IS NULL）
-- 不受这条约束，靠 facts.add_fact() 在应用层收口（先 supersede 再 insert）。
CREATE UNIQUE INDEX IF NOT EXISTS uq_facts_active
    ON facts(novel_id, subject_type, subject_id, predicate)
    WHERE status='active' AND subject_id IS NOT NULL;

-- ------------------------------------------------------------
-- 14.2 character_knowledge —— 知识状态（约束 1 的另一半）
-- facts 回答「世界是什么样」；本表回答「**谁**以为世界是什么样」。
-- 二者**可以互相矛盾**——is_false=1 就是标记这种矛盾的地方，不是错误，是剧情。
-- 同一件事，李四 SUSPECT、王五 BELIEVE 相反、赵六 KNOW：
-- 三个角色看到的是完全不同的世界，所以第三页喂给模型的必须是「POV 那个世界」。
CREATE TABLE IF NOT EXISTS character_knowledge (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id          INTEGER NOT NULL,
    character_id      INTEGER NOT NULL,               -- 谁知道

    know_type         TEXT    NOT NULL DEFAULT 'know'
                      --   know    = 亲眼/亲耳确认
                      --   believe = 相信但未证实
                      --   suspect = 怀疑
                      --   assume  = 为行动暂时假设
                      --   memory  = 回忆（▲ 可能记错，不得当作世界真相）
                      --   dream   = 梦境（▲ 不是发生过的事，绝不可当真相）
                      CHECK (know_type IN ('know', 'believe', 'suspect', 'assume',
                                           'memory', 'dream')),
    about_type        TEXT    NOT NULL DEFAULT 'fact'
                      CHECK (about_type IN ('character', 'entity', 'event', 'location',
                                            'fact', 'world')),
    about_id          INTEGER,
    about_text        TEXT    NOT NULL DEFAULT '',    -- 认知对象（名字 / 一句话）

    content           TEXT    NOT NULL DEFAULT '',    -- 他知道/相信的内容
    source            TEXT    NOT NULL DEFAULT 'witnessed'
                      CHECK (source IN ('witnessed', 'told', 'inferred', 'assumed',
                                        'read', 'public', 'dream', 'recalled')),
    confidence        INTEGER NOT NULL DEFAULT 3,     -- 1-5

    -- 错的认知与正史的对照（问"谁把假消息当真了"就靠这两列）
    is_false          INTEGER NOT NULL DEFAULT 0,
    contradicts_fact_id INTEGER,

    learned_event_id  INTEGER,
    learned_chapter   INTEGER NOT NULL DEFAULT 0,
    learned_tick      INTEGER NOT NULL DEFAULT 0,

    status            TEXT    NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active', 'confirmed', 'refuted', 'revoked')),
    superseded_by     INTEGER,
    created_at        TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE,
    FOREIGN KEY (character_id) REFERENCES characters(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_ck_char
    ON character_knowledge(novel_id, character_id, status);
CREATE INDEX IF NOT EXISTS idx_ck_about
    ON character_knowledge(novel_id, about_type, about_id);

-- ------------------------------------------------------------
-- 14.3 branches —— 沙盘分支
-- 为什么既加 story_nodes.branch_id 又要这张表：
--   「这条分支上有哪些节点」→ WHERE branch_id=?
--   「这条分支累计改变了什么」→ overlay_* 一次查询，不用沿 parent_id 走一遍再合并
--   「从哪儿岔开」→ base_event_id / base_tick / parent_branch
--   「还活着吗」→ status（prune/reset/commit 时置 discarded）
-- parent_branch 支持"沙盘套沙盘"。overlay_* 是缓存，权威是节点上的 *_delta。
CREATE TABLE IF NOT EXISTS branches (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id          INTEGER NOT NULL,
    root_node_id      INTEGER,                        -- 本分支的入口节点（选中 option 节点）
    parent_branch     INTEGER NOT NULL DEFAULT 0,     -- 从哪个分支岔开（0 = 主干）
    base_event_id     INTEGER NOT NULL DEFAULT 0,     -- 从正史的第几个事件之后分叉
    base_tick         INTEGER NOT NULL DEFAULT 0,     -- 分叉点的世界时间

    label             TEXT    NOT NULL DEFAULT '',    -- 展示名（取 option 标题）

    overlay_state     TEXT    NOT NULL DEFAULT '{}',
    overlay_facts     TEXT    NOT NULL DEFAULT '[]',
    overlay_knowledge TEXT    NOT NULL DEFAULT '[]',
    node_count        INTEGER NOT NULL DEFAULT 0,

    status            TEXT    NOT NULL DEFAULT 'open'
                      CHECK (status IN ('open', 'active', 'committed', 'discarded')),
    created_at        TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    discarded_at      TEXT,
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE,
    FOREIGN KEY (root_node_id) REFERENCES story_nodes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_branches_novel
    ON branches(novel_id, status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_branches_root
    ON branches(novel_id, root_node_id) WHERE root_node_id IS NOT NULL;

-- ------------------------------------------------------------
-- 14.4 world_rules —— 世界硬约束
-- 现状问题：guard.build_state_constraints() 生成的是一坨自由文本给模型看，
-- 模型违反了也没人知道。本表把"用户明确说过不能违反的事"变成**可校验的行**。
CREATE TABLE IF NOT EXISTS world_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id        INTEGER NOT NULL,
    rule_key        TEXT    NOT NULL DEFAULT '',      -- 短标识，用于去重与引用
    content         TEXT    NOT NULL DEFAULT '',      -- 规则原文："死者不能复活"
    scope           TEXT    NOT NULL DEFAULT 'global'
                    CHECK (scope IN ('global', 'character', 'entity', 'location', 'faction')),
    subject_name    TEXT    NOT NULL DEFAULT '',      -- scope 非 global 时指谁
    is_hard         INTEGER NOT NULL DEFAULT 1,       -- 1=违反即拦 0=违反只警告
    severity        TEXT    NOT NULL DEFAULT 'error'
                    CHECK (severity IN ('error', 'warning')),
    check_hint      TEXT    NOT NULL DEFAULT '',      -- 给校验器看的判据（比 content 更具体）
    source          TEXT    NOT NULL DEFAULT 'user'
                    CHECK (source IN ('user', 'llm_extracted', 'system')),
    is_active       INTEGER NOT NULL DEFAULT 1,
    created_chapter INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_world_rules_key
    ON world_rules(novel_id, rule_key) WHERE rule_key <> '';

-- ------------------------------------------------------------
-- 14.5 chapter_event_links —— 章节↔事件映射
-- chapters.source_event_ids 是 JSON 数组，**没法索引、没法反查、没法约束**。
-- 本表补三件事：① 反查"这个正史事件被哪一章写了"（防止同一事件被两章重复写）
--             ② 记录 mention（只是被提到）与 conflict（章节与正史冲突）
--             ③ release_chapter()（退回成素材）时能精确解绑
CREATE TABLE IF NOT EXISTS chapter_event_links (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id       INTEGER NOT NULL,
    chapter_number INTEGER NOT NULL,
    event_id       INTEGER NOT NULL,
    seq            INTEGER NOT NULL DEFAULT 0,
    link_type      TEXT    NOT NULL DEFAULT 'source'
                   CHECK (link_type IN ('source', 'mention', 'conflict')),
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (novel_id, chapter_number, event_id, link_type),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE,
    FOREIGN KEY (event_id) REFERENCES events(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_cel_event
    ON chapter_event_links(novel_id, event_id);

-- ------------------------------------------------------------
-- 14.6 fact_conflicts —— 冲突队列（正文与正史打架时，正文永远不能直接覆盖）
CREATE TABLE IF NOT EXISTS fact_conflicts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id        INTEGER NOT NULL,
    chapter_number  INTEGER NOT NULL,
    conflict_type   TEXT    NOT NULL DEFAULT 'prose_invents_fact'
                    CHECK (conflict_type IN ('prose_contradicts_fact',
                                             'prose_invents_fact',
                                             'fact_missing_in_prose',
                                             'knowledge_leak')),
    fact_id         INTEGER,
    subject_name    TEXT    NOT NULL DEFAULT '',
    prose_quote     TEXT    NOT NULL DEFAULT '',      -- 正文原句
    fact_statement  TEXT    NOT NULL DEFAULT '',      -- 与之冲突的正史陈述
    detail          TEXT    NOT NULL DEFAULT '',
    severity        TEXT    NOT NULL DEFAULT 'error'
                    CHECK (severity IN ('error', 'warning')),
    detected_by     TEXT    NOT NULL DEFAULT 'python'
                    CHECK (detected_by IN ('python', 'llm')),
    status          TEXT    NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'keep_prose', 'keep_canon', 'ignored')),
    resolution_note TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    resolved_at     TEXT,
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE,
    FOREIGN KEY (fact_id) REFERENCES facts(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_fact_conflicts_open
    ON fact_conflicts(novel_id, status, chapter_number);

-- ------------------------------------------------------------
-- 14.7 fact_sources —— 事实来源
-- 一条事实可以由多个来源共同支撑（推演说 + 正文也写了 + 用户手工确认）。
-- facts.source_event_id 只记**最先生效**的那个来源，去向追溯走这张表。
CREATE TABLE IF NOT EXISTS fact_sources (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    fact_id        INTEGER NOT NULL,
    event_id       INTEGER NOT NULL DEFAULT 0,
    chapter_number INTEGER NOT NULL DEFAULT 0,
    kind           TEXT    NOT NULL DEFAULT 'sim'
                   CHECK (kind IN ('sim', 'prose', 'user', 'init')),
    quote          TEXT    NOT NULL DEFAULT '',
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (fact_id, kind, event_id),
    FOREIGN KEY (fact_id) REFERENCES facts(id) ON DELETE CASCADE
);

-- ------------------------------------------------------------
-- 14.8 fact_candidates —— 待分类/待裁决事实缓冲区（约束 2 的落点）
-- 解决的是这条漏洞：**「无冲突」不等于「是真的」**，它只说明"没跟已知的打架"。
-- 三类正文都能通过"无冲突"检查，但它们都不是世界真相：
--   「李四突然想起，三年前自己杀过一个人」→ extraction 出"李四三年前杀过人"→ 正史没记过
--   「李四梦见自己站在仓库门口」          → 梦里的事没发生过
--   「李四说：'张三是卧底'」              → 转述≠真相（他可能在撒谎）
CREATE TABLE IF NOT EXISTS fact_candidates (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id         INTEGER NOT NULL,

    -- 来源
    source_kind      TEXT    NOT NULL DEFAULT 'prose'
                     CHECK (source_kind IN ('prose', 'sim', 'user')),
    source_event_id  INTEGER,
    source_chapter   INTEGER NOT NULL DEFAULT 0,
    quote            TEXT    NOT NULL DEFAULT '',     -- 原文原句（裁决时最重要的一列）
    provenance       TEXT    NOT NULL DEFAULT '',     -- 叙述语境："角色回忆"/"对话中声称"/"梦境"

    -- 候选内容（结构同 facts 的主谓宾）
    subject_type     TEXT    NOT NULL DEFAULT 'character'
                     CHECK (subject_type IN ('character', 'entity', 'world')),
    subject_id       INTEGER,
    subject_name     TEXT    NOT NULL DEFAULT '',
    predicate        TEXT    NOT NULL DEFAULT '',
    object_text      TEXT    NOT NULL DEFAULT '',
    fact_type        TEXT    NOT NULL DEFAULT 'other',

    -- ▲ 分类结果（约束 2 的核心）
    classification   TEXT    NOT NULL DEFAULT 'unverified'
                     CHECK (classification IN ('world',          -- 世界真相候选
                                               'cognition',      -- 角色认知（谁的信见 target_character_id）
                                               'unverified',     -- 定不了性
                                               'dream',          -- 梦境
                                               'memory',         -- 回忆/不可靠叙述
                                               'author_claim')), -- 角色在对话里声称（可能是撒谎）
    classified_by    TEXT    NOT NULL DEFAULT 'python'
                     CHECK (classified_by IN ('python', 'llm', 'user')),
    classify_reason  TEXT    NOT NULL DEFAULT '',     -- 为什么这么判（UI 要显示给用户看）
    confidence       INTEGER NOT NULL DEFAULT 3
                     CHECK (confidence BETWEEN 1 AND 5),

    -- cognition 类专用：谁的信
    target_character_id INTEGER,

    status           TEXT    NOT NULL DEFAULT 'pending'
                     CHECK (status IN ('pending',      -- 等分类/等裁决
                                       'accepted',     -- 已接受 → 已写入 facts 或 character_knowledge
                                       'rejected',     -- 已否决（作者说这不是真的）
                                       'converted')),  -- 已转成 canon_revision
    resolved_fact_id INTEGER,                         -- status='accepted' 时写入的 fact id
    resolution_note  TEXT    NOT NULL DEFAULT '',
    created_at       TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    resolved_at      TEXT,
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE,
    FOREIGN KEY (source_event_id) REFERENCES events(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_fc_pending
    ON fact_candidates(novel_id, status, classification);
CREATE INDEX IF NOT EXISTS idx_fc_chapter
    ON fact_candidates(novel_id, source_chapter);

-- ------------------------------------------------------------
-- 14.9 world_snapshots —— 快照 / 事件重放预留（约束 5）
-- ⚠ P1：本表第一版**不写入任何数据**，仅预留接口。
--    建立它的唯一目的是让 facts.replay_from() / load_world_at() 的签名
--    从第一天起就是"可能快照加速"的形状，避免日后改所有调用方。
--    规模依据：1000 章 / 几万个事件 / 几十万个 character_knowledge。
--    若每次从事件 1 replay，"改第 350 章"的成本随篇幅线性增长；
--    有快照后只需 replay 最近 ≤500 个事件。
--    失效规则：supersede 只影响 at_tick 之后的快照；用户手工订正历史事实时，
--    把 at_tick >= 修正点 的快照批量置 is_valid=0。
CREATE TABLE IF NOT EXISTS world_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    novel_id       INTEGER NOT NULL,

    upto_event_id  INTEGER NOT NULL DEFAULT 0,     -- 包含此事件
    at_tick        INTEGER NOT NULL DEFAULT 0,
    event_count    INTEGER NOT NULL DEFAULT 0,     -- 覆盖了多少个 canonical 事件

    payload        TEXT    NOT NULL DEFAULT '{}',  -- {"facts":[...], "state":{...},
                                                   --  "knowledge":[...], "clock":{...}}
    payload_bytes  INTEGER NOT NULL DEFAULT 0,
    payload_hash   TEXT    NOT NULL DEFAULT '',    -- 校验用（重放结果与快照对不上要能发现）

    is_valid       INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now', 'localtime')),
    UNIQUE (novel_id, upto_event_id),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_snap_novel
    ON world_snapshots(novel_id, at_tick DESC);
