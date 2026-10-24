# -*- coding: utf-8 -*-
"""
增量同步引擎：把微信 4.x 实时数据同步到已解密数据库目录
  1. 变更检测覆盖主库与 -wal（WAL 模式下新消息只写 wal，主库 mtime 不变）
  2. 复制变更库（含 -wal）-> 解密主库 -> 合并 WAL 已提交帧 -> quick_check
  3. 原子替换解密库（清掉旧 -wal/-shm），记录 sync_state.json
密钥从 key.json 读取；缺失且微信在运行时现场提取并持久化。
调用方负责在替换前关闭解密库的全部 sqlite 连接。
"""
import hmac
import json
import os
import shutil
import sqlite3
import struct
import tempfile
import time
from contextlib import contextmanager

from Crypto.Cipher import AES
from Crypto.Hash import SHA512
from Crypto.Protocol.KDF import PBKDF2

PAGE_SIZE = 4096
SALT_SIZE = 16
RESERVE = 80  # IV(16) + HMAC-SHA512(64)


class _LockBusy(RuntimeError):
    pass


@contextmanager
def _file_lock(path: str):
    """跨进程文件锁（Windows msvcrt 字节锁）；拿不到时抛 _LockBusy"""
    import msvcrt
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    f = open(path, 'a+b')
    try:
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            raise _LockBusy(path)
        try:
            yield
        finally:
            try:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
    finally:
        f.close()


# ---------------------------------------------------------------- 密钥
def get_key(wxid: str, out_root: str) -> str:
    """读持久化密钥（DPAPI 加密，见 secure_store）；没有则现场提取（需微信已登录）"""
    from secure_store import load_key_file, save_key_file
    key_file = os.path.join(out_root, wxid, 'key.json')
    data = load_key_file(key_file)
    if data and data.get('key'):
        return data['key']
    from wxManager.decrypt import get_info_v4
    target = next((i for i in get_info_v4() if i.key), None)
    if target is None:
        raise RuntimeError('未提取到微信密钥（微信需已登录）')
    os.makedirs(os.path.dirname(key_file), exist_ok=True)
    save_key_file(key_file, target.key, target.wx_dir)
    return target.key


def wechat_running() -> bool:
    try:
        import psutil
        return any(p.name().lower().startswith('weixin') for p in psutil.process_iter(['name']))
    except Exception:
        return False


# ---------------------------------------------------------------- 页级解密
def _derive_keys(pkey_hex: str, salt: bytes):
    mac_salt = bytes(x ^ 0x3a for x in salt)
    passphrase = bytes.fromhex(pkey_hex)
    key = PBKDF2(passphrase, salt, dkLen=32, count=256000, hmac_hash_module=SHA512)
    mac_key = PBKDF2(key, mac_salt, dkLen=32, count=2, hmac_hash_module=SHA512)
    return key, mac_key


def decrypt_page(page: bytes, pgno: int, key, mac_key):
    """解密单个 4096 字节页（SQLCipher 4 默认参数）；HMAC 校验失败返回 None"""
    offset = SALT_SIZE if pgno == 1 else 0
    end = len(page)
    if end != PAGE_SIZE:
        return None
    mac = hmac.new(mac_key, page[offset:end - RESERVE + 16], SHA512)
    mac.update(struct.pack('<I', pgno))
    if mac.digest() != page[end - RESERVE + 16:end - RESERVE + 16 + 64]:
        return None
    iv = page[end - RESERVE:end - RESERVE + 16]
    cipher = AES.new(key, AES.MODE_CBC, iv)
    return cipher.decrypt(page[offset:end - RESERVE]) + page[end - RESERVE:end]


def decrypt_main_db(pkey_hex: str, src: str, dst: str) -> bool:
    """解密主库文件到 dst（页级 AES-CBC + HMAC 校验，与 decrypt_v4 等价）"""
    try:
        with open(src, 'rb') as f_in, open(dst, 'wb') as f_out:
            salt = f_in.read(SALT_SIZE)
            if not salt:
                return False
            key, mac_key = _derive_keys(pkey_hex, salt)
            f_out.write(b'SQLite format 3\x00')
            cur_page = 0
            while True:
                if cur_page == 0:
                    page = f_in.read(PAGE_SIZE - SALT_SIZE)
                    if not page:
                        break
                    page = salt + page
                else:
                    page = f_in.read(PAGE_SIZE)
                    if not page:
                        break
                if all(x == 0 for x in page):
                    break  # 尾部零页=库实际结束
                pt = decrypt_page(page, cur_page + 1, key, mac_key)
                if pt is None:
                    return False
                f_out.write(pt)
                cur_page += 1
        return True
    except OSError:
        return False


