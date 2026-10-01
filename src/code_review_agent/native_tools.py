"""Explicit local tools, with no model-controlled execution."""

from typing import Any

from pydantic import BaseModel, ConfigDict


class BaseTool(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    name: str = ""
    description: str = ""
    args_schema: type[BaseModel] | None = None

    def run(self, *args: Any, **kwargs: Any) -> Any:
        if args:
            return self._run(*args, **kwargs)
        parsed = self.args_schema.model_validate(kwargs) if self.args_schema else None
        return self._run(**(parsed.model_dump() if parsed else kwargs))

    def _run(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError
