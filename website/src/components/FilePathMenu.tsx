/**
 * Shared file-path context menu: Open in default app, Reveal in Finder / file
 * manager, Copy path. Exposed as a right-click wrapper (`FilePathMenu`); the
 * item rows themselves are a private building block (`FilePathMenuItems`).
 *
 * The two OTHER file-location surfaces (MarkdownPanel's overflow and
 * FileViewer's overflow) are custom plain-button dropdowns, not Radix
 * ContextMenus, so they cannot host Radix `ContextMenuItem`s. They dedupe by
 * reusing the shared `revealOrOpen` failure path and `useRevealLabel` platform
 * label instead of the row component — so `FilePathMenuItems` has no consumer
 * beyond this file and is deliberately NOT exported (a zero-consumer export is
 * dead surface area).
 *
 * Open/Reveal items render only when `directLocal` is true (the backend reports
 * the request comes from a browser on the same machine). Remote and tunneled
 * sessions see Copy path only, because opening Finder on a host the user is not
 * looking at is useless.
 *
 * The reveal label is platform-aware — it reuses the same gatewayPlatform-driven
 * wording MarkdownPanel's overflow menu uses, so the two menus name the identical
 * action identically ("Open in Finder" on macOS, "Open in File Explorer" on
 * Windows, "Show in file manager" otherwise) instead of drifting apart.
 *
 * "Open with default app" is hidden for directories: `/api/reveal` rejects an
 * `open` action on a directory (400), so offering it would be a guaranteed-fail
 * click. Reveal still applies — it shows the folder in the file manager.
 *
 * It is also hidden on a Windows gateway: files.py refuses the launch-by-
 * association verb there (platform_compat.open_with_default_app answers False,
 * so the backend degrades an `open` to a clipboard copy), which would make the
 * row promise a launch it can never perform. Reveal still works on Windows.
 */
import { type ReactNode, useCallback, useEffect, useRef, useState } from 'react'
import { ExternalLink, FolderOpen, Copy, Check, AlertCircle } from 'lucide-react'
import {
  ContextMenu,
  ContextMenuTrigger,
  ContextMenuContent,
  ContextMenuItem,
} from './ui/context-menu'
import ErrorNotice from './ErrorNotice'
import { useBranding } from '../hooks/useBranding'
import { useGatewayPlatform } from '../hooks/useGatewayPlatform'
import { api, ApiError } from '../api/client'
import { copyToClipboard } from '../utils/clipboard'
import { i18nT } from '../i18n/t'

/** What the wrapped path is on disk. Directories cannot be "opened".
 *  Local to this module: passed inline as a union by callers, never imported. */
type FilePathKind = 'file' | 'dir'

/**
 * Perform a reveal/open and, on failure, show the SHARED i18n failure message.
 *
 * Exported so every file-location surface (this menu, MarkdownPanel's overflow,
 * FileViewer's overflow) funnels its reveal through one failure path instead of
 * each re-deriving its own `alert(err.message)` — a raw server string leaks
 * internal wording and drifts per surface.
 *
 * A `/api/reveal` denial for a sensitive path is a deliberate security decision
 * (files.py answers 403 for a path `is_sensitive_path` refuses), not a
 * malfunction. Flattening it into the generic "couldn't open" wording reads as a
 * bug and invites a retry, so a 403 that is NOT the auth-expiry 403
 * (`authRequired`, which has its own re-auth recovery) gets the blocked-by-policy
 * string instead. The branch keys off the status code carried on `ApiError`, not
 * the server's prose — the raw denial text never reaches the UI.
 *
 * Returns `{ copied, copyFailed }` so a caller that promised a reveal/open through a
 * "Show in file manager" affordance can acknowledge the silent degrade. On a
 * headless direct-local session the backend still degrades an open/reveal to a
 * clipboard copy, and without a signal the click looks like a dead no-op. The
 * copy itself is still performed HERE (so callers that ignore the return — the
 * chip's Shift+click, the overflow buttons — keep working unchanged and
 * `api.revealPath` stays side-effect-free); the flag only lets the menu flip its
 * existing "Path copied" swap. A failed reveal/open is non-routine and IS
 * surfaced regardless (below).
 *
 * `copyFailed` is separate from `!copied` because the two mean different things
 * to the caller: `copied: false, copyFailed: false` is the ordinary case where
 * the backend drove a real file manager and there was nothing to acknowledge,
 * while `copyFailed` is a degrade that then FAILED and must not be acknowledged
 * as a copy. `copyToClipboard` signals that by RETURNING false rather than
 * throwing (its `execCommand` fallback path — see utils/clipboard.ts), so the
 * boolean is the only evidence the clipboard actually changed; claiming a write
 * that did not happen is worse than saying nothing, because the user then pastes
 * whatever was there before.
 *
 * A failed reveal/open is reported through `onError` with the (already
 * localized) message, and the caller renders it in place through the shared
 * `ErrorNotice` (the `errors-use-error-notice` rule) — there is no blocking
 * `alert()` path any more. `useRevealFailure` below is the ready-made state for
 * a caller that has nothing else to hang it on.
 */
