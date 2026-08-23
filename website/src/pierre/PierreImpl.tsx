/**
 * The one module that imports `@pierre/diffs` at runtime. Reached only through
 * the `React.lazy` boundaries in `./index`, so Pierre + Shiki stay out of the
 * eager bundle. Every surface renders through these three components, and all
 * of them resolve their options through `./config` — the single place the
 * look/behavior of code and diff rendering is decided.
 */
import { useId, useMemo } from 'react'
import type { BaseCodeOptions, FileContents, SupportedLanguages } from '@pierre/diffs'
import { EXTENSION_TO_FILE_FORMAT, parsePatchFiles, setCustomExtension } from '@pierre/diffs'
import { File, FileDiff, MultiFileDiff, Virtualizer, WorkerPoolContext } from '@pierre/diffs/react'
import { getOrCreateWorkerPoolSingleton } from '@pierre/diffs/worker'
import { useIsDark } from '../hooks/useIsDark'
import { recordCacheKey, startPierrePerfReporting } from '../lib/pierrePerf'
import { PlainCodeFallback } from './PlainCodeFallback'
import {
  PIERRE_EXTENSION_OVERRIDES,
  PIERRE_THEMES,
  PIERRE_VIRTUALIZER_CONFIG,
  PIERRE_WORKER_POOL_SIZE,
  pierreDiffOptions,
  pierreFileOptions,
  pierreThemeType,
  type PierreDiffOptions,
} from './config'

// Registered once, at the only module that loads the library, so every surface
// that resolves a language from a FILENAME picks the override up. Fence tags go
// through `fenceLanguage` below, which consults the same table — Pierre's custom
// map is keyed for filename lookups and is not consulted by a direct
// EXTENSION_TO_FILE_FORMAT read.
for (const [ext, lang] of Object.entries(PIERRE_EXTENSION_OVERRIDES)) {
  setCustomExtension(ext, lang as SupportedLanguages)
}

/** Markdown fence tags that are Shiki language NAMES rather than file
 *  extensions (extensions resolve through EXTENSION_TO_FILE_FORMAT). Only
 *  tags known to resolve are forwarded; anything else falls back to plain
 *  text instead of surfacing a highlighter error in the block. */
const FENCE_NAME_LANGS = new Set<string>([
  'python', 'bash', 'shell', 'zsh', 'console', 'typescript', 'javascript', 'tsx', 'jsx',
  'rust', 'yaml', 'json', 'jsonc', 'markdown', 'html', 'css', 'scss', 'less', 'sql',
  'java', 'kotlin', 'ruby', 'cpp', 'c', 'csharp', 'go', 'php', 'swift', 'diff',
  'docker', 'dockerfile', 'hcl', 'terraform', 'proto', 'ini', 'xml', 'http', 'graphql',
  'lua', 'perl', 'r', 'scala', 'toml', 'vue', 'svelte', 'haskell', 'clojure', 'elixir',
  'erlang', 'dart', 'groovy', 'julia', 'latex', 'make', 'makefile', 'nginx',
  'objective-c', 'powershell', 'prisma', 'regex', 'solidity', 'vim', 'zig',
])

/** Resolve a markdown fence tag to a language Pierre can highlight. */
export function fenceLanguage(tag?: string): SupportedLanguages {
  if (!tag) return 'text'
  const t = tag.toLowerCase()
  // Checked first so a ```tex fence and a .tex FILE render through the same
  // grammar; the extension table below would answer with the coarser default.
  const override = PIERRE_EXTENSION_OVERRIDES[t]
  if (override != null) return override as SupportedLanguages
  const mapped = EXTENSION_TO_FILE_FORMAT[t]
  if (mapped != null) return mapped
  if (FENCE_NAME_LANGS.has(t)) return t as SupportedLanguages
  return 'text'
}

