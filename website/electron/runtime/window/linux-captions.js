"use strict";

/**
 * Caption controls for frameless Linux, where no window manager draws them.
 * The minimize / maximize / close buttons are CSS-drawn so distro fonts cannot
 * shift their glyphs, and their actions cross the allowlisted preload IPC
 * (window.kirocrew.windowControl). The maximize button tracks the window's
 * real state, since a double-click or a WM shortcut can change it too.
 *
 * The injected CSS and script literals are whitespace-normalized: their
 * indentation follows this file's nesting and carries no meaning to the page,
 * and test/window-lifecycle.test.js records each one by its first trimmed line.
 */

function syncLinuxMaximizeState(win, view) {
  const push = () => {
    if (win.isDestroyed() || view.webContents.isDestroyed()) return;
    const maxed = win.isMaximized();
    view.webContents.executeJavaScript(`
      {
        const wrap = document.getElementById('electron-linux-controls');
        if (wrap) {
          wrap.classList.toggle('is-maximized', ${maxed});
          const b = wrap.querySelector('button.maximize');
          if (b) b.setAttribute('aria-label', ${maxed} ? 'Restore' : 'Maximize');
        }
      }
    `).catch(() => {});
  };
  // did-finish-load re-fires on reload. Window listeners must be armed once.
  if (!win._mcLinuxMaximizeSyncArmed) {
    win._mcLinuxMaximizeSyncArmed = true;
    win.on("maximize", push);
    win.on("unmaximize", push);
  }
  push();
}

/**
 * Inject the caption controls into a frameless Linux dashboard. Runs on every
 * did-finish-load, because a reload replaces the document; the window's own
 * maximize listeners are armed only once.
 */
function injectLinuxCaptionControls(win, view) {
  view.webContents.insertCSS(`
    #electron-linux-controls {
      position: fixed;
      top: 0; right: 0;
      height: 42px;
      display: flex;
      align-items: stretch;
      z-index: 100000;
      -webkit-app-region: no-drag;
    }
    #electron-linux-controls button {
      position: relative;
      width: 36px;
      border: 0;
      background: transparent;
      color: var(--text, #e2e8f0);
      opacity: 0.55;
      cursor: default;
      -webkit-app-region: no-drag;
    }
    #electron-linux-controls button:hover {
      opacity: 1;
      background: rgba(128,128,128,0.18);
    }
    #electron-linux-controls button.close:hover {
      background: #e81123;
      color: #fff;
    }
    #electron-linux-controls button::before {
      content: "";
      position: absolute;
      top: 50%; left: 50%;
      transform: translate(-50%, -50%);
    }
    #electron-linux-controls button.minimize::before {
      width: 10px; height: 0;
      border-top: 1px solid currentColor;
    }
    #electron-linux-controls button.maximize::before {
      width: 9px; height: 9px;
      border: 1px solid currentColor;
    }
    #electron-linux-controls.is-maximized button.maximize::before {
      width: 7px; height: 7px;
      transform: translate(-70%, -30%);
    }
    #electron-linux-controls.is-maximized button.maximize::after {
      content: "";
      position: absolute;
      top: 50%; left: 50%;
      width: 7px; height: 7px;
      transform: translate(-30%, -70%);
      border: 1px solid currentColor;
      border-bottom: 0;
      border-left: 0;
    }
    #electron-linux-controls button.close::before {
      width: 12px; height: 0;
      border-top: 1px solid currentColor;
      transform: translate(-50%, -50%) rotate(45deg);
    }
    #electron-linux-controls button.close::after {
      content: "";
      position: absolute;
      top: 50%; left: 50%;
      width: 12px; height: 0;
      border-top: 1px solid currentColor;
      transform: translate(-50%, -50%) rotate(-45deg);
    }
  `);
  view.webContents.executeJavaScript(`
    if (!document.getElementById('electron-linux-controls')) {
      const wrap = document.createElement('div');
      wrap.id = 'electron-linux-controls';
      const mk = (cls, label, action) => {
        const button = document.createElement('button');
        button.className = cls;
        button.setAttribute('aria-label', label);
        // Native caption controls are not in the tab order either.
        button.tabIndex = -1;
        button.addEventListener(
          'click',
          () => window.kirocrew?.windowControl?.(action),
        );
        return button;
      };
      wrap.append(
        mk('minimize', 'Minimize', 'minimize'),
        mk('maximize', 'Maximize', 'maximize-toggle'),
        mk('close', 'Close', 'close'),
      );
      document.body.prepend(wrap);
    }
  `);
  syncLinuxMaximizeState(win, view);
}

module.exports = { injectLinuxCaptionControls };
