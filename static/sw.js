"use strict";
// Service worker —— 讓整個 app 離線可用。
//
// 為什麼值得做：刷題的真實情境是零碎時間（捷運上、排隊時），而這是內網服務，
// 手機一離開 tailnet 或進地下街就開不了。整包離線負載小得可笑：
//   兩份題庫 JSON 1.5 MB（gzip 179 KB）+ 354 張題目圖 1.8 MB。
//
// 分工：
//   Cache Storage（這裡）→ 靜態的東西：app shell、題庫 JSON、題目圖
//   IndexedDB（store.js）→ 使用者的東西：作答狀態、待補送佇列、筆記
//   /api/*               → 一律不快取。過期的 API 回應比一個誠實的錯誤更糟。
//
// 改了 static/ 底下任何檔案就把 VERSION 加一，否則舊 SW 會繼續餵舊的 shell。

const VERSION = "v4";
const SHELL_CACHE = `shell-${VERSION}`;
const ASSET_CACHE = `assets-${VERSION}`;   // 題庫與圖片；換版本會整批重抓，
                                           // 這是題庫改版時唯一能讓舊圖失效的手段

const SHELL = [
  "/",
  "/static/app.js",
  "/static/srs.js",
  "/static/store.js",
  "/static/style.css",
  // 字型自帶而不是連 Google Fonts：離線時外部 CDN 會整個掉回系統字型
  "/static/fonts/archivo-latin.woff2",
  "/static/fonts/archivo-latin-ext.woff2",
  "/manifest.webmanifest",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/static/apple-touch-icon.png",
];

const BANKS = ["/data/car.json", "/data/moto.json"];

// 安裝時一律繞過瀏覽器的 HTTP 快取。addAll() 預設會先問 HTTP 快取，
// 拿到啟發式新鮮度留下來的舊檔案，新版 shell 就會裝進舊的 CSS／JS。
// 伺服器那邊也送了 no-cache（app/main.py 的 _Static），兩層都補上。
const noHttpCache = (url) => new Request(url, { cache: "reload" });

self.addEventListener("install", (e) => {
  e.waitUntil(
    (async () => {
      const shell = await caches.open(SHELL_CACHE);
      await shell.addAll(SHELL.map(noHttpCache));
      // 題庫跟 shell 一起裝好：沒有題目的離線 app 是沒有意義的
      const assets = await caches.open(ASSET_CACHE);
      await assets.addAll(BANKS.map(noHttpCache));
      await self.skipWaiting();
    })()
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    (async () => {
      const keep = new Set([SHELL_CACHE, ASSET_CACHE]);
      for (const k of await caches.keys()) if (!keep.has(k)) await caches.delete(k);
      await self.clients.claim();
      precacheImages();   // 不 await：354 張圖不該擋住啟用
    })()
  );
});

// 題目圖分批灌進快取，順便回報進度給頁面顯示。
// 已經在快取裡的就跳過，所以重跑很便宜（換版本、使用者手動觸發都可以）。
let imagesRunning = false;
async function precacheImages() {
  if (imagesRunning) return;
  imagesRunning = true;
  try {
    const cache = await caches.open(ASSET_CACHE);
    const res = await fetch("/api/image-manifest");
    if (!res.ok) return;
    const { files } = await res.json();

    const missing = [];
    for (const f of files) if (!(await cache.match(f))) missing.push(f);
    if (!missing.length) return post({ type: "images", done: files.length, total: files.length });

    let done = files.length - missing.length;
    const BATCH = 12;   // 別一次開 354 條連線把手機的網路堆疊塞爆
    for (let i = 0; i < missing.length; i += BATCH) {
      await Promise.all(
        missing.slice(i, i + BATCH).map((f) => cache.add(f).catch(() => {}))
      );
      done += Math.min(BATCH, missing.length - i);
      post({ type: "images", done, total: files.length });
    }
  } catch {
    // 離線時抓不到 manifest 是正常的，下次上線再說
  } finally {
    imagesRunning = false;
  }
}

async function post(msg) {
  for (const c of await self.clients.matchAll()) c.postMessage(msg);
}

self.addEventListener("message", (e) => {
  if (e.data === "precache-images") precacheImages();
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // API 一律走網路、絕不快取。離線時讓它失敗，由 app.js 改用 IndexedDB 的本地狀態。
  if (url.pathname.startsWith("/api/")) return;

  // 導覽請求：網路優先（拿得到新版就用新版），失敗才回快取的 shell
  if (req.mode === "navigate") {
    e.respondWith(
      fetch(req)
        .then((r) => {
          caches.open(SHELL_CACHE).then((c) => c.put("/", r.clone()));
          return r;
        })
        .catch(async () => (await caches.match("/")) || Response.error())
    );
    return;
  }

  // 題庫與圖片：快取優先。內容是靜態的，沒有新鮮度問題。
  if (url.pathname.startsWith("/images/") || url.pathname.startsWith("/data/")) {
    e.respondWith(
      caches.open(ASSET_CACHE).then(async (cache) => {
        const hit = await cache.match(req);
        if (hit) return hit;
        const res = await fetch(req);
        if (res.ok) cache.put(req, res.clone());
        return res;
      })
    );
    return;
  }

  // app shell：stale-while-revalidate —— 先給快取（離線也秒開），背景更新，
  // 下一次載入就是新版。這樣改 app.js 不必每次都動 VERSION。
  if (url.pathname.startsWith("/static/") || url.pathname === "/manifest.webmanifest") {
    e.respondWith(
      caches.open(SHELL_CACHE).then(async (cache) => {
        const hit = await cache.match(req);
        const fresh = fetch(req)
          .then((r) => {
            if (r.ok) cache.put(req, r.clone());
            return r;
          })
          .catch(() => hit);
        return hit || fresh;
      })
    );
  }
});
