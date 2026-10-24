#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""账号引导：统一「找已解密账号 → 设置 Me 单例 → 连接数据库」流程。

webui/app.py、run_memotrace.py、wechat_channel.py 共用，替代各自重复的实现。
"""
import json
import os
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from config import get_data_root

OUT_ROOT = get_data_root()


def find_accounts(out_root=OUT_ROOT) -> list:
    """返回 [{wxid, db_dir, key_file}]：目录下有解密库或 key.json 即算账号"""
    accounts = []
    if not os.path.isdir(out_root):
        return accounts
    for d in os.listdir(out_root):
        db_dir = os.path.join(out_root, d, 'db_storage')
        key_file = os.path.join(out_root, d, 'key.json')
        if os.path.isfile(os.path.join(db_dir, 'contact', 'contact.db')) or os.path.isfile(key_file):
            accounts.append({'wxid': d, 'db_dir': db_dir, 'key_file': key_file})
    return accounts


def setup_me(wxid: str, db_dir: str):
    """从 info.json 恢复 Me 单例（wx_dir/xor_key 媒体路由与导出器依赖）"""
    from wxManager import Me
    me = Me()
    me.wxid = wxid
    info_file = os.path.join(db_dir, 'info.json')
    if os.path.isfile(info_file):
        try:
            with open(info_file, encoding='utf-8') as f:
                info = json.load(f)
            me.wx_dir = info.get('wx_dir', '')
            me.name = info.get('name', '') or wxid
            me.xor_key = info.get('xor_key', 0)
        except (OSError, ValueError):
            pass
    return me


def ensure_decrypted(out_root=OUT_ROOT, verbose=False) -> str:
    """现场提取密钥并解密（微信需已登录）；已有解密数据时只刷新 key.json/info.json。
    返回 db_dir。"""
    from wxManager import Me
    from wxManager.decrypt import get_info_v4, decrypt_v4
    from wxManager.decrypt.decrypt_dat import get_decode_code_v4

    infos = get_info_v4()
    if verbose:
        for info in infos:
            print(f'    {info}')
    target = next((i for i in infos if i.key), None)
    if target is None:
        raise RuntimeError('未提取到微信密钥，请先登录微信后重试')

    dest = os.path.join(out_root, target.wxid)
    db_dir = os.path.join(dest, 'db_storage')
    os.makedirs(db_dir, exist_ok=True)
    # 密钥持久化（增量同步复用，避免每次扫内存）；DPAPI 加密存储
    try:
        from secure_store import save_key_file
        save_key_file(os.path.join(dest, 'key.json'), target.key, target.wx_dir)
    except Exception:
        pass

    me = Me()
    me.wxid, me.wx_dir = target.wxid, target.wx_dir
    me.name = target.nick_name or target.wxid
    try:
        me.xor_key = get_decode_code_v4(target.wx_dir)
    except Exception:
        me.xor_key = 0
    with open(os.path.join(db_dir, 'info.json'), 'w', encoding='utf-8') as f:
        json.dump(me.to_json(), f, ensure_ascii=False, indent=4)

    if not os.path.isfile(os.path.join(db_dir, 'contact', 'contact.db')):
        print(f'    正在解密数据库 -> {dest}（首次运行约十几秒）...')
        decrypt_v4.decrypt_db_files(target.key, src_dir=target.wx_dir, dest_dir=dest)
    else:
        print('    已有解密数据，跳过解密')
    return db_dir


def init_account(out_root=OUT_ROOT):
    """返回 (wxid, db_dir)：优先复用已解密账号，没有则现场提取并解密"""
    candidates = [a for a in find_accounts(out_root)
                  if os.path.isfile(os.path.join(a['db_dir'], 'contact', 'contact.db'))]
    if not candidates:
        db_dir = ensure_decrypted(out_root)
        from wxManager import Me
        return Me().wxid, db_dir
    account = candidates[0]  # 单账号场景
    setup_me(account['wxid'], account['db_dir'])
    return account['wxid'], account['db_dir']


def open_database(db_dir):
    """打开解密库并返回接口；失败抛 RuntimeError"""
    from wxManager import DatabaseConnection
    interface = DatabaseConnection(db_dir, 4).get_interface()
    if interface is None:
        raise RuntimeError(f'数据库初始化失败: {db_dir}')
    return interface
