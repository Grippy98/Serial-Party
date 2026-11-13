(() => {
  "use strict";
  const terminal = document.getElementById("terminal");
  const badge = document.getElementById("mode");
  const button = document.getElementById("mode-button");
  const hint = document.getElementById("hint");
  const selector = document.getElementById("board");
  const base = new URL(".", window.location.href);
  let boards = [];
  let pageActive = true;
  let control = false;
  let frame;

  function unload() {
    if (frame) {
      // Remove the browsing context and its websocket before opening another.
      frame.src = "about:blank";
      frame.remove();
      frame = undefined;
    }
  }

  function show(nextControl) {
    unload();
    const board = boards.find(item => item.id === selector.value);
    if (!board) return;
    terminal.replaceChildren();
    control = nextControl;
    badge.textContent = control ? "Control requested" : "Read only";
    badge.classList.toggle("control", control);
    button.textContent = control ? "Return to monitor" : "Request control";
    hint.textContent = control
      ? "Control opens only if available. If busy, return to the monitor and try later."
      : "Read-only view. Requesting control will not interrupt another controller.";
    frame = document.createElement("iframe");
    frame.title = `${board.name} serial ${control ? "control" : "monitor"}`;
    frame.referrerPolicy = "same-origin";
    const url = new URL(`boards/${encodeURIComponent(board.id)}/${control ? "control" : "monitor"}/`, base);
    url.searchParams.set("rendererType", "dom");
    url.searchParams.set("disableLeaveAlert", "true");
    frame.src = url.href;
    terminal.appendChild(frame);
  }

  button.addEventListener("click", () => show(!control));
  selector.addEventListener("change", () => show(false));
  window.addEventListener("pagehide", () => { pageActive = false; unload(); });
  // A restored page must never silently restore a writable session.
  window.addEventListener("pageshow", event => { pageActive = true; if (event.persisted) show(false); });
  async function loadBoards() {
    try {
      const response = await fetch(new URL("api/boards", base), { credentials: "same-origin", cache: "no-store" });
      if (!response.ok) throw new Error("Board list unavailable");
      const value = await response.json();
      if (!Array.isArray(value) || !value.length || value.some(item => !item || !/^[a-z][a-z0-9_-]{0,31}$/.test(item.id) || typeof item.name !== "string")) {
        throw new Error("Invalid board list");
      }
      boards = value;
      selector.replaceChildren();
      for (const board of boards) {
        const option = document.createElement("option");
        option.value = board.id;
        option.textContent = board.name;
        selector.appendChild(option);
      }
      selector.disabled = false;
      button.disabled = false;
      if (pageActive) show(false);
    } catch (_) {
      terminal.replaceChildren();
      const message = document.createElement("p");
      message.className = "empty-state";
      message.textContent = "The board list could not be loaded. Refresh this page to try again.";
      terminal.appendChild(message);
      hint.textContent = "No serial session is open.";
    }
  }
  loadBoards();
})();
