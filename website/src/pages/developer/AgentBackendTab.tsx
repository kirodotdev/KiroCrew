import { useEffect, useRef, useState } from 'react'
import { useInRouterContext, useSearchParams } from 'react-router-dom'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Bot, Boxes, Check, CircleHelp, Sparkles, Terminal, X } from 'lucide-react'

import { acpBackendName, acpProbeBlocksUse, agentChoiceSaved, setupMarkerErrorBody, setupMarkerErrorMessage } from '../../api/acpBackend'
import { api } from '../../api/client'
import type { AcpBackendProbe } from '../../api/client'
import ErrorBoundary from '../../components/ErrorBoundary'
import ErrorNotice from '../../components/ErrorNotice'
import { Badge } from '../../components/ui'
import {
  AgentDetailActions,
  AgentInstallDetail,
  AgentPickerRow,
  AgentStatusBadge,
} from '../../components/agentHarness'
import { SettingsCard } from '../../components/settings'
import { useConfigSchema } from '../../components/settingRef/useConfigSchema'
import { KIRO_SIGN_IN_HIGHLIGHT_ANCHOR } from '../../hooks/useSettingHighlight'
import { i18nT } from '../../i18n/t'
import { clearCachedModels } from '../../providers/adapters/acp'
import { KiroSignInCard } from './KiroSignInCard'
import { KIRO_SIGN_IN_BACKEND } from './kiroSignInLink'

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
 * The tool-approval mechanism that means nothing establishes how a harness asks.
 *
 * The core's own `Routing.UNVERIFIED` value. A harness carrying it can never be
 * selectable — `register_selectable_backend` refuses it — so it is also the
 * reason a known agent is not on offer.
 */
const APPROVAL_UNVERIFIED = 'unverified'

/**
 * DOM id of the status strip alone.
 *
 * The Use button's `aria-describedby`, so the reason a rendered button is dead is
 * what a screen reader gets — the state block alone, not the whole detail it sits in.
 */
const STRIP_ID = 'agent-backend-status'

/**
 * DOM id of the one open detail. ONE id rather than one per backend, because
 * exactly one detail is ever rendered; the checked radio names it through
 * `aria-controls`.
 */
const DETAIL_ID = 'agent-backend-detail'

/** The radio group's `name`: one group, one tab stop, arrows move within it. */
const GROUP_NAME = 'agent-harness'

/**
 * DOM id of the sentence a row's glyph summarises.
 *
 * Referenced as the row's `aria-describedby` and rendered OUTSIDE the row, which is
 * the whole reason it is a second element: text inside the button would join its
 * accessible NAME, so a screen reader would read "Kiro CLI, missing on this machine:
 * kiro-cli" as the row's identity and every row would be named after its own
 * problem. As a description it arrives after the name, which is what a description
 * is for.
 */
const rowStatusId = (value: string) => `agent-backend-row-status-${value || 'kiro'}`

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
 * Settings > Agent Harness — pick which agent runs a session. (It was the
 * Developer page's Agent Backend tab until first-run setup started offering
 * agents other than Kiro CLI; the component keeps its old name and directory.)
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
 * ## Why it is a list and a detail, and not one card per harness
 *
 * Every harness's whole capability card used to render stacked down the page. That
 * is the right amount of information and the wrong amount at once: eight harnesses
 * times fifteen capability lines is a wall, and the reader's question is about one
 * harness at a time. So the harnesses are a LIST and the card belongs to whichever
 * row is checked. Exactly one card is ever rendered, which is also what let the
 * capability list come OUT of the disclosure it used to need: it was collapsed
 * because eight open cards buried the control, and with one card there is nothing to
 * bury.
 *
 * ## Why it looks like first-run setup
 *
 * The same person meets two agent pickers: the "Use other coding agents" section of
 * first-run setup, and this tab. An earlier revision of this tab was a tablist in a
 * grey strip with a detail pane beside it — truncated names, tiny status words, a
 * "Kiro sign-in" card the full width of the page under it whatever agent was in use.
 * It is now built from the SAME pieces as the setup picker
 * (`components/agentHarness/`): radio rows with the harness's full name and a
 * status badge, the checked row's detail opening directly under it, and one
 * accent "Use <agent>" button in that detail. Radios rather than tabs because
 * that is what the control is — pick one of several — and because the browser
 * then supplies the keyboard (one tab stop, arrows move check and focus together)
 * that the tablist had to reimplement.
 *
 * Each row states TWO facts, not one: an "In use" word for the backend that is
 * running, and a badge for READINESS. They are independent and an operator needs
 * both at once — a harness that is the one running AND missing its binary has to
 * read as both, and one mark with a precedence between them can only ever show the
 * winner.
 *
 * This layout does give something up, and the trade is worth stating rather than
 * leaving to be discovered. Every card on the page at once meant two harnesses'
 * capability sets could be read side by side; now that comparison costs moving
 * between two rows. It is accepted because the comparison was already poor — the sets
 * were fifteen lines apart in a vertical stack, never aligned in columns — and
 * because choosing a backend is a decision an operator makes rarely and reverses
 * cheaply, while scanning WHICH harnesses exist and which are usable is what they
 * open this panel to do. A real side-by-side would be a comparison table, which is a
 * different control.
 *
 * ## Checking a row is not selecting an agent, and that is the load-bearing part
 *
 * Checking a row opens its detail and nothing else. The active backend changes in
 * exactly one place — the **Use** button in the detail — so reading about a
 * harness can never switch to it. That separation is why a harness this machine
 * cannot run still gets a row: under the old control an unselectable harness had
 * no chip, so the harnesses an operator most needed to read about were the ones
 * the page had least room for. A row is free; a switch is not.
 *
 * The two states are carried by different ARIA, deliberately: the radio's
 * `checked` is which row you are LOOKING at, `aria-current` is which backend is
 * RUNNING. A screen reader gets the same two facts the row shows.
 *
 * ## Where the Kiro sign-in lives
 *
 * Inside the KAS row's detail, and nowhere else. The identity it stores is
 * consumed by the KAS relay alone, so a sign-in card under the whole list read as
 * a step every user had to take — including one running Claude Code, for whom it
 * does nothing. The chat's "Sign in to Kiro" link still lands on it:
 * `SignInDeepLink` reads the same `?highlight=` the Settings highlight hook does
 * and checks the KAS row, so the anchor mounts and rings.
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
 * second fact per backend, and the two compose: the switch is dead when this build
 * will not serve it OR this machine is missing it.
 *
 * The probe has THREE answers and the third is load-bearing. `unknown` means the
 * check itself failed, and it leaves the switch ENABLED — collapsing it onto
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
 * ## What the detail says about the harness, and where those words come from
 *
 * A reader choosing between agents is choosing between capability sets, so the
 * detail carries a CARD: one line per capability, marked available, not available or
 * not measured, plus the notes that hold and the one line about tool approval.
 *
 * Two things on it are never behind the disclosure: how the agent is made to ask
 * before it runs a tool, and the SECURITY notes, which say which layer confines
 * it and whether Crew hands it Crew's own credential. Those are the facts an
 * operator is choosing between, and a fact behind a closed disclosure is a fact
 * they do not see. Which notes are security notes is the SERVER's
 * classification, sent as its own list, so this file cannot promote or bury one.
 *
 * Not one word of it is authored per agent. The server projects every line from
 * the capability memberships the core already declares
 * (`agent_sdk/backend_cards.py`) and sends them as ids; this file holds a LABEL
 * per id. That is the whole reason the card can be translated at all: a label
 * belongs to a CAPABILITY, so it is written once and every agent reuses it, and a
 * new agent renders a complete detail with no edit here and no locale edit either.
 * A new LINE is what costs thirteen locale files.
 *
 * An earlier revision instead wrote a prose sentence per agent claiming what each
 * one supports — sandboxing, shared processes, mid-turn steer, subagent progress.
 * Those claims were not measured anywhere; they were asserted here, in the view
 * layer, where nothing can contradict them. They were wrong in the ways unmeasured
 * claims usually are. The card is the opposite arrangement: every mark on it is a
 * membership some other file had to justify with evidence, and this file cannot
 * state anything the core does not already claim.
 *
 * The card has three levels: available, not available, and NOT MEASURED. The first
 * two are the server's projection over membership, and a set carries one bit, so
 * "does it differently" and "cannot" reach this file as the same absence. The third
 * is the server's own declaration rather than a projection — it arrives per line as
 * `measured: false` plus a reason code, for the cells where Crew has no answer yet
 * instead of a negative one (`/compact` on a harness nobody has driven).
 *
 * It gets its own glyph and its own word, never the tick and never the cross, and it
 * counts as neither half of "supports N of M": a reader who cannot tell "no" from
 * "nobody looked" cannot tell which cell a measurement would pay for, which is the
 * whole reason the state exists. `available` is false on such a line too, so this
 * file cannot render a promise even where it ignores the flag — an older panel
 * against a newer gateway shows the honest cross.
 *
 * The one genuinely graded fact — how the harness is made to ask before running a
 * tool — arrives as the core's own five-mechanism enum and is rendered from it.
 *
 * ## The MCP half, which a capability set cannot answer
 *
 * The lines above say what the harness can DO. They cannot say what happens to the
 * user's own AGENT FILE on the way to it, and that is where these harnesses differ
 * most: on one, switching a single tool off narrows that tool; on another it
 * withholds the whole server, Crew's own control plane included. The permission
 * mode a spec asks for is honoured on one and overridden on another. Hooks reach
 * one and no other.
 *
 * Every one of those is a declared, defensible ruling that already existed in
 * `providers/mirrors/registry.py` and reached no reader. The server projects it
 * (`agent_sdk/backend_mcp_ability.py`) as a kind, a per-tool deny reach, and two
 * lists of spec concerns — withheld, and no-channel-yet — and this file holds a
 * label per KIND, per REACH and per CONCERN, never per agent.
 *
 * Two of its facts need no click. The KIND rides in the disclosure's own summary,
 * because it decides whether the rest matters. And the whole-server tool-off cost
 * renders OUTSIDE the disclosure beside the security notes, under the same rule they
 * follow: it is the one fact here an operator meets by accident, since switching a
 * tool off says nothing about servers until it removes one. The other two reaches
 * stay inside, where reassurance belongs.
 *
 * It is ADVISORY: per-tool MCP deny is not a requirement on every agent, so the
 * card's job is to say which form a reader is getting before a session runs, not to
 * refuse the selection.
 *
 * The status strip keeps its own job: it says whether this harness is live on this
 * machine, names what is absent, and prints the command that installs it. Those are
 * measurements this gateway took, not claims about capability.
 *
 * Deliberately NOT under `pages/settings/`: `gen-settings-registry.mjs` scans that
 * directory, and indexing an agent switch into Settings search would advertise it
 * as an ordinary preference — it changes which agent binary runs.
 */
