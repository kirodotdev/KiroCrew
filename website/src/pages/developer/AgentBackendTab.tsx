import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Bot, Sparkles, Terminal } from 'lucide-react'

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
 * Claude Code is the case that makes this worth doing: this build does not include
 * it. Hiding it would imply it does not exist; enabling it would produce a 400 from
 * a control that looked live. It is listed, disabled, and says which it is.
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
   * Not selectable = this build/policy will not serve it. Read from the schema
   * first, since that is the set the PATCH validates against; the probe's own
   * `selectable` is the same fact from the same source, so it is honoured too and
   * the two cannot disagree in a way that lets a dead option look live. Both fall
   * open when absent.
   */
  const unavailable = (value: string) =>
    (selectable ? !selectable.includes(value) : false) || probe(value)?.selectable === false

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
  const disabledOption = (value: string) =>
    unavailable(value) || notInstalled(value) || needsRestart(value)

  const NAME: Record<string, string> = {
    [KIRO]: i18nT('pages.developer.agentBackendTab.kiro_cli'),
    [CLAUDE]: i18nT('pages.developer.agentBackendTab.claude_code'),
    [KAS]: i18nT('pages.developer.agentBackendTab.kas_kiro_agent'),
  }

  /**
   * The one status line a row carries, derived rather than authored per agent.
   *
   * The order is strict, because the reasons are not equally actionable. Not-enabled
   * is checked FIRST and off the schema, so a build that starts shipping an agent
   * stops calling it unavailable without an edit here — and there is no point naming
   * an install for a backend this build would refuse anyway. `missing` comes next
   * because it is the one line that tells the user what to DO, and it names the
   * command only when the server had one to give. `unknown` follows and must never
   * read as missing; it reports a failed check, not an absent binary. Only then do
   * the pre-existing default/experimental lines apply. KIRO is the all-supported
   * descriptor, so it gets that sentence; anything else that is selectable is not,
   * so it gets `Experimental` rather than a claim.
   */
  const status = (value: string): string => {
    if (unavailable(value)) return i18nT('pages.developer.agentBackendTab.not_enabled_in_this_build')
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
          options={[
            {
              value: KIRO,
              label: NAME[KIRO],
              icon: <Terminal size={14} />,
              disabled: disabledOption(KIRO),
              describedById: statusId(KIRO),
            },
            {
              value: CLAUDE,
              label: NAME[CLAUDE],
              icon: <Sparkles size={14} />,
              disabled: disabledOption(CLAUDE),
              describedById: statusId(CLAUDE),
            },
            {
              value: KAS,
              label: NAME[KAS],
              icon: <Bot size={14} />,
              disabled: disabledOption(KAS),
              describedById: statusId(KAS),
            },
          ]}
          onChange={v => patchMut.mutate(v)}
        />
        {/* One line per agent, always all three — the reader is choosing BETWEEN
            them, so showing only the selected one's status would hide the very
            comparison the control is for. */}
        <dl className="mt-2 space-y-1.5">
          {[KIRO, CLAUDE, KAS].map(value => (
            <div key={value} className="flex gap-2 text-[11px] leading-relaxed">
              <dt className={`shrink-0 font-semibold ${value === current ? 'text-text-strong' : 'text-muted'}`}>
                {NAME[value]}
              </dt>
              <dd
                id={statusId(value)}
                className={`m-0 ${disabledOption(value) ? 'text-warn' : 'text-muted'}`}
              >
                {status(value)}
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
