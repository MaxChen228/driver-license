"use strict";
// local-first：題目、狀態、排程全部在本地，伺服器是同步的目的地而不是每翻一張卡的必要條件。
//
//   題目      /data/*.json（service worker 預先快取）→ 記憶體
//   使用者狀態 IndexedDB 快照（store.js）
//   作答      先在本地算完並立刻顯示，同時排進 outbox，有網路才補送
//
// 這個順序是重點：作答流程完全不等網路。連線只影響「什麼時候同步」，不影響「能不能刷」。

import * as srs from "./srs.js";
import * as store from "./store.js";

const $ = (s) => document.querySelector(s);

const api = async (path, opts) => {
  const r = await fetch(path, { headers: { "content-type": "application/json" }, ...opts });
  if (!r.ok) throw new Error(`${r.status} ${path}`);
  return r.json();
};

const VIEWS = ["home", "session", "done", "browse", "stats", "settings"];
const TITLES = {
  home: "駕照筆試", session: "作答", done: "完成",
  browse: "題庫", stats: "統計", settings: "設定",
};

let SNAP = null;    // 伺服器快照：params / paused / decks / states(Map) / notes(Map)
let BANK = {};      // deck → { questions, byId, categories, label, title, version }
let S = null;       // 目前 session
let syncing = false;

// ── 開機 ──────────────────────────────────────────────
async function boot() {
  registerSW();
  store.requestPersistence();

  const cached = await store.kvGet("snapshot");
  try {
    // 有網路就拿權威快照（順便把 outbox 補送掉）；沒網路就用上次存的
    SNAP = adopt(await sync({ silent: true }));
  } catch {
    if (!cached) return bootFail();
    SNAP = adopt(cached);
  }

  try {
    BANK = await loadBanks(SNAP.decks);
  } catch {
    return bootFail();
  }

  $("#boot").hidden = true;
  await replayPending();       // outbox 裡還沒送出去的，疊回權威狀態上
  paintNet();
  renderHome();
}

function bootFail() {
  $("#boot").innerHTML =
    `<p class="kicker kicker-accent">無法啟動</p>
     <h2>離線，而且這台裝置<br>還沒有下載過題庫</h2>
     <p class="muted">請先在有連線時開啟一次，之後就能完全離線使用。</p>`;
}

// 圖示：全部走 index.html 裡的 sprite，不重複內嵌 path
function ico(name, cls = "ico") {
  return `<svg class="${cls}"><use href="#i-${name}"></use></svg>`;
}

async function loadBanks(decks) {
  const out = {};
  await Promise.all(
    decks.map(async (d) => {
      const raw = await (await fetch(d.url)).json();
      const questions = raw.questions;
      questions.forEach((q) => (q.deck = d.deck));
      out[d.deck] = {
        ...d,
        questions,
        byId: Object.fromEntries(questions.map((q) => [q.id, q])),
      };
    })
  );
  return out;
}

function adopt(payload) {
  return {
    ...payload,
    states: new Map(payload.states.map((s) => [s.id, s])),
    notes: new Map(Object.entries(payload.notes || {})),
  };
}

// 快照存進 IndexedDB 前要轉回可結構化複製的形狀（Map 可以，但存純物件比較好對帳）
async function persist() {
  await store.kvPut("snapshot", {
    ...SNAP,
    states: [...SNAP.states.values()],
    notes: Object.fromEntries(SNAP.notes),
  });
}

// 凍結時鐘：暫停期間所有到期計算都用 paused_at，本地也要照做，否則
// 離線算出來的下次複習時間跟伺服器補送後的結果會對不起來。
function refNow() {
  return SNAP.paused && SNAP.paused_at ? new Date(SNAP.paused_at) : new Date();
}

// ── 同步 ──────────────────────────────────────────────
// 時間序的事件 id：時間戳當前綴，IndexedDB 依 key 取出來就是作答順序。
// 順序很重要 —— 同一張卡答兩次，先後不同結果就不同。
let evSeq = 0;
function eventId() {
  const t = Date.now().toString(36).padStart(9, "0");
  const s = (evSeq++ % 46656).toString(36).padStart(3, "0");
  return `${t}-${s}-${Math.random().toString(36).slice(2, 8)}`;
}

