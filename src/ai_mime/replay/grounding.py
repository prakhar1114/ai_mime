import base64
import json
import re
import textwrap
from pathlib import Path
from typing import Any

from openai import OpenAI
from PIL import Image

from .engine import ReplayConfig, ReplayError


_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")

# A lightweight, OpenAI-compatible tool-instruction prompt inspired by the Qwen
# computer use cookbook: https://github.com/QwenLM/Qwen3-VL/blob/main/cookbooks/computer_use.ipynb
#
# We don't depend on qwen-agent's NousFnCallPrompt; instead we embed the tool schema text directly.
COMPUTER_USE_SYSTEM_PROMPT = textwrap.dedent(
    """
    # Tools

    You may call one function per turn to assist with the user query.

    You are provided with function signatures within <tools></tools> XML tags:
    <tools>
    [
      {
        "type": "function",
        "function": {
          "name": "computer_use",
          "description": "Use a mouse and keyboard to interact with a computer.\\n* The screen's resolution is 1000x1000.\\n* Whenever you intend to click, consult the screenshot to determine coordinates.\\n* Click with the cursor tip in the center of the element.",
          "parameters": {
            "type": "object",
            "required": ["action", "observation", "task_memory"],
            "properties": {
              "action": {
                "type": "string",
                "enum": [
                  "key",
                  "type",
                  "mouse_move",
                  "left_click",
                  "left_click_drag",
                  "right_click",
                  "middle_click",
                  "double_click",
                  "triple_click",
                  "scroll",
                  "hscroll",
                  "wait"
                ],
                "description": "The action to perform."
              },
              "observation": {
                "type": "string",
                "description": "Required. Current UI specific observationrequired to complete the current subtask."
              },
              "task_memory": {
                "type": "string",
                "description": "Required. A concise memory string to carry across subtasks. Update sparingly with important results."
              },
              "keys": {
                "type": "array",
                "description": "Required only by action=key. Example: [\\"cmd\\", \\"space\\"] or [\\"enter\\"]."
              },
              "text": {
                "type": "string",
                "description": "Required only by action=type."
              },
              "coordinate": {
                "type": "array",
                "description": "(x, y) in 0..1000 reference frame for mouse actions."
              },
              "pixels": {
                "type": "number",
                "description": "Scroll amount for action=scroll/hscroll."
              },
              "time": {
                "type": "number",
                "description": "Seconds to wait for action=wait."
              }
            }
          }
        }
      },
      {
        "type": "function",
        "function": {
          "name": "done",
          "description": "Signal that the current subtask is complete and provide the result to carry forward.",
          "parameters": {
            "type": "object",
            "required": ["result", "task_memory"],
            "properties": {
              "result": {
                "type": "string",
                "description": "Required. The final result of this subtask (e.g., 'Spotify is open', 'Song is playing', or info to pass to later subtasks)."
              },
              "task_memory": {
                "type": "string",
                "description": "Required. Updated memory string to carry across subtasks. Use sparingly for important results."
              }
            }
          }
        }
      }
    ]
    </tools>

    IMPORTANT:
    - If you call computer_use, you MUST include BOTH "observation" and "task_memory" in arguments.
    - "observation" must be current-step specific (what you see/what changed that is relevant right now).
    - "task_memory" must be a concise carried-forward memory; update sparingly with important results.
    - If you call done, you MUST include "result" and "task_memory".

    For each function call, return a JSON object with function name and arguments within <tool_call></tool_call> XML tags:
    <tool_call>
    {"name":"computer_use","arguments":{"action":"left_click","coordinate":[500,500],"observation":"...","task_memory":"..."}}
    </tool_call>

    Or, to finish the current subtask:
    <tool_call>
    {"name":"done","arguments":{"result":"...","task_memory":"..."}}
    </tool_call>
    """
).strip()


