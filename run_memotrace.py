#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
留痕（MemoTrace）一键运行脚本（本 worktree 为核心库，无 GUI，走示例流程）：
  1. 从运行中的微信 4.x 提取密钥（已适配微信 4.1），刷新 key.json / info.json
  2. 解密当前登录账号的全部数据库 -> OUT_ROOT/<wxid>/（已有解密数据自动跳过）
  3. 打开解密后的数据库，导出最近聊天会话的 HTML
可重复运行。
"""
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, PROJECT_ROOT)

from config import get_data_root
from webui.bootstrap import ensure_decrypted, open_database

OUT_ROOT = get_data_root()


def get_session_username(session):
    """兼容 tuple / sqlite3.Row / dict / 对象四种返回类型"""
    if isinstance(session, (tuple, list)):
        return session[0] if session else None
    try:
        return session['username']
    except (TypeError, IndexError, KeyError):
        return getattr(session, 'username', None)


def main():
    from datetime import date, timedelta
    from exporter import HtmlExporter
    from exporter.config import FileType

    print('[1/3] 提取微信密钥并确保解密数据（微信需已登录）...')
    db_dir = ensure_decrypted(OUT_ROOT, verbose=True)
    from wxManager import Me
    wxid = Me().wxid
    dest = os.path.dirname(db_dir)
    print(f'    账号 {wxid}，数据库: {db_dir}')

    print('[2/3] 打开数据库...')
    database = open_database(db_dir)
    contacts = database.get_contacts()
    print(f'    联系人 {len(contacts)} 个')
    sessions = database.get_session()
    if not sessions:
        print('[!] 没有会话记录')
        return 1

    print('[3/3] 导出最近聊天会话 HTML（限最近 30 天，避免超大会话耗时过久）...')
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
    database.close()
    print('[OK] 全部完成！')
    print(f'     解密数据库: {db_dir}')
    print(f'     HTML 聊天记录: {html_dir}')
    return 0


if __name__ == '__main__':
    from multiprocessing import freeze_support
    freeze_support()
    sys.exit(main())
