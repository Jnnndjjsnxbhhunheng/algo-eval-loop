#!/usr/bin/env python3
"""
skill_loop.py — 纯基础设施 harness

五个子命令：
  load-cases  从 feedback.xlsx 提取 bad cases，输出 JSON 供 Claude 使用
  evaluate    并发调用 LLM API 跑 bad cases（batch=10），输出 eval_results.json
  commit      git add + commit skill 目录
  revert      git reset --hard HEAD~1
  log         追加一行记录到 results.tsv

用法：
  python skill_loop.py load-cases --feedback feedback.xlsx [--threshold 3] [--max 15]
  python skill_loop.py evaluate --results pipeline_results.json [--batch 10] [--output eval_results.json]
  python skill_loop.py commit <skill路径> [--message "描述"]
  python skill_loop.py revert [--repo <repo根目录>]
  python skill_loop.py log --tsv results.tsv --round 3 --score 7.5 --decision advance --hash abc1234

环境变量：
  OPENAI_API_KEY     API 密钥（必填）
  OPENAI_BASE_URL    API base URL（必填，如 https://open.bigmodel.cn/api/paas/v4）
  OPENAI_MODEL       模型名称（默认 GLM-5，可被 --model 覆盖）
"""

import argparse
import asyncio
import csv
import json
import os
import re
import subprocess
import sys
from pathlib import Path

try:
    from openai import AsyncOpenAI
    HAS_OPENAI = True
except ImportError:
    HAS_OPENAI = False

try:
    import openpyxl
    HAS_OPENPYXL = True
except ImportError:
    HAS_OPENPYXL = False

DEFAULT_EVAL_MODEL = os.environ.get("OPENAI_MODEL", "GLM-5")


# ── evaluate（并发评估） ────────────────────────────────────────────────────────

EVAL_PROMPT_TEMPLATE = """\
你是一个评估助手，判断 pipeline 的真实输出是否解决了 PM 的备注问题。

【PM 备注】（必须完全解决其中所有问题，才算 resolved=True）
{pm_note}

【Pipeline 真实输出】
{actual_output}

请以如下 JSON 格式输出（只输出 JSON，不要其他文字）：
{{"resolved": true 或 false, "reason": "哪些问题解决了、哪些没有（50字以内）"}}
"""


async def _run_one_case(semaphore: asyncio.Semaphore, client: "AsyncOpenAI", case: dict, idx: int, total: int, model: str) -> dict:
    """在 semaphore 控制下，调用 LLM API 判断单条真实输出是否 resolved。"""
    async with semaphore:
        actual_output = case.get("actual_output", "")
        if not actual_output:
            print(f"  [{idx+1}/{total}] row={case.get('row','?')} SKIP 缺少 actual_output", file=sys.stderr)
            return {**case, "resolved": False, "reason": "缺少 actual_output 字段"}

        prompt = EVAL_PROMPT_TEMPLATE.format(
            pm_note=case.get("pm_note", ""),
            actual_output=actual_output,
        )

        try:
            response = await client.chat.completions.create(
                model=model,
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = response.choices[0].message.content.strip()
        except Exception as e:
            print(f"  [{idx+1}/{total}] row={case.get('row','?')} ERROR: {e}", file=sys.stderr)
            return {**case, "resolved": False, "reason": f"执行错误: {e}"}

        result_json = _extract_json(raw)
        resolved = bool(result_json.get("resolved", False)) if result_json else False
        mark = "✓" if resolved else "✗"
        print(f"  [{idx+1}/{total}] row={case.get('row','?')} {mark}  {result_json.get('reason','')[:60] if result_json else raw[:60]}",
              file=sys.stderr)

        return {
            **case,
            "resolved": resolved,
            "reason": result_json.get("reason", raw[:200]) if result_json else raw[:200],
        }


def _extract_json(text: str) -> dict | None:
    """从文本中提取第一个 JSON 对象。"""
    # 直接解析
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # 找 {...} 块
    match = re.search(r'\{[^{}]*"resolved"[^{}]*\}', text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group())
        except json.JSONDecodeError:
            pass
    return None


async def _evaluate_all(cases: list, batch: int, model: str) -> list:
    client = AsyncOpenAI(
        api_key=os.environ.get("OPENAI_API_KEY"),
        base_url=os.environ.get("OPENAI_BASE_URL"),
    )
    semaphore = asyncio.Semaphore(batch)
    tasks = [
        _run_one_case(semaphore, client, case, idx, len(cases), model)
        for idx, case in enumerate(cases)
    ]
    return await asyncio.gather(*tasks)


def cmd_evaluate(args: argparse.Namespace) -> None:
    """
    并发调用 LLM API 评判真实 pipeline 输出，输出 eval_results.json 和汇总分数。

    输入文件格式（每条必须包含 actual_output 和 pm_note）：
    [
      {
        "row": 2,
        "dimension": "完整性",
        "pm_score": 2.0,
        "pm_note": "缺少主流品牌 SKG、倍轻松",
        "actual_output": "pipeline 实际输出的内容（品牌榜文本等）",
        ...
      }
    ]
    """
    if not HAS_OPENAI:
        print(json.dumps({"error": "需要 openai SDK: pip install openai"}))
        sys.exit(1)
    if not os.environ.get("OPENAI_API_KEY"):
        print(json.dumps({"error": "缺少环境变量 OPENAI_API_KEY"}))
        sys.exit(1)
    if not os.environ.get("OPENAI_BASE_URL"):
        print(json.dumps({"error": "缺少环境变量 OPENAI_BASE_URL"}))
        sys.exit(1)

    results_path = Path(args.results)
    if not results_path.exists():
        print(json.dumps({"error": f"文件不存在: {results_path}"}))
        sys.exit(1)
    cases = json.loads(results_path.read_text("utf-8"))
    if not cases:
        print(json.dumps({"score": 0, "resolved": 0, "total": 0, "results": []}))
        return

    model = getattr(args, "model", None) or DEFAULT_EVAL_MODEL
    print(f"开始评估：{len(cases)} 条结果，并发 batch={args.batch}，模型={model}", file=sys.stderr)

    results = asyncio.run(_evaluate_all(cases, args.batch, model))

    resolved_count = sum(1 for r in results if r.get("resolved"))
    score = resolved_count / len(results) * 10

    output = {
        "score": round(score, 2),
        "resolved": resolved_count,
        "total": len(results),
        "results": results,
    }

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), "utf-8")

    print(f"\n评估完成：score={score:.1f}  resolved={resolved_count}/{len(results)}", file=sys.stderr)
    print(json.dumps({"score": round(score, 2), "resolved": resolved_count, "total": len(results)}))





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

