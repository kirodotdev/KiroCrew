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
 * sessions do not get them, because opening Finder on a host the user is not
 * looking at is useless.
 *
 * The in-app items ("Open in side panel", "Show folder in side panel") render
 * whenever the host passes the matching handler, in every deployment, as their
 * own group above the native rows. They act on the viewer's own screen, so they
 * are the menu's one action that works whether or not the browser shares a
 * machine with the gateway, including an `ssh -L` tunnel that `directLocal`
 * cannot tell apart from a local browser. They need no locality gate and do not
 * change the native rows' gates.
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
import { type ReactNode, useCallback, useEffect, useId, useRef, useState } from 'react'
import { ExternalLink, FolderOpen, FolderTree, Copy, Check, AlertCircle, PenLine, PanelRight, Code2 } from 'lucide-react'
import {
  ContextMenu,
  ContextMenuTrigger,
  ContextMenuContent,
  ContextMenuItem,
  ContextMenuSeparator,
} from './ui/context-menu'
import ErrorNotice, { ErrorNoticeMenuItem } from './ErrorNotice'
import { useBranding } from '../hooks/useBranding'
import { useGatewayPlatform } from '../hooks/useGatewayPlatform'
import { api, ApiError } from '../api/client'
import { copyToClipboard } from '../utils/clipboard'
import { canOpenFileInEditor, isElectron, openFileInEditor } from '../lib/electron'
import { isEmbeddedPane } from '../lib/embedded'
import { parentDir } from '../apps/file-explorer/deepLink'
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

/**
 * The ONE owner of the desktop shell's "Open in editor" launch for a path.
 *
 * It hands the PATH to the shell's shell.openPath bridge so the file opens in
 * the user's own OS default handler — the one place the built-in viewer and the
 * gateway-host reveal cannot reach. `canOpenInEditor` is true only when the
 * fileOpenAPI preload bridge exists (desktop shell, not a browser tab or the
 * PWA) and the target is not a directory. A failed launch is held as
 * `editorError` for the caller to render in place through the shared
 * `ErrorNotice`; it is separate from a gateway reveal's error, since this launch
 * happens entirely in the shell and never touches /api/reveal.
 *
 * Shared by this menu's row and the artifacts list's session-document paths, so
 * both surfaces gate, launch and report a refusal identically.
 */
export function useOpenInEditor(filePath: string, kind?: FilePathKind) {
  const canOpenInEditor = kind !== 'dir' && canOpenFileInEditor()
  const [editorError, setEditorError] = useState<string | null>(null)
  useEffect(() => { setEditorError(null) }, [filePath])
  const openInEditor = async () => {
    setEditorError(null)
    const r = await openFileInEditor(filePath)
    // The bridge always names a reason on failure (bad path, unsupported type,
    // or the OS refusal string), so there is no English fallback to translate.
    if (!r.ok) setEditorError(r.error || 'error')
  }
  const clearEditorError = useCallback(() => setEditorError(null), [])
  return { canOpenInEditor, openInEditor, editorError, clearEditorError }
}

/**
 * The VS Code-family editors the "Open in <editor>" row can target. The id IS
 * the URL scheme; each keeps the `vscode-remote` authority, so the link is
 * `<scheme>://vscode-remote/ssh-remote+<host><path>`. `label` is the brand
 * proper noun — passed to i18n as an interpolation var, never a catalog value,
 * so it is displayed verbatim in every language.
 *
 * This map is also the frontend's own allowlist: `remoteEditorUrl` returns null
 * for any editor id not a key here, so even a value the server-side clamp missed
 * cannot mint a link to an arbitrary registered protocol handler.
 */
export const REMOTE_EDITORS: Record<string, { scheme: string; label: string }> = {
  vscode: { scheme: 'vscode', label: 'VS Code' },
  kiro: { scheme: 'kiro', label: 'Kiro' },
}

/**
 * Build the remote-authority editor URL for `absPath`, or null when the feature
 * is off (no editor), no host is set, or the editor id is unknown.
 *
 * The host goes verbatim after `ssh-remote+` — it is charset-restricted at the
 * config boundary, so it cannot carry URL structure. Every path SEGMENT is
 * percent-encoded (so a space, `#`, `?` or `%` in a filename cannot break out of
 * the path) while the `/` separators stay literal, which is the shape a
 * vscode-remote authority expects.
 *
 * Non-UTF-8 POSIX filenames can arrive as lone surrogates that URI encoding
 * rejects; hiding the editor row is safer than crashing the file surface.
 *
 * Only ABSOLUTE paths are accepted: a relative path would glue its first
 * segment directly onto the SSH authority (`prod` + `-backup/x` would target
 * host `prod-backup`), so anything not starting with `/` returns null.
 *
 * Dot segments (`.` / `..`) are rejected too. `encodeURIComponent` leaves them
 * unchanged, and URL parsing then removes them BEFORE the editor reads the
 * `ssh-remote+<host>` path segment, so `/x/../../ssh-remote+evil/y` would open
 * an SSH session to `evil` and `/../etc/hosts` would target host `etc`. The
 * gateway's own realpath accepts such a path as a real file, so the check must
 * live here. Dotted NAMES (`.env`, `..b`, `...`) are ordinary segments and
 * stay allowed. As a last line, the built URL is re-parsed and must still
 * carry `/ssh-remote+<host>/` as its leading path segment.
 */
