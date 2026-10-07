(() => {
  'use strict';

  // ---------- Speicher ----------
  const KEY = 'anacrusis.events.v1';
  const uid = () => Math.random().toString(36).slice(2, 10) + Date.now().toString(36).slice(-4);

  let state = load();

  function load() {
    try {
      const s = JSON.parse(localStorage.getItem(KEY));
      if (s && Array.isArray(s.events)) return s;
    } catch (e) { /* ignorieren */ }
    return { events: [] };
  }
  function save() {
    try { localStorage.setItem(KEY, JSON.stringify(state)); }
    catch (e) { alert('Speichern im Browser nicht möglich: ' + e.message); }
  }

  // ---------- Helfer ----------
  const $ = (sel, el = document) => el.querySelector(sel);
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const eur = n => (Number(n) || 0).toLocaleString('de-DE', { style: 'currency', currency: 'EUR' });
  const pct = (a, b) => (b > 0 ? Math.min(100, Math.round((a / b) * 100)) : 0);
  const fmtDate = (d, t) => {
    if (!d) return 'Kein Datum';
    const dt = new Date(d + 'T' + (t || '00:00'));
    const ds = dt.toLocaleDateString('de-DE', { weekday: 'short', day: '2-digit', month: '2-digit', year: 'numeric' });
    return t ? `${ds}, ${t} Uhr` : ds;
  };
  const today = () => new Date().toISOString().slice(0, 10);
  const findEvent = id => state.events.find(e => e.id === id);
  const num = v => (v === '' || v == null || isNaN(Number(v)) ? 0 : Number(v));

  const RSVP = { zugesagt: ['Zugesagt', 'ok'], offen: ['Offen', 'warn'], abgesagt: ['Abgesagt', 'bad'] };
  const BUDGET_CATS = ['Location', 'Catering', 'Technik', 'Dekoration', 'Personal', 'Marketing', 'Sonstiges'];

  function stats(ev) {
    const confirmed = ev.guests.filter(g => g.rsvp === 'zugesagt');
    const attendees = confirmed.reduce((s, g) => s + 1 + num(g.plusOnes), 0);
    const planned = ev.budget.reduce((s, b) => s + num(b.planned), 0);
    const actual = ev.budget.reduce((s, b) => s + num(b.actual), 0);
    const tasksDone = ev.tasks.filter(t => t.done).length;
    const overdue = ev.tasks.filter(t => !t.done && t.due && t.due < today()).length;
    return { attendees, planned, actual, tasksDone, overdue };
  }

  // ---------- Dialoge ----------
  const dlg = $('#dialog');
  const dlgForm = $('#dialog-form');

  // fields: [{name,label,type,value,options,required,placeholder,step}] — Layout: Felder mit half:true paarweise
  function openForm({ title, fields, submitLabel = 'Speichern', onSubmit, onDelete }) {
    const fieldHtml = f => {
      const v = f.value ?? '';
      const req = f.required ? 'required' : '';
      let input;
      if (f.type === 'select') {
        input = `<select name="${f.name}" ${req}>${f.options.map(([val, lab]) => `<option value="${esc(val)}" ${val === v ? 'selected' : ''}>${esc(lab)}</option>`).join('')}</select>`;
      } else if (f.type === 'textarea') {
        input = `<textarea name="${f.name}" rows="3" ${req}>${esc(v)}</textarea>`;
      } else {
        input = `<input name="${f.name}" type="${f.type || 'text'}" value="${esc(v)}" ${req} ${f.step ? `step="${f.step}"` : ''} ${f.min != null ? `min="${f.min}"` : ''} placeholder="${esc(f.placeholder || '')}">`;
      }
      return `<label>${esc(f.label)}${input}</label>`;
    };
    let body = '';
    for (let i = 0; i < fields.length; i++) {
      if (fields[i].half && fields[i + 1]?.half) { body += `<div class="two">${fieldHtml(fields[i])}${fieldHtml(fields[i + 1])}</div>`; i++; }
      else body += fieldHtml(fields[i]);
    }
    dlgForm.innerHTML = `<h2>${esc(title)}</h2>${body}
      <menu>
        ${onDelete ? '<button type="button" class="btn danger" data-del>Löschen</button><span class="spacer" style="flex:1"></span>' : ''}
        <button type="button" class="btn" data-cancel>Abbrechen</button>
        <button type="submit" class="btn primary">${esc(submitLabel)}</button>
      </menu>`;
    dlgForm.onsubmit = e => {
      e.preventDefault();
      const data = Object.fromEntries(new FormData(dlgForm).entries());
      dlg.close();
      onSubmit(data);
    };
    $('[data-cancel]', dlgForm).onclick = () => dlg.close();
    if (onDelete) $('[data-del]', dlgForm).onclick = () => {
      if (confirm('Wirklich löschen?')) { dlg.close(); onDelete(); }
    };
    dlg.showModal();
    const first = $('input, select, textarea', dlgForm);
    if (first) first.focus();
  }

  // ---------- Event-Aktionen ----------
  function editEvent(ev) {
    const isNew = !ev;
    const e = ev || { name: '', date: '', time: '', location: '', capacity: '', description: '' };
    openForm({
      title: isNew ? 'Neues Event' : 'Event bearbeiten',
      fields: [
        { name: 'name', label: 'Name', value: e.name, required: true },
        { name: 'date', label: 'Datum', type: 'date', value: e.date, half: true },
        { name: 'time', label: 'Uhrzeit', type: 'time', value: e.time, half: true },
        { name: 'location', label: 'Ort', value: e.location },
        { name: 'capacity', label: 'Max. Teilnehmer (optional)', type: 'number', min: 0, value: e.capacity },
        { name: 'description', label: 'Beschreibung', type: 'textarea', value: e.description }
      ],
      onSubmit: d => {
        if (isNew) {
          const n = { id: uid(), ...d, capacity: d.capacity === '' ? '' : num(d.capacity), guests: [], tasks: [], budget: [] };
          state.events.push(n); save(); location.hash = '#/event/' + n.id;
        } else {
          Object.assign(ev, d, { capacity: d.capacity === '' ? '' : num(d.capacity) }); save(); render();
        }
      },
      onDelete: isNew ? null : () => { state.events = state.events.filter(x => x.id !== ev.id); save(); location.hash = '#/events'; }
    });
  }

  function editGuest(ev, g) {
    const isNew = !g;
    const x = g || { name: '', email: '', rsvp: 'offen', plusOnes: 0, note: '' };
    openForm({
      title: isNew ? 'Gast hinzufügen' : 'Gast bearbeiten',
      fields: [
        { name: 'name', label: 'Name', value: x.name, required: true },
        { name: 'email', label: 'E-Mail', type: 'email', value: x.email },
        { name: 'rsvp', label: 'Status', type: 'select', value: x.rsvp, options: Object.entries(RSVP).map(([k, v]) => [k, v[0]]), half: true },
        { name: 'plusOnes', label: 'Begleitpersonen', type: 'number', min: 0, value: x.plusOnes, half: true },
        { name: 'note', label: 'Notiz (z. B. Allergien)', value: x.note }
      ],
      onSubmit: d => {
        d.plusOnes = num(d.plusOnes);
        if (isNew) ev.guests.push({ id: uid(), ...d }); else Object.assign(g, d);
        save(); render();
      },
      onDelete: isNew ? null : () => { ev.guests = ev.guests.filter(i => i.id !== g.id); save(); render(); }
    });
  }

  function editTask(ev, t) {
    const isNew = !t;
    const x = t || { title: '', due: '', owner: '', done: false };
    openForm({
      title: isNew ? 'Aufgabe hinzufügen' : 'Aufgabe bearbeiten',
      fields: [
        { name: 'title', label: 'Aufgabe', value: x.title, required: true },
        { name: 'due', label: 'Fällig am', type: 'date', value: x.due, half: true },
        { name: 'owner', label: 'Verantwortlich', value: x.owner, half: true }
      ],
      onSubmit: d => { if (isNew) ev.tasks.push({ id: uid(), done: false, ...d }); else Object.assign(t, d); save(); render(); },
      onDelete: isNew ? null : () => { ev.tasks = ev.tasks.filter(i => i.id !== t.id); save(); render(); }
    });
  }

  function editBudget(ev, b) {
    const isNew = !b;
    const x = b || { item: '', category: 'Sonstiges', planned: '', actual: '' };
    openForm({
      title: isNew ? 'Budgetposten hinzufügen' : 'Budgetposten bearbeiten',
      fields: [
        { name: 'item', label: 'Posten', value: x.item, required: true },
        { name: 'category', label: 'Kategorie', type: 'select', value: x.category, options: BUDGET_CATS.map(c => [c, c]) },
        { name: 'planned', label: 'Geplant (€)', type: 'number', step: '0.01', min: 0, value: x.planned, half: true },
        { name: 'actual', label: 'Tatsächlich (€)', type: 'number', step: '0.01', min: 0, value: x.actual, half: true }
      ],
      onSubmit: d => {
        d.planned = num(d.planned); d.actual = num(d.actual);
        if (isNew) ev.budget.push({ id: uid(), ...d }); else Object.assign(b, d);
        save(); render();
      },
      onDelete: isNew ? null : () => { ev.budget = ev.budget.filter(i => i.id !== b.id); save(); render(); }
    });
  }

  // ---------- Ansichten ----------
  const app = $('#app');
  const ui = { guestFilter: '', guestStatus: '', eventSearch: '', eventScope: 'alle' };

  function eventCard(ev) {
    const s = stats(ev);
    const past = ev.date && ev.date < today();
    return `<a class="event-card" href="#/event/${ev.id}">
      <div class="row between"><h3>${esc(ev.name)}</h3>${past ? '<span class="badge">Vorbei</span>' : ''}</div>
      <div class="meta">${esc(fmtDate(ev.date, ev.time))}${ev.location ? ' · ' + esc(ev.location) : ''}</div>
      <div class="meta" style="margin-top:8px">👥 ${s.attendees}${ev.capacity ? ' / ' + ev.capacity : ''} Teilnehmer · ✔ ${s.tasksDone}/${ev.tasks.length} Aufgaben</div>
      <div class="progress"><i style="width:${pct(s.tasksDone, ev.tasks.length)}%"></i></div>
    </a>`;
  }

  function viewDashboard() {
    const evs = state.events;
    const upcoming = evs.filter(e => !e.date || e.date >= today()).sort((a, b) => (a.date || '9').localeCompare(b.date || '9'));
    const all = evs.map(stats);
    const sum = k => all.reduce((s, x) => s + x[k], 0);
    const openTasks = evs.flatMap(e => e.tasks.filter(t => !t.done).map(t => ({ ...t, ev: e })))
      .sort((a, b) => (a.due || '9').localeCompare(b.due || '9')).slice(0, 8);

    app.innerHTML = `<h1>Übersicht</h1>
      <div class="stats">
        <div class="stat"><b>${upcoming.length}</b><small>Kommende Events</small></div>
        <div class="stat"><b>${sum('attendees')}</b><small>Bestätigte Teilnehmer</small></div>
        <div class="stat"><b>${sum('overdue')}</b><small>Überfällige Aufgaben</small></div>
        <div class="stat"><b>${eur(sum('actual'))}</b><small>Ausgaben (Plan ${eur(sum('planned'))})</small></div>
      </div>
      ${evs.length === 0 ? `<div class="card empty">Noch keine Events. <br><br><button class="btn primary" data-act="new">Erstes Event anlegen</button> oder <button class="btn" data-act="demo">Beispieldaten laden</button></div>` : `
      <div class="card"><h2>Nächste Events</h2>${upcoming.length ? `<div class="grid">${upcoming.slice(0, 6).map(eventCard).join('')}</div>` : '<div class="empty">Keine kommenden Events.</div>'}</div>
      <div class="card"><h2>Offene Aufgaben</h2>${openTasks.length ? `<table><tbody>${openTasks.map(t => `<tr>
        <td class="title">${esc(t.title)}<br><a href="#/event/${t.ev.id}/tasks" class="muted">${esc(t.ev.name)}</a></td>
        <td class="num">${t.due ? `<span class="badge ${t.due < today() ? 'bad' : ''}">${esc(fmtDate(t.due))}</span>` : ''}</td></tr>`).join('')}</tbody></table>` : '<div class="empty">Alles erledigt 🎉</div>'}</div>`}`;
  }

  function viewEvents() {
    const q = ui.eventSearch.toLowerCase();
    let list = state.events.filter(e => (e.name + ' ' + e.location).toLowerCase().includes(q));
    if (ui.eventScope === 'kommend') list = list.filter(e => !e.date || e.date >= today());
    if (ui.eventScope === 'vergangen') list = list.filter(e => e.date && e.date < today());
    list.sort((a, b) => (a.date || '9').localeCompare(b.date || '9'));
    app.innerHTML = `<h1>Events</h1>
      <div class="toolbar">
        <input type="search" id="ev-search" placeholder="Suchen…" value="${esc(ui.eventSearch)}">
        <select id="ev-scope">${['alle', 'kommend', 'vergangen'].map(s => `<option ${s === ui.eventScope ? 'selected' : ''}>${s}</option>`).join('')}</select>
      </div>
      ${list.length ? `<div class="grid">${list.map(eventCard).join('')}</div>` : '<div class="card empty">Keine Events gefunden.</div>'}`;
    const search = $('#ev-search');
    search.oninput = () => { ui.eventSearch = search.value; const pos = search.selectionStart; viewEvents(); const n = $('#ev-search'); n.focus(); n.setSelectionRange(pos, pos); };
    $('#ev-scope').onchange = e => { ui.eventScope = e.target.value; viewEvents(); };
  }

  function viewEvent(ev, tab) {
    const s = stats(ev);
    const tabs = [['', 'Überblick'], ['guests', `Gäste (${ev.guests.length})`], ['tasks', `Aufgaben (${ev.tasks.length})`], ['budget', 'Budget']];
    let content = '';
    if (tab === 'guests') content = tabGuests(ev);
    else if (tab === 'tasks') content = tabTasks(ev);
    else if (tab === 'budget') content = tabBudget(ev);
    else content = tabOverview(ev, s);

    app.innerHTML = `<div class="row between"><h1>${esc(ev.name)}</h1><button class="btn" data-act="edit-event">Bearbeiten</button></div>
      <div class="muted" style="margin:-8px 0 14px">${esc(fmtDate(ev.date, ev.time))}${ev.location ? ' · ' + esc(ev.location) : ''}</div>
      <div class="tabs">${tabs.map(([k, l]) => `<a href="#/event/${ev.id}${k ? '/' + k : ''}" class="${k === (tab || '') ? 'active' : ''}">${l}</a>`).join('')}</div>
      ${content}`;

    app.onclick = e => handleEventClick(e, ev);
    const gf = $('#guest-filter'); if (gf) {
      gf.oninput = () => { ui.guestFilter = gf.value; const p = gf.selectionStart; viewEvent(ev, tab); const n = $('#guest-filter'); n.focus(); n.setSelectionRange(p, p); };
      $('#guest-status').onchange = e => { ui.guestStatus = e.target.value; viewEvent(ev, tab); };
    }
  }

  function tabOverview(ev, s) {
    const over = ev.capacity && s.attendees > ev.capacity;
    const count = r => ev.guests.filter(g => g.rsvp === r).length;
    return `<div class="stats">
        <div class="stat"><b>${s.attendees}${ev.capacity ? ' / ' + ev.capacity : ''}</b><small>Bestätigte Teilnehmer ${over ? '<span class="badge bad">Kapazität überschritten</span>' : ''}</small></div>
        <div class="stat"><b>${s.tasksDone} / ${ev.tasks.length}</b><small>Aufgaben erledigt${s.overdue ? ` · <span class="badge bad">${s.overdue} überfällig</span>` : ''}</small></div>
        <div class="stat"><b>${eur(s.actual)}</b><small>von ${eur(s.planned)} geplant</small></div>
      </div>
      <div class="card"><h2>Beschreibung</h2><p>${ev.description ? esc(ev.description).replace(/\n/g, '<br>') : '<span class="muted">Keine Beschreibung.</span>'}</p></div>
      <div class="card"><h2>Antworten</h2>
        <span class="badge ok">${count('zugesagt')} zugesagt</span>
        <span class="badge warn">${count('offen')} offen</span>
        <span class="badge bad">${count('abgesagt')} abgesagt</span></div>`;
  }

  function tabGuests(ev) {
    const q = ui.guestFilter.toLowerCase();
    const list = ev.guests.filter(g => (!ui.guestStatus || g.rsvp === ui.guestStatus) && (g.name + ' ' + g.email + ' ' + g.note).toLowerCase().includes(q));
    return `<div class="toolbar">
        <input type="search" id="guest-filter" placeholder="Gäste suchen…" value="${esc(ui.guestFilter)}">
        <select id="guest-status"><option value="">Alle Status</option>${Object.entries(RSVP).map(([k, v]) => `<option value="${k}" ${ui.guestStatus === k ? 'selected' : ''}>${v[0]}</option>`).join('')}</select>
        <button class="btn" data-act="export-guests">CSV</button>
        <button class="btn primary" data-act="add-guest">+ Gast</button>
      </div>
      <div class="card">${list.length ? `<table><thead><tr><th>Name</th><th>Status</th><th class="num">+</th><th></th></tr></thead><tbody>
        ${list.map(g => `<tr><td>${esc(g.name)}<br><span class="muted">${esc(g.email)}${g.note ? ' · ' + esc(g.note) : ''}</span></td>
          <td><span class="badge ${RSVP[g.rsvp]?.[1]}">${RSVP[g.rsvp]?.[0] ?? esc(g.rsvp)}</span></td>
          <td class="num">${g.plusOnes || 0}</td>
          <td class="actions"><button class="btn sm" data-act="edit-guest" data-id="${g.id}">Bearbeiten</button></td></tr>`).join('')}
        </tbody></table>` : '<div class="empty">Keine Gäste.</div>'}</div>`;
  }

  function tabTasks(ev) {
    const list = [...ev.tasks].sort((a, b) => (a.done - b.done) || (a.due || '9').localeCompare(b.due || '9'));
    return `<div class="toolbar"><span class="spacer" style="flex:1"></span><button class="btn primary" data-act="add-task">+ Aufgabe</button></div>
      <div class="card">${list.length ? `<table><tbody>${list.map(t => `<tr class="${t.done ? 'done' : ''}">
        <td style="width:30px"><input type="checkbox" data-act="toggle-task" data-id="${t.id}" ${t.done ? 'checked' : ''} aria-label="Erledigt"></td>
        <td class="title">${esc(t.title)}${t.owner ? `<br><span class="muted">${esc(t.owner)}</span>` : ''}</td>
        <td class="num">${t.due ? `<span class="badge ${!t.done && t.due < today() ? 'bad' : ''}">${esc(fmtDate(t.due))}</span>` : ''}</td>
        <td class="actions"><button class="btn sm" data-act="edit-task" data-id="${t.id}">Bearbeiten</button></td></tr>`).join('')}</tbody></table>` : '<div class="empty">Keine Aufgaben.</div>'}</div>`;
  }

  function tabBudget(ev) {
    const s = stats(ev);
    const byCat = {};
    ev.budget.forEach(b => { (byCat[b.category] ||= { p: 0, a: 0 }); byCat[b.category].p += num(b.planned); byCat[b.category].a += num(b.actual); });
    return `<div class="stats">
        <div class="stat"><b>${eur(s.planned)}</b><small>Geplant</small></div>
        <div class="stat"><b>${eur(s.actual)}</b><small>Tatsächlich</small></div>
        <div class="stat"><b style="color:${s.actual > s.planned ? 'var(--bad)' : 'var(--ok)'}">${eur(s.planned - s.actual)}</b><small>${s.actual > s.planned ? 'Überschreitung' : 'Verbleibend'}</small></div>
      </div>
      <div class="toolbar"><span class="spacer" style="flex:1"></span><button class="btn primary" data-act="add-budget">+ Posten</button></div>
      <div class="card">${ev.budget.length ? `<table><thead><tr><th>Posten</th><th class="num">Geplant</th><th class="num">Tatsächlich</th><th></th></tr></thead><tbody>
        ${ev.budget.map(b => `<tr><td>${esc(b.item)}<br><span class="muted">${esc(b.category)}</span></td>
          <td class="num">${eur(b.planned)}</td>
          <td class="num" style="${num(b.actual) > num(b.planned) ? 'color:var(--bad)' : ''}">${eur(b.actual)}</td>
          <td class="actions"><button class="btn sm" data-act="edit-budget" data-id="${b.id}">Bearbeiten</button></td></tr>`).join('')}
        </tbody></table>` : '<div class="empty">Noch keine Budgetposten.</div>'}</div>
      ${Object.keys(byCat).length ? `<div class="card"><h2>Nach Kategorie</h2>${Object.entries(byCat).map(([c, v]) => `
        <div style="margin-bottom:10px"><div class="row between"><span>${esc(c)}</span><span class="muted">${eur(v.a)} / ${eur(v.p)}</span></div>
        <div class="progress ${v.a > v.p ? 'over' : ''}"><i style="width:${pct(v.a, v.p || v.a)}%"></i></div></div>`).join('')}</div>` : ''}`;
  }

  function handleEventClick(e, ev) {
    const el = e.target.closest('[data-act]');
    if (!el) return;
    const act = el.dataset.act, id = el.dataset.id;
    if (act === 'edit-event') editEvent(ev);
    else if (act === 'add-guest') editGuest(ev);
    else if (act === 'edit-guest') editGuest(ev, ev.guests.find(x => x.id === id));
    else if (act === 'add-task') editTask(ev);
    else if (act === 'edit-task') editTask(ev, ev.tasks.find(x => x.id === id));
    else if (act === 'toggle-task') { const t = ev.tasks.find(x => x.id === id); t.done = el.checked; save(); render(); }
    else if (act === 'add-budget') editBudget(ev);
    else if (act === 'edit-budget') editBudget(ev, ev.budget.find(x => x.id === id));
    else if (act === 'export-guests') exportGuestsCsv(ev);
  }

  function exportGuestsCsv(ev) {
    const cell = v => {
      let s = String(v ?? '');
      if (/^[=+\-@\t\r]/.test(s)) s = "'" + s; // CSV-Injection vermeiden
      return '"' + s.replace(/"/g, '""') + '"';
    };
    const rows = [['Name', 'E-Mail', 'Status', 'Begleitpersonen', 'Notiz'],
      ...ev.guests.map(g => [g.name, g.email, RSVP[g.rsvp]?.[0] ?? g.rsvp, g.plusOnes, g.note])];
    download(`gaeste-${slug(ev.name)}.csv`, '﻿' + rows.map(r => r.map(cell).join(';')).join('\n'), 'text/csv;charset=utf-8');
  }

  // ---------- Router ----------
  function render() {
    const [, route, id, tab] = location.hash.split('/');
    app.onclick = null;
    document.querySelectorAll('[data-nav]').forEach(a => a.classList.toggle('active',
      (a.dataset.nav === 'events' && (route === 'events' || route === 'event')) || (a.dataset.nav === 'dashboard' && !route)));
    if (route === 'event') {
      const ev = findEvent(id);
      if (ev) return viewEvent(ev, tab);
      location.hash = '#/events'; return;
    }
    if (route === 'events') return viewEvents();
    viewDashboard();
    app.onclick = e => {
      const act = e.target.closest('[data-act]')?.dataset.act;
      if (act === 'new') editEvent();
      if (act === 'demo') { loadDemo(); }
    };
  }

  // ---------- Export / Import / Demo ----------
  const slug = s => String(s).toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '') || 'event';
  function download(name, content, type) {
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([content], { type }));
    a.download = name; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  }

  $('#btn-new-event').onclick = () => editEvent();
  $('#btn-export').onclick = () => download(`anacrusis-events-${today()}.json`, JSON.stringify(state, null, 2), 'application/json');
  $('#file-import').onchange = async e => {
    const file = e.target.files[0]; e.target.value = '';
    if (!file) return;
    try {
      const data = JSON.parse(await file.text());
      if (!data || !Array.isArray(data.events)) throw new Error('Ungültiges Format');
      if (!confirm(`${data.events.length} Event(s) importieren? Bestehende Daten werden ersetzt.`)) return;
      data.events.forEach(ev => { ev.guests ||= []; ev.tasks ||= []; ev.budget ||= []; ev.id ||= uid(); });
      state = { events: data.events }; save(); render();
    } catch (err) { alert('Import fehlgeschlagen: ' + err.message); }
  };

  function loadDemo() {
    const d = n => { const x = new Date(); x.setDate(x.getDate() + n); return x.toISOString().slice(0, 10); };
    state.events.push({
      id: uid(), name: 'Sommerfest 2026', date: d(30), time: '16:00', location: 'Stadtpark Pavillon', capacity: 80,
      description: 'Jährliches Sommerfest für Team und Freunde mit Grill, Livemusik und Spielen.',
      guests: [
        { id: uid(), name: 'Anna Schmidt', email: 'anna@example.com', rsvp: 'zugesagt', plusOnes: 1, note: 'vegetarisch' },
        { id: uid(), name: 'Ben Weber', email: 'ben@example.com', rsvp: 'offen', plusOnes: 0, note: '' },
        { id: uid(), name: 'Clara Fischer', email: 'clara@example.com', rsvp: 'abgesagt', plusOnes: 0, note: '' }
      ],
      tasks: [
        { id: uid(), title: 'Location buchen', due: d(-2), owner: 'Anna', done: true },
        { id: uid(), title: 'Catering anfragen', due: d(7), owner: 'Ben', done: false },
        { id: uid(), title: 'Einladungen versenden', due: d(-1), owner: 'Clara', done: false }
      ],
      budget: [
        { id: uid(), item: 'Pavillon-Miete', category: 'Location', planned: 800, actual: 800 },
        { id: uid(), item: 'Grill & Getränke', category: 'Catering', planned: 1200, actual: 1350 },
        { id: uid(), item: 'Live-Band', category: 'Technik', planned: 600, actual: 0 }
      ]
    });
    save(); render();
  }

  window.addEventListener('hashchange', render);
  render();
})();
