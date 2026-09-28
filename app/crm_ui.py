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
    .tabs{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:18px}.tabs button[aria-selected="true"]{background:var(--accent);color:#fff}
    .fbw-group{border:1px solid var(--line);border-radius:10px;margin:12px 0;overflow:hidden}
    .fbw-group summary{cursor:pointer;padding:14px 16px;background:#fafafa;overflow-wrap:anywhere}
    .fbw-group summary .badge{margin-left:10px}.fbw-table-wrap{overflow-x:auto}
    .fbw-table{min-width:800px}.fbw-table th{position:static}.fbw-table td{vertical-align:top;max-width:310px;overflow-wrap:anywhere}
    .order-link{border:0;background:none;padding:0;text-align:left;color:#6d28d9;text-decoration:underline;overflow-wrap:anywhere}
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
    .chart-wrap{width:100%;overflow:hidden;border:1px solid var(--line);border-radius:14px;background:linear-gradient(180deg,#fafaff,#fff);padding:12px 0}.sales-chart{width:100%;height:auto;display:block}.sales-chart text{fill:var(--muted);font-size:12px}
    .chart-grid{stroke:var(--line)}.chart-zero{stroke:#16a085;stroke-dasharray:4 4}.chart-bar{fill:url(#salesBarGradient)}
    .chart-money{stroke:#087f5b;stroke-width:3;stroke-linecap:round;stroke-linejoin:round;fill:none}.chart-dot{fill:#fff;stroke:#087f5b;stroke-width:2}.chart-area{fill:url(#salesAreaGradient)}.chart-hit{fill:transparent;cursor:crosshair}
    .chart-hit:focus{stroke:#087f5b;stroke-width:2}.chart-legend{display:flex;gap:20px;flex-wrap:wrap}.chart-legend span:first-child{color:#7c3aed}.chart-legend span:last-child{color:#087f5b}
    .analytics-summary .card span{display:block;color:var(--muted);font-size:13px;margin-bottom:8px}.analytics-summary .card strong{display:block;font-size:24px}
    .detail-toolbar{padding:18px}.lookup-form select{width:100%;min-height:40px}
  </style>
</head>
<body>
<div class="shell">
  <header>
    <div><h1>CRM склада</h1><div class="sub">Остатки товаров · локальный склад · WB · OZON</div></div>
    <button class="btn" id="refreshButton">Обновить экран</button>
  </header>

  <nav class="tabs" role="tablist" aria-label="Разделы CRM">
    <button class="btn" id="inventoryTab" role="tab" aria-controls="inventoryView" aria-selected="true">Остатки</button>
    <button class="btn" id="lookupTab" role="tab" aria-controls="lookupView" aria-selected="false">Поиск заказа WB</button>
    <button class="btn" id="fbwTab" role="tab" aria-controls="fbwView" aria-selected="false">Заказы FBW</button>
    <button class="btn" id="analyticsTab" role="tab" aria-controls="analyticsView" aria-selected="false">Продажи WB</button>
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
  <section id="fbwView" role="tabpanel" aria-labelledby="fbwTab" hidden>
    <div class="panel" id="fbwList">
      <div class="panel-head"><div><h2>Заказы со складов WB</h2><div class="sub">Выберите период и раскройте артикул, чтобы увидеть заказы товара</div></div></div>
      <form id="fbwForm" class="lookup-form">
        <label for="fbwDateFrom">С даты<input id="fbwDateFrom" class="search" type="date" required></label>
        <label for="fbwDateTo">По дату включительно<input id="fbwDateTo" class="search" type="date" required></label>
        <button class="btn primary" type="submit">Показать заказы</button>
      </form>
      <p class="lookup-help">Период — по дате заказа, по московскому времени. Статус — последнее известное событие из отчётов WB: продажа, отмена или возврат; этапы доставки недоступны. Отчёты обновляются с задержкой. По номеру заказа можно открыть подробности.</p>
      <div id="fbwBody" class="lookup-body" aria-live="polite"></div>
    </div>
    <div class="panel" id="fbwDetail" hidden>
      <div class="detail-toolbar"><button type="button" class="btn" id="fbwBack">← Назад к заказам FBW</button></div>
      <div id="fbwDetailBody" class="lookup-body" aria-live="polite"></div>
    </div>
  </section>
  <section id="analyticsView" role="tabpanel" aria-labelledby="analyticsTab" hidden>
    <div class="panel">
      <div class="panel-head"><div><h2>Продажи и начисления WB</h2><div class="sub">Все продажи WB: FBW и FBS</div></div></div>
      <form id="analyticsForm" class="lookup-form">
        <label for="analyticsSku">Артикул<select id="analyticsSku" class="search"><option value="">Выберите артикул</option></select></label>
        <label for="analyticsDateFrom">С даты<input id="analyticsDateFrom" class="search" type="date" required></label>
        <label for="analyticsDateTo">По дату включительно<input id="analyticsDateTo" class="search" type="date" required></label>
        <button class="btn primary" type="submit">Показать график</button>
      </form>
      <p class="lookup-help">По опубликованным еженедельным финансовым отчётам, по дате операции (МСК). «К перечислению за товар» уже учитывает комиссию WB и платёжные услуги; возвраты уменьшают сумму. Логистика, хранение и другие отдельные расходы не вычитаются. Это начисление, а не подтверждение банковского перевода. Свежие продажи появляются после публикации отчёта.</p>
      <div id="analyticsBody" class="lookup-body" aria-live="polite"></div>
    </div>
  </section>
</div>

<script>
let inventoryRows = [];
let activeView = 'inventory', lookupSequence = 0, lookupTimer = null;
let fbwSequence = 0, fbwTimer = null, fbwNeedsPoll = false;
function showView(view) {
  activeView = view;
  for (const name of ['inventory','lookup','fbw','analytics']) {
    document.getElementById(name+'View').hidden = name !== view;
    document.getElementById(name+'Tab').setAttribute('aria-selected', String(name === view));
  }
  if (view === 'lookup') {
    document.getElementById('orderNumber').focus();
    if (document.getElementById('orderNumber').value.trim()) lookupOrder();
  }
  else { clearTimeout(lookupTimer); lookupSequence++; }
  clearTimeout(detailTimer); detailSequence++;
  if (view === 'fbw') {if (fbwOrderNumber) openFbwOrder(fbwOrderNumber,true); else loadFbwOrders();}
  else { clearTimeout(fbwTimer); fbwSequence++; }
  if (view === 'analytics') loadAnalytics();
  else {clearTimeout(analyticsTimer); analyticsSequence++;}
}
function refreshView() {
  if (activeView === 'inventory') loadInventory();
  else if (activeView === 'fbw') {if (fbwOrderNumber) openFbwOrder(fbwOrderNumber,true); else loadFbwOrders();}
  else if (activeView === 'analytics') loadAnalytics();
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
let fbwOrderNumber = '', detailSequence = 0, detailTimer = null, fbwScroll = 0, fbwReturnFocus = null;
async function openFbwOrder(number, poll=false) {
  if (!poll) {
    fbwScroll = globalThis.scrollY || 0;
    fbwReturnFocus = document.activeElement;
    fbwOrderNumber = number;
    clearTimeout(fbwTimer); fbwSequence++;
    document.getElementById('fbwList').hidden = true;
    document.getElementById('fbwDetail').hidden = false;
    document.getElementById('fbwDetailBody').innerHTML = '<div class="spinner">Ищу заказ…</div>';
    document.getElementById('fbwBack').focus();
  }
  const sequence = ++detailSequence;
  clearTimeout(detailTimer);
  try {
    const data = await api('/api/wb/order-lookup?number='+encodeURIComponent(number));
    if (sequence !== detailSequence || number !== fbwOrderNumber || activeView !== 'fbw') return;
    renderOrderLookup(data, 'fbwDetailBody');
    if (data.syncing) detailTimer = setTimeout(() => openFbwOrder(number, true), 5000);
  } catch(e) {if (sequence === detailSequence) setError('fbwDetailBody', e);}
}
function backToFbw() {
  clearTimeout(detailTimer); detailSequence++; fbwOrderNumber = '';
  document.getElementById('fbwDetail').hidden = true;
  document.getElementById('fbwList').hidden = false;
  if (fbwReturnFocus?.isConnected) fbwReturnFocus.focus({preventScroll:true});
  else document.getElementById('fbwTab').focus({preventScroll:true});
  globalThis.scrollTo?.({top:fbwScroll});
  if (fbwNeedsPoll) fbwTimer = setTimeout(() => loadFbwOrders(true), 5000);
}
let analyticsSequence = 0, analyticsTimer = null;
const rubles = value => Number(value).toLocaleString('ru-RU', {minimumFractionDigits:2, maximumFractionDigits:2})+' ₽';
async function loadAnalytics(poll=false) {
  if (!document.getElementById('analyticsForm').reportValidity()) return;
  const sequence = ++analyticsSequence;
  clearTimeout(analyticsTimer);
  const from = document.getElementById('analyticsDateFrom').value;
  const to = document.getElementById('analyticsDateTo').value;
  const sku = document.getElementById('analyticsSku').value;
  if (from > to) {setError('analyticsBody', 'Дата начала не должна быть позже даты окончания.'); return;}
  if (!poll) document.getElementById('analyticsBody').innerHTML = '<div class="spinner">Загружаю финансовый отчёт…</div>';
  try {
    const data = await api('/api/wb/sales-analytics?'+new URLSearchParams({date_from:from,date_to:to,sku}));
    if (sequence !== analyticsSequence) return;
    const select = document.getElementById('analyticsSku');
    select.innerHTML = '<option value="">Выберите артикул</option>'+data.products.map(p => '<option value="'+esc(p.sku)+'">'+esc(p.sku)+' — '+esc(p.title)+'</option>').join('');
    select.value = sku;
    renderAnalytics(data);
    if (data.syncing && activeView === 'analytics') analyticsTimer = setTimeout(() => loadAnalytics(true), 5000);
  } catch(e) {if (sequence === analyticsSequence) setError('analyticsBody', e);}
}
function renderAnalytics(data) {
  let html = '<p><strong>'+esc(data.date_from)+' — '+esc(data.date_to)+'</strong> · FBW и FBS · МСК</p>';
  if (data.error) html += '<div class="notice">'+esc(data.error)+'</div>';
  if (data.syncing) html += '<p class="source-status">Финансовый отчёт загружается. Это может занять несколько минут; результат обновится автоматически.</p>';
  if (data.updated_at) html += '<p class="source-status">Загружено: '+esc(displayDate(data.updated_at))+(data.stale ? ' · сохранённые данные, требуется обновление' : '')+'</p>';
  if (!data.sku) html += '<div class="empty">Выберите артикул товара.</div>';
  else if (!data.ready) html += '<div class="empty">Финансовые данные пока не получены. Нулевые значения не подставляются.</div>';
  else {
    html += '<div class="cards analytics-summary"><div class="card"><span>Продано</span><strong>'+esc(data.totals.sales)+' шт.</strong></div>'+
      '<div class="card"><span>Возвраты</span><strong>'+esc(data.totals.returns)+' шт.</strong></div>'+
      '<div class="card"><span>К перечислению за товар</span><strong>'+esc(rubles(data.totals.net))+'</strong></div></div>';
    if (!data.has_rows) html += '<div class="notice">В опубликованных отчётах за период нет операций по этому артикулу. Свежие продажи ещё могут не попасть в отчёт.</div>';
    html += salesChart(data.points);
    html += '<details><summary>Данные по дням</summary><div class="fbw-table-wrap"><table class="fbw-table"><thead><tr><th>Дата операции</th><th>Продано, шт.</th><th>Возвраты, шт.</th><th>К перечислению, ₽</th></tr></thead><tbody>'+
      data.points.map(p => '<tr><td>'+esc(p.date)+'</td><td>'+esc(p.sales)+'</td><td>'+esc(p.returns)+'</td><td>'+esc(rubles(p.net))+'</td></tr>').join('')+'</tbody></table></div></details>';
  }
  document.getElementById('analyticsBody').innerHTML = html;
}
// Horizontal tangents give a smooth curve through exact daily values, without overshoot.
function smoothChartPath(coords) {
  if (!coords.length) return '';
  let path='M '+coords[0][0]+' '+coords[0][1];
  for(let i=1;i<coords.length;i++) {
    const [x0,y0]=coords[i-1], [x1,y1]=coords[i], dx=(x1-x0)/3;
    path+=' C '+(x0+dx)+' '+y0+', '+(x1-dx)+' '+y1+', '+x1+' '+y1;
  }
  return path;
}
function salesChart(allPoints) {
  if (!allPoints.length) return '<div class="empty">Нет данных для графика.</div>';
  // A return or a monetary adjustment is meaningful even when there are no sales.
  const first=allPoints.findIndex(p=>Number(p.sales || 0)!==0 || Number(p.returns || 0)!==0 || Number(p.net || 0)!==0);
  if (first<0) return '<div class="empty"><strong>Нет данных для графика</strong>За выбранный период в загруженном отчёте нет ненулевых продаж, возвратов или начислений. Нулевой график скрыт.</div>';
  const points=allPoints.slice(first);
  const notice=first ? '<div class="notice">Начало периода '+esc(allPoints[0].date)+' — '+esc(allPoints[first-1].date)+': в загруженном отчёте нет продаж, возвратов и начислений. Эти дни скрыты на графике. Данные начинаются с '+esc(points[0].date)+'.</div>' : '';
  const left=70, width=760, right=left+width, svgWidth=960;
  const top=36, bottom=248, height=bottom-top;
  const qtyMax=Math.max(4,Math.ceil(Math.max(...points.map(p=>p.sales))/4)*4);
  const cash=points.map(p=>Number(p.net));
  const rawMin=Math.min(0,...cash), rawMax=Math.max(0,...cash);
  const cashPad=Math.max(1,(rawMax-rawMin)*0.1);
  const cashMin=rawMin<0 ? rawMin-cashPad : 0, cashMax=rawMax+cashPad;
  const x=i=>left+(i+0.5)*width/points.length;
  const yQty=v=>bottom-v/qtyMax*height;
  const yCash=v=>bottom-(v-cashMin)/(cashMax-cashMin)*height;
  let svg='<svg viewBox="0 0 '+svgWidth+' 390" class="sales-chart" role="img" aria-label="Продажи и начисления по дням: количество слева, рубли справа">'+
    '<defs><linearGradient id="salesBarGradient" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#a78bfa"/><stop offset="100%" stop-color="#7c3aed"/></linearGradient><linearGradient id="salesAreaGradient" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#10b981" stop-opacity="0.16"/><stop offset="100%" stop-color="#10b981" stop-opacity="0.02"/></linearGradient></defs>'+
    '<text x="16" y="20">Шт.</text><text x="'+(right+16)+'" y="20">Рубли</text>';
  for (let i=0;i<=4;i++) {
    const y=bottom-i*height/4;
    svg+='<line class="chart-grid" x1="'+left+'" x2="'+right+'" y1="'+y+'" y2="'+y+'"/>'+
      '<text text-anchor="end" x="64" y="'+(y+4)+'">'+esc(qtyMax*i/4)+'</text>'+
      '<text x="'+(right+16)+'" y="'+(y+4)+'">'+esc((cashMin+(cashMax-cashMin)*i/4).toLocaleString('ru-RU',{maximumFractionDigits:2}))+'</text>';
  }
  const path=smoothChartPath(cash.map((v,i)=>[x(i),yCash(v)]));
  svg+='<path class="chart-area" d="'+path+' L '+x(points.length-1)+' '+bottom+' L '+x(0)+' '+bottom+' Z"/>';
  svg+='<line class="chart-zero" x1="'+left+'" x2="'+right+'" y1="'+yCash(0)+'" y2="'+yCash(0)+'"/>';
  points.forEach((p,i)=>{const bar=Math.min(24,width/points.length*0.34);svg+='<rect class="chart-bar" rx="5" x="'+(x(i)-bar/2)+'" y="'+yQty(p.sales)+'" width="'+bar+'" height="'+(bottom-yQty(p.sales))+'"/>';});
  svg+='<path class="chart-money" d="'+path+'"/>';
  const dateLabelCount=Math.min(8,points.length);
  const dateLabels=new Set(Array.from({length:dateLabelCount},(_,label)=>
    dateLabelCount===1 ? 0 : Math.round(label*(points.length-1)/(dateLabelCount-1))));
  points.forEach((p,i)=>{
    const tip=p.date+' · Продано: '+p.sales+' · Возвраты: '+p.returns+' · К перечислению: '+rubles(p.net);
    svg+='<circle class="chart-dot" cx="'+x(i)+'" cy="'+yCash(Number(p.net))+'" r="4"/>';
    if (dateLabels.has(i)) svg+='<text text-anchor="middle" x="'+x(i)+'" y="314">'+esc(p.date.slice(5).split('-').reverse().join('.'))+'</text>';
    svg+='<rect tabindex="0" class="chart-hit" data-chart-tip="'+esc(tip)+'" aria-label="'+esc(tip)+'" x="'+(left+i*width/points.length)+'" y="'+top+'" width="'+width/points.length+'" height="335"><title>'+esc(tip)+'</title></rect>';
  });
  return notice+'<p class="chart-legend"><span>■ Продано, шт. — левая шкала</span><span>━ К перечислению, ₽ — правая шкала</span></p><div class="chart-wrap">'+svg+'</svg></div><p id="chartTooltip" class="sub" aria-live="polite">Наведите курсор на день, нажмите на него или выберите клавишей Tab, чтобы увидеть продажи, возвраты и начисление. Плавная линия соединяет дневные значения.</p>';
}

async function loadFbwOrders(poll=false) {
  const form = document.getElementById('fbwForm');
  if (!form.reportValidity()) return;
  const from = document.getElementById('fbwDateFrom').value;
  const to = document.getElementById('fbwDateTo').value;
  const sequence = ++fbwSequence;
  clearTimeout(fbwTimer);
  if (from > to) {setError('fbwBody', 'Дата начала не должна быть позже даты окончания.'); return;}
  // Keep the list while updating so expanded products survive refresh and a return from details.
  if (!poll && !document.getElementById('fbwBody').innerHTML) document.getElementById('fbwBody').innerHTML = '<div class="spinner">Загружаю заказы FBW…</div>';
  try {
    const data = await api('/api/wb/fbw-orders?'+new URLSearchParams({date_from:from,date_to:to}));
    if (sequence !== fbwSequence) return;
    renderFbwOrders(data);
    if (data.syncing && activeView === 'fbw') fbwTimer = setTimeout(() => loadFbwOrders(true), 5000);
  } catch (e) { if (sequence === fbwSequence) setError('fbwBody', e); }
}
function renderFbwOrders(data) {
  fbwNeedsPoll = !!data.syncing;
  const body = document.getElementById('fbwBody');
  const expanded = new Set(Array.from(body.querySelectorAll('details[data-fbw-group][open]'), node => node.dataset.fbwGroup));
  let html = '<p><strong>Период: '+esc(data.date_from)+' — '+esc(data.date_to)+'</strong> · МСК · обе даты включительно</p>'+
    '<div class="cards"><div class="card"><div class="n">'+esc(data.order_count)+'</div><div class="l">Заказов в архиве за период</div></div>'+
    '<div class="card"><div class="n">'+esc(data.product_count)+'</div><div class="l">Артикулов</div></div></div>';
  for (const source of data.sources) {
    html += '<div class="source-status"><strong>'+esc(source.name)+'</strong>: '+
      esc(source.syncing ? 'загрузка…' : (source.updated_at ? 'обновлено '+displayDate(source.updated_at) : 'ещё не загружен'))+
      (source.error ? '<div class="notice">'+esc(source.error)+'</div>' : '')+'</div>';
  }
  if (data.partial) html += '<div class="notice">Данные пока неполные: часть отчётов ещё не загружена или недоступна. Количество и статусы могут измениться после обновления.</div>';
  html += '<p class="sub">Показаны заказы из сохранённого архива. Первичная загрузка охватывает отчёты за 90 дней; более ранние заказы доступны, если уже были сохранены. Нумерация начинается с 1 внутри каждого артикула; сначала новые заказы.</p>';
  if (data.unclassified_count) html += '<div class="notice">Без подтверждённого типа склада: '+esc(data.unclassified_count)+'. Эти заказы не включены в список FBW.</div>';
  if (!data.groups.length) html += '<div class="empty">'+(data.syncing ? 'Архив загружается. Список обновится автоматически.' : 'В доступном архиве нет заказов FBW за выбранный период.')+'</div>';
  for (const group of data.groups) {
    html += '<details class="fbw-group" data-fbw-group="'+esc(group.key)+'"'+(expanded.has(group.key) ? ' open' : '')+'><summary><strong>'+esc(group.title)+'</strong> · '+esc(group.article)+'<span class="badge">Заказов: '+esc(group.count)+'</span></summary>'+
      '<div class="fbw-table-wrap"><table class="fbw-table"><thead><tr><th>№</th><th>Номер заказа</th><th>Заказан</th><th>Склад отгрузки</th><th>Регион назначения</th><th>Статус по отчётам WB</th></tr></thead><tbody>';
    for (const order of group.orders) {
      html += '<tr><td>'+esc(order.index)+'</td><td><button type="button" class="order-link" data-order-number="'+esc(order.lookup_number)+'">'+esc(order.number)+'</button>'+
        (order.number !== order.lookup_number ? '<div class="sku">srid: '+esc(order.lookup_number)+'</div>' : '')+'</td>'+
        '<td>'+esc(displayDate(order.created_at))+'</td><td>'+esc(order.warehouse || 'Не указан')+'</td><td>'+esc(order.region || 'Не указан')+'</td>'+
        '<td>'+esc(order.status.label)+(order.status.at ? '<div class="sub">'+esc(displayDate(order.status.at))+'</div>' : '')+'</td></tr>';
    }
    html += '</tbody></table></div></details>';
  }
  body.innerHTML = html;
}
function renderOrderLookup(data, target='lookupBody') {
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
  document.getElementById(target).innerHTML = html;
}

const esc = (v) => String(v ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
let csrfPromise = null;
async function api(url, options={}) {
  const headers = {'Content-Type':'application/json', ...(options.headers||{})};
  if (options.method && !['GET','HEAD'].includes(options.method.toUpperCase())) {
    if (!csrfPromise) csrfPromise = api('/api/session').catch(error => {csrfPromise=null; throw error;});
    headers['X-CSRF-Token'] = (await csrfPromise).csrf_token;
  }
  const response = await fetch(url, {...options, credentials:'same-origin', headers});
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

    html += '<tr><td><div class="title">'+esc(x.title||'Без названия')+'</div><div class="sku">'+esc(x.sku)+'</div></td>'+
      '<td><div class="stock-edit">'+
      '<input id="qty-'+x.key+'" type="number" min="0" value="'+esc(x.local)+'">'+
      '<button class="mini" title="Сохранить «Мой склад»" data-stock-action="local" data-sku="'+esc(x.sku)+'" data-key="'+esc(x.key)+'">✓</button></div></td>'+
      '<td><div class="stock-edit">'+
      '<input id="available-'+x.key+'" type="number" min="0" value="'+esc(x.available)+'">'+
      '<button class="mini" title="Сохранить «Доступно для заказа»" data-stock-action="available" data-sku="'+esc(x.sku)+'" data-key="'+esc(x.key)+'">✓</button></div></td>'+
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

document.getElementById('refreshButton').addEventListener('click', refreshView);
for (const view of ['inventory','lookup','fbw','analytics']) document.getElementById(view+'Tab').addEventListener('click', () => showView(view));
document.getElementById('inventoryBody').addEventListener('click', event => {
  const button = event.target.closest('button[data-stock-action]');
  if (!button) return;
  const {sku,key,stockAction} = button.dataset;
  if (stockAction === 'local') setStock(sku,key);
  else if (stockAction === 'available') setAvailable(sku,key);
});
const moscowDay = (daysAgo=0) => new Date(Date.now() + 3*3600000 - daysAgo*86400000).toISOString().slice(0,10);
document.getElementById('fbwBack').addEventListener('click', backToFbw);
document.getElementById('analyticsDateFrom').value = moscowDay(29);
document.getElementById('analyticsDateTo').value = moscowDay();
document.getElementById('analyticsForm').addEventListener('submit', event => {event.preventDefault(); loadAnalytics();});
for (const id of ['analyticsDateFrom','analyticsDateTo','analyticsSku']) document.getElementById(id).addEventListener('input', () => {clearTimeout(analyticsTimer); analyticsSequence++; document.getElementById('analyticsBody').innerHTML = '<div class="empty">Нажмите «Показать график» для выбранных параметров.</div>';});
for (const kind of ['pointerover','focusin','click']) document.getElementById('analyticsBody').addEventListener(kind, event => {const node=event.target.closest('[data-chart-tip]'); if(node) document.getElementById('chartTooltip').textContent=node.dataset.chartTip;});
document.getElementById('fbwDateFrom').value = moscowDay(6);
document.getElementById('fbwDateTo').value = moscowDay();
document.getElementById('fbwForm').addEventListener('submit', event => {event.preventDefault(); loadFbwOrders();});
for (const id of ['fbwDateFrom','fbwDateTo']) document.getElementById(id).addEventListener('input', () => {clearTimeout(fbwTimer); fbwSequence++;});
document.getElementById('fbwBody').addEventListener('click', event => {
  const link = event.target.closest('button[data-order-number]');
  if (link) openFbwOrder(link.dataset.orderNumber);
});
document.getElementById('lookupForm').addEventListener('submit', event => {event.preventDefault(); lookupOrder();});
document.getElementById('orderNumber').addEventListener('input', () => { clearTimeout(lookupTimer); lookupSequence++; });
document.getElementById('inventorySearch').addEventListener('input',renderInventory);
loadInventory();
</script>
</body></html>
"""
