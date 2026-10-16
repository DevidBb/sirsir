// HUD Джарвиса: WebSocket-клиент, чат, ход задачи, результаты, память, настройки, диагностика.
(function () {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const md = window.JarvisMarkdown;
  const STATE_TEXT = { idle: "Готов", thinking: "Думаю", acting: "Действую", listening: "Слушаю",
    speaking: "Говорю", error: "Ошибка" };
  const TOOL_TEXT = {
    youtube_search: "Поиск на YouTube", internet_search: "Поиск в интернете", open_result: "Открываю результат",
    read_page: "Читаю страницу", browser_open: "Открываю адрес", browser_click: "Нажимаю элемент",
    browser_type: "Ввожу текст", browser_press: "Нажимаю клавишу", browser_scroll: "Прокручиваю",
    browser_back: "Назад", browser_tabs: "Вкладки", browser_switch_tab: "Переключаю вкладку",
    browser_close_tab: "Закрываю вкладку", browser_screenshot: "Снимок страницы",
    open_website: "Открываю сайт", look_at_screen: "Смотрю на экран", screen_screenshot: "Снимок экрана", open_app: "Запускаю программу", web_search: "Поиск на сайте",
  };

  let ws = null, reconnectDelay = 500, uiState = "idle";
  const bubbles = new Map();   // task -> {el, text}
  const toolRows = [];         // незавершённые шаги
  let speaking = false;
  // Локальный голос на стороне сервера (python -m jarvis --web --voice): Vosk/Whisper/Silero.
  let serverVoice = { enabled: false, muted: false, mic: null };

  // ---------- соединение ----------
  function connect() {
    ws = new WebSocket(`ws://${location.host}/ws`);
    ws.onopen = () => { setConn(true); reconnectDelay = 500; refreshStatus(); };
    ws.onclose = () => { setConn(false); setTimeout(connect, reconnectDelay); reconnectDelay = Math.min(reconnectDelay * 2, 8000); };
    ws.onmessage = (e) => { try { handle(JSON.parse(e.data)); } catch (err) { console.error(err); } };
  }
  function send(obj) {
    if (ws && ws.readyState === 1) { ws.send(JSON.stringify(obj)); return true; }
    addSystem("Нет связи с сервером Джарвиса. Он запущен?", true);
    return false;
  }
  function setConn(ok) {
    const b = $("connBadge");
    b.textContent = ok ? "связь есть" : "нет связи";
    b.className = "badge " + (ok ? "ok" : "bad");
  }

  // ---------- события ----------
  function handle(ev) {
    switch (ev.type) {
      case "hello":
        setState(ev.state || "idle");
        setProvider(ev.provider);
        applyVoice(ev.voice);
        (ev.history || []).slice(-60).forEach(h => { if (h.type !== "state") timelineEvent(h, true); });
        break;
      case "state": setState(ev.state, ev.detail); break;
      case "voice": applyVoice(ev); break;
      case "task":
        addUser(ev.text, ev.source === "voice");
        timelineAdd("task", ev.text, "", "task");
        hideConfirm();
        break;
      case "delta": appendDelta(ev.task, ev.text); break;
      case "delta_end": break;
      case "tool_start": case "tool_end": timelineEvent(ev); if (ev.type === "tool_end" && ev.data) showData(ev.data); break;
      case "confirm": showConfirm(ev.text); timelineAdd("warn", "Нужно подтверждение", ev.action || ""); break;
      case "confirm_resolved": hideConfirm(); break;
      case "reply": finishReply(ev); if (ev.scheduled && document.querySelector('.tab.active')?.dataset.tab === "schedule") loadSchedules(); break;
      case "cancelled": hideConfirm(); timelineAdd("fail", "Задача остановлена", ""); stopSpeech(); break;
      case "notice": addSystem(ev.text); if (ev.reminder) speak(ev.text); break;
      case "skill": timelineAdd("task", "Навык: " + ev.name, ev.description || "", "", ev.ts); break;
      case "delegate": timelineAdd("task", "Поручение агенту " + (AGENT_TEXT[ev.agent] || ev.agent), ev.text || "", "", ev.ts); break;
    }
  }

  function setState(state, detail) {
    uiState = state;
    $("statePill").dataset.state = state;
    $("core").dataset.state = state;
    $("stateText").textContent = STATE_TEXT[state] || state;
    $("stateDetail").textContent = state === "acting" ? (TOOL_TEXT[detail] || detail || "") : (state === "error" ? (detail || "") : "");
    $("cancelBtn").disabled = state === "idle";
  }

  function setProvider(p) {
    const b = $("providerBadge");
    if (p === "claude") { b.textContent = "Claude · облако"; b.className = "badge cloud"; }
    else if (p === "hybrid") { b.textContent = "Гибрид · локально + Claude"; b.className = "badge cloud"; }
    else { b.textContent = "Ollama · локально"; b.className = "badge local"; }
  }

  // ---------- чат ----------
  function scrollChat() { const c = $("chat"); c.scrollTop = c.scrollHeight; }
  function addUser(text, byVoice) {
    const el = document.createElement("div");
    el.className = "msg user";
    el.innerHTML = `<div class="who">${byVoice ? "Вы · голос" : "Вы"}</div><div class="body"></div>`;
    el.querySelector(".body").textContent = text;
    $("chat").appendChild(el); scrollChat();
  }
  function addSystem(text, error) {
    const el = document.createElement("div");
    el.className = "msg system" + (error ? " error" : "");
    el.textContent = text;
    $("chat").appendChild(el); scrollChat();
  }
  function bubble(task) {
    let b = bubbles.get(task);
    if (!b) {
      const el = document.createElement("div");
      el.className = "msg assistant streaming";
      el.innerHTML = `<div class="who">Джарвис</div><div class="body"></div>`;
      $("chat").appendChild(el);
      b = { el, text: "" };
      bubbles.set(task, b);
    }
    return b;
  }
  function appendDelta(task, text) {
    const b = bubble(task);
    b.text += text;
    b.el.querySelector(".body").innerHTML = md.render(b.text);
    scrollChat();
  }
  function finishReply(ev) {
    if (ev.ignored) { const b = bubbles.get(ev.task); if (b && !b.text) b.el.remove(); return; }
    const b = bubble(ev.task);
    b.el.classList.remove("streaming");
    const text = ev.text || b.text || "";
    const listed = ev.data && (ev.data.kind === "youtube" || ev.data.kind === "web") && !b.text;
    // Список уже показан карточками — в тексте оставляем только первую строку.
    b.el.querySelector(".body").innerHTML = md.render(listed ? text.split("\n")[0] : text);
    if (ev.ok === false) b.el.classList.add("error");
    if (ev.data) b.el.appendChild(renderData(ev.data));
    bubbles.delete(ev.task);
    if (ev.pending_confirmation) showConfirm(text);
    toolRows.splice(0).forEach(r => { if (r.classList.contains("run")) r.className = "fail"; });
    scrollChat();
    speak(text);
  }

  // ---------- результаты ----------
  function link(url, text) {
    const a = document.createElement("a");
    if (/^https?:\/\//i.test(url)) { a.href = url; a.target = "_blank"; a.rel = "noopener noreferrer"; }
    a.textContent = text || url;
    return a;
  }
  function renderData(data) {
    const wrap = document.createElement("div");
    if (data.kind === "youtube" || data.kind === "web") {
      wrap.className = "cards";
      (data.items || []).forEach((it, i) => {
        const c = document.createElement("div");
        c.className = "card";
        const num = document.createElement("div"); num.className = "num"; num.textContent = String(i + 1).padStart(2, "0");
        const title = document.createElement("div"); title.className = "title"; title.appendChild(link(it.url, it.title));
        const meta = document.createElement("div"); meta.className = "meta";
        meta.textContent = data.kind === "youtube"
          ? [it.channel, it.duration, it.views, it.published].filter(Boolean).join(" · ")
          : (it.source || "");
        const snip = document.createElement("div"); snip.className = "snippet";
        snip.textContent = data.kind === "youtube" ? (it.description || "") : (it.snippet || "");
        const acts = document.createElement("div"); acts.className = "acts";
        const open = document.createElement("button"); open.className = "link-btn"; open.type = "button";
        open.textContent = data.kind === "youtube" ? "▶ открыть у Джарвиса" : "→ открыть у Джарвиса";
        open.onclick = () => send({ type: "tool", name: "open_result", args: { index: i + 1 } });
        acts.appendChild(open);
        c.append(num, title, meta, snip, acts);
        wrap.appendChild(c);
      });
    } else if (data.kind === "screenshot" || data.kind === "screen") {
      const img = document.createElement("img"); img.className = "shot";
      img.alt = data.kind === "screen" ? "Снимок экрана" : "Снимок страницы";
      img.src = "/api/screenshots/" + encodeURIComponent(data.file);
      wrap.appendChild(img);
    } else if (data.kind === "page" || data.kind === "opened") {
      wrap.className = "meta small";
      wrap.appendChild(link(data.url, data.title || data.url));
    } else if ((data.kind === "diff" || data.kind === "file_changed") && data.diff) {
      // Изменение файла: до подтверждения — открыто, после записи — свёрнуто.
      const det = document.createElement("details"); det.className = "diff-box"; det.open = data.kind === "diff";
      const sum = document.createElement("summary");
      sum.textContent = data.kind === "diff"
        ? `${data.new ? "Новый файл" : "Изменения"}: ${data.path} (+${data.added} −${data.removed})`
        : "Что изменено";
      const pre = document.createElement("pre"); pre.className = "diff";
      data.diff.split("\n").forEach(line => {
        const span = document.createElement("span");
        span.className = line.startsWith("@@") ? "hunk" : line.startsWith("+++") || line.startsWith("---") ? "head"
          : line.startsWith("+") ? "add" : line.startsWith("-") ? "del" : "";
        span.textContent = line || " ";
        pre.appendChild(span);
      });
      det.append(sum, pre); wrap.appendChild(det);
    }
    return wrap;
  }
  function showData(data) {
    if (!["youtube", "web", "screenshot", "screen"].includes(data.kind)) return;
    const box = $("results");
    box.innerHTML = "";
    const h = document.createElement("h3");
    h.textContent = data.kind === "youtube" ? `YouTube: ${data.query}` : data.kind === "web" ? `Поиск: ${data.query} (${data.engine})` : data.kind === "screen" ? "Снимок экрана" : "Снимок страницы";
    box.append(h, renderData(data));
  }

  // ---------- ход задачи ----------
  function time(ts) { const d = ts ? new Date(ts * 1000) : new Date(); return d.toTimeString().slice(0, 8); }
  function timelineAdd(cls, title, detail, extra, ts) {
    const li = document.createElement("li");
    li.className = cls + (extra ? " " + extra : "");
    const t = document.createElement("span"); t.className = "t"; t.textContent = time(ts);
    const b = document.createElement("span"); b.textContent = title;
    li.append(t, b);
    if (detail) { const d = document.createElement("span"); d.className = "d"; d.textContent = detail; li.appendChild(d); }
    const tl = $("timeline");
    tl.appendChild(li);
    while (tl.children.length > 200) tl.removeChild(tl.firstChild);
    li.scrollIntoView({ block: "nearest" });
    return li;
  }
  function argsText(args) {
    if (!args) return "";
    return Object.entries(args).map(([k, v]) => `${k}: ${typeof v === "string" ? v : JSON.stringify(v)}`).join(", ");
  }
  const AGENT_TEXT = { researcher: "«исследователь»", "file-reader": "«аналитик файлов»" };
  function timelineEvent(ev, replay) {
    const name = TOOL_TEXT[ev.name] || ev.name;
    if (ev.type === "task") timelineAdd("task", ev.text, "", "task", ev.ts);
    else if (ev.type === "tool_start") {
      const li = timelineAdd(replay ? "ok" : "run", name, argsText(ev.args), "", ev.ts);
      if (!replay) toolRows.push(li);
    } else if (ev.type === "tool_end") {
      const row = toolRows.shift();
      // Для ответа внешнего MCP-сервера — сам ответ, без служебной пометки для модели.
      const text = ((ev.data && ev.data.display) || ev.text || "").split("\n")[0];
      if (row && !replay) { row.className = ev.ok ? "ok" : "fail"; const d = document.createElement("span"); d.className = "d"; d.textContent = text; row.appendChild(d); }
      else if (replay) { /* в истории tool_end идёт сразу после tool_start */ }
      else timelineAdd(ev.ok ? "ok" : "fail", name, text, "", ev.ts);
    } else if (ev.type === "confirm") timelineAdd("warn", "Нужно подтверждение", ev.action || "", "", ev.ts);
    else if (ev.type === "cancelled") timelineAdd("fail", "Задача остановлена", "", "", ev.ts);
  }

  // ---------- подтверждения ----------
  function showConfirm(text) { $("confirmText").textContent = text; $("confirmBar").hidden = false; }
  function hideConfirm() { $("confirmBar").hidden = true; }
  $("confirmYes").onclick = () => { send({ type: "confirm", answer: true }); hideConfirm(); };
  $("confirmNo").onclick = () => { send({ type: "confirm", answer: false }); hideConfirm(); };

  // ---------- ввод ----------
  $("composer").onsubmit = (e) => {
    e.preventDefault();
    const text = $("input").value.trim();
    if (!text) return;
    if (send({ type: "message", text })) $("input").value = "";
  };
  $("cancelBtn").onclick = () => { send({ type: "cancel" }); stopSpeech(); };
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") { send({ type: "cancel" }); stopSpeech(); } });

  // ---------- голос в браузере ----------
  // Распознавание — Web Speech API браузера. В Chrome/Edge звук обрабатывается сервисом Google
  // (облако); Firefox его не поддерживает. Локальное распознавание — режим run.bat / python -m jarvis.
  const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
  let rec = null, listening = false;
  function applyVoice(v) {
    if (!v) return;
    serverVoice = v;
    $("listenBtn").hidden = !v.enabled;
    // Локальный голос сам озвучивает ответы — озвучка браузера дала бы второй голос.
    $("ttsToggle").closest("label").hidden = !!v.enabled;
    if (v.enabled) stopSpeech();
    if (v.enabled) {
      $("micBtn").disabled = false;
      $("micBtn").setAttribute("aria-pressed", v.muted ? "false" : "true");
      $("micBtn").title = v.muted ? "Включить микрофон" : "Выключить микрофон";
      $("micHint").textContent = v.muted ? "Локальный микрофон выключен." :
        `Микрофон: ${v.mic || "по умолчанию"} — скажите «Джарвис» или нажмите «Слушать». Звук обрабатывается на этом компьютере.`;
    } else if (v.error) {
      $("micHint").textContent = "Локальный голос недоступен: " + v.error;
    }
  }
  $("listenBtn").onclick = () => { stopSpeech(); send({ type: "listen" }); };

  if (!Rec) {
    $("micBtn").disabled = true;
    $("micHint").textContent = "Голосовой ввод в этом браузере недоступен (локальный голос — режим python -m jarvis).";
  } else {
    $("micHint").textContent = "Микрофон выключен. Распознавание браузера отправляет звук в облако (Google).";
    rec = new Rec();
    rec.lang = "ru-RU"; rec.interimResults = true; rec.continuous = false;
    rec.onresult = (e) => {
      let text = "";
      for (const r of e.results) text += r[0].transcript;
      $("input").value = text;
      if (e.results[e.results.length - 1].isFinal && text.trim()) { send({ type: "message", text: text.trim() }); $("input").value = ""; }
    };
    rec.onend = () => { listening = false; micUi(); };
    rec.onerror = (e) => { addSystem("Микрофон: " + e.error, true); listening = false; micUi(); };
  }
  function micUi() {
    $("micBtn").setAttribute("aria-pressed", listening ? "true" : "false");
    if (listening) setState("listening"); else if (uiState === "listening") setState("idle");
    if (rec) $("micHint").textContent = listening ? "Слушаю… (звук уходит в сервис распознавания браузера)" : "Микрофон выключен.";
  }
  $("micBtn").onclick = () => {
    if (serverVoice.enabled) { send({ type: "mic", on: serverVoice.muted }); return; }
    if (!rec) return;
    if (listening) { rec.stop(); listening = false; }
    else { stopSpeech(); rec.lang = /[a-z]/i.test($("input").value) ? "en-US" : "ru-RU"; rec.start(); listening = true; }
    micUi();
  };

  // Озвучивание — speechSynthesis (голоса системы, обычно локально).
  try { $("ttsToggle").checked = localStorage.getItem("jarvis.tts") === "1"; } catch (e) { /* приватный режим */ }
  $("ttsToggle").onchange = () => { try { localStorage.setItem("jarvis.tts", $("ttsToggle").checked ? "1" : "0"); } catch (e) { /* */ } if (!$("ttsToggle").checked) stopSpeech(); };
  function plain(text) { return String(text).replace(/https?:\/\/\S+/g, "").replace(/[#*`|>_]/g, " ").replace(/\s+/g, " ").trim(); }
  function speak(text) {
    if (serverVoice.enabled || !$("ttsToggle").checked || !window.speechSynthesis || !text) return;
    const u = new SpeechSynthesisUtterance(plain(text).slice(0, 1200));
    u.lang = /[а-яё]/i.test(text) ? "ru-RU" : "en-US";
    u.onstart = () => { speaking = true; setState("speaking"); };
    u.onend = u.onerror = () => { speaking = false; if (uiState === "speaking") setState("idle"); };
    speechSynthesis.cancel(); speechSynthesis.speak(u);
  }
  function stopSpeech() { if (window.speechSynthesis) speechSynthesis.cancel(); if (speaking) { speaking = false; setState("idle"); } }
  $("shutUpBtn").onclick = () => { if (serverVoice.enabled) send({ type: "shut_up" }); stopSpeech(); };

  // ---------- вкладки ----------
  document.querySelectorAll(".tab").forEach(t => t.onclick = () => {
    document.querySelectorAll(".tab").forEach(x => x.classList.toggle("active", x === t));
    document.querySelectorAll(".panel").forEach(p => p.classList.toggle("active", p.id === "tab-" + t.dataset.tab));
    if (t.dataset.tab === "memory") loadMemory();
    if (t.dataset.tab === "schedule") loadSchedules();
    if (t.dataset.tab === "settings" || t.dataset.tab === "diag") refreshStatus();
    if (t.dataset.tab === "diag") { loadAudio(); if (!levelTimer) pollLevel(); }
  });

  // ---------- память ----------
  async function api(path, opts) {
    const r = await fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts || {}));
    if (!r.ok) throw new Error(`${r.status}`);
    return r.json();
  }
  const KIND_TITLES = { fact: "факт", preference: "предпочтение", note: "заметка", task: "задача" };
  function memItem(kind, item, editable) {
    const li = document.createElement("li");
    if (item.kind) {
      const k = document.createElement("span"); k.className = "kind " + item.kind; k.textContent = KIND_TITLES[item.kind] || item.kind;
      if (kind === "facts") {
        k.title = "Нажмите, чтобы сменить тип"; k.style.cursor = "pointer";
        k.onclick = async () => {
          await api(`/api/memory/facts/${item.id}`, { method: "PUT", body: JSON.stringify({ text: item.text, kind: item.kind === "fact" ? "preference" : "fact" }) });
          loadMemory();
        };
      }
      li.appendChild(k);
    }
    const span = document.createElement("span"); span.textContent = item.text;
    li.appendChild(span);
    if (editable) {
      const ed = document.createElement("button"); ed.className = "link-btn"; ed.textContent = "изменить";
      ed.onclick = async () => {
        const v = prompt("Исправить:", item.text);
        if (v === null) return;
        const res = await api(`/api/memory/${kind}/${item.id}`, { method: "PUT", body: JSON.stringify({ text: v }) });
        if (!res.ok) alert(res.error || "Не сохранено");
        loadMemory();
      };
      li.appendChild(ed);
    }
    const del = document.createElement("button"); del.className = "link-btn del"; del.textContent = "удалить";
    del.onclick = async () => { if (confirm("Удалить запись?")) { await api(`/api/memory/${kind}/${item.id}`, { method: "DELETE" }); loadMemory(); } };
    li.appendChild(del);
    return li;
  }
  async function loadMemory() {
    try {
      const m = await api("/api/memory");
      const fill = (id, kind, items, editable, fmt) => {
        const ul = $(id); ul.innerHTML = "";
        if (!items.length) { const li = document.createElement("li"); li.className = "muted"; li.textContent = "пусто"; ul.appendChild(li); }
        items.forEach(it => ul.appendChild(memItem(kind, fmt ? fmt(it) : it, editable)));
      };
      fill("facts", "facts", m.facts, true);
      fill("notes", "notes", m.notes, false);
      fill("tasks", "tasks", m.tasks || [], false, (t) => ({ id: t.id, kind: "task", text: `${new Date(t.created_at * 1000).toLocaleString()} — ${t.request} → ${t.result}` }));
      fill("reminders", "reminders", m.reminders, false, (r) => ({ id: r.id, text: `${new Date(r.due_at * 1000).toLocaleString()} — ${r.text}` }));
      const al = $("actions"); al.innerHTML = "";
      (m.actions || []).forEach(a => { const li = document.createElement("li"); li.textContent = a; al.appendChild(li); });
    } catch (e) { addSystem("Не удалось загрузить память: " + e.message, true); }
  }
  $("memRefresh").onclick = loadMemory;
  $("memImportFile").onchange = async (e) => {
    const file = e.target.files[0];
    e.target.value = "";
    if (!file) return;
    if (!confirm(`Добавить в память записи из «${file.name}»?`)) return;
    try {
      const r = await fetch("/api/memory/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: await file.text() });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || String(r.status));
      addSystem("Импорт памяти: " + d.text);
    } catch (err) { addSystem("Импорт не выполнен: " + err.message, true); }
    loadMemory();
  };

  // ---------- расписание ----------
  const RUN_STATUS = { ok: "✓", error: "✗", skipped: "пропущено", stopped: "остановлено" };
  async function schedCall(path, method, body) {
    const r = await fetch(path, { method, headers: { "Content-Type": "application/json" }, body: body ? JSON.stringify(body) : undefined });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(data.error || String(r.status));
    return data;
  }
  async function loadSchedules() {
    let d;
    try { d = await schedCall("/api/schedules", "GET"); } catch (e) { addSystem("Расписание: " + e.message, true); return; }
    const ul = $("schedList"); ul.innerHTML = "";
    if (!d.items.length) { const li = document.createElement("li"); li.className = "muted"; li.textContent = "задач нет"; ul.appendChild(li); }
    const names = {};
    d.items.forEach(it => {
      names[it.id] = it.prompt;
      const li = document.createElement("li");
      const text = document.createElement("span");
      const next = it.enabled && it.next_run ? ` · следующий: ${new Date(it.next_run * 1000).toLocaleString()}` : (it.enabled ? "" : " · выключена");
      text.textContent = `№${it.id} «${it.prompt}» — ${it.when}${next}` + (it.running ? " · выполняется…" : "")
        + (it.last ? ` · последний: ${RUN_STATUS[it.last.status] || it.last.status} ${new Date(it.last.started_at * 1000).toLocaleString()}` : "");
      const acts = document.createElement("div"); acts.className = "sched-acts";
      const mk = (label, fn, cls) => { const b = document.createElement("button"); b.className = "link-btn" + (cls ? " " + cls : ""); b.type = "button"; b.textContent = label; b.onclick = async () => { try { await fn(); } catch (e) { addSystem("Расписание: " + e.message, true); } loadSchedules(); }; acts.appendChild(b); };
      mk("запустить сейчас", () => schedCall(`/api/schedules/${it.id}/run`, "POST"));
      mk(it.enabled ? "выключить" : "включить", () => schedCall(`/api/schedules/${it.id}`, "PUT", { enabled: !it.enabled }));
      mk("удалить", () => confirm(`Удалить задачу «${it.prompt}»?`) ? schedCall(`/api/schedules/${it.id}`, "DELETE") : null, "del");
      li.append(text, acts); ul.appendChild(li);
    });
    const rl = $("schedRuns"); rl.innerHTML = "";
    if (!d.runs.length) { const li = document.createElement("li"); li.className = "muted"; li.textContent = "запусков ещё не было"; rl.appendChild(li); }
    d.runs.forEach(r => {
      const li = document.createElement("li");
      const det = document.createElement("details");
      const sum = document.createElement("summary");
      sum.textContent = `${new Date(r.started_at * 1000).toLocaleString()} · ${RUN_STATUS[r.status] || r.status} · ${names[r.schedule_id] || "№" + r.schedule_id}`;
      const body = document.createElement("div"); body.className = "small"; body.innerHTML = md.render(r.result || "");
      det.append(sum, body); li.appendChild(det); rl.appendChild(li);
    });
  }
  $("schedRefresh").onclick = loadSchedules;
  $("schedMode").onchange = () => {
    const m = $("schedMode").value;
    $("schedTime").hidden = m === "every"; $("schedDate").hidden = m !== "once"; $("schedEvery").hidden = m !== "every";
  };
  $("schedForm").onsubmit = async (e) => {
    e.preventDefault();
    const m = $("schedMode").value;
    const body = { prompt: $("schedPrompt").value.trim() };
    if (m === "every") body.every_minutes = Number($("schedEvery").value);
    else { body.time = $("schedTime").value; if (m === "once") body.date = $("schedDate").value; else body.days = m; }
    try { await schedCall("/api/schedules", "POST", body); $("schedPrompt").value = ""; addSystem("Задача запланирована."); }
    catch (err) { addSystem("Не запланировал: " + err.message, true); }
    loadSchedules();
  };
  const KIND_TABLES = { fact: "facts", preference: "facts", note: "notes", task: "tasks" };
  $("memSearchForm").onsubmit = async (e) => {
    e.preventDefault();
    const q = $("memSearch").value.trim();
    const ul = $("memFound"); ul.innerHTML = "";
    if (!q) return;
    const r = await api("/api/memory?q=" + encodeURIComponent(q));
    if (!r.found.length) { const li = document.createElement("li"); li.className = "muted"; li.textContent = "ничего не найдено"; ul.appendChild(li); return; }
    r.found.forEach(f => ul.appendChild(memItem(KIND_TABLES[f.kind], f, false)));
  };
  $("factForm").onsubmit = async (e) => {
    e.preventDefault();
    const text = $("factInput").value.trim();
    if (!text) return;
    const res = await api("/api/memory/facts", { method: "POST", body: JSON.stringify({ text, kind: $("factKind").value }) });
    if (!res.ok) alert(res.error); else $("factInput").value = "";
    loadMemory();
  };

  // ---------- настройки и диагностика ----------
  async function refreshStatus() {
    let s;
    try { s = await api("/api/status"); } catch (e) { return; }
    setProvider(s.provider.active);
    const pv = $("providers"); pv.innerHTML = "";
    Object.entries(s.provider.options).forEach(([key, note]) => {
      const d = document.createElement("label");
      d.className = "provider" + (s.provider.active === key ? " active" : "");
      const r = document.createElement("input"); r.type = "radio"; r.name = "prov"; r.checked = s.provider.active === key;
      r.onchange = async () => { await api("/api/settings", { method: "POST", body: JSON.stringify({ provider: key }) }); refreshStatus(); };
      const t = document.createElement("div");
      const extra = key === "claude"
        ? (s.provider.claude.available ? ` Модель: ${s.provider.claude.model || "по умолчанию"}. ${s.provider.claude.note}.` : ` Недоступно: ${s.provider.claude.note}.`)
        : key === "hybrid"
          ? ` Короткие вопросы: Ollama (${s.provider.ollama.model}); сложные и актуальные: Claude. ` +
            (s.provider.claude.available ? s.provider.claude.note + "." : `Claude недоступен: ${s.provider.claude.note}. Все ответы останутся локальными.`) +
            (s.ollama_ready === false ? " Ollama не отвечает или модель не скачана." : "")
          : ` Модель: ${s.provider.ollama.model}.` + (s.ollama_ready === false ? " Ollama не отвечает или модель не скачана." : "");
      t.innerHTML = `<div><b></b></div><div class="pnote"></div>`;
      t.querySelector("b").textContent = key === "claude" ? "Claude (Agent SDK)" : key === "hybrid" ? "Гибрид (Ollama + Claude)" : "Ollama";
      t.querySelector(".pnote").textContent = note + "." + extra;
      d.append(r, t); pv.appendChild(d);
    });
    renderSkills(s.skills || []);
    const tl = $("tools"); tl.innerHTML = "";
    s.tools.forEach(t => {
      const li = document.createElement("li");
      const c = document.createElement("code"); c.textContent = t.name;
      const p = document.createElement("span"); p.className = "perm " + t.permission;
      p.textContent = { auto: "без подтверждения", confirm: "с подтверждением", conditional: "подтверждение при риске" }[t.permission];
      const d = document.createElement("div"); d.className = "muted small"; d.textContent = t.description;
      li.append(c, p, d); tl.appendChild(li);
    });
    const dg = $("diag"); dg.innerHTML = "";
    const row = (ok, warn, text) => {
      const li = document.createElement("li"); li.className = ok ? "ok" : (warn ? "warn" : "bad");
      const mk = document.createElement("span"); mk.className = "mk"; mk.textContent = ok ? "✓" : (warn ? "!" : "✗");
      const tx = document.createElement("span"); tx.textContent = text;
      li.append(mk, tx); dg.appendChild(li);
    };
    row(true, false, `Состояние: ${STATE_TEXT[s.state] || s.state}${s.current ? " — " + s.current : ""}`);
    const b = s.browser;
    row(!b.last_error, true, `Браузер Джарвиса: ${b.running ? "запущен" : "не запущен (откроется при первой команде)"}` + (b.url ? ` · ${b.url}` : "") + (b.last_error ? ` · последняя ошибка: ${b.last_error}` : "") + ` · профиль: ${b.profile || "—"}`);
    s.checks.forEach(c => row(c.ok, !c.required, `${c.name}: ${c.detail}`));
    const f = s.files || { roots: [], missing: [] };
    row(f.roots.length > 0, true, f.roots.length
      ? `Файлы: разрешены ${f.roots.join(", ")}` + (f.missing.length ? ` · не найдены: ${f.missing.join(", ")}` : "")
      : "Файлы: папки не разрешены (config.toml → [files] allowed_dirs)" + (f.missing.length ? ` · не найдены: ${f.missing.join(", ")}` : ""));
    (s.mcp || []).forEach(m => {
      const st = { ready: "подключён", starting: "подключается…", error: "ошибка", off: "не подключён" }[m.state] || m.state;
      const tools = m.tools.length ? ` · инструменты: ${m.tools.length}` : "";
      const skipped = m.skipped.length ? ` · пропущены: ${m.skipped.join("; ")}` : "";
      row(m.state === "ready", m.state === "starting", `MCP ${m.name}: ${st}${m.error ? " — " + m.error : ""}${tools}${skipped}`);
    });
    const v = s.voice || {};
    row(v.enabled, !v.error, v.enabled ? `Локальный голос: включён, микрофон ${v.mic || "по умолчанию"}${v.muted ? " (выключен)" : ""}`
      : `Локальный голос: ${v.error ? "ошибка — " + v.error : "не запущен (run_web.bat или --web --voice)"}`);
    row(true, false, `Микрофон браузера: ${Rec ? "распознавание браузера (облако Google)" : "недоступно в этом браузере"}; озвучивание: ${window.speechSynthesis ? "голоса системы" : "нет"}`);
  }
  $("diagRefresh").onclick = () => { refreshStatus(); loadAudio(); };

  function renderSkills(list) {
    const ul = $("skills"); ul.innerHTML = "";
    if (!list.length) { const li = document.createElement("li"); li.className = "muted small"; li.textContent = "Навыков нет."; ul.appendChild(li); return; }
    list.forEach(k => {
      const li = document.createElement("li");
      const c = document.createElement("code"); c.textContent = k.name;
      const d = document.createElement("div"); d.className = "muted small";
      if (k.error) { li.className = "bad"; d.textContent = "Не загружен: " + k.error; }
      else {
        d.textContent = k.description
          + (k.triggers.length ? ` · фразы: «${k.triggers.join("», «")}»` : "")
          + ` · инструменты: ${k.tools.length ? k.tools.join(", ") : "все"}`
          + (k.missing_tools.length ? ` · нет сейчас: ${k.missing_tools.join(", ")}` : "");
      }
      li.append(c, d); ul.appendChild(li);
    });
  }
  $("skillsReload").onclick = async () => {
    try { const r = await api("/api/skills/reload", { method: "POST" }); renderSkills(r.skills || []); }
    catch (e) { addSystem("Навыки: " + e.message); }
  };

  // ---------- звук ----------
  let levelTimer = null;
  async function loadAudio() {
    let a;
    try { a = await api("/api/audio"); } catch (e) { return; }
    const sel = $("micSelect"); sel.innerHTML = "";
    const def = document.createElement("option"); def.value = ""; def.textContent = "По умолчанию (Windows)";
    sel.appendChild(def);
    (a.devices || []).forEach(d => {
      const o = document.createElement("option");
      o.value = d.spec;
      o.textContent = `${d.name} · ${d.api}${d.default ? " · по умолчанию" : ""}${d.virtual ? " · виртуальный, звука не будет" : ""}`;
      if (d.spec === a.selected) o.selected = true;
      sel.appendChild(o);
    });
    $("audioResult").textContent = a.error || (a.speaker ? `Голос: ${a.speaker}` : "");
    $("micTest").disabled = !!(a.voice && a.voice.enabled);
    $("ttsTest").disabled = !(a.voice && a.voice.enabled);
  }
  async function pollLevel() {
    if (!$("tab-diag").classList.contains("active")) { levelTimer = null; return; }
    try {
      const l = await api("/api/audio/level");
      const pct = Math.min(100, Math.round(l.level / 0.3 * 100));
      $("levelBar").style.width = (l.active && !l.muted ? pct : 0) + "%";
      $("levelText").textContent = !l.active ? "Уровень появится, когда работает локальный голос." :
        l.muted ? "Микрофон выключен." : `Уровень: ${l.level.toFixed(3)} ${l.level < 0.01 ? "— тишина или микрофон не тот" : ""}`;
    } catch (e) { /* сервер перезапускается */ }
    levelTimer = setTimeout(pollLevel, 250);
  }
  $("micSave").onclick = async () => {
    const r = await api("/api/audio/device", { method: "POST", body: JSON.stringify({ spec: $("micSelect").value }) });
    $("audioResult").textContent = r.ok ? `Микрофон: ${r.name}${r.applied ? " — уже слушаю его." : " — будет использован при запуске голоса."}` : r.error;
  };
  $("micTest").onclick = async () => {
    $("audioResult").textContent = "Запись 4 секунды: первую секунду помолчите, потом скажите «Джарвис, который час?»…";
    $("micTest").disabled = true;
    const r = await api("/api/audio/test", { method: "POST", body: JSON.stringify({ spec: $("micSelect").value }) });
    $("micTest").disabled = false;
    $("audioResult").textContent = r.ok
      ? `Оценка: ${r.verdict}. Речь ${r.speech}, шум ${r.noise}, речь громче шума в ${r.snr} раза. ${(r.advice || []).join(" ")}`
      : r.error;
  };
  $("ttsTest").onclick = async () => {
    const r = await api("/api/audio/speak", { method: "POST", body: "{}" });
    $("audioResult").textContent = r.ok ? `Говорю: ${r.speaker}` : r.error;
  };

  setState("idle");
  connect();
})();
