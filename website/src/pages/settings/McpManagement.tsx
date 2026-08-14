import { useMemo, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ExternalLink } from 'lucide-react'
import { api, type McpManagedServer } from '../../api/client'
import {
  Dialog,
  DialogBody,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '../../components/ui/dialog'
import { splitOnPlaceholder } from '../../apps/crew-companion/splitOnPlaceholder'
import { i18nT } from '../../i18n/t'

/**
 * MCP Management — two decisions, one per layer, and nothing else.
 *
 * Per server (the table): interpose Kiro Crew's stub. That alone is what lets the
 * server render its own UI, and it leaves the backend private to each session —
 * the useful state for a server that holds per-session state.
 *
 * Global (the card): route those stubs to ONE shared backend process. Sharing is
 * the only thing this switch does, and it can only act on servers that already
 * have a stub, so the two layers never overlap.
 *
 * The words matter here: "stub" is the per-server layer and "routing to a shared
 * backend" is the global one. Naming the per-server switch "route" would claim the
 * global layer's job for it, which is exactly the confusion this page has to avoid.
 *
 * There is deliberately no per-server sharing control. The previous page had one,
 * which is how an operator could end up with sharing "on" while the allowlist it
 * acted on was empty — a switch with no observable effect.
 */

const DOCS_URL =
  'https://github.com/kirodotdev/KiroCrew/blob/main/docs/architecture/design-notes/mcp-stub-decoupling.md'

type GatewayStatus = {
  enabled: boolean
  stub: string[]
  stub_count: number
  running: boolean
  ping_ok: boolean
  supported: boolean
}

function Switch({
  on,
  disabled,
  onClick,
  label,
  describedBy,
}: {
  on: boolean
  disabled?: boolean
  onClick: () => void
  label: string
  describedBy?: string
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      aria-label={label}
      aria-describedby={describedBy}
      disabled={disabled}
      onClick={onClick}
      className={[
        'relative inline-flex h-[22px] w-[38px] shrink-0 items-center rounded-full transition-colors',
        on ? 'bg-accent' : 'bg-[var(--border-strong,var(--border))]',
        disabled ? 'cursor-not-allowed opacity-50' : 'cursor-pointer',
      ].join(' ')}
    >
      <span
        className={[
          // The knob rides ON the accent fill, so it needs the same light face in
          // every theme; there is no token for that pairing (`--accent-fg` is for
          // text) and the app-scoped switches paint theirs from CSS we cannot use
          // from a settings page.
          'absolute h-[18px] w-[18px] rounded-full bg-white shadow transition-all',
          on ? 'left-[18px]' : 'left-[2px]',
        ].join(' ')}
      />
    </button>
  )
}

export function McpManagement() {
  const qc = useQueryClient()
  const [confirmSharing, setConfirmSharing] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const statusQ = useQuery<GatewayStatus>({
    queryKey: ['mcpGatewayStatus'],
    queryFn: () => api.mcpGatewayStatus(),
  })
  const serversQ = useQuery<{ servers: McpManagedServer[] }>({
    queryKey: ['mcpGatewayServers'],
    queryFn: () => api.mcpGatewayServers(),
  })

  const status = statusQ.data
  const servers = serversQ.data?.servers ?? []
  const stubCount = useMemo(() => servers.filter(s => s.stub).length, [servers])
  const eligibleCount = useMemo(() => servers.filter(s => s.can_stub).length, [servers])
  const supported = status?.supported ?? true

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['mcpGatewayStatus'] })
    void qc.invalidateQueries({ queryKey: ['mcpGatewayServers'] })
  }

  // Both endpoints persist to config.json BEFORE the in-process apply, so a 500
  // means "saved but not live" — not "nothing happened". Claiming nothing was
  // saved would leave the operator with a setting that quietly takes effect on
  // the next restart, so the failure path refetches and says so.
  const onApplyError = (key: string) => () => {
    invalidate()
    setError(i18nT(key))
  }

  const setStub = useMutation({
    mutationFn: ({ name, stub }: { name: string; stub: boolean }) =>
      api.mcpGatewaySetStub(name, stub),
    // A 200 means the config was persisted, NOT that the broker reached the
    // wanted state: a failed start still answers 200 with `applied: false`.
    // Reporting that as success would draw a live-looking switch over routing
    // that never came up.
    onSuccess: res => {
      invalidate()
      if (res && res.applied === false) {
        setError(i18nT('pages.mcpManagement.stub_not_live'))
      }
    },
    onError: onApplyError('pages.mcpManagement.stub_failed'),
  })

  const setSharing = useMutation({
    mutationFn: (enabled: boolean) => api.mcpGatewayEnable(enabled),
    // Same asymmetry: enabling sharing can persist and still leave the broker
    // unreachable, and `ping_ok` is the only thing that says so.
    onSuccess: (res, enabled) => {
      invalidate()
      if (enabled && !res.ping_ok) {
        setError(i18nT('pages.mcpManagement.sharing_not_live'))
      }
    },
    onError: onApplyError('pages.mcpManagement.sharing_failed'),
  })

  const busy = setStub.isPending || setSharing.isPending
  // An unsupported platform must never TRAP an operator in a state they cannot
  // leave: a config carried over from another machine can arrive with sharing on
  // or servers stubbed, so turning things OFF stays available and only turning
  // them ON is blocked. Enabling sharing over an empty stub set is blocked for
  // the same reason it no longer exists as a state: it would do nothing.
  const canEnableSharing = supported && stubCount > 0

  return (
    <div className="space-y-4">
      {/* No <h2> here: the Developer tab header already names this surface, and a
          second copy of the title read as two stacked headings. */}
      <header>
        <p className="max-w-[76ch] text-[13px] leading-relaxed text-[var(--muted)]">
          {/*
           * One key holds the whole sentence with a {{link}} placeholder, rather
           * than joining a lede key and a link-label key side by side. Halves
           * that each end mid-sentence cannot be reordered by a translator, and
           * plenty of languages need the link somewhere other than the end.
           */}
          {splitOnPlaceholder(i18nT('pages.mcpManagement.lede'), 'link').map((part, i) =>
            part === null ? (
              <a
                key="link"
                href={DOCS_URL}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 text-[var(--accent)] hover:underline"
              >
                {i18nT('pages.mcpManagement.learn_more')}
                <ExternalLink size={12} />
              </a>
            ) : (
              <span key={i}>{part}</span>
            ),
          )}
        </p>
      </header>

      {error && (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-lg border border-[var(--danger)] bg-[var(--danger-subtle,transparent)] px-3.5 py-2.5 text-[13px] text-[var(--text)]"
        >
          <AlertTriangle size={14} className="mt-0.5 shrink-0 text-[var(--danger)]" />
          <span>{error}</span>
        </div>
      )}

      {/* Global: route every stub to one shared backend. */}
      <section className="rounded-xl border border-[var(--border)] bg-[var(--card)] px-5 py-4">
        <div className="flex items-start gap-5">
          <div className="flex-1">
            <div className="text-[15px] font-semibold text-[var(--text)]">
              {i18nT('pages.mcpManagement.sharing_label')}
            </div>
            <p
              id="mcp-sharing-desc"
              className="mt-1.5 max-w-[64ch] text-[13px] leading-relaxed text-[var(--muted)]"
            >
              {i18nT('pages.mcpManagement.sharing_description')}
            </p>
            {!supported && (
              <p className="mt-2 text-[12.5px] text-[var(--muted)]">
                {i18nT('pages.mcpManagement.unsupported_platform')}
              </p>
            )}
            {/* A disabled control has to say why. This is the page's headline
                switch, so with nothing stubbed a first-time user's very first
                click silently did nothing and only the lede's last clause
                hinted at the gate. */}
            {supported && !status?.enabled && stubCount === 0 && (
              <p className="mt-2 text-[12.5px] text-[var(--muted)]">
                {i18nT('pages.mcpManagement.sharing_needs_a_stub')}
              </p>
            )}
            {/* Sharing left ON over an empty stub set is the exact "switch with
                no observable effect" state this page exists to eliminate —
                reachable by unstubbing the last server. Name it instead of
                showing a live switch that governs nothing. */}
            {supported && status?.enabled && stubCount === 0 && (
              <p className="mt-2 text-[12.5px] text-[var(--muted)]">
                {i18nT('pages.mcpManagement.sharing_on_but_nothing_stubbed')}
              </p>
            )}
          </div>
          <span className="shrink-0 whitespace-nowrap pt-1 font-mono text-[12px] text-[var(--muted)]">
            {i18nT('pages.mcpManagement.stubbed_of_total', {
              stubbed: stubCount,
              total: eligibleCount,
            })}
          </span>
          <Switch
            on={!!status?.enabled}
            disabled={
              busy || statusQ.isLoading || (!status?.enabled && !canEnableSharing)
            }
            label={i18nT('pages.mcpManagement.sharing_label')}
            describedBy="mcp-sharing-desc"
            onClick={() => {
              setError(null)
              // Turning sharing ON changes the topology of every stubbed server
              // at once, so it asks first. Turning it OFF only ever narrows,
              // and a confirm on the safe direction trains people to click
              // through the dangerous one.
              if (!status?.enabled) setConfirmSharing(true)
              else setSharing.mutate(false)
            }}
          />
        </div>
      </section>

      {/* Per server: interpose the stub. */}
      <section className="overflow-hidden rounded-xl border border-[var(--border)] bg-[var(--card)]">
        {/* Both switches on this page are next-chat scoped: the apply path
            rebuilds the provider factory and drains the warm pool, but
            deliberately does not touch live sessions — a running session has
            already sent session/new and cannot be retrofitted. Say so, because
            the row toggle is the control people use routinely and a silently
            partial apply reads as a broken switch. */}
        <p className="border-b border-[var(--border)] px-4 py-2.5 text-[12.5px] text-[var(--muted)]">
          {i18nT('pages.mcpManagement.open_sessions_note')}
        </p>
        <table className="w-full border-collapse">
          <thead>
            <tr>
              <th className="w-[34%] px-4 pb-2.5 pt-3.5 text-left text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
                {i18nT('pages.mcpManagement.col_server')}
              </th>
              <th className="w-[34%] px-4 pb-2.5 pt-3.5 text-left text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
                {i18nT('pages.mcpManagement.col_used_by')}
              </th>
              <th className="w-[16%] px-4 pb-2.5 pt-3.5 text-left text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
                {i18nT('pages.mcpManagement.col_state')}
              </th>
              <th className="w-[16%] px-4 pb-2.5 pt-3.5 text-right text-[11px] font-semibold uppercase tracking-wider text-[var(--muted)]">
                {i18nT('pages.mcpManagement.col_stub')}
              </th>
            </tr>
          </thead>
          <tbody>
            {servers.map(s => {
              const shared = s.stub && !!status?.enabled
              return (
                <tr key={s.name} className="border-t border-[var(--border)]">
                  <td
                    className={[
                      'px-4 py-3 font-mono text-[13px]',
                      s.stub ? 'text-[var(--text)]' : 'text-[var(--muted)]',
                    ].join(' ')}
                  >
                    {s.name}
                  </td>
                  <td className="px-4 py-3 text-[12.5px] text-[var(--muted)]">
                    {s.agents.join(', ')}
                  </td>
                  <td className="px-4 py-3">
                    <span
                      className={[
                        'inline-block rounded-full px-2 py-0.5 font-mono text-[11px]',
                        shared
                          ? 'bg-[var(--accent-subtle,transparent)] text-[var(--accent)]'
                          : 'border border-[var(--border)] text-[var(--muted)]',
                      ].join(' ')}
                    >
                      {!s.can_stub
                        ? i18nT('pages.mcpManagement.state_no_stub')
                        : shared
                          ? i18nT('pages.mcpManagement.state_shared')
                          : s.stub
                            ? i18nT('pages.mcpManagement.state_stub')
                            : i18nT('pages.mcpManagement.state_direct')}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right">
                    <Switch
                      on={s.stub}
                      disabled={!s.can_stub || busy || (!s.stub && !supported)}
                      label={i18nT('pages.mcpManagement.stub_aria', { name: s.name })}
                      onClick={() => {
                        setError(null)
                        setStub.mutate({ name: s.name, stub: !s.stub })
                      }}
                    />
                  </td>
                </tr>
              )
            })}
            {serversQ.isError && (
              <tr className="border-t border-[var(--border)]">
                <td colSpan={4} className="px-4 py-6 text-center text-[13px] text-[var(--danger)]">
                  {/* Distinct from the empty state on purpose: a failed request
                      knows nothing about the operator's servers, and saying
                      "none are configured" would be a claim we cannot make. */}
                  {i18nT('pages.mcpManagement.servers_failed')}
                </td>
              </tr>
            )}
            {servers.length === 0 && !serversQ.isLoading && !serversQ.isError && (
              <tr className="border-t border-[var(--border)]">
                <td colSpan={4} className="px-4 py-6 text-center text-[13px] text-[var(--muted)]">
                  {i18nT('pages.mcpManagement.no_servers')}
                </td>
              </tr>
            )}
          </tbody>
        </table>
        <div className="border-t border-[var(--border)] px-4 py-3 text-[12.5px] leading-relaxed text-[var(--muted)]">
          {i18nT('pages.mcpManagement.legend')}
        </div>
      </section>

      <ConfirmSharing
        open={confirmSharing}
        stubCount={stubCount}
        busy={setSharing.isPending}
        onCancel={() => setConfirmSharing(false)}
        onConfirm={() => {
          setConfirmSharing(false)
          setSharing.mutate(true)
        }}
      />
    </div>
  )
}

