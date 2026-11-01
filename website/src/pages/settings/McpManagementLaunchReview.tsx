/**
 * A row's share controls on the MCP page: its Share switch, its Approve button,
 * its Details toggle and what Details shows. `McpManagement` mounts them per row.
 *
 * Details shows the launch only as the gateway's display-safe form (bounded
 * argv, env KEY names; no value). A press reads the launch at that moment and
 * the write carries its `expected_launch`, so the approval is pinned to the
 * exact launch the gateway resolved whether or not Details was open; a later
 * change to it still pauses sharing until the person presses again.
 */
import { Fragment, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Check, ChevronRight } from 'lucide-react'
import { api, type McpManagedServer } from '../../api/client'
import { Btn } from '../../components/ui'
import ErrorNotice from '../../components/ErrorNotice'
import { i18nT } from '../../i18n/t'
import {
  argvLine,
  detailsWhyKey,
  envKeyNames,
  launchDiff,
  type LaunchRefusal,
  launchIsApprovable,
  type McpRowState,
} from './mcpManagementModel'

/** The id of a row's Details sub-row, so its toggle can point at it. */
export function detailsRowId(name: string): string {
  return `mcp-row-details-${name}`
}

/**
 * A row's "Details" toggle. It opens a full-width sub-row under the table row
 * (`ServerDetailsBody`), so it is a real button carrying `aria-expanded` and
 * `aria-controls`, not a `<details>` inside one cell.
 */
export function DetailsToggle({
  server,
  open,
  onToggle,
}: {
  server: McpManagedServer
  open: boolean
  onToggle: () => void
}) {
  if (server.agents.length === 0) return null
  return (
    <button
      type="button"
      aria-expanded={open}
      aria-controls={detailsRowId(server.name)}
      aria-label={i18nT('pages.mcpManagement.details_aria', { name: server.name })}
      onClick={onToggle}
      className="mt-1 inline-flex items-center gap-1 rounded text-[12px] text-[var(--muted)] hover:text-[var(--text)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]"
    >
      <ChevronRight
        size={12}
        className={['transition-transform', open ? 'rotate-90' : ''].join(' ')}
        aria-hidden="true"
      />
      {i18nT('pages.mcpManagement.details')}
    </button>
  )
}

/** The id of a row's "why" line in Details, so its controls can point at it. */
export function detailsWhyId(name: string): string {
  return `mcp-row-why-${name}`
}

const DETAILS_LABEL_CLASS = 'text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]'

/**
 * One launch as the gateway displayed it: each argv on its own line in a mono
 * block, then the env KEY names. Values are never drawn, not even the
 * gateway's own redacted form.
 */
function LaunchBlock({
  label,
  commands,
  envs,
  complete,
  marked,
}: {
  label: string
  commands?: string[][]
  envs?: string[][]
  complete?: boolean
  /** Arguments and env names to mark as changed (the NEW side of a change). */
  marked?: { args: ReadonlySet<string>; env: ReadonlySet<string> }
}) {
  const names = envKeyNames(envs)
  return (
    <div className="min-w-0 space-y-1">
      <div className={DETAILS_LABEL_CLASS}>{label}</div>
      <pre className="m-0 whitespace-pre-wrap [overflow-wrap:anywhere] rounded bg-[var(--bg)] px-2 py-1.5 font-mono text-[12px] text-[var(--text)]">
        {(commands ?? []).map((argv, line) => (
          <Fragment key={line}>
            {line > 0 && '\n'}
            {argv.map((arg, i) => (
              <Fragment key={i}>
                {i > 0 && ' '}
                {marked?.args.has(arg) ? (
                  <mark className="rounded-sm bg-warn/25 px-0.5 text-[var(--text)]">{argvLine([arg])}</mark>
                ) : (
                  argvLine([arg])
                )}
              </Fragment>
            ))}
          </Fragment>
        ))}
      </pre>
      {names.length > 0 && (
        <p className="m-0 break-words text-[12px] [overflow-wrap:anywhere]">
          <span className={DETAILS_LABEL_CLASS}>{i18nT('pages.mcpManagement.details_env')}</span>{' '}
          {names.map((n, i) => (
            <Fragment key={n}>
              {i > 0 && ', '}
              {marked?.env.has(n) ? (
                <mark className="rounded-sm bg-warn/25 px-0.5 font-mono text-[var(--text)]">{n}</mark>
              ) : (
                <span className="font-mono text-[var(--text)]">{n}</span>
              )}
            </Fragment>
          ))}
        </p>
      )}
      {complete === false && (
        <p className="m-0 text-[12px]">{i18nT('pages.mcpManagement.details_runs_cut')}</p>
      )}
    </div>
  )
}