def merge_wal(pkey_hex: str, dec_db: str, enc_main: str, wal_path: str) -> int:
    """把加密 WAL 中最后一代已提交帧合并进解密库；返回成功合并的页数"""
    try:
        if not os.path.isfile(wal_path) or os.path.getsize(wal_path) <= 32:
            return 0
        with open(enc_main, 'rb') as f:
            salt = f.read(SALT_SIZE)
        if not salt:
            return 0
        key, mac_key = _derive_keys(pkey_hex, salt)

        frames = []
        with open(wal_path, 'rb') as f:
            wh = f.read(32)
            if len(wh) < 32:
                return 0
            magic, _fmt, psz, _ckpt, s1, s2, _c1, _c2 = struct.unpack('>8I', wh)
            if magic not in (0x377f0682, 0x377f0683) or psz != PAGE_SIZE:
                return 0
            last_commit_idx = -1
            while True:
                fh = f.read(24)
                if len(fh) < 24:
                    break
                pgno, commit, fs1, fs2, _fc1, _fc2 = struct.unpack('>6I', fh)
                page = f.read(psz)
                if len(page) < psz:
                    break
                if (fs1, fs2) != (s1, s2):
                    continue  # 上一代 wal 残留帧
                frames.append((pgno, commit, page))
                if commit:
                    last_commit_idx = len(frames) - 1
        if last_commit_idx < 0:
            return 0
        # 每页取已提交区间内最后一次出现的帧
        latest = {}
        for pgno, _commit, page in frames[:last_commit_idx + 1]:
            latest[pgno] = page
        if not latest:
            return 0

        merged = 0
        with open(dec_db, 'r+b') as f:
            f.seek(0, 2)
            file_pages = f.tell() // PAGE_SIZE
            for pgno, page in latest.items():
                pt = decrypt_page(page, pgno, key, mac_key)
                if pt is None:
                    continue  # 撕裂帧：保留主库版本
                if pgno == 1:
                    pt = b'SQLite format 3\x00' + pt[SALT_SIZE:]
                if pgno > file_pages:
                    f.seek(0, 2)
                    f.write(b'\x00' * ((pgno - 1 - file_pages) * PAGE_SIZE))
                    file_pages = pgno - 1
                f.seek((pgno - 1) * PAGE_SIZE)
                f.write(pt)
                merged += 1
        return merged
    except (OSError, struct.error):
        return 0


# ---------------------------------------------------------------- 变更检测
def _file_sig(path: str):
    try:
        st = os.stat(path)
        return [int(st.st_mtime), st.st_size]
    except OSError:
        return [0, 0]


def _db_sig(src_root: str, rel: str):
    """一个库的指纹 = 主库 (mtime,size) + wal (mtime,size)"""
    main = os.path.join(src_root, rel)
    wal = main + '-wal'
    return _file_sig(main) + _file_sig(wal)


def load_sync_state(out_root: str, wxid: str) -> dict:
    p = os.path.join(out_root, wxid, 'sync_state.json')
    if os.path.isfile(p):
        try:
            with open(p, encoding='utf-8') as f:
                return json.load(f).get('files', {})
        except Exception:
            return {}
    return {}


def save_sync_state(out_root: str, wxid: str, state: dict):
    """原子写（临时文件 + 替换），避免并发写坏状态文件"""
    p = os.path.join(out_root, wxid, 'sync_state.json')
    tmp = p + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'files': state, 'time': time.time()}, f, ensure_ascii=False)
    os.replace(tmp, p)


def find_changed(src_root: str, known: dict):
    """返回 [(相对路径, 主库是否存在wal)]：指纹与上次同步记录不一致的 .db"""
    changed = []
    for root, _dirs, files in os.walk(src_root):
        for fn in files:
            if not fn.endswith('.db'):
                continue
            src = os.path.join(root, fn)
            rel = os.path.relpath(src, src_root)
            sig = _db_sig(src_root, rel)
            if known.get(rel) != sig:
                changed.append(rel)
    return changed


def stable_copy(src: str, dst: str, tries: int = 3) -> bool:
    """复制微信正在写的文件：复制后源大小稳定才算成功"""
    for _ in range(tries):
        try:
            shutil.copyfile(src, dst)
            if os.path.getsize(src) == os.path.getsize(dst):
                return True
        except OSError:
            pass
        time.sleep(0.4)
    return False


# ---------------------------------------------------------------- 主入口
def has_changes(wx_dir: str, out_root: str, wxid: str) -> bool:
    """快速检查 live 库是否有更新（纯 stat，不解密不关连接）"""
    src_root = os.path.join(wx_dir, 'db_storage')
    if not os.path.isdir(src_root):
        return False
    return bool(find_changed(src_root, load_sync_state(out_root, wxid)))


def sync_databases(wx_dir: str, out_root: str, wxid: str, progress=None) -> dict:
    """
    增量同步 live db_storage -> 解密目录。
    返回 {'changed': [相对路径...], 'skipped': int, 'error': str|None}
    """
    src_root = os.path.join(wx_dir, 'db_storage')
    dst_root = os.path.join(out_root, wxid, 'db_storage')
    result = {'changed': [], 'skipped': 0, 'error': None}
    if not os.path.isdir(src_root):
        result['error'] = f'微信数据目录不存在: {src_root}'
        return result
    # 跨进程互斥：WebUI 自动/手动同步、channel 刷新可能并发，防止文件替换竞态
    try:
        with _file_lock(os.path.join(out_root, wxid, '.sync.lock')):
            _sync_under_lock(src_root, dst_root, out_root, wxid, result, progress)
    except _LockBusy:
        result['error'] = '另一个进程正在同步，本次跳过'
    return result


def _sync_under_lock(src_root: str, dst_root: str, out_root: str, wxid: str,
                     result: dict, progress):
    known = load_sync_state(out_root, wxid)
    todo = find_changed(src_root, known)
    if not todo:
        return
    try:
        key = get_key(wxid, out_root)
    except RuntimeError as e:
        result['error'] = str(e)
        return
    os.makedirs(dst_root, exist_ok=True)
    # 临时目录放在目标盘（os.replace 跨盘会 WinError 17）
    try:
        tmp = tempfile.mkdtemp(prefix='.memotrace_sync_', dir=dst_root)
    except OSError:
        tmp = tempfile.mkdtemp(prefix='memotrace_sync_')
    try:
        for i, rel in enumerate(todo):
            if progress:
                progress(i, len(todo), rel)
            src = os.path.join(src_root, rel)
            dst = os.path.join(dst_root, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            enc = os.path.join(tmp, f'w{i}.enc')
            wal = os.path.join(tmp, f'w{i}.wal')
            dec = os.path.join(tmp, f'w{i}.dec')
            if not stable_copy(src, enc):
                result['skipped'] += 1
                continue
            has_wal = os.path.isfile(src + '-wal') and stable_copy(src + '-wal', wal)
            if not decrypt_main_db(key, enc, dec):
                result['skipped'] += 1
                continue
            if has_wal:
                merge_wal(key, dec, enc, wal)
            try:
                conn = sqlite3.connect(dec)
                try:
                    ok = conn.execute('PRAGMA quick_check').fetchone()[0]
                finally:
                    conn.close()
                if ok != 'ok':
                    result['skipped'] += 1
                    continue
            except sqlite3.DatabaseError:
                result['skipped'] += 1
                continue
            # 原子替换：先清解密库旧 -wal/-shm（旧连接残留会污染新库）
            stale_blocked = False
            for suffix in ('-wal', '-shm'):
                p = dst + suffix
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except OSError:
                        stale_blocked = True
            if stale_blocked:
                result['skipped'] += 1
                continue
            try:
                os.replace(dec, dst)
            except OSError:
                shutil.move(dec, dst)  # 跨盘兜底（非原子，连接已关闭，安全）
            known[rel] = _db_sig(src_root, rel)
            result['changed'].append(rel)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if result['changed']:
        save_sync_state(out_root, wxid, known)
    return result
