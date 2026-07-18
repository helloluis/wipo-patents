// Floating research assistant — streams from /api/chat (server proxies Fireworks/Qwen), renders
// markdown with copy-paste-ready R/Stata code. History persists in localStorage (per browser) and
// renders paginated (recent messages first, "load earlier" pages back). No secrets in the browser.
(function () {
  const $ = s => document.querySelector(s);
  const asst = $("#asst"), log = $("#asst-log"), form = $("#asst-form"), text = $("#asst-text");
  const KEY = "wipo_chat_v1", PAGE = 16, MAX_STORE = 500, CTX = 12;
  let history = [];   // full conversation [{role, content}]
  let shown = 0;      // how many messages (from the end) are currently rendered
  let busy = false, opened = false;

  marked.setOptions({ breaks: true, gfm: true });
  const renderMd = md => DOMPurify.sanitize(marked.parse(md || ""));

  const save = () => { try { localStorage.setItem(KEY, JSON.stringify(history.slice(-MAX_STORE))); } catch (e) {} };
  const loadStore = () => { try { history = JSON.parse(localStorage.getItem(KEY) || "[]") || []; } catch (e) { history = []; } };

  function addCopyButtons(el) {
    el.querySelectorAll("pre").forEach(pre => {
      if (pre.querySelector(".code-copy")) return;
      const btn = document.createElement("button");
      btn.className = "code-copy"; btn.type = "button"; btn.textContent = "Copy";
      btn.onclick = () => navigator.clipboard.writeText(pre.innerText.replace(/\nCopy$/, "")).then(() => {
        btn.textContent = "Copied ✓"; setTimeout(() => (btn.textContent = "Copy"), 1400);
      });
      pre.appendChild(btn);
    });
  }

  function appendBubble(cls, md, scroll = true) {
    const div = document.createElement("div");
    div.className = "asst-msg " + cls;
    div.innerHTML = renderMd(md); addCopyButtons(div);
    log.appendChild(div);
    if (scroll) log.scrollTop = log.scrollHeight;
    return div;
  }

  function setMd(el, md) { el.innerHTML = renderMd(md); addCopyButtons(el); log.scrollTop = log.scrollHeight; }

  // If the assistant's reply contains a ```csv file spec, POST it to the server to generate the
  // file, then swap the block for a status note (the file appears in the Downloads section).
  async function handleCsvBlock(md, bot) {
    const m = md.match(/```csv\s*\n([\s\S]+?)```/);
    if (!m) return md;
    let note, spec;
    try { spec = JSON.parse(m[1]); } catch (e) { spec = null; }
    if (!spec) {
      note = "⚠️ Couldn't create the file (malformed file spec) — try rephrasing the request.";
    } else {
      try {
        const r = await fetch("/api/downloads", {
          method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(spec)
        });
        const d = await r.json().catch(() => ({}));
        if (r.ok) {
          window.refreshDownloads && window.refreshDownloads();
          note = `📄 **${d.name}** is ready — ${(d.rows ?? 0).toLocaleString()} rows. ` +
            `It's in the **Downloads** section at the bottom of the page (or [download it now](/api/downloads/${d.id}/file)).`;
        } else {
          note = `⚠️ Couldn't create the file: ${d.error || ("error " + r.status)}`;
        }
      } catch (e) {
        note = "⚠️ Couldn't create the file — connection error.";
      }
    }
    const final = md.replace(m[0], note);
    setMd(bot, final);
    return final;
  }

  // paginated render: show the most-recent `shown` messages; a "load earlier" button pages back
  function renderWindow(toBottom = true) {
    log.innerHTML = "";
    const start = Math.max(0, history.length - shown);
    if (start > 0) {
      const b = document.createElement("button");
      b.className = "asst-earlier"; b.type = "button";
      b.textContent = "↑ Load earlier messages";
      b.onclick = () => { shown = Math.min(history.length, shown + PAGE); renderWindow(false); };
      log.appendChild(b);
    }
    for (let i = start; i < history.length; i++)
      appendBubble(history[i].role === "user" ? "me" : "bot", history[i].content, false);
    log.scrollTop = toBottom ? log.scrollHeight : 0;
  }

  function greet() {
    appendBubble("bot", "Hi Jannie 👋 I'm your research assistant. I know this patent database and can help you:\n\n" +
      "- get **R or Stata** connection code + queries\n" +
      "- figure out **how to view or pull** specific data\n" +
      "- create **downloadable CSV files** (they land in the Downloads section below)\n" +
      "- use the **filters** on this page\n\n" +
      "Try: *\"Give me R code to plot AI patents (IPC G06N) per year for China vs the US.\"*");
  }

  function openChat() {
    asst.classList.remove("collapsed");
    if (!opened) {
      opened = true;
      loadStore();
      if (history.length) { shown = Math.min(history.length, PAGE); renderWindow(); }
      else greet();
    }
    text.focus();
  }

  async function send(q) {
    if (busy || !q.trim()) return;
    busy = true; $("#asst-send").disabled = true;
    history.push({ role: "user", content: q }); shown++; save();
    appendBubble("me", q);
    const bot = appendBubble("bot", "");
    bot.innerHTML = "<span class='asst-typing'>…</span>";
    let acc = "";
    try {
      const r = await fetch("/api/chat", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ messages: history.slice(-CTX).map(m => ({ role: m.role, content: m.content })) })
      });
      if (!r.ok) {
        const e = await r.json().catch(() => ({}));
        setMd(bot, "⚠️ " + (e.error || ("Error " + r.status)));
      } else {
        const reader = r.body.getReader(), dec = new TextDecoder();
        for (;;) {
          const { value, done } = await reader.read();
          if (done) break;
          acc += dec.decode(value, { stream: true });
          setMd(bot, acc);
        }
        history.push({ role: "assistant", content: await handleCsvBlock(acc, bot) }); shown++; save();
      }
    } catch (err) { setMd(bot, "⚠️ Connection error. Please try again."); }
    busy = false; $("#asst-send").disabled = false; text.focus();
  }

  function clearAll() {
    if (busy) return;
    history = []; shown = 0; save();
    log.innerHTML = ""; greet();
  }

  // events
  $("#asst-fab").onclick = openChat;
  $("#asst-min").onclick = () => asst.classList.add("collapsed");
  $("#asst-clear").onclick = clearAll;
  $("#asst-max").onclick = () => {
    const on = asst.classList.toggle("max");
    const b = $("#asst-max");
    b.textContent = on ? "⤡" : "⤢";
    b.title = on ? "Restore width" : "Maximize";
  };
  form.addEventListener("submit", e => { e.preventDefault(); const q = text.value; text.value = ""; text.style.height = "auto"; send(q); });
  text.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); form.requestSubmit(); } });
  text.addEventListener("input", () => { text.style.height = "auto"; text.style.height = Math.min(text.scrollHeight, 120) + "px"; });
})();
