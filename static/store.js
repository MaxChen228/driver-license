"use strict";
// IndexedDB 存放層。只存「使用者的東西」：狀態快照、待補送的作答、未同步的筆記。
// 題目本身不進這裡 —— 那是 service worker 的 Cache Storage 的職責（1.5 MB 靜態 JSON）。
//
// 用 IndexedDB 而不是 localStorage：localStorage 是同步 API，會卡住主執行緒，
// 而且 iOS Safari 在儲存壓力下清得比 IndexedDB 積極。作答紀錄不能被清掉。

const DB_NAME = "driver-license";
const DB_VERSION = 1;

let dbp = null;

function open() {
  if (dbp) return dbp;
  dbp = new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains("kv")) db.createObjectStore("kv");
      if (!db.objectStoreNames.contains("outbox")) {
        db.createObjectStore("outbox", { keyPath: "client_event_id" });
      }
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
  return dbp;
}

async function tx(store, mode, fn) {
  const db = await open();
  return new Promise((resolve, reject) => {
    const t = db.transaction(store, mode);
    const req = fn(t.objectStore(store));
    t.oncomplete = () => resolve(req ? req.result : undefined);
    t.onerror = () => reject(t.error);
    t.onabort = () => reject(t.error);
  });
}

const kvGet = (k) => tx("kv", "readonly", (s) => s.get(k));
const kvPut = (k, v) => tx("kv", "readwrite", (s) => s.put(v, k));

const outboxAll = () => tx("outbox", "readonly", (s) => s.getAll());
const outboxPut = (ev) => tx("outbox", "readwrite", (s) => s.put(ev));
const outboxDelete = (id) => tx("outbox", "readwrite", (s) => s.delete(id));
const outboxCount = () => tx("outbox", "readonly", (s) => s.count());
const outboxGet = (id) => tx("outbox", "readonly", (s) => s.get(id));

async function outboxRemoveMany(ids) {
  const db = await open();
  return new Promise((resolve, reject) => {
    const t = db.transaction("outbox", "readwrite");
    const st = t.objectStore("outbox");
    ids.forEach((id) => id && st.delete(id));
    t.oncomplete = resolve;
    t.onerror = () => reject(t.error);
  });
}

// 要求瀏覽器把這個 origin 的資料標成「持久」，否則 iOS 在儲存吃緊時會整包丟掉。
// 已加到主畫面的 PWA 幾乎必然核准；純瀏覽器分頁可能被拒，被拒也不影響功能。
async function requestPersistence() {
  try {
    if (navigator.storage && navigator.storage.persist) return await navigator.storage.persist();
  } catch { /* 不支援就算了 */ }
  return false;
}

export {
  kvGet, kvPut,
  outboxAll, outboxPut, outboxDelete, outboxCount, outboxGet, outboxRemoveMany,
  requestPersistence,
};
