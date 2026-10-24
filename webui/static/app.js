/* ═══════════════ 留痕 WebUI 前端逻辑 ═══════════════ */
const $ = (sel) => document.querySelector(sel);
const msgScroll = $('#msgScroll');
const msgList = $('#msgList');
const sessionList = $('#sessionList');

let currentSession = null;   // {username, name}
let nextBefore = 0;          // 分页游标（向更早翻页）
let atLatest = true;         // 当前是否在最新消息底部
let loading = false;
let sessions = [];
let lastRenderedTs = 0;
let unreadSet = new Set();   // 同步后有新消息的会话
let syncStatus = null;
let calendarDates = new Set(); // 有聊天记录的日期 'YYYY-MM-DD'
let calYear, calMonth;         // 日历当前显示的年月
let calSelected = '';

/* ═══════════ 工具 ═══════════ */
function toast(text, ms = 2600) {
  const el = $('#toast');
  el.textContent = text;
  el.style.display = 'block';
  requestAnimationFrame(() => el.classList.add('show'));
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.classList.remove('show'); setTimeout(() => el.style.display = 'none', 250); }, ms);
}

function esc(s) {
  const div = document.createElement('div');
  div.textContent = s == null ? '' : String(s);
  return div.innerHTML;
}

function fmtDivider(ts) {
  const d = new Date(ts * 1000);
  const now = new Date();
  const yd = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const hm = (t) => `${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}`;
  if (d >= yd) return hm(d);
  if (d >= new Date(yd - 86400000)) return '昨天 ' + hm(d);
  const ymd = `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()}`;
  return d >= new Date(yd - 6 * 86400000)
    ? `${['周日','周一','周二','周三','周四','周五','周六'][d.getDay()]} ${hm(d)}`
    : ymd;
}

function fmtClock(sec) {
  if (!sec) return '';
  const diff = Date.now() / 1000 - sec;
  if (diff < 60) return '刚刚';
  if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
  const d = new Date(sec * 1000);
  const now = new Date();
  if (d.toDateString() === now.toDateString()) return '今天 ' + d.toTimeString().slice(0, 5);
  return `${d.getMonth() + 1}/${d.getDate()} ${d.toTimeString().slice(0, 5)}`;
}

const DEFAULT_AVATAR = 'data:image/svg+xml;utf8,' + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><rect width="80" height="80" rx="12" fill="#dcdee2"/><circle cx="40" cy="32" r="15" fill="#fff"/><path d="M14 70c3-16 14-24 26-24s23 8 26 24z" fill="#fff"/></svg>');

const PLAY_SVG = '<svg viewBox="0 0 24 24" width="18" height="18" fill="#fff"><path d="M8 5.5v13l11-6.5z"/></svg>';

/* ═══════════ 同步 ═══════════ */
async function loadSyncStatus() {
  try {
    const res = await fetch('/api/sync/status');
    syncStatus = await res.json();
    renderSyncStatus();
  } catch { /* 服务暂不可达 */ }
}

function renderSyncStatus() {
  const s = syncStatus || {};
  const pill = $('#connPill');
  if (s.wechat_running) { pill.className = 'conn-pill ok'; $('#connText').textContent = '微信已登录'; }
  else { pill.className = 'conn-pill bad'; $('#connText').textContent = '微信未登录'; }

  $('#autoChk').checked = !!s.auto;
  $('#intervalSel').value = String(s.interval || 60);

  const btn = $('#btnSync');
  btn.disabled = !!s.running;
  btn.classList.toggle('busy', !!s.running);
  btn.querySelector('span').textContent = s.running ? '同步中…' : '立即同步';

  const meta = $('#syncMeta');
  if (s.running) {
    meta.textContent = '正在同步微信数据…';
    meta.classList.remove('clickable');
  } else if (s.last_sync) {
    const r = s.last_result || {};
    const n = (r.sessions || []).length;
    meta.innerHTML = n
      ? `<span class="new-tag">NEW</span>上次同步 ${fmtClock(s.last_sync)} · ${n} 个会话更新 <u>详情</u>`
      : `上次同步 ${fmtClock(s.last_sync)} · 已是最新`;
    meta.classList.toggle('clickable', !!n);
  }
}

