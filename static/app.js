'use strict';
(async function startApp() {
  const READ_TIMEOUT_MS = 15000;
  const WRITE_TIMEOUT_MS = 60000;
  function loadTelegramSdk() {
    if (window.Telegram && window.Telegram.WebApp) return Promise.resolve(window.Telegram.WebApp);
    return new Promise((resolve) => {
      const script = document.createElement('script');
      let timeout;
      const finish = () => {
        clearTimeout(timeout);
        script.onload = null;
        script.onerror = null;
        resolve((window.Telegram && window.Telegram.WebApp) || null);
      };
      script.src = 'https://telegram.org/js/telegram-web-app.js';
      script.onload = finish;
      script.onerror = () => {
        script.remove();
        finish();
      };
      timeout = setTimeout(() => {
        script.remove();
        finish();
      }, READ_TIMEOUT_MS);
      document.head.append(script);
    });
  }
  const telegramPromise = loadTelegramSdk();
  const translationController = new AbortController();
  const translationTimeout = setTimeout(() => translationController.abort(), READ_TIMEOUT_MS);
  let messages;
  try {
    const response = await fetch('/assets/ru.json?v=2', {
      cache: 'no-cache',
      signal: translationController.signal,
    });
    if (!response.ok) throw new Error('Could not load translations.');
    messages = await response.json();
  } finally {
    clearTimeout(translationTimeout);
  }
  const translate = (key) => {
    if (!Object.prototype.hasOwnProperty.call(messages, key))
      throw new Error(`Missing translation: ${key}`);
    return messages[key];
  };
  const root = document.getElementById('app');
  const toastEl = document.getElementById('toast');
  const state = {
    me: null,
    lots: [],
    demo: false,
    trial: false,
    role: sessionStorage.getItem('demoRole') || 'guest',
    end: 0,
    adminView: null,
    adminLotId: null,
    applications: null,
    adminLotSignature: null,
    myBidsSignature: null,
  };
  const tg = await telegramPromise;
  if (tg) {
    tg.ready();
    tg.expand();
  }
  const esc = (s) =>
    String(s ?? '').replace(
      /[&<>"']/g,
      (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c],
    );
  const money = (n) => `${Number(n).toLocaleString('ru-RU')} ₽`;
  const decl = (n) =>
    n % 10 === 1 && n % 100 !== 11
      ? translate('lot_count_one')
      : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14)
        ? translate('lot_count_few')
        : translate('lot_count_many');
  const bidsWord = (n) =>
    n % 10 === 1 && n % 100 !== 11
      ? translate('bid_count_one')
      : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14)
        ? translate('bid_count_few')
        : translate('bid_count_many');
  let toastTimer;
  let pendingWrites = 0,
    mutationVersion = 0,
    loading = 0,
    viewVersion = 0,
    ready = false,
    detailRenderedSignature = null;
  function toast(message) {
    toastEl.textContent = message;
    toastEl.classList.add('visible');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove('visible'), 4000);
  }
  async function api(path, options = {}) {
    const isWrite = options.method && options.method.toUpperCase() !== 'GET';
    if (isWrite) {
      pendingWrites++;
      mutationVersion++;
    }
    const { signal: parentSignal, ...requestOptions } = options;
    const controller = new AbortController();
    const abort = () => controller.abort();
    if (parentSignal) {
      if (parentSignal.aborted) abort();
      else parentSignal.addEventListener('abort', abort, { once: true });
    }
    const timeout = setTimeout(abort, isWrite ? WRITE_TIMEOUT_MS : READ_TIMEOUT_MS);
    try {
      const headers = { ...(requestOptions.headers || {}) };
      if (tg && tg.initData) headers['X-Telegram-Init-Data'] = tg.initData;
      if (state.demo) headers['X-Demo-User'] = state.role;
      if (requestOptions.body && !(requestOptions.body instanceof FormData)) {
        headers['Content-Type'] = 'application/json';
        requestOptions.body = JSON.stringify(requestOptions.body);
      }
      let response, data;
      try {
        response = await fetch(path, { ...requestOptions, headers, signal: controller.signal });
        data = await response.json();
      } catch (error) {
        const key = isWrite
          ? 'error_request_timeout_write'
          : error.name === 'AbortError'
            ? 'error_request_timeout_read'
            : response
              ? 'error_response_unreadable'
              : 'error_connection_failed';
        const failure = new Error(translate(key));
        failure.outcomeUnknown = Boolean(isWrite);
        throw failure;
      }
      if (!response.ok) throw new Error(data.error || translate('error_request_failed'));
      return data;
    } finally {
      clearTimeout(timeout);
      if (parentSignal) parentSignal.removeEventListener('abort', abort);
      if (isWrite) pendingWrites--;
    }
  }
  const post = (path, body = {}) => api(path, { method: 'POST', body });
  // Enter in a single-line field finishes editing; only an explicit button submits.
  document.addEventListener('keydown', (event) => {
    const target = event.target;
    if (
      event.key === 'Enter' &&
      !event.isComposing &&
      target instanceof HTMLInputElement &&
      ['text', 'number', 'email', 'tel', 'url', 'search'].includes(target.type)
    ) {
      event.preventDefault();
      target.blur();
    }
  });
  const authHint = () =>
    `<div class="panel"><h3>${translate('auth_title')}</h3><p>${translate('auth_description')}</p></div>`;
  function nav(which) {
    document
      .querySelectorAll('[data-nav]')
      .forEach((a) => a.classList.toggle('active', a.dataset.nav === which));
  }
  function demoBar() {
    const old = document.querySelector('.demo-strip');
    if (old) old.remove();
    if (!state.demo) return;
    const el = document.createElement('div');
    el.className = 'demo-strip';
    el.textContent = translate('demo_banner');
    const s = document.createElement('select');
    s.setAttribute('aria-label', translate('demo_role_label'));
    for (const [v, t] of [
      ['guest', translate('demo_role_guest')],
      ['friend', translate('demo_role_first_participant')],
      ['other', translate('demo_role_second_participant')],
      ['admin', translate('demo_role_admin')],
    ]) {
      const o = document.createElement('option');
      o.value = v;
      o.textContent = t;
      s.append(o);
    }
    s.value = state.role;
    s.onchange = () => {
      state.role = s.value;
      sessionStorage.setItem('demoRole', s.value);
      location.hash = '#/';
      load();
    };
    el.append(s);
    document.body.prepend(el);
  }
  function trialBar() {
    const old = document.querySelector('.trial-strip');
    if (old) old.remove();
    if (!state.trial || state.demo) return;
    const el = document.createElement('div');
    el.className = 'trial-strip';
    el.textContent = translate('integration_banner');
    document.body.prepend(el);
  }
  function telegramBar() {
    const old = document.querySelector('.telegram-strip');
    if (old) old.remove();
    if (tg || state.demo) return;
    const el = document.createElement('div');
    el.className = 'telegram-strip';
    el.setAttribute('role', 'status');
    const message = document.createElement('span');
    message.textContent = translate('ui_telegram_unavailable');
    const retry = document.createElement('button');
    retry.type = 'button';
    retry.className = 'button secondary';
    retry.textContent = translate('ui_telegram_retry');
    retry.onclick = () => location.reload();
    el.append(message, retry);
    document.body.prepend(el);
  }
  async function load() {
    loading++;
    try {
      const me = await api('/api/me');
      state.demo = me.demo;
      state.trial = me.trial;
      if (state.demo) {
        const again = await api('/api/me');
        state.me = again.user;
      } else state.me = me.user;
      state.end = me.end;
      document.getElementById('admin-link').hidden = !(state.me && state.me.admin);
      demoBar();
      trialBar();
      telegramBar();
      const data = await api('/api/lots');
      state.lots = data.lots;
      ready = true;
      render();
    } catch (e) {
      root.innerHTML =
        `<div class="page"><div class="empty"><strong>${translate('catalog_load_failed')}</strong><p>` +
        esc(e.message) +
        '</p></div></div>';
    } finally {
      loading--;
    }
  }
  function card(lot) {
    const image = lot.photos.length
      ? `<img src="${esc(lot.photos[0])}" alt="${esc(lot.title)}">`
      : `<span class="placeholder-art">${translate('photo_missing')}</span>`;
    const status =
      lot.status === 'cancelled'
        ? translate('lot_badge_cancelled')
        : lot.status === 'ended'
          ? translate('lot_badge_ended')
          : lot.condition;
    return `<a class="card" href="#/lot/${lot.id}"><div class="card-image">${image}${status ? `<span class="photo-label">${esc(status)}</span>` : ''}</div><div class="card-bottom"><h3 class="card-title">${esc(lot.title)}</h3><div class="card-price-row"><div><span class="small-label">${lot.price === null ? translate('price_start_label') : translate('price_current_label')}</span><span class="price">${money(lot.price === null ? lot.start_price : lot.price)}</span></div><span class="card-arrow" aria-hidden="true">↗</span></div><p class="card-meta">${lot.count} ${bidsWord(lot.count)} <span aria-hidden="true">·</span>${translate('bid_step_prefix')}${money(lot.step)}</p></div></a>`;
  }
  function catalog() {
    nav('catalog');
    const visible = state.lots;
    root.innerHTML = `<section class="catalog-heading"><div><p class="catalog-kicker">${translate('catalog_kicker')}<span>/</span> ${visible.length} ${decl(visible.length)}</p><h1>${translate('catalog_title')}</h1><p>${translate('catalog_description')}</p></div><a class="rules-link" href="#/about">${translate('nav_rules')}<span aria-hidden="true">↗</span></a></section>
 <section class="auction-info" aria-label="${translate('auction_conditions_label')}"><div><span class="info-label">${translate('auction_end_label')}</span><strong>${translate('auction_end_summary')}<small>UTC+7</small></strong></div><div><span class="info-label">${translate('pickup_label')}</span><strong>${translate('pickup_value')}</strong></div><span class="auction-info-note">${translate('auction_no_extension')}</span></section>
 <div class="section-head"><h2>${translate('catalog_items_title')}</h2><span class="count">${visible.length} ${decl(visible.length)}</span></div>
 ${visible.length ? `<div class="grid">${visible.map(card).join('')}</div>` : `<div class="empty"><strong>${translate('catalog_empty_title')}</strong><p>${translate('catalog_empty_description')}</p></div>`}<footer class="footnote"><span>${translate('footer_title')}</span><a href="#/about">${translate('footer_rules')}</a></footer>`;
  }
  function refreshCatalog() {
    const active = document.activeElement;
    const focusedCard = active && root.contains(active) ? active.closest('.card') : null;
    const focusedHref = focusedCard && focusedCard.getAttribute('href');
    const scrollX = window.scrollX,
      scrollY = window.scrollY;
    catalog();
    if (focusedHref) {
      const restored = Array.from(root.querySelectorAll('.card')).find(
        (card) => card.getAttribute('href') === focusedHref,
      );
      if (restored) restored.focus({ preventScroll: true });
    }
    window.scrollTo(scrollX, scrollY);
  }
  function detail(lot) {
    detailRenderedSignature = JSON.stringify([
      lot,
      state.me && state.me.status,
      Boolean(state.me && state.me.admin),
    ]);
    nav('catalog');
    const price = lot.price === null ? lot.start_price : lot.price;
    const minimum = lot.price === null ? lot.start_price : lot.price + lot.step;
    const images = lot.photos.length
      ? `<img id="main-photo" src="${esc(lot.photos[0])}" alt="${esc(lot.title)}">`
      : `<span class="placeholder-art">${translate('photo_missing')}</span>`;
    const thumbs =
      lot.photos.length > 1
        ? `<div class="thumbs">${lot.photos.map((p, i) => `<button data-photo="${esc(p)}" aria-label="${translate('photo_number_prefix')}${i + 1}"><img src="${esc(p)}" alt=""></button>`).join('')}</div>`
        : '';
    let action = '';
    if (lot.status !== 'active')
      action = `<div class="panel"><strong>${lot.status === 'cancelled' ? translate('lot_cancelled_title') : lot.leading ? translate('lot_won_title') : translate('lot_ended_title')}</strong>${lot.status === 'ended' && lot.leading ? `<p>${translate('lot_winner_contact')}</p>` : ''}</div>`;
    else if (state.me && state.me.admin && state.me.status !== 'approved') action = '';
    else if (!state.me) action = authHint();
    else if (state.me.status === 'pending')
      action = `<div class="panel"><h3>${translate('application_pending_title')}</h3><p>${translate('application_pending_description')}</p></div>`;
    else if (state.me.status === 'rejected')
      action = `<div class="panel"><h3>${translate('application_rejected_title')}</h3><p>${translate('application_rejected_description')}</p></div>`;
    else if (state.me.status !== 'approved')
      action = `<div class="panel application-panel"><h3>${translate('application_title')}</h3><p>${translate('application_description')}</p><form id="application-form"><label class="field"><span>${translate('applicant_given_name')}</span><input name="given_name" autocomplete="given-name" enterkeyhint="done" maxlength="100" required></label><label class="field"><span>${translate('applicant_family_name')}</span><input name="family_name" autocomplete="family-name" enterkeyhint="done" maxlength="100" required></label><p class="hint">${translate('application_scope')}</p><button class="button full" type="submit">${translate('application_submit')}</button></form></div>`;
    else if (lot.leading)
      action = `<div class="panel"><h3>${translate('bid_leading_title')}</h3><p>${translate('bid_leading_description')}</p></div>`;
    else
      action = `<div class="field"><label class="field-label" for="bid-amount">${translate('bid_amount_label')}</label><div class="input-row"><input id="bid-amount" type="number" inputmode="numeric" min="${minimum}" step="${lot.step}" value="${minimum}" aria-describedby="live-bid-hint"><button class="button" id="bid">${translate('bid_submit')}</button></div><p class="hint" id="live-bid-hint">${translate('bid_minimum_prefix')}${money(minimum)}${translate('bid_step_separator')}${money(lot.step)}${translate('bid_multiple_steps_hint')}</p></div>`;
    const subs = state.me
      ? `<div class="toggle-row"><span>${translate('subscription_watch_label')}</span><input type="checkbox" id="watch" ${lot.watching ? 'checked' : ''} aria-label="${translate('subscription_watch_label')}"></div>${lot.has_bid ? `<div class="toggle-row"><span>${translate('subscription_participation_label')}</span><input type="checkbox" id="participation" ${lot.participation ? 'checked' : ''} aria-label="${translate('subscription_participation_label')}"></div>` : ''}<p class="hint">${translate('subscription_bot_hint')}</p>`
      : '';
    const history =
      state.me && state.me.admin
        ? `<section class="admin-bid-history" aria-labelledby="admin-history-title"><div class="admin-history-heading"><h2 id="admin-history-title">${translate('history_title')}</h2><span>${translate('history_private_hint')}</span></div><div id="admin-bid-history" data-lot="${lot.id}" aria-live="polite"><p class="hint">${translate('history_loading')}</p></div></section>`
        : '';
    root.innerHTML = `<div class="page"><a class="back" href="#/">${translate('nav_catalog')}</a><div class="detail-layout"><div><div class="detail-photo">${images}</div>${thumbs}</div><div class="detail"><p class="eyebrow">${translate('pickup_value')}</p><h1 id="live-title">${esc(lot.title)}</h1><p class="detail-description" id="live-description">${esc(lot.description || translate('lot_description_missing'))}</p><p class="hint" id="live-condition">${translate('lot_condition_prefix')}${esc(lot.condition || translate('lot_condition_missing'))}</p><div class="detail-info"><div><span class="small-label" id="live-price-label">${lot.price === null ? translate('price_start_label') : translate('price_current_label')}</span><strong id="live-price">${money(price)}</strong></div><div><span class="small-label">${translate('lot_end_label')}</span><strong>${translate('lot_end_date')}</strong><div class="hint">23:59 · UTC+7</div></div></div><p class="live-change" id="live-change" role="status" hidden></p>${action}${subs}${history}</div></div></div>`;
    document.querySelectorAll('[data-photo]').forEach(
      (el) =>
        (el.onclick = () => {
          document.getElementById('main-photo').src = el.dataset.photo;
        }),
    );
    const application = document.getElementById('application-form');
    if (application)
      application.onsubmit = async (event) => {
        event.preventDefault();
        const submit = application.querySelector('button');
        const form = new FormData(application);
        submit.disabled = true;
        try {
          await post('/api/application', {
            given_name: form.get('given_name'),
            family_name: form.get('family_name'),
          });
          toast(translate('application_sent'));
          await load();
        } catch (e) {
          submit.disabled = false;
          toast(e.message);
        }
      };
    const bid = document.getElementById('bid');
    if (bid)
      bid.onclick = async () => {
        const entered = document.getElementById('bid-amount').value.trim(),
          amount = Number(entered);
        const current = state.lots.find((item) => item.id === lot.id);
        if (
          !current ||
          current.status !== 'active' ||
          !state.me ||
          state.me.status !== 'approved' ||
          current.leading
        ) {
          toast(translate('bid_conditions_changed'));
          return;
        }
        if (!entered || !Number.isSafeInteger(amount)) {
          toast(translate('bid_integer_required'));
          return;
        }
        if (!confirm(`${translate('bid_confirm_prefix')}${money(amount)}?`)) return;
        bid.disabled = true;
        try {
          await post(`/api/lots/${lot.id}/bid`, { amount, expected_price: current.price });
          toast(translate('bid_accepted'));
          await load();
        } catch (e) {
          toast(e.message);
          bid.disabled = false;
          requestImmediateSync();
        }
      };
    for (const kind of ['watch', 'participation']) {
      const el = document.getElementById(kind);
      if (el)
        el.onchange = async () => {
          const enabled = el.checked;
          el.disabled = true;
          try {
            await post(`/api/lots/${lot.id}/subscription`, {
              kind: kind === 'watch' ? 'watching' : 'participation',
              enabled,
            });
            toast(translate('subscriptions_saved'));
          } catch (e) {
            if (!e.outcomeUnknown) el.checked = !enabled;
            toast(e.message);
            requestImmediateSync();
          } finally {
            el.disabled = false;
          }
        };
    }
    if (state.me && state.me.admin) loadProductHistory(lot.id);
  }
  function drawProductHistory(lotId, data) {
    const box = document.getElementById('admin-bid-history');
    if (!box || box.dataset.lot !== String(lotId) || !state.me || !state.me.admin) return;
    const signature = JSON.stringify(data.bids);
    if (box.historySignature === signature) return;
    box.historySignature = signature;
    box.innerHTML = data.bids.length
      ? `<div class="admin-history-list">${data.bids.map((b) => `<div class="admin-history-row"><div class="admin-history-person"><strong>${privateName(b)}</strong><small>${b.username ? '@' + esc(b.username) + ' · ' : ''}${esc(new Date(b.created * 1000).toLocaleString('ru-RU', { timeZone: 'Asia/Novosibirsk', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }))}</small></div><strong class="admin-history-amount">${money(b.amount)}</strong></div>`).join('')}</div>`
      : `<p class="hint">${translate('history_empty')}</p>`;
  }
  async function loadProductHistory(lotId) {
    const box = document.getElementById('admin-bid-history'),
      role = state.role;
    try {
      const data = await api(`/api/admin/lots/${lotId}`);
      if (
        document.getElementById('admin-bid-history') === box &&
        state.role === role &&
        box &&
        !box.historySignature
      )
        drawProductHistory(lotId, data);
    } catch (e) {
      if (
        box &&
        !box.historySignature &&
        document.getElementById('admin-bid-history') === box &&
        state.me &&
        state.me.admin
      )
        box.innerHTML = `<p class="hint">${translate('history_load_failed')}</p>`;
    }
  }
  function drawMyBids(data) {
    const box = document.getElementById('my-content');
    if (!box) return;
    state.myBidsSignature = JSON.stringify([data.bids, state.lots]);
    if (!data.bids.length) {
      box.innerHTML = `<div class="empty"><strong>${translate('my_bids_empty_title')}</strong><p>${translate('my_bids_empty_description')}</p><a class="button" href="#/">${translate('my_bids_catalog_link')}</a></div>`;
      return;
    }
    box.innerHTML = data.bids
      .map((b) => {
        const lot = state.lots.find((x) => x.id === b.lot_id);
        const status =
          lot && lot.status === 'cancelled'
            ? translate('lot_status_cancelled')
            : lot && lot.status === 'ended'
              ? lot.leading
                ? translate('lot_status_won')
                : translate('lot_ended_title')
              : lot && lot.leading
                ? translate('lot_status_leading')
                : translate('lot_status_outbid');
        return `<a class="list-item" href="#/lot/${b.lot_id}"><span><strong>${esc(b.title)}</strong><small>${new Date(b.created * 1000).toLocaleString('ru-RU', { timeZone: 'Asia/Novosibirsk' })} · ${status}</small></span><strong>${money(b.amount)}</strong></a>`;
      })
      .join('');
  }
  async function my() {
    nav('my');
    root.innerHTML = `<div class="page"><p class="eyebrow">${translate('my_bids_kicker')}</p><h1 class="page-title">${translate('my_bids_title')}</h1><div id="my-content"></div></div>`;
    const box = document.getElementById('my-content');
    if (!state.me) {
      box.innerHTML = authHint();
      return;
    }
    try {
      const data = await api('/api/my-bids');
      if (location.hash === '#/my' && document.getElementById('my-content') === box)
        drawMyBids(data);
    } catch (e) {
      if (document.getElementById('my-content') === box) box.textContent = e.message;
    }
  }
  function about() {
    nav('about');
    root.innerHTML = `<div class="page"><p class="eyebrow">${translate('rules_kicker')}</p><h1 class="page-title">${translate('rules_title')}</h1><div class="panel explain"><h3>${translate('rules_choose_title')}</h3><p>${translate('rules_choose_description')}</p><h3>${translate('rules_apply_title')}</h3><p>${translate('rules_apply_description')}</p><h3>${translate('rules_bid_title')}</h3><p>${translate('rules_bid_description')}</p><h3>${translate('rules_pickup_title')}</h3><p>${translate('rules_pickup_description')}</p><h3>${translate('rules_privacy_title')}</h3><p>${translate('rules_privacy_description')}</p></div></div>`;
  }
  function privateName(user) {
    return esc(
      [user.applicant_given_name, user.applicant_family_name].filter(Boolean).join(' ') ||
        user.first_name ||
        translate('applicant_name_missing'),
    );
  }
  const applicationSignature = (users) => JSON.stringify([...users].sort((a, b) => a.id - b.id));
  async function runAdminAction(button, action) {
    if (button.disabled) return;
    const controls = Array.from(button.closest('.actions')?.querySelectorAll('button') || [button]);
    const disabledBefore = controls.map((control) => control.disabled);
    const currentView = viewVersion;
    controls.forEach((control) => {
      control.disabled = true;
    });
    try {
      await action(() => currentView === viewVersion && location.hash === '#/admin');
    } catch (error) {
      toast(error.message);
      if (error.outcomeUnknown) requestImmediateSync();
    } finally {
      controls.forEach((control, index) => {
        control.disabled = disabledBefore[index];
      });
    }
  }
  function adminShell(tab) {
    nav('');
    root.innerHTML = `<div class="page"><div class="admin-header"><div><p class="eyebrow">${translate('admin_kicker')}</p><h1 class="page-title">${translate('admin_title')}</h1></div><a class="back" href="#/">${translate('nav_home')}</a></div><div class="tabs"><button class="tab ${tab === 'lots' ? 'active' : ''}" data-tab="lots">${translate('admin_lots_tab')}</button><button class="tab ${tab === 'applications' ? 'active' : ''}" data-tab="applications">${translate('admin_applications_tab')}</button><button class="tab ${tab === 'new' ? 'active' : ''}" data-tab="new">${translate('admin_create_tab')}</button></div><div id="admin-content"></div></div>`;
    document
      .querySelectorAll('[data-tab]')
      .forEach((b) => (b.onclick = () => admin(b.dataset.tab)));
  }
  function drawApplications(data) {
    const box = document.getElementById('admin-content');
    if (!box) return;
    state.applications = data.users;
    box.innerHTML = data.users.length
      ? data.users
          .map(
            (u) =>
              `<div class="list-item application-item" data-application="${u.id}" data-current-status="${esc(u.status)}"><div class="application-person"><strong>${privateName(u)} ${u.username ? '@' + esc(u.username) : ''}</strong><small>Telegram ID: ${u.id}</small><span class="application-status ${u.status === 'approved' ? 'approved' : u.status === 'rejected' ? 'rejected' : 'pending'}" role="status">${u.status === 'pending' ? translate('application_status_pending') : u.status === 'approved' ? translate('application_status_approved') : translate('application_status_rejected')}</span><p class="application-result" aria-live="polite" hidden></p></div><div class="actions"><button type="button" class="button secondary" data-user="${u.id}" data-status="approved" ${u.status === 'approved' ? 'disabled' : ''}>${translate('application_approve')}</button><button type="button" class="button danger" data-user="${u.id}" data-status="rejected" ${u.status === 'rejected' ? 'disabled' : ''}>${translate('application_reject')}</button></div></div>`,
          )
          .join('')
      : `<div class="empty"><strong>${translate('applications_empty')}</strong></div>`;
    box.querySelectorAll('[data-user]').forEach(
      (b) =>
        (b.onclick = async () => {
          const row = b.closest('[data-application]');
          const buttons = Array.from(row.querySelectorAll('[data-user]'));
          const message = row.querySelector('.application-result');
          const badge = row.querySelector('.application-status');
          const original = b.textContent;
          buttons.forEach((button) => {
            button.disabled = true;
          });
          b.textContent = translate('saving_status');
          message.hidden = true;
          try {
            const status = b.dataset.status;
            await post(`/api/admin/users/${b.dataset.user}`, { status });
            row.dataset.currentStatus = status;
            const applicant =
              state.applications &&
              state.applications.find((user) => String(user.id) === b.dataset.user);
            if (applicant) applicant.status = status;
            badge.className = `application-status ${status}`;
            badge.textContent =
              status === 'approved'
                ? translate('application_status_approved')
                : translate('application_status_rejected');
            message.textContent =
              status === 'approved'
                ? translate('application_approved_feedback')
                : translate('application_rejected_feedback');
            message.className = 'application-result success';
            message.hidden = false;
          } catch (e) {
            message.textContent = `${translate('application_save_failed_prefix')}${e.message}`;
            message.className = 'application-result error';
            message.hidden = false;
          } finally {
            b.textContent = original;
            buttons.forEach((button) => {
              button.disabled = button.dataset.status === row.dataset.currentStatus;
            });
          }
        }),
    );
  }
  async function admin(tab = 'lots') {
    if (!state.me || !state.me.admin) {
      root.innerHTML = `<div class="page"><div class="empty"><strong>${translate('admin_access_denied')}</strong></div></div>`;
      return;
    }
    viewVersion++;
    state.adminView = tab;
    state.adminLotId = null;
    adminShell(tab);
    const box = document.getElementById('admin-content');
    if (tab === 'new') {
      box.innerHTML = `<div class="panel"><h3>${translate('lot_create_title')}</h3><p>${translate('lot_common_deadline_hint')}</p><form id="new-lot"><label class="field"><span>${translate('lot_title_label_with_space')}<span class="required">*</span></span><input name="title" maxlength="120" required placeholder="${translate('lot_title_placeholder')}"></label><label class="field"><span>${translate('lot_description_label')}</span><textarea name="description" maxlength="3000" placeholder="${translate('lot_description_placeholder')}"></textarea></label><label class="field"><span>${translate('lot_condition_label')}</span><input name="condition" maxlength="120" placeholder="${translate('lot_condition_placeholder')}"></label><div class="input-row"><label class="field"><span>${translate('lot_start_price_label')}</span><input name="start_price" type="number" min="0" max="100000000" value="0" required></label><label class="field"><span>${translate('lot_step_label')}</span><input name="step" type="number" min="1" max="100000000" value="50" required></label></div><label class="field"><span>${translate('lot_photos_label')}</span><input name="photos" type="file" accept="image/jpeg,image/png,image/webp,image/heic,image/heif" multiple></label><p class="hint">${translate('lot_photos_privacy_hint')}</p><button class="button full" type="submit">${translate('lot_publish')}</button></form></div>`;
      document.getElementById('new-lot').onsubmit = async (e) => {
        e.preventDefault();
        const form = e.target;
        await runAdminAction(form.querySelector('button'), async (isCurrent) => {
          await api('/api/admin/lots', { method: 'POST', body: new FormData(form) });
          toast(translate('lot_published'));
          const d = await api('/api/lots');
          state.lots = d.lots;
          if (isCurrent()) admin('lots');
        });
      };
      return;
    }
    if (tab === 'applications') {
      try {
        const data = await api('/api/admin/applications');
        if (state.adminView === 'applications' && document.getElementById('admin-content') === box)
          drawApplications(data);
      } catch (e) {
        if (document.getElementById('admin-content') === box) box.textContent = e.message;
      }
      return;
    }
    box.innerHTML = state.lots.length
      ? state.lots
          .map(
            (l) =>
              `<div class="list-item"><div><strong>${esc(l.title)}</strong><small>${l.status === 'cancelled' ? translate('lot_badge_cancelled') : l.status === 'ended' ? translate('lot_badge_ended') : translate('lot_status_active')} · ${l.count}${translate('bid_count_price_separator')}${money(l.price === null ? l.start_price : l.price)}</small></div><button class="button secondary" data-manage="${l.id}">${translate('lot_open')}</button></div>`,
          )
          .join('')
      : `<div class="empty"><strong>${translate('admin_lots_empty_title')}</strong><p>${translate('admin_lots_empty_description')}</p></div>`;
    box
      .querySelectorAll('[data-manage]')
      .forEach((b) => (b.onclick = () => adminLot(Number(b.dataset.manage))));
  }
  async function adminLot(id, preloaded = null) {
    viewVersion++;
    state.adminView = 'lot';
    state.adminLotId = id;
    try {
      const data = preloaded || (await api(`/api/admin/lots/${id}`));
      if (location.hash !== '#/admin' || state.adminView !== 'lot' || state.adminLotId !== id)
        return;
      state.adminLotSignature = JSON.stringify(data);
      const l = data.lot;
      const box = document.getElementById('admin-content');
      if (!box) return;
      box.innerHTML = `<button class="back back-button" id="admin-back">${translate('nav_catalog')}</button><div class="panel"><h3>${esc(l.title)}</h3><p>${l.status === 'active' ? translate('lot_bidding_active') : l.status === 'ended' ? translate('lot_ended_title') : translate('lot_status_cancelled')} · ${l.count}${translate('bid_count_suffix')}</p><div class="actions">${l.status === 'active' && l.count === 0 ? `<button class="button secondary" id="edit-lot">${translate('lot_edit')}</button>` : ''}${l.status === 'active' ? `<button class="button danger" id="cancel-lot">${translate('lot_cancel')}</button>` : ''}${l.status === 'ended' && data.candidates.length ? `<button class="button danger" id="refuse">${translate('candidate_refuse')}</button>${data.has_refusals ? `<button class="button secondary" id="notify-candidate">${translate('candidate_notify')}</button>` : ''}<button class="button secondary" data-fulfill="agreed">${translate('fulfillment_agreed_action')}</button><button class="button secondary" data-fulfill="delivered">${translate('fulfillment_delivered_action')}</button>` : ''}</div><p>${translate('fulfillment_label')}${data.fulfillment === 'agreed' ? translate('fulfillment_agreed') : data.fulfillment === 'delivered' ? translate('fulfillment_delivered') : translate('fulfillment_unset')}</p></div><h3>${translate('candidates_title')}</h3>${data.candidates.length ? data.candidates.map((u, i) => `<div class="list-item"><span><strong>${i === 0 ? '★ ' : ''}${privateName(u)} ${u.username ? '@' + esc(u.username) : ''}</strong><small>Telegram ID: ${u.id}</small></span><strong>${money(u.amount)}</strong></div>`).join('') : `<p class="hint">${translate('candidates_empty')}</p>`}<h3>${translate('history_title')}</h3>${data.bids.map((b) => `<div class="list-item"><span>${privateName(b)} ${b.username ? '@' + esc(b.username) : ''}</span><strong>${money(b.amount)}</strong></div>`).join('')}`;
      document.getElementById('admin-back').onclick = () => admin('lots');
      const edit = document.getElementById('edit-lot');
      if (edit)
        edit.onclick = () => {
          viewVersion++;
          state.adminView = 'edit';
          box.innerHTML = `<button class="back back-button" id="admin-back">${translate('nav_lot')}</button><div class="panel"><h3>${translate('lot_edit_title')}</h3><p>${translate('lot_edit_policy')}</p><p class="live-change" id="live-edit-warning" role="status" hidden></p><form id="edit-form"><label class="field"><span>${translate('lot_title_label')}</span><input name="title" maxlength="120" required value="${esc(l.title)}"></label><label class="field"><span>${translate('lot_description_label')}</span><textarea name="description" maxlength="3000">${esc(l.description)}</textarea></label><label class="field"><span>${translate('lot_condition_label')}</span><input name="condition" maxlength="120" value="${esc(l.condition)}"></label><label class="field"><span>${translate('lot_start_price_label')}</span><input name="start_price" type="number" min="0" required value="${l.start_price}"></label><label class="field"><span>${translate('lot_step_label')}</span><input name="step" type="number" min="1" required value="${l.step}"></label><button class="button full">${translate('lot_save')}</button></form></div>`;
          document.getElementById('admin-back').onclick = () => adminLot(id);
          document.getElementById('edit-form').onsubmit = async (e) => {
            e.preventDefault();
            const form = e.target,
              f = new FormData(form);
            await runAdminAction(form.querySelector('button'), async (isCurrent) => {
              await post(`/api/admin/lots/${id}/edit`, {
                title: f.get('title'),
                description: f.get('description'),
                condition: f.get('condition'),
                start_price: Number(f.get('start_price')),
                step: Number(f.get('step')),
              });
              const x = await api('/api/lots');
              state.lots = x.lots;
              toast(translate('lot_saved'));
              if (isCurrent()) await adminLot(id);
            });
          };
        };
      const cancel = document.getElementById('cancel-lot');
      if (cancel)
        cancel.onclick = () => {
          if (confirm(translate('lot_cancel_confirm')))
            runAdminAction(cancel, async (isCurrent) => {
              await post(`/api/admin/lots/${id}`, { action: 'cancel' });
              toast(translate('lot_status_cancelled'));
              const x = await api('/api/lots');
              state.lots = x.lots;
              if (isCurrent()) admin('lots');
            });
        };
      const refuse = document.getElementById('refuse');
      if (refuse)
        refuse.onclick = () => {
          if (confirm(translate('candidate_refuse_confirm')))
            runAdminAction(refuse, async (isCurrent) => {
              await post(`/api/admin/lots/${id}`, {
                action: 'refuse',
                expected_candidate_id: data.candidates[0].id,
              });
              if (isCurrent()) await adminLot(id);
            });
        };
      const notify = document.getElementById('notify-candidate');
      if (notify)
        notify.onclick = () => {
          if (confirm(translate('candidate_notify_confirm')))
            runAdminAction(notify, async () => {
              await post(`/api/admin/lots/${id}/notify-candidate`, {
                expected_candidate_id: data.candidates[0].id,
              });
              toast(translate('candidate_notification_sent'));
            });
        };
      box.querySelectorAll('[data-fulfill]').forEach(
        (b) =>
          (b.onclick = () =>
            runAdminAction(b, async (isCurrent) => {
              await post(`/api/admin/lots/${id}`, { action: b.dataset.fulfill });
              if (isCurrent()) await adminLot(id);
            })),
      );
    } catch (e) {
      toast(e.message);
    }
  }
  function render() {
    viewVersion++;
    const hash = location.hash || '#/';
    if (hash.startsWith('#/lot/')) {
      const id = Number(hash.split('/')[2]);
      const l = state.lots.find((x) => x.id === id);
      if (l) detail(l);
      else
        root.innerHTML = `<div class="page"><div class="empty"><strong>${translate('lot_not_found_title')}</strong></div></div>`;
    } else if (hash === '#/my') my();
    else if (hash === '#/about') about();
    else if (hash === '#/admin') admin();
    else catalog();
    window.scrollTo(0, 0);
  }

  // The temporary tunnel does not support server-sent events. Poll while the app is
  // visible, discard stale responses, and never replace a form containing a draft.
  let syncTimer = null,
    syncing = false,
    resyncRequested = false;
  function liveIndicator(connected) {
    const indicator = document.getElementById('live-indicator');
    if (!indicator) return;
    const text = connected ? translate('connection_online') : translate('connection_offline');
    if (indicator.textContent !== text) indicator.textContent = text;
    indicator.classList.toggle('offline', !connected);
  }
  function detailHasDraft() {
    const active = document.activeElement;
    if (active && root.contains(active) && active.matches('input:not([type=checkbox]), textarea'))
      return true;
    const application = document.getElementById('application-form');
    if (
      application &&
      Array.from(application.querySelectorAll('input')).some((input) => input.value.trim() !== '')
    )
      return true;
    const amount = document.getElementById('bid-amount');
    return !!(amount && amount.value !== amount.defaultValue);
  }
  function patchDetail(lot) {
    const byId = (id) => document.getElementById(id);
    const title = byId('live-title'),
      description = byId('live-description'),
      condition = byId('live-condition');
    const price = byId('live-price'),
      label = byId('live-price-label'),
      hint = byId('live-bid-hint'),
      amount = byId('bid-amount');
    if (title) title.textContent = lot.title;
    if (description)
      description.textContent = lot.description || translate('lot_description_missing');
    if (condition)
      condition.textContent =
        translate('lot_condition_prefix') + (lot.condition || translate('lot_condition_missing'));
    if (price) price.textContent = money(lot.price === null ? lot.start_price : lot.price);
    if (label)
      label.textContent =
        lot.price === null ? translate('price_start_label') : translate('price_current_label');
    const minimum = lot.price === null ? lot.start_price : lot.price + lot.step;
    if (amount) {
      amount.min = String(minimum);
      amount.step = String(lot.step);
    }
    if (hint)
      hint.textContent = `${translate('bid_minimum_prefix')}${money(minimum)}${translate('bid_step_separator')}${money(lot.step)}${translate('bid_multiple_steps_hint')}`;
    const watch = byId('watch'),
      participation = byId('participation');
    if (watch) watch.checked = lot.watching;
    if (participation) participation.checked = lot.participation;
    const warning = byId('live-change'),
      bid = byId('bid');
    const previous = detailRenderedSignature ? JSON.parse(detailRenderedSignature)[0] : null;
    let message = '';
    if (lot.status !== 'active')
      message =
        lot.status === 'cancelled'
          ? translate('lot_cancelled_notice')
          : translate('lot_ended_notice');
    else if (bid && (!state.me || state.me.status !== 'approved'))
      message = translate('bid_access_changed_notice');
    else if (bid && lot.leading) message = translate('bid_leading_notice');
    else if (previous && (previous.price !== lot.price || previous.step !== lot.step))
      message = translate('bid_price_changed_notice');
    if (bid)
      bid.disabled =
        lot.status !== 'active' || !state.me || state.me.status !== 'approved' || lot.leading;
    if (warning) {
      if (warning.textContent !== message) warning.textContent = message;
      warning.hidden = !message;
    }
  }
  function requestImmediateSync() {
    if (syncing) {
      resyncRequested = true;
      return;
    }
    clearTimeout(syncTimer);
    syncTimer = setTimeout(syncNow, 0);
  }
  async function syncNow() {
    if (syncing) {
      resyncRequested = true;
      return;
    }
    if (document.hidden || (tg && tg.isActive === false) || loading || pendingWrites) {
      syncTimer = setTimeout(syncNow, 3000);
      return;
    }
    if (!ready) {
      await load();
      if (!ready) liveIndicator(false);
      syncTimer = setTimeout(syncNow, 3000);
      return;
    }
    syncing = true;
    const version = mutationVersion,
      currentView = viewVersion,
      route = location.hash || '#/',
      role = state.role;
    const adminView = state.adminView,
      adminLotId = state.adminLotId;
    const controller = new AbortController(),
      timeout = setTimeout(() => controller.abort(), 8000);
    try {
      const options = { signal: controller.signal };
      const [me, lots] = await Promise.all([api('/api/me', options), api('/api/lots', options)]);
      let extra = null;
      if (route === '#/my' && me.user) extra = await api('/api/my-bids', options);
      if (route.startsWith('#/lot/') && me.user && me.user.admin)
        extra = await api(`/api/admin/lots/${Number(route.split('/')[2])}`, options);
      if (route === '#/admin' && me.user && me.user.admin && adminView === 'applications')
        extra = await api('/api/admin/applications', options);
      if (
        route === '#/admin' &&
        me.user &&
        me.user.admin &&
        adminView === 'lot' &&
        adminLotId !== null
      )
        extra = await api(`/api/admin/lots/${adminLotId}`, options);
      if (
        version !== mutationVersion ||
        pendingWrites ||
        loading ||
        currentView !== viewVersion ||
        route !== (location.hash || '#/') ||
        role !== state.role
      )
        return;
      const oldMe = state.me,
        lotsChanged = JSON.stringify(state.lots) !== JSON.stringify(lots.lots);
      state.me = me.user;
      state.lots = lots.lots;
      state.end = lots.end;
      document.getElementById('admin-link').hidden = !(state.me && state.me.admin);
      liveIndicator(true);
      if (route === '#/' || route === '#') {
        if (lotsChanged) refreshCatalog();
      } else if (route.startsWith('#/lot/')) {
        const id = Number(route.split('/')[2]),
          lot = state.lots.find((item) => item.id === id);
        const signature = lot
          ? JSON.stringify([lot, state.me && state.me.status, Boolean(state.me && state.me.admin)])
          : null;
        if (signature !== detailRenderedSignature) {
          if (lot && detailHasDraft()) patchDetail(lot);
          else if (lot) {
            const photo = document.getElementById('main-photo');
            const selected = photo && photo.getAttribute('src');
            detail(lot);
            const restored = document.getElementById('main-photo');
            if (restored && selected && lot.photos.includes(selected)) restored.src = selected;
          } else
            root.innerHTML = `<div class="page"><div class="empty"><strong>${translate('lot_not_found_title')}</strong></div></div>`;
          if (oldMe && oldMe.status === 'pending' && state.me && state.me.status === 'approved')
            toast(translate('application_approved_notice'));
        }
        if (lot && extra && state.me && state.me.admin) drawProductHistory(id, extra);
      } else if (route === '#/my') {
        if (!state.me && oldMe) my();
        else if (extra && JSON.stringify([extra.bids, state.lots]) !== state.myBidsSignature)
          drawMyBids(extra);
      } else if (route === '#/admin') {
        if (!state.me || !state.me.admin) admin();
        else if (
          adminView === 'applications' &&
          extra &&
          (!state.applications ||
            applicationSignature(extra.users) !== applicationSignature(state.applications))
        )
          drawApplications(extra);
        else if (adminView === 'lots' && lotsChanged) admin('lots');
        else if (adminView === 'lot' && extra && JSON.stringify(extra) !== state.adminLotSignature)
          adminLot(adminLotId, extra);
        else if (adminView === 'edit') {
          const lot = state.lots.find((item) => item.id === adminLotId),
            warning = document.getElementById('live-edit-warning'),
            save = document.querySelector(
              '#edit-form button[type=submit], #edit-form button:not([type])',
            );
          const unavailable = !lot || lot.status !== 'active' || lot.count > 0;
          if (warning) {
            warning.hidden = !unavailable;
            warning.textContent = unavailable ? translate('lot_edit_locked_notice') : '';
          }
          if (save) save.disabled = unavailable;
        }
      }
    } catch (e) {
      if (!document.hidden) liveIndicator(false);
    } finally {
      clearTimeout(timeout);
      syncing = false;
      const delay = resyncRequested ? 0 : 3000;
      resyncRequested = false;
      clearTimeout(syncTimer);
      syncTimer = setTimeout(syncNow, delay);
    }
  }
  document.addEventListener('visibilitychange', () => {
    if (!document.hidden) requestImmediateSync();
  });
  window.addEventListener('online', requestImmediateSync);
  if (tg && typeof tg.onEvent === 'function') tg.onEvent('activated', requestImmediateSync);
  window.addEventListener('hashchange', render);
  document.getElementById('admin-link').onclick = () => {
    location.hash = '#/admin';
  };
  load().then(requestImmediateSync);
})().catch((error) => {
  console.error('Application startup failed:', error);
  const root = document.getElementById('app');
  root.textContent = root.dataset.startupError;
});
