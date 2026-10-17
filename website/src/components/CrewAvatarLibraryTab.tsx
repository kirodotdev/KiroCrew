/**
 * The avatar builder's Library pane — pick, import or delete an appearance pack.
 *
 * A pack is art somebody else drew: the crew record names it by id and the
 * server serves each state's frame, so the whole of "wearing a pack" is one
 * string. That is why this pane holds no draft the way the ghost and picture
 * panes do — clicking a card is the edit, and Apply commits the id.
 *
 * Every format the library holds is selectable: core ships the Lottie and sprite
 * players (`components/appearancePacks/`), so a crew wears any pack. The grid
 * branches on the format for COST, not capability: an svg card's thumbnail is one
 * `<img>` on the per-slot route and reads no pack, while a lottie or sprite card
 * draws through `PackAvatar` (the slot route serves the first as JSON and the
 * second as a whole sheet, neither of which an `<img>` can show). Detail inlines
 * every file in the pack, which is why the svg cards stay off it.
 *
 * The list is re-read on every open rather than cached. Import and delete both
 * change it, and a stale grid here shows a pack that is gone or hides one the
 * user just installed — the two mistakes this pane must not make.
 */
import { useCallback, useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Trash2, Upload } from 'lucide-react'
import { Btn } from './ui'
import CrewAvatar from './CrewAvatar'
import PackAvatar from './appearancePacks/PackAvatar'
import { useInvalidatePackDetail } from '../hooks/usePackDetail'
import ErrorNotice from './ErrorNotice'
import { api } from '../api/client'
import {
  BUILTIN_PACK_ID,
  MAX_BUNDLE_BYTES,
  bundleFromText,
  packSlotUrl,
  packSummariesFrom,
  type AppearancePackSummary,
} from '../lib/appearancePacks/library'

/** Literal catalog keys, indexed rather than assembled — same extractor
 *  constraint as the builder's own label maps (see `dynamicKeys.test.ts`). */
const FORMAT_LABEL_KEYS: Record<string, string> = {
  svg: 'components.avatarBuilder.lib_format_svg',
  lottie: 'components.avatarBuilder.lib_format_lottie',
  sprite: 'components.avatarBuilder.lib_format_sprite',
}

/** How tall the scroll-edge fade is. Enough to read as "more below" without
 *  dimming a whole card. */
const FADE_PX = 24

/**
 * The gradient STOPS that fade whichever edge of the grid has content past it,
 * or `null` for a list that fits — which carries no mask at all, so a view with
 * nothing hidden is never dimmed.
 *
 * Returns the stops rather than the finished `linear-gradient(...)` so the CSS
 * value is assembled in a `style` prop, which is where every other gradient in
 * this tree is written.
 */
function fadeStops({ top, bottom }: { top: boolean; bottom: boolean }): string | null {
  if (!top && !bottom) return null
  const stops = ['#000 0']
  if (top) stops.splice(0, 1, 'transparent 0', `#000 ${FADE_PX}px`)
  if (bottom) stops.push(`#000 calc(100% - ${FADE_PX}px)`, 'transparent 100%')
  else stops.push('#000 100%')
  return stops.join(', ')
}

/** The crews named by a 409 `pack_in_use` body, or `null` when the rejection was
 *  something else. The names are the whole point of that refusal: "in use" with
 *  no list leaves the user hunting a roster for a face they cannot see. */
function wearersFrom(error: unknown): string[] | null {
  const status = (error as { status?: unknown } | null)?.status
  if (status !== 409) return null
  const body = (error as { body?: unknown }).body
  if (typeof body !== 'string') return null
  try {
    const parsed = JSON.parse(body) as { crews?: unknown }
    const crews = Array.isArray(parsed.crews) ? parsed.crews.filter(c => typeof c === 'string') : []
    return crews as string[]
  } catch {
    return []
  }
}

