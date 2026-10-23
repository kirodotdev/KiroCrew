import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn } from '../../components/ui'
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader, DialogTitle } from '../../components/ui/dialog'
import ChangeCard from '../../cards/ChangeCard'
import { useChangeCards } from '../../cards/useChangeCards'
import { CREWMATE_IMPORT_SLOT, cardsApi, errorCode, type Card } from '../../api/cards'
import { i18nT } from '../../i18n/t'

/** The file picker's filter: a `.crewmate` file is a zip under its own name. */
export const CREWMATE_FILE_ACCEPT = '.crewmate'

/** Cards that still ask something of the owner: press, wait, retry or undo. */
const OPEN: ReadonlySet<string> = new Set(['pending', 'applying', 'failed', 'partial'])

/** The gateway's refusal codes for a file, in the reader's words. */
const REFUSALS: Record<string, () => string> = {
  newer_format: () => i18nT('pages.membersPage.import_refused_newer'),
  too_large: () => i18nT('pages.membersPage.import_refused_too_large'),
  too_many_entries: () => i18nT('pages.membersPage.import_refused_too_large'),
  grant_in_bundle: () => i18nT('pages.membersPage.import_refused_grant'),
  needs_mate_config: () => i18nT('pages.membersPage.import_refused_mcp'),
  user_template: () => i18nT('pages.membersPage.import_refused_user_template'),
  invalid_text: () => i18nT('pages.membersPage.import_refused_text'),
}

/** What the dialog says about a refused file: the code's own line, else the generic one. */
export function importRefusal(e: unknown): string {
  const code = errorCode(e)
  return (code && REFUSALS[code]?.()) || i18nT('pages.membersPage.import_refused')
}

/**
 * Import from file (rfc-crewmate-share.md §5.4): pick one `.crewmate` file, the
 * gateway vets it and raises ONE `crewmate.import` card, and the card opens here
 * in the shared card component. Nothing is created until the owner presses the
 * card. A card left pending lives under the reserved `owner:crewmate-import`
 * slot, so opening this again shows it instead of asking for a file.
 */
export default function CrewmateImportDialog({ open, onOpenChange }: {
  open: boolean
  onOpenChange: (open: boolean) => void
}) {
  const { t } = useTranslation()
  const { cards, error: readError, isFetched, store, refresh, remove } = useChangeCards(open ? CREWMATE_IMPORT_SLOT : null)
  const [busy, setBusy] = useState(false)
  const [problem, setProblem] = useState<string | null>(null)
  const [justRaised, setJustRaised] = useState<Card | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  // Every card the slot returns (live ones and those finished in the last day,
  // so a failed or partial import keeps its Retry or Undo), plus the one this
  // dialog just raised until the cache has it.
  const shown = [...cards]
  if (justRaised && !shown.some(c => c.id === justRaised.id)) shown.push(justRaised)
  const waiting = shown.some(c => OPEN.has(c.status))
  const pending = shown.some(c => c.status === 'pending')
  // A dismissed card leaves the cache; the just-raised copy must leave with it.
  const dismiss = (id: string) => {
    if (justRaised?.id === id) setJustRaised(null)
    remove(id)
  }
  const asked = useRef(false)

  // Opened with nothing waiting: go straight to the file picker, once. Never
  // while the cards could not be read: one may be waiting unseen.
  useEffect(() => {
    if (!open) { asked.current = false; setProblem(null); setJustRaised(null); return }
    if (isFetched && !readError && !asked.current && !waiting) {
      asked.current = true
      inputRef.current?.click()
    }
  }, [open, isFetched, readError, waiting])

  const pick = async (file: File | undefined) => {
    if (!file) return
    setBusy(true)
    setProblem(null)
    try {
      const card = await cardsApi.importCrewmate(file)
      if (card) {
        store(card)
        setJustRaised(card)
      }
    } catch (e) {
      setProblem(importRefusal(e))
    } finally {
      setBusy(false)
      if (inputRef.current) inputRef.current.value = ''
    }
  }

  return (
    <>
      <input
        ref={inputRef}
        type="file"
        accept={CREWMATE_FILE_ACCEPT}
        className="hidden"
        aria-hidden="true"
        tabIndex={-1}
        onChange={(e) => void pick(e.target.files?.[0])}
        data-testid="crewmate-import-input"
      />
      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent maxWidth={560} data-testid="crewmate-import-dialog">
          <DialogHeader>
            <DialogTitle>{t('pages.membersPage.import_title')}</DialogTitle>
          </DialogHeader>
          <DialogBody>
            <div className="flex flex-col gap-3">
            {pending && (
              <p className="m-0 text-[13px] text-muted" data-testid="crewmate-import-keep-hint">{t('pages.membersPage.import_keep_hint')}</p>
            )}
            {shown.map((c) => (
              <ChangeCard key={c.id} card={c} store={store} refresh={refresh} remove={dismiss} />
            ))}
            {shown.length === 0 && !busy && !problem && !readError && (
              <p className="m-0 text-[13px] text-muted">{t('pages.membersPage.import_hint')}</p>
            )}
            {readError && (
              <ErrorNotice
                message={readError instanceof Error ? readError.message : String(readError)}
                testId="crewmate-import-read-error"
                askAgent
              />
            )}
            {busy && <p className="m-0 text-[13px] text-muted" role="status">{t('pages.membersPage.import_checking')}</p>}
            <ErrorNotice message={problem} testId="crewmate-import-refused" askAgent />
            </div>
          </DialogBody>
          <DialogFooter>
            <Btn onClick={() => inputRef.current?.click()} disabled={busy} data-testid="crewmate-import-pick">
              {t('pages.membersPage.import_pick')}
            </Btn>
            <Btn onClick={() => onOpenChange(false)} data-testid="crewmate-import-close">
              {pending ? t('pages.membersPage.import_later') : t('pages.membersPage.close')}
            </Btn>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
