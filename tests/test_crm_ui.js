// Exercise the embedded UI without a browser or external dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/crm_ui.py', 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
const nodes = new Map();
function element(id) {
  if (!nodes.has(id)) nodes.set(id, {value:'', innerHTML:'', hidden:false, attributes:{}, listeners:{},
    setAttribute(k,v) {this.attributes[k]=v;}, addEventListener(k, fn) {this.listeners[k]=fn;},
    focus() {}, reportValidity() {return true;}, querySelectorAll() {return [];}});
  return nodes.get(id);
}
const context = vm.createContext({document:{getElementById:element}, console, URLSearchParams,
  setTimeout:()=>1, clearTimeout:()=>{},
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
  assert.equal(element('orderNumber').value, 'exact.srid');
  assert.equal(element('lookupView').hidden, false);
  assert.equal(element('fbwView').hidden, true);
  assert.equal(element('lookupTab').attributes['aria-selected'], 'true');
  assert.equal(urls.at(-1), '/api/wb/order-lookup?number=exact.srid');
  assert.equal(element('fbwDateFrom').value, '2026-09-20');

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
  console.log('CRM UI: lookup and FBW rendering, escaping, tabs, navigation, dates and request races passed');
})().catch(error => { console.error(error); process.exitCode=1; });
