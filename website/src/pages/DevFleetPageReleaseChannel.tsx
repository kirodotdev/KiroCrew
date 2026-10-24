/**
 * The release-channel row's parts: the version badge that carries the pin's
 * currency, the two columns a tag-detached tree cannot answer (PR and Behind),
 * the sentence a branch checkout gets when it occupies the reserved name, the
 * shared error surface, and the placeholder row that offers Create before any
 * worktree exists.
 *
 * It lives beside the facade rather than in it because the placeholder is a
 * SECOND row renderer: it has no path, and every real row's affordance assumes
 * one. `devFleetPageModel.ts` decides WHICH row a payload describes; this file
 * decides what that row says.
 */
import { type CSSProperties } from 'react'
import { Btn, Badge } from '../components/ui'
import ErrorNotice from '../components/ErrorNotice'
import { i18nT } from '../i18n/t'
import { type ReleaseChannel } from './devFleetPageModel'


/** The muted, tabular style the row's own numeric cells use. */
const MUTED_CELL: CSSProperties = {
  fontSize: 12.5,
  color: 'var(--muted)',
  fontVariantNumeric: 'tabular-nums',
  fontFamily: 'ui-monospace, SF Mono, Menlo, monospace',
}

/**
 * The adopted row's version badge.
 *
 * The lane is already in the row NAME (`release-channel-stable`), so the badge
 * carries only what the name cannot: which release the tree is actually sitting
 * on. `ok` when it is at the channel tip, `warn` when a newer release shipped.
 *
 * `version` is the tree's OWN release, never the channel tip: a badge fed the
 * resolved tip would flip to each new release as it ships while the tree stayed
 * put, so the row would name a build it does not contain. When the tree is
 * detached at no release tag at all the badge reads "not on a release" — the
 * tree's condition, so it cannot be mistaken for the resolver-error row, whose
 * badge keeps its version — and never the bare lane word: on a row already named
 * `release-channel-stable` a pill reading `stable` is the row's own name said
 * twice, and a reader takes it for a version.
 *
 * Behind the tip is stated in the TEXT, not only in the variant: `0.5.0 · latest
 * 0.5.3` — "latest", the update-banner word, not git's "tip", which read as a
 * branch head. A warn-orange pill and an ok-green pill carrying the same version
 * text told a colourblind or keyboard reader nothing, and the tip existed only on
 * hover. When the tip is unknown (the resolver failed) the version stands alone
 * and the tooltip says the tip could not be resolved.
 *
 * A resolver error is NOT handled here: it routes to the row's ErrorNotice, the
 * sole error surface, never a Badge title.
 */
export function ReleaseChannelBadge({ rc }: { rc: ReleaseChannel }) {
  const version = rc.version || null
  const tip = rc.tip_version || null
  // The tip is `tip_version` and nothing else. `ref` is the TREE's own tag, so
  // falling back to it would print the row's own version as "the tip" on the one
  // row where the resolver failed — asserting the channel is current when
  // nothing is known. No tip means the tip-unknown sentence, which interpolates
  // no version and no `?`.
  const title = rc.at_tip
    ? i18nT('pages.devFleetPage.release_channel_pinned_at', { lane: rc.lane, ref: rc.ref || '?' })
    // The behind cases state the fact and name no control. Closing the gap is
    // Remove followed by Create — two controls on two surfaces (Remove in the
    // row's detail panel, Create on the placeholder that appears afterwards) —
    // so naming one of them here would misdirect the operator, and there is no
    // single label that is the answer.
    : version
      ? tip
        ? i18nT('pages.devFleetPage.release_channel_on_older_release', { lane: rc.lane, version, tip })
        : i18nT('pages.devFleetPage.release_channel_on_release_tip_unknown', { lane: rc.lane, version })
      : tip
        ? i18nT('pages.devFleetPage.release_channel_on_no_release', { lane: rc.lane, tip })
        : i18nT('pages.devFleetPage.release_channel_on_no_release_tip_unknown', { lane: rc.lane })
  const text = version
    ? !rc.at_tip && tip
      ? i18nT('pages.devFleetPage.release_channel_version_behind_tip', { version, tip })
      : version
    : i18nT('pages.devFleetPage.release_channel_no_release')
  return (
    <Badge variant={rc.at_tip ? 'ok' : 'warn'} className="text-[10px] px-1.5 py-0" title={title}>
      {text}
    </Badge>
  )
}

