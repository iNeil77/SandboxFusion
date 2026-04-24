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
"""Linux isolation primitives for the *lite* sandbox mode.

This module provides async context managers and helper functions that set up
and tear down the OS-level isolation layers used when
``RunConfig.sandbox.isolation == 'lite'``:

* **Filesystem isolation** -- :func:`tmp_overlayfs` creates an overlayfs whose
  lower layer is the host root (``/``), with a tmpfs-backed upper layer so all
  writes are ephemeral.
* **Resource limits** -- :func:`tmp_cgroup` creates cgroup v1 or v2 controllers
  (auto-detected) for memory and/or CPU quota.
* **Network isolation** -- :func:`tmp_netns` creates a dedicated network
  namespace via helper shell scripts, drawing subnet addresses from a
  pre-computed pool that is partitioned per ``pytest-xdist`` worker to avoid
  conflicts during parallel testing.

All context managers perform full cleanup (unmount, cgroup deletion, namespace
removal) when exiting, even on error.
"""

import asyncio
import os
import shutil
import stat
import sys
import threading
import time
from contextlib import asynccontextmanager
from typing import List, Optional

import aiofiles
import aiofiles.os
import structlog

from sandbox.utils.common import random_cgroup_name

logger = structlog.stdlib.get_logger()


def _detect_cgroup_version() -> int:
    """Return 1 or 2 depending on which cgroup hierarchy is mounted at /sys/fs/cgroup."""
    try:
        result = os.statvfs('/sys/fs/cgroup')
        with open('/proc/filesystems') as f:
            fs_types = f.read()
        if os.path.isfile('/sys/fs/cgroup/cgroup.controllers'):
            return 2
    except OSError:
        pass
    return 1


CGROUP_VERSION = _detect_cgroup_version()

_cgroup_v2_initialized = False
_cgroup_v2_lock = threading.Lock()


def _init_cgroup_v2_delegation():
    """Enable memory+cpu controllers on the root cgroup for v2.

    Cgroup v2 requires that no processes live in the root cgroup when
    enabling subtree_control.  We move ALL processes into an ``init``
    child cgroup first, then enable the controllers.  This is idempotent
    and uses subprocess calls to ensure root privileges via sudo.

    Protected by a lock to prevent concurrent first-time callers from
    racing on the cgroup filesystem writes.
    """
    global _cgroup_v2_initialized
    if _cgroup_v2_initialized:
        return
    with _cgroup_v2_lock:
        if _cgroup_v2_initialized:
            return
        try:
            import subprocess
            init_cg = '/sys/fs/cgroup/sandbox_init'
            subprocess.run(['sudo', 'mkdir', '-p', init_cg], check=True)
            with open('/sys/fs/cgroup/cgroup.procs') as f:
                pids = f.read().splitlines()
            for pid in pids:
                if pid.strip():
                    subprocess.run(
                        ['sudo', 'bash', '-c', f'echo {pid.strip()} > {init_cg}/cgroup.procs'],
                        check=False)
            subprocess.run(
                ['sudo', 'bash', '-c', 'echo "+memory +cpu" > /sys/fs/cgroup/cgroup.subtree_control'],
                check=True)
            _cgroup_v2_initialized = True
        except Exception as e:
            logger.warning(f'Failed to initialize cgroup v2 delegation: {e}')


_EXECUTE_CMD_TIMEOUT = 30


async def execute_command(cmd: List[str], raise_nonzero: bool = True, timeout: float = _EXECUTE_CMD_TIMEOUT):
    """Run a command as an async subprocess and optionally raise on failure.

    Args:
        cmd: Argument list for the command to execute.
        raise_nonzero: If True (default), raise ``RuntimeError`` when the
            process exits with a non-zero return code.
        timeout: Maximum seconds to wait for the command to complete.
    """
    process = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        raise RuntimeError(f'Command timed out after {timeout}s: {" ".join(cmd)}')

    if process.returncode != 0 and raise_nonzero:
        raise RuntimeError(f'Failed to execute {" ".join(cmd)}: {stdout.decode()}\n{stderr.decode()}')


async def mount_tmpfs(mount_point: str):
    """Mount a tmpfs filesystem at *mount_point* (requires sudo)."""
    mount_cmd = ['sudo', 'mount', '-t', 'tmpfs', 'tmpfs', mount_point]
    await execute_command(mount_cmd)


