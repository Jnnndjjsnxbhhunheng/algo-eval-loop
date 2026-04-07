#!/usr/bin/env python3
"""
skill_loop.py — Skill 迭代 harness

两种使用方式：

1. eval 模式（Claude 自主循环时调用）：
   python scripts/skill_loop.py eval <skill名称> [--feedback feedback.xlsx]
   → 评估当前 skill 质量，输出 skill_score: X.X，退出

2. run 模式（有人监督时使用）：
   python scripts/skill_loop.py run <skill名称> [--feedback feedback.xlsx] [--rounds 20]
   → 交互式循环，每轮等待用户确认

Claude 自主整夜运行时只用 eval 模式，循环逻辑由 Claude 通过 Bash 工具实现。
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


# ── Patterns 加载 ─────────────────────────────────────────────────────────────

def load_patterns(skill_dir: Path, feedback_path: Path | None) -> list[dict]:
    for p in [
        skill_dir / "patterns.json",
        (feedback_path.parent / "patterns.json") if feedback_path else None,
    ]:
        if p and p.exists():
            try:
                return json.loads(p.read_text(encoding="utf-8"))
            except Exception:
                pass
    return []


# ── 评估 ──────────────────────────────────────────────────────────────────────

def rule_coverage(skill_text: str, patterns: list[dict]) -> float:
    if not patterns:
        score = 0.0
        score += min(len(re.findall(r'"[^"]{3,}"', skill_text)) * 0.4, 3.0)
        score += min(len(re.findall(r"^\d+\.\s", skill_text, re.MULTILINE)) * 0.5, 3.0)
        if re.search(r"(问用户|不确定|判断不了|直接问)", skill_text):
            score += 2.0
        if re.search(r"(如果|否则|根据)", skill_text):
            score += 2.0
        return round(min(score, 10.0), 2)

    covered = sum(
        1 for p in patterns
        if any(kw.lower() in skill_text.lower() for kw in p.get("keywords", []))
    )
    coverage = covered / len(patterns)
    quality = 0.0
    if re.search(r"(问用户|不确定|直接问)", skill_text):
        quality += 5.0
    if len(re.findall(r"^\d+\.\s", skill_text, re.MULTILINE)) >= 3:
        quality += 5.0
    return round(coverage * 7.0 + quality * 0.3, 2)


def llm_coverage(skill_text: str, patterns: list[dict], feedback_summary: str) -> float | None:
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    try:
        import anthropic
    except ImportError:
        return None

    patterns_text = json.dumps(patterns, ensure_ascii=False, indent=2) if patterns else "（无）"
    prompt = f"""评估改进后的 skill 是否解决了 PM 反馈中的 bad case patterns。

## Bad Case Patterns
{patterns_text}

## PM 反馈摘要
{feedback_summary[:500] or "（无）"}

## 当前 Skill（前3000字符）
{skill_text[:3000]}

评分（0-10）：
8-10 = skill 改动直接针对 patterns，预期能显著改善
5-7  = 有改善但未完全覆盖
0-4  = 改动与 patterns 无关

