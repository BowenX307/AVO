const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("avo", {
  onEvent: (handler) => ipcRenderer.on("avo", (_e, msg) => handler(msg)),
});
