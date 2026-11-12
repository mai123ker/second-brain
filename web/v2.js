'use strict';

const $ = (id) => document.getElementById(id);
const ESC_MAP = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g, (c) => ESC_MAP[c]);

const state = {
  messages: [], knowledge: [], actions: [], counts: {}, model: {}, situation: '',
  planResult: null, tab: 'chat', docId: '', docTitle: '个人使用说明', closed: new Set(),
  booted: false, docLoaded: false, reflectFor: '', cheer: null, voiceUsed: false,
};

async function api(path, options) {
  const opts = Object.assign({ headers: { 'Content-Type': 'application/json' } }, options || {});
  const res = await fetch(path, opts);
  let payload;
  try { payload = await res.json(); } catch (err) { throw new Error('服务没有返回有效数据'); }
  if (!payload.ok) throw new Error((payload.error && payload.error.message) || '请求失败');
  return payload.data;
}

let toastTimer = null;
function toast(message) {
  const el = $('toast');
  el.textContent = message;
  el.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), 3600);
}

/* ---------- 渲染 ---------- */

function renderBadges() {
  const badge = $('model-badge');
  badge.textContent = state.model.mock ? '演示模式 · 规则整理'
    : (state.model.configured ? '模型 · ' + (state.model.name || '已配置') : '规则版 · 未配模型');
  badge.classList.toggle('off', !state.model.configured);
  $('kb-count').textContent = (state.counts.knowledge || 0) + ' 条';
  $('todo-count').textContent = (state.counts.todos_open || 0) + ' 待办';
  $('kb-foot').textContent = (state.counts.frames || 0) + ' 条框架 · '
    + (state.counts.unabsorbed || 0) + ' 条待吸收 · '
    + (state.counts.sources || 0) + ' 份原文归档（不可变）';
  $('loop-hint').textContent = '闭环：投喂 / 对话 → 提炼入库 → 处境梳理 → 待办 → 完成激励 → 新想法回补 → 下次相似问题调用框架';
  if (state.cheer) {
    $('cheer').innerHTML = '<div>' + esc(state.cheer.line) + '</div>'
      + '<span class="quote">' + esc(state.cheer.quote.text) + '　— ' + esc(state.cheer.quote.from) + '</span>';
    $('cheer').classList.remove('hidden');
  }
}

function renderKB() {
  const q = $('kb-q').value.trim().toLowerCase();
  const items = state.knowledge.filter((k) => !q || (
    (k.title || '') + ' ' + (k.statement || '') + ' ' + (k.domain || '')
  ).toLowerCase().includes(q));
  const order = ['制度', '法律', '经济运行', '教育', '组织', '用户增长', '通用认知'];
  const domains = order.filter((d) => items.some((k) => (k.domain || '通用认知') === d));
  const html = domains.map((domain) => {
    const list = items.filter((k) => (k.domain || '通用认知') === domain);
    const frames = list.filter((k) => k.type === 'frame').length;
    const closed = state.closed.has(domain) ? ' closed' : '';
    const rows = list.map((k) => {
      const flags = (k.conflicts || []).length
        ? '<span class="pill warn">待对照</span>'
        : (k.status === 'unabsorbed' ? '<span class="pill">未吸收</span>' : '');
      return '<div class="kb-item' + (k.id === state.docId ? ' on' : '') + '" data-id="' + esc(k.id) + '">'
        + '<div class="kb-line"><span class="kb-dot ' + (k.type === 'frame' ? '' : 'point') + '"></span>'
        + '<span class="kb-title">' + esc(k.title) + '</span>' + flags + '</div>'
        + '</div>';
    }).join('');
    return '<div class="kb-group' + closed + '" data-domain="' + esc(domain) + '">'
      + '<div class="kb-group-head"><svg class="kb-caret" viewBox="0 0 10 10" fill="none" stroke="currentColor" stroke-width="1.2"><path d="M3 2l4 3-4 3"/></svg>'
      + esc(domain) + '<span class="kb-num">' + list.length + (frames ? ' · 框架 ' + frames : '') + '</span></div>'
      + '<div class="kb-items">' + rows + '</div></div>';
  }).join('');
  $('kb-list').innerHTML = html || '<div class="empty">知识库还是空的。贴一段文字、投喂一份 PDF，或直接说话。</div>';
}

