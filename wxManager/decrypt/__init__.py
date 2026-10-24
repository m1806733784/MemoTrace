#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
@Time        : 2025/1/10 2:34
@Author      : SiYuan
@Email       : 863909694@qq.com
@File        : wxManager-__init__.py.py
@Description :
"""
from typing import List

import psutil

from wxManager.decrypt.wx_info_v4 import dump_wechat_info_v4
from wxManager.decrypt.common import WeChatInfo


def get_info_v4() -> List[WeChatInfo]:
    result_v4 = []
    for process in psutil.process_iter(['name', 'exe', 'pid']):
        if process.name() == 'Weixin.exe':
            wechat_base_address = 0
            for module in process.memory_maps(grouped=False):
                if module.path and 'Weixin.dll' in module.path:
                    wechat_base_address = int(module.addr, 16)
                    break
            if wechat_base_address == 0:
                continue
            pid = process.pid
            wxinfo = dump_wechat_info_v4(pid)
            result_v4.append(
                wxinfo
            )
    return result_v4


if __name__ == "__main__":
    for wx_info in get_info_v4():
        print(wx_info)
