#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
留痕（MemoTrace）一键运行脚本（本 worktree 为核心库，无 GUI，走示例流程）：
  1. 从运行中的微信 4.x 提取密钥（已适配微信 4.1）
  2. 解密当前登录账号的全部数据库 -> OUT_ROOT/<wxid>/
  3. 打开解密后的数据库，导出最近聊天会话的 HTML
可重复运行：已有解密数据时自动跳过解密，直接导出。
"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

# 解密数据输出位置（不要放在仓库里）
OUT_ROOT = r'D:\Files\WeChatFiles\MemoTrace_数据'


def get_session_username(session):
    """兼容 tuple / sqlite3.Row / dict / 对象四种返回类型"""
    if isinstance(session, (tuple, list)):
        return session[0] if session else None
    try:
        return session['username']
    except (TypeError, IndexError, KeyError):
        return getattr(session, 'username', None)


def main():
    import json
    import time
    from wxManager import Me, DatabaseConnection
    from wxManager.decrypt import get_info_v4, decrypt_v4
    from wxManager.decrypt.decrypt_dat import get_decode_code_v4
    from exporter import HtmlExporter
    from exporter.config import FileType

    print('[1/4] 提取微信密钥（微信需已登录）...')
    infos = get_info_v4()
    target = None
    for info in infos:
        print(f'    {info}')
        if info.key:
            target = info
    if target is None:
        print('[!] 未提取到密钥，请确认微信已登录后重试')
        return 1

    dest = os.path.join(OUT_ROOT, target.wxid)
    db_dir = os.path.join(dest, 'db_storage')
    os.makedirs(db_dir, exist_ok=True)

    # 设置 Me 单例（导出器读取头像、媒体路径要用）
    me = Me()
    me.wx_dir = target.wx_dir
    me.wxid = target.wxid
    me.name = target.nick_name
    try:
        me.xor_key = get_decode_code_v4(target.wx_dir)
    except Exception:
        me.xor_key = 0
    with open(os.path.join(db_dir, 'info.json'), 'w', encoding='utf-8') as f:
        json.dump(me.to_json(), f, ensure_ascii=False, indent=4)

    print(f'[2/4] 解密数据库 -> {dest}')
    if os.path.isfile(os.path.join(db_dir, 'contact', 'contact.db')):
        print('    已有解密数据，跳过解密')
    else:
        t0 = time.time()
        decrypt_v4.decrypt_db_files(target.key, src_dir=target.wx_dir, dest_dir=dest)
        print(f'    解密完成，耗时 {time.time() - t0:.1f}s')

    print('[3/4] 打开数据库...')
    conn = DatabaseConnection(db_dir, 4)
    database = conn.get_interface()
    contacts = database.get_contacts()
    print(f'    联系人 {len(contacts)} 个')
    sessions = database.get_session()
    if not sessions:
        print('[!] 没有会话记录')
        return 1

    print('[4/4] 导出最近聊天会话 HTML（限最近 30 天，避免超大会话耗时过久）...')
    from datetime import date, timedelta
    end = date.today() + timedelta(days=1)
    start = end - timedelta(days=31)
    time_range = (start, end)

    contact = None
    for session in sessions:
        username = get_session_username(session)
        if not username:
            continue
        contact = database.get_contact_by_username(username)
        if contact is not None:
            break
    if contact is None:
        print('[!] 未找到可导出的联系人')
        return 1
    print(f'    导出对象: {contact.remark}({contact.wxid})')

    output_dir = os.path.join(dest, '导出')
    exporter = HtmlExporter(database, contact, output_dir=output_dir, type_=FileType.HTML,
                            time_range=time_range)
    exporter.start()

    html_dir = os.path.join(output_dir, '聊天记录', f'{contact.remark}({contact.wxid})')
    print(f'[OK] 全部完成！')
    print(f'     解密数据库: {db_dir}')
    print(f'     HTML 聊天记录: {html_dir}')
    return 0


if __name__ == '__main__':
    from multiprocessing import freeze_support
    freeze_support()
    sys.exit(main())
