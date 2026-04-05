#!/usr/bin/env python3
"""
产品反馈 Excel 分析器 → 生成 analysis.md。

用法：
    python scripts/analyze.py <feedback_xlsx> [--version v1] [--workspace eval-workspace]
"""

import argparse
import re
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

try:
    import openpyxl
except ImportError:
    sys.exit("请先安装依赖：pip install openpyxl")

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False


# ── Excel 读取 ────────────────────────────────────────────────────────────────

def read_feedback(path: Path) -> tuple[list[str], list[dict]]:
    """返回 (列名列表, 记录列表)，跳过第二行（说明行）。"""
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    ws = wb.active
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return [], []
    headers = [str(h) if h is not None else f"col_{i}" for i, h in enumerate(rows[0])]
    # 第二行如果是说明行（含"分"、"填写"等关键词）则跳过
    data_start = 1
    if len(rows) > 1:
        sample = " ".join(str(v) for v in rows[1] if v is not None)
        if re.search(r"(分|填写|说明|评分|备注)", sample):
            data_start = 2
    records = []
    for row in rows[data_start:]:
        rec = dict(zip(headers, row))
        # 跳过全空行
        if all(v is None or str(v).strip() == "" for v in rec.values()):
            continue
        records.append(rec)
    wb.close()
    return headers, records


# ── 列分类 ────────────────────────────────────────────────────────────────────

def classify_columns(headers: list[str]) -> tuple[list[str], list[str], list[str]]:
    """返回 (数据列, 评分列, 备注列)。"""
    score_cols = [h for h in headers if h.endswith("_评分")]
    note_cols = [h for h in headers if h.endswith("_备注")]
    data_cols = [h for h in headers if h not in score_cols and h not in note_cols]
    return data_cols, score_cols, note_cols


# ── 统计分析 ───────────────────────────────────────────────────────────────────

def safe_float(v) -> float | None:
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def dimension_stats(records: list[dict], score_cols: list[str]) -> list[dict]:
    stats = []
    for col in score_cols:
        dim = col[: -len("_评分")]
        values = [safe_float(r.get(col)) for r in records]
        values = [v for v in values if v is not None]
        if not values:
            stats.append({"维度": dim, "均分": "N/A", "低分占比": "N/A", "bad_case数": "N/A"})
            continue
        avg = sum(values) / len(values)
        low_threshold = 3.0
        low_count = sum(1 for v in values if v < low_threshold)
        stats.append({
            "维度": dim,
            "均分": round(avg, 2),
            "最低": min(values),
            "最高": max(values),
            "低分占比": f"{low_count / len(values) * 100:.1f}%",
            "bad_case数": low_count,
        })
    return stats


# ── Bad Case Pattern 分析 ─────────────────────────────────────────────────────

def extract_patterns(records: list[dict], score_cols: list[str], note_cols: list[str], data_cols: list[str]) -> list[str]:
    """
    从 bad cases 的备注列中聚类找共性 pattern。
    策略：按词频统计高频关键词，并关联到维度。
    """
    bad_cases = defaultdict(list)  # dim → [备注文本]

    for rec in records:
        for score_col in score_cols:
            v = safe_float(rec.get(score_col))
            if v is not None and v < 3.0:
                dim = score_col[: -len("_评分")]
                note_col = f"{dim}_备注"
                note = str(rec.get(note_col, "") or "")
                if note.strip():
                    bad_cases[dim].append(note)

    patterns = []
    for dim, notes in bad_cases.items():
        if not notes:
            continue
        # 简单关键词频率统计
        word_freq: dict[str, int] = defaultdict(int)
        for note in notes:
            for word in re.findall(r"[\u4e00-\u9fff]{2,}|[a-zA-Z]{3,}", note):
                word_freq[word] += 1
        # 过滤低频词
        top_words = sorted(word_freq.items(), key=lambda x: -x[1])[:5]
        top_words = [(w, c) for w, c in top_words if c >= 2]
        total_bad = len(notes)
        if top_words:
            keywords = "、".join(f"{w}({c}次)" for w, c in top_words)
            patterns.append(f"**{dim}**：{total_bad} 条 bad case，高频问题关键词：{keywords}")
        else:
            patterns.append(f"**{dim}**：{total_bad} 条 bad case，问题较分散，无明显共性")

    return patterns


# ── 报告生成 ───────────────────────────────────────────────────────────────────

