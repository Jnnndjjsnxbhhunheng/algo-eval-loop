#!/usr/bin/env python3
"""
算法输出 → 产品评估 Excel 转换器。

支持输入格式：JSON / JSON Lines / CSV / Markdown 表格 / 纯文本（启发式解析）
用法：
    python scripts/convert.py <input_file> [--criteria eval-workspace/eval-criteria.md]
                              [--output eval-workspace/versions/v1/output.xlsx]
                              [--save-raw]
"""

import argparse
import csv
import json
import re
import shutil
import sys
from pathlib import Path

try:
    import openpyxl
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter
except ImportError:
    sys.exit("请先安装依赖：pip install openpyxl")

try:
    import pandas as pd
    HAS_PANDAS = True
except ImportError:
    HAS_PANDAS = False


# ── 格式检测 ──────────────────────────────────────────────────────────────────

def detect_format(text: str, path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return "csv"
    if suffix in (".jsonl", ".ndjson"):
        return "jsonl"
    if suffix == ".json":
        return "json"
    if suffix in (".md", ".markdown"):
        return "markdown"

    stripped = text.strip()
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            json.loads(stripped)
            return "json"
        except json.JSONDecodeError:
            pass
        try:
            json.loads(stripped.splitlines()[0])
            return "jsonl"
        except json.JSONDecodeError:
            pass
    if "|" in stripped and "---" in stripped:
        return "markdown"
    try:
        next(csv.reader(stripped.splitlines()))
        if "," in stripped.splitlines()[0] or "\t" in stripped.splitlines()[0]:
            return "csv"
    except Exception:
        pass
    return "text"


# ── 各格式解析 → list[dict] ───────────────────────────────────────────────────

def parse_json(text: str) -> list[dict]:
    data = json.loads(text)
    if isinstance(data, list):
        return [item if isinstance(item, dict) else {"value": item} for item in data]
    if isinstance(data, dict):
        # 判断是否是 {key: [records]} 形式
        for v in data.values():
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return v
        return [data]
    return [{"value": data}]


def parse_jsonl(text: str) -> list[dict]:
    records = []
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            records.append(obj if isinstance(obj, dict) else {"value": obj})
        except json.JSONDecodeError:
            pass
    return records


def parse_csv(text: str) -> list[dict]:
    dialect = "excel-tab" if "\t" in text.splitlines()[0] else "excel"
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    return list(reader)


def parse_markdown_table(text: str) -> list[dict]:
    lines = [l.strip() for l in text.splitlines() if "|" in l]
    if len(lines) < 2:
        return []
    header = [h.strip() for h in lines[0].strip("|").split("|")]
    records = []
    for line in lines[2:]:  # 跳过分隔行
        cells = [c.strip() for c in line.strip("|").split("|")]
        if len(cells) != len(header):
            continue
        records.append(dict(zip(header, cells)))
    return records


def parse_text(text: str) -> list[dict]:
    """启发式解析：每段落或每行作为一条记录。"""
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text) if p.strip()]
    if len(paragraphs) > 1:
        return [{"内容": p} for p in paragraphs]
    return [{"内容": line.strip()} for line in text.splitlines() if line.strip()]


def flatten(obj: dict, prefix: str = "") -> dict:
    """递归展平嵌套字典（一层）。"""
    out = {}
    for k, v in obj.items():
        key = f"{prefix}{k}" if prefix else k
        if isinstance(v, dict):
            out.update(flatten(v, prefix=f"{key}."))
        elif isinstance(v, list):
            out[key] = json.dumps(v, ensure_ascii=False)
        else:
            out[key] = v
    return out


def parse_input(text: str, path: Path) -> list[dict]:
    fmt = detect_format(text, path)
    print(f"检测到格式：{fmt}")
    if fmt == "json":
        records = parse_json(text)
    elif fmt == "jsonl":
        records = parse_jsonl(text)
    elif fmt == "csv":
        records = parse_csv(text)
    elif fmt == "markdown":
        records = parse_markdown_table(text)
    else:
        records = parse_text(text)
    # 展平嵌套字段
    return [flatten(r) for r in records]


# ── 评估标准读取 ───────────────────────────────────────────────────────────────

def load_criteria(criteria_path: Path) -> list[str]:
    """从 eval-criteria.md 提取维度名称列表。"""
    if not criteria_path.exists():
        return []
    text = criteria_path.read_text(encoding="utf-8")
    dimensions = []
    for line in text.splitlines():
        m = re.match(r"^###\s+(.+)", line)
        if m:
            name = m.group(1).strip()
            # 跳过"维度 N：[...]"这类模板占位
            if not re.match(r"维度\s*\d+", name) and "[" not in name:
                dimensions.append(name)
    return dimensions


