import electron from "electron";
import path from "path";
import fs from "fs";
import { fileURLToPath } from "url";
import { spawn } from "child_process";

const {
  app,
  BrowserWindow,
  Menu,
  Tray,
  globalShortcut,
  ipcMain,
  shell,
  safeStorage,
  screen,
  dialog,
} = electron;

// Pin a stable app identity BEFORE anything touches userData (the single-instance lock below
// does). Without this the name comes from package.json ("tanstack_start_ts") when run
// unpackaged in dev, but from the packaged productName ("JARVIS") once installed — two
// DIFFERENT userData directories. safeStorage binds its OS-crypt key to the userData dir, so
// that split silently makes a saved API key undecryptable in the other context (and on any
// name drift). One fixed name → one key store → keys persist across dev, packaged, and reopens.
app.setName("JARVIS");

const isDev = !app.isPackaged;
const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);
const uiRoot = path.resolve(__dirname, "..");
const appRoot = uiRoot; // api.py lives in the repo root
// Mutable: the backend auto-selects a free port if its default (8000) is busy (e.g. Docker
// Desktop squats on 8000) and prints "[[JARVIS_PORT]] <n>" on stdout, which we parse below
// to point every fetch / window-load / readiness-check at the real port.
let backendUrl = process.env.JARVIS_BACKEND_URL ?? "http://127.0.0.1:8000";
const devUiUrl = process.env.JARVIS_DEV_UI_URL ?? "http://127.0.0.1:8080";

// c0mr4des trading terminal — a separate app JARVIS launches in its own window.
const tradingRoot = process.env.C0MR4DES_DIR ?? path.resolve(appRoot, "..", "c0mr4des_terminal");
const tradingApiPort = 8100; // its backend (kept off JARVIS's :8000)
const tradingUiPort = 5173; // its Vite frontend
const tradingUiUrl = `http://127.0.0.1:${tradingUiPort}`;

let mainWindow;
let tray;
let pythonProcess;
let ownsBackend = false;
let tradingWindow;
let tradingProcs = [];

app.commandLine.appendSwitch("enable-gpu-rasterization");
app.commandLine.appendSwitch("enable-zero-copy");
app.commandLine.appendSwitch("ignore-gpu-blocklist");

const gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  });
}

let appIsQuitting = false;

// ── Secure API-key storage ──────────────────────────────────────────────────
// Keys are encrypted with the OS keychain (DPAPI on Windows, Keychain on macOS,
// libsecret on Linux) via Electron safeStorage — never written in plaintext and
// never handed back to the renderer. The decrypted values are injected into the
// Python backend's environment when it spawns.
// Every id here is encrypted at rest, loaded on launch, and injected into the Python
// backend's env at spawn — the storage/load/inject logic is generic over this list.
const KEY_IDS = ["GROQ_API_KEY", "ANTHROPIC_API_KEY", "MEM0_API_KEY"];
const keysFilePath = () => path.join(app.getPath("userData"), "jarvis-keys.json");

function loadDecryptedKeys() {
  let enc;
  try {
    enc = JSON.parse(fs.readFileSync(keysFilePath(), "utf-8"));
  } catch {
    return {}; // no file yet / unreadable
  }
  if (!safeStorage.isEncryptionAvailable()) return {};
  const out = {};
  let dropped = false;
  for (const id of KEY_IDS) {
    if (!enc[id]) continue;
    try {
      out[id] = safeStorage.decryptString(Buffer.from(enc[id], "base64"));
    } catch {
      // The blob exists but can't be decrypted — the OS-crypt key that encrypted it is gone
      // (a different app identity/userData wrote it, or the key was regenerated). Don't
      // silently retry it forever: drop the dead entry so status is honest and the user is
      // cleanly prompted to re-enter, rather than "forgetting" the key with no explanation.
      console.warn(
        `[Electron] API key "${id}" could not be decrypted — dropping the stale ` +
          `entry. Re-enter it in Settings (it will persist from now on).`,
      );
      delete enc[id];
      dropped = true;
    }
  }
  if (dropped) {
    try {
      fs.writeFileSync(keysFilePath(), JSON.stringify(enc), { mode: 0o600 });
    } catch {
      /* best-effort cleanup */
    }
  }
  return out;
}

