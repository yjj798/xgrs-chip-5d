# -*- coding: utf-8 -*-
"""把 391 只股票的同花顺日 K 增量写入 data/pool_quant.db。

只追加数据库中尚不存在的交易日，不改写已有历史，保证与本地库历史一致。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = Path(__file__).resolve().parents[1]
DB = Path(os.environ.get("POOL_DB", str(BASE / "data" / "pool_quant.db")))
INTRA_DB = Path(os.environ.get("INTRA_DB", str(BASE / ".cache" / "pool_quant_intraday.db")))
CST = timezone(timedelta(hours=8))
TODAY = datetime.now(CST).strftime("%Y%m%d")
KLINE_URL = "https://d.10jqka.com.cn/v6/line/hs_{code}/01/last.js"
TENCENT_URL = "https://qt.gtimg.cn/q={q}"
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


def fetch_tencent_quotes(codes: list[str]) -> dict[str, dict]:
    """腾讯盘后快照，作为当日 OHLCV 的最终值；同花顺 last.js 偶发半日快照。"""
    out: dict[str, dict] = {}
    for start in range(0, len(codes), 60):
        batch = codes[start:start + 60]
        q = ",".join(("sh" if c.startswith("6") else "sz") + c for c in batch)
        try:
            r = requests.get(TENCENT_URL.format(q=q), headers={"User-Agent": UA}, timeout=20)
            text = r.content.decode("gbk", errors="ignore")
        except Exception as exc:  # noqa: BLE001
            print(f"  [腾讯行情失败] {exc}")
            continue
        for line in text.split(";"):
            if '="' not in line:
                continue
            body = line.split('="', 1)[1].rstrip('"')
            f = body.split("~")
            if len(f) < 39:
                continue
            code = f[2].strip()
            ts = f[30].strip()
            if len(ts) != 14 or not ts.isdigit():
                continue
            d = ts[:8]
            close, open_, high, low = _num(f[3]), _num(f[5]), _num(f[33]), _num(f[34])
            vol, amt_wan, turn = _num(f[6]), _num(f[37]), _num(f[38])
            if not all(x and x > 0 for x in (close, open_, high, low, vol)):
                continue
            out[code] = {
                "date": d, "open": open_, "high": high, "low": low, "close": close,
                "volume": vol, "amount": (amt_wan * 10000.0 if amt_wan else None),
                "turnover": turn, "source": "tencent_quote",
            }
        time.sleep(0.2)
    return out


def _float_shares(base: dict) -> float | None:
    base_close = _num(base.get("close"))
    base_cmv = _num(base.get("circ_mv_est")) or _num(base.get("circ_mv"))
    return (base_cmv / base_close) if (base_cmv and base_close) else None


def _insert_quote(con, code: str, name: str, board: str, x: dict, float_shares: float | None) -> bool:
    if not all(x.get(k) and x[k] > 0 for k in ("open", "high", "low", "close")):
        return False
    vol = x.get("volume")
    vol_hands = vol if x.get("source") == "tencent_quote" else ((vol / 100.0) if vol else None)
    turn = x.get("turnover")
    if (turn is None or turn <= 0) and vol_hands and float_shares:
        turn = (vol_hands * 100.0 / float_shares) * 100.0
    cmv = (x["close"] * float_shares) if float_shares else None
    is_st = 1 if re.search(r"ST", name, re.I) else 0
    con.execute("delete from quote_clean where trade_date=? and code=?", (x["date"], code))
    con.execute(
        "insert into quote_clean "
        "(trade_date,code,name,board,is_st,is_bj,open,high,low,close,volume,amount,"
        "turnover_rate,circ_mv,data_source,turnover_est,circ_mv_est) "
        "values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (x["date"], code, name, board, is_st, 0, x["open"], x["high"], x["low"], x["close"],
         vol_hands, x.get("amount"), (turn / 100.0 if turn else None), cmv,
         x.get("source", "10jqka_kline_cloud"), turn, cmv),
    )
    return True


def run_intraday(pool: dict, last: dict) -> int:
    """盘中只改临时库，绝不把未收盘的半日 OHLCV 写入正式库。"""
    INTRA_DB.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(DB, INTRA_DB)
    quotes = fetch_tencent_quotes(list(pool))
    qtoday = {c: q for c, q in quotes.items() if q["date"] == TODAY}
    print(f"盘中快照 {len(quotes)} 只，其中当日 {len(qtoday)} 只 -> {INTRA_DB}")
    if not qtoday:
        print("开盘前/无当日成交，临时库保持上一交易日数据，页面不写入半日 K 线")
        return 0

    con = sqlite3.connect(INTRA_DB)
    inserted = 0
    for code, q in qtoday.items():
        name, board = pool[code]
        if _insert_quote(con, code, name, board, q, _float_shares(last.get(code) or {})):
            inserted += 1
    con.commit()
    new_max = con.execute("select max(trade_date) from quote_clean").fetchone()[0]
    con.close()
    print(f"OK 盘中临时库写入 {inserted} 只，最新 {new_max}；正式库未改")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--intraday", action="store_true", help="盘中模式：只更新临时库，不写正式库")
    args = ap.parse_args()
    if not DB.exists():
        raise SystemExit(f"缺少数据库 {DB}")
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    max_date = con.execute("select max(trade_date) from quote_clean").fetchone()[0]
    pool = {r["code"]: (r["name"] or "", r["board"] or "") for r in con.execute("select code,name,board from pool_quotes")}
    last = {}
    code_max = {}
    for r in con.execute(
        "select code,close,circ_mv_est,circ_mv,turnover_est,trade_date from quote_clean q "
        "where trade_date=(select max(trade_date) from quote_clean q2 where q2.code=q.code)"
    ):
        last[r["code"]] = dict(r)
    for r in con.execute("select code,max(trade_date) as d from quote_clean group by code"):
        code_max[r["code"]] = r["d"]
    con.close()

    if args.intraday:
        return run_intraday(pool, last)

    print(f"DB最新 {max_date}，股票 {len(pool)} 只，开始拉取日K")
    quotes = fetch_tencent_quotes(list(pool))
    print(f"腾讯盘后快照 {len(quotes)} 只")
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
                fetched[code] = []
                print(f"  [失败] {code}: {exc}")
            if i % 50 == 0:
                print(f"  进度 {i}/{len(pool)}")

    if failed and len(failed) > len(pool) * 0.15 and len(quotes) < len(pool) * 0.85:
        raise SystemExit(f"K线失败过多 {len(failed)}/{len(pool)}，腾讯快照 {len(quotes)}，停止更新")

    con = sqlite3.connect(DB)
    inserted = 0
    skipped_stale = 0
    for code, rows in fetched.items():
        name, board = pool[code]
        float_shares = _float_shares(last.get(code) or {})
        q = quotes.get(code)
        if q and q["date"] == TODAY:
            rows = [x for x in rows if x["date"] != TODAY] + [q]
        threshold = code_max.get(code) or "00000000"
        new_rows = [
            x for x in rows
            if x["date"] > threshold or (x["date"] == TODAY and x["date"] == threshold)
        ]
        if not new_rows:
            skipped_stale += 1
            continue
        for x in new_rows:
            if _insert_quote(con, code, name, board, x, float_shares):
                inserted += 1
    con.commit()
    new_max = con.execute("select max(trade_date) from quote_clean").fetchone()[0]
    con.close()
    print(f"OK 写入 {inserted} 行，失败 {len(failed)} 只，库内最新 {new_max}，无新数据 {skipped_stale} 只")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
