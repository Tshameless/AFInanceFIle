import json
from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import sync_limit_up as app


class DailyPlanTests(unittest.TestCase):
    def test_two_sectors_can_have_different_outcomes(self):
        item = {"promotion_groups": {"甲组": ["000001"], "乙组": ["000002"]}}
        stocks = {"000001": {"name": "甲", "streak": 2}}
        self.assertEqual(app.review_first_boards(item, stocks, True)["status"], "部分兑现")
        stocks["000002"] = {"name": "乙", "streak": None}
        self.assertEqual(app.review_first_boards(item, stocks, True)["status"], "证据不足")

    def test_calendar_crosses_month_and_skips_holiday(self):
        september = {"data": [{"jyrq": "2026-09-30", "jybz": "0"}]}
        october = {"data": [{"jyrq": f"2026-10-{day:02}", "jybz": str(int(day == 8))}
                            for day in range(1, 9)]}
        with patch.object(app, "fetch_json", side_effect=[september, october]):
            self.assertEqual(app.next_trading_day(date(2026, 9, 29)), date(2026, 10, 8))
        with patch.object(app, "fetch_json", return_value={"data": []}), self.assertRaises(ValueError):
            app.next_trading_day(date(2026, 9, 29))

    def test_plan_lifecycle_cannot_see_intraday_or_overwrite_original(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            notes = root / "复盘笔记"
            before, after = "2026-11-25", "2026-11-26"
            source = "https://example.org/pool"
            a = {"code": "000001", "name": "甲", "streak": 3, "first_limit_time": "09:33:00"}
            b = {"code": "000002", "name": "乙", "streak": 1, "first_limit_time": "09:35:00"}
            snapshot = {"date": before, "pool_source": source, "session": "盘中快照", "stocks": [a, b]}
            with patch.object(app, "datetime") as clock, patch.object(app, "next_trading_day", return_value=date.fromisoformat(after)):
                clock.now.return_value.date.return_value = date.fromisoformat(before)
                app.save_daily_plan(snapshot, root)
                self.assertFalse(notes.exists())
                snapshot["session"] = "盘后抓取快照"
                app.save_daily_plan(snapshot, root)
                original = notes / "预案" / f"{before}.json"
                saved = original.read_bytes()
                app.save_daily_plan({**snapshot, "stocks": [b]}, root)
                self.assertEqual(original.read_bytes(), saved)
            following = {"date": after, "pool_source": source, "session": "盘中快照", "stocks": [{**a, "streak": 4}]}
            def read():
                return app.load_plans(notes, {before: snapshot, after: following})[before]
            self.assertFalse(read()["closed"])
            self.assertEqual(read()["review"], {})
            following["session"] = "盘后抓取快照"
            self.assertEqual(read()["review"]["leaders"]["status"], "兑现")
            self.assertEqual(read()["review"]["firsts"]["status"], "未兑现")
            following["pool_source"] = "https://example.org/other"
            self.assertEqual(read()["review"]["firsts"]["status"], "证据不足")
            folder = notes / "预案回顾"
            folder.mkdir()
            findings = {"leaders": {"status": "部分兑现", "actual": "核实分时", "problem": "盘中分歧", "next_time": "查看首次回封"}}
            (folder / f"{after}.json").write_text(json.dumps({"plan_date": before, "items": findings}), encoding="utf-8")
            self.assertEqual(read()["review"]["leaders"], findings["leaders"])
            self.assertEqual(original.read_bytes(), saved)
