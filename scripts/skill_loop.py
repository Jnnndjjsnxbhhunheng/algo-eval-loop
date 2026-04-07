#!/usr/bin/env python3
"""
skill_loop.py — 纯基础设施 harness

只做四件事，不做评估：
  load-cases  从 feedback.xlsx 提取 bad cases，输出 JSON 供 Claude 使用
  commit      git add + commit skill 目录
  revert      git reset --hard HEAD~1
  log         追加一行记录到 results.tsv

评估和 pipeline 执行由 Claude 通过工具调用完成（见 SKILL.md）。

用法：
  python skill_loop.py load-cases --feedback feedback.xlsx [--threshold 3] [--max 15]
  python skill_loop.py commit <skill路径> [--message "描述"]
  python skill_loop.py revert [--repo <repo根目录>]
  python skill_loop.py log --tsv results.tsv --round 3 --score 7.5 --decision advance --hash abc1234
"""

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path

try:
    import openpyxl
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False


# ── Git ───────────────────────────────────────────────────────────────────────

def git(*args: str, cwd: Path | None = None) -> str:
    r = subprocess.run(
        ["git"] + list(args),
        capture_output=True, text=True,
        cwd=str(cwd) if cwd else None,
    )
    return r.stdout.strip()


def find_repo_root(path: Path) -> Path:
    p = path.resolve()
    while p != p.parent:
        if (p / ".git").exists():
            return p
        p = p.parent
    return path.resolve()


# ── load-cases ────────────────────────────────────────────────────────────────

