"use strict";

const $ = (s) => document.querySelector(s);
const api = async (path, opts) => {
  const r = await fetch(path, {
    headers: { "content-type": "application/json" },
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

let decks = [];                                   // /api/decks 快取
let S = null;                                     // 目前 session 狀態

function show(view) {
  VIEWS.forEach((v) => ($(`#view-${v}`).hidden = v !== view));
  $("#title").textContent = TITLES[view];
  $("#btn-back").hidden = view !== "session";
  window.scrollTo(0, 0);
}

// ── 首頁 ──────────────────────────────────────────────
async function renderHome() {
  const data = await api("/api/decks");
  decks = data.decks;
  $("#view-home").innerHTML = decks.map(deckCard).join("") +
    (data.paused ? `<p class="muted">⏸ 時鐘已凍結 — 到期時間不會推進。</p>` : "");

  $("#view-home").querySelectorAll("[data-start]").forEach((b) =>
    b.addEventListener("click", () =>
      startSession(b.dataset.deck, b.dataset.start, $(`#cat-${b.dataset.deck}`).value)
    )
  );
  show("home");
}

function deckCard(d) {
  const cats = d.categories
    .map((c) => `<option value="${esc(c.category)}">${esc(c.category)}（${c.count}）</option>`)
    .join("");
  return `
  <div class="deck">
    <h2>${esc(d.label)}</h2>
    <p class="sub">${esc(d.title)} · ${esc(d.version)} · 共 ${d.total} 題</p>
    <div class="counts">
      <div class="count"><b>${d.unlearned}</b><span>未學習</span></div>
      <div class="count"><b>${d.due}</b><span>待複習</span></div>
      <div class="count"><b>${d.reviewed}</b><span>已複習</span></div>
    </div>
    <div class="actions">
      <button class="primary" data-start="due"  data-deck="${d.deck}" ${d.due ? "" : "disabled"}>
        複習到期（${d.due}）</button>
      <button class="alt" data-start="unlearned" data-deck="${d.deck}" ${d.unlearned ? "" : "disabled"}>
        學新題（${d.unlearned}）</button>
      <button class="alt" data-start="all" data-deck="${d.deck}">全部（${d.due + d.unlearned}）</button>
    </div>
    <div class="catrow">
      <select id="cat-${d.deck}"><option value="">全部分類</option>${cats}</select>
    </div>
  </div>`;
}

// ── 作答 ──────────────────────────────────────────────
async function startSession(deck, kind, category) {
  const p = new URLSearchParams({ deck, shuffle: kind === "unlearned" ? "false" : "true" });
  if (category) p.set("category", category);
  const q = await api(`/api/queue/${kind}?${p}`);
  if (!q.count) return renderHome();
  S = { ids: q.card_ids, i: 0, deck, answered: false, cardId: null, t0: 0 };
  show("session");
  await loadCard();
}

async function loadCard() {
  const id = S.ids[S.i];
  S.cardId = id;
  S.answered = false;
  const data = await api(`/api/card/${id}`);
  const q = data.question;

  $("#progress-bar").style.width = `${(S.i / S.ids.length) * 100}%`;
  $("#progress-text").textContent = `${S.i + 1} / ${S.ids.length}　·　${q.category}`;
  $("#q-meta").textContent = `${q.id}　${q.architecture}`;
  $("#q-stem").textContent = q.stem;

  // 題幹是圖的題目（汽車 95 題、機車 126 題）沒有文字題幹，圖就是題目本身
  const stemImgs = q.options.some((o) => o.is_image) ? [] : q.images;
  $("#q-images").innerHTML = stemImgs
    .map((im) => `<img src="/${im.file}" alt="題目圖">`)
    .join("");

  $("#options").innerHTML = q.options
    .map((o, idx) => {
      // 選項本身是圖的 6 題（汽車 183/185/233/523/723/1004）走這個分支
      const body = o.is_image && q.images[idx]
        ? `<img src="/${q.images[idx].file}" alt="選項 ${o.n}">`
        : `<span>${esc(o.text)}</span>`;
      return `<button class="opt" data-n="${o.n}"><span class="n">${o.n}</span>${body}</button>`;
    })
    .join("");
  $("#options").querySelectorAll(".opt").forEach((b) =>
    b.addEventListener("click", () => submit(Number(b.dataset.n)))
  );

  $("#verdict").hidden = true;
  $("#btn-next").hidden = true;
  $("#btn-guessed").hidden = false;
  $("#note").value = data.note || "";
  $("#note-status").textContent = "";
  $("#note-box").open = false;
  S.t0 = performance.now();
}

async function submit(chosen, guessed = false) {
  if (S.answered) return;
  S.answered = true;
  const r = await api("/api/answer", {
    method: "POST",
    body: JSON.stringify({
      card_id: S.cardId, chosen,
      elapsed_ms: Math.round(performance.now() - S.t0), guessed,
    }),
  });
  S.chosen = chosen;
  paintVerdict(r, chosen);
}

function paintVerdict(r, chosen) {
  $("#options").querySelectorAll(".opt").forEach((b) => {
    const n = Number(b.dataset.n);
    b.disabled = true;
    if (n === r.answer) b.classList.add("correct");
    else if (n === chosen) b.classList.add("wrong");
  });
  const v = $("#verdict-text");
  v.textContent = r.correct ? "答對" : "答錯";
  v.className = r.correct ? "ok" : "bad";
  $("#verdict-sched").textContent =
    `下次複習：${fmtDue(r.next_review_at)}（間隔 ${fmtHours(r.interval_hours)}）` +
    (r.correct ? "" : `　·　累計答錯 ${r.lapse_count} 次`);
  $("#btn-guessed").hidden = !r.correct || !r.remembered;
  $("#verdict").hidden = false;
  $("#btn-next").hidden = false;
}

// 改判上一次作答，不是再送一次（再送會從已放大的間隔再乘 0.5，數字會錯）
$("#btn-guessed").addEventListener("click", async () => {
  const r = await api("/api/answer/regrade", {
    method: "POST",
    body: JSON.stringify({ card_id: S.cardId }),
  });
  paintVerdict(r, S.chosen);
  $("#btn-guessed").hidden = true;
});

$("#btn-next").addEventListener("click", next);
function next() {
  S.i += 1;
  if (S.i >= S.ids.length) { show("done"); S = null; return; }
  loadCard();
}

// 筆記：離開輸入框才存，不要每個字打一次 API
$("#note").addEventListener("blur", async () => {
  if (!S) return;
  await api(`/api/note/${S.cardId}`, {
    method: "PUT",
    body: JSON.stringify({ body: $("#note").value }),
  });
  $("#note-status").textContent = "已儲存";
});

// 桌機鍵盤流（比照 kg 的 Mac Catalyst）
document.addEventListener("keydown", (e) => {
  if ($("#view-session").hidden) return;
  if (e.target.tagName === "TEXTAREA" || e.target.tagName === "INPUT") return;
  if (["1", "2", "3"].includes(e.key)) { submit(Number(e.key)); e.preventDefault(); }
  else if (e.key === " " || e.key === "Enter") {
    if (S && S.answered) { next(); e.preventDefault(); }
  } else if (e.key.toLowerCase() === "g") { $("#btn-guessed").click(); }
  else if (e.key.toLowerCase() === "n") { $("#note-box").open = true; $("#note").focus(); }
});

// ── 題庫瀏覽 ──────────────────────────────────────────
async function renderBrowse() {
  fillDeckSelect("#br-deck");
  const deck = $("#br-deck").value;
  const d = decks.find((x) => x.deck === deck);
  $("#br-cat").innerHTML = `<option value="">全部分類</option>` +
    d.categories.map((c) => `<option value="${esc(c.category)}">${esc(c.category)}</option>`).join("");
  await refreshBrowse();
  show("browse");
}

async function refreshBrowse() {
  const p = new URLSearchParams({ deck: $("#br-deck").value });
  if ($("#br-q").value.trim()) p.set("q", $("#br-q").value.trim());
  if ($("#br-cat").value) p.set("category", $("#br-cat").value);
  if ($("#br-wrong").checked) p.set("wrong_only", "true");
  if ($("#br-noted").checked) p.set("noted_only", "true");
  const r = await api(`/api/browse?${p}`);
  $("#br-count").textContent = `${r.count} 題`;
  $("#br-list").innerHTML = r.items.slice(0, 400).map((i) => `
    <button class="row" data-id="${i.id}">
      <span class="dot ${i.lapsed ? "lapsed" : i.state}"></span>
      <span class="num">${i.number}</span>
      <span class="txt">${esc(i.stem_is_image ? "（圖片題）" : i.stem)}</span>
    </button>`).join("");
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
  const s = await api(`/api/stats?deck=${$("#st-deck").value}`);
  const max = Math.max(1, ...s.forecast_14d.map((f) => f.count));
  $("#st-body").innerHTML = `
    <div class="tiles">
      <div class="tile"><b>${s.answered_total}</b><span>累計作答</span></div>
      <div class="tile"><b>${s.accuracy === null ? "—" : Math.round(s.accuracy * 100) + "%"}</b><span>正確率</span></div>
      <div class="tile"><b>${s.streak_days}</b><span>連續天數</span></div>
    </div>
    <p class="sec">未來 14 天到期量</p>
    <div class="bars">${s.forecast_14d.map((f) =>
      `<div style="height:${(f.count / max) * 100}%" title="${f.day}：${f.count}"></div>`).join("")}</div>
    <div class="barlabels">${s.forecast_14d.map((f, i) =>
      `<span>${i % 2 ? "" : f.day.slice(8)}</span>`).join("")}</div>
    <p class="sec">最常答錯的分類</p>
    ${s.worst_categories.length
      ? s.worst_categories.map((c) =>
          `<div class="row"><span class="txt">${esc(c.category)}</span><span class="num">${c.lapses} 次</span></div>`).join("")
      : `<p class="muted">還沒有答錯紀錄。</p>`}`;
  show("stats");
}
$("#st-deck").addEventListener("change", renderStats);

// ── 設定 ──────────────────────────────────────────────
async function renderSettings() {
  const s = await api("/api/settings");
  const p = s.params;
  $("#set-body").innerHTML = `
    <div class="field">
      <h3>複習節奏</h3>
      <p class="muted">目前：首次 ${fmtHours(p.initial_hours)}　答對 ×${p.remembered_mult}　答錯 ×${p.forgot_mult}　範圍 ${fmtHours(p.min_hours)}–${fmtHours(p.max_hours)}</p>
      <select id="set-preset">${s.presets.map((n) =>
        `<option value="${n}" ${n === s.preset ? "selected" : ""}>${n === "relaxed" ? "relaxed（寬鬆，預設）" : "intensive（密集）"}</option>`).join("")}</select>
    </div>
    <div class="field">
      <h3>凍結時鐘</h3>
      <p class="muted">暫停期間到期時間不再推進，回來時不會有一座待複習山。</p>
      <label class="switch"><span>暫停累積${s.paused_at && s.paused ? `（自 ${s.paused_at.slice(0, 10)}）` : ""}</span>
        <input type="checkbox" id="set-pause" ${s.paused ? "checked" : ""}></label>
    </div>
    <div class="field">
      <h3>答錯不會歸零</h3>
      <p class="muted">答錯是把間隔乘上 ${p.forgot_mult}、不是打回新卡。排到 30 天的題答錯後回到 15 天，累積的記憶強度會保留。</p>
    </div>`;
  $("#set-preset").addEventListener("change", async (e) => {
    await api("/api/settings", { method: "PUT", body: JSON.stringify({ preset: e.target.value }) });
    renderSettings();
  });
  $("#set-pause").addEventListener("change", async (e) => {
    await api("/api/settings", { method: "PUT", body: JSON.stringify({ paused: e.target.checked }) });
    renderSettings();
  });
  show("settings");
}

// ── 雜項 ──────────────────────────────────────────────
function fillDeckSelect(sel) {
  if ($(sel).options.length) return;
  $(sel).innerHTML = decks.map((d) => `<option value="${d.deck}">${esc(d.label)}</option>`).join("");
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
  // 用「日曆日」差，不是經過時數。12 小時後可能已是明天凌晨，算成 0 天會印出「今天 7/28」這種自相矛盾的字串。
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

renderHome();
