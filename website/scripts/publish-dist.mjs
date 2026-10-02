/**
 * Atomic publication of every Vite build: nothing ever empties the live outDir.
 *
 * `atomicPublishPlugin` (registered LAST in vite.config.ts) redirects the build
 * to a per-build scratch sibling of the configured outDir,
 * `.<outDir>.next-<pid>-<id>`, and publishes it in the last closeBundle hook,
 * only when the build wrote its bundle -- so every other closeBundle hook (the
 * sw.js stamp, precompression) has finished, and a failed build never touches
 * the live tree.
 *
 * Publishing renames the live tree aside and the new one over it. A reader of
 * the live tree -- a gateway serving `website/dist` through the dev link, or the
 * stale-asset watchdog -- sees the old complete bundle or the new complete one.
 * The one gap is between the two renames: the in-gap rename gets
 * {@link IN_GAP_BUDGET_MS}, and if it misses, the old tree is renamed back.
 *
 * This is the one place the mechanism is explained; the Python stager and the
 * specs point here. `vite build --watch` and a live outDir that is a mount point
 * keep Vite's own in-place output.
 */

import { cpSync, lstatSync, mkdirSync, readFileSync, readdirSync, renameSync, rmSync, statSync } from 'node:fs'
import { randomBytes } from 'node:crypto'
import { performance } from 'node:perf_hooks'
import path from 'node:path'

/**
 * Total time to keep retrying a refused rename on Windows. Windows refuses a
 * rename while any handle is open in the tree, and antivirus and the search
 * indexer scan a freshly written tree exactly then; npm's own graceful-fs
 * retries such renames for up to 60 s for the same reason.
 */
const RENAME_RETRY_BUDGET_MS = 60_000

/**
 * Budget for the rename made while the live tree is absent. Under the
 * stale-asset watchdog's 2 s re-check (`_CONFIRM_DELAY_SECS`, pinned by
 * test_frontend_dist_resolve.py), so a swap that lands is never read as a
 * vanished install.
 */
export const IN_GAP_BUDGET_MS = 1_500

const FIRST_RETRY_DELAY_MS = 50
const MAX_RETRY_DELAY_MS = 2_000

/** The error codes Windows uses for "another handle holds this tree". */
const RETRYABLE_CODES = new Set(['EPERM', 'EACCES', 'EBUSY'])

/**
 * The POSIX codes for "this directory cannot be renamed at all": a mount point
 * the st_dev test cannot see (a bind mount from the same filesystem) or a move
 * across filesystems. Such a live tree is rewritten in place instead.
 */
const IN_PLACE_CODES = new Set(['EBUSY', 'EXDEV'])

/**
 * The completeness rule, shared byte-for-byte with frontend._ASSET_REF in
 * src/kiro_crew/frontend.py (test_frontend_dist_resolve.py pins the two equal):
 * every `/assets/*.js|css` index.html references, ignoring a query or hash.
 */
export const ASSET_REF_PATTERN = String.raw`(?:src|href)="(/assets/[^"?#]+\.(?:js|css))(?:[?#][^"]*)?"`

function sleepSync(ms) {
  if (!Number.isFinite(ms) || ms <= 0) throw new RangeError(`sleep needs a positive delay, got ${ms}`)
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, ms)
}

/**
 * `renameSync(from, to)`, retried on Windows while a handle holds the tree.
 *
 * POSIX renames a directory with open files in it, so there a refusal is real
 * and is thrown at once. The budget runs on a monotonic clock, so a wall-clock
 * step cannot stretch it.
 */
export function renameWithRetry(
  from,
  to,
  {
    rename = renameSync,
    sleep = sleepSync,
    isWindows = process.platform === 'win32',
    budgetMs = RENAME_RETRY_BUDGET_MS,
    now = () => performance.now(),
  } = {},
) {
  const deadline = now() + budgetMs
  let delay = FIRST_RETRY_DELAY_MS
  for (;;) {
    try {
      rename(from, to)
      return
    } catch (err) {
      if (!isWindows || !RETRYABLE_CODES.has(err?.code) || now() + delay > deadline) throw err
      sleep(delay)
      delay = Math.min(delay * 2, MAX_RETRY_DELAY_MS)
    }
  }
}

