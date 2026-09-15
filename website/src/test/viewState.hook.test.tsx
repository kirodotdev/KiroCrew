/**
 * View-state store — the hook.
 *
 * Three things here are not visible in the pure tests: the builtin gate, WHEN the record
 * is read (which is what makes a restored coordinate usable by a query key), and what
 * happens when the scope changes without a remount.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { lazy, Suspense } from 'react'
import { render, act, screen } from '@testing-library/react'
import { AppIdentityProvider } from '../app-sdk/identity'
import { AppApiProvider } from '../app-sdk'
// The scoped layer is imported BY PATH: it was taken off the barrel (and out of the
// vendor stub) because publishing to third-party apps is a one-way door and it has no
// external consumers yet -- the same reasoning that keeps the identity layer and this
// store off it.
import { AppScopedApiProvider } from '../app-sdk/scopedApi'
import {
  useAppViewState,
  isViewString,
  type ViewStateDecl,
  type ViewStateSetter,
} from '../app-sdk/viewState'

interface DriveView {
  path: string
}

const DECL: ViewStateDecl<DriveView> = {
  name: 'drive',
  revision: 1,
  fields: { path: isViewString },
  defaults: { path: '' },
}

const ACCOUNT = '111122223333'
/**
 * The key spelled out, NOT recomputed with the module's own derivation.
 *
 * This is the on-disk contract: appId, then the surface, then the scope. Asserting the
 * literal is what makes the tests able to catch a derivation that drops a segment --
 * recomputing it would make code and test agree on the same mistake.
 */
const KEY = `kc:app:aws-control:view:drive:${ACCOUNT}`
const OTHER_ACCOUNT = '999988887777'
const OTHER_KEY = `kc:app:aws-control:view:drive:${OTHER_ACCOUNT}`

/** Seed the record for one scope. The scope selects the KEY; it is not in the record. */
function seed(revision: number, scope: string, state: Record<string, unknown>): void {
  localStorage.setItem(`kc:app:aws-control:view:drive:${scope}`, JSON.stringify({ revision, state }))
}

/** Records the state seen on EVERY render, so the first one can be asserted on its own. */
let seen: DriveView[] = []
let setLatest: ViewStateSetter<DriveView> = () => {}

function Probe({ scope }: { scope?: string }) {
  const [state, setState] = useAppViewState(DECL, { scope })
  seen.push(state)
  setLatest = setState
  return <div data-testid="path">{state.path || '(root)'}</div>
}

function mount(node: React.ReactNode) {
  return render(node)
}

function builtin(scope?: string) {
  return (
    <AppIdentityProvider appId="aws-control" origin="builtin">
      <Probe scope={scope} />
    </AppIdentityProvider>
  )
}

beforeEach(() => {
  localStorage.clear()
  seen = []
  vi.restoreAllMocks()
})

afterEach(() => {
  vi.restoreAllMocks()
})

describe('useAppViewState — the builtin gate', () => {
  it('restores under a builtin identity', () => {
    seed(1, ACCOUNT, { path: 'docs' })
    mount(builtin(ACCOUNT))
    expect(seen[0]).toEqual({ path: 'docs' })
  })

  it('neither reads nor writes for an external app', () => {
    // The gate is `useTrustedAppId()`, whose null covers a non-builtin origin. Reading
    // `useAppIdentity().appId` instead would hand an external app the builtin's keys —
    // an app can self-register under the name `aws-control`.
    seed(1, ACCOUNT, { path: 'docs' })
    mount(
      <AppIdentityProvider appId="aws-control" origin="external">
        <Probe scope={ACCOUNT} />
      </AppIdentityProvider>,
    )
    expect(seen[0]).toEqual({ path: '' })

    act(() => setLatest({ path: 'elsewhere' }))
    // The seeded record is untouched: no read, and no write over it either.
    expect(JSON.parse(localStorage.getItem(KEY) as string).state).toEqual({ path: 'docs' })
  })

  it('neither reads nor writes on a host page with no identity', () => {
    seed(1, ACCOUNT, { path: 'docs' })
    mount(<Probe scope={ACCOUNT} />)
    expect(seen[0]).toEqual({ path: '' })
    act(() => setLatest({ path: 'elsewhere' }))
    expect(JSON.parse(localStorage.getItem(KEY) as string).state).toEqual({ path: 'docs' })
  })
})

