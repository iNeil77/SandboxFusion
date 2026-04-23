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
"""Runner functions for the 15 "major" supported languages.

Each runner follows a common pattern:

1. Create a temporary directory.
2. Restore any supplementary files from ``args.files``.
3. Write ``args.code`` to a language-appropriate temp file.
4. Call :func:`~sandbox.runners.base.run_commands` with the correct compile
   and/or run commands.

The module also exposes two cached helper functions --
:func:`get_python_rt_env` and :func:`get_cpp_rt_flags` -- that probe the host
environment once and cache the results for the lifetime of the process.

``MAJOR_RUNNERS`` maps language identifier strings to their runner coroutines
and is merged into ``CODE_RUNNERS`` by :mod:`sandbox.runners`.
"""

import os
import shutil
import subprocess
import tempfile
from functools import cache

import structlog

from sandbox.configs.run_config import RunConfig
from sandbox.runners.base import restore_files, run_command_bare, run_commands
from sandbox.runners.types import CodeRunArgs, CodeRunResult, CommandRunStatus
from sandbox.utils.common import ensure_php_tag_in_string, find_conda_root
from sandbox.utils.execution import get_tmp_dir
from sandbox.utils.extraction import find_java_public_class_name

logger = structlog.stdlib.get_logger()
config = RunConfig.get_instance_sync()


@cache
def get_python_rt_env(env_name: str):
    """Return a ``PATH`` env dict pointing at the named conda environment's Python.

    Activates the specified conda environment, locates its ``python`` binary,
    and builds a ``PATH`` string that prepends that directory while filtering
    out any existing ``sandbox`` environment paths to avoid conflicts.

    Args:
        env_name: Name of the conda environment (e.g. ``'sandbox-runtime'``).

    Returns:
        A dict with a single ``'PATH'`` key suitable for passing as
        *extra_env* to :func:`~sandbox.runners.base.run_command_bare`.
    """
    r = subprocess.run(f'bash -c "source {find_conda_root()}/bin/activate {env_name} && which python"',
                       capture_output=True,
                       text=True,
                       check=True,
                       shell=True)
    python_path = os.path.dirname(r.stdout)
    original_paths = os.environ.get('PATH', '').split(':')
    filtered_path = ':'.join([p for p in original_paths if '/envs/sandbox/' not in p])
    return {'PATH': f'{python_path}:{filtered_path}'}


__cpp_rt_flags = None


async def get_cpp_rt_flags():
    """Probe the host compiler for optional linker flags and cache the result.

    Tries each flag in ``[-lcrypto, -lssl, -lpthread]`` by compiling a trivial
    C++ program.  Flags that produce a successful build are remembered and
    appended to all subsequent C++ compile commands.

    Returns:
        A list of flag strings that the host ``g++`` supports.
    """
    global __cpp_rt_flags
    if __cpp_rt_flags is not None:
        return __cpp_rt_flags

    logger.info(f'checking available gcc flags...')
    optional_flags = ['-lcrypto', '-lssl', '-lpthread']
    with tempfile.TemporaryDirectory(dir=get_tmp_dir(), ignore_cleanup_errors=True) as tmp_dir:
        with tempfile.NamedTemporaryFile(mode='w', dir=tmp_dir, suffix='.cpp', delete=False) as f:
            f.write('int main() {return 0;}')

        __cpp_rt_flags = []
        for flag in optional_flags:
            compile_res = await run_command_bare(f'g++ {f.name} -o test {flag}', cwd=tmp_dir)
            if compile_res.status == CommandRunStatus.Finished and compile_res.return_code == 0:
                __cpp_rt_flags.append(flag)
                logger.info(f'flag {flag} added')
            else:
                logger.info(f'flag {flag} not available')
    return __cpp_rt_flags


async def run_python(args: CodeRunArgs) -> CodeRunResult:
    """Run Python code using the ``sandbox-runtime`` conda environment."""
    with tempfile.TemporaryDirectory(dir=get_tmp_dir(), ignore_cleanup_errors=True) as tmp_dir:
        restore_files(tmp_dir, args.files)
        with tempfile.NamedTemporaryFile(mode='w', dir=tmp_dir, suffix='.py', delete=False) as f:
            f.write(args.code)

        return await run_commands(None,
                                  f'python {f.name}',
                                  tmp_dir,
                                  get_python_rt_env('sandbox-runtime'),
                                  args)


async def run_pytest(args: CodeRunArgs) -> CodeRunResult:
    """Run Python test code via ``pytest`` in the ``sandbox-runtime`` conda environment."""
    with tempfile.TemporaryDirectory(dir=get_tmp_dir(), ignore_cleanup_errors=True) as tmp_dir:
        restore_files(tmp_dir, args.files)
        with tempfile.NamedTemporaryFile(mode='w', dir=tmp_dir, suffix='.py', delete=False) as f:
            f.write(args.code)

        return await run_commands(None, f'pytest {f.name}', tmp_dir, get_python_rt_env('sandbox-runtime'), args)


