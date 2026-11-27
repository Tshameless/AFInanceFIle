"""复现神驰机电截图的历史事件研究。依赖 baostock、numpy、pandas。

python 探低收阳回测.py --cache 本地缓存目录
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
import socket
import subprocess
import sys
import time

import baostock as bs
import numpy as np
import pandas as pd


TARGET = "sh.603109"
INDEX = "sh.000300"
SAMPLE_DATE = "2019-12-31"
BEGIN = "2019-12-01"
END = "2026-11-25"
HISTORY_END = "2026-10-27"
FIELDS = "date,code,open,high,low,close,preclose,volume,tradestatus,pctChg"
HORIZONS = (2, 5, 10)
COST = 0.0015


def rows(result):
    records = []
    while result.error_code == "0" and result.next():
        records.append(result.get_row_data())
    if result.error_code != "0" or not records:
        raise RuntimeError(f"BaoStock {result.error_code} {result.error_msg} ({len(records)} rows)")
    return {"fields": result.fields, "rows": records}


def get_cached(cache, key, query):
    path = cache / f"{key}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    socket.setdefaulttimeout(20)
    for attempt in range(3):
        login = bs.login()
        try:
            if login.error_code != "0":
                raise RuntimeError(login.error_msg)
            result = rows(query())
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
            return result
        except Exception:
            if attempt == 2:
                raise
            time.sleep(2)
        finally:
            bs.logout()


def download(task):
    cache, code = task
    fields = FIELDS if code == INDEX else FIELDS + ",isST"
    get_cached(cache, code, lambda: bs.query_history_k_data_plus(
        code, fields, start_date=BEGIN, end_date=END, frequency="d", adjustflag="3"))
    return code


def download_with_timeout(task):
    cache, code = task
    path = cache / f"{code}.json"
    for attempt in range(3):
        if path.is_file():
            return code
        try:
            subprocess.run([sys.executable, __file__, "--cache", str(cache), "--symbol", code],
                           check=True, timeout=70, stdout=subprocess.DEVNULL)
            return code
        except (subprocess.TimeoutExpired, subprocess.CalledProcessError):
            if attempt == 2 and not path.is_file():
                raise
    return code


def sample_universe(cache):
    listing = get_cached(cache, "universe-20191231", lambda: bs.query_all_stock(SAMPLE_DATE))
    codes = {r[0] for r in listing["rows"] if re.fullmatch(r"(sh\.60|sz\.00)\d{4}", r[0])}
    codes.remove(TARGET)
    return sorted(codes, key=lambda code: hashlib.sha256(
        f"2019-12-31/{code}".encode("ascii")).digest())[:240]


def read_prices(cache, code):
    data = json.loads((cache / f"{code}.json").read_text(encoding="utf-8"))
    frame = pd.DataFrame(data["rows"], columns=data["fields"]).set_index("date")
    if not frame.index.is_unique or not frame.index.is_monotonic_increasing or set(frame.code) != {code}:
        raise ValueError(f"Unordered or mismatched data for {code}")
    return frame.drop(columns="code").apply(pd.to_numeric).loc[:END]


def event_table(frame, index, code, history_end=HISTORY_END):
    """行索引为信号收盘日，进出场按沪深300的交易日历定位。"""
    df = frame.reindex(index.index)
    traded = df.tradestatus.eq(1) & df.volume.gt(0) & df.preclose.gt(0)
    # 交易所除权参考昨收使相邻交易日处于同一口径，避免现金分红或送转被识别为暴跌。
    growth = (df.close / df.preclose).where(traded, 1).cumprod()
    scale = growth / df.close
    adj = df[["close", "low", "high", "open"]].mul(scale, axis=0).where(traded)
    prev = adj.close.shift(1)
    falling = (prev < adj.close.shift(2) - 1e-10) & (adj.close.shift(2) < adj.close.shift(3) - 1e-10)
    five_day_low = adj.low.shift(1).rolling(5).min()
    background = (falling & (prev < prev.rolling(10).mean() - 1e-10) &
                  (adj.low < five_day_low - 1e-10) & df.isST.eq(0) & traded)
    spread = df.high - df.low
    recovery = ((df.close > df.open) & (df.open - df.low >= spread * 0.20 - 1e-9) &
                (df.close - df.low >= spread * 0.50 - 1e-9))
    signal = background & recovery
    confirmed = signal.shift(1, fill_value=False) & (adj.close > adj.high.shift(1) + 1e-10) & df.isST.eq(0)
    can_buy = traded & df.isST.eq(0) & (df.open < df.preclose * 1.095 - 1e-9)
    entry = adj.open.shift(-1).where(can_buy.shift(-1, fill_value=False))
    # 计划卖出日停牌或一字下跌则顺延至下个可估计成交的收盘。
    one_price_down = (df.high == df.low) & (df.close < df.preclose)
    can_sell = traded & ~one_price_down
    dates = pd.Series(index.index, index=index.index, dtype="string")
    sale_price = adj.close.where(can_sell).bfill()
    sale_date = dates.where(can_sell).bfill()
    bench_exit = index.close.where(can_sell).bfill()
    out = pd.DataFrame({"code": code, "background": background, "signal": signal,
                        "steep": prev / adj.close.shift(3) <= 0.95 + 1e-10,
                        "confirmed": confirmed, "eligible_entry": entry.notna()}, index=index.index)
    # 买入当日不能卖；取第2至第5日盘中最高价，终点必须在观察窗内。
    best_high = pd.concat([adj.high.shift(-step) for step in range(2, 6)], axis=1).max(axis=1)
    out["touch3"] = ((best_high / entry >= 1.03 - 1e-10).astype(float)
                     .where(entry.notna() & (dates.shift(-5) <= history_end)))
    for n in HORIZONS:
        eligible = (sale_date.shift(-n) <= history_end) & (dates.shift(-n) <= history_end)
        gross = sale_price.shift(-n) / entry - 1
        out[f"net{n}"] = (gross - COST).where(eligible)
        out[f"index{n}"] = (bench_exit.shift(-n) / index.open.shift(-1) - 1 - COST).where(eligible & entry.notna())
        out[f"delayed{n}"] = (sale_date.shift(-n) != dates.shift(-n)).fillna(False)
        out[f"due{n}"] = (dates.shift(-n) <= history_end).fillna(False)
    return out.loc[(out.background | out.confirmed) & (out.index >= "2020-01-01") & (out.index <= history_end)]


def describe(events, n):
    vals = events[f"net{n}"].dropna()
    if vals.empty:
        return {"n": 0}
    idx = events.loc[events[f"net{n}"].notna(), f"index{n}"]
    return {"n": len(vals), "wins": int((vals > 1e-10).sum()), "win": float((vals > 1e-10).mean()),
            "gross_win": float((vals + COST > 1e-10).mean()),
            "mean": float(vals.mean()), "median": float(vals.median()), "index_win": float((idx > 1e-10).mean()),
            "index_mean": float(idx.mean()), "delayed": int(events.loc[events[f"net{n}"].notna(), f"delayed{n}"].sum()),
            "unresolved": int((events.eligible_entry & events[f"due{n}"] & events[f"net{n}"].isna()).sum())}


def compare(events, field):
    eligible = events[events.background & events[field].notna()].copy()
    eligible["positive"] = eligible[field] > 1e-10
    groups = eligible.groupby([eligible.index, "signal"]).positive.agg(["sum", "count"]).unstack("signal").dropna()
    wins = groups["sum"][True]
    counts = groups["count"][True]
    expected_wins = (groups["sum"][False] / groups["count"][False]) * counts
    monthly = pd.DataFrame({"difference": wins - expected_wins, "count": counts})
    blocks = monthly.groupby(monthly.index.str[:7]).sum().to_numpy()
    rng = np.random.default_rng(20191231)
    draws = blocks[rng.integers(len(blocks), size=(5000, len(blocks)))].sum(axis=1)
    ci = np.quantile(draws[:, 0] / draws[:, 1], [0.025, 0.975])
    return {"n": int(counts.sum()), "dates": len(groups), "control_observations": int(groups["count"][False].sum()),
            "signal_win": float(wins.sum() / counts.sum()), "control_win": float(expected_wins.sum() / counts.sum()),
            "diff": float((wins.sum() - expected_wins.sum()) / counts.sum()), "ci": ci.tolist()}


def analyze(cache, cohort):
    bench = read_prices(cache, INDEX)
    pieces = [event_table(read_prices(cache, code), bench, code) for code in [TARGET, *cohort]]
    own = pieces[0]
    market = pd.concat(pieces[1:]).sort_index()
    result = {"period_end": HISTORY_END, "cost": COST, "cohort_codes": cohort}
    for key, subset in [("market", market), ("target", own)]:
        result[key] = {}
        for label in ("signal", "confirmed"):
            group = subset[subset[label]]
            result[key][label] = {n: describe(group, n) for n in HORIZONS}
            result[key][label]["unique_codes_5"] = int(group.loc[group.net5.notna(), "code"].nunique())
    result["matched"] = {n: compare(market, f"net{n}") for n in HORIZONS}
    result["matched_touch3"] = compare(market, "touch3")
    steep = market[market.steep]
    result["steep"] = {"all5": describe(steep[steep.signal], 5), "matched5": compare(steep, "net5")}
    result["split5"] = {label: compare(group, "net5") for label, group in (
        ("2020-2022", market[market.index < "2023"]), ("2023-end", market[market.index >= "2023"]))}
    result["last_price_before_cutoff"] = [code for code in cohort if read_prices(cache, code).index[-1] < HISTORY_END]
    target = read_prices(cache, TARGET)
    case_events = event_table(target, bench, TARGET, END)
    result["illustrations"] = {}
    for day in ("2026-11-02", "2026-11-23"):
        row = case_events.loc[day]
        entry_day = bench.index[bench.index.get_loc(day) + 1]
        result["illustrations"][day] = {
            "signal": bool(row.signal), "entry_day": entry_day, "entry_open": float(target.at[entry_day, "open"]),
            "daily_ohlc": {k: float(target.at[day, k]) for k in ("open", "high", "low", "close")},
            "net": {n: (float(row[f"net{n}"]) if pd.notna(row[f"net{n}"]) else None) for n in HORIZONS}}
    result["screenshot_mark"] = {"date": "2026-11-26", "price_from_image": 15.25,
                                 "gross_vs_nov24_open": 15.25 / target.at["2026-11-24", "open"] - 1}
    (cache / "results.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k != "cohort_codes"}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--download-only", action="store_true")
    parser.add_argument("--symbol", help=argparse.SUPPRESS)
    args = parser.parse_args()
    args.cache.mkdir(exist_ok=True, parents=True)
    if args.symbol:
        download((args.cache, args.symbol))
        return
    cohort = sample_universe(args.cache)
    codes = [INDEX, TARGET, *cohort]
    with ThreadPoolExecutor(args.workers) as pool:
        for i, _ in enumerate(pool.map(download_with_timeout, [(args.cache, code) for code in codes]), 1):
            if i % 40 == 0:
                print(f"{i}/{len(codes)}", flush=True)
    if not args.download_only:
        analyze(args.cache, cohort)


if __name__ == "__main__":
    main()
