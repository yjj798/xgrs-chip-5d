# -*- coding: utf-8 -*-
"""
表头可排序的筹码矩阵：每股一行，近5日+当天共6个交易日，每页50只。
保留：价格/距5日线/集中度/存量获利/近2-5日流入占比/概念；其余字段已移除。
"""
import csv
import html
import json
import math
import os
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

BASE = Path(os.environ.get("XGRS_BASE", r"D:\ai\xgrs"))
CHIP_JSON = Path(os.environ.get("CHIP_JSON", str(BASE / "reports" / "chip_6d_data.json")))
POOL_CSV = Path(os.environ.get("POOL_CSV", r"E:\stockpool\out\股票池.csv"))
DB = Path(os.environ.get("POOL_DB", r"E:\stockpool\engine\ingest\data\quant_v2.db"))
DATA_ROOT = Path(os.environ.get("DATA_ROOT", str(BASE / "data")))
OUT = Path(os.environ.get("OUT_HTML", str(BASE / "reports" / "五日筹码_表头排序_6日_50只一页.html")))
PAGE_SIZE = 50
WARN_CAP = 20.0
EVENT_WORDS = (
    "回购", "业绩", "三季报", "中报", "年报", "股权激励", "协议转让", "在手订单",
    "中标", "涨价", "增持", "减持", "分红", "定增", "可转债", "要约收购", "控制权变更",
)


def primary_concept(raw: object) -> str:
    parts = [p.strip() for p in re.split(r"[·,，]", str(raw or "")) if p.strip()]
    starred = [p.lstrip("★ ").strip() for p in parts if p.startswith("★")]
    if starred:
        return starred[0]
    return parts[0] if parts else "其他"


def fmt_pct(x, nd=1):
    return "—" if x is None else f"{x:.{nd}f}%"


def build_concept_chart(dates, pool_concepts, top_n=15):
    """最近6个交易日（含当天）涨停股按主概念去重计数，输出每日柱状图。"""
    daily = defaultdict(lambda: defaultdict(set))
    for d in dates:
        path = DATA_ROOT / d / f"01_stocks_raw_{d}.json"
        if not path.exists():
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        for x in data.get("stocks", []):
            if not x.get("is_limit_up"):
                continue
            code = str(x.get("code") or "")
            c = pool_concepts.get(code)
            if not c:
                tags = [str(t).strip() for t in (x.get("all_tags") or []) if str(t).strip()]
                c = next((t for t in tags if not any(w in t for w in EVENT_WORDS)), "其他")
            daily[c or "其他"][d].add(code)

    total = len(daily)
    ranked = sorted(
        daily.items(),
        key=lambda kv: (-sum(len(kv[1].get(d, ())) for d in dates), kv[0]),
    )[:top_n]
    max_n = max((len(byday.get(d, ())) for _, byday in ranked for d in dates), default=1)
    rows = []
    for name, byday in ranked:
        counts = [len(byday.get(d, ())) for d in dates]
        bars = []
        for i, (d, n) in enumerate(zip(dates, counts)):
            prev = counts[i - 1] if i > 0 else None
            if prev is None:
                cls = "flat"
            elif n > prev:
                cls = "up"
            elif n < prev:
                cls = "down"
            else:
                cls = "flat"
            if i == len(dates) - 1:
                cls += " now"
            h = max(1, round(n / max_n * 26))
            chg = "" if prev is None else f"，较前一日{n - prev:+d}"
            bars.append(
                f'<span class="cb {cls}" title="{d} 涨停 {n} 只{chg}">'
                f'<em>{n}</em><i style="height:{h}px"></i><u>{d[8:]}</u></span>'
            )
        safe = html.escape(name, quote=True)
        rows.append(
            f'<div class="crow"><span class="cl concept-link" data-concept="{safe}" '
            f'title="点击只看该概念：{safe}">{safe}</span>'
            f'<span class="cbs">{"".join(bars)}</span>'
            f'<b class="ctotal" title="6日合计">{sum(counts)}</b></div>'
        )
    return (
        '<details class="concept-panel">'
        '<summary class="concept-head" title="点击展开 / 收起">'
        '<span class="ctitle">概念近6日涨停家数</span>'
        f'<span class="ccount">{total} 个概念</span>'
        '<span class="chev">▸</span></summary>'
        f'<div class="concept-grid">{"".join(rows)}</div></details>'
    )


def profit_ratio(dist, bins, close):
    total = sum(dist)
    if total <= 0 or close is None:
        return None
    got = sum(w for p, w in zip(bins, dist) if p <= close)
    return max(0.0, min(100.0, got / total * 100.0))


def stability_coef(dist, bins):
    if not dist:
        return None
    sm = []
    for i in range(len(dist)):
        seg = dist[max(0, i - 1):i + 2]
        sm.append(sum(seg) / len(seg))
    peaks = []
    for i in range(1, len(sm) - 1):
        if sm[i] >= sm[i - 1] and sm[i] >= sm[i + 1] and sm[i] > 0:
            peaks.append((sm[i], i))
    if not peaks:
        return None
    peaks.sort(reverse=True)
    p1 = peaks[0]
    p2 = None
    for p in peaks[1:]:
        if abs(p[1] - p1[1]) >= 5:
            p2 = p
            break
    if p2 is None or p2[0] <= 0:
        return 9.99
    return min(9.99, p1[0] / p2[0])


def pct_position(closes):
    if not closes:
        return None
    vals = closes[-60:]
    lo, hi = min(vals), max(vals)
    if hi <= lo:
        return 50.0
    return max(0.0, min(100.0, (vals[-1] - lo) / (hi - lo) * 100.0))


def triangle_weights(low, high, avg, centers):
    if high <= low:
        idx = min(range(len(centers)), key=lambda i: abs(centers[i] - low))
        w = [0.0] * len(centers)
        w[idx] = 1.0
        return w
    avg = min(max(avg, low), high)
    raw = []
    for p in centers:
        if p < low or p > high:
            raw.append(0.0)
        elif p <= avg:
            raw.append(max(0.0, (p - low) / max(avg - low, 1e-9)))
        else:
            raw.append(max(0.0, (high - p) / max(high - avg, 1e-9)))
    total = sum(raw)
    if total <= 0:
        idx = min(range(len(centers)), key=lambda i: abs(centers[i] - avg))
        w = [0.0] * len(centers)
        w[idx] = 1.0
        return w
    return [x / total for x in raw]


