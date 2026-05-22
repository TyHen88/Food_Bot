/* Shared helpers for the Food Bot Mini App. */
(function () {
  // Inject the role-gate CSS as early as we can. Combined with each page
  // setting <html data-role="loading"> in its markup, this guarantees that
  // .admin-only / .member-only elements stay hidden until /api/me resolves —
  // so members never see Settings briefly flash on page load.
  (function injectRoleGate() {
    const style = document.createElement("style");
    style.id = "role-gate";
    style.textContent =
      '[data-role="loading"] .admin-only,' +
      '[data-role="loading"] .member-only,' +
      '[data-role="member"]  .admin-only,' +
      '[data-role="admin"]   .member-only { display: none !important; }';
    document.head.appendChild(style);
  })();

  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
  }

  // Use Telegram's BackButton to navigate to /index.html on every page except index itself.
  const isIndex = location.pathname === "/" || location.pathname.endsWith("/index.html");
  if (tg && tg.BackButton) {
    if (isIndex) {
      tg.BackButton.hide();
    } else {
      tg.BackButton.show();
      tg.BackButton.onClick(() => { location.href = "/"; });
    }
  }

  window.FoodBot = {
    tg,
    initData: tg ? tg.initData : "",
    user: tg && tg.initDataUnsafe ? tg.initDataUnsafe.user : null,

    /** Filled by /api/me on every page load — drives admin-only UI hiding. */
    isAdmin: false,

    /** Fetch wrapper that attaches the init-data header and parses JSON. */
    async api(path, options = {}) {
      const headers = Object.assign(
        { "Content-Type": "application/json" },
        options.headers || {},
        { "X-Telegram-Init-Data": this.initData },
      );
      const resp = await fetch(`/api${path}`, { ...options, headers });
      if (!resp.ok) {
        let detail = `HTTP ${resp.status}`;
        try {
          const body = await resp.json();
          if (body && body.detail) detail = body.detail;
        } catch (_) { /* ignore */ }
        throw new Error(detail);
      }
      if (resp.status === 204) return null;
      return resp.json();
    },

    /** Briefly show a toast at the bottom of the screen. */
    toast(message, kind = "info") {
      let el = document.getElementById("status-banner");
      if (!el) {
        el = document.createElement("div");
        el.id = "status-banner";
        document.body.appendChild(el);
      }
      el.textContent = message;
      el.className = `show ${kind === "error" ? "error" : ""}`;
      if (tg && tg.HapticFeedback) {
        tg.HapticFeedback.notificationOccurred(kind === "error" ? "error" : "success");
      }
      clearTimeout(this._toastTimer);
      this._toastTimer = setTimeout(() => {
        el.className = "";
      }, 2200);
    },

    /** Tiny DOM helper. */
    el(tag, attrs = {}, children = []) {
      const e = document.createElement(tag);
      for (const [k, v] of Object.entries(attrs)) {
        if (k === "class") e.className = v;
        else if (k === "html") e.innerHTML = v;
        else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
        else e.setAttribute(k, v);
      }
      for (const child of [].concat(children)) {
        if (child == null) continue;
        e.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
      }
      return e;
    },
  };

  /**
   * Ask the bot who we are. Used to hide admin-only UI for members.
   * Silently falls back to non-admin if the call fails.
   */
  async function loadAuth() {
    try {
      const me = await window.FoodBot.api("/me");
      window.FoodBot.isAdmin = !!(me && me.is_admin);
    } catch (_) {
      window.FoodBot.isAdmin = false;
    }
    applyRoleVisibility();
  }

  /** Flip the document role so the gate CSS picks the right visibility.
   *  Switching the data-role attribute is atomic — no per-element flicker. */
  function applyRoleVisibility() {
    const isAdmin = !!window.FoodBot.isAdmin;
    document.documentElement.dataset.role = isAdmin ? "admin" : "member";
    document.body.classList.toggle("is-admin", isAdmin);
    document.body.classList.toggle("is-member", !isAdmin);
  }
  window.FoodBot.applyRoleVisibility = applyRoleVisibility;

  // Each page defines a `window.pageInit` AFTER this script tag.
  // Wait for the rest of the body to parse, fetch auth, then call it.
  async function runPageInit() {
    await loadAuth();
    if (typeof window.pageInit === "function") {
      try {
        await window.pageInit();
      } catch (err) {
        window.FoodBot.toast(err.message || String(err), "error");
      }
    }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", runPageInit);
  } else {
    setTimeout(runPageInit, 0);
  }
})();
