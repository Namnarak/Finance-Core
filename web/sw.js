const CACHE_NAME = 'finance-core-shell-v6';
const QUEUE_DB = 'finance-core-pwa';
const QUEUE_STORE = 'pendingEntries';
const SYNC_TAG = 'finance-entry-sync';

const SHELL = [
  '/',
  '/index.html',
  '/manifest.webmanifest?v=7',
  '/pwa.js?v=7',
  '/icon-192.png?v=3',
  '/app-icon-180.png?v=7',
  '/app-icon-192.png?v=7',
  '/app-icon-512.png?v=7',
  '/app-icon-maskable-512.png?v=7'
];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE_NAME).then(cache => cache.addAll(SHELL)));
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', event => {
  const request = event.request;
  const url = new URL(request.url);

  if (url.origin !== self.location.origin) return;

  // Sensitive financial API responses are intentionally never cached.
  if (url.pathname.startsWith('/api/')) return;

  if (request.mode === 'navigate') {
    event.respondWith((async () => {
      try {
        return await fetch(request);
      } catch (_) {
        return (await caches.match('/index.html')) || (await caches.match('/'));
      }
    })());
    return;
  }

  if (request.method !== 'GET') return;

  event.respondWith((async () => {
    const cached = await caches.match(request, {ignoreSearch: false});
    if (cached) return cached;
    try {
      const response = await fetch(request);
      if (response && response.ok) {
        const cache = await caches.open(CACHE_NAME);
        cache.put(request, response.clone()).catch(() => {});
      }
      return response;
    } catch (_) {
      return Response.error();
    }
  })());
});

self.addEventListener('message', event => {
  const data = event.data || {};
  if (data.type === 'SKIP_WAITING') {
    self.skipWaiting();
    return;
  }
  if (data.type === 'FLUSH_FINANCE_QUEUE') {
    event.waitUntil(flushQueue());
  }
});

self.addEventListener('sync', event => {
  if (event.tag === SYNC_TAG) event.waitUntil(flushQueue());
});

self.addEventListener('push', event => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (_) {
    payload = {body: event.data ? event.data.text() : ''};
  }
  const title = payload.title || 'Finance Core';
  event.waitUntil(self.registration.showNotification(title, {
    body: payload.body || 'มีการแจ้งเตือนใหม่จาก Finance Core',
    icon: payload.icon || '/app-icon-192.png?v=7',
    badge: payload.badge || '/app-icon-192.png?v=7',
    tag: payload.tag || 'finance-push',
    timestamp: payload.timestamp ? Date.parse(payload.timestamp) : Date.now(),
    vibrate: [120, 60, 120],
    data: {url: payload.url || '/#overview'}
  }));
});

self.addEventListener('notificationclick', event => {
  const url = event.notification?.data?.url || '/#overview';
  event.notification.close();
  event.waitUntil((async () => {
    const all = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
    const target = all.find(c => new URL(c.url).origin === self.location.origin);
    if (target) {
      if ('navigate' in target) await target.navigate(url).catch(() => {});
      await target.focus();
      target.postMessage({type: 'OPEN_VIEW', view: url.includes('transactions') ? 'transactions' : 'overview'});
      return;
    }
    await self.clients.openWindow(url);
  })());
});

function openQueueDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(QUEUE_DB, 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(QUEUE_STORE)) {
        db.createObjectStore(QUEUE_STORE, {keyPath: 'id'});
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function listQueue() {
  const db = await openQueueDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(QUEUE_STORE, 'readonly');
    const req = tx.objectStore(QUEUE_STORE).getAll();
    req.onsuccess = () => resolve(req.result || []);
    req.onerror = () => reject(req.error);
    tx.oncomplete = () => db.close();
  });
}

async function deleteQueued(id) {
  const db = await openQueueDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(QUEUE_STORE, 'readwrite');
    tx.objectStore(QUEUE_STORE).delete(id);
    tx.oncomplete = () => { db.close(); resolve(); };
    tx.onerror = () => { db.close(); reject(tx.error); };
  });
}

async function queueCount() {
  const db = await openQueueDB();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(QUEUE_STORE, 'readonly');
    const req = tx.objectStore(QUEUE_STORE).count();
    req.onsuccess = () => resolve(req.result || 0);
    req.onerror = () => reject(req.error);
    tx.oncomplete = () => db.close();
  });
}

async function updateBadge() {
  const count = await queueCount().catch(() => 0);
  if (typeof self.registration.setAppBadge === 'function') {
    try {
      if (count) await self.registration.setAppBadge(count);
      else if (typeof self.registration.clearAppBadge === 'function') await self.registration.clearAppBadge();
    } catch (_) {}
  }
  const clients = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
  clients.forEach(c => c.postMessage({type: 'QUEUE_COUNT', count}));
  return count;
}

async function flushQueue() {
  const items = (await listQueue()).sort((a, b) => (a.createdAt || 0) - (b.createdAt || 0));
  let synced = 0;
  let failed = 0;

  for (const item of items) {
    let response;
    try {
      response = await fetch('/api/entry', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        credentials: 'same-origin',
        body: JSON.stringify({text: item.text, idempotency_key: item.id})
      });
    } catch (_) {
      throw new Error('network unavailable');
    }

    if (response.ok) {
      await deleteQueued(item.id);
      synced += 1;
      continue;
    }

    // 4xx is a permanent validation error. Remove it so Background Sync does not loop forever.
    if (response.status >= 400 && response.status < 500) {
      await deleteQueued(item.id);
      failed += 1;
      continue;
    }

    throw new Error(`server unavailable: ${response.status}`);
  }

  const remaining = await updateBadge();
  const clients = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
  clients.forEach(c => c.postMessage({type: 'QUEUE_SYNCED', synced, failed, remaining}));

  if ((synced || failed) && typeof Notification !== 'undefined' && Notification.permission === 'granted') {
    const body = failed
      ? `ซิงก์ ${synced} รายการ • ล้มเหลว ${failed} รายการ`
      : `ซิงก์รายการออฟไลน์แล้ว ${synced} รายการ`;
    await self.registration.showNotification('Finance Core', {
      body,
      icon: '/app-icon-192.png?v=6',
      badge: '/app-icon-192.png?v=6',
      tag: 'finance-sync',
      renotify: false,
      data: {url: '/#overview'}
    }).catch(() => {});
  }
}
