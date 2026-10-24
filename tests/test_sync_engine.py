#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""sync_engine 增量同步引擎的纯逻辑单测：页级解密、WAL 合并、变更检测"""
import hmac
import os
import struct
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
sys.path.insert(0, os.path.join(PROJECT_ROOT, 'webui'))

import sync_engine
from sync_engine import (PAGE_SIZE, RESERVE, SALT_SIZE, SHA512, _db_sig,
                         _derive_keys, decrypt_page, find_changed,
                         load_sync_state, save_sync_state, stable_copy)
from Crypto.Cipher import AES

PKEY = 'a' * 64  # 32字节密钥的hex
WAL_MAGIC = 0x377f0682


def build_page(pgno: int, key, mac_key, seed: int = 1, salt: bytes = b''):
    """按 sync_engine 的页格式构造 (密文页, decrypt_page 的期望输出)。

    页布局：[salt(仅首页16B)] + AES-CBC密文 + IV(16) + HMAC-SHA512(64)；
    decrypt_page 输出 = 明文体 + IV + HMAC（保留区原样保留）。
    """
    offset = SALT_SIZE if pgno == 1 else 0
    body_len = PAGE_SIZE - RESERVE - offset
    body = bytes([(seed * 31 + i) & 0xff for i in range(body_len)])
    iv = bytes([(seed + i) & 0xff for i in range(16)])
    ct = AES.new(key, AES.MODE_CBC, iv).encrypt(body)
    mac = hmac.new(mac_key, ct + iv, SHA512)
    mac.update(struct.pack('<I', pgno))
    reserve = iv + mac.digest()
    page = (salt if pgno == 1 else b'') + ct + reserve
    expected = body + reserve
    assert len(page) == PAGE_SIZE, len(page)
    return page, expected


class TestDecryptPage(unittest.TestCase):
    def setUp(self):
        self.salt = bytes(range(16))
        self.key, self.mac_key = _derive_keys(PKEY, self.salt)

    def test_derive_keys_deterministic(self):
        k1, m1 = _derive_keys(PKEY, self.salt)
        k2, m2 = _derive_keys(PKEY, self.salt)
        self.assertEqual(k1, k2)
        self.assertEqual(m1, m2)
        self.assertEqual(len(k1), 32)
        k3, _ = _derive_keys(PKEY, self.salt + b'x')
        self.assertNotEqual(k1, k3)

    def test_roundtrip_normal_page(self):
        page, expected = build_page(2, self.key, self.mac_key, seed=2)
        self.assertEqual(decrypt_page(page, 2, self.key, self.mac_key), expected)

    def test_roundtrip_first_page(self):
        page, expected = build_page(1, self.key, self.mac_key, seed=1, salt=self.salt)
        self.assertEqual(len(page), PAGE_SIZE)
        self.assertEqual(len(expected), PAGE_SIZE - SALT_SIZE)
        self.assertEqual(decrypt_page(page, 1, self.key, self.mac_key), expected)

    def test_hmac_tamper_rejected(self):
        page, _ = build_page(2, self.key, self.mac_key, seed=3)
        page = bytearray(page)
        page[100] ^= 0xff
        self.assertIsNone(decrypt_page(bytes(page), 2, self.key, self.mac_key))

    def test_wrong_size_rejected(self):
        self.assertIsNone(decrypt_page(b'\x00' * 100, 2, self.key, self.mac_key))


