#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""
@Time        : 2025/1/10 2:36
@Author      : SiYuan
@Email       : 863909694@qq.com
@File        : wxManager-wx_info_v4.py
@Description : 部分思路参考：https://github.com/0xlane/wechat-dump-rs
"""

import ctypes
import multiprocessing
import os.path

import hmac
import os
import re
import struct
import time
from ctypes import wintypes
from multiprocessing import freeze_support

import pymem
from Crypto.Protocol.KDF import PBKDF2
from Crypto.Hash import SHA512
import yara

from wxManager.decrypt.common import WeChatInfo
from wxManager.decrypt.common import get_version

# 定义必要的常量
PROCESS_ALL_ACCESS = 0x1F0FFF
PAGE_READWRITE = 0x04
MEM_COMMIT = 0x1000
MEM_PRIVATE = 0x20000

# Constants
IV_SIZE = 16
HMAC_SHA256_SIZE = 64
HMAC_SHA512_SIZE = 64
KEY_SIZE = 32
AES_BLOCK_SIZE = 16
ROUND_COUNT = 256000
PAGE_SIZE = 4096
SALT_SIZE = 16

finish_flag = False


# 定义 MEMORY_BASIC_INFORMATION 结构
class MEMORY_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", ctypes.c_ulong),
        ("RegionSize", ctypes.c_size_t),
        ("State", ctypes.c_ulong),
        ("Protect", ctypes.c_ulong),
        ("Type", ctypes.c_ulong),
    ]


# Windows API Constants
PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400

# Load Windows DLLs
kernel32 = ctypes.windll.kernel32


# 打开目标进程
def open_process(pid):
    return ctypes.windll.kernel32.OpenProcess(PROCESS_ALL_ACCESS, False, pid)


# 读取目标进程内存
def read_process_memory(process_handle, address, size):
    buffer = ctypes.create_string_buffer(size)
    bytes_read = ctypes.c_size_t(0)
    success = ctypes.windll.kernel32.ReadProcessMemory(
        process_handle,
        ctypes.c_void_p(address),
        buffer,
        size,
        ctypes.byref(bytes_read)
    )
    if not success:
        return None
    return buffer.raw


# 获取所有内存区域
def get_memory_regions(process_handle):
    regions = []
    mbi = MEMORY_BASIC_INFORMATION()
    address = 0
    while ctypes.windll.kernel32.VirtualQueryEx(
            process_handle,
            ctypes.c_void_p(address),
            ctypes.byref(mbi),
            ctypes.sizeof(mbi)
    ):
        if mbi.State == MEM_COMMIT and mbi.Type == MEM_PRIVATE:
            regions.append((mbi.BaseAddress, mbi.RegionSize))
        address += mbi.RegionSize
    return regions


rules_v4 = r'''
rule GetDataDir {
    strings:
        $a = /[a-zA-Z]:\\(.{1,100}?\\){0,1}?xwechat_files\\[0-9a-zA-Z_-]{6,24}?\\db_storage\\/
    condition:
        $a
}

rule GetPhoneNumberOffset {
    strings:
        $a = /[\x01-\x20]\x00{7}(\x0f|\x1f)\x00{7}[0-9]{11}\x00{5}\x0b\x00{7}\x0f\x00{7}/
    condition:
        $a
}
rule GetKeyAddrStub
{
    strings:
        $a = /.{6}\x00{2}\x00{8}\x20\x00{7}\x2f\x00{7}/
    condition:
        all of them
}
'''


# ==================== 微信 4.1 支持：静态 XOR key 提取 ====================
# 微信 4.1.x 起，数据库密钥（passphrase）在进程内存中以 XOR 混淆形式存放，
# 需要先从 Weixin.dll 的 .text 段静态提取 XOR key（mov rdx, imm64 ×4 + test rax,rax 特征），
# 内存候选值 XOR 还原后方可通过 HMAC 校验。
# 参考: https://github.com/LifeArchiveProject/WeChatDataAnalysis

# mov rdx, <8字节> ×4 + 中间若干指令 + test rax, rax
XOR_KEY_PATTERN = re.compile(
    b"^\x48\xBA(.{8})"
    b".{3,8}?"
    b"\x48\xBA(.{8})"
    b".{3,8}?"
    b"\x48\xBA(.{8})"
    b".{3,8}?"
    b"\x48\xBA(.{8})"
    b".{3,8}?"
    b"\x48\x85\xC0",
    re.DOTALL
)

_xor_keys_cache = {}  # {dll_path: (mtime, [xor_key_bytes, ...])}


def scan_dll_for_xor_keys(dll_path):
    """扫描 Weixin.dll 代码段，提取 32 字节 XOR key 列表"""
    try:
        import pefile
    except ImportError:
        print('[!] 微信4.1需要 pefile 库: pip install pefile')
        return []
    try:
        mtime = os.path.getmtime(dll_path)
        cached = _xor_keys_cache.get(dll_path)
        if cached and cached[0] == mtime:
            return cached[1]
    except OSError:
        pass
    try:
        pe = pefile.PE(dll_path, fast_load=True)
    except Exception as e:
        print(f'[!] 解析 Weixin.dll 失败: {e}')
        return []
    keys = []
    for section in pe.sections:
        if not (section.Characteristics & 0x20000000):  # 仅代码段
            continue
        sec_size = section.SizeOfRawData
        with open(dll_path, 'rb') as f:
            f.seek(section.PointerToRawData)
            data = f.read(sec_size)
        offset = 0
        while True:
            idx = data.find(b'\x48\xBA', offset)
            if idx == -1:
                break
            m = XOR_KEY_PATTERN.match(data[idx: idx + 85])
            if m:
                keys.append(m.group(1) + m.group(2) + m.group(3) + m.group(4))
                offset = idx + len(m.group(0))
            else:
                offset = idx + 1
    pe.close()
    # 去重，保持顺序
    seen = set()
    unique_keys = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            unique_keys.append(k)
    if dll_path:
        _xor_keys_cache[dll_path] = (mtime, unique_keys)
    return unique_keys


def get_weixin_dll_path(pid):
    """根据进程可执行文件路径定位 Weixin.dll（安装根目录/版本号子目录/当前目录）"""
    try:
        import psutil
        exe_path = psutil.Process(pid).exe()
    except Exception:
        return ''
    root = os.path.dirname(exe_path)
    try:
        version = get_version(pid)
    except Exception:
        version = ''
    candidates = []
    if version:
        candidates.append(os.path.join(root, version, 'Weixin.dll'))
    candidates.append(os.path.join(root, 'Weixin.dll'))
    for c in candidates:
        if os.path.isfile(c):
            return c
    # 兜底：扫子目录
    try:
        for name in os.listdir(root):
            p = os.path.join(root, name, 'Weixin.dll')
            if os.path.isfile(p):
                return p
    except OSError:
        pass
    return ''


def get_xor_keys_from_dll(pid):
    """提取当前微信版本对应的静态 XOR key 列表（4.1 及以后）"""
    dll_path = get_weixin_dll_path(pid)
    if not dll_path:
        print('[!] 未找到 Weixin.dll，跳过 XOR key 提取')
        return []
    print(f'[*] 从 {dll_path} 提取静态 XOR key (微信4.1适配)')
    keys = scan_dll_for_xor_keys(dll_path)
    print(f'[*] 提取到 {len(keys)} 枚 XOR key')
    return keys


def is_potential_key(key: bytes) -> bool:
    """熵过滤：排除非密码学随机的普通数据，加速校验"""
    if len(key) != KEY_SIZE:
        return False
    if len(set(key)) < 15:
        return False
    printable_count = sum(32 <= b <= 126 for b in key)
    if printable_count > 24:
        return False
    return True


def read_string(data: bytes, offset, size):
    try:
        return data[offset:offset + size].decode('utf-8')
    except:
        # print(data[offset:offset + size])
        # print(traceback.format_exc())
        return ''


def read_num(data: bytes, offset, size):
    # 构建格式字符串，根据 size 来选择相应的格式
    if size == 1:
        fmt = '<B'  # 1 字节，unsigned char
    elif size == 2:
        fmt = '<H'  # 2 字节，unsigned short
    elif size == 4:
        fmt = '<I'  # 4 字节，unsigned int
    elif size == 8:
        fmt = '<Q'  # 8 字节，unsigned long long
    else:
        raise ValueError("Unsupported size")

    # 使用 struct.unpack 从指定 offset 开始读取 size 字节的数据并转换为数字
    result = struct.unpack_from(fmt, data, offset)[0]  # 通过 unpack_from 来读取指定偏移的数据
    return result


def read_bytes(data: bytes, offset, size):
    return data[offset:offset + size]


# def read_bytes_from_pid(pid, offset, size):
#     with open(f'/proc/{pid}/mem', 'rb') as mem_file:
#         mem_file.seek(offset)
#         return mem_file.read(size)


# 导入 Windows API 函数
kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)

OpenProcess = kernel32.OpenProcess
OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
OpenProcess.restype = wintypes.HANDLE

ReadProcessMemory = kernel32.ReadProcessMemory
ReadProcessMemory.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.LPVOID, ctypes.c_size_t,
                              ctypes.POINTER(ctypes.c_size_t)]
ReadProcessMemory.restype = wintypes.BOOL

CloseHandle = kernel32.CloseHandle
CloseHandle.argtypes = [wintypes.HANDLE]
CloseHandle.restype = wintypes.BOOL


def read_bytes_from_pid(pid: int, addr: int, size: int):
    # 打开进程
    hprocess = OpenProcess(PROCESS_VM_READ | PROCESS_QUERY_INFORMATION, False, pid)
    if not hprocess:
        raise Exception(f"Failed to open process with PID {pid}")
    buffer = b''
    try:
        # 创建缓冲区
        buffer = ctypes.create_string_buffer(size)

        # 读取内存
        bytes_read = ctypes.c_size_t(0)
        success = ReadProcessMemory(hprocess, addr, buffer, size, ctypes.byref(bytes_read))
        if not success:
            CloseHandle(hprocess)
            return b''
            raise Exception(f"Failed to read memory at address {hex(addr)}")

        # 关闭句柄
        CloseHandle(hprocess)
    except:
        pass
    # 返回读取的字节数组
    return bytes(buffer)


def read_string_from_pid(pid: int, addr: int, size: int):
    bytes0 = read_bytes_from_pid(pid, addr, size)
    try:
        return bytes0.decode('utf-8')
    except:
        return ''


def is_ok(passphrase, buf):
    global finish_flag
    if finish_flag:
        return False
    # 获取文件开头的 salt
    salt = buf[:SALT_SIZE]
    # salt 异或 0x3a 得到 mac_salt，用于计算 HMAC
    mac_salt = bytes(x ^ 0x3a for x in salt)
    # 使用 PBKDF2 生成新的密钥
    new_key = PBKDF2(passphrase, salt, dkLen=KEY_SIZE, count=ROUND_COUNT, hmac_hash_module=SHA512)
    # 使用新的密钥和 mac_salt 计算 mac_key
    mac_key = PBKDF2(new_key, mac_salt, dkLen=KEY_SIZE, count=2, hmac_hash_module=SHA512)
    # 计算 hash 校验码的保留空间
    reserve = IV_SIZE + HMAC_SHA512_SIZE
    reserve = ((reserve + AES_BLOCK_SIZE - 1) // AES_BLOCK_SIZE) * AES_BLOCK_SIZE
    # 校验 HMAC
    start = SALT_SIZE
    end = PAGE_SIZE
    mac = hmac.new(mac_key, buf[start:end - reserve + IV_SIZE], SHA512)
    mac.update(struct.pack('<I', 1))  # page number as 1
    hash_mac = mac.digest()
    # 校验 HMAC 是否一致
    hash_mac_start_offset = end - reserve + IV_SIZE
    hash_mac_end_offset = hash_mac_start_offset + len(hash_mac)
    if hash_mac == buf[hash_mac_start_offset:hash_mac_end_offset]:
        print(f"[v] found key at 0x{start:x}")
        finish_flag = True
        return True
    return False


def check_chunk(chunk, buf):
    global finish_flag
    if finish_flag:
        return False
    if is_ok(chunk, buf):
        return chunk
    return False


def verify_key(key: bytes, buffer: bytes, flag, result):
    if len(key) != 32:
        return False
    if flag.value:  # 如果其他进程已找到结果，提前退出
        return False
    if is_ok(key, buffer):  # 替换为实际的目标检测条件
        print("Key found!", key)
        with flag.get_lock():  # 保证线程安全
            flag.value = True
            return key
    else:
        return False


def get_key_(keys, buf):
    pool = multiprocessing.Pool(processes=multiprocessing.cpu_count() // 2)
    results = pool.starmap(check_chunk, ((key, buf) for key in keys))
    pool.close()
    pool.join()

    for r in results:
        if r:
            print("Key found!", r)
            return bytes.hex(r)
    return None


# ==================== 微信 4.1 专用：XOR 还原 + 校验 ====================

def is_ok_with_xor(cand: bytes, xor_key_hex: str | None, buf: bytes) -> bool:
    """先按 XOR key 还原内存候选为 passphrase，再做 HMAC 校验；xor_key_hex 为 None 表示原样"""
    if len(cand) != KEY_SIZE:
        return False
    if finish_flag:
        return False
    if xor_key_hex:
        passphrase = bytes(a ^ b for a, b in zip(cand, bytes.fromhex(xor_key_hex)))
    else:
        passphrase = cand
    return is_ok(passphrase, buf)


def check_chunk_xor(args):
    """多进程 worker: (候选32字节, xor_key_hex或None, db首页) -> 命中返回 (cand, xk_hex)，未命中返回 (None, None)"""
    global finish_flag
    chunk, xor_key_hex, buf = args
    if finish_flag:
        return None, None
    if is_ok_with_xor(chunk, xor_key_hex, buf):
        return chunk, xor_key_hex
    return None, None


def get_key_v41_xor(keys, xor_keys, buf):
    """
    微信 4.1 路径：内存中的 32 字节候选是被 XOR 混淆的值，需分别用各 XOR key 还原后再校验，
    命中返回还原后的 passphrase(64位 hex)，未命中返回 None。
    """
    global finish_flag
    finish_flag = False  # 复位上一轮可能残留的命中标志
    combos = [(k, xk) for k in keys for xk in (xor_keys + [None])]
    pool = multiprocessing.Pool(processes=max(1, multiprocessing.cpu_count() // 2))
    try:
        for cand, xk in pool.imap_unordered(check_chunk_xor,
                                            ((c, (xk.hex() if isinstance(xk, (bytes, bytearray)) else xk), buf)
                                             for c, xk in combos),
                                            chunksize=8):
            if cand:
                pool.terminate()
                passphrase = bytes(a ^ b for a, b in zip(cand, bytes.fromhex(xk))) if xk else cand
                return passphrase.hex()
    finally:
        pool.close()
        pool.join()
    return None


# ==================== 微信 4.1：高熵块扫描（不依赖易变的指针特征） ====================
# 微信 4.1 起，数据库密钥（passphrase）在内存中以 XOR 混淆形式存放，且不同进程/版本下
# 其"指针特征"字节模式不稳定（实战中曾从 6301 个候选骤降到 100 且不含真值）。
# 因此不再依赖固定字节特征，改为：扫描全部私有内存，按 16 字节对齐提取 32 字节高熵块，
# 再用 XOR 还原 + HMAC 校验筛选。密钥本身是高熵数据，必被该扫描覆盖。

def _scan_region_entropy(args):
    """多进程 worker：扫描给定内存区域分块，返回高熵 32 字节候选（去重）"""
    pid, regions_chunk = args
    ph = open_process(pid)
    cands = set()
    for base, size in regions_chunk:
        mem = read_process_memory(ph, base, size)
        if not mem or len(mem) < KEY_SIZE:
            continue
        end = len(mem) - KEY_SIZE
        off = 0
        while off <= end:
            block = mem[off:off + KEY_SIZE]
            if is_potential_key(block):
                cands.add(block)
            off += 16
    CloseHandle(ph)
    return list(cands)


def get_v41_candidates(pid):
    """
    微信 4.1 路径：扫描进程全部私有内存，收集高熵 32 字节候选。多进程加速。
    返回候选列表（bytes）。
    """
    ph = open_process(pid)
    regions = get_memory_regions(ph)
    CloseHandle(ph)
    if not regions:
        return []
    n = min(len(regions), max(1, multiprocessing.cpu_count()))
    chunks = [regions[i::n] for i in range(n)]
    pool = multiprocessing.Pool(processes=n)
    try:
        results = pool.map(_scan_region_entropy, ((pid, c) for c in chunks))
    finally:
        pool.close()
        pool.join()
    cands = set()
    for r in results:
        cands.update(r)
    return list(cands)


# 微信 4.1 专用：密钥指针结构特征（已实机验证：6301 原始候选 -> 熵过滤 217 个 -> 20 秒内命中）
# 匹配的 32 字节结构为：[8字节密钥指针][10字节0][8字节0x20(=32,密钥长度)][8字节0x2f(类型标记)]
# 注意：必须遍历全部匹配实例（instances[0] 只会拿到每区域第一个，曾导致真密钥被漏掉）
RULE_V41_KEY = r'''
    rule GetKeyAddrStub
    {
        strings:
            $a = { ?? ?? ?? ?? ?? ?? 00 00 00 00 00 00 00 00 00 00 20 00 00 00 00 00 00 00 2f 00 00 00 00 00 00 00 }
        condition:
            all of them
    }
    '''


def get_v41_candidates_by_pointer(pid):
    """微信 4.1 主路径：按密钥指针结构特征扫描，解引用取 32 字节候选，熵过滤后返回"""
    process_handle = open_process(pid)
    if not process_handle:
        print(f"无法打开进程 {pid}")
        return []
    try:
        rules = yara.compile(source=RULE_V41_KEY)
    except Exception as e:
        print(f'[!] YARA 规则编译失败: {e}')
        return []
    candidates = set()
    for base_address, region_size in get_memory_regions(process_handle):
        memory = read_process_memory(process_handle, base_address, region_size)
        if not memory:
            continue
        try:
            matches = rules.match(data=memory)
        except Exception:
            continue
        if not matches:
            continue
        for match in matches:
            for s in match.strings:
                for instance in s.instances:
                    offset = instance.offset
                    if offset + 8 > len(memory):
                        continue
                    addr = struct.unpack_from('<Q', memory, offset)[0]
                    if addr == 0:
                        continue
                    key = read_process_memory(process_handle, addr, KEY_SIZE)
                    if key and len(key) == KEY_SIZE:
                        candidates.add(key)
    CloseHandle(process_handle)
    filtered = [k for k in candidates if is_potential_key(k)]
    print(f'[*] 4.1 指针特征扫描: 原始候选 {len(candidates)} 个, 熵过滤后 {len(filtered)} 个')
    return filtered


def get_key_inner(pid, process_infos, rule_source=None):
    """
    扫描可能为key的内存
    :param pid:
    :param process_infos:
    :param rule_source: 自定义 YARA 规则；None 时使用 4.0 默认正则
    :return:
    """
    process_handle = open_process(pid)
    if rule_source is None:
        rule_source = r'''
        rule GetKeyAddrStub
        {
            strings:
                $a = /.{6}\x00{2}\x00{8}\x20\x00{7}\x2f\x00{7}/
            condition:
                all of them
        }
        '''
    rules = yara.compile(source=rule_source)
    pre_addresses = []
    for base_address, region_size in process_infos:
        memory = read_process_memory(process_handle, base_address, region_size)
        # 定义目标数据（如内存或文件内容）
        target_data = memory  # 二进制数据
        if not memory:
            continue
        # 加上这些判断条件时灵时不灵
        # if b'-----BEGIN PUBLIC KEY-----' not in target_data or b'USER_KEYINFO' not in target_data:
        #     continue
        # if b'db_storage' not in memory:
        #     continue
        # with open(f'key-{base_address}.bin', 'wb') as f:
        #     f.write(target_data)
        matches = rules.match(data=target_data)
        if matches:
            for match in matches:
                rule_name = match.rule
                if rule_name == 'GetKeyAddrStub':
                    for string in match.strings:
                        instance = string.instances[0]
                        offset, content = instance.offset, instance.matched_data
                        addr = read_num(target_data, offset, 8)
                        pre_addresses.append(addr)
    keys = []
    key_set = set()
    for pre_address in pre_addresses:
        if True or any([base_address <= pre_address <= base_address + region_size - KEY_SIZE for base_address, region_size in
                process_infos]):
            key = read_bytes_from_pid(pid, pre_address, 32)
            if key not in key_set:
                keys.append(key)
                key_set.add(key)
    return keys


def get_key(pid, process_handle, buf, version=''):
    # 微信 4.1 起，内存中的数据库密钥是被 XOR 混淆的值，需要还原后才能通过 HMAC 校验。
    is_v41 = False
    try:
        vt = tuple(int(x) for x in version.split('.'))
        if len(vt) >= 2 and vt >= (4, 1):
            is_v41 = True
    except Exception:
        is_v41 = False

    if is_v41:
        # 4.1 路径：指针结构特征扫描（快，已实机验证） -> 高熵全内存扫描兜底（慢但覆盖面广）
        xor_keys = get_xor_keys_from_dll(pid)
        if not xor_keys:
            print('[!] 未从 Weixin.dll 提取到 XOR key，无法还原 4.1 密钥')
            return None
        keys = get_v41_candidates_by_pointer(pid)
        key = get_key_v41_xor(keys, xor_keys, buf[:PAGE_SIZE])
        if key:
            return key
        print('[*] 4.1 指针特征未命中，启用高熵全内存扫描兜底（较慢）...')
        keys = get_v41_candidates(pid)
        print(f'[*] 4.1 高熵扫描候选 {len(keys)} 个')
        return get_key_v41_xor(keys, xor_keys, buf[:PAGE_SIZE])

    # 微信 4.0 路径：指针特征扫描 + 直接 HMAC 校验
    xor_keys = []
    process_infos = get_memory_regions(process_handle)

    def split_list(lst, n):
        k, m = divmod(len(lst), n)
        return (lst[i * k + min(i, m):(i + 1) * k + min(i + 1, m)] for i in range(n))

    keys = []
    pool = multiprocessing.Pool(processes=multiprocessing.cpu_count() // 2)
    results = pool.starmap(get_key_inner, ((pid, process_info_, None) for process_info_ in
                                           split_list(process_infos, min(len(process_infos), 40))))
    pool.close()
    pool.join()
    for r in results:
        if r:
            keys += r
    return get_key_(keys, buf)


def get_wx_dir(process_handle):
    rules_v4_dir = r'''
    rule GetDataDir {
        strings:
            $a = /[a-zA-Z]:\\(.{1,100}?\\){0,1}?xwechat_files\\[0-9a-zA-Z_-]{6,24}?\\db_storage\\/
        condition:
            $a
    }
    '''
    rules = yara.compile(source=rules_v4_dir)
    process_infos = get_memory_regions(process_handle)
    wx_dir_cnt = {}
    for base_address, region_size in process_infos:
        memory = read_process_memory(process_handle, base_address, region_size)
        # 定义目标数据（如内存或文件内容）
        target_data = memory  # 二进制数据
        if not memory:
            continue
        if b'db_storage' not in memory:
            continue
        matches = rules.match(data=target_data)
        if matches:
            # 输出匹配结果
            for match in matches:
                rule_name = match.rule
                if rule_name == 'GetDataDir':
                    for string in match.strings:
                        content = string.instances[0].matched_data
                        wx_dir_cnt[content] = wx_dir_cnt.get(content, 0) + 1
    return max(wx_dir_cnt, key=wx_dir_cnt.get).decode('utf-8') if wx_dir_cnt else ''


def get_nickname(pid):
    process_handle = open_process(pid)
    if not process_handle:
        print(f"无法打开进程 {pid}")
        return {}
    process_infos = get_memory_regions(process_handle)
    # 加载规则
    r'''$a = /(.{16}[\x00-\x20]\x00{7}(\x0f|\x1f)\x00{7}){2}.{16}[\x01-\x20]\x00{7}(\x0f|\x1f)\x00{7}[0-9]{11}\x00{5}\x0b\x00{7}\x0f\x00{7}.{25}\x00{7}(\x3f|\x2f|\x1f|\x0f)\x00{7}/s'''
    rules_v4_phone = r'''
    rule GetPhoneNumberOffset {
        strings:
            $a = /[\x01-\x20]\x00{7}(\x0f|\x1f)\x00{7}[0-9]{11}\x00{5}\x0b\x00{7}\x0f\x00{7}/
        condition:
            $a
    }
    '''
    nick_name = ''
    phone = ''
    account_name = ''
    rules = yara.compile(source=rules_v4_phone)
    for base_address, region_size in process_infos:
        memory = read_process_memory(process_handle, base_address, region_size)
        # 定义目标数据（如内存或文件内容）
        target_data = memory  # 二进制数据
        if not memory:
            continue
        # if not (b'db_storage' in target_data or b'USER_KEYINFO' in target_data):
        #     continue
        # if not (b'-----BEGIN PUBLIC KEY-----' in target_data):
        #     continue
        matches = rules.match(data=target_data)
        if matches:
            # 输出匹配结果
            for match in matches:
                rule_name = match.rule
                if rule_name == 'GetPhoneNumberOffset':
                    for string in match.strings:
                        instance = string.instances[0]
                        offset, content = instance.offset, instance.matched_data
                        phone_addr = offset + 0x10
                        phone = read_string(target_data, phone_addr, 11)

                        # 提取前 8 个字节
                        data_slice = target_data[offset:offset + 8]
                        # 使用 struct.unpack() 将字节转换为 u64，'<Q' 表示小端字节序的 8 字节无符号整数
                        nick_name_length = struct.unpack('<Q', data_slice)[0]
                        # print('nick_name_length', nick_name_length)
                        nick_name = read_string(target_data, phone_addr - 0x20, nick_name_length)
                        a = target_data[phone_addr - 0x60:phone_addr + 0x50]
                        account_name_length = read_num(target_data, phone_addr - 0x30, 8)
                        # print('account_name_length', account_name_length)
                        account_name = read_string(target_data, phone_addr - 0x40, account_name_length)
                        # with open('a.bin', 'wb') as f:
                        #     f.write(target_data)
                        if not account_name:
                            addr = read_num(target_data, phone_addr - 0x40, 8)
                            # print(hex(addr))
                            account_name = read_string_from_pid(pid, addr, account_name_length)
    return {
        'nick_name': nick_name,
        'phone': phone,
        'account_name': account_name
    }


def worker(pid, queue):
    nickname_dic = get_nickname(pid)
    queue.put(nickname_dic)


def dump_wechat_info_v4(pid) -> WeChatInfo | None:
    wechat_info = WeChatInfo()
    wechat_info.pid = pid
    wechat_info.version = get_version(pid)
    process_handle = open_process(pid)
    if not process_handle:
        print(f"无法打开进程 {pid}")
        return wechat_info
    queue = multiprocessing.Queue()
    process = multiprocessing.Process(target=worker, args=(pid, queue))

    process.start()

    wechat_info.wx_dir = get_wx_dir(process_handle)
    # print(wx_dir_cnt)
    if not wechat_info.wx_dir:
        return wechat_info
    db_file_path = os.path.join(wechat_info.wx_dir, 'favorite', 'favorite_fts.db')
    if not os.path.exists(db_file_path):
        db_file_path = os.path.join(wechat_info.wx_dir, 'head_image', 'head_image.db')
    with open(db_file_path, 'rb') as f:
        buf = f.read()
    wechat_info.key = get_key(pid, process_handle, buf, wechat_info.version)
    ctypes.windll.kernel32.CloseHandle(process_handle)
    wechat_info.wxid = '_'.join(wechat_info.wx_dir.split('\\')[-3].split('_')[0:-1])
    wechat_info.wx_dir = '\\'.join(wechat_info.wx_dir.split('\\')[:-2])
    process.join()  # 等待子进程完成
    if not queue.empty():
        nickname_info = queue.get()
        wechat_info.nick_name = nickname_info.get('nick_name', '')
        wechat_info.phone = nickname_info.get('phone', '')
        wechat_info.account_name = nickname_info.get('account_name', '')
    if not wechat_info.key:
        wechat_info.errcode = 404
    else:
        wechat_info.errcode = 200
    return wechat_info


if __name__ == '__main__':
    freeze_support()
    st = time.time()
    pm = pymem.Pymem("Weixin.exe")
    pid = pm.process_id
    w = dump_wechat_info_v4(pid)
    print(w)
    et = time.time()
    print(et - st)
