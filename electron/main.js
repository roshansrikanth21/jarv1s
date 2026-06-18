import electron from "electron";
import path from "path";
import fs from "fs";
import { fileURLToPath } from "url";
import { spawn } from "child_process";
import isDev from "electron-is-dev";

const { app, BrowserWindow, Menu, Tray, globalShortcut, ipcMain, shell } = electron;
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const uiRoot  = path.resolve(__dirname, "..");
const appRoot = uiRoot; // api.py lives in the repo root
const backendUrl = process.env.JARVIS_BACKEND_URL ?? "http://127.0.0.1:8000";
const devUiUrl = process.env.JARVIS_DEV_UI_URL ?? "http://127.0.0.1:8080";

let mainWindow;
let tray;
let pythonProcess;
let ownsBackend = false;

app.commandLine.appendSwitch("enable-gpu-rasterization");
app.commandLine.appendSwitch("enable-zero-copy");
app.commandLine.appendSwitch("ignore-gpu-blocklist");

function resolvePythonPath() {
  const isWindows = process.platform === "win32";

  const packagedBackend = isWindows
    ? path.join(process.resourcesPath, "jarvis_backend", "jarvis_backend.exe")
    : path.join(process.resourcesPath, "jarvis_backend", "jarvis_backend");

  if (!isDev && fs.existsSync(packagedBackend)) {
    return { path: packagedBackend, isPackaged: true };
  }

  const candidates = [
    isWindows ? path.join(appRoot, "venv", "Scripts", "python.exe") : path.join(appRoot, "venv", "bin", "python"),
    isWindows ? "py" : "python3",
    "python",
  ];
  const found = candidates.find((candidate) => candidate === "py" || candidate === "python3" || candidate === "python" || fs.existsSync(candidate));
  return { path: found, isPackaged: false };
}

async function isBackendReady() {
  try {
    const response = await fetch(`${backendUrl}/api/agent/status`);
    return response.ok;
  } catch {
    return false;
  }
}

async function waitForBackend(timeoutMs = 90000) {
  const startedAt = Date.now();
  while (Date.now() - startedAt < timeoutMs) {
    if (await isBackendReady()) return true;
    await new Promise((resolve) => setTimeout(resolve, 750));
  }
  return false;
}

async function startPythonBackend() {
  if (await isBackendReady()) {
    console.log("[Electron] Reusing existing backend.");
    return true;
  }

  const pythonInfo = resolvePythonPath();
  const pythonPath = pythonInfo.path;

  let pythonArgs;
  if (pythonInfo.isPackaged) {
    pythonArgs = [];
  } else {
    const scriptPath = path.join(appRoot, "api.py");
    pythonArgs = pythonPath === "py" ? ["-3", scriptPath] : [scriptPath];
  }

  console.log(`[Electron] Starting Python backend: ${pythonPath} ${pythonArgs.join(" ")}`);
  pythonProcess = spawn(pythonPath, pythonArgs, {
    cwd: appRoot,
    env: {
      ...process.env,
      PYTHONUNBUFFERED: "1",
      JARVIS_DESKTOP: "1",
    },
    stdio: ["ignore", "pipe", "pipe"],
    windowsHide: true,
  });
  ownsBackend = true;

  pythonProcess.stdout.on("data", (data) => {
    const lines = data.toString().split("\n");
    lines.forEach((line) => {
      if (line.trim()) console.log(`[Python] ${line.trim()}`);
    });
  });

  pythonProcess.stderr.on("data", (data) => {
    const lines = data.toString().split("\n");
    lines.forEach((line) => {
      if (line.trim()) console.error(`[Python-Err] ${line.trim()}`);
    });
  });

  pythonProcess.on("error", (err) => {
    console.error("[Electron] Failed to start Python backend:", err);
  });

  pythonProcess.on("exit", (code) => {
    console.log(`[Electron] Python backend exited with code ${code}`);
    pythonProcess = undefined;
  });

  return waitForBackend();
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1360,
    height: 860,
    minWidth: 1100,
    minHeight: 720,
    frame: false,
    transparent: false,
    backgroundColor: "#080403",
    titleBarStyle: "hidden",
    show: false,
    title: "JARVIS Command Deck",
    icon: path.join(uiRoot, "public", "favicon.ico"),
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      nodeIntegration: false,
      contextIsolation: true,
      backgroundThrottling: false,
    },
  });

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  mainWindow.once("ready-to-show", () => {
    mainWindow.show();
    mainWindow.focus();
  });

  mainWindow.on("close", (event) => {
    if (!app.isQuitting) {
      event.preventDefault();
      mainWindow.hide();
    }
  });

  mainWindow.on("closed", () => {
    mainWindow = undefined;
  });

  // Notify the renderer so the maximize/restore button shows the right icon.
  mainWindow.on("maximize", () => mainWindow?.webContents.send("window-maximized", true));
  mainWindow.on("unmaximize", () => mainWindow?.webContents.send("window-maximized", false));
}

