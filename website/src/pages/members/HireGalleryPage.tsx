/**
 * Crew › Hire Crewmates: the template gallery (design step 6), at `/members/hire`.
 *
 * The ONE place a crewmate is hired from, whatever the template's origin: the
 * job cards enabled installed apps offer (`crew.templates`), the agent files
 * this package ships (built-ins) and the user's own agent files. The catalog
 * is `GET /api/members/templates`.
 *
 * A crewmate is named before it exists. Hire is the PRIMARY action on every
 * card and in the detail layer, and it always opens the naming step: the user
 * types a name of its own (the role is the placeholder, never the value),
 * confirms, and ONLY THEN does `POST /api/members {source, display_name}` mint
 * the copy, publish the row, enroll it and land in its DM thread. Cancel at
 * the naming step writes nothing. A blank name cannot be confirmed.
 *
 * Beside Hire, one quieter action follows how many crewmates the card already
 * has (`hired_as`: the enrolled, active roster hired from it, by the server's
 * own count): none -- nothing; one -- "Chat with <name>", straight to that
 * crewmate's DM by its immutable id; two or more -- "Your crewmates (N)", a
 * picker listing each by name with its id beside (two may carry one label),
 * where navigation happens only on an explicit choice. Opening a chat never
 * hires; a target that has gone since the catalog was read is said, not
 * silently swapped for another crewmate.
 *
 * The rail stays on Crew: this is a view under the page, not a new surface.
 * Scenario chips file cards by the card's `category`; a card opens a detail
 * layer (full description, tags, Try asking, capabilities, a quiet publisher
 * line).
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, ArrowRight, ChevronDown, ChevronRight, FileText, Link2, MessageCircle, RefreshCw, Sparkles, UserPlus, Users } from 'lucide-react'
import { api, type HireTemplateCard, type HiredCrewmate } from '../../api/client'
import { MEMBERS_ROSTER_QUERY_KEY } from '../../api/membersQuery'
import { PageHeader, SearchInput, IconButton, Btn, Input } from '../../components/ui'
import { Dialog, DialogBody, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '../../components/ui/dialog'
import CrewAvatar from '../../components/CrewAvatar'
import ErrorNotice from '../../components/ErrorNotice'
import { MEMBER_LABEL_MAX_LEN } from './MemberThreadExtras'

export const HIRE_TEMPLATES_QUERY_KEY = ['members-hire-templates'] as const
/** The installed-agent list (`/api/agents/installed`), shared with the agents
 *  page and the drawer's Capabilities section: a hire adds a row to it. */
export const AGENTS_INSTALLED_QUERY_KEY = ['agents-installed'] as const

/** The scenario chips, in the order the design lists them; `other` last so a
 *  card without a category is never lost. Keyed to the server's
 *  `CREW_CATEGORIES` vocabulary. */
export const HIRE_CATEGORIES = ['engineering', 'ops', 'research', 'release', 'product', 'writing', 'other'] as const
export type HireCategory = (typeof HIRE_CATEGORIES)[number]

/** The chip label key per category, spelled out (never `category_${c}`): the
 *  catalog's dead-key scan matches literal keys, so a template would read every
 *  label as unused. */
export const CATEGORY_LABEL_KEYS: Record<HireCategory | 'all', string> = {
  all: 'pages.hireGallery.category_all',
  engineering: 'pages.hireGallery.category_engineering',
  ops: 'pages.hireGallery.category_ops',
  research: 'pages.hireGallery.category_research',
  release: 'pages.hireGallery.category_release',
  product: 'pages.hireGallery.category_product',
  writing: 'pages.hireGallery.category_writing',
  other: 'pages.hireGallery.category_other',
}

/** Cards matching the chip and the search, in catalog order. Pure, so the
 *  filter is testable without the page. */