def cmd_load_cases(args: argparse.Namespace) -> None:
    """
    从 feedback.xlsx 提取低分 bad cases，输出 JSON。
    Claude 读取这份 JSON 后逐条跑真实 pipeline。
    """
    feedback_path = Path(args.feedback)
    if not feedback_path.exists():
        print(json.dumps({"error": f"文件不存在: {feedback_path}"}))
        sys.exit(1)

    if not HAS_OPENPYXL:
        print(json.dumps({"error": "需要 openpyxl: pip install openpyxl"}))
        sys.exit(1)

    wb = openpyxl.load_workbook(feedback_path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    wb.close()

    if not rows:
        print(json.dumps([]))
        return

    headers = [str(h) if h is not None else f"col_{i}" for i, h in enumerate(rows[0])]

    # 跳过说明行
    import re
    data_start = 1
    if len(rows) > 1:
        sample = " ".join(str(v) for v in rows[1] if v is not None)
        if re.search(r"(1-5分|填写|说明|示例)", sample):
            data_start = 2

    score_cols = [(i, h) for i, h in enumerate(headers) if h.endswith("_评分")]
    note_cols  = {h[:-3]: i for i, h in enumerate(headers) if h.endswith("_备注")}
    data_cols  = [(i, h) for i, h in enumerate(headers)
                  if not h.endswith(("_评分", "_备注"))]

    bad_cases = []
    for row_num, row in enumerate(rows[data_start:], start=data_start + 1):
        if all(v is None or str(v).strip() == "" for v in row):
            continue
        for score_idx, score_col in score_cols:
            try:
                score = float(row[score_idx])
            except (TypeError, ValueError):
                continue
            if score >= args.threshold:
                continue

            dim = score_col[:-3]
            note_idx = note_cols.get(dim)
            note = str(row[note_idx]).strip() if note_idx is not None and row[note_idx] else ""

            # 所有数据列作为输入上下文
            input_data = {
                col_name: str(row[col_idx])
                for col_idx, col_name in data_cols
                if col_idx < len(row) and row[col_idx] is not None and str(row[col_idx]).strip()
            }

            bad_cases.append({
                "row": row_num,
                "dimension": dim,
                "pm_score": score,
                "pm_note": note,
                "input": input_data,
            })

    bad_cases.sort(key=lambda x: x["pm_score"])
    bad_cases = bad_cases[:args.max]

    print(json.dumps(bad_cases, ensure_ascii=False, indent=2))


# ── commit ────────────────────────────────────────────────────────────────────

def cmd_commit(args: argparse.Namespace) -> None:
    skill_path = Path(args.skill_path)
    repo_root = find_repo_root(skill_path)
    git("add", str(skill_path.resolve()), cwd=repo_root)
    msg = args.message or f"skill-iter: update {skill_path.name}"
    result = subprocess.run(
        ["git", "commit", "-m", msg],
        capture_output=True, text=True, cwd=str(repo_root),
    )
    if result.returncode != 0 and "nothing to commit" in result.stdout + result.stderr:
        print("nothing to commit")
    else:
        commit_hash = git("rev-parse", "--short", "HEAD", cwd=repo_root)
        print(f"commit: {commit_hash}")


# ── revert ────────────────────────────────────────────────────────────────────

def cmd_revert(args: argparse.Namespace) -> None:
    repo_root = Path(args.repo).resolve() if args.repo else Path.cwd()
    git("reset", "--hard", "HEAD~1", cwd=repo_root)
    print(f"reverted to: {git('rev-parse', '--short', 'HEAD', cwd=repo_root)}")


# ── log ───────────────────────────────────────────────────────────────────────

HEADER = ["round", "timestamp", "score", "baseline", "delta", "decision", "hash", "note"]


def cmd_log(args: argparse.Namespace) -> None:
    from datetime import datetime
    tsv_path = Path(args.tsv)

    # 初始化（不存在时建表头）
    if not tsv_path.exists():
        tsv_path.parent.mkdir(parents=True, exist_ok=True)
        with open(tsv_path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f, delimiter="\t").writerow(HEADER)

    # 读取基线（第一行数据，round=0）
    baseline = args.score
    rows_existing = []
    try:
        with open(tsv_path, encoding="utf-8") as f:
            rows_existing = list(csv.DictReader(f, delimiter="\t"))
        if rows_existing:
            baseline_row = next((r for r in rows_existing if r["round"] == "0"), None)
            if baseline_row:
                baseline = float(baseline_row["score"])
    except Exception:
        pass

    delta = round(args.score - baseline, 2) if args.round > 0 else 0

    with open(tsv_path, "a", newline="", encoding="utf-8") as f:
        csv.writer(f, delimiter="\t").writerow([
            args.round,
            datetime.now().isoformat(),
            args.score,
            baseline,
            f"{delta:+.2f}",
            args.decision,
            args.hash or "-",
            args.note or "",
        ])

    print(f"logged: round={args.round} score={args.score} decision={args.decision}")


# ── 主入口 ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Skill 迭代基础设施 harness（不含评估逻辑）")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # load-cases
    p = sub.add_parser("load-cases", help="从 feedback.xlsx 提取 bad cases → JSON")
    p.add_argument("--feedback", required=True, help="feedback.xlsx 路径")
    p.add_argument("--threshold", type=float, default=3.0, help="低分阈值（默认 3.0）")
    p.add_argument("--max", type=int, default=15, help="最多返回几条（默认 15）")

    # commit
    p = sub.add_parser("commit", help="git add + commit skill 目录")
    p.add_argument("skill_path", help="skill 目录路径")
    p.add_argument("--message", "-m", default=None, help="commit message")

    # revert
    p = sub.add_parser("revert", help="git reset --hard HEAD~1")
    p.add_argument("--repo", default=None, help="git repo 根目录（默认 cwd）")

    # log
    p = sub.add_parser("log", help="追加一行到 results.tsv")
    p.add_argument("--tsv", required=True, help="results.tsv 路径")
    p.add_argument("--round", type=int, required=True, help="轮次编号")
    p.add_argument("--score", type=float, required=True, help="本轮得分")
    p.add_argument("--decision", required=True, choices=["baseline", "advance", "revert", "skip"])
    p.add_argument("--hash", default=None, help="commit hash")
    p.add_argument("--note", default=None, help="备注（可选）")

    args = parser.parse_args()
    {
        "load-cases": cmd_load_cases,
        "commit": cmd_commit,
        "revert": cmd_revert,
        "log": cmd_log,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
