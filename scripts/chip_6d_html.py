# -*- coding: utf-8 -*-
"""
按 Chip_Distirbution_Metric 的移动成本模型，生成“每只股票连续6个交易日筹码分布”网页。

数据源：E:\\stockpool\\engine\\ingest\\data\\quant_v2.db -> quote_clean
说明：
- 每日 moved = min(换手率 × 衰减系数, 1)，默认 decay=1.0（本地库没有前十大股东数据）
- 新筹码按当日 [low, high] 三角分布叠加，峰值在当日成交均价
- 只输出最近6个交易日的分布和指标，但计算使用该股票在库内的全部历史
- 本地库多数股票只有约65个交易日，部分股票约505个交易日；不是IPO以来全历史
"""
import json
import math
import os
import sqlite3
from collections import defaultdict
from datetime import datetime
from pathlib import Path

BASE = Path(os.environ.get("XGRS_BASE", r"D:\ai\xgrs"))
DB = Path(os.environ.get("POOL_DB", r"E:\stockpool\engine\ingest\data\quant_v2.db"))
POOL_CSV = Path(os.environ.get("POOL_CSV", r"E:\stockpool\out\股票池.csv"))
OUT_DIR = Path(os.environ.get("OUT_DIR", str(BASE / "reports")))
OUT_HTML = Path(os.environ.get("OUT_HTML_6D", str(OUT_DIR / "每日筹码_股票池_连续6日.html")))
OUT_JSON = Path(os.environ.get("CHIP_JSON", str(OUT_DIR / "chip_6d_data.json")))

BINS = 72
DECAY = 1.0
MIN_ROWS = 20


def fmt_date(s):
    return f"{s[:4]}-{s[4:6]}-{s[6:8]}" if s and len(s) == 8 else s


def safe_float(v, default=None):
    try:
        if v is None:
            return default
        return float(v)
    except Exception:
        return default


def triangle_weights(low, high, avg, centers):
    """当日新筹码在当前价格网格上的三角分布，返回归一化权重。"""
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


def percentile_price(centers, weights, pct):
    total = sum(weights)
    if total <= 0:
        return 0.0
    target = total * pct / 100.0
    cum = 0.0
    prev_p, prev_cum = centers[0], 0.0
    for p, w in zip(centers, weights):
        new_cum = cum + w
        if new_cum >= target:
            if new_cum > prev_cum and p > prev_p:
                frac = (target - prev_cum) / (new_cum - prev_cum)
                return prev_p + frac * (p - prev_p)
            return p
        prev_p, prev_cum = p, new_cum
        cum = new_cum
    return centers[-1]


