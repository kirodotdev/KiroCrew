/**
 * Issue Radar's api client facade and its two owners.
 *
 * `api.ts` stays the one import path for the client: most of the app's modules
 * import it and some thirty specs `vi.mock` it by module id, so the calls and
 * every public shape must stay reachable there. The shapes themselves live in
 * `apiTypes.ts` (repos, issues, pull requests) and `apiCrewTypes.ts` (crews).
 * These pins keep that shape honest:
 *
 *  - every name in the pinned lists below is still exported by the facade, and
 *    each value is the owner's own binding, not a copy, so an `instanceof`
 *    against a re-exported error class matches what the calls throw;
 *  - every public name an owner declares is re-exported, and the two decoders
 *    the owner exports for the calls alone are not;
 *  - no owner imports the facade back, and nothing else in the app imports an
 *    owner, so a spec's mock of the facade reaches every importer.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve, sep } from 'node:path'

import * as facade from './api'
import * as apiTypes from './apiTypes'
import * as apiCrewTypes from './apiCrewTypes'

const SRC = resolve(__dirname, '..', '..')
const FACADE = join(__dirname, 'api.ts')
const OWNERS = { apiTypes: join(__dirname, 'apiTypes.ts'), apiCrewTypes: join(__dirname, 'apiCrewTypes.ts') }
type Owner = keyof typeof OWNERS
const read = (p: string) => readFileSync(p, 'utf8')
/** A path relative to `src/`, with forward slashes on every platform. */
const srcPath = (p: string) => relative(SRC, p).split(sep).join('/')

// The runtime values the facade serves, by the owner that defines each.
const PINNED_VALUES: Record<Owner, readonly string[]> = {
  apiTypes: [
    'GitLabGroupError', 'AssigneesConflictError', 'DEFAULT_REPO_SETTINGS', 'SettingsConflictError',
    'accountQuery', 'repoQuery', 'repoBody',
  ],
  apiCrewTypes: [
    'CREW_PHASES', 'CREW_EVENT_KINDS', 'TERMINAL_PHASES', 'TTL_ACTIVE_PHASES', 'EDITING_PHASES',
    'countsTowardOpen',
  ],
}

// The interfaces and type aliases the facade serves, by owner.
const PINNED_TYPES: Record<Owner, readonly string[]> = {
  apiTypes: [
    'ConnectResponse', 'Issue', 'IssuesResponse', 'PullRequest', 'PullsResponse', 'GitLabGroup',
    'GitLabGroupsResponse', 'GitLabGroupReview', 'GitLabGroupReviewsResponse', 'PrDetailData', 'PrCheck',
    'PullDetailResponse', 'Reactions', 'DetailLabel', 'Milestone', 'IssueDetailData', 'TimelineEvent',
    'IssueDetailResponse', 'RefSummary', 'RefSummaryResponse', 'SuggestedLabel', 'IssueAiResponse',
    'PrAiResponse', 'ApplyLabelsResponse', 'IssueStateResponse', 'IssueAssigneesResponse', 'PrAction',
    'BulkPrAction', 'PrMergeResponse', 'PrStateResponse', 'PrReviewResponse', 'PrCommentResponse',
    'PrAutoMergeResponse', 'PrWorkflowRun', 'PrRunsResponse', 'CiRun', 'CiRepoRuns', 'CiRunsResponse',
    'PrRunActionResponse', 'BulkPrResponse', 'RepoLabel', 'LabelsResponse', 'RepoMember', 'MembersResponse',
    'RepoPermissions', 'RepoSettings', 'SettingsResponse', 'RecommendationCategory', 'LabelRecommendation',
    'RecommendationsResponse', 'CreateLabelResponse', 'TaggingSuggestions', 'UntaggedIssue',
    'TaggingResponse', 'GenerateTaggingResponse', 'BulkApplyResponse', 'ConnectedRepo', 'ReposResponse',
    'RecentRepo', 'GhSetupReason', 'RecentReposResponse', 'MeResponse', 'InvestigationFindings',
    'InvestigationRecord', 'ItemKind', 'InvestigationResponse', 'InvestigationPatch', 'ApiError', 'DepEdge',
    'DepNode', 'DepsResponse', 'RepoRef', 'SourceProvider', 'AccountScope',
  ],
  apiCrewTypes: [
    'CrewPhase', 'CrewEventKind', 'CrewTriedEntry', 'CrewCiState', 'Crew', 'WorkItem', 'CrewEvent',
    'CrewSettings', 'CrewCounts', 'CrewPatch', 'CrewSpec', 'WorkItemPatch', 'CrewSettingsPatch',
    'CrewsResponse', 'CrewResponse', 'CrewNamesResponse', 'CrewDetailResponse', 'CrewWorkResponse',
    'CrewSettingsResponse',
  ],
}

// Exported by `apiTypes.ts` for `issueRadarApi` alone; the facade never serves them.
const FACADE_PRIVATE = ['parseErrorBody', 'gitlabGroupError']