/**
 * The launch of a row with no refusal, read when its Details opens from the
 * same display-safe route a press reads. Only a stdio server has one.
 */
function PreviewLaunch({ name }: { name: string }) {
  const q = useQuery({
    queryKey: ['mcpGatewayLaunchPreview', name],
    queryFn: () => api.mcpGatewayLaunchPreview(name),
    retry: false,
    staleTime: 30_000,
  })
  if (q.isPending) {
    return <p className="m-0 text-[12px]">{i18nT('pages.mcpManagement.details_runs_loading')}</p>
  }
  if (q.isError) {
    return <ErrorNotice variant="inline" message={i18nT('pages.mcpManagement.details_runs_failed')} askAgent />
  }
  return (
    <LaunchBlock
      label={i18nT('pages.mcpManagement.details_runs')}
      commands={q.data.commands}
      envs={q.data.envs}
      complete={q.data.complete}
    />
  )
}

/**
 * What a row's Details shows, all from what the gateway already sent: what it
 * runs (command, args, env key names), and one line on why the row is in its
 * state. A row paused by a change shows the approved launch beside the new one.
 */
export function ServerDetailsBody({
  server,
  state,
  refusal,
}: {
  server: McpManagedServer
  state: McpRowState
  refusal?: LaunchRefusal
}) {
  return (
    <div className="min-w-0 space-y-2 rounded-lg bg-[var(--hover)] px-3 py-2 text-[12px] leading-relaxed text-[var(--muted)]">
      <p id={detailsWhyId(server.name)} className="m-0 break-words text-[12.5px] [overflow-wrap:anywhere]">
        {i18nT(detailsWhyKey(server, state, refusal))}
      </p>
      {refusal ? (
        <RefusalLaunch refusal={refusal} />
      ) : server.transport === 'stdio' ? (
        <PreviewLaunch name={server.name} />
      ) : null}
    </div>
  )
}

/**
 * What a refused launch runs, from the refusal itself (no second read): the
 * approved launch beside the new one for a changed command, side by side from
 * `md` and stacked below it; otherwise one Runs block. Shared by a row's
 * Details and the Review dialog, so both show the launch an Approve pins.
 */
export function RefusalLaunch({ refusal }: { refusal: LaunchRefusal }) {
  const changed = refusal.reason === 'changed_needs_reapproval' && refusal.approved_commands !== undefined
  if (changed) {
    const diff = launchDiff(refusal)
    const lines = [
      diff.removed.length > 0 && diff.added.length > 0
        ? i18nT('pages.mcpManagement.diff_changed', { from: diff.removed.join(' '), to: diff.added.join(' ') })
        : diff.added.length > 0
          ? i18nT('pages.mcpManagement.diff_added', { tokens: diff.added.join(' ') })
          : diff.removed.length > 0
            ? i18nT('pages.mcpManagement.diff_removed', { tokens: diff.removed.join(' ') })
            : null,
      diff.envAdded.length > 0 ? i18nT('pages.mcpManagement.diff_env_added', { names: diff.envAdded.join(', ') }) : null,
      diff.envRemoved.length > 0
        ? i18nT('pages.mcpManagement.diff_env_removed', { names: diff.envRemoved.join(', ') })
        : null,
    ].filter((l): l is string => l !== null)
    return (
      <div className="min-w-0 space-y-2">
        {/* What changed, said once above the two columns, so the reader does
            not compare two long commands by eye. */}
        {lines.length > 0 && (
          <ul className="m-0 list-none space-y-0.5 p-0 font-mono text-[12px] text-[var(--text)] [overflow-wrap:anywhere]">
            {lines.map(l => (
              <li key={l}>{l}</li>
            ))}
          </ul>
        )}
        <div className="grid min-w-0 gap-2 md:grid-cols-2">
          <LaunchBlock
            label={i18nT('pages.mcpManagement.details_approved')}
            commands={refusal.approved_commands}
            envs={refusal.approved_envs}
          />
          <LaunchBlock
            label={i18nT('pages.mcpManagement.details_new')}
            commands={refusal.commands}
            envs={refusal.envs}
            complete={refusal.complete}
            marked={{ args: new Set(diff.added), env: new Set(diff.envAdded) }}
          />
        </div>
      </div>
    )
  }
  return (
    <LaunchBlock
      label={i18nT('pages.mcpManagement.details_runs')}
      commands={refusal.commands}
      envs={refusal.envs}
      complete={refusal.complete}
    />
  )
}

