'use strict';

(() => {

const { LANG, T, applyI18n, toggleLang } = window.HaoI18n;

const MUTATION_HEADERS = { 'X-Requested-With': 'haodrive' };
// The app is mounted under a random secret path; derive it from the URL.
const BASE = location.pathname.replace(/\/+$/, '');
const API_TIMEOUT = 30000;
const SMALL_DIRECT_DOWNLOAD = 32 * 1024 * 1024;

const state = {
  folders: [],
  folderId: null,
  files: [],
  nextOffset: 0,
  query: '',
  loading: false,
  reqSeq: 0,
  selecting: false,
  selection: new Set(),
  uploads: new Map(),
  page: 'files',
  statsDays: 30,
  statsTimer: null,
  statsBusy: false,
  statsSeeded: false,
  demo: false,
};

const $ = (id) => document.getElementById(id);

const ICON_NAMES = { image: 'i-image', video: 'i-video', audio: 'i-audio', pdf: 'i-pdf', text: 'i-text' };
function icon(name, cls = 'ic') {
  return `<svg class="${cls}"><use href="#${name}"/></svg>`;
}

const THEME_KEY = 'haodrive_theme';
const THEMES = ['system', 'light', 'dark'];
function currentTheme() {
  try {
    const saved = localStorage.getItem(THEME_KEY);
    if (THEMES.includes(saved)) return saved;
  } catch (_) { /* private mode */ }
  return 'system';
}
function applyTheme(mode) {
  const root = document.documentElement;
  if (mode === 'system') root.removeAttribute('data-theme');
  else root.dataset.theme = mode;
  try { localStorage.setItem(THEME_KEY, mode); } catch (_) { /* ignore */ }
  const dark = mode === 'dark'
    || (mode === 'system' && matchMedia('(prefers-color-scheme: dark)').matches);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = dark ? '#0b0d12' : '#f5f6fa';
  for (const button of document.querySelectorAll('.theme-toggle')) {
    button.innerHTML = icon(dark ? 'i-sun' : 'i-moon');
    button.title = T(mode === 'system' ? '跟随系统' : (mode === 'light' ? '浅色' : '深色'));
  }
}

/* ---------- helpers ---------- */

function escapeHtml(value) {
  return String(value ?? '')
    .replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;').replaceAll("'", '&#39;');
}

function bytes(size) {
  if (!size) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let index = 0;
  let value = size;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
  return `${value >= 10 || index === 0 ? Math.round(value) : value.toFixed(1)} ${units[index]}`;
}

function when(ts) {
  if (!ts) return '';
  return new Date(ts * 1000).toLocaleString(LANG === 'zh' ? 'zh-CN' : 'en-US', {
    year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit',
  });
}

function countFiles(n) {
  return LANG === 'en' ? `${n} file${n === 1 ? '' : 's'}` : `${n} 个文件`;
}

function countTransfers(n) {
  return LANG === 'en' ? `${n} transfer${n === 1 ? '' : 's'}` : `${n} 次`;
}

function dateLabel(day) {
  const parts = String(day).split('-');
  return `${parts[1]}-${parts[2]}`;
}

function toast(message, bad = false) {
  const node = document.createElement('div');
  node.className = 'toast' + (bad ? ' bad' : '');
  node.textContent = message;
  $('toasts').appendChild(node);
  setTimeout(() => node.remove(), 4200);
}

function errorText(payload, status) {
  const detail = payload && typeof payload.detail === 'string' ? payload.detail : '';
  let match;
  if ((match = detail.match(/^文件超过 (\d+)GB 上限$/))) return T('文件超过 {n}GB 上限', { n: match[1] });
  if ((match = detail.match(/^还差 (\d+) 字节$/))) return T('还差 {n} 字节', { n: match[1] });
  if ((match = detail.match(/^Telegram 限流，等待 (\d+) 秒$/))) return T('Telegram 限流，等待 {n} 秒', { n: match[1] });
  if (detail) return T(detail);
  return T('请求失败 ({status})', { status });
}

async function api(path, { method = 'GET', body, timeout = API_TIMEOUT } = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  const options = {
    method,
    headers: { ...MUTATION_HEADERS },
    credentials: 'same-origin',
    signal: controller.signal,
  };
  if (body !== undefined) {
    options.headers['Content-Type'] = 'application/json';
    options.body = JSON.stringify(body);
  }
  try {
    const response = await fetch(BASE + path, options);
    let payload = null;
    try { payload = await response.json(); } catch (_) { /* empty body */ }
    if (!response.ok) {
      if (response.status === 401 && path !== '/api/login') {
        showLogin();
        throw new Error(T('登录已失效'));
      }
      const error = new Error(errorText(payload, response.status));
      error.status = response.status;
      error.payload = payload;
      throw error;
    }
    return payload;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error(T('请求超时，检查网络'));
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function show(id) {
  for (const view of ['view-login', 'view-setup', 'view-main']) {
    $(view).classList.toggle('hidden', view !== id);
  }
}

function showLogin() { show('view-login'); }

/* ---------- boot ---------- */

function initChrome() {
  applyI18n();
  for (const button of document.querySelectorAll('.lang-toggle')) {
    button.textContent = LANG === 'zh' ? 'EN' : '中文';
    button.addEventListener('click', toggleLang);
  }
  applyTheme(currentTheme());
  for (const button of document.querySelectorAll('.theme-toggle')) {
    button.addEventListener('click', () => {
      const dark = currentTheme() === 'dark'
        || (currentTheme() === 'system' && matchMedia('(prefers-color-scheme: dark)').matches);
      applyTheme(dark ? 'light' : 'dark');
    });
  }
  try {
    const legacyFolder = localStorage.getItem('sd_folder');
    if (legacyFolder && !localStorage.getItem('hd_folder')) {
      localStorage.setItem('hd_folder', legacyFolder);
    }
  } catch (_) { /* private mode */ }
}

function applySiteName(name) {
  if (!name) return;
  document.title = name;
  const brand = $('brand-name');
  if (brand) brand.textContent = name;
  const login = $('login-title');
  if (login) login.textContent = name;
}

async function boot() {
  initChrome();
  try {
    const session = await api('/api/session');
    applySiteName(session.site_name);
    if (!session.authenticated) { showLogin(); return; }
    if (session.demo) { await enterApp(); return; }
    const tg = session.telegram || {};
    if (tg.state === 'ready') { await enterApp(); return; }
    show('view-setup');
    applySetupState(tg.state);
    if (tg.error) $('setup-err').textContent = T(tg.error);
  } catch (error) {
    showLogin();
    $('login-err').textContent = error.message;
  }
}

function applySetupState(tgState) {
  $('setup-step-1').classList.toggle('hidden', tgState === 'awaiting_code' || tgState === 'awaiting_password');
  $('setup-step-2').classList.toggle('hidden', tgState !== 'awaiting_code');
  $('setup-step-3').classList.toggle('hidden', tgState !== 'awaiting_password');
}

async function enterApp() {
  show('view-main');
  const session = await api('/api/session');
  state.demo = Boolean(session.demo);
  const user = session.telegram && session.telegram.user;
  $('account-line').textContent = user
    ? `@${user.username || user.name}`
    : (session.demo ? T('演示模式') : '');
  $('btn-tg-logout').classList.toggle('hidden', Boolean(session.demo));
  try {
    await loadFolders();
  } catch (error) {
    // A transient network error must not look like a logout.
    toast(T('加载失败：{msg}', { msg: error.message }), true);
    $('folder-title').textContent = T('加载失败');
    $('files-page').classList.add('hidden');
    $('empty').classList.remove('hidden');
    $('empty').innerHTML = `<p>${escapeHtml(T('加载失败：{msg}', { msg: error.message }))}</p><button class="ghost" id="retry-folders">${escapeHtml(T('重试'))}</button>`;
    $('retry-folders')?.addEventListener('click', () => loadFolders().catch((e) => toast(e.message, true)));
  }
}

/* ---------- login / setup ---------- */

$('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('login-err').textContent = '';
  try {
    await api('/api/login', { method: 'POST', body: { password: $('login-password').value } });
    $('login-password').value = '';
    await boot();
  } catch (error) {
    $('login-err').textContent = error.message;
  }
});

$('btn-logout').addEventListener('click', async () => {
  try { await api('/api/logout', { method: 'POST' }); } catch (_) { /* ignore */ }
  showLogin();
});

$('btn-tg-logout').addEventListener('click', async () => {
  if (!confirm(T('断开 Telegram？下次需要重新登录。'))) return;
  try {
    await api('/api/tg/logout', { method: 'POST' });
    location.reload();
  } catch (error) { toast(error.message, true); }
});

$('btn-send-code').addEventListener('click', async () => {
  $('setup-err').textContent = '';
  const button = $('btn-send-code');
  button.disabled = true;
  button.textContent = T('发送中…');
  try {
    const result = await api('/api/tg/login/start', {
      method: 'POST',
      body: { api_id: Number($('api-id').value), api_hash: $('api-hash').value.trim(), phone: $('api-phone').value.trim() },
    });
    applySetupState(result.state);
    $('setup-note').textContent = result.code_type === 'SentCodeTypeApp'
      ? T('验证码已经发到你的 Telegram 客户端里了。') : '';
  } catch (error) {
    $('setup-err').textContent = error.message;
  } finally {
    button.disabled = false;
    button.textContent = T('发送验证码');
  }
});

$('btn-submit-code').addEventListener('click', async () => {
  $('setup-err').textContent = '';
  try {
    const result = await api('/api/tg/login/code', { method: 'POST', body: { code: $('login-code').value.trim() } });
    if (result.state === 'awaiting_password') { applySetupState('awaiting_password'); return; }
    await enterApp();
  } catch (error) { $('setup-err').textContent = error.message; }
});

$('btn-submit-2fa').addEventListener('click', async () => {
  $('setup-err').textContent = '';
  try {
    await api('/api/tg/login/password', { method: 'POST', body: { password: $('login-2fa').value } });
    await enterApp();
  } catch (error) { $('setup-err').textContent = error.message; }
});

/* ---------- folders ---------- */

async function loadFolders() {
  const data = await api('/api/folders');
  state.folders = data.folders || [];
  renderFolders();
  if (state.folders.length) {
    let saved = 0;
    try { saved = Number(localStorage.getItem('hd_folder') || 0); } catch (_) { saved = 0; }
    const target = state.folders.find((f) => f.id === saved) || state.folders[0];
    await selectFolder(target.id);
  } else {
    openFilesPage();
    $('folder-title').textContent = T('还没有文件夹');
    $('file-grid').innerHTML = '';
    $('empty').classList.remove('hidden');
  }
}

function renderFolders() {
  const nav = $('folder-list');
  nav.innerHTML = '';
  for (const folder of state.folders) {
    const button = document.createElement('button');
    button.className = 'folder' + (folder.id === state.folderId ? ' active' : '');
    button.innerHTML = `<span class="icon">${icon('i-folder')}</span><span class="name">${escapeHtml(folder.name)}</span>`;
    button.addEventListener('click', () => { closeSidebar(); selectFolder(folder.id); });
    button.addEventListener('contextmenu', (event) => {
      event.preventDefault();
      if (confirm(T('删除文件夹「{name}」？频道里的文件也会一起删掉。', { name: folder.name }))) {
        api(`/api/folders/${folder.id}`, { method: 'DELETE' })
          .then(() => { toast(T('已删除')); loadFolders(); })
          .catch((error) => toast(error.message, true));
      }
    });
    nav.appendChild(button);
  }
}

function setActiveNav(page) {
  for (const box of document.querySelectorAll('#bottom-nav .nav-box')) {
    box.classList.toggle('active', box.dataset.nav === page);
  }
}

function openFilesPage() {
  state.page = 'files';
  setActiveNav('files');
  $('files-page').classList.remove('hidden');
  $('stats-page').classList.add('hidden');
  $('settings-page').classList.add('hidden');
  $('topbar-files').classList.remove('hidden');
  $('folder-title').textContent =
    (state.folders.find((f) => f.id === state.folderId) || {}).name || T('文件');
  if (state.statsTimer) { clearInterval(state.statsTimer); state.statsTimer = null; }
}

async function selectFolder(folderId) {
  state.folderId = folderId;
  state.selection.clear();
  updateBatchBar();
  try { localStorage.setItem('hd_folder', String(folderId)); } catch (_) { /* ignore */ }
  renderFolders();
  closeSidebar();
  openFilesPage();
  await loadFiles(true);
}

$('btn-new-folder').addEventListener('click', () => {
  openDialog(`
    <h3>${escapeHtml(T('新建文件夹'))}</h3>
    <p class="muted small">${escapeHtml(T('会在 Telegram 里建一个私有频道，名字带 [TD] 标记。'))}</p>
    <input id="new-folder-name" placeholder="${escapeHtml(T('比如：电影'))}">
    <div class="dialog-actions"><button class="ghost" data-close>${escapeHtml(T('取消'))}</button><button class="primary" id="do-create-folder">${escapeHtml(T('创建'))}</button></div>
  `);
  $('do-create-folder').addEventListener('click', async () => {
    const name = $('new-folder-name').value.trim();
    if (!name) return;
    try {
      await api('/api/folders', { method: 'POST', body: { name } });
      closeDialog();
      toast(T('文件夹建好了'));
      await loadFolders();
    } catch (error) { toast(error.message, true); }
  });
});

/* ---------- files ---------- */

async function loadFiles(reset) {
  if (state.loading && !reset) return;
  const seq = ++state.reqSeq;
  state.loading = true;
  if (reset) {
    state.files = [];
    state.nextOffset = 0;
    state.selection.clear();
    updateBatchBar();
    $('file-grid').innerHTML = '<div class="skeleton"></div>';
  }
  try {
    const params = new URLSearchParams({ limit: '60' });
    if (state.nextOffset) params.set('offset', String(state.nextOffset));
    if (state.query) params.set('q', state.query);
    const data = await api(`/api/folders/${state.folderId}/files?${params}`);
    if (seq !== state.reqSeq) return;  // a newer request owns the view now
    state.files = reset ? data.items : state.files.concat(data.items);
    state.nextOffset = data.next_offset || 0;
    renderFiles();
  } catch (error) {
    if (seq === state.reqSeq) {
      toast(error.message, true);
      if (reset) {
        $('file-grid').innerHTML = '';
        $('empty').classList.remove('hidden');
        $('empty').innerHTML = `<p>${escapeHtml(T('加载失败：{msg}', { msg: error.message }))}</p><button class="ghost" id="retry-load">${escapeHtml(T('重试'))}</button>`;
        const retry = $('retry-load');
        if (retry) retry.addEventListener('click', () => loadFiles(true));
      }
    }
  } finally {
    if (seq === state.reqSeq) state.loading = false;
  }
}

function fileIcon(kind) {
  return icon(ICON_NAMES[kind] || 'i-file');
}

function renderFiles() {
  const grid = $('file-grid');
  grid.innerHTML = '';
  const isEmpty = state.files.length === 0;
  $('empty').classList.toggle('hidden', !isEmpty);
  if (isEmpty) {
    $('empty').innerHTML = state.query
      ? `<p>${escapeHtml(T('没有匹配「{q}」的文件。', { q: state.query }))}</p>`
      : `<p>${escapeHtml(T('这里还是空的。'))}</p><p class="muted">${escapeHtml(T('把文件拖进来，或者点右上角「上传」。'))}</p>`;
  }
  $('load-more').classList.toggle('hidden', !state.nextOffset);
  for (const file of state.files) {
    const card = document.createElement('article');
    card.className = 'file-card' + (state.selection.has(file.id) ? ' selected' : '');
    card.tabIndex = 0;
    const thumb = `${BASE}/api/folders/${state.folderId}/files/${file.id}/thumb`;
    const media = file.has_thumb
      ? `<img loading="lazy" src="${thumb}" alt="">`
      : (file.kind === 'image'
        ? `<img loading="lazy" src="${BASE}/api/folders/${state.folderId}/files/${file.id}/stream" alt="">`
        : `<div class="file-icon">${fileIcon(file.kind)}</div>`);
    const parts = file.parts > 1 ? ` · ${file.parts} ${T('分片')}` : '';
    card.innerHTML = `
      <div class="thumb-wrap">${media}${state.selecting ? '<span class="pick"></span>' : ''}</div>
      <div class="file-meta">
        <div class="file-name" title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</div>
        <div class="file-sub">${bytes(file.size)}${escapeHtml(parts)} · ${escapeHtml(when(file.date))}</div>
      </div>
      <div class="file-actions">
        <button class="ghost icon" data-act="download" title="${escapeHtml(T('下载'))}">⬇</button>
        <button class="ghost icon" data-act="share" title="${escapeHtml(T('分享'))}">🔗</button>
        <button class="ghost icon" data-act="more" title="${escapeHtml(T('更多'))}">⋯</button>
      </div>`;
    const img = card.querySelector('.thumb-wrap img');
    if (img) img.addEventListener('error', () => {
      const fallback = document.createElement('div');
      fallback.className = 'file-icon';
      fallback.innerHTML = fileIcon(file.kind);
      img.replaceWith(fallback);
    });
    card.addEventListener('click', (event) => {
      if (event.target.closest('.file-actions')) return;
      if (state.selecting) { toggleSelect(file.id); return; }
      openPreview(file);
    });
    card.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        if (state.selecting) toggleSelect(file.id); else openPreview(file);
      }
    });
    card.querySelector('[data-act="download"]').addEventListener('click', () => downloadFile(file));
    card.querySelector('[data-act="share"]').addEventListener('click', () => shareFile(file));
    card.querySelector('[data-act="more"]').addEventListener('click', () => fileMenu(file));
    grid.appendChild(card);
  }
}

