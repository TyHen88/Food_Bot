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

    // Apply the last-known role synchronously, before /api/me resolves, so
    // admin-only nav items (Templates, Settings) render in their final state
    // immediately. Without this the bottom nav loads with the items hidden,
    // then reflows to show them once /api/me returns — which looks like the
    // tab bar flickering on every navigation. loadAuth() re-confirms below
    // and corrects the attribute if the role actually changed; server-side
    // require_admin still gates every admin endpoint regardless.
    try {
      const cached = localStorage.getItem("fb_role");
      if (cached === "admin" || cached === "member") {
        document.documentElement.dataset.role = cached;
      }
    } catch (_) { /* localStorage unavailable — fall back to the loading gate */ }
  })();

  // Full-screen loading overlay shown on every page until its data finishes
  // fetching. Injected here (shared) so all tabs get it without per-page markup;
  // runPageInit() hides it once loadAuth + the page's pageInit resolve.
  (function injectLoading() {
    const style = document.createElement("style");
    style.id = "fb-loading-style";
    style.textContent =
      ".fb-loading{position:fixed;top:0;bottom:0;left:50%;transform:translateX(-50%);" +
      "width:100%;max-width:var(--frame-w,430px);display:flex;align-items:center;" +
      "justify-content:center;background:var(--bg,#F7F5F0);z-index:9999;" +
      "transition:opacity .25s ease}" +
      ".fb-loading.hide{opacity:0;pointer-events:none}" +
      ".fb-spinner{width:34px;height:34px;border-radius:50%;" +
      "border:3px solid var(--border,rgba(0,0,0,.12));border-top-color:var(--accent,#2D6A4F);" +
      "animation:fb-spin .7s linear infinite}" +
      "@keyframes fb-spin{to{transform:rotate(360deg)}}" +
      // Slim top progress bar shown whenever an /api request is in flight.
      ".fb-bar{position:fixed;top:0;left:50%;transform:translateX(-50%);" +
      "width:100%;max-width:var(--frame-w,430px);height:3px;overflow:hidden;" +
      "z-index:10000;opacity:0;transition:opacity .3s ease;pointer-events:none}" +
      ".fb-bar.active{opacity:1}" +
      ".fb-bar::before{content:'';position:absolute;top:0;height:100%;width:40%;" +
      "background:var(--accent,#2D6A4F);animation:fb-slide 1.1s infinite ease-in-out}" +
      "@keyframes fb-slide{0%{left:-40%}100%{left:100%}}";
    document.head.appendChild(style);

    const mount = () => {
      if (document.getElementById("fb-loading")) return;
      const overlay = document.createElement("div");
      overlay.id = "fb-loading";
      overlay.className = "fb-loading";
      overlay.innerHTML = '<div class="fb-spinner"></div>';
      document.body.appendChild(overlay);

      const bar = document.createElement("div");
      bar.id = "fb-bar";
      bar.className = "fb-bar";
      document.body.appendChild(bar);
    };
    if (document.body) mount();
    else document.addEventListener("DOMContentLoaded", mount);
  })();

  function hideLoading() {
    const el = document.getElementById("fb-loading");
    if (el) el.classList.add("hide");
  }

  // Top progress bar driven by in-flight /api requests, so every data fetch
  // (initial load and later refreshes/saves) shows a loading indicator.
  let _pending = 0;
  function bumpLoading(delta) {
    _pending = Math.max(0, _pending + delta);
    const bar = document.getElementById("fb-bar");
    if (bar) bar.classList.toggle("active", _pending > 0);
  }

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
      bumpLoading(1);
      let resp;
      try {
        resp = await fetch(`/api${path}`, { ...options, headers });
      } finally {
        bumpLoading(-1);
      }
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
      // Cache for the next page load's synchronous role gate (see injectRoleGate).
      try {
        localStorage.setItem("fb_role", window.FoodBot.isAdmin ? "admin" : "member");
      } catch (_) { /* ignore */ }
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
    try {
      await loadAuth();
      if (typeof window.pageInit === "function") {
        await window.pageInit();
      }
    } catch (err) {
      window.FoodBot.toast(err.message || String(err), "error");
    } finally {
      hideLoading();
    }
  }
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", runPageInit);
  } else {
    setTimeout(runPageInit, 0);
  }
})();
