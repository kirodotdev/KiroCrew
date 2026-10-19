/**
 * `types/index.ts` and the owner files beside it.
 *
 * The facade is the one import path for the shared types it serves: consumers
 * across src/, capture/ and integration/ import `…/types`, and most of those
 * files are specs, which tsc does not check. So a name that only specs use, or
 * that nothing imports, would fail nowhere but here. The module has no runtime
 * values, so these pins read the family's source with the TypeScript parser:
 *
 *  - every name the file exported as one file is still served, by an explicit,
 *    unrenamed `export type { … } from` list naming the owner that declares it,
 *    and every name an owner exports is served the same way;
 *  - the facade declares nothing, and each name is declared once in the family;
 *  - owners import only sibling owners, with `import type`, never the facade,
 *    and export only through `export` on a declaration; nothing else in
 *    website/src, capture, integration, playwright, .storybook or scripts
 *    imports an owner.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { dirname, join, relative, resolve, sep } from 'node:path'
import ts from 'typescript'

const DIR = __dirname
const SRC = resolve(DIR, '..')
const WEBSITE = resolve(SRC, '..')
const FACADE = join(DIR, 'index.ts')
/** Owner files are `index.<area>.ts` beside the facade. */
const OWNER_RE = /^index\.[a-z][A-Za-z]*\.ts$/
const OWNERS = readdirSync(DIR).filter((f) => OWNER_RE.test(f)).sort().map((f) => join(DIR, f))
const FAMILY = [FACADE, ...OWNERS]
/** The website roots whose modules could import an owner. */
const ROOTS = ['src', 'capture', 'integration', 'playwright', '.storybook', 'scripts']
const read = (p: string) => readFileSync(p, 'utf8')
const parse = (p: string, text: string = read(p)) =>
  ts.createSourceFile(p, text, ts.ScriptTarget.Latest, true, ts.ScriptKind.TS)
/** A path relative to `website/`, with forward slashes on every platform. */
const webPath = (p: string) => relative(WEBSITE, p).split(sep).join('/')

// Every name `types/index.ts` exported as one file, in that file's order. Pinned as
// "still served", never as the whole namespace: an owner may add names.
const BASE_NAMES: readonly string[] = [
  'StatusData', 'UpdateCheckResult', 'SystemData', 'SessionStorageBucket', 'SessionStorageBatch',
  'SessionStorageReport', 'SessionStorageCleanup', 'SessionStorageEmptyJob', 'SessionInventoryItem',
  'SessionInventoryList', 'SessionInventoryDetail', 'SessionTrashRefusal', 'SessionTrashResult',
  'CronJob', 'Lesson', 'MemoryStoreSummary', 'RetiredMemory', 'MemoryBackup', 'MemoryCarveEntry',
  'Skill', 'SkillBudgetRow', 'SkillBudgetResponse', 'SkillTreeEntry', 'SteeringFile', 'SteeringList',
  'DiscoveredSkill', 'SkillProviderOutcome', 'DiscoverSkillsResponse', 'DiscoverSkillPreview',
  'DiscoverInstallResult', 'DiscoveredMcpServer', 'McpProviderOutcome', 'McpDiscoverResponse',
  'McpInstallPlan', 'McpDiscoverDetail', 'McpDiscoverInstallResult', 'McpCustomSpec',
  'McpCustomSpecResponse', 'McpScopePresence', 'McpTempRefusal', 'McpServer', 'McpApplyChange',
  'McpGlobalScope', 'TodoTask', 'TodoList', 'McpSessionReport', 'SessionLink',
  'ConfiguredChannelTarget', 'RemoteCrewApprovalState', 'RemoteCrewCapabilities', 'InstanceVersionRow',
  'InstanceVersions', 'RemoteUpdateJob', 'TurnIdentityFields', 'ChatDonePayload', 'ChatSlot',
  'PullRequestCommit', 'PullRequestCheck', 'PullRequestStatus', 'PullRequestStatusBatch',
  'PullRequestComment', 'PullRequestFile', 'IssueLabel', 'IssueMilestone', 'IssueComment',
  'SourceProviderId', 'IssueLinkedChange', 'IssueReactions', 'IssueSource', 'AppContributor',
  'PullRequestSource', 'ChatFolder', 'AgentTagPolicy', 'ChatTag', 'TagColumnMode', 'SessionLaneKey',
  'TagColumn', 'ChatMessage', 'ChatSlotDetailResponse', 'SubagentActivity', 'ToolPayloadCut',
  'ToolActivity', 'BlockType', 'ContentBlock', 'Notification', 'NotificationChannel', 'PendingApproval',
  'SubagentInfo', 'SessionInfo', 'TaskDetail', 'RunStatus', 'ProjectRun', 'TaskRunnerStatus',
  'ArtifactPublication', 'PublishProviderDescriptor', 'ForkMetadata', 'RemoteArtifact', 'Artifact',
  'SessionDoc', 'ArtifactFolder', 'ArtifactEvent', 'CommentAnchor', 'ArtifactComment',
  'WebAppDeployTarget', 'WebAppArchitecture', 'WebAppLifecycle', 'WebAppCost', 'WebAppTeardown',
  'WorkflowRunSummary', 'WebAppMetadata',
]