function saveEncryptedKeys(keys) {
  if (!safeStorage.isEncryptionAvailable()) {
    throw new Error("OS secure storage is unavailable on this machine.");
  }
  let store = {};
  try {
    store = JSON.parse(fs.readFileSync(keysFilePath(), "utf-8"));
  } catch {
    /* first write */
  }
  for (const id of KEY_IDS) {
    if (!(id in keys)) continue; // omitted field → leave unchanged
    const val = String(keys[id] ?? "").trim();
    if (val) store[id] = safeStorage.encryptString(val).toString("base64");
    else delete store[id]; // empty string → clear it
  }
  fs.writeFileSync(keysFilePath(), JSON.stringify(store), { mode: 0o600 });

  // Verify the write landed and reads back — so we never report a key as saved when it
  // silently didn't persist (a disk/permission hiccup, or storage that can't round-trip).
  const check = loadDecryptedKeys();
  for (const id of KEY_IDS) {
    const want = String(keys[id] ?? "").trim();
    if (want && check[id] !== want) {
      throw new Error(
        "The key was written but couldn't be read back — it may not persist. " +
          "Please try again.",
      );
    }
  }
}

function apiKeyStatus() {
  let secure = false;
  try {
    secure = safeStorage.isEncryptionAvailable();
  } catch {
    /* not ready yet */
  }
  const keys = loadDecryptedKeys();
  return {
    secure,
    groq: Boolean(keys.GROQ_API_KEY),
    anthropic: Boolean(keys.ANTHROPIC_API_KEY),
    mem0: Boolean(keys.MEM0_API_KEY),
  };
}

function resolvePythonPath() {
  const isWindows = process.platform === "win32";

  const packagedBackend = isWindows
    ? path.join(process.resourcesPath, "jarvis_backend", "jarvis_backend.exe")
    : path.join(process.resourcesPath, "jarvis_backend", "jarvis_backend");

  if (!isDev && fs.existsSync(packagedBackend)) {
    return { path: packagedBackend, isPackaged: true };
  }

  const venvPy = (root) =>
    isWindows
      ? path.join(root, "venv", "Scripts", "python.exe")
      : path.join(root, "venv", "bin", "python");

  const candidates = [
    process.env.JARVIS_PYTHON, // explicit override
    venvPy(appRoot), // venv inside the repo (portable)
    venvPy(path.resolve(appRoot, "..")), // venv one level up (e.g. C:\Users\rosha\venv)
    isWindows ? "py" : "python3",
    "python",
  ].filter(Boolean);
  const found = candidates.find(
    (candidate) =>
      candidate === "py" ||
      candidate === "python3" ||
      candidate === "python" ||
      fs.existsSync(candidate),
  );
  return { path: found, isPackaged: false };
}