export function remoteEditorUrl(editor: string, host: string, absPath: string): string | null {
  const entry = REMOTE_EDITORS[editor]
  if (!entry || !host || !absPath || !absPath.startsWith('/')) return null
  const segments = absPath.split('/')
  if (segments.some((s) => s === '.' || s === '..')) return null
  let encodedPath: string
  try {
    encodedPath = segments.map(encodeURIComponent).join('/')
  } catch (error) {
    if (error instanceof URIError) return null
    throw error
  }
  const url = `${entry.scheme}://vscode-remote/ssh-remote+${host}${encodedPath}`
  const authority = `/ssh-remote+${host}/`
  try {
    const parsed = new URL(url)
    if (parsed.host !== 'vscode-remote' || !parsed.pathname.startsWith(authority)) return null
  } catch {
    return null
  }
  return url
}

/**
 * The remote-editor menu action for `filePath`, or null when the row must not
 * render. Shown whenever an editor AND a host are configured.
 *
 * It is deliberately NOT gated on `directLocal`. That flag cannot tell an
 * `ssh -L` port-forward from a browser on the gateway host (the request arrives
 * on loopback either way), and a headless gateway reached over a forward is the
 * main topology this row exists for. Configuring an SSH host is itself the
 * user's statement that the gateway is reached remotely, and the link only ever
 * acts on the viewer's own machine (their local editor, over their own SSH), so
 * showing it on a genuinely local session costs nothing: the link still works,
 * it just connects to this host over SSH.
 *
 * Unlike the Open row it is deliberately NOT gated on `kind`: a vscode-remote
 * authority opens a directory as a remote workspace, which is useful for editing
 * a whole config tree. Shared by the three file-location surfaces (this menu,
 * MarkdownPanel's overflow, FileViewer's overflow) so the row is gated and
 * labelled identically everywhere, the same way `useCanOpenFile` /
 * `useRevealLabel` are shared.
 *
 * `open` is the click handler and `sent` / `label` are its acknowledgment. The
 * built link is not returned: `open` is the only way to route it (see
 * `openRemoteEditor`), so a consumer cannot skip the acknowledgment by
 * navigating the URL itself. A custom-scheme navigation is fire-and-forget:
 * when no handler is registered for `vscode://` / `kiro://`, or the editor's
 * SSH connection fails, the browser drops the navigation silently and the page
 * reports nothing back. With no toast in the app, the ONE place that can tell
 * the user anything is the row itself, so after a click `label` swaps from
 * "Open in <editor>" to a sent-plus-troubleshooting hint and `sent` is true
 * (the consumer uses it to let the row wrap). The hint has no timer of its own:
 * it would otherwise vanish mid-read while the menu is still open, and the
 * troubleshooting line is exactly what a user whose editor did not open needs
 * to be reading. It stays until the menu closes — the consumer calls `reset()`
 * from its menu's close path (FilePathMenu's items unmount with the Radix
 * content, so there the unmount cleanup is the reset) — and is also reset on
 * path change, so a menu moved to another file does not show it as already
 * sent.
 */
export function useRemoteEditorOpen(
  filePath: string,
): { label: string; open: () => void; sent: boolean; reset: () => void } | null {
  const { remoteEditor } = useBranding()
  const [sent, setSent] = useState(false)
  // Stable so a consumer can hang a close-effect on it without re-firing.
  const reset = useCallback(() => setSent(false), [])
  // An acknowledgment belongs to the path it was earned for: moving the menu
  // to another file must not show it as already sent.
  useEffect(() => { setSent(false) }, [filePath])
  // `!remoteEditor` guards a branding value that has not loaded yet or a
  // partial test stub that omits the field.
  if (!remoteEditor) return null
  // Inside an embedded instance pane the frame's CSP `frame-src` refuses a
  // custom-scheme navigation before it reaches the OS. Electron also denies
  // the renderer's `openExternal` permission and exposes no URL-launch bridge,
  // so assigning `window.location.href` there cannot hand the scheme to the OS.
  // In either host the row would be a dead click that then blames the editor.
  if (isEmbeddedPane() || isElectron) return null
  const url = remoteEditorUrl(remoteEditor.editor, remoteEditor.host, filePath)
  if (!url) return null
  const editor = REMOTE_EDITORS[remoteEditor.editor].label
  const open = () => {
    openRemoteEditor(url)
    setSent(true)
  }
  return {
    label: i18nT(sent
      ? 'components.filePathMenu.remote_editor_sent'
      : 'components.filePathMenu.open_in_remote_editor', { editor }),
    open,
    sent,
    reset,
  }
}

