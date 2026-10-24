import logging
import os
import time
import traceback
from functools import wraps

filename = time.strftime("%Y-%m-%d", time.localtime())


def _log_dir() -> str:
    """日志目录：本地应用数据目录优先，临时目录兜底，绝不再写当前工作目录"""
    candidates = []
    local_appdata = os.environ.get('LOCALAPPDATA')
    if local_appdata:
        candidates.append(os.path.join(local_appdata, 'MemoTrace', 'logs'))
    temp = os.environ.get('TEMP') or os.path.gettempdir()
    candidates.append(os.path.join(temp, 'MemoTrace', 'logs'))
    for d in candidates:
        try:
            os.makedirs(d, exist_ok=True)
            return d
        except OSError:
            continue
    return os.path.gettempdir()


logger = logging.getLogger('MemoTrace')
logger.setLevel(level=logging.DEBUG)
formatter = logging.Formatter('%(asctime)s - %(filename)s[line:%(lineno)d] - %(levelname)s: %(message)s')
try:
    file_handler = logging.FileHandler(os.path.join(_log_dir(), f'{filename}-log.log'), encoding='utf-8')
    file_handler.setLevel(level=logging.INFO)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
except OSError:
    pass  # 文件日志不可用时仅保留控制台输出

stream_handler = logging.StreamHandler()
stream_handler.setLevel(logging.DEBUG)
stream_handler.setFormatter(formatter)
logger.addHandler(stream_handler)


def log(func):
    """记录异常完整堆栈后原样抛出（此前静默吞掉返回 None，会掩盖故障）"""
    @wraps(func)
    def log_(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except Exception:
            logger.error(
                f"\n{func.__qualname__} is error,params:{(args, kwargs)},here are details:\n{traceback.format_exc()}")
            raise
    return log_