export function filterCards(cards: readonly HireTemplateCard[], category: HireCategory | 'all', query: string): HireTemplateCard[] {
  const q = query.trim().toLowerCase()
  return cards.filter((c) => {
    if (category !== 'all' && (c.category || 'other') !== category) return false
    if (!q) return true
    return `${c.role} ${c.duty} ${c.tags.join(' ')} ${c.publisher}`.toLowerCase().includes(q)
  })
}

/** Which chips have at least one card: an empty scenario is not offered. */
export function categoriesPresent(cards: readonly HireTemplateCard[]): HireCategory[] {
  const present = new Set(cards.map((c) => (c.category || 'other') as HireCategory))
  return HIRE_CATEGORIES.filter((c) => present.has(c))
}

/** The name a hire sends: what the user typed, whitespace collapsed; empty
 *  when nothing was typed. Pure, so the confirm gate is testable. The server
 *  applies the same rule and refuses a blank (`missing_display_name`). */
export function hireName(raw: string): string {
  return raw.trim().replace(/\s+/g, ' ')
}

function memberPath(id: string) {
  return `/members?member=${encodeURIComponent(id)}`
}

/** The card's avatar seed: the card id, so two cards never share a default
 *  face by accident; a card with a ghost wears it. */
function cardAvatar(c: HireTemplateCard) {
  return c.avatar ?? undefined
}

/** Why a card cannot be hired, in task language. The server's refusal names
 *  the mechanism ("has not installed its agent 'scribe' yet; re-enable the
 *  app"); the reader needs the task and the destination -- the "Browse apps"
 *  link on this page -- so the two codes a user can act on are said that way
 *  (the surface here is the App Store; "Apps" is not a name it wears); anything else is
 *  the server's own sentence. */
function unavailableReason(card: HireTemplateCard, t: (k: string, o?: Record<string, unknown>) => string): string {
  const app = card.publisher || (card.source.kind === 'store' ? card.source.app : '')
  if (card.unavailable_code === 'template_not_materialized') return t('pages.hireGallery.unavailable_not_materialized', { app })
  if (card.unavailable_code === 'app_disabled') return t('pages.hireGallery.unavailable_app_disabled', { app })
  return card.unavailable_reason || t('pages.hireGallery.unavailable')
}

type CardActionsProps = {
  card: HireTemplateCard
  pending: boolean
  onHire: () => void
  onChat: (who: HiredCrewmate) => void
  onPick: () => void
  size?: 'sm' | 'lg'
}

/** Hire, primary, always; beside it the one secondary action the card's
 *  crewmate count calls for (none / Chat with <name> / Your crewmates (N)). */
function CardActions({ card, pending, onHire, onChat, onPick, size = 'sm' }: CardActionsProps) {
  const { t } = useTranslation()
  const cls = size === 'lg' ? 'px-4 py-1.5 text-[13.5px]' : ''
  const disabled = pending || !card.hireable
  const title = !card.hireable ? unavailableReason(card, t) : undefined
  const count = card.hired_as.length
  return (
    <span className="inline-flex flex-wrap items-center justify-end gap-1.5 max-w-full">
      {count === 1 && (
        <Btn
          className={`${cls} max-w-[220px]`}
          onClick={(e) => { e.stopPropagation(); onChat(card.hired_as[0]) }}
          title={t('pages.hireGallery.chat_with', { name: card.hired_as[0].display_name })}
          data-testid="hire-chat-with"
        >
          <MessageCircle size={13} className="lucide-inline shrink-0" />
          <span className="truncate">{t('pages.hireGallery.chat_with', { name: card.hired_as[0].display_name })}</span>
        </Btn>
      )}
      {count >= 2 && (
        <Btn
          className={cls}
          onClick={(e) => { e.stopPropagation(); onPick() }}
          aria-haspopup="dialog"
          data-testid="hire-your-crewmates"
        >
          <Users size={13} className="lucide-inline shrink-0" />
          {t('pages.hireGallery.your_crewmates', { count })}
        </Btn>
      )}
      <Btn
        primary
        className={cls}
        disabled={disabled}
        aria-disabled={disabled}
        title={title}
        onClick={(e) => { e.stopPropagation(); if (!disabled) onHire() }}
        data-testid="hire-button"
      >
        <UserPlus size={13} className="lucide-inline" />
        {pending ? t('pages.hireGallery.hiring') : t('pages.hireGallery.hire')}
      </Btn>
    </span>
  )
}