async function sync({ silent = false } = {}) {
  const answers = await store.outboxAll();
  const notes = (await store.kvGet("notes_dirty")) || {};
  const res = await api("/api/sync", {
    method: "POST",
    body: JSON.stringify({ answers, notes }),
  });

  // applied 與 skipped 都代表「伺服器已經有了」；failed 是永久性失敗（例如題號不存在），
  // 留著只會每次重試都失敗，一併清掉並留下 console 紀錄。
  const { applied, skipped, failed } = res.sync;
  if (failed.length) console.warn("同步失敗的事件已丟棄", failed);
  await store.outboxRemoveMany([
    ...applied, ...skipped, ...failed.map((f) => f.client_event_id),
  ]);
  await store.kvPut("notes_dirty", {});
  if (!silent) paintNet();
  return res;
}

async function syncSoon() {
  if (syncing || !navigator.onLine) return paintNet();
  syncing = true;
  try {
    SNAP = adopt(await sync());
    await replayPending();
  } catch {
    // 送不出去就留在 outbox，下次上線或重新開啟再說
  } finally {
    syncing = false;
    await persist();
    paintNet();
  }
}

// 把 outbox 裡尚未確認的事件重新疊到權威狀態上。
// 沒有這一步的話：同步進行中使用者又答了一題，伺服器回來的快照不含那一題，
// 本地狀態就會倒退回去。
async function replayPending() {
  for (const ev of await store.outboxAll()) {
    const q = question(ev.card_id);
    if (!q) continue;
    const remembered = ev.chosen === q.answer && !ev.guessed;
    SNAP.states.set(
      ev.card_id,
      {
        id: ev.card_id, deck: q.deck,
        ...srs.applyFeedback(SNAP.states.get(ev.card_id), remembered, SNAP.params,
                             new Date(ev.reviewed_at)),
      }
    );
  }
  await persist();
}

async function paintNet() {
  const pending = await store.outboxCount();
  const dot = $("#netdot");
  if (!navigator.onLine) {
    dot.hidden = false;
    dot.className = "tag off";
    dot.textContent = pending ? `離線 · ${pending} 待送` : "離線";
  } else if (pending) {
    dot.hidden = false;
    dot.className = "tag pending";
    dot.textContent = `${pending} 待送`;
  } else {
    dot.hidden = true;
  }
}

window.addEventListener("online", syncSoon);
window.addEventListener("offline", paintNet);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) syncSoon();
});

// ── 本地佇列 ──────────────────────────────────────────
function question(id) {
  for (const b of Object.values(BANK)) if (b.byId[id]) return b.byId[id];
  return null;
}

function stateOf(id) {
  const s = SNAP.states.get(id);
  return srs.deriveState(s ? s.review_count : 0, s ? s.next_review_at : null, refNow());
}

function counts(deck) {
  const c = { unlearned: 0, due: 0, reviewed: 0 };
  for (const q of BANK[deck].questions) c[stateOf(q.id)] += 1;
  return c;
}

function buildQueue(deck, kind, category) {
  const now = refNow();
  const unlearned = [];
  const due = [];
  for (const q of BANK[deck].questions) {
    if (category && q.category !== category) continue;
    const s = SNAP.states.get(q.id);
    const st = srs.deriveState(s ? s.review_count : 0, s ? s.next_review_at : null, now);
    if (st === srs.UNLEARNED) unlearned.push(q.id);
    else if (st === srs.DUE) due.push([s && s.next_review_at ? new Date(s.next_review_at) : now, q.id]);
  }
  due.sort((a, b) => a[0] - b[0]);            // 到期由舊到新
  const dueIds = due.map((d) => d[1]);
  const ids = { unlearned, due: dueIds, all: dueIds.concat(unlearned) }[kind];
  return kind === "unlearned" ? ids : shuffled(ids);
}

function shuffled(a) {
  const x = a.slice();
  for (let i = x.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [x[i], x[j]] = [x[j], x[i]];
  }
  return x;
}

