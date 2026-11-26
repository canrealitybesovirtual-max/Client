/* TradeComply CA web app. Vanilla JS, no build step, no CDN: works offline (app shell + field logs). */
(() => {
  'use strict';

  // ======================================================================== helpers
  const SVGNS = 'http://www.w3.org/2000/svg';
  const $ = (s, r = document) => r.querySelector(s);

  function icon(name, cls = 'icon') {
    const s = document.createElementNS(SVGNS, 'svg');
    s.setAttribute('class', cls);
    s.setAttribute('aria-hidden', 'true');
    s.setAttribute('focusable', 'false');
    const u = document.createElementNS(SVGNS, 'use');
    u.setAttribute('href', '#i-' + name);
    s.appendChild(u);
    return s;
  }

  function append(el, kids) {
    for (const k of kids.flat(Infinity)) {
      if (k === null || k === undefined || k === false) continue;
      el.appendChild(k instanceof Node ? k : document.createTextNode(String(k)));
    }
    return el;
  }

  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    let value;
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === undefined || v === null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'html') el.innerHTML = v;              // only ever used with escaped markdown output
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2), v);
      else if (k === 'value') value = v;
      else if (k === 'checked') el.checked = !!v;
      else if (v === true) el.setAttribute(k, '');
      else el.setAttribute(k, String(v));
    }
    append(el, kids);
    if (value !== undefined) el.value = value;
    return el;
  }

  // "Who do I ask?" text. Whoever runs this server can set SUPPORT_EMAIL / SUPPORT_URL (sent in /api/config); there
  // is no built-in contact, so without them customers are told to ask the people who run their TradeComply server.
  function supportText(what) {
    const sc = (state.config && state.config.support_contact) || {};
    const links = [];
    if (sc.email) links.push(h('a', { href: `mailto:${sc.email}` }, sc.email));
    if (sc.url && /^https?:\/\//i.test(sc.url)) links.push(h('a', { href: sc.url, target: '_blank', rel: 'noopener noreferrer' }, 'support page'));
    if (!links.length) return [`${what}, ask whoever runs this TradeComply server.`];
    return [`${what}, contact support: `, links.map((l, k) => (k ? [' or ', l] : l)), '.'];
  }

  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const WORDS = { cal: 'Cal', usd: '($)', cslb: 'CSLB', coi: 'COI', noc: 'NOC', wc: "workers' comp", dir: 'DIR', osha: 'OSHA', ccr: 'CCR',
    id: 'ID', iipp: 'IIPP', hers: 'HERS', pto: 'PTO', ca: 'CA', url: 'link', w9: 'W-9', ecpr: 'eCPR', ppe: 'PPE', smip: 'SMIP',
    ft: '(ft)', pct: '(%)', kwh: '(kWh)', naics: 'NAICS', llc: 'LLC', rme: 'RME', rmo: 'RMO', cpr: 'CPR', pwcr: 'PWCR', das: 'DAS',
    pwc: 'PWC', ui: 'UI', ett: 'ETT', sdi: 'SDI', edd: 'EDD', ein: 'EIN', fein: 'FEIN', gc: 'GC', cba: 'CBA', wvpp: 'WVPP', lc: 'LC', dob: 'date of birth',
    csv: 'CSV', adu: 'ADU', jadu: 'JADU', calgreen: 'CALGreen', hvac: 'HVAC', sqft: '(sq ft)', ev: 'EV', pv: 'PV', ceqa: 'CEQA', swppp: 'SWPPP',
    qsd: 'QSD', qsp: 'QSP', ahj: 'AHJ', mep: 'MEP', ada: 'ADA', hoa: 'HOA', ssn: 'SSN', itin: 'ITIN' };
  function humanize(name) {
    const words = String(name || '').replace(/(^|_)sq_ft(?=_|$)/g, '$1sqft').replace(/([a-z])([A-Z])/g, '$1 $2').split(/[_\s.]+/).filter(Boolean)
      .map((w) => WORDS[w.toLowerCase()] || w.toLowerCase().replace(/^([a-z]{2,})(\d)/, '$1 $2'));     // log300 -> log 300
    const s = words.join(' ').replace('Cal OSHA', 'Cal/OSHA').replace(/^lookup\b/, 'look up').replace(/^\(sq ft\)/, 'square feet').replace(/^\((.*?)\)/, '$1');
    return s.charAt(0).toUpperCase() + s.slice(1);
  }
  const plural = (n, word, many) => `${n} ${n === 1 ? word : (many || word + 's')}`;
  // Tool descriptions and hints are written for AI agents too: they name parameters in snake_case, show JSON shapes
  // and say "ISO date". Rewrite them for people, using the labels the form actually shows.
  function cleanHint(text, { params = {}, enumValues = [], boolean = false } = {}) {
    let s = String(text || '');
    if (!s) return '';
    s = s.replace(/\s*\[\{[^\]]*\}\]/g, '').replace(/\s*\{[^{}]*\b\w+_\w+\b[^{}]*\}/g, '');       // JSON shapes
    s = s.replace(/\{'([^']+)':\s*([^}]+)\}/g, '$1: $2').replace(/(^|\s)''(?=\s|$)/g, '$1blank');            // {'a': 1} -> a: 1
    s = s.replace(/\(ISO [^)]*\)/g, '').replace(/\bISO[- ]date\b/gi, 'Date').replace(/\bISO\b\s*/g, '');
    s = s.replace(/\(default ([a-z][a-z0-9_]*)\)/gi, (_, p) => `(leave blank to use ${params[p] ? `“${params[p]}”` : humanize(p).toLowerCase()})`);
    s = s.replace(/\bdefault(?:s to)? ([a-z][a-z0-9]*_[a-z0-9_]+)\b/gi, (_, p) => `defaults to ${params[p] ? `“${params[p]}”` : humanize(p).toLowerCase()}`);
    s = s.replace(/\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b(=\{[^}]*\})?/g, (m, p) => (params[p] ? `“${params[p]}”` : humanize(p).toLowerCase()));
    if (boolean) s = s.replace(/^True\b/, 'Yes').replace(/^False\b/, 'No').replace(/\btrue\b/g, 'yes').replace(/\bfalse\b/g, 'no');
    if (enumValues.length) {
      // a hint that only lists the choices adds nothing to the select
      const rest = enumValues.reduce((acc, v) => acc.split(humanize(v).toLowerCase()).join('').split(String(v)).join(''), s.toLowerCase());
      if (!rest.replace(/[\s,;:/.|]|\bor\b|\band\b/g, '')) return '';
    }
    s = s.replace(/\s{2,}/g, ' ').replace(/\s+([.,;:)])/g, '$1').replace(/\(\s*\)/g, '').replace(/(,\s*e\.g\.|[,;:])\s*$/, '').trim();
    return s.charAt(0).toUpperCase() + s.slice(1);
  }
  // Notes in tool output end with the rule ids they came from ("... [daily-weekly-overtime-1-5x]"); citations
  // already list the sources, so drop the ids from what people read.
  const stripRuleIds = (s) => String(s).replace(/\s*\[[a-z0-9]+(?:[-.][a-z0-9]+){2,}(?:,\s*[a-z0-9]+(?:[-.][a-z0-9]+)+)*\]/g, '');
  function firstSentence(text) {
    const t = String(text || '').replace(/\s+/g, ' ').trim();
    const m = t.match(/^(.{20,}?[.!?])\s+[A-Z(]/);
    return m && m[1].length < t.length ? m[1] : t;
  }
  function fmtDate(iso) {
    if (!iso) return '—';
    const m = String(iso).match(/^(\d{4})-(\d{2})-(\d{2})/);
    if (!m) return String(iso);
    const d = new Date(Date.UTC(+m[1], +m[2] - 1, +m[3]));
    return d.toLocaleDateString('en-US', { timeZone: 'UTC', month: 'short', day: 'numeric', year: 'numeric' });
  }
  function fmtDateTime(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return isNaN(d) ? String(iso) : d.toLocaleString('en-US', { month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit' });
  }
  function daysText(n) {
    if (n === 0) return 'today';
    if (n === 1) return 'tomorrow';
    if (n === -1) return 'yesterday';
    return n > 0 ? `in ${n} days` : `${-n} days ago`;
  }
  const money = (n) => (typeof n === 'number' ? n.toLocaleString('en-US', { style: 'currency', currency: 'USD',
    minimumFractionDigits: Number.isInteger(n) ? 0 : 2, maximumFractionDigits: 2 }) : n);
  function todayISO() {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
  }
  function cookie(name) {
    const m = document.cookie.match(new RegExp('(?:^|; )' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : '';
  }
  function uuid() {
    if (crypto.randomUUID) return crypto.randomUUID();
    const b = crypto.getRandomValues(new Uint8Array(16));
    b[6] = (b[6] & 0x0f) | 0x40; b[8] = (b[8] & 0x3f) | 0x80;
    const hex = Array.from(b, (x) => x.toString(16).padStart(2, '0')).join('');
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
  }
  let toastTimer;
  function toast(msg, isError = false) {
    const t = $('#toast');
    t.textContent = msg;
    t.className = 'toast show' + (isError ? ' error' : '');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.className = 'toast'; }, isError ? 6000 : 3500);
  }
  async function copyText(text, what = 'Copied') {
    try { await navigator.clipboard.writeText(text); toast(what); } catch (e) {
      const ta = h('textarea', { class: 'visually-hidden' }); ta.value = text; document.body.append(ta); ta.select();
      try { document.execCommand('copy'); toast(what); } catch (e2) { toast('Copy failed. Select the text and copy it.', true); }
      ta.remove();
    }
  }
  function saveBlob(filename, blob) {
    const url = URL.createObjectURL(blob);
    const a = h('a', { href: url, download: filename });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  }
  const download = (filename, text, type = 'text/plain') => saveBlob(filename, new Blob([text], { type }));
  function copyBox(text, label, code = false) {
    const pre = h('pre', { class: 'mono' + (code ? ' code' : ''), tabindex: code ? '0' : null }, text);
    return h('div', { class: 'copy-box' }, pre, h('button', { type: 'button', class: 'btn secondary small', 'aria-label': 'Copy ' + (label || ''), onclick: () => copyText(text) }, 'Copy'));
  }
  function busy(btn, on, text) {
    if (!btn) return;
    if (on) { btn.dataset.label = btn.textContent; btn.disabled = true; btn.textContent = text || 'Working…'; } else { btn.disabled = false; if (btn.dataset.label) btn.textContent = btn.dataset.label; }
  }
  // Per-browser conveniences only (a hidden checklist, the last job picked). Storage can be blocked or throw (private
  // windows, strict settings, full disk), so every read and write is guarded and the page works without it.
  const local = {
    get(k) { try { return window.localStorage.getItem(k); } catch (e) { return null; } },
    set(k, v) { try { window.localStorage.setItem(k, v); return true; } catch (e) { return false; } },
    del(k) { try { window.localStorage.removeItem(k); } catch (e) { /* storage unavailable */ } },
  };
  const reducedMotion = () => { try { return window.matchMedia('(prefers-reduced-motion: reduce)').matches; } catch (e) { return false; } };
  const scrollBehavior = () => (reducedMotion() ? 'auto' : 'smooth');

  // ======================================================================== API
  class ApiError extends Error {
    constructor(status, message, data) { super(message); this.status = status; this.data = data; }
  }
  function errMessage(data, status) {
    const d = data && data.detail !== undefined ? data.detail : data;
    if (typeof d === 'string' && d) return d;
    if (Array.isArray(d)) return d.map((e) => `${humanize((e.loc || []).filter((x) => x !== 'body').join(' ')) || 'Input'}: ${e.msg}`).join('. ');
    if (d && typeof d === 'object' && d.message) return d.message;
    return status === 0 ? 'You appear to be offline.' : `Something went wrong (${status}).`;
  }
  async function api(path, { method = 'GET', body, form, raw, contentType, keepSession = false } = {}) {
    const opts = { method, headers: {}, credentials: 'same-origin' };
    if (method !== 'GET') opts.headers['X-CSRF-Token'] = cookie('tc_csrf');
    if (form) opts.body = form;
    else if (raw !== undefined) { opts.body = raw; opts.headers['Content-Type'] = contentType || 'text/plain'; }
    else if (body !== undefined) { opts.headers['Content-Type'] = 'application/json'; opts.body = JSON.stringify(body); }
    let res;
    try { res = await fetch(path, opts); } catch (e) { throw new ApiError(0, 'You appear to be offline. Try again when you have signal.'); }
    const ct = res.headers.get('content-type') || '';
    const data = ct.includes('json') ? await res.json().catch(() => null) : await res.text();
    if (!res.ok) {
      const err = new ApiError(res.status, errMessage(data, res.status), data);
      // keepSession: a 401 that means "wrong password" (closing the account) must not sign the person out
      if (res.status === 401 && !keepSession && !path.startsWith('/api/auth/') && state.me) {
        state.me = null;
        if (parseHash().name === 'connect') render(); else go('login');   // the consent shows the login and keeps its request
      }
      if (res.status === 402 && state.me) { state.me.entitlement = { ...(state.me.entitlement || {}), ok: false, message: err.message }; renderBanner(); }
      throw err;
    }
    return data;
  }

  // ======================================================================== state + router
  const state = { me: null, config: null, tools: null, online: navigator.onLine, renderSeq: 0, gsAway: null };
  const can = {
    write: () => state.me && ['owner', 'office'].includes(state.me.user.role),
    owner: () => state.me && state.me.user.role === 'owner',
  };
  const entitled = () => !state.me || !state.me.entitlement || state.me.entitlement.ok !== false;

  async function loadMe() {
    try {
      const [me, config] = await Promise.all([api('/api/me'), state.config ? state.config : api('/api/config')]);
      state.me = me; state.config = config;
    } catch (e) {
      state.me = null;
      if (!state.config) { try { state.config = await api('/api/config'); } catch (e2) { /* offline */ } }
    }
    $('#company').textContent = state.me ? state.me.tenant.company_name : '';
    $('#nav').hidden = !state.me;
    return state.me;
  }

  function parseHash() {
    const raw = location.hash.replace(/^#\/?/, '');
    const [path, qs] = raw.split('?');
    const parts = path.split('/').filter(Boolean).map(decodeURIComponent);
    return { name: parts[0] || '', args: parts.slice(1), query: new URLSearchParams(qs || '') };
  }
  const go = (path) => { if (location.hash === '#/' + path) render(); else location.hash = '#/' + path; };

  const routes = {};
  const PUBLIC = new Set(['login', 'signup']);
  const TITLES = { dashboard: 'Dashboard', projects: 'Projects', project: 'Project', subs: 'Subcontractors', tools: 'Tools', documents: 'Documents', document: 'Document', field: 'Field', integrations: 'Integrations', professionals: 'Find a professional', settings: 'Settings', welcome: 'Get set up', login: 'Log in', signup: 'Start free trial', connect: 'Connect an app' };

  async function render() {
    const seq = ++state.renderSeq;
    let { name, args, query } = parseHash();
    if (!state.me && !PUBLIC.has(name)) await loadMe();
    if (!state.me && !PUBLIC.has(name)) name = 'login';
    if (state.me && PUBLIC.has(name)) name = 'dashboard';
    if (!routes[name]) name = state.me ? 'dashboard' : 'login';
    document.querySelectorAll('#nav a').forEach((a) => {
      const r = a.dataset.route;
      const cur = r === name || (r === 'projects' && name === 'project') || (r === 'documents' && name === 'document');
      if (cur) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
    });
    renderBanner();
    const main = $('#main');
    main.replaceChildren(h('p', { class: 'loading' }, 'Loading…'));
    let view;
    try { view = await routes[name]({ args, query }); } catch (e) { view = errorCard(e); }
    if (seq !== state.renderSeq) return;
    main.replaceChildren(view);
    document.title = `${TITLES[name] || 'TradeComply'} · TradeComply CA`;
    main.focus({ preventScroll: true });
    window.scrollTo(0, 0);
  }

  function errorCard(e) {
    if (e && e.status === 402) return subscribeCard(e.message);
    return h('div', { class: 'card' }, h('h1', {}, 'Something went wrong'), h('p', {}, e && e.message ? e.message : String(e)),
      h('button', { class: 'btn secondary', type: 'button', onclick: () => render() }, 'Try again'));
  }

  // ======================================================================== banners + billing
  async function subscribe(btn) {
    busy(btn, true, 'Opening checkout…');
    try { const r = await api('/api/billing/checkout', { method: 'POST' }); location.href = r.url; } catch (e) { toast(e.message, true); busy(btn, false); }
  }
  async function billingPortal(btn) {
    busy(btn, true, 'Opening…');
    try { const r = await api('/api/billing/portal', { method: 'POST' }); location.href = r.url; } catch (e) { toast(e.message, true); busy(btn, false); }
  }
  function subscribeCard(message) {
    return h('div', { class: 'card' },
      h('h1', {}, 'Subscribe to keep your compliance checks running'),
      h('p', {}, message || 'Your free trial has ended.'),
      h('p', { class: 'muted' }, `TradeComply Core is $${((state.config && state.config.price_usd_month) || 1000).toLocaleString('en-US')}/month: license and sub monitoring, lien calendar, Cal/OSHA programs, payroll checks, field logs and AI-agent access. Your projects, subs and documents stay available.`),
      can.owner() ? h('button', { class: 'btn', type: 'button', onclick: (e) => subscribe(e.currentTarget) }, 'Subscribe now') : h('p', {}, 'Ask your account owner to subscribe.'));
  }
  function renderBanner() {
    const slot = $('#banner');
    slot.replaceChildren();
    if (!state.me) return;
    const ent = state.me.entitlement || {};
    if (state.me.user.must_change_password && parseHash().name !== 'connect') {      // the consent asks for it itself
      slot.append(h('div', { class: 'banner warn', role: 'status' }, icon('alert'), h('p', {}, 'You are using a temporary password. Set your own password now.'),
        h('a', { class: 'btn small secondary', href: '#/settings' }, 'Change password')));
    }
    if (ent.ok === false) {
      slot.append(h('div', { class: 'banner error', role: 'alert' }, icon('stop'), h('p', {}, ent.message || 'Subscription required.'),
        can.owner() ? h('button', { class: 'btn small', type: 'button', onclick: (e) => subscribe(e.currentTarget) }, 'Subscribe') : null));
    } else if (ent.warning) {
      slot.append(h('div', { class: 'banner warn', role: 'status' }, icon('alert'), h('p', {}, ent.message),
        can.owner() ? h('button', { class: 'btn small secondary', type: 'button', onclick: (e) => billingPortal(e.currentTarget) }, 'Update card') : null));
    } else if (ent.reason === 'trial' && ent.trial_days_left <= 7) {
      slot.append(h('div', { class: 'banner info', role: 'status' }, h('p', {}, `${ent.message}.`),
        can.owner() ? h('button', { class: 'btn small secondary', type: 'button', onclick: (e) => subscribe(e.currentTarget) }, 'Subscribe') : null));
    }
  }

  // ======================================================================== shared UI pieces
  const RAG_TEXT = { red: 'Act now', amber: 'Coming up', green: 'On track' };
  const RAG_ICON = { red: 'stop', amber: 'alert', green: 'check' };
  const ragPill = (rag, text) => h('span', { class: `pill ${rag || 'gray'}` }, rag ? icon(RAG_ICON[rag]) : null, text || RAG_TEXT[rag] || 'Unknown');
  const RISK_TEXT = { low: 'Low risk', medium: 'Medium risk', high: 'High risk', do_not_use: 'Do not use' };

  function verifiedInfo(r) {
    if (!r || r.verified !== true) return { ok: false };
    const dates = [];
    const walk = (v, depth) => {
      if (!v || typeof v !== 'object' || depth > 4) return;
      if (Array.isArray(v)) { v.forEach((x) => walk(x, depth + 1)); return; }
      for (const [k, x] of Object.entries(v)) {
        if (k === 'checked_on' && typeof x === 'string' && /^\d{4}-\d{2}-\d{2}/.test(x)) dates.push(x.slice(0, 10));
        else if (typeof x === 'string') { const m = x.match(/snapshot (\d{4}-\d{2}-\d{2})/); if (m) dates.push(m[1]); } else walk(x, depth + 1);
      }
    };
    walk(r, 0);
    dates.sort();
    return { ok: true, date: dates[0] };
  }
  function verifiedBadge(r) {
    const v = verifiedInfo(r);
    if (v.ok) return h('span', { class: 'verify yes' }, icon('check'), v.date ? `Verified against official source on ${fmtDate(v.date)}` : 'Verified against official source');
    return h('span', { class: 'verify no' }, icon('alert'), 'Not yet verified');
  }
  function citationList(cites) {
    if (!cites || !cites.length) return null;
    return h('ul', { class: 'citations bullets' }, cites.map((c) => {
      if (typeof c === 'string') return h('li', {}, c);
      const src = c.source && /^https?:\/\//.test(c.source) ? h('a', { href: c.source, target: '_blank', rel: 'noopener noreferrer' }, c.citation || c.rule_id || c.source) : (c.citation || c.rule_id || '');
      const ok = ['confirmed', 'corrected'].includes(c.status);
      return h('li', {}, src, c.source && !/^https?:/.test(c.source) ? ` (${c.source})` : '', ' ',
        c.status ? h('span', { class: `pill ${ok ? 'green' : 'amber'}` }, ok ? 'verified' : 'unverified') : null,
        c.checked_on ? h('span', { class: 'muted small' }, ` checked ${fmtDate(c.checked_on)}`) : null);
    }));
  }
  const disclaimerBox = (text) => text ? h('p', { class: 'disclaimer' }, text) : null;

  function field(label, input, { hint, required, wide } = {}) {
    const id = input.id || ('f' + Math.random().toString(36).slice(2, 9));
    input.id = id;
    if (required) input.required = true;
    let hintEl = null;
    if (hint) { hintEl = h('span', { class: 'hint', id: id + '-h' }, hint); input.setAttribute('aria-describedby', id + '-h'); }
    return h('div', { class: 'field' + (wide ? ' wide' : '') }, h('label', { for: id }, label, required ? h('span', { class: 'req', 'aria-hidden': 'true' }, '*') : null), input, hintEl);
  }
  const input = (name, attrs = {}) => h('input', { name, type: 'text', ...attrs });
  function select(name, options, value, attrs = {}) {
    return h('select', { name, ...attrs, value: value ?? '' }, options.map(([v, label]) => h('option', { value: v }, label)));
  }
  function formData(form) {
    const out = {};
    for (const el of form.elements) {
      if (!el.name) continue;
      if (el.type === 'checkbox') out[el.name] = el.checked;
      else if (el.type === 'number') out[el.name] = el.value === '' ? null : Number(el.value);
      else out[el.name] = el.value.trim() === '' ? null : el.value.trim();
    }
    return out;
  }
  function openDialog(title, content) {
    const dlg = h('dialog', { 'aria-labelledby': 'dlg-title' });
    const close = () => { dlg.close(); dlg.remove(); };
    dlg.append(h('div', { class: 'dialog-body' }, h('div', { class: 'card-head' }, h('h2', { id: 'dlg-title' }, title),
      h('button', { type: 'button', class: 'btn ghost small', onclick: close, 'aria-label': 'Close' }, 'Close')), content));
    dlg.addEventListener('cancel', () => setTimeout(() => dlg.remove(), 0));
    document.body.append(dlg);
    dlg.showModal();
    return { close, el: dlg };
  }
  function table(columns, rows, { cards = true } = {}) {
    return h('div', { class: 'table-wrap' }, h('table', { class: 'data' + (cards ? ' cards' : '') },
      h('thead', {}, h('tr', {}, columns.map((c) => h('th', { scope: 'col' }, c.label)))),
      h('tbody', {}, rows.map((r) => h('tr', {}, columns.map((c) => h('td', { 'data-label': c.bare ? null : c.label, class: c.bare ? 'cell-main' : null }, c.render ? c.render(r) : (r[c.key] ?? '—'))))))));
  }

  // ======================================================================== markdown (escaped first, then a small safe subset)
  function inlineMd(s) {
    // Code spans and links are swapped for opaque placeholders as soon as they are built, so no later rule
    // (autolinks, emphasis) can ever run inside generated markup: earlier, "[a](https://b(https://x/onfocus=...)"
    // nested a second <a> inside the first href and smuggled attributes into the tag.
    const slots = [];
    const keep = (html) => '\u0000' + (slots.push(html) - 1) + '\u0000';
    const link = (href, text) => keep(`<a href="${href}" target="_blank" rel="noopener noreferrer">${text}</a>`);
    s = esc(String(s ?? '').replace(/\u0000/g, ''));
    s = s.replace(/`([^`]+)`/g, (_, c) => keep(`<code>${c}</code>`));
    s = s.replace(/\[([^\]\u0000]+)\]\(((?:https?:\/\/|mailto:)[^\s()\u0000]+)\)/g, (_, text, href) => link(href, text));
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s()<\u0000]+?)([.,;:!?]*)(?=[\s()<\u0000]|$)/g, (_, pre, href, tail) => pre + link(href, href) + tail);
    s = s.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    s = s.replace(/(^|[\s(])\*([^*\s][^*]*?)\*(?=[\s).,;:!?]|$)/g, '$1<em>$2</em>');
    s = s.replace(/(^|[\s(])_([^_\s][^_]*?)_(?=[\s).,;:!?]|$)/g, '$1<em>$2</em>');
    s = s.replace(/\[ \]/g, '☐').replace(/\[x\]/gi, '☑');
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => slots[+i]);
  }
  function splitRow(line) {
    return line.trim().replace(/^\|/, '').replace(/\|$/, '').split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, '|'));
  }
  function md(src) {
    const lines = String(src || '').replace(/\r\n?/g, '\n').split('\n');
    const out = [];
    let i = 0;
    const isBlock = (l) => /^(#{1,6}\s|```|>|\s*([-*+]|\d+[.)])\s+|\s*\|.*\|\s*$|(-{3,}|\*{3,}|_{3,})\s*$)/.test(l);
    while (i < lines.length) {
      const line = lines[i];
      if (!line.trim()) { i++; continue; }
      let m;
      if ((m = line.match(/^(#{1,6})\s+(.*)$/))) { out.push(`<h${m[1].length}>${inlineMd(m[2])}</h${m[1].length}>`); i++; continue; }
      if (/^(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) { out.push('<hr>'); i++; continue; }
      if (/^```/.test(line)) {
        const buf = []; i++;
        while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
        i++; out.push(`<pre><code>${esc(buf.join('\n'))}</code></pre>`); continue;
      }
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
        const head = splitRow(line); i += 2;
        const body = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) body.push(splitRow(lines[i++]));
        out.push('<table><thead><tr>' + head.map((c) => `<th>${inlineMd(c)}</th>`).join('') + '</tr></thead><tbody>' +
          body.map((r) => '<tr>' + r.map((c) => `<td>${inlineMd(c)}</td>`).join('') + '</tr>').join('') + '</tbody></table>');
        continue;
      }
      if (/^>\s?/.test(line)) {
        const buf = [];
        while (i < lines.length && /^>\s?/.test(lines[i])) buf.push(lines[i++].replace(/^>\s?/, ''));
        out.push(`<blockquote>${md(buf.join('\n'))}</blockquote>`); continue;
      }
      if (/^\s*([-*+]|\d+[.)])\s+/.test(line)) {
        const ordered = /^\s*\d+[.)]\s+/.test(line);
        const items = [];
        while (i < lines.length && /^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) {
          let item = lines[i++].replace(/^\s*([-*+]|\d+[.)])\s+/, '');
          while (i < lines.length && /^\s{2,}\S/.test(lines[i]) && !/^\s*([-*+]|\d+[.)])\s+/.test(lines[i])) item += ' ' + lines[i++].trim();
          items.push(`<li>${inlineMd(item)}</li>`);
        }
        out.push(`<${ordered ? 'ol' : 'ul'}>${items.join('')}</${ordered ? 'ol' : 'ul'}>`); continue;
      }
      const buf = [];
      while (i < lines.length && lines[i].trim() && !(buf.length && isBlock(lines[i]))) buf.push(lines[i++]);
      out.push(`<p>${buf.map((l) => inlineMd(l.trim())).join('<br>')}</p>`);
    }
    return out.join('\n');
  }

  // ======================================================================== tools: discovery + schema forms
  async function getTools(refresh = false) {
    if (!state.tools || refresh) state.tools = await api('/api/tools');
    return state.tools;
  }
  let fieldSeq = 0;
  const omit = (o, ...keys) => Object.fromEntries(Object.entries(o).filter(([k]) => !keys.includes(k)));
  const defined = (o) => Object.fromEntries(Object.entries(o).filter(([, v]) => v !== undefined));
  function resolveRef(s, root) {
    let guard = 0;
    while (s && s.$ref && guard++ < 20) {
      const name = s.$ref.replace(/^#\/(\$defs|definitions)\//, '');
      const target = (root.$defs || {})[name] || (root.definitions || {})[name] || {};
      s = { ...target, ...omit(s, '$ref') };
    }
    return s || {};
  }
  function norm(schema, root) {
    let s = resolveRef(schema, root);
    if (s.allOf && s.allOf.length === 1) s = { ...resolveRef(s.allOf[0], root), ...omit(s, 'allOf') };
    const alts = s.anyOf || s.oneOf;
    if (alts) {
      const opts = alts.map((a) => resolveRef(a, root));
      const nonNull = opts.filter((a) => a.type !== 'null');
      const meta = defined({ title: s.title, description: s.description, default: s.default });
      let base;
      if (nonNull.length && nonNull.every((a) => a.const !== undefined || a.enum)) base = { type: 'string', enum: nonNull.flatMap((a) => a.enum || [a.const]) };
      else if (nonNull.length > 1) {
        // e.g. "a list of rows, or CSV text": let the user pick how to enter it
        const csvText = /\bCSV\b/.test(s.description || '');
        return { ...meta, nullable: nonNull.length < opts.length,
          alternatives: nonNull.map((a) => (a.type === 'string' && csvText ? { ...a, 'x-csv': true } : a)) };
      } else base = nonNull[0] || { type: 'string' };
      return { ...base, ...meta, nullable: nonNull.length < opts.length };
    }
    if (Array.isArray(s.type)) { const t = s.type.filter((x) => x !== 'null'); return { ...s, type: t[0] || 'string', nullable: t.length < s.type.length }; }
    if (s.const !== undefined && !s.enum) return { ...s, enum: [s.const] };
    return s;
  }
  const SIMPLE = ['number', 'integer', 'string', 'boolean'];
  function kindOf(name, s, root) {
    if (s.enum) return 'enum';
    if (s.alternatives) return 'choice';
    const t = s.type || (s.properties ? 'object' : 'string');
    if (t === 'boolean') return s.default === undefined || s.default === null ? 'tristate' : 'bool';
    if (t === 'integer' || t === 'number') return 'number';
    if (t === 'array') {
      const it = norm(s.items || {}, root);
      if (it.enum) return 'multienum';
      if (it.type === 'object' || it.properties) return it.properties ? 'rows' : 'json';
      return 'list';
    }
    if (t === 'object') {
      if (s.properties) return 'object';
      const ap = s.additionalProperties && typeof s.additionalProperties === 'object' ? norm(s.additionalProperties, root) : null;
      return ap && SIMPLE.includes(ap.type) ? 'map' : 'json';
    }
    if (s['x-csv']) return 'csv';
    const desc = s.description || '';
    if (s.format === 'time' || (/(^|_)(start|end|time|clock_in|clock_out|time_in|time_out)$/.test(name) && /\b\d{1,2}:\d{2}\b|\bclock\b/i.test(desc))) return 'time';
    if (s.format === 'date' || /(^|_)(date|expiry|expiration|deadline|dob)$/.test(name) || /\bISO date\b/i.test(desc)) return 'date';
    if (s.format === 'date-time') return 'datetime';
    if (/csv/i.test(name) || /\bCSV\b/.test(desc)) return 'csv';
    if ((s.maxLength || 0) > 300 || /(notes|text|markdown|description|body|details|comments|narrative)$/.test(name)) return 'textarea';
    return 'text';
  }
  function enumLabel(v) {
    const roles = (state.config && state.config.claimant_roles) || {};
    if (roles[v]) return roles[v];
    return typeof v === 'string' ? humanize(v) : String(v);
  }
  function labelFor(name, s, kind) {
    const auto = String(name).split('_').map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(' ');
    if (s.title && s.title !== auto) return s.title;
    if ((kind === 'bool' || kind === 'tristate') && /^is_/.test(name)) return humanize(name.slice(3)) + '?';
    return humanize(name);
  }
  // parameter name -> the label people see, for every property in the schema (top level and nested), so hints
  // like "must match a key in hourly_rates" can say "Hourly rates" instead
  const paramLabelCache = new WeakMap();
  function paramLabels(root) {
    if (paramLabelCache.has(root)) return paramLabelCache.get(root);
    const out = {};
    const visit = (s, depth) => {
      if (!s || typeof s !== 'object' || depth > 6) return;
      for (const [k, v] of Object.entries(s.properties || {})) { if (!out[k]) out[k] = humanize(k); visit(v, depth + 1); }
      for (const key of ['items', 'additionalProperties']) if (s[key] && typeof s[key] === 'object') visit(s[key], depth + 1);
      for (const key of ['anyOf', 'oneOf', 'allOf']) (s[key] || []).forEach((x) => visit(x, depth + 1));
    };
    visit(root, 0);
    Object.values(root.$defs || root.definitions || {}).forEach((d) => visit(d, 1));
    paramLabelCache.set(root, out);
    return out;
  }
  const ADVANCED = /^(as_of|today|evaluation_date)$/;

  function buildField(name, schema, root, required, prefix = '') {
    const s = norm(schema, root);
    const kind = kindOf(name, s, root);
    const id = `tf-${++fieldSeq}`;
    const label = labelFor(name, s, kind);
    const enumValues = s.enum || (kind === 'multienum' ? (norm(s.items, root).enum || []) : []);
    let desc = cleanHint(s.description, { params: paramLabels(root), enumValues, boolean: kind === 'bool' || kind === 'tristate' });
    if (ADVANCED.test(name) && kind === 'date') desc = 'Leave blank to check as of today (California time).';
    // A field that takes "rows or CSV" describes both formats at length; the radio buttons already explain the
    // choice, so the format details go in a disclosure.
    let formatDetails = null;
    if (kind === 'choice' && desc.length > 140) { formatDetails = desc; desc = 'Enter the rows below, or paste CSV text.'; }
    const hintText = [desc, kind === 'list' ? 'One per line.' : '', kind === 'csv' ? 'Paste CSV text or load a .csv file.' : ''].filter(Boolean).join(' ');
    const hint = hintText ? h(formatDetails ? 'div' : 'span', { class: 'hint', id: id + '-h' }, hintText,
      formatDetails ? h('details', { class: 'format-details' }, h('summary', {}, 'Format details'), formatDetails) : null) : null;
    const err = h('span', { class: 'error-text', id: id + '-e' });
    const described = [hint ? id + '-h' : '', id + '-e'].filter(Boolean).join(' ');
    const lbl = () => h('label', { for: id }, label, required ? h('span', { class: 'req', 'aria-hidden': 'true' }, '*') : null);
    const def = s.default !== undefined && s.default !== null ? s.default : undefined;
    let el, get, isEmpty, wide = false;
    const fail = (msg, target) => { err.textContent = msg; if (target) target.setAttribute('aria-invalid', 'true'); return msg; };
    const ok = (target) => { err.textContent = ''; if (target) target.removeAttribute('aria-invalid'); return ''; };

    const star = () => (required ? h('span', { class: 'req', 'aria-hidden': 'true' }, '*') : null);
    if (kind === 'choice') {
      const altLabel = (a) => (a['x-csv'] ? 'Paste CSV' : a.type === 'array' ? 'Enter rows' : a.type === 'object' ? 'Fill in fields'
        : a.type === 'string' ? 'Type text' : humanize(a.type || 'value'));
      const subs = s.alternatives.map((a) => buildField(name, { ...a, title: altLabel(a), description: undefined }, root, required, prefix));
      let active = Math.max(0, s.alternatives.findIndex((a) => a['x-csv']));
      const show = () => subs.forEach((f, k) => { f.el.hidden = k !== active; });
      const radios = s.alternatives.map((a, k) => h('label', {}, h('input', { type: 'radio', name: 'alt-' + id, value: String(k), checked: k === active,
        onchange: () => { active = k; show(); } }), altLabel(a)));
      el = h('fieldset', { class: 'field wide' }, h('legend', {}, label, star()), hint,
        h('div', { class: 'checks', role: 'radiogroup', 'aria-label': `How to enter ${label}` }, radios), subs.map((f) => f.el), err);
      show();
      wide = true;
      get = () => subs[active].get();
      isEmpty = () => subs[active].isEmpty();
    } else if (kind === 'tristate') {
      const cur = def === true ? 'true' : def === false ? 'false' : '';
      const sel = select(name, [['', required ? 'Choose…' : 'Not answered'], ['true', 'Yes'], ['false', 'No']], cur, { id, 'aria-describedby': described });
      el = h('div', { class: 'field' }, lbl(), sel, hint, err);
      get = () => {
        if (sel.value === '') return { value: undefined, error: required ? fail('Choose Yes or No', sel) : ok(sel) };
        ok(sel); return { value: sel.value === 'true', error: '' };
      };
      isEmpty = () => sel.value === '';
    } else if (kind === 'map') {
      const vs = norm(s.additionalProperties, root);
      const isNum = vs.type === 'number' || vs.type === 'integer';
      const rows = [];
      const list = h('div', {});
      const addRow = (k = '', v = '') => {
        const keyIn = h('input', { type: 'text', 'aria-label': `${label}: name`, placeholder: 'Name', value: k, autocomplete: 'off' });
        const valIn = vs.type === 'boolean' ? h('input', { type: 'checkbox', 'aria-label': `${label}: yes or no`, checked: v === 'true' })
          : h('input', { type: isNum ? 'number' : 'text', step: isNum ? 'any' : undefined, inputmode: isNum ? 'decimal' : undefined,
            'aria-label': `${label}: value`, placeholder: isNum ? '0' : 'Value', value: v });
        const row = h('div', { class: 'row map-row' }, keyIn, valIn);
        const entry = { keyIn, valIn, row };
        row.append(h('button', { type: 'button', class: 'btn ghost small', onclick: () => { rows.splice(rows.indexOf(entry), 1); row.remove(); } }, 'Remove'));
        rows.push(entry); list.append(row);
        return entry;
      };
      if (def && typeof def === 'object') Object.entries(def).forEach(([k, v]) => addRow(k, String(v))); else addRow();
      el = h('fieldset', { class: 'field wide' }, h('legend', {}, label, star()), hint, list,
        h('button', { type: 'button', class: 'btn secondary small', onclick: () => addRow().keyIn.focus() }, '+ Add'), err);
      wide = true;
      const rawOf = (r) => (vs.type === 'boolean' ? r.valIn.checked : r.valIn.value.trim());
      get = () => {
        const out = {};
        for (const r of rows) {
          const k = r.keyIn.value.trim();
          const raw = rawOf(r);
          if (!k && (raw === '' || raw === false)) continue;
          if (!k) return { value: undefined, error: fail('Each value needs a name') };
          if (isNum) {
            const n = Number(raw);
            if (raw === '' || !isFinite(n)) return { value: undefined, error: fail(`Enter a number for ${k}`) };
            out[k] = n;
          } else out[k] = raw;
        }
        if (!Object.keys(out).length) return { value: undefined, error: required ? fail('Add at least one') : ok() };
        ok(); return { value: out, error: '' };
      };
      isEmpty = () => rows.every((r) => !r.keyIn.value.trim() && (rawOf(r) === '' || rawOf(r) === false));
    } else if (kind === 'bool') {
      const cb = h('input', { type: 'checkbox', id, role: 'switch', checked: def === true, 'aria-describedby': described });
      el = h('div', { class: 'field' }, h('label', { class: 'switch', for: id }, cb, h('span', {}, label)), hint, err);
      get = () => ({ value: cb.checked, error: '' });
      isEmpty = () => cb.checked === (def === true);
    } else if (kind === 'enum') {
      const opts = (required && def !== undefined ? [] : [['', required ? 'Choose…' : '— Not set —']]).concat(s.enum.map((v) => [String(v), enumLabel(v)]));
      const sel = select(name, opts, def !== undefined ? String(def) : '', { id, 'aria-describedby': described });
      el = h('div', { class: 'field' }, lbl(), sel, hint, err);
      get = () => {
        if (sel.value === '') return { value: undefined, error: required ? fail('Choose an option', sel) : ok(sel) };
        ok(sel);
        const orig = s.enum.find((v) => String(v) === sel.value);
        return { value: orig !== undefined ? orig : sel.value, error: '' };
      };
      isEmpty = () => sel.value === '' || (def !== undefined && sel.value === String(def));
    } else if (kind === 'multienum') {
      const items = norm(s.items, root);
      const boxes = items.enum.map((v) => h('input', { type: 'checkbox', value: String(v), checked: Array.isArray(def) && def.includes(v) }));
      el = h('fieldset', { class: 'field wide', 'aria-describedby': described }, h('legend', {}, label), h('div', { class: 'checks' }, boxes.map((b, k) => h('label', {}, b, enumLabel(items.enum[k])))), hint, err);
      wide = true;
      get = () => {
        const vals = boxes.filter((b) => b.checked).map((b) => items.enum.find((v) => String(v) === b.value));
        if (!vals.length) return { value: undefined, error: required ? fail('Pick at least one') : ok() };
        ok(); return { value: vals, error: '' };
      };
      isEmpty = () => !boxes.some((b) => b.checked);
    } else if (kind === 'number') {
      const inp = h('input', { type: 'number', id, inputmode: s.type === 'integer' ? 'numeric' : 'decimal', step: s.type === 'integer' ? '1' : 'any',
        min: s.minimum ?? (s.exclusiveMinimum !== undefined ? s.exclusiveMinimum : undefined), max: s.maximum, value: def !== undefined ? String(def) : '', 'aria-describedby': described });
      el = h('div', { class: 'field' }, lbl(), inp, hint, err);
      get = () => {
        if (inp.value === '') return { value: undefined, error: required ? fail('Required', inp) : ok(inp) };
        const n = Number(inp.value);
        if (!isFinite(n) || (s.type === 'integer' && !Number.isInteger(n))) return { value: undefined, error: fail(s.type === 'integer' ? 'Enter a whole number' : 'Enter a number', inp) };
        if (s.exclusiveMinimum !== undefined && n <= s.exclusiveMinimum) return { value: undefined, error: fail(`Must be more than ${s.exclusiveMinimum}`, inp) };
        if (s.minimum !== undefined && n < s.minimum) return { value: undefined, error: fail(`Must be at least ${s.minimum}`, inp) };
        if (s.maximum !== undefined && n > s.maximum) return { value: undefined, error: fail(`Must be at most ${s.maximum}`, inp) };
        ok(inp); return { value: n, error: '' };
      };
      isEmpty = () => inp.value === '' || (def !== undefined && inp.value === String(def));
    } else if (kind === 'rows' || kind === 'object') {
      const objSchema = kind === 'rows' ? norm(s.items, root) : s;
      const props = objSchema.properties || {};
      const req = new Set(objSchema.required || []);
      const makeGroup = () => Object.entries(props).map(([n, ps]) => ({ name: n, ...buildField(n, ps, root, req.has(n), prefix + name + '.') }));
      const blank = (group) => group.every((f) => f.isEmpty());
      const collect = (group) => {
        const out = {}; let error = '';
        for (const f of group) {
          const r = f.get();
          if (r.error) error = error || r.error;
          if (r.value !== undefined && r.value !== '') out[f.name] = r.value;
        }
        return { value: Object.keys(out).length ? out : undefined, error };
      };
      wide = true;
      if (kind === 'object') {
        const group = makeGroup();
        el = h('fieldset', { class: 'field wide' }, h('legend', {}, label, required ? h('span', { class: 'req', 'aria-hidden': 'true' }, '*') : null), hint, h('div', { class: 'form-grid' }, group.map((f) => f.el)), err);
        get = () => {
          if (blank(group)) return { value: undefined, error: required ? fail('Fill in this section') : ok() };
          ok(); return collect(group);
        };
        isEmpty = () => blank(group);
      } else {
        const rows = [];
        const list = h('div', {});
        const addRow = () => {
          const group = makeGroup();
          const rowEl = h('div', { class: 'repeat-row' });
          const remove = h('button', { type: 'button', class: 'btn ghost small', onclick: () => { rows.splice(rows.indexOf(entry), 1); rowEl.remove(); renumber(); } }, 'Remove');
          const title = h('span', {});
          rowEl.append(h('div', { class: 'repeat-row-head' }, title, remove), h('div', { class: 'form-grid' }, group.map((f) => f.el)));
          const entry = { group, rowEl, title };
          rows.push(entry); list.append(rowEl); renumber();
          return entry;
        };
        const singular = humanize(name).replace(/s$/, '');
        const renumber = () => rows.forEach((r, k) => { r.title.textContent = `${singular} ${k + 1}`; });
        const addBtn = h('button', { type: 'button', class: 'btn secondary small', onclick: () => { const r = addRow(); const first = r.rowEl.querySelector('input,select,textarea'); if (first) first.focus(); } }, `+ Add ${singular.toLowerCase()}`);
        el = h('fieldset', { class: 'field wide' }, h('legend', {}, label, required ? h('span', { class: 'req', 'aria-hidden': 'true' }, '*') : null), hint, list, addBtn, err);
        addRow();
        get = () => {
          const values = []; let error = '';
          for (const r of rows) {
            if (blank(r.group)) continue;
            const res = collect(r.group);
            if (res.error) error = error || res.error;
            if (res.value) values.push(res.value);
          }
          if (!values.length) return { value: undefined, error: error || (required ? fail('Add at least one row') : ok()) };
          ok(); return { value: values, error };
        };
        isEmpty = () => rows.every((r) => blank(r.group));
      }
    } else if (kind === 'json') {
      const ta = h('textarea', { id, class: 'mono', rows: '5', placeholder: kind === 'json' && s.type === 'array' ? '[ ]' : '{ }', 'aria-describedby': described });
      if (def !== undefined) ta.value = JSON.stringify(def, null, 2);
      el = h('div', { class: 'field wide' }, lbl(), ta, h('span', { class: 'hint' }, 'JSON format.'), hint, err);
      wide = true;
      get = () => {
        if (!ta.value.trim()) return { value: undefined, error: required ? fail('Required', ta) : ok(ta) };
        try { const v = JSON.parse(ta.value); ok(ta); return { value: v, error: '' }; } catch (e) { return { value: undefined, error: fail('Not valid JSON', ta) }; }
      };
      isEmpty = () => !ta.value.trim();
    } else {
      let inp;
      if (kind === 'textarea' || kind === 'csv' || kind === 'list') {
        inp = h('textarea', { id, rows: kind === 'csv' ? '8' : '4', class: kind === 'csv' ? 'mono' : '', 'aria-describedby': described, spellcheck: kind === 'csv' ? 'false' : undefined });
        wide = true;
      } else {
        inp = h('input', { id, type: { date: 'date', datetime: 'datetime-local', time: 'time' }[kind] || 'text', 'aria-describedby': described,
          maxlength: s.maxLength, autocomplete: 'off' });
      }
      if (def !== undefined) inp.value = Array.isArray(def) ? def.join('\n') : String(def);
      else if (!prefix && /^(company_name|employer_name|legal_company_name|business_name_on_documents)$/.test(name) && state.me) inp.value = state.me.tenant.company_name || '';
      const extras = [];
      if (kind === 'csv') {
        const file = h('input', { type: 'file', accept: '.csv,text/csv,text/plain', class: 'visually-hidden', id: id + '-file' });
        file.addEventListener('change', async () => { if (file.files[0]) { inp.value = await file.files[0].text(); toast(`Loaded ${file.files[0].name}`); } });
        extras.push(file, h('label', { for: id + '-file', class: 'btn secondary small' }, 'Load CSV file…'));
      }
      el = h('div', { class: 'field' + (wide ? ' wide' : '') }, lbl(), inp, hint, extras.length ? h('div', { class: 'row' }, extras) : null, err);
      get = () => {
        const raw = inp.value;
        if (!raw.trim()) return { value: undefined, error: required ? fail('Required', inp) : ok(inp) };
        if (kind === 'list') {
          const it = norm(s.items || {}, root);
          const vals = raw.split(/\n/).map((x) => x.trim()).filter(Boolean).map((x) => (it.type === 'number' || it.type === 'integer' ? Number(x) : x));
          ok(inp); return { value: vals, error: '' };
        }
        if (s.minLength && raw.trim().length < s.minLength) return { value: undefined, error: fail(`At least ${s.minLength} characters`, inp) };
        if (s.pattern && kind === 'text') { try { if (!new RegExp(s.pattern).test(raw.trim())) return { value: undefined, error: fail('Check the format', inp) }; } catch (e) { /* ignore bad pattern */ } }
        ok(inp); return { value: kind === 'csv' || kind === 'textarea' ? raw : raw.trim(), error: '' };
      };
      isEmpty = () => !inp.value.trim();
    }
    // setter for simple fields (used by "Fill in from a project")
    const primary = el.querySelector(`#${id}`);
    const set = primary && ['INPUT', 'SELECT', 'TEXTAREA'].includes(primary.tagName) ? (v) => {
      if (primary.type === 'checkbox') primary.checked = !!v;
      else if (primary.tagName === 'SELECT') { const o = [...primary.options].find((x) => x.value === String(v)); if (!o) return false; primary.value = o.value; } else primary.value = String(v);
      primary.dispatchEvent(new Event('input', { bubbles: true }));
      return true;
    } : null;
    return { el, get: () => get(), isEmpty, kind, wide, set };
  }

  function buildSchemaForm(spec) {
    const root = spec.input_schema || {};
    const props = root.properties || {};
    const req = new Set(root.required || []);
    const fields = Object.entries(props).map(([n, s]) => ({ name: n, ...buildField(n, s, root, req.has(n)) }));
    const place = (f) => { if (f.wide) f.el.classList.add('wide'); return f.el; };
    // "as of" dates (check against another day than today) are rarely needed: keep them out of the way
    const advanced = fields.filter((f) => ADVANCED.test(f.name) && !req.has(f.name));
    const grid = h('div', {}, h('div', { class: 'form-grid' }, fields.filter((f) => !advanced.includes(f)).map(place)),
      advanced.length ? h('details', { class: 'advanced' }, h('summary', {}, 'Check as of a different date'), h('div', { class: 'form-grid' }, advanced.map(place))) : null);
    return {
      el: grid,
      fields,
      values() {
        const out = {}; const errors = [];
        for (const f of fields) {
          const r = f.get();
          if (r.error) errors.push(`${labelFor(f.name, norm(props[f.name], root), f.kind)}: ${r.error}`);
          else if (r.value !== undefined) out[f.name] = r.value;
        }
        return { values: out, errors };
      },
    };
  }

  // ======================================================================== tool results
  const MD_KEY = /markdown|(^|_)md$|^document$|^body$|_document$|^draft$|_text$|^letter$|^notice$/;
  const SKIP_KEYS = new Set(['verified', 'citations', 'disclaimer', 'handoffs']);     // handoffs: handoffsLine()
  const HIDE_KEY = /^(rule_ids?|citation_ids|unverified_rule_ids|internal_\w+)$/;    // ids for machines; people get "Sources"
  const hiddenKey = (k, v) => HIDE_KEY.test(k) || (k === 'unverified' && Array.isArray(v));   // lists of unverified rule keys
  const HEADLINE_KEYS = new Set(['overall_risk', 'recommended_action', 'risk_level', 'rag']);     // shown as chips at the top
  const NOT_MONEY = /(hours|days|count|pct|percent|periods?|weeks|months|years|(^|_)ft$|stories|employees|workers|ratio|multiplier|number|kwh|sq_ft|square|rate_per|(^|_)rate$|score|(^|_)mod$|minutes|age)/;
  const MONEY = /(usd|amount|price|fee|cost|wage|penalt|premium|total|owed|shortfall|gross|bond|payment|deposit|retention|subsidy|valuation)/;
  const MONEY_PARENT = /(_usd$|fees|line_items|costs|penalt|amounts|wages|premiums|exposure)/;
  const isMoney = (key, ctx) => /(^|_)usd($|_)/.test(key) || ((MONEY.test(key) || ctx) && !NOT_MONEY.test(key));
  let proseParams = {};      // parameter (and result field) labels of the tool whose result is being drawn
  function resultKeyLabels(r) {
    const out = {};
    const walk = (o, depth) => {
      if (!o || typeof o !== 'object' || depth > 3) return;
      if (Array.isArray(o)) { o.slice(0, 3).forEach((x) => walk(x, depth + 1)); return; }
      for (const [k, v] of Object.entries(o)) { if (k.includes('_')) out[k] = humanize(k); walk(v, depth + 1); }
    };
    walk(r, 0);
    return out;
  }
  function prose(text) {
    // Notes name the tool's own parameters ("pass local_minimum_wage_usd") and end with rule ids: say it in the
    // form's words, keep links clickable (inlineMd escapes everything first).
    const t = stripRuleIds(text).replace(/\b([a-z][a-z0-9]*(?:_[a-z0-9]+)+)\b(=\{[^}]*\})?/g, (m, p) => (proseParams[p] ? `“${proseParams[p]}”` : m));
    return h('span', { html: inlineMd(t) });
  }
  function linkText(url) {
    try { return 'Open ' + new URL(url).hostname.replace(/^www\./, '') + ' ↗'; } catch (e) { return url; }
  }
  function statusRag(v) {
    const s = String(v).toLowerCase();
    if (/suspend|expired|revoked|inactive|cancel|not_found|denied|fail|overdue|violation|invalid|unregistered|not_registered|unlicensed|do_not_use|lapsed|late/.test(s)) return 'red';
    if (/pending|warning|due_soon|review|unknown|partial|incomplete|at_risk/.test(s)) return 'amber';
    if (/^(active|ok|clear|compliant|valid|current|registered|passed|on_time|paid|good)$/.test(s)) return 'green';
    return null;
  }
  function markdownFields(r, prefix = '') {
    const out = [];
    for (const [k, v] of Object.entries(r || {})) {
      if (typeof v === 'string' && MD_KEY.test(k) && (v.includes('\n') || v.startsWith('#'))) out.push({ key: prefix + k, label: humanize(k), value: v });
      else if (v && typeof v === 'object' && !Array.isArray(v) && !prefix) out.push(...markdownFields(v, k + '.'));
    }
    return out;
  }
  function fmtValue(key, v, ctx = false) {
    if (v === null || v === undefined || v === '') return '—';
    if (typeof v === 'boolean') return v ? 'Yes' : 'No';
    if (typeof v === 'number') return isMoney(key, ctx) ? money(v) : v.toLocaleString('en-US');
    if (typeof v === 'string') {
      if (/^https?:\/\/\S+$/.test(v)) return h('a', { href: v, target: '_blank', rel: 'noopener noreferrer', title: v }, linkText(v));
      if (/^\d{4}-\d{2}-\d{2}$/.test(v)) return fmtDate(v);
      if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(v)) return fmtDateTime(v);
      if (/^[a-z]+(_[a-z0-9]+)*$/.test(v) && v.length <= 40) return enumLabel(v);        // enum values like "new_single_family"
      if (/^CSLB master file \(snapshot \d{4}-\d{2}-\d{2}\)$/.test(v)) return sourceText(v).replace(/^Source: /, '');
      return prose(v);
    }
    return JSON.stringify(v);
  }
  const isEmptyVal = (v) => v === null || v === undefined || v === '' || (Array.isArray(v) && !v.length);
  const moneyLabel = (key, v, ctx) => { const l = humanize(key); return typeof v === 'number' && isMoney(key, ctx) ? l.replace(/ \(\$\)$/, '') : l; };
  const singular = (key) => humanize(key).toLowerCase().replace(/ \(.*\)$/, '').replace(/ies$/, 'y').replace(/s$/, '');
  function cellValue(key, v, ctx) {
    if (key === 'verified' && typeof v === 'boolean') return h('span', { class: `pill ${v ? 'green' : 'amber'}` }, v ? 'Verified' : 'Not yet verified');
    if (Array.isArray(v)) {
      if (!v.length) return '—';
      if (v.every((x) => x === null || typeof x !== 'object')) return v.map((x) => (typeof x === 'string' ? stripRuleIds(x) : fmtValue(key, x, ctx))).join(', ');
      // e.g. the daily lines of a payroll check: collapsed, so the table stays readable
      return h('details', { class: 'cell-details' }, h('summary', {}, `${v.length} ${v.length === 1 ? singular(key) : humanize(key).toLowerCase().replace(/ \(.*\)$/, '')}`),
        v.map((x) => h('div', { class: 'cell-item' }, x && typeof x === 'object' ? renderObject(x, 2, new Set(), '', MONEY_PARENT.test(key)) : fmtValue(key, x, ctx))));
    }
    if (v && typeof v === 'object') return renderObject(v, 2, new Set(), '', ctx || MONEY_PARENT.test(key));
    return fmtValue(key, v, ctx);
  }
  let resultCtx = null;      // {spec, result, project select} of the result being rendered
  const mdKeysIn = (o) => Object.entries(o || {}).filter(([k, x]) => typeof x === 'string' && MD_KEY.test(k) && (x.includes('\n') || x.startsWith('#'))).map(([k]) => k);
  const TITLE_KEYS = ['name', 'title', 'item', 'label'];
  const itemTitle = (x, key, i) => x.name || x.title || x.item || x.label || `${humanize(key)} ${i + 1}`;
  function saveFieldButton(fieldKey, title) {
    if (!resultCtx || !can.write()) return null;
    const ctx = resultCtx;
    const btn = h('button', { type: 'button', class: 'btn secondary small no-print', onclick: async () => {
      busy(btn, true, 'Saving…');
      try {
        const doc = await api('/api/documents/from_result', { method: 'POST', body: { tool: ctx.spec.name, result: ctx.result, result_sig: ctx.sig, field: fieldKey, title,
          project_id: ctx.project && ctx.project.value ? Number(ctx.project.value) : null } });
        toast(`Saved "${title}" to Documents`);
        btn.replaceWith(h('a', { class: 'btn ghost small', href: `#/document/${doc.id}` }, 'Open saved document'));
      } catch (e) { toast(e.message, true); busy(btn, false); }
    } }, `Save “${title}” as a document`);
    return btn;
  }
  function renderValue(key, v, depth, mdKeys, prefix = '', ctx = false) {
    const mctx = ctx || MONEY_PARENT.test(key);
    if (Array.isArray(v)) {
      if (!v.length) return null;
      if (v.some((x) => x && typeof x === 'object' && !Array.isArray(x) && mdKeysIn(x).length)) {
        // one card per item (e.g. one per safety program): what it is and why up front, the document itself, and
        // the element-by-element detail folded away (it repeats what the document says)
        return h('div', { class: 'result-section' }, h('h4', {}, humanize(key)), v.map((x, i) => {
          const mk = mdKeysIn(x);
          const title = itemTitle(x, key, i);
          const titleKey = TITLE_KEYS.find((k) => x[k] === title);
          const rest = Object.fromEntries(Object.entries(x).filter(([k2]) => !mk.includes(k2) && k2 !== titleKey && k2 !== 'status' && k2 !== 'verified'));
          const simpleRest = Object.fromEntries(Object.entries(rest).filter(([, x2]) => x2 === null || typeof x2 !== 'object'));
          const complexRest = Object.fromEntries(Object.entries(rest).filter(([, x2]) => x2 !== null && typeof x2 === 'object' && !isEmptyVal(x2)));
          const hasComplex = Object.entries(complexRest).some(([k2, x2]) => !hiddenKey(k2, x2));
          return h('details', { class: 'item-card', open: i === 0 ? true : undefined },
            h('summary', {}, h('strong', {}, title), ' ', x.status ? h('span', { class: `pill ${statusRag(x.status) || 'blue'}` }, humanize(x.status)) : null,
              typeof x.verified === 'boolean' ? verifiedBadge({ verified: x.verified }) : null),
            renderObject(simpleRest, depth + 1, new Set(), '', mctx),
            mk.map((m) => h('div', {}, h('div', { class: 'md md-paper', html: md(x[m]) }), saveFieldButton(`${prefix}${key}.${i}.${m}`, title))),
            hasComplex ? h('details', { class: 'more' }, h('summary', {}, 'Item-by-item detail'), renderObject(complexRest, depth + 1, new Set(), '', mctx)) : null);
        }));
      }
      if (v.every((x) => x === null || typeof x !== 'object')) {
        return h('div', { class: 'result-section' }, h('h4', {}, humanize(key)), h('ul', { class: 'bullets' + (key === 'problems' ? ' problems' : '') }, v.map((x) => h('li', {}, fmtValue(key, x, mctx)))));
      }
      if (key === 'citations') return null;
      const cols = [...new Set(v.flatMap((x) => (x && typeof x === 'object' ? Object.keys(x) : [])))]
        .filter((c) => !HIDE_KEY.test(c) && !(c === 'unverified' && v.some((x) => x && Array.isArray(x[c]))) && c !== 'disclaimer' && c !== 'citations' && v.some((x) => x && !isEmptyVal(x[c]))).slice(0, 10);
      return h('div', { class: 'result-section' }, h('h4', {}, humanize(key).replace(/ \(\$\)$/, '')),
        table(cols.map((c) => ({ label: isMoney(c, mctx) ? humanize(c).replace(/ \(\$\)$/, '') : humanize(c), render: (row) => cellValue(c, row ? row[c] : null, mctx) })), v));
    }
    if (v && typeof v === 'object') {
      const inner = renderObject(v, depth + 1, mdKeys, prefix + key + '.', mctx);
      return inner.childNodes.length ? h('div', { class: 'result-section' }, h('h4', {}, humanize(key).replace(mctx ? / \(\$\)$/ : /^$/, '')), inner) : null;
    }
    return null;
  }
  function renderObject(obj, depth = 0, mdKeys = new Set(), prefix = '', ctx = false) {
    const simple = [], complex = [];
    for (const [k, v] of Object.entries(obj)) {
      if ((depth === 0 && (SKIP_KEYS.has(k) || HEADLINE_KEYS.has(k))) || mdKeys.has(prefix + k)) continue;
      if (hiddenKey(k, v) || ((k === 'disclaimer' || k === 'citations') && depth > 0)) continue;
      if (k === 'key' && typeof v === 'string' && /^[a-z0-9_-]+$/.test(v)) continue;
      if (Array.isArray(v) && !v.length) continue;
      if (v !== null && typeof v === 'object') complex.push([k, v]); else simple.push([k, v]);
    }
    const long = (v) => typeof v === 'string' && v.length > 80;
    return h('div', {},
      simple.length ? h('dl', { class: 'kv' }, simple.flatMap(([k, v]) => [h('dt', { class: long(v) ? 'full' : null }, moneyLabel(k, v, ctx)), h('dd', { class: long(v) ? 'full' : null }, fmtValue(k, v, ctx))])) : null,
      complex.map(([k, v]) => renderValue(k, v, depth, mdKeys, prefix, ctx)));
  }
  function riskChips(r) {
    const chips = [];
    if (r.overall_risk) chips.push(ragPill({ low: 'green', medium: 'amber', high: 'red', do_not_use: 'red' }[r.overall_risk], RISK_TEXT[r.overall_risk] || humanize(r.overall_risk)));
    if (r.risk_level) chips.push(ragPill({ normal: 'green', urgent: 'amber', critical: 'red', expired: 'red' }[r.risk_level], r.risk_level === 'normal' ? 'Not urgent' : humanize(r.risk_level)));
    if (r.rag) chips.push(ragPill(r.rag));
    if (r.status && typeof r.status === 'string') { const rag = statusRag(r.status); chips.push(rag ? ragPill(rag, humanize(r.status)) : h('span', { class: 'pill blue' }, humanize(r.status))); }
    if (r.found === false || r.matched === false || r.supported === false) chips.push(h('span', { class: 'pill amber' }, r.found === false ? 'Not found' : r.matched === false ? 'No match' : 'Not supported'));
    return chips;
  }
  async function projectOptions() {
    try { return (await api('/api/projects')).map((p) => [String(p.id), p.name]); } catch (e) { return []; }
  }
  function renderToolResult(spec, out, { projectId = null } = {}) {
    const raw = out.structured;
    const card = h('section', { class: 'card result', 'aria-label': 'Result' });
    if (!raw || typeof raw !== 'object') {
      card.append(h('h2', {}, 'Result'), h('pre', {}, out.text || 'No output.'));
      return card;
    }
    const r = Object.keys(raw).length === 1 && 'result' in raw ? (raw.result && typeof raw.result === 'object' && !Array.isArray(raw.result) ? raw.result : { result: raw.result }) : raw;
    const mds = markdownFields(r);
    const mdKeys = new Set(mds.map((m) => m.key));
    const proj = can.write() ? select('project_id', [['', 'No project']], '', { 'aria-label': 'Link the saved document to a project' }) : null;
    resultCtx = { spec, result: r, sig: out.result_sig, project: proj };
    proseParams = { ...resultKeyLabels(r), ...paramLabels(spec.input_schema || {}) };
    card.append(h('div', { class: 'card-head' }, h('h2', {}, `Result: ${spec.title}`), verifiedBadge(r)));
    const chips = riskChips(r);
    if (chips.length) card.append(h('div', { class: 'row' }, chips));
    if (r.recommended_action) card.append(h('p', { class: 'notice' }, h('strong', {}, 'What to do: '), prose(r.recommended_action)));
    // the API adds "handoffs" next to the result; a tool may also return them inside it
    const handoffs = handoffsLine(Array.isArray(out.handoffs) ? out.handoffs : (raw.handoffs || r.handoffs), { projectId });
    if (handoffs) card.append(handoffs);
    card.append(renderObject(r, 0, mdKeys));
    for (const m of mds) card.append(h('div', { class: 'result-section' }, h('h4', {}, m.label), h('div', { class: 'md md-paper', html: md(m.value) }),
      mds.length > 1 ? saveFieldButton(m.key, `${m.label}`) : null));
    resultCtx = null;
    if (r.citations && r.citations.length) {
      card.append(r.citations.length > 8
        ? h('details', { class: 'result-section' }, h('summary', {}, h('strong', {}, `Sources (${r.citations.length})`)), citationList(r.citations))
        : h('div', { class: 'result-section' }, h('h4', {}, 'Sources'), citationList(r.citations)));
    }
    card.append(disclaimerBox(r.disclaimer || 'Compliance information, not legal advice.'));
    if (can.write()) {
      projectOptions().then((opts) => { opts.forEach(([v, l]) => proj.append(h('option', { value: v }, l))); if (projectId) proj.value = String(projectId); });
      const save = h('button', { type: 'button', class: 'btn', onclick: async () => {
        busy(save, true, 'Saving…');
        try {
          const doc = await api('/api/documents/from_result', { method: 'POST', body: { tool: spec.name, result: r, result_sig: out.result_sig, title: `${spec.title} (${fmtDate(todayISO())})`, project_id: proj.value ? Number(proj.value) : null } });
          toast('Saved to Documents');
          save.replaceWith(h('a', { class: 'btn secondary', href: `#/document/${doc.id}` }, 'Open saved document'));
        } catch (e) { toast(e.message, true); busy(save, false); }
      } }, mds.length > 1 || r.programs ? 'Save everything as one document' : 'Save as document');
      card.append(h('div', { class: 'save-row no-print' },
        h('div', { class: 'field' }, h('label', { for: `save-proj-${fieldSeq + 1}` }, 'Project for the saved document'), Object.assign(proj, { id: `save-proj-${++fieldSeq}` })),
        h('div', { class: 'row' }, save,
          h('button', { type: 'button', class: 'btn ghost', onclick: () => window.print() }, 'Print / PDF'),
          h('button', { type: 'button', class: 'btn ghost', onclick: () => copyText(JSON.stringify(raw, null, 2), 'Result copied as JSON') }, 'Copy as JSON'))));
    } else {
      card.append(h('div', { class: 'row no-print' }, h('button', { type: 'button', class: 'btn ghost', onclick: () => window.print() }, 'Print / PDF')));
    }
    return card;
  }

  // tool parameter -> value from a saved project, so nobody retypes a job's details into every check
  const PROJECT_FIELDS = {
    project_name: (p) => p.name, city: (p) => p.city, jurisdiction: (p) => p.city, county: (p) => p.county,
    address: (p) => p.address, site_address: (p) => p.address, project_address: (p) => p.address,
    owner_name: (p) => p.owner_name, property_owner: (p) => p.owner_name, lender_name: (p) => p.lender_name,
    contract_price_usd: (p) => p.contract_price, contract_value_usd: (p) => p.contract_price, contract_amount_usd: (p) => p.contract_price,
    prime_contract_usd: (p) => p.contract_price, project_value_usd: (p) => p.contract_price,
    start_date: (p) => p.start_date, construction_start_date: (p) => p.start_date,
    first_furnishing_date: (p) => p.first_furnishing_date, last_furnishing_date: (p) => p.last_furnishing_date,
    completion_date: (p) => p.completion_date, notice_of_completion_date: (p) => p.noc_date, noc_recorded_date: (p) => p.noc_date, noc_date: (p) => p.noc_date,
    claimant_role: (p) => (p.extra || {}).claimant_role, public_or_private: (p) => p.public_or_private,
    is_public: (p) => p.public_or_private === 'public', public_project: (p) => p.public_or_private === 'public',
  };
  function toolPanel(spec, { onResult } = {}) {
    const form = buildSchemaForm(spec);
    let filledFrom = null;
    const fillable = form.fields.filter((x) => x.set && PROJECT_FIELDS[x.name]);
    const fillSlot = h('div', { class: 'fill-from' });
    if (fillable.length >= 2) {
      api('/api/projects').then((projects) => {
        if (!projects.length) return;
        const sel = select('fill_project', [['', 'Choose a project…'], ...projects.map((p) => [String(p.id), p.name])], '', { id: `fill-${++fieldSeq}` });
        sel.addEventListener('change', () => {
          const p = projects.find((x) => String(x.id) === sel.value);
          if (!p) return;
          let n = 0;
          for (const fld of fillable) { const v = PROJECT_FIELDS[fld.name](p); if (v !== null && v !== undefined && v !== '' && fld.set(v)) n++; }
          filledFrom = p.id;
          toast(n ? `Filled ${plural(n, 'field')} from “${p.name}”. Check them before running.` : `“${p.name}” has none of these details yet.`);
        });
        fillSlot.append(h('div', { class: 'field' }, h('label', { for: sel.id }, 'Fill in from a project'), sel,
          h('span', { class: 'hint' }, 'Copies the project\'s name, address, dates and contract price into the matching fields below.')));
      }).catch(() => {});
    }
    const result = h('div', { 'aria-live': 'polite' });
    const errBox = h('div', { role: 'alert' });
    const run = h('button', { type: 'submit', class: 'btn' }, 'Run check');
    const f = h('form', { novalidate: true, onsubmit: async (e) => {
      e.preventDefault();
      errBox.replaceChildren();
      const { values, errors } = form.values();
      if (errors.length) { errBox.append(h('div', { class: 'banner error' }, h('p', {}, 'Fix these first: ' + errors.join('; ')))); const bad = f.querySelector('[aria-invalid=true]'); if (bad) bad.focus(); else errBox.scrollIntoView({ block: 'center' }); return; }
      busy(run, true, 'Checking…');
      result.replaceChildren(h('p', { class: 'loading' }, 'Running…'));
      try {
        const out = await api(`/api/tools/${encodeURIComponent(spec.name)}`, { method: 'POST', body: values });
        result.replaceChildren(renderToolResult(spec, out, { projectId: filledFrom }));
        result.firstChild.scrollIntoView({ behavior: scrollBehavior(), block: 'start' });
        if (onResult) onResult(out);
      } catch (err) {
        result.replaceChildren();
        if (err.status === 402) result.append(subscribeCard(err.message));
        else errBox.append(h('div', { class: 'banner error' }, h('p', {}, h('strong', {}, 'The check did not run. '), cleanHint(err.message, { params: paramLabels(spec.input_schema || {}) }))));
      } finally { busy(run, false); }
    } }, form.el, errBox, h('div', { class: 'row' }, run, h('button', { type: 'reset', class: 'btn ghost', onclick: () => setTimeout(() => result.replaceChildren(), 0) }, 'Clear')));
    const ann = spec.annotations || {};
    // Tool descriptions are also read by AI agents, so they run long and list every output field. People get the
    // first sentence; the rest is one tap away.
    const desc = String(spec.description || '').trim();
    const leadRaw = firstSentence(desc);
    const lead = cleanHint(leadRaw, { params: paramLabels(spec.input_schema || {}) });
    const more = desc.length > leadRaw.length + 1 ? cleanHint(desc.slice(leadRaw.length).trim(), { params: paramLabels(spec.input_schema || {}) }) : '';
    return h('div', {},
      h('section', { class: 'card no-print' },
        desc ? h('p', { class: 'tool-lead' }, lead) : null,
        more ? h('details', { class: 'tool-more' }, h('summary', {}, 'What this check covers'), h('p', { class: 'small muted' }, more)) : null,
        ann.readOnlyHint ? null : h('p', { class: 'small muted' }, 'This tool may create or change records.'),
        fillSlot,
        h('p', { class: 'small muted req-note' }, h('span', { class: 'req', 'aria-hidden': 'true' }, '*'), ' Required. Everything else is optional.'),
        f),
      result);
  }

  // ======================================================================== offline outbox (IndexedDB)
  const Outbox = {
    db: null,
    open() {
      if (this.db) return Promise.resolve(this.db);
      return new Promise((resolve, reject) => {
        if (!('indexedDB' in window)) { reject(new Error('This browser cannot store logs offline.')); return; }
        const req = indexedDB.open('tradecomply', 1);
        req.onupgradeneeded = () => req.result.createObjectStore('outbox', { keyPath: 'client_uuid' });
        req.onsuccess = () => { this.db = req.result; resolve(req.result); };
        req.onerror = () => reject(req.error);
      });
    },
    async run(mode, fn) {
      const db = await this.open();
      return new Promise((resolve, reject) => {
        const tx = db.transaction('outbox', mode);
        const req = fn(tx.objectStore('outbox'));
        tx.oncomplete = () => resolve(req ? req.result : undefined);
        tx.onerror = () => reject(tx.error);
      });
    },
    async all() { const tid = state.me && state.me.tenant.id; return (await this.run('readonly', (s) => s.getAll())).filter((x) => x._tenant === tid); },
    put(v) { return this.run('readwrite', (s) => s.put(v)); },
    del(k) { return this.run('readwrite', (s) => s.delete(k)); },
  };
  let syncing = false;
  async function syncOutbox({ quiet = true } = {}) {
    if (syncing || !state.me || !navigator.onLine) return null;
    syncing = true;
    try {
      const items = await Outbox.all();
      if (!items.length) return { created: 0 };
      const logs = items.map((x) => Object.fromEntries(Object.entries(x).filter(([k]) => !k.startsWith('_'))));
      const res = await api('/api/field_logs/sync', { method: 'POST', body: { logs } });
      for (const r of res.results) {
        if (r.status === 'created' || r.status === 'duplicate') await Outbox.del(r.client_uuid);
        else { const it = items.find((x) => x.client_uuid === r.client_uuid); if (it) { it._error = r.error; await Outbox.put(it); } }
      }
      if (!quiet || res.created) toast(`${plural(res.created, 'field log')} synced${res.errors ? `, ${res.errors} need fixing` : ''}`, !!res.errors);
      updateQueueBadge();
      return res;
    } catch (e) {
      if (!quiet) toast(e.message, true);
      return null;
    } finally { syncing = false; }
  }
  async function updateQueueBadge() {
    const el = document.getElementById('queue-count');
    if (!el) return;
    try { const n = (await Outbox.all()).length; el.textContent = n ? `${n} waiting to sync` : 'All field logs synced'; el.className = `pill ${n ? 'amber' : 'green'}`; } catch (e) { el.textContent = ''; }
  }

  // ======================================================================== auth views
  let lastDeletion = null;   // messages from the DELETE /api/tenant receipt, shown once on the login page
  routes.login = async ({ query } = {}) => {
    const deleted = query && query.get('deleted') === '1';
    // Shown in place of #/connect while signed out: remember the app's request for the trip through SSO or sign-up.
    const connecting = parseHash().name === 'connect';
    if (connecting) pendingConnect.save(query.get('request'));
    const ssoError = query && query.get('sso') === 'error' ? query.get('message') || 'Signing in with SSO didn\'t work. Try again.' : null;
    const err = h('div', { role: 'alert' }, ssoError ? h('div', { class: 'banner error' }, h('p', {}, ssoError)) : null);
    const btn = h('button', { type: 'submit', class: 'btn block' }, 'Log in');
    const sso = h('button', { type: 'button', class: 'btn secondary block', onclick: async () => {
      err.replaceChildren();
      const email = form.elements.email;
      if (!/^\S+@\S+$/.test(email.value.trim())) { err.append(h('div', { class: 'banner error' }, h('p', {}, 'Enter your work email, then choose Sign in with SSO.'))); email.focus(); return; }
      busy(sso, true, 'Opening your company sign-in…');
      try {
        const r = await api('/api/auth/sso/start', { method: 'POST', body: { email: email.value.trim() } });
        if (!webUrl(r.authorize_url)) throw new Error('The single sign-on address is not valid.');
        location.assign(r.authorize_url);          // the company's identity provider; it comes back signed in
      } catch (ex) { err.append(h('div', { class: 'banner error' }, h('p', {}, ex.message))); busy(sso, false); }
    } }, 'Sign in with SSO');
    const form = h('form', { onsubmit: async (e) => {
      e.preventDefault(); err.replaceChildren(); busy(btn, true, 'Logging in…');
      try {
        const r = await api('/api/auth/login', { method: 'POST', body: formData(form) });
        state.config = null; await loadMe();
        if (!resumeConnect()) go(r.must_change_password ? 'settings' : 'dashboard');
      } catch (ex) { err.append(h('div', { class: 'banner error' }, h('p', {}, ex.message))); busy(btn, false); }
    } },
    field('Work email', input('email', { type: 'email', autocomplete: 'username', inputmode: 'email' }), { required: true }),
    field('Password', input('password', { type: 'password', autocomplete: 'current-password' }), { required: true }),
    err, btn, h('p', { class: 'auth-or small muted' }, 'or'), sso);
    return h('div', { class: 'auth-wrap' },
      h('div', { class: 'auth-hero' }, h('h1', {}, 'Log in to TradeComply'), h('p', {}, connecting ? 'Log in to finish connecting the app.' : 'California construction compliance for your company')),
      deleted ? h('div', { class: 'banner info', role: 'status' }, h('p', {}, 'Your company account was deleted and you are signed out.'), ...(lastDeletion || []).map((m) => h('p', { class: 'small' }, m))) : null,
      navigator.onLine ? null : h('div', { class: 'banner warn' }, h('p', {}, 'You are offline. Log in once with signal; after that the Field screen works offline.')),
      h('div', { class: 'card' }, form, h('p', { class: 'small muted' }, 'New here? ', h('a', { href: '#/signup' }, 'Start a 14-day free trial'))),
      connecting ? null : mcpDemoNote());
  };

  function mcpDemoNote() {
    const url = state.config ? state.config.mcp_url : location.origin + '/mcp';
    return h('div', { class: 'card small' }, h('h2', {}, 'Using an AI assistant?'),
      h('p', { class: 'muted' }, state.config && state.config.mcp_require_key ? 'Connect it to this MCP server with an API key from Settings:' : 'Point it at this MCP server to try the checks:'),
      copyBox(url, 'MCP URL'));
  }

  routes.signup = async () => {
    const err = h('div', { role: 'alert' });
    const btn = h('button', { type: 'submit', class: 'btn block' }, 'Start free trial');
    const form = h('form', { onsubmit: async (e) => {
      e.preventDefault(); err.replaceChildren();
      const data = formData(form);
      if ((data.password || '').length < 10) { err.append(h('div', { class: 'banner error' }, h('p', {}, 'Use a password of at least 10 characters.'))); return; }
      busy(btn, true, 'Creating your account…');
      try { await api('/api/auth/signup', { method: 'POST', body: data }); state.config = null; await loadMe(); if (!resumeConnect()) go('welcome'); } catch (ex) { err.append(h('div', { class: 'banner error' }, h('p', {}, ex.message))); busy(btn, false); }
    } },
    field('Company name', input('company_name', { autocomplete: 'organization' }), { required: true }),
    field('Your name', input('name', { autocomplete: 'name' })),
    field('Work email', input('email', { type: 'email', autocomplete: 'email', inputmode: 'email' }), { required: true }),
    field('Password', input('password', { type: 'password', autocomplete: 'new-password', minlength: '10' }), { required: true, hint: 'At least 10 characters.' }),
    field('CSLB license number', input('cslb_license', { inputmode: 'numeric', autocomplete: 'off' }), { hint: 'Optional. We check your own license first.' }),
    err, btn);
    return h('div', { class: 'auth-wrap' },
      h('div', { class: 'auth-hero' }, h('h1', {}, 'Start your free trial'), h('p', {}, '14 days free, then $1,000/month. No card needed to start.')),
      h('div', { class: 'card' }, form, h('p', { class: 'small muted' }, 'Already have an account? ', h('a', { href: '#/login' }, 'Log in'))),
      h('div', { class: 'card small' }, h('h2', {}, 'In the first 15 minutes'), h('ul', { class: 'value-list' },
        h('li', {}, 'License health check on your own CSLB license'), h('li', {}, 'Risk report on your sub list'),
        h('li', {}, 'Lien deadline calendar for a project'), h('li', {}, 'Draft Cal/OSHA safety programs'), h('li', {}, 'Connect your AI assistant'))));
  };

  // ======================================================================== connect an app (OAuth consent) + connected apps
  // An MCP client such as Claude sends the person to /oauth/authorize, which checks the app and sends the browser to
  // #/connect?request=<id> (app/oauth.py). Signed out, the login shows in its place; the request id is kept in this
  // browser for 10 minutes (the server's limit) so it survives SSO and sign-up, then the consent comes back. An app
  // registers its own name (anyone can call one "Claude"), so the card leads with the address it returns to, which it
  // can't fake. Approve navigates to the app; Deny only offers a link back (OAuth 2.1 section 7.12.2). Text only.
  const REQUEST_RE = /^[A-Za-z0-9_-]{20,100}$/;
  const CONNECT_KEY = 'tc.connect';
  const pendingConnect = {
    save(id) { if (REQUEST_RE.test(id || '')) local.set(CONNECT_KEY, `${id} ${Date.now()}`); },
    take() {
      const [id, at] = String(local.get(CONNECT_KEY) || '').split(' ');
      local.del(CONNECT_KEY);
      return REQUEST_RE.test(id || '') && Date.now() - Number(at) < 10 * 60 * 1000 ? id : null;
    },
  };
  // After signing in (password, SSO or a new account): back to the app's request, if one is waiting.
  function resumeConnect() {
    const id = pendingConnect.take();
    if (!id) return false;
    if (parseHash().name === 'connect') render();        // a request in the address wins over an older one
    else location.replace(`#/connect?request=${id}`);
    return true;
  }
  const webUrl = (u) => (typeof u === 'string' && /^https?:\/\/[^\s"'<>\\]+$/i.test(u) ? u : null);
  const bannerText = (kind, text) => h('div', { class: `banner ${kind}` }, h('p', {}, text));

  routes.connect = async ({ query }) => {
    const id = query.get('request') || '';
    const wrap = h('div', { class: 'auth-wrap' });
    const show = (card) => {          // a new step: its heading takes the focus, so screen readers read it
      wrap.replaceChildren(card);
      const t = card.querySelector('h1');
      if (t && wrap.isConnected) t.focus();
      return wrap;
    };
    const gone = (message, status) => show(h('div', { class: 'card' },
      h('h1', { tabindex: '-1' }, status === 403 ? 'Start again in the app' : 'This request has expired'),
      h('p', {}, message || 'This sign-in request has expired or was already used. Go back to the app and connect again.'),
      h('a', { class: 'btn secondary', href: '#/dashboard' }, 'Go to your dashboard')));
    if (!REQUEST_RE.test(id)) return gone();
    // A temporary password first: changing it later would disconnect the app again.
    if (state.me.user.must_change_password && state.connectPwLater !== id) return show(passwordFirst(id));
    let req;
    try { req = await api(`/api/oauth/requests/${id}`); } catch (e) {
      // 403: started in another browser (the request is tied to the one that opened it)
      if (e.status === 404 || e.status === 403 || e.status === 422) return gone(e.status === 422 ? null : e.message, e.status);
      throw e;
    }
    const ent = state.me.entitlement || {};
    const err = h('div', { role: 'alert' });
    const approve = h('button', { type: 'button', class: 'btn', onclick: () => decide('approve', approve) }, 'Approve');
    const deny = h('button', { type: 'button', class: 'btn secondary', onclick: () => decide('deny', deny) }, 'Deny');
    async function decide(what, btn) {
      err.replaceChildren();
      approve.disabled = deny.disabled = true;
      busy(btn, true, what === 'approve' ? 'Connecting…' : 'Denying…');
      let r;
      try { r = await api(`/api/oauth/requests/${id}/${what}`, { method: 'POST' }); } catch (e) {
        if (e.status === 404 || e.status === 403) { gone(e.message, e.status); return; }
        err.append(bannerText(isOffline(e) ? 'warn' : 'error', e.message));
        busy(btn, false); approve.disabled = deny.disabled = false;
        return;
      }
      const to = webUrl(r.redirect_to);
      const back = to ? h('a', { class: 'btn', href: to }, `Back to ${req.redirect_host}`) : null;
      if (what === 'approve') {
        show(h('div', { class: 'card', role: 'status' }, h('h1', { tabindex: '-1' }, 'Connected'),
          h('p', {}, `Taking you back to ${req.redirect_host}…`), back));
        if (to) location.assign(to);
      } else {
        show(h('div', { class: 'card' }, h('h1', { tabindex: '-1' }, 'Not connected'),
          h('p', {}, `“${req.client_name}” can't use TradeComply. Nothing was shared with it.`),
          h('div', { class: 'row' }, back, h('a', { class: 'btn secondary', href: '#/dashboard' }, 'Go to your dashboard'))));
      }
    }
    const company = req.company_name || 'your company';
    const other = h('button', { type: 'button', class: 'btn ghost', onclick: async () => {
      await api('/api/auth/logout', { method: 'POST' }).catch(() => {});
      await signedOut();
      render();                        // the login, for this same request
    } }, 'Use another account');
    return show(h('div', { class: 'card connect' },
      h('h1', { tabindex: '-1' }, 'Connect an app to TradeComply?'),
      h('div', { class: 'connect-app' },
        h('p', { class: 'small' }, 'The app returns to'),
        h('p', { class: 'connect-host' }, req.redirect_host),
        h('p', { class: 'small muted' }, 'Check this address. An app can give itself any name, but not this.')),
      req.local_only ? bannerText('warn', 'This app runs on a computer, not a website (it returns to localhost). Approve it only if you started connecting it on this computer just now.') : null,
      h('p', {}, 'It calls itself ', h('strong', { class: 'connect-name' }, `“${req.client_name}”`), ' and wants to use TradeComply as ',
        h('strong', {}, req.user_email), ' at ', h('strong', {}, company), /[.!?]$/.test(company) ? '' : '.'),
      h('h2', {}, 'If you approve, it can'),
      h('ul', { class: 'bullets' }, req.scopes.map((s) => h('li', {}, s.label)),
        h('li', {}, `Act as you, with your role (${humanize(req.role)}). Every check it runs is logged.`)),
      h('p', { class: 'small muted' }, 'It can\'t change your records or settings and never sees your password. You can disconnect it any time in Settings → Connected apps.'),
      ent.ok === false ? bannerText('warn', 'Your company\'s subscription isn\'t active, so the app\'s checks won\'t run until it is.') : null,
      state.me.user.must_change_password ? bannerText('warn', 'You still have a temporary password. Changing it later disconnects this app, and you would approve it again.') : null,
      h('p', { class: 'small' }, 'Approve only if you started connecting this app yourself just now.'),
      err,
      h('div', { class: 'connect-actions' }, approve, deny),
      h('p', { class: 'small muted connect-other' }, `Not ${req.user_email}? `, other)));
  };

  function passwordFirst(id) {
    const err = h('div', { role: 'alert' });
    const btn = h('button', { type: 'submit', class: 'btn' }, 'Set password and continue');
    const form = h('form', { onsubmit: async (e) => {
      e.preventDefault(); err.replaceChildren();
      const d = formData(form);
      if ((d.new_password || '').length < 10) { err.append(bannerText('error', 'Use at least 10 characters.')); return; }
      busy(btn, true, 'Saving…');
      try {
        await api('/api/auth/password', { method: 'POST', body: d });
        toast('Password set. Other devices were logged out.');
        await loadMe();
        render();
      } catch (ex) { err.append(bannerText('error', ex.message)); busy(btn, false); }
    } },
    field('Temporary password', input('current_password', { type: 'password', autocomplete: 'current-password' }), { required: true }),
    field('New password', input('new_password', { type: 'password', autocomplete: 'new-password', minlength: '10' }), { required: true, hint: 'At least 10 characters.' }),
    err, btn);
    return h('div', { class: 'card' }, h('h1', { tabindex: '-1' }, 'Set your own password first'),
      h('p', {}, 'You logged in with a temporary password. Changing your password disconnects the apps you connected, so set your own now. Then you can approve the app.'),
      form,
      h('p', { class: 'small muted connect-other' }, 'Signed in with SSO, or doing this later? ',
        h('button', { type: 'button', class: 'btn ghost', onclick: () => { state.connectPwLater = id; render(); } }, 'Not now')));
  }

  // Settings: the apps that act as this person (an owner sees and can disconnect everyone's in the company).
  function connectedAppsSection() {
    const owner = can.owner();
    const slot = h('div', { 'aria-live': 'polite' });
    const heading = h('h2', { id: 'apps-h', tabindex: '-1' }, 'Connected apps');
    const draw = async () => {
      const rows = await api('/api/oauth/connections');
      const disconnect = async (c, btn) => {
        busy(btn, true, 'Disconnecting…');
        try {
          await api(`/api/oauth/connections/${c.id}`, { method: 'DELETE' });
          toast(`Disconnected “${c.client_name}”. It can't use TradeComply any more.`);
          await draw();
          heading.focus();
        } catch (e) { toast(e.message, true); busy(btn, false); }
      };
      fill(slot, rows.length ? table([
        { label: 'App', bare: true, render: (c) => h('div', {}, h('strong', { class: 'connect-name' }, c.client_name), h('div', { class: 'small muted' }, `Returns to ${c.redirect_host}`)) },
        owner ? { label: 'Person', render: (c) => (c.mine ? `${c.user_email} (you)` : c.user_email) } : null,
        { label: 'Connected', render: (c) => fmtDateTime(c.created) },
        { label: 'Last used', render: (c) => (c.last_used ? fmtDateTime(c.last_used) : 'Not yet') },
        { label: 'Actions', bare: true, render: (c) => { const b = h('button', { type: 'button', class: 'btn secondary', 'aria-label': `Disconnect ${c.client_name}${c.mine ? '' : ` for ${c.user_email}`}`, onclick: () => disconnect(c, b) }, 'Disconnect'); return b; } },
      ].filter(Boolean), rows) : h('p', { class: 'muted' }, 'No apps are connected.'));
    };
    draw().catch((e) => fill(slot, bannerText(isOffline(e) ? 'warn' : 'error', e.message)));
    return h('section', { class: 'card', id: 'connected-apps', 'aria-labelledby': 'apps-h' }, heading,
      h('p', { class: 'small muted' }, owner ? 'Apps that people in your company approved, such as Claude. Each acts as the person who approved it. Disconnect one to stop it at once.'
        : 'Apps you approved to use TradeComply as you, such as Claude. Disconnect one to stop it at once.', ' ', h('a', { href: '#/integrations' }, 'How to add TradeComply to Claude'), '.'),
      slot);
  }

  // How to add this server to Claude as a custom connector (Claude's help pages "Get started with custom connectors
  // using remote MCP" and "Add an unlisted connector", read 2026-11-26; the same steps as mcp_client_config's
  // claude_connector). Claude connects from Anthropic's servers, so it needs the server's public https address.
  function claudeConnector(url) {
    let host = '';
    try { host = new URL(url).hostname; } catch (e) { /* not a URL */ }
    const reachable = /^https:\/\//i.test(url) && !['localhost', '127.0.0.1', '[::1]'].includes(host);
    const open = state.config && state.config.mcp_require_key === false;
    return [
      reachable ? null : bannerText('warn', `Claude can't reach ${url}: it connects from Anthropic's servers, so it needs this server's public https:// address. Claude Code and API keys still work.`),
      open ? bannerText('info', 'This server lets AI assistants in without signing in right now, so Claude won\'t ask you to sign in or approve it, and its checks won\'t be linked to your company.') : null,
      h('ol', { class: 'steps' },
        h('li', {}, 'In Claude, open ', h('strong', {}, 'Customize → Connectors'), ' and choose ', h('strong', {}, '+ Add → Add custom connector'), '.'),
        h('li', {}, 'Name it TradeComply, paste this address as the URL and choose ', h('strong', {}, 'Add'), '. Keep the settings Claude detects.', copyBox(url, 'TradeComply connector address')),
        h('li', {}, 'Choose ', h('strong', {}, 'Connect'), '. Log in to TradeComply if asked, check that the app returns to claude.ai, and choose ', h('strong', {}, 'Approve'), '.')),
      more('Claude Team or Enterprise', h('p', { class: 'small' }, 'An Owner adds it once under Organization settings → Connectors → Add → Custom → Web, with the same address. Then each person chooses Connect next to TradeComply and approves it here.')),
      more('Claude Code (terminal)', copyBox(`claude mcp add --transport http tradecomply ${url}`, 'Claude Code command', true),
        h('p', { class: 'small muted' }, 'Then run /mcp in Claude Code and choose TradeComply to log in.')),
      h('p', { class: 'small muted' }, 'No API key needed. The connection acts as the person who approved it, with their role, and can only run read-only checks. Disconnect it any time in Settings → Connected apps.'),
    ];
  }

  // ======================================================================== license + alerts components
  // CSLB classification names (cslb.ca.gov), so "C10" reads as "C-10 Electrical"
  const CSLB_CLASSES = { A: 'General Engineering', B: 'General Building', B2: 'Residential Remodeling', C2: 'Insulation and Acoustical',
    C4: 'Boiler, Hot Water Heating and Steam Fitting', C5: 'Framing and Rough Carpentry', C6: 'Cabinet, Millwork and Finish Carpentry',
    C7: 'Low Voltage Systems', C8: 'Concrete', C9: 'Drywall', C10: 'Electrical', C11: 'Elevator', C12: 'Earthwork and Paving', C13: 'Fencing',
    C15: 'Flooring and Floor Covering', C16: 'Fire Protection', C17: 'Glazing', C20: 'Warm-Air Heating, Ventilating and Air-Conditioning',
    C21: 'Building Moving/Demolition', C22: 'Asbestos Abatement', C23: 'Ornamental Metal', C27: 'Landscaping', C28: 'Lock and Security Equipment',
    C29: 'Masonry', C31: 'Construction Zone Traffic Control', C32: 'Parking and Highway Improvement', C33: 'Painting and Decorating',
    C34: 'Pipeline', C35: 'Lathing and Plastering', C36: 'Plumbing', C38: 'Refrigeration', C39: 'Roofing', C42: 'Sanitation System',
    C43: 'Sheet Metal', C45: 'Sign', C46: 'Solar', C47: 'General Manufactured Housing', C49: 'Tree and Palm', C50: 'Reinforcing Steel',
    C51: 'Structural Steel', C53: 'Swimming Pool', C54: 'Ceramic and Mosaic Tile', C55: 'Water Conditioning', C57: 'Well Drilling',
    C60: 'Welding', C61: 'Limited Specialty' };
  function className(code) {
    const raw = String(code || '').trim();
    const m = raw.match(/^([A-D])-?(\d*)\b\s*(.*)$/i);
    if (!m) return raw;
    const key = (m[1] + m[2]).toUpperCase();
    const label = m[2] ? `${m[1].toUpperCase()}-${m[2].replace(/^0+(?=\d)/, '')}` : m[1].toUpperCase();
    const name = m[3] || CSLB_CLASSES[key] || (m[1].toUpperCase() === 'D' ? 'Limited Specialty' : '');
    return name ? `${label} ${name}` : label;
  }
  const LICENSE_RAG = { red: 'Problem', amber: 'Check this', green: 'Good standing' };
  function licenseCardView(card) {
    const lic = card.license;
    const el = h('section', { class: 'card rag-bar ' + card.rag, 'aria-labelledby': 'lic-h' },
      h('div', { class: 'card-head' }, h('h2', { id: 'lic-h' }, 'Your license health'), ragPill(card.rag, LICENSE_RAG[card.rag])));
    if (lic && !lic.found) {
      el.append(h('dl', { class: 'kv' }, h('dt', {}, 'License #'), h('dd', {}, lic.license_number || card.license_number || '—'), h('dt', {}, 'Status'), h('dd', {}, 'Not found')),
        h('ul', { class: 'bullets problems' }, card.problems.map((p) => h('li', {}, p))),
        h('p', { class: 'small muted' }, 'Check the number on your pocket card. ', sourceText(lic.data_source)),
        lic.official_lookup_url ? h('p', { class: 'small' }, h('a', { href: lic.official_lookup_url, target: '_blank', rel: 'noopener noreferrer' }, 'Look it up on the CSLB website')) : null);
      return el;
    }
    if (!lic) {
      el.append(h('ul', { class: 'bullets' }, card.problems.map((p) => h('li', {}, p))), can.write() ? h('a', { class: 'btn secondary small', href: '#/settings' }, 'Add license number') : null);
      return el;
    }
    el.append(h('dl', { class: 'kv' },
      h('dt', {}, 'Business'), h('dd', {}, lic.business_name || '—'),
      h('dt', {}, 'License #'), h('dd', {}, lic.license_number),
      h('dt', {}, 'Status'), h('dd', {}, lic.found ? humanize(lic.status) : 'Not found'),
      h('dt', {}, 'Classes'), h('dd', {}, (lic.classifications || []).map(className).join('; ') || '—'),
      h('dt', {}, 'Expires'), h('dd', {}, lic.expiration_date ? `${fmtDate(lic.expiration_date)} (${daysText(lic.expires_in_days)})` : '—'),
      h('dt', {}, "Workers' comp"), h('dd', {}, humanize(lic.workers_comp)),
      h('dt', {}, 'Contractor bond'), h('dd', {}, lic.contractor_bond_on_file ? 'On file' : 'Not on file')));
    if (card.problems.length) el.append(h('ul', { class: 'bullets problems' }, card.problems.map((p) => h('li', {}, p))));
    el.append(h('div', { class: 'row' }, verifiedBadge(lic), h('span', { class: 'small muted' }, sourceText(lic.data_source))),
      h('p', { class: 'small' }, h('a', { href: lic.official_lookup_url, target: '_blank', rel: 'noopener noreferrer' }, 'Confirm on the CSLB website')));
    return el;
  }
  function sourceText(src) {
    const m = String(src || '').match(/snapshot (\d{4}-\d{2}-\d{2})/);
    if (m) return `Source: CSLB public license data as of ${fmtDate(m[1])}`;
    if (/demo/.test(src || '')) return 'Source: demo data (no CSLB data loaded on this server)';
    if (/partial/.test(src || '')) return 'Source: CSLB public license data (this server has a partial copy).';
    return src ? `Source: ${src}` : '';
  }
  function alertList(items, { empty = 'Nothing due. Nice work.', inProject = false } = {}) {
    if (!items.length) return h('p', { class: 'empty' }, empty);
    const thisYear = todayISO().slice(0, 4);
    return h('ul', { class: 'alerts' }, items.map((i) => {
      const cites = (i.citations || []).map((c) => c.citation).filter(Boolean);
      return h('li', { class: 'alert-item' },
        h('div', { class: 'alert-date' }, String(i.date).startsWith(thisYear) ? fmtDate(i.date).replace(/, \d{4}$/, '') : fmtDate(i.date), h('span', { class: 'days' }, daysText(i.days_left))),
        h('div', { class: 'alert-body' },
          h('div', { class: 'row' }, ragPill(i.rag), i.project_id && !inProject ? h('a', { class: 'small', href: `#/project/${i.project_id}` }, i.project_name) : null,
            i.sub_name ? h('a', { class: 'small', href: '#/subs' }, i.sub_name) : null),
          h('h3', {}, i.title),
          i.detail ? h('p', {}, i.detail) : null,
          // Only rule-based dates carry a verified flag; a sub's COI date comes from your own records.
          cites.length ? h('div', { class: 'row small' }, verifiedBadge(i), h('span', { class: 'muted' }, [...new Set(cites)].join('; '))) : null));
    }));
  }
  const RISK_ORDER = { do_not_use: 0, high: 1, medium: 2, low: 3 };
  const RAG_SORT = { red: 0, amber: 1, green: 2 };
  const worstFirst = (list, risk = (s) => s.overall_risk, rag = (s) => s.rag) => [...list].sort((a, b) =>
    ((RISK_ORDER[risk(a)] ?? RAG_SORT[rag(a)] ?? 4) - (RISK_ORDER[risk(b)] ?? RAG_SORT[rag(b)] ?? 4)) || String(a.name).localeCompare(String(b.name)));
  function subRiskTable(rows) {
    return table([
      { label: 'Subcontractor', bare: true, render: (s) => h('div', {}, h('strong', {}, s.name), s.license && s.license.business_name && s.license.business_name.toLowerCase() !== String(s.name).toLowerCase() ? h('div', { class: 'small muted' }, `CSLB: ${s.license.business_name}`) : null) },
      { label: 'License', render: (s) => s.license_number ? `${s.license_number}${s.license ? ' · ' + humanize(s.license.status) : ''}` : 'None given' },
      { label: 'Risk', render: (s) => ragPill(s.rag, RISK_TEXT[s.overall_risk] || RAG_TEXT[s.rag]) },
      { label: 'Problems', render: (s) => s.problems && s.problems.length ? h('ul', { class: 'bullets small' }, s.problems.map((p) => h('li', {}, p))) : 'None' },
      { label: 'What to do', render: (s) => s.recommended_action || '—' },
    ], worstFirst(rows));
  }

  // ======================================================================== dashboard
  // "Getting started": up to four steps worked out from what the company has actually done (not from clicks on a
  // tour). Done steps don't show. Finishing every step puts it away for the whole company; "Hide" puts it away for
  // this person. Both are remembered in this browser at once and on the server (tenant settings), so the checklist
  // doesn't come back on their next phone or laptop. Adding the license number isn't a step here: the license card
  // right below already asks for it.
  const gsKey = () => `tc.gettingStarted.${state.me.tenant.id}`;
  const tipsKey = () => (state.me.user && state.me.user.id ? `hidden_tips_u${state.me.user.id}` : null);   // one key per person
  const tenantSetting = (k) => (k ? (state.me.tenant.settings || {})[k] : undefined);
  const hiddenTips = () => { const v = tenantSetting(tipsKey()); return Array.isArray(v) ? v.filter((x) => typeof x === 'string') : []; };
  const tipHidden = (tip) => hiddenTips().includes(tip);
  const gsAway = () => !can.write() || state.gsAway === state.me.tenant.id || !!local.get(gsKey())
    || tenantSetting('getting_started') === 'done' || tipHidden('getting_started');
  function saveTenantSetting(key, value) {
    if (!key) return;
    const tid = state.me.tenant.id;
    api('/api/tenant', { method: 'PATCH', body: { settings: { [key]: value } } })
      .then((t) => { if (state.me && state.me.tenant.id === tid) state.me.tenant = { ...state.me.tenant, ...t }; })
      .catch(() => { /* offline or not allowed: this browser still remembers */ });
  }
  async function gettingStartedFacts() {
    if (gsAway()) return null;
    const soft = (path) => api(path).catch(() => null);       // a signal we can't read counts as "not done yet"
    const [audit, links, hooks, ints] = await Promise.all([soft('/api/audit?limit=200'), soft('/api/integrations/calendar_feed/links'),
      soft('/api/integrations/webhooks'), soft('/api/integrations')]);
    return {
      ranCheck: Array.isArray(audit) && audit.some((r) => r && r.ok !== false && /^[a-z][a-z0-9_]*$/.test(String(r.tool || ''))),   // tool names; "api_key.create" etc. are not checks
      reminders: !!((links && (links.active === true || (links.links || []).length)) || (Array.isArray(hooks) && hooks.some((w) => w && w.enabled))
        || (ints && (ints.providers || []).some((p) => p.provider === 'twilio_sms' && (p.connection || {}).status === 'connected'))),
    };
  }
  function gettingStartedCard(d, facts, setupDone) {
    if (!facts || gsAway()) return null;
    const steps = [
      { done: d.subs.length > 0, text: 'Add your subcontractors', href: '#/subs' },
      { done: d.projects.length > 0, text: 'Add a project', href: '#/project/new' },
      { done: facts.ranCheck, text: 'Run a check', href: '#/tools' },
      { done: facts.reminders, text: 'Set up deadline reminders', href: '#/integrations?integration=calendar-feed' },
    ];
    const left = steps.filter((s) => !s.done);
    if (!left.length) { local.set(gsKey(), 'done'); if (tenantSetting('getting_started') !== 'done') saveTenantSetting('getting_started', 'done'); return null; }
    const card = h('section', { class: 'card gs no-print', 'aria-labelledby': 'gs-h' },
      h('div', { class: 'gs-head' },
        h('div', { class: 'gs-title' }, h('h2', { id: 'gs-h' }, 'Getting started'), h('span', { class: 'small muted' }, `${steps.length - left.length} of ${steps.length} done`)),
        h('button', { type: 'button', class: 'btn ghost small', 'aria-label': 'Hide getting started', onclick: () => {
          state.gsAway = state.me.tenant.id;          // stays hidden this session even if the browser blocks storage
          local.set(gsKey(), 'hidden');
          if (!tipHidden('getting_started')) saveTenantSetting(tipsKey(), [...hiddenTips(), 'getting_started']);
          card.remove();
          $('#main').focus({ preventScroll: true });
        } }, 'Hide')),
      h('ul', { class: 'gs-steps' }, left.map((s) => h('li', {}, h('a', { href: s.href }, s.text)))),
      setupDone ? null : h('p', { class: 'small muted gs-more' }, 'Or go step by step: ', h('a', { href: '#/welcome' }, 'guided setup'), '.'));
    return card;
  }
  routes.dashboard = async () => {
    if (!entitled()) return subscribeCard(state.me.entitlement.message);
    const [d, facts] = await Promise.all([api('/api/dashboard'), gettingStartedFacts()]);
    const filter = { rag: null };
    const listSlot = h('div', {});
    const subSlot = h('div', {});
    const draw = () => {
      const items = filter.rag ? d.items.filter((i) => i.rag === filter.rag) : d.items;
      const subs = filter.rag ? d.subs.filter((s) => s.rag === filter.rag) : d.subs;
      listSlot.replaceChildren(alertList(items, { empty: filter.rag ? 'Nothing in this group.' : 'No deadlines or alerts. Add projects and subs to fill this in.' }));
      subSlot.replaceChildren(d.subs.length ? (subs.length ? subRiskTable(subs) : h('p', { class: 'empty' }, 'No subs in this group.')) :
        h('div', { class: 'empty' }, h('p', {}, 'No subcontractors yet.'), can.write() ? h('a', { class: 'btn secondary', href: '#/subs' }, 'Import your sub list') : null));
      tiles.forEach((t) => t.setAttribute('aria-pressed', String(t.dataset.rag === filter.rag)));
    };
    const tiles = ['red', 'amber', 'green'].map((rag) => h('button', { type: 'button', class: `tile ${rag}`, 'data-rag': rag, 'aria-pressed': 'false',
      onclick: () => { filter.rag = filter.rag === rag ? null : rag; draw(); } },
    h('span', { class: 'n' }, d.overall[rag]), h('span', { class: 't' }, RAG_TEXT[rag])));
    const setupDone = (((state.me.tenant.settings || {}).onboarding || {}).done || []).length >= 3;
    const wrap = h('div', {},
      h('div', { class: 'page-head' }, h('div', {}, h('h1', {}, 'Compliance today'), h('p', {}, `As of ${fmtDate(d.today)} (California time). Select a box to show only those items.`)),
        h('div', { class: 'row no-print' }, h('a', { class: 'btn secondary small', href: '/api/integrations/calendar_feed/calendar.ics', download: 'tradecomply.ics' }, 'Download calendar'),
          h('button', { type: 'button', class: 'btn ghost small', onclick: () => window.print() }, 'Print'))),
      gettingStartedCard(d, facts, setupDone),
      h('div', { class: 'tiles', role: 'group', 'aria-label': 'Filter by status' }, tiles),
      h('div', { class: 'grid grid-2' },
        h('section', { class: 'card', 'aria-labelledby': 'dl-h' }, h('div', { class: 'card-head' }, h('h2', { id: 'dl-h' }, 'Deadlines and alerts'), h('span', { class: 'small muted' }, `${d.counts.red} act now · ${d.counts.amber} coming up`)), listSlot),
        h('div', {}, licenseCardView(d.company),
          h('section', { class: 'card' }, h('h2', {}, 'Projects'), d.projects.length ? h('ul', { class: 'clean' }, d.projects.map((p) => h('li', { class: 'row' }, h('a', { href: `#/project/${p.id}` }, p.name), h('span', { class: 'pill gray' }, humanize(p.status)), p.public_or_private === 'public' ? h('span', { class: 'pill blue' }, 'Public') : null))) :
            h('div', { class: 'empty' }, h('p', {}, 'No projects yet.'), can.write() ? h('a', { class: 'btn secondary', href: '#/project/new' }, 'Add a project') : null)))),
      h('section', { class: 'card', 'aria-labelledby': 'subs-h' }, h('div', { class: 'card-head' }, h('h2', { id: 'subs-h' }, 'Subcontractors'), h('span', { class: 'small muted' }, `${d.sub_counts.red} high risk · ${d.sub_counts.amber} need documents · ${d.sub_counts.green} clear`)), subSlot));
    draw();
    return wrap;
  };

  // ======================================================================== projects
  function projectForm(p, onSaved) {
    const roles = Object.entries((state.config && state.config.claimant_roles) || { direct_contractor: 'Prime / general contractor' });
    const x = (p && p.extra) || {};
    const err = h('div', { role: 'alert' });
    const btn = h('button', { type: 'submit', class: 'btn' }, p ? 'Save changes' : 'Add project');
    const dateIn = (name) => input(name, { type: 'date', value: (p && p[name]) || '' });
    const form = h('form', { novalidate: true, onsubmit: async (e) => {
      e.preventDefault(); err.replaceChildren();
      const data = formData(form);
      if (!data.name) { err.append(h('div', { class: 'banner error' }, h('p', {}, 'Give the project a name.'))); return; }
      busy(btn, true, 'Saving…');
      try {
        const saved = await api(p ? `/api/projects/${p.id}` : '/api/projects', { method: p ? 'PATCH' : 'POST', body: data });
        toast(p ? 'Project saved' : 'Project added'); onSaved(saved);
      } catch (ex) { err.append(h('div', { class: 'banner error' }, h('p', {}, ex.message))); busy(btn, false); }
    } },
    h('div', { class: 'form-grid' },
      field('Project name', input('name', { value: (p && p.name) || '' }), { required: true, wide: true }),
      field('Your role on this job', select('claimant_role', roles, x.claimant_role || 'direct_contractor'), { hint: 'Sets which lien and notice deadlines apply to you.' }),
      field('Public or private job', select('public_or_private', [['private', 'Private'], ['public', 'Public (government agency)']], (p && p.public_or_private) || 'private'),
        { hint: 'Public = a city, county, state agency, school or other public owner.' }),
      field('Street address', input('address', { value: (p && p.address) || '', autocomplete: 'street-address' })),
      field('City', input('city', { value: (p && p.city) || '' })),
      field('County', input('county', { value: (p && p.county) || '', list: 'ca-counties', autocomplete: 'off' })),
      h('datalist', { id: 'ca-counties' }, CA_COUNTIES.map((c) => h('option', { value: c }))),
      field('Property owner', input('owner_name', { value: (p && p.owner_name) || '' })),
      field('Construction lender', input('lender_name', { value: (p && p.lender_name) || '' }), { hint: 'If there is a construction loan.' }),
      // B&P 7159.5(a)(1): the contract amount includes profit, labor and materials but not finance charges (2026 CSLB
      // Law Book, PDF p. 410, https://www.cslb.ca.gov/Resources/GuidesAndPublications/2026/2026_CSLB_Law_Book.pdf, read 2026-11-26)
      field('Contract price ($)', input('contract_price', { type: 'number', min: '0', step: 'any', inputmode: 'decimal', value: p && p.contract_price != null ? String(p.contract_price) : '' }),
        { hint: 'Your whole price for this job: labor, materials and profit. On a home improvement contract, leave out finance charges (B&P 7159.5).' }),
      h('h3', { class: 'wide form-section' }, 'Dates for the lien calendar', h('span', { class: 'hint' }, ' Leave blank what you don\'t know yet; add it later.')),
      field('Construction start date', dateIn('start_date')),
      field('First day you furnished labor or materials', dateIn('first_furnishing_date'), { hint: 'Starts the 20-day preliminary notice clock.' }),
      field('Last day you furnished labor or materials', dateIn('last_furnishing_date'), { hint: 'Your own last day of work or deliveries here, which can differ from the project\'s completion date.' }),
      field('Project completion date', dateIn('completion_date'), { hint: 'When the whole project was finished, if known.' }),
      field('Notice of completion recorded on', dateIn('noc_date'), { hint: 'Shortens lien deadlines if recorded on time.' }),
      field('Status', select('status', [['active', 'Active'], ['planned', 'Planned'], ['completed', 'Completed'], ['closed', 'Closed (stop tracking)']], (p && p.status) || 'active'))),
    err, h('div', { class: 'row' }, btn));
    return form;
  }

  routes.projects = async () => {
    const rows = await api('/api/projects');
    return h('div', {},
      h('div', { class: 'page-head' }, h('div', {}, h('h1', {}, 'Projects'), h('p', {}, 'Each project gets a lien calendar and a toolbox-talk schedule.')),
        can.write() ? h('a', { class: 'btn', href: '#/project/new' }, '+ Add project') : null),
      rows.length ? h('div', { class: 'grid grid-2' }, rows.map((p) => h('a', { class: 'card', href: `#/project/${p.id}` },
        h('div', { class: 'card-head' }, h('h2', {}, p.name), h('span', { class: 'pill gray' }, humanize(p.status))),
        h('p', { class: 'muted small' }, [p.address, p.city, p.county].filter(Boolean).join(', ') || 'No address yet'),
        h('div', { class: 'row small' }, h('span', { class: 'pill blue' }, p.public_or_private === 'public' ? 'Public' : 'Private'),
          h('span', {}, ((state.config && state.config.claimant_roles) || {})[(p.extra || {}).claimant_role || 'direct_contractor'] || ''))))) :
        h('div', { class: 'card empty' }, h('p', {}, 'No projects yet. Add one to build its lien calendar.'), can.write() ? h('a', { class: 'btn', href: '#/project/new' }, 'Add a project') : null));
  };

  routes.project = async ({ args }) => {
    const id = args[0];
    if (id === 'new') {
      if (!can.write()) return h('div', { class: 'card' }, h('p', {}, 'Ask the office to add projects.'));
      return h('div', {}, h('div', { class: 'page-head' }, h('h1', {}, 'Add a project')), h('div', { class: 'card' }, projectForm(null, (p) => go(`project/${p.id}`))));
    }
    const p = await api(`/api/projects/${encodeURIComponent(id)}`);
    const [dl, logs, docs, subs] = await Promise.all([
      api(`/api/projects/${p.id}/deadlines`).catch((e) => ({ error: e })),
      api(`/api/field_logs?project_id=${p.id}`).catch(() => []),
      api(`/api/documents?project_id=${p.id}`).catch(() => []),
      api(`/api/subs?project_id=${p.id}`).catch(() => [])]);
    const role = ((state.config && state.config.claimant_roles) || {})[(p.extra || {}).claimant_role || 'direct_contractor'];
    const edit = () => { const d = openDialog('Edit project', projectForm(p, () => { d.close(); render(); })); };
    const del = async () => { if (!confirm(`Delete project "${p.name}"? Its field logs and documents stay but lose the project link.`)) return; await api(`/api/projects/${p.id}`, { method: 'DELETE' }); toast('Project deleted'); go('projects'); };
    return h('div', {},
      h('div', { class: 'page-head' }, h('div', {}, h('p', {}, h('a', { href: '#/projects' }, '← Projects')), h('h1', {}, p.name), h('p', {}, [p.address, p.city, p.county].filter(Boolean).join(', '))),
        can.write() ? h('div', { class: 'row no-print' }, h('button', { type: 'button', class: 'btn secondary', onclick: edit }, 'Edit'), h('button', { type: 'button', class: 'btn danger', onclick: del }, 'Delete')) : null),
      h('div', { class: 'grid grid-2' },
        h('section', { class: 'card', 'aria-labelledby': 'lc-h' }, h('h2', { id: 'lc-h' }, 'Lien and safety calendar'),
          dl.error ? (dl.error.status === 402 ? subscribeCard(dl.error.message) : h('p', {}, dl.error.message)) : [alertList(dl.items, { empty: 'Add dates to this project to see its deadlines.', inProject: true }),
            dl.notes && dl.notes.length ? h('details', {}, h('summary', {}, 'How these dates were worked out'), h('ul', { class: 'bullets small' }, dl.notes.map((n) => h('li', {}, n)))) : null]),
        h('section', { class: 'card' }, h('h2', {}, 'Details'), h('dl', { class: 'kv' },
          h('dt', {}, 'Your role'), h('dd', {}, role || '—'),
          h('dt', {}, 'Job type'), h('dd', {}, p.public_or_private === 'public' ? 'Public' : 'Private'),
          h('dt', {}, 'Status'), h('dd', {}, humanize(p.status)),
          h('dt', {}, 'Owner'), h('dd', {}, p.owner_name || '—'),
          h('dt', {}, 'Lender'), h('dd', {}, p.lender_name || '—'),
          h('dt', {}, 'Contract price'), h('dd', {}, p.contract_price != null ? money(p.contract_price) : '—'),
          h('dt', {}, 'Start'), h('dd', {}, fmtDate(p.start_date)),
          h('dt', {}, 'First furnished'), h('dd', {}, fmtDate(p.first_furnishing_date)),
          h('dt', {}, 'Last furnished'), h('dd', {}, fmtDate(p.last_furnishing_date)),
          h('dt', {}, 'Completed'), h('dd', {}, fmtDate(p.completion_date)),
          h('dt', {}, 'Notice of completion'), h('dd', {}, fmtDate(p.noc_date))))),
      h('div', { class: 'grid grid-3' },
        h('section', { class: 'card' }, h('h2', {}, 'Subs on this job'), subs.length ? h('ul', { class: 'bullets' }, subs.map((s) => h('li', {}, s.name, s.scope ? ` (${s.scope})` : ''))) : h('p', { class: 'muted' }, 'None linked.')),
        h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, 'Field logs'), h('a', { class: 'btn secondary small', href: `#/field?project=${p.id}` }, 'New log')),
          logs.length ? h('ul', { class: 'bullets' }, logs.slice(0, 8).map((l) => h('li', {}, `${fmtDate(l.date)}: ${humanize(l.kind)}${l.topic ? ' – ' + l.topic : ''}`))) : h('p', { class: 'muted' }, 'No logs yet.')),
        h('section', { class: 'card' }, h('h2', {}, 'Documents'), docs.length ? h('ul', { class: 'bullets' }, docs.map((d) => h('li', {}, h('a', { href: `#/document/${d.id}` }, d.title)))) : h('p', { class: 'muted' }, 'None yet. Save tool results here.'))));
  };

  // ======================================================================== subcontractors
  const CSV_TEMPLATE = 'name,license_number,scope,required_class,coi_expiry,wc_required,w9_on_file,dir_number\nExample Plumbing,123456,Plumbing rough-in,C-36,2027-06-30,yes,yes,\n';
  function subForm(s, onSaved, projects) {
    const err = h('div', { role: 'alert' });
    const btn = h('button', { type: 'submit', class: 'btn' }, s ? 'Save' : 'Add subcontractor');
    const form = h('form', { novalidate: true, onsubmit: async (e) => {
      e.preventDefault(); err.replaceChildren();
      const data = formData(form);
      data.project_id = data.project_id ? Number(data.project_id) : null;
      if (!data.name) { err.append(h('div', { class: 'banner error' }, h('p', {}, 'Enter the company name.'))); return; }
      busy(btn, true, 'Saving…');
      try { await api(s ? `/api/subs/${s.id}` : '/api/subs', { method: s ? 'PATCH' : 'POST', body: data }); toast('Saved'); onSaved(); } catch (ex) { err.append(h('div', { class: 'banner error' }, h('p', {}, ex.message))); busy(btn, false); }
    } },
    h('div', { class: 'form-grid' },
      field('Company name', input('name', { value: (s && s.name) || '' }), { required: true }),
      field('CSLB license number', input('license_number', { value: (s && s.license_number) || '', inputmode: 'numeric' })),
      field('Scope of work', input('scope', { value: (s && s.scope) || '' })),
      field('License class the work needs', input('required_class', { value: (s && s.required_class) || '', placeholder: 'e.g. C-10' })),
      field('Insurance certificate (COI) expires', input('coi_expiry', { type: 'date', value: (s && s.coi_expiry) || '' })),
      field('DIR registration # (public works)', input('dir_number', { value: (s && s.dir_number) || '' })),
      field('Email', input('email', { type: 'email', value: (s && s.email) || '' })),
      field('Phone', input('phone', { type: 'tel', value: (s && s.phone) || '' })),
      field('Project', select('project_id', [['', 'Not linked'], ...projects], s && s.project_id ? String(s.project_id) : '')),
      h('div', { class: 'field' }, h('label', { class: 'switch' }, h('input', { type: 'checkbox', name: 'wc_required', checked: s ? s.wc_required : true }), h('span', {}, 'They will have employees on site'))),
      h('div', { class: 'field' }, h('label', { class: 'switch' }, h('input', { type: 'checkbox', name: 'w9_on_file', checked: s ? s.w9_on_file : false }), h('span', {}, 'W-9 on file')))),
    err, btn);
    return form;
  }
  function importPanel(onDone) {
    const file = h('input', { type: 'file', accept: '.csv,text/csv,text/plain', id: 'sub-csv', class: 'file-input' });
    const fileName = h('span', { class: 'small muted', 'aria-live': 'polite' }, 'No file chosen');
    file.addEventListener('change', () => { fileName.textContent = file.files[0] ? file.files[0].name : 'No file chosen'; });
    const paste = h('textarea', { id: 'sub-paste', rows: '5', class: 'mono', placeholder: CSV_TEMPLATE, spellcheck: 'false' });
    const out = h('div', { 'aria-live': 'polite' });
    const btn = h('button', { type: 'submit', class: 'btn' }, 'Import and check');
    const form = h('form', { onsubmit: async (e) => {
      e.preventDefault(); out.replaceChildren();
      let opts;
      if (file.files[0]) { const fd = new FormData(); fd.append('file', file.files[0]); opts = { method: 'POST', form: fd }; } else if (paste.value.trim()) opts = { method: 'POST', raw: paste.value, contentType: 'text/csv' };
      else { out.append(h('div', { class: 'banner error' }, h('p', {}, 'Choose a CSV file or paste your list.'))); return; }
      busy(btn, true, 'Checking every license…');
      try {
        const r = await api('/api/subs/import', opts);
        out.append(h('div', { class: 'banner info' }, h('p', {}, `${r.created} added, ${r.updated} updated${r.skipped.length ? `, ${r.skipped.length} skipped` : ''}.`)));
        if (r.skipped.length) out.append(h('p', { class: 'small' }, h('strong', {}, 'Not imported:')), h('ul', { class: 'bullets small' }, r.skipped.map((s) => h('li', {}, `Line ${s.line}: ${s.reason}`))));
        if (r.report_available) {
          out.append(h('div', { class: 'tiles static' }, ['red', 'amber', 'green'].map((k) => h('div', { class: `tile ${k}` }, h('span', { class: 'n' }, r.counts[k]), h('span', { class: 't' }, { red: 'High risk', amber: 'Need documents', green: 'Clear' }[k])))), subRiskTable(r.report));
        } else if (r.message) out.append(h('p', {}, r.message));
        file.value = ''; fileName.textContent = 'No file chosen'; if (onDone) onDone(r);
      } catch (ex) { out.append(h('div', { class: 'banner error' }, h('p', {}, ex.message))); } finally { busy(btn, false); }
    } },
    h('div', { class: 'grid grid-2' },
      h('div', { class: 'field' }, h('span', { class: 'label' }, 'CSV file'),
        h('div', { class: 'row' }, file, h('label', { for: 'sub-csv', class: 'btn secondary small' }, 'Choose CSV file…'), fileName),
        h('span', { class: 'hint' }, 'Export your vendor list from QuickBooks, Excel or Google Sheets as CSV. We read common headings such as Vendor, License #, Trade, Class, COI expiration and W-9. A license number is enough: we fill in the name from CSLB.')),
      field('…or paste the list', paste)),
    h('div', { class: 'row' }, btn, h('button', { type: 'button', class: 'btn ghost', onclick: () => download('tradecomply-subs-template.csv', CSV_TEMPLATE, 'text/csv') }, 'Download template')));
    return h('div', {}, form, out);
  }

  async function loadSubs() {
    try { return await api('/api/subs?check=1'); } catch (e) { if (e.status !== 402) throw e; return api('/api/subs'); }
  }
  routes.subs = async () => {
    const rows = await loadSubs();
    const projects = await projectOptions();
    const add = () => { const d = openDialog('Add subcontractor', subForm(null, () => { d.close(); render(); }, projects)); };
    const cols = [
      { label: 'Subcontractor', bare: true, render: (s) => h('div', {}, h('strong', {}, s.name), s.scope ? h('div', { class: 'small muted' }, s.scope) : null) },
      { label: 'License', render: (s) => s.license_number ? h('div', {}, s.license_number, s.check && s.check.license ? h('div', { class: 'small muted' }, `${humanize(s.check.license.status)}${s.check.license.classifications.length ? ' · ' + s.check.license.classifications.join(', ') : ''}`) : null) : '—' },
      { label: 'Risk', render: (s) => s.check ? ragPill(s.check.rag, RISK_TEXT[s.check.overall_risk]) : h('span', { class: 'pill gray' }, 'Not checked') },
      { label: 'Problems', render: (s) => s.check && s.check.problems.length ? h('ul', { class: 'bullets small' }, s.check.problems.map((p) => h('li', {}, p))) : (s.check ? 'None' : '—') },
      { label: 'COI expires', render: (s) => s.coi_expiry && s.coi_expiry < todayISO() ? h('span', { class: 'error-text' }, `${fmtDate(s.coi_expiry)} (expired)`) : fmtDate(s.coi_expiry) },
      { label: 'Actions', bare: true, render: (s) => can.write() ? h('div', { class: 'row' },
        h('button', { type: 'button', class: 'btn ghost small', onclick: () => { const d = openDialog(`Edit ${s.name}`, subForm(s, () => { d.close(); render(); }, projects)); } }, 'Edit'),
        h('button', { type: 'button', class: 'btn ghost small', onclick: async () => { if (confirm(`Remove ${s.name}?`)) { await api(`/api/subs/${s.id}`, { method: 'DELETE' }); toast('Removed'); render(); } } }, 'Remove')) : null },
    ];
    const tableSlot = h('section', { class: 'card', 'aria-label': 'Subcontractor list' });
    const drawTable = (list) => tableSlot.replaceChildren(list.length ? table(cols, worstFirst(list, (x) => x.check && x.check.overall_risk, (x) => x.check && x.check.rag))
      : h('div', { class: 'empty' }, h('p', {}, 'No subcontractors yet.'), h('p', { class: 'small' }, 'Import your list above, or add them one at a time.')));
    drawTable(rows);
    return h('div', {},
      h('div', { class: 'page-head' }, h('div', {}, h('h1', {}, 'Subcontractors'), h('p', {}, 'License, classification, insurance, workers\' comp and bond, checked against CSLB data.')),
        can.write() ? h('button', { type: 'button', class: 'btn', onclick: add }, '+ Add subcontractor') : null),
      can.write() ? h('details', { class: 'card', open: rows.length ? undefined : true }, h('summary', {}, h('strong', {}, 'Import your sub list (CSV)')),
        h('div', { class: 'result-section' }, importPanel(async () => { drawTable(await loadSubs()); }))) : null,
      tableSlot);
  };

  // ======================================================================== tools
  routes.tools = async ({ args }) => {
    const data = await getTools();
    const current = args[0] ? data.tools.find((t) => t.name === args[0]) : null;
    const search = h('input', { type: 'search', placeholder: 'Search tools', 'aria-label': 'Search tools' });
    const list = h('div', {});
    const drawList = () => {
      const q = search.value.trim().toLowerCase();
      list.replaceChildren(...data.modules.map((m) => {
        const tools = m.tools.filter((t) => !q || (t.title + ' ' + t.name + ' ' + t.description).toLowerCase().includes(q));
        if (!tools.length) return null;
        return h('details', { open: q || !current || (current && current.module === m.id) ? true : undefined },
          h('summary', {}, m.label, ' ', h('span', { class: 'count' }, `(${tools.length})`)),
          tools.map((t) => h('a', { href: `#/tools/${encodeURIComponent(t.name)}`, 'aria-current': current && current.name === t.name ? 'page' : undefined },
            t.title, h('span', { class: 'desc' }, firstSentence(t.description)))));
      }).filter(Boolean));
      if (!list.children.length) list.append(h('p', { class: 'muted' }, 'No tools match.'));
    };
    search.addEventListener('input', drawList);
    drawList();
    const panel = current ? toolPanel(current) : h('div', { class: 'card tool-intro' }, h('h2', {}, 'Pick a check'),
      h('p', {}, `${data.count} checks, grouped by topic. Each result shows its official source, whether that source has been verified, and a disclaimer. Save any result to Documents or print it.`),
      h('p', { class: 'small' }, 'When a result needs a licensed person (an engineer, an attorney, an asbestos consultant…), it says who, why and where to look.'),
      h('p', { class: 'muted small' }, 'Your AI assistant can run the same checks (Settings → AI assistant).'));
    return h('div', {},
      h('div', { class: 'page-head' }, h('div', {}, h('h1', {}, current ? current.title : 'Tools'), h('p', {}, current ? current.module_label : 'Checks and calculators built on California rules')),
        current ? h('a', { class: 'btn ghost small no-print', href: '#/tools' }, '← All tools')
          : h('a', { class: 'btn secondary small no-print nav-narrow', href: '#/professionals' }, 'Find a professional')),     // phones: not in the bottom bar
      h('div', { class: 'tool-layout' + (current ? ' has-tool' : '') }, h('aside', { class: 'card tool-list', 'aria-label': 'Tool list' }, search, list), panel));
  };

  // ======================================================================== licensed professionals: handoffs + finder
  // Some work California law reserves for a licensed or certified person (attorney, engineer, notary, asbestos
  // consultant and others). A result that needs one carries "handoffs" ([{type_id, name, why, legal_basis, verified,
  // find_url, county_filtered}]): the app says who, why and where to look, and links to the official public search.
  // It never names, ranks or recommends a person or firm and never copies results out of those sites.
  const OFFICIAL_NOTE = 'TradeComply links to official state lookups and doesn\'t recommend specific people.';
  // California's 58 counties as DIR's public works apprenticeship search lists them (dir.ca.gov/databases/das/
  // pwaddrstart.asp, read 2026-11-26). GET /api/professionals/types can send its own list; this is the fallback.
  const CA_COUNTIES = ['Alameda', 'Alpine', 'Amador', 'Butte', 'Calaveras', 'Colusa', 'Contra Costa', 'Del Norte', 'El Dorado', 'Fresno',
    'Glenn', 'Humboldt', 'Imperial', 'Inyo', 'Kern', 'Kings', 'Lake', 'Lassen', 'Los Angeles', 'Madera', 'Marin', 'Mariposa', 'Mendocino',
    'Merced', 'Modoc', 'Mono', 'Monterey', 'Napa', 'Nevada', 'Orange', 'Placer', 'Plumas', 'Riverside', 'Sacramento', 'San Benito',
    'San Bernardino', 'San Diego', 'San Francisco', 'San Joaquin', 'San Luis Obispo', 'San Mateo', 'Santa Barbara', 'Santa Clara',
    'Santa Cruz', 'Shasta', 'Sierra', 'Siskiyou', 'Solano', 'Sonoma', 'Stanislaus', 'Sutter', 'Tehama', 'Trinity', 'Tulare', 'Tuolumne',
    'Ventura', 'Yolo', 'Yuba'];
  // Only plain https addresses become links (the official pages in tradecomply/data/professionals.json are all
  // https): never javascript:, data: or http:, nor one with spaces, quotes, angle brackets or backslashes.
  const linkUrl = (u) => (typeof u === 'string' && /^https:\/\/[a-z0-9.-]+(:\d{1,5})?([\/?#][^\s"'<>\\`]*)?$/i.test(u.trim()) ? u.trim() : null);
  const hostOf = (u) => { try { return new URL(u).hostname.replace(/^www\./, ''); } catch (e) { return ''; } };
  const typeIdOf = (t) => String((t && (t.type_id || t.id)) || '');
  // "San Diego County", "san diego co." -> "San Diego"; anything that isn't one of the counties -> ''
  function countyMatch(text, counties = CA_COUNTIES) {
    const k = String(text || '').toLowerCase().replace(/\b(city and county of|county of|city and|county|co|california|ca)\b\.?/g, ' ')
      .replace(/[^a-z ]/g, ' ').replace(/\s+/g, ' ').trim();
    return (k && counties.find((c) => c.toLowerCase() === k)) || '';
  }
  function finderHref(typeId, projectId) {
    const q = new URLSearchParams();
    if (typeId) q.set('type', typeId);
    if (projectId) q.set('project', String(projectId));
    return '#/professionals' + (q.toString() ? `?${q}` : '');
  }
  // The requirement in plain words ("Required by law if this applies"). The API's label may end with "(legal basis
  // not yet verified)"; that is shown as the Unverified pill instead, so it isn't said twice.
  const REQ_TEXT = { required: 'Required by law', conditional: 'Required by law if this applies', recommended: 'Suggested, not required by law', optional: 'Optional' };
  const reqLabel = (x) => String(x.requirement_label || REQ_TEXT[x.requirement] || '').replace(/\s*\(legal basis not yet verified\)\s*$/i, '');
  const unverifiedPill = () => h('span', { class: 'pill amber' }, 'Unverified',
    h('span', { class: 'visually-hidden' }, ': the law behind this has not been checked against the official source yet'));
  const str = (v) => (typeof v === 'string' && v.trim() ? v.trim() : '');
  function officialLink(url, text) {
    // "Find one near the job · cab.ca.gov ↗" on one line; on a phone the address drops to its own line without the dot.
    // A screen reader hears "Find one near the job on cab.ca.gov (official site, opens in a new tab)".
    return h('a', { class: 'btn secondary small ho-find', href: url, target: '_blank', rel: 'noopener noreferrer' }, text,
      h('span', { class: 'ho-host' }, h('span', { class: 'ho-dot', 'aria-hidden': 'true' }, ' · '), h('span', { class: 'visually-hidden' }, ' on '), hostOf(url),
        h('span', { 'aria-hidden': 'true' }, ' ↗')),
      h('span', { class: 'visually-hidden' }, ' (official site, opens in a new tab)'));
  }
  const MAY_HAVE_MOVED = 'We couldn\'t open this page when we last checked it, so its address may have changed.';
  // The closed line has to read in one glance, so it names at most two people ("Licensed architect, Licensed
  // professional engineer and 6 more") and drops long explanations in brackets ("(civil or structural)"); short ones
  // that are part of the name ("(CASp)") stay. Opening the line shows everyone in full.
  const shortName = (n) => n.replace(/\s*\([^()]*\s[^()]*\)\s*$/, '') || n;
  // "May need" only when every item says it applies in some cases or is only suggested; anything required, or
  // anything that doesn't say, reads "Needs", so a required professional is never made to sound optional.
  const SOFT_REQ = new Set(['conditional', 'recommended', 'optional']);
  function handoffsSummary(items) {
    const names = [...new Set(items.map((x) => shortName(x.name)))];
    const may = items.every((x) => SOFT_REQ.has(x.req));
    const lead = `${may ? 'May need' : 'Needs'} ${names.length === 1 ? 'a licensed or qualified person' : 'licensed or qualified people'}:`;
    const shown = names.length <= 2 ? names.join(' and ') : `${names.slice(0, 2).join(', ')} and ${names.length - 2} more`;
    return [h('span', { class: 'ho-lead' }, lead), ' ', h('span', { class: 'ho-names' }, shown)];
  }
  // One collapsed line under a result: "Needs a licensed or qualified person: Notary public". Opening it shows, for
  // each: why (the law), when, a link to the official search near the job, and how to check them. With three or more
  // people each one is its own short row that opens on its own, so the open list isn't a wall of text.
  function handoffsLine(list, { projectId = null } = {}) {
    if (!Array.isArray(list)) return null;
    const items = list.filter((x) => x && typeof x === 'object' && (x.name || x.type_id)).map((x) => ({
      typeId: typeIdOf(x), name: String(x.name || humanize(x.type_id)), why: str(x.why), basis: str(x.legal_basis), verified: x.verified === true,
      url: linkUrl(x.find_url), countyFiltered: x.county_filtered === true, requirement: reqLabel(x), req: str(x.requirement), when: str(x.when),
      due: /^\d{4}-\d{2}-\d{2}$/.test(String(x.due_date || '')) ? x.due_date : null, label: str(x.find_label), owner: str(x.find_owner),
      unchecked: x.find_link_checked === false, checkHow: str(x.check_how),
      also: (Array.isArray(x.also) ? x.also : []).filter((a) => a && str(a.why)) }));
    if (!items.length) return null;
    const rows = items.length > 2;
    const head = (x, tag) => h(tag, { class: 'ho-name' }, h('strong', {}, x.name), x.requirement ? [' ', h('span', { class: 'pill gray' }, x.requirement)] : null, x.verified ? null : [' ', unverifiedPill()]);
    const body = (x) => [
      x.why ? h('p', { class: 'ho-why' }, prose(x.why)) : null,
      x.when || x.due ? h('p', { class: 'small' }, h('strong', {}, 'When: '), x.when, x.due ? `${x.when ? ' ' : ''}Key date: ${fmtDate(x.due)}.` : '') : null,
      x.basis ? h('p', { class: 'small muted' }, 'The law: ', stripRuleIds(x.basis)) : null,
      x.also.map((a) => h('p', { class: 'small' }, h('strong', {}, 'Also: '), prose(String(a.why)), a.legal_basis ? h('span', { class: 'muted' }, ` (${stripRuleIds(String(a.legal_basis))})`) : null,
        a.verified === false ? [' ', unverifiedPill()] : null)),
      h('div', { class: 'row ho-actions' }, x.url ? officialLink(x.url, 'Find one near the job') : h('span', { class: 'small muted' }, 'No official search page is linked for this one.'),
        x.typeId ? h('a', { class: 'small', href: finderHref(x.typeId, projectId) }, 'How to find and check one') : null),
      x.url ? h('p', { class: 'hint' }, x.label ? `${x.label}${x.owner ? `, run by ${x.owner}` : ''}. ` : x.owner ? `Run by ${x.owner}. ` : '',
        x.countyFiltered ? 'Opens already filtered to the job\'s county.' : 'Opens the statewide search; look for the job\'s area there.',
        x.unchecked ? ` ${MAY_HAVE_MOVED}` : '') : null,
      x.checkHow ? h('p', { class: 'small' }, h('strong', {}, 'Check them: '), x.checkHow) : null];
    return h('details', { class: 'handoffs' },
      h('summary', {}, handoffsSummary(items)),
      h('div', { class: 'ho-body' },
        h('ul', { class: 'ho-list' + (rows ? ' ho-rows' : '') }, items.map((x) => h('li', { class: 'ho-item' },
          rows ? h('details', { class: 'ho-more' }, h('summary', {}, head(x, 'span')), h('div', { class: 'ho-more-body' }, body(x))) : [head(x, 'p'), body(x)]))),
        h('p', { class: 'small muted ho-note' }, OFFICIAL_NOTE)));
  }
  // Printed results keep the list: open every handoff line (and each person in it) for printing, then put them back.
  const HO_DETAILS = 'details.handoffs, details.handoffs details.ho-more';
  window.addEventListener('beforeprint', () => document.querySelectorAll(HO_DETAILS).forEach((d) => { if (!d.open) { d.open = true; d.dataset.printOpened = '1'; } }));
  window.addEventListener('afterprint', () => document.querySelectorAll(HO_DETAILS).forEach((d) => { if (d.dataset.printOpened) { d.open = false; delete d.dataset.printOpened; } }));

  // ---------- the "Find a professional" page (GET /api/professionals/types, GET /api/professionals/find)
  const notOnServer = (e) => !!e && (e.status === 501 || e.status === 405 || (e.status === 404 && (!e.data || !e.data.detail || e.data.detail === 'Not Found')));
  const isOffline = (e) => !!e && (e.status === 0 || !!(e.data && e.data.offline));
  function finderProblem(e, retry) {
    const again = retry ? h('button', { type: 'button', class: 'btn secondary small', onclick: retry }, 'Try again') : null;
    if (isOffline(e)) return h('div', { class: 'banner warn' }, icon('cloud-off'), h('p', {}, 'You\'re offline. The official lookups need a connection; try again when you have signal.'), again);
    if (notOnServer(e)) return h('div', { class: 'banner info' }, h('p', {}, 'The professional finder isn\'t turned on on this server yet. ', supportText('To have it turned on')));
    return h('div', { class: 'banner error' }, h('p', {}, e && e.message ? e.message : 'Something went wrong.'), again);
  }
  function normTypes(r) {
    const list = Array.isArray(r) ? r : (r && (r.types || r.items)) || [];
    return list.filter((t) => t && typeof t === 'object').map((t) => ({ id: typeIdOf(t), name: String(t.name || t.label || humanize(typeIdOf(t))), desc: String(t.plain_description || t.description || '') }))
      .filter((t) => t.id).sort((a, b) => a.name.localeCompare(b.name));
  }
  function normFound(r) {
    const list = Array.isArray(r) ? r : r && Array.isArray(r.results) ? r.results : r && Array.isArray(r.professionals) ? r.professionals : r && typeof r === 'object' ? [r] : [];
    return list.filter((x) => x && typeof x === 'object' && (x.name || x.type_id || x.id));
  }
  function foundLinks(x) {
    const raw = [].concat(Array.isArray(x.find) ? x.find : [], Array.isArray(x.links) ? x.links : [], Array.isArray(x.directories) ? x.directories : []);
    const out = raw.filter((f) => f && typeof f === 'object').map((f) => ({ label: String(f.label || f.name || 'Official search'), url: linkUrl(f.find_url || f.url),
      owner: str(f.official_owner) || str(f.agency), countyFiltered: f.county_filtered === true, how: str(f.how_to_search),
      unchecked: f.link_checked === false || (typeof f.status === 'string' && f.status.startsWith('blocked')),
      checkedOn: f.link_checked === true && /^\d{4}-\d{2}-\d{2}$/.test(String(f.last_checked || '')) ? f.last_checked : null,
      elsewhere: f.applies_here === false ? (str(f.only_for) || 'another area') : null }))
      .filter((f) => f.url);
    if (!out.length && linkUrl(x.find_url)) out.push({ label: 'Official search', url: linkUrl(x.find_url), owner: str(x.official_owner), countyFiltered: x.county_filtered === true });
    return out;
  }
  // Situations from the registry: the law requires one / suggests one / doesn't require one. Collapsed.
  function basisList(summary, list) {
    const rows = (Array.isArray(list) ? list : []).filter((b) => b && str(b.situation));
    if (!rows.length) return null;
    return h('details', { class: 'more' }, h('summary', {}, `${summary} (${rows.length})`),
      h('ul', { class: 'bullets small' }, rows.map((b) => {
        const ok = typeof b.verified === 'boolean' ? b.verified : b.status === 'confirmed';
        return h('li', {}, String(b.situation), str(b.legal_basis) ? h('span', { class: 'muted' }, ` (${stripRuleIds(b.legal_basis)})`) : null, ' ',
          ok ? h('span', { class: 'pill green' }, 'verified') : h('span', { class: 'pill amber' }, 'unverified'));
      })));
  }
  function foundEntry(x, county, k) {
    const id = `pf-e-${k}`;
    const links = foundLinks(x);
    const bring = (Array.isArray(x.bring_or_ask) ? x.bring_or_ask : []).filter((s) => str(s));
    const notes = (Array.isArray(x.notes) ? x.notes : []).filter((s) => str(s));
    const others = (Array.isArray(x.other_possible_types) ? x.other_possible_types : []).filter((o) => o && typeIdOf(o));
    const sources = Array.isArray(x.citations) && x.citations.length ? x.citations : null;
    return h('article', { class: 'card pf-entry', 'aria-labelledby': id },
      h('h2', { id }, String(x.name || humanize(typeIdOf(x))), x.verified === false ? [' ', unverifiedPill()] : null),
      str(x.plain_description) ? h('p', {}, x.plain_description) : null,
      str(x.why) ? h('p', {}, prose(x.why)) : null,
      str(x.legal_basis) ? h('p', { class: 'small muted' }, 'The law: ', stripRuleIds(x.legal_basis)) : null,
      h('h3', {}, 'Find one'),
      links.length ? h('ul', { class: 'pf-links clean' }, links.map((l) => h('li', { class: 'pf-link' },
        officialLink(l.url, l.label),
        l.owner ? h('p', { class: 'small muted' }, `Run by ${l.owner}.`) : null,
        l.elsewhere ? h('p', { class: 'hint' }, `This page covers ${l.elsewhere} only.`) : null,
        // the API's own sentence says what the link opens (and whether it could be checked); otherwise ours
        l.how ? h('p', { class: 'hint' }, l.how)
          : [h('p', { class: 'hint' }, l.countyFiltered ? `Opens already filtered to ${county ? `${county} County` : 'the job\'s county'}.` : 'Opens the statewide search; look for the job\'s area there.'),
            l.unchecked ? h('p', { class: 'hint' }, MAY_HAVE_MOVED) : null],
        l.checkedOn ? h('p', { class: 'hint' }, `Link checked ${fmtDate(l.checkedOn)}.`) : null)))
        : h('p', { class: 'muted' }, 'No official public search is listed for this one yet. Ask the state agency that issues this license or certificate.'),
      str(x.verify_license_how) ? [h('h3', {}, 'How to check their license'), h('p', {}, x.verify_license_how)] : null,
      notes.length ? h('ul', { class: 'bullets small muted' }, notes.map((n) => h('li', {}, prose(n)))) : null,
      basisList('When the law requires one', x.when_required),
      basisList('When it\'s suggested but not required', x.optional_when),
      basisList('When you don\'t need one', x.not_required_when),
      bring.length ? h('details', { class: 'more' }, h('summary', {}, 'What to bring and ask'), h('ul', { class: 'bullets small' }, bring.map((s) => h('li', {}, s)))) : null,
      sources ? h('details', { class: 'more' }, h('summary', {}, `Sources (${sources.length})`), citationList(sources)) : null,
      others.length ? h('p', { class: 'small' }, 'You might also mean: ', others.map((o, i) => [i ? ', ' : '', h('a', { href: finderHref(typeIdOf(o)) }, String(o.name || humanize(typeIdOf(o))))])) : null);
  }
  routes.professionals = async ({ query }) => {
    const head = h('div', { class: 'page-head' }, h('div', {}, h('h1', {}, 'Find a licensed professional'),
      h('p', {}, 'When a job needs a licensed or certified person, find one near the job and check their license on the official state lookups.')));
    let typesRes;
    try { typesRes = await api('/api/professionals/types'); } catch (e) {
      return h('div', {}, head, finderProblem(e, () => render()), h('p', { class: 'small muted' }, OFFICIAL_NOTE));
    }
    const types = normTypes(typesRes);
    const counties = typesRes && Array.isArray(typesRes.counties) && typesRes.counties.length && typesRes.counties.every((c) => typeof c === 'string') ? typesRes.counties : CA_COUNTIES;
    if (!types.length) return h('div', {}, head, h('div', { class: 'card empty' }, h('p', {}, 'No kinds of professionals are listed on this server yet.')), h('p', { class: 'small muted' }, OFFICIAL_NOTE));
    const projects = await api('/api/projects').catch(() => []);
    const lastKey = `tc.finder.project.${state.me.tenant.id}`;
    const active = projects.filter((p) => !['closed', 'completed'].includes(p.status));
    let projectId = query.get('project') || local.get(lastKey) || (active.length === 1 ? String(active[0].id) : '');
    if (!projects.some((p) => String(p.id) === projectId)) projectId = '';
    const projectOf = (id) => projects.find((p) => String(p.id) === String(id)) || null;
    const startCounty = countyMatch(query.get('county'), counties) || (projectId ? countyMatch(projectOf(projectId).county, counties) : '');
    const wanted = query.get('type');
    const typeSel = select('type', [['', 'Choose…'], ...types.map((t) => [t.id, t.name])], types.some((t) => t.id === wanted) ? wanted : '', { id: 'pf-type', 'aria-describedby': 'pf-type-h' });
    const typeHint = h('span', { class: 'hint', id: 'pf-type-h' });
    const showDesc = () => { const t = types.find((x) => x.id === typeSel.value); typeHint.textContent = t && t.desc ? firstSentence(t.desc) : ''; };
    typeSel.addEventListener('change', showDesc);
    showDesc();
    const countySel = select('county', [['', 'Not sure (statewide)'], ...counties.map((c) => [c, c])], startCounty, { id: 'pf-county', 'aria-describedby': 'pf-county-h' });
    const countyHint = h('span', { class: 'hint', id: 'pf-county-h' }, 'Where the work is. Some state sites open already filtered to it.');
    const projHint = h('span', { class: 'hint', id: 'pf-project-h' }, 'Fills in the job\'s county.');
    let projSel = null;
    if (projects.length) {
      projSel = select('project', [['', 'No project'], ...projects.map((p) => [String(p.id), p.name])], projectId, { id: 'pf-project', 'aria-describedby': 'pf-project-h' });
      projSel.addEventListener('change', () => {
        const p = projectOf(projSel.value);
        if (p) local.set(lastKey, String(p.id)); else local.del(lastKey);
        const c = p ? countyMatch(p.county, counties) : '';
        if (c) countySel.value = c;
        projHint.textContent = p && !c ? `“${p.name}” has no county we recognize. Pick the county below, or add it to the project.` : 'Fills in the job\'s county.';
      });
    }
    const status = h('p', { class: 'visually-hidden', role: 'status' });
    const out = h('div', { class: 'pf-results' });
    const btn = h('button', { type: 'submit', class: 'btn' }, 'Show official lookups');
    // The "doesn't recommend" note sits under the form until results show; then the results' own (fuller) note
    // replaces it, so it isn't said twice on one screen.
    const formNote = h('p', { class: 'small muted pf-note' }, OFFICIAL_NOTE);
    // After a tap on the button (not when the page opens from a result), bring what came back into view if it
    // landed below the fold or behind the phone's bottom bar.
    const reveal = (asked) => { if (asked && out.firstElementChild && out.getBoundingClientRect().top > window.innerHeight - 160) out.firstElementChild.scrollIntoView({ behavior: scrollBehavior(), block: 'start' }); };
    const run = async (asked = false) => {
      fill(out);
      formNote.hidden = false;
      status.textContent = '';
      if (!typeSel.value) { out.append(msgBox('error', 'Choose the kind of professional you need.')); typeSel.focus(); return; }
      const t = types.find((x) => x.id === typeSel.value);
      const q = new URLSearchParams({ type_id: typeSel.value });
      if (countySel.value) q.set('county', countySel.value);
      const qs = new URLSearchParams({ type: typeSel.value });
      if (countySel.value) qs.set('county', countySel.value);
      if (projSel && projSel.value) qs.set('project', projSel.value);
      history.replaceState(null, '', `#/professionals?${qs}`);
      busy(btn, true, 'Looking up…');
      try {
        const found = normFound(await api(`/api/professionals/find?${q}`));
        if (!found.length) {
          fill(out, h('div', { class: 'card empty' }, h('p', {}, `No official lookup is listed for ${t ? t.name.toLowerCase() : 'this'} yet. Ask the state agency that issues this license or certificate.`)));
          status.textContent = 'No official lookups found.';
        } else {
          const checked = found.map((x) => x.checked_on).find((d) => /^\d{4}-\d{2}-\d{2}$/.test(String(d || '')));
          fill(out, found.map((x, k) => foundEntry(x, countySel.value, k)),
            h('p', { class: 'small muted' }, str(found[0].neutral_note) || `${OFFICIAL_NOTE} Choosing and hiring someone is up to you.`,
              checked ? ` Directory list checked ${fmtDate(checked)}.` : ''));
          formNote.hidden = true;
          const n = found.reduce((s, x) => s + foundLinks(x).length, 0);
          status.textContent = `Showing ${plural(n, 'official lookup')} for ${t ? t.name : 'this'}${countySel.value ? ` near ${countySel.value} County` : ''}.`;
        }
      } catch (e) {
        fill(out, finderProblem(e, () => run(true)));
        status.textContent = isOffline(e) ? 'You are offline.' : 'The lookup did not load.';
      } finally { busy(btn, false); }
      reveal(asked);
    };
    const form = h('form', { novalidate: true, onsubmit: (e) => { e.preventDefault(); run(true); } },
      h('div', { class: 'form-grid' },
        h('div', { class: 'field wide' }, h('label', { for: 'pf-type' }, 'What do you need?'), typeSel, typeHint),
        projSel ? h('div', { class: 'field' }, h('label', { for: 'pf-project' }, 'Job (optional)'), projSel, projHint) : null,
        h('div', { class: 'field' }, h('label', { for: 'pf-county' }, 'County of the job'), countySel, countyHint)),
      h('div', { class: 'row' }, btn));
    if (typeSel.value) setTimeout(run, 0);         // came from a result's "How to find and check one"
    return h('div', {}, head,
      h('section', { class: 'card', 'aria-label': 'What you need and where' }, form, formNote),
      status, out);
  };

  // ======================================================================== documents
  function docEditor(doc, onSaved) {
    const err = h('div', { role: 'alert' });
    const ta = h('textarea', { name: 'body_markdown', rows: '14', class: 'mono', value: (doc && doc.body_markdown) || '' });
    const preview = h('div', { class: 'md md-paper' });
    const btn = h('button', { type: 'submit', class: 'btn' }, 'Save');
    const form = h('form', { onsubmit: async (e) => {
      e.preventDefault(); err.replaceChildren();
      const data = formData(form);
      if (!data.title) { err.append(h('div', { class: 'banner error' }, h('p', {}, 'Add a title.'))); return; }
      busy(btn, true, 'Saving…');
      try { const saved = await api(doc ? `/api/documents/${doc.id}` : '/api/documents', { method: doc ? 'PATCH' : 'POST', body: { title: data.title, body_markdown: ta.value } }); toast('Saved'); onSaved(saved); } catch (ex) { err.append(h('div', { class: 'banner error' }, h('p', {}, ex.message))); busy(btn, false); }
    } },
    field('Title', input('title', { value: (doc && doc.title) || '' }), { required: true }),
    field('Text', ta, { hint: 'Markdown: # Heading, - bullet, **bold**, | table |.' }),
    h('details', { ontoggle: () => { preview.innerHTML = md(ta.value); } }, h('summary', {}, 'Preview'), preview),
    err, h('div', { class: 'row' }, btn));
    return form;
  }
  const DOC_KIND = { tool_result: 'Check result', note: 'Note', program: 'Safety program' };
  function docExcerpt(d) {
    // skip the title line and the "verified" line: the card already shows both
    const lines = String(d.excerpt || '').split('\n').map((l) => l.trim())
      .filter((l) => l && !/^#\s/.test(l) && !/^\*\*(Not yet verified|Verified against)/.test(l) && !/^(-{3,}|\|[-| :]+\|)$/.test(l))
      .map((l) => l.replace(/^#+\s*/, '').replace(/^([-*+]|\d+[.)])\s+/, '').replace(/\*\*|__|`|\|/g, ' ').trim());
    return lines.join(' · ').replace(/\s+/g, ' ').slice(0, 170) || 'No text yet.';
  }
  routes.documents = async () => {
    const rows = await api('/api/documents');
    const add = () => { const d = openDialog('New document', docEditor(null, (doc) => { d.close(); go(`document/${doc.id}`); })); };
    return h('div', {},
      h('div', { class: 'page-head' }, h('div', {}, h('h1', {}, 'Documents'), h('p', {}, 'Safety programs, notices, prequal reports and saved tool results.')),
        can.write() ? h('button', { type: 'button', class: 'btn', onclick: add }, '+ New document') : null),
      rows.length ? h('div', { class: 'grid grid-2' }, rows.map((d) => h('a', { class: 'card card-link', href: `#/document/${d.id}` },
        h('div', { class: 'card-head' }, h('h2', {}, d.title), d.verified === null || d.verified === undefined ? null : verifiedBadge({ verified: !!d.verified })),
        h('p', { class: 'small muted' }, `${DOC_KIND[d.kind] || humanize(d.kind)} · ${fmtDateTime(d.created)}${d.source_tool ? ' · from ' + humanize(d.source_tool) : ''}`),
        h('p', { class: 'small excerpt' }, docExcerpt(d))))) :
        h('div', { class: 'card empty' }, h('p', {}, 'No documents yet. Run a tool and choose "Save as document".'), h('a', { class: 'btn secondary', href: '#/tools' }, 'Open tools')));
  };
  // the page already shows the title (and the print header repeats it), so drop a matching first heading
  const stripTitle = (body, title) => String(body || '').replace(/^\s*#\s+(.+)\n+/, (m, t) => (t.trim() === String(title).trim() ? '' : m));
  routes.document = async ({ args }) => {
    const d = await api(`/api/documents/${encodeURIComponent(args[0])}`);
    const edit = () => { const dlg = openDialog('Edit document', docEditor(d, () => { dlg.close(); render(); })); };
    const del = async () => { if (!confirm(`Delete "${d.title}"?`)) return; await api(`/api/documents/${d.id}`, { method: 'DELETE' }); toast('Deleted'); go('documents'); };
    return h('div', {},
      h('div', { class: 'page-head no-print' }, h('div', {}, h('p', {}, h('a', { href: '#/documents' }, '← Documents')), h('h1', {}, d.title),
        h('p', {}, `${humanize(d.kind)} · created ${fmtDateTime(d.created)}${d.created_by ? ' by ' + d.created_by : ''}`)),
      h('div', { class: 'row' }, d.verified === null || d.verified === undefined ? null : verifiedBadge({ verified: !!d.verified }),
        h('button', { type: 'button', class: 'btn secondary', onclick: () => window.print() }, 'Print / PDF'),
        h('button', { type: 'button', class: 'btn ghost', onclick: () => download(`${d.title.replace(/[^\w.-]+/g, '_').slice(0, 80)}.md`, d.body_markdown, 'text/markdown') }, 'Download'),
        can.write() ? h('button', { type: 'button', class: 'btn ghost', onclick: edit }, 'Edit') : null,
        can.write() ? h('button', { type: 'button', class: 'btn danger', onclick: del }, 'Delete') : null)),
      h('div', { class: 'print-only print-head' }, h('div', { class: 'small' }, h('strong', {}, state.me.tenant.company_name), state.me.tenant.cslb_license ? ` · CSLB license #${state.me.tenant.cslb_license}` : '',
        ` · printed ${fmtDate(todayISO())}`), h('h1', {}, d.title)),
      h('article', { class: 'card' }, h('div', { class: 'md', html: md(stripTitle(d.body_markdown, d.title)) }),
        h('p', { class: 'print-only small muted' }, 'Prepared with TradeComply CA. Compliance information, not legal advice.')));
  };

  // ======================================================================== field (offline-first)
  function signaturePad() {
    const canvas = h('canvas', { class: 'sig-pad', role: 'img', 'aria-label': 'Signature area. Sign with a finger, pen or mouse.' });
    let ctx = null, drawing = false, signed = false, last = null;
    const setup = () => {
      const ratio = Math.min(window.devicePixelRatio || 1, 2);
      const rect = canvas.getBoundingClientRect();
      canvas.width = Math.max(1, Math.round(rect.width * ratio)); canvas.height = Math.max(1, Math.round(rect.height * ratio));
      ctx = canvas.getContext('2d'); ctx.scale(ratio, ratio);
      ctx.lineWidth = 2.4; ctx.lineCap = 'round'; ctx.lineJoin = 'round'; ctx.strokeStyle = '#111';
    };
    const pos = (e) => { const r = canvas.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top }; };
    canvas.addEventListener('pointerdown', (e) => {
      if (!ctx) setup();
      drawing = true; last = pos(e); canvas.setPointerCapture(e.pointerId); e.preventDefault();
    });
    canvas.addEventListener('pointermove', (e) => {
      if (!drawing) return;
      const p = pos(e);
      ctx.beginPath(); ctx.moveTo(last.x, last.y); ctx.lineTo(p.x, p.y); ctx.stroke();
      last = p;
      if (!signed) { signed = true; canvas.classList.add('signed'); canvas.setAttribute('aria-label', 'Signed'); }
    });
    const end = () => { drawing = false; };
    canvas.addEventListener('pointerup', end); canvas.addEventListener('pointercancel', end);
    return {
      el: canvas,
      signed: () => signed,
      data: () => (signed ? canvas.toDataURL('image/png') : null),
      clear: () => { if (ctx) ctx.clearRect(0, 0, canvas.width, canvas.height); signed = false; canvas.classList.remove('signed'); canvas.setAttribute('aria-label', 'Signature area. Sign with a finger, pen or mouse.'); },
    };
  }
  const TOPICS = ['Fall protection', 'Heat illness prevention', 'Ladder safety', 'Trenching and excavation', 'Silica dust', 'Electrical safety / lockout', 'Scaffolds', 'Housekeeping and slips', 'PPE', 'Hand and power tools', 'Struck-by hazards', 'Fire prevention', 'Emergency action plan'];
  routes.field = async ({ query }) => {
    let projects = [];
    try { projects = (await api('/api/projects')).filter((p) => p.status !== 'closed'); } catch (e) { /* offline with no cache */ }
    const attendees = [];
    const list = h('div', {});
    const addAttendee = (name = '') => {
      const pad = signaturePad();
      const nameIn = h('input', { type: 'text', class: 'attendee-name', placeholder: 'e.g. Luis Ramirez', value: name, autocomplete: 'off' });
      const ack = h('input', { type: 'checkbox' });
      const row = h('div', { class: 'sig-row' });
      const entry = { pad, nameIn, ack, row };
      const n = attendees.length + 1;
      row.append(h('div', { class: 'field' }, h('label', {}, `Person ${n}: full name`, nameIn)),
        h('div', { class: 'sig-label' }, h('span', { class: 'label' }, 'Signature'), h('span', { class: 'hint' }, 'Sign in the box with a finger or stylus.')),
        pad.el, h('div', { class: 'row' }, h('button', { type: 'button', class: 'btn ghost small', onclick: () => pad.clear() }, 'Clear signature'),
          h('button', { type: 'button', class: 'btn ghost small', onclick: () => { attendees.splice(attendees.indexOf(entry), 1); row.remove(); } }, 'Remove person')),
        h('label', { class: 'switch small-switch' }, ack, h('span', {}, 'Was there but can\'t sign (mark as attended)')));
      attendees.push(entry); list.append(row);
      return entry;
    };
    const err = h('div', { role: 'alert' });
    const btn = h('button', { type: 'submit', class: 'btn' }, 'Save log');
    const kind = select('kind', [['toolbox_talk', 'Toolbox / tailgate talk'], ['inspection', 'Daily inspection'], ['incident', 'Incident first report']], 'toolbox_talk');
    const form = h('form', { novalidate: true, onsubmit: async (e) => {
      e.preventDefault(); err.replaceChildren();
      const data = formData(form);
      if (!data.date) { err.append(h('div', { class: 'banner error' }, h('p', {}, 'Pick a date.'))); return; }
      const people = attendees.map((a) => ({ name: a.nameIn.value.trim(), signature: a.pad.data(), acknowledged: a.ack.checked, signed_at: a.pad.signed() ? new Date().toISOString().slice(0, 16) : null })).filter((a) => a.name);
      if (data.kind === 'toolbox_talk' && !people.length) { err.append(h('div', { class: 'banner error' }, h('p', {}, 'Add at least one attendee.'))); return; }
      const log = { client_uuid: uuid(), project_id: data.project_id ? Number(data.project_id) : null, kind: data.kind, date: data.date, topic: data.topic, notes: data.notes, attendees: people, _tenant: state.me.tenant.id, _saved: new Date().toISOString() };
      busy(btn, true, 'Saving…');
      try {
        await Outbox.put(log);
        const res = navigator.onLine ? await syncOutbox({ quiet: true }) : null;
        toast(res && res.created ? 'Saved and synced' : 'Saved on this device. It will sync when you have signal.');
        render();
      } catch (ex) { err.append(h('div', { class: 'banner error' }, h('p', {}, ex.message))); busy(btn, false); }
    } },
    h('div', { class: 'form-grid' },
      field('Type', kind),
      field('Project', select('project_id', [['', 'No project'], ...projects.map((p) => [String(p.id), p.name])],
        query && query.get('project') && projects.some((p) => String(p.id) === query.get('project')) ? query.get('project') : projects.length === 1 ? String(projects[0].id) : '')),
      field('Date', input('date', { type: 'date', value: todayISO() }), { required: true }),
      field('Topic', input('topic', { list: 'topic-list', autocomplete: 'off' }), { hint: 'What you covered or inspected.' }),
      h('datalist', { id: 'topic-list' }, TOPICS.map((t) => h('option', { value: t }))),
      field('Notes', h('textarea', { name: 'notes', rows: '3' }), { wide: true })),
    h('fieldset', {}, h('legend', {}, 'Attendees and signatures'), list,
      h('button', { type: 'button', class: 'btn secondary small', onclick: () => { const a = addAttendee(); a.nameIn.focus(); } }, '+ Add person')),
    err, h('div', { class: 'row' }, btn));
    addAttendee();
    const queue = await Outbox.all().catch(() => []);
    let recent = [];
    try { recent = await api('/api/field_logs'); } catch (e) { /* offline */ }
    const projName = (id) => (projects.find((p) => p.id === id) || {}).name || '';
    const syncBtn = h('button', { type: 'button', class: 'btn secondary small', onclick: async () => { busy(syncBtn, true, 'Syncing…'); await syncOutbox({ quiet: false }); busy(syncBtn, false); render(); } }, 'Sync now');
    return h('div', {},
      h('div', { class: 'page-head' }, h('div', {}, h('h1', {}, 'Field log'), h('p', {}, 'Toolbox talks, inspections and incident reports. Works without signal.')),
        h('div', { class: 'queue' }, h('span', { id: 'queue-count', class: `pill ${queue.length ? 'amber' : 'green'}` }, queue.length ? `${queue.length} waiting to sync` : 'All field logs synced'), queue.length && navigator.onLine ? syncBtn : null)),
      h('section', { class: 'card' }, form),
      h('section', { class: 'card' }, h('h2', {}, 'Recent logs'),
        queue.length ? h('ul', { class: 'bullets' }, queue.map((q) => h('li', {}, `${fmtDate(q.date)} · ${humanize(q.kind)}${q.topic ? ' – ' + q.topic : ''} `, h('span', { class: 'pill amber' }, q._error ? `Needs fixing: ${q._error}` : 'Waiting to sync')))) : null,
        recent.length ? table([
          { label: 'Date', render: (l) => fmtDate(l.date) }, { label: 'Type', render: (l) => humanize(l.kind) },
          { label: 'Project', render: (l) => projName(l.project_id) || '—' }, { label: 'Topic', key: 'topic' },
          { label: 'People', render: (l) => { const a = l.attendees || []; const att = a.filter((x) => x.signed).length; return `${plural(a.length, 'person', 'people')}: ${l.signature_count || 0} signed${att > (l.signature_count || 0) ? `, ${att - (l.signature_count || 0)} marked attended` : ''}`; } },
          { label: 'By', key: 'created_by' }], recent.slice(0, 30)) :
          (queue.length ? null : h('p', { class: 'muted' }, navigator.onLine ? 'No logs yet.' : 'Connect to see logs from the office.'))));
  };

  // ======================================================================== AI agent connection (settings + onboarding)
  function agentSnippets(url, key) {
    const k = key || '<YOUR_API_KEY>';
    return {
      code: `claude mcp add --transport http tradecomply ${url} --header "Authorization: Bearer ${k}"`,
      desktop: JSON.stringify({ mcpServers: { tradecomply: { command: 'npx', args: ['-y', 'mcp-remote', url, '--header', 'Authorization:${TC_AUTH}'], env: { TC_AUTH: `Bearer ${k}` } } } }, null, 2),
      local: JSON.stringify({ mcpServers: { tradecomply: { command: 'python', args: ['/path/to/TradeComply/server.py'] } } }, null, 2),
    };
  }
  function agentConnect() {
    const url = (state.me && state.me.mcp_url) || (state.config && state.config.mcp_url) || location.origin + '/mcp';
    const snippetSlot = h('div', {});
    const keySlot = h('div', { 'aria-live': 'polite' });
    const drawSnippets = (key) => {
      const s = agentSnippets(url, key);
      snippetSlot.replaceChildren(
        h('h3', {}, key ? '2. Paste it into your assistant' : 'Set up your assistant'),
        h('details', { class: 'snippet' }, h('summary', {}, 'Claude Desktop (config file)'),
          h('p', { class: 'small muted' }, 'In Claude Desktop: Settings → Developer → Edit config. Paste this into claude_desktop_config.json, save, then restart Claude Desktop. Needs Node.js installed.'), copyBox(s.desktop, 'Claude Desktop config', true)),
        h('details', { class: 'snippet' }, h('summary', {}, 'Claude Code (terminal)'), copyBox(s.code, 'Claude Code command', true)),
        h('details', { class: 'snippet' }, h('summary', {}, 'Other assistants (ChatGPT, Copilot, Procore, Autodesk)'),
          h('p', { class: 'small muted' }, 'Add a remote MCP server with the URL above and this header:'), copyBox(`Authorization: Bearer ${key || '<YOUR_API_KEY>'}`, 'header', true)),
        h('details', { class: 'snippet' }, h('summary', {}, 'Without internet, on a computer with TradeComply installed'),
          h('p', { class: 'small muted' }, 'Runs the checks from a local copy over stdio. Change the path to where TradeComply is installed.'), copyBox(s.local, 'local config', true)));
    };
    drawSnippets(null);
    const nameIn = input('key_name', { value: 'Claude Desktop', id: 'key-name-' + (++fieldSeq) });
    const expIn = select('key_expiry', [['', 'Never expires'], ['30', 'Expires in 30 days'], ['90', 'Expires in 90 days'], ['365', 'Expires in 1 year']], '', { id: 'key-exp-' + fieldSeq, 'aria-label': 'Key expiry' });
    const btn = h('button', { type: 'button', class: 'btn', onclick: async () => {
      busy(btn, true, 'Creating…');
      try {
        const body = { name: nameIn.value.trim() || 'AI assistant' };
        if (expIn.value) body.expires_in_days = Number(expIn.value);
        const k = await api('/api/keys', { method: 'POST', body });
        keySlot.replaceChildren(h('div', { class: 'secret' }, h('p', {}, h('strong', {}, 'Your new API key. Copy it now: it will not be shown again.'), ' The setup below already includes it.'), copyBox(k.key, 'API key')));
        drawSnippets(k.key);
        if (state.onKeysChanged) state.onKeysChanged().catch(() => {});
      } catch (e) { toast(e.message, true); } finally { busy(btn, false); }
    } }, 'Create API key');
    return h('div', {},
      h('p', {}, 'Your AI assistant (Claude, ChatGPT, Copilot or a Procore/Autodesk agent) can run the same California checks. It connects over MCP, the standard way assistants plug into tools. Every call is logged in your activity log.'),
      h('div', { class: 'field' }, h('span', { class: 'label' }, 'Server address (MCP URL)'), copyBox(url, 'MCP URL')),
      h('details', { class: 'snippet', id: 'claude-connector', open: true }, h('summary', {}, 'Claude on the web, desktop or phone: log in, no key'), claudeConnector(url)),
      can.write() ? h('div', { class: 'key-create' }, h('h3', {}, '1. Create a key for your assistant'),
        h('p', { class: 'small muted' }, (state.config && state.config.mcp_require_key ? 'An API key is required. ' : 'With a key, your assistant\'s checks count toward your account and show in your logs. ') + 'A new key can run every read-only check and acts with office permissions. Use one key per assistant or gateway, then narrow its tools, rate and lifetime with Edit access under API keys below. Revoke it any time.'),
        h('div', { class: 'field' }, h('label', { for: nameIn.id }, 'Name this key (so you know where it is used)'), h('div', { class: 'row' }, nameIn, expIn, btn))) : null,
      keySlot, snippetSlot);
  }

  // ======================================================================== MCP governance: key access + MCP call log
  // Owners and office users choose which tools each API key may call, its rate and its lifetime; owners see and
  // download every /mcp tool call (docs/mcp-security.md). Text from the server is always set as text, never HTML.
  async function mcpPolicy() {
    if (!state.mcpPolicy) state.mcpPolicy = await api('/api/mcp/policy');
    return state.mcpPolicy;
  }
  function keyAccessText(k) {
    const s = k.scope || {};
    return [s.summary || 'All read-only tools', s.rest_api === false ? 'tool calls only' : null].filter(Boolean).join(' · ');
  }
  function keyExpiryCell(k) {
    if (!k.expires_at) return 'Never';
    return k.expired ? h('span', { class: 'pill red' }, `Expired ${fmtDate(k.expires_at)}`) : fmtDate(k.expires_at);
  }
  async function editKeyAccess(k, onSaved) {
    const pol = await mcpPolicy();
    const sc = k.scopes || null;
    const custom = !!(sc && (sc.tools || sc.categories));
    const chosenTools = new Set((sc && sc.tools) || []);
    const chosenCats = new Set((sc && sc.categories) || []);
    const uid = ++fieldSeq;
    const err = h('div', { role: 'alert' });
    const modeAll = h('input', { type: 'radio', name: `ka-mode-${uid}`, value: 'all', id: `ka-all-${uid}`, checked: !custom });
    const modePick = h('input', { type: 'radio', name: `ka-mode-${uid}`, value: 'pick', id: `ka-pick-${uid}`, checked: custom });
    const picker = h('div', { class: 'scope-picker', hidden: !custom });
    const catBoxes = [];
    const toolBoxes = [];
    for (const c of pol.categories) {
      const cb = h('input', { type: 'checkbox', value: c.id, checked: chosenCats.has(c.id), id: `ka-c-${uid}-${c.id}` });
      catBoxes.push(cb);
      const items = c.tools.map((t) => {
        const tb = h('input', { type: 'checkbox', value: t.name, checked: chosenTools.has(t.name), id: `ka-t-${uid}-${t.name}` });
        toolBoxes.push(tb);
        return h('li', {}, h('label', { for: tb.id }, tb, ' ', t.title), t.read_only ? null : h('span', { class: 'pill amber' }, 'Changes data: only by name'));
      });
      picker.append(h('div', { class: 'scope-cat' },
        h('label', { for: cb.id }, cb, ' ', h('strong', {}, c.label), h('span', { class: 'small muted' }, ` (all ${plural(c.tools.length, 'tool')})`)),
        h('details', {}, h('summary', { class: 'small' }, 'Or pick single tools'), h('ul', { class: 'scope-tools' }, items))));
    }
    const toggle = () => { picker.hidden = !modePick.checked; };
    modeAll.addEventListener('change', toggle);
    modePick.addEventListener('change', toggle);
    const rest = h('input', { type: 'checkbox', id: `ka-rest-${uid}`, checked: !sc || sc.rest_api !== false });
    const rate = input('rate_limit_per_min', { type: 'number', min: '1', max: String(pol.key_rate_limit_max), inputmode: 'numeric', value: k.rate_limit_per_min ?? '', placeholder: `${pol.key_rate_limit_per_min} (default)` });
    const expiry = select('expiry', [['keep', k.expires_at ? `Keep: ${fmtDate(k.expires_at)}` : 'Keep: never expires'], ['never', 'Never expires'], ['30', '30 days from today'], ['90', '90 days from today'], ['365', '1 year from today']], 'keep');
    const save = h('button', { type: 'submit', class: 'btn' }, 'Save access');
    let dlg = null;
    const form = h('form', { onsubmit: async (e) => {
      e.preventDefault(); err.replaceChildren();
      const body = { rate_limit_per_min: rate.value === '' ? null : Number(rate.value) };
      if (modePick.checked) body.scopes = { tools: toolBoxes.filter((b) => b.checked).map((b) => b.value), categories: catBoxes.filter((b) => b.checked).map((b) => b.value), rest_api: rest.checked };
      else body.scopes = rest.checked ? null : { rest_api: false };
      if (expiry.value === 'never') body.expires_at = null; else if (expiry.value !== 'keep') body.expires_in_days = Number(expiry.value);
      busy(save, true, 'Saving…');
      try { await api(`/api/keys/${k.id}`, { method: 'PATCH', body }); toast('Key access saved'); if (dlg) dlg.close(); if (onSaved) onSaved(); }
      catch (ex) { err.append(h('div', { class: 'banner error' }, h('p', {}, ex.message))); } finally { busy(save, false); }
    } },
    h('fieldset', { class: 'scope-mode' }, h('legend', {}, 'Tools this key may call'),
      h('div', {}, h('label', { for: modeAll.id }, modeAll, ` All read-only checks (${pol.read_only_tools}), the default`)),
      h('div', {}, h('label', { for: modePick.id }, modePick, ' Only the ones I choose'))),
    picker,
    h('div', { class: 'field check' }, h('label', { for: rest.id }, rest, ' Also let this key read and change company data through the REST API (projects, subs, documents, imports)'),
      h('span', { class: 'hint' }, 'Leave this off for an AI assistant that only runs checks. Recipes that import subs (for example Workato) need it on.')),
    h('div', { class: 'form-grid' },
      field('Tool calls per minute', rate, { hint: `Up to ${pol.key_rate_limit_max}. Blank uses the default.` }),
      field('Expires', expiry)),
    err, h('div', { class: 'row' }, save));
    dlg = openDialog(`Access for “${k.name}”`, form);
  }
  function nextDay(iso) {
    const d = new Date(iso + 'T00:00:00Z');
    d.setUTCDate(d.getUTCDate() + 1);
    return d.toISOString().slice(0, 10);
  }
  const MCP_STATUS = { ok: ['green', 'OK'], error: ['red', 'Error'], denied: ['amber', 'Not allowed'], rate_limited: ['amber', 'Rate limited'] };
  function mcpLogSection() {
    const slot = h('div', { 'aria-live': 'polite' });
    const keySel = select('key_id', [['', 'All keys']], '', { id: 'mcp-log-key' });
    const statusSel = select('status', [['', 'Any result'], ...Object.entries(MCP_STATUS).map(([v, [, t]]) => [v, t])], '', { id: 'mcp-log-status' });
    const from = input('start', { type: 'date', id: 'mcp-log-from' });
    const to = input('end', { type: 'date', id: 'mcp-log-to' });
    const csvLink = h('a', { class: 'btn secondary small', href: '/api/mcp/audit/export?format=csv', download: '' }, 'Download CSV');
    const jsonLink = h('a', { class: 'btn ghost small', href: '/api/mcp/audit/export?format=json', download: '' }, 'Download JSON');
    let rows = [];
    let cursor = null;
    let keysLoaded = false;
    const params = () => {
      const q = new URLSearchParams();
      if (keySel.value) q.set('key_id', keySel.value);
      if (statusSel.value) q.set('status', statusSel.value);
      if (from.value) q.set('start', from.value);
      if (to.value) q.set('end', nextDay(to.value));
      return q;
    };
    const draw = () => slot.replaceChildren(...[rows.length ? table([
      { label: 'When', render: (r) => fmtDateTime(r.created) },
      { label: 'Key or app', render: (r) => r.key_name ? `${r.key_name} (${r.key_prefix}…)` : r.api_key_id ? `Key #${r.api_key_id}` : r.app_name ? `App: ${r.app_name}` : r.oauth_grant_id ? `App #${r.oauth_grant_id}` : 'No key' },
      { label: 'Tool', render: (r) => humanize(r.tool) },
      { label: 'Result', render: (r) => { const [c, t] = MCP_STATUS[r.status] || ['gray', r.status]; return h('span', { class: `pill ${c}` }, t); } },
      { label: 'Time', render: (r) => r.duration_ms === null || r.duration_ms === undefined ? '—' : `${r.duration_ms} ms` },
      { label: 'From', render: (r) => r.ip || '—' },
      { label: 'Arguments', render: (r) => r.args_sha256 ? h('code', { title: `SHA-256 of the arguments: ${r.args_sha256}` }, r.args_sha256.slice(0, 12) + '…') : '—' }], rows) :
      h('p', { class: 'muted' }, 'No AI assistant tool calls match.'),
    cursor ? h('button', { type: 'button', class: 'btn secondary small', onclick: () => load(true).catch((e) => toast(e.message, true)) }, 'Show older') : null].filter(Boolean));
    const load = async (more = false) => {
      const q = params();
      if (more && cursor) q.set('cursor', cursor);
      const r = await api(`/api/mcp/audit?${q}`);
      rows = more ? rows.concat(r.items) : r.items;
      cursor = r.next_cursor;
      if (!keysLoaded) {
        keysLoaded = true;
        r.keys.forEach((k) => keySel.append(h('option', { value: String(k.id) }, `${k.name} (${k.prefix}…)${k.revoked ? ', revoked' : ''}`)));
      }
      const e = params();
      e.set('format', 'csv'); csvLink.href = `/api/mcp/audit/export?${e}`;
      e.set('format', 'json'); jsonLink.href = `/api/mcp/audit/export?${e}`;
      draw();
    };
    const reload = () => load().catch((e) => slot.replaceChildren(h('p', {}, e.message)));
    [keySel, statusSel, from, to].forEach((el) => el.addEventListener('change', reload));
    reload();
    return h('section', { class: 'card', id: 'mcp-log' },
      h('div', { class: 'card-head' }, h('h2', {}, 'AI assistant call log (MCP)'), h('button', { type: 'button', class: 'btn ghost small', onclick: reload }, 'Refresh')),
      h('p', { class: 'small muted' }, 'Every tool call made through the MCP server: which key or connected app, which tool, the result, how long it took and where it came from. Arguments are never stored, because they can hold employee data; each row keeps a SHA-256 fingerprint of them instead.'),
      h('div', { class: 'form-grid' }, field('API key', keySel), field('Result', statusSel), field('From', from), field('To', to)),
      h('div', { class: 'row' }, csvLink, jsonLink), slot);
  }

  // ======================================================================== onboarding
  const STEPS = ['Your license', 'Sub list', 'First project', 'Safety programs', 'AI assistant'];
  routes.welcome = async ({ query }) => {
    if (!can.write()) return h('div', { class: 'card' }, h('h1', {}, 'Welcome'), h('p', {}, 'Your office has set things up. Use the Field tab for toolbox talks and inspections.'), h('a', { class: 'btn', href: '#/field' }, 'Open Field'));
    const settings = state.me.tenant.settings || {};
    const ob = settings.onboarding || { done: [] };
    let step = Math.min(5, Math.max(1, Number(query.get('step')) || ob.step || 1));
    const saveProgress = async (done) => {
      const set = new Set(ob.done || []); if (done) set.add(step);
      ob.done = [...set]; ob.step = step;
      try { const t = await api('/api/tenant', { method: 'PATCH', body: { settings: { onboarding: ob } } }); state.me.tenant = { ...state.me.tenant, ...t }; } catch (e) { /* non-fatal */ }
    };
    const body = h('div', {});
    const stepper = h('ol', { class: 'stepper' });
    const drawStepper = () => stepper.replaceChildren(...STEPS.map((label, k) => h('li', { class: (ob.done || []).includes(k + 1) ? 'done' : '' },
      h('button', { type: 'button', 'aria-current': step === k + 1 ? 'step' : undefined, onclick: () => { step = k + 1; draw(); } }, h('span', { class: 'num' }, k + 1), label))));
    // A step's "Next step" button appears once that step has produced something (license checked, list imported,
    // project added, programs generated), so nobody taps Next and loses a filled-in form; "Skip for now" is always there.
    let navSlot = null;
    const gate = { ready: true, open() { if (!this.ready) { this.ready = true; if (navSlot) navSlot.replaceChildren(nav(true)); } } };
    const nav = (canNext = true) => h('div', { class: 'row' },
      step > 1 ? h('button', { type: 'button', class: 'btn ghost', onclick: () => { step -= 1; draw(); } }, 'Back') : null,
      h('button', { type: 'button', class: 'btn ghost', onclick: async () => { await saveProgress(false); if (step < 5) { step += 1; draw(); } else go('dashboard'); } }, step < 5 ? 'Skip for now' : 'Finish later'),
      canNext ? h('button', { type: 'button', class: 'btn', onclick: async () => { await saveProgress(true); if (step < 5) { step += 1; draw(); } else { toast('Setup complete'); go('dashboard'); } } }, step < 5 ? 'Next step' : 'Go to dashboard') : null);

    const steps = {
      1: async () => {
        const out = h('div', { 'aria-live': 'polite' });
        const lic = input('cslb_license', { inputmode: 'numeric', value: state.me.tenant.cslb_license || '', autocomplete: 'off' });
        const btn = h('button', { type: 'submit', class: 'btn' }, 'Check my license');
        const check = async () => {
          out.replaceChildren();
          const num = lic.value.replace(/\D/g, '');
          if (!num) { out.append(h('div', { class: 'banner error' }, h('p', {}, 'Enter your CSLB license number (digits only).'))); lic.focus(); return; }
          busy(btn, true, 'Checking CSLB data…');
          try {
            if (num !== state.me.tenant.cslb_license) {
              const t = await api('/api/tenant', { method: 'PATCH', body: { cslb_license: num } });
              state.me.tenant = { ...state.me.tenant, ...t };
            }
            out.append(licenseCardView(await api(`/api/license/${encodeURIComponent(num)}`)));
            gate.open();
          } catch (ex) { out.append(ex.status === 402 ? subscribeCard(ex.message) : h('div', { class: 'banner error' }, h('p', {}, ex.message))); } finally { busy(btn, false); }
        };
        const form = h('form', { novalidate: true, onsubmit: (e) => { e.preventDefault(); check(); } },
          field('Your CSLB license number', lic, { required: true, hint: 'Find it on your pocket card or at cslb.ca.gov.' }), btn);
        if (state.me.tenant.cslb_license) check();        // entered at sign-up: show the result right away
        return [h('h2', {}, 'Step 1: Check your own license'), h('p', { class: 'muted' }, 'A lapsed bond or workers\' comp policy suspends your license automatically, and work done while suspended can mean giving back what you were paid.'), form, h('div', { class: 'step-out' }, out)];
      },
      2: async () => [h('h2', {}, 'Step 2: Upload your sub list'), h('p', { class: 'muted' }, 'We check each license, class, workers\' comp and bond. Most firms with 20+ subs find at least one problem.'), importPanel(() => gate.open())],
      3: async () => {
        const out = h('div', { 'aria-live': 'polite' });
        const form = projectForm(null, async (p) => {
          form.replaceWith(h('div', { class: 'banner info' }, h('p', {}, `Added "${p.name}".`), h('a', { class: 'btn small secondary', href: `#/project/${p.id}` }, 'Open project')));
          gate.open();
          try { const dl = await api(`/api/projects/${p.id}/deadlines`); out.replaceChildren(h('h3', {}, 'Lien and safety calendar'), alertList(dl.items, { empty: 'Add furnishing dates to see deadlines.', inProject: true }), dl.notes.length ? h('ul', { class: 'bullets small muted' }, dl.notes.map((n) => h('li', {}, n))) : null); } catch (e) { out.replaceChildren(h('p', {}, e.message)); }
        });
        return [h('h2', {}, 'Step 3: Add an active project'), h('p', { class: 'muted' }, 'We build the lien calendar: 20-day preliminary notice, lien recording and foreclosure deadlines.'), form, out];
      },
      4: async () => {
        let tools = [];
        try { tools = (await getTools()).tools; } catch (e) { /* offline */ }
        const pick = tools.filter((t) => t.module === 'safety_programs' && /program|iipp|plan|questionnaire|generate/.test(t.name))
          .sort((a, b) => (/generate|program/.test(b.name) ? 1 : 0) - (/generate|program/.test(a.name) ? 1 : 0));
        if (!pick.length) {
          const lookup = tools.find((t) => t.module === 'safety_programs');
          return [h('h2', {}, 'Step 4: Safety programs'), h('p', {}, 'The Cal/OSHA program generator is not installed on this server yet.'),
            lookup ? h('a', { class: 'btn secondary', href: `#/tools/${lookup.name}` }, `Open ${lookup.title}`) : null];
        }
        return [h('h2', {}, 'Step 4: Answer the safety questionnaire'), h('p', { class: 'muted' }, 'About 10 minutes. You get draft programs (IIPP, Code of Safe Practices, heat plan and more) to review and sign. Save them to Documents.'),
          pick.length > 1 ? h('p', { class: 'small' }, 'Also available: ', pick.slice(1).map((t, k) => [k ? ', ' : '', h('a', { href: `#/tools/${t.name}` }, t.title)])) : null,
          toolPanel(pick[0], { onResult: () => gate.open() })];
      },
      5: async () => [h('h2', {}, 'Step 5: Connect your AI assistant'), agentConnect()],
    };
    const draw = async () => {
      drawStepper();
      body.replaceChildren(h('p', { class: 'loading' }, 'Loading…'));
      gate.ready = step === 5 || (ob.done || []).includes(step);
      navSlot = null;
      const content = await steps[step]();
      navSlot = h('div', {}, nav(gate.ready));
      body.replaceChildren(h('section', { class: 'card' }, content, h('hr'), navSlot));
      history.replaceState(null, '', `#/welcome?step=${step}`);
    };
    await draw();
    return h('div', {}, h('div', { class: 'page-head' }, h('div', {}, h('h1', {}, `Welcome, ${state.me.tenant.company_name}`), h('p', {}, 'Five steps, about 15 minutes. You can skip any step and come back.'))), stepper, body);
  };

  // ======================================================================== integrations
  // The catalog (GET /api/integrations/catalog) says what each product is and how it fits; the live connectors
  // (GET /api/integrations) say what is installed on this server and connected for this company. Connect panels are
  // drawn generically from each connector's config_schema, so new connectors need no UI code here.
  const INT_KIND_PILL = { builtin: 'green', ai_assistant: 'blue', import_export: 'violet', reference: 'gray' };
  const INT_ORDER = { builtin: 0, ai_assistant: 1, import_export: 2, reference: 3 };
  const GOV_PILL = { yes: 'green', partial: 'amber', no: 'red', unconfirmed: 'gray', not_applicable: 'gray' };
  const CONN_TEXT = { connected: ['green', 'Connected'], pending: ['amber', 'Waiting for sign-in'], error: ['red', 'Needs attention'], disconnected: ['gray', 'Not connected'] };
  const PANEL_TITLE = { connector: 'Connect', webhooks: 'Set up alerts', calendar_feed: 'Get your calendar link', schedule_import: 'Upload a schedule' };
  const HOOK_HELP = {
    slack: 'In Slack, create an incoming webhook for the channel (your Slack app\'s settings → Incoming Webhooks; your workspace admin may need to approve the app) and paste its https://hooks.slack.com/services/… link here.',
    teams: 'In Teams, open the channel\'s ••• menu → Workflows → “Send webhook alerts to a channel” (or build a workflow with the “When a Teams webhook request is received” trigger) and paste the link it creates. Add a co-owner to the workflow so it keeps working if its creator leaves.',
    generic: 'Enter your HTTPS endpoint. TradeComply signs every message the Standard Webhooks way; you will see the signing secret once, right after you add it.',
  };
  const HOOK_PLACEHOLDER = { slack: 'https://hooks.slack.com/services/…', teams: 'https://…', generic: 'https://your-system.example/webhooks/tradecomply' };
  const intState = { catalog: null, providers: null, providersError: null, loadErrors: [], events: null, toolRe: null, schedule: undefined };

  const fill = (el, ...kids) => { el.replaceChildren(); return append(el, kids); };   // replaceChildren() would print null
  const msgBox = (kind, ...text) => h('div', { class: `banner ${kind}` }, h('p', {}, ...text));
  const ext = (url, text) => h('a', { href: url, target: '_blank', rel: 'noopener noreferrer' }, text || url);
  const more = (summary, ...content) => h('details', { class: 'more' }, h('summary', {}, summary), ...content);
  function vendorStatusPill(label) {
    const t = String(label || '');
    const cls = /deprecat/i.test(t) ? 'red' : /preview|beta|early access|proof|rolling|unconfirmed|mixed|maintenance|stale|community/i.test(t) ? 'amber' : /generally available/i.test(t) ? 'green' : 'gray';
    return h('span', { class: `pill ${cls}` }, h('span', { class: 'visually-hidden' }, 'Vendor status: '), t);
  }
  function withToolLinks(text) {
    const re = intState.toolRe;
    if (!re) return text;
    const out = [];
    let last = 0;
    let m;
    re.lastIndex = 0;
    while ((m = re.exec(text))) {
      out.push(text.slice(last, m.index), h('a', { class: 'tool-ref', href: `#/tools/${m[1]}`, title: `TradeComply check: ${m[1]}` }, humanize(m[1])));
      last = m.index + m[1].length;
    }
    out.push(text.slice(last));
    return out;
  }
  function providerFor(e) {
    const list = intState.providers || [];
    const ids = (e.builtin && e.builtin.providers) || [];
    return list.find((p) => p.catalog_id === e.id) || list.find((p) => ids.includes(p.provider)) || null;
  }
  async function loadEvents() {
    if (!intState.events) intState.events = (await api('/api/integrations/events')).events;
    return intState.events;
  }

  // ---------- generic connector panel (any provider from GET /api/integrations)
  function configForm(schema, config, saved) {
    const form = h('form', { class: 'form-grid', novalidate: true, onsubmit: (ev) => ev.preventDefault() });
    for (const f of schema) {
      const id = `cfg-${++fieldSeq}`;
      const val = f.secret ? '' : (config[f.name] ?? f.default ?? '');
      const isSaved = f.secret && saved.includes(f.name);
      const hint = [f.help, isSaved ? 'A value is saved. It is never shown; leave this blank to keep it.' : ''].filter(Boolean).join(' ');
      if (f.type === 'boolean') {
        form.append(h('div', { class: 'field wide' }, h('label', { class: 'switch', for: id }, h('input', { type: 'checkbox', name: f.name, id, checked: !!val }), f.label), hint ? h('span', { class: 'hint' }, hint) : null));
        continue;
      }
      let el;
      const attrs = { name: f.name, id, maxlength: f.max_length || null, placeholder: isSaved ? 'Saved' : (f.placeholder || null), autocomplete: 'off', spellcheck: 'false' };
      if (f.type === 'select') el = select(f.name, [['', 'Choose…'], ...(f.options || []).map((o) => [o.value, o.label])], val, { id });
      else if (f.type === 'textarea') el = h('textarea', { ...attrs, value: val });
      else {
        // A YYYY-MM-DD text field gets the browser's date picker (its value is the same YYYY-MM-DD text).
        const isDate = f.type === 'text' && f.pattern === '\\d{4}-\\d{2}-\\d{2}';
        el = h('input', { ...attrs, value: val, type: f.secret || f.type === 'password' ? 'password' : isDate ? 'date' : ({ url: 'url', email: 'email', number: 'number' }[f.type] || 'text') });
      }
      // Long choices ("A week before and the day before") would be cut off in half a narrow card.
      const wide = f.type === 'textarea' || (f.type === 'select' && (f.options || []).some((o) => String(o.label).length > 22));
      form.append(field(f.label, el, { hint: hint || undefined, required: f.required && !isSaved, wide }));
    }
    return form;
  }
  function configValues(form, schema) {
    const out = {};
    for (const f of schema) {
      const el = form.elements[f.name];
      if (!el) continue;
      if (f.type === 'boolean') { out[f.name] = el.checked; continue; }
      const v = el.value.trim();
      if (f.secret) { if (v) out[f.name] = v; continue; }       // blank keeps the saved secret
      out[f.name] = v === '' ? null : (f.type === 'number' ? Number(v) : v);
    }
    return out;
  }
  // ---------- after connecting: the one or two things each connector is for
  const RISK_LABEL = { do_not_use: ['red', 'Do not use'], high: ['red', 'High risk'], medium: ['amber', 'Medium risk'], low: ['green', 'Low risk'] };
  function procoreLast(s) {
    if (!s) return h('p', { class: 'small muted' }, 'Nothing imported yet.');
    const pr = s.projects || {}, v = s.vendors || {}, counts = s.counts_by_risk || {};
    return h('div', { class: 'small' },
      h('p', {}, `Last import ${fmtDateTime(s.at)}${s.company_name ? ` from ${s.company_name}` : ''}: ${plural(pr.created || 0, 'new project')} and ${plural(pr.updated || 0, 'project')} updated; ${plural(v.created || 0, 'new sub')} and ${plural(v.updated || 0, 'sub')} updated.`),
      Object.keys(RISK_LABEL).some((k) => counts[k]) ? h('p', { class: 'row' }, Object.entries(RISK_LABEL).filter(([k]) => counts[k]).map(([k, [cls, label]]) => h('span', { class: `pill ${cls}` }, `${label}: ${counts[k]}`))) : null,
      s.report_document_id ? h('p', {}, h('a', { href: `#/document/${encodeURIComponent(s.report_document_id)}` }, 'Open the license report')) : null);
  }
  const NEXT_STEPS = {
    procore: () => {
      const box = h('div', { class: 'stack' });
      const out = h('div', { 'aria-live': 'polite' });
      const draw = async () => {
        const [co, rep] = await Promise.all([api('/api/integrations/procore/companies'), api('/api/integrations/procore/report')]);
        const sel = select('procore_company', [['', 'Choose your Procore company…'], ...co.companies.map((c) => [String(c.id), c.name])], co.company_id ? String(co.company_id) : '');
        sel.addEventListener('change', async () => {
          if (!sel.value) return;
          try { await api('/api/integrations/procore/company', { method: 'POST', body: { company_id: Number(sel.value) } }); toast('Procore company saved'); } catch (err) { fill(out, msgBox('error', err.message)); }
        });
        const go = h('button', { type: 'button', class: 'btn', onclick: async () => {
          fill(out);
          if (!sel.value) { out.append(msgBox('error', 'Choose your Procore company first.')); sel.focus(); return; }
          busy(go, true, 'Importing… this can take a minute');
          try { await api('/api/integrations/procore/sync', { method: 'POST', body: { projects: true, vendors: true } }); toast('Procore import finished'); await draw(); } catch (err) { fill(out, msgBox('error', err.message)); } finally { busy(go, false); }
        } }, 'Import projects and subs now');
        fill(box, h('h4', {}, 'Next: import from Procore'),
          h('p', { class: 'small muted' }, 'TradeComply copies your Procore projects (California only, unless you change that in Settings) and your Procore vendor directory, checks each vendor\'s CSLB license and saves a license report. It never changes anything in Procore. Run it again whenever your vendor list changes.'),
          field('Procore company', sel), h('div', { class: 'row' }, go), procoreLast(rep.last_sync));
      };
      draw().catch((err) => fill(box, msgBox('error', err.message)));
      return h('div', {}, box, out);
    },
    twilio_sms: () => {
      const R = '/api/integrations/twilio_sms/recipients';
      const box = h('div', { class: 'stack' });
      const out = h('div', { 'aria-live': 'polite' });
      const draw = async () => {
        const d = await api(R);
        const rows = d.recipients || [];
        const list = rows.length ? table([
          { label: 'Name', bare: true, render: (r) => h('div', {}, h('strong', {}, r.name), h('div', { class: 'small muted' }, r.phone)) },
          { label: 'Status', render: (r) => h('div', {}, r.status === 'active' ? h('span', { class: 'pill green' }, 'Gets texts') : h('span', { class: 'pill gray' }, 'Opted out (replied STOP)'), r.last_error ? h('div', { class: 'small muted' }, r.last_error) : null) },
          { label: 'Actions', bare: true, render: (r) => {
            const test = r.status === 'active' ? h('button', { type: 'button', class: 'btn secondary small', onclick: async () => {
              busy(test, true, 'Sending…');
              try { const t = await api(`${R}/${encodeURIComponent(r.id)}/test`, { method: 'POST' }); fill(out, msgBox(t.ok ? 'info' : 'error', t.message)); } catch (err) { fill(out, msgBox('error', err.message)); } finally { busy(test, false); }
            } }, 'Send test text') : null;
            const del = h('button', { type: 'button', class: 'btn ghost small', onclick: async () => {
              if (!confirm(`Stop texting ${r.name}?`)) return;
              try { await api(`${R}/${encodeURIComponent(r.id)}`, { method: 'DELETE' }); await draw(); toast('Phone number removed'); } catch (err) { fill(out, msgBox('error', err.message)); }
            } }, 'Remove');
            return h('div', { class: 'row' }, test, del);
          } }], rows) : h('p', { class: 'small muted' }, 'No phone numbers yet.');
        const nameIn = input('name', { maxlength: '100', autocomplete: 'off' });
        const phoneIn = input('phone', { type: 'tel', maxlength: '40', autocomplete: 'off', placeholder: '(415) 555-0100' });
        const consent = h('input', { type: 'checkbox', name: 'consent' });
        const addBtn = h('button', { type: 'submit', class: 'btn' }, 'Add phone number');
        const form = h('form', { novalidate: true, onsubmit: async (ev) => {
          ev.preventDefault();
          fill(out);
          if (!nameIn.value.trim() || !phoneIn.value.trim()) { out.append(msgBox('error', 'Enter the person\'s name and mobile number.')); return; }
          if (!consent.checked) { out.append(msgBox('error', 'Check the box to confirm this person agreed to get alert texts.')); consent.focus(); return; }
          busy(addBtn, true, 'Adding…');
          try {
            const r = await api(R, { method: 'POST', body: { name: nameIn.value.trim(), phone: phoneIn.value.trim(), consent: true, send_confirmation: true } });
            await draw();
            fill(out, msgBox('info', `Added ${r.recipient.name}.`, r.confirmation ? ` ${r.confirmation.message}` : ''));
          } catch (err) { fill(out, msgBox('error', err.message)); } finally { busy(addBtn, false); }
        } },
        h('div', { class: 'form-grid' }, field('Name', nameIn), field('Mobile number', phoneIn, { hint: 'US or Canadian mobile number.' })),
        h('div', { class: 'field check' }, h('label', {}, consent, h('span', {}, d.consent_text))),
        addBtn);
        fill(box, h('h4', {}, 'Who gets the texts'), list, h('h4', {}, 'Add a phone number'), form,
          d.a2p_10dlc_note ? h('p', { class: 'small muted' }, d.a2p_10dlc_note) : null);
      };
      draw().catch((err) => fill(box, msgBox('error', err.message)));
      return h('div', {}, box, out);
    },
  };

  // A link the server returns once (the calendar feed's feed_url / url): copy it now, it is never shown again.
  function onceLink(url, webcal) {
    return h('div', { class: 'secret' },
      h('p', {}, h('strong', {}, 'Your calendar link. Copy it now: it is shown only this once.'), ' Anyone with the link can see your deadlines and project names, so keep it private.'),
      copyBox(url, 'calendar link'),
      webcal ? h('p', { class: 'small' }, h('a', { href: webcal }, 'Open it in this computer\'s calendar app'), ' (if nothing happens, paste the link into your calendar instead).') : null);
  }
  // settingsOnly: the calendar and schedule panels, where "connecting" means nothing to the user. Their settings are
  // saved without a connection check, and turning the feature off lives in the panel above.
  function providerPanel(p, lead, { settingsOnly = false, onChange = null } = {}) {
    const wrap = h('div', { class: 'stack' });
    const out = h('div', { 'aria-live': 'polite' });
    let detail = null;
    const refresh = async () => {
      if (can.write()) detail = await api(`/api/integrations/${encodeURIComponent(p.provider)}`);
      const fresh = detail || p;
      p.connection = fresh.connection;
      draw();
    };
    const act = async (btn, label, fn) => {
      busy(btn, true, label);
      fill(out);
      try { await fn(); } catch (err) { fill(out, msgBox('error', err.message)); } finally { busy(btn, false); }
    };
    const draw = () => {
      const view = detail || p;
      const c = view.connection || {};
      const [cls, text] = CONN_TEXT[c.status] || CONN_TEXT.disconnected;
      const parts = [lead, settingsOnly || !p.operator_configured ? null : h('p', { class: 'row' }, h('span', { class: `pill ${cls}` }, text),
        c.external_account ? h('span', { class: 'small' }, `Account: ${c.external_account}`) : null,
        c.updated ? h('span', { class: 'small muted' }, `Updated ${fmtDateTime(c.updated)}`) : null)];
      if (c.last_error && c.status !== 'connected' && !settingsOnly) parts.push(msgBox('error', c.last_error));
      // Developer-level caveats go last, after the action, so the office sees what to click first.
      const unconfirmed = p.unconfirmed && p.unconfirmed.length ? [h('p', { class: 'small' }, `Not yet confirmed in the official documentation (${p.unconfirmed.length}):`), h('ul', { class: 'bullets small' }, p.unconfirmed.map((u) => h('li', {}, u)))] : null;
      if (!p.operator_configured) {
        // Nothing the customer fills in here can work until the server has the vendor app's keys, so no form.
        const local = state.me && state.me.tenant && state.me.tenant.plan === 'local';
        parts.push(h('div', { class: 'banner warn' }, h('p', {}, h('strong', {}, `${p.display_name} isn't turned on yet. `),
          local ? `This copy of TradeComply needs ${p.display_name}'s app keys before it can connect. Your IT person can add them (see the details below); then come back here to connect.`
            : [`This TradeComply server needs ${p.display_name}'s app keys before it can connect. `, supportText('To have it turned on'), ' Then come back here to connect.'])),
          (p.missing_env && p.missing_env.length) || unconfirmed ? more('Details for whoever runs the server',
            p.missing_env && p.missing_env.length ? h('p', { class: 'small' }, 'Missing server settings: ', p.missing_env.map((n, k) => [k ? ', ' : '', h('code', {}, n)])) : null, unconfirmed) : null,
          out);
        fill(wrap, ...parts);
        return;
      }
      const techNotes = unconfirmed ? more(`For your IT person: details not yet confirmed (${p.unconfirmed.length})`, unconfirmed) : null;
      if (!can.write()) {
        parts.push(h('p', { class: 'small muted' }, `Ask the owner or office staff to connect ${p.display_name}.`));
        fill(wrap, ...parts);
        return;
      }
      const schema = view.config_schema || [];
      const form = configForm(schema, view.config || {}, view.secrets_saved || []);
      const off = !p.operator_configured;
      const buttons = h('div', { class: 'row' });
      if (settingsOnly) {
        if (!schema.length) { fill(wrap, ...parts); return; }
        const save = h('button', { type: 'button', class: 'btn secondary', onclick: () => act(save, 'Saving…', async () => {
          detail = await api(`/api/integrations/${encodeURIComponent(p.provider)}/config`, { method: 'PUT', body: { config: configValues(form, schema) } }); draw(); toast('Settings saved');
          if (onChange) onChange();
        }) }, 'Save settings');
        buttons.append(save);
        fill(wrap, ...parts, form, buttons, out, techNotes);
        return;
      }
      if (p.auth === 'oauth2') {
        if (schema.length) {
          const save = h('button', { type: 'button', class: 'btn secondary', onclick: () => act(save, 'Saving…', async () => {
            detail = await api(`/api/integrations/${encodeURIComponent(p.provider)}/config`, { method: 'PUT', body: { config: configValues(form, schema) } }); draw(); toast('Settings saved');
          }) }, 'Save settings');
          buttons.append(save);
        }
        const signIn = h('button', { type: 'button', class: 'btn', disabled: off, onclick: () => act(signIn, 'Opening sign-in…', async () => {
          if (schema.length) await api(`/api/integrations/${encodeURIComponent(p.provider)}/config`, { method: 'PUT', body: { config: configValues(form, schema) } });
          const r = await api(`/api/integrations/${encodeURIComponent(p.provider)}/oauth/start`, { method: 'POST' });
          location.href = r.authorize_url;
        }) }, c.status === 'connected' ? `Sign in to ${p.display_name} again` : `Sign in with ${p.display_name}`);
        buttons.append(signIn);
      } else {
        const connect = h('button', { type: 'button', class: 'btn', disabled: off, onclick: () => act(connect, 'Connecting…', async () => {
          const r = await api(`/api/integrations/${encodeURIComponent(p.provider)}/connect`, { method: 'POST', body: { config: configValues(form, schema) } });
          await refresh();
          const t = r.test || {};
          fill(out, msgBox(r.status === 'connected' ? 'info' : 'error', r.status === 'connected' ? `Connected${r.external_account ? ` to ${r.external_account}` : ''}.` : (t.message || r.last_error || 'The connection check failed.')),
            r.feed_url ? onceLink(r.feed_url, r.webcal_url) : null);       // the calendar feed's first link, shown once
          if (onChange) onChange();
        }) }, c.status === 'connected' ? 'Save changes' : schema.length ? 'Save and connect' : 'Connect');
        buttons.append(connect);
      }
      if (c.status && c.status !== 'disconnected') {
        const test = h('button', { type: 'button', class: 'btn secondary', disabled: off, onclick: () => act(test, 'Checking…', async () => {
          const r = await api(`/api/integrations/${encodeURIComponent(p.provider)}/test`, { method: 'POST' });
          await refresh();
          fill(out, msgBox(r.ok ? 'info' : 'error', r.message || (r.ok ? 'The connection works.' : 'The connection check failed.')));
        }) }, 'Test connection');
        const disc = h('button', { type: 'button', class: 'btn danger', onclick: () => {
          if (!confirm(`Disconnect ${p.display_name}? TradeComply forgets the saved sign-in; your settings stay.`)) return;
          act(disc, 'Disconnecting…', async () => { await api(`/api/integrations/${encodeURIComponent(p.provider)}/disconnect`, { method: 'POST' }); await refresh(); toast(`${p.display_name} disconnected`); if (onChange) onChange(); });
        } }, 'Disconnect');
        buttons.append(test, disc);
      }
      // What to do once connected: connector-specific actions, built once and kept across redraws.
      const live = ['connected', 'error'].includes(c.status);
      if (live && !extra && NEXT_STEPS[p.provider]) extra = NEXT_STEPS[p.provider]();
      if (!live) extra = null;
      let settings = form;
      if (live) {
        settings = more('Settings', form);
        if (c.status === 'error') settings.open = true;      // the fix (a new token, a new link) is usually in here
      }
      parts.push(schema.length ? settings : null, buttons, out, extra, techNotes);
      fill(wrap, ...parts);
    };
    let extra = null;
    draw();
    if (can.write()) refresh().catch((err) => fill(out, msgBox('error', err.message)));
    return wrap;
  }

  // ---------- panels for the built-in features that aren't connectors
  async function drawWebhooks(e, body, onChange) {
    const kind = e.builtin.webhook_kind;
    const lead = h('p', { class: 'small' }, e.builtin.what_it_does);
    if (!can.write()) { fill(body, lead, h('p', { class: 'small muted' }, 'Ask the owner or office staff to set up alerts.')); return; }
    const list = h('div', {});
    const out = h('div', { 'aria-live': 'polite' });
    const events = await loadEvents();
    const showSecret = (r) => fill(out, h('div', { class: 'secret' }, h('p', {}, h('strong', {}, 'Signing secret. Copy it now: it will not be shown again.'), ' ', r.message || ''), copyBox(r.signing_secret, 'signing secret')));
    const drawList = async () => {
      const hooks = await hooksOf(kind, true);
      if (onChange) onChange();
      fill(list, hooks.length ? table([
        { label: 'Name', bare: true, render: (w) => h('div', {}, h('strong', {}, w.name), h('div', { class: 'small muted' }, w.url_hint)) },
        { label: 'Sends', render: (w) => (w.events.includes('*') ? 'All alerts' : w.events.map(humanize).join(', ')) },
        { label: 'Status', render: (w) => h('div', {}, h('span', { class: `pill ${w.enabled ? 'green' : 'gray'}` }, w.enabled ? 'On' : 'Off'), w.last_status ? h('div', { class: 'small muted' }, `${w.last_status}${w.last_sent ? ' · ' + fmtDateTime(w.last_sent) : ''}`) : null) },
        { label: 'Actions', bare: true, render: (w) => {
          const testBtn = h('button', { type: 'button', class: 'btn secondary small', onclick: async () => {
            busy(testBtn, true, 'Sending…');
            try { const r = await api(`/api/integrations/webhooks/${w.id}/test`, { method: 'POST' }); fill(out, msgBox(r.ok ? 'info' : 'error', r.ok ? `Test sent: ${r.message || 'delivered'}.` : (r.message || 'The test was not accepted.'), r.disabled ? ' The receiver says this webhook is gone, so it was turned off.' : '')); await drawList(); } catch (err) { fill(out, msgBox('error', err.message)); } finally { busy(testBtn, false); }
          } }, 'Send test');
          const toggle = h('button', { type: 'button', class: 'btn ghost small', onclick: async () => { try { await api(`/api/integrations/webhooks/${w.id}`, { method: 'PATCH', body: { enabled: !w.enabled } }); await drawList(); } catch (err) { fill(out, msgBox('error', err.message)); } } }, w.enabled ? 'Turn off' : 'Turn on');
          const rotate = kind === 'generic' ? h('button', { type: 'button', class: 'btn ghost small', onclick: async () => { if (!confirm('Make a new signing secret? The old one stops working right away.')) return; try { showSecret(await api(`/api/integrations/webhooks/${w.id}/rotate-secret`, { method: 'POST' })); } catch (err) { fill(out, msgBox('error', err.message)); } } }, 'New secret') : null;
          const del = h('button', { type: 'button', class: 'btn ghost small', onclick: async () => { if (!confirm(`Delete “${w.name}”?`)) return; try { await api(`/api/integrations/webhooks/${w.id}`, { method: 'DELETE' }); await drawList(); toast('Webhook deleted'); } catch (err) { fill(out, msgBox('error', err.message)); } } }, 'Delete');
          return h('div', { class: 'row' }, testBtn, toggle, rotate, del);
        } }], hooks) : h('p', { class: 'small muted' }, 'None set up yet.'));
    };
    const nameIn = input('name', { value: { slack: 'Slack alerts', teams: 'Teams alerts', generic: 'Our system' }[kind], maxlength: '100' });
    const urlIn = input('url', { type: 'url', placeholder: HOOK_PLACEHOLDER[kind], autocomplete: 'off', spellcheck: 'false', maxlength: '2000' });
    const allBox = h('input', { type: 'checkbox', name: 'ev_all', checked: true });
    const evBoxes = events.map((ev) => h('label', { title: ev.description }, h('input', { type: 'checkbox', name: 'ev', value: ev.name }), humanize(ev.name)));
    const add = h('button', { type: 'submit', class: 'btn' }, 'Add');
    const form = h('form', { novalidate: true, onsubmit: async (ev) => {
      ev.preventDefault();
      fill(out);
      const picked = evBoxes.map((l) => l.querySelector('input')).filter((x) => x.checked).map((x) => x.value);
      if (!urlIn.value.trim()) { out.append(msgBox('error', 'Paste the webhook link.')); urlIn.focus(); return; }
      busy(add, true, 'Adding…');
      try {
        const r = await api('/api/integrations/webhooks', { method: 'POST', body: { name: nameIn.value.trim() || 'Alerts', url: urlIn.value.trim(), kind, events: allBox.checked || !picked.length ? ['*'] : picked } });
        urlIn.value = '';
        if (r.signing_secret) showSecret(r); else fill(out, msgBox('info', 'Added. Send a test to check it.'));
        await drawList();
      } catch (err) { fill(out, msgBox('error', err.message)); } finally { busy(add, false); }
    } },
    h('p', { class: 'small muted' }, HOOK_HELP[kind]),
    h('div', { class: 'form-grid' }, field('Name', nameIn, { hint: 'So you know which channel or system it is.' }), field('Webhook link', urlIn, { required: true, hint: 'Stored encrypted; afterwards only the start of the address is shown.' })),
    h('fieldset', {}, h('legend', {}, 'Which alerts'), h('div', { class: 'checks' }, h('label', {}, allBox, 'All alerts'), evBoxes),
      // Only these three are raised anywhere today (alerts.py deadline scan, procore.py import, docusign.py); update this line when more are.
      h('p', { class: 'hint' }, 'Today TradeComply sends deadline reminders, license problems found by a Procore import, and Docusign contract updates. The other types are listed so you can choose them now; nothing sends them yet.')),
    add);
    fill(body, lead, list, h('h4', {}, 'Add one'), form, out);
    await drawList();
  }
  // Private calendar links (/api/integrations/calendar_feed/links). Only a hash of each link is stored, so its address
  // is shown once, right after it is made. One link per person or device; each can be turned off on its own.
  const CAL_LINKS = '/api/integrations/calendar_feed/links';
  function calendarLinks({ onChange } = {}) {
    const box = h('div', {});
    const out = h('div', { 'aria-live': 'polite' });
    const changed = () => { if (onChange) onChange(); };
    const run = async (btn, label, fn) => { busy(btn, true, label); try { await fn(); } catch (err) { fill(out, msgBox('error', err.message)); } finally { busy(btn, false); } };
    const draw = async () => {
      const d = await api(CAL_LINKS);
      const links = d.links || [];
      const make = h('button', { type: 'button', class: 'btn', onclick: () => run(make, 'Making the link…', async () => {
        const r = await api(`${CAL_LINKS}/rotate`, { method: 'POST' });
        await draw(); fill(out, onceLink(r.url, r.webcal_url)); changed();
      }) }, 'Make my calendar link');
      const add = h('button', { type: 'button', class: 'btn secondary', onclick: () => run(add, 'Making the link…', async () => {
        const r = await api(CAL_LINKS, { method: 'POST' });
        await draw(); fill(out, onceLink(r.url, r.webcal_url)); changed();
      }) }, 'Add a link for another person');
      const replace = h('button', { type: 'button', class: 'btn ghost small', onclick: () => {
        if (!confirm('Replace every calendar link with one new link? All the old links stop working, so everyone who subscribed has to add the new one.')) return;
        run(replace, 'Replacing…', async () => { const r = await api(`${CAL_LINKS}/rotate`, { method: 'POST' }); await draw(); fill(out, onceLink(r.url, r.webcal_url)); changed(); });
      } }, 'Replace all links');
      const rows = links.map((l, k) => {
        const off = h('button', { type: 'button', class: 'btn ghost small', 'aria-label': `Turn off link ${k + 1}, made ${fmtDateTime(l.created)}`, onclick: () => {
          if (!confirm('Turn off this calendar link? Calendars subscribed with it stop updating.')) return;
          run(off, 'Turning off…', async () => { await api(`${CAL_LINKS}/${encodeURIComponent(l.id)}`, { method: 'DELETE' }); fill(out, msgBox('info', 'That link is off.')); await draw(); changed(); });
        } }, 'Turn off');
        return h('li', { class: 'row' }, h('span', {}, `Link ${k + 1}, made ${fmtDateTime(l.created)}`), off);
      });
      fill(box, links.length
        ? [h('p', { class: 'row' }, h('span', { class: 'pill green' }, 'On'), h('span', {}, `${plural(links.length, 'private link')} in use. For your safety a link is shown only once, when it is made: if you lost it, add a new one and turn the old one off.`)),
          h('ul', { class: 'clean stack small' }, rows), h('div', { class: 'row' }, add, replace)]
        : [h('p', { class: 'row' }, h('span', { class: 'pill gray' }, 'No link yet'), h('span', {}, 'Make a link, then add it to your calendar app.')), h('div', { class: 'row' }, make)]);
    };
    const el = h('div', { class: 'cal-links' }, box, out,
      h('p', { class: 'small muted' }, 'In Google Calendar, Outlook or Apple Calendar, add a calendar from a URL (Outlook calls it subscribing from the web) and paste the link. Each calendar app decides how often it checks for changes, often every few hours and sometimes longer.'),
      h('p', { class: 'small' }, h('a', { href: '/api/integrations/calendar_feed/calendar.ics', download: 'tradecomply.ics' }, 'Download today\'s deadlines once (.ics file)'), ' (a one-time copy that won\'t update).'));
    return { el, draw };
  }
  async function drawCalendar(e, body, onChange) {
    const lead = h('p', { class: 'small' }, e.builtin.what_it_does);
    if (!can.write()) { fill(body, lead, h('p', { class: 'small muted' }, 'Ask the owner or office staff for a calendar link.')); return; }
    const links = calendarLinks({ onChange });
    await links.draw();
    fill(body, lead, links.el);
  }
  async function scheduleEndpoint() {
    if (intState.schedule !== undefined) return intState.schedule;
    intState.schedule = null;
    try {
      const spec = await api('/api/openapi.json');
      for (const [path, ops] of Object.entries(spec.paths || {})) {
        if (ops.post && /schedule/i.test(path) && /(import|upload)/i.test(path)) {
          const op = ops.post;
          const props = (((op.requestBody || {}).content || {})['multipart/form-data'] || {}).schema || {};
          intState.schedule = { path, op, fileField: 'file', needsProject: /\{project_id\}/.test(path) || (op.parameters || []).some((x) => x.name === 'project_id'), propsRef: props.$ref || null };
          break;
        }
      }
    } catch (err) { intState.schedule = undefined; throw err; }
    return intState.schedule;
  }
  async function drawSchedule(e, body) {
    const lead = h('p', { class: 'small' }, e.builtin.what_it_does);
    const ep = await scheduleEndpoint();
    if (!ep) { fill(body, lead, msgBox('info', 'Schedule import is being added to TradeComply and isn\'t on this server yet. Until then, enter your completion and key dates on each project page so the lien calendar stays right.')); return; }
    if (!can.write()) { fill(body, lead, h('p', { class: 'small muted' }, 'Ask the office to upload the schedule.')); return; }
    const out = h('div', { 'aria-live': 'polite' });
    const fileIn = h('input', { type: 'file', name: 'file', accept: e.builtin.accept || '.xer,.xml' });
    let projSel = null;
    if (ep.needsProject) {
      const projects = await api('/api/projects').catch(() => []);
      projSel = select('project_id', [['', 'Choose a project…'], ...projects.map((p) => [String(p.id), p.name])], '');
    }
    const send = h('button', { type: 'submit', class: 'btn' }, 'Upload schedule');
    const form = h('form', { novalidate: true, onsubmit: async (ev) => {
      ev.preventDefault();
      fill(out);
      const file = fileIn.files && fileIn.files[0];
      if (!file) { out.append(msgBox('error', 'Choose the schedule file you exported.')); return; }
      if (projSel && !projSel.value) { out.append(msgBox('error', 'Choose the project this schedule is for.')); return; }
      let path = ep.path;
      const fd = new FormData();
      fd.append(ep.fileField, file);
      if (projSel) {
        if (/\{project_id\}/.test(path)) path = path.replace('{project_id}', encodeURIComponent(projSel.value));
        else path += `?project_id=${encodeURIComponent(projSel.value)}`;
      }
      busy(send, true, 'Uploading…');
      try {
        const r = await api(path, { method: 'POST', form: fd });
        fill(out, msgBox('info', (r && r.message) || 'Schedule uploaded.'), more('What the server read', h('pre', { class: 'mono' }, JSON.stringify(r, null, 2).slice(0, 20000))));
      } catch (err) { fill(out, msgBox('error', err.message)); } finally { busy(send, false); }
    } }, field('Schedule file', fileIn, { hint: `Microsoft Project: File → Save As → XML Format (*.xml). Primavera P6: export as XER or P6 XML.` }), projSel ? field('Project', projSel) : null, send);
    fill(body, lead, form, out);
  }
  async function drawConnect(e, body, onChange) {
    const panel = (e.builtin && e.builtin.panel) || 'connector';
    const p = providerFor(e);
    const lead = h('p', { class: 'small' }, (e.builtin && e.builtin.what_it_does) || '');
    if (panel === 'webhooks') return drawWebhooks(e, body, onChange);
    if (panel === 'calendar_feed') await drawCalendar(e, body, onChange);
    else if (panel === 'schedule_import') await drawSchedule(e, body);
    if (panel !== 'connector') {
      if (p && can.write()) body.append(more(panel === 'calendar_feed' ? 'Calendar settings: reminders, filing dates, policy renewals' : 'Import settings', providerPanel(p, null, { settingsOnly: true, onChange })));
      return;
    }
    if (intState.providersError) { fill(body, lead, msgBox('warn', `Can't check this connection right now: ${intState.providersError}`)); return; }
    if (!p) { fill(body, lead, msgBox('info', `${e.name.split(' (')[0]} can't be set up from this page yet.`, (e.mcp.servers || []).length ? ' You can still use it with your AI assistant: see below.' : '')); return; }
    fill(body, providerPanel(p, lead, { onChange }));
  }
  async function hooksOf(kind, fresh = false) {      // the basic webhooks (Slack, Teams, signed) the alert panels manage
    if (fresh || !intState.hooks) intState.hooks = api('/api/integrations/webhooks').catch((err) => { intState.hooks = null; throw err; });
    return (await intState.hooks).filter((x) => x.kind === kind);
  }
  function slotStatus(e, status) {
    const panel = (e.builtin && e.builtin.panel) || 'connector';
    const p = providerFor(e);
    const pill = (cls, text) => fill(status, h('span', { class: `pill ${cls}` }, text));
    if (panel === 'webhooks') {
      if (!can.write()) return;
      hooksOf(e.builtin.webhook_kind).then((hs) => { const on = hs.filter((w) => w.enabled).length; if (on) pill('green', `${on} on`); else pill('gray', hs.length ? 'Turned off' : 'None set up'); }).catch(() => fill(status));
    } else if (panel === 'schedule_import') pill('green', 'Ready to upload');
    else if (panel === 'calendar_feed') {
      if (p && (p.connection || {}).status === 'connected') pill('green', 'On'); else pill('gray', 'No link yet');
    } else if (!p) pill('gray', intState.providersError ? 'Status unavailable' : 'Not available yet');
    else if (!p.operator_configured) pill('amber', 'Not turned on yet');
    else { const [cls, text] = CONN_TEXT[(p.connection || {}).status] || CONN_TEXT.disconnected; pill(cls, text); }
  }
  function connectSlot(e) {
    const panel = (e.builtin && e.builtin.panel) || 'connector';
    const status = h('span', { class: 'int-conn-status' });
    slotStatus(e, status);
    const onChange = async () => {
      const p = providerFor(e);
      if (panel === 'calendar_feed' && p) { try { const d = await api(CAL_LINKS); p.connection = { ...(p.connection || {}), status: d.active ? 'connected' : 'disconnected' }; } catch (err) { /* keep the old pill */ } }
      slotStatus(e, status);
    };
    const body = h('div', { class: 'int-connect-body' }, h('p', { class: 'loading small' }, 'Loading…'));
    const dt = h('details', { class: 'int-connect', 'data-entry': e.id }, h('summary', {}, h('span', { class: 'int-connect-title' }, PANEL_TITLE[panel] || 'Connect'), status), body);
    let loaded = false;
    const load = () => { if (loaded) return; loaded = true; drawConnect(e, body, onChange).catch((err) => fill(body, msgBox('error', err.message))); };
    dt.addEventListener('toggle', () => { if (dt.open) load(); });
    dt.openPanel = () => { dt.open = true; load(); };
    return dt;
  }

  // ---------- catalog cards
  function mcpBlock(e) {
    const servers = e.mcp.servers || [];
    if (!servers.length && !e.mcp.note) return null;
    return h('div', { class: 'int-mcp' },
      h('h4', {}, 'With your AI assistant (MCP) ', h('span', { class: 'pill gray' }, e.mcp.kind_label)),
      servers.map((s) => h('div', { class: 'int-server' },
        h('p', { class: 'row' }, h('strong', {}, s.label),
          s.auth_needed ? h('span', { class: 'pill amber' }, 'Sign-in or key needed') : h('span', { class: 'pill green' }, 'No sign-in needed'),
          s.confirmed ? null : h('span', { class: 'pill gray' }, 'Address not confirmed')),
        s.url ? copyBox(s.url, `${s.label} address`) : null,
        s.url_template ? [h('p', { class: 'small muted' }, s.url ? 'Form for your own account:' : 'Address for your own account (fill in your values):'), copyBox(s.url_template, `${s.label} address form`)] : null,
        s.command ? [h('p', { class: 'small muted' }, 'Runs on your computer with:'), copyBox(s.command, `${s.label} command`)] : null,
        s.auth ? h('p', { class: 'small' }, h('span', { class: 'muted' }, 'Sign-in: '), s.auth) : null,
        s.notes ? h('p', { class: 'small muted' }, s.notes) : null)),
      e.mcp.note ? h('p', { class: 'small muted' }, e.mcp.note) : null);
  }
  function govLine(e) {
    const g = e.gov;
    if (!g) return h('p', { class: 'small muted int-gov' }, 'Government cloud: no US government version was found in our research.');
    return h('p', { class: 'small int-gov' }, h('span', { class: `pill ${GOV_PILL[g.supported] || 'gray'}` }, g.label), ' ', g.note, g.url ? [' ', ext(g.url, g.name || 'Government version')] : null);
  }
  function intCard(e, cat, kindLabels) {
    const id = `int-${e.id}`;
    const useLi = (u) => h('li', {}, withToolLinks(u.text));
    const uses = e.uses || [];
    const notes = (e.api && e.api.notes) || [];
    const card = h('article', { class: 'card int-card', id, 'aria-labelledby': `${id}-h` },
      h('div', { class: 'int-head' },
        h('div', { class: 'int-title' }, h('h3', { id: `${id}-h` }, e.name), h('p', { class: 'small muted' }, e.vendor)),
        h('div', { class: 'int-badges' }, h('span', { class: `pill ${INT_KIND_PILL[e.integration] || 'gray'}` }, kindLabels[e.integration] || e.integration), vendorStatusPill(e.status.label),
          e.recommended === false ? h('span', { class: 'pill red' }, 'Not recommended') : null)),
      h('p', {}, e.summary),
      e.recommended === false && e.not_recommended_reason ? msgBox('warn', e.not_recommended_reason) : null,
      h('p', { class: 'int-role' }, h('strong', {}, 'With TradeComply: '), e.tradecomply_role),
      e.builtin || (intState.providers || []).some((p) => p.catalog_id === e.id) ? connectSlot(e) : null,
      uses.length ? [h('h4', {}, 'How contractors use it with TradeComply'), h('ul', { class: 'bullets int-uses' }, uses.slice(0, 3).map(useLi))] : null,
      uses.length > 3 ? more(`${uses.length - 3} more ${uses.length - 3 === 1 ? 'idea' : 'ideas'}`, h('ul', { class: 'bullets int-uses' }, uses.slice(3).map(useLi))) : null,
      mcpBlock(e),
      govLine(e),
      e.caveats.length ? more(`Check before you rely on it (${e.caveats.length})`, h('ul', { class: 'bullets small' }, e.caveats.map((c) => h('li', {}, c)))) : null,
      (e.details || []).length ? more(`More about ${e.name.split(' (')[0]}`, e.details.map((d) => h('p', { class: 'small' }, d))) : null,
      e.api && (e.api.auth || notes.length) ? more('For your IT person or developer', e.api.auth ? h('p', { class: 'small' }, h('strong', {}, 'Sign-in: '), e.api.auth) : null, notes.length ? h('ul', { class: 'bullets small int-dev' }, notes.map((n) => h('li', {}, n))) : null) : null,
      more(`Sources and how we checked (${e.sources.length})`,
        h('p', { class: 'small' }, h('strong', {}, 'Vendor status: '), e.status.note),
        (e.research_notes || []).map((r) => h('p', { class: 'small muted' }, r)),
        h('ul', { class: 'bullets small int-sources' }, e.sources.map((s) => h('li', {}, s.url ? ext(s.url) : h('code', {}, s.path), s.note ? h('span', { class: 'muted' }, ` (${s.note})`) : null))),
        h('p', { class: 'small muted' }, `Checked ${fmtDate(cat.checked_on)}.`)),
      h('p', { class: 'int-links small' }, e.links.map((l, k) => [k ? ' · ' : '', ext(l.url, l.label)])));
    card.dataset.hay = [e.name, e.vendor, e.summary, e.tradecomply_role, (e.keywords || []).join(' '), uses.map((u) => u.text).join(' '),
      (e.tools || []).map((t) => `${t} ${humanize(t)}`).join(' '), kindLabels[e.integration], (e.builtin || {}).what_it_does || ''].join(' ').toLowerCase();
    return card;
  }
  function explainer(cat) {
    const ways = [
      ['builtin', 'TradeComply connects to it for you. Open the panel on its card (Connect, Set up alerts, Upload a schedule), then sign in, paste a link or upload a file. Examples: Procore, Docusign, Slack and Teams alerts, text messages and your calendar.'],
      ['ai_assistant', 'If your office uses an AI assistant such as Claude, ChatGPT or Copilot, add TradeComply and the other product to it. The assistant can read from one and check with the other in the same conversation, for example “pull our subs from Procore and check their licenses”. You sign in to each product yourself; TradeComply never sees those passwords.'],
      ['import_export', 'Some products don\'t connect directly. Export a file from them (a CSV list or a schedule) and upload it here, or download TradeComply\'s results and attach them there.'],
      ['reference', 'Background for your IT or security team, such as security checklists. Nothing to connect.'],
    ];
    const kindLabels = Object.fromEntries(cat.integration_types.map((k) => [k.id, k.label]));
    return h('section', { class: 'card', 'aria-labelledby': 'int-how-h' },
      h('h2', { id: 'int-how-h' }, 'How TradeComply works with your other tools'),
      h('p', {}, 'TradeComply doesn\'t replace the software you already use. It checks the California rules behind what\'s in it: licenses, liens, payroll, safety and contracts. There are four ways to put them together:'),
      h('ul', { class: 'int-ways clean' }, ways.map(([k, text]) => h('li', {}, h('span', { class: `pill ${INT_KIND_PILL[k]}` }, kindLabels[k]), h('p', {}, text)))),
      h('p', { class: 'small muted' }, 'TradeComply\'s AI-assistant tools only look things up and check them; they can\'t change anything in your other systems. Built-in connections only do what you set them up to do, like posting an alert to your channel. Product details were checked against each vendor\'s official documentation on ', fmtDate(cat.checked_on), '; anything we couldn\'t confirm is marked.'));
  }
  function claudeCard(url) {
    return h('section', { class: 'card', id: 'int-claude', 'aria-labelledby': 'int-claude-h' },
      h('h2', { id: 'int-claude-h' }, 'Add TradeComply to Claude'),
      h('p', {}, 'Use TradeComply\'s checks in Claude on the web, in Claude Desktop and in the Claude phone apps. Add it once by its address, then log in to TradeComply and approve it.'),
      claudeConnector(url || location.origin + '/mcp'));
  }
  function assistantBuilder(cat) {
    const ready = new Set(cat.config_ready_ids || []);
    const choices = cat.entries.filter((e) => ready.has(e.id));
    const out = h('div', { 'aria-live': 'polite' });
    const boxes = choices.map((e) => h('label', {}, h('input', { type: 'checkbox', name: 'cfg_pick', value: e.id }), e.name.split(' (')[0]));
    const make = h('button', { type: 'submit', class: 'btn' }, 'Make my setup');
    const form = h('form', { novalidate: true, onsubmit: async (ev) => {
      ev.preventDefault();
      fill(out);
      const ids = boxes.map((l) => l.querySelector('input')).filter((x) => x.checked).map((x) => x.value);
      if (!ids.length) { out.append(msgBox('error', 'Pick at least one product.')); return; }
      busy(make, true, 'Making…');
      try {
        const r = await api(`/api/integrations/catalog/mcp-config?ids=${encodeURIComponent(ids.join(','))}`);
        fill(out, 
          h('h3', {}, 'Claude Code'), h('p', { class: 'small muted' }, 'Save as .mcp.json in your project folder, or run the commands below. Set the environment variables first; keys stay out of the file.'),
          copyBox(r.claude_code_mcp_json_text, '.mcp.json', true),
          more('Or add them with commands', copyBox(r.claude_code_commands.join('\n'), 'commands', true)),
          h('h3', {}, 'Claude Desktop'), h('p', { class: 'small muted' }, 'Paste this into claude_desktop_config.json (Settings → Developer → Edit config; needs Node.js), then add the other products under Customize → Connectors → + Add → Add custom connector.'),
          copyBox(JSON.stringify(r.claude_desktop_config, null, 2), 'Claude Desktop config', true),
          r.claude_desktop_connectors.length ? table([{ label: 'Connector', key: 'name' }, { label: 'Address', render: (c) => h('code', {}, c.url) }, { label: 'Sign-in', render: (c) => h('span', { class: 'small' }, c.sign_in) }], r.claude_desktop_connectors) : null,
          r.claude_connector ? more('Or skip the config file: add TradeComply to Claude as a connector', h('ol', { class: 'steps small' }, (r.claude_connector.steps || []).map((s) => h('li', {}, s))),
            (r.claude_connector.notes || []).map((n) => h('p', { class: 'small muted' }, n))) : null,
          h('h3', {}, 'Keys to set'), h('ul', { class: 'bullets small' }, r.env_vars.map((v) => h('li', {}, h('code', {}, v.name), ` — ${v.used_for}`))),
          h('p', { class: 'small' }, 'No TradeComply API key yet? ', h('a', { href: '#/settings' }, 'Create one in Settings → AI assistant (MCP)'), '.'),
          r.skipped.length ? more(`Not added (${r.skipped.length})`, h('ul', { class: 'bullets small' }, r.skipped.map((s) => h('li', {}, h('strong', {}, s.name || s.id), `: ${s.reason}`)))) : null,
          h('ul', { class: 'bullets small muted' }, r.notes.map((n) => h('li', {}, n))));
      } catch (err) { fill(out, msgBox('error', err.message)); } finally { busy(make, false); }
    } }, h('fieldset', {}, h('legend', {}, 'Products to add next to TradeComply'), h('div', { class: 'checks' }, boxes)), make);
    return h('section', { class: 'card', id: 'int-assistant', 'aria-labelledby': 'int-asst-h' },
      h('h2', { id: 'int-asst-h' }, 'Set up your AI assistant with several tools'),
      h('p', { class: 'muted' }, 'Pick the products your office uses. You get one setup that adds TradeComply and them to Claude Code or Claude Desktop. Only products whose MCP address is confirmed in the vendor\'s own documentation are listed.'),
      form, out);
  }

  routes.integrations = async ({ query }) => {
    const [cat, live] = await Promise.all([api('/api/integrations/catalog'), api('/api/integrations').catch((err) => ({ error: err }))]);
    intState.catalog = cat;
    intState.hooks = null;
    if (live.error) { intState.providers = []; intState.providersError = live.error.message; } else { intState.providers = live.providers || []; intState.providersError = null; intState.loadErrors = live.load_errors || []; }
    const toolNames = [...new Set(cat.entries.flatMap((e) => e.tools || []))].sort((a, b) => b.length - a.length);
    intState.toolRe = toolNames.length ? new RegExp(`\\b(${toolNames.join('|')})\\b`, 'g') : null;
    const kindLabels = Object.fromEntries(cat.integration_types.map((k) => [k.id, k.label]));
    const filters = { q: query.get('q') || '', cat: query.get('cat') || '', types: new Set((query.get('type') || '').split(',').filter((t) => kindLabels[t])), gov: query.get('gov') === '1' };
    const top = [];
    const result = query.get('result');
    const focusId = query.get('integration');
    const focusEntry = focusId ? cat.entries.find((e) => e.id === focusId || (e.builtin && e.builtin.providers.includes(focusId))) || null : null;
    if (result === 'connected') top.push(msgBox('info', `${focusEntry ? focusEntry.name.split(' (')[0] : 'The integration'} is connected.`));
    else if (result === 'error') top.push(msgBox('error', `Couldn't connect ${focusEntry ? focusEntry.name.split(' (')[0] : 'the integration'}: `, query.get('message') || 'sign-in was not completed.'));
    if (can.write() && intState.loadErrors.length) top.push(msgBox('warn', `Some built-in connections failed to load on this server: ${intState.loadErrors.join(', ')}. The server log has the details.`));
    if (intState.providersError) top.push(msgBox('warn', `Connection status is unavailable right now: ${String(intState.providersError).replace(/\.?$/, '.')} You can still browse the catalog.`));

    // filters
    const search = h('input', { type: 'search', id: 'int-q', value: filters.q, placeholder: 'e.g. e-signature, text alerts, Procore, schedule', autocomplete: 'off' });
    const catSel = select('cat', [['', 'All categories'], ...cat.categories.map((c) => [c.id, c.label])], filters.cat, { id: 'int-cat' });
    const govBox = h('input', { type: 'checkbox', id: 'int-gov', checked: filters.gov });
    const count = h('p', { class: 'small muted', 'aria-live': 'polite', id: 'int-count' });
    const chips = cat.integration_types.map((k) => h('button', { type: 'button', class: 'chip', 'aria-pressed': filters.types.has(k.id) ? 'true' : 'false', 'data-type': k.id, title: k.description, onclick: (ev) => {
      const b = ev.currentTarget;
      if (filters.types.has(k.id)) filters.types.delete(k.id); else filters.types.add(k.id);
      b.setAttribute('aria-pressed', filters.types.has(k.id) ? 'true' : 'false');
      apply();
    } }, kindLabels[k.id]));
    const filterCard = h('section', { class: 'card int-filters no-print', 'aria-label': 'Find an integration' },
      h('div', { class: 'int-filter-row' },
        h('div', { class: 'field' }, h('label', { for: 'int-q' }, 'Search'), search),
        h('div', { class: 'field' }, h('label', { for: 'int-cat' }, 'Category'), catSel)),
      h('div', { class: 'row' }, h('span', { class: 'label', id: 'int-type-l' }, 'Show:'), h('div', { class: 'chips', role: 'group', 'aria-labelledby': 'int-type-l' }, chips)),
      h('label', { class: 'switch small-switch', for: 'int-gov' }, govBox, 'Only products with a US government cloud version (or no cloud involved)'),
      count);

    // sections
    const cards = new Map();
    const sections = cat.categories.map((c) => {
      const list = cat.entries.filter((e) => e.category === c.id)
        .sort((a, b) => ((a.recommended === false) - (b.recommended === false)) || (INT_ORDER[a.integration] - INT_ORDER[b.integration]) || a.name.localeCompare(b.name));
      if (!list.length) return null;
      const grid = h('div', { class: 'int-grid' }, list.map((e) => { const el = intCard(e, cat, kindLabels); cards.set(e.id, { el, e }); return el; }));
      return h('section', { class: 'int-section', id: `int-cat-${c.id}`, 'data-cat': c.id, 'aria-labelledby': `int-cat-${c.id}-h` },
        h('h2', { id: `int-cat-${c.id}-h` }, c.label), h('p', { class: 'muted small' }, c.description), grid);
    }).filter(Boolean);
    const matched = new Set(cat.entries.map((e) => providerFor(e)).filter(Boolean).map((p) => p.provider));
    const extra = (intState.providers || []).filter((p) => !matched.has(p.provider));
    const extraSection = extra.length ? h('section', { class: 'int-section', 'aria-labelledby': 'int-extra-h' }, h('h2', { id: 'int-extra-h' }, 'More connections on this server'),
      h('div', { class: 'int-grid' }, extra.map((p) => h('article', { class: 'card int-card', 'aria-label': p.display_name }, h('h3', {}, p.display_name), h('p', {}, p.description),
        p.docs_url ? h('p', { class: 'small' }, ext(p.docs_url, `${p.display_name} documentation`)) : null,
        h('div', { class: 'int-connect-body' }, providerPanel(p, null)))))) : null;
    const empty = h('div', { class: 'card empty', hidden: true }, h('p', {}, 'Nothing matches. Try another word, or clear the filters.'),
      h('button', { type: 'button', class: 'btn secondary', onclick: () => { search.value = ''; catSel.value = ''; govBox.checked = false; filters.types.clear(); chips.forEach((b) => b.setAttribute('aria-pressed', 'false')); apply(); } }, 'Clear filters'));

    function apply() {
      filters.q = search.value.trim();
      filters.cat = catSel.value;
      filters.gov = govBox.checked;
      const words = filters.q.toLowerCase().split(/\s+/).filter(Boolean);
      let shown = 0;
      for (const { el, e } of cards.values()) {
        const ok = (!filters.cat || e.category === filters.cat) && (!filters.types.size || filters.types.has(e.integration))
          && (!filters.gov || (e.gov && ['yes', 'partial', 'not_applicable'].includes(e.gov.supported)))
          && words.every((w) => el.dataset.hay.includes(w));
        el.hidden = !ok;
        if (ok) shown += 1;
      }
      sections.forEach((s) => { s.hidden = !s.querySelector('.int-card:not([hidden])'); });
      empty.hidden = shown > 0;
      count.textContent = `${plural(shown, 'product')} shown of ${cards.size}.`;
      const qs = new URLSearchParams();
      if (filters.q) qs.set('q', filters.q);
      if (filters.cat) qs.set('cat', filters.cat);
      if (filters.types.size) qs.set('type', [...filters.types].join(','));
      if (filters.gov) qs.set('gov', '1');
      history.replaceState(null, '', `#/integrations${qs.toString() ? '?' + qs : ''}`);
    }
    let timer;
    search.addEventListener('input', () => { clearTimeout(timer); timer = setTimeout(apply, 150); });
    catSel.addEventListener('change', apply);
    govBox.addEventListener('change', apply);

    const jump = h('nav', { class: 'jump no-print', 'aria-label': 'Integration categories' }, sections.map((s) => h('a', { href: '#', class: 'pill gray', onclick: (ev) => { ev.preventDefault(); s.scrollIntoView({ behavior: scrollBehavior(), block: 'start' }); } }, s.querySelector('h2').textContent)),
      h('a', { href: '#', class: 'pill blue', onclick: (ev) => { ev.preventDefault(); $('#int-assistant').scrollIntoView({ behavior: scrollBehavior(), block: 'start' }); } }, 'AI assistant setup'));
    const view = h('div', {},
      h('div', { class: 'page-head' }, h('div', {}, h('h1', {}, 'Integrations'), h('p', {}, 'Use TradeComply with Procore, Docusign, Slack, Teams, your calendar, your AI assistant and more.'))),
      top, explainer(cat), claudeCard(cat.tradecomply.mcp_url), filterCard, jump, empty, sections, extraSection, assistantBuilder(cat),
      h('p', { class: 'small muted' }, cat.about));
    apply();
    if (focusEntry && cards.has(focusEntry.id)) {
      const { el } = cards.get(focusEntry.id);
      el.hidden = false;
      setTimeout(() => { const dt = el.querySelector('details.int-connect'); if (dt && dt.openPanel) dt.openPanel(); el.scrollIntoView({ block: 'start' }); }, 0);
    }
    return view;
  };

  // ======================================================================== settings
  // Forget everything this browser holds for the signed-in company (the cached API copies and app state); after the
  // company is deleted, also its checklist and finder choices and any field logs still waiting to sync.
  async function signedOut({ forgetCompany = false } = {}) {
    const tid = state.me && state.me.tenant && state.me.tenant.id;
    if (forgetCompany && tid) {
      local.del(`tc.gettingStarted.${tid}`);
      local.del(`tc.finder.project.${tid}`);
      try { for (const x of await Outbox.all()) await Outbox.del(x.client_uuid); } catch (e) { /* no offline store */ }
    }
    if (navigator.serviceWorker && navigator.serviceWorker.controller) navigator.serviceWorker.controller.postMessage('clear-api-cache');
    if (window.caches) caches.delete('tc-api-v1').catch(() => {});
    state.me = null; state.tools = null; state.mcpPolicy = null; state.gsAway = null;
    $('#nav').hidden = true; $('#company').textContent = '';
  }
  // Owner only: one-file export of the company's records (GET /api/tenant/export) and closing the account
  // (DELETE /api/tenant). Until the server has those endpoints (404/405/501) the section says so instead of failing.
  async function downloadCompanyData(btn, out) {
    fill(out);
    busy(btn, true, 'Preparing your file…');
    try {
      let res;
      try { res = await fetch('/api/tenant/export', { credentials: 'same-origin', headers: { Accept: 'application/json' } }); } catch (e) {
        throw new ApiError(0, 'You appear to be offline. Try again when you have signal.');
      }
      if (!res.ok) {
        const data = (res.headers.get('content-type') || '').includes('json') ? await res.json().catch(() => null) : null;
        throw new ApiError(res.status, errMessage(data, res.status), data);
      }
      const blob = await res.blob();
      const m = (res.headers.get('content-disposition') || '').match(/filename\*?=(?:UTF-8'')?"?([^";]+)"?/i);
      let name = '';
      try { name = m ? decodeURIComponent(m[1]) : ''; } catch (e) { name = ''; }
      const slug = String(state.me.tenant.company_name || 'company').toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '').slice(0, 40) || 'company';
      name = name.replace(/[^\w.-]+/g, '_').slice(0, 120) || `tradecomply-${slug}-${todayISO()}.json`;
      saveBlob(name, blob);
      fill(out, msgBox('info', `Your download has started: ${name}. It can include employee and customer details, so keep it somewhere safe.`));
    } catch (e) {
      if (notOnServer(e)) fill(out, msgBox('info', 'Downloading all company data isn\'t available on this server yet. ', supportText('For a copy of your data')));
      else fill(out, msgBox(isOffline(e) ? 'warn' : 'error', e.message));
    } finally { busy(btn, false); }
  }
  function companyDataSection() {
    const tenant = state.me.tenant;
    const company = String(tenant.company_name || '').trim();
    const dlOut = h('div', { role: 'status' });
    const dl = h('button', { type: 'button', class: 'btn secondary', onclick: () => downloadCompanyData(dl, dlOut) }, 'Download all company data');
    const nameIn = input('confirm_company_name', { id: 'del-name', autocomplete: 'off', spellcheck: 'false' });
    const pwIn = input('password', { id: 'del-pw', type: 'password', autocomplete: 'current-password' });
    const err = h('div', { role: 'alert' });
    const del = h('button', { type: 'submit', class: 'btn danger' }, 'Delete company account permanently');
    const form = h('form', { novalidate: true, onsubmit: async (e) => {
      e.preventDefault();
      fill(err);
      if (nameIn.value.trim() !== company) { err.append(msgBox('error', `Type the company name exactly as it appears: ${company}`)); nameIn.setAttribute('aria-invalid', 'true'); nameIn.focus(); return; }
      nameIn.removeAttribute('aria-invalid');
      if (!pwIn.value) { err.append(msgBox('error', 'Enter your password.')); pwIn.setAttribute('aria-invalid', 'true'); pwIn.focus(); return; }
      pwIn.removeAttribute('aria-invalid');
      busy(del, true, 'Deleting…');
      try {
        const receipt = await api('/api/tenant', { method: 'DELETE', body: { confirm_company_name: nameIn.value.trim(), password: pwIn.value }, keepSession: true });
        // Billing and integration outcomes are only shown once, on the login page (the owner can't sign in to Billing now).
        lastDeletion = [receipt && receipt.billing && receipt.billing.message, ...((receipt && receipt.integrations) || []).map((i) => i && i.message)].filter((m) => typeof m === 'string' && m);
        await signedOut({ forgetCompany: true });
        go('login?deleted=1');
      } catch (ex) {
        pwIn.value = '';
        if (notOnServer(ex)) err.append(msgBox('info', 'Closing a company account isn\'t available on this server yet. ', supportText('To close your account')));
        else err.append(msgBox(isOffline(ex) ? 'warn' : 'error', ex.message));
        busy(del, false);
      }
    } },
    h('p', {}, 'This permanently deletes your company account and the records in it: projects, subcontractors, documents, field logs, team members and their logins, API keys, connected integrations and settings. Everyone on your team is signed out. It can\'t be undone.'),
    h('p', { class: 'small muted' }, 'Copies already outside TradeComply (files you downloaded or printed, calendars you subscribed, messages sent to Slack, Teams or by text) are not affected. Download your data first if you might need it.'),
    tenant.has_billing_account ? h('p', { class: 'small' }, 'If you pay for TradeComply by card, TradeComply tries to cancel the subscription and tells you the result after the account is deleted.') : null,
    h('div', { class: 'form-grid' },
      field('Type your company name to confirm', nameIn, { hint: `Type it exactly: ${company}` }),
      field('Your password', pwIn)),
    err, h('div', { class: 'row' }, del));
    return h('section', { class: 'card', id: 'company-data', 'aria-labelledby': 'cd-h' },
      h('h2', { id: 'cd-h' }, 'Account and data'),
      h('p', { class: 'muted' }, 'Download one file (JSON) with all your company\'s TradeComply records, for your own files or to move to another system.'),
      h('div', { class: 'row' }, dl), dlOut,
      h('details', { class: 'danger-zone' }, h('summary', {}, 'Delete company account'), h('div', { class: 'danger-body' }, form)));
  }
  routes.settings = async ({ query }) => {
    if (query.get('integration')) {         // the OAuth callback lands on #/settings?integration=..&result=..
      location.replace(`#/integrations?${query.toString()}`);
      return h('p', { class: 'loading' }, 'Loading…');
    }
    if (query.get('billing') === 'success') { toast('Thanks! Your subscription is being activated.'); setTimeout(async () => { await loadMe(); renderBanner(); }, 2500); }
    const me = state.me;
    const sections = [];
    // account
    const pwErr = h('div', { role: 'alert' });
    const pwForm = h('form', { onsubmit: async (e) => {
      e.preventDefault(); pwErr.replaceChildren();
      const d = formData(pwForm);
      if ((d.new_password || '').length < 10) { pwErr.append(h('div', { class: 'banner error' }, h('p', {}, 'Use at least 10 characters.'))); return; }
      try { await api('/api/auth/password', { method: 'POST', body: d }); toast('Password changed. Other devices were logged out.'); pwForm.reset(); await loadMe(); renderBanner(); } catch (ex) { pwErr.append(h('div', { class: 'banner error' }, h('p', {}, ex.message))); }
    } },
    h('div', { class: 'form-grid' }, field('Current password', input('current_password', { type: 'password', autocomplete: 'current-password' }), { required: true }),
      field('New password', input('new_password', { type: 'password', autocomplete: 'new-password', minlength: '10' }), { required: true, hint: 'At least 10 characters.' })),
    pwErr, h('button', { type: 'submit', class: 'btn secondary' }, 'Change password'));
    const logout = async () => {
      await api('/api/auth/logout', { method: 'POST' }).catch(() => {});
      signedOut();
      go('login');
    };
    sections.push(h('section', { class: 'card', 'aria-labelledby': 'acct-h' }, h('div', { class: 'card-head' }, h('h2', { id: 'acct-h' }, 'Your account'), h('button', { type: 'button', class: 'btn ghost', onclick: logout }, 'Log out')),
      h('dl', { class: 'kv' }, h('dt', {}, 'Email'), h('dd', {}, me.user.email || '—'), h('dt', {}, 'Role'), h('dd', {}, humanize(me.user.role))), pwForm));
    sections.push(connectedAppsSection());
    if (can.write()) {
      // company
      const cErr = h('div', { role: 'alert' });
      const cForm = h('form', { onsubmit: async (e) => {
        e.preventDefault(); cErr.replaceChildren();
        try { const t = await api('/api/tenant', { method: 'PATCH', body: formData(cForm) }); state.me.tenant = { ...state.me.tenant, ...t }; $('#company').textContent = t.company_name; toast('Company saved'); } catch (ex) { cErr.append(h('div', { class: 'banner error' }, h('p', {}, ex.message))); }
      } }, h('div', { class: 'form-grid' }, field('Company name', input('company_name', { value: me.tenant.company_name }), { required: true }),
        field('CSLB license number', input('cslb_license', { value: me.tenant.cslb_license || '', inputmode: 'numeric' }))), cErr, h('button', { type: 'submit', class: 'btn secondary' }, 'Save company'));
      sections.unshift(h('section', { class: 'card' }, h('h2', {}, 'Company'), cForm));
      // team
      const team = h('div', {});
      const drawTeam = async () => {
        const users = await api('/api/users');
        team.replaceChildren(table([
          { label: 'Person', bare: true, render: (u) => h('div', {}, h('strong', {}, u.name || u.email, u.id === me.user.id ? ' (you)' : ''), u.name ? h('div', { class: 'small muted' }, u.email) : null) },
          { label: 'Role', render: (u) => can.owner() && u.id !== me.user.id ? (() => { const s = select('role', [['owner', 'Owner'], ['office', 'Office'], ['field', 'Field']], u.role, { 'aria-label': `Role for ${u.email}` }); s.addEventListener('change', async () => { try { await api(`/api/users/${u.id}`, { method: 'PATCH', body: { role: s.value } }); toast('Role updated'); } catch (e) { toast(e.message, true); } }); return s; })() : humanize(u.role) },
          { label: 'Last login', render: (u) => fmtDateTime(u.last_login) },
          { label: 'Actions', bare: true, render: (u) => can.owner() && u.id !== me.user.id ? h('button', { type: 'button', class: 'btn ghost small', onclick: async () => { if (confirm(`Remove ${u.email}?`)) { await api(`/api/users/${u.id}`, { method: 'DELETE' }); drawTeam(); } } }, 'Remove') : null }], users));
      };
      await drawTeam();
      let invite = null;
      if (can.owner()) {
        const out = h('div', { 'aria-live': 'polite' });
        const iForm = h('form', { onsubmit: async (e) => {
          e.preventDefault(); out.replaceChildren();
          try {
            const r = await api('/api/users', { method: 'POST', body: formData(iForm) });
            out.append(h('div', { class: 'secret' }, h('p', {}, h('strong', {}, `Temporary password for ${r.email}`), ` (${humanize(r.role)}). ${r.message}`), copyBox(r.temporary_password, 'temporary password')));
            iForm.reset(); drawTeam();
          } catch (ex) { out.append(h('div', { class: 'banner error' }, h('p', {}, ex.message))); }
        } }, h('div', { class: 'form-grid' }, field('Email', input('email', { type: 'email' }), { required: true }),
          field('Role', select('role', [['field', 'Field (logs only)'], ['office', 'Office (everything but billing)'], ['owner', 'Owner (everything)']], 'field'))),
        h('button', { type: 'submit', class: 'btn secondary' }, 'Invite'));
        invite = h('div', {}, h('h3', {}, 'Invite someone'), iForm, out);
      }
      sections.push(h('section', { class: 'card' }, h('h2', {}, 'Team'), team, invite));
      // AI + keys
      const keys = h('div', {});
      const drawKeys = async () => {
        const rows = await api('/api/keys');
        keys.replaceChildren(rows.length ? table([
          { label: 'Name', key: 'name' }, { label: 'Key', render: (k) => h('code', {}, `${k.prefix}…`) }, { label: 'Created', render: (k) => fmtDateTime(k.created) },
          { label: 'Last used', render: (k) => fmtDateTime(k.last_used) },
          { label: 'Access', render: (k) => k.revoked ? '—' : keyAccessText(k) },
          { label: 'Calls/min', render: (k) => k.revoked ? '—' : (k.rate_limit_per_min ? String(k.rate_limit_per_min) : 'Default') },
          { label: 'Expires', render: (k) => k.revoked ? '—' : keyExpiryCell(k) },
          { label: 'Status', render: (k) => k.revoked ? h('span', { class: 'pill gray' }, 'Revoked') : k.expired ? h('span', { class: 'pill red' }, 'Expired') : h('span', { class: 'pill green' }, 'Active') },
          { label: 'Actions', bare: true, render: (k) => k.revoked ? null : h('div', { class: 'row' },
            h('button', { type: 'button', class: 'btn ghost small', onclick: () => editKeyAccess(k, drawKeys).catch((e) => toast(e.message, true)) }, 'Edit access'),
            h('button', { type: 'button', class: 'btn ghost small', onclick: async () => { if (confirm(`Revoke "${k.name}"? Anything using it stops working.`)) { await api(`/api/keys/${k.id}`, { method: 'DELETE' }); toast('Key revoked'); drawKeys(); } } }, 'Revoke')) }], rows) : h('p', { class: 'muted' }, 'No API keys yet.'));
      };
      await drawKeys();
      state.onKeysChanged = drawKeys;
      sections.push(h('section', { class: 'card', id: 'ai' }, h('h2', {}, 'AI assistant (MCP)'), agentConnect(), h('h3', {}, 'API keys'), keys));
      sections.push(h('section', { class: 'card' }, h('h2', {}, 'Integrations'), h('p', { class: 'muted' }, 'Connect Procore, Docusign, Slack or Teams alerts, text messages, your calendar and schedule files, and see which other tools work with TradeComply in your AI assistant.'),
        h('a', { class: 'btn secondary', href: '#/integrations' }, 'Open Integrations')));
      // calendar
      const cal = h('div', {});
      try {
        const links = calendarLinks();
        await links.draw();
        cal.append(links.el);
      } catch (e) { cal.append(h('p', { class: 'muted' }, e.message)); }
      sections.push(h('section', { class: 'card' }, h('h2', {}, 'Calendar'), h('p', { class: 'muted' }, 'Your deadlines as a private calendar link for Google Calendar, Outlook or Apple Calendar. Reminders and extra dates: ', h('a', { href: '#/integrations?integration=calendar-feed' }, 'Integrations → Calendar feed'), '.'), cal));
      // activity log
      const auditSlot = h('div', {});
      let auditShown = 10;
      const loadAudit = async () => {
        const all = await api('/api/audit?limit=200');
        const rows = all.slice(0, auditShown);
        const moreBtn = all.length > auditShown ? h('button', { type: 'button', class: 'btn secondary small', onclick: () => { auditShown += 25; loadAudit(); } }, `Show more (${all.length - auditShown} older)`) : null;
        auditSlot.replaceChildren(...[rows.length ? table([
          { label: 'When', render: (r) => fmtDateTime(r.created) }, { label: 'Who', key: 'user' }, { label: 'Check', render: (r) => humanize(r.tool) },
          { label: 'Via', render: (r) => r.source === 'mcp' ? 'AI assistant' : 'Web/API' },
          { label: 'Result', render: (r) => r.ok ? (r.verified === true ? h('span', { class: 'pill green' }, 'Verified') : r.verified === false ? h('span', { class: 'pill amber' }, 'Not yet verified') : h('span', { class: 'pill gray' }, 'Done')) : h('span', { class: 'pill red' }, 'Error') }], rows) : h('p', { class: 'muted' }, 'No checks run yet.'), moreBtn].filter(Boolean));
      };
      sections.push(h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', {}, 'Activity log'), h('button', { type: 'button', class: 'btn ghost small', onclick: loadAudit }, 'Refresh')), auditSlot));
      loadAudit().catch((e) => auditSlot.replaceChildren(h('p', {}, e.message)));
      if (can.owner()) sections.push(mcpLogSection());
    }
    if (can.owner()) {
      const b = await api('/api/billing/status').catch(() => null);
      const ent = me.entitlement || {};
      const items = [h('dl', { class: 'kv' }, h('dt', {}, 'Plan'), h('dd', {}, me.tenant.plan === 'local' ? 'Local install' : `Core, $${((b && b.price_usd_month) || 1000).toLocaleString('en-US')}/month`),
        h('dt', {}, 'Status'), h('dd', {}, h('span', { class: `pill ${ent.ok === false ? 'red' : ent.warning ? 'amber' : 'green'}` }, ent.message || humanize(me.tenant.status))))];
      if (me.tenant.plan !== 'local') {
        if (b && !b.configured) items.push(h('p', { class: 'muted' }, 'Online billing is not set up on this server yet. ', supportText('To subscribe')));
        items.push(h('div', { class: 'row' },
          me.tenant.status !== 'active' && b && b.configured ? h('button', { type: 'button', class: 'btn', onclick: (e) => subscribe(e.currentTarget) }, 'Subscribe') : null,
          me.tenant.has_billing_account ? h('button', { type: 'button', class: 'btn secondary', onclick: (e) => billingPortal(e.currentTarget) }, 'Manage billing and invoices') : null));
      }
      sections.push(h('section', { class: 'card', id: 'billing' }, h('h2', {}, 'Billing'), items));
      sections.push(companyDataSection());
    }
    // a long page on a phone: jump links to each section
    sections.forEach((sec, k) => { if (!sec.id) sec.id = `set-${k}`; });
    const jump = sections.length < 3 ? null : h('nav', { class: 'jump no-print', 'aria-label': 'Settings sections' }, sections.map((sec) => {
      const t = sec.querySelector('h2'); return t ? h('a', { href: '#', class: 'pill gray', onclick: (e) => { e.preventDefault(); sec.scrollIntoView({ behavior: scrollBehavior(), block: 'start' }); } }, t.textContent.replace(/ \(MCP\)$/, '')) : null;
    }));
    return h('div', {}, h('div', { class: 'page-head' }, h('h1', {}, 'Settings')), jump, h('div', { class: 'stack' }, sections));
  };

  // ======================================================================== boot
  function setOnline(on) {
    state.online = on;
    $('#net').hidden = on;
    if (on) syncOutbox({ quiet: true });
  }
  window.addEventListener('online', () => setOnline(true));
  window.addEventListener('offline', () => setOnline(false));
  window.addEventListener('hashchange', render);
  if ('serviceWorker' in navigator && (location.protocol === 'https:' || ['localhost', '127.0.0.1'].includes(location.hostname))) {
    navigator.serviceWorker.register('/sw.js').catch(() => {});
  }
  setInterval(() => syncOutbox({ quiet: true }), 60000);
  document.addEventListener('DOMContentLoaded', async () => {
    setOnline(navigator.onLine);
    await loadMe();
    if (!location.hash) location.replace(state.me ? '#/dashboard' : '#/login');
    if (!(state.me && resumeConnect())) await render();     // back from SSO: the app's request, if one was waiting
    syncOutbox({ quiet: true });
  });
})();