describe('useAppViewState — when the record is read', () => {
  it('has the restored value on the consumer\'s FIRST render', () => {
    // The assertion is `seen[0]`, not the settled DOM. An effect-published value reaches
    // the DOM too, one render later, so a settled-DOM assertion passes either way, which
    // is how a first-render test goes vacuous without looking wrong. The first render is
    // the one that matters here because the consumer keys a query off this value: a render
    // with the default fires a request for the wrong folder.
    seed(1, ACCOUNT, { path: 'a/b' })
    mount(builtin(ACCOUNT))
    expect(seen[0]).toEqual({ path: 'a/b' })
    expect(seen.every((s) => s.path === 'a/b')).toBe(true)
  })

  it('never shows the default first when a record exists', () => {
    seed(1, ACCOUNT, { path: 'docs' })
    mount(builtin(ACCOUNT))
    expect(seen.map((s) => s.path)).not.toContain('')
  })
})

describe('useAppViewState — persistence', () => {
  it('writes a record once there is a position', () => {
    mount(builtin(ACCOUNT))
    expect(localStorage.getItem(KEY)).toBeNull()

    act(() => setLatest({ path: 'docs' }))
    expect(JSON.parse(localStorage.getItem(KEY) as string)).toEqual({
      revision: 1,
      state: { path: 'docs' },
    })
  })

  it('removes the record when the user returns to the defaults', () => {
    // The store keeps a row only while there is somewhere to return to.
    seed(1, ACCOUNT, { path: 'docs' })
    mount(builtin(ACCOUNT))
    act(() => setLatest({ path: '' }))
    expect(localStorage.getItem(KEY)).toBeNull()
  })

  it('drops an undeclared field handed to the setter', () => {
    mount(builtin(ACCOUNT))
    act(() => setLatest({ path: 'docs', contents: ['a.txt'] } as unknown as Partial<DriveView>))
    const stored = localStorage.getItem(KEY) as string
    expect(stored).not.toContain('contents')
    // And it never entered the live state either, so what the app reads back and what is
    // persisted cannot disagree.
    expect(seen[seen.length - 1]).toEqual({ path: 'docs' })
  })

  it('discards a stale record it refused to restore', () => {
    seed(2, ACCOUNT, { path: 'docs' })
    mount(builtin(ACCOUNT))
    expect(localStorage.getItem(KEY)).toBeNull()
  })
})

describe('useAppViewState — scope', () => {
  it('mounts with defaults when the record belongs to another scope', () => {
    seed(1, OTHER_ACCOUNT, { path: 'docs' })
    mount(builtin(ACCOUNT))
    expect(seen[0]).toEqual({ path: '' })
  })

  it('re-reads when the scope changes WITHOUT a remount', () => {
    // Without this, the old position stays live and the next write stores it labelled
    // with the NEW scope — the record would state something false. The consumer is not
    // required to remember to remount for that not to happen.
    seed(1, ACCOUNT, { path: 'from-a' })
    const view = mount(builtin(ACCOUNT))
    expect(seen[seen.length - 1]).toEqual({ path: 'from-a' })

    seen = []
    view.rerender(builtin(OTHER_ACCOUNT))
    expect(seen[seen.length - 1]).toEqual({ path: '' })
    // The record on disk is still account A's own position, NOT A's path relabelled as
    // the new account's. That relabelling is the falsehood the re-read prevents.
    expect(JSON.parse(localStorage.getItem(KEY) as string)).toEqual({
      revision: 1,
      state: { path: 'from-a' },
    })
  })

  it('leaves another scope\'s record alone while sitting at the defaults', () => {
    // Merely opening account A at the root must not throw away the folder you were in
    // under account B — that is state the user never touched.
    seed(1, OTHER_ACCOUNT, { path: 'from-b' })
    mount(builtin(ACCOUNT))
    expect(seen[0]).toEqual({ path: '' })
    // B's record is under B's key, so A's visit is structurally incapable of touching it.
    expect(JSON.parse(localStorage.getItem(OTHER_KEY) as string).state).toEqual({ path: 'from-b' })
  })

  it('restores the new scope\'s own record on a scope change', () => {
    seed(1, OTHER_ACCOUNT, { path: 'from-b' })
    const view = mount(builtin(ACCOUNT))
    expect(seen[seen.length - 1]).toEqual({ path: '' })
    seen = []
    view.rerender(builtin(OTHER_ACCOUNT))
    expect(seen[seen.length - 1]).toEqual({ path: 'from-b' })
  })

  it('keeps BOTH accounts\' positions, one record each', () => {
    // The UX a single shared record could not give: a user working across two accounts
    // finds both folders where they left them. Under the previous shape, whichever account
    // was used last overwrote the other.
    seed(1, OTHER_ACCOUNT, { path: 'from-b' })
    mount(builtin(ACCOUNT))
    act(() => setLatest({ path: 'from-a' }))
    expect(JSON.parse(localStorage.getItem(KEY) as string).state).toEqual({ path: 'from-a' })
    expect(JSON.parse(localStorage.getItem(OTHER_KEY) as string).state).toEqual({ path: 'from-b' })
  })
})

