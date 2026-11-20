# kernel-kb

一个**只读投影**式的 Kernel 迭代知识库。把不可变的实验事实（节点、边、评测）投影成一个
SQLite 文件，再加一张受控的「优化手段」词表。

**人和 agent 读同一份数据。** 人用 SQL 和 Datasette；agent 用 `digest` 和 `card`。

> 它不是一个服务，是一个文件。`kb.db` 可以随时删掉，`rebuild` 从 run 目录重算。

---

## 30 秒

```bash
# 1. 构建（幂等，可反复跑）
python3 kb/kb.py rebuild --runs runs --profiles configs

# 2. 看某个算子现在到哪了
python3 kb/kb.py digest --operator sim_copy

# 3. 看某个节点做了什么
python3 kb/kb.py card sim_copy_0000

# 4. 临时查询
python3 kb/kb.py q "select * from v_tech_lift"
```

数据库默认写在 `kb/kb.db`。

---

# Part A · 给 Agent

> 如果你是一个正在迭代 kernel 的 agent，**只读这一节就够了**。

## A1. 硬性规则

1. **只读。** 唯一的写操作是 `rebuild`（幂等）和 `tech add` / `tech merge`。不要 `INSERT` 到别的表，不要改 `kb.db`。
2. **规划前先 `digest`。** 不要跳过——它是你唯一应该相信的历史经验来源。
3. **改代码前先 `card <node>`。** 确认父节点做了什么、为什么没通过。
4. **不要直接读 `runs/` 下的原始 JSON**，除非 `card` 里给了具体路径。原始文件没有聚合，读了会浪费上下文。
5. **任何结论都要带 `node_id` 或 `evaluation_id`。** 没有引用来源的判断不要写进 plan。
6. **区分 claim 和 fact。** `hypothesis` 是 agent 写的主张；`verdict` 和 `speedup` 是 evaluator 产出的事实。不要把前者当后者用。
7. **`technique_id` 不确定时先查词表**，不要现场编一个新名字。见 A3。

## A2. 任务 → 命令

| 我想知道 | 命令 |
|---|---|
| 这个算子目前最好到哪 | `kb.py digest --operator <op>` |
| 这个节点具体改了什么 | `kb.py card <node> --format json` |
| 某个手段试过没有 | `kb.py q "select * from v_tech_lift where technique_id like '%swizzle%'"` |
| 哪些方向已经失败 | `digest` 的「已否方向」段 |
| 别的 lane 有什么能抄 | `digest` 的「跨 lane 候选」段 |
| 跨厂商对比 | `kb.py q "select * from v_tech_lift order by technique_id"` |
| 有几个可比域 | `kb.py lanes` |
| 词表里有哪些手段 | `kb.py tech list` |

## A3. 词表使用方法

- 先 `kb.py tech list`，用 `grep` 或 `description` 找最接近的条目。
- 找到了 → 直接用那个 `technique_id`，把你的具体取值放进 `params`（例如 `{"num_warps": 8}`）。
- 找不到 → **用「族.具体动作」命名**，例如 `mem.ldmatrix_layout`、`sched.swizzled_grid`。族只能取：
  `memory_access` / `launch_config` / `parallelism` / `scheduling` / `algorithm` / `fusion` / `precision` / `other`。
- 你的新名字会在下一次 `rebuild` 时自动进入词表（`status=draft`）。不要手动 add，除非你确定它值得长期保留。

## A4. `digest` 的输出契约

固定四段，顺序不变：

```
## <operator> @ <vendor>/<device> · <dtype> · <protocol>
lane `<id>` · N nodes
champion `<node>` X.XXx
path: <root> → ... → <champion>

### 本 lane 已验证手段 (n≥2)
| technique | n | win | avg |

### 已否方向（最近 N）
- `<node>` <STATUS>：<hypothesis> → <reason>

### 跨 lane 候选（别处有效、本 lane 未试）
- `<technique>` — <vendor>/<device>: win X / avg X.XXx (n=N)
```

解读要点：

- `win` = `speedup ≥ 1.05` 的比例。`n` 小于 2 的不要当结论。
- 「已否方向」比「已验证手段」更值得读。不要重复踩坑。
- 「跨 lane 候选」是迁移机会，但**必须先在本 lane 复测**，不同芯片上的收益不可直接相加。

## A5. 一次完整迭代的推荐顺序

```bash
python3 kb/kb.py digest --operator moe_align_block_size     # 1. 读经验
python3 kb/kb.py card <champion>                            # 2. 看当前最好版本改过什么
# ... 写 plan，semantic_changes 里带 technique_id ...
# ... runner 跑完，driver 写 evaluations/ ...
python3 kb/kb.py rebuild --runs runs --profiles configs     # 3. 重新投影
python3 kb/kb.py card <new_node>                            # 4. 复核
```