const CHANGE_LABEL = { created: '新增', merged: '并入', conflict: '待对照', skipped: '跳过' };

function ledgerHtml(ledger) {
  if (!ledger) return '';
  const changes = ledger.changes || [];
  const removed = ledger.removed || {};
  const rows = changes.length ? changes.map((c) =>
    '<div class="ledger-row"><span class="pill ' + esc(c.action) + '">'
    + esc(CHANGE_LABEL[c.action] || c.action) + '</span>'
    + '<span class="ledger-title">' + esc(c.title || '') + '</span>'
    + '<span class="muted">' + esc(c.domain || '') + '</span></div>').join('')
    : '<div class="ledger-row muted">没有可沉淀的内容；原文只留在归档里。</div>';
  return '<div class="ledger"><div class="ledger-head"><span>'
    + (ledger.mode === 'llm' ? '模型整理' : '规则整理') + '</span><span class="muted">去口语 '
    + (removed.filler || 0) + ' · 去情绪 ' + (removed.emotion || 0)
    + ' · 去重 ' + (removed.duplicate || 0) + '</span></div>'
    + (ledger.summary ? '<div class="ledger-summary">' + esc(ledger.summary) + '</div>' : '')
    + rows + '</div>';
}

function renderChat() {
  if (!state.messages.length) {
    $('chat-log').innerHTML = '<div class="empty">说一段想法，或投喂资料。<br>系统会去掉口语、重复和情绪，只把判断留进左边的知识库。</div>';
    return;
  }
  const hasTalk = state.messages.some((m) => m.role === 'user' || m.role === 'assistant');
  const html = state.messages.map((m) => {
    if (m.role === 'user') {
      return '<div class="msg user"><div class="bubble">' + esc(m.text) + '</div></div>';
    }
    if (m.role === 'feed') {
      const count = ((m.ledger && m.ledger.changes) || []).length;
      return '<details class="feed-line"><summary>' + esc(m.text)
        + (count ? ' · ' + count + ' 条变更' : '') + '</summary>' + ledgerHtml(m.ledger) + '</details>';
    }
    return '<div class="msg assistant"><div class="bubble">' + esc(m.text) + '</div>'
      + ledgerHtml(m.ledger) + '</div>';
  }).join('');
  $('chat-log').innerHTML = html
    + (hasTalk ? '' : '<div class="empty">在下面说一句，系统会整理进左边的知识库。</div>');
  $('chat-log').scrollTop = $('chat-log').scrollHeight;
}

function renderPlanSide() {
  const result = state.planResult;
  const problems = $('problems');
  const similar = $('similar');
  if (!result) { problems.innerHTML = ''; similar.innerHTML = ''; return; }
  similar.innerHTML = (result.similar || []).length
    ? '<div class="section-title">遇到过的相似处境</div>' + result.similar.map((h) =>
      '<div class="mini-card hit">相似度 ' + h.score + ' · ' + esc(h.created || '')
      + '<div>' + esc(h.text) + '</div>'
      + (h.pitfalls || []).map((p) => '<div class="mini-note">要避开：' + esc(p) + '</div>').join('')
      + '</div>').join('')
    : '';
  problems.innerHTML = (result.problems || []).length
    ? '<div class="section-title">我面临什么问题</div>' + result.problems.map((p) =>
      '<div class="mini-card">' + esc(p.text)
      + (p.note ? '<div class="mini-note">' + esc(p.note) + '</div>' : '') + '</div>').join('')
    : '';
}

