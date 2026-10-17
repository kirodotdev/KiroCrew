# Using LM Studio and OpenRouter models through OpenCode or Pi

Kiro Crew does not call LM Studio or OpenRouter itself. Two of the harnesses it
can drive already do: [OpenCode](https://opencode.ai/docs/) and
[Pi](https://github.com/badlogic/pi-mono/tree/main/packages/coding-agent). Each
keeps its own provider configuration and credentials. Kiro Crew starts the
harness, and the chat model picker lists the models that harness advertises.
Kiro Crew holds no LM Studio or OpenRouter key and needs no setting of its own
for either provider.

The provider steps below follow each harness's own documentation, which is the
authority when the two differ:

- OpenCode: [Providers](https://opencode.ai/docs/providers/) (LM Studio and
  OpenRouter sections), [Config](https://opencode.ai/docs/config/) and
  [CLI](https://opencode.ai/docs/cli/).
- Pi: [Choose a Model](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/models.md),
  [Providers](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/providers.md)
  and [Configuration](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/configuration.md).

Do every step as the user the gateway runs as, on the machine it runs on: the
harness reads that user's configuration, and `localhost` means that machine.

---

## 1. Install and select the harness

Install the harness on the gateway host. These are the commands the dashboard
offers when the harness is missing:

| Harness | Install | What it installs |
|---|---|---|
| OpenCode | `npm i -g opencode-ai@1` | The `opencode` binary, which serves ACP itself (`opencode acp`). This is OpenCode 1, the line the launch is tested against; a binary of any other major is refused. OpenCode's [intro](https://opencode.ai/docs/) lists its other installers. |
| Pi | `npm i -g pi-acp @earendil-works/pi-coding-agent` | The `pi-acp` adapter and the `pi` agent it starts. Pi needs Node.js 22.19 or newer. |

Then select it in **Settings → Agent Harness**, or set it in
`~/.kiro/crew/config.json`:

```json
{
  "agent": {
    "acp_backend": "opencode"
  }
}
```

Use `"pi"` for Pi. The Agent Harness tab reports a harness whose binary is
missing. A chat started on a `pi` too old for the adapter fails with a message
that names the update command.

New chats start on the selected harness. Crew Member DM threads follow
`agent.member_acp_backend` (default `"kas"`) instead. OpenCode is one of the
harnesses a member's DM thread can run on with its dispatch tools, so set that
field to `"opencode"` as well if members should use these models. Pi is not
one of them; leave the field unchanged when you use Pi.

## 2. LM Studio

### Start the LM Studio server

1. In LM Studio, open the **Developer** tab and turn on **Start server**, or
   run `lms server start` in a terminal
   ([LM Studio server docs](https://lmstudio.ai/docs/developer/core/server)).
2. Load the model you want to use. An agent harness calls tools on most turns,
   so pick a model that supports
   [tool use](https://lmstudio.ai/docs/developer/openai-compat/tools).
3. Confirm the OpenAI-compatible endpoint answers and note the model's
   identifier:

   ```bash
   curl http://localhost:1234/v1/models
   ```

   `1234` is the port LM Studio's documentation uses; use yours if you changed
   it. The examples below use the identifier `google/gemma-3n-e4b`.

LM Studio does not require an API token unless you turned on authentication in
its server settings
([Authentication](https://lmstudio.ai/docs/developer/core/authentication)).

### Through OpenCode

1. Add LM Studio as a provider in OpenCode's global config,
   `~/.config/opencode/opencode.json`, so it applies to every project a chat
   runs in. A project's own `opencode.json` works too; OpenCode merges the two.

   ```json
   {
     "$schema": "https://opencode.ai/config.json",
     "provider": {
       "lmstudio": {
         "npm": "@ai-sdk/openai-compatible",
         "name": "LM Studio (local)",
         "options": {
           "baseURL": "http://127.0.0.1:1234/v1"
         },
         "models": {
           "google/gemma-3n-e4b": {
             "name": "Gemma 3n-e4b (local)"
           }
         }
       }
     }
   }
   ```

   `lmstudio` is a provider id you choose, and each key under `models` is an
   identifier from step 3 above. No sign-in is needed for a local model.
2. Optional: make it OpenCode's default model by adding
   `"model": "lmstudio/google/gemma-3n-e4b"` at the top level of the same file.
   A chat on **auto** runs the harness's default.
3. Check that OpenCode sees it:

   ```bash
   opencode models lmstudio
   ```

   The output includes `lmstudio/google/gemma-3n-e4b`.

### Through Pi

1. Add LM Studio as a compatible endpoint in `~/.pi/agent/models.json` (Pi's
   agent directory; `PI_CODING_AGENT_DIR` moves it):

   ```json
   {
     "$schema": "https://pi.dev/schemas/models.schema.json",
     "providers": {
       "lmstudio": {
         "baseUrl": "http://localhost:1234/v1",
         "api": "openai-completions",
         "apiKey": "lm-studio",
         "models": [
           { "id": "google/gemma-3n-e4b" }
         ]
       }
     }
   }
   ```

   Keep the `apiKey` line even though LM Studio does not check it while its
   authentication is off: Pi lists a custom model only once it can resolve a
   credential for its provider. If you turned on LM Studio authentication, put
   your LM Studio token there instead.
2. Check that Pi sees it, from a terminal:

   ```bash
   pi --list-models gemma
   ```

   The output has a row for provider `lmstudio`, model `google/gemma-3n-e4b`.
   Inside Pi, `/model` shows the same model; press `Ctrl+S` on it to save it
   as Pi's default for new sessions.

## 3. OpenRouter

Create a key on the [OpenRouter keys page](https://openrouter.ai/settings/keys)
first.

### Through OpenCode

1. Store the key with OpenCode's own sign-in:

   ```bash
   opencode auth login
   ```

   Choose **OpenRouter** and paste the key. OpenCode saves it in
   `~/.local/share/opencode/auth.json`. The `/connect` command inside the
   OpenCode TUI does the same.
2. List the models OpenCode offers for it:

   ```bash
   opencode models openrouter
   ```

   OpenCode lists the `openrouter` provider only once step 1 has stored a key.
   Many OpenRouter models are preloaded. To add one OpenCode does not list, name
   it under `provider.openrouter.models` in `opencode.json`, as the OpenCode
   providers page shows.
3. Optional: set `"model": "openrouter/<model-id>"` in `opencode.json` to make
   it the default.

### Through Pi

1. Run `pi`, then `/login openrouter`. Choose the API-key method if Pi offers
   more than one, and paste the key. Pi saves it in `~/.pi/agent/auth.json`.
2. Open `/model`. With a key, Pi lists the OpenRouter models that key may use.
   Press `Ctrl+S` on one to make it the default.

## 4. Pick the model in Kiro Crew

1. Start a new chat. Its first session reports the harness's models to Kiro
   Crew; until then the picker offers **auto**, plus the configured default,
   and auto runs the harness's own default model.
2. Open the composer's model picker. It lists the ids the harness advertised,
   in the harness's own `provider/model` form, for example
   `lmstudio/google/gemma-3n-e4b` on OpenCode.
3. Pick one. The choice applies to that chat and is sent to the harness as the
   session's model.

Models added to the harness later appear once a new session reports them.

## 5. Credentials

Kiro Crew stores no provider key for this setup. OpenCode and Pi read their own
credential files (`~/.local/share/opencode/auth.json`,
`~/.pi/agent/auth.json`) when they start. The agent's file tools are refused
those files; the harness process itself still reads them. The exact posture
for each harness is its auth declaration, described in Stage 5 of
[harness-onboarding.md](../system-specs/modules/harness-onboarding.md).

## Troubleshooting

| Symptom | Check |
|---|---|
| "OpenCode is not signed in" or "Pi is not signed in" when a chat starts | The harness found no usable provider. Run the sign-in step for OpenRouter, or add the LM Studio provider entry, then start a new chat. |
| The model is missing from the Kiro Crew picker | Run `opencode models <provider>` or Pi's `/model`. If the harness does not list it, fix the harness config first. If it does, start a new chat so a session reports it. |
| LM Studio requests fail | `curl http://localhost:1234/v1/models` must answer on the gateway host. Check that the server is started and the port in the config matches. |
| Pi lists the LM Studio model nowhere | The provider entry needs an `apiKey`, and `models.json` must be valid JSON. `pi --list-models` reads the file on each run; inside Pi, opening `/model` reloads it. |
| `opencode models openrouter` answers "Provider not found" | No OpenRouter key is stored yet. Run `opencode auth login` and choose OpenRouter. |
| A Crew Member DM does not use the model | Member threads follow `agent.member_acp_backend`, not `agent.acp_backend`. See step 1. |
