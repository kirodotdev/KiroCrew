import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Bot, Boxes, Sparkles, Terminal } from 'lucide-react'

import { api } from '../../api/client'
import type { AcpBackendProbe } from '../../api/client'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsCard, SettingsButtonGroup } from '../../components/settings'
import { useConfigSchema } from '../../components/settingRef/useConfigSchema'
import { i18nT } from '../../i18n/t'

/** The config field the switch owns. Also the schema path the options are gated on. */
const CONFIG_KEY = 'agent.acp_backend'

/**
 * Backend ids, verbatim from `acp/types.py`. `''` (Kiro CLI) is the shipped
 * default and is a REAL value, not "unset" — the empty string is how the core
 * spells the Kiro backend, so it must round-trip as itself.
 */
const KIRO = ''
const CLAUDE = 'claude'
const KAS = 'kas'
/**
 * Named for `caveat` alone, and deliberately NOT added to `NAMED`: this frontend
 * has no translated label for Codex, so its chip carries the server's `policy_id`
 * (see the fallback in `nameOf`). Listing it as NAMED without a translated entry
 * would trade a legible wire name for a chip with no text.
 */
const CODEX = 'codex'

/**
 * The agents this frontend has a translated name and an icon for.
 *
 * A FLOOR for what the panel renders, never a ceiling — see `candidates`. Every id
 * here is a core agent the server always knows, so listing them costs nothing and
 * keeps the control populated while the schema and probe queries are still in
 * flight. An agent absent from this list still gets a row once a server answer
 * names it, labelled with its `policy_id`.
 */
const NAMED = [KIRO, CLAUDE, KAS]

/**
 * DOM id of the row that states a backend's status.
 *
 * The option button carries this as `aria-describedby`, so the reason a choice is
 * dead reaches a screen reader instead of living in visual proximity only. KIRO is
 * the empty string, hence the explicit `kiro` fallback — an id must not end in the
 * bare separator.
 */
const statusId = (value: string) => `agent-backend-status-${value || 'kiro'}`

/**
 * Poll interval for the machine probe, in ms.
 *
 * Matched to `acp_backend_probe.CACHE_TTL_SECONDS` (30s) on purpose: the endpoint
 * serves that cache, so polling faster only adds requests that return the same
 * bytes, and polling slower leaves a just-installed harness disabled for longer than
 * the server would.
 */
const PROBE_REFRESH_MS = 30_000

