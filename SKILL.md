---
name: algo-eval-loop
description: >
  基于 PM 的 feedback.xlsx，自动分析问题并迭代目标 skill（SKILL.md、代码、子MD等）的工作流。
  给定 feedback.xlsx + skill 名称，自动找到该 skill，分析反馈中的 bad case pattern，
  持续迭代改进 skill 直到质量收敛。

  触发词：
  "帮我根据反馈迭代 xxx skill"、"PM 给了反馈，帮我改进 my-skill"、
  "分析这份反馈然后迭代 skill"、"feedback 回来了，开始迭代"、
  "根据评估结果优化 xxx"、"badcase 分析 + 迭代 skill"、
  "帮我看看反馈然后改 skill"、"产品给了评估表，迭代一下"
---

# algo-eval-loop

## 核心理念（借鉴 karpathy/autoresearch）

```
feedback.xlsx（PM质量信号）
      ↓
分析 bad case patterns → 找到 skill 哪里有问题
      ↓
改进目标 skill 文件（每次一个方向）
      ↓
git commit → 用 patterns 代理评估新版本
      ↓
改善 → 保留；退化 → git reset 回滚
      ↓
循环，直到收敛
```

**skill_loop.py 只负责 git 状态管理**，分析和改进全由 Claude 完成。

---

## 完整流程

### 第一步：准备

**用户需要提供：**
1. `feedback.xlsx` — PM 填写的评估表（唯一必须输入）
2. 目标 skill 名称（或路径）

**立即执行：**
```bash
python scripts/skill_loop.py <skill名称> --feedback <feedback.xlsx路径>
```

---

### 第二步：分析 feedback.xlsx（建立基线）

从 feedback.xlsx 提取质量信号：

```python
# Claude 按需写内联代码完成以下分析
import openpyxl

# 1. 识别评估列（_评分、_备注 后缀，或从列名推断）
# 2. 计算各维度均分、低分占比
# 3. 提取 bad case 的备注内容
# 4. 聚类找 pattern（不逐条罗列，找共性）
```

**输出分析摘要：**
- 综合得分（作为本次迭代基线）
- Top 3 bad case patterns（这是迭代方向的来源）
- 问题归因：哪些 pattern 是 skill 描述/逻辑导致的

**示例：**
```
基线综合分：6.2/10
Top patterns：
  1. 触发词太窄，用户说"帮我看看"时 skill 没响应（占 34% 低分case）
  2. 步骤3描述模糊，Claude 跳过了关键判断（占 28% 低分case）
  3. 边界情况未处理，直接报错而非提问（占 18% 低分case）
归因：pattern 1 → SKILL.md description
     pattern 2 → SKILL.md 操作步骤第3条
     pattern 3 → SKILL.md 关键原则缺失
```

---

### 第三步：确定目标 skill

按以下顺序搜索目标 skill：
1. `~/.claude/skills/{name}/`
2. `/mnt/skills/user/{name}/`
3. 当前项目目录递归搜索

迭代对象包括该目录下的所有相关文件：
- `SKILL.md` — 触发词、操作步骤（最常改）
- `*.py` / `*.ts` — 工具代码
- `references/*.md` — 参考文档
- `templates/*.md` — 模板文件

---

### 第四步：迭代循环

每一轮：

**1. 确定本轮改哪里**

根据 bad case patterns 和当前得分选择改进方向：
```
pattern 覆盖率最高的问题 → 优先改
同分时 → 改最容易验证的
已改过的方向没效果 → 换下一个 pattern
```

**2. 修改 skill 文件**（只改一个方向）

**3. skill_loop.py 接管**：
```
git commit 变更
      ↓
代理评估：新版 skill 能覆盖多少 bad case patterns？
（LLM 判断：改动是否针对性地解决了归因的问题）
      ↓
得分提升 → ✅ 保留 commit
得分持平/下降 → ❌ git reset 回滚，换方向
      ↓
追加写入 results.tsv
```

**4. 进入下一轮**

---

### 第五步：收敛与交付

**停止条件：**
- 连续 5 轮无改善
- 代理得分 ≥ 8.5
- 达到最大轮次（默认 20）

**输出：**
- 改进后的 skill 文件（已在 git 历史中）
- `results.tsv` — 每轮迭代记录
- 迭代摘要：基线分 → 最终分，改了哪些方向，哪些有效

**下一轮循环：**
改进后的 skill 运行产出新结果 → PM 评估 → 新 feedback.xlsx → 再次触发本流程

---

## 代理评估逻辑（每轮打分）

不等 PM 反馈，用以下代理指标衡量每轮改动质量：

1. **Pattern 覆盖率**（主要）：改动后的 skill 描述，是否能处理分析出的 bad case patterns？
   - 用 LLM 判断（需要 ANTHROPIC_API_KEY）
   - 无 API Key 时用规则：关键词覆盖率

2. **描述质量**（辅助）：触发词数量、步骤完整性、边界处理

综合分 = 覆盖率得分 × 0.7 + 描述质量得分 × 0.3

---

## 工作目录

```
eval-workspace/
├── versions/
│   └── vN/
│       ├── feedback.xlsx     # PM 评估反馈（输入）
│       ├── analysis.md       # 自动生成的分析报告
│       └── patterns.json     # 提取的 bad case patterns（供迭代循环使用）
└── {skill-name}/
    └── results_YYYYMMDD.tsv  # 迭代记录
```

---

## 关键原则

**feedback.xlsx 是唯一必须输入**：不需要预配置 eval-criteria.md，从列名直接推断维度。

**分析找 pattern 而非罗列**：几十条 bad case 逐条列出无意义，聚类找共性。

**skill 改动以小步为主**：每轮只改一个方向，git 保证可回滚。

**Claude 是执行者**：分析、归因、改进都由 Claude 完成；skill_loop.py 只管 git 状态。

**迭代方向来自 bad case**：不是随机优化，每一步都对应具体的反馈问题。
