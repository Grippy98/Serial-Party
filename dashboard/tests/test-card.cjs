const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const source = fs.readFileSync(path.join(__dirname, '..', 'serial-party-card.js'), 'utf8');
class E extends EventTarget {
 constructor(tag='div') { super(); this.tagName=tag; this.children=[]; this.nodes={}; this.style={}; }
 attachShadow() { return this.shadowRoot=new E('shadow'); }
 set innerHTML(value) { this.html=value; for (const m of value.matchAll(/id="([^"]+)"/g)) this.nodes['#'+m[1]]=new E(); }
 querySelector(selector) { return this.nodes[selector] || null; }
 appendChild(child) { child.parent=this;this.children.push(child); }
 remove() { if(this.parent)this.parent.children=this.parent.children.filter(c=>c!==this); }
 setAttribute(k,v) { this[k]=v; }
}
function setup(handler) {
 const elements=new Map(), timers=new Map(), calls=[], cookies=[];
 const document=Object.assign(new EventTarget(),{hidden:false,createElement:tag=>new E(tag)});
 Object.defineProperty(document,'cookie',{set:value=>cookies.push(value),get:()=>cookies.at(-1)||''});
 let timer=0;
 const window=Object.assign(new EventTarget(),{setInterval:f=>{timers.set(++timer,f);return timer;},clearInterval:id=>timers.delete(id)});
 const context={HTMLElement:E,document,window,location:{origin:'https://ha.test',protocol:'https:'},URL,URLSearchParams,customElements:{get:k=>elements.get(k),define:(k,v)=>elements.set(k,v)}};
 vm.runInNewContext(source,context);
 const C=elements.get('serial-party-card'),card=new C();
 const hass={connected:true,user:{id:'admin',is_admin:true},callWS:async message=>{calls.push(message); if(handler)return handler(message); if(message.type.endsWith('/info'))return{state:'started',ingress_url:'/api/hassio_ingress/test-path/'};if(message.type.endsWith('/session'))return{session:'test_session'};return null;}};
 return{card,hass,document,window,timers,calls,cookies};
}
const flush=async()=>{for(let i=0;i<15;i++)await Promise.resolve();};
const mount=s=>{s.card.setConfig({type:'custom:serial-party-card',app_slug:'local_serial_party',board:'board-a'});s.card.hass=s.hass;s.card.connectedCallback();};
(async()=>{
 const s=setup();mount(s);await flush();
 assert.equal(s.calls.length,2);assert.equal(s.timers.size,1);assert.ok(s.card._frame.src.includes('/test-path/boards/board-a/monitor/?rendererType=dom'));assert.ok(!s.card._frame.src.includes('/control/'));assert.ok(s.cookies[0].endsWith(';Secure'));const frame=s.card._frame;
 for(let i=0;i<100;i++)s.card.hass={...s.hass};await flush();assert.equal(s.calls.length,2);assert.equal(s.card._frame,frame);
 await [...s.timers.values()][0]();assert.equal(s.calls.length,3);assert.equal(s.card._frame,frame);
 s.document.hidden=true;s.document.dispatchEvent(new Event('visibilitychange'));assert.equal(s.card._frame,null);assert.equal(s.timers.size,0);assert.equal(frame.src,'about:blank');
 s.document.hidden=false;s.document.dispatchEvent(new Event('visibilitychange'));await flush();assert.ok(s.card._frame);assert.equal(s.calls.filter(c=>c.type==='supervisor/ingress/session').length,1);
 s.card.disconnectedCallback();assert.equal(s.timers.size,0);assert.equal(s.card._frame,null);assert.equal(s.card._session,null);
 const denied=setup();denied.hass.user.is_admin=false;mount(denied);await flush();assert.equal(denied.calls.length,0);assert.match(denied.card._status.textContent,/administrator/);
 let resolveSession;
 const late=setup(m=>m.type.endsWith('/info')?{state:'started',ingress_url:'/api/hassio_ingress/test-path/'}:new Promise(r=>resolveSession=r));mount(late);await flush();late.card.disconnectedCallback();resolveSession({session:'late_session'});await flush();assert.equal(late.card._frame,null);assert.equal(late.cookies.length,0);assert.equal(late.timers.size,0);
 const offline=setup();offline.hass.connected=false;mount(offline);for(let i=0;i<30;i++)offline.card.hass={...offline.hass};await flush();assert.equal(offline.calls.length,0);offline.hass.connected=true;offline.card.hass=offline.hass;await flush();assert.ok(offline.card._frame);offline.card.disconnectedCallback();
 let expired=false,count=0;
 const renew=setup(m=>{if(m.type.endsWith('/info'))return{state:'started',ingress_url:'/api/hassio_ingress/test-path/'};if(m.type.endsWith('/validate_session')){if(expired)throw Error('expired');return null;}return{session:'session_'+(++count)};});mount(renew);await flush();expired=true;await [...renew.timers.values()][0]();assert.equal(count,2);assert.ok(renew.cookies.at(-1).includes('session_2'));assert.equal(renew.timers.size,1);renew.card.disconnectedCallback();
 const configurable=setup();mount(configurable);await flush();const first=configurable.card._frame;
 const cfg={type:'custom:serial-party-card',app_slug:'local_serial_party',board:'board-a'};
 configurable.card.setConfig({...cfg});await flush();assert.equal(configurable.card._frame,first);
 configurable.card.setConfig({...cfg,board:'sensor-node',title:'<img src=x onerror=alert(1)>',height:420,font_size:14});await flush();
 assert.equal(first.src,'about:blank');assert.ok(configurable.card._frame.src.includes('/boards/sensor-node/monitor/'));assert.ok(configurable.card._frame.src.includes('fontSize=14'));
 assert.equal(configurable.card.shadowRoot.querySelector('#title').textContent,'<img src=x onerror=alert(1)>');assert.equal(configurable.card.shadowRoot.querySelector('#card').style.height,'420px');
 configurable.card.setConfig({...cfg,app_slug:'repo_serial_party'});await flush();assert.equal(configurable.calls.filter(c=>c.type==='supervisor/ingress/session').length,2);assert.equal(configurable.calls.at(-1).slug,'repo_serial_party');
 for(const invalid of [{}, {...cfg,app_slug:'../other'}, {...cfg,board:'../../control'}, {...cfg,board:'Board A'}, {...cfg,board:'1board'}, {...cfg,board:'b'.repeat(33)}, {...cfg,height:'360'}, {...cfg,font_size:100}, {...cfg,title:{html:'unsafe'}}]) assert.throws(()=>configurable.card.setConfig(invalid));
 configurable.card.disconnectedCallback();
 console.log('PASS: auth and readonly URL; 100 hass updates; keepalive; hide/resume/disconnect cleanup; admin denial; stale async guard; offline recovery; expired session replacement; board/app reconfiguration; text-only titles; config validation.');
})().catch(error=>{console.error(error);process.exitCode=1;});
