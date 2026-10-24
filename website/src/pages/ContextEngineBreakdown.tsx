import { fmtList, fmtNumber, fmtUnit } from '../i18n/format'
import { i18nT } from '../i18n/t'

/**
 * What the agent engine measured of one turn, as GET /api/telemetry/context-trace
 * returns it on `ContextTurn.engine`. Every size is in CHARACTERS and describes the
 * turn's LAST model request; none is a token count.
 */
export interface EngineMeasurement {
  total_chars: number | null
  model_calls: number | null
  /** Engine category -> characters, for the categories this version knows. */
  categories: Record<string, number>
  /** The history category's own split: user, assistant, thinking, compaction_summary. */
  history: Record<string, number>
  /** Characters in categories this version does not know, kept by name on the trace. */
  unknown_chars: number
  compacted: boolean
  schema_known: boolean
  /** Characters of every prompt this app composed that is still in the engine's context. */
  crew_chars: number
  /** User-role text in the engine's context that this app did not compose; null when the engine sent no split. */
  user_other_chars: number | null
  /** How far this app's own count exceeds the engine's user text. Zero when they agree. */
  mismatch_chars: number
  /** A turn was sent again (retry, regenerate, rewind), so this app's share may count
   *  an attempt the engine no longer holds. Absent on a gateway older than the field. */
  approximate?: boolean
}

/** The turn's wait for its first output, in milliseconds. */
export interface EngineTtft {
  total_ms: number
  setup_ms?: number
  model_ms?: number
}

// Stable category -> catalog-key map, so every key is referenced statically.
const CATEGORY_KEY: Record<string, string> = {
  kiroInstructions: 'pages.contextBreakdown.engine_cat_kiroInstructions',
  toolSpecs: 'pages.contextBreakdown.engine_cat_toolSpecs',
  mcpTools: 'pages.contextBreakdown.engine_cat_mcpTools',
  agentsIndex: 'pages.contextBreakdown.engine_cat_agentsIndex',
  skillsIndex: 'pages.contextBreakdown.engine_cat_skillsIndex',
  powersIndex: 'pages.contextBreakdown.engine_cat_powersIndex',
  steering: 'pages.contextBreakdown.engine_cat_steering',
  skills: 'pages.contextBreakdown.engine_cat_skills',
  toolIO: 'pages.contextBreakdown.engine_cat_toolIO',
  workspace: 'pages.contextBreakdown.engine_cat_workspace',
  memory: 'pages.contextBreakdown.engine_cat_memory',
  attachedFiles: 'pages.contextBreakdown.engine_cat_attachedFiles',
  other: 'pages.contextBreakdown.engine_cat_other',
}

const fmtN = (n: number): string => fmtNumber(Math.round(n))
const fmtSeconds = (ms: number): string => fmtUnit(ms / 1000, 'second', { maximumFractionDigits: 1 })

interface EngineRow {
  id: string
  label: string
  chars: number
}

/**
 * The engine's categories as rows, largest first. `history` is split: the part this
 * app composed (its own count, clamped to what the engine holds), the user text it
 * did not compose, and the engine's replies, reasoning and summary.
 */
export function engineRows(engine: EngineMeasurement, unknownNames: string[] = []): EngineRow[] {
  const rows: EngineRow[] = []
  for (const [id, chars] of Object.entries(engine.categories)) {
    if (id === 'history') continue
    const key = CATEGORY_KEY[id]
    rows.push({ id, label: key ? i18nT(key) : id, chars })
  }
  const user = engine.history.user
  if (typeof user === 'number') {
    rows.push({
      id: 'history.crew',
      label: i18nT('pages.contextBreakdown.engine_hist_crew'),
      chars: Math.min(engine.crew_chars, user),
    })
    rows.push({
      id: 'history.user_other',
      label: i18nT('pages.contextBreakdown.engine_hist_user_other'),
      chars: engine.user_other_chars ?? 0,
    })
  } else if (typeof engine.categories.history === 'number') {
    rows.push({ id: 'history', label: i18nT('pages.contextBreakdown.engine_hist_all'), chars: engine.categories.history })
  }
  const parts: [string, string][] = [
    ['assistant', 'pages.contextBreakdown.engine_hist_assistant'],
    ['thinking', 'pages.contextBreakdown.engine_hist_thinking'],
    ['compaction_summary', 'pages.contextBreakdown.engine_hist_summary'],
  ]
  for (const [part, key] of parts) {
    const chars = engine.history[part]
    if (typeof chars === 'number') rows.push({ id: `history.${part}`, label: i18nT(key), chars })
  }
  if (engine.unknown_chars > 0) {
    const label =
      unknownNames.length > 0
        ? i18nT('pages.contextBreakdown.engine_unknown_named', { names: fmtList(unknownNames) })
        : i18nT('pages.contextBreakdown.engine_unknown')
    rows.push({ id: 'unknown', label, chars: engine.unknown_chars })
  }
  return rows.filter(r => r.chars > 0).sort((a, b) => b.chars - a.chars)
}