export async function revealOrOpen(
  filePath: string,
  action: 'open' | 'reveal',
  { onError }: { onError: (message: string) => void },
): Promise<{ copied: boolean; copyFailed: boolean }> {
  try {
    const r = await api.revealPath(filePath, action)
    if (r.copy) {
      const copied = await copyToClipboard(r.copy)
      return { copied, copyFailed: !copied }
    }
    return { copied: false, copyFailed: false }
  } catch (err) {
    // eslint-disable-next-line no-console -- surface reveal failures for diagnostics
    console.error('revealPath failed', err)
    const blockedByPolicy = err instanceof ApiError && err.status === 403 && !err.authRequired
    onError(i18nT(blockedByPolicy
      ? 'components.filePathMenu.reveal_blocked'
      : 'components.filePathMenu.reveal_failed'))
    return { copied: false, copyFailed: false }
  }
}

/**
 * The failure state a `revealOrOpen` caller renders: pass `onError` to the
 * helper, render `<ErrorNotice message={error} onDismiss={clear} …/>` where the
 * click happened. Shared so every reveal button (folder panels, file viewer,
 * deck viewer, transcript chips) reports the same way instead of each holding
 * its own copy of the state.
 */
export function useRevealFailure(subject?: string) {
  const [error, setError] = useState<string | null>(null)
  // A failure belongs to the path it was raised for: when the caller moves on
  // to another file / folder, a late rejection for the old one must not be
  // shown under the new one.
  useEffect(() => { setError(null) }, [subject])
  const onError = useCallback((message: string) => setError(message), [])
  const clear = useCallback(() => setError(null), [])
  return { error, onError, clear }
}

/**
 * The platform-aware reveal label ("Open in Finder" / "Open in File Explorer" /
 * "Show in file manager"), read from the GATEWAY's platform.
 *
 * The single owner of this wording: MarkdownPanel's overflow and FileViewer's
 * overflow both call this instead of re-deriving the same three-arm ternary, so
 * every file-location surface names the identical action identically. `/api/reveal`
 * shells out on the gateway, so the gateway's platform is the one to name.
 */
export function useRevealLabel(): string {
  const gatewayPlatform = useGatewayPlatform()
  return gatewayPlatform === 'darwin'
    ? i18nT('components.markdownPanel.open_in_finder')
    : gatewayPlatform === 'windows'
      ? i18nT('components.markdownPanel.open_in_file_explorer')
      : i18nT('components.markdownPanel.show_in_file_manager')
}

/**
 * The ONE gate that decides whether the "Open with default app" row is shown.
 *
 * Every file-location surface (this menu, MarkdownPanel's overflow, FileViewer's
 * overflow) reads it from here rather than re-deriving `directLocal && …`, so a
 * local Windows user does not get an Open row in one menu that another menu
 * deliberately hides. Open needs all three: the browser is on the gateway host
 * (`directLocal`), the target is a file not a directory (`/api/reveal` rejects an
 * `open` on a dir), and the gateway is not Windows (files.py degrades an `open`
 * to a clipboard copy there). Reveal has a laxer gate — `directLocal` alone — so
 * it is intentionally NOT folded in here.
 */
export function useCanOpenFile(kind?: FilePathKind): boolean {
  const { directLocal } = useBranding()
  const gatewayPlatform = useGatewayPlatform()
  return !!directLocal && kind !== 'dir' && gatewayPlatform !== 'windows'
}

