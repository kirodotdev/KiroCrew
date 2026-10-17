/**
 * Screenshot harness for the desktop "installation still finishing" dialog's
 * auto-retry states (#4757).
 *
 * The dialog is a BrowserWindow that loads a data: URL the gateway supervisor
 * builds, then repaints its message through webContents.executeJavaScript on
 * every bundle probe. There is no Electron runtime in CI or on a dev desk, so
 * this harness runs the REAL supervisor (website/electron/gateway-supervisor.js)
 * against a fake BrowserWindow that records the data: URL and every script the
 * supervisor emits, and renders those verbatim in Chromium at the window's own
 * content size. Nothing here is mocked at the UI layer: the HTML, the copy and
 * the repaint scripts are the supervisor's own bytes.
 *
 * Scenes, each in light and dark:
 *   1. first paint -- the pre-spawn refusal with 3 components missing
 *   2. one probe later -- two parts landed, "1 component is" repainted in place
 *   3. complete -- "Installation finished — starting Kiro Crew…" (the linger
 *      frame before the dialog closes itself)
 *
 * Usage: node scripts/capture-installing-dialog.mjs [outDir]
 */
import { chromium } from 'playwright'
import { EventEmitter } from 'node:events'
import { mkdirSync } from 'node:fs'
import { createRequire } from 'node:module'
import path from 'node:path'

const require = createRequire(import.meta.url)
const { createGatewaySupervisor } = require('../electron/gateway-supervisor.js')
const { REQUIRED_STDLIB_PARTS } = require('../electron/bundle-integrity.js')

const OUT = process.argv[2] || '../temp-screenshots/installing-dialog-auto-retry'
mkdirSync(OUT, { recursive: true })

const PROBE_MS = 5000
const BUNDLE_ROOT = '/virtual/resources/backend-dist/kirocrew-backend-x64'
const BUNDLE_BIN = `${BUNDLE_ROOT}/bin/kirocrew`
const BUNDLE_LIB = `${BUNDLE_ROOT}/lib/python3.12`

function bundleFiles(missing) {
  const files = new Set([BUNDLE_BIN, `${BUNDLE_ROOT}/bin`, `${BUNDLE_ROOT}/lib`, BUNDLE_LIB])
  for (const part of REQUIRED_STDLIB_PARTS) {
    if (!missing.includes(part)) files.add(`${BUNDLE_LIB}/${part}/__init__.py`)
  }
  return files
}

function fakeFs(files) {
  const dirs = {
    [`${BUNDLE_ROOT}/bin`]: ['python3', 'kirocrew'],
    [`${BUNDLE_ROOT}/lib`]: ['python3.12'],
  }
  return {
    constants: { X_OK: 1 },
    mkdirSync() {},
    accessSync(file) {
      if (files.has(file)) return
      throw Object.assign(new Error('not found'), { code: 'ENOENT' })
    },
    existsSync: file => files.has(file),
    readdirSync(dir) {
      if (!dirs[dir]) throw Object.assign(new Error('ENOENT'), { code: 'ENOENT' })
      return dirs[dir]
    },
    openSync: () => 41,
    closeSync() {},
    readFileSync() { throw new Error('no launch log yet') },
  }
}

