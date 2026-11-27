# -*- coding: utf-8 -*-
"""GitHub Actions 每日更新入口：涨停池 -> 日K增量 -> 筹码JSON -> 页面 index.html。"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]
CST = timezone(timedelta(hours=8))
INTRA_DB = BASE / ".cache" / "pool_quant_intraday.db"


def is_holiday(day: str) -> bool:
    path = BASE / "holiday_list.json"
    if not path.exists():
        return False
    try:
        dates = set(json.loads(path.read_text(encoding="utf-8")).get("holidays", []))
        return day in dates
    except Exception:
        return False


def run(script: str, *args: str, allow_fail: bool = False, intraday: bool = False) -> int:
    cmd = [sys.executable, str(BASE / "scripts" / script), *args]
    print("$", " ".join(cmd), flush=True)
    env = dict(os.environ)
    env.update({
        "XGRS_BASE": str(BASE),
        "POOL_DB": str(BASE / "data" / "pool_quant.db"),
        "POOL_CSV": str(BASE / "data" / "股票池.csv"),
        "CHIP_JSON": str(BASE / "data" / "chip_6d_data.json"),
        "DATA_ROOT": str(BASE / "data"),
        "OUT_DIR": str(BASE / "reports"),
        "OUT_HTML": str(BASE / "index.html"),
        "OUT_HTML_6D": str(BASE / "reports" / "每日筹码_股票池_连续6日.html"),
    })
    if intraday:
        env["POOL_DB"] = str(INTRA_DB)
    rc = subprocess.run(cmd, cwd=str(BASE), env=env).returncode
    if rc != 0 and not allow_fail:
        raise SystemExit(f"{script} 失败，退出码 {rc}")
    return rc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="YYYY-MM-DD；缺省北京时间今天")
    ap.add_argument("--mode", choices=("close", "intraday"), default="close", help="close=收盘最终；intraday=盘中临时")
    args = ap.parse_args()
    day = args.date or datetime.now(CST).strftime("%Y-%m-%d")
    if is_holiday(day):
        print(f"{day} 是非交易日，跳过（holiday_list.json）")
        return 0
    (BASE / "reports").mkdir(parents=True, exist_ok=True)
    intraday = args.mode == "intraday"

    # 盘中/收盘都重新拉当日涨停池；盘中空结果不覆盖已有文件。
    fetch_args = ["--date", day.replace("-", "")]
    if intraday:
        fetch_args.append("--keep-existing-on-empty")
    run("fetch_limitup_cloud.py", *fetch_args, allow_fail=True)

    if intraday:
        run("update_pool_db.py", "--intraday")
        run("chip_6d_html.py", intraday=True)
        run("chip_5d_14cols.py", intraday=True)
    else:
        run("update_pool_db.py", allow_fail=True)
        run("chip_6d_html.py")
        run("chip_5d_14cols.py")

    html = BASE / "index.html"
    if not html.exists() or html.stat().st_size < 100_000:
        raise SystemExit("index.html 未生成或过小")
    print(f"OK mode={args.mode} index={html} size={html.stat().st_size/1024/1024:.2f}MB date={day}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
