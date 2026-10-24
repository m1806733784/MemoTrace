#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""db_v4 数据访问层单测：用临时 sqlite 库验证查询行为，不依赖真微信数据"""
import os
import sqlite3
import sys
import tempfile
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from wxManager.db_v4 import ContactDB, SessionDB

CONTACT_SCHEMA = '''
CREATE TABLE contact (
    username TEXT PRIMARY KEY, alias TEXT, local_type INTEGER, flag INTEGER,
    remark TEXT, nick_name TEXT, pin_yin_initial TEXT, remark_pin_yin_initial TEXT,
    small_head_url TEXT, big_head_url TEXT, extra_buffer BLOB, head_img_md5 TEXT,
    chat_room_notify INTEGER, is_in_chat_room INTEGER, description TEXT,
    chat_room_type INTEGER, remark_quan_pin TEXT, quan_pin TEXT
);
CREATE TABLE contact_label (label_id_ INTEGER, label_name_ TEXT);
'''

SESSION_SCHEMA = '''
CREATE TABLE SessionTable (
    username TEXT PRIMARY KEY, type INTEGER, unread_count INTEGER,
    unread_first_msg_srv_id INTEGER, last_timestamp INTEGER, summary TEXT,
    last_msg_type INTEGER, last_msg_sub_type INTEGER, sort_timestamp INTEGER,
    last_sender_display_name TEXT, last_msg_sender TEXT
);
'''


class TestContactDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_dir = os.path.join(self.tmp.name, 'contact')
        os.makedirs(db_dir)
        conn = sqlite3.connect(os.path.join(db_dir, 'contact.db'))
        conn.executescript(CONTACT_SCHEMA)
        conn.execute("insert into contact values ('wxid_a', 'a_alias', 1, 0, "
                     "'备注A', '昵称A', 'A', 'A', '', '', x'', '', 0, 0, '', 0, 'beiA', 'A')")
        conn.execute("insert into contact values ('wxid_b', 'b_alias', 3, 0, "
                     "'', '被过滤的', 'B', 'B', '', '', x'', '', 0, 0, '', 0, '', 'B')")
        conn.execute("insert into contact_label values (10, '朋友')")
        conn.commit()
        conn.close()
        self.db = ContactDB('contact/contact.db')
        self.assertTrue(self.db.init_database(self.tmp.name))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_get_contacts_filters_local_type(self):
        rows = self.db.get_contacts()
        self.assertEqual(len(rows), 1)  # local_type=3 不在 1/2/5 之列
        self.assertEqual(rows[0][0], 'wxid_a')
        self.assertEqual(rows[0][4], '备注A')

    def test_get_contact_by_username(self):
        row = self.db.get_contact_by_username('wxid_b')
        self.assertIsNotNone(row)
        self.assertEqual(row[5], '被过滤的')
        self.assertIsNone(self.db.get_contact_by_username('nobody'))

    def test_get_label_by_id(self):
        self.assertEqual(self.db.get_label_by_id(10), '朋友')
        self.assertEqual(self.db.get_label_by_id(99), '')

    def test_get_labels(self):
        self.assertEqual(self.db.get_labels('10,10'), '朋友,朋友')
        self.assertEqual(self.db.get_labels(''), '')


class TestSessionDB(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_dir = os.path.join(self.tmp.name, 'session')
        os.makedirs(db_dir)
        conn = sqlite3.connect(os.path.join(db_dir, 'session.db'))
        conn.executescript(SESSION_SCHEMA)
        conn.execute("insert into SessionTable values "
                     "('wxid_old', 1, 0, 0, 1000, '旧消息', 1, 0, 1000, '', '')")
        conn.execute("insert into SessionTable values "
                     "('wxid_new', 1, 2, 0, 2000, '新消息', 1, 0, 2000, '', '')")
        conn.commit()
        conn.close()
        self.db = SessionDB('session/session.db')
        self.assertTrue(self.db.init_database(self.tmp.name))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_get_session_ordered_by_sort(self):
        rows = self.db.get_session()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][0], 'wxid_new')  # sort_timestamp 大的在前
        self.assertEqual(rows[0][5], '新消息')


if __name__ == '__main__':
    unittest.main()
