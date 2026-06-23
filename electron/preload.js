const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("electronAPI", {
  hideWindow: () => ipcRenderer.send("window-hide"),
  minimizeWindow: () => ipcRenderer.send("window-minimize"),
  toggleMaximize: () => ipcRenderer.send("window-toggle-maximize"),
  closeWindow: () => ipcRenderer.send("window-close"),
  restartBackend: () => ipcRenderer.invoke("backend-restart"),
  openTrading: () => ipcRenderer.invoke("open-trading"),
  onMaximizeChange: (cb) => {
    const handler = (_e, isMax) => cb(isMax);
    ipcRenderer.on("window-maximized", handler);
    return () => ipcRenderer.removeListener("window-maximized", handler);
  },
});
