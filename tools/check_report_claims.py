"""报告取证核对：把 `docs/demo功能对齐与改造报告.md` 里的**结论**拿代码核对一遍。

用法::

    .\\.venv\\Scripts\\python.exe -X utf8 tools\\check_report_claims.py [--report docs/...md]

为什么需要它：这份报告里大量结论写成"某文件（N 例）"、"只剩 X 条既有违规"、
"某端点/某字段"这种**可核对**的形式。后续几轮一直在加用例，不核对就会出现
"报告说 11 例、实际 15 例"这类漂移——报告是给人当作事实看的，漂了比没有更糟。

检查三件事（都能机器判）：
1. 报告里提到的每个文件路径**存在**；
2. "``tests/xxx.py``（N 例）"里的 N 与 `pytest --collect-only` 的实际条数**一致**；
3. 报告声明的既有 ruff 违规条数与实际一致。

不核对"语义是否正确"（那要人读），只把**能被机器证伪的数字与路径**挑出来。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = REPO_ROOT / ".venv" / "Scripts" / "python.exe"
DEFAULT_REPORT = REPO_ROOT / "docs" / "demo功能对齐与改造报告.md"

# 报告里的路径引用：带反引号的 tests/ tools/ docs/ apps/ packages/
PATH_PATTERN = re.compile(
    r"`((?:tests|tools|docs|apps|packages|deploy)/[^`\s]+?\.(?:py|md|txt|json))`"
)
# “`tests/xxx.py`（N 例：……）”——**带冒号的才是"这个文件总共多少例"**。
# 报告里还有“（13 例）+ 界面 8 例”这种写法，那是"这个功能在该文件里 8 例"，
# 不是文件总数，机器判不了，所以不在这里核对（宁可不查，也不误报）。
COUNT_PATTERN = re.compile(r"`(tests/[^`\s]+\.py)`\**\s*（\**\s*(\d+)\s*例\s*[:：]")
# 全量单测总数：“**444 passed, 236 subtests**”
TOTAL_PATTERN = re.compile(r"(\d[\d,]*)\s*passed,\s*(\d+)\s*subtests")
# 既有违规条数：“只剩 **5 条既有**违规”
RUFF_PATTERN = re.compile(r"只剩\s*\**\s*(\d+)\s*条\**\s*既有")


def collect_counts() -> dict[str, int]:
    """每个测试文件的用例条数（用 pytest 自己报的，不猜）。"""
    result = subprocess.run(
        [str(VENV_PYTHON), "-m", "pytest", "tests", "--collect-only", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    counts: dict[str, int] = {}
    for line in result.stdout.splitlines():
        if not line.startswith("tests/"):
            continue
        path = line.split("::")[0]
        counts[path] = counts.get(path, 0) + 1
    return counts


def count_pass() -> tuple[int, int]:
    """跑一遍全量单测，拿实际 passed / subtests（报告里的两个总数要能对上）。"""
    result = subprocess.run(
        [str(VENV_PYTHON), "-m", "pytest", "tests", "-q"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
        env={
            **os.environ,
            "QT_QPA_PLATFORM": "offscreen",
            "QT_QPA_FONTDIR": r"C:\Windows\Fonts",
        },
    )
    match = TOTAL_PATTERN.search(result.stdout.replace("\n", " "))
    if not match:
        return (0, 0)
    return (int(match.group(1).replace(",", "")), int(match.group(2)))


def ruff_count() -> int:
    result = subprocess.run(
        [str(VENV_PYTHON), "-m", "ruff", "check", "apps", "packages", "tests", "tools",
         "--output-format", "json"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    try:
        return len(json.loads(result.stdout or "[]"))
    except json.JSONDecodeError:
        return -1


def main() -> int:
    parser = argparse.ArgumentParser(description="核对报告里的可机检结论")
    parser.add_argument("--report", default=str(DEFAULT_REPORT))
    parser.add_argument("--skip-tests", action="store_true", help="跳过全量单测复跑")
    args = parser.parse_args()

    report = Path(args.report)
    text = report.read_text(encoding="utf-8")
    problems: list[str] = []

    print(f"核对对象：{report}")
    print("\n[1] 报告里提到的路径是否存在")
    paths = sorted(set(PATH_PATTERN.findall(text)))
    missing = [p for p in paths if not (REPO_ROOT / p).exists()]
    print(f"  引用 {len(paths)} 个路径，缺失 {len(missing)} 个")
    for path in missing:
        print(f"  [FAIL] 报告引用了不存在的路径：{path}")
        problems.append(f"缺失路径 {path}")

    print("\n[2] “某测试文件（N 例）”与实际条数")
    counts = collect_counts()
    claims = COUNT_PATTERN.findall(text)
    for path, claimed in claims:
        actual = counts.get(path)
        if actual is None:
            print(f"  [FAIL] {path}：报告说 {claimed} 例，但收集不到该文件")
            problems.append(f"{path} 收集不到")
            continue
        if int(claimed) != actual:
            print(f"  [FAIL] {path}：报告说 {claimed} 例，实际 {actual} 例")
            problems.append(f"{path} 说 {claimed} 实际 {actual}")
    print(f"  核对了 {len(claims)} 处声明，"
          f"不符 {sum(1 for p, c in claims if counts.get(p) != int(c))} 处")

    print("\n[3] 既有 ruff 违规条数")
    actual_ruff = ruff_count()
    claimed_ruff = [int(value) for value in RUFF_PATTERN.findall(text)]
    print(f"  报告声明过 {claimed_ruff}，实际 {actual_ruff} 条")
    if claimed_ruff and claimed_ruff[-1] != actual_ruff:
        print(f"  [WARN] 最后一次声明 {claimed_ruff[-1]} 条与实测 {actual_ruff} 条不一致")
        problems.append(f"ruff 声明 {claimed_ruff[-1]} 实际 {actual_ruff}")

    if not args.skip_tests:
        print("\n[4] 全量单测总数（报告里出现过的 passed/subtests）")
        claimed_totals = TOTAL_PATTERN.findall(text.replace("\n", " "))
        passed, subtests = count_pass()
        print(f"  实测 {passed} passed / {subtests} subtests")
        print(f"  报告里出现过的总数：{claimed_totals}")
        if not any(int(p.replace(",", "")) == passed for p, _s in claimed_totals):
            print("  [WARN] 报告里的 passed 总数都不是本轮实测值（历史记录可以保留，"
                  "但最新一条必须对得上）")
            problems.append("passed 总数与实测不一致")

    print()
    if problems:
        print(f"发现 {len(problems)} 处需要修：")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("报告里的可机检结论与代码一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
