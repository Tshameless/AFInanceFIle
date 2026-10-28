"""每日涨停池抓取与静态页面生成，仅依赖 Python 标准库。"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
import math
from pathlib import Path
import re
import sys
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_DIR = Path(__file__).resolve().parent
CHINA = timezone(timedelta(hours=8))
POOL_API = "https://push2ex.eastmoney.com/getTopicZTPool"
REASON_API = "https://data.10jqka.com.cn/dataapi/limit_up/limit_up_pool"
PAGE_PATHS = (
    "index.html",
    "股票分析工作台.html",
    "半导体股票分析/A股AI产业链全景图谱_低价精选.html",
)


def fetch_json(url: str) -> dict:
    for attempt in range(3):
        try:
            request = Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://data.10jqka.com.cn/" if "10jqka" in url else "https://quote.eastmoney.com/",
            })
            with urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8-sig"))
        except (OSError, ValueError):
            if attempt == 2:
                raise
            time.sleep(attempt + 1)
    raise RuntimeError("请求失败")


def pool_url(day: date) -> str:
    return POOL_API + "?" + urlencode({
        "ut": "7eea3edcaed734bea9cbfc24409ed989", "dpt": "wz.ztzt",
        "Pageindex": 0, "pagesize": 1000, "sort": "fbt:asc",
        "date": day.strftime("%Y%m%d"),
    })


def fetch_pool(day: date) -> tuple[list[dict] | None, str]:
    url = pool_url(day)
    result = fetch_json(url)
    if result.get("rc") != 0:
        raise ValueError(f"东方财富接口错误: {result.get('rc')}")
    data = result.get("data")
    # 空数据不能当作成功获取的零涨停交易日。
    if data is None:
        return None, url
    # qdate 在历史查询中也可能是接口当前交易日，不能据此重标历史数据。
    try:
        quote_day = datetime.strptime(str(data.get("qdate")), "%Y%m%d").date()
    except ValueError as error:
        raise ValueError("涨停池返回日期无效") from error
    if quote_day < day or quote_day > datetime.now(CHINA).date():
        raise ValueError("涨停池返回日期异常")
    if day == datetime.now(CHINA).date() and quote_day != day:
        raise ValueError("今日涨停池返回日期不一致")
    rows = data.get("pool")
    if not isinstance(rows, list) or int(data.get("tc", -1)) != len(rows):
        raise ValueError("涨停池数量不完整，停止更新以保留原快照")
    codes = [str(row.get("c", "")) for row in rows]
    if len(set(codes)) != len(codes) or any(not re.fullmatch(r"\d{6}", c) for c in codes):
        raise ValueError("涨停池股票代码无效或重复")
    return rows or None, url


def fetch_reasons(day: date) -> tuple[dict, str]:
    params = {
        "page": 1, "limit": 200,
        "field": "199112,10,9001,9002,330329,133971,133970,1968584,3475914,9003,9004",
        "filter": "HS,GEM2STAR", "date": day.strftime("%Y%m%d"),
        "order_field": "199112", "order_type": 0,
    }
    reasons = {}
    source = REASON_API + "?" + urlencode(params)
    page_count = 1
    page_number = 1
    expected_total = None
    while page_number <= page_count:
        params["page"] = page_number
        result = fetch_json(REASON_API + "?" + urlencode(params))
        if result.get("status_code") != 0:
            raise ValueError(f"同花顺原因接口错误: {result.get('status_msg')}")
        data = result.get("data") or {}
        if str(data.get("date")) != day.strftime("%Y%m%d"):
            raise ValueError("原因数据日期不一致，不跨日关联")
        rows = data.get("info")
        pagination = data.get("page") or {}
        if not isinstance(rows, list) or "total" not in pagination:
            raise ValueError("同花顺原因数据结构异常")
        total = int(pagination["total"])
        if expected_total is not None and total != expected_total:
            raise ValueError("原因池分页期间数量发生变化，请稍后重试")
        expected_total = total
        page_count = max(1, math.ceil(total / int(pagination.get("limit") or 200)))
        if page_count > 50:
            raise ValueError("原因接口分页异常")
        for row in rows:
            code = str(row.get("code", ""))
            if not re.fullmatch(r"\d{6}", code) or code in reasons:
                raise ValueError("原因池股票代码无效或重复")
            reasons[code] = row
        page_number += 1
    if len(reasons) != expected_total:
        raise ValueError("原因池分页数量不完整")
    return reasons, source


def number(value, divisor=1):
    try:
        value = float(value) / divisor
        return value if math.isfinite(value) else None
    except (ValueError, TypeError):
        return None


def clock_text(value):
    try:
        raw = f"{int(value):06d}"
        datetime.strptime(raw, "%H%M%S")
        return f"{raw[:2]}:{raw[2:4]}:{raw[4:]}"
    except (ValueError, TypeError):
        return None


def normalize(row: dict, reasons: dict, reason_url: str) -> dict:
    code = str(row["c"])
    reason_row = reasons.get(code, {})
    reason = str(reason_row.get("reason_type") or "").strip()
    if reason in ("--", "-", "暂无", "暂无涨停原因"):
        reason = ""
    streak = number(row.get("lbc"))
    # 只能按连续板数 lbc 分类，不能用 zttj.ct 或同花顺 high_days 的 N 天 M 板替代。
    streak = int(streak) if streak is not None and streak >= 1 and streak.is_integer() else None
    group = str(streak) if streak in (1, 2, 3) else "4+" if streak else "unknown"
    return {
        "code": code, "name": str(row.get("n") or code),
        "streak": streak, "group": group, "price": number(row.get("p"), 1000),
        "change_pct": number(row.get("zdp")), "turnover_pct": number(row.get("hs")),
        "amount": number(row.get("amount")), "seal_amount": number(row.get("fund")),
        "first_limit_time": clock_text(row.get("fbt")),
        "last_limit_time": clock_text(row.get("lbt")),
        "open_count": number(row.get("zbc")), "industry": str(row.get("hybk") or "未提供"),
        "reason": reason or "待核实（来源暂未提供）",
        "reason_status": "available" if reason else "missing",
        "reason_source": "同花顺涨停原因标签" if reason else "暂无来源",
        "reason_url": reason_url if reason else None,
        "source_pattern": str(reason_row.get("high_days") or ""),
    }


def collect(day: date, allow_fallback: bool) -> dict:
    requested = day
    pool = None
    for offset in range(15 if allow_fallback else 1):
        candidate = requested - timedelta(days=offset)
        print(f"获取涨停池: {candidate.isoformat()}")
        pool, source = fetch_pool(candidate)
        if pool:
            day = candidate
            break
    if not pool:
        raise ValueError("所选日期没有可用涨停池（可能休市、尚未开盘或超出接口历史范围），保留现有数据")
    warnings = []
    try:
        reasons, reason_source = fetch_reasons(day)
    except (OSError, ValueError, KeyError, TypeError) as error:
        reasons, reason_source = {}, ""
        warnings.append(f"涨停原因抓取失败: {error}；缺失原因均标注待核实。")
    # 历史池的 qdate 不可靠，要求另一来源明确确认日期与个股，避免跨日污染。
    if day < datetime.now(CHINA).date():
        for row in pool:
            matched = reasons.get(str(row["c"]))
            if matched is None:
                raise ValueError("历史涨停池无法通过同花顺同日股票代码交叉校验，保留已有快照")
            expected = number(matched.get("latest"))
            actual = number(row.get("p"), 1000)
            if expected is not None and actual is not None and abs(expected - actual) > 0.011:
                raise ValueError("历史涨停池与同日原因池价格不一致，停止发布")
    rows = [normalize(row, reasons, reason_source) for row in pool]
    rows.sort(key=lambda row: (-(row["streak"] or 0), row["first_limit_time"] or "99", row["code"]))
    now = datetime.now(CHINA)
    intraday = day == now.date() and now.hour * 60 + now.minute < 15 * 60 + 15
    return {
        "date": day.isoformat(), "requested_date": requested.isoformat(),
        "fetched_at": now.isoformat(timespec="seconds"),
        "session": "盘中快照" if intraday else "盘后抓取快照",
        "pool_source": source, "reason_source": reason_source,
        "warnings": warnings, "stocks": rows,
    }


def atomic_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def publish(snapshot=None, root=BASE_DIR):
    days = {}
    for path in sorted((root / "data" / "limit-up").glob("????-??-??.json")):
        saved = json.loads(path.read_text(encoding="utf-8"))
        if saved.get("date") != path.stem or not isinstance(saved.get("stocks"), list):
            raise ValueError(f"历史快照格式异常: {path.name}")
        days[path.stem] = saved
    if snapshot:
        days[snapshot["date"]] = snapshot
    payload = {"latest_date": max(days) if days else None, "days": days}
    # JSON 内联时转义 HTML 元字符，防止来源文本提前结束 script 标签。
    embedded = json.dumps(payload, ensure_ascii=False, allow_nan=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    template = (root / "limit-up.template.html").read_text(encoding="utf-8")
    if template.count("__LIMIT_UP_DATA__") != 1:
        raise ValueError("页面模板数据占位符异常")
    html = template.replace("__LIMIT_UP_DATA__", embedded)
    if snapshot:
        atomic_write(root / "data" / "limit-up" / f"{snapshot['date']}.json", json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    for relative in PAGE_PATHS:
        atomic_write(root / relative, html)
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", type=date.fromisoformat, help="指定交易日 YYYY-MM-DD；不跨日回退")
    parser.add_argument("--render-only", action="store_true", help="仅用已保存快照重新生成页面，不访问网络")
    args = parser.parse_args(argv)
    if args.render_only and args.date:
        parser.error("--date 与 --render-only 不能同时使用")
    try:
        if args.date and args.date > datetime.now(CHINA).date():
            raise ValueError("不能抓取未来日期")
        snapshot = None if args.render_only else collect(args.date or datetime.now(CHINA).date(), not args.date)
        publish(snapshot)
        if snapshot:
            available = sum(row["reason_status"] == "available" for row in snapshot["stocks"])
            print(f"完成: {snapshot['date']}，涨停 {len(snapshot['stocks'])} 只，已有原因 {available} 只。")
            for warning in snapshot["warnings"]:
                print(f"提示: {warning}")
        else:
            print("已按本地快照生成页面。")
        return 0
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f"更新失败: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