async function manualSync() {
  const btn = $('#btnSync');
  btn.disabled = true; btn.classList.add('busy');
  btn.querySelector('span').textContent = '同步中…';
  $('#syncMeta').textContent = '正在同步微信数据…（首次约半分钟）';
  try {
    const res = await fetch('/api/sync', { method: 'POST' });
    const r = await res.json();
    if (r.running) { toast('同步正在进行中，请稍候'); return; }
    if (r.error) { toast('同步失败：' + r.error, 4000); return; }
    // 标记有新消息的会话
    (r.sessions || []).forEach(x => unreadSet.add(x.username));
    await loadSessions();
    await loadSyncStatus();
    const n = (r.sessions || []).length;
    if (n) {
      toast(`同步完成：${n} 个会话有新消息`);
      showSyncResult(r);
      // 当前打开的会话若有更新则刷新
      if (currentSession && unreadSet.has(currentSession.username)) openSession(currentSession, true);
      unreadSet.clear();
      const kw = $('#searchInput').value.trim().toLowerCase();
      renderSessions(!kw ? sessions : sessions.filter(
        s => s.name.toLowerCase().includes(kw) || s.username.toLowerCase().includes(kw)));
    } else {
      toast(r.changed ? '同步完成，暂无新消息' : '已是最新，无需同步');
    }
  } catch (e) {
    toast('同步失败：' + e.message, 4000);
  } finally {
    loadSyncStatus();
  }
}

function showSyncResult(r) {
  const body = $('#srBody');
  const ss = r.sessions || [];
  let html = `<div class="sr-stat">
      <span class="sr-chip">更新数据库 <b>${r.changed ?? 0}</b> 个</span>`;
  if (r.skipped) html += `<span class="sr-chip">跳过 <b>${r.skipped}</b> 个</span>`;
  html += `</div>`;
  if (r.error) html += `<div class="sr-error">${esc(r.error)}</div>`;
  if (!ss.length) {
    html += '<div class="sr-empty">本次同步没有新的聊天消息</div>';
  } else {
    html += ss.map(x => `
      <div class="sr-session">
        <span class="s-name">${esc(x.name)}${x.is_new ? ' <span style="color:#07c160;font-size:11px">新会话</span>' : ''}</span>
        <span class="s-range">${esc(x.range)}</span>
      </div>`).join('');
  }
  body.innerHTML = html;
  $('#syncResultOverlay').classList.remove('hidden');
}

