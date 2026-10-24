#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
全局配置：解密数据输出根目录（OUT_ROOT）解析
优先级：环境变量 MEMOTRACE_DATA_DIR > 仓库根 config_local.json > ~/Documents/MemoTrace
config_local.json 已在 .gitignore 中忽略，用于本机指定数据盘。
"""
import json
import os

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_LOCAL_CONFIG_FILE = os.path.join(PROJECT_ROOT, 'config_local.json')
_DEFAULT_ROOT = os.path.join(os.path.expanduser('~'), 'Documents', 'MemoTrace')


def get_data_root() -> str:
    env = os.environ.get('MEMOTRACE_DATA_DIR')
    if env:
        return env
    if os.path.isfile(_LOCAL_CONFIG_FILE):
        try:
            with open(_LOCAL_CONFIG_FILE, encoding='utf-8') as f:
                value = json.load(f).get('data_root', '')
            if value:
                return value
        except (OSError, ValueError):
            pass
    return _DEFAULT_ROOT


if __name__ == '__main__':
    print(get_data_root())