describe('useAppViewState — what gets reported', () => {
  it('says nothing on a scope mismatch', () => {
    // Routine: it fires every time the user switches account. Logging it would train
    // everyone to ignore the channel.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const debug = vi.spyOn(console, 'debug').mockImplementation(() => {})
    seed(1, OTHER_ACCOUNT, { path: 'docs' })
    mount(builtin(ACCOUNT))
    expect(warn).not.toHaveBeenCalled()
    expect(debug).not.toHaveBeenCalled()
  })

  it('says nothing when there is no record, or when one restores', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const debug = vi.spyOn(console, 'debug').mockImplementation(() => {})
    mount(builtin(ACCOUNT))
    seed(1, ACCOUNT, { path: 'docs' })
    mount(builtin(ACCOUNT))
    expect(warn).not.toHaveBeenCalled()
    expect(debug).not.toHaveBeenCalled()
  })

  it('debugs a revision mismatch, without warning', () => {
    // Only reachable after someone deliberately changed the schema, and it answers "why
    // did everyone's position reset" immediately.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const debug = vi.spyOn(console, 'debug').mockImplementation(() => {})
    seed(2, ACCOUNT, { path: 'docs' })
    mount(builtin(ACCOUNT))
    expect(debug).toHaveBeenCalledTimes(1)
    expect(String(debug.mock.calls[0][0])).toContain(KEY)
    expect(warn).not.toHaveBeenCalled()
  })

  it('warns ONCE per key for a corrupt record', () => {
    // A real fault — something wrote garbage under a host-owned key — but a warning
    // repeated on every remount drowns out real signal.
    //
    // Uses its OWN appId, so the assertion does not depend on whether an earlier test in
    // this file already consumed the once-per-key budget for the shared key. That is what
    // lets the ledger stay private to the module instead of exposing a reset seam from
    // production code purely so a test can call it.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const ownKey = `kc:app:dedupe-probe:view:drive:${ACCOUNT}`
    const tree = (
      <AppIdentityProvider appId="dedupe-probe" origin="builtin">
        <Probe scope={ACCOUNT} />
      </AppIdentityProvider>
    )
    localStorage.setItem(ownKey, 'not json')
    mount(tree)
    localStorage.setItem(ownKey, 'still not json')
    mount(tree)
    expect(warn).toHaveBeenCalledTimes(1)
    expect(String(warn.mock.calls[0][0])).toContain(ownKey)
  })
})

describe('useAppViewState — under a nested scoped-API provider', () => {
  // The failure this guards against is silent, which is why it is worth a test at THIS
  // layer rather than trusting the layer above. `AppApiProvider` defaults to
  // `origin: 'external'`, so a builtin page that mounts one to get `useAppApi()` would —
  // if that provider published its own identity — replace the host-minted `builtin`
  // identity underneath it. `useTrustedAppId()` would then return null, and this store
  // would fall back to defaults forever with no error anywhere: the page simply stops
  // remembering where you were.
  //
  // What prevents it is that identity is published only when there is none already in
  // context. Wherever that rule lives, this test asserts the CONSEQUENCE for the store
  // rather than the rule itself: the namespace survives a nested provider, so the record
  // is still read and still written under the builtin appId. Phrased as the property, so
  // it stays true regardless of which change ships the rule.
  it('keeps the host namespace when AppApiProvider is mounted inside the page', () => {
    seed(1, ACCOUNT, { path: 'docs' })
    render(
      <AppIdentityProvider appId="aws-control" origin="builtin">
        <AppApiProvider
          appName="aws-control"
          allowedApiPaths={['/api/apps/aws-control']}
          navigateFn={() => {}}
        >
          <Probe scope={ACCOUNT} />
        </AppApiProvider>
      </AppIdentityProvider>,
    )
    expect(seen[0]).toEqual({ path: 'docs' })

    act(() => setLatest({ path: 'docs/reports' }))
    expect(JSON.parse(localStorage.getItem(KEY) as string)).toEqual({
      revision: 1,
      state: { path: 'docs/reports' },
    })
  })

  it('keeps the host namespace under the scoped layer mounted alone', () => {
    // The shape a builtin page should actually use: identity comes from the host, and
    // the scoped layer resolves the app name from it.
    seed(1, ACCOUNT, { path: 'docs' })
    render(
      <AppIdentityProvider appId="aws-control" origin="builtin">
        <AppScopedApiProvider allowedApiPaths={['/api/apps/aws-control']} navigateFn={() => {}}>
          <Probe scope={ACCOUNT} />
        </AppScopedApiProvider>
      </AppIdentityProvider>,
    )
    expect(seen[0]).toEqual({ path: 'docs' })
  })

  it('still refuses the namespace when the page itself is external', () => {
    // The gate is not weakened by the composition: an external page under its own
    // provider gets identity but no host namespace.
    seed(1, ACCOUNT, { path: 'docs' })
    render(
      <AppApiProvider
        appName="aws-control"
        allowedApiPaths={['/api/apps/aws-control']}
        navigateFn={() => {}}
      >
        <Probe scope={ACCOUNT} />
      </AppApiProvider>,
    )
    expect(seen[0]).toEqual({ path: '' })
    expect(JSON.parse(localStorage.getItem(KEY) as string).state).toEqual({ path: 'docs' })
  })
})

