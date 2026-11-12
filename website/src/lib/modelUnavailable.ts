/**
 * Row kind the backend stamps on the terminal error that ends a spent retry
 * ladder on a model it refused for capacity or a region rollout
 * (`chat_utils.MODEL_UNAVAILABLE_KIND`). The ladder already re-sent the turn
 * to that model and the fallback chain found nothing that served it, so the
 * row settles the turn: the card offers the model picker instead of Resume,
 * and `selectTurnInterrupted` does not count it as an interruption.
 *
 * Both carriers are read, as for the other error kinds: the live broadcast
 * ships `kind`, a rebuilt transcript `meta.kind`. Mirrors
 * `is_model_unavailable_row` in
 * `src/kiro_crew/dashboard/state_parts/transcript_rows.py`.
 */
export const MODEL_UNAVAILABLE_KIND = 'model_unavailable'

export const isModelUnavailable = (m: { role?: string; kind?: string; meta?: unknown }): boolean =>
  m.role === 'error'
  && (m.kind === MODEL_UNAVAILABLE_KIND || (m.meta as { kind?: unknown } | undefined)?.kind === MODEL_UNAVAILABLE_KIND)
