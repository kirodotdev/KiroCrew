/**
 * A slot's display name.
 *
 * The keys live in a FILE-SCOPE map indexed inside the `i18nT(...)` call, rather
 * than being assembled as `` `apps.crewCompanion.state.${slot}` ``. The template
 * form reads better but is invisible to the tooling: the key appears nowhere in
 * the source, so `deadKeys.test.ts` reports every state string as unreferenced
 * and `check-i18n-keys.mjs` cannot verify the entry exists. It also made this
 * file's only proof of the `walking` key a doc comment that happened to quote it
 * — delete the comment and the gate's verdict changes, which is not a contract.
 * The map must stay at file scope and be indexed in place: the key checker
 * resolves file-scope bindings only, so a function-local intermediate would make
 * the call site unresolvable again.
 *
 * A pack may declare a slot this build never enters, so an unknown id is normal
 * and returns the raw slot name. Mapping first means the fallback is a plain
 * lookup miss; the previous version had to detect i18next echoing the key back,
 * because a missing string renders as `apps.crewCompanion.state.walking` on
 * screen rather than as an empty value.
 */
import { i18nT } from '../../i18n/t'

/** Slot id → catalog key, for the states this build ships. */
const STATE_LABEL_KEY = {
  approval_pending: 'apps.crewCompanion.state.approval_pending',
  done: 'apps.crewCompanion.state.done',
  error: 'apps.crewCompanion.state.error',
  hiding: 'apps.crewCompanion.state.hiding',
  idle: 'apps.crewCompanion.state.idle',
  listening: 'apps.crewCompanion.state.listening',
  offline: 'apps.crewCompanion.state.offline',
  peekThinking: 'apps.crewCompanion.state.peekThinking',
  peeking: 'apps.crewCompanion.state.peeking',
  thinking: 'apps.crewCompanion.state.thinking',
  walking: 'apps.crewCompanion.state.walking',
  working: 'apps.crewCompanion.state.working',
} as const

export function slotLabel(slot: string): string {
  const k = slot as keyof typeof STATE_LABEL_KEY
  return STATE_LABEL_KEY[k] ? i18nT(STATE_LABEL_KEY[k]) : slot
}