// ── Menu-item building blocks ────────────────────────────────────────────────

interface FilePathMenuItemsProps {
  /** Absolute file path to act on. */
  filePath: string
  /** Whether the path is a file or a directory. The Open item is suppressed for
   *  directories, which the reveal endpoint cannot `open`. */
  kind?: FilePathKind
}

/**
 * The ONE owner of the inline "Path copied" acknowledgment for a path action.
 *
 * The app has no toast, so a copy — or an open/reveal the backend silently
 * DEGRADED to a copy on a headless session — is acknowledged by flipping a
 * label to `Path copied` / `Couldn't copy the path` for 1.5s and back. Both
 * consumers read it from here rather than re-deriving the timer, the attempt
 * guard and the wording: the file-path menu (whose Copy row and Open/Reveal
 * rows share one status) and the Office card's primary Open button, which
 * would otherwise read as a dead click on a headless direct-local host.
 *
 * `attemptRef` is what makes a late-settling write safe: it is bumped on
 * unmount and on every `filePath` change, so a resolution that arrives after
 * the component moved on cannot acknowledge a path that is no longer shown.
 */
export function useCopyAck(filePath: string) {
  const [copyStatus, setCopyStatus] = useState<'idle' | 'copied' | 'failed'>('idle')
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  const copyAttemptRef = useRef(0)
  useEffect(() => () => {
    copyAttemptRef.current += 1
    if (copyTimerRef.current) clearTimeout(copyTimerRef.current)
  }, [])
  useEffect(() => {
    copyAttemptRef.current += 1
    setCopyStatus('idle')
  }, [filePath])
  // Flip to `copied`/`failed`, then reset. Guarded by the attempt counter so a
  // stale resolution cannot acknowledge a path left behind.
  const flashCopyStatus = (attempt: number, next: 'copied' | 'failed') => {
    if (attempt !== copyAttemptRef.current) return
    if (copyTimerRef.current) clearTimeout(copyTimerRef.current)
    setCopyStatus(next)
    copyTimerRef.current = setTimeout(() => setCopyStatus('idle'), 1500)
  }
  const copyPath = async () => {
    const attempt = ++copyAttemptRef.current
    if (copyTimerRef.current) clearTimeout(copyTimerRef.current)
    let next: 'copied' | 'failed'
    try {
      // The boolean, not merely the absence of a throw: the `execCommand`
      // fallback reports failure by returning false.
      next = (await copyToClipboard(filePath)) ? 'copied' : 'failed'
    } catch {
      next = 'failed'
    }
    flashCopyStatus(attempt, next)
  }
  // Open/Reveal degrade to a clipboard copy on a remote or headless session
  // (files.py cannot drive a file manager there), so acknowledge that instead of
  // leaving a click that silently copied looking like a no-op — no blocking
  // alert, no new toast surface.
  //
  // A reveal/open that FAILED (policy-blocked path, backend error) is held here
  // too, so the consumer renders it in place through the shared `ErrorNotice`
  // rather than the legacy blocking `alert()`. Cleared when the path changes,
  // on the next attempt, and by the consumer's dismiss.
  const [revealError, setRevealError] = useState<string | null>(null)
  useEffect(() => { setRevealError(null) }, [filePath])
  const revealOrOpenWithAck = async (action: 'open' | 'reveal') => {
    const attempt = ++copyAttemptRef.current
    setRevealError(null)
    const { copied, copyFailed } = await revealOrOpen(filePath, action, {
      // Guarded like the copy ack: a stale failure must not land on a path
      // this component has already moved on from.
      onError: (message) => { if (attempt === copyAttemptRef.current) setRevealError(message) },
    })
    if (copied) flashCopyStatus(attempt, 'copied')
    // A degrade that could not reach the clipboard flips to the same
    // `copy_failed` wording, rather than leaving a click that did nothing
    // looking like one that worked.
    else if (copyFailed) flashCopyStatus(attempt, 'failed')
  }
  const clearRevealError = () => setRevealError(null)
  return { copyStatus, copyPath, revealOrOpenWithAck, revealError, clearRevealError }
}

/**
 * Renders the file-path action items (Open / Reveal / Copy path) as
 * ContextMenu items. Drop these into any ContextMenuContent.
 */
