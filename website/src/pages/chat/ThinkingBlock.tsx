import { useState, memo } from 'react'
import { AnimatePresence, motion } from 'framer-motion'
import { ChevronRight } from 'lucide-react'

import { i18nT } from '../../i18n/t'
/**
 * Collapsible reasoning trace shown above an assistant answer.
 *
 * kiro-cli/ACP streams the model's chain-of-thought as `agent_thought_chunk`
 * updates; the backend broadcasts them as `chat_thinking` WS events, which the
 * chatSlice accumulates into a content-bearing `thinking`-role message. This
 * component renders that text as a collapsed-by-default disclosure so the
 * reasoning is available without cluttering the conversation.
 *
 * Defaults collapsed (the answer is what matters); click to expand. Reasoning
 * is rendered as dim pre-wrapped text rather than markdown -- thought streams
 * are often partial/ill-formed and shouldn't run through the markdown renderer.
 */
function ThinkingBlock({ content }: { content: string }) {
  const [expanded, setExpanded] = useState(false)
  if (!content) return null

  return (
    <div className="self-start max-w-[550px] w-full">
      <button
        type="button"
        onClick={() => setExpanded(v => !v)}
        className="inline-flex items-center gap-1.5 text-[12px] text-muted hover:text-text transition-colors cursor-pointer bg-transparent border-none p-0 focus:outline-none focus-visible:ring-1 focus-visible:ring-accent rounded-sm"
        aria-expanded={expanded}
        aria-label={expanded ? i18nT('pages.chat.thinkingBlock.collapse_model_reasoning') : i18nT('pages.chat.thinkingBlock.expand_model_reasoning')}
        title={expanded ? i18nT('pages.chat.thinkingBlock.hide_reasoning') : i18nT('pages.chat.thinkingBlock.show_reasoning')}
      >
        <span>{i18nT('pages.chat.thinkingBlock.thinking')}</span>
        <ChevronRight
          size={13}
          className="shrink-0 transition-transform duration-200"
          style={{ transform: expanded ? 'rotate(90deg)' : 'none' }}
        />
      </button>
      <AnimatePresence initial={false}>
        {expanded && (
          <motion.div
            key="reasoning"
            initial={{ height: 0, opacity: 0, marginTop: 0 }}
            animate={{ height: 'auto', opacity: 1, marginTop: 6 }}
            exit={{ height: 0, opacity: 0, marginTop: 0 }}
            transition={{ type: 'spring', damping: 26, stiffness: 280, mass: 0.8 }}
            style={{ overflow: 'hidden' }}
          >
            <div className="max-h-[360px] overflow-auto">
              <div
                className="flex gap-3 py-1 text-[12px] text-muted leading-[1.6] whitespace-pre-wrap"
                style={{ wordBreak: 'break-word' }}
              >
                <span aria-hidden className="w-[3px] shrink-0 self-stretch rounded-full bg-accent opacity-40" />
                <span className="flex-1 min-w-0">{content}</span>
              </div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  )
}

export default memo(ThinkingBlock)