/**
 * Why `dir` is not a complete build, or `''` if it is: index.html exists and
 * every asset it references is a file. Messages match
 * frontend._incomplete_bundle_reason.
 */
export function incompleteReason(dir) {
  let html
  try {
    html = readFileSync(path.join(dir, 'index.html'), 'utf-8')
  } catch (err) {
    return err?.code === 'ENOENT' ? 'no index.html' : `index.html is unreadable (${err?.code ?? err})`
  }
  const isFile = rel => {
    try {
      return statSync(path.join(dir, rel)).isFile()
    } catch {
      return false
    }
  }
  const refs = [...html.matchAll(new RegExp(ASSET_REF_PATTERN, 'g'))].map(m => m[1])
  const missing = refs.filter(ref => !isFile(ref.replace(/^\//, '')))
  if (missing.length) return `${missing.length} referenced asset(s) missing, e.g. ${missing[0]}`
  return ''
}

function lexists(p) {
  try {
    lstatSync(p)
    return true
  } catch {
    return false
  }
}

/** Whether `p` is a mount point. A link is not one: it is renamed like a file. */
export function isMountPoint(p) {
  try {
    const st = lstatSync(p)
    return !st.isSymbolicLink() && st.dev !== statSync(path.dirname(p)).dev
  } catch {
    return false
  }
}

function removeBestEffort(p, warn) {
  try {
    rmSync(p, { recursive: true, force: true, maxRetries: 10, retryDelay: 100 })
  } catch (err) {
    warn(`publish-dist: could not remove ${p} (${err?.code ?? err}); remove it by hand`)
  }
}

/** Remove what `live` holds and `next` does not, depth first. */
function removeExtras(live, next) {
  for (const entry of readdirSync(live, { withFileTypes: true })) {
    const there = path.join(live, entry.name)
    const here = path.join(next, entry.name)
    if (!lexists(here)) rmSync(there, { recursive: true, force: true })
    else if (entry.isDirectory() && statSync(here).isDirectory()) removeExtras(there, here)
  }
}

/**
 * Stage the new build inside a live tree that cannot be renamed, so a copy
 * failure leaves every live file untouched. Merge by per-file rename with the
 * top-level index.html last, then remove extras and the staging tree.
 */
function rewriteInPlace(next, live, warn) {
  const staging = path.join(live, `.publish-${process.pid}-${randomBytes(4).toString('hex')}`)
  const merge = (from, to) => {
    mkdirSync(to, { recursive: true })
    for (const entry of readdirSync(from, { withFileTypes: true })) {
      if (from === staging && entry.name === 'index.html') continue
      const source = path.join(from, entry.name)
      const destination = path.join(to, entry.name)
      if (entry.isDirectory()) merge(source, destination)
      else renameSync(source, destination)
    }
  }
  try {
    cpSync(next, staging, { recursive: true, force: true })
    merge(staging, live)
    renameSync(path.join(staging, 'index.html'), path.join(live, 'index.html'))
    removeExtras(live, next)
  } finally {
    removeBestEffort(staging, warn)
  }
}

/**
 * Swap the complete build at `next` in as `live`. On any failure throws with
 * `live` as it was, and the new build left in place for inspection.
 */
export function publishDist({ next, live, warn = console.warn, ...renameOptions }) {
  const reason = incompleteReason(next)
  if (reason) throw new Error(`${next} is not a complete build (${reason}); ${live} was left as it was`)
  const isWindows = renameOptions.isWindows ?? process.platform === 'win32'
  const id = `${process.pid}-${randomBytes(4).toString('hex')}`
  const base = path.basename(live)
  const parent = path.dirname(live)
  const ready = path.join(parent, `.${base}.ready-${id}`)
  const previous = path.join(parent, `.${base}.prev-${id}`)
  // Every contended wait happens while the live tree still serves: moving the
  // new tree once proves nothing holds it, and moving the live tree aside is
  // atomic, so a refusal there changes nothing.
  renameWithRetry(next, ready, renameOptions)
  const hadLive = lexists(live) // lstat: a dangling link is still moved aside
  if (hadLive) {
    try {
      renameWithRetry(live, previous, renameOptions)
    } catch (err) {
      if (!isWindows && IN_PLACE_CODES.has(err?.code)) {
        warn(`publish-dist: ${live} cannot be renamed (${err.code}), so it is rewritten in place`)
        rewriteInPlace(ready, live, warn)
        removeBestEffort(ready, warn)
        return
      }
      throw new Error(`could not move ${live} aside (${err?.code ?? err.message}): ${holdHint()} ${live} was left as it was; the new build is at ${ready}.`)
    }
  }
  try {
    renameWithRetry(ready, live, { ...renameOptions, budgetMs: IN_GAP_BUDGET_MS })
  } catch (err) {
    // The old tree goes back with the full budget: giving up sooner would not
    // shorten the gap, only leave the live tree absent until someone notices.
    let kept = `there was no previous build; the new build is at ${ready}`
    if (hadLive) {
      try {
        renameWithRetry(previous, live, renameOptions)
        kept = `${live} was left as it was; the new build is at ${ready}`
      } catch {
        kept = `the previous build is at ${previous} and the new one at ${ready}`
      }
    }
    throw new Error(`could not publish ${ready} as ${live} (${err?.code ?? err.message}): ${holdHint()} ${kept}.`)
  }
  if (hadLive) removeBestEffort(previous, warn)
}

function holdHint() {
  return process.platform === 'win32'
    ? 'a process holds a handle in the tree -- usually antivirus or the search indexer scanning new files.'
    : 'the rename was refused.'
}

function pidAlive(pid) {
  try {
    process.kill(pid, 0)
    return true
  } catch (err) {
    return err?.code === 'EPERM'
  }
}

/** Scratch, ready and previous trees left by builds whose process is gone. */
function sweepAbandoned(live, warn) {
  const base = path.basename(live)
  const parent = path.dirname(live)
  const pattern = new RegExp(`^\\.${base.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\.(?:next|ready|prev)-(\\d+)-`)
  let names = []
  try {
    names = readdirSync(parent)
  } catch {
    return
  }
  for (const name of names) {
    const pid = Number(name.match(pattern)?.[1])
    if (pid && pid !== process.pid && !pidAlive(pid)) removeBestEffort(path.join(parent, name), warn)
  }
}

/**
 * Vite plugin: build into a scratch sibling of the configured outDir and
 * publish it atomically once the build has written it. Build-only; `--watch`
 * builds and a mount-point outDir keep Vite's own in-place output.
 *
 * Success is one flag, set by a `writeBundle` that runs `order: 'post'` from
 * the last plugin, so it runs after every other plugin's `writeBundle`: any
 * failure before it -- a transform, a render, an earlier `writeBundle` -- leaves
 * the flag unset, and nothing is published. Such a build has already failed, so
 * an unset flag is reported, not thrown: a throw here would replace the build's
 * own error in Vite's output.
 */
export function atomicPublishPlugin({ publish = publishDist, warn = console.warn } = {}) {
  let live = null
  let next = null
  let written = false
  return {
    name: 'kirocrew-atomic-publish',
    apply: 'build',
    // After every user plugin: its config() redirect is the one that lands, and
    // its post-order writeBundle runs after theirs.
    enforce: 'post',
    config(config) {
      next = null
      written = false
      if (config.build?.watch) return undefined
      const root = path.resolve(config.root ?? process.cwd())
      live = path.resolve(root, config.build?.outDir ?? 'dist')
      // At the start, not after a publish: a build whose closeBundle chain
      // stopped early never reaches its own cleanup.
      sweepAbandoned(live, warn)
      if (isMountPoint(live)) {
        warn(`publish-dist: ${live} is a mount point, so the build rewrites it in place`)
        return undefined
      }
      next = path.join(path.dirname(live), `.${path.basename(live)}.next-${process.pid}-${randomBytes(4).toString('hex')}`)
      return { build: { outDir: next, emptyOutDir: true } }
    },
    writeBundle: {
      order: 'post',
      sequential: true,
      handler() {
        written = true
      },
    },
    closeBundle: {
      order: 'post',
      sequential: true,
      handler() {
        const scratch = next
        if (!scratch) return
        next = null
        if (!written) {
          removeBestEffort(scratch, warn)
          warn(`publish-dist: the build did not finish, so ${live} was left as it was`)
          return
        }
        publish({ next: scratch, live, warn })
        console.log(`publish-dist: published ${live}`)
      },
    },
  }
}
