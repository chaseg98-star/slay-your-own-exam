// Slay Your Own Exam — Electron desktop wrapper.
//
// Loads the live web app (which is a PWA, so its service worker makes it work
// offline after the first successful load). If every remote URL fails — e.g. a
// fully offline first run — it falls back to a bundled copy of the site in
// ./site (copied there by CI at build time), and failing that shows a minimal
// inline "you're offline" page.

'use strict';

const { app, BrowserWindow, shell } = require('electron');
const fs = require('fs');
const path = require('path');

// Remote app URLs, tried in order.
const APP_URLS = [
  'https://cg1k.github.io/slay-your-own-exam/',
  'https://slayyourexam.web.app/',
];

// Hosts allowed to load/navigate inside the app window. Anything else opens
// in the system browser instead.
const ALLOWED_HOSTS = new Set([
  'cg1k.github.io',
  'slayyourexam.web.app',
  'localhost',
  '127.0.0.1',
]);

const BUNDLED_INDEX = path.join(__dirname, 'site', 'index.html');

const OFFLINE_PAGE =
  'data:text/html;charset=utf-8,' +
  encodeURIComponent(
    '<!doctype html><html><head><meta charset="utf-8">' +
      '<title>Slay Your Own Exam</title>' +
      '<style>body{font-family:system-ui,sans-serif;display:flex;align-items:center;' +
      'justify-content:center;height:100vh;margin:0;background:#111;color:#eee;text-align:center}' +
      'div{max-width:32rem;padding:2rem}h1{font-size:1.4rem}</style></head>' +
      '<body><div><h1>You&#39;re offline</h1>' +
      '<p>Connect to the internet once to set up. After the first load, ' +
      'Slay Your Own Exam works offline.</p></div></body></html>'
  );

/** True when a URL may stay inside the app window. */
function isAllowedInApp(rawUrl) {
  let url;
  try {
    url = new URL(rawUrl);
  } catch {
    return false;
  }
  if (url.protocol === 'file:') return true; // bundled fallback site
  if (url.protocol === 'https:' || url.protocol === 'http:') {
    return ALLOWED_HOSTS.has(url.hostname);
  }
  return false;
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 850,
    minWidth: 900,
    minHeight: 600,
    title: 'Slay Your Own Exam',
    icon: path.join(__dirname, 'build', 'icon.png'),
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  // --- URL fallback chain -------------------------------------------------
  let urlIndex = 0;
  let fallbackShown = false;

  const loadNext = () => {
    if (urlIndex < APP_URLS.length) {
      win.loadURL(APP_URLS[urlIndex++]);
    } else if (!fallbackShown) {
      fallbackShown = true;
      if (fs.existsSync(BUNDLED_INDEX)) {
        win.loadFile(BUNDLED_INDEX);
      } else {
        win.loadURL(OFFLINE_PAGE);
      }
    }
  };

  win.webContents.on(
    'did-fail-load',
    (_event, errorCode, _errorDescription, _validatedURL, isMainFrame) => {
      if (!isMainFrame) return;
      if (errorCode === -3) return; // ERR_ABORTED (e.g. superseded navigation)
      loadNext();
    }
  );

  // --- Keep the window on app hosts; everything else -> system browser ----
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (isAllowedInApp(url)) {
      win.loadURL(url); // keep single-window app
    } else if (/^https?:/i.test(url)) {
      shell.openExternal(url);
    }
    return { action: 'deny' };
  });

  win.webContents.on('will-navigate', (event, url) => {
    if (isAllowedInApp(url)) return;
    event.preventDefault();
    if (/^https?:/i.test(url)) {
      shell.openExternal(url);
    }
  });

  loadNext();
  return win;
}

// --- Single instance ------------------------------------------------------
const gotTheLock = app.requestSingleInstanceLock();

if (!gotTheLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    const [win] = BrowserWindow.getAllWindows();
    if (win) {
      if (win.isMinimized()) win.restore();
      win.focus();
    }
  });

  app.whenReady().then(() => {
    createWindow();

    app.on('activate', () => {
      // macOS: re-create a window when the dock icon is clicked.
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });

  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
  });
}