async function saveSyncSettings() {
  const body = { auto: $('#autoChk').checked, interval: +$('#intervalSel').value };
  try {
    await fetch('/api/sync/settings', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (syncStatus) { syncStatus.auto = body.auto; syncStatus.interval = body.interval; }
    toast(body.auto ? `自动同步已开启（每 ${body.interval >= 60 ? body.interval / 60 + ' 分钟' : body.interval + ' 秒'}）` : '自动同步已关闭');
  } catch { toast('设置保存失败'); }
}

/* ═══════════ 会话列表 ═══════════ */
async function loadSessions(retry = 2) {
  try {
    const res = await fetch('/api/sessions');
    sessions = await res.json();
    if (!Array.isArray(sessions)) throw new Error('bad response');
    renderSessions(sessions);
  } catch (e) {
    if (retry > 0) { setTimeout(() => loadSessions(retry - 1), 2500); return; }
    sessionList.innerHTML = '<div class="empty-mini">会话加载失败，请刷新重试</div>';
  }
}

function renderSessions(list) {
  sessionList.innerHTML = '';
  if (!list.length) {
    sessionList.innerHTML = '<div class="empty-mini">没有匹配的会话</div>';
    return;
  }
  list.forEach(s => {
    const div = document.createElement('div');
    div.className = 'session-item'
      + (currentSession && s.username === currentSession.username ? ' active' : '')
      + (unreadSet.has(s.username) ? ' unread' : '');
    div.innerHTML = `
      <img class="avatar" src="${s.avatar}" onerror="this.src='${DEFAULT_AVATAR}'">
      <div class="s-info">
        <div class="s-top">
          <span class="s-name">${esc(s.name)}${s.is_chatroom ? ' <span style="color:#bbb;font-size:11px">[群]</span>' : ''}</span>
          <span class="s-time">${esc(s.time)}</span>
        </div>
        <div class="s-summary">${esc(s.summary)}</div>
      </div>`;
    div.onclick = () => openSession(s);
    sessionList.appendChild(div);
  });
}

$('#searchInput').addEventListener('input', (e) => {
  const kw = e.target.value.trim().toLowerCase();
  renderSessions(!kw ? sessions : sessions.filter(
    s => s.name.toLowerCase().includes(kw) || s.username.toLowerCase().includes(kw)));
});

/* ═══════════ 消息加载与渲染 ═══════════ */
async function openSession(s, keepScroll) {
  currentSession = s;
  nextBefore = 0;
  atLatest = true;
  lastRenderedTs = 0;
  msgList.innerHTML = '';
  unreadSet.delete(s.username);
  $('#chatTitle').textContent = s.name;
  $('#chatSub').textContent = s.username;
  $('#chatEmpty').classList.add('hidden');
  $('#btnCalendar').disabled = false;
  $('#btnExport').disabled = false;
  $('#btnLatest').classList.add('hidden');
  calSelected = '';
  document.querySelectorAll('.session-item').forEach(el => el.classList.remove('active'));
  [...sessionList.children].forEach((el, i) => {
    if (sessions[i] && sessions[i].username === s.username) el.classList.add('active');
  });
  loadCalendar();
  await loadMessages(false);
  if (!keepScroll) msgScroll.scrollTop = msgScroll.scrollHeight;
}

async function loadMessages(older) {
  if (!currentSession || loading) return;
  loading = true;
  const cursor = nextBefore;
  try {
    const url = `/api/messages?u=${encodeURIComponent(currentSession.username)}&num=50` +
      (older && cursor ? `&before=${cursor}` : '');
    const res = await fetch(url);
    const data = await res.json();
    nextBefore = data.next_before;
    $('#loadMore').classList.toggle('hidden', !data.has_more);
    if (!data.has_more) $('#loadMore').classList.add('hidden');

    const anchorHeight = msgScroll.scrollHeight;
    const frag = document.createDocumentFragment();
    data.messages.forEach(m => appendMessage(frag, m));
    if (older) {
      msgList.insertBefore(frag, msgList.firstChild);
      msgScroll.scrollTop = msgScroll.scrollHeight - anchorHeight;
    } else {
      msgList.appendChild(frag);
    }
  } finally {
    loading = false;
  }
}

async function jumpToDate(day) {  // day: 'YYYY-MM-DD'
  if (!currentSession) return;
  nextBefore = 0;
  atLatest = false;
  lastRenderedTs = 0;
  msgList.innerHTML = '';
  $('#loadMore').classList.add('hidden');
  const res = await fetch(`/api/messages?u=${encodeURIComponent(currentSession.username)}&date=${day}&num=50`);
  const data = await res.json();
  nextBefore = data.next_before;
  $('#loadMore').classList.toggle('hidden', !data.has_more);
  const frag = document.createDocumentFragment();
  data.messages.forEach(m => appendMessage(frag, m));
  msgList.appendChild(frag);
  msgScroll.scrollTop = msgScroll.scrollHeight;
  $('#btnLatest').classList.remove('hidden');
  $('#calOverlay').classList.add('hidden');
}

$('#loadMore').onclick = () => loadMessages(true);
msgScroll.addEventListener('scroll', () => {
  if (msgScroll.scrollTop < 80) loadMessages(true);
});
$('#btnLatest').onclick = () => currentSession && openSession(currentSession);

function timeDividerIfNeeded(frag, ts) {
  if (!lastRenderedTs || ts - lastRenderedTs > 300) {
    const div = document.createElement('div');
    div.className = 'time-divider';
    div.innerHTML = `<span>${fmtDivider(ts)}</span>`;
    frag.appendChild(div);
  }
  lastRenderedTs = ts;
}

function appendMessage(frag, m) {
  if (m.type_id === 10000 || m.type_id === 266287972401 || m.type_id === 50) {
    const row = document.createElement('div');
    row.className = 'msg-row system';
    row.innerHTML = `<div class="bubble">${esc(m.text || m.type)}</div>`;
    frag.appendChild(row);
    return;
  }
  timeDividerIfNeeded(frag, m.timestamp);

  const row = document.createElement('div');
  row.className = 'msg-row' + (m.is_sender ? ' self' : '');
  const avatar = `<img class="avatar" src="${m.avatar}" onerror="this.src='${DEFAULT_AVATAR}'">`;

  let body = '';
  const showName = (!m.is_sender && currentSession && currentSession.username.endsWith('@chatroom'))
    ? `<div class="sender-name">${esc(m.sender)}</div>` : '';

  if (m.type_id === 1) {                       // 文本
    body = `<div class="bubble">${esc(m.text)}</div>`;
  } else if (m.type_id === 244813135921) {     // 引用
    body = `<div class="bubble">${esc(m.text)}
      ${m.quote ? `<div class="quote-block"><div class="quote-sender">${esc(m.quote.sender)}</div>${esc(m.quote.text)}</div>` : ''}
    </div>`;
  } else if (m.type_id === 3) {                // 图片
    body = `<div class="bubble img-bubble">
      <img loading="lazy" src="${m.thumb || m.img}" data-full="${m.img}"
        onerror="this.onerror=null;this.replaceWith(Object.assign(document.createElement('div'),{textContent:'[图片未下载]',style:'color:#999;font-size:13px;padding:20px'}))">
    </div>`;
  } else if (m.type_id === 47) {               // 表情
    body = `<div class="bubble emoji-bubble"><img loading="lazy" src="${m.img}" onerror="this.parentElement.innerHTML='<span style=\\'color:#999\\'>[表情]</span>'"></div>`;
  } else if (m.type_id === 43) {               // 视频
    if (m.video_ok) {
      body = `<div class="bubble video-bubble">
        <video controls preload="metadata" src="${m.video}" ${m.thumb ? `poster="${m.thumb}"` : ''}></video>
      </div>`;
    } else {
      body = `<div class="bubble video-bubble">
        <div class="video-missing">
          <img loading="lazy" src="${m.thumb || ''}" onerror="this.style.minHeight='120px'">
          <div class="vm-mask">
            <div class="vm-play">${PLAY_SVG}</div>
            <div class="vm-tip">视频未在微信中下载</div>
            <div class="vm-tip" style="opacity:.75;font-size:11px">在微信聊天中点开该视频后，再同步即可播放</div>
          </div>
        </div>
      </div>`;
    }
  } else if (m.type_id === 34) {               // 语音
    const dur = m.duration || 1;
    body = `<div class="bubble voice-bubble" data-src="${m.voice}">
      <span class="voice-wave"><i></i><i></i><i></i><i></i></span>
      <span class="voice-dur">${dur}″</span>
    </div>
    ${m.voice_text ? `<div class="voice-text">转文字: ${esc(m.voice_text)}</div>` : ''}`;
  } else if (m.type_id === 25769803825) {      // 文件
    const ext = (m.file_name || '').split('.').pop().toUpperCase().slice(0, 4);
    body = `<div class="bubble"><div class="file-card">
      <div class="file-icon">${esc(ext || 'FILE')}</div>
      <div class="file-meta">
        <div class="file-name">${esc(m.file_name)}</div>
        <div class="file-size">${esc(m.file_size)}</div>
      </div>
      <a class="file-dl" href="${m.download}" download>下载</a>
    </div></div>`;
  } else if ([21474836529, 292057776177, 4294967345, 326417514545, 17179869233].includes(m.type_id)) {
    body = `<div class="bubble"><div class="link-card" ${m.href ? `data-href="${esc(m.href)}"` : ''}>
      <div class="link-title">${esc(m.title || '[链接]')}</div>
      ${m.desc ? `<div class="link-desc">${esc(m.desc)}</div>` : ''}
      ${m.href ? `<div class="link-domain">${esc(new URL(m.href, 'https://x').hostname)}</div>` : ''}
    </div></div>`;
  } else {
    body = `<div class="bubble" style="color:#8a8a8a">${esc(m.text || '[' + m.type + ']')}</div>`;
  }

  row.innerHTML = `${avatar}<div class="msg-body">${showName}${body}</div>`;
  frag.appendChild(row);
}

/* 事件委托：图片放大 / 语音 / 链接 */
msgList.addEventListener('click', (e) => {
  const img = e.target.closest('.img-bubble img');
  if (img && img.dataset.full) {
    $('#lightboxImg').src = img.dataset.full;
    $('#lightbox').classList.remove('hidden');
    return;
  }
  const voice = e.target.closest('.voice-bubble');
  if (voice) { playVoice(voice); return; }
  const link = e.target.closest('.link-card');
  if (link && link.dataset.href) window.open(link.dataset.href, '_blank');
});
$('#lightbox').onclick = () => $('#lightbox').classList.add('hidden');

let currentAudio = null;
function playVoice(el) {
  if (currentAudio) { currentAudio.pause(); currentAudio.el.classList.remove('playing'); }
  const src = el.dataset.src;
  if (currentAudio && currentAudio.src.endsWith(src)) { currentAudio = null; return; }
  const audio = new Audio(src);
  audio.el = el;
  el.classList.add('playing');
  audio.onended = () => { el.classList.remove('playing'); currentAudio = null; };
  audio.play().catch(() => toast('语音播放失败'));
  currentAudio = audio;
}

/* ═══════════ 日历 ═══════════ */
async function loadCalendar() {
  if (!currentSession) return;
  try {
    const res = await fetch(`/api/calendar?u=${encodeURIComponent(currentSession.username)}`);
    const data = await res.json();
    calendarDates = new Set(data.dates || []);
    if (!calYear) {
      const last = [...calendarDates].pop();
      const d = last ? new Date(last + 'T00:00:00') : new Date();
      calYear = d.getFullYear(); calMonth = d.getMonth();
    }
    renderCalendar();
  } catch { /* 忽略 */ }
}

function renderCalendar() {
  $('#calTitle').textContent = `${calYear}年${calMonth + 1}月`;
  const grid = $('#calGrid');
  grid.innerHTML = '';
  const first = new Date(calYear, calMonth, 1);
  const daysInMonth = new Date(calYear, calMonth + 1, 0).getDate();
  const startWeek = first.getDay();
  const today = new Date();
  const todayStr = `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, '0')}-${String(today.getDate()).padStart(2, '0')}`;
  for (let i = 0; i < startWeek; i++) grid.appendChild(Object.assign(document.createElement('span'), { className: 'cal-day dim' }));
  for (let d = 1; d <= daysInMonth; d++) {
    const mm = String(calMonth + 1).padStart(2, '0'), dd = String(d).padStart(2, '0');
    const ds = `${calYear}-${mm}-${dd}`;
    const cell = document.createElement('div');
    cell.className = 'cal-day'
      + (calendarDates.has(ds) ? ' has' : '')
      + (ds === todayStr ? ' today' : '')
      + (ds === calSelected ? ' selected' : '');
    cell.textContent = d;
    cell.onclick = () => {
      calSelected = ds;
      renderCalendar();
      if (calendarDates.has(ds)) jumpToDate(ds);
      else toast('该日期没有聊天记录');
    };
    grid.appendChild(cell);
  }
}

$('#btnCalendar').onclick = () => {
  if (!currentSession) return;
  $('#calOverlay').classList.remove('hidden');
  if (!calendarDates.size) loadCalendar(); else renderCalendar();
};
$('#calOverlay').onclick = (e) => { if (e.target === e.currentTarget) e.currentTarget.classList.add('hidden'); };
$('#calPrev').onclick = () => { calMonth--; if (calMonth < 0) { calMonth = 11; calYear--; } renderCalendar(); };
$('#calNext').onclick = () => { calMonth++; if (calMonth > 11) { calMonth = 0; calYear++; } renderCalendar(); };

/* ═══════════ 导出 ═══════════ */
let exportFmt = 'csv', exportDays = 30;
$('#btnExport').onclick = (e) => { e.stopPropagation(); $('#exportPanel').classList.toggle('hidden'); };
document.addEventListener('click', (e) => {
  if (!e.target.closest('.export-wrap')) $('#exportPanel').classList.add('hidden');
});
$('#fmtGroup').onclick = (e) => {
  if (e.target.dataset.fmt) {
    exportFmt = e.target.dataset.fmt;
    [...$('#fmtGroup').children].forEach(b => b.classList.toggle('active', b === e.target));
    $('#expTip').textContent = exportFmt === 'html'
      ? 'HTML 含图片/语音/视频，打包 zip 下载' : `导出为 ${exportFmt.toUpperCase()} 纯文本格式`;
  }
};
$('#daysGroup').onclick = (e) => {
  if (e.target.dataset.days != null) {
    exportDays = +e.target.dataset.days;
    [...$('#daysGroup').children].forEach(b => b.classList.toggle('active', b === e.target));
  }
};
$('#btnDoExport').onclick = async () => {
  if (!currentSession) { toast('请先选择一个会话'); return; }
  const btn = $('#btnDoExport');
  const u = encodeURIComponent(currentSession.username);
  try {
    if (exportFmt === 'html') { await exportHtml(btn, u); return; }
    btn.disabled = true; btn.textContent = '导出中，请稍候...';
    const url = `/api/export?u=${u}&fmt=${exportFmt}&days=${exportDays}`;
    const res = await fetch(url);
    if (!res.ok) {
      const msg = await res.text();
      toast('导出失败: ' + msg.slice(0, 120), 5000);
      return;
    }
    const blob = await res.blob();
    const cd = res.headers.get('Content-Disposition') || '';
    const m = cd.match(/filename\*?=(?:UTF-8''|")?([^";]+)/);
    let filename = m ? decodeURIComponent(m[1]) : `导出_${Date.now()}.${exportFmt}`;
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = filename;
    a.click();
    URL.revokeObjectURL(a.href);
    toast('导出完成: ' + filename);
    $('#exportPanel').classList.add('hidden');
  } catch (err) {
    toast('导出失败: ' + err.message, 5000);
  } finally {
    btn.disabled = false; btn.textContent = '开始导出';
  }
};

