/**
 * A refused page registration has to be visible where a user meets it.
 *
 * The failure this locks down shipped: a downstream edition registered ten
 * builtin pages the old way, every one was refused for a missing `appId`, and
 * each refused route then fell through `BuiltinAppRoute`'s miss path to
 * `Navigate to="/chat"`. Clicking any of the ten silently landed on chat. The
 * only trace was a `console.warn` in a production bundle.
 *
 * The seam's warn-in-production policy is right and stays: throwing at
 * registration would take the whole dashboard down over one bad entry. What was
 * wrong is that the refusal was UNREADABLE afterwards. So `builtinRegistry.ts`
 * records the refusal under its route, and the route renders it — named, in
 * plain language, with the registration diagnostic kept below and no retry
 * button, since a refusal cannot be retried away.
 *
 * The record is LOCAL to `builtinRegistry.ts`, not part of `seamCollision.ts`:
 * remembering a refusal is only useful to a seam with somewhere to show it, and
 * this is the only one whose refused keys stay navigable.
 *
 * `import.meta.env.DEV` is TRUE under vitest, so the production branch is
 * reachable here only by stubbing it — which these tests do, because production
 * is the only build where a refusal survives registration at all.
 *
 * Every route below is unique to its own case: the refusal record is
 * module-level and keyed by route, so shared route names would let one case
 * answer another.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { lazy, useEffect } from 'react'
import { render, screen, act } from '@testing-library/react'
import { MemoryRouter, Routes, Route, useNavigate } from 'react-router-dom'

import BuiltinAppRoute from '../apps/BuiltinAppRoute'
import {
  builtinRefusalReason,
  getBuiltinApp,
  hasBuiltinComponent,
  registerBuiltinComponents,
  type BuiltinAppEntry,
} from '../apps/builtinRegistry'
import { reportSeamCollision } from '../apps/seamCollision'
import { findReport } from '../utils/errorReport'

const dummy = () => lazy(async () => ({ default: () => null }))

/** A registrable page that renders one identifiable node. */
const pageWithTestId = (testId: string) =>
  lazy(async () => ({ default: () => <div data-testid={testId} /> }))

/** A registrable page that throws on render, to arm the route-level boundary. */
const crashingPage = (message: string) =>
  lazy(async () => ({ default: (): never => { throw new Error(message) } }))

/**
 * Real navigation inside ONE router, which is the condition the defect needs.
 *
 * `App.tsx` serves every builtin page from a single unkeyed
 * `<Route path="/:builtinApp/*" element={<BuiltinAppRoute />} />`, so a
 * builtin→builtin move is a PARAM CHANGE on a mounted component, not a fresh
 * mount. Re-rendering with different `initialEntries` would not reproduce that
 * (MemoryRouter reads them once), and a mocked `useNavigate` would not move the
 * router at all — so these tests drive the real one.
 */
let navigateTo: ((to: string) => void) | null = null
function NavHarness() {
  const navigate = useNavigate()
  useEffect(() => { navigateTo = navigate }, [navigate])
  return null
}

