'use strict';

const MUTATION_HEADERS = { 'X-Requested-With': 'seagulldrive' };
// The app is mounted under a random secret path; derive it from the URL.
const BASE = location.pathname.replace(/\/+$/, '');
const API_TIMEOUT = 30000;

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
};

const $ = (id) => document.getElementById(id);

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
  const d = new Date(ts * 1000);
  const pad = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function toast(message, bad = false) {
  const node = document.createElement('div');
  node.className = 'toast' + (bad ? ' bad' : '');
  node.textContent = message;
  $('toasts').appendChild(node);
  setTimeout(() => node.remove(), 4200);
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
        throw new Error('登录已失效');
      }
      throw new Error((payload && payload.detail) || `请求失败 (${response.status})`);
    }
    return payload;
  } catch (error) {
    if (error.name === 'AbortError') throw new Error('请求超时，检查网络');
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

async function boot() {
  try {
    const session = await api('/api/session');
    if (!session.authenticated) { showLogin(); return; }
    if (session.demo) { await enterApp(); return; }
    const tg = session.telegram || {};
    if (tg.state === 'ready') { await enterApp(); return; }
    show('view-setup');
    applySetupState(tg.state);
    if (tg.error) $('setup-err').textContent = tg.error;
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
  const user = session.telegram && session.telegram.user;
  $('account-line').textContent = user ? `@${user.username || user.name}` : (session.demo ? '演示模式' : '');
  $('btn-tg-logout').classList.toggle('hidden', Boolean(session.demo));
  try {
    await loadFolders();
  } catch (error) {
    // A transient network error must not look like a logout.
    toast(`加载失败：${error.message}`, true);
    $('folder-title').textContent = '加载失败';
    $('empty').classList.remove('hidden');
    $('empty').innerHTML = `<p>加载失败：${escapeHtml(error.message)}</p><button class="ghost" id="retry-folders">重试</button>`;
    $('retry-folders')?.addEventListener('click', () => loadFolders().catch((e) => toast(e.message, true)));
  }
}

/* ---------- login ---------- */

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
  if (!confirm('断开 Telegram？下次需要重新登录。')) return;
  try {
    await api('/api/tg/logout', { method: 'POST' });
    location.reload();
  } catch (error) { toast(error.message, true); }
});

/* ---------- telegram setup ---------- */

function setupError(message) { $('setup-err').textContent = message || ''; }

$('btn-send-code').addEventListener('click', async () => {
  setupError('');
  const button = $('btn-send-code');
  button.disabled = true;
  button.textContent = '发送中…';
  try {
    const result = await api('/api/tg/login/start', {
      method: 'POST',
      body: { api_id: Number($('api-id').value), api_hash: $('api-hash').value.trim(), phone: $('api-phone').value.trim() },
    });
    applySetupState(result.state);
    $('setup-note').textContent = result.code_type === 'SentCodeTypeApp'
      ? '验证码已经发到你的 Telegram 客户端里了。' : '';
  } catch (error) {
    setupError(error.message);
  } finally {
    button.disabled = false;
    button.textContent = '发送验证码';
  }
});

$('btn-submit-code').addEventListener('click', async () => {
  setupError('');
  try {
    const result = await api('/api/tg/login/code', { method: 'POST', body: { code: $('login-code').value.trim() } });
    if (result.state === 'awaiting_password') { applySetupState('awaiting_password'); return; }
    await enterApp();
  } catch (error) { setupError(error.message); }
});

$('btn-submit-2fa').addEventListener('click', async () => {
  setupError('');
  try {
    await api('/api/tg/login/password', { method: 'POST', body: { password: $('login-2fa').value } });
    await enterApp();
  } catch (error) { setupError(error.message); }
});

/* ---------- folders ---------- */

