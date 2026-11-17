# Google Sheets card: Google notes

Set up Google once for every Google card with [google.md](google.md): run the script, three clicks, paste once. This page keeps only what is specific to Google Sheets.

| | |
|---|---|
| Redirect URI | `http://127.0.0.1:48109/callback` |
| MCP endpoint | `https://sheetsmcp.googleapis.com/mcp/v1` |
| APIs the script enables | `sheets.googleapis.com` and `sheetsmcp.googleapis.com` |
| Environment variable prefix | `KIROCREW_CONNECTIONS_GOOGLE_SHEETS_` (`CLIENT_ID`, `CLIENT_SECRET`) |

## Scopes

| Scope | Kiro Crew requests it | What it allows | Tools it serves |
|---|---|---|---|
| `https://www.googleapis.com/auth/spreadsheets.readonly` | yes | See all your spreadsheets | `get_spreadsheet`, `get_values` |
| `https://www.googleapis.com/auth/drive.readonly` | yes | **Restricted**. View and download all Drive files | Google lists it for this server |
| `https://www.googleapis.com/auth/spreadsheets` | no | See, edit, create and delete spreadsheets | `update_spreadsheet`, `update_values`, `update_formulas`, `insert_dimension` |
| `https://www.googleapis.com/auth/drive.file` | no | Non-sensitive. Files the app opened or created | creating files |

Google's page does not map tools to scopes; the mapping follows each scope's description. Scopes on **Data Access** are a ceiling; the consent screen shows what Kiro Crew actually asks for.

## Check it works

After **Connect** shows **Connected**, ask in a chat: "Read the first ten rows of the spreadsheet Budget 2026." (use a real name or paste its link).

Tools the server lists: `get_spreadsheet`, `get_values`, `insert_dimension`, `update_formulas`, `update_spreadsheet`, `update_values`.

## Gotchas

- The write tools fail with a permission error. Kiro Crew asks Google only for the read-only scopes above, so adding more on **Data Access** does not change what Connect grants.

## Sources

- https://developers.google.com/workspace/sheets/api/guides/configure-mcp-server
- https://developers.google.com/workspace/guides/configure-mcp-servers
