from __future__ import annotations

INDEX_HTML = r"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>CRM склада</title>
  <style>
    :root {
      color-scheme: light;
      --bg:#f5f6f8; --panel:#fff; --text:#15171a; --muted:#6b7280;
      --line:#e5e7eb; --accent:#111827; --ok:#087f5b; --warn:#b45309; --bad:#b42318;
      --shadow:0 1px 2px rgba(0,0,0,.04),0 6px 20px rgba(0,0,0,.05);
    }
    *{box-sizing:border-box} body{margin:0;font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--text)}
    button,input{font:inherit} button{cursor:pointer}
    .shell{max-width:1400px;margin:0 auto;padding:24px}
    header{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:18px}
    h1{font-size:24px;margin:0}.sub{color:var(--muted);font-size:13px}
    .panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;box-shadow:var(--shadow);overflow:visible}
    .panel-head{padding:16px 18px;border-bottom:1px solid var(--line);display:flex;justify-content:space-between;gap:12px;align-items:center}
    .panel-head h2{font-size:16px;margin:0}.actions{display:flex;gap:8px;align-items:center}
    .btn{border:1px solid var(--line);background:#fff;padding:7px 10px;border-radius:8px}
    .btn:hover{background:#f9fafb}.btn.primary{background:var(--accent);color:#fff;border-color:var(--accent)}
    .cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:14px}
    .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
    .card .n{font-size:24px;font-weight:700}.card .l{color:var(--muted);margin-top:3px}
    .table-wrap{overflow:visible}
    table{width:100%;border-collapse:separate;border-spacing:0;min-width:1220px}
    th,td{text-align:left;padding:11px 12px;border-bottom:1px solid var(--line);vertical-align:middle}
    th{font-size:12px;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);background:#fafafa;position:sticky;top:0;z-index:5;box-shadow:0 1px 0 var(--line)}
    tr:last-child td{border-bottom:0}.title{font-weight:700}.sku{color:var(--muted);font-size:12px;margin-top:2px}
    .qty{font-variant-numeric:tabular-nums;font-weight:650}.muted{color:var(--muted)}
    .badge{display:inline-flex;align-items:center;padding:3px 7px;border-radius:999px;font-size:12px;background:#eef2ff;color:#3730a3}
    .badge.ok{background:#ecfdf3;color:var(--ok)}.badge.warn{background:#fff7ed;color:var(--warn)}.badge.bad{background:#fef3f2;color:var(--bad)}
    .stock-edit{display:flex;gap:6px;align-items:center}.stock-edit input{width:82px;border:1px solid var(--line);border-radius:7px;padding:6px 7px}
    .mini{border:1px solid var(--line);background:#fff;border-radius:7px;padding:6px 9px;min-width:34px}
    .empty{padding:48px 20px;text-align:center;color:var(--muted)}.empty strong{display:block;color:var(--text);font-size:18px;margin-bottom:6px}
    .notice{padding:10px 12px;border-radius:9px;background:#fff7ed;color:#92400e;font-size:13px;margin-bottom:12px}
    .search{border:1px solid var(--line);border-radius:8px;padding:8px 10px;min-width:230px}
    .spinner{color:var(--muted);padding:20px}.error{color:var(--bad);padding:16px}
    [hidden]{display:none!important}
    .tabs{display:flex;gap:8px;margin-bottom:18px}.tabs button[aria-selected="true"]{background:var(--accent);color:#fff}
    .lookup-form{padding:18px;display:flex;gap:10px;align-items:end;flex-wrap:wrap}
    .lookup-form label{display:grid;gap:6px;flex:1;min-width:230px}.lookup-form input{width:100%}
    .lookup-body{padding:0 18px 18px}.lookup-card{border-top:1px solid var(--line);padding:20px 0}
    .lookup-card h3{margin:0 0 10px}.lookup-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(210px,1fr));gap:12px;margin:18px 0}
    .lookup-grid dt{font-size:12px;color:var(--muted)}.lookup-grid dd{margin:3px 0 0;overflow-wrap:anywhere}
    .timeline{list-style:none;padding:0 0 0 18px;border-left:2px solid var(--line);margin-left:6px}
    .timeline li{position:relative;padding:0 0 18px 8px}.timeline li:before{content:'';position:absolute;left:-24px;top:5px;width:10px;height:10px;border-radius:50%;background:#7c3aed}
    .lookup-card pre{white-space:pre-wrap;overflow-wrap:anywhere;background:var(--bg);padding:12px;border-radius:8px;font-size:12px}
    .source-status{font-size:12px;color:var(--muted);margin:8px 0 14px}.lookup-help{margin:0 18px 16px;color:var(--muted);font-size:13px}
    @media(max-width:800px){.shell{padding:14px}.cards{grid-template-columns:repeat(2,minmax(0,1fr))}header{align-items:flex-start;flex-direction:column}.panel-head{align-items:flex-start;flex-direction:column}.search{width:100%;min-width:0}}
  </style>
</head>
<body>
<div class="shell">
  <header>
    <div><h1>CRM склада</h1><div class="sub">Остатки товаров · локальный склад · WB · OZON</div></div>
    <button class="btn" onclick="refreshView()">Обновить экран</button>
  </header>

  <nav class="tabs" role="tablist" aria-label="Разделы CRM">
    <button class="btn" id="inventoryTab" role="tab" aria-controls="inventoryView" aria-selected="true" onclick="showView('inventory')">Остатки</button>
    <button class="btn" id="lookupTab" role="tab" aria-controls="lookupView" aria-selected="false" onclick="showView('lookup')">Поиск заказа WB</button>
  </nav>
  <section id="inventoryView" role="tabpanel" aria-labelledby="inventoryTab">
    <div id="inventoryCards" class="cards"></div>
    <div class="panel">
      <div class="panel-head">
        <div><h2>Остатки товаров</h2><div class="sub">«Мой склад» — полный физический остаток; «Доступно для заказа» — сколько из него можно продавать сейчас</div></div>
        <input id="inventorySearch" class="search" placeholder="Поиск по артикулу или названию">
      </div>
      <div id="inventoryBody" class="spinner">Загрузка…</div>
    </div>
  </section>

  <section id="lookupView" role="tabpanel" aria-labelledby="lookupTab" hidden>
    <div class="panel">
      <div class="panel-head"><div><h2>Найти заказ Wildberries</h2><div class="sub">FBS и FBW · сведения о заказе и его история</div></div></div>
      <form id="lookupForm" class="lookup-form">
        <label for="orderNumber">Номер заказа<input id="orderNumber" class="search" required maxlength="200" autocomplete="off" placeholder="ID сборочного задания, rid/srid или gNumber"></label>
        <button class="btn primary" type="submit">Найти заказ</button>
      </form>
      <p class="lookup-help">Поиск по всем заказам, доступным в архиве бота и отчётах WB. Первая загрузка архива может занять несколько минут. Номер из приложения покупателя может отличаться от номера в кабинете продавца.</p>
      <div id="lookupBody" class="lookup-body" aria-live="polite"><div class="empty">Введите номер, чтобы увидеть сведения о заказе и его путь.</div></div>
    </div>
  </section>
</div>

<script>
let inventoryRows = [];
let activeView = 'inventory', lookupSequence = 0, lookupTimer = null;
function showView(view) {
  activeView = view;
  for (const name of ['inventory','lookup']) {
    document.getElementById(name+'View').hidden = name !== view;
    document.getElementById(name+'Tab').setAttribute('aria-selected', String(name === view));
  }
  if (view === 'lookup') {
    document.getElementById('orderNumber').focus();
    if (document.getElementById('orderNumber').value.trim()) lookupOrder();
  }
  else { clearTimeout(lookupTimer); lookupSequence++; }
}
function refreshView() {
  if (activeView === 'inventory') loadInventory();
  else lookupOrder();
}
function displayDate(value) {
  if (!value) return 'Неизвестно';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('ru-RU', {timeZoneName:'short'});
}
async function lookupOrder(poll=false) {
  const number = document.getElementById('orderNumber').value.trim();
  if (!number) { document.getElementById('orderNumber').reportValidity(); return; }
  const sequence = ++lookupSequence;
  clearTimeout(lookupTimer);
  if (!poll) document.getElementById('lookupBody').innerHTML = '<div class="spinner">Ищу заказ…</div>';
  try {
    const data = await api('/api/wb/order-lookup?number='+encodeURIComponent(number));
    if (sequence !== lookupSequence) return;
    renderOrderLookup(data);
    if (data.syncing && activeView === 'lookup') lookupTimer = setTimeout(() => lookupOrder(true), 5000);
  } catch (e) { if (sequence === lookupSequence) setError('lookupBody', e); }
}
function renderOrderLookup(data) {
  let html = '<p class="sub">'+esc(data.coverage)+'</p>';
  for (const source of data.sources) {
    html += '<div class="source-status"><strong>'+esc(source.name)+'</strong>: '+
      esc(source.syncing ? 'загрузка…' : (source.updated_at ? 'обновлено '+displayDate(source.updated_at) : 'ещё не загружен'))+
      (source.error ? '<div class="notice">'+esc(source.error)+'</div>' : '')+'</div>';
  }
  if (!data.items.length) html += '<div class="empty"><strong>'+(data.syncing ? 'Архив загружается' : 'В доступных данных заказ не найден')+'</strong>'+
    (data.syncing ? 'Результат обновится автоматически.' : (data.partial ? 'Часть источников недоступна. Поиск пока неполный.' : 'Проверьте номер. Более старого заказа или заказа без подтверждённой оплаты может не быть в отчётах WB.'))+'</div>';
  if (data.items.length > 1) html += '<p>Найдено позиций: '+data.items.length+'. У каждой позиции своя история.</p>';
  for (const item of data.items) {
    html += '<article class="lookup-card"><h3>Заказ '+esc(item.order_id || item.srid || data.number)+'</h3>'+
      '<span class="badge">'+esc(item.model)+'</span> <strong>'+esc(item.status)+'</strong>'+
      '<div class="sub">'+(item.status_at ? 'Статус проверен: '+esc(displayDate(item.status_at)) : 'Точное текущее местоположение неизвестно')+'</div>';
    if (item.last_report_event) html += '<p>Последнее событие в отчётах: <strong>'+esc(item.last_report_event.title)+'</strong> · '+esc(displayDate(item.last_report_event.at))+'</p>';
    const fields = [['Артикул продавца',item.article],['Артикул WB',item.nm_id],['Заказан',displayDate(item.created_at)],['ID сборочного задания',item.order_id],['rid / srid',item.srid],['Номер заказа в отчёте',item.group_number],['Поставка FBS',item.supply_id],['Склад отгрузки (не текущее местоположение)',item.warehouse],['Регион назначения',item.destination]];
    html += '<dl class="lookup-grid">'+fields.map(([label,value]) => '<div><dt>'+esc(label)+'</dt><dd>'+esc(value || '—')+'</dd></div>').join('')+'</dl>';
    html += '<h3>Путь заказа</h3><p class="sub">Подтверждённые события и наблюдения бота. Время наблюдения — когда бот увидел статус, а не точное время перехода. Даты отображаются в вашем часовом поясе.</p>';
    html += item.events.length ? '<ol class="timeline">'+item.events.map(event => '<li><strong>'+esc(event.title)+'</strong><div>'+esc(displayDate(event.at))+(event.observed ? ' · зафиксировано ботом' : '')+'</div><div class="sub">'+esc(event.source)+'</div></li>').join('')+'</ol>' : '<p class="muted">Датированных событий пока нет.</p>';
    html += '<details><summary>Все полученные данные WB</summary>'+item.raw.map(row => '<h4>'+esc(row.source)+'</h4><div class="sub">Получено: '+esc(displayDate(row.observed_at))+'</div><pre>'+esc(JSON.stringify(row.data,null,2))+'</pre>').join('')+'</details></article>';
  }
  document.getElementById('lookupBody').innerHTML = html;
}

const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
async function api(url, options={}) {
  const response = await fetch(url, {headers:{'Content-Type':'application/json', ...(options.headers||{})}, ...options});
  const text = await response.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = {error:text}; }
  if (!response.ok) throw new Error(data?.error || response.statusText);
  return data;
}
function setError(id, err){ document.getElementById(id).innerHTML = '<div class="error">'+esc(err.message || err)+'</div>'; }

async function loadInventory() {
  try {
    const data = await api('/api/inventory');
    inventoryRows = data.items;
    const totals = data.totals;
    document.getElementById('inventoryCards').innerHTML = [
      ['Мой склад', totals.local],
      ['Доступно для заказа', totals.available],
      ['WB FBS', totals.wb_fbs],
      ['Ozon FBS', totals.ozon_fbs],
      ['FBW', totals.wb_warehouses],
      ['FBO', totals.ozon_fbo],
      ['Расхождений', totals.drift]
    ].map(x => '<div class="card"><div class="n">'+esc(x[1])+'</div><div class="l">'+esc(x[0])+'</div></div>').join('');
    renderInventory();
  } catch (e) { setError('inventoryBody', e); }
}
function renderInventory() {
  const q = document.getElementById('inventorySearch').value.trim().toLowerCase();
  const rows = inventoryRows.filter(x => !q || x.sku.toLowerCase().includes(q) || x.title.toLowerCase().includes(q));
  if (!rows.length) { document.getElementById('inventoryBody').innerHTML='<div class="empty">Ничего не найдено</div>'; return; }
  let html = '<div class="table-wrap"><table><thead><tr><th>Товар</th><th>Мой склад</th><th>Доступно для заказа</th><th>WB FBS</th><th>FBW</th><th>Ozon FBS</th><th>FBO</th><th>Состояние</th></tr></thead><tbody>';
  for (const x of rows) {
    const badges = [];
    if (x.fbs_suppressed) {
      badges.push('<span class="badge warn">WB FBS намеренно 0</span>');
    }
    if (x.ozon_fbs_suppressed) {
      badges.push('<span class="badge warn">Ozon FBS намеренно 0</span>');
    }
    for (const channel of (x.drift_channels || [])) {
      badges.push('<span class="badge bad">'+esc(channel)+' ≠ доступно</span>');
    }
    if (!badges.length) badges.push('<span class="badge ok">синхронно</span>');
    const state = badges.join(' ');
    const encodedSku = encodeURIComponent(x.sku);
    html += '<tr><td><div class="title">'+esc(x.title||'Без названия')+'</div><div class="sku">'+esc(x.sku)+'</div></td>'+
      '<td><div class="stock-edit">'+
      '<input id="qty-'+x.key+'" type="number" min="0" value="'+esc(x.local)+'">'+
      '<button class="mini" title="Сохранить «Мой склад»" onclick="setStock(decodeURIComponent(\''+encodedSku+'\'),\''+x.key+'\')">✓</button></div></td>'+
      '<td><div class="stock-edit">'+
      '<input id="available-'+x.key+'" type="number" min="0" value="'+esc(x.available)+'">'+
      '<button class="mini" title="Сохранить «Доступно для заказа»" onclick="setAvailable(decodeURIComponent(\''+encodedSku+'\'),\''+x.key+'\')">✓</button></div></td>'+
      '<td>'+(x.wb_fbs === null ? '<span class="muted">—</span>' : '<span class="qty">'+esc(x.wb_fbs)+'</span>')+'</td>'+
      '<td>'+(x.wb_warehouses === null ? '<span class="muted">—</span>' : '<span class="qty">'+esc(x.wb_warehouses)+'</span>')+'</td>'+
      '<td>'+(x.ozon_fbs === null ? '<span class="muted">—</span>' : '<span class="qty">'+esc(x.ozon_fbs)+'</span>')+'</td>'+
      '<td>'+(x.ozon_fbo === null ? '<span class="muted">—</span>' : '<span class="qty">'+esc(x.ozon_fbo)+'</span>')+'</td>'+
      '<td>'+state+'</td></tr>';
  }
  html += '</tbody></table></div>';
  document.getElementById('inventoryBody').innerHTML = html;
}
async function setStock(sku, key) {
  const el = document.getElementById('qty-'+key);
  const quantity = Number(el.value);
  try { await api('/api/inventory/set',{method:'POST',body:JSON.stringify({sku,quantity})}); await loadInventory(); }
  catch(e){ alert(e.message); }
}
async function setAvailable(sku, key) {
  const el = document.getElementById('available-'+key);
  const quantity = Number(el.value);
  try { await api('/api/inventory/available/set',{method:'POST',body:JSON.stringify({sku,quantity})}); await loadInventory(); }
  catch(e){ alert(e.message); }
}

document.getElementById('lookupForm').addEventListener('submit', event => {event.preventDefault(); lookupOrder();});
document.getElementById('orderNumber').addEventListener('input', () => { clearTimeout(lookupTimer); lookupSequence++; });
document.getElementById('inventorySearch').addEventListener('input',renderInventory);
loadInventory();
</script>
</body></html>
"""