async function loadFolders() {
  const data = await api('/api/folders');
  state.folders = data.folders || [];
  renderFolders();
  if (state.folders.length) {
    const saved = Number(localStorage.getItem('sd_folder') || 0);
    const target = state.folders.find((f) => f.id === saved) || state.folders[0];
    await selectFolder(target.id);
  } else {
    $('folder-title').textContent = '还没有文件夹';
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
    button.innerHTML = `<span class="icon">📁</span><span class="name">${escapeHtml(folder.name)}</span>`;
    button.addEventListener('click', () => { closeSidebar(); selectFolder(folder.id); });
    button.addEventListener('contextmenu', (event) => {
      event.preventDefault();
      if (confirm(`删除文件夹「${folder.name}」？频道里的文件也会一起删掉。`)) {
        api(`/api/folders/${folder.id}`, { method: 'DELETE' })
          .then(() => { toast('已删除'); loadFolders(); })
          .catch((error) => toast(error.message, true));
      }
    });
    nav.appendChild(button);
  }
}

async function selectFolder(folderId) {
  state.folderId = folderId;
  state.selection.clear();
  updateBatchBar();
  localStorage.setItem('sd_folder', String(folderId));
  const folder = state.folders.find((f) => f.id === folderId);
  $('folder-title').textContent = folder ? folder.name : '文件';
  renderFolders();
  closeSidebar();
  await loadFiles(true);
}

