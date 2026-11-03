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