/**
 * The PR cell on a channel row.
 *
 * A release worktree is detached at a tag, so it has no branch and can never
 * have a pull request. Rendered as an explicit "n/a" rather than the PR-less em
 * dash, which on every other row means "no PR yet" — a state that invites
 * waiting for one.
 */
export function ReleaseChannelPrCell() {
  return (
    <span style={{ ...MUTED_CELL, opacity: 0.5 }} title={i18nT('pages.devFleetPage.release_channel_has_no_pr')}>
      {i18nT('pages.devFleetPage.not_applicable_short')}
    </span>
  )
}

/**
 * The Behind cell on a channel row.
 *
 * The column counts commits behind main, and that figure on a release worktree
 * is large by construction (a shipped tag is behind main by every commit merged
 * since) and says nothing the operator can act on. Three attempts at a second
 * denominator in this cell (`↓3 tip`, `↓3 newer`, tooltips) all left two rows
 * reading `↓N` in one column with two meanings, so the cell says the column does
 * not apply — the same `n/a` the PR cell uses — and points at the badge, which
 * carries the row's currency in its text and tooltip. The payload carries no
 * channel distance at all, so there is nothing here that could regain a count.
 */
export function ReleaseChannelBehindCell() {
  return (
    <span style={{ ...MUTED_CELL, opacity: 0.5 }} title={i18nT('pages.devFleetPage.release_rows_measured_by_version')}>
      {i18nT('pages.devFleetPage.not_applicable_short')}
    </span>
  )
}

/**
 * This checkout holds the channel's reserved name but is on a branch, so it is
 * NOT a channel pin and gets none of the channel treatment. Said on the row
 * rather than as a second placeholder row, so one directory stays one row — and
 * said at all, because otherwise the channel simply has no row and no
 * explanation for why it cannot be created.
 *
 * A SENTENCE, not a pill. Three pill wordings ("Not a release worktree", "Not
 * the channel", "Name reserved — on a branch") each failed a cold read: a badge
 * has room for a verdict but not for the situation AND the way out, and a verdict
 * alone read as the pin degrading. So this is the same muted inline status line
 * the placeholder row uses for "No worktree yet — Create checks out X" — same
 * slot, same styling — and it carries both halves in visible text: the row is on
 * a branch, which is why the stable release cannot use this name, and freeing it
 * means renaming or removing the checkout. Not a warn Badge: nothing is broken
 * on this row; it is an ordinary feature worktree with an unlucky name. The
 * tooltip names the controls that actually exist (Prune merged, or git worktree
 * move/remove in a shell) — the row menu has no Remove or Rename, so the
 * sentence must not point at one.
 */
export function ReleaseChannelReservedName({ rc, name }: { rc: ReleaseChannel; name: string }) {
  return (
    <span
      data-testid={'release-channel-name-taken-' + rc.lane}
      title={i18nT('pages.devFleetPage.release_channel_name_taken_by_branch', { name, lane: rc.lane })}
      style={{ fontSize: 11.5, color: 'var(--muted)', overflow: 'hidden', textOverflow: 'ellipsis', minWidth: 0 }}
    >
      {i18nT('pages.devFleetPage.reserved_name_on_a_branch', { lane: rc.lane })}
    </span>
  )
}

/**
 * The sole error surface for a channel row: an ADOPTED row whose resolve()
 * failed, OR the reserved directory whose HEAD could not be read
 * (`channelErrorFor` folds both). A Badge title is not keyboard-reachable and
 * carries no agent hand-off, so every channel error routes here, matching the
 * placeholder row's own notice. Nothing on this page is typed, so the hand-off
 * loses nothing.
 */
export function ReleaseChannelRowError({ rc }: { rc: ReleaseChannel }) {
  return (
    <div style={{ margin: '2px 0 8px 32px' }}>
      <ErrorNotice
        message={i18nT('pages.devFleetPage.release_channel_unresolved', { lane: rc.lane, error: rc.error as string })}
        askAgent
        testId={'release-channel-error-' + rc.lane}
      />
    </div>
  )
}

/**
 * A channel with no worktree yet, rendered as a dimmed row carrying only Create.
 *
 * This placeholder is the ONLY place the feature is discoverable — there is no
 * header control — so it is listed even though nothing exists on disk. Kept out
 * of `worktrees` on the backend for the same reason it is a distinct renderer
 * here: it has no path, and every real row's affordance assumes one.
 */
