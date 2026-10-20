/**
 * Mochi - Pinned-files side panel (the vertical strip beside the chat panel)
 *
 * `PinnedSidePanel` groups the pinned files by parent folder and renders one
 * `PinnedChip` per pin: an unseen-update dot, click-to-preview in the desktop
 * shell, and an unpin control that hover or keyboard focus reveals. One of the
 * owners `ChatPanel.tsx` composes; ChatApp imports the panel through
 * `./ChatPanel`, which re-exports it. Upstream Mochi keeps this code in
 * `src/renderer/ChatPanel.tsx`.
 */
import React from 'react'
import { File, Pin, PinOff } from 'lucide-react'
import Clickable from '../../../../components/Clickable'
import { normalizeWindowsPath } from '../../../../utils/fileTokens'
import { PINNED_PANEL_WIDTH } from '../shared/constants'
import { api } from '../mochiApi'
import { DEFAULT_PET_NAME } from '../../builtinPacks'
import { i18nT } from '../../../../i18n/t'
import { isElectron } from '../../../../lib/electron'

// ── Pinned Files Side Panel ──────────────────────────────────────────────────

/** Local re-declaration of PinnedFileEntry (matches src/main/pinnedFilesService.ts) */
interface PinnedFileEntry {
  path: string
  label: string
  pinnedAt: number
  updatedAt?: number
}

interface PinnedSidePanelProps {
  pins: PinnedFileEntry[]
  updatedPaths: Set<string>
  deletedPaths: Set<string>
  visible: boolean
  lang?: string
  onMarkSeen?: (path: string) => void
  /** ADDED (not upstream): the empty hint names the PET, which is renameable. */
  petName?: string
}

/** Vertical side panel showing pinned file chips — renders on the right side of chat. */
export const PinnedSidePanel: React.FC<PinnedSidePanelProps> = ({ pins, updatedPaths, deletedPaths, visible, onMarkSeen, petName }) => {
  if (!visible) return null

  return (
    <div className="pin-panel-root" style={{
      width: PINNED_PANEL_WIDTH,
      minWidth: PINNED_PANEL_WIDTH,
      height: '100vh',
      overflow: 'hidden',
      display: 'flex',
      flexDirection: 'column',
      flexShrink: 0,
      background: 'var(--bg)',
      borderLeft: '1px solid var(--border)',
    }}>
      <style>{`
        .pin-panel-root { animation: pin-panel-enter 0.2s ease-out both; }
        @keyframes pin-panel-enter { from { opacity: 0; transform: translateX(-8px); } to { opacity: 1; transform: translateX(0); } }
        @keyframes pulse-dot {
          0%, 100% { opacity: 1; transform: scale(1); }
          50% { opacity: 0.6; transform: scale(0.85); }
        }
        /* Chip row highlight + unpin reveal on hover OR keyboard focus. Using
           :focus-within (not JS hover state) means the keyboard path matches the
           pointer path with no extra handlers, and the always-rendered unpin
           button — hidden here until reveal — is the chip's natural tab stop. */
        .pin-chip:hover, .pin-chip:focus-within { background: rgba(255,255,255,0.07) !important; }
        .pin-chip-unpin { opacity: 0; pointer-events: none; transition: opacity 0.15s ease; }
        .pin-chip:hover .pin-chip-unpin,
        .pin-chip:focus-within .pin-chip-unpin { opacity: 1; pointer-events: auto; }
        /* The control must be reachable and visible whenever it itself has focus,
           even if a browser scopes :focus-within differently. */
        .pin-chip-unpin:focus, .pin-chip-unpin:focus-visible { opacity: 1; pointer-events: auto; }
      `}</style>
      <div style={{
        flex: 1,
        overflowY: 'auto',
        overflowX: 'hidden',
        padding: '8px 6px',
        display: 'flex',
        flexDirection: 'column',
        gap: 1,
      }}>
        {pins.length === 0 ? (
          <div style={{
            display: 'flex', flexDirection: 'column', alignItems: 'center',
            justifyContent: 'center', flex: 1, padding: '16px 8px', textAlign: 'center',
            opacity: 0.5,
          }}>
            <Pin size={24} strokeWidth={1.5} style={{ marginBottom: 8, color: 'rgba(255,255,255,0.4)' }} />
            <span style={{ fontSize: 11, color: 'rgba(255,255,255,0.5)', lineHeight: 1.4 }}>
              {i18nT('apps.mochi.pinned.empty_hint', { name: petName || DEFAULT_PET_NAME })}
            </span>
          </div>
        ) : (() => {
          // Group pins by full parent path (use full path as key to avoid collisions)
          //
          // `normalizeWindowsPath` first, or that collision-avoidance is exactly
          // inverted on Windows: the store only accepts an ABSOLUTE path
          // (`pinned_files_service.py`, `os.path.isabs`) and keeps it verbatim, so
          // `pin.path` is a native `C:\…` string with no forward slash in it. A
          // bare `split('/')` then yields one element, `pop()` empties it, and
          // every pin — whatever folder it is really in — lands in the same
          // bucket. Only a Windows-SHAPED path is rewritten, so a POSIX
          // directory legitimately named `we\ird` is left alone.
          const folderMap = new Map<string, PinnedFileEntry[]>()
          for (const pin of pins) {
            const parts = normalizeWindowsPath(pin.path).split('/')
            parts.pop() // remove filename
            const fullParent = parts.join('/') || '/'
            if (!folderMap.has(fullParent)) folderMap.set(fullParent, [])
            folderMap.get(fullParent)!.push(pin)
          }
          const groups: { folder: string; fullPath: string; items: PinnedFileEntry[] }[] = []
          for (const [fullPath, items] of folderMap) {
            const folder = fullPath.split('/').pop() || '/'
            groups.push({ folder, fullPath, items })
          }
          return groups.map((group) => (
            <div key={group.fullPath} style={{ marginBottom: 4 }}>
              <div style={{
                fontSize: 10, fontWeight: 600, color: 'rgba(255,255,255,0.35)',
                padding: '4px 8px 2px', textTransform: 'uppercase', letterSpacing: '0.03em',
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              }} title={group.fullPath}>
                {group.folder}
              </div>
              {group.items.map((pin) => (
                <PinnedChip
                  key={pin.path}
                  pin={pin}
                  isUpdated={updatedPaths.has(pin.path)}
                  isDeleted={deletedPaths.has(pin.path)}

                  onMarkSeen={onMarkSeen}
                />
              ))}
            </div>
          ))
        })()}
      </div>
    </div>
  )
}

