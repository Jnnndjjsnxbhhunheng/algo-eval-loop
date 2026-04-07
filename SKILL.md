---
name: algo-eval-loop
description: >
  算法-产品评估迭代工作流 Skill，同时支持对任意 Claude Code Skill 进行自主迭代优化。
  
  【Skill 迭代模式】触发词：给定一个 skill 名称，自动找到该 skill 并开始长时间自主迭代：
  "迭代 xxx skill"、"优化 xxx 这个 skill"、"帮我改进 my-skill"、"自动跑 N 轮迭代 xxx"、
  "让 xxx skill 更好"、"xxx skill 质量不够，帮我迭代"
  
  【评估工作流模式】触发词：评估算法结果、分析产品反馈、迭代算法代码、对比版本差异、
  生成评估报告、把算法输出转成Excel、查看或更新评估标准、跑一版新结果、根据反馈改代码、
  版本对比、badcase分析、产品给了评估表要分析。
---

# algo-eval-loop：Skill 迭代 + 产品评估工作流

## 概览

本 Skill 有两个独立入口：

### 入口 A：Skill 自主迭代（主要场景）

给我一个 skill 名字，我去找到它，然后自主迭代它的 SKILL.md，直到质量收敛。

```
用户："迭代 my-skill"
         │
         ▼
  找到 my-skill 的 SKILL.md
         │
         ▼
┌─────────────────────────────────────────┐
│  Skill 迭代循环（autoresearch 理念）       │
│                                         │
│  读当前 SKILL.md                         │
│       ↓                                 │
│  生成改进版 SKILL.md（LLM 策略分析）       │
│       ↓                                 │
│  git commit 改动                         │
│       ↓                                 │
│  用测试用例评估 skill 质量                 │
│       ↓                          ↓      │
│   质量提升 → 保留          质量下降 → 回滚 │
│       ↓                                 │
│  记录到 results.tsv                      │
│       ↓                                 │
│  继续下一轮 / 达到停止条件                 │
└─────────────────────────────────────────┘
```

### 入口 B：产品评估工作流

管理算法输出 → 产品评估 Excel → 反馈分析 → 代码迭代的完整闭环（详见后文）。

---

## 入口判断

| 用户输入特征 | 对应操作 |
|-------------|---------|
| "迭代 xxx skill"、"优化 xxx"、给了 skill 名称 | → **Skill 自主迭代循环** |
| 提供了算法输出文件或说"结果出来了" | → 格式转换 |
| 提供了产品填写的评估 Excel 或说"反馈回来了" | → 反馈分析 |
| 说"怎么改"、"生成改进方案" | → 迭代方案 |
| 说"改吧"、"跑一下"、确认了方案 | → 执行迭代 |
| 说"对比版本"、"v1 和 v2 差异" | → 版本对比 |
| 说"加个评估维度"、"改评估标准" | → 标准管理 |
| 首次使用或说"新建项目" | → 初始化 |

---

## 操作 A：Skill 自主迭代循环

### 第一步：找到目标 Skill

给定 skill 名称，按以下顺序搜索：
1. `~/.claude/skills/{name}/SKILL.md`
2. `~/.claude/skills/{name}.md`
3. `/mnt/skills/user/{name}/SKILL.md`
4. `/mnt/skills/user/{name}.md`
5. 当前项目目录递归搜索 `{name}/SKILL.md` 或 `{name}.md`

找不到时告知用户，列出已发现的所有 skill 供选择。

### 第二步：理解当前 Skill

读取 SKILL.md 后分析：
- **用途**：这个 skill 是做什么的？
- **触发场景**：什么时候应该被激活？
- **操作流程**：它告诉 Claude 怎么执行？
- **已知缺陷**：描述是否模糊、触发词是否太窄、步骤是否不清晰？

### 第三步：建立测试用例

检查 skill 目录下是否有 `tests/cases.json`。没有则自动生成：
- 根据 skill 描述，构造 3-5 个典型触发场景（输入）
- 为每个场景定义期望行为（expected behavior）
- 定义评分标准（scoring criteria）

格式：
```json
[
  {
    "id": "case_1",
    "input": "用户说的话或提供的文件描述",
    "expected_behavior": "Claude 应该做什么（功能层面，不是字面回答）",
    "scoring_criteria": ["是否触发正确操作", "步骤是否完整", "是否遗漏关键判断"]
  }
]
```

### 第四步：评估当前 Skill 质量（建立基线）

