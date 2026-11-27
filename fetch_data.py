"""fetch_data.py — 阶段1：拉取同花顺涨停池/炸板池/跌停池，规范化、概念聚合、RPS、输出结构化文件。

不做打分、不做选股。所有阈值/接口参数来自 config.yaml。

  python fetch_data.py --date 2026-11-26
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

import common

CST = timezone(timedelta(hours=8))
REFERER = "https://data.10jqka.com.cn/datacenterph/limitup/limtupInfo.html"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
API = "https://data.10jqka.com.cn/dataapi/limit_up/{path}"
KLINE_URL = "https://d.10jqka.com.cn/v6/line/hs_{code}/01/last.js"
F10_URL = "http://basic.10jqka.com.cn/{code}/concept.html"
# 阶段0 实测同一参数，不改
FIELDS = ("199112,10,9001,330323,330324,330325,9002,330329,133971,133970,1968584,3475914,9003,9004,"
          "330343,330344,330345,330346,330347,330348,3541450,3551450")

_STATS = {"kline": {"hit": 0, "miss": 0, "fail": 0, "stale": 0, "today_missing": 0},
          "f10": {"hit": 0, "miss": 0, "fail": 0}}

# 重拉后仍没有当日行的股票代码（阶段3 起用于标记 kline_today_missing）
_KLINE_MISSING_TODAY: set[str] = set()

_THROTTLE_STATE = {"last": 0.0}


def throttle() -> None:
    interval = float(common.cfg()["request_throttle"].get("ths_interval", 2))
    wait = interval - (time.monotonic() - _THROTTLE_STATE["last"])
    if _THROTTLE_STATE["last"] and wait > 0:
        time.sleep(wait)
    _THROTTLE_STATE["last"] = time.monotonic()


class FetchError(RuntimeError):
    pass


def ths_get(path: str, date: str, page: int = 1, limit: int = 200) -> tuple[dict, float]:
    """带节流 + 指数退避重试的同花顺 dataapi GET。"""
    cfg = common.cfg()["fetch_retry"]
    params = {"page": page, "limit": limit, "field": FIELDS, "filter": "HS,GEM2STAR",
              "order_field": "330324", "order_type": 0, "date": date.replace("-", "")}
    last_err = None
    for attempt in range(int(cfg["max_retries"])):
        throttle()
        t0 = time.perf_counter()
        try:
            r = requests.get(API.format(path=path), params=params,
                             headers={"User-Agent": UA, "Referer": REFERER,
                                      "Accept": "application/json, text/plain, */*"}, timeout=20)
            r.raise_for_status()
            body = r.json()
            if body.get("status_code") != 0:
                raise FetchError(f"status_code={body.get('status_code')} status_msg={body.get('status_msg')} 完整返回={r.text[:400]}")
            return body, round(time.perf_counter() - t0, 2)
        except Exception as exc:  # noqa: BLE001  按规格只重试，不换源
            last_err = exc
            delay = min(int(cfg["base_delay"]) * (2 ** attempt), int(cfg["max_delay"]))
            print(f"  [重试] {path} 第 {attempt + 1} 次失败：{exc}；{delay}s 后重试")
            time.sleep(delay)
    raise FetchError(f"{path} 连续 {cfg['max_retries']} 次失败：{last_err}")


def pull_pool(path: str, date: str, log) -> dict:
    """分页拉取，返回 {rows, total, pages, elapsed, data_timestamp}。"""
    rows, page, total, elapsed = [], 1, None, 0.0
    while True:
        body, secs = ths_get(path, date, page=page, limit=200)
        elapsed += secs
        data = body.get("data", {})
        info = data.get("info", []) or []
        total = (data.get("page") or {}).get("total", len(info))
        rows.extend(info)
        if len(info) < 200 or len(rows) >= (total or 0):
            break
        page += 1
        if page > 20:
            log.warning("%s 翻页超过 20 页，停止", path)
            break
    trade_status = (body.get("data") or {}).get("trade_status") or {}
    data_ts = f"{date} {trade_status.get('name', '')}".strip()
    return {"rows": rows, "total": total, "pages": page, "elapsed": round(elapsed, 2),
            "data_timestamp": data_ts, "limit_up_count": (body.get("data") or {}).get("limit_up_count")}


# ---------------------------------------------------------------- 规范化

def _f(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_high_days(raw: str) -> tuple[dict, bool, bool]:
    """'首板' / '8天7板' / '4连板' → {days, boards}；返回 (结构, 解析失败, 缺失)。"""
    if raw is None or str(raw).strip() == "":
        return {"days": 1, "boards": 1}, False, True
    s = str(raw).strip()
    if s in ("首板", "首板涨停"):
        return {"days": 1, "boards": 1}, False, False
    m = re.match(r"^(\d+)天(\d+)板$", s)
    if m:
        return {"days": int(m.group(1)), "boards": int(m.group(2))}, False, False
    m = re.match(r"^(\d+)连板$", s)
    if m:
        n = int(m.group(1))
        return {"days": n, "boards": n}, False, False
    m = re.match(r"^(\d+)板$", s)
    if m:
        n = int(m.group(1))
        return {"days": n, "boards": n}, False, False
    return {"days": 1, "boards": 1}, True, False


def ts_to_minutes(ts) -> tuple[int | None, str | None, bool]:
    """Unix 时间戳 → (距 09:30 分钟数(含午休), 'HH:MM', 是否竞价封板)；失败 (None, None, False)。"""
    try:
        t = int(float(ts))
    except (TypeError, ValueError):
        return None, None, False
    dt = datetime.fromtimestamp(t, CST)
    hhmm = dt.strftime("%H:%M")
    mins = (dt.hour * 60 + dt.minute) - (9 * 60 + 30)
    if mins < 0:
        # 09:15-09:30 的集合竞价封板，按 0 分钟计，不标不确定
        if dt.hour == 9 and dt.minute >= 15:
            return 0, hhmm, True
        return None, hhmm, False
    return int(mins), hhmm, False


def load_mapping() -> dict:
    return common.read_json(common.root_path("concept_mapping_path"), {}) or {}


def build_alias_index(mapping: dict) -> dict:
    """标签/别名/概念名 → concept_id（大小写与空格归一）。"""
    idx = {}
    for cid, c in (mapping.get("concepts") or {}).items():
        for key in [c.get("name", "")] + list(c.get("alias") or []) + list(c.get("tags") or []) \
                + list((c.get("tag_priority") or {}).keys()):
            if key:
                idx[str(key).replace(" ", "").lower()] = cid
    return idx


def match_concept(reason_raw: str, mapping: dict, alias_idx: dict) -> tuple[str | None, str | None, int | None]:
    """返回 (concept_id, 主标签, 优先级)。复合标签按 '+'-join 组合尝试。"""
    if not reason_raw:
        return None, None, None
    tags = [t.strip() for t in str(reason_raw).split("+") if t.strip()]
    candidates = [str(reason_raw).strip()] + tags
    for i in range(len(tags) - 1):
        candidates.append(tags[i] + "+" + tags[i + 1])
    best = None  # (priority, concept_id, tag)
    for cand in candidates:
        key = cand.replace(" ", "").lower()
        cid = alias_idx.get(key)
        if not cid:
            continue
        prio_map = (mapping["concepts"][cid].get("tag_priority") or {})
        prio = prio_map.get(cand)
        if prio is None:
            prio = min(prio_map.values()) if prio_map else 99
        if best is None or prio < best[0]:
            best = (int(prio), cid, cand)
    return (best[1], best[2], best[0]) if best else (None, None, None)


def normalize_stocks(rows: list[dict], kind: str, mapping: dict, alias_idx: dict,
                     unmapped: list[str]) -> list[dict]:
    out = []
    for row in rows:
        code = str(row.get("code", "")).zfill(6)
        amount_raw = _f(row.get("order_amount"))
        vol = _f(row.get("order_volume"))
        latest = _f(row.get("latest"))
        order_amount, unit_uncertain = amount_raw, False
        if amount_raw and amount_raw > 0:
            if vol and latest:
                ratio = amount_raw / (vol * latest)  # order_volume 与 latest 都已验证为「股 / 元」
                if ratio < 0.01:  # 万元口径
                    order_amount = amount_raw * 1e4
                    unit_uncertain = True
            elif amount_raw < 1e6:
                order_amount = amount_raw * 1e4  # 无法交叉核验，按任务四量级规则
                unit_uncertain = True
        turnover_raw = _f(row.get("turnover_rate"))
        change_raw = _f(row.get("change_rate"))
        turnover = None if turnover_raw is None else round(turnover_raw / 100, 6)
        change = None if change_raw is None else round(change_raw / 100, 6)
        first_min, first_hm, first_auction = ts_to_minutes(row.get("first_limit_up_time"))
        last_min, last_hm, _ = ts_to_minutes(row.get("last_limit_up_time"))
        hd, parse_err, hd_missing = parse_high_days(row.get("high_days"))
        cid, main_tag, prio = match_concept(row.get("reason_type"), mapping, alias_idx)
        tags = [t.strip() for t in str(row.get("reason_type") or "").split("+") if t.strip()]
        if not cid:
            for t in tags:
                unmapped.append(t)
        item = {
            "code": code,
            "name": row.get("name", ""),
            "high_days_raw": row.get("high_days"),
            "reason_type_raw": row.get("reason_type"),
            "order_amount": order_amount,
            "order_volume": _f(row.get("order_volume")),
            "open_num": int(row["open_num"]) if row.get("open_num") is not None else None,
            "first_limit_up_time_ts": row.get("first_limit_up_time"),
            "last_limit_up_time_ts": row.get("last_limit_up_time"),
            "turnover_rate": turnover,
            "currency_value": _f(row.get("currency_value")),
            "limit_up_suc_rate": _f(row.get("limit_up_suc_rate")),
            "limit_up_type": row.get("limit_up_type"),
            "is_new": int(row.get("is_new") or 0),
            "is_again_limit": int(row.get("is_again_limit") or 0),
            "change_rate": change,
            "change_tag": row.get("change_tag"),
            "market_type": row.get("market_type"),
            "sum_market_value": _f(row.get("sum_market_value")),
            "latest": _f(row.get("latest")),
            "days": hd["days"],
            "boards": hd["boards"],
            "first_seal_minutes": first_min,
            "first_seal_hhmm": first_hm,
            "first_seal_auction": first_auction,
            "last_seal_minutes": last_min,
            "last_seal_hhmm": last_hm,
            "concept_id": cid,
            "main_tag": main_tag,
            "tag_priority": prio,
            "all_tags": tags,
            "unit_uncertain": unit_uncertain,
            "first_seal_time_uncertain": first_min is None,
            "high_days_parse_error": parse_err,
            "high_days_missing": hd_missing,
            "concept_unmatched": cid is None,
            "one_word_board": (turnover is not None and turnover < 0.01 and (row.get("open_num") or 0) == 0),
            "is_limit_up": kind == "limit_up",
            "is_zhaban": kind == "zhaban",
            "is_limit_down": kind == "limit_down",
        }
        out.append(item)
    return out


# ---------------------------------------------------------------- 概念聚合

def board_bucket(boards: int) -> str:
    if boards <= 1:
        return "first_board"
    if boards == 2:
        return "second_board"
    if boards == 3:
        return "third_board"
    return "higher"


def backfill_zhaban(stocks: list[dict], mapping: dict, alias_idx: dict, date: str, log) -> dict:
    """炸板池 reason_type 缺失时的同源补全：① 涨停池同码 ② F10 概念（clid / 名称）。

    只做同源补全，不改动任何原始字段；每条都留 zhaban_source 痕迹。
    """
    lu_by_code = {s["code"]: s for s in stocks if s["is_limit_up"]}
    clid_index = {}
    for cid, c in (mapping.get("concepts") or {}).items():
        ic = str(c.get("index_code") or "")
        if ic.isdigit():
            clid_index[ic] = cid
    stat = {"limit_up": 0, "f10": 0, "tag_match": 0, "still_missing": 0}
    for s in stocks:
        if not s["is_zhaban"]:
            continue
        if s.get("concept_id"):
            s["zhaban_source"] = "tag_match"
            stat["tag_match"] += 1
            continue
        src = lu_by_code.get(s["code"])
        if src and src.get("concept_id"):
            s["concept_id"] = src["concept_id"]
            s["main_tag"] = src.get("main_tag")
            s["tag_priority"] = src.get("tag_priority")
            s["all_tags"] = list(src.get("all_tags") or [])
            s["concept_unmatched"] = False
            s["zhaban_source"] = "backfilled_limit_up"
            stat["limit_up"] += 1
            continue
        rows = fetch_f10_concepts(s["code"], date)
        cid, main_tag, prio, row_name = None, None, None, None
        for row in rows:
            if row["clid"] in clid_index:
                cid, main_tag, row_name = clid_index[row["clid"]], row["name"], row["name"]
                break
        if cid is None:
            for row in rows:
                got_cid, got_tag, got_prio = match_concept(row["name"], mapping, alias_idx)
                if got_cid:
                    cid, main_tag, prio, row_name = got_cid, got_tag, got_prio, row["name"]
                    break
        if cid:
            s["concept_id"] = cid
            s["main_tag"] = row_name or main_tag
            s["tag_priority"] = prio
            s["all_tags"] = [r["name"] for r in rows]
            s["f10_concepts"] = rows
            s["concept_unmatched"] = False
            s["zhaban_source"] = "backfilled_f10"
            stat["f10"] += 1
        else:
            s["f10_concepts"] = rows
            s["zhaban_source"] = "missing_reason_type"
            stat["still_missing"] += 1
    if stat["limit_up"] or stat["f10"] or stat["still_missing"]:
        print(f"  炸板 reason_type 回填：涨停池同码 {stat['limit_up']} 条 / F10 {stat['f10']} 条；"
              f"仍缺 {stat['still_missing']} 条")
    return stat


def aggregate_concepts(stocks: list[dict], mapping: dict, still_missing: int = 0) -> list[dict]:
    concepts = []
    any_zb_mapped = any(s.get("concept_id") for s in stocks if s["is_zhaban"])
    total_zb = sum(1 for s in stocks if s["is_zhaban"])
    for cid, c in (mapping.get("concepts") or {}).items():
        lu = [s for s in stocks if s["concept_id"] == cid and s["is_limit_up"]]
        zb = [s for s in stocks if s["concept_id"] == cid and s["is_zhaban"]]
        dist = {"first_board": 0, "second_board": 0, "third_board": 0, "higher": 0}
        for s in lu:
            dist[board_bucket(s["boards"])] += 1
        uniq = len({s["code"] for s in lu})
        highest = max([s["boards"] for s in lu], default=0)
        mid_ratio = None
        if dist["first_board"] > 0:
            mid_ratio = round((dist["second_board"] + dist["third_board"]) / dist["first_board"], 4)
        if not any_zb_mapped:
            zhaban_n = None
            zhaban_rate = None
            zhaban_source = "missing_reason_type"
        else:
            zhaban_n = len({s["code"] for s in zb})
            denom = uniq + zhaban_n
            zhaban_rate = round(zhaban_n / denom, 4) if denom > 0 else None
            if not zb:
                zhaban_source = "none_matched"
            elif any(str(s.get("zhaban_source", "")).startswith("backfilled") for s in zb):
                zhaban_source = "backfilled"
            else:
                zhaban_source = "tag_match"
        zhaban_incomplete = bool(any_zb_mapped) and still_missing > 0
        if zhaban_incomplete:
            zhaban_source = zhaban_source + "_partial"
        zj = sorted([s for s in lu], key=lambda x: (x["currency_value"] or 0), reverse=True)[:3]
        concepts.append({
            "concept_id": cid,
            "name": c.get("name", ""),
            "index_code": c.get("index_code"),
            "unique_stock_count": uniq,
            "limit_up_count": len(lu),
            "board_level_distribution": dist,
            "highest_board": highest,
            "mid_ratio": mid_ratio,
            "zhaban_count": zhaban_n,
            "zhaban_rate": zhaban_rate,
            "zhaban_source": zhaban_source,
            "zhaban_coverage": None if not any_zb_mapped else round(
                (total_zb - still_missing) / total_zb, 4),
            "zhaban_rate_lower_bound": zhaban_incomplete,
            "zhengjun_candidates": [s["code"] for s in zj],
            "limit_up_stock_codes": [s["code"] for s in lu],
            "one_word_board_count": sum(1 for s in lu if s["one_word_board"]),
        })
    return concepts


# ---------------------------------------------------------------- RPS

def cache_dir(kind: str, date: str) -> Path:
    """按日期分目录的本地缓存（命中即读，未命中才联网）。"""
    p = common.ROOT / "data" / "cache" / kind / date
    p.mkdir(parents=True, exist_ok=True)
    return p


def _parse_kline_text(text: str) -> tuple[list[str], list[float]]:
    """解析同花顺 last.js：返回 (dates[YYYYMMDD], closes)。"""
    body = text[text.index("(") + 1:text.rindex(")")]
    d = json.loads(body)
    rows = [x for x in str(d.get("data", "")).split(";") if x]
    dates, closes = [], []
    for row in rows:
        parts = row.split(",")
        if len(parts) < 5:
            continue
        dates.append(parts[0])
        closes.append(float(parts[4]))
    return dates, closes


def _trim_to_date(dates: list[str], closes: list[float], need_date: str) -> tuple[list[str], list[float]]:
    """裁掉 need_date 之后的行。接口返回的是「最近一段」到当前自然日，
    复盘历史交易日时必须裁到当日，否则 closes[-1] 会是下一交易日、RPS 被污染。"""
    keep = [i for i, d in enumerate(dates) if d <= need_date]
    if not keep:
        return [], []
    return dates[:keep[-1] + 1], closes[:keep[-1] + 1]


def fetch_kline(code: str, date: str, need_date: str | None = None) -> tuple[list[str], list[float]] | None:
    """同花顺个股/板块日 K（last.js），返回 (dates, closes)；带日期级缓存。

    need_date=YYYYMMDD 时额外做两件事（阶段3 修「K线缓存跨日不齐」）：
    1) 结果裁剪到 need_date（含），避免读到下一交易日的行；
    2) 校验缓存里是否存在当日行，不存在视为跨日不齐 → 重拉；
       重拉后仍不存在 → 标 kline_today_missing（写 .notoday 标记 + 全局集合）。
    """
    path = cache_dir("kline", date) / f"{code}.js"
    fail_mark = path.with_name(f"{code}.fail")
    today_mark = path.with_name(f"{code}.notoday")
    if fail_mark.exists():
        _STATS["kline"]["fail"] += 1
        return None
    text = ""
    parsed: tuple[list[str], list[float]] | None = None
    if path.exists() and path.stat().st_size > 0:
        text = path.read_text(encoding="utf-8")
        if need_date:
            try:
                dates, closes = _parse_kline_text(text)
                dates, closes = _trim_to_date(dates, closes, need_date)
                if need_date in dates:
                    _STATS["kline"]["hit"] += 1
                    return dates, closes
                # 跨日不齐：缓存最新行早于当日，重拉
                _STATS["kline"]["stale"] += 1
                text = ""
            except Exception:  # noqa: BLE001
                text = ""  # 解析不了就当坏缓存，重拉
        else:
            _STATS["kline"]["hit"] += 1

    if not text:
        try:
            throttle()
            r = requests.get(KLINE_URL.format(code=code), headers={"User-Agent": UA, "Referer": REFERER}, timeout=15)
            if r.status_code != 200 or not r.text.strip():
                _STATS["kline"]["fail"] += 1
                fail_mark.write_text(f"{code} status={r.status_code}", encoding="utf-8", newline="\n")
                return None
            text = r.text
            path.write_text(text, encoding="utf-8", newline="\n")
            _STATS["kline"]["miss"] += 1
        except Exception as exc:  # noqa: BLE001
            _STATS["kline"]["fail"] += 1
            fail_mark.write_text(f"{code} {exc}", encoding="utf-8", newline="\n")
            print(f"  [K线失败] {code}: {exc}")
            return None
    try:
        dates, closes = _parse_kline_text(text)
        if need_date:
            dates, closes = _trim_to_date(dates, closes, need_date)
            if need_date not in dates:
                _STATS["kline"]["today_missing"] += 1
                _KLINE_MISSING_TODAY.add(code)
                today_mark.write_text(f"{code} need={need_date} last={dates[-1] if dates else '无行'}",
                                      encoding="utf-8", newline="\n")
            elif today_mark.exists():
                today_mark.unlink()
        return dates, closes
    except Exception as exc:  # noqa: BLE001
        print(f"  [K线解析失败] {code}: {exc}")
        _STATS["kline"]["fail"] += 1
        return None


def fetch_f10_concepts(code: str, date: str) -> list[dict]:
    """同花顺 F10 概念表：返回 [{clid, name}]；带日期级缓存。用于炸板池 reason_type 缺失时的同源补全。"""
    path = cache_dir("f10", date) / f"{code}.json"
    if path.exists() and path.stat().st_size > 0:
        _STATS["f10"]["hit"] += 1
        return json.loads(path.read_text(encoding="utf-8"))
    try:
        throttle()
        r = requests.get(F10_URL.format(code=code),
                         headers={"User-Agent": UA, "Referer": "http://basic.10jqka.com.cn/"}, timeout=15)
        if r.status_code != 200 or not r.text:
            _STATS["f10"]["fail"] += 1
            return []
        r.encoding = "gbk"
        rows = [{"clid": cid, "name": name.strip()} for cid, name in
                re.findall(r'<td class="gnName"\s+clid="(\d+)">([^<]*)</td>', r.text)]
        path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8", newline="\n")
        _STATS["f10"]["miss"] += 1
        return rows
    except Exception as exc:  # noqa: BLE001
        _STATS["f10"]["fail"] += 1
        print(f"  [F10 失败] {code}: {exc}")
        return []


def pct_return(closes: list[float], latest: float | None, n: int) -> float | None:
    series = list(closes)
    if latest is not None:
        series.append(latest)
    if len(series) < n + 1:
        return None
    base = series[-(n + 1)]
    if not base:
        return None
    return series[-1] / base - 1


def compute_rps(concepts: list[dict], stocks: list[dict], mapping: dict, log, date: str) -> tuple[str, dict]:
    """先试板块指数，失败则用概念内涨停股等权 5/20 日涨幅代理。"""
    t0 = time.perf_counter()
    need_date = date.replace("-", "")
    latest = {s["code"]: s.get("latest") for s in stocks if s["is_limit_up"]}
    by_code = {s["code"]: s for s in stocks if s["is_limit_up"]}
    kline_cache: dict[str, tuple | None] = {}
    index_err = ""
    # 1) 指数路径
    idx_results = {}
    for c in concepts:
        ic = c.get("index_code")
        if not ic:
            idx_results[c["concept_id"]] = None
            continue
        got = fetch_kline(str(ic), date, need_date)
        if got is None:
            index_err = f"index_code {ic} 拉取失败（同花顺指数 K 线不可用）"
            idx_results[c["concept_id"]] = None
            continue
        dates, closes = got
        idx_results[c["concept_id"]] = (pct_return(closes, None, 5), pct_return(closes, None, 20))
    if all(v for v in idx_results.values()) and idx_results:
        for c in concepts:
            r5, r20 = idx_results[c["concept_id"]]
            c["rps_5"], c["rps_20"], c["rps_source"] = r5, r20, "index"
        return "index", {"index_error": "", "elapsed": round(time.perf_counter() - t0, 2)}

    # 2) 等权代理
    ok_count = 0
    for c in concepts:
        rets5, rets20 = [], []
        for code in c["limit_up_stock_codes"]:
            if code not in kline_cache:
                kline_cache[code] = fetch_kline(code, date, need_date)
            got = kline_cache[code]
            if not got:
                continue
            dates, closes = got
            r5 = pct_return(closes, latest.get(code), 5)
            r20 = pct_return(closes, latest.get(code), 20)
            if r5 is not None:
                rets5.append(r5)
            if r20 is not None:
                rets20.append(r20)
        if rets5 and rets20:
            c["rps_5"] = round(sum(rets5) / len(rets5), 6)
            c["rps_20"] = round(sum(rets20) / len(rets20), 6)
            c["rps_source"] = "equal_weight"
            c["rps_sample"] = len(rets5)
            ok_count += 1
        else:
            c["rps_5"], c["rps_20"], c["rps_source"] = 0.0, 0.0, "unavailable"
            c["rps_sample"] = 0
    src = "equal_weight" if ok_count else "unavailable"
    if ok_count == 0:
        log.warning("RPS 全部不可用：%s", index_err)
    return src, {"index_error": index_err, "concepts_with_rps": ok_count,
                 "elapsed": round(time.perf_counter() - t0, 2)}


# ---------------------------------------------------------------- 输出

def md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def write_json(path: Path, obj: dict) -> None:
    common.write_json(path, obj)


def quality_report(date: str, src_status: dict, stocks: list[dict], concepts: list[dict],
                   rps_src: str, rps_info: dict, extra: dict | None = None) -> str:
    extra = extra or {}
    backfill = extra.get("backfill") or {}
    missing = {
        "unit_uncertain": sum(1 for s in stocks if s["unit_uncertain"]),
        "first_seal_time_uncertain": sum(1 for s in stocks if s["first_seal_time_uncertain"]),
        "high_days_parse_error": sum(1 for s in stocks if s["high_days_parse_error"]),
        "concept_unmatched": sum(1 for s in stocks if s["concept_unmatched"]),
        "high_days_missing": sum(1 for s in stocks if s.get("high_days_missing")),
    }
    anomaly = {
        "one_word_board": sum(1 for s in stocks if s["one_word_board"]),
        "new_stock(is_new=1)": sum(1 for s in stocks if s["is_new"] == 1),
        "concept_unmatched": missing["concept_unmatched"],
        "first_seal_auction": sum(1 for s in stocks if s.get("first_seal_auction")),
        "rps_unavailable_concepts": sum(1 for c in concepts if c.get("rps_source") == "unavailable"),
        "zhaban_concepts_missing": sum(1 for c in concepts if c.get("zhaban_source") == "missing_reason_type"),
        "zhaban_backfilled_limit_up": backfill.get("limit_up", 0),
        "zhaban_backfilled_f10": backfill.get("f10", 0),
        "zhaban_still_missing": backfill.get("still_missing", 0),
    }
    lu_total = sum(1 for s in stocks if s["is_limit_up"])
    lu_matched = sum(1 for s in stocks if s["is_limit_up"] and not s["concept_unmatched"])
    coverage = round(lu_matched / lu_total, 3) if lu_total else 0.0
    min_ratio = float(common.cfg().get("concept_pool_min_match_ratio", 0.5))
    critical = missing["first_seal_time_uncertain"] + missing["high_days_parse_error"]
    if all(v.get("ok") for v in src_status.values()) and critical == 0:
        conclusion = "数据完整" if coverage >= min_ratio else f"部分降级（概念映射覆盖不足 {coverage:.0%}，涨停股 {lu_matched}/{lu_total} 匹配上）"
    elif any(v.get("ok") for v in src_status.values()):
        conclusion = "部分降级"
    else:
        conclusion = "不建议使用"
    lines = [
        f"# 数据质量报告 {date}", "",
        "## 1. 数据源状态", "",
        "| 源 | 状态 | 响应耗时(s) | 返回条数 | 数据日期／接口状态（请求时刻，非数据时间） |", "| --- | --- | --- | --- | --- |",
    ]
    for name, st in src_status.items():
        lines.append(f"| {name} | {'成功' if st.get('ok') else '失败' + ('/降级' if st.get('degraded') else '')} "
                     f"| {st.get('elapsed')} | {st.get('count')} | {st.get('data_timestamp', '-')} |")
    lines += ["", "> 注：同花顺 `trade_status` 是**请求时刻**的盘口状态"
              "（00:00—09:00 固定显示「未开盘」），与 `date` 参数无关，不代表数据时间；"
              "数据是否完整看返回条数与 01/02 文件。", ""]
    lines += ["", "## 2. 字段缺失统计", "", "| 字段 | 条数 |", "| --- | --- |"]
    lines += [f"| {k} | {v} |" for k, v in missing.items()]
    lines += ["", "## 3. 异常值统计", "", "| 项 | 条数 |", "| --- | --- |"]
    lines += [f"| {k} | {v} |" for k, v in anomaly.items()]
    lines += ["", f"RPS 数据源：**{rps_src}**；概念覆盖 {rps_info.get('concepts_with_rps', 0)} 个；"
                  f"RPS 耗时 {rps_info.get('elapsed', '-')}s（K线缓存命中 "
                  f"{_STATS['kline']['hit']} / 新增 {_STATS['kline']['miss']} / 失败 {_STATS['kline']['fail']}）。"
              + (f" 指数路径失败原因：{rps_info.get('index_error')}" if rps_src != "index" else "")]
    if backfill.get("limit_up") or backfill.get("f10") or backfill.get("still_missing"):
        lines += ["", f"炸板 reason_type 回填：涨停池同码 **{backfill.get('limit_up', 0)}** 条 / "
                      f"F10 clid **{backfill.get('f10', 0)}** 条 / 仍缺 **{backfill.get('still_missing', 0)}** 条"
                      f"（f10 缓存命中 {_STATS['f10']['hit']} / 新增 {_STATS['f10']['miss']}）。"]
    lines += ["", f"概念映射覆盖率：**{coverage:.1%}**（涨停股 {lu_matched}/{lu_total}；阈值 "
                  f"{min_ratio:.0%}，见 `concept_pool_min_match_ratio`）"]
    unav = [c["concept_id"] for c in concepts if c.get("rps_source") == "unavailable"]
    if unav:
        lines.append(f"\n> RPS不可用概念：{'、'.join(unav)}；预备分将缺失该子项"
                     "（`fallback_plan.rps_unavailable: prep_rps_zero`）。")
    lines += ["", "## 4. 结论", ""]
    if coverage < min_ratio:
        lines.append(f"**⚠️ 概念映射覆盖率 {coverage:.1%}（{lu_matched}/{lu_total}），低于阈值 "
                     f"{min_ratio:.0%}，阶段2评分可信度低。**")
    else:
        lines.append(f"**✅ 概念映射覆盖率 {coverage:.1%}（{lu_matched}/{lu_total}），达到阈值 "
                     f"{min_ratio:.0%}。**")
    lines += ["", f"**{conclusion}**", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=common.today())
    ap.add_argument("--no-rps", action="store_true", help="跳过 RPS（仅调试用）")
    args = ap.parse_args()
    date = args.date
    log = common.logger(date, "fetch_data")
    dd = common.data_dir(date)
    started = datetime.now().isoformat(timespec="seconds")
    t_start = time.perf_counter()
    log.info("fetch_data start date=%s", date)

    stocks_out = dd / f"01_stocks_raw_{date}.json"
    concepts_out = dd / f"02_concepts_raw_{date}.json"
    # 先写 incomplete，进程中断即留痕
    for p in (stocks_out, concepts_out):
        write_json(p, {"schema_version": common.cfg()["schema_version"], "data_date": date,
                       "run_status": "incomplete", "producer": "fetch_data.py"})

    mapping = load_mapping()
    alias_idx = build_alias_index(mapping)
    unmapped: list[str] = []
    src_status: dict[str, dict] = {}
    degraded_sources: list[str] = []
    stocks: list[dict] = []

    for name, path, kind, required in (
        ("同花顺涨停池", "limit_up_pool", "limit_up", True),
        ("同花顺炸板池", "open_limit_pool", "zhaban", False),
        ("同花顺跌停池", "lower_limit_pool", "limit_down", False),
    ):
        if common.cfg().get("system_paused"):
            print("system_paused: true，停止抓取")
            return 0
        print(f"== 拉取 {name} ==")
        try:
            res = pull_pool(path, date, log)
            norm = normalize_stocks(res["rows"], kind, mapping, alias_idx, unmapped)
            stocks.extend(norm)
            src_status[name] = {"ok": True, "elapsed": res["elapsed"], "count": len(res["rows"]),
                                "pages": res["pages"], "data_timestamp": res["data_timestamp"]}
            print(f"  总数 {res['total']}，拉取 {len(res['rows'])} 条，耗时 {res['elapsed']}s，{res['pages']} 页")
            for row in norm[:3]:
                print("  样例:", json.dumps({k: row[k] for k in ("code", "name", "high_days_raw", "reason_type_raw",
                                                                  "order_amount", "open_num", "first_seal_minutes",
                                                                  "turnover_rate", "concept_id", "one_word_board")},
                                            ensure_ascii=False))
        except Exception as exc:  # noqa: BLE001
            print(f"  {name} 失败：{exc}")
            src_status[name] = {"ok": False, "elapsed": None, "count": 0, "error": str(exc),
                                "data_timestamp": "-", "degraded": not required}
            degraded_sources.append(name)
            if required:
                alert = dd / "ALERT.md"
                common.write_text(alert, f"# ⚠️ 数据告警\n\n- 日期：{date}\n- 失败源：{name}（硬门槛）\n"
                                         f"- 影响字段：涨停清单全部字段（成交额、封单额、首封时间、涨停原因等）\n"
                                         f"- 完整报错：`{exc}`\n"
                                         f"- 建议：`fallback_plan.ths_limit_up_pool_failed = stop_and_alert`，"
                                           f"修好接口后重跑；不要用 pywencai 顶替（已 403）。\n")
                log.error("硬门槛失败：%s", exc)
                write_json(stocks_out, {"schema_version": common.cfg()["schema_version"], "data_date": date,
                                        "run_status": "incomplete", "producer": "fetch_data.py",
                                        "error": str(exc), "stocks": stocks})
                write_degraded(date, degraded_sources, ["涨停清单全部字段"])
                return 1

    seen = set()
    uniq_stocks = []
    for s in stocks:
        key = (s["code"], s["is_limit_up"], s["is_zhaban"], s["is_limit_down"])
        if key in seen:
            continue
        seen.add(key)
        uniq_stocks.append(s)
    if len(uniq_stocks) != len(stocks):
        print(f"  去重：{len(stocks)} -> {len(uniq_stocks)}（分页重复）")
    stocks = uniq_stocks

    zhaban_rows = [s for s in stocks if s["is_zhaban"]]
    backfill = {"limit_up": 0, "f10": 0, "tag_match": 0, "still_missing": 0}
    if zhaban_rows:
        print("== 炸板池 reason_type 补全 ==")
        backfill = backfill_zhaban(stocks, mapping, alias_idx, date, log)
    concepts = aggregate_concepts(stocks, mapping, backfill.get("still_missing", 0))
    zb_mapped = sum(1 for s in stocks if s["is_zhaban"] and s["concept_id"])

    if args.no_rps:
        for c in concepts:
            c["rps_5"], c["rps_20"], c["rps_source"] = None, None, "unavailable"
        rps_src, rps_info = "unavailable", {"concepts_with_rps": 0, "index_error": "--no-rps"}
    else:
        print("== 计算 RPS ==")
        rps_src, rps_info = compute_rps(concepts, stocks, mapping, log, date)
        print(f"  RPS 源 = {rps_src}；有 RPS 的概念 {rps_info.get('concepts_with_rps')} 个；"
              f"耗时 {rps_info.get('elapsed')}s；K线缓存命中 {_STATS['kline']['hit']} / 新增 "
              f"{_STATS['kline']['miss']} / 失败 {_STATS['kline']['fail']}；指数错误：{rps_info.get('index_error')}")

    # 输出
    stocks_payload = {
        "schema_version": common.cfg()["schema_version"],
        "data_date": date,
        "producer": "fetch_data.py",
        "adj": "none",
        "data_source": common.cfg()["data_source"],
        "run_status": "complete",
        "counts": {"limit_up": sum(1 for s in stocks if s["is_limit_up"]),
                   "zhaban": sum(1 for s in stocks if s["is_zhaban"]),
                   "limit_down": sum(1 for s in stocks if s["is_limit_down"])},
        "stocks": stocks,
    }
    concepts_payload = {
        "schema_version": common.cfg()["schema_version"],
        "data_date": date,
        "producer": "fetch_data.py",
        "run_status": "complete",
        "rps_source": rps_src,
        "concepts": concepts,
    }
    write_json(stocks_out, stocks_payload)
    write_json(concepts_out, concepts_payload)
    # 兼容别名（任务七按名引用）
    write_json(dd / "stocks_raw.json", stocks_payload)
    write_json(dd / "concepts_raw.json", concepts_payload)

    checks = {}
    for p in (stocks_out, concepts_out, dd / "stocks_raw.json", dd / "concepts_raw.json"):
        checks[p.name] = {"md5": md5(p), "size": p.stat().st_size}
    write_json(dd / "checksums.json", {"data_date": date, "files": checks})

    if unmapped:
        u = sorted(set(unmapped))
        log_path = common.root_path("log_dir") / "unmapped_tags.log"
        log_path.write_text(f"# {date} 未匹配标签 {len(u)} 个\n" + "\n".join(u) + "\n",
                            encoding="utf-8", newline="\n")
        print(f"  未匹配标签 {len(u)} 个 → {log_path}")

    report = quality_report(date, src_status, stocks, concepts, rps_src, rps_info,
                            {"backfill": backfill})
    common.write_text(common.reports_dir(date) / "data_quality_report.md", report)

    if backfill.get("still_missing"):
        degraded_sources.append(f"炸板池 {backfill['still_missing']} 条无法归属概念（reason_type 缺失且 F10 无匹配）")
    if rps_src == "unavailable":
        degraded_sources.append("RPS 数据源不可用")
    if degraded_sources:
        missing_fields = []
        missing_zb_concepts = sum(1 for c in concepts if c.get("zhaban_count") is None)
        if missing_zb_concepts:
            missing_fields += ["zhaban_count", "zhaban_rate"]
        if not src_status.get("同花顺跌停池", {}).get("ok"):
            missing_fields += ["limit_down_count"]
        if rps_src == "unavailable":
            missing_fields += ["rps_5", "rps_20"]
        active = []
        if missing_zb_concepts and "ths_zhaban_failed" in common.cfg()["fallback_plan"]:
            active.append("ths_zhaban_failed")
        if rps_src == "unavailable" and "rps_unavailable" in common.cfg()["fallback_plan"]:
            active.append("rps_unavailable")
        write_degraded(date, degraded_sources, missing_fields, active)
        print(f"  降级记录：{missing_fields}，fallback {active}")

    common.write_status("1", "fetch_data.py", date, True, started_at=started)
    if _KLINE_MISSING_TODAY:
        common.write_json(common.data_dir(date) / "kline_today_missing.json", {
            "date": date,
            "need_date": date.replace("-", ""),
            "count": len(_KLINE_MISSING_TODAY),
            "codes": sorted(_KLINE_MISSING_TODAY),
            "note": "缓存中有该股 K 线，但重拉后仍无当日行；下游按 kline_today_missing=true 处理，不猜。",
        })
    total_secs = round(time.perf_counter() - t_start, 2)
    print(f"\n输出：{stocks_out.name}（{len(stocks)} 条）、{concepts_out.name}（{len(concepts)} 个概念）、checksums.json")
    print(f"报告：{common.reports_dir(date) / 'data_quality_report.md'}")
    print(f"总耗时：{total_secs}s；K线缓存 命中 {_STATS['kline']['hit']} / 新增 {_STATS['kline']['miss']} / 失败 {_STATS['kline']['fail']}；"
          f"跨日重拉 {_STATS['kline']['stale']} / 当日行仍缺 {_STATS['kline']['today_missing']}；"
          f"F10 缓存 命中 {_STATS['f10']['hit']} / 新增 {_STATS['f10']['miss']}")
    return 0


def write_degraded(date: str, sources: list[str], missing: list[str], fallback: list[str] | None = None) -> None:
    common.write_json(common.data_dir(date) / "degraded_mode_log.json", {
        "date": date,
        "degraded_sources": sources,
        "missing_fields": missing,
        "fallback_plan_active": fallback or [],
    })


if __name__ == "__main__":
    raise SystemExit(main())
