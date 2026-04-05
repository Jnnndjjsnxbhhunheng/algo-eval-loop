#!/usr/bin/env python3
"""
初始化 eval-workspace/ 目录结构。
用法：python scripts/init_workspace.py [--version v1]
"""

import argparse
import os
from pathlib import Path


def init_workspace(base_dir: Path, version: str = "v1") -> None:
    dirs = [
        base_dir / "versions" / version / "raw_output",
        base_dir / "reports",
    ]
    for d in dirs:
        d.mkdir(parents=True, exist_ok=True)

    criteria_path = base_dir / "eval-criteria.md"
    if not criteria_path.exists():
        template_path = Path(__file__).parent.parent / "templates" / "eval-criteria-template.md"
        if template_path.exists():
            criteria_path.write_text(template_path.read_text(encoding="utf-8"), encoding="utf-8")
        else:
            criteria_path.write_text(
                "# 评估标准\n\n## 元信息\n- 当前版本：v1.0\n- 最后更新：\n- 生效起始版本：v1\n\n## 评估维度\n\n## 变更记录\n\n| 日期 | 变更内容 | 原因 | 生效版本 |\n|------|---------|------|--------|\n",
                encoding="utf-8",
            )

    changelog_path = base_dir / "versions" / "changelog.md"
    if not changelog_path.exists():
        changelog_path.write_text(
            "# 版本变更日志\n\n| 版本 | 日期 | 变更摘要 |\n|------|------|--------|\n",
            encoding="utf-8",
        )

    print(f"eval-workspace 初始化完成：{base_dir.resolve()}")
    print(f"  ├── eval-criteria.md")
    print(f"  ├── versions/")
    print(f"  │   ├── changelog.md")
    print(f"  │   └── {version}/")
    print(f"  │       └── raw_output/")
    print(f"  └── reports/")


def next_version(base_dir: Path) -> str:
    versions_dir = base_dir / "versions"
    if not versions_dir.exists():
        return "v1"
    existing = sorted(
        [d.name for d in versions_dir.iterdir() if d.is_dir() and d.name.startswith("v")]
    )
    if not existing:
        return "v1"
    last = existing[-1]
    try:
        num = int(last[1:])
        return f"v{num + 1}"
    except ValueError:
        return "v1"


def main() -> None:
    parser = argparse.ArgumentParser(description="初始化 eval-workspace 目录")
    parser.add_argument(
        "--workspace",
        default="eval-workspace",
        help="工作目录路径（默认：eval-workspace）",
    )
    parser.add_argument(
        "--version",
        default=None,
        help="初始版本号（默认：自动递增）",
    )
    args = parser.parse_args()

    base_dir = Path(args.workspace)
    version = args.version or next_version(base_dir)
    init_workspace(base_dir, version)


if __name__ == "__main__":
    main()
