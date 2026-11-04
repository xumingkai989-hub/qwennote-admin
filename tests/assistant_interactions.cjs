const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const script = fs.readFileSync('app/static/assistant.js', 'utf8');
function el(props = {}) {
  return {value:'', textContent:'', dataset:{}, hidden:false, attrs:{}, listeners:{},
    classList: {add(){}, remove(){}},
    addEventListener(event, fn){this.listeners[event] = fn;},
    setAttribute(k,v){this.attrs[k]=v;}, getAttribute(k){return this.attrs[k];}, ...props};
}
function harness(response = {ok:true, json:async()=>({saved:true})}) {
  const ids = Object.fromEntries(['instructions', 'instructions-save', 'instructions-error', 'instructions-count',
    'instructions-saved', 'instructions-form'].map(id=>[id, el()]));
  const groups = [1,2].map(id=> {
    const count = el(), toggle = el({attrs:{'aria-controls':`category-body-${id}`,'aria-expanded':'true'}});
    const rows = [];
    const tbody = {appendChild(row){groups.forEach(g=>{const i=g.rows.indexOf(row);if(i>=0)g.rows.splice(i,1);});rows.push(row);}};
    const group = {rows,toggle,count, querySelector:s=>s === '.category-toggle'?toggle:count, querySelectorAll:()=>rows};
    const body = el({querySelector:()=>tbody,closest:()=>group});
    ids[`category-body-${id}`]=body; group.body=body;
    return group;
  });
  const select = el({value:'1',dataset:{savedValue:'1'}}), status=el(), row={};
  const form={action:'/assistant/tags/category', querySelector:s=>s==='select'?select:status, closest:()=>row};
  groups[0].rows.push(row);
  const calls=[];
  vm.runInNewContext(script, {document:{getElementById:id=>ids[id], querySelectorAll:s=>({
    '.category-toggle':groups.map(g=>g.toggle), '.tag-category-form':[form], '.tag-category':groups, '.merge-form':[]
  })[s] || []}, window:{}, URLSearchParams,
  FormData: class {constructor(){assert.equal(select.disabled,undefined);} *[Symbol.iterator](){yield ['category_id',select.value];yield ['csrf_token','signed'];}},
  fetch:async(url,options)=>{calls.push({url,options}); if(response instanceof Error)throw response;return response;}});
  return {ids,groups,select,status,calls,change:async()=>{select.value='2';await select.listeners.change();}};
}
test('categories start expanded and independently collapse/reopen with accessible state',()=>{
  const h=harness(), g=h.groups[0];
  assert.equal(g.body.hidden,false);
  g.toggle.listeners.click();assert.equal(g.body.hidden,true);assert.equal(g.toggle.attrs['aria-expanded'],'false');
  assert.equal(h.groups[1].body.hidden,false);
  g.toggle.listeners.click();assert.equal(g.body.hidden,false);assert.equal(g.toggle.attrs['aria-expanded'],'true');
});
test('category selection saves immediately with CSRF then moves row and updates both counts',async()=>{
  const h=harness(); h.groups[1].body.hidden=true;
  await h.change();
  assert.equal(h.calls.length,1);assert.equal(h.calls[0].options.body.get('csrf_token'),'signed');
  assert.equal(h.calls[0].options.body.get('category_id'),'2');assert.equal(h.calls[0].options.method,'POST');
  assert.equal(h.select.dataset.savedValue,'2');assert.equal(h.select.disabled,false);
  assert.equal(h.groups[0].count.textContent,'（0）');assert.equal(h.groups[1].count.textContent,'（1）');
  assert.equal(h.groups[1].body.hidden,false);assert.equal(h.status.textContent,'已保存');
});
for(const [name,response] of [['network',new Error('offline')],['csrf',{ok:false,status:403}],
  ['bad body',{ok:true,json:async()=>({})}],['invalid json',{ok:true,json:async()=>{throw new Error('html');}}]]) {
  test(`${name} failure restores old category without moving rows`,async()=>{
    const h=harness(response);await h.change();
    assert.equal(h.select.value,'1');assert.equal(h.select.disabled,false);assert.equal(h.groups[0].rows.length,1);
    assert.equal(h.groups[1].rows.length,0);assert.match(h.status.textContent,/保存失败/);
  });
}
