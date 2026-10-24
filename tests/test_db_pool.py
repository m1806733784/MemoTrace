#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""DBPool 连接池单测：并发 checkout、独占等待、空闲复用"""
import os
import sys
import threading
import time
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'webui'))

from db_pool import DBPool


class FakeDB:
    def __init__(self, pool_id):
        self.pool_id = pool_id
        self.closed = False

    def close(self):
        self.closed = True


class TestDBPool(unittest.TestCase):
    def _factory(self):
        FakeDB._count = getattr(FakeDB, '_count', 0) + 1
        return FakeDB(FakeDB._count)

    def test_concurrent_checkouts_get_distinct_instances(self):
        pool = DBPool(self._factory)
        with pool.checkout() as a, pool.checkout() as b:
            self.assertIsNot(a, b)

    def test_idle_reuse(self):
        pool = DBPool(self._factory)
        with pool.checkout() as a:
            pass
        with pool.checkout() as b:
            self.assertIs(a, b)  # 归还后复用，不新建

    def test_exclusive_closes_idle_and_waits_for_active(self):
        pool = DBPool(self._factory)
        with pool.checkout() as a:
            pass  # a 进入空闲
        held = []
        release = threading.Event()

        def holder():
            with pool.checkout() as b:
                held.append(b)
                release.wait(2)  # 模拟长请求

        t = threading.Thread(target=holder)
        t.start()
        deadline = time.time() + 1
        while not held and time.time() < deadline:
            time.sleep(0.01)
        self.assertTrue(held)  # b 正在使用

        exclusive_done = threading.Event()

        def sync_worker():
            with pool.exclusive():
                exclusive_done.set()

        t2 = threading.Thread(target=sync_worker)
        t2.start()
        time.sleep(0.2)
        self.assertFalse(exclusive_done.is_set())  # 在用连接未归还，独占等待
        release.set()
        t.join()
        t2.join()
        self.assertTrue(exclusive_done.is_set())
        self.assertTrue(a.closed)      # 空闲实例在独占时被关闭
        self.assertTrue(held[0].closed)  # 归还进空闲后同样被独占关闭

    def test_checkout_blocks_during_exclusive(self):
        pool = DBPool(self._factory)
        acquired = []

        with pool.exclusive():
            def try_checkout():
                with pool.checkout() as db:
                    acquired.append(db)

            t = threading.Thread(target=try_checkout)
            t.start()
            time.sleep(0.2)
            self.assertFalse(acquired)  # 独占期间 checkout 排队
        t.join(2)
        self.assertTrue(acquired)  # 独占结束后放行

    def test_new_instances_after_exclusive(self):
        pool = DBPool(self._factory)
        with pool.checkout() as a:
            pass
        with pool.exclusive():
            pass
        self.assertTrue(a.closed)
        with pool.checkout() as b:
            self.assertIsNot(a, b)  # 独占清空后只能新建


if __name__ == '__main__':
    unittest.main()