function FilePathMenuItems({ filePath, kind }: FilePathMenuItemsProps) {
  const isLocal = useBranding().directLocal
  // Shared owner of the platform-aware reveal label (see useRevealLabel) — the
  // same wording MarkdownPanel's overflow and FileViewer's overflow use.
  const revealLabel = useRevealLabel()
  const openLabel = i18nT('components.markdownPanel.open_with_default_app')
  // The one shared Open gate (see useCanOpenFile) — the same predicate the two
  // overflow menus consume, so a Windows/dir target hides Open identically.
  const canOpen = useCanOpenFile(kind)

  // Copy path's whole acknowledgment is the glyph flipping to a tick and back
  // (see useCopyAck, shared with the Office card). The Copy item keeps the menu
  // open on select so the confirmation is not taken off screen the instant it is
  // earned.
  const { copyStatus, copyPath, revealOrOpenWithAck, revealError, clearRevealError } = useCopyAck(filePath)
  const copyLabel = copyStatus === 'copied'
    ? i18nT('components.filePathMenu.path_copied')
    : copyStatus === 'failed'
      ? i18nT('components.filePathMenu.copy_failed')
      : i18nT('components.filePathMenu.copy_path')

  return (
    <>
      {/* A failed reveal/open renders IN the menu (which the rows keep open on
          select) instead of the old blocking alert(). askAgent on: a context
          menu holds no draft, and a policy-blocked or failed reveal is a
          gateway-side condition the agent can explain. */}
      {revealError && (
        <div className="px-2 py-1.5 max-w-[260px]">
          <ErrorNotice
            variant="inline"
            className="whitespace-normal"
            message={revealError}
            askAgent
            onDismiss={clearRevealError}
            testId="file-path-menu-error"
          />
        </div>
      )}
      {canOpen && (
        <ContextMenuItem
          // preventDefault keeps the menu open, matching the Copy row: on a
          // headless direct-local session an open degrades to a clipboard copy
          // and the row must stay on screen long enough to show the "Path
          // copied" acknowledgment revealOrOpenWithAck flips on.
          onSelect={(e) => { e.preventDefault(); void revealOrOpenWithAck('open') }}
          aria-label={openLabel}
        >
          <ExternalLink size={14} className="lucide-inline" />
          {openLabel}
        </ContextMenuItem>
      )}
      {isLocal && (
        <ContextMenuItem
          onSelect={(e) => { e.preventDefault(); void revealOrOpenWithAck('reveal') }}
          aria-label={revealLabel}
        >
          <FolderOpen size={14} className="lucide-inline" />
          {revealLabel}
        </ContextMenuItem>
      )}
      <ContextMenuItem
        onSelect={(e) => { e.preventDefault(); void copyPath() }}
        aria-label={copyLabel}
      >
        {copyStatus === 'copied'
          ? <Check size={14} className="lucide-inline text-ok" aria-hidden="true" />
          : copyStatus === 'failed'
            ? <AlertCircle size={14} className="lucide-inline text-danger" aria-hidden="true" />
            : <Copy size={14} className="lucide-inline" aria-hidden="true" />}
        {copyLabel}
      </ContextMenuItem>
    </>
  )
}

// ── Right-click wrapper ──────────────────────────────────────────────────────

export interface FilePathMenuProps {
  /** Absolute file path to act on. */
  filePath: string
  /** The element that triggers the context menu on right-click. */
  children: ReactNode
  /** File or directory — directories hide the Open item (see FilePathMenuItems). */
  kind?: FilePathKind
}

/**
 * Wrap any element to give it a right-click menu with file-path actions.
 *
 * ```tsx
 * <FilePathMenu filePath="/home/user/report.md">
 *   <span className="file-title">report.md</span>
 * </FilePathMenu>
 * ```
 */
export default function FilePathMenu({ filePath, children, kind }: FilePathMenuProps) {
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>
        {children}
      </ContextMenuTrigger>
      <ContextMenuContent className="min-w-[180px]" onClick={e => e.stopPropagation()}>
        <FilePathMenuItems
          filePath={filePath}
          kind={kind}
        />
      </ContextMenuContent>
    </ContextMenu>
  )
}