function toggleSelect(id) {
  if (state.selection.has(id)) state.selection.delete(id); else state.selection.add(id);
  updateBatchBar();
  const cards = $('file-grid').querySelectorAll('.file-card');
  state.files.forEach((file, index) => {
    if (cards[index]) cards[index].classList.toggle('selected', state.selection.has(file.id));
  });
}

function updateBatchBar() {
  $('batch-count').textContent = T('已选 {n} 项', { n: state.selection.size });
  $('batch-bar').classList.toggle('hidden', !state.selecting);
  $('btn-select').classList.toggle('active', state.selecting);
}

/* ---------- download (browser-side part merge) ---------- */

function downloadDirect(file, folderId) {
  const link = document.createElement('a');
  link.href = `${BASE}/api/folders/${folderId}/files/${file.id}/download`;
  link.download = file.name;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

function makeTransferRow(name, kind) {
  $('upload-panel').classList.remove('hidden');
  const row = document.createElement('div');
  row.className = 'upload-row';
  row.innerHTML = `
    <div class="upload-name">${escapeHtml(name)} <span class="transfer-kind">${escapeHtml(kind)}</span></div>
    <div class="bar"><div class="bar-fill"></div></div>
    <div class="upload-foot">
      <span class="upload-status">${escapeHtml(T('准备中…'))}</span>
      <span class="upload-actions"></span>
    </div>`;
  $('upload-list').appendChild(row);
  return {
    row,
    fill: row.querySelector('.bar-fill'),
    status: row.querySelector('.upload-status'),
    actions: row.querySelector('.upload-actions'),
    paint(percent) { this.fill.style.width = `${Math.max(0, Math.min(100, percent))}%`; },
    setStatus(text, bad = false) {
      this.status.textContent = text;
      this.row.classList.toggle('bad', bad);
    },
    remove() { row.remove(); },
  };
}

async function downloadFile(file) {
  const folderId = state.folderId;
  const needsMerge = (file.parts || 1) > 1 || Number(file.size) > SMALL_DIRECT_DOWNLOAD;
  const canMerge = needsMerge && typeof window.showSaveFilePicker === 'function' && window.isSecureContext;
  if (!canMerge) {
    if (needsMerge) toast(T('浏览器不支持边下边合并，已切换直连下载'));
    downloadDirect(file, folderId);
    return;
  }

  // The picker must run inside the click's user activation, before any await.
  let picker;
  try {
    picker = window.showSaveFilePicker({ suggestedName: file.name });
  } catch (_) {
    return;
  }

  const transfer = makeTransferRow(file.name, T('下载'));
  const controller = new AbortController();
  let writable = null;
  let stopped = false;
  let received = 0;
  let partGot = 0;
  const total = Number(file.size) || 1;

  function paint() {
    const percent = Math.round(((received + partGot) / total) * 100);
    transfer.paint(percent);
    transfer.setStatus(T('下载中 {p}%', { p: Math.min(100, percent) }));
  }

  const cancel = document.createElement('button');
  cancel.className = 'ghost icon';
  cancel.textContent = T('取消');
  cancel.addEventListener('click', async () => {
    stopped = true;
    controller.abort();
    try { if (writable) await writable.abort(); } catch (_) { /* ignore */ }
    transfer.remove();
  });
  transfer.actions.appendChild(cancel);

  try {
    const [handle, manifest] = await Promise.all([
      picker,
      api(`/api/folders/${folderId}/files/${file.id}/manifest`),
    ]);
    writable = await handle.createWritable();
    for (const part of manifest.parts) {
      if (stopped) return;
      partGot = 0;
      await downloadPart(file, folderId, part, writable, controller.signal, (chunkSize) => {
        partGot += chunkSize;
        paint();
      }, async () => {
        // A retried part must not append duplicate bytes: rewind the file to
        // the end of the last fully written part before fetching again.
        if (typeof writable.truncate === 'function') await writable.truncate(received);
        if (typeof writable.seek === 'function') await writable.seek(received);
        partGot = 0;
        paint();
      });
      received += Number(part.size) || 0;
      partGot = 0;
      paint();
    }
    if (stopped) return;
    transfer.setStatus(T('合并中…'));
    await writable.close();
    writable = null;
    transfer.paint(100);
    transfer.setStatus(T('已保存'));
    transfer.row.classList.add('done');
    transfer.actions.innerHTML = '';
  } catch (error) {
    if (stopped) return;
    try { if (writable) await writable.abort(); } catch (_) { /* ignore */ }
    transfer.setStatus(error.message || T('下载失败'), true);
    toast(error.message || T('下载失败'), true);
  }
}

async function downloadPart(file, folderId, part, writable, signal, onBytes, onReset) {
  if (!part.size) return;
  let attempt = 0;
  for (;;) {
    let got = 0;
    await onReset();
    try {
      const start = part.offset;
      const end = part.offset + part.size - 1;
      const response = await fetch(
        `${BASE}/api/folders/${folderId}/files/${file.id}/download`,
        { headers: { Range: `bytes=${start}-${end}` }, credentials: 'same-origin', signal },
      );
      if (response.status === 401) { showLogin(); throw new Error(T('登录已失效')); }
      if (!response.ok) throw new Error(T('请求失败 ({status})', { status: response.status }));
      if (response.status !== 206 && Number(part.size) < Number(file.size)) {
        throw new Error(T('请求失败 ({status})', { status: response.status }));
      }
      const reader = response.body.getReader();
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        if (!value || !value.length) continue;
        await writable.write(value);
        got += value.length;
        onBytes(value.length);
      }
      if (got !== Number(part.size)) throw new Error(T('下载失败'));
      return;
    } catch (error) {
      if (signal.aborted) throw error;
      attempt += 1;
      if (attempt > 3) throw error;
      await new Promise((resolve) => setTimeout(resolve, 500 * attempt));
    }
  }
}

