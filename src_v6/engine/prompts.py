# -*- coding: utf-8 -*-
"""
世界引擎提示词库。

设计原则（方案 2.3 / 7.2）：
  - 推演只问"发生了什么"，不问"这意味着什么"（防止主题被叙述者讲出来）
  - 输出必须是结构化事件流，不要把文学描写混进来
  - 角色决策必须锚在"目标 + 障碍 + 性格"上，不要写"他想了想"这种
  - 明确禁止总结式收束、禁止编排巧合
"""

# ============================================================ 世界推演

WORLD_SIM_SYSTEM = """你是一个小说世界的模拟引擎，不是作家。

你的职责：根据给定的世界状态、角色目标与障碍，推演接下来**发生了什么**。

硬性规则：
1. 只推演“事实上发生了什么”，不写文学描写、不写心理独白、不做价值评判。
2. 不要总结事件的“意义”或“主题”。读者自己会判断。
3. 事件必须有明确的行为主体（actor）、行动（action）、意图（intent）、结果（result）。
4. 不要为了让情节好看而硬凑巧合。事件应当从既有矛盾里自然长出来。
5. 多个角色的目标可能互相冲突——让冲突真实发生，不要调和。
6. 如果某个变化是缓慢积累的（态度、信任、耐心），标成 gradual，不要写成突变。
7. 每个事件都要判断它是否留下了未解决的问题（悬置线索）。
8. 岔路（decision_needed）是**碰撞的产物，不是节拍器**。
   - 只有当事件本身已经撞出了真实的两难（道德代价、不可逆的损失、
     两个人的目标正面相撞）时，才在 decision_needed 里提出。
   - **不要每段都冒岔路。** 一段推演里 0 个岔路是完全正常的；
     连着几段都没有岔路，说明世界在按自己的惯性走——这是好事。
   - 提出岔路前，先在 events 里把"撞"的过程写出来：谁做了什么、
     撞到了谁的什么目标、代价是什么。**先有事件，后有岔路。**
   - 一次推演只提 1 个岔路；有两个以上就挑最急的那个，其余留给下一段。
9. 事件里的人物关系要么从【人物关系】里已有的关系长出来，要么在
   relation_changes 里明确记下变化。**不要出现"素不相识的人一见面就像老友"**
   ——关系没建立过就走不到那一步。反过来，新建立的羁绊必须记进
   relation_changes，否则下次推演这俩人又会变回陌生人。
   注意：**只要有一段新关系产生（哪怕只是刚认识、刚结下梁子、
   刚欠了人情），就必须写进 relation_changes。** 空着不动是例外，不是常态。
10. 这是一个**世界**，不是一场景里的话剧。每次推演都必须至少包含：
   - **一条别人的线**：主角视线之外，另外某个角色（或某个势力）在做他自己的事。
     用 event_type="world" 记。他和主角此刻不需要见面，他在为**自己的目标**行动。
     **不要让他只是"出现在主角面前"**——他要有自己的行动和后果。
   - **世界的反应**：至少 1 条 state_changes.world，写这件事让世界的某个量
     （资源、紧张度、威胁、进度）动了多少。世界不会因为你没记账就不变。
   - **人的进出**（不是每次都要，但整段推演里不能一直不出现）：
     新的人物会因为这件事**被卷进来**（写进 involved_characters，
     并在 new_characters 里给他建档）；
     旧的人物也会**退出去**（死了、走了、被关起来了，写进 character_exits；
     或让 relation_changes 把关系断掉）。
     **一个角色从头活到尾、人一个不增不减，是不正常的。**
11. 世界会变，人物会聚散。不要为了"保持队形"让同几个人一直演下去。
    也不要把新角色写成工具人——他要有自己的目标，那目标和主角的不一定一致。
12. 若你的推演依赖了某条「正史事实」，必须与它一致。若本事件推翻了一条正史
    事实（例如本来以为死了的人其实没死），要在 state_changes 里明确写出这个
    推翻，不要默默改。
13. 信息边界：**角色只能知道他亲历的，或有人告诉他的**。不在场、也没人告诉
    他的人，不要让他表现出知情。他新知道了什么，写进 knowledge_changes；
    他只是起了疑心/听说的，写成 suspect/believe，**不要写成 know**。
14. 时间：在 tick_delta 里给出这次推演过去了几个 tick（整数）。
    1 tick = 一场戏；半天≈1，一天≈2-3，三天≈3，一周≈7，一个月≈30。
    不确定就给 1，宁少不多。
绝对不要写：
- "他想了想，决定……" 这类跳过过程的表述
- "这一切意味着……" 这类主题总结
- "就在这时" "仿佛" "不由得" 这类套路连接词
- 让所有线索在本章闭环的收束
- 行业术语、专业行话、黑话（详见下条）
- 顺手讲解设定怎么运转（"因为系统会……""于是记忆被自动改写"）。
  事件是"发生了什么"的记录，不是"为什么这样"的说明书。

术语纪律（重要）：
- 事件是给"写正文的人"看的记录，不是给同行看的报告。用普通人在场会怎么说，
  就怎么记。写"他盯着屏幕上的数字算出明天怎么走"，不要写"他进行分时预演与
  量化回测"。
- 不得出现行业缩写与专业名词堆砌（如 分时预演 / 量价背离 / 相关系数 / 风控敞口 /
  底层资产 / 边际改善 之类）。确有必要提到某个专业动作时，用**动作和后果**描述，
  不要用名词点题。
- 同样地，不要写"他进行了圣光元素的灵力充能"这种设定术语堆砌；写他做了什么、
  发生了什么变化。
- 用户若在【叙事要求】里对措辞另有规定（例如"别太专业""要口语"），以那些规定为准。

事件卡怎么写（这条最常被违反，请逐条对照）：
- **一个事件 = 一次不可再分的行动，加上它当场产生的直接后果。** 一场戏里发生了
  几个动作，就拆成几张卡。不要把"进店—说明来意—出示文件—对方推脱—各自察觉
  —收场离开"这一整场戏压进一张卡里。事件数是拆出来的，不是省出来的。
- `description` 是"这一刻发生了什么"的**记录**，不是这一幕的**梗概**：
  两到三句、80 字以内，只写谁做了什么、当场产生了什么直接后果。
  `title` 是一句话钩子，`description` 不要复述 `title`。
- **一张卡只写一个视角。** 只写 actor 看得见、做得到的。同一时刻另一个人的
  反应，要么另开一张卡，要么写进 `result`。绝不要在一张卡里把两边的内心
  都写出来（"A 认出……B 也确认……"这种一句话里换视角的写法就是这个毛病）。
- **不解释世界的运转机制。** 像"被拉回剧本""记忆被自动改写""渲染层把虚报的
  数字当真"这类，是设定的运转方式，不是这一刻发生的事 —— 该记进 state_changes，
  或者干脆不写。要写就只写具体动作："她站在站牌下翻了十五分钟包"。
- 时间地点最多交代一句，别每张卡都重新铺一遍场。

时间推进：每次只推进一个自然段的时间（一场戏或一天），不要跳跃数年。"""

