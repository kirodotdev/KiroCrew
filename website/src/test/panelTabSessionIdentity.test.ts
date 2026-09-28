/**
 * A contributed panel tab mounts app code through `AppHost` and that code makes scoped
 * API calls. Two properties are asserted at the SOURCE, because both live in one JSX
 * expression and rendering the whole SidePanel would test the harness more than the
 * wiring — what must not regress is that each participates at all.
 *
 * 1. The host receives the OWNING slot's session key. Without it the backend's
 *    restricted-session guard fails open, so an app mounted in an incognito chat is
 *    allowed the persistent writes incognito exists to deny. It must be `tabSlot`, not
 *    `activeSlot`: with cross-slot hosting the body may belong to another chat.
 * 2. A failed `['apps']` request is SURFACED. The body resolves its app out of that list,
 *    so a failure otherwise renders the tab blank with no statement of why
 *    (`errors-use-error-notice`).
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const src = readFileSync(join(__dirname, '..', 'pages', 'chat', 'SidePanel.tsx'), 'utf-8')

describe('contributed panel tab body', () => {
  it('hands AppHost the owning slot as its session key', () => {
    expect(src).toContain('sessionKey={`dashboard:${slot}`}')
    // The slot threaded in is the TAB's, not the panel's active one.
    expect(src).toContain('slot={tabSlot}')
  })

  it('renders the app-list failure OUTSIDE the descriptor-pruned tab body', () => {
    // Inside the body it was unreachable in the very case it reports: no descriptors
    // means every contributed tab is pruned from the strip, `activeId` moves elsewhere,
    // and the body's wrapper goes to `display:none`. Panel level survives that.
    expect(src).toContain('<AppPanelTabsErrorNotice />')
    expect(src).toMatch(/<ErrorNotice message=\{errMessage\(error\)\} askAgent \/>/)
    // Above the bodies, which are `absolute inset-0` and would paint over a
    // normal-flow sibling; anchored to the top edge so it does not blanket the body.
    expect(src).toContain('absolute left-0 right-0 top-0 z-10')
    // Every failure is reported: a "does the reader have one stored" gate was a second
    // silence, because a first load has no contributed tab stored yet.
    expect(src).toMatch(/const \{ isError, error \} = useInstalledApps\(\)\n  if \(!isError\) return null/)
    // The notice must NOT sit inside the per-tab body any more.
    const bodyStart = src.indexOf('function AppPanelTabBody(')
    const bodyEnd = src.indexOf('function AppPanelTabsErrorNotice(')
    expect(bodyStart).toBeGreaterThan(-1)
    expect(bodyEnd).toBeGreaterThan(bodyStart)
    expect(src.slice(bodyStart, bodyEnd)).not.toContain('ErrorNotice')
  })

  it('uses the shared guarded apps observer rather than a second inline useQuery', () => {
    // A hand-rolled `['apps']` observer is how the unguarded `queryFn: api.listApps`
    // form comes back, and it breaks the composer's session controls rather than
    // anything here.
    expect(src).toContain('useInstalledApps()')
    expect(src).not.toMatch(/queryKey: \['apps'\]/)
  })
})

describe('AppHost session key plumbing', () => {
  const host = readFileSync(join(__dirname, '..', 'components', 'AppHost.tsx'), 'utf-8')

  it('forwards sessionKey through both layers to AppApiProvider', () => {
    // Three hops, and dropping any one of them fails open silently.
    expect(host).toMatch(/function AppHostInner\(\{[^}]*sessionKey[^}]*\}: AppHostProps\)/)
    expect(host).toMatch(/function AppHost\(\{[^}]*sessionKey[^}]*\}: AppHostProps\)/)
    expect(host).toContain('<AppHostInner app={app} entry={entry} active={active} sessionKey={sessionKey} />')
    expect(host).toContain('sessionKey={sessionKey}')
  })
})