describe('useAppViewState — identity that is not there yet, or changes', () => {
  it('restores when the namespace is granted AFTER the first render', () => {
    // A consumer can render before its namespace is resolvable — AppHost forwards an
    // installed app's `origin` from data, so the value above a continuously-mounted page
    // can change. Keying the snapshot on scope alone left that first namespace-less
    // answer in place forever and the page silently never restored.
    //
    // `origin` flips on a provider that stays mounted, which is what makes this a
    // re-read rather than a remount: swapping `<Probe>` for `<Provider><Probe></Provider>`
    // changes Probe's position in the tree, so React would remount it and re-run the
    // initializer, and the test would pass whether or not the re-read exists.
    seed(1, ACCOUNT, { path: 'docs' })
    const view = render(
      <AppIdentityProvider appId="aws-control" origin="external">
        <Probe scope={ACCOUNT} />
      </AppIdentityProvider>,
    )
    expect(seen[0]).toEqual({ path: '' })

    seen = []
    view.rerender(
      <AppIdentityProvider appId="aws-control" origin="builtin">
        <Probe scope={ACCOUNT} />
      </AppIdentityProvider>,
    )
    expect(seen[seen.length - 1]).toEqual({ path: 'docs' })
  })

  it('re-reads when the appId changes, so one app cannot write into another\'s namespace', () => {
    seed(1, ACCOUNT, { path: 'from-aws-control' })
    localStorage.setItem(
      `kc:app:file-explorer:view:drive:${ACCOUNT}`,
      JSON.stringify({ revision: 1, scope: ACCOUNT, state: { path: 'from-file-explorer' } }),
    )
    const view = render(builtin(ACCOUNT))
    expect(seen[seen.length - 1]).toEqual({ path: 'from-aws-control' })

    seen = []
    view.rerender(
      <AppIdentityProvider appId="file-explorer" origin="builtin">
        <Probe scope={ACCOUNT} />
      </AppIdentityProvider>,
    )
    expect(seen[seen.length - 1]).toEqual({ path: 'from-file-explorer' })
    // Neither app's record was overwritten with the other's position.
    expect(JSON.parse(localStorage.getItem(KEY) as string).state).toEqual({
      path: 'from-aws-control',
    })
  })
})

