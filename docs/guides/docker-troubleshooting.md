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

### Allocate a TTY

`kiro-cli login` is interactive and needs a pseudo-terminal. Always use
`-it`:

```bash
docker exec -it kirocrew kiro-cli login
```

If you omit `-it`, the login prompt has no TTY to read from and fails.

### Running from CI / scripts (non-interactive)

If no interactive terminal is available (CI pipeline, cron job), `kiro-cli
login` cannot work. Instead, pre-authenticate on a workstation and copy the
credential file into the volume:

```bash
# On your workstation (already logged in):
docker cp ~/.kiro/credentials kirocrew:/home/kirocrew/.kiro/credentials
docker exec -u 0 kirocrew chown kirocrew:kirocrew /home/kirocrew/.kiro/credentials
```

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

**Symptoms:** agent commands fail or are disabled; startup log shows "agent
exec DISABLED."

### Check the startup log

```bash
docker logs kirocrew | grep '\[entrypoint\]'
```

Look for:

```
[entrypoint] sandbox probe: no backend (EPERM) → agent exec DISABLED (set KIROCREW_ALLOW_UNSANDBOXED=1 to enable)
```

### Option A: Apply the custom seccomp profile (recommended)

The image needs `unshare(CLONE_NEWUSER)` and `unshare(CLONE_NEWNS)` for the
inner namespace sandbox. The Docker default seccomp profile blocks these.

```bash
docker run -d --name kirocrew \
  -p 127.0.0.1:5476:5476 \
  -v kirocrew-home:/home/kirocrew \
  --security-opt seccomp=docker/seccomp/kirocrew-seccomp.json \
  ghcr.io/kirodotdev/kirocrew:stable
```

Download the profile if you don't have the repo checked out:

```bash
curl -fsSL https://raw.githubusercontent.com/kirodotdev/KiroCrew/main/docker/seccomp/kirocrew-seccomp.json \
  -o kirocrew-seccomp.json
```

### Option B: Allow unsandboxed execution

If you cannot modify seccomp (managed Kubernetes, Docker Desktop
restrictions):

```bash
docker run -d --name kirocrew \
  -p 127.0.0.1:5476:5476 \
  -v kirocrew-home:/home/kirocrew \
  -e KIROCREW_ALLOW_UNSANDBOXED=1 \
  ghcr.io/kirodotdev/kirocrew:stable
```

> **Security note:** in unsandboxed mode the container is the only isolation
> boundary. Do not mount host paths you would not hand to the agent.

### Verify the active posture

```bash
docker exec kirocrew python3 -c \
  "from kiro_crew.sandbox import detect_backend; print(detect_backend())"
# "namespace" = sandbox active, "none" = unsandboxed
```

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

Common causes: corrupted `config.json` (delete it and let the gateway
regenerate defaults), or missing credentials for a configured channel bot
(remove the channel config or supply the token).

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

**Symptoms:** the agent responds conversationally but refuses to run
commands, or commands fail with "execution not available."

### Sandbox consent required

If the sandbox probe failed and `KIROCREW_ALLOW_UNSANDBOXED` is not set,
agent execution is disabled by design. See [section 4](#4-sandbox-related-errors).

### Missing tools in the container

The image is minimal — it does not include language runtimes, compilers, or
package managers beyond Python. If the agent needs `git`, `node`, `gcc`, etc.,
they are not available by default.

**Options:**

1. **Mount the tool from the host** (bind-mount a binary or use a shared
   tools volume).
2. **Build a custom image** extending the official one:

   ```dockerfile
   FROM ghcr.io/kirodotdev/kirocrew:stable
   USER root
   RUN apt-get update && apt-get install -y git nodejs npm && rm -rf /var/lib/apt/lists/*
   USER kirocrew
   ```

3. **Use the agent in "plan-only" mode** — let it generate code and
   instructions that you apply on the host.

### Agent skill not installed

Some commands require installed skills. Check available skills:

```bash
docker exec kirocrew kiro-cli skill list
```

Install missing ones:

```bash
docker exec -it kirocrew kiro-cli skill install <skill-name>
```

---

## 8. High memory usage

**Symptoms:** the container uses several GB of RAM; the host swaps or the
OOM killer terminates the container.

### Subagent limits

Each active chat session can spawn subagents. Limit concurrent sessions or
set a memory cap:

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

The first time embeddings or the fast-apply model run, they download into
the volume. If the download is interrupted and retried, temporary files may
accumulate. Clear the model cache if needed:

```bash
docker exec kirocrew rm -rf /home/kirocrew/.kiro/crew/models/.tmp
docker restart kirocrew
```

### Reduce memory pressure

- Limit the number of concurrent chat sessions in the dashboard settings.
- Avoid mounting very large repositories as context — the agent indexes
  them into memory.
- If running on a memory-constrained host (< 4 GB), consider disabling
  local embeddings and using an API-based provider instead.

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
