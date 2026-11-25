import type { AcpBackendProbe } from '../../api/client'
import { ACP_BACKEND_KAS, ACP_BACKEND_KIRO, acpBackendName, acpProbeBlocksUse, DEFAULT_ACP_BACKEND } from '../../api/acpBackend'
import { fmtList } from '../../i18n/format'
import { i18nT } from '../../i18n/t'

const KIRO = ACP_BACKEND_KIRO
const KAS = ACP_BACKEND_KAS
const CLAUDE = 'claude'
const STRANDS = 'strands'
/**
 * The agents this frontend has a translated name and an icon for.
 *
 * A FLOOR for what the panel renders, never a ceiling — see `candidates`. Every id
 * here is a core agent the server always knows, so listing them costs nothing and
 * keeps the control populated while the schema and probe queries are still in
 * flight. An agent absent from this list still gets a row once a server answer
 * names it, labelled with its `policy_id`.
 */
const NAMED = [KAS, KIRO, CLAUDE]

/**
 * The harnesses listed first, in this order; the rest follow by `policy_id`. KAS
 * leads because it is the default, and Strands sits directly under the two Kiro
 * harnesses by the owner's ruling.
 */
const PINNED_ORDER = [KAS, KIRO, STRANDS]

/**
 * The harnesses that never carry the Experimental tag: the default a new install
 * runs (`DEFAULT_ACP_BACKEND`) and Kiro CLI, the all-supported governance floor.
 * Only the default gets the Recommended tag; keying it on the constant keeps the
 * page from naming a different harness than the one a new user is on.
 */
export const stable = (value: string) => value === DEFAULT_ACP_BACKEND || value === KIRO

/**
 * The one harness list Settings > Agent Harness and first-run setup both read:
 * which ids get a row, in what order, which may be switched to, each row's
 * install and use verdict, its display name and its status sentence. Pure: the
 * caller owns the queries and passes their answers in.
 */
