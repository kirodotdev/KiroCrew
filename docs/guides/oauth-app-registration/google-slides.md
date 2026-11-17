# Google Slides card: Google notes

Set up Google once for every Google card with [google.md](google.md): run the script, three clicks, paste once. This page keeps only what is specific to Google Slides.

| | |
|---|---|
| Redirect URI | `http://127.0.0.1:48111/callback` |
| MCP endpoint | `https://slidesmcp.googleapis.com/mcp/v1` |
| APIs the script enables | `slides.googleapis.com` and `slidesmcp.googleapis.com` |
| Environment variable prefix | `KIROCREW_CONNECTIONS_GOOGLE_SLIDES_` (`CLIENT_ID`, `CLIENT_SECRET`) |

## Scopes

| Scope | Kiro Crew requests it | What it allows | Tools it serves |
|---|---|---|---|
| `https://www.googleapis.com/auth/presentations.readonly` | yes | See all your presentations | `read_presentation`, `read_slide_page`, `read_slide_page_thumbnail` |
| `https://www.googleapis.com/auth/drive.readonly` | yes | **Restricted**. View and download all Drive files | Google lists it for this server |
| `https://www.googleapis.com/auth/presentations` | no | See, edit, create and delete presentations | `update_presentation` |
| `https://www.googleapis.com/auth/drive.file` | no | Non-sensitive. Files the app opened or created | creating files |

Google's page does not map tools to scopes; the mapping follows each scope's description. Scopes on **Data Access** are a ceiling; the consent screen shows what Kiro Crew actually asks for.

## Check it works

After **Connect** shows **Connected**, ask in a chat: "List the slide titles in the presentation Q3 Review." (use a real name or paste its link).

Tools the server lists: `read_presentation`, `read_slide_page`, `read_slide_page_thumbnail`, `update_presentation`.

## Gotchas

- `update_presentation` fails with a permission error. Kiro Crew asks Google only for the read-only scopes above, so adding more on **Data Access** does not change what Connect grants.

## Sources

- https://developers.google.com/workspace/slides/api/guides/configure-mcp-server
- https://developers.google.com/workspace/guides/configure-mcp-servers