export function AgentBackendTab() {
  const qc = useQueryClient()
  /**
   * The one message for "the thing you just pressed did not work".
   *
   * Shared by the switch and the re-check rather than one state each. Both are
   * failures of an action the operator took, `ErrorNotice` is the panel's single
   * recovery surface, and only one of these actions can be in flight at a time — two
   * notices stacked would be two dismissals for one problem. A hand-rolled `<span>`
   * beside the button is what `errors-use-error-notice` forbids, and rightly: it
   * offers no dismissal and no consistent place to look.
   */
  const [actionError, setActionError] = useState('')
  /**
   * Which row's detail is on screen, or `null` for "follow the active backend".
   *
   * `null` rather than seeding it with `current`: the config is still in flight on
   * first render, so seeding would pin the highlight to a guess and then leave it
   * there once the real value arrived. Resolved every render by `shown` instead.
   */
  const [highlighted, setHighlighted] = useState<string | null>(null)
  /**
   * A failure of one of the STRIP's own controls -- the re-check, or the copy.
   *
   * Separate from `actionError` because it renders in a different place, and the place
   * is the point: an error about this harness's probe belongs beside this harness's
   * buttons, not at the top of a panel whose other seven rows are fine.
   *
   * KEYED BY BACKEND, and that is the whole reason it is an object rather than a
   * string. The request is asynchronous and the highlight is not: press Check again
   * on A, move to B, and A's rejection arrives with B on screen -- an unkeyed message
   * then renders under B, telling the reader that B could not be checked when nothing
   * about B was ever asked. Clearing on row change does not fix it either, because the
   * rejection lands AFTER the move. Holding the id the failure belongs to makes the
   * render a match rather than a race, and it also keeps the message: re-highlight A
   * and the error it earned is still there.
   */
  const [stripError, setStripError] = useState<{ backend: string; message: string } | null>(null)
  const schema = useConfigSchema()
  const inRouter = useInRouterContext()

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

  /**
   * The cache work a successful switch does, factored out because a 503
   * `setup_marker_write_failed` that DID commit the choice (`config_saved:
   * true`) has to run exactly the same steps -- the agent on disk moved, so a
   * cache still holding the old one keeps badging the wrong harness and offers
   * the old backend's models.
   *
   * `resetQueries`, not `invalidateQueries`: invalidate keeps the OLD backend's
   * rows on screen until the refetch lands, and a cold `--list-models` spawn can
   * take the gateway's full 10s. A pick in that window writes an id the new
   * backend rejects. Reset drops the data to `undefined` first, so every picker
   * shows the auto-only placeholder for those seconds, then refetches. Drop the
   * last-good localStorage list FIRST for the same reason: a failing first fetch
   * must degrade to auto-only, not to the old backend's ids.
   */
  const applyBackendSwitchSideEffects = () => {
    qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
    // The model list is the NEW backend's now. `/api/models` re-reads
    // `agent.acp_backend` on every call, so the server side needs no restart;
    // only the frontend cache did, because `['available-models']` is refetched
    // in exactly one other place — a spawned session (`useWebSocket`'s
    // `activity_event`) — and the global `staleTime: Infinity` plus the
    // self-heal poll stopping after one live success mean nothing else ever
    // re-asks. That is why the picker looked like it needed a gateway restart:
    // the restart was just the next session spawn.
    clearCachedModels()
    qc.resetQueries({ queryKey: ['available-models'] })
  }

  const patchMut = useMutation({
    mutationFn: (value: string) => api.patchConfig(CONFIG_KEY, value),
    onSuccess: () => {
      setActionError('')
      applyBackendSwitchSideEffects()
    },
    // No optimistic write and no local mirror of the value: the list reads
    // straight from the query, so a rejected PATCH needs no revert — the cache was
    // never moved off the server's answer.
    //
    // A 503 `setup_marker_write_failed` carrying `config_saved: true` is NOT a
    // failed save: `agent.acp_backend` is on disk and only the first-run marker
    // is not. So the switch's side effects run just as on success, and the
    // server's marker-write message is shown in place of the generic save
    // failure -- badging the new agent, not the old one.
    onError: (error: unknown) => {
      if (agentChoiceSaved(error)) {
        applyBackendSwitchSideEffects()
        setActionError(
          setupMarkerErrorMessage(error)
            ?? i18nT('pages.developer.agentBackendTab.could_not_save_the_agent_backend'),
        )
        return
      }
      setActionError(i18nT('pages.developer.agentBackendTab.could_not_save_the_agent_backend'))
    },
  })

  /**
   * Replace ONE backend's row in the list query, or append it when the list has
   * no row for it yet. Shared by the re-check's success and its
   * `setup_marker_write_failed` 503 (which carries the same fresh row): the
   * server sends the re-checked row in the shape the list holds, so replacing the
   * one row is both cheaper than a refetch and the only way the fresh verdict
   * survives inside the poll's TTL window.
   */
  const spliceProbeRow = (row: AcpBackendProbe) =>
    qc.setQueryData<{ backends: AcpBackendProbe[] }>(['acpBackends'], prev =>
      prev
        ? {
            backends: prev.backends.some(b => b.id === row.id)
              ? prev.backends.map(b => (b.id === row.id ? row : b))
              : [...prev.backends, row],
          }
        : { backends: [row] },
    )

  /**
   * Re-take ONE backend's verdict, with this gateway's cached absence dropped first.
   *
   * The remedy for the state the panel could previously only describe: an operator
   * runs the install command printed beside the row, and the row keeps saying the
   * component is absent — because the running gateway resolved that absence once and
   * caches it for the life of the process. `restart_required` is the honest report of
   * that, and a restart is a real cost for a fact only this process still believes.
   *
   * The answer is SPLICED into the list query rather than invalidating it, for the
   * same reason the poll interval matches the server's TTL: a full refetch would
   * re-read seven other rows from a cache that has not moved, and would answer this
   * backend's row from that cache too if the write landed inside the TTL window. The
   * server sends the re-checked row in the shape the list holds, so replacing the one
   * row is both cheaper and the only way the fresh verdict survives.
   */
  const recheckMut = useMutation({
    // The poll is cancelled BEFORE the request goes out, not after it returns. A GET
    // that was already in flight carries the pre-install list, and react-query would
    // write that answer into the cache whenever it resolved -- including after the
    // splice below -- putting the stale row back and killing the switch again until
    // the next poll. Cancelling marks the in-flight fetch's result as unwanted, so
    // the splice is the last write.
    onMutate: () => qc.cancelQueries({ queryKey: ['acpBackends'] }),
    mutationFn: (value: string) => api.acpBackendRecheck(value),
    onSuccess: ({ backend }) => {
      // Only this harness's failure is answered by this harness's success. Another
      // row's unread error is not this request's to discard.
      setStripError(prev => (prev && prev.backend === backend.id ? null : prev))
      spliceProbeRow(backend)
    },
    // Surfaced, unlike a failed probe GET. A failed poll is absent information the
    // user did not ask for; a failed re-check is a button they pressed, and silence
    // would read as "checked, still missing".
    // The failing backend comes from the mutation's own variables, not from `shown`:
    // `shown` is whatever row is highlighted when the rejection arrives, which is the
    // bug this keying exists for.
    //
    // A 503 `setup_marker_write_failed` carries the fresh probe row: the re-probe
    // succeeded and only the marker write did not, so the row is spliced in exactly
    // as on success and the server's marker-write message is shown in place of the
    // generic install-check-failed text.
    onError: (error: unknown, backend) => {
      const row = setupMarkerErrorBody(error)?.backend
      const markerMessage = setupMarkerErrorMessage(error)
      if (row && markerMessage) {
        spliceProbeRow(row)
        setStripError({ backend, message: markerMessage })
        return
      }
      setStripError({
        backend,
        message: i18nT('pages.developer.agentBackendTab.install_check_failed'),
      })
    },
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
   * then unknown, and defaulting paints Kiro CLI as the active harness, so an
   * operator running KAS is shown the wrong agent by a control that looks live.
   * Offer the retry instead of guessing.
   */
  if (cfgQ.isError) {
    return (
      <div className="py-12 text-center">
        {/* A `useQuery` failure, so it belongs to `ErrorNotice` rather than to a
            hand-written line: the journal holds the route, endpoint and status this
            component never sees, and `askAgent` hands that to the agent. Nothing is
            lost by navigating away -- this branch renders instead of the panel, so
            there is no draft and not even a highlight to keep. */}
        <ErrorNotice
          message={i18nT('pages.developer.agentBackendTab.could_not_load_the_agent_backend')}
          askAgent
        />
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
   * `undefined` while the schema is in flight — the switch stays enabled rather
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
   * Sorted rather than left in arrival order: the two kiro-family harnesses first —
   * KIRO because it is the default and the floor, then KAS — and everything else by
   * `policy_id`, which is the order the probe endpoint already sorts by. Set iteration
   * order would otherwise follow whichever query resolved first and reshuffle the
   * list between renders.
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
    // one install belong adjacent at the head of the list. Under `policy_id` alone it
    // sorts on 'k' and lands behind every adapter whose name happens to start earlier
    // ('claude', 'codex'), which reads to the operator as a rank rather than an
    // alphabet.
    if (a === KAS) return -1
    if (b === KAS) return 1
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
   * The row whose detail is on screen.
   *
   * Derived rather than stored, so the highlight cannot outlive its row: the listed
   * set moves as the schema and probe queries answer, and a stored id that dropped
   * out of it would render an empty pane. Falls back to the ACTIVE backend, which is
   * both the useful default and the one id `rows` always contains.
   */
  const shown =
    highlighted !== null && rows.includes(highlighted)
      ? highlighted
      : rows.includes(current)
        ? current
        : (rows[0] ?? current)

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
   * A standing caveat about the harness itself, independent of whether it is
   * installed. Unlike the status strip, this does not change with the probe.
   *
   * ## Tool gating, which is stated here
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
   * letting them find it in a shell command that never asked. It is a TOOL-GATING
   * disclosure and not an auth one, so nothing below replaces it.
   *
   * The card's tool-approval line comes CLOSE to stating this from data — Claude's
   * routing is the declared-but-unenforced mechanism, and the label says the setting
   * cannot be read back — and it does not carry the part that matters most here,
   * which is WHERE the pre-approval an operator did not write can come from. Until
   * that is expressed as data, this sentence stays.
   *
   * Which is why this returns a LIST rather than one string. Claude is the harness
   * that carries both -- its tool gating has the caveat above AND it signs in through
   * its own credential file -- and an earlier revision returned early on the gating
   * line, so the one harness with two facts to state showed one of them.
   *
   * ## Signing in, which the SERVER states
   *
   * `auth.sign_in_remedy` arrives as a finished sentence and is rendered verbatim;
   * `auth.signs_in_separately` decides whether it is rendered at all, because a
   * harness that authenticates through Crew's own identity store has no separate
   * sign-in to finish. Absent `auth` says nothing, like every other absent probe
   * field.
   *
   * It is NOT translated, and that is the trade rather than an oversight. A
   * translated per-harness sentence is, by construction, a per-harness edit to
   * thirteen locale files, so the harness that needs the sentence most -- one an
   * edition registered and this frontend has never heard of -- is exactly the one
   * that would get no sentence at all. An untranslated remedy that is CORRECT beats
   * a translated one nobody adds.
   *
   * This also finishes the pattern the row list already follows: `candidates` is
   * a union of server answers rather than ids written here, and `nameOf` falls back
   * to the wire id when this frontend has no translated name. The `value === CODEX`
   * branch this replaces was the panel's last per-harness literal. Now the server
   * names a harness and states its remedy, and adding one costs no edit here.
   *
   * Still a caveat and not a probe line, deliberately. A measurement here would gate
   * the control -- `missing` disables the switch -- and the paths that authenticate a
   * harness are not all checkable: an ambient key, a relocated config home, an
   * adapter carrying its own configuration. Each of those is an operator whose switch
   * we would have disabled while they were already signed in, which the probe module
   * names as the more expensive mistake. A standing sentence cannot be wrong in that
   * direction.
   */
  const caveats = (value: string): string[] => {
    const lines: string[] = []
    if (value === CLAUDE)
      lines.push(i18nT('pages.developer.agentBackendTab.claude_uses_its_own_permissions'))
    const auth = probe(value)?.auth
    if (auth?.signs_in_separately) lines.push(auth.sign_in_remedy)
    return lines
  }

  const ICON: Record<string, React.ReactNode> = {
    [KIRO]: <Terminal size={14} />,
    [CLAUDE]: <Sparkles size={14} />,
    [KAS]: <Bot size={14} />,
  }

  /**
   * One label per CAPABILITY, keyed by the id the server sends.
   *
   * Per capability and never per agent: that is what makes a new agent cost no
   * locale edit, and it is the difference from `auth.sign_in_remedy`, which is
   * per-agent prose and therefore stays untranslated. An id absent here is SKIPPED
   * — a raw `private_memory_mcp` in front of a reader is worse than one line fewer
   * — which is the opposite of `nameOf`'s fallback, because a row with no text at
   * all is worse than a policy id.
   */
  const CAPABILITY_LABEL: Record<string, string> = {
    crew_tools: i18nT('pages.developer.agentBackendTab.card_crew_tools'),
    member_thread_tools: i18nT('pages.developer.agentBackendTab.card_member_thread_tools'),
    member_saved_agent: i18nT('pages.developer.agentBackendTab.card_member_saved_agent'),
    private_member_sessions: i18nT('pages.developer.agentBackendTab.card_private_member_sessions'),
    side_chat_tools: i18nT('pages.developer.agentBackendTab.card_side_chat_tools'),
    subagent_continuation: i18nT('pages.developer.agentBackendTab.card_subagent_continuation'),
    mid_turn_steer: i18nT('pages.developer.agentBackendTab.card_mid_turn_steer'),
    manual_compact: i18nT('pages.developer.agentBackendTab.card_manual_compact'),
    reasoning_effort: i18nT('pages.developer.agentBackendTab.card_reasoning_effort'),
    model_switch: i18nT('pages.developer.agentBackendTab.card_model_switch'),
    markdown_agents: i18nT('pages.developer.agentBackendTab.card_markdown_agents'),
  }

  /**
   * One label per REASON a line is unmeasured, keyed by the server's reason code.
   *
   * Keyed by reason and not by agent, for the same trade as the capability labels: a
   * reason is a class of missing evidence, so it is phrased once and every agent
   * reuses it. A code this frontend has no label for renders the line with its glyph
   * and its word and no clause — the same rule as an unlabelled capability id, and
   * for the same reason: a raw `no_driven_capture` in front of a reader says less
   * than nothing.
   */
  const UNMEASURED_REASON_LABEL: Record<string, string> = {
    no_driven_capture: i18nT('pages.developer.agentBackendTab.card_unmeasured_no_driven_capture'),
  }

  /**
   * One label per note, for BOTH note lists. The server keeps them in two lists
   * because they render in two places, and the ids are disjoint, so one map
   * cannot confuse them. Same rule as the capability labels above: keyed by
   * capability, skipped when this frontend has no label for the id.
   *
   * These are stated only when they HOLD, so each reads as a fact rather than as a
   * mark on a scale — the server sends the ids that apply and nothing else.
   */
  const NOTE_LABEL: Record<string, string> = {
    crew_sandbox_stands_down: i18nT('pages.developer.agentBackendTab.note_crew_sandbox_stands_down'),
    refuses_unclassified_tools: i18nT('pages.developer.agentBackendTab.note_refuses_unclassified_tools'),
    host_credential_to_child: i18nT('pages.developer.agentBackendTab.note_host_credential_to_child'),
    pod_home_relocated: i18nT('pages.developer.agentBackendTab.note_pod_home_relocated'),
    own_credential_store: i18nT('pages.developer.agentBackendTab.note_own_credential_store'),
    keeps_own_chat_record: i18nT('pages.developer.agentBackendTab.note_keeps_own_chat_record'),
    harness_model_list: i18nT('pages.developer.agentBackendTab.note_harness_model_list'),
    crew_command_channel: i18nT('pages.developer.agentBackendTab.note_crew_command_channel'),
  }

  /**
   * One label per tool-approval MECHANISM — the core's own `Routing` values.
   *
   * The single graded line on the card, and the only one that is not a boolean,
   * because the source data is not one either: `Routing` already distinguishes a
   * guarantee that holds by construction, one applied and read back, one written
   * and unconfirmable, and one that is not established at all. Keyed by mechanism
   * rather than by agent, so a harness declaring an existing mechanism needs no
   * label of its own.
   */
  const APPROVAL_LABEL: Record<string, string> = {
    agent_spec: i18nT('pages.developer.agentBackendTab.approval_agent_spec'),
    session_config: i18nT('pages.developer.agentBackendTab.approval_session_config'),
    seeded_settings: i18nT('pages.developer.agentBackendTab.approval_seeded_settings'),
    verified_seeded_settings: i18nT('pages.developer.agentBackendTab.approval_verified_seeded_settings'),
    verified_gate_extension: i18nT('pages.developer.agentBackendTab.approval_verified_gate_extension'),
    [APPROVAL_UNVERIFIED]: i18nT('pages.developer.agentBackendTab.approval_unverified'),
  }


  /**
   * The deny-reach RULE for one agent, or `''` where it declares no reach.
   *
   * One sentence, same shape on every agent that has one: *turning off one MCP tool
   * stops every tool on the same server*. It is on the card because it is a RISK the
   * reader meets by accident — switching a tool off is an ordinary action that says
   * nothing about servers — and it is worded as a rule so the exception below can be
   * an exception to something.
   *
   * Only the reaches that can cost a whole server get a line. `settings-file` stops
   * the tool it names and nothing else, which costs the reader nothing to know.
   */
  const mcpDenyRule = (value: string): string => {
    if (!mcpCanCostWholeServer(value)) return ''
    return i18nT('pages.developer.agentBackendTab.mcp_deny_rule', { name: nameOf(value) })
  }

  /**
   * The EXCEPTION to that rule, where the agent has one, or `''`.
   *
   * `per-call` is the one reach that spares Crew's own servers: it refuses the call
   * itself there, so `kirocrew-core` keeps working tool by tool and the session can
   * still reply. Rendered directly under the rule and labelled as its exception,
   * because two sentences that qualify each other without saying so read as a
   * contradiction.
   */
  const mcpDenyException = (value: string): string => {
    if (probe(value)?.mcp?.per_tool_deny !== 'per-call') return ''
    return i18nT('pages.developer.agentBackendTab.mcp_deny_exception', {
      name: nameOf(value),
    })
  }

  /**
   * The settings in the reader's agent config file that will not take effect here.
   *
   * ONE list, whatever the core ruled: a withhold is a settled decision and a
   * no-channel is an open gap, which is a distinction for whoever maintains the
   * mirror. The reader's question is the same either way — does the thing I wrote in
   * my file happen — and each setting's own sentence answers it.
   *
   * An id with no phrase here renders as itself rather than being dropped, so a
   * setting added to the core does not silently vanish from the card.
   */
  const mcpIneffective = (value: string) => probe(value)?.mcp?.ineffective ?? []

  /**
   * One label per SPEC CONCERN — the thing in the user's agent file, not the agent.
   *
   * Keyed by the concern, and an id with no label here renders as ITSELF rather than
   * being dropped, so a setting added to the core does not vanish from the card. Which concerns reach a card at
   * all is the SERVER's classification (`agent_sdk/backend_mcp_ability.py` records
   * the reason per concern it leaves off), so this file cannot add one it thinks a
   * reader wants.
   */
  const MCP_CONCERN_LABEL: Record<string, (name: string) => string> = {
    mcp_servers: () => i18nT('pages.developer.agentBackendTab.mcp_concern_mcp_servers'),
    tool_allowlist: () => i18nT('pages.developer.agentBackendTab.mcp_concern_tool_allowlist'),
    denied_tools: () => i18nT('pages.developer.agentBackendTab.mcp_concern_denied_tools'),
    auto_approve: (name: string) =>
      i18nT('pages.developer.agentBackendTab.mcp_concern_auto_approve', { name }),
    // Named rather than "this agent", under the same grounding rule the kind and deny
    // phrases follow: the reader met agents inside agent config files and could not
    // tell the two senses apart. A function per entry rather than a string, so the
    // harness name is resolved when the row renders -- the record is declared above
    // `nameOf`, and a value that read it eagerly would be a use-before-declaration.
    permission_mode: (name: string) =>
      i18nT('pages.developer.agentBackendTab.mcp_concern_permission_mode', { name }),
    // Named, like the permission-mode line beside it: "your agent config file" and "this
    // agent" in one sentence left a reader unable to tell whether the two senses of agent
    // were the same thing, and the harness's own display name is the one spelling that
    // cannot be read as the FILE.
    model: (name: string) => i18nT('pages.developer.agentBackendTab.mcp_concern_model', { name }),
    model_allowlist: (name: string) =>
      i18nT('pages.developer.agentBackendTab.mcp_concern_model_allowlist', { name }),
    hooks: () => i18nT('pages.developer.agentBackendTab.mcp_concern_hooks'),
  }

  /** One ineffective-setting line: this frontend's phrase, or the setting's own id. */
  const mcpSettingLine = (value: string, id: string): string =>
    MCP_CONCERN_LABEL[id]?.(nameOf(value)) ?? id

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
   * KIRO is the empty string, so the `||` chain must not treat it as absent — it is
   * always named by the shared helper, before the policy-id fallback.
   */
  const nameOf = (value: string): string => acpBackendName({ id: value, policy_id: probe(value)?.policy_id })

  /** Generic mark for an agent this frontend has no icon for. */
  const iconOf = (value: string): React.ReactNode => ICON[value] ?? <Boxes size={14} />

  /**
   * The card's capability lines for one agent, dropping ids with no label here.
   *
   * Empty when the payload carried none — an older gateway, a 403, a query in
   * flight — and the detail then renders no capability list at all, like every other
   * absent probe field.
   */
  const capabilityLines = (value: string) =>
    (probe(value)?.capabilities ?? []).filter(line => CAPABILITY_LABEL[line.id])

  /**
   * The SECURITY notes that hold for one agent: which layer confines it, whether
   * Crew hands over its own credential, how an unclassifiable approval is
   * answered. Rendered outside the disclosure, so a confinement boundary is
   * never one click away from a reader comparing agents.
   */
  const securityNotes = (value: string) =>
    (probe(value)?.security_notes ?? []).filter(id => NOTE_LABEL[id])

  /**
   * The sentence for an unmeasured line's reason, or `''` where this frontend has no
   * label for the code the server sent.
   */
  const unmeasuredReason = (code: string | undefined): string =>
    (code && UNMEASURED_REASON_LABEL[code]) || ''

  /** The where-it-lives notes that hold, dropping ids with no label here. */
  const noteLines = (value: string) =>
    (probe(value)?.operator_notes ?? []).filter(id => NOTE_LABEL[id])

  /** The tool-approval sentence, or `''` when the payload named no mechanism. */
  const approvalLine = (value: string): string => {
    const mechanism = probe(value)?.tool_approval
    return (mechanism && APPROVAL_LABEL[mechanism]) || ''
  }



  /**
   * Whether a tool-off on this agent can cost a WHOLE server rather than the tool.
   *
   * The SERVER's classification, read off the payload rather than re-derived here.
   * `agent_sdk/backend_mcp_ability.COSTS_WHOLE_SERVER` holds which reaches cost a
   * whole server, beside the record of which concerns reach a card at all, and a
   * completeness test holds it against the vocabulary — so a reach added to the core
   * arrives already classified instead of rendering as ordinary until someone edits
   * this file. It holds for two of the three today: `whole-server`, and the case that
   * hid, `per-call`, which stays per tool on Crew's OWN servers and withholds any
   * other server whole.
   *
   * `false` when the payload carried no flag — an older gateway, a 403, a query in
   * flight — which renders the deny line inside the disclosure, where it sat before
   * any of this. Compared against `true` rather than coerced, so a reach this
   * frontend has no label for is never promoted on the strength of a truthy string.
   */
  const mcpCanCostWholeServer = (value: string): boolean =>
    probe(value)?.mcp?.costs_whole_server === true


  /**
   * The one status sentence a harness carries, derived rather than authored per agent.
   *
   * The order is strict, because the reasons are not equally actionable. A
   * build-excluded agent comes first: nothing about installing or re-checking is
   * worth telling someone about an option this build will never offer, and the
   * detail's tool-approval line already says why. `missing` comes next because
   * it is the line that tells the user what to DO, and it names the components
   * without the command — the command gets its own copyable block, so folding it
   * into a sentence would put the one string an operator has to run somewhere they
   * cannot click. `unknown` follows and must never read as missing; it reports a
   * failed check, not an absent binary. Only then do the default/experimental lines
   * apply. KIRO is the all-supported descriptor, so it gets that sentence; anything
   * else is not, so it gets `Experimental` rather than a claim.
   *
   * Rendered as the row's screen-reader text as well as in the strip, which is what
   * lets the row's single glyph stay a glyph: the state is a WORD somewhere for
   * every row, not a shape a reader has to decode.
   */
  const status = (value: string): string => {
    const row = probe(value)
    if (buildExcluded(value)) return i18nT('pages.developer.agentBackendTab.not_offered_by_this_build')
    if (row?.installed === 'missing')
      return i18nT('pages.developer.agentBackendTab.missing_components', {
        components: row.missing_components.join(', '),
      })
    // The check failed, and the SECOND half of that sentence is what UX blocked on:
    // the switch deliberately stays live here, and a bright Use button under a
    // "could not check" line reads as a mistake the reader refuses to touch. The
    // reasoning was in this file's own header, where no user will ever see it.
    if (row?.installed === 'unknown')
      return `${i18nT('pages.developer.agentBackendTab.install_check_failed')} ${i18nT(
        'pages.developer.agentBackendTab.can_still_switch',
      )}`
    // AFTER the missing/unknown lines and BEFORE the descriptor lines: this row
    // has a positive install verdict, so it would otherwise fall through to
    // `Experimental` and say nothing about why the switch is dead.
    // Names the cheap remedy, which is the button directly beside this line, before
    // the gateway restart. The SAME sentence first-run setup shows for this state:
    // the two screens are built from one picker, and two remedies for one state
    // ("Settings > About" here, "that host" there) read as two different problems.
    if (row?.restart_required)
      return i18nT('components.kiroPrerequisiteGate.agent_restart_required_detail', {
        name: nameOf(value),
      })
    if (value === KIRO) return i18nT('pages.developer.agentBackendTab.default_all_features_supported')
    return i18nT('pages.developer.agentBackendTab.experimental')
  }

  /**
   * Highlight *value*. Nothing else: the strip error is keyed by backend.
   *
   * An earlier shape cleared the error here, which read as a fix and was not one --
   * a rejection that arrives after the move still paints under the row moved to. The
   * keying in `stripError` is what makes the render match the harness that failed,
   * and it also means a message survives a look at another row.
   */
  const highlightRow = (value: string) => {
    setHighlighted(value)
  }

  /**
   * The probe row for the harness on screen, and the two flags the action row
   * reads. `busy` covers BOTH mutations: a switch in flight disables Check again
   * and a re-check in flight disables Use, because either one changes what the
   * other would act on.
   */
  const shownProbe = probe(shown)
  const busy = patchMut.isPending || recheckMut.isPending
  // Every state the re-check can help with, and only those. A build-excluded harness
  // is not one: nothing this machine holds is why it is not on offer, so a button
  // that re-measured the machine would answer a question nobody asked.
  const canRecheck =
    !buildExcluded(shown) &&
    (notInstalled(shown) || needsRestart(shown) || shownProbe?.installed === 'unknown')

  /**
   * The detail's lead: what state this harness is in, in the words first-run
   * setup uses where the two screens share a state, and in this panel's own
   * where they do not.
   *
   * The order is strict, because the reasons are not equally actionable. A
   * build-excluded agent comes first: nothing about installing or re-checking is
   * worth telling someone about an option this build will never offer, and the
   * tool-approval line below already says why. `missing` comes next because it
   * is the block that tells the user what to DO — the install command, copyable,
   * and what to press afterwards. `unknown` follows and must never read as
   * missing; it reports a failed check, and says the switch stays live, because
   * a bright Use button under a bare "could not check" line reads as a mistake.
   * `restart_required` names the cheap remedy, the Check again button beside it,
   * before the gateway restart — in first-run setup's own sentence, so the two
   * screens built from one picker give one remedy for one state. Only then do
   * the descriptor lines apply:
   * KIRO is the all-supported default; anything else installed is ready to use
   * and marked Experimental rather than given a claim.
   *
   * With no probe answer at all (a 404, a 403, a query in flight) the block says
   * only what it can: the default line for Kiro CLI, Experimental for the rest —
   * never "installed and ready", which nobody measured.
   */
  const lead = (value: string) => {
    const row = probe(value)
    const name = nameOf(value)
    if (buildExcluded(value))
      return (
        <p className="text-[13px] leading-relaxed text-text">
          {i18nT('pages.developer.agentBackendTab.not_offered_by_this_build')}
        </p>
      )
    if (row?.installed === 'missing') return <AgentInstallDetail name={name} probe={row} />
    if (row?.installed === 'unknown')
      return (
        <p className="text-[13px] leading-relaxed text-text">
          {i18nT('pages.developer.agentBackendTab.install_check_failed')}{' '}
          {i18nT('pages.developer.agentBackendTab.can_still_switch')}
        </p>
      )
    if (row?.restart_required)
      return (
        <p className="text-[13px] leading-relaxed text-text">
          {i18nT('components.kiroPrerequisiteGate.agent_restart_required_detail', { name })}
        </p>
      )
    if (value === KIRO)
      return (
        <p className="text-[13px] leading-relaxed text-text">
          {i18nT('pages.developer.agentBackendTab.default_all_features_supported')}
        </p>
      )
    if (row?.installed === 'installed')
      return (
        // One line, with Experimental as a tag on the sentence it qualifies: as a
        // paragraph of its own it read as an orphaned second status.
        <p className="flex flex-wrap items-center gap-x-2 gap-y-1 text-[13px] leading-relaxed text-text">
          <span>{i18nT('components.kiroPrerequisiteGate.agent_ready_to_use', { name })}</span>
          <Badge variant="muted">{i18nT('pages.developer.agentBackendTab.experimental')}</Badge>
        </p>
      )
    return (
      <p className="text-[13px] leading-relaxed text-text">
        {i18nT('pages.developer.agentBackendTab.experimental')}
      </p>
    )
  }

  /**
   * The standing facts about the harness on screen, under the actions: how it is
   * made to ask, the gating caveat, the security notes, the capability card, the
   * MCP half and the one where-it-lives note. Reference material, so it sits
   * below the state and the button rather than between them — the reader who
   * came to switch finds the switch where first-run setup put it, and the reader
   * comparing agents scrolls one row.
   *
   * Tool approval stays first among the standing lines. It is the one
   * security-relevant line on the card and the reason a build-excluded agent
   * cannot be picked -- and on the one agent that also carries a gating caveat,
   * "how it is made to ask" has to precede "and here is the hole in that", or the
   * two read as two answers to one question.
   */
  const facts = (value: string) => {
    const hasCard =
      approvalLine(value) ||
      caveats(value).length > 0 ||
      securityNotes(value).length > 0 ||
      capabilityLines(value).length > 0 ||
      mcpDenyRule(value) ||
      mcpIneffective(value).length > 0 ||
      noteLines(value).length > 0
    if (!hasCard) return null
    return (
      <div className="border-t border-border pt-3">
        {approvalLine(value) && (
          <p className="mb-0 text-[12px] leading-relaxed text-muted">{approvalLine(value)}</p>
        )}
        {caveats(value).map(line => (
          <p key={line} className="mt-1 mb-0 text-[12px] leading-relaxed text-muted">
            {line}
          </p>
        ))}
        {/* Security notes sit beside the approval line for the reason it is not
            behind a disclosure either. Two of them describe a boundary MOVING --
            Crew's own sandbox standing down for this child, and Crew handing over
            its own credential -- and the sandbox one fails open by design. WHICH
            notes these are is the server's classification, sent as its own list.
            Emphasis as WEIGHT rather than as alarm: these are permanent
            properties of the agent, not problems awaiting a fix. */}
        {securityNotes(value).map(id => (
          <p key={id} className="mt-1 mb-0 text-[12px] leading-relaxed text-text-strong">
            {NOTE_LABEL[id]}
          </p>
        ))}
        {capabilityLines(value).length > 0 && (
          /* Open, not collapsed. It was a `<details>` because up to fifteen lines
             times eight harnesses buried the control the panel exists for; with
             one harness on screen there is nothing to bury, and the capability
             set is the thing the reader came for. */
          /* Muted for a harness this build does not offer: a full-strength
             capability list directly under "This build does not offer this agent"
             reads as a mixed message. The facts are still true and still shown --
             an operator asking "why can I not pick that?" needs them -- they just
             stop competing with the line that answers the question. */
          <div className={`mt-3 ${buildExcluded(value) ? 'opacity-60' : ''}`}>
            <div className="text-[12px] font-semibold text-muted">
              {i18nT('pages.developer.agentBackendTab.card_supports_n_of_m', {
                name: nameOf(value),
                available: capabilityLines(value).filter(line => line.available).length,
                // CHECKED lines, not every line. An unchecked cell inside the
                // denominator and named as uncounted in the same breath is a
                // sentence that contradicts itself, and a reader has no way to
                // tell which half is true. Out of the fraction, it is counted as
                // neither BY the arithmetic, and the clause beside it says how
                // many sit outside. Identical to the old number on any harness
                // with nothing unchecked, which is every harness but two.
                total: capabilityLines(value).filter(line => line.measured !== false).length,
              })}
              {capabilityLines(value).filter(line => line.measured === false).length > 0 && (
                /* The count above is supported of TOTAL, and an unmeasured line sits
                   in the total without being in the supported half -- so the numbers
                   alone read as "unsupported" by subtraction. Naming the remainder is
                   what stops the count from making the claim the third state exists
                   to withdraw. */
                <span className="font-normal text-[11px] opacity-80">
                  {' '}
                  {i18nT('pages.developer.agentBackendTab.card_n_not_measured', {
                    unmeasured: capabilityLines(value).filter(line => line.measured === false)
                      .length,
                  })}
                </span>
              )}
            </div>
            <ul className="mt-1 mb-0 list-none pl-0 space-y-0.5 text-[12px] leading-relaxed">
              {capabilityLines(value).map(line => (
                <li key={line.id} className="flex items-start gap-1.5">
                  {/* The icon is decorative and the STATE is text: a mark that
                      only differs by shape and colour is unreadable to a screen
                      reader and to anyone who cannot tell the two colours apart. */}
                  {/* Three answers, three glyphs, and the unmeasured one borrows
                      neither of the others: a tick would promise what nobody has
                      driven, and the cross a real absence wears is what hides the
                      cell a measurement would pay for. Tested `=== false` rather
                      than for falsiness, so a gateway that sends no flag reads as
                      measured and the card it serves is the card it served. */}
                  {line.measured === false ? (
                    /* 11 and not 12: a ring encloses its ink where a tick and a
                       cross are two strokes, so the same nominal size renders a
                       heavier mark and the third state reads as a badge among ten
                       line glyphs. */
                    <CircleHelp
                      size={11}
                      strokeWidth={1.75}
                      aria-hidden
                      className="mt-1 shrink-0 text-warn"
                    />
                  ) : line.available ? (
                    <Check size={12} aria-hidden className="mt-1 shrink-0 text-ok" />
                  ) : (
                    <X size={12} aria-hidden className="mt-1 shrink-0 text-muted" />
                  )}
                  <span className={line.available ? 'text-text-strong' : 'text-muted'}>
                    <span className="sr-only">
                      {line.measured === false
                        ? i18nT('pages.developer.agentBackendTab.card_not_measured')
                        : line.available
                          ? i18nT('pages.developer.agentBackendTab.card_available')
                          : i18nT('pages.developer.agentBackendTab.card_not_available')}
                    </span>
                    {CAPABILITY_LABEL[line.id]}
                    {line.measured === false && unmeasuredReason(line.unmeasured_reason) && (
                      /* Its OWN line, dimmer and smaller, indented to the label
                         column by sitting inside the label's span. Inline it fused
                         with the label instead -- two blind readers of the rendered
                         card both read "The /compact command works No live run has
                         measured this yet" as one clause, which states the opposite
                         of what the row means. A block also gives the longest text
                         on the card somewhere to wrap to that is not the glyph
                         column. */
                      <span className="mb-0.5 block max-w-[44ch] text-[11px] leading-snug opacity-80">
                        {unmeasuredReason(line.unmeasured_reason)}
                      </span>
                    )}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* The MCP half, and the rule for what is on it: a line reaches the reader
            only where switching to this agent costs them a feature, adds a risk, or
            makes one of their own agent-file settings ineffective. Two things pass
            that test, and the route Crew takes to the agent is not one of them.

            First the RISK, as a rule with its exception directly under it. Switching
            one tool off is an ordinary action that says nothing about servers, so a
            reader meets this by accident -- and the exception is labelled as one,
            because two sentences that qualify each other without saying so read as a
            contradiction.

            Weight, not colour: a permanent property of the agent, not a problem
            awaiting a fix, and legible to a reader who cannot tell two colours
            apart. */}
        {mcpDenyRule(value) && (
          <p className="mt-3 mb-0 text-[12px] leading-relaxed text-text-strong">
            {mcpDenyRule(value)}
          </p>
        )}
        {mcpDenyException(value) && (
          <p className="mt-1 mb-0 text-[12px] leading-relaxed text-muted">
            {mcpDenyException(value)}
          </p>
        )}

        {/* Then the settings the reader wrote that will not take effect here. ONE
            group however the core ruled them: a withhold is a settled decision and a
            no-channel is an open gap, which is the mirror maintainer's distinction --
            the reader's question is whether the thing they wrote happens, and each
            line answers it. Open rather than behind a disclosure: with the route
            lines gone there is nothing left to bury, and this is the half a reader
            comparing two agents came for. */}
        {mcpIneffective(value).length > 0 && (
          <>
            <div className="mt-3 text-[12px] font-semibold leading-relaxed text-muted">
              {i18nT('pages.developer.agentBackendTab.card_mcp_ineffective', {
                name: nameOf(value),
              })}
            </div>
            <ul className="mt-0.5 mb-0 list-disc pl-4 space-y-0.5 text-[12px] leading-relaxed text-muted">
              {mcpIneffective(value).map(id => (
                <li key={id}>{mcpSettingLine(value, id)}</li>
              ))}
            </ul>
          </>
        )}

        {/* The one where-it-lives fact left on the card, and it is here because it
            is also the reader's: whose secret store this agent signs in against is a
            risk they take on. The three that named a route Crew takes -- whose disk
            holds the transcript, which registry fills the model picker, which channel
            carries a slash command -- are recorded off the card in
            `agent_sdk/backend_cards.py` and stated in `kirocrew doctor`.

            A plain line rather than a disclosure: one line needs no toggle, and the
            toggle's label was a heading for a list that no longer exists. */}
        {noteLines(value).map(id => (
          <p key={id} className="mt-2 mb-0 text-[12px] leading-relaxed text-muted">
            {NOTE_LABEL[id]}
          </p>
        ))}
      </div>
    )
  }

  return (
    <>
      {/* `askAgent` is ON, and the rule makes that the author's call rather than the
          reviewer's. It is right here because there is nothing for the hand-off to
          destroy: this panel has no editable field and no draft. It is a row list, a
          read-only detail and two buttons, and the one value it writes goes straight
          to `PATCH /api/config/kirocrew` -- so navigating to the chat can only cost
          the highlight, which is re-derived from the saved backend on return. Every
          failure that reaches this notice (a refused PATCH, a failed re-probe, a
          clipboard the browser would not grant) is also one an agent can act on with
          the structured context `ErrorNotice` recovers. */}
      <ErrorNotice
        message={actionError}
        askAgent
        onDismiss={() => setActionError('')}
      />
      {/* The chat's "Sign in to Kiro" link lands here with the sign-in anchor as
          its highlight. The anchor lives inside the KAS row's detail, so the
          detail has to be open for the anchor to mount and ring; this opens it.
          Rendered only under a router: the panel is also rendered bare, in tests
          and captures, where there is no URL to read. */}
      {inRouter && <SignInDeepLink onSignIn={() => highlightRow(KIRO_SIGN_IN_BACKEND)} />}
      <SettingsCard>
        <p className="mb-0 text-[13px] leading-relaxed text-muted">
          {i18nT('pages.developer.agentBackendTab.new_sessions_use_this_agent_a_session_that_is_al')}
        </p>
        {/* One radio group, laid out like first-run setup's picker: each harness is a
            row, the checked row opens its detail directly under itself, and the
            browser supplies the keyboard (one tab stop, arrows move check and focus
            together). Checking a row is looking at it; the config changes only
            through the detail's own Use button. */}
        <fieldset className="mx-0 space-y-2 border-none p-0">
          <legend className="sr-only">
            {i18nT('components.kiroPrerequisiteGate.other_agents_list_label')}
          </legend>
          {rows.map(value => {
            const row = probe(value)
            const selected = value === shown
            const isCurrent = value === current
            return (
              <AgentPickerRow
                key={value}
                id={value}
                group={GROUP_NAME}
                label={nameOf(value)}
                selected={selected}
                onSelect={() => highlightRow(value)}
                icon={
                  <span className="flex shrink-0 items-center text-muted" aria-hidden="true">
                    {iconOf(value)}
                  </span>
                }
                current={isCurrent}
                describedBy={rowStatusId(value)}
                status={
                  <span className="flex shrink-0 items-center gap-2">
                    {/* Which backend is IN USE, as a word and not a mark: with the
                        badge beside it, "Installed" would otherwise stand for two
                        facts at once -- able to run, and the one running. Only on
                        the configured row, so the column reads as a column. */}
                    {isCurrent && (
                      <span
                        className="text-[11px] font-medium text-accent"
                        title={i18nT('pages.developer.agentBackendTab.use_button_in_use')}
                      >
                        {i18nT('pages.developer.agentBackendTab.use_button_in_use')}
                      </span>
                    )}
                    {/* No verdict is not a verdict: a probe that did not answer gets
                        no badge rather than a claim, so the row never says an
                        install it did not measure. */}
                    {row && (
                      <AgentStatusBadge
                        probe={row}
                        blocked={false}
                        notOffered={buildExcluded(value)}
                      />
                    )}
                  </span>
                }
                detailId={DETAIL_ID}
                detailTestId="agent-harness-detail"
              >
                {selected && (
                  <>
                    {/* The state, first. `STRIP_ID` is what the Use button describes
                        itself with, so the reason a dead button is dead is the one
                        block a screen reader gets -- not the whole detail, this
                        button included. */}
                    <div id={STRIP_ID} className="space-y-3">
                      {lead(value)}
                    </div>
                    {/* What Use DOES, on the one row where the question arises. A
                        checked row is the one being READ, `aria-current` is the one
                        RUNNING, and the two coincide by default — so a reader who
                        checked another row has a radio that says "chosen" and a
                        button that says "Use", with nothing stating that neither has
                        switched anything yet. One sentence, directly above the
                        button, names the from and the to. Absent on the running row
                        (there is no button) and on a harness this build never
                        offers (same). */}
                    {!buildExcluded(value) && !isCurrent && (
                      <p className="mb-0 text-[12px] leading-relaxed text-muted">
                        {i18nT('pages.developer.agentBackendTab.use_switches_new_sessions', {
                          current: nameOf(current),
                          name: nameOf(value),
                        })}
                      </p>
                    )}
                    {/* The only control that switches anything. ABSENT rather than
                        dead for the harness already in use (the row says so) and for
                        a harness this build never offers: a PATCH the wire refuses is
                        not a button. Check again only where a re-check can change the
                        verdict. */}
                    <AgentDetailActions
                      name={nameOf(value)}
                      onUse={
                        !buildExcluded(value) && !isCurrent
                          ? () => {
                              // A PATCH writing the value already stored still
                              // resolves, which would run `onSuccess` and reset the
                              // model list. The button is absent on the active
                              // harness; this is the second guard, because an
                              // absence is a render away from being wrong.
                              if (value !== current) patchMut.mutate(value)
                            }
                          : undefined
                      }
                      useDisabled={cannotUse(value)}
                      useDescribedBy={STRIP_ID}
                      busy={busy}
                      switching={patchMut.isPending && patchMut.variables === value}
                      onRecheck={canRecheck ? () => recheckMut.mutate(value) : undefined}
                      rechecking={recheckMut.isPending}
                    />
                    {/* Inline, inside the detail, because that is where the control
                        that failed is. `askAgent` for the same reason as the notice at
                        the top: nothing here is an unsaved draft, and a failed probe
                        is exactly the kind of thing the agent can read the structured
                        context for. Keyed by backend (see `stripError`), so a late
                        rejection paints under the harness it belongs to. */}
                    {stripError?.backend === value && (
                      <ErrorNotice
                        message={stripError.message}
                        variant="inline"
                        askAgent
                        onDismiss={() => setStripError(null)}
                      />
                    )}
                    {/* Kiro sign-in, inside the one row whose harness uses it. The
                        identity the card stores is consumed by the KAS relay alone
                        (`ACP_BACKENDS_HOST_AUTH_CALLBACK`), so it is offered exactly
                        where KAS is: on a build that lists KAS without offering it
                        there is nothing to sign in for, and a chooser there would be
                        a sign-in to nothing. Isolated so a throwing card cannot take
                        the switch down with it. */}
                    {value === KIRO_SIGN_IN_BACKEND && offered.includes(KIRO_SIGN_IN_BACKEND) && (
                      <ErrorBoundary scope="developer-kiro-sign-in" fallback={null}>
                        <KiroSignInCard compact />
                      </ErrorBoundary>
                    )}
                    {facts(value)}
                  </>
                )}
              </AgentPickerRow>
            )
          })}
        </fieldset>

        {/* Every row's state as a WORD, for a reader who gets no badge at all.
            Hidden, outside the rows, and one per row rather than one for the shown
            row: a reader arrowing down the list is told each harness's state as
            they reach it, which is the only way the list is scannable without
            sight. The in-use word is here as well as in `aria-current` because a
            description is what a reader gets on the row they are ON, while
            `aria-current` is announced inconsistently across screen readers -- and
            "this is the one running" is not a fact to leave to chance. */}
        <div className="sr-only">
          {rows.map(value => (
            <span key={value} id={rowStatusId(value)}>
              {value === current
                ? `${i18nT('pages.developer.agentBackendTab.in_use')} ${status(value)}`
                : status(value)}
            </span>
          ))}
        </div>

        {/* The one thing the per-row lines cannot say. A managed fleet can bound
            this set through the `agent_backend` governance policy, and that policy
            is read once when the gateway starts — so an operator who edits it and
            sees no change here is not looking at a bug. Nothing in the UI can
            detect a not-yet-applied policy edit (that would mean reading the
            trust-root policy on a request path, which the harness-parity rules
            forbid), so stating the semantics is the honest substitute. */}
        <p className="mb-0 text-[12px] leading-relaxed text-muted">
          {i18nT('pages.developer.agentBackendTab.set_is_fixed_at_gateway_start')}
        </p>
      </SettingsCard>
    </>
  )
}

/**
 * Opens the KAS detail when the URL carries the sign-in anchor as its highlight.
 *
 * `useSettingHighlight` (mounted by SettingsPage) waits for the anchor element
 * and rings it, but it cannot mount the anchor: that lives inside the KAS row's
 * detail, which opens only when KAS is the checked row. So this watches the same
 * parameter and checks that row. The callback is held in a ref so the effect
 * runs once per appearance of the parameter, not once per render — a reader who
 * then clicks another row must not be dragged back while the highlight is still
 * being consumed.
 */
function SignInDeepLink({ onSignIn }: { onSignIn: () => void }) {
  const [params] = useSearchParams()
  const wanted = params.get('highlight') === `key:${KIRO_SIGN_IN_HIGHLIGHT_ANCHOR}`
  const callback = useRef(onSignIn)
  callback.current = onSignIn
  useEffect(() => {
    if (wanted) callback.current()
  }, [wanted])
  return null
}
