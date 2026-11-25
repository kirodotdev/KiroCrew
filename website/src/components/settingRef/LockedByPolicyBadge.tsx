/**
 * <LockedByPolicyBadge> — the one "locked by policy" pill.
 *
 * An LTS build ignores some local switches: only the organization's
 * policy can turn them on (RFC crew-lts-edition D3). The backend says so
 * per field: `GET /api/config/schema` carries `lockedByPolicy` on a locked path,
 * and a feature's own GET can carry `locked_by_policy`. The badge shows that the
 * setting is locked, so a toggle that does nothing explains itself. Absent on an
 * oss build, where nothing is locked.
 *
 * `edition` is for a lock no policy term can lift (`locked_by_edition`, and
 * every schema lock): naming a policy there sends an admin looking for a
 * control that does not exist, so the pill names the edition instead.
 * `setByEdition` is for a value the edition fixes rather than withholds (the
 * update channel): "not available" there would read as "your channel is broken".
 */
import { Lock } from 'lucide-react'
import type { SchemaEntry } from './resolveSettingRef'
import { i18nT } from '../../i18n/t'

/** True when the backend reports this config path as locked by policy. */
export function schemaLockedByPolicy(configKey: string, schemaIndex?: Map<string, SchemaEntry>): boolean {
  return schemaIndex?.get(configKey)?.lockedByPolicy === true
}

/** The sentence that explains a lock: the edition's, or the policy's. */
export function lockedNote(edition?: boolean): string {
  return edition
    ? i18nT('components.lockedByPolicyBadge.edition_tooltip')
    : i18nT('components.lockedByPolicyBadge.tooltip')
}

export function LockedByPolicyBadge({ id, edition, setByEdition }: { id?: string; edition?: boolean; setByEdition?: boolean }) {
  const label = setByEdition
    ? i18nT('components.lockedByPolicyBadge.edition_set_label')
    : edition ? i18nT('components.lockedByPolicyBadge.edition_label') : i18nT('components.lockedByPolicyBadge.label')
  return (
    <span
      id={id}
      className="inline-flex shrink-0 items-center gap-1 whitespace-nowrap text-[10px] text-muted bg-bg-accent border border-border px-1.5 py-0.5 rounded"
      title={setByEdition ? i18nT('components.lockedByPolicyBadge.edition_set_tooltip') : lockedNote(edition)}
      data-testid="locked-by-policy-badge"
    >
      <Lock className="lucide-inline" />
      {label}
    </span>
  )
}
