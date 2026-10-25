#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""导出时间范围解析单测：自定义 start/end 优先，days 预设回落"""
import os
import sys
import unittest
from datetime import date, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'webui'))

import app


class TestTimeRange(unittest.TestCase):
    def test_custom_range_inclusive_end(self):
        tr = app._time_range({'start': '2026-01-01', 'end': '2026-01-31'})
        self.assertEqual(tr, (date(2026, 1, 1), date(2026, 2, 1)))  # 含端日：end+1天

    def test_custom_range_swapped(self):
        tr = app._time_range({'start': '2026-02-10', 'end': '2026-01-01'})
        self.assertEqual(tr, (date(2026, 1, 1), date(2026, 2, 11)))

    def test_days_fallback(self):
        tr = app._time_range({'days': 7})
        expected_end = date.today() + timedelta(days=1)
        self.assertEqual(tr, (expected_end - timedelta(days=7), expected_end))

    def test_days_zero_means_all(self):
        self.assertIsNone(app._time_range({'days': 0}))

    def test_no_args_defaults_to_30_days(self):
        tr = app._time_range({})
        expected_end = date.today() + timedelta(days=1)
        self.assertEqual(tr, (expected_end - timedelta(days=30), expected_end))

    def test_invalid_dates_fall_back_to_days(self):
        tr = app._time_range({'start': 'not-a-date', 'end': '2026-01-01', 'days': 7})
        expected_end = date.today() + timedelta(days=1)
        self.assertEqual(tr, (expected_end - timedelta(days=7), expected_end))

    def test_partial_custom_ignored(self):
        # 只填一端不生效，回落天数
        tr = app._time_range({'start': '2026-01-01', 'days': 7})
        expected_end = date.today() + timedelta(days=1)
        self.assertEqual(tr, (expected_end - timedelta(days=7), expected_end))

    def test_custom_takes_priority_over_days(self):
        tr = app._time_range({'start': '2026-01-01', 'end': '2026-03-01', 'days': 7})
        self.assertEqual(tr, (date(2026, 1, 1), date(2026, 3, 2)))


class TestRangeTag(unittest.TestCase):
    def test_tags(self):
        self.assertEqual(app._range_tag({'start': '2026-01-01', 'end': '2026-01-31'}),
                         '2026-01-01_2026-01-31')
        self.assertEqual(app._range_tag({'start': '2026-02-10', 'end': '2026-01-01'}),
                         '2026-01-01_2026-02-10')  # 乱序自动交换
        self.assertEqual(app._range_tag({'days': 30}), '30天')
        self.assertEqual(app._range_tag({'days': 0}), '全部')
        self.assertEqual(app._range_tag({}), '30天')


if __name__ == '__main__':
    unittest.main()
