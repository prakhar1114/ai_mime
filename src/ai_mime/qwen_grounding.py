import base64
import json
import re
import textwrap
from pathlib import Path
from typing import Any

from openai import OpenAI
from PIL import Image

from ai_mime.replay_engine import ReplayConfig, ReplayError


_JSON_OBJECT_RE = re.compile(r"\{[\s\S]*\}")

# A lightweight, OpenAI-compatible tool-instruction prompt inspired by the Qwen
# computer use cookbook: https://github.com/QwenLM/Qwen3-VL/blob/main/cookbooks/computer_use.ipynb
#
# We don't depend on qwen-agent's NousFnCallPrompt; instead we embed the tool schema text directly.
COMPUTER_USE_SYSTEM_PROMPT = textwrap.dedent(
    """
    # Tools

    You may call one or more functions to assist with the user query.

    You are provided with function signatures within <tools></tools> XML tags:
    <tools>
    {
      "type": "function",
      "function": {
        "name": "computer_use",
        "description": "Use a mouse and keyboard to interact with a computer.\\n* The screen's resolution is 1000x1000.\\n* Whenever you intend to click, consult the screenshot to determine coordinates.\\n* Click with the cursor tip in the center of the element.",
        "parameters": {
          "type": "object",
          "required": ["action"],
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
                "wait",
                "terminate",
                "answer"
              ],
              "description": "The action to perform."
            },
            "keys": {
              "type": "array",
              "description": "Required only by action=key. Example: [\\"cmd\\", \\"space\\"] or [\\"enter\\"]."
            },
            "text": {
              "type": "string",
              "description": "Required only by action=type and action=answer."
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
            },
            "status": {
              "type": "string",
              "enum": ["success", "failure"],
              "description": "Required only by action=terminate."
            }
          }
        }
      }
    }
    </tools>

    For each function call, return a JSON object with function name and arguments within <tool_call></tool_call> XML tags:
    <tool_call>
    {"name":"computer_use","arguments":{...}}
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
                {
                    "type": "text",
                    "text": "You are a helpful assistant.",
                },
                {
                    "type": "text",
                    "text": COMPUTER_USE_SYSTEM_PROMPT,
                }
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

    completion = client.chat.completions.create(
        model=cfg.model,
        messages=messages,
    )

    content = (completion.choices[0].message.content or "").strip()
    tool_call = _extract_json_payload(content)
    if not isinstance(tool_call, dict) or tool_call.get("name") != "computer_use":
        raise ReplayError(f"Expected tool call with name=computer_use, got: {tool_call}")
    args = tool_call.get("arguments")
    if not isinstance(args, dict) or "action" not in args:
        raise ReplayError(f"Tool call missing arguments.action: {tool_call}")
    return tool_call


def tool_call_to_pixel_action(image_path: Path, tool_call: dict[str, Any]) -> dict[str, Any]:
    """
    Convert a tool_call with 0..1000 coordinates into pixel coordinates for the given screenshot.
    Returns a dict with normalized fields:
      - action: str
      - keys/text/time/pixels optional
      - x_px/y_px for mouse actions when coordinate present
    """
    args = tool_call.get("arguments") or {}
    if not isinstance(args, dict):
        raise ReplayError(f"Invalid tool_call.arguments: {tool_call}")

    action = args.get("action")
    if not isinstance(action, str) or not action:
        raise ReplayError(f"Invalid tool_call.arguments.action: {tool_call}")

    out: dict[str, Any] = {"action": action}

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
