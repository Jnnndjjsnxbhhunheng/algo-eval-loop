#!/usr/bin/env python3
"""
skill_loop.py — 基于 feedback.xlsx 的 Skill 迭代 harness

唯一职责：git 状态管理 + 代理评估 + 结果追踪。
分析、归因、改进由 Claude 通过 SKILL.md 指令完成。

用法：
    python scripts/skill_loop.py <skill名称或路径> \\
        --feedback path/to/feedback.xlsx \\
        [--rounds 20] [--patience 5] [--target 8.5]

流程：
    1. 加载 feedback.xlsx 分析结果（patterns.json，由 Claude 预先生成）
    2. 评估当前 skill 对 patterns 的覆盖率，建立基线
    3. 循环：Claude 修改 skill → commit → 代理评估 → 保留/回滚
    4. 收敛时生成摘要
"""

import argparse
import csv
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path


# ── Skill 搜索 ────────────────────────────────────────────────────────────────

SEARCH_ROOTS = [
    Path.home() / ".claude" / "skills",
    Path("/mnt/skills/user"),
    Path.cwd(),
]


def find_skill_dir(name: str) -> Path | None:
    target = Path(name)
    if target.exists():
        return target if target.is_dir() else target.parent

    for root in SEARCH_ROOTS:
        if not root.exists():
            continue
        for pattern in (f"{name}/SKILL.md", f"**/{name}/SKILL.md"):
            for found in root.glob(pattern):
                return found.parent
    return None


# ── Git ───────────────────────────────────────────────────────────────────────

def git(*args: str, cwd: Path) -> str:
    r = subprocess.run(["git"] + list(args), capture_output=True, text=True, cwd=cwd)
    return r.stdout.strip()


def git_has_changes(skill_dir: Path, repo_root: Path) -> bool:
    return bool(git("status", "--porcelain", str(skill_dir), cwd=repo_root).strip())


def git_commit(skill_dir: Path, msg: str, repo_root: Path) -> str:
    git("add", str(skill_dir), cwd=repo_root)
    git("commit", "-m", msg, cwd=repo_root)
    return git("rev-parse", "--short", "HEAD", cwd=repo_root)


def git_revert(repo_root: Path) -> None:
    git("reset", "--hard", "HEAD~1", cwd=repo_root)


def find_repo_root(path: Path) -> Path:
    p = path
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    return path


# ── Patterns 加载（由 Claude 预先分析 feedback.xlsx 生成）──────────────────────