describe('useAppViewState — under a real lazy boundary', () => {
  // The Suspense masking trap, walked deliberately. Under `React.lazy` the parent commits
  // while the child is still suspended, so an ancestor that publishes in an effect looks
  // on time on a COLD load and only reds on a WARM second mount, where the module is
  // already resolved and parent and child render in one pass.
  //
  // This store's own read is in the CONSUMER's hook rather than an ancestor, so it is not
  // exposed the same way — but the identity it depends on comes from an ancestor, so the
  // warm path is worth walking against the real thing rather than assumed.
  const LazyProbe = lazy(async () => ({
    default: function LazyView() {
      const [state] = useAppViewState(DECL, { scope: ACCOUNT })
      seen.push(state)
      return <div data-testid="lazy-path">{state.path || '(root)'}</div>
    },
  }))

  function lazyTree() {
    return (
      <AppIdentityProvider appId="aws-control" origin="builtin">
        <Suspense fallback={<div data-testid="pending">loading</div>}>
          <LazyProbe />
        </Suspense>
      </AppIdentityProvider>
    )
  }

  it('restores on a cold mount, and on the WARM second mount\'s first render', async () => {
    seed(1, ACCOUNT, { path: 'docs/reports' })

    // Cold: the module is not loaded, so the child suspends first.
    const cold = render(lazyTree())
    expect(screen.getByTestId('pending')).toBeTruthy()
    expect(await screen.findByTestId('lazy-path')).toBeTruthy()
    expect(seen[0]).toEqual({ path: 'docs/reports' })
    cold.unmount()

    // Warm: the module is resolved, so this is the pass where nothing can be masked.
    seen = []
    render(lazyTree())
    expect(screen.getByTestId('lazy-path')).toBeTruthy()
    expect(seen[0]).toEqual({ path: 'docs/reports' })
  })
})

describe('useAppViewState — record handling, through the only surface a caller has', () => {
  // These properties used to be asserted against exported pure functions. Those are
  // internal now (they had no non-test consumer), so they are exercised where a caller
  // actually meets them: seed storage, mount, and read back what the hook restored and
  // what it wrote.

  it('restores a record whose declared field validates', () => {
    seed(1, ACCOUNT, { path: 'docs' })
    mount(builtin(ACCOUNT))
    expect(seen[0]).toEqual({ path: 'docs' })
  })

  it.each([
    ['not json at all', 'not json'],
    ['a bare string', '"docs"'],
    ['an array', '[{"path":"docs"}]'],
    ['null', 'null'],
    ['a record with no state object', JSON.stringify({ revision: 1 })],
    ['a record whose state is an array', JSON.stringify({ revision: 1, state: [] })],
    ['a declared field of the wrong type', JSON.stringify({ revision: 1, state: { path: 42 } })],
  ])('mounts with defaults when the record is %s', (_label, raw) => {
    // Every rejection lands on the same answer, because a changed or broken schema must
    // never be able to stop a page from mounting.
    vi.spyOn(console, 'warn').mockImplementation(() => {})
    localStorage.setItem(KEY, raw)
    mount(builtin(ACCOUNT))
    expect(seen[0]).toEqual({ path: '' })
  })

  it('rejects the WHOLE record when a declared field fails its guard', () => {
    // Not a partial restore: a half-applied position is a state the app was never written
    // to handle, and the corrupt case is not one worth salvaging.
    //
    // The assertion that discriminates is the WARN, not the restored state. Skipping the
    // bad field instead of rejecting the record also yields the defaults -- the difference
    // is that it reports `restored` and says nothing, so a corrupt record would pass as a
    // user who simply had no saved position.
    //
    // Its own appId, because the warn is deduped per key and the table above already spent
    // that budget for the shared key.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    const ownKey = `kc:app:whole-record-probe:view:drive:${ACCOUNT}`
    localStorage.setItem(ownKey, JSON.stringify({ revision: 1, state: { path: 42 } }))
    render(
      <AppIdentityProvider appId="whole-record-probe" origin="builtin">
        <Probe scope={ACCOUNT} />
      </AppIdentityProvider>,
    )
    expect(seen[0]).toEqual({ path: '' })
    expect(warn).toHaveBeenCalledTimes(1)
    expect(String(warn.mock.calls[0][0])).toContain('could not be read')
    // And it is discarded rather than left to fail again on the next visit.
    expect(localStorage.getItem(ownKey)).toBeNull()
  })

  it('drops a DECLARED field whose value fails its guard, on the way in', () => {
    // The write-side guard, which the undeclared-field test cannot reach: `contents` is
    // dropped because it is not declared at all, so removing the per-field guard would not
    // show up there. A declared field carrying the wrong type is the case that needs it.
    seed(1, ACCOUNT, { path: 'docs' })
    mount(builtin(ACCOUNT))
    act(() => setLatest({ path: 42 } as unknown as Partial<DriveView>))
    expect(seen[seen.length - 1]).toEqual({ path: 'docs' })
    expect(localStorage.getItem(KEY)).toBe('{"revision":1,"state":{"path":"docs"}}')
  })

  it('lets an absent field fall back to its default', () => {
    // How a field added at the same revision reads against an older record.
    localStorage.setItem(KEY, JSON.stringify({ revision: 1, state: {} }))
    mount(builtin(ACCOUNT))
    expect(seen[0]).toEqual({ path: '' })
  })

  it('ignores an undeclared field found in a stored record', () => {
    localStorage.setItem(KEY, JSON.stringify({ revision: 1, state: { path: 'docs', contents: ['a'] } }))
    mount(builtin(ACCOUNT))
    expect(seen[0]).toEqual({ path: 'docs' })
  })

  it('writes a byte-stable record for the same position', () => {
    mount(builtin(ACCOUNT))
    act(() => setLatest({ path: 'docs' }))
    const first = localStorage.getItem(KEY)
    localStorage.clear()
    seen = []
    mount(builtin(ACCOUNT))
    act(() => setLatest({ path: 'docs' }))
    expect(localStorage.getItem(KEY)).toBe(first)
    expect(first).toBe('{"revision":1,"state":{"path":"docs"}}')
  })

  it('treats a state of only undeclared fields as the defaults', () => {
    // Otherwise a stray field would make a default position look worth keeping, and the
    // store would leave a record holding nothing.
    seed(1, ACCOUNT, { path: 'docs' })
    mount(builtin(ACCOUNT))
    act(() => setLatest({ path: '', contents: ['a'] } as unknown as Partial<DriveView>))
    expect(localStorage.getItem(KEY)).toBeNull()
  })
})

