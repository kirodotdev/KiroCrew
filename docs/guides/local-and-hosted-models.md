# Using local and hosted models through OpenCode, Pi, or goose

Kiro Crew does not call LM Studio, OpenRouter, or Ollama itself. The harnesses
it can drive already do: [OpenCode](https://opencode.ai/docs/),
[Pi](https://github.com/badlogic/pi-mono/tree/main/packages/coding-agent), and
[goose](https://block.github.io/goose/). Each keeps its own provider
configuration and credentials. Kiro Crew starts the harness, and the chat model
picker lists the models that harness advertises. Kiro Crew holds no LM Studio,
OpenRouter, or Ollama key and needs no setting of its own for any of them;
`agent.provider` stays `"acp"` throughout, and `agent.acp_backend` is the only
choice that changes.

The provider steps below follow each harness's own documentation, which is the
authority when they differ:

- OpenCode: [Providers](https://opencode.ai/docs/providers/) (LM Studio,
  OpenRouter, and Ollama sections), [Config](https://opencode.ai/docs/config/)
  and [CLI](https://opencode.ai/docs/cli/).
- Pi: [Choose a Model](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/models.md),
  [Providers](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/providers.md)
  and [Configuration](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/configuration.md).
- goose: [block.github.io/goose](https://block.github.io/goose/) and its
  [providers docs](https://block.github.io/goose/docs/getting-started/providers)
  for a local Ollama (OpenAI-compatible) provider.
- Ollama: [ollama.com](https://ollama.com), [github.com/ollama/ollama](https://github.com/ollama/ollama),
  the [FAQ](https://github.com/ollama/ollama/blob/main/docs/faq.md) (exposing
  Ollama on the network) and the
  [OpenAI-compatibility docs](https://github.com/ollama/ollama/blob/main/docs/openai.md).
- Agent Client Protocol: [agentclientprotocol.com](https://agentclientprotocol.com).

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
| goose | `curl -fsSL https://raw.githubusercontent.com/block/goose/main/download_cli.sh \| bash` | A standalone `goose` binary, which also serves ACP itself (`goose acp`). No Node and no separate adapter package. |

Then select it in **Settings → Agent Harness**, or set it in
`~/.kiro/crew/config.json`:

```json
{
  "agent": {
    "acp_backend": "opencode"
  }
}
```

Use `"pi"` for Pi or `"goose"` for goose. The switch takes effect on the
**next** session; an existing chat keeps the backend it started on. The Agent
Harness tab reports a harness whose binary is missing, with the install
command to run. (`kirocrew doctor` shows each selected harness's sign-in state,
but it checks the installed binary only for kiro-cli and Claude, so use the
Agent Harness tab to confirm an OpenCode, Pi, or goose binary is present.) A
chat started on a `pi` too old for the adapter fails with a message that names
the update command.

New chats start on the selected harness. Crew Member DM threads follow
`agent.member_acp_backend` (default `"kas"`) instead. OpenCode, Pi, and goose
are each a harness a member's DM thread can run on with its dispatch tools, so
set that field to the same value (`"opencode"`, `"pi"`, or `"goose"`) as well if
members should use these models.

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

## 4. Ollama

Ollama runs models locally and needs no account or API key. It exposes a
native API and an
[OpenAI-compatible API](https://github.com/ollama/ollama/blob/main/docs/openai.md),
and either OpenCode or goose can be pointed at it.

Pull a model that supports **tool/function calling** first - an agent harness
calls tools on most turns, and a model without tool support will stall. Check
the model's page on [ollama.com/library](https://ollama.com/library) for a
`tools` tag, then pull it:

```bash
ollama pull llama3.2:3b
```

### 4.1 Ollama on the same machine

Ollama's local server listens on `127.0.0.1:11434` by default:

```bash
# Native API: lists local models
curl http://localhost:11434/api/tags

# OpenAI-compatible API: lists the same models in OpenAI shape
curl http://localhost:11434/v1/models
```

If either answers with your pulled models, Ollama is ready.

**Through OpenCode** - add Ollama as a provider in
`~/.config/opencode/opencode.json`, using the same `@ai-sdk/openai-compatible`
shape as LM Studio:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "provider": {
    "ollama": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Ollama (local)",
      "options": {
        "baseURL": "http://127.0.0.1:11434/v1"
      },
      "models": {
        "llama3.2:3b": { "name": "Llama 3.2 3B (local)" }
      }
    }
  }
}
```

Each key under `models` is an id Ollama advertises. Confirm OpenCode sees it:

```bash
opencode models ollama
```

**Through Pi** - add Ollama's OpenAI-compatible endpoint in
`~/.pi/agent/models.json`, the same way as LM Studio in section 2, with
`baseUrl` set to `http://localhost:11434/v1` and a placeholder `apiKey` (Ollama
does not check it).

**Through goose** - configure an OpenAI-compatible (or Ollama) provider pointed
at `http://localhost:11434`. goose stores provider settings in its own config
(run `goose configure`, or edit `~/.config/goose/config.yaml`), following the
goose [providers docs](https://block.github.io/goose/docs/getting-started/providers).

Select the harness as in section 1, for example
`kirocrew config set agent.acp_backend opencode` (or `pi`, or `goose`), then
start a new chat.

### 4.2 Ollama on another machine (across the network)

Ollama can run on a separate host (a GPU box, a home server) while the
harness on the Kiro Crew host reaches it over the network. By default Ollama
binds to `127.0.0.1`, which only accepts local connections. On the **Ollama
host**, bind it to all interfaces with `OLLAMA_HOST`, as described in the
[Ollama FAQ](https://github.com/ollama/ollama/blob/main/docs/faq.md):

```bash
# On the Ollama host
export OLLAMA_HOST=0.0.0.0:11434
ollama serve
```

If Ollama runs as a systemd service, set the variable in the unit environment
(`systemctl edit ollama`, add `Environment="OLLAMA_HOST=0.0.0.0:11434"`) and
restart it. Open port `11434` in the host's firewall so the Kiro Crew host can
reach it.

> **Security caveat:** Ollama has **no built-in authentication**. Binding to
> `0.0.0.0` exposes the model server to every machine that can route to it.
> Only do this on a **trusted LAN**, or put it behind a **reverse proxy that
> adds auth / TLS** or a **VPN** (for example WireGuard/Tailscale), so the raw
> port is never open to untrusted networks. Treat `OLLAMA_HOST=0.0.0.0` as "on
> my private network," not "on the internet."

From the Kiro Crew host, confirm you can reach the remote server (replace
`REMOTE_IP`):

```bash
curl http://REMOTE_IP:11434/api/tags
```

Then point the harness at the remote URL instead of `127.0.0.1`/`localhost`.
For OpenCode, the same provider block as section 4.1 with
`"baseURL": "http://REMOTE_IP:11434/v1"`; for Pi, the same `models.json` entry
with `baseUrl` pointed there; for goose, the same `baseUrl` change in
`goose configure` / `config.yaml`.

If you placed a reverse proxy in front of Ollama (recommended for anything
beyond a trusted LAN), use the **proxy's** URL and scheme instead
(`https://ollama.example.internal`), plus whatever auth header or key the proxy
requires in the harness provider config.

### 4.3 Multiple machines: spreading load around

A single harness process talks to **one endpoint**. Kiro Crew does not
round-robin Ollama hosts internally - there is no such feature. Spreading load
across several Ollama machines is done at the **endpoint layer** or by
**configuring different members at different hosts**. Two approaches, which
you can also combine:

**Approach A - one proxy / load balancer in front of many Ollama hosts.** Stand
up a reverse proxy or load balancer (for example [nginx](https://nginx.org), or
a purpose-built Ollama proxy/load-balancer) that fronts several Ollama hosts
and exposes a **single endpoint**. Point the harness config at that one
endpoint; the proxy distributes requests across the backends.

Minimal nginx upstream sketch (treat as a starting point; add TLS and auth per
the security caveat in section 4.2):

```nginx
upstream ollama_pool {
    server 10.0.0.11:11434;
    server 10.0.0.12:11434;
    server 10.0.0.13:11434;
}

server {
    listen 8080;
    location / {
        proxy_pass http://ollama_pool;
        proxy_set_header Host $host;
    }
}
```

The harness base URL then becomes `http://PROXY_IP:8080/v1` (OpenCode, Pi) or
`http://PROXY_IP:8080` (goose). From Kiro Crew's point of view there is still
one harness and one endpoint; the fan-out is entirely the proxy's concern. Each
Ollama host behind the proxy must have the **same model pulled**, since the
proxy forwards an opaque model id - keep the model catalogs in sync across the
pool.

**Approach B - different crew members / sessions at different hosts.** Because
one session uses one harness and one endpoint, you can split work by pointing
different parts of Kiro Crew at different Ollama hosts: run more than one
session or worker, each driving a harness configured for a different Ollama
host. For **crew-member DM sessions** specifically, the backend is chosen by
`agent.member_acp_backend` (not `agent.acp_backend`); set it to a local harness
if members should also run on Ollama:

```bash
kirocrew config set agent.member_acp_backend opencode   # or goose
```

Approach A spreads load per request; Approach B spreads it per session or
member. Approach A is the cleaner answer for raw throughput across N boxes;
Approach B helps when specific members or workloads should be pinned to
specific hardware. They compose: point the harness at a proxy **and** assign
members to different proxies/pools.

## 5. Pick the model in Kiro Crew

1. Start a new chat. Its first session reports the harness's models to Kiro
   Crew; until then the picker offers **auto**, plus the configured default,
   and auto runs the harness's own default model.
2. Open the composer's model picker. It lists the ids the harness advertised,
   in the harness's own `provider/model` form, for example
   `lmstudio/google/gemma-3n-e4b` on OpenCode, or `ollama/llama3.2:3b`.
3. Pick one. The choice applies to that chat and is sent to the harness as the
   session's model.

Models added to the harness later appear once a new session reports them.
While a turn runs on Ollama, `ollama ps` on the Ollama host shows the model
loaded and serving the request, which confirms the turn landed there rather
than on a hosted provider.

## 6. Switching back and forth with kiro-cli / KAS

The backend is one config key, so moving between a local or hosted-model
harness and the cloud defaults is a single command each way. Set `acp_backend`
to one concrete value from the table below, for example:

```bash
kirocrew config set agent.acp_backend goose
```

| Goal | Command |
|---|---|
| Use the local **OpenCode** harness | `kirocrew config set agent.acp_backend opencode` |
| Use the local **Pi** harness | `kirocrew config set agent.acp_backend pi` |
| Use the local **goose** harness | `kirocrew config set agent.acp_backend goose` |
| Back to **kiro-cli** | `kirocrew config set agent.acp_backend kiro` |
| Back to the cloud default **KAS** | `kirocrew config set agent.acp_backend kas` |

`agent.provider` always stays `acp`; you only ever change `acp_backend`. The
switch takes effect on the **next** session - an existing chat keeps the
backend it started on, so start a new chat after switching. An explicit value
that is unrecognized or unselectable logs a warning and falls back to the Kiro
CLI (kiro-cli) floor, not KAS; read it back with
`kirocrew config get agent.acp_backend`, and check **Settings → Agent Harness**
to confirm the harness is installed. To move crew-member DM sessions too, set
`agent.member_acp_backend` the same way (`kiro` there selects KAS, not
kiro-cli).

## 7. Choosing a harness: OpenCode vs Pi vs goose

All three serve ACP natively (`opencode acp`, `pi-acp`, `goose acp`) and run
the operator's own provider and model ids, including LM Studio, OpenRouter, and
Ollama. Pick based on what you are connecting to and how you want to install
and configure it:

| Aspect | OpenCode | Pi | goose |
|---|---|---|---|
| Project | [OpenCode](https://opencode.ai) (sst) | [Pi](https://github.com/badlogic/pi-mono/tree/main/packages/coding-agent) | [goose](https://block.github.io/goose/) (Block) |
| Install | `npm i -g opencode-ai@1` (Node/npm) | `npm i -g pi-acp @earendil-works/pi-coding-agent` (Node 22.19+) | `curl … download_cli.sh \| bash` (standalone installer, no Node) |
| Binary override env var | `OPENCODE_BIN` | `PI_ACP_BIN` / `PI_ACP_PI_COMMAND` | `GOOSE_BIN` |
| Provider config | `provider.<id>` block in `opencode.json`, `@ai-sdk/openai-compatible` for a local/compatible endpoint | `providers.<id>` block in `~/.pi/agent/models.json`, `api: "openai-completions"` for a local/compatible endpoint | `goose configure` / `~/.config/goose/config.yaml` |
| Hosted-provider sign-in | `opencode auth login` (OpenRouter and others) | `/login <provider>` inside the `pi` TUI | Per-provider, through `goose configure` |
| Member DM dispatch | Supported (`agent.member_acp_backend`) | Supported | Supported |

Reach for **OpenCode** when your gateway host already has Node/npm and you are
mainly pointing at LM Studio, OpenRouter, or Ollama through one JSON provider
block. Its install is pinned to major 1, so a newer OpenCode major is refused
until Kiro Crew's launch supports it.

Reach for **Pi** when you want its own model picker and sign-in flow
(`/model`, `/login`), or you are already using Pi's `models.json` /
`auth.json` layout for other providers.

Reach for **goose** when you would rather not manage an npm global and prefer
a standalone binary, or you already use goose's own configuration and
extension ecosystem for Ollama or another OpenAI-compatible endpoint. Kiro Crew
tested goose on the 1.50.x line; a goose outside it still runs, with a warning
row in the chat that names the installed version and the tested range.

All three reach the same providers the same way underneath, so you can try one
and switch with a single `kirocrew config set` command (section 6).

## 8. Credentials

Kiro Crew stores no provider key for any of these setups. OpenCode, Pi, and
goose read their own credential files (`~/.local/share/opencode/auth.json`,
`~/.pi/agent/auth.json`, `~/.config/goose/secrets.yaml`) when they start. The
agent's file tools are refused those files; the harness process itself still
reads them. The exact posture for each harness is its auth declaration,
described in Stage 5 of
[harness-onboarding.md](../system-specs/modules/harness-onboarding.md).

## Troubleshooting

For anything about whether the selected harness is installed, check
**Settings → Agent Harness**: it reports a missing harness binary and the
command that installs it.

| Symptom | Check |
|---|---|
| "OpenCode is not signed in" or "Pi is not signed in" when a chat starts | The harness found no usable provider. Run the sign-in step for OpenRouter, or add the LM Studio or Ollama provider entry, then start a new chat. |
| The model is missing from the Kiro Crew picker | Run `opencode models <provider>` or Pi's `/model` (goose: `/model` inside its TUI). If the harness does not list it, fix the harness config first. If it does, start a new chat so a session reports it. |
| LM Studio requests fail | `curl http://localhost:1234/v1/models` must answer on the gateway host. Check that the server is started and the port in the config matches. |
| Harness cannot reach Ollama | `curl http://HOST:11434/api/tags` must answer from the Kiro Crew host. For a remote host, confirm `OLLAMA_HOST=0.0.0.0:11434` on the Ollama side and that the firewall allows port `11434`. |
| Pi lists the LM Studio or Ollama model nowhere | The provider entry needs an `apiKey`, and `models.json` must be valid JSON. `pi --list-models` reads the file on each run; inside Pi, opening `/model` reloads it. |
| `opencode models openrouter` answers "Provider not found" | No OpenRouter key is stored yet. Run `opencode auth login` and choose OpenRouter. |
| A turn on Ollama stalls or loops without using tools | The chosen Ollama model likely lacks tool/function-calling support. Pick a tool-capable model from [ollama.com/library](https://ollama.com/library). |
| New chats still hit the cloud after switching to a local harness | The switch applies on the next session - start a new chat. Re-read `agent.acp_backend` with `kirocrew config get agent.acp_backend`; an explicit value that is unrecognized or unselectable falls back to the Kiro CLI (kiro-cli) floor, not KAS. |
| The selected harness is not installed | **Settings → Agent Harness** reports a missing harness binary and prints the command that installs it (`npm i -g opencode-ai@1`, Pi's install command, or goose's `download_cli.sh`). If the binary lives somewhere unusual, set `OPENCODE_BIN` / `PI_ACP_BIN` / `GOOSE_BIN`. |
| A Crew Member DM does not use the model | Member threads follow `agent.member_acp_backend`, not `agent.acp_backend`. See section 1 and section 6. |

For the authoritative backend reference, see the `agent.acp_backend` section of
[configuration.md](../../src/kiro_crew/docs/configuration.md).