def _encode_image_data_url(image_path: Path) -> str:
    suffix = image_path.suffix.lower()
    if suffix == ".png":
        mime = "image/png"
    elif suffix in (".jpg", ".jpeg"):
        mime = "image/jpeg"
    elif suffix == ".webp":
        mime = "image/webp"
    else:
        # Default to png if unknown; caller should save png.
        mime = "image/png"
    b64 = base64.b64encode(image_path.read_bytes()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def _extract_json_payload(text: str) -> dict[str, Any]:
    """
    Try to extract a JSON object from model output.
    Supports raw JSON or <tool_call> wrapped output like the notebook.
    """
    if not text:
        raise ReplayError("Empty model output")

    # If wrapped like <tool_call> ... </tool_call>
    if "<tool_call>" in text:
        try:
            inner = text.split("<tool_call>", 1)[1]
            inner = inner.split("</tool_call>", 1)[0]
            inner = inner.strip()
            return json.loads(inner)
        except Exception:
            pass

    # Try raw json parse first (common when we instruct strict JSON)
    try:
        return json.loads(text.strip())
    except Exception:
        pass

    # Fallback: find first JSON-ish object.
    m = _JSON_OBJECT_RE.search(text)
    if not m:
        raise ReplayError(f"Could not find JSON object in model output: {text[:2000]}")
    try:
        return json.loads(m.group(0))
    except Exception as e:
        raise ReplayError(f"Failed to parse JSON from model output: {e}") from e


def _validate_relative_coordinate(payload: dict[str, Any]) -> tuple[float, float]:
    coord = payload.get("coordinate") or payload.get("arguments", {}).get("coordinate")
    if not isinstance(coord, list) or len(coord) != 2:
        raise ReplayError(f"Invalid coordinate field in payload: {payload}")
    try:
        x = float(coord[0])
        y = float(coord[1])
    except Exception as e:
        raise ReplayError(f"Coordinate values must be numeric: {coord}") from e
    return x, y


def predict_computer_use_tool_call(image_path: Path, user_query: str, cfg: ReplayConfig) -> dict[str, Any]:
    """
    Ask Qwen3-VL to output exactly one <tool_call> for the next GUI action.
    Returns the parsed tool call object: {"name": "...", "arguments": {...}}.
    """
    if not cfg.api_key:
        raise ReplayError("Missing DASHSCOPE_API_KEY (or api_key) for grounding")

    img_path = Path(image_path)
    if not img_path.exists():
        raise ReplayError(f"Screenshot not found: {img_path}")

    data_url = _encode_image_data_url(img_path)
    client = OpenAI(api_key=cfg.api_key, base_url=cfg.base_url)

    # openai-python has strict typing for messages; keep runtime structure but cast for type checkers.
    messages: Any = [
        {
            "role": "system",
            "content": [
                {"type": "text", "text": "You are a helpful assistant."},
                {"type": "text", "text": COMPUTER_USE_SYSTEM_PROMPT},
            ],
        },
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_url}},
                {"type": "text", "text": user_query},
            ],
        },
    ]

    last_err: Exception | None = None
    last_text: str = ""

    for attempt in range(3):
        if attempt > 0:
            # Repair prompt to force the missing required fields.
            messages = [
                *messages,
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Your previous tool call was invalid.\n"
                                f"Failure: {last_err}\n"
                                f"Previous output: {last_text}\n\n"
                                "Return EXACTLY ONE <tool_call> JSON.\n"
                                "- If name=computer_use: arguments MUST include action, observation (non-empty), task_memory (string).\n"
                                "- If name=done: arguments MUST include result (non-empty), task_memory (string).\n"
                                "Do not omit required fields."
                            ),
                        }
                    ],
                },
            ]

        try:
            completion = client.chat.completions.create(
                model=cfg.model,
                messages=messages,
            )
            content = (completion.choices[0].message.content or "").strip()
            last_text = content

            tool_call = _extract_json_payload(content)
            if not isinstance(tool_call, dict) or tool_call.get("name") not in {"computer_use", "done"}:
                raise ReplayError(f"Expected tool call with name=computer_use|done, got: {tool_call}")
            args = tool_call.get("arguments")
            if not isinstance(args, dict):
                raise ReplayError(f"Tool call missing arguments: {tool_call}")

            name = tool_call.get("name")
            if name == "computer_use":
                if "action" not in args:
                    raise ReplayError(f"computer_use missing arguments.action: {tool_call}")
                if not isinstance(args.get("observation"), str) or not args.get("observation", "").strip():
                    raise ReplayError(f"computer_use missing required arguments.observation: {tool_call}")
                if not isinstance(args.get("task_memory"), str):
                    raise ReplayError(f"computer_use missing required arguments.task_memory: {tool_call}")
            else:
                if not isinstance(args.get("result"), str) or not args.get("result", "").strip():
                    raise ReplayError(f"done missing required arguments.result: {tool_call}")
                if not isinstance(args.get("task_memory"), str):
                    raise ReplayError(f"done missing required arguments.task_memory: {tool_call}")

            return tool_call
        except Exception as e:
            last_err = e

    raise ReplayError(f"Failed to get a valid tool call after retries: {last_err}. Last output: {last_text}") from last_err


def tool_call_to_pixel_action(image_path: Path, tool_call: dict[str, Any]) -> dict[str, Any]:
    """
    Convert a tool_call with 0..1000 coordinates into pixel coordinates for the given screenshot.
    Returns a dict with normalized fields:
      - action: str
      - keys/text/time/pixels optional
      - x_px/y_px for mouse actions when coordinate present
    """
    if tool_call.get("name") == "done":
        # No screen interaction; pass through done payload.
        args = tool_call.get("arguments") or {}
        if not isinstance(args, dict):
            raise ReplayError(f"Invalid done.arguments: {tool_call}")
        return {"action": "done", "result": args.get("result"), "task_memory": args.get("task_memory")}

    args = tool_call.get("arguments") or {}
    if not isinstance(args, dict):
        raise ReplayError(f"Invalid tool_call.arguments: {tool_call}")

    action = args.get("action")
    if not isinstance(action, str) or not action:
        raise ReplayError(f"Invalid tool_call.arguments.action: {tool_call}")

    out: dict[str, Any] = {"action": action}
    # Required metadata for replay loop
    out["observation"] = args.get("observation")
    out["task_memory"] = args.get("task_memory")

    # Pass-through fields
    if "keys" in args:
        out["keys"] = args.get("keys")
    if "text" in args:
        out["text"] = args.get("text")
    if "time" in args:
        out["time"] = args.get("time")
    if "pixels" in args:
        out["pixels"] = args.get("pixels")
    if "status" in args:
        out["status"] = args.get("status")

    # Map coordinates if present
    if "coordinate" in args and args.get("coordinate") is not None:
        x_rel, y_rel = _validate_relative_coordinate(args)
        with Image.open(Path(image_path)) as im:
            w, h = im.size
        x_px = int(round((x_rel / 1000.0) * w))
        y_px = int(round((y_rel / 1000.0) * h))
        x_px = max(0, min(w - 1, x_px))
        y_px = max(0, min(h - 1, y_px))
        out["x_px"] = x_px
        out["y_px"] = y_px

    return out