async def unmount_fs(mount_point: str, recursive: bool = False):
    """Lazily unmount the filesystem at *mount_point* (requires sudo).

    Args:
        mount_point: Path to unmount.
        recursive: If True, pass ``-R`` to also detach all submounts
            (needed for ``--rbind`` mounts like ``/dev``).
    """
    flags = ['-Rl'] if recursive else ['-l']
    mount_cmd = ['sudo', 'umount'] + flags + [mount_point]
    await execute_command(mount_cmd)


async def _sweep_remaining_mounts(base_dir: str):
    """Force-detach any mounts still present under *base_dir*.

    Reads ``/proc/mounts`` to discover leftover mount points (deepest first)
    and issues ``umount -Rl`` for each.  Errors are logged but never raised,
    since this is a last-resort safety net.
    """
    try:
        mounts = await asyncio.to_thread(_read_mounts_under, base_dir)
        for mp in mounts:
            try:
                await execute_command(['sudo', 'umount', '-Rl', mp])
            except Exception:
                logger.warning('sweep unmount failed', mount_point=mp)
    except Exception:
        logger.warning('sweep mount enumeration failed', base_dir=base_dir)


def _read_mounts_under(base_dir: str) -> list:
    """Return mount points under *base_dir*, deepest first."""
    result = []
    try:
        with open('/proc/mounts') as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    mp = parts[1]
                    if mp == base_dir or mp.startswith(base_dir + '/'):
                        result.append(mp)
    except OSError:
        pass
    result.sort(key=len, reverse=True)
    return result


@asynccontextmanager
async def tmp_overlayfs():
    """Async context manager that creates a temporary overlayfs sandbox.

    The overlay uses the host root (``/``) as its read-only lower layer and a
    tmpfs-backed upper layer so that all modifications made by sandboxed
    processes are ephemeral.  Inside the merged directory ``/proc``, ``/sys``,
    and ``/dev`` are mounted, and ``/etc/hosts`` and ``/etc/resolv.conf`` are
    copied from the host for basic network resolution.

    Yields:
        The path to the merged overlay directory suitable for use as a
        ``chroot`` target.

    On exit, all mounts are torn down and the temporary directory is removed.
    """
    base_dir = f'/tmp/overlay_{random_cgroup_name()}'
    merged_dir = f'{base_dir}/merged'
    tmpfs_dir = f'{base_dir}/tmpfs'
    upper_dir = f'{tmpfs_dir}/upper'
    work_dir = f'{tmpfs_dir}/work'

    try:
        for sub_dir in [tmpfs_dir, merged_dir]:
            await aiofiles.os.makedirs(sub_dir)
        await mount_tmpfs(tmpfs_dir)
        for sub_dir in [upper_dir, work_dir]:
            await aiofiles.os.makedirs(sub_dir)

        mount_cmd = [
            'sudo', 'mount', '-t', 'overlay', 'overlay', '-o',
            f'lowerdir=/,upperdir={upper_dir},workdir={work_dir}', merged_dir
        ]
        await execute_command(mount_cmd)

        async def _mount_dev():
            await execute_command(['sudo', 'mount', '--rbind', '/dev', f'{merged_dir}/dev'])
            await execute_command(['sudo', 'mount', '--make-rprivate', f'{merged_dir}/dev'])

        await asyncio.gather(
            execute_command(['sudo', 'mount', '-t', 'proc', '/proc', f'{merged_dir}/proc']),
            execute_command(['sudo', 'mount', '-t', 'sysfs', '/sys', f'{merged_dir}/sys']),
            _mount_dev(),
        )
        await execute_command([
            'cp', '/etc/hosts', f'{merged_dir}/etc/',
            '/etc/resolv.conf', f'{merged_dir}/etc/',
        ], raise_nonzero=False)
    except BaseException:
        await _sweep_remaining_mounts(base_dir)
        try:
            await asyncio.to_thread(shutil.rmtree, base_dir)
        except Exception:
            pass
        raise

    try:
        yield merged_dir
    finally:
        # Batch all unmounts into a single shell invocation to avoid spawning
        # 5 separate subprocesses.  /dev uses -R for recursive unmount because
        # --rbind creates submounts; the rest use plain -l.
        batch_umount = (
            f'umount -Rl {merged_dir}/dev 2>/dev/null;'
            f'umount -l {merged_dir}/sys 2>/dev/null;'
            f'umount -l {merged_dir}/proc 2>/dev/null;'
            f'umount -l {merged_dir} 2>/dev/null;'
            f'umount -l {tmpfs_dir} 2>/dev/null;'
            f'true'
        )
        try:
            await execute_command(['sudo', 'bash', '-c', batch_umount], raise_nonzero=False)
        except Exception:
            logger.warning('batch unmount failed, falling back to sweep', base_dir=base_dir)

        # Safety-net: enumerate any remaining mounts under base_dir and force-detach them.
        remaining = await asyncio.to_thread(_read_mounts_under, base_dir)
        if remaining:
            await _sweep_remaining_mounts(base_dir)
            remaining = await asyncio.to_thread(_read_mounts_under, base_dir)
            if remaining:
                await asyncio.sleep(0.5)
                await _sweep_remaining_mounts(base_dir)
                still = await asyncio.to_thread(_read_mounts_under, base_dir)
                if still:
                    logger.error('mounts still present after sweep retries', base_dir=base_dir, mounts=still)

        try:
            await asyncio.to_thread(shutil.rmtree, base_dir)
        except Exception:
            logger.warning('rmtree failed for overlay dir', path=base_dir)


