const main = document.querySelector('#main');
const list = document.querySelector('#campaign-list');
const search = document.querySelector('#campaign-search');
const createLink = document.querySelector('#create-link');
const numbers = new Intl.NumberFormat('ru-RU');
const historyDates = new Intl.DateTimeFormat('ru-RU', { dateStyle: 'medium', timeStyle: 'short' });

const state = {
  campaigns: [], config: null, catalogs: null, catalogPromise: null,
  view: 0, campaignId: null, listError: null,
};

function node(tag, properties = {}, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(properties)) {
    if (key === 'className') element.className = value;
    else element.setAttribute(key, value);
  }
  element.append(...children);
  return element;
}

function button(text, classes, click) {
  const control = node('button', { type: 'button', className: classes }, text);
  control.addEventListener('click', click);
  return control;
}

async function api(path, data) {
  let response;
  try {
    response = await fetch(`/api${path}`, data === undefined ? {} : {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(data),
    });
  } catch {
    throw new Error('Нет связи с приложением. Проверьте, что сервер запущен.');
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const message = Array.isArray(body.detail)
      ? body.detail.map((issue) => issue.msg.replace(/^Value error, /, '')).join(' ')
      : body.detail;
    const error = new Error(message || `Запрос завершился ошибкой (${response.status}).`);
    error.editor = body.editor;
    throw error;
  }
  return body;
}

function setNotice(element, text, error = false) {
  element.textContent = text;
  element.hidden = !text;
  element.classList.toggle('error', error);
}

function showTemplate(name) {
  main.replaceChildren(document.querySelector(`#${name}-template`).content.cloneNode(true));
}

function showFailure(error, retry) {
  const notice = node('p', { className: 'notice error', role: 'alert' }, error.message);
  main.replaceChildren(notice, button('Повторить', 'secondary', retry));
}

function optionList(select, items, placeholder) {
  const first = new Option(placeholder, '');
  first.disabled = true;
  first.selected = true;
  select.replaceChildren(first, ...items.map(({ id, name }) => new Option(`${name} (#${id})`, id)));
  if (items.length === 1) select.value = String(items[0].id);
}

function campaignStatus(status) {
  return status === 'active' ? 'Активна' : 'Выключена';
}

function renderList() {
  const filter = search.value.trim().toLowerCase();
  const campaigns = state.campaigns.filter(({ name, id }) =>
    name.toLowerCase().includes(filter) || `#${id}`.includes(filter));
  if (state.listError) {
    list.replaceChildren(node('p', { className: 'muted' }, state.listError.message),
      button('Обновить список', 'secondary', async () => { await loadList(); navigate(); }));
    return;
  }
  if (!campaigns.length) {
    list.replaceChildren(node('p', { className: 'muted' }, filter ? 'Кампании не найдены.' : 'Создайте первую кампанию.'));
    return;
  }
  list.replaceChildren(...campaigns.map((campaign) => {
    const link = node('a', { className: 'campaign-link', href: `#campaign-${campaign.id}` },
      node('strong', {}, campaign.name),
      node('small', {}, node('span', { className: `dot ${campaign.state}`, 'aria-hidden': 'true' }),
        `#${campaign.id} · ${campaignStatus(campaign.state)}`));
    if (campaign.id === state.campaignId) link.setAttribute('aria-current', 'page');
    return link;
  }));
}

async function loadList() {
  try {
    state.campaigns = await api('/campaigns');
    state.listError = null;
  } catch (error) {
    state.listError = error;
  }
  renderList();
}

function loadCatalogs() {
  state.catalogPromise ??= api('/catalogs').then((data) => {
    state.catalogs = data;
    return data;
  }).catch((error) => {
    state.catalogPromise = null;
    throw error;
  });
  return state.catalogPromise;
}

function catalogName(catalog, id) {
  return state.catalogs?.[catalog]?.find((item) => item.id === id)?.name ?? (id ? `#${id}` : '—');
}

