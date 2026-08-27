(() => {
  'use strict';

  const DB_NAME = 'finance-core-pwa';
  const STORE = 'pendingEntries';
  const SYNC_TAG = 'finance-entry-sync';
  const SNAPSHOT_KEY = 'finance-core-last-dashboard-v1';

  let swRegistration = null;
  let refreshing = false;

  function openDB() {
    return new Promise((resolve, reject) => {
      const req = indexedDB.open(DB_NAME, 1);
      req.onupgradeneeded = () => {
        const db = req.result;
        if (!db.objectStoreNames.contains(STORE)) db.createObjectStore(STORE, {keyPath: 'id'});
      };
      req.onsuccess = () => resolve(req.result);
      req.onerror = () => reject(req.error);
    });
  }

  async function putQueue(item) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readwrite');
      tx.objectStore(STORE).put(item);
      tx.oncomplete = () => { db.close(); resolve(); };
      tx.onerror = () => { db.close(); reject(tx.error); };
    });
  }

  async function getQueue() {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readonly');
      const req = tx.objectStore(STORE).getAll();
      req.onsuccess = () => resolve(req.result || []);
      req.onerror = () => reject(req.error);
      tx.oncomplete = () => db.close();
    });
  }

  async function deleteQueue(id) {
    const db = await openDB();
    return new Promise((resolve, reject) => {
      const tx = db.transaction(STORE, 'readwrite');
      tx.objectStore(STORE).delete(id);
      tx.oncomplete = () => { db.close(); resolve(); };
      tx.onerror = () => { db.close(); reject(tx.error); };
    });
  }

  function makeId() {
    if (crypto && typeof crypto.randomUUID === 'function') return `pwa:${crypto.randomUUID()}`;
    const rnd = Math.random().toString(36).slice(2);
    return `pwa:${Date.now()}:${rnd}`;
  }

  async function setBadge(count) {
    try {
      if ('setAppBadge' in navigator) {
        if (count > 0) await navigator.setAppBadge(count);
        else if ('clearAppBadge' in navigator) await navigator.clearAppBadge();
      }
    } catch (_) {}
  }

  function statusEl() { return document.getElementById('offline-queue-status'); }
  function networkEl() { return document.getElementById('pwa-network-status'); }

  async function refreshQueueUI(forcedCount) {
    let count = forcedCount;
    if (typeof count !== 'number') count = (await getQueue().catch(() => [])).length;
    const el = statusEl();
    if (el) {
      el.textContent = count
        ? `มี ${count} รายการรอซิงก์ • จะส่งอัตโนมัติเมื่อออนไลน์`
        : 'ไม่มีรายการค้างซิงก์';
      el.className = `status-msg${count ? ' warning' : ''}`;
    }
    await setBadge(count);
    return count;
  }

  function ensureNetworkToast() {
    let el = document.getElementById('finance-network-toast');
    if (el) return el;
    el = document.createElement('div');
    el.id = 'finance-network-toast';
    Object.assign(el.style, {
      position: 'fixed',
      zIndex: '1000',
      left: '50%',
      top: '12px',
      transform: 'translateX(-50%) translateY(-80px)',
      padding: '10px 14px',
      borderRadius: '999px',
      border: '1px solid #263241',
      background: 'rgba(12,17,23,.94)',
      color: '#f5f7fa',
      fontSize: '12px',
      boxShadow: '0 16px 45px rgba(0,0,0,.35)',
      backdropFilter: 'blur(18px)',
      transition: 'transform .24s ease, opacity .24s ease',
      opacity: '0',
      pointerEvents: 'none'
    });
    document.body.appendChild(el);
    return el;
  }

  function showToast(text, timeout = 2600) {
    const el = ensureNetworkToast();
    el.textContent = text;
    el.style.opacity = '1';
    el.style.transform = 'translateX(-50%) translateY(0)';
    clearTimeout(showToast.timer);
    if (timeout > 0) {
      showToast.timer = setTimeout(() => {
        el.style.opacity = '0';
        el.style.transform = 'translateX(-50%) translateY(-80px)';
      }, timeout);
    }
  }

  function updateNetworkState(showMessage = false) {
    const online = navigator.onLine;
    const el = networkEl();
    if (el) {
      el.textContent = online ? 'Online • พร้อมซิงก์' : 'Offline • App shell ยังใช้งานได้';
      el.className = online ? 'online' : 'warning';
    }
    if (showMessage) showToast(online ? 'กลับมาออนไลน์แล้ว • กำลังซิงก์' : 'Offline mode • รายการใหม่จะเข้าคิว', online ? 2200 : 0);
    return online;
  }

  async function registerSync() {
    if (!('serviceWorker' in navigator)) return;
    const reg = swRegistration || await navigator.serviceWorker.ready.catch(() => null);
    if (!reg) return;
    try {
      if ('sync' in reg) await reg.sync.register(SYNC_TAG);
      else if (navigator.serviceWorker.controller) navigator.serviceWorker.controller.postMessage({type: 'FLUSH_FINANCE_QUEUE'});
    } catch (_) {}
  }

  async function queueEntry(text) {
    const value = String(text || '').trim();
    if (!value) return false;
    const item = {id: makeId(), text: value, createdAt: Date.now()};
    await putQueue(item);
    await refreshQueueUI();
    await registerSync();
    showToast('บันทึกไว้ในคิวออฟไลน์แล้ว');
    return true;
  }

  async function flushQueueFromPage() {
    if (!navigator.onLine) return;
    const items = (await getQueue().catch(() => [])).sort((a, b) => (a.createdAt || 0) - (b.createdAt || 0));
    if (!items.length) return refreshQueueUI(0);

    let synced = 0;
    for (const item of items) {
      let res;
      try {
        res = await fetch('/api/entry', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          credentials: 'same-origin',
          body: JSON.stringify({text: item.text, idempotency_key: item.id})
        });
      } catch (_) {
        break;
      }
      if (res.ok) {
        await deleteQueue(item.id);
        synced += 1;
      } else if (res.status >= 400 && res.status < 500) {
        await deleteQueue(item.id);
      } else {
        break;
      }
    }

    const remaining = await refreshQueueUI();
    if (synced) {
      showToast(`ซิงก์แล้ว ${synced} รายการ${remaining ? ` • เหลือ ${remaining}` : ''}`);
      if (typeof window.load === 'function') window.load();
    }
  }

  function saveSnapshot(data) {
    try {
      localStorage.setItem(SNAPSHOT_KEY, JSON.stringify({savedAt: Date.now(), data}));
    } catch (_) {}
  }

  function loadSnapshot() {
    try {
      const raw = localStorage.getItem(SNAPSHOT_KEY);
      if (!raw) return null;
      const parsed = JSON.parse(raw);
      if (!parsed || !parsed.data) return null;
      return parsed;
    } catch (_) {
      return null;
    }
  }

  function showUpdate(reg) {
    if (!reg || !reg.waiting) return;
    const el = document.getElementById('app-update-status');
    const btn = document.getElementById('apply-app-update');
    if (el) {
      el.textContent = 'มีเวอร์ชันใหม่พร้อมใช้งาน';
      el.className = 'status-msg online';
    }
    if (btn) btn.style.display = 'inline-block';
    showToast('Finance Core มีอัปเดตใหม่', 4500);
  }

  async function setupServiceWorker() {
    if (!('serviceWorker' in navigator) || !window.isSecureContext) return;
    try {
      swRegistration = await navigator.serviceWorker.register('/sw.js', {updateViaCache: 'none'});
      if (swRegistration.waiting) showUpdate(swRegistration);

      swRegistration.addEventListener('updatefound', () => {
        const worker = swRegistration.installing;
        if (!worker) return;
        worker.addEventListener('statechange', () => {
          if (worker.state === 'installed' && navigator.serviceWorker.controller) showUpdate(swRegistration);
        });
      });

      navigator.serviceWorker.addEventListener('controllerchange', () => {
        if (refreshing) return;
        refreshing = true;
        location.reload();
      });

      navigator.serviceWorker.addEventListener('message', event => {
        const data = event.data || {};
        if (data.type === 'QUEUE_COUNT') refreshQueueUI(Number(data.count || 0));
        if (data.type === 'QUEUE_SYNCED') {
          refreshQueueUI(Number(data.remaining || 0));
          if (data.synced) {
            showToast(`ซิงก์ออฟไลน์แล้ว ${data.synced} รายการ`);
            if (typeof window.load === 'function') window.load();
          }
        }
        if (data.type === 'OPEN_VIEW' && typeof window.go === 'function') window.go(data.view || 'overview');
      });

      const updateEl = document.getElementById('app-update-status');
      if (updateEl && !swRegistration.waiting) updateEl.textContent = 'Service Worker พร้อมใช้งาน';
    } catch (err) {
      const updateEl = document.getElementById('app-update-status');
      if (updateEl) {
        updateEl.textContent = `Service Worker error: ${err.message}`;
        updateEl.className = 'status-msg danger';
      }
    }
  }

  function urlBase64ToUint8Array(value) {
    const padding = '='.repeat((4 - value.length % 4) % 4);
    const base64 = (value + padding).replace(/-/g, '+').replace(/_/g, '/');
    const raw = atob(base64);
    return Uint8Array.from([...raw].map(ch => ch.charCodeAt(0)));
  }

  async function ensurePushSubscription() {
    const reg = swRegistration || await navigator.serviceWorker.ready;
    if (!reg.pushManager) throw new Error('เบราว์เซอร์นี้ไม่รองรับ Web Push');

    let subscription = await reg.pushManager.getSubscription();
    if (!subscription) {
      const cfgRes = await fetch('/api/push/config', {cache: 'no-store'});
      const cfg = await cfgRes.json();
      if (!cfgRes.ok || !cfg.public_key) throw new Error('โหลด Push config ไม่สำเร็จ');
      subscription = await reg.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(cfg.public_key)
      });
    }

    const saveRes = await fetch('/api/push/subscribe', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({subscription: subscription.toJSON()})
    });
    const saved = await saveRes.json();
    if (!saveRes.ok) throw new Error(saved.error || 'บันทึก Push subscription ไม่สำเร็จ');
    return subscription;
  }

  async function enableNotifications() {
    const status = document.getElementById('notification-status');
    if (!('Notification' in window)) {
      if (status) status.textContent = 'เบราว์เซอร์นี้ไม่รองรับ Notification API';
      return;
    }
    const permission = await Notification.requestPermission();
    if (permission !== 'granted') {
      if (status) {
        status.textContent = `Notification: ${permission}`;
        status.className = 'status-msg warning';
      }
      return;
    }

    try {
      await ensurePushSubscription();
      if (status) {
        status.textContent = 'เปิดการแจ้งเตือนแล้ว • Web Push เชื่อมกับ Home Server แล้ว';
        status.className = 'status-msg online';
      }
      const reg = swRegistration || await navigator.serviceWorker.ready.catch(() => null);
      if (reg) await reg.showNotification('Finance Core', {
        body: 'เปิดการแจ้งเตือนแล้ว',
        icon: '/app-icon-192.png?v=7',
        badge: '/app-icon-192.png?v=7',
        tag: 'finance-ready',
        data: {url: '/#overview'}
      }).catch(() => {});
    } catch (err) {
      if (status) {
        status.textContent = `เปิด Notification ได้ แต่ Push setup ไม่สำเร็จ: ${err.message}`;
        status.className = 'status-msg warning';
      }
    }
  }

  async function testRemoteNotification() {
    const status = document.getElementById('notification-status');
    try {
      if (!('Notification' in window)) throw new Error('เบราว์เซอร์นี้ไม่รองรับ Notification API');
      let permission = Notification.permission;
      if (permission !== 'granted') permission = await Notification.requestPermission();
      if (permission !== 'granted') throw new Error(`Notification permission: ${permission}`);

      if (status) {
        status.textContent = 'กำลังยิง Web Push จาก Home Server…';
        status.className = 'status-msg';
      }
      const subscription = await ensurePushSubscription();
      const r = await fetch('/api/push/test', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({endpoint: subscription.endpoint})
      });
      const out = await r.json();
      if (!r.ok || !out.sent) throw new Error(out.errors?.[0] || 'ส่ง Push ไม่สำเร็จ');
      if (status) {
        status.textContent = `ยิงจาก Home Server แล้ว • sent ${out.sent}/${out.targets}`;
        status.className = 'status-msg online';
      }
    } catch (err) {
      if (status) {
        status.textContent = `Push test failed: ${err.message}`;
        status.className = 'status-msg danger';
      }
    }
  }

  async function checkForUpdates() {
    const el = document.getElementById('app-update-status');
    if (el) el.textContent = 'กำลังตรวจสอบอัปเดต…';
    const reg = swRegistration || await navigator.serviceWorker.ready.catch(() => null);
    if (!reg) {
      if (el) el.textContent = 'ยังไม่มี Service Worker';
      return;
    }
    await reg.update().catch(() => {});
    if (reg.waiting) showUpdate(reg);
    else if (el) el.textContent = 'เป็นเวอร์ชันล่าสุดแล้ว';
  }

  function applyWaitingUpdate() {
    const reg = swRegistration;
    if (reg && reg.waiting) reg.waiting.postMessage({type: 'SKIP_WAITING'});
  }

  function handleLaunchParams() {
    const params = new URLSearchParams(location.search);
    const quick = params.get('quick') === '1';
    const shareTarget = params.get('share_target') === '1';

    if (shareTarget) {
      const parts = [params.get('title'), params.get('text'), params.get('url')].filter(Boolean);
      const shared = parts.join(' ').trim();
      if (shared) {
        const input = document.getElementById('entry');
        if (input) input.value = shared;
        if (typeof window.go === 'function') window.go('overview');
        setTimeout(() => input && input.focus(), 80);
      }
    } else if (quick) {
      if (typeof window.go === 'function') window.go('overview');
      setTimeout(() => document.getElementById('entry')?.focus(), 80);
    }

    if (quick || shareTarget) {
      const clean = `${location.pathname}${location.hash || '#overview'}`;
      history.replaceState(null, '', clean);
    }
  }

  function setupButtons() {
    document.getElementById('enable-notifications')?.addEventListener('click', enableNotifications);
    document.getElementById('test-notification')?.addEventListener('click', testRemoteNotification);
    document.getElementById('check-app-update')?.addEventListener('click', checkForUpdates);
    document.getElementById('apply-app-update')?.addEventListener('click', applyWaitingUpdate);

    const status = document.getElementById('notification-status');
    if (status && 'Notification' in window) {
      status.textContent = Notification.permission === 'granted'
        ? 'เปิดการแจ้งเตือนแล้ว'
        : 'ยังไม่ได้เปิดการแจ้งเตือน';
    }
  }

  window.financePWA = {
    queueEntry,
    flushQueue: flushQueueFromPage,
    refreshQueueUI,
    saveSnapshot,
    loadSnapshot,
    showToast,
    ensurePushSubscription,
    testRemoteNotification
  };

  window.addEventListener('online', () => {
    updateNetworkState(true);
    flushQueueFromPage();
    registerSync();
  });
  window.addEventListener('offline', () => updateNetworkState(true));

  document.addEventListener('visibilitychange', () => {
    if (!document.hidden && navigator.onLine) {
      flushQueueFromPage();
      if (swRegistration) swRegistration.update().catch(() => {});
    }
  });

  window.addEventListener('DOMContentLoaded', async () => {
    updateNetworkState(false);
    setupButtons();
    handleLaunchParams();
    await refreshQueueUI();
    await setupServiceWorker();

    // Permission and PushSubscription are separate states. If the user already
    // allowed notifications, automatically repair/register the server-side
    // subscription after updates, browser restarts, or a cleared subscription.
    if ('Notification' in window && Notification.permission === 'granted') {
      try {
        await ensurePushSubscription();
        const status = document.getElementById('notification-status');
        if (status) {
          status.textContent = 'เปิดการแจ้งเตือนแล้ว • Web Push เชื่อมกับ Home Server แล้ว';
          status.className = 'status-msg online';
        }
      } catch (err) {
        const status = document.getElementById('notification-status');
        if (status) {
          status.textContent = `Notification เปิดอยู่ แต่ Push ยังไม่เชื่อม: ${err.message}`;
          status.className = 'status-msg warning';
        }
      }
    }

    if (navigator.onLine) flushQueueFromPage();
  });
})();