def streak_up(rows):
    """最近连续收涨天数，最多5日（6个交易日只有5次比较）。"""
    n = 0
    for i in range(len(rows) - 1, 0, -1):
        a, b = rows[i].get("close"), rows[i - 1].get("close")
        if a is not None and b is not None and a > b:
            n += 1
        else:
            break
    return n


def streak_flow(rows):
    """最近连续新进筹码量增加天数，最多5日。"""
    n = 0
    for i in range(len(rows) - 1, 0, -1):
        a, b = rows[i].get("new_vol"), rows[i - 1].get("new_vol")
        if a is not None and b is not None and a > b:
            n += 1
        else:
            break
    return n


def main():
    chip = json.loads(CHIP_JSON.read_text(encoding="utf-8"))
    dates = chip["dates"]
    last_key = dates[-1].replace("-", "")
    present = {s["code"]: s for s in chip["stocks"]}
    date_keys = [d.replace("-", "") for d in dates]

    pool_codes = []
    pool_concepts = {}
    with POOL_CSV.open(encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            code = (row.get("代码") or "").strip()
            if code:
                pool_codes.append(code)
                pool_concepts[code] = primary_concept(row.get("概念"))

    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    names = {r["code"]: (r["name"] or "", r["board"] or "") for r in con.execute("select code,name,board from pool_quotes")}
    latest = {}
    hist = {}
    placeholders = ",".join("?" for _ in pool_codes)
    for r in con.execute(
        f"select code,name,is_st,trade_date,close,high,low,volume,amount,circ_mv,circ_mv_est,turnover_est "
        f"from quote_clean where code in ({placeholders}) order by code,trade_date", pool_codes
    ):
        q = dict(r)
        hist.setdefault(q["code"], []).append(q)
        if q["trade_date"] == last_key:
            latest[q["code"]] = q
    con.close()

    stocks = []
    for code in pool_codes:
        qlist = hist.get(code, [])
        q = latest.get(code)
        if not q:
            continue
        close = q["close"]
        if close is None or close > 8.0001:
            continue
        nm = (q["name"] or names.get(code, ("", ""))[0] or code)
        if re.search(r"ST", nm, re.I) or q.get("is_st"):
            continue

        # 行情派生指标
        closes = [x["close"] for x in qlist if x["close"] is not None]
        cap_raw = q.get("circ_mv") or q.get("circ_mv_est")
        cap_yi = cap_raw / 1e8 if cap_raw else None
        ma5 = sum(closes[-5:]) / 5 if len(closes) >= 5 else None
        ma20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
        dev5 = (close / ma5 - 1) * 100 if ma5 else None
        pos = pct_position(closes)
        above20 = close > ma20 if ma20 else None

        s = present.get(code)
        if s is None or len(s.get("days", [])) < 6:
            stocks.append(dict(
                code=code, name=nm, board=names.get(code, ("", ""))[1], price=close,
                concept=pool_concepts.get(code, ""),
                cap=cap_yi, ma5=ma5, ma20=ma20, dev5=dev5, pos=pos, above20=above20,
                missing=True, signal="数据不足",
                winner=None, new_profit=None, prev_new_profit=None, stability=None,
                concentration=None, conc_prev=None, conc_narrow=None, turnover=None,
                avg_turn=None, vs5=None, cum_new=0, cum_amt=0,
                inflow_pct={}, profit_cum={},
                consec_profit=0, consec_inflow=0,
                rows=[dict(date=dates[i], missing=True) for i in range(6)], spark=[0] * 6,
            ))
            continue

        ds = s["days"][:6]
        bins = s["bins"]
        dists = s["dists"][:6]

        # 每日：昨获、迁移方向
        qmap = {x["trade_date"]: x for x in qlist}
        rows = []
        for i, d in enumerate(ds):
            prev_profit = None
            new_profit = None
            if i > 0:
                prev_profit = profit_ratio(dists[i - 1], bins, d["close"])
                stay = max(0.0, 1.0 - min(1.0, d["moved"] / 100.0))
                new_w = [max(0.0, dists[i][j] - dists[i - 1][j] * stay) for j in range(len(bins))]
                new_profit = profit_ratio(new_w, bins, d["close"])
            else:
                # 首日：用当日三角新增权重反推前一日存量分布，补全“昨获”
                q0 = qmap.get(date_keys[0])
                if q0 and q0.get("low") and q0.get("high") and q0.get("volume"):
                    h, l = q0["high"], q0["low"]
                    v, amt = q0["volume"], (q0.get("amount") or 0)
                    avg = amt / (v * 100.0) if amt > 0 else (q0["close"] + h + l) / 3.0
                    new0 = triangle_weights(l, h, avg, bins)
                    moved0 = min(1.0, max(0.0, d["moved"] / 100.0))
                    stay0 = 1.0 - moved0
                    if stay0 > 0:
                        old0 = [max(0.0, dists[0][j] - new0[j] * moved0) / stay0 for j in range(len(bins))]
                        prev_profit = profit_ratio(old0, bins, d["close"])
                    new_profit = profit_ratio(new0, bins, d["close"])

            # 当日收盘在60日区间的位置
            day_closes = [x["close"] for x in qlist if x["trade_date"] <= date_keys[i] and x["close"] is not None]
            day_pos = pct_position(day_closes)
            avg_delta = None
            if i > 0 and ds[i - 1].get("avg_cost") and d.get("avg_cost"):
                avg_delta = d["avg_cost"] - ds[i - 1]["avg_cost"]
            if day_pos is not None and day_pos <= 35 and (
                (d.get("vs5") or 0) >= 1.2 or (avg_delta is not None and avg_delta < 0) or (d.get("profit") or 0) < 30
            ):
                move = "低位吸筹"
            elif day_pos is not None and day_pos >= 65 and (d.get("profit") or 0) >= 50 and (
                (d.get("vs5") or 0) >= 1.2 or (avg_delta is not None and avg_delta > 0)
            ):
                move = "高位派发"
            else:
                move = "横盘"

            flow_share = d.get("moved")
            if flow_share is not None:
                flow_share = round(float(flow_share), 2)

            rows.append(dict(
                date=dates[i], close=d["close"], new_vol=d["new_vol_wan"], new_amt=d["new_amt_yi"],
                moved=d["moved"], vs5=d["vs5"], avg_cost=d["avg_cost"], profit=d["profit"],
                conc=d["concentration_90"], signal=d["signal"], flow_share=flow_share,
                prev_profit=prev_profit, new_profit=new_profit, move=move, day_pos=day_pos,
                missing=False,
            ))

        latest_d = ds[-1]
        winner = latest_d.get("profit")
        new_profit_latest = rows[-1]["new_profit"]
        prev_new_profit = rows[-2]["new_profit"] if len(rows) >= 2 else None
        stability = stability_coef(dists[-1], bins)
        concentration = latest_d.get("concentration_90")
        conc_prev = ds[-2].get("concentration_90") if len(ds) >= 2 else None
        conc_narrow = (concentration < conc_prev) if (concentration is not None and conc_prev is not None) else None
        turnover = latest_d.get("moved")
        vs5 = latest_d.get("vs5")
        avg_turn = sum(x["moved"] for x in ds[:-1]) / 5 if len(ds) >= 6 else None

        # 规则打标已移除
        stocks.append(dict(
            code=s["code"], name=s["name"], board=s.get("board", ""), price=s["latest_close"],
            concept=pool_concepts.get(code, ""),
            cap=cap_yi, ma5=ma5, ma20=ma20, dev5=dev5, pos=pos, above20=above20,
            missing=False, signal=s["latest_signal"],
            winner=winner, new_profit=new_profit_latest,
            stability=stability, concentration=concentration, conc_prev=conc_prev, conc_narrow=conc_narrow,
            turnover=turnover, avg_turn=avg_turn, vs5=vs5,
            consec_profit=streak_up(rows), consec_inflow=streak_flow(rows),
            cum_new=sum(x["new_vol"] or 0 for x in rows),
            cum_amt=sum(x["new_amt"] or 0 for x in rows),
            rows=rows, spark=[x["new_vol"] or 0 for x in rows],
            inflow_pct={}, profit_cum={},
        ))

    # 默认排序：距5日线由远到近（按绝对值）
    stocks.sort(key=lambda x: (-(abs(x["dev5"]) if x["dev5"] is not None else -999), x["code"]))

    # 近1-5日流入占比与获利总和排名：流入累计新进筹码量/流通股本；获利累加每日获利比例
    for n in (1, 2, 3, 4, 5):
        for x in stocks:
            if x["missing"] or len(x["rows"]) < n:
                continue
            tail = x["rows"][-n:]
            if any(r.get("missing") for r in tail):
                continue
            if x.get("cap") and x.get("price"):
                cum = sum(r.get("new_vol") or 0 for r in tail)
                float_yi = x["cap"] / x["price"]
                pct = cum / (float_yi * 1e4) * 100 if float_yi > 0 else None
                if pct is not None:
                    x["inflow_pct"][str(n)] = round(pct, 2)
            if all(r.get("profit") is not None for r in tail):
                x["profit_cum"][str(n)] = round(sum(r["profit"] for r in tail), 2)

    concept_panel = build_concept_chart(dates, pool_concepts)

    missing_count = sum(1 for x in stocks if x["missing"])
    streak2 = sum(1 for x in stocks if not x["missing"] and x["consec_profit"] >= 2)
    flow2 = sum(1 for x in stocks if not x["missing"] and x["consec_inflow"] >= 2)
    payload = dict(dates=dates, total=len(stocks), page_size=PAGE_SIZE,
                   generated_at=chip.get("generated_at"), stocks=stocks)
    data_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    head_dates = "".join(f'<th class="date sortable" data-sort="day{i}" title="点击按当日流入占总筹码比例排序">{d[5:]}<span class="arrow"></span></th>' for i, d in enumerate(dates))

    html = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>牛逼选股</title>
<meta name="application-name" content="牛逼选股">
<meta property="og:site_name" content="牛逼选股">
<meta property="og:title" content="牛逼选股">
<meta property="og:image" content="https://t.legaga.top/logo.png">
<style>
:root {
  --bg:#0d1016; --panel:#141922; --panel2:#111722; --line:#2a3341; --text:#d9e0ea;
  --muted:#8591a3; --weak:#5d6878; --accent:#8fb3ff; --gold:#d9a85f;
  --green:#6fbf8f; --red:#d97b7b; --amber:#d6ad63;
}
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:12.5px/1.4 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif; }
header { padding:14px 22px 10px; border-bottom:1px solid var(--line); display:flex; justify-content:center; align-items:center; }
h1.brand { margin:0; padding:7px 16px; background:#fff; border-radius:10px; box-shadow:0 3px 14px rgba(0,0,0,.28); line-height:0; }
h1.brand img { display:block; height:52px; width:auto; max-width:min(70vw, 420px); }
.toolbar { padding:8px 22px; display:flex; gap:7px; align-items:center; flex-wrap:wrap; border-bottom:1px solid var(--line); position:sticky; top:0; background:var(--bg); z-index:40; }
.concept-panel { margin:10px 22px 0; background:var(--panel); border:1px solid var(--line); border-radius:7px; padding:0 12px; }
.concept-head { display:flex; gap:10px; align-items:center; padding:10px 0; cursor:pointer; list-style:none; user-select:none; }
.concept-head::-webkit-details-marker { display:none; }
.concept-head .ctitle { font-size:13px; font-weight:650; color:var(--text); }
.concept-head .ccount { color:var(--weak); font-size:10px; }
.concept-head .chev { margin-left:auto; color:var(--weak); font-size:12px; transition:transform .15s; }
.concept-head:hover .ctitle { color:var(--accent); }
.concept-panel[open] .chev { transform:rotate(90deg); }
.concept-grid { display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:0; margin:0 0 10px; border-top:1px solid var(--line); border-left:1px solid var(--line); }
.crow { display:flex; align-items:center; gap:8px; min-width:0; font-size:10px; height:54px; padding:0 12px; border-right:1px solid var(--line); border-bottom:1px solid var(--line); background:#131923; }
.crow:hover { background:#182131; }
.crow .cl { width:94px; color:var(--muted); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.crow .cl.concept-link:hover { color:var(--accent); text-decoration:underline; }
.crow .cl.concept-link.on { color:var(--accent); font-weight:650; text-decoration:underline; }
.concept-link { cursor:pointer; }
.concept-cell .cc.concept-link:hover { color:var(--accent); text-decoration:underline; }
.concept-cell .cc.concept-link.on { color:var(--accent); font-weight:650; }
.concept-clear { background:#1c2635; color:var(--accent); border:1px solid #4b6288; border-radius:4px; padding:4px 8px; cursor:pointer; font-size:11px; }
.concept-clear:hover { background:#243145; }
.concept-clear b { color:var(--text); font-weight:600; }
.crow .cbs { flex:0 0 160px; display:flex; align-items:flex-end; gap:8px; height:50px; }
.crow .cb { width:20px; height:48px; display:flex; flex-direction:column; align-items:center; justify-content:flex-end; }
.crow .cb em { font-style:normal; font-size:8.5px; color:var(--muted); line-height:1; margin-bottom:2px; font-weight:650; }
.crow .cb i { display:block; width:11px; min-height:1px; background:#4a5f7d; border-radius:2px 2px 0 0; }
.crow .cb.up i { background:#d9534f; }
.crow .cb.down i { background:#2f9e73; }
.crow .cb.now i { outline:1px solid #b8863b; outline-offset:1px; }
.crow .cb u { margin-top:2px; font-size:7.5px; line-height:1; color:var(--weak); text-decoration:none; }
.crow .cb.now u { color:var(--gold); }
.crow .ctotal { margin-left:auto; width:30px; text-align:right; color:var(--gold); font-weight:650; }
input,select { background:#10151d; color:var(--text); border:1px solid var(--line); border-radius:5px; padding:6px 8px; outline:none; font-size:11.5px; }
input { width:174px; }
input:focus,select:focus { border-color:#4b6288; }
.count { color:var(--weak); font-size:11px; margin-left:auto; white-space:nowrap; }
.pager { display:flex; gap:4px; align-items:center; }
.pager button { background:#10151d; color:var(--text); border:1px solid var(--line); border-radius:4px; padding:4px 7px; cursor:pointer; font-size:11px; }
.pager button:hover:not(:disabled) { background:#1a2230; }
.pager button:disabled { color:#3c4654; cursor:default; }
.pager button.on { border-color:#4b6288; background:#1c2635; color:var(--accent); }
.table-wrap { margin:0 22px; overflow:auto; max-height:calc(100vh - 162px); }
table { border-collapse:separate; border-spacing:0; table-layout:fixed; width:100%; min-width:1432px; }
th,td { border-right:1px solid var(--line); border-bottom:1px solid var(--line); }
thead th { position:sticky; top:0; z-index:12; background:var(--panel2); color:var(--muted); font-weight:500; font-size:10.5px; padding:6px 5px; }
thead .row2 th { top:27px; z-index:12; }
thead .grp { text-align:center; border-bottom:1px solid var(--line); color:var(--text); font-size:11.5px; }
thead .date { text-align:center; font-size:10.5px; width:115px; }
thead .stock-h { width:130px; text-align:left; padding-left:9px; }
thead .stat-h { text-align:left; padding-left:7px; }
thead .sortable { cursor:pointer; user-select:none; }
thead .sortable:hover { color:var(--text); background:#172030; }
thead .sortable.active { color:var(--accent); }
.arrow { display:inline-block; width:9px; margin-left:2px; color:var(--accent); font-size:9px; }
tbody td { padding:6px 6px; vertical-align:top; }
tbody tr:hover td { background:#151c27; }
.stock { position:sticky; left:0; z-index:8; background:var(--panel); width:130px; }
.stock .nm { font-size:13.5px; font-weight:650; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.stock .nm a, .stock .cd a { color:inherit; text-decoration:none; }
.stock .nm a:hover, .stock .cd a:hover { color:var(--accent); }
.stock .cd { color:var(--weak); font-size:10px; white-space:nowrap; }
.price { width:82px; }
.price .px { color:var(--gold); font-size:16px; font-weight:650; line-height:1.2; }
.position { width:84px; }
.position .dev { font-size:13px; font-weight:650; white-space:nowrap; }
.position .dev.up { color:var(--red); }
.position .dev.down { color:var(--green); }
.position .far { color:var(--muted); font-size:9.5px; margin-top:1px; white-space:nowrap; }
.conc { width:78px; }
.conc .c90 { font-size:12px; font-weight:600; white-space:nowrap; }
.conc .trend { font-size:9.5px; white-space:nowrap; margin-top:1px; }
.winner { width:78px; }
.winner .v { font-size:13px; font-weight:650; white-space:nowrap; }
.profit-total { width:82px; }
.profit-total .p { font-size:12.5px; font-weight:650; color:var(--gold); white-space:nowrap; }
.inflow { width:82px; }
.inflow .p { font-size:12.5px; font-weight:650; color:var(--accent); white-space:nowrap; }
.concept-cell { width:124px; }
.concept-cell .cc { font-size:10.5px; color:var(--text); white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.daycell { width:115px; }
.daycell .rl { display:flex; justify-content:space-between; gap:5px; white-space:nowrap; font-size:9.5px; color:var(--muted); }
.daycell .rl b { font-size:12.5px; font-weight:650; font-variant-numeric:tabular-nums; color:var(--text); }
.daycell .rl b.up { color:var(--red); }
.daycell .rl b.down { color:var(--green); }
.daycell .rl b.hot { color:var(--red); }
.daycell .rl b.cold { color:var(--green); }
.daycell.missing { color:var(--weak); background:#10151d; font-size:10px; }
.empty { padding:30px; color:var(--muted); text-align:center; }
.footer-pager { padding:8px 22px 14px; display:flex; gap:6px; align-items:center; }
.site-footer { margin:0 22px; padding:14px 0 20px; border-top:1px solid var(--line); display:flex; flex-direction:column; align-items:center; gap:7px; color:var(--weak); font-size:10.5px; text-align:center; }
.site-footer .update-line { display:flex; align-items:center; justify-content:center; gap:10px; flex-wrap:wrap; }
.site-footer #refreshBtn { background:#1c2635; color:var(--text); border:1px solid #4b6288; border-radius:5px; padding:7px 14px; cursor:pointer; font-size:12px; }
.site-footer #refreshBtn:hover:not(:disabled) { background:#243145; border-color:#6a87b3; }
.site-footer #refreshBtn:disabled { color:var(--weak); cursor:wait; border-color:var(--line); }
.site-footer #updateMsg { color:var(--muted); font-size:11px; }
.site-footer .meta b { color:var(--muted); font-weight:600; }
.site-footer .copy { color:#4f5a6a; font-size:10px; }
.up { color:var(--red); }
.down { color:var(--green); }
.hot { color:var(--red); }
.cold { color:var(--green); }
.warn { color:var(--amber); }
.ok { color:var(--green); }
#gate { position:fixed; inset:0; z-index:99999; background:rgba(8,11,16,.94); display:flex; align-items:center; justify-content:center; padding:20px; }
#gate.hidden { display:none; }
#gate .box { width:min(360px,92vw); background:var(--panel); border:1px solid var(--line); border-radius:10px; padding:24px 22px; box-shadow:0 18px 60px rgba(0,0,0,.45); }
#gate .gate-logo { width:max-content; max-width:100%; margin:0 auto 16px; padding:6px 12px; background:#fff; border-radius:8px; line-height:0; }
#gate .gate-logo img { display:block; height:44px; width:auto; max-width:100%; }
#gate h2 { margin:0 0 6px; font-size:20px; font-weight:650; }
#gate p { margin:0 0 16px; color:var(--muted); font-size:12px; }
#gate input { width:100%; background:#10151d; color:var(--text); border:1px solid var(--line); border-radius:6px; padding:10px 11px; outline:none; font-size:14px; }
#gate input:focus { border-color:#4b6288; }
#gate button { width:100%; margin-top:10px; background:#26364d; color:var(--text); border:1px solid #3b5272; border-radius:6px; padding:10px; cursor:pointer; font-size:14px; }
#gate button:hover { background:#30445f; }
#gate .err { min-height:18px; margin-top:8px; color:var(--red); font-size:12px; }
@media (max-width:1200px) { .table-wrap { max-height:none; } }
</style>
</head>
<body>
<div id="gate">
  <div class="box">
    <div class="gate-logo"><img src="logo.png" alt="牛逼选股"></div>
    <p>请输入访问密码</p>
    <input id="gatePwd" type="password" autocomplete="off" spellcheck="false" placeholder="访问密码">
    <button id="gateBtn" type="button">进入</button>
    <div class="err" id="gateErr"></div>
  </div>
</div>
<header>
  <h1 class="brand"><img src="logo.png" alt="牛逼选股"></h1>
</header>
__CONCEPT__
<div class="toolbar">
  <input id="q" placeholder="搜索代码 / 名称">
  <select id="win"><option value="all">全部 存量获利</option><option value="50">&gt;50%</option><option value="60">&gt;60%</option><option value="70" selected>&gt;70%</option><option value="80">&gt;80%</option><option value="90">&gt;90%</option></select>
  <select id="streak" title="连续获利=最近N个交易日收盘价连续高于前一日；最多5日"><option value="all">连续获利</option><option value="1">&gt;1天</option><option value="2" selected>&gt;2天</option><option value="3">&gt;3天</option><option value="4">&gt;4天</option><option value="5">&gt;5天</option></select>
  <select id="flow" title="连续流入增多=新进筹码量连续高于前一日；最多5日"><option value="all">连续流入增多</option><option value="1">&gt;1天</option><option value="2" selected>&gt;2天</option><option value="3">&gt;3天</option><option value="4">&gt;4天</option><option value="5">&gt;5天</option></select>
  <select id="inflowN" title="N日累计新进筹码量占流通股本的比例；默认2日"><option value="1">1日流入总排行</option><option value="2" selected>2日流入总排行</option><option value="3">3日流入总排行</option><option value="4">4日流入总排行</option><option value="5">5日流入总排行</option></select>
  <select id="profitN" title="N日每日获利比例累加，按总和降序；默认2日"><option value="1">1日获利总排行</option><option value="2" selected>2日获利总排行</option><option value="3">3日获利总排行</option><option value="4">4日获利总排行</option><option value="5">5日获利总排行</option></select>
  <button id="conceptClear" class="concept-clear" style="display:none">概念：<b id="conceptName"></b> ×</button>
  <div class="pager" id="pagerTop"></div>
  <div class="count" id="count"></div>
</div>
<div class="table-wrap">
<table>
<thead>
<tr>
  <th class="stock-h sortable" data-sort="code" rowspan="2">股票<span class="arrow"></span></th>
  <th class="stat-h price sortable" data-sort="price" rowspan="2">收盘价<span class="arrow"></span></th>
  <th class="stat-h position sortable" data-sort="dev_abs" rowspan="2" title="点击按距5日线绝对距离远近排序">距5日线%<span class="arrow"></span></th>
  <th class="stat-h conc sortable" data-sort="conc" rowspan="2" title="90%筹码集中度，数值越小越集中">集中度<span class="arrow"></span></th>
  <th class="stat-h winner sortable" data-sort="winner" rowspan="2">存量获利<span class="arrow"></span></th>
  <th class="stat-h profit-total sortable" data-sort="profit_total" rowspan="2" title="点击按N日每日获利比例累加排序"><span id="profitLabel">2日获利总和</span><span class="arrow"></span></th>
  <th class="stat-h inflow sortable" data-sort="inflow" rowspan="2" title="点击按N日累计新进筹码量占流通股本比例排序"><span id="inflowLabel">2日流入占比</span><span class="arrow"></span></th>
  <th class="grp" colspan="6">筹码变化（近5日 + 当天）</th>
  <th class="stat-h concept-cell sortable" data-sort="concept" rowspan="2">概念<span class="arrow"></span></th>
</tr>
<tr class="row2">__HEAD_DATES__</tr>
</thead>
<tbody id="tbody"></tbody>
</table>
</div>
<div class="footer-pager"><div class="pager" id="pagerBottom"></div></div>
<footer class="site-footer">
  <div class="update-line">
    <button id="refreshBtn" type="button">立即更新</button>
    <span id="updateMsg">更新包含股价、获利、概念、涨停等全部数据</span>
  </div>
  <div class="meta">最后更新：<b id="updatedAt">—</b> · 数据日期：<b id="latestDate">—</b></div>
  <div class="meta">更新内容：股价 · 筹码获利 · 概念统计 · 涨停池 · 流入/获利排行</div>
  <div class="copyright">© 2026 乐嘎嘎legaga.top版权所有</div>
</footer>
<script id="payload" type="application/json">__DATA__</script>
<script>
const DATA = JSON.parse(document.getElementById('payload').textContent);
const stocks = DATA.stocks;
const PAGE_SIZE = DATA.page_size;
const state = { q:'', win:'70', streak:'2', flow:'2', inN:'2', profitN:'2', concept:'', sort:'inflow', dir:-1, page:1 };
const $ = id => document.getElementById(id);
const esc = s => String(s == null ? '' : s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const n0 = n => n == null ? '—' : Number(n).toLocaleString('zh-CN', {maximumFractionDigits:0});
const n1 = n => n == null ? '—' : Number(n).toLocaleString('zh-CN', {minimumFractionDigits:1, maximumFractionDigits:1});
const n2 = n => n == null ? '—' : Number(n).toLocaleString('zh-CN', {minimumFractionDigits:2, maximumFractionDigits:2});
const pct = (n,d=1) => n == null ? '—' : Number(n).toFixed(d) + '%';
const xx = n => n == null ? '—' : Number(n).toFixed(2) + 'x';
const posLabel = p => p == null ? '—' : p <= 30 ? '低位' : p >= 70 ? '高位' : '中位';
const concLabel = c => c == null ? '—' : c < 10 ? '高度集中' : c < 15 ? '较集中' : '分散';
const conceptHtml = s => s.concept
  ? `<div class="cc concept-link" data-concept="${esc(s.concept)}" title="点击只看该概念：${esc(s.concept)}">${esc(s.concept)}</div>`
  : '<div class="cc">—</div>';

function dayCell(s, i) {
  const d = s.rows[i];
  if (d.missing) return `<td class="daycell missing">停牌 / 缺数据</td>`;
  const flow = d.flow_share;
  const prevFlow = i > 0 && s.rows[i - 1] && !s.rows[i - 1].missing ? s.rows[i - 1].flow_share : null;
  const flowCls = flow == null || prevFlow == null ? '' : flow > prevFlow ? 'up' : flow < prevFlow ? 'down' : '';
  const dtxt = flow == null ? '—' : `${Number(flow).toFixed(1)}%`;
  const prevProfit = i > 0 && s.rows[i - 1] && !s.rows[i - 1].missing ? s.rows[i - 1].profit : null;
  const profitCls = d.profit == null || prevProfit == null ? '' : d.profit > prevProfit ? 'up' : d.profit < prevProfit ? 'down' : '';
  return `<td class="daycell">
    <div class="rl"><span title="当日新进筹码占总筹码的比例（约等于换手率 × decay）">流入占比</span><b class="${flowCls}" title="较前一交易日：增多红 / 减少绿">${dtxt}</b></div>
    <div class="rl"><span>获利比例</span><b class="${profitCls}" title="较前一交易日：增多红 / 减少绿">${pct(d.profit)}</b></div>
  </td>`;
}

function rowHtml(s) {
  if (s.missing) {
    return `<tr>
      <td class="stock"><div class="nm"><a href="https://stockpage.10jqka.com.cn/${s.code}/" target="_blank" rel="noopener">${esc(s.name)}</a></div><div class="cd"><a href="https://stockpage.10jqka.com.cn/${s.code}/" target="_blank" rel="noopener">${s.code}</a></div></td>
      <td class="price"><div class="px">${n2(s.price)}</div></td>
      <td class="position"><div class="dev">—</div><div class="far">距5日线</div></td>
      <td class="conc"><div class="c90">—</div><div class="trend">—</div></td>
      <td class="winner"><div class="v">—</div></td>
      <td class="profit-total"><div class="p">—</div></td>
      <td class="inflow"><div class="p">—</div></td>
      ${s.rows.map((_,i) => dayCell(s,i)).join('')}
      <td class="concept-cell">${conceptHtml(s)}</td>
    </tr>`;
  }
  const devCls = s.dev5 == null ? '' : s.dev5 > 0 ? 'up' : s.dev5 < 0 ? 'down' : '';
  const devTxt = s.dev5 == null ? '—' : `${s.dev5 > 0 ? '+' : ''}${s.dev5.toFixed(2)}%`;
  const concTrend = s.conc_narrow == null ? '—' : s.conc_narrow ? '收窄' : '发散';
  const concTrendCls = s.conc_narrow == null ? '' : s.conc_narrow ? 'up' : 'down';
  const winCls = s.winner == null ? '' : s.winner >= 80 ? 'hot' : s.winner <= 10 ? 'cold' : '';
  return `<tr>
    <td class="stock">
      <div class="nm" title="${esc(s.name)} ${s.code}"><a href="https://stockpage.10jqka.com.cn/${s.code}/" target="_blank" rel="noopener">${esc(s.name)}</a></div>
      <div class="cd"><a href="https://stockpage.10jqka.com.cn/${s.code}/" target="_blank" rel="noopener">${s.code}</a></div>
    </td>
    <td class="price"><div class="px">${n2(s.price)}</div></td>
    <td class="position"><div class="dev ${devCls}">${devTxt}</div><div class="far">距5日线</div></td>
    <td class="conc"><div class="c90">${pct(s.concentration,2)}</div><div class="trend"><span class="${concTrendCls}">${concTrend}</span> · ${concLabel(s.concentration)}</div></td>
    <td class="winner"><div class="v ${winCls}">${pct(s.winner)}</div></td>
    <td class="profit-total"><div class="p">${s.profit_cum[state.profitN] == null ? '—' : pct(s.profit_cum[state.profitN],1)}</div></td>
    <td class="inflow"><div class="p">${s.inflow_pct[state.inN] == null ? '—' : pct(s.inflow_pct[state.inN],1)}</div></td>
    ${s.rows.map((_,i) => dayCell(s,i)).join('')}
    <td class="concept-cell">${conceptHtml(s)}</td>
  </tr>`;
}

function inWin(s) {
  if (state.win === 'all') return true;
  if (s.winner == null) return false;
  return s.winner >= Number(state.win);
}
function inStreak(s) {
  if (state.streak === 'all') return true;
  return !s.missing && (s.consec_profit || 0) >= Number(state.streak);
}
function inFlow(s) {
  if (state.flow === 'all') return true;
  return !s.missing && (s.consec_inflow || 0) >= Number(state.flow);
}
function sortVal(s, key) {
  if (s.missing) return null;
  if (key.startsWith('day')) {
    const i = Number(key.slice(3));
    const r = s.rows[i];
    return r && !r.missing ? r.flow_share : null;
  }
  switch (key) {
    case 'code': return s.code;
    case 'price': return s.price;
    case 'dev_abs': return s.dev5 == null ? null : Math.abs(s.dev5);
    case 'conc': return s.concentration;
    case 'winner': return s.winner;
    case 'profit_total': return s.profit_cum[state.profitN];
    case 'concept': return s.concept || '';
    case 'inflow': return s.inflow_pct[state.inN];
    case 'cum': return s.cum_new;
    default: return s.code;
  }
}
function cmpSort(a, b) {
  const av = sortVal(a, state.sort), bv = sortVal(b, state.sort);
  if (av == null && bv == null) return a.code.localeCompare(b.code);
  if (av == null) return 1;
  if (bv == null) return -1;
  let c;
  if (typeof av === 'string') c = av.localeCompare(bv);
  else c = av - bv;
  if (c === 0) c = a.code.localeCompare(b.code);
  return c * state.dir;
}
function filtered() {
  let arr = stocks.filter(s => {
    if (!inWin(s)) return false;
    if (!inStreak(s) || !inFlow(s)) return false;
    if (state.concept && (s.concept || '') !== state.concept) return false;
    if (state.q) {
      const q = state.q.toLowerCase();
      if (!(s.code.toLowerCase().includes(q) || (s.name || '').toLowerCase().includes(q))) return false;
    }
    return true;
  });
  arr.sort(cmpSort);
  return arr;
}
function pagerHtml(totalPages) {
  let btns = `<button data-p="prev" ${state.page<=1?'disabled':''}>上一页</button>`;
  for (let p=1; p<=totalPages; p++) {
    if (totalPages > 9 && ((p < state.page - 2 && p > 2) || (p > state.page + 2 && p < totalPages - 1))) {
      if (p === 3 || p === totalPages - 2) btns += `<span style="color:#5d6878">…</span>`;
      continue;
    }
    btns += `<button data-p="${p}" class="${p===state.page?'on':''}">${p}</button>`;
  }
  btns += `<button data-p="next" ${state.page>=totalPages?'disabled':''}>下一页</button>`;
  return btns;
}
function bindPager(el, totalPages) {
  el.innerHTML = pagerHtml(totalPages);
  el.querySelectorAll('button').forEach(b => b.onclick = () => {
    const p = b.dataset.p;
    if (p === 'prev') state.page = Math.max(1, state.page - 1);
    else if (p === 'next') state.page = Math.min(totalPages, state.page + 1);
    else state.page = Number(p);
    render();
  });
}
function defaultDir(key) {
  return ['code','price','conc','winner','concept'].includes(key) ? 1 : -1;
}
function updateSortIndicators() {
  document.querySelectorAll('th.sortable').forEach(th => {
    const active = th.dataset.sort === state.sort;
    th.classList.toggle('active', active);
    const arrow = th.querySelector('.arrow');
    if (arrow) arrow.textContent = active ? (state.dir > 0 ? '▲' : '▼') : '';
  });
}
function render() {
  const arr = filtered();
  const totalPages = Math.max(1, Math.ceil(arr.length / PAGE_SIZE));
  if (state.page > totalPages) state.page = totalPages;
  const start = (state.page - 1) * PAGE_SIZE;
  const pageRows = arr.slice(start, start + PAGE_SIZE);
  $('count').textContent = `${arr.length} 只 · 第 ${state.page}/${totalPages} 页 · ${arr.length ? start+1 : 0}-${Math.min(start+PAGE_SIZE, arr.length)}`;
  $('conceptClear').style.display = state.concept ? '' : 'none';
  $('conceptName').textContent = state.concept;
  document.querySelectorAll('.concept-link').forEach(el => el.classList.toggle('on', el.dataset.concept === state.concept));
  bindPager($('pagerTop'), totalPages);
  bindPager($('pagerBottom'), totalPages);
  $('tbody').innerHTML = pageRows.length ? pageRows.map(rowHtml).join('') : '<tr><td colspan="14" class="empty">没有符合条件的结果</td></tr>';
  $('inflowLabel').textContent = `${state.inN}日流入占比`;
  $('profitLabel').textContent = `${state.profitN}日获利总和`;
  updateSortIndicators();
}
['q','win','streak','flow'].forEach(id => {
  $(id).addEventListener(id === 'q' ? 'input' : 'change', e => {
    state[id] = e.target.value.trim ? e.target.value.trim() : e.target.value;
    state.page = 1;
    render();
  });
});
$('inflowN').addEventListener('change', e => {
  state.inN = e.target.value;
  state.sort = 'inflow';
  state.dir = -1;
  state.page = 1;
  render();
});
$('profitN').addEventListener('change', e => {
  state.profitN = e.target.value;
  state.sort = 'profit_total';
  state.dir = -1;
  state.page = 1;
  render();
});
document.querySelectorAll('th.sortable').forEach(th => {
  th.addEventListener('click', () => {
    const key = th.dataset.sort;
    if (state.sort === key) state.dir = -state.dir;
    else {
      state.sort = key;
      state.dir = defaultDir(key);
    }
    state.page = 1;
    render();
  });
});
document.addEventListener('click', e => {
  const link = e.target.closest('.concept-link');
  if (!link) return;
  const c = link.dataset.concept || '';
  state.concept = state.concept === c ? '' : c;
  state.page = 1;
  render();
});
$('conceptClear').addEventListener('click', () => {
  state.concept = '';
  state.page = 1;
  render();
});

// 底部更新信息 + 一键更新
const UPDATE_API = 'https://up.legaga.top';
const updatedAt = $('updatedAt');
const latestDate = $('latestDate');
const refreshBtn = $('refreshBtn');
const updateMsg = $('updateMsg');
updatedAt.textContent = DATA.generated_at || '—';
latestDate.textContent = (DATA.dates && DATA.dates.length) ? DATA.dates[DATA.dates.length - 1] : '—';
const sleep = ms => new Promise(r => setTimeout(r, ms));
function setUpdateMsg(t) { updateMsg.textContent = t; }
async function pageEtag() {
  try {
    const r = await fetch(location.pathname, { method: 'HEAD', cache: 'no-store' });
    return r.headers.get('etag') || r.headers.get('last-modified') || '';
  } catch (e) {
    return '';
  }
}
async function waitForDeploy(beforeEtag) {
  if (!beforeEtag) {
    setUpdateMsg('更新完成，正在加载最新页面…');
    await sleep(30000);
    location.replace(location.pathname + '?v=' + Date.now());
    return;
  }
  for (let i = 0; i < 24; i++) {
    await sleep(5000);
    const nowEtag = await pageEtag();
    if (nowEtag && nowEtag !== beforeEtag) {
      setUpdateMsg('更新完成，正在加载最新页面…');
      await sleep(8000);
      location.replace(location.pathname + '?v=' + Date.now());
      return;
    }
    setUpdateMsg(`更新完成，等待页面部署…（${i + 1}/24）`);
  }
  setUpdateMsg('更新任务已完成，但页面尚未切换，请稍后手动刷新');
  refreshBtn.disabled = false;
  refreshBtn.textContent = '立即更新';
}
async function pollUpdate(runId, beforeEtag) {
  for (let i = 0; i < 120; i++) {
    await sleep(5000);
    let s;
    try {
      const r = await fetch(`${UPDATE_API}/status?id=${encodeURIComponent(runId)}`, { cache: 'no-store' });
      if (!r.ok) throw new Error(`HTTP ${r.status}`);
      s = await r.json();
    } catch (e) {
      setUpdateMsg('更新状态查询失败，正在重试…');
      continue;
    }
    if (s.status === 'completed') {
      if (s.conclusion === 'success') {
        return waitForDeploy(beforeEtag);
      }
      setUpdateMsg(`更新失败（${s.conclusion || '未知原因'}），请稍后重试`);
      refreshBtn.disabled = false;
      refreshBtn.textContent = '立即更新';
      return;
    }
    setUpdateMsg(`正在更新…（${s.status || '排队中'}）`);
  }
  setUpdateMsg('更新耗时较长，请稍后刷新页面查看');
  refreshBtn.disabled = false;
  refreshBtn.textContent = '立即更新';
}
async function startUpdate() {
  if (refreshBtn.disabled) return;
  refreshBtn.disabled = true;
  refreshBtn.textContent = '更新中…';
  setUpdateMsg('正在请求云端更新全部数据…');
  const beforeEtag = await pageEtag();
  try {
    const r = await fetch(`${UPDATE_API}/update`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: 'close' })
    });
    let res = {};
    try { res = await r.json(); } catch (e) {}
    if (!r.ok || !res.ok) {
      if (res.error === 'running' && res.run && res.run.id) {
        setUpdateMsg('云端正在更新，等待完成…');
        return pollUpdate(res.run.id, beforeEtag);
      }
      throw new Error(res.message || res.error || `HTTP ${r.status}`);
    }
    if (res.run && res.run.id) {
      setUpdateMsg('已提交更新任务，等待云端执行…');
      return pollUpdate(res.run.id, beforeEtag);
    }
    setUpdateMsg('已提交更新任务，请稍后刷新页面');
    refreshBtn.disabled = false;
    refreshBtn.textContent = '立即更新';
  } catch (e) {
    setUpdateMsg(`更新请求失败：${e.message || '网络错误'}，请稍后重试`);
    refreshBtn.disabled = false;
    refreshBtn.textContent = '立即更新';
  }
}
refreshBtn.addEventListener('click', startUpdate);
(() => {
  const HASH = 'b74369740a48edd315d66684cd22a301beb95d778f4bbf01ba6da5ab216d2238';
  const KEY = 'chip5d_ok';
  const gate = document.getElementById('gate');
  const pwd = document.getElementById('gatePwd');
  const btn = document.getElementById('gateBtn');
  const err = document.getElementById('gateErr');
  const hide = () => {
    gate.classList.add('hidden');
    document.body.style.overflow = '';
    setTimeout(() => document.getElementById('q')?.focus(), 30);
  };
  async function digest(s) {
    if (!window.crypto?.subtle) throw new Error('当前浏览器不支持安全校验，请用 https 打开');
    const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(s));
    return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, '0')).join('');
  }
  async function check() {
    err.textContent = '';
    const v = pwd.value;
    if (!v) { err.textContent = '请输入密码'; return; }
    try {
      const h = await digest(v);
      if (h === HASH) {
        try { sessionStorage.setItem(KEY, '1'); } catch (e) {}
        hide();
      } else {
        err.textContent = '密码错误';
        pwd.value = '';
        pwd.focus();
      }
    } catch (e) {
      err.textContent = e.message || '校验失败';
    }
  }
  let unlocked = false;
  try { unlocked = sessionStorage.getItem(KEY) === '1'; } catch (e) {}
  if (unlocked) { hide(); } else { document.body.style.overflow = 'hidden'; setTimeout(() => pwd.focus(), 60); }
  btn.addEventListener('click', check);
  pwd.addEventListener('keydown', e => { if (e.key === 'Enter') check(); });
})();
render();
</script>
</body>
</html>"""

    html = (html.replace("__HEAD_DATES__", head_dates)
            .replace("__CONCEPT__", concept_panel)
            .replace("__DATA__", data_json))
    OUT.write_text(html, encoding="utf-8")
    print(f"OK stocks={len(stocks)} pages={(len(stocks)+PAGE_SIZE-1)//PAGE_SIZE} out={OUT} size={OUT.stat().st_size/1024/1024:.2f}MB")
    print(f"missing={missing_count} streak2={streak2} flow2={flow2}")
    print("dates", dates)


if __name__ == "__main__":
    main()
