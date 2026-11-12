const $ = (id) => document.getElementById(id);
const state = {
  doc: null, goals: [],
  coverage: null,
  editing: false, dirty: false, timer: null, lastSaveAt: 0, baseUpdated: '',
  shortcutTimer: null,   // Markdown 快捷成型的防抖：等输入稳定再转换，避免打散 `- [ ] `
  collapsed: new Set(), drag: null, dragCopy: false,
  anchor: null,           // 正文里最后选中的文字 / 光标所在段，作为插入与替换的目标
  inbox: [],              // 文档主动提问的收件箱（打开应用时问题已经在等你）
  web: false,             // 联网搜索开关：这次对话要不要先上网查（只发问题关键词，不上传文档）
};

function esc(value) {
  return String(value == null ? '' : value)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

const undoStack = [];

function saveUndo() {
  try { sessionStorage.setItem('mirror-undo', JSON.stringify(undoStack.slice(-50))); } catch (err) { /* ignore */ }
}

function loadUndo() {
  try {
    const raw = sessionStorage.getItem('mirror-undo');
    const list = raw ? JSON.parse(raw) : [];
    if (Array.isArray(list)) undoStack.push(...list.slice(-50));
  } catch (err) { /* ignore */ }
}

function pushUndo(op) {
  undoStack.push(op);
  if (undoStack.length > 50) undoStack.shift();
  saveUndo();
}

async function undoLast() {
  const op = undoStack.pop();
  if (!op) { notice('没有可撤销的操作了。'); return; }
  saveUndo();
  try {
    if (op.type === 'delete-doc' || op.type === 'delete-dir') {
      await api('/trash/restore', {
        method: 'POST', body: JSON.stringify({ name: op.trashName, to: op.to }),
      });
    } else if (op.type === 'move-doc') {
      await api('/docs/' + encodeURIComponent(op.id) + '/move', {
        method: 'POST', body: JSON.stringify({ dir: op.from }),
      });
    } else if (op.type === 'move-dir') {
      await api('/folders/move', {
        method: 'POST', body: JSON.stringify({ from: op.to, to: op.from || 'records' }),
      });
    } else if (op.type === 'rename-doc') {
      await api('/docs/' + encodeURIComponent(op.id) + '/rename', {
        method: 'POST', body: JSON.stringify({ title: op.oldTitle, dir: op.dir }),
      });
    } else if (op.type === 'rename-dir') {
      await api('/folders/rename', {
        method: 'POST', body: JSON.stringify({ path: op.path, name: op.oldName }),
      });
    } else if (op.type === 'create-doc') {
      await api('/docs/' + encodeURIComponent(op.id) + '/delete', { method: 'POST' });
    } else if (op.type === 'create-dir') {
      await api('/folders/delete', {
        method: 'POST', body: JSON.stringify({ path: op.path }),
      });
    } else if (op.type === 'edit') {
      await api('/docs/' + encodeURIComponent(op.id) + '/revisions/' +
        encodeURIComponent(op.rev) + '/restore', { method: 'POST' });
    }
    await loadTree();
    await loadGoals();
    await loadTodoBadge();
    if (op.type === 'delete-doc') await openDoc(op.id);
    else if (state.doc) await openDoc(state.doc.id);
    notice('已撤销：' + (op.label || '上一步'));
  } catch (err) {
    notice(failureText(err.code, err.message));
  }
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, { headers: { 'Content-Type': 'application/json' }, ...options });
  } catch (err) {
    throw { code: 'NETWORK', message: '服务没有响应' };
  }
  const body = await response.json();
  if (!body.ok) throw { code: body.error.code, message: body.error.message };
  return body.data;
}

function notice(text, action) {
  const el = $('notice');
  el.textContent = '';
  if (text) el.appendChild(document.createTextNode(text));
  el.querySelectorAll('.notice-act').forEach((n) => n.remove());
  const actions = Array.isArray(action) ? action : (action ? [action] : []);
  for (const act of actions) {
    if (!act || !act.label || !act.onClick) continue;
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'notice-act';
    btn.textContent = act.label;
    btn.onclick = act.onClick;
    el.appendChild(btn);
  }
  el.classList.toggle('hidden', !text);
}

function setSaveState(text) { $('save-state').textContent = text || ''; }

/* 轻量 toast：操作反馈三秒内可见，自动消失，不占正文位置 */
let toastTimer = null;
function toast(text) {
  const el = $('toast');
  if (!el) { notice(text); return; }
  el.textContent = text || '';
  el.classList.toggle('hidden', !text);
  clearTimeout(toastTimer);
  if (text) toastTimer = setTimeout(() => el.classList.add('hidden'), 2600);
}

function failureText(code, message) {
  if (code === 'LLM_UNAVAILABLE') return '这次没能连上模型，你的记录没有丢。可以稍后重试。';
  if (code === 'EXTERNAL_FIELD_MISSING') return '外部素材缺少来源 / 立场 / 适用前提，没有保存。';
  if (code === 'CONFLICT') return message;
  if (code === 'NETWORK') return '服务没有响应，请确认本地服务还在运行。';
  return message || '操作没有完成，请重试。';
}

function inline(text) {
  let s = esc(text);
  s = s.replace(/!\[([^\]]*)\]\(([^)]+)\)/g, (m, alt, src) => {
    if (/^(https?:)?\/\//i.test(src)) return esc(alt);
    return `<img src="${esc(src)}" alt="${esc(alt)}">`;
  });
  s = s.replace(/\[([^\]]+)\]\(([^)]+)\)/g, (m, label, href) => {
    if (!/^(https?:\/\/|\.{0,2}\/|#)/i.test(href)) return esc(label);
    return `<a href="${esc(href)}" target="_blank" rel="noreferrer">${esc(label)}</a>`;
  });
  s = s.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/`(.+?)`/g, '<code>$1</code>');
  s = s.replace(/\[\[(.+?)\]\]/g, '<span class="wikilink">$1</span>');
  // 四象限依据标签：在正文里也显示成小徽标（inlineMd 会原样写回 #重要 / #紧急）
  s = s.replace(/#(重要|紧急)/g,
    (match, tag) => `<span class="todo-tag ${tag === '重要' ? 'imp' : 'urg'}">#${tag}</span>`);
  return s;
}

