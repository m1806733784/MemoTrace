#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""secure_store 密钥加密存储单测（DPAPI 当前用户作用域）"""
import json
import os
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from secure_store import load_key_file, save_key_file


class TestSecureStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, 'key.json')

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip(self):
        save_key_file(self.path, 'a' * 64, r'C:\wx\dir')
        with open(self.path, 'rb') as f:
            raw = f.read()
        self.assertFalse(raw.startswith(b'{'))       # 落盘不是明文 json
        self.assertNotIn(b'wx_dir', raw)             # 内容不含明文字段
        data = load_key_file(self.path)
        self.assertEqual(data['key'], 'a' * 64)
        self.assertEqual(data['wx_dir'], r'C:\wx\dir')

    def test_old_plaintext_upgrades_to_encrypted(self):
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump({'key': 'b' * 64, 'wx_dir': 'X'}, f)
        data = load_key_file(self.path)
        self.assertEqual(data['key'], 'b' * 64)
        with open(self.path, 'rb') as f:
            self.assertFalse(f.read().startswith(b'{'))  # 读取后已升级加密

    def test_corrupted_blob_returns_none(self):
        with open(self.path, 'wb') as f:
            f.write(b'\x00\x01\x02garbage')
        self.assertIsNone(load_key_file(self.path))

    def test_missing_file_returns_none(self):
        self.assertIsNone(load_key_file(os.path.join(self.tmp.name, 'nope.json')))

    def test_save_creates_missing_dir(self):
        path = os.path.join(self.tmp.name, 'new_dir', 'sub', 'key.json')
        save_key_file(path, 'c' * 64, 'Y')
        self.assertEqual(load_key_file(path)['key'], 'c' * 64)


if __name__ == '__main__':
    unittest.main()
