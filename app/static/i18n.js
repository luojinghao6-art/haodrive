'use strict';

/* Zero-build i18n. Chinese source text doubles as the key, so the markup can
   stay readable and any missing translation falls back to Chinese instead of
   showing an empty label. */

const LANG_KEY = 'haodrive_lang';

const LANG = (() => {
  try {
    const saved = localStorage.getItem(LANG_KEY);
    if (saved === 'zh' || saved === 'en') return saved;
  } catch (_) { /* private mode */ }
  return (navigator.language || 'zh').toLowerCase().startsWith('zh') ? 'zh' : 'en';
})();

const EN = {
  /* ---- auth ---- */
  '管理密码': 'Admin password',
  '进入': 'Sign in',
  'Telegram 频道当硬盘，自己部署自己用': 'Your own cloud drive, backed by Telegram channels',
  '连接 Telegram': 'Connect Telegram',
  '到 my.telegram.org → API development tools 建一个应用，拿 api_id / api_hash。手机号要带国家码，比如 +8613800000000。':
    'Create an app at <a href="https://my.telegram.org" target="_blank" rel="noopener">my.telegram.org</a> → API development tools and copy its api_id / api_hash. Include the country code in the phone number, e.g. +8613800000000.',
  '手机号': 'Phone number',
  '发送验证码': 'Send code',
  '验证码': 'Code',
  '提交': 'Submit',
  '两步验证密码': 'Two-step password',
  '发送中…': 'Sending…',
  '验证码已经发到你的 Telegram 客户端里了。': 'The code was sent to your Telegram app.',

  /* ---- shell ---- */
  '新建文件夹': 'New folder',
  '我的分享': 'My shares',
  'WebDAV 挂载': 'WebDAV',
  '断开 Telegram': 'Disconnect Telegram',
  '退出登录': 'Sign out',
  '概览': 'Overview',
  '文件': 'Files',
  '分享文件夹': 'Share folder',
  '多选': 'Select',
  '搜索文件名': 'Search files',
  '上传': 'Upload',
  '菜单': 'Menu',
  '语言': 'Language',
  '演示模式': 'Demo mode',
  '加载失败': 'Load failed',
  '加载失败：{msg}': 'Failed to load: {msg}',
  '重试': 'Retry',
  '还没有文件夹': 'No folders yet',
  '先选一个文件夹': 'Pick a folder first',
  '松开上传到当前文件夹': 'Drop to upload to this folder',

  /* ---- file list ---- */
  '这里还是空的。': 'Nothing here yet.',
  '把文件拖进来，或者点右上角「上传」。': 'Drag files here, or click Upload.',
  '没有匹配「{q}」的文件。': 'No files match "{q}".',
  '加载更多': 'Load more',
  '已选 {n} 项': '{n} selected',
  '下载': 'Download',
  '移动': 'Move',
  '删除': 'Delete',
  '取消': 'Cancel',
  '关闭': 'Close',
  '确定': 'OK',
  '创建': 'Create',
  '处理中…': 'Working…',
  '生成中…': 'Creating…',
  '准备中…': 'Preparing…',
  '完成': 'Done',
  '失败': 'Failed',
  '分片': 'parts',
  '分享': 'Share',
  '更多': 'More',
  '重命名': 'Rename',
  '移动到…': 'Move to…',
  '移动到': 'Move to',
  '复制': 'Copy',
  '撤销': 'Revoke',
  '先选文件': 'Select files first',
  '删除文件夹「{name}」？频道里的文件也会一起删掉。': 'Delete folder "{name}"? Files in its channel will be deleted too.',
  '会在 Telegram 里建一个私有频道，名字带 [TD] 标记。': 'Creates a private Telegram channel marked [TD].',
  '比如：电影': 'e.g. Movies',
  '文件夹建好了': 'Folder created',
  '已删除': 'Deleted',
  '已移动': 'Moved',
  '已重命名': 'Renamed',
  '没有别的文件夹': 'No other folders',
  '确定删除「{name}」？': 'Delete "{name}"?',
  '改的是网盘里显示的名字，立即生效。Telegram 客户端里仍是原始文件名（协议不支持改）。':
    'Changes the name shown in this app. Telegram keeps the original filename — the protocol has no rename.',
  '删除选中的 {n} 个文件？': 'Delete {n} selected files?',
  '完成，{n} 个失败': 'Done — {n} failed',
  '移动 {n} 个文件到': 'Move {n} files to',
  '这个类型不能在线预览': 'This file type cannot be previewed',
  '读取失败': 'Read failed',
  '加载中…': 'Loading…',

  /* ---- shares ---- */
  '分享「{name}」': 'Share "{name}"',
  '有效期（小时，0 = 永久）': 'Expires (hours, 0 = never)',
  '访问密码（可留空）': 'Access password (optional)',
  '生成链接': 'Create link',
  '链接': 'Link',
  '链接已复制': 'Link copied',
  '复制失败，请手动选中复制': 'Copy failed — select the text and copy manually',
  '分享文件夹「{name}」': 'Share folder "{name}"',
  '访客能看到这个文件夹里的文件列表并逐个下载。': 'Visitors can list and download the files in this folder.',
  '永久': 'Never expires',
  '文件夹': 'Folder',
  '下载 {n} 次': '{n} downloads',
  '已复制': 'Copied',
  '已撤销': 'Revoked',
  '还没有分享链接': 'No share links yet',

  /* ---- webdav ---- */
  'rclone / Cyberduck / RaiDrive 都能用。Windows 资源管理器只认 443，这个端口它挂不上。':
    'rclone / Cyberduck / RaiDrive all work. Windows Explorer only mounts on port 443, so it cannot use this address.',
  '地址': 'URL',
  '用户名': 'Username',
  '密码': 'Password',
  '换密码': 'Rotate password',
  '知道了': 'Got it',
  'WebDAV 密码已更换': 'WebDAV password rotated',
  '断开 Telegram？下次需要重新登录。': 'Disconnect Telegram? You will need to sign in again.',

  /* ---- transfers ---- */
  '传输队列': 'Transfers',
  '上传队列': 'Transfers',
  '上传中 {p}%': 'Uploading {p}%',
  '上传到 Telegram…': 'Sending to Telegram…',
  '上传到 Telegram {p}%': 'Sending to Telegram {p}%',
  '排队中…': 'Queued…',
  '第 {i}/{n} 片': 'Part {i}/{n}',
  '{done} / {total} 片': '{done} / {total} parts',
  '（可重试）': '(retry available)',
  '下载中 {p}%': 'Downloading {p}%',
  '正在保存…': 'Saving…',
  '已保存': 'Saved',
  '合并中…': 'Merging…',
  '下载失败': 'Download failed',
  '已取消': 'Cancelled',
  '浏览器不支持边下边合并，已切换直连下载': 'This browser cannot merge parts — switched to direct download',

  /* ---- stats ---- */
  '流量统计': 'Traffic',
  '上传/下载流量': 'Upload / download traffic',
  '近 7 天': 'Last 7 days',
  '近 30 天': 'Last 30 days',
  '近 90 天': 'Last 90 days',
  '已用空间': 'Used storage',
  '累计上传': 'Total uploaded',
  '累计下载': 'Total downloaded',
  '今日上传': 'Uploaded today',
  '今日下载': 'Downloaded today',
  '总容量': 'Quota',
  '未设置配额': 'No quota set',
  '文件数': 'Files',
  '文件夹数': 'Folders',
  '服务器磁盘': 'Server disk',
  '可用': 'free',
  '已用': 'used',
  '文件夹占用': 'Storage by folder',
  '暂无数据': 'No data yet',
  '暂无流量数据': 'No traffic yet',
  '刷新': 'Refresh',
  '更新于': 'Updated',
  '数据表': 'Data table',
  '日期': 'Date',
  '总计': 'Total',
  '加载统计数据失败': 'Failed to load stats',
  '正在加载…': 'Loading…',
  '{n} 个文件': '{n} files',
  '{n} 次': '{n} transfers',
  '取消上传？': 'Cancel this upload?',
  '文件条目': 'File',

  /* ---- api / backend errors ---- */
  '请求失败 ({status})': 'Request failed ({status})',
  '请求超时，检查网络': 'Request timed out — check your network',
  '登录已失效': 'Session expired',
  '密码错误': 'Wrong password',
  '密码格式不对': 'Invalid password format',
  '尝试次数太多，等五分钟再试': 'Too many attempts — try again in five minutes',
  '请求体过大': 'Request body too large',
  '目标文件夹相同': 'Source and destination are the same',
  '文件名不能为空': 'Filename cannot be empty',
  '一次最多上传 20 个文件': 'At most 20 files per upload',
  '服务器磁盘空间不足': 'Server disk is full',
  '服务器临时空间不足（2GB 上限），等上一片发完再试':
    'Server temp space is full (2GB cap) — wait for the current part to finish',
  '同时进行的上传太多，先完成或取消一些': 'Too many uploads in progress — finish or cancel some',
  'size 不合法': 'Invalid size',
  '上传会话不存在': 'Upload session not found',
  '超过声明的文件大小': 'Larger than the declared size',
  '服务器临时空间不足，等上一个上传完成再试':
    'Server temp space is full — wait for the current upload to finish',
  '服务器临时空间不足，请等上一个上传完成':
    'Server temp space is full — wait for the current upload to finish',
  'Telegram 还没登录': 'Telegram is not signed in',
  'api_id / api_hash / 手机号都要填': 'api_id, api_hash and phone number are all required',
  '会话未授权': 'Session not authorized',
  '先获取验证码': 'Request a code first',
  '文件夹名字不能为空': 'Folder name cannot be empty',
  '频道不存在或已退出': 'Channel not found or already left',
  '文件还没上传完': 'File is not fully uploaded yet',
  '文件不存在': 'File not found',
  '文件已删除': 'File deleted',
  '链接不存在': 'Link not found',
  '链接已过期': 'Link expired',
  '需要密码': 'Password required',
  '文件夹不存在': 'Folder not found',
  '任务不存在': 'Job not found',
  '这个任务不能重试': 'This job cannot be retried',
  '临时文件已删除，请重新上传': 'Temp file was removed — upload again',
  '任务状态已变化': 'Job state changed',
  '文件夹分享没有直链': 'Folder shares have no direct link',
  '尝试次数太多，稍后再试': 'Too many attempts — try again later',
  '上传分组不存在': 'Upload group not found',
  '这一片正在发往 Telegram，等它完成': 'This part is being sent to Telegram — wait for it to finish',
  'group_key 不合法': 'Invalid group key',
  '分片数量和文件大小对不上': 'Part count does not match the file size',
  'part_index 越界': 'Part index out of range',
  '上传分组参数不一致': 'Upload group parameters changed',
  '超过 2GB 临时空间上限，请使用网页端分片上传':
    'Over the 2GB temp cap — use the browser part upload',
  '超过服务器 2GB 临时空间，请用网页端分片上传':
    'Over the server 2GB temp cap — use the browser part upload',
  '上传内容为空': 'Empty upload',
  '文件超过 {n}GB 上限': 'File exceeds the {n}GB limit',
  '还差 {n} 字节': '{n} bytes still missing',
  'offset mismatch': 'Offset mismatch',
  'unauthorized': 'Unauthorized',
  'missing mutation header': 'Missing mutation header',

  /* ---- telegram errors ---- */
  'Telegram 限流了，等一会儿再试（FLOOD_WAIT）':
    'Telegram rate limit — try again shortly (FLOOD_WAIT)',
  'Telegram 限流，等待 {n} 秒': 'Telegram rate limit — wait {n} seconds',
  'api_id / api_hash 无效': 'Invalid api_id / api_hash',
  '这个 api_id 是公开过的，去 my.telegram.org 新建一个':
    'This api_id is public — create a new app at my.telegram.org',
  '手机号格式不对，要带国际区号': 'Invalid phone number — include the country code',
  '手机号被 Telegram 封禁': 'This phone number is banned by Telegram',
  '验证码错误': 'Wrong code',
  '验证码过期了，重新获取': 'Code expired — request a new one',
  '两步验证密码错误': 'Wrong two-step password',
  '需要两步验证密码': 'Two-step password required',
  '频道不可访问（可能被踢了或者已删除）': 'Channel inaccessible (removed or deleted)',
  '频道不存在或没有权限': 'Channel not found or no permission',
  '没有发消息权限，你是不是频道管理员？': 'No permission to post — are you an admin?',
  '需要频道管理员权限': 'Channel admin permission required',
  '文件超过 Telegram 上限（2GB）': "File exceeds Telegram's 2GB limit",
  '账号不在这个频道里': 'Account is not in this channel',
  '会话已失效，请重新登录': 'Session expired — sign in again',
  '会话在别处被使用了，需要重新登录': 'Session used elsewhere — sign in again',
  'demo 模式不支持登录 Telegram': 'Demo mode cannot sign in to Telegram',
  'demo 模式不能退出': 'Demo mode cannot sign out',
  'demo 模式不能删除文件夹': 'Demo mode cannot delete folders',
  'demo 模式只有一个文件夹': 'Demo mode has only one folder',
  '转发失败，源文件已保留': 'Forward failed — source kept',
  '复制失败': 'Copy failed',
  '任务被中断': 'Job interrupted',
  '服务重启，临时文件已清理，请重新上传':
    'Server restarted and temp files were cleaned — upload again',
};