const hasExport = (s: ts.Statement) =>
  ts.canHaveModifiers(s) && (ts.getModifiers(s) ?? []).some((m) => m.kind === ts.SyntaxKind.ExportKeyword)
function bindingNames(n: ts.BindingName): string[] {
  return ts.isIdentifier(n)
    ? [n.text]
    : n.elements.flatMap((e) => (ts.isOmittedExpression(e) ? [] : bindingNames(e.name)))
}

/** Names a file declares with `export` on the declaration, in every declaration form: interfaces
 *  and type aliases are types; variables (any binding pattern, `declare` included), classes,
 *  functions, enums and namespaces are values. */
function declared(p: string, text?: string): { types: string[]; values: string[] } {
  const types: string[] = []
  const values: string[] = []
  for (const s of parse(p, text).statements) {
    if (!hasExport(s)) continue
    if (ts.isInterfaceDeclaration(s) || ts.isTypeAliasDeclaration(s)) types.push(s.name.text)
    else if (ts.isVariableStatement(s)) for (const d of s.declarationList.declarations) values.push(...bindingNames(d.name))
    else if (
      (ts.isClassDeclaration(s) || ts.isFunctionDeclaration(s) || ts.isEnumDeclaration(s) || ts.isModuleDeclaration(s)) &&
      s.name
    )
      values.push(s.name.text)
  }
  return { types, values }
}
const declaredCache = new Map<string, string[]>()
const declaredNames = (p: string) => {
  if (!declaredCache.has(p)) {
    const d = declared(p)
    declaredCache.set(p, [...d.types, ...d.values])
  }
  return declaredCache.get(p)!
}

interface ReExport { name: string; orig: string; from: string; typeOnly: boolean; star: boolean }
/** Every `export … from` row of a file: one per name, `*` for a wildcard or namespace re-export. */
function reExports(p: string, text?: string): ReExport[] {
  const rows: ReExport[] = []
  for (const s of parse(p, text).statements) {
    if (!ts.isExportDeclaration(s) || !s.moduleSpecifier || !ts.isStringLiteral(s.moduleSpecifier)) continue
    const from = s.moduleSpecifier.text
    if (!s.exportClause || ts.isNamespaceExport(s.exportClause)) {
      const name = s.exportClause ? s.exportClause.name.text : '*'
      rows.push({ name, orig: name, from, typeOnly: s.isTypeOnly, star: true })
      continue
    }
    for (const e of s.exportClause.elements)
      rows.push({ name: e.name.text, orig: (e.propertyName ?? e.name).text, from, typeOnly: s.isTypeOnly || e.isTypeOnly, star: false })
  }
  return rows
}

const stem = (p: string) => p.replace(/\.(tsx?|jsx?|mjs|cjs)$/, '').replace(/[\\/]index$/, '')
/** A specifier resolved to an absolute path without its extension or a trailing `/index`;
 *  `@/` is the `src/` alias. A bare package specifier comes back unchanged. */
function resolveSpec(file: string, spec: string): string {
  if (spec.startsWith('@/')) return stem(join(SRC, spec.slice(2)))
  if (spec.startsWith('.')) return stem(resolve(dirname(file), spec))
  return spec
}

/** Every relative or `@/` module a file names (static, side-effect, `export … from`, `import type`,
 *  dynamic, `import('…').X` type queries and vitest mocks), read from the raw text. */
