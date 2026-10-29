const { contextBridge, ipcRenderer } = require("electron");

const TO_MAIN = new Set(["caption-size", "caption-hide", "answer"]);

contextBridge.exposeInMainWorld("avo", {
  onEvent: (handler) => ipcRenderer.on("avo", (_e, msg) => handler(msg)),
  send: (channel, data) => {
    if (TO_MAIN.has(channel)) ipcRenderer.send(channel, data);
  },
});
