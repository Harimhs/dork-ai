// Bridge for Dork's assistant window: lets the page (served by the Dork server) show/hide itself and open
// the chat window. Nothing else from Node/Electron is exposed.
const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("dorkDesktop", {
  show: () => ipcRenderer.send("assistant", "show"),
  hide: () => ipcRenderer.send("assistant", "hide"),
  toggleFull: () => ipcRenderer.send("assistant", "full"),
  openChat: (path) => ipcRenderer.send("open-chat", String(path || "/")),
  onVisible: (fn) => ipcRenderer.on("assistant-visible", (_e, v) => fn(!!v)),
});
