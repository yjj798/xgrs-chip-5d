# -*- coding: utf-8 -*-
"""把 391 只股票的同花顺日 K 增量写入 data/pool_quant.db。

只追加数据库中尚不存在的交易日，不改写已有历史，保证与本地库历史一致。
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parents[1]
DB = Path(os.environ.get("POOL_DB", str(BASE / "data" / "pool_quant.db")))
KLINE_URL = "https://d.10jqka.com.cn/v6/line/hs_{code}/01/last.js"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
REFERER = "https://data.10jqka.com.cn/datacenterph/limitup/limtupInfo.html"


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def fetch_kline(code: str) -> list[dict]:
    headers = {"User-Agent": UA, "Referer": REFERER}
    last_err = None
    for attempt in range(4):
        try:
            r = requests.get(KLINE_URL.format(code=code), headers=headers, timeout=25)
            r.raise_for_status()
            text = r.text.strip()
            a, b = text.find("("), text.rfind(")")
            if a < 0 or b <= a:
                raise ValueError("K线返回格式异常")
            body = json.loads(text[a + 1:b])
            rows = []
            for line in str(body.get("data") or "").split(";"):
                cols = line.split(",")
                if len(cols) < 7:
                    continue
                d = cols[0].strip()
                if len(d) != 8 or not d.isdigit():
                    continue
                rows.append({
                    "date": d,
                    "open": _num(cols[1]), "high": _num(cols[2]),
                    "low": _num(cols[3]), "close": _num(cols[4]),
                    "volume": _num(cols[5]), "amount": _num(cols[6]),
                    "turnover": _num(cols[7]) if len(cols) > 7 else None,
                })
            if not rows:
                raise ValueError("K线无数据")
            return rows
        except Exception as exc:  # noqa: BLE001
            last_err = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{code} K线失败: {last_err}")


def main() -> int:
    if not DB.exists():
        raise SystemExit(f"缺少数据库 {DB}")
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    max_date = con.execute("select max(trade_date) from quote_clean").fetchone()[0]
    pool = {r["code"]: (r["name"] or "", r["board"] or "") for r in con.execute("select code,name,board from pool_quotes")}
    last = {}
    for r in con.execute(
        "select code,close,circ_mv_est,circ_mv,turnover_est from quote_clean "
        "where trade_date=(select max(trade_date) from quote_clean)"
    ):
        last[r["code"]] = dict(r)
    con.close()

    print(f"DB最新 {max_date}，股票 {len(pool)} 只，开始拉取日K")
    fetched = {}
    failed = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fetch_kline, code): code for code in pool}
        for i, f in enumerate(as_completed(futs), 1):
            code = futs[f]
            try:
                fetched[code] = f.result()
            except Exception as exc:  # noqa: BLE001
                failed.append(code)
                print(f"  [失败] {code}: {exc}")
            if i % 50 == 0:
                print(f"  进度 {i}/{len(pool)}")

    if failed and len(failed) > len(pool) * 0.15:
        raise SystemExit(f"K线失败过多 {len(failed)}/{len(pool)}，停止更新")

    con = sqlite3.connect(DB)
    inserted = 0
    skipped_stale = 0
    for code, rows in fetched.items():
        name, board = pool[code]
        base = last.get(code) or {}
        base_close = _num(base.get("close"))
        base_cmv = _num(base.get("circ_mv_est")) or _num(base.get("circ_mv"))
        float_shares = (base_cmv / base_close) if (base_cmv and base_close) else None
        is_st = 1 if re.search(r"ST", name, re.I) else 0
        new_rows = [x for x in rows if x["date"] > (max_date or "00000000")]
        if not new_rows:
            skipped_stale += 1
            continue
        for x in new_rows:
            if not all(x.get(k) and x[k] > 0 for k in ("open", "high", "low", "close")):
                continue
            vol = x.get("volume")
            vol_hands = (vol / 100.0) if vol else None
            turn = x.get("turnover")
            if (turn is None or turn <= 0) and vol_hands and float_shares:
                turn = (vol_hands * 100.0 / float_shares) * 100.0
            cmv = (x["close"] * float_shares) if float_shares else None
            con.execute(
                "insert or ignore into quote_clean "
                "(trade_date,code,name,board,is_st,is_bj,open,high,low,close,volume,amount,"
                "turnover_rate,circ_mv,data_source,turnover_est,circ_mv_est) "
                "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (x["date"], code, name, board, is_st, 0, x["open"], x["high"], x["low"], x["close"],
                 vol_hands, x.get("amount"), (turn / 100.0 if turn else None), cmv,
                 "10jqka_kline_cloud", turn, cmv),
            )
            inserted += 1
    con.commit()
    new_max = con.execute("select max(trade_date) from quote_clean").fetchone()[0]
    con.close()
    print(f"OK 新增 {inserted} 行，失败 {len(failed)} 只，库内最新 {new_max}，无新数据 {skipped_stale} 只")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
