/**
 * A `<mcwidget>` whose body is markdown, not HTML (a "Task board" table, say).
 * Drawn in the HTML frame it reads as raw `**` and `|` text, so the body is
 * rendered as the markdown it is, under its title -- and the title row keeps the
 * artifact identity every widget has (`WidgetArtifact`): the link to the artifact
 * page and the star, which the backend's registration of the widget as an
 * artifact makes meaningful whatever the body is.
 */
import { useContext, type ReactNode } from 'react'
import { IconButtonGroup } from './ui'
import { useWidgetArtifact, WidgetArtifactTitle, WidgetStarButton } from './WidgetArtifact'
import { WidgetFrameProfileContext, widgetFramePolicy } from './widgetFrameProfile'

export default function MarkdownWidget({ body, title, slug, messageTs, slotKey, children }: {
  /** The body as emitted, untrimmed: the artifact content and the derived slug read it. */
  body: string
  title?: string
  slug?: string
  messageTs?: string
  slotKey?: string
  /** The rendered markdown. */
  children: ReactNode
}) {
  // A welcome brief's widget is never saved (widgetFrameProfile.ts).
  const { saveAsArtifact } = widgetFramePolicy(useContext(WidgetFrameProfileContext))
  // An empty title falls back to the hook's default artifact name.
  const artifact = useWidgetArtifact({ body, title: title || undefined, slug, messageTs, slotKey })
  return (
    <section className="group my-2 rounded-lg border border-border bg-bg-elevated/40 px-3 py-2" data-testid="markdown-widget" aria-label={title || undefined}>
      <div className="mb-1 flex items-center justify-between">
        <WidgetArtifactTitle title={title || ''} artifact={artifact} className="text-[12px] font-semibold text-muted truncate" />
        <IconButtonGroup reveal>
          {saveAsArtifact && <WidgetStarButton artifact={artifact} />}
        </IconButtonGroup>
      </div>
      {children}
    </section>
  )
}
