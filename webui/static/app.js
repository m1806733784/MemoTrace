/* 留痕 WebUI 前端逻辑 */
const $ = (sel) => document.querySelector(sel);
const msgScroll = $('#msgScroll');
const msgList = $('#msgList');
const sessionList = $('#sessionList');

let currentSession = null;   // {username, name}
let nextBefore = 0;          // 分页游标
let loading = false;
let sessions = [];
let lastRenderedTs = 0;      // 时间分割线用

// ---------------- 工具 ----------------
function toast(text, ms = 2400) {
  let el = $('#toast');
  if (!el) {
    el = document.createElement('div');
    el.id = 'toast';
    document.body.appendChild(el);
  }
  el.textContent = text;
  el.style.display = 'block';
  clearTimeout(el._t);
  el._t = setTimeout(() => el.style.display = 'none', ms);
}

function fmtDivider(ts) {
  const d = new Date(ts * 1000);
  const now = new Date();
  const yd = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const one = (t) => `${String(t.getHours()).padStart(2, '0')}:${String(t.getMinutes()).padStart(2, '0')}`;
  if (d >= yd) return one(d);
  const yest = new Date(yd - 86400000);
  if (d >= yest) return '昨天 ' + one(d);
  const ymd = `${d.getFullYear()}/${d.getMonth() + 1}/${d.getDate()}`;
  return d >= new Date(yd - 6 * 86400000)
    ? `${['周日','周一','周二','周三','周四','周五','周六'][d.getDay()]} ${one(d)}`
    : ymd;
}

function esc(s) {
  const div = document.createElement('div');
  div.textContent = s == null ? '' : String(s);
  return div.innerHTML;
}

const DEFAULT_AVATAR = 'data:image/svg+xml;utf8,' + encodeURIComponent(
  '<svg xmlns="http://www.w3.org/2000/svg" width="80" height="80"><rect width="80" height="80" rx="8" fill="#c8c9cd"/><circle cx="40" cy="31" r="15" fill="#fff"/><path d="M14 70c3-16 14-24 26-24s23 8 26 24z" fill="#fff"/></svg>');

// ---------------- 会话列表 ----------------
async function loadSessions() {
  const res = await fetch('/api/sessions');
  sessions = await res.json();
  renderSessions(sessions);
  $('#meAvatar').textContent = (sessions.length ? '痕' : '痕');
}

function renderSessions(list) {
  sessionList.innerHTML = '';
  list.forEach(s => {
    const div = document.createElement('div');
    div.className = 'session-item' + (currentSession && s.username === currentSession.username ? ' active' : '');
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

// ---------------- 消息加载与渲染 ----------------
async function openSession(s) {
  currentSession = s;
  nextBefore = 0;
  lastRenderedTs = 0;
  msgList.innerHTML = '';
  $('#chatTitle').textContent = s.name;
  $('#chatSub').textContent = s.username;
  document.querySelectorAll('.session-item').forEach(el => el.classList.remove('active'));
  [...sessionList.children].forEach((el, i) => {
    if (sessions[i] && sessions[i].username === s.username) el.classList.add('active');
  });
  await loadMessages(false);
  msgScroll.scrollTop = msgScroll.scrollHeight;
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

$('#loadMore').onclick = () => loadMessages(true);
msgScroll.addEventListener('scroll', () => {
  if (msgScroll.scrollTop < 80) loadMessages(true);
});

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
    // 系统/拍一拍/通话 居中显示
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
    body = `<div class="bubble video-bubble">
      <video controls preload="metadata" src="${m.video}" ${m.thumb ? `poster="${m.thumb}"` : ''}></video>
    </div>`;
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
    // 链接
    body = `<div class="bubble"><div class="link-card" ${m.href ? `data-href="${esc(m.href)}"` : ''}>
      <div class="link-title">${esc(m.title || '[链接]')}</div>
      ${m.desc ? `<div class="link-desc">${esc(m.desc)}</div>` : ''}
      ${m.href ? `<div class="link-domain">${esc(new URL(m.href, 'https://x').hostname)}</div>` : ''}
    </div></div>`;
  } else {                                     // 其他类型
    body = `<div class="bubble" style="color:#8a8a8a">${esc(m.text || '[' + m.type + ']')}</div>`;
  }

  row.innerHTML = `${avatar}<div class="msg-body">${showName}${body}</div>`;
  frag.appendChild(row);
}

// 消息区事件委托：图片放大 / 语音播放 / 链接打开
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

// ---------------- 导出 ----------------
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
  btn.disabled = true; btn.textContent = '导出中，请稍候（大会话可能需要几分钟）...';
  const url = `/api/export?u=${encodeURIComponent(currentSession.username)}&fmt=${exportFmt}&days=${exportDays}`;
  try {
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

// ---------------- 启动 ----------------
loadSessions();
