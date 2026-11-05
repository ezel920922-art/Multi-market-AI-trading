const CACHE='multi-market-trading-shell-v1';
const SHELL=['/desk','/manifest.webmanifest','/static/icons/icon.svg','/static/icons/icon-192.svg','/static/icons/icon-512.svg'];
self.addEventListener('install',e=>{e.waitUntil(caches.open(CACHE).then(c=>c.addAll(SHELL)));self.skipWaiting();});
self.addEventListener('activate',e=>{e.waitUntil(caches.keys().then(ks=>Promise.all(ks.filter(k=>k!==CACHE).map(k=>caches.delete(k)))));self.clients.claim();});
self.addEventListener('fetch',e=>{const u=new URL(e.request.url);if(u.origin!==self.location.origin||u.pathname.startsWith('/api/')||u.pathname.startsWith('/paper/')||u.pathname.startsWith('/risk/')||e.request.method!=='GET')return;if(e.request.mode==='navigate'){e.respondWith(fetch(e.request).then(r=>{if(r.ok){const c=r.clone();caches.open(CACHE).then(x=>x.put('/desk',c));}return r;}).catch(()=>caches.match('/desk')));return;}if(SHELL.includes(u.pathname))e.respondWith(caches.match(e.request).then(r=>r||fetch(e.request)));});