const TODO_LINE = /^(\s*(?:[-*]|\d+\.)\s+)\[([ xX])\]\s*(.*)$/;
const LIST_LINE = /^(\s*)([-*]|\d+\.)\s+(.*)$/;
const SPECIAL = /^(#{1,4}\s|\s*[-*]\s|\s*\d+\.\s|>\s?|\s*\||\s*```|\s*!\[)/;

function renderList(rows, startLine) {
  let html = `<ul data-line="${startLine}">`, level = 0;
  rows.forEach((row) => {
    const depth = Math.floor(row.indent / 2);
    if (depth > level) { html += '<ul>'.repeat(depth - level); level = depth; }
    else if (depth < level) { html += '</ul>'.repeat(level - depth); level = depth; }
    html += `<li>${inline(row.text)}</li>`;
  });
  return html + '</ul>'.repeat(level + 1);
}

function renderMarkdown(md) {
  const lines = String(md || '').split('\n');
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    const start = i;
    if (/^\s*```/.test(line)) {
      const fence = line.match(/^\s*```(\S*)/);
      const lang = (fence[1] || '').toLowerCase();
      const code = [];
      i += 1;
      while (i < lines.length && !/^\s*```/.test(lines[i])) { code.push(lines[i]); i += 1; }
      i += 1;
      out.push(`<pre class="md-code" data-line="${start}"><code${lang ? ` class="language-${esc(lang)}"` : ''}>${esc(code.join('\n'))}</code></pre>`);
      continue;
    }
    const todo = line.match(TODO_LINE);
    if (todo) {
      const checked = todo[2].toLowerCase() === 'x';
      out.push(`<div class="todo" data-line="${start}"><input type="checkbox" data-line="${start}" ${checked ? 'checked' : ''}>`
        + `<span class="${checked ? 'done' : ''}">${inline(todo[3])}</span></div>`);
      i += 1;
      continue;
    }
    if (/^\s*\|/.test(line)) {
      const rows = [];
      while (i < lines.length && /^\s*\|/.test(lines[i])) { rows.push(lines[i]); i += 1; }
      const cells = rows.map((r) => r.trim().replace(/^\||\|$/g, '').split('|').map((c) => c.trim()))
        .filter((r) => !r.every((c) => /^:?-+:?$/.test(c)));
      let table = '<table>';
      cells.forEach((row, n) => {
        const tag = n === 0 ? 'th' : 'td';
        table += '<tr>' + row.map((c) => `<${tag}>${inline(c)}</${tag}>`).join('') + '</tr>';
      });
      out.push(table + '</table>');
      continue;
    }
    const heading = line.match(/^(#{1,4})\s+(.*)$/);
    if (heading) {
      const level = Math.min(heading[1].length, 4);
      out.push(`<h${level} data-line="${start}">${inline(heading[2])}</h${level}>`);
      i += 1;
      continue;
    }
    if (/^>\s?/.test(line)) {
      const quote = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) {
        quote.push(lines[i].replace(/^>\s?/, '')); i += 1;
      }
      const paras = [];
      let cur = [];
      quote.forEach((q) => {
        if (!q.trim()) { if (cur.length) { paras.push(cur.join(' ')); cur = []; } }
        else cur.push(q.trim());
      });
      if (cur.length) paras.push(cur.join(' '));
      out.push(`<blockquote data-line="${start}">${paras.map((p) => `<p>${inline(p)}</p>`).join('')}</blockquote>`);
      continue;
    }
    if (LIST_LINE.test(line)) {
      const rows = [];
      while (i < lines.length && LIST_LINE.test(lines[i])) {
        const m = lines[i].match(LIST_LINE);
        rows.push({ indent: m[1].length, text: m[3] });
        i += 1;
      }
      out.push(renderList(rows, start));
      continue;
    }
    if (!line.trim()) { i += 1; continue; }
    const para = [];
    while (i < lines.length && lines[i].trim() && !SPECIAL.test(lines[i]) && !TODO_LINE.test(lines[i])) {
      para.push(lines[i].trim()); i += 1;
    }
    out.push(`<p data-line="${start}">${inline(para.join(' '))}</p>`);
  }
  return out.join('') || '<p class="empty">这篇文档还是空的，点一下就能开始写。</p>';
}

/* ---------- 所见即所得：直接在渲染后的文档上改，再序列化回 Markdown ---------- */

/* ---------- 代码高亮（highlight.js 本地 vendor，仅渲染时着色，不改原文） ---------- */

function highlightCode(root) {
  if (!window.hljs || !root) return;
  root.querySelectorAll('pre code').forEach((el) => {
    if (el.dataset.highlighted) return;
    try { window.hljs.highlightElement(el); } catch (err) { /* 高亮失败不影响阅读 */ }
  });
}

/* ---------- [[链接]]：点击跳转到同名文档，不做反链不做图谱 ---------- */

function findDocByTitle(title) {
  const want = String(title || '').trim().toLowerCase();
  if (!want) return null;
  let found = null;
  (function walk(nodes) {
    (nodes || []).forEach((node) => {
      if (found) return;
      if (node.type !== 'dir' && String(node.name || '').trim().toLowerCase() === want) { found = node; return; }
      if (node.children) walk(node.children);
    });
  })(state.tree ? state.tree.children : []);
  return found;
}

/* ---------- 导出 HTML：单文件、内嵌样式，打印即 PDF；不写 records/ ---------- */

const EXPORT_CSS = `
  body { margin: 0; background: #FDFCFA; color: #26251F; font: 15px/1.75 Georgia, 'Times New Roman', 'Songti SC', 'SimSun', serif; }
  article { max-width: 760px; margin: 0 auto; padding: 56px 32px 80px; }
  h1,h2,h3,h4 { font-weight: 500; line-height: 1.4; margin: 1.6em 0 .6em; }
  h1 { font-size: 1.7em; } h2 { font-size: 1.35em; } h3 { font-size: 1.15em; }
  p { margin: .5em 0; } a { color: #1D6E56; }
  blockquote { margin: 1em 0; padding: 2px 18px; border-left: 3px solid #D3D1C7; color: #5F5E5A; }
  pre { background: #F1EFE8; border-radius: 6px; padding: 14px 16px; overflow: auto; font: 13px/1.6 Consolas, Menlo, monospace; }
  code { font-family: Consolas, Menlo, monospace; }
  table { border-collapse: collapse; width: 100%; margin: 1em 0; font-size: .92em; }
  th, td { border: 1px solid #D3D1C7; padding: 7px 11px; text-align: left; }
  img { max-width: 100%; border-radius: 4px; }
  .wikilink { border-bottom: 1px dashed #B4B2A9; }
  .todo { list-style: none; margin: 4px 0; }
  @media print { body { background: #fff; } article { padding: 0; max-width: none; } }
`;

function exportDocHtml(node) {
  const run = (doc) => {
    const wrapper = document.createElement('div');
    wrapper.innerHTML = renderMarkdown(doc.body);
    wrapper.querySelectorAll('.wikilink').forEach((el) => { el.className = 'wikilink'; });
    const inner = wrapper.innerHTML;
    const html = `<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">`
      + `<title>${esc(doc.title || '文档')}</title><style>${EXPORT_CSS}</style></head>`
      + `<body><article>${inner}</article></body></html>`;
    const blob = new Blob([html], { type: 'text/html;charset=utf-8' });
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob);
    a.download = `${doc.title || '文档'}.html`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 3000);
    notice('已导出 HTML 单文件，浏览器里 Ctrl+P 打印即可存成 PDF。');
  };
  if (node && node.id) {
    api(`/docs/${encodeURIComponent(node.id)}`).then((data) => run(data.doc)).catch((err) => notice(failureText(err.code, err.message)));
  } else if (state.doc) {
    if (state.editing) { exitEdit().then(() => run(state.doc)); } else run(state.doc);
  } else {
    notice('先选一篇文档。');
  }
}

function inlineMd(node) {
  let out = '';
  Array.from(node.childNodes).forEach((child) => {
    if (child.nodeType === 3) { out += child.nodeValue.replace(/\u00a0/g, ' '); return; }
    if (child.nodeType !== 1) return;
    const tag = child.tagName.toLowerCase();
    if (tag === 'strong' || tag === 'b') out += `**${inlineMd(child).trim()}**`;
    else if (tag === 'em' || tag === 'i') out += `*${inlineMd(child).trim()}*`;
    else if (tag === 'code') out += '`' + child.textContent + '`';
    else if (tag === 'a') out += `[${inlineMd(child)}](${child.getAttribute('href') || ''})`;
    else if (tag === 'img') out += `![${child.getAttribute('alt') || ''}](${child.getAttribute('src') || ''})`;
    else if (tag === 'br') out += '\n';
    else if (child.classList && child.classList.contains('wikilink')) out += `[[${child.textContent}]]`;
    else out += inlineMd(child);
  });
  return out;
}

function listMd(listEl, depth) {
  const ordered = listEl.tagName.toLowerCase() === 'ol';
  const lines = [];
  let index = 0;
  Array.from(listEl.children).forEach((child) => {
    const tag = child.tagName.toLowerCase();
    if (tag === 'ul' || tag === 'ol') {
      lines.push(...listMd(child, depth + 1).split('\n'));
      return;
    }
    if (tag !== 'li') return;
    index += 1;
    const indent = '  '.repeat(depth);
    const marker = ordered ? `${index}. ` : '- ';
    const nested = Array.from(child.children).filter((c) => /^(ul|ol)$/i.test(c.tagName));
    const own = Array.from(child.childNodes).filter((n) => !(n.nodeType === 1 && /^(ul|ol)$/i.test(n.tagName)));
    const wrap = document.createElement('span');
    own.forEach((n) => wrap.appendChild(n.cloneNode(true)));
    lines.push(indent + marker + inlineMd(wrap).trim());
    nested.forEach((n) => lines.push(...listMd(n, depth + 1).split('\n')));
  });
  return lines.join('\n');
}

function tableMd(table) {
  const rows = Array.from(table.querySelectorAll('tr')).map((tr) =>
    Array.from(tr.children).map((cell) => inlineMd(cell).replace(/\s+/g, ' ').trim()));
  if (!rows.length) return '';
  const width = Math.max(...rows.map((row) => row.length));
  const pad = (row) => { const next = row.slice(); while (next.length < width) next.push(''); return next; };
  const line = (cells) => '| ' + pad(cells).join(' | ') + ' |';
  const head = rows[0];
  const rest = rows.slice(1);
  return [line(head), '| ' + Array(width).fill('---').join(' | ') + ' |', ...rest.map(line)].join('\n');
}

function todoMd(el) {
  const box = el.querySelector('input[type=checkbox]');
  const text = inlineMd(el).replace(/\s+/g, ' ').trim();
  return `- [${box && box.checked ? 'x' : ' '}] ${text}`;
}

function htmlToMarkdown(root) {
  const blocks = [];
  const push = (text, kind) => {
    const value = String(text == null ? '' : text).replace(/[ \t]+$/gm, '').trim();
    if (!value) return;
    blocks.push({ text: value, kind });
  };
  const collect = (parent) => {
    Array.from(parent.childNodes).forEach((node) => {
      if (node.nodeType === 3) { const text = node.nodeValue.trim(); if (text) push(text, 'text'); return; }
      if (node.nodeType !== 1) return;
      const el = node;
      const tag = el.tagName.toLowerCase();
      if (el.classList.contains('empty')) return;
      if (el.classList.contains('todo')) { push(todoMd(el), 'todo'); return; }
      if (tag === 'ul' || tag === 'ol') { push(listMd(el, 0), 'list'); return; }
      if (tag === 'table') { push(tableMd(el), 'table'); return; }
      if (tag === 'pre') { push('```\n' + el.textContent.replace(/\n+$/, '') + '\n```', 'code'); return; }
      if (/^h[1-6]$/.test(tag)) { push('#'.repeat(Number(tag[1])) + ' ' + inlineMd(el).trim(), 'text'); return; }
      if (tag === 'blockquote') {
        const paras = Array.from(el.querySelectorAll('p'))
          .map((p) => inlineMd(p).replace(/\s+/g, ' ').trim()).filter(Boolean);
        push(paras.length ? paras.map((p) => '> ' + p).join('\n>\n')
                          : '> ' + inlineMd(el).replace(/\s+/g, ' ').trim(), 'text');
        return;
      }
      if (tag === 'div' || tag === 'section' || tag === 'article') {
        const hasBlock = el.querySelector('h1,h2,h3,h4,p,ul,ol,table,pre,blockquote,.todo');
        if (hasBlock) { collect(el); return; }
        push(inlineMd(el), 'text');
        return;
      }
      push(inlineMd(el), 'text');
    });
  };
  collect(root);
  let out = '';
  blocks.forEach((block, index) => {
    if (index === 0) { out = block.text; return; }
    const prev = blocks[index - 1];
    const tight = prev.kind === block.kind && (block.kind === 'list' || block.kind === 'todo');
    out += (tight ? '\n' : '\n\n') + block.text;
  });
  return out ? out + '\n' : '';
}

function placeCaret(x, y) {
  const body = $('doc-body');
  let range = null;
  if (typeof document.caretRangeFromPoint === 'function') {
    range = document.caretRangeFromPoint(x, y);
  } else if (document.caretPositionFromPoint) {
    const pos = document.caretPositionFromPoint(x, y);
    if (pos && pos.offsetNode) {
      range = document.createRange();
      range.setStart(pos.offsetNode, pos.offset);
      range.collapse(true);
    }
  }
  if (!range || !body.contains(range.startContainer)) {
    range = document.createRange();
    range.selectNodeContents(body);
    range.collapse(y > body.getBoundingClientRect().bottom);
  }
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
}

function enterEdit(x, y) {
  if (!state.doc || state.editing) return;
  const body = $('doc-body');
  state.editing = true;
  body.classList.add('wysiwyg');
  body.setAttribute('contenteditable', 'true');
  body.setAttribute('spellcheck', 'false');
  if (body.querySelector('.empty')) body.innerHTML = '<p><br></p>';
  /* H1 是标题，不是正文：编辑态下不可续写。
     旧行为让用户「新建后直接打字」并进 H1，连带污染 frontmatter 的 title 与树名。 */
  body.querySelectorAll('h1').forEach((h) => h.setAttribute('contenteditable', 'false'));
  /* 只有标题的文档补一个空段落，光标落在正文而不是标题上。 */
  let seeded = false;
  if (!body.querySelector('p, h2, h3, h4, ul, ol, table, pre, blockquote, .todo')) {
    const p = document.createElement('p');
    p.appendChild(document.createElement('br'));
    body.appendChild(p);
    seeded = true;
  }
  body.querySelectorAll('input[type=checkbox]').forEach((box) => {
    box.onchange = () => {
      const span = box.parentElement.querySelector('span');
      if (span) span.classList.toggle('done', box.checked);
      scheduleSave();
    };
  });
  body.focus({ preventScroll: true });
  if (seeded) focusAt(body.querySelector('p:last-of-type') || body, 0);
  else placeCaret(x, y);
  setSaveState('编辑中… Esc 退出');
}

/* Markdown 快捷输入：在段落开头敲 # / - / 1. / > / - [ ] 直接变成对应区块 */
function currentBlock() {
  const sel = window.getSelection();
  if (!sel || !sel.rangeCount) return null;
  let node = sel.anchorNode;
  if (!node) return null;
  if (node.nodeType === 3) node = node.parentElement;
  const body = $('doc-body');
  let el = node;
  while (el && el.parentElement && el.parentElement !== body) el = el.parentElement;
  return el && el.parentElement === body ? el : null;
}

function focusStart(el) {
  focusAt(el, 0);
}

function caretOffsetIn(el) {
  const sel = window.getSelection();
  if (!sel || !sel.rangeCount) return null;
  const range = sel.getRangeAt(0);
  if (!el.contains(range.endContainer)) return null;
  const before = document.createRange();
  before.selectNodeContents(el);
  try { before.setEnd(range.endContainer, range.endOffset); } catch (err) { return null; }
  return before.toString().length;
}

function focusAt(el, offset) {
  if (!el) return;
  const range = document.createRange();
  const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
  let node = walker.nextNode();
  let remaining = Math.max(0, offset || 0);
  if (!node) {
    range.selectNodeContents(el);
    range.collapse(false);
  } else {
    for (;;) {
      const length = node.nodeValue.length;
      if (remaining <= length) { range.setStart(node, remaining); break; }
      remaining -= length;
      const next = walker.nextNode();
      if (!next) { range.setStart(node, length); break; }
      node = next;
    }
    range.collapse(true);
  }
  const sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
}

function applyMdShortcut() {
  const el = currentBlock();
  if (!el || !/^(P|DIV)$/.test(el.tagName)) return false;
  const text = el.textContent;
  /* 标记序列还没敲完（`- ` / `- [` / `- [ ]`）：先不转换。
     否则 `/^[-*]\s/` 会在用户敲到 `- ` 的瞬间抢先转成列表，
     后面的 `[ ] ` 只剩残片，落盘成 `- ] …` / `- [` 这种坏 Markdown。 */
  if (/^[-*]\s*(?:\[[ xX]?\]?)?\s*$/.test(text)) return false;
  const heading = text.match(/^(#{1,4})\s/);
  const todo = text.match(/^[-*]\s\[([ xX])\]\s?/);
  const bullet = text.match(/^[-*]\s/);
  const num = text.match(/^(\d+)\.\s/);
  const quote = text.match(/^>\s/);
  if (!heading && !todo && !bullet && !num && !quote) return false;

  const markerLength = heading ? heading[0].length
    : todo ? todo[0].length : bullet ? bullet[0].length : num ? num[0].length : quote[0].length;
  const caretOffset = caretOffsetIn(el);
  /* 光标不在该块里时回落到块末尾，别把光标甩回块首打乱输入。 */
  const targetOffset = Math.max(0, (caretOffset === null ? text.length : caretOffset) - markerLength);

  let replacement;
  if (heading) {
    const level = Math.min(heading[1].length, 4);
    replacement = document.createElement('h' + level);
    replacement.textContent = text.slice(heading[0].length);
  } else if (todo) {
    replacement = document.createElement('div');
    replacement.className = 'todo';
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = todo[1].toLowerCase() === 'x';
    const span = document.createElement('span');
    if (box.checked) span.className = 'done';
    span.textContent = text.slice(todo[0].length);
    replacement.append(box, span);
    box.onchange = () => { span.classList.toggle('done', box.checked); scheduleSave(); };
  } else if (bullet || num) {
    const list = document.createElement(num ? 'ol' : 'ul');
    const li = document.createElement('li');
    li.textContent = text.slice((bullet || num)[0].length);
    list.appendChild(li);
    replacement = list;
  } else {
    replacement = document.createElement('blockquote');
    replacement.textContent = text.slice(quote[0].length);
  }
  el.replaceWith(replacement);
  const target = /^(UL|OL)$/.test(replacement.tagName) ? replacement.querySelector('li') : replacement;
  if (target && !target.textContent && !target.querySelector('br')) {
    target.appendChild(document.createElement('br'));
  }
  focusAt(target, targetOffset);
  /* 延后成型后不再做 rAF 二次定位：那时用户可能已经继续输入，
     把光标拉回旧 offset 会把字符插错位置（实测中文输入会出现字符错位）。 */
  scheduleSave();
  return true;
}

/* 空列表项上回车 = 退出列表，回到普通段落（Chrome 默认只会一直加 li） */
function exitEmptyListItem(event) {
  const sel = window.getSelection();
  if (!sel || !sel.rangeCount) return false;
  let node = sel.anchorNode;
  if (!node) return false;
  if (node.nodeType === 3) node = node.parentElement;
  const li = node && node.closest ? node.closest('li') : null;
  if (!li || li.textContent.trim() !== '') return false;
  const list = li.closest('ul,ol');
  if (!list) return false;
  event.preventDefault();
  const paragraph = document.createElement('p');
  paragraph.appendChild(document.createElement('br'));
  if (list.querySelectorAll('li').length > 1) {
    li.remove();
    list.after(paragraph);
  } else {
    list.replaceWith(paragraph);
  }
  focusStart(paragraph);
  scheduleSave();
  return true;
}

/* 空待办项上回车 = 退出待办，回到普通段落 */
function exitEmptyTodo(event) {
  const el = currentBlock();
  if (!el || !el.classList.contains('todo')) return false;
  if (el.textContent.trim() !== '') return false;
  event.preventDefault();
  const paragraph = document.createElement('p');
  paragraph.appendChild(document.createElement('br'));
  el.replaceWith(paragraph);
  focusStart(paragraph);
  scheduleSave();
  return true;
}

/* ---------- 表格增删行列：右键单元格，直接改渲染后的表格 ---------- */

function tableAction(cell, action) {
  const table = cell.closest('table');
  if (!table) return;
  const rows = Array.from(table.querySelectorAll('tr'));
  const tr = cell.parentElement;
  const rowIndex = rows.indexOf(tr);
  const colIndex = Array.from(tr.children).indexOf(cell);
  const width = Math.max(...rows.map((r) => r.children.length));
  const blank = () => { const el = document.createElement('td'); el.innerHTML = '<br>'; return el; };

  if (action === 'row-above' || action === 'row-below') {
    const fresh = document.createElement('tr');
    for (let i = 0; i < width; i += 1) fresh.appendChild(blank());
    tr.parentNode.insertBefore(fresh, action === 'row-above' ? tr : tr.nextSibling);
  } else if (action === 'col-left' || action === 'col-right') {
    rows.forEach((row) => {
      const ref = row.children[colIndex] || null;
      row.insertBefore(blank(), action === 'col-left' ? ref : (ref ? ref.nextSibling : null));
    });
  } else if (action === 'row-delete') {
    if (rows.length <= 1) { notice('最后一行不能删。'); return; }
    tr.remove();
  } else if (action === 'col-delete') {
    if (rows.some((row) => row.children.length <= 1)) { notice('最后一列不能删。'); return; }
    rows.forEach((row) => { const target = row.children[colIndex]; if (target) target.remove(); });
  } else {
    return;
  }
  scheduleSave();
  notice('表格已改动，正在自动保存。');
}

function showTableMenu(x, y, cell) {
  document.querySelectorAll('.table-menu').forEach((el) => el.remove());
  const menu = document.createElement('div');
  menu.className = 'ctx-menu table-menu';
  const actions = [
    ['row-above', '在上方插入一行'], ['row-below', '在下方插入一行'],
    ['col-left', '在左侧插入一列'], ['col-right', '在右侧插入一列'],
    ['sep', ''], ['row-delete', '删除这一行'], ['col-delete', '删除这一列'],
  ];
  menu.innerHTML = '<span class="ctx-title">表格</span>' + actions.map(([act, label]) =>
    act === 'sep' ? '<div class="ctx-sep"></div>'
      : `<button type="button" class="ctx-item${act.includes('delete') ? ' danger' : ''}" data-act="${act}">${label}</button>`).join('');
  document.body.appendChild(menu);
  menu.style.left = Math.max(4, Math.min(x, window.innerWidth - 200)) + 'px';
  menu.style.top = Math.max(4, Math.min(y, window.innerHeight - 250)) + 'px';
  menu.querySelectorAll('[data-act]').forEach((btn) => {
    btn.onclick = () => { const act = btn.dataset.act; menu.remove(); tableAction(cell, act); };
  });
  const dismiss = (event) => {
    if (!menu.contains(event.target)) { menu.remove(); document.removeEventListener('mousedown', dismiss); }
  };
  setTimeout(() => document.addEventListener('mousedown', dismiss), 0);
}

/* ---------- 图片：粘贴 / 拖入 → 服务端落盘 → 插入 ![]() ---------- */

async function uploadImage(file) {
  const selection = window.getSelection();
  const saved = selection && selection.rangeCount ? selection.getRangeAt(0).cloneRange() : null;
  notice('图片上传中…');
  try {
    const dataUrl = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ''));
      reader.onerror = () => reject(new Error('读取图片失败'));
      reader.readAsDataURL(file);
    });
    const out = await api('/assets/upload', {
      method: 'POST', body: JSON.stringify({ data: dataUrl, name: file.name }),
    });
    if (saved && selection) { selection.removeAllRanges(); selection.addRange(saved); }
    $('doc-body').focus({ preventScroll: true });
    document.execCommand('insertHTML', false,
      `<img src="${esc(out.url)}" alt="${esc(file.name || '图片')}"><p><br></p>`);
    scheduleSave();
    notice('图片已插入，正在自动保存。');
  } catch (err) {
    notice('图片没能插入：' + (err.message || '上传失败'));
  }
}

function bindWysiwyg() {
  const body = $('doc-body');
  body.addEventListener('input', () => {
    if (!state.editing) return;
    /* 延后到输入停顿再成型：逐字符即时转换会把 `- [ ] ` 打散成坏 Markdown。 */
    clearTimeout(state.shortcutTimer);
    state.shortcutTimer = setTimeout(() => {
      state.shortcutTimer = null;
      if (state.editing) applyMdShortcut();
    }, 240);
    scheduleSave();
  });
  body.addEventListener('keydown', (event) => {
    if (!state.editing) return;
    /* 回车 / Esc / Ctrl+S 之前先把待成型的标记落定，别把半成品留在正文里。 */
    if (state.shortcutTimer) {
      clearTimeout(state.shortcutTimer);
      state.shortcutTimer = null;
      applyMdShortcut();
    }
    if (event.key === 'Enter' && !event.shiftKey && exitEmptyListItem(event)) return;
    if (event.key === 'Enter' && !event.shiftKey && exitEmptyTodo(event)) return;
    if (event.key === 'Escape') { event.preventDefault(); exitEdit(); }
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 's') {
      event.preventDefault();
      flushSave(true);
      return;
    }
    if ((event.metaKey || event.ctrlKey) && (event.key.toLowerCase() === 'b' || event.key.toLowerCase() === 'i')) {
      event.preventDefault();
      document.execCommand(event.key.toLowerCase() === 'b' ? 'bold' : 'italic');
      scheduleSave();
    }
  });
  body.addEventListener('paste', (event) => {
    if (!state.editing) return;
    const clip = event.clipboardData || window.clipboardData;
    const items = (clip && clip.items) || [];
    for (const item of items) {
      if (item.type && item.type.startsWith('image/')) {
        event.preventDefault();
        const file = item.getAsFile();
        if (file) uploadImage(file);
        return;
      }
    }
    event.preventDefault();
    const text = (clip && clip.getData('text/plain')) || '';
    document.execCommand('insertText', false, text);
  });
  body.addEventListener('contextmenu', (event) => {
    const cell = event.target.closest ? event.target.closest('th,td') : null;
    if (!cell || !body.contains(cell)) return;
    event.preventDefault();
    if (!state.editing) enterEdit(event.clientX, event.clientY);
    showTableMenu(event.clientX, event.clientY, cell);
  });
  body.addEventListener('dragover', (event) => {
    if (!state.editing || !event.dataTransfer) return;
    if (Array.from(event.dataTransfer.types || []).includes('Files')) event.preventDefault();
  });
  body.addEventListener('drop', (event) => {
    if (!state.editing || !event.dataTransfer || !event.dataTransfer.files.length) return;
    const file = Array.from(event.dataTransfer.files).find((item) => item.type.startsWith('image/'));
    if (!file) return;
    event.preventDefault();
    uploadImage(file);
  });
  body.addEventListener('click', (event) => {
    if (state.editing && event.target.tagName === 'A') event.preventDefault();
  });
}

function scheduleSave() {
  state.dirty = true;
  setSaveState('编辑中… Esc 退出');
  clearTimeout(state.timer);
  const wait = Math.max(700, 3000 - (Date.now() - state.lastSaveAt));
  state.timer = setTimeout(() => flushSave(), wait);
}

function showConflict(message) {
  const el = form(`<div class="conflict-bar"><span>${esc(message)}</span>
    <button type="button" class="btn" id="conflict-overwrite">用我的覆盖</button>
    <button type="button" class="btn" id="conflict-reload">载入磁盘版本</button></div>`);
  el.querySelector('#conflict-overwrite').onclick = async () => {
    try {
      const fresh = await api('/docs/' + encodeURIComponent(state.doc.id));
      state.baseUpdated = fresh.doc.updated;
      closeForm();
      await flushSave(true);
    } catch (err) { notice(failureText(err.code, err.message)); }
  };
  el.querySelector('#conflict-reload').onclick = async () => {
    state.dirty = false;
    closeForm();
    await openDoc(state.doc.id);
    notice('已载入磁盘上的版本。');
  };
}

async function flushSave(force) {
  clearTimeout(state.timer);
  if (!state.doc) return;
  if (!state.editing) return;
  if (!state.dirty && !force) return;
  const content = htmlToMarkdown($('doc-body'));
  if (!content.trim()) { setSaveState('正文不能为空'); return; }
  state.dirty = false;
  setSaveState('保存中…');
  try {
    const data = await api('/docs/' + encodeURIComponent(state.doc.id), {
      method: 'PUT',
      body: JSON.stringify({ content, base_updated: state.baseUpdated || state.doc.updated }),
    });
    state.doc.body = content;
    state.doc.updated = data.updated;
    state.baseUpdated = data.updated;
    state.lastSaveAt = Date.now();
    if (data.previous_revision) {
      const top = undoStack[undoStack.length - 1];
      const sameDoc = top && top.type === 'edit' && top.id === state.doc.id;
      if (!sameDoc) {
        pushUndo({ type: 'edit', id: state.doc.id, rev: data.previous_revision, label: '编辑正文' });
      }
    }
    setSaveState('已自动保存 · ' + new Date().toTimeString().slice(0, 8));
  } catch (err) {
    state.dirty = true;
    if (err.code === 'CONFLICT') { showConflict(err.message); return; }
    setSaveState('保存失败，内容还在输入框里');
    notice(failureText(err.code, err.message));
  }
}

async function exitEdit() {
  if (state.shortcutTimer) {
    clearTimeout(state.shortcutTimer);
    state.shortcutTimer = null;
    if (state.editing) applyMdShortcut();
  }
  await flushSave();
  if (state.dirty) return;
  state.editing = false;
  const body = $('doc-body');
  body.classList.remove('wysiwyg');
  body.removeAttribute('contenteditable');
  setSaveState('');
  renderDoc();
  $('chat-sub').textContent = chatSubText();
  await loadTree();
}

async function loadGoals() {
  const data = await api('/goals');
  state.goals = data.goals;
}

function dirOptions(node, prefix, acc) {
  (node.children || []).forEach((child) => {
    if (child.type === 'dir') {
      const path = prefix ? prefix + '/' + child.name : child.name;
      acc.push(path);
      dirOptions(child, path, acc);
    }
  });
  return acc;
}

function parentPathOf(node) {
  const parts = String(node.path || '').split('/').filter(Boolean);
  return parts.slice(1, -1).join('/');
}

function dirSelectHtml(id, current, exclude, withRoot) {
  // exclude 传进来的是 "records/xxx"，选项里是 "xxx"，先去掉前缀再比较
  const skip = exclude ? String(exclude).replace(/^records\/?/, '') : '';
  const dirs = dirOptions(state.tree || { children: [] }, '', []).filter((d) =>
    !skip || (d !== skip && !d.startsWith(skip + '/')));
  const rootOption = withRoot
    ? `<option value=""${!current ? ' selected' : ''}>（笔记根）</option>` : '';
  return `<select id="${id}">${rootOption}${dirs.map((d) =>
    `<option value="${esc(d)}"${d === current ? ' selected' : ''}>${esc(d)}</option>`).join('')}</select>`;
}

function closeMenu() {
  const menu = document.getElementById('ctx-menu');
  if (menu) menu.remove();
}

function openContextMenu(x, y, node) {
  closeMenu();
  const items = node.type === 'dir'
    ? [
        { label: '新建笔记', run: () => newNoteIn(node.path) },
        { label: '重命名', run: () => openRenameFolder(node) },
        { label: '移动到…', run: () => openMoveFolder(node) },
        { label: '复制到…', run: () => openCopyFolder(node) },
        { label: '删除（移入回收站）', danger: true, run: () => openDeleteFolder(node) },
      ]
    : (node.orphan
      // 孤儿还没有 id，按 id 的操作（重命名/移动/删除/导出）都用不了，
      // 所以只给「补齐」——补齐完才有 id，才谈得上其他操作。
      ? [
          { label: '补齐（加上元信息）', run: () => openAdopt(node) },
        ]
      : [
          { label: '重命名', run: () => openRenameDoc(node) },
          { label: '移动到…', run: () => openMoveDoc(node) },
          { label: '复制到…', run: () => openCopyDoc(node) },
          { label: '导出 HTML', run: () => exportDocHtml(node) },
          { label: '删除（移入回收站）', danger: true, run: () => openDeleteDoc(node) },
        ]);
  const menu = document.createElement('div');
  menu.className = 'ctx-menu';
  menu.id = 'ctx-menu';
  menu.style.left = Math.min(x, window.innerWidth - 190) + 'px';
  menu.style.top = Math.min(y, window.innerHeight - items.length * 34 - 12) + 'px';
  items.forEach((item) => {
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'ctx-item' + (item.danger ? ' danger' : '');
    btn.textContent = item.label;
    btn.onclick = () => { closeMenu(); item.run(); };
    menu.appendChild(btn);
  });
  document.body.appendChild(menu);
}

async function newNoteIn(dirPath) {
  if (state.editing) await exitEdit();
  try {
    const created = await api('/records', {
      method: 'POST',
      body: JSON.stringify({
        title: '未命名笔记', content: '# 未命名笔记\n\n', dir: dirPath || 'records',
        stream: 'self', kind: 'thought', topic: [],
      }),
    });
    await loadTree();
    await openDoc(created.id);
    pushUndo({ type: 'create-doc', id: created.id, label: '新建笔记' });
    toast('已新建笔记，在左侧树里给它起个名字。');
    startRename('doc', created.id);
  } catch (err) { notice(failureText(err.code, err.message)); }
}

async function newFolderIn(dirPath) {
  if (state.editing) await exitEdit();
  try {
    const data = await api('/folders', {
      method: 'POST',
      body: JSON.stringify({ parent: dirPath || '', name: '未命名文件夹' }),
    });
    await loadTree();
    pushUndo({ type: 'create-dir', path: data.path, label: '新建文件夹' });
    toast('已新建文件夹，在左侧树里给它起个名字。');
    startRename('dir', data.path);
  } catch (err) { notice(failureText(err.code, err.message)); }
}

function startRename(kind, key) {
  const selector = kind === 'doc'
    ? `.node[data-id="${CSS.escape(key)}"] .node-label`
    : `.folder-row[data-path="${CSS.escape(key)}"] .fname`;
  const label = document.querySelector(selector);
  if (!label) return;
  const host = label.closest('.node, .folder-row');
  const original = label.textContent;
  const input = document.createElement('input');
  input.className = 'rename-input';
  input.value = original;
  label.replaceWith(input);
  input.focus();
  input.select();

  let settled = false;
  const finish = async (commit) => {
    if (settled) return;
    settled = true;
    const value = input.value.trim();
    if (!commit || !value || value === original) {
      input.replaceWith(label);
      return;
    }
    try {
      if (kind === 'doc') {
        await api('/docs/' + encodeURIComponent(key) + '/rename', {
          method: 'POST',
          body: JSON.stringify({ title: value, dir: (host && host.dataset.parent) || '' }),
        });
        pushUndo({ type: 'rename-doc', id: key, oldTitle: original,
                   dir: (host && host.dataset.parent) || '', label: '重命名' });
      } else {
        const renamed = await api('/folders/rename', {
          method: 'POST',
          body: JSON.stringify({ path: key, name: value }),
        });
        pushUndo({ type: 'rename-dir', path: renamed.path, oldName: original,
                   label: '重命名文件夹' });
      }
      await loadTree();
      if (kind === 'doc' && state.doc && state.doc.id === key) await openDoc(key);
    } catch (err) {
      notice(failureText(err.code, err.message));
      input.replaceWith(label);
    }
  };
  input.onblur = () => finish(true);
  input.onkeydown = (event) => {
    if (event.key === 'Enter') { event.preventDefault(); finish(true); }
    if (event.key === 'Escape') { event.preventDefault(); finish(false); }
  };
}

function openRenameDoc(node) {
  const el = form(`<label>重命名《${esc(node.name)}》</label>
    <div class="row"><input id="rd-title" type="text" value="${esc(node.name)}">
    ${dirSelectHtml('rd-dir', parentPathOf(node))}
    <button type="button" class="btn" id="rd-save">保存</button></div>`);
  el.querySelector('#rd-save').onclick = async () => {
    try {
      const targetDir = el.querySelector('#rd-dir').value;
      await api('/docs/' + encodeURIComponent(node.id) + '/rename', {
        method: 'POST',
        body: JSON.stringify({ title: el.querySelector('#rd-title').value, dir: targetDir }),
      });
      pushUndo({ type: 'rename-doc', id: node.id, oldTitle: node.name,
                 dir: parentPathOf(node), label: '重命名' });
      closeForm(); await loadTree();
      if (state.doc && state.doc.id === node.id) await openDoc(node.id);
      notice('已重命名。');
    } catch (err) { notice(failureText(err.code, err.message)); }
  };
}

function openMoveFolder(node) {
  const el = form(`<label>把文件夹《${esc(node.name)}》移动到</label>
    <div class="row">${dirSelectHtml('mvf-dir', parentPathOf(node), node.path, true)}
    <button type="button" class="btn" id="mvf-save">移动</button></div>
    <p class="hint" style="padding:0">可以放进任意一层文件夹；不能移到它自己或它的子文件夹里。</p>`);
  el.querySelector('#mvf-save').onclick = async () => {
    try {
      const target = el.querySelector('#mvf-dir').value;
      await api('/folders/move', {
        method: 'POST',
        body: JSON.stringify({ from: node.path, to: target || 'records' }),
      });
      closeForm(); await loadTree();
      pushUndo({ type: 'move-dir', from: parentPathOf(node), to: target || 'records',
                 label: '移动文件夹「' + node.name + '」' });
      if (state.doc && (state.doc.path || '').startsWith(node.path + '/')) await openDoc(state.doc.id);
      notice(target ? '已移动到「' + target + '」。' : '已移动到笔记根。');
    } catch (err) { notice(failureText(err.code, err.message)); }
  };
}

function openMoveDoc(node) {
  const el = form(`<label>把《${esc(node.name)}》移动到</label>
    <div class="row">${dirSelectHtml('mv-dir', parentPathOf(node))}
    <button type="button" class="btn" id="mv-save">移动</button></div>`);
  el.querySelector('#mv-save').onclick = async () => {
    try {
      await api('/docs/' + encodeURIComponent(node.id) + '/move', {
        method: 'POST',
        body: JSON.stringify({ dir: el.querySelector('#mv-dir').value }),
      });
      closeForm(); await loadTree();
      if (state.doc && state.doc.id === node.id) await openDoc(node.id);
      notice('已移动。');
    } catch (err) { notice(failureText(err.code, err.message)); }
  };
}

function openCopyDoc(node) {
  const el = form(`<label>复制《${esc(node.name)}》到</label>
    <div class="row">${dirSelectHtml('cp-dir', parentPathOf(node))}
    <button type="button" class="btn" id="cp-save">复制</button></div>`);
  el.querySelector('#cp-save').onclick = async () => {
    try {
      await api('/docs/' + encodeURIComponent(node.id) + '/copy', {
        method: 'POST',
        body: JSON.stringify({ dir: el.querySelector('#cp-dir').value }),
      });
      closeForm(); await loadTree();
      notice('已复制一份。');
    } catch (err) { notice(failureText(err.code, err.message)); }
  };
}

async function openDeleteDoc(node) {
  try {
    const data = await api('/docs/' + encodeURIComponent(node.id) + '/delete', { method: 'POST' });
    pushUndo({ type: 'delete-doc', id: node.id, trashName: data.trash_name,
               to: String(data.original || '').split('/').slice(1, -1).join('/'),
               label: '删除《' + node.name + '》' });
    await loadTree();
    if (state.doc && state.doc.id === node.id) {
      state.doc = null;
      closeForm();
      $('doc-body').innerHTML = '<p class="empty">这篇已移入回收站。按 Ctrl+Z 可以撤回。</p>';
      $('crumb').textContent = '已删除';
      $('doc-target').classList.add('hidden');
      $('pending-actions').classList.add('hidden');
    }
    notice('已移入回收站。按 Ctrl+Z 可以撤回。');
  } catch (err) { notice(failureText(err.code, err.message)); }
}

function openRenameFolder(node) {
  const el = form(`<label>重命名文件夹《${esc(node.name)}》</label>
    <div class="row"><input id="rf-name" type="text" value="${esc(node.name)}">
    <button type="button" class="btn" id="rf-save">保存</button></div>`);
  el.querySelector('#rf-save').onclick = async () => {
    try {
      const renamed = await api('/folders/rename', {
        method: 'POST',
        body: JSON.stringify({ path: node.path, name: el.querySelector('#rf-name').value }),
      });
      pushUndo({ type: 'rename-dir', path: renamed.path, oldName: node.name,
                 label: '重命名文件夹' });
      closeForm(); await loadTree();
      notice('文件夹已重命名。');
    } catch (err) { notice(failureText(err.code, err.message)); }
  };
}

function openCopyFolder(node) {
  const el = form(`<label>复制文件夹《${esc(node.name)}》到</label>
    <div class="row">${dirSelectHtml('cf-dir', parentPathOf(node))}
    <button type="button" class="btn" id="cf-save">复制</button></div>`);
  el.querySelector('#cf-save').onclick = async () => {
    try {
      await api('/folders/copy', {
        method: 'POST',
        body: JSON.stringify({ from: node.path,
                               to: el.querySelector('#cf-dir').value || 'records' }),
      });
      closeForm(); await loadTree();
      notice('文件夹已复制。');
    } catch (err) { notice(failureText(err.code, err.message)); }
  };
}

async function openDeleteFolder(node) {
  try {
    const data = await api('/folders/delete', {
      method: 'POST', body: JSON.stringify({ path: node.path }),
    });
    pushUndo({ type: 'delete-dir', trashName: data.trash_name,
               to: String(data.original || '').split('/').slice(1, -1).join('/'),
               label: '删除文件夹《' + node.name + '》' });
    await loadTree();
    if (state.doc && state.doc.path && state.doc.path.startsWith(node.path + '/')) {
      state.doc = null;
      closeForm();
      $('doc-body').innerHTML = '<p class="empty">所在文件夹已移入回收站。按 Ctrl+Z 可以撤回。</p>';
      $('crumb').textContent = '已删除';
      $('doc-target').classList.add('hidden');
    }
    notice('文件夹已移入回收站。按 Ctrl+Z 可以撤回。');
  } catch (err) { notice(failureText(err.code, err.message)); }
}

function clearDropMarks() {
  document.querySelectorAll('.drop-in, .drop-before, .drop-after').forEach((el) => {
    el.classList.remove('drop-in', 'drop-before', 'drop-after');
  });
}

function attachDrag(el, node, parentPath) {
  el.dataset.kind = node.type;
  el.dataset.path = node.path;
  el.dataset.file = node.file || '';
  el.dataset.parent = parentPath;
  if (node.id) el.dataset.id = node.id;
  // 孤儿还没有 id，拖动后的移动/复制都无从下手——先补齐，再整理。
  // 右键菜单仍然保留（补齐入口在那儿）。
  if (node.orphan) {
    el.draggable = false;
    el.addEventListener('contextmenu', (event) => {
      event.preventDefault();
      event.stopPropagation();
      openContextMenu(event.clientX, event.clientY, node);
    });
    return;
  }
  el.draggable = true;

  el.addEventListener('dragstart', (event) => {
    if (state.editing) { event.preventDefault(); return; }
    state.drag = { kind: node.type, path: node.path, file: node.file, id: node.id, parent: parentPath };
    event.dataTransfer.effectAllowed = 'move';
    event.dataTransfer.setData('text/plain', node.path);
    el.classList.add('dragging');
  });
  el.addEventListener('dragend', () => {
    el.classList.remove('dragging');
    state.drag = null;
    clearTimeout(state.expandTimer);
    state.expandTimer = null;
    clearDropMarks();
  });
  el.addEventListener('dragover', (event) => {
    if (!state.drag || state.drag.path === node.path) return;
    event.preventDefault();
    event.stopPropagation();
    state.dragCopy = Boolean(event.ctrlKey || event.metaKey);
    event.dataTransfer.dropEffect = state.dragCopy ? 'copy' : 'move';
    const rect = el.getBoundingClientRect();
    const ratio = (event.clientY - rect.top) / rect.height;
    el.classList.remove('drop-in', 'drop-before', 'drop-after');
    const intoFolder = node.type === 'dir' && ratio > 0.22 && ratio < 0.78;
    if (node.type === 'dir' && intoFolder) {
      el.classList.add('drop-in');
      // 悬停在收起的文件夹上 0.6 秒自动展开，方便拖进更深的一层
      if (!state.expandTimer && state.collapsed.has(node.path)) {
        state.expandTimer = setTimeout(() => {
          state.expandTimer = null;
          if (el.classList.contains('drop-in') && state.collapsed.has(node.path)) el.click();
        }, 600);
      }
    } else {
      clearTimeout(state.expandTimer);
      state.expandTimer = null;
      el.classList.add(ratio < 0.5 ? 'drop-before' : 'drop-after');
    }
  });
  el.addEventListener('dragleave', () => {
    el.classList.remove('drop-in', 'drop-before', 'drop-after');
    clearTimeout(state.expandTimer);
    state.expandTimer = null;
  });
  el.addEventListener('contextmenu', (event) => {
    event.preventDefault();
    event.stopPropagation();
    openContextMenu(event.clientX, event.clientY, node);
  });
  el.addEventListener('drop', async (event) => {
    event.preventDefault();
    event.stopPropagation();
    const mode = el.classList.contains('drop-in') ? 'in'
      : el.classList.contains('drop-before') ? 'before' : 'after';
    const copy = Boolean(event.ctrlKey || event.metaKey) || Boolean(state.dragCopy);
    clearDropMarks();
    await handleDrop(node, parentPath, mode, copy);
  });
}

async function handleDrop(target, parentPath, mode, copy = false) {
  const drag = state.drag;
  if (!drag) return;
  try {
    if (mode === 'in' && target && target.type === 'dir') {
      if (copy) {
        if (drag.kind === 'dir') {
          await api('/folders/copy', {
            method: 'POST', body: JSON.stringify({ from: drag.path, to: target.path }),
          });
        } else {
          await api(`/docs/${encodeURIComponent(drag.id)}/copy`, {
            method: 'POST', body: JSON.stringify({ dir: target.path }),
          });
        }
        state.drag = null;
        state.dragCopy = false;
        await loadTree();
        notice('已复制一份到「' + target.name + '」。');
        return;
      }
      if (drag.kind === 'dir') {
        await api('/folders/move', {
          method: 'POST', body: JSON.stringify({ from: drag.path, to: target.path }),
        });
        pushUndo({ type: 'move-dir', from: drag.parent || '', to: target.path,
                   label: '移动文件夹「' + drag.file + '」' });
      } else {
        await api(`/docs/${encodeURIComponent(drag.id)}/move`, {
          method: 'POST', body: JSON.stringify({ dir: target.path }),
        });
        pushUndo({ type: 'move-doc', id: drag.id, from: drag.parent || '',
                   label: '移动「' + String(drag.file || '').replace(/\.md$/, '') + '」' });
      }
      state.drag = null;
      state.dragCopy = false;
      await loadTree();
      if (drag.kind === 'doc' && state.doc && state.doc.id === drag.id) await openDoc(drag.id);
      if (drag.kind === 'dir' && state.doc && (state.doc.path || '').startsWith(drag.path + '/')) {
        await openDoc(state.doc.id);
      }
      notice('已移动到「' + target.name + '」。');
      return;
    }
    if (copy) {
      if (drag.kind === 'doc') {
        await api(`/docs/${encodeURIComponent(drag.id)}/copy`, {
          method: 'POST', body: JSON.stringify({ dir: parentPath || '' }),
        });
      } else {
        await api('/folders/copy', {
          method: 'POST', body: JSON.stringify({ from: drag.path, to: parentPath || 'records' }),
        });
      }
      state.drag = null;
      state.dragCopy = false;
      await loadTree();
      notice('已复制一份。');
      return;
    }
    const targetDir = parentPath || '';
    const sourceDir = drag.parent || '';
    if (sourceDir !== targetDir) {
      // 拖到别的目录（含笔记根）的空白处：先真正移动，再排序
      if (drag.kind === 'dir') {
        await api('/folders/move', {
          method: 'POST', body: JSON.stringify({ from: drag.path, to: targetDir || 'records' }),
        });
        pushUndo({ type: 'move-dir', from: sourceDir, to: targetDir || 'records',
                   label: '移动文件夹「' + drag.file + '」' });
      } else {
        await api(`/docs/${encodeURIComponent(drag.id)}/move`, {
          method: 'POST', body: JSON.stringify({ dir: targetDir || 'records' }),
        });
        pushUndo({ type: 'move-doc', id: drag.id, from: sourceDir,
                   label: '移动「' + String(drag.file || '').replace(/\.md$/, '') + '」' });
      }
      if (drag.kind === 'doc' && state.doc && state.doc.id === drag.id) await openDoc(drag.id);
      if (drag.kind === 'dir' && state.doc && (state.doc.path || '').startsWith(drag.path + '/')) {
        await openDoc(state.doc.id);
      }
    }
    const container = document.querySelector(`.children[data-dir="${CSS.escape(parentPath || '')}"]`);
    if (!container) { state.drag = null; state.dragCopy = false; await loadTree(); return; }
    const items = Array.from(container.children).filter((x) => x.dataset.file);
    const names = items.map((x) => x.dataset.file).filter((n) => n !== drag.file);
    if (target && target.file) {
      const at = items.findIndex((x) => x.dataset.file === target.file);
      let insert = at < 0 ? names.length : at;
      if (mode === 'after') insert += 1;
      names.splice(insert, 0, drag.file);
    } else {
      names.push(drag.file);
    }
    state.drag = null;
    state.dragCopy = false;
    await api('/tree/order', {
      method: 'POST', body: JSON.stringify({ dir: parentPath || '', order: names }),
    });
    await loadTree();
  } catch (err) {
    state.drag = null;
    state.dragCopy = false;
    notice(failureText(err.code, err.message));
  }
}

function renderNodes(nodes, container, parentPath) {
  container.dataset.dir = parentPath || '';
  container.addEventListener('dragover', (event) => {
    if (!state.drag) return;
    event.preventDefault();
  });
  container.addEventListener('drop', async (event) => {
    if (event.target !== container || !state.drag) return;
    event.preventDefault();
    clearDropMarks();
    await handleDrop(null, parentPath, 'after');
  });

  nodes.forEach((node) => {
    if (node.type === 'dir') {
      const box = document.createElement('div');
      box.className = 'folder';
      box.dataset.path = node.path;

      const collapsed = state.collapsed.has(node.path);
      const row = document.createElement('div');
      row.className = 'folder-row' + (collapsed ? '' : ' open');
      // 待归类目录给数量 badge：只数真正还没归到目标的（status=pending），数字与内容一致
      const pendingCount = (node.name === '待归位' || node.name === '外部素材')
        ? (node.children || []).filter((c) => c.type === 'doc' && c.status === 'pending').length : 0;
      row.innerHTML = '<span class="twisty"><svg viewBox="0 0 12 12" aria-hidden="true">'
        + '<path d="M4 2.5L7.5 6 4 9.5"/></svg></span>'
        + `<span class="fname">${esc(node.name)}</span>`
        + (pendingCount ? `<span class="dir-badge" title="${pendingCount} 篇还没归类，点开可处理">${pendingCount}</span>` : '');
      box.appendChild(row);

      const children = document.createElement('div');
      children.className = 'children' + (collapsed ? ' hidden' : '');
      renderNodes(node.children || [], children, node.path);
      box.appendChild(children);

      row.onclick = () => {
        const hidden = children.classList.toggle('hidden');
        row.classList.toggle('open', !hidden);
        if (hidden) state.collapsed.add(node.path);
        else state.collapsed.delete(node.path);
      };
      attachDrag(row, node, parentPath);
      container.appendChild(box);
    } else {
      const el = document.createElement('div');
      const isOrphan = Boolean(node.orphan);
      el.className = 'node' + (isOrphan ? ' orphan' : '')
        + (state.doc && state.doc.id === node.id ? ' on' : '');
      // 状态点：琥珀＝还没归类，绿＝已归类。无声(silent)=不显示，别吵。
      // 孤儿（还没初始化的文件）不画状态点，它不是「还没归类」，是另一回事。
      const dot = isOrphan ? ''
        : (node.status === 'pending'
          ? '<span class="status-dot pending" title="还没归到目标，打开后点「选个目标」即可"></span>'
          : (node.status === 'placed'
            ? '<span class="status-dot placed" title="' + esc(goalTitle(node.target)) + '"></span>' : ''));
      const label = document.createElement('span');
      label.className = 'node-label';
      label.innerHTML = dot + esc(node.name)
        + (isOrphan ? '<span class="orphan-tag" title="这个文件还没初始化（可能是在 Obsidian 里直接新建的）">未初始化</span>' : '');
      el.appendChild(label);
      el.onclick = () => (isOrphan ? openAdopt(node) : openDoc(node.id));
      attachDrag(el, node, parentPath);
      container.appendChild(el);
    }
  });
}

async function loadTree() {
  const data = await api('/docs/tree');
  state.tree = data;
  const box = $('tree');
  box.innerHTML = '';
  box.removeAttribute('data-dir');
  renderNodes(data.children || [], box, '');
  refreshCoverage(); // 顺手刷新未探索徽章；失败静默，不打扰
}

/* ---------- P1 统计：速记 / 未探索提示 / 悬空概念。只报事实，判定权在用户 ---------- */

async function refreshCoverage() {
  const badge = $('cov-badge');
  if (!badge) return;
  try {
    const data = await api('/stats/coverage');
    state.coverage = data;
    if (data.on_single_line) {
      badge.textContent = `● ${data.active_days} 天只写了 1 条线`;
      badge.title = `最近 ${data.window} 天有 ${data.active_days} 天动过笔记，且只出现「${data.line}」一条线。点开看依据。`;
      badge.classList.remove('hidden');
    } else {
      badge.classList.add('hidden');
    }
  } catch (err) { badge.classList.add('hidden'); }
}

function openCoverage() {
  const c = state.coverage;
  if (!c) return;
  const el = form(`<label>未探索提示 · 纯统计，系统不替你判断</label>
    <div class="cov-detail">
      最近 ${c.window} 天：有 ${c.active_days} 天动过笔记
      ${c.active_days ? `（${c.days.map(esc).join('、')}）` : ''}，
      动过的线：${c.lines.length ? c.lines.map(esc).join('、') : '（无）'}。
      ${c.on_single_line
        ? `<p>这段时间只出现了「${esc(c.line)}」一条线。也许你正在聚焦 —— 也许有别的区域一直没碰。判断权在你。</p>`
        : '<p>没有单一聚焦信号。</p>'}
    </div>
    <div class="row"><button type="button" class="link" id="cov-close">关闭</button></div>`);
  el.querySelector('#cov-close').onclick = closeForm;
}

async function loadDanglingRow(doc) {
  const row = $('dangling-row');
  if (!row || !doc) return;
  try {
    const data = await api('/stats/dangling');
    const concepts = (data.concepts || []).filter((c) => !(doc.title || '').includes(c.word));
    if (!concepts.length) { row.classList.add('hidden'); return; }
    row.classList.remove('hidden');
    row.innerHTML = '<span class="dangling-label">反复出现的词（≥2 篇还没归类的笔记里提到）：</span>'
      + concepts.map((c, i) => `<span class="dangling-chip">${esc(c.word)} · ${c.docs} 篇`
        + `<button type="button" class="link dangling-make" data-i="${i}" title="新建一个目标，并把这篇归进去">建成目标</button></span>`).join('');
    row.querySelectorAll('.dangling-make').forEach((btn) => {
      btn.onclick = async () => {
        const c = concepts[Number(btn.dataset.i)];
        try {
          await api(`/records/${encodeURIComponent(doc.id)}/place`, {
            method: 'POST',
            body: JSON.stringify({ action: 'create', name: c.word }),
          });
          notice(`已新建目标「${c.word}」，这篇也归进去了。正文一个字没改。`);
          await loadGoalOptions();
          await loadTree();
          await openDoc(doc.id);
        } catch (err) { notice(failureText(err.code, err.message)); }
      };
    });
  } catch (err) { row.classList.add('hidden'); }
}

async function loadTodoBadge() {
  try {
    const data = await api('/todos');
    $('todo-badge').textContent = data.open.length;
  } catch (err) { /* 静默 */ }
}

function crumb(path, title) {
  // 顶栏窄，但至少要让当前文档名可见（多窗口时尤其需要）。完整路径在左侧树里高亮。
  const parts = String(path || '').split('/').filter(Boolean).slice(1, -1);
  const dir = parts.length ? parts[parts.length - 1] : '笔记';
  return dir + ' / ' + (title || '未命名');
}

/* 孤儿文件（用户在 Obsidian 里直接新建、还没有元数据）：
   点它只弹一个「补齐」按钮——补齐才写盘，不点就一个字节都不动。 */
async function openAdopt(node) {
  if (state.editing) {
    await flushSave();
    if (state.dirty) { notice('这篇还没保存成功，先别切走。'); return; }
  }
  let info = { path: node.path, name: node.name, preview: '' };
  try {
    const [orphans, goals] = await Promise.all([api('/docs/orphans'), api('/goals')]);
    state.goals = goals.goals || state.goals;
    const found = (orphans.orphans || []).find((o) => o.path === node.path);
    if (found) info = found;
  } catch (err) { /* 拿不到预览也不影响补齐 */ }
  const goals = (state.goals || []).map((g) =>
    `<option value="${esc(g.id)}">${esc(g.name)}</option>`).join('');
  const el = form(`<label>《${esc(info.name)}》还没初始化</label>
    <div class="adopt-note">
      这个文件没有元数据（多半是在 Obsidian 里直接新建的）。
      ${info.preview ? `开头写着：<span class="adopt-preview">${esc(info.preview)}</span>` : '目前是空文件。'}
      补齐只会在文件开头加一段元信息，<b>正文一个字都不会改</b>。
    </div>
    <div class="row">
      <input id="adopt-title" type="text" value="${esc(info.name)}" placeholder="标题">
      <select id="adopt-target"><option value="">先不归类</option>${goals}</select>
      <button type="button" class="btn accent" id="adopt-go">补齐</button>
      <button type="button" class="link" id="adopt-cancel">取消</button>
    </div>`);
  /* 补齐面板插在正文上方，明示它、并滚过去，避免用户以为点击没反应。 */
  el.classList.add('adopt-form');
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  el.querySelector('#adopt-cancel').onclick = closeForm;
  el.querySelector('#adopt-go').onclick = async () => {
    const title = el.querySelector('#adopt-title').value.trim();
    const target = el.querySelector('#adopt-target').value;
    try {
      const data = await api('/docs/adopt', {
        method: 'POST', body: JSON.stringify({ path: info.path, title, target }),
      });
      closeForm();
      await loadTree();
      await loadGoalOptions();
      await openDoc(data.id);
      notice('已补齐，正文一个字没改。');
    } catch (err) { notice(failureText(err.code, err.message)); }
  };
}

async function openDoc(id) {
  if (state.editing) {
    await flushSave();
    if (state.dirty) { notice('这篇还没保存成功，先别切走。'); return; }
    state.editing = false;
    setSaveState('');
  }
  try {
    const data = await api('/docs/' + encodeURIComponent(id));
    state.doc = data.doc;
    state.baseUpdated = data.doc.updated;
    state.anchor = null;
    try { localStorage.setItem('mirror.lastDocId', id); } catch (err) { /* 忽略 */ }
    document.title = (data.doc.title || '未命名') + ' · Second Brain';
    renderAnchorHint();
    notice('');
    closeForm();
    renderDoc();
    await loadTree();
    state.chat = await chatStore.loadRemote();
    state.chatError = '';
    state.chatRetry = null;
    $('chat-sub').textContent = chatSubText();
    renderChat();
  } catch (err) {
    notice(failureText(err.code, err.message));
  }
}

/* 目标名查询：树里的绿点和顶栏都用它，把抽象状态说成一句具体的话。 */
function goalTitle(id) {
  const goal = (state.goals || []).find((g) => g.id === id);
  return goal ? '已归到：' + goal.name : '已归类';
}

function renderDocTarget(doc) {
  const el = $('doc-target');
  if (!el) return;
  const goal = (state.goals || []).find((g) => g.id === doc.target);
  if (doc.status !== 'placed' || !goal) {
    el.textContent = '';
    el.title = '';
    el.classList.add('hidden');
    return;
  }
  el.textContent = '已归到：' + goal.name;
  el.title = '已归到目标「' + goal.name + '」';
  el.classList.remove('hidden');
}

function renderDoc() {
  const doc = state.doc;
  if (!doc) {
    $('doc-body').innerHTML = '<p class="empty">还没有文档。点左上角「＋笔记」写第一篇。</p>';
    $('doc-target').classList.add('hidden');
    return;
  }
  $('crumb').textContent = crumb(doc.path, doc.title);
  renderDocTarget(doc);
  const body = $('doc-body');
  body.innerHTML = renderMarkdown(doc.body);
  highlightCode(body);
  body.onclick = (event) => {
    if (state.editing) return;
    /* 点标题是「重命名」，不是「开始写正文」；标题不由正文编辑态改写。 */
    if (event.target.closest && event.target.closest('h1')) {
      event.stopPropagation();
      startRename('doc', doc.id);
      return;
    }
    if (event.target.matches('input[type=checkbox]')) return;
    if (event.target.tagName === 'A' || event.target.tagName === 'IMG') return;
    const link = event.target.closest ? event.target.closest('.wikilink') : null;
    if (link) {
      event.stopPropagation();
      const title = link.textContent.trim();
      const target = findDocByTitle(title);
      if (target && target.id !== doc.id) openDoc(target.id);
      else if (!target) notice(`库里还没有叫《${title}》的文档。`);
      return;
    }
    enterEdit(event.clientX, event.clientY);
  };
  body.querySelectorAll('input[type=checkbox]').forEach((box) => {
    box.onchange = async () => {
      try {
        const data = await api(`/docs/${encodeURIComponent(doc.id)}/todo`, {
          method: 'POST',
          body: JSON.stringify({ action: 'toggle', line: Number(box.dataset.line), checked: box.checked }),
        });
        box.nextElementSibling.classList.toggle('done', box.checked);
        if (data.updated) { state.doc.updated = data.updated; state.baseUpdated = data.updated; }
        toast(box.checked ? '已勾选并写回文档。' : '已取消勾选并写回文档。');
        loadTodoBadge();
      } catch (err) {
        box.checked = !box.checked;
        notice(failureText(err.code, err.message));
      }
    };
  });
  const pending = $('pending-actions');
  if (doc.status === 'pending') {
    pending.classList.remove('hidden');
    // 还没归类：横幅内直接给可见动作，不要求先跟 AI 说话。
    pending.innerHTML = '<span class="pending-dot" aria-hidden="true"></span>'
      + '<span class="pending-text">这篇还没归到目标 · 不影响使用，想好了再归类。</span>'
      + '<div class="pending-ops">'
      + '<button type="button" class="btn" id="pending-attach">选个目标</button>'
      + '<button type="button" class="btn" id="pending-create">新建目标</button>'
      + '<button type="button" class="link" id="pending-later">稍后再说</button>'
      + '<span class="pending-pick hidden" id="pending-pick"></span>'
      + '</div>'
      + '<div class="dangling-row hidden" id="dangling-row"></div>';
    bindPendingActions(doc);
    loadDanglingRow(doc);
  } else {
    pending.classList.add('hidden');
  }
  renderOutline();
}

/* 归类横幅：不跟 AI 说话也能完成「归到目标」这一步。
   复用已有的 /goals 与 /records/{id}/place（attach / create），不新增接口、不改正文。 */
async function bindPendingActions(doc) {
  const attach = $('pending-attach');
  const create = $('pending-create');
  const later = $('pending-later');
  const pick = $('pending-pick');
  if (!attach || !create || !later || !pick) return;

  const closePick = () => { pick.classList.add('hidden'); pick.innerHTML = ''; };
  const finish = async (label) => {
    try {
      await loadGoalOptions();
      await loadTree();
      await openDoc(doc.id);
      notice(label);
    } catch (err) { notice(failureText(err.code, err.message)); }
  };

  attach.onclick = async () => {
    try {
      const data = await api('/goals');
      const goals = data.goals || [];
      if (!goals.length) { notice('库里还没有目标，先点「新建目标」。'); return; }
      pick.classList.remove('hidden');
      pick.innerHTML = '<span class="pending-pick-title">选一个目标</span>'
        + '<select id="pending-goal">'
        + goals.map((g) => `<option value="${esc(g.id)}">${esc(g.name)}</option>`).join('')
        + '</select>'
        + '<button type="button" class="btn accent" id="pending-goal-go">归进去</button>'
        + '<button type="button" class="link" id="pending-goal-cancel">取消</button>';
      pick.querySelector('#pending-goal-cancel').onclick = closePick;
      pick.querySelector('#pending-goal-go').onclick = async () => {
        const target = pick.querySelector('#pending-goal').value;
        if (!target) return;
        try {
          await api(`/records/${encodeURIComponent(doc.id)}/place`, {
            method: 'POST', body: JSON.stringify({ action: 'attach', target }),
          });
          await finish('已归到目标。正文一个字没改。');
        } catch (err) { notice(failureText(err.code, err.message)); }
      };
    } catch (err) { notice(failureText(err.code, err.message)); }
  };

  create.onclick = () => {
    pick.classList.remove('hidden');
    pick.innerHTML = '<span class="pending-pick-title">新建一个目标，并把这篇归进去</span>'
      + '<input id="pending-line-name" type="text" placeholder="目标名，例如：完成季度复盘">'
      + '<button type="button" class="btn accent" id="pending-line-go">新建目标</button>'
      + '<button type="button" class="link" id="pending-line-cancel">取消</button>';
    const input = pick.querySelector('#pending-line-name');
    input.focus();
    pick.querySelector('#pending-line-cancel').onclick = closePick;
    const go = async () => {
      const name = input.value.trim();
      if (!name) { notice('先给这个目标起个名字。'); return; }
      try {
        await api(`/records/${encodeURIComponent(doc.id)}/place`, {
          method: 'POST', body: JSON.stringify({ action: 'create', name }),
        });
        await finish(`已新建目标「${name}」，这篇也归进去了。正文一个字没改。`);
      } catch (err) { notice(failureText(err.code, err.message)); }
    };
    pick.querySelector('#pending-line-go').onclick = go;
    input.onkeydown = (event) => { if (event.key === 'Enter') go(); };
  };

  later.onclick = () => {
    $('pending-actions').classList.add('hidden');
    notice('先放着。下次点开这篇，这条提示还在。');
  };
}

/* ---------- 长文大纲：从渲染后的标题生成，点击跳转 + 滚动高亮 ---------- */

function outlineHeadings() {
  return Array.from($('doc-body').querySelectorAll('h1,h2,h3,h4'));
}

function renderOutline() {
  const panel = $('outline');
  if (!panel) return;
  const heads = outlineHeadings();
  if (!state.doc || heads.length < 2) { panel.classList.add('hidden'); return; }
  const items = heads.map((h, index) => {
    const level = Number(h.tagName[1]) || 2;
    return `<button type="button" class="outline-item" data-index="${index}"
      style="padding-left:${6 + (level - 1) * 11}px" title="${esc(h.textContent || '')}">${esc((h.textContent || '').trim() || '（无标题）')}</button>`;
  }).join('');
  panel.innerHTML = `<div class="outline-head"><span>大纲 · ${heads.length} 节</span>
      <button type="button" id="outline-toggle" title="折叠 / 展开">−</button></div>
    <div class="outline-list">${items}</div>`;
  panel.classList.remove('hidden', 'collapsed');
  panel.querySelectorAll('.outline-item').forEach((btn) => {
    btn.onclick = () => {
      const target = outlineHeadings()[Number(btn.dataset.index)];
      if (target) target.scrollIntoView({ behavior: 'smooth', block: 'start' });
    };
  });
  const toggle = $('outline-toggle');
  if (toggle) toggle.onclick = () => {
    toggle.textContent = panel.classList.toggle('collapsed') ? '+' : '−';
  };
  positionOutline();
  updateOutlineActive();
}

function positionOutline() {
  const panel = $('outline');
  if (!panel || panel.classList.contains('hidden')) return;
  const main = document.querySelector('.main');
  const chat = document.querySelector('.chat');
  const app = document.querySelector('.app');
  if (main && main.getBoundingClientRect().width < 900) {
    panel.classList.add('narrow-hidden');
    return;
  }
  panel.classList.remove('narrow-hidden');
  const rightEdge = (chat && app && !app.classList.contains('chat-off'))
    ? chat.getBoundingClientRect().left : window.innerWidth;
  panel.style.right = Math.max(8, window.innerWidth - rightEdge + 14) + 'px';
}

function updateOutlineActive() {
  const panel = $('outline');
  if (!panel || panel.classList.contains('hidden')) return;
  let active = 0;
  outlineHeadings().forEach((h, index) => {
    if (h.getBoundingClientRect().top <= 150) active = index;
  });
  panel.querySelectorAll('.outline-item').forEach((btn, index) => {
    btn.classList.toggle('on', index === active);
  });
}

function bindOutline() {
  const main = document.querySelector('.main');
  if (main) main.addEventListener('scroll', () => updateOutlineActive(), { passive: true });
  window.addEventListener('resize', () => { positionOutline(); updateOutlineActive(); });
}

function form(html) {
  const el = $('inline-form');
  el.innerHTML = html;
  el.classList.remove('hidden');
  /* 面板打开时大纲浮层会压住表单右上区域，先收起。 */
  const outline = $('outline');
  if (outline) outline.classList.add('hidden');
  return el;
}

function closeForm() {
  $('inline-form').classList.add('hidden');
  renderOutline();
}

/* 命中词高亮：先转义再包 mark，含 <>& 的关键词不会被当成 HTML 解析 */
function highlightHit(text, q) {
  const s = String(text || '');
  if (!q) return esc(s);
  const lower = s.toLowerCase();
  const idx = lower.indexOf(q.toLowerCase());
  if (idx < 0) return esc(s);
  return esc(s.slice(0, idx)) + '<mark>' + esc(s.slice(idx, idx + q.length)) + '</mark>'
    + esc(s.slice(idx + q.length));
}

/* 搜索摘要清洗 Markdown 残标记：用户看到的是句子，不是 # / - [ ] 语法。 */
function cleanSnippet(text) {
  return String(text || '')
    .replace(/^#+\s*/gm, '')
    .replace(/^[-*+]\s+\[[ xX]\]\s*/gm, '')
    .replace(/^[-*+]\s+/gm, '')
    .replace(/^>\s?/gm, '')
    .replace(/[`*]/g, '')
    .replace(/\s+/g, ' ')
    .trim();
}

function openSearch() {
  const dirs = [];
  (function walk(nodes, prefix) {
    (nodes || []).forEach((node) => {
      if (node.type !== 'dir') return;
      const path = prefix ? prefix + '/' + node.name : node.name;
      dirs.push({ name: node.name, path });
      walk(node.children, path);
    });
  })(state.tree ? state.tree.children : [], '');
  let dirFilter = '';
  const el = form(`<label>搜索标题与正文（Ctrl+K）</label>
    <input id="search-input" type="text" placeholder="输入关键词">
    ${dirs.length ? `<div class="dir-filter-label">限定目录 <span id="dir-filter-current">当前：全库</span></div><div id="search-dirs"></div>` : ''}
    <div id="search-results"></div>`);
  const input = el.querySelector('#search-input');
  const dirBox = el.querySelector('#search-dirs');
  if (dirBox) {
    dirBox.innerHTML = dirs.map((d) =>
      `<button type="button" class="dir-chip" data-path="${esc(d.path)}">${esc(d.name)}</button>`).join('');
    dirBox.querySelectorAll('.dir-chip').forEach((chip) => {
      chip.onclick = () => {
        dirFilter = dirFilter === chip.dataset.path ? '' : chip.dataset.path;
        dirBox.querySelectorAll('.dir-chip').forEach((c) =>
          c.classList.toggle('on', c.dataset.path === dirFilter));
        const hint = el.querySelector('#dir-filter-current');
        if (hint) hint.textContent = dirFilter ? '当前：' + dirFilter : '当前：全库';
        run();
      };
    });
  }
  input.focus();
  let timer = null;
  const run = async () => {
    const q = input.value.trim();
    const box = el.querySelector('#search-results');
    if (!q) { box.innerHTML = ''; return; }
    try {
      const data = await api('/search?q=' + encodeURIComponent(q));
      const rows = (data.results || []).filter((r) =>
        !dirFilter || String(r.path || '').startsWith('records/' + dirFilter));
      const label = rows.length
        ? `<div class="backup-meta">找到 ${rows.length} 条</div>`
        : '<p class="empty">没有找到。换个词试试。</p>';
      box.innerHTML = label + rows.map((r) => `<div class="result-item" data-id="${esc(r.id)}">
          <div>${highlightHit(r.title, q)}</div>
          <div class="snippet">${highlightHit(cleanSnippet(r.snippet), q)}</div></div>`).join('');
      box.querySelectorAll('.result-item').forEach((item) => {
        item.onclick = () => { closeForm(); openDoc(item.dataset.id); };
      });
    } catch (err) { notice(failureText(err.code, err.message)); }
  };
  input.oninput = () => {
    clearTimeout(timer);
    timer = setTimeout(run, 200);
  };
}

/* 把 20261112-115713-422 说成人话，用户才能判断该恢复哪一版 */
function humanRevision(name) {
  const m = String(name || '').match(/^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/);
  if (!m) return esc(name);
  const now = new Date();
  const today = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}-${String(now.getDate()).padStart(2, '0')}`;
  const stamp = `${m[1]}-${m[2]}-${m[3]}`;
  return (stamp === today ? '今天' : stamp) + ` ${m[4]}:${m[5]} · 自动保存`;
}

async function openRevisions() {
  if (!state.doc) { notice('先选一篇文档。'); return; }
  if (state.editing) await exitEdit();
  try {
    const data = await api(`/docs/${encodeURIComponent(state.doc.id)}/revisions`);
    const revs = data.revisions || [];
    const el = form(`<label>历史版本（每次保存前自动快照，保留最近 50 版）</label>
      <div id="revision-list">${revs.length ? revs.map((r, i) => {
        const prev = revs[i + 1];
        const delta = (prev && r.lines != null && prev.lines != null)
          ? ` · 比上一版 ${r.lines - prev.lines >= 0 ? '+' : ''}${r.lines - prev.lines} 行` : '';
        return `<div class="revision-item"><span>${humanRevision(r.created)}${r.lines != null ? ` · ${r.lines} 行` : ''}${delta}</span>
         <span>${Math.max(1, Math.round(r.size / 1024))} KB</span>
         <button type="button" class="btn" data-name="${esc(r.name)}">查看</button>
         <button type="button" class="btn" data-restore="${esc(r.name)}">恢复</button></div>`;
      }).join('')
        : '<p class="empty">这篇还没有历史版本。改一次就会有一版。</p>'}</div>
      <div id="revision-preview"></div>`);
    el.querySelectorAll('[data-name]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          const rev = await api(`/docs/${encodeURIComponent(state.doc.id)}/revisions/${encodeURIComponent(btn.dataset.name)}`);
          const preview = el.querySelector('#revision-preview');
          preview.innerHTML = `<div class="revision-body">${esc(rev.body)}</div>
            <div class="row"><button type="button" class="btn" id="restore-btn">恢复这一版</button></div>`;
          preview.querySelector('#restore-btn').onclick = async () => {
            try {
              await api(`/docs/${encodeURIComponent(state.doc.id)}/revisions/${encodeURIComponent(btn.dataset.name)}/restore`, { method: 'POST' });
              closeForm();
              await openDoc(state.doc.id);
              toast('已恢复到该版本；恢复前的版本也进了历史。');
            } catch (err) { notice(failureText(err.code, err.message)); }
          };
        } catch (err) { notice(failureText(err.code, err.message)); }
      };
    });
    /* 列表里直接给「恢复」：不必先点开预览再找按钮 */
    el.querySelectorAll('[data-restore]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api(`/docs/${encodeURIComponent(state.doc.id)}/revisions/${encodeURIComponent(btn.dataset.restore)}/restore`, { method: 'POST' });
          closeForm();
          await openDoc(state.doc.id);
          toast('已恢复到该版本；恢复前的版本也进了历史。');
        } catch (err) { notice(failureText(err.code, err.message)); }
      };
    });
  } catch (err) { notice(failureText(err.code, err.message)); }
}

/* ---------- 全部待办：四象限排列 ----------
   判定权完全归用户：只认写在 Markdown 行尾的 #重要 / #紧急，
   这两个标签就是「依据」，随时可取消推翻；系统不做任何自动推断。 */

const QUADRANTS = [
  { key: 'iu', title: '重要且紧急', hint: '两个标签都打了' },
  { key: 'i',  title: '重要不紧急', hint: '只标了重要' },
  { key: 'u',  title: '紧急不重要', hint: '只标了紧急' },
  { key: 'n',  title: '不重要不紧急', hint: '未标注 / 两标都没打，默认落这里' },
];

/* 互斥分配：一条待办只进一个象限，总数恒等于待办总数 */
function quadrantIndex(t) {
  if (t.important && t.urgent) return 0;
  if (t.important) return 1;
  if (t.urgent) return 2;
  return 3;
}

/* 拖到某个象限 = 把两个标签一次写成该象限对应的状态 */
const QUADRANT_FLAGS = [
  { important: true, urgent: true },
  { important: true, urgent: false },
  { important: false, urgent: true },
  { important: false, urgent: false },
];

async function placeTodo(docId, line, flags, index) {
  try {
    await api(`/docs/${encodeURIComponent(docId)}/todo`, {
      method: 'POST',
      body: JSON.stringify({ action: 'place', line,
                             important: flags.important, urgent: flags.urgent }),
    });
    state.todoItems = (await api('/todos')).todos;
    renderAllTodos();
    notice('已拖到「' + QUADRANTS[index].title + '」 · 依据写回原文，随时可以拖回来。');
    await loadTodoBadge();
  } catch (err) {
    notice(failureText(err.code, err.message));
    renderAllTodos();
  }
}

function bindTodoDrag(box) {
  const quadrants = Array.from(box.querySelectorAll('.quadrant'));
  if (!quadrants.length) return;
  box.querySelectorAll('.todo-line').forEach((row) => {
    row.addEventListener('dragstart', (event) => {
      // 点在复选框 / 标签按钮 / 来源上时不启动拖拽，免得误操作
      if (event.target.closest('input, button, .todo-src, .todo-tag')) {
        event.preventDefault();
        return;
      }
      event.dataTransfer.effectAllowed = 'move';
      event.dataTransfer.setData('text/plain', JSON.stringify({
        doc: row.dataset.doc, line: Number(row.dataset.line),
      }));
      row.classList.add('dragging');
    });
    row.addEventListener('dragend', () => {
      row.classList.remove('dragging');
      quadrants.forEach((q) => q.classList.remove('drop-on'));
    });
  });
  quadrants.forEach((q, index) => {
    q.addEventListener('dragover', (event) => {
      event.preventDefault();
      event.dataTransfer.dropEffect = 'move';
      q.classList.add('drop-on');
    });
    q.addEventListener('dragleave', (event) => {
      if (!q.contains(event.relatedTarget)) q.classList.remove('drop-on');
    });
    q.addEventListener('drop', (event) => {
      event.preventDefault();
      q.classList.remove('drop-on');
      let payload;
      try { payload = JSON.parse(event.dataTransfer.getData('text/plain')); }
      catch (err) { return; }
      if (!payload || !payload.doc || !QUADRANT_FLAGS[index]) return;
      const item = (state.todoItems || []).find((t) =>
        t.doc_id === payload.doc && Number(t.line) === Number(payload.line));
      if (item && quadrantIndex(item) === index) return;  // 原地放下，不写盘
      placeTodo(payload.doc, Number(payload.line), QUADRANT_FLAGS[index], index);
    });
  });
}

function todoRowHtml(t) {
  const impFlag = `<button type="button" class="flag-btn${t.important ? ' on' : ''}"
      data-field="important" data-value="${t.important ? 0 : 1}"
      title="${t.important ? '取消' : '标为'}重要 · 依据会写回原文 #重要">重要</button>`;
  const urgFlag = `<button type="button" class="flag-btn${t.urgent ? ' on' : ''}"
      data-field="urgent" data-value="${t.urgent ? 0 : 1}"
      title="${t.urgent ? '取消' : '标为'}紧急 · 依据会写回原文 #紧急">紧急</button>`;
  return `<div class="todo-line ${t.checked ? 'done' : ''}" draggable="true"
      data-doc="${esc(t.doc_id)}" data-line="${t.line}" title="按住拖到别的象限；点小按钮也能改判定">
      <input type="checkbox" ${t.checked ? 'checked' : ''} title="标记完成">
      <span class="todo-text">${esc(t.text)}</span>
      <span class="todo-flags">${impFlag}${urgFlag}</span>
      <span class="todo-src" data-doc="${esc(t.doc_id)}" title="${esc(t.path)}">${esc(t.title)}</span>
    </div>`;
}

function todoView() {
  try { return localStorage.getItem('mirror.todoView') || 'quadrant'; } catch (err) { return 'quadrant'; }
}

function renderAllTodos() {
  const all = state.todoItems || [];
  const view = todoView();
  const openCount = all.filter((t) => !t.checked).length;
  const doneCount = all.length - openCount;
  const showDone = state.todoShowDone === true;
  /* 打开待办是为了看「要做什么」：默认只渲染未完成，已完成折叠起来。 */
  const items = showDone ? all : all.filter((t) => !t.checked);
  const el = form(`<label>全部待办（未完成 ${openCount} / 共 ${all.length} 条）</label>
    <div class="row todo-viewbar">
      <button type="button" class="btn${view === 'quadrant' ? ' accent' : ''}" data-view="quadrant">四象限</button>
      <button type="button" class="btn${view === 'group' ? ' accent' : ''}" data-view="group">按文档</button>
      <button type="button" class="btn" id="todo-add-btn" title="写入当前打开的文档">＋ 待办</button>
      ${doneCount ? `<button type="button" class="btn" id="todo-done-toggle">${showDone ? '收起已完成 ▴' : '已完成 ' + doneCount + ' 条 ▾'}</button>` : ''}
      <span class="todo-basis">依据：你自己在行尾标的 <code>#重要</code> / <code>#紧急</code>，可随时取消；系统不替你判定</span>
    </div>
    <div id="todo-add-slot"></div>
    <div id="all-todos"></div>`);
  const box = el.querySelector('#all-todos');
  const addSlot = el.querySelector('#todo-add-slot');

  el.querySelector('#todo-add-btn').onclick = () => {
    if (!state.doc) { notice('先在左侧打开一篇文档，待办才会写进它。'); return; }
    addSlot.innerHTML = `<div class="row"><input id="todo-add-text" type="text" placeholder="待办内容（写入《${esc(state.doc.title)}》）">
      <button type="button" class="btn" id="todo-add-save">添加</button></div>`;
    const input = addSlot.querySelector('#todo-add-text');
    input.focus();
    const save = async () => {
      try {
        await api(`/docs/${encodeURIComponent(state.doc.id)}/todo`, {
          method: 'POST',
          body: JSON.stringify({ action: 'add', text: input.value }),
        });
        addSlot.innerHTML = '';
        toast(`待办已写进《${state.doc.title}》。`);
        await loadTodoBadge();
        state.todoItems = (await api('/todos')).todos;
        renderAllTodos();
      } catch (err) { notice(failureText(err.code, err.message)); }
    };
    addSlot.querySelector('#todo-add-save').onclick = save;
    input.onkeydown = (event) => { if (event.key === 'Enter') save(); };
  };

  if (!all.length) {
    box.innerHTML = '<p class="empty">还没有待办。在文档里写 - [ ] 就会出现在这里。</p>';
  } else if (!items.length) {
    box.innerHTML = `<p class="empty">今天没有未完成的事项了。已完成的 ${doneCount} 条可以点上方展开。</p>`;
  } else if (view === 'quadrant') {
    box.innerHTML = '<div class="quadrant-grid">' + QUADRANTS.map((q, index) => {
      const allInQuad = all.filter((t) => quadrantIndex(t) === index);
      const list = allInQuad.filter((t) => !t.checked || showDone);
      return `<section class="quadrant q${index}">
        <div class="q-head"><span class="q-title">${q.title}</span>
          <span class="q-count">未完成 ${allInQuad.filter((t) => !t.checked).length} / 共 ${allInQuad.length}</span></div>
        <div class="q-hint">${q.hint}</div>
        <div class="q-list">${list.length ? list.map(todoRowHtml).join('')
          : '<p class="empty">这里没有未完成事项</p>'}</div>
      </section>`;
    }).join('') + '</div>';
  } else {
    const groups = {};
    items.forEach((t) => { (groups[t.title] = groups[t.title] || []).push(t); });
    box.innerHTML = Object.entries(groups).map(([title, list]) => `<div class="todo-group">
        <div class="group-title">${esc(title)}</div>${list.map(todoRowHtml).join('')}</div>`).join('');
  }

  const doneBtn = el.querySelector('#todo-done-toggle');
  if (doneBtn) doneBtn.onclick = () => { state.todoShowDone = !showDone; renderAllTodos(); };
  el.querySelectorAll('[data-view]').forEach((btn) => {
    btn.onclick = () => {
      try { localStorage.setItem('mirror.todoView', btn.dataset.view); } catch (err) { /* 忽略 */ }
      renderAllTodos();
    };
  });
  box.querySelectorAll('.todo-line input').forEach((input) => {
    input.onchange = async () => {
      const row = input.closest('.todo-line');
      try {
        await api(`/docs/${encodeURIComponent(row.dataset.doc)}/todo`, {
          method: 'POST',
          body: JSON.stringify({ action: 'toggle', line: Number(row.dataset.line), checked: input.checked }),
        });
        row.classList.toggle('done', input.checked);
        const cached = (state.todoItems || []).find((t) =>
          t.doc_id === row.dataset.doc && Number(t.line) === Number(row.dataset.line));
        if (cached) cached.checked = input.checked;
        toast(input.checked ? '已勾选并写回文档。' : '已取消勾选并写回文档。');
        await loadTodoBadge();
        renderAllTodos();
      } catch (err) {
        input.checked = !input.checked;
        notice(failureText(err.code, err.message));
      }
    };
  });
  box.querySelectorAll('.flag-btn').forEach((btn) => {
    btn.onclick = async () => {
      const row = btn.closest('.todo-line');
      const value = btn.dataset.value === '1';
      try {
        await api(`/docs/${encodeURIComponent(row.dataset.doc)}/todo`, {
          method: 'POST',
          body: JSON.stringify({ action: 'flag', line: Number(row.dataset.line),
                                field: btn.dataset.field, value }),
        });
        state.todoItems = (await api('/todos')).todos;
        renderAllTodos();
        notice((value ? '已标为' : '已取消') + (btn.dataset.field === 'important' ? '「重要」' : '「紧急」')
          + ' · 依据写回了原文档，随时可以改。');
        await loadTodoBadge();
      } catch (err) {
        notice(failureText(err.code, err.message));
      }
    };
  });
  box.querySelectorAll('.todo-src').forEach((src) => {
    src.onclick = () => { closeForm(); openDoc(src.dataset.doc); };
  });
  bindTodoDrag(box);
}

async function openAllTodos() {
  if (state.editing) await exitEdit();
  try {
    state.todoItems = (await api('/todos')).todos;
    renderAllTodos();
  } catch (err) { notice(failureText(err.code, err.message)); }
}

/* 导出入口在对话面板头部（与「清空」成对）；右侧对话区专注「写进正文」 */
function openChatExport() {
  const el = form(`<label>导出这篇文档的对话记录（下载到本机，不改正文）</label>
    <div class="row">
      <button type="button" class="btn" id="exp-md">导出 Markdown</button>
      <button type="button" class="btn" id="exp-json">导出 JSON</button>
      <button type="button" class="btn" id="exp-copy">复制全部文字</button>
    </div>
    <div id="exp-status" class="backup-meta"></div>`);
  const status = el.querySelector('#exp-status');
  const run = async (format) => {
    status.textContent = '正在处理…';
    await exportChat(format);
    status.textContent = format === 'copy' ? '已尝试复制到剪贴板。' : '已生成下载文件。';
  };
  el.querySelector('#exp-md').onclick = () => run('md');
  el.querySelector('#exp-json').onclick = () => run('json');
  el.querySelector('#exp-copy').onclick = () => run('copy');
}

async function loadGoalOptions() {
  await loadGoals();
  await loadTodoBadge();
}

function bindForm() {
  $('search-btn').onclick = openSearch;
  $('cov-badge').onclick = openCoverage;
  $('revisions-btn').onclick = openRevisions;
  $('all-todos-btn').onclick = openAllTodos;
  /* 设置入口与对话栏显隐解耦：收起右栏后仍可达。 */
  $('settings-btn').onclick = openSettings;

  $('new-note-btn').onclick = () => {
    const dir = state.doc ? state.doc.path.split('/').slice(1, -1).join('/') : '';
    newNoteIn(dir);
  };

  $('new-folder-btn').onclick = () => {
    const parent = state.doc ? state.doc.path.split('/').slice(1, -1).join('/') : '';
    newFolderIn(parent);
  };

  $('collapse-btn').onclick = () => {
    document.querySelectorAll('.folder').forEach((box) => {
      state.collapsed.add(box.dataset.path);
      const kids = box.querySelector('.children');
      if (kids) kids.classList.add('hidden');
      const row = box.querySelector('.folder-row');
      if (row) row.classList.remove('open');
    });
  };

  $('expand-btn').onclick = () => {
    state.collapsed.clear();
    document.querySelectorAll('.folder').forEach((box) => {
      const kids = box.querySelector('.children');
      if (kids) kids.classList.remove('hidden');
      const row = box.querySelector('.folder-row');
      if (row) row.classList.add('open');
    });
  };

  document.addEventListener('click', closeMenu);
  document.addEventListener('keydown', (event) => {
    if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      openSearch();
    }
    if ((event.metaKey || event.ctrlKey) && !event.shiftKey
        && event.key.toLowerCase() === 'z' && !state.editing) {
      const tag = (event.target.tagName || '').toLowerCase();
      if (tag !== 'input' && tag !== 'textarea') {
        event.preventDefault();
        undoLast();
      }
    }
    if (event.key === 'Escape') closeMenu();
  });
}