VALID_CHANGE_TYPES = {"prompt_instruction", "logic", "checkpoint"}

def cmd_commit(args: argparse.Namespace) -> None:
    # ── overfit 自检 gate ──────────────────────────────────────────────────────
    change_type = args.change_type.strip().lower()
    if change_type not in VALID_CHANGE_TYPES:
        print(
            f"Error: --change-type='{change_type}' 不合法。\n"
            f"只允许：{', '.join(sorted(VALID_CHANGE_TYPES))}\n"
            "'data' / 'example' 类修改属于 overfit，禁止提交。\n"
            "请回到 Step 1 重新分析根因，找通用改进方向。",
            file=sys.stderr,
        )
        sys.exit(1)

    if args.generalizes.strip().lower() != "yes":
        print(
            "Error: --generalizes 必须为 'yes'。\n"
            "当前修改对未见过的新输入无效，属于特解，禁止提交。\n"
            "请回到 Step 1 重新分析根因。",
            file=sys.stderr,
        )
        sys.exit(1)

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
    parser = argparse.ArgumentParser(description="Skill 迭代基础设施 harness")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # load-cases
    p = sub.add_parser("load-cases", help="从 feedback.xlsx 提取 bad cases → JSON")
    p.add_argument("--feedback", required=True, help="feedback.xlsx 路径")
    p.add_argument("--threshold", type=float, default=3.0, help="低分阈值（默认 3.0）")
    p.add_argument("--max", type=int, default=15, help="最多返回几条（默认 15）")

    # evaluate
    p = sub.add_parser("evaluate", help="并发调用 LLM API 评判真实 pipeline 输出，输出得分")
    p.add_argument("--results", required=True, help="pipeline 运行结果 JSON（每条含 actual_output + pm_note）")
    p.add_argument("--batch", type=int, default=10, help="并发量（默认 10）")
    p.add_argument("--model", default=None, help="模型名称，覆盖 OPENAI_MODEL 环境变量")
    p.add_argument("--output", default="eval_results.json", help="结果输出路径（默认 eval_results.json）")

    # commit
    p = sub.add_parser("commit", help="git add + commit skill 目录（需通过 overfit 自检）")
    p.add_argument("skill_path", help="skill 目录路径")
    p.add_argument("--change-type", required=True,
                   help=f"修改类型，只允许：{', '.join(sorted(VALID_CHANGE_TYPES))}。填 data/example 会被拒绝")
    p.add_argument("--generalizes", required=True,
                   help="换成未见过的新输入，修改仍然有效吗？必须填 yes，填 no 会被拒绝")
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
        "evaluate":   cmd_evaluate,
        "commit":     cmd_commit,
        "revert":     cmd_revert,
        "log":        cmd_log,
    }[args.cmd](args)


if __name__ == "__main__":
    main()
