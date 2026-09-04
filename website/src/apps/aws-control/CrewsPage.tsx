/**
 * AWS Control - the remote crews pane.
 *
 * A remote crew is a Kiro Crew gateway the owner deployed into their OWN AWS
 * account as a service their customers reach: one CloudFormation stack per crew,
 * one ECS service inside it, behind the shared load balancer the base stack owns.
 * This pane answers what exists and lets the owner open one. It creates nothing.
 *
 * THE NAME IS OVERLOADED and the copy here is what keeps it apart. On the Agents
 * page a "crew" is a LOCAL agent and its card component is literally called
 * `CrewCard`. Ours are remote and run on someone else's machine, in the cloud, for
 * other people. The rail says "Crews" (the reader is inside the AWS app, so the
 * short word is unambiguous there) and every surface below says "remote crews",
 * with a blurb that names the other kind explicitly rather than trusting the
 * adjective to carry it.
 *
 * The detail view is VIEW STATE in this pane, not a route: `BuiltinAppRoute`
 * resolves only single-segment routes, which is the same reason the drive page is
 * view state inside `AwsControlPage`. A breadcrumb returns.
 *
 * The card shape follows the Agents page's `CrewCard`: a fixed-height header
 * block, then a 2x2 grid of labelled facts under a divider. Same shape, this
 * pane's own four facts - the same relationship `DrivePage` has with
 * `LibraryTable`'s header chrome. The header height is fixed for the reason that
 * card documents: a badge is taller than plain text, so a card carrying one would
 * push its fact grid below its neighbour's and the row would read as ragged. The
 * NUMBER differs (38px, not 54px) because that card reserves two description
 * lines and a remote crew has no description field in the wire shape - here the
 * block holds one name line plus one identity line.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  Server, RefreshCw, ChevronLeft, Database, Layers, Globe, Boxes, CloudOff,
} from 'lucide-react'
import { Btn, Badge, EmptyState, ContentSkeleton } from '../../components/ui'
import Clickable from '../../components/Clickable'
import { i18nT } from '../../i18n/t'
import { fmtNumber } from '../../i18n/format'
import { awsControlApi, AwsControlError } from './api'
import { CopyBtn, PaneHeader, AwsErrorNotice } from './shared'
import type { CrewMemoryMode, RemoteCrew } from './types'

/* Literal-key maps from value → full catalog key, so no i18nT() call assembles a
 * key by interpolation (dynamicKeys gate). Same shape the drive page uses. */

/**
 * What the stack's `Memory` parameter means, in words.
 *
 * `''` maps to "Unknown" and MUST NOT fall through to chatbot. The backend
 * returns empty for a stack deployed before the parameter existed, deliberately,
 * so this pane can say it does not know - defaulting would state a fact about
 * someone's live deployment that nothing was ever read from.
 */
const MODE_LABEL_KEY: Record<CrewMemoryMode, string> = {
  chatbot: 'apps.awsControl.crews.mode_chatbot',
  persistent: 'apps.awsControl.crews.mode_persistent',
  '': 'apps.awsControl.crews.mode_unknown',
}

/**
 * Stack states that need saying, and how loudly.
 *
 * A settled stack (`CREATE_COMPLETE`, `UPDATE_COMPLETE`) is deliberately ABSENT:
 * it gets no badge at all, the same rule the accounts pane applies to a healthy
 * account row - the word appears exactly when something needs attention. That is
 * also what makes badge-carrying and badge-free cards share one row, which is why
 * the header block's height is fixed.
 *
 * The two delete states are `err`, not `warn`. A crew mid-delete is listed on
 * purpose (hiding it is how a half-deleted crew becomes a surprise on the next
 * bill), so it has to be visibly not healthy rather than quietly present.
 */
