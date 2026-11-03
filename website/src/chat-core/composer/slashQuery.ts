/** The draft text the `/` command menu shows for: a lone `/` or a bare
 *  command name being typed. Group 1 is the name typed so far.
 *
 *  One matcher on purpose: the menu renders from it (`SlashCommandMenu`), and
 *  the composer's picker gate opens from it (`pickers.ts`). If the two drift,
 *  a menu nobody can see keeps Escape, Tab and ↑/↓ from the composer. */
export const SLASH_MENU_QUERY = /^\/([a-z]*)$/
