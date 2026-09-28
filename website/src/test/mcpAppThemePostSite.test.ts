/**
 * Property 14: one outbound post site on the theme path.
 *
 * SEP-1865 theming reaches a mounted app as a `host-context-changed`
 * notification, and the frame is deliberately built to post that from a SINGLE
 * place — `notifyHostContext` — which already carries the navigated-away guard
 * and the wildcard-origin (null-origin sandbox) review annotation. A second
 * outbound post site would mean re-auditing a null-origin target and could
 * drift from that guard, so the theme-change effect routes THROUGH
 * `notifyHostContext` rather than calling `postMessage` itself.
 *
 * This is a source-level pin in the same shape as the repo's other "no second
 * call site" guards (e.g. ArtifactsPage.dragSensors, AgentSelector.dialog): the
 * mechanism is a structural property of the file, not something a rendered test
 * can demonstrate, so it is asserted against the source text at the one place
 * where it is decided.
 *
 * Validates: Requirements 4.6
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { join } from 'node:path'

const SRC = readFileSync(
  join(__dirname, '..', 'components', 'McpAppFrame.tsx'),
  'utf8',
)

describe('McpAppFrame keeps one outbound post site on the theme path', () => {
  it('has exactly two postMessage call sites: the inbound bridge and notifyHostContext', () => {
    // The whole file reaches `postMessage` twice: the inbound `post()` helper
    // (`cw.postMessage(out, '*')`) that answers app requests/notifications, and
    // the single outbound `notifyHostContext` (`cw.postMessage(...)`). No third
    // site may appear — a new one on the theme path is exactly the regression
    // this pins.
    const sites = SRC.match(/\.postMessage\(/g) ?? []
    expect(sites.length).toBe(2)
  })

  it('routes the theme-change effect through notifyHostContext, not its own postMessage', () => {
    // The theme path is the useEffect gated on `sentThemeKeyRef` / keyed on
    // `themeContextKey(theme, styleVars)`. Slice its body from the ref decl to
    // the effect's dependency array and assert it posts via the single site
    // rather than reaching `postMessage` directly.
    const start = SRC.indexOf('const sentThemeKeyRef')
    expect(start).toBeGreaterThan(-1)
    const rest = SRC.slice(start)
    // The effect closes with `}, [theme, styleVars, notifyHostContext])`.
    const end = rest.indexOf('notifyHostContext])')
    expect(end).toBeGreaterThan(-1)
    const themeEffect = rest.slice(0, end + 'notifyHostContext])'.length)

    // The theme path reaches the outbound site only through the helper...
    expect(themeEffect).toContain('notifyHostContext(')
    // ...and never carries its own post.
    expect(themeEffect).not.toContain('postMessage')
  })

  it('keeps the single outbound post inside notifyHostContext', () => {
    // The one outbound `postMessage` lives in `notifyHostContext`. Slice that
    // callback and confirm the outbound site is the one it owns.
    const start = SRC.indexOf('const notifyHostContext = useCallback(')
    expect(start).toBeGreaterThan(-1)
    // The callback ends at its own `}, [])` dependency array.
    const end = SRC.indexOf('}, [])', start)
    expect(end).toBeGreaterThan(-1)
    const body = SRC.slice(start, end)
    expect((body.match(/\.postMessage\(/g) ?? []).length).toBe(1)
  })
})