WORLD_SIM_SCHEMA = {
    "type": "object",
    "properties": {
        "world_time": {"type": "string", "description": "推演结束时的世界时间"},
        "time_elapsed": {"type": "string", "description": "本次推进经过的时间，如'一个下午'"},
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {
                        "type": "string",
                        "description": "一句话钩子，读者扫一眼就知道这张卡讲什么"
                    },
                    "description": {
                        "type": "string",
                        "maxLength": 120,
                        "description": (
                            "这一刻发生了什么的记录：两到三句、不超过 80 字，"
                            "一个视角，只写动作与当场后果。不要复述 title，"
                            "不要写成整场戏的梗概，不要解释设定怎么运转。"
                            "细节交给 action / result。"
                        )
                    },
                    "event_type": {
                        "type": "string",
                        "enum": ["plot", "character", "world", "conflict",
                                 "reveal", "decision", "transition"]
                    },
                    "actor": {"type": "string",
                              "description": "这个事件的行为主体（一个人）"},
                    "action": {
                        "type": "string",
                        "description": "他具体做了什么（用动作和后果描述，不要用名词点题）"
                    },
                    "intent": {"type": "string", "description": "他想达成什么"},
                    "result": {
                        "type": "string",
                        "description": "当场产生了什么直接后果。另一方的反应写在这里"
                    },
                    "involved_characters": {"type": "array", "items": {"type": "string"}},
                    "involved_entities": {"type": "array", "items": {"type": "string"}},
                    "importance": {"type": "integer", "minimum": 1, "maximum": 5},
                    "is_gradual": {
                        "type": "boolean",
                        "description": "是否是缓慢积累型变化（态度/信任/耐心），而非突变"
                    },
                    "state_changes": {
                        "type": "object",
                        "properties": {
                            "world": {"type": "array", "items": {
                                "type": "object",
                                "properties": {
                                    "key": {"type": "string"},
                                    "value": {},
                                    "value_type": {
                                        "type": "string",
                                        "enum": ["text", "int", "float", "bool", "json"]
                                    },
                                    "category": {
                                        "type": "string",
                                        "enum": ["tension", "resource", "threat",
                                                 "relation", "progress", "other"]
                                    },
                                    "reason": {"type": "string"}
                                },
                                "required": ["key", "value"]
                            }},
                            "characters": {"type": "array", "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "location": {
                                        "type": "string",
                                        "description": (
                                            "该角色在本事件**结束后**身处何处。"
                                            "层级从大到小用空格分开，能精确到哪一级就写到哪一级："
                                            "'旧城区 调查局本部 地下二层 封锁库'、"
                                            "'调查局本部 茶水间'。"
                                            "只知道大致范围就写范围（'旧城区'），"
                                            "不知道就别写这个字段。"
                                            "**没移动的角色也要写当前位置**——"
                                            "不写的话地图上他会一直停在旧位置。"
                                            "确实行踪不明就写 '某处'，不要编造具体地点。")
                                    },
                                    "location_secret": {
                                        "type": "boolean",
                                        "description": (
                                            "这次移动是否瞒着别人（秘密潜入、私下接头）。"
                                            "为 true 时，不在场的角色仍以为他在老地方——"
                                            "所以只在**当事人刻意隐瞒**时才填 true，"
                                            "普通走动一律 false。")
                                    },
                                    "status": {"type": "string"},
                                    "trait_delta": {
                                        "type": "object",
                                        "description": "性格维度变化，如 {'对调查局的信任': -10}"
                                    },
                                    "memory": {"type": "string",
                                               "description": "该角色亲历的、值得记住的一件事"}
                                },
                                "required": ["name"]
                            }}
                        }
                    },
                    "open_threads": {
                        "type": "array",
                        "description": "本事件留下的未解问题（悬置线索）",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title": {"type": "string"},
                                "description": {"type": "string"},
                                "tension": {"type": "integer", "minimum": 1, "maximum": 5}
                            },
                            "required": ["title"]
                        }
                    },
                    # v6.7：认知变化（信息边界的落点）。
                    # 角色能知道什么，取决于他**在场**或**有人告诉他**。
                    # 不显式写出来，模型默认所有人都全知。
                    "knowledge_changes": {
                        "type": "array",
                        "description": ("本事件改变了谁的认知。**只写真的变的**："
                                        "他亲眼看到了什么（know）、听谁说了什么"
                                        "（believe）、起了什么疑心（suspect）、"
                                        "为了行动暂时假定什么（assume）。"
                                        "没在场又没人告诉他的人**不要写**。"
                                        "拿不准就写成 suspect/believe，别写 know。"),
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "know_type": {
                                    "type": "string",
                                    "enum": ["know", "believe", "suspect", "assume"]
                                },
                                "about": {"type": "string",
                                          "description": "认知对象（人名/事件/事实）"},
                                "content": {"type": "string"}
                            },
                            "required": ["name", "know_type", "content"]
                        }
                    }
                },
                "required": ["title", "description", "event_type", "actor",
                             "action", "intent", "result"]
            }
        },
        # v6.7：直接产出的事实候选（可选）。
        # state_changes.world 只能表达"数量型"变化；"老周持有钥匙"这类
        # 不落在数字上的事实需要一个出口，否则它们只活在事件描述的自由文本里，
        # 下次推演看不见，几十章之后就彻底丢了。
        "facts": {
            "type": "array",
            "description": ("本次推演**新写定的世界真相**（主谓宾三元组）。"
                            "只写真的改变了的、不落在数字上的事实；没有就给空数组。"
                            "**不要写角色以为/怀疑的事**——那些写进 knowledge_changes。"),
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string",
                                "description": "主语（角色名 / 实体名 / 世界）"},
                    "subject_type": {"type": "string",
                                     "enum": ["character", "entity", "world"]},
                    "predicate": {"type": "string",
                                  "description": "谓词：存活 / 位于 / 持有 / 阵营 / 身份 …"},
                    "object": {"type": "string", "description": "宾语或值"},
                    "fact_type": {
                        "type": "string",
                        "enum": ["identity", "status", "location", "possession",
                                 "relationship", "ability", "injury", "resource",
                                 "outcome", "other"]
                    }
                },
                "required": ["subject", "subject_type", "predicate", "object"]
            }
        },
        "decision_needed": {
            "type": "array",
            "description": ("需要人拍板的岔路。**宁缺勿滥**：没有就给空数组。"
                            "只有事件已经撞出真实两难时才提，一次最多 1 个。"
                            "如果你提了岔路，必须在 events 里已经写出"
                            "\"谁撞上了谁的什么目标\"——不要凭空给一个岔路。"),
            "maxItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "trigger_type": {
                        "type": "string",
                        "enum": ["moral", "cost", "irreversible", "user_focus"]
                    },
                    "actor_name": {"type": "string"},
                    "title": {"type": "string"},
                    "situation": {"type": "string"},
                    "stakes": {"type": "string",
                               "description": "选错会失去什么（具体代价，不要写'后果严重'）"},
                    "reason": {
                        "type": "string",
                        "description": ("为什么这里必须有人拍板：是哪几个人的目标"
                                        "撞在了一起、撞出了什么不可两全的代价。"
                                        "**必填**——这段是给写正文的人看的，"
                                        "空着等于没解释为什么分岔。")
                    },
                    "forks_from": {
                        "type": "string",
                        "description": ("这条岔路是哪个事件逼出来的 —— 写那个事件的"
                                        "标题。让人能一眼看出\"岔路从哪儿长出来\"。")
                    }
                },
                "required": ["trigger_type", "actor_name", "title", "situation",
                             "stakes", "reason"]
            }
        },
        "new_characters": {
            "type": "array",
            "description": ("本次推演里**被卷进来的人**（此前不在【人物】名单里的）。"
                            "只写真的出场了的。他们没有就给空数组，不要硬凑。"),
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "role_tag": {"type": "string",
                                 "description": "身份/职务，如'夜班店员''街道办科员'"},
                    "personality": {"type": "string"},
                    "goal": {"type": "string",
                             "description": ("**他自己想要什么**。必须写——"
                                             "没有目标的人会变成工具人，"
                                             "下次推演他不知道该干嘛。")},
                    "relation_to": {"type": "string",
                                    "description": "他和谁产生了关系（人名）"},
                    "relation_desc": {"type": "string",
                                      "description": "这关系是什么（刚认识/结怨/欠人情…）"}
                },
                "required": ["name", "role_tag", "goal"]
            }
        },
        "character_exits": {
            "type": "array",
            "description": ("本次推演里**退场的人**：死亡、失踪、被关押、远走、"
                            "彻底退出这条线。写的是变动，不是状态描述——"
                            "本来就已经死了的不要再写。没有就给空数组。"),
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "status": {
                        "type": "string",
                        "enum": ["dead", "missing", "retired"],
                        "description": "dead 死了 / missing 失踪 / retired 退出这条线"
                    },
                    "reason": {"type": "string",
                               "description": "怎么退的场（发生了什么事）"}
                },
                "required": ["name", "status", "reason"]
            }
        },
        "relation_changes": {
            "type": "array",
            "description": (
                "本次推演里人物关系发生的实质变化。**只写真的变了的关系**："
                "结盟、反目、翻脸、生情、欠下人情、信任破裂、决裂。"
                "关系没动就别写——日常寒暄不算关系变化。"
                "新角色与旧角色之间产生的羁绊也必须写在这里，"
                "否则后面推演会以为这俩人素不相识。"),
            "items": {
                "type": "object",
                "properties": {
                    "a": {"type": "string", "description": "角色 A 的名字"},
                    "b": {"type": "string", "description": "角色 B 的名字"},
                    "relation_type": {
                        "type": "string",
                        "enum": ["family", "friend", "enemy", "lover", "colleague",
                                 "mentor", "rival", "other"]
                    },
                    "description": {
                        "type": "string",
                        "description": "变化后的关系内容：现在他们之间是什么状态、"
                                       "刚发生了什么导致这样"
                    },
                    "intensity": {"type": "integer", "minimum": 1, "maximum": 5},
                    "change": {
                        "type": "string",
                        "enum": ["new", "evolved", "broken"],
                        "description": "new 新建立 / evolved 性质变了 / broken 破裂了"
                    },
                    "is_mutual": {"type": "boolean",
                                  "description": "默认 true。单向的猜忌/暗恋填 false"}
                },
                "required": ["a", "b", "relation_type", "description"]
            }
        }
    },
    "required": ["world_time", "events"]
}


def world_sim_user(ctx_brief, extra="", focus=""):
    """推演的用户消息。

    ctx_brief 里已经带了【叙事要求】段（世界简报装配时注入的作者声音）。
    这里不再单独收 voice 参数——加一个可选参数会打破所有既有测试桩的签名
    （见 MEMORY 里那条铁律），而简报本来就该是唯一上下文入口。
    """
    parts = ["## 当前世界", ctx_brief]
    if focus:
        parts.append("\n## 本次推演请重点着眼\n%s" % focus)
    if extra:
        parts.append("\n## 补充约束\n%s" % extra)
    parts.append("\n请推演接下来发生的事，输出结构化事件流。")
    return "\n".join(parts)


