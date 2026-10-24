#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
全局配置：解密数据输出根目录（OUT_ROOT）解析
优先级：环境变量 MEMOTRACE_DATA_DIR > 仓库根 config_local.json > ~/Documents/MemoTrace
config_local.json 已在 .gitignore 中忽略，用于本机指定数据盘。
"""
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
_LOCAL_CONFIG_FILE = os.path.join(PROJECT_ROOT, 'config_local.json')
_DEFAULT_ROOT = os.path.join(os.path.expanduser('~'), 'Documents', 'MemoTrace')


def _local_config_candidates():
    """本机配置文件候选：打包 exe 时读 exe 同目录，源码运行读仓库根"""
    if getattr(sys, 'frozen', False):
        yield os.path.join(os.path.dirname(sys.executable), 'config_local.json')
    yield _LOCAL_CONFIG_FILE


def get_data_root() -> str:
    env = os.environ.get('MEMOTRACE_DATA_DIR')
    if env:
        return env
    for path in _local_config_candidates():
        if os.path.isfile(path):
            try:
                with open(path, encoding='utf-8') as f:
                    value = json.load(f).get('data_root', '')
                if value:
                    return value
            except (OSError, ValueError):
                pass
    return _DEFAULT_ROOT


if __name__ == '__main__':
    print(get_data_root())
