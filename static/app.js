'use strict';

/**
 * Find a view element and verify its native DOM class before reading typed properties.
 * Missing optional controls return null; a mismatched template is a programming error.
 * @template {Element} T
 * @param {string} selector
 * @param {{new(): T}} Type
 * @param {ParentNode} [parent=document]
 * @returns {T | null}
 */
function findElement(selector, Type, parent = document) {
  const element = parent.querySelector(selector);
  if (element !== null && !(element instanceof Type))
    throw new Error(`Unexpected element type: ${selector}`);
  return element;
}

/**
 * Require an element owned by the current template rather than asserting away null.
 * @template {Element} T
 * @param {string} selector
 * @param {{new(): T}} Type
 * @param {ParentNode} [parent=document]
 * @returns {T}
 */
function requireElement(selector, Type, parent = document) {
  const element = findElement(selector, Type, parent);
  if (!element) throw new Error(`Missing view element: ${selector}`);
  return element;
}

/**
 * Collect controls from a template with the same native-class check as findElement.
 * @template {Element} T
 * @param {string} selector
 * @param {{new(): T}} Type
 * @param {ParentNode} [parent=document]
 * @returns {T[]}
 */
function elements(selector, Type, parent = document) {
  return Array.from(parent.querySelectorAll(selector)).map((element) => {
    if (!(element instanceof Type)) throw new Error(`Unexpected element type: ${selector}`);
    return element;
  });
}

/** @param {unknown} error @returns {string} A useful message even for non-Error rejections. */
function errorMessage(error) {
  return error instanceof Error ? error.message : String(error);
}

/** @param {unknown} error @returns {boolean} Whether a failed write may have reached the server. */
function outcomeUnknown(error) {
  return error instanceof Error && 'outcomeUnknown' in error && error.outcomeUnknown === true;
}

/** Create a UUID with cryptographic randomness, including browsers without randomUUID.
 * @returns {string} A canonical operation ID; unavailable secure randomness is an error.
 */