async def _wait_pid_exit(pid: str, label: str):
    """Wait for a process to exit with exponential backoff (max ~5s total)."""
    delay = 0.005
    elapsed = 0.0
    while elapsed < 5.0:
        if not os.path.exists(f'/proc/{pid}'):
            return
        await asyncio.sleep(delay)
        elapsed += delay
        delay = min(delay * 2, 0.5)
    logger.warning(f'{label} process still alive after kill', pid=pid)


async def _cleanup_group_v1(cg):
    """Kill all processes in a cgroup v1 group and delete it."""
    try:
        with open(f'/sys/fs/cgroup/{cg.replace(":", "/")}/tasks', 'r') as f:
            pids = [p.strip() for p in f.read().splitlines() if p.strip()]

        if pids:
            await execute_command(
                ['sudo', 'bash', '-c', ' '.join(f'kill -9 {pid};' for pid in pids) + 'true'],
                raise_nonzero=False)
            await asyncio.gather(*[_wait_pid_exit(pid, f'cgroup v1 ({cg})') for pid in pids])

        await execute_command(['sudo', 'cgdelete', '-g', cg])
    except Exception as e:
        logger.error(f"Error cleaning up group {cg}: {e}")


async def _cleanup_group_v2(cg_path):
    """Kill all processes in a cgroup v2 group and remove it."""
    try:
        procs_file = os.path.join(cg_path, 'cgroup.procs')
        if os.path.isfile(procs_file):
            with open(procs_file, 'r') as f:
                pids = [p.strip() for p in f.read().splitlines() if p.strip()]
            if pids:
                await execute_command(
                    ['sudo', 'bash', '-c', ' '.join(f'kill -9 {pid};' for pid in pids) + 'true'],
                    raise_nonzero=False)
                await asyncio.gather(*[_wait_pid_exit(pid, f'cgroup v2 ({cg_path})') for pid in pids])
        await execute_command(['sudo', 'rmdir', cg_path], False)
    except Exception as e:
        logger.error(f"Error cleaning up cgroup v2 {cg_path}: {e}")


def _parse_mem_limit(mem_limit: str) -> int:
    """Convert a human-readable memory limit like '4G' to bytes."""
    mem_limit = mem_limit.strip().upper()
    multipliers = {'K': 1024, 'M': 1024**2, 'G': 1024**3, 'T': 1024**4}
    if mem_limit[-1] in multipliers:
        return int(float(mem_limit[:-1]) * multipliers[mem_limit[-1]])
    return int(mem_limit)