/** File extension to color mapping for visual distinction */
const extColors: Record<string, string> = {
  ts: '#3b82f6', tsx: '#3b82f6', js: '#eab308', jsx: '#eab308',
  py: '#22c55e', md: '#a78bfa', json: '#f97316', yaml: '#f97316', yml: '#f97316',
  css: '#ec4899', html: '#f43f5e', sh: '#6ee7b7', toml: '#fb923c',
}

function getExtColor(filePath: string): string {
  const ext = filePath.split('.').pop()?.toLowerCase() || ''
  return extColors[ext] || 'var(--accent)'
}

/** Individual pinned file chip — horizontal: icon left, filename right. */
const PinnedChip: React.FC<{
  pin: PinnedFileEntry
  isUpdated: boolean
  isDeleted: boolean
  lang?: string
  onMarkSeen?: (path: string) => void
}> = ({ pin, isUpdated, isDeleted, onMarkSeen }) => {
  // Same native-path rule as the grouping above. `add_pin` fills `label` with
  // `os.path.basename`, so this fallback is only reached by an entry that
  // reached the store without one — the reader tolerates arbitrary shapes in
  // `pinned-files.json` — but when it is reached a bare `split('/')` renders
  // the whole `C:\…` path where the file name belongs.
  const displayName = pin.label || normalizeWindowsPath(pin.path).split('/').pop() || pin.path
  const extColor = getExtColor(pin.path)

  const handleClick = () => {
    api?.markPinnedSeen?.(pin.path)
    // Preview is a shell-bridge capability; in a browser tab the click can
    // only mark the pin seen, not open the OS previewer.
    if (isElectron) api?.previewFile?.(pin.path)
    onMarkSeen?.(pin.path)
  }

  const handleDismiss = (e: React.MouseEvent) => {
    e.stopPropagation()
    api?.unpinFile?.(pin.path)
  }

  // The row is a click target only while the click has a visible payoff:
  // opening the OS previewer (shell only), or clearing an unseen-update dot
  // (HTTP-backed, works everywhere). Otherwise it renders inert — same
  // treatment as the inline file chip — so a browser tab never shows a
  // live-looking control that silently does nothing. Hover still reveals the
  // unpin button, which is its own control and works everywhere.
  const clickable = isElectron || (isUpdated && !isDeleted)

  const chipStyle: React.CSSProperties = {
    display: 'flex',
    flexDirection: 'row',
    alignItems: 'center',
    gap: 6,
    padding: '5px 8px',
    borderRadius: 8,
    cursor: clickable ? 'pointer' : 'default',
    // Hover / keyboard-focus highlight is applied via the `.pin-chip` CSS class
    // (see PinnedSidePanel's <style>): `:hover` and `:focus-within` both light
    // the row and reveal the unpin control, so the keyboard path matches the
    // pointer path without any React hover state.
    background: 'transparent',
    transition: 'background 0.15s ease',
    position: 'relative',
    opacity: isDeleted ? 0.35 : 1,
    flexShrink: 0,
  }

  const body = (
    <>
      {/* Colored file icon */}
      <File size={14} color={isDeleted ? 'var(--text-muted)' : extColor} style={{ flexShrink: 0 }} />

      {/* Filename */}
      <span style={{
        fontSize: 11,
        fontWeight: 400,
        color: isDeleted ? 'var(--text-muted)' : 'rgba(255,255,255,0.8)',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        whiteSpace: 'nowrap',
        flex: 1,
        minWidth: 0,
        lineHeight: 1.3,
      }}>
        {displayName}
      </span>

      {/* Update indicator — pulsing green dot */}
      {isUpdated && !isDeleted && (
        <span style={{
          width: 6,
          height: 6,
          borderRadius: '50%',
          background: '#34d399',
          boxShadow: '0 0 4px rgba(52,211,153,0.6)',
          animation: 'pulse-dot 2s ease-in-out infinite',
          flexShrink: 0,
        }} />
      )}

      {/* Unpin control. Rendered ALWAYS (not hover-gated) so it is a real
          keyboard tab stop and discoverable by screen-reader virtual cursor and
          touch; the `.pin-chip-unpin` class hides it visually until the chip is
          hovered OR focus lands inside it. A pin-off icon PLUS a visible "Unpin"
          word — the panel otherwise shows no pin vocabulary, so text on reveal
          is what tells the user this removes (not adds) the pin, and the
          accessible name carries the filename too. */}
      {!isDeleted && (
        <button
          onClick={handleDismiss}
          className="pin-chip-unpin"
          title={i18nT('apps.mochi.pinned.unpin')}
          aria-label={`${i18nT('apps.mochi.pinned.unpin')} ${displayName}`}
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: 3,
            height: 16,
            borderRadius: 4,
            padding: '0 4px',
            background: 'transparent',
            border: 'none',
            color: 'var(--text-muted, rgba(255,255,255,0.6))',
            fontSize: 10,
            lineHeight: 1,
            cursor: 'pointer',
            flexShrink: 0,
          }}
        >
          <PinOff size={12} />
          <span>{i18nT('apps.mochi.pinned.unpin')}</span>
        </button>
      )}
    </>
  )

  if (!clickable) {
    return (
      // Inert chip: no path to open, so the wrapper carries no click action and
      // is not itself a tab stop — the real keyboard tab stop is the always-
      // rendered unpin <button> inside `body`, which lands focus on an
      // actionable control rather than a silent wrapper. `role="group"` +
      // `aria-label={pin.path}` give the chip an accessible name (the full path)
      // that a screen reader announces in browse mode, where before the path
      // lived only in the mouse-only `title`. `.pin-chip` drives the hover /
      // focus-within reveal of the unpin control.
      <div
        role="group"
        aria-label={pin.path}
        className="pin-chip"
        title={pin.path}
        style={chipStyle}
      >
        {body}
      </div>
    )
  }

  return (
    <Clickable
      onClick={handleClick}
      className="pin-chip"
      aria-label={pin.path}
      title={pin.path}
      style={chipStyle}
    >
      {body}
    </Clickable>
  )
}
