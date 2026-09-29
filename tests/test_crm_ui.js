// Exercise the embedded UI without a browser or external dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const page = fs.readFileSync('app/crm_ui.py', 'utf8');
assert.match(page,/а не число заказов из воронки WB/);
assert.match(page,/Чистые выкупы/);
const source = page.match(/<script>([\s\S]*?)<\/script>/)[1];
const nodes = new Map();
function element(id) {
  if (!nodes.has(id)) nodes.set(id, {value:'', innerHTML:'', hidden:false, attributes:{}, listeners:{},
    dataset:{}, disabled:false, textContent:'', text:'',
    setAttribute(k,v) {this.attributes[k]=v;}, addEventListener(k, fn) {this.listeners[k]=fn;},
    focus() {}, reportValidity() {return true;}, querySelectorAll() {return [];}});
  return nodes.get(id);
}
const context = vm.createContext({document:{getElementById:element}, console, URLSearchParams,
  setTimeout:()=>1, clearTimeout:()=>{}, AbortController,
  fetch:async()=>({ok:true, text:async()=>JSON.stringify({items:[], totals:{}})})});
vm.runInContext(source, context);
const empty = {coverage:'Test', sources:[], items:[], syncing:false, partial:false};
context.result = empty;
vm.runInContext('renderOrderLookup(result)', context);
assert.match(element('lookupBody').innerHTML, /В доступных данных заказ не найден/);
context.result = {...empty, syncing:true};
vm.runInContext('renderOrderLookup(result)', context);
assert.match(element('lookupBody').innerHTML, /Архив загружается/);
context.result = {...empty, partial:true};
vm.runInContext('renderOrderLookup(result)', context);
assert.match(element('lookupBody').innerHTML, /Поиск пока неполный/);
const malicious = '<img src=x onerror=alert(1)>';
context.result = {...empty, sources:[{name:malicious,error:malicious}], items:[{
  order_id:'123', article:malicious, status:malicious, model:'FBS',
  events:[{at:'2026-09-28T10:00:00Z', title:malicious, source:'WB', observed:true}],
  raw:[{source:'fbs', data:{article:malicious}}],
}]};
vm.runInContext('renderOrderLookup(result)', context);
assert.ok(!element('lookupBody').innerHTML.includes('<img'));
assert.match(element('lookupBody').innerHTML, /&lt;img/);
assert.match(element('lookupBody').innerHTML, /зафиксировано ботом/);
assert.match(element('lookupBody').innerHTML, /Путь заказа/);
element('inventorySearch').value = 'existing search';
vm.runInContext("showView('lookup')", context);
assert.equal(element('inventoryView').hidden, true);
assert.equal(element('lookupView').hidden, false);
assert.equal(element('lookupTab').attributes['aria-selected'], 'true');
vm.runInContext("showView('inventory')", context);
assert.equal(element('inventorySearch').value, 'existing search');
// A slow response to an old search must not overwrite a newer result.
(async()=> {
  let finishFirst;
  context.fetch = () => new Promise(resolve => {finishFirst = resolve;});
  element('orderNumber').value = 'first';
  const first = vm.runInContext('lookupOrder()', context);
  context.fetch = async()=>({ok:true,text:async()=>JSON.stringify({...empty,coverage:'second result'})});
  element('orderNumber').value = 'second';
  await vm.runInContext('lookupOrder()', context);
  finishFirst({ok:true,text:async()=>JSON.stringify({...empty,coverage:'first result'})});
  await first;
  assert.match(element('lookupBody').innerHTML, /second result/);
  assert.ok(!element('lookupBody').innerHTML.includes('first result'));

  const fbwEmpty = {date_from:'2026-09-20', date_to:'2026-09-21', groups:[], sources:[],
    order_count:0, product_count:0, syncing:false, partial:false, unclassified_count:0};
  context.result = {...fbwEmpty, partial:true};
  vm.runInContext('renderFbwOrders(result)', context);
  assert.match(element('fbwBody').innerHTML, /Данные пока неполные/);
  assert.match(element('fbwBody').innerHTML, /нет заказов FBW/);
  context.result = {...fbwEmpty, syncing:true};
  vm.runInContext('renderFbwOrders(result)', context);
  assert.match(element('fbwBody').innerHTML, /Архив загружается/);
  element('fbwBody').querySelectorAll = () => [{dataset:{fbwGroup:'SKU'}}];
  context.result = {...fbwEmpty, order_count:1, product_count:1, unclassified_count:2,
    groups:[{key:'SKU',title:malicious,article:'SKU',count:1, orders:[{
      index:1, number:'basket', lookup_number:'srid"'+malicious, warehouse:malicious, region:malicious,
      status:{label:malicious, at:''}, created_at:'2026-09-20T09:00:00Z',
    }]}]};
  vm.runInContext('renderFbwOrders(result)', context);
  assert.ok(!element('fbwBody').innerHTML.includes('<img'));
  assert.match(element('fbwBody').innerHTML, /data-fbw-group="SKU" open/);
  assert.match(element('fbwBody').innerHTML, /<td>1<\/td>/);
  assert.match(element('fbwBody').innerHTML, /Заказов: 1/);
  assert.match(element('fbwBody').innerHTML, /Без подтверждённого типа склада: 2/);

  let urls = [];
  context.fetch = async url => {urls.push(url);return {ok:true,text:async()=>JSON.stringify(fbwEmpty)};};
  element('fbwDateFrom').value = '2026-09-20';
  element('fbwDateTo').value = '2026-09-21';
  vm.runInContext("showView('fbw')", context);
  assert.equal(element('lookupView').hidden, true);
  assert.equal(element('fbwView').hidden, false);
  assert.equal(element('fbwTab').attributes['aria-selected'], 'true');
  assert.equal(urls[0], '/api/wb/fbw-orders?date_from=2026-09-20&date_to=2026-09-21');
  context.fetch = async url => {urls.push(url);return {ok:true,text:async()=>JSON.stringify(empty)};};
  element('fbwBody').listeners.click({target:{closest:()=>({dataset:{orderNumber:'exact.srid'}})}});
  assert.equal(element('orderNumber').value, 'second'); // Standalone search remains untouched.
  assert.equal(element('lookupView').hidden, true);
  assert.equal(element('fbwView').hidden, false);
  assert.equal(element('fbwList').hidden, true);
  assert.equal(element('fbwDetail').hidden, false);
  assert.equal(element('fbwTab').attributes['aria-selected'], 'true');
  assert.equal(urls.at(-1), '/api/wb/order-lookup?number=exact.srid');
  assert.equal(element('fbwDateFrom').value, '2026-09-20');
  const listBefore = element('fbwBody').innerHTML;
  vm.runInContext('backToFbw()', context);
  assert.equal(element('fbwBody').innerHTML, listBefore);
  assert.equal(element('fbwDetail').hidden, true);
  assert.equal(element('fbwList').hidden, false);
  // A slow detail request must not reopen the detail after Back.
  context.fetch = () => new Promise(resolve => {finishFirst = resolve;});
  const detailsRequest = vm.runInContext("openFbwOrder('slow')", context);
  vm.runInContext('backToFbw()', context);
  finishFirst({ok:true,text:async()=>JSON.stringify({...empty,coverage:'late detail'})});
  await detailsRequest;
  assert.ok(!element('fbwDetailBody').innerHTML.includes('late detail'));

  element('fbwDateFrom').value = '2026-09-22';
  const requests = urls.length;
  await vm.runInContext('loadFbwOrders()', context);
  assert.equal(urls.length, requests);
  assert.match(element('fbwBody').innerHTML, /Дата начала не должна быть позже/);

  element('fbwDateFrom').value = '2026-09-20';
  context.fetch = () => new Promise(resolve => {finishFirst = resolve;});
  const oldPeriod = vm.runInContext('loadFbwOrders()', context);
  context.fetch = async()=>({ok:true,text:async()=>JSON.stringify({...fbwEmpty,date_from:'2026-09-21'})});
  element('fbwDateFrom').value = '2026-09-21';
  await vm.runInContext('loadFbwOrders()', context);
  finishFirst({ok:true,text:async()=>JSON.stringify(fbwEmpty)});
  await oldPeriod;
  assert.match(element('fbwBody').innerHTML, /Период: 2026-09-21/);
  const analytics = {sku:'SKU', products:[{sku:'SKU',title:malicious}], date_from:'2026-09-20',date_to:'2026-09-21',
    ready:true,syncing:false,error:'',updated_at:'',stale:false,has_rows:true,
    totals:{sales:2,returns:1,net:'-123.45'},points:[{date:'2026-09-20',sales:2,returns:0,net:'100.00'},{date:'2026-09-21',sales:0,returns:1,net:'-223.45'}]};
  context.result=analytics;
  vm.runInContext('renderAnalytics(result)', context);
  assert.match(element('analyticsBody').innerHTML, /<svg/);
  assert.match(element('analyticsBody').innerHTML, /левая шкала/);
  assert.match(element('analyticsBody').innerHTML, /правая шкала/);
  assert.match(element('analyticsBody').innerHTML, /-123,45/);
  assert.match(element('analyticsBody').innerHTML, /Итого за период/);
  assert.match(element('analyticsBody').innerHTML, /<tfoot>/);
  assert.ok(!/NaN|Infinity/.test(element('analyticsBody').innerHTML));
  context.result={sku:'SKU',products:[],date_from:'2026-09-20',date_to:'2026-09-21',ready:true,
    syncing:false,error:'',updated_at:'',stale:false,has_rows:true,unit_cost:'10.00',
    totals:{sales:2,returns:1,net:'150.00'},economics:{net_units:1,cogs:'10.00',wb_charges:'30.00',profit:'111.00',profit_per_unit:'111.00',roi_percent:'1110.00'},
    cost_totals:{delivery:'10',penalties:'3',storage:'2',deductions:'4',acceptance:'5',rebill_logistics:'6',additional_payments:'1'},
    costs:[{delivery:'10',penalties:'3',storage:'2',deductions:'4',acceptance:'5',rebill_logistics:'6',additional_payments:'1'}],
    points:[{date:'2026-09-20',sales:2,returns:1,net:'150.00'}],cost_reasons:[{reason:malicious,amount:'7.00'}]};
  vm.runInContext('renderUnitEconomics(result)',context);
  assert.match(element('unitBody').innerHTML,/Итого прибыль/);
  assert.match(element('unitBody').innerHTML,/Итого прибыль/);
  assert.match(element('unitBody').innerHTML,/Итого за период/);
  assert.match(element('unitBody').innerHTML,/Итого по причинам/);
  assert.match(element('unitBody').innerHTML,/Штрафы и удержания по причинам/);
  assert.ok(!element('unitBody').innerHTML.includes('<img'));
  context.result={...context.result,unit_cost:null,economics:{...context.result.economics,cogs:null,profit:null,profit_per_unit:null,roi_percent:null}};
  vm.runInContext('renderUnitEconomics(result)',context);
  assert.match(element('unitBody').innerHTML,/нельзя рассчитать итоговую прибыль/);
  context.result={...analytics,ready:false,error:malicious,points:[]};
  vm.runInContext('renderAnalytics(result)', context);
  assert.ok(!element('analyticsBody').innerHTML.includes('<svg'));
  assert.ok(!element('analyticsBody').innerHTML.includes('<img'));
  context.result={...analytics,points:[{date:'2026-09-20',sales:0,returns:0,net:'0.00'}]};
  vm.runInContext('renderAnalytics(result)', context);
  assert.ok(!/NaN|Infinity/.test(element('analyticsBody').innerHTML));
  // Trim only the initial inactive days; keep returns, adjustments and later zero days.
  const quiet={sales:0,returns:0,net:'0.00'};
  context.points=[{...quiet,date:'2026-09-18'},{...quiet,date:'2026-09-19'},
    {date:'2026-09-20',sales:2,returns:0,net:'12.34'},{...quiet,date:'2026-09-21'}];
  let chart=vm.runInContext('salesChart(points)',context);
  assert.match(chart,/2026-09-18 — 2026-09-19/);
  assert.match(chart,/Данные начинаются с 2026-09-20/);
  assert.equal((chart.match(/data-chart-tip=/g)||[]).length,2);
  assert.doesNotMatch(chart,/chart-qty-label|chart-cash-label/);
  assert.match(chart,/Возвраты: 0/);
  assert.match(chart,/Выкупы: 2/);
  assert.match(chart,/12,34/);
  assert.match(chart,/>21.09<\/text>/);
  assert.match(chart,/viewBox="0 0 960 390"/);
  assert.match(chart,/<path class="chart-money" d="M [^"]* C /);
  context.points=[{...quiet,date:'2026-09-20'}];
  chart=vm.runInContext('salesChart(points)',context);
  assert.ok(!chart.includes('<svg'));
  assert.match(chart,/Нет данных для графика/);
  for (const activity of [{sales:1},{returns:1},{net:'-1.00'},{net:'0.01'}]) {
    context.points=[{...quiet,date:'2026-09-20',...activity}];
    chart=vm.runInContext('salesChart(points)',context);
    assert.match(chart,/<svg/);
    assert.ok(!/NaN|Infinity/.test(chart));
  }
  assert.equal(vm.runInContext('smoothChartPath([[0,0],[3,30],[6,0]])',context),
    'M 0 0 C 1 0, 2 30, 3 30 C 4 30, 5 0, 6 0');
  context.points=Array.from({length:100},(_,i)=>({date:'2026-09-20',sales:i+1,returns:0,net:'1.00'}));
  chart=vm.runInContext('salesChart(points)',context);
  assert.equal((chart.match(/data-chart-tip=/g)||[]).length,100);
  assert.equal((chart.match(/<text text-anchor="middle"/g)||[]).length,8);
  assert.doesNotMatch(chart,/chart-qty-label|chart-cash-label/);
  assert.match(page,/\.chart-wrap\{width:100%;overflow:hidden/);
  assert.match(page,/\.sales-chart\{width:100%;height:auto/);
  assert.match(page,/th\{[^}]*position:sticky/);
  assert.match(page,/\.fbw-table th\{position:sticky/);

  element('analyticsDateFrom').value='2026-09-20'; element('analyticsDateTo').value='2026-09-21'; element('analyticsSku').value='SKU';
  context.fetch=()=>new Promise(resolve=>{finishFirst=resolve;});
  const oldChart=vm.runInContext('loadAnalytics()',context);
  element('analyticsSku').listeners.input();
  finishFirst({ok:true,text:async()=>JSON.stringify(analytics)});
  await oldChart;
  assert.ok(!element('analyticsBody').innerHTML.includes('<svg'));
  context.fetch=async()=>({ok:true,text:async()=>JSON.stringify(analytics)});
  await vm.runInContext('loadAnalytics()',context);
  assert.match(element('analyticsBody').innerHTML, /<svg/);
  assert.ok(!element('analyticsSku').innerHTML.includes('<img'));

  // A malicious seller article must remain inert HTML data, never executable JS.
  element('inventorySearch').value = '';
  context.badSku = "x');alert(1);//\"><img src=x onerror=alert(1)>";
  vm.runInContext("inventoryRows = [{sku:badSku,title:'Test',key:'p0',local:1,available:1,drift_channels:[]}]; renderInventory()", context);
  assert.ok(!element('inventoryBody').innerHTML.includes('onclick='));
  assert.ok(!element('inventoryBody').innerHTML.includes('<img'));
  assert.match(element('inventoryBody').innerHTML, /data-stock-action="local"/);
  assert.match(element('inventoryBody').innerHTML, /&lt;img/);
  // Authenticated bootstrap supplies CSRF for all write requests.
  const writes = [];
  context.fetch = async (url, options) => {
    writes.push({url,options});
    return {ok:true,text:async()=>JSON.stringify(url === '/api/session' ? {csrf_token:'test-csrf'} : {ok:true})};
  };
  await vm.runInContext("api('/api/inventory/set', {method:'POST',body:JSON.stringify({sku:'SKU',quantity:1})})", context);
  assert.equal(writes[0].url, '/api/session');
  assert.equal(writes[1].options.headers['X-CSRF-Token'], 'test-csrf');
  assert.equal(writes[1].options.credentials, 'same-origin');

  const updateRequests=[];
  context.fetch = async (url, options) => {
    updateRequests.push({url,options});
    return {ok:true,text:async()=>JSON.stringify(
      url.startsWith('/api/update') && !options?.method
        ? {available:true,version:'v1.2.3',current:'1234567'}
        : {message:'Обновление запущено.'})};
  };
  await vm.runInContext('checkCRMUpdate()', context);
  assert.equal(element('crmUpdateButton').hidden, false);
  assert.equal(element('crmCheckUpdatesButton').hidden, true);
  assert.equal(element('crmUpdateButton').dataset.tag, 'v1.2.3');
  await vm.runInContext('checkCRMUpdate(true)', context);
  assert.equal(updateRequests.at(-1).url, '/api/update?force=1');
  assert.equal(element('crmCheckUpdatesButton').disabled, false);
  assert.equal(element('crmCheckUpdatesButton').hidden, true);
  await vm.runInContext('installCRMUpdate()', context);
  assert.equal(element('crmUpdateButton').hidden, true);
  assert.equal(updateRequests.at(-1).url, '/api/update');
  assert.ok(updateRequests.at(-1).options.signal instanceof AbortSignal);
  assert.equal(JSON.parse(updateRequests.at(-1).options.body).tag, 'v1.2.3');
  context.fetch = async()=>({ok:false,statusText:'Conflict',text:async()=>JSON.stringify({message:'sudo: a password is required'})});
  const updateError = await vm.runInContext("api('/api/update',{method:'POST',body:'{}'}).catch(error=>error.message)",context);
  assert.equal(updateError,'sudo: a password is required');
  context.fetch = async()=>({ok:true,text:async()=>JSON.stringify({available:false,message:'Установлена последняя версия.'})});
  await vm.runInContext('checkCRMUpdate(true)', context);
  assert.equal(element('crmUpdateButton').hidden, true);
  assert.equal(element('crmCheckUpdatesButton').hidden, false);
  assert.match(page,/id="crmUpdateButton"[^>]*hidden/);
  console.log('CRM UI: lookup and FBW rendering, escaping, tabs, navigation, dates and request races passed');
})().catch(error => { console.error(error); process.exitCode=1; });