async function renderCreate(view) {
  const catalogs = await loadCatalogs();
  if (view !== state.view) return;
  showTemplate('create');
  const form = main.querySelector('#campaign-form');
  const notice = main.querySelector('#create-message');
  const fields = form.elements;
  const submit = form.querySelector('button[type="submit"]');
  main.querySelector('#name-prefix').textContent = state.config.prefix;
  fields.name.maxLength = 100 - state.config.prefix.length;
  for (const [field, kind, placeholder] of [
    ['offer_id', 'offers', 'Выберите оффер'], ['domain_id', 'domains', 'Выберите домен'],
    ['group_id', 'groups', 'Выберите группу'], ['traffic_source_id', 'sources', 'Выберите источник'],
  ]) optionList(fields[field], catalogs[kind], placeholder);
  const missing = Object.values(catalogs).some((items) => !items.length);
  if (missing) {
    submit.disabled = true;
    setNotice(notice, 'В Keitaro отсутствуют доступные настройки кампании или оффер.', true);
  }

  const preview = () => {
    const country = fields.country.value.toUpperCase();
    main.querySelector('#preview-country').textContent = country || 'Выбранная страна';
    const offer = Number(fields.offer_id.value);
    main.querySelector('#preview-offer').textContent = offer
      ? `100% на ${catalogName('offers', offer)}` : '100% на выбранный оффер';
  };
  preview();
  form.addEventListener('input', preview);

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const data = Object.fromEntries(new FormData(form));
    for (const field of ['offer_id', 'domain_id', 'group_id', 'traffic_source_id']) data[field] = Number(data[field]);
    for (const field of fields) field.disabled = true;
    submit.textContent = 'Создаём в Keitaro…';
    setNotice(notice, 'Сохраняем кампанию и проверяем оба потока.');
    try {
      const campaign = await api('/campaigns', data);
      await loadList();
      if (view === state.view) location.hash = `campaign-${campaign.id}`;
    } catch (error) {
      setNotice(notice, error.message, true);
    } finally {
      for (const field of fields) field.disabled = false;
      submit.textContent = 'Создать кампанию';
    }
  });
}

function flowCaption(stream) {
  if (stream.destination) {
    const geo = stream.countries.join(', ');
    return geo ? `${geo} → ${stream.destination}` : stream.destination;
  }
  return stream.type === 'default' ? 'Остальной трафик' : `Поток #${stream.id}`;
}

