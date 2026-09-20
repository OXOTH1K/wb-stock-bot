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
    table{width:100%;border-collapse:separate;border-spacing:0;min-width:1080px}
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
    @media(max-width:800px){.shell{padding:14px}.cards{grid-template-columns:repeat(2,minmax(0,1fr))}header{align-items:flex-start;flex-direction:column}.panel-head{align-items:flex-start;flex-direction:column}.search{width:100%;min-width:0}}
  </style>
</head>
<body>
<div class="shell">
  <header>
    <div><h1>CRM склада</h1><div class="sub">Остатки товаров · локальный склад · WB · OZON</div></div>
    <button class="btn" onclick="loadInventory()">Обновить экран</button>
  </header>

  <section id="inventoryView">
    <div id="inventoryCards" class="cards"></div>
    <div class="panel">
      <div class="panel-head">
        <div><h2>Остатки товаров</h2><div class="sub">Локальный склад можно редактировать вручную</div></div>
        <input id="inventorySearch" class="search" placeholder="Поиск по артикулу или названию">
      </div>
      <div id="inventoryBody" class="spinner">Загрузка…</div>
    </div>
  </section>

</div>

<script>
let inventoryRows = [];

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
      ['Локальный склад', totals.local],
      ['WB FBS', totals.wb_fbs],
      ['OZON FBS', totals.ozon_fbs],
      ['На складах WB', totals.wb_warehouses],
      ['Склад OZON (FBO)', totals.ozon_fbo],
      ['Расхождений', totals.drift]
    ].map(x => '<div class="card"><div class="n">'+esc(x[1])+'</div><div class="l">'+esc(x[0])+'</div></div>').join('');
    renderInventory();
  } catch (e) { setError('inventoryBody', e); }
}
function renderInventory() {
  const q = document.getElementById('inventorySearch').value.trim().toLowerCase();
  const rows = inventoryRows.filter(x => !q || x.sku.toLowerCase().includes(q) || x.title.toLowerCase().includes(q));
  if (!rows.length) { document.getElementById('inventoryBody').innerHTML='<div class="empty">Ничего не найдено</div>'; return; }
  let html = '<div class="table-wrap"><table><thead><tr><th>Товар</th><th>Локальный склад</th><th>WB FBS</th><th>Склады WB</th><th>OZON FBS</th><th>Склад OZON (FBO)</th><th>Состояние</th></tr></thead><tbody>';
  for (const x of rows) {
    const badges = [];
    if (x.fbs_suppressed) badges.push('<span class="badge warn">WB FBS намеренно 0</span>');
    if (x.ozon_fbs_suppressed) badges.push('<span class="badge warn">OZON FBS намеренно 0</span>');
    for (const channel of (x.drift_channels || [])) {
      badges.push('<span class="badge bad">'+esc(channel)+' ≠ локал</span>');
    }
    if (!badges.length) badges.push('<span class="badge ok">синхронно</span>');
    const state = badges.join(' ');
    const encodedSku = encodeURIComponent(x.sku);
    html += '<tr><td><div class="title">'+esc(x.title||'Без названия')+'</div><div class="sku">'+esc(x.sku)+'</div></td>'+
      '<td><div class="stock-edit">'+
      '<input id="qty-'+x.key+'" type="number" min="0" value="'+esc(x.local)+'">'+
      '<button class="mini" title="Сохранить" onclick="setStock(decodeURIComponent(\''+encodedSku+'\'),\''+x.key+'\')">✓</button></div></td>'+
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

document.getElementById('inventorySearch').addEventListener('input',renderInventory);
loadInventory();
</script>
</body></html>
"""
