/**
 * chat-core P5-e contract: the virtualized transcript is ONE unit that every
 * non-page host mounts, not a per-host copy. Source-text guards, in the style
 * of the transport contract and the scroll-shell recipe: jsdom cannot see a
 * scroller's cost, but it can see which module owns it.
 */
import { existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'

const read = (rel: string) => readFileSync(resolve(__dirname, rel), 'utf8')

const HOSTS = {
  ChatPane: read('../components/ChatPane.tsx'),
  SideChat: read('../pages/chat/SideChat.tsx'),
  ChatEmbed: read('../app-sdk/ChatEmbed.tsx'),
}

describe('chat-core transcript contract', () => {
  it('every non-page host mounts ChatMessageList with the transcript wiring instead of its own scroller', () => {
    // The hook the hosts used to wear is gone with its last consumer.
    expect(existsSync(resolve(__dirname, '../app-sdk/useChatScrollFollow.ts'))).toBe(false)
    for (const [name, src] of Object.entries(HOSTS)) {
      expect(src, `${name} mounts the transcript`).toContain('transcript={{')
      expect(src, `${name} has no follow hook of its own`).not.toContain('useChatScrollFollow')
      expect(src, `${name} has no transcript scroll div of its own`).not.toMatch(/className="[^"]*overflow-y-auto[^"]*"[^>]*>\s*\n?\s*<div ref=\{follow\.contentRef\}/)
    }
  })

  it('each host partitions the height/anchor caches under its own prefix', () => {
    expect(HOSTS.ChatPane).toContain('sessionId: `pane:${slotKey}`')
    expect(HOSTS.SideChat).toContain('sessionId: `side:${slot}`')
    expect(HOSTS.ChatEmbed).toContain('sessionId: `embed:${slotKey}`')
  })

  it('ChatEmbed polls a bounded page and pages earlier history through the transcript bar', () => {
    expect(HOSTS.ChatEmbed).toContain("'?limit=' + limit")
    expect(HOSTS.ChatEmbed).toContain('earlier: { hasMore: canWiden')
    // A rejected widen keeps the settled page on screen and offers a retry.
    expect(HOSTS.ChatEmbed).toContain('const shown = slotData ?? settled')
    expect(HOSTS.ChatEmbed).toContain('onLoad: widenFailed ? retryWiden : widen, handOff: false')
    // A failed FIRST read is an error with a retry, never the empty-session copy.
    expect(HOSTS.ChatEmbed).toContain('testId="chat-embed-load-error"')
    // Placeholder rows survive a widen of the SAME slot only.
    expect(HOSTS.ChatEmbed).toContain("(prevQuery.queryKey as unknown[])[1] === slotKey ? prev : undefined")
  })

  it('VirtualTranscript composes the page virtualizer, the shared shell and the row-identity builders', () => {
    const src = read('../chat-core/transcript/VirtualTranscript.tsx')
    expect(src).toContain("from '../../hooks/virtualizer/useVirtualChat'")
    expect(src).toContain("from '../../pages/chat/TranscriptScrollShell'")
    expect(src).toContain("from './rowKeys'")
    // The page's overlay-header band is the page's; hosts must not reserve it.
    expect(src).toContain('headerSpacer={false}')
    // Rows are the same measured, indexed blocks the page mounts.
    expect(src).toContain('if (!vi.mounted) return null')
    expect(src).toContain('ref={virt.measureRef(vi.index)}')
    expect(src).toContain('data-display-index={vi.index}')
  })

  it('the page keeps its row-identity import path through a re-export of the chat-core module', () => {
    const page = read('../pages/chat/ChatPageMessageContent.tsx')
    expect(page).toContain("} from '../../chat-core/transcript/rowKeys'")
    const keys = read('../chat-core/transcript/rowKeys.ts')
    for (const name of ['msgIdentityKey', 'turnLeadKey', 'virtualKeyFor', 'uniqueRowKeys', 'stableAnchorIdFor', 'anchorAltIdFor']) {
      expect(keys, name).toContain(`export function ${name}(`)
      expect(page, `page re-exports ${name}`).toContain(name)
    }
  })

  it('ChatMessageList is the SDK seam: a transcript prop that mounts VirtualTranscript, else the bare fragment', () => {
    const list = read('../app-sdk/ChatMessageList.tsx')
    expect(list).toContain("from '../chat-core/transcript/VirtualTranscript'")
    expect(list).toContain('transcript?: TranscriptMount')
    expect(list).toContain('if (transcript) {')
    expect(list).toContain('{displayItems.map(renderDisplayItem)}')
  })
})
