// Небольшой безопасный рендер markdown для ответов модели.
// Сначала всё экранируется, затем добавляются только известные теги; ссылки — только http(s).
(function () {
  "use strict";

  function esc(s) {
    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  function inline(s) {
    const codes = [];
    s = s.replace(/`([^`]+)`/g, (_, c) => { codes.push(c); return "\u0000" + (codes.length - 1) + "\u0000"; });
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
      (_, t, u) => `<a href="${u}" target="_blank" rel="noopener noreferrer">${t}</a>`);
    s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+[^\s<).,;:!?])/g,
      (_, p, u) => `${p}<a href="${u}" target="_blank" rel="noopener noreferrer">${u}</a>`);
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*\s][^*]*)\*/g, "$1<em>$2</em>");
    return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[+i]}</code>`);
  }

  function table(lines) {
    const rows = lines.filter(l => !/^\s*\|?\s*:?-{2,}/.test(l))
      .map(l => l.trim().replace(/^\||\|$/g, "").split("|").map(c => inline(c.trim())));
    if (!rows.length) return "";
    const head = rows[0].map(c => `<th>${c}</th>`).join("");
    const body = rows.slice(1).map(r => "<tr>" + r.map(c => `<td>${c}</td>`).join("") + "</tr>").join("");
    return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  }

  function render(text) {
    const lines = esc(String(text || "")).split(/\r?\n/);
    const out = [];
    let para = [], list = null, tbl = [], code = null;
    const flushPara = () => { if (para.length) { out.push("<p>" + inline(para.join("<br>")) + "</p>"); para = []; } };
    const flushList = () => { if (list) { out.push(`<${list.tag}>` + list.items.map(i => `<li>${inline(i)}</li>`).join("") + `</${list.tag}>`); list = null; } };
    const flushTable = () => { if (tbl.length) { out.push(table(tbl)); tbl = []; } };
    for (const raw of lines) {
      // Блок кода ```…```: строки как есть (уже экранированы), без разметки внутри.
      if (/^\s*```/.test(raw)) {
        if (code === null) { flushPara(); flushList(); flushTable(); code = []; }
        else { out.push("<pre><code>" + code.join("\n") + "</code></pre>"); code = null; }
        continue;
      }
      if (code !== null) { code.push(raw); continue; }
      const line = raw.trimEnd();
      if (/^\s*\|.*\|\s*$/.test(line)) { flushPara(); flushList(); tbl.push(line); continue; }
      flushTable();
      let m;
      if (!line.trim()) { flushPara(); flushList(); continue; }
      if ((m = line.match(/^(#{1,3})\s+(.*)$/))) { flushPara(); flushList(); out.push(`<h${m[1].length}>${inline(m[2])}</h${m[1].length}>`); continue; }
      if ((m = line.match(/^\s*[-*•]\s+(.*)$/))) { flushPara(); if (!list || list.tag !== "ul") { flushList(); list = { tag: "ul", items: [] }; } list.items.push(m[1]); continue; }
      if ((m = line.match(/^\s*\d+[.)]\s+(.*)$/))) { flushPara(); if (!list || list.tag !== "ol") { flushList(); list = { tag: "ol", items: [] }; } list.items.push(m[1]); continue; }
      flushList();
      para.push(line);
    }
    if (code !== null) out.push("<pre><code>" + code.join("\n") + "</code></pre>");  // не закрыт — всё равно код
    flushPara(); flushList(); flushTable();
    return out.join("");
  }

  window.JarvisMarkdown = { render, esc };
})();