describe('useAppViewState — the key the record lands under', () => {
  // Asserted as literal strings, because the derivation is internal now and recomputing it
  // would let a dropped segment agree with itself. These are the on-disk contract.

  it('addresses appId, surface and scope, in that order', () => {
    mount(builtin(ACCOUNT))
    act(() => setLatest({ path: 'docs' }))
    expect(localStorage.getItem('kc:app:aws-control:view:drive:111122223333')).toBe(
      '{"revision":1,"state":{"path":"docs"}}',
    )
  })

  it('omits the scope segment when the consumer passes none', () => {
    render(
      <AppIdentityProvider appId="aws-control" origin="builtin">
        <Probe />
      </AppIdentityProvider>,
    )
    act(() => setLatest({ path: 'docs' }))
    expect(localStorage.getItem('kc:app:aws-control:view:drive')).not.toBeNull()
  })

  it('percent-encodes a scope so it cannot forge a segment boundary', () => {
    // `scope` is DATA, so it is encoded rather than refused. Escaping `:` is what stops one
    // scope value from addressing another scope's key.
    render(
      <AppIdentityProvider appId="aws-control" origin="builtin">
        <Probe scope="a:b" />
      </AppIdentityProvider>,
    )
    act(() => setLatest({ path: 'docs' }))
    expect(localStorage.getItem('kc:app:aws-control:view:drive:a%3Ab')).not.toBeNull()
    // And NOT under a key where the colon stayed raw, which would collide with scope 'a'
    // owning a surface called 'b'.
    expect(localStorage.getItem('kc:app:aws-control:view:drive:a:b')).toBeNull()
  })

  it.each([
    ['a parent traversal', '..'],
    ['an escape into another app', '../file-explorer'],
    ['a separator', 'drive/sub'],
    ['uppercase', 'Drive'],
    ['empty', ''],
  ])('refuses %s as a surface name', (_label, name) => {
    // The name becomes a key segment, so an unvalidated one addresses a namespace other
    // than its own. It is authored in code and never data, so it throws rather than being
    // sanitized into a namespace nobody asked for.
    const BAD: ViewStateDecl<DriveView> = { name, revision: 1, fields: { path: isViewString }, defaults: { path: '' } }
    function BadProbe() {
      useAppViewState(BAD, { scope: ACCOUNT })
      return null
    }
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    expect(() =>
      render(
        <AppIdentityProvider appId="aws-control" origin="builtin">
          <BadProbe />
        </AppIdentityProvider>,
      ),
    ).toThrow(/not a valid key segment/)
    spy.mockRestore()
  })
})

describe('useAppViewState — storage that refuses', () => {
  it('mounts with defaults when reading throws', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('SecurityError')
    })
    mount(builtin(ACCOUNT))
    expect(seen[0]).toEqual({ path: '' })
  })

  it('keeps working when writing throws', () => {
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('QuotaExceededError')
    })
    mount(builtin(ACCOUNT))
    act(() => setLatest({ path: 'docs' }))
    // The position is still live in the session; only its durability was lost.
    expect(seen[seen.length - 1]).toEqual({ path: 'docs' })
  })
})