/* HTML 导出：后台任务 + 轮询进度（大会话耗时长，不能占用请求线程） */
async function exportHtml(btn, u) {
  btn.disabled = true;
  let dot = 0;
  const timer = setInterval(() => {
    btn.textContent = '导出中' + '.'.repeat(++dot % 4);
  }, 600);
  try {
    const r = await fetch(`/api/export/start?u=${u}&days=${exportDays}`);
    if (!r.ok) { toast('导出失败: ' + (await r.text()).slice(0, 120), 5000); return; }
    const { id } = await r.json();
    while (true) {
      await new Promise(res => setTimeout(res, 1500));
      const p = await (await fetch(`/api/export/progress?id=${id}`)).json();
      if (p.status === 'done') {
        window.location = `/api/export/download?id=${id}`;
        toast('导出完成: ' + (p.filename || 'HTML zip'));
        $('#exportPanel').classList.add('hidden');
        return;
      }
      if (p.status === 'error') {
        toast('导出失败: ' + (p.error || '').slice(0, 120), 5000);
        return;
      }
    }
  } catch (err) {
    toast('导出失败: ' + err.message, 5000);
  } finally {
    clearInterval(timer);
    btn.disabled = false; btn.textContent = '开始导出';
  }
}

/* ═══════════ 同步控件绑定 ═══════════ */
$('#btnSync').onclick = manualSync;
$('#autoChk').onchange = saveSyncSettings;
$('#intervalSel').onchange = saveSyncSettings;
$('#syncMeta').onclick = () => {
  const r = syncStatus && syncStatus.last_result;
  if (r && (r.sessions || []).length) showSyncResult(r);
};
$('#srClose').onclick = () => $('#syncResultOverlay').classList.add('hidden');
$('#syncResultOverlay').onclick = (e) => {
  if (e.target === e.currentTarget) e.currentTarget.classList.add('hidden');
};

/* ═══════════ 启动 ═══════════ */
(async function boot() {
  await Promise.all([loadSessions(), loadSyncStatus()]);
  setInterval(loadSyncStatus, 8000);  // 轮询同步状态（自动同步由服务端线程执行）
})();
