// PublicPublishAckModal — the blocking acknowledgment gate in front of every
// action that creates a publicly accessible website (issue #3599).
//
// #3493 put the exposure warning next to each confirm button, but inline text can
// be scrolled past and read as decoration. This modal cannot: it takes the
// keyboard (focus trap in `Modal`), states what is about to become public, states
// how long it stays public, and requires the human to press a button whose label
// IS the acknowledgment.
//
// Two deliberate choices, both from the issue:
//   • the confirm is `danger`, never `primary` — it is not the default action, and
//     nothing here submits a form, so Enter cannot fire it;
//   • it is not pre-focused — `Modal` moves focus to the FIRST focusable node,
//     which is its own close button, and Cancel precedes Confirm in the footer.
// A test pins both, because either one is easy to undo by reordering markup.
import { AlertTriangle } from 'lucide-react'
import Modal from './Modal'
import { Btn } from './ui'

import { i18nT } from '../i18n/t'

export default function PublicPublishAckModal({
  open,
  target,
  ttlHours,
  persistentExposureNote,
  busy = false,
  onCancel,
  onConfirm,
}: {
  /** Whether the acknowledgment gate is showing. */
  open: boolean
  /** What is about to become public (artifact slug or site id). */
  target: string
  /**
   * Hours the URL stays public; 0 means persistent (no expiry). The exposure
   * WINDOW is part of the acknowledgment — a link that never expires is a
   * different decision from one that dies tomorrow, so the modal says which.
   */
  ttlHours: number
  /**
   * Overrides the persistent-exposure sentence for a destination that ends the
   * exposure some other way. The default names `recall` and `destroy`, which are
   * the DEPLOY surface's actions; a destination without them would be telling the
   * user their way out is something that does not exist for it. Ignored when
   * ``ttlHours`` is set, since the link then expires on its own.
   */
  persistentExposureNote?: string
  /** Publish in flight — both buttons are held so the modal cannot double-fire. */
  busy?: boolean
  /** Backdrop, Escape, the close button, and Cancel all land here (safe direction). */
  onCancel: () => void
  /** The acknowledged publish. Only ever called from the explicit confirm button. */
  onConfirm: () => void
}) {
  return (
    <Modal
      open={open}
      onClose={() => { if (!busy) onCancel() }}
      maxWidth={520}
      title={
        <span className="inline-flex items-center gap-2 text-warn">
          <AlertTriangle size={16} aria-hidden="true" />
          {i18nT('components.publicPublishAckModal.title')}
        </span>
      }
      ariaLabel={i18nT('components.publicPublishAckModal.title')}
      footer={
        <>
          <Btn onClick={onCancel} disabled={busy}>
            {i18nT('components.publicPublishAckModal.cancel')}
          </Btn>
          <Btn danger onClick={onConfirm} disabled={busy}>
            {busy
              ? i18nT('components.publicPublishAckModal.publishing')
              : i18nT('components.publicPublishAckModal.acknowledge_publish')}
          </Btn>
        </>
      }
    >
      <p className="text-sm text-text m-0">
        {i18nT('components.publicPublishAckModal.anyone_with_the_link', { target })}
      </p>
      <p className="text-sm text-text mt-3 mb-0 font-medium">
        {i18nT('components.publicPublishAckModal.do_not_publish_anything')}
      </p>
      <div className="mt-3 text-sm text-warn p-2 rounded border border-warn/30 bg-warn-subtle">
        {/* Body-size, not fine print: how long the link stays public is the
            decision the human is actually making here. */}
        {ttlHours > 0
          ? i18nT('components.publicPublishAckModal.exposure_window_ttl', { count: ttlHours })
          : persistentExposureNote ||
            i18nT('components.publicPublishAckModal.exposure_window_persistent')}
      </div>
    </Modal>
  )
}
