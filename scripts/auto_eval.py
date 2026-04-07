#!/usr/bin/env python3
"""
自动评估器 —— 在无产品人工评估的情况下，自动对算法输出打分。

支持三种评估模式：
1. metric   - 从输出文件/日志提取数值指标
2. rule     - 基于 eval-criteria.md 中定义的规则自动打分
3. diff     - 与 ground truth 对比计算准确率

用法：
    # 从算法输出中提取指标
    python scripts/auto_eval.py metric --output-dir results/ --metric accuracy

    # 基于规则评估（检查格式、完整性等）
    python scripts/auto_eval.py rule --output-dir results/ --criteria eval-workspace/eval-criteria.md

    # 与标注数据对比
    python scripts/auto_eval.py diff --output-dir results/ --ground-truth data/labels.json --metric f1
"""

import argparse
import csv
import json
import re
import sys
from pathlib import Path


# ── 模式 1：指标提取（metric）────────────────────────────────────────────────

def eval_metric(output_dir: Path, metric_name: str) -> dict:
    """从输出目录中的文件提取指标值。扫描所有文本文件，取最后出现的值。"""
    last_value = None
    source_file = None

    patterns = [
        rf"{re.escape(metric_name)}\s*[:=]\s*([\d.eE+\-]+)",
        rf'"{re.escape(metric_name)}"\s*:\s*([\d.eE+\-]+)',
        rf"{re.escape(metric_name)}\s+([\d.eE+\-]+)",
    ]

    search_files = []
    if output_dir.is_file():
        search_files = [output_dir]
    else:
        for ext in ("*.log", "*.txt", "*.json", "*.csv", "*.out", "*.md"):
            search_files.extend(output_dir.glob(ext))
        search_files.extend(output_dir.glob("**/metrics*"))
        search_files.extend(output_dir.glob("**/result*"))

    for fpath in search_files:
        try:
            text = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for pattern in patterns:
            for match in re.finditer(pattern, text, re.IGNORECASE):
                try:
                    last_value = float(match.group(1))
                    source_file = str(fpath)
                except ValueError:
                    pass

    return {
        "mode": "metric",
        "metric": metric_name,
        "value": last_value,
        "source": source_file,
    }


# ── 模式 2：规则评估（rule）──────────────────────────────────────────────────

def load_criteria_rules(criteria_path: Path) -> list[dict]:
    """从 eval-criteria.md 提取评估维度及其评分标准。"""
    if not criteria_path.exists():
        return []
    text = criteria_path.read_text(encoding="utf-8")
    dimensions = []
    current_dim = None
    current_scores = {}

    for line in text.splitlines():
        dim_match = re.match(r"^###\s+(.+)", line)
        if dim_match:
            if current_dim and current_scores:
                dimensions.append({"name": current_dim, "scores": current_scores})
            name = dim_match.group(1).strip()
            if not re.match(r"维度\s*\d+", name) and "[" not in name:
                current_dim = name
                current_scores = {}
            else:
                current_dim = None
            continue
        score_match = re.match(r"\s*-\s*(\d+)分[：:]\s*(.+)", line)
        if score_match and current_dim:
            score = int(score_match.group(1))
            desc = score_match.group(2).strip()
            current_scores[score] = desc

    if current_dim and current_scores:
        dimensions.append({"name": current_dim, "scores": current_scores})

    return dimensions


def eval_rule(output_dir: Path, criteria_path: Path) -> dict:
    """基于规则的自动评估。检查输出的基本质量指标。"""
    dimensions = load_criteria_rules(criteria_path)

    # 收集输出文件内容
    output_files = []
    if output_dir.is_file():
        output_files = [output_dir]
    else:
        for ext in ("*.json", "*.csv", "*.txt", "*.md"):
            output_files.extend(output_dir.glob(ext))

    total_content = ""
    record_count = 0
    for fpath in output_files:
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
            total_content += content + "\n"
            # 尝试计算记录数
            try:
                data = json.loads(content)
                if isinstance(data, list):
                    record_count += len(data)
            except json.JSONDecodeError:
                record_count += len([l for l in content.splitlines() if l.strip()])
        except Exception:
            pass

    # 自动规则检查
    checks = {
        "非空检查": 1.0 if total_content.strip() else 0.0,
        "记录数量": record_count,
        "输出大小(bytes)": len(total_content.encode("utf-8")),
        "JSON格式有效": 0.0,
        "字段覆盖率": 0.0,
    }

    # JSON 有效性
    try:
        data = json.loads(total_content.strip())
        checks["JSON格式有效"] = 1.0
        if isinstance(data, list) and data:
            # 计算字段覆盖率（非空字段 / 总字段）
            all_keys = set()
            non_empty = set()
            for item in data:
                if isinstance(item, dict):
                    for k, v in item.items():
                        all_keys.add(k)
                        if v is not None and str(v).strip():
                            non_empty.add(k)
            checks["字段覆盖率"] = len(non_empty) / len(all_keys) if all_keys else 0.0
    except (json.JSONDecodeError, ValueError):
        pass

    # 综合得分
    score = 0.0
    if checks["非空检查"] > 0:
        score += 2.0
    if checks["记录数量"] > 0:
        score += 1.0
    if checks["JSON格式有效"] > 0:
        score += 1.0
    if checks["字段覆盖率"] > 0.8:
        score += 1.0
    elif checks["字段覆盖率"] > 0.5:
        score += 0.5

    return {
        "mode": "rule",
        "score": score,
        "max_score": 5.0,
        "checks": checks,
        "dimensions_found": len(dimensions),
        "output_files": len(output_files),
    }


