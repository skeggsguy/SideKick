# Code sandbox setup (`sandbox_run`)

`sandbox_run` runs model-written code inside an ephemeral Docker container. It is
the agent's workspace for executing scripts (the diagram/script use case), **not**
a project-dev environment — it has no network, ephemeral state, and no project
venv, so it can't run `uv run pytest` or install packages at run time. That's by
design (see the safety model below).

## Install (one-time, macOS)

The engine runs in OrbStack's Linux VM; the `docker` CLI and every flag in the
safety model below work unchanged (arm64 image, built natively).

```bash
brew install --cask orbstack
orbctl start                               # no GUI onboarding needed
orbctl config set memory_mib 2048          # VM cap
orbctl config set app.start_at_login true
orbctl stop && orbctl start                # apply the config
# the CLI ships inside the app bundle — link it onto PATH + the buildx plugin
X=/Applications/OrbStack.app/Contents/MacOS/xbin
ln -s $X/docker /opt/homebrew/bin/docker
mkdir -p ~/.docker/cli-plugins && ln -s $X/docker-buildx ~/.docker/cli-plugins/
scripts/build_sandbox_image.sh
```

No docker group / session restart on macOS — the socket
(`~/.orbstack/run/docker.sock`, context `orbstack`) is owned by you. `--user
uid:gid` is honored: workspace outputs land owned by you (verified 2026-09-28).
**agentd under launchd** does not read your shell profile — its plist PATH must
include `/opt/homebrew/bin` or `sandbox_run` reports docker as not installed.

## The safety model (locked — do not relax)

Each container runs:

```
docker run --rm --network none \
  --cap-drop ALL --security-opt no-new-privileges \
  --pids-limit 256 --memory 512m --memory-swap 512m --cpus 2 \
  --env HOME=/tmp --user <uid>:<gid> \
  -v <workspace>:<workspace> -w <workspace> <image> bash -c <command>
```

- **`--network none`** — no internet. Outward actions are gated by the policy
  engine, not made safe by the container; keep them separate.
- **dedicated workspace mount** — `<repo>/scratch/sandbox` (override
  `HARNESS_SANDBOX_MOUNT`) is the only **writable** mount, at the **same absolute
  path** inside the container as on the host, so a file the model writes there
  hands back to the host-side tools (drive_upload, read, edit) at the identical
  path — no `/work`-vs-host translation, which silently broke the handback. The
  agent's file-area roots (`<repo>/scratch`, incl. `downloads/`) are additionally
  mounted **read-only** so sandboxed code can process downloaded or user-dropped
  files without a copy step. The repo root is **not** mounted, so `.env`, `.git`,
  and `src/` are unreachable by model code (which may be prompt-injection-steered).
  Do not revert to mounting `cwd`.
- **`--user <uid>:<gid>`** — non-root, so files written to the workspace are owned
  by you (clean handback). Safe now that only the workspace is mounted.
- **`--env HOME=/tmp`** — the container has no passwd entry for the host uid, so
  HOME would default to `/` (unwritable); `/tmp` gives matplotlib/etc. a cache dir.
- **`--cap-drop ALL` / `no-new-privileges` / pids+memory+cpu limits** — cheap
  defense in depth against escape and host DoS (a fork bomb would otherwise reap
  agentd).

## The image

`harness-sandbox:latest` (built from `docker/sandbox/Dockerfile`) bakes the common
offline libs at **build** time (network is off at *run* time): numpy, pandas,
matplotlib, pyyaml, pillow, tabulate, openpyxl. No HTTP client is baked in
deliberately.

To add a library, edit `docker/sandbox/Dockerfile`, then rebuild:

```bash
scripts/build_sandbox_image.sh
```

If the image isn't built, `sandbox_run` falls back to bare `python:3.12-slim`
(stdlib only) and prepends a `[note]` telling you to build it. Set
`HARNESS_SANDBOX_IMAGE` to override the image entirely (trusted as-is).

## Troubleshooting

`sandbox_run` runs a pre-flight (`docker version`) and returns one of these:

| Message contains | Cause | Fix |
| --- | --- | --- |
| `permission denied … docker socket` | context not pointing at OrbStack, or its socket is stale | `docker context use orbstack`, then `orbctl stop && orbctl start` |
| `the Docker engine isn't running` | OrbStack stopped | `orbctl start` (check `orbctl status`) |
| `docker is not installed` | no `docker` on this process's PATH | the Install steps above; under launchd, add `/opt/homebrew/bin` to the plist PATH |
| `harness-sandbox image not built` | image missing | `scripts/build_sandbox_image.sh` |
| `ModuleNotFoundError … no network` | lib not pre-baked | add it to `docker/sandbox/Dockerfile`, rebuild |