/** Content-derived cache key (djb2). Pierre defaults a file's cacheKey to its
 *  NAME and caches highlight results by it, so two renders of the same file
 *  name with different text (a streaming patch, a live-edited buffer) would
 *  serve the first render's cached tokens forever. Keying on content keeps the
 *  cache correct while still deduping identical re-renders.
 *
 *  `surface` identifies the MOUNTED SURFACE INSTANCE for the churn accounting
 *  ONLY — it never enters the returned key, so cache identity is unchanged. It
 *  must be instance-qualified (each caller prefixes a React `useId()`), because
 *  every content-derived proxy identity admits collisions: a filename conflates
 *  a diff's two sides, and a surface KIND still conflates two same-named fences
 *  rendered independently. Only the component instance is the true unit of
 *  tokenization — two instances can never share a `useId`, while a streaming
 *  block is one instance re-rendering, so churn attribution is exact in both
 *  directions. */
export function contentCacheKey(name: string, contents: string, surface = 'file'): string {
  let h = 5381
  for (let i = 0; i < contents.length; i++) h = ((h << 5) + h + contents.charCodeAt(i)) | 0
  const key = `${name}:${contents.length}:${(h >>> 0).toString(36)}`
  // Accounting for the streaming re-tokenize hypothesis (see lib/pierrePerf.ts).
  // This is the right site because it is the one place that observes both halves
  // of the cost: the O(n) hash just paid, and the key whose churn decides whether
  // Pierre reuses cached tokens or re-tokenizes the whole file again.
  recordCacheKey(surface, name, key, contents.length)
  return key
}

/** Rewrites hand-written patches into ones Pierre's parser accepts.
 *
 *  Pierre is structure-driven: content lives inside a hunk, and a hunk needs a
 *  well-formed `@@ -o,c +n,c @@` whose counts match its body. Hand-authored
 *  patches fail that three ways, each with its own broken render:
 *   - header without line numbers (`@@`, `@@ .selector @@`) — the hunk is
 *     dropped, and with no hunks left the block falls to plain text;
 *   - header whose counts disagree with the body — the hunk renders truncated
 *     or is rejected outright;
 *   - NO header at all, just `---`/`+++` and `+`/`-` lines — the file parses
 *     with zero hunks and Pierre reads it as a pure RENAME: header only,
 *     `+0 −0`, no rows.
 *  So counts are always recomputed from the body (never trusted), and a file
 *  section carrying changes without any header gets one synthesized. Declared
 *  start lines are preserved — only the counts are authoritative here. */
