// Dork Desktop: a native window around Dork, with a tray icon and a global hotkey.
//
//   Assistant         a hidden reactor window that pops up when Dork is woken by voice ("Dork, wake up")
//   Ctrl+Shift+Space  show / hide the assistant from anywhere
//   Tray click        show / hide the chat window
//   Close button      hides to the tray (Quit from the tray menu)
//
// It starts with Windows (hidden), and on the Dork PC it also starts the Dork server if it isn't running.

const { app, BrowserWindow, Tray, Menu, globalShortcut, nativeTheme, shell, net, screen, session, ipcMain } = require("electron");
const path = require("path");
const fs = require("fs");
const { spawn } = require("child_process");

const SERVER = process.env.DORK_URL || "http://localhost:7777";
const HOTKEY = "CommandOrControl+Shift+Space";
// Where Dork itself lives: $DORK_HOME, else the folder setup recorded (dork-home.txt in the app's data folder),
// else the repo this app was built from (desktop/ sits inside it).
function dorkHome() {
  if (process.env.DORK_HOME) return process.env.DORK_HOME;
  try {
    const rec = path.join(app.getPath("userData"), "dork-home.txt");
    if (fs.existsSync(rec)) return fs.readFileSync(rec, "utf8").trim();
  } catch {}
  return path.resolve(__dirname, "..");
}
const DORK_HOME = dorkHome();
const LAUNCHER = path.join(DORK_HOME, process.platform === "win32" ? "start-dork.vbs" : "start-dork.sh");
const ICON = path.join(__dirname, "icons", "dork.ico");
// Written by the Dork server on this PC: lets the desktop app sign in without the master key.
const LOCAL_TOKEN = path.join(DORK_HOME, "data", "local-app.token");
const TITLEBAR = 52; // matches the web UI's top bar height

let win = null;
let assistant = null;
let tray = null;
// --assistant: started by Dork's server because it was woken by voice — show the reactor, not the chat.
const startAssistant = process.argv.includes("--assistant");
const startHidden = process.argv.includes("--hidden") || startAssistant;
let quitting = false;

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", (_e, argv) => (argv.includes("--assistant") ? showAssistant() : show()));
}
app.setAppUserModelId("dev.hari.dork");

// ---------- server ----------

function serverUp() {
  return new Promise((resolve) => {
    const req = net.request({ url: SERVER + "/manifest.json", method: "GET" });
    const timer = setTimeout(() => { req.abort(); resolve(false); }, 2500);
    req.on("response", (res) => { clearTimeout(timer); resolve(res.statusCode === 200); res.on("data", () => {}); });
    req.on("error", () => { clearTimeout(timer); resolve(false); });
    req.end();
  });
}

async function ensureServer() {
  if (await serverUp()) return true;
  const local = /^http:\/\/(localhost|127\.0\.0\.1)/.test(SERVER);
  if (local && fs.existsSync(LAUNCHER)) {
    const [cmd, args] = process.platform === "win32" ? ["wscript.exe", [LAUNCHER]] : ["bash", [LAUNCHER]];
    spawn(cmd, args, { cwd: DORK_HOME, detached: true, stdio: "ignore", windowsHide: true }).unref();
    for (let i = 0; i < 40; i++) {
      await new Promise((r) => setTimeout(r, 500));
      if (await serverUp()) return true;
    }
  }
  return false;
}

async function signIn() {
  if (!/^http:\/\/(localhost|127\.0\.0\.1)/.test(SERVER)) return;  // remote PCs pair with a code
  for (let i = 0; i < 20 && !fs.existsSync(LOCAL_TOKEN); i++) await new Promise((r) => setTimeout(r, 250));
  if (!fs.existsSync(LOCAL_TOKEN)) return;
  const value = fs.readFileSync(LOCAL_TOKEN, "utf8").trim();
  const existing = await session.defaultSession.cookies.get({ url: SERVER, name: "dork_token" });
  if (existing.some((c) => c.value === value)) return;
  await session.defaultSession.cookies.set({
    url: SERVER, name: "dork_token", value, httpOnly: true, sameSite: "strict",
    expirationDate: Math.floor(Date.now() / 1000) + 10 * 365 * 86400,
  });
}

