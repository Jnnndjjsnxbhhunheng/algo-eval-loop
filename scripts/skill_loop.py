#!/usr/bin/env python3
"""
skill_loop.py — Skill 自主迭代 harness

对标 karpathy/autoresearch 的核心循环，专为迭代目标 skill 设计。
唯一职责：git 状态管理 + 结果追踪 + 保留/回滚决策。
"改什么"和"评估"由 Claude 通过 SKILL.md 指令完成。

用法：
    python skill_loop.py <skill_name_or_path> [--rounds 20] [--patience 5] [--target 8.5]

流程（对标 autoresearch）：
    1. 找到目标 skill 目录
    2. 评估当前质量，建立基线（baseline_score）
    3. 循环：
       a. 等待 Claude 修改 skill 文件（SKILL.md / 代码 / 子 MD）
       b. git commit 变更
       c. 重新评估质量
       d. 改善 → 保留；退化 → git reset 回滚
       e. 追加写入 results.tsv
    4. 达到停止条件时退出，生成摘要
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
    """按名称查找 skill 目录（含 SKILL.md）。"""
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


# ── 评估（Claude 是 evaluator，这里只做规则兜底）────────────────────────────────

def score_skill(skill_dir: Path) -> float:
    """
    规则评分（0-10），仅作为无 API Key 时的兜底。
    有 ANTHROPIC_API_KEY 时，由 Claude API 评估（见 llm_score）。
    """
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return 0.0

    text = skill_md.read_text(encoding="utf-8")

    score = 0.0
    # 触发词丰富度
    triggers = re.findall(r'"[^"]{3,}"', text)
    score += min(len(triggers) * 0.4, 3.0)
    # 步骤完整性
    steps = len(re.findall(r"^\d+\.\s", text, re.MULTILINE))
    score += min(steps * 0.5, 3.0)
    # 有边界处理
    if re.search(r"(问用户|不确定|判断不了|直接问)", text):
        score += 2.0
    # 有条件分支
    if re.search(r"(如果|否则|根据)", text):
        score += 2.0

    return round(min(score, 10.0), 2)


def llm_score(skill_dir: Path) -> float | None:
    """用 Claude API 评估 skill 质量（需要 ANTHROPIC_API_KEY）。"""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return None

    try:
        import anthropic
    except ImportError:
        return None

    skill_md = skill_dir / "SKILL.md"
    if not skill_md.exists():
        return None

    # 收集 skill 目录所有文本文件
    files_content = ""
    for f in sorted(skill_dir.rglob("*")):
        if f.suffix in (".md", ".py", ".txt", ".json") and f.stat().st_size < 50000:
            rel = f.relative_to(skill_dir)
            files_content += f"\n### {rel}\n{f.read_text(encoding='utf-8', errors='replace')}\n"

    prompt = f"""评估以下 Claude Code skill 的质量，输出 0-10 的分数（JSON格式）。

评分维度：
- 触发准确性（触发词是否覆盖真实场景）
- 步骤完整性（操作路径是否清晰可执行）
- 边界处理（不确定时是否有明确策略）
- 描述清晰度（无歧义，有示例）

{files_content}

只输出 JSON：{{"score": <0-10>, "top_issue": "<最主要的一个问题>", "direction": "<本轮建议改哪个方向>"}}"""

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
            return float(data.get("score", 0))
    except Exception as e:
        print(f"  [llm_score] {e}", file=sys.stderr)

    return None


def evaluate(skill_dir: Path) -> tuple[float, str]:
    """评估 skill，返回 (score, mode)。"""
    score = llm_score(skill_dir)
    if score is not None:
        return score, "llm"
    return score_skill(skill_dir), "rule"


# ── Results 追踪（对标 autoresearch 的 results.tsv）──────────────────────────

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
    parser = argparse.ArgumentParser(description="Skill 自主迭代 harness")
    parser.add_argument("skill", help="skill 名称或路径")
    parser.add_argument("--rounds", type=int, default=20, help="最大轮次（默认 20）")
    parser.add_argument("--patience", type=int, default=5, help="连续无改善停止（默认 5）")
    parser.add_argument("--target", type=float, default=8.5, help="目标分（默认 8.5）")
    args = parser.parse_args()

    # 找 skill
    skill_dir = find_skill_dir(args.skill)
    if skill_dir is None:
        print(f"找不到 skill：{args.skill}")
        sys.exit(1)

    repo_root = find_repo_root(skill_dir)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_path = skill_dir / f"results_{ts}.tsv"
    init_tsv(results_path)

    print("=" * 55)
    print(f"  Skill Loop: {skill_dir.name}")
    print(f"  路径: {skill_dir}")
    print(f"  最大轮次: {args.rounds}  耐心值: {args.patience}  目标: {args.target}")
    print("=" * 55)

    # 基线
    baseline, mode = evaluate(skill_dir)
    print(f"\n[基线] score={baseline}  mode={mode}")
    log_row(results_path, [0, datetime.now().isoformat(), "baseline",
                            baseline, baseline, 0, "baseline",
                            git("rev-parse", "--short", "HEAD", cwd=repo_root)])

    best = baseline
    best_round = 0
    no_improve = 0
    stop_reason = ""

    for n in range(1, args.rounds + 1):
        print(f"\n{'─'*45}")
        print(f"[第 {n}/{args.rounds} 轮]  best={best:.2f}")
        print("  等待 skill 文件变更（Claude 修改后按 Enter 继续，或输入 q 退出）...")

        try:
            user_input = input("  > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            stop_reason = "用户中断"
            break

        if user_input == "q":
            stop_reason = "用户退出"
            break

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
        print(f"  commit: {commit_hash}")

        # 评估
        new_score, mode = evaluate(skill_dir)
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

    # 摘要
    print(f"\n{'='*55}")
    print(f"  完成  skill={skill_dir.name}")
    print(f"  基线={baseline:.2f}  最佳={best:.2f}（第{best_round}轮）")
    print(f"  停止原因：{stop_reason}")
    print(f"  记录：{results_path}")
    print(f"{'='*55}")


if __name__ == "__main__":
    main()
