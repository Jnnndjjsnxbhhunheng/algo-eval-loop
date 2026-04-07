#!/usr/bin/env python3
"""
自动迭代循环运行器（Auto-Loop Runner）

借鉴 karpathy/autoresearch 理念：
  修改代码 → git commit → 运行 → 评估 → 保留/回滚 → 循环

用法：
    python scripts/autoloop.py --script train.py \
                               --metric val_loss \
                               --direction lower \
                               --max-rounds 20 \
                               --timeout 300 \
                               --patience 5

    python scripts/autoloop.py --script run.py \
                               --eval-script scripts/auto_eval.py \
                               --metric accuracy \
                               --direction higher \
                               --max-rounds 10
"""

import argparse
import csv
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


# ── 配置 ──────────────────────────────────────────────────────────────────────

DEFAULT_MAX_ROUNDS = 20
DEFAULT_TIMEOUT = 300       # 5 分钟
DEFAULT_PATIENCE = 5        # 连续无改善轮次
DEFAULT_WORKSPACE = "eval-workspace"


# ── Git 操作 ──────────────────────────────────────────────────────────────────

def git_run(*args: str) -> str:
    result = subprocess.run(
        ["git"] + list(args),
        capture_output=True, text=True, timeout=30,
    )
    return result.stdout.strip()


def git_current_branch() -> str:
    return git_run("rev-parse", "--abbrev-ref", "HEAD")


def git_create_branch(name: str) -> None:
    git_run("checkout", "-b", name)


def git_commit(message: str) -> str:
    git_run("add", "-A")
    git_run("commit", "-m", message)
    return git_run("rev-parse", "--short", "HEAD")


def git_revert_last() -> None:
    git_run("reset", "--hard", "HEAD~1")


def git_has_changes() -> bool:
    status = git_run("status", "--porcelain")
    return len(status) > 0


# ── 指标提取 ───────────────────────────────────────────────────────────────────

def extract_metric_from_output(output: str, metric_name: str) -> float | None:
    """
    从脚本输出中提取指标值。
    支持格式：
      metric_name: 0.123
      metric_name = 0.123
      metric_name  0.123
      {"metric_name": 0.123}
    取最后一次出现的值（训练过程中指标会多次打印，取最终值）。
    """
    patterns = [
        rf"{re.escape(metric_name)}\s*[:=]\s*([\d.eE+\-]+)",
        rf'"{re.escape(metric_name)}"\s*:\s*([\d.eE+\-]+)',
        rf"{re.escape(metric_name)}\s+([\d.eE+\-]+)",
    ]
    last_value = None
    for pattern in patterns:
        for match in re.finditer(pattern, output, re.IGNORECASE):
            try:
                last_value = float(match.group(1))
            except ValueError:
                pass
    return last_value


# ── 脚本运行 ──────────────────────────────────────────────────────────────────

def run_script(script_path: str, timeout: int) -> tuple[str, int, float]:
    """运行脚本，返回 (output, return_code, elapsed_seconds)。"""
    start = time.time()
    try:
        result = subprocess.run(
            ["python", script_path],
            capture_output=True, text=True,
            timeout=timeout,
        )
        elapsed = time.time() - start
        output = result.stdout + "\n" + result.stderr
        return output, result.returncode, elapsed
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        return f"TIMEOUT after {timeout}s", -1, elapsed
    except Exception as e:
        elapsed = time.time() - start
        return f"ERROR: {e}", -2, elapsed


def run_eval_script(eval_script: str, output_dir: str, metric_name: str) -> float | None:
    """运行独立的评估脚本，提取指标。"""
    try:
        result = subprocess.run(
            ["python", eval_script, "--output-dir", output_dir, "--metric", metric_name],
            capture_output=True, text=True, timeout=120,
        )
        combined = result.stdout + "\n" + result.stderr
        return extract_metric_from_output(combined, metric_name)
    except Exception:
        return None


# ── Results 追踪 ──────────────────────────────────────────────────────────────

RESULTS_HEADER = ["轮次", "时间戳", "改动描述", "指标名", "指标值", "基线值", "变化", "决策", "commit_hash", "耗时(s)"]


def init_results(results_path: Path) -> None:
    results_path.parent.mkdir(parents=True, exist_ok=True)
    with open(results_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(RESULTS_HEADER)


def append_result(results_path: Path, row: list) -> None:
    with open(results_path, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, delimiter="\t")
        writer.writerow(row)


# ── 决策逻辑 ──────────────────────────────────────────────────────────────────

def is_improvement(new_val: float, best_val: float, direction: str) -> bool:
    if direction == "lower":
        return new_val < best_val
    return new_val > best_val


def format_delta(new_val: float, baseline: float) -> str:
    delta = new_val - baseline
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta:.6f}"


# ── 摘要生成 ───────────────────────────────────────────────────────────────────

