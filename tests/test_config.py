#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""config 数据目录解析单测：环境变量 > config_local.json > 默认路径"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import config


class TestGetDataRoot(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.local_file = os.path.join(self.tmp.name, 'config_local.json')

    def tearDown(self):
        self.tmp.cleanup()

    def _patch_local(self, data_root=None):
        if data_root is not None:
            with open(self.local_file, 'w', encoding='utf-8') as f:
                json.dump({'data_root': data_root}, f)
        return mock.patch.object(config, '_LOCAL_CONFIG_FILE', self.local_file)

    def test_env_var_wins(self):
        with self._patch_local('D:\\from_file'), \
                mock.patch.dict(os.environ, {'MEMOTRACE_DATA_DIR': 'D:\\from_env'}):
            self.assertEqual(config.get_data_root(), 'D:\\from_env')

    def test_local_file_overrides_default(self):
        with self._patch_local('D:\\from_file'), \
                mock.patch.dict(os.environ, {}, clear=True):
            if 'MEMOTRACE_DATA_DIR' in os.environ:
                del os.environ['MEMOTRACE_DATA_DIR']
            self.assertEqual(config.get_data_root(), 'D:\\from_file')

    def test_default_when_no_env_no_file(self):
        path = os.path.join(self.tmp.name, 'missing.json')
        with mock.patch.object(config, '_LOCAL_CONFIG_FILE', path):
            os.environ.pop('MEMOTRACE_DATA_DIR', None)
            self.assertEqual(config.get_data_root(), config._DEFAULT_ROOT)

    def test_broken_local_file_falls_back_to_default(self):
        with open(self.local_file, 'w', encoding='utf-8') as f:
            f.write('{ not json')
        with mock.patch.object(config, '_LOCAL_CONFIG_FILE', self.local_file):
            os.environ.pop('MEMOTRACE_DATA_DIR', None)
            self.assertEqual(config.get_data_root(), config._DEFAULT_ROOT)

    def test_frozen_mode_reads_config_beside_exe(self):
        exe_side = os.path.join(self.tmp.name, 'appdir', 'config_local.json')
        os.makedirs(os.path.dirname(exe_side), exist_ok=True)
        with open(exe_side, 'w', encoding='utf-8') as f:
            json.dump({'data_root': 'E:\\exe_side_data'}, f)
        with mock.patch.object(config.sys, 'frozen', True, create=True), \
                mock.patch.object(config.sys, 'executable', os.path.join(self.tmp.name, 'appdir', 'webui.exe')), \
                mock.patch.object(config, '_LOCAL_CONFIG_FILE', os.path.join(self.tmp.name, 'missing.json')):
            os.environ.pop('MEMOTRACE_DATA_DIR', None)
            self.assertEqual(config.get_data_root(), 'E:\\exe_side_data')


if __name__ == '__main__':
    unittest.main()
