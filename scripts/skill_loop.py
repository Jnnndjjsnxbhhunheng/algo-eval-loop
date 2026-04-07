#!/usr/bin/env python3
"""
skill_loop.py — 基于 feedback.xlsx 语义评估的 Skill 迭代 harness

评估逻辑（语义而非关键词）：
  1. 从 feedback.xlsx 提取低分 bad cases（含输入数据 + PM 备注）
  2. LLM 判断：当前 skill 指令，能否解决这些具体 case？
  3. 分数 = 被解决的 bad case 比例 × 10

两种使用方式：
  eval 模式（Claude 自主循环时调用，只打分退出）：
    python scripts/skill_loop.py eval <skill名称> --feedback feedback.xlsx

  run 模式（有人监督的交互循环）：
    python scripts/skill_loop.py run <skill名称> --feedback feedback.xlsx [--rounds 20]
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


def collect_skill_text(skill_dir: Path) -> str:
    """收集 skill 目录下所有相关文本文件内容。"""
    parts = []
    for f in sorted(skill_dir.rglob("*")):
        if f.suffix in (".md", ".py", ".ts", ".txt", ".json") and f.stat().st_size < 30000:
            rel = f.relative_to(skill_dir)
            parts.append(f"### {rel}\n{f.read_text(encoding='utf-8', errors='replace')}")
    return "\n\n".join(parts)


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


# ── feedback.xlsx 解析 ────────────────────────────────────────────────────────

def load_bad_cases(feedback_path: Path, low_threshold: float = 3.0, max_cases: int = 15) -> list[dict]:
    """
    从 feedback.xlsx 提取低分 bad cases。
    返回格式：[{"input": ..., "score": ..., "note": ..., "dimension": ...}, ...]
    """
    try:
        import openpyxl
    except ImportError:
        print("[warn] 需要 openpyxl：pip install openpyxl", file=sys.stderr)
        return []

    try:
        wb = openpyxl.load_workbook(feedback_path, read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        wb.close()
    except Exception as e:
        print(f"[warn] 读取 feedback.xlsx 失败：{e}", file=sys.stderr)
        return []

    if len(rows) < 2:
        return []

    headers = [str(h) if h is not None else f"col_{i}" for i, h in enumerate(rows[0])]

    # 跳过说明行
    data_start = 1
    if len(rows) > 1:
        sample = " ".join(str(v) for v in rows[1] if v is not None)
        if re.search(r"(1-5分|填写|说明)", sample):
            data_start = 2

    # 识别评分列和备注列
    score_cols = [(i, h) for i, h in enumerate(headers) if h.endswith("_评分")]
    note_cols  = {h[:-3]: i for i, h in enumerate(headers) if h.endswith("_备注")}
    data_cols  = [i for i, h in enumerate(headers) if not h.endswith(("_评分", "_备注"))]

    bad_cases = []
    for row in rows[data_start:]:
        if all(v is None or str(v).strip() == "" for v in row):
            continue
        for score_idx, score_col in score_cols:
            try:
                score = float(row[score_idx])
            except (TypeError, ValueError):
                continue
            if score >= low_threshold:
                continue

            dim = score_col[:-3]
            note_idx = note_cols.get(dim)
            note = str(row[note_idx]).strip() if note_idx is not None and row[note_idx] else ""

            # 用数据列组成"输入"描述
            input_parts = {
                headers[i]: str(row[i]) for i in data_cols
                if i < len(row) and row[i] is not None and str(row[i]).strip()
            }

            bad_cases.append({
                "dimension": dim,
                "score": score,
                "note": note,
                "input": input_parts,
            })

    # 按分数从低到高排序，取最严重的
    bad_cases.sort(key=lambda x: x["score"])
    return bad_cases[:max_cases]


# ── 语义评估（核心）──────────────────────────────────────────────────────────

def semantic_eval(skill_text: str, bad_cases: list[dict]) -> tuple[float, str]:
    """
    LLM 语义评估：对每个 bad case，判断当前 skill 指令能否解决它。
    返回 (score 0-10, detail)。
    """
    if not bad_cases:
        return _rule_fallback(skill_text), "rule(no-cases)"

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        return _rule_fallback(skill_text), "rule(no-api-key)"

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
    except ImportError:
        return _rule_fallback(skill_text), "rule(no-anthropic)"

    resolved = 0
    details = []

    for case in bad_cases:
        input_str = json.dumps(case["input"], ensure_ascii=False)
        prompt = f"""你是 skill 质量评审专家。判断改进后的 skill 指令，能否解决 PM 指出的具体问题。

## 当前 Skill 指令
{skill_text[:4000]}

## PM 评估的 Bad Case
- 评估维度：{case['dimension']}
- PM 打分：{case['score']}/5（低分）
- PM 备注：{case['note'] or '（无备注）'}
- 对应输入数据：{input_str[:500]}

## 问题
如果算法严格遵循上述 skill 指令处理这条输入，能否避免 PM 指出的问题？

