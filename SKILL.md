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
autoresearch:  修改 train.py → python train.py → 读 val_bpb → 保留/回滚
本 skill:      修改 skill   → 跑真实 pipeline → 对比输出  → 保留/回滚
```

**Claude 是执行者**：读目标 skill → 用 MCP 工具跑真实 pipeline → 打分 → 改 skill → 循环。
`skill_loop.py` 只提供基础设施（加载 bad cases、git 操作、记录结果），不做评估。

---

## ⚠️ 自主运行指令

**一旦循环开始，绝对不要停下来询问用户是否继续。**
循环一直跑，直到：
- 连续 5 轮 score 无改善（收敛）
- score ≥ 8.5（达标，= 85% bad cases 被解决）
- 达到最大轮次（默认 20）
- 用户主动中断

如果某轮没有改进思路，**不要停**：重读 feedback 备注列，尝试更激进的改动，组合之前接近有效的方向。

---

## 完整流程

### 第一步：加载 bad cases

```bash
python ~/.claude/skills/algo-eval-loop/scripts/skill_loop.py load-cases \
  --feedback <feedback.xlsx路径> \
  --threshold 3.0 \
  --max 15
```

输出 JSON 格式的 bad cases，每条包含：
- `input`：PM 评估时的输入数据（原始列内容）
- `dimension`：哪个评估维度低分
- `pm_score`：PM 给的分（< 3.0）
- `pm_note`：PM 的备注说明

**把这份 bad cases 列表保存在上下文中，后续每轮复用。**

---

### 第二步：找到目标 skill

搜索顺序：
1. `~/.claude/skills/{name}/`
2. `/mnt/skills/user/{name}/`
3. 当前项目目录递归搜索

读取该 skill 目录下的所有文件（SKILL.md、子 MD、代码等），理解它的 pipeline 结构。

---

### 第三步：建立基线（第 0 轮）

对每条 bad case 执行真实 pipeline，记录当前得分：

```
for each bad_case in bad_cases:
    1. 读取目标 skill 的 SKILL.md，理解 pipeline 各阶段
    2. 用 bad_case["input"] 作为输入，按 skill 指令执行完整 pipeline
       （调用目标 skill 需要的 MCP 工具、Bash 命令等）
    3. 收集 pipeline 输出结果
    4. 对比 PM 期望（bad_case["pm_note"] 描述了问题所在）
    5. 判断：当前输出是否解决了 PM 指出的问题？→ resolved: true/false

baseline_score = resolved_count / total_bad_cases * 10
```

记录基线：
```bash
python skill_loop.py log --tsv results.tsv --round 0 \
  --score <baseline_score> --decision baseline
```

---

### 第四步：迭代循环（不停止，直到收敛）

每一轮：

**1. 分析本轮改哪里**

从上一轮 unresolved cases 中找共性：
- 哪类输入在 pipeline 哪个阶段出了问题？
- 是 skill 的哪条指令（或缺失的指令）导致的？
- 本轮选择覆盖最多 unresolved cases 的方向

**2. 修改 skill 文件**（只改一个方向）

**3. Commit**
```bash
python skill_loop.py commit <skill目录> --message "skill-iter N: <改动描述>"
```

**4. 重跑 bad cases，计算新得分**

```
for each bad_case in bad_cases:
    按更新后的 skill 指令重新执行 pipeline
    → resolved: true/false

new_score = resolved_count / total_bad_cases * 10
```

**5. 保留或回滚**
```bash
# 改善 → 保留，记录
python skill_loop.py log --tsv results.tsv --round N \
  --score <new_score> --decision advance --hash <commit_hash>

# 未改善 → 回滚，记录
python skill_loop.py revert
python skill_loop.py log --tsv results.tsv --round N \
  --score <new_score> --decision revert
```

**6. 直接进入下一轮，不询问用户**

---

## Pipeline 执行要点

对 bad case 执行 pipeline 时：

- **严格按目标 skill 的指令执行**，不要跳步骤
- **使用目标 skill 需要的 MCP 工具**（如 search_notes、extract_entities 等）
- **记录每个阶段的中间输出**，方便定位问题出在哪一步
- **判断 resolved 时对比 PM 的 pm_note**，不是泛泛评价输出质量

---

## 工作目录

```
eval-workspace/
├── versions/
│   └── vN/
│       └── feedback.xlsx       # PM 评估反馈（输入）
└── {skill-name}/
    └── results_YYYYMMDD.tsv    # 每轮迭代记录
```

---

## 关键原则

**评估 = 跑真实 pipeline**：不是看 skill 文字写得好不好，而是用 bad case 输入真正执行，看输出是否解决了 PM 的问题。

**每次只改一个方向**：小步迭代，改完立刻测试，退化就回滚。

**score = resolved / total × 10**：直接对应"解决了多少 PM 反馈的问题"。

**不要假设入口**：如果缺少 feedback.xlsx 或 skill 名称，直接问用户。
