#!/usr/bin/env python3
"""
Skill 质量评估器

给定一个 SKILL.md 和测试用例，输出综合质量分（0-10）。

评估维度：
  1. 触发准确性  —— description/触发词能否覆盖真实场景
  2. 步骤完整性  —— 操作步骤是否覆盖所有关键路径
  3. 边界处理    —— 不确定时是否有明确的提问/回退策略
  4. 描述清晰度  —— 指令是否具体可执行，无歧义
  5. 测试覆盖率  —— 测试用例与 skill 描述的匹配程度

评估方式（按优先级）：
  1. LLM judge（需要 ANTHROPIC_API_KEY）
  2. 规则评分（离线，不依赖 API）

用法：
    python scripts/skill_eval.py --skill path/to/SKILL.md
    python scripts/skill_eval.py --skill path/to/SKILL.md --cases path/to/cases.json
    python scripts/skill_eval.py --skill path/to/SKILL.md --mode rule
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path


# ── 规则评分（离线）────────────────────────────────────────────────────────────

def score_trigger_coverage(skill_text: str, cases: list[dict]) -> tuple[float, str]:
    """触发准确性：description 中的触发词能否覆盖测试用例的输入场景。"""
    desc_section = ""
    in_yaml = False
    yaml_lines = []
    for line in skill_text.splitlines():
        if line.strip() == "---":
            in_yaml = not in_yaml
            continue
        if in_yaml:
            yaml_lines.append(line)
        else:
            desc_section += line + "\n"

    yaml_text = "\n".join(yaml_lines)
    trigger_words = re.findall(r'"([^"]+)"', yaml_text)
    trigger_words += re.findall(r'「([^」]+)」', yaml_text)

    if not trigger_words:
        return 3.0, "未找到明确的触发词示例，描述可能过于抽象"

    if not cases:
        coverage = len(trigger_words) / max(1, 3)
        score = min(10.0, coverage * 4 + 5)
        return round(score, 1), f"有 {len(trigger_words)} 个触发词示例，无测试用例可验证覆盖率"

    covered = 0
    for case in cases:
        inp = case.get("input", "").lower()
        for tw in trigger_words:
            if any(word.lower() in inp for word in tw.split()):
                covered += 1
                break

    coverage_rate = covered / len(cases)
    score = 5.0 + coverage_rate * 5.0
    note = f"测试用例覆盖率 {coverage_rate:.0%}（{covered}/{len(cases)} 个 case 与触发词匹配）"
    return round(score, 1), note


def score_step_completeness(skill_text: str) -> tuple[float, str]:
    """步骤完整性：是否有清晰的操作步骤或决策路径。"""
    step_patterns = [
        r"^\d+\.\s",          # 1. 步骤
        r"^-\s\*\*",          # - **步骤**
        r"^#{2,3}\s+操作",    # ## 操作：xxx
        r"^#{2,3}\s+步骤",
        r"^\*\*步骤",
        r"^【\d+】",
    ]
    step_count = 0
    for line in skill_text.splitlines():
        for pattern in step_patterns:
            if re.match(pattern, line.strip()):
                step_count += 1
                break

    has_conditional = bool(re.search(r"(如果|否则|不确定|判断|选择|根据)", skill_text))
    has_fallback = bool(re.search(r"(问用户|不要猜|直接问|告知用户)", skill_text))

    score = 0.0
    notes = []

    if step_count >= 5:
        score += 5.0
        notes.append(f"有 {step_count} 个明确步骤")
    elif step_count >= 2:
        score += 3.0
        notes.append(f"有 {step_count} 个步骤（建议更细化）")
    else:
        score += 1.0
        notes.append("步骤不清晰")

    if has_conditional:
        score += 2.5
        notes.append("有条件判断逻辑")
    else:
        notes.append("缺少条件判断逻辑")

    if has_fallback:
        score += 2.5
        notes.append("有不确定时的回退策略")
    else:
        notes.append("缺少不确定时的提问策略")

    return round(min(score, 10.0), 1), "；".join(notes)


def score_clarity(skill_text: str) -> tuple[float, str]:
    """描述清晰度：指令是否具体，无歧义模糊表达。"""
    vague_patterns = [
        r"\b(等等|类似|之类|诸如|或者什么的|其他情况)\b",
        r"\b(适当|合理|合适|根据情况|视情况)\b",
        r"\b(可能|也许|大概|一般来说)\b",
    ]
    vague_count = 0
    for pattern in vague_patterns:
        vague_count += len(re.findall(pattern, skill_text))

    has_examples = bool(re.search(r'(例如|示例|比如|"[^"]{5,}")', skill_text))
    has_table = "|" in skill_text and "---" in skill_text
    has_code_blocks = skill_text.count("```") >= 2

    score = 8.0
    notes = []

    if vague_count > 5:
        score -= 3.0
        notes.append(f"有 {vague_count} 处模糊表达")
    elif vague_count > 2:
        score -= 1.5
        notes.append(f"有 {vague_count} 处模糊表达")

    if has_examples:
        score += 1.0
        notes.append("有具体示例")
    if has_table:
        score += 0.5
        notes.append("有结构化表格")
    if has_code_blocks:
        score += 0.5
        notes.append("有代码/格式示例")

    return round(min(score, 10.0), 1), "；".join(notes) if notes else "描述较清晰"


def score_boundary_handling(skill_text: str) -> tuple[float, str]:
    """边界处理：不确定情况是否有明确处理。"""
    has_ask_user = bool(re.search(r"(问用户|直接问|不确定时|判断不了|告知用户|不要猜|不要假设)", skill_text))
    has_error_case = bool(re.search(r"(找不到|不存在|失败|错误|无法|异常)", skill_text))
    has_entry_detection = bool(re.search(r"(入口|判断|触发|根据.*输入|识别)", skill_text))

    score = 0.0
    notes = []

    if has_ask_user:
        score += 4.0
        notes.append("有不确定时的提问策略")
    else:
        notes.append("缺少不确定时的处理")

    if has_error_case:
        score += 3.0
        notes.append("有错误/边界情况处理")
    else:
        notes.append("缺少边界情况说明")

    if has_entry_detection:
        score += 3.0
        notes.append("有入口判断逻辑")
    else:
        notes.append("缺少入口判断")

    return round(min(score, 10.0), 1), "；".join(notes)


def rule_based_score(skill_text: str, cases: list[dict]) -> dict:
    """综合规则评分，返回各维度得分和总分。"""
    dimensions = {
        "触发准确性": score_trigger_coverage(skill_text, cases),
        "步骤完整性": score_step_completeness(skill_text),
        "描述清晰度": score_clarity(skill_text),
        "边界处理":   score_boundary_handling(skill_text),
    }

    weights = {
        "触发准确性": 0.25,
        "步骤完整性": 0.35,
        "描述清晰度": 0.20,
        "边界处理":   0.20,
    }

    total = sum(dimensions[d][0] * weights[d] for d in dimensions)

    return {
        "mode": "rule",
        "total_score": round(total, 2),
        "max_score": 10.0,
        "dimensions": {
            d: {"score": s, "note": n}
            for d, (s, n) in dimensions.items()
        },
        "weights": weights,
    }


# ── LLM Judge（需要 API Key）──────────────────────────────────────────────────

def llm_judge(skill_text: str, cases: list[dict]) -> dict | None:
    """使用 Claude API 对 skill 进行综合评分。"""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    try:
        import anthropic
    except ImportError:
        return None

    cases_text = json.dumps(cases, ensure_ascii=False, indent=2) if cases else "（无测试用例）"

    prompt = f"""你是一个 Claude Code skill 质量评审专家。请对以下 SKILL.md 进行质量评分。