const STATUS_BADGE: Record<string, { key: string; variant: 'warn' | 'err' }> = {
  CREATE_IN_PROGRESS: { key: 'apps.awsControl.crews.status_creating', variant: 'warn' },
  UPDATE_IN_PROGRESS: { key: 'apps.awsControl.crews.status_updating', variant: 'warn' },
  UPDATE_ROLLBACK_COMPLETE: { key: 'apps.awsControl.crews.status_rolled_back', variant: 'warn' },
  ROLLBACK_COMPLETE: { key: 'apps.awsControl.crews.status_rolled_back', variant: 'warn' },
  DELETE_IN_PROGRESS: { key: 'apps.awsControl.crews.status_deleting', variant: 'err' },
  DELETE_FAILED: { key: 'apps.awsControl.crews.status_delete_failed', variant: 'err' },
}

/** Stack states that need no badge, listed so an UNKNOWN one cannot join them. */
const SETTLED = new Set(['CREATE_COMPLETE', 'UPDATE_COMPLETE'])

/**
 * The badge for one stack status, or null when the stack is settled.
 *
 * A status we do not recognise renders the raw AWS token in a muted badge rather
 * than nothing: the backend filters to a known set today, and if that set grows
 * the honest failure is an untranslated token the owner can search for, not a
 * card that quietly claims everything is fine.
 */
function statusBadge(status: string) {
  const known = STATUS_BADGE[status]
  if (known) return <Badge variant={known.variant} className="shrink-0 text-[11px]">{i18nT(known.key)}</Badge>
  if (!status || SETTLED.has(status)) return null
  return <Badge variant="muted" className="shrink-0 text-[11px]">{status}</Badge>
}

/** True while the stack is being torn down - the card must not read as healthy. */
function isDeleting(status: string): boolean {
  return status === 'DELETE_IN_PROGRESS' || status === 'DELETE_FAILED'
}

/**
 * What to show for `image` on a CARD: the digest, short.
 *
 * The template pins by digest and refuses a tag (`AllowedPattern`
 * `.+@sha256:[a-f0-9]{64}$`), so the real value is a registry host, a repository
 * path and 64 hex characters - over a hundred, with the part that identifies the
 * build at the very END. Truncating it left to right cuts exactly that part, and
 * every card in the grid then reads
 * `827641299822.dkr.ecr.us-west-2.amazonaws.com/…`: a label with no fact under
 * it, which is the same defect as showing the same stack name on every card.
 *
 * So the card renders the digest and the DETAIL renders the whole URI with a copy
 * button. This is a rendering of `image`, not a new field computed from it: no
 * value is invented, and the full one is a click away and on hover. A value
 * without a digest is shown whole, because `crews.py` returns the stack parameter
 * verbatim and this UI does not get to assume what an older stack put there.
 *
 * The `sha256:` prefix is dropped along with the registry: the template's pattern
 * makes it the same on every crew that can exist, so it spends a third of the
 * cell's width saying nothing, and the label already reads IMAGE.
 */
function imageDigest(image: string): string {
  const at = image.lastIndexOf('@')
  if (at < 0) return image
  const hex = image.slice(at + 1).replace(/^sha256:/, '')
  // Twelve, and no ellipsis: this is a short digest in the manner of a short
  // commit id, where twelve characters IS the conventional form rather than a
  // truncation of one. An ellipsis would also read as "the cell ran out of room",
  // which is the thing this function exists to avoid. Hover and the detail view
  // carry the whole reference.
  return hex.slice(0, 12)
}

/**
 * One labelled fact on a crew card: icon, what it is, what it says.
 *
 * `pr-0.5` is load-bearing with `truncate`, exactly as on the Agents card: an
 * italic glyph leans past its own advance width and `overflow:hidden` clips the
 * lean instead of showing an ellipsis, so "Unknown" rendered as "Unknowr".
 *
 * `wide` exists because this card's values are longer than that one's. The Agents
 * card pairs four SHORT values (a template name, a workspace, `Inherited`) so a
 * 2x2 grid fits them all. Two of ours are a URL and a container image reference,
 * and at half a card's width both rendered as pure ellipsis while the stack name
 * clipped to `smc-crew-s…` on every card at once, which is a label with no fact
 * under it. A wide cell spans both columns so those two read in full.
 */