// ── 首頁 ──────────────────────────────────────────────
function renderHome() {
  $("#view-home").innerHTML =
    SNAP.decks.map((d) => deckCard(d, counts(d.deck))).join("") +
    (SNAP.paused
      ? `<p class="home-note tag tag-accent">${ico("pause", "ico ico-sm")}時鐘已凍結 —— 到期時間不會推進</p>`
      : "");

  $("#view-home").querySelectorAll("[data-start]").forEach((b) =>
    b.addEventListener("click", () =>
      startSession(b.dataset.deck, b.dataset.start, $(`#cat-${b.dataset.deck}`).value)
    )
  );
  show("home");
}

function deckCard(d, c) {
  const cats = d.categories
    .map((x) => `<option value="${esc(x.category)}">${esc(x.category)}（${x.count}）</option>`)
    .join("");
  return `
  <article class="deck">
    <h2>${esc(d.label)}</h2>
    <p class="sub kicker">${esc(d.title)} · ${esc(d.version)} · 共 ${d.total} 題</p>
    <div class="metrics">
      <div class="metric"><b>${c.unlearned}</b><span>未學習</span></div>
      <div class="metric${c.due ? " is-accent" : ""}"><b>${c.due}</b><span>待複習</span></div>
      <div class="metric"><b>${c.reviewed}</b><span>已複習</span></div>
    </div>
    <div class="deck-actions">
      <button class="btn btn-primary btn-lg" data-start="due" data-deck="${d.deck}" ${c.due ? "" : "disabled"}>
        ${ico("play", "ico ico-sm")}<span>複習到期</span><span class="btn-count">${c.due}</span></button>
      <button class="btn btn-secondary" data-start="unlearned" data-deck="${d.deck}" ${c.unlearned ? "" : "disabled"}>
        ${ico("plus", "ico ico-sm")}<span>學新題</span><span class="btn-count">${c.unlearned}</span></button>
      <button class="btn btn-secondary" data-start="all" data-deck="${d.deck}">
        ${ico("layers", "ico ico-sm")}<span>全部</span><span class="btn-count">${c.due + c.unlearned}</span></button>
    </div>
    <div class="catrow">
      <label class="field-label" for="cat-${d.deck}">分類範圍</label>
      <select id="cat-${d.deck}" class="input"><option value="">全部分類</option>${cats}</select>
    </div>
  </article>`;
}

function show(view) {
  VIEWS.forEach((v) => ($(`#view-${v}`).hidden = v !== view));
  $("#title").textContent = TITLES[view];
  $("#btn-back").hidden = view !== "session";
  // 作答與完成沒有對應的 nav 鍵，那時就全部不標 —— 標錯的當前位置比不標更糟
  document.querySelectorAll(".nav-links [data-nav]").forEach((b) => {
    if (b.dataset.nav === view) b.setAttribute("aria-current", "page");
    else b.removeAttribute("aria-current");
  });
  window.scrollTo(0, 0);
}

// ── 作答 ──────────────────────────────────────────────
function startSession(deck, kind, category) {
  const ids = buildQueue(deck, kind, category);
  if (!ids.length) return renderHome();
  S = { ids, i: 0, deck, answered: false, cardId: null, t0: 0 };
  show("session");
  loadCard();
}

function loadCard() {
  const id = S.ids[S.i];
  S.cardId = id;
  S.answered = false;
  const q = question(id);

  $("#progress-bar").style.width = `${(S.i / S.ids.length) * 100}%`;
  $("#progress-text").textContent = `${S.i + 1} / ${S.ids.length}　·　${q.category}`;
  $("#btn-shuffle").disabled = S.ids.length - S.i < 2;
  $("#q-meta").textContent = `${q.id}　${q.architecture}`;
  $("#q-stem").textContent = q.stem;

  // 題幹是圖的題目（汽車 95 題、機車 126 題）沒有文字題幹，圖就是題目本身
  const stemImgs = q.options.some((o) => o.is_image) ? [] : q.images;
  $("#q-images").innerHTML = stemImgs.map((im) => `<img src="/${im.file}" alt="題目圖">`).join("");

  $("#options").innerHTML = q.options
    .map((o, idx) => {
      // 選項本身是圖的 6 題（汽車 183/185/233/523/723/1004）走這個分支
      const body = o.is_image && q.images[idx]
        ? `<img src="/${q.images[idx].file}" alt="選項 ${o.n}">`
        : esc(o.text);
      return `<button class="opt" data-n="${o.n}">
        <span class="n">${o.n}</span><span class="body">${body}</span><span class="mark"></span>
      </button>`;
    })
    .join("");
  $("#options").querySelectorAll(".opt").forEach((b) =>
    b.addEventListener("click", () => submit(Number(b.dataset.n)))
  );

  const st = SNAP.states.get(id);
  $("#q-warn").innerHTML = st && st.lapse_count
    ? `${ico("alert", "ico ico-sm")}<span>這題你答錯過 ${st.lapse_count} 次</span>` : "";
  $("#q-warn").hidden = !(st && st.lapse_count);

  $("#verdict").hidden = true;
  $("#btn-next").hidden = true;
  $("#btn-guessed").hidden = false;
  $("#note").value = SNAP.notes.get(id) || "";
  $("#note-status").textContent = "";
  $("#note-box").open = false;
  S.t0 = performance.now();
}

