/**
 * The app's tags as a card on its detail page. Renders nothing when it has none.
 */
import { Card, CardTitle } from '../components/ui'
import { i18nT } from '../i18n/t'

export default function AppDetailTags({ tags }: { tags?: string[] }) {
  if (!tags || tags.length === 0) return null
  return (
    <Card>
      <CardTitle>{i18nT('pages.appDetailPage.tags')}</CardTitle>
      <div className="flex items-center gap-1.5 flex-wrap mt-2">
        {tags.map(t => (
          <span key={t} className="bg-bg-elevated border border-border px-2 py-0.5 rounded text-[11px] text-muted">{t}</span>
        ))}
      </div>
    </Card>
  )
}
