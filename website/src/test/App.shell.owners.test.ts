/**
 * The app shell's composition contract.
 *
 * `App.tsx` is the composition root: the route table, the chrome markup the
 * read-only layout tests pin there, and the calls into the owners under
 * `src/shell/`. React runs a component's effects in declaration order, and a
 * custom hook's effects run where the hook is called, so the ORDER of those
 * calls is the order the shell's effects run in — the boot reads, the listener
 * registrations and the platform calls. Each call sits at the position of the
 * inline block it replaced; four independent effects changed places (the macOS
 * fullscreen subscription, the Developer page's seen mark, the dock badge, and
 * the update modal's auto-update refetch, which does nothing until the modal
 * opens). This pins that order, the facade's public exports, and the
 * one-way edge: owners never import the facade, and nothing outside the shell
 * imports an owner.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import * as AppModule from '../App'
import { RailHeaderGlyph } from '../shell/nav/railChrome'
import { metricColor } from '../utils/metricColor'

const SRC = join(__dirname, '..')
const APP = readFileSync(join(SRC, 'App.tsx'), 'utf8')

function* walk(dir: string): Generator<string> {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) { yield* walk(p); continue }
    if (/\.(ts|tsx)$/.test(name) && !/\.(test|stories)\./.test(name)) yield p
  }
}

/** Every module specifier `src` names: static and side-effect imports, re-exports, dynamic imports. */
const importsOf = (src: string) => [
  ...src.matchAll(/^(?:import|export)\s[^'"]*?\sfrom\s*['"]([^'"]+)['"]/gm),
  ...src.matchAll(/^import\s*['"]([^'"]+)['"]/gm),
  ...src.matchAll(/\bimport\(\s*['"]([^'"]+)['"]\s*\)/g),
].map(m => m[1])

describe('app shell composition', () => {
  it('calls the owner hooks at the positions of the inline blocks they replaced', () => {
    const order = [
      'useConfigAutolinkRules()',
      'useGlobalApprovalCount()',
      'useTerminalRestoreProbe()',
      'useMobileConnect(location.key)',
      'useTheme()',
      'useFirstRunChapters({',
      'useUpdateSubscription()',
      'useShellBranding({',
      'useRumPageView()',
      'useNotificationSound()',
      'useMacFullscreen()',
      'useFocusChrome({',
      'useDrawerSwipe(shellRef, {',
      'useAppRailOrder(appNavItems)',
      'useRailBadges(approvalCount)',
      'useShellKeyboard({',
      'useKiroUsageReadout()',
      'useMetricsReadout(isMobile, updateAvailable, topbarLevels.right)',
      'useDeveloperMode(location.pathname)',
      'subscribeNativeNavigate(',
      'dispatch(fetchSlots())',
      'useWebSocket()',
      'useDashboardHealthProbe(forceReconnect)',
      'useUpdateFlow()',
      'useStartupVideo({',
      'useNativeNotification(botName, avatar)',
      'useRequestFeature(colorTheme)',
      'useRouteActiveModel(',
    ]
    const at = order.map(call => {
      const i = APP.indexOf(call)
      expect(i, `App.tsx no longer calls ${call}`).toBeGreaterThan(-1)
      expect(APP.indexOf(call, i + 1), `${call} is called twice`).toBe(-1)
      return i
    })
    expect([...at].sort((a, b) => a - b)).toEqual(at)
  })

  it('installs the WebSocket exactly once, in the shell', () => {
    const installs = [...walk(SRC)].filter(f => readFileSync(f, 'utf8').includes('useWebSocket()'))
      .map(f => relative(SRC, f).replaceAll('\\', '/'))
      .filter(f => f !== 'hooks/useWebSocket.ts')
    expect(installs).toEqual(['App.tsx'])
  })

  it('keeps the facade exports and their identities', () => {
    expect(Object.keys(AppModule).sort()).toEqual([
      'MobileNavGlyph', 'NavBadge', 'NavItem', 'RailHeaderGlyph', 'UpdateOverlay', 'WsContext',
      'default', 'memColorClass', 'metricColor',
    ])
    expect(AppModule.RailHeaderGlyph).toBe(RailHeaderGlyph)
    expect(AppModule.metricColor).toBe(metricColor)
    expect(AppModule.memColorClass).toBe(metricColor)
  })

  it('owners never import the facade, and only the shell imports an owner', () => {
    const shellDir = join(SRC, 'shell')
    const importers: string[] = []
    for (const file of walk(SRC)) {
      const rel = relative(SRC, file).replaceAll('\\', '/')
      const specs = importsOf(readFileSync(file, 'utf8'))
      if (file.startsWith(shellDir)) {
        for (const s of specs) expect(s, `${rel} imports the App facade`).not.toMatch(/(^|\/)App$/)
        continue
      }
      if (specs.some(s => /(^|\/)shell\//.test(s) && s.startsWith('.'))) importers.push(rel)
    }
    expect(importers).toEqual(['App.tsx'])
  })
})