function TemplateCard({ card, pending, onOpen, onHire, onChat, onPick }: { card: HireTemplateCard; pending: boolean; onOpen: () => void; onHire: () => void; onChat: (who: HiredCrewmate) => void; onPick: () => void }) {
  const { t } = useTranslation()
  return (
    <div
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen() } }}
      // ``flex-wrap`` with the text given a minimum basis: on a narrow viewport
      // (a 320px phone, a split pane) the action cluster drops to its own row
      // under the text instead of crushing the role and duty to nothing or
      // overflowing the card.
      className="flex flex-wrap items-start gap-3.5 p-3.5 border border-border rounded-xl bg-card hover:border-border-strong transition-colors cursor-pointer focus-ring"
      data-testid="hire-template-card"
      data-card-id={card.id}
      aria-label={t('pages.hireGallery.card_aria', { role: card.role })}
    >
      <div className="relative shrink-0">
        <CrewAvatar seed={card.id} avatar={cardAvatar(card)} size={64} className="rounded-2xl" />
      </div>
      <div className="flex-1 min-w-0 basis-40 flex flex-col gap-1">
        <div className="text-[14.5px] font-semibold text-text-strong truncate">{card.role}</div>
        {card.duty && <div className="text-[12.5px] text-muted truncate" title={card.duty}>{card.duty}</div>}
        {!card.hireable && (
          // The reason on the face, not only in the disabled button's tooltip:
          // a touch reader never sees a title.
          <div className="text-[11.5px] text-warn leading-snug" data-testid="hire-card-unavailable">{unavailableReason(card, t)}</div>
        )}
        {card.tags.length > 0 && (
          <div className="flex items-center gap-1 min-w-0 mt-0.5">
            {card.tags.slice(0, 3).map((tag) => (
              <span key={tag} className="text-[10.5px] text-muted bg-bg-elevated border border-border rounded-md px-1.5 h-[18px] inline-flex items-center whitespace-nowrap truncate">{tag}</span>
            ))}
          </div>
        )}
      </div>
      <div className="shrink-0 self-center ml-auto max-w-full" onKeyDown={(e) => e.stopPropagation()} role="presentation" data-testid="hire-card-actions">
        <CardActions card={card} pending={pending} onHire={onHire} onChat={onChat} onPick={onPick} />
      </div>
    </div>
  )
}

/** Plain words for the kind tag beside a capability: "MCP" / "SKILL" meant
 *  nothing to a reader at the moment of the hire decision. A static map, so
 *  the catalog scan sees every key. */
const CAPABILITY_KIND_KEY: Record<'skill' | 'mcp', string> = {
  skill: 'pages.hireGallery.capability_kind_skill',
  mcp: 'pages.hireGallery.capability_kind_mcp',
}

/** The two kinds the backend emits (`member_gallery.capabilities_of`: skills
 *  and MCP servers). A kind nothing produces has no icon here. */
function CapIcon({ kind }: { kind: 'skill' | 'mcp' }) {
  const cls = 'inline-flex items-center justify-center w-6 h-6 rounded-md border border-border bg-bg-elevated shrink-0'
  if (kind === 'mcp') return <span className={cls}><Link2 size={12} className="text-accent" /></span>
  return <span className={cls}><Sparkles size={12} className="text-accent" /></span>
}

