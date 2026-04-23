# Isolation Modes

SandboxFusion provides two isolation modes to control how user-submitted code is executed. The mode is selected via the `sandbox.isolation` field in the YAML configuration file.

## Overview

| Mode | Mechanism | Overhead | Platform | Privileges |
|------|-----------|----------|----------|------------|
| `lite` | overlayfs + cgroups v1 + network namespaces + PID namespace + chroot | ~100ms | Linux only | Requires root (sudo) or privileged container |
| `full` | Docker containers with resource limits | ~500ms+ | Any platform with Docker | Requires Docker daemon access |

Both modes execute untrusted code in an isolated environment. Neither mode uses nested Docker.

---

## Lite Isolation

Lite isolation is the default mode. It provides fast, lightweight isolation using Linux kernel primitives directly (no container runtime involved).

### What It Does

When a code execution request arrives, the sandbox:

1. **Creates a temporary directory** under `/tmp` for the code and any uploaded files.
2. **Sets up an overlayfs mount**: The host filesystem is mounted read-only as the lower layer, with a disposable upper layer for writes. The executed code sees a copy-on-write view of the filesystem -- any modifications (including to system files) are discarded when execution completes.
3. **Creates a cgroup** (v1): Applies memory and CPU limits to the sandbox process. The memory limit defaults to the `memory_limit_MB` value from the request (or the system default if `-1`).
4. **Enters a network namespace**: The sandbox process is placed in a separate network namespace with no external connectivity, preventing the executed code from making network requests.
5. **Enters a PID namespace**: Uses `unshare` to give the sandbox process its own PID namespace, preventing it from seeing or signaling host processes.
6. **Chroots** into the overlayfs mount: The sandbox process sees the overlayfs as its root filesystem, isolating it from the real filesystem.
7. **Executes the code** within this isolated environment with the configured timeouts.
8. **Tears down** the overlayfs, cgroup, and namespace after execution completes (or on timeout).

### Requirements

- **Linux**: overlayfs, cgroups v1, and namespaces are Linux kernel features.
- **Root / privileged**: Setting up overlayfs and cgroups requires root privileges. When running inside Docker, the container must be started with `--privileged`.
- **Network namespace scripts**: The scripts `scripts/create_net_namespace.sh` and `scripts/clean_net_namespace.sh` manage the network namespace lifecycle.

### Configuration

```yaml
sandbox:
  isolation: lite
  max_concurrency: 34
```

### Implementation

The isolation primitives are implemented in `sandbox/runners/isolation.py`:

- `tmp_overlayfs(lower_dirs)` -- Context manager that creates an overlayfs mount with the given lower directories (read-only) and a temporary upper directory (read-write).
- `tmp_cgroup(memory_limit_mb)` -- Context manager that creates a cgroup with the specified memory limit and cleans it up on exit.
- `tmp_netns()` -- Context manager that creates and destroys a network namespace.

These are composed together in `sandbox/runners/base.py:run_commands()` when the isolation mode is `lite`.

---

## Full Isolation (Docker)

Full isolation runs each code execution inside a disposable Docker container. This provides the strongest isolation and works on any platform with Docker.

### What It Does

When a code execution request arrives, the sandbox:

1. **Launches a Docker container** from the configured `docker_image` (default: `sandbox:base`) with these flags:
   - `--rm` -- Container is automatically removed after exit.
   - `--memory` -- Memory limit from the request (or system default).
   - `--cpus 1` -- Limits CPU to 1 core.
   - `--network none` -- No network connectivity.
   - `--pids-limit 256` -- Limits the number of processes to prevent fork bombs.
2. **Mounts the temporary directory** containing the code and uploaded files into the container.
3. **Executes the compile and run commands** inside the container.
4. **Collects results** (stdout, stderr, exit code, fetched files) from the container.
5. **The container is destroyed** automatically on exit (`--rm`).

### Requirements

- **Docker daemon**: Must be installed and running on the host.
- **`sandbox:base` image**: The Docker image must have all language runtimes pre-installed. Build it with `make build-base-image`.
- **No nested Docker**: Full isolation does NOT use Docker-in-Docker. The host Docker daemon creates sibling containers.

### Configuration

```yaml
sandbox:
  isolation: full
  max_concurrency: 100
  docker_image: sandbox:base
```

The `docker_image` field specifies which image to use for sandbox containers. It defaults to `sandbox:base`, which is built by `make build-base-image` and includes all 20+ language runtimes.

---

## Choosing a Mode

| Consideration | Lite | Full |
|---------------|------|------|
| **Performance** | Fast (~100ms overhead) | Slower (~500ms+ overhead per execution) |
| **Isolation strength** | Strong (kernel-level) but shares kernel with host | Strongest (full container boundary) |
| **Platform** | Linux only | Any OS with Docker |
| **Development** | Good for local Linux dev, CI | Good for any platform |
| **Production** | Recommended when running inside Docker already | Recommended for bare-metal hosts |

### Typical deployment patterns

**Docker deployment (recommended):**
- Build `sandbox:server` which runs the SandboxFusion server inside a Docker container.
- The server uses **lite** isolation inside the container (overlayfs + cgroups within the privileged container).
- The container needs `--privileged` for overlayfs/cgroups.
- No nested Docker involved.

**Bare-metal Linux deployment:**
- Install runtimes directly on the host (`make install-runtimes`).
- Use **lite** isolation for best performance.
- Requires root/sudo for overlayfs and cgroups.

**Full isolation deployment:**
- The SandboxFusion server runs on the host (or in a container with Docker socket access).
- Each code execution spawns a separate `sandbox:base` container via the host Docker daemon.
- Use when you want maximum isolation and can tolerate higher latency.

---

## Execution Flow

Regardless of isolation mode, the general code execution flow is:

1. Create a temporary directory under `/tmp`.
2. Write files passed through the `files` parameter.
3. Write the `code` parameter to a temporary file (e.g. `/tmp/tmpha4dcl5b/tmpx8k1pnfh.py`).
4. Set up the isolation environment (overlayfs + cgroup + namespace for lite, or Docker container for full).
5. Execute compilation commands (if the language requires compilation).
6. Execute the run command.
7. Retrieve files specified by `fetch_files`.
8. Tear down the isolation environment and clean up the temporary directory.

File paths in `files` and `fetch_files` support both absolute and relative paths. Relative paths are resolved relative to the temporary directory.
