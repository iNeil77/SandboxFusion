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

from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from sandbox.runners.types import CommandRunResult, CommandRunStatus, Language  # nopycln: import
from sandbox.server.sandbox_api import RunCodeRequest, RunCodeResponse, RunStatus  # nopycln: import


# OJ related


class Message(BaseModel):
    role: str
    content: str


class Prompt(BaseModel):
    id: int | str
    prompt: str | List[Message]
    labels: Dict[str, Any] = {}


class GeneralStdioTest(BaseModel):
    # stdin / stdout for the standard streams, other names for files
    input: Dict[str, str]
    output: Dict[str, str]


class TestConfig(BaseModel):
    '''
    custom_extract_logic: a piece of python code that calls `submit_code_blocks(cbs)` to extract custom code
                          cbs: List[CodeBlock], CodeBlock(priority=40, code='xxx', language='xxx')
                          priority: fenced = 30, incomplete fenced = 20, heuristic = 10
    '''
    __test__ = False
    language: Optional[Language] = None
    locale: Optional[str] = None
    compile_timeout: Optional[float] = None
    run_timeout: Optional[float] = None
    custom_extract_logic: Optional[str] = None
    extra: Dict[str, Any] = {}


class EvalTestCase(BaseModel):
    passed: bool
    exec_info: RunCodeResponse
    test_info: Optional[Dict[str, Any]] = None


class EvalResult(BaseModel):
    id: int | str
    accepted: bool
    extracted_code: str
    full_code: Optional[str] = None
    test_code: Optional[str] = None
    tests: List[EvalTestCase]
    extracted_type: Optional[Literal['fenced', 'incomplete_fenced', 'heuristic', 'empty']] = None
    extra: Optional[Dict] = None


class SubmitRequest(BaseModel):
    id: int | str
    completion: str
    config: TestConfig
    test_cases: List[GeneralStdioTest]