const SPEC_RE = /(?:from\s+|import\s*\(?|(?:mock|doMock|importActual)\(\s*)\s*['"]([^'"]+)['"]/g
function importsOf(file: string, text = read(file)): string[] {
  return [...text.matchAll(SPEC_RE)]
    .map((m) => m[1])
    .filter((spec) => spec.startsWith('@/') || spec.startsWith('.'))
    .map((spec) => resolveSpec(file, spec))
}

/** Every module reference in an owner's syntax tree, comments and strings aside: import and
 *  export declarations, `import x = require()`, `import('…').X` type queries and calls. */
interface ModuleRef { kind: string; spec: string; typeOnly: boolean }
function moduleRefs(p: string, text?: string): ModuleRef[] {
  const refs: ModuleRef[] = []
  const sf = parse(p, text)
  const visit = (n: ts.Node) => {
    if (ts.isImportDeclaration(n) && ts.isStringLiteral(n.moduleSpecifier))
      refs.push({ kind: 'import', spec: n.moduleSpecifier.text, typeOnly: !!n.importClause?.isTypeOnly })
    else if (ts.isExportDeclaration(n))
      refs.push({ kind: 'export', spec: n.moduleSpecifier && ts.isStringLiteral(n.moduleSpecifier) ? n.moduleSpecifier.text : '', typeOnly: n.isTypeOnly })
    else if (ts.isExportAssignment(n)) refs.push({ kind: 'export =', spec: '', typeOnly: false })
    else if (ts.isImportEqualsDeclaration(n)) {
      const m = n.moduleReference
      const spec = ts.isExternalModuleReference(m) && ts.isStringLiteral(m.expression) ? m.expression.text : m.getText(sf)
      refs.push({ kind: 'import =', spec, typeOnly: n.isTypeOnly })
    }
    else if (ts.isImportTypeNode(n) && ts.isLiteralTypeNode(n.argument) && ts.isStringLiteral(n.argument.literal))
      refs.push({ kind: 'import()', spec: n.argument.literal.text, typeOnly: true })
    else if (ts.isCallExpression(n) && (n.expression.kind === ts.SyntaxKind.ImportKeyword || (ts.isIdentifier(n.expression) && n.expression.text === 'require')))
      refs.push({ kind: 'call', spec: n.arguments[0] && ts.isStringLiteral(n.arguments[0]) ? n.arguments[0].text : '', typeOnly: false })
    ts.forEachChild(n, visit)
  }
  visit(sf)
  return refs
}

function walk(dir: string, out: string[] = []): string[] {
  let names: string[]
  try {
    names = readdirSync(dir)
  } catch {
    return out
  }
  for (const name of names) {
    if (name === 'node_modules') continue
    const p = join(dir, name)
    if (statSync(p).isDirectory()) walk(p, out)
    else if (/\.(ts|tsx|mts|cts|js|jsx|mjs|cjs)$/.test(name)) out.push(p)
  }
  return out
}

/** Where the facade serves each name: the owner stem a list names, or the facade itself for a
 *  name it declares. */
function served(): Map<string, { from: string; row?: ReExport }> {
  const out = new Map<string, { from: string; row?: ReExport }>()
  for (const n of declaredNames(FACADE)) out.set(n, { from: stem(FACADE) })
  for (const r of reExports(FACADE)) out.set(r.name, { from: stem(resolve(DIR, r.from)), row: r })
  return out
}

describe('types/index.ts facade', () => {
  it('serves every name it exported as one file, from the owner that declares it, unrenamed', () => {
    expect(BASE_NAMES).toHaveLength(110)
    const map = served()
    expect(BASE_NAMES.filter((n) => !map.has(n))).toEqual([])
    const wrong = BASE_NAMES.filter((n) => {
      const s = map.get(n)!
      const owner = OWNERS.find((f) => stem(f) === s.from)
      return !owner || !declaredNames(owner).includes(n) || s.row?.orig !== n
    })
    expect(wrong).toEqual([])
  })

  it('declares nothing and re-exports owners with explicit `export type` lists only', () => {
    const offenders = parse(FACADE).statements.filter(
      (s) => !(ts.isExportDeclaration(s) && s.moduleSpecifier && s.isTypeOnly && s.exportClause && ts.isNamedExports(s.exportClause)),
    )
    expect(offenders.map((s) => s.getText().split('\n')[0])).toEqual([])
    const ownerStems = new Set(OWNERS.map(stem))
    expect(reExports(FACADE).filter((r) => !ownerStems.has(stem(resolve(DIR, r.from)))).map((r) => r.from)).toEqual([])
  })

  it('serves every name an owner exports, from that owner', () => {
    expect(OWNERS.length).toBeGreaterThan(0)
    const map = served()
    for (const owner of OWNERS) {
      const names = declaredNames(owner)
      expect(names.length, webPath(owner)).toBeGreaterThan(0)
      expect(names.filter((n) => map.get(n)?.from !== stem(owner)), webPath(owner)).toEqual([])
    }
  })

  it('declares each name once across the family', () => {
    const seen = FAMILY.flatMap((f) => declaredNames(f))
    expect(seen.filter((n, i) => seen.indexOf(n) !== i)).toEqual([])
  })
})

describe('types/index.ts owners', () => {
  it('never import the facade back', () => {
    const offenders = OWNERS.flatMap((o) =>
      moduleRefs(o).filter((r) => resolveSpec(o, r.spec) === stem(FACADE)).map((r) => `${webPath(o)}: ${r.kind} ${r.spec}`),
    )
    expect(offenders).toEqual([])
  })

  it('import only sibling owners, only with `import type`, and export only on declarations', () => {
    const ownerStems = new Set(OWNERS.map(stem))
    for (const owner of OWNERS) {
      const bad = moduleRefs(owner).filter((r) => {
        const target = resolveSpec(owner, r.spec)
        const sibling = ownerStems.has(target) && target !== stem(owner)
        return !(sibling && r.typeOnly && (r.kind === 'import' || r.kind === 'import()'))
      })
      expect(bad.map((r) => `${r.kind} ${r.spec}`), webPath(owner)).toEqual([])
    }
  })

  it('are imported by the family alone', () => {
    const owners = new Set(OWNERS.map(stem))
    const family = new Set(FAMILY.map((f) => resolve(f)))
    const self = resolve(__filename)
    const offenders = ROOTS.flatMap((root) => walk(join(WEBSITE, root)))
      .filter((f) => !family.has(resolve(f)) && resolve(f) !== self)
      .flatMap((f) => importsOf(f).filter((t) => owners.has(t)).map((t) => `${webPath(f)} -> ${webPath(t)}`))
    expect(offenders).toEqual([])
  })

  it('read imports and exports the way the pins above rely on', () => {
    const probe = join(DIR, 'probe.ts')
    const text = [
      "import './Side'",
      "import type { T } from '@/types/index.one'",
      "export type { X } from './index'",
      "let y: import('../types').ChatSlot",
      "const L = () => import('./Lazy')",
      "vi.mock('./index.two')",
      "import React from 'react'",
    ].join('\n')
    expect(importsOf(probe, text)).toEqual([
      join(DIR, 'Side'), join(DIR, 'index.one'), DIR, DIR, join(DIR, 'Lazy'), join(DIR, 'index.two'),
    ])
    const owner =
      "import type { A } from './index.one'\nimport { B } from '@/types/index.two'\n" +
      "// see import type { Q } from './index'\nconst s = \"from './index'\"\n" +
      "export type { L }\nexport default s\nimport R = require('./index.three')\n" +
      "type Q = import('./index').ChatSlot\nconst d = () => import('./index.four')\n"
    expect(moduleRefs(probe, owner).map((r) => [r.kind, r.spec, r.typeOnly])).toEqual([
      ['import', './index.one', true], ['import', '@/types/index.two', false], ['export', '', true],
      ['export =', '', false], ['import =', './index.three', false], ['import()', './index', true],
      ['call', './index.four', false],
    ])
    expect(resolveSpec(probe, '@/types/index.two')).toBe(join(DIR, 'index.two'))
    expect(resolveSpec(probe, 'react')).toBe('react')
    const sf =
      "export type { A, B as C } from './index.one'\nexport type * from './index.two'\n" +
      'export declare const f: boolean\nexport enum K { x }\nexport interface I {}\nexport let [p, , q] = [1, 2, 3]\n'
    expect(reExports(probe, sf).map((r) => [r.name, r.orig, r.typeOnly, r.star])).toEqual([
      ['A', 'A', true, false], ['C', 'B', true, false], ['*', '*', true, true],
    ])
    expect(declared(probe, sf)).toEqual({ types: ['I'], values: ['f', 'K', 'p', 'q'] })
    expect(stem(join(DIR, 'index.ts'))).toBe(DIR)
    expect(OWNER_RE.test('index.chat.ts') && !OWNER_RE.test('index.owners.test.tsx') && !OWNER_RE.test('index.ts')).toBe(true)
  })
})