## 待评估的 SKILL.md

{skill_text}

## 测试用例

{cases_text}

## 评分要求

请从以下 4 个维度打分（每项 0-10 分），并给出总分（加权均值）：

1. **触发准确性**（权重 25%）：description 中的触发词和场景描述，能否准确覆盖真实用户的触发场景？触发词是否足够多样？
2. **步骤完整性**（权重 35%）：操作步骤是否覆盖主要路径？是否有清晰的条件分支？步骤是否细化到可执行？
3. **描述清晰度**（权重 20%）：指令是否具体、无歧义？是否有示例降低误解？
4. **边界处理**（权重 20%）：不确定情况是否有明确的提问/回退策略？边界和异常是否被考虑？

请用以下 JSON 格式输出，不要输出其他内容：

{{
  "mode": "llm",
  "total_score": <0-10 的浮点数>,
  "dimensions": {{
    "触发准确性": {{"score": <分数>, "note": "<简短说明>", "issues": ["<问题1>", "<问题2>"]}},
    "步骤完整性": {{"score": <分数>, "note": "<简短说明>", "issues": ["<问题1>"]}},
    "描述清晰度": {{"score": <分数>, "note": "<简短说明>", "issues": []}},
    "边界处理": {{"score": <分数>, "note": "<简短说明>", "issues": []}}
  }},
  "top_issues": ["<最重要的问题1>", "<最重要的问题2>", "<最重要的问题3>"],
  "improvement_direction": "<本轮建议改哪一个方向（只选一个）>"
}}"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        # 提取 JSON
        json_match = re.search(r'\{[\s\S]+\}', raw)
        if json_match:
            return json.loads(json_match.group())
    except Exception as e:
        print(f"[LLM judge] 调用失败：{e}", file=sys.stderr)

    return None