/* ---------- file menu / rename / move ---------- */

function fileMenu(file) {
  openDialog(`
    <h3>${escapeHtml(file.name)}</h3>
    <p class="muted small">${bytes(file.size)} · ${escapeHtml(when(file.date))}</p>
    <div class="menu-list">
      <button class="ghost full" id="m-download">${escapeHtml(T('下载'))}</button>
      <button class="ghost full" id="m-rename">${escapeHtml(T('重命名'))}</button>
      <button class="ghost full" id="m-move">${escapeHtml(T('移动到…'))}</button>
      <button class="ghost full danger" id="m-delete">${escapeHtml(T('删除'))}</button>
    </div>
  `);
  $('m-download').addEventListener('click', () => { closeDialog(); downloadFile(file); });
  $('m-rename').addEventListener('click', () => { closeDialog(); renameFile(file); });
  $('m-move').addEventListener('click', () => { closeDialog(); moveFile(file); });
  $('m-delete').addEventListener('click', async () => {
    if (!confirm(T('确定删除「{name}」？', { name: file.name }))) return;
    try {
      await api(`/api/folders/${state.folderId}/files/${file.id}`, { method: 'DELETE' });
      closeDialog();
      toast(T('已删除'));
      await loadFiles(true);
    } catch (error) { toast(error.message, true); }
  });
}

function renameFile(file) {
  openDialog(`
    <h3>${escapeHtml(T('重命名'))}</h3>
    <p class="muted small">${escapeHtml(T('改的是网盘里显示的名字，立即生效。Telegram 客户端里仍是原始文件名（协议不支持改）。'))}</p>
    <input id="rename-input" value="${escapeHtml(file.name)}">
    <div class="dialog-actions"><button class="ghost" data-close>${escapeHtml(T('取消'))}</button><button class="primary" id="do-rename">${escapeHtml(T('确定'))}</button></div>
  `);
  const input = $('rename-input');
  input.focus();
  input.select();
  $('do-rename').addEventListener('click', async () => {
    const name = input.value.trim();
    if (!name || name === file.name) { closeDialog(); return; }
    const button = $('do-rename');
    button.disabled = true;
    button.textContent = T('处理中…');
    try {
      await api(`/api/folders/${state.folderId}/files/${file.id}/rename`, { method: 'POST', body: { name } });
      closeDialog();
      toast(T('已重命名'));
      await loadFiles(true);
    } catch (error) {
      toast(error.message, true);
      button.disabled = false;
      button.textContent = T('确定');
    }
  });
}

function moveFile(file) {
  const others = state.folders.filter((f) => f.id !== state.folderId);
  if (!others.length) { toast(T('没有别的文件夹'), true); return; }
  openDialog(`
    <h3>${escapeHtml(T('移动到'))}</h3>
    <div class="menu-list">
      ${others.map((f) => `<button class="ghost full" data-move="${f.id}">📁 ${escapeHtml(f.name)}</button>`).join('')}
    </div>
    <div class="dialog-actions"><button class="ghost" data-close>${escapeHtml(T('取消'))}</button></div>
  `);
  for (const button of document.querySelectorAll('[data-move]')) {
    button.addEventListener('click', async () => {
      try {
        await api(`/api/folders/${state.folderId}/files/${file.id}/move`, {
          method: 'POST', body: { to: Number(button.dataset.move) },
        });
        closeDialog();
        toast(T('已移动'));
        await loadFiles(true);
      } catch (error) { toast(error.message, true); }
    });
  }
}