async function renderCampaign(campaignId, view, selectedId = null) {
  const { campaign, streams } = await api(`/campaigns/${campaignId}`);
  if (view !== state.view) return;
  showTemplate('editor');
  const refresh = main.querySelector('#refresh-campaign');
  const flowList = main.querySelector('#flows');
  const panel = main.querySelector('#offers-panel');
  const meta = main.querySelector('#campaign-meta');
  const editable = streams.filter((stream) => stream.schema === 'landings');
  let activeStream = null;
  let activePanel = 0;

  main.querySelector('#campaign-name').textContent = campaign.name;
  main.querySelector('#campaign-state').append(node('span', { className: `dot ${campaign.state}` }), campaignStatus(campaign.state));
  const showMeta = () => {
    meta.textContent = `#${campaign.id} · ${catalogName('domains', campaign.domain_id)} · ${catalogName('groups', campaign.group_id)} · ${catalogName('sources', campaign.traffic_source_id)}`;
  };
  showMeta();
  const catalogs = loadCatalogs().then(showMeta, () => {});
  const externalLink = main.querySelector('#open-keitaro');
  if (campaign.admin_url) {
    externalLink.href = campaign.admin_url;
    externalLink.hidden = false;
  }
  refresh.addEventListener('click', () => {
    state.catalogPromise = null;
    navigate(activeStream);
  });

  function showFlows() {
    flowList.replaceChildren(...streams.map((stream, index) => {
      const editableFlow = stream.schema === 'landings';
      const item = editableFlow
        ? button('', 'flow', () => openStream(stream)) : node('div', { className: 'flow' });
      if (editableFlow) item.setAttribute('aria-pressed', String(stream.id === activeStream));
      item.append(node('span', { className: 'route-number', 'aria-label': 'Порядок ' + (index + 1) }, String(index + 1)),
        node('span', {}, node('strong', {}, stream.name), node('small', {}, flowCaption(stream))));
      return item;
    }));
  }

  async function openStream(stream) {
    activeStream = stream.id;
    const panelView = ++activePanel;
    showFlows();
    panel.replaceChildren(node('p', { className: 'muted' }, 'Загружаем офферы…'));
    const path = `/campaigns/${campaignId}/streams/${stream.id}`;
    const target = node('div');
    const context = { target, path, stats: null, statsFailed: false, data: null, notice: '' };
    try {
      const [data] = await Promise.all([api(path), catalogs]);
      if (view !== state.view || panelView !== activePanel) return;
      context.data = data;
      context.history = node('details', { className: 'publication-history' },
        node('summary', {}, 'История применений'), node('div', { className: 'history-body' }));
      context.history.addEventListener('toggle', () => {
        if (context.history.open) loadHistory(context);
      });
      panel.replaceChildren(target, context.history);
      renderOffers(context);
      api(`${path}/stats`).then((stats) => {
        context.stats = stats;
        paintStats(context);
      }).catch(() => {
        context.statsFailed = true;
        paintStats(context);
      });
    } catch (error) {
      if (view !== state.view || panelView !== activePanel) return;
      panel.replaceChildren(node('p', { className: 'notice error', role: 'alert' }, error.message),
        button('Повторить', 'secondary', () => openStream(stream)));
    }
  }

  const chosen = editable.find((stream) => stream.id === selectedId) ?? editable[0];
  if (chosen) openStream(chosen);
  else {
    showFlows();
    panel.replaceChildren(node('p', { className: 'muted' }, 'В кампании пока нет потока со схемой «Офферы».'));
  }
}

function renderOffers(context) {
  const { data, target } = context;
  const title = node('div', { className: 'offer-title' }, node('h2', {}, 'Офферы потока'));
  const status = node('p', { className: 'stats-label', 'data-stats': '' });
  const actions = node('div', { className: 'editor-actions' });
  if (data.dirty) {
    title.append(node('span', { className: 'draft-label' }, data.stale ? 'Конфликт версий' : 'Черновик'));
    const apply = button('Применить в Keitaro', 'primary', () => mutate(context, '/draft/publish'));
    apply.disabled = data.stale;
    actions.append(button('Отменить черновик', 'secondary', () => mutate(context, '/draft/cancel')), apply);
  }
  const header = node('header', { className: 'offer-head' }, node('div', {}, title, status), actions);
  target.replaceChildren(header);

  const active = data.offers.filter((offer) => !offer.removed && offer.state === 'active');
  const colors = new Map(active.map((offer, index) => [offer.offer_id, index % 5]));
  const tape = node('div', { className: 'allocation', 'aria-hidden': 'true' });
  for (const offer of active) {
    if (!offer.share) continue;
    const segment = node('span', { className: `tape-colors-${colors.get(offer.offer_id)}`, title: `${catalogName('offers', offer.offer_id)} · ${offer.share}%` });
    segment.style.flexGrow = String(offer.share);
    tape.append(segment);
  }
  target.append(tape);

  const notice = node('p', { className: 'notice', role: 'status' });
  let text = context.notice;
  if (data.stale) text = 'Офферы изменились в Keitaro. Отмените черновик, затем повторите правки.';
  else if (data.preview?.already_applied) text = 'Keitaro уже сохранил эти офферы и доли. Нажмите «Применить в Keitaro», чтобы завершить сохранение черновика.';
  else if (data.dirty && !active.length) text = 'После применения в потоке не останется активных офферов.';
  setNotice(notice, text, data.stale);
  target.append(notice);
  if (data.preview) target.append(renderPreview(data.preview, data.stale));
  if (data.offers.length) target.append(renderOfferTable(context, colors));
  else target.append(node('p', { className: 'empty' }, 'В этом потоке пока нет офферов.'));
  target.append(renderAddOffer(context));
  paintStats(context);
}

