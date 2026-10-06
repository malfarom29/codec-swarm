// Dashboard behaviour htmx doesn't cover: terminals that stay at the bottom, and sounds for what needs me.
(() => {
  // --- terminals: live updates replace the HTML, so keep each one pinned to its end unless I scrolled up ---
  const keyOf = (t) => `${t.closest("[data-lane]")?.dataset.lane}/${t.closest("[data-role]")?.dataset.role}`;
  let saved = {};
  const remember = (root) => {
    saved = {};
    root.querySelectorAll("[data-terminal]").forEach((t) => {
      saved[keyOf(t)] = { top: t.scrollTop, atEnd: t.scrollHeight - t.scrollTop - t.clientHeight < 24 };
    });
  };
  const restore = (root) => {
    root.querySelectorAll("[data-terminal]").forEach((t) => {
      const s = saved[keyOf(t)];
      t.scrollTop = !s || s.atEnd ? t.scrollHeight : s.top;
    });
  };
  document.addEventListener("htmx:beforeSwap", (e) => remember(e.detail.target));
  // Right after the swap Alpine hasn't re-shown the open tab, so a terminal has no height yet; by settle it has.
  // Timers, not animation frames: a background tab pauses frames and would lose the pin across several updates.
  document.addEventListener("htmx:afterSettle", (e) => { restore(e.detail.target); setTimeout(() => restore(e.detail.target), 50); });
  document.addEventListener("DOMContentLoaded", () => restore(document));
  // A terminal in a hidden tab has no height until the tab opens; pin it then.
  document.addEventListener("click", (e) => { if (e.target.closest("[role=tab]")) setTimeout(() => restore(document), 0); });

  // --- sounds ------------------------------------------------------------------------------------------
  const store = { get: (k) => { try { return localStorage.getItem(k); } catch { return null; } },
                  set: (k, v) => { try { localStorage.setItem(k, v); } catch { /* private mode: not remembered */ } } };
  let on = store.get("codec.sound") === "on";
  let audio = null;
  const toggle = document.getElementById("sound-toggle");
  const paint = () => { if (toggle) { toggle.textContent = on ? "Sound on" : "Sound off"; toggle.setAttribute("aria-pressed", String(on)); toggle.classList.toggle("on", on); } };
  const unlock = () => { if (!audio) { try { audio = new (window.AudioContext || window.webkitAudioContext)(); } catch { audio = null; } } if (audio?.state === "suspended") audio.resume(); };
  // Browsers only allow sound after a click on the page, so any click arms it.
  document.addEventListener("pointerdown", () => { if (on) unlock(); }, { once: false, passive: true });

  const tones = { input: [[660, 0], [880, 0.16]], done: [[523, 0], [659, 0.11], [784, 0.22], [1047, 0.33]] };
  const play = (kind) => {
    if (!on || !audio) return;
    const t0 = audio.currentTime + 0.02;
    for (const [freq, at] of tones[kind] || tones.input) {
      const osc = audio.createOscillator(), gain = audio.createGain();
      osc.type = "square"; osc.frequency.value = freq;
      gain.gain.setValueAtTime(0.0001, t0 + at);
      gain.gain.exponentialRampToValueAtTime(0.06, t0 + at + 0.01);
      gain.gain.exponentialRampToValueAtTime(0.0001, t0 + at + 0.13);
      osc.connect(gain).connect(audio.destination);
      osc.start(t0 + at); osc.stop(t0 + at + 0.15);
    }
  };
  const notify = (note) => {
    play(note.kind);
    if (on && document.hidden && "Notification" in window && Notification.permission === "granted") {
      const n = new Notification(`codec-swarm · ${note.title}`, { body: note.body, icon: "/static/logo.svg", tag: `${note.title}:${note.kind}` });
      n.onclick = () => { window.focus(); location.href = note.url; };
    }
  };
  toggle?.addEventListener("click", () => {
    on = !on; store.set("codec.sound", on ? "on" : "off"); paint();
    if (on) { unlock(); play("input"); if ("Notification" in window && Notification.permission === "default") Notification.requestPermission(); }
  });
  paint();

  const since = document.currentScript?.dataset.since || "0";
  try {
    const feed = new EventSource(`/events/stream?since=${since}`);
    feed.addEventListener("notify", (e) => { try { notify(JSON.parse(e.data)); } catch { /* a malformed note is skipped */ } });
    // The page's only live connection: htmx refreshes whatever listens for codec:change.
    feed.addEventListener("change", () => window.htmx?.trigger(document.body, "codec:change"));
  } catch { /* no live notifications without EventSource */ }
})();