async function load(path = "") {
  if (await ensureServer()) {
    await signIn();
    win.loadURL(SERVER + path);
    if (assistant) assistant.loadURL(SERVER + "/assistant");
  } else {
    win.loadFile(path.join(__dirname, "offline.html"), { query: { server: SERVER } });
  }
}

// ---------- window ----------

function overlayColors() {
  return nativeTheme.shouldUseDarkColors
    ? { color: "#0e0f11", symbolColor: "#a9adb3", height: TITLEBAR }
    : { color: "#f4f3ef", symbolColor: "#4a4a47", height: TITLEBAR };
}

// Make the web UI's top bar act as the window's title bar (drag to move, room for the caption buttons).
const DESKTOP_CSS = `
  .topbar, .side-head, .threads-head { -webkit-app-region: drag; }
  .topbar button, .side-head *, .threads-head button, .topbar .conv-title { -webkit-app-region: no-drag; }
  .topbar { padding-right: 150px !important; }
  #install-app { display: none !important; }
  .lock { -webkit-app-region: drag; } .lock-card { -webkit-app-region: no-drag; }
`;

function createWindow() {
  const area = screen.getPrimaryDisplay().workAreaSize;  // fit small laptop screens
  win = new BrowserWindow({
    width: Math.min(1200, Math.round(area.width * 0.85)),
    height: Math.min(820, Math.round(area.height * 0.9)),
    center: true,
    minWidth: 420,
    minHeight: 520,
    show: false,
    icon: ICON,
    title: "Dork",
    backgroundColor: nativeTheme.shouldUseDarkColors ? "#0e0f11" : "#f4f3ef",
    titleBarStyle: "hidden",
    titleBarOverlay: overlayColors(),
    webPreferences: { contextIsolation: true, sandbox: true, spellcheck: true },
  });
  win.setMenuBarVisibility(false);

  win.webContents.on("did-finish-load", () => win.webContents.insertCSS(DESKTOP_CSS));
  win.once("ready-to-show", () => { if (!startHidden) win.show(); });

  // Links (sources, GitHub, …) open in the normal browser, not inside Dork.
  win.webContents.setWindowOpenHandler(({ url }) => { shell.openExternal(url); return { action: "deny" }; });
  win.webContents.on("will-navigate", (e, url) => {
    if (!url.startsWith(SERVER) && !url.startsWith("file:")) { e.preventDefault(); shell.openExternal(url); }
  });

  win.on("close", (e) => {
    if (!quitting) { e.preventDefault(); win.hide(); }
  });
  load();
}

// ---------- assistant: the voice-only reactor window ----------

function createAssistant() {
  const area = screen.getPrimaryDisplay().workAreaSize;
  const w = Math.min(560, Math.round(area.width * 0.42)), h = Math.min(640, Math.round(area.height * 0.72));
  assistant = new BrowserWindow({
    width: w, height: h, center: true, show: false, frame: false, resizable: true, minWidth: 360, minHeight: 420,
    alwaysOnTop: true, skipTaskbar: false, icon: ICON, title: "Dork Assistant", backgroundColor: "#030507",
    webPreferences: {
      contextIsolation: true, sandbox: true, preload: path.join(__dirname, "preload.js"),
      backgroundThrottling: false,            // keep listening for events while hidden
      autoplayPolicy: "no-user-gesture-required",  // Dork speaks without a click
    },
  });
  assistant.setAlwaysOnTop(true, "floating");
  assistant.setMenuBarVisibility(false);
  assistant.webContents.setWindowOpenHandler(({ url }) => { shell.openExternal(url); return { action: "deny" }; });
  assistant.on("close", (e) => { if (!quitting) { e.preventDefault(); assistant.hide(); } });
  // Stop drawing the 3D reactor while nobody can see it (it keeps listening for events either way).
  const vis = (v) => assistant.webContents.send("assistant-visible", v);
  assistant.on("show", () => vis(true));
  assistant.on("hide", () => vis(false));
  assistant.on("minimize", () => vis(false));
  assistant.on("restore", () => vis(true));
  assistant.webContents.on("did-finish-load", () => vis(assistant.isVisible()));
}