/**
 * Developer > Agent Backend — pick which agent runs a session.
 *
 * ## Why this exists again
 *
 * The public core used to ship a multi-provider `ProviderPanel` and deleted it
 * when it collapsed to Kiro CLI only (`refactor(website): collapse provider layer
 * to KiroACP-only`). The backend kept all three agents wired the whole time, so
 * `agent.acp_backend` has been switchable with no way to switch it. This is that
 * control, minus the dead parts of the old panel (Bedrock model ids, a Claude Code
 * migration wizard, a provider enum that now has exactly one member).
 *
 * ## Why the choices come from the server
 *
 * Every agent the code knows about is listed, but only the ones this build can
 * actually run are selectable — that set is read from `GET /api/config/schema`
 * (`enumValues`), which the backend resolves per request from
 * `acp_backends.selectable_backend_values()`, the same owner
 * `PATCH /api/config/kirocrew` validates against. So the enabled options and the
 * values the wire accepts cannot disagree, and a build that ships another agent
 * lights it up here with no frontend change.
 *
 * That last clause is why `candidates` is a union of server answers rather than a
 * list of ids written here. An earlier revision filtered a hard-coded
 * `[KIRO, CLAUDE, KAS]` by the schema, which narrows correctly and can never widen —
 * so an agent an edition registered through `register_selectable_backend` was
 * selectable on the wire and invisible in the only control that sets it. Ids this
 * frontend has no translated name for render under their `policy_id`.
 *
 * ## Why there is a SECOND gate, and why it is allowed to say nothing
 *
 * The schema answers a build/edition-and-policy question — can this gateway serve
 * that agent at all. It cannot answer the machine question: whether the harness's
 * components are actually installed here. So a build that ships an agent lit the
 * option up whether or not the binary existed, and a user could neither see why it
 * was dead nor be told what to install. `GET /api/acp-backends` supplies that
 * second fact per backend, and the two compose: an option is dead when this build
 * will not serve it OR this machine is missing it.
 *
 * The probe has THREE answers and the third is load-bearing. `unknown` means the
 * check itself failed, and it leaves the option ENABLED — collapsing it onto
 * `missing` would tell someone to run a global install for something they may
 * already have. The same fail-open applies to the query being in flight, having
 * failed, or the endpoint answering 403 (non-owner) or 404 (older gateway): all of
 * those are absent information, not a verdict, so gating falls back to the schema
 * alone and behaves exactly as it did before this endpoint existed. Nothing here
 * flashes disabled and then live. The owner `PATCH` allowlist is the real gate, so
 * an optimistic enable can only ever cost one visible refusal, while an optimistic
 * DISABLE costs a user a control they were entitled to and an install they did not
 * need.
 *
 * ## Why each row says so little
 *
 * An earlier revision wrote a prose sentence per agent claiming what each one
 * supports — sandboxing, shared processes, mid-turn steer, subagent progress.
 * Those claims were not measured anywhere; they were asserted here, in the view
 * layer, where nothing can contradict them. They were wrong in the ways
 * unmeasured claims usually are.
 *
 * The status line per row is therefore limited to what this build can actually
 * establish, and the vocabulary is taken from the ACP-adapter card rather than
 * invented again: `Default. All features supported.` for the backend whose
 * descriptor is all-supported, `Experimental` for one that is not, and a
 * not-enabled line for one this build cannot run. Per-capability detail
 * (which feature is supported, degraded, or unverified per backend) needs the
 * descriptor table that owns those facts and is deliberately NOT restated here.
 *
 * The two probe lines (missing components, and check-failed) are the exception
 * that proves the rule rather than a relaxation of it: they are not claims about
 * what a backend supports, they are a measurement the server took on this machine
 * and named. They say only what was measured — which components are absent, and
 * the command that installs them when there is one to give.
 *
 * Deliberately NOT under `pages/settings/`: `gen-settings-registry.mjs` scans that
 * directory, and indexing an agent switch into Settings search would advertise it
 * as an ordinary preference — it changes which agent binary runs.
 */
