/**
 * The body of a `crewmate.import` card: every file the `.crewmate` file brings,
 * in full (rfc-crewmate-share.md §5.4). The card itself lists each file by name
 * and digest; the text comes from the local backend's owner-only staged copy
 * (`GET /api/members/import/{sha256}/files`), re-checked there on every read.
 * Text is shown as text, never run or rendered as markup.
 */
import { useQuery } from '@tanstack/react-query'
import ErrorNotice from '../components/ErrorNotice'
import { i18nT } from '../i18n/t'
import { cardsApi, type CrewmateImportFiles } from '../api/cards'
import { CardRow, type KindBodyProps } from './kindBodies'

// A failed card can be retried, so its staged copy is still kept.
const LIVE: ReadonlySet<string> = new Set(['pending', 'applying', 'failed'])

function FileText({ path, text }: { path: string; text: string }) {
  return (
    // Folded: the whole file is one click away, and the decision reads first.
    <details className="mb-2 last:mb-0" data-testid="import-file">
      <summary className="cursor-pointer text-[12px] font-medium text-muted [overflow-wrap:anywhere]">
        {path} · {i18nT('components.changeCards.carried_show_code')}
      </summary>
      <pre className="m-0 mt-1 max-h-60 overflow-auto rounded bg-bg-hover p-2 text-[12px] leading-4 whitespace-pre-wrap [overflow-wrap:anywhere]">{text}</pre>
    </details>
  )
}

export function ImportFilesBody({ card }: KindBodyProps) {
  const sha = typeof card.params.bundle_sha256 === 'string' ? card.params.bundle_sha256 : ''
  const carried = card.params.carried
  const wanted = !!sha && !!carried && LIVE.has(card.status)
  const { data, error } = useQuery<CrewmateImportFiles>({
    queryKey: ['crewmate-import-files', sha],
    queryFn: () => cardsApi.importFiles(sha),
    enabled: wanted,
    retry: false,
    staleTime: Infinity,
  })
  if (!wanted) return null
  if (error) {
    return (
      <CardRow label={i18nT('components.changeCards.field_carried_files')}>
        <ErrorNotice message={i18nT('components.changeCards.carried_unavailable')} testId="import-files-unavailable" askAgent />
      </CardRow>
    )
  }
  if (!data) {
    return <CardRow label={i18nT('components.changeCards.field_carried_files')}><span role="status">{i18nT('components.changeCards.carried_loading')}</span></CardRow>
  }
  return (
    <>
      {Object.entries(data.skills).map(([name, files]) => (
        <CardRow key={`s-${name}`} label={i18nT('components.changeCards.carried_skill', { name })}>
          {Object.entries(files).sort(([a], [b]) => a.localeCompare(b, 'en')).map(([path, text]) => (
            <FileText key={path} path={path} text={text} />
          ))}
        </CardRow>
      ))}
      {Object.entries(data.scripts).map(([file, text]) => (
        <CardRow key={`c-${file}`} label={i18nT('components.changeCards.carried_script')}>
          <FileText path={file} text={text} />
        </CardRow>
      ))}
      {data.avatar && (
        <CardRow label={i18nT('components.changeCards.field_carried_avatar')}>
          <img src={data.avatar} alt="" className="h-16 w-16 rounded-full object-cover" data-testid="import-avatar" />
        </CardRow>
      )}
    </>
  )
}
