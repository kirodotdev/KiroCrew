import { memo } from 'react'
import { Hourglass, Search, Lightbulb, Settings, Zap, Check, Sparkles, Brain, Pen } from 'lucide-react'
import { motion } from 'framer-motion'

import { i18nT } from '../../i18n/t'
type StopState = 'idle' | 'soft_pending' | 'killing'

const ChatFooter = memo(function ChatFooter({ running, stopping, state, lastRole, regenerating, stopState }: { running: boolean; stopping: boolean; state: string; lastRole: string; regenerating?: boolean; stopState?: StopState }) {
  if (!regenerating && !running && stopState !== 'soft_pending' && stopState !== 'killing') return null
  if (!regenerating && lastRole === 'streaming' && state !== 'compacting' && stopState !== 'soft_pending' && stopState !== 'killing') return null
  if (!regenerating && !stopping && state !== 'compacting' && stopState !== 'soft_pending' && stopState !== 'killing' && (state === 'tool_running' || lastRole === 'tool')) return null
  // width from CSS var --mc-content-width
  return (
    <div data-testid="chat-footer" className={`px-5 mx-auto w-full py-1${regenerating ? '' : ' animate-slide-up'}`} style={{ maxWidth: 'var(--mc-content-width, 900px)' }}>
      <div className="px-3.5 py-2.5">
        {stopState === 'soft_pending' ? (
          <motion.span
            className="text-danger text-[13px] font-mono"
            animate={{ opacity: [0.6, 1, 0.6] }}
            transition={{ duration: 1.2, repeat: Infinity }}
          >{i18nT('pages.chat.chatFooter.stopping')}</motion.span>
        ) : stopState === 'killing' ? (
          <span className="text-danger text-[13px] font-mono">{i18nT('pages.chat.chatFooter.killing')}</span>
        ) : !regenerating && stopping ? (
          <span className="text-muted text-[13px] font-mono animate-pulse">{i18nT('pages.chat.chatFooter.stopping')}</span>
        ) : !regenerating && state === 'compacting' ? (
          <span className="text-muted text-[13px] font-mono animate-pulse"><Hourglass className="lucide-inline" /> {i18nT('pages.chat.chatFooter.compacting')}</span>
        ) : (
          <div className="csb4">
            <div className="slot"><Search size={14} /><Lightbulb size={14} /></div>
            <div className="slot"><Settings size={14} /><Zap size={14} /></div>
            <div className="slot"><Check size={14} /><Sparkles size={14} /></div>
            <div className="slot"><Brain size={14} /><Pen size={14} /></div>
          </div>
        )}
      </div>
    </div>
  )
})

export default ChatFooter
