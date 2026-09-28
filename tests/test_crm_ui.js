// Exercise the embedded UI without a browser or external dependencies.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('app/crm_ui.py', 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];
const nodes = new Map();
function element(id) {
  if (!nodes.has(id)) nodes.set(id, {value:'', innerHTML:'', hidden:false, attributes:{},
    setAttribute(k,v) {this.attributes[k]=v;}, addEventListener() {}, focus() {}, reportValidity() {}});
  return nodes.get(id);
}
const context = vm.createContext({document:{getElementById:element}, console,
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
  console.log('CRM UI: rendering, escaping, tabs, empty/partial states and search races passed');
})().catch(error => { console.error(error); process.exitCode=1; });
