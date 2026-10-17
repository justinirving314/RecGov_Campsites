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
  return w.min_nights ? `Any ${w.min_nights}+ nights in a row` : 'Whole stay at one site';
}

function campgroundLabel(t) {
  const name = t.campground_name || `Campground ${t.campground_id}`;
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
  const expired = !w.active && w.status_note;
  const pill = w.active
    ? h('span', { class: 'pill ok' }, 'Watching')
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
    w.status_note && h('p', { class: 'note' }, w.status_note),
    w.last_error && h('p', { class: 'error-text' }, `Last check had problems: ${w.last_error}`),
    w.openings.length > 0 && h('div', { class: 'openings' },
      h('h3', {}, `${w.openings.length} open now`),
      w.openings.slice(0, 8).map(openingRow),
      w.openings.length > 8 && h('p', { class: 'small muted' }, `and ${w.openings.length - 8} more`)),
    h('div', { class: 'card-foot' },
      h('span', { class: 'small muted' }, w.last_checked_at ? `Checked ${ago(w.last_checked_at)}` : 'Not checked yet'),
      h('div', { class: 'actions' }, checkBtn, toggleBtn, h('a', { class: 'btn small', href: `#/edit/${w.id}` }, 'Edit'))));
  return card;
}

function openingRow(o) {
  return h('div', { class: 'opening' },
    h('div', {},
      h('strong', {}, `Site ${o.site}`),
      o.loop && h('span', { class: 'muted small' }, ` · ${o.loop}`),
      h('div', { class: 'small' }, `${o.campground_name} · ${rangeLabel(o)}`),
      o.cart_status === 'held' && h('div', { class: 'small' }, '🛒 Held in your cart'),
      o.cart_status === 'failed' && h('div', { class: 'small error-text' }, `Cart hold failed: ${o.cart_message || ''}`)),
    h('a', { class: 'btn primary small', href: o.book_url, target: '_blank', rel: 'noopener' }, 'Book'));
}

// ---------- Editor ----------