function Fact({ icon, label, value, title, muted, wide, testId }: {
  icon: React.ReactNode
  label: string
  value: string
  /**
   * What hover reveals, when the rendered value is a SHORTENED form of a longer
   * one (the image digest). Defaults to the value, so a cell that shows its whole
   * value still gets a tooltip when it truncates.
   */
  title?: string
  /** An absent or unknown value: italic and muted, never dressed up as data. */
  muted?: boolean
  /** Span both columns, for a value too long to survive half a card's width. */
  wide?: boolean
  testId?: string
}) {
  return (
    <div className={`flex items-center gap-2 min-w-0 ${wide ? 'col-span-2' : ''}`}>
      <span className="text-muted">{icon}</span>
      <span className="min-w-0">
        <span className="block text-[10px] uppercase tracking-wider text-muted">{label}</span>
        <span className="block truncate pr-0.5 text-[12px]" data-testid={testId} title={title || value}>
          <span className={muted ? 'italic text-muted' : 'font-mono text-text'}>{value}</span>
        </span>
      </span>
    </div>
  )
}

/**
 * One remote crew in the grid. The whole card opens it.
 *
 * The four facts are the four the LIST route can actually answer: mode, stack,
 * endpoint, image. `running`/`desired` are absent from a list payload by design
 * (they cost one ECS call per crew), and `healthy` is derived from that pair, so
 * on this payload it is false for every crew - showing either here would report
 * every crew as down. The stack STATUS carries the state instead, and the detail
 * view is where the serving counts live.
 *
 * Region is the card's identity line rather than a fifth fact: it is where this
 * crew lives, context under the name in the same slot the account switcher puts
 * an account id under an account name.
 */
function CrewCard({ crew, onOpen }: { crew: RemoteCrew; onOpen: () => void }) {
  const dying = isDeleting(crew.stackStatus)
  return (
    <Clickable
      onClick={onOpen}
      aria-label={i18nT('apps.awsControl.crews.open', { name: crew.name })}
      data-testid="crew-card"
      data-crew={crew.name}
      data-status={crew.stackStatus}
      className={`group flex flex-col gap-3 rounded-lg border bg-card p-3.5 transition-all
                  hover:border-border-strong hover:shadow-md focus-ring
                  ${dying ? 'border-danger/40' : 'border-border'}`}
    >
      <div className="flex items-center gap-3">
        <span
          className={`flex h-[38px] w-[38px] shrink-0 items-center justify-center rounded-md ${
            dying ? 'bg-danger-subtle text-danger' : 'bg-accent-subtle text-accent'
          }`}
          aria-hidden="true"
        >
          <Server size={18} />
        </span>
        {/* Fixed height for the whole header block, so a card carrying a status
            badge cannot push its fact grid lower than a neighbour without one.
            One name line (20px) plus one identity line (16px) plus the 2px gap. */}
        <div className="flex h-[38px] min-w-0 flex-1 flex-col justify-center" data-testid="crew-card-header">
          {/* One line, never wrapping: the name truncates and the badge holds
              its size, so a long name cannot make this header taller than its
              neighbours' and knock the row out of alignment. */}
          <div className="flex items-center gap-2 min-w-0">
            <span className="truncate font-mono text-[14px] font-semibold leading-[20px] text-text-strong" data-testid="crew-name">
              {crew.name}
            </span>
            {statusBadge(crew.stackStatus)}
          </div>
          <span className="mt-0.5 block h-[16px] truncate font-mono text-[11px] leading-[16px] text-muted" data-testid="crew-region">
            {crew.region}
          </span>
        </div>
      </div>
      {/* Two short values share a row; the two that can run long get one each.
          A crew name may be 32 characters, so `smc-crew-<name>` clipped in a half
          cell for every crew but the shortest-named ones. */}
      <div className="grid grid-cols-2 gap-x-3 gap-y-2 border-t border-border pt-3" data-testid="crew-card-facts">
        <Fact
          icon={<Database className="lucide-inline" aria-hidden="true" />}
          label={i18nT('apps.awsControl.crews.fact_mode')}
          value={i18nT(MODE_LABEL_KEY[crew.memory])}
          muted={crew.memory === ''}
          testId="crew-mode"
        />
        <Fact
          icon={<Boxes className="lucide-inline" aria-hidden="true" />}
          label={i18nT('apps.awsControl.crews.fact_image')}
          value={crew.image ? imageDigest(crew.image) : i18nT('apps.awsControl.crews.unset')}
          title={crew.image}
          muted={!crew.image}
          testId="crew-image"
        />
        <Fact
          icon={<Layers className="lucide-inline" aria-hidden="true" />}
          label={i18nT('apps.awsControl.crews.fact_stack')}
          value={crew.stack}
          wide
          testId="crew-stack"
        />
        <Fact
          icon={<Globe className="lucide-inline" aria-hidden="true" />}
          label={i18nT('apps.awsControl.crews.fact_endpoint')}
          value={crew.controlBase || i18nT('apps.awsControl.crews.unset')}
          muted={!crew.controlBase}
          wide
          testId="crew-endpoint"
        />
      </div>
    </Clickable>
  )
}