使用 `scripts/skill_eval.py` 对当前 SKILL.md 打分：
- 逐条测试用例分析：当前 skill 指令是否能引导正确行为？
- 打分维度：触发准确性 / 步骤完整性 / 边界处理 / 描述清晰度
- 输出综合分（0-10），写入 `results.tsv` 作为基线

### 第五步：生成改进版 SKILL.md

根据评估发现的问题，生成改进策略：
- **触发词优化**：覆盖更多真实用户表达方式
- **描述精确化**：模糊的操作步骤改为具体的判断树
- **边界处理**：补充"不确定时如何提问"的指导
- **示例增强**：添加更多具体示例降低歧义

每次只改一个方向（小步迭代）。

### 第六步：执行一轮迭代

```
git commit -m "skill-iter {N}: {改动描述}"
→ 评估新版本 SKILL.md
→ 得分提升：保留 commit，记录 advance
→ 得分持平或下降：git reset --hard HEAD~1，记录 revert
→ 追加写入 results.tsv
→ 继续下一轮
```

### 停止条件

- 连续 5 轮无改善（默认，可配置）
- 达到最大轮次（默认 20）
- 综合分达到 8.5/10（可配置目标分）

### 结束后输出

- `skill_iter_summary.md`：迭代历程、最佳版本、改善轨迹
- 询问用户是否要将最佳版本复制回原 skill 目录

---

## 工作目录结构

```
eval-workspace/
├── eval-criteria.md          # 产品评估标准文档
├── skill-iterations/         # Skill 迭代产物
│   └── {skill-name}/
│       ├── tests/
│       │   └── cases.json    # 测试用例
│       ├── results.tsv       # 每轮迭代记录
│       └── skill_iter_summary.md
├── versions/                 # 产品评估版本
│   ├── v1/
│   │   ├── raw_output/
│   │   ├── output.xlsx
│   │   ├── feedback.xlsx
│   │   ├── analysis.md
│   │   └── plan.md
│   └── changelog.md
├── autoloop/                 # 算法自动迭代产物
│   └── {timestamp}/
│       ├── results.tsv
│       └── autoloop_summary.md
└── reports/
    └── comparison_v1_v2.md
```

---

## 操作 B：产品评估工作流

### 格式转换（算法输出 → 产品 Excel）

算法输出可能是 JSON、Markdown、CSV、文本等任意格式。核心目标：**理解内容，转成产品能评估的 Excel**。

**步骤：**
1. 读取并判断格式，理解数据结构
2. 读取 `eval-criteria.md` 获取评估维度（不存在则先问用户）
3. 编写 Python 代码完成转换（openpyxl / pandas）
   - 列名用业务语言
   - 每个维度生成 `[维度名]_评分` 和 `[维度名]_备注` 两列
   - 表头加粗、评估列底色标记、冻结首行
4. 原始输出 → `raw_output/`，转换结果 → `output.xlsx`
5. 告知路径，提醒产品填写后返回

### 反馈分析

**触发**：产品填写完的 `feedback.xlsx` 回来了。

1. 识别评分列（`_评分`、`_备注` 后缀）
2. 统计各维度均分、分布、低分占比
3. Bad case pattern 聚类分析（不逐条罗列，找共性）
4. 生成 `analysis.md`
5. 呈现摘要：总分概况 + Top 3-5 问题 + 退化项（对比上版）

### 迭代方案

基于 `analysis.md` 的问题 pattern，阅读算法代码，生成 `plan.md`：
- 问题归因（pattern → 代码位置）
- 改进措施（具体到函数/逻辑）
- 预期效果
- 风险评估

### 执行迭代 → 验证

备份 → 修改代码（每次一个方向）→ 运行 → 验证 bad cases → 格式转换进入下一轮

### 版本对比

两版 `feedback.xlsx` → 维度得分变化 + 逐条改善/退化 → `comparison_vX_vY.md`

### 评估标准管理

读取 `eval-criteria.md` → 修改 → 记录变更（注明生效版本）

---

## 关键原则

**Skill 迭代以小步为主**：每轮只改一个方向（触发词 / 步骤 / 边界 / 描述），改完评估，退化就回滚。

**格式转换是理解而非搬运**：产品看到的 Excel 应像人工整理的评估表。

**分析找 pattern 而非罗列**：聚类、找共性、给可操作结论。

**版本可追溯**：所有产物（原始输出、Excel、反馈、分析、方案）都保留。

**不要假设入口**：判断不了就问用户。

**自动迭代要安全**：Git 隔离 + 超时保护 + 每步 commit 确保可回滚。