def generate_summary(
    results_path: Path,
    summary_path: Path,
    original_branch: str,
    metric_name: str,
    direction: str,
    baseline: float | None,
    best_val: float | None,
    best_round: int,
    total_rounds: int,
    stop_reason: str,
) -> None:
    lines = [
        "# 自动迭代循环摘要报告",
        f"\n生成时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"原始分支：{original_branch}",
        f"评估指标：{metric_name}（{'越低越好' if direction == 'lower' else '越高越好'}）",
        f"总轮次：{total_rounds}",
        f"停止原因：{stop_reason}",
        "",
        "## 结果概览",
        "",
        f"- 基线指标：{baseline}",
        f"- 最佳指标：{best_val}（第 {best_round} 轮）",
    ]

    if baseline is not None and best_val is not None:
        improvement = abs(best_val - baseline)
        pct = (improvement / abs(baseline) * 100) if baseline != 0 else 0
        lines.append(f"- 总改善：{improvement:.6f}（{pct:.2f}%）")

    # 统计决策分布
    advances = 0
    reverts = 0
    crashes = 0
    try:
        with open(results_path, encoding="utf-8") as f:
            reader = csv.DictReader(f, delimiter="\t")
            for row in reader:
                decision = row.get("决策", "")
                if "advance" in decision:
                    advances += 1
                elif "revert" in decision:
                    reverts += 1
                elif "crash" in decision:
                    crashes += 1
    except Exception:
        pass

    lines += [
        "",
        "## 实验统计",
        "",
        f"- ✅ 保留（advance）：{advances} 轮",
        f"- ❌ 回滚（revert）：{reverts} 轮",
        f"- 💥 崩溃（crash）：{crashes} 轮",
        "",
        "## 详细记录",
        "",
        f"完整实验记录见 `{results_path.name}`",
        "",
        "## 下一步",
        "",
        "- [ ] 检查最佳版本的代码改动",
        "- [ ] 将结果转为 Excel 给产品做主观评估",
        "- [ ] 如需继续优化，可再次启动自动迭代",
    ]

    summary_path.write_text("\n".join(lines), encoding="utf-8")


