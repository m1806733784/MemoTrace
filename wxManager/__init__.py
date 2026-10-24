# -*- coding: utf-8 -*-
"""
@File    : __init__.py.py
@Author  : Shuaikang Zhou
@Time    : 2023/1/5 0:10
@IDE     : Pycharm
@Version : Python3.10
@comment : ···
"""
from .log import logger
from .model import Me, MessageType, Message, Person, Contact, TextMessage, ImageMessage
from .db_main import DataBaseInterface
from .manager_v4 import DataBaseV4

__version__ = '3.1.0'


class DatabaseConnection:
    def __init__(self, db_dir, db_version=4):
        self.db_dir = db_dir
        self.db_version = db_version
        self.database_interface = self._initialize_database()

    def _initialize_database(self) -> DataBaseInterface:
        if self.db_version != 4:
            raise ValueError(f'仅支持微信4.x数据库（db_version=4），收到: {self.db_version}。'
                             f'微信3.x支持已封存至 legacy/v3 分支')
        database0 = DataBaseV4()
        if database0.init_database(self.db_dir):
            return database0
        else:
            logger.error(f'数据库初始化失败, 请检查路径或数据库版本是否正确, db_dir:{self.db_dir}')
            return None

    def get_interface(self) -> DataBaseInterface:
        return self._initialize_database()


"""
使用示例：
conn = DatabaseConnection(USER_DB_DIR, 4)
database: DataBaseInterface = conn.get_interface()
"""