export function harnessList({ backends, selectable, current }: {
  backends: AcpBackendProbe[] | undefined
  /** The `agent.acp_backend` schema enum; `undefined` while it is in flight. */
  selectable: string[] | undefined
  /** The configured harness, which always keeps a row. */
  current: string
}) {
  /**
   * This machine's verdict for one backend, or `undefined` when there is none —
   * query in flight, 403, 404, an outright failure, or a row the payload omits.
   * Every caller below treats `undefined` as "say nothing, gate nothing".
   */
  const probe = (value: string): AcpBackendProbe | undefined =>
    backends?.find(b => b.id === value)

  /**
   * Not selectable = this build or the live policy will not serve it. Read from the
   * schema first, since that is the set the PATCH validates against; the probe's own
   * `selectable` is the same fact from the same source, so it is honoured too and the
   * two cannot disagree in a way that lets a dead switch look live. Both fall open
   * when absent, so an in-flight query or a 403 hides nothing.
   */
  const unavailable = (value: string) =>
    (selectable ? !selectable.includes(value) : false) || probe(value)?.selectable === false

  /**
   * Known to the core, and never on offer from this BUILD — as opposed to denied by
   * this deployment's policy.
   *
   * The two look identical through `selectable` alone, and they are not the same
   * thing to a reader: a policy denial is not theirs to fix and is hidden, while a
   * build exclusion is a standing fact about the agent that its own tool-approval
   * line explains. `offered_by_build` is what tells them apart, and it is read
   * together with `unavailable` so a payload that somehow said both would defer to
   * selectability rather than hiding a live option.
   */
  const buildExcluded = (value: string) =>
    probe(value)?.offered_by_build === false && unavailable(value)

  /**
   * Every agent id this panel could render, from the SERVER rather than a literal.
   *
   * This used to be `[KIRO, CLAUDE, KAS]`, which quietly made the panel the last
   * hard-coded copy of the selectable list — the very thing
   * `register_selectable_backend` exists to retire. Filtering a literal by the live
   * schema narrows correctly but can never WIDEN, so an agent an edition registered
   * was selectable on the wire, valid to PATCH, present in the probe payload, and
   * absent from this control. The module note above already promised the opposite
   * ("a build that ships another agent lights it up here with no frontend change");
   * this is what makes that true.
   *
   * Union of the schema enum and the probe payload, because the two answer different
   * questions and either can be in flight: the enum is what PATCH accepts, the probe
   * is every id the core knows (including ones this build cannot select, which
   * `unavailable` then drops).
   *
   * `NAMED` is unioned in as a FLOOR, not a ceiling, and the distinction is the whole
   * fix. As a ceiling it capped the panel at three ids forever. As a floor it only
   * guarantees the core agents still have rows when neither query has answered —
   * which the loading behaviour requires, since hiding a row on absent information is
   * the same mistake as disabling one. `current` joins for the same reason: the saved
   * value must always have a row.
   *
   * Sorted rather than left in arrival order: `PINNED_ORDER` first, then
   * everything else by `policy_id`. Set iteration order would
   * otherwise follow whichever query resolved first and reshuffle the list.
   */
  const candidates = Array.from(
    new Set<string>([
      ...NAMED,
      current,
      ...(selectable ?? []),
      ...(backends ?? []).map(b => b.id),
    ]),
  ).sort((a, b) => {
    const pa = PINNED_ORDER.indexOf(a)
    const pb = PINNED_ORDER.indexOf(b)
    if (pa !== pb) return pa === -1 ? 1 : pb === -1 ? -1 : pa - pb
    // Byte order, not `localeCompare`/`compareText`: these are machine identifiers,
    // and the point of the sort (see above) is to reproduce the order the probe
    // endpoint already returned them in. A collator reads the READER's locale, so
    // the same deployment would order the rows differently per browser -- the
    // between-render reshuffle this sort exists to prevent, just keyed on locale
    // instead of query timing.
    const ka = probe(a)?.policy_id || a
    const kb = probe(b)?.policy_id || b
    if (ka === kb) return 0
    return ka < kb ? -1 : 1
  })

  /**
   * The agents this panel can SWITCH TO.
   *
   * An agent the deployment may not select is HIDDEN, not shown dead. Advertising a
   * forbidden option invites the reader to find out how to enable it, and under a
   * managed policy there is nothing they can do — the answer is not on their machine.
   *
   * `current` is always kept, whatever the verdict. The backend degrades a denied
   * persisted value to the floor on load, so this should not arise; if it ever does,
   * a control that lists no active harness is a worse failure than one extra row.
   */
  const offered = candidates.filter(value => value === current || !unavailable(value))

  /**
   * The agents this panel LISTS, which is a wider set than it offers.
   *
   * Every offered agent, plus the ones this build never offers at all. Those get a
   * row and no Use button: the core knows them well enough for a governance rule to
   * name one, and an operator asking "why can I not pick that?" is asking about a
   * fact the detail already carries. Hiding them answers the question with silence,
   * and offering them would be a switch whose PATCH is refused.
   *
   * Deployment-denied agents stay out — that is `offered`'s rule and it is unchanged.
   */
  const rows = candidates.filter(value => offered.includes(value) || buildExcluded(value))

  /**
   * Installed === 'missing' is the only verdict that disables. `'unknown'` and an
   * absent row explicitly do not: see the header comment on why an optimistic
   * disable is the more expensive mistake.
   */
  const notInstalled = (value: string) => probe(value)?.installed === 'missing'
  /**
   * Installed on disk, but this gateway process cached its absence and cannot
   * spawn it until the cache is dropped. Disabling is right here even though the
   * binary IS present: the click would reach a spawn that fails. This is the one
   * case where a positive install verdict still gates the control — and the one the
   * **Check again** button exists to clear without a restart.
   */
  const needsRestart = (value: string) => probe(value)?.restart_required === true
  /**
   * The probe row's own verdict, shared with first-run setup (`acpProbeBlocksUse`),
   * so the two screens cannot disagree on what a row means. It includes the row's
   * `selectable: false`, but that never disables a rendered button here: an
   * unselectable agent is filtered out of `offered` or shown build-excluded with no
   * Use button at all, so the only reasons a rendered button is dead are ones the
   * user can act on — install the binary, or re-check.
   */
  const cannotUse = (value: string) => acpProbeBlocksUse(probe(value))

  /**
   * A label for any listed id, known to this frontend or not.
   *
   * The fallback is the server's `policy_id`, which exists precisely to be a
   * human-readable wire name (`acp_backends.POLICY_ID_BY_BACKEND`) — it is what a
   * governance rule spells, so it is already a word rather than an internal token.
   * Untranslated, and that is the deliberate trade: a registered agent rendering
   * under its policy name is legible, whereas a missing display name
   * renders a row with no text at all. A core agent that ships selectable gets a
   * real translated entry in the shared helper; this keeps a plugin-registered one usable until
   * then.
   *
   * KIRO is always named by the shared helper, before the policy-id fallback.
   */
  const nameOf = (value: string): string => acpBackendName({ id: value, policy_id: probe(value)?.policy_id })

  /**
   * Why every session start refuses this harness on this host, in first-run setup's
   * words: the setting's sentence when `agent.sandbox` leaves the host's sandbox off,
   * the host's when it has none. The two remedies are opposite, so they never share one.
   */
  const sandboxRefusalLine = (value: string): string =>
    probe(value)?.sandbox_refusal === 'setting'
      ? i18nT('components.kiroPrerequisiteGate.sandbox_off_agent_detail', { name: nameOf(value) })
      : i18nT('components.kiroPrerequisiteGate.sandbox_blocked_agent_detail', { name: nameOf(value) })

  /**
   * The one status sentence a harness carries, derived rather than authored per agent.
   *
   * The order is strict, because the reasons are not equally actionable. A
   * build-excluded agent comes first: nothing about installing or re-checking is
   * worth telling someone about an option this build will never offer, and the
   * detail's folded tool-approval line says why. `missing` comes next because
   * it is the line that tells the user what to DO, and it names the components
   * without the command — the command gets its own copyable block, so folding it
   * into a sentence would put the one string an operator has to run somewhere they
   * cannot click. A sandbox refusal goes before `missing`, for the reason `lead`
   * gives. `unknown` follows and must never read as missing; it reports a
   * failed check, not an absent binary. Only then do the default/experimental lines
   * apply. `DEFAULT_ACP_BACKEND` gets the default sentence; Kiro CLI is stable but
   * not the default, so it says only what the probe measured; anything else gets
   * `Experimental` rather than a claim.
   *
   * Rendered as the row's screen-reader text as well as in the strip, which is what
   * lets the row's single glyph stay a glyph: the state is a WORD somewhere for
   * every row, not a shape a reader has to decode. `named` marks a sentence that
   * already names the agent, so the hidden block does not lead it with the name too.
   */
  const status = (value: string): { text: string; named?: boolean } => {
    const row = probe(value)
    if (buildExcluded(value)) return { text: i18nT('pages.developer.agentBackendTab.not_offered_by_this_build') }
    if (row?.sandbox_refusal) return { text: sandboxRefusalLine(value), named: true }
    if (row?.installed === 'missing' && row.found_version)
      return {
        text: i18nT('components.kiroPrerequisiteGate.agent_wrong_version_detail', {
          name: nameOf(value),
          version: row.found_version,
        }),
        named: true,
      }
    if (row?.installed === 'missing')
      return {
        text: i18nT('pages.developer.agentBackendTab.missing_components', {
          components: fmtList(row.missing_components, { style: 'narrow' }),
        }),
      }
    // The check failed, and the SECOND half of that sentence is what UX blocked on:
    // the switch deliberately stays live here, and a bright Use button under a
    // "could not check" line reads as a mistake the reader refuses to touch. The
    // reasoning was in this file's own header, where no user will ever see it.
    if (row?.installed === 'unknown')
      return { text: i18nT('pages.developer.agentBackendTab.install_check_failed_can_still_switch') }
    // AFTER the missing/unknown lines and BEFORE the descriptor lines: this row
    // has a positive install verdict, so it would otherwise fall through to
    // `Experimental` and say nothing about why the switch is dead.
    // Names the cheap remedy, which is the button directly beside this line, before
    // the gateway restart. The SAME sentence first-run setup shows for this state:
    // the two screens are built from one picker, and two remedies for one state
    // ("Settings > About" here, "that host" there) read as two different problems.
    if (row?.restart_required)
      return {
        text: i18nT('components.kiroPrerequisiteGate.agent_restart_required_detail', {
          name: nameOf(value),
        }),
        named: true,
      }
    if (value === DEFAULT_ACP_BACKEND) return { text: i18nT('pages.developer.agentBackendTab.default_harness') }
    if (stable(value))
      return row?.installed === 'installed'
        ? { text: i18nT('components.kiroPrerequisiteGate.agent_ready_to_use', { name: nameOf(value) }), named: true }
        : { text: '' }
    return { text: i18nT('pages.developer.agentBackendTab.experimental') }
  }

  return {
    probe, unavailable, buildExcluded, candidates, offered, rows, notInstalled, needsRestart, cannotUse,
    nameOf, sandboxRefusalLine, status,
  }
}
