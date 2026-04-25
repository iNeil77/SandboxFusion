# Copyright 2024 Bytedance Ltd. and/or its affiliates
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Pytest configuration hooks for the SandboxFusion test suite.

Tests always run against a real server inside a Docker container,
mirroring production.  The ``--sandbox-docker`` option (required)
selects the isolation backend:

* ``full`` — Docker-in-Docker via the host Docker socket.
* ``lite`` — privileged container with overlayfs + chroot + cgroups.
"""

import os
import shutil
import subprocess
import tempfile
import time

import pytest

_container_name: str | None = None
_workdir: str | None = None


def pytest_addoption(parser):
    parser.addoption(
        '--sandbox-docker',
        action='store',
        default=None,
        metavar='MODE',
        help='Start the sandbox server in a Docker container before tests. '
             'MODE is the isolation backend: "lite" or "full". Required.',
    )


def _is_xdist_worker(config) -> bool:
    return hasattr(config, 'workerinput')


def pytest_configure(config):
    docker_mode = config.getoption('--sandbox-docker', default=None)
    if docker_mode is None and os.environ.get('SANDBOX_TEST_DOCKER'):
        docker_mode = os.environ['SANDBOX_TEST_DOCKER']

    if docker_mode and not _is_xdist_worker(config):
        _start_docker_server(docker_mode)
    elif not _is_xdist_worker(config) and not os.environ.get('SANDBOX_TEST_SERVER_URL'):
        raise pytest.UsageError(
            'Tests require --sandbox-docker full or --sandbox-docker lite.')


def pytest_unconfigure(config):
    if not _is_xdist_worker(config):
        _stop_docker_server()


def _start_docker_server(mode: str):
    global _container_name, _workdir
    import secrets
    _container_name = f'sandbox_test_{secrets.token_hex(4)}'

    image = 'ineil77/sandbox-fusion-server:25042026'
    port = int(os.environ.get('SANDBOX_TEST_PORT', '18080'))

    cmd = [
        'docker', 'run', '-d',
        '--name', _container_name,
        '-p', f'{port}:8080',
        '--memory', '16g',
        '--cpus', '8',
        '--pids-limit', '4096',
    ]

    if mode == 'full':
        _workdir = tempfile.mkdtemp(prefix='sandbox_work_')
        os.chmod(_workdir, 0o777)
        cmd += [
            '-v', '/var/run/docker.sock:/var/run/docker.sock',
            '-v', f'{_workdir}:{_workdir}',
            '-e', f'SANDBOX_TMP_DIR={_workdir}',
            '-e', 'SANDBOX_CONFIG=docker_full',
        ]
    elif mode == 'lite':
        cmd += [
            '--privileged',
            '-e', 'SANDBOX_CONFIG=docker_lite',
        ]
    else:
        raise ValueError(f'--sandbox-docker must be "lite" or "full", got {mode!r}')

    cmd.append(image)

    print(f'\n--- Starting sandbox server container ({mode} mode): {_container_name} ---')
    subprocess.run(cmd, check=True, timeout=30)

    url = f'http://localhost:{port}'
    os.environ['SANDBOX_TEST_SERVER_URL'] = url
    os.environ['SANDBOX_ISOLATION_MODE'] = mode

    from sandbox.tests import client as client_mod
    import httpx
    client_mod.client = httpx.Client(base_url=url, timeout=120)

    _wait_for_server(url, timeout=120)
    print(f'--- Server ready at {url} ---\n')


def _wait_for_server(url: str, timeout: float):
    import httpx
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f'{url}/v1/ping', timeout=5)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(2)
    raise TimeoutError(f'Sandbox server at {url} did not become healthy within {timeout}s')


def _stop_docker_server():
    global _container_name, _workdir
    if _container_name is None:
        return
    name = _container_name
    _container_name = None
    print(f'\n--- Stopping sandbox server container: {name} ---')
    try:
        subprocess.run(
            ['docker', 'logs', '--tail', '50', name],
            timeout=10,
        )
    except Exception:
        pass
    try:
        subprocess.run(
            ['docker', 'rm', '-f', name],
            timeout=30,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass
    if _workdir and os.path.isdir(_workdir):
        shutil.rmtree(_workdir, ignore_errors=True)
        _workdir = None
