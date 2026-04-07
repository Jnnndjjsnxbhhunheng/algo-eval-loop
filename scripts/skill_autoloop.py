#!/usr/bin/env python3
"""
Skill 自主迭代运行器

给定一个 skill 名称，自动找到该 skill 的 SKILL.md，
然后自主循环迭代改进，直到质量收敛。

借鉴 karpathy/autoresearch 理念：
  改进 SKILL.md → git commit → 评估质量 → 保留/回滚 → 循环

用法：
    # 最简用法：给 skill 名字就行
    python scripts/skill_autoloop.py my-skill-name

    # 指定轮次和目标分
    python scripts/skill_autoloop.py my-skill-name --max-rounds 20 --target-score 8.5

    # 指定 skill 路径（跳过搜索）
    python scripts/skill_autoloop.py --skill-path ~/.claude/skills/my-skill/SKILL.md

    # 使用测试用例
    python scripts/skill_autoloop.py my-skill-name --cases path/to/cases.json
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


# ── Skill 搜索 ────────────────────────────────────────────────────────────────

SKILL_SEARCH_PATHS = [
    Path.home() / ".claude" / "skills",
    Path("/mnt/skills/user"),
    Path("/mnt/user-data/uploads"),
]


def find_skill(name: str) -> Path | None:
    """按名称搜索 skill 的 SKILL.md 文件。"""
    candidates = []

    for base in SKILL_SEARCH_PATHS:
        if not base.exists():
            continue
        for pattern in (
            f"{name}/SKILL.md",
            f"{name}.md",
            f"*/{name}/SKILL.md",
        ):
            for found in base.glob(pattern):
                candidates.append(found)

    # 也搜索当前目录
    cwd = Path.cwd()
    for pattern in (f"{name}/SKILL.md", f"**/{name}/SKILL.md", f"{name}.md"):
        for found in cwd.glob(pattern):
            if "eval-workspace" not in str(found):
                candidates.append(found)

    if not candidates:
        return None
    if len(candidates) == 1:
        return candidates[0]

    # 多个候选：优先精确匹配
    for c in candidates:
        if c.parent.name == name or c.stem == name:
            return c
    return candidates[0]


def list_available_skills() -> list[Path]:
    """列出所有可发现的 skill。"""
    skills = []
    for base in SKILL_SEARCH_PATHS:
        if not base.exists():
            continue
        skills.extend(base.glob("*/SKILL.md"))
        skills.extend(base.glob("*.md"))
    skills.extend(Path.cwd().glob("*/SKILL.md"))
    return [s for s in skills if "eval-workspace" not in str(s)]


# ── Git 操作 ──────────────────────────────────────────────────────────────────

def git(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git"] + list(args),
        capture_output=True, text=True, timeout=30,
        cwd=str(cwd) if cwd else None,
    )
    return result.stdout.strip()


def git_has_changes(path: Path, cwd: Path) -> bool:
    status = git("status", "--porcelain", str(path), cwd=cwd)
    return bool(status.strip())


def git_commit_file(path: Path, message: str, cwd: Path) -> str:
    git("add", str(path), cwd=cwd)
    git("commit", "-m", message, cwd=cwd)
    return git("rev-parse", "--short", "HEAD", cwd=cwd)


def git_revert_last(cwd: Path) -> None:
    git("reset", "--hard", "HEAD~1", cwd=cwd)


def git_current_branch(cwd: Path) -> str:
    return git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)


def git_create_branch(name: str, cwd: Path) -> None:
    git("checkout", "-b", name, cwd=cwd)


# ── LLM 改进生成 ───────────────────────────────────────────────────────────────

def generate_improved_skill(
    current_skill_text: str,
    eval_result: dict,
    improvement_prompt: str,
    round_num: int,
) -> str | None:
    """
    使用 Claude API 生成改进版 SKILL.md。
    返回新的 skill 全文，失败时返回 None。
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    try:
        import anthropic
    except ImportError:
        print("[warn] anthropic 包未安装，跳过 LLM 改进", file=sys.stderr)
        return None

    prompt = f"""你是一个 Claude Code skill 优化专家。你需要改进以下 SKILL.md 文件。

## 当前 SKILL.md

{current_skill_text}

## 评估反馈（第 {round_num} 轮）

{improvement_prompt}

## 改进要求

1. **只改一个方向**（见"本轮改进方向"），不要大范围重写
2. 保持 YAML frontmatter（---...---）格式不变
3. 改动要具体、可验证，不要只是换个措辞
4. 改动后 skill 应该仍然内聚、逻辑完整

请直接输出改进后的完整 SKILL.md 内容，不要有任何前缀说明或代码块包裹。"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-opus-4-6",
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()
    except Exception as e:
        print(f"[LLM improve] 调用失败：{e}", file=sys.stderr)
        return None


def rule_based_improvement(current_skill_text: str, eval_result: dict) -> str:
    """
    无 API Key 时的规则改进：针对最低分维度进行启发式改进。
    """
    dims = eval_result.get("dimensions", {})
    sorted_dims = sorted(dims.items(), key=lambda x: x[1]["score"])
    if not sorted_dims:
        return current_skill_text

    worst_dim = sorted_dims[0][0]
    text = current_skill_text

    if worst_dim == "触发准确性":
        # 尝试在 description 末尾添加更多触发词
        more_triggers = '\n  更多触发场景："请帮我做"、"我想要"、"帮我看看"、"分析一下"'
        if "description: >" in text:
            text = text.replace(
                "---\n\n#",
                f'{more_triggers}\n---\n\n#',
                1,
            )

    elif worst_dim == "边界处理":
        # 在关键原则里追加不确定时的策略
        append_text = "\n\n**不确定时直接问**：遇到任何不明确的输入，不要猜测，直接问用户。"
        if "## 关键原则" in text:
            text = text.replace("## 关键原则", f"## 关键原则{append_text}", 1)
        else:
            text += append_text

    elif worst_dim == "步骤完整性":
        # 暂时标记需要补充步骤
        text += "\n\n<!-- TODO: 补充操作步骤细节 -->"

    return text


# ── 测试用例生成 ───────────────────────────────────────────────────────────────

def generate_test_cases(skill_text: str, skill_name: str) -> list[dict]:
    """从 skill 的 description 中提取触发词，自动生成测试用例。"""
    trigger_quotes = re.findall(r'"([^"]{3,})"', skill_text)

    cases = []
    for i, trigger in enumerate(trigger_quotes[:5]):
        cases.append({
            "id": f"case_{i+1}",
            "input": trigger,
            "expected_behavior": f"skill 被正确触发并执行 {skill_name} 的核心操作",
            "scoring_criteria": [
                "是否识别到正确的操作类型",
                "是否按步骤执行",
                "是否有边界处理",
            ],
        })

    if not cases:
        cases = [
            {
                "id": "case_1",
                "input": f"帮我使用 {skill_name}",
                "expected_behavior": f"触发 {skill_name} skill 并开始执行",
                "scoring_criteria": ["skill 被正确触发"],
            }
        ]

    return cases


# ── Results 追踪 ──────────────────────────────────────────────────────────────

RESULTS_HEADER = [
    "轮次", "时间戳", "改动描述", "得分", "基线分", "变化", "决策", "commit_hash"
]


def init_results(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f, delimiter="\t").writerow(RESULTS_HEADER)


def append_result(path: Path, row: list) -> None:
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.writer(f, delimiter="\t").writerow(row)


# ── 评估调用 ──────────────────────────────────────────────────────────────────

def evaluate_skill(skill_path: Path, cases_path: Path | None, mode: str) -> dict | None:
    """调用 skill_eval.py 评估当前 SKILL.md，返回结果 dict。"""
    eval_script = Path(__file__).parent / "skill_eval.py"
    if not eval_script.exists():
        print("[warn] skill_eval.py 不存在", file=sys.stderr)
        return None

    cmd = [sys.executable, str(eval_script), "--skill", str(skill_path), "--mode", mode]
    if cases_path and cases_path.exists():
        cmd += ["--cases", str(cases_path)]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        output = result.stdout
        # 提取 JSON（取第一个完整 JSON 块）
        json_match = re.search(r'\{[\s\S]+?\}(?=\n\nskill_score:|\Z)', output)
        if json_match:
            return json.loads(json_match.group())
        # 备选：解析最后的 skill_score: 行
        score_match = re.search(r"skill_score:\s*([\d.]+)", output)
        if score_match:
            return {"total_score": float(score_match.group(1)), "mode": "fallback"}
    except Exception as e:
        print(f"[eval] 失败：{e}", file=sys.stderr)

    return None


# ── 摘要生成 ───────────────────────────────────────────────────────────────────

def write_summary(
    summary_path: Path,
    skill_name: str,
    skill_path: Path,
    baseline: float,
    best_score: float,
    best_round: int,
    total_rounds: int,
    stop_reason: str,
    results_path: Path,
) -> None:
    lines = [
        f"# Skill 迭代摘要：{skill_name}",
        f"\n生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"Skill 路径：{skill_path}",
        f"总轮次：{total_rounds}",
        f"停止原因：{stop_reason}",
        "",
        "## 得分变化",
        "",
        f"- 基线得分：{baseline}/10",
        f"- 最佳得分：{best_score}/10（第 {best_round} 轮）",
    ]

    if baseline > 0:
        improvement = best_score - baseline
        pct = improvement / baseline * 100
        lines.append(f"- 总改善：+{improvement:.2f}（{pct:.1f}%）")

    # 统计决策
    advances, reverts = 0, 0
    try:
        with open(results_path, encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                if "advance" in row.get("决策", ""):
                    advances += 1
                elif "revert" in row.get("决策", ""):
                    reverts += 1
    except Exception:
        pass

    lines += [
        "",
        "## 迭代统计",
        "",
        f"- ✅ 保留：{advances} 轮",
        f"- ❌ 回滚：{reverts} 轮",
        "",
        "## 下一步",
        "",
        "- [ ] 检查最佳版本的 SKILL.md 改动",
        "- [ ] 将最佳版本复制回原 skill 目录",
        "- [ ] 继续人工评审优化方向",
    ]

    summary_path.write_text("\n".join(lines), encoding="utf-8")


# ── 主循环 ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Skill 自主迭代运行器",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 最简：只给 skill 名称
  python scripts/skill_autoloop.py my-skill-name

  # 指定目标分和最大轮次
  python scripts/skill_autoloop.py my-skill-name --max-rounds 15 --target-score 8.5

  # 直接指定路径
  python scripts/skill_autoloop.py --skill-path ~/.claude/skills/my-skill/SKILL.md
        """,
    )
    parser.add_argument("skill_name", nargs="?", default=None, help="Skill 名称（自动搜索）")
    parser.add_argument("--skill-path", default=None, help="直接指定 SKILL.md 路径")
    parser.add_argument("--cases", default=None, help="测试用例 JSON 路径")
    parser.add_argument("--max-rounds", type=int, default=20, help="最大迭代轮次（默认 20）")
    parser.add_argument("--target-score", type=float, default=8.5, help="目标得分（默认 8.5/10）")
    parser.add_argument("--patience", type=int, default=5, help="连续无改善停止轮次（默认 5）")
    parser.add_argument("--eval-mode", choices=["auto", "rule", "llm"], default="auto",
                        help="评估模式（默认 auto）")
    parser.add_argument("--workspace", default="eval-workspace", help="工作目录")
    args = parser.parse_args()

    # ── 找到 skill ──
    if args.skill_path:
        skill_path = Path(args.skill_path)
        if not skill_path.exists():
            sys.exit(f"Skill 文件不存在：{skill_path}")
        skill_name = skill_path.parent.name or skill_path.stem
    elif args.skill_name:
        skill_path = find_skill(args.skill_name)
        skill_name = args.skill_name
        if skill_path is None:
            print(f"找不到 skill：{args.skill_name}")
            available = list_available_skills()
            if available:
                print(f"\n可用的 skills：")
                for s in available:
                    print(f"  - {s.parent.name or s.stem}  ({s})")
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)

    print("=" * 60)
    print(f"  Skill 自主迭代运行器")
    print("=" * 60)
    print(f"  Skill：{skill_name}")
    print(f"  路径：{skill_path}")
    print(f"  最大轮次：{args.max_rounds}")
    print(f"  目标得分：{args.target_score}/10")
    print(f"  耐心值：{args.patience} 轮")
    print(f"  评估模式：{args.eval_mode}")
    print("=" * 60)

    # ── 工作目录 ──
    workspace = Path(args.workspace)
    iter_dir = workspace / "skill-iterations" / skill_name
    iter_dir.mkdir(parents=True, exist_ok=True)
    results_path = iter_dir / "results.tsv"
    summary_path = iter_dir / "skill_iter_summary.md"
    init_results(results_path)

    # ── 测试用例 ──
    cases_path = None
    if args.cases:
        cases_path = Path(args.cases)
    else:
        auto_cases_path = skill_path.parent / "tests" / "cases.json"
        if auto_cases_path.exists():
            cases_path = auto_cases_path
        else:
            # 自动生成测试用例
            skill_text = skill_path.read_text(encoding="utf-8")
            generated_cases = generate_test_cases(skill_text, skill_name)
            auto_cases_path.parent.mkdir(parents=True, exist_ok=True)
            auto_cases_path.write_text(
                json.dumps(generated_cases, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            cases_path = auto_cases_path
            print(f"  自动生成测试用例：{cases_path}（{len(generated_cases)} 个）")

    # ── Git 设置 ──
    git_root = skill_path.parent
    while git_root != git_root.parent:
        if (git_root / ".git").exists():
            break
        git_root = git_root.parent
    else:
        # 如果 skill 不在 git 仓库中，用当前目录
        git_root = Path.cwd()

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    branch_name = f"skill-iter/{skill_name}/{timestamp}"
    original_branch = git_current_branch(git_root)

    try:
        git_create_branch(branch_name, git_root)
        print(f"  实验分支：{branch_name}")
    except Exception:
        print(f"  [warn] 无法创建分支，在当前分支继续")

    # ── 基线评估 ──
    print("\n[基线] 评估当前 SKILL.md 质量...")
    baseline_result = evaluate_skill(skill_path, cases_path, args.eval_mode)
    if baseline_result is None:
        print("[基线] 评估失败，使用默认分 5.0")
        baseline_score = 5.0
    else:
        baseline_score = baseline_result.get("total_score", 5.0)
        print(f"[基线] skill_score = {baseline_score}/10")

    append_result(results_path, [
        0, datetime.now().isoformat(), "baseline",
        baseline_score, baseline_score, "0", "baseline",
        git("rev-parse", "--short", "HEAD", cwd=git_root),
    ])

    # ── 主循环 ──
    best_score = baseline_score
    best_round = 0
    no_improve_count = 0
    stop_reason = ""

    for round_num in range(1, args.max_rounds + 1):
        print(f"\n{'─' * 50}")
        print(f"[第 {round_num}/{args.max_rounds} 轮]  当前最佳：{best_score}/10")

        # 读取当前 skill
        current_skill_text = skill_path.read_text(encoding="utf-8")

        # 获取改进方向
        improvement_prompt = ""
        if baseline_result:
            improvement_prompt = baseline_result.get(
                "improvement_prompt",
                "请改进 skill 的描述和步骤清晰度。",
            )

        # 生成改进版
        print("  生成改进版 SKILL.md...")
        improved_text = generate_improved_skill(
            current_skill_text, baseline_result or {}, improvement_prompt, round_num
        )
        if improved_text is None:
            # 无 API Key，用规则改进
            improved_text = rule_based_improvement(current_skill_text, baseline_result or {})
            print("  [规则改进] 使用离线规则生成改进（无 ANTHROPIC_API_KEY）")

        if improved_text.strip() == current_skill_text.strip():
            print("  内容无变化，跳过此轮")
            no_improve_count += 1
            if no_improve_count >= args.patience:
                stop_reason = "内容无法继续改进（收敛）"
                break
            continue

        # 写入改动
        skill_path.write_text(improved_text, encoding="utf-8")

        # 检测实际变化
        if not git_has_changes(skill_path, git_root):
            print("  git 无差异，跳过")
            no_improve_count += 1
            continue

        # 生成 commit 描述
        diff_stat = git("diff", "--stat", "HEAD", "--", str(skill_path), cwd=git_root)
        change_summary = diff_stat.splitlines()[-1] if diff_stat.strip() else f"round {round_num} improvements"

        # Commit
        commit_hash = git_commit_file(
            skill_path,
            f"skill-iter {round_num}: {change_summary[:80]}",
            git_root,
        )
        print(f"  提交：{commit_hash}")

        # 评估新版本
        print("  评估新版本...")
        new_result = evaluate_skill(skill_path, cases_path, args.eval_mode)
        if new_result is None:
            print("  评估失败，回滚")
            git_revert_last(git_root)
            skill_path.write_text(current_skill_text, encoding="utf-8")
            continue

        new_score = new_result.get("total_score", 0.0)
        delta = new_score - best_score
        print(f"  skill_score = {new_score}/10（{'+' if delta >= 0 else ''}{delta:.2f}）")

        if new_score > best_score:
            print(f"  ✅ 改善 → 保留")
            best_score = new_score
            best_round = round_num
            baseline_result = new_result  # 更新改进方向
            no_improve_count = 0
            decision = "✅ advance"
        else:
            print(f"  ❌ 未改善 → 回滚")
            git_revert_last(git_root)
            skill_path.write_text(current_skill_text, encoding="utf-8")
            no_improve_count += 1
            decision = "❌ revert"

        append_result(results_path, [
            round_num, datetime.now().isoformat(), change_summary[:60],
            new_score, baseline_score, f"{delta:+.2f}", decision, commit_hash,
        ])

        # 检查停止条件
        if best_score >= args.target_score:
            stop_reason = f"达到目标得分 {args.target_score}"
            print(f"\n🎯 {stop_reason}")
            break

        if no_improve_count >= args.patience:
            stop_reason = f"连续 {args.patience} 轮无改善（收敛）"
            print(f"\n⏹ {stop_reason}")
            break

    else:
        stop_reason = f"达到最大轮次 {args.max_rounds}"

    total_rounds = round_num if 'round_num' in dir() else 0

    write_summary(
        summary_path, skill_name, skill_path,
        baseline_score, best_score, best_round, total_rounds,
        stop_reason, results_path,
    )

    print(f"\n{'=' * 60}")
    print(f"  Skill 迭代完成：{skill_name}")
    print(f"  基线得分：{baseline_score}/10")
    print(f"  最佳得分：{best_score}/10（第 {best_round} 轮）")
    print(f"  停止原因：{stop_reason}")
    print(f"  实验记录：{results_path}")
    print(f"  摘要报告：{summary_path}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
