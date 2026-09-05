import type { CronJob } from '../types'
import { i18nT } from '../i18n/t'

type MigratedTo = NonNullable<CronJob['migrated_to']>

/**
 * Shows where a migrated unit went.
 *
 * Requirement 7.3: the tombstone must be discoverable from the surface that
 * listed the unit before the move. A migrated cron job is retained and
 * non-executing, which on reload is indistinguishable from a user-paused one --
 * cron persists `enabled=false` (so the double-fire guard holds) and derives
 * `user_paused` from it. Without this line the work had moved to another crew
 * and the page said nothing.
 *
 * The redirect is real information, not decoration, so it carries a text label
 * a screen reader reaches rather than a colour or an icon alone.
 */
export function MigratedBadge({ migratedTo }: { migratedTo?: MigratedTo | null }) {
  if (!migratedTo) return null

  const where = migratedTo.label || migratedTo.crew_id

  return (
    <div
      role="note"
      aria-label={i18nT('components.migratedBadge.aria_label', { where, id: migratedTo.remote_unit_id })}
      className="mt-1 flex items-center gap-1.5 text-xs text-amber-700 dark:text-amber-400"
    >
      <span aria-hidden="true">↪</span>
      <span>
        {i18nT('components.migratedBadge.redirect_prefix')} <span className="font-medium">{where}</span> {i18nT('components.migratedBadge.redirect_connector')}{' '}
        <code className="font-mono">{migratedTo.remote_unit_id}</code>
      </span>
    </div>
  )
}