/* ---------- batch ---------- */

$('btn-select').addEventListener('click', () => {
  state.selecting = !state.selecting;
  if (!state.selecting) state.selection.clear();
  updateBatchBar();
  renderFiles();
});

$('batch-cancel').addEventListener('click', () => {
  state.selecting = false;
  state.selection.clear();
  updateBatchBar();
  renderFiles();
});

$('batch-download').addEventListener('click', () => {
  const chosen = state.files.filter((f) => state.selection.has(f.id));
  if (!chosen.length) { toast(T('先选文件'), true); return; }
  const folderId = state.folderId;
  // A save picker needs its own user gesture; batch stays on direct downloads.
  chosen.forEach((file, index) => setTimeout(() => downloadDirect(file, folderId), index * 350));
});

$('batch-delete').addEventListener('click', async () => {
  const chosen = state.files.filter((f) => state.selection.has(f.id));
  if (!chosen.length) { toast(T('先选文件'), true); return; }
  if (!confirm(T('删除选中的 {n} 个文件？', { n: chosen.length }))) return;
  let failed = 0;
  for (const file of chosen) {
    try { await api(`/api/folders/${state.folderId}/files/${file.id}`, { method: 'DELETE' }); }
    catch (_) { failed += 1; }
  }
  toast(failed ? T('完成，{n} 个失败', { n: failed }) : T('已删除'), failed > 0);
  state.selection.clear();
  await loadFiles(true);
});

$('batch-move').addEventListener('click', () => {
  const chosen = state.files.filter((f) => state.selection.has(f.id));
  if (!chosen.length) { toast(T('先选文件'), true); return; }
  const others = state.folders.filter((f) => f.id !== state.folderId);
  if (!others.length) { toast(T('没有别的文件夹'), true); return; }
  openDialog(`
    <h3>${escapeHtml(T('移动 {n} 个文件到', { n: chosen.length }))}</h3>
    <div class="menu-list">
      ${others.map((f) => `<button class="ghost full" data-bmove="${f.id}">📁 ${escapeHtml(f.name)}</button>`).join('')}
    </div>
    <div class="dialog-actions"><button class="ghost" data-close>${escapeHtml(T('取消'))}</button></div>
  `);
  for (const button of document.querySelectorAll('[data-bmove]')) {
    button.addEventListener('click', async () => {
      const target = Number(button.dataset.bmove);
      closeDialog();
      let failed = 0;
      for (const file of chosen) {
        try {
          await api(`/api/folders/${state.folderId}/files/${file.id}/move`, { method: 'POST', body: { to: target } });
        } catch (_) { failed += 1; }
      }
      toast(failed ? T('完成，{n} 个失败', { n: failed }) : T('已移动'), failed > 0);
      state.selection.clear();
      await loadFiles(true);
    });
  }
});

/* ---------- upload: browser slices, server sends one part ---------- */

function uploadKey(file, folderId) {
  return `hd_up_${folderId}_${file.size}_${file.lastModified || 0}_${file.name}`;
}

function fnvPair(text) {
  let a = 0x811c9dc5;
  let b = 0x01000193;
  for (let i = 0; i < text.length; i += 1) {
    const code = text.charCodeAt(i);
    a ^= code; a = Math.imul(a, 0x01000193) >>> 0;
    b ^= code + 0x9e3779b9; b = Math.imul(b, 0x85ebca6b) >>> 0;
  }
  return (a >>> 0).toString(16).padStart(8, '0') + (b >>> 0).toString(16).padStart(8, '0');
}

function groupKeyFor(file, folderId) {
  return fnvPair(`${folderId}|${file.name}|${file.size}|${file.lastModified || 0}`);
}

function sizesFor(size, partBytes) {
  if (size <= 0) return [0];
  const count = Math.ceil(size / partBytes);
  return Array.from({ length: count }, (_, index) => Math.min(partBytes, size - index * partBytes));
}

function uploadFiles(fileList) {
  if (!state.folderId) { toast(T('先选一个文件夹'), true); return; }
  if (!fileList.length) return;
  $('upload-panel').classList.remove('hidden');
  for (const file of fileList) startUpload(file);
}

async function startUpload(file) {
  const folderId = state.folderId;
  const key = uploadKey(file, folderId);
  if (state.uploads.has(key)) return;
  const transfer = makeTransferRow(file.name, T('上传'));
  state.uploads.set(key, true);
  const groupKey = groupKeyFor(file, folderId);
  let stopped = false;
  let controller = null;
  let sessionId = null;
  let partBytes = 0;
  let doneBytes = 0;

  const cancel = document.createElement('button');
  cancel.className = 'ghost icon';
  cancel.textContent = T('取消');
  cancel.addEventListener('click', async () => {
    if (!confirm(T('取消上传？'))) return;
    stopped = true;
    try { controller?.abort(); } catch (_) { /* ignore */ }
    transfer.setStatus(T('已取消'));
    state.uploads.delete(key);
    try { if (sessionId) await api(`/api/uploads/${sessionId}`, { method: 'DELETE' }); } catch (_) { /* ignore */ }
    try { await api(`/api/parts/${groupKey}`, { method: 'DELETE' }); } catch (_) { /* ignore */ }
    try { localStorage.removeItem(key); } catch (_) { /* ignore */ }
    transfer.remove();
  });
  transfer.actions.appendChild(cancel);

  function paint() {
    const percent = file.size ? Math.round((doneBytes / file.size) * 100) : 100;
    transfer.paint(percent);
  }

  function retryButton() {
    transfer.actions.querySelector('[data-retry]')?.remove();
    const retry = document.createElement('button');
    retry.className = 'ghost icon';
    retry.dataset.retry = '1';
    retry.textContent = T('重试');
    retry.addEventListener('click', () => {
      retry.remove();
      transfer.row.classList.remove('bad');
      run().catch(() => { /* handled in run */ });
    });
    transfer.actions.appendChild(retry);
  }

  async function run() {
    try {
      const limits = await api('/api/parts/limits');
      const saved = (() => { try { return localStorage.getItem(key); } catch (_) { return null; } })();
      let plan = null;
      if (saved) {
        // Only probe the server when this browser remembers an earlier attempt;
        // a brand-new upload has no group yet and must not log a 404.
        try {
          plan = await api(`/api/parts/${groupKey}`);
        } catch (error) {
          if (error.status !== 404) throw error;
          // A group disappears only after every part was registered.
          localStorage.removeItem(key);
          state.uploads.delete(key);
          transfer.paint(100);
          transfer.setStatus(T('完成'));
          transfer.row.classList.add('done');
          if (state.folderId === folderId && state.page === 'files') await loadFiles(true);
          return;
        }
      }
      partBytes = plan ? Number(plan.part_bytes) : Number(limits.part_bytes);
      const computed = sizesFor(Number(file.size) || 0, partBytes);
      const partSizes = plan ? plan.parts.map((part) => Number(part.size)) : computed;
      const partCount = plan ? Number(plan.part_count) : computed.length;
      const doneSet = new Set();
      doneBytes = 0;
      if (plan) {
        for (const part of plan.parts) {
          if (part.state === 'done') { doneSet.add(part.index); doneBytes += Number(part.size); }
        }
      }
      paint();

      for (let index = 0; index < partCount; index += 1) {
        if (stopped) return;
        if (doneSet.has(index)) continue;
        const partSize = partSizes[index];
        const partOffset = index * partBytes;
        const session = await api('/api/parts', {
          method: 'POST',
          body: {
            folder_id: folderId, name: file.name, size: Number(file.size) || 0,
            part_bytes: partBytes, part_index: index, part_count: partCount,
            group_key: groupKey,
          },
        });
        if (session.state === 'done') {
          doneBytes += partSize; paint(); continue;
        }
        sessionId = session.id;
        let offset = Number(session.offset) || 0;
        const chunkSize = Number(session.chunk_size) || Number(limits.chunk_size);
        transfer.setStatus(T('第 {i}/{n} 片', { i: index + 1, n: partCount }));
        let streaming = false;
        let sentBytes = 0;
        while (offset < partSize) {
          if (stopped) return;
          const end = Math.min(offset + chunkSize, partSize);
          const blob = file.slice(partOffset + offset, partOffset + end);
          let attempt = 0;
          for (;;) {
            try {
              controller = new AbortController();
              // A streaming PUT can sit behind Telegram's buffer, so give a
              // stalled pump more room before declaring the chunk dead.
              const timer = setTimeout(() => controller.abort(), 300000);
              const response = await fetch(`${BASE}/api/uploads/${sessionId}?offset=${offset}`, {
                method: 'PUT',
                headers: { ...MUTATION_HEADERS, 'Content-Type': 'application/octet-stream' },
                body: blob,
                credentials: 'same-origin',
                signal: controller.signal,
              });
              clearTimeout(timer);
              if (response.status === 401) { showLogin(); throw new Error(T('登录已失效')); }
              if (response.status === 409) {
                const data = await response.json();
                offset = Number(data.offset) || offset;
                break;
              }
              if (!response.ok) {
                const data = await response.json().catch(() => ({}));
                const error = new Error(errorText(data, response.status));
                error.status = response.status;
                throw error;
              }
              const data = await response.json();
              offset = Number(data.offset);
              if (data.stream) {
                streaming = true;
                sentBytes = Number(data.sent) || 0;
              }
              break;
            } catch (error) {
              if (stopped) return;
              attempt += 1;
              if (attempt > 3) throw error;
              await new Promise((resolve) => setTimeout(resolve, 600 * attempt));
            }
          }
          const overall = (doneBytes + offset) / (file.size || 1);
          transfer.paint(Math.round(overall * 100));
          const partPercent = Math.round((offset / (partSize || 1)) * 100);
          if (streaming) {
            // Bytes leave the browser while Telegram's copy is already moving;
            // show both sides instead of pretending the upload starts after.
            const tgPercent = Math.min(100, Math.round((sentBytes / (partSize || 1)) * 100));
            transfer.setStatus(T('直传中 {p}%（Telegram {q}%）', { p: partPercent, q: tgPercent }));
          } else {
            transfer.setStatus(T('上传中 {p}%', { p: partPercent }));
          }
        }
        const job = await api(`/api/uploads/${sessionId}/complete`, { method: 'POST' });
        await waitJob(job.id, (sent, total) => {
          const percent = total ? Math.round((sent / total) * 100) : 0;
          transfer.setStatus(T('上传到 Telegram {p}%', { p: percent }));
        });
        doneBytes += partSize;
        paint();
        sessionId = null;
        try { localStorage.setItem(key, groupKey); } catch (_) { /* ignore */ }
      }

      try { localStorage.removeItem(key); } catch (_) { /* ignore */ }
      state.uploads.delete(key);
      transfer.paint(100);
      transfer.setStatus(T('完成'));
      transfer.row.classList.add('done');
      transfer.actions.innerHTML = '';
      if (state.folderId === folderId && state.page === 'files') await loadFiles(true);
      if (state.page === 'stats') loadStats();
    } catch (error) {
      if (stopped) return;
      state.uploads.delete(key);
      transfer.setStatus(`${error.message}${T('（可重试）')}`, true);
      retryButton();
    }
  }

  await run();
}

