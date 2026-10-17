/** Marks the Dashboard panel's heading, the element an opener moves focus to
 * once the panel is shown: the heading names where the user landed. Its own
 * module so an opener does not pull the whole panel into the shell chunk; the
 * panel itself loads lazily with its tab. */
export const PANEL_HEADING_ATTR = 'data-command-center-heading'
