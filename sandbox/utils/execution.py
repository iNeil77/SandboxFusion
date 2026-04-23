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

import asyncio
import os
from functools import cache, wraps
from typing import Any, Callable, Coroutine, TypeVar

import psutil
import structlog

logger = structlog.stdlib.get_logger()


def try_decode(s: bytes) -> str:
    try:
        r = s.decode()
    except Exception as e:
        r = f'[DecodeError] {e}'
    return r


async def get_output_non_blocking(fd):
    res = b''
    try:
        # read up to 1MB
        res = await asyncio.wait_for(fd.read(1024 * 1024), timeout=0.0001)
    except asyncio.TimeoutError:
        pass
    return try_decode(res)


def kill_process_tree(pid):
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
    TMP_DIR = '/tmp'
    os.makedirs(TMP_DIR, exist_ok=True)
    logger.info(f'tmp dir: {TMP_DIR}')
    return TMP_DIR


@cache
def get_memory_nodes() -> str:
    return open('/sys/fs/cgroup/cpuset/cpuset.mems').read().strip()


T = TypeVar('T', bound=Callable[..., Coroutine[Any, Any, Any]])


def max_concurrency(limit: int) -> Callable[[T], T]:
    """ Decorator to limit the maximum number of concurrent executions of an async function """
    semaphore = asyncio.Semaphore(limit)

    def decorator(func: T) -> T:

        @wraps(func)
        async def wrapper(*args, **kwargs):
            async with semaphore:
                return await func(*args, **kwargs)

        return wrapper

    return decorator


def find_child_with_least_pid(ppid) -> int | None:
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
