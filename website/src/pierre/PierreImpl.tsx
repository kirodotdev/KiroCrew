/**
 * The one module that imports `@pierre/diffs` at runtime. Reached only through
 * the `React.lazy` boundaries in `./index`, so Pierre + Shiki stay out of the
 * eager bundle. Every surface renders through these three components, and all
 * of them resolve their options through `./config` — the single place the
 * look/behavior of code and diff rendering is decided.
 */
import { createPortal } from 'react-dom'
import { useCallback, useEffect, useId, useMemo, useRef, useSyncExternalStore } from 'react'
import type { BaseCodeOptions, FileContents, SupportedLanguages } from '@pierre/diffs'
import { EXTENSION_TO_FILE_FORMAT, parsePatchFiles, setCustomExtension } from '@pierre/diffs'
import { File, FileDiff, MultiFileDiff, Virtualizer, WorkerPoolContext } from '@pierre/diffs/react'
import { WorkerPoolManager, type WorkerRequest, type WorkerResponse } from '@pierre/diffs/worker'
import { useIsDark } from '../hooks/useIsDark'
import { usePlainDiff } from '../hooks/usePlainDiff'
import ErrorNotice from '../components/ErrorNotice'
import { i18nT } from '../i18n/t'
import { PlainCodeFallback, PlainFilePairFallback } from './PlainCodeFallback'
import {
  PIERRE_EXTENSION_OVERRIDES,
  PIERRE_REGEX_ENGINE,
  PIERRE_THEMES,
  PIERRE_VIRTUALIZER_CONFIG,
  PIERRE_WORKER_COOLDOWN_MS,
  PIERRE_WORKER_INITIALIZATION_TIMEOUT_MS,
  PIERRE_WORKER_POOL_SIZE,
  PIERRE_WORKER_REQUEST_TIMEOUT_MS,
  PIERRE_WORKER_RETRY_DELAYS_MS,
  PIERRE_WORKER_STABLE_AFTER_MS,
  pierreDiffOptions,
  pierreFileOptions,
  pierreThemeType,
  type PierreDiffOptions,
} from './config'
import { WorkerPoolLifecycle, type WorkerPoolSnapshot } from './workerPoolLifecycle'

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
export function contentCacheKey(name: string, contents: string, _surface = 'file'): string {
  let h = 5381
  for (let i = 0; i < contents.length; i++) h = ((h << 5) + h + contents.charCodeAt(i)) | 0
  const key = `${name}:${contents.length}:${(h >>> 0).toString(36)}`
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

/** Wrap one Pierre worker with request watchdogs. Pierre assigns at most one
 *  active request to a worker, but keying timers by request ID also makes late
 *  responses harmless and keeps the protocol contract explicit. */
export function createMonitoredWorker(reportFailure: (reason?: unknown) => void): Worker {
  const worker = new Worker(new URL('@pierre/diffs/worker/worker-portable.js', import.meta.url), {
    type: 'module',
  })
  const watchdogs = new Map<string, ReturnType<typeof setTimeout>>()
  const clearWatchdog = (id: string) => {
    const timer = watchdogs.get(id)
    if (timer !== undefined) clearTimeout(timer)
    watchdogs.delete(id)
  }
  const clearWatchdogs = () => {
    for (const timer of watchdogs.values()) clearTimeout(timer)
    watchdogs.clear()
  }

  worker.addEventListener('message', event => {
    const response = event.data as Partial<WorkerResponse>
    if (typeof response.id === 'string') clearWatchdog(response.id)
  })
  worker.addEventListener('error', event => {
    clearWatchdogs()
    reportFailure(event.message || event)
  })
  worker.addEventListener('messageerror', () => {
    clearWatchdogs()
    reportFailure('worker message could not be deserialized')
  })

  const postMessage = worker.postMessage.bind(worker)
  worker.postMessage = ((...args: Parameters<Worker['postMessage']>) => {
    const request = args[0] as Partial<WorkerRequest>
    // The watchdog keys on Pierre's string request IDs. If an upgrade stops
    // sending them the watchdog silently disarms; make that loud in dev.
    // eslint-disable-next-line no-console -- dev-only contract check, see pierre.workerProtocol.test.ts
    if (typeof request.id !== 'string' && import.meta.env.DEV) console.warn('Pierre worker request without a string id; the request watchdog cannot monitor it.', request)
    if (typeof request.id === 'string') {
      clearWatchdog(request.id)
      const timeoutMs = request.type === 'initialize'
        ? PIERRE_WORKER_INITIALIZATION_TIMEOUT_MS
        : PIERRE_WORKER_REQUEST_TIMEOUT_MS
      watchdogs.set(request.id, setTimeout(() => {
        watchdogs.delete(request.id as string)
        reportFailure(`worker request ${request.id} (${request.type ?? 'unknown'}) timed out`)
      }, timeoutMs))
    }
    try {
      Reflect.apply(postMessage, worker, args)
    } catch (error) {
      if (typeof request.id === 'string') clearWatchdog(request.id)
      reportFailure(error)
      throw error
    }
  }) as Worker['postMessage']

  const terminate = worker.terminate.bind(worker)
  worker.terminate = () => {
    clearWatchdogs()
    terminate()
  }

  return worker
}


/** Snapshot for consumers that never requested a pool (plain-diff mode) and
 *  for environments without a `Worker` API: Pierre renders on the main thread,
 *  as it always did before worker recovery existed. */
const unsupportedWorkerPoolSnapshot: WorkerPoolSnapshot = Object.freeze({
  phase: 'unsupported',
  generation: 0,
})
const subscribeUnsupportedWorkerPool = () => () => {}
const getUnsupportedWorkerPoolSnapshot = () => unsupportedWorkerPoolSnapshot

/** The generations a surface may hand to Pierre: a starting or ready pool, or
 *  `null` for main-thread rendering when this environment has no workers.
 *  `undefined` means "render app-owned plain text instead". */
export function activeWorkerPool(state: WorkerPoolSnapshot): WorkerPoolManager | null | undefined {
  if (state.phase === 'ready' || state.phase === 'starting') return state.pool
  if (state.phase === 'unsupported') return null
  return undefined
}

const passiveNoticeClaimants = new Set<symbol>()
const passiveNoticeListeners = new Set<() => void>()
let passiveNoticeOwner: symbol | undefined
/** The terminal phase never exits, so the one notice is dismissible and the
 *  dismissal is tab-wide: no surface re-raises it until reload. */
let passiveNoticeDismissed = false

function dismissPassiveNotice() {
  passiveNoticeDismissed = true
  for (const notify of [...passiveNoticeListeners]) notify()
}

/** Mounted editor surfaces in this tab. While one exists the passive notice
 *  stays silent: telling a reader on a read-only surface to reload would risk
 *  a draft they cannot see, and the editor already shows its own save-first
 *  notice where the draft lives. The global notice returns once every editor
 *  has unmounted. */
const editorSurfaceIds = new Set<symbol>()
const editorSurfaceListeners = new Set<() => void>()

function subscribeEditorSurfaces(listener: () => void): () => void {
  editorSurfaceListeners.add(listener)
  return () => { editorSurfaceListeners.delete(listener) }
}

export function useRegisterEditorSurface(): void {
  const id = useRef(Symbol('pierre-editor-surface')).current
  useEffect(() => {
    editorSurfaceIds.add(id)
    for (const notify of [...editorSurfaceListeners]) notify()
    return () => {
      editorSurfaceIds.delete(id)
      for (const notify of [...editorSurfaceListeners]) notify()
    }
  }, [id])
}

function subscribePassiveNotice(id: symbol, listener: () => void): () => void {
  passiveNoticeClaimants.add(id)
  if (passiveNoticeOwner === undefined) passiveNoticeOwner = id
  passiveNoticeListeners.add(listener)
  for (const notify of [...passiveNoticeListeners]) notify()
  return () => {
    passiveNoticeClaimants.delete(id)
    passiveNoticeListeners.delete(listener)
    if (passiveNoticeOwner === id) passiveNoticeOwner = passiveNoticeClaimants.values().next().value
    for (const notify of [...passiveNoticeListeners]) notify()
  }
}

function PierreWorkerUnavailableNotice() {
  const id = useRef(Symbol('pierre-worker-unavailable-notice')).current
  const subscribe = useCallback(
    (listener: () => void) => subscribePassiveNotice(id, listener),
    [id],
  )
  const ownsNotice = useSyncExternalStore(
    subscribe,
    () => passiveNoticeOwner === id && !passiveNoticeDismissed,
    () => false,
  )
  const editorMounted = useSyncExternalStore(
    subscribeEditorSurfaces,
    () => editorSurfaceIds.size > 0,
    () => false,
  )
  if (!ownsNotice || editorMounted || typeof document === 'undefined') return null
  return createPortal(
    <ErrorNotice
      askAgent
      onDismiss={dismissPassiveNotice}
      className="fixed bottom-safe-offset-4 right-safe-offset-4 z-[100] w-[min(32rem,calc(100vw-2rem))] shadow-lg"
      message={i18nT('components.pierreEditorImpl.highlighting_unavailable_content_readable_reload')}
    />,
    document.body,
  )
}

/** One demand-created, replaceable pool for the whole tab. A failure first
 *  unmounts every imperative Pierre renderer into app-owned plain text, then
 *  terminates all workers and pending requests before constructing the next
 *  generation. Keeping construction behind the first consumer avoids charging
 *  chunk preloads and raw patch plain-mode surfaces for worker startup. */
let workerPoolLifecycle: WorkerPoolLifecycle | undefined

function getWorkerPoolLifecycle(): WorkerPoolLifecycle {
  if (workerPoolLifecycle !== undefined) return workerPoolLifecycle
  workerPoolLifecycle = new WorkerPoolLifecycle({
  create: reportFailure => {
    const pool = new WorkerPoolManager(
      {
        poolSize: PIERRE_WORKER_POOL_SIZE,
        workerFactory: () => createMonitoredWorker(reportFailure),
      },
      { theme: PIERRE_THEMES, preferredHighlighter: PIERRE_REGEX_ENGINE },
    )
    return {
      pool,
      ready: pool.initialize(),
      terminate: () => pool.terminate(),
    }
  },
  retryDelaysMs: PIERRE_WORKER_RETRY_DELAYS_MS,
  cooldownMs: PIERRE_WORKER_COOLDOWN_MS,
  stableAfterMs: PIERRE_WORKER_STABLE_AFTER_MS,
  warn: reason => {
    // eslint-disable-next-line no-console -- plain-text recovery is user-visible; retain the root cause for diagnosis
    console.warn('Pierre highlight worker failed; showing plain text while the worker pool recovers.', reason)
  },
  })
  workerPoolLifecycle.start()
  return workerPoolLifecycle
}

/** `Worker`-less environments never construct a lifecycle: there is no pool to
 *  recover, so they read the frozen `unsupported` snapshot and render Pierre on
 *  the main thread as they always did. */
const workersSupported = typeof window !== 'undefined' && typeof Worker !== 'undefined'

export function usePierreWorkerPool(enabled = true): WorkerPoolSnapshot {
  const lifecycle = enabled && workersSupported ? getWorkerPoolLifecycle() : undefined
  return useSyncExternalStore(
    lifecycle?.subscribe ?? subscribeUnsupportedWorkerPool,
    lifecycle?.getSnapshot ?? getUnsupportedWorkerPoolSnapshot,
    lifecycle?.getSnapshot ?? getUnsupportedWorkerPoolSnapshot,
  )
}

/** Hands descendants the current ready generation. The key is required because
 *  Pierre captures the manager when its imperative renderer is constructed;
 *  changing context alone does not rebind an already-mounted instance. A `null`
 *  pool is the `Worker`-less environment: no manager in context, so Pierre
 *  highlights on the main thread exactly as it did before recovery existed. */
export function PierreShell({ pool, generation, children }: {
  pool: WorkerPoolManager | null
  generation: number
  children: React.ReactNode
}) {
  return <WorkerPoolContext.Provider key={generation} value={pool ?? undefined}>{children}</WorkerPoolContext.Provider>
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
  const poolState = usePierreWorkerPool()
  const activePool = activeWorkerPool(poolState)
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
  if (activePool === undefined) {
    const fallback = <>
      <PlainCodeFallback text={resolvedFile.contents} />
      {poolState.phase === 'unavailable' ? <PierreWorkerUnavailableNotice /> : null}
    </>
    return scrollClassName ? <div className={scrollClassName}>{fallback}</div> : fallback
  }
  const code = <File className={className} file={resolvedFile} options={resolved} />
  return (
    <PierreShell pool={activePool} generation={poolState.generation}>
      {scrollClassName
        ? <Virtualizer config={PIERRE_VIRTUALIZER_CONFIG} className={scrollClassName}>{code}</Virtualizer>
        : code}
    </PierreShell>
  )
}

export function PierrePatchImpl({ patch, options, className, renderHeaderMetadata, renderHeaderPrefix, renderHeaderFilenameSuffix }: {
  patch: string
  options?: PierreDiffOptions
  className?: string
  renderHeaderMetadata?: () => React.ReactNode
  renderHeaderPrefix?: () => React.ReactNode
  renderHeaderFilenameSuffix?: () => React.ReactNode
}) {
  const dark = useIsDark()
  const surfaceId = useId()
  const resolved = useMemo(
    () => pierreDiffOptions({ themeType: pierreThemeType(dark), ...options }),
    [dark, options],
  )
  const poolState = usePierreWorkerPool()
  const activePool = activeWorkerPool(poolState)
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
  if (files.length === 0 || (noHunks && looksLikeChanges)) {
    return <PlainCodeFallback text={patch} />
  }
  if (activePool === undefined) {
    // No header actions in the fallback (`max-two-buttons-per-row`); they
    // return with Pierre's own header when a generation is ready.
    return <>
      <PlainCodeFallback text={patch} />
      {poolState.phase === 'unavailable' ? <PierreWorkerUnavailableNotice /> : null}
    </>
  }
  return (
    <PierreShell pool={activePool} generation={poolState.generation}>
      {files.map((fileDiff, i) => (
        <FileDiff
          key={`${fileDiff.name ?? ''}:${i}`}
          className={className}
          fileDiff={fileDiff}
          options={resolved}
          renderHeaderMetadata={i === 0 && renderHeaderMetadata ? renderHeaderMetadata : undefined}
          renderHeaderPrefix={i === 0 ? renderHeaderPrefix : undefined}
          renderHeaderFilenameSuffix={i === 0 ? renderHeaderFilenameSuffix : undefined}
        />
      ))}
    </PierreShell>
  )
}

export function PierreFilePairImpl({ oldFile, newFile, options, className, fallbackClassName, fallbackContentStyle, renderHeaderMetadata, renderHeaderPrefix, renderHeaderFilenameSuffix }: {
  oldFile: FileContents | null
  newFile: FileContents | null
  options?: PierreDiffOptions
  className?: string
  fallbackClassName?: string
  fallbackContentStyle?: React.CSSProperties
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
  const [plain] = usePlainDiff()
  const plainLang: SupportedLanguages | undefined = plain ? 'text' : undefined
  const poolState = usePierreWorkerPool(!plain)
  const activePool = activeWorkerPool(poolState)
  // MultiFileDiff requires at least one populated side; both-null cannot
  // happen from our call sites (DiffPanel banners the identical case away and
  // new/deleted files carry one side), but the type demands the narrowing.
  //
  // The cacheKey carries the mode: Pierre caches tokens by that key, so without
  // the suffix a live toggle (`usePersistedBool` re-renders every surface in the
  // tab) would serve the coloured render's cached tokens to the plain one and
  // vice versa.
  const keyedOld = useMemo(
    () => (oldFile ? { ...oldFile, lang: plainLang ?? oldFile.lang, cacheKey: contentCacheKey(oldFile.name, oldFile.contents, surfaceId + ':diff-old') + (plain ? ':plain' : '') } : null),
    [oldFile, surfaceId, plain, plainLang],
  )
  const keyedNew = useMemo(
    () => (newFile ? { ...newFile, lang: plainLang ?? newFile.lang, cacheKey: contentCacheKey(newFile.name, newFile.contents, surfaceId + ':diff-new') + (plain ? ':plain' : '') } : null),
    [newFile, surfaceId, plain, plainLang],
  )
  if (!keyedOld && !keyedNew) return null
  const input = (keyedOld && keyedNew
    ? { oldFile: keyedOld, newFile: keyedNew }
    : keyedOld
      ? { oldFile: keyedOld, newFile: null }
      : { oldFile: null, newFile: keyedNew as FileContents })
  if (plain) {
    return (
      <MultiFileDiff
        className={className}
        {...input}
        options={resolved}
        renderHeaderMetadata={renderHeaderMetadata}
        renderHeaderPrefix={renderHeaderPrefix}
        renderHeaderFilenameSuffix={renderHeaderFilenameSuffix}
      />
    )
  }
  if (activePool === undefined) {
    return (
      <>
        <PlainFilePairFallback
          oldFile={keyedOld}
          newFile={keyedNew}
          options={resolved}
          geometry="pierre-swap"
          className={fallbackClassName}
          contentStyle={fallbackContentStyle}
          renderHeaderMetadata={renderHeaderMetadata}
          renderHeaderPrefix={renderHeaderPrefix}
          renderHeaderFilenameSuffix={renderHeaderFilenameSuffix}
        />
        {poolState.phase === 'unavailable' ? <PierreWorkerUnavailableNotice /> : null}
      </>
    )
  }
  return (
    <PierreShell pool={activePool} generation={poolState.generation}>
      <MultiFileDiff className={className} {...input} options={resolved} renderHeaderMetadata={renderHeaderMetadata} renderHeaderPrefix={renderHeaderPrefix} renderHeaderFilenameSuffix={renderHeaderFilenameSuffix} />
    </PierreShell>
  )
}