function TemplateDetail({ card, pending, onClose, onHire, onChat, onPick }: { card: HireTemplateCard | null; pending: boolean; onClose: () => void; onHire: (c: HireTemplateCard) => void; onChat: (who: HiredCrewmate) => void; onPick: (c: HireTemplateCard) => void }) {
  const { t } = useTranslation()
  const [capsOpen, setCapsOpen] = useState(true)
  const originLabel: Record<HireTemplateCard['origin'], string> = {
    app: t('pages.hireGallery.origin_app'),
    builtin: t('pages.hireGallery.origin_builtin'),
    local: t('pages.hireGallery.origin_local'),
  }
  return (
    <Dialog open={!!card} onOpenChange={(o) => { if (!o) onClose() }}>
      {card && (
        <DialogContent maxWidth={640} className="p-0" data-testid="hire-template-detail">
          <div className="overflow-y-auto min-h-0 px-7 pt-7 pb-4">
            <CrewAvatar seed={card.id} avatar={cardAvatar(card)} size={72} className="rounded-2xl" />
            <DialogTitle className="mt-4 text-[20px] font-semibold text-text-strong leading-tight">{card.role}</DialogTitle>
            {(card.description || card.duty) && (
              <p className="mt-1.5 text-[13px] text-muted leading-relaxed">{card.description || card.duty}</p>
            )}
            {card.tags.length > 0 && (
              <div className="mt-4 flex items-center gap-1.5 flex-wrap">
                {card.tags.map((tag) => (
                  <span key={tag} className="text-[12px] text-text bg-bg-elevated border border-border rounded-full px-2.5 h-[24px] inline-flex items-center">{tag}</span>
                ))}
              </div>
            )}
            {!card.hireable && (
              <div className="mt-4">
                <ErrorNotice message={unavailableReason(card, t)} variant="inline" askAgent testId="hire-detail-unavailable" />
              </div>
            )}

            {card.starter_prompts.length > 0 && (
              <>
                <div className="mt-6 text-[13px] font-semibold text-text-strong">{t('pages.hireGallery.try_asking')}</div>
                <ul className="list-none m-0 p-0 mt-2 space-y-1.5" data-testid="hire-detail-starters">
                  {/* Previews of what the colleague is for -- not controls, and
                      not dressed as the thread's Ask rows either (the same
                      bordered shape must not act in one place and not in the
                      other): quoted lines. The hire is the one primary button
                      below, and these same prompts become the Ask cards in the
                      new member's thread. */}
                  {card.starter_prompts.map((s) => (
                    <li key={s.text} className="flex items-start gap-2.5 pl-3 border-l-2 border-border" data-testid="hire-detail-starter">
                      <span className="flex-1 min-w-0 flex flex-col gap-1">
                        <span className="text-[13px] text-muted italic leading-snug">“{s.text}”</span>
                        {s.attachment && (
                          <span className="inline-flex items-center gap-1 self-start text-[11px] text-muted border border-border rounded-md px-1.5 h-[20px] bg-card">
                            <FileText size={11} /> {s.attachment}
                          </span>
                        )}
                      </span>
                    </li>
                  ))}
                </ul>
              </>
            )}

            {card.capabilities.length > 0 && (
              <>
                <button
                  type="button"
                  onClick={() => setCapsOpen((v) => !v)}
                  aria-expanded={capsOpen}
                  className="mt-6 w-full flex items-center gap-1.5 text-[13px] font-semibold text-text-strong bg-transparent border-none px-0 cursor-pointer"
                  data-testid="hire-detail-caps-toggle"
                >
                  {capsOpen ? <ChevronDown size={14} className="text-muted" /> : <ChevronRight size={14} className="text-muted" />}
                  {t('pages.hireGallery.capabilities')} <span className="text-muted font-normal">{card.capabilities.length}</span>
                </button>
                {capsOpen && (
                  <ul className="list-none m-0 p-0 mt-2 grid grid-cols-2 gap-x-4 gap-y-1.5" data-testid="hire-detail-caps">
                    {card.capabilities.map((c) => (
                      <li key={`${c.kind}:${c.name}`} className="flex items-center gap-2 text-[12.5px] min-w-0">
                        <CapIcon kind={c.kind} />
                        <span className="truncate">{c.name}</span>
                        <span className="text-[10.5px] text-muted ml-auto shrink-0">{t(CAPABILITY_KIND_KEY[c.kind])}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </>
            )}

            <div className="mt-6 text-[11.5px] text-muted flex items-center gap-1.5 flex-wrap" data-testid="hire-detail-meta">
              {card.publisher && <><span>{card.publisher}</span><span aria-hidden>·</span></>}
              {card.version && <><span>v{card.version}</span><span aria-hidden>·</span></>}
              <span>{originLabel[card.origin]}</span>
              {card.hired_as.length > 0 && (
                <><span aria-hidden>·</span><span>{card.hired_as.length === 1
                  ? t('pages.hireGallery.hired_as_named', { name: card.hired_as[0].display_name })
                  : t('pages.hireGallery.hired_as', { count: card.hired_as.length })}</span></>
              )}
            </div>
          </div>
          <div className="shrink-0 flex items-center justify-end gap-3 px-7 py-4 border-t border-border bg-card">
            <CardActions card={card} pending={pending} size="lg" onHire={() => onHire(card)} onChat={onChat} onPick={() => onPick(card)} />
          </div>
        </DialogContent>
      )}
    </Dialog>
  )
}

/** The naming step. Nothing is written until Hire here: Cancel (or Escape)
 *  closes it with no request made; a blank name cannot be confirmed; a refused
 *  hire is said in this dialog, where the user is looking, with the typed
 *  name still in the field to retry or change. */
function HireNameDialog({ card, pending, error, onCancel, onConfirm }: { card: HireTemplateCard | null; pending: boolean; error: string; onCancel: () => void; onConfirm: (card: HireTemplateCard, name: string) => void }) {
  const { t } = useTranslation()
  const [draft, setDraft] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  const cardId = card?.id
  useEffect(() => {
    // A fresh field per card: a name typed for one role must not be handed to
    // the next card's dialog.
    setDraft('')
  }, [cardId])
  useEffect(() => {
    if (card) inputRef.current?.focus()
  }, [card])
  const name = hireName(draft)
  const canConfirm = !!card && !!name && !pending
  const confirm = () => { if (card && canConfirm) onConfirm(card, name) }
  return (
    <Dialog open={!!card} onOpenChange={(o) => { if (!o && !pending) onCancel() }}>
      {card && (
        <DialogContent maxWidth={440} data-testid="hire-name-dialog">
          {/* The dialog's standard composition: DialogContent carries no padding
              of its own -- the header, body and footer slots supply the inset
              (and the header keeps clear of the close button). A form placed
              straight into the content would sit flush against the edges. The
              form wraps all three so Enter in the field submits. */}
          <form
            className="flex min-h-0 flex-1 flex-col"
            onSubmit={(e) => { e.preventDefault(); confirm() }}
          >
            <DialogHeader data-testid="hire-name-header">
              <CrewAvatar seed={card.id} avatar={cardAvatar(card)} size={32} className="rounded-lg shrink-0" />
              <DialogTitle>{t('pages.hireGallery.name_title')}</DialogTitle>
            </DialogHeader>
            <DialogBody className="flex flex-col gap-4" data-testid="hire-name-body">
              <DialogDescription id="hire-name-description">
                {card.hired_as.length > 0
                  ? t('pages.hireGallery.name_description_another', { role: card.role, count: card.hired_as.length })
                  : t('pages.hireGallery.name_description', { role: card.role })}
              </DialogDescription>
              <label className="flex flex-col gap-1.5 text-[12.5px] text-text">
                <span>{t('pages.hireGallery.name_label')}</span>
                <Input
                  ref={inputRef}
                  value={draft}
                  maxLength={MEMBER_LABEL_MAX_LEN}
                  disabled={pending}
                  placeholder={card.role}
                  autoComplete="off"
                  aria-describedby="hire-name-description hire-name-hint"
                  aria-invalid={!name && draft.length > 0 ? true : undefined}
                  onChange={(e) => setDraft(e.target.value)}
                  data-testid="hire-name-input"
                />
                <span id="hire-name-hint" className="text-[11.5px] text-muted">
                  {name ? t('pages.hireGallery.name_hint_rename') : t('pages.hireGallery.name_hint_required')}
                </span>
              </label>
              {error && <ErrorNotice message={error} variant="inline" askAgent={false} testId="hire-name-error" />}
            </DialogBody>
            <DialogFooter data-testid="hire-name-footer">
              <Btn type="button" onClick={onCancel} disabled={pending} data-testid="hire-name-cancel">
                {t('components.confirmDialog.cancel')}
              </Btn>
              <Btn type="submit" primary disabled={!canConfirm} aria-disabled={!canConfirm} data-testid="hire-name-confirm">
                <UserPlus size={13} className="lucide-inline" />
                {pending ? t('pages.hireGallery.hiring') : t('pages.hireGallery.hire')}
              </Btn>
            </DialogFooter>
          </form>
        </DialogContent>
      )}
    </Dialog>
  )
}

/** The picker for a card with two or more crewmates: each by name with its id
 *  beside (two crewmates may carry one label; the id tells them apart), in
 *  roster order. Nothing navigates until a row is chosen; Escape or the close
 *  button leaves the gallery where it was. */
function CrewmatePicker({ card, onClose, onSelect }: { card: HireTemplateCard | null; onClose: () => void; onSelect: (who: HiredCrewmate) => void }) {
  const { t } = useTranslation()
  return (
    <Dialog open={!!card} onOpenChange={(o) => { if (!o) onClose() }}>
      {card && (
        <DialogContent maxWidth={440} data-testid="hire-crewmate-picker">
          {/* Standard composition, as the naming dialog: the header carries the
              inset and the close-button clearance, the body the scroll (a long
              roster stays inside the 90vh cap). */}
          <DialogHeader data-testid="hire-picker-header">
            <DialogTitle>{t('pages.hireGallery.picker_title', { role: card.role })}</DialogTitle>
          </DialogHeader>
          <DialogBody className="flex flex-col gap-4" data-testid="hire-picker-body">
            <DialogDescription id="hire-picker-description">
              {t('pages.hireGallery.picker_description', { count: card.hired_as.length })}
            </DialogDescription>
          <ul className="list-none m-0 p-0 flex flex-col gap-1.5" aria-describedby="hire-picker-description" data-testid="hire-crewmate-list">
            {card.hired_as.map((who) => (
              <li key={who.id}>
                <button
                  type="button"
                  onClick={() => onSelect(who)}
                  className="w-full flex items-center gap-3 px-3 py-2 rounded-lg border border-border bg-bg-elevated hover:border-border-strong hover:bg-bg-hover cursor-pointer text-left focus-ring"
                  data-testid="hire-pick-crewmate"
                  data-member-id={who.id}
                >
                  <MessageCircle size={14} className="text-accent shrink-0" />
                  <span className="min-w-0 flex-1 flex flex-col">
                    <span className="text-[13px] font-medium text-text-strong truncate">{who.display_name}</span>
                    <span className="text-[11px] text-muted font-mono truncate">{who.id}</span>
                  </span>
                  <ArrowRight size={13} className="text-muted shrink-0" />
                </button>
              </li>
            ))}
          </ul>
          </DialogBody>
        </DialogContent>
      )}
    </Dialog>
  )
}

export default function HireGalleryPage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const qc = useQueryClient()
  const [query, setQuery] = useState('')
  const [category, setCategory] = useState<HireCategory | 'all'>('all')
  const [detailId, setDetailId] = useState<string | null>(null)
  const [namingId, setNamingId] = useState<string | null>(null)
  const [pickingId, setPickingId] = useState<string | null>(null)
  const [hireError, setHireError] = useState<string>('')
  const [chatError, setChatError] = useState<string>('')

  const templates = useQuery({
    queryKey: HIRE_TEMPLATES_QUERY_KEY,
    queryFn: () => api.memberTemplates().then((r) => r.templates),
    staleTime: 30_000,
  })

  const cards = useMemo(() => templates.data ?? [], [templates.data])
  const shown = useMemo(() => filterCards(cards, category, query), [cards, category, query])
  const chips = useMemo(() => categoriesPresent(cards), [cards])
  const byId = useCallback((id: string | null) => (id ? cards.find((c) => c.id === id) ?? null : null), [cards])
  const detail = byId(detailId)
  const naming = byId(namingId)
  const picking = byId(pickingId)

  const hire = useMutation({
    mutationFn: ({ card, name }: { card: HireTemplateCard; name: string }) => api.hireMember({ source: card.source, display_name: name }),
    onMutate: () => setHireError(''),
    onSuccess: (r) => {
      // The new crewmate's thread is the destination; the roster, the catalog
      // (hired_as) and the installed-agent list (the drawer's Capabilities
      // reads the new copy from it) are refetched so every surface the thread
      // opens already knows the hire.
      if (r.ok && r.id) {
        void qc.invalidateQueries({ queryKey: MEMBERS_ROSTER_QUERY_KEY })
        void qc.invalidateQueries({ queryKey: HIRE_TEMPLATES_QUERY_KEY })
        void qc.invalidateQueries({ queryKey: AGENTS_INSTALLED_QUERY_KEY })
        setNamingId(null)
        navigate(memberPath(r.id))
      } else {
        setHireError(r.error || t('pages.hireGallery.hire_failed'))
      }
    },
    onError: (e: Error) => setHireError(e.message || t('pages.hireGallery.hire_failed')),
  })
  const pendingId = hire.isPending ? hire.variables?.card.id ?? null : null

  // Opening a chat reads the roster FRESH before navigating: the catalog may
  // be up to 30 s old, and a crewmate fired since must be said, not opened as
  // an empty thread or swapped for another one from the same card.
  const openChat = useMutation({
    mutationFn: async (who: HiredCrewmate) => {
      const roster = (await api.members()).members
      return { who, present: roster.some((r) => r.name === who.id) }
    },
    onMutate: () => setChatError(''),
    // Every outcome closes the layer the action came from -- the detail or the
    // picker -- BEFORE anything else: the notice lives on the page under them,
    // and a dialog left open would cover the one sentence that explains why
    // nothing opened.
    onSuccess: ({ who, present }) => {
      setPickingId(null)
      setDetailId(null)
      if (present) {
        navigate(memberPath(who.id))
      } else {
        setChatError(t('pages.hireGallery.chat_target_gone', { name: who.display_name }))
        void qc.invalidateQueries({ queryKey: HIRE_TEMPLATES_QUERY_KEY })
      }
    },
    onError: () => {
      setPickingId(null)
      setDetailId(null)
      setChatError(t('pages.hireGallery.roster_failed'))
    },
  })

  const beginHire = useCallback((card: HireTemplateCard) => {
    setHireError('')
    // One layer at a time: the detail dialog closes as the naming step opens,
    // so a single Hire button is on screen -- two enabled-looking ones (the
    // detail's under the naming dialog) read as "which one is real".
    setDetailId(null)
    setNamingId(card.id)
  }, [])

  return (
    <div className="flex-1 min-h-0 min-w-0 flex flex-col overflow-hidden" data-testid="hire-gallery">
      <div className="pt-3">
        <PageHeader
          title={
            <span className="inline-flex items-center gap-2">
              <button type="button" onClick={() => navigate('/members')} className="inline-flex items-center justify-center w-7 h-7 rounded-md text-muted hover:text-text hover:bg-bg-hover cursor-pointer bg-transparent border-none" aria-label={t('pages.hireGallery.back')} data-testid="hire-back">
                <ArrowLeft size={16} />
              </button>
              {t('pages.hireGallery.title')}
            </span>
          }
          subtitle={t('pages.hireGallery.subtitle')}
          actions={<>
            <SearchInput placeholder={t('pages.hireGallery.search')} value={query} onChange={(e) => setQuery(e.target.value)} className="w-[220px]" aria-label={t('pages.hireGallery.search')} />
            <IconButton aria-label={t('pages.hireGallery.rescan')} title={t('pages.hireGallery.rescan')} onClick={() => void templates.refetch()} data-testid="hire-rescan">
              <RefreshCw size={15} className={templates.isFetching ? 'animate-spin' : ''} />
            </IconButton>
            <Link to="/apps" className="text-[13px] text-accent hover:underline inline-flex items-center gap-1 whitespace-nowrap" data-testid="hire-browse-apps">
              {t('pages.hireGallery.browse_apps')} <ArrowRight size={13} />
            </Link>
          </>}
        />
      </div>
      <div className="px-6 pb-8 overflow-y-auto flex-1 min-h-0">
        {chatError && (
          <div className="mb-3">
            <ErrorNotice message={chatError} variant="inline" askAgent testId="hire-chat-error" />
          </div>
        )}
        {templates.isError ? (
          <ErrorNotice message={t('pages.hireGallery.catalog_failed')} variant="inline" askAgent testId="hire-catalog-error" />
        ) : (
          <>
            <div className="flex items-center gap-1.5 flex-wrap mb-4" role="radiogroup" aria-label={t('pages.hireGallery.scenario')} data-testid="hire-category-chips">
              {(['all', ...chips] as const).map((c) => {
                const active = c === category
                return (
                  <button
                    key={c}
                    type="button"
                    role="radio"
                    aria-checked={active}
                    onClick={() => setCategory(c)}
                    className={`px-3 h-[28px] rounded-full text-[12.5px] border transition-colors cursor-pointer ${active ? 'bg-accent-subtle border-accent text-accent font-medium' : 'border-border text-muted hover:text-text hover:border-border-strong bg-transparent'}`}
                  >
                    {t(CATEGORY_LABEL_KEYS[c])}
                  </button>
                )
              })}
            </div>
            {templates.isSuccess && cards.length === 0 && (
              <div className="text-[13px] text-muted" data-testid="hire-empty">{t('pages.hireGallery.empty')}</div>
            )}
            {templates.isSuccess && cards.length > 0 && shown.length === 0 && (
              <div className="text-[13px] text-muted" data-testid="hire-no-match">{t('pages.hireGallery.no_match')}</div>
            )}
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-3" data-testid="hire-template-grid">
              {shown.map((card) => (
                <TemplateCard
                  key={card.id}
                  card={card}
                  pending={pendingId === card.id}
                  onOpen={() => setDetailId(card.id)}
                  onHire={() => beginHire(card)}
                  onChat={(who) => openChat.mutate(who)}
                  onPick={() => setPickingId(card.id)}
                />
              ))}
            </div>
          </>
        )}
      </div>
      <TemplateDetail card={detail} pending={pendingId !== null && pendingId === detail?.id} onClose={() => setDetailId(null)} onHire={beginHire} onChat={(who) => openChat.mutate(who)} onPick={(c) => setPickingId(c.id)} />
      <HireNameDialog card={naming} pending={pendingId !== null && pendingId === naming?.id} error={naming ? hireError : ''} onCancel={() => { setNamingId(null); setHireError('') }} onConfirm={(card, name) => hire.mutate({ card, name })} />
      <CrewmatePicker card={picking} onClose={() => setPickingId(null)} onSelect={(who) => openChat.mutate(who)} />
    </div>
  )
}
