const assert = require('node:assert/strict');

module.exports = async function inspect(section, weeks, javascript = true) {
  const facts = await section.evaluate(section => {
    const plot = section.querySelector('.board-comparison-plot');
    const rows = [...plot.querySelectorAll('[data-week]')];
    const box = e => ({x: +e.getAttribute('x'), y: +e.getAttribute('y'),
      width: +e.getAttribute('width'), height: +e.getAttribute('height')});
    const colors = {};
    for (const rect of plot.querySelectorAll('rect')) (colors[rect.dataset.category] ??= []).push(getComputedStyle(rect).fill);
    const bands = [...plot.querySelectorAll('polygon')].map(p => {
      const a = rows.findIndex(r => r.dataset.week === p.dataset.week1);
      const b = rows.findIndex(r => r.dataset.week === p.dataset.week2);
      const segment = row => [...row.querySelectorAll('rect')].find(r => r.dataset.category === p.dataset.category);
      const start = segment(rows[a]), end = segment(rows[b]);
      const r1 = box(start), r2 = box(end);
      return {a, b, points: [...p.points].map(v => [v.x, v.y]),
        expected: [[r1.x+r1.width,r1.y],[r2.x,r2.y],[r2.x,r2.y+r2.height],[r1.x+r1.width,r1.y+r1.height]],
        opacity: getComputedStyle(p).fillOpacity, color: getComputedStyle(p).fill, expectedColor: getComputedStyle(start).fill};
    });
    const labels = [...section.querySelectorAll('[data-week-label]')].filter(e => getComputedStyle(e).display !== 'none');
    return {overflow:document.documentElement.scrollWidth>innerWidth, colors, bands,
      expectedBands: rows.slice(1).reduce((n,row,i)=>n+[...row.querySelectorAll('rect')].filter(e=>[...rows[i].querySelectorAll('rect')].some(r=>r.dataset.category===e.dataset.category)).length,0),
      rows:rows.map(row=>({day:row.dataset.week,cx:+row.dataset.cx,minutes:+row.dataset.minutes,
        height:+row.dataset.height,segments:[...row.querySelectorAll('rect')].map(e=>({...box(e),pixelWidth:e.getBoundingClientRect().width}))})),
      labels:labels.map(e=>{const text=e.querySelector('text'),r=text.getBoundingClientRect();return {left:r.left,right:r.right,fontSize:getComputedStyle(text).fontSize,text:text.textContent};}),
      totals:[...section.querySelectorAll('[data-total-label]')].map(e=>{const r=e.getBoundingClientRect();return {day:e.dataset.totalLabel,size:getComputedStyle(e).fontSize,left:r.left,right:r.right,top:r.top,bottom:r.bottom};}),
      bounds:{left:plot.getBoundingClientRect().left,right:plot.getBoundingClientRect().right,top:section.querySelector('.board-comparison-svg').getBoundingClientRect().top},
      grid:[...plot.querySelectorAll('line')].map(e=>getComputedStyle(e).strokeOpacity),
      plotWidth:plot.getBoundingClientRect().width,viewBoxWidth:plot.viewBox.baseVal.width,
      donutColors:[...document.querySelectorAll('[data-allocation-panel="by_category"] .board-legend-row')].map(e=>({name:e.querySelector('.board-allocation-name').textContent.trim(),color:getComputedStyle(e.querySelector('.board-legend-swatch')).backgroundColor})),
      legendColors:[...section.querySelectorAll('li[data-category]')].map(e=>({name:e.dataset.category,color:getComputedStyle(e.querySelector('.board-legend-swatch')).backgroundColor}))};
  });
  assert.equal(facts.overflow,false);
  assert.equal(facts.rows.length,weeks);
  assert.deepEqual(facts.rows.map(r=>r.day),facts.rows.map(r=>r.day).sort());
  const maximum=Math.max(...facts.rows.map(r=>r.minutes));
  facts.rows.forEach((row,i)=>{
    if(i) assert.ok(row.cx>facts.rows[i-1].cx);
    assert.ok(Math.abs(row.height-(maximum ? row.minutes/maximum*240/1.1 : 0))<.01);
    let bottom=264;
    for(const r of row.segments) {
      assert.ok(Math.abs(r.y+r.height-bottom)<.01);
      assert.ok(r.pixelWidth>=8);
      assert.ok(Math.abs(r.pixelWidth-facts.plotWidth/weeks*.6)<.1);
      bottom=r.y;
    }
    assert.ok(Math.abs(264-bottom-row.height)<.01);
  });
  assert.equal(facts.bands.length,facts.expectedBands);
  for(const band of facts.bands) {
    assert.equal(band.b,band.a+1);
    assert.equal(band.opacity,'0.18'); assert.equal(band.color,band.expectedColor);
    band.points.forEach((p,i)=>p.forEach((v,j)=>assert.ok(Math.abs(v-band.expected[i][j])<.01)));
  }
  assert.equal(facts.grid.length,maximum?5:1);
  assert.ok(facts.grid.every(opacity=>opacity==='0.5'));
  facts.labels.forEach((label,i)=>{
    assert.equal(label.fontSize,'12px'); assert.match(label.text,/^\d\d-\d\d$/);
    assert.ok(label.left>=facts.bounds.left && label.right<=facts.bounds.right);
    if(i) assert.ok(label.left>facts.labels[i-1].right);
  });
  for(const label of facts.totals) {
    assert.equal(label.size,'11px');
    assert.ok(facts.rows.find(r=>r.day===label.day).height>=30);
  }
  if(javascript) facts.totals.forEach((label,i)=>{
    assert.ok(label.left>=facts.bounds.left && label.right<=facts.bounds.right && label.top>=facts.bounds.top);
    for(const prior of facts.totals.slice(0,i)) assert.ok(label.left>=prior.right || label.right<=prior.left || label.top>=prior.bottom || label.bottom<=prior.top);
  });
  for(const values of Object.values(facts.colors)) assert.equal(new Set(values).size,1);
  for(const item of facts.donutColors) assert.deepEqual(facts.legendColors.find(x=>x.name===item.name),item);
  return facts;
};
