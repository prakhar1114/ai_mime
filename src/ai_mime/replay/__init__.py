"""
Replay package.

This package contains:
- workflow discovery ("catalog")
- schema-driven replay engine
- Qwen computer-use grounding
- OS action execution
"""

from .catalog import WorkflowRef, list_replayable_workflows, resolve_workflow
from .engine import ReplayConfig, ReplayError, load_schema, resolve_params, run_plan
from .grounding import (
    COMPUTER_USE_SYSTEM_PROMPT,
    predict_computer_use_tool_call,
    tool_call_to_pixel_action,
)
from .os_executor import exec_computer_use_action

__all__ = [
    "WorkflowRef",
    "list_replayable_workflows",
    "resolve_workflow",
    "ReplayConfig",
    "ReplayError",
    "load_schema",
    "resolve_params",
    "run_plan",
    "COMPUTER_USE_SYSTEM_PROMPT",
    "predict_computer_use_tool_call",
    "tool_call_to_pixel_action",
    "exec_computer_use_action",
]
