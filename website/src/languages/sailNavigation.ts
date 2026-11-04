/**
 * Cmd/Ctrl+click "go to definition" for SAIL, in the spirit of the SAIL
 * IntelliJ plugin's reference navigation:
 *  - `a!name` / `rule!name` / `fn!name` -> opens `SYSTEM_SYSRULES_name.xml`
 *    (or `name.xml`) found by filename search in the chat's project;
 *  - `ri!name`    -> jumps to the `<name>name</name>` rule-input definition in
 *                    the same file;
 *  - `local!name` -> jumps to the first `local!name:` binding in the same file.
 *
 * Works on both Pierre surfaces (read-only view and editor) by reading the
 * clicked token span from the event's composed path (Pierre renders rows in a
 * shadow root) and its offset within the row.
 */
import { api } from '../api/client'

const REF_RE = /'?\b(rule|a|fn|ri|local)!([A-Za-z_][A-Za-z0-9_]*)'?/g

export interface SailRef { domain: string; name: string }

/** The SAIL reference under a click, or null. */
export function sailRefAtEvent(ev: MouseEvent): SailRef | null {
  const path = ev.composedPath()
  const token = path.find((n): n is HTMLElement => n instanceof HTMLElement && !!n.textContent && n.childElementCount === 0)
  if (!token) return null
  // The row: the nearest ancestor Pierre marks with a line attribute.
  const row = path.find((n): n is HTMLElement =>
    n instanceof HTMLElement && n !== token && (n.hasAttribute('data-line') || n.hasAttribute('data-line-index')))
    // Fallback when the surface does not mark rows: the token's own parent.
    ?? token.parentElement
  if (!row) return null
  // Offset of the clicked token within the row's text.
  let offset = 0
  const walker = document.createTreeWalker(row, NodeFilter.SHOW_TEXT)
  for (let t = walker.nextNode(); t; t = walker.nextNode()) {
    if (token.contains(t)) break
    offset += t.textContent?.length ?? 0
  }
  const line = row.textContent ?? ''
  const end = offset + (token.textContent?.length ?? 0)
  for (const m of line.matchAll(REF_RE)) {
    const s = m.index ?? 0
    if (s < end && s + m[0].length > offset) {
      if (m[1] === 'local' && /^\s*:(?!=)/.test(line.slice(s + m[0].length))) return { domain: 'localdecl', name: m[2] }
      return { domain: m[1], name: m[2] }
    }
  }
  // A Java-backed function declaration (the patterns the SAIL plugin's
  // SystemFunctionDeclarationsCache reads): FN_NAME = "x", register("x", ...),
  // new Id(Domain.X, "x").
  for (const re of JAVA_DECL_RES) {
    for (const m of line.matchAll(re)) {
      const s = (m.index ?? 0) + m[0].lastIndexOf(m[1])
      if (s < end && s + m[1].length > offset) return { domain: 'jdecl', name: m[1] }
    }
  }
  // A declaration: `<name>foo</name>` (the rule's own name, or a rule input).
  for (const m of line.matchAll(DECL_RE)) {
    const s = (m.index ?? 0) + m[0].indexOf(m[1])
    if (s < end && s + m[1].length > offset) return { domain: 'decl', name: m[1] }
  }
  return null
}