/* 左右分栏拖拽：宽度写进 --sidebar-w，双击复位，方向键微调，宽度存 localStorage */
function bindSplit(opts) {
  const app = document.querySelector('.app');
  const target = document.querySelector(opts.targetSelector);
  const bar = $(opts.barId);
  if (!app || !target || !bar) return;
  const { min, max, reset, invert } = opts;
  /* reserve 支持函数：左侧栏/对话面板互相挤占时，按对方实时宽度动态预留正文最小宽度 */
  const reserveOf = () => (typeof opts.reserve === 'function' ? opts.reserve() : opts.reserve);
  const clamp = (w) => Math.max(min, Math.min(max, Math.min(w, window.innerWidth - reserveOf())));
  const apply = (w, silent) => {
    app.style.setProperty(opts.varName, Math.round(w) + 'px');
    if (!silent) window.dispatchEvent(new CustomEvent('split-change', { detail: { source: opts.barId } }));
  };
  /* 联动钳制：对方拖宽/窗口缩小时把自己压回合法区间，且不再广播，避免死循环 */
  const reclamp = () => apply(clamp(current()), true);
  const current = () => target.getBoundingClientRect().width;
  const save = (w) => {
    try { localStorage.setItem(opts.storageKey, String(Math.round(w))); } catch (err) { /* 隐私模式忽略 */ }
  };

  try {
    const saved = parseInt(localStorage.getItem(opts.storageKey) || '', 10);
    if (saved > 0) apply(clamp(saved));
  } catch (err) { /* 忽略 */ }

  let startX = 0;
  let startW = 0;
  bar.addEventListener('pointerdown', (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    startX = event.clientX;
    startW = current();
    bar.classList.add('dragging');
    document.body.classList.add('resizing');
    try { bar.setPointerCapture(event.pointerId); } catch (err) { /* 忽略 */ }
  });
  bar.addEventListener('pointermove', (event) => {
    if (!bar.classList.contains('dragging')) return;
    const delta = (event.clientX - startX) * (invert ? -1 : 1);
    apply(clamp(startW + delta));
  });
  const finish = (event) => {
    if (!bar.classList.contains('dragging')) return;
    bar.classList.remove('dragging');
    document.body.classList.remove('resizing');
    save(current());
    try { if (event && bar.hasPointerCapture(event.pointerId)) bar.releasePointerCapture(event.pointerId); } catch (err) { /* 忽略 */ }
  };
  bar.addEventListener('pointerup', finish);
  bar.addEventListener('pointercancel', finish);
  bar.addEventListener('dblclick', () => { apply(reset); save(reset); });
  bar.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    const step = event.key === 'ArrowLeft' ? -16 : 16;
    const next = clamp(current() + step * (invert ? -1 : 1));
    apply(next);
    save(next);
  });
  window.addEventListener('resize', reclamp);
  window.addEventListener('split-change', (event) => {
    if (event.detail && event.detail.source === opts.barId) return;
    reclamp();
  });
}

