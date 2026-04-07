---
name: algo-eval-loop
description: >
  对任意 Claude Code Skill 进行自主迭代优化的工作流。给定 skill 名称，
  自动找到该 skill（SKILL.md、代码、子 MD 等），持续迭代改进直到质量收敛。
  同时支持产品评估工作流：分析 PM 的 feedback.xlsx，生成迭代方案。

  【Skill 迭代】触发词：
  "迭代 xxx skill"、"优化 xxx"、"帮我改进 my-skill"、
  "自动跑 N 轮迭代 xxx"、"让 xxx skill 更好"

  【产品评估】触发词：
  "分析反馈"、"PM 发来了评估结果"、"这轮 badcase 有哪些"、
  "生成迭代方案"、"对比 v1 和 v2"、"帮我看看 feedback"
---

# algo-eval-loop

## 核心理念（借鉴 karpathy/autoresearch）

autoresearch 的精髓：**agent 自己就是执行者**，不需要预建工具链。
```
program.md（自然语言指令）→ agent 读懂 → agent 用 git + shell 自主执行循环
```

本 skill 同理：SKILL.md 是指令，Claude 是执行者，`skill_loop.py` 只负责 git 状态管理。

---

## 入口 A：Skill 自主迭代

### 触发后立即做的事

1. **找到目标 skill 目录**（含 SKILL.md、代码、子 MD 等）
   - 搜索顺序：`~/.claude/skills/{name}/` → `/mnt/skills/user/{name}/` → 当前项目目录
   - 找不到时列出所有可发现的 skill 供用户选择

2. **读懂目标 skill** — 分析：
   - 用途和触发场景
   - 当前描述的模糊点或缺失步骤
   - 是否有代码文件需要一起迭代

3. **启动 skill_loop.py**（迭代 harness）：
   ```bash
   python scripts/skill_loop.py <skill名称> --rounds 20 --target 8.5
   ```

4. **进入循环**，每一轮：
   - 分析上一轮评估结果（或基线），确定本轮改哪个方向
   - 修改 skill 文件（SKILL.md / 代码 / 子 MD，**每次只改一个方向**）
   - 在 skill_loop.py 提示处按 Enter 确认
   - harness 自动 commit → 评估 → 保留/回滚
   - 根据结果决定下一轮方向

### 每轮改哪里的判断逻辑

```
评分 < 6   → 优先修触发词（description 中的触发场景太窄）
评分 6-7   → 优先补步骤（操作路径不完整或有歧义）
评分 7-8   → 优先加边界处理（不确定时的提问策略）
评分 8-8.5 → 精炼示例（让描述更具体、减少歧义）
评分 > 8.5 → 收敛，询问用户是否继续
```

### 迭代对象不只是 SKILL.md

目标 skill 可能包含：
- `SKILL.md` — 触发词、操作步骤（必改）
- `*.py` / `*.ts` — 工具代码（按需改）
- `references/*.md` — 参考文档（按需改）
- `templates/*.md` — 模板（按需改）
- `tests/cases.json` — 测试用例（按需补充）

每次 commit 包含这一轮的所有变更文件，harness 统一 commit/revert。

### 停止条件

- 连续 5 轮无改善（收敛）
- 达到目标分 8.5/10
- 达到最大轮次（默认 20）
- 用户输入 `q`

---

## 入口 B：产品评估工作流

**最小启动**：只需一个 `feedback.xlsx`（PM 填写的评估表）。

### 分析 feedback.xlsx

1. 读取 Excel，自动识别评估列（`_评分`、`_备注` 后缀）
2. 统计各维度均分、低分占比、bad case 数量
3. 聚类找 bad case pattern（不逐条罗列，找共性）
4. 如有上一版本，对比改善/退化
5. **生成分析报告**呈现给用户

```python
# Claude 按需写内联代码完成 Excel 解析，无需预建脚本
import openpyxl
# ... 读取、统计、输出 analysis.md
```

### 生成迭代方案

基于分析报告，阅读算法代码，生成 `plan.md`：
- 问题归因（pattern → 代码位置）
- 改进措施（具体到函数/逻辑层面）
- 预期效果 + 风险

### 执行迭代

修改代码（每次一个方向）→ 运行验证 → 下一轮。
有量化指标时可用 `skill_loop.py` 的同等逻辑自动迭代算法代码。

### 版本管理

```
eval-workspace/versions/
├── v1/feedback.xlsx   ← 已有，自动对比
└── v2/feedback.xlsx   ← 当前提供
```

只提供当前轮 `feedback.xlsx` 即可，历史自动对比。

---

## 工作目录结构

```
eval-workspace/
├── versions/
│   └── vN/
│       ├── feedback.xlsx     # PM 评估反馈
│       ├── analysis.md       # 反馈分析报告
│       └── plan.md           # 迭代改进方案
└── {skill-name}/
    └── results_YYYYMMDD.tsv  # 每轮迭代记录
```

---

## 关键原则

**Claude 是执行者，不是调度器**：不需要预建工具链，遇到需要代码的地方直接写内联代码。

**每次只改一个方向**：小步迭代，改完评估，退化就回滚。

**skill_loop.py 只管 git 状态**：commit、评估调用、keep/revert、记录 tsv——其他全由 Claude 完成。

**产品评估从 feedback.xlsx 出发**：不依赖预先配置的 eval-criteria.md，从列名直接推断维度。

**不要假设入口**：判断不了时直接问用户。