---

# Part B · 给人

## B1. 它读什么

```
runs/<run>/config.json               → lane（算子/dtype/shape/协议）
runs/<run>/map/nodes/*.json          → node + change
runs/<run>/map/edges.jsonl           → edge
runs/<run>/map/evaluations/*.json    → evaluation + measurement
configs/*.json  (DeviceProfile)      → vendor / device / software stack
```

**它不写 run 目录。** 原始文件仍然是唯一事实来源。

## B2. 数据模型

| 表 / 视图 | 一行是什么 | 为什么要它 |
|---|---|---|
| `lane` | 一个可比域（算子×dtype×shape×硬件×软件栈×协议） | 没有它，跨芯片的 speedup 无法比较 |
| `node` | 一次不可变实验 | 状态 |
| `edge` | 代码谱系（`DERIVED_FROM`/`COMBINES`/…） | 变化 |
| `change` | 某节点应用了某个 `technique_id` | 手段 |
| `evaluation` | 一次判定（ACCEPT/REJECT/INVALID） | 结论 |
| `measurement` | 某次判定下某个 workload 的数字 | 证据 |
| `technique` | 受控词表条目 | 索引 |
| `v_tech_lift` | 手段 × lane 的 n / 胜率 / 平均加速 | 挖掘入口 |
| `v_champion` | 每个 lane 当前未被取代的 ACCEPT 节点 | 现状 |

记忆口诀：**状态在节点，变化在边，证据在观测，索引在词表。**

## B3. 事实 vs 词表

- `lane` / `node` / `edge` / `change` / `evaluation` / `measurement` 是**派生表**：每次 `rebuild` 全量重建。
- `technique` 是**累积表**：`rebuild` 只插入，从不删除。人工标 `stable` / 合并撤销都保留。

所以：**事实永远可以重算，词表是资产，要备份。**

## B4. 维护词表

```bash
python3 kb/kb.py tech list
python3 kb/kb.py tech add mem.ldmatrix_layout --family memory_access \
    --description "Load tiles through ldmatrix to avoid per-element shared loads"
python3 kb/kb.py tech merge mem.vectorize_load --into mem.vectorized_load
python3 kb/kb.py tech merge shared_mem_padding --into mem.swizzled_shared_layout
```

自动进入词表的 draft 条目会越来越多，**每月花十分钟合并同义词**就够了。这是唯一需要人工维护的部分。

## B5. UI

```bash
pip install datasette
datasette kb/kb.db --open
```

直接能浏览所有表和视图，每个页面都有 `.json` 版本，agent 也可以调。

---

# Part C · 边界

明确不做：

| 不做 | 原因 |
|---|---|
| 图数据库 / Cypher | 几千节点，递归 CTE 足够 |
| Web 后端 / 前端框架 | Datasette 够用 |
| embedding / 向量检索 | 结构化字段能答的，不需要 |
| event sourcing / 事件重放 | run 目录本身就是日志 |
| `finding` 表 / `INFLUENCED_BY` 边 | 等真的需要再加 |

**不存的东西**：代码、diff 正文、原始日志。它们留在 run 目录和 git 里，`node.patch_path` 只做引用。

---

# Part D · 独立使用 / 迁移

整个 `kb/` 目录是自包含的，**只依赖 Python 标准库**：

```
kb/
├── README.md            # 本文件
├── kb.py                # CLI
├── project.py           # 摄入
├── digest.py            # agent 摘要
├── schema.sql           # 表结构
├── techniques.json      # 词表种子
├── kb.db                # 生成物（可删）
└── tests/test_kb.py
```

拷走后：

```bash
cp -r kb/ /somewhere/else/kb
cd /somewhere/else
python3 kb/kb.py --db kb/kb.db rebuild --runs <你的 runs 目录>
```

只要 run 目录结构符合 B1，就能工作。**没有隐藏的包依赖，没有环境变量。**

> `kb.db` 默认与代码同目录。如果把它放到别处，所有命令都要带 `--db <path>`。

---

## 附：为什么没有「借鉴边」

一开始设计过 `INFLUENCED_BY` 之类的知识边，最后删掉了。原因：

- 「借鉴了谁」需要 agent 在写入时做主观判断，是最容易变脏的数据；
- 真正要回答的问题（某手段在什么条件下有效、能否迁移）都是 `technique × lane` 聚合，不需要点对点归因；
- 词表里的 `aliases` + `applies_when` 已经能表达"同一手段的不同实现"。

如果将来确实需要归因，再加一张 `finding` 表或一类边，不要现在提前建。
