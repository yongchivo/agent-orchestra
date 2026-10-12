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
    const role = roleOf.get(name); if (role && !a.node.querySelector('.role').textContent) a.node.querySelector('.role').textContent = role;
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
      case 'start': setAgent(e.source, 'running', 0, e.message); if (e.model) { const rl = nodeFor(e.source).node.querySelector('.role'); rl.textContent = `${roleOf.get(e.source) || ''} · ${usageText(e)}`.replace(/^ · /, ''); rl.classList.toggle('fable', !!e.override_only); } pulse('orchestrator', e.source); break;
      case 'progress': setAgent(e.source, 'running', e.progress); return;  // shown on the node only
      case 'tool': case 'say': setAgent(e.source, undefined, undefined, e.message); break;
      case 'done': setAgent(e.source, 'done', 100, e.message); if (e.model) { const rl = nodeFor(e.source).node.querySelector('.role'); rl.textContent = `${roleOf.get(e.source) || ''} · ${usageText(e)}`.replace(/^ · /, ''); rl.classList.toggle('fable', !!e.override_only); } pulse(e.source, 'orchestrator'); break;
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
      if (a.usage) head.appendChild(el('span', 'usage' + (a.usage.override_only ? ' fable' : ''), usageText(a.usage)));
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

  // --- models, efforts and profiles: the catalog comes from the server (models.json) --------
  let catalog = { models: [], efforts: [], profiles: {}, role_defaults: {} };
  const profileOf = (model, effort) => Object.keys(catalog.profiles).find(n => catalog.profiles[n].model === model && catalog.profiles[n].effort === effort) || '';
  const defaultPair = name => catalog.profiles[catalog.role_defaults[name] || catalog.role_defaults['*']] || { model: '', effort: '' };
  const overrideOnly = id => (catalog.override_only || []).includes(id);
  function modelSelect(sel, value, perRun) {
    // Saved things (roles, templates) never list an override-only model; a per-run override does.
    clear(sel); catalog.models.forEach(m => { if (perRun || !overrideOnly(m.id)) { const o = el('option', '', m.label || m.id); o.value = m.id; sel.appendChild(o); } });
    sel.value = value;
  }
  function effortSelect(sel, value) {
    clear(sel); catalog.efforts.forEach(e => sel.appendChild(el('option', '', e)).value = e);
    sel.value = value;
  }
  function profileSelect(sel, value) {
    clear(sel); Object.keys(catalog.profiles).forEach(n => { const o = el('option', '', t('profile.' + n)); o.value = n; sel.appendChild(o); });
    const custom = el('option', '', t('profile.custom')); custom.value = ''; custom.disabled = true; sel.appendChild(custom);
    sel.value = value;
  }
  const usageText = u => u && u.model ? (u.override_only || overrideOnly(u.model) ? t('usage.fable') + ' · ' : '') + `${u.model_used || u.model} · ${u.effort}` + (u.turns ? ` · ${u.turns} ${t('usage.turns')}` : '') + (u.seconds != null ? ` · ${u.seconds}s` : '') : '';

  // --- roles (dialog, both modes) & project context (.uno/ in the repository) --------------
  let rolesData = { roles: [] };
  const R = { name: 'r-name', preset: 'r-preset', output: 'r-output', max_turns: 'r-turns',
              description: 'r-desc', instructions: 'r-instr', output_format: 'r-format', paths: 'r-paths' };
  function fillRole(role) {
    const r = role || { name: '', preset: 'edit', output: 'text', max_turns: 20, description: '', instructions: '', output_format: '', paths: [], ...defaultPair('*') };
    for (const [k, id] of Object.entries(R)) $(id).value = k === 'paths' ? (r.paths || []).join(' ') : (r[k] ?? '');
    modelSelect($('r-model'), r.model); effortSelect($('r-effort'), r.effort); profileSelect($('r-profile'), profileOf(r.model, r.effort));
    $('r-newname').value = '';
  }
  function readRole() {
    const r = {};
    for (const [k, id] of Object.entries(R)) r[k] = $(id).value;
    r.name = r.name.trim(); r.max_turns = parseInt(r.max_turns, 10) || 0;
    r.paths = r.paths.split(/[\s,]+/).filter(Boolean);
    // Simple mode: the profile decides; advanced: the exact pair. Both are catalog values only.
    const simple = document.body.classList.contains('simple'), prof = catalog.profiles[$('r-profile').value];
    r.model = simple && prof ? prof.model : $('r-model').value;
    r.effort = simple && prof ? prof.effort : $('r-effort').value;
    return r;
  }
  $('r-profile').addEventListener('change', () => { const p = catalog.profiles[$('r-profile').value]; if (p) { $('r-model').value = p.model; $('r-effort').value = p.effort; } });
  ['r-model', 'r-effort'].forEach(id => $(id).addEventListener('change', () => { $('r-profile').value = profileOf($('r-model').value, $('r-effort').value); }));

  // Per-run overrides (advanced): the planner row plus one row per role, prefilled from the saved values.
  function renderOverrides() {
    const tb = $('overrides').querySelector('tbody'); const prev = readOverrides(); clear(tb);
    const row = (key, label, model, effort) => {
      const tr = el('tr'); tr.dataset.key = key; tr.appendChild(el('td', '', label));
      const ms = el('select', 'ov-model'); modelSelect(ms, (prev[key] || {}).model || model, true); tr.appendChild(el('td')).appendChild(ms);
      const es = el('select', 'ov-effort'); effortSelect(es, (prev[key] || {}).effort || effort); tr.appendChild(el('td')).appendChild(es);
      tb.appendChild(tr);
    };
    const planner = (statusData.planner || defaultPair('planner'));
    row('*planner', t('chat.planner'), planner.model, planner.effort);
    (rolesData.roles || []).forEach(r => row(r.name, r.name, r.model, r.effort));
  }
  function readOverrides() {
    const out = {};
    $('overrides').querySelectorAll('tr').forEach(tr => { out[tr.dataset.key] = { model: tr.querySelector('.ov-model').value, effort: tr.querySelector('.ov-effort').value }; });
    return out;
  }
  // Simple mode: one chip per role (and the orchestrator) switches that step to the
  // override-only model for the next run only; the saved role never changes.
  const special = new Set();
  function renderSpecialChips() {
    const box = $('specialChips'); clear(box);
    const first = (catalog.override_only || [])[0]; if (!first) return;
    [['*planner', t('chat.special.planner')], ...(rolesData.roles || []).map(r => [r.name, r.name])].forEach(([key, label]) => {
      const b = el('button', 'ghost', label); b.type = 'button'; b.setAttribute('aria-pressed', String(special.has(key)));
      b.addEventListener('click', () => { special.has(key) ? special.delete(key) : special.add(key); renderSpecialChips(); });
      box.appendChild(b);
    });
  }
  function runSettings() {
    const simple = document.body.classList.contains('simple'), first = (catalog.override_only || [])[0];
    const overrides = {};
    let planner;
    if (simple) {
      (rolesData.roles || []).forEach(r => { if (special.has(r.name) && first) overrides[r.name] = { model: first, effort: r.effort }; });
      if (special.has('*planner') && first) planner = { model: first, effort: (statusData.planner || {}).effort };
    } else {
      const all = readOverrides(); planner = all['*planner']; delete all['*planner'];
      (rolesData.roles || []).forEach(r => { const o = all[r.name]; if (o && (o.model !== r.model || o.effort !== r.effort)) overrides[r.name] = o; });
    }
    return { overrides, planner: planner || undefined };
  }
  function renderRoles() {
    const sel = $('roleList'); const current = sel.value; clear(sel);
    sel.appendChild(el('option', '', t('roles.newOption'))).value = '';
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
      `${r.name} · ${r.preset} · ${r.output} · ${t('roles.preview.paths')} ${r.paths.join(' ')} — ${r.description} — ${t('roles.preview.instructions')} ${r.instructions}`)
      .concat(rolesData.project_context ? [`${t('roles.preview.context')} ${rolesData.project_context}`] : [])
      .concat((rolesData.references || []).map(r => t('roles.preview.reference', { path: r.path, n: r.size }))));
    $('rolesState').textContent = (untrusted ? t('roles.state.unconfirmed') + ' · ' : '') + t('roles.state.count', { n: (rolesData.roles || []).length }) +
      ((rolesData.uncommitted || []).length ? ` · ${t('roles.state.uncommitted', { files: rolesData.uncommitted.join(', ') })}` : '') +
      Object.entries(rolesData.truncated || {}).map(([doc, n]) => ' · ' + t('roles.state.truncated', { n, doc })).join('');
    $('rolesOpen').disabled = !$('repo').value.trim();
    renderOverrides(); renderSpecialChips();
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
    if (!$('repo').value.trim()) { setList($('rolesErrors'), [t('roles.noproject')]); return Promise.resolve(false); }
    return loadRoles().then(() => { fillRole(currentRole()); $('rolesDialog').showModal(); return true; });
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
    // A pending question is answered with these two buttons, not by typing in the chat.
    ['pResume', 'pCancel'].forEach(id => { $(id).classList.toggle('primary', st === 'question'); $(id).classList.toggle('ghost', st !== 'question'); });
    $('chatSend').disabled = st === 'planning';

    const plan = pipe.plan;
    $('planCard').hidden = !plan;
    if (plan) {
      $('planSummary').textContent = plan.summary;
      const tb = $('planTable').querySelector('tbody'); clear(tb);
      plan.steps.forEach((s, i) => {
        const tr = el('tr');
        tr.appendChild(el('td', '', String(i + 1)));
        tr.appendChild(el('td', 'mono', s.id));
        const roleTd = el('td', '', s.role); if (s.model) roleTd.appendChild(el('div', 'usage' + (s.override_only ? ' fable' : ''), usageText(s.usage && s.usage.model_used ? s.usage : { model: s.model, effort: s.effort, override_only: s.override_only }))); tr.appendChild(roleTd);
        tr.appendChild(el('td', 'goal', s.goal));
        tr.appendChild(el('td', 'mono', s.paths.join(' '))); tr.appendChild(el('td', 'mono', s.depends_on.join(', ') || '—'));
        const td = el('td'); td.appendChild(chip(s.status));
        if (s.detail) td.appendChild(el('div', 'hint', s.detail));
        if (s.findings && s.findings.length) td.appendChild(el('div', 'hint', t('review.findings.count', { n: s.findings.length })));
        tr.appendChild(td);
        tr.appendChild(el('td', '', s.iteration ? String(s.iteration) : '—'));
        tb.appendChild(tr);
      });
      $('planActions').hidden = st !== 'proposed';
      const fable = (pipe.override_only_steps || []).length > 0 || pipe.planner_override_only;
      $('maxNote').hidden = !fable && !plan.steps.some(s => s.profile === 'max');
      $('maxNote').textContent = t(fable ? 'review.fableNote' : 'review.maxNote');
    }
    const review = st === 'review' || st === 'merged';
    $('pipeReview').hidden = !review;
    if (review) {
      const ul = $('pendingFindings'); clear(ul);
      const findings = pipe.pending_findings || [], failures = pipe.review_failures || [];
      const evidence = findings.filter(f => f.kind === 'evidence'), defects = findings.filter(f => f.kind !== 'evidence');
      const item = f => el('li', 'sev-' + String(f.severity || '').toLowerCase(),
        `[${f.severity}] ${f.step} (${f.role}): ${f.file}${f.line ? ':' + f.line : ''} — ${f.description} → ${f.recommendation}`);
      // "No pending findings" only when every review step finished with nothing at all.
      failures.forEach(f => ul.appendChild(el('li', 'sev-high', t(f.status === 'failed' ? 'review.failed' : 'review.unfinished',
        { step: f.step, status: f.status, detail: f.detail }))));
      if (!findings.length && !failures.length) ul.appendChild(el('li', 'none', t('review.findings.none')));
      defects.forEach(f => ul.appendChild(item(f)));
      const vl = $('validationFindings'); clear(vl);
      evidence.forEach(f => vl.appendChild(item(f)));
      $('validationBox').hidden = !evidence.length;
      $('mergeReady').textContent = t(pipe.ready_to_merge ? 'review.ready' : 'review.notReady');
      $('mergeReady').classList.toggle('warn', !pipe.ready_to_merge);
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
      parallel: parseInt($('parallel').value, 10) || 0, max_iterations: parseInt($('iters').value, 10) || 0, ...runSettings() });
    if (r) $('chatInput').value = '';
  }
  $('chatSend').addEventListener('click', send);
  $('chatInput').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.metaKey && !e.ctrlKey) { e.preventDefault(); send(); } });
  async function approvePlan() {
    const body = {};
    if ((pipe.override_only_steps || []).length) {
      if (!confirm(t('review.confirmFable'))) return;   // explicit, separate from the plan approval
      body.confirm_override_only = true;
    }
    const r = await pipePost('/api/pipeline/approve', body);
    if (!r) return;
    running = true; pipelineLive = true; $('results').hidden = true; resetStage();
    stream(r.offset);
  }
  function merge() { if (confirm(t(pipe.ready_to_merge ? 'review.confirm.merge' : 'review.confirm.mergeNotReady', { branch: pipe.branch }))) pipePost('/api/pipeline/merge'); }
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

  // --- tabs: orchestrator / Prompt Studio. A CSS class and two `hidden` flags; nothing else.
  function setTab(tab) {
    const studio = tab === 'prompts';
    document.body.classList.toggle('studio', studio);
    $('layout').hidden = studio; $('studio').hidden = !studio;
    $('tabSeg').querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String((b.dataset.tab === 'prompts') === studio)));
    if (studio && !ps.loaded) loadStudio();
  }
  $('tabSeg').addEventListener('click', e => { const b = e.target.closest('button'); if (b) setTab(b.dataset.tab); });

  // --- Prompt Studio: idea -> questions -> structured prompt -> lint -> copy / library / orchestrator.
  // Everything the model or the library returns is rendered with textContent; the structure,
  // the score and the diff come from the server, never from HTML.
  const ps = { loaded: false, view: { modes: [], templates: [] }, mode: 'create', target: 'claude-code', questions: [], result: null, lib: [], entry: null, busy: false };
  const psBusy = on => { ps.busy = on; ['psGenerate', 'psAnswer', 'psSkip'].forEach(id => { $(id).disabled = on; }); $('psState').textContent = on ? t('ps.generating') : ''; };
  const psErr = msgs => setList($('psErrors'), msgs);
  function psModel() {
    const simple = document.body.classList.contains('simple'), first = (catalog.override_only || [])[0];
    if (!simple) return { model: $('ps-model').value, effort: $('ps-effort').value };
    const prof = catalog.profiles[$('ps-profile').value] || (ps.view.default || {});
    return { model: $('psFable').getAttribute('aria-pressed') === 'true' && first ? first : prof.model, effort: prof.effort };
  }
  function psBody() {
    const b = { mode: ps.mode, target: ps.target, idea: $('ps-idea').value.trim(), kind: $('ps-kind').value, goal: $('ps-goal').value, stack: $('ps-stack').value,
                avoid: $('ps-avoid').value, detail: $('ps-detail').value, lang: $('ps-lang').value, backend: $('backend').value, ...psModel() };
    const repo = $('repo').value.trim(); if (repo) b.repo = repo;
    const tpl = $('ps-template').value; if (tpl) b.template = tpl;
    if (ps.mode === 'review' || ps.mode === 'edit') b.prompt = $('ps-prompt').value;
    if (ps.mode === 'edit') b.changes = $('ps-changes').value;
    return b;
  }
  function setTarget(target) {
    ps.target = target === 'uno' ? 'uno' : 'claude-code';
    $('psTarget').querySelectorAll('button').forEach(b => b.setAttribute('aria-pressed', String(b.dataset.target === ps.target)));
    $('psTargetHint').textContent = t('ps.target.hint.' + ps.target);
  }
  $('psTarget').addEventListener('click', e => { const b = e.target.closest('button'); if (b) setTarget(b.dataset.target); });
  function renderModes() {
    setTarget(ps.target);
    const box = $('psModes'); clear(box);
    ps.view.modes.forEach(m => {
      const b = el('button', 'ghost', t('ps.mode.' + m)); b.type = 'button'; b.setAttribute('aria-pressed', String(m === ps.mode));
      b.addEventListener('click', () => { ps.mode = m; renderModes(); });
      box.appendChild(b);
    });
    const userPrompt = ps.mode === 'review' || ps.mode === 'edit';
    $('psUserPrompt').hidden = !userPrompt; $('psChangesLabel').hidden = ps.mode !== 'edit';
    $('ps-idea').placeholder = t(userPrompt ? 'ps.idea.' + ps.mode : 'ps.idea');
    const sel = $('ps-template'); const cur = ps.view.templates.find(x => x.name === sel.value);
    if (!cur || cur.mode !== ps.mode) sel.value = (ps.view.templates.find(x => x.mode === ps.mode && !x.builtin) || ps.view.templates.find(x => x.mode === ps.mode) || {}).name || '';
  }
  function renderTemplates() {
    const sel = $('ps-template'), cur = sel.value; clear(sel);
    ps.view.templates.forEach(x => { const o = el('option', '', `${x.name} · ${t(x.builtin ? 'roles.builtin' : 'roles.mine')}${x.version ? ' · v' + x.version : ''}`); o.value = x.name; sel.appendChild(o); });
    sel.value = cur;
    const list = $('psTplList'), curT = list.value; clear(list);
    ps.view.templates.forEach(x => { const o = el('option', '', `${x.name} · ${t(x.builtin ? 'roles.builtin' : 'roles.mine')}`); o.value = x.name; list.appendChild(o); });
    list.value = [...list.options].some(o => o.value === curT) ? curT : (ps.view.templates[0] || {}).name || '';
    const modeSel = $('psTplMode'); clear(modeSel); ps.view.modes.forEach(m => { const o = el('option', '', t('ps.mode.' + m)); o.value = m; modeSel.appendChild(o); });
    const lm = $('psLibMode'); const curM = lm.value; clear(lm); lm.appendChild(el('option', '', t('ps.anyMode'))).value = '';
    ps.view.modes.forEach(m => { const o = el('option', '', t('ps.mode.' + m)); o.value = m; lm.appendChild(o); }); lm.value = curM;
    fillTpl();
  }
  function fillTpl() {
    const x = ps.view.templates.find(y => y.name === $('psTplList').value); if (!x) return;
    $('psTplName').value = x.name; $('psTplMode').value = x.mode; $('psTplText').value = x.instructions;
    $('psTplState').textContent = x.builtin ? t('roles.builtin') : t('ps.tpl.version', { n: x.version, all: (x.versions || []).join(', ') });
  }
  $('psTplList').addEventListener('change', fillTpl);
  $('psTplSave').addEventListener('click', async () => {
    try {
      const r = await api('/api/prompts/templates', { name: $('psTplName').value.trim(), mode: $('psTplMode').value, instructions: $('psTplText').value });
      ps.view.templates = r.templates; renderTemplates(); $('psTplList').value = r.saved; fillTpl(); psErr([]);
    } catch (e) { psErr([e.message]); }
  });
  $('psTplDelete').addEventListener('click', async () => {
    const name = $('psTplList').value; if (!name || !confirm(t('ps.tpl.confirmDelete', { name }))) return;
    try { const r = await api('/api/prompts/templates/delete', { name }); ps.view.templates = r.templates; renderTemplates(); psErr([]); }
    catch (e) { psErr([e.message]); }
  });
  async function loadStudio() {
    try { ps.view = await api('/api/prompts'); ps.loaded = true; } catch (e) { psErr([e.message]); return; }
    const d = ps.view.default || defaultPair('*');
    modelSelect($('ps-model'), d.model, true); effortSelect($('ps-effort'), d.effort); profileSelect($('ps-profile'), profileOf(d.model, d.effort) || 'balanced');
    renderTemplates(); renderModes(); loadLibrary();
  }
  function renderStudio() { if (!ps.loaded) return; renderTemplates(); renderModes(); renderLibrary(); if (ps.result) renderResult(ps.result); }
  $('psFable').addEventListener('click', () => { const b = $('psFable'); b.setAttribute('aria-pressed', String(b.getAttribute('aria-pressed') !== 'true')); });

  // step a: clarification (0-5 questions the user answers or skips), then step b.
  async function psStart() {
    psErr([]); $('psQuestions').hidden = true;
    const body = psBody();
    if (!body.idea) return psErr([t('ps.err.idea')]);
    if (overrideOnly(body.model)) { if (!confirm(t('ps.confirmFable'))) return; body.confirm_override_only = true; }
    psBusy(true);
    try {
      const r = await api('/api/prompts/clarify', body);
      ps.questions = r.questions || [];
      if (!ps.questions.length) return await psGenerate([], body);
      const list = $('psQList'); clear(list);
      ps.questions.forEach((q, i) => {
        const lab = el('label'); lab.appendChild(el('span', '', q.question)); lab.appendChild(el('span', 'hint', q.why));
        const inp = el('input'); inp.dataset.i = i; inp.maxLength = 500; inp.placeholder = t('ps.answer.ph'); lab.appendChild(inp); list.appendChild(lab);
      });
      $('psQuestions').hidden = false; list.querySelector('input').focus();
    } catch (e) { psErr([e.message]); }
    finally { psBusy(false); }
  }
  function psAnswers(skipAll) {
    return ps.questions.map((q, i) => ({ question: q.question, answer: skipAll ? '' : ($('psQList').querySelector(`input[data-i="${i}"]`) || {}).value || '' }));
  }
  async function psGenerate(answers, body) {
    body = body || psBody(); body.answers = answers;
    if (overrideOnly(body.model) && !body.confirm_override_only) { if (!confirm(t('ps.confirmFable'))) return; body.confirm_override_only = true; }
    psBusy(true); $('psQuestions').hidden = true;
    try { ps.result = await api('/api/prompts/generate', body); ps.last = body; ps.entry = null; renderResult(ps.result); $('psName').value = ''; $('studioOut').scrollIntoView({ behavior: 'smooth' }); }
    catch (e) { psErr([e.message]); }
    finally { psBusy(false); }
  }
  $('psGenerate').addEventListener('click', psStart);
  $('psAnswer').addEventListener('click', () => psGenerate(psAnswers(false)));
  $('psSkip').addEventListener('click', () => psGenerate(psAnswers(true)));

  const lintText = w => t('lint.' + w.code) + (w.detail ? ` — ${w.detail}` : '');
  function renderLint(lint) {
    const badge = $('psScore'); badge.textContent = String(lint.score); badge.className = 'badge ' + (lint.score >= 80 ? 'ok' : lint.score >= 50 ? 'warn' : 'bad');
    const ul = $('psLint'); clear(ul);
    (lint.warnings || []).forEach(w => ul.appendChild(el('li', 'sev-' + w.severity, lintText(w))));
    if (!(lint.warnings || []).length) ul.appendChild(el('li', 'none ok', t('lint.clean')));
  }
  function renderResult(r) {
    $('psEmpty').hidden = true; $('psResult').hidden = false;
    $('psText').value = r.text;
    renderLint(r.lint);
    $('psMeta').textContent = [t('ps.mode.' + r.mode), t('ps.target.' + (r.target || 'claude-code')), r.lang, r.template, usageText({ model: r.model, effort: r.effort, override_only: r.override_only })].filter(Boolean).join(' · ');
    setList($('psAssumptions'), (r.assumptions || []).length ? r.assumptions : [t('ps.none')]);
    setList($('psOpen'), (r.open_questions || []).length ? r.open_questions : [t('ps.none')]);
    const rv = $('psReview'); rv.hidden = !r.review;
    if (r.review) { $('psGrade').textContent = t('ps.review.grade', { n: r.review.grade }); setList($('psProblems'), r.review.problems); }
    const missing = r.missing_roles || []; $('psRoles').hidden = !missing.length;
    if (missing.length) $('psRolesText').textContent = t('ps.roles.missing', { roles: missing.join(', ') });
    $('psDiffBox').hidden = !r.diff; if (r.diff) { const d = $('psDiff'); clear(d); d.appendChild(diffPre(r.diff)); }
    if (r.lint_input) $('psLint').insertBefore(el('li', 'none', t('ps.lint.input', { n: r.lint_input.score })), $('psLint').firstChild);
  }
  // The text stays editable; the server re-lints it (debounced) so the score is always the server's.
  let lintTimer = null;
  $('psText').addEventListener('input', () => {
    clearTimeout(lintTimer);
    lintTimer = setTimeout(async () => { try { renderLint(await api('/api/prompts/lint', { text: $('psText').value, target: (ps.result || {}).target || ps.target })); } catch (e) { psErr([e.message]); } }, 500);
  });
  $('psRolesOpen').addEventListener('click', () => {
    const name = (ps.result && ps.result.missing_roles || [])[0];
    openRoles().then(ok => { if (ok) { $('roleList').value = ''; fillRole(null); $('r-name').value = name || ''; $('r-instr').focus(); } });
  });
  $('psCopy').addEventListener('click', () => navigator.clipboard.writeText($('psText').value).then(() => { $('psState').textContent = t('ps.copied'); }).catch(() => {}));
  $('psSave').addEventListener('click', async () => {
    const r = ps.result || {}, name = $('psName').value.trim();
    if (!name) return psErr([t('ps.err.name')]);
    const body = { name, text: $('psText').value, tags: $('psTags').value.split(/[\s,]+/).filter(Boolean), mode: r.mode || ps.mode, lang: r.lang || $('ps-lang').value, target: r.target || ps.target,
                   idea: r.idea || $('ps-idea').value.trim(), score: (r.lint || {}).score || 0, model: r.model || '', effort: r.effort || '' };
    try { const s = await api('/api/prompts/library', body); psErr([]); $('psState').textContent = t('ps.saved', { name: s.saved, n: s.version }); loadLibrary(); }
    catch (e) { psErr([e.message]); }
  });
  // Only a prompt generated for Uno is sent (the server checks it too); one generated for Claude
  // Code is regenerated for Uno with the same inputs and answers, never sent as it is.
  async function psSend(text, target, regenerate) {
    const repo = $('repo').value.trim();
    if (!repo) return psErr([t('err.noproject')]);
    if (target !== 'uno') { if (regenerate && confirm(t('ps.send.regenerate'))) regenerate(); else psErr([t('ps.send.notUno')]); return; }
    if (!confirm(t('ps.send.confirm'))) return;
    try {
      await api('/api/prompts/send', { text, target, repo, backend: $('backend').value, parallel: parseInt($('parallel').value, 10) || 0,
                                       max_iterations: parseInt($('iters').value, 10) || 0, ...runSettings() });
      psErr([]); setTab('orchestra'); refreshPipeline();
    } catch (e) { psErr([e.message]); }
  }
  $('psSend').addEventListener('click', () => {
    const r = ps.result || {};
    psSend($('psText').value, r.target, () => {
      setTarget('uno');
      if (ps.last) psGenerate(ps.last.answers || [], { ...ps.last, target: 'uno', confirm_override_only: undefined });
      else psStart();
    });
  });

  // library: versions, tags, search; the diff between two versions is computed by the server.
  let libTimer = null;
  async function loadLibrary() {
    const q = $('psSearch').value.trim(), mode = $('psLibMode').value;
    try { ps.lib = (await api('/api/prompts/library?q=' + encodeURIComponent(q) + '&mode=' + encodeURIComponent(mode))).entries || []; }
    catch (e) { psErr([e.message]); return; }
    renderLibrary();
  }
  function renderLibrary() {
    const box = $('psLibList'); clear(box);
    $('psLibCount').textContent = t('ps.lib.count', { n: ps.lib.length });
    if (!ps.lib.length) box.appendChild(el('div', 'empty', t('ps.lib.empty')));
    ps.lib.forEach(e => {
      const row = el('div', 'librow' + (ps.entry && ps.entry.name === e.name ? ' current' : ''));
      const head = el('div', 'head');
      head.appendChild(el('b', '', e.name)); head.appendChild(el('span', 'hint', `v${e.version} · ${t('ps.mode.' + e.mode)} · ${t('ps.target.' + (e.target || 'claude-code'))} · ${e.lang}`));
      const sc = el('span', 'badge ' + (e.score >= 80 ? 'ok' : e.score >= 50 ? 'warn' : 'bad'), String(e.score)); head.appendChild(sc);
      (e.tags || []).forEach(tag => head.appendChild(el('span', 'tag', tag)));
      row.appendChild(head);
      row.appendChild(el('div', 'hint idea', e.idea || ''));
      const acts = el('div', 'row');
      const open = el('button', 'ghost small', t('ps.lib.open')); open.type = 'button'; open.addEventListener('click', () => openEntry(e.name)); acts.appendChild(open);
      const send = el('button', 'ghost small', t('ps.send')); send.type = 'button'; send.addEventListener('click', async () => { try { const en = await api('/api/prompts/library?name=' + encodeURIComponent(e.name)); psSend(en.text, en.target, () => { ps.mode = en.mode; $('ps-idea').value = en.idea || ''; setTarget('uno'); renderModes(); psStart(); }); } catch (x) { psErr([x.message]); } }); acts.appendChild(send);
      const del = el('button', 'ghost small', t('ps.lib.delete')); del.type = 'button';
      del.addEventListener('click', async () => { if (!confirm(t('ps.lib.confirmDelete', { name: e.name }))) return; try { await api('/api/prompts/library/delete', { name: e.name }); if (ps.entry && ps.entry.name === e.name) ps.entry = null; loadLibrary(); } catch (x) { psErr([x.message]); } });
      acts.appendChild(del); row.appendChild(acts); box.appendChild(row);
    });
    const e = ps.entry; [$('psCmpA'), $('psCmpB')].forEach((sel, i) => {
      const cur = sel.value; clear(sel);
      (e ? e.versions : []).forEach(v => sel.appendChild(el('option', '', `${e.name} v${v}`)).value = String(v));
      sel.value = [...sel.options].some(o => o.value === cur) ? cur : (e ? String(e.versions[i === 0 ? Math.max(0, e.versions.length - 2) : e.versions.length - 1]) : '');
    });
    $('psCompare').disabled = !e || e.versions.length < 2;
  }
  async function openEntry(name, version) {
    try {
      const e = await api('/api/prompts/library?name=' + encodeURIComponent(name) + (version ? '&version=' + version : ''));
      const lint = await api('/api/prompts/lint', { text: e.text, target: e.target });
      ps.entry = e; ps.result = { text: e.text, lint, mode: e.mode, lang: e.lang, target: e.target, model: e.model, effort: e.effort, assumptions: [], open_questions: [], missing_roles: [], idea: e.idea, template: `v${e.version}` };
      renderResult(ps.result); $('psName').value = e.name; $('psTags').value = (e.tags || []).join(' '); renderLibrary(); psErr([]);
    } catch (x) { psErr([x.message]); }
  }
  $('psCompare').addEventListener('click', async () => {
    const e = ps.entry; if (!e) return;
    try {
      const r = await api('/api/prompts/diff', { name: e.name, a: parseInt($('psCmpA').value, 10), b: parseInt($('psCmpB').value, 10) });
      const d = $('psCmpDiff'); clear(d); d.appendChild(diffPre(r.diff || t('ps.lib.same')));
    } catch (x) { psErr([x.message]); }
  });
  $('psSearch').addEventListener('input', () => { clearTimeout(libTimer); libTimer = setTimeout(loadLibrary, 300); });
  $('psLibMode').addEventListener('change', loadLibrary);

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
    renderRoles();
    renderStudio();
  }

  // --- boot --------------------------------------------------------------------------------
  async function boot() {
    setMode(fragPref('mode') || pref('mode') || 'simple');
    await setLang(fragPref('lang') || pref('lang') || 'es');
    try {
      statusData = await api('/api/status');
      catalog = statusData.models || catalog;
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
    setTab(fragPref('tab') === 'prompts' ? 'prompts' : 'orchestra');
  }
  boot();
})();