$('btn-new-folder').addEventListener('click', () => {
  openDialog(`
    <h3>新建文件夹</h3>
    <p class="muted small">会在 Telegram 里建一个私有频道，名字带 [TD] 标记。</p>
    <input id="new-folder-name" placeholder="比如：电影">
    <div class="dialog-actions"><button class="ghost" data-close>取消</button><button class="primary" id="do-create-folder">创建</button></div>
  `);
  $('do-create-folder').addEventListener('click', async () => {
    const name = $('new-folder-name').value.trim();
    if (!name) return;
    try {
      await api('/api/folders', { method: 'POST', body: { name } });
      closeDialog();
      toast('文件夹建好了');
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
        $('empty').innerHTML = `<p>加载失败：${escapeHtml(error.message)}</p><button class="ghost" id="retry-load">重试</button>`;
        const retry = $('retry-load');
        if (retry) retry.addEventListener('click', () => loadFiles(true));
      }
    }
  } finally {
    if (seq === state.reqSeq) state.loading = false;
  }
}

function fileIcon(kind) {
  return ({ image: '🖼️', video: '🎬', audio: '🎵', pdf: '📕', text: '📄' })[kind] || '📦';
}

function renderFiles() {
  const grid = $('file-grid');
  grid.innerHTML = '';
  const isEmpty = state.files.length === 0;
  $('empty').classList.toggle('hidden', !isEmpty);
  if (isEmpty) {
    $('empty').innerHTML = state.query
      ? `<p>没有匹配「${escapeHtml(state.query)}」的文件。</p>`
      : '<p>这里还是空的。</p><p class="muted">把文件拖进来，或者点右上角「上传」。</p>';
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
    card.innerHTML = `
      <div class="thumb-wrap">${media}${state.selecting ? '<span class="pick"></span>' : ''}</div>
      <div class="file-meta">
        <div class="file-name" title="${escapeHtml(file.name)}">${escapeHtml(file.name)}</div>
        <div class="file-sub">${bytes(file.size)} · ${when(file.date)}</div>
      </div>
      <div class="file-actions">
        <button class="ghost icon" data-act="download" title="下载">⬇</button>
        <button class="ghost icon" data-act="share" title="分享">🔗</button>
        <button class="ghost icon" data-act="more" title="更多">⋯</button>
      </div>`;
    const img = card.querySelector('.thumb-wrap img');
    if (img) img.addEventListener('error', () => {
      const fallback = document.createElement('div');
      fallback.className = 'file-icon';
      fallback.textContent = fileIcon(file.kind);
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
  $('batch-count').textContent = `已选 ${state.selection.size} 项`;
  $('batch-bar').classList.toggle('hidden', !state.selecting);
  $('btn-select').classList.toggle('active', state.selecting);
}

function downloadFile(file) {
  const link = document.createElement('a');
  link.href = `${BASE}/api/folders/${state.folderId}/files/${file.id}/download`;
  link.download = file.name;
  document.body.appendChild(link);
  link.click();
  link.remove();
}

function fileMenu(file) {
  openDialog(`
    <h3>${escapeHtml(file.name)}</h3>
    <p class="muted small">${bytes(file.size)} · ${when(file.date)}</p>
    <div class="menu-list">
      <button class="ghost full" id="m-download">下载</button>
      <button class="ghost full" id="m-rename">重命名</button>
      <button class="ghost full" id="m-move">移动到…</button>
      <button class="ghost full danger" id="m-delete">删除</button>
    </div>
  `);
  $('m-download').addEventListener('click', () => { closeDialog(); downloadFile(file); });
  $('m-rename').addEventListener('click', () => { closeDialog(); renameFile(file); });
  $('m-move').addEventListener('click', () => { closeDialog(); moveFile(file); });
  $('m-delete').addEventListener('click', async () => {
    if (!confirm(`确定删除「${file.name}」？`)) return;
    try {
      await api(`/api/folders/${state.folderId}/files/${file.id}`, { method: 'DELETE' });
      closeDialog();
      toast('已删除');
      await loadFiles(true);
    } catch (error) { toast(error.message, true); }
  });
}

function renameFile(file) {
  openDialog(`
    <h3>重命名</h3>
    <p class="muted small">改的是网盘里显示的名字，立即生效。Telegram 客户端里仍是原始文件名（协议不支持改）。</p>
    <input id="rename-input" value="${escapeHtml(file.name)}">
    <div class="dialog-actions"><button class="ghost" data-close>取消</button><button class="primary" id="do-rename">确定</button></div>
  `);
  const input = $('rename-input');
  input.focus();
  input.select();
  $('do-rename').addEventListener('click', async () => {
    const name = input.value.trim();
    if (!name || name === file.name) { closeDialog(); return; }
    const button = $('do-rename');
    button.disabled = true;
    button.textContent = '处理中…';
    try {
      await api(`/api/folders/${state.folderId}/files/${file.id}/rename`, { method: 'POST', body: { name } });
      closeDialog();
      toast('已重命名');
      await loadFiles(true);
    } catch (error) {
      toast(error.message, true);
      button.disabled = false;
      button.textContent = '确定';
    }
  });
}

function moveFile(file) {
  const others = state.folders.filter((f) => f.id !== state.folderId);
  if (!others.length) { toast('没有别的文件夹', true); return; }
  openDialog(`
    <h3>移动到</h3>
    <div class="menu-list">
      ${others.map((f) => `<button class="ghost full" data-move="${f.id}">📁 ${escapeHtml(f.name)}</button>`).join('')}
    </div>
    <div class="dialog-actions"><button class="ghost" data-close>取消</button></div>
  `);
  for (const button of document.querySelectorAll('[data-move]')) {
    button.addEventListener('click', async () => {
      try {
        await api(`/api/folders/${state.folderId}/files/${file.id}/move`, {
          method: 'POST', body: { to: Number(button.dataset.move) },
        });
        closeDialog();
        toast('已移动');
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
  if (!chosen.length) { toast('先选文件', true); return; }
  chosen.forEach((file, index) => setTimeout(() => downloadFile(file), index * 350));
});

$('batch-delete').addEventListener('click', async () => {
  const chosen = state.files.filter((f) => state.selection.has(f.id));
  if (!chosen.length) { toast('先选文件', true); return; }
  if (!confirm(`删除选中的 ${chosen.length} 个文件？`)) return;
  let failed = 0;
  for (const file of chosen) {
    try { await api(`/api/folders/${state.folderId}/files/${file.id}`, { method: 'DELETE' }); }
    catch (_) { failed += 1; }
  }
  toast(failed ? `完成，${failed} 个失败` : '已删除', failed > 0);
  state.selection.clear();
  await loadFiles(true);
});

$('batch-move').addEventListener('click', () => {
  const chosen = state.files.filter((f) => state.selection.has(f.id));
  if (!chosen.length) { toast('先选文件', true); return; }
  const others = state.folders.filter((f) => f.id !== state.folderId);
  if (!others.length) { toast('没有别的文件夹', true); return; }
  openDialog(`
    <h3>移动 ${chosen.length} 个文件到</h3>
    <div class="menu-list">
      ${others.map((f) => `<button class="ghost full" data-bmove="${f.id}">📁 ${escapeHtml(f.name)}</button>`).join('')}
    </div>
    <div class="dialog-actions"><button class="ghost" data-close>取消</button></div>
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
      toast(failed ? `完成，${failed} 个失败` : '已移动', failed > 0);
      state.selection.clear();
      await loadFiles(true);
    });
  }
});

/* ---------- upload ---------- */

function uploadFiles(fileList) {
  if (!state.folderId) { toast('先选一个文件夹', true); return; }
  if (!fileList.length) return;
  $('upload-panel').classList.remove('hidden');
  for (const file of fileList) startUpload(file);
}

function uploadKey(file) {
  return `sd_up_${state.folderId}_${file.size}_${file.lastModified || 0}_${file.name}`;
}

function makeUploadRow(file) {
  const row = document.createElement('div');
  row.className = 'upload-row';
  row.innerHTML = `
    <div class="upload-name">${escapeHtml(file.name)}</div>
    <div class="bar"><div class="bar-fill"></div></div>
    <div class="upload-foot">
      <span class="upload-status">准备中…</span>
      <span class="upload-actions"></span>
    </div>`;
  $('upload-list').appendChild(row);
  return {
    row,
    fill: row.querySelector('.bar-fill'),
    status: row.querySelector('.upload-status'),
    actions: row.querySelector('.upload-actions'),
  };
}

async function startUpload(file) {
  const { row, fill, status, actions } = makeUploadRow(file);
  const key = uploadKey(file);
  let chunkSize = 8 * 1024 * 1024;
  let sessionId = localStorage.getItem(key);
  let offset = 0;
  let stopped = false;
  let currentController = null;

  function attach(session) {
    sessionId = session.id;
    offset = session.offset;
    if (session.chunk_size) chunkSize = session.chunk_size;
    localStorage.setItem(key, sessionId);
  }

  async function ensureSession() {
    if (sessionId) {
      try {
        const info = await api(`/api/uploads/${sessionId}`);
        if (info.size === file.size) { offset = info.offset; return; }
      } catch (_) { /* stale id */ }
      localStorage.removeItem(key);
      sessionId = null;
    }
    attach(await api('/api/uploads', {
      method: 'POST',
      body: { folder_id: state.folderId, name: file.name, size: file.size },
    }));
  }

  function setActions(paused) {
    actions.innerHTML = '';
    if (paused) {
      const retry = document.createElement('button');
      retry.className = 'ghost icon';
      retry.textContent = '重试';
      retry.addEventListener('click', () => { actions.innerHTML = ''; uploadLoop(); });
      actions.appendChild(retry);
    }
    const cancel = document.createElement('button');
    cancel.className = 'ghost icon';
    cancel.textContent = '取消';
    cancel.addEventListener('click', async () => {
      stopped = true;
      currentController?.abort();
      try { if (sessionId) await api(`/api/uploads/${sessionId}`, { method: 'DELETE' }); } catch (_) { /* ignore */ }
      localStorage.removeItem(key);
      row.remove();
    });
    actions.appendChild(cancel);
  }

  function paint() {
    const percent = Math.round((offset / (file.size || 1)) * 100);
    fill.style.width = `${percent}%`;
    status.textContent = offset >= file.size ? '上传到 Telegram…' : `上传中 ${percent}%`;
  }

  async function uploadLoop() {
    try {
      await ensureSession();
      state.uploads.set(key, true);
      setActions(false);
      paint();
      while (offset < file.size) {
        if (stopped) return;
        const blob = file.slice(offset, Math.min(offset + chunkSize, file.size));
        let attempt = 0;
        for (;;) {
          try {
            if (stopped) return;
            currentController = new AbortController();
            const timer = setTimeout(() => currentController?.abort(), 120000);
            const response = await fetch(`${BASE}/api/uploads/${sessionId}?offset=${offset}`, {
              method: 'PUT',
              headers: { 'X-Requested-With': 'seagulldrive', 'Content-Type': 'application/octet-stream' },
              body: blob,
              signal: currentController.signal,
            });
            clearTimeout(timer);
            if (response.status === 401) { showLogin(); throw new Error('登录已失效'); }
            if (response.status === 409) {
              const data = await response.json();
              offset = data.offset;
              break;
            }
            if (!response.ok) {
              const data = await response.json().catch(() => ({}));
              throw new Error(data.detail || `分片上传失败 (${response.status})`);
            }
            const data = await response.json();
            offset = data.offset;
            break;
          } catch (error) {
            if (stopped) return;
            attempt += 1;
            if (attempt > 3) throw error;
            await new Promise((resolve) => setTimeout(resolve, 600 * attempt));
          }
        }
        paint();
      }
      const job = await api(`/api/uploads/${sessionId}/complete`, { method: 'POST' });
      localStorage.removeItem(key);
      state.uploads.delete(key);
      pollJob(job.id, fill, status, row);
    } catch (error) {
      state.uploads.delete(key);
      status.textContent = `${error.message}（可重试）`;
      row.classList.add('bad');
      setActions(true);
    }
  }

  setActions(false);
  await uploadLoop();
}

async function pollJob(jobId, fill, status, row) {
  try {
    const job = await api(`/api/jobs/${jobId}`);
    if (job.state === 'running' || job.state === 'queued') {
      const percent = job.total ? Math.round((job.sent / job.total) * 100) : 0;
      fill.style.width = `${percent}%`;
      status.textContent = job.state === 'queued' ? '排队中…' : `上传到 Telegram ${percent}%`;
      setTimeout(() => pollJob(jobId, fill, status, row), 900);
      return;
    }
    if (job.state === 'done') {
      fill.style.width = '100%';
      status.textContent = '完成';
      row.classList.add('done');
      await loadFiles(true);
    } else {
      status.textContent = job.error || '失败';
      row.classList.add('bad');
      const retry = document.createElement('button');
      retry.className = 'ghost icon';
      retry.textContent = '重试';
      retry.addEventListener('click', async () => {
        try {
          await api(`/api/jobs/${jobId}/retry`, { method: 'POST' });
          row.classList.remove('bad');
          retry.remove();
          pollJob(jobId, fill, status, row);
        } catch (error) { toast(error.message, true); }
      });
      row.querySelector('.upload-actions').appendChild(retry);
    }
  } catch (error) {
    status.textContent = error.message;
    row.classList.add('bad');
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
  if (!state.folderId) return;
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
  if (state.folderId && event.dataTransfer.files.length) uploadFiles([...event.dataTransfer.files]);
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
    body.innerHTML = `<video src="${streamUrl(file)}" controls preload="metadata" playsinline${poster}></video>`;
  } else if (file.kind === 'audio') {
    body.innerHTML = `<audio src="${streamUrl(file)}" controls preload="metadata"></audio>`;
  } else if (file.kind === 'pdf') {
    body.innerHTML = `<iframe src="${streamUrl(file)}"></iframe>`;
  } else if (file.kind === 'text') {
    body.innerHTML = '<pre>加载中…</pre>';
    fetch(streamUrl(file), { headers: { 'Range': 'bytes=0-200000' } })
      .then((r) => r.text())
      .then((text) => {
        if (seq !== previewSeq) return;  // a newer preview owns the modal
        body.innerHTML = `<pre>${escapeHtml(text)}</pre>`;
      })
      .catch(() => { if (seq === previewSeq) body.innerHTML = '<pre>读取失败</pre>'; });
  } else {
    body.innerHTML = `<div class="preview-none"><div>${fileIcon(file.kind)}</div><p>这个类型不能在线预览</p></div>`;
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
    <h3>分享「${escapeHtml(file.name)}」</h3>
    <label>有效期（小时，0 = 永久）<input id="share-hours" type="number" min="0" value="24"></label>
    <label>访问密码（可留空）<input id="share-password" type="password" placeholder="可选"></label>
    <div class="dialog-actions"><button class="ghost" data-close>取消</button><button class="primary" id="do-share">生成链接</button></div>
    <div id="share-result"></div>
  `);
  $('do-share').addEventListener('click', async () => {
    const button = $('do-share');
    if (button.disabled) return;
    button.disabled = true;
    button.textContent = '生成中…';
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
      $('share-result').innerHTML = `<p class="muted small">链接</p><input id="share-url" readonly value="${escapeHtml(url)}">`;
      $('share-url').select();
      navigator.clipboard?.writeText(url)
        .then(() => toast('链接已复制'))
        .catch(() => toast('复制失败，请手动选中复制', true));
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = '生成链接';
    }
  });
}

$('btn-share-folder').addEventListener('click', () => {
  if (!state.folderId) { toast('先选一个文件夹', true); return; }
  const folder = state.folders.find((f) => f.id === state.folderId);
  openDialog(`
    <h3>分享文件夹「${escapeHtml(folder ? folder.name : '')}」</h3>
    <p class="muted small">访客能看到这个文件夹里的文件列表并逐个下载。</p>
    <label>有效期（小时，0 = 永久）<input id="folder-share-hours" type="number" min="0" value="24"></label>
    <label>访问密码（可留空）<input id="folder-share-password" type="password" placeholder="可选"></label>
    <div class="dialog-actions"><button class="ghost" data-close>取消</button><button class="primary" id="do-share-folder">生成链接</button></div>
    <div id="folder-share-result"></div>
  `);
  $('do-share-folder').addEventListener('click', async () => {
    const button = $('do-share-folder');
    button.disabled = true;
    button.textContent = '生成中…';
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
      $('folder-share-result').innerHTML = `<p class="muted small">链接</p><input id="folder-share-url" readonly value="${escapeHtml(url)}">`;
      $('folder-share-url').select();
      navigator.clipboard?.writeText(url).then(() => toast('链接已复制')).catch(() => {});
    } catch (error) {
      toast(error.message, true);
    } finally {
      button.disabled = false;
      button.textContent = '生成链接';
    }
  });
});

$('btn-shares').addEventListener('click', async () => {
  try {
    const data = await api('/api/shares');
    const rows = (data.shares || []).map((share) => {
      const link = `${location.origin}/s/${share.token}`;
      const expiry = share.expires_at ? new Date(share.expires_at * 1000).toLocaleString() : '永久';
      const kind = share.message_id ? '文件' : '文件夹';
      return `<li class="share-row">
        <div><b>${kind}</b> · ${escapeHtml(expiry)} · 下载 ${share.downloads} 次</div>
        <div class="share-actions">
          <button class="ghost icon" data-copy="${escapeHtml(link)}">复制</button>
          <button class="ghost icon danger" data-revoke="${escapeHtml(share.token)}">撤销</button>
        </div></li>`;
    }).join('');
    openDialog(`<h3>我的分享</h3><ul class="share-list">${rows || '<li class="muted">还没有分享链接</li>'}</ul>
      <div class="dialog-actions"><button class="ghost" data-close>关闭</button></div>`);
    for (const button of document.querySelectorAll('[data-copy]')) {
      button.addEventListener('click', () => {
        navigator.clipboard?.writeText(button.dataset.copy).catch(() => {});
        toast('已复制');
      });
    }
    for (const button of document.querySelectorAll('[data-revoke]')) {
      button.addEventListener('click', async () => {
        try {
          await api(`/api/share/${button.dataset.revoke}`, { method: 'DELETE' });
          button.closest('li').remove();
          toast('已撤销');
        } catch (error) { toast(error.message, true); }
      });
    }
  } catch (error) { toast(error.message, true); }
});

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

$('btn-settings').addEventListener('click', async () => {
  try {
    const info = await api('/api/settings/webdav');
    openDialog(`
      <h3>WebDAV 挂载</h3>
      <p class="muted small">rclone / Cyberduck / RaiDrive 都能用。Windows 资源管理器只认 443，这个端口它挂不上。</p>
      <label>地址 <input id="dav-url" readonly value="${escapeHtml(location.origin + info.path)}"></label>
      <label>用户名 <input id="dav-user" readonly value="${escapeHtml(info.user)}"></label>
      <label>密码 <input id="dav-pass" readonly value="${escapeHtml(info.password)}"></label>
      <div class="dialog-actions"><button class="ghost" id="dav-rotate">换密码</button><button class="primary" data-close>知道了</button></div>
    `);
    $('dav-rotate').addEventListener('click', async () => {
      const result = await api('/api/settings/webdav/rotate', { method: 'POST' });
      $('dav-pass').value = result.password;
      toast('WebDAV 密码已更换');
    });
  } catch (error) { toast(error.message, true); }
});

if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register(`${BASE}/sw.js`).catch(() => {});
}

boot();
