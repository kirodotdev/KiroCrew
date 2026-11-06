/**
 * Frozen inventory of the source files that name a stream frame `chunk_scope.1`
 * holds back (`chat_chunk`, `chat_thinking`, `tool_call`, `tool_result`).
 *
 * A dashboard socket that declares the capability gets these frames only for the
 * slots its `chunk_watch` set names (hooks/websocket/chunkWatch.ts). A new reader
 * of one of them that renders a slot the tab does not watch would silently see
 * nothing, so adding a file here is a decision: say why it still works.
 */
import { describe, it, expect } from 'vitest'
import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'

const SRC = join(__dirname, '..')
const FRAME = /['"](tool_call|tool_result|chat_chunk|chat_thinking)['"]/

/** Each file, with why it is not broken by the watch set. */
const READERS: Record<string, string> = {
  'app-sdk/ChatMessageList.tsx': 'message roles in a comment, not frames',
  'app-sdk/index.ts': 'the app SDK event list; app-token sockets are never scoped',
  'app-sdk/messageRenderers.tsx': 'message roles, not frames',
  'apps/mochi/panel/panelBridge.ts': 'its own socket watches the pet slot, the only slot it reads',
  'apps/mochi/src/shared/types.ts': 'a pet activity name, not a frame',
  'components/chat/crewmateBubbles.ts': 'message roles, not frames',
  'hooks/useWebSocket.ts': 'the frame router; stream frames arrive only for watched slots',
  'hooks/websocket/chatStream.ts': 'message roles, not frames',
  // Spelled in parts: chatSlice.ownership.test.ts reads a literal store path
  // outside src/store as an import of an owner.
  [['store', 'chat', 'selectors.ts'].join('/')]: 'message roles, not frames',
  'utils/appSlotPush.ts': 'app embed signals; an embed slot is in the watch set (appWatchedSlots)',
}

function sourceFiles(dir: string): string[] {
  const out: string[] = []
  for (const name of readdirSync(dir)) {
    const path = join(dir, name)
    if (statSync(path).isDirectory()) {
      if (name !== 'test' && name !== 'node_modules') out.push(...sourceFiles(path))
    } else if (/\.tsx?$/.test(name) && !/\.test\.tsx?$/.test(name)) {
      out.push(path)
    }
  }
  return out
}

describe('stream frame consumers', () => {
  it('names every file that reads a scoped stream frame', () => {
    const found = sourceFiles(SRC)
      .filter(path => FRAME.test(readFileSync(path, 'utf8')))
      .map(path => relative(SRC, path).split('\\').join('/'))
      .sort()
    expect(found).toEqual(Object.keys(READERS).sort())
  })
})
