/** How a command, a payload or a tool ref wraps wherever an approval surface
 *  prints it: at its spaces, like the words it is made of, and inside a token
 *  only when that one token is longer than the line (a sha, a base64
 *  argument, a long path). `break-all` split every word at the edge ("hea" /
 *  "d"), so the reader had to reassemble the string they consent to. Spacing
 *  (`whitespace-pre-wrap`) is set where each string is.
 *
 *  Its own module, with no imports, so a hook (`approval.ts`) can use it
 *  without pulling in the menu components. */
export const COMMAND_WRAP = 'break-normal wrap-anywhere'
