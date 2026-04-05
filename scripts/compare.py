#!/usr/bin/env python3
"""
版本对比工具：对比两个版本的 feedback.xlsx，输出 comparison_vX_vY.md。

用法：
    python scripts/compare.py <vX_feedback.xlsx> <vY_feedback.xlsx>
                              [--vx v1] [--vy v2]
                              [--workspace eval-workspace]
                              [--key-col <数据唯一标识列名>]
"""

import argparse
import re
import sys
from datetime import date
from pathlib import Path

try:
    import openpyxl
except ImportError:
    sys.exit("请先安装依赖：pip install openpyxl")


# ── 复用 analyze.py 中的读取逻辑 ──────────────────────────────────────────────

def safe_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def read_feedback(path: Path) -> tuple[list[str], list[dict]]:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return [], []
    headers = [str(h) if h is not None else f"col_{i}" for i, h in enumerate(rows[0])]
    data_start = 1
    if len(rows) > 1:
        sample = " ".join(str(v) for v in rows[1] if v is not None)
        if re.search(r"(分|填写|说明|评分|备注)", sample):
            data_start = 2
    records = []
    for row in rows[data_start:]:
        rec = dict(zip(headers, row))
        if all(v is None or str(v).strip() == "" for v in rec.values()):
            continue
        records.append(rec)
    wb.close()
    return headers, records


def get_score_cols(headers: list[str]) -> list[str]:
    return [h for h in headers if h.endswith("_评分")]


# ── 维度级对比 ────────────────────────────────────────────────────────────────

def dim_avg(records: list[dict], col: str) -> float | None:
    vals = [safe_float(r.get(col)) for r in records]
    vals = [v for v in vals if v is not None]
    return round(sum(vals) / len(vals), 2) if vals else None


def compare_dimensions(
    records_x: list[dict], headers_x: list[str],
    records_y: list[dict], headers_y: list[str],
    vx: str, vy: str,
) -> tuple[list[dict], list[str], list[str]]:
    """
    返回 (维度对比列表, 改善维度, 退化维度)
    维度对比行：{维度, vx均分, vy均分, 变化, 变化方向}
    """
    score_cols_x = set(get_score_cols(headers_x))
    score_cols_y = set(get_score_cols(headers_y))
    all_score_cols = score_cols_x | score_cols_y

    rows = []
    improved = []
    degraded = []

    for col in sorted(all_score_cols):
        dim = col[: -len("_评分")]
        avg_x = dim_avg(records_x, col) if col in score_cols_x else None
        avg_y = dim_avg(records_y, col) if col in score_cols_y else None

        if avg_x is None and avg_y is None:
            continue

        if avg_x is not None and avg_y is not None:
            delta = round(avg_y - avg_x, 2)
            direction = "↑ 改善" if delta > 0.1 else ("↓ 退化" if delta < -0.1 else "→ 持平")
            if delta > 0.1:
                improved.append(dim)
            elif delta < -0.1:
                degraded.append(dim)
        else:
            delta = "N/A"
            direction = "新增" if avg_x is None else "已删除"

        rows.append({
            "维度": dim,
            f"{vx}均分": avg_x if avg_x is not None else "-",
            f"{vy}均分": avg_y if avg_y is not None else "-",
            "变化": delta,
            "方向": direction,
        })

    return rows, improved, degraded


# ── 逐条对比（基于 key 列）────────────────────────────────────────────────────

def compare_cases(
    records_x: list[dict], records_y: list[dict],
    score_cols: list[str], key_col: str | None,
) -> tuple[list[dict], list[dict]]:
    """
    返回 (改善 cases, 退化 cases)。
    如果有 key_col 则精确匹配，否则按顺序匹配。
    """
    if key_col:
        map_x = {str(r.get(key_col, "")): r for r in records_x}
        map_y = {str(r.get(key_col, "")): r for r in records_y}
        common_keys = set(map_x) & set(map_y)
        pairs = [(map_x[k], map_y[k], k) for k in common_keys]
    else:
        n = min(len(records_x), len(records_y))
        pairs = [(records_x[i], records_y[i], str(i + 1)) for i in range(n)]

    improved_cases = []
    degraded_cases = []

    for rx, ry, key in pairs:
        for col in score_cols:
            sx = safe_float(rx.get(col))
            sy = safe_float(ry.get(col))
            if sx is None or sy is None:
                continue
            delta = sy - sx
            dim = col[: -len("_评分")]
            entry = {"key": key, "维度": dim, "旧分": sx, "新分": sy, "变化": round(delta, 1)}
            if delta >= 1.0:
                improved_cases.append(entry)
            elif delta <= -1.0:
                degraded_cases.append(entry)

    improved_cases.sort(key=lambda x: -x["变化"])
    degraded_cases.sort(key=lambda x: x["变化"])
    return improved_cases, degraded_cases


# ── 报告生成 ───────────────────────────────────────────────────────────────────