# ── Excel 生成 ────────────────────────────────────────────────────────────────

EVAL_FILL = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")
HEADER_FONT = Font(bold=True)


def write_excel(records: list[dict], dimensions: list[str], output_path: Path) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "评估数据"

    data_cols = list(records[0].keys()) if records else []
    eval_cols = []
    for dim in dimensions:
        eval_cols.append(f"{dim}_评分")
        eval_cols.append(f"{dim}_备注")

    all_cols = data_cols + eval_cols

    # 第一行：表头
    for col_idx, col_name in enumerate(all_cols, start=1):
        cell = ws.cell(row=1, column=col_idx, value=col_name)
        cell.font = HEADER_FONT
        if col_name in eval_cols:
            cell.fill = EVAL_FILL

    # 第二行：说明行
    for col_idx, col_name in enumerate(all_cols, start=1):
        if col_name.endswith("_评分"):
            ws.cell(row=2, column=col_idx, value="1-5分")
        elif col_name.endswith("_备注"):
            ws.cell(row=2, column=col_idx, value="填写问题说明")

    # 数据行（从第三行起）
    for row_idx, record in enumerate(records, start=3):
        for col_idx, col_name in enumerate(all_cols, start=1):
            if col_name in data_cols:
                ws.cell(row=row_idx, column=col_idx, value=record.get(col_name, ""))

    # 样式：冻结首行、自适应列宽
    ws.freeze_panes = "A2"
    for col_idx, col_name in enumerate(all_cols, start=1):
        letter = get_column_letter(col_idx)
        max_len = max(len(col_name), 10)
        for row in ws.iter_rows(min_col=col_idx, max_col=col_idx, min_row=1, max_row=min(20, ws.max_row)):
            for cell in row:
                if cell.value:
                    max_len = max(max_len, min(len(str(cell.value)), 50))
        ws.column_dimensions[letter].width = max_len + 2

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    print(f"Excel 已保存：{output_path}")
    print(f"  数据列 ({len(data_cols)}): {', '.join(data_cols)}")
    print(f"  评估列 ({len(eval_cols)}): {', '.join(eval_cols) if eval_cols else '（无，请在 eval-criteria.md 中定义维度）'}")
    print(f"  共 {len(records)} 条数据")


# ── 版本目录管理 ───────────────────────────────────────────────────────────────

def resolve_output_path(workspace: Path) -> tuple[Path, str]:
    versions_dir = workspace / "versions"
    existing = sorted(
        [d.name for d in versions_dir.iterdir() if d.is_dir() and d.name.startswith("v")]
    ) if versions_dir.exists() else []
    if not existing:
        version = "v1"
    else:
        try:
            version = f"v{int(existing[-1][1:]) + 1}"
        except ValueError:
            version = "v1"
    return workspace / "versions" / version / "output.xlsx", version


# ── 主入口 ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="算法输出 → 产品评估 Excel")
    parser.add_argument("input", help="算法输出文件路径")
    parser.add_argument("--criteria", default="eval-workspace/eval-criteria.md", help="评估标准文件")
    parser.add_argument("--output", default=None, help="输出 Excel 路径（默认自动版本化）")
    parser.add_argument("--workspace", default="eval-workspace", help="工作目录")
    parser.add_argument("--save-raw", action="store_true", help="同时保存原始输入到 raw_output/")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        sys.exit(f"输入文件不存在：{input_path}")

    workspace = Path(args.workspace)
    criteria_path = Path(args.criteria)

    if args.output:
        output_path = Path(args.output)
        version = output_path.parent.name
    else:
        output_path, version = resolve_output_path(workspace)

    text = input_path.read_text(encoding="utf-8", errors="replace")
    records = parse_input(text, input_path)

    if not records:
        sys.exit("解析结果为空，请检查输入文件格式。")

    dimensions = load_criteria(criteria_path)
    if not dimensions:
        print(f"⚠ 未找到评估维度（{criteria_path}），将只生成数据列，请手动添加评估列。")

    if args.save_raw:
        raw_dir = workspace / "versions" / version / "raw_output"
        raw_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(input_path, raw_dir / input_path.name)
        print(f"原始文件已保存：{raw_dir / input_path.name}")

    write_excel(records, dimensions, output_path)
    print(f"\n请将 {output_path} 发送给产品，填写评分列和备注列后放回 {output_path.parent}/feedback.xlsx")


if __name__ == "__main__":
    main()