export function normalizePatchHunks(patch: string): string {
  const lines = patch.split('\n')
  let oldLine = 1
  let newLine = 1
  let changed = false
  /** Hunk-body extents derived from UNAMBIGUOUS delimiters only (`@@`, `diff `).
   *  Header detection consults this, so it can ask "am I inside a hunk body?"
   *  without depending on itself. */
  const markHunkBodies = () => {
    const body = new Array<boolean>(lines.length).fill(false)
    for (let h = 0; h < lines.length; h++) {
      if (!lines[h].startsWith('@@')) continue
      for (let j = h + 1; j < lines.length; j++) {
        if (lines[j].startsWith('@@') || lines[j].startsWith('diff ')) break
        body[j] = true
      }
    }
    return body
  }
  let hunkBody = markHunkBodies()
  /** True when `--- `/`+++ ` at `i` is a real file-header pair rather than a
   *  hunk body line deleting `-- x` / adding `++ x`.
   *
   *  Inside a hunk body those two are indistinguishable by shape — a deletion of
   *  `-- foo/bar` IS the text `--- foo/bar` — so a pair there is content unless
   *  it announces itself the way a real file section does: `diff ` above it, or a
   *  `@@` hunk header immediately below. Known limit: a second file section that
   *  is BOTH headerless and un-announced while a previous hunk is open reads as
   *  content; git always emits `diff --git`, so that shape is not produced. */
  const isFileHeader = (i: number) => {
    const minus = lines[i].startsWith('--- ') ? i : lines[i].startsWith('+++ ') ? i - 1 : -1
    if (minus < 0) return false
    if (!(lines[minus] ?? '').startsWith('--- ')) return false
    if (!(lines[minus + 1] ?? '').startsWith('+++ ')) return false
    if (!hunkBody[minus]) return true
    return (lines[minus - 1] ?? '').startsWith('diff ') || (lines[minus + 2] ?? '').startsWith('@@')
  }
  /** Body extent + line tallies for the hunk starting after `start`. */
  const measure = (start: number) => {
    let oldCount = 0
    let newCount = 0
    let end = lines.length
    for (let j = start + 1; j < lines.length; j++) {
      const b = lines[j]
      if (b.startsWith('@@') || b.startsWith('diff ') || isFileHeader(j)) {
        end = j
        break
      }
      if (b === '' && j === lines.length - 1) {
        end = j
        break
      }
      if (b.startsWith('+')) newCount++
      else if (b.startsWith('-')) oldCount++
      else if (!b.startsWith('\\')) { oldCount++; newCount++ }
    }
    return { oldCount, newCount, end }
  }
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    if (line.startsWith('--- ') && isFileHeader(i)) {
      oldLine = 1
      newLine = 1
      continue
    }
    // A `+++ ` file header with change lines but no `@@` before the next file:
    // synthesize one, so the content becomes a hunk instead of vanishing into a
    // zero-hunk "pure rename". Requires the preceding `--- ` partner, because a
    // hunk BODY line can legitimately read `+++ x` (an addition of a line
    // starting `++ `) and must not be mistaken for a file header.
    if (line.startsWith('+++ ') && isFileHeader(i)) {
      const { oldCount, newCount, end } = measure(i)
      const hasChange = lines
        .slice(i + 1, end)
        .some(b => (b.startsWith('+') || b.startsWith('-')) && !b.startsWith('+++ ') && !b.startsWith('--- '))
      if (hasChange) {
        lines.splice(i + 1, 0, `@@ -${oldLine},${oldCount} +${newLine},${newCount} @@`)
        hunkBody = markHunkBodies() // the splice shifted every later index
        oldLine += oldCount
        newLine += newCount
        changed = true
        i++ // skip the header just inserted; its body was already measured
      }
      continue
    }
    if (!line.startsWith('@@')) continue
    const valid = /^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/.exec(line)
    const { oldCount, newCount } = measure(i)
    // Declared starts are kept (they position the hunk); declared counts are
    // replaced, because a hand-written count that overshoots its body makes
    // Pierre truncate or reject the hunk.
    const oldStart = valid ? parseInt(valid[1], 10) : oldLine
    const newStart = valid ? parseInt(valid[3], 10) : newLine
    const section = valid
      ? line.slice(valid[0].length).trim()
      : line.replace(/^@@+/, '').replace(/@@\s*$/, '').trim()
    const rewritten = `@@ -${oldStart},${oldCount} +${newStart},${newCount} @@${section ? ` ${section}` : ''}`
    if (rewritten !== line) {
      lines[i] = rewritten
      changed = true
    }
    oldLine = oldStart + oldCount
    newLine = newStart + newCount
  }
  // Canonicalize hunk bodies: a context line MUST carry a leading space, but
  // hand-written diffs routinely omit it (and elision markers like `...` never
  // have one). Pierre's line parser rejects such a line outright — logging
  // `parseLineType: Invalid firstChar` and dropping it — so pad them here.
  // Classification is unchanged (they already counted as context above), so the
  // headers written in the loop stay correct.
  let inHunk = false
  for (let i = 0; i < lines.length; i++) {
    const line = lines[i]
    if (line.startsWith('@@')) {
      inHunk = true
      continue
    }
    if (line.startsWith('diff ') || line.startsWith('index ') || isFileHeader(i)) {
      inHunk = false
      continue
    }
    if (!inHunk) continue
    // Trailing newline artifact, not a body line.
    if (line === '' && i === lines.length - 1) continue
    if (line.startsWith('+') || line.startsWith('-') || line.startsWith(' ') || line.startsWith('\\')) continue
    lines[i] = ` ${line}`
    changed = true
  }
  return changed ? lines.join('\n') : patch
}

/** One highlight worker pool for the whole tab, created on first Pierre
 *  surface and never torn down. Deliberately NOT `WorkerPoolContextProvider`:
 *  that provider terminates the shared singleton when the LAST provider
 *  unmounts, and chat surfaces live in virtualized lists where every instance
 *  can scroll out at once — remounting blocks would then queue highlights
 *  into a dead pool and paint nothing. */