export function AgentBackendTab() {
  const qc = useQueryClient()
  const [saveError, setSaveError] = useState('')
  const schema = useConfigSchema()

  const cfgQ = useQuery<{ agent?: { acp_backend?: string } }>({
    queryKey: ['kirocrewConfig'],
    queryFn: () => api.kirocrewConfig(),
  })

  /**
   * The machine probe. `retry: false` because the two expected failures — 403 for a
   * non-owner and 404 on a gateway that predates the endpoint — are permanent
   * answers, and retrying them just delays the fail-open path this component
   * already handles. A rejection is never surfaced as an error to the user: the
   * absence of probe information is not something they can act on.
   *
   * `staleTime: 0` + `refetchInterval` are load-bearing, not tuning. This app sets a
   * GLOBAL `staleTime: Infinity`, and inheriting it makes the probe answer permanent
   * for the life of the page: an operator who follows the panel's own install
   * instruction would leave the option disabled with no way to re-ask short of a
   * reload. The interval matches the server probe's own TTL, so a poll can never be
   * cheaper than the answer it re-reads, and the endpoint is a resolver read behind
   * that TTL cache rather than a fresh shell-out per request.
   */
  const probeQ = useQuery<{ backends: AcpBackendProbe[] }>({
    queryKey: ['acpBackends'],
    queryFn: () => api.acpBackends(),
    retry: false,
    staleTime: 0,
    refetchInterval: PROBE_REFRESH_MS,
  })

  const patchMut = useMutation({
    mutationFn: (value: string) => api.patchConfig(CONFIG_KEY, value),
    onSuccess: () => {
      setSaveError('')
      qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    },
    // No optimistic write and no local mirror of the value: the button group reads
    // straight from the query, so a rejected PATCH needs no revert — the cache was
    // never moved off the server's answer.
    onError: () => setSaveError(i18nT('pages.developer.agentBackendTab.could_not_save_the_agent_backend')),
  })

  if (cfgQ.isLoading) {
    return (
      <div className="text-muted text-sm py-12 text-center">
        {i18nT('pages.developer.agentBackendTab.loading_configuration')}
      </div>
    )
  }

  /**
   * A failed read is NOT the default value.
   *
   * `?? KIRO` is right for a config that genuinely omits the key — the shipped
   * default really is Kiro CLI. It is wrong for a read that FAILED: the value is
   * then unknown, and defaulting paints Kiro CLI as the pressed option, so an
   * operator running KAS is shown the wrong agent by a control that looks live.
   * Offer the retry instead of guessing.
   */
  if (cfgQ.isError) {
    return (
      <div className="py-12 text-center">
        <div className="text-muted text-sm">
          {i18nT('pages.developer.agentBackendTab.could_not_load_the_agent_backend')}
        </div>
        <button
          type="button"
          className="mt-3 text-[13px] px-3 py-[5px] rounded-md border border-border bg-bg-elevated text-text-strong cursor-pointer"
          onClick={() => cfgQ.refetch()}
        >
          {i18nT('pages.developer.agentBackendTab.retry')}
        </button>
      </div>
    )
  }

  const current = cfgQ.data?.agent?.acp_backend ?? KIRO

  /**
   * `undefined` while the schema is in flight — every option stays enabled rather
   * than flashing disabled and then live, which would read as a broken control on
   * a slow load. The PATCH allowlist is the real gate either way, so an optimistic
   * enable can only cost one visible refusal.
   */
  const selectable = schema?.get(CONFIG_KEY)?.enum

  /**
   * This machine's verdict for one backend, or `undefined` when there is none —
   * query in flight, 403, 404, an outright failure, or a row the payload omits.
   * Every caller below treats `undefined` as "say nothing, gate nothing".
   */
  const probe = (value: string): AcpBackendProbe | undefined =>
    probeQ.data?.backends.find(b => b.id === value)

  /**
   * Not selectable = this build or the live policy will not serve it. Read from the
   * schema first, since that is the set the PATCH validates against; the probe's own
   * `selectable` is the same fact from the same source, so it is honoured too and the
   * two cannot disagree in a way that lets a dead option look live. Both fall open
   * when absent, so an in-flight query or a 403 hides nothing.
   */
  const unavailable = (value: string) =>
    (selectable ? !selectable.includes(value) : false) || probe(value)?.selectable === false

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
   * value must always have a chip.
   *
   * Sorted rather than left in arrival order: the two kiro-family harnesses first —
   * KIRO because it is the default and the floor, then KAS — and everything else by
   * `policy_id`, which is the order the probe endpoint already sorts by. Set iteration
   * order would otherwise follow whichever query resolved first and reshuffle the
   * control between renders.
   */
  const candidates = Array.from(
    new Set<string>([
      ...NAMED,
      current,
      ...(selectable ?? []),
      ...(probeQ.data?.backends ?? []).map(b => b.id),
    ]),
  ).sort((a, b) => {
    if (a === KIRO) return -1
    if (b === KIRO) return 1
    // KAS second, ahead of the byte order below. It is not an adapter: it is kiro-cli's
    // own ACP relay, resolved from the same binary and sharing kiro's install verdict
    // (`_probe_kas` delegates to `_probe_kiro`), so the two harnesses that are really
    // one install belong adjacent at the head of the row. Under `policy_id` alone it
    // sorts on 'k' and lands behind every adapter whose name happens to start earlier
    // ('claude', 'codex'), which reads to the operator as a rank rather than an
    // alphabet.
    if (a === KAS) return -1
    if (b === KAS) return 1
    // Byte order, not `localeCompare`/`compareText`: these are machine identifiers,
    // and the point of the sort (see above) is to reproduce the order the probe
    // endpoint already returned them in. A collator reads the READER's locale, so
    // the same deployment would order the chips differently per browser -- the
    // between-render reshuffle this sort exists to prevent, just keyed on locale
    // instead of query timing.
    const ka = probe(a)?.policy_id || a
    const kb = probe(b)?.policy_id || b
    if (ka === kb) return 0
    return ka < kb ? -1 : 1
  })

  /**
   * The agents this panel renders at all.
   *
   * An agent the deployment may not select is HIDDEN, not shown disabled. A greyed
   * chip invites the reader to find out how to enable it, and under a managed policy
   * there is nothing they can do — the answer is not on their machine. Advertising a
   * forbidden option is also the opposite of what a restriction is for.
   *
   * `current` is always kept, whatever the verdict. The backend degrades a denied
   * persisted value to the floor on load, so this should not arise; if it ever does,
   * a control rendering no selected chip is a worse failure than one extra row.
   */
  const visible = candidates.filter(value => value === current || !unavailable(value))

  /**
   * Installed === 'missing' is the only verdict that disables. `'unknown'` and an
   * absent row explicitly do not: see the header comment on why an optimistic
   * disable is the more expensive mistake.
   */
  const notInstalled = (value: string) => probe(value)?.installed === 'missing'
  /**
   * Installed on disk, but this gateway process cached its absence and cannot
   * spawn it until restarted. Disabling is right here even though the binary IS
   * present: the click would reach a spawn that fails. This is the one case where
   * a positive install verdict still gates the control.
   */
  const needsRestart = (value: string) => probe(value)?.restart_required === true
  /**
   * Selectability is deliberately NOT part of this: an unselectable agent is absent
   * from `visible` rather than disabled, so the only reasons a rendered chip is dead
   * are ones the user can act on — install the binary, or restart the gateway.
   */
  const disabledOption = (value: string) => notInstalled(value) || needsRestart(value)

  /**
   * A standing caveat about the harness itself, independent of whether it is
   * installed. Unlike `status`, this does not change with the probe.
   *
   * The DEFAULT path is gated: Claude asks, `claude-agent-acp` turns that into
   * `session/request_permission`, and Crew's own approval path decides. What escapes
   * is narrower and worth stating precisely -- a tool ALREADY pre-approved in Claude's
   * own settings never asks at all, because the SDK approves an allow-rule match
   * before consulting the client. Those settings include a `.claude/settings.json`
   * inside the project directory, which is the copy an operator did not write.
   *
   * That is documented, intended Claude behaviour rather than a defect here, but it
   * means the guarantee differs per harness. An operator choosing between harnesses is
   * choosing between governance models, so the panel names the difference instead of
   * letting them find it in a shell command that never asked.
   *
   * Codex carries the OTHER thing a harness can be missing. Its adapter ships a Codex
   * binary of its own, so `installed` answers the whole binary question and a session
   * can still die on the first turn for want of a credential — which the install line
   * then has nothing to say about. The remedy is two-branched (Codex's own sign-in, or
   * a `model_provider` in `~/.codex/config.toml` where the credentials come from
   * elsewhere entirely), so it is stated once rather than inferred from a failure.
   *
   * A caveat and not a probe line, deliberately. Reading those files would make this a
   * measurement, and a measurement here gates the control: `missing` disables the chip,
   * and the checkable paths are not the only ones that authenticate a Codex — an
   * ambient key, a relocated `CODEX_HOME`, a `CODEX_ACP_BIN` adapter with its own
   * configuration. Every one of those is an operator we would have disabled the switch
   * for while they were already signed in, which the probe module names as the more
   * expensive mistake. A standing sentence cannot be wrong in that direction.
   */
  const caveat = (value: string): string => {
    if (value === CLAUDE) return i18nT('pages.developer.agentBackendTab.claude_uses_its_own_permissions')
    if (value === CODEX) return i18nT('pages.developer.agentBackendTab.codex_signs_in_separately')
    return ''
  }

  /**
   * Translated display names for the agents this frontend knows by name.
   *
   * Deliberately NOT the list of agents the panel renders — see `candidates`. An id
   * absent here still gets a row; `nameOf` falls back to the server's `policy_id`.
   */
  const NAME: Record<string, string> = {
    [KIRO]: i18nT('pages.developer.agentBackendTab.kiro_cli'),
    [CLAUDE]: i18nT('pages.developer.agentBackendTab.claude_code'),
    [KAS]: i18nT('pages.developer.agentBackendTab.kas_kiro_agent'),
  }

  const ICON: Record<string, React.ReactNode> = {
    [KIRO]: <Terminal size={14} />,
    [CLAUDE]: <Sparkles size={14} />,
    [KAS]: <Bot size={14} />,
  }

  /**
   * A label for any selectable id, known to this frontend or not.
   *
   * The fallback is the server's `policy_id`, which exists precisely to be a
   * human-readable wire name (`acp_backends.POLICY_ID_BY_BACKEND`) — it is what a
   * governance rule spells, so it is already a word rather than an internal token.
   * Untranslated, and that is the deliberate trade: a registered agent rendering
   * under its policy name is legible, whereas `NAME[value]` returning `undefined`
   * renders a chip with no text at all. A core agent that ships selectable gets a
   * real translated entry above; this keeps a plugin-registered one usable until
   * then.
   *
   * KIRO is the empty string, so the `||` chain must not treat it as absent — it is
   * always in NAME, which is why the lookup comes first.
   */
  const nameOf = (value: string): string => NAME[value] || probe(value)?.policy_id || value

  /** Generic mark for an agent this frontend has no icon for. */
  const iconOf = (value: string): React.ReactNode => ICON[value] ?? <Boxes size={14} />


  /**
   * The one status line a row carries, derived rather than authored per agent.
   *
   * The order is strict, because the reasons are not equally actionable. There is no
   * not-selectable line: such an agent is not rendered at all, so every line here
   * describes something the reader can act on. `missing` comes first because it is the
   * one line that tells the user what to DO, and it names the command only when the
   * server had one to give. `unknown` follows and must never read as missing; it
   * reports a failed check, not an absent binary. Only then do the pre-existing
   * default/experimental lines apply. KIRO is the all-supported descriptor, so it gets
   * that sentence; anything else is not, so it gets `Experimental` rather than a claim.
   */
  const status = (value: string): string => {
    const row = probe(value)
    if (row?.installed === 'missing') {
      const components = row.missing_components.join(', ')
      return row.install_command
        ? i18nT('pages.developer.agentBackendTab.missing_components_with_command', {
            components,
            command: row.install_command,
          })
        : i18nT('pages.developer.agentBackendTab.missing_components', { components })
    }
    if (row?.installed === 'unknown') return i18nT('pages.developer.agentBackendTab.install_check_failed')
    // AFTER the missing/unknown lines and BEFORE the descriptor lines: this row
    // has a positive install verdict, so it would otherwise fall through to
    // `Experimental` and say nothing about why the option is dead.
    if (row?.restart_required)
      return i18nT('pages.developer.agentBackendTab.installed_restart_required')
    if (value === KIRO) return i18nT('pages.developer.agentBackendTab.default_all_features_supported')
    return i18nT('pages.developer.agentBackendTab.experimental')
  }

  return (
    <>
      <ErrorNotice message={saveError} onDismiss={() => setSaveError('')} />
      <SettingsCard>
        <SettingsButtonGroup
          label={i18nT('pages.developer.agentBackendTab.agent_backend')}
          description={i18nT('pages.developer.agentBackendTab.new_sessions_use_this_agent_a_session_that_is_al')}
          configKey={CONFIG_KEY}
          value={current}
          disabled={patchMut.isPending}
          options={visible.map(value => ({
            value,
            label: nameOf(value),
            icon: iconOf(value),
            disabled: disabledOption(value),
            describedById: statusId(value),
          }))}
          onChange={v => patchMut.mutate(v)}
        />
        {/* One line per agent the panel offers — the reader is choosing BETWEEN them,
            so showing only the selected one's status would hide the very comparison
            the control is for. Agents this deployment may not select are absent from
            `visible`, so they carry no line either. */}
        <dl className="mt-2 space-y-1.5">
          {visible.map(value => (
            <div key={value} className="flex gap-2 text-[11px] leading-relaxed">
              <dt className={`shrink-0 font-semibold ${value === current ? 'text-text-strong' : 'text-muted'}`}>
                {nameOf(value)}
              </dt>
              <dd
                id={statusId(value)}
                className={`m-0 ${disabledOption(value) ? 'text-warn' : 'text-muted'}`}
              >
                {status(value)}
                {caveat(value) && <div className="mt-0.5 text-muted">{caveat(value)}</div>}
              </dd>
            </div>
          ))}
        </dl>
        {/* The one thing the per-row lines cannot say. A managed fleet can bound
            this set through the `agent_backend` governance policy, and that policy
            is read once when the gateway starts — so an operator who edits it and
            sees no change here is not looking at a bug. Nothing in the UI can
            detect a not-yet-applied policy edit (that would mean reading the
            trust-root policy on a request path, which the harness-parity rules
            forbid), so stating the semantics is the honest substitute. */}
        <p className="mt-3 text-[11px] leading-relaxed text-muted">
          {i18nT('pages.developer.agentBackendTab.set_is_fixed_at_gateway_start')}
        </p>
      </SettingsCard>
    </>
  )
}
