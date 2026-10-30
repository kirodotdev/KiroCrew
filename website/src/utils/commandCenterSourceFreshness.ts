/** Freshness for every `['command-center', *]` source: the app-wide policy is
 *  never-stale, and a finite staleTime here made every window focus re-read all
 *  of them. Frames and reconnect own freshness; a failed source also re-reads on
 *  focus, since its frame may never come. Kept in its own module so a reader
 *  outside the lazy command center chunk (`useSlotHasWorkflowRuns`) shares
 *  these options without pulling that chunk in. */
export const COMMAND_CENTER_SOURCE_FRESHNESS = {
  staleTime: 3_000,
  refetchOnWindowFocus: (query: { state: { status: string } }) => query.state.status === 'error',
}
