/** Marks the panel heading the chat dock moves focus to once it has opened the
 * panel: the dock's open action is a small icon button, and the heading names
 * where the user landed. Its own module so the dock does not pull the whole
 * panel into the shell chunk; the panel itself loads lazily with its tab. */
export const PANEL_HEADING_ATTR = 'data-command-center-heading'
