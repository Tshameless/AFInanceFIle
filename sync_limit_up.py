"""每日涨停池抓取与静态页面生成，仅依赖 Python 标准库。"""
from __future__ import annotations

import argparse
import calendar
from datetime import date, datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import sys
import tempfile
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
    referer = "https://quote.eastmoney.com/"
    if "10jqka.com.cn/" in url:
        referer = "https://data.10jqka.com.cn/"
    elif url.startswith("https://www.szse.cn/"):
        referer = "https://www.szse.cn/"
    for attempt in range(3):
        try:
            request = Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": referer,
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


def next_trading_day(written: date) -> date:
    """深交所月历覆盖跨周末、节假日和年末；缺失日期时停止推算。"""
    candidate = written + timedelta(days=1)
    for _ in range(2):
        url = f"https://www.szse.cn/api/report/exchange/onepersistenthour/monthList?month={candidate:%Y-%m}"
        flags = {row["jyrq"]: row["jybz"] for row in fetch_json(url)["data"]}
        last = candidate.replace(day=calendar.monthrange(candidate.year, candidate.month)[1])
        while candidate <= last:
            flag = str(flags.get(candidate.isoformat()))
            if flag == "1":
                return candidate
            if flag != "0":
                raise ValueError(f"交易日历缺少有效标记: {candidate}")
            candidate += timedelta(days=1)
    raise ValueError("交易日历未返回下一交易日")


def save_daily_plan(snapshot, root=BASE_DIR):
    written = date.fromisoformat(snapshot["date"])
    path = root / "复盘笔记" / "预案" / f"{written}.json"
    if path.exists() or snapshot["session"] != "盘后抓取快照" or written != datetime.now(CHINA).date():
        return
    rows = [s for s in snapshot["stocks"] if s["streak"]]
    if not rows:
        return
    height = max(s["streak"] for s in rows)
    cohorts = []
    if height > 1:
        cohorts.append(("leaders", f"{height}板股能否继续晋级", height, [s for s in rows if s["streak"] == height]))
    firsts = sorted((s for s in rows if s["streak"] == 1 and s.get("first_limit_time")),
                    key=lambda s: (s["first_limit_time"], s["code"]))[:3]
    if firsts:
        cohorts.append(("firsts", "最早首封的一组能否走出二板", 1, firsts))
    if not cohorts:
        return
    items = []
    for key, title, streak, stocks in cohorts:
        items.append({
            "id": key, "title": title,
            "expectation": f"检验本组至少一只股票能否在下一交易日收盘达到 {streak + 1} 板。",
            "check": f"逐只比对目标日同口径涨停池。记录达到 {streak + 1} 板的家数，并区分盘中触板和收盘封住。",
            "watch": {s["code"]: s["name"] for s in stocks}, "required_streak": streak + 1,
        })
    plan = {"target_date": next_trading_day(written).isoformat(),
            "source": "收盘同步按当日涨停池生成。首板按首封时间、同秒按代码排序，最多取三只。", "items": items}
    path.parent.mkdir(parents=True, exist_ok=True)
    # 完整写入后以不覆盖方式发布；重试或并行抓取均保留最先归档的原案。
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                     suffix=".tmp", delete=False) as temp:
        temporary = Path(temp.name)
        temp.write(json.dumps(plan, ensure_ascii=False, indent=2) + "\n")
    try:
        os.link(temporary, path)
    except FileExistsError:
        pass
    finally:
        temporary.unlink(missing_ok=True)


def review_promotion(item, stocks, same_pool):
    required = item["required_streak"]
    watched = [stocks[code] for code in item["watch"] if code in stocks]
    hits = [s["name"] for s in watched if (s["streak"] or 0) >= required]
    unknown = sum(s["streak"] is None for s in watched)
    if not same_pool or (not hits and unknown):
        return {"status": "证据不足", "actual": "收录口径不同或存在板数待核实的股票。",
                "problem": "现有涨停池无法可靠判定整组晋级情况。", "next_time": "逐只核对连续两个交易日的收盘价和涨停状态。"}
    return {"status": "兑现" if hits else "未兑现",
            "actual": f"原组 {len(item['watch'])} 只，收盘达到 {required} 板的有 {len(hits)} 只"
                      + (f"（{'、'.join(hits)}）。" if hits else "。")
                      + (f"另有 {unknown} 只板数待确认。" if unknown else ""),
            "problem": "组内出现晋级，仅验证了最低家数；组内个股的接力结果仍需分开看。" if hits
                       else "按前一日板数或首封顺序选出的这一组，未能延续到目标日收盘。",
            "next_time": "对照晋级与掉队个股的同题材跟随和首次开板后的表现，再判断筛选条件是否有区分度。"}


