/* API reference rendered from /api/openapi.json with DOM text only (the strict CSP blocks the CDN Swagger UI). */
(() => {
  'use strict';
  const h = (tag, cls, text) => { const el = document.createElement(tag); if (cls) el.className = cls; if (text !== undefined) el.textContent = String(text); return el; };
  const refName = (s) => (s && s.$ref ? s.$ref.split('/').pop() : s && s.type ? s.type : '');
  fetch('/api/openapi.json', { credentials: 'same-origin' }).then((r) => r.json()).then((spec) => {
    const root = document.getElementById('ops');
    root.textContent = '';
    const groups = {};
    for (const [path, ops] of Object.entries(spec.paths || {})) {
      for (const [method, op] of Object.entries(ops)) {
        const tag = (op.tags && op.tags[0]) || 'other';
        (groups[tag] = groups[tag] || []).push({ path, method: method.toUpperCase(), op });
      }
    }
    for (const [tag, ops] of Object.entries(groups)) {
      const sec = h('section', 'card');
      sec.append(h('h2', '', tag));
      for (const { path, method, op } of ops) {
        const d = h('details');
        const sum = h('summary');
        sum.append(h('code', '', method + ' ' + path), document.createTextNode(op.summary ? '  ' + op.summary : ''));
        d.append(sum);
        if (op.description) d.append(h('p', 'small', op.description));
        const params = (op.parameters || []).map((p) => `${p.name} (${p.in}${p.required ? ', required' : ''}${p.schema ? ', ' + refName(p.schema) : ''})`);
        if (params.length) d.append(h('p', 'small', 'Parameters: ' + params.join('; ')));
        const body = op.requestBody && op.requestBody.content;
        if (body) d.append(h('p', 'small', 'Body: ' + Object.entries(body).map(([ct, c]) => `${ct} ${refName(c.schema)}`).join('; ')));
        sec.append(d);
      }
      root.append(sec);
    }
    const schemas = (spec.components && spec.components.schemas) || {};
    if (Object.keys(schemas).length) {
      const sec = h('section', 'card');
      sec.append(h('h2', '', 'Schemas'));
      for (const [name, sch] of Object.entries(schemas)) {
        const d = h('details');
        d.append(h('summary', '', name));
        d.append(h('pre', 'mono', JSON.stringify(sch, null, 2)));
        sec.append(d);
      }
      root.append(sec);
    }
  }).catch(() => { document.getElementById('ops').textContent = 'Could not load /api/openapi.json.'; });
})();
