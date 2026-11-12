/** The window event a bulk Resume run's `resume_all_done` frame is
 *  re-broadcast as. The socket's `emitResumeAllDone` dispatches it and the
 *  sidebar's Resume-all row listens on it.
 *
 *  Its own module, not only an export of the `useWebSocket` facade: many page
 *  tests mock that facade with a fixed export list, and the sidebar row reads
 *  this name when it mounts, so a facade-only home turned every such test red. */
export const RESUME_ALL_DONE_EVENT = 'kirocrew:resume-all-done'