class TestMergeWal(unittest.TestCase):
    def setUp(self):
        self.salt = bytes(range(16))
        self.key, self.mac_key = _derive_keys(PKEY, self.salt)
        self.tmp = tempfile.TemporaryDirectory()
        self.dec_db = os.path.join(self.tmp.name, 'dec.db')
        page1 = b'SQLite format 3\x00' + b'\x11' * (PAGE_SIZE - 16)
        page2_old = b'\x22' * PAGE_SIZE
        with open(self.dec_db, 'wb') as f:
            f.write(page1 + page2_old)
        # 加密主库只需要 salt 头
        self.enc_main = os.path.join(self.tmp.name, 'enc.db')
        with open(self.enc_main, 'wb') as f:
            f.write(self.salt)

    def tearDown(self):
        self.tmp.cleanup()

    def _wal(self, frames):
        """frames: [(pgno, commit, seed, salt_match)]，单一代的 wal 文件"""
        s1, s2 = 111, 222
        out = struct.pack('>8I', WAL_MAGIC, 1, PAGE_SIZE, 0, s1, s2, 0, 0)
        pages = {}
        for pgno, commit, seed, salt_match in frames:
            fs = (s1, s2) if salt_match else (s1 + 1, s2 + 1)
            page, expected = build_page(pgno, self.key, self.mac_key, seed=seed)
            pages[seed] = (pgno, expected)
            out += struct.pack('>6I', pgno, commit, *fs, 0, 0) + page
        return out, pages

    def test_merge_committed_frames(self):
        wal_path = os.path.join(self.tmp.name, 'x.db-wal')
        with open(wal_path, 'wb') as f:
            wal, pages = self._wal([(2, 0, 21, True), (3, 1, 31, True)])
            f.write(wal)
        merged = sync_engine.merge_wal(PKEY, self.dec_db, self.enc_main, wal_path)
        self.assertEqual(merged, 2)
        with open(self.dec_db, 'rb') as f:
            data = f.read()
        self.assertEqual(len(data), 3 * PAGE_SIZE)
        self.assertEqual(data[PAGE_SIZE:2 * PAGE_SIZE], pages[21][1])
        self.assertEqual(data[2 * PAGE_SIZE:3 * PAGE_SIZE], pages[31][1])

    def test_stale_and_uncommitted_frames_ignored(self):
        wal_path = os.path.join(self.tmp.name, 'x.db-wal')
        with open(wal_path, 'wb') as f:
            # 上一代残留帧(盐不匹配) + 已提交帧 + 未提交帧
            wal, pages = self._wal([(2, 0, 99, False), (2, 1, 22, True), (3, 0, 32, True)])
            f.write(wal)
        merged = sync_engine.merge_wal(PKEY, self.dec_db, self.enc_main, wal_path)
        self.assertEqual(merged, 1)
        with open(self.dec_db, 'rb') as f:
            data = f.read()
        self.assertEqual(data[PAGE_SIZE:2 * PAGE_SIZE], pages[22][1])
        self.assertEqual(len(data), 2 * PAGE_SIZE)  # 未提交帧未扩展文件

    def test_empty_or_missing_wal(self):
        self.assertEqual(sync_engine.merge_wal(PKEY, self.dec_db, self.enc_main,
                                               os.path.join(self.tmp.name, 'none')), 0)
        empty = os.path.join(self.tmp.name, 'e.db-wal')
        with open(empty, 'wb') as f:
            f.write(struct.pack('>8I', WAL_MAGIC, 1, PAGE_SIZE, 0, 1, 2, 0, 0))
        self.assertEqual(sync_engine.merge_wal(PKEY, self.dec_db, self.enc_main, empty), 0)


class TestChangeDetection(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = os.path.join(self.tmp.name, 'db_storage')
        os.makedirs(os.path.join(self.root, 'message'))
        with open(os.path.join(self.root, 'message', 'message_0.db'), 'wb') as f:
            f.write(b'\x01' * 100)
        with open(os.path.join(self.root, 'message', 'message_0.db-wal'), 'wb') as f:
            f.write(b'\x02' * 50)

    def tearDown(self):
        self.tmp.cleanup()

    def test_all_new_when_no_state(self):
        changed = find_changed(self.root, {})
        self.assertEqual(changed, [os.path.join('message', 'message_0.db')])

    def test_unchanged_when_sig_recorded(self):
        rel = os.path.join('message', 'message_0.db')
        sig = _db_sig(self.root, rel)
        self.assertEqual(find_changed(self.root, {rel: sig}), [])
        # wal 变化也算变更
        with open(os.path.join(self.root, 'message', 'message_0.db-wal'), 'ab') as f:
            f.write(b'\x02' * 10)
        self.assertEqual(find_changed(self.root, {rel: sig}), [rel])

    def test_state_roundtrip(self):
        rel = os.path.join('message', 'message_0.db')
        out_root = self.tmp.name
        os.makedirs(os.path.join(out_root, 'wxid_test'))  # save_sync_state 不建目录
        save_sync_state(out_root, 'wxid_test', {rel: _db_sig(self.root, rel)})
        state = load_sync_state(out_root, 'wxid_test')
        self.assertEqual(state[rel], _db_sig(self.root, rel))
        self.assertEqual(find_changed(self.root, state), [])

    def test_stable_copy(self):
        src = os.path.join(self.root, 'message', 'message_0.db')
        dst = os.path.join(self.tmp.name, 'copy.db')
        self.assertTrue(stable_copy(src, dst))
        with open(src, 'rb') as a, open(dst, 'rb') as b:
            self.assertEqual(a.read(), b.read())
        self.assertFalse(stable_copy(os.path.join(self.tmp.name, 'nope'), dst))


if __name__ == '__main__':
    unittest.main()
