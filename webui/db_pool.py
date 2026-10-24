#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sqlite 连接池：每个并发请求独占一个数据库接口实例，读请求真正并行。

此前 WebUI 用一把全局锁串行化所有数据库访问（消息分页、头像、媒体互相排队）。
池化后 checkout() 的请求各持实例并发读；同步（替换解密库文件）前用
exclusive()：等全部在用实例归还并关闭空闲实例，期间新请求在 checkout() 排队，
避免读到正在被替换的文件。
"""
import threading
from contextlib import contextmanager


class DBPool:
    def __init__(self, factory):
        """factory: () -> 数据库接口，每次调用创建全新实例（含独立 sqlite 连接）"""
        self._factory = factory
        self._cond = threading.Condition()
        self._idle = []       # 空闲实例（可复用）
        self._active = 0      # 正在被请求持有的实例数
        self._exclusive = False

    @contextmanager
    def checkout(self):
        with self._cond:
            while self._exclusive:
                self._cond.wait()
            db = self._idle.pop() if self._idle else self._factory()
            self._active += 1
        try:
            yield db
        finally:
            with self._cond:
                self._active -= 1
                if self._exclusive and self._active == 0:
                    self._cond.notify_all()  # 唤醒等待独占的同步线程
                elif not self._exclusive:
                    self._idle.append(db)
                    self._cond.notify()

    @contextmanager
    def exclusive(self):
        """独占模式：等全部在用实例归还、关闭空闲实例（替换 db 文件前调用）"""
        with self._cond:
            while self._exclusive or self._active > 0:
                self._cond.wait()
            self._exclusive = True
            while self._idle:
                self._idle.pop().close()
        try:
            yield
        finally:
            with self._cond:
                self._exclusive = False
                self._cond.notify_all()
