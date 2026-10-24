#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
留痕 WebUI —— 仿微信界面的聊天记录可视化、同步与导出
  浏览：左侧会话列表 + 右侧聊天窗口，支持文本/图片/语音/视频/文件/表情/引用等
  同步：检测微信登录状态；手动/自动增量同步（解密+WAL合并+原子替换+连接热重载）
  日历：按天查看哪些日期有聊天记录，点击日期跳转
  导出：CSV / TXT / JSON / HTML(含媒体 zip)
数据来源：已解密数据库（数据根目录/<wxid>/db_storage，见 config.py）
"""
import csv
import io
import json
import os
import sys
import threading
import time
import zipfile
from datetime import date, datetime, timedelta

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from config import get_data_root

OUT_ROOT = get_data_root()

from flask import Flask, Response, abort, jsonify, render_template, request, send_file

from wxManager import Me, DatabaseConnection, MessageType
from wxManager.decrypt.decrypt_dat import decode_dat_v4
from exporter.exporter import decode_audio_to_mp3
from exporter.exporter_html import HtmlExporter
from exporter.config import FileType
from sync_engine import sync_databases, wechat_running, has_changes

app = Flask(__name__)
DB_LOCK = threading.RLock()  # 串行化数据库访问（sqlite 连接跨线程共享）
SYNC_LOCK = threading.Lock()  # 防止并发同步

database = None
CACHE_DIR = ''     # 媒体解密缓存根目录
EXPORT_DIR = ''    # 导出产物根目录
ME_WXID = ''
DB_DIR = ''        # 解密数据库目录（热重载用）

# 同步运行时状态（settings 持久化到磁盘）
SYNC = {
    'running': False,
    'auto': False,          # 自动同步开关
    'interval': 60,         # 自动同步间隔（秒）
    'last_sync': 0,         # 上次同步时间戳
    'last_result': None,    # {changed, skipped, sessions:[{name,range}], error}
}
SETTINGS_FILE = ''


# ---------------------------------------------------------------- 初始化
def ensure_decrypted():
    """没有解密数据时，现场提取密钥并解密（需微信已登录）"""
    from wxManager.decrypt import get_info_v4, decrypt_v4
    infos = get_info_v4()
    target = next((i for i in infos if i.key), None)
    if target is None:
        raise RuntimeError('未提取到微信密钥，请先登录微信后重试')
    dest = os.path.join(OUT_ROOT, target.wxid)
    db_dir = os.path.join(dest, 'db_storage')
    # 密钥持久化（同步功能复用，避免每次扫内存）
    try:
        with open(os.path.join(dest, 'key.json'), 'w', encoding='utf-8') as f:
            json.dump({'key': target.key, 'wx_dir': target.wx_dir, 'time': time.time()}, f)
    except OSError:
        pass
    if not os.path.isfile(os.path.join(db_dir, 'contact', 'contact.db')):
        print('    正在解密数据库（首次运行约十几秒）...')
        decrypt_v4.decrypt_db_files(target.key, src_dir=target.wx_dir, dest_dir=dest)
    me = Me()
    me.wxid, me.wx_dir = target.wxid, target.wx_dir
    me.name = target.nick_name or target.wxid
    from wxManager.decrypt.decrypt_dat import get_decode_code_v4
    try:
        me.xor_key = get_decode_code_v4(me.wx_dir)
    except Exception:
        me.xor_key = 0
    return db_dir


def _load_settings():
    global SETTINGS_FILE
    SETTINGS_FILE = os.path.join(OUT_ROOT, ME_WXID, 'webui_settings.json')
    # auto 开关不做持久化：程序启动默认手动同步，需要自动时在界面开启
    SYNC['auto'] = False
    if os.path.isfile(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, encoding='utf-8') as f:
                s = json.load(f)
            SYNC['interval'] = max(15, int(s.get('interval', 60)))
            SYNC['last_sync'] = float(s.get('last_sync', 0))
            SYNC['last_result'] = s.get('last_result')
        except Exception:
            pass


def _save_settings():
    try:
        with open(SETTINGS_FILE, 'w', encoding='utf-8') as f:
            json.dump({'interval': SYNC['interval'],
                       'last_sync': SYNC['last_sync'], 'last_result': SYNC['last_result'],
                       }, f, ensure_ascii=False)
    except OSError:
        pass


def init_database():
    global database, CACHE_DIR, EXPORT_DIR, ME_WXID, DB_DIR
    # 找已解密的账号
    candidates = []
    for wxid in os.listdir(OUT_ROOT):
        db_dir = os.path.join(OUT_ROOT, wxid, 'db_storage')
        if os.path.isfile(os.path.join(db_dir, 'contact', 'contact.db')):
            candidates.append((wxid, db_dir))
    if not candidates:
        db_dir = ensure_decrypted()
        wxid = Me().wxid
    else:
        wxid, db_dir = candidates[0]  # 单账号场景

    info_file = os.path.join(db_dir, 'info.json')
    me = Me()
    me.wxid = wxid
    ME_WXID = wxid
    DB_DIR = db_dir
    if os.path.isfile(info_file):
        with open(info_file, encoding='utf-8') as f:
            info = json.load(f)
        me.wx_dir = info.get('wx_dir', '')
        me.name = info.get('name', wxid)
        me.xor_key = info.get('xor_key', 0)
    if not getattr(me, 'wx_dir', ''):
        raise RuntimeError('info.json 缺少 wx_dir，请先运行 run_memotrace.py')

    database = DatabaseConnection(db_dir, 4).get_interface()
    CACHE_DIR = os.path.join(OUT_ROOT, wxid, 'media_cache')
    EXPORT_DIR = os.path.join(OUT_ROOT, wxid, '导出UI')
    os.makedirs(os.path.join(CACHE_DIR, 'image'), exist_ok=True)
    os.makedirs(os.path.join(CACHE_DIR, 'voice'), exist_ok=True)
    os.makedirs(EXPORT_DIR, exist_ok=True)
    _load_settings()
    # 预热 4.1 图片解密 key（MMKV 离线派生，写入磁盘缓存供多进程子进程使用）
    from wxManager.decrypt.decrypt_dat import get_v2_keys
    keys = get_v2_keys()
    if not keys:
        print('[!] 警告：未能从 MMKV 派生图片解密 key，图片可能无法显示')
    else:
        print(f'[i] 图片解密 key 已派生（{len(keys)} 个候选）')
    print(f'[i] 账号 {wxid} 数据库已连接')


# ---------------------------------------------------------------- 同步
def _close_all_connections():
    """关闭 manager 持有的全部 sqlite 连接（Windows 下替换文件前必须）"""
    from wxManager.model.db_model import DataBaseBase
    for v in vars(database).values():
        if isinstance(v, DataBaseBase):
            try:
                v.close()
            except Exception:
                pass


def _sessions_snapshot():
    """{username: last_timestamp}，用于对比同步前后哪些会话有更新"""
    try:
        return {s[0]: s[4] for s in database.get_session() if s[0]}
    except Exception:
        return {}


def _contact_name(username):
    try:
        c = database.get_contact_by_username(username)
        if c is not None:
            return c.remark or c.nickname or username
    except Exception:
        pass
    return username


def _fmt_range(ts1, ts2):
    f1, f2 = '%m-%d %H:%M', '%m-%d %H:%M'
    if ts1 and datetime.fromtimestamp(ts1).date() == datetime.fromtimestamp(ts2).date():
        f2 = '%H:%M'
    a = datetime.fromtimestamp(ts1).strftime(f1) if ts1 else '—'
    b = datetime.fromtimestamp(ts2).strftime(f2)
    return f'{a} ~ {b}'


def do_sync(manual=False):
    """增量同步：关闭连接 -> 解密变更库(含WAL) -> 原子替换 -> 重建连接 -> 汇报结果"""
    global database
    with SYNC_LOCK:
        if SYNC['running']:
            return {'running': True, 'last_result': SYNC['last_result']}
        SYNC['running'] = True
    result = {'error': None}
    try:
        if not wechat_running():
            result['error'] = '未检测到微信，请先在本机登录微信'
            SYNC['last_result'] = result
            return result
        wx_dir = Me().wx_dir
        if not has_changes(wx_dir, OUT_ROOT, ME_WXID):
            # 无变更：不关连接不重建，避免打断正在进行的请求
            result['changed'] = 0
            result['skipped'] = 0
            result['sessions'] = []
            result['has_update'] = False
            SYNC['last_sync'] = time.time()
            SYNC['last_result'] = result
            _save_settings()
            return result
        before = {}
        with DB_LOCK:
            # 全程持锁：关闭连接->替换文件->重建连接，期间其他请求排队等待而非报错
            before = _sessions_snapshot()
            _close_all_connections()
            try:
                r = sync_databases(wx_dir, OUT_ROOT, ME_WXID)
            except Exception as e:
                r = {'changed': [], 'skipped': 0, 'error': f'{e}'}
            database = DatabaseConnection(DB_DIR, 4).get_interface()
            after = _sessions_snapshot()

        result['changed'] = len(r.get('changed') or [])
        result['skipped'] = r.get('skipped', 0)
        result['error'] = r.get('error')
        # 会话级变化：哪些聊天更新了、覆盖的时间段
        sessions = []
        for username, new_ts in after.items():
            old_ts = before.get(username)
            if not new_ts or (old_ts and new_ts <= old_ts):
                continue
            sessions.append({
                'username': username,
                'name': _contact_name(username),
                'range': _fmt_range(old_ts or new_ts, new_ts),
                'ts': new_ts,
                'is_new': old_ts is None,
            })
        sessions.sort(key=lambda x: -x['ts'])
        result['sessions'] = sessions[:50]
        result['has_update'] = bool(sessions) or result['changed'] > 0
        SYNC['last_sync'] = time.time()
        SYNC['last_result'] = result
        _save_settings()
        return result
    finally:
        SYNC['running'] = False
        _save_settings()


def _auto_sync_loop():
    while True:
        try:
            if SYNC['auto'] and not SYNC['running']:
                do_sync()
        except Exception:
            pass
        time.sleep(3)


@app.route('/api/sync/status')
def api_sync_status():
    return jsonify({
        'wechat_running': wechat_running(),
        'running': SYNC['running'],
        'auto': SYNC['auto'],
        'interval': SYNC['interval'],
        'last_sync': SYNC['last_sync'],
        'last_result': SYNC['last_result'],
        'me': {'wxid': ME_WXID, 'name': getattr(Me(), 'name', '') or ME_WXID},
    })


@app.route('/api/sync', methods=['POST'])
def api_sync():
    r = do_sync(manual=True)
    return jsonify(r)


@app.route('/api/sync/settings', methods=['POST'])
def api_sync_settings():
    data = request.get_json(silent=True) or {}
    if 'auto' in data:
        SYNC['auto'] = bool(data['auto'])
    if 'interval' in data:
        try:
            SYNC['interval'] = max(15, int(data['interval']))
        except (TypeError, ValueError):
            pass
    _save_settings()
    return jsonify({'auto': SYNC['auto'], 'interval': SYNC['interval']})


@app.route('/api/calendar')
def api_calendar():
    u = request.args.get('u', '')
    with DB_LOCK:
        try:
            dates = database.get_messages_calendar(u) or []
        except Exception:
            dates = []
    return jsonify({'dates': dates})


# ---------------------------------------------------------------- 序列化
def serialize_message(msg, username):
    t = msg.type
    m = {
        'id': str(msg.server_id),
        'sort_seq': msg.sort_seq,
        'type_id': int(t),
        'type': MessageType.name(t),
        'is_sender': bool(msg.is_sender),
        'time': msg.str_time,
        'timestamp': msg.timestamp,
        'sender': msg.display_name or msg.sender_id,
        'sender_id': msg.sender_id,
        'avatar': f'/api/avatar/{msg.sender_id if msg.is_chatroom() else msg.talker_id}',
    }
    u = f'u={username}'

    if t == MessageType.Text:
        m['text'] = msg.content
    elif t == MessageType.Quote:
        m['text'] = msg.content
        try:
            q = msg.quote_message
            if q.type == MessageType.Quote:
                m['quote'] = {'sender': q.display_name, 'text': q.content}
            else:
                m['quote'] = {'sender': q.display_name, 'text': q.to_text()[:500]}
        except Exception:
            m['quote'] = None
    elif t == MessageType.Image:
        m['img'] = f'/api/media/image/{msg.server_id}?{u}&t=raw'
        m['thumb'] = f'/api/media/image/{msg.server_id}?{u}&t=thumb'
    elif t == MessageType.Video:
        m['video'] = f'/api/media/video/{msg.server_id}?{u}'
        m['thumb'] = f'/api/media/video/{msg.server_id}?{u}&t=thumb'
        m['duration'] = getattr(msg, 'duration', 0)
        m['video_ok'] = os.path.isfile(os.path.join(Me().wx_dir, getattr(msg, 'path', '') or 'x'))
    elif t == MessageType.Audio:
        m['voice'] = f'/api/media/voice/{msg.server_id}?{u}'
        m['duration'] = getattr(msg, 'duration', 0)
        m['voice_text'] = getattr(msg, 'audio_text', '') or ''
    elif t == MessageType.File:
        m['file_name'] = msg.file_name
        try:
            m['file_size'] = msg.get_file_size()
        except Exception:
            m['file_size'] = f'{msg.file_size} B'
        m['download'] = f'/api/media/file/{msg.server_id}?{u}'
    elif t == MessageType.Emoji:
        m['img'] = getattr(msg, 'url', '') or f'/api/media/image/{msg.server_id}?{u}&t=raw'
    elif t in (MessageType.LinkMessage, MessageType.LinkMessage2, MessageType.LinkMessage4,
               MessageType.LinkMessage5, MessageType.LinkMessage6):
        m['title'] = getattr(msg, 'title', '')
        m['href'] = getattr(msg, 'href', '')
        m['desc'] = getattr(msg, 'description', '')
    else:
        try:
            m['text'] = (msg.to_text() or '')[:600]
        except Exception:
            m['text'] = f'[{m["type"]}]'
    return m


def is_system_like(t):
    return t in (MessageType.System, MessageType.Pat, MessageType.Voip)


# ---------------------------------------------------------------- 页面与基础 API
@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/sessions')
def api_sessions():
    out = []
    with DB_LOCK:
        sessions = database.get_session()
        for s in sessions:
            username = s[0]
            if not username:
                continue
            last_timestamp = s[4]
            summary = s[5] or ''
            try:
                contact = database.get_contact_by_username(username)
            except Exception:
                contact = None
            if contact is not None:
                name = contact.remark or contact.nickname or username
            else:
                name = username
            out.append({
                'username': username,
                'name': name,
                'avatar': f'/api/avatar/{username}',
                'time': datetime.fromtimestamp(last_timestamp).strftime('%m/%d') if last_timestamp else '',
                'ts': last_timestamp,
                'summary': summary[:60],
                'is_chatroom': username.endswith('@chatroom'),
            })
    return jsonify(out)


@app.route('/api/avatar/<wxid>')
def api_avatar(wxid):
    buf = b''
    with DB_LOCK:
        try:
            buf = database.get_avatar_buffer(wxid) or b''
        except Exception:
            pass
    if buf:
        return Response(buf, mimetype='image/png')
    # 兜底：灰色占位头像（内嵌 SVG）
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80">'
           '<rect width="80" height="80" rx="8" fill="#c8c9cd"/>'
           '<circle cx="40" cy="31" r="15" fill="#fff"/>'
           '<path d="M14 70c3-16 14-24 26-24s23 8 26 24z" fill="#fff"/></svg>')
    return Response(svg, mimetype='image/svg+xml')


@app.route('/api/messages')
def api_messages():
    username = request.args.get('u', '')
    try:
        num = min(int(request.args.get('num', 50)), 200)
    except ValueError:
        num = 50
    before = request.args.get('before', '')
    day = request.args.get('date', '')
    if day:  # 跳转日期：取该日（次日元点）之前的最新消息
        try:
            d = datetime.strptime(day, '%Y-%m-%d')
            start = int((d + timedelta(days=1)).timestamp() * 1000)
        except ValueError:
            start = 2 ** 62
    elif before:
        start = int(before)
    else:
        start = 2 ** 62
    with DB_LOCK:
        msgs, last_seq = database.get_messages_by_num(username, start, num)
        data = [serialize_message(x, username) for x in reversed(msgs)]  # 正序返回
    return jsonify({
        'messages': data,
        'has_more': len(msgs) == num,
        'next_before': last_seq if len(msgs) == num else 0,
    })


# ---------------------------------------------------------------- 媒体
def _get_message(username, sid):
    msg = database.get_message_by_server_id(username, int(sid))
    if msg is None:
        abort(404, '消息不存在（可能未包含在该账号数据库中）')
    return msg


@app.route('/api/media/image/<sid>')
def media_image(sid):
    username = request.args.get('u', '')
    kind = request.args.get('t', 'raw')
    with DB_LOCK:
        msg = _get_message(username, sid)
    src_rel = getattr(msg, 'thumb_path', '') if kind == 'thumb' else getattr(msg, 'path', '')
    if not src_rel:
        abort(404, '图片文件缺失（可能未下载）')
    src = os.path.join(Me().wx_dir, src_rel)
    out_dir = os.path.join(CACHE_DIR, 'image')
    name = f'{kind}_{sid}'
    cached = [f for f in os.listdir(out_dir) if f.startswith(name + '.')]
    if cached:
        return send_file(os.path.join(out_dir, cached[0]), max_age=86400)
    with DB_LOCK:
        try:
            out = decode_dat_v4(Me().xor_key, src, out_dir, name)
        except Exception:
            out = ''
    if not out or not os.path.isfile(out):
        abort(404, '图片解密失败或文件不存在')
    return send_file(out, max_age=86400)


def _video_thumb_file(msg):
    """视频封面实际落盘名与 db 记录常不一致：依次尝试多个候选"""
    wx_dir = Me().wx_dir
    cands = []
    tp = getattr(msg, 'thumb_path', '') or ''
    p = getattr(msg, 'path', '') or ''
    if tp:
        cands.append(tp)
    if p:
        stem = os.path.splitext(p)[0]
        cands.append(stem + '_thumb.jpg')
        cands.append(stem + '.jpg')
    for c in cands:
        fp = os.path.join(wx_dir, c)
        if os.path.isfile(fp):
            return fp
    return ''


@app.route('/api/media/video/<sid>')
def media_video(sid):
    username = request.args.get('u', '')
    kind = request.args.get('t', 'raw')
    with DB_LOCK:
        msg = _get_message(username, sid)
    if kind == 'thumb':
        src = _video_thumb_file(msg)
        if not src:
            abort(404, '封面不存在')
        out_dir = os.path.join(CACHE_DIR, 'image')
        name = f'vthumb_{sid}'
        cached = [f for f in os.listdir(out_dir) if f.startswith(name + '.')]
        if cached:
            return send_file(os.path.join(out_dir, cached[0]), max_age=86400)
        try:
            out = decode_dat_v4(Me().xor_key, src, out_dir, name)
        except Exception:
            out = ''
        if out and os.path.isfile(out):
            return send_file(out, max_age=86400)
        abort(404)
    src = os.path.join(Me().wx_dir, getattr(msg, 'path', '') or '')
    if not os.path.isfile(src):
        abort(404, '视频文件缺失（可能未下载）')
    return send_file(src, mimetype='video/mp4', conditional=True)


@app.route('/api/media/voice/<sid>')
def media_voice(sid):
    username = request.args.get('u', '')
    out_dir = os.path.join(CACHE_DIR, 'voice')
    mp3 = os.path.join(out_dir, f'{sid}.mp3')
    if not os.path.isfile(mp3):
        with DB_LOCK:
            msg = _get_message(username, sid)
            buf = database.get_media_buffer(msg.server_id) or b''
        if not buf:
            abort(404, '语音数据缺失')
        out = decode_audio_to_mp3(buf, out_dir, str(sid))
        if not out or not os.path.isfile(out):
            abort(500, '语音转码失败')
    return send_file(mp3, mimetype='audio/mpeg', max_age=86400)


@app.route('/api/media/file/<sid>')
def media_file(sid):
    username = request.args.get('u', '')
    with DB_LOCK:
        msg = _get_message(username, sid)
    src = os.path.join(Me().wx_dir, getattr(msg, 'path', '') or '')
    if not os.path.isfile(src):
        abort(404, '文件缺失（可能未下载）')
    return send_file(src, as_attachment=True,
                     download_name=getattr(msg, 'file_name', None) or os.path.basename(src))


# ---------------------------------------------------------------- 导出
def _time_range(days):
    if not days or int(days) <= 0:
        return None
    end = date.today() + timedelta(days=1)
    return (end - timedelta(days=int(days)), end)


def _messages_rows(username, time_range):
    with DB_LOCK:
        messages = database.get_messages(username, time_range)
    rows = []
    for msg in messages:
        try:
            content = msg.to_text()
        except Exception:
            content = f'[{MessageType.name(msg.type)}]'
        rows.append({
            'time': msg.str_time,
            'sender': msg.display_name or msg.sender_id,
            'direction': '发送' if msg.is_sender else '接收',
            'type': MessageType.name(msg.type),
            'content': str(content).replace('\n', ' '),
        })
    return rows


@app.route('/api/export')
def api_export():
    username = request.args.get('u', '')
    fmt = (request.args.get('fmt') or 'csv').lower()
    time_range = _time_range(request.args.get('days', 30))

    with DB_LOCK:
        try:
            contact = database.get_contact_by_username(username)
        except Exception:
            contact = None
    name = (contact.remark or contact.nickname or username) if contact is not None else username
    safe_name = ''.join(c for c in name if c not in '\\/:*?"<>|') or username

    if fmt in ('csv', 'txt', 'json'):
        rows = _messages_rows(username, time_range)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        if fmt == 'csv':
            buf = io.StringIO()
            writer = csv.writer(buf)
            writer.writerow(['时间', '发送者', '方向', '类型', '内容'])
            writer.writerows((r['time'], r['sender'], r['direction'], r['type'], r['content'])
                             for r in rows)
            data = io.BytesIO(buf.getvalue().encode('utf-8-sig'))
            return send_file(data, as_attachment=True, download_name=f'{safe_name}_{stamp}.csv',
                             mimetype='text/csv')
        if fmt == 'txt':
            lines = [f"[{r['time']}] {r['sender']}({r['direction']}) [{r['type']}]: {r['content']}"
                     for r in rows]
            data = io.BytesIO('\n'.join(lines).encode('utf-8'))
            return send_file(data, as_attachment=True, download_name=f'{safe_name}_{stamp}.txt',
                             mimetype='text/plain')
        if fmt == 'json':
            data = io.BytesIO(json.dumps(rows, ensure_ascii=False, indent=1).encode('utf-8'))
            return send_file(data, as_attachment=True, download_name=f'{safe_name}_{stamp}.json',
                             mimetype='application/json')

    if fmt == 'html':
        # 完整 HTML（含图片/语音/视频媒体），打包 zip 下载
        days_tag = f'{request.args.get("days", 30)}天'
        work = os.path.join(EXPORT_DIR, f'{safe_name}_{days_tag}')  # 固定目录，覆盖复用
        exporter = HtmlExporter(database, contact, output_dir=work,
                                type_=FileType.HTML, time_range=time_range)
        exporter.start()
        html_root = os.path.join(work, '聊天记录')
        html_dirs = [os.path.join(html_root, d) for d in os.listdir(html_root)] \
            if os.path.isdir(html_root) else []
        if not html_dirs:
            abort(500, 'HTML 导出失败')
        src_dir = html_dirs[0]
        zpath = os.path.join(EXPORT_DIR, f'{safe_name}_{days_tag}.zip')
        if os.path.exists(zpath):
            os.remove(zpath)
        with zipfile.ZipFile(zpath, 'w', zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(src_dir):
                for fn in files:
                    fp = os.path.join(root, fn)
                    zf.write(fp, os.path.relpath(fp, src_dir))
        return send_file(zpath, as_attachment=True, download_name=os.path.basename(zpath),
                         mimetype='application/zip')

    abort(400, f'不支持的导出格式: {fmt}')


if __name__ == '__main__':
    from multiprocessing import freeze_support
    freeze_support()
    import webbrowser
    init_database()
    threading.Thread(target=_auto_sync_loop, daemon=True).start()
    print('[i] 留痕 WebUI 启动: http://127.0.0.1:5000')
    threading.Timer(1.5, webbrowser.open, args=['http://127.0.0.1:5000']).start()
    app.run(host='127.0.0.1', port=5000, threaded=True, use_reloader=False)