const workerPool = typeof window === 'undefined' || typeof Worker === 'undefined'
  ? undefined
  : getOrCreateWorkerPoolSingleton({
      poolOptions: {
        poolSize: PIERRE_WORKER_POOL_SIZE,
        // worker-portable is the self-contained bundle: the plain worker.js
        // entry carries bare package imports, which resolve when Rollup
        // bundles the worker for production but NOT when the vite dev server
        // serves it — the worker then errors at load and every surface waits
        // on a pool that never initializes.
        workerFactory: () =>
          new Worker(new URL('@pierre/diffs/worker/worker-portable.js', import.meta.url), {
            type: 'module',
          }),
      },
      highlighterOptions: { theme: PIERRE_THEMES },
    })

// Report highlight churn once this module is live. Placed here rather than in
// `main.tsx` so the timer only ever runs in a realm that actually renders code:
// this module is reached through the `React.lazy` boundaries in `./index`, so a
// tab that never shows a diff never starts it.
startPierrePerfReporting()

/** Hands every descendant the shared pool. Exported because the editor surface
 *  lives in a sibling module and needs the same one — without it Pierre falls
 *  back to `workerManager === undefined` and tokenizes on the main thread. */
export function PierreShell({ children }: { children: React.ReactNode }) {
  return <WorkerPoolContext.Provider value={workerPool}>{children}</WorkerPoolContext.Provider>
}

export function PierreCodeImpl({ file, options, className, langHint, scrollClassName }: {
  file: FileContents
  options?: BaseCodeOptions
  className?: string
  /** Markdown fence tag; resolved to a highlightable language (falling back
   *  to plain text) when the file has no explicit `lang`. */
  langHint?: string
  /** Hands Pierre ownership of the scroll container, which is what switches it
   *  from a row per source line to a windowed range. The classes belong to the
   *  element Pierre scrolls, so the caller must NOT also scroll its own box —
   *  the virtualizer listens on this element and uses it as its
   *  IntersectionObserver root. Omit it for snippet surfaces, which are short
   *  and already inside someone else's scroller. */
  scrollClassName?: string
}) {
  const dark = useIsDark()
  // Instance identity for churn accounting: two independently mounted blocks —
  // even with identical fence names — must never share an identity, while this
  // one instance re-rendering with streamed content must keep its own.
  const surfaceId = useId()
  const resolved = useMemo(
    () => pierreFileOptions({ themeType: pierreThemeType(dark), ...options }),
    [dark, options],
  )
  const resolvedFile = useMemo(() => {
    const withLang = file.lang || !langHint ? file : { ...file, lang: fenceLanguage(langHint) }
    return withLang.cacheKey
      ? withLang
      : { ...withLang, cacheKey: contentCacheKey(withLang.name, withLang.contents, surfaceId + ':file') }
  }, [file, langHint, surfaceId])
  const code = <File className={className} file={resolvedFile} options={resolved} />
  return (
    <PierreShell>
      {scrollClassName
        ? <Virtualizer config={PIERRE_VIRTUALIZER_CONFIG} className={scrollClassName}>{code}</Virtualizer>
        : code}
    </PierreShell>
  )
}