async def run_cpp(args: CodeRunArgs) -> CodeRunResult:
    """Compile C++ code with ``g++ -std=c++17`` (plus probed flags) and run the binary."""
    flags = await get_cpp_rt_flags()
    with tempfile.TemporaryDirectory(dir=get_tmp_dir(), ignore_cleanup_errors=True) as tmp_dir:
        restore_files(tmp_dir, args.files)
        with tempfile.NamedTemporaryFile(mode='w', dir=tmp_dir, suffix='.cpp', delete=False) as f:
            f.write(args.code)

        return await run_commands(f'g++ -std=c++17 {f.name} -o test {" ".join(flags)}', './test', tmp_dir, {}, args)


async def run_csharp(args: CodeRunArgs) -> CodeRunResult:
    """Create a .NET console project, replace ``Program.cs``, and run via ``dotnet run``."""
    with tempfile.TemporaryDirectory(dir=get_tmp_dir(), ignore_cleanup_errors=True) as tmp_dir:
        restore_files(tmp_dir, args.files)
        create_project_command = f"dotnet new console -o {tmp_dir}"
        await run_command_bare(create_project_command, timeout=args.compile_timeout, cwd=tmp_dir)
        cs_file_path = os.path.join(tmp_dir, 'Program.cs')
        with open(cs_file_path, 'w') as cs_file:
            cs_file.write(args.code)

        return await run_commands(None, f'dotnet run --project {tmp_dir}', tmp_dir, {}, args)


async def run_go_test(args: CodeRunArgs) -> CodeRunResult:
    """Run Go test code via ``go test``, copying runtime support files first."""
    with tempfile.TemporaryDirectory(dir=get_tmp_dir(), ignore_cleanup_errors=True) as tmp_dir:
        source_dir = os.path.abspath(os.path.join(__file__, '../../../runtime/go'))
        for file in os.listdir(source_dir):
            shutil.copy2(os.path.join(source_dir, file), tmp_dir)
        restore_files(tmp_dir, args.files)
        with tempfile.NamedTemporaryFile(mode='w', dir=tmp_dir, suffix='_test.go', delete=False) as f:
            f.write(args.code)

        return await run_commands(None, f'go test {f.name}', tmp_dir, {}, args)


async def run_go(args: CodeRunArgs) -> CodeRunResult:
    """Compile Go code with ``go build`` and run the resulting binary."""
    with tempfile.TemporaryDirectory(dir=get_tmp_dir(), ignore_cleanup_errors=True) as tmp_dir:
        source_dir = os.path.abspath(os.path.join(__file__, '../../../runtime/go'))
        for file in os.listdir(source_dir):
            shutil.copy2(os.path.join(source_dir, file), tmp_dir)
        restore_files(tmp_dir, args.files)
        with tempfile.NamedTemporaryFile(mode='w', dir=tmp_dir, suffix='.go', delete=False) as f:
            f.write(args.code)

        return await run_commands(f'go build -o out {f.name}', './out', tmp_dir, {}, args)


async def run_java(args: CodeRunArgs) -> CodeRunResult:
    """Compile and run Java code, including ``javatuples`` and any user-supplied JARs on the classpath."""
    with tempfile.TemporaryDirectory(dir=get_tmp_dir(), ignore_cleanup_errors=True) as tmp_dir:
        deps_dir = os.path.abspath(os.path.join(__file__, '../../../runtime/java'))
        shutil.copy2(os.path.join(deps_dir, 'javatuples-1.2.jar'), tmp_dir)
        restore_files(tmp_dir, args.files)
        jars = ['.', 'javatuples-1.2.jar'] + [filename for filename in args.files.keys() if filename.endswith('.jar')]
        cpargs = f'-cp {":".join(jars)}'
        fn = os.path.join(tmp_dir, 'Main.java')
        with open(fn, 'w') as f:
            f.write(args.code)

        return await run_commands(f'javac {cpargs} Main.java', f'java {cpargs} -ea Main', tmp_dir, {}, args)


async def run_junit(args: CodeRunArgs) -> CodeRunResult:
    """Compile Java test code and execute it with JUnit 5 console launcher."""
    junit_jar = 'junit-platform-console-standalone-1.8.2.jar'
    deps = ['junit-jupiter-api-5.11.0-javadoc.jar']
    with tempfile.TemporaryDirectory(dir=get_tmp_dir(), ignore_cleanup_errors=True) as tmp_dir:
        deps_dir = os.path.abspath(os.path.join(__file__, '../../../runtime/java'))
        for dep in deps:
            shutil.copy2(os.path.join(deps_dir, dep), tmp_dir)
        shutil.copy2(os.path.join(deps_dir, junit_jar), tmp_dir)
        restore_files(tmp_dir, args.files)
        jars = ['.', junit_jar] + deps + [filename for filename in args.files.keys() if filename.endswith('.jar')]
        cpargs = f'{":".join(jars)}'
        if args.code:
            class_name = find_java_public_class_name(args.code) or 'Main'
            fn = os.path.join(tmp_dir, f'{class_name}.java')
            with open(fn, 'w') as f:
                f.write(args.code)

        return await run_commands(f'javac -cp {cpargs} *.java',
                                  f'java -jar ./{junit_jar} --class-path {cpargs} --scan-class-path', tmp_dir, {}, args)