export default function CrewAvatarLibraryTab({
  open,
  name,
  selectedId,
  onSelect,
}: {
  /** True while the builder dialog is showing this pane's dialog. The list is
   *  read on every transition to true, so a pack imported or deleted in a
   *  previous opening cannot linger. */
  open: boolean
  /** Crew name — the seed of the built-in pack's locally composed face. */
  name: string
  /** The pack the draft currently names, or null. */
  selectedId: string | null
  /** null clears the draft's pack — what a delete of the selected pack means. */
  onSelect: (id: string | null) => void
}) {
  const { t } = useTranslation()
  const [packs, setPacks] = useState<AppearancePackSummary[] | null>(null)
  /** The library could not be read. Its own state because it replaces the grid,
   *  where the two below sit above a grid that is still usable. */
  const [loadError, setLoadError] = useState('')
  /** What is wrong with the FILE the user picked — not a bundle, or over the
   *  size cap.
   *
   *  Deliberately not an error and deliberately not an `ErrorNotice`: nothing
   *  failed, the agent cannot help, and the fix is to pick a different file.
   *  `errors-use-error-notice` says a validation hint must not be dressed as an
   *  error, so this renders as plain muted text beside the control that produced
   *  it, while `requestError` and `loadError` — where something really did
   *  fail — keep the notice. */
  const [pickHint, setPickHint] = useState('')
  const invalidatePackDetail = useInvalidatePackDetail()
  /** Packs whose art failed to render as a thumbnail. Keyed by id so a re-import
   *  under the same id gets a fresh chance (the import path clears it). */
  const [brokenArt, setBrokenArt] = useState<ReadonlySet<string>>(() => new Set())
  const markBroken = useCallback(
    (id: string) => setBrokenArt(prev => (prev.has(id) ? prev : new Set(prev).add(id))),
    [],
  )
  /** A request the server or the transport refused. */
  const [requestError, setRequestError] = useState('')
  /** The armed confirm's SAFE button, focused when a delete arms. Arming unmounts
   *  the Delete link the user just clicked, which drops keyboard focus to the body
   *  in the middle of a destructive flow — a keyboard user then tabs from the top
   *  of the dialog to find out what happened. Focus goes to "Keep" rather than to
   *  the confirm: landing on the destructive half means a stray Enter completes a
   *  delete the user has not read yet. */
  const keepRef = useRef<HTMLButtonElement | null>(null)
  /** Which pack's delete is armed. A pack is destroyed by two clicks, never one:
   *  the control sits on a card the user is also clicking to SELECT. */
  const [armedDelete, setArmedDelete] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)
  /** Monotonic load generation: only the latest read may land, so a slow list
   *  from a previous opening cannot overwrite a fresh one. */
  const loadGen = useRef(0)
  /** Which edges of the card grid have more content past them.
   *
   *  The grid is a fixed-height scroll region, so at phone width a row sits half
   *  past its edge — and a card cut at a hard border reads as a LAYOUT BUG, not
   *  as a list that scrolls (three separate readers called it clipped). A fade on
   *  whichever edge has more behind it is what tells those two apart. */
  const [fade, setFade] = useState({ top: false, bottom: false })
  const gridRef = useRef<HTMLDivElement | null>(null)
  const readFade = useCallback((el: HTMLElement | null) => {
    if (!el) return
    // 2px, not 0: a fractional scroll offset otherwise leaves a fade showing at
    // a hard end of the list.
    setFade({
      top: el.scrollTop > 2,
      bottom: el.scrollTop + el.clientHeight < el.scrollHeight - 2,
    })
  }, [])

  /** Read the library, and hand the rows back: the import path has to look the
   *  just-installed pack up in the FRESH list to learn its format. */
  const load = useCallback(async (): Promise<AppearancePackSummary[]> => {
    const gen = ++loadGen.current
    setLoadError('')
    try {
      const rows = packSummariesFrom(await api.appearances.list())
      if (gen === loadGen.current) setPacks(rows)
      return rows
    } catch (e) {
      if (gen !== loadGen.current) return []
      setPacks([])
      setLoadError(e instanceof Error && e.message ? e.message : t('components.avatarBuilder.lib_error'))
      return []
    }
  }, [t])

  useEffect(() => {
    if (!open) return
    setPickHint('')
    setRequestError('')
    setArmedDelete(null)
    void load()
  }, [open, load])

  // Measured AFTER the rows are laid out, not in the ref callback: at ref time
  // the grid has no height yet, so an overflowing library read as "nothing
  // below" and showed no fade until the first scroll — the one moment the fade
  // exists to get ahead of. Re-run whenever the row set changes, since an import
  // or a delete changes what overflows.
  useEffect(() => { readFade(gridRef.current) }, [packs, readFade])

  useEffect(() => { if (armedDelete) keepRef.current?.focus() }, [armedDelete])

  const pickBundle = async (file: File | undefined | null) => {
    setPickHint('')
    setRequestError('')
    if (!file) return
    if (file.size > MAX_BUNDLE_BYTES) {
      setPickHint(t('components.avatarBuilder.lib_import_too_large'))
      return
    }
    setBusy(true)
    try {
      const parsed = bundleFromText(await file.text())
      if (!parsed.ok) {
        setPickHint(
          t(
            parsed.reason === 'too_large'
              ? 'components.avatarBuilder.lib_import_too_large'
              : 'components.avatarBuilder.lib_import_unreadable',
          ),
        )
        return
      }
      const result = await api.appearances.importBundle(parsed.bundle)
      const rows = await load()
      // Select what was just installed — an import whose only visible effect is
      // one more card reads as having done nothing to this crew. Every format the
      // library holds is wearable, so nothing gates this on the format any more.
      // The pack's art may also have CHANGED under an id already on screen, so
      // drop the cached read before anything draws it.
      const installed = rows.find(pack => pack.id === result.id)
      if (!installed) return
      invalidatePackDetail(installed.id)
      setBrokenArt(prev => {
        if (!prev.has(installed.id)) return prev
        const next = new Set(prev)
        next.delete(installed.id)
        return next
      })
      onSelect(installed.id)
    } catch (e) {
      // The server's own message: it names WHICH check the bundle failed, and
      // "invalid bundle" alone gives the user nothing to fix.
      setRequestError(
        e instanceof Error && e.message ? e.message : t('components.avatarBuilder.lib_import_unreadable'),
      )
    } finally {
      setBusy(false)
    }
  }

  const remove = async (id: string, label: string) => {
    setPickHint('')
    setRequestError('')
    setBusy(true)
    // Cleared BEFORE the request, not after it.
    //
    // Clearing afterwards left a window the length of the round-trip in which
    // the draft still named a pack that was being deleted — and Apply is gated
    // only on `packId`, which the parent owns, so an Apply during that window
    // persisted a reference to a pack that no longer exists. Clearing first
    // closes the window with the guard Apply already has, rather than by
    // threading this tab's `busy` up into the dialog's footer.
    const wasSelected = selectedId === id
    if (wasSelected) onSelect(null)
    try {
      await api.appearances.remove(id)
      // Forget the cached read: any avatar still pointed at this id must now
      // report a load failure and fall back to the seeded ghost, rather than
      // keep drawing art the library no longer holds.
      invalidatePackDetail(id)
      setArmedDelete(null)
      await load()
    } catch (e) {
      // The pack survived — a 409 while a crew wears it is the ordinary case —
      // so a draft that named it is put back. Safe to restore unconditionally:
      // every selector is disabled while `busy`, so nothing else can have been
      // picked in the meantime.
      if (wasSelected) onSelect(id)
      const wearers = wearersFrom(e)
      setRequestError(
        // A 409 says the pack is worn; whether it says BY WHOM depends on a body
        // this client may not be able to read. Naming the crews is the useful
        // message, but the fallback cannot be the same sentence with nothing
        // after its colon — the outcome has to survive an unreadable body, which
        // is the whole reason a 409 is not folded into the generic failure.
        wearers?.length
          ? t('components.avatarBuilder.lib_in_use', {
              name: label,
              crews: wearers.join(', '),
            })
          : wearers
            ? t('components.avatarBuilder.lib_in_use_unknown', { name: label })
            : e instanceof Error && e.message
              ? e.message
              : t('components.avatarBuilder.lib_delete_failed'),
      )
      setArmedDelete(null)
    } finally {
      setBusy(false)
    }
  }

  /** The grid's scroll-edge mask for this render, or null when nothing is hidden. */
  const fadeMask = fadeStops(fade)

  const card = (pack: AppearancePackSummary) => {
    const selected = selectedId === pack.id
    const formatKey = FORMAT_LABEL_KEYS[pack.format]
    return (
      <div
        key={pack.id}
        className={`flex flex-col gap-1.5 rounded-lg border-2 p-2 transition-colors ${
          selected ? 'border-ring bg-accent-subtle' : 'border-border'
        }`}
        data-testid={`avatar-pack-card-${pack.id}`}
      >
        <button
          type="button"
          role="option"
          aria-selected={selected}
          disabled={busy}
          onClick={() => onSelect(pack.id)}
          // A pointer cursor and a hover ring, because the first-run reader rated
          // clicking a card a GUESS — the grid looked like a display of what is
          // installed rather than a picker, which is the PR's main path.
          className="flex cursor-pointer flex-col items-center gap-1.5 rounded-md text-center ring-offset-2 ring-offset-bg transition-shadow hover:ring-2 hover:ring-ring disabled:cursor-not-allowed"
          data-testid={`avatar-pack-select-${pack.id}`}
        >
          {pack.id === BUILTIN_PACK_ID ? (
            // The built-in pack's art is this bundle's own ghost; the slot route
            // answers 404 for it on purpose.
            <CrewAvatar seed={name} avatar={{ kind: 'ghost' }} size={72} className="rounded-lg" />
          ) : pack.format === 'svg' ? (
            // An SVG pack's thumbnail is ONE per-slot request and no pack read —
            // which is why the format is worth branching on here: the detail route
            // inlines every file in the pack, so drawing this whole grid through
            // it would load N packs to show N frames.
            <img
              src={packSlotUrl(pack.id, 'idle')}
              alt=""
              aria-hidden="true"
              width={72}
              height={72}
              className="h-[72px] w-[72px] rounded-lg border border-border bg-bg-elevated object-cover"
              data-testid={`avatar-pack-thumb-${pack.id}`}
            />
          ) : brokenArt.has(pack.id) ? (
            // The renderer could not draw this pack (unreadable, malformed, or a
            // sheet whose rows do not match its map). A blank card reads as a
            // layout bug; the notice says "installed, but no picture", and the
            // card stays selectable so the crew editor's own warning can name it.
            // It is an `ErrorNotice` because it reports a render that FAILED —
            // where `pickHint` (a validation hint, nothing failed) is not.
            // No hand-off: the failure is one pack's art inside an unsaved avatar
            // draft, and the recovery is the Import button beside this grid,
            // which the message names — an agent has nothing to act on here.
            <div
              className="flex h-[72px] w-[72px] items-center justify-center rounded-lg border border-border bg-bg-elevated px-1"
              data-testid={`avatar-pack-noart-${pack.id}`}
            >
              {/* 10px is the frontend's typography floor; the copy is sized to wrap
                  on its sentence break inside the 72px tile at that size. */}
              <ErrorNotice
                variant="inline"
                className="flex-col gap-1 text-center"
                messageClassName="text-[10px] leading-tight"
                message={t('components.avatarBuilder.lib_art_failed')}
                testId={`avatar-pack-noart-notice-${pack.id}`}
              />
            </div>
          ) : (
            // A Lottie or sprite pack has no single image to point an `<img>` at:
            // the slot route serves the first as `application/json` and the second
            // as the whole sheet, so a bare `<img>` showed a broken-image glyph or
            // a strip of every frame. `PackAvatar` reads the pack and draws its
            // real art — one detail read per non-SVG pack, cached for the crew
            // that then wears it.
            <PackAvatar
              id={pack.id}
              state="idle"
              size={72}
              className="rounded-lg"
              onError={() => markBroken(pack.id)}
            />
          )}
          <span className="text-[12px] font-medium leading-tight">{pack.name}</span>
          {/* Author only on a CUSTOM pack. On the built-in, "Author: Kiro Crew" left the
              reader unable to tell whether that named a person, a team or the app — and
              the pack already carries a "Built-in" badge that says the useful part. */}
          {pack.author && pack.type === 'custom' && (
            <span className="text-[10.5px] leading-tight text-muted">
              {t('components.avatarBuilder.lib_by_author', { author: pack.author })}
            </span>
          )}
          {/* The chosen card says so IN WORDS, not only through its border, and every
              OTHER card carries an empty radio ring in the same place. A hover ring is
              invisible at rest, so nothing at first sight distinguished this grid from a
              display of what is installed — the reader rated clicking it a guess, twice:
              once with no mark at all, and again when only the chosen card was marked,
              because a lone "Selected" has no visible siblings to make it a choice among
              several. The ring is the sibling: one filled, the rest empty, is the shape
              of a radio group, which is what this grid is. Decorative to assistive tech
              (the option's `aria-selected` already says which is chosen). */}
          {selected ? (
            <span
              className="inline-flex items-center gap-1 text-[10.5px] font-medium leading-tight text-accent"
              data-testid={`avatar-pack-selected-${pack.id}`}
            >
              {/* The same ring the unchosen cards carry, filled: one filled among
                  empty is the radio group; a check beside empty rings read as a
                  chip next to unexplained circles. */}
              <span
                aria-hidden="true"
                className="inline-block h-[11px] w-[11px] rounded-full border-2 border-current bg-current"
                data-testid={`avatar-pack-ring-${pack.id}`}
              />
              {t('components.avatarBuilder.lib_selected')}
            </span>
          ) : (
            <span
              aria-hidden="true"
              className="mt-[1px] inline-block h-[11px] w-[11px] rounded-full border-2 border-border-strong"
              data-testid={`avatar-pack-unselected-${pack.id}`}
              data-ring="empty"
            />
          )}
        </button>
        <div className="flex flex-wrap items-center justify-center gap-1">
          <span className="rounded border border-border px-1 text-[10px] text-muted">
            {t(
              pack.type === 'builtin'
                ? 'components.avatarBuilder.lib_badge_builtin'
                : 'components.avatarBuilder.lib_badge_custom',
            )}
          </span>
          <span className="rounded border border-border px-1 text-[10px] text-muted">
            {formatKey ? t(formatKey) : pack.format}
          </span>
        </div>
        {pack.type === 'custom' &&
          (armedDelete === pack.id ? (
            <div className="flex flex-col gap-1">
              <Btn
                danger
                disabled={busy}
                onClick={() => void remove(pack.id, pack.name)}
                className="w-full justify-center whitespace-nowrap"
                data-testid={`avatar-pack-delete-confirm-${pack.id}`}
              >
                {t('components.avatarBuilder.lib_delete_confirm')}
              </Btn>
              {/* Filled, not outlined: a Btn's default 1px border is the theme's
                  `--border`, which vanishes against a card of the same family —
                  so the safe half of a destructive confirm read as plain text.
                  The danger half keeps the outline idiom the rest of the app
                  uses for a destructive action. */}
              {/* What the destructive half actually costs, next to the button that
                  does it. The reader refused to click because nothing said whether a
                  delete could be undone — and the honest answer is that it cannot, but
                  the pack is re-importable, which is the part that makes the decision
                  easy rather than frightening. */}
              <span className="text-center text-[10px] leading-tight text-muted" data-testid={`avatar-pack-delete-note-${pack.id}`}>
                {t('components.avatarBuilder.lib_delete_note')}
              </span>
              <Btn
                ref={keepRef}
                onClick={() => setArmedDelete(null)}
                className="w-full justify-center whitespace-nowrap bg-bg-elevated"
                data-testid={`avatar-pack-delete-cancel-${pack.id}`}
              >
                {t('components.avatarBuilder.lib_delete_keep')}
              </Btn>
            </div>
          ) : (
            <button
              type="button"
              disabled={busy}
              onClick={() => { setRequestError(''); setArmedDelete(pack.id) }}
              className="mx-auto flex items-center gap-1 text-[11px] text-muted underline underline-offset-2 hover:text-danger"
              data-testid={`avatar-pack-delete-${pack.id}`}
            >
              <Trash2 size={11} aria-hidden="true" />
              {t('components.avatarBuilder.lib_delete')}
            </button>
          ))}
      </div>
    )
  }

  return (
    <div className="flex flex-col gap-3" data-testid="avatar-library-pane">
      {/* Stacked below sm: side by side, a phone left the hint a ~200px column
          wrapping to six ragged lines beside the button. */}
      <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <p className="m-0 min-w-0 text-[11.5px] text-muted sm:flex-1">
          {t('components.avatarBuilder.lib_hint')}
        </p>
        <Btn disabled={busy} onClick={() => fileInput.current?.click()} data-testid="avatar-pack-import">
          <Upload className="lucide-inline" aria-hidden="true" />
          {t('components.avatarBuilder.lib_import')}
        </Btn>
        <input
          ref={fileInput}
          type="file"
          accept="application/json,.json"
          className="hidden"
          aria-label={t('components.avatarBuilder.lib_import')}
          onChange={e => {
            void pickBundle(e.target.files?.[0])
            // Allow re-picking the same file after a rejection.
            e.target.value = ''
          }}
          data-testid="avatar-pack-import-input"
        />
      </div>
      {/* Muted text, not an ErrorNotice: see `pickHint`. Nothing failed, so
          dressing it as an error would be the inverse of the rule. */}
      {pickHint && (
        <p className="m-0 text-[11.5px] text-muted" data-testid="avatar-pack-pick-hint">
          {pickHint}
        </p>
      )}
      {/* No hand-off on either notice below: this pane sits inside the avatar
          builder, which holds the crew's unsaved avatar draft, and the editor
          behind it holds every other unsaved crew edit. The chat hand-off
          navigates away and unmounts both, so offering it here would trade an
          error message for the work the message is about. Same reason the
          picture pane states. */}
      {requestError && (
        <ErrorNotice
          variant="inline"
          message={requestError}
          onDismiss={() => setRequestError('')}
          testId="avatar-pack-error"
        />
      )}
      {packs === null ? (
        <p className="m-0 text-[12px] text-muted" data-testid="avatar-library-loading">
          {t('components.avatarBuilder.lib_loading')}
        </p>
      ) : loadError ? (
        <div className="flex flex-col items-start gap-2">
          {/* No hand-off, for the reason given on the two notices above. */}
          <ErrorNotice variant="inline" message={loadError} testId="avatar-library-error" />
          <Btn onClick={() => void load()} data-testid="avatar-library-retry">
            {t('components.avatarBuilder.lib_retry')}
          </Btn>
        </div>
      ) : packs.length === 0 ? (
        <p className="m-0 text-[12px] text-muted" data-testid="avatar-library-empty">
          {t('components.avatarBuilder.lib_empty')}
        </p>
      ) : (
        <div
          className="grid max-h-[380px] grid-cols-[repeat(auto-fill,minmax(120px,1fr))] gap-2 overflow-y-auto pr-1"
          role="listbox"
          aria-label={t('components.avatarBuilder.mode_library')}
          ref={gridRef}
          onScroll={e => readFade(e.currentTarget)}
          style={
            fadeMask
              ? { maskImage: `linear-gradient(to bottom, ${fadeMask})`, WebkitMaskImage: `linear-gradient(to bottom, ${fadeMask})` }
              : undefined
          }
          data-testid="avatar-library-grid"
          data-fade={`${fade.top ? 'top' : ''}${fade.bottom ? 'bottom' : ''}` || 'none'}
        >
          {packs.map(card)}
        </div>
      )}
    </div>
  )
}