# ============================================================ 角色决策（AI 托管）

CHARACTER_DECIDE_SYSTEM = """你在为小说里的一个角色做决定。你就是这个人，不是上帝。

你会拿到这个角色的性格、当前目标、面对的障碍，以及他此刻掌握的信息。

规则：
1. 只使用“他可能知道的信息”。不知道的事不能成为决策依据。
2. 决策必须符合性格与既往行为。若这次要偏离性格，偏离必须有已积累的铺垫。
3. 优先服务于他的**当前目标**；若多个目标冲突，按优先级取舍，并承受代价。
4. 不要选择“最合理”或“最讨读者喜欢”的选项，要选择**这个人真的会做的**。
5. 说明理由时不要写“他想了想”，直接给出他的判断依据。

【信息边界铁律】
- 你的信息块里【亲历/知道】【认为但没证实】【怀疑】【暂且假定】四类之外的事，
  你**就是不知道**。不要表现出你知道。
- 不要说出你没在场、也没人告诉过你的事。
- 如果某个信息前缀是"你以为""你怀疑"，那它可能是错的——**按你以为的去做**，
  哪怕事实不是这样。人会基于错误认知行动，这才是真实。
- 想知道什么，得先有人告诉你，或你自己去看、去查。"""

CHARACTER_DECIDE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "description": "他做了什么"},
        "reasoning": {"type": "string", "description": "他的判断依据（内心，不给读者看）"},
        "targets": {"type": "array", "items": {"type": "string"},
                    "description": "行动指向的角色/实体"},
        "intent": {"type": "string", "description": "他想达成什么"},
        "expected_result": {"type": "string", "description": "他预期会怎样"},
        "cost_accepted": {"type": "string", "description": "他愿意承受什么代价"},
        "may_trigger_decision": {
            "type": "boolean",
            "description": "这个行动是否会让局面出现需要人来拍板的岔路"
        }
    },
    "required": ["action", "reasoning", "intent"]
}


def character_decide_user(char_block, situation, knowledge="", constraints=""):
    parts = ["## 你是谁\n%s" % char_block,
             "\n## 此刻的局面\n%s" % situation]
    if knowledge:
        parts.append("\n## 你能掌握的信息\n%s" % knowledge)
    if constraints:
        parts.append("\n## 你的处境限制\n%s" % constraints)
    parts.append("\n请以这个人的身份做出决定。")
    return "\n".join(parts)


# ============================================================ 选项生成

OPTION_GEN_SYSTEM = """你为小说读者/创作者生成"岔路口选项"。

规则：
1. 至少给 3 个选项，且必须是**实质不同**的路线，不要只换措辞。
2. 每个选项都要标注它的后果倾向（consequence_hint）——不是剧透，是让玩家知道
   "这个选择偏冒险/偏稳妥/偏道德代价"。
3. 选项之间不能有明显最优解。如果有一个选项显然最好，说明选项设计失败。
4. 可以包含一个"看起来最坏但最符合角色性格"的选项。
5. tendency 用简短短语描述倾向，如"稳妥""激进""狡诈""自我牺牲"。
6. 不要写选项会导致的具体情节，只写倾向。"""

OPTION_GEN_SCHEMA = {
    "type": "object",
    "properties": {
        "options": {
            "type": "array",
            "minItems": 3,
            "items": {
                "type": "object",
                "properties": {
                    "label": {"type": "string", "description": "短标签，4-10 字"},
                    "description": {"type": "string", "description": "具体做法"},
                    "consequence_hint": {"type": "string", "description": "后果倾向"},
                    "tendency": {"type": "string", "description": "倾向标签"}
                },
                "required": ["label", "description", "consequence_hint", "tendency"]
            }
        }
    },
    "required": ["options"]
}


def option_gen_user(situation, actor, stakes, constraints=""):
    parts = ["## 局面\n%s" % situation,
             "\n## 谁要决定\n%s" % actor,
             "\n## 代价与风险\n%s" % stakes]
    if constraints:
        parts.append("\n## 额外限制\n%s" % constraints)
    parts.append("\n请给出至少 3 个实质不同的选项。")
    return "\n".join(parts)


# ============================================================ 状态抽取

STATE_EXTRACT_SYSTEM = """你从一段小说正文中抽取结构化状态变化。

只抽取**正文里确实写出来的**变化，不要推测、不要补全。
如果某类没有变化，就返回空数组。

抽取项：
- world_state：可量化的世界状态变化（数量/进度/关系值/威胁等级）
- facts：正文里**新写定的事实**（主谓宾三元组）。只写正文确实交代了的
- character_status：角色状态变化（存活/位置/伤势）
- character_knowledge：某个角色**在正文中确实获知**的新信息
- new_threads：正文里新出现但未解决的问题
- resolved_threads：正文里明确解决了的问题（给出标题关键词）
- new_characters：正文里新出场且尚未建档的角色
- conflicts：正文与下面【已写定的正史事实】**明确冲突**的地方

## 最关键的一条：区分「世界真的发生了」与「某个人脑子里的东西」

正文里有大量语句**不是事实**：
- 「他忽然想起，三年前自己杀过一个人」→ 这是**回忆**，可能记错
- 「他梦见自己站在废墟上」→ 这是**梦**，没发生过
- 「据说那批货早就运走了」→ 这是**传闻**，真假未定
- 「他觉得老周在撒谎」→ 这是**认知**，不等于老周真在撒谎

所以每条 facts 都要在 `provenance` 里写清**这句话是从哪儿来的**：
  亲眼所见 / 角色回忆 / 梦境 / 对话声称 / 转述传闻 / 内心推断 / 叙述者陈述
`classification` 只在你有把握时才填；拿不准就填 `world`，系统会再判一次。

**不要**把角色的猜想当成世界真相写进 facts。写错了比漏写更糟——
一条假的正史会在后面几十章里被当成前提使用。"""

STATE_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "world_state": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "value": {},
                "value_type": {"type": "string",
                               "enum": ["text", "int", "float", "bool", "json"]},
                "category": {"type": "string",
                             "enum": ["tension", "resource", "threat",
                                      "relation", "progress", "other"]},
                "reason": {"type": "string"}
            },
            "required": ["key", "value"]
        }},
        # v6.7：结构化事实（正文回流的"结构化比对"就靠它）
        "facts": {
            "type": "array",
            "description": ("正文里**新写定的事实**（主谓宾三元组）。"
                            "只写正文确实交代了的，不要推测。"),
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string",
                                "description": "主语（角色名 / 实体名 / 世界）"},
                    "subject_type": {"type": "string",
                                     "enum": ["character", "entity", "world"]},
                    "predicate": {"type": "string",
                                  "description": "谓词：存活 / 位于 / 持有 / 金额 / 阵营 …"},
                    "object": {"type": "string", "description": "宾语或值"},
                    "fact_type": {
                        "type": "string",
                        "enum": ["identity", "status", "location", "possession",
                                 "relationship", "ability", "injury", "resource",
                                 "outcome", "other"]
                    },
                    "quote": {"type": "string", "description": "正文里支撑它的原句"},
                    "provenance": {
                        "type": "string",
                        "description": ("这句话的叙述语境：亲眼所见 / 角色回忆 / "
                                        "梦境 / 对话声称 / 转述传闻 / 内心推断 / "
                                        "叙述者陈述")
                    },
                    "classification": {
                        "type": "string",
                        "enum": ["world", "cognition", "unverified", "dream",
                                 "memory", "author_claim"],
                        "description": ("world=世界真的发生了；cognition=某个角色的"
                                        "认知；memory=回忆；dream=梦/幻觉；"
                                        "author_claim=转述声称；unverified=说不清。"
                                        "拿不准就填 world，系统会再判一次。")
                    },
                    "belief_by": {
                        "type": "string",
                        "description": "如果是某个角色的认知，写是谁相信"
                    }
                },
                "required": ["subject", "subject_type", "predicate", "object"]
            }
        },
        "character_status": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "status": {"type": "string"},
                           "location": {"type": "string"}},
            "required": ["name"]
        }},
        "character_knowledge": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "knowledge": {"type": "string"}},
            "required": ["name", "knowledge"]
        }},
        "knowledge": {
            "type": "array",
            "description": ("正文里角色新得知的事。**不在场又没人告诉他的不要写**。"
                            "「他怀疑」「他认为」这类要写成 suspect/believe。"),
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "know_type": {
                        "type": "string",
                        "enum": ["know", "believe", "suspect", "assume",
                                 "memory", "dream"]
                    },
                    "about": {"type": "string"},
                    "content": {"type": "string"},
                    "source": {
                        "type": "string",
                        "enum": ["witnessed", "told", "inferred", "assumed",
                                 "read", "public", "dream", "recalled"]
                    }
                },
                "required": ["name", "know_type", "content"]
            }
        },
        "new_threads": {"type": "array", "items": {
            "type": "object",
            "properties": {"title": {"type": "string"},
                           "description": {"type": "string"},
                           "tension": {"type": "integer"}},
            "required": ["title"]
        }},
        "resolved_threads": {"type": "array", "items": {"type": "string"}},
        "new_characters": {"type": "array", "items": {
            "type": "object",
            "properties": {"name": {"type": "string"}, "role_tag": {"type": "string"},
                           "first_impression": {"type": "string"}},
            "required": ["name"]
        }},
        "conflicts": {
            "type": "array",
            "description": ("正文与「已写定的正史事实」明确冲突的地方。"
                            "只报你能指出具体冲突的；没有就给空数组。"),
            "items": {
                "type": "object",
                "properties": {
                    "fact": {"type": "string", "description": "与之冲突的正史陈述"},
                    "quote": {"type": "string", "description": "正文原句"},
                    "detail": {"type": "string"}
                },
                "required": ["fact", "quote"]
            }
        }
    }
}


