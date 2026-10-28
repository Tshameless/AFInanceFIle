from datetime import date
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import backfill_limit_up as app


class BackfillTests(unittest.TestCase):
    def row(self, code="000001", pattern="首板"):
        return {"code": code, "name": "测试股票", "latest": 10.2, "change_rate": 10,
                "reason_type": "业绩增长", "high_days": pattern, "open_num": None}

    def saved(self, day="2025-10-28", rows=None):
        row = self.row()
        return {"date": day, "fetched_at": "2026-10-28T12:00:00+08:00", "url": "https://example.com",
                "stocks": rows if rows is not None else {row["code"]: row}}

    def test_multi_day_patterns_use_previous_trading_day(self):
        row = self.row(pattern="4天3板")
        self.assertEqual(app.consecutive_streak(row, {"000001": {"streak": 1}})[0], 2)
        self.assertEqual(app.consecutive_streak(row, {})[0], 1)
        self.assertIsNone(app.consecutive_streak(row, None)[0])
        self.assertEqual(app.consecutive_streak(self.row(pattern="3天3板"), None)[0], 3)

    def test_conflicting_source_shape_not_silently_classified(self):
        streak, basis = app.consecutive_streak(self.row(pattern="首板"), {"000001": {"streak": 3}})
        self.assertIsNone(streak)
        self.assertIn("冲突", basis)

    def test_missing_fields_not_fabricated(self):
        snapshot = app.build_snapshot(self.saved(), {})
        row = snapshot["stocks"][0]
        self.assertEqual(row["price"], 10.2)
        self.assertEqual(row["group"], "1")
        self.assertEqual(row["reason"], "业绩增长")
        for key in ("first_limit_time", "last_limit_time", "amount", "open_count", "seal_amount"):
            self.assertIsNone(row[key])
        self.assertEqual(snapshot["pool_source_name"], app.SOURCE_NAME)

    def test_calendar_validation(self):
        payload = {"code": 0, "data": {"sh000001": {"day": [["2025-10-28"], ["2025-10-29"]]}}}
        with patch.object(app.app, "fetch_json", return_value=payload):
            days, _ = app.trading_calendar(date(2025, 10, 28), date(2025, 10, 29))
            self.assertEqual(len(days), 2)
        payload["data"]["sh000001"]["day"].reverse()
        with patch.object(app.app, "fetch_json", return_value=payload), self.assertRaises(ValueError):
            app.trading_calendar(date(2025, 10, 28), date(2025, 10, 29))

    def test_resume_preserves_existing_and_reports_gaps(self):
        days = [date(2025, 10, 27), date(2025, 10, 28), date(2025, 10, 29), date(2025, 10, 30)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            existing = root / "data/limit-up/2025-10-28.json"
            app.app.atomic_write(existing, json.dumps({"date": "2025-10-28", "stocks": [], "marker": "preserve"}))
            before = existing.read_bytes()
            with patch.object(app, "trading_calendar", return_value=(days, "source")), patch.object(app, "load_source", side_effect=[self.saved("2025-10-27"), self.saved(), OSError("network failed"), self.saved("2025-10-30")]), patch.object(app.app, "publish"):
                report, path = app.backfill(date(2025, 10, 28), date(2025, 10, 30), root, 0)
            self.assertEqual(existing.read_bytes(), before)
            self.assertEqual(report["counts"], {"preserved": 1, "failed": 1, "saved": 1})
            self.assertFalse(report["complete"])
            self.assertTrue(path.exists())
            self.assertFalse((root / "data/limit-up/2025-10-29.json").exists())

    def test_cached_data_skips_network(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data = self.saved()
            app.app.atomic_write(root / "data/backfill/ths/2025-10-28.json", json.dumps(data))
            with patch.object(app.app, "fetch_reasons") as fetch:
                self.assertEqual(app.load_source(date(2025, 10, 28), root, 0), data)
                fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