/**
 * The screen-reader half of the "Sent to <editor>" acknowledgment. The row's
 * label swap is a silent in-place text change (an `aria-label` rewrite announces
 * nothing), so this renders the same hint into a visually-hidden polite live
 * region while `sent` is on, and nothing otherwise. The region is mounted (empty)
 * from the moment the menu opens so the later text change is what gets announced.
 * Shared by the three file-location menus beside their remote-editor row, the
 * same way the row itself is shared through `useRemoteEditorOpen`.
 */
export function RemoteEditorSentStatus({ action }: { action: { label: string; sent: boolean } }) {
  return (
    <span role="status" aria-live="polite" className="sr-only" data-testid="remote-editor-sent-status">
      {action.sent ? action.label : ''}
    </span>
  )
}

/**
 * Route a custom-scheme editor link. Uses `window.location.href` (not an
 * `<a href>` the chat-markdown link rewriter would rewrite — see #9925), which
 * hands the URL to the OS scheme handler and leaves the dashboard page in place.
 * Only `useRemoteEditorOpen`'s `open` calls it, so the acknowledgment cannot be
 * skipped by a consumer routing the URL directly; not exported for that reason.
 */
function openRemoteEditor(url: string): void {
  window.location.href = url
}

// ── Menu-item building blocks ────────────────────────────────────────────────

/** The host's in-app openers. Each one, when passed, adds its menu row; a host
 *  with no side panel passes neither and the menu keeps its native rows only. */
interface InAppPathHandlers {
  /** Open the FILE in the host's side panel ("Open in side panel" on a file). */
  onFileOpen?: (path: string) => void
  /** Open a folder as a folder tab in the host's side panel: the directory
   *  itself on a directory ("Open in side panel"), the containing folder on a
   *  file ("Show folder in side panel", not offered for a path with no parent
   *  directory). */
  onFolderOpen?: (path: string) => void
}