def state_extract_user(chapter_text, known_characters="", known_threads="",
                       canon_facts=""):
    parts = []
    if known_characters:
        parts.append("## 已有角色（用于避免重复建档）\n%s" % known_characters)
    if known_threads:
        parts.append("\n## 当前活跃线索（用于判断是否被解决）\n%s" % known_threads)
    # v6.7：把正史摆给抽取器。
    # 没有这一段，它不知道正文里哪句话是"新事实"、哪句话是"与正史冲突"——
    # 只能一股脑全当新事实报上来，冲突检测就永远不会有输入。
    if canon_facts:
        parts.append(
            "\n## 已写定的正史事实（与它们矛盾的地方要写进 conflicts）\n%s"
            % canon_facts)
    parts.append("\n## 待抽取的正文\n%s" % chapter_text)
    parts.append("\n请抽取正文中确实发生的状态变化。")
    return "\n".join(parts)


# ============================================================ 摘要

SUMMARIZE_SYSTEM = """你为小说章节写摘要。

规则：只写"发生了什么"，不写"这说明了什么"。不超过 150 字。
按时间顺序，保留具体的人名、地点、数字。不要用"本章讲述了"这类套话。"""


# ============================================================ 完结判定

COMPLETION_JUDGE_SYSTEM = """你判断一个小说世界是否已经走到自然的终点。

不要只看"是否解决了一个问题"。请依次回答三个问题：

1. **核心张力是否已经消解或转化？**
   世界初始化时设定的核心矛盾，现在还存在吗？或者它已经变成了另一种矛盾？
2. **主要角色的目标是否已经达成、失败、或失去意义？**
   注意：目标"失去意义"也是一种结束（角色不再在乎了）。
3. **继续推进是否只是重复？**
   如果接下来的事件只是在同样的矛盾里打转，没有新的质变，那它就该结束了。

只有当三个问题都指向"该结束了"，才判定可以完结。
如果只是"这一卷结束了"，那不算完结。

给出判断时要具体引用世界当前的状态，不要泛泛而谈。"""

COMPLETION_JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "should_complete": {"type": "boolean"},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "q1_tension": {"type": "string", "description": "核心张力的现状"},
        "q2_goals": {"type": "string", "description": "角色目标的现状"},
        "q3_repetition": {"type": "string", "description": "是否只是在重复"},
        "reasoning": {"type": "string"},
        "suggested_ending_note": {
            "type": "string",
            "description": "若建议完结，给一句收尾方向的提示（不写正文）"
        }
    },
    "required": ["should_complete", "confidence", "q1_tension", "q2_goals",
                 "q3_repetition", "reasoning"]
}


def completion_judge_user(state_brief, goals_brief, recent_brief):
    return "\n".join([
        "## 世界当前状态\n%s" % state_brief,
        "\n## 完结目标与角色目标\n%s" % goals_brief,
        "\n## 最近发生的事\n%s" % recent_brief,
        "\n请回答三个问题并给出判断。",
    ])


# ============================================================ 世界脉搏（时间自然演化）

WORLD_PULSE_SYSTEM = """你在为一个小说世界"补背景"——时间流逝本身会改变一些东西。

主角没有参与、但世界仍在转动。你的职责是：**只推演与主角无关或弱相关、
却真实随时间发生的变化**。这是蝴蝶效应的燃料：现在无关，后面可能撞进主线。

硬性规则：
1. 只写"世界自己发生的事"，**主角与主要角色不得作为行动主体**。
   他们可以被动受影响（听到消息、被波及），但不能主动行动。
2. 变化必须从【世界时间】【世界状态】【活跃世界实体】【未解决的事】里
   自然长出来——不要凭空发明新势力、新大陆。允许推进已有实体的动向。
3. **允许"没有值得记录的变化"**：如果这个时间跨度里世界确实无事发生，
   events 给空数组。凑数比留白更糟。
4. 至少一条变化应当是"离主线较远"的——远处的一次调动、某个行业的变化、
   一条地方新闻。这是"与剧情无缘"的那类，正是要留下来的。
5. 每条变化都要写明它会如何**间接**影响到主线（哪怕只是"暂时不会"）。
6. 描述用日常口语，不要堆专业术语；不要写文学描写。

事件数量：1-3 条。宁少勿多。

绝对不要写：
- "就在这时" "仿佛" "不由得" 这类套路连接词
- 主题总结、意义评述
- 主角的决定、行动、心理活动
"""


WORLD_PULSE_SCHEMA = {
    "type": "object",
    "properties": {
        "world_time": {"type": "string", "description": "这批背景变化发生时的世界时间"},
        "events": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "event_type": {
                        "type": "string",
                        "enum": ["world", "transition", "reveal", "plot"]
                    },
                    "actor": {"type": "string",
                              "description": "行动主体（不得是主角/主要角色，可以是组织、群体、无名者）"},
                    "action": {"type": "string"},
                    "intent": {"type": "string"},
                    "result": {"type": "string"},
                    "importance": {"type": "integer", "minimum": 1, "maximum": 2,
                                   "description": "背景事件，重要性一律低"},
                    "involved_entities": {"type": "array",
                                          "items": {"type": "string"}},
                    "impact_on_main": {
                        "type": "string",
                        "description": "它会如何间接影响主线；暂时不影响就写「暂时无关」"
                    }
                },
                "required": ["title", "description", "event_type", "actor",
                             "action", "intent", "result"]
            }
        }
    },
    "required": ["world_time", "events"]
}


def world_pulse_user(ctx_brief, time_span="", focus=""):
    L = ["## 当前世界", ctx_brief]
    if time_span:
        L.append("\n## 这段时间跨度\n%s" % time_span)
    if focus:
        L.append("\n## 请重点留意\n%s" % focus)
    L.append("\n请只推演这段时间里「世界自己发生的变化」（主角不参与）。"
             "没有值得记录的就说没有，events 给空数组。")
    return "\n".join(L)


# ============================================================ 时间线锚点生成

TIMELINE_GEN_SYSTEM = """你看一段推演出来的事件，判断里面有没有"将来一定会到期"的东西。

时间线锚点 = 一个挂在未来某个时点上的约定/期限/倒数，到期时会再撞回剧情。
例：「三日后的听证会」「霜月十五的祭典」「下个月还款日」「倒计时 7 天」。

规则：
1. 只从给定事件里提取，不要发明事件里没影的期限。
2. 宁缺勿滥。没有就返回空数组——不要为凑数编一个"某天会发生某事"。
3. time_anchor 写"什么时候到期"，用世界时间的口径（如"霜月十五"），
   不要写"3 天后"这种相对表述——世界时间读数只有你知道。
4. countdown_value 是剩余量的可读描述（如"还剩 3 天"），没有倒数就给空串。
5. description 写清楚"到期时会发生什么"。
"""


TIMELINE_GEN_SCHEMA = {
    "type": "object",
    "properties": {
        "anchors": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "time_anchor": {"type": "string",
                                    "description": "到期时点（世界时间口径）"},
                    "time_span": {"type": "string", "description": "距当前还有多久"},
                    "countdown_name": {"type": "string",
                                       "description": "倒数项名称，可为空"},
                    "countdown_value": {"type": "string",
                                        "description": "剩余量描述，可为空"},
                    "description": {"type": "string",
                                    "description": "到期时会发生什么"}
                },
                "required": ["time_anchor", "description"]
            }
        }
    },
    "required": ["anchors"]
}


def timeline_gen_user(ctx_brief, events_block):
    return "\n".join([
        "## 当前世界", ctx_brief,
        "\n## 本次推演出的事件\n%s" % events_block,
        "\n请提取其中会在将来某时到期的约定/期限/倒数。没有就给空数组。",
    ])


# ============================================================ 章节选角建议

CASTING_SYSTEM = """你为一个小说章节决定出场角色名单。

规则：
1. 只选**这一章真的需要出场**的角色。不要为了"照顾"把所有人都排进来。
2. 每章新出场角色最多 2 个（含之前从未建档的）。
3. 优先复用已有角色和他们的已有矛盾，而不是引入新角色。
4. 主角默认是 POV（视角人物）。
5. 如果上一章留下了未处理的线头，优先让相关角色出场。"""