function ConfirmSharing({
  open,
  stubCount,
  busy,
  onCancel,
  onConfirm,
}: {
  open: boolean
  stubCount: number
  busy: boolean
  onCancel: () => void
  onConfirm: () => void
}) {
  // Built on the repo's Radix Dialog rather than a bare `<div role="dialog">`:
  // that primitive owns the focus trap, initial focus, Escape-to-dismiss and
  // focus return. Hand-rolling the markup looked identical but let a keyboard
  // user Tab into the page behind the overlay and gave them no way out.
  return (
    <Dialog
      open={open}
      onOpenChange={next => {
        if (!next && !busy) onCancel()
      }}
    >
      <DialogContent maxWidth={520}>
        <DialogHeader>
          <DialogTitle>{i18nT('pages.mcpManagement.confirm_title')}</DialogTitle>
        </DialogHeader>
        <DialogBody>
          <DialogDescription className="text-text">
            {i18nT('pages.mcpManagement.confirm_lede', { count: stubCount })}
          </DialogDescription>
          <ul className="mt-2.5 list-disc space-y-1.5 pl-5 text-[13.5px] leading-relaxed text-muted">
            <li>{i18nT('pages.mcpManagement.confirm_stateful')}</li>
            <li>{i18nT('pages.mcpManagement.confirm_restart')}</li>
            <li>{i18nT('pages.mcpManagement.confirm_reversible')}</li>
          </ul>
        </DialogBody>
        <DialogFooter className="justify-between">
          <a
            href={DOCS_URL}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1 text-[13px] text-accent hover:underline"
          >
            {i18nT('pages.mcpManagement.learn_more_docs')}
            <ExternalLink size={12} />
          </a>
          <div className="flex gap-2.5">
            <button
              type="button"
              onClick={onCancel}
              className="rounded-md border border-border px-3.5 py-2 text-[13.5px] text-text"
            >
              {i18nT('pages.mcpManagement.cancel')}
            </button>
            <button
              type="button"
              autoFocus
              disabled={busy}
              onClick={onConfirm}
              className="rounded-md bg-accent px-3.5 py-2 text-[13.5px] font-medium text-accent-fg disabled:opacity-60"
            >
              {i18nT('pages.mcpManagement.confirm_turn_on')}
            </button>
          </div>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export default McpManagement
