// CampWatch web app. No build step: plain ES modules talking to the JSON API under /api.

const state = { status: null, refreshTimer: null, siteCache: new Map() };
const view = document.getElementById('view');

// ---------- Small helpers ----------

function h(tag, props, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') el.className = value;
    else if (key.startsWith('on')) el.addEventListener(key.slice(2), value);
    else if (value === true) el.setAttribute(key, '');
    else el.setAttribute(key, value);
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function api(path, { method = 'GET', body } = {}) {
  const res = await fetch(`/api${path}`, {
    method,
    credentials: 'same-origin',
    headers: body ? { 'Content-Type': 'application/json' } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (res.status === 401 && path !== '/login') {
    showLogin();
    throw new ApiError(401, 'Log in first');
  }
  if (!res.ok) {
    let message = res.statusText || 'Request failed';
    try {
      message = describeError((await res.json()).detail) || message;
    } catch { /* not JSON */ }
    throw new ApiError(res.status, message);
  }
  return res.status === 204 ? null : res.json();
}

function describeError(detail) {
  if (Array.isArray(detail)) {
    return detail.map((d) => String(d.msg || '').replace(/^Value error, /, '')).join('; ');
  }
  return detail;
}

let toastTimer;
function toast(message, kind = '') {
  const el = document.getElementById('toast');
  el.textContent = message;
  el.className = `toast ${kind}`;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, kind === 'error' ? 6000 : 3000);
}

function showError(err) {
  if (err?.status !== 401) toast(err?.message || String(err), 'error');
}

// Dates arrive as "YYYY-MM-DD"; build them in local time so they don't shift a day.
function parseDay(s) {
  const [y, m, d] = s.split('-').map(Number);
  return new Date(y, m - 1, d);
}

function isoDay(date) {
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
}

function addDays(s, n) {
  const d = parseDay(s);
  d.setDate(d.getDate() + n);
  return isoDay(d);
}

function nightsBetween(a, b) {
  return Math.round((parseDay(b) - parseDay(a)) / 86400000);
}

const dayFmt = new Intl.DateTimeFormat(undefined, { weekday: 'short', month: 'short', day: 'numeric' });

function rangeLabel({ check_in, check_out }) {
  const n = nightsBetween(check_in, check_out);
  return `${dayFmt.format(parseDay(check_in))} → ${dayFmt.format(parseDay(check_out))} (${n} night${n === 1 ? '' : 's'})`;
}

function ago(iso) {
  const secs = Math.max(0, (Date.now() - new Date(iso)) / 1000);
  if (secs < 45) return 'just now';
  if (secs < 3600) return `${Math.round(secs / 60)} min ago`;
  if (secs < 86400) return `${Math.round(secs / 3600)} h ago`;
  return new Date(iso).toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

function inSeconds(iso) {
  const secs = Math.round((new Date(iso) - Date.now()) / 1000);
  return secs <= 0 ? 'any moment' : secs < 90 ? `in ${secs} s` : `in ${Math.round(secs / 60)} min`;
}

function stayLabel(w) {
  if (w.piecemeal) return 'Night by night';
  return w.min_nights ? `Any ${w.min_nights}+ nights in a row` : 'Whole stay at one site';
}

// ---------- Night-by-night progress ----------

const NIGHT_LABELS = { needed: 'Looking', booked: 'Booked', held: 'In cart', skipped: 'Skipped', past: 'Passed' };
const NIGHT_ACTIONS = {
  needed: [['booked', 'I booked it'], ['skipped', 'Skip this night']],
  held: [['booked', 'I booked it'], ['needed', 'Didn’t book, keep looking']],
  booked: [['needed', 'Cancelled, look again'], ['skipped', 'Don’t need it']],
  skipped: [['needed', 'Look for it again']],
};
const weekdayFmt = new Intl.DateTimeFormat(undefined, { weekday: 'short' });
const monthDayFmt = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' });

function nightProgress(w) {
  const today = isoDay(new Date());
  const future = w.night_plan.filter((p) => p.night >= today);
  return { today, future, looking: future.filter((p) => p.status === 'needed') };
}

function nightStrip(w, onUpdate) {
  const { today, future, looking } = nightProgress(w);
  const menu = h('div', { class: 'night-menu', hidden: true });
  let shown = null;

  const showMenu = (p, status) => {
    if (shown === p.night) { menu.hidden = true; shown = null; return; }
    shown = p.night;
    const where = p.site ? `Site ${p.site}${p.campground_name ? `, ${p.campground_name}` : ''}` : '';
    menu.replaceChildren(
      h('div', { class: 'small' }, h('strong', {}, dayFmt.format(parseDay(p.night))), where && ` · ${where}`),
      h('div', { class: 'actions' }, (NIGHT_ACTIONS[status] || []).map(([to, label]) => {
        const button = h('button', { class: 'btn small', type: 'button' }, label);
        button.addEventListener('click', () => withBusy(button, async () => {
          onUpdate(await api(`/watches/${w.id}/nights`, { method: 'PATCH', body: { nights: [p.night], status: to } }));
        }).catch(showError));
        return button;
      })));
    menu.hidden = false;
  };

  const chips = w.night_plan.map((p) => {
    const status = p.status === 'needed' && p.night < today ? 'past' : p.status;
    const day = parseDay(p.night);
    return h('button', {
      class: `night ${status}`,
      type: 'button',
      disabled: status === 'past',
      'aria-label': `${dayFmt.format(day)}: ${NIGHT_LABELS[status]}`,
      onclick: () => showMenu(p, status),
    },
    h('span', { class: 'night-day' }, weekdayFmt.format(day)),
    h('span', { class: 'night-date' }, monthDayFmt.format(day)),
    h('span', { class: 'night-status' }, NIGHT_LABELS[status] || status));
  });

  const summary = looking.length
    ? `Still looking for ${looking.length} of ${future.length} night${future.length === 1 ? '' : 's'}. Tap a night to update it.`
    : 'Every night is covered. Tap a night to change it.';
  return h('div', { class: 'night-plan' },
    h('p', { class: 'small muted' }, summary),
    h('div', { class: 'night-strip' }, chips),
    menu);
}

const PROVIDERS = { recgov: 'Recreation.gov', reservecalifornia: 'CA State Parks' };
const providerOf = (t) => t.provider || 'recgov';

function providerBadge(t) {
  const key = providerOf(t);
  return h('span', { class: `badge ${key}` }, PROVIDERS[key] || key);
}

function campgroundLabel(t) {
  const base = t.campground_name || `Campground ${t.campground_id}`;
  const name = providerOf(t) === 'recgov' ? base : `${base} (${PROVIDERS[providerOf(t)]})`;
  if (!t.sites.length) return name;
  const list = t.sites.join(', ');
  return `${name}, sites ${list.length > 30 ? `${list.slice(0, 29)}…` : list}`;
  return t.sites.length ? `${name} (${t.sites.length} site${t.sites.length === 1 ? '' : 's'})` : name;
}

function setPage(title, { tab = null, showAdd = false } = {}) {
  document.getElementById('page-title').textContent = title;
  document.getElementById('add-btn').hidden = !showAdd;
  const tabs = document.getElementById('tabs');
  tabs.hidden = false;
  document.body.classList.add('has-tabs');
  for (const a of tabs.querySelectorAll('a')) a.classList.toggle('active', a.dataset.tab === tab);
}

function autoRefresh(fn, ms = 30000) {
  clearInterval(state.refreshTimer);
  state.refreshTimer = setInterval(() => {
    if (document.visibilityState === 'visible') fn().catch(() => {});
  }, ms);
}

async function withBusy(button, fn) {
  const label = button.textContent;
  button.disabled = true;
  button.classList.add('spin');
  try {
    return await fn();
  } finally {
    button.disabled = false;
    button.classList.remove('spin');
    button.textContent = label;
  }
}

// ---------- Login ----------

function showLogin() {
  clearInterval(state.refreshTimer);
  document.getElementById('page-title').textContent = 'CampWatch';
  document.getElementById('tabs').hidden = true;
  document.getElementById('add-btn').hidden = true;
  document.body.classList.remove('has-tabs');

  const password = h('input', { type: 'password', autocomplete: 'current-password', required: true, autofocus: true });
  const submit = h('button', { class: 'btn primary block', type: 'submit' }, 'Log in');
  const form = h('form', {
    onsubmit: async (e) => {
      e.preventDefault();
      try {
        await withBusy(submit, () => api('/login', { method: 'POST', body: { password: password.value } }));
        document.getElementById('toast').hidden = true;
        await start();
      } catch (err) {
        toast(err.message, 'error'); // showError hides 401s, but here a 401 means a wrong password
        password.select();
      }
    },
  }, h('label', {}, 'Password', password), submit);

  view.replaceChildren(h('div', { class: 'login card' },
    h('h2', {}, 'Welcome back'),
    h('p', { class: 'muted small' }, 'Enter the app password set in CAMPWATCH_APP_PASSWORD.'),
    form));
}

// ---------- Watches ----------

async function renderWatches() {
  setPage('Watches', { tab: 'watches', showAdd: true });
  const draw = async () => {
    const watches = await api('/watches');
    if (!watches.length) {
      view.replaceChildren(h('div', { class: 'empty' },
        h('div', { class: 'art', 'aria-hidden': 'true' }, '⛺'),
        h('h2', {}, 'No watches yet'),
        h('p', {}, 'Pick campgrounds and dates, and CampWatch checks Recreation.gov around the clock. You get a phone alert the moment a site opens up.'),
        h('a', { class: 'btn primary', href: '#/new' }, 'Create a watch')));
      return;
    }
    view.replaceChildren(h('div', { class: 'stack' }, watches.map(watchCard)));
  };
  await draw();
  autoRefresh(draw);
}

function watchCard(w) {
  const update = (updated) => card.replaceWith(watchCard(updated));
  const done = w.piecemeal && w.night_plan.length > 0 && nightProgress(w).looking.length === 0;
  const expired = !w.active && w.status_note;
  const pill = w.active
    ? h('span', { class: 'pill ok' }, 'Watching')
    : done ? h('span', { class: 'pill ok' }, 'Done')
      : h('span', { class: 'pill' }, expired ? 'Stopped' : 'Paused');

  const checkBtn = h('button', { class: 'btn small', type: 'button' }, 'Check now');
  checkBtn.addEventListener('click', () => withBusy(checkBtn, async () => {
    const updated = await api(`/watches/${w.id}/check`, { method: 'POST' });
    card.replaceWith(watchCard(updated));
    toast(updated.openings.length ? `${updated.openings.length} open now` : 'Nothing open yet');
  }).catch(showError));

  const toggleBtn = h('button', { class: 'btn small', type: 'button' }, w.active ? 'Pause' : 'Resume');
  toggleBtn.addEventListener('click', () => withBusy(toggleBtn, async () => {
    const updated = await api(`/watches/${w.id}`, { method: 'PATCH', body: { active: !w.active } });
    card.replaceWith(watchCard(updated));
  }).catch(showError));

  const card = h('article', { class: `card${w.active ? '' : ' inactive'}` },
    h('div', { class: 'card-head' }, h('h2', {}, w.name), pill),
    h('p', { class: 'meta' }, w.targets.map(campgroundLabel).join(' · ')),
    h('p', { class: 'meta' }, w.date_ranges.map(rangeLabel).join(' · ')),
    h('p', { class: 'meta small' }, `${stayLabel(w)} · ${w.action === 'cart' ? 'Alert + hold in cart' : 'Alert only'}`),
    w.piecemeal && nightStrip(w, update),
    w.status_note && !done && h('p', { class: 'note' }, w.status_note),
    w.last_error && h('p', { class: 'error-text' }, `Last check had problems: ${w.last_error}`),
    w.openings.length > 0 && h('div', { class: 'openings' },
      h('h3', {}, `${w.openings.length} open now`),
      w.openings.slice(0, 8).map((o) => openingRow(o, update, w)),
      w.openings.length > 8 && h('p', { class: 'small muted' }, `and ${w.openings.length - 8} more`)),
    h('div', { class: 'card-foot' },
      h('span', { class: 'small muted' }, w.last_checked_at ? `Checked ${ago(w.last_checked_at)}` : 'Not checked yet'),
      h('div', { class: 'actions' }, checkBtn, !done && toggleBtn, h('a', { class: 'btn small', href: `#/edit/${w.id}` }, 'Edit'))));
  return card;
}

function openingRow(o, onUpdate, w) {
  const booked = o.cart_status === 'booked';
  let bookedBtn = null;
  if (!booked) {
    bookedBtn = h('button', { class: 'btn small', type: 'button' }, 'I booked it');
    bookedBtn.addEventListener('click', () => withBusy(bookedBtn, async () => {
      const updated = await api(`/openings/${o.id}/booked`, { method: 'POST' });
      onUpdate(updated);
      if (!w.piecemeal) toast('Got it. This watch is paused.');
      else if (nightProgress(updated).looking.length) toast('Got it. Still looking for the other nights.');
      else toast('Got it. That covers every night.');
    }).catch(showError));
  }
  return h('div', { class: 'opening' },
    h('div', {},
      h('strong', {}, `Site ${o.site}`),
      o.loop && h('span', { class: 'muted small' }, ` · ${o.loop}`),
      h('div', { class: 'small' }, `${o.campground_name} · ${rangeLabel(o)}`),
      o.cart_status === 'held' && h('div', { class: 'small' }, '🛒 Held in your cart'),
      booked && h('div', { class: 'small' }, '✓ You booked this'),
      o.cart_status === 'failed' && h('div', { class: 'small error-text' }, `Cart hold failed: ${o.cart_message || ''}`)),
    h('div', { class: 'opening-actions' },
      h('a', { class: 'btn primary small', href: o.book_url, target: '_blank', rel: 'noopener' }, 'Book'),
      bookedBtn));
}

// ---------- Editor ----------

function emptyDraft() {
  return { name: '', targets: [], date_ranges: [{ check_in: '', check_out: '' }], min_nights: null, piecemeal: false, action: 'alert', active: true };
}

async function renderEditor(id) {
  setPage(id ? 'Edit watch' : 'New watch', { tab: 'watches' });
  clearInterval(state.refreshTimer);
  const draft = id ? structuredClone(await api(`/watches/${id}`)) : emptyDraft();
  if (!draft.date_ranges.length) draft.date_ranges.push({ check_in: '', check_out: '' });
  const today = isoDay(new Date());

  // Campgrounds
  const targetsBox = h('div', { class: 'stack' });
  const results = h('div', { class: 'results' });
  const search = h('input', {
    type: 'search',
    placeholder: 'Campground or park name, or a booking link',
    autocomplete: 'off',
    enterkeyhint: 'search',
    'aria-label': 'Search campgrounds',
  });
  let searchTimer;
  let searchSeq = 0;
  search.addEventListener('input', () => {
    clearTimeout(searchTimer);
    const q = search.value.trim();
    if (q.length < 2) { results.replaceChildren(); return; }
    searchTimer = setTimeout(async () => {
      const mine = ++searchSeq;
      results.replaceChildren(h('div', { class: 'muted small pad' }, 'Searching…'));
      try {
        const { results: found, errors } = await api(`/campgrounds/search?q=${encodeURIComponent(q)}`);
        if (mine !== searchSeq) return;
        results.replaceChildren(
          ...found.map((c) => h('button', {
            class: 'result',
            type: 'button',
            onclick: () => {
              if (!draft.targets.some((t) => t.campground_id === c.id && providerOf(t) === c.provider)) {
                draft.targets.push({
                  provider: c.provider,
                  campground_id: c.id,
                  campground_name: c.name,
                  location: [c.parent_name, c.location].filter(Boolean).join(' · '),
                  park_id: c.park_id || '',
                  sites: [],
                });
              }
              search.value = '';
              results.replaceChildren();
              drawTargets();
            },
          },
          h('span', { class: 'result-title' }, c.name, providerBadge(c)),
          h('span', { class: 'sub' }, [c.parent_name, c.location].filter(Boolean).join(' · ') || `ID ${c.id}`))),
          ...errors.map((e) => h('div', { class: 'error-text pad' }, `Search failed for ${e}`)),
          ...(found.length || errors.length ? [] : [h('div', { class: 'muted small pad' }, 'No campgrounds found. Try another name, or paste the campground link.')]));
      } catch (err) {
        if (mine === searchSeq) results.replaceChildren(h('div', { class: 'error-text pad' }, err.message));
      }
    }, 350);
  });

  function drawTargets() {
    targetsBox.replaceChildren(...draft.targets.map((t, i) => targetItem(t, () => {
      draft.targets.splice(i, 1);
      drawTargets();
    })));
  }

  function targetItem(t, onRemove) {
    t.mode = t.mode || (t.sites.length ? 'some' : 'any');
    let stash = [];
    const anyBtn = h('button', { class: 'seg', type: 'button' }, 'Any site');
    const someBtn = h('button', { class: 'seg', type: 'button' }, 'Specific sites');
    const panel = h('div', { class: 'site-picker' });
    const setMode = (mode) => {
      t.mode = mode;
      anyBtn.setAttribute('aria-pressed', String(mode === 'any'));
      someBtn.setAttribute('aria-pressed', String(mode === 'some'));
      panel.hidden = mode === 'any';
      if (mode === 'some' && !panel.childElementCount) buildSitePicker(t, panel, draft);
    };
    anyBtn.addEventListener('click', () => {
      if (t.sites.length) stash = t.sites; // so switching back restores what was typed
      t.sites = [];
      panel.replaceChildren();
      setMode('any');
    });
    someBtn.addEventListener('click', () => {
      if (t.mode === 'some') return;
      if (!t.sites.length) t.sites = stash;
      setMode('some');
      panel.querySelector('input')?.focus();
    });
    setMode(t.mode);
    return h('div', { class: 'target' },
      h('div', { class: 'target-head' },
        h('div', {},
          h('div', { class: 'result-title' }, h('strong', {}, t.campground_name || `Campground ${t.campground_id}`), providerBadge(t)),
          h('div', { class: 'small muted' }, t.location || `ID ${t.campground_id}`)),
        h('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Remove campground', onclick: onRemove }, '×')),
      h('div', { class: 'segmented', role: 'group', 'aria-label': 'Which sites' }, anyBtn, someBtn),
      panel);
  }

  // Dates
  const datesBox = h('div', { class: 'stack' });
  function drawDates() {
    datesBox.replaceChildren(...draft.date_ranges.map((r, i) => {
      const nights = h('span', { class: 'nights' });
      const showNights = () => {
        const n = r.check_in && r.check_out ? nightsBetween(r.check_in, r.check_out) : 0;
        nights.textContent = n > 0 ? `${n} night${n === 1 ? '' : 's'}` : '';
      };
      const checkOut = h('input', { type: 'date', value: r.check_out, min: r.check_in ? addDays(r.check_in, 1) : today, required: true });
      const checkIn = h('input', { type: 'date', value: r.check_in, min: today, required: true });
      checkIn.addEventListener('change', () => {
        r.check_in = checkIn.value;
        if (r.check_in) {
          checkOut.min = addDays(r.check_in, 1);
          if (!r.check_out || r.check_out <= r.check_in) {
            r.check_out = addDays(r.check_in, 1);
            checkOut.value = r.check_out;
          }
        }
        showNights();
      });
      checkOut.addEventListener('change', () => { r.check_out = checkOut.value; showNights(); });
      showNights();
      return h('div', { class: 'date-row' },
        h('label', {}, 'Check in', checkIn),
        h('label', {}, 'Check out', checkOut),
        h('div', { class: 'date-row-foot' },
          nights,
          draft.date_ranges.length > 1 && h('button', {
            class: 'btn small ghost danger',
            type: 'button',
            onclick: () => { draft.date_ranges.splice(i, 1); drawDates(); },
          }, 'Remove')));
    }));
  }

  // Stay length
  const minNights = h('input', { type: 'number', min: 1, max: 30, value: draft.min_nights || 2, inputmode: 'numeric', 'aria-label': 'Minimum nights' });
  const wholeStay = h('input', { type: 'radio', name: 'stay', value: 'whole', checked: !draft.min_nights && !draft.piecemeal });
  const flexStay = h('input', { type: 'radio', name: 'stay', value: 'flex', checked: !!draft.min_nights && !draft.piecemeal });
  const pieceStay = h('input', { type: 'radio', name: 'stay', value: 'piece', checked: !!draft.piecemeal });
  minNights.addEventListener('focus', () => { flexStay.checked = true; });

  // Action
  const cart = state.status?.cart || { available: false, reason: null };
  const alertOnly = h('input', { type: 'radio', name: 'action', value: 'alert', checked: draft.action !== 'cart' });
  const holdCart = h('input', { type: 'radio', name: 'action', value: 'cart', checked: draft.action === 'cart', disabled: !cart.available && draft.action !== 'cart' });

  const name = h('input', { type: 'text', value: draft.name, maxlength: 100, placeholder: 'Optional, e.g. "Yosemite July 4th"' });
  const saveBtn = h('button', { class: 'btn primary', type: 'submit' }, id ? 'Save' : 'Start watching');

  const form = h('form', {
    class: 'editor',
    novalidate: true,
    onsubmit: async (e) => {
      e.preventDefault();
      const ranges = draft.date_ranges.filter((r) => r.check_in || r.check_out);
      if (!draft.targets.length) return toast('Add at least one campground.', 'error');
      for (const t of draft.targets) {
        const label = t.campground_name || `campground ${t.campground_id}`;
        if (t.mode === 'some' && !t.sites.length) {
          return toast(`Enter sites for ${label}, or choose “Any site”.`, 'error');
        }
        const backwards = backwardsRanges(t.sites);
        if (backwards.length) return toast(`Site range ${backwards[0]} is backwards (${label}).`, 'error');
      }
      if (!ranges.length) return toast('Add at least one set of dates.', 'error');
      if (ranges.some((r) => !r.check_in || !r.check_out || r.check_out <= r.check_in)) {
        return toast('Each check-out date must be after its check-in date.', 'error');
      }
      const body = {
        name: name.value.trim(),
        targets: draft.targets.map((t) => ({
          provider: providerOf(t),
          campground_id: t.campground_id,
          campground_name: t.campground_name,
          location: t.location || '',
          park_id: t.park_id || '',
          sites: t.mode === 'some' ? t.sites : [],
        })),
        date_ranges: ranges.map(({ check_in, check_out }) => ({ check_in, check_out })),
        min_nights: flexStay.checked ? Number(minNights.value) || 1 : null,
        piecemeal: pieceStay.checked,
        action: holdCart.checked ? 'cart' : 'alert',
        active: id ? draft.active : true,
      };
      try {
        await withBusy(saveBtn, () => api(id ? `/watches/${id}` : '/watches', { method: id ? 'PUT' : 'POST', body }));
        toast(id ? 'Saved' : 'Watching. You’ll get an alert when a site opens up.');
        location.hash = '#/';
      } catch (err) {
        showError(err);
      }
    },
  },
  h('fieldset', {},
    h('legend', {}, 'Campgrounds'),
    targetsBox,
    search,
    results,
    h('p', { class: 'hint' }, 'Searches Recreation.gov and ReserveCalifornia (CA State Parks). Add as many as you like; any of them will do.')),
  h('fieldset', {},
    h('legend', {}, 'Dates'),
    datesBox,
    h('div', {}, h('button', {
      class: 'btn small',
      type: 'button',
      onclick: () => { draft.date_ranges.push({ check_in: '', check_out: '' }); drawDates(); },
    }, '+ Add more dates')),
    h('label', { class: 'choice' }, wholeStay, h('span', {}, 'The whole stay, at one site')),
    h('label', { class: 'choice' }, flexStay,
      h('span', {},
        h('span', { class: 'inline' }, 'At least', minNights, 'nights in a row'),
        h('span', { class: 'hint', style: 'display:block' }, 'Anywhere within the dates above, at one site'))),
    h('label', { class: 'choice' }, pieceStay,
      h('span', {},
        'Night by night: grab any open nights',
        h('span', { class: 'hint', style: 'display:block' },
          'Takes nights as they open up, even one at a time, and keeps looking for the rest until every night is booked or skipped. Sites can differ from night to night; it prefers extending a site you already have.')))),
  h('fieldset', {},
    h('legend', {}, 'When a site opens up'),
    h('label', { class: 'choice' }, alertOnly, h('span', {}, 'Alert me')),
    h('label', { class: 'choice' }, holdCart,
      h('span', {},
        'Alert me and hold the site in my Recreation.gov cart',
        h('span', { class: 'hint', style: 'display:block' },
          cart.available
            ? 'Experimental, Recreation.gov sites only (CA State Parks sites still alert). You’ll have about 15 minutes to sign in and pay. Other watches pause after a hold; night-by-night watches keep looking for the remaining nights.'
            : `Not set up yet. ${cart.reason || ''}`)))),
  h('fieldset', {},
    h('legend', {}, 'Name'),
    name),
  h('div', { class: 'form-actions' },
    id && h('button', {
      class: 'btn danger',
      type: 'button',
      onclick: async () => {
        if (!confirm('Delete this watch?')) return;
        try {
          await api(`/watches/${id}`, { method: 'DELETE' });
          location.hash = '#/';
        } catch (err) { showError(err); }
      },
    }, 'Delete'),
    h('div', { class: 'right' }, h('a', { class: 'btn ghost', href: '#/' }, 'Cancel'), saveBtn)));

  view.replaceChildren(form);
  drawTargets();
  drawDates();
}

// Site entries are names ("12", "A5") or ranges ("10-25", "A1-A10"). This mirrors
// site_matcher in campwatch/availability.py, which does the real matching on the server.
const compactSite = (s) => s.toUpperCase().replace(/\s+/g, '');
const normalizeSite = (s) => compactSite(s).replace(/\d+/g, (d) => String(Number(d)));

function parseSiteRange(entry) {
  const m = compactSite(entry).match(/^([A-Z]*)(\d+)-([A-Z]*)(\d+)$/);
  if (!m || (m[3] && m[3] !== m[1])) return null;
  return { prefix: m[1], start: Number(m[2]), end: Number(m[4]) };
}

function siteMatcher(entries) {
  if (!entries.length) return null;
  const exact = new Set(entries.map(normalizeSite));
  const ranges = entries.map(parseSiteRange).filter(Boolean);
  return (name) => {
    if (exact.has(normalizeSite(name))) return true;
    const m = compactSite(name).match(/^([A-Z]*)(\d+)[A-Z]*$/);
    if (!m) return false;
    const n = Number(m[2]);
    return ranges.some((r) => r.prefix === m[1] && n >= Math.min(r.start, r.end) && n <= Math.max(r.start, r.end));
  };
}

function parseSiteText(text) {
  return [...new Set(text.split(/[,;\n]+/).map((e) => e.trim().replace(/\s*-\s*/g, '-')).filter(Boolean))];
}

function backwardsRanges(entries) {
  return entries.filter((e) => {
    const r = parseSiteRange(e);
    return r && r.start > r.end;
  });
}

async function loadSites(t, draft) {
  const firstDate = draft.date_ranges.map((r) => r.check_in).filter(Boolean).sort()[0];
  const month = (firstDate || isoDay(new Date())).slice(0, 7);
  const cacheKey = `${providerOf(t)}:${t.campground_id}:${month}`;
  if (!state.siteCache.has(cacheKey)) {
    const sites = await api(`/campgrounds/${t.campground_id}/sites?month=${month}-01&provider=${providerOf(t)}`);
    state.siteCache.set(cacheKey, sites);
  }
  return state.siteCache.get(cacheKey);
}

function buildSitePicker(t, panel, draft) {
  const input = h('input', {
    type: 'text',
    value: t.sites.join(', '),
    placeholder: 'e.g. 12, 15-22, A1-A10',
    autocomplete: 'off',
    autocapitalize: 'characters',
    spellcheck: 'false',
  });
  const status = h('p', { class: 'hint' });
  const chipsBox = h('div', { class: 'site-list' }, h('div', { class: 'muted small' }, 'Loading the site list…'));
  let sites = null;
  let loadError = null;
  let redrawChips = () => {};

  const refresh = () => {
    const backwards = backwardsRanges(t.sites);
    status.classList.toggle('error-text', backwards.length > 0);
    if (backwards.length) {
      status.textContent = `${backwards.join(', ')}: write ranges low to high, like 12-20.`;
    } else if (!t.sites.length) {
      status.textContent = 'Type site numbers or ranges, separated by commas, or tap sites below.';
    } else if (sites) {
      const matches = siteMatcher(t.sites);
      const count = sites.filter((s) => matches(s.site)).length;
      const unmatched = t.sites.filter((e) => !sites.some((s) => siteMatcher([e])(s.site)));
      status.textContent = `Watching ${count} of ${sites.length} sites.`
        + (unmatched.length ? ` Nothing matches ${unmatched.join(', ')}.` : '');
    } else {
      status.textContent = loadError ? 'Couldn’t check these against the site list.' : '';
    }
    redrawChips();
  };

  input.addEventListener('input', () => { t.sites = parseSiteText(input.value); refresh(); });
  input.addEventListener('blur', () => { input.value = t.sites.join(', '); });

  panel.replaceChildren(h('label', {}, 'Sites or ranges', input), status, chipsBox);
  refresh();

  loadSites(t, draft).then((list) => {
    sites = list;
    const filter = h('input', { type: 'search', placeholder: 'Filter sites or loops', autocomplete: 'off', 'aria-label': 'Filter sites' });
    const chips = h('div', { class: 'chips' });
    redrawChips = () => {
      const matches = siteMatcher(t.sites);
      const q = filter.value.trim().toLowerCase();
      const items = [];
      let loop = null;
      for (const s of sites) {
        if (q && !s.site.toLowerCase().includes(q) && !s.loop.toLowerCase().includes(q)) continue;
        if (s.loop !== loop) {
          loop = s.loop;
          if (loop) items.push(h('div', { class: 'chip-group' }, loop));
        }
        const on = !!matches && matches(s.site);
        items.push(h('button', {
          class: `chip${on ? ' on' : ''}`,
          type: 'button',
          'aria-pressed': String(on),
          title: s.campsite_type,
          onclick: () => {
            const exact = t.sites.findIndex((e) => normalizeSite(e) === normalizeSite(s.site));
            if (exact >= 0) {
              t.sites = t.sites.filter((_, i) => i !== exact);
            } else if (on) {
              toast(`Site ${s.site} is part of a range. Edit the range to leave it out.`);
              return;
            } else {
              t.sites = [...t.sites, s.site];
            }
            input.value = t.sites.join(', ');
            refresh();
          },
        }, s.site));
      }
      chips.replaceChildren(...(items.length ? items : [h('div', { class: 'muted small' }, 'No matching sites')]));
    };
    filter.addEventListener('input', redrawChips);
    chipsBox.replaceChildren(h('p', { class: 'hint' }, 'Or tap sites to add or remove them:'), filter, chips);
    refresh();
  }).catch((err) => {
    loadError = err;
    chipsBox.replaceChildren(h('div', { class: 'muted small' }, `Couldn’t load the site list (${err.message}). You can still type sites.`));
    refresh();
  });
}

// ---------- Activity ----------

async function renderActivity() {
  setPage('Activity', { tab: 'activity' });
  const draw = async () => {
    const openings = await api('/openings?limit=200');
    if (!openings.length) {
      view.replaceChildren(h('div', { class: 'empty' },
        h('div', { class: 'art', 'aria-hidden': 'true' }, '🔭'),
        h('h2', {}, 'Nothing found yet'),
        h('p', {}, 'Every site that opens up for one of your watches shows up here, including ones that got booked again before you could grab them.')));
      return;
    }
    view.replaceChildren(h('div', { class: 'stack' }, openings.map((o) => {
      const open = !o.gone_at;
      const pill = o.cart_status === 'booked' ? h('span', { class: 'pill ok' }, 'Booked')
        : o.cart_status === 'held' ? h('span', { class: 'pill ok' }, 'In cart')
        : open ? h('span', { class: 'pill ok' }, 'Open')
          : h('span', { class: 'pill' }, 'Gone');
      return h('article', { class: `card list-item${open ? '' : ' gone'}` },
        h('div', {},
          h('div', {}, h('strong', {}, `Site ${o.site}`), ' · ', o.campground_name),
          h('div', { class: 'small' }, rangeLabel(o)),
          h('div', { class: 'small muted' },
            `${o.watch_name} · found ${ago(o.first_seen_at)}${o.gone_at ? `, gone ${ago(o.gone_at)}` : ''}`),
          o.cart_status === 'failed' && h('div', { class: 'small error-text' }, `Cart hold failed: ${o.cart_message || ''}`)),
        h('div', { class: 'actions' }, pill, open && h('a', { class: 'btn primary small', href: o.book_url, target: '_blank', rel: 'noopener' }, 'Book')));
    })));
  };
  await draw();
  autoRefresh(draw);
}

// ---------- Settings ----------

async function renderSettings() {
  setPage('Settings', { tab: 'settings' });
  const draw = async () => {
    const s = await api('/status');
    state.status = s;
    const channelNames = { ntfy: 'ntfy push', pushover: 'Pushover push', email: 'Email' };

    const testBtn = h('button', { class: 'btn small', type: 'button', disabled: !s.channels.length }, 'Send a test alert');
    testBtn.addEventListener('click', () => withBusy(testBtn, async () => {
      const { results } = await api('/notifications/test', { method: 'POST' });
      const failed = Object.entries(results).filter(([, err]) => err);
      if (failed.length) toast(failed.map(([ch, err]) => `${ch}: ${err}`).join('; '), 'error');
      else toast('Sent. Check your phone.');
    }).catch(showError));

    const last = s.last_cycle;
    view.replaceChildren(h('div', { class: 'stack' },
      h('section', { class: 'card' },
        h('h2', {}, 'Alerts'),
        s.channels.length
          ? h('p', {}, 'Sending to: ', s.channels.map((c) => channelNames[c] || c).join(', '))
          : h('p', { class: 'note' }, 'No alert channel is set up, so openings only show in this app. Set CAMPWATCH_NTFY_TOPIC (easiest), Pushover or email settings on the server. See the README.'),
        h('div', {}, testBtn)),
      h('section', { class: 'card' },
        h('h2', {}, 'Checker'),
        h('dl', { class: 'kv' },
          h('dt', {}, 'Checks'), h('dd', {}, s.scheduler_running ? `Every ${Math.round(s.poll_interval_seconds / 6) / 10} min` : 'Background checks are off'),
          h('dt', {}, 'Last check'), h('dd', {}, last ? `${ago(last.started_at)}: ${last.watches_checked} watch${last.watches_checked === 1 ? '' : 'es'}, ${last.requests} request${last.requests === 1 ? '' : 's'}, ${last.new_openings} new` : 'Not yet'),
          s.next_cycle_at && h('dt', {}, 'Next check'), s.next_cycle_at && h('dd', {}, inSeconds(s.next_cycle_at)),
          s.backoff_seconds > 0 && h('dt', {}, 'Slowed down'), s.backoff_seconds > 0 && h('dd', {}, `Recreation.gov asked us to back off. Waiting an extra ${Math.round(s.backoff_seconds / 60)} min.`)),
        last?.errors?.length > 0 && h('p', { class: 'error-text' }, last.errors.join('; '))),
      h('section', { class: 'card' },
        h('h2', {}, 'Cart holds'),
        h('p', { class: s.cart.available ? '' : 'muted' },
          s.cart.available
            ? 'Ready. Watches set to “hold in cart” will add the site to your Recreation.gov cart.'
            : `Off. ${s.cart.reason || ''}`)),
      h('section', { class: 'card' },
        h('h2', {}, 'Install on your iPhone'),
        h('p', { class: 'small' }, 'Open this page in Safari, tap the Share button, then “Add to Home Screen”. CampWatch opens full screen like an app.'),
        h('p', { class: 'small muted' }, 'For alerts, install the free ntfy app and subscribe to your topic.')),
      s.auth_required && h('section', { class: 'card' },
        h('div', { class: 'card-head' },
          h('h2', {}, 'Account'),
          h('button', {
            class: 'btn small',
            type: 'button',
            onclick: async () => { await api('/logout', { method: 'POST' }); showLogin(); },
          }, 'Log out'))),
      h('p', { class: 'small muted', style: 'text-align:center' }, `CampWatch ${s.version}`)));
  };
  await draw();
  autoRefresh(draw);
}

// ---------- Routing ----------

const routes = [
  [/^#?\/?$/, renderWatches],
  [/^#\/new$/, () => renderEditor(null)],
  [/^#\/edit\/(\d+)$/, (m) => renderEditor(Number(m[1]))],
  [/^#\/activity$/, renderActivity],
  [/^#\/settings$/, renderSettings],
];

async function route() {
  clearInterval(state.refreshTimer);
  const hash = location.hash || '#/';
  for (const [pattern, render] of routes) {
    const match = hash.match(pattern);
    if (match) {
      try {
        await render(match);
      } catch (err) {
        showError(err);
      }
      window.scrollTo(0, 0);
      return;
    }
  }
  location.hash = '#/';
}

async function start() {
  try {
    const session = await api('/session');
    if (!session.logged_in) return showLogin();
    state.status = await api('/status');
  } catch (err) {
    if (err.status === 401) return;
    showError(err);
  }
  await route();
}

window.addEventListener('hashchange', route);
start();
