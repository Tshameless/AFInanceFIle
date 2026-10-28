"""补抓指定区间的历史涨停池，缓存来源数据并按实际交易日核算连板。"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlencode

import sync_limit_up as app

CALENDAR_API = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
SOURCE_NAME = "同花顺历史涨停池"


def trading_calendar(start: date, end: date):
    url = CALENDAR_API + "?" + urlencode({
        "param": f"sh000001,day,{start.isoformat()},{end.isoformat()},640,qfq",
    })
    result = app.fetch_json(url)
    if result.get("code") != 0:
        raise ValueError("上证指数交易日期请求失败")
    data = (result.get("data") or {}).get("sh000001") or {}
    rows = data.get("day") or data.get("qfqday") or []
    days = [date.fromisoformat(row[0]) for row in rows]
    if not days or days != sorted(set(days)):
        raise ValueError("交易日期为空、重复或无序")
    if any(day < start or day > end for day in days):
        raise ValueError("交易日期超出请求范围")
    if (days[0] - start).days > 15 or (end - days[-1]).days > 15:
        raise ValueError("交易日期未覆盖所需区间，请拆分补抓")
    return days, url


def load_source(day: date, root: Path, delay: float):
    path = root / "data" / "backfill" / "ths" / f"{day}.json"
    if path.exists():
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("date") != day.isoformat() or not isinstance(saved.get("stocks"), dict):
            raise ValueError(f"来源缓存损坏: {path}")
        return saved
    rows, url = app.fetch_reasons(day)
    if not rows:
        raise ValueError("交易日期返回空涨停池，无法区分零涨停与来源缺失")
    # 分时走势不是复盘数据所需字段，避免在每只股票中保存重复的图表序列。
    rows = {code: {k: v for k, v in row.items() if k != "time_preview"} for code, row in rows.items()}
    saved = {"date": day.isoformat(), "fetched_at": datetime.now(app.CHINA).isoformat(timespec="seconds"),
             "url": url, "stocks": rows}
    app.atomic_write(path, json.dumps(saved, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    if delay:
        time.sleep(delay)
    return saved


def exact_pattern_streak(row):
    pattern = str(row.get("high_days") or "").strip()
    if pattern == "首板":
        return 1
    match = re.fullmatch(r"(\d+)天(\d+)板", pattern)
    if match and int(match[1]) == int(match[2]) and int(match[1]) > 0:
        return int(match[1])
    match = re.fullmatch(r"(\d+)连板", pattern)
    return int(match[1]) if match and int(match[1]) > 0 else None


def consecutive_streak(row, previous):
    """previous 为前一实际交易日的已核算记录；None 表示该日数据缺失。"""
    direct = exact_pattern_streak(row)
    computed = None
    if previous is not None:
        prior = previous.get(str(row["code"]))
        if prior is None:
            computed = 1
        elif prior["streak"] is not None:
            computed = prior["streak"] + 1
    if computed is not None and direct is not None and computed != direct:
        return None, "来源连续板形态与逐日涨停名单冲突"
    if computed is not None:
        return computed, "连续交易日涨停名单核算"
    if direct is not None:
        return direct, "同花顺明确的连续板形态"
    return None, "缺少前一交易日记录，无法确定连续板数"


def build_snapshot(saved, previous):
    rows = []
    conflicts = []
    for code, source_row in saved["stocks"].items():
        streak, basis = consecutive_streak(source_row, previous)
        price = app.number(source_row.get("latest"))
        mapped = {
            "c": code, "n": source_row.get("name"), "lbc": streak,
            "p": price * 1000 if price is not None else None,
            "zdp": source_row.get("change_rate"), "hs": source_row.get("turnover_rate"),
            "fund": source_row.get("order_amount"), "zbc": source_row.get("open_num"),
        }
        row = app.normalize(mapped, saved["stocks"], saved["url"])
        row["streak_basis"] = basis
        rows.append(row)
        if streak is None:
            conflicts.append(code)
    rows.sort(key=lambda row: (-(row["streak"] or 0), row["code"]))
    warnings = ["历史补抓来源为同花顺；封板时间、成交额、行业等接口未返回的字段留空。"]
    if conflicts:
        warnings.append(f"{len(conflicts)} 只股票连续板数无法确认，归入板数待核实。")
    return {
        "date": saved["date"], "requested_date": saved["date"], "fetched_at": saved["fetched_at"],
        "session": "历史补抓快照", "pool_source": saved["url"], "pool_source_name": SOURCE_NAME,
        "reason_source": saved["url"], "warnings": warnings, "stocks": rows,
        "coverage": "同花顺 HS,GEM2STAR 收录范围，与东方财富每日池口径可能不同",
        "classification_method": "按上证指数日线交易日期顺序核算同花顺涨停池，并核对明确连续板形态；多天多板不直接作连板数",
    }


def backfill(start: date, end: date, root=app.BASE_DIR, delay=0.3):
    if start > end:
        raise ValueError("开始日期不能晚于结束日期")
    if end >= datetime.now(app.CHINA).date():
        raise ValueError("历史补抓结束日期必须早于北京时间今天")
    if (end - start).days > 400:
        raise ValueError("单次最多补抓 400 天，请拆分区间")
    days, calendar_url = trading_calendar(start - timedelta(days=30), end)
    targets = [day for day in days if day >= start]
    report_path = root / "data" / "backfill" / f"report_{start}_{end}.json"
    report = {
        "start": str(start), "end": str(end), "calendar_source": calendar_url,
        "calendar_basis": "腾讯行情上证指数日线日期（作为交易日期参考）",
        "expected_trading_days": len(targets), "source": SOURCE_NAME,
        "entries": [], "warmup_failures": [], "complete": False,
    }
    previous = None
    for i, day in enumerate(days):
        path = root / "data" / "limit-up" / f"{day}.json"
        in_range = day >= start
        try:
            saved = load_source(day, root, delay)
            derived = build_snapshot(saved, previous)
            previous = {s["code"]: s for s in derived["stocks"]}
            if not in_range:
                continue
            if path.exists():
                existing = json.loads(path.read_text(encoding="utf-8"))
                if existing.get("date") != str(day) or not isinstance(existing.get("stocks"), list):
                    raise ValueError("现有快照格式无效，未覆盖")
                snapshot = existing
                status = "preserved"
            else:
                snapshot = derived
                app.atomic_write(path, json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
                status = "saved"
            report["entries"].append({
                "date": str(day), "status": status, "stocks": len(snapshot["stocks"]),
                "reasons": sum(s["reason_status"] == "available" for s in snapshot["stocks"]),
                "unknown_streak": sum(s["streak"] is None for s in snapshot["stocks"]),
            })
        except (OSError, ValueError, KeyError, TypeError) as error:
            previous = None
            entry = {"date": str(day), "status": "failed", "error": str(error)}
            if in_range:
                report["entries"].append(entry)
            else:
                report["warmup_failures"].append(entry)
        if in_range:
            report["updated_at"] = datetime.now(app.CHINA).isoformat(timespec="seconds")
            report["counts"] = dict(Counter(entry["status"] for entry in report["entries"]))
            app.atomic_write(report_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            if len(report["entries"]) % 10 == 0 or i == len(days) - 1 or report["entries"][-1]["status"] == "failed":
                print(f"进度 {len(report['entries'])}/{len(targets)}，{day}，{report['counts']}", flush=True)
    report["complete"] = len(report["entries"]) == len(targets) and not any(e["status"] == "failed" for e in report["entries"])
    report["total_stocks"] = sum(e.get("stocks", 0) for e in report["entries"])
    report["total_reasons"] = sum(e.get("reasons", 0) for e in report["entries"])
    report["unknown_streaks"] = sum(e.get("unknown_streak", 0) for e in report["entries"])
    app.atomic_write(report_path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    app.publish(root=root)
    return report, report_path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--delay", type=float, default=0.3, help="每次请求后的间隔秒数，默认 0.3")
    args = parser.parse_args(argv)
    if not 0 <= args.delay <= 60:
        parser.error("--delay 必须在 0 到 60 秒之间")
    try:
        report, path = backfill(args.start, args.end, delay=args.delay)
        print(json.dumps({k: v for k, v in report.items() if k not in ("entries", "warmup_failures")}, ensure_ascii=False, indent=2))
        print(f"逐日补抓报告: {path}")
        return 0 if report["complete"] else 1
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"补抓失败: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