const _missing = new Set();

function T(text, vars) {
  if (text === undefined || text === null) return '';
  let out = text;
  if (LANG === 'en') {
    if (Object.prototype.hasOwnProperty.call(EN, text)) out = EN[text];
    else if (/[一-鿿]/.test(String(text)) && !_missing.has(text)) {
      _missing.add(text);
      console.warn('[i18n] missing English text:', text);
    }
  }
  if (vars) {
    out = String(out).replace(/\{(\w+)\}/g, (match, key) =>
      Object.prototype.hasOwnProperty.call(vars, key) ? String(vars[key]) : match);
  }
  return out;
}

function applyI18n(root = document) {
  if (LANG === 'en') {
    for (const node of root.querySelectorAll('[data-i18n]')) {
      const value = T(node.dataset.i18n);
      if (value) node.textContent = value;
    }
    for (const node of root.querySelectorAll('[data-i18n-html]')) {
      const value = EN[node.dataset.i18nHtml];
      if (value) node.innerHTML = value;
    }
    for (const node of root.querySelectorAll('[data-i18n-placeholder]')) {
      node.placeholder = T(node.dataset.i18nPlaceholder);
    }
    for (const node of root.querySelectorAll('[data-i18n-title]')) {
      node.title = T(node.dataset.i18nTitle);
    }
    for (const node of root.querySelectorAll('[data-i18n-aria]')) {
      node.setAttribute('aria-label', T(node.dataset.i18nAria));
    }
  }
  document.documentElement.lang = LANG === 'zh' ? 'zh-CN' : 'en';
}

function setLang(lang) {
  try { localStorage.setItem(LANG_KEY, lang); } catch (_) { /* ignore */ }
  try { document.cookie = `hd_lang=${lang};path=/;max-age=31536000;samesite=lax`; } catch (_) { /* ignore */ }
  location.reload();
}

function toggleLang() { setLang(LANG === 'zh' ? 'en' : 'zh'); }

window.HaoI18n = { LANG, T, EN, applyI18n, setLang, toggleLang };
