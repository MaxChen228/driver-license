"use strict";
// IndexedDB 存放層。只存「使用者的東西」：狀態快照、待補送的作答、未同步的筆記。
// 題目本身不進這裡 —— 那是 service worker 的 Cache Storage 的職責（1.5 MB 靜態 JSON）。
//
// 用 IndexedDB 而不是 localStorage：localStorage 是同步 API，會卡住主執行緒，
// 而且 iOS Safari 在儲存壓力下清得比 IndexedDB 積極。作答紀錄不能被清掉。

const DB_NAME = "driver-license";
const DB_VERSION = 2;

// 保留上限。一題約 120 bytes，兩萬筆約 2.4 MB —— 遠低於任何瀏覽器的配額，
// 但有個上限總比沒有好：這是唯一會單調成長的東西。
const EVENTS_MAX = 20000;

let dbp = null;

function open() {
  if (dbp) return dbp;
  dbp = new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, DB_VERSION);
    req.onupgradeneeded = (e) => {
      const db = req.result;
      if (!db.objectStoreNames.contains("kv")) db.createObjectStore("kv");
      if (!db.objectStoreNames.contains("outbox")) {
        db.createObjectStore("outbox", { keyPath: "client_event_id" });
      }
      // v2：events 是「這台裝置的完整作答歷史」，跟 outbox 是兩回事 ——
      // outbox 送出去就清掉，events 永遠留著。登入時要把歷史併進帳號、
      // 匿名時要算正確率與連續天數，靠的都是它。
      if (!db.objectStoreNames.contains("events")) {
        db.createObjectStore("events", { keyPath: "client_event_id" });
        // 升級前那些還沒送出去的，是這台裝置僅有的歷史，不能丟
        if (e.oldVersion >= 1) {
          const tx = req.transaction;
          const evs = tx.objectStore("events");
          tx.objectStore("outbox").openCursor().onsuccess = (ev) => {
            const cur = ev.target.result;
            if (!cur) return;
            evs.put(cur.value);
            cur.continue();
          };
        }
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
const kvDelete = (k) => tx("kv", "readwrite", (s) => s.delete(k));

const eventsAll = () => tx("events", "readonly", (s) => s.getAll());
const eventsGet = (id) => tx("events", "readonly", (s) => s.get(id));
const eventsCount = () => tx("events", "readonly", (s) => s.count());

function deleteCardFrom(name, cardId) {
  return open().then((db) => new Promise((resolve, reject) => {
    const t = db.transaction(name, "readwrite");
    t.objectStore(name).openCursor().onsuccess = (e) => {
      const cur = e.target.result;
      if (!cur) return;
      if (cur.value.card_id === cardId) cur.delete();
      cur.continue();
    };
    t.oncomplete = resolve;
    t.onerror = () => reject(t.error);
    t.onabort = () => reject(t.error);
  }));
}

const eventsDeleteCard = (cardId) => deleteCardFrom("events", cardId);

async function eventsPut(ev) {
  await tx("events", "readwrite", (s) => s.put(ev));
  // client_event_id 以時間戳當前綴，所以 key 順序就是時間順序 ——
  // 修剪時從最舊的那頭刪即可，不必先全部讀出來排序。
  const n = await eventsCount();
  if (n <= EVENTS_MAX) return;
  await tx("events", "readwrite", (s) => {
    let left = n - EVENTS_MAX;
    s.openCursor().onsuccess = (e) => {
      const cur = e.target.result;
      if (!cur || left-- <= 0) return;
      cur.delete();
      cur.continue();
    };
  });
}

async function clearAll() {
  const db = await open();
  return new Promise((resolve, reject) => {
    const t = db.transaction(["kv", "outbox", "events"], "readwrite");
    ["kv", "outbox", "events"].forEach((n) => t.objectStore(n).clear());
    t.oncomplete = resolve;
    t.onerror = () => reject(t.error);
    t.onabort = () => reject(t.error);  // 少了它，交易被中止時這個 promise 永遠不會落地
  });
}
const outboxPut = (ev) => tx("outbox", "readwrite", (s) => s.put(ev));
const outboxDelete = (id) => tx("outbox", "readwrite", (s) => s.delete(id));
const outboxCount = () => tx("outbox", "readonly", (s) => s.count());
const outboxGet = (id) => tx("outbox", "readonly", (s) => s.get(id));
const outboxDeleteCard = (cardId) => deleteCardFrom("outbox", cardId);

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
  kvGet, kvPut, kvDelete,
  outboxAll, outboxPut, outboxDelete, outboxCount, outboxGet, outboxDeleteCard, outboxRemoveMany,
  eventsAll, eventsGet, eventsPut, eventsCount, eventsDeleteCard,
  clearAll, requestPersistence,
};