function renderPreview(preview, stale) {
  const panel = node('section', { className: 'change-preview', 'aria-label': 'Предпросмотр изменений' });
  panel.append(node('h3', {}, 'Предпросмотр изменений'));
  if (!preview.baseline_known) {
    panel.append(node('p', { className: 'stats-label' }, 'Для этого черновика доступно сравнение с текущими настройками Keitaro.'));
  }
  if (preview.changes.length) {
    panel.append(renderChanges(preview.changes, preview.baseline_known ? 'Было' : 'Сейчас в Keitaro', stale ? 'Черновик' : 'Станет'));
  } else {
    const reference = preview.baseline_known ? 'с исходными настройками' : 'с текущими настройками Keitaro';
    panel.append(node('p', { className: 'stats-label' }, `Настройки офферов в черновике совпадают ${reference}.`));
  }
  if (preview.external_changes.length) {
    panel.append(node('h4', {}, 'Что изменилось в Keitaro'),
      renderChanges(preview.external_changes, 'Было', 'Сейчас в Keitaro'));
  }
  return panel;
}

function renderChanges(changes, beforeLabel, afterLabel) {
  const head = node('thead', {}, node('tr', {}, ...['Оффер', beforeLabel, afterLabel].map(
    (title) => node('th', { scope: 'col' }, title))));
  const body = node('tbody', {}, ...changes.map(({ offer_id: id, before, after }) => {
    const offer = node('th', { scope: 'row' }, catalogName('offers', id));
    if (!before || !after) {
      offer.append(node('small', { className: before ? 'change-removed' : 'change-added' }, before ? 'Удаление' : 'Добавление'));
    }
    return node('tr', {}, offer, renderChangeValue(before), renderChangeValue(after));
  }));
  return node('div', { className: 'change-table-wrap' }, node('table', { className: 'change-table' }, head, body));
}

function renderChangeValue(value) {
  if (!value) return node('td', { className: 'muted' }, node('span', { 'aria-hidden': 'true' }, '—'), node('span', { className: 'sr-only' }, 'Нет в потоке'));
  const cell = node('td', {}, `${value.share}%`);
  if (value.state !== 'active') cell.append(node('small', { className: 'muted' }, 'Выключен'));
  else if (value.pinned != null) cell.append(node('small', { className: 'muted' }, 'Закреплено'));
  return cell;
}