interface FilePathMenuItemsProps extends InAppPathHandlers {
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
 * Renders the file-path action items (in-app open / Open / Reveal / Copy path)
 * as ContextMenu items. Drop these into any ContextMenuContent.
 */
function FilePathMenuItems({ filePath, kind, onFileOpen, onFolderOpen }: FilePathMenuItemsProps) {
  const revealErrorId = useId()
  const editorErrorId = useId()
  const isLocal = useBranding().directLocal
  // Shared owner of the platform-aware reveal label (see useRevealLabel) — the
  // same wording MarkdownPanel's overflow and FileViewer's overflow use.
  const revealLabel = useRevealLabel()
  const openLabel = i18nT('components.markdownPanel.open_with_default_app')
  // The one shared Open gate (see useCanOpenFile) — the same predicate the two
  // overflow menus consume, so a Windows/dir target hides Open identically.
  const canOpen = useCanOpenFile(kind)
  // The shared remote-editor action (see useRemoteEditorOpen): an "Open in
  // <editor>" row whenever an editor and SSH host are configured, gated and
  // labelled identically to the MarkdownPanel / FileViewer overflows. null when
  // the feature is off.
  const remoteEditorAction = useRemoteEditorOpen(filePath)

  // In-app rows: gated on the host's handler alone, never on locality, so a
  // viewer on another machine always has an entry that acts on its own screen.
  // "Open in side panel" opens the path itself: a file in the viewer, a
  // directory as a folder tab. A file also gets its containing folder, the
  // in-app counterpart of the native reveal; a path with no parent (a
  // filesystem root, a bare relative name) has nothing to open there, so that
  // row is not offered. Its label leads with the folder, so it reads apart
  // from "Open in side panel" and not as selecting the file in a listing.
  const isDir = kind === 'dir'
  const openInPanel = (isDir ? onFolderOpen : onFileOpen) ?? null
  const containingFolder = !isDir && onFolderOpen ? parentDir(filePath) : null
  const openInPanelLabel = i18nT('components.filePathMenu.open_in_side_panel')
  const openFolderLabel = i18nT('components.filePathMenu.show_folder_in_side_panel')

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

  // "Open in editor" through the shell bridge (see useOpenInEditor), with its
  // own error line separate from the gateway reveal's.
  const { canOpenInEditor, openInEditor, editorError, clearEditorError } = useOpenInEditor(filePath, kind)
  const openInEditorLabel = i18nT('components.markdownPanel.open_in_editor')

  return (
    <>
      {/* A failed reveal/open renders IN the menu (which the rows keep open on
          select) instead of the old blocking alert(). askAgent on: a context
          menu holds no draft, and a policy-blocked or failed reveal is a
          gateway-side condition the agent can explain. */}
      {revealError && (
        <div className="px-2 py-1.5 max-w-[260px]">
          <ErrorNotice
            id={revealErrorId}
            variant="inline"
            className="whitespace-normal"
            message={revealError}
            onDismiss={clearRevealError}
            testId="file-path-menu-error"
          />
        </div>
      )}
      {editorError && (
        <div className="px-2 py-1.5 max-w-[260px]">
          <ErrorNotice
            id={editorErrorId}
            variant="inline"
            className="whitespace-normal"
            message={editorError}
            onDismiss={clearEditorError}
            testId="file-path-menu-editor-error"
          />
        </div>
      )}
      {openInPanel && (
        <ContextMenuItem
          onSelect={() => openInPanel(filePath)}
          aria-label={openInPanelLabel}
        >
          <PanelRight size={14} className="lucide-inline" aria-hidden="true" />
          {openInPanelLabel}
        </ContextMenuItem>
      )}
      {containingFolder && onFolderOpen && (
        <ContextMenuItem
          onSelect={() => onFolderOpen(containingFolder)}
          aria-label={openFolderLabel}
        >
          <FolderTree size={14} className="lucide-inline" aria-hidden="true" />
          {openFolderLabel}
        </ContextMenuItem>
      )}
      {/* The in-app rows are their own group, so they do not read as variants of
          the native rows below that act on the gateway host. */}
      {(openInPanel || containingFolder) && <ContextMenuSeparator />}
      {canOpenInEditor && (
        <ContextMenuItem
          // preventDefault keeps the menu open so a launch failure can render
          // in place through editorError rather than vanishing with the menu.
          onSelect={(e) => { e.preventDefault(); void openInEditor() }}
          aria-label={openInEditorLabel}
        >
          <PenLine size={14} className="lucide-inline" />
          {openInEditorLabel}
        </ContextMenuItem>
      )}
      {editorError && (
        <ErrorNoticeMenuItem
          Item={ContextMenuItem}
          message={editorError}
          describedBy={editorErrorId}
        />
      )}
      {/* Open in the user's LOCAL editor over their own SSH session (see
          useRemoteEditorOpen), beside the desktop shell's Open in editor: both
          launch an editor on the viewer's machine rather than acting on the
          gateway host. preventDefault keeps the menu open, matching the Copy
          row: the OS scheme handoff reports nothing back, so the row's label
          swap is the only acknowledgment and it must stay on screen to be
          read. `sent` lets the longer hint wrap instead of widening the menu,
          at the same cap the in-menu ErrorNotice uses; the sibling live region
          (RemoteEditorSentStatus) is what announces that swap to a screen
          reader. Directory targets are
          allowed (opens as a remote workspace), so it is not gated on `kind`. */}
      {remoteEditorAction && (
        <>
          <ContextMenuItem
            onSelect={(e) => { e.preventDefault(); remoteEditorAction.open() }}
            aria-label={remoteEditorAction.label}
            className={remoteEditorAction.sent ? 'whitespace-normal max-w-[260px]' : undefined}
          >
            <Code2 size={14} className="lucide-inline" />
            {remoteEditorAction.label}
          </ContextMenuItem>
          <RemoteEditorSentStatus action={remoteEditorAction} />
        </>
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
      {revealError && (
        <ErrorNoticeMenuItem
          Item={ContextMenuItem}
          message={revealError}
          describedBy={revealErrorId}
        />
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

export interface FilePathMenuProps extends InAppPathHandlers {
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
export default function FilePathMenu({ filePath, children, kind, onFileOpen, onFolderOpen }: FilePathMenuProps) {
  return (
    <ContextMenu>
      <ContextMenuTrigger asChild>
        {children}
      </ContextMenuTrigger>
      <ContextMenuContent className="min-w-[180px]" onClick={e => e.stopPropagation()}>
        <FilePathMenuItems
          filePath={filePath}
          kind={kind}
          onFileOpen={onFileOpen}
          onFolderOpen={onFolderOpen}
        />
      </ContextMenuContent>
    </ContextMenu>
  )
}