CASTING_SCHEMA = {
    "type": "object",
    "properties": {
        "casting": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "character_name": {"type": "string"},
                "role_in_chapter": {"type": "string",
                                    "enum": ["pov", "main", "supporting",
                                             "cameo", "mentioned"]},
                "is_new_character": {"type": "boolean"},
                "reason": {"type": "string", "description": "为什么需要他出场"}
            },
            "required": ["character_name", "role_in_chapter"]
        }},
        "chapter_focus": {"type": "string", "description": "这一章的核心矛盾"},
        "why_now": {"type": "string", "description": "为什么是现在推进这件事"}
    },
    "required": ["casting", "chapter_focus"]
}


# ============================================================ 初始阵容生成

CAST_GEN_SYSTEM = """你为一个刚建立的小说世界设计**初始角色阵容**。

这个世界目前还没有任何角色。接下来的世界推演要靠这些人互相推动，
所以你要给出一组"目标互相咬合"的人，而不是几张各自孤立的设定卡。

规则：
1. 只设计指定数量的角色，其中**恰好一个**主角（is_protagonist=true）。
2. 角色之间必须有真实的摩擦：争夺同一样东西、立场对立、互相隐瞒、
   或者一方是另一方的障碍。不要设计"目标一致、其乐融融"的组合。
3. 每个目标都要具体到「对象 + 想要的结果」，不要写"变强""活下去""查明真相"
   这类放到谁身上都成立的空话。
4. 至少两个角色的目标会直接冲突——冲突是世界推演的燃料。
5. 性格写"他会怎么做"，不要只堆形容词；要能解释他在压力下的选择。
6. 背景只写与当前核心张力有关的部分，不要写编年史。
7. 说话方式要具体到句式习惯，让写手能直接模仿。
8. 名字要贴合题材与基调，不要用"张三""主角""神秘人"这类占位名。
9. 位置写这个世界里的具体地点（可用世界前提里出现的地名）。
10. **必须同时给出关系网**（relations）。这套人不是各自孤立的卡片：
    - 每个角色至少出现在一条关系里，不许有孤岛。
    - 至少一条负向关系（敌对 / 竞争 / 猜忌），它是最结实的剧情燃料。
    - 关系要写"现在处什么状态、芥蒂在哪"，不要写"他们是同事"这种空标签。
    - 允许单向关系（is_mutual=false）：一方敌视、暗恋、猜疑而另一方不知道，
      这类不对称是最好的戏剧来源。
    - 有秘密关系的，写清"还有谁知道"（known_to）。不知道的人在场时
      表现会不一样——这正是推演需要的差异。

不要写任何文学描写，只输出结构化档案。"""

CAST_GEN_SCHEMA = {
    "type": "object",
    "properties": {
        "characters": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "is_protagonist": {"type": "boolean"},
                    "rank": {"type": "string", "enum": ["A", "B", "C", "D", "E"],
                             "description": "在故事中的分量"},
                    "role_tag": {"type": "string", "description": "身份/职务"},
                    "personality": {"type": "string"},
                    "background": {"type": "string"},
                    "speech_style": {"type": "string"},
                    "decision_tendency": {"type": "string"},
                    "location": {"type": "string", "description": "起始所在地点"},
                    "goals": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {"type": "string"},
                                "goal_type": {"type": "string",
                                              "enum": ["short", "long"]},
                                "motivation": {"type": "string"},
                                "obstacle": {"type": "string"},
                                "priority": {"type": "integer",
                                             "minimum": 1, "maximum": 5}
                            },
                            "required": ["content", "goal_type"]
                        }
                    }
                },
                "required": ["name", "is_protagonist", "role_tag", "personality",
                             "goals"]
            }
        },
        "conflict_map": {
            "type": "string",
            "description": "一句话说明这套阵容的冲突结构：谁和谁在争什么"
        },
        "relations": {
            "type": "array",
            "description": (
                "这套阵容的关系网。**必须**覆盖每个角色至少一条，"
                "且至少有一条是负向的（敌对/竞争/猜忌）。"
                "只写你设计出来的人之间的关系，不要写'他们是同乡'这类无信息量的敷衍描述。"),
            "items": {
                "type": "object",
                "properties": {
                    "a": {"type": "string", "description": "角色 A 的名字（用上面 characters 里的名字）"},
                    "b": {"type": "string", "description": "角色 B 的名字"},
                    "relation_type": {
                        "type": "string",
                        "enum": ["family", "friend", "enemy", "lover", "colleague",
                                 "mentor", "rival", "other"],
                        "description": "family 亲属 / friend 朋友 / enemy 敌对 / "
                                       "lover 恋慕 / colleague 同僚 / mentor 师徒 / "
                                       "rival 竞争 / other 其他"
                    },
                    "description": {
                        "type": "string",
                        "description": "这段关系的具体内容：他们之间有过什么、"
                                       "现在处在什么状态、芥蒂在哪里。"
                                       "要能解释他们见面时会怎么对待彼此。"
                    },
                    "intensity": {"type": "integer", "minimum": 1, "maximum": 5,
                                  "description": "这段关系对剧情的影响强度"},
                    "is_mutual": {
                        "type": "boolean",
                        "description": "是否双向。false 表示 A 对 B 是这样，B 对 A 未必——"
                                       "单向的敌意/暗恋/猜疑是很好的戏剧燃料。默认 true。"
                    },
                    "known_to": {
                        "type": "array", "items": {"type": "string"},
                        "description": "还有谁知道这段关系（限定名字）。"
                                       "秘密关系必须填——不知道的人在场时会表现得不一样。"
                    }
                },
                "required": ["a", "b", "relation_type", "description"]
            }
        }
    },
    "required": ["characters", "conflict_map", "relations"]
}


def cast_gen_user(title="", genre="", tone="", premise="", initial_tension="",
                  count=4, focus=""):
    L = ["## 世界"]
    if title:
        L.append("书名：%s" % title)
    if genre:
        L.append("题材：%s" % genre)
    if tone:
        L.append("基调：%s" % tone)
    if premise:
        L.append("\n## 世界前提\n%s" % premise)
    if initial_tension:
        L.append("\n## 核心张力\n%s" % initial_tension)
    if focus:
        L.append("\n## 额外要求\n%s" % focus)
    L.append("\n请设计 %d 个初始角色（含 1 个主角）。" % count)
    return "\n".join(L)


# ============================================================ 世界构建（v6.8）
#
# 用户场景：
#   「创建世界的时候可以去网络上搜索热门小说类型、题材，然后根据这些数据让用户
#     选择生成哪一类，生成的时候用户可以增加一些提示内容。然后自动创建出这个
#     虚拟世界。」
#
# 分工：
#   · 题材热度与题材语义 —— engine/genretrend.py 提供（联网抓 + 手工语义库），
#     这段提示词只负责**把选定的那一个题材变成一份能推演的世界**。
#   · 这里**不做选择题**：题材已经由用户选定，模型的活是"把这个题材落成具体世界"。
#     让模型再推荐一次题材，它只会给出放之四海皆准的通用设定。

WORLD_GEN_SYSTEM = """你要为一个小说创作系统**搭一个能长期推演的世界**。

用户已经选定了一个题材，可能还给了额外要求。你要交出这个世界的完整初始设定，
让后续的世界推演能立刻开始跑——不是交一份"题材介绍"，是一份**可执行的世界配置**。

最重要的三条（违反任何一条，这个世界的推演都会出问题）：

**1. 世界状态必须是可计算的数字，键必须是「主体.谓词」。**
   推演引擎拿世界状态当硬事实用：每轮推演后它会比对数字变化来判断世界怎么动了。
   所以键写成 `主体.谓词`（主体在前、点号、谓词在后），值尽量是数字。
   · 对：`调查局.在编调查员 = 12`、`异常实体.剩余数量 = 7`、`封印.剩余年限 = 180`
   · 错：`世界很危险`（不是键值）、`在编人数`（缺主体）、`调查局12人`（没点号）
   这些数字是**这个世界的度量衡**：它们会变，变化本身就是剧情。
   给 4 到 8 条，覆盖这个题材最该紧张的那几个量。

**2. 核心张力必须是一个会持续产生事件的矛盾，不是一句背景描述。**
   它要能回答："下一章该发生什么？"
   · 错：「这个世界充满未知的危险」——推演不出任何具体事件
   · 对：「调查局每月必须上报处理数量，但真实异常远少于指标，
          于是开始伪造记录；而伪造的记录会引来真正的异常。」
   张力的形状是：**有人必须持续做某件事，而这件事会持续产生代价。**

**3. 世界规则是这个世界的物理定律，不是道德劝告。**
   写成像「死人不会复活」这样可判定的陈述句。每条给一个适用范围
   （`global` 全局 / 某个群体或实体名）。
   规则会被当成铁律摆给推演模型，写含糊了等于没写。

其他要求：
- **地名、机构名、术语都要具体**，不要用「某市」「神秘组织」这类占位符。
  名字是世界的质感来源。
- 世界时间写一个具体的中文时间（如「霜月十五 黄昏」），别写「故事开始时」。
- 书名要贴合题材与基调，6 到 12 字，不要用烂大街的词。
- 如果用户给了额外要求，**以他的要求为准**，宁可偏离题材套路也要满足他。

不要写任何文学描写、不要写人物小传、不要解释你的思路，只输出结构化配置。"""