async def run_nodejs(args: CodeRunArgs) -> CodeRunResult:
    """Run JavaScript code via ``node``, symlinking the shared ``node_modules``."""
    # Would be costly to copy entire node_modules into a new tmp folder, so we just symlink it
    deps_dir = os.path.abspath(os.path.join(__file__, '../../../runtime/node'))
    with tempfile.TemporaryDirectory(dir=get_tmp_dir(), ignore_cleanup_errors=True) as tmp_dir:
        restore_files(tmp_dir, args.files)
        os.symlink(os.path.join(deps_dir, 'node_modules'), os.path.join(tmp_dir, 'node_modules'))
        with tempfile.NamedTemporaryFile(mode='w', dir=tmp_dir, suffix='.js', delete=False) as f:
            f.write(args.code)

        return await run_commands(None, f'node {f.name}', tmp_dir, {}, args)


async def run_typescript(args: CodeRunArgs) -> CodeRunResult:
    """Run TypeScript code via ``tsx``, symlinking the shared ``node_modules``."""
    deps_dir = os.path.abspath(os.path.join(__file__, '../../../runtime/node'))
    with tempfile.TemporaryDirectory(dir=get_tmp_dir(), ignore_cleanup_errors=True) as tmp_dir:
        restore_files(tmp_dir, args.files)
        os.symlink(os.path.join(deps_dir, 'node_modules'), os.path.join(tmp_dir, 'node_modules'))
        with tempfile.NamedTemporaryFile(mode='w', dir=tmp_dir, suffix='.ts', delete=False) as f:
            f.write(args.code)

        return await run_commands(None, f'tsx {f.name}', tmp_dir, {}, args)


async def run_jest(args: CodeRunArgs) -> CodeRunResult:
    """Run TypeScript test code via Jest (``npm run test``), symlinking shared Node runtime files."""
    deps_dir = os.path.abspath(os.path.join(__file__, '../../../runtime/node'))
    with tempfile.TemporaryDirectory(dir=get_tmp_dir(), ignore_cleanup_errors=True) as tmp_dir:
        restore_files(tmp_dir, args.files)
        for fn in ['node_modules', 'package.json', 'babel.config.js']:
            os.symlink(os.path.join(deps_dir, fn), os.path.join(tmp_dir, fn))
        with tempfile.NamedTemporaryFile(mode='w', dir=tmp_dir, suffix='.test.ts', delete=False) as f:
            f.write(args.code)

        return await run_commands(None, f'npm run test', tmp_dir, {}, args)


async def run_php(args: CodeRunArgs) -> CodeRunResult:
    """Run PHP code via ``php -f``, ensuring the ``<?php`` tag is present."""
    with tempfile.TemporaryDirectory(dir=get_tmp_dir(), ignore_cleanup_errors=True) as tmp_dir:
        restore_files(tmp_dir, args.files)
        with tempfile.NamedTemporaryFile(mode='w', dir=tmp_dir, suffix='.php', delete=False) as f:
            f.write(ensure_php_tag_in_string(args.code))

        return await run_commands(None, f'php -f {f.name}', tmp_dir, {}, args)


async def run_rust(args: CodeRunArgs) -> CodeRunResult:
    """Compile Rust code with ``rustc`` and run the resulting binary."""
    with tempfile.TemporaryDirectory(dir=get_tmp_dir(), ignore_cleanup_errors=True) as tmp_dir:
        restore_files(tmp_dir, args.files)
        with tempfile.NamedTemporaryFile(mode='w', dir=tmp_dir, suffix='.rs', delete=False) as f:
            f.write(args.code)

        return await run_commands(f'rustc {f.name} -o test', './test', tmp_dir, {}, args)


async def run_bash(args: CodeRunArgs) -> CodeRunResult:
    """Run a Bash script via ``/bin/bash``."""
    with tempfile.TemporaryDirectory(dir=get_tmp_dir(), ignore_cleanup_errors=True) as tmp_dir:
        restore_files(tmp_dir, args.files)
        with tempfile.NamedTemporaryFile(mode='w', dir=tmp_dir, suffix='.sh', delete=False) as f:
            f.write(args.code)

        return await run_commands(None, f'/bin/bash {f.name}', tmp_dir, {}, args)


# Mapping of language identifier strings to their async runner coroutines.
# Includes aliases (e.g. 'js' -> run_nodejs, 'ts' -> run_typescript).
MAJOR_RUNNERS = {
    'cpp': run_cpp,
    'go': run_go,
    'go_test': run_go_test,
    'java': run_java,
    'junit': run_junit,
    'nodejs': run_nodejs,
    'js': run_nodejs,
    'ts': run_typescript,
    'typescript': run_typescript,
    'python': run_python,
    'pytest': run_pytest,
    'csharp': run_csharp,
    'rust': run_rust,
    'php': run_php,
    'bash': run_bash,
    'jest': run_jest,
}