/** One labelled row in the detail panel: a term, a value, and optional actions. */
function DetailRow({ label, children, testId }: {
  label: string
  children: React.ReactNode
  testId?: string
}) {
  return (
    <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-b border-border py-2 last:border-b-0" data-testid={testId}>
      <span className="w-[140px] shrink-0 text-[11px] uppercase tracking-wider text-muted">{label}</span>
      <span className="min-w-0 flex-1 break-all text-[13px] text-text">{children}</span>
    </div>
  )
}

/**
 * One crew, opened.
 *
 * This is the only surface that reads the serving counts, because reading them
 * costs an ECS call the listing deliberately does not make. `healthy` is trusted
 * HERE and nowhere else, for the same reason: on a detail payload the counts
 * behind it were actually fetched.
 */
function CrewDetail({ account, name, onBack }: {
  account: string
  name: string
  onBack: () => void
}) {
  const q = useQuery({
    queryKey: ['aws-control', 'crew', account, name],
    queryFn: () => awsControlApi.crew(account, name),
  })
  const err = q.error instanceof AwsControlError ? q.error : null
  // A crew that finished deleting between the grid rendering and this click is
  // gone, not broken: 404 gets its own sentence, and there is nothing to retry.
  const absent = err?.message === 'crew_absent'
  const mismatch = err?.message === 'account_mismatch'
  const crew = q.data

  return (
    <section data-testid="crew-detail">
      <button
        onClick={onBack}
        className="mb-1 inline-flex max-w-full items-center gap-1 border-none bg-transparent p-0 text-[13px] text-muted cursor-pointer hover:text-text focus-ring"
        data-testid="crew-detail-back"
      >
        <ChevronLeft size={14} className="shrink-0" aria-hidden="true" />
        <span className="min-w-0 truncate">{i18nT('apps.awsControl.crews.title')}</span>
      </button>
      <PaneHeader
        icon={<Server size={18} />}
        title={name}
        meta={crew ? statusBadge(crew.stackStatus) : undefined}
        actions={
          <Btn onClick={() => q.refetch()} disabled={q.isFetching} data-testid="crew-detail-refresh">
            <RefreshCw size={13} className={q.isFetching ? 'animate-spin' : ''} />
            {i18nT('apps.awsControl.page.refresh')}
          </Btn>
        }
      />

      {q.isLoading && <ContentSkeleton rows={3} />}

      {absent && (
        /* askAgent is ON here, like the other notices. Two earlier rounds rebutted
           this finding on the grounds that a crew which finished deleting leaves
           nothing to investigate. That is not the exception the rule grants: it
           turns the hand-off on "wherever the hand-off cannot destroy anything",
           which is about destruction (an unsaved draft), not about usefulness. And
           there IS something to hand off -- ErrorNotice is the one surface that
           carries route, failed endpoint, status and backend code to the agent, so
           it can tell the owner the stack is gone rather than leaving them to guess
           whether they mistyped the name. The back button above stays the direct
           way out; it is no longer the only one. */
        <AwsErrorNotice
          askAgent
          error={q.error}
          message={i18nT('apps.awsControl.crews.absent')}
          testId="crew-detail-absent"
        />
      )}
      {mismatch && (
        <AwsErrorNotice
          askAgent
          error={q.error}
          message={i18nT('apps.awsControl.crews.mismatch')}
          testId="crew-detail-mismatch"
        />
      )}
      <AwsErrorNotice
        askAgent
        error={q.error}
        message={q.isError && !absent && !mismatch ? i18nT('apps.awsControl.crews.detail_failed') : null}
        onRetry={() => q.refetch()}
        testId="crew-detail-error"
      />

      {crew && (
        <div className="rounded-lg border border-border bg-card px-4 py-2" data-testid="crew-detail-facts">
          <DetailRow label={i18nT('apps.awsControl.crews.fact_stack')} testId="crew-detail-stack">
            <span className="font-mono">{crew.stack}</span>
          </DetailRow>
          <DetailRow label={i18nT('apps.awsControl.crews.fact_mode')} testId="crew-detail-mode">
            <span className={crew.memory === '' ? 'italic text-muted' : ''} data-testid="crew-detail-mode-value">
              {i18nT(MODE_LABEL_KEY[crew.memory])}
            </span>
            {/* The one state that needs a sentence: an unknown mode is not a
                gap in this page, it is a fact about a stack too old to carry
                the answer, and without saying so the italic word reads as a
                failure to load. */}
            {crew.memory === '' && (
              <span className="mt-0.5 block text-[12px] text-muted" data-testid="crew-detail-mode-why">
                {i18nT('apps.awsControl.crews.mode_unknown_why')}
              </span>
            )}
          </DetailRow>
          <DetailRow label={i18nT('apps.awsControl.crews.detail_service')} testId="crew-detail-service">
            <span className="flex flex-wrap items-center gap-2">
              <span className="font-mono">{crew.service || i18nT('apps.awsControl.crews.unset')}</span>
              {crew.desired > 0 ? (
                <>
                  <span className="text-muted" data-testid="crew-detail-tasks">
                    {i18nT('apps.awsControl.crews.detail_tasks', {
                      running: fmtNumber(crew.running),
                      desired: fmtNumber(crew.desired),
                    })}
                  </span>
                  <Badge variant={crew.healthy ? 'ok' : 'err'} className="text-[11px]">
                    {i18nT(crew.healthy
                      ? 'apps.awsControl.crews.serving'
                      : 'apps.awsControl.crews.not_serving')}
                  </Badge>
                </>
              ) : (
                /* Zero desired tasks: nothing is meant to be running, so the
                   red "Not serving" badge would name a fault that is not one.
                   `healthy` is false here too, and saying so would be the same
                   mistake. */
                <span className="italic text-muted" data-testid="crew-detail-idle">
                  {i18nT('apps.awsControl.crews.detail_idle')}
                </span>
              )}
            </span>
          </DetailRow>
          <DetailRow label={i18nT('apps.awsControl.crews.fact_endpoint')} testId="crew-detail-endpoint">
            {crew.controlBase ? (
              <span className="flex flex-wrap items-center gap-2">
                <span className="min-w-0 break-all font-mono">{crew.controlBase}</span>
                <CopyBtn
                  text={crew.controlBase}
                  testId="crew-copy-endpoint"
                  ariaLabel={i18nT('apps.awsControl.crews.copy_endpoint')}
                />
              </span>
            ) : (
              <span className="italic text-muted">{i18nT('apps.awsControl.crews.unset')}</span>
            )}
          </DetailRow>
          <DetailRow label={i18nT('apps.awsControl.crews.fact_image')} testId="crew-detail-image">
            {crew.image ? (
              <span className="flex flex-wrap items-center gap-2">
                <span className="min-w-0 break-all font-mono">{crew.image}</span>
                <CopyBtn
                  text={crew.image}
                  testId="crew-copy-image"
                  ariaLabel={i18nT('apps.awsControl.crews.copy_image')}
                />
              </span>
            ) : (
              <span className="italic text-muted">{i18nT('apps.awsControl.crews.unset')}</span>
            )}
          </DetailRow>
          <DetailRow label={i18nT('apps.awsControl.console.setup_preview_region')} testId="crew-detail-region">
            <span className="font-mono">{crew.region}</span>
          </DetailRow>
        </div>
      )}
    </section>
  )
}