# ── 模式 3：对比评估（diff）──────────────────────────────────────────────────

def eval_diff(output_dir: Path, ground_truth_path: Path, metric_name: str) -> dict:
    """与标注数据对比，计算准确率 / F1 / 完全匹配率。"""
    # 读取 ground truth
    gt_text = ground_truth_path.read_text(encoding="utf-8")
    try:
        gt_data = json.loads(gt_text)
    except json.JSONDecodeError:
        gt_data = [line.strip() for line in gt_text.splitlines() if line.strip()]

    # 读取预测输出
    pred_data = None
    if output_dir.is_file():
        pred_text = output_dir.read_text(encoding="utf-8")
    else:
        pred_files = list(output_dir.glob("*.json")) + list(output_dir.glob("output*"))
        if not pred_files:
            return {"mode": "diff", "error": "未找到预测输出文件", "metric": metric_name, "value": None}
        pred_text = pred_files[0].read_text(encoding="utf-8")

    try:
        pred_data = json.loads(pred_text)
    except json.JSONDecodeError:
        pred_data = [line.strip() for line in pred_text.splitlines() if line.strip()]

    # 转为列表
    if isinstance(gt_data, dict):
        gt_list = list(gt_data.values())
    elif isinstance(gt_data, list):
        gt_list = gt_data
    else:
        gt_list = [gt_data]

    if isinstance(pred_data, dict):
        pred_list = list(pred_data.values())
    elif isinstance(pred_data, list):
        pred_list = pred_data
    else:
        pred_list = [pred_data]

    n = min(len(gt_list), len(pred_list))
    if n == 0:
        return {"mode": "diff", "error": "数据为空", "metric": metric_name, "value": None}

    # 计算指标
    correct = 0
    tp, fp, fn = 0, 0, 0

    for i in range(n):
        gt_val = str(gt_list[i]).strip() if not isinstance(gt_list[i], dict) else json.dumps(gt_list[i], ensure_ascii=False)
        pred_val = str(pred_list[i]).strip() if not isinstance(pred_list[i], dict) else json.dumps(pred_list[i], ensure_ascii=False)

        if gt_val == pred_val:
            correct += 1
            tp += 1
        else:
            fp += 1
            fn += 1

    accuracy = correct / n
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0

    metrics = {
        "accuracy": accuracy,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "exact_match": accuracy,
    }

    target = metrics.get(metric_name.lower(), accuracy)

    return {
        "mode": "diff",
        "metric": metric_name,
        "value": round(target, 6),
        "total": n,
        "correct": correct,
        "all_metrics": {k: round(v, 6) for k, v in metrics.items()},
        "gt_count": len(gt_list),
        "pred_count": len(pred_list),
    }


# ── 主入口 ────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="自动评估器（无需产品人工评估）")
    subparsers = parser.add_subparsers(dest="mode", required=True, help="评估模式")

    # metric 模式
    p_metric = subparsers.add_parser("metric", help="从输出中提取数值指标")
    p_metric.add_argument("--output-dir", required=True, help="算法输出目录或文件")
    p_metric.add_argument("--metric", required=True, help="指标名称")

    # rule 模式
    p_rule = subparsers.add_parser("rule", help="基于规则的自动评估")
    p_rule.add_argument("--output-dir", required=True, help="算法输出目录或文件")
    p_rule.add_argument("--criteria", default="eval-workspace/eval-criteria.md", help="评估标准文件")

    # diff 模式
    p_diff = subparsers.add_parser("diff", help="与标注数据对比")
    p_diff.add_argument("--output-dir", required=True, help="算法输出目录或文件")
    p_diff.add_argument("--ground-truth", required=True, help="标注数据文件（JSON/文本）")
    p_diff.add_argument("--metric", default="accuracy", help="指标名称（accuracy/f1/precision/recall）")

    args = parser.parse_args()

    if args.mode == "metric":
        result = eval_metric(Path(args.output_dir), args.metric)
    elif args.mode == "rule":
        result = eval_rule(Path(args.output_dir), Path(args.criteria))
    elif args.mode == "diff":
        gt_path = Path(args.ground_truth)
        if not gt_path.exists():
            sys.exit(f"标注文件不存在：{gt_path}")
        result = eval_diff(Path(args.output_dir), gt_path, args.metric)

    # 输出结果（JSON 格式，方便 autoloop.py 解析）
    print(json.dumps(result, ensure_ascii=False, indent=2))

    # 同时输出关键指标行（方便 autoloop.py 的 extract_metric_from_output 提取）
    if "value" in result and result["value"] is not None:
        metric_name = result.get("metric", "score")
        print(f"\n{metric_name}: {result['value']}")
    elif "score" in result:
        print(f"\nscore: {result['score']}")


if __name__ == "__main__":
    main()
