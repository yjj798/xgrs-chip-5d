from __future__ import annotations

import hashlib
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import yaml

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.yaml"

_cfg_cache: dict[str, Any] | None = None


def cfg(reload: bool = False) -> dict[str, Any]:
    """读 config.yaml，全项目唯一参数入口。"""
    global _cfg_cache
    if _cfg_cache is None or reload:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            _cfg_cache = yaml.safe_load(f)
    return _cfg_cache


def root_path(key: str) -> Path:
    """config 里的相对路径按项目根解析，保证整目录可搬。"""
    return (ROOT / cfg()[key]).resolve()


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def data_dir(date: str) -> Path:
    p = root_path("output_root") / date
    p.mkdir(parents=True, exist_ok=True)
    return p


def reports_dir(date: str) -> Path:
    p = ROOT / "reports" / date
    p.mkdir(parents=True, exist_ok=True)
    return p


def tracking_dir() -> Path:
    p = ROOT / "tracking"
    p.mkdir(parents=True, exist_ok=True)
    return p


def read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def make_checksums(files: Iterable[Path], out: Path) -> None:
    write_json(out, {p.name: sha256(p) for p in files if p.exists()})


def logger(date: str, name: str = "xgrs") -> logging.Logger:
    log_dir = root_path("log_dir")
    log_dir.mkdir(parents=True, exist_ok=True)
    log = logging.getLogger(f"{name}.{date}")
    log.setLevel(logging.INFO)
    if not log.handlers:
        fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
        fh = logging.FileHandler(log_dir / f"{date}.log", encoding="utf-8")
        fh.setFormatter(fmt)
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(fh)
        log.addHandler(sh)
    return log


def status_path(stage: str, date: str) -> Path:
    return data_dir(date) / f"13_run_status_{stage}_{date}.json"


def write_status(stage: str, script: str, date: str, ok: bool, error: str | None = None,
                 started_at: str | None = None) -> Path:
    path = status_path(stage, date)
    prev = read_json(path, {}) or {}
    payload = {
        "stage": stage,
        "script": script,
        "date": date,
        "run_status": "complete" if ok else "failed",
        "started_at": started_at or prev.get("started_at") or datetime.now().isoformat(timespec="seconds"),
        "finished_at": datetime.now().isoformat(timespec="seconds"),
        "exit_code": 0 if ok else 1,
        "error": error,
    }
    write_json(path, payload)
    return path


def upstream_ok(date: str, filenames: list[str]) -> tuple[bool, list[str]]:
    """校验上游文件存在且 run_status: complete（顶层没有该字段的文件视为通过）。"""
    missing: list[str] = []
    dd = data_dir(date)
    for name in filenames:
        p = dd / name
        if not p.exists():
            missing.append(f"{name}（文件不存在）")
            continue
        if p.suffix == ".json":
            obj = read_json(p, None)
            if isinstance(obj, dict) and obj.get("run_status") not in (None, "complete"):
                missing.append(f"{name}（run_status={obj.get('run_status')}）")
            if obj is None:
                missing.append(f"{name}（JSON 无法解析）")
    return (not missing, missing)


def require_upstream(date: str, filenames: list[str], stage: str, script: str) -> bool:
    ok, missing = upstream_ok(date, filenames)
    if not ok:
        print(f"[{script}] 上游校验失败，拒绝运行（阶段{stage}）：")
        for m in missing:
            print("  -", m)
        write_status(stage, script, date, False, "upstream missing: " + "; ".join(missing))
    return ok


TRADING_HOLIDAYS_FALLBACK = "holiday_list.json"


def prev_trade_date(date: str, back: int = 1) -> str:
    """上一个交易日（粗判：跳过周末；holiday_list 缺失时不额外处理节假日）。"""
    d = datetime.strptime(date, "%Y-%m-%d")
    n = 0
    while n < back:
        d = d - timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return d.strftime("%Y-%m-%d")


def load_catalyst_levels() -> dict[str, str]:
    """catalyst.json → {concept_id: 催化级别}；缺失的键不补，由调用方按「未标注」处理。"""
    obj = read_json(root_path("catalyst_path"), {}) or {}
    return {cid: (v or {}).get("催化级别", "未标注") for cid, v in (obj.get("concepts") or {}).items()}


def catalyst_points(level: str, cfg_: dict | None = None) -> tuple[int, str]:
    """催化级别 → 档位分（scorer 叙事维与 prep 预备分共用这一套读法）。

    产业/政策 = narrative_catalyst_policy(3)；事件 = narrative_catalyst_event(1)；
    纯情绪 / 未标注 = narrative_catalyst_pure(0)。返回 (分数, 依据说明)。
    """
    c = cfg_ or cfg()
    mapping = {"产业/政策": c["narrative_catalyst_policy"], "政策": c["narrative_catalyst_policy"],
               "事件": c["narrative_catalyst_event"], "纯情绪": c["narrative_catalyst_pure"],
               "未标注": c["narrative_catalyst_pure"]}
    pts = mapping.get(level, c["narrative_catalyst_pure"])
    return pts, f"催化「{level}」→ {pts}"


def is_trading_day(date: str, log: logging.Logger | None = None) -> bool:
    """先读 holiday_list.json（节假日直接 False），否则按工作日判断；文件缺失时降级为工作日粗判并警告。"""
    path = root_path("holiday_list_path")
    holidays = read_json(path, None)
    weekday = datetime.strptime(date, "%Y-%m-%d").weekday()
    if holidays is None:
        if log:
            log.warning("%s 不存在，按工作日粗判（周末视为非交易日），节假日可能误判", TRADING_HOLIDAYS_FALLBACK)
        return weekday < 5
    items = holidays.get("holidays", holidays) if isinstance(holidays, dict) else holidays
    if log:
        log.info("交易日历：holiday_list.json 已加载（%d 条节假日）", len(items))
    if date in items:
        return False
    return weekday < 5