function createOperationId() {
  if (typeof crypto.randomUUID === 'function') return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/**
 * Initialize SDK/localization, bind the views and start visibility-aware polling.
 * @returns {Promise<void>}
 */
(async function startApp() {
  const READ_TIMEOUT_MS = 15000;
  const WRITE_TIMEOUT_MS = 60000;
  /** Load the SDK with a deadline; null keeps the public catalog available without authentication.
   * @returns {Promise<TelegramWebApp | null>}
   */
  function loadTelegramSdk() {
    if (window.Telegram && window.Telegram.WebApp) return Promise.resolve(window.Telegram.WebApp);
    return new Promise((resolve) => {
      const script = document.createElement('script');
      /** @type {ReturnType<typeof setTimeout> | undefined} */
      let timeout;
      /** Release startup exactly once the load finishes or its deadline expires. */
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
  /** Normalize optional browser storage to one of the explicit demonstration identities.
   * @param {string | null} value @returns {DemoRole}
   */
  function demoRole(value) {
    return value === 'friend' || value === 'other' || value === 'admin' ? value : 'guest';
  }
  const telegramPromise = loadTelegramSdk();
  const translationController = new AbortController();
  const translationTimeout = setTimeout(() => translationController.abort(), READ_TIMEOUT_MS);
  /** @type {Record<string, string>} */
  let messages;
  try {
    const response = await fetch('/assets/ru.json?v=4', {
      cache: 'no-cache',
      signal: translationController.signal,
    });
    if (!response.ok) throw new Error('Could not load translations.');
    // This is a trusted deployment resource assertion, not JSON schema validation.
    messages = /** @type {Record<string, string>} */ (await response.json());
  } finally {
    clearTimeout(translationTimeout);
  }
  /** Resolve a deployment message and fail explicitly on missing keys.
   * @param {string} key @returns {string}
   */
  const translate = (key) => {
    if (!Object.prototype.hasOwnProperty.call(messages, key))
      throw new Error(`Missing translation: ${key}`);
    return messages[key];
  };
  const root = requireElement('#app', HTMLElement);
  const toastEl = requireElement('#toast', HTMLElement);
  /** @type {AppState} */
  const state = {
    me: null,
    lots: [],
    demo: false,
    trial: false,
    role: demoRole(sessionStorage.getItem('demoRole')),
    end: 0,
    adminView: null,
    adminLotId: null,
    adminTarget: null,
    adminLotsSignature: null,
    applications: null,
    adminLotSignature: null,
    myBidsSignature: null,
  };
  const tg = await telegramPromise;
  if (tg) {
    tg.ready();
    tg.expand();
  }
  /** Escape untrusted display values for the existing HTML templates.
   * @param {unknown} s @returns {string}
   */
  const esc = (s) =>
    String(s ?? '').replace(
      /[&<>"']/g,
      (c) =>
        /** @type {Record<string, string>} */ ({
          '&': '&amp;',
          '<': '&lt;',
          '>': '&gt;',
          '"': '&quot;',
          "'": '&#39;',
        })[c],
    );
  /** @param {number} n @returns {string} Display integer rubles using the Russian number format. */
  const money = (n) => `${Number(n).toLocaleString('ru-RU')} ₽`;
  /** @param {number} n @returns {string} Russian grammatical form for the public lot count. */
  const decl = (n) =>
    n % 10 === 1 && n % 100 !== 11
      ? translate('lot_count_one')
      : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14)
        ? translate('lot_count_few')
        : translate('lot_count_many');
  /** @param {number} n @returns {string} Russian grammatical form for an accepted bid count. */
  const bidsWord = (n) =>
    n % 10 === 1 && n % 100 !== 11
      ? translate('bid_count_one')
      : n % 10 >= 2 && n % 10 <= 4 && (n % 100 < 12 || n % 100 > 14)
        ? translate('bid_count_few')
        : translate('bid_count_many');
  /** @type {ReturnType<typeof setTimeout> | undefined} */
  let toastTimer;
  let pendingWrites = 0,
    mutationVersion = 0,
    loading = 0,
    viewVersion = 0,
    ready = false;
  /** @type {string | null} */
  let detailRenderedSignature = null;
  /** The signature belongs to a rendered history container, never another route's replacement.
   * @type {WeakMap<HTMLElement, string>}
   */
  const historySignatures = new WeakMap();
  /** An unresolved publication survives navigation within this app without another create intent.
   * @type {CreateAttempt | null}
   */
  let createAttempt = null;
  /** Show a transient status message, replacing any previous toast.
   * @param {string} message @returns {void}
   */
  function toast(message) {
    toastEl.textContent = message;
    toastEl.classList.add('visible');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toastEl.classList.remove('visible'), 4000);
  }
  /** Record a focused control's stable identity before replacing a view.
   * @param {HTMLElement} parent @returns {FocusSnapshot | null}
   */
  function captureFocus(parent) {
    const active = document.activeElement;
    if (!(active instanceof HTMLElement) || !parent.contains(active)) return null;
    return {
      id: active.id,
      photo: active.dataset.photo ?? null,
      user: active.dataset.user ?? null,
      status: active.dataset.status ?? null,
    };
  }
  /** Restore a matching enabled control, or a heading when that action no longer exists.
   * @param {HTMLElement} parent @param {FocusSnapshot | null} snapshot @returns {void}
   */
  function restoreFocus(parent, snapshot) {
    if (!snapshot) return;
    let target = snapshot.id ? document.getElementById(snapshot.id) : null;
    if (!target && snapshot.photo)
      target =
        elements('button[data-photo]', HTMLButtonElement, parent).find(
          (button) => button.dataset.photo === snapshot.photo,
        ) ?? null;
    if (!target && snapshot.user)
      target =
        elements('button[data-user]', HTMLButtonElement, parent).find(
          (button) =>
            button.dataset.user === snapshot.user && button.dataset.status === snapshot.status,
        ) ?? null;
    if (
      !target ||
      !parent.contains(target) ||
      target.hidden ||
      ((target instanceof HTMLButtonElement ||
        target instanceof HTMLInputElement ||
        target instanceof HTMLTextAreaElement ||
        target instanceof HTMLSelectElement) &&
        target.disabled)
    ) {
      target = findElement('h1, h2, h3', HTMLElement, parent);
      if (target) target.tabIndex = -1;
    }
    target?.focus({ preventScroll: true });
  }
  /**
   * Send an authenticated JSON/FormData request with a bounded lifetime.
   * Successful JSON remains unknown until an endpoint wrapper asserts its wire contract.
   * Transport failures on writes carry outcomeUnknown; callers must refresh, never retry blindly.
   * @param {string} path
   * @param {ApiRequestOptions} [options={}]
   * @returns {Promise<unknown>}
   */
  async function requestJson(path, options = {}) {
    const isWrite = options.method && options.method.toUpperCase() !== 'GET';
    if (isWrite) {
      pendingWrites++;
      mutationVersion++;
    }
    const { signal: parentSignal, ...requestOptions } = options;
    const controller = new AbortController();
    /** Forward caller cancellation and the request's own deadline to the same fetch. */
    const abort = () => controller.abort();
    if (parentSignal) {
      if (parentSignal.aborted) abort();
      else parentSignal.addEventListener('abort', abort, { once: true });
    }
    const timeout = setTimeout(abort, isWrite ? WRITE_TIMEOUT_MS : READ_TIMEOUT_MS);
    try {
      /** @type {Record<string, string>} */
      const headers = { ...(requestOptions.headers || {}) };
      if (tg && tg.initData) headers['X-Telegram-Init-Data'] = tg.initData;
      if (state.demo) headers['X-Demo-User'] = state.role;
      const body = requestOptions.body;
      const encodedBody = body && !(body instanceof FormData) ? JSON.stringify(body) : body;
      if (body && !(body instanceof FormData)) {
        headers['Content-Type'] = 'application/json';
      }
      let response;
      /** @type {unknown} */
      let data;
      try {
        response = await fetch(path, {
          ...requestOptions,
          body: encodedBody,
          headers,
          signal: controller.signal,
        });
        data = await response.json();
      } catch (error) {
        const key = isWrite
          ? 'error_request_timeout_write'
          : error instanceof Error && error.name === 'AbortError'
            ? 'error_request_timeout_read'
            : response
              ? 'error_response_unreadable'
              : 'error_connection_failed';
        /** @type {RequestFailure} */
        const failure = new Error(translate(key));
        failure.outcomeUnknown = Boolean(isWrite);
        throw failure;
      }
      if (!response.ok) {
        const message =
          data !== null &&
          typeof data === 'object' &&
          'error' in data &&
          typeof data.error === 'string'
            ? data.error
            : translate('error_request_failed');
        /** A server-side failure can follow a committed write; retain its reconciliation state.
         * @type {RequestFailure}
         */
        const failure = new Error(
          isWrite && response.status >= 500 ? translate('error_request_timeout_write') : message,
        );
        failure.outcomeUnknown = Boolean(isWrite && response.status >= 500);
        throw failure;
      }
      return data;
    } finally {
      clearTimeout(timeout);
      if (parentSignal) parentSignal.removeEventListener('abort', abort);
      if (isWrite) pendingWrites--;
    }
  }
  /**
   * Read an endpoint's declared successful response. The assertion marks the external JSON boundary;
   * it does not provide runtime schema validation or replace server authorization.
   * @template {keyof ApiGetResponses} P
   * @param {P} path
   * @param {ApiReadOptions} [options={}]
   * @returns {Promise<ApiGetResponses[P]>}
   */
  async function api(path, options = {}) {
    return /** @type {ApiGetResponses[P]} */ (await requestJson(path, options));
  }
  /**
   * Submit an endpoint-specific body once; state is subsequently reconciled through typed reads.
   * Response types are asserted at the same external JSON boundary as api().
   * @template {keyof ApiPostBodies} P
   * @param {P} path
   * @param {ApiPostBodies[P]} body
   * @returns {Promise<ApiPostResponses[P]>}
   */
  async function post(path, body) {
    return /** @type {ApiPostResponses[P]} */ (await requestJson(path, { method: 'POST', body }));
  }
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
  /** Return participation guidance without exposing authenticated account data.
   * @returns {string}
   */
  const authHint = () =>
    `<div class="panel"><h3>${translate('auth_title')}</h3><p>${translate('auth_description')}</p></div>`;
  /** Mark the active public navigation item; an empty value is used for administrator views.
   * @param {'catalog' | 'my' | 'about' | ''} which @returns {void}
   */
  function nav(which) {
    elements('[data-nav]', HTMLAnchorElement).forEach((a) =>
      a.classList.toggle('active', a.dataset.nav === which),
    );
  }
  /** Render the role selector only in the server-declared local demonstration environment.
   * @returns {void}
   */
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
      state.role = demoRole(s.value);
      sessionStorage.setItem('demoRole', s.value);
      location.hash = '#/';
      load();
    };
    el.append(s);
    document.body.prepend(el);
  }
  /** Label integration-mode activity so test bids are never presented as real bids.
   * @returns {void}
   */
  function trialBar() {
    const old = document.querySelector('.trial-strip');
    if (old) old.remove();
    if (!state.trial || state.demo) return;
    const el = document.createElement('div');
    el.className = 'trial-strip';
    el.textContent = translate('integration_banner');
    document.body.prepend(el);
  }
  /** Keep SDK failure visible with an explicit reload action; late SDK arrival cannot silently change identity.
   * @returns {void}
   */
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
  /** Reload the user's permissions and catalog before rendering the currently selected route.
   * @returns {Promise<void>}
   */
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
      requireElement('#admin-link', HTMLButtonElement).hidden = !(state.me && state.me.admin);
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
        esc(errorMessage(e)) +
        '</p></div></div>';
    } finally {
      loading--;
    }
  }
  /** Build one public lot card, escaping server-controlled title, condition and photo URLs.
   * @param {Lot} lot @returns {string}
   */
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
  /** Render the public catalog from the latest user-scoped lot snapshot.
   * @returns {void}
   */
  function catalog() {
    nav('catalog');
    const visible = state.lots;
    root.innerHTML = `<section class="catalog-heading"><div><p class="catalog-kicker">${translate('catalog_kicker')}<span>/</span> ${visible.length} ${decl(visible.length)}</p><h1>${translate('catalog_title')}</h1><p>${translate('catalog_description')}</p></div><a class="rules-link" href="#/about">${translate('nav_rules')}<span aria-hidden="true">↗</span></a></section>
 <section class="auction-info" aria-label="${translate('auction_conditions_label')}"><div><span class="info-label">${translate('auction_end_label')}</span><strong>${translate('auction_end_summary')}<small>UTC+7</small></strong></div><div><span class="info-label">${translate('pickup_label')}</span><strong>${translate('pickup_value')}</strong></div><span class="auction-info-note">${translate('auction_no_extension')}</span></section>
 <div class="section-head"><h2>${translate('catalog_items_title')}</h2><span class="count">${visible.length} ${decl(visible.length)}</span></div>
 ${visible.length ? `<div class="grid">${visible.map(card).join('')}</div>` : `<div class="empty"><strong>${translate('catalog_empty_title')}</strong><p>${translate('catalog_empty_description')}</p></div>`}<footer class="footnote"><span>${translate('footer_title')}</span><a href="#/about">${translate('footer_rules')}</a></footer>`;
  }
  /** Replace changed catalog cards while preserving the focused lot and scroll position.
   * @returns {void}
   */
  function refreshCatalog() {
    const active = document.activeElement;
    const focusedCard = active && root.contains(active) ? active.closest('.card') : null;
    const focusedHref = focusedCard && focusedCard.getAttribute('href');
    const scrollX = window.scrollX,
      scrollY = window.scrollY;
    catalog();
    if (focusedHref) {
      const restored = Array.from(elements('.card', HTMLAnchorElement, root)).find(
        (card) => card.getAttribute('href') === focusedHref,
      );
      if (restored) restored.focus({ preventScroll: true });
    }
    window.scrollTo(scrollX, scrollY);
  }
  /** Render a lot and bind explicit bid/application actions; private history is administrator-only.
   * @param {Lot} lot @returns {void}
   */
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
    root.innerHTML = `<div class="page"><a class="back" href="#/">${translate('nav_catalog')}</a><div class="detail-layout"><div><div class="detail-photo">${images}</div>${thumbs}</div><div class="detail"><p class="eyebrow">${translate('pickup_value')}</p><h1 id="live-title">${esc(lot.title)}</h1><p class="detail-description" id="live-description">${esc(lot.description || translate('lot_description_missing'))}</p><p class="hint" id="live-condition">${translate('lot_condition_prefix')}${esc(lot.condition || translate('lot_condition_missing'))}</p><div class="detail-info"><div><span class="small-label" id="live-price-label">${lot.price === null ? translate('price_start_label') : translate('price_current_label')}</span><strong id="live-price">${money(price)}</strong></div><div><span class="small-label">${translate('lot_end_label')}</span><strong>${translate('lot_end_date')}</strong><div class="hint">23:59 · UTC+7</div></div></div><p class="live-change" id="live-change" role="status" hidden></p>${state.me?.admin ? `<section class="panel lot-admin-tools"><h3>${translate('lot_admin_tools')}</h3><div class="actions"><button class="button secondary" id="product-edit" ${lot.status !== 'active' || lot.count > 0 ? 'disabled' : ''}>${translate('lot_edit')}</button><button class="button danger" id="product-delete">${translate('lot_delete')}</button></div><p class="hint">${translate('lot_edit_available_hint')}</p></section>` : ''}${action}${subs}${history}</div></div></div>`;
    const productEdit = findElement('#product-edit', HTMLButtonElement);
    if (productEdit) productEdit.onclick = () => openLotAdmin(lot.id, true);
    const productDelete = findElement('#product-delete', HTMLButtonElement);
    if (productDelete) productDelete.onclick = () => removeLot(lot, productDelete);
    elements('[data-photo]', HTMLButtonElement).forEach(
      (el) =>
        (el.onclick = () => {
          requireElement('#main-photo', HTMLImageElement).src = el.dataset.photo || '';
        }),
    );
    const application = findElement('#application-form', HTMLFormElement);
    if (application)
      application.onsubmit = async (event) => {
        event.preventDefault();
        const submit = requireElement('button', HTMLButtonElement, application);
        const form = new FormData(application);
        submit.disabled = true;
        try {
          await post('/api/application', {
            given_name: /** @type {string} */ (form.get('given_name')),
            family_name: /** @type {string} */ (form.get('family_name')),
          });
          toast(translate('application_sent'));
          await load();
        } catch (e) {
          submit.disabled = false;
          toast(errorMessage(e));
        }
      };
    const bid = findElement('#bid', HTMLButtonElement);
    if (bid)
      bid.onclick = async () => {
        const entered = requireElement('#bid-amount', HTMLInputElement).value.trim(),
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
          await post(`/api/lots/${lot.id}/bid`, {
            amount,
            expected_price: current.price,
            expected_revision: current.revision,
          });
          toast(translate('bid_accepted'));
          await load();
        } catch (e) {
          toast(errorMessage(e));
          bid.disabled = false;
          requestImmediateSync();
        }
      };
    for (const kind of ['watch', 'participation']) {
      const el = findElement(`#${kind}`, HTMLInputElement);
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
            if (!outcomeUnknown(e)) el.checked = !enabled;
            toast(errorMessage(e));
            requestImmediateSync();
          } finally {
            el.disabled = false;
          }
        };
    }
    if (state.me && state.me.admin) loadProductHistory(lot.id);
  }
  /** Apply private bid history only to the current lot's administrator container.
   * @param {number} lotId @param {AdminLotResponse} data @returns {void}
   */
  function drawProductHistory(lotId, data) {
    const box = findElement('#admin-bid-history', HTMLElement);
    if (!box || box.dataset.lot !== String(lotId) || !state.me || !state.me.admin) return;
    const signature = JSON.stringify(data.bids);
    if (historySignatures.get(box) === signature) return;
    historySignatures.set(box, signature);
    box.innerHTML = data.bids.length
      ? `<div class="admin-history-list">${data.bids.map((b) => `<div class="admin-history-row"><div class="admin-history-person"><strong>${privateName(b)}</strong><small>${b.username ? '@' + esc(b.username) + ' · ' : ''}${esc(new Date(b.created * 1000).toLocaleString('ru-RU', { timeZone: 'Asia/Novosibirsk', day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' }))}</small></div><strong class="admin-history-amount">${money(b.amount)}</strong></div>`).join('')}</div>`
      : `<p class="hint">${translate('history_empty')}</p>`;
  }
  /** Load administrator history without allowing a stale role/view response to replace current content.
   * @param {number} lotId @returns {Promise<void>}
   */
  async function loadProductHistory(lotId) {
    const box = findElement('#admin-bid-history', HTMLElement),
      role = state.role;
    try {
      const data = await api(`/api/admin/lots/${lotId}`);
      if (
        findElement('#admin-bid-history', HTMLElement) === box &&
        state.role === role &&
        box &&
        !historySignatures.get(box)
      )
        drawProductHistory(lotId, data);
    } catch (e) {
      if (
        box &&
        !historySignatures.get(box) &&
        findElement('#admin-bid-history', HTMLElement) === box &&
        state.me &&
        state.me.admin
      )
        box.innerHTML = `<p class="hint">${translate('history_load_failed')}</p>`;
    }
  }
  /** Render only the requesting participant's bids, with status derived from the current catalog.
   * @param {MyBidsResponse} data @returns {void}
   */
  function drawMyBids(data) {
    const box = findElement('#my-content', HTMLElement);
    if (!box) return;
    state.myBidsSignature = JSON.stringify([data.bids, state.lots]);
    if (!data.bids.length) {
      box.innerHTML = `<div class="empty"><strong>${translate('my_bids_empty_title')}</strong><p>${translate('my_bids_empty_description')}</p><a class="button" href="#/">${translate('my_bids_catalog_link')}</a></div>`;
      return;
    }
    box.innerHTML = data.bids
      .map((b) => {
        const status = b.deleted
          ? translate('lot_deleted_badge')
          : b.status === 'cancelled'
            ? translate('lot_status_cancelled')
            : b.status === 'ended'
              ? b.own_leading
                ? translate('lot_status_won')
                : translate('lot_ended_title')
              : b.own_leading
                ? translate('lot_status_leading')
                : translate('lot_status_outbid');
        const content = `<span><strong>${esc(b.title)}</strong><small>${new Date(b.created * 1000).toLocaleString('ru-RU', { timeZone: 'Asia/Novosibirsk' })} · ${status}</small></span><strong>${money(b.amount)}</strong>`;
        return b.deleted
          ? `<div class="list-item">${content}</div>`
          : `<a class="list-item" href="#/lot/${b.lot_id}">${content}</a>`;
      })
      .join('');
  }
  /** Load the current participant's history and discard responses for a replaced route container.
   * @returns {Promise<void>}
   */
  async function my() {
    nav('my');
    root.innerHTML = `<div class="page"><p class="eyebrow">${translate('my_bids_kicker')}</p><h1 class="page-title">${translate('my_bids_title')}</h1><div id="my-content"></div></div>`;
    const box = requireElement('#my-content', HTMLElement);
    if (!state.me) {
      box.innerHTML = authHint();
      return;
    }
    try {
      const data = await api('/api/my-bids');
      if (location.hash === '#/my' && findElement('#my-content', HTMLElement) === box)
        drawMyBids(data);
    } catch (e) {
      if (findElement('#my-content', HTMLElement) === box) box.textContent = errorMessage(e);
    }
  }
  /** Render the localized participation and data-retention rules.
   * @returns {void}
   */
  function about() {
    nav('about');
    root.innerHTML = `<div class="page"><p class="eyebrow">${translate('rules_kicker')}</p><h1 class="page-title">${translate('rules_title')}</h1><div class="panel explain"><h3>${translate('rules_choose_title')}</h3><p>${translate('rules_choose_description')}</p><h3>${translate('rules_apply_title')}</h3><p>${translate('rules_apply_description')}</p><h3>${translate('rules_bid_title')}</h3><p>${translate('rules_bid_description')}</p><h3>${translate('rules_pickup_title')}</h3><p>${translate('rules_pickup_description')}</p><h3>${translate('rules_privacy_title')}</h3><p>${translate('rules_privacy_description')}</p></div></div>`;
  }
  /** Format and escape private applicant names for views already restricted to the organizer.
   * @param {PrivateIdentity} user @returns {string}
   */
  function privateName(user) {
    return esc(
      [user.applicant_given_name, user.applicant_family_name].filter(Boolean).join(' ') ||
        user.first_name ||
        translate('applicant_name_missing'),
    );
  }
  /** Compare applications independently of server row ordering.
   * @param {Applicant[]} users @returns {string}
   */
  const applicationSignature = (users) => JSON.stringify([...users].sort((a, b) => a.id - b.id));
  /** Lock sibling actions until completion; the callback can reject stale navigation before updating a view.
   * @param {HTMLButtonElement} button
   * @param {(isCurrent: () => boolean) => Promise<void>} action
   * @returns {Promise<void>}
   */
  async function runAdminAction(button, action) {
    if (button.disabled) return;
    const controls = Array.from(button.closest('.actions')?.querySelectorAll('button') || [button]);
    const disabledBefore = controls.map((control) => control.disabled);
    const currentView = viewVersion,
      currentRoute = location.hash,
      currentRole = state.role;
    controls.forEach((control) => {
      control.disabled = true;
    });
    try {
      await action(
        () =>
          currentView === viewVersion &&
          location.hash === currentRoute &&
          state.role === currentRole &&
          Boolean(state.me?.admin),
      );
    } catch (error) {
      toast(errorMessage(error));
      if (outcomeUnknown(error)) requestImmediateSync();
    } finally {
      controls.forEach((control, index) => {
        control.disabled = disabledBefore[index];
      });
    }
  }
  /** Render organizer navigation; server authorization is still required for every private request.
   * @param {AdminTab} tab @returns {void}
   */
  function adminShell(tab) {
    nav('');
    root.innerHTML = `<div class="page"><div class="admin-header"><div><p class="eyebrow">${translate('admin_kicker')}</p><h1 class="page-title">${translate('admin_title')}</h1></div><a class="back" href="#/">${translate('nav_home')}</a></div><div class="tabs"><button class="tab ${tab === 'lots' ? 'active' : ''}" data-tab="lots">${translate('admin_lots_tab')}</button><button class="tab ${tab === 'archive' ? 'active' : ''}" data-tab="archive">${translate('admin_archive_tab')}</button><button class="tab ${tab === 'applications' ? 'active' : ''}" data-tab="applications">${translate('admin_applications_tab')}</button><button class="tab ${tab === 'new' ? 'active' : ''}" data-tab="new">${translate('admin_create_tab')}</button></div><div id="admin-content"></div></div>`;
    elements('[data-tab]', HTMLButtonElement).forEach(
      (b) =>
        (b.onclick = () => {
          const tab = b.dataset.tab;
          if (tab === 'lots' || tab === 'archive' || tab === 'applications' || tab === 'new')
            admin(tab);
        }),
    );
  }
  /** Render private applications and bind explicit admission decisions with persistent feedback.
   * @param {ApplicationsResponse} data @returns {void}
   */
  function drawApplications(data) {
    const box = findElement('#admin-content', HTMLElement);
    if (!box) return;
    const focus = captureFocus(box);
    state.applications = data.users;
    box.innerHTML = data.users.length
      ? data.users
          .map(
            (u) =>
              `<div class="list-item application-item" data-application="${u.id}" data-current-status="${esc(u.status)}"><div class="application-person"><strong>${privateName(u)} ${u.username ? '@' + esc(u.username) : ''}</strong><small>Telegram ID: ${u.id}</small><span class="application-status ${u.status === 'approved' ? 'approved' : u.status === 'rejected' ? 'rejected' : 'pending'}" role="status">${u.status === 'pending' ? translate('application_status_pending') : u.status === 'approved' ? translate('application_status_approved') : translate('application_status_rejected')}</span><p class="application-result" aria-live="polite" hidden></p></div><div class="actions"><button type="button" class="button secondary" data-user="${u.id}" data-status="approved" ${u.status === 'approved' ? 'disabled' : ''}>${translate('application_approve')}</button><button type="button" class="button danger" data-user="${u.id}" data-status="rejected" ${u.status === 'rejected' ? 'disabled' : ''}>${translate('application_reject')}</button></div></div>`,
          )
          .join('')
      : `<div class="empty"><strong>${translate('applications_empty')}</strong></div>`;
    elements('[data-user]', HTMLButtonElement, box).forEach(
      (b) =>
        (b.onclick = async () => {
          const row = b.closest('[data-application]');
          if (!(row instanceof HTMLElement)) throw new Error('Missing application row.');
          const buttons = Array.from(elements('[data-user]', HTMLButtonElement, row));
          const message = requireElement('.application-result', HTMLElement, row);
          const badge = requireElement('.application-status', HTMLElement, row);
          const original = b.textContent;
          buttons.forEach((button) => {
            button.disabled = true;
          });
          b.textContent = translate('saving_status');
          message.hidden = true;
          try {
            const status = b.dataset.status;
            if (status !== 'approved' && status !== 'rejected')
              throw new Error('Invalid application decision.');
            await post(`/api/admin/users/${Number(b.dataset.user)}`, { status });
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
            message.textContent = `${translate('application_save_failed_prefix')}${errorMessage(e)}`;
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
    restoreFocus(box, focus);
  }
  /** Restore a pending publication's frozen fields and keep its result visible until resolved.
   * @param {HTMLFormElement} form @returns {void}
   */
  function updateCreateAttemptForm(form) {
    const attempt = createAttempt;
    const locked = Boolean(attempt && (attempt.pending || attempt.unknown));
    for (const field of elements('input, textarea', HTMLElement, form)) {
      if (!(field instanceof HTMLInputElement || field instanceof HTMLTextAreaElement)) continue;
      if (attempt && field.name && !(field instanceof HTMLInputElement && field.type === 'file')) {
        const value = attempt.payload.get(field.name);
        if (typeof value === 'string') field.value = value;
      }
      field.disabled = locked;
    }
    const result = requireElement('#new-lot-result', HTMLElement, form);
    result.hidden = !locked;
    result.textContent = locked
      ? translate(attempt?.pending ? 'lot_publication_pending' : 'lot_publication_unknown')
      : '';
    const button = requireElement('button[type=submit]', HTMLButtonElement, form);
    button.textContent = translate(attempt?.unknown ? 'lot_publication_check' : 'lot_publish');
    button.disabled = Boolean(attempt?.pending);
  }
  /** Bind one publication intent; retries keep the frozen payload and operation ID.
   * Changed editable forms become a new intent, while an unresolved write locks its fields.
   * @param {HTMLFormElement} form @returns {void}
   */
  function bindCreateForm(form) {
    /** A deliberate edit after a definitive failure starts another creation intent.
     * @returns {void}
     */
    const invalidate = () => {
      if (createAttempt && !createAttempt.pending && !createAttempt.unknown) createAttempt = null;
    };
    form.addEventListener('input', invalidate);
    form.addEventListener('change', invalidate);
    updateCreateAttemptForm(form);
    form.onsubmit = async (event) => {
      event.preventDefault();
      const button = requireElement('button[type=submit]', HTMLButtonElement, form);
      await runAdminAction(button, async () => {
        if (!createAttempt) {
          const operationId = createOperationId();
          const payload = new FormData(form);
          payload.set('operation_id', operationId);
          createAttempt = { operationId, payload, unknown: false, pending: false };
        }
        const attempt = createAttempt;
        attempt.pending = true;
        updateCreateAttemptForm(form);
        try {
          const result = await post('/api/admin/lots', attempt.payload);
          createAttempt = null;
          state.lots = [...state.lots.filter((lot) => lot.id !== result.lot.id), result.lot];
          toast(translate('lot_published'));
          if (location.hash === '#/admin' && state.me?.admin && state.adminView === 'new')
            await admin('lots');
        } catch (error) {
          if (outcomeUnknown(error)) attempt.unknown = true;
          throw error;
        } finally {
          attempt.pending = false;
          const current = findElement('#new-lot', HTMLFormElement);
          if (current) updateCreateAttemptForm(current);
        }
      });
    };
  }
  /** Open an organizer tab while retaining unsaved create/edit forms during background polling.
   * @param {AdminTab} [tab='lots'] @returns {Promise<void>}
   */
  async function admin(tab = 'lots') {
    if (!state.me || !state.me.admin) {
      root.innerHTML = `<div class="page"><div class="empty"><strong>${translate('admin_access_denied')}</strong></div></div>`;
      return;
    }
    viewVersion++;
    state.adminView = tab;
    state.adminLotId = null;
    adminShell(tab);
    const box = requireElement('#admin-content', HTMLElement);
    if (tab === 'new') {
      box.innerHTML = `<div class="panel"><h3>${translate('lot_create_title')}</h3><p>${translate('lot_common_deadline_hint')}</p><form id="new-lot"><label class="field"><span>${translate('lot_title_label_with_space')}<span class="required">*</span></span><input name="title" maxlength="120" required placeholder="${translate('lot_title_placeholder')}"></label><label class="field"><span>${translate('lot_description_label')}</span><textarea name="description" maxlength="3000" placeholder="${translate('lot_description_placeholder')}"></textarea></label><label class="field"><span>${translate('lot_condition_label')}</span><input name="condition" maxlength="120" placeholder="${translate('lot_condition_placeholder')}"></label><div class="input-row"><label class="field"><span>${translate('lot_start_price_label')}</span><input name="start_price" type="number" min="0" max="100000000" value="0" required></label><label class="field"><span>${translate('lot_step_label')}</span><input name="step" type="number" min="1" max="100000000" value="50" required></label></div><label class="field"><span>${translate('lot_photos_label')}</span><input name="photos" type="file" accept="image/jpeg,image/png,image/webp,image/heic,image/heif" multiple></label><p class="hint">${translate('lot_photos_privacy_hint')}</p><p id="new-lot-result" class="live-change" role="status" hidden></p><button class="button full" type="submit">${translate('lot_publish')}</button></form></div>`;
      bindCreateForm(requireElement('#new-lot', HTMLFormElement));
      return;
    }
    if (tab === 'applications') {
      try {
        const data = await api('/api/admin/applications');
        if (
          state.adminView === 'applications' &&
          findElement('#admin-content', HTMLElement) === box
        )
          drawApplications(data);
      } catch (e) {
        if (findElement('#admin-content', HTMLElement) === box) box.textContent = errorMessage(e);
      }
      return;
    }
    try {
      const data = await api('/api/admin/lots');
      if (state.adminView === tab && findElement('#admin-content', HTMLElement) === box)
        drawAdminLots(data, tab === 'archive');
    } catch (error) {
      if (findElement('#admin-content', HTMLElement) === box) box.textContent = errorMessage(error);
    }
  }
  /** Open organizer controls from either a public lot or the management list.
   * @param {number} id @param {boolean} [edit=false] @returns {void}
   */
  function openLotAdmin(id, edit = false) {
    if (location.hash === '#/admin') {
      adminLot(id, null, edit);
      return;
    }
    state.adminTarget = { id, edit };
    location.hash = '#/admin';
  }
  /** Archive a confirmed lot, reconciling both public and organizer snapshots afterwards.
   * @param {Lot} lot @param {HTMLButtonElement} button @returns {Promise<void>}
   */
  async function removeLot(lot, button) {
    if (!confirm(translate('lot_delete_confirm'))) return;
    await runAdminAction(button, async (isCurrent) => {
      const current = state.lots.find((item) => item.id === lot.id) || lot;
      await post(`/api/admin/lots/${lot.id}/delete`, { expected_bid_count: current.count });
      state.lots = (await api('/api/lots')).lots;
      toast(translate('lot_deleted'));
      if (!isCurrent()) return;
      if (location.hash === '#/admin') await admin('lots');
      else location.hash = '#/';
    });
  }
  /** Render current or archived organizer lots, preserving the focused action during polling.
   * @param {AdminLotsResponse} data @param {boolean} archived @returns {void}
   */
  function drawAdminLots(data, archived) {
    const box = findElement('#admin-content', HTMLElement);
    if (!box) return;
    const focused = document.activeElement;
    const focusedAction =
      focused instanceof HTMLButtonElement && box.contains(focused)
        ? [focused.dataset.manage, focused.dataset.edit, focused.dataset.delete]
        : null;
    state.adminLotsSignature = JSON.stringify(data);
    const lots = data.lots.filter((lot) => lot.deleted === archived);
    box.innerHTML = lots.length
      ? lots
          .map(
            (lot) =>
              `<div class="list-item lot-admin-item"><div><strong>${esc(lot.title)}</strong><small>${lot.deleted ? translate('lot_deleted_badge') : lot.status === 'cancelled' ? translate('lot_badge_cancelled') : lot.status === 'ended' ? translate('lot_badge_ended') : translate('lot_status_active')} · ${lot.count}${translate('bid_count_price_separator')}${money(lot.price === null ? lot.start_price : lot.price)}</small></div><div class="actions"><button class="button secondary" data-manage="${lot.id}">${translate('lot_open')}</button>${!lot.deleted ? `<button class="button secondary" data-edit="${lot.id}" ${lot.status !== 'active' || lot.count > 0 ? 'disabled' : ''}>${translate('lot_edit')}</button><button class="button danger" data-delete="${lot.id}">${translate('lot_delete')}</button>` : ''}</div></div>`,
          )
          .join('')
      : `<div class="empty"><strong>${translate(archived ? 'admin_archive_empty' : 'admin_lots_empty_title')}</strong></div>`;
    elements('[data-manage]', HTMLButtonElement, box).forEach(
      (button) => (button.onclick = () => adminLot(Number(button.dataset.manage))),
    );
    elements('[data-edit]', HTMLButtonElement, box).forEach(
      (button) => (button.onclick = () => adminLot(Number(button.dataset.edit), null, true)),
    );
    elements('[data-delete]', HTMLButtonElement, box).forEach(
      (button) =>
        (button.onclick = () => {
          const lot = lots.find((item) => item.id === Number(button.dataset.delete));
          if (lot) removeLot(lot, button);
        }),
    );
    if (focusedAction)
      elements('button', HTMLButtonElement, box)
        .find(
          (button) =>
            button.dataset.manage === focusedAction[0] &&
            button.dataset.edit === focusedAction[1] &&
            button.dataset.delete === focusedAction[2],
        )
        ?.focus({ preventScroll: true });
  }
  /** Load an organizer's lot controls and candidate snapshot, ignoring responses after route changes.
   * @param {number} id @param {AdminLotResponse | null} [preloaded=null] @param {boolean} [editRequested=false] @returns {Promise<void>}
   */
  async function adminLot(id, preloaded = null, editRequested = false) {
    viewVersion++;
    state.adminView = 'lot';
    state.adminLotId = id;
    try {
      const data = preloaded || (await api(`/api/admin/lots/${id}`));
      if (location.hash !== '#/admin' || state.adminView !== 'lot' || state.adminLotId !== id)
        return;
      state.adminLotSignature = JSON.stringify(data);
      const l = data.lot;
      const delivery = data.candidate_notification;
      const deliveryBlocksActions = delivery?.state === 'sending' || delivery?.state === 'unknown';
      const currentDeliverySent =
        delivery?.state === 'sent' && delivery.candidate_id === data.candidates[0]?.id;
      const recipient = delivery
        ? data.bids.find((person) => person.id === delivery.candidate_id)
        : null;
      const deliveryNotice = delivery
        ? `<section class="panel candidate-notification"><h3>${translate('candidate_notification_title')}</h3><p>${translate('candidate_notification_recipient')}${recipient ? privateName(recipient) : esc(delivery.candidate_id)}</p><p role="status">${translate(`candidate_notification_${delivery.state}`)}</p>${delivery.state === 'unknown' ? `<div class="actions"><button type="button" class="button secondary" data-notification-outcome="sent">${translate('candidate_notification_checked_sent')}</button><button type="button" class="button secondary" data-notification-outcome="not_sent">${translate('candidate_notification_checked_not_sent')}</button></div>` : ''}</section>`
        : '';
      const box = findElement('#admin-content', HTMLElement);
      if (!box) return;
      const focus = captureFocus(box);
      const scrollX = window.scrollX,
        scrollY = window.scrollY;
      box.innerHTML = `<button class="back back-button" id="admin-back">${translate(l.deleted ? 'nav_archive' : 'nav_catalog')}</button><div class="panel"><h3>${esc(l.title)}</h3><p>${l.deleted ? translate('lot_archive_hint') : l.status === 'active' ? translate('lot_bidding_active') : l.status === 'ended' ? translate('lot_ended_title') : translate('lot_status_cancelled')} · ${l.count}${translate('bid_count_suffix')}</p><div class="actions">${l.status === 'active' && l.count === 0 ? `<button class="button secondary" id="edit-lot">${translate('lot_edit')}</button>` : ''}${!l.deleted ? `<button class="button danger" id="delete-lot" ${deliveryBlocksActions ? 'disabled' : ''}>${translate('lot_delete')}</button>` : ''}${l.status === 'active' ? `<button class="button danger" id="cancel-lot" ${deliveryBlocksActions ? 'disabled' : ''}>${translate('lot_cancel')}</button>` : ''}${l.status === 'ended' && data.candidates.length ? `<button class="button danger" id="refuse" ${deliveryBlocksActions ? 'disabled' : ''}>${translate('candidate_refuse')}</button>${data.has_refusals ? `<button class="button secondary" id="notify-candidate" ${deliveryBlocksActions || currentDeliverySent ? 'disabled' : ''}>${translate('candidate_notify')}</button>` : ''}<button class="button secondary" data-fulfill="agreed">${translate('fulfillment_agreed_action')}</button><button class="button secondary" data-fulfill="delivered">${translate('fulfillment_delivered_action')}</button>` : ''}</div><p>${translate('fulfillment_label')}${data.fulfillment === 'agreed' ? translate('fulfillment_agreed') : data.fulfillment === 'delivered' ? translate('fulfillment_delivered') : translate('fulfillment_unset')}</p></div>${deliveryNotice}<h3>${translate('candidates_title')}</h3>${data.candidates.length ? data.candidates.map((u, i) => `<div class="list-item"><span><strong>${i === 0 ? '★ ' : ''}${privateName(u)} ${u.username ? '@' + esc(u.username) : ''}</strong><small>Telegram ID: ${u.id}</small></span><strong>${money(u.amount)}</strong></div>`).join('') : `<p class="hint">${translate('candidates_empty')}</p>`}<h3>${translate('history_title')}</h3>${data.bids.map((b) => `<div class="list-item"><span>${privateName(b)} ${b.username ? '@' + esc(b.username) : ''}</span><strong>${money(b.amount)}</strong></div>`).join('')}`;
      requireElement('#admin-back', HTMLButtonElement).onclick = () =>
        admin(l.deleted ? 'archive' : 'lots');
      const edit = findElement('#edit-lot', HTMLButtonElement);
      if (edit)
        edit.onclick = () => {
          viewVersion++;
          state.adminView = 'edit';
          box.innerHTML = `<button class="back back-button" id="admin-back">${translate('nav_lot')}</button><div class="panel"><h3>${translate('lot_edit_title')}</h3><p>${translate('lot_edit_policy')}</p><p class="live-change" id="live-edit-warning" role="status" hidden></p><form id="edit-form"><label class="field"><span>${translate('lot_title_label')}</span><input name="title" maxlength="120" required value="${esc(l.title)}"></label><label class="field"><span>${translate('lot_description_label')}</span><textarea name="description" maxlength="3000">${esc(l.description)}</textarea></label><label class="field"><span>${translate('lot_condition_label')}</span><input name="condition" maxlength="120" value="${esc(l.condition)}"></label><label class="field"><span>${translate('lot_start_price_label')}</span><input name="start_price" type="number" min="0" required value="${l.start_price}"></label><label class="field"><span>${translate('lot_step_label')}</span><input name="step" type="number" min="1" required value="${l.step}"></label><button class="button full">${translate('lot_save')}</button></form></div>`;
          requireElement('#admin-back', HTMLButtonElement).onclick = () => adminLot(id);
          requireElement('#edit-form', HTMLFormElement).onsubmit = async (e) => {
            e.preventDefault();
            const form = requireElement('#edit-form', HTMLFormElement),
              f = new FormData(form);
            await runAdminAction(
              requireElement('button', HTMLButtonElement, form),
              async (isCurrent) => {
                await post(`/api/admin/lots/${id}/edit`, {
                  title: /** @type {string} */ (f.get('title')),
                  description: /** @type {string} */ (f.get('description')),
                  condition: /** @type {string} */ (f.get('condition')),
                  start_price: Number(f.get('start_price')),
                  step: Number(f.get('step')),
                });
                const x = await api('/api/lots');
                state.lots = x.lots;
                toast(translate('lot_saved'));
                if (isCurrent()) await adminLot(id);
              },
            );
          };
        };
      const remove = findElement('#delete-lot', HTMLButtonElement);
      if (remove) remove.onclick = () => removeLot(l, remove);
      const cancel = findElement('#cancel-lot', HTMLButtonElement);
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
      const refuse = findElement('#refuse', HTMLButtonElement);
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
      const notify = findElement('#notify-candidate', HTMLButtonElement);
      if (notify)
        notify.onclick = () => {
          if (confirm(translate('candidate_notify_confirm')))
            runAdminAction(notify, async (isCurrent) => {
              try {
                await post(`/api/admin/lots/${id}/notify-candidate`, {
                  expected_candidate_id: data.candidates[0].id,
                });
                toast(translate('candidate_notification_sent'));
              } finally {
                if (isCurrent()) await adminLot(id);
                else requestImmediateSync();
              }
            });
        };
      elements('[data-notification-outcome]', HTMLButtonElement, box).forEach(
        (button) =>
          (button.onclick = () => {
            const outcome = button.dataset.notificationOutcome;
            if (!delivery || (outcome !== 'sent' && outcome !== 'not_sent')) return;
            if (!confirm(translate(`candidate_notification_confirm_${outcome}`))) return;
            runAdminAction(button, async (isCurrent) => {
              await post(`/api/admin/lots/${id}/candidate-notification/resolve`, {
                expected_candidate_id: delivery.candidate_id,
                outcome,
              });
              if (isCurrent()) await adminLot(id);
            });
          }),
      );
      elements('[data-fulfill]', HTMLButtonElement, box).forEach(
        (b) =>
          (b.onclick = () =>
            runAdminAction(b, async (isCurrent) => {
              const action = b.dataset.fulfill;
              if (action !== 'agreed' && action !== 'delivered')
                throw new Error('Invalid fulfillment action.');
              await post(`/api/admin/lots/${id}`, { action });
              if (isCurrent()) await adminLot(id);
            })),
      );
      restoreFocus(box, focus);
      window.scrollTo(scrollX, scrollY);
      if (editRequested) edit?.click();
    } catch (e) {
      toast(errorMessage(e));
    }
  }
  /** Dispatch the hash route against the current snapshot and reset scroll for deliberate navigation.
   * @returns {void}
   */
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
    else if (hash === '#/admin') {
      const target = state.adminTarget;
      state.adminTarget = null;
      if (target && state.me?.admin) {
        adminShell('lots');
        adminLot(target.id, null, target.edit);
      } else admin();
    } else catalog();
    window.scrollTo(0, 0);
  }

  // The temporary tunnel does not support server-sent events. Poll while the app is
  // visible, discard stale responses, and never replace a form containing a draft.
  /** @type {ReturnType<typeof setTimeout> | undefined} */
  let syncTimer;
  let syncing = false,
    resyncRequested = false;
  /** Report whether the most recent visible refresh completed successfully.
   * @param {boolean} connected @returns {void}
   */
  function liveIndicator(connected) {
    const indicator = findElement('#live-indicator', HTMLElement);
    if (!indicator) return;
    const text = connected ? translate('connection_online') : translate('connection_offline');
    if (indicator.textContent !== text) indicator.textContent = text;
    indicator.classList.toggle('offline', !connected);
  }
  /** Detect focused or edited fields so polling cannot discard a bid or application draft.
   * @returns {boolean}
   */
  function detailHasDraft() {
    const active = document.activeElement;
    if (active && root.contains(active) && active.matches('input:not([type=checkbox]), textarea'))
      return true;
    const application = findElement('#application-form', HTMLFormElement);
    if (
      application &&
      Array.from(application.querySelectorAll('input')).some((input) => input.value.trim() !== '')
    )
      return true;
    const amount = findElement('#bid-amount', HTMLInputElement);
    return !!(amount && amount.value !== amount.defaultValue);
  }
  /** Update live terms in place, preserving the draft and disabling bids when permissions or status change.
   * @param {Lot} lot @returns {void}
   */
  function patchDetail(lot) {
    /** @param {string} id @returns {HTMLElement | null} Optional text-only view node. */
    const byId = (id) => findElement(`#${id}`, HTMLElement);
    const title = byId('live-title'),
      description = byId('live-description'),
      condition = byId('live-condition');
    const price = byId('live-price'),
      label = byId('live-price-label'),
      hint = byId('live-bid-hint'),
      amount = findElement('#bid-amount', HTMLInputElement);
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
    const productEdit = findElement('#product-edit', HTMLButtonElement);
    if (productEdit) productEdit.disabled = lot.status !== 'active' || lot.count > 0;
    const watch = findElement('#watch', HTMLInputElement),
      participation = findElement('#participation', HTMLInputElement);
    if (watch) watch.checked = lot.watching;
    if (participation) participation.checked = lot.participation;
    const warning = byId('live-change'),
      bid = findElement('#bid', HTMLButtonElement);
    // The signature was produced locally from this exact tuple, not received from the network.
    const previous = detailRenderedSignature
      ? /** @type {DetailSnapshot} */ (JSON.parse(detailRenderedSignature))[0]
      : null;
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
  /** Coalesce refresh requests without overlapping active poll cycles.
   * @returns {void}
   */
  function requestImmediateSync() {
    if (syncing) {
      resyncRequested = true;
      return;
    }
    clearTimeout(syncTimer);
    syncTimer = setTimeout(syncNow, 0);
  }
  /** Refresh visible views with an eight-second poll budget and reject stale mutation, role or view snapshots.
   * @returns {Promise<void>}
   */
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
      /** @type {PollExtra} */
      let extra = null;
      if (route === '#/my' && me.user)
        extra = { kind: 'my', data: await api('/api/my-bids', options) };
      if (route.startsWith('#/lot/') && me.user && me.user.admin)
        extra = {
          kind: 'lot',
          data: await api(`/api/admin/lots/${Number(route.split('/')[2])}`, options),
        };
      if (
        route === '#/admin' &&
        me.user?.admin &&
        (adminView === 'lots' || adminView === 'archive')
      )
        extra = { kind: 'adminLots', data: await api('/api/admin/lots', options) };
      if (route === '#/admin' && me.user && me.user.admin && adminView === 'applications')
        extra = { kind: 'applications', data: await api('/api/admin/applications', options) };
      if (
        route === '#/admin' &&
        me.user &&
        me.user.admin &&
        adminView === 'lot' &&
        adminLotId !== null
      )
        extra = { kind: 'lot', data: await api(`/api/admin/lots/${adminLotId}`, options) };
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
      requireElement('#admin-link', HTMLButtonElement).hidden = !(state.me && state.me.admin);
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
            const focus = captureFocus(root);
            const scrollX = window.scrollX,
              scrollY = window.scrollY;
            const photo = findElement('#main-photo', HTMLImageElement);
            const selected = photo && photo.getAttribute('src');
            detail(lot);
            const restored = findElement('#main-photo', HTMLImageElement);
            if (restored && selected && lot.photos.includes(selected)) restored.src = selected;
            restoreFocus(root, focus);
            window.scrollTo(scrollX, scrollY);
          } else
            root.innerHTML = `<div class="page"><div class="empty"><strong>${translate('lot_not_found_title')}</strong></div></div>`;
          if (oldMe && oldMe.status === 'pending' && state.me && state.me.status === 'approved')
            toast(translate('application_approved_notice'));
        }
        if (lot && extra?.kind === 'lot' && state.me && state.me.admin)
          drawProductHistory(id, extra.data);
      } else if (route === '#/my') {
        if (!state.me && oldMe) my();
        else if (
          extra?.kind === 'my' &&
          JSON.stringify([extra.data.bids, state.lots]) !== state.myBidsSignature
        )
          drawMyBids(extra.data);
      } else if (route === '#/admin') {
        if (!state.me || !state.me.admin) admin();
        else if (
          adminView === 'applications' &&
          extra?.kind === 'applications' &&
          (!state.applications ||
            applicationSignature(extra.data.users) !== applicationSignature(state.applications))
        )
          drawApplications(extra.data);
        else if (
          (adminView === 'lots' || adminView === 'archive') &&
          extra?.kind === 'adminLots' &&
          JSON.stringify(extra.data) !== state.adminLotsSignature
        )
          drawAdminLots(extra.data, adminView === 'archive');
        else if (
          adminView === 'lot' &&
          adminLotId !== null &&
          extra?.kind === 'lot' &&
          JSON.stringify(extra.data) !== state.adminLotSignature
        )
          adminLot(adminLotId, extra.data);
        else if (adminView === 'edit') {
          const lot = state.lots.find((item) => item.id === adminLotId),
            warning = findElement('#live-edit-warning', HTMLElement),
            save = findElement(
              '#edit-form button[type=submit], #edit-form button:not([type])',
              HTMLButtonElement,
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
  requireElement('#admin-link', HTMLButtonElement).onclick = () => {
    location.hash = '#/admin';
  };
  load().then(requestImmediateSync);
})().catch(
  /** @param {unknown} error Startup recovery cannot depend on the downloaded locale. */ (
    error,
  ) => {
    console.error('Application startup failed:', error);
    const root = requireElement('#app', HTMLElement);
    root.textContent = root.dataset.startupError || '';
  },
);