export function PierrePatchImpl({ patch, options, className, renderHeaderMetadata }: {
  patch: string
  options?: PierreDiffOptions
  className?: string
  renderHeaderMetadata?: () => React.ReactNode
}) {
  const dark = useIsDark()
  const surfaceId = useId()
  const resolved = useMemo(
    () => pierreDiffOptions({ themeType: pierreThemeType(dark), ...options }),
    [dark, options],
  )
  // Parse here rather than using <PatchDiff>: that component ASSERTS exactly
  // one complete file diff and throws otherwise, but chat patches stream
  // through partial frames (bare headers, unterminated hunks) and may carry
  // several files. Unparseable-yet text renders as plain monospace until a
  // later frame parses; a parser throw is treated the same way.
  const files = useMemo(() => {
    try {
      const parsed = parsePatchFiles(normalizePatchHunks(patch)).flatMap(p => p.files)
      for (const f of parsed) {
        // Strip git's a/ b/ prefixes: Pierre keeps them verbatim, so every
        // file would render as a rename (a/x → b/x) in the file header.
        if (f.name?.startsWith('b/') && f.prevName?.startsWith('a/')) {
          f.name = f.name.slice(2)
          const prev = f.prevName.slice(2)
          f.prevName = prev === f.name ? undefined : prev
        }
        f.cacheKey = contentCacheKey(f.name ?? '', patch, surfaceId + ':patch')
      }
      return parsed
    } catch {
      return []
    }
  }, [patch, surfaceId])
  // Zero files is an outright parse failure. Zero HUNKS across every file is
  // the subtler one: Pierre reads that as a pure rename and draws a header with
  // `+0 −0` and no rows — so when the raw text plainly carries changes, treat
  // it as a failure too rather than showing an empty rename of a file nobody
  // renamed. normalizePatchHunks should prevent this; the guard is what keeps a
  // future unparseable shape readable instead of blank.
  const noHunks = files.length > 0 && files.every(f => (f.hunks?.length ?? 0) === 0)
  const looksLikeChanges = /^[+-](?![+-][+-] )/m.test(patch)
  if (files.length === 0 || (noHunks && looksLikeChanges)) return <PlainCodeFallback text={patch} />
  return (
    <PierreShell>
      {files.map((fileDiff, i) => (
        <FileDiff
          key={`${fileDiff.name ?? ''}:${i}`}
          className={className}
          fileDiff={fileDiff}
          options={resolved}
          renderHeaderMetadata={i === 0 && renderHeaderMetadata ? renderHeaderMetadata : undefined}
        />
      ))}
    </PierreShell>
  )
}

export function PierreFilePairImpl({ oldFile, newFile, options, className, renderHeaderMetadata, renderHeaderPrefix, renderHeaderFilenameSuffix }: {
  oldFile: FileContents | null
  newFile: FileContents | null
  options?: PierreDiffOptions
  className?: string
  /** Injected into the file header's metadata slot (light DOM, so outer-tree
   *  styling and hover reveals apply). Rendered in the collapsed state too —
   *  a collapsed diff is header-only, which is what makes it a usable row. */
  renderHeaderMetadata?: () => React.ReactNode
  /** Injected at the START of the header content, before the change icon and
   *  filename — the slot for an expand/collapse affordance. */
  renderHeaderPrefix?: () => React.ReactNode
  /** Injected directly AFTER the filename, inside the header's content row. */
  renderHeaderFilenameSuffix?: () => React.ReactNode
}) {
  const dark = useIsDark()
  const surfaceId = useId()
  const resolved = useMemo(
    () => pierreDiffOptions({ themeType: pierreThemeType(dark), ...options }),
    [dark, options],
  )
  // MultiFileDiff requires at least one populated side; both-null cannot
  // happen from our call sites (DiffPanel banners the identical case away and
  // new/deleted files carry one side), but the type demands the narrowing.
  const keyedOld = useMemo(
    () => (oldFile ? { ...oldFile, cacheKey: contentCacheKey(oldFile.name, oldFile.contents, surfaceId + ':diff-old') } : null),
    [oldFile, surfaceId],
  )
  const keyedNew = useMemo(
    () => (newFile ? { ...newFile, cacheKey: contentCacheKey(newFile.name, newFile.contents, surfaceId + ':diff-new') } : null),
    [newFile, surfaceId],
  )
  if (!keyedOld && !keyedNew) return null
  const input = (keyedOld && keyedNew
    ? { oldFile: keyedOld, newFile: keyedNew }
    : keyedOld
      ? { oldFile: keyedOld, newFile: null }
      : { oldFile: null, newFile: keyedNew as FileContents })
  return (
    <PierreShell>
      <MultiFileDiff className={className} {...input} options={resolved} renderHeaderMetadata={renderHeaderMetadata} renderHeaderPrefix={renderHeaderPrefix} renderHeaderFilenameSuffix={renderHeaderFilenameSuffix} />
    </PierreShell>
  )
}
