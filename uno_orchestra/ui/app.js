// Uno control UI. No inline scripts (CSP) and no HTML string injection:
// everything that comes from agents, diffs, files, dictionaries or the server is rendered
// with textContent. Preferences (language, mode, last project) live in localStorage; the
// token never does: it is read once from the URL fragment and kept in memory.
(() => {
  'use strict';

  // --- token: read once from the fragment, kept in memory only, URL cleaned ---------------
  let token = '';
  const frag = location.hash;
  const m = /(?:^|[#&])token=([A-Za-z0-9_-]+)/.exec(frag);
  if (m) token = m[1];
  const fragPref = key => { const r = new RegExp('(?:^|[#&])' + key + '=([a-z]+)').exec(frag); return r ? r[1] : null; };
  if (frag) history.replaceState(null, '', location.pathname);

  const $ = id => document.getElementById(id);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const clear = node => { while (node.firstChild) node.firstChild.remove(); };
  const setList = (ul, items) => { clear(ul); items.forEach(t => ul.appendChild(el('li', '', t))); };

  // --- preferences: language, mode, last project. Never the token. -----------------------
  const PREFS = { lang: 'uno.lang', mode: 'uno.mode', repo: 'uno.repo' };
  function pref(key, value) {
    try {
      if (value === undefined) return localStorage.getItem(PREFS[key]);
      localStorage.setItem(PREFS[key], value);
    } catch { /* storage unavailable: the page still works */ }
    return null;
  }

  // --- i18n: static dictionaries, interface only; agent text is shown as it comes ---------
  let dict = {};
  const t = (key, vars) => {
    let s = Object.prototype.hasOwnProperty.call(dict, key) ? dict[key] : key;
    if (vars) for (const [k, v] of Object.entries(vars)) s = s.split('{' + k + '}').join(String(v));
    return s;
  };
  function applyI18n(root) {
    root.querySelectorAll('[data-i18n]').forEach(n => { n.textContent = t(n.dataset.i18n); });
    root.querySelectorAll('[data-i18n-placeholder]').forEach(n => { n.placeholder = t(n.dataset.i18nPlaceholder); });
  }
  async function setLang(lang) {
    if (!['es', 'en'].includes(lang)) lang = 'es';
    try { const r = await fetch('/i18n/' + lang + '.json'); dict = r.ok ? await r.json() : {}; } catch { dict = {}; }
    document.documentElement.lang = lang;
    pref('lang', lang);
    $('langSeg').querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.lang === lang)));
    applyI18n(document);
    renderAll();
  }
  $('langSeg').addEventListener('click', e => { const b = e.target.closest('button'); if (b) setLang(b.dataset.lang); });

  // --- simple / advanced: a CSS class. Permissions and limits are the server's, not the mode's.
  function setMode(mode) {
    const simple = mode !== 'advanced';
    document.body.classList.toggle('simple', simple);
    pref('mode', simple ? 'simple' : 'advanced');
    $('modeSeg').querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String((b.dataset.mode === 'advanced') !== simple)));
  }
  $('modeSeg').addEventListener('click', e => { const b = e.target.closest('button'); if (b) setMode(b.dataset.mode); });

  // --- connection and notices ------------------------------------------------------------
  const conn = $('conn');
  function setConn(state) {
    conn.className = 'conn ' + ({ live: 'live', refused: 'bad', offline: 'bad' }[state] || 'idle');
    $('connText').textContent = t('conn.' + state);
  }
  let noticeKey = null;
  function notice(key) { noticeKey = key; const n = $('notice'); n.hidden = !key; n.textContent = key ? t(key) : ''; }

  async function api(path, body) {
    const headers = { 'X-Orchestra-Token': token };
    const opts = { method: body ? 'POST' : 'GET', headers };
    if (body) { headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    let r;
    try { r = await fetch(path, opts); }
    catch { setConn('offline'); notice('notice.offline'); throw new Error(t('notice.offline')); }
    const data = await r.json().catch(() => ({ error: `HTTP ${r.status}` }));
    if (r.status === 401) { setConn('offline'); notice('notice.token'); }
    else if (r.ok) notice(null);
    if (!r.ok && !data.errors) throw new Error(data.error || `HTTP ${r.status}`);
    return data;
  }

  // --- status chips: colour independent of the gradient, always icon + text ---------------
  const STATUS_CLASS = { merged: 'ok', done: 'ok', running: 'run', pending: 'dim', blocked: 'warn', failed: 'bad',
                         conflict: 'bad', out_of_scope: 'warn', needs_review: 'warn' };
  const STATUS_ICON = { ok: '✔', run: '●', dim: '○', warn: '▲', bad: '✖' };
  function chip(status) {
    const c = STATUS_CLASS[status] || 'dim';
    return el('span', 'chip ' + c, STATUS_ICON[c] + ' ' + t('status.' + (STATUS_CLASS[status] ? status : 'unknown')));
  }

  // --- project selector: recent repositories, the free path only in advanced mode ----------
  let statusData = {};
  function fillProjects() {
    const sel = $('project'), cur = $('repo').value.trim(); clear(sel);
    sel.appendChild(el('option', '', t('project.none'))).value = '';
    const configured = statusData.configured || [];
    (statusData.recent_repos || []).forEach(r => {
      const name = r.split('/').filter(Boolean).pop() || r;
      const o = el('option', '', configured.includes(r) ? name : `${name} · ${t('project.unconfigured')}`);
      o.value = r; o.title = r; sel.appendChild(o);
    });
    if (cur && ![...sel.options].some(o => o.value === cur)) { const o = el('option', '', cur.split('/').filter(Boolean).pop() || cur); o.value = cur; sel.appendChild(o); }
    sel.value = cur;
    const dl = $('recent'); clear(dl); (statusData.recent_repos || []).forEach(r => dl.appendChild(el('option', '', r)));
  }
  function selectRepo(path) {
    $('repo').value = path;
    pref('repo', path);
    fillProjects(); loadRoles(); runCheck(); updateGithub();
  }
  $('project').addEventListener('change', () => selectRepo($('project').value));
  $('repo').addEventListener('change', () => selectRepo($('repo').value.trim()));

  // Plain-language state of the selected project, with the action to take.
  function renderProjectState() {
    const box = $('projectState'); clear(box); box.className = 'pstate';
    const repo = $('repo').value.trim();
    const say = (key, vars, action) => {
      box.classList.add('err'); box.appendChild(el('span', '', t(key, vars)));
      if (action) { const b = el('button', 'ghost small', t(action.label)); b.addEventListener('click', action.run); box.appendChild(b); }
    };
    if (!repo) return say('err.noproject');
    const d = rolesData;
    if (d.error) { box.classList.add('err'); return box.appendChild(el('span', '', d.error)); }
    if (!(d.roles || []).length) return say('err.noroles');
    if (d.on_main) return say('err.main', { branch: d.branch }, { label: 'err.main.action', run: createBranch });
    if (d.preflight) { if (/uncommitted/.test(d.preflight)) return say('err.dirty'); box.classList.add('err'); return box.appendChild(el('span', '', d.preflight)); }
    if (d.trusted === false) return say('err.untrusted', {}, { label: 'err.untrusted.action', run: () => { setMode('advanced'); $('rolesPanel').scrollIntoView({ behavior: 'smooth' }); } });
    box.appendChild(el('span', 'hint', t('ok.project', { branch: d.branch || '?', n: d.roles.length })));
  }

  async function createBranch() {
    const repo = $('repo').value.trim();
    const name = 'uno-run-' + new Date().toISOString().slice(0, 16).replace(/[-T:]/g, '').replace(/(\d{8})(\d{4})/, '$1-$2');
    if (!confirm(t('branch.confirm', { name, repo }))) return;
    try { const r = await api('/api/branch', { repo, name }); $('runState').textContent = t('branch.created', { branch: r.branch }); }
    catch (e) { $('runState').textContent = e.message; }
    loadRoles(); runCheck();
  }

  // --- task editor (advanced: parallel runs) ----------------------------------------------
  const tasksBox = $('tasks');
  function addTask(task) {
    const row = $('taskRow').content.firstElementChild.cloneNode(true);
    applyI18n(row);
    if (task) {
      row.querySelector('.t-name').value = task.name || '';
      row.querySelector('.t-prompt').value = task.prompt || '';
      row.querySelector('.t-paths').value = (task.paths || []).join(' ');
      row.querySelector('.t-preset').value = task.preset || 'edit';
      row.querySelector('.t-turns').value = task.max_turns || 10;
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
    setList($('steering'), (lastCheck.steering || []).length ? [t('adv.steering', { files: lastCheck.steering.join(', ') })] : []);
    $('start').disabled = !lastCheck.ok || running;
  }
  ['repo', 'backend', 'parallel'].forEach(id => $(id).addEventListener('input', scheduleCheck));
  $('addTask').addEventListener('click', () => addTask());

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
      fillTemplates(r.templates); $('runState').textContent = t('run.saved', { name: r.saved });
    } catch (e) { $('runState').textContent = e.message; }
  });
  $('tplLoad').addEventListener('click', async () => {
    const name = $('tplList').value; if (!name) return;
    try {
      const tpl = await api('/api/templates/' + encodeURIComponent(name));
      $('backend').value = tpl.backend || 'mock'; $('parallel').value = tpl.parallel || 2;
      if (tpl.repo) selectRepo(tpl.repo);
      clear(tasksBox); tpl.tasks.forEach(addTask);
    } catch (e) { $('runState').textContent = e.message; }
  });

  // --- live agents: one node per agent plus the orchestrator, pulses for messages ---------
  let running = false;
  const agents = new Map(), roleOf = new Map();  // roleOf: step -> role, from a pipeline's run_start
  let lastRun = null;
  function nodeFor(name) {
    if (!agents.has(name)) {
      const orc = name === 'orchestrator';
      const node = el('div', 'node' + (orc ? ' orc' : ''));
      node.appendChild(el('div', 'title', orc ? t('agents.orchestrator') : name));
      node.appendChild(el('div', 'role', roleOf.get(name) || ''));
      node.appendChild(el('span', 'task', ''));
      const foot = el('div', 'foot'); foot.appendChild(chip('pending')); foot.appendChild(el('span', 'pct', '')); node.appendChild(foot);
      const bar = el('div', 'bar'); bar.appendChild(el('i')); node.appendChild(bar);
      $('agents').appendChild(node); $('stage').classList.add('busy');
      agents.set(name, { node, pct: 0, orc });
    }
    return agents.get(name);
  }
  function setAgent(name, status, pct, label) {
    const a = nodeFor(name);
    if (status) { a.status = status; a.node.className = 'node ' + status + (a.orc ? ' orc' : ''); a.node.querySelector('.chip').replaceWith(chip(status)); }
    if (pct !== undefined) { a.pct = Math.max(a.pct, pct); a.node.querySelector('.pct').textContent = a.pct + '%'; a.node.querySelector('.bar i').style.width = a.pct + '%'; }
    if (label) a.node.querySelector('.task').textContent = label;
    const role = roleOf.get(name); if (role) a.node.querySelector('.role').textContent = role;
  }
  function pulse(from, to) {
    const a = agents.get(from), b = agents.get(to);
    if (!a || !b || a === b || typeof a.node.animate !== 'function') return;
    const stage = $('stage'), sr = stage.getBoundingClientRect();
    const centre = n => { const r = n.node.getBoundingClientRect(); return [r.left - sr.left + r.width / 2 + stage.scrollLeft, r.top - sr.top + r.height / 2 + stage.scrollTop]; };
    const [x1, y1] = centre(a), [x2, y2] = centre(b);
    const dot = el('i', 'pulse'); dot.style.left = (x1 - 5) + 'px'; dot.style.top = (y1 - 5) + 'px'; stage.appendChild(dot);
    const anim = dot.animate([{ transform: 'translate(0,0)', opacity: 1 }, { transform: `translate(${x2 - x1}px,${y2 - y1}px)`, opacity: .3 }],
                             { duration: 700, easing: 'ease-in-out' });
    anim.onfinish = () => dot.remove();
  }
  function resetStage() { clear($('agents')); agents.clear(); roleOf.clear(); clear($('log')); lastRun = null; $('liveTitle').textContent = ''; }
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
      case 'run_start':
        Object.entries(e.roles || {}).forEach(([w, r]) => roleOf.set(w, r));
        setAgent('orchestrator', 'running', undefined, e.message); (e.workers || []).forEach(w => nodeFor(w));
        lastRun = { id: e.run_id, backend: e.backend }; $('liveTitle').textContent = t('agents.run', lastRun); break;
      case 'start': setAgent(e.source, 'running', 0, e.message); pulse('orchestrator', e.source); break;
      case 'progress': setAgent(e.source, 'running', e.progress); return;  // shown on the node only
      case 'tool': case 'say': setAgent(e.source, undefined, undefined, e.message); break;
      case 'done': setAgent(e.source, 'done', 100, e.message); pulse(e.source, 'orchestrator'); break;
      case 'failed': setAgent(e.source, 'failed', undefined, e.message); break;
      case 'conflict': case 'out_of_scope': case 'needs_review': setAgent(e.target, e.kind, undefined, e.message); pulse('orchestrator', e.target); break;
      case 'merge': setAgent(e.target, 'merged', 100, e.message); pulse('orchestrator', e.target); break;
      case 'handoff': setAgent(e.source, undefined, undefined, e.message); pulse(e.source, e.target); break;
      case 'iteration': case 'question': case 'warning': setAgent('orchestrator', undefined, undefined, e.message); break;
      case 'run_end': setAgent('orchestrator', 'done', undefined, e.message); break;
    }
    addLog(e);
  }

  async function stream(offset) {
    let r;
    try { r = await fetch('/api/events?from=' + offset, { headers: { 'X-Orchestra-Token': token } }); }
    catch { setConn('offline'); notice('notice.offline'); return; }
    if (!r.ok) { setConn('refused'); if (r.status === 401) notice('notice.token'); return; }
    setConn('live');
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
    running = false; $('stop').disabled = true; $('runState').textContent = t('run.finished');
    setConn('idle');
    if (pipelineLive) { pipelineLive = false; await refreshPipeline(); }
    else { try { renderResults(await api('/api/results'), true); } catch (e) { $('runState').textContent = e.message; } }
    runCheck();
  }

  $('start').addEventListener('click', async () => {
    $('start').disabled = true;
    try {
      const r = await api('/api/start', payload());
      if (r.errors) { setList($('errors'), r.errors); $('start').disabled = false; return; }
      running = true; $('stop').disabled = false; $('runState').textContent = t('run.running');
      $('results').hidden = true; resetStage();
      stream(r.offset);
    } catch (e) { $('runState').textContent = e.message; $('start').disabled = false; }
  });
  $('stop').addEventListener('click', async () => {
    $('stop').disabled = true;
    try { await api('/api/stop', {}); $('runState').textContent = t('run.stopping'); }
    catch (e) { $('runState').textContent = e.message; }
  });

  // --- results: fail closed, no merge button for anything the gate rejected ---------------
  function diffPre(text) {
    const pre = el('pre', 'diff');
    (text || t('review.nodiff')).split('\n').forEach(line => {
      const cls = line.startsWith('+') && !line.startsWith('+++') ? 'add'
                : line.startsWith('-') && !line.startsWith('---') ? 'del'
                : /^(diff|index|@@|\+\+\+|---)/.test(line) ? 'meta' : '';
      pre.appendChild(el('span', cls, line + '\n'));
    });
    return pre;
  }
  let lastResults = null;
  function renderResults(res, scroll) {
    lastResults = res;
    const box = $('resultList'); clear(box);
    $('results').hidden = false;
    const v = Object.values(res.summary || {});
    $('resultsTitle').textContent = `${res.run_id || '—'} · ${res.repo} · ${res.branch || '?'} · ${res.seconds}s · `
      + t('run.summary', { merged: v.filter(x => x === 'merged').length, total: v.length }) + (res.error ? ` · ${res.error}` : '');
    if (res.error) box.appendChild(el('p', 'reminder', res.error));
    if ((res.steering || []).length) box.appendChild(el('p', 'reminder', t('adv.steering', { files: res.steering.join(', ') })));
    for (const a of res.agents) {
      const card = el('div', 'result ' + a.verdict);
      const head = el('div', 'head');
      head.appendChild(chip(a.verdict)); head.appendChild(el('b', '', a.name));
      head.appendChild(el('span', 'hint', `preset ${a.preset} · paths ${a.paths.join(' ')} · ${a.branch || '—'}`));
      card.appendChild(head);
      if (a.detail) card.appendChild(el('div', 'detail', a.detail));
      if (a.commands.length) {
        card.appendChild(el('div', 'detail', t('adv.results.manual')));
        card.appendChild(el('pre', '', a.commands.join('\n')));
      }
      card.appendChild(diffPre(a.diff));
      box.appendChild(card);
    }
    if (scroll) { setMode('advanced'); $('results').scrollIntoView({ behavior: 'smooth' }); }
  }

  // --- roles (dialog, both modes) & project context (.uno/ in the repository) --------------
  let rolesData = { roles: [] };
  const R = { name: 'r-name', preset: 'r-preset', output: 'r-output', max_turns: 'r-turns',
              description: 'r-desc', instructions: 'r-instr', output_format: 'r-format', paths: 'r-paths' };
  function fillRole(role) {
    const r = role || { name: '', preset: 'edit', output: 'text', max_turns: 20, description: '', instructions: '', output_format: '', paths: [] };
    for (const [k, id] of Object.entries(R)) $(id).value = k === 'paths' ? (r.paths || []).join(' ') : (r[k] ?? '');
    $('r-newname').value = '';
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
    (rolesData.roles || []).forEach(r => { const o = el('option', '', `${r.name} · ${r.preset}`); o.value = r.name; sel.appendChild(o); });
    if ([...sel.options].some(o => o.value === current)) sel.value = current;
    const lib = $('libList'); const curLib = lib.value; clear(lib);
    (rolesData.library || []).forEach(x => { const o = el('option', '', `${x.name} · ${t(x.builtin ? 'roles.builtin' : 'roles.mine')}`); o.value = x.name; lib.appendChild(o); });
    if ([...lib.options].some(o => o.value === curLib)) lib.value = curLib;
    $('rolesCount').textContent = t('roles.count', { n: (rolesData.roles || []).length, max: rolesData.max_roles || 12 });
    $('projectCtx').value = rolesData.project_context || '';
    $('orchestraCommit').hidden = !(rolesData.uncommitted || []).length;
    setList($('rolesErrors'), rolesData.error ? [rolesData.error] : []);
    const untrusted = rolesData.trusted === false && !rolesData.error;
    $('rolesTrust').hidden = !untrusted;
    if (untrusted) setList($('rolesPreview'), (rolesData.roles || []).map(r =>
      `${r.name} · ${r.preset} · ${r.output} · paths ${r.paths.join(' ')} — ${r.description} — instructions: ${r.instructions}`)
      .concat(rolesData.project_context ? [`project context: ${rolesData.project_context}`] : [])
      .concat((rolesData.references || []).map(r => `referenced document (hashed with the roles, protected from the agents): ${r.path} · ${r.size} characters`)));
    $('rolesState').textContent = (untrusted ? 'NOT CONFIRMED · ' : '') + `${(rolesData.roles || []).length} roles` +
      ((rolesData.uncommitted || []).length ? ` · uncommitted: ${rolesData.uncommitted.join(', ')}` : '') +
      Object.entries(rolesData.truncated || {}).map(([doc, n]) => ` · context truncated: ${n} characters of ${doc} omitted (agents can Read the file)`).join('');
    $('rolesOpen').disabled = !$('repo').value.trim();
    renderProjectState();
  }
  async function loadRoles() {
    const repo = $('repo').value.trim();
    if (!repo) { rolesData = { roles: [] }; renderRoles(); renderPipeline(); return; }
    try { rolesData = await api('/api/roles?repo=' + encodeURIComponent(repo)); }
    catch (e) { rolesData = { roles: [], error: e.message }; }
    renderRoles(); renderPipeline();
  }
  async function rolesPost(path, body) {
    try { rolesData = await api(path, { repo: $('repo').value.trim(), ...body }); renderRoles(); return true; }
    catch (e) { setList($('rolesErrors'), [e.message]); return false; }
  }
  const currentRole = () => (rolesData.roles || []).find(r => r.name === $('roleList').value);
  function openRoles() {
    if (!$('repo').value.trim()) { setList($('rolesErrors'), [t('roles.noproject')]); return; }
    loadRoles().then(() => { fillRole(currentRole()); $('rolesDialog').showModal(); });
  }
  $('rolesOpen').addEventListener('click', openRoles);
  $('rolesOpen2').addEventListener('click', openRoles);
  $('roleList').addEventListener('change', () => fillRole(currentRole()));
  $('roleNew').addEventListener('click', () => { $('roleList').value = ''; fillRole(null); $('r-name').focus(); });
  $('roleDup').addEventListener('click', () => { const r = currentRole(); if (!r) return; $('roleList').value = ''; fillRole({ ...r, name: '' }); $('r-name').focus(); });
  $('roleSave').addEventListener('click', () => { const r = readRole(); rolesPost('/api/roles', { role: r }).then(ok => { if (ok) { $('roleList').value = r.name; fillRole(currentRole()); } }); });
  $('roleDelete').addEventListener('click', () => { const n = $('roleList').value; if (n && confirm(t('adv.roleDelete.confirm', { name: n }))) rolesPost('/api/roles/delete', { name: n }).then(ok => { if (ok) fillRole(null); }); });
  $('roleRename').addEventListener('click', () => {
    const name = $('roleList').value, next = $('r-newname').value.trim(); if (!name || !next) return;
    rolesPost('/api/roles/rename', { name, new_name: next }).then(ok => { if (ok) { $('roleList').value = next; fillRole(currentRole()); } });
  });
  $('roleFromTpl').addEventListener('click', () => {
    const template = $('libList').value; if (!template) return;
    let name = template;
    if ((rolesData.roles || []).some(r => r.name === template)) { name = prompt(t('roles.copyName'), template + '-2'); if (!name) return; }
    rolesPost('/api/roles/from-template', { template, name }).then(ok => { if (ok) { $('roleList').value = name; fillRole(currentRole()); } });
  });
  $('roleTplSave').addEventListener('click', async () => {
    try { const r = await api('/api/role-templates', { role: readRole() }); rolesData.library = r.library; renderRoles(); setList($('rolesErrors'), []); }
    catch (e) { setList($('rolesErrors'), [e.message]); }
  });
  $('tplDelete').addEventListener('click', async () => {
    const name = $('libList').value; if (!name || !confirm(t('roles.confirmDeleteTpl', { name }))) return;
    try { const r = await api('/api/role-templates/delete', { name }); rolesData.library = r.library; renderRoles(); }
    catch (e) { setList($('rolesErrors'), [e.message]); }
  });
  $('rolesDialog').addEventListener('close', () => { runCheck(); refreshPipeline(); });
  $('projectSave').addEventListener('click', () => rolesPost('/api/project', { context: $('projectCtx').value }));
  $('roleTrust').addEventListener('click', () => rolesPost('/api/roles/trust', {}).then(runCheck));
  $('orchestraCommit').addEventListener('click', () => { if (confirm(t('adv.commit.confirm'))) rolesPost('/api/roles/commit', {}); });

  // --- pipeline: chat with the orchestrator, approve, watch, merge ------------------------
  let pipe = { state: 'idle', transcript: [] }, pipeTimer = null, lastState = 'idle';
  const ACTIVE = ['planning', 'running', 'paused', 'question'];
  function pendingCount() { return (pipe.state === 'proposed' ? 1 : 0) + (pipe.state === 'review' ? 1 : 0); }
  function renderPipeline() {
    const st = pipe.state || 'idle';
    $('pipeState').textContent = t('state.' + st) + (pipe.iterations ? ' · ' + t('chat.round', { n: pipe.iterations, max: pipe.max_iterations }) : '');
    $('pipeInfo').textContent = pipe.branch ? t('review.branch', { branch: pipe.branch }) : '';
    const box = $('chat'); clear(box);
    const transcript = pipe.transcript || [];
    if (!transcript.length) box.appendChild(el('div', 'empty', t($('repo').value.trim() ? 'chat.empty.ready' : 'chat.empty.project')));
    transcript.forEach(msg => {
      const row = el('div', 'chat-msg ' + (msg.who === 'you' ? 'you' : 'orchestrator'));
      row.appendChild(el('span', 'who', t(msg.who === 'you' ? 'chat.you' : 'chat.orchestrator')));
      row.appendChild(el('span', 'text', msg.text)); box.appendChild(row);
    });
    box.scrollTop = box.scrollHeight;
    $('pPause').disabled = st !== 'running';
    $('pResume').disabled = !(st === 'paused' || st === 'question');
    $('pCancel').disabled = !['running', 'paused', 'question'].includes(st);
    $('chatSend').disabled = st === 'planning';

    const plan = pipe.plan;
    $('planCard').hidden = !plan;
    if (plan) {
      $('planSummary').textContent = plan.summary;
      const tb = $('planTable').querySelector('tbody'); clear(tb);
      plan.steps.forEach((s, i) => {
        const tr = el('tr');
        tr.appendChild(el('td', '', String(i + 1)));
        tr.appendChild(el('td', 'mono', s.id)); tr.appendChild(el('td', '', s.role)); tr.appendChild(el('td', 'goal', s.goal));
        tr.appendChild(el('td', 'mono', s.paths.join(' '))); tr.appendChild(el('td', 'mono', s.depends_on.join(', ') || '—'));
        const td = el('td'); td.appendChild(chip(s.status));
        if (s.detail) td.appendChild(el('div', 'hint', s.detail));
        if (s.findings && s.findings.length) td.appendChild(el('div', 'hint', `${s.findings.length} findings`));
        tr.appendChild(td);
        tr.appendChild(el('td', '', s.iteration ? String(s.iteration) : '—'));
        tb.appendChild(tr);
      });
      $('planActions').hidden = st !== 'proposed';
    }
    const review = st === 'review' || st === 'merged';
    $('pipeReview').hidden = !review;
    if (review) {
      const ul = $('pendingFindings'); clear(ul);
      const findings = pipe.pending_findings || [];
      if (!findings.length) ul.appendChild(el('li', 'none', t('review.findings.none')));
      findings.forEach(f => ul.appendChild(el('li', 'sev-' + String(f.severity || '').toLowerCase(),
        `[${f.severity}] ${f.step} (${f.role}): ${f.file}${f.line ? ':' + f.line : ''} — ${f.description} → ${f.recommendation}`)));
      const d = $('pipeDiff'); clear(d); d.appendChild(diffPre(pipe.diff));
      $('pipeCmds').textContent = (pipe.commands || []).join('\n');
      $('pMerge').disabled = st !== 'review';
    }
    const n = pendingCount();
    $('pendingCount').textContent = String(n); $('pendingCount').classList.toggle('zero', !n);
    if (st !== lastState) { lastState = st; $('reviewPanel').scrollTop = 0; }
    $('reviewEmpty').hidden = !!(plan || review);
    clearTimeout(pipeTimer);
    if (ACTIVE.includes(st)) pipeTimer = setTimeout(refreshPipeline, 1500);
  }
  async function refreshPipeline() {
    try { pipe = await api('/api/pipeline'); } catch (e) { $('pipeState').textContent = e.message; return; }
    renderPipeline();
  }
  async function pipePost(path, body) {
    try { const r = await api(path, body || {}); await refreshPipeline(); return r; }
    catch (e) { $('pipeState').textContent = e.message; }
  }
  async function send() {
    const message = $('chatInput').value.trim(); if (!message) return;
    const r = await pipePost('/api/chat', { message, repo: $('repo').value.trim(), backend: $('backend').value,
      parallel: parseInt($('parallel').value, 10) || 0, max_iterations: parseInt($('iters').value, 10) || 0 });
    if (r) $('chatInput').value = '';
  }
  $('chatSend').addEventListener('click', send);
  $('chatInput').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.metaKey && !e.ctrlKey) { e.preventDefault(); send(); } });
  async function approvePlan() {
    const r = await pipePost('/api/pipeline/approve');
    if (!r) return;
    running = true; pipelineLive = true; $('results').hidden = true; resetStage();
    stream(r.offset);
  }
  function merge() { if (confirm(t('review.confirm.merge', { branch: pipe.branch }))) pipePost('/api/pipeline/merge'); }
  // Cmd/Ctrl+Enter approves whatever is pending, always after a confirmation.
  function approvePending() {
    if (pipe.state === 'proposed') { if (confirm(t('review.confirm.approve', { n: pipe.plan ? pipe.plan.steps.length : 0 }))) approvePlan(); }
    else if (pipe.state === 'review') merge();
  }
  document.addEventListener('keydown', e => { if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); approvePending(); } });
  $('pApprove').addEventListener('click', approvePlan);
  $('pReject').addEventListener('click', () => pipePost('/api/pipeline/cancel'));
  $('pPause').addEventListener('click', () => pipePost('/api/pipeline/pause'));
  $('pResume').addEventListener('click', () => pipePost('/api/pipeline/resume'));
  $('pCancel').addEventListener('click', () => pipePost('/api/pipeline/cancel'));
  $('pMerge').addEventListener('click', merge);
  $('pDiscard').addEventListener('click', () => pipePost('/api/pipeline/discard'));

  // --- brand: PNGs served from outside the repository, or the neutral mark -----------------
  function brand(on) {
    document.body.classList.toggle('branded', !!on);
    if (!on) return;
    $('brandImg').src = '/brand/lockup.png'; $('heroImg').src = '/brand/mark.png';
    $('favicon').href = '/brand/favicon.png';
    const touch = document.createElement('link'); touch.rel = 'apple-touch-icon'; touch.href = '/brand/apple-touch.png';
    document.head.appendChild(touch);
  }

  function renderAll() {
    fillProjects(); renderProjectState(); renderPipeline();
    agents.forEach(a => {
      if (a.orc) a.node.querySelector('.title').textContent = t('agents.orchestrator');
      a.node.querySelector('.chip').replaceWith(chip(a.status || 'pending'));
    });
    if (lastRun) $('liveTitle').textContent = t('agents.run', lastRun);
    setConn(conn.className.includes('live') ? 'live' : conn.className.includes('bad') ? 'offline' : 'idle');
    if (lastResults) renderResults(lastResults);
    if (noticeKey) notice(noticeKey);
  }

  // --- boot --------------------------------------------------------------------------------
  async function boot() {
    setMode(fragPref('mode') || pref('mode') || 'simple');
    await setLang(fragPref('lang') || pref('lang') || 'es');
    try {
      statusData = await api('/api/status');
      brand(statusData.brand);
      const saved = pref('repo'), recent = statusData.recent_repos || [];
      $('repo').value = saved && recent.includes(saved) ? saved : (recent[0] || '');
      fillProjects(); fillTemplates(statusData.templates || []); setConn('idle');
      if (statusData.running) { running = true; $('stop').disabled = false; stream(statusData.offset); }
      else if (statusData.has_results) { try { renderResults(await api('/api/results')); } catch { /* none */ } }
    } catch { setConn('offline'); }
    if (!tasksBox.children.length) addTask();
    updateGithub();
    runCheck();
    fillRole(null); loadRoles(); refreshPipeline();
  }
  boot();
})();
