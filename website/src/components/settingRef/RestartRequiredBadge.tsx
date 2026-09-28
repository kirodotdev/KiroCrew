/**
 * <RestartRequiredBadge> — the one "restart required" pill in Settings.
 *
 * The badge is SCHEMA-DRIVEN, never hand-maintained. `GET /api/config/schema`
 * marks a field `requiresRestart: true` only when it genuinely cannot take
 * effect in a running gateway (a bound socket, a re-exec jail); every other
 * field hot-reloads and the flag is absent. So a field carries a restart hint
 * exactly when its schema entry says so, and a per-page list of "these need a
 * restart" cannot drift out of date with the backend.
 *
 * `<SchemaRestartBadge configKey="...">` is the form pages use. It renders
 * nothing while the schema is still loading, so a slow fetch shows no hint
 * rather than a wrong one.
 */
import { RotateCcw } from 'lucide-react'
import type { SchemaEntry } from './resolveSettingRef'
import { useConfigSchemaQuery } from './useConfigSchema'
import ErrorNotice from '../ErrorNotice'
import { i18nT } from '../../i18n/t'

/** True when the backend declares this path boot-only. */
export function schemaRequiresRestart(
  configKey: string,
  schemaIndex?: Map<string, SchemaEntry>,
): boolean {
  return schemaIndex?.get(configKey)?.requiresRestart === true
}

/** The pill itself, for a caller that already knows the field is boot-only. */
export function RestartRequiredBadge() {
  return (
    <span
      className="inline-flex items-center gap-1 text-[10px] text-warn bg-warn-subtle px-1.5 py-0.5 rounded"
      title={i18nT('components.restartRequiredBadge.tooltip')}
      data-testid="restart-required-badge"
    >
      <RotateCcw className="lucide-inline" />
      {i18nT('components.restartRequiredBadge.label')}
    </span>
  )
}

export interface SchemaRestartBadgeProps {
  /** Dotted config path, e.g. `dashboard.url`. */
  configKey: string
  /** Schema index override; tests pass a Map instead of mounting a QueryClient. */
  schemaIndex?: Map<string, SchemaEntry>
}

/**
 * Renders the badge only when the schema entry for `configKey` is boot-only.
 * Returns `null` for a hot field, an unknown path, and a schema that has not
 * loaded yet.
 */
export function SchemaRestartBadge({ configKey, schemaIndex: schemaIndexProp }: SchemaRestartBadgeProps) {
  // The hook must run unconditionally; its result is used only when no prop came in.
  const { schema: hookSchema, error } = useConfigSchemaQuery()
  const schemaIndex = schemaIndexProp !== undefined ? schemaIndexProp : hookSchema
  if (schemaIndexProp === undefined && error) {
    // A failed schema fetch is not "no restart needed": without the schema the
    // badge cannot answer, and the user is told so instead of shown nothing.
    return (
      <>
        {/* No hand-off: this badge renders beside editable fields whose unsaved
            draft the hand-off's navigation would destroy — the Slack panel's
            `draft.command` and the Knowledge panel's `localPoolSize`. */}
        <ErrorNotice
          variant="inline"
          message={i18nT('components.restartRequiredBadge.schema_unavailable')}
          testId="restart-required-badge-error"
        />
      </>
    )
  }
  if (!schemaRequiresRestart(configKey, schemaIndex)) return null
  return <RestartRequiredBadge />
}
