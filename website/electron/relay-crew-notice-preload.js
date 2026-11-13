"use strict";

// The relay Crew notice's only script (relay-crew-notice.html runs none). It
// reads the text main put on the query string, sets it as plain text, and sends
// a button press to main on this view's own channel. Nothing is exposed to the
// page: it has no script to call it.

const { ipcRenderer } = require("electron");

const CHANNEL = "relay-crew-notice:act";

window.addEventListener("DOMContentLoaded", () => {
  const params = new URLSearchParams(window.location.search);
  for (const id of ["title", "body", "check", "close"]) {
    const el = document.getElementById(id);
    if (!el) continue;
    const text = params.get(id) || "";
    el.textContent = text;
    el.hidden = text === "";
  }
  for (const action of ["check", "close"]) {
    const el = document.getElementById(action);
    if (el) el.addEventListener("click", () => ipcRenderer.send(CHANNEL, action));
  }
  const first = document.querySelector("button:not([hidden])");
  if (first) first.focus();
});