function showAssistant() {
  if (!assistant) return;
  if (assistant.isMinimized()) assistant.restore();
  assistant.show();
  assistant.moveTop();
  assistant.focus();
}

function toggleAssistant() {
  if (assistant.isVisible() && assistant.isFocused()) assistant.hide();
  else { showAssistant(); assistant.webContents.executeJavaScript("fetch('/api/assistant/wake',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json'},body:'{}'})").catch(() => {}); }
}

ipcMain.on("assistant", (e, what) => {
  if (!assistant || e.sender !== assistant.webContents) return;
  if (what === "show") showAssistant();
  else if (what === "hide") assistant.hide();
  else if (what === "full") assistant.setFullScreen(!assistant.isFullScreen());
});

ipcMain.on("open-chat", (e, p) => {
  if (!assistant || e.sender !== assistant.webContents) return;
  const pathOnly = p.startsWith("/") ? p : "/";
  win.loadURL(SERVER + pathOnly);
  show();
});

function show() {
  if (!win) return;
  if (win.isMinimized()) win.restore();
  win.show();
  win.focus();
}

function toggle() {
  if (win.isVisible() && win.isFocused()) win.hide();
  else show();
}

// ---------- tray ----------

function buildTray() {
  tray = new Tray(ICON);
  tray.setToolTip("Dork  (Ctrl+Shift+Space)");
  const menu = () => Menu.buildFromTemplate([
    { label: "Open Dork", click: show },
    { label: "Assistant (Ctrl+Shift+Space)", click: toggleAssistant },
    { label: "New thread", click: () => { show(); win.loadURL(SERVER + "/?new=1"); } },
    { label: "Reload", click: () => load() },
    { type: "separator" },
    {
      label: "Start with Windows",
      type: "checkbox",
      checked: app.getLoginItemSettings().openAtLogin,
      click: (item) => app.setLoginItemSettings({ openAtLogin: item.checked, args: ["--hidden"] }),
    },
    { type: "separator" },
    { label: "Quit Dork Desktop", click: () => { quitting = true; app.quit(); } },
  ]);
  tray.setContextMenu(menu());
  tray.on("click", toggle);
  tray.on("right-click", () => tray.setContextMenu(menu()));
}

// ---------- app ----------

app.whenReady().then(() => {
  // Voice: let Dork's own page use the microphone (and play audio); nothing else gets it.
  const own = (url) => (url || "").startsWith(SERVER);
  session.defaultSession.setPermissionRequestHandler((wc, permission, cb) =>
    cb(own(wc.getURL()) && ["media", "audioCapture", "speaker-selection"].includes(permission)));
  session.defaultSession.setPermissionCheckHandler((wc, permission, origin) =>
    own(origin) && ["media", "audioCapture"].includes(permission));
  createAssistant();
  createWindow();
  buildTray();
  // First run: start with Windows, hidden (Dork listens in the background; voice brings it up).
  const flag = path.join(app.getPath("userData"), "login-item-set");
  if (app.isPackaged && !fs.existsSync(flag)) {
    app.setLoginItemSettings({ openAtLogin: true, args: ["--hidden"] });
    fs.writeFileSync(flag, "1");
  }
  if (startAssistant) assistant.once("ready-to-show", showAssistant);
  if (!globalShortcut.register(HOTKEY, toggleAssistant)) console.warn("Hotkey already in use:", HOTKEY);
  nativeTheme.on("updated", () => win && win.setTitleBarOverlay(overlayColors()));
});

app.on("will-quit", () => globalShortcut.unregisterAll());
app.on("window-all-closed", (e) => e.preventDefault()); // keep running in the tray
