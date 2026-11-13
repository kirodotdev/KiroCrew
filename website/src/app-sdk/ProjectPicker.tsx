/**
 * The dashboard's project picker, offered to apps (`@kirocrew/app-sdk`).
 *
 * It is the same `components/ProjectPicker` the chat sidebar's folder settings
 * and the composer's project chooser open (Favorites, Recent, Browse, the system
 * folder picker on the desktop app, New folder), so an app that asks for a
 * project directory does not restate it, and a directory reachable there is
 * reachable here, spelled the same way.
 *
 * Which picker mounts is decided by the identity the host published, never by a
 * prop the app passes:
 *
 * - An EXTERNAL app gets the picker once its manifest's `permissions.api`
 *   admits every path the picker sends (`PROJECT_PICKER_PATHS`). With any of
 *   them not admitted it renders a refusal naming the first missing path and
 *   mounts nothing, so no request is sent.
 * - A BUILTIN page is host code and gets the picker as it always has.
 * - With no app identity at all the picker fails closed, like an external app
 *   with nothing declared.
 *
 * The picker is host code with a fixed set of endpoints, so checking each of
 * them before it mounts covers every request it can send. Each path is checked
 * on its own, never through its parent: an exact entry (`=/api/browse-dirs`)
 * admits the listing and not New folder's write beneath it. Its requests keep the
 * dashboard client's deadlines, error codes and journaled error reports, which
 * is what its failure notices read; a second client would word the same
 * refusal differently in an app. That grants nothing the scoped transport would
 * not: a scoped request carries the dashboard user's own authority too (the
 * frontend scoping is a guardrail, see the trust model), and the same matcher
 * `useAppApi()` and the gateway's token check use decides here.
 *
 * `errorHandoff` is not offered: its hand-off navigates to the chat, which
 * unmounts the app page and whatever the user had not saved on it.
 */
import { useContext, type RefObject } from 'react'
import HostProjectPicker from '../components/ProjectPicker'
import ErrorNotice from '../components/ErrorNotice'
import { i18nT } from '../i18n/t'
import { useAppIdentity } from './identity'
import { AppSdkContext, apiPathAllowed } from './scopedApi'

/** Every path the picker sends a request to. A bare manifest entry covers its
 *  children, so declaring `/api/browse-dirs` admits both browse paths; an exact
 *  `=/api/browse-dirs` admits the listing alone, and the picker refuses. The
 *  favourite add and remove use `/api/favorite-projects` itself. */
export const PROJECT_PICKER_PATHS = [
  '/api/browse-dirs',
  '/api/browse-dirs/create',
  '/api/recent-projects',
  '/api/favorite-projects',
] as const

const NO_PATHS: readonly string[] = []

/** The first path `allowedPaths` does not admit, or null when all are admitted.
 *  Exported for tests. */
export function undeclaredProjectPickerPath(allowedPaths: readonly string[]): string | null {
  return PROJECT_PICKER_PATHS.find((path) => !apiPathAllowed(allowedPaths, path)) ?? null
}

export interface AppProjectPickerProps {
  /** Whether the picker is shown. */
  open: boolean
  /** Called with `false` when the picker asks to close (Escape, a click outside). */
  onOpenChange: (open: boolean) => void
  /** The element the popover is anchored to. */
  anchorRef?: RefObject<HTMLElement | null>
  /** An anchor rectangle, for a caller with no element to anchor to. */
  anchorRect?: DOMRect | null
  /** Called with the absolute directory the user chose. */
  onSelect: (path: string) => void
  /** The directory Browse opens in. Empty or absent opens at the gateway user's home. */
  startPath?: string
}

export default function ProjectPicker({ open, onOpenChange, anchorRef, anchorRect, onSelect, startPath = '' }: AppProjectPickerProps) {
  const identity = useAppIdentity()
  const sdk = useContext(AppSdkContext)
  const builtin = identity?.origin === 'builtin'
  // Read off the context directly, not through useAppApi(), which throws with
  // no provider: a surface with no provider must fail CLOSED.
  const missing = builtin ? null : undeclaredProjectPickerPath(sdk?.info.permissions.api ?? NO_PATHS)
  if (!open) return null
  if (missing !== null) {
    return (
      // No hand-off: it navigates to the chat and unmounts the app page, losing
      // what the form the picker opens from has not saved (the directory field
      // above it on Create Folders), the same reason errorHandoff is not offered.
      <ErrorNotice
        variant="inline"
        testId="app-project-picker-not-declared"
        message={i18nT('appSdk.projectPicker.not_declared', { path: missing })}
      />
    )
  }
  return (
    <HostProjectPicker
      open={open}
      onOpenChange={onOpenChange}
      anchorRef={anchorRef}
      anchorRect={anchorRect}
      onSelect={onSelect}
      startPath={startPath}
    />
  )
}
