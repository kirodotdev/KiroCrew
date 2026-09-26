import { i18nT } from '../i18n/t'

/**
 * Standing "gateway offline" explanation for a surface whose controls are
 * dimmed and inert, or whose edit is being held.
 *
 * A dimmed control carries its reason in a `title`, which a coarse pointer never
 * surfaces — so a touch user meets dead controls with no explanation anywhere.
 * This row is the visible channel for that, and it is a standing row rather than
 * a tooltip because a disabled item carries `pointer-events-none` and so opens
 * none.
 *
 * Renders a block-level `span` so it nests validly inside an inline wrapper as
 * well as a block one; hosts here include a table cell's `span` and a menu's div.
 */
export function OfflineReasonRow({ testId, className = '', message, id }: {
  testId: string
  /** Placement for the host surface; the menu insets it, the table pins it. */
  className?: string
  /** Overrides the surface-wide reason where a host needs a narrower one, such as
   *  a field whose typed value is held rather than a set of dimmed controls. */
  message?: string
  /** Lets a host point `aria-describedby` here, for a control that is refused but
   *  still interactive and so must keep its own accessible name. */
  id?: string
}) {
  return (
    <span role="status" id={id} data-testid={testId} className={`block text-[11px] text-muted ${className}`}>
      {/* Homed under `utils.offline` alongside the tooltip strings, not under the
          page that happens to mount it first: a default owned by one page would
          render that page's text on every other surface this component reaches. */}
      {message ?? i18nT('utils.offline.dimmed_actions_need_connection')}
    </span>
  )
}
