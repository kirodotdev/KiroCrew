/** A bundle that declares this in `/api/ws?caps=` sends `chunk_watch` and
 *  handles `chat_phase`, `tool_status` and `chunk_watch_ack` (see
 *  `hooks/websocket/chunkWatch.ts`). A wire token, not copy. Its own module so the Mochi panel
 *  and the Crew Companion declare it without loading the dashboard store. */
export const chunkScopeCapability = 'chunk_scope.1'
