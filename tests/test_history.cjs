// 原生 Node 回归测试，直接执行模板中的历史计算，不依赖浏览器或第三方包。
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const template=fs.readFileSync(path.join(__dirname,'../limit-up.template.html'),'utf8');
const logic=template.slice(template.indexOf('let historyIndexCache=null;'),template.indexOf("$('boards').addEventListener('click',event=>"));
function stock(code,streak,reason='机器人',extra={}){
 return {code,name:code,streak,reason,reason_status:'available',price:10,change_pct:10,...extra};
}
function context(rows){
 const days=Object.fromEntries(rows.map(([date,stocks])=>[date,{date,stocks}]));
 const ctx=vm.createContext({DATA:{days},esc:s=>String(s),fmt:(n,d=2)=>Number(n).toFixed(d)});
 vm.runInContext(logic,ctx);return ctx;
}
const run=(ctx,expression)=>vm.runInContext(expression,ctx);

test('仅使用截至所选日的记录，同股不同轮行情分开',()=>{
 const ctx=context([
  ['2026-10-19',[stock('A',1)]],['2026-10-20',[stock('A',2)]],
  ['2026-10-21',[stock('A',3)]],['2026-10-22',[]],
  ['2026-10-23',[stock('A',1)]],['2026-10-26',[stock('A',2)]],
 ]);
 assert.equal(run(ctx,"buildHistoryIndex('2026-10-21').episodes.length"),1);
 assert.equal(run(ctx,"buildHistoryIndex('2026-10-21').episodes[0].height"),3);
 assert.equal(run(ctx,"buildHistoryIndex('2026-10-26').episodes.length"),2);
 assert.equal(run(ctx,"buildHistoryIndex('2026-10-26').episodes[1].start"),'2026-10-23');
});

test('板数冲突会拆段；缺失起点不推断同题材先后关系',()=>{
 const ctx=context([
  ['2026-10-19',[stock('A',3),stock('B',1)]],
  ['2026-10-20',[stock('A',4),stock('B',2)]],
  ['2026-10-21',[stock('A',7),stock('B',3)]],
 ]);
 assert.equal(run(ctx,"buildHistoryIndex('2026-10-21').episodes.filter(e=>e.code==='A').length"),2);
 assert.equal(run(ctx,"buildHistoryIndex('2026-10-21').episodes[0].completeStart"),false);
 assert.equal(run(ctx,"relatedEpisodes(buildHistoryIndex('2026-10-21').episodes[0],buildHistoryIndex('2026-10-21')).length"),0);
});

test('按共同题材和启动顺序分类，不将相似字符串当作同一题材',()=>{
 const ctx=context([
  ['2026-10-19',[stock('E',1)]],
  ['2026-10-20',[stock('E',2),stock('A',1),stock('S',1),stock('X',1,'机器人零件')]],
  ['2026-10-21',[stock('A',2),stock('S',2),stock('F',1),stock('X',2,'机器人零件')]],
  ['2026-10-22',[stock('A',3),stock('F',2)]],
 ]);
 const peers=JSON.parse(run(ctx,"JSON.stringify((()=>{const i=buildHistoryIndex('2026-10-22');return relatedEpisodes(i.episodes.find(e=>e.code==='A'),i).map(p=>[p.episode.code,p.relation[0]]);})())"));
 assert.deepEqual(peers,[['E','first'],['S','same'],['F','follow']]);
});

test('当时高度板截止目标峰值日，不能显示之后的高度',()=>{
 const ctx=context([
  ['2026-10-19',[stock('M',1,'重组')]],
  ['2026-10-20',[stock('M',2,'重组'),stock('A',1)]],
  ['2026-10-21',[stock('M',3,'重组'),stock('A',2)]],
  ['2026-10-22',[stock('M',4,'重组'),stock('A',3)]],
  ['2026-10-23',[stock('M',5,'重组')]],
 ]);
 const peer=JSON.parse(run(ctx,"JSON.stringify((()=>{const i=buildHistoryIndex('2026-10-23');return relatedEpisodes(i.episodes.find(e=>e.code==='A'),i)[0];})())"));
 assert.equal(peer.relation[0],'market');
 assert.equal(peer.episode.height,4);
 assert.equal(peer.episode.end,'2026-10-22');
});

test('缺失形态不补造，竞价封板与开板只取明确来源字段',()=>{
 const ctx=context([]);
 assert.equal(run(ctx,"shapeLabels({open_count:null,first_limit_time:null}).length"),0);
 assert.equal(run(ctx,"shapeLabels({open_count:0,first_limit_time:'09:30:00'}).length"),0);
 assert.equal(run(ctx,"JSON.stringify(shapeLabels({open_count:2,first_limit_time:'09:25:01'}))"),'["开板×2","竞价封板"]');
});

test('五年范围之外的数据不纳入；无记录返回空索引',()=>{
 const ctx=context([['2020-10-28',[stock('A',1)]],['2026-10-28',[stock('A',3)]]]);
 assert.equal(run(ctx,"buildHistoryIndex('2026-10-28').savedDates.length"),1);
 assert.equal(run(ctx,"buildHistoryIndex('2019-01-01').episodes.length"),0);
});