async function loadHistory(context, beforeId = null) {
  const body = context.history.querySelector('.history-body');
  const request = (context.historyRequest ?? 0) + 1;
  context.historyRequest = request;
  if (beforeId === null) body.replaceChildren(node('p', { className: 'muted' }, 'Загружаем историю…'));
  const more = body.querySelector('.history-more');
  if (more) more.disabled = true;
  const query = beforeId === null ? '' : `?before_id=${beforeId}`;
  try {
    const page = await api(`${context.path}/history${query}`);
    if (request !== context.historyRequest || !body.isConnected) return;
    if (beforeId === null) {
      if (!page.entries.length) {
        body.replaceChildren(node('p', { className: 'muted' }, 'После применения изменений здесь появится история.'));
        return;
      }
      const restore = page.restore;
      const action = restore.reason
        ? node('p', { className: 'stats-label' }, restore.reason)
        : button('Вернуть предыдущее распределение', 'secondary',
          () => mutate(context, `/history/${restore.id}/restore`));
      body.replaceChildren(action, node('ol', { className: 'history-entries' }));
    }
    const records = body.querySelector('.history-entries');
    for (const entry of page.entries) {
      const time = node('time', { datetime: entry.applied_at }, historyDates.format(new Date(entry.applied_at)));
      const detail = node('details', { className: 'history-record' }, node('summary', {}, time));
      if (entry.id === page.restore?.id) detail.open = true;
      detail.append(entry.changes
        ? renderChanges(entry.changes, 'Было', 'Применено')
        : node('p', { className: 'stats-label' }, 'Снимок состояния до применения недоступен.'));
      records.append(node('li', {}, detail));
    }
    body.querySelector('.history-error')?.remove();
    more?.remove();
    if (page.next_before_id !== null) {
      body.append(button('Показать ещё', 'secondary history-more', () => loadHistory(context, page.next_before_id)));
    }
  } catch (error) {
    if (request !== context.historyRequest || !body.isConnected) return;
    const notice = node('p', { className: 'notice error history-error', role: 'status' }, error.message);
    body.querySelector('.history-error')?.remove();
    if (more) {
      more.disabled = false;
      body.append(notice);
    } else {
      body.replaceChildren(notice, button('Повторить', 'secondary', () => loadHistory(context)));
    }
  }
}

function renderOfferTable(context, colors) {
  const titles = [['Оффер', ''], ['Доля, %', 'number'], ['Клики', 'number stat'], ['Конверсии', 'number stat'], ['Действия', 'row-actions']];
  const head = node('thead', {}, node('tr', {}, ...titles.map(([label, className]) => node('th', { scope: 'col', className }, label))));
  const body = node('tbody');
  for (const offer of context.data.offers) {
    const id = offer.offer_id;
    const name = catalogName('offers', id);
    const color = colors.get(id);
    const description = `#${id}${offer.removed ? ' · удалён' : offer.state !== 'active' ? ' · выключен' : ''}`;
    const offerName = node('td', {}, node('div', { className: 'offer-name' },
      node('span', { className: color === undefined ? 'dot' : `dot tape-colors-${color}`, 'aria-hidden': 'true' }),
      node('span', {}, node('strong', {}, name), node('small', {}, description))));
    const percent = node('td', { className: 'number' }, `${offer.share}%`);
    if (!offer.removed && offer.state === 'active' && !context.data.stale) {
      const pinned = offer.pinned !== null;
      const pin = node('form', { className: 'pin' });
      const input = node('input', { type: 'number', min: '1', max: '100', step: '1', value: offer.share, required: '', 'aria-label': `Доля для ${name}, в процентах` });
      input.readOnly = pinned;
      if (pinned) pin.append(input, button('Открепить', 'secondary', () => mutate(context, '/draft', { action: 'unpin', offer_id: id })));
      else pin.append(input, node('button', { type: 'submit', className: 'secondary' }, 'Закрепить'));
      pin.addEventListener('submit', (event) => {
        event.preventDefault();
        if (!pinned) mutate(context, '/draft', { action: 'pin', offer_id: id, share: Number(input.value) });
      });
      percent.replaceChildren(pin);
    }
    const action = offer.removed ? 'restore' : 'remove';
    const edit = button(offer.removed ? 'Вернуть' : 'Удалить', `quiet ${offer.removed ? '' : 'danger'}`,
      () => mutate(context, '/draft', { action, offer_id: id }));
    edit.disabled = context.data.stale;
    body.append(node('tr', { className: offer.removed ? 'removed' : '' }, offerName, percent,
      node('td', { className: 'number stat', 'data-offer': id, 'data-metric': 'clicks' }, '—'),
      node('td', { className: 'number stat', 'data-offer': id, 'data-metric': 'conversions' }, '—'),
      node('td', { className: 'row-actions' }, edit)));
  }
  return node('div', { className: 'table-wrap' }, node('table', { className: 'offer-table' }, head, body));
}

