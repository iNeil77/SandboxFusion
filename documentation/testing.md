# Testing Guide

SandboxFusion ships with 217 tests across 35 test files. Every test runs against a real server inside a Docker container, mirroring production exactly. There is no in-process or mock mode.

## Quick Start

```bash
# One-time: build the Docker images
make build-base-image       # ~20 min first time (installs 20+ language runtimes)
make build-server-image     # ~1 min (layers the app on top)

# Run all tests in full (Docker-in-Docker) isolation mode
make test

# Run all tests in lite (overlayfs + cgroups) isolation mode
make test-docker-lite
```

## How It Works

The test harness is driven by a root-level `conftest.py` that hooks into pytest's lifecycle:

1. **`pytest_configure`** -- starts a Docker container running the SandboxFusion server.
2. **Tests execute** -- each test sends HTTP requests to the container via `httpx`.
3. **`pytest_unconfigure`** -- stops and removes the container, cleans up temp directories.

The `--sandbox-docker` flag selects the isolation mode:

```
pytest --sandbox-docker full    # Docker-in-Docker (sibling containers)
pytest --sandbox-docker lite    # overlayfs + cgroups inside a privileged container
```

This flag is required. Running `pytest` without it raises a `UsageError`.

### Container Provisioning

| Mode | Memory | CPUs | Flags |
|------|--------|------|-------|
| **full** | 16 GB | 8 | `-v /var/run/docker.sock`, shared workdir |
| **lite** | 256 GB | 128 | `--privileged` |

Lite mode gets more resources because all sandbox processes run *inside* the single container (overlayfs + cgroup isolation), whereas full mode spawns separate sibling containers that draw from the host's resources directly.

### pytest-xdist Compatibility

Tests run in parallel via `pytest-xdist`. The conftest detects xdist workers (`hasattr(config, 'workerinput')`) and ensures only the controller process starts/stops the Docker container. Workers inherit `SANDBOX_TEST_SERVER_URL` from the environment.

The default parallelism is 16 workers (`TEST_NP=16` in the Makefile). Override it:

```bash
make test-docker-full TEST_NP=4    # fewer workers (useful for debugging)
make test-docker-lite TEST_NP=32   # more workers (if the host can handle it)
```

## Makefile Targets

| Target | Description |
|--------|-------------|
| `make test` | Alias for `make test-docker-full` |
| `make test-docker-full` | Full suite in Docker-in-Docker mode |
| `make test-docker-lite` | Full suite in lite isolation mode |
| `make test-case CASE=test_python MODE=full` | Single test with stdout visible |
| `make test-case CASE=test_java MODE=lite` | Single test in lite mode |

The `test-case` target uses `pytest -s -vv -k $(CASE)`, so `CASE` is a pytest `-k` expression -- it can be a test name, substring, or boolean expression:

```bash
make test-case CASE=test_python_print MODE=full           # exact match
make test-case CASE="test_python and not timeout" MODE=lite  # expression
make test-case CASE=test_cpp MODE=full                    # all C++ tests
```

## Test Architecture

### Directory Structure

```
SandboxFusion/
├── conftest.py                         # Pytest hooks: container lifecycle
├── sandbox/tests/
│   ├── __init__.py
│   ├── client.py                       # HTTP client (reads SANDBOX_TEST_SERVER_URL)
│   ├── datasets/                       # Evaluation / submission tests
│   │   ├── test_extraction.py          #   Code extraction from LLM completions
│   │   ├── test_submission_rigor.py    #   End-to-end /submit endpoint tests
│   │   └── test_utils.py              #   Utility function tests
│   └── runners/                        # Code execution tests
│       ├── test_python.py              #   Python happy-path
│       ├── test_cpp.py                 #   C++ happy-path
│       ├── test_<language>.py          #   ... one file per language (22 files)
│       ├── test_execution_rigor.py     #   Cross-language edge cases (45 tests)
│       ├── test_output_consistency.py  #   Determinism / reproducibility (16 tests)
│       ├── test_isolation.py           #   Network namespace isolation (7 tests)
│       └── test_load.py               #   Concurrent load tests (9 tests)
```

### Test Categories

**Per-language happy-path tests** (`test_python.py`, `test_cpp.py`, etc.)
Each language has its own test file covering basic scenarios: print output, assertion errors, syntax errors, stdin, file I/O, timeouts, and compile errors (for compiled languages). These are the first tests to check when a language runner breaks.