// Boots the supervisor into the dialog for one theme and returns the recorded
// window plus a `tick()` that runs the supervisor's own probe interval once.
async function openDialog(dark) {
  const files = bundleFiles(['urllib', 'zipfile', 'zoneinfo'])
  const intervals = []
  let dialog = null
  class FakeBrowserWindow {
    constructor(options) {
      this.options = options
      this.handlers = {}
      this.destroyed = false
      this.url = null
      this.scripts = []
      this.webContents = {
        executeJavaScript: js => { this.scripts.push(js); return Promise.resolve() },
      }
      dialog = this
    }
    setMenu() {}
    on(event, fn) { (this.handlers[event] ||= []).push(fn) }
    loadURL(url) { this.url = url }
    isDestroyed() { return this.destroyed }
    close() {
      if (this.destroyed) return
      this.destroyed = true
      for (const fn of this.handlers.closed || []) fn()
    }
  }
  let spawned = 0
  const window = {
    isDestroyed: () => spawned > 0,
    show() {}, focus() {}, isMinimized: () => false, restore() {},
    webContents: { loadFile() {}, send() {} },
  }
  const supervisor = createGatewaySupervisor({
    app: { isPackaged: true, getVersion: () => '0.8.0', quit() {}, focus() {}, show() {} },
    store: { get: (_k, fallback) => fallback, set() {} },
    BrowserWindow: FakeBrowserWindow,
    nativeTheme: { shouldUseDarkColors: dark },
    dialog: { showMessageBox: async () => ({ response: 1 }) },
    shell: { showItemInFolder() {} },
    ipcMain: { on() {}, removeListener() {} },
    port: 5476,
    home: '/virtual/kirocrew-home',
    getMainWindow: () => window,
    isQuitting: () => false,
    requestQuit() {},
    log() {}, warn() {}, error() {},
    logPath: () => '/virtual/logs/gateway-launch.log',
    fsMod: fakeFs(files),
    osMod: { homedir: () => '/virtual/home' },
    pathMod: path.posix,
    httpMod: {
      get() {
        const request = new EventEmitter()
        request.destroy = () => {}
        queueMicrotask(() => request.emit('error', new Error('connection refused')))
        return request
      },
    },
    spawnFn: () => { spawned += 1; const child = { pid: 1, exitCode: null, on() {}, kill() {}, unref() {} }; return child },
    execFileFn() { throw new Error('execFile must not run here') },
    execFileSyncFn() { throw new Error('execFileSync must not run here') },
    setTimeoutFn: (fn, ms) => ({ fn, ms }),
    clearTimeoutFn() {},
    setIntervalFn: (fn, ms) => { intervals.push({ fn, ms }); return intervals.length },
    clearIntervalFn(id) { intervals[id - 1] = null },
    processRef: {
      platform: 'linux', arch: 'x64', env: { KIROCREW_HOME: '/virtual/kirocrew-home' },
      resourcesPath: '/virtual/resources', kill() {},
    },
    dirname: '/virtual/electron',
  })
  const started = await supervisor.start()
  if (started !== false) throw new Error('expected the incomplete bundle to be refused before spawn')
  void supervisor.connect(window)
  await new Promise(resolve => setImmediate(resolve))
  if (!dialog || !dialog.url) throw new Error('the failure dialog did not open')
  const probe = intervals.find(i => i && i.ms === PROBE_MS)
  if (!probe) throw new Error('no probe interval was armed for the installing dialog')
  return { dialog, files, tick: () => probe.fn() }
}

async function capture(dark) {
  const theme = dark ? 'dark' : 'light'
  const { dialog, files, tick } = await openDialog(dark)
  const browser = await chromium.launch()
  const page = await browser.newPage({
    viewport: { width: dialog.options.width, height: dialog.options.height },
    colorScheme: theme,
  })
  await page.goto(dialog.url)
  const shoot = async (name, expect) => {
    const text = await page.locator('.msg').innerText()
    if (!expect.test(text)) throw new Error(`${name}: message did not match ${expect}: ${text}`)
    await page.screenshot({ path: `${OUT}/${name}-${theme}.png` })
    console.log(`wrote ${OUT}/${name}-${theme}.png`)
  }
  await shoot('1-installing-3-remaining', /3 components are[\s\S]*starts on its own/)

  // Two parts land; the supervisor's probe repaints the count in place.
  files.add(`${BUNDLE_LIB}/urllib/__init__.py`)
  files.add(`${BUNDLE_LIB}/zipfile/__init__.py`)
  tick()
  await page.evaluate(dialog.scripts.at(-1))
  await shoot('2-installing-1-remaining', /1 component is/)

  // The last part lands; the completion frame is painted (message, title,
  // buttons), and the window lingers on it.
  files.add(`${BUNDLE_LIB}/zoneinfo/__init__.py`)
  const before = dialog.scripts.length
  tick()
  for (const script of dialog.scripts.slice(before)) await page.evaluate(script)
  const title = await page.locator('.title').innerText()
  if (!/installation finished/.test(title)) throw new Error(`title still says finishing: ${title}`)
  await shoot('3-installation-finished', /Installation finished/)

  await browser.close()
}

await capture(false)
await capture(true)