只回答 JSON：{{"resolved": true/false, "reason": "<一句话说明>"}}"""

        try:
            msg = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=128,
                messages=[{"role": "user", "content": prompt}],
            )
            raw = msg.content[0].text
            m = re.search(r'\{[^}]+\}', raw)
            if m:
                data = json.loads(m.group())
                if data.get("resolved"):
                    resolved += 1
                    details.append(f"✅ {case['dimension']}: {data.get('reason','')[:40]}")
                else:
                    details.append(f"❌ {case['dimension']}: {data.get('reason','')[:40]}")
        except Exception as e:
            print(f"  [llm] case 评估失败：{e}", file=sys.stderr)

    evaluated = len([d for d in details if d])
    if evaluated == 0:
        return _rule_fallback(skill_text), "rule(llm-failed)"

    score = round(resolved / len(bad_cases) * 10.0, 2)
    detail = f"resolved {resolved}/{len(bad_cases)} bad cases"
    return score, f"semantic({detail})"


def _rule_fallback(skill_text: str) -> float:
    """无 API Key 或解析失败时的规则兜底评分。"""
    score = 0.0
    score += min(len(re.findall(r'"[^"]{3,}"', skill_text)) * 0.4, 3.0)
    score += min(len(re.findall(r"^\d+\.\s", skill_text, re.MULTILINE)) * 0.5, 3.0)
    if re.search(r"(问用户|不确定|判断不了|直接问)", skill_text):
        score += 2.0
    if re.search(r"(如果|否则|根据|当.*时)", skill_text):
        score += 2.0
    return round(min(score, 10.0), 2)


def evaluate(skill_dir: Path, feedback_path: Path | None) -> tuple[float, str]:
    skill_text = collect_skill_text(skill_dir)
    if not feedback_path or not feedback_path.exists():
        return _rule_fallback(skill_text), "rule(no-feedback)"
    bad_cases = load_bad_cases(feedback_path)
    if not bad_cases:
        return _rule_fallback(skill_text), "rule(no-bad-cases)"
    return semantic_eval(skill_text, bad_cases)


# ── Results 追踪 ──────────────────────────────────────────────────────────────

HEADER = ["round", "timestamp", "change", "score", "baseline", "delta", "decision", "hash", "eval_mode"]


def init_tsv(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        csv.writer(f, delimiter="\t").writerow(HEADER)


def log_row(path: Path, row: list) -> None:
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.writer(f, delimiter="\t").writerow(row)


# ── eval 子命令（Claude 自主循环调用）────────────────────────────────────────

def cmd_eval(args: argparse.Namespace) -> None:
    """
    评估当前 skill 质量，输出 skill_score: X.X，退出。
    Claude 在每次 git commit 后调用此命令读取分数。
    """
    skill_dir = find_skill_dir(args.skill)
    if skill_dir is None:
        print("skill_score: 0.0")
        print("eval_mode: error(skill-not-found)")
        sys.exit(1)

    feedback_path = Path(args.feedback) if args.feedback else None
    score, mode = evaluate(skill_dir, feedback_path)

    # 固定输出格式，方便 grep（对标 autoresearch 的 val_bpb: X）
    print(f"skill_score: {score}")
    print(f"eval_mode: {mode}")
    if feedback_path:
        bad_cases = load_bad_cases(feedback_path) if feedback_path.exists() else []
        print(f"bad_cases_total: {len(bad_cases)}")


# ── run 子命令（有人监督的交互模式）──────────────────────────────────────────

def cmd_run(args: argparse.Namespace) -> None:
    skill_dir = find_skill_dir(args.skill)
    if skill_dir is None:
        print(f"找不到 skill：{args.skill}")
        sys.exit(1)

    repo_root = find_repo_root(skill_dir)
    feedback_path = Path(args.feedback) if args.feedback else None

    # 预加载 bad cases，供循环复用
    bad_cases = load_bad_cases(feedback_path) if (feedback_path and feedback_path.exists()) else []

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    results_path = skill_dir / f"results_{ts}.tsv"
    init_tsv(results_path)

    print("=" * 55)
    print(f"  Skill Loop [run]: {skill_dir.name}")
    print(f"  Feedback:  {feedback_path}")
    print(f"  Bad cases: {len(bad_cases)} 条（低分 case）")
    print(f"  Rounds: {args.rounds}  Patience: {args.patience}  Target: {args.target}")
    print("=" * 55)

    if not bad_cases:
        print("\n⚠ 未提取到 bad cases，将使用规则评分（效果较差）")

    # 基线
    baseline, mode = evaluate(skill_dir, feedback_path)
    print(f"\n[基线] score={baseline:.2f}  mode={mode}")
    log_row(results_path, [
        0, datetime.now().isoformat(), "baseline",
        baseline, baseline, 0, "baseline",
        git("rev-parse", "--short", "HEAD", cwd=repo_root), mode,
    ])

    best, best_round, no_improve, stop_reason = baseline, 0, 0, ""

    for n in range(1, args.rounds + 1):
        print(f"\n{'─'*45}")
        print(f"[第 {n}/{args.rounds} 轮]  best={best:.2f}/{args.target}")
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

        new_score, mode = evaluate(skill_dir, feedback_path)
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

        log_row(results_path, [
            n, datetime.now().isoformat(), summary,
            new_score, baseline, f"{delta:+.2f}", decision, commit_hash, mode,
        ])

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
    parser = argparse.ArgumentParser(description="基于 feedback.xlsx 语义评估的 Skill 迭代 harness")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_eval = sub.add_parser("eval", help="语义评估当前 skill（Claude 自主循环时调用）")
    p_eval.add_argument("skill", help="skill 名称或路径")
    p_eval.add_argument("--feedback", required=True, help="feedback.xlsx 路径")

    p_run = sub.add_parser("run", help="有人监督的交互式迭代循环")
    p_run.add_argument("skill", help="skill 名称或路径")
    p_run.add_argument("--feedback", required=True, help="feedback.xlsx 路径")
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
