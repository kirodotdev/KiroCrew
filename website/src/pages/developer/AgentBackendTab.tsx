import { useRef, useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Bot,
  Boxes,
  Check,
  Circle,
  CircleCheck,
  CircleDot,
  CircleHelp,
  Download,
  Minus,
  RotateCw,
  Sparkles,
  Terminal,
  X,
} from 'lucide-react'

import { api } from '../../api/client'
import type { AcpBackendProbe } from '../../api/client'
import ErrorBoundary from '../../components/ErrorBoundary'
import ErrorNotice from '../../components/ErrorNotice'
import { SettingsCard } from '../../components/settings'
import { CopyCommandButton } from '../../components/settingRef/CopyCommandButton'
import { useConfigSchema } from '../../components/settingRef/useConfigSchema'
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
 * DOM id of the detail PANEL — the whole right-hand pane.
 *
 * ONE id rather than one per backend, because exactly one detail is ever rendered.
 * This is what the rows point `aria-controls` at, which is the tabs pattern's own
 * requirement: a tab names the panel it shows, not a fragment inside it.
 *
 * Deliberately NOT what the Use button describes itself with — see `STRIP_ID`. A
 * button whose `aria-describedby` named this element would have the entire card read
 * out as its description, including the button itself.
 */
const PANEL_ID = 'agent-backend-panel'

/**
 * DOM id of the status strip alone.
 *
 * The Use button's `aria-describedby`, so the reason a rendered button is dead is
 * what a screen reader gets — one sentence, not the whole pane it happens to sit in.
 */
const STRIP_ID = 'agent-backend-status'

