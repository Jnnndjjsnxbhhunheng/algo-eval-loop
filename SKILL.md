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

对全部 bad cases 各跑一次完整 pipeline，统计 resolved 数量：

```
results = []
for each bad_case in bad_cases:          ← 内层：评估用，跑完所有，不在这里改 skill
    output = 按目标 skill 指令执行完整 pipeline(bad_case["input"])
    resolved = 输出是否解决了 bad_case["pm_note"] 指出的问题？
    results.append(resolved)

baseline_score = sum(results) / len(results) * 10
```

```bash
python skill_loop.py log --tsv results.tsv --round 0 --score <baseline_score> --decision baseline
```

---

### 迭代阶段（外层循环，不停止）

**每一轮的顺序必须是：先改 skill → 再跑全部 cases → 再决定保留/回滚。**

#### Step 1：分析改哪里

看上一轮哪些 cases 还是 unresolved，找共性：
- 多个 unresolved cases 在 pipeline 哪个阶段失败？
- 对应 skill 的哪条指令缺失或有误？
- 本轮选择能覆盖最多 unresolved cases 的改动方向

#### Step 2：修改 skill（只改一个方向）

#### Step 3：Commit

```bash
python skill_loop.py commit <skill目录> -m "skill-iter N: <改动描述>"
```

#### Step 4：评估（内层循环，跑完全部，不在这里改 skill）

```
results = []
for each bad_case in bad_cases:          ← 注意：跑完全部再统计，不要边跑边改
    output = 按更新后的 skill 执行完整 pipeline(bad_case["input"])
    resolved = 输出是否解决了 pm_note 的问题？
    results.append(resolved)

new_score = sum(results) / len(results) * 10
```

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