def generate_report(
    dim_rows: list[dict], improved_dims: list[str], degraded_dims: list[str],
    improved_cases: list[dict], degraded_cases: list[dict],
    vx: str, vy: str,
    total_x: int, total_y: int,
) -> str:
    lines = [
        f"# 版本对比报告：{vx} vs {vy}",
        f"\n生成日期：{date.today()}  ",
        f"{vx} 数据量：{total_x} 条  ",
        f"{vy} 数据量：{total_y} 条",
        "\n## 总结",
        "",
    ]

    if improved_dims:
        lines.append(f"**改善维度**（+0.1分以上）：{'、'.join(improved_dims)}")
    else:
        lines.append("**改善维度**：无明显改善")

    if degraded_dims:
        lines.append(f"**退化维度**（-0.1分以上）：{'、'.join(degraded_dims)}")
    else:
        lines.append("**退化维度**：无退化")

    lines += [
        "\n## 各维度得分对比",
        "",
        f"| 维度 | {vx}均分 | {vy}均分 | 变化 | 方向 |",
        "|------|---------|---------|------|------|",
    ]
    for row in dim_rows:
        lines.append(
            f"| {row['维度']} | {row[f'{vx}均分']} | {row[f'{vy}均分']} | {row['变化']} | {row['方向']} |"
        )

    lines += [
        "\n## 改善 Cases（分数提升 ≥ 1）",
        "",
    ]
    if improved_cases:
        lines.append(f"共 {len(improved_cases)} 条")
        lines.append("")
        lines.append("| # | 维度 | 旧分 | 新分 | 变化 |")
        lines.append("|---|------|------|------|------|")
        for c in improved_cases[:20]:
            lines.append(f"| {c['key']} | {c['维度']} | {c['旧分']} | {c['新分']} | +{c['变化']} |")
        if len(improved_cases) > 20:
            lines.append(f"（仅展示前 20 条，共 {len(improved_cases)} 条）")
    else:
        lines.append("无")

    lines += [
        "\n## 退化 Cases（分数下降 ≥ 1）",
        "",
    ]
    if degraded_cases:
        lines.append(f"共 {len(degraded_cases)} 条  ⚠ 需重点关注")
        lines.append("")
        lines.append("| # | 维度 | 旧分 | 新分 | 变化 |")
        lines.append("|---|------|------|------|------|")
        for c in degraded_cases[:20]:
            lines.append(f"| {c['key']} | {c['维度']} | {c['旧分']} | {c['新分']} | {c['变化']} |")
        if len(degraded_cases) > 20:
            lines.append(f"（仅展示前 20 条，共 {len(degraded_cases)} 条）")
    else:
        lines.append("无")

    lines += [
        "\n## 行动建议",
        "",
    ]
    if degraded_dims:
        lines.append(f"- ⚠ 重点排查退化维度：{'、'.join(degraded_dims)}，确认是否为代码改动引入的回归")
    if improved_dims:
        lines.append(f"- 确认改善维度（{'、'.join(improved_dims)}）的改善是否稳定，归纳可复用的优化经验")
    lines.append("- 如退化 case 集中在某类输入，回溯对应代码逻辑")

    return "\n".join(lines)


# ── 主入口 ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="两版本 feedback.xlsx 对比 → comparison_vX_vY.md")
    parser.add_argument("feedback_x", help="旧版本 feedback.xlsx")
    parser.add_argument("feedback_y", help="新版本 feedback.xlsx")
    parser.add_argument("--vx", default=None, help="旧版本号（默认从路径推断）")
    parser.add_argument("--vy", default=None, help="新版本号（默认从路径推断）")
    parser.add_argument("--workspace", default="eval-workspace", help="工作目录")
    parser.add_argument("--key-col", default=None, help="用于逐条匹配的唯一标识列名")
    parser.add_argument("--output", default=None, help="输出报告路径")
    args = parser.parse_args()

    def infer_version(path_str: str) -> str:
        for part in Path(path_str).parts:
            if re.match(r"v\d+", part):
                return part
        return path_str

    vx = args.vx or infer_version(args.feedback_x)
    vy = args.vy or infer_version(args.feedback_y)

    path_x = Path(args.feedback_x)
    path_y = Path(args.feedback_y)
    for p in (path_x, path_y):
        if not p.exists():
            sys.exit(f"文件不存在：{p}")

    headers_x, records_x = read_feedback(path_x)
    headers_y, records_y = read_feedback(path_y)

    if not records_x or not records_y:
        sys.exit("其中一个反馈文件没有数据。")

    print(f"对比：{vx}（{len(records_x)} 条）vs {vy}（{len(records_y)} 条）")

    score_cols_x = get_score_cols(headers_x)
    score_cols_y = get_score_cols(headers_y)
    common_score_cols = list(set(score_cols_x) & set(score_cols_y))

    dim_rows, improved_dims, degraded_dims = compare_dimensions(
        records_x, headers_x, records_y, headers_y, vx, vy
    )
    improved_cases, degraded_cases = compare_cases(
        records_x, records_y, common_score_cols, args.key_col
    )

    report = generate_report(
        dim_rows, improved_dims, degraded_dims,
        improved_cases, degraded_cases,
        vx, vy, len(records_x), len(records_y),
    )

    if args.output:
        output_path = Path(args.output)
    else:
        workspace = Path(args.workspace)
        output_path = workspace / "reports" / f"comparison_{vx}_{vy}.md"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    print(f"对比报告已生成：{output_path}")
    print("\n--- 摘要 ---")
    for line in report.splitlines()[:20]:
        print(line)


if __name__ == "__main__":
    main()
