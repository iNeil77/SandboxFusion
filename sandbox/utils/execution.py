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

"""Process management and concurrency utilities for sandbox execution.

Provides helpers for safe byte decoding, non-blocking async stream reading,
process tree termination, temporary directory management, cgroup memory node
discovery, async concurrency limiting, and child process identification.
"""

import asyncio
import os
from functools import cache, wraps
from typing import Any, Callable, Coroutine, TypeVar

import psutil
import structlog

logger = structlog.stdlib.get_logger()


def try_decode(s: bytes) -> str:
    """Safely decode bytes to a string, returning an error message on failure.

    Args:
        s: The byte sequence to decode.

    Returns:
        The decoded string, or a ``[DecodeError] ...`` message string if
        decoding fails.
    """
    try:
        r = s.decode()
    except Exception as e:
        r = f'[DecodeError] {e}'
    return r


async def get_output_non_blocking(fd):
    """Read available data from an async stream without blocking.

    Attempts to read up to 1 MB from the given async file descriptor with a
    near-zero timeout (0.1 ms). If no data is available within the timeout,
    returns an empty string.

    Args:
        fd: An async stream object supporting ``read(n)`` (e.g., an
            ``asyncio.StreamReader``).

    Returns:
        The decoded string content read from the stream, or an empty string
        if nothing was available.
    """
    res = b''
    try:
        # read up to 1MB
        res = await asyncio.wait_for(fd.read(1024 * 1024), timeout=0.0001)
    except asyncio.TimeoutError:
        pass
    return try_decode(res)


def kill_process_tree(pid):
    """Kill a process and all of its descendant processes.

    Uses ``psutil`` to recursively discover all child processes of the given
    PID, kills them first, then kills the parent. Logs a warning if any
    error occurs (e.g., if the process has already exited).

    Args:
        pid: The process ID of the root process to kill.
    """
    try:
        parent = psutil.Process(pid)
        children = parent.children(recursive=True)
        for child in children:
            child.kill()
        parent.kill()
    except Exception as e:
        logger.warn(f'error on killing process tree: {e}')


@cache
def get_tmp_dir() -> str:
    """Return the path to the temporary directory, creating it if needed.

    The result is cached so the directory creation and log message only
    occur on the first call.

    Returns:
        The path string ``'/tmp'``.
    """
    TMP_DIR = '/tmp'
    os.makedirs(TMP_DIR, exist_ok=True)
    logger.info(f'tmp dir: {TMP_DIR}')
    return TMP_DIR


@cache
def get_memory_nodes() -> str:
    """Read the available memory nodes from the cgroup cpuset filesystem.

    Reads ``/sys/fs/cgroup/cpuset/cpuset.mems`` and returns its contents.
    The result is cached after the first call.

    Returns:
        A string representing the available memory node set (e.g., ``'0'``
        or ``'0-3'``).
    """
    return open('/sys/fs/cgroup/cpuset/cpuset.mems').read().strip()


T = TypeVar('T', bound=Callable[..., Coroutine[Any, Any, Any]])


def max_concurrency(limit: int) -> Callable[[T], T]:
    """Decorator that limits the maximum number of concurrent executions of an async function.

    Creates an ``asyncio.Semaphore`` with the given limit and wraps the
    decorated async function so that at most ``limit`` invocations can run
    concurrently. Additional callers will await until a semaphore slot is
    available.

    Args:
        limit: The maximum number of concurrent executions allowed.

    Returns:
        A decorator that wraps an async function with concurrency limiting.
    """
    semaphore = asyncio.Semaphore(limit)

    def decorator(func: T) -> T:

        @wraps(func)
        async def wrapper(*args, **kwargs):
            async with semaphore:
                return await func(*args, **kwargs)

        return wrapper

    return decorator


def find_child_with_least_pid(ppid) -> int | None:
    """Find the child process with the smallest PID for a given parent PID.

    Iterates over all running processes to find children of ``ppid``, then
    returns the PID of the child with the lowest PID value. This is useful
    for identifying the primary child process spawned by a parent.

    Args:
        ppid: The parent process ID to search children for.

    Returns:
        The PID of the child process with the smallest PID, or ``None`` if
        no children are found or an error occurs.
    """
    try:
        processes = []
        for p in psutil.process_iter(['pid', 'ppid']):
            if p.ppid() == ppid:
                processes.append(p)
        if not processes:
            logger.warning(f'no child process found')
            return None

        child_with_least_pid = min(processes, key=lambda p: p.pid)
        return child_with_least_pid.pid
    except psutil.NoSuchProcess:
        logger.warning(f'no process with PPID {ppid} found.')
        return None
    except Exception as e:
        logger.warning(f'failed to find_child_with_least_pid: {e}')
        return None