function renderAtPath(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <NavHarness />
      <Routes>
        <Route path="/:builtinApp" element={<BuiltinAppRoute />} />
        <Route path="/chat" element={<div data-testid="chat-page">Chat</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

/**
 * The same render under App.tsx's actual pattern, `/:builtinApp/*`.
 *
 * `renderAtPath` uses the single-segment form, which cannot match a pathname the
 * router leaves with two segments — the case a malformed escape produces, since
 * `decodePath` then re-encodes nothing and the second segment stays. Only the
 * splat pattern reaches `BuiltinAppRoute` there, so the multi-segment cases have
 * to go through this one to be tested end to end at all.
 */
function renderUnderSplatAtPath(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <NavHarness />
      <Routes>
        <Route path="/:builtinApp/*" element={<BuiltinAppRoute />} />
        <Route path="/chat" element={<div data-testid="chat-page">Chat</div>} />
      </Routes>
    </MemoryRouter>,
  )
}

describe('a refused registration is recorded', () => {
  let warn: ReturnType<typeof vi.spyOn>
  beforeEach(() => {
    vi.stubEnv('DEV', false)
    warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
  })
  afterEach(() => {
    vi.unstubAllEnvs()
    vi.restoreAllMocks()
  })

  it('records the reason a missing appId was refused, and still does not register', () => {
    const route = '/zzq-refusal-missing-appid'
    registerBuiltinComponents({ [route]: { component: dummy() } as unknown as BuiltinAppEntry })

    expect(hasBuiltinComponent(route)).toBe(false)
    expect(builtinRefusalReason(route)).toMatch(/not a valid app id/)
    // Degrade-safe is unchanged: production warns rather than throwing.
    expect(warn).toHaveBeenCalled()
  })

  it('records a malformed route under the segment the router resolves, still naming the full route', () => {
    // This assertion USED to read the raw registration string back, which is
    // what hid the defect: nothing can ask for `/zzq-refusal/nested`, so a
    // reason filed there is unreadable and the route still vanished into chat.
    // The key is the leading segment; the MESSAGE still names the whole route,
    // which is what makes the refusal actionable for whoever registered it.
    const route = '/zzq-refusal/nested'
    registerBuiltinComponents({ [route]: { component: dummy(), appId: 'zzq-refusal-nested' } })

    expect(builtinRefusalReason(route)).toBeUndefined()
    const reason = builtinRefusalReason('/zzq-refusal')
    expect(reason).toMatch(/not a single plain path segment/)
    expect(reason).toContain(route)
  })

  it('keeps the FIRST reason when one route is refused twice', () => {
    // A retry does not explain why the route is empty any better than the
    // original refusal, and a later report must not overwrite the first.
    const route = '/zzq-refusal-twice'
    registerBuiltinComponents({ [route]: { component: dummy(), appId: 'BAD' } as BuiltinAppEntry })
    const first = builtinRefusalReason(route)
    registerBuiltinComponents({ [route]: { component: dummy(), appId: '' } as BuiltinAppEntry })

    expect(first).toMatch(/"BAD"/)
    expect(builtinRefusalReason(route)).toBe(first)
  })

  it('records nothing when the SHARED reporter is called directly — the negative control', () => {
    // The record lives in `builtinRegistry.ts`, written by `refuseBadEntry`, not
    // in `reportSeamCollision`. So a seam that reports a collision — including
    // this registry's own duplicate-route branch — warns and records nothing,
    // which is what keeps a lost duplicate from claiming a route is empty.
    reportSeamCollision('builtinRegistry', 'route /zzq-unkeyed already registered; ignoring duplicate')

    expect(builtinRefusalReason('/zzq-unkeyed')).toBeUndefined()
  })

  it('does not record a duplicate route, because the route still resolves', () => {
    const route = '/zzq-refusal-duplicate'
    const first = dummy()
    registerBuiltinComponents({ [route]: { component: first, appId: 'zzq-refusal-duplicate' } })
    registerBuiltinComponents({ [route]: { component: dummy(), appId: 'zzq-refusal-duplicate' } })

    expect(getBuiltinApp(route)?.component).toBe(first)
    expect(builtinRefusalReason(route)).toBeUndefined()
  })
})

describe('a refused route renders the reason instead of redirecting', () => {
  beforeEach(() => {
    vi.stubEnv('DEV', false)
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    vi.spyOn(console, 'error').mockImplementation(() => {})
  })
  afterEach(() => {
    vi.unstubAllEnvs()
    vi.restoreAllMocks()
  })

  it('shows the refusal on the page the user asked for', () => {
    const route = '/zzq-visible-refused'
    registerBuiltinComponents({ [route]: { component: dummy() } as unknown as BuiltinAppEntry })
    expect(hasBuiltinComponent(route)).toBe(false)

    renderAtPath(route)

    // The user stays where they navigated, and reads why it is empty.
    expect(screen.queryByTestId('chat-page')).not.toBeInTheDocument()
    // Named, so the reader can tell which page they are on — nothing in the
    // sidebar is active on a refused route.
    expect(screen.getByText(`${route} could not load`)).toBeInTheDocument()
    // A plain sentence leads, addressed to whoever is looking at the page.
    expect(screen.getByText(/rejected this page when it started/)).toBeInTheDocument()
    // And the registration diagnostic is still there, below it, under a label
    // that says who it is for. Dropping it would leave the page with nothing
    // the person who CAN fix this could act on.
    expect(screen.getByText('Details for whoever assembled this copy')).toBeInTheDocument()
    expect(screen.getByText(/not a valid app id/)).toBeInTheDocument()
  })

  it('still redirects a route nobody ever registered — the negative control', () => {
    // Without this, "render the reason" could be "never redirect", and a typo or
    // a stale link would show a crash card instead of going to chat.
    renderAtPath('/zzq-never-registered-at-all')

    expect(screen.getByTestId('chat-page')).toBeInTheDocument()
  })
})

describe('the dev/test build still fails loud at registration', () => {
  // vitest runs with DEV true, so no stub here: this is the unmodified path.
  it('throws, and records the same reason a production run would', () => {
    const route = '/zzq-refusal-dev-throw'
    expect(() =>
      registerBuiltinComponents({ [route]: { component: dummy() } as unknown as BuiltinAppEntry }),
    ).toThrow(/not a valid app id/)

    expect(hasBuiltinComponent(route)).toBe(false)
    expect(builtinRefusalReason(route)).toMatch(/not a valid app id/)
  })
})

/**
 * A refusal is recorded under the key the ROUTER will ask for, not the string it
 * was registered with.
 *
 * `BuiltinAppRoute` resolves one path parameter, so a multi-segment, query- or
 * hash-shaped registration is looked up as its leading segment alone. Recording
 * the raw registration string instead left a reason nothing could read: exactly
 * the refusal class this file's own registrar comment enumerates stayed as
 * silent as it was before the feature existed.
 */
describe('a refusal is recorded under the segment the router resolves', () => {
  beforeEach(() => {
    vi.stubEnv('DEV', false)
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    vi.spyOn(console, 'error').mockImplementation(() => {})
  })
  afterEach(() => {
    vi.unstubAllEnvs()
    vi.restoreAllMocks()
  })

  it('shows the reason for a multi-segment route at its leading segment', () => {
    registerBuiltinComponents({
      '/zzq-nested-refused/daily': { component: dummy(), appId: 'zzq-nested-refused' },
    })

    renderAtPath('/zzq-nested-refused')

    expect(screen.queryByTestId('chat-page')).not.toBeInTheDocument()
    expect(screen.getByText(/not a single plain path segment/)).toBeInTheDocument()
  })

  it('does the same for the query- and hash-shaped routes', () => {
    registerBuiltinComponents({
      '/zzq-query-refused?daily': { component: dummy(), appId: 'zzq-query-refused' },
      '/zzq-hash-refused#x': { component: dummy(), appId: 'zzq-hash-refused' },
      '/zzq-slash-refused/': { component: dummy(), appId: 'zzq-slash-refused' },
    })

    expect(builtinRefusalReason('/zzq-query-refused')).toMatch(/plain path segment/)
    expect(builtinRefusalReason('/zzq-hash-refused')).toMatch(/plain path segment/)
    expect(builtinRefusalReason('/zzq-slash-refused')).toMatch(/plain path segment/)
  })

  it('does not invent a routable key for a route with no leading slash — the negative control', () => {
    // Normalising `zzq-no-slash` to `/zzq-no-slash` would let a refusal for one
    // registration key answer for a DIFFERENT one, and no URL produces the
    // slashless spelling, so there is no empty page asking to be explained.
    registerBuiltinComponents({
      'zzq-no-slash': { component: dummy(), appId: 'zzq-no-slash' },
    })

    expect(builtinRefusalReason('/zzq-no-slash')).toBeUndefined()
    expect(builtinRefusalReason('zzq-no-slash')).toMatch(/plain path segment/)
  })
})

/**
 * A percent escape is resolved before the lookup, so the key must be resolved too.
 *
 * `%` is outside `_BUILTIN_ROUTE_RE`, so a percent-escaped route is always REFUSED
 * — it is one of the shapes this record exists to explain. But react-router hands
 * `BuiltinAppRoute` a DECODED param, so a reason filed under the raw registration
 * string is unreadable for exactly those routes, and they fell through to the chat
 * redirect as silently as before the record existed.
 *
 * The router's pipeline, read off react-router 7.18.2 and asserted here rather
 * than assumed, is two passes: `decodePath` decodes every segment of the pathname
 * under ONE try/catch (so a single malformed escape anywhere leaves the whole
 * pathname undecoded) and re-encodes any `/` the decode produced; then `matchPath`
 * turns `%2F` back into `/` on the captured param. `builtinRouteKey` mirrors both,
 * which is why a plain `decodeURIComponent` is not the fix.
 */
describe('a refusal is recorded under the key the router DECODES to', () => {
  beforeEach(() => {
    vi.stubEnv('DEV', false)
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    vi.spyOn(console, 'error').mockImplementation(() => {})
  })
  afterEach(() => {
    vi.unstubAllEnvs()
    vi.restoreAllMocks()
  })

  it('shows the reason for a percent-escaped route on the page the user asked for', () => {
    // `/zzq-pct%2Drefused` is asked for as `/zzq-pct-refused`. Filed verbatim, the
    // reason answered for a key nothing can request.
    registerBuiltinComponents({
      '/zzq-pct%2Drefused': { component: dummy(), appId: 'zzq-pct-refused' },
    })

    renderAtPath('/zzq-pct%2Drefused')

    expect(screen.queryByTestId('chat-page')).not.toBeInTheDocument()
    expect(screen.getByText(/not a single plain path segment/)).toBeInTheDocument()
    expect(builtinRefusalReason('/zzq-pct-refused')).toMatch(/plain path segment/)
  })

  it('keeps an encoded slash inside the one segment the router resolves', () => {
    // `%2F` decodes to a `/` that stays INSIDE the param: the router asks for
    // `/zzq-pct/slash`, not `/zzq-pct`. So the key is decoded and then read to its
    // end — truncating the decoded form at the first `/` would file the reason
    // under `/zzq-pct` and answer for a different page.
    registerBuiltinComponents({
      '/zzq-pct%2Fslash': { component: dummy(), appId: 'zzq-pct-slash' },
    })

    renderAtPath('/zzq-pct%2Fslash')

    expect(screen.queryByTestId('chat-page')).not.toBeInTheDocument()
    expect(builtinRefusalReason('/zzq-pct/slash')).toMatch(/plain path segment/)
    expect(builtinRefusalReason('/zzq-pct')).toBeUndefined()
  })

  it('applies the second pass a double-encoded slash needs', () => {
    // `%252F` survives the first decode as the literal text `%2F`, and the
    // router's own second pass then turns it into `/`. One decode alone stops a
    // step short of the key the router asks for — so this goes through the real
    // router rather than asserting what the router does from the reason map.
    registerBuiltinComponents({
      '/zzq-pct%252Fdbl': { component: dummy(), appId: 'zzq-pct-dbl' },
    })

    renderAtPath('/zzq-pct%252Fdbl')

    expect(screen.queryByTestId('chat-page')).not.toBeInTheDocument()
    expect(screen.getByText(/not a single plain path segment/)).toBeInTheDocument()
    expect(builtinRefusalReason('/zzq-pct/dbl')).toMatch(/plain path segment/)
  })

  it('cuts the query and hash on a LITERAL one, not a decoded one', () => {
    // `/zzq-pct?q` loses `?q` before the router ever sees it, so it is asked for
    // as `/zzq-pct-q-lit`. But an ENCODED `%3F`/`%23` is still in the pathname and
    // decodes INSIDE the one param, so `/zzq-pct-enc%3Fq` is asked for as
    // `/zzq-pct-enc?q` — the whole thing. Cutting after decoding would collapse
    // the second case onto the first and answer for the wrong page.
    registerBuiltinComponents({
      '/zzq-pct-q-lit?q': { component: dummy(), appId: 'zzq-pct-q-lit' },
      '/zzq-pct-enc%3Fq': { component: dummy(), appId: 'zzq-pct-enc' },
      '/zzq-pct-hash%23h': { component: dummy(), appId: 'zzq-pct-hash' },
    })

    expect(builtinRefusalReason('/zzq-pct-q-lit')).toMatch(/plain path segment/)
    expect(builtinRefusalReason('/zzq-pct-enc?q')).toMatch(/plain path segment/)
    expect(builtinRefusalReason('/zzq-pct-enc')).toBeUndefined()
    expect(builtinRefusalReason('/zzq-pct-hash#h')).toMatch(/plain path segment/)
  })

  it('reaches the page for an encoded query, proving the router keeps it in the segment', () => {
    // The claim above, put to the router instead of to the reason map: only one
    // render can be asserted per test, so the end-to-end half lives here.
    registerBuiltinComponents({
      '/zzq-pct-enc-e2e%3Fq': { component: dummy(), appId: 'zzq-pct-enc-e2e' },
    })

    renderAtPath('/zzq-pct-enc-e2e%3Fq')

    expect(screen.queryByTestId('chat-page')).not.toBeInTheDocument()
    expect(screen.getByText(/not a single plain path segment/)).toBeInTheDocument()
  })

  it('leaves a malformed escape alone, and does not take the registrar down', () => {
    // `decodeURIComponent` THROWS on `%ZZ`. The router catches and uses the raw
    // pathname, so the key must be raw too — and the throw must not escape
    // `registerBuiltinComponents`, which would abort every later entry in the
    // same batch. The second route proves the batch survived.
    registerBuiltinComponents({
      '/zzq-pct%ZZbad': { component: dummy(), appId: 'zzq-pct-bad' },
      '/zzq-pct-after-bad/x': { component: dummy(), appId: 'zzq-pct-after-bad' },
    })

    expect(builtinRefusalReason('/zzq-pct%ZZbad')).toMatch(/plain path segment/)
    expect(builtinRefusalReason('/zzq-pct-after-bad')).toMatch(/plain path segment/)
  })

  it('does not decode when a LATER segment is malformed — the negative control', () => {
    // `decodePath` spans the whole pathname in one try/catch, so `%ZZ` in the
    // SECOND segment leaves the first undecoded: the router asks for
    // `/zzq-pct%2Dlate`, with the `%2D` intact. A key normaliser that decoded its
    // own segment in isolation would file `/zzq-pct-late` and miss again — this
    // is the control that fails if the fix over-normalises.
    registerBuiltinComponents({
      '/zzq-pct%2Dlate/x%ZZ': { component: dummy(), appId: 'zzq-pct-late' },
    })

    renderUnderSplatAtPath('/zzq-pct%2Dlate/x%ZZ')

    expect(screen.queryByTestId('chat-page')).not.toBeInTheDocument()
    expect(screen.getByText(/not a single plain path segment/)).toBeInTheDocument()
    expect(builtinRefusalReason('/zzq-pct%2Dlate')).toMatch(/plain path segment/)
    expect(builtinRefusalReason('/zzq-pct-late')).toBeUndefined()
  })

  it('leaves an escape-free route exactly as it was — the second negative control', () => {
    // Decoding must be identity on every route that carries no `%`, which is
    // every route the core registers. If this moves, the fix has changed the keys
    // of the 23 pages that work.
    registerBuiltinComponents({
      '/zzq-pct-plain/nested': { component: dummy(), appId: 'zzq-pct-plain' },
    })

    expect(builtinRefusalReason('/zzq-pct-plain')).toMatch(/plain path segment/)
  })
})

/**
 * The browser normalises the path before the router ever sees it.
 *
 * A dot segment is not a character class problem, it is a NORMALISATION problem:
 * `/../reports` is never requested: the browser resolves it to `/reports` and asks
 * for that. So the key has to be the pathname a browser would send, which is why
 * it is derived through `URL` rather than by cutting the string at `?`/`#` — a cut
 * that was right about the query and hash and blind to every other normalisation
 * step, one round of review at a time.
 *
 * `%2e` is included because it is the spelling a hand-rolled `..` stripper misses:
 * the URL parser treats `%2e` as `.` for dot-segment removal, so `/%2e%2e/x`
 * resolves to `/x` exactly as `/../x` does.
 */
describe('a refusal is recorded under the key the BROWSER normalises to', () => {
  beforeEach(() => {
    vi.stubEnv('DEV', false)
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    vi.spyOn(console, 'error').mockImplementation(() => {})
  })
  afterEach(() => {
    vi.unstubAllEnvs()
    vi.restoreAllMocks()
  })

  it('shows the reason for a dot-segment route on the page the user asked for', () => {
    registerBuiltinComponents({
      '/../zzq-dot-refused': { component: dummy(), appId: 'zzq-dot-refused' },
    })

    renderAtPath('/zzq-dot-refused')

    expect(screen.queryByTestId('chat-page')).not.toBeInTheDocument()
    expect(screen.getByText(/not a single plain path segment/)).toBeInTheDocument()
    // And nothing is filed under the unnormalised leading segment, which no URL
    // can ask for.
    expect(builtinRefusalReason('/..')).toBeUndefined()
  })

  it('resolves the other dot-segment spellings the same way', () => {
    registerBuiltinComponents({
      '/./zzq-dot-single': { component: dummy(), appId: 'zzq-dot-single' },
      '/%2e%2e/zzq-dot-pct': { component: dummy(), appId: 'zzq-dot-pct' },
      '/zzq-dot-up/../zzq-dot-sibling': { component: dummy(), appId: 'zzq-dot-sibling' },
    })

    expect(builtinRefusalReason('/zzq-dot-single')).toMatch(/plain path segment/)
    expect(builtinRefusalReason('/zzq-dot-pct')).toMatch(/plain path segment/)
    expect(builtinRefusalReason('/zzq-dot-sibling')).toMatch(/plain path segment/)
    // The segment the route was WRITTEN with is not a page either.
    expect(builtinRefusalReason('/zzq-dot-up')).toBeUndefined()
  })

  it('does not claim a page on another origin — the negative control', () => {
    // `//host/path` opens with a `/` but leaves this origin, as do the spellings
    // the URL parser treats identically: a backslash, and a tab smuggled between
    // the separators. None of them leaves an empty page HERE, so none may file a
    // reason under a local key — that would let a refusal answer for a page it
    // has nothing to do with.
    registerBuiltinComponents({
      '//zzq-other-host/zzq-authority-refused': { component: dummy(), appId: 'zzq-auth-a' },
      '/\\zzq-other-host/zzq-backslash-refused': { component: dummy(), appId: 'zzq-auth-b' },
      '/\t/zzq-other-host/zzq-tab-refused': { component: dummy(), appId: 'zzq-auth-c' },
    })

    expect(builtinRefusalReason('/zzq-authority-refused')).toBeUndefined()
    expect(builtinRefusalReason('/zzq-backslash-refused')).toBeUndefined()
    expect(builtinRefusalReason('/zzq-tab-refused')).toBeUndefined()
    // The refusal is still recorded and reported, under the raw string — a key
    // nothing can request, which is the honest place for it.
    expect(builtinRefusalReason('//zzq-other-host/zzq-authority-refused'))
      .toMatch(/plain path segment/)
  })

  it('does not take the registrar down on an unparseable authority', () => {
    // `new URL('//[zzq-bad', …)` THROWS. The throw must not escape
    // `registerBuiltinComponents`, which would abandon every later entry in the
    // same batch — the second route proves the batch survived.
    registerBuiltinComponents({
      '//[zzq-bad': { component: dummy(), appId: 'zzq-bad-authority' },
      '/zzq-after-bad-authority/x': { component: dummy(), appId: 'zzq-after-bad-authority' },
    })

    expect(builtinRefusalReason('//[zzq-bad')).toMatch(/plain path segment/)
    expect(builtinRefusalReason('/zzq-after-bad-authority')).toMatch(/plain path segment/)
  })
})

/**
 * A refused page must not offer an action that cannot work.
 *
 * The default `ErrorBoundary` card carries "Try Again", which clears
 * `state.error` and re-renders the same children. For an ordinary crash that is
 * a real chance — a transient fetch, a race — and it stays. For a REFUSAL it is
 * a certainty: the decision was made once at startup and is immutable for the
 * life of the page load, so `RefusedRoute` throws the identical reason and the
 * identical card returns. So the refused branch supplies its own `fallback`.
 *
 * `fallback` overrides what RENDERS, not what is JOURNALED: `componentDidCatch`
 * runs either way, which is why the throw is kept rather than replaced with a
 * direct render, and is asserted below rather than assumed.
 */
describe('a refused page offers only actions that can work', () => {
  beforeEach(() => {
    vi.stubEnv('DEV', false)
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    vi.spyOn(console, 'error').mockImplementation(() => {})
  })
  afterEach(() => {
    vi.unstubAllEnvs()
    vi.restoreAllMocks()
  })

  it('drops Try Again and keeps the agent hand-off', () => {
    const route = '/zzq-retry-refused'
    registerBuiltinComponents({ [route]: { component: dummy() } as unknown as BuiltinAppEntry })

    renderAtPath(route)

    expect(screen.queryByRole('button', { name: /try again/i })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /ask the agent/i })).toBeInTheDocument()
  })

  it('says in the page what its one action does', () => {
    // The hand-off's promise — that the diagnostic above travels into the chat —
    // used to live only in the button's `title`. A tooltip reaches neither
    // keyboard nor touch, so on a page whose ONLY affordance is this button, a
    // reader could not tell it apart from a plain new chat with a different
    // label. So it is stated as text, and asserted OUTSIDE the button: reading
    // the `title` back would pass on exactly the arrangement that failed.
    const route = '/zzq-handoff-note'
    registerBuiltinComponents({ [route]: { component: dummy() } as unknown as BuiltinAppEntry })

    renderAtPath(route)

    const note = screen.getByText('Asking the agent opens a chat with these details already attached.')
    expect(note).toBeInTheDocument()
    expect(note.closest('button')).toBeNull()
  })

  it('leaves an ordinary crash card without it — the negative control', async () => {
    // The note describes the REFUSED page's single-action layout. The default
    // card offers Try Again alongside the hand-off, so the reader is not down to
    // one guess there, and repeating the sentence on every crash in the app
    // would be noise rather than the answer to a specific doubt.
    const route = '/zzq-handoff-note-crash'
    registerBuiltinComponents({
      [route]: { component: crashingPage('zzq-handoff-crash-threw'), appId: 'zzq-handoff-crash' },
    })

    renderAtPath(route)
    expect(await screen.findByText(/zzq-handoff-crash-threw/)).toBeInTheDocument()

    expect(
      screen.queryByText('Asking the agent opens a chat with these details already attached.'),
    ).not.toBeInTheDocument()
  })

  it('leaves Try Again on an ordinary page crash — the negative control', async () => {
    // The override is scoped to the refusal branch. A page that crashes at render
    // MAY recover on a retry, so removing the button everywhere would take a
    // working affordance away from every other failure on this route.
    const route = '/zzq-retry-crash'
    registerBuiltinComponents({
      [route]: { component: crashingPage('zzq-retry-crash-threw'), appId: 'zzq-retry-crash' },
    })

    renderAtPath(route)
    expect(await screen.findByText(/zzq-retry-crash-threw/)).toBeInTheDocument()

    expect(screen.getByRole('button', { name: /try again/i })).toBeInTheDocument()
  })

  it('still journals the refusal, so the hand-off carries its context', () => {
    // The load-bearing reason the refusal is THROWN rather than rendered
    // directly. `componentDidCatch` feeds `recordError` and the RUM
    // `react_error` event, and `AskAgentButton` resolves the journal entry by
    // message — so the diagnostic reaching the journal is what makes the one
    // remaining button carry anything. A future change that renders the reason
    // without throwing fails here.
    const route = '/zzq-journal-refused'
    registerBuiltinComponents({ [route]: { component: dummy() } as unknown as BuiltinAppEntry })
    const reason = builtinRefusalReason(route)
    expect(reason).toBeDefined()

    renderAtPath(route)

    const journaled = findReport(`Page ${route} was refused at registration: ${reason}`)
    expect(journaled?.source).toBe('render')
    expect(journaled?.code).toBe('builtin-route-refused')
  })
})

