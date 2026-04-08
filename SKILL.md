---
name: algo-eval-loop
description: >
  基于 PM 的 feedback.xlsx，对目标 skill 进行真实 pipeline 测试 + 自主迭代优化。
  给定 feedback.xlsx + skill 名称，提取 bad cases，跑真实 pipeline，对比输出，
  持续修改 skill 直到 bad cases 被解决。

  触发词：
  "帮我根据反馈迭代 xxx skill"、"PM 给了反馈，帮我改进 my-skill"、
  "feedback 回来了，开始迭代"、"根据评估结果优化 xxx"、
  "badcase 分析 + 迭代 skill"、"帮我看看反馈然后改 skill"
---

# algo-eval-loop

## 核心理念（对标 karpathy/autoresearch）

```
autoresearch:  修改 train.py → python train.py（跑全部训练数据）→ 读 val_bpb → 保留/回滚
本 skill:      修改 skill   → 跑全部 bad cases → 算总分            → 保留/回滚
```

**关键：每次修改 skill 后，必须跑【全部】bad cases，用总分决定保留/回滚。**
不是"修改 skill → 跑一个 case → 继续改"，而是"修改 skill → 跑全部 → 算总分 → 改下一轮"。

---

## ⚠️ 两个循环，主次分明

```
【外层：skill 迭代循环】← 这是主循环，每轮改一次 skill
    修改 skill（一个方向）
    ↓
    【内层：评估循环】← 这只是打分手段，不是迭代单位
        for bad_case_1: 跑 pipeline → resolved?
        for bad_case_2: 跑 pipeline → resolved?
        for bad_case_3: 跑 pipeline → resolved?
        ...所有 bad cases 跑完
    ↓
    score = resolved_count / total × 10
    ↓
    score 提升 → 保留，进入下一轮外层迭代
    score 不变/下降 → 回滚，换方向，进入下一轮外层迭代
```

**绝对不能**：在内层循环中发现某个 case 还没解决就去修改 skill——那是把内层当成了外层。

---

## ⚠️ 自主运行指令

**一旦循环开始，绝对不要停下来询问用户是否继续。**
循环一直跑，直到：
- 连续 5 轮 score 无改善（收敛）
- score ≥ 8.5（= 85% bad cases 被解决）
- 达到最大轮次（默认 20）
- 用户主动中断

---

## 完整流程

### 准备阶段（只做一次）

**1. 加载全部 bad cases**

```bash
python ~/.claude/skills/algo-eval-loop/scripts/skill_loop.py load-cases \
  --feedback <feedback.xlsx路径> --threshold 3.0 --max 15
```

输出 JSON，每条包含 `input`、`dimension`、`pm_score`、`pm_note`。
**这份列表在整个迭代过程中固定不变，每轮都用同一批 cases 打分。**

**2. 找到目标 skill**

搜索顺序：`~/.claude/skills/{name}/` → `/mnt/skills/user/{name}/` → 当前项目目录。
读取 SKILL.md 及所有子文件，理解 pipeline 结构。

**3. 建立基线（Round 0）**

用脚本并发跑全部 bad cases（batch=10，自动启动 10 个并行 `claude -p` 子进程）：

```bash
# 先把 bad cases 保存到文件
python skill_loop.py load-cases --feedback <feedback.xlsx路径> --threshold 3.0 --max 15 \
  > bad_cases.json

# 并发评估，输出 eval_results.json 并打印得分
python skill_loop.py evaluate \
  --cases bad_cases.json \
  --skill-path <目标skill目录> \
  --batch 10 \
  --output eval_results.json
# stdout: {"score": X.X, "resolved": N, "total": M}
```

```bash
python skill_loop.py log --tsv results.tsv --round 0 --score <score> --decision baseline
```

---

### 迭代阶段（外层循环，不停止）

**每一轮的顺序必须是：先改 skill → 再跑全部 cases → 再决定保留/回滚。**

#### Step 1：分析改哪里（通法 vs 特解）

看上一轮哪些 cases 还是 unresolved，找共性：
- 多个 unresolved cases 在 pipeline 哪个阶段失败？
- 对应 skill 的哪条指令缺失或有误？
- 本轮选择能覆盖最多 unresolved cases 的改动方向

**⚠️ 核心原则：改规则，不改数据。找通法，不做特解。**

