#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""密钥安全存储：key.json 用 Windows DPAPI 加密（仅当前用户可解，其他程序/用户读不走）。

兼容：读取时自动识别旧版明文 json 并就地升级为加密格式；解密失败（换机器/换用户/损坏）
返回 None，调用方回落到现场提取密钥。
"""
import json
import os
import time


def _protect(data: bytes) -> bytes:
    import win32crypt
    return win32crypt.CryptProtectData(data, 'MemoTrace', None, None, None, 0)


def _unprotect(data: bytes) -> bytes:
    import win32crypt
    return win32crypt.CryptUnprotectData(data, None, None, None, 0)[1]


def save_key_file(path: str, key_hex: str, wx_dir: str):
    """DPAPI 加密后原子写入（临时文件 + 替换）"""
    payload = json.dumps({'key': key_hex, 'wx_dir': wx_dir, 'time': time.time()},
                         ensure_ascii=False)
    blob = _protect(payload.encode('utf-8'))
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'wb') as f:
        f.write(blob)
    os.replace(tmp, path)


def load_key_file(path: str):
    """返回 {'key','wx_dir',...} 或 None；旧明文格式读取成功后自动升级为加密"""
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except OSError:
        return None
    if not raw:
        return None
    if raw[:1] == b'{':  # 旧版明文 json：读出后升级加密
        try:
            data = json.loads(raw.decode('utf-8'))
        except (UnicodeDecodeError, ValueError):
            return None
        if data.get('key'):
            try:
                save_key_file(path, data['key'], data.get('wx_dir', ''))
            except Exception:
                pass  # 升级失败不影响本次使用
        return data
    try:
        return json.loads(_unprotect(raw).decode('utf-8'))
    except Exception:
        return None  # 解不开：调用方回落现场提取
