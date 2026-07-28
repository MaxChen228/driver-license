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

// X-DL-Client 讓任何跨站的表單／img 式偽造直接死在伺服器的 middleware：
// 自訂標頭會觸發 preflight，而我們不回應 preflight。
const api = async (path, opts) => {
  const r = await fetch(path, {
    headers: { "content-type": "application/json", "x-dl-client": "1" },
    ...opts,
  });
  if (!r.ok) throw new Error(`${r.status} ${path}`);
  return r.json();
};

const VIEWS = ["home", "session", "done", "browse", "stats", "settings"];
const TITLES = {
  home: "駕照筆試", session: "作答", done: "完成",
  browse: "題庫", stats: "統計", settings: "設定",
};

let BANK = {};      // deck → { questions, byId, categories, label, title, version }
let CONFIG = null;  // /api/config：題庫目錄，對所有人都一樣，可長期快取
let SNAP = null;    // 使用者狀態：preset / paused / states(Map) / notes(Map)
let MODE = "local"; // "local" = 匿名純本機；"account" = 已登入，伺服器是權威
let ME = null;      // 登入中的帳號（未登入為 null）
let S = null;       // 目前 session
let syncing = false;

const params = () => srs.params(SNAP.preset);
const signedIn = () => MODE === "account";

// ── 開機 ──────────────────────────────────────────────
// 順序是重點：題庫與設定檔先到位，app 就能用了。身分是之後的事，
// 而且失敗不影響刷題 —— 匿名模式本來就不需要伺服器。
async function boot() {
  registerSW();
  store.requestPersistence();

  try {
    CONFIG = await loadConfig();
  } catch {
    return bootFail();
  }
  try {
    BANK = await loadBanks(CONFIG.decks);
  } catch {
    return bootFail();
  }

  const round = await store.kvGet("round_size");
  if (typeof round === "number") ROUND = round;
  MODE = (await store.kvGet("mode")) || "local";
  if (MODE === "account") ME = { username: (await store.kvGet("username")) || "" };
  SNAP = adopt((await store.kvGet("snapshot")) || emptySnapshot());

  if (signedIn()) {
    try {
      ME = await api("/api/me");
      SNAP = adopt(await sync({ silent: true }));
      await replayPending();
    } catch (e) {
      // 401 = session 過期。切回本機繼續用，不清資料 —— 只有明確登出才清。
      if (String(e.message).startsWith("401")) await goLocal();
      else console.warn("開機時同步失敗（離線或伺服器有問題），先用本機快照", e);
    }
  }

  $("#boot").hidden = true;
  paintNet();
  renderHome();
}

function emptySnapshot() {
  return {
    preset: (CONFIG && CONFIG.default_preset) || srs.DEFAULT_PRESET,
    paused: false, paused_at: null, states: [], notes: {},
  };
}

// 設定檔：有網路就更新，沒網路就用上次存的。service worker 也預先快取了它，
// 所以第一次以外的離線開機都拿得到。
async function loadConfig() {
  try {
    const c = await api("/api/config");
    await store.kvPut("config", c);
    return c;
  } catch {
    const cached = await store.kvGet("config");
    if (cached) return cached;
    throw new Error("no config");
  }
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
    preset: payload.preset || srs.DEFAULT_PRESET,
    states: new Map((payload.states || []).map((s) => [s.id, s])),
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
  const dirty = (await store.kvGet("notes_dirty")) || {};
  const res = await api("/api/sync", {
    method: "POST",
    body: JSON.stringify({ answers, notes: dirty }),
  });

  // applied 與 skipped 都代表「伺服器已經有了」；failed 是永久性失敗（例如題號不存在），
  // 留著只會每次重試都失敗，一併清掉並留下 console 紀錄。
  const { applied, skipped, failed } = res.sync;
  if (failed.length) console.warn("同步失敗的事件已丟棄", failed);
  await store.outboxRemoveMany([
    ...applied, ...skipped, ...failed.map((f) => f.client_event_id),
  ]);
  // 只刪這次真的送出去的那幾把 key。整包清掉的話，POST 進行中寫的筆記會被吃掉，
  // 而且完全沒有跡象 —— outbox 那邊本來就只刪伺服器確認過的，這裡跟它一致。
  const still = (await store.kvGet("notes_dirty")) || {};
  for (const k of Object.keys(dirty)) if (still[k] === dirty[k]) delete still[k];
  await store.kvPut("notes_dirty", still);

  if (!silent) paintNet();
  return res;
}

async function syncSoon() {
  if (!signedIn() || syncing || !navigator.onLine) return paintNet();
  syncing = true;
  try {
    SNAP = adopt(await sync());
    await replayPending();
  } catch (e) {
    if (String(e.message).startsWith("401")) await goLocal();
    // 其餘（送不出去）就留在 outbox，下次上線或重新開啟再說
  } finally {
    syncing = false;
    await persist();
    paintNet();
  }
}

// 登入失效時退回本機模式。刻意不清資料：使用者沒有要求登出，
// 手上的進度是他的，只是暫時沒有伺服器可同步而已。
async function goLocal() {
  MODE = "local";
  ME = null;
  await store.kvPut("mode", "local");
  if (typeof renderSettings === "function" && !$("#view-settings").hidden) renderSettings();
}

// 把 outbox 裡尚未確認的事件重新疊到權威狀態上。
// 只在「剛採用伺服器回應」之後呼叫 —— 對著本機快照再疊一次會重複套用，
// 因為 persist() 存下來的 SNAP 已經含了那些作答的效果。
async function replayPending() {
  for (const ev of await store.outboxAll()) {
    const q = question(ev.card_id);
    if (!q) continue;
    const remembered = ev.chosen === q.answer && !ev.guessed;
    SNAP.states.set(
      ev.card_id,
      {
        id: ev.card_id, deck: q.deck,
        ...srs.applyFeedback(SNAP.states.get(ev.card_id), remembered,
                             srs.params(ev.preset || SNAP.preset),
                             new Date(ev.reviewed_at)),
      }
    );
  }
  await persist();
}