async function waitJob(jobId, onProgress) {
  for (;;) {
    const job = await api(`/api/jobs/${jobId}`);
    if (job.state === 'done') return job;
    if (job.state === 'error') {
      const error = new Error(errorText({ detail: job.error || '失败' }, 500));
      error.jobId = jobId;
      throw error;
    }
    if (job.state === 'running') onProgress(job.sent, job.total);
    else onProgress(0, job.total);
    await new Promise((resolve) => setTimeout(resolve, 900));
  }
}

$('file-input').addEventListener('change', (event) => {
  uploadFiles([...event.target.files]);
  event.target.value = '';
});

$('btn-close-uploads').addEventListener('click', () => {
  $('upload-panel').classList.add('hidden');
  $('upload-list').innerHTML = '';
});

let dragDepth = 0;
document.addEventListener('dragenter', (event) => {
  if (!state.folderId || state.page !== 'files') return;
  event.preventDefault();
  dragDepth += 1;
  $('drop-hint').classList.remove('hidden');
});
document.addEventListener('dragleave', () => {
  dragDepth = Math.max(0, dragDepth - 1);
  if (!dragDepth) $('drop-hint').classList.add('hidden');
});
document.addEventListener('dragover', (event) => event.preventDefault());
document.addEventListener('drop', (event) => {
  event.preventDefault();
  dragDepth = 0;
  $('drop-hint').classList.add('hidden');
  if (state.folderId && state.page === 'files' && event.dataTransfer.files.length) {
    uploadFiles([...event.dataTransfer.files]);
  }
});

window.addEventListener('beforeunload', (event) => {
  if (state.uploads.size) {
    event.preventDefault();
    event.returnValue = '';
  }
});

/* ---------- preview ---------- */

function streamUrl(file, inline = true) {
  return `${BASE}/api/folders/${state.folderId}/files/${file.id}/stream?inline=${inline ? 1 : 0}`;
}

let previewSeq = 0;

function openPreview(file) {
  const seq = ++previewSeq;
  const body = $('preview-body');
  $('preview-name').textContent = file.name;
  $('preview-download').href = `${BASE}/api/folders/${state.folderId}/files/${file.id}/download`;
  $('preview-download').setAttribute('download', file.name);
  if (file.kind === 'image') {
    body.innerHTML = `<img src="${streamUrl(file)}" alt="">`;
  } else if (file.kind === 'video') {
    const poster = file.has_thumb ? ` poster="${BASE}/api/folders/${state.folderId}/files/${file.id}/thumb"` : '';
    body.innerHTML = `<video src="${streamUrl(file)}" controls preload="auto" playsinline${poster}></video>`;
  } else if (file.kind === 'audio') {
    body.innerHTML = `<audio src="${streamUrl(file)}" controls preload="auto"></audio>`;
  } else if (file.kind === 'pdf') {
    body.innerHTML = `<iframe src="${streamUrl(file)}"></iframe>`;
  } else if (file.kind === 'text') {
    body.innerHTML = `<pre>${escapeHtml(T('加载中…'))}</pre>`;
    fetch(streamUrl(file), { headers: { 'Range': 'bytes=0-200000' } })
      .then((r) => r.text())
      .then((text) => {
        if (seq !== previewSeq) return;  // a newer preview owns the modal
        body.innerHTML = `<pre>${escapeHtml(text)}</pre>`;
      })
      .catch(() => { if (seq === previewSeq) body.innerHTML = `<pre>${escapeHtml(T('读取失败'))}</pre>`; });
  } else {
    body.innerHTML = `<div class="preview-none"><div>${fileIcon(file.kind)}</div><p>${escapeHtml(T('这个类型不能在线预览'))}</p></div>`;
  }
  $('preview').classList.remove('hidden');
}

$('preview-close').addEventListener('click', () => {
  $('preview-body').innerHTML = '';
  $('preview').classList.add('hidden');
});
$('preview').addEventListener('click', (event) => {
  if (event.target === $('preview')) $('preview-close').click();
});
document.addEventListener('keydown', (event) => {
  if (event.key === 'Escape') {
    if (!$('preview').classList.contains('hidden')) $('preview-close').click();
    else closeDialog();
  }
});

/* ---------- share ---------- */

function shareFile(file) {
  openDialog(`
    <h3>${escapeHtml(T('分享「{name}」', { name: file.name }))}</h3>
    <label>${escapeHtml(T('有效期（小时，0 = 永久）'))}<input id="share-hours" type="number" min="0" value="24"></label>
    <label>${escapeHtml(T('访问密码（可留空）'))}<input id="share-password" type="password" placeholder="${escapeHtml(T('访问密码（可留空）'))}"></label>
    <div class="dialog-actions"><button class="ghost" data-close>${escapeHtml(T('取消'))}</button><button class="primary" id="do-share">${escapeHtml(T('生成链接'))}</button></div>
    <div id="share-result"></div>
  `);
  $('do-share').addEventListener('click', async () => {
    const button = $('do-share');
    if (button.disabled) return;
    button.disabled = true;
    button.textContent = T('生成中…');
    try {
      const result = await api('/api/share', {
        method: 'POST',
        body: {
          folder_id: state.folderId,
          message_id: file.id,
          hours: Number($('share-hours').value || 0),
          password: $('share-password').value,
        },
      });
      const url = `${location.origin}${result.url}`;
      $('share-result').innerHTML = `<p class="muted small">${escapeHtml(T('链接'))}</p><input id="share-url" readonly value="${escapeHtml(url)}">`;
      $('share-url').select();
      navigator.clipboard?.writeText(url)
        .then(() => toast(T('链接已复制')))
        .catch(() => toast(T('复制失败，请手动选中复制'), true));
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = T('生成链接');
    }
  });
}

$('btn-share-folder').addEventListener('click', () => {
  if (!state.folderId) { toast(T('先选一个文件夹'), true); return; }
  const folder = state.folders.find((f) => f.id === state.folderId);
  openDialog(`
    <h3>${escapeHtml(T('分享文件夹「{name}」', { name: folder ? folder.name : '' }))}</h3>
    <p class="muted small">${escapeHtml(T('访客能看到这个文件夹里的文件列表并逐个下载。'))}</p>
    <label>${escapeHtml(T('有效期（小时，0 = 永久）'))}<input id="folder-share-hours" type="number" min="0" value="24"></label>
    <label>${escapeHtml(T('访问密码（可留空）'))}<input id="folder-share-password" type="password" placeholder="${escapeHtml(T('访问密码（可留空）'))}"></label>
    <div class="dialog-actions"><button class="ghost" data-close>${escapeHtml(T('取消'))}</button><button class="primary" id="do-share-folder">${escapeHtml(T('生成链接'))}</button></div>
    <div id="folder-share-result"></div>
  `);
  $('do-share-folder').addEventListener('click', async () => {
    const button = $('do-share-folder');
    button.disabled = true;
    button.textContent = T('生成中…');
    try {
      const result = await api('/api/share', {
        method: 'POST',
        body: {
          folder_id: state.folderId,
          message_id: 0,
          hours: Number($('folder-share-hours').value || 0),
          password: $('folder-share-password').value,
        },
      });
      const url = `${location.origin}${result.url}`;
      $('folder-share-result').innerHTML = `<p class="muted small">${escapeHtml(T('链接'))}</p><input id="folder-share-url" readonly value="${escapeHtml(url)}">`;
      $('folder-share-url').select();
      navigator.clipboard?.writeText(url).then(() => toast(T('链接已复制'))).catch(() => {});
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = T('生成链接');
    }
  });
});