只输出 JSON：{{"score": <0-10>, "note": "<一句话>"}}"""

    try:
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        m = re.search(r'\{[^}]+\}', msg.content[0].text)
        if m:
            data = json.loads(m.group())
            return float(data.get("score", 0))
    except Exception as e:
        print(f"[llm] {e}", file=sys.stderr)
    return None


def evaluate(skill_dir: Path, patterns: list[dict], feedback_summary: str) -> tuple[float, str]:
    skill_md = skill_dir / "SKILL.md"
    text = skill_md.read_text(encoding="utf-8") if skill_md.exists() else ""
    score = llm_coverage(text, patterns, feedback_summary)
    if score is not None:
        return score, "llm"
    return rule_coverage(text, patterns), "rule"


# ── Results 追踪 ──────────────────────────────────────────────────────────────

HEADER = ["round", "timestamp", "change", "score", "baseline", "delta", "decision", "hash"]


def init_tsv(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f, delimiter="\t").writerow(HEADER)


def log_row(path: Path, row: list) -> None:
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.writer(f, delimiter="\t").writerow(row)


# ── eval 子命令（Claude 自主循环时调用）──────────────────────────────────────

def cmd_eval(args: argparse.Namespace) -> None:
    """
    仅评估当前 skill 质量，输出 skill_score: X.X，退出。
    Claude 在每次修改并 git commit 后调用此命令，读取分数决定保留/回滚。
    """
    skill_dir = find_skill_dir(args.skill)
    if skill_dir is None:
        print(f"skill_score: 0.0")
        sys.exit(1)

    feedback_path = Path(args.feedback) if args.feedback else None
    patterns = load_patterns(skill_dir, feedback_path)

    feedback_summary = ""
    if feedback_path and (feedback_path.parent / "analysis.md").exists():
        feedback_summary = (feedback_path.parent / "analysis.md").read_text(encoding="utf-8")[:800]

    score, mode = evaluate(skill_dir, patterns, feedback_summary)
    # 输出格式固定，方便 grep 提取（对标 autoresearch 的 val_bpb: X）
    print(f"skill_score: {score}")
    print(f"eval_mode: {mode}")
    print(f"patterns_loaded: {len(patterns)}")


# ── run 子命令（有人监督时的交互模式）────────────────────────────────────────

def cmd_run(args: argparse.Namespace) -> None:
    skill_dir = find_skill_dir(args.skill)
    if skill_dir is None:
        print(f"找不到 skill：{args.skill}")
        sys.exit(1)

    repo_root = find_repo_root(skill_dir)
    feedback_path = Path(args.feedback) if args.feedback else None
    patterns = load_patterns(skill_dir, feedback_path)
    feedback_summary = ""
    if feedback_path and (feedback_path.parent / "analysis.md").exists():
        feedback_summary = (feedback_path.parent / "analysis.md").read_text(encoding="utf-8")[:800]

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_path = skill_dir / f"results_{ts}.tsv"
    init_tsv(results_path)

    print("=" * 55)
    print(f"  Skill Loop [run]: {skill_dir.name}")
    print(f"  Patterns: {len(patterns)}  Rounds: {args.rounds}  Target: {args.target}")
    print("=" * 55)

    baseline, mode = evaluate(skill_dir, patterns, feedback_summary)
    print(f"\n[基线] score={baseline:.2f}  mode={mode}")
    log_row(results_path, [0, datetime.now().isoformat(), "baseline",
                            baseline, baseline, 0, "baseline",
                            git("rev-parse", "--short", "HEAD", cwd=repo_root)])

    best, best_round, no_improve, stop_reason = baseline, 0, 0, ""

    for n in range(1, args.rounds + 1):
        print(f"\n{'─'*45}")
        print(f"[第 {n}/{args.rounds} 轮]  best={best:.2f}")
        print("  修改完成后按 Enter，输入 q 退出，s 跳过")
        try:
            user_input = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            stop_reason = "用户中断"
            break
        if user_input == "q":
            stop_reason = "用户退出"
            break
        if user_input == "s":
            continue

        if not git_has_changes(skill_dir, repo_root):
            print("  无变更，跳过")
            no_improve += 1
            if no_improve >= args.patience:
                stop_reason = f"连续 {args.patience} 轮无变更"
                break
            continue

        diff = git("diff", "--stat", "HEAD", "--", str(skill_dir), cwd=repo_root)
        summary = (diff.splitlines()[-1] if diff.strip() else "changes").strip()[:60]
        commit_hash = git_commit(skill_dir, f"skill-iter {n}: {summary}", repo_root)
        print(f"  commit: {commit_hash}")

        new_score, mode = evaluate(skill_dir, patterns, feedback_summary)
        delta = new_score - best
        print(f"  score={new_score:.2f}  delta={delta:+.2f}  mode={mode}")

        if new_score > best:
            print("  ✅ 保留")
            best, best_round, no_improve = new_score, n, 0
            decision = "advance"
        else:
            print("  ❌ 回滚")
            git_revert(repo_root)
            no_improve += 1
            decision = "revert"
            commit_hash = "-"

        log_row(results_path, [n, datetime.now().isoformat(), summary,
                                new_score, baseline, f"{delta:+.2f}", decision, commit_hash])

        if best >= args.target:
            stop_reason = f"达到目标分 {args.target}"
            break
        if no_improve >= args.patience:
            stop_reason = f"连续 {args.patience} 轮无改善"
            break
    else:
        stop_reason = f"达到最大轮次 {args.rounds}"

    print(f"\n{'='*55}")
    print(f"  基线={baseline:.2f} → 最佳={best:.2f}（第{best_round}轮）  {stop_reason}")
    print(f"  记录：{results_path}")
    print(f"{'='*55}")


# ── 主入口 ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Skill 迭代 harness")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # eval 子命令
    p_eval = sub.add_parser("eval", help="评估当前 skill 质量（Claude 自主循环时调用）")
    p_eval.add_argument("skill", help="skill 名称或路径")
    p_eval.add_argument("--feedback", default=None, help="feedback.xlsx 路径")

    # run 子命令
    p_run = sub.add_parser("run", help="交互式迭代循环（有人监督时使用）")
    p_run.add_argument("skill", help="skill 名称或路径")
    p_run.add_argument("--feedback", default=None, help="feedback.xlsx 路径")
    p_run.add_argument("--rounds", type=int, default=20)
    p_run.add_argument("--patience", type=int, default=5)
    p_run.add_argument("--target", type=float, default=8.5)

    args = parser.parse_args()
    if args.cmd == "eval":
        cmd_eval(args)
    else:
        cmd_run(args)


if __name__ == "__main__":
    main()