**Execution rigor tests** (`test_execution_rigor.py`)
45 tests covering cross-cutting edge cases that apply to multiple languages: explicit exit codes, stderr vs stdout separation, large I/O and buffering, Unicode handling, empty/whitespace-only code, execution timing guarantees, multi-line stdin, file write + fetch round-trips, compile-succeed-but-run-fail scenarios, and concurrent execution correctness.

**Output consistency tests** (`test_output_consistency.py`)
16 tests that run the same code multiple times and verify identical output. These catch nondeterminism bugs in the sandbox (e.g., race conditions in output capture, timing-dependent behavior, file system state leaks between executions).

**Isolation tests** (`test_isolation.py`)
7 tests verifying network namespace separation: HTTP servers on loopback, two servers on the same port in different namespaces (proving isolation), and outbound network access (lite mode only -- full mode uses `--network none`).

**Load tests** (`test_load.py`)
9 tests that fire many concurrent requests to stress the server's concurrency control, semaphore queuing, and resource cleanup under load.

**Submission rigor tests** (`test_submission_rigor.py`)
28 end-to-end tests for the `/submit` evaluation endpoint: correct answers, wrong answers, partial failures, compile errors, runtime errors, multiple test cases, and edge cases in code extraction.

### The Test Client

All tests use a shared HTTP client defined in `sandbox/tests/client.py`:

```python
from sandbox.tests.client import client

def test_example():
    request = RunCodeRequest(language='python', code='print(1)', run_timeout=5)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Success
```

The client is an `httpx.Client` instance pointed at `SANDBOX_TEST_SERVER_URL` (set by conftest). It has a 120-second timeout to accommodate slow compilation (Scala, Lean) and load tests.

### Environment Variables

| Variable | Set by | Purpose |
|----------|--------|---------|
| `SANDBOX_TEST_SERVER_URL` | conftest | Base URL for the test client (e.g., `http://localhost:18080`) |
| `SANDBOX_ISOLATION_MODE` | conftest | `full` or `lite` -- used in `pytest.mark.skipif` conditions |
| `SANDBOX_TEST_PORT` | user (optional) | Override the default test port (18080) |
| `SANDBOX_TEST_DOCKER` | user (optional) | Alternative to `--sandbox-docker` flag |

### Skip Conditions

Some tests only apply to one isolation mode. Use the `SANDBOX_ISOLATION_MODE` environment variable:

```python
import os
import pytest

@pytest.mark.skipif(
    os.environ.get('SANDBOX_ISOLATION_MODE') == 'full',
    reason='Full mode uses --network none which blocks all egress traffic')
def test_external_network():
    ...
```

Current skip conditions:
- `test_python_fetch_files_absolute_path` -- skipped in full mode (only bind-mounts cwd, not arbitrary paths)
- `test_isolation_network_external_access` -- skipped in full mode (`--network none` blocks egress)

## Writing New Tests

### Adding a Test for an Existing Language

Add your test function to the appropriate `test_<language>.py` file:

```python
from sandbox.server.sandbox_api import RunCodeRequest, RunCodeResponse, RunStatus
from sandbox.tests.client import client

def test_python_list_comprehension():
    """List comprehension should produce correct output."""
    request = RunCodeRequest(
        language='python',
        code='print([x**2 for x in range(5)])',
        run_timeout=5,
    )
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Success
    assert result.run_result.stdout.strip() == '[0, 1, 4, 9, 16]'
```

### Adding a Test for a New Language

1. Create `sandbox/tests/runners/test_<language>.py`.
2. Follow the pattern from an existing file (e.g., `test_ruby.py` for interpreted languages, `test_rust.py` for compiled languages).
3. Cover at minimum: print output, syntax/compile error, assertion/runtime error, and timeout.

### Testing Compiled Languages

Compiled languages have both a compile step and a run step. Check both:

```python
from sandbox.runners import CommandRunStatus
from sandbox.server.sandbox_api import RunCodeRequest, RunCodeResponse, RunStatus
from sandbox.tests.client import client

def test_cpp_compile_error():
    """Invalid C++ should fail at compile time with an error in stderr."""
    request = RunCodeRequest(language='cpp', code='not valid c++', run_timeout=5)
    response = client.post('/run_code', json=request.model_dump())
    assert response.status_code == 200
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Failed
    assert result.compile_result.return_code != 0
    assert 'error' in result.compile_result.stderr.lower()
    assert result.run_result is None  # run step was skipped
```

### Testing File I/O

Provide input files as base64-encoded strings and retrieve output files via `fetch_files`:

```python
import base64

def test_file_round_trip():
    """Write a file during execution and fetch it back."""
    request = RunCodeRequest(
        language='python',
        code='open("output.txt", "w").write("hello")',
        run_timeout=5,
        fetch_files=['output.txt'],
    )
    response = client.post('/run_code', json=request.model_dump())
    result = RunCodeResponse(**response.json())
    assert result.status == RunStatus.Success
    content = base64.b64decode(result.files['output.txt']).decode()
    assert content == 'hello'
```

### Testing the /submit Endpoint

```python
from sandbox.server.sandbox_api import RunCodeRequest

def test_submit_addition():
    """Submit a completion that solves a+b and verify it passes."""
    response = client.post('/submit', json={
        'id': 'test-add',
        'completion': '```python\na, b = map(int, input().split())\nprint(a + b)\n```',
        'config': {'language': 'python'},
        'test_cases': [
            {'input': {'stdin': '1 2\n'}, 'output': {'stdout': '3\n'}},
            {'input': {'stdin': '10 20\n'}, 'output': {'stdout': '30\n'}},
        ],
    })
    assert response.status_code == 200
    result = response.json()
    assert result['accepted'] is True
```

### Async Tests

Tests that need `asyncio` (e.g., for `asyncio.gather`) are automatically supported -- `asyncio_mode = "auto"` is set in `pyproject.toml`:

```python
import asyncio

async def test_concurrent_execution():
    """Two concurrent requests should both succeed."""
    request = RunCodeRequest(language='python', code='print("ok")', run_timeout=5)

    def post():
        return client.post('/run_code', json=request.model_dump())

    results = await asyncio.gather(
        asyncio.to_thread(post),
        asyncio.to_thread(post),
    )
    for response in results:
        assert response.status_code == 200
        result = RunCodeResponse(**response.json())
        assert result.status == RunStatus.Success
```

## Debugging Failures

### Viewing Server Logs

When a test fails, the conftest prints the last 50 lines of the server container's logs during teardown. For more detail, run the failing test in isolation with stdout visible:

```bash
make test-case CASE=test_python_timeout MODE=lite
```

### Connecting to a Running Test Container

If you need to inspect the server while tests are running, find the container:

```bash
docker ps --filter "name=sandbox_test"
```

Then exec into it:

```bash
docker exec -it sandbox_test_<hex> bash
```

### Common Failure Patterns

**`Cannot open network namespace "sbox_...": No such file or directory`**
The network namespace was created but disappeared before the sandboxed process could enter it. This typically indicates a race condition in namespace setup. If it only happens under high concurrency, reduce `TEST_NP`.

**`AssertionError: assert <RunStatus.Failed> == <RunStatus.Success>`**
The code execution failed. Check `result.run_result.stderr` (or `result.compile_result.stderr` for compiled languages) for the actual error message. Common causes: missing runtime in the Docker image, timeout too short, or an isolation setup failure.

**`TimeLimitExceeded` when it shouldn't be**
The default `run_timeout` in tests is typically 5-10 seconds. Under heavy load, the server's concurrency semaphore may queue the request, and the timeout starts *after* the semaphore is acquired (it measures actual execution time, not queue time). If tests only fail under high parallelism, this is likely the cause.

**Tests pass individually but fail in parallel**
Resource contention under concurrent execution. Check: (1) the container has enough CPUs and memory, (2) `max_concurrency` in the config is high enough, (3) the subnet pool isn't exhausted (4096 subnets available).

### Running Against an External Server

If you already have a running SandboxFusion server, you can skip container management and point tests directly at it:

```bash
SANDBOX_TEST_SERVER_URL=http://your-server:8080 \
SANDBOX_ISOLATION_MODE=lite \
pytest -m "not datalake" -n 16
```

This bypasses the conftest's Docker container lifecycle entirely.

## Test Marks

| Mark | Purpose |
|------|---------|
| `datalake` | Tests requiring external datalake access (excluded by default) |
| `minor` | Tests for minor/less-common languages |
| `lean` | Lean-specific tests (slow due to `lake build`) |
| `verilog` | Verilog-specific tests |

Filter by mark:

```bash
pytest -m "minor" --sandbox-docker lite        # only minor language tests
pytest -m "not datalake" --sandbox-docker full  # everything except datalake (default)
```

## CI Integration

To run the test suite in a CI pipeline, ensure the runner has Docker available and sufficient resources:

```yaml
# Example GitHub Actions step
- name: Run tests
  run: |
    make build-server-image
    make test-docker-full TEST_NP=8
    make test-docker-lite TEST_NP=8
```

The test harness is self-contained: it builds nothing, downloads nothing, and cleans up after itself. The only prerequisite is a built `ineil77/sandbox-fusion-server:25042026-2` image available locally.