/**
 * The pane: every crew in the selected account as a card grid, or the one of
 * three non-error states that applies.
 *
 * The three states are distinct answers and must never collapse into one "no
 * crews" sentence, because the repair differs. `baseMissing` means no crew CAN
 * exist yet (the shared load balancer and cluster are not there); an empty list
 * with the base present means the account is ready and holds none; and a failed
 * read means neither of those is known, which is why it renders a notice rather
 * than an empty state.
 */
export function CrewsPane({ account }: { account: string }) {
  const [open, setOpen] = useState<string | null>(null)
  const q = useQuery({
    queryKey: ['aws-control', 'crews', account],
    queryFn: () => awsControlApi.crews(account),
    enabled: Boolean(account),
  })
  const data = q.data
  const err = q.error instanceof AwsControlError ? q.error : null
  // Not a generic failure. The profile behind this account now signs in to a
  // DIFFERENT AWS account, so the backend refused rather than report that
  // account's crews - listing them would be a disclosure. The fix is a
  // reconnect, not a retry, and the copy has to say so.
  const mismatch = err?.message === 'account_mismatch'

  if (open) {
    return <CrewDetail account={account} name={open} onBack={() => setOpen(null)} />
  }

  return (
    <section data-testid="crews-pane">
      <PaneHeader
        icon={<Server size={18} />}
        title={i18nT('apps.awsControl.crews.title')}
        meta={data && data.crews.length > 0 ? (
          <span className="text-[13px] text-muted" data-testid="crews-count">
            {i18nT('apps.awsControl.crews.count', { count: fmtNumber(data.crews.length) })}
          </span>
        ) : undefined}
        actions={
          <Btn onClick={() => q.refetch()} disabled={q.isFetching} data-testid="crews-refresh">
            <RefreshCw size={13} className={q.isFetching ? 'animate-spin' : ''} />
            {i18nT('apps.awsControl.page.refresh')}
          </Btn>
        }
      />

      {/* Said once, where the reader lands: these are not the crews on the
          Agents page. The adjective alone does not carry it - both kinds are
          called crews and one of them is a card grid too. */}
      <p className="mb-3 text-[12px] text-muted" data-testid="crews-blurb">
        {i18nT('apps.awsControl.crews.blurb')}
      </p>

      {q.isLoading && <ContentSkeleton rows={3} />}

      {/* A failed read is not an empty account. Rendered before either empty
          state so a 502 can never be mistaken for "you have no crews". */}
      {mismatch ? (
        <AwsErrorNotice
          askAgent
          error={q.error}
          message={i18nT('apps.awsControl.crews.mismatch')}
          testId="crews-mismatch"
        />
      ) : (
        <AwsErrorNotice
          askAgent
          error={q.error}
          message={q.isError ? i18nT('apps.awsControl.crews.load_failed') : null}
          onRetry={() => q.refetch()}
          testId="crews-error"
        />
      )}

      {/* No base stack: nothing is wrong and nothing is missing from this page.
          There is nowhere for a crew to run yet, which is a different sentence
          from "this account has no crews" and a different thing to do about it. */}
      {data?.baseMissing && (
        <EmptyState
          testId="crews-base-missing"
          icon={<CloudOff />}
          title={i18nT('apps.awsControl.crews.base_missing_title')}
          subtitle={i18nT('apps.awsControl.crews.base_missing_body')}
        />
      )}

      {data && !data.baseMissing && data.crews.length === 0 && (
        <EmptyState
          testId="crews-empty"
          icon={<Server />}
          title={i18nT('apps.awsControl.crews.empty_title')}
          subtitle={i18nT('apps.awsControl.crews.empty_body')}
        />
      )}

      {data && data.crews.length > 0 && (
        /* Two across at most, never three. A crew's facts include a hostname and
           a container image reference, and at a third of the content width every
           one of them rendered as an ellipsis - four labels with no facts under
           them. The card is the unit that has to be readable, not the number of
           them that fit on a line. */
        <div
          className="grid grid-cols-1 gap-3 sm:grid-cols-2"
          data-testid="crews-grid"
        >
          {data.crews.map((c) => (
            <CrewCard key={c.name} crew={c} onOpen={() => setOpen(c.name)} />
          ))}
        </div>
      )}
    </section>
  )
}
