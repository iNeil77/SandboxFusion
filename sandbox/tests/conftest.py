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

Handles two responsibilities:

1. Setting a ``sandbox._called_from_test`` flag so that production code
   can detect when it is running inside the test harness (e.g. to use
   lighter-weight resource defaults).
2. Initialising structured logging early so that log output from runners
   and datasets is captured consistently during test runs.
"""


def pytest_configure(config):
    """Set the test-mode flag on the sandbox package and configure logging.

    Called by pytest before test collection begins.  Sets
    ``sandbox._called_from_test = True`` so that runtime code can adapt
    its behaviour for testing, then initialises structured logging via
    :func:`sandbox.utils.logging.configure_logging`.
    """
    import sandbox
    sandbox._called_from_test = True
    from sandbox.utils.logging import configure_logging
    configure_logging()


def pytest_unconfigure(config):
    """Remove the test-mode flag after all tests have finished.

    Called by pytest during shutdown.  Deletes the
    ``sandbox._called_from_test`` attribute so that the package state is
    clean if it continues to be used in the same process.
    """
    import sandbox
    del sandbox._called_from_test