/** DOM id of a row, so the panel it controls can name it and vice versa. */
const rowId = (value: string) => `agent-backend-row-${value || 'kiro'}`

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
 * ## Why it is a list and a detail, and not one card per harness
 *
 * Every harness's whole capability card used to render stacked down the page. That
 * is the right amount of information and the wrong amount at once: eight harnesses
 * times fifteen capability lines is a wall, and the reader's question is about one
 * harness at a time. So the harnesses are a LIST and the card belongs to whichever
 * row is highlighted. Exactly one card is ever rendered, which is also what let the
 * capability list come OUT of the disclosure it used to need: it was collapsed
 * because eight open cards buried the control, and with one card there is nothing to
 * bury.
 *
 * Each row carries TWO marks, not one: a leading dot for the backend in USE, and a
 * trailing glyph for READINESS. They are two independent facts and an operator needs
 * both at once — a harness that is the one running AND missing its binary has to read
 * as both, and one mark with a precedence between them can only ever show the winner.
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
 * ## Highlight is not selection, and that is the load-bearing part
 *
 * Clicking a row changes the detail pane and nothing else. The active backend
 * changes in exactly one place — the **Use** button at the foot of the detail — so
 * reading about a harness can never switch to it. That separation is why a harness
 * this machine cannot run still gets a row: under the old control an unselectable
 * harness had no chip, so the harnesses an operator most needed to read about were
 * the ones the page had least room for. A row is free; a switch is not.
 *
 * The two states are carried by different ARIA, deliberately: `aria-selected` is
 * which row you are LOOKING at, `aria-current` is which backend is RUNNING. A
 * screen reader gets the same two facts the glyphs carry.
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
  /**
   * The row elements, so keyboard navigation can move real focus.
   *
   * A tablist with automatic activation has to carry FOCUS to the row it activated:
   * moving only `aria-selected` and the roving `tabIndex` leaves a screen reader
   * announcing the row the user has left, and leaves Tab continuing from an element
   * that is now `tabIndex={-1}`.
   */
  const rowRefs = useRef<Record<string, HTMLButtonElement | null>>({})
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
      setActionError('')
      qc.invalidateQueries({ queryKey: ['kirocrewConfig'] })
      // The model list is the NEW backend's now. `/api/models` re-reads
      // `agent.acp_backend` on every call, so the server side needs no restart;
      // only the frontend cache did, because `['available-models']` is refetched
      // in exactly one other place — a spawned session (`useWebSocket`'s
      // `activity_event`) — and the global `staleTime: Infinity` plus the
      // self-heal poll stopping after one live success mean nothing else ever
      // re-asks. That is why the picker looked like it needed a gateway restart:
      // the restart was just the next session spawn.
      //
      // `resetQueries`, not `invalidateQueries`: invalidate keeps the OLD
      // backend's rows on screen until the refetch lands, and a cold
      // `--list-models` spawn can take the gateway's full 10s. A pick in that
      // window writes an id the new backend rejects. Reset drops the data to
      // `undefined` first, so every picker shows the auto-only placeholder for
      // those seconds, then refetches. Drop the last-good localStorage list
      // FIRST for the same reason: a failing first fetch must degrade to
      // auto-only, not to the old backend's ids.
      clearCachedModels()
      qc.resetQueries({ queryKey: ['available-models'] })
    },
    // No optimistic write and no local mirror of the value: the list reads
    // straight from the query, so a rejected PATCH needs no revert — the cache was
    // never moved off the server's answer.
    onError: () => setActionError(i18nT('pages.developer.agentBackendTab.could_not_save_the_agent_backend')),
  })

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
      qc.setQueryData<{ backends: AcpBackendProbe[] }>(['acpBackends'], prev =>
        prev
          ? {
              backends: prev.backends.some(b => b.id === backend.id)
                ? prev.backends.map(b => (b.id === backend.id ? backend : b))
                : [...prev.backends, backend],
            }
          : { backends: [backend] },
      )
    },
    // Surfaced, unlike a failed probe GET. A failed poll is absent information the
    // user did not ask for; a failed re-check is a button they pressed, and silence
    // would read as "checked, still missing".
    // The failing backend comes from the mutation's own variables, not from `shown`:
    // `shown` is whatever row is highlighted when the rejection arrives, which is the
    // bug this keying exists for.
    onError: (_error, backend) =>
      setStripError({
        backend,
        message: i18nT('pages.developer.agentBackendTab.install_check_failed'),
      }),
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
   * Selectability is deliberately NOT part of this: an unselectable agent has no Use
   * button at all rather than a dead one, so the only reasons a rendered button is
   * dead are ones the user can act on — install the binary, or re-check.
   */
  const cannotUse = (value: string) => notInstalled(value) || needsRestart(value)

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
   * under its policy name is legible, whereas `NAME[value]` returning `undefined`
   * renders a row with no text at all. A core agent that ships selectable gets a
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
    // Names the cheap remedy, which is the button directly beside this line, rather
    // than the gateway restart. A reader who does not know what the gateway is cannot
    // act on "restart it", and now does not have to.
    if (row?.restart_required)
      return i18nT('pages.developer.agentBackendTab.installed_check_again_to_use')
    if (value === KIRO) return i18nT('pages.developer.agentBackendTab.default_all_features_supported')
    return i18nT('pages.developer.agentBackendTab.experimental')
  }

  /**
   * The trailing mark: is this harness READY, and nothing else.
   *
   * Deliberately says nothing about which backend is in use. Those are two
   * independent facts and an operator needs both at once — a harness that is active
   * AND missing its binary is a real state (the component was removed under a saved
   * value) and it has to read as "the one in use, and it is broken". One mark with a
   * precedence can only ever show the winner, so the two get separate columns: the
   * leading dot below is in-use, this is readiness.
   *
   * Every icon is `aria-hidden`; the row's accessible description is `status(value)`
   * in full, so the glyph summarises a sentence that is also present rather than
   * being the only place the state is stated.
   */
  const readinessGlyph = (value: string): React.ReactNode => {
    if (buildExcluded(value)) return <Minus size={13} aria-hidden className="text-muted" />
    if (notInstalled(value)) return <Download size={13} aria-hidden className="text-warn" />
    if (needsRestart(value)) return <RotateCw size={13} aria-hidden className="text-warn" />
    // No verdict is not a verdict: a probe that did not answer gets an outline
    // rather than the tick, so the panel never claims an install it did not measure.
    if (!probe(value) || probe(value)?.installed === 'unknown')
      return <Circle size={13} aria-hidden className="text-muted" />
    // `CircleCheck` and not `Check`: the card below marks each CAPABILITY with a bare
    // tick, and the same glyph meaning "this harness is ready" in the list and "this
    // feature is available" in the detail is one alphabet doing two jobs in one view.
    return <CircleCheck size={13} aria-hidden className="text-ok" />
  }

  /**
   * Highlight *value* and put the keyboard on it.
   *
   * One helper for every keyboard route so focus cannot be moved by some of them and
   * not others. Focusing straight after the state write is safe because the row
   * already exists — only its `tabIndex` changes on the re-render — and focusing an
   * element that is still `tabIndex={-1}` works programmatically.
   */
  const focusRow = (value: string) => {
    highlightRow(value)
    rowRefs.current[value]?.focus()
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
   * The readiness word that accompanies the glyph, in the glyph's own precedence.
   *
   * A glyph with only a `title` is a glyph a touch device never explains and a
   * first-time reader has to guess at. One short word costs the row very little and
   * removes the guessing. Derived from the same branches as `readinessGlyph` rather
   * than passed alongside it, so the mark and the word cannot drift apart.
   */
  const readinessWord = (value: string): string => {
    if (buildExcluded(value)) return i18nT('pages.developer.agentBackendTab.word_not_offered')
    if (notInstalled(value)) return i18nT('pages.developer.agentBackendTab.word_missing')
    if (needsRestart(value)) return i18nT('pages.developer.agentBackendTab.word_recheck')
    if (!probe(value) || probe(value)?.installed === 'unknown')
      return i18nT('pages.developer.agentBackendTab.word_not_checked')
    return i18nT('pages.developer.agentBackendTab.word_ready')
  }

  /** Move the highlight by *delta* rows, clamped rather than wrapped. */
  const moveHighlight = (delta: number) => {
    const at = rows.indexOf(shown)
    const next = rows[Math.min(rows.length - 1, Math.max(0, at + delta))]
    if (next !== undefined) focusRow(next)
  }

  /**
   * Whether the one mutating control is pressable, computed once.
   *
   * Read by both the `disabled` attribute and the styling, so the two cannot disagree
   * -- a button that looks live and is not is the defect this replaced.
   */
  const useDisabled = shown === current || cannotUse(shown) || patchMut.isPending

  const install = probe(shown)?.install_command ?? ''
  // Every state the re-check can help with, and only those. A build-excluded harness
  // is not one: nothing this machine holds is why it is not on offer, so a button
  // that re-measured the machine would answer a question nobody asked.
  const canRecheck =
    !buildExcluded(shown) &&
    (notInstalled(shown) || needsRestart(shown) || probe(shown)?.installed === 'unknown')

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
      <SettingsCard>
        <div className="text-[13px] font-semibold text-text-strong">
          {i18nT('pages.developer.agentBackendTab.agent_backend')}
        </div>
        <p className="mt-0.5 mb-2 text-[12px] leading-relaxed text-muted">
          {i18nT('pages.developer.agentBackendTab.new_sessions_use_this_agent_a_session_that_is_al')}
        </p>
        {/* List and detail. On a narrow viewport the grid collapses to one column and
            the row list becomes a horizontal strip above the detail -- the same
            elements and the same ARIA, laid out along the other axis, so there is no
            second implementation to keep honest. `min-w-0` on both tracks because a
            long install command in the detail would otherwise force the grid wider
            than its container and push the list off screen. */}
        <div className="grid grid-cols-1 gap-3 md:grid-cols-[minmax(8rem,13rem)_minmax(0,1fr)]">
          <div
            role="tablist"
            // Vertical is the wide layout's axis, and BOTH axes are handled below --
            // the orientation flips with a CSS breakpoint that this component never
            // reads, so refusing one axis would break the keyboard in whichever
            // layout the reader happens to be in.
            aria-orientation="vertical"
            aria-label={i18nT('pages.developer.agentBackendTab.agent_backend')}
            className="flex min-w-0 flex-row gap-0.5 overflow-x-auto rounded-lg border border-border bg-bg-accent p-[3px] md:flex-col md:overflow-x-visible"
          >
            {rows.map(value => (
              <button
                key={value}
                id={rowId(value)}
                ref={el => {
                  rowRefs.current[value] = el
                }}
                type="button"
                role="tab"
                // Which row you are LOOKING at. `aria-current` below is which backend
                // is RUNNING. Two facts, two attributes -- the whole point of the
                // layout is that they are not the same thing.
                aria-selected={value === shown}
                aria-current={value === current ? 'true' : undefined}
                aria-controls={PANEL_ID}
                aria-describedby={rowStatusId(value)}
                // Roving tabindex: one stop for the whole list, and the arrow keys
                // move within it. Every row being tabbable would put eight stops
                // between the panel's heading and its only button.
                tabIndex={value === shown ? 0 : -1}
                // `w-full` only from `md`, where the strip is a COLUMN and a row
                // should fill it. On narrow the strip is a horizontal scroller, and a
                // full-width row there means exactly one row on screen and seven
                // unreachable ones -- which reads as a dropdown that does nothing when
                // tapped.
                //
                // `max-w` on narrow for the same reachability reason: content sizing
                // alone lets a long name plus its readiness word fill the viewport, so
                // the cap makes the NAME truncate and keeps several harnesses visible.
                // A truncated name is still recognisable and carries its full text as
                // the row's accessible name; a hidden row carries nothing.
                className={`flex max-w-[11rem] shrink-0 items-center gap-1.5 rounded-md border px-2 py-[5px] text-left text-[13px] cursor-pointer transition-colors md:max-w-none md:w-full ${
                  value === shown
                    ? 'border-border-strong bg-bg-elevated text-text-strong font-semibold shadow-sm'
                    : 'border-transparent bg-transparent text-muted font-medium hover:text-text-strong'
                }`}
                onClick={() => highlightRow(value)}
                onKeyDown={e => {
                  if (e.key === 'ArrowDown' || e.key === 'ArrowRight') {
                    e.preventDefault()
                    moveHighlight(1)
                  } else if (e.key === 'ArrowUp' || e.key === 'ArrowLeft') {
                    e.preventDefault()
                    moveHighlight(-1)
                  } else if (e.key === 'Home') {
                    e.preventDefault()
                    focusRow(rows[0] ?? shown)
                  } else if (e.key === 'End') {
                    e.preventDefault()
                    focusRow(rows[rows.length - 1] ?? shown)
                  } else if (e.key === 'Enter' || e.key === ' ') {
                    // Highlight, never select. The browser would fire `click` for
                    // both of these on a <button> anyway; naming them is what makes
                    // "Enter does not switch the backend" a property of this file
                    // rather than an accident of which handler happens to be wired.
                    e.preventDefault()
                    highlightRow(value)
                  }
                }}
              >
                {/* Leading, radio-style: which backend is IN USE. Its own column
                    rather than a value of the readiness mark, so `● … ⬇` — the one in
                    use, and it is broken — is a state the row can actually show.
                    Reserved even when empty so the names stay on one left edge and
                    the column reads as a column. */}
                <span className="flex w-3.5 shrink-0 items-center justify-center">
                  {value === current && (
                    /* `title` on the wrapper, not on the reserved column: an empty
                       column must not offer a tooltip for a mark it is not showing.
                       Short form of the same word the trailing cluster prints, so the
                       hover text and the visible text are one string. */
                    <span
                      className="flex items-center"
                      title={i18nT('pages.developer.agentBackendTab.use_button_in_use')}
                    >
                      <CircleDot size={12} aria-hidden className="text-accent" />
                    </span>
                  )}
                </span>
                <span className="truncate">{nameOf(value)}</span>
                {/* Trailing: readiness. The name yields before either mark does — on a
                    narrow strip the name truncates and the state does not, because the
                    state is what the row is scanned for. */}
                {/* `title` so the glyph has a word a sighted reader can reach without
                    opening the row. The same sentence the row's description carries, so
                    the hover text and the announced text cannot drift -- and the glyph
                    stays a summary of something stated in full elsewhere rather than
                    the only place the state exists. */}
                <span
                  className="ml-auto flex shrink-0 items-center gap-1"
                  title={status(value)}
                >
                  {/* The leading dot's word, printed where words live. Without it
                      the only visible word on the running harness is its readiness one,
                      so "Ready" stands for two facts at once: able to run, and the one
                      running. `aria-hidden` for the same reason the readiness word is
                      -- the row's description states both in full. */}
                  {value === current && (
                    <span aria-hidden className="text-[10px] font-medium text-accent">
                      {i18nT('pages.developer.agentBackendTab.use_button_in_use')}
                    </span>
                  )}
                  {readinessGlyph(value)}
                  {/* The word the glyph stands for. `aria-hidden` because the row's
                      description already states the state in full, and announcing a
                      one-word summary on top of that sentence would say it twice. */}
                  <span aria-hidden className="text-[10px] font-medium">
                    {readinessWord(value)}
                  </span>
                </span>
              </button>
            ))}
          </div>

          {/* Every row's state as a WORD, for a reader who gets no glyph at all.
              Hidden, outside the rows, and one per row rather than one for the shown
              row: a reader arrowing down the list is told each harness's state as
              they reach it, which is the only way the list is scannable without
              sight. Same sentence the strip carries, so there is one source for it. */}
          <div className="sr-only">
            {rows.map(value => (
              <span key={value} id={rowStatusId(value)}>
                {/* Both marks in words, in the order they are read visually. The
                    in-use word is here as well as in `aria-current` because a
                    description is what a reader gets on the row they are ON, while
                    `aria-current` is announced inconsistently across screen readers —
                    and "this is the one running" is not a fact to leave to chance. */}
                {value === current
                  ? `${i18nT('pages.developer.agentBackendTab.in_use')} ${status(value)}`
                  : status(value)}
              </span>
            ))}
          </div>

          <div
            role="tabpanel"
            id={PANEL_ID}
            aria-labelledby={rowId(shown)}
            // Keyed on the shown row so React remounts the pane per harness: a
            // <details> left open on one harness must not decide the next one's, and
            // the disclosure state is exactly what would otherwise survive.
            key={shown}
            className="min-w-0"
          >
            <div className="flex items-center gap-1.5 text-[13px] font-semibold text-text-strong">
              {iconOf(shown)}
              {nameOf(shown)}
            </div>

            {/* The status strip. One state at a time, and every string on it comes
                from the payload or from a label above -- there is no per-harness
                sentence here to go stale. */}
            <div
              id={STRIP_ID}
              className={`mt-1.5 rounded-md border px-2.5 py-2 text-[11px] leading-relaxed ${
                cannotUse(shown) ? 'border-warn/40 text-warn' : 'border-border text-muted'
              }`}
            >
              {/* Said out loud, not only to a screen reader. Two dim Use buttons
                  otherwise look alike for different reasons -- one because the harness
                  is already running, one because its binary is missing -- and the
                  reader cannot tell which. Same key as the row description, so the
                  visible and the announced text cannot drift. */}
              {shown === current && (
                <span className="font-semibold text-text-strong">
                  {i18nT('pages.developer.agentBackendTab.in_use')}{' '}
                </span>
              )}
              {status(shown)}
              {install && notInstalled(shown) && (
                <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                  {/* Named for a screen reader and not on screen: the command sits
                      directly under "Missing on this machine: X" with a Copy command
                      button beside it, so a sighted reader already knows what the
                      monospace block is, while a screen reader would otherwise be
                      read a bare shell string with no idea what it is for. */}
                  <span className="sr-only">
                    {i18nT('pages.developer.agentBackendTab.install_command')}
                  </span>
                  {/* Selectable text as well as copyable: an operator on a machine
                      whose clipboard the browser will not touch still has the string. */}
                  <code className="min-w-0 break-all rounded bg-bg-accent px-1.5 py-0.5 text-text-strong">
                    {install}
                  </code>
                  {/* The SHARED copy control, not a hand-rolled one. A bare
                      `navigator.clipboard.writeText` silently no-ops wherever the async
                      Clipboard API is missing -- it needs a secure context, and a
                      plain-HTTP LAN or remote gateway is not one, which is a large share
                      of this product's real deployments. `CopyCommandButton` goes through
                      `copyCode`, which falls back to `execCommand`, and it shows its tick
                      only once the text actually reached the clipboard. A tick over an
                      unchanged clipboard is worse than no affordance: the reader walks
                      away believing they hold the command. */}
                  <CopyCommandButton text={install} />
                </div>
              )}
              {canRecheck && (
                <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
                  <button
                    type="button"
                    disabled={recheckMut.isPending}
                    className="rounded-md border border-border bg-bg-elevated px-2 py-[3px] text-text-strong cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
                    onClick={() => recheckMut.mutate(shown)}
                  >
                    {recheckMut.isPending
                      ? i18nT('pages.developer.agentBackendTab.checking')
                      : i18nT('pages.developer.agentBackendTab.check_again')}
                  </button>
                </div>
              )}
              {/* Inline, inside the strip, because that is where the control that
                  failed is. `askAgent` for the same reason as the notice at the top:
                  nothing here is an unsaved draft, and a failed probe or a refused
                  clipboard is exactly the kind of thing the agent can read the
                  structured context for. */}
              {stripError?.backend === shown && (
                <div className="mt-1.5">
                  <ErrorNotice
                    message={stripError.message}
                    variant="inline"
                    askAgent
                    onDismiss={() => setStripError(null)}
                  />
                </div>
              )}
            </div>

            {/* Tool approval stays first among the standing lines. It is the one
                security-relevant line on the card and the reason a build-excluded
                agent cannot be picked -- and on the one agent that also carries a
                gating caveat, "how it is made to ask" has to precede "and here is the
                hole in that", or the two read as two answers to one question. */}
            {approvalLine(shown) && (
              <p className="mt-2 mb-0 text-[11px] leading-relaxed text-muted">{approvalLine(shown)}</p>
            )}
            {caveats(shown).map(line => (
              <p key={line} className="mt-1 mb-0 text-[11px] leading-relaxed text-muted">
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
            {securityNotes(shown).map(id => (
              <p key={id} className="mt-1 mb-0 text-[11px] leading-relaxed text-text-strong">
                {NOTE_LABEL[id]}
              </p>
            ))}
            {/* The MCP half renders BEFORE the tool-off cost below it, and the
                order is the point: the cost line is the first place a reader meets
                the word "MCP", and the summary is where it is explained. A gloss a
                reader reaches only after the sentence that needed it arrived too
                late. */}
            {capabilityLines(shown).length > 0 && (
              /* Open, not collapsed. It was a `<details>` because up to fifteen lines
                 times eight harnesses buried the control the panel exists for; with
                 one harness on screen there is nothing to bury, and the capability
                 set is the thing the reader came for. */
              /* Muted for a harness this build does not offer: a full-strength
                 capability list directly under "This build does not offer this agent"
                 reads as a mixed message. The facts are still true and still shown --
                 an operator asking "why can I not pick that?" needs them -- they just
                 stop competing with the line that answers the question. */
              <div className={`mt-2 ${buildExcluded(shown) ? 'opacity-60' : ''}`}>
                <div className="text-[11px] font-semibold text-muted">
                  {i18nT('pages.developer.agentBackendTab.card_supports_n_of_m', {
                    name: nameOf(shown),
                    available: capabilityLines(shown).filter(line => line.available).length,
                    // CHECKED lines, not every line. An unchecked cell inside the
                    // denominator and named as uncounted in the same breath is a
                    // sentence that contradicts itself, and a reader has no way to
                    // tell which half is true. Out of the fraction, it is counted as
                    // neither BY the arithmetic, and the clause beside it says how
                    // many sit outside. Identical to the old number on any harness
                    // with nothing unchecked, which is every harness but two.
                    total: capabilityLines(shown).filter(line => line.measured !== false).length,
                  })}
                  {capabilityLines(shown).filter(line => line.measured === false).length > 0 && (
                    /* The count above is supported of TOTAL, and an unmeasured line sits
                       in the total without being in the supported half -- so the numbers
                       alone read as "unsupported" by subtraction. Naming the remainder is
                       what stops the count from making the claim the third state exists
                       to withdraw. */
                    <span className="font-normal text-[10px] opacity-80">
                      {' '}
                      {i18nT('pages.developer.agentBackendTab.card_n_not_measured', {
                        unmeasured: capabilityLines(shown).filter(
                          line => line.measured === false,
                        ).length,
                      })}
                    </span>
                  )}
                </div>
                <ul className="mt-1 mb-0 list-none pl-0 space-y-0.5 text-[11px] leading-relaxed">
                  {capabilityLines(shown).map(line => (
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
                          className="mt-0.5 shrink-0 text-warn"
                        />
                      ) : line.available ? (
                        <Check size={12} aria-hidden className="mt-0.5 shrink-0 text-ok" />
                      ) : (
                        <X size={12} aria-hidden className="mt-0.5 shrink-0 text-muted" />
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
                          <span className="mb-0.5 block max-w-[44ch] text-[10px] leading-snug opacity-80">
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
            {mcpDenyRule(shown) && (
              <p className="mt-2 mb-0 text-[11px] leading-relaxed text-text-strong">
                {mcpDenyRule(shown)}
              </p>
            )}
            {mcpDenyException(shown) && (
              <p className="mt-1 mb-0 text-[11px] leading-relaxed text-muted">
                {mcpDenyException(shown)}
              </p>
            )}

            {/* Then the settings the reader wrote that will not take effect here. ONE
                group however the core ruled them: a withhold is a settled decision and a
                no-channel is an open gap, which is the mirror maintainer's distinction --
                the reader's question is whether the thing they wrote happens, and each
                line answers it. Open rather than behind a disclosure: with the route
                lines gone there is nothing left to bury, and this is the half a reader
                comparing two agents came for. */}
            {mcpIneffective(shown).length > 0 && (
              <>
                <div className="mt-2 text-[11px] font-semibold leading-relaxed text-muted">
                  {i18nT('pages.developer.agentBackendTab.card_mcp_ineffective', {
                    name: nameOf(shown),
                  })}
                </div>
                <ul className="mt-0.5 mb-0 list-disc pl-4 space-y-0.5 text-[11px] leading-relaxed text-muted">
                  {mcpIneffective(shown).map(id => (
                    <li key={id}>{mcpSettingLine(shown, id)}</li>
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
            {noteLines(shown).map(id => (
              <p key={id} className="mt-1 mb-0 text-[11px] leading-relaxed text-muted">
                {NOTE_LABEL[id]}
              </p>
            ))}

            {/* The only control that switches anything, alone at the foot of the
                detail. Absent rather than dead for a harness this build never offers:
                a PATCH the wire refuses is not a button. `aria-describedby` names the
                STRIP and not the panel, so the reason a dead button is dead is the one
                sentence a screen reader gets — naming the panel would read the whole
                card out, this button included. */}
            {!buildExcluded(shown) && (
              <div className="mt-3 flex justify-end">
                {/* Raised and bordered only while it can actually be pressed. A dead
                    click on the panel's ONE mutating control is answered by silence, so
                    the dead state drops every affordance at once: the border, the fill
                    and the elevation all go, and the label fades on top of that. Any
                    one of them left in place reads as pressable on its own. */}
                <button
                  type="button"
                  disabled={useDisabled}
                  aria-describedby={STRIP_ID}
                  className={`rounded-md border px-3 py-[5px] text-[13px] font-semibold transition-colors ${
                    useDisabled
                      ? 'border-transparent bg-transparent text-muted opacity-40 cursor-not-allowed'
                      : 'border-border-strong bg-bg-elevated text-text-strong shadow-sm cursor-pointer hover:bg-bg-hover'
                  }`}
                  onClick={() => {
                    // A PATCH writing the value already stored still resolves
                    // successfully, which would run `onSuccess` and reset the model
                    // list -- blanking every picker and spawning `--list-models` for a
                    // backend that did not change. The button is disabled on the
                    // active harness; this is the second guard, because a disabled
                    // attribute is a render away from being wrong.
                    if (shown !== current) patchMut.mutate(shown)
                  }}
                >
                  {/* The reason IS the label on the active harness. Two dead buttons
                      otherwise look identical for different reasons -- already running
                      versus binary absent -- and the strip was the only thing telling
                      them apart. */}
                  {shown === current
                    ? i18nT('pages.developer.agentBackendTab.use_button_in_use')
                    : i18nT('pages.developer.agentBackendTab.use_harness', {
                        name: nameOf(shown),
                      })}
                </button>
              </div>
            )}
          </div>
        </div>
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
      {/* Kiro sign-in, under the switch that gives it a purpose. The identity the
          card stores is consumed by the KAS relay alone
          (`ACP_BACKENDS_HOST_AUTH_CALLBACK`), so the card is offered exactly when
          KAS is: on a build or policy that hides that option there is nothing to
          sign in for, and a chooser there would be a sign-in to nothing. Keyed on
          `offered` — the set the switch can move TO, not the wider set it lists, so
          a harness that is only listed never draws a sign-in for an option nobody
          can pick. Gated on KAS being OFFERED rather than SELECTED, so the user can
          sign in first and switch second instead of paying one "not signed in" turn
          to find the card. Isolated so a throwing card cannot take the switch
          down with it. */}
      {offered.includes(KIRO_SIGN_IN_BACKEND) && (
        <ErrorBoundary scope="developer-kiro-sign-in" fallback={null}>
          <KiroSignInCard />
        </ErrorBoundary>
      )}
    </>
  )
}