# ── 主循环 ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="自动迭代循环运行器 (Auto-Loop Runner)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例：
  # 最小化 val_loss，每轮5分钟超时，最多跑20轮
  python scripts/autoloop.py --script train.py --metric val_loss --direction lower

  # 最大化 accuracy，用独立评估脚本
  python scripts/autoloop.py --script run.py --eval-script scripts/auto_eval.py \\
                             --metric accuracy --direction higher --max-rounds 10

  # 恢复上次中断的循环
  python scripts/autoloop.py --script train.py --metric val_loss --direction lower --resume
        """,
    )
    parser.add_argument("--script", required=True, help="算法入口脚本路径")
    parser.add_argument("--metric", required=True, help="评估指标名称（从脚本输出中提取）")
    parser.add_argument("--direction", choices=["higher", "lower"], required=True, help="优化方向")
    parser.add_argument("--eval-script", default=None, help="独立评估脚本（可选，默认从主脚本输出提取指标）")
    parser.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS, help=f"最大轮次（默认 {DEFAULT_MAX_ROUNDS}）")
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"每轮超时秒数（默认 {DEFAULT_TIMEOUT}）")
    parser.add_argument("--patience", type=int, default=DEFAULT_PATIENCE, help=f"连续无改善停止轮次（默认 {DEFAULT_PATIENCE}）")
    parser.add_argument("--target", type=float, default=None, help="目标指标阈值（达到即停止）")
    parser.add_argument("--workspace", default=DEFAULT_WORKSPACE, help="工作目录")
    parser.add_argument("--resume", action="store_true", help="从上次中断处恢复")
    parser.add_argument("--dry-run", action="store_true", help="只跑基线，不进入循环")
    args = parser.parse_args()

    script_path = args.script
    if not Path(script_path).exists():
        sys.exit(f"脚本不存在：{script_path}")

    workspace = Path(args.workspace)
    workspace.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    autoloop_dir = workspace / "autoloop" / timestamp
    autoloop_dir.mkdir(parents=True, exist_ok=True)
    results_path = autoloop_dir / "results.tsv"
    summary_path = autoloop_dir / "autoloop_summary.md"

    original_branch = git_current_branch()
    branch_name = f"autoloop/{timestamp}"

    print("=" * 60)
    print("  自动迭代循环 (Auto-Loop Runner)")
    print("=" * 60)
    print(f"  脚本：{script_path}")
    print(f"  指标：{args.metric}（{'越低越好' if args.direction == 'lower' else '越高越好'}）")
    print(f"  最大轮次：{args.max_rounds}")
    print(f"  每轮超时：{args.timeout}s")
    print(f"  耐心值：{args.patience} 轮")
    if args.target is not None:
        print(f"  目标阈值：{args.target}")
    print(f"  实验分支：{branch_name}")
    print(f"  结果目录：{autoloop_dir}")
    print("=" * 60)

    # 创建实验分支
    if not args.resume:
        git_create_branch(branch_name)
        init_results(results_path)

    # 跑基线
    print("\n[基线] 运行基线版本...")
    output, retcode, elapsed = run_script(script_path, args.timeout)
    if retcode != 0:
        print(f"[基线] 基线运行失败（返回码 {retcode}）：")
        print(output[-500:] if len(output) > 500 else output)
        sys.exit(1)

    baseline = extract_metric_from_output(output, args.metric)
    if baseline is None and args.eval_script:
        baseline = run_eval_script(args.eval_script, ".", args.metric)

    if baseline is None:
        print(f"[基线] 无法从输出中提取指标 '{args.metric}'")
        print("  请确保脚本输出包含格式如：{metric}: {value}")
        print(f"  脚本输出最后 500 字符：\n{output[-500:]}")
        sys.exit(1)

    print(f"[基线] {args.metric} = {baseline}（耗时 {elapsed:.1f}s）")

    append_result(results_path, [
        0, datetime.now().isoformat(), "baseline",
        args.metric, baseline, baseline, "0", "baseline",
        git_run("rev-parse", "--short", "HEAD"), f"{elapsed:.1f}",
    ])

    if args.dry_run:
        print("\n[dry-run] 基线运行完成，退出。")
        return

    # 进入循环
    best_val = baseline
    best_round = 0
    no_improve_count = 0
    stop_reason = ""

    for round_num in range(1, args.max_rounds + 1):
        print(f"\n{'─' * 50}")
        print(f"[第 {round_num}/{args.max_rounds} 轮]")

        # 检查是否有代码变更（如果用户在外部修改了代码）
        if not git_has_changes():
            print("  ⚠ 没有代码变更，跳过此轮")
            print("  提示：自动迭代需要在循环外有代码修改机制（如 Claude agent）")
            print("  本脚本负责「运行 → 评估 → 保留/回滚」，代码修改由调用方负责。")
            print("  退出循环。")
            stop_reason = "无代码变更"
            break

        # 提交变更
        change_desc = git_run("diff", "--stat", "HEAD")
        short_desc = change_desc.splitlines()[-1] if change_desc else "unknown changes"
        commit_hash = git_commit(f"experiment {round_num}: {short_desc}")
        print(f"  提交：{commit_hash}")

        # 运行
        print(f"  运行中（超时 {args.timeout}s）...")
        output, retcode, elapsed = run_script(script_path, args.timeout)

        if retcode != 0:
            print(f"  💥 运行失败（返回码 {retcode}，耗时 {elapsed:.1f}s）")
            git_revert_last()
            append_result(results_path, [
                round_num, datetime.now().isoformat(), short_desc,
                args.metric, "CRASH", best_val, "N/A", "💥 crash", "-", f"{elapsed:.1f}",
            ])
            continue

        # 评估
        new_val = extract_metric_from_output(output, args.metric)
        if new_val is None and args.eval_script:
            new_val = run_eval_script(args.eval_script, ".", args.metric)

        if new_val is None:
            print(f"  ⚠ 无法提取指标 '{args.metric}'，回滚")
            git_revert_last()
            append_result(results_path, [
                round_num, datetime.now().isoformat(), short_desc,
                args.metric, "N/A", best_val, "N/A", "❌ revert (no metric)", "-", f"{elapsed:.1f}",
            ])
            continue

        delta = format_delta(new_val, best_val)
        improved = is_improvement(new_val, best_val, args.direction)

        if improved:
            print(f"  ✅ {args.metric} = {new_val}（{delta}）→ 保留")
            best_val = new_val
            best_round = round_num
            no_improve_count = 0
            decision = "✅ advance"
        else:
            print(f"  ❌ {args.metric} = {new_val}（{delta}）→ 回滚")
            git_revert_last()
            no_improve_count += 1
            decision = "❌ revert"

        append_result(results_path, [
            round_num, datetime.now().isoformat(), short_desc,
            args.metric, new_val, best_val, delta, decision, commit_hash, f"{elapsed:.1f}",
        ])

        # 检查停止条件
        if args.target is not None:
            if args.direction == "lower" and best_val <= args.target:
                stop_reason = f"达到目标阈值 {args.target}"
                print(f"\n🎯 {stop_reason}")
                break
            if args.direction == "higher" and best_val >= args.target:
                stop_reason = f"达到目标阈值 {args.target}"
                print(f"\n🎯 {stop_reason}")
                break

        if no_improve_count >= args.patience:
            stop_reason = f"连续 {args.patience} 轮无改善（收敛）"
            print(f"\n⏹ {stop_reason}")
            break
    else:
        stop_reason = f"达到最大轮次 {args.max_rounds}"

    # 生成摘要
    total_rounds = round_num if 'round_num' in dir() else 0
    generate_summary(
        results_path, summary_path, original_branch,
        args.metric, args.direction, baseline, best_val,
        best_round, total_rounds, stop_reason,
    )

    print(f"\n{'=' * 60}")
    print(f"  自动迭代完成")
    print(f"  基线：{args.metric} = {baseline}")
    print(f"  最佳：{args.metric} = {best_val}（第 {best_round} 轮）")
    if baseline is not None and best_val is not None:
        improvement = abs(best_val - baseline)
        print(f"  改善：{improvement:.6f}")
    print(f"  停止原因：{stop_reason}")
    print(f"  实验记录：{results_path}")
    print(f"  摘要报告：{summary_path}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
