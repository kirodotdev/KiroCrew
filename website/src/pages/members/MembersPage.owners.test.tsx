/**
 * The Crewmates page's facade and its owners beside it.
 *
 * MembersPage.tsx stays the page's one public surface (App's lazy route and the
 * capture entries import its default export; tests import its named exports)
 * and calls each owner hook at the point its code sits in the page's hook order.
 * These pins keep that shape honest:
 *
 *  - the facade still exports every name its importers use, each the SAME
 *    binding its owner defines, not a copy;
 *  - owners never import the facade, and nothing outside this page imports an
 *    owner, so a `vi.mock('./MembersPage')` (App.mobileSingleTopbar.test.tsx)
 *    still replaces the whole page;
 *  - the facade calls the owner hooks, and runs the page code it keeps, in the
 *    page's registration order. React runs a component's effects in that order,
 *    so a reshuffle is a behaviour change even when every test of each owner
 *    still passes. Kept page code an owner is ordered against is pinned in the
 *    same sequence by an anchor text unique to it.
 *
 * The order pin guards the split itself: the owners are stretches of the hook
 * sequence the page had as one file, not a design to keep. A later change that
 * regroups state by responsibility (the Schedules guard state declared in
 * `useMembersPageSidePanel`, say) reorders registrations on purpose, reviews that
 * as the behaviour change it is, and edits `ORDER` below to match.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve } from 'node:path'

import * as facade from './MembersPage'
import * as model from './membersPageModel'
import { membersPageOwners } from '../../test/membersPageFamily'

const DIR = __dirname
const SRC = resolve(DIR, '../..')
const FACADE = join(DIR, 'MembersPage.tsx')
// The page-file rule lives in one place, the shared helper the source pins read too.
const owners = membersPageOwners()
const read = (p: string) => readFileSync(p, 'utf8')

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.(ts|tsx)$/.test(name) && !/\.test\.|\.spec\./.test(name)) out.push(p)
  }
  return out
}

/** Every module a file imports (static, `export … from` and dynamic), resolved to
 *  an absolute path without its extension. `@/` is the `src/` alias. */
const SPEC_RE = /(?:from\s+|import\s*\()\s*['"]([^'"]+)['"]/g
const stem = (p: string) => p.replace(/\.(tsx?|jsx?)$/, '').replace(/\/index$/, '')
function importsOf(file: string): string[] {
  return [...read(file).matchAll(SPEC_RE)].map((m) => m[1]).flatMap((spec) => {
    if (spec.startsWith('@/')) return [stem(join(SRC, spec.slice(2)))]
    if (spec.startsWith('.')) return [stem(resolve(dirname(file), spec))]
    return []
  })
}

describe('MembersPage facade', () => {
  it('keeps the exports its importers use, as the owners’ own bindings', () => {
    expect(typeof facade.default).toBe('function')
    expect(facade.resolveDefaultMember).toBe(model.resolveDefaultMember)
    expect(facade.resolveMateLanding).toBe(model.resolveMateLanding)
    expect(facade.memberMemoryDisplay).toBe(model.memberMemoryDisplay)
    expect(facade.panelSitsBeside).toBe(model.panelSitsBeside)
    expect(facade.panelChrome).toBe(model.panelChrome)
    expect(facade.createDraftFromHref).toBe(model.createDraftFromHref)
    expect(facade.CREW_DASHBOARD_TAB_ID).toBe(model.CREW_DASHBOARD_TAB_ID)
    expect(facade.CREW_PANEL_TAB_IDS).toBe(model.CREW_PANEL_TAB_IDS)
    expect(facade.MEMBERS_UNFED_VIEWS).toBe(model.MEMBERS_UNFED_VIEWS)
    expect(facade.MEMBERS_WITHHELD_VIEWS).toBe(model.MEMBERS_WITHHELD_VIEWS)
    expect(facade.MEMBERS_UNCONFIRMED_WITHHELD_VIEWS).toBe(model.MEMBERS_UNCONFIRMED_WITHHELD_VIEWS)
  })
})

describe('MembersPage owners', () => {
  it('exist, and none imports the facade back', () => {
    expect(owners.length).toBeGreaterThan(0)
    for (const f of owners) {
      expect(importsOf(join(DIR, f)), f).not.toContain(stem(FACADE))
    }
  })

  it('are imported only by the facade and by each other', () => {
    const ownerPaths = new Set(owners.map((f) => join(DIR, f)))
    const ownerStems = new Set(owners.map((f) => stem(join(DIR, f))))
    const outside = walk(SRC)
      .filter((p) => p !== FACADE && !ownerPaths.has(p))
      .filter((p) => importsOf(p).some((s) => ownerStems.has(s)))
      .map((p) => relative(SRC, p))
    expect(outside).toEqual([])
  })

  it('are called by the facade in the page’s hook registration order', () => {
    const src = read(FACADE)
    const ORDER = [
      'const rosterRead = useMembersPageRoster()',
      "const [editingCrew, setEditingCrew] = useState('')",
      'const [profile, setProfile] = useState<',
      'useMembersPageCrewEditor(',
      'useMembersPageTeams(',
      "markGuideOpenedRef.current = useGuideOpenedSurface('crewmate.create'",
      'const threadQuery = useQuery<MemberThreadOutcome>(',
      'useMembersPageLayout(',
      'useMembersPageProfile(',
      'useMembersPageDrawers(',
      'const liveSlots = useAppSelector(',
      "useGuideSelection('crewmate_selected'",
      "useGuideRevealScope(isMobile ? undefined : 'members.roster'",
      'const membersRootRef = useRef',
      "useGuideRevealScope(isMobile ? 'members.roster-phone'",
      'useMembersPageDrawerSwipes(',
      "useGuideRevealScope('members.profile'",
      'useMembersPageRosterView(',
      'useMembersPageThread(',
      'useMembersPageSidePanel(',
      'useMembersPageSchedules(',
      'useRegisterNavigationLeaveGuard(',
      'usePublishNavigationStake(',
      'useMembersPageThreadView(',
      'useMembersPageRosterSignals(',
      'useMembersPageProfileFlight(',
      'const [leftCreateFailure, setLeftCreateFailure]',
      'useMembersPageNavigation(',
      'useMembersPageTeamView(',
    ]
    const at = ORDER.map((anchor) => {
      const first = src.indexOf(anchor)
      expect(first, anchor).toBeGreaterThan(-1)
      expect(src.indexOf(anchor, first + 1), `${anchor} appears once`).toBe(-1)
      return first
    })
    for (let i = 1; i < at.length; i++) {
      expect(at[i], `${ORDER[i]} after ${ORDER[i - 1]}`).toBeGreaterThan(at[i - 1])
    }
  })

  it('each owner hook is called once, by the facade', () => {
    const hooks = owners.flatMap((f) => [...read(join(DIR, f)).matchAll(/^export function (use\w+)\(/gm)].map((m) => m[1]))
    expect(hooks.length).toBeGreaterThan(0)
    const src = read(FACADE)
    for (const h of hooks) {
      expect(src.match(new RegExp(`\\b${h}\\(`, 'g'))?.length ?? 0, h).toBe(1)
      for (const f of owners) {
        const calls = read(join(DIR, f)).match(new RegExp(`(?<!function )\\b${h}\\(`, 'g'))?.length ?? 0
        expect(calls, `${f} calls ${h}`).toBe(0)
      }
    }
  })
})
