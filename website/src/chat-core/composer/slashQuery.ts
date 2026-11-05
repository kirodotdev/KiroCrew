/** The draft text the `/` command menu shows for: a lone `/` or a bare
 *  command name being typed. Group 1 is the name typed so far.
 *
 *  One matcher on purpose: the menu renders from it (`SlashCommandMenu`), and
 *  the composer's picker gate opens from it (`pickers.ts`). If the two drift,
 *  a menu nobody can see keeps Escape, Tab and ↑/↓ from the composer.
 *
 *  A command name can carry a hyphen, a digit or an underscore after its first
 *  letter (`/security-review`, `/list-dirs`), so the menu keeps filtering
 *  through them; a space ends the name and closes the menu. */
export const SLASH_MENU_QUERY = /^\/([a-z][a-z0-9_-]*)?$/

/** The draft text the same menu lists saved workflows for: `/workflow `
 *  followed by the slug typed so far (group 1). A slug is lowercase letters,
 *  digits and dashes (`workflows/library.py` `_slugify`), so anything else --
 *  a second word, the run input -- means the pick is already made. Shared by
 *  the menu and the picker gate for the same reason as `SLASH_MENU_QUERY`. */
export const WORKFLOW_PICK_QUERY = /^\/workflow ([a-z0-9-]*)$/
