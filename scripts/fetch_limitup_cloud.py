# -*- coding: utf-8 -*-
"""云端涨停池取数：只取同花顺 limit_up_pool，落 data/<date>/01_stocks_raw_<date>.json。

页面概念柱只依赖 code / is_limit_up / all_tags，故不需要本地库与 F10。
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

CST = timezone(timedelta(hours=8))
BASE = Path(__file__).resolve().parents[1]
OUT_ROOT = BASE / "data"
API = "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
REFERER = "https://data.10jqka.com.cn/datacenterph/limitup/limtupInfo.html"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
FIELDS = ("199112,10,9001,330323,330324,330325,9002,330329,133971,133970,1968584,3475914,9003,9004,"
          "330343,330344,330345,330346,330347,330348,3541450,3551450")


def today() -> str:
    return datetime.now(CST).strftime("%Y%m%d")


def fetch_page(date: str, page: int, limit: int = 200) -> dict:
    params = {"page": page, "limit": limit, "field": FIELDS, "filter": "HS,GEM2STAR", "date": date}
    headers = {"User-Agent": UA, "Referer": REFERER}
    for attempt in range(4):
        try:
            r = requests.get(API, params=params, headers=headers, timeout=25)
            r.raise_for_status()
            body = r.json()
            if body.get("status_code") == 0:
                return body
            raise RuntimeError(body.get("status_msg") or str(body)[:120])
        except Exception:
            if attempt == 3:
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError("unreachable")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="YYYYMMDD；缺省北京时间今天")
    args = ap.parse_args()
    date = (args.date or today()).replace("-", "")
    if len(date) != 8 or not date.isdigit():
        raise SystemExit("日期必须是 YYYYMMDD")

    rows, page, total = [], 1, None
    while True:
        body = fetch_page(date, page)
        data = body.get("data") or {}
        info = data.get("info") or []
        total = (data.get("page") or {}).get("total", len(info))
        rows.extend(info)
        if len(info) < 200 or len(rows) >= int(total or 0) or page >= 20:
            break
        page += 1
        time.sleep(0.35)

    stocks = []
    for r in rows:
        code = str(r.get("code") or "").strip()
        if not code:
            continue
        reason = str(r.get("reason_type_raw") or "").strip()
        tags = [t.strip() for t in reason.split("+") if t.strip()]
        stocks.append({
            "code": code,
            "name": str(r.get("name") or "").strip(),
            "high_days_raw": r.get("high_days_raw") or r.get("high_days") or "",
            "reason_type_raw": reason,
            "all_tags": tags,
            "is_limit_up": True,
            "is_zhaban": False,
            "is_limit_down": False,
            "latest": r.get("latest"),
            "change_rate": r.get("change_rate"),
            "turnover_rate": r.get("turnover_rate"),
            "currency_value": r.get("currency_value"),
        })

    out = OUT_ROOT / f"{date[:4]}-{date[4:6]}-{date[6:]}" / f"01_stocks_raw_{date[:4]}-{date[4:6]}-{date[6:]}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "data_date": f"{date[:4]}-{date[4:6]}-{date[6:]}",
        "producer": "fetch_limitup_cloud.py",
        "adj": "none",
        "data_source": "ths_dataapi",
        "run_status": "complete",
        "counts": {"limit_up": len(stocks), "zhaban": 0, "limit_down": 0},
        "stocks": stocks,
    }
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"OK limitup date={date} rows={len(stocks)} total={total} file={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
