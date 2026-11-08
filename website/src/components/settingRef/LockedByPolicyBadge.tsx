/**
 * <LockedByPolicyBadge> — the one "locked by policy" pill.
 *
 * An enterprise build ignores some local switches: only the organization's
 * policy can turn them on (RFC crew-enterprise-edition D3). The backend says so
 * per field: `GET /api/config/schema` carries `lockedByPolicy` on a locked path,
 * and a feature's own GET can carry `locked_by_policy`. The badge shows that the
 * setting is locked, so a toggle that does nothing explains itself. Absent on an
 * oss build, where nothing is locked.
 */
import { Lock } from 'lucide-react'
import type { SchemaEntry } from './resolveSettingRef'
import { i18nT } from '../../i18n/t'

/** True when the backend reports this config path as locked by policy. */
export function schemaLockedByPolicy(configKey: string, schemaIndex?: Map<string, SchemaEntry>): boolean {
  return schemaIndex?.get(configKey)?.lockedByPolicy === true
}

export function LockedByPolicyBadge({ id }: { id?: string }) {
  return (
    <span
      id={id}
      className="inline-flex items-center gap-1 text-[10px] text-muted bg-bg-accent border border-border px-1.5 py-0.5 rounded"
      title={i18nT('components.lockedByPolicyBadge.tooltip')}
      data-testid="locked-by-policy-badge"
    >
      <Lock className="lucide-inline" />
      {i18nT('components.lockedByPolicyBadge.label')}
    </span>
  )
}
