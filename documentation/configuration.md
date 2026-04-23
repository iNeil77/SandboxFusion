# Configuration

SandboxFusion is configured via YAML files. The active configuration is selected by the `SANDBOX_CONFIG` environment variable.

## Configuration File Selection

The configuration file path is:

```
sandbox/configs/{SANDBOX_CONFIG}.yaml
```

If `SANDBOX_CONFIG` is not set, it defaults to `"local"`, so the file `sandbox/configs/local.yaml` is loaded.

Examples:

```bash
# Use the default local development config
make run

# Explicitly select a config
SANDBOX_CONFIG=local make run

# Use the CI config
SANDBOX_CONFIG=ci make run-online
```

## Configuration Schema

The YAML file maps to the `RunConfig` pydantic model defined in `sandbox/configs/run_config.py`. It has three top-level sections:

```yaml
sandbox:
  isolation: lite           # "lite" or "full"
  max_concurrency: 34       # Max simultaneous sandbox instances (0 = unlimited)
  docker_image: ineil77/sandbox-fusion-base:23042026  # Docker image for "full" isolation mode

eval:
  max_runner_concurrency: 3  # Max parallel test case evaluations (0 = unlimited)

common:
  logging_color: true        # Enable ANSI color codes in structlog output
```

### sandbox

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `isolation` | `"lite"` or `"full"` | -- (required) | Isolation mode for code execution. See [Isolation Modes](isolation-modes.md). |
| `max_concurrency` | `int` | -- (required) | Maximum number of sandbox instances that may run in parallel. Set to `0` to disable the internal concurrency limiter (useful when concurrency is managed externally, e.g. by pytest-xdist). |
| `docker_image` | `string` | `"ineil77/sandbox-fusion-base:23042026"` | Docker image used when `isolation` is `"full"`. Must have all language runtimes installed. |

### eval

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_runner_concurrency` | `int` | `0` | Maximum number of test cases evaluated concurrently when processing a `/submit` request. `0` means no limit. A value like `3` keeps resource usage moderate during development. |

### common

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `logging_color` | `bool` | -- (required) | When `true`, structlog output includes ANSI colour codes for terminal readability. |

## Provided Configuration Files

### local.yaml (default for development)

```yaml
sandbox:
  isolation: lite
  max_concurrency: 34
  docker_image: ineil77/sandbox-fusion-base:23042026

eval:
  max_runner_concurrency: 3

common:
  logging_color: true
```

- Uses lite isolation (overlayfs + cgroups + network namespaces + chroot).
- Limits to 34 concurrent sandbox instances (reasonable for modern multi-core machines).
- Limits parallel test-case evaluation to 3 to keep resource usage moderate.

### ci.yaml (for CI/CD environments)

```yaml
sandbox:
  isolation: lite
  max_concurrency: 0
  docker_image: ineil77/sandbox-fusion-base:23042026

eval:
  max_runner_concurrency: 3

common:
  logging_color: true
```

- Uses lite isolation.
- `max_concurrency: 0` disables the internal limiter because CI parallelism is managed by pytest-xdist (each worker runs tests independently).

## Creating a Custom Configuration

To create a custom configuration:

1. Create a new YAML file in `sandbox/configs/`, e.g. `sandbox/configs/production.yaml`.
2. Define all three sections (`sandbox`, `eval`, `common`).
3. Set `SANDBOX_CONFIG=production` when starting the server.

Example for a production deployment using Docker-based full isolation:

```yaml
sandbox:
  isolation: full
  max_concurrency: 100
  docker_image: ineil77/sandbox-fusion-base:23042026

eval:
  max_runner_concurrency: 10

common:
  logging_color: false
```

## Server Startup

The server is started via uvicorn. The `HOST` and `PORT` can be overridden:

```bash
# Default: 0.0.0.0:8080
make run

# Custom host/port
HOST=127.0.0.1 PORT=9090 make run

# Production mode (no hot-reload)
make run-online

# With a specific config
SANDBOX_CONFIG=production make run-online
```

Under the hood, `make run` executes:

```bash
uvicorn sandbox.server.server:app --reload --host 0.0.0.0 --port 8080
```

And `make run-online` executes:

```bash
uvicorn sandbox.server.server:app --host 0.0.0.0 --port 8080
```