# ── 改进建议生成 ───────────────────────────────────────────────────────────────

def generate_improvement_prompt(skill_text: str, eval_result: dict) -> str:
    """根据评估结果生成改进指导（供 skill_autoloop.py 调用）。"""
    issues = []
    direction = ""

    if "top_issues" in eval_result:
        issues = eval_result["top_issues"]
        direction = eval_result.get("improvement_direction", "")
    else:
        # 规则评分模式：找最低分维度
        dims = eval_result.get("dimensions", {})
        sorted_dims = sorted(dims.items(), key=lambda x: x[1]["score"])
        if sorted_dims:
            worst_dim, worst_info = sorted_dims[0]
            direction = worst_dim
            issues = [worst_info.get("note", "")]

    return f"""当前 skill 评分：{eval_result['total_score']}/10

主要问题：
{chr(10).join(f'- {issue}' for issue in issues)}

本轮改进方向：**{direction}**

请基于以上反馈，对 SKILL.md 进行针对性改进。只改一个方向，小步迭代。"""


# ── 主入口 ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Skill 质量评估器")
    parser.add_argument("--skill", required=True, help="SKILL.md 文件路径")
    parser.add_argument("--cases", default=None, help="测试用例 JSON 文件路径（cases.json）")
    parser.add_argument("--mode", choices=["auto", "rule", "llm"], default="auto",
                        help="评估模式（auto: 优先 LLM，降级到 rule）")
    parser.add_argument("--output", default=None, help="结果输出 JSON 文件路径")
    args = parser.parse_args()

    skill_path = Path(args.skill)
    if not skill_path.exists():
        sys.exit(f"SKILL.md 不存在：{skill_path}")

    skill_text = skill_path.read_text(encoding="utf-8")

    cases = []
    cases_path = Path(args.cases) if args.cases else skill_path.parent / "tests" / "cases.json"
    if cases_path.exists():
        try:
            cases = json.loads(cases_path.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[warn] 无法读取测试用例：{e}", file=sys.stderr)

    # 执行评估
    result = None
    if args.mode in ("auto", "llm"):
        result = llm_judge(skill_text, cases)
        if result is None and args.mode == "llm":
            sys.exit("LLM judge 失败（检查 ANTHROPIC_API_KEY）")

    if result is None:
        result = rule_based_score(skill_text, cases)

    # 添加改进提示
    result["improvement_prompt"] = generate_improvement_prompt(skill_text, result)

    # 输出
    output_json = json.dumps(result, ensure_ascii=False, indent=2)
    print(output_json)

    # 同时打印可被 autoloop 提取的指标行
    print(f"\nskill_score: {result['total_score']}")

    if args.output:
        Path(args.output).write_text(output_json, encoding="utf-8")


if __name__ == "__main__":
    main()
