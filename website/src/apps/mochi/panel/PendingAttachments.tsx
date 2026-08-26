/**
 * Pending-attachment strip for the chat composer.
 *
 * NOT from the original Mochi — it belongs to the drag-and-drop feature kept
 * from the desktop-buddy fork, extended here to any number of images.
 *
 * Driven by the composer's attachment STATE, not by its text. The reference
 * markdown is composed only at send time (composerDrop.composeMessage), so the
 * box the user types in stays clean while the strip stays the single record of
 * what will be attached.
 *
 * Thumbnails load through core's /api/file-raw rather than a file:// URL: the
 * panel is an ordinary page on the gateway origin, and that route already
 * enforces the symlink and sensitive-path checks.
 */
import { localFileUrl, openLightbox } from './panelBridge'
import { type PendingAttachment } from './composerDrop'
import { i18nT } from '../../../i18n/t'

interface Props {
  /** Attachments queued for the next send, in display order. */
  items: readonly PendingAttachment[]
  /** Called with the path of the chip the user dismissed. */
  onRemove: (path: string) => void
}

export function PendingAttachments({ items, onRemove }: Props) {
  if (items.length === 0) return null

  return (
    <div
      style={{
        display: 'flex',
        gap: 6,
        flexWrap: 'wrap',
        padding: '6px 10px',
        borderTop: '1px solid var(--border)',
      }}
    >
      {items.map((item) => (
        <Chip
          key={item.path}
          item={item}
          onRemove={() => onRemove(item.path)}
        />
      ))}
    </div>
  )
}

function Chip({ item, onRemove }: { item: PendingAttachment; onRemove: () => void }) {
  return (
    <div
      style={{
        position: 'relative',
        display: 'flex',
        alignItems: 'center',
        gap: 5,
        maxWidth: 140,
        height: item.isImage ? 40 : 24,
        padding: item.isImage ? 0 : '0 6px',
        borderRadius: 6,
        border: '1px solid var(--border)',
        background: 'var(--bg-input)',
        overflow: 'hidden',
      }}
      title={item.name}
    >
      {item.isImage ? (
        <img
          // Clickable: the chip is a 40px thumbnail, so seeing what was actually
          // attached requires opening it. Passes the PATH, not the rendered URL,
          // because the OS viewer opens files.
          onClick={() => openLightbox(item.path)}
          title={item.name}
          src={localFileUrl(item.path)}
          alt={item.name}
          style={{ width: 40, height: 40, objectFit: 'cover', display: 'block' }}
        />
      ) : (
        <span
          style={{
            fontSize: 11,
            color: 'var(--text)',
            whiteSpace: 'nowrap',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
          }}
        >
          {item.name}
        </span>
      )}
      <button
        onClick={onRemove}
        aria-label={`${i18nT('components.chatInput.remove')}: ${item.name}`}
        style={{
          position: item.isImage ? 'absolute' : 'static',
          top: item.isImage ? 1 : undefined,
          right: item.isImage ? 1 : undefined,
          width: 14,
          height: 14,
          flexShrink: 0,
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          borderRadius: '50%',
          border: 'none',
          // Always visible rather than hover-only: the strip is small and a
          // hidden control on a 40px thumbnail is hard to find, especially on a
          // trackpad where there is no hover before the click.
          background: 'rgba(0,0,0,0.6)',
          color: '#fff',
          fontSize: 10,
          lineHeight: 1,
          cursor: 'pointer',
          padding: 0,
        }}
      >
        ×
      </button>
    </div>
  )
}