// 本地算完就直接顯示，不等網路。伺服器之後重放同一個事件會得到同樣的結果。
async function submit(chosen, guessed = false) {
  if (S.answered) return;
  S.answered = true;

  const q = question(S.cardId);
  const correct = chosen === q.answer;
  const remembered = correct && !guessed;
  const before = SNAP.states.get(q.id);
  const now = refNow();
  const after = srs.applyFeedback(before, remembered, SNAP.params, now);

  SNAP.states.set(q.id, { id: q.id, deck: q.deck, ...after });

  const ev = {
    client_event_id: eventId(),
    card_id: q.id,
    chosen,
    guessed,
    elapsed_ms: Math.round(performance.now() - S.t0),
    reviewed_at: new Date().toISOString(),   // 真實牆鐘時間，供熱力圖與補送排序
  };
  await store.outboxPut(ev);
  await persist();

  S.chosen = chosen;
  S.before = before || null;
  S.now = now;
  S.eventId = ev.client_event_id;

  paintVerdict({ correct, answer: q.answer, remembered, ...after }, chosen);
  paintNet();
  syncSoon();
}

function paintVerdict(r, chosen) {
  $("#options").querySelectorAll(".opt").forEach((b) => {
    const n = Number(b.dataset.n);
    b.disabled = true;
    // 顏色不是唯一線索：正解與誤選各自帶一個圖示，色盲與強光下都讀得出來
    if (n === r.answer) { b.classList.add("correct"); b.querySelector(".mark").innerHTML = ico("check"); }
    else if (n === chosen) { b.classList.add("wrong"); b.querySelector(".mark").innerHTML = ico("x"); }
  });
  const v = $("#verdict-text");
  v.textContent = r.correct ? "答對" : "答錯";
  v.className = r.correct ? "" : "bad";
  $("#verdict-icon").innerHTML = `<use href="#i-${r.correct ? "check" : "x"}"></use>`;
  $("#verdict-icon").style.color = r.correct ? "" : "var(--color-accent)";
  $("#verdict-sched").textContent =
    `下次複習：${fmtDue(r.next_review_at)}（間隔 ${fmtHours(r.interval_hours)}）` +
    (r.correct ? "" : `　·　累計答錯 ${r.lapse_count} 次`);
  $("#btn-guessed").hidden = !r.correct || !r.remembered;
  $("#verdict").hidden = false;
  $("#btn-next").hidden = false;
}

// 「我是猜的」＝ 把上一次作答改判為 forgot，不是再送一次作答
// （再送會從已放大的間隔再乘 0.5，60h → 30h 而不是應有的 12h，review_count 也會多一次）。
$("#btn-guessed").addEventListener("click", async () => {
  if (!S || !S.answered) return;
  const q = question(S.cardId);
  const pending = await store.outboxGet(S.eventId);

  if (pending) {
    // 還沒送出去 —— 直接改 outbox 裡那筆，並用當初的 before 重算，本地就能完成
    pending.guessed = true;
    await store.outboxPut(pending);
    const after = srs.applyFeedback(S.before, false, SNAP.params, S.now);
    SNAP.states.set(q.id, { id: q.id, deck: q.deck, ...after });
    await persist();
    paintVerdict({ correct: true, answer: q.answer, remembered: false, ...after }, S.chosen);
  } else {
    // 已經送出去了 —— 請伺服器用事件裡的 before 快照精確回捲
    try {
      const r = await api("/api/answer/regrade", {
        method: "POST", body: JSON.stringify({ card_id: S.cardId }),
      });
      SNAP.states.set(q.id, {
        id: q.id, deck: q.deck,
        interval_hours: r.interval_hours, next_review_at: r.next_review_at,
        last_reviewed_at: new Date().toISOString(), review_count: r.review_count,
        lapse_count: r.lapse_count, streak: r.streak, last_feedback: srs.FORGOT,
      });
      await persist();
      paintVerdict({ ...r, remembered: false }, S.chosen);
    } catch {
      flash($("#btn-guessed"), "離線，稍後再試");
      return;
    }
  }
  $("#btn-guessed").hidden = true;
});