$('btn-shares').addEventListener('click', async () => {
  try {
    const data = await api('/api/shares');
    const rows = (data.shares || []).map((share) => {
      const link = `${location.origin}/s/${share.token}`;
      const expiry = share.expires_at ? when(share.expires_at) : T('永久');
      const kind = share.message_id ? T('文件条目') : T('文件夹');
      return `<li class="share-row">
        <div><b>${escapeHtml(kind)}</b> · ${escapeHtml(expiry)} · ${escapeHtml(T('下载 {n} 次', { n: share.downloads }))}</div>
        <div class="share-actions">
          <button class="ghost icon" data-copy="${escapeHtml(link)}">${escapeHtml(T('复制'))}</button>
          <button class="ghost icon danger" data-revoke="${escapeHtml(share.token)}">${escapeHtml(T('撤销'))}</button>
        </div></li>`;
    }).join('');
    openDialog(`<h3>${escapeHtml(T('我的分享'))}</h3><ul class="share-list">${rows || `<li class="muted">${escapeHtml(T('还没有分享链接'))}</li>`}</ul>
      <div class="dialog-actions"><button class="ghost" data-close>${escapeHtml(T('关闭'))}</button></div>`);
    for (const button of document.querySelectorAll('[data-copy]')) {
      button.addEventListener('click', () => {
        navigator.clipboard?.writeText(button.dataset.copy).catch(() => {});
        toast(T('已复制'));
      });
    }
    for (const button of document.querySelectorAll('[data-revoke]')) {
      button.addEventListener('click', async () => {
        try {
          await api(`/api/share/${button.dataset.revoke}`, { method: 'DELETE' });
          button.closest('li').remove();
          toast(T('已撤销'));
        } catch (error) { toast(error.message, true); }
      });
    }
  } catch (error) { toast(error.message, true); }
});

/* ---------- stats dashboard ---------- */

function niceMax(value) {
  if (!value || value <= 0) return 1;
  const exponent = Math.floor(Math.log10(value));
  const magnitude = 10 ** exponent;
  const fraction = value / magnitude;
  const step = fraction <= 1 ? 1 : fraction <= 2 ? 2 : fraction <= 5 ? 5 : 10;
  return step * magnitude;
}

function renderTrafficChart(series) {
  const host = $('traffic-chart');
  const width = Math.max(320, host.clientWidth || 680);
  const height = 240;
  const margin = { top: 18, right: 14, bottom: 30, left: 58 };
  const plotW = width - margin.left - margin.right;
  const plotH = height - margin.top - margin.bottom;
  const totals = series.map((day) => day.up + day.down);
  const max = niceMax(Math.max(1, ...totals));
  const band = plotW / Math.max(1, series.length);
  const barWidth = Math.max(2, Math.min(18, band * 0.62));
  const labelEvery = Math.max(1, Math.ceil(series.length / 6));
  let svg = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${escapeHtml(T('上传/下载流量'))}">`;

  for (let step = 0; step <= 4; step += 1) {
    const value = (max / 4) * step;
    const y = margin.top + plotH - (value / max) * plotH;
    svg += `<line class="grid-line" x1="${margin.left}" y1="${y.toFixed(1)}" x2="${width - margin.right}" y2="${y.toFixed(1)}"></line>`;
    svg += `<text class="axis-text" x="${margin.left - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end">${escapeHtml(bytes(value))}</text>`;
  }

  series.forEach((day, index) => {
    const total = day.up + day.down;
    const totalH = (total / max) * plotH;
    const upH = (day.up / max) * plotH;
    const downH = (day.down / max) * plotH;
    const x = margin.left + index * band + (band - barWidth) / 2;
    const bottom = margin.top + plotH;
    const y = bottom - totalH;
    if (downH > 0.5) {
      const gap = upH > 0.5 ? 1 : 0;
      svg += `<rect class="bar-down" x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barWidth.toFixed(1)}" height="${Math.max(1, downH - gap).toFixed(1)}" rx="2"></rect>`;
    }
    if (upH > 0.5) {
      const gap = downH > 0.5 ? 1 : 0;
      svg += `<rect class="bar-up" x="${x.toFixed(1)}" y="${(bottom - upH + gap).toFixed(1)}" width="${barWidth.toFixed(1)}" height="${Math.max(1, upH - gap).toFixed(1)}" rx="2"></rect>`;
    }
    if (index % labelEvery === 0 || index === series.length - 1) {
      svg += `<text class="axis-text" x="${(margin.left + index * band + band / 2).toFixed(1)}" y="${height - 8}" text-anchor="middle">${escapeHtml(dateLabel(day.day))}</text>`;
    }
    svg += `<rect class="hit" tabindex="0" role="button" aria-label="${escapeHtml(`${day.day} ${T('上传')} ${bytes(day.up)} ${T('下载')} ${bytes(day.down)}`)}" data-day="${escapeHtml(day.day)}" data-up="${day.up}" data-down="${day.down}" x="${(margin.left + index * band).toFixed(1)}" y="${margin.top}" width="${band.toFixed(1)}" height="${plotH}"></rect>`;
  });

  const last = series[series.length - 1];
  if (last && last.up + last.down > 0) {
    const lastTotal = last.up + last.down;
    const x = margin.left + (series.length - 1) * band + band / 2;
    const y = margin.top + plotH - (lastTotal / max) * plotH - 6;
    svg += `<text class="bar-label" x="${x.toFixed(1)}" y="${y.toFixed(1)}" text-anchor="middle">${escapeHtml(bytes(lastTotal))}</text>`;
  }
  svg += '</svg><div class="chart-tooltip hidden"></div>';
  host.innerHTML = svg;

  const tooltip = host.querySelector('.chart-tooltip');
  for (const hit of host.querySelectorAll('.hit')) {
    const show = (event) => {
      const up = Number(hit.dataset.up);
      const down = Number(hit.dataset.down);
      tooltip.innerHTML = `<b>${escapeHtml(hit.dataset.day)}</b><br>${escapeHtml(T('上传'))} ${escapeHtml(bytes(up))}<br>${escapeHtml(T('下载'))} ${escapeHtml(bytes(down))}<br>${escapeHtml(T('总计'))} ${escapeHtml(bytes(up + down))}`;
      tooltip.classList.remove('hidden');
      const box = host.getBoundingClientRect();
      const point = event && event.clientX !== undefined
        ? { x: event.clientX - box.left, y: event.clientY - box.top }
        : { x: width / 2, y: 12 };
      tooltip.style.left = `${Math.min(Math.max(6, point.x + 12), box.width - 150)}px`;
      tooltip.style.top = `${Math.max(6, point.y - 70)}px`;
    };
    hit.addEventListener('mouseenter', show);
    hit.addEventListener('mousemove', show);
    hit.addEventListener('focus', () => show(null));
    hit.addEventListener('mouseleave', () => tooltip.classList.add('hidden'));
    hit.addEventListener('blur', () => tooltip.classList.add('hidden'));
  }
}

function renderTrafficTable(series) {
  const rows = series.slice().reverse().map((day) => (
    `<tr><td>${escapeHtml(day.day)}</td><td>${escapeHtml(bytes(day.up))}</td><td>${escapeHtml(bytes(day.down))}</td><td>${escapeHtml(bytes(day.up + day.down))}</td></tr>`
  )).join('');
  $('traffic-table').innerHTML = `<table class="data-table"><thead><tr><th>${escapeHtml(T('日期'))}</th><th>${escapeHtml(T('上传'))}</th><th>${escapeHtml(T('下载'))}</th><th>${escapeHtml(T('总计'))}</th></tr></thead><tbody>${rows}</tbody></table>`;
}

function renderFolderBars(rows) {
  const host = $('folder-bars');
  if (!rows || !rows.length) {
    host.innerHTML = `<p class="muted small">${escapeHtml(T('暂无数据'))}</p>`;
    return;
  }
  const max = Math.max(1, ...rows.map((row) => row.bytes));
  host.innerHTML = rows.map((row) => {
    const width = Math.max(row.bytes > 0 ? 2 : 0, (row.bytes / max) * 100);
    return `<div class="folder-bar-row">
      <div class="folder-bar-head"><span>${escapeHtml(row.name)}</span><span class="muted small">${escapeHtml(bytes(row.bytes))} · ${escapeHtml(countFiles(row.files))}</span></div>
      <div class="folder-bar-track"><span style="width:${width.toFixed(2)}%"></span></div>
    </div>`;
  }).join('');
}

