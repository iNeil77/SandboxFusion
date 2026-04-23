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

from enum import Enum
from typing import Dict, Literal, Optional, List, Any, Union, TYPE_CHECKING
if TYPE_CHECKING:
    from pydantic.v1 import BaseModel, Field
else:
    try:
        from pydantic.v1 import BaseModel, Field
    except ImportError:
        from pydantic import BaseModel, Field

# Sandbox related


class CommandRunStatus(str, Enum):
    Finished = 'Finished'
    Error = 'Error'
    TimeLimitExceeded = 'TimeLimitExceeded'


class CommandRunResult(BaseModel):
    status: CommandRunStatus
    execution_time: Optional[float] = None
    return_code: Optional[int] = None
    stdout: Optional[str] = None
    stderr: Optional[str] = None


class CodeRunArgs(BaseModel):
    code: str
    files: Dict[str, str] = {}
    compile_timeout: float = 10
    run_timeout: float = 10
    stdin: Optional[str] = None
    fetch_files: List[str] = []


class CodeRunResult(BaseModel):
    compile_result: Optional[CommandRunResult] = None
    run_result: Optional[CommandRunResult] = None
    files: Dict[str, str] = {}


Language = Literal['python', 'cpp', 'nodejs', 'go', 'go_test', 'java', 'php', 'csharp', 'bash', 'typescript', 'sql',
                   'rust', 'lua', 'R', 'perl', 'D_ut', 'ruby', 'scala', 'julia', 'pytest', 'junit',
                   'kotlin_script', 'jest', 'verilog', 'lean', 'swift', 'racket']


class RunStatus(str, Enum):
    Success = 'Success'
    Failed = 'Failed'
    SandboxError = 'SandboxError'


class RunCodeRequest(BaseModel):
    compile_timeout: float = Field(10, description='compile timeout for compiled languages')
    run_timeout: float = Field(10, description='code run timeout')
    memory_limit_MB: int = Field(-1, description='maximum memory allowed in megabytes')
    code: str = Field(..., examples=['print("hello")'], description='the code to run')
    stdin: Optional[str] = Field(None, examples=[''], description='optional string to pass into stdin')
    language: Language = Field(..., examples=['python'], description='the language or execution mode to run the code')
    files: Dict[str, Optional[str]] = Field({}, description='a dict from file path to base64 encoded file content')
    fetch_files: List[str] = Field([], description='a list of file paths to fetch after code execution')


class RunCodeResponse(BaseModel):
    status: RunStatus
    message: str
    compile_result: Optional[CommandRunResult] = None
    run_result: Optional[CommandRunResult] = None
    executor_pod_name: Optional[str] = None
    files: Dict[str, str] = {}


class SummaryMapping(BaseModel):
    Success: str = RunStatus.Success
    Failed: str = RunStatus.Failed
    CompileFailed: Optional[str] = None
    CompileTimeout: Optional[str] = None
    RunFailed: Optional[str] = None
    RunTimeout: Optional[str] = None


# Eval related


class GeneralStdioTest(BaseModel):
    input: Dict[str, str]
    output: Dict[str, str]


class TestConfig(BaseModel):
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
    id: Union[int, str]
    accepted: bool
    extracted_code: str
    full_code: Optional[str] = None
    test_code: Optional[str] = None
    tests: List[EvalTestCase]
    extracted_type: Optional[Literal['fenced', 'incomplete_fenced', 'heuristic', 'empty']] = None
    extra: Optional[Dict] = None


class SubmitRequest(BaseModel):
    id: Union[int, str]
    completion: str
    config: TestConfig
    test_cases: List[GeneralStdioTest]
