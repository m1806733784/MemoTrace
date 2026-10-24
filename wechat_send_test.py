# -*- coding: utf-8 -*-
"""微信 PC 窗口键盘自动化测试：给文件传输助手发一条消息，每步截图验证"""
import os
import sys
import time

import uiautomation as auto
from PIL import ImageGrab

from wechat_channel import _find_wechat_window

SHOT_DIR = os.path.join(os.environ['TEMP'], 'wx_shots')
os.makedirs(SHOT_DIR, exist_ok=True)


def shot(name):
    win = _find_wechat_window()
    if win is None:
        print('!! 微信窗口未找到')
        return
    r = win.BoundingRectangle
    bbox = (max(r.left, 0), max(r.top, 0), r.right, r.bottom)
    img = ImageGrab.grab(bbox=all_bbox if name == 'full' else bbox)
    p = os.path.join(SHOT_DIR, name + '.png')
    img.save(p)
    print('shot:', p)


all_bbox = None


def main():
    global all_bbox
    win = _find_wechat_window(per_class_timeout=3)
    if win is None:
        print('!! 微信窗口未找到')
        return 1
    win.SetActive()
    time.sleep(0.8)
    r = win.BoundingRectangle
    all_bbox = (max(r.left, 0), max(r.top, 0), r.right, r.bottom)
    print('window rect:', all_bbox)
    shot('1_activated')

    # Ctrl+F 搜索
    auto.SendKeys('{Ctrl}f', waitTime=0.1)
    time.sleep(0.6)
    auto.SetClipboardText('文件传输助手')
    auto.SendKeys('{Ctrl}v', waitTime=0.1)
    time.sleep(1.2)
    shot('2_searched')

    # 回车打开第一个结果
    auto.SendKeys('{Enter}', waitTime=0.1)
    time.sleep(0.8)
    shot('3_chat_opened')

    # 粘贴消息并发送
    msg = '【留痕·自主模式测试】主人你好，这是通过微信自动化发出的测试消息。收到请回复任意内容，验证双向通道。'
    auto.SetClipboardText(msg)
    auto.SendKeys('{Ctrl}v', waitTime=0.1)
    time.sleep(0.5)
    shot('4_typed')
    auto.SendKeys('{Enter}', waitTime=0.1)
    time.sleep(0.8)
    shot('5_sent')
    print('done')
    return 0


if __name__ == '__main__':
    sys.exit(main())