// 洗牌（照 kg：只打亂「還沒作答」的尾段，已經做過的不動，才不會重複刷同一批）
$("#btn-shuffle").addEventListener("click", () => {
  if (!S) return;
  const from = S.answered ? S.i + 1 : S.i;
  const tail = shuffled(S.ids.slice(from));
  S.ids = S.ids.slice(0, from).concat(tail);
  if (!S.answered) loadCard();
  flash($("#btn-shuffle"), `已洗牌（剩 ${tail.length} 題）`);
});

// 存 innerHTML 而不是 textContent —— 按鈕裡有 <svg> 圖示，用 textContent 還原會把它吃掉
function flash(btn, msg) {
  if (btn.dataset.flashing) return;
  const old = btn.innerHTML;
  btn.dataset.flashing = "1";
  btn.textContent = msg;
  btn.disabled = true;
  setTimeout(() => {
    btn.innerHTML = old;
    btn.disabled = false;
    delete btn.dataset.flashing;
  }, 1100);
}

$("#btn-next").addEventListener("click", next);
function next() {
  S.i += 1;
  if (S.i >= S.ids.length) { show("done"); S = null; return; }
  loadCard();
}

// 筆記：離開輸入框才存，不要每個字打一次；離線時排進 notes_dirty 等補送
$("#note").addEventListener("blur", async () => {
  if (!S) return;
  const id = S.cardId;
  const body = $("#note").value.trim();
  if ((SNAP.notes.get(id) || "") === body) return;

  body ? SNAP.notes.set(id, body) : SNAP.notes.delete(id);
  const dirty = (await store.kvGet("notes_dirty")) || {};
  dirty[id] = body;
  await store.kvPut("notes_dirty", dirty);
  await persist();
  $("#note-status").textContent = navigator.onLine ? "已儲存" : "已存在本機，連線後同步";
  syncSoon();
});

// 桌機鍵盤流（比照 kg 的 Mac Catalyst）
document.addEventListener("keydown", (e) => {
  if ($("#view-session").hidden) return;
  if (e.target.tagName === "TEXTAREA" || e.target.tagName === "INPUT") return;
  if (["1", "2", "3"].includes(e.key)) { submit(Number(e.key)); e.preventDefault(); }
  else if (e.key === " " || e.key === "Enter") {
    if (S && S.answered) { next(); e.preventDefault(); }
  } else if (e.key.toLowerCase() === "g") { $("#btn-guessed").click(); }
  else if (e.key.toLowerCase() === "s") { $("#btn-shuffle").click(); }
  else if (e.key.toLowerCase() === "n") { $("#note-box").open = true; $("#note").focus(); }
});

// ── 題庫瀏覽（全本地，離線一樣好用）──────────────────
const STATE_LABEL = { unlearned: "未學習", due: "待複習", reviewed: "已複習" };

function renderBrowse() {
  fillDeckSelect("#br-deck");
  const d = BANK[$("#br-deck").value];
  $("#br-cat").innerHTML = `<option value="">全部分類</option>` +
    d.categories.map((c) => `<option value="${esc(c.category)}">${esc(c.category)}</option>`).join("");
  refreshBrowse();
  show("browse");
}