分析 bad cases 时，必须区分两个层次：
- **表象**：散粉品类缺了 NARS、按摩仪排序不对
- **根因**：为什么 pipeline 漏掉了这些品牌？是搜索策略不够？是过滤规则太严？是排序权重有问题？

**修改方向必须指向根因，而非表象。**

#### Step 2：修改 skill（只改通用规则，严禁硬编码答案）

**⚠️ 这是最重要的约束——overfit 检测**

每次修改 skill 前，必须自问：**这个修改只对 bad cases 中出现的品类有效，还是对所有品类都有效？**

**严禁的修改方式（overfit / 抄答案）**：

1. **为特定品类加数据行**：
   - ✗ bad case 说"散粉缺 NARS" → 在品类参考表里加一行 `| 散粉 | NARS、玫珂菲... |`
   - ✗ bad case 说"按摩仪缺攀高" → 在品类参考表里加一行 `| 按摩仪 | SKG、倍轻松... |`
   - 这是把答案抄进 skill，下次遇到"净水器"还是会错

2. **为特定品类加排序示例**：
   - ✗ `散粉榜：NARS 应排在花西子前面`
   - ✗ `充电宝榜：小米应排在安克前面`
   - 这是直接看了 PM 给的正确答案，硬编码进去

3. **为特定品类加过滤规则**：
   - ✗ `散粉品类应排除 AKF`
   - ✗ `水杯品类应排除苏泊尔`
   - 这只解决了见过的品类，没见过的品类不会受益

**正确的修改方式（通法 / generalize）**：

修改应该是**品类无关的通用规则改进**，让 pipeline 对所有品类都变强：

1. **改搜索策略**：如果多个品类都漏品牌 → 改搜索 query 的生成方式、增加搜索轮次、改搜索关键词模板
2. **改过滤规则**：如果过滤阶段误杀主流品牌 → 放宽过滤条件的阈值、增加通用的保留规则
3. **改排序权重**：如果排序普遍不合理 → 调整品牌档次权重比例、改排序算法的通用逻辑
4. **改检查点指令**：如果模型在某个 checkpoint 判断力不足 → 改 prompt 中的通用判断标准
5. **改方法论**：如果品牌补充总是不够 → 改"如何补充遗漏品牌"的通用方法描述

**自检**：写完修改后，做一次思想实验——如果把 bad cases 的品类全换成没见过的品类（如"净水器"、"行李箱"、"猫粮"），这次修改还能帮助 pipeline 做得更好吗？如果不能，说明这是特解，不是通法。

**唯一例外**：如果 skill 本身就有一个"品类参考表"设计，且分析后确认该表的覆盖面不足是系统性问题，可以批量扩充表格——但必须一次性补充大量品类（≥10个），而非只加 bad cases 涉及的 1-2 个品类。

#### Step 3：Commit

```bash
python skill_loop.py commit <skill目录> -m "skill-iter N: <改动描述>"
```

#### Step 4：评估（脚本并发跑，batch=10，不在这里改 skill）

```bash
# 一条命令跑完全部 bad cases，内部 10 并发，自动统计分数
python skill_loop.py evaluate \
  --cases bad_cases.json \
  --skill-path <目标skill目录> \
  --batch 10 \
  --output eval_results.json
# stdout: {"score": X.X, "resolved": N, "total": M}
```

> 原理：每条 case 独立启动 `claude -p` 子进程执行完整 pipeline，
> `asyncio.Semaphore(10)` 控制并发量，全部跑完后再汇总分数。
> 详细结果（含每条 reason）保存在 `eval_results.json`。

#### Step 5：保留或回滚

```bash
# new_score > 上轮 score → 保留
python skill_loop.py log --tsv results.tsv --round N --score <new_score> --decision advance --hash <hash>

# new_score ≤ 上轮 score → 回滚
python skill_loop.py revert
python skill_loop.py log --tsv results.tsv --round N --score <new_score> --decision revert
```

#### Step 6：直接进入下一轮，不询问用户

---

## Pipeline 执行要点

- **严格按目标 skill 的指令执行全部阶段**，不要跳步骤
- **使用目标 skill 需要的 MCP 工具**（如 search_notes、extract_entities 等）
- **判断 resolved 时只看 pm_note**：PM 说"缺少主流品牌"，就看输出里有没有主流品牌

---

## 工作目录

```
eval-workspace/
└── {skill-name}/
    └── results_YYYYMMDD.tsv    # 每轮迭代记录（round/score/decision/hash）
```
