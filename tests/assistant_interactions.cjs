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
    const count = el(), rows = [];
    const group = {rows,count, querySelector:()=>count, querySelectorAll:()=>rows};
    const body = el({children:rows, insertBefore(row, following){
      groups.forEach(g=>{const i=g.rows.indexOf(row);if(i>=0)g.rows.splice(i,1);});
      rows.splice(following ? rows.indexOf(following) : rows.length,0,row);
    }});
    ids[`category-body-${id}`]=body; group.body=body;
    return group;
  });
  const select = el({value:'1',dataset:{savedValue:'1'}}), status=el(), row={dataset:{tagId:'1', usageCount:'16'}};
  const form={action:'/assistant/tags/category', querySelector:s=>s==='select'?select:status, closest:()=>row};
  groups[0].rows.push(row);
  const calls=[];
  vm.runInNewContext(script, {document:{getElementById:id=>ids[id], querySelectorAll:s=>({
    '.tag-category-form':[form], '.tag-category':groups, '.merge-form':[]
  })[s] || []}, window:{}, URLSearchParams,
  FormData: class {constructor(){assert.equal(select.disabled,undefined);} *[Symbol.iterator](){yield ['category_id',select.value];yield ['csrf_token','signed'];}},
  fetch:async(url,options)=>{calls.push({url,options}); if(response instanceof Error)throw response;return response;}});
  return {ids,groups,select,status,calls,change:async()=>{select.value='2';await select.listeners.change();}};
}
test('categories remain expanded without collapse handlers',()=>{
  const h=harness();
  h.groups.forEach(g=>{assert.equal(g.body.hidden,false);assert.deepEqual(g.body.listeners,{});});
});
test('category selection saves immediately with CSRF then moves row and updates both counts',async()=>{
  const h=harness();
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

test('moving a tag preserves descending usage and id order in its destination', async()=>{
  const h=harness();
  const high={dataset:{tagId:'2',usageCount:'20'}}, tie={dataset:{tagId:'3',usageCount:'16'}}, low={dataset:{tagId:'4',usageCount:'1'}};
  h.groups[1].rows.push(high,tie,low);
  await h.change();
  assert.deepEqual(h.groups[1].rows.map(row=>row.dataset.tagId),['2','1','3','4']);
});