function refreshBrowse() {
  const d = BANK[$("#br-deck").value];
  const term = $("#br-q").value.trim();
  const cat = $("#br-cat").value;
  const wrongOnly = $("#br-wrong").checked;
  const notedOnly = $("#br-noted").checked;

  const items = d.questions.filter((q) => {
    if (cat && q.category !== cat) return false;
    const st = SNAP.states.get(q.id);
    if (wrongOnly && !(st && st.lapse_count)) return false;
    if (notedOnly && !SNAP.notes.has(q.id)) return false;
    if (term && !(q.stem + q.options.map((o) => o.text).join("")).includes(term)) return false;
    return true;
  });

  $("#br-count").textContent =
    `${items.length} 題` + (items.length > 400 ? "（顯示前 400 題）" : "");
  $("#br-list").innerHTML = items.slice(0, 400).map((q) => {
    const st = SNAP.states.get(q.id);
    // 答錯次數不再跟「到期」搶同一個紅點（兩個都紅等於沒有區別），改成獨立標籤
    const lapses = st && st.lapse_count
      ? `<span class="tag tag-accent trail">錯 ${st.lapse_count}</span>` : "";
    // 圖片題在清單裡只剩「（圖片題）」四個字，等於認不出來。放張縮圖，
    // 圖本來就已經在 Cache Storage 裡，lazy 載入不花任何網路。
    const thumb = q.images && q.images.length
      ? `<img class="thumb" src="/${q.images[0].file}" alt="" loading="lazy" decoding="async">` : "";
    return `<button class="row" data-id="${q.id}">
      <span class="dot ${stateOf(q.id)}" title="${STATE_LABEL[stateOf(q.id)]}"></span>
      <span class="num">${q.number}</span>
      ${thumb}
      <span class="txt">${esc(q.stem_is_image ? "（圖片題）" : q.stem)}</span>
      ${lapses}
    </button>`;
  }).join("");
  $("#br-list").querySelectorAll(".row").forEach((b) =>
    b.addEventListener("click", () => {
      S = { ids: [b.dataset.id], i: 0, deck: $("#br-deck").value };
      show("session"); loadCard();
    })
  );
}
["#br-q", "#br-cat", "#br-wrong", "#br-noted"].forEach((s) =>
  $(s).addEventListener("input", refreshBrowse)
);
$("#br-deck").addEventListener("change", renderBrowse);

// ── 統計 ──────────────────────────────────────────────
async function renderStats() {
  fillDeckSelect("#st-deck");
  const deck = $("#st-deck").value;
  let s = null;
  try {
    if (navigator.onLine) s = await api(`/api/stats?deck=${deck}`);
  } catch { /* 離線就用本地能算的那部分 */ }

  const local = localStats(deck);
  const forecast = s ? s.forecast_14d : local.forecast;
  const worst = s ? s.worst_categories : local.worst;
  const max = Math.max(1, ...forecast.map((f) => f.count));

  $("#st-body").innerHTML = `
    <div class="sec">概況${s ? "" : `<span class="sec-note">離線 · 僅本機資料</span>`}</div>
    <div class="tiles">
      <div class="tile"><b>${s ? s.answered_total : local.answered}</b><span>累計作答</span></div>
      <div class="tile"><b>${s && s.accuracy !== null ? Math.round(s.accuracy * 100) + "%" : "—"}</b><span>正確率</span></div>
      <div class="tile"><b>${s ? s.streak_days : "—"}</b><span>連續天數</span></div>
    </div>
    ${s ? "" : `<p class="muted">正確率與連續天數需要伺服器上的作答紀錄。</p>`}
    <div class="sec">未來 14 天到期量<span class="sec-note">尖峰 ${max} 題</span></div>
    <div class="chart">
      <div class="bars">${forecast.map((f) =>
        `<div class="${f.count ? "" : "empty"}" style="height:${Math.max((f.count / max) * 100, 1.5)}%" title="${f.day}：${f.count} 題"></div>`).join("")}</div>
    </div>
    <div class="barlabels">${forecast.map((f, i) =>
      `<span>${i % 2 ? "" : f.day.slice(8)}</span>`).join("")}</div>
    <div class="sec">最常答錯的分類<span class="sec-note">點一列直接複習</span></div>
    ${worst.length
      ? `<div class="rows">${worst.map((c) => `<button class="row" data-cat="${esc(c.category)}">
          <span class="txt">${esc(c.category)}</span>
          <span class="tag tag-accent trail">錯 ${c.lapses}</span>
          ${ico("chevron", "ico ico-sm")}</button>`).join("")}</div>`
      : `<p class="muted">還沒有答錯紀錄。</p>`}`;

  // 統計不該只能看 —— 點分類直接開一輪那個分類的複習
  $("#st-body").querySelectorAll("[data-cat]").forEach((b) =>
    b.addEventListener("click", () => startSession(deck, "all", b.dataset.cat))
  );
  show("stats");
}

