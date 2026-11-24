/**
 * The guard and the readers that the gateway puts into a panel's page (`browser_harness/panel_page.py`, `GUARD_JS`),
 * taken from the gateway's own source and run in the test's document. A test that renders a real surface can then
 * ask the gateway's own reader what it says of it, so the marks and the reader cannot drift apart unseen.
 */
import { readFileSync } from 'node:fs'

const SOURCE = '../src/kiro_crew/browser_harness/panel_page.py'

export interface PanelFocus { editable: boolean; kind: string; rect: number[] | null; guarded: boolean }
export interface PanelGuard {
  focus(): PanelFocus
  target(x: number, y: number): { rect: number[]; kind: string; label: string; guarded: boolean } | null
}

/** The guard script as the gateway builds it. */
export function panelGuardSource(): string { return panelScriptSource('GUARD_JS') }

/** One of the gateway's scripts for a panel's page: adjacent Python string literals and named constants, joined. A line that is a Python comment is left out. */
export function panelScriptSource(name: string): string {
  const py = readFileSync(SOURCE, 'utf-8')
  const names: Record<string, string> = {}
  for (const m of py.matchAll(/^([A-Z_]+) = "([^"\n]*)"$/gm)) names[m[1]] = m[2]
  const head = `\n${name} = (`
  const start = py.indexOf(head)
  const end = py.indexOf('\n)\n', start)
  if (start < 0 || end < 0) throw new Error(`no ${name} in ${SOURCE}`)
  let js = ''
  for (const m of py.slice(start + head.length, end).replace(/^\s*#.*$/gm, '').matchAll(/"((?:[^"\\\n]|\\.)*)"|'((?:[^'\\\n]|\\.)*)'|([A-Z_]+)|\+|\s+/g)) {
    const text = m[1] ?? m[2]
    if (text !== undefined) js += text.replace(/\\(.)/g, '$1')
    else if (m[3]) {
      if (!(m[3] in names)) throw new Error(`${name} names ${m[3]}, which ${SOURCE} does not define as a plain string`)
      js += names[m[3]]
    }
  }
  return js
}

type Holder = { __kcPanel?: PanelGuard }

/** Run the script in this document and hand back its readers. Call `removePanelGuard` when the test is done. */
export function installPanelGuard(): PanelGuard {
  const holder = globalThis as Holder
  delete holder.__kcPanel
  // The script is the gateway's own, read from this repository; it is run as the gateway's browser runs it.
  new Function(panelGuardSource())()
  const guard = holder.__kcPanel
  if (!guard) throw new Error('the guard script did not install its readers')
  return guard
}

export function removePanelGuard(): void {
  delete (globalThis as Holder).__kcPanel
}