async function loadApp() {
  if (!mainWindow) return;
  const backendReady = await startPythonBackend();
  const targetUrl = isDev && process.env.JARVIS_USE_VITE === "1" ? devUiUrl : backendUrl;

  if (!backendReady) {
    await mainWindow.loadURL(
      `data:text/html;charset=utf-8,${encodeURIComponent(`
        <body style="margin:0;background:#090403;color:#f4d28b;font:14px monospace;display:grid;place-items:center;height:100vh">
          <div style="border:1px solid rgba(220,38,38,.45);padding:24px;max-width:620px">
            <div style="color:#ef4444;letter-spacing:.25em;text-transform:uppercase;margin-bottom:12px">JARVIS backend failed to boot</div>
            <div>Start api.py manually once to inspect the Python error, then relaunch the desktop app.</div>
          </div>
        </body>
      `)}`,
    );
    return;
  }

  await mainWindow.loadURL(targetUrl);
}

function createTray() {
  const iconPath = path.join(uiRoot, "public", "favicon.ico");
  if (!fs.existsSync(iconPath)) return;

  tray = new Tray(iconPath);
  const contextMenu = Menu.buildFromTemplate([
    { label: "Show JARVIS", click: () => mainWindow?.show() },
    { label: "Restart Backend", click: restartBackend },
    { type: "separator" },
    {
      label: "Quit",
      click: () => {
        app.isQuitting = true;
        app.quit();
      },
    },
  ]);
  tray.setToolTip("JARVIS Command Deck");
  tray.setContextMenu(contextMenu);
  tray.on("click", () => {
    if (!mainWindow) return;
    mainWindow.isVisible() ? mainWindow.hide() : mainWindow.show();
  });
}

async function restartBackend() {
  stopPythonBackend();
  await startPythonBackend();
  await mainWindow?.loadURL(backendUrl);
}

function stopPythonBackend() {
  if (!pythonProcess || !ownsBackend) return;
  console.log("[Electron] Terminating Python backend...");
  if (process.platform === "win32") {
    spawn("taskkill", ["/pid", pythonProcess.pid, "/f", "/t"]);
  } else {
    pythonProcess.kill();
  }
  pythonProcess = undefined;
  ownsBackend = false;
}

ipcMain.on("window-hide", () => mainWindow?.hide());
ipcMain.on("window-minimize", () => mainWindow?.minimize());
ipcMain.on("window-toggle-maximize", () => {
  if (!mainWindow) return;
  mainWindow.isMaximized() ? mainWindow.unmaximize() : mainWindow.maximize();
});
ipcMain.on("window-close", () => {
  if (mainWindow) mainWindow.hide();
});
ipcMain.handle("backend-restart", restartBackend);

app.whenReady().then(async () => {
  createWindow();
  createTray();
  await loadApp();

  globalShortcut.register("Alt+Space", () => {
    if (!mainWindow) return;
    if (mainWindow.isVisible()) {
      mainWindow.hide();
    } else {
      mainWindow.show();
      mainWindow.focus();
    }
  });

  app.on("activate", async () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
      await loadApp();
    }
  });
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", stopPythonBackend);

app.on("will-quit", () => {
  globalShortcut.unregisterAll();
});