function renderStats(data) {
  const storage = data.storage || {};
  const traffic = data.traffic || {};
  const totals = traffic.totals || { up: 0, down: 0, up_files: 0, down_files: 0 };
  const today = traffic.today || { up: 0, down: 0 };
  const disk = data.vps_disk || {};

  $('stat-used').textContent = bytes(storage.used_bytes || 0);
  const quota = Number(storage.quota_bytes) || 0;
  if (quota > 0) {
    const percent = Math.min(100, ((storage.used_bytes || 0) / quota) * 100);
    $('stat-meter').parentElement.classList.remove('hidden');
    $('stat-meter').style.width = `${percent.toFixed(1)}%`;
    $('stat-quota').textContent = `${bytes(storage.used_bytes || 0)} / ${bytes(quota)}`;
  } else {
    $('stat-meter').style.width = '0%';
    $('stat-meter').parentElement.classList.add('hidden');
    $('stat-quota').textContent = T('未设置配额');
  }
  $('stat-up').textContent = bytes(totals.up || 0);
  $('stat-up-files').textContent = countTransfers(totals.up_files || 0);
  $('stat-down').textContent = bytes(totals.down || 0);
  $('stat-down-files').textContent = countTransfers(totals.down_files || 0);
  $('stat-disk').textContent = bytes(disk.used || 0);
  $('stat-disk-free').textContent = `${bytes(disk.free || 0)} ${T('可用')} / ${bytes(disk.total || 0)}`;
  $('stats-today').textContent = `${T('今日上传')} ${bytes(today.up || 0)} · ${T('今日下载')} ${bytes(today.down || 0)}`;
  $('stats-updated').textContent = storage.updated
    ? `${T('更新于')} ${when(storage.updated)}` : '';

  renderTrafficChart(traffic.days || []);
  renderTrafficTable(traffic.days || []);
  renderFolderBars(storage.by_folder || []);
}

async function loadStats() {
  if (state.statsBusy) return;
  state.statsBusy = true;
  try {
    let data = await api(`/api/stats?days=${state.statsDays}`);
    if (!state.demo && !state.statsSeeded && !Number((data.storage || {}).updated)) {
      // First visit after a Telegram login that happened before the startup
      // scan: build the storage cache once, then re-read it.
      state.statsSeeded = true;
      try {
        await api('/api/stats/refresh', { method: 'POST' });
        data = await api(`/api/stats?days=${state.statsDays}`);
      } catch (_) { /* the refresh button is still available */ }
    }
    renderStats(data);
  } catch (error) {
    toast(T('加载统计数据失败'), true);
  } finally {
    state.statsBusy = false;
  }
}

async function openStats() {
  state.page = 'stats';
  setActiveNav('stats');
  state.selecting = false;
  state.selection.clear();
  updateBatchBar();
  $('files-page').classList.add('hidden');
  $('stats-page').classList.remove('hidden');
  $('settings-page').classList.add('hidden');
  $('topbar-files').classList.add('hidden');
  $('folder-title').textContent = T('概览');
  closeSidebar();
  await loadStats();
  if (state.statsTimer) clearInterval(state.statsTimer);
  state.statsTimer = setInterval(() => { if (state.page === 'stats') loadStats(); }, 10000);
}

$('btn-nav-files').addEventListener('click', () => {
  closeSidebar();
  openFilesPage();
});
$('btn-stats').addEventListener('click', () => { openStats(); });
$('stats-days').addEventListener('change', (event) => {
  state.statsDays = Number(event.target.value) || 30;
  loadStats();
});
$('stats-refresh').addEventListener('click', async () => {
  const button = $('stats-refresh');
  button.disabled = true;
  try {
    await api('/api/stats/refresh', { method: 'POST' });
    await loadStats();
  } catch (error) {
    toast(error.message, true);
  } finally {
    button.disabled = false;
  }
});


/* ---------- settings page ---------- */

const GROUP_META = {
  general: { icon: 'i-settings', zh: '通用', en: 'General' },
  storage: { icon: 'i-database', zh: '存储', en: 'Storage' },
  transfer: { icon: 'i-upload', zh: '传输', en: 'Transfer' },
  media: { icon: 'i-video', zh: '媒体', en: 'Media' },
  security: { icon: 'i-lock', zh: '安全', en: 'Security' },
};
const BYTE_UNITS = { MB: 1024 ** 2, GB: 1024 ** 3 };

function unitValue(value) {
  const gb = 1024 ** 3;
  if (value >= gb) return [Math.round((value / gb) * 100) / 100, 'GB'];
  return [Math.round((value / 1024 ** 2) * 100) / 100, 'MB'];
}

function settingRow(item) {
  const label = LANG === 'en' ? item.label_en : item.label;
  const hint = LANG === 'en' ? item.hint_en : item.hint;
  let control;
  if (item.kind === 'bytes') {
    const [number, unit] = unitValue(Number(item.value));
    control = `<input type="number" min="0" step="0.01" value="${number}">
      <select class="unit-select"><option ${unit === 'MB' ? 'selected' : ''}>MB</option><option ${unit === 'GB' ? 'selected' : ''}>GB</option></select>`;
  } else if (item.kind === 'int' || item.kind === 'float') {
    control = `<input type="number" step="${item.kind === 'float' ? '0.5' : '1'}" value="${item.value}">`;
  } else {
    control = `<input type="text" value="${escapeHtml(String(item.value))}">`;
  }
  return `<div class="settings-row" data-setting="${item.key}" data-kind="${item.kind}">
    <div class="settings-text">
      <div class="settings-label">${escapeHtml(label)}</div>
      ${hint ? `<div class="settings-hint">${escapeHtml(hint)}</div>` : ''}
    </div>
    <div class="settings-control">${control}</div>
  </div>`;
}

function passwordRow() {
  return `<div class="settings-row stack">
    <div class="settings-text">
      <div class="settings-label">${escapeHtml(T('修改管理密码'))}</div>
      <div class="settings-hint">${escapeHtml(T('至少 10 位；改完当前会话会失效'))}</div>
    </div>
    <form id="password-form" class="password-form">
      <input type="password" id="pw-current" autocomplete="current-password" placeholder="${escapeHtml(T('当前密码'))}">
      <input type="password" id="pw-new" autocomplete="new-password" placeholder="${escapeHtml(T('新密码'))}">
      <input type="password" id="pw-confirm" autocomplete="new-password" placeholder="${escapeHtml(T('确认新密码'))}">
      <button class="ghost" type="submit">${escapeHtml(T('修改密码'))}</button>
    </form>
  </div>`;
}

