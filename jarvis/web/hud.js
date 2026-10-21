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
    open_anything: "Открываю", open_folder: "Открываю папку", open_file: "Открываю файл", fetch_page: "Читаю страницу",
    calculate: "Считаю", current_time: "Смотрю время", system_info: "Состояние компьютера", media_control: "Музыка и громкость",
    set_timer: "Ставлю таймер", set_reminder: "Ставлю напоминание", list_reminders: "Напоминания", cancel_reminders: "Отменяю напоминания",
    add_note: "Записываю заметку", read_notes: "Читаю заметки", search_notes: "Ищу в заметках", delete_last_note: "Удаляю заметку", clear_notes: "Очищаю заметки",
    remember_fact: "Запоминаю", forget_fact: "Забываю", search_memory: "Ищу в памяти", change_setting: "Меняю настройку", set_voice: "Меняю голос",
    files_list: "Смотрю папку", files_read: "Читаю файл", files_search: "Ищу в файлах", files_write: "Записываю файл", files_edit: "Правлю файл",
    search_files: "Ищу файлы", delete_file: "Удаляю в корзину", list_windows: "Окна", close_window: "Закрываю окно", lock_screen: "Блокирую экран",
    schedule_task: "Планирую задачу", list_schedules: "Плановые задачи", cancel_schedule: "Отменяю плановую задачу", use_skill: "Навык",
    shutdown_computer: "Выключение", restart_computer: "Перезагрузка", sleep_computer: "Сон", cancel_shutdown: "Отмена выключения",
    site_find: "Ищу текущий сайт", site_analyze: "Разбираю текущий сайт", site_design: "Стиль и тексты", site_review: "Команда проверяет тексты", site_polish: "Правки по замечаниям",
    site_layout: "Вёрстка", site_check: "Проверка качества", site_show: "Показываю сайт", site_edit: "Правка сайта",
    site_open: "Открываю сайт", site_folder: "Папка сайта",
    team_claude: "Спрашиваю Claude", team_codex: "Спрашиваю ChatGPT", team_gemini: "Спрашиваю Gemini",
    team_local: "Спрашиваю локальную модель", team_ollama_cloud: "Спрашиваю Ollama Cloud", team_lead: "Ведущий сводит ответы",
  };

  let ws = null, reconnectDelay = 500, uiState = "idle", uiVersion = null, replayed = false;
  const bubbles = new Map();   // task -> {el, text}
  const toolRows = [];         // незавершённые шаги
  let speaking = false;
  // Локальный голос на стороне сервера (python -m jarvis --web --voice): Vosk/Whisper/Silero.
  let serverVoice = { enabled: false, muted: false, mic: null };

  // ---------- соединение ----------
  function connect() {
    ws = new WebSocket(`ws://${location.host}/ws`);
    ws.onopen = () => { setConn(true); reconnectDelay = 500; refreshStatus(); loadBrief(); };
    ws.onclose = () => { setConn(false); setTimeout(connect, reconnectDelay); reconnectDelay = Math.min(reconnectDelay * 2, 8000); };
    ws.onmessage = (e) => { try { handle(JSON.parse(e.data)); } catch (err) { console.error(err); } };
  }
  function send(obj) {
    if (ws && ws.readyState === 1) { ws.send(JSON.stringify(obj)); return true; }
    addSystem("Нет связи с сервером Джарвиса. Он запущен?", true);
    return false;
  }
  function setConn(ok) {
    $("connDot").classList.toggle("on", ok);
    $("connText").textContent = ok ? "" : "нет связи";
  }

  // ---------- события ----------
  function handle(ev) {
    switch (ev.type) {
      case "hello":
        // Сервер перезапустился с новым интерфейсом (обновление) — загрузить новую версию страницы.
        if (uiVersion && ev.ui && ev.ui !== uiVersion) { location.reload(); return; }
        uiVersion = ev.ui || uiVersion;
        setState(ev.state || "idle");
        setProvider(ev.provider);
        applyVoice(ev.voice);
        // История сервера: при переподключении заменяет «Шаги» (без повторов); после перезапуска
        // сервера она пуста — тогда остаётся то, что уже показано.
        if ((ev.history || []).length) {
          $("timeline").innerHTML = "";
          ev.history.slice(-60).forEach(h => { if (h.type !== "state") timelineEvent(h, true); });
        }
        if (!replayed) { replayed = true; replayFeed((ev.history || []).slice(-80)); }
        { const jobs = (ev.history || []).filter(h => h.type === "site" || h.type === "team");
          const last = jobs[jobs.length - 1];
          if (last) last.type === "site" ? renderProject(last.project) : renderTeam(last.job); }
        applyUpdateInfo(ev.update);
        break;
      case "update": update.status = ev; renderUpdate(); break;
      case "restarting":
        restarting = true;
        addSystem("Обновляюсь и перезапускаюсь… Обычно это 10–60 секунд, страница переподключится сама.");
        setState("acting"); $("stateText").textContent = "Перезапуск";
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
      case "route": timelineEvent(ev); break;
      case "delegate": timelineAdd("task", "Поручение агенту " + (AGENT_TEXT[ev.agent] || ev.agent), ev.text || "", "", ev.ts); break;
      case "site": renderProject(ev.project, true); break;
      case "team": renderTeam(ev.job, true); break;
      case "memory": renderMemoryEvent(ev, true); break;
      case "memory_saved":
        timelineAdd("ok", "Записано в память: " + (ev.note.title || ""), [NOTE_TYPES[ev.note.type] || ev.note.type,
          STATUS_TEXT[ev.note.status] || ev.note.status].filter(Boolean).join(" · "), "", ev.ts);
        loadBrief();
        break;
    }
  }

  function setState(state, detail) {
    uiState = state;
    $("statePill").dataset.state = state;
    $("core").dataset.state = state;
    $("stateText").textContent = STATE_TEXT[state] || state;
    $("stateDetail").textContent = state === "acting" ? (TOOL_TEXT[detail] || detail || "")
      : (state === "error" || state === "idle") ? (detail || "") : "";
    $("cancelBtn").hidden = !["thinking", "acting", "speaking"].includes(state);
    $("shutUpBtn").hidden = state !== "speaking";
    if (orb) orb.setState(state);
    updateHeadline();
    eyeLevel();
  }

  // Крупная строка слева — то, что Джарвис делает или ждёт прямо сейчас.
  let restarting = false;
  function updateHeadline() {
    const v = serverVoice;
    $("headline").textContent =
      restarting ? "Перезапускаюсь." :
      uiState === "listening" ? "Слушаю." :
      uiState === "thinking" ? "Думаю над задачей." :
      uiState === "acting" ? "Выполняю." :
      uiState === "speaking" ? "Отвечаю." :
      uiState === "error" ? "Задача не выполнена." :
      v.enabled && !v.muted ? "Скажите «Джарвис» — я слушаю." :
      v.enabled ? "Микрофон выключен. Напишите, что сделать." : "Напишите, что сделать.";
  }

  // Модель: Гибрид / Ollama / Claude / Gemini — переключатель вверху.
  function setProvider(p) {
    document.querySelectorAll("#modeSwitch [data-provider]").forEach(b =>
      b.setAttribute("aria-checked", String(b.dataset.provider === p)));
  }
  document.querySelectorAll("#modeSwitch [data-provider]").forEach(b => b.onclick = async () => {
    if (b.getAttribute("aria-checked") === "true") return;
    try {
      await api("/api/settings", { method: "POST", body: JSON.stringify({ provider: b.dataset.provider }) });
      setProvider(b.dataset.provider);
      refreshStatus();
    } catch (e) { addSystem("Не удалось переключить модель: " + e.message, true); }
  });

  // ---------- разговор ----------
  function scrollChat() { const c = $("chat"); c.scrollTop = c.scrollHeight; }
  function hhmm(d) { return (d || new Date()).toTimeString().slice(0, 5); }
  function entry(kind, who, ts) {
    const el = document.createElement("article");
    el.className = "entry " + kind;
    if (who) {
      const head = document.createElement("div"); head.className = "who";
      const name = document.createElement("span"); name.textContent = who;
      const t = document.createElement("time"); t.textContent = hhmm(ts ? new Date(ts * 1000) : undefined);
      head.append(name, t); el.appendChild(head);
    }
    const body = document.createElement("div"); body.className = "body";
    el.appendChild(body);
    $("chat").appendChild(el);
    return el;
  }
  function addUser(text, byVoice, ts) {
    entry("user", byVoice ? "Вы сказали" : "Вы написали", ts).querySelector(".body").textContent = text;
    scrollChat();
  }
  // Разговор до открытия страницы (голосом или в другой вкладке) — из истории сервера.
  function replayFeed(history) {
    history.forEach(h => {
      if (h.type === "task") addUser(h.text, h.source === "voice", h.ts);
      else if (h.type === "reply" && !h.ignored && h.text) {
        const el = entry("jarvis" + (h.ok === false ? " error" : ""), "Джарвис", h.ts);
        const listed = h.data && (h.data.kind === "youtube" || h.data.kind === "web");
        el.querySelector(".body").innerHTML = md.render(listed ? h.text.split("\n")[0] : h.text);
        if (h.data) el.appendChild(renderData(h.data));
      }
    });
    scrollChat();
  }
  function addSystem(text, error) {
    entry("system" + (error ? " error" : ""), "").querySelector(".body").textContent = text;
    scrollChat();
  }
  function bubble(task) {
    let b = bubbles.get(task);
    if (!b) {
      b = { el: entry("jarvis streaming", "Джарвис"), text: "" };
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
      // Номера настоящие: «открой второй результат» открывает №2.
      const list = document.createElement("ol");
      list.className = "results-list";
      (data.items || []).forEach((it, i) => {
        const c = document.createElement("li");
        c.className = "result";
        const num = document.createElement("span"); num.className = "num"; num.textContent = String(i + 1);
        const title = document.createElement("div"); title.className = "title"; title.appendChild(link(it.url, it.title));
        const meta = document.createElement("div"); meta.className = "meta";
        (data.kind === "youtube" ? [it.channel, it.duration, it.views, it.published] : [it.source])
          .filter(Boolean).forEach(x => { const s = document.createElement("span"); s.textContent = x; meta.appendChild(s); });
        const snip = document.createElement("div"); snip.className = "snippet";
        snip.textContent = data.kind === "youtube" ? (it.description || "") : (it.snippet || "");
        const acts = document.createElement("div"); acts.className = "acts";
        const open = document.createElement("button"); open.className = "link-btn"; open.type = "button";
        open.textContent = "Открыть у Джарвиса";
        open.onclick = () => send({ type: "tool", name: "open_result", args: { index: i + 1 } });
        acts.appendChild(open);
        c.append(num, title);
        if (meta.childNodes.length) c.appendChild(meta);
        if (snip.textContent) c.appendChild(snip);
        c.appendChild(acts);
        list.appendChild(c);
      });
      wrap.appendChild(list);
    } else if (data.kind === "screenshot" || data.kind === "screen") {
      const img = document.createElement("img"); img.className = "shot";
      img.alt = data.kind === "screen" ? "Снимок экрана" : "Снимок страницы";
      img.src = "/api/screenshots/" + encodeURIComponent(data.file);
      wrap.appendChild(img);
    } else if (data.kind === "draft" && data.text) {
      // Текст из чата HUD не печатается в окно (активно само окно HUD) — копируется кнопкой.
      wrap.className = "draft-acts";
      const copy = document.createElement("button");
      copy.type = "button"; copy.className = "btn"; copy.textContent = "Скопировать текст";
      copy.onclick = async () => {
        try { await navigator.clipboard.writeText(data.text); }
        catch (e) {
          const area = document.createElement("textarea");
          area.value = data.text; document.body.appendChild(area); area.select();
          document.execCommand("copy"); area.remove();
        }
        copy.textContent = "Скопировано";
        setTimeout(() => { copy.textContent = "Скопировать текст"; }, 2000);
      };
      wrap.appendChild(copy);
    } else if (data.kind === "page" || data.kind === "opened") {
      wrap.className = "opened";
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
    } else if (data.kind === "site" && data.project && data.project.result) {
      wrap.className = "site-card";
      wrap.appendChild(siteResult(data.project.result, true));
    }
    return wrap;
  }

  // ---------- студия сайтов: вкладка «Проекты» ----------
  const STEP_STATUS = { wait: "ждёт", run: "в работе", ok: "готово", skip: "пропущено", fail: "ошибка" };
  const dismissed = new Set();   // отклонённые предложения: «проект|текст»
  let shownProject = null;
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function shot(file, alt, cls) {
    const img = el("img", cls || "site-shot");
    img.alt = alt; img.loading = "lazy";
    img.src = "/api/screenshots/" + encodeURIComponent(file);
    return img;
  }
  function siteResult(r, compact) {
    const box = el("div", "site-result");
    const meta = [r.seconds != null ? `за ${r.seconds} с` : "", r.model, r.style ? `стиль «${r.style}»` : "",
      r.background === "dark" ? "тёмная тема" : ""].filter(Boolean).join(" · ");
    if (meta) box.appendChild(el("p", "site-meta", meta));
    if ((r.team || []).length) {  // «… командой»: что заметил каждый ИИ команды
      const team = el("div", "site-team");
      team.appendChild(el("p", "scores-head", "Замечания команды"));
      r.team.forEach(n => {
        const more = el("details", "job-answer");
        more.appendChild(el("summary", "", n.status === "ok" ? `${n.title} · ${Math.round(n.seconds)} с` : `${n.title} · не ответил`));
        const body = el("div", "body");
        if (n.status === "ok") body.innerHTML = md.render(n.text); else body.textContent = n.text;
        more.appendChild(body);
        team.appendChild(more);
      });
      box.appendChild(team);
    }
    if (r.shots) {
      const shots = el("div", "site-shots");
      shots.append(shot(r.shots.desktop, "Новый сайт на компьютере", "site-shot desk"),
        shot(r.shots.mobile, "Новый сайт на телефоне", "site-shot phone"));
      box.appendChild(shots);
    }
    if ((r.rows || []).length) {
      const t = el("div", "scores");
      const hasBefore = r.rows.some(x => x.before != null);
      t.appendChild(el("p", "scores-head", hasBefore ? "Проверка качества: было → стало" : "Проверка качества нового сайта"));
      r.rows.forEach(x => {
        const row = el("div", "score-row");
        row.appendChild(el("span", "score-label", x.label));
        const bars = el("div", "score-bars");
        if (x.before != null) { const b = el("i", "bar before"); b.style.width = x.before + "%"; bars.appendChild(b); }
        const a = el("i", "bar after"); a.style.width = (x.after || 0) + "%"; bars.appendChild(a);
        row.appendChild(bars);
        row.appendChild(el("span", "score-num", (x.before != null ? x.before + " → " : "") + (x.after != null ? x.after : "—")));
        t.appendChild(row);
      });
      box.appendChild(t);
    }
    const acts = el("div", "row site-acts");
    const open = el("button", "btn primary", "Открыть сайт"); open.type = "button";
    open.onclick = () => send({ type: "tool", name: "site_open", args: {} });
    const folder = el("button", "btn", "Папка с файлами"); folder.type = "button";
    folder.onclick = () => send({ type: "tool", name: "site_folder", args: {} });
    acts.append(open, folder);
    box.appendChild(acts);
    if (!compact && r.before_shots && r.before_shots.desktop) {
      const det = el("details", "site-before");
      det.appendChild(el("summary", "", "Как было: " + (r.before_url || "старый сайт")));
      det.appendChild(shot(r.before_shots.desktop, "Старый сайт на компьютере", "site-shot desk"));
      box.appendChild(det);
    }
    return box;
  }
  function renderProject(p, live) {
    if (!p) return;
    const box = $("project");
    box.innerHTML = "";
    const head = el("div", "panel-head");
    head.appendChild(el("h3", "", (p.kind === "edit" ? "Правка сайта «" : "Сайт «") + (p.brand || p.id) + "»"));
    if (p.url) head.appendChild(link(p.url, p.url.replace(/^https?:\/\//, "").replace(/\/$/, "")));
    box.appendChild(head);
    const list = el("ol", "jobs");
    (p.steps || []).forEach(s => {
      const li = el("li", "job " + s.status);
      const top = el("div", "job-top");
      top.append(el("span", "job-status", STEP_STATUS[s.status] || s.status), el("span", "job-title", s.title));
      li.appendChild(top);
      if (s.detail) li.appendChild(el("p", "job-detail", s.detail));
      list.appendChild(li);
    });
    box.appendChild(list);
    if (p.result) box.appendChild(siteResult(p.result, false));
    const props = (p.proposals || []).filter(x => !dismissed.has(p.id + "|" + x.text));
    if (props.length) {
      box.appendChild(el("h3", "", "Предложения"));
      const ul = el("ul", "proposals");
      props.forEach(x => {
        const li = el("li", "proposal");
        li.appendChild(el("span", "", x.text));
        const acts = el("div", "row");
        const yes = el("button", "btn primary", "Сделать"); yes.type = "button";
        yes.onclick = () => { if (send({ type: "message", text: x.say })) li.remove(); };
        const no = el("button", "btn", "Отклонить"); no.type = "button";
        no.onclick = () => { dismissed.add(p.id + "|" + x.text); li.remove(); };
        acts.append(yes, no); li.appendChild(acts); ul.appendChild(li);
      });
      box.appendChild(ul);
    }
    // Новая задача студии — показать вкладку «Проекты» (один раз за задачу, дальше вкладку выбирает человек).
    const key = p.id + "|" + p.kind + "|" + (p.steps || []).length;
    if (live && shownProject !== key) {
      shownProject = key;
      const tab = document.querySelector('.tab[data-tab="projects"]');
      if (tab && !tab.classList.contains("active")) tab.click();
    }
  }
  // Команда ИИ: карточка на каждого участника (статус, время, ответ), ведущий и итог.
  function renderTeam(j, live) {
    if (!j) return;
    const box = $("project");
    box.innerHTML = "";
    const head = el("div", "panel-head");
    head.appendChild(el("h3", "", "Команда ИИ"));
    box.append(head, el("p", "team-question", "«" + j.question + "»"));
    const list = el("ol", "jobs");
    (j.answers || []).forEach(a => {
      const li = el("li", "job " + a.status);
      const top = el("div", "job-top");
      top.append(el("span", "job-status", STEP_STATUS[a.status] || a.status), el("span", "job-title", a.title));
      if (a.status === "ok" || a.status === "fail") top.appendChild(el("span", "job-time", Math.round(a.seconds) + " с"));
      li.appendChild(top);
      if (a.text && a.status === "ok") {
        const plain = a.text.replace(/^\s*(?:[-*•]|\d+[.)])\s+/gm, "").replace(/[*_`#|]+/g, "").replace(/\s+/g, " ").trim();
        const more = el("details", "job-answer");
        const full = el("div", "body");
        full.innerHTML = md.render(a.text);
        more.append(el("summary", "", plain.length > 140 ? plain.slice(0, 140) + "…" : plain), full);
        li.appendChild(more);
      } else if (a.text) li.appendChild(el("p", "job-detail", a.text));
      list.appendChild(li);
    });
    if (j.lead && j.lead_status !== "skip") {
      const li = el("li", "job " + j.lead_status);
      const top = el("div", "job-top");
      const who = (j.answers || []).find(a => a.key === j.lead);
      top.append(el("span", "job-status", STEP_STATUS[j.lead_status] || j.lead_status),
        el("span", "job-title", "Ведущий " + (who ? who.title : j.lead) + " сводит ответы"));
      li.appendChild(top);
      list.appendChild(li);
    }
    box.appendChild(list);
    if (j.final) {
      const fin = el("div", "team-final");
      const body = el("div", "body");
      body.innerHTML = md.render(j.final);
      fin.append(el("p", "scores-head", "Итог команды" + (j.seconds ? ` · за ${j.seconds} с` : "")), body);
      box.appendChild(fin);
    }
    const key = j.id + "|team";
    if (live && shownProject !== key) {
      shownProject = key;
      const tab = document.querySelector('.tab[data-tab="projects"]');
      if (tab && !tab.classList.contains("active")) tab.click();
    }
  }
  function showData(data) {
    if (!["youtube", "web", "screenshot", "screen"].includes(data.kind)) return;
    const box = $("results");
    box.innerHTML = "";
    const h = document.createElement("h3");
    h.textContent = data.kind === "youtube" ? `Видео по запросу «${data.query}»`
      : data.kind === "web" ? `Поиск «${data.query}»` : data.kind === "screen" ? "Снимок экрана" : "Снимок страницы";
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
    else if (ev.type === "route") timelineAdd("route", ROUTE_TEXT[ev.target] || ev.target, ROUTE_WHERE[ev.target] + (ev.reason || ""), "", ev.ts);
  }
  // Гибридный режим: кто отвечает на реплику и почему.
  const ROUTE_TEXT = { claude: "Отвечает Claude", local: "Отвечает Ollama" };
  const ROUTE_WHERE = { claude: "в облаке: ", local: "на этом компьютере: " };

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
    $("input").placeholder = v.enabled ? "Скажите «Джарвис» или напишите здесь" : "Напишите, что сделать";
    $("core").setAttribute("aria-label", v.enabled ? (v.muted ? "Включить микрофон" : "Слушать") : (Rec ? "Голосовой ввод" : "Написать команду"));
    updateHeadline();
    eyeLevel();
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
  // Глаз — главная кнопка голоса: слушать без «Джарвис» (или включить микрофон).
  $("core").onclick = () => {
    if (serverVoice.enabled) {
      if (serverVoice.muted) send({ type: "mic", on: true });
      else { stopSpeech(); send({ type: "listen" }); }
    } else if (Rec) $("micBtn").click();
    else $("input").focus();
  };
  // ---------- сфера ----------
  // Сигнал для сферы — только настоящий: уровень микрофона, пока Джарвис слушает, и громкость
  // звучащего голоса Silero, пока говорит (локальный голос). Сигнала нет (системный голос Windows,
  // распознавание браузера) — сфера играет анимацию состояния, и подпись под ней говорит об этом.
  let orb = null, sigLoop = false, sigKind = null;
  function startOrb() {
    const pref = store("jarvis.orb") || "auto";
    $("orbQuality").value = pref;
    if (window.JarvisOrb && pref !== "off") {
      try { orb = window.JarvisOrb.create($("orbCanvas"), { quality: pref, noWebGL: /[?&]orb=canvas\b/.test(location.search) }); }
      catch (e) { console.warn("Сфера не запустилась:", e); orb = null; }
    }
    $("core").classList.toggle("orb-on", !!(orb && orb.active));
  }
  function orbInfo() {
    const i = orb ? orb.info() : null;
    $("orbInfo").textContent = !window.JarvisOrb ? "Сфера недоступна: не загрузился orb.js."
      : !i || i.renderer === "none" ? "Сфера выключена — показан простой глаз."
      : `${i.renderer === "webgl" ? "WebGL" : "Canvas 2D (WebGL недоступен)"} · ${{ high: "высокое", medium: "среднее", low: "низкое", canvas: "облегчённое" }[i.quality] || i.quality} качество · ${i.points} частиц · ${i.fps} кадров/с`
        + (i.software ? " · видеокарта не используется (программный рендер)" : "")
        + (i.reducedMotion ? " · «уменьшить движение» в системе: сфера почти неподвижна" : "")
        + (i.degraded.length ? " · снижено: " + i.degraded.join(", ") : "");
  }
  $("orbQuality").onchange = () => {
    const q = $("orbQuality").value;
    store("jarvis.orb", q);
    if (!orb && q !== "off") startOrb(); else if (orb) orb.setQuality(q);
    $("core").classList.toggle("orb-on", !!(orb && orb.active));
    orbSource(); orbInfo();
  };
  function orbSource() {
    const box = $("orbSource");
    box.hidden = !(orb && orb.active) || !(uiState === "listening" || uiState === "speaking");
    if (box.hidden) return;
    box.textContent = sigKind === "mic" ? "Сфера: уровень микрофона"
      : sigKind === "speech" ? "Сфера: громкость голоса Джарвиса"
      : sigKind === "words" ? "Сфера: слова озвучки браузера"
      : "Сфера: анимация состояния — сигнала звука нет";
  }
  function eyeLevel() {
    if (serverVoice.enabled && (uiState === "listening" || uiState === "speaking") && !document.hidden) {
      if (!sigLoop) { sigLoop = true; pollSignal(); }
    } else if (!sigLoop) {
      $("core").style.setProperty("--lvl", "0");
      if (orb) orb.setSignal(null);
      if (sigKind !== "words") sigKind = null;
    }
    orbSource();
  }
  async function pollSignal() {
    const listen = uiState === "listening", talk = uiState === "speaking";
    if (!(serverVoice.enabled && (listen || talk)) || document.hidden) {
      sigLoop = false; sigKind = null;
      if (orb) orb.setSignal(null);
      $("core").style.setProperty("--lvl", "0");
      orbSource();
      return;
    }
    try {
      const l = await api("/api/audio/level");
      let v = null;
      if (listen && l.active && !l.muted) v = Math.min(1, Math.sqrt(l.level / 0.25));
      else if (talk && l.speech != null) v = l.speech;
      sigKind = v == null ? null : listen ? "mic" : "speech";
      if (orb) orb.setSignal(v);
      $("core").style.setProperty("--lvl", String(listen && v != null ? v : 0));  // простой глаз без сферы
    } catch (e) { /* сервер перезапускается */ }
    orbSource();
    setTimeout(pollSignal, 70);
  }
  document.addEventListener("visibilitychange", eyeLevel);

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
    // Голос браузера не даёт громкость, но сообщает границы слов — сфера вздрагивает на каждом слове.
    u.onboundary = (e) => { if (orb && e.name !== "sentence") { orb.pulse(0.7); if (sigKind !== "words") { sigKind = "words"; orbSource(); } } };
    u.onend = u.onerror = () => { speaking = false; sigKind = null; if (uiState === "speaking") setState("idle"); };
    speechSynthesis.cancel(); speechSynthesis.speak(u);
  }
  function stopSpeech() { if (window.speechSynthesis) speechSynthesis.cancel(); if (speaking) { speaking = false; setState("idle"); } }
  $("shutUpBtn").onclick = () => { if (serverVoice.enabled) send({ type: "shut_up" }); stopSpeech(); };

  // ---------- верхняя панель: часы, загрузка компьютера, тема ----------
  function greetingText(h) {
    return h >= 5 && h < 12 ? "Доброе утро." : h >= 12 && h < 17 ? "Добрый день." : h >= 17 && h < 23 ? "Добрый вечер." : "Доброй ночи.";
  }
  function tick() {
    const d = new Date();
    $("clock").textContent = hhmm(d);
    $("clock").dateTime = d.toISOString();
    $("clock").title = d.toLocaleDateString("ru-RU", { weekday: "long", day: "numeric", month: "long" });
    $("greeting").textContent = greetingText(d.getHours());
  }
  const num1 = (n) => n.toFixed(1).replace(".", ",");
  function gauge(box, value, text, hot, title) {
    $(box).hidden = text == null;
    if (text == null) return;
    $(value).textContent = text;
    $(box).classList.toggle("hot", !!hot);
    $(box).title = title || "";
  }
  async function pollSystem() {
    if (document.visibilityState === "visible") {
      try {
        const s = await api("/api/system");
        gauge("gCpu", "sysCpu", s.cpu == null ? null : `${Math.round(s.cpu)}%`, s.cpu >= 90, "Загрузка процессора");
        gauge("gRam", "sysRam", s.ram_used == null ? null : `${num1(s.ram_used)} из ${Math.round(s.ram_total)} ГБ`,
          s.ram_used / s.ram_total > 0.9, "Занятая оперативная память");
        const g = s.gpu;
        gauge("gGpu", "sysGpu", g ? `${Math.round(g.util)}%` : null, g && g.util >= 95,
          g ? `${g.name}: видеопамять ${num1(g.mem_used)} из ${num1(g.mem_total)} ГБ` : "");
      } catch (e) { /* сервер перезапускается */ }
    }
    setTimeout(pollSystem, 3000);
  }
  function applyTheme(t) {
    document.documentElement.dataset.theme = t;
    const next = t === "blue" ? "Красная тема" : "Синяя тема";
    $("themeBtn").title = next;
    $("themeBtn").setAttribute("aria-label", next);
  }
  $("themeBtn").onclick = () => {
    const t = document.documentElement.dataset.theme === "blue" ? "red" : "blue";
    applyTheme(t);
    store("jarvis.theme", t);
  };
  // Быстрые команды — те же фразы, что можно сказать или написать.
  document.querySelectorAll("#chips [data-say]").forEach(b => b.onclick = () => send({ type: "message", text: b.dataset.say }));
  // «Сайт для бизнеса»: начало команды в поле ввода — осталось дописать, для кого.
  document.querySelectorAll("#chips [data-fill]").forEach(b => b.onclick = () => {
    const input = $("input"); input.value = b.dataset.fill; input.focus();
    input.setSelectionRange(input.value.length, input.value.length);
  });

  // ---------- обновления ----------
  // Сервер проверяет обновления сам (git fetch при запуске и раз в несколько часов) и шлёт
  // событие update; «Обновить и перезапустить» работает, когда HUD запущен через run_web.bat.
  const update = { status: null, supervised: false, version: null };
  function store(key, value) {
    try { if (value === undefined) return localStorage.getItem(key); localStorage.setItem(key, value); }
    catch (e) { /* приватный режим */ }
    return null;
  }
  function plural(n, forms) {
    const m10 = n % 10, m100 = n % 100;
    return `${n} ${forms[m10 === 1 && m100 !== 11 ? 0 : m10 >= 2 && m10 <= 4 && (m100 < 10 || m100 >= 20) ? 1 : 2]}`;
  }
  const CHANGES = ["изменение", "изменения", "изменений"];
  function ago(ts) {
    const s = Math.max(0, Date.now() / 1000 - ts);
    if (s < 90) return "только что";
    if (s < 3600) return `${Math.round(s / 60)} мин назад`;
    if (s < 86400 * 2) return `${Math.round(s / 3600)} ч назад`;
    return new Date(ts * 1000).toLocaleDateString();
  }
  function applyUpdateInfo(info) {
    if (!info) return;
    update.supervised = !!info.supervised;
    update.version = info.version || update.version;
    if (info.status) update.status = info.status;
    renderUpdate();
    showUpdateResult(info.result);
  }
  function renderUpdate() {
    const st = update.status, avail = !!(st && st.available);
    $("updateBadge").hidden = !avail;
    if (avail) {
      $("updateBadge").textContent = `Обновление (${st.behind})`;
      const head = st.commits && st.commits.length ? st.commits[0].sha : String(st.behind);
      if (store("jarvis.update.notified") !== head) {
        store("jarvis.update.notified", head);
        addSystem(`Доступно обновление Джарвиса: ${plural(st.behind, CHANGES)}. Нажмите «Обновление» вверху.`);
      }
    }
    if ($("updateDialog").open) renderUpdateDialog();
    renderUpdateDiag();
  }
  function versionText(v) { return v ? `${v.sha} от ${new Date(v.time * 1000).toLocaleString()} — ${v.subject}` : "неизвестна (не git-установка)"; }
  function renderUpdateDialog() {
    const st = update.status, body = $("updateBody");
    body.innerHTML = "";
    const p = (text, cls) => { const el = document.createElement("p"); if (cls) el.className = cls; el.textContent = text; body.appendChild(el); };
    p("Сейчас: " + versionText(update.version), "muted small");
    if (!st) p("Проверка ещё не выполнялась — нажмите «Проверить ещё раз».");
    else if (st.error) p("Не удалось проверить: " + st.error, "bad");
    else if (!st.available) p(`Установлена последняя версия (проверено ${ago(st.checked_at)}).` + (st.ahead ? ` Своих неотправленных коммитов: ${st.ahead}.` : ""));
    else {
      p(`Новых изменений: ${st.behind} (проверено ${ago(st.checked_at)})`);
      const ul = document.createElement("ul"); ul.className = "commit-list";
      st.commits.forEach(c => {
        const li = document.createElement("li");
        const code = document.createElement("code"); code.textContent = c.sha;
        const subj = document.createElement("span"); subj.textContent = c.subject;
        const when = document.createElement("span"); when.className = "muted"; when.textContent = ago(c.time);
        li.append(code, subj, when); ul.appendChild(li);
      });
      if (st.behind > st.commits.length) { const li = document.createElement("li"); li.className = "muted"; li.textContent = `… и ещё ${st.behind - st.commits.length}`; ul.appendChild(li); }
      body.appendChild(ul);
      if (st.needs_packages) p("Изменились зависимости — обновление доустановит пакеты, это займёт несколько минут.", "muted small");
      if (st.config_edited) p("Ваши правки config.toml будут перенесены в config.local.toml (он не перезаписывается обновлениями).", "muted small");
      if (st.blocked.length) p("Изменены файлы проекта: " + st.blocked.join(", ") + ". Закоммитьте или верните их — иначе обновление остановится.", "bad small");
      if (st.ahead) p(`Есть свои неотправленные коммиты (${st.ahead}) — история разошлась, объединить нужно вручную: git pull --rebase.`, "bad small");
    }
    if (!update.supervised) p("Кнопка перезапуска работает, когда Джарвис запущен через run_web.bat. Сейчас: закройте Джарвиса и запустите update.bat.", "muted small");
    $("updateApply").hidden = !(st && st.available);
    $("updateApply").disabled = !(update.supervised && st && st.can_apply);
  }
  function renderUpdateDiag() {
    const ul = $("updateDiag"); ul.innerHTML = "";
    const row = (cls, mk, text) => {
      const li = document.createElement("li"); li.className = cls;
      const m = document.createElement("span"); m.className = "mk"; m.textContent = mk;
      const t = document.createElement("span"); t.textContent = text;
      li.append(m, t); ul.appendChild(li);
    };
    row("ok", "✓", "Версия: " + versionText(update.version));
    const st = update.status;
    if (!st) row("warn", "!", "Обновления: ещё не проверялись");
    else if (st.error) row("warn", "!", "Обновления: не удалось проверить — " + st.error);
    else if (st.available) row("warn", "↑", `Обновления: доступно ${plural(st.behind, CHANGES)} (проверено ${ago(st.checked_at)})`);
    else row("ok", "✓", `Обновления: последняя версия (проверено ${ago(st.checked_at)})`);
    row(update.supervised ? "ok" : "warn", update.supervised ? "✓" : "!", update.supervised
      ? "Перезапуск после обновления: из HUD (запущено через run_web.bat)"
      : "Перезапуск после обновления: только вручную — update.bat (Джарвис запущен не через run_web.bat)");
  }
  function showUpdateResult(r) {
    if (!r || !r.at || Number(store("jarvis.update.shown") || 0) >= r.at) return;
    store("jarvis.update.shown", String(r.at));
    if (!r.ok) { addSystem("Обновление не выполнено: " + r.message, true); return; }
    const commits = r.commits || [];
    addSystem(commits.length
      ? `Джарвис обновлён (${r.from.sha} → ${r.to.sha}), ${plural(commits.length, CHANGES)}:\n` + commits.slice(0, 10).map(c => "• " + c.subject).join("\n")
      : r.message);
  }
  function openUpdate() { renderUpdateDialog(); $("updateDialog").showModal(); }
  $("updateBadge").onclick = openUpdate;
  $("updateOpen").onclick = openUpdate;
  $("updateClose").onclick = () => $("updateDialog").close();
  $("updateCheck").onclick = async () => {
    $("updateCheck").disabled = true;
    $("updateCheck").textContent = "Проверяю…";
    try { applyUpdateInfo(await api("/api/update/check", { method: "POST" })); }
    catch (e) { addSystem("Проверка обновлений: " + e.message, true); }
    $("updateCheck").disabled = false;
    $("updateCheck").textContent = "Проверить ещё раз";
    renderUpdateDialog();
  };
  $("updateApply").onclick = async () => {
    if (!confirm("Джарвис закроется, обновится и запустится снова — обычно это 10–60 секунд. Продолжить?")) return;
    try {
      const r = await fetch("/api/update/apply", { method: "POST", headers: { "Content-Type": "application/json" } });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || String(r.status));
      $("updateDialog").close();
    } catch (e) { alert("Обновление не запущено: " + e.message); }
  };

  // ---------- вкладки ----------
  document.querySelectorAll(".tab").forEach(t => t.onclick = () => {
    document.querySelectorAll(".tab").forEach(x => { x.classList.toggle("active", x === t); x.setAttribute("aria-selected", String(x === t)); });
    document.querySelectorAll(".panel").forEach(p => p.classList.toggle("active", p.id === "tab-" + t.dataset.tab));
    if (t.dataset.tab === "memory") loadMemory();
    if (t.dataset.tab === "schedule") loadSchedules();
    if (t.dataset.tab === "settings" || t.dataset.tab === "diag") refreshStatus();
    if (t.dataset.tab === "diag") { loadAudio(); loadAutostart(); orbInfo(); if (!levelTimer) pollLevel(); }
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
    loadBrief();
  }
  $("memRefresh").onclick = loadMemory;

  // ---------- компактная панель памяти: контекст задачи, заметки, решения, шаги ----------
  const STATUS_TEXT = { fact: "факт", assumption: "предположение", plan: "план", done: "сделано", superseded: "заменено" };
  const NOTE_TYPES = { project: "проект", goal: "цель", task: "задача", decision: "решение", check: "проверка", note: "заметка" };
  function noteLink(text, uri) {
    const a = el(uri ? "a" : "span", "", text);
    if (uri) { a.href = uri; a.title = "Открыть в Obsidian"; }
    return a;
  }
  function mbFill(listId, sumId, items, render, empty, summary) {
    const ul = $(listId); ul.innerHTML = "";
    items.forEach(it => ul.appendChild(render(it)));
    $(sumId).textContent = items.length ? summary(items) : empty;
    $(listId).closest("details").classList.toggle("empty", !items.length);
  }
  function noteRow(n) {
    const li = el("li");
    if (n.status) li.appendChild(el("span", "st " + n.status, STATUS_TEXT[n.status] || n.status));
    li.appendChild(noteLink(n.title, n.uri));
    if (n.date) li.appendChild(el("span", "muted", " · " + n.date));
    return li;
  }
  function renderMemoryEvent(ev, live) {
    if (!ev) return;
    const used = ev.used || [];
    mbFill("mbUsedList", "mbUsedSum", used, (u) => {
      const li = el("li");
      li.appendChild(el("span", "kind " + (u.kind || ""), u.label || u.kind));
      li.appendChild(noteLink(u.text, u.uri));
      return li;
    }, ev.note ? "не использовалась: " + ev.note.charAt(0).toLowerCase() + ev.note.slice(1) : "ничего подходящего в памяти",
    (items) => {
      const counts = {};
      items.forEach(u => { const k = (u.label || u.kind || "").split(" · ")[0]; counts[k] = (counts[k] || 0) + 1; });
      return `${items.length} в промпте: ` + Object.entries(counts).map(([k, n]) => n > 1 ? `${k} ×${n}` : k).join(", ");
    });
    mbFill("mbRelList", "mbRelSum", ev.related || [], noteRow, "по этому вопросу заметок нет",
      (items) => items[0].title + (items.length > 1 ? ` и ещё ${items.length - 1}` : ""));
    $("mbNote").textContent = ev.query ? "«" + (ev.query.length > 48 ? ev.query.slice(0, 48) + "…" : ev.query) + "»" : "";
    $("mbNote").title = ev.query || "";
    if (live) timelineAdd("route", "Память", used.length ? $("mbUsedSum").textContent : "в промпт ничего не добавлено", "", ev.ts);
  }
  function renderBrief(b) {
    const v = b.vault || {};
    $("mbOpen").hidden = !v.uri; if (v.uri) $("mbOpen").href = v.uri;
    $("vaultOpen").hidden = !v.uri; if (v.uri) $("vaultOpen").href = v.uri;
    mbFill("mbDecList", "mbDecSum", b.decisions || [], noteRow, v.enabled ? "решений пока нет" : "заметки Obsidian выключены",
      (items) => items[0].title + (items.length > 1 ? ` и ещё ${items.length - 1}` : ""));
    mbFill("mbStepsList", "mbStepsSum", b.steps || [], noteRow, v.enabled ? "запланированных шагов нет" : "—",
      (items) => items[0].title + (items.length > 1 ? ` и ещё ${items.length - 1}` : ""));
    if (b.last) renderMemoryEvent(b.last, false);
    const ul = $("vaultInfo"); ul.innerHTML = "";
    const row = (ok, text) => { const li = el("li", ok ? "ok" : "warn"); li.append(el("span", "mk", ok ? "✓" : "!"), el("span", "", text)); ul.appendChild(li); };
    if (!v.enabled) row(false, "Заметки Obsidian выключены (config.toml → [memory.vault] enabled).");
    else {
      row(!v.error, v.error ? "Папка недоступна: " + v.error : "Папка: " + v.path);
      const c = v.counts || {};
      row(true, ["project", "goal", "task", "decision", "check", "note"].filter(k => c[k])
        .map(k => `${NOTE_TYPES[k]}: ${c[k]}`).join(" · ") || "Заметок пока нет — скажите «запиши решение: …».");
    }
  }
  async function loadBrief() {
    try { renderBrief(await api("/api/memory/brief")); } catch (e) { /* сервер перезапускается */ }
  }
  $("vaultSync").onclick = async () => {
    $("vaultSync").disabled = true;
    try {
      const r = await fetch("/api/memory/vault/sync", { method: "POST", headers: { "Content-Type": "application/json" } });
      const d = await r.json().catch(() => ({}));
      if (!r.ok || !d.ok) throw new Error(d.error || (d.vault && d.vault.error) || String(r.status));
      addSystem(`Указатель памяти пересобран: заметок ${d.stats.notes}, обновлено файлов ${d.stats.mirror + d.stats.indexes}.`);
    } catch (e) { addSystem("Указатель не пересобран: " + e.message, true); }
    $("vaultSync").disabled = false;
    loadBrief();
  };
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
      const gem = s.provider.gemini || {};
      const cloud = s.provider.ollama_cloud || {};
      const participants = (s.provider.team || {}).members || [];
      const extra = key === "team"
        ? " " + participants.filter(m => m.enabled).map(m => m.title + ": " + m.note).join("; ") + "."
        : key === "codex"
        ? ` ${(s.provider.codex || {}).note || "Нужен вход codex login"}.`
        : key === "ollama_cloud"
        ? (cloud.available ? ` Модель: ${cloud.model}. Ключ найден.` : ` Недоступно: ${cloud.note}.`)
        : key === "gemini"
        ? (gem.available ? ` Модель: ${gem.model}. Ключ найден.` : ` Недоступно: ${gem.note}.`)
        : key === "claude"
        ? (s.provider.claude.available ? ` Модель: ${s.provider.claude.model || "по умолчанию"}. ${s.provider.claude.note}.` : ` Недоступно: ${s.provider.claude.note}.`)
        : key === "hybrid"
          ? ` Короткие вопросы: Ollama (${s.provider.ollama.model}); сложные и актуальные: Claude. ` +
            (s.provider.claude.available ? s.provider.claude.note + "." : `Claude недоступен: ${s.provider.claude.note}. Все ответы останутся локальными.`) +
            (s.ollama_ready === false ? " Ollama не отвечает или модель не скачана." : "")
          : ` Модель: ${s.provider.ollama.model}.` + (s.ollama_ready === false ? " Ollama не отвечает или модель не скачана." : "");
      t.innerHTML = `<div><b></b></div><div class="pnote"></div>`;
      t.querySelector("b").textContent = {team: "Команда ИИ", codex: "ChatGPT (Codex)", ollama_cloud: "Ollama Cloud",
        claude: "Claude (Agent SDK)", gemini: "Gemini (Google)", hybrid: "Гибрид (Ollama + Claude)", ollama: "Ollama"}[key] || key;
      t.querySelector(".pnote").textContent = note + "." + extra;
      d.append(r, t); pv.appendChild(d);
    });
    const claude = document.querySelector('#modeSwitch [data-provider="claude"]');
    claude.disabled = !s.provider.claude.available && s.provider.active !== "claude";
    claude.title = s.provider.claude.available ? "Все вопросы к модели — Claude (облако Anthropic)"
      : "Claude недоступен: " + s.provider.claude.note;
    const gemini = document.querySelector('#modeSwitch [data-provider="gemini"]');
    if (gemini && s.provider.gemini) {
      gemini.disabled = !s.provider.gemini.available && s.provider.active !== "gemini";
      gemini.title = s.provider.gemini.available ? "Все вопросы к модели — Gemini (облако Google)"
        : "Gemini недоступен: " + s.provider.gemini.note;
    }
    for (const key of ["codex", "ollama_cloud"]) {
      const button = document.querySelector(`#modeSwitch [data-provider="${key}"]`);
      const status = s.provider[key];
      if (button && status) {
        button.disabled = !status.available && s.provider.active !== key;
        if (!status.available) button.title = status.note;
      }
    }
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
    applyUpdateInfo(s.update);
  }
  $("diagRefresh").onclick = () => { refreshStatus(); loadAudio(); loadAutostart(); };

  // ---------- запуск при входе в Windows ----------
  const AUTOSTART_NOTE = "Джарвис запустится с голосом и значком в трее; HUD открывается щелчком по значку.";
  function showAutostart(a) {
    const box = $("autostartToggle");
    box.disabled = !a.supported;
    box.checked = !!a.enabled;
    $("autostartNote").textContent = a.supported ? AUTOSTART_NOTE : "Доступно только в Windows: " + (a.error || "");
  }
  async function loadAutostart() {
    try { showAutostart(await api("/api/autostart")); } catch (e) { /* сервер перезапускается */ }
  }
  $("autostartToggle").onchange = async () => {
    const box = $("autostartToggle");
    box.disabled = true;
    try {
      const r = await fetch("/api/autostart", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ on: box.checked }) });
      const d = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(d.error || String(r.status));
      showAutostart(d);
      addSystem(d.enabled ? "Автозапуск включён: Джарвис будет запускаться при входе в Windows." : "Автозапуск выключен.");
    } catch (e) {
      addSystem("Автозапуск не изменён: " + e.message, true);
      loadAutostart();
    }
  };

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

  applyTheme(document.documentElement.dataset.theme === "blue" ? "blue" : "red");
  tick();
  setInterval(tick, 5000);
  addSystem("Например: «Открой YouTube и найди видео про локальных ИИ-ассистентов» или «Найди в интернете, что такое MCP, и сравни два источника».");
  startOrb();
  setState("idle");
  // Для проверок в браузере (scripts/hud_smoke.py): события подаются тем же путём, что из WebSocket.
  window.JarvisHUD = { handle, get orb() { return orb; } };
  connect();
  pollSystem();
})();
