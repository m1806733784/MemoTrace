#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""decrypt_dat 图片解密单测：XOR/V1 AES/V2 AES 格式与文件落盘"""
import os
import struct
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from wxManager.decrypt import decrypt_dat
from wxManager.decrypt.decrypt_dat import (AES_KEY_MAP, V2_MAGIC, clean_wxid,
                                           decode_dat, decode_dat_v41,
                                           decode_dat_v4, get_code,
                                           get_image_type)

V1_MAGIC = b'\x07\x08V1\x08\x07'
V2_FIXED_KEY = b'43e7d25eb1b9bb64'
XOR_KEY = 0xAB


def xor(data: bytes, key: int) -> bytes:
    return bytes(b ^ key for b in data)


class TestPureHelpers(unittest.TestCase):
    def test_clean_wxid(self):
        self.assertEqual(clean_wxid('wxid_abc_96c0'), 'wxid_abc')
        self.assertEqual(clean_wxid('wxid_ab'), 'wxid_ab')
        self.assertEqual(clean_wxid('foo_bar_baz'), 'foo_bar_baz')

    def test_get_code_jpg(self):
        header = xor(b'\xff\xd8', XOR_KEY)
        file_type, code = get_code(header)
        self.assertEqual((file_type, code), (1, XOR_KEY))

    def test_get_code_png(self):
        header = xor(b'\x89\x50', XOR_KEY)
        file_type, code = get_code(header)
        self.assertEqual((file_type, code), (3, XOR_KEY))

    def test_get_code_unknown(self):
        self.assertEqual(get_code(b'\x00\x00'), (-1, -1))

    def test_get_image_type(self):
        self.assertEqual(get_image_type(b'\xff\xd8\xff\xe0'), 'jpg')
        self.assertEqual(get_image_type(b'\x89PNG\r\n\x1a\n'), 'png')
        self.assertEqual(get_image_type(b'GIF89a'), 'gif')
        self.assertEqual(get_image_type(b'what'), 'bin')


class TestDecodeDatV3Xor(unittest.TestCase):
    """4.0 之前的整文件单字节 XOR 格式（decode_dat 走 get_code 分支）"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.jpg = b'\xff\xd8\xff\xe0' + os.urandom(200)
        self.dat = os.path.join(self.tmp.name, 'img.dat')
        with open(self.dat, 'wb') as f:
            f.write(xor(self.jpg, XOR_KEY))

    def tearDown(self):
        self.tmp.cleanup()

    def test_roundtrip(self):
        out = decode_dat(XOR_KEY, self.dat, self.tmp.name, 'result')
        self.assertTrue(out.endswith('result.jpg'))
        with open(out, 'rb') as f:
            self.assertEqual(f.read(), self.jpg)


class TestDecodeDatV4(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        decrypt_dat._v2_resolved = None  # 隔离全局缓存

    def tearDown(self):
        self.tmp.cleanup()

    def write_dat(self, name: str, data: bytes) -> str:
        path = os.path.join(self.tmp.name, name)
        with open(path, 'wb') as f:
            f.write(data)
        return path

    def test_plain_image_passthrough(self):
        png = b'\x89PNG\r\n\x1a\n' + os.urandom(50)
        path = self.write_dat('plain.dat', png)
        out = decode_dat_v4(XOR_KEY, path, self.tmp.name, 'plain_out')
        self.assertTrue(out.endswith('plain_out.png'))
        with open(out, 'rb') as f:
            self.assertEqual(f.read(), png)

    def test_v1_fixed_key_format(self):
        """V1：6字节magic + <H>加密长度 + AES-ECB 头部 + XOR 尾部"""
        from Crypto.Cipher import AES
        head_plain = b'\xff\xd8\xff\xe0' + b'A' * 26          # 30 字节
        padded = head_plain + b'\x02\x02'                       # PKCS7 到 32
        enc = AES.new(AES_KEY_MAP[V1_MAGIC], AES.MODE_ECB).encrypt(padded)
        tail_plain = b'T' * 30
        # V1 头 15 字节：magic(6) + <H>encrypt_length(2) + 7 字节保留
        header = V1_MAGIC + struct.pack('<H', 31) + b'\x00' * 7
        dat = header + enc + xor(tail_plain, XOR_KEY)
        path = self.write_dat('v1.dat', dat)
        out = decode_dat_v4(XOR_KEY, path, self.tmp.name, 'v1_out')
        self.assertTrue(out.endswith('v1_out.jpg'), out)
        with open(out, 'rb') as f:
            self.assertEqual(f.read(), head_plain + tail_plain)

    def test_v41_v2_format(self):
        """V2 (4.1)：magic + 双 <I> 长度 + 1字节 + AES-ECB 头 + 16字节分隔 + XOR 尾"""
        from Crypto.Cipher import AES
        head_plain = b'\xff\xd8\xff\xe0' + b'B' * 28          # 32 字节
        tail_plain = b'Q' * 40
        head_enc = AES.new(V2_FIXED_KEY, AES.MODE_ECB).encrypt(head_plain)
        data = (V2_MAGIC + struct.pack('<I', len(head_enc))
                + struct.pack('<I', len(tail_plain)) + b'\x01'
                + head_enc + b'\x00' * 16 + xor(tail_plain, XOR_KEY))
        result = decode_dat_v41(data, XOR_KEY)
        self.assertIsNotNone(result)
        self.assertEqual(result, head_plain + tail_plain)

    def test_v41_rejects_garbage(self):
        self.assertIsNone(decode_dat_v41(b'\x07\x08V2', XOR_KEY))
        self.assertIsNone(decode_dat_v41(V2_MAGIC + b'\x00' * 40, XOR_KEY))

    def test_v41_key_resolution_persists(self):
        """首次成功解密后记住密钥组合，后续不依赖候选列表顺序"""
        from Crypto.Cipher import AES
        head_plain = b'\x89PNG\r\n\x1a\n' + b'C' * 24          # 32 字节
        tail_plain = b'R' * 20
        head_enc = AES.new(V2_FIXED_KEY, AES.MODE_ECB).encrypt(head_plain)
        data = (V2_MAGIC + struct.pack('<I', 32) + struct.pack('<I', 20) + b'\x01'
                + head_enc + b'\x00' * 16 + xor(tail_plain, XOR_KEY))
        self.assertIsNotNone(decode_dat_v41(data, XOR_KEY))
        self.assertEqual(decrypt_dat._v2_resolved, (V2_FIXED_KEY, XOR_KEY))
        self.assertIsNotNone(decode_dat_v41(data, XOR_KEY))


if __name__ == '__main__':
    unittest.main()