function localStats(deck) {
  const now = refNow();
  const day0 = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  const buckets = new Array(14).fill(0);
  let answered = 0;
  const worst = new Map();

  for (const q of BANK[deck].questions) {
    const st = SNAP.states.get(q.id);
    if (!st || !st.review_count) continue;
    answered += st.review_count;
    if (st.lapse_count) worst.set(q.category, (worst.get(q.category) || 0) + st.lapse_count);
    if (!st.next_review_at) continue;
    const d = new Date(st.next_review_at);
    const idx = Math.max(0, Math.round(
      (new Date(d.getFullYear(), d.getMonth(), d.getDate()) - day0) / 86400e3));
    if (idx < 14) buckets[idx] += 1;
  }

  return {
    answered,
    forecast: buckets.map((count, i) => ({
      day: new Date(day0.getTime() + i * 86400e3).toISOString().slice(0, 10), count,
    })),
    worst: [...worst.entries()].sort((a, b) => b[1] - a[1]).slice(0, 10)
      .map(([category, lapses]) => ({ category, lapses })),
  };
}
$("#st-deck").addEventListener("change", renderStats);

// ── 設定 ──────────────────────────────────────────────
async function renderSettings() {
  const p = SNAP.params;
  const off = !navigator.onLine;
  $("#set-body").innerHTML = `
    <div class="set-block">
      <h3>複習節奏</h3>
      <dl class="kv">
        <div><dt>首次</dt><dd>${fmtHours(p.initial_hours)}</dd></div>
        <div><dt>答對</dt><dd>×${p.remembered_mult}</dd></div>
        <div><dt>答錯</dt><dd>×${p.forgot_mult}</dd></div>
        <div><dt>範圍</dt><dd>${fmtHours(p.min_hours)}–${fmtHours(p.max_hours)}</dd></div>
      </dl>
      <select id="set-preset" class="input" ${off ? "disabled" : ""}>${SNAP.presets.map((n) =>
        `<option value="${n}" ${n === SNAP.preset ? "selected" : ""}>${n === "relaxed" ? "relaxed（寬鬆，預設）" : "intensive（密集）"}</option>`).join("")}</select>
      ${off ? `<p class="muted" style="margin-top:var(--space-2)">離線時無法變更 —— 這是全域設定，必須由伺服器裁決。</p>` : ""}
    </div>
    <div class="set-block">
      <h3>凍結時鐘</h3>
      <p>暫停期間到期時間不再推進，回來時不會有一座待複習山。</p>
      <div class="switch">
        <span>暫停累積${SNAP.paused && SNAP.paused_at ? `（自 ${SNAP.paused_at.slice(0, 10)}）` : ""}</span>
        <label class="toggle"><input type="checkbox" id="set-pause" ${SNAP.paused ? "checked" : ""} ${off ? "disabled" : ""}><span class="track"></span></label>
      </div>
    </div>
    <div class="set-block">
      <h3>離線可用</h3>
      <p id="set-cache">清點中…</p>
      <p id="set-pending" class="muted"></p>
      <button class="btn btn-secondary" id="set-precache" ${off ? "disabled" : ""}>
        ${ico("download", "ico ico-sm")}<span>重新下載離線題庫</span></button>
    </div>
    <div class="set-block">
      <h3>答錯不會歸零</h3>
      <p>答錯是把間隔乘上 ${p.forgot_mult}、不是打回新卡。排到 30 天的題答錯後回到 15 天，累積的記憶強度會保留。</p>
    </div>`;

  $("#set-pending").textContent = `待補送的作答：${await store.outboxCount()} 筆`;
  $("#set-cache").textContent = await cacheReport();
  $("#set-preset").addEventListener("change", async (e) => {
    await api("/api/settings", { method: "PUT", body: JSON.stringify({ preset: e.target.value }) });
    SNAP = adopt(await api("/api/bootstrap"));
    await persist(); renderSettings();
  });
  $("#set-pause").addEventListener("change", async (e) => {
    await api("/api/settings", { method: "PUT", body: JSON.stringify({ paused: e.target.checked }) });
    SNAP = adopt(await api("/api/bootstrap"));
    await persist(); renderSettings();
  });
  $("#set-precache").addEventListener("click", () => {
    navigator.serviceWorker && navigator.serviceWorker.controller &&
      navigator.serviceWorker.controller.postMessage("precache-images");
    flash($("#set-precache"), "下載中…");
  });
  show("settings");
}