const DECL_RE = /<name>\s*([A-Za-z_][A-Za-z0-9_]*)\s*<\/name>/g
const JAVA_DECL_RES = [
  /FN_NAME\s*=\s*"([A-Za-z_][A-Za-z0-9_]*)"/g,
  /\.register\(\s*"([A-Za-z_][A-Za-z0-9_]*)"/g,
  /new Id\(Domain\.[A-Z_]+,\s*"([A-Za-z_][A-Za-z0-9_]*)"\)/g,
]

export interface SailUsage { path: string; line: number; text: string }

/** The rule name a file defines (`SYSTEM_SYSRULES_foo.xml` -> `foo`). */
export function ruleNameOfFile(filePath: string): string {
  const base = filePath.split('/').pop() ?? ''
  return base.replace(/\.xml$/i, '').replace(/^SYSTEM_SYSRULES_/i, '')
}

/** Usages of a declared name: repo-wide callers for the rule's own name
 *  (`a!name` / `rule!name`, via git grep), or in-file `ri!name` uses for a
 *  rule input. */
export async function findSailUsages(name: string, ctx: { filePath: string; content: string; local?: boolean; repo?: boolean }): Promise<{ usages: SailUsage[]; truncated: boolean; scope: 'repo' | 'file' }> {
  if (ctx.repo || (!ctx.local && name.toLowerCase() === ruleNameOfFile(ctx.filePath).toLowerCase())) {
    const r = await fetch(`/api/sail/usages?file=${encodeURIComponent(ctx.filePath)}&name=${encodeURIComponent(name)}`, { credentials: 'same-origin' })
    const body = r.ok ? await r.json() as { usages: SailUsage[]; truncated: boolean } : { usages: [], truncated: false }
    return { usages: body.usages, truncated: body.truncated, scope: 'repo' }
  }
  const esc = name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  const re = new RegExp(`\\b${ctx.local ? 'local' : 'ri'}!${esc}\\b`, 'i')
  const usages = ctx.content.split('\n').flatMap((text, i) => (re.test(text) ? [{ path: ctx.filePath, line: i + 1, text: text.trim() }] : []))
  return { usages, truncated: false, scope: 'file' }
}

/** 1-based line of the first match of `re` in `content`, or undefined. */
function lineOf(content: string, re: RegExp): number | undefined {
  const lines = content.split('\n')
  const i = lines.findIndex(l => re.test(l))
  return i >= 0 ? i + 1 : undefined
}

/** Resolve and open a SAIL reference. Returns false when nothing was found. */
export async function openSailRef(
  ref: SailRef,
  ctx: { filePath: string; content: string; project?: string; onFileOpen: (path: string, opts?: { line?: number }) => void },
): Promise<boolean> {
  const esc = ref.name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  if (ref.domain === 'ri') {
    const line = lineOf(ctx.content, new RegExp(`<name>\\s*${esc}\\s*</name>`))
    if (line) ctx.onFileOpen(ctx.filePath, { line })
    return !!line
  }
  if (ref.domain === 'local') {
    const line = lineOf(ctx.content, new RegExp(`local!${esc}\\s*:`))
    if (line) ctx.onFileOpen(ctx.filePath, { line })
    return !!line
  }
  // a! / rule! / fn!: from the per-repo index (cached in memory) -- instant
  // after the first load. A SAIL rule file (rules) wins for a!/rule!; a
  // Java-backed function (functions: FN_NAME / register("x") / @Function)
  // answers fn! and any a! that is not a rule.
  let index = await sailRuleIndex(ctx.filePath)
  const key = ref.name.toLowerCase()
  const resolve = (i: SailRuleIndex | null): string | undefined => {
    if (!i) return undefined
    if (ref.domain === 'rule') return i.rules[key]
    if (ref.domain === 'fn') return i.functions[key] ?? i.rules[key]
    return i.rules[key] ?? i.functions[key]
  }
  let target = resolve(index)
  // The Java index builds in the background (~7s on first sight of a repo):
  // a miss before it landed refetches the index once.
  if (!target && index && !index.functionsReady) {
    index = await sailRuleIndex(ctx.filePath, true)
    target = resolve(index)
  }
  if (target) {
    // Java targets carry ":line".
    const m = /^(.*):(\d+)$/.exec(target)
    if (m) ctx.onFileOpen(m[1], { line: Number(m[2]) })
    else ctx.onFileOpen(target)
    return true
  }
  // Not in git (an untracked new rule): fall back to the filename search,
  // scoped to the current file's content tree so the walk stays small.
  const parts = ctx.filePath.split('/')
  const anchor = Math.max(parts.lastIndexOf('content'), parts.lastIndexOf('appianApp'))
  if (anchor <= 0) return false
  const q = `system_sysrules_${ref.name}.xml`.toLowerCase()
  try {
    const { results } = await api.fileSearch(q, parts.slice(0, anchor + 1).join('/'), undefined, 'files', 15)
    const hit = results.find(r => r.name.toLowerCase() === q)
    if (hit) { ctx.onFileOpen(hit.path); return true }
  } catch { /* fall through */ }
  return false
}

interface SailRuleIndex {
  root: string
  /** rule name -> abs path of SYSTEM_SYSRULES_<name>.xml */
  rules: Record<string, string>
  /** Java-backed function name -> "abs path:line" */
  functions: Record<string, string>
  functionsReady: boolean
}
/** Loaded indexes, one per repo root, plus in-flight loads keyed by root/dir. */
const loadedIndexes: SailRuleIndex[] = []
const inflight = new Map<string, Promise<SailRuleIndex | null>>()

/** The rule index for the repo holding `filePath` (memoized for the session;
 *  `refresh` drops the cached copy first). */
export function sailRuleIndex(filePath: string, refresh = false): Promise<SailRuleIndex | null> {
  const at = loadedIndexes.findIndex(i => filePath.startsWith(i.root + '/'))
  if (at >= 0 && !refresh) return Promise.resolve(loadedIndexes[at])
  if (at >= 0) loadedIndexes.splice(at, 1)
  const dir = filePath.slice(0, filePath.lastIndexOf('/')) || filePath
  const pending = inflight.get(dir)
  if (pending) return pending
  const load = fetch(`/api/sail/rule-index?file=${encodeURIComponent(filePath)}`, { credentials: 'same-origin' })
    .then(r => (r.ok ? r.json() : null))
    .then((body: { root: string | null; rules: Record<string, string>; functions?: Record<string, string>; functionsReady?: boolean } | null) => {
      if (!body?.root) return null
      const idx = { root: body.root, rules: body.rules, functions: body.functions ?? {}, functionsReady: !!body.functionsReady }
      loadedIndexes.push(idx)
      return idx
    })
    .catch(() => null)
    .finally(() => inflight.delete(dir))
  inflight.set(dir, load)
  return load
}