/** The selected turn's engine measurement, below this app's own blocks. */
export function EngineBreakdown({
  engine,
  ttft,
  unknownNames = [],
}: {
  engine: EngineMeasurement | null | undefined
  ttft: EngineTtft | null | undefined
  /** Names of the categories this version does not know, as the trace kept them. */
  unknownNames?: string[]
}) {
  const tokenAt =
    ttft && typeof ttft.setup_ms === 'number' && typeof ttft.model_ms === 'number' ? ttft.setup_ms + ttft.model_ms : null
  // Shown only when it says something the total does not: equal once rounded, the
  // second time would read as the same fact twice.
  const firstToken =
    tokenAt !== null && ttft && fmtSeconds(tokenAt) !== fmtSeconds(ttft.total_ms) ? tokenAt : null
  return (
    <div className="mt-4 pt-3 border-t border-border" data-testid="engine-breakdown">
      <div className="text-[13px] font-semibold text-text">{i18nT('pages.contextBreakdown.engine_heading')}</div>
      {ttft ? (
        <p className="m-0 mt-1 text-[12px] text-muted tabular-nums" data-testid="engine-ttft">
          {firstToken !== null
            ? i18nT('pages.contextBreakdown.engine_ttft_detail', {
                total: fmtSeconds(ttft.total_ms),
                model: fmtSeconds(firstToken),
              })
            : i18nT('pages.contextBreakdown.engine_ttft', { total: fmtSeconds(ttft.total_ms) })}
        </p>
      ) : null}
      {engine ? (
        <>
          {typeof engine.total_chars === 'number' ? (
            <p className="m-0 mt-1 text-[12px] text-muted tabular-nums">
              {i18nT('pages.contextBreakdown.engine_total', { chars: fmtN(engine.total_chars) })}
              {typeof engine.model_calls === 'number' ? (
                <> {i18nT('pages.contextBreakdown.engine_calls', { n: fmtN(engine.model_calls) })}</>
              ) : null}
            </p>
          ) : null}
          <ul className="list-none m-0 mt-2 p-0" data-testid="engine-rows">
            {engineRows(engine, unknownNames).map(row => (
              <li key={row.id} data-engine-row={row.id} className="flex items-center justify-between gap-3 py-1 text-[12px] text-text border-b border-border last:border-b-0">
                <span className="truncate">{row.label}</span>
                <span className="font-mono text-muted tabular-nums shrink-0">{fmtN(row.chars)}</span>
              </li>
            ))}
          </ul>
          {engine.mismatch_chars > 0 ? (
            <p className="m-0 mt-2 text-[12px] text-[var(--warn)]" data-testid="engine-mismatch">
              {engine.compacted || (engine.history.compaction_summary ?? 0) > 0
                ? i18nT('pages.contextBreakdown.engine_mismatch_compacted', { chars: fmtN(engine.mismatch_chars) })
                : i18nT('pages.contextBreakdown.engine_mismatch', { chars: fmtN(engine.mismatch_chars) })}
            </p>
          ) : null}
          {engine.approximate ? (
            <p className="m-0 mt-2 text-[12px] text-muted" data-testid="engine-approximate">
              {i18nT('pages.contextBreakdown.engine_approximate')}
            </p>
          ) : null}
          {!engine.schema_known ? (
            <p className="m-0 mt-2 text-[12px] text-muted" data-testid="engine-schema-unknown">
              {i18nT('pages.contextBreakdown.engine_schema_unknown')}
            </p>
          ) : null}
          <p className="m-0 mt-2 text-[12px] text-muted">{i18nT('pages.contextBreakdown.engine_note')}</p>
        </>
      ) : (
        <p className="m-0 mt-1 text-[12px] text-muted" data-testid="engine-not-measured">
          {i18nT('pages.contextBreakdown.engine_not_measured')}
        </p>
      )}
    </div>
  )
}