const owners = { apiTypes, apiCrewTypes } as Record<Owner, Record<string, unknown>>
const facadeNs = facade as Record<string, unknown>
const ownerNames = Object.keys(OWNERS) as Owner[]

/** Every top-level `export` declaration form: `declare`, `async` and `abstract` prefixes, and enums,
 *  `let` / `var`, generators and namespaces as well as the forms the owners use today. */
const DECL_RE =
  /^export\s+(?:declare\s+)?(?:async\s+)?(?:abstract\s+)?(const\s+enum|enum|interface|type|class|const|let|var|function\*?|namespace)\s+(\w+)/gm

/** Names an owner declares with `export`, read from its source: types are interfaces and aliases,
 *  everything else (an enum included) is a value. */
function declared(owner: Owner): { values: string[]; types: string[] } {
  const values: string[] = []
  const types: string[] = []
  for (const m of read(OWNERS[owner]).matchAll(DECL_RE)) {
    ;(m[1] === 'interface' || m[1] === 'type' ? types : values).push(m[2])
  }
  return { values, types }
}

/** Names the facade re-exports from one owner: `export { ... } from` for values, `export type { ... } from`
 *  for types. */
function facadeExports(owner: Owner, kind: 'value' | 'type'): string[] {
  const out: string[] = []
  const re = new RegExp(`export ${kind === 'type' ? 'type ' : ''}\\{([^}]*)\\} from '\\./${owner}'`, 'g')
  for (const m of read(FACADE).matchAll(re)) out.push(...m[1].split(',').map(s => s.trim()).filter(Boolean))
  return out
}

function walk(dir: string, out: string[] = []): string[] {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.(ts|tsx)$/.test(name) && !/\.test\.|\.spec\./.test(name) && !srcPath(p).startsWith('test/')) out.push(p)
  }
  return out
}

/** Every module a file imports (static, side-effect, `export … from`, `import type` and
 *  dynamic), resolved to an absolute path without its extension. `@/` is the `src/` alias. */
const SPEC_RE = /(?:from\s+|import\s*\(?)\s*['"]([^'"]+)['"]/g
const stem = (p: string) => p.replace(/\.(tsx?|jsx?)$/, '').replace(/[\\/]index$/, '')
function importsOf(file: string): string[] {
  return [...read(file).matchAll(SPEC_RE)].map(m => m[1]).flatMap(spec => {
    if (spec.startsWith('@/')) return [stem(join(SRC, spec.slice(2)))]
    if (spec.startsWith('.')) return [stem(resolve(dirname(file), spec))]
    return []
  })
}

describe('issue-radar api facade', () => {
  it('exports every pinned value, as the owner binding', () => {
    expect(typeof facade.issueRadarApi.issues).toBe('function')
    for (const owner of ownerNames) {
      for (const name of PINNED_VALUES[owner]) {
        expect(owners[owner][name], `${owner}.${name}`).toBeDefined()
        expect(facadeNs[name], name).toBe(owners[owner][name])
      }
    }
  })

  it('exports every pinned type', () => {
    for (const owner of ownerNames) {
      const served = new Set(facadeExports(owner, 'type'))
      expect(PINNED_TYPES[owner].filter(n => !served.has(n)), owner).toEqual([])
      const own = new Set(declared(owner).types)
      expect(PINNED_TYPES[owner].filter(n => !own.has(n)), `${owner} declares`).toEqual([])
    }
  })

  it('re-exports every public name an owner declares, and not the decoders', () => {
    for (const owner of ownerNames) {
      const { values, types } = declared(owner)
      expect(values.length + types.length, owner).toBeGreaterThan(0)
      const servedTypes = new Set(facadeExports(owner, 'type'))
      expect(types.filter(n => !servedTypes.has(n)), `${owner} types`).toEqual([])
      const servedValues = new Set(facadeExports(owner, 'value'))
      const publicValues = values.filter(n => !FACADE_PRIVATE.includes(n))
      expect(publicValues.filter(n => !servedValues.has(n)), `${owner} values`).toEqual([])
      expect(publicValues.filter(n => facadeNs[n] !== owners[owner][n]), `${owner} bindings`).toEqual([])
    }
    const served = new Set(ownerNames.flatMap(o => facadeExports(o, 'value')))
    for (const name of FACADE_PRIVATE) {
      expect(typeof (apiTypes as Record<string, unknown>)[name], name).toBe('function')
      expect(name in facadeNs, name).toBe(false)
      expect(served.has(name), name).toBe(false)
    }
  })
})

describe('issue-radar api owners', () => {
  it('never import the facade back', () => {
    for (const [owner, path] of Object.entries(OWNERS)) expect(importsOf(path), owner).not.toContain(stem(FACADE))
  })

  it('are imported by the facade alone', () => {
    const ownerStems = Object.values(OWNERS).map(stem)
    const importers = walk(SRC).filter(f => importsOf(f).some(t => ownerStems.includes(t))).map(srcPath).sort()
    expect(importers).toEqual(['apps/issue-radar/api.ts'])
  })
})
