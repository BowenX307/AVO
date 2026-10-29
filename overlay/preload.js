const { contextBridge, ipcRenderer } = require("electron");

const TO_MAIN = new Set([
  "island-size", "island-open", "island-cancel", "island-submit", "island-control",
  "bubble-size", "bubble-choice", "answer", "select-box",
]);

contextBridge.exposeInMainWorld("avo", {
  onEvent: (handler) => ipcRenderer.on("avo", (_e, msg) => handler(msg)),
  send: (channel, data) => {
    if (TO_MAIN.has(channel)) ipcRenderer.send(channel, data);
  },
});
