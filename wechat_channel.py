# -*- coding: utf-8 -*-
"""
微信通道（PC 端键盘自动化）：自主模式下向主人提问/汇报用
  发送：激活微信窗口 -> Ctrl+F 搜索联系人 -> 粘贴 -> 回车进聊天 -> 粘贴 -> 回车发送
        发送后同步解密库验证消息真的发出（UI 自动化可能静默失败），失败自动重试一次
  接收：同步后从解密库读该联系人的最新回复（文件传输助手的回复记录为"自发"，特殊处理）

限制：微信 4.x 不暴露 UIA 控件树只能走键盘模拟；**屏幕锁定时无法发送**（系统拦截输入）
用法：
  python wechat_channel.py send <联系人名> <消息>
  python wechat_channel.py ask <联系人名> <问题> [--timeout 300]   # 发送并等回复
"""
import os
import sqlite3
import sys
import time

import uiautomation as auto

from webui.bootstrap import OUT_ROOT, find_accounts, open_database

FILE_HELPER_NAMES = ('文件传输助手', 'filehelper')


def _wx_account():
    """返回 (wxid, wx_dir)：以 key.json 为准（DPAPI 加密，见 secure_store）"""
    from secure_store import load_key_file
    for account in find_accounts(OUT_ROOT):
        if os.path.isfile(account['key_file']):
            data = load_key_file(account['key_file'])
            if data:
                return account['wxid'], data.get('wx_dir', '')
    return None, None


def resolve_contact(name: str) -> str:
    """联系人显示名 -> username（文件传输助手特判；否则查 contact.db 的备注/昵称）"""
    if name in FILE_HELPER_NAMES:
        return 'filehelper'
    wxid, _ = _wx_account()
    if not wxid:
        return name
    cdb = os.path.join(OUT_ROOT, wxid, 'db_storage', 'contact', 'contact.db')
    try:
        conn = sqlite3.connect(cdb)
        row = conn.execute(
            "select username from contact where remark=? or nick_name=? limit 1",
            (name, name)).fetchone()
        conn.close()
        return row[0] if row else name
    except Exception:
        return name


# 微信 4.x 主窗口 Qt 类名（随 Qt 版本变化，逐个尝试；升级适配时往这里加）
WECHAT_WINDOW_CLASSES = ('Qt51514QWindowIcon', 'Qt51514QWindowToolSaveBits', 'Qt6QWindowIcon')


def _find_wechat_window(per_class_timeout=1):
    for class_name in WECHAT_WINDOW_CLASSES:
        win = auto.WindowControl(searchDepth=1, ClassName=class_name, Name='微信')
        if win.Exists(per_class_timeout):
            return win
    return None


def _send_keys_robust(contact, message):
    win = _find_wechat_window()
    if win is None:
        print('[!] 未找到微信窗口（微信需在本机运行）')
        return
    win.SetActive()
    time.sleep(0.8)
    auto.SendKeys('{Ctrl}f', waitTime=0.1)
    time.sleep(0.6)
    auto.SetClipboardText(contact)
    auto.SendKeys('{Ctrl}v', waitTime=0.1)
    time.sleep(1.3)
    auto.SendKeys('{Enter}', waitTime=0.1)
    time.sleep(0.8)
    auto.SendKeys('{Esc}', waitTime=0.1)   # 收起残留的搜索面板
    time.sleep(0.3)
    auto.SetClipboardText(message)
    auto.SendKeys('{Ctrl}v', waitTime=0.1)
    time.sleep(0.5)
    auto.SendKeys('{Enter}', waitTime=0.1)
    time.sleep(0.6)


# ---------------------------------------------------------------- 库读取
def _open_db():
    wxid, _ = _wx_account()
    if not wxid:
        return None, None
    return open_database(os.path.join(OUT_ROOT, wxid, 'db_storage')), wxid


def _close_db(db):
    if db:
        db.close()


def _refresh(wxid, wx_dir):
    from webui.sync_engine import sync_databases, has_changes
    if has_changes(wx_dir, OUT_ROOT, wxid):
        sync_databases(wx_dir, OUT_ROOT, wxid)


def send_message(contact: str, message: str, verify: bool = True) -> bool:
    """发送并验证落库；verify=False 时只执行发送动作（用于测试/重试内部）"""
    username = resolve_contact(contact)
    wxid, wx_dir = _wx_account()
    if not wxid:
        print('[!] 无已解密账号')
        return False
    sent_before = time.time() - 5
    _send_keys_robust(contact, message)
    if not verify:
        return True
    from wxManager import MessageType
    for attempt in range(2):          # 验证 + 重试一次
        time.sleep(6)
        db = None
        try:
            _refresh(wxid, wx_dir)
            db, _ = _open_db()
            if db:
                msgs, _ = db.get_messages_by_num(username, 2 ** 62, 10)
                for m in msgs:
                    if m.timestamp >= sent_before and m.type == MessageType.Text \
                            and m.content and message[:20] in m.content:
                        return True                    # 已落库 = 真的发出去了
        except Exception as e:
            print(f'[!] 验证异常: {e}')
        finally:
            if db:
                _close_db(db)
        if attempt == 0:
            print('[!] 未检测到消息落库，重试发送...')
            _send_keys_robust(contact, message)
    return False


def wait_reply(contact: str, after_ts: float, timeout: int = 300):
    """同步并轮询解密库等回复。返回 (文本, ts) 或 (None, None)
    文件传输助手特殊：主人在手机上的回复也记录为 is_sender=1（自发），
    所以对该会话取「时间晚于问题、且文本不是我们刚发的问题」的最新文本。"""
    from wxManager import MessageType
    username = resolve_contact(contact)
    wxid, wx_dir = _wx_account()
    if not wxid:
        return None, None
    is_fh = username == 'filehelper'
    deadline = time.time() + timeout
    db = None
    while time.time() < deadline:
        time.sleep(10)
        try:
            _refresh(wxid, wx_dir)
            if db is None:
                db, _ = _open_db()
            if not db:
                continue
            msgs, _ = db.get_messages_by_num(username, 2 ** 62, 10)
            for m in reversed(msgs):        # 正序找最新的合格回复
                if m.timestamp <= after_ts or m.type != MessageType.Text or not m.content:
                    continue
                if not m.is_sender:         # 普通联系人：对方的回复
                    return m.content, m.timestamp
                if is_fh and not m.content.startswith('【'):   # 文件助手：非通知格式的"自发"文本=主人回复
                    return m.content, m.timestamp
        except Exception as e:
            print(f'[!] 轮询异常: {e}')
    if db:
        _close_db(db)
    return None, None


def main():
    if len(sys.argv) < 4 or sys.argv[1] not in ('send', 'ask'):
        print(__doc__)
        return 1
    action, contact, message = sys.argv[1], sys.argv[2], sys.argv[3]
    timeout = 300
    if '--timeout' in sys.argv:
        timeout = int(sys.argv[sys.argv.index('--timeout') + 1])
    t_question = time.time()
    if not send_message(contact, message):
        print('[FAIL] 发送未落库（屏幕可能锁定，或微信窗口异常）')
        return 1
    print('[OK] 已发送并验证')
    if action == 'ask':
        print(f'[*] 等待回复（最长 {timeout}s，每 10 秒同步检查）...')
        reply, ts = wait_reply(contact, t_question, timeout)
        if reply:
            from datetime import datetime
            print(f'[REPLY {datetime.fromtimestamp(ts):%H:%M:%S}] {reply}')
            return 0
        print('[!] 超时未收到回复')
        return 2
    return 0


if __name__ == '__main__':
    sys.exit(main())