function emptyDraft() {
  return { name: '', targets: [], date_ranges: [{ check_in: '', check_out: '' }], min_nights: null, action: 'alert', active: true };
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
    placeholder: 'Campground name or recreation.gov link',
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
        const found = await api(`/campgrounds/search?q=${encodeURIComponent(q)}`);
        if (mine !== searchSeq) return;
        results.replaceChildren(...(found.length
          ? found.map((c) => h('button', {
            class: 'result',
            type: 'button',
            onclick: () => {
              if (!draft.targets.some((t) => t.campground_id === c.id)) {
                draft.targets.push({ campground_id: c.id, campground_name: c.name, location: [c.parent_name, c.location].filter(Boolean).join(' · '), sites: [] });
              }
              search.value = '';
              results.replaceChildren();
              drawTargets();
            },
          }, c.name, h('span', { class: 'sub' }, [c.parent_name, c.location].filter(Boolean).join(' · ') || `ID ${c.id}`)))
          : [h('div', { class: 'muted small pad' }, 'No campgrounds found. Try another name, or paste the campground link.')]));
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
    const summary = h('button', { class: 'btn small', type: 'button' });
    const picker = h('div', { class: 'site-picker', hidden: true });
    const updateSummary = () => {
      summary.textContent = t.sites.length
        ? `Only site${t.sites.length === 1 ? '' : 's'} ${t.sites.slice(0, 6).join(', ')}${t.sites.length > 6 ? '…' : ''} ▾`
        : 'Any site ▾';
    };
    updateSummary();
    summary.addEventListener('click', async () => {
      if (!picker.hidden) { picker.hidden = true; return; }
      picker.hidden = false;
      if (!picker.childElementCount) await fillPicker(t, picker, updateSummary, draft);
    });
    return h('div', { class: 'target' },
      h('div', { class: 'target-head' },
        h('div', {},
          h('strong', {}, t.campground_name || `Campground ${t.campground_id}`),
          h('div', { class: 'small muted' }, t.location || `ID ${t.campground_id}`)),
        h('button', { class: 'icon-btn', type: 'button', 'aria-label': 'Remove campground', onclick: onRemove }, '×')),
      h('div', {}, summary),
      picker);
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
  const wholeStay = h('input', { type: 'radio', name: 'stay', value: 'whole', checked: !draft.min_nights });
  const flexStay = h('input', { type: 'radio', name: 'stay', value: 'flex', checked: !!draft.min_nights });
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
      if (!ranges.length) return toast('Add at least one set of dates.', 'error');
      if (ranges.some((r) => !r.check_in || !r.check_out || r.check_out <= r.check_in)) {
        return toast('Each check-out date must be after its check-in date.', 'error');
      }
      const body = {
        name: name.value.trim(),
        targets: draft.targets.map(({ campground_id, campground_name, location, sites }) => ({ campground_id, campground_name, location: location || '', sites })),
        date_ranges: ranges.map(({ check_in, check_out }) => ({ check_in, check_out })),
        min_nights: flexStay.checked ? Number(minNights.value) || 1 : null,
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
    h('p', { class: 'hint' }, 'Add as many as you like. Any of them will do. To watch only certain sites, tap “Any site”.')),
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
        h('span', { class: 'hint', style: 'display:block' }, 'Anywhere within the dates above, at one site')))),
  h('fieldset', {},
    h('legend', {}, 'When a site opens up'),
    h('label', { class: 'choice' }, alertOnly, h('span', {}, 'Alert me')),
    h('label', { class: 'choice' }, holdCart,
      h('span', {},
        'Alert me and hold the site in my Recreation.gov cart',
        h('span', { class: 'hint', style: 'display:block' },
          cart.available
            ? 'Experimental. You’ll have about 15 minutes to sign in and pay. The watch pauses after a successful hold.'
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

async function fillPicker(t, picker, onChange, draft) {
  picker.replaceChildren(h('div', { class: 'muted small' }, 'Loading sites…'));
  const firstDate = draft.date_ranges.map((r) => r.check_in).filter(Boolean).sort()[0];
  const month = firstDate || isoDay(new Date());
  const cacheKey = `${t.campground_id}:${month.slice(0, 7)}`;
  let sites = state.siteCache.get(cacheKey);
  try {
    if (!sites) {
      sites = await api(`/campgrounds/${t.campground_id}/sites?month=${month.slice(0, 7)}-01`);
      state.siteCache.set(cacheKey, sites);
    }
  } catch (err) {
    picker.replaceChildren(h('div', { class: 'error-text' }, `Couldn't load sites: ${err.message}`));
    return;
  }

  const chosen = new Set(t.sites);
  const chips = h('div', { class: 'chips' });
  const filter = h('input', { type: 'search', placeholder: 'Filter sites or loops', autocomplete: 'off', 'aria-label': 'Filter sites' });
  const sync = () => { t.sites = [...chosen]; onChange(); };

  const draw = () => {
    const q = filter.value.trim().toLowerCase();
    const items = [];
    let loop = null;
    for (const s of sites) {
      if (q && !s.site.toLowerCase().includes(q) && !s.loop.toLowerCase().includes(q)) continue;
      if (s.loop !== loop) {
        loop = s.loop;
        if (loop) items.push(h('div', { class: 'chip-group' }, loop));
      }
      const chip = h('button', { class: `chip${chosen.has(s.site) ? ' on' : ''}`, type: 'button', 'aria-pressed': String(chosen.has(s.site)), title: s.campsite_type }, s.site);
      chip.addEventListener('click', () => {
        if (chosen.has(s.site)) chosen.delete(s.site); else chosen.add(s.site);
        chip.classList.toggle('on', chosen.has(s.site));
        chip.setAttribute('aria-pressed', String(chosen.has(s.site)));
        sync();
      });
      items.push(chip);
    }
    chips.replaceChildren(...(items.length ? items : [h('div', { class: 'muted small' }, 'No matching sites')]));
  };
  filter.addEventListener('input', draw);
  draw();

  picker.replaceChildren(
    h('p', { class: 'hint' }, `${sites.length} sites. Tap to pick the ones you want, or leave all off for any site.`),
    filter,
    chips,
    h('div', {}, h('button', {
      class: 'btn small ghost',
      type: 'button',
      onclick: () => { chosen.clear(); sync(); draw(); },
    }, 'Clear (any site)')));
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
      const pill = o.cart_status === 'held' ? h('span', { class: 'pill ok' }, 'In cart')
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