@asynccontextmanager
async def tmp_cgroup(mem_limit: Optional[str] = None, cpu_limit: Optional[float] = None):
    """Async context manager that creates temporary cgroup controllers.

    Supports both cgroup v1 (``cgcreate``/``cgset``/``cgexec``) and cgroup v2
    (direct filesystem manipulation under ``/sys/fs/cgroup/``). The version is
    auto-detected at module load time.

    For v1, yields a list of ``cgexec``-compatible specifiers. For v2, yields a
    list containing a single shell snippet that moves the current process into
    the cgroup before executing the target command.

    Args:
        mem_limit: Memory limit string, e.g. ``'4G'``.
        cpu_limit: Fraction of one CPU core, e.g. ``1`` for a full core.

    Yields:
        A list of cgroup specifier strings (v1) or wrapper-script paths (v2).
    """
    if mem_limit is None and cpu_limit is None:
        raise Exception('every resource is unlimited, no need for cgroup')

    if CGROUP_VERSION == 2:
        _init_cgroup_v2_delegation()
        cg_name = f'sandbox_{random_cgroup_name()}'
        cg_path = f'/sys/fs/cgroup/{cg_name}'

        # Batch mkdir + limit writes into a single shell invocation.
        setup_script = f'mkdir -p {cg_path}'
        if mem_limit is not None:
            mem_bytes = str(_parse_mem_limit(mem_limit))
            setup_script += f' && echo {mem_bytes} > {cg_path}/memory.max'
        if cpu_limit is not None:
            quota = int(100000 * cpu_limit)
            setup_script += f' && echo "{quota} 100000" > {cg_path}/cpu.max'
        await execute_command(['sudo', 'bash', '-c', setup_script])

        wrapper = f'/tmp/cg_enter_{cg_name}.sh'
        with open(wrapper, 'w') as f:
            f.write(f'#!/bin/bash\necho $$ > {cg_path}/cgroup.procs\nexec "$@"\n')
        os.chmod(wrapper, stat.S_IRWXU | stat.S_IRGRP | stat.S_IXGRP | stat.S_IROTH | stat.S_IXOTH)

        try:
            yield [wrapper]
        finally:
            await _cleanup_group_v2(cg_path)
            try:
                os.unlink(wrapper)
            except OSError:
                pass
    else:
        groups = []
        setup_tasks = []

        if mem_limit is not None:
            mem_group_name = f'sandbox_mem_{random_cgroup_name()}'
            groups.append(f'memory:{mem_group_name}')
            setup_tasks.append(execute_command([
                'sudo', 'bash', '-c',
                f'cgcreate -g memory:{mem_group_name} && '
                f'cgset -r memory.limit_in_bytes={mem_limit} {mem_group_name}'
            ]))

        if cpu_limit is not None:
            cpu_group_name = f'sandbox_cpu_{random_cgroup_name()}'
            groups.append(f'cpu:{cpu_group_name}')
            setup_tasks.append(execute_command([
                'sudo', 'bash', '-c',
                f'cgcreate -g cpu:{cpu_group_name} && '
                f'cgset -r cpu.cfs_quota_us={int(100000 * cpu_limit)} {cpu_group_name} && '
                f'cgset -r cpu.cfs_period_us=100000 {cpu_group_name}'
            ]))

        if setup_tasks:
            await asyncio.gather(*setup_tasks)

        try:
            yield groups
        finally:
            if len(groups) > 1:
                await asyncio.gather(*[_cleanup_group_v1(cg) for cg in groups])
            elif groups:
                await _cleanup_group_v1(groups[0])


# Pool of /24 subnets in the 172.16.0.0/12 private range, used by
# :func:`tmp_netns` to assign unique subnet addresses to each network
# namespace.  When running under ``pytest-xdist``, the pool is partitioned by
# worker ID so that parallel workers never allocate overlapping subnets.
#
# Access is protected by ``_subnet_lock`` to prevent races between concurrent
# async requests popping/appending on the same list.
_available_subnets: list = []
_subnet_lock = threading.Lock()
_subnet_available_event: asyncio.Event | None = None


def _get_subnet_event() -> asyncio.Event:
    """Lazily create the subnet availability event (must be called within an event loop)."""
    global _subnet_available_event
    if _subnet_available_event is None:
        _subnet_available_event = asyncio.Event()
        if _available_subnets:
            _subnet_available_event.set()
    return _subnet_available_event


pytest_worker_id = os.environ.get("PYTEST_XDIST_WORKER")
if pytest_worker_id is not None:
    for j in range(0, 256):
        _available_subnets.append(f"172.{16 + int(pytest_worker_id[2:])}.{j}")
else:
    for i in range(16, 32):
        for j in range(0, 256):
            _available_subnets.append(f"172.{i}.{j}")
create_netns_script = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../scripts/create_net_namespace.sh'))
clean_netns_script = os.path.abspath(os.path.join(os.path.dirname(__file__), '../../scripts/clean_net_namespace.sh'))