async function isBackendReady() {
  // Timeout the probe: if a dead process (or Docker) squats the port and accepts TCP but never
  // responds, a bare fetch would hang forever and stall startup. 2s is plenty for a loopback probe.
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 2000);
  try {
    const response = await fetch(`${backendUrl}/api/agent/status`, { signal: ctrl.signal });
    return response.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

async function waitForBackend(timeoutMs = 150000) {
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
  const apiKeys = loadDecryptedKeys();
  pythonProcess = spawn(pythonPath, pythonArgs, {
    cwd: appRoot,
    env: {
      ...process.env,
      ...apiKeys,
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
      const t = line.trim();
      if (!t) return;
      // The backend announces its actual port (it may differ from 8000 if that was busy).
      const m = t.match(/^\[\[JARVIS_PORT\]\]\s+(\d+)/);
      if (m) {
        backendUrl = `http://127.0.0.1:${m[1]}`;
        console.log(`[Electron] Backend on port ${m[1]}`);
      }
      console.log(`[Python] ${t}`);
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

// Remember window position/size/maximized-state across launches — a native
// app doesn't reset itself to a hardcoded size every time you open it.
const windowStatePath = () => path.join(app.getPath("userData"), "window-state.json");

function loadWindowState() {
  try {
    const raw = JSON.parse(fs.readFileSync(windowStatePath(), "utf-8"));
    const { width, height, x, y, isMaximized } = raw;
    if (typeof width !== "number" || typeof height !== "number") return null;
    if (typeof x === "number" && typeof y === "number") {
      // Discard a saved position that's no longer on any connected display
      // (e.g. an external monitor was unplugged) — recenter instead of
      // risking an unreachable off-screen window.
      const onScreen = screen.getAllDisplays().some((d) => {
        const a = d.workArea;
        return x >= a.x - width && x <= a.x + a.width && y >= a.y - height && y <= a.y + a.height;
      });
      if (onScreen) return { width, height, x, y, isMaximized: Boolean(isMaximized) };
    }
    return { width, height, isMaximized: Boolean(isMaximized) };
  } catch {
    return null;
  }
}

function saveWindowState() {
  if (!mainWindow || mainWindow.isDestroyed()) return;
  try {
    const isMaximized = mainWindow.isMaximized();
    // getBounds() while maximized returns the maximized size — save the
    // pre-maximize (normal) bounds so un-maximizing restores the right size.
    const bounds = isMaximized ? mainWindow.getNormalBounds() : mainWindow.getBounds();
    fs.writeFileSync(windowStatePath(), JSON.stringify({ ...bounds, isMaximized }), "utf-8");
  } catch {
    /* best-effort — a failed save just means next launch uses defaults */
  }
}

function createWindow() {
  const saved = loadWindowState();
  mainWindow = new BrowserWindow({
    width: saved?.width ?? 1360,
    height: saved?.height ?? 860,
    ...(saved?.x !== undefined && saved?.y !== undefined ? { x: saved.x, y: saved.y } : {}),
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

  if (saved?.isMaximized) mainWindow.maximize();

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  mainWindow.once("ready-to-show", () => {
    mainWindow.show();
    mainWindow.focus();
  });

  mainWindow.on("hide", () => {
    mainWindow?.webContents.setBackgroundThrottling(true);
  });

  mainWindow.on("show", () => {
    mainWindow?.webContents.setBackgroundThrottling(false);
  });

  mainWindow.on("close", (event) => {
    if (!appIsQuitting) {
      event.preventDefault();
      mainWindow.hide();
    }
  });

  mainWindow.on("closed", () => {
    mainWindow = undefined;
  });

  // Notify the renderer so the maximize/restore button shows the right icon.
  mainWindow.on("maximize", () => {
    mainWindow?.webContents.send("window-maximized", true);
    saveWindowState();
  });
  mainWindow.on("unmaximize", () => {
    mainWindow?.webContents.send("window-maximized", false);
    saveWindowState();
  });

  // Debounced — resize/move fire continuously while dragging.
  let boundsSaveTimer;
  const scheduleBoundsSave = () => {
    clearTimeout(boundsSaveTimer);
    boundsSaveTimer = setTimeout(saveWindowState, 500);
  };
  mainWindow.on("resize", scheduleBoundsSave);
  mainWindow.on("move", scheduleBoundsSave);
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
        appIsQuitting = true;
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
  await stopPythonBackend();
  await startPythonBackend();
  // Must match loadApp() — Vite-dev serves the UI on :8080, not the FastAPI port.
  const targetUrl = isDev && process.env.JARVIS_USE_VITE === "1" ? devUiUrl : backendUrl;
  await mainWindow?.loadURL(targetUrl);
}

// A real application menu — mainly for its keyboard accelerators (Ctrl+R,
// Ctrl+Q, etc.). The window is frameless, so no bar is ever drawn; this is
// purely what makes native-feeling shortcuts actually work.
function createAppMenu() {
  const template = [
    {
      label: "JARVIS",
      submenu: [
        {
          label: "Restart Backend",
          accelerator: "CmdOrCtrl+Shift+R",
          click: () => restartBackend(),
        },
        { type: "separator" },
        { label: "About JARVIS", click: () => showAboutDialog() },
        { type: "separator" },
        {
          label: "Quit",
          accelerator: "CmdOrCtrl+Q",
          click: () => {
            appIsQuitting = true;
            app.quit();
          },
        },
      ],
    },
    {
      label: "View",
      submenu: [{ role: "reload" }, { role: "forceReload" }, { role: "toggleDevTools" }],
    },
    { label: "Window", submenu: [{ role: "minimize" }, { role: "close" }] },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function showAboutDialog() {
  dialog.showMessageBox(mainWindow, {
    type: "info",
    title: "About JARVIS",
    message: "JARVIS",
    detail: `Version ${app.getVersion()}\nA personal, compute-elastic AI assistant.`,
    buttons: ["OK"],
  });
}

// Returns a promise that resolves once the old process has actually exited (or
// after a 5s safety timeout), so restartBackend() can't spawn a new backend —
// and have startPythonBackend()'s isBackendReady() falsely "reuse" the still-
// dying old one — while the previous process is still holding the port.
function stopPythonBackend() {
  if (!pythonProcess || !ownsBackend) return Promise.resolve();
  console.log("[Electron] Terminating Python backend...");
  const proc = pythonProcess;
  pythonProcess = undefined;
  ownsBackend = false;
  return new Promise((resolve) => {
    let settled = false;
    const done = () => {
      if (!settled) {
        settled = true;
        resolve();
      }
    };
    proc.once("exit", done);
    setTimeout(done, 5000); // don't let a stuck kill hang the restart forever
    try {
      if (process.platform === "win32") {
        spawn("taskkill", ["/pid", String(proc.pid), "/f", "/t"], { windowsHide: true });
      } else {
        proc.kill("SIGTERM");
      }
    } catch (err) {
      console.error("[Electron] Failed to stop Python backend:", err);
      done();
    }
  });
}

// ── Trading terminal (c0mr4des) — launched in its own window ──────────────────
async function isUrlReady(url) {
  // r.ok (2xx) only — a transient 404/other 4xx while a dev server is mid-boot
  // must not be treated as "ready", or the caller loads a broken page and stops
  // polling instead of waiting for the real response.
  try {
    const r = await fetch(url);
    return r.ok;
  } catch {
    return false;
  }
}

function spawnAndLog(cmd, args, opts, tag) {
  const p = spawn(cmd, args, opts);
  p.stdout?.on("data", (d) => console.log(`[${tag}] ${d.toString().trim()}`));
  p.stderr?.on("data", (d) => console.error(`[${tag}] ${d.toString().trim()}`));
  tradingProcs.push(p);
  return p;
}

const LOADING_HTML = (msg, color = "#f59e0b") =>
  "data:text/html," +
  encodeURIComponent(
    `<body style="background:#0a0705;color:${color};font-family:ui-monospace,monospace;display:flex;` +
      `align-items:center;justify-content:center;height:100vh;margin:0;text-align:center">` +
      `<div><div style="font-size:18px">${msg}</div>` +
      `<div style="opacity:.55;margin-top:10px;font-size:12px">c0mr4des terminal · backend :${tradingApiPort} · ui :${tradingUiPort}</div></div></body>`,
  );

async function openTradingTerminal() {
  if (tradingWindow && !tradingWindow.isDestroyed()) {
    tradingWindow.show();
    tradingWindow.focus();
    return { ok: true };
  }
  if (!fs.existsSync(tradingRoot)) {
    return { ok: false, error: `c0mr4des_terminal not found at ${tradingRoot}` };
  }

  tradingWindow = new BrowserWindow({
    width: 1500,
    height: 920,
    backgroundColor: "#0a0705",
    title: "JARVIS · Trading Terminal",
    autoHideMenuBar: true,
    webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true },
  });
  // Same popup/navigation hardening as mainWindow — this window loads content
  // from a sibling repo (c0mr4des_terminal) not reviewed by this codebase.
  tradingWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });
  tradingWindow.on("closed", () => {
    tradingWindow = undefined;
  });
  tradingWindow.loadURL(LOADING_HTML("⟳ Spinning up the trading terminal…"));

  if (await isUrlReady(tradingUiUrl)) {
    tradingWindow.loadURL(tradingUiUrl);
    return { ok: true };
  }

  const isWin = process.platform === "win32";
  const venvPy = path.join(tradingRoot, ".venv", "Scripts", isWin ? "python.exe" : "python");
  const py = fs.existsSync(venvPy) ? venvPy : isWin ? "py" : "python3";
  const env = {
    ...process.env,
    PYTHONUNBUFFERED: "1",
    C0MR4DES_API: `http://127.0.0.1:${tradingApiPort}`,
  };
  const npm = isWin ? "npm.cmd" : "npm";

  spawnAndLog(
    py,
    ["-m", "uvicorn", "backend.main:app", "--port", String(tradingApiPort)],
    { cwd: tradingRoot, env, stdio: ["ignore", "pipe", "pipe"], windowsHide: true },
    "Trading-BE",
  );

  // First run: install frontend deps before starting Vite.
  if (!fs.existsSync(path.join(tradingRoot, "node_modules"))) {
    tradingWindow.loadURL(LOADING_HTML("⟳ Installing trading UI deps (first run, ~1–2 min)…"));
    await new Promise((resolve) => {
      const inst = spawnAndLog(
        npm,
        ["install"],
        {
          cwd: tradingRoot,
          env,
          stdio: ["ignore", "pipe", "pipe"],
          windowsHide: true,
          shell: isWin,
        },
        "Trading-npm",
      );
      inst.on("exit", resolve);
      inst.on("error", resolve);
    });
    if (tradingWindow?.isDestroyed())
      return { ok: false, error: "Trading window was closed before it finished starting." };
    tradingWindow?.loadURL(LOADING_HTML("⟳ Starting the trading terminal…"));
  }

  spawnAndLog(
    npm,
    ["run", "dev"],
    { cwd: tradingRoot, env, stdio: ["ignore", "pipe", "pipe"], windowsHide: true, shell: isWin },
    "Trading-FE",
  );

  const deadline = Date.now() + 180000;
  while (Date.now() < deadline) {
    if (!tradingWindow || tradingWindow.isDestroyed()) {
      return { ok: false, error: "Trading window was closed before it finished starting." };
    }
    if (await isUrlReady(tradingUiUrl)) {
      tradingWindow.loadURL(tradingUiUrl);
      return { ok: true };
    }
    await new Promise((r) => setTimeout(r, 1500));
  }
  if (tradingWindow && !tradingWindow.isDestroyed()) {
    tradingWindow.loadURL(
      LOADING_HTML("Trading terminal didn't start in time — check deps + console.", "#ff6b6b"),
    );
  }
  return { ok: false, error: "timeout" };
}

function stopTrading() {
  tradingProcs.forEach((p) => {
    try {
      p.kill();
    } catch (_) {}
  });
  tradingProcs = [];
}

ipcMain.handle("open-trading", openTradingTerminal);

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

ipcMain.handle("keys:status", () => apiKeyStatus());
ipcMain.handle("keys:set", async (_event, keys) => {
  saveEncryptedKeys(keys || {});
  // Restart the Python backend so it picks up the new keys from its environment.
  await stopPythonBackend();
  await startPythonBackend();
  return apiKeyStatus();
});
ipcMain.on("open-external", (_event, url) => {
  if (typeof url === "string" && /^https:\/\//i.test(url)) shell.openExternal(url);
});

if (gotSingleInstanceLock) {
  app.whenReady().then(async () => {
    createWindow();
    createTray();
    createAppMenu();
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
}

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});

app.on("before-quit", () => {
  appIsQuitting = true;
  saveWindowState();
  stopPythonBackend();
  stopTrading();
});

app.on("will-quit", () => {
  globalShortcut.unregisterAll();
});