export function ReleaseChannelPlaceholder({
  rc, busy, createError, onCreate, onDismissCreateError,
}: {
  rc: ReleaseChannel
  busy: boolean
  createError: string | null
  onCreate: () => void
  onDismissCreateError: () => void
}) {
  // Two states this row tells apart, and they read differently on purpose.
  // `error` is a GENUINE resolver failure — git could not be read — so it goes
  // through the shared ErrorNotice, the same surface every other error on this
  // page uses, and it blocks Create. `unpublished` is the benign documented
  // state: this checkout has fetched no release tag, which is ordinary
  // information, not an incident. The backend nulls `error` for that state, so a
  // benign row leaves `blocked` empty and Create enabled — and Create fetches
  // first, so it is the action that resolves the state.
  const blocked = rc.error || null
  const unpublished = !blocked && !!rc.unpublished
  // Framed for the ErrorNotice and the disabled-button tooltip. Only a genuine
  // failure produces it, so a benign checkout never renders as an incident.
  const blockedText = blocked
    ? i18nT('pages.devFleetPage.release_channel_unresolved', { lane: rc.lane, error: blocked })
    : null
  // What Create would check out, when the backend resolved one. Absent (no tag
  // fetched, or the resolver failed) there is NO version pill at all. It must not
  // fall back to the lane word — `stable` beside a row already named
  // `release-channel-stable` reads as a version called "stable" — and a "no
  // release" label is not the answer either: the status line beside it already
  // says nothing was fetched, so the pill only restated it, in the same words
  // the adopted off-tag badge uses for a DIFFERENT state (a real tree sitting on
  // no tag). One string for two states is one state too many; the adopted badge
  // keeps the label, the placeholder keeps the sentence.
  const wouldCreateAt = rc.version || rc.ref || null
  // The cell's own status line. A genuine failure shows a SHORT plain label and
  // hands the full text to the ErrorNotice below, so the cell can ellipsise
  // without ever cutting the sentence a user needs mid-clause; the benign and
  // resolved states read as plain informational text. The resolved state SAYS
  // what Create does, in visible text: before this, a version pill plus "no
  // worktree yet" left Create explained only by the pill's hover, and a reader
  // who did not hover saw a bare version beside a button with no stated effect.
  const statusText = blocked
    ? i18nT('pages.devFleetPage.release_channel_unresolved_short', { lane: rc.lane })
    : unpublished
      ? i18nT('pages.devFleetPage.release_channel_unpublished', { lane: rc.lane })
      : wouldCreateAt
        ? i18nT('pages.devFleetPage.no_worktree_yet_create_checks_out', { version: wouldCreateAt })
        : i18nT('pages.devFleetPage.no_worktree_yet')
  // Title only where the button cannot otherwise be understood. With a resolved
  // version the pill beside it already says what Create checks out, and the
  // success toast names provisioning as the next step — a second copy here
  // restated the pill's first clause. The blocked case needs one because a
  // disabled button cannot explain itself any other way; the unpublished case
  // needs one because its status line says "nothing fetched yet" and an enabled
  // button beside that sentence is only clickable with confidence once it says
  // that clicking IS the fetch.
  const createTitle = blockedText
    || (unpublished ? i18nT('pages.devFleetPage.release_channel_create_fetches', { lane: rc.lane }) : undefined)
  return (
    <div key={'rc-' + rc.lane}>
      {/* The row reads as a muted placeholder by dimming its cells directly, NOT
          the grid container: an opacity < 1 on the container composites the whole
          subtree as one group, and a descendant cannot exceed its ancestor's
          opacity, so a container opacity dims the Create button with it. The
          muting lives on the sibling cells — the pod-column dash, the worktree
          cell (name, version pill, status) and the already-dimmed n/a columns —
          so the actions cell holding Create sits outside any dimmed subtree at
          full contrast. Keep every opacity on a CELL; none on an ancestor of
          Create. */}
      {/* The 7-column template is the one DevFleetPage.tsx's header and every
          worktree row spell inline, so the placeholder reads as one aligned row of
          the same table. Desktop-only by design (AUTOSDE narrow-viewport-required,
          stated at the layout root): a lone responsive row would misalign. */}
      <div data-testid={'release-channel-placeholder-' + rc.lane} style={{ display: 'grid', gridTemplateColumns: '16px 84px minmax(0,1fr) 64px 48px 44px 212px', gap: 8, alignItems: 'center', padding: '5px 0', borderTop: '1px solid var(--border)', minHeight: 30, minWidth: 640 } as CSSProperties}>
        <span style={{ width: 15 }} />
        <span style={{ fontSize: 12.5, color: 'var(--muted)', opacity: 0.62 }}>{'\u2014'}</span>
        <div style={{ minWidth: 0, display: 'flex', alignItems: 'baseline', gap: 6, whiteSpace: 'nowrap', overflow: 'hidden', opacity: 0.62 } as CSSProperties}>
          <span style={{ fontFamily: 'ui-monospace, monospace', fontSize: 13.5, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis' }}>{rc.name}</span>
          {/* Dashed border, not a filled pill: this names a ref that has been
              RESOLVED but not checked out anywhere, so it must not read like the
              solid version badge an adopted row carries. It carries its own
              tooltip because only the Create button used to explain what the
              version means, so a user hovering the version itself — the thing
              they are trying to understand — got nothing. Rendered only with
              something to name; see `wouldCreateAt`. */}
          {wouldCreateAt ? (
            <span
              title={i18nT('pages.devFleetPage.release_channel_would_create_at', { lane: rc.lane, version: wouldCreateAt })}
              style={{ fontSize: 10, padding: '1px 6px', borderRadius: 999, border: '1px dashed var(--border)', color: 'var(--muted)', fontFamily: 'ui-monospace, monospace' }}
            >
              {wouldCreateAt}
            </span>
          ) : null}
          {/* Never the bare backend string, and never the error path: `rc.error`
              is git/resolver mechanism, so it belongs in the framed ErrorNotice
              below with the agent hand-off. This cell carries only a short plain
              status — a benign "no release yet", or a one-clause "could not be
              resolved" whose full text is in the notice — so ellipsising it can
              never cut the sentence a user is reading. Always muted: the danger
              styling lives on the ErrorNotice, not here. */}
          <span
            title={blockedText || undefined}
            style={{ fontSize: 11.5, color: 'var(--muted)', overflow: 'hidden', textOverflow: 'ellipsis', minWidth: 0 }}
          >
            {statusText}
          </span>
        </div>
        <ReleaseChannelPrCell />
        <span style={{ fontSize: 12.5, color: 'var(--muted)', opacity: 0.5 }}>{'\u2014'}</span>
        <span style={{ fontSize: 12.5, color: 'var(--muted)', opacity: 0.5 }}>{'\u2014'}</span>
        <div style={{ display: 'flex', gap: 6, justifyContent: 'flex-end', alignItems: 'center', minWidth: 0 } as CSSProperties}>
          <Btn onClick={onCreate} disabled={busy || !!rc.error} title={createTitle}>
            {busy ? i18nT('pages.devFleetPage.creating') : i18nT('pages.devFleetPage.create_worktree')}
          </Btn>
        </div>
      </div>
      {/* A resolver failure is the one error class on this page that reached the
          user only as tooltip and cell text — every other one goes through
          ErrorNotice with the agent hand-off. Row-scoped and BELOW the grid, not
          page-level: a page-level surface would fire on every mount for a
          checkout with no release tags at all (a shallow clone, a fork before its
          first release), which is a normal state and not an incident. Here it
          sits on the row that cannot resolve, where it is the answer to a
          question the operator is already asking. */}
      {blockedText ? (
        <div style={{ padding: '2px 0 8px 30px', minWidth: 640 } as CSSProperties}>
          <ErrorNotice message={blockedText} askAgent testId={'release-channel-error-' + rc.lane} />
        </div>
      ) : null}
      {/* A failed Create is a DIFFERENT event from a resolver failure — the row
          resolved fine, the click did not take — so it is its own notice with its
          own testId, never folded into the one above. It outlives the fleet
          refetch that follows the failure (which re-renders this placeholder
          unchanged) and goes only when the operator retries or dismisses it; the
          inputs are all in git and on the backend, so the hand-off is on. */}
      {createError ? (
        <div style={{ margin: '2px 0 8px 32px' }}>
          <ErrorNotice
            message={createError}
            askAgent
            onDismiss={onDismissCreateError}
            testId={'release-channel-create-error-' + rc.lane}
          />
        </div>
      ) : null}
    </div>
  )
}
