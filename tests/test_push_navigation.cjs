const test=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const fs=require('node:fs');
function harness(){
 const handlers={},shown=[],opened=[];
 const context={self:{addEventListener:(n,f)=>handlers[n]=f,registration:{showNotification:(title,options)=>{shown.push(options);return Promise.resolve();}}},clients:{openWindow:url=>{opened.push(url);return Promise.resolve();}}};
 vm.runInNewContext(fs.readFileSync('mobile/sw.js','utf8'),context);
 return {handlers,shown,opened};
}
test('push retains exact notification ID and click opens its screen',async()=>{
 const h=harness(),id='a'.repeat(32);let pending;
 h.handlers.push({data:{json:()=>({notification_id:id})},waitUntil:p=>pending=p});await pending;
 assert.equal(h.shown[0].data.url,'/?screen=guardian-view&notification='+id);
 h.handlers.notificationclick({notification:{data:h.shown[0].data,close(){}},waitUntil:p=>pending=p});await pending;
 assert.equal(h.opened[0],h.shown[0].data.url);
});
test('legacy notifications and untrusted URLs open guardian screen',async()=>{
 for(const data of [undefined,{url:'https://evil.test'}]){
 const h=harness();let pending;
 h.handlers.notificationclick({notification:{data,close(){}},waitUntil:p=>pending=p});await pending;
 assert.equal(h.opened[0],'/?screen=guardian-view');
 }
});
