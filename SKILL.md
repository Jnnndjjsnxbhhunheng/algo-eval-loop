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

## ⚠️ 评估方式：必须用大模型评估，严禁自写规则脚本

评估 bad case 是否 resolved，**唯一正确方式**是调用 `skill_loop.py evaluate`，它接收真实 pipeline 输出，用大模型语义判断是否 resolved。

**严禁自己编写规则匹配脚本来评估**，例如：
- 用正则提取 PM 备注中的品牌名，再检查输出里有没有
- 用 `resolved_issues >= total_issues * 0.5` 这类硬编码阈值判断
- 写任何 Python/bash 脚本替代 `skill_loop.py evaluate`

**为什么规则评估不可用**：
- 正则提取不准确：`"并未上榜, 但排不进top3"` 会产生噪声
- 无法理解语义：`"但排不进top3"` ≠ `"未上榜"`，规则无法区分
- 阈值是拍脑袋：`>= 0.5` 没有任何依据

**大模型评估的优势**：能理解语义、准确提取实体、权衡多因素、给出可解释的理由。

`skill_loop.py` 是固定基础设施，**不得在迭代过程中修改**。如需改进评估逻辑，由用户决定，不由迭代自主修改。

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
  --feedback <feedback.xlsx路径> --threshold 3.0 --max 15 \
  > bad_cases.json
```

输出 JSON，每条包含 `input`、`dimension`、`pm_score`、`pm_note`。
**这份列表在整个迭代过程中固定不变，每轮都用同一批 cases 打分。**

**2. 找到目标 skill，并完整探索整个系统**

搜索顺序：`~/.claude/skills/{name}/` → `/mnt/skills/user/{name}/` → 当前项目目录。

**⚠️ 只读 SKILL.md 是不够的。** 在做任何修改决定之前，必须完整理解目标 skill 的整个系统——读 SKILL.md 引用的所有脚本、工具库、后端服务、中间文件，理解数据从输入到输出的完整路径。

**探索完成的标志**：能回答——
1. 一条 bad case 的输入，经过哪些模块，最终变成什么输出？
2. 问题出在哪一层？是 prompt 指令问题、脚本逻辑问题，还是底层服务的实现问题？
3. 改 SKILL.md 能解决，还是需要改其他层？

**常见错误**：只看 SKILL.md 就开始改，把所有问题归因于"模型没执行好"，忽略了其他层面的设计缺陷。

**3. 建立基线（Round 0）**

并发调用 Agent API 跑全部 bad cases，再用 LLM API 评判结果：

```bash
# 并发跑完 pipeline 后，将结果（含 actual_output 字段）保存为 pipeline_results.json
# 然后并发评估，输出 eval_results.json 并打印得分
python skill_loop.py evaluate \
  --results pipeline_results.json \
  --batch 10 \
  --model $OPENAI_MODEL \
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
- **表象**：某个 case 的输出结果不对
- **根因**：pipeline 的哪个环节导致了这个结果？是 prompt 指令问题、逻辑分支问题，还是底层工具的实现问题？

**修改方向必须指向根因，而非表象。**

**⚠️ 简洁性原则：复杂度成本必须匹配改进幅度**

- 增加大量代码只换来微小提升？不值得，丢弃
- 删除代码获得同等甚至更好的结果？必须保留
- 大幅简化但提升接近 0？保留

pipeline 结构调整尤其要遵守这条——重组步骤、增加中间层、调整数据传递方式，都必须能解释"为什么这个结构改动比局部微调更有效"。如果说不清楚，先从局部改动入手。

**⚠️ 平台期策略：连续 3 轮未超越 best → 必须升级改动幅度**

`skill_loop.py log` 会自动检测：连续 3 轮 decision=revert（无论是追平还是回退），打印平台期警告。

**收到警告后，Step 1 的分析方向必须改变**：
- 禁止继续在同一层面微调（改 prompt 措辞、调小参数、换例子）
- 必须升级到结构级改动：重组处理步骤、调整数据流、增删中间环节
- 结构级改动仍须满足简洁性原则和 overfit 约束

#### Step 2：修改 skill（只改通用规则，严禁硬编码答案）

**⚠️ 这是最重要的约束——overfit 检测**

每次修改 skill 前，必须自问：**这个修改只对 bad cases 中出现的具体输入有效，还是对所有输入都有效？**

**严禁的修改方式（overfit / 抄答案）**：

1. **为特定输入加硬编码数据**：把 bad case 的正确答案直接写进 skill（如参考表、示例列表）
2. **为特定输入加专项规则**：针对某个 case 的特征写判断条件，其他 case 不受益
3. **为特定输入加输出示例**：把 PM 给的正确结果直接作为 few-shot 例子写进去

这些修改本质上是把答案抄进 skill，只对见过的输入有效。

**正确的修改方式（通法 / generalize）**：

修改应该是**输入无关的通用改进**，让 pipeline 对所有输入都变强：

1. **改 prompt 指令**：如果模型在某个阶段判断力不足 → 改通用判断标准，而非加特例说明
2. **改处理逻辑**：如果某个阶段的处理方式系统性地有问题 → 改通用逻辑
3. **改检查点**：如果某个 checkpoint 缺失或不够严格 → 加通用验证步骤

**自检**：写完修改后做思想实验——把 bad cases 的输入全换成没见过的新输入，这次修改还有效吗？如果不能，说明这是特解，不是通法。

**唯一例外**：如果 skill 中存在某类参考数据表，且覆盖面不足是系统性问题，可以批量扩充——但必须一次性大量补充，而非只补 bad cases 涉及的少数条目。

#### Step 3：Commit（必须通过 overfit 自检才能提交）

```bash
python skill_loop.py commit <skill目录> \
  --change-type <类型> \
  -m "skill-iter N: <改动描述>"
```

`--change-type` 只允许三个值，填其他值（如 `data`、`example`）命令直接报错退出：
- `prompt_instruction`：改了模型的 prompt 或判断指令
- `logic`：改了处理逻辑、流程分支
- `checkpoint`：改了验证步骤或检查点

**如果无法归入这三类，说明是 data/example 类的 overfit 修改，回到 Step 1 重新分析根因。**

#### Step 4：评估（脚本并发跑，batch=10，不在这里改 skill）

```bash
# pipeline 并发跑完后，把结果（含 actual_output 字段）保存为 pipeline_results.json
# 再用 evaluate 并发评判，内部 10 并发，自动统计分数
python skill_loop.py evaluate \
  --results pipeline_results.json \
  --batch 10 \
  --model $OPENAI_MODEL \
  --output eval_results.json
# stdout: {"score": X.X, "resolved": N, "total": M}
```

> 原理：并发调用 LLM API（`asyncio.Semaphore(10)` 控制并发量），
> 每条 case 独立调用一次 GLM-5 做语义判断，全部完成后汇总分数。
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

## 工作目录

```
eval-workspace/
└── {skill-name}/
    └── results_YYYYMMDD.tsv    # 每轮迭代记录（round/score/decision/hash）
```