def chip_metrics(centers, weights, close):
    total = sum(weights)
    if total <= 0:
        return dict(avg_cost=0, profit=0, p90_low=0, p90_high=0, concentration_90=0,
                    p70_low=0, p70_high=0, concentration_70=0, peak_price=0, peak_weight=0)
    avg_cost = sum(p * w for p, w in zip(centers, weights)) / total
    profit = sum(w for p, w in zip(centers, weights) if p <= close) / total
    p5 = percentile_price(centers, weights, 5)
    p95 = percentile_price(centers, weights, 95)
    p15 = percentile_price(centers, weights, 15)
    p85 = percentile_price(centers, weights, 85)

    def conc(lo, hi):
        return (hi - lo) / (hi + lo) if (hi + lo) > 0 else 0.0

    # 局部峰值：比左右都高，且权重大于总量0.1%
    peaks = []
    for i in range(1, len(weights) - 1):
        if weights[i] >= total * 0.001 and weights[i] > weights[i - 1] and weights[i] > weights[i + 1]:
            peaks.append((centers[i], weights[i]))
    peaks.sort(key=lambda x: x[1], reverse=True)
    peak_price, peak_weight = peaks[0] if peaks else (max(zip(weights, centers))[1], max(weights))
    return dict(
        avg_cost=round(avg_cost, 3),
        profit=round(profit * 100, 2),
        p90_low=round(p5, 3), p90_high=round(p95, 3),
        concentration_90=round(conc(p5, p95) * 100, 2),
        p70_low=round(p15, 3), p70_high=round(p85, 3),
        concentration_70=round(conc(p15, p85) * 100, 2),
        peak_price=round(peak_price, 3),
        peak_weight=round(peak_weight / total * 100, 2),
    )


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(DB))
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    market_dates = [r[0] for r in cur.execute(
        "select distinct trade_date from quote_clean order by trade_date desc limit 6"
    )]
    market_dates = list(reversed(market_dates))
    if len(market_dates) < 6:
        raise SystemExit(f"交易日不足6天: {market_dates}")

    # 股票池名称、板块
    pool = {}
    for r in cur.execute("select code,name,board from pool_quotes"):
        pool[r["code"]] = (r["name"] or "", r["board"] or "")

    # 当前 391 只股票池（用于页面默认范围）
    pool_codes = set()
    if POOL_CSV.exists():
        import csv
        with POOL_CSV.open(encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                code = (row.get("代码") or "").strip()
                if code:
                    pool_codes.add(code)

    # 只取最近6日都有成交的股票，保证“连续6天”一致
    placeholders = ",".join("?" for _ in market_dates)
    codes = [r[0] for r in cur.execute(
        f"""select code from quote_clean
            where trade_date in ({placeholders})
            group by code having count(distinct trade_date)=6
            order by code""", market_dates
    )]

    rows_by_code = defaultdict(list)
    code_set = set(codes)
    for r in cur.execute(
        """select code,trade_date,open,high,low,close,volume,amount,
                  turnover_est,turnover_rate,circ_mv_est,circ_mv,is_st,is_bj
           from quote_clean
           order by code,trade_date"""
    ):
        if r["code"] in code_set:
            rows_by_code[r["code"]].append(r)
    con.close()

    stocks = []
    skipped = []
    for code in codes:
        rows = rows_by_code.get(code, [])
        if len(rows) < MIN_ROWS:
            skipped.append((code, "历史不足"))
            continue

        # 当前流通股本/换手率基准：优先最新 turnover_est，否则用 circ_mv/close
        latest = rows[-1]
        float_shares = None
        te = safe_float(latest["turnover_est"])
        vol = safe_float(latest["volume"])
        if te and te > 0 and vol and vol > 0:
            float_shares = vol * 100.0 / (te / 100.0)
        if not float_shares:
            cmv = safe_float(latest["circ_mv_est"]) or safe_float(latest["circ_mv"])
            close = safe_float(latest["close"])
            if cmv and close and close > 0:
                float_shares = cmv / close
        if not float_shares or float_shares <= 0:
            skipped.append((code, "缺流通股本"))
            continue

        # 价格网格：按该股全部历史高低价，72档
        lows = [safe_float(r["low"]) for r in rows if safe_float(r["low"]) is not None]
        highs = [safe_float(r["high"]) for r in rows if safe_float(r["high"]) is not None]
        if not lows or not highs:
            skipped.append((code, "缺价格"))
            continue
        min_p, max_p = min(lows), max(highs)
        if max_p <= min_p:
            max_p = min_p + 0.01
        step = max(0.01, (max_p - min_p) / (BINS - 1))
        centers = [min_p + step * (i + 0.5) for i in range(BINS)]

        chip = [0.0] * BINS
        moved_values = []
        valid_dates = []
        rec_by_date = {}
        dist_by_date = {}
        for r in rows:
            o = safe_float(r["open"]); h = safe_float(r["high"]); l = safe_float(r["low"])
            c = safe_float(r["close"]); v = safe_float(r["volume"]); amt = safe_float(r["amount"])
            if None in (o, h, l, c) or h <= 0 or l <= 0 or c <= 0:
                continue
            if v is None or v <= 0:
                # 停牌/无成交：不更新，但仍保留当日快照
                moved = 0.0
            else:
                turn = safe_float(r["turnover_est"])
                if turn and turn > 0:
                    # turnover_est 统一是百分数（如 3.12 表示 3.12%）
                    turn = turn / 100.0
                else:
                    turn = safe_float(r["turnover_rate"])
                    if turn and turn > 0:
                        # turnover_rate 兼容百分数/小数
                        turn = turn / 100.0 if turn > 1.5 else turn
                if not turn or turn <= 0:
                    turn = (v * 100.0) / float_shares
                moved = min(max(turn * DECAY, 0.0), 1.0)

            stay = 1.0 - moved
            chip = [w * stay for w in chip]
            if v and v > 0 and h > l:
                avg = (amt / (v * 100.0)) if amt and amt > 0 else (o + h + l + c) / 4.0
                daily = triangle_weights(l, h, avg, centers)
                for i, w in enumerate(daily):
                    chip[i] += w * moved
            total = sum(chip)
            if total <= 0:
                continue
            moved_values.append(moved)
            valid_dates.append(r["trade_date"])
            m = chip_metrics(centers, chip, c)
            m.update(dict(
                date=r["trade_date"], close=round(c, 3),
                moved=round(moved * 100, 2),
                new_vol_wan=round(v * DECAY / 100.0, 2) if v else 0.0,
                new_amt_yi=round((amt or 0) * DECAY / 1e8, 3),
                turnover=round((moved / DECAY) * 100, 2),
            ))
            rec_by_date[r["trade_date"]] = m
            if r["trade_date"] in market_dates:
                dist_by_date[r["trade_date"]] = [round(w / total, 4) for w in chip]

        # 6日都必须有快照
        if any(d not in rec_by_date for d in market_dates):
            skipped.append((code, "6日数据不全"))
            continue

        # 每个目标日相对前5个交易日的 moved 均值
        valid_pos = {d: i for i, d in enumerate(valid_dates)}
        days = []
        for d in market_dates:
            m = dict(rec_by_date[d])
            pos = valid_pos[d]
            prev = moved_values[max(0, pos - 5):pos]
            avg5 = sum(prev) / len(prev) if prev else 0.0
            ratio = (m["moved"] / 100.0) / avg5 if avg5 > 0 else None
            m["vs5"] = round(ratio, 3) if ratio is not None else None
            if ratio is None:
                m["signal"] = "无基准"
            elif ratio >= 1.2:
                m["signal"] = "增多"
            elif ratio <= 0.8:
                m["signal"] = "减少"
            else:
                m["signal"] = "平稳"
            days.append(m)

        if any(d not in dist_by_date for d in market_dates):
            skipped.append((code, "分布数据不全"))
            continue

        name, board = pool.get(code, ("", ""))
        if not name:
            name = next((r["name"] for r in reversed(rows) if r["name"]), code)
        latest_m = days[-1]
        stocks.append(dict(
            code=code,
            name=name or code,
            board=board,
            in_pool=code in pool_codes,
            first_date=fmt_date(rows[0]["trade_date"]),
            last_date=fmt_date(rows[-1]["trade_date"]),
            history_rows=len(rows),
            latest_close=latest_m["close"],
            latest_moved=latest_m["moved"],
            latest_vs5=latest_m["vs5"],
            latest_signal=latest_m["signal"],
            latest_new_vol_wan=latest_m["new_vol_wan"],
            latest_new_amt_yi=latest_m["new_amt_yi"],
            latest_profit=latest_m["profit"],
            latest_avg_cost=latest_m["avg_cost"],
            latest_concentration=latest_m["concentration_90"],
            bins=[round(p, 3) for p in centers],
            days=days,
            dists=[dist_by_date[d] for d in market_dates],
        ))

    # 按今日新进筹码放大倍数排序，增多在前
    stocks.sort(key=lambda s: (-(s["latest_vs5"] or -1), s["code"]))
    data = dict(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        dates=[fmt_date(d) for d in market_dates],
        raw_dates=market_dates,
        bin_count=BINS,
        decay=DECAY,
        total_stocks=len(stocks),
        pool_codes=len(pool_codes),
        pool_stocks=sum(1 for s in stocks if s.get("in_pool")),
        skipped=len(skipped),
        stocks=stocks,
    )
    OUT_JSON.write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    html = build_html(data)
    OUT_HTML.write_text(html, encoding="utf-8")
    print(f"OK stocks={len(stocks)} skipped={len(skipped)} html={OUT_HTML} size={OUT_HTML.stat().st_size/1024/1024:.2f}MB json={OUT_JSON.stat().st_size/1024/1024:.2f}MB")
    print("dates", data["dates"])
    print("top", [(s["code"], s["name"], s["latest_vs5"], s["latest_signal"], s["latest_moved"]) for s in stocks[:8]])


def build_html(data):
    data_json = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>每日筹码 · 连续6日</title>
<style>
:root {{
  --bg:#0d1016; --panel:#141922; --panel2:#181f2a; --line:#2a3341;
  --text:#d9e0ea; --muted:#8591a3; --weak:#5d6878;
  --accent:#8fb3ff; --gold:#d9a85f; --green:#6fbf8f; --red:#d97b7b;
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:14px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",sans-serif; }}
header {{ padding:22px 28px 14px; border-bottom:1px solid var(--line); display:flex; gap:24px; align-items:flex-end; flex-wrap:wrap; }}
h1 {{ margin:0; font-size:26px; letter-spacing:.5px; font-weight:650; }}
.sub {{ color:var(--muted); font-size:12.5px; max-width:820px; }}
.stats {{ margin-left:auto; display:flex; gap:18px; color:var(--muted); font-size:12px; }}
.stats b {{ display:block; color:var(--text); font-size:18px; font-weight:600; }}
.layout {{ display:grid; grid-template-columns:290px minmax(0,1fr); min-height:calc(100vh - 92px); }}
aside {{ border-right:1px solid var(--line); padding:14px 12px 20px; position:sticky; top:0; height:calc(100vh - 92px); overflow:auto; }}
.controls {{ display:grid; gap:8px; margin-bottom:10px; }}
input,select {{ width:100%; background:#10151d; color:var(--text); border:1px solid var(--line); border-radius:6px; padding:8px 9px; outline:none; }}
input:focus,select:focus {{ border-color:#4b6288; }}
.count {{ color:var(--weak); font-size:12px; margin:6px 2px 8px; }}
.list {{ display:flex; flex-direction:column; gap:2px; }}
.item {{ border:0; background:transparent; color:var(--text); text-align:left; padding:8px 9px; border-radius:6px; cursor:pointer; display:grid; grid-template-columns:1fr auto; gap:8px; }}
.item:hover {{ background:#18202c; }}
.item.active {{ background:#1c2635; box-shadow:inset 2px 0 0 var(--accent); }}
.item .nm {{ font-weight:550; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
.item .cd {{ color:var(--weak); font-size:11px; font-variant-numeric:tabular-nums; }}
.badge {{ font-size:11px; padding:1px 6px; border-radius:99px; border:1px solid var(--line); color:var(--muted); white-space:nowrap; }}
.badge.增多 {{ color:var(--green); border-color:#315a45; }}
.badge.减少 {{ color:var(--red); border-color:#603838; }}
.badge.平稳 {{ color:var(--muted); }}
main {{ padding:20px 26px 40px; overflow:hidden; }}
.headline {{ display:flex; gap:18px; align-items:baseline; flex-wrap:wrap; margin-bottom:16px; }}
.headline h2 {{ margin:0; font-size:24px; font-weight:650; }}
.headline .code {{ color:var(--muted); font-variant-numeric:tabular-nums; }}
.headline .sig {{ font-size:12px; padding:3px 8px; border:1px solid var(--line); border-radius:4px; }}
.summary {{ display:grid; grid-template-columns:repeat(5,minmax(110px,1fr)); gap:10px; margin-bottom:18px; }}
.kpi {{ background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:11px 13px; }}
.kpi .k {{ color:var(--muted); font-size:12px; }}
.kpi .v {{ font-size:20px; font-weight:600; margin-top:2px; font-variant-numeric:tabular-nums; }}
.kpi .n {{ color:var(--weak); font-size:11px; margin-top:2px; }}
.grid {{ display:grid; grid-template-columns:repeat(3,minmax(0,1fr)); gap:12px; }}
.card {{ background:var(--panel); border:1px solid var(--line); border-radius:9px; padding:12px 13px 10px; min-width:0; }}
.card .top {{ display:flex; justify-content:space-between; align-items:baseline; gap:8px; }}
.card .d {{ font-weight:600; font-variant-numeric:tabular-nums; }}
.card .s {{ font-size:11px; }}
.card .m {{ display:flex; gap:12px; color:var(--muted); font-size:11.5px; margin:4px 0 4px; flex-wrap:wrap; }}
.card .m b {{ color:var(--text); font-weight:550; }}
.chart {{ width:100%; height:132px; display:block; }}
.axis {{ display:flex; justify-content:space-between; color:var(--weak); font-size:10px; margin-top:-2px; }}
.legend {{ display:flex; gap:12px; color:var(--weak); font-size:10.5px; margin-top:4px; }}
.legend i {{ display:inline-block; width:16px; height:2px; vertical-align:middle; margin-right:3px; }}
table {{ width:100%; border-collapse:collapse; margin-top:16px; font-size:12px; }}
th,td {{ border-bottom:1px solid var(--line); padding:7px 8px; text-align:right; font-variant-numeric:tabular-nums; }}
th {{ color:var(--muted); font-weight:500; }}
th:first-child,td:first-child {{ text-align:left; }}
.note {{ color:var(--weak); font-size:11.5px; margin-top:14px; line-height:1.7; }}
.empty {{ color:var(--muted); padding:40px 0; }}
@media (max-width:1100px) {{ .layout {{ grid-template-columns:240px minmax(0,1fr); }} .grid {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} .summary {{ grid-template-columns:repeat(3,minmax(0,1fr)); }} }}
@media (max-width:760px) {{ .layout {{ display:block; }} aside {{ position:relative; height:auto; border-right:0; border-bottom:1px solid var(--line); }} .grid {{ grid-template-columns:1fr; }} .summary {{ grid-template-columns:repeat(2,minmax(0,1fr)); }} header {{ padding:18px 16px 12px; }} main {{ padding:16px; }} }}
</style>
</head>
<body>
<header>
  <div>
    <h1>每日筹码 · 连续6日</h1>
    <div class="sub">按 Chip_Distirbution_Metric 的移动成本模型递推：昨日筹码衰减 + 当日新成交筹码按 [最低, 最高] 三角分布叠加。数值为模型推算，不是交易所真实持仓。</div>
  </div>
  <div class="stats">
    <div><b id="stStocks">{data["pool_stocks"]}</b>只股票池 · 全库 {data["total_stocks"]}</div>
    <div><b id="stDays">6</b>个交易日</div>
    <div><b id="stDates">{data["dates"][0]} ~ {data["dates"][-1]}</b>区间</div>
  </div>
</header>
<div class="layout">
  <aside>
    <div class="controls">
      <input id="q" placeholder="搜索代码 / 名称">
      <select id="u">
        <option value="pool">当前股票池（{data["pool_stocks"]}只）</option>
        <option value="all">全部行情库（{data["total_stocks"]}只）</option>
      </select>
      <select id="f">
        <option value="all">全部信号</option>
        <option value="增多">今日新进筹码增多</option>
        <option value="平稳">平稳</option>
        <option value="减少">减少</option>
      </select>
      <select id="sort">
        <option value="vs5">按今日新进放大倍数</option>
        <option value="moved">按今日新进比例</option>
        <option value="vol">按今日新进筹码量</option>
        <option value="code">按代码</option>
      </select>
    </div>
    <div class="count" id="count"></div>
    <div class="list" id="list"></div>
  </aside>
  <main>
    <div id="detail"></div>
    <div class="note">
      口径：moved = min(换手率 × 衰减系数, 1)，本页 decay=1.0（本地库无前十大股东修正数据）；今日新进筹码量约 = 成交量 × decay，按“万股”展示；今日新进筹码额约 = 成交额 × decay。与5日均值比较，≥1.2 倍标“增多”，≤0.8 倍标“减少”。<br>
      数据：{data["dates"][0]} 至 {data["dates"][-1]}，本地 quote_clean；默认展示当前股票池 387 只（391 只中 4 只缺连续6日行情），可切换全部行情库 {data["total_stocks"]} 只。价格按未复权价计算，若区间内有除权，成本线会有偏差；每只股票使用库内全部历史递推，但多数股票只有约65个交易日，仅141只约505个交易日，起点并非IPO，早期筹码可能缺失。生成时间：{data["generated_at"]}。
    </div>
  </main>
</div>
<script id="payload" type="application/json">{data_json}</script>
<script>
const DATA = JSON.parse(document.getElementById('payload').textContent);
const stocks = DATA.stocks;
const state = {{ q:'', u:'pool', f:'all', sort:'vs5', selected: (stocks.find(s => s.in_pool) || stocks[0] || {{}}).code }};
const $ = id => document.getElementById(id);

function esc(s) {{ return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c])); }}
function fmt(n, d=2) {{ return n == null || Number.isNaN(n) ? '—' : Number(n).toFixed(d); }}
function pct(n) {{ return n == null ? '—' : Number(n).toFixed(2) + '%'; }}
function sigClass(s) {{ return s === '增多' ? '增多' : s === '减少' ? '减少' : '平稳'; }}

function filtered() {{
  let arr = stocks.filter(s => {{
    if (state.u === 'pool' && !s.in_pool) return false;
    if (state.f !== 'all' && s.latest_signal !== state.f) return false;
    if (state.q) {{
      const q = state.q.toLowerCase();
      if (!(s.code.toLowerCase().includes(q) || (s.name||'').toLowerCase().includes(q))) return false;
    }}
    return true;
  }});
  if (state.sort === 'vs5') arr.sort((a,b) => (b.latest_vs5 ?? -1) - (a.latest_vs5 ?? -1) || a.code.localeCompare(b.code));
  else if (state.sort === 'moved') arr.sort((a,b) => (b.latest_moved ?? -1) - (a.latest_moved ?? -1));
  else if (state.sort === 'vol') arr.sort((a,b) => (b.latest_new_vol_wan ?? -1) - (a.latest_new_vol_wan ?? -1));
  else arr.sort((a,b) => a.code.localeCompare(b.code));
  return arr;
}}

function renderList() {{
  const arr = filtered();
  $('count').textContent = `显示 ${{arr.length}} / ${{state.u === 'pool' ? DATA.pool_stocks : stocks.length}} 只`;
  const limit = state.q ? 400 : 220;
  $('list').innerHTML = arr.slice(0, limit).map(s => `
    <button class="item ${{s.code===state.selected?'active':''}}" data-code="${{s.code}}">
      <span><span class="nm">${{esc(s.name)}}</span><span class="cd">${{s.code}} · ${{fmt(s.latest_close,2)}}</span></span>
      <span class="badge ${{sigClass(s.latest_signal)}}">${{s.latest_signal}}</span>
    </button>`).join('') + (arr.length > limit ? `<div class="count">仅显示前 ${{limit}} 条，搜索可定位其余股票</div>` : '');
  document.querySelectorAll('.item').forEach(b => b.onclick = () => {{ state.selected = b.dataset.code; renderList(); renderDetail(); }});
}}

function chipSvg(s, i) {{
  const bins = s.bins, dist = s.dists[i], day = s.days[i];
  const w = 360, h = 132, padL = 8, padR = 8, padT = 12, padB = 18;
  const max = Math.max(...dist) || 1;
  const minP = bins[0], maxP = bins[bins.length-1];
  const x = p => padL + (p - minP) / (maxP - minP || 1) * (w - padL - padR);
  const y = v => padT + (h - padT - padB) - (v / max) * (h - padT - padB);
  let pts = dist.map((v, j) => `${{x(bins[j]).toFixed(1)}},${{y(v).toFixed(1)}}`).join(' ');
  const closeX = x(day.close), avgX = x(day.avg_cost);
  return `<svg class="chart" viewBox="0 0 ${{w}} ${{h}}" preserveAspectRatio="none">
    <line x1="${{padL}}" y1="${{h-padB}}" x2="${{w-padR}}" y2="${{h-padB}}" stroke="#2a3341" stroke-width="1"/>
    <polygon points="${{padL}},${{h-padB}} ${{pts}} ${{w-padR}},${{h-padB}}" fill="rgba(143,179,255,.16)" stroke="none"/>
    <polyline points="${{pts}}" fill="none" stroke="#8fb3ff" stroke-width="1.2"/>
    <line x1="${{closeX.toFixed(1)}}" y1="${{padT}}" x2="${{closeX.toFixed(1)}}" y2="${{h-padB}}" stroke="#d9a85f" stroke-width="1" stroke-dasharray="3 3"/>
    <line x1="${{avgX.toFixed(1)}}" y1="${{padT}}" x2="${{avgX.toFixed(1)}}" y2="${{h-padB}}" stroke="#8591a3" stroke-width="1"/>
  </svg>
  <div class="axis"><span>${{fmt(minP,2)}}</span><span>收盘 ${{fmt(day.close,2)}} · 成本 ${{fmt(day.avg_cost,2)}}</span><span>${{fmt(maxP,2)}}</span></div>`;
}}

function renderDetail() {{
  const s = stocks.find(x => x.code === state.selected);
  if (!s) {{ $('detail').innerHTML = '<div class="empty">没有匹配的股票</div>'; return; }}
  const sig = s.latest_signal;
  const vs = s.latest_vs5 == null ? '—' : fmt(s.latest_vs5,2) + 'x';
  $('detail').innerHTML = `
    <div class="headline">
      <h2>${{esc(s.name)}}</h2><span class="code">${{s.code}} · ${{esc(s.board)}}</span>
      <span class="sig badge ${{sigClass(sig)}}">${{sig}}</span>
    </div>
    <div class="summary">
      <div class="kpi"><div class="k">最新收盘</div><div class="v">${{fmt(s.latest_close,2)}}</div><div class="n">${{DATA.dates[DATA.dates.length-1]}}</div></div>
      <div class="kpi"><div class="k">今日新进筹码</div><div class="v">${{fmt(s.latest_new_vol_wan,1)}}</div><div class="n">万股 · 约 ${{fmt(s.latest_new_amt_yi,2)}} 亿元</div></div>
      <div class="kpi"><div class="k">今日换手筹码</div><div class="v">${{pct(s.latest_moved)}}</div><div class="n">较5日均值 ${{vs}}</div></div>
      <div class="kpi"><div class="k">平均成本</div><div class="v">${{fmt(s.latest_avg_cost,2)}}</div><div class="n">获利盘 ${{pct(s.latest_profit)}}</div></div>
      <div class="kpi"><div class="k">90%集中度</div><div class="v">${{pct(s.latest_concentration)}}</div><div class="n">越小越集中</div></div>
    </div>
    <div class="grid">${{s.days.map((d,i) => `
      <div class="card">
        <div class="top"><span class="d">${{DATA.dates[i]}}</span><span class="badge ${{sigClass(d.signal)}}">${{d.signal}}</span></div>
        <div class="m"><span>新进 <b>${{fmt(d.new_vol_wan,1)}}</b> 万股</span><span>换手 <b>${{pct(d.moved)}}</b></span><span>较5日 <b>${{d.vs5==null?'—':fmt(d.vs5,2)+'x'}}</b></span></div>
        ${{chipSvg(s,i)}}
        <div class="legend"><span><i style="background:#8fb3ff"></i>筹码分布</span><span><i style="background:#d9a85f"></i>收盘</span><span><i style="background:#8591a3"></i>平均成本</span></div>
      </div>`).join('')}}</div>
    <table>
      <thead><tr><th>日期</th><th>收盘</th><th>新进筹码(万股)</th><th>新进筹码额(亿元)</th><th>换手筹码</th><th>较5日均值</th><th>平均成本</th><th>获利盘</th><th>90%集中度</th><th>信号</th></tr></thead>
      <tbody>${{s.days.map((d,i) => `<tr>
        <td>${{DATA.dates[i]}}</td><td>${{fmt(d.close,2)}}</td><td>${{fmt(d.new_vol_wan,1)}}</td><td>${{fmt(d.new_amt_yi,2)}}</td>
        <td>${{pct(d.moved)}}</td><td>${{d.vs5==null?'—':fmt(d.vs5,2)+'x'}}</td><td>${{fmt(d.avg_cost,2)}}</td>
        <td>${{pct(d.profit)}}</td><td>${{pct(d.concentration_90)}}</td><td>${{d.signal}}</td></tr>`).join('')}}</tbody>
    </table>`;
}}

$('q').addEventListener('input', e => {{ state.q = e.target.value.trim(); renderList(); }});
$('u').addEventListener('change', e => {{ state.u = e.target.value; renderList(); }});
$('f').addEventListener('change', e => {{ state.f = e.target.value; renderList(); }});
$('sort').addEventListener('change', e => {{ state.sort = e.target.value; renderList(); }});
$('stStocks').textContent = DATA.pool_stocks;
$('stDates').textContent = DATA.dates[0] + ' ~ ' + DATA.dates[DATA.dates.length-1];
renderList();
renderDetail();
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()