/** The id of a row's server-name element, so a button can name its server. */
export function serverNameId(name: string): string {
  return `mcp-row-name-${name}`
}

/** A labelled button's size and focus ring, shared by every button on the page. */
export const SHARE_BUTTON_CLASS =
  'shrink-0 px-3 py-1.5 font-medium focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)]'

/**
 * A row's on/off control in the table's Share column: one `<button
 * role="switch">`. The column header already says what it does, so the words
 * "Enable MCP sharing" are kept for the accessible name (followed by the row's
 * server name) but not drawn; the button pads the track so the target stays
 * comfortable, and hover and focus show on it.
 */
function ShareSwitch({
  on,
  disabled,
  labelId,
  labelledBy,
  describedBy,
  onClick,
}: {
  on: boolean
  disabled: boolean
  labelId: string
  labelledBy: string
  describedBy?: string
  onClick: () => void
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-labelledby={labelledBy}
      aria-describedby={describedBy}
      disabled={disabled}
      onClick={onClick}
      className="inline-flex shrink-0 items-center rounded-full p-1 transition-colors hover:bg-[var(--hover)] focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-[var(--accent)] disabled:cursor-not-allowed disabled:opacity-50"
    >
      <span id={labelId} className="sr-only">
        {i18nT('pages.mcpManagement.share_toggle')}
      </span>
      <span
        aria-hidden="true"
        className={[
          'relative inline-flex h-[20px] w-[34px] shrink-0 items-center rounded-full transition-colors',
          on ? 'bg-accent' : 'bg-[var(--border-strong,var(--border))]',
        ].join(' ')}
      >
        {/* The knob rides on the accent fill, so it keeps one light face in every theme. */}
        <span
          className={[
            'absolute h-[16px] w-[16px] rounded-full bg-white shadow transition-all',
            on ? 'left-[16px]' : 'left-[2px]',
          ].join(' ')}
        />
      </span>
    </button>
  )
}

/**
 * The one control a row offers: an "Enable MCP sharing" switch.
 *
 * Turning it on is one click: the page reads the launch and, if it can be
 * approved, writes the share carrying its `expected_launch` (see
 * `McpManagement`). Turning it off stops sharing, or takes the mark off while
 * page-wide sharing is off. A row whose launch was refused runs nothing
 * shared, so its switch reads off, beside an "Approve update" button carrying
 * the refused launch's own `expected_launch`; pressing the switch writes
 * nothing and moves focus to that button. A refused launch with no approvable identity offers no
 * button and says why, and its switch takes the leftover mark off.
 */