function renderTodos() {
  const items = (state.actions || []).slice().reverse();
  if (!items.length) {
    $('todos').innerHTML = '<div class="empty">还没有待办。在上方写下当前处境，点「梳理」。</div>';
    return;
  }
  $('todos').innerHTML = '<div class="section-title">接下来该做什么</div>' + items.map((a) => {
    const done = a.status === 'done';
    const reflect = state.reflectFor === a.id
      ? '<div class="reflect-box"><textarea id="reflect-input" rows="2" placeholder="执行中有新想法？写下来会回补进知识库"></textarea>'
        + '<div class="row"><button class="btn accent" data-submit-reflect="' + esc(a.id) + '">回补进知识库</button>'
        + '<button class="link" data-close-reflect>先不写</button></div></div>'
      : '';
    return '<div class="todo ' + (done ? 'done' : '') + '" data-id="' + esc(a.id) + '">'
      + '<div class="todo-head">'
      + (done ? '<span class="muted">✓</span>'
        : '<input type="checkbox" data-complete="' + esc(a.id) + '" title="标记完成">')
      + '<span class="todo-title">' + esc(a.title) + '</span></div>'
      + (a.why ? '<div class="todo-why">为什么：' + esc(a.why) + '</div>' : '')
      + (!done && a.next_action ? '<div class="todo-why">下一步：' + esc(a.next_action) + '</div>' : '')
      + (a.guard ? '<div class="todo-guard">' + esc(a.guard) + '</div>' : '')
      + (done && a.encouragement ? '<div class="todo-encourage">' + esc(a.encouragement) + '</div>' : '')
      + reflect + '</div>';
  }).join('');
}

function renderAll() {
  renderBadges(); renderKB(); renderChat(); renderTodos(); renderPlanSide();
}

async function refresh() {
  try {
    const [st, kb, acts] = await Promise.all([
      api('/v2/state'), api('/v2/knowledge'), api('/v2/actions'),
    ]);
    state.counts = st.counts; state.model = st.model;
    state.knowledge = kb.items; state.actions = acts.items; state.situation = acts.situation;
    const history = await api('/v2/chat/history');
    state.messages = history.messages;
    if (!state.booted) {
      $('situation').value = state.situation || '';
      state.booted = true;
    }
    renderAll();
    if (!state.docLoaded) { state.docLoaded = true; openManual(); }
  } catch (err) { toast('加载失败：' + err.message); }
}

/* ---------- 中间长文区 ---------- */

function setDoc(title, html) {
  state.docTitle = title;
  $('center-title').textContent = title;
  $('detail-view').innerHTML = html;
  $('detail-view').scrollTop = 0;
  $('clear-view').classList.toggle('hidden', title === '个人使用说明');
}

function showManual() { openManual(); }

async function reloadDoc() {
  if (state.docId) await openKnowledge(state.docId);
  else await openManual();
}