function bindResizer() {
  /* 正文列保底宽度：低于它就钳制侧栏/对话面板，顶栏按钮不再被对话面板盖住 */
  const MAIN_MIN = 400;
  const side = document.querySelector('.sidebar');
  const chat = document.querySelector('.chat');
  bindSplit({ barId: 'resizer', targetSelector: '.sidebar', varName: '--sidebar-w',
              storageKey: 'mirror.sidebar', min: 200, max: 620, reset: 272, invert: false,
              reserve: () => MAIN_MIN + (chat ? chat.getBoundingClientRect().width : 0) });
  bindSplit({ barId: 'resizer-chat', targetSelector: '.chat', varName: '--chat-w',
              storageKey: 'mirror.chatWidth', min: 260, max: 620, reset: 320, invert: true,
              reserve: () => MAIN_MIN + (side ? side.getBoundingClientRect().width : 0) });
}

/* ---------- 右侧对话面板：只读当前文档，不写任何 .md ---------- */

const chatStore = {
  key() { return 'mirror.chat.' + (state.doc ? state.doc.id : '_none'); },
  docId() { return state.doc ? state.doc.id : '_none'; },
  load() {
    try {
      const list = JSON.parse(localStorage.getItem(this.key()) || '[]');
      return Array.isArray(list) ? list : [];
    } catch (err) { return []; }
  },
  /* 服务端是唯一权威；本地 localStorage 只当离线兜底 + 旧记录迁移源 */
  async loadRemote() {
    try {
      const data = await api('/chat/history?doc_id=' + encodeURIComponent(this.docId()));
      const remote = Array.isArray(data.messages) ? data.messages : [];
      const local = this.load();
      if (remote.length) return remote;
      if (local.length) {
        await api('/chat/history', {
          method: 'PUT',
          body: JSON.stringify({ doc_id: this.docId(), messages: local }),
        });
        return local;
      }
      return [];
    } catch (err) {
      return this.load();
    }
  },
  save(list, immediate) {
    const kept = (list || []).slice(-60);
    try { localStorage.setItem(this.key(), JSON.stringify(kept)); } catch (err) { /* 隐私模式忽略 */ }
    clearTimeout(this._timer);
    const body = JSON.stringify({ doc_id: this.docId(), messages: kept });
    const push = () => fetch('/chat/history', {
      method: 'PUT', headers: { 'Content-Type': 'application/json' }, body,
    }).catch(() => { /* 服务断开时本地仍有一份，下次启动会迁移 */ });
    if (immediate) push();
    else this._timer = setTimeout(push, 250);
  },
};