def generate_analysis(
    records: list[dict],
    headers: list[str],
    data_cols: list[str],
    score_cols: list[str],
    note_cols: list[str],
    version: str,
) -> str:
    stats = dimension_stats(records, score_cols)
    patterns = extract_patterns(records, score_cols, note_cols, data_cols)

    total = len(records)
    overall_scores = []
    for s in stats:
        avg = s.get("均分")
        if isinstance(avg, float):
            overall_scores.append(avg)
    overall_avg = round(sum(overall_scores) / len(overall_scores), 2) if overall_scores else "N/A"

    lines = [
        f"# {version} 反馈分析报告",
        f"\n生成日期：{date.today()}  ",
        f"数据量：{total} 条  ",
        f"综合均分：{overall_avg}（各维度均分平均）",
        "\n## 各维度统计",
        "",
        "| 维度 | 均分 | 最低 | 最高 | 低分占比 | bad case 数 |",
        "|------|------|------|------|---------|------------|",
    ]
    for s in stats:
        lines.append(
            f"| {s['维度']} | {s['均分']} | {s.get('最低', '-')} | {s.get('最高', '-')} | {s['低分占比']} | {s['bad_case数']} |"
        )

    lines += [
        "\n## Bad Case Pattern 分析",
        "",
    ]
    if patterns:
        for p in patterns:
            lines.append(f"- {p}")
    else:
        lines.append("- 暂无足够 bad case 数据（低分 case < 2）")

    lines += [
        "\n## 重点关注维度",
        "",
    ]
    # 按 bad case 数降序列出需要重点关注的维度
    sorted_stats = sorted(
        [s for s in stats if isinstance(s["bad_case数"], int)],
        key=lambda x: -x["bad_case数"],
    )
    if sorted_stats:
        for s in sorted_stats[:3]:
            lines.append(f"- **{s['维度']}**：{s['bad_case数']} 条 bad case（低分占比 {s['低分占比']}）")
    else:
        lines.append("- 暂无数据")

    lines += [
        "\n## 下一步建议",
        "",
        "- [ ] 针对重点维度的 bad case 进行根因分析",
        "- [ ] 生成迭代改进方案（见 plan.md）",
        "- [ ] 重点关注低分占比 > 30% 的维度",
    ]

    return "\n".join(lines)


# ── 主入口 ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="产品反馈 Excel 分析 → analysis.md")
    parser.add_argument("feedback", help="产品填写的 feedback.xlsx 路径")
    parser.add_argument("--version", default=None, help="版本号（默认从路径推断）")
    parser.add_argument("--workspace", default="eval-workspace", help="工作目录")
    parser.add_argument("--output", default=None, help="analysis.md 输出路径（默认放入版本目录）")
    args = parser.parse_args()

    feedback_path = Path(args.feedback)
    if not feedback_path.exists():
        sys.exit(f"文件不存在：{feedback_path}")

    # 推断版本号
    version = args.version
    if not version:
        parts = feedback_path.parts
        for p in parts:
            if re.match(r"v\d+", p):
                version = p
                break
        if not version:
            version = "v?"

    headers, records = read_feedback(feedback_path)
    if not records:
        sys.exit("反馈文件中没有数据。")

    data_cols, score_cols, note_cols = classify_columns(headers)
    if not score_cols:
        print("⚠ 未检测到评分列（格式：[维度名]_评分），请确认列名格式是否正确。")
        print(f"  当前列名：{headers}")
        sys.exit(1)

    print(f"版本：{version}，数据量：{len(records)} 条")
    print(f"评分维度：{[c[:-3] for c in score_cols]}")

    report = generate_analysis(records, headers, data_cols, score_cols, note_cols, version)

    # 确定输出路径
    if args.output:
        output_path = Path(args.output)
    else:
        workspace = Path(args.workspace)
        output_path = workspace / "versions" / version / "analysis.md"

    # 同时把 feedback 复制到版本目录（如果不在那里）
    target_feedback = output_path.parent / "feedback.xlsx"
    if feedback_path.resolve() != target_feedback.resolve():
        import shutil
        target_feedback.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(feedback_path, target_feedback)
        print(f"feedback.xlsx 已复制到：{target_feedback}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(report, encoding="utf-8")
    print(f"分析报告已生成：{output_path}")
    print("\n--- 摘要 ---")
    # 打印前 30 行作为预览
    for line in report.splitlines()[:30]:
        print(line)


if __name__ == "__main__":
    main()