def review_first_boards(item, stocks, same_pool):
    results = {}
    facts = []
    for name, codes in item["promotion_groups"].items():
        cohort = {"required_streak": 2, "watch": codes}
        finding = review_promotion(cohort, stocks, same_pool)
        results[name] = finding["status"]
        facts.append(name + "：" + finding["actual"])
    statuses = set(results.values())
    if "证据不足" in statuses:
        status = "证据不足"
    elif len(statuses) == 1:
        status = statuses.pop()
    else:
        status = "部分兑现"
    return {"status": status, "actual": " ".join(facts),
            "problem": "两组仅各选三只首板。晋级家数可以检验所选样本，无法衡量全行业上涨家数或确认谁带动谁。",
            "next_time": "分别记录两组晋级率，把高位股封板时间与同业首次封板先后对照，再评价带动关系。"}


def load_plans(directory: Path, days: dict) -> dict:
    plans = {}
    for path in sorted((directory / "预案").glob("*.json")):
        plan = json.loads(path.read_text(encoding="utf-8"))
        written = date.fromisoformat(path.stem)
        target = date.fromisoformat(plan["target_date"])
        items = plan["items"]
        ids = {item["id"] for item in items}
        if written >= target or not items or len(ids) != len(items):
            raise ValueError(f"预案日期或条目标识无效: {path}")
        if any(written.isoformat() < day < target.isoformat() for day in days):
            raise ValueError(f"预案目标跨过已保存的交易日: {path}")
        evidence = {}
        review = {}
        snapshot = days.get(plan["target_date"], {})
        closed = snapshot.get("session") in {"盘后抓取快照", "历史补抓快照"}
        if closed:
            stocks = {stock["code"]: stock for stock in snapshot["stocks"]}
            origin = days.get(path.stem, {}).get("pool_source", "").split("?")[0]
            same_pool = bool(origin) and origin == snapshot.get("pool_source", "").split("?")[0]
            for item in items:
                facts = []
                for code, name in item["watch"].items():
                    stock = stocks.get(code)
                    if not stock or stock["streak"] is None:
                        result = "本日涨停池未收录" if not stock else "板数待核实"
                    else:
                        result = f'{stock["streak"]}板'
                        if stock.get("last_limit_time"):
                            result += f'，末封 {stock["last_limit_time"]}'
                    facts.append(f"{name}：{result}")
                evidence[item["id"]] = facts
                if "required_streak" in item:
                    review[item["id"]] = review_promotion(item, stocks, same_pool)
                elif "promotion_groups" in item:
                    review[item["id"]] = review_first_boards(item, stocks, same_pool)
            review_path = directory / "预案回顾" / f"{target}.json"
            if review_path.exists():
                record = json.loads(review_path.read_text(encoding="utf-8"))
                findings = record["items"]
                if record["plan_date"] != path.stem or not set(findings).issubset(ids):
                    raise ValueError(f"回顾与原预案不匹配: {review_path}")
                for result in findings.values():
                    if result["status"] not in {"兑现", "部分兑现", "未兑现", "未触发", "证据不足"} or not all(
                        result.get(field) for field in ("actual", "problem", "next_time")
                    ):
                        raise ValueError(f"预案回顾缺少结论或复盘依据: {review_path}")
                review.update(findings)
        if any(saved["target_date"] == plan["target_date"] for saved in plans.values()):
            raise ValueError(f"同一交易日对应多份原预案: {target}")
        plans[path.stem] = {**plan, "closed": closed, "evidence": evidence, "review": review}
    return plans


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
    review_dir = root / "复盘笔记"
    payload["review_dates"] = sorted(path.stem for path in review_dir.glob("*.html") if path.stem in days)
    payload["plans"] = load_plans(review_dir, days)
    # JSON 内联时转义 HTML 元字符，防止来源文本提前结束 script 标签。
    embedded = json.dumps(payload, ensure_ascii=False, allow_nan=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    template = (root / "limit-up.template.html").read_text(encoding="utf-8")
    if template.count("__LIMIT_UP_DATA__") != 1:
        raise ValueError("页面模板数据占位符异常")
    if snapshot:
        atomic_write(root / "data" / "limit-up" / f"{snapshot['date']}.json", json.dumps(snapshot, ensure_ascii=False, indent=2, allow_nan=False) + "\n")
    for relative in PAGE_PATHS:
        review_base = Path(os.path.relpath(review_dir, (root / relative).parent)).as_posix()
        page = template.replace("__REVIEW_BASE__", review_base).replace("__LIMIT_UP_DATA__", embedded)
        atomic_write(root / relative, page)
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
        if snapshot:
            try:
                save_daily_plan(snapshot)
            except (OSError, ValueError, KeyError, TypeError) as error:
                print(f"次日预案未生成（可在今日收盘后重试）: {error}", file=sys.stderr)
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
