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

import os
from typing import Literal, Optional

import structlog
import yaml
from pydantic import BaseModel

logger = structlog.stdlib.get_logger()


class RunConfig(BaseModel):

    class SandboxConfig(BaseModel):
        '''
        lite: handcrafted overlayfs + chroot + cgroups isolation, fast (< 100 ms overhead)
        full: Docker container isolation with resource limits
        '''
        isolation: Literal['lite', 'full']
        max_concurrency: int
        docker_image: str = 'sandbox:base'

    class EvalConfig(BaseModel):
        max_runner_concurrency: int = 0

    class Common(BaseModel):
        logging_color: bool

    sandbox: SandboxConfig
    eval: EvalConfig = EvalConfig()
    common: Common

    def __init__(self):
        config_path = os.path.abspath(
            os.path.join(os.path.dirname(__file__), f'{os.getenv("SANDBOX_CONFIG", "local")}.yaml'))
        with open(config_path) as f:
            data = yaml.safe_load(f)
        super().__init__(**data)

    # singleton logic
    _instance: Optional['RunConfig'] = None

    @classmethod
    def get_instance_sync(cls, *args, **kwargs) -> 'RunConfig':
        if not cls.__private_attributes__['_instance'].default:
            self = cls(*args, **kwargs)
            assert not hasattr(
                self, 'async_init'), f'class {cls.__name__} has async_init function, init it with get_instance_async.'
            cls.__private_attributes__['_instance'].default = self
            logger.debug('singleton class initialized', name=cls.__name__)
        return cls.__private_attributes__['_instance'].default