WORLD_GEN_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string", "maxLength": 20,
                  "description": "书名，6-12 字"},
        "genre": {"type": "string", "maxLength": 30,
                  "description": "题材，可直接沿用用户选定的题材名"},
        "tone": {"type": "string", "maxLength": 24,
                 "description": "基调，如 冷峻 / 诙谐 / 悲悯 / 压抑"},
        "premise": {"type": "string", "maxLength": 700,
                    "description": "世界前提：这个世界是什么样、正在发生什么。"
                                   "要交代清这个题材的几条世界轴"},
        "initial_tension": {"type": "string", "maxLength": 300,
                            "description": "核心张力：一个会持续产生事件的矛盾"},
        "world_time": {"type": "string", "maxLength": 40,
                       "description": "起始世界时间，中文写法"},
        "tags": {"type": "array", "maxItems": 6,
                 "items": {"type": "string", "maxLength": 10},
                 "description": "3-6 个题材标签，用于以后检索与分类"},
        "world_state": {
            "type": "array", "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "maxLength": 30,
                            "description": "必须是「主体.谓词」格式"},
                    "value": {"type": "string", "maxLength": 20,
                              "description": "尽量是数字"},
                    "value_type": {"type": "string",
                                   "enum": ["int", "float", "text"]},
                    "note": {"type": "string", "maxLength": 40},
                },
                "required": ["key", "value"],
            },
        },
        "rules": {
            "type": "array", "maxItems": 6,
            "items": {
                "type": "object",
                "properties": {
                    "content": {"type": "string", "maxLength": 80,
                                "description": "一句可判定的陈述句"},
                    "scope": {"type": "string", "maxLength": 30},
                    "severity": {"type": "string",
                                 "enum": ["error", "warning"]},
                    "check_hint": {"type": "string", "maxLength": 60,
                                   "description": "怎么判断有没有违反"},
                },
                "required": ["content"],
            },
        },
        "design_note": {"type": "string", "maxLength": 240,
                        "description": "一句话说明这个世界最要紧的矛盾是什么，"
                                       "给作者看的，不进推演"},
    },
    "required": ["title", "genre", "tone", "premise", "initial_tension",
                 "world_state"],
}


def world_gen_user(genre_block="", hint="", audience="", avoid=""):
    """组装世界构建的输入。

    `genre_block` 由 `genretrend.genre_block()` 渲染——里面已经带了该题材的
    世界轴、套路、开篇钩子、避坑，以及**用户的额外要求**。
    这里只补上"还差什么"，不重复它的内容。
    """
    L = []
    if genre_block:
        L.append(genre_block)
    else:
        # 用户没从热门题材里挑（自己写了个题材名）也要能走通：
        # 这时 genretrend 里没有对应条目，但世界构建不该因此不可用。
        L.append("## 用户自定题材")
        if _s(hint):
            L.append(hint)
    if audience and not genre_block:
        L.append("受众：%s" % audience)
    if avoid:
        L.append("\n## 明确不要\n%s" % avoid)
    L.append("\n请按上面的题材与要求，输出这个世界的完整初始配置。")
    return "\n".join(L)


def _s(v):
    return "" if v is None else str(v).strip()


# ============================================================ 章节诊断（v6.6）
#
# 用户场景（原话）：
#   「当读正文发现有逻辑问题的时候，又不知道是哪里的问题，可能是世界设定，
#     可能是事件推理，也有可能是人物设定的。」
# 所以这里不猜"哪里错了"，而是**把七层事实全部摊开让模型逐层比对**。
#
# v6.7：五层 → 七层。新增的两层（知识边界 / 正史一致）都是原五层**结构上装不下**
# 的问题：角色"不该知道却知道"可以勉强塞进人物层，但"他以为的和事实不符"
# 是独立的一层；而"正文与正史账本冲突"在事件层里根本看不出来。

DIAGNOSE_SYSTEM = """你是一个小说逻辑审查员。作者读完自己这一章，觉得"有地方不对劲"，
但说不清问题出在哪一层。你的任务是**逐层比对，定位矛盾，给出可执行的修改建议**。

你会拿到七层事实（世界 / 人物 / 关系 / 位置 / 知识边界 / 事件 / 正史）和本章正文。
按下面顺序逐层检查，**每层单独看，不要跳层**：

1. **世界层**：正文有没有违反已写定的世界状态量、世界时间、已死/失踪/退场角色、
   已摧毁实体、活跃线索？把已死的角色当活人写、让时间倒流、让摧毁的东西复原——
   都是这一层的错。
2. **人物层**：角色的性格、目标、说话方式、外貌，与正文表现是否冲突？
3. **关系层**：正文里两个人怎么相处，与已写定的关系类型/强度/状态（破裂/变化）对得上吗？
   单向关系里，被指向的一方**不该知情**——正文若写成"他心里有数"就是错。
   不在关系表里的两人，不该写得像多年老友。
4. **位置层**：角色的**实际位置**对不对？地图上的位置、场所的层级、
   该在 A 地的人出现在 B 地——这一层的错。
5. **知识边界层**（v6.7 新增）：某人的**认知**与他获取信息的渠道对得上吗？
   · 他说出了他没在场、也没人告诉过他的事 → 错
   · 他"以为"的事与事实不符，这是**正常的**（人会被骗），
     但正文若把他的误解**当成事实**叙述出来（叙述者跟着一起错）→ 错
   · 注意与位置层分工：位置层看"实际在哪"，知识边界层看"谁以为他在哪"。
6. **事件层**：正文有没有偏离它该写的事件记录？多写了没有的事、
   漏写了关键结果、把"意图"写成了"已经做到"、把结果写反——都算。
7. **正史一致层**（v6.7 新增）：正文与**正史账本**（已写定的事实）直接冲突吗？
   · 正史"老周已死亡"，正文写"老周推门进来" → 错
   · 正史"那笔钱在保险柜"，正文写"他从抽屉里拿出那笔钱" → 错
   · 正文**补充**正史没提的细节是正常的（那是丰富）；只有**不能同时为真**才算错。

判断纪律（很重要）：
- **只报你能指出具体冲突的问题**。正文里必须引得出原句，
  库里必须找得到与之矛盾的事实。**不要凭"感觉不太对"报问题。**
- 文风、节奏、用词好不好，**不是你的职责**。你只管逻辑与设定冲突。
- 分层归因要准：同一个现象如果跨层，优先归到**最根本的那一层**
  （比如"他不知道却知道"应归知识边界层，而不是事件层）。
- 如果某一层确实没问题，就不要为了凑数造问题。**没有问题是正常的结论。**
- 严重度：`error` = 硬矛盾（读者会一眼看出错）；`warning` = 存疑（可能是有意为之）。

**输出篇幅的硬约束（务必遵守）**：
- `issues` **最多 8 条**。挑最要命的报，按严重度从高到低排。
  同一个根因导致的多处表现，**合并成一条**，在 problem 里一并说明。
- 每条问题：`problem` 一句话（≤60 字）；`quote` 只抄**最关键的那一句**
  （≤80 字，别整段照搬）；`conflicts_with` 只引材料里的**关键事实**
  （≤80 字）；`suggestion` 写清"把 X 改成 Y，因为 Z"（≤140 字）。
- `summary` ≤120 字；`verdict` ≤80 字。
- 不要复述材料原文，不要解释你的检查过程，不要输出推理步骤——
  **只输出结论**。原文已经很长，你多写一个字都是在挤占真正有用的信息。

每条问题必须给出：哪一层、多严重、正文原句（照抄）、与之冲突的库中事实、
以及**具体到"怎么改"的建议**（不要写"建议重新考虑"这种废话，
要写"把 X 改成 Y，因为 Z"）。"""

# 七层的**唯一收口**：提示词、schema、前端图例、diagnose 引擎都用这一个元组。
DIAGNOSE_LAYERS = ("world", "character", "relation", "location",
                   "knowledge", "event", "canon")

DIAGNOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "layers_checked": {
            "type": "array", "items": {
                "type": "string",
                "enum": list(DIAGNOSE_LAYERS) + ["unknown"]},
            "description": "实际检查过的层，取值 world/character/relation/"
                           "location/knowledge/event/canon"
        },
        "issues": {
            "type": "array", "maxItems": 8,
            "description": "最多 8 条，只报最要命的；同根因的合并成一条",
            "items": {
                "type": "object",
                "properties": {
                    "layer": {"type": "string",
                              "enum": list(DIAGNOSE_LAYERS) + ["unknown"]},
                    "level": {"type": "string", "enum": ["error", "warning"]},
                    "problem": {"type": "string", "maxLength": 90,
                                "description": "一句话说清是什么矛盾，≤60 字"},
                    "quote": {"type": "string", "maxLength": 120,
                              "description": "正文里出问题的**那一句**，照抄，≤80 字，别整段搬"},
                    "conflicts_with": {"type": "string", "maxLength": 120,
                                       "description": "与之冲突的库中事实，引关键处，≤80 字"},
                    "suggestion": {"type": "string", "maxLength": 200,
                                   "description": "具体改法：把 X 改成 Y，因为 Z。≤140 字"},
                    "severity_note": {"type": "string", "maxLength": 80,
                                      "description": "为什么判成这个严重度，≤40 字"}
                },
                "required": ["layer", "level", "problem", "suggestion"]
            }
        },
        "summary": {"type": "string", "maxLength": 200,
                    "description": "两三句话总结：主要问题集中在哪一层，根子是什么。≤120 字"},
        "suspected_root": {"type": "string",
                           "enum": list(DIAGNOSE_LAYERS) + ["unknown"],
                           "description": "你判断的根因层"},
        "verdict": {"type": "string", "maxLength": 140,
                    "description": "如果作者反馈的现象其实不是逻辑错，而是别的问题，在这里说明。≤80 字"}
    },
    "required": ["issues", "summary"]
}


def diagnose_user(novel_block="", world_block="", characters_block="",
                  relations_block="", locations_block="", events_block="",
                  chapter_text="", complaint="", knowledge_block="",
                  canon_block=""):
    """诊断提示词的装配。

    `knowledge_block` / `canon_block` 是 v6.7 新增的两层材料。
    两者都有默认空值——**老调用方不传也照样工作**（材料少一层，
    模型就在那一层报"暂无记录"，不会报错）。
    """
    L = []
    if complaint:
        L.append("## 作者的反馈（他觉得哪里不对劲）\n%s" % complaint)
    L.append("\n## 作品基调\n%s" % (novel_block or "（未设置）"))
    L.append("\n## 【第 1 层】世界事实（正文不得与这些矛盾）\n%s"
             % (world_block or "（暂无世界状态记录）"))
    L.append("\n## 【第 2 层】人物档案（性格/目标/说话方式/已知信息）\n%s"
             % (characters_block or "（暂无角色档案）"))
    L.append("\n## 【第 3 层】人物关系（决定了他们怎么对待彼此）\n%s"
             % (relations_block or "（暂无关系记录）"))
    L.append("\n## 【第 4 层】位置与场所（谁实际在哪）\n%s"
             % (locations_block or "（暂无位置记录）"))
    L.append("\n## 【第 5 层】知识边界（谁以为事情是什么样）\n%s"
             % (knowledge_block or "（暂无认知记录）"))
    L.append("\n## 【第 6 层】本章该写的事件记录（正文取材于此）\n%s"
             % (events_block or "（本章没有关联事件记录）"))
    L.append("\n## 【第 7 层】正史账本（已写定的事实，正文不得与之矛盾）\n%s"
             % (canon_block or "（暂无正史记录）"))
    L.append("\n## 本章正文\n%s" % chapter_text)

    L.append("\n请按 %s 的顺序逐层比对，"
             "输出你找到的矛盾。只报能指出具体冲突的问题；没有问题就返回空数组。"
             % " → ".join(DIAGNOSE_LAYERS))
    return "\n".join(L)


REVISE_SYSTEM = """你是一个小说修稿员。审查员已经指出这一章的几处逻辑问题，
作者也给了处理意见。你的任务是**只改动出问题的地方**。

铁律：
1. **只改有问题的段落。** 没有问题的地方一个字都不要动——
   不要顺手润色、不要调整语气、不要重组段落顺序。作者认可那些文字。
2. 改动必须**精确落在**被指出的问题上：审查员说"他不知道这件事"，
   你就改掉这个信息越界；说"这人应该是单向知情"，你就改掉对方的知情表现。
3. 改完要保持**上下文衔接**。如果改动影响了紧邻的句子（比如删掉一句导致指代不清），
   允许最小幅度调整邻句，但要在改动说明里讲清楚。
4. 保持原有文风与叙事视角，不要因为改一处就把整段的语感换掉。
5. **不要把改动做成"打补丁"的痕迹**——读者不该看出这里有块补丁。

输出：改后的**完整正文**（不是片段），以及一份改动清单。"""

REVISE_SCHEMA = {
    "type": "object",
    "properties": {
        "content": {"type": "string",
                    "description": "改后的完整正文。没问题的段落原样保留。"},
        "changes": {
            "type": "array", "items": {
                "type": "object",
                "properties": {
                    "where": {"type": "string",
                              "description": "改动位置，引用改动前的原句片段"},
                    "before": {"type": "string", "description": "改前文字"},
                    "after": {"type": "string", "description": "改后文字"},
                    "why": {"type": "string", "description": "对应哪条问题/哪个意见"}
                },
                "required": ["before", "after", "why"]
            }
        },
        "note": {"type": "string",
                 "description": "补充说明：如果有意见你认为不该照办，在这里说明理由"}
    },
    "required": ["content"]
}


def revise_user(chapter_text, issues=None, direction="", handle=None):
    """issues: 审查报告里的问题列表；direction: 作者自己写的修改方向；
    handle: 作者对某条问题的处理意见（列表，同序对应 issues）。"""
    L = ["## 原正文\n%s" % chapter_text]
    if issues:
        L.append("\n## 审查员指出的问题（共 %d 条）" % len(issues))
        for i, it in enumerate(issues, 1):
            L.append("%d. 【%s/%s】%s" % (i, it.get("layer"), it.get("level"),
                                         it.get("problem")))
            if it.get("quote"):
                L.append("   正文原句：%s" % it["quote"])
            if it.get("conflicts_with"):
                L.append("   冲突事实：%s" % it["conflicts_with"])
            if it.get("suggestion"):
                L.append("   建议改法：%s" % it["suggestion"])
            if handle and i <= len(handle) and handle[i - 1]:
                L.append("   **作者的处理意见：%s**（以作者意见为准）"
                         % handle[i - 1])
    if direction:
        L.append("\n## 作者自己给的修改方向（必须落实）\n%s" % direction)
    L.append("\n请只改动上述问题涉及的段落，输出改后的完整正文与改动清单。")
    return "\n".join(L)


# ============================================================ 事件卡修正（v6.8）
#
# 用户场景（原话）：
#   「事件卡片有逻辑不对的地方，可以点击修改，也可以提交文字说明哪里不对，
#     让AI修改。修改后需要校验后续事件是否和修改的内容上下衔接并且符合逻辑。」
#
# 所以这里是**两件事**，别混成一个：
#   A. 修这一张卡  → EVENT_FIX_*（把某一处逻辑不对改掉，只动该动的字段）
#   B. 查后续衔接  → CARD_CHAIN_*（改完之后，往下游逐条比对是否还接得上）
#
# 为什么必须分开：改卡是"生成"，校验是"审查"。让同一个模型在一次调用里
# 既改又问自己改得对不对，它几乎永远说"没问题"——这是自评失效的经典形态。

E_CARD_FIELDS = ("title", "description", "action", "intent", "result",
                 "stakes", "event_type", "actor_name")

EVENT_FIX_SYSTEM = """你是一个小说剧情校对员。作者指出**某一张事件卡**里有逻辑不对的地方，
你要按他的说明把这张卡改对。

事件卡由这些字段构成：
- `title`   一句话概括这段剧情
- `action`  这张卡里**做了什么**（具体的行动）
- `intent`  **为什么**这么做（动机、想要什么）
- `result`  **结果**（这个行动造成了什么、有没有达到目的）
- `stakes`  代价 / 风险
- `description` 连贯叙述（把上面三行揉成一段，给正文层当素材）

铁律：
1. **只改作者指出的那一处**。其他字段一个字都不要动——不要顺手润色、
   不要"顺便让文笔更好"、不要调整叙事顺序。作者认可其余部分。
2. 作者的说明是**最高指令**。他说"这个人不该知道这件事"，你就改掉越界的信息；
   他说"行动和结果对不上"，你就理顺因果。即便你认为原文更合理，也**照作者的改**，
   只在 `note` 里说明你的保留意见。
3. 改完必须**自身自洽**：`action`（做了什么）、`intent`（为什么）、
   `result`（结果如何）三者要能连成一条因果链。别出现"想救人"却"刺杀了他"
   这种动机与行动打架的情况。
4. `title` 要跟着改后的内容走。如果改动很小、title 仍然贴切，就别动它。
5. **不要引入新的事实、新的人物、新的地名**——除非作者的说明里明确要求。
   擅自加设定会把它下游的整条线全部带偏。
6. 保持原来的叙事口吻与详略程度，不要因为改一处就把整段的语感换掉。

输出里 `patch` **只放你真正改动的字段**，没改的字段一律不要出现。
`changed` 与 `patch` 的键必须完全一致（都是真改了的那些）。
`note` 用来说明你的保留意见或需要作者留意的地方；没有要说的就留空字符串。

为什么只输出改动项：把整张卡回一遍的话，模型得把没动过的字段照样抄一遍
（`description` 动辄几百字），输出量翻好几倍——实测因此撞上输出上限被截断
（finish=length），反而把已经改对的部分一起丢掉。只回改动项既省预算，
也顺手堵住了"顺手改动别的字段"这条歧路。"""

