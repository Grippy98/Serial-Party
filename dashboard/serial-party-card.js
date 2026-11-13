/* Serial Party observer. Uses HA ingress; never opens a writable terminal. */
(() => {
  "use strict";
  class SerialPartyCard extends HTMLElement {
    constructor() {
      super();
      this.attachShadow({ mode: "open" });
      this.shadowRoot.innerHTML = `
        <style>
          :host{display:block;height:100%;min-height:260px}
          ha-card{display:flex;flex-direction:column;height:360px;min-height:260px;overflow:hidden;background:#1b1f25;color:#f1f5f9}
          header{display:flex;align-items:center;gap:10px;min-height:44px;padding:8px 12px;border-bottom:1px solid #38424e;font:14px/1.4 system-ui,sans-serif}
          h2{font:600 15px/1.4 system-ui,sans-serif;margin:0}
          .badge{color:#98ddba;background:#173e2d;border-radius:12px;padding:3px 7px;font-size:11px;white-space:nowrap}
          a{margin-left:auto;color:#8bd7f7;font-size:12px;text-decoration:none;white-space:nowrap}
          a:hover{text-decoration:underline}
          button{border:0;background:transparent;color:#c6e9f7;font:24px/1 system-ui;min-width:32px;min-height:32px;cursor:pointer;padding:3px;border-radius:4px}
          a:focus-visible,button:focus-visible{outline:2px solid #8bd7f7;outline-offset:3px}
          #content{flex:1;min-height:0;position:relative}
          iframe{display:block;width:100%;height:100%;border:0;background:#1b1f25}
          #status{box-sizing:border-box;height:100%;display:flex;align-items:center;padding:24px;color:#cbd5e1;font:14px/1.6 system-ui,sans-serif}
          #status[hidden]{display:none}
          @media(max-width:400px){header{gap:7px;padding-inline:10px}a{font-size:11px}}
        </style>
        <ha-card id="card">
          <header><h2 id="title">Serial log</h2><span class="badge">Read only</span>
            <a id="controls">Open controls</a>
            <button id="refresh" type="button" aria-label="Reconnect serial monitor" title="Reconnect serial monitor">↻</button>
          </header>
          <div id="content"><div id="status" role="status">Connecting to Home Assistant…</div></div>
        </ha-card>`;
      this._content = this.shadowRoot.querySelector("#content");
      this._status = this.shadowRoot.querySelector("#status");
      this._generation = 0;
      this._mounted = false;
      this._pageVisible = true;
      this._active = false;
      this._attempted = false;
      this._refreshing = false;
      this._session = null;
      this._frame = null;
      this._timer = null;
      this._onVisibility = () => this._reconcile();
      this._onPageHide = () => { this._pageVisible = false; this._reconcile(); };
      this._onPageShow = () => { this._pageVisible = true; this._reconcile(); };
      this.shadowRoot.querySelector("#refresh").addEventListener("click", () => {
        this._stop();
        this._reconcile();
      });
    }

    setConfig(config) {
      if (!config || typeof config.app_slug !== "string" || !/^[a-z0-9][a-z0-9_.-]{0,127}$/.test(config.app_slug)) {
        throw new Error("app_slug is required; use the installed Home Assistant app slug.");
      }
      if (typeof config.board !== "string" || !/^[a-z][a-z0-9_-]{0,31}$/.test(config.board)) {
        throw new Error("board is required and must match a configured board ID.");
      }
      const title = config.title ?? `Serial log · ${config.board}`;
      const height = config.height ?? 360;
      const fontSize = config.font_size ?? 12;
      if (typeof title !== "string" || !title.trim() || title.length > 120) throw new Error("title must contain 1 to 120 characters.");
      if (!Number.isInteger(height) || height < 260 || height > 1200) throw new Error("height must be an integer from 260 to 1200.");
      if (!Number.isInteger(fontSize) || fontSize < 8 || fontSize > 32) throw new Error("font_size must be an integer from 8 to 32.");
      const next = { app_slug: config.app_slug, board: config.board, title: title.trim(), height, font_size: fontSize };
      if (JSON.stringify(next) === JSON.stringify(this._config)) return;
      const changedApp = this._config?.app_slug !== next.app_slug;
      this._stop();
      if (changedApp) this._session = null;
      this._config = next;
      this._configured = true;
      this.shadowRoot.querySelector("#title").textContent = next.title;
      this.shadowRoot.querySelector("#controls").href = `/app/${encodeURIComponent(next.app_slug)}`;
      this.shadowRoot.querySelector("#card").style.height = `${next.height}px`;
      this._reconcile();
    }
    getCardSize() { return Math.ceil((this._config?.height ?? 360) / 50); }
    getGridOptions() { return { columns: 12, rows: Math.ceil(((this._config?.height ?? 360) + 8) / 64), min_rows: 4, min_columns: 6 }; }

    set hass(value) {
      const changedUser = this._hass?.user?.id !== value?.user?.id;
      this._hass = value;
      if (changedUser) { this._stop(); this._session = null; }
      this._reconcile();
    }

    connectedCallback() {
      this._mounted = true;
      this._pageVisible = true;
      document.addEventListener("visibilitychange", this._onVisibility);
      window.addEventListener("pagehide", this._onPageHide);
      window.addEventListener("pageshow", this._onPageShow);
      document.addEventListener("resume", this._onVisibility);
      this._reconcile();
    }

    disconnectedCallback() {
      this._mounted = false;
      this._stop();
      this._session = null;
      document.removeEventListener("visibilitychange", this._onVisibility);
      document.removeEventListener("resume", this._onVisibility);
      window.removeEventListener("pagehide", this._onPageHide);
      window.removeEventListener("pageshow", this._onPageShow);
      // Do not delete the shared ingress cookie: other app views may use it.
    }

    _stop() {
      this._generation += 1;
      this._active = false;
      this._attempted = false;
      this._refreshing = false;
      if (this._timer !== null) window.clearInterval(this._timer);
      this._timer = null;
      if (this._frame) {
        this._frame.src = "about:blank";
        this._frame.remove();
        this._frame = null;
      }
    }

    _message(text) {
      this._status.textContent = text;
      this._status.hidden = false;
    }

    _reconcile() {
      if (!this._mounted || !this._configured) return;
      if (!this._pageVisible || document.hidden) { if (this._active) this._stop(); return; }
      if (!this._hass || this._hass.connected === false) {
        if (this._active) this._stop();
        this._message("Home Assistant is disconnected. The monitor will resume when it reconnects.");
        return;
      }
      if (!this._hass.user?.is_admin) {
        if (this._active) this._stop();
        this._message("An administrator account is required to view this serial monitor.");
        return;
      }
      if (!this._active) { this._active = true; this._attempted = false; }
      if (!this._attempted) { this._attempted = true; this._connect(); }
    }

    _current(generation) {
      return this._mounted && this._active && generation === this._generation && !document.hidden && this._pageVisible;
    }

    _cookie(session) {
      // This matches native HA ingress. Never store tokens in card config/storage.
      document.cookie = `ingress_session=${session};path=/api/hassio_ingress/;SameSite=Strict${location.protocol === "https:" ? ";Secure" : ""}`;
    }

    async _ensureSession(generation) {
      let session = this._session;
      if (session) {
        try { await this._hass.callWS({ type: "supervisor/ingress/validate_session", session }); }
        catch (_) { session = null; }
        if (!this._current(generation)) return null;
      }
      if (!session) {
        const result = await this._hass.callWS({ type: "supervisor/ingress/session", slug: this._config.app_slug });
        if (!this._current(generation)) return null;
        session = result.session;
      }
      if (typeof session !== "string" || !/^[A-Za-z0-9_-]+$/.test(session)) throw new Error("Invalid ingress session");
      this._session = session;
      this._cookie(session);
      return session;
    }

    async _connect() {
      const generation = this._generation;
      this._message("Connecting to the read-only serial log…");
      try {
        const info = await this._hass.callWS({ type: "supervisor/ingress/info", slug: this._config.app_slug });
        if (!this._current(generation)) return;
        if (info.state !== "started" || !info.ingress_url) {
          this._message("The serial app is not running. Open controls to check it, then reconnect this monitor.");
          return;
        }
        const base = new URL(info.ingress_url, location.origin);
        if (base.origin !== location.origin || !/^\/api\/hassio_ingress\/[A-Za-z0-9_-]+\/?$/.test(base.pathname) || base.search || base.hash) {
          throw new Error("Unexpected ingress address");
        }
        base.pathname = base.pathname.replace(/\/?$/, "/");
        const url = new URL(`boards/${encodeURIComponent(this._config.board)}/monitor/`, base);
        url.search = new URLSearchParams({ rendererType: "dom", fontSize: String(this._config.font_size), disableStdin: "true", disableLeaveAlert: "true" }).toString();
        if (!await this._ensureSession(generation) || !this._current(generation)) return;
        const frame = document.createElement("iframe");
        frame.title = `${this._config.title} — read-only serial log`;
        frame.referrerPolicy = "same-origin";
        frame.setAttribute("sandbox", "allow-scripts allow-same-origin");
        frame.src = url.href;
        this._frame = frame;
        this._status.hidden = true;
        this._content.appendChild(frame);
        this._timer = window.setInterval(() => this._keepAlive(generation), 60000);
      } catch (error) {
        if (!this._current(generation)) return;
        this._message(error?.code === "unauthorized"
          ? "Home Assistant did not grant access to this serial monitor. An administrator account is required."
          : "The serial monitor could not connect. Use the reconnect button to try again.");
      }
    }

    async _keepAlive(generation) {
      if (!this._current(generation) || this._refreshing || this._hass.connected === false) return;
      this._refreshing = true;
      try { await this._ensureSession(generation); }
      catch (_) {
        if (this._current(generation)) {
          this._stop();
          this._active = true;
          this._attempted = true;
          this._message("The serial session expired or disconnected. Use the reconnect button to resume.");
        }
      } finally { if (generation === this._generation) this._refreshing = false; }
    }
  }
  if (!customElements.get("serial-party-card")) customElements.define("serial-party-card", SerialPartyCard);
  window.customCards = window.customCards || [];
  if (!window.customCards.some(card => card.type === "serial-party-card")) {
    window.customCards.push({ type: "serial-party-card", name: "Serial Party", description: "Authenticated read-only serial log" });
  }
})();