function mdLite(text) {
  let skip = false;
  return esc(text || '').split('\n').map((line) => {
    if (/^##\s+/.test(line)) {
      const head = line.replace(/^##\s+/, '').trim();
      skip = head === '来源';
      return skip ? '' : '<h3>' + head + '</h3>';
    }
    if (skip) return '';
    if (/^-\s+/.test(line)) return '<div class="md-li">· ' + line.replace(/^-\s+/, '') + '</div>';
    if (!line.trim()) return '';
    return '<p>' + line + '</p>';
  }).join('');
}

function clip(text, limit) {
  const value = String(text || '').replace(/\s+/g, ' ').trim();
  return value.length > limit ? value.slice(0, limit - 1) + '…' : value;
}

async function openKnowledge(id) {
  try {
    const data = await api('/v2/knowledge/' + encodeURIComponent(id));
    const k = data.item;
    state.docId = k.id;
    renderKB();
    const sources = (k.sources_detail || []).map((s) =>
      '<li>' + esc(s.title) + ' · ' + esc(s.created) + ' · ' + esc(s.kind) + '</li>').join('');
    const conflicts = (k.conflicts || []).length
      ? '<h3>待对照</h3><div class="muted">与 ' + k.conflicts.length + ' 条既有框架可能不一致，尚未覆盖原框架。</div>' : '';
    const notice = k.status === 'unabsorbed'
      ? '<div class="notice"><div class="notice-line"><span class="notice-dot"></span>这条还没吸收 · 不影响使用，想好了再标记。</div>'
        + '<div class="notice-actions"><button class="link" id="absorb-btn" data-id="' + esc(k.id) + '">标记为已吸收</button>'
        + '<button class="link" id="absorb-later">稍后再说</button></div></div>'
      : '';
    setDoc((k.domain || '通用认知') + ' · ' + (k.type === 'frame' ? '框架' : '知识点'),
      notice + '<div class="detail-card">'
      + '<div class="doc-meta">' + esc(k.domain || '') + ' · '
      + esc(k.status === 'absorbed' ? '已吸收' : '尚未吸收') + ' · ' + (k.sources || []).length + ' 个来源</div>'
      + '<h1 class="doc-title">' + esc(k.title) + '</h1>'
      + '<div class="doc-body">' + mdLite(k.body) + '</div>'
      + conflicts
      + '<h3>来源（原文只归档，不进知识库）</h3><ul>' + (sources || '<li>暂无</li>') + '</ul>'
      + '</div>');
    const btn = $('absorb-btn');
    if (btn) btn.onclick = async () => {
      try {
        await api('/v2/knowledge/' + encodeURIComponent(btn.dataset.id) + '/absorb', { method: 'POST' });
        toast('已标记为吸收，之后会作为框架被调用。');
        await refresh(); await openKnowledge(btn.dataset.id);
      } catch (err) { toast(err.message); }
    };
    const later = $('absorb-later');
    if (later) later.onclick = () => { const box = $('detail-view').querySelector('.notice'); if (box) box.classList.add('hidden'); };
  } catch (err) { toast(err.message); }
}

async function openManual() {
  try {
    const data = await api('/v2/manual');
    const sections = (data.sections || []).filter((s) => s.count);
    state.docId = '';
    renderKB();
    const html = '<div class="detail-card">'
      + '<div class="doc-meta">按领域整理你的主观理解 · 共 ' + data.total + ' 条 · ' + esc(data.generated) + '</div>'
      + '<h1 class="doc-title">个人使用说明</h1>'
      + '<div class="doc-body muted">成型框架排在前面，尚未吸收的认知单独列出。点左栏任意一条，能看整理后的原文与来源。</div></div>'
      + (sections.length ? sections.map((s) => '<div class="detail-card"><h2>' + esc(s.domain)
        + ' <span class="muted" style="font-size:12px;font-weight:400">' + s.count + ' 条</span></h2>'
        + '<div class="doc-body">' + esc(s.narrative) + '</div>'
        + '<h3>成型框架</h3><ul>' + (s.frames.map((f) => {
          const head = String(f.title || '').replace(/…$/, '');
          const rest = String(f.statement || '').startsWith(head) ? '' : '：' + clip(f.statement, 56);
          return '<li><b>' + esc(f.title) + '</b>' + esc(rest) + '</li>';
        }).join('') || '<li>暂无</li>') + '</ul>'
        + '<h3>知识点</h3><ul>' + (s.points.map((f) => '<li>' + esc(f.title) + '</li>').join('') || '<li>暂无</li>') + '</ul>'
        + (s.unabsorbed.length ? '<h3>尚未吸收 / 待对照</h3><ul>' + s.unabsorbed.map((f) => '<li>' + esc(f.title) + '</li>').join('') + '</ul>' : '')
        + '</div>').join('') : '<div class="detail-card"><div class="empty">知识库还是空的，先投喂一份资料。</div></div>');
    setDoc('个人使用说明', html);
  } catch (err) { toast(err.message); }
}

async function globalSearch() {
  const q = $('global-q').value.trim();
  if (!q) { toast('先输入要搜的词。'); return; }
  try {
    const data = await api('/v2/search?q=' + encodeURIComponent(q));
    const kindLabel = { knowledge: '知识', source: '原文归档', action: '待办' };
    const html = '<div class="detail-card"><h2>搜索「' + esc(q) + '」</h2>'
      + '<div class="muted">' + data.items.length + ' 条结果</div></div>'
      + (data.items.length ? data.items.map((r) =>
        '<div class="result-item" data-kind="' + esc(r.kind) + '" data-id="' + esc(r.id) + '">'
        + '<div class="r-title">' + esc(r.title) + '</div>'
        + '<div class="r-meta">' + esc(kindLabel[r.kind] || r.kind) + ' · ' + esc(r.meta || '') + ' · ' + esc(r.snippet || '') + '</div>'
        + '</div>').join('') : '<div class="detail-card"><div class="empty">没有找到。换个词试试。</div></div>');
    setDoc('检索结果', html);
    $('detail-view').querySelectorAll('.result-item[data-kind="knowledge"]').forEach((el) => {
      el.onclick = () => openKnowledge(el.dataset.id);
    });
  } catch (err) { toast(err.message); }
}

/* ---------- 动作 ---------- */

async function sendChat() {
  const text = $('entry').value.trim();
  if (!text) { toast('先写点什么。'); return; }
  const btn = $('send-btn'); btn.disabled = true;
  try {
    await api('/v2/chat', {
      method: 'POST',
      body: JSON.stringify({ text, via: state.voiceUsed ? 'voice' : '' }),
    });
    $('entry').value = '';
    state.voiceUsed = false;
    await refresh();
    await reloadDoc();
  } catch (err) { toast('发送失败：' + err.message); }
  finally { btn.disabled = false; }
}

async function ingestFile(file) {
  const reader = new FileReader();
  reader.onload = async () => {
    toast('正在解析并增量整理…');
    try {
      const data = await api('/v2/ingest/file', {
        method: 'POST', body: JSON.stringify({ name: file.name, data: reader.result }),
      });
      toast('已整理：' + (data.changes || []).length + ' 条变更'
        + (data.truncated ? '（原文超长，已截断并注明）' : ''));
      await refresh(); await reloadDoc();
    } catch (err) { toast('投喂失败：' + err.message); }
  };
  reader.readAsDataURL(file);
}

async function runPlan() {
  const text = $('situation').value.trim();
  if (!text) { toast('先用一句话说说现在的处境。'); return; }
  const btn = $('plan-btn'); btn.disabled = true;
  toast('正在结合知识库梳理…');
  try {
    const data = await api('/v2/plan', {
      method: 'POST', body: JSON.stringify({ situation: text }),
    });
    state.planResult = data;
    await refresh();
    const hits = (data.similar || []).length;
    toast('梳理完成：' + (data.todos || []).length + ' 条待办'
      + (hits ? '，命中 ' + hits + ' 个相似处境' : ''));
  } catch (err) { toast('梳理失败：' + err.message); }
  finally { btn.disabled = false; }
}

async function completeTodo(id) {
  try {
    const data = await api('/v2/actions/' + encodeURIComponent(id) + '/complete', { method: 'POST' });
    state.cheer = data.encouragement;
    state.reflectFor = id;
    await refresh();
    if (!data.already) toast(data.encouragement.line);
    const box = $('reflect-input'); if (box) box.focus();
  } catch (err) { toast(err.message); }
}

async function submitReflect(id) {
  const box = $('reflect-input');
  const text = box ? box.value.trim() : '';
  if (!text) { toast('写一句执行中的新想法再回补。'); return; }
  try {
    const data = await api('/v2/actions/' + encodeURIComponent(id) + '/reflect', {
      method: 'POST', body: JSON.stringify({ text }),
    });
    state.reflectFor = '';
    await refresh(); await reloadDoc();
    toast('已回补：' + (data.distill.changes || []).length + ' 条变更进入知识库');
  } catch (err) { toast(err.message); }
}

/* ---------- 语音 ---------- */

const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
let recognition = null, mediaRecorder = null, mediaStream = null, chunks = [];

function setVoice(text) {
  $('voice-status').textContent = text;
  $('voice-btn').classList.toggle('on', Boolean(text));
}

function appendEntry(text) {
  const el = $('entry');
  el.value = el.value.trim() ? el.value.replace(/\s+$/, '') + '\n' + text : text;
  el.focus();
}

async function toggleVoice() {
  if (recognition) { recognition.stop(); return; }
  if (mediaRecorder && mediaRecorder.state === 'recording') { mediaRecorder.stop(); return; }
  if (SpeechRecognition) {
    try {
      recognition = new SpeechRecognition();
      recognition.lang = 'zh-CN';
      recognition.interimResults = false;
      recognition.continuous = false;
      recognition.onstart = () => setVoice('正在听… 再点一次结束');
      recognition.onresult = (event) => {
        appendEntry(event.results[0][0].transcript);
        state.voiceUsed = true;
        toast('已转成文字，确认后点发送。');
      };
      recognition.onerror = (event) => { toast('语音识别失败：' + event.error); };
      recognition.onend = () => { recognition = null; setVoice(''); };
      recognition.start();
    } catch (err) { toast('浏览器没能启动语音识别：' + err.message); }
    return;
  }
  try {
    mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    chunks = [];
    mediaRecorder = new MediaRecorder(mediaStream);
    const mime = mediaRecorder.mimeType || 'audio/webm';
    mediaRecorder.ondataavailable = (event) => chunks.push(event.data);
    mediaRecorder.onstop = () => {
      setVoice('正在转写…');
      const blob = new Blob(chunks, { type: mime });
      const reader = new FileReader();
      reader.onload = async () => {
        try {
          const data = await api('/stt', {
            method: 'POST', body: JSON.stringify({ data: reader.result }),
          });
          appendEntry(data.text || '');
          state.voiceUsed = true;
          toast('已转成文字，确认后点发送。');
        } catch (err) { toast('转写失败：' + err.message); }
        finally { setVoice(''); }
      };
      reader.readAsDataURL(blob);
      mediaStream.getTracks().forEach((t) => t.stop());
      mediaRecorder = null;
    };
    mediaRecorder.start();
    setVoice('正在录音… 再点一次结束');
  } catch (err) { toast('无法使用麦克风：' + err.message); }
}

/* ---------- 设置 ---------- */

async function openSettings() {
  $('settings-modal').classList.remove('hidden');
  $('cfg-status').textContent = '';
  try {
    const data = await api('/config');
    $('cfg-models').innerHTML = (data.models || []).map((m) =>
      '<button class="chip ' + (m.id === data.active ? 'on' : '') + '" data-model="' + esc(m.id) + '">'
      + esc(m.label) + (m.has_key ? '' : '（缺 Key）') + '</button>').join('')
      || '<span class="muted">还没有模型，在下面新增一个。</span>';
    $('cfg-models').querySelectorAll('[data-model]').forEach((btn) => {
      btn.onclick = async () => {
        try {
          await api('/config/active', { method: 'POST', body: JSON.stringify({ id: btn.dataset.model }) });
          toast('已切换模型。'); await openSettings(); await refresh();
        } catch (err) { toast(err.message); }
      };
    });
  } catch (err) { $('cfg-status').textContent = err.message; }
}

/* ---------- 绑定 ---------- */

function switchTab(tab) {
  state.tab = tab;
  document.querySelectorAll('.tab').forEach((el) => el.classList.toggle('on', el.dataset.tab === tab));
  $('pane-chat').classList.toggle('hidden', tab !== 'chat');
  $('pane-action').classList.toggle('hidden', tab !== 'action');
}

function bind() {
  document.querySelectorAll('.tab').forEach((el) => { el.onclick = () => switchTab(el.dataset.tab); });
  $('send-btn').onclick = sendChat;
  $('entry').onkeydown = (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') { event.preventDefault(); sendChat(); }
  };
  $('file-input').onchange = (event) => {
    const file = event.target.files && event.target.files[0];
    if (file) ingestFile(file);
    event.target.value = '';
  };
  $('link-btn').onclick = () => $('link-row').classList.toggle('hidden');
  $('link-cancel').onclick = () => $('link-row').classList.add('hidden');
  $('link-go').onclick = async () => {
    try {
      const data = await api('/v2/ingest/link', {
        method: 'POST',
        body: JSON.stringify({
          url: $('link-url').value.trim(), title: $('link-title').value.trim(),
          note: $('link-note').value.trim(),
        }),
      });
      toast(data.needs_text ? '只登记了链接；贴字幕或摘要才能提炼。'
        : '已整理：' + (data.changes || []).length + ' 条变更');
      $('link-row').classList.add('hidden');
      $('link-note').value = ''; $('link-url').value = '';
      await refresh(); await reloadDoc();
    } catch (err) { toast('投喂失败：' + err.message); }
  };
  $('voice-btn').onclick = toggleVoice;

  $('distill-btn').onclick = async () => {
    const text = $('entry').value.trim();
    if (!text) { toast('把要整理的文字写在输入框里，或先用文件投喂。'); return; }
    try {
      const data = await api('/v2/distill', {
        method: 'POST', body: JSON.stringify({ text, title: '手动整理', kind: 'note' }),
      });
      $('entry').value = '';
      toast('已整理：' + (data.changes || []).length + ' 条变更');
      await refresh(); await reloadDoc();
    } catch (err) { toast(err.message); }
  };
  $('plan-jump').onclick = () => {
    const text = $('entry').value.trim();
    if (text) { $('situation').value = text; toast('已带到处境栏，点「梳理」即可。'); }
    switchTab('action');
    $('situation').focus();
  };
  $('plan-btn').onclick = runPlan;
  $('clear-view').onclick = showManual;
  $('manual-btn').onclick = openManual;
  $('import-btn').onclick = async () => {
    toast('正在把 records/ 里的文档整理进知识库…');
    try {
      const data = await api('/v2/import/records', { method: 'POST' });
      toast('导入完成：' + data.docs + ' 篇有内容，新增 ' + data.created + ' 条、并入 ' + data.merged + ' 条'
        + (data.skipped_docs ? '；另有 ' + data.skipped_docs + ' 篇是空文档或纯清单，跳过' : ''));
      await refresh(); await openManual();
    } catch (err) { toast('导入失败：' + err.message); }
  };
  $('kb-q').oninput = renderKB;
  $('kb-list').onclick = (event) => {
    const head = event.target.closest('.kb-group-head');
    if (head) {
      const domain = head.parentElement.dataset.domain;
      if (state.closed.has(domain)) state.closed.delete(domain);
      else state.closed.add(domain);
      renderKB();
      return;
    }
    const item = event.target.closest('.kb-item');
    if (item) openKnowledge(item.dataset.id);
  };
  $('global-go').onclick = globalSearch;
  $('global-q').onkeydown = (event) => { if (event.key === 'Enter') globalSearch(); };

  $('todos').onclick = async (event) => {
    const done = event.target.closest('[data-complete]');
    if (done) { await completeTodo(done.dataset.complete); return; }
    const submit = event.target.closest('[data-submit-reflect]');
    if (submit) { await submitReflect(submit.dataset.submitReflect); return; }
    if (event.target.closest('[data-close-reflect]')) { state.reflectFor = ''; renderTodos(); }
  };

  $('toggle-right').onclick = () => {
    const closed = $('cols').classList.toggle('right-closed');
    $('toggle-right').textContent = closed ? '展开对话栏' : '收起对话栏';
  };
  $('legacy-btn').onclick = () => { location.href = '/'; };
  $('settings-btn').onclick = openSettings;
  $('settings-close').onclick = () => $('settings-modal').classList.add('hidden');
  $('settings-modal').onclick = (event) => {
    if (event.target.id === 'settings-modal') $('settings-modal').classList.add('hidden');
  };
  $('cfg-save').onclick = async () => {
    const base = $('cfg-base').value.trim();
    const model = $('cfg-model').value.trim();
    const key = $('cfg-key').value.trim();
    if (!base || !model || !key) { $('cfg-status').textContent = '新增模型需要 Base URL / 模型名 / API Key。'; return; }
    try {
      await api('/config/models', {
        method: 'POST',
        body: JSON.stringify({ id: 'v2-byok', label: model, base_url: base, model, api_key: key }),
      });
      await api('/config/active', { method: 'POST', body: JSON.stringify({ id: 'v2-byok' }) });
      $('cfg-key').value = '';
      $('cfg-status').textContent = '已保存并启用；密钥只存在本机 config.json。';
      await openSettings(); await refresh();
    } catch (err) { $('cfg-status').textContent = err.message; }
  };
  $('cfg-test').onclick = async () => {
    $('cfg-status').textContent = '正在测试…';
    try {
      const data = await api('/config/test', { method: 'POST', body: JSON.stringify({}) });
      $('cfg-status').textContent = data.message;
    } catch (err) { $('cfg-status').textContent = '连接失败：' + err.message; }
  };
}

bind();
refresh();