def load_patterns(skill_dir: Path, feedback_path: Path | None) -> list[dict]:
    """
    加载 bad case patterns。
    优先读取 skill 目录下的 patterns.json（由 Claude 分析 feedback.xlsx 后生成）。
    """
    patterns_path = skill_dir / "patterns.json"
    if not patterns_path.exists() and feedback_path:
        # 也尝试 feedback 同目录
        patterns_path = feedback_path.parent / "patterns.json"

    if patterns_path.exists():
        try:
            return json.loads(patterns_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    return []


# ── 代理评估：skill 对 patterns 的覆盖率 ──────────────────────────────────────

def rule_coverage(skill_text: str, patterns: list[dict]) -> float:
    """
    规则评分：skill 描述中是否覆盖了 bad case patterns 的关键词。
    作为无 API Key 时的兜底。
    """
    if not patterns:
        # 无 patterns 时退化为通用规则评分
        score = 0.0
        triggers = re.findall(r'"[^"]{3,}"', skill_text)
        score += min(len(triggers) * 0.4, 3.0)
        steps = len(re.findall(r"^\d+\.\s", skill_text, re.MULTILINE))
        score += min(steps * 0.5, 3.0)
        if re.search(r"(问用户|不确定|判断不了|直接问)", skill_text):
            score += 2.0
        if re.search(r"(如果|否则|根据)", skill_text):
            score += 2.0
        return round(min(score, 10.0), 2)

    covered = 0
    for p in patterns:
        keywords = p.get("keywords", [])
        if any(kw.lower() in skill_text.lower() for kw in keywords):
            covered += 1

    coverage = covered / len(patterns)
    # pattern 覆盖率 70% + 通用质量 30%
    quality = 0.0
    if re.search(r"(问用户|不确定|直接问)", skill_text):
        quality += 5.0
    if len(re.findall(r"^\d+\.\s", skill_text, re.MULTILINE)) >= 3:
        quality += 5.0

    return round(coverage * 7.0 + quality * 0.3, 2)


def llm_coverage(skill_text: str, patterns: list[dict], feedback_summary: str) -> float | None:
    """
    LLM 评估：改进后的 skill 能否解决 bad case patterns。
    需要 ANTHROPIC_API_KEY。
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic
    except ImportError:
        return None

    patterns_text = json.dumps(patterns, ensure_ascii=False, indent=2) if patterns else "（无具体patterns）"

    prompt = f"""评估改进后的 skill 是否解决了 PM 反馈中的 bad case patterns。

## Bad Case Patterns（来自 feedback.xlsx 分析）
{patterns_text}

## PM 反馈摘要
{feedback_summary or "（无摘要）"}

## 当前 Skill 内容
{skill_text[:3000]}

评分标准（0-10）：
- 8-10：skill 改动直接针对 patterns，预期能显著改善这些 case
- 5-7：skill 有改善，但未完全覆盖 patterns
- 0-4：改动与 patterns 无关，或方向错误

只输出 JSON：{{"score": <0-10>, "covered_patterns": <覆盖的pattern数>, "note": "<一句话说明>"}}"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=256,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = msg.content[0].text
        m = re.search(r'\{[^}]+\}', raw)
        if m:
            data = json.loads(m.group())
            score = float(data.get("score", 0))
            note = data.get("note", "")
            print(f"  [llm] score={score:.1f}  {note}")
            return score
    except Exception as e:
        print(f"  [llm] {e}", file=sys.stderr)
    return None


def evaluate(skill_dir: Path, patterns: list[dict], feedback_summary: str) -> tuple[float, str]:
    skill_md = skill_dir / "SKILL.md"
    skill_text = skill_md.read_text(encoding="utf-8") if skill_md.exists() else ""

    score = llm_coverage(skill_text, patterns, feedback_summary)
    if score is not None:
        return score, "llm"
    return rule_coverage(skill_text, patterns), "rule"


# ── Results 追踪 ──────────────────────────────────────────────────────────────

HEADER = ["round", "timestamp", "change", "score", "baseline", "delta", "decision", "hash"]


def init_tsv(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f, delimiter="\t").writerow(HEADER)


def log_row(path: Path, row: list) -> None:
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.writer(f, delimiter="\t").writerow(row)


# ── 主循环 ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="基于 feedback.xlsx 的 Skill 迭代 harness",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 基本用法
  python scripts/skill_loop.py my-skill --feedback feedback.xlsx

  # 指定轮次和目标分
  python scripts/skill_loop.py my-skill --feedback v2/feedback.xlsx --rounds 15 --target 8.0
        """,
    )
    parser.add_argument("skill", help="skill 名称或路径")
    parser.add_argument("--feedback", default=None, help="feedback.xlsx 路径（PM 评估结果）")
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--patience", type=int, default=5)
    parser.add_argument("--target", type=float, default=8.5)
    args = parser.parse_args()

    # 找 skill
    skill_dir = find_skill_dir(args.skill)
    if skill_dir is None:
        print(f"找不到 skill：{args.skill}")
        sys.exit(1)

    repo_root = find_repo_root(skill_dir)
    feedback_path = Path(args.feedback) if args.feedback else None

    # 加载 patterns（由 Claude 分析 feedback.xlsx 后写入）
    patterns = load_patterns(skill_dir, feedback_path)
    feedback_summary = ""
    summary_path = skill_dir.parent / "eval-workspace" / "versions"
    # 尝试读取 analysis.md 作为 feedback 摘要
    if feedback_path and (feedback_path.parent / "analysis.md").exists():
        feedback_summary = (feedback_path.parent / "analysis.md").read_text(encoding="utf-8")[:1000]

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_path = skill_dir / f"results_{ts}.tsv"
    init_tsv(results_path)

    print("=" * 55)
    print(f"  Skill Loop: {skill_dir.name}")
    print(f"  Skill 路径: {skill_dir}")
    if feedback_path:
        print(f"  Feedback:   {feedback_path}")
    print(f"  Patterns:   {len(patterns)} 个 bad case patterns")
    print(f"  最大轮次: {args.rounds}  耐心值: {args.patience}  目标: {args.target}")
    print("=" * 55)

    if not patterns:
        print("\n⚠ 未找到 patterns.json，Claude 需要先分析 feedback.xlsx 并生成 patterns.json")
        print("  路径：skill目录/patterns.json 或 feedback同目录/patterns.json")
        print("  继续运行（使用通用规则评分）...\n")

    # 基线
    baseline, mode = evaluate(skill_dir, patterns, feedback_summary)
    print(f"\n[基线] score={baseline:.2f}  mode={mode}")
    log_row(results_path, [0, datetime.now().isoformat(), "baseline",
                            baseline, baseline, 0, "baseline",
                            git("rev-parse", "--short", "HEAD", cwd=repo_root)])

    best = baseline
    best_round = 0
    no_improve = 0
    stop_reason = ""

    for n in range(1, args.rounds + 1):
        print(f"\n{'─'*45}")
        print(f"[第 {n}/{args.rounds} 轮]  best={best:.2f}/{args.target}")
        print("  等待 skill 文件变更...")
        print("  （Claude 修改完成后按 Enter 继续，输入 q 退出，输入 s 跳过）")

        try:
            user_input = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            stop_reason = "用户中断"
            break

        if user_input == "q":
            stop_reason = "用户退出"
            break
        if user_input == "s":
            print("  跳过此轮")
            continue

        if not git_has_changes(skill_dir, repo_root):
            print("  无变更，跳过")
            no_improve += 1
            if no_improve >= args.patience:
                stop_reason = f"连续 {args.patience} 轮无变更"
                break
            continue

        # commit
        diff = git("diff", "--stat", "HEAD", "--", str(skill_dir), cwd=repo_root)
        summary = (diff.splitlines()[-1] if diff.strip() else "changes").strip()[:60]
        commit_hash = git_commit(skill_dir, f"skill-iter {n}: {summary}", repo_root)
        print(f"  commit: {commit_hash}  ({summary})")

        # 评估
        new_score, mode = evaluate(skill_dir, patterns, feedback_summary)
        delta = new_score - best
        print(f"  score={new_score:.2f}  delta={delta:+.2f}  mode={mode}")

        if new_score > best:
            print("  ✅ 改善 → 保留")
            best = new_score
            best_round = n
            no_improve = 0
            decision = "advance"
        else:
            print("  ❌ 未改善 → 回滚")
            git_revert(repo_root)
            no_improve += 1
            decision = "revert"
            commit_hash = "-"

        log_row(results_path, [n, datetime.now().isoformat(), summary,
                                new_score, baseline, f"{delta:+.2f}", decision, commit_hash])

        if best >= args.target:
            stop_reason = f"达到目标分 {args.target}"
            print(f"\n🎯 {stop_reason}")
            break

        if no_improve >= args.patience:
            stop_reason = f"连续 {args.patience} 轮无改善"
            print(f"\n⏹ {stop_reason}")
            break
    else:
        stop_reason = f"达到最大轮次 {args.rounds}"

    total = n if 'n' in dir() else 0
    print(f"\n{'='*55}")
    print(f"  完成  skill={skill_dir.name}")
    print(f"  基线={baseline:.2f} → 最佳={best:.2f}（第{best_round}轮）")
    print(f"  改善={best-baseline:+.2f}  停止：{stop_reason}")
    print(f"  记录：{results_path}")
    print(f"{'='*55}")


if __name__ == "__main__":
    main()
