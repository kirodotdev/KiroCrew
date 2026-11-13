/**
 * Which app bundles this document has imported, and which of those the gateway
 * has since replaced.
 *
 * The browser's ESM module map is keyed by URL and keeps every module for the
 * life of the document, so importing an app's entry again after an install,
 * update or Sync returns the module already loaded. The gateway announces new
 * files with `mc:app-reload` (the `app_reload` frame). A mounted AppHost reloads
 * the window on it; an announcement that arrives while no host shows the app is
 * recorded here, so the next mount reloads instead of rendering the old module.
 */

const imported = new Set<string>()
const replaced = new Set<string>()
let listening = false

function onAppReload(e: Event): void {
  const app = (e as CustomEvent<{ app?: unknown } | undefined>).detail?.app
  if (typeof app === 'string' && imported.has(app)) replaced.add(app)
}

/** Record that this document is importing `app`'s UI bundle. */
export function noteAppBundleImport(app: string): void {
  imported.add(app)
  if (listening) return
  window.addEventListener('mc:app-reload', onAppReload)
  listening = true
}

/** Whether the gateway replaced `app`'s files after this document imported its bundle. */
export function appBundleReplacedSinceImport(app: string): boolean {
  return replaced.has(app)
}

export function __resetAppBundleFreshnessForTests(): void {
  imported.clear()
  replaced.clear()
  if (listening) window.removeEventListener('mc:app-reload', onAppReload)
  listening = false
}