function renderAddOffer(context) {
  const existing = new Set(context.data.offers.map((offer) => offer.offer_id));
  const available = state.catalogs?.offers.filter((offer) => !existing.has(offer.id)) ?? [];
  const form = node('form', { className: 'add-offer' });
  const select = node('select', { required: '', 'aria-label': 'Добавить оффер в поток' });
  const submit = node('button', { type: 'submit', className: 'secondary' }, 'Добавить');
  optionList(select, available, state.catalogs ? 'Выберите оффер' : 'Справочник недоступен');
  if (context.data.stale || !available.length) {
    select.disabled = true;
    submit.disabled = true;
    if (state.catalogs && !available.length) select.options[0].text = 'Все доступные офферы уже показаны';
  }
  form.append(select, submit);
  form.addEventListener('submit', (event) => {
    event.preventDefault();
    mutate(context, '/draft', { action: 'add', offer_id: Number(select.value) });
  });
  return form;
}

function formatDate(value) {
  const [year, month, day] = value.split('-');
  return `${day}.${month}.${year}`;
}

function paintStats({ stats, statsFailed, target }) {
  const label = target.querySelector('[data-stats]');
  if (!target.isConnected || !label) return;
  label.textContent = stats
    ? `Статистика за ${formatDate(stats.from)}–${formatDate(stats.to)} (${stats.timezone})`
    : statsFailed ? 'Статистика Keitaro недоступна' : 'Загружаем статистику…';
  for (const cell of target.querySelectorAll('[data-metric]')) {
    cell.textContent = stats ? numbers.format(stats.offers[cell.dataset.offer]?.[cell.dataset.metric] ?? 0) : '—';
  }
}

async function mutate(context, suffix, body = {}) {
  if (context.busy) return;
  context.busy = true;
  const controls = 'button:not(:disabled), input:not(:disabled), select:not(:disabled)';
  const disabled = [...context.target.querySelectorAll(controls), ...context.history.querySelectorAll(controls)];
  for (const input of disabled) input.disabled = true;
  try {
    context.data = await api(context.path + suffix, { ...body, version: context.data.version });
    context.notice = suffix.endsWith('publish') ? 'Офферы и доли подтверждены в Keitaro.' : '';
    const restoring = suffix.startsWith('/history/');
    if (restoring) context.notice = 'Предыдущее распределение подготовлено в черновике.';
    renderOffers(context);
    if (restoring) context.target.querySelector('.change-preview')?.scrollIntoView({ block: 'nearest' });
  } catch (error) {
    if (error.editor) {
      context.data = error.editor;
      context.notice = '';
      renderOffers(context);
    } else {
      setNotice(context.target.querySelector('.notice'), error.message, true);
    }
  } finally {
    context.busy = false;
    for (const input of disabled) input.disabled = false;
    if (context.history.open && context.history.isConnected) loadHistory(context);
  }
}

function navigate(selectedId = null) {
  const view = ++state.view;
  const match = /^#campaign-(\d+)$/.exec(location.hash);
  const id = match ? Number(match[1]) : state.campaigns[0]?.id;
  const create = location.hash === '#new' || !id;
  state.campaignId = create ? null : id;
  renderList();
  if (create) createLink.setAttribute('aria-current', 'page');
  else createLink.removeAttribute('aria-current');
  main.replaceChildren(node('p', { className: 'muted' }, 'Загружаем данные…'));
  const pending = create ? renderCreate(view) : renderCampaign(id, view, selectedId);
  pending.catch((error) => {
    if (view === state.view) showFailure(error, () => navigate(selectedId));
  });
}

async function start() {
  const [config] = await Promise.all([api('/config'), loadList()]);
  state.config = config;
  const environment = document.querySelector('#environment');
  environment.textContent = config.mode === 'demo' ? 'Демо-режим' : 'Keitaro подключён';
  environment.hidden = false;
  window.addEventListener('hashchange', () => navigate());
  search.addEventListener('input', renderList);
  navigate();
}

start().catch((error) => showFailure(error, start));