/**
 * One armed boundary must not follow the user to the next builtin page.
 *
 * Every builtin page renders through the SAME `<Route path="/:builtinApp/*">`
 * element, so a builtin→builtin move re-renders `BuiltinAppRoute` in place.
 * Both of its branches return `<ErrorBoundary>` as their root, and
 * `ErrorBoundary.render()` returns the fallback before it ever reads `children`
 * with no reset on a prop change — so an unkeyed boundary that caught one throw
 * keeps showing it on every page the user visits next, until a reload. Keying
 * the boundary by the resolved path makes the move a remount.
 */
describe('an armed route boundary does not follow the user to the next page', () => {
  beforeEach(() => {
    vi.stubEnv('DEV', false)
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    vi.spyOn(console, 'error').mockImplementation(() => {})
  })
  afterEach(() => {
    vi.unstubAllEnvs()
    vi.restoreAllMocks()
    navigateTo = null
  })

  it('leaves a REFUSED page for a working one and renders it', async () => {
    const refused = '/zzq-poison-refused'
    const working = '/zzq-poison-working'
    registerBuiltinComponents({ [refused]: { component: dummy() } as unknown as BuiltinAppEntry })
    registerBuiltinComponents({
      [working]: { component: pageWithTestId('zzq-poison-working-page'), appId: 'zzq-poison-working' },
    })

    renderAtPath(refused)
    expect(screen.getByText(`${refused} could not load`)).toBeInTheDocument()

    await act(async () => { navigateTo?.(working) })

    expect(await screen.findByTestId('zzq-poison-working-page')).toBeInTheDocument()
    expect(screen.queryByText(`${refused} could not load`)).not.toBeInTheDocument()
  })

  it('leaves a CRASHED page for a working one and renders it — the sibling instance', async () => {
    // The same mechanism through the other branch: the refusal path is not the
    // only thing that arms this boundary, so keying only that branch would
    // leave a normal page crash poisoning every builtin page after it.
    const crashed = '/zzq-crash-source'
    const working = '/zzq-crash-working'
    registerBuiltinComponents({
      [crashed]: { component: crashingPage('zzq-crash-source-threw'), appId: 'zzq-crash-source' },
      [working]: { component: pageWithTestId('zzq-crash-working-page'), appId: 'zzq-crash-working' },
    })

    renderAtPath(crashed)
    expect(await screen.findByText(/zzq-crash-source-threw/)).toBeInTheDocument()

    await act(async () => { navigateTo?.(working) })

    expect(await screen.findByTestId('zzq-crash-working-page')).toBeInTheDocument()
    expect(screen.queryByText(/zzq-crash-source-threw/)).not.toBeInTheDocument()
  })

  it('keeps a page mounted across its own sub-path navigation — the negative control', async () => {
    // The key is the RESOLVED single segment, not the full URL: an app that
    // navigates inside itself (`/settings/<tab>` shape) must not remount and
    // lose its state on every sub-path change.
    const route = '/zzq-subpath'
    registerBuiltinComponents({
      [route]: { component: pageWithTestId('zzq-subpath-page'), appId: 'zzq-subpath' },
    })

    render(
      <MemoryRouter initialEntries={[route]}>
        <NavHarness />
        <Routes>
          <Route path="/:builtinApp/*" element={<BuiltinAppRoute />} />
        </Routes>
      </MemoryRouter>,
    )
    const first = await screen.findByTestId('zzq-subpath-page')

    await act(async () => { navigateTo?.(`${route}/usage`) })

    expect(screen.getByTestId('zzq-subpath-page')).toBe(first)
  })
})