/* ---------- 文档主动提问：收件箱 ----------
   诚实前提：纯本地应用收不到推送，「主动」= 打开应用时问题已经在等你。
   生成失败（没配模型 / 素材不足）静默降级，收件箱为空，不打扰。 */

const ASK_STALE_HOURS = 20;

function renderInbox() {
  const box = $('chat-inbox');
  if (!box) return;
  const items = state.inbox || [];
  if (!items.length) { box.classList.add('hidden'); box.innerHTML = ''; return; }
  const byId = {};
  chatDocList().forEach((d) => { byId[d.id] = d; });
  box.classList.remove('hidden');
  box.innerHTML = '<div class="inbox-head">文档在问你 · ' + items.length
    + ' <span class="inbox-hint">点文档名先看原文，点「回答它」直接进对话</span></div>'
    + items.map((q) => {
      const meta = byId[q.doc_id] || null;
      const dir = meta && meta.path
        ? String(meta.path).replace(/^records\//, '').split('/').slice(0, -1).join(' / ') : '';
      const full = meta ? `${dir ? dir + ' · ' : ''}${meta.title}` : (q.doc_title || '');
      return `<div class="inbox-item" data-qid="${esc(q.id)}">
        <div class="inbox-q">${esc(q.question)}</div>
        ${q.reason ? `<div class="inbox-why">依据：${esc(q.reason)}</div>` : ''}
        <div class="inbox-meta">
          <button type="button" class="link inbox-open" data-doc="${esc(q.doc_id)}"
            title="打开《${esc(full)}》原文">${esc(q.doc_title || '某篇文档')}${dir ? `<span class="inbox-path"> · ${esc(dir)}</span>` : ''}</button>
        </div>
        <div class="inbox-actions">
          <button type="button" class="btn inbox-answer" data-qid="${esc(q.id)}">回答它</button>
          <button type="button" class="link inbox-drop" data-qid="${esc(q.id)}">忽略</button>
        </div>
      </div>`;
    }).join('');
  box.querySelectorAll('.inbox-answer').forEach((btn) => {
    btn.onclick = () => answerInbox(btn.dataset.qid);
  });
  box.querySelectorAll('.inbox-drop').forEach((btn) => {
    btn.onclick = () => dismissInbox(btn.dataset.qid);
  });
  box.querySelectorAll('.inbox-open').forEach((btn) => {
    btn.onclick = () => openDoc(btn.dataset.doc);
  });
  /* 树上同步标记：哪篇文档在等你回答，浏览树时一眼可见 */
  document.querySelectorAll('.tree .ask-badge').forEach((n) => n.remove());
  const asking = new Set(items.map((q) => q.doc_id).filter(Boolean));
  if (asking.size) {
    document.querySelectorAll('.tree .node[data-id]').forEach((node) => {
      if (!asking.has(node.dataset.id) || node.querySelector('.ask-badge')) return;
      const tag = document.createElement('span');
      tag.className = 'ask-badge';
      tag.textContent = '问';
      tag.title = '这篇文档在等你回答';
      node.appendChild(tag);
    });
  }
}

async function refreshInbox(autofill) {
  try {
    const data = await api('/questions/pending');
    state.inbox = Array.isArray(data.items) ? data.items : [];
    if (!state.inbox.length && autofill) {
      const last = Date.parse(data.last_generated || '') || 0;
      const stale = Date.now() - last > ASK_STALE_HOURS * 3600 * 1000;
      const canAsk = chatConfig && (chatConfig.mock || chatConfig.configured);
      if (stale && canAsk) {
        try {
          await api('/questions/generate', { method: 'POST', body: JSON.stringify({ doc_id: '' }) });
          const again = await api('/questions/pending');
          state.inbox = Array.isArray(again.items) ? again.items : [];
        } catch (err) { /* 素材不足 / 模型没配：收件箱静默为空 */ }
      }
    }
  } catch (err) {
    state.inbox = [];
  }
  renderInbox();
}

/* 「去回答」：问题以「文档先问的」身份进这篇文档的对话，你接话即可 */
async function answerInbox(qid) {
  const q = (state.inbox || []).find((x) => x.id === qid);
  if (!q) return;
  try {
    if (q.doc_id && (!state.doc || state.doc.id !== q.doc_id)) await openDoc(q.doc_id);
    state.chat = state.chat || [];
    state.chat.push({ role: 'assistant', content: q.question, asked: true });
    chatStore.save(state.chat, true);
    renderChat();
    await api('/questions/consume', { method: 'POST', body: JSON.stringify({ id: qid }) });
    state.inbox = (state.inbox || []).filter((x) => x.id !== qid);
    renderInbox();
    const input = $('chat-input');
    if (input) input.focus();
  } catch (err) {
    notice(failureText(err.code, err.message));
  }
}

async function dismissInbox(qid) {
  try {
    await api('/questions/consume', { method: 'POST', body: JSON.stringify({ id: qid }) });
  } catch (err) { /* 已被处理就算了 */ }
  state.inbox = (state.inbox || []).filter((x) => x.id !== qid);
  renderInbox();
}

/* 现场问：让当前打开的这篇开口 */
async function askNow(docId) {
  if (!chatConfig || (!chatConfig.mock && !chatConfig.configured)) {
    notice('还没配模型，配好之后它才能来问你。');
    openSettings();
    return;
  }
  notice('正在读这篇，看它有什么拿不准的…');
  try {
    const data = await api('/questions/generate', {
      method: 'POST', body: JSON.stringify({ doc_id: docId || (state.doc ? state.doc.id : '') }),
    });
    if (!data.questions || !data.questions.length) {
      notice((data.message || '它觉得没什么拿不准的。')
        + ' 再写 2–3 句你的判断就能问了；正文点一下就能接着写。');
      return;
    }
    await refreshInbox(false);
    notice('它有 ' + data.questions.length + ' 个问题在收件箱等你。');
  } catch (err) {
    notice(failureText(err.code, err.message));
  }
}

function chatSubText() {
  if (!state.doc) return '未打开文档';
  const length = String(state.doc.body || '').replace(/\s+/g, '').length;
  return `${state.doc.title || '未命名'} · 已读 ${length} 字`;
}

/* 流式过程中补丁段先别露出来，免得满屏 <<<PATCH>>> 标记 */
function streamVisible(text) {
  const raw = String(text || '');
  const i = raw.indexOf('<<<PATCH>>>');
  if (i < 0) return raw;
  return raw.slice(0, i).trim() + '\n\n（正在生成可插入正文…）';
}

function splitPatchLocal(text) {
  const raw = String(text || '');
  const i = raw.indexOf('<<<PATCH>>>');
  if (i < 0) return { reply: raw.trim(), patch: '' };
  const j = raw.indexOf('<<<END>>>', i + 11);
  return { reply: raw.slice(0, i).trim(),
           patch: raw.slice(i + 11, j > i ? j : undefined).trim() };
}

/* 只改最后一个气泡的文字，不整块重建 DOM，流式时不会闪、也不会打断选字 */
function updateStreamText(text) {
  const log = $('chat-log');
  const node = log ? log.querySelector('.msg.ai.streaming .body') : null;
  if (!node) return false;
  node.textContent = streamVisible(text);
  log.scrollTop = log.scrollHeight;
  return true;
}

function renderChat() {
  const log = $('chat-log');
  const list = state.chat || [];
  const canWrite = state.allowPatch !== false;
  if (!list.length && !state.chatError && !state.chatSending) {
    const needModel = chatConfig && !chatConfig.mock && !chatConfig.configured;
    log.innerHTML = '<p class="chat-empty">它读得到当前这篇：指出你写下的东西之间的偏差，'
      + '再问你一个问题。<br>不给建议，不替你决定；要不要写进文档，都由你点。</p>'
      + (needModel ? '<button type="button" class="btn" id="chat-setup">先填一个模型 →</button>'
        : (state.doc ? '<button type="button" class="link" id="chat-ask-now">让这篇文档问我一个问题 →</button>' : ''));
    if (needModel) {
      const setup = $('chat-setup');
      if (setup) setup.onclick = openSettings;
    } else {
      const ask = $('chat-ask-now');
      if (ask) ask.onclick = () => askNow(state.doc ? state.doc.id : '');
    }
    return;
  }
  let html = list.map((m, index) => {
    if (m.role === 'user') {
      const chips = (m.attach || []).map((title) => `<span class="msg-chip">${esc(title)}</span>`).join('');
      const head = chips ? `<div class="msg-chips">${chips}</div>` : '';
      return `<div class="msg user">${head}<div class="body">${esc(m.content)}</div>`
        + writeActionsHtml(m, index) + '</div>';
    }
    if (m.streaming) {
      if (!m.content) return '<div class="msg ai pending">正在读你这句话…</div>';
      return `<div class="msg ai streaming"><div class="body">${esc(streamVisible(m.content))}</div></div>`;
    }
    // 有草稿时操作条放在草稿卡片里，没有就直接挂在回答下面
    let block = `<div class="msg ai"><div class="body">${m.asked ? '<span class="asked-tag">文档先问的</span>' : ''}${esc(m.content)}</div>`
      + (Array.isArray(m.sources) && m.sources.length
        ? '<div class="msg-sources">来源：' + m.sources.map((s, i) =>
            `<a href="${esc(s.url || '')}" target="_blank" rel="noopener noreferrer" title="${esc(s.snippet || '')}">${i + 1}·${esc(s.title || '无标题')}</a>`).join('')
          + '</div>' : '')
      + (m.webNote ? `<div class="msg-webnote">${esc(m.webNote)}</div>` : '')
      + (m.patch ? '' : writeActionsHtml(m, index)) + '</div>';
    if (m.patch) {
      const count = m.patch.replace(/\s+/g, '').length;
      block += `<div class="patch" data-index="${index}">
        <div class="patch-head">模型给的正文草稿 · ${count} 字</div>
        <pre class="patch-body">${esc(m.patch)}</pre>
        <div class="patch-actions">
          ${canWrite ? `<button type="button" class="btn patch-insert" data-write="append" data-index="${index}">追加到文末</button>
          <button type="button" class="btn" data-write="insert" data-index="${index}">插入到这段后</button>
          <button type="button" class="btn" data-write="replace" data-index="${index}">替换选中段</button>
          <button type="button" class="btn" data-write="rehome" data-index="${index}">归到…</button>`
            : '<span class="patch-locked">只读对话 · 这段草稿不会写进文档</span>'}
          <button type="button" class="link patch-drop" data-index="${index}">丢弃</button>
        </div>
      </div>`;
    }
    if (m.rehome && !m.rehomed) {
      const is_new = m.rehome === 'new';
      block += `<div class="rehome" data-index="${index}">
        <div class="rehome-head">${is_new
          ? '库里的文章都不太对口——建议把这段话新建一篇'
          : `这段话和《${esc(m.rehome)}》聊的是同一件事`}</div>
        ${m.rehomeWhy ? `<div class="rehome-why">你写过：“${esc(m.rehomeWhy.excerpt)}”${m.rehomeWhy.date ? `（${esc(m.rehomeWhy.date)}）` : ''}</div>` : ''}
        <div class="rehome-actions">
          ${canWrite
            ? `<button type="button" class="btn rehome-apply" data-rehome="${esc(m.rehome)}" data-index="${index}">${is_new ? '新建一篇' : '归到这篇'}</button>`
            : '<span class="patch-locked">只读对话 · 写入已关闭</span>'}
          <button type="button" class="link rehome-drop" data-index="${index}">忽略</button>
        </div>
      </div>`;
    }
    if (m.rehomed) {
      block += `<div class="rehome done">${m.rehomed === 'new'
        ? '已新建一篇，打开它选个目标就行。'
        : `已归到《${esc(m.rehomed)}》文末 · Ctrl+Z 可撤回。`}</div>`;
    }
    return block;
  }).join('');
  if (state.chatError) {
    html += `<div class="msg err"><span>${esc(state.chatError)}</span>`
      + (state.chatRetry ? '<button type="button" class="btn" id="chat-retry">重试这次回答</button>' : '')
      + '</div>';
  }
  log.innerHTML = html;
  log.querySelectorAll('.write-toggle').forEach((btn) => {
    btn.onclick = () => {
      const menu = log.querySelector(`[data-menu="${btn.dataset.toggle}"]`);
      if (!menu) return;
      menu.classList.toggle('hidden');
      btn.textContent = menu.classList.contains('hidden') ? '写入正文 ▾' : '写入正文 ▴';
    };
  });
  log.querySelectorAll('.patch-insert').forEach((btn) => {
    btn.onclick = () => insertChatPatch(Number(btn.dataset.index));
  });
  log.querySelectorAll('[data-write]:not(.patch-insert)').forEach((btn) => {
    btn.onclick = () => writeToDoc(Number(btn.dataset.index), btn.dataset.write);
  });
  log.querySelectorAll('.patch-drop').forEach((btn) => {
    btn.onclick = () => dropChatPatch(Number(btn.dataset.index));
  });
  log.querySelectorAll('.rehome-apply').forEach((btn) => {
    btn.onclick = () => applyRehome(Number(btn.dataset.index), btn.dataset.rehome);
  });
  log.querySelectorAll('.rehome-drop').forEach((btn) => {
    btn.onclick = () => dropRehome(Number(btn.dataset.index));
  });
  const retry = $('chat-retry');
  if (retry) retry.onclick = () => {
    const pending = state.chatRetry;
    if (!pending || state.chatSending) return;
    state.chatRetry = null;
    state.chatError = '';
    requestReply(pending.attach, pending.allowPatch);
  };
  log.scrollTop = log.scrollHeight;
}

function dropChatPatch(index) {
  const item = (state.chat || [])[index];
  if (!item) return;
  delete item.patch;
  chatStore.save(state.chat, true);
  renderChat();
}

/* ---------- 对话结束后：把记录的内容写进正文（追加 / 插入 / 替换） ----------
   模型自己永远写不进去；要用户点一下才落盘，且每次保存前都有快照、可 Ctrl+Z 撤回。 */

const WRITE_MODES = {
  append: '追加到文末',
  insert: '插入到正文里选中的那一段之后',
  replace: '用这段内容替换正文里选中的那一段',
  rehome: '归到库里另一篇文章的末尾（自动追加）',
};

/* 对话写盘权限的唯一状态源。
   旧实现里 allowPatch 只控制「模型给不给草稿」，写盘按钮完全不受约束，
   于是「只读对话」名不副实。现在权限切换会重渲染，写盘入口全部随之关闭。 */
function applyChatPerm(value, rerender) {
  state.allowPatch = value !== 'read';
  try { localStorage.setItem('mirror.chatPerm', value); } catch (err) { /* 忽略 */ }
  const perm = $('chat-perm');
  if (perm) perm.value = value;
  const badge = $('chat-readonly');
  if (badge) badge.classList.toggle('hidden', state.allowPatch);
  if (rerender !== false) renderChat();
  return state.allowPatch;
}

function writeActionsHtml(item, index) {
  /* 用户自己的提问不挂写盘操作；只读对话下整条操作条不渲染。 */
  if (item.role === 'user' || state.allowPatch === false) return '';
  const names = { append: '追加', insert: '插入', replace: '替换', rehome: '归到…' };
  const writtenLabel = item.written === 'rehome'
    ? (item.rehomedTo ? '已归到《' + item.rehomedTo + '》' : '已归档') : null;
  return '<div class="msg-actions">'
    + `<button type="button" class="msg-act write-toggle" data-toggle="${index}">写入正文 ▾</button>`
    + `<span class="write-menu hidden" data-menu="${index}">`
    + Object.keys(WRITE_MODES).map((mode) =>
      `<button type="button" class="msg-act${item.written === mode ? ' written' : ''}"
        data-write="${mode}" data-index="${index}" title="${WRITE_MODES[mode]}">${names[mode]}</button>`).join('')
    + '</span>'
    + (writtenLabel ? '<span class="msg-act-label">' + esc(writtenLabel) + '</span>' : '')
    + '</div>';
}

/* 锚点：正文里最后选中的文字 / 光标所在的段落 */
function updateAnchor() {
  const body = $('doc-body');
  const sel = window.getSelection();
  if (!body || !sel || !sel.rangeCount || !body.contains(sel.anchorNode)) return;
  const selected = sel.toString().replace(/\s+/g, ' ').trim();
  let node = sel.anchorNode;
  if (node && node.nodeType === 3) node = node.parentElement;
  const block = node && node.closest
    ? node.closest('[data-line], p, li, h1, h2, h3, h4, blockquote, pre, td, th') : null;
  const blockText = block ? (block.textContent || '').replace(/\s+/g, ' ').trim() : '';
  const anchorText = (selected || blockText).slice(0, 120);
  if (!anchorText) return;
  state.anchor = { text: anchorText, kind: selected ? 'selection' : 'caret' };
  renderAnchorHint();
}

function renderAnchorHint() {
  const el = $('anchor-hint');
  if (!el) return;
  const anchor = state.anchor;
  if (!anchor || !state.doc) { el.classList.add('hidden'); return; }
  const short = anchor.text.slice(0, 16) + (anchor.text.length > 16 ? '…' : '');
  el.textContent = (anchor.kind === 'selection' ? '已选中：' : '光标段：') + short;
  el.title = '插入 / 替换的目标：' + anchor.text;
  el.classList.remove('hidden');
}

function bindAnchor() {
  let timer = null;
  document.addEventListener('selectionchange', () => {
    clearTimeout(timer);
    timer = setTimeout(updateAnchor, 160);
  });
}

/* 忽略 Markdown 标记做块匹配，避免 **加粗** / 表格管道影响定位 */
function normalizeBlock(text) {
  return String(text || '').replace(/[*_`#>|\[\]()\-\s:：，。、]/g, '').toLowerCase();
}

function locateBlock(body, anchorText) {
  const key = normalizeBlock(anchorText).slice(0, 24);
  if (key.length < 3) return -1;
  const blocks = body.split(/\n{2,}/);
  let hit = blocks.findIndex((block) => normalizeBlock(block).includes(key));
  if (hit < 0 && key.length > 8) {
    hit = blocks.findIndex((block) => normalizeBlock(block).includes(key.slice(0, 10)));
  }
  return hit;
}

async function writeToDoc(index, mode) {
  /* 只读对话：所有写盘入口（消息操作条、补丁卡片、历史消息）唯一硬拦截点。 */
  if (state.allowPatch === false) {
    notice('当前是「只读对话」，不会写入任何文档。想写入请先把权限切回「允许补充」。');
    return;
  }
  const item = (state.chat || [])[index];
  if (!item || !WRITE_MODES[mode]) return;
  // 归到另一篇：不需要当前文档，先选文章、再选小节（或文末）
  if (mode === 'rehome') {
    const text = String(item.patch || item.content || '').trim();
    if (!text) { notice('这条记录没有可写入的内容。'); return; }
    const list = chatDocList().filter((d) => d.id !== (state.doc ? state.doc.id : ''));
    if (!list.length) { notice('库里还没有别的文章可归。'); return; }
    openChatPop(list.map((d) => ({ title: d.title, sub: d.path, id: d.id })),
      (picked) => rehomePickTarget(picked.id, picked.title, text, item),
      '库里没有别的文章');
    return;
  }
  if (!state.doc) { notice('先打开一篇文档，再把这段写进去。'); return; }
  if (state.editing) {
    await exitEdit();
    if (state.dirty) { notice('这篇还有改动没保存，先处理保存再写入。'); return; }
  }
  // 模型给过草稿就优先用草稿（是可直接插入的正文），否则用这条记录的原话
  const text = String(item.patch || item.content || '').trim();
  if (!text) { notice('这条记录没有可写入的内容。'); return; }

  const base = String(state.doc.body || '').replace(/\s+$/, '');
  let next = '';
  let label = '';
  if (mode === 'append') {
    next = (base ? base + '\n\n' : '') + text + '\n';
    label = '追加到文末';
  } else {
    if (!state.anchor || !state.anchor.text) {
      notice('先在正文里点一下或选中一段，作为' + (mode === 'insert' ? '插入' : '替换') + '的位置。');
      return;
    }
    const blocks = base.split(/\n{2,}/);
    const hit = locateBlock(base, state.anchor.text);
    if (hit < 0) {
      notice('没在正文里找到刚选中的那段，请重新点一下正文再试。');
      return;
    }
    if (mode === 'replace') {
      blocks[hit] = text;
      label = '替换选中段落';
    } else {
      blocks.splice(hit + 1, 0, text);
      label = '插入到锚点段之后';
    }
    next = blocks.join('\n\n').replace(/\s+$/, '') + '\n';
  }

  try {
    const data = await api('/docs/' + encodeURIComponent(state.doc.id), {
      method: 'PUT',
      body: JSON.stringify({ content: next, base_updated: state.baseUpdated || state.doc.updated }),
    });
    state.doc.body = next;
    state.doc.updated = data.updated;
    state.baseUpdated = data.updated;
    if (data.previous_revision) {
      pushUndo({ type: 'edit', id: state.doc.id, rev: data.previous_revision,
                 label: '对话写入：' + label });
    }
    item.written = mode;
    if (item.patch) delete item.patch;   // 草稿落地后不再重复显示
    if (mode !== 'append') { state.anchor = null; renderAnchorHint(); }
    chatStore.save(state.chat, true);
    renderChat();
    renderDoc();
    await loadTree();
    await loadTodoBadge();
    $('chat-sub').textContent = chatSubText();
    const main = document.querySelector('.main');
    if (main && mode === 'append') main.scrollTo({ top: main.scrollHeight, behavior: 'smooth' });
    notice('已' + label + ' · 不满意按 Ctrl+Z 撤回。');
  } catch (err) {
    if (err.code === 'CONFLICT') { showConflict(err.message); return; }
    notice(failureText(err.code, err.message));
  }
}

/* 兼容旧入口：补丁卡片里的「追加到文末」 */
async function insertChatPatch(index) {
  await writeToDoc(index, 'append');
}

/* ---------- 归位：把聊天里的一段话写进库里另一篇文章（S1/S2 共用） ----------
   永远是追加你的原话、不改写已有正文；写盘前快照、可 Ctrl+Z 撤回。
   落点二选一：文末，或目标文档某个小节的末尾（读书笔记按章节归）。 */

/* 归到某小节末尾：定位标题块 → 找下一个同级/更高级标题前的位置插入。
   找不到小节返回 null（不写盘，让用户重选）。 */
function insertUnderHeading(body, headingText, text) {
  const blocks = body.split(/\n{2,}/);
  const key = normalizeBlock(headingText).slice(0, 24);
  if (key.length < 2) return null;
  const start = blocks.findIndex((block) => normalizeBlock(block).includes(key));
  if (start < 0) return null;
  const levelOf = (block) => {
    const m = block.match(/^#{1,6}\s/);
    return m ? m[0].length : 99;
  };
  const baseLevel = levelOf(blocks[start]);
  let end = blocks.length;
  for (let i = start + 1; i < blocks.length; i += 1) {
    if (levelOf(blocks[i]) <= baseLevel) { end = i; break; }
  }
  blocks.splice(end, 0, text);
  return blocks.join('\n\n').replace(/\s+$/, '') + '\n';
}

/* 第一步选完文章，第二步选落点：文末 / 某个小节 */
async function rehomePickTarget(docId, docTitle, text, item) {
  try {
    const target = await api('/docs/' + encodeURIComponent(docId));
    const doc = (target && target.doc) || target;
    const body = String((doc && doc.body) || '');
    const heads = body.split('\n').filter((line) => /^#{1,6}\s+\S/.test(line))
      .map((line) => line.replace(/^#+\s*/, '').trim()).slice(0, 30);
    if (!heads.length) {
      return await writeTextIntoDoc(docId, docTitle, text, null, doc, item);
    }
    const options = [{ title: '（追加到文末）', sub: docTitle, heading: null }]
      .concat(heads.map((h) => ({ title: h, sub: '归入这个小节的末尾', heading: h })));
    openChatPop(options, async (picked) => {
      const done = await writeTextIntoDoc(docId, docTitle, text, picked.heading, doc, item);
      if (done) {
        item.rehomed = docTitle;
        chatStore.save(state.chat, true);
        renderChat();
      }
    }, '这篇没有可归的小节');
    return true;
  } catch (err) {
    notice(failureText(err.code, err.message));
  }
}

async function writeTextIntoDoc(docId, docTitle, text, heading, docInfo, item) {
  try {
    const base = String((docInfo && docInfo.body) || '').replace(/\s+$/, '');
    let next = '';
    let label = '';
    if (!heading) {
      next = (base ? base + '\n\n' : '') + text + '\n';
      label = '追加到文末';
    } else {
      const placed = insertUnderHeading(base, heading, text);
      if (placed === null) {
        notice('没找到「' + heading + '」这个小节（可能刚被改名），请重新归一次。');
        return false;
      }
      next = placed;
      label = '归入「' + heading + '」小节';
    }
    const data = await api('/docs/' + encodeURIComponent(docId), {
      method: 'PUT',
      body: JSON.stringify({ content: next, base_updated: docInfo.updated }),
    });
    if (data.previous_revision) {
      pushUndo({ type: 'edit', id: docId, rev: data.previous_revision,
                 label: '对话归档：' + docTitle });
    }
    if (item) {
      item.written = 'rehome';
      item.rehomed = docTitle;
      item.rehomedTo = docTitle;
      if (item.patch) delete item.patch;
      chatStore.save(state.chat, true);
    }
    renderChat();
    await loadTree();
    await loadTodoBadge();
    const id = docId;
    notice('已' + label + '（' + docTitle + '）· 不满意按 Ctrl+Z 撤回。',
      { label: '打开', onClick: () => openDoc(id) });
    return true;
  } catch (err) {
    if (err.code === 'CONFLICT') { showConflict(err.message); return false; }
    notice(failureText(err.code, err.message));
    return false;
  }
}

/* 建议卡片「归到这篇 / 新建一篇」：归档的是你自己的原话（离这条回答最近的 user 消息） */
function rehomeSourceText(index) {
  const chat = state.chat || [];
  for (let i = index - 1; i >= 0; i -= 1) {
    if (chat[i].role === 'user' && String(chat[i].content || '').trim()) {
      return String(chat[i].content).trim();
    }
  }
  const self = String((chat[index] || {}).content || '').trim();
  return self;
}

/* 拿到归位建议后，取检索命中的原句当「理由」展示；失败静默，卡片照常 */
async function fetchRelatedWhy(index) {
  const item = (state.chat || [])[index];
  if (!item || !item.rehome || item.rehome === 'new') return;
  const q = rehomeSourceText(index).slice(0, 120);
  if (!q) return;
  try {
    const data = await api('/related?q=' + encodeURIComponent(q));
    const hits = data.passages || [];
    const hit = hits.find((p) => p.title === item.rehome) || hits[0];
    if (hit && hit.excerpt) {
      item.rehomeWhy = { excerpt: String(hit.excerpt).slice(0, 80), date: hit.date || '' };
      chatStore.save(state.chat, true);
      renderChat();
    }
  } catch (err) { /* 静默：没有理由行照常拍板 */ }
}

async function applyRehome(index, rehome) {
  if (state.allowPatch === false) {
    notice('当前是「只读对话」，不会写入任何文档。');
    return;
  }
  const item = (state.chat || [])[index];
  if (!item || item.rehomed) return;
  const text = rehomeSourceText(index);
  if (!text) { notice('这条回答前面没有你的原话，没法归档。'); return; }
  if (rehome === 'new') {
    const now = new Date();
    const first = text.split('\n')[0].replace(/^#+\s*/, '').trim();
    const title = first.slice(0, 20)
      || `对话归档 ${now.getMonth() + 1}月${now.getDate()}日`;
    try {
      const created = await api('/records', {
        method: 'POST',
        body: JSON.stringify({ title, content: text, stream: 'self', kind: 'thought' }),
      });
      item.rehomed = 'new';
      chatStore.save(state.chat, true);
      renderChat();
      await loadTree();
      const newId = created && created.id;
      notice(`已新建「${title}」——打开它选个目标就行。`, [
        { label: '打开', onClick: () => { if (newId) openDoc(newId); } },
        { label: '移到目录…', onClick: () => {
          if (!newId) return;
          const node = { id: newId, name: title };
          openMoveDoc(node);
        } },
      ]);
    } catch (err) {
      notice(failureText(err.code, err.message));
    }
    return;
  }
  const doc = findDocByTitle(rehome) || chatDocList().find((d) => d.title === rehome);
  if (!doc || !doc.id) {
    notice(`库里没找到《${rehome}》（可能已改名），用下面的「归到…」手动选一篇。`);
    return;
  }
  await rehomePickTarget(doc.id, rehome, text, item);
}

function dropRehome(index) {
  const item = (state.chat || [])[index];
  if (!item) return;
  delete item.rehome;
  chatStore.save(state.chat, true);
  renderChat();
}

/* ---------- 总结归档：把这段对话的关键内容整理成一篇笔记草稿 ----------
   草稿由模型生成，落不落盘、落到哪一篇，全部由你点按钮决定（快照 + Ctrl+Z 照旧）。 */
async function summarizeAndArchive() {
  if (state.chatSending) return;
  const msgs = (state.chat || [])
    .filter((m) => (m.role === 'user' || m.role === 'assistant')
      && !m.streaming && String(m.content || '').trim())
    .map((m) => ({ role: m.role, content: m.content }));
  if (msgs.length < 2) { notice('对话还没几句，没什么可总结的。'); return; }
  notice('正在总结这段对话的关键内容…');
  try {
    const data = await api('/chat/summarize', {
      method: 'POST',
      body: JSON.stringify({ doc_id: state.doc ? state.doc.id : '', messages: msgs }),
    });
    const draft = String((data && data.draft) || '').trim();
    if (!draft) { notice('总结是空的，没有可归档的内容。'); return; }
    // 归到哪由你选：当前文档排最前、新建一篇随时可选、全库文章都在列
    const options = [];
    if (state.doc) {
      options.push({ title: '《' + (state.doc.title || '当前文档') + '》',
        sub: '当前打开的这篇', id: state.doc.id });
    }
    options.push({ title: '（新建一篇）', sub: '总结落成新笔记，先进探索区', id: '__new__' });
    const rest = chatDocList()
      .filter((d) => !state.doc || d.id !== state.doc.id)
      .map((d) => ({ title: d.title, sub: d.path, id: d.id }));
    openChatPop(options.concat(rest), async (picked) => {
      if (picked.id === '__new__') {
        const first = draft.split('\n').map((line) => line.replace(/^#+\s*/, '').trim())
          .find(Boolean) || '';
        const now = new Date();
        const title = ('总结 ' + first).slice(0, 20)
          || `对话总结 ${now.getMonth() + 1}月${now.getDate()}日`;
        try {
          const created = await api('/records', {
            method: 'POST',
            body: JSON.stringify({ title, content: draft, stream: 'self', kind: 'thought' }),
          });
          const newId = created && created.id;
          notice(`已新建「${created.title || title}」进探索区。`,
            { label: '打开', onClick: () => { if (newId) openDoc(newId); } });
          await loadTree();
        } catch (err) {
          notice(failureText(err.code, err.message));
        }
        return;
      }
      // item 传空对象：总结不是某条消息的写入，复用同一套「选小节→写盘→快照」链路
      await rehomePickTarget(picked.id, picked.title, draft, {});
    }, '库里没有可归的文章');
  } catch (err) {
    notice(failureText(err.code, err.message));
  }
}

const CHAT_COMMANDS = [
  { title: '/补充', sub: '让模型给一段可直接插入的正文', patch: true,
    prompt: '基于这篇文档，补充一段可以直接插进去的正文。' },
  { title: '/追问', sub: '只提一个问题，不要补丁', patch: false,
    prompt: '只用一个问题，指出这篇里最需要我确认的地方。' },
  { title: '/找矛盾', sub: '指出前后不一致的两句话', patch: false,
    prompt: '这篇里有没有前后不一致的两句话？只引用原文，不要给建议。' },
];

function chatDocList() {
  const out = [];
  const walk = (nodes) => (nodes || []).forEach((node) => {
    if (node.type === 'dir') walk(node.children);
    else if (node.id) out.push({ id: node.id, title: node.name, path: node.path });
  });
  walk((state.tree || {}).children);
  return out;
}

function hideChatPop() {
  const pop = $('chat-pop');
  if (pop) pop.classList.add('hidden');
}

function openChatPop(items, onPick, emptyText) {
  const pop = $('chat-pop');
  if (!pop) return;
  if (!items.length) {
    pop.innerHTML = `<p class="chat-pop-empty">${esc(emptyText || '没有匹配项')}</p>`;
    pop.classList.remove('hidden');
    return;
  }
  pop.innerHTML = items.map((item, index) => `<button type="button" class="chat-pop-item" data-index="${index}">
      <span class="pi-title">${esc(item.title)}</span>
      ${item.sub ? `<span class="pi-sub">${esc(item.sub)}</span>` : ''}
    </button>`).join('');
  pop.classList.remove('hidden');
  pop.querySelectorAll('.chat-pop-item').forEach((btn) => {
    btn.onmousedown = (event) => event.preventDefault();
    btn.onclick = () => { hideChatPop(); onPick(items[Number(btn.dataset.index)]); };
  });
}

function renderChatChips() {
  const box = $('chat-chips');
  const list = state.attach || [];
  box.innerHTML = list.map((doc, index) => `<span class="chip"><span>${esc(doc.title)}</span>
      <button type="button" data-index="${index}" title="移除">×</button></span>`).join('');
  box.classList.toggle('hidden', !list.length);
  box.querySelectorAll('button').forEach((btn) => {
    btn.onclick = () => {
      state.attach.splice(Number(btn.dataset.index), 1);
      renderChatChips();
    };
  });
}

function addChatAttach(doc) {
  if (!state.attach) state.attach = [];
  if (state.attach.some((item) => item.id === doc.id)) return;
  if (state.attach.length >= 5) { notice('最多附加 5 篇文档当上下文。'); return; }
  state.attach.push({ id: doc.id, title: doc.title });
  renderChatChips();
}

/* ---------- 拖文件进对话面板：入库外部素材 + 自动附加，AI 帮你整理 ---------- */

function bindChatDrop() {
  const panel = $('chat');
  const hasFiles = (event) => Array.from(event.dataTransfer?.types || []).includes('Files');
  const highlight = (on) => panel.classList.toggle('drop-on', on);

  panel.addEventListener('dragover', (event) => {
    if (!hasFiles(event)) return;
    event.preventDefault();
    event.dataTransfer.dropEffect = 'copy';
    highlight(true);
  });
  panel.addEventListener('dragleave', (event) => {
    if (!panel.contains(event.relatedTarget)) highlight(false);
  });

  panel.addEventListener('drop', async (event) => {
    if (!event.dataTransfer || !event.dataTransfer.files.length) return;
    event.preventDefault();
    highlight(false);
    await ingestFiles(event.dataTransfer.files);
  });
}

async function ingestFiles(fileList) {
  const all = Array.from(fileList);
  const list = all.slice(0, 5);
  const errs = [];
  const okItems = [];
  for (const file of list) {
    try {
      const data = await new Promise((resolve, reject) => {
        const reader = new FileReader();
        reader.onload = () => resolve(reader.result);
        reader.onerror = () => reject(new Error('读不出文件内容'));
        reader.readAsDataURL(file);
      });
      const out = await api('/ingest/file', {
        method: 'POST', body: JSON.stringify({ name: file.name, data }),
      });
      okItems.push(out);
      addToAttach({ id: out.id, title: out.title });
    } catch (err) {
      errs.push(`${file.name}：${failureText(err.code, err.message)}`);
    }
  }
  const notes = [];
  if (all.length > 5) notes.push('一次最多 5 个，多余的没收');
  if (okItems.length) {
    notes.push(`${okItems.length} 篇已入库（外部素材，之后再归类）并附到对话`);
    if (okItems.some((o) => o.kind === 'image')) notes.push('图片 AI 看不了内容，只存档');
    if (okItems.some((o) => o.truncated)) notes.push('有文件超过 2 万字，只收了前 2 万字');
    notes.push('直接说「帮我整理一下」就行');
  }
  if (errs.length) notes.push('失败：' + errs.join('；'));
  if (notes.length) notice(notes.join('。') + '。');
}

function chatSendingUI(on) {
  state.chatSending = on;
  const btn = $('chat-send');
  if (!btn) return;
  btn.classList.toggle('sending', on);
  btn.disabled = false;
  btn.querySelector('.icon-send').classList.toggle('hidden', on);
  btn.querySelector('.icon-stop').classList.toggle('hidden', !on);
  btn.title = on ? '停止' : '发送（Enter）';
}

async function sendChat() {
  const input = $('chat-input');
  const text = input.value.trim();
  if (!text || state.chatSending) return;
  const attach = (state.attach || []).slice();
  input.value = '';
  input.style.height = '';
  state.chatError = '';
  state.chatRetry = null;
  state.chat.push({ role: 'user', content: text, attach: attach.map((doc) => doc.title) });
  chatStore.save(state.chat, true);
  state.attach = [];
  renderChatChips();
  await requestReply(attach.map((doc) => doc.id), state.allowPatch !== false);
}

/* SSE 流式：边收边渲染；失败保留原话并给「重试」，不用整段重打 */
async function requestReply(attachIds, allowPatch) {
  if (state.chatSending) return;
  const snapshot = (state.chat || [])
    .filter((m) => (m.role === 'user' || m.role === 'assistant') && !m.streaming && m.content)
    .map((m) => ({ role: m.role, content: m.content }));
  if (!snapshot.length || snapshot[snapshot.length - 1].role !== 'user') {
    state.chatError = '没有可发送的问题。';
    renderChat();
    return;
  }
  const message = { role: 'assistant', content: '', streaming: true };
  state.chat.push(message);
  state.chatSending = true;
  state.chatError = '';
  state.chatRetry = null;
  chatSendingUI(true);
  renderChat();

  const controller = new AbortController();
  state.chatAbort = controller;
  let streamed = '';
  let finished = false;
  try {
    const response = await fetch('/chat/stream', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      signal: controller.signal,
      body: JSON.stringify({
        doc_id: state.doc ? state.doc.id : '',
        attach: attachIds || [],
        allow_patch: !!allowPatch,
        web: !!state.web,
        messages: snapshot,
      }),
    });
    if (!response.ok || !response.body) {
      let code = 'NETWORK';
      let detail = '服务没有返回流式响应';
      try {
        const body = await response.json();
        code = (body.error && body.error.code) || code;
        detail = (body.error && body.error.message) || detail;
      } catch (err) { /* 非 JSON 错误体 */ }
      throw { code, message: detail };
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });
      let cut;
      while ((cut = buffer.indexOf('\n\n')) >= 0) {
        const frame = buffer.slice(0, cut);
        buffer = buffer.slice(cut + 2);
        const line = frame.split('\n').find((item) => item.startsWith('data:'));
        if (!line) continue;
        let event;
        try { event = JSON.parse(line.slice(5).trim()); } catch (err) { continue; }
        if (event.type === 'delta') {
          streamed += event.text;
          message.content = streamed;
          if (!updateStreamText(streamed)) renderChat();
        } else if (event.type === 'error') {
          throw { code: event.code || 'LLM_UNAVAILABLE',
                  message: event.message || '模型不可用' };
        } else if (event.type === 'done') {
          message.content = event.reply || streamed;
          if (event.patch) message.patch = event.patch;
          if (event.rehome) {
            message.rehome = event.rehome;
            fetchRelatedWhy(state.chat.indexOf(message));
          }
          if (Array.isArray(event.sources) && event.sources.length) message.sources = event.sources;
          if (event.web_note) message.webNote = event.web_note;
          finished = true;
        }
      }
    }
    if (!finished && !streamed.trim()) {
      throw { code: 'LLM_UNAVAILABLE', message: '模型没有返回内容' };
    }
  } catch (err) {
    if (controller.signal.aborted) {
      message.content = (streamed || '').trim() || '（已停止，这次没有收到完整回答）';
      state.chatError = '已停止这次回答。';
    } else {
      state.chat.pop();  // 只撤掉没写完的回答，自己那句话原样留着
      const friendly = err && err.code === 'LLM_UNAVAILABLE'
        ? (err.message || '模型不可用') + ' · 点输入框上方的模型名可打开设置。'
        : failureText(err && err.code, err && err.message);
      state.chatError = friendly;
      state.chatRetry = { attach: attachIds || [], allowPatch: !!allowPatch };
    }
  } finally {
    if (message.streaming) {
      if (!finished && !controller.signal.aborted) {
        const parts = splitPatchLocal(message.content);
        message.content = parts.reply;
        if (parts.patch && allowPatch) message.patch = parts.patch;
      }
      delete message.streaming;
    }
    state.chatAbort = null;
    state.chatSending = false;
    chatSendingUI(false);
    chatStore.save(state.chat, true);
    renderChat();
  }
}

async function exportChat(format) {
  const docId = state.doc ? state.doc.id : '_none';
  if (format === 'copy') {
    const text = (state.chat || []).map((m) =>
      (m.role === 'user' ? '我' : '模型') + '：' + (m.content || '')).join('\n\n');
    try {
      await navigator.clipboard.writeText(text);
      notice('对话文字已复制到剪贴板。');
    } catch (err) {
      notice('浏览器不允许直接复制，请用 Markdown 导出。');
    }
    return;
  }
  try {
    const data = await api('/chat/export?doc_id=' + encodeURIComponent(docId)
      + '&format=' + encodeURIComponent(format));
    const blob = new Blob([data.content], {
      type: format === 'json' ? 'application/json;charset=utf-8' : 'text/markdown;charset=utf-8',
    });
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = data.filename;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    notice('已导出 ' + data.filename);
  } catch (err) {
    notice(failureText(err.code, err.message));
  }
}


/* ---------- 模型设置（本机 config.json，Key 不回显） ---------- */
let chatConfig = null;

function updateModelLabel() {
  const el = $('chat-model');
  if (!el) return;
  if (!chatConfig) { el.textContent = '未配置模型'; el.classList.add('warn'); return; }
  el.textContent = chatConfig.model || '未配置模型';
  el.classList.toggle('warn', !chatConfig.mock && !chatConfig.configured);
}

async function loadChatConfig() {
  try {
    chatConfig = await api('/config');
  } catch (err) {
    chatConfig = null;
  }
  updateModelLabel();
  return chatConfig;
}

function renderModelList() {
  const box = $('model-list');
  const cfg = chatConfig || { models: [], active: '' };
  const rows = [];
  if (cfg.env_override) {
    rows.push(`<div class="model-item on"><div class="mi-main">
      <div class="mi-label">环境变量指定的模型</div>
      <div class="mi-meta">MIRROR_LLM_API_KEY / MODEL 优先级最高，下面的选择暂时不生效</div>
    </div></div>`);
  }
  if (!cfg.models.length) {
    rows.push('<div class="model-item"><div class="mi-main"><div class="mi-meta">还没有模型，填下面的表单加一个</div></div></div>');
  }
  cfg.models.forEach((item) => {
    const on = item.id === cfg.active;
    rows.push(`<div class="model-item${on ? ' on' : ''}" data-id="${esc(item.id)}">
      <div class="mi-main">
        <div class="mi-label">${esc(item.label)}${on ? ' <span class="mi-tag">当前</span>' : ''}</div>
        <div class="mi-meta">${esc(item.model)} · ${esc(item.base_url)}</div>
      </div>
      ${item.has_key ? '' : '<span class="mi-tag warn">缺 Key</span>'}
      ${on ? '' : `<button type="button" class="btn" data-use="${esc(item.id)}">启用</button>`}
      <button type="button" class="btn" data-del="${esc(item.id)}">删除</button>
    </div>`);
  });
  box.innerHTML = rows.join('');
  box.querySelectorAll('[data-use]').forEach((btn) => {
    btn.onclick = async () => {
      try {
        chatConfig = await api('/config/active', { method: 'POST', body: JSON.stringify({ id: btn.dataset.use }) });
        renderModelList(); updateModelLabel(); notice('已切换模型。');
      } catch (err) { notice(failureText(err.code, err.message)); }
    };
  });
  box.querySelectorAll('[data-del]').forEach((btn) => {
    let armed = false;
    btn.onclick = async () => {
      if (!armed) { armed = true; btn.textContent = '确认删除'; setTimeout(() => { armed = false; btn.textContent = '删除'; }, 3000); return; }
      try {
        chatConfig = await api('/config/models/remove', { method: 'POST', body: JSON.stringify({ id: btn.dataset.del }) });
        renderModelList(); updateModelLabel(); notice('模型已删除。');
      } catch (err) { notice(failureText(err.code, err.message)); }
    };
  });
}

function openSettings() {
  $('settings-modal').classList.remove('hidden');
  loadChatConfig().then(renderModelList);
  if (chatConfig && chatConfig.config_path) $('settings-path').textContent = chatConfig.config_path;
}

function closeSettings() {
  $('settings-modal').classList.add('hidden');
  $('mf-status').textContent = '';
}

function settingsStatus(text, kind) {
  const el = $('mf-status');
  el.textContent = text || '';
  el.className = 'model-status' + (kind ? ' ' + kind : '');
}

function bindSettings() {
  const modal = $('settings-modal');
  if (!modal) return;
  $('settings-close').onclick = closeSettings;
  $('settings-mask').onclick = closeSettings;
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && !modal.classList.contains('hidden')) closeSettings();
  });

  $('mf-save').onclick = async () => {
    const payload = {
      label: $('mf-label').value.trim(),
      base_url: $('mf-base').value.trim(),
      model: $('mf-model').value.trim(),
      api_key: $('mf-key').value.trim(),
    };
    if (!payload.model) { settingsStatus('模型名必填，例如 deepseek-chat', 'err'); return; }
    settingsStatus('保存中…');
    try {
      chatConfig = await api('/config/models', { method: 'POST', body: JSON.stringify(payload) });
      const saved = (chatConfig.models || []).find((item) =>
        item.model === payload.model && (!payload.label || item.label === payload.label));
      if (saved) {
        chatConfig = await api('/config/active', { method: 'POST', body: JSON.stringify({ id: saved.id }) });
      }
      $('mf-key').value = '';
      renderModelList();
      updateModelLabel();
      settingsStatus('已保存并启用', 'ok');
      notice('模型已保存。发一句话试试。');
    } catch (err) {
      settingsStatus(failureText(err.code, err.message), 'err');
    }
  };

  $('mf-test').onclick = async () => {
    if (!chatConfig || !chatConfig.active) { settingsStatus('先保存一个模型再测试', 'err'); return; }
    settingsStatus('测试中…');
    try {
      const result = await api('/config/test', { method: 'POST', body: JSON.stringify({ id: chatConfig.active }) });
      settingsStatus(result.message || '连接成功', 'ok');
    } catch (err) {
      settingsStatus(err.message || '连接失败', 'err');
    }
  };
}

function bindChat() {
  const input = $('chat-input');
  const send = $('chat-send');
  if (!input || !send) return;
  state.chat = chatStore.load();
  state.chatSending = false;
  state.chatError = '';
  state.attach = [];
  renderChatChips();
  renderChat();

  const grow = () => {
    input.style.height = 'auto';
    input.style.height = Math.min(input.scrollHeight, 160) + 'px';
  };

  // @ 引用文档、/ 调用指令
  const detectTrigger = () => {
    const value = input.value;
    const at = value.lastIndexOf('@');
    const slash = value.lastIndexOf('/');
    const atActive = at >= 0 && !/\s/.test(value.slice(at + 1));
    const slashActive = slash >= 0 && !/\s/.test(value.slice(slash + 1))
      && (slash === 0 || /\s/.test(value[slash - 1]));
    if (atActive) {
      const query = value.slice(at + 1).trim().toLowerCase();
      const docs = chatDocList().filter((doc) => !query || doc.title.toLowerCase().includes(query));
      openChatPop(docs.map((doc) => ({ ...doc, sub: doc.path.replace(/^records\//, '') })),
        (doc) => {
          addChatAttach(doc);
          input.value = value.slice(0, at);
          grow();
          input.focus();
        }, '没有匹配的文档');
      return true;
    }
    if (slashActive) {
      const query = value.slice(slash + 1).trim();
      const cmds = CHAT_COMMANDS.filter((cmd) => !query || cmd.title.includes(query));
      openChatPop(cmds, (cmd) => {
        input.value = cmd.prompt;
        if (cmd.patch) applyChatPerm('patch');
        grow();
        input.focus();
      }, '没有匹配的指令');
      return true;
    }
    hideChatPop();
    return false;
  };

  input.addEventListener('input', () => {
    grow();
    if (!detectTrigger()) hideChatPop();
  });
  input.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') { hideChatPop(); return; }
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault();
      hideChatPop();
      sendChat();
    }
  });
  input.addEventListener('blur', () => setTimeout(hideChatPop, 120));

  send.onclick = () => {
    if (state.chatSending) {
      if (state.chatAbort) {
        state.chatAbort.abort();
        state.chatAbort = null;
      }
      return;
    }
    sendChat();
  };

  $('chat-plus').onclick = () => {
    const active = input.value;
    openChatPop(chatDocList().map((doc) => ({ ...doc, sub: doc.path.replace(/^records\//, '') })),
      (doc) => { addChatAttach(doc); input.focus(); }, '还没有可引用的文档');
    if (!active) input.focus();
  };

  // 权限：允许补充 / 只读
  const perm = $('chat-perm');
  let savedPerm = 'patch';
  try { savedPerm = localStorage.getItem('mirror.chatPerm') || 'patch'; } catch (err) { /* 忽略 */ }
  perm.value = savedPerm;
  applyChatPerm(savedPerm, false);
  perm.onchange = () => {
    const allowed = applyChatPerm(perm.value);
    notice(allowed ? '这次对话允许模型给「可插入正文」。'
      : '只读对话：不会写入任何文档，写盘入口已全部关闭。');
  };

  // 当前模型
  loadChatConfig();
  $('chat-model').title = '点这里设置 / 切换模型';
  $('chat-model').onclick = openSettings;

  // 语音输入：三级策略——配了转写接口就录音发本机接口；否则用浏览器自带识别；
  // 都没有就诚实说（按钮常显，点一下才知道自己处在哪一级，不假装有）。
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const mic = $('chat-mic');
  mic.classList.remove('hidden');
  let recognizer = null;
  let recorder = null;
  mic.onclick = async () => {
    if (recorder) { recorder.stop(); recorder = null; return; }
    if (recognizer) { recognizer.stop(); recognizer = null; mic.classList.remove('on'); return; }
    const sttReady = chatConfig && chatConfig.stt && chatConfig.stt.available;
    if (sttReady) {
      recorder = await startRecording(mic);
      if (recorder) notice('正在录音…说完了再点一下麦克风停止。');
      return;
    }
    if (SR) {
      recognizer = new SR();
      recognizer.lang = 'zh-CN';
      recognizer.interimResults = false;
      mic.classList.add('on');
      recognizer.onresult = (event) => {
        const text = event.results[0][0].transcript || '';
        input.value += text;
        input.dispatchEvent(new Event('input'));
        input.focus();
      };
      recognizer.onerror = (event) => {
        const why = {
          'not-allowed': '麦克风权限被拒绝：地址栏麦克风图标里放行后重试。',
          'no-speech': '没听到你说话，靠近点再试一次。',
          'network': '浏览器语音识别连不上（Chrome 把音频发往 Google 服务器，国内网络常常不通）。两个办法：换 Edge 直接用；或在 config.json 的 "stt".api_key 里填上硅基流动的 Key（接口已预置好，模型免费），重启即可用录音转写。',
        };
        notice(why[event.error] || '语音输入不可用：' + event.error);
      };
      recognizer.onend = () => { recognizer = null; mic.classList.remove('on'); };
      try { recognizer.start(); } catch (err) { recognizer = null; mic.classList.remove('on'); }
      return;
    }
    notice('这个浏览器没有自带语音识别：config.json 加 "stt" 节（OpenAI 兼容的 /audio/transcriptions）即可用录音转写。');
  };

  async function startRecording(micBtn) {
    let stopped = false;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const ctx = new (window.AudioContext || window.webkitAudioContext)();
      const source = ctx.createMediaStreamSource(stream);
      // 录成 16k 单声道 WAV（PCM16）：国内各家转写接口（硅基流动等）都直接吃 wav，
      // 不碰 Chrome 私有的 webm 编码，也不需要服务端再转格式。
      const proc = ctx.createScriptProcessor(4096, 1, 1);
      const chunks = [];
      proc.onaudioprocess = (e) => { chunks.push(new Float32Array(e.inputBuffer.getChannelData(0))); };
      source.connect(proc);
      proc.connect(ctx.destination);
      const finish = async () => {
        if (stopped) return;
        stopped = true;
        try {
          proc.disconnect(); source.disconnect();
          stream.getTracks().forEach((t) => t.stop());
          ctx.close();
        } catch (err) { /* 忽略 */ }
        micBtn.classList.remove('rec');
        const blob = encodeWav(chunks, ctx.sampleRate);
        if (blob.size < 800) { notice('没录到内容。'); return; }
        notice('正在转写…');
        try {
          const data = await api('/stt', {
            method: 'POST',
            body: JSON.stringify({ data: await blobToDataUrl(blob) }),
          });
          input.value += String((data && data.text) || '');
          input.dispatchEvent(new Event('input'));
          input.focus();
          notice('已转成文字，检查一下再发送。');
        } catch (err) {
          notice(failureText(err.code, err.message));
        }
      };
      micBtn.classList.add('rec');
      return { stop: finish };
    } catch (err) {
      notice('麦克风没拿到：地址栏的麦克风图标里放行权限后重试。');
      return null;
    }
  }

  function encodeWav(chunks, sampleRate) {
    const total = chunks.reduce((n, c) => n + c.length, 0);
    const flat = new Float32Array(total);
    let off = 0;
    for (const c of chunks) { flat.set(c, off); off += c.length; }
    const ratio = Math.max(1, Math.round(sampleRate / 16000));
    const n = Math.floor(total / ratio);
    const pcm = new Int16Array(n);
    for (let i = 0; i < n; i++) {
      let sum = 0;
      for (let k = 0; k < ratio; k++) sum += flat[i * ratio + k];
      pcm[i] = Math.max(-32768, Math.min(32767, Math.round((sum / ratio) * 32767)));
    }
    const header = new ArrayBuffer(44);
    const view = new DataView(header);
    const tag = (p, s) => { for (let i = 0; i < s.length; i++) view.setUint8(p + i, s.charCodeAt(i)); };
    const dataLen = pcm.length * 2;
    tag(0, 'RIFF'); view.setUint32(4, 36 + dataLen, true); tag(8, 'WAVE'); tag(12, 'fmt ');
    view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true);
    view.setUint32(24, 16000, true); view.setUint32(28, 32000, true);
    view.setUint16(32, 2, true); view.setUint16(34, 16, true);
    tag(36, 'data'); view.setUint32(40, dataLen, true);
    return new Blob([header, pcm.buffer], { type: 'audio/wav' });
  }

  function blobToDataUrl(blob) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(new Error('录音读取失败'));
      reader.readAsDataURL(blob);
    });
  }

  let armed = false;
  let armTimer = null;
  $('chat-clear').onclick = () => {
    const btn = $('chat-clear');
    if (!armed) {
      armed = true;
      btn.textContent = '再点一次';
      armTimer = setTimeout(() => { armed = false; btn.textContent = '清空'; }, 3000);
      return;
    }
    clearTimeout(armTimer);
    armed = false;
    btn.textContent = '清空';
    state.chat = [];
    state.chatError = '';
    chatStore.save(state.chat, true);
    renderChat();
    notice('这段对话已清空（本机与服务端的记录一起清掉，不动文档）。');
  };

  $('chat-export').onclick = () => openChatExport();
  const webBtn = $('chat-web');
  if (webBtn) {
    webBtn.onclick = () => {
      state.web = !state.web;
      webBtn.classList.toggle('active', state.web);
      webBtn.setAttribute('aria-pressed', state.web ? 'true' : 'false');
      if (state.web && chatConfig && chatConfig.search && !chatConfig.search.available) {
        notice('联网搜索还没配好：config.json 加 "search" 节（provider 可用 bing 免 Key），重启后生效；这次发送会按未联网处理。');
      }
    };
  }
  const sumBtn = $('chat-summarize');
  if (sumBtn) sumBtn.onclick = () => summarizeAndArchive();
  bindChatDrop();

  const app = document.querySelector('.app');
  try {
    if (localStorage.getItem('mirror.chatClosed') === '1') app.classList.add('chat-off');
  } catch (err) { /* 忽略 */ }
  $('chat-btn').onclick = () => {
    const closed = app.classList.toggle('chat-off');
    try { localStorage.setItem('mirror.chatClosed', closed ? '1' : '0'); } catch (err) { /* 忽略 */ }
    if (!closed) { renderChat(); input.focus(); }
    positionOutline();
  };
}

async function init() {
  bindForm();
  bindResizer();
  bindWysiwyg();
  bindOutline();
  bindAnchor();
  bindChat();
  bindSettings();
  loadUndo();
  try {
    await loadGoals();
    await loadTree();
    await loadTodoBadge();
    // 优先恢复上次打开的文档；找不到时再回落第一篇（保留避免 /docs/undefined 的兜底）。
    let lastId = '';
    try { lastId = localStorage.getItem('mirror.lastDocId') || ''; } catch (err) { /* 忽略 */ }
    const nodes = document.querySelectorAll('.tree .node[data-id]');
    const remembered = lastId && document.querySelector(`.tree .node[data-id="${CSS.escape(lastId)}"]`);
    const first = nodes.length ? nodes[0] : null;
    const targetId = remembered ? lastId : (first ? first.dataset.id : '');
    if (targetId) await openDoc(targetId);
  } catch (err) {
    notice(failureText(err.code, err.message));
  }
  await loadChatConfig();
  refreshInbox(true);
}

init();