function formatUptime(seconds) {
  const days = Math.floor(seconds / 86400);
  const hours = Math.floor((seconds % 86400) / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  if (days) return T('{n} 天 {h} 小时', { n: days, h: hours });
  if (hours) return T('{n} 小时 {m} 分钟', { n: hours, m: minutes });
  return T('{n} 分钟', { n: minutes });
}

function buildSettingsHtml(data, dav) {
  const groups = {};
  for (const item of data.items) (groups[item.group] ||= []).push(item);
  const sys = data.system || {};
  const tg = sys.telegram || {};
  const tgReady = tg.state === 'ready';
  const tgName = tg.user ? '@' + (tg.user.username || tg.user.name) : (tgReady ? T('已连接') : T('未连接'));
  const theme = currentTheme();
  const parts = [];

  parts.push(`<p class="settings-intro muted">${escapeHtml(T('这些参数存在服务器上，保存后立即生效。'))}</p>`);

  parts.push(`<section class="settings-group">
    <div class="settings-group-head">${icon('i-sun')}<h4>${escapeHtml(T('外观'))}</h4></div>
    <div class="settings-row">
      <div class="settings-text">
        <div class="settings-label">${escapeHtml(T('主题'))}</div>
        <div class="settings-hint">${escapeHtml(T('跟随系统，或固定浅色 / 深色'))}</div>
      </div>
      <div class="settings-control"><div class="seg" id="theme-seg">
        ${[['system', 'i-desktop'], ['light', 'i-sun'], ['dark', 'i-moon']].map(([mode, name]) =>
          `<button data-theme-mode="${mode}" class="${theme === mode ? 'active' : ''}">${icon(name)}${escapeHtml(T(mode === 'system' ? '跟随系统' : mode === 'light' ? '浅色' : '深色'))}</button>`).join('')}
      </div></div>
    </div>
    <div class="settings-row">
      <div class="settings-text">
        <div class="settings-label">${escapeHtml(T('语言'))}</div>
        <div class="settings-hint">${escapeHtml(T('界面文字，切换后刷新页面'))}</div>
      </div>
      <div class="settings-control">
        <button class="ghost" id="btn-settings-lang">${LANG === 'zh' ? 'English' : '中文'}</button>
      </div>
    </div>
  </section>`);

  for (const [group, items] of Object.entries(groups)) {
    const meta = GROUP_META[group] || { icon: 'i-settings', zh: group, en: group };
    parts.push(`<section class="settings-group">
      <div class="settings-group-head">${icon(meta.icon)}<h4>${escapeHtml(LANG === 'en' ? meta.en : meta.zh)}</h4></div>
      ${items.map(settingRow).join('')}
      ${group === 'security' ? passwordRow() : ''}
    </section>`);
  }

  parts.push(`<section class="settings-group">
    <div class="settings-group-head">${icon('i-link')}<h4>${escapeHtml(T('连接'))}</h4></div>
    <div class="settings-row">
      <div class="settings-text">
        <div class="settings-label">Telegram</div>
        <div class="settings-hint">${escapeHtml(tgName)}</div>
      </div>
      <div class="settings-control">
        <span class="chip ${tgReady ? 'ok' : 'bad'}"><i class="dot"></i>${escapeHtml(tgReady ? T('已连接') : T('未连接'))}</span>
        ${tgReady ? `<button class="ghost" id="btn-settings-tg-logout">${escapeHtml(T('断开'))}</button>` : ''}
      </div>
    </div>
    ${dav ? `<div class="settings-row">
      <div class="settings-text">
        <div class="settings-label">WebDAV</div>
        <div class="settings-hint">${escapeHtml(T('rclone / Cyberduck / RaiDrive 都能用'))}</div>
      </div>
      <div class="settings-control">
        <button class="ghost" id="btn-dav-rotate">${escapeHtml(T('换密码'))}</button>
      </div>
    </div>
    <div class="settings-row stack">
      <div class="copy-row"><input id="dav-url" readonly value="${escapeHtml(location.origin + dav.path)}"><button class="ghost" data-copy="dav-url">${escapeHtml(T('复制'))}</button></div>
      <div class="copy-row"><input id="dav-user" readonly value="${escapeHtml(dav.user)}"><button class="ghost" data-copy="dav-user">${escapeHtml(T('复制'))}</button></div>
      <div class="copy-row"><input id="dav-pass" readonly value="${escapeHtml(dav.password)}"><button class="ghost" data-copy="dav-pass">${escapeHtml(T('复制'))}</button></div>
    </div>` : ''}
  </section>`);

  parts.push(`<section class="settings-group">
    <div class="settings-group-head">${icon('i-server')}<h4>${escapeHtml(T('系统信息'))}</h4></div>
    <div class="system-grid">
      <div class="system-item"><div class="k">${escapeHtml(T('版本'))}</div><div class="v">v${escapeHtml(data.version)}</div></div>
      <div class="system-item"><div class="k">${escapeHtml(T('运行时长'))}</div><div class="v">${escapeHtml(formatUptime(sys.uptime || 0))}</div></div>
      <div class="system-item"><div class="k">${escapeHtml(T('磁盘可用'))}</div><div class="v">${bytes(sys.disk_free || 0)} / ${bytes(sys.disk_total || 0)}</div></div>
      <div class="system-item"><div class="k">${escapeHtml(T('媒体缓存'))}</div><div class="v">${bytes((sys.cache || {}).used || 0)} / ${bytes((sys.cache || {}).capacity || 0)}</div></div>
      <div class="system-item"><div class="k">Python</div><div class="v">${escapeHtml(sys.python || '—')}</div></div>
      <div class="system-item" title="${escapeHtml(sys.data_dir || '')}"><div class="k">${escapeHtml(T('数据目录'))}</div><div class="v">${escapeHtml(sys.data_dir || '—')}</div></div>
    </div>
  </section>`);

  parts.push(`<div class="save-bar"><button class="primary" id="btn-save-settings">${icon('i-check')}${escapeHtml(T('保存设置'))}</button></div>`);
  return parts.join('');
}

function bindSettingsPage() {
  for (const button of document.querySelectorAll('#theme-seg button')) {
    button.addEventListener('click', () => {
      applyTheme(button.dataset.themeMode);
      for (const other of document.querySelectorAll('#theme-seg button')) {
        other.classList.toggle('active', other === button);
      }
    });
  }
  $('btn-settings-lang')?.addEventListener('click', toggleLang);
  $('btn-settings-tg-logout')?.addEventListener('click', async () => {
    if (!confirm(T('断开 Telegram？下次需要重新登录。'))) return;
    try { await api('/api/tg/logout', { method: 'POST' }); location.reload(); }
    catch (error) { toast(error.message, true); }
  });
  $('btn-dav-rotate')?.addEventListener('click', async () => {
    try {
      const result = await api('/api/settings/webdav/rotate', { method: 'POST' });
      $('dav-pass').value = result.password;
      toast(T('WebDAV 密码已更换'));
    } catch (error) { toast(error.message, true); }
  });
  for (const button of document.querySelectorAll('[data-copy]')) {
    button.addEventListener('click', async () => {
      const input = $(button.dataset.copy);
      try {
        await navigator.clipboard.writeText(input.value);
        toast(T('已复制'));
      } catch (_) {
        input.select();
        document.execCommand('copy');
        toast(T('已复制'));
      }
    });
  }
  $('password-form')?.addEventListener('submit', async (event) => {
    event.preventDefault();
    const current = $('pw-current').value;
    const next = $('pw-new').value;
    if (next !== $('pw-confirm').value) { toast(T('两次输入的新密码不一样'), true); return; }
    try {
      await api('/api/settings/password', { method: 'POST', body: { current, new: next } });
      toast(T('密码已修改，请重新登录'));
      setTimeout(() => location.reload(), 900);
    } catch (error) { toast(error.message, true); }
  });
  $('btn-save-settings')?.addEventListener('click', async () => {
    const values = {};
    for (const row of document.querySelectorAll('#settings-body [data-setting]')) {
      const key = row.dataset.setting;
      const kind = row.dataset.kind;
      if (kind === 'bytes') {
        const number = Number(row.querySelector('input').value);
        const unit = row.querySelector('select').value;
        if (!Number.isFinite(number) || number < 0) { toast(T('数值不对'), true); return; }
        values[key] = Math.round(number * BYTE_UNITS[unit]);
      } else if (kind === 'int' || kind === 'float') {
        const number = Number(row.querySelector('input').value);
        if (!Number.isFinite(number)) { toast(T('数值不对'), true); return; }
        values[key] = kind === 'int' ? Math.round(number) : number;
      } else {
        values[key] = row.querySelector('input').value.trim();
      }
    }
    const button = $('btn-save-settings');
    button.disabled = true;
    try {
      await api('/api/settings', { method: 'POST', body: { values } });
      await loadSettings();
      toast(T('已保存'));
    } catch (error) {
      toast(error.message, true);
      const again = $('btn-save-settings');
      if (again) again.disabled = false;
    }
  });
}

async function loadSettings() {
  const [data, dav] = await Promise.all([
    api('/api/settings'),
    api('/api/settings/webdav').catch(() => null),
  ]);
  state.settingsData = data;
  applySiteName(data.site_name);
  $('settings-body').innerHTML = buildSettingsHtml(data, dav);
  bindSettingsPage();
}

async function openSettings() {
  state.page = 'settings';
  setActiveNav('settings');
  state.selecting = false;
  state.selection.clear();
  updateBatchBar();
  $('files-page').classList.add('hidden');
  $('stats-page').classList.add('hidden');
  $('settings-page').classList.remove('hidden');
  $('topbar-files').classList.add('hidden');
  $('folder-title').textContent = T('设置');
  if (state.statsTimer) { clearInterval(state.statsTimer); state.statsTimer = null; }
  closeSidebar();
  try {
    await loadSettings();
  } catch (error) {
    $('settings-body').innerHTML = `<div class="card"><p>${escapeHtml(T('加载失败：{msg}', { msg: error.message }))}</p></div>`;
  }
}

/* ---------- dialog ---------- */

function openDialog(html) {
  $('dialog').innerHTML = html;
  $('dialog-backdrop').classList.remove('hidden');
  for (const button of $('dialog').querySelectorAll('[data-close]')) {
    button.addEventListener('click', closeDialog);
  }
}
function closeDialog() { $('dialog-backdrop').classList.add('hidden'); $('dialog').innerHTML = ''; }
$('dialog-backdrop').addEventListener('click', (event) => {
  if (event.target === $('dialog-backdrop')) closeDialog();
});

/* ---------- search / paging / sidebar ---------- */

let searchTimer = null;
$('search').addEventListener('input', (event) => {
  clearTimeout(searchTimer);
  searchTimer = setTimeout(() => {
    state.query = event.target.value.trim();
    if (state.folderId) loadFiles(true);
  }, 350);
});

$('btn-load-more').addEventListener('click', () => loadFiles(false));

$('btn-menu').addEventListener('click', () => {
  $('sidebar').classList.toggle('open');
  $('sidebar-backdrop').classList.toggle('hidden', !$('sidebar').classList.contains('open'));
});
$('sidebar-backdrop').addEventListener('click', closeSidebar);
function closeSidebar() {
  $('sidebar').classList.remove('open');
  $('sidebar-backdrop').classList.add('hidden');
}

$('btn-settings').addEventListener('click', () => openSettings());

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register(`${BASE}/sw.js`).catch(() => {});
}

boot();
})();
