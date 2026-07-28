import { Link2, List } from 'lucide-react'
import type { ExtractedLink } from '../../utils/extractChatLinks'
import { dedupResourceLinks } from '../../utils/extractChatLinks'
import type { ChatSection } from '../../hooks/useChatNavigation'
import Clickable from '../../components/Clickable'

import { i18nT } from '../../i18n/t'
interface ChatNavContentProps {
  links: ExtractedLink[]
  sections: ChatSection[]
  onScrollToSection: (displayIdx: number) => void
  resolving?: boolean
}

const TYPE_COLORS: Record<string, string> = {
  taskei: 'bg-blue-500/15 text-blue-400',
  quip: 'bg-purple-500/15 text-purple-400',
  tt: 'bg-orange-500/15 text-orange-400',
  mcm: 'bg-danger/15 text-danger',
  wiki: 'bg-green-500/15 text-green-400',
  sim: 'bg-yellow-500/15 text-yellow-400',
  cr: 'bg-cyan-500/15 text-cyan-400',
  other: 'bg-muted/15 text-muted',
}

const TYPE_LABELS: Record<string, string> = {
  taskei: 'Taskei',
  quip: 'Quip',
  tt: 'TT',
  mcm: 'MCM',
  wiki: 'Wiki',
  sim: 'SIM',
  cr: 'CR',
  other: 'Link',
}

/** Presentational Navigation body — Resources + Outline. Rendered inside the
 *  activity sidebar's Navigation tab (see ActivityViewer). Was formerly a
 *  floating overlay (ChatNavPanel); the overlay shell was dropped when nav
 *  was merged into the activity sidebar. */
export default function ChatNavContent({ links, sections, onScrollToSection, resolving }: ChatNavContentProps) {
  const resourceLinks = dedupResourceLinks(links)
  return (
    <div className="flex-1 overflow-y-auto px-3 py-2 flex flex-col gap-2">
      {/* Key Resources */}
      <div>
        <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-muted font-semibold mb-1.5">
          <Link2 size={11} /> {i18nT('pages.chat.chatNavPanel.resources')}
          {resolving && <span className="ml-auto text-[10px] text-accent animate-pulse">{i18nT('pages.chat.chatNavPanel.resolving')}</span>}
        </div>
        {resourceLinks.length > 0 ? (
          <div className="flex flex-col gap-0.5">
            {resourceLinks.map((link, i) => (
              <a
                key={i}
                href={link.url}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-2 px-2 py-1 rounded hover:bg-bg-hover transition-colors no-underline group"
              >
                <span className={`text-[10px] font-medium px-1.5 py-0.5 rounded ${TYPE_COLORS[link.type] || TYPE_COLORS.other}`}>
                  {TYPE_LABELS[link.type] || 'Link'}
                </span>
                <span className="text-[12px] text-text truncate group-hover:text-accent transition-colors">
                  {link.label}
                </span>
              </a>
            ))}
          </div>
        ) : (
          <span className="text-muted text-[12px] px-2">{i18nT('pages.chat.chatNavPanel.no_links_found')}</span>
        )}
      </div>

      {/* Divider */}
      <div className="border-b border-border" />

      {/* Conversation Outline */}
      <div>
        <div className="flex items-center gap-1.5 text-[11px] uppercase tracking-wider text-muted font-semibold mb-1.5">
          <List size={11} /> {i18nT('pages.chat.chatNavPanel.outline')}
        </div>
        {sections.length > 0 ? (
          <div className="flex flex-col gap-0.5">
            {sections.map((section, i) => (
              <Clickable
                key={i}
                className="text-left text-[12px] leading-tight px-2 py-1.5 rounded text-text hover:bg-bg-hover hover:text-accent transition-colors cursor-pointer truncate"
                onClick={() => onScrollToSection(section.displayIdx)}
                title={section.label}
              >
                <span className="text-muted mr-1.5">{i + 1}.</span>
                {section.label}
              </Clickable>
            ))}
          </div>
        ) : (
          <span className="text-muted text-[12px] px-2">{i18nT('pages.chat.chatNavPanel.start_chatting_to_see_sections')}</span>
        )}
      </div>
    </div>
  )
}
