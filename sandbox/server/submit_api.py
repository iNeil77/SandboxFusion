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

from fastapi import APIRouter, HTTPException

from sandbox.datasets.types import EvalResult, SubmitRequest
from sandbox.utils.extraction import default_extract_helper
from sandbox.utils.testing import check_stdio_test_cases_parallel

submit_router = APIRouter()


@submit_router.post("/submit", description='Submit code for evaluation against stdin/stdout test cases', tags=['eval'])
async def submit(request: SubmitRequest) -> EvalResult:
    if not request.config.language:
        raise HTTPException(status_code=400, detail='config.language is required')

    code = default_extract_helper(request.completion, request.config.language, request.config.custom_extract_logic)
    outcomes = await check_stdio_test_cases_parallel(code, request.test_cases, request.config)
    return EvalResult(
        id=request.id,
        accepted=all(o.passed for o in outcomes),
        extracted_code=code,
        tests=outcomes,
    )