def get_subnet_ip_rfc_2322():
    """Pop and return a subnet prefix from the available pool (thread-safe).

    Returns:
        A subnet prefix string (e.g. ``'172.16.0'``), or ``None`` if the pool
        is exhausted.
    """
    with _subnet_lock:
        if not _available_subnets:
            return None
        return _available_subnets.pop()


def return_subnet_ip_rfc_2322(ip):
    """Return a previously allocated subnet prefix back to the pool (thread-safe).

    Args:
        ip: Subnet prefix string to return (e.g. ``'172.16.0'``).
    """
    with _subnet_lock:
        _available_subnets.append(ip)
    evt = _subnet_available_event
    if evt is not None:
        evt.set()


_SUBNET_ACQUIRE_TIMEOUT = 60


@asynccontextmanager
async def tmp_netns(no_bridge: bool = False):
    """Async context manager that creates a temporary network namespace.

    A unique subnet is allocated from the pool and passed to the
    ``create_net_namespace.sh`` helper script.  On exit the namespace is
    torn down by ``clean_net_namespace.sh`` and the subnet is returned to
    the pool.

    Raises ``RuntimeError`` if no subnet becomes available within
    ``_SUBNET_ACQUIRE_TIMEOUT`` seconds.

    Args:
        no_bridge: If True, the ``--no-bridge`` flag is passed to the creation
            script, preventing bridge/veth setup (useful when no outbound
            connectivity is needed).

    Yields:
        The name of the newly created network namespace.
    """
    net_ns_name = random_cgroup_name()
    evt = _get_subnet_event()
    deadline = time.monotonic() + _SUBNET_ACQUIRE_TIMEOUT
    while True:
        subnet_ip = get_subnet_ip_rfc_2322()
        if subnet_ip is not None:
            break
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                f'subnet pool exhausted: no subnet available after {_SUBNET_ACQUIRE_TIMEOUT}s')
        evt.clear()
        try:
            await asyncio.wait_for(evt.wait(), timeout=min(remaining, 5.0))
        except asyncio.TimeoutError:
            pass
    args = [net_ns_name, subnet_ip]
    if no_bridge:
        args += ['--no-bridge']
    await execute_command(['sudo', create_netns_script] + args)
    try:
        yield net_ns_name
    finally:
        try:
            await execute_command(['sudo', clean_netns_script] + args)
        except Exception:
            logger.warning('netns cleanup failed', netns=net_ns_name)
        return_subnet_ip_rfc_2322(subnet_ip)


async def main():
    """Demo / manual test entry point for the isolation primitives.

    Sets up an overlayfs, cgroup, and network namespace, then runs the command
    given via ``sys.argv[1:]`` inside the chroot.  Prints timing for each
    phase.  Intended for interactive debugging; the call to ``asyncio.run`` at
    the bottom of the file is commented out by default.
    """
    begin = time.time()
    print(f'start: {begin}')
    async with tmp_overlayfs() as root, tmp_cgroup(mem_limit='4G', cpu_limit=0.5) as cgroups, tmp_netns() as netns:
        init = time.time()
        print(f'init finish: {init - begin}')
        prefix = []
        if CGROUP_VERSION == 2:
            prefix += cgroups
        else:
            for cg in cgroups:
                prefix += ['cgexec', '-g', cg]
        chroot_cmd = ['chroot', root]
        # unshare_cmd = ['unshare', '--net', '--pid', '--fork', '--mount-proc']
        unshare_cmd = ['unshare', '--pid', '--fork', '--mount-proc']
        # TODO: mount other volumns per need. see https://superuser.com/questions/165116/mount-dev-proc-sys-in-a-chroot-environment
        final_cmd = prefix + chroot_cmd + ['bash', '-c', f'cd /tmp && {" ".join(sys.argv[1:])}']
        # final_cmd = prefix + chroot_cmd + unshare_cmd + ['bash', '-c', f'cd /tmp && echo $GFD']
        # final_cmd = prefix + chroot_cmd + unshare_cmd + ['bash', '-c', 'cd', '/tmp', '&&'] + sys.argv[1:]
        print(f'cmd: {" ".join(final_cmd)}')
        await execute_command(final_cmd)
        cmd = time.time()
        print(f'run command finish: {cmd - init}')
    teardown = time.time()
    print(f'teardown finish: {teardown - cmd}')


# asyncio.run(main())