// 直接數 Cache Storage，不靠 service worker 回報 —— SW 只在下載當下發訊息，
// 之後再開設定頁就什麼都收不到，畫面會停在「即將下載」這種不實的字串上。
async function cacheReport() {
  if (!("caches" in window)) return "這個瀏覽器不支援離線快取。";
  let imgs = 0;
  let banks = 0;
  for (const name of await caches.keys()) {
    for (const req of await (await caches.open(name)).keys()) {
      const p = new URL(req.url).pathname;
      if (p.startsWith("/images/")) imgs += 1;
      else if (p.startsWith("/data/")) banks += 1;
    }
  }
  if (!banks) return "題庫還沒下載到這台裝置 —— 現在是連線才能用。";
  return `題庫 ${banks} / 2 份、題目圖 ${imgs} 張已存在這台裝置，可完全離線使用。`;
}

// ── service worker ────────────────────────────────────
function registerSW() {
  if (!("serviceWorker" in navigator)) return;

  // 換了新版 SW 就重載一次，否則會停在舊的 app shell 上（stale-while-revalidate
  // 本來就是「這次給舊的、下次才新的」）。第一次註冊時 controller 是 null，
  // 那不算換版，不能重載，否則每個新裝置都會白閃一次。
  const hadController = !!navigator.serviceWorker.controller;
  let reloading = false;
  navigator.serviceWorker.addEventListener("controllerchange", () => {
    if (!hadController || reloading) return;
    reloading = true;
    location.reload();
  });

  navigator.serviceWorker.register("/sw.js").catch((e) => console.warn("SW 註冊失敗", e));
  // 下載進行中才用得到：讓設定頁的數字即時跳動，停下來後由 cacheReport 接手
  navigator.serviceWorker.addEventListener("message", (e) => {
    const el = $("#set-cache");
    if (el && e.data && e.data.type === "images") {
      el.textContent = `下載離線題目圖 ${e.data.done} / ${e.data.total} 張…`;
    }
  });
}

// ── 雜項 ──────────────────────────────────────────────
function fillDeckSelect(sel) {
  if ($(sel).options.length) return;
  $(sel).innerHTML = SNAP.decks.map((d) => `<option value="${d.deck}">${esc(d.label)}</option>`).join("");
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}
function fmtHours(h) {
  if (h < 24) return `${+h.toFixed(1)} 小時`;
  const d = h / 24;
  return d < 30 ? `${+d.toFixed(1)} 天` : `${+(d / 30).toFixed(1)} 個月`;
}
function fmtDue(iso) {
  // 用「日曆日」差，不是經過時數。12 小時後可能已是明天凌晨，算成 0 天會印出
  // 「今天 7/28」這種自相矛盾的字串。
  const dt = new Date(iso);
  const midnight = (d) => new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
  const days = Math.round((midnight(dt) - midnight(new Date())) / 86400000);
  const time = dt.toLocaleString("zh-TW", { hour: "2-digit", minute: "2-digit" });
  const date = dt.toLocaleString("zh-TW", { month: "numeric", day: "numeric" });
  if (days <= 0) return `今天 ${time}`;
  if (days === 1) return `明天 ${time}`;
  return `${date} ${time}（${days} 天後）`;
}

document.querySelectorAll("[data-nav]").forEach((b) =>
  b.addEventListener("click", () => {
    const v = b.dataset.nav;
    if (v === "home") renderHome();
    else if (v === "browse") renderBrowse();
    else if (v === "stats") renderStats();
    else if (v === "settings") renderSettings();
  })
);
$("#btn-back").addEventListener("click", renderHome);

boot();