export function ShareAction({
  server,
  refusal,
  busy,
  reading,
  supported,
  describedBy,
  onShare,
  onConfirm,
  onStop,
}: {
  server: McpManagedServer
  /** Set when the gateway refused this shared server's current launch. */
  refusal?: LaunchRefusal
  busy: boolean
  /** True while this row's press is reading the launch it will pin. */
  reading: boolean
  supported: boolean
  describedBy?: string
  onShare: () => void
  onConfirm: (expectedLaunch: string) => void
  onStop: () => void
}) {
  const approveRef = useRef<HTMLButtonElement>(null)
  if (!server.can_stub) return null
  // Each control's accessible name is its visible label followed by the row's
  // server name, read from the page itself, so a voice command for the label
  // on screen matches it.
  const labelId = `mcp-share-label-${server.name}`
  const approveId = `mcp-approve-label-${server.name}`
  const rowName = serverNameId(server.name)
  // On, the switch always turns off. Off, it turns on only where sharing works.
  // A marked row that cannot share (an env a shared copy would withhold, or a
  // refused launch with no approvable identity) is not drawn as a switch: an
  // "on" switch beside "direct" or "Can't share" gives two answers. It gets
  // one plain Remove button, which takes the mark off.
  const cannotShare =
    server.stub &&
    (server.pooling_blocked_by_env === true || (refusal !== undefined && !launchIsApprovable(refusal)))
  if (cannotShare) {
    const removeId = `mcp-remove-label-${server.name}`
    return (
      <div className="flex min-w-0 flex-col items-end gap-1">
        <Btn
          type="button"
          className={SHARE_BUTTON_CLASS}
          disabled={busy}
          aria-labelledby={`${removeId} ${rowName}`}
          aria-describedby={describedBy}
          onClick={onStop}
        >
          <span id={removeId}>{i18nT('pages.mcpManagement.remove_mark')}</span>
        </Btn>
      </div>
    )
  }
  const toggle = (
    <ShareSwitch
      on={server.stub}
      disabled={busy || (!server.stub && (!supported || reading))}
      labelId={labelId}
      labelledBy={`${labelId} ${rowName}`}
      describedBy={describedBy}
      onClick={server.stub ? onStop : onShare}
    />
  )
  if (server.stub && refusal) {
    const approvable = launchIsApprovable(refusal)
    // A server that reached the list without a press was never approved, so
    // its first approval is not an "update".
    const firstApproval = refusal.reason === 'added_outside_dashboard'
    // A refused row runs nothing shared, so its switch reads off. Pressing it
    // WRITES NOTHING: a changed launch is approved only by its own Approve
    // button, after the person had the chance to read it, so the switch only
    // moves focus there. With no approvable identity there is nothing to
    // approve; that row is drawn with a Remove button above, never this switch.
    // An approvable row reads off (nothing runs shared). A row with no
    // approvable identity reads ON: it is still marked, and the press turns
    // that mark off, so the switch says what the press does.
    const refusedToggle = (
      <ShareSwitch
        on={false}
        disabled={busy}
        labelId={labelId}
        labelledBy={`${labelId} ${rowName}`}
        describedBy={describedBy}
        onClick={() => approveRef.current?.focus()}
      />
    )
    return (
      <div className="flex min-w-0 flex-wrap items-center justify-end gap-2">
        {refusedToggle}
        {approvable ? (
          <Btn
            ref={approveRef}
            type="button"
            primary
            className={SHARE_BUTTON_CLASS}
            disabled={busy || !supported}
            aria-labelledby={`${approveId} ${rowName}`}
            aria-describedby={describedBy}
            onClick={() => onConfirm(refusal.expected_launch as string)}
          >
            <Check size={14} aria-hidden="true" />
            <span id={approveId}>
              {i18nT(firstApproval ? 'pages.mcpManagement.approve_button' : 'pages.mcpManagement.new_command_button')}
            </span>
          </Btn>
        ) : null}
        {/* No approvable identity means no button; the row's hint says why. */}
      </div>
    )
  }
  return (
    <div className="flex min-w-0 flex-col items-end gap-1">
      {toggle}
      {reading && (
        <span role="status" className="text-[12px] text-[var(--muted)]">
          {i18nT('pages.mcpManagement.share_checking')}
        </span>
      )}
    </div>
  )
}
