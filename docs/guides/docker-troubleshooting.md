# Docker Troubleshooting Guide

Practical solutions for common issues when running Kiro Crew in Docker. For
general setup and configuration, see [docker.md](docker.md).

---

## 1. Container starts but dashboard is unreachable

**Symptoms:** `docker ps` shows the container running, but browsing to
`http://localhost:5476` times out or refuses the connection.

### Check port binding

```bash
# Verify the port mapping
docker port kirocrew
# Expected: 5476/tcp -> 0.0.0.0:5476 (or 127.0.0.1:5476)
```

If no mapping shows, you forgot `-p` in `docker run` or the `ports:` key in
compose. Re-create the container with the correct mapping:

```bash
docker run -d --name kirocrew \
  -p 127.0.0.1:5476:5476 \
  -v kirocrew-home:/home/kirocrew \
  ghcr.io/kirodotdev/kirocrew:stable
```

### Check KIROCREW_BIND

The image defaults to `KIROCREW_BIND=0.0.0.0` so published ports work. If
you overrode this to `127.0.0.1`, the gateway only listens on the
container's internal loopback — unreachable from the host.

```bash
docker exec kirocrew printenv KIROCREW_BIND
```

Remove any override or explicitly set `-e KIROCREW_BIND=0.0.0.0`.

### Check KIROCREW_PORT mismatch

If you changed `KIROCREW_PORT` inside the container but did not update the
`-p` mapping, the host forwards to the wrong port:

```bash
# If KIROCREW_PORT=8080 inside the container:
docker run -d --name kirocrew \
  -p 127.0.0.1:8080:8080 \
  -e KIROCREW_PORT=8080 \
  -v kirocrew-home:/home/kirocrew \
  ghcr.io/kirodotdev/kirocrew:stable
```

### Firewall / Docker Desktop

- **Linux:** check `iptables -L -n` or `nft list ruleset` for DROP rules
  on the Docker bridge.
- **macOS / Windows (Docker Desktop):** the VM network stack may need a
  restart. Try `docker restart kirocrew` or restart Docker Desktop.
- **Remote host:** ensure the host firewall allows inbound on the published
  port, and use the host's IP (not `localhost`).

---

## 2. kiro-cli login fails inside the container

**Symptoms:** `docker exec kirocrew kiro-cli login` hangs, shows garbled
output, or immediately exits with "not a terminal."