EVENT_FIX_SCHEMA = {
    "type": "object",
    "properties": {
        "patch": {
            "type": "object",
            "description": "**只放真正被改动的字段**（键取自白名单），"
                           "没改的字段不要出现在这里。",
            "properties": {
                "title": {"type": "string", "maxLength": 60},
                "action": {"type": "string", "maxLength": 300},
                "intent": {"type": "string", "maxLength": 300},
                "result": {"type": "string", "maxLength": 300},
                "stakes": {"type": "string", "maxLength": 200},
                "description": {"type": "string", "maxLength": 800},
                "event_type": {"type": "string", "maxLength": 20},
                "actor_name": {"type": "string", "maxLength": 40},
            },
        },
        "changed": {
            "type": "array", "maxItems": 8,
            "items": {"type": "string", "enum": list(E_CARD_FIELDS)},
            "description": "实际被改动的字段名，必须与 patch 的键一致",
        },
        "why": {"type": "string", "maxLength": 300,
                "description": "改动说明：对应作者的哪句话、怎么改的"},
        "note": {"type": "string", "maxLength": 300,
                 "description": "保留意见或需作者留意处；没有就留空"},
    },
    "required": ["patch", "changed"],
}


def event_fix_user(card, complaint="", direction="", context=""):
    """装配「改这一张卡」的输入。

    `card`      当前卡片字段（dict）
    `complaint` 作者写的"哪里不对"（自然语言）
    `direction` 作者对改法的具体指示（可选，优先级高于 complaint 的描述）
    `context`   这张卡在世界里的处境：前因、涉及角色、当前局势等
    """
    L = ["## 待修改的事件卡（当前内容）"]
    for k, label in (("title", "标题"), ("action", "做了什么"),
                     ("intent", "为什么"), ("result", "结果"),
                     ("stakes", "代价"), ("event_type", "类型"),
                     ("actor_name", "行动主体")):
        v = _s((card or {}).get(k))
        L.append("- %s（%s）：%s" % (label, k, v or "（空）"))
    desc = _s((card or {}).get("description"))
    if desc:
        L.append("\n### 该卡的连贯叙述（description）\n%s" % desc)

    if context:
        L.append("\n## 这张卡在世界里的处境（改的时候要照顾到）\n%s" % context)
    if complaint:
        L.append("\n## 作者说哪里不对（必须解决）\n%s" % complaint)
    if direction:
        L.append("\n## 作者要求怎么改（优先级最高，必须落实）\n%s" % direction)
    L.append("\n请只改动作者指出的那处问题。输出**只含被改动的字段**（patch），"
             "没改的字段一个都不要写——不要复述原文。")
    return "\n".join(L)


# ------------------------------------------------------------ 改完后查下游衔接

CARD_CHAIN_SYSTEM = """你是一个剧情连贯性审查员。上游的某张事件卡刚被作者改过，
你要检查**它下游的每一个事件**是否还与改后的内容衔接得上、逻辑是否还成立。

给你三样东西：
  · 【改后的事件卡】—— 变动的源头
  · 【改前的内容】—— 用来对照"这次到底改动了什么"
  · 【下游事件清单】—— 从这张卡往后，沿着当前这条线依次发生的事件

按下面的顺序逐条检查每个下游事件。**每个事件单独看，不要跳。**

1. **因果是否还成立**：下游事件的前提，是不是还由改后的卡（或它更上游的链条）支撑？
   如果改后的结果不再能推出下游的起因，这就是断裂。
2. **事实是否还一致**：改后是否与下游事件里陈述的事实冲突？
   比如改后"他留在城里"，下游却写"他赶到码头"——这就是硬矛盾。
3. **人物认知是否还合理**：改后是否让某人"知道了不该知道的"，
   或者相反"该知道的却无从得知"？信息来源必须走得通。
4. **世界状态量是否还接得上**：改后涉及的数值、时间、位置、存亡状态，
   与下游事件的前提对得上吗？（时间倒流、死人复活、摧毁的复原都是硬错）
5. **动机链是否还连贯**：下游人物的动机，还能从改后的局面里长出来吗？
   改完变成一个"没理由这么做"的行为，也算断裂。

判断纪律（很重要）：
- **只报你能指出具体冲突的问题**。必须引得出下游事件的原文，
  也说得出它与改后内容的哪一处对不上。**不要凭"感觉不太对"报问题。**
- 改动**可能只影响少数下游事件**，后面的事件链条往往自行恢复了自洽——
  这很正常。**不要为了凑数把整条链都标成有问题。**
- 如果改后内容其实**加强了**下游的合理性，就在 `ok_summary` 里说，
  不要把它当成问题报出来。
- 严重度：`broken` = 硬断裂（读者会看出对不上，必须处理）；
  `weak` = 勉强能圆但牵强（建议处理）；`ok` = 没问题（**不用逐个列出来**，
  只把有问题的列进 `issues`）。

**输出篇幅的硬约束**：
- `issues` **最多 8 条**，按严重度从高到低排。同一个根因导致的多处表现合并成一条。
- 每条 `reason` ≤100 字，`fix_hint` ≤140 字（要写"把 X 改成 Y"，不要写废话）。
- `ok_summary` ≤120 字。
- 不要复述材料原文，不要输出你的推理过程，**只输出结论**。"""

CARD_CHAIN_SCHEMA = {
    "type": "object",
    "properties": {
        "issues": {
            "type": "array", "maxItems": 8,
            "description": "只列有问题的下游事件；没问题的不要列",
            "items": {
                "type": "object",
                "properties": {
                    "node_id": {"type": "integer",
                                "description": "下游事件的节点 id，必须来自材料里给的清单"},
                    "title": {"type": "string", "maxLength": 60,
                              "description": "该事件标题，照抄材料"},
                    "level": {"type": "string", "enum": ["broken", "weak"]},
                    "aspect": {
                        "type": "string",
                        "enum": ["causality", "fact", "knowledge",
                                 "world_state", "motive"],
                        "description": "causality=因果断裂 fact=事实冲突 "
                                       "knowledge=认知不合理 world_state=世界状态量接不上 "
                                       "motive=动机链断裂",
                    },
                    "reason": {"type": "string", "maxLength": 160,
                               "description": "说清下游哪里和改后内容对不上，≤100 字"},
                    "quote": {"type": "string", "maxLength": 160,
                              "description": "下游事件里的关键原句，照抄，≤80 字"},
                    "fix_hint": {"type": "string", "maxLength": 220,
                                 "description": "具体改法：把 X 改成 Y。≤140 字"},
                },
                "required": ["node_id", "level", "aspect", "reason"],
            },
        },
        "ok_summary": {"type": "string", "maxLength": 200,
                       "description": "整体结论：下游链条是否还立得住，≤120 字"},
        "affected_count": {"type": "integer",
                           "description": "受影响的下游事件条数（= issues 的条数）"},
        "checked_count": {"type": "integer",
                          "description": "实际检查过的下游事件条数"},
    },
    "required": ["issues", "ok_summary"],
}


def card_chain_user(card, before="", downstream=None, world_context=""):
    """装配「查下游衔接」的输入。

    `card`        改后的卡片
    `before`      改前的卡片内容（字符串，用来对照改了什么）
    `downstream`  下游事件列表，每项 {'id','title','description','card'}
    `world_context` 世界状态摘要（可空）
    """
    L = ["## 改后的事件卡"]
    for k, label in (("title", "标题"), ("action", "做了什么"),
                     ("intent", "为什么"), ("result", "结果"),
                     ("stakes", "代价")):
        v = _s((card or {}).get(k))
        L.append("- %s：%s" % (label, v or "（空）"))
    desc = _s((card or {}).get("description"))
    if desc:
        L.append("\n改后叙述：%s" % desc)

    if before:
        L.append("\n## 改前的内容（用来对照这次改了什么）\n%s" % before)
    if world_context:
        L.append("\n## 当前世界状态摘要\n%s" % world_context)

    items = list(downstream or [])
    L.append("\n## 下游事件清单（从这张卡往后依次发生，共 %d 条）" % len(items))
    if not items:
        L.append("（这张卡后面还没有事件——那就不存在衔接问题，返回空数组即可）")
    for n in items:
        L.append("\n### 节点 %s：%s" % (n.get("id"), _s(n.get("title"))))
        c = n.get("card") or {}
        for k, label in (("action", "做了什么"), ("intent", "为什么"),
                         ("result", "结果")):
            v = _s(c.get(k))
            if v:
                L.append("  - %s：%s" % (label, v))
        d = _s(n.get("description"))
        if d:
            L.append("  叙述：%s" % d)

    L.append("\n请逐个检查上面每个下游事件是否还与改后的卡衔接、逻辑是否成立。"
             "只报能指出具体冲突的问题；都没有问题就返回空数组。")
    return "\n".join(L)

