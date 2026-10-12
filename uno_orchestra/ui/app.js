// Uno control UI. No inline scripts (CSP) and no HTML string injection:
// everything that comes from agents, diffs, files or the server is rendered with textContent.
(() => {
  'use strict';

  // --- token: read once from the fragment, kept in memory only, URL cleaned ---------------
  let token = '';
  const m = /(?:^|[#&])token=([A-Za-z0-9_-]+)/.exec(location.hash);
  if (m) {
    token = m[1];
    history.replaceState(null, '', location.pathname);
  }

  const $ = id => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const clear = node => { while (node.firstChild) node.firstChild.remove(); };
  const setList = (ul, items) => { clear(ul); items.forEach(t => ul.appendChild(el('li', '', t))); };

  async function api(path, body) {
    const headers = { 'X-Orchestra-Token': token };
    const opts = { method: body ? 'POST' : 'GET', headers };
    if (body) { headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    const r = await fetch(path, opts);
    const data = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
    if (r.status === 401) setConn('● no token: restart uno ui', 'var(--red)');
    if (!r.ok && !data.errors) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
  }

  const conn = $('conn'), statusText = $('statusText');
  function setConn(text, color) { conn.textContent = text; conn.style.color = color; }

  // --- task editor -----------------------------------------------------------------------
  const tasksBox = $('tasks');
  function addTask(t) {
    const row = $('taskRow').content.firstElementChild.cloneNode(true);
    if (t) {
      row.querySelector('.t-name').value = t.name || '';
      row.querySelector('.t-prompt').value = t.prompt || '';
      row.querySelector('.t-paths').value = (t.paths || []).join(' ');
      row.querySelector('.t-preset').value = t.preset || 'edit';
      row.querySelector('.t-turns').value = t.max_turns || 10;
    }
    row.querySelector('.t-remove').addEventListener('click', () => { row.remove(); scheduleCheck(); });
    row.querySelectorAll('input, textarea, select').forEach(i => i.addEventListener('input', scheduleCheck));
    tasksBox.appendChild(row);
    scheduleCheck();
  }
  function readTasks() {
    return [...tasksBox.querySelectorAll('.task')].map(row => ({
      name: row.querySelector('.t-name').value.trim(),
      prompt: row.querySelector('.t-prompt').value,
      paths: row.querySelector('.t-paths').value.split(/[\s,]+/).filter(Boolean),
      preset: row.querySelector('.t-preset').value,
      max_turns: parseInt(row.querySelector('.t-turns').value, 10) || 0,
    }));
  }
  function payload() {
    return { repo: $('repo').value.trim(), backend: $('backend').value,
             parallel: parseInt($('parallel').value, 10) || 0, tasks: readTasks() };
  }

  // --- server-side checks (the UI only displays them) -----------------------------------
  let checkTimer = null, lastCheck = null;
  function scheduleCheck() { clearTimeout(checkTimer); checkTimer = setTimeout(runCheck, 350); }
  async function runCheck() {
    try {
      lastCheck = await api('/api/check', payload());
    } catch (e) {
      lastCheck = { ok: false, errors: [e.message], warnings: [], steering: [], on_main: false };
    }
    setList($('errors'), lastCheck.errors || []);
    setList($('warnings'), lastCheck.warnings || []);
    setList($('steering'), (lastCheck.steering || []).length
      ? [`the repository contains agent instruction files that may reach the model: ${lastCheck.steering.join(', ')}`] : []);
    $('start').disabled = !lastCheck.ok || running;
    $('branch').hidden = !lastCheck.on_main;
    updateGithub();
  }
  ['repo', 'backend', 'parallel'].forEach(id => $(id).addEventListener('input', scheduleCheck));
  $('addTask').addEventListener('click', () => addTask());

  $('branch').addEventListener('click', async () => {
    const name = 'uno-run-' + new Date().toISOString().slice(0, 16).replace(/[-T:]/g, '').replace(/(\d{8})(\d{4})/, '$1-$2');
    if (!confirm(`Create and check out branch ${name} in ${$('repo').value.trim()}?`)) return;
    try { const r = await api('/api/branch', { repo: $('repo').value.trim(), name }); $('runState').textContent = `on branch ${r.branch}`; }
    catch (e) { $('runState').textContent = e.message; }
    runCheck();
  });

  // --- GitHub helpers: a link and a command to copy; the UI never runs gh -----------------
  function updateGithub() {
    const repo = $('repo').value.trim();
    const name = repo.split('/').filter(Boolean).pop() || '<name>';
    $('ghNew').href = 'https://github.com/new?name=' + encodeURIComponent(name);
    $('ghCmd').textContent = `gh repo create ${name} --source ${repo || '<path>'} --private`;
  }
  $('ghCopy').addEventListener('click', () => navigator.clipboard.writeText($('ghCmd').textContent).catch(() => {}));

  // --- templates --------------------------------------------------------------------------
  function fillTemplates(names) {
    const sel = $('tplList'); clear(sel);
    names.forEach(n => sel.appendChild(el('option', '', n)));
  }
  $('tplSave').addEventListener('click', async () => {
    try {
      const r = await api('/api/templates', { name: $('tplName').value.trim(), ...payload() });
      fillTemplates(r.templates); $('runState').textContent = `saved template ${r.saved}`;
    } catch (e) { $('runState').textContent = e.message; }
  });
  $('tplLoad').addEventListener('click', async () => {
    const name = $('tplList').value; if (!name) return;
    try {
      const t = await api('/api/templates/' + encodeURIComponent(name));
      $('repo').value = t.repo || ''; $('backend').value = t.backend || 'mock'; $('parallel').value = t.parallel || 2;
      clear(tasksBox); t.tasks.forEach(addTask);
    } catch (e) { $('runState').textContent = e.message; }
  });

  // --- run: start, stream, stop ----------------------------------------------------------
  let running = false;
  const agents = new Map(), roleOf = new Map();  // roleOf: step -> role, from a pipeline's run_start
  function agentCard(name) {
    if (!agents.has(name)) {
      const node = el('div', 'node');
      node.appendChild(el('div', 'title', name));
      const body = el('div', 'body'); body.appendChild(el('span', 'task', '—')); body.appendChild(el('span', 'pct', '0%'));
      node.appendChild(body);
      const bar = el('div', 'bar'); bar.appendChild(el('i')); node.appendChild(bar);
      $('agents').appendChild(node);
      agents.set(name, { node, pct: 0 });
    }
    return agents.get(name);
  }
  function setAgent(name, status, pct, label) {
    const a = agentCard(name);
    a.node.className = 'node ' + status;
    if (pct !== undefined) { a.pct = Math.max(a.pct, pct); a.node.querySelector('.pct').textContent = a.pct + '%'; a.node.querySelector('.bar i').style.width = a.pct + '%'; }
    if (label) a.node.querySelector('.task').textContent = label;
  }
  function fmtTs(ts) { const d = new Date(ts * 1000); return d.toTimeString().slice(3, 8) + '.' + String(d.getMilliseconds()).padStart(3, '0'); }
  function addLog(e) {
    const row = el('div', 'msg kind-' + e.kind);
    row.appendChild(el('span', 'ts', fmtTs(e.ts)));
    const who = el('span'); who.appendChild(el('span', 'src' + (e.source === 'orchestrator' ? ' orc' : ''), e.source));
    who.appendChild(el('span', 'arrow', e.source === 'orchestrator' ? ' ⇉ ' : ' → ')); who.appendChild(el('span', 'tgt', e.target || '·'));
    row.appendChild(who);
    const txt = el('span', 'txt', `${e.kind}: ${e.message || ''}`); txt.title = e.message || ''; row.appendChild(txt);
    const log = $('log'); log.appendChild(row); while (log.children.length > 400) log.firstChild.remove(); log.scrollTop = log.scrollHeight;
  }
  function handle(e) {
    switch (e.kind) {
      case 'run_start': (e.workers || []).forEach(w => agentCard(w)); Object.entries(e.roles || {}).forEach(([w, r]) => roleOf.set(w, r)); $('liveTitle').textContent = `${e.run_id} · ${e.backend} · ${e.message}`; break;
      case 'start': setAgent(e.source, 'running', 0, roleOf.get(e.source) || (e.message || '').split(/\s+/).slice(0, 3).join(' ')); break;
      case 'progress': setAgent(e.source, 'running', e.progress); return;  // shown on the card only
      case 'done': setAgent(e.source, 'done', 100); break;
      case 'failed': if (e.source !== 'orchestrator') setAgent(e.source, 'failed'); break;
      case 'conflict': case 'out_of_scope': case 'needs_review': setAgent(e.target, e.kind); break;
      case 'merge': setAgent(e.target, 'merged', 100); break;
      case 'handoff': case 'iteration': case 'question': break;  // logged below, shown as bus messages
    }
    addLog(e);
  }

  async function stream(offset) {
    const r = await fetch('/api/events?from=' + offset, { headers: { 'X-Orchestra-Token': token } });
    if (!r.ok) { setConn('● stream refused', 'var(--red)'); return; }
    setConn('● live', 'var(--green)');
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split('\n'); buf = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        let e; try { e = JSON.parse(line); } catch { continue; }
        if (e.kind === 'stream_end') { await finish(); return; }
        handle(e);
      }
    }
    await finish();
  }

  let pipelineLive = false;
  async function finish() {
    running = false; $('stop').disabled = true; $('runState').textContent = 'finished';
    setConn('● idle', 'var(--dim)');
    if (pipelineLive) { pipelineLive = false; await refreshPipeline(); }
    else { try { renderResults(await api('/api/results'), true); } catch (e) { $('runState').textContent = e.message; } }
    runCheck();
  }

  $('start').addEventListener('click', async () => {
    $('start').disabled = true;
    try {
      const r = await api('/api/start', payload());
      if (r.errors) { setList($('errors'), r.errors); $('start').disabled = false; return; }
      running = true; $('stop').disabled = false; $('runState').textContent = 'running…';
      $('live').hidden = false; $('results').hidden = true; clear($('agents')); clear($('log')); agents.clear();
      statusText.textContent = `running in ${$('repo').value.trim()}`;
      stream(r.offset);
    } catch (e) { $('runState').textContent = e.message; $('start').disabled = false; }
  });
  $('stop').addEventListener('click', async () => {
    $('stop').disabled = true;
    try { await api('/api/stop', {}); $('runState').textContent = 'stopping: killing agent process groups…'; }
    catch (e) { $('runState').textContent = e.message; }
  });

  // --- results: fail closed, no merge button for anything the gate rejected ---------------
  function diffPre(text) {
    const pre = el('pre', 'diff');
    (text || '(no diff)').split('\n').forEach(line => {
      const cls = line.startsWith('+') && !line.startsWith('+++') ? 'add'
                : line.startsWith('-') && !line.startsWith('---') ? 'del'
                : /^(diff|index|@@|\+\+\+|---)/.test(line) ? 'meta' : '';
      pre.appendChild(el('span', cls, line + '\n'));
    });
    return pre;
  }
  function renderResults(res, scroll) {
    const box = $('resultList'); clear(box);
    $('results').hidden = false;
    $('resultsTitle').textContent = `${res.run_id || '—'} · ${res.repo} on ${res.branch || '?'} · ${res.seconds}s`;
    const v = Object.values(res.summary || {});
    statusText.textContent = `run finished · ${v.filter(x => x === 'merged').length}/${v.length} merged` + (res.error ? ` · ${res.error}` : '');
    if (res.error) box.appendChild(el('p', 'reminder', `run error: ${res.error}`));
    if ((res.steering || []).length) box.appendChild(el('p', 'reminder', `agent instruction files in the repository: ${res.steering.join(', ')}`));
    for (const a of res.agents) {
      const card = el('div', 'result ' + a.verdict);
      const head = el('div', 'head');
      head.appendChild(el('span', 'verdict', a.verdict)); head.appendChild(el('b', '', a.name));
      head.appendChild(el('span', 'hint', `preset ${a.preset} · paths ${a.paths.join(' ')} · ${a.branch || 'no branch'}`));
      card.appendChild(head);
      if (a.detail) card.appendChild(el('div', 'detail', a.detail));
      if (a.commands.length) {
        card.appendChild(el('div', 'detail', 'Not merged. There is no merge button for this by design: review the diff, then merge by hand if it is right.'));
        card.appendChild(el('pre', '', a.commands.join('\n')));
      }
      card.appendChild(diffPre(a.diff));
      box.appendChild(card);
    }
    if (scroll) $('results').scrollIntoView({ behavior: 'smooth' });
  }

  // --- roles & project context (.uno/ in the repository) ----------------------------
  let rolesData = { roles: [] };
  const R = { name: 'r-name', preset: 'r-preset', output: 'r-output', max_turns: 'r-turns',
              description: 'r-desc', instructions: 'r-instr', output_format: 'r-format', paths: 'r-paths' };
  function fillRole(role) {
    const r = role || { name: '', preset: 'edit', output: 'text', max_turns: 20, description: '', instructions: '', output_format: '', paths: [] };
    for (const [k, id] of Object.entries(R)) $(id).value = k === 'paths' ? (r.paths || []).join(' ') : (r[k] ?? '');
  }
  function readRole() {
    const r = {};
    for (const [k, id] of Object.entries(R)) r[k] = $(id).value;
    r.name = r.name.trim(); r.max_turns = parseInt(r.max_turns, 10) || 0;
    r.paths = r.paths.split(/[\s,]+/).filter(Boolean);
    return r;
  }
  function renderRoles() {
    const sel = $('roleList'); const current = sel.value; clear(sel);
    sel.appendChild(el('option', '', '(new)')).value = '';
    (rolesData.roles || []).forEach(r => { const o = el('option', '', `${r.name} · ${r.preset} · ${r.output}`); o.value = r.name; sel.appendChild(o); });
    if ([...sel.options].some(o => o.value === current)) sel.value = current;
    $('projectCtx').value = rolesData.project_context || '';
    $('roleTemplates').disabled = !(rolesData.templates || []).length;
    $('orchestraCommit').hidden = !(rolesData.uncommitted || []).length;
    setList($('rolesErrors'), rolesData.error ? [rolesData.error] : []);
    const untrusted = rolesData.trusted === false && !rolesData.error;
    $('rolesTrust').hidden = !untrusted;
    if (untrusted) setList($('rolesPreview'), (rolesData.roles || []).map(r =>
      `${r.name} · ${r.preset} · ${r.output} · paths ${r.paths.join(' ')} — ${r.description} — instructions: ${r.instructions}`)
      .concat(rolesData.project_context ? [`project context: ${rolesData.project_context}`] : [])
      .concat((rolesData.references || []).map(r => `referenced document (hashed with the roles): ${r.path} · ${r.size} characters`)));
    $('rolesState').textContent = (untrusted ? 'NOT CONFIRMED · ' : '') + `${(rolesData.roles || []).length} roles` +
      ((rolesData.uncommitted || []).length ? ` · uncommitted: ${rolesData.uncommitted.join(', ')}` : '') +
      Object.entries(rolesData.truncated || {}).map(([doc, n]) => ` · context truncated: ${n} characters of ${doc} omitted (agents can Read the file)`).join('');
  }
  async function loadRoles() {
    const repo = $('repo').value.trim(); if (!repo) return;
    try { rolesData = await api('/api/roles?repo=' + encodeURIComponent(repo)); }
    catch (e) { rolesData = { roles: [], error: e.message }; }
    renderRoles();
  }
  async function rolesPost(path, body) {
    try { rolesData = await api(path, { repo: $('repo').value.trim(), ...body }); renderRoles(); }
    catch (e) { setList($('rolesErrors'), [e.message]); }
  }
  $('roleList').addEventListener('change', () => fillRole((rolesData.roles || []).find(r => r.name === $('roleList').value)));
  $('roleNew').addEventListener('click', () => { $('roleList').value = ''; fillRole(null); });
  $('roleSave').addEventListener('click', () => rolesPost('/api/roles', { role: readRole() }).then(() => { $('roleList').value = readRole().name; }));
  $('roleDelete').addEventListener('click', () => { const n = $('roleList').value; if (n && confirm(`Delete role ${n}?`)) rolesPost('/api/roles/delete', { name: n }).then(() => fillRole(null)); });
  $('roleTemplates').addEventListener('click', () => rolesPost('/api/roles/templates', {}));
  $('projectSave').addEventListener('click', () => rolesPost('/api/project', { context: $('projectCtx').value }));
  $('roleTrust').addEventListener('click', () => rolesPost('/api/roles/trust', {}).then(runCheck));
  $('orchestraCommit').addEventListener('click', () => { if (confirm('Commit .uno/ (roles and project context) on the current branch?')) rolesPost('/api/roles/commit', {}); });
  $('repo').addEventListener('change', loadRoles);

  // --- pipeline: chat with the orchestrator, approve, watch, merge ------------------------
  let pipe = { state: 'idle', transcript: [] }, pipeTimer = null;
  function renderPipeline() {
    const st = pipe.state;
    $('pipeState').textContent = st + (pipe.iterations ? ` · round ${pipe.iterations}/${pipe.max_iterations}` : '');
    const box = $('chat'); clear(box);
    (pipe.transcript || []).forEach(m => {
      const row = el('div', 'chat-msg ' + m.who); row.appendChild(el('span', 'who', m.who === 'you' ? 'you' : 'orchestrator'));
      row.appendChild(el('span', 'text', m.text)); box.appendChild(row);
    });
    box.scrollTop = box.scrollHeight;
    $('pApprove').disabled = st !== 'proposed';
    $('pPause').disabled = st !== 'running';
    $('pResume').disabled = !(st === 'paused' || st === 'question');
    $('pCancel').disabled = !['proposed', 'running', 'paused', 'question'].includes(st);
    $('chatSend').disabled = st === 'planning';
    $('pipeInfo').textContent = pipe.branch ? `integration branch ${pipe.branch}` : '';
    const table = $('planTable'); table.hidden = !pipe.plan;
    if (pipe.plan) {
      const tb = table.querySelector('tbody'); clear(tb);
      pipe.plan.steps.forEach((s, i) => {
        const tr = el('tr');
        [String(i + 1), s.id, s.role, s.goal, s.paths.join(' '), s.depends_on.join(', ') || '—'].forEach(t => tr.appendChild(el('td', '', t)));
        tr.appendChild(el('td', 'st-' + s.status, s.status + (s.detail ? `: ${s.detail}` : '') +
          (s.findings && s.findings.length ? ` · ${s.findings.length} findings` : '')));
        tr.appendChild(el('td', '', s.iteration ? String(s.iteration) : '—'));
        tb.appendChild(tr);
      });
    }
    const review = st === 'review' || st === 'merged';
    $('pipeReview').hidden = !review;
    if (review) {
      setList($('pendingFindings'), (pipe.pending_findings || []).map(f =>
        `[${f.severity}] ${f.step} (${f.role}): ${f.file}${f.line ? ':' + f.line : ''} — ${f.description} → ${f.recommendation}`));
      const d = $('pipeDiff'); clear(d); d.appendChild(diffPre(pipe.diff));
      $('pipeCmds').textContent = (pipe.commands || []).join('\n');
      $('pMerge').disabled = st !== 'review';
    }
    clearTimeout(pipeTimer);
    if (['planning', 'running', 'paused', 'question'].includes(st)) pipeTimer = setTimeout(refreshPipeline, 1500);
  }
  async function refreshPipeline() {
    try { pipe = await api('/api/pipeline'); } catch (e) { $('pipeState').textContent = e.message; return; }
    renderPipeline();
  }
  async function pipePost(path, body) {
    try { const r = await api(path, body || {}); await refreshPipeline(); return r; }
    catch (e) { $('pipeState').textContent = e.message; }
  }
  $('chatSend').addEventListener('click', async () => {
    const message = $('chatInput').value.trim(); if (!message) return;
    const r = await pipePost('/api/chat', { message, repo: $('repo').value.trim(), backend: $('backend').value,
      parallel: parseInt($('parallel').value, 10) || 0, max_iterations: parseInt($('iters').value, 10) || 0 });
    if (r) $('chatInput').value = '';
  });
  $('chatInput').addEventListener('keydown', e => { if (e.key === 'Enter') $('chatSend').click(); });
  $('pApprove').addEventListener('click', async () => {
    const r = await pipePost('/api/pipeline/approve');
    if (!r) return;
    running = true; pipelineLive = true; $('live').hidden = false; $('results').hidden = true;
    clear($('agents')); clear($('log')); agents.clear(); roleOf.clear();
    statusText.textContent = `pipeline running in ${$('repo').value.trim()}`;
    stream(r.offset);
  });
  $('pPause').addEventListener('click', () => pipePost('/api/pipeline/pause'));
  $('pResume').addEventListener('click', () => pipePost('/api/pipeline/resume'));
  $('pCancel').addEventListener('click', () => pipePost('/api/pipeline/cancel'));
  $('pMerge').addEventListener('click', () => {
    if (confirm(`Merge ${pipe.branch} into your branch? Review the diff and the pending findings first.`)) pipePost('/api/pipeline/merge');
  });
  $('pDiscard').addEventListener('click', () => pipePost('/api/pipeline/discard'));

  // --- boot --------------------------------------------------------------------------------
  async function boot() {
    try {
      const s = await api('/api/status');
      const dl = $('recent'); clear(dl); (s.recent_repos || []).forEach(r => dl.appendChild(el('option', '', r)));
      if (!$('repo').value && s.recent_repos && s.recent_repos[0]) $('repo').value = s.recent_repos[0];
      fillTemplates(s.templates || []);
      setConn('● idle', 'var(--dim)');
      statusText.textContent = `local control · loopback only · auto-shutdown after ${Math.round(s.idle_timeout / 60)} min idle`;
      if (s.running) { running = true; $('stop').disabled = false; $('live').hidden = false; stream(s.offset); }
      else if (s.has_results) { try { renderResults(await api('/api/results')); } catch { /* none */ } }
    } catch (e) { statusText.textContent = e.message; }
    if (!tasksBox.children.length) addTask();
    updateGithub();
    runCheck();
    fillRole(null); loadRoles(); refreshPipeline();
  }
  boot();
})();