The login command itself, and where its credentials persist, are covered by
[docker.md → First-run setup](docker.md#first-run-setup). This section only
covers what makes that command fail.

### Allocate a TTY

`kiro-cli login` is interactive and needs a pseudo-terminal. Always use
`-it`:

```bash
docker exec -it kirocrew kiro-cli login
```

If you omit `-it`, the login prompt has no TTY to read from and fails.

Login is therefore a **one-time interactive step**; there is no supported
non-interactive equivalent. Credentials persist in the volume
([docker.md → State and upgrades](docker.md#state-and-upgrades)), so a
CI-driven deployment logs in once against the volume it will keep reusing —
don't try to bake the login into the pipeline itself.

### Docker Desktop TTY issues on Windows

Git Bash / MINGW terminals sometimes break TTY passthrough. Use PowerShell
or `cmd.exe` instead:

```powershell
docker exec -it kirocrew kiro-cli login
```

Or prefix with `winpty` in Git Bash:

```bash
winpty docker exec -it kirocrew kiro-cli login
```

---

## 3. Permission denied errors

**Symptoms:** the container exits immediately with permission errors, or the
dashboard cannot save settings / write files.

### Volume ownership (uid mismatch)

The container runs as `kirocrew` (uid 1000). If the volume was previously
owned by another uid, or you bind-mount a host directory owned by a
different user, writes fail.

**Fix for named volumes** (first time):

```bash
# Named volumes inherit ownership from the image — usually fine.
# If corrupted, reset ownership:
docker exec -u 0 kirocrew chown -R kirocrew:kirocrew /home/kirocrew
```

**Fix for bind mounts:**

```bash
# On the host, set the directory to uid 1000:
sudo chown -R 1000:1000 /path/to/host/dir

# Then mount:
docker run -d --name kirocrew \
  -p 127.0.0.1:5476:5476 \
  -v /path/to/host/dir:/home/kirocrew \
  ghcr.io/kirodotdev/kirocrew:stable
```

### Read-only filesystem

If you accidentally added `:ro` to the volume mount, remove it:

```yaml
# Wrong:
volumes:
  - kirocrew-home:/home/kirocrew:ro
# Correct:
volumes:
  - kirocrew-home:/home/kirocrew
```

### SELinux (Fedora / RHEL)

On SELinux-enforcing hosts, bind mounts need the `:z` or `:Z` suffix:

```bash
-v /path/to/host/dir:/home/kirocrew:Z
```

---

## 4. Sandbox-related errors

**Symptoms:** agent commands are refused with "No OS-level sandbox backend is
available on this host"; the startup log says agent command execution is
`DISABLED (fail-closed)`.

### Check the startup log

```bash
docker logs kirocrew | grep '\[entrypoint\]'
```

The fail-closed posture announces itself on the run that chose it. The
entrypoint emits this as one long line — wrapped here to read:

```
[entrypoint] First run: NO inner sandbox backend under this runtime's seccomp
policy. Seeded /home/kirocrew/.kiro/crew/config.json with sandbox=auto: agent
command execution is DISABLED (fail-closed) until you choose one of: (a) permit
user namespaces (--security-opt seccomp=<profile permitting unshare/clone>) and
restart to get the inner sandbox, or (b) restart with -e
KIROCREW_ALLOW_UNSANDBOXED=1 to explicitly accept unsandboxed agent execution
(the container is then the only isolation boundary).
```

That line is written **only on the run that seeds the config**, so a container
whose volume already has one will not repeat it — scroll back to the first run,
or grep the phrase that a later start does print:

```bash
# On a first run: which posture was chosen.
docker logs kirocrew | grep 'inner sandbox backend'
# On every later start with an unset opt-out: the standing reminder.
docker logs kirocrew | grep 'sandbox_allow_unsandboxed_exec is not set'
```

### Pick a posture

The postures, the probe table that maps each log line to the posture it
wrote, and the three ways out (seccomp profile · explicit unsandboxed
consent · `--privileged`) are documented once in
[docker.md → Sandbox troubleshooting](docker.md#sandbox-troubleshooting).
Start there — this section exists only to get you from the symptom to that
table.

The short version: the sandbox needs `unshare(CLONE_NEWUSER)` and
`unshare(CLONE_NEWNS)`, which Docker's default seccomp profile blocks, so
the probe fails closed and agent execution stays disabled until you choose.
Prefer [Option A — the shipped seccomp profile](docker.md#option-a--kiro-crew-seccomp-profile-recommended);
fall back to [Option B](docker.md#option-b--explicit-unsandboxed-consent)
only where you cannot set seccomp at all (managed Kubernetes, some Docker
Desktop setups).

### `KIROCREW_ALLOW_UNSANDBOXED=1` had no effect

That variable is read on **first run only** — the entrypoint probes and
seeds `config.json` when the volume has none, and an existing `config.json`
is operator-owned and never rewritten. So adding the variable to a container
whose volume already holds a config changes nothing, and the startup log
says so:

```
[entrypoint] Note: agent.sandbox_allow_unsandboxed_exec is not set in …/config.json.
```

Set the key in the file instead. `config.json` is JSON, and the image ships
no editor, so use the copy-out / edit / copy-back recipe in
[docker.md → Configuration](docker.md#configuration) (it includes the
`chown` back to uid 1000 that `docker cp` otherwise breaks) and add:

```json
{
  "agent": {
    "sandbox": "auto",
    "sandbox_allow_unsandboxed_exec": true
  }
}
```

The seccomp route needs no such edit: every first-run branch seeds
`agent.sandbox=auto`, which detects the backend at each start, so adding the
profile and restarting is enough. Confirm with
[docker.md → Verifying your posture](docker.md#verifying-your-posture)
rather than assuming the flag you passed took.

---

## 5. Health check failing

**Symptoms:** `docker ps` shows `(unhealthy)`; orchestrators restart the
container in a loop.

### Allow startup time

The gateway needs a few seconds to initialize. The image `HEALTHCHECK` has a
`--start-period` grace window, but custom orchestrator probes (Kubernetes
`livenessProbe`) may not. Increase `initialDelaySeconds`:

```yaml
livenessProbe:
  httpGet:
    path: /api/health
    port: 5476
  initialDelaySeconds: 15
  periodSeconds: 10
```

### Port mismatch

If you set `KIROCREW_PORT` to a non-default value but did not update the
health check, the probe hits the wrong port:

```bash
# Check what port the gateway is actually listening on:
docker exec kirocrew printenv KIROCREW_PORT
```

For custom Kubernetes probes, match the port. The built-in Docker
`HEALTHCHECK` uses the container's own port automatically — this issue
only affects external probes that hardcode `5476`.

### Gateway crash-looping

If health fails because the process is crashing, check logs:

```bash
docker logs --tail 50 kirocrew
```

Common causes: a corrupted `config.json`, or missing credentials for a
configured channel bot (remove the channel config or supply the token).

For the corrupted-config case, **rename the file — never delete it.** It
holds every setting you have ever changed (sandbox posture, channel wiring,
model choices) and the gateway seeds only bare defaults in its place; the
copy is the only way back, and reading it is usually how you find the one
bad edit:

```bash
# Keep the original under a name no later recovery can clobber, then let the
# gateway seed defaults on restart:
docker exec kirocrew sh -c \
  'mv /home/kirocrew/.kiro/crew/config.json \
      "/home/kirocrew/.kiro/crew/config.json.broken.$(date +%Y%m%d-%H%M%S)"'
docker restart kirocrew

# Read the saved copies on the host to recover your settings:
docker exec kirocrew ls /home/kirocrew/.kiro/crew/config.json.broken.*
docker cp kirocrew:/home/kirocrew/.kiro/crew/config.json.broken.<stamp> .
```

A plain `.broken` suffix would be overwritten the second time you did this,
destroying the copy from the first attempt — which is the one holding your
working settings.

---

## 6. Session data lost after restart

**Symptoms:** after `docker compose down && docker compose up -d`, agents
are logged out, settings are gone, or chat history is empty.

### Volume not mounted

If you omit the volume mount, all state lives in the ephemeral container
layer and vanishes on removal:

```bash
# WRONG — no volume:
docker run -d --name kirocrew -p 5476:5476 ghcr.io/kirodotdev/kirocrew:stable

# CORRECT — named volume:
docker run -d --name kirocrew \
  -p 127.0.0.1:5476:5476 \
  -v kirocrew-home:/home/kirocrew \
  ghcr.io/kirodotdev/kirocrew:stable
```

### `docker compose down -v` removes volumes

The `-v` flag deletes named volumes. Use `docker compose down` (no `-v`) to
keep data:

```bash
# Preserves volumes:
docker compose down
docker compose up -d

# DESTROYS volumes (data loss):
docker compose down -v   # ← don't do this unless you intend a full reset
```

### Bind mount pointing to wrong directory

If you use a bind mount, ensure the path is correct and consistent:

```yaml
volumes:
  - ./data/kirocrew:/home/kirocrew   # relative path — ensure compose always runs from the same dir
```

Prefer absolute paths or named volumes for production.

---

## 7. Agent can't execute commands

**Symptoms:** the agent responds conversationally but refuses to run commands,
reporting that "No OS-level sandbox backend is available on this host, and the
agent subprocess cannot be safely isolated."

### Sandbox consent required

If the sandbox probe failed and `KIROCREW_ALLOW_UNSANDBOXED` is not set,
agent execution is disabled by design. See [section 4](#4-sandbox-related-errors).

### Missing tools in the container

The image is minimal — it does not include language runtimes, compilers, or
package managers beyond Python. If the agent needs `git`, `node`, `gcc`, etc.,
they are not available by default.

**Options:**

1. **Build a custom image** extending the official one — the reliable route:

   ```dockerfile
   FROM ghcr.io/kirodotdev/kirocrew:stable
   USER root
   RUN apt-get update && apt-get install -y git nodejs npm && rm -rf /var/lib/apt/lists/*
   USER kirocrew
   ```

2. **Mount a tools volume read-only.** Only worth trying for a statically
   linked binary — a host `git` or `node` is dynamically linked against the
   host's libc and will not run under this image. Where it does apply, mount
   it `:ro`: the container runs as uid 1000, which a typical host account
   shares, so a writable mount lets the agent replace the executable and the
   next host invocation runs whatever it wrote.

   ```bash
   -v /opt/static-tools:/opt/tools:ro
   ```

3. **Use the agent in "plan-only" mode** — let it generate code and
   instructions that you apply on the host.

### Agent skill not found

Skills are files, not packages: the built-in set is synced from the wheel to
`~/.kiro/crew/skills/` at startup, and there is no install command to run.
List what the container actually has:

```bash
docker exec kirocrew ls /home/kirocrew/.kiro/crew/skills
```

To add your own, write it into that directory and restart:

```bash
docker cp ./my-skill kirocrew:/home/kirocrew/.kiro/crew/skills/my-skill
docker exec -u 0 kirocrew chown -R kirocrew:kirocrew /home/kirocrew/.kiro/crew/skills/my-skill
docker restart kirocrew
```

---

## 8. High memory usage

**Symptoms:** the container uses several GB of RAM; the host swaps or the
OOM killer terminates the container.

### Cap the container

Each active chat session can spawn subagents, so peak usage scales with how
many sessions are running at once. There is no setting that caps that count
— bound the container instead:

```yaml
# compose.yaml
services:
  kirocrew:
    # ...
    deploy:
      resources:
        limits:
          memory: 4G
```

With a hard limit, the kernel OOM-kills the container rather than swapping
the entire host. Monitor usage:

```bash
docker stats kirocrew --no-stream
```

### Model downloads

The embedding model downloads on first use into `~/.kiro/crew/models/`
inside the volume (a flat directory — that is the only tree
`embeddings.py` writes models to). Downloads stage into that same
directory as `.<model-file>.<pid>.tmp` and are unlinked in a `finally`, so a
retry does not accumulate files — only a hard kill mid-download (OOM,
`docker kill`) can strand one. Check before clearing anything:

```bash
docker exec kirocrew ls -la /home/kirocrew/.kiro/crew/models
# Strays are dot-prefixed and end in .tmp; the real model files are not.
# -mmin +60 is what makes this safe: it skips a download still in flight.
docker exec kirocrew find /home/kirocrew/.kiro/crew/models \
  -maxdepth 1 -name '.*.tmp' -mmin +60 -delete
```

**Do not drop the `-mmin +60`.** A running download holds its `.tmp` staging
path open, and deleting it does not stop the transfer — it makes the install
step fail once the bytes have all arrived, so a download that was nearly done
is discarded and starts over from zero. Only a `.tmp` file that has not been
written to for an hour is genuinely stranded.

Do not clear the directory itself either — the models re-download on next use,
and on a metered or slow link that is a multi-GB round trip for nothing.

### Reduce memory pressure

- Avoid mounting very large repositories as context — the agent indexes
  them into memory.
- Close chat sessions you are not using; each one that is running is a
  live agent process, and that count is what peak memory tracks.

Embeddings cannot be traded away here: they are always-on and there is no
config knob to disable them (`embedding_provider` is coerced to `llama_cpp`
whatever you set, and `POST /api/memory/disable-embeddings` is a deliberate
HTTP 410 stub). On a memory-constrained host they degrade gracefully to
keyword/FTS search while the model is absent, so a memory cap is the lever,
not a provider swap.

---

## Quick diagnostics checklist

```bash
# 1. Container running?
docker ps -f name=kirocrew

# 2. Logs (last 30 lines)
docker logs --tail 30 kirocrew

# 3. Health status
docker inspect --format='{{.State.Health.Status}}' kirocrew

# 4. Port mapping
docker port kirocrew

# 5. Volume mounted?
docker inspect --format='{{range .Mounts}}{{.Destination}} → {{.Source}}{{"\n"}}{{end}}' kirocrew

# 6. Sandbox posture
docker logs kirocrew | grep '\[entrypoint\]'

# 7. Memory usage
docker stats kirocrew --no-stream
```

---

## Still stuck?

- Check the full startup log: `docker logs kirocrew`
- Review [docker.md](docker.md) for the complete configuration reference.
- Open an [issue](https://github.com/kirodotdev/KiroCrew/issues) with your
  Docker version (`docker version`), OS, and the relevant log output.

**Redact before you post.** A container log is not guaranteed to be free of
secrets: anything you or an app wrote to it, a tunnel URL, and any dashboard
link you pasted into a command can end up in the same output you are about to
attach to a public issue. Strip `?token=` values, bot tokens, and API keys, and
paste only the lines around the failure rather than the whole log.