async function paintNet() {
  const dot = $("#netdot");
  if (!signedIn()) {          // 匿名：沒有「待送」這回事，伺服器一列都不寫
    dot.hidden = navigator.onLine;
    dot.className = "tag off";
    dot.textContent = "離線";
    return;
  }
  const pending = await store.outboxCount();
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
    CONFIG.decks.map((d) => deckCard(d, counts(d.deck))).join("") +
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
// 一輪的題數。0 = 不分節（整副佇列一次做完，也就是原本的行為）。
// 為什麼需要它：「學新題」按下去是 1090 題的佇列，進度條一題只前進 0.09%，
// 視覺上永遠不動，而「完成」畫面實質上到不了 —— 那個 1/1090 只有勸退效果。
// 這不是每日上限（README 的「刻意沒有每日上限」仍然成立）：做完一輪可以立刻
// 再來一輪，只是中間給一個結算與一個決定要不要繼續的點。
// 存在裝置本機、不同步：這是顯示偏好，不是學習模型的一部分，伺服器不該有它。
let ROUND = 20;
const ROUND_CHOICES = [10, 20, 30, 50, 0];

function startSession(deck, kind, category) {
  const ids = buildQueue(deck, kind, category);
  if (!ids.length) return renderHome();
  S = {
    ids: ROUND > 0 ? ids.slice(0, ROUND) : ids,
    i: 0, deck, kind, category,
    answered: false, cardId: null, t0: 0,
    right: 0, guessed: 0,
  };
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
  // 圖片題（汽車 95、機車 126）沒有文字題幹，畫面上半部就完全沒有「要回答什麼」。
  // 補一行提示，但樣式刻意跟真題幹拉開兩級（見 style.css 的 .is-fallback）——
  // 這個題庫的價值在於逐字元對帳過，混進一句我們自己寫的話不能看起來像題目本文。
  const stemIsImage = q.stem_is_image || !q.stem;
  $("#q-stem").textContent = stemIsImage ? "依下圖作答" : q.stem;
  $("#q-stem").className = stemIsImage ? "is-fallback" : "";

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
  $("#next-bar").hidden = true;
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
  if (correct) S.right += 1;   // 本輪結算用
  const remembered = correct && !guessed;
  const before = SNAP.states.get(q.id);
  const now = refNow();
  const after = srs.applyFeedback(before, remembered, params(), now);

  SNAP.states.set(q.id, { id: q.id, deck: q.deck, ...after });

  const ev = {
    client_event_id: eventId(),
    card_id: q.id,
    chosen,
    guessed,
    elapsed_ms: Math.round(performance.now() - S.t0),
    reviewed_at: new Date().toISOString(),   // 真實牆鐘時間，供熱力圖與補送排序
    preset: SNAP.preset,  // 重放時要用「當時」的參數，不是現在的（比照 schema v3）
  };
  // events 是這台裝置的完整歷史，兩種模式都寫、永遠留著（登入時要拿它併進帳號，
  // 匿名時要拿它算正確率與連續天數）。outbox 只是待補送佇列，匿名模式不寫 ——
  // 「匿名」的定義就是伺服器一列都不寫。
  await store.eventsPut(ev);
  if (signedIn()) await store.outboxPut(ev);
  await persist();

  S.chosen = chosen;
  S.before = before || null;
  S.now = now;
  S.eventId = ev.client_event_id;

  // 答錯給一下短震動。手機上的即時回饋目前只有顏色變化，而這是一天會遇到
  // 上百次的那個瞬間。答對不震 —— 對的事情不需要打斷你。
  if (!correct && navigator.vibrate) navigator.vibrate(30);

  paintVerdict({ correct, answer: q.answer, remembered, ...after }, chosen);
  paintNet();
  syncSoon();
}

function paintVerdict(r, chosen) {
  $("#options").querySelectorAll(".opt").forEach((b) => {
    const n = Number(b.dataset.n);
    b.disabled = true;
    // 顏色不是唯一線索：正解與誤選各自帶一個圖示，色盲與強光下都讀得出來。
    // 但 class 與圖示都是純視覺的 —— 螢幕閱讀器逐項瀏覽時完全讀不出哪個是正解，
    // 所以同一件事也要寫進無障礙名稱。
    const body = b.querySelector(".body").textContent.trim();
    const label = (prefix) => `${prefix}，選項 ${n}${body ? "：" + body : ""}`;
    if (n === r.answer) {
      b.classList.add("correct");
      b.querySelector(".mark").innerHTML = ico("check");
      b.setAttribute("aria-label", label("正解"));
    } else if (n === chosen) {
      b.classList.add("wrong");
      b.querySelector(".mark").innerHTML = ico("x");
      b.setAttribute("aria-label", label("你選的，答錯"));
    }
  });
  const v = $("#verdict-text");
  v.textContent = r.correct ? "答對" : "答錯";
  v.className = r.correct ? "" : "bad";
  $("#verdict-icon").innerHTML = `<use href="#i-${r.correct ? "check" : "x"}"></use>`;
  $("#verdict-icon").style.color = r.correct ? "" : "var(--color-accent)";
  // 原本還接一句「（間隔 2.5 天）」—— 跟前面的「3 天後」是同一件事的兩種單位，
  // 連著兩個括號。間隔屬於演算法的內部量，設定頁的「複習節奏」已經完整交代。
  $("#verdict-sched").textContent =
    `下次複習：${fmtDue(r.next_review_at)}` +
    (r.correct ? "" : `　·　累計答錯 ${r.lapse_count} 次`);
  $("#btn-guessed").hidden = !r.correct || !r.remembered;
  $("#verdict").hidden = false;
  $("#next-bar").hidden = false;
  ensureVisible($("#verdict"));
  announce(
    (r.correct ? "答對" : `答錯，正解是選項 ${r.answer}`) +
    `。下次複習 ${fmtDue(r.next_review_at)}。`
  );
  // 焦點原本留在剛剛按下、而且已經 disabled 的那顆選項上 —— disabled 的元素
  // 不能有焦點，於是整個掉回 body：鍵盤使用者要重新 Tab 一輪，螢幕閱讀器的
  // 游標也回到頁首。交給下一題，接下來的動作就在手上。
  $("#btn-next").focus({ preventScroll: true });
}

// 螢幕閱讀器播報。同一段文字連送兩次不會被唸第二次，而「答對」後面接「答對」
// 是這個 app 最常見的序列 —— 補一個零寬字元讓內容確實有變化。
function announce(text) {
  const el = $("#sr-live");
  el.textContent = el.textContent === text ? text + "​" : text;
}

// 判定出現後把它捲進畫面 —— 只補不足的那一段，不重新定位整頁（使用者的視線
// 本來就停在他剛剛按下的那個選項上，整頁跳動比不捲更糟）。
// 底部要扣掉 sticky 的下一題列，否則判定會剛好被它蓋掉。
// 刻意用 behavior:"auto"（瞬移）而不是 "smooth"。實測在這個頁面上 smooth 是
// 完全的 no-op：同一個位置 `scrollBy({top:100, behavior:'smooth'})` 之後
// scrollY 仍是 0，換成 'auto' 就是 100 —— 而且不會有任何錯誤。這個修正的
// 全部意義就是「使用者不必自己捲」，不能建立在一個會安靜失效的動畫上。
// 瞬移在這裡也比較誠實：使用者剛按下答案，要的是立刻看到結果。
function ensureVisible(el) {
  const limit = window.innerHeight - $("#next-bar").offsetHeight - 8;
  const bottom = el.getBoundingClientRect().bottom;
  if (bottom <= limit) return;
  window.scrollBy({ top: bottom - limit, behavior: "auto" });
}

// 「我是猜的」＝ 把上一次作答改判為 forgot，不是再送一次作答
// （再送會從已放大的間隔再乘 0.5，60h → 30h 而不是應有的 12h，review_count 也會多一次）。
$("#btn-guessed").addEventListener("click", async () => {
  if (!S || !S.answered) return;
  const q = question(S.cardId);
  // 匿名模式沒有 outbox，那一筆永遠只在本地 —— 一律走本地重算這條路。
  const pending = signedIn() ? await store.outboxGet(S.eventId) : true;

  if (pending) {
    // 還沒送出去 —— 直接改本地那筆，並用當初的 before 重算，不需要網路
    const ev = await store.eventsGet(S.eventId);
    if (ev) {
      ev.guessed = true;
      await store.eventsPut(ev);
      if (signedIn()) await store.outboxPut(ev);
    }
    const after = srs.applyFeedback(S.before, false, params(), S.now);
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
  // 本輪結算：按了「我是猜的」就不算真的會，從答對數扣回來另外記
  S.guessed += 1;
  if (S.right > 0) S.right -= 1;
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
  if (S.i >= S.ids.length) return finishRound();
  loadCard();
}

function finishRound() {
  // 題庫頁點單一題進來的那條路沒有 kind，不是「一輪」，也沒有結算可言。
  if (!S.kind) { S = null; return renderHome(); }
  const r = {
    deck: S.deck, kind: S.kind, category: S.category,
    total: S.ids.length, right: S.right, guessed: S.guessed,
  };
  S = null;
  renderDone(r);
}

function renderDone(r) {
  // 剩幾題要重新算，不能拿原本佇列的尾巴 —— 剛剛答過的那些已經不再到期／未學，
  // 而且答錯的可能又排回來了。重建一次才是當下的事實。
  const remaining = buildQueue(r.deck, r.kind, r.category).length;
  const pct = Math.round((r.right / r.total) * 100);
  $("#view-done").innerHTML = `
    <div class="done">
      ${ico("check", "ico tick")}
      <h2>本輪完成</h2>
      <p class="done-score"><b>${r.right} / ${r.total}</b>
        <span>答對 ${pct}%${r.guessed ? `　·　其中 ${r.guessed} 題按了「我是猜的」` : ""}</span></p>
      <p>${remaining
        ? `這副牌還剩 ${remaining} 題。`
        : "這副牌目前沒有到期或未學的題目了。"}</p>
      <div class="done-actions">
        ${remaining ? `<button class="btn" id="done-again">再來一輪${ico("right", "ico btn-trail")}</button>` : ""}
        <button class="btn" id="done-home">回首頁${ico("right", "ico btn-trail")}</button>
      </div>
    </div>`;
  // 這個畫面是動態產生的，接不到檔尾那個一次性的 [data-nav] 綁定
  if (remaining) {
    $("#done-again").addEventListener("click", () => startSession(r.deck, r.kind, r.category));
  }
  $("#done-home").addEventListener("click", renderHome);
  show("done");
  announce(`本輪完成，答對 ${r.right} 題，共 ${r.total} 題。`);
}

// 筆記：離開輸入框才存，不要每個字打一次；離線時排進 notes_dirty 等補送
$("#note").addEventListener("blur", async () => {
  if (!S) return;
  const id = S.cardId;
  const body = $("#note").value.trim();
  if ((SNAP.notes.get(id) || "") === body) return;

  body ? SNAP.notes.set(id, body) : SNAP.notes.delete(id);
  // 匿名模式沒有要送去哪裡，筆記就住在 SNAP 裡；登入才需要排隊補送。
  if (signedIn()) {
    const dirty = (await store.kvGet("notes_dirty")) || {};
    dirty[id] = body;
    await store.kvPut("notes_dirty", dirty);
  }
  await persist();
  $("#note-status").textContent = !signedIn() || navigator.onLine
    ? "已儲存" : "已存在本機，連線後同步";
  syncSoon();
});

// 桌機鍵盤流（比照 kg 的 Mac Catalyst）
document.addEventListener("keydown", (e) => {
  if ($("#view-session").hidden) return;
  if (e.target.tagName === "TEXTAREA" || e.target.tagName === "INPUT") return;
  if (["1", "2", "3"].includes(e.key)) { submit(Number(e.key)); e.preventDefault(); }
  else if (e.key === " " || e.key === "Enter") {
    // 答完題之後焦點會被移到「下一題」上（見 paintVerdict）。那時空白鍵／Enter
    // 已經是這顆按鈕自己的啟用行為，這裡再叫一次 next() 就會一次跳兩題 ——
    // 理論上 preventDefault() 會擋掉原生啟用，但那條路徑在這裡驗不到（自動化
    // 環境送不進真實鍵盤事件），而代價是安靜地漏掉一題。直接讓路，不賭。
    if (e.target === $("#btn-next")) return;
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

// 分批渲染。原本硬切前 400 題：汽車有 1090 題，一開就被截掉六成，而且沒有
// 分頁也沒有捲動載入 —— 第 401 題之後只能靠搜尋碰運氣。整份 innerHTML 一次
// 塞完也不是辦法（400 列就已經是 1672 個節點）。改成一次 60 列，捲到底再補。
const BROWSE_PAGE = 80;
let brItems = [];
let brShown = 0;
let brTail = null;

function refreshBrowse() {
  const d = BANK[$("#br-deck").value];
  const term = $("#br-q").value.trim();
  const cat = $("#br-cat").value;
  const wrongOnly = $("#br-wrong").checked;
  const notedOnly = $("#br-noted").checked;

  brItems = d.questions.filter((q) => {
    if (cat && q.category !== cat) return false;
    const st = SNAP.states.get(q.id);
    if (wrongOnly && !(st && st.lapse_count)) return false;
    if (notedOnly && !SNAP.notes.has(q.id)) return false;
    if (term && !(q.stem + q.options.map((o) => o.text).join("")).includes(term)) return false;
    return true;
  });

  $("#br-count").textContent = `${brItems.length} 題`;
  $("#br-list").innerHTML = "";
  brShown = 0;
  appendRows();
}

function rowHtml(q) {
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
    <span class="sr-only">${STATE_LABEL[stateOf(q.id)]}</span>
    <span class="num">${q.number}</span>
    ${thumb}
    <span class="txt">${esc(q.stem_is_image ? "（圖片題）" : q.stem)}</span>
    ${lapses}
  </button>`;
}

function appendRows() {
  const more = $("#br-more");
  if (more) more.remove();
  const slice = brItems.slice(brShown, brShown + BROWSE_PAGE);
  $("#br-list").insertAdjacentHTML("beforeend", slice.map(rowHtml).join(""));
  brShown += slice.length;

  const rest = brItems.length - brShown;
  if (!rest) { if (brTail) brTail.disconnect(); return; }
  // 尾端放一顆真的按鈕，而不是一個看不見的哨兵 div。它同時是兩件事：
  // IntersectionObserver 的觀察對象（捲到就自動補，使用者不會看到它），
  // 以及 IO 沒有運作時的手動出口。無限捲動全押在 IO 上的失敗模式是「清單
  // 安靜地停在第 80 題」，比原本硬切 400 還糟；有這顆按鈕就退化成多按一下。
  $("#br-list").insertAdjacentHTML(
    "beforeend",
    `<button class="btn btn-secondary btn-block br-more" id="br-more">載入更多（還有 ${rest} 題）</button>`
  );
  watchTail();
}

// 監看尾端那顆按鈕，進入視野（含 400px 的預載距離）就補下一批。每次都先
// disconnect：舊按鈕已經被換掉了，留著只會在往回捲時重覆觸發。
function watchTail() {
  if (!brTail) {
    brTail = new IntersectionObserver(
      (entries) => { if (entries.some((e) => e.isIntersecting)) appendRows(); },
      { rootMargin: "400px" }
    );
  }
  brTail.disconnect();
  const more = $("#br-more");
  if (more) brTail.observe(more);
}

// 事件委派：列是分批產生的，逐列掛 listener 會漏掉後來補上的那些
$("#br-list").addEventListener("click", (e) => {
  if (e.target.closest("#br-more")) return appendRows();
  const row = e.target.closest(".row");
  if (!row) return;
  // kind 為 null＝這不是「一輪」，做完直接回首頁而不是給結算（見 finishRound）
  S = { ids: [row.dataset.id], i: 0, deck: $("#br-deck").value, kind: null,
        right: 0, guessed: 0 };
  show("session"); loadCard();
});
["#br-q", "#br-cat", "#br-wrong", "#br-noted"].forEach((s) =>
  $(s).addEventListener("input", refreshBrowse)
);
$("#br-deck").addEventListener("change", renderBrowse);

// ── 統計 ──────────────────────────────────────────────
async function renderStats() {
  fillDeckSelect("#st-deck");
  const deck = $("#st-deck").value;
  // 登入且有網路才問伺服器。匿名模式沒有伺服器可問，但本機的事件日誌
  // 含有完整歷史，正確率與連續天數一樣算得出來。
  let s = null;
  try {
    if (signedIn() && navigator.onLine) s = await api(`/api/stats?deck=${deck}`);
  } catch { /* 離線就用本地能算的那部分 */ }

  const local = await localStats(deck);
  const forecast = s ? s.forecast_14d : local.forecast;
  const worst = s ? s.worst_categories : local.worst;
  const max = Math.max(1, ...forecast.map((f) => f.count));

  $("#st-body").innerHTML = `
    <div class="sec">概況${s ? "" : `<span class="sec-note">${signedIn() ? "離線 · " : ""}本機資料</span>`}</div>
    <div class="tiles">
      <div class="tile"><b>${s ? s.answered_total : local.answered}</b><span>累計作答</span></div>
      <div class="tile"><b>${accuracyText(s, local)}</b><span>正確率</span></div>
      <div class="tile"><b>${s ? s.streak_days : local.streak}</b><span>連續天數</span></div>
    </div>
    <div class="sec">未來 14 天到期量<span class="sec-note">尖峰 ${max} 題</span></div>
    <div class="chart">
      <div class="bars">${forecast.map((f) =>
        `<div class="${f.count ? "" : "empty"}" data-h="${Math.max((f.count / max) * 100, 1.5)}" title="${f.day}：${f.count} 題"></div>`).join("")}</div>
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

  // 高度用 JS 設，不寫成 style="" 內聯屬性：CSP 的 style-src 不含 'unsafe-inline'，
  // style-src-attr 會 fallback 到它，內聯屬性一律被擋 —— 長條圖會整排變成高度 0，
  // 而且沒有錯誤訊息，只是圖表變成一條線。CSSOM 賦值不受 CSP 管。
  $("#st-body").querySelectorAll("[data-h]").forEach((b) => {
    b.style.height = `${b.dataset.h}%`;
  });

  // 統計不該只能看 —— 點分類直接開一輪那個分類的複習
  $("#st-body").querySelectorAll("[data-cat]").forEach((b) =>
    b.addEventListener("click", () => startSession(deck, "all", b.dataset.cat))
  );
  show("stats");
}

function accuracyText(server, local) {
  if (server) return server.accuracy !== null ? Math.round(server.accuracy * 100) + "%" : "—";
  return local.accuracy !== null ? Math.round(local.accuracy * 100) + "%" : "—";
}

// 正確率與連續天數要從事件日誌算，不能從 states 算 —— states 只有「現在的樣子」，
// 沒有「答過幾次、對幾次、哪幾天答的」。events 兩種模式都寫，所以匿名也有得算。
async function localStats(deck) {
  const now = refNow();
  // 日界用 UTC，不是本地。原本 day0 取本地午夜、再用 toISOString() 印成標籤 ——
  // UTC+8 的本地午夜等於前一天 16:00Z，於是整排日期退一天：實測今天 7/28、
  // X 軸第一格卻標 27，卡片說「3 天後（7/31）」而長條落在標著 30 的格子上。
  // 對齊伺服器而不是改成本地：app/main.py 的 forecast_14d 與 day_key 都取 UTC 的
  // date()，上面算連續天數的地方也已經為了同一個理由用 UTC。兩邊必須是同一套，
  // 否則同一個人登入前後會看到兩張不一樣的圖。
  const utcDay = (d) => Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
  const day0 = utcDay(now);
  const buckets = new Array(14).fill(0);
  let answered = 0;
  const worst = new Map();

  for (const q of BANK[deck].questions) {
    const st = SNAP.states.get(q.id);
    if (!st || !st.review_count) continue;
    answered += st.review_count;
    if (st.lapse_count) worst.set(q.category, (worst.get(q.category) || 0) + st.lapse_count);
    if (!st.next_review_at) continue;
    const idx = Math.max(0, Math.round((utcDay(new Date(st.next_review_at)) - day0) / 86400e3));
    if (idx < 14) buckets[idx] += 1;
  }

  let total = 0;
  let ok = 0;
  const days = new Set();
  for (const ev of await store.eventsAll()) {
    const q = question(ev.card_id);
    if (!q || q.deck !== deck) continue;
    total += 1;
    if (ev.chosen === q.answer) ok += 1;
    // 用 UTC 日期，跟伺服器的 day_key 同一套（app/main.py 拿 UTC 的 date()）。
    // 這裡若改用當地時區，同一個人登入前後看到的連續天數會不一樣 ——
    // 一致比「對」更重要，而伺服器那份是權威。
    days.add(ev.reviewed_at.slice(0, 10));
  }
  let streak = 0;
  for (let d = new Date(); days.has(d.toISOString().slice(0, 10)); d.setUTCDate(d.getUTCDate() - 1)) {
    streak += 1;
  }

  return {
    answered: total || answered,
    accuracy: total ? ok / total : null,
    streak,
    forecast: buckets.map((count, i) => ({
      day: new Date(day0 + i * 86400e3).toISOString().slice(0, 10), count,
    })),
    worst: [...worst.entries()].sort((a, b) => b[1] - a[1]).slice(0, 10)
      .map(([category, lapses]) => ({ category, lapses })),
  };
}
$("#st-deck").addEventListener("change", renderStats);

// ── 設定 ──────────────────────────────────────────────
async function renderSettings() {
  const p = params();
  // 只有「登入 + 離線」才改不動設定：那時伺服器是權威，本地改了會跟它對不起來。
  // 匿名模式的設定本來就住在這台裝置上，離線照樣能改。
  const off = signedIn() && !navigator.onLine;
  $("#set-body").innerHTML = accountBlock() + `
    <div class="set-block">
      <h3>複習節奏</h3>
      <dl class="kv">
        <div><dt>首次</dt><dd>${fmtHours(p.initial_hours)}</dd></div>
        <div><dt>答對</dt><dd>×${p.remembered_mult}</dd></div>
        <div><dt>答錯</dt><dd>×${p.forgot_mult}</dd></div>
        <div><dt>範圍</dt><dd>${fmtHours(p.min_hours)}–${fmtHours(p.max_hours)}</dd></div>
      </dl>
      <select id="set-preset" class="input" ${off ? "disabled" : ""}>${CONFIG.presets.map((n) =>
        `<option value="${n}" ${n === SNAP.preset ? "selected" : ""}>${n === "relaxed" ? "relaxed（寬鬆，預設）" : "intensive（密集）"}</option>`).join("")}</select>
      ${off ? `<p class="muted set-hint">離線時無法變更 —— 登入後這是帳號設定，由伺服器裁決。</p>` : ""}
    </div>
    <div class="set-block">
      <h3>每輪題數</h3>
      <p>做完一輪會給結算，再決定要不要繼續 —— 不是每日上限，想做幾輪都可以。
      1090 題的佇列裡進度條一題只走 0.09%，那個數字只會讓人放棄。
      這是這台裝置的顯示偏好，不會同步到帳號。</p>
      <select id="set-round" class="input">${ROUND_CHOICES.map((n) =>
        `<option value="${n}" ${n === ROUND ? "selected" : ""}>${n ? `${n} 題` : "不分節（整副做完）"}</option>`).join("")}</select>
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
      <h3>備份這台裝置的紀錄</h3>
      <p>${signedIn()
        ? "已登入，進度也存在伺服器上。匯出仍可留一份自己的副本。"
        : "進度只存在這個瀏覽器裡 —— 清除瀏覽資料就會消失。"}</p>
      <div class="acct-row">
        <button class="btn btn-secondary" id="set-export">匯出 JSON</button>
        <button class="btn btn-secondary" id="set-import">匯入備份</button>
        <input type="file" id="set-import-file" accept="application/json" hidden>
      </div>
    </div>
    <div class="set-block">
      <h3>答錯不會歸零</h3>
      <p>答錯是把間隔乘上 ${p.forgot_mult}、不是打回新卡。排到 30 天的題答錯後回到 15 天，累積的記憶強度會保留。</p>
    </div>`;

  $("#set-pending").textContent = signedIn()
    ? `待補送的作答：${await store.outboxCount()} 筆`
    : `這台裝置的作答紀錄：${await store.eventsCount()} 筆（只存在本機）`;
  $("#set-cache").textContent = await cacheReport();
  // 設定在兩種模式都能改。匿名時就是本機的事；登入時伺服器是權威，
  // 改完直接採用它回傳的狀態，不要自己猜它會怎麼算（例如 paused_at）。
  $("#set-round").addEventListener("change", async (e) => {
    ROUND = Number(e.target.value);
    await store.kvPut("round_size", ROUND);
  });
  $("#set-preset").addEventListener("change", (e) => setSetting({ preset: e.target.value }));
  $("#set-pause").addEventListener("change", (e) => setSetting({ paused: e.target.checked }));
  bindAccountHandlers();
  $("#set-export").addEventListener("click", exportData);
  $("#set-import").addEventListener("click", () => $("#set-import-file").click());
  $("#set-import-file").addEventListener("change", (e) => {
    if (e.target.files[0]) importData(e.target.files[0]);
  });
  $("#set-precache").addEventListener("click", () => {
    navigator.serviceWorker && navigator.serviceWorker.controller &&
      navigator.serviceWorker.controller.postMessage("precache-images");
    flash($("#set-precache"), "下載中…");
  });
  show("settings");
}

// ── 帳號 ──────────────────────────────────────────────
// 帳號存在的唯一理由是跨裝置同步。不登入一樣能用完整的 app，所以這一段的
// 每個失敗路徑都必須留在原地不動，不能把人鎖在外面。

async function pushLocalHistory() {
  // 分批送，一批 500 筆。整批一次送會撞上伺服器的 5000 筆上限，而且
  // 一個請求做太多事本來就不是好主意。events 依 key 排序即時間序，
  // 照順序送 = 伺服器走增量快路徑，不必回頭重算。
  const evs = await store.eventsAll();
  const notes = Object.fromEntries(SNAP.notes);
  // 至少送一次，即使一題都沒答過。筆記可以在沒有任何作答的情況下存在
  // （題庫頁點開一題、直接寫筆記就是），而 enterAccount 之後 SNAP 會被伺服器的
  // 狀態整份取代 —— 迴圈被跳過就等於把那些筆記安靜地刪掉。
  const batches = Math.max(1, Math.ceil(evs.length / 500));
  for (let i = 0; i < batches; i += 1) {
    const answers = evs.slice(i * 500, (i + 1) * 500).map((e) => ({
      card_id: e.card_id, chosen: e.chosen, guessed: e.guessed,
      elapsed_ms: e.elapsed_ms, reviewed_at: e.reviewed_at,
      client_event_id: e.client_event_id,
    }));
    await api("/api/sync", {
      method: "POST",
      body: JSON.stringify({ answers, notes: i === 0 ? notes : {} }),
    });
  }
}

async function enterAccount(username, { merge, replaceLocal = false }) {
  // 先確定伺服器真的認這個 session，最後才切換模式。反過來的話，只要中間
  // 任何一步失敗（cookie 沒帶上、網路斷了），app 就會停在「自稱已登入但
  // 每個請求都 401」的狀態，而使用者看到的是一個壞掉的畫面而不是錯誤訊息。
  if (merge) await pushLocalHistory();
  const state = await api("/api/state");
  // 清本機一定要等到「伺服器狀態已經拿在手上」之後。先清再拿的話，中間任何一個
  // 失敗都會讓資料已經沒了、而使用者只看到「登入失敗」。
  // config 要放回去 —— clearAll 連它一起清掉，而 loadConfig 的離線退路靠它，
  // 少了它就會在下次離線開機時謊稱「這台裝置還沒下載過題庫」。
  if (replaceLocal) {
    await store.clearAll();
    await store.kvPut("config", CONFIG);
  }
  MODE = "account";
  ME = { username };
  await store.kvPut("mode", "account");
  await store.kvPut("username", username);  // 離線冷開機時 /api/me 問不到，靠這個顯示
  SNAP = adopt(state);
  await store.kvPut("notes_dirty", {});
  await persist();
}

async function leaveAccount() {
  // 登出要把本機清乾淨。共用裝置上留著別人的進度是不能接受的 ——
  // 而且下一個匿名使用者會看到一份不屬於他的快照，還以為是自己的。
  const config = CONFIG;
  await store.clearAll();
  await store.kvPut("config", config);
  MODE = "local";
  ME = null;
  SNAP = adopt(emptySnapshot());
  await persist();
}

// 未登入時的帳號區塊分三種模式。原本把登入與註冊擠在同一組欄位裡，代價有三個：
//   1. 密碼欄只能掛一種 autocomplete（原本是 current-password），於是註冊時
//      密碼管理器不會提議產生新密碼，只會塞一組已經在用的舊密碼 —— 對一個
//      「至少 10 字、且擋外洩清單」的 app 來說正好是反效果。
//   2. 兩顆按鈕並排時「登入」是裸 .btn（透明底 + 透明框）＝看起來不像按鈕，
//      次要的「註冊新帳號」反而有框，主要動作的視覺權重是反的。
//   3. 沒有地方放「忘記密碼」。/api/auth/recover 早就實作好了，但前端沒有任何
//      入口 —— 註冊時告訴使用者「救援碼是唯一能拿回帳號的東西」，然後不給那條路。
//      忘記密碼在這個 app 等於帳號永久失聯（沒有 email 可以寄）。
let acctMode = "login";

const ACCT_MODES = [["login", "登入"], ["register", "註冊"], ["recover", "用救援碼重設"]];
const ACCT_SUBMIT = { login: "登入", register: "建立帳號", recover: "重設密碼" };
const ACCT_INTRO = {
  login: "在另一台裝置登入同一個帳號，兩邊的進度就會合起來算。",
  register: "現在的進度只存在這台裝置上。要在手機與電腦之間同步，才需要帳號。",
  recover: "沒有 email 可以寄，救援碼是忘記密碼時唯一的後路。重設後所有裝置都會被登出，並拿到一組新的救援碼。",
};

function accountBlock() {
  if (signedIn()) {
    return `<div class="set-block">
      <h3>帳號</h3>
      <p>以 <b>${esc(ME ? ME.username : "")}</b> 登入中，進度會同步到這個帳號。</p>
      <div class="acct-row">
        <button class="btn btn-secondary" id="acct-logout">登出這台裝置</button>
        <button class="btn btn-secondary" id="acct-chpw">更改密碼</button>
        <button class="btn btn-danger" id="acct-delete">刪除帳號</button>
      </div>
      <p id="acct-msg" class="muted"></p>
    </div>`;
  }
  const isRecover = acctMode === "recover";
  const fresh = isRecover || acctMode === "register";
  return `<div class="set-block">
    <h3>帳號<span class="tag">選用</span></h3>
    <div class="acct-tabs">${ACCT_MODES.map(([k, label]) =>
      `<button class="btn btn-sm acct-tab${k === acctMode ? " is-on" : ""}" data-acct-mode="${k}">${label}</button>`
    ).join("")}</div>
    <p>${ACCT_INTRO[acctMode]}</p>
    <div class="acct-form">
      <input class="input" id="acct-user" placeholder="帳號（3-32 字，英數字、_、-）"
             autocomplete="username" autocapitalize="off" spellcheck="false">
      ${isRecover
        ? `<input class="input" id="acct-code" placeholder="救援碼（dl_ 開頭）"
                  autocomplete="off" autocapitalize="off" spellcheck="false">` : ""}
      <input class="input" id="acct-pass" type="password"
             placeholder="${fresh ? "新密碼（至少 10 字）" : "密碼"}"
             autocomplete="${fresh ? "new-password" : "current-password"}">
      ${acctMode === "register" && CONFIG.turnstile_sitekey
        ? `<div class="cf-turnstile" data-sitekey="${esc(CONFIG.turnstile_sitekey)}"></div>` : ""}
      <div class="acct-row">
        <button class="btn btn-primary" id="acct-submit">${ACCT_SUBMIT[acctMode]}</button>
      </div>
    </div>
    <p id="acct-msg" class="muted"></p>
  </div>`;
}

function acctMsg(text, bad = false) {
  const el = $("#acct-msg");
  if (!el) return;
  el.textContent = text;
  el.className = bad ? "danger" : "muted";
}

// 沒有 email 就沒有寄信重設密碼。救援碼是唯一的後路，所以顯示它的時候
// 要擋在流程中間，不能只是印一行讓人滑過去。
// note 走同一個框而不是另外呼叫 acctMsg()：兩者寫的是同一個 #acct-msg，
// 後呼叫的那個會把救援碼整個蓋掉，而那是使用者唯一一次看到它的機會。
function showRecoveryCode(code, note = "") {
  const box = $("#acct-msg");
  box.className = "recovery";
  box.innerHTML =
    `<b>救援碼</b><code>${esc(code)}</code>
     <span>忘記密碼時，這是唯一能拿回帳號的東西 —— 沒有 email 可以寄。
     抄到手機以外的地方。它只會出現這一次。</span>` +
    (note ? `<span>${esc(note)}</span>` : "");
}

async function doRegister() {
  const username = $("#acct-user").value.trim();
  const password = $("#acct-pass").value;
  const token = window.turnstile && $(".cf-turnstile")
    ? window.turnstile.getResponse() : undefined;
  try {
    const r = await api("/api/auth/register", {
      method: "POST",
      body: JSON.stringify({ username, password, turnstile_token: token }),
    });
    // 剛註冊的帳號伺服器上必然是空的，所以一律合併 —— 這裡問「要不要保留
    // 本機進度」只會製造一個「選錯就全沒了」的選項。
    await enterAccount(r.username, { merge: true });
    await renderSettings();
    showRecoveryCode(r.recovery_code);  // 必須在重繪之後，否則會被蓋掉
  } catch (e) {
    acctMsg(await errText(e, "註冊失敗"), true);
  }
}

// 這台裝置上已經有紀錄時，要問清楚是併入還是丟掉。登入與救援碼重設都會走到
// 同一個岔路（兩者都是「這台裝置接上一個既有帳號」），所以只寫一次。
async function askMerge() {
  const local = await store.eventsCount();
  if (!local) return { merge: true, local: 0 };
  // 破壞性的那一邊必須是「確定」，不能是「取消」。瀏覽器抑制對話框時
  // confirm() 會直接回 false 而且根本不顯示（Chrome 的「防止此頁面產生額外對話方塊」
  // 在同一次設定頁操作中就按得到）—— 那時預設落在哪一邊，就決定了使用者的
  // 全部本機紀錄會不會在沒被問過的情況下被清掉。
  const replace = confirm(
    `這台裝置上有 ${local} 筆作答紀錄。\n\n` +
    `［確定］改用帳號上的紀錄 —— 這台裝置上的 ${local} 筆會被丟掉。\n` +
    `［取消］把它們併入帳號 —— 兩邊的紀錄會合起來算。\n\n` +
    `這個選擇無法復原。不確定就選取消。`
  );
  return { merge: !replace, local };
}

async function doLogin() {
  const username = $("#acct-user").value.trim();
  const password = $("#acct-pass").value;
  const { merge, local } = await askMerge();
  try {
    const r = await api("/api/auth/login", {
      method: "POST", body: JSON.stringify({ username, password }),
    });
    await enterAccount(r.username, { merge, replaceLocal: !merge });
    await renderSettings();
    acctMsg(merge && local ? `已登入，${local} 筆本機紀錄已併入帳號。` : "已登入。");
  } catch (e) {
    acctMsg(await errText(e, "登入失敗"), true);
  }
}

// 忘記密碼的唯一出路。伺服器會換掉密碼與救援碼、撤掉全部 session，並直接發一個
// 新 session 給這台裝置 —— 所以這裡跟登入一樣要處理本機紀錄的併入。
async function doRecover() {
  const username = $("#acct-user").value.trim();
  const recovery_code = $("#acct-code").value.trim();
  const new_password = $("#acct-pass").value;
  const { merge, local } = await askMerge();
  try {
    const r = await api("/api/auth/recover", {
      method: "POST", body: JSON.stringify({ username, recovery_code, new_password }),
    });
    await enterAccount(r.username, { merge, replaceLocal: !merge });
    await renderSettings();
    // 舊救援碼用掉就作廢了，新的一樣只出現這一次 —— 必須擋在流程中間，
    // 不能只印一行讓人滑過去（比照註冊）。
    showRecoveryCode(
      r.recovery_code,
      merge && local ? `密碼已重設，${local} 筆本機紀錄已併入帳號。` : "密碼已重設。"
    );
  } catch (e) {
    acctMsg(await errText(e, "重設失敗", "帳號或救援碼不正確。"), true);
  }
}

function submitAccount() {
  if (acctMode === "login") return doLogin();
  if (acctMode === "register") return doRegister();
  return doRecover();
}

async function errText(e, fallback, unauthorized = "帳號或密碼不正確。") {
  const m = String(e.message || "");
  if (m.startsWith("429")) return "嘗試太頻繁，稍後再試。";
  if (m.startsWith("401")) return unauthorized;
  if (m.startsWith("409")) return "這個帳號已經有人用了。";
  if (m.startsWith("400")) return "帳號或密碼不符合規則（密碼至少 10 字，且不能是常見密碼）。";
  return fallback;
}

// ── 匯出 / 匯入 ───────────────────────────────────────
// 匿名使用者的進度只活在這個瀏覽器裡。清一次瀏覽資料就沒了，
// 而那是使用者自己隨時可能做的事 —— 這是唯一的保險。

async function exportData() {
  const payload = {
    version: 1,
    exported_at: new Date().toISOString(),
    events: await store.eventsAll(),
    notes: Object.fromEntries(SNAP.notes),
    settings: { preset: SNAP.preset, paused: SNAP.paused, paused_at: SNAP.paused_at },
  };
  const url = URL.createObjectURL(
    new Blob([JSON.stringify(payload)], { type: "application/json" })
  );
  const a = document.createElement("a");
  a.href = url;
  a.download = `driver-license-${new Date().toISOString().slice(0, 10)}.json`;
  // 要掛進 document 才點得動（iOS standalone 對 detached 的 <a download> 不友善），
  // 而 revoke 不能緊接著做 —— 那會跟下載開始賽跑，Safari／Firefox 上會拿到空檔。
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 60_000);
}

async function importData(file) {
  let data;
  try {
    data = JSON.parse(await file.text());
    if (data.version !== 1 || !Array.isArray(data.events)) throw new Error("shape");
    // 每一筆都要有 keyPath 與最小欄位。缺 client_event_id 的話 IndexedDB 會丟
    // DataError，而那個 rejection 沒有人接 —— 使用者會得到一份半套的匯入，
    // 而且畫面上什麼都不會說。
    for (const ev of data.events) {
      if (!ev || typeof ev.client_event_id !== "string" ||
          typeof ev.card_id !== "string" || typeof ev.chosen !== "number" ||
          typeof ev.reviewed_at !== "string") throw new Error("event");
    }
  } catch {
    return acctMsg("這個檔案不是本 app 匯出的備份，或內容已損壞。", true);
  }
  if (!confirm(
    `要匯入 ${data.events.length} 筆作答紀錄嗎？\n` +
    `它們會與這台裝置現有的紀錄合併（同一筆不會重複計算）。`
  )) return;

  // 逐筆重放，順序照 reviewed_at —— 與伺服器的重算是同一套規則
  for (const ev of data.events) await store.eventsPut(ev);
  const all = (await store.eventsAll())
    .sort((a, b) => new Date(a.reviewed_at) - new Date(b.reviewed_at));
  SNAP.states = new Map();
  for (const ev of all) {
    const q = question(ev.card_id);
    if (!q) continue;
    const remembered = ev.chosen === q.answer && !ev.guessed;
    SNAP.states.set(ev.card_id, {
      id: ev.card_id, deck: q.deck,
      ...srs.applyFeedback(SNAP.states.get(ev.card_id), remembered,
                           srs.params(ev.preset || SNAP.preset),
                           new Date(ev.reviewed_at)),
    });
  }
  for (const [k, v] of Object.entries(data.notes || {})) SNAP.notes.set(k, v);
  if (data.settings && data.settings.preset) SNAP.preset = data.settings.preset;
  await persist();
  if (signedIn()) {
    for (const ev of data.events) await store.outboxPut(ev);
    await syncSoon();
  }
  await renderSettings();
  acctMsg(`已匯入 ${data.events.length} 筆紀錄。`);
}

function bindAccountHandlers() {
  if (signedIn()) {
    $("#acct-logout").addEventListener("click", async () => {
      if (!confirm(
        "登出會把這台裝置上的本機資料一併清掉（伺服器上的進度不受影響）。\n" +
        "共用裝置上不能把進度留給下一個人。要繼續嗎？"
      )) return;
      await api("/api/auth/logout", { method: "POST" }).catch(() => {});
      await leaveAccount();
      await renderSettings();
      acctMsg("已登出，這台裝置回到未登入狀態。");
    });
    $("#acct-chpw").addEventListener("click", async () => {
      const current = prompt("目前的密碼：");
      if (!current) return;
      const next = prompt("新密碼（至少 10 字）：");
      if (!next) return;
      try {
        const r = await api("/api/auth/password", {
          method: "POST",
          body: JSON.stringify({ current_password: current, new_password: next }),
        });
        await renderSettings();
        showRecoveryCode(r.recovery_code);
      } catch (e) {
        acctMsg(await errText(e, "改密碼失敗"), true);
      }
    });
    $("#acct-delete").addEventListener("click", async () => {
      const pw = prompt("刪除帳號會連同伺服器上的所有進度一起消失，無法復原。\n輸入密碼確認：");
      if (!pw) return;
      try {
        await api("/api/account", { method: "DELETE", body: JSON.stringify({ password: pw }) });
        await leaveAccount();
        await renderSettings();
        acctMsg("帳號已刪除。");
      } catch (e) {
        acctMsg(await errText(e, "刪除失敗"), true);
      }
    });
    return;
  }
  $("#set-body").querySelectorAll("[data-acct-mode]").forEach((b) =>
    b.addEventListener("click", () => {
      acctMode = b.dataset.acctMode;
      renderSettings();
    })
  );
  $("#acct-submit").addEventListener("click", submitAccount);
  // 打完密碼按 Enter 原本什麼都不會發生（沒有 <form>，所以沒有隱含送出）。
  $("#set-body").querySelectorAll(".acct-form input").forEach((i) =>
    i.addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); submitAccount(); }
    })
  );
  mountTurnstile();
}

// Turnstile 的 widget 必須在每次重繪後重新掛上。api.js 只會自動 render
// 「腳本載入當下就存在」的 .cf-turnstile —— 而 renderSettings() 每次都會把
// 那個 div 換成新的（改 preset、按暫停、切換帳號分頁都會觸發）。
// 原本只在第一次載入腳本、之後不再 render，於是重繪過就拿不到 token，
// 註冊一律回 400「人機驗證未通過」，而且看不出為什麼。
function mountTurnstile() {
  const el = document.querySelector(".cf-turnstile");
  if (!el || !CONFIG.turnstile_sitekey) return;
  if (window.turnstile) {
    window.turnstile.render(el, { sitekey: CONFIG.turnstile_sitekey });
    return;
  }
  if (document.getElementById("turnstile-js")) return;  // 載入中，onload 會自己 render
  const sc = document.createElement("script");
  sc.id = "turnstile-js";
  sc.src = "https://challenges.cloudflare.com/turnstile/v0/api.js";
  sc.async = true;
  document.head.appendChild(sc);
}

async function setSetting(patch) {
  if (signedIn()) {
    try {
      await api("/api/settings", { method: "PUT", body: JSON.stringify(patch) });
      SNAP = adopt({ ...await api("/api/state") });
    } catch (e) {
      if (String(e.message).startsWith("401")) await goLocal();
      // 不能用 flash()：它會在 1.1 秒後還原 innerHTML，把 renderSettings 綁的
      // 每一個 listener（preset、暫停、五個帳號按鈕、匯出匯入）全部炸掉，
      // 整頁變成死的而且沒有任何跡象。
      else return acctMsg("設定改不動，稍後再試。", true);
    }
  }
  if (!signedIn()) {
    if (patch.preset !== undefined) SNAP.preset = patch.preset;
    if (patch.paused !== undefined) {
      SNAP.paused = patch.paused;
      // 進入暫停時把當下時間凍住，之後所有到期計算都用它 —— 與 app/main.py 同一套
      if (patch.paused) SNAP.paused_at = new Date().toISOString();
    }
  }
  await persist();
  renderSettings();
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
  $(sel).innerHTML = CONFIG.decks.map((d) => `<option value="${d.deck}">${esc(d.label)}</option>`).join("");
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
  // 兩天以上不印時刻。「7/31 上午12:08」的那個時刻是乘出來的，沒有人會為它
  // 定鬧鐘，印出來只會讓人以為那是個約定；使用者要的是「還有幾天」。
  return `${days} 天後（${date}）`;
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
