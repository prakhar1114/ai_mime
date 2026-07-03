import asyncio
import json
import os
import re
from pathlib import Path

import httpx
from mcp.server import Server
import mcp.server.stdio
from mcp.types import Tool, TextContent

from ai_mime.app_data import get_workflows_dir
from ai_mime.editor.server import EDITOR_SERVER_PORT

app = Server("ai-mime-mcp")

def parse_skill_frontmatter_fields(skill_dir: Path) -> dict[str, str]:
    skill_md = skill_dir / "SKILL.md"
    try:
        text = skill_md.read_text(encoding="utf-8")
    except Exception:
        return {}
    m = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text, re.DOTALL)
    if not m:
        return {}
    out = {}
    for line in m.group(1).splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        out[key.strip()] = value.strip().strip("\"'")
    return out

def find_built_skill_dir(workflow_dir: Path) -> Path | None:
    skills_root = workflow_dir / "skills"
    if not skills_root.is_dir():
        return None
    candidates = []
    for child in skills_root.iterdir():
        run_sh = child / "run.sh"
        if child.is_dir() and run_sh.is_file() and os.access(run_sh, os.X_OK):
            candidates.append(child)
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0]

def build_json_schema(template: dict, examples: dict) -> dict:
    properties = {}
    required = []
    
    for key, value in template.items():
        if isinstance(value, dict):
            prop = build_json_schema(value, examples.get(key, {}))
            properties[key] = prop
        elif isinstance(value, list):
            prop = {"type": "array", "items": {"type": "string"}}
            properties[key] = prop
        else:
            prop = {"type": "string", "description": str(value)}
            if key in examples:
                prop["examples"] = [examples[key]]
            properties[key] = prop
        required.append(key)
        
    return {
        "type": "object",
        "properties": properties,
        "required": required
    }


@app.list_tools()
async def handle_list_tools() -> list[Tool]:
    workflows_root = get_workflows_dir()
    
    if not workflows_root.exists() or not workflows_root.is_dir():
        return []
        
    workflows = []
    for child in workflows_root.iterdir():
        if child.is_dir() and child.name != ".agent":
            workflows.append(child)
            
    workflows.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    
    tools_by_skill = {}
    
    for child in workflows:
        skill_dir = find_built_skill_dir(child)
        if not skill_dir:
            continue
            
        fields = parse_skill_frontmatter_fields(skill_dir)
        skill_name = fields.get("name", skill_dir.name)
        
        if skill_name in tools_by_skill:
            continue
            
        description = fields.get("description", f"AI Mime Skill: {skill_name}")
        description += f"\nSkill directory, access ONLY if needed to inspect or run directly: {skill_dir.absolute()}"
        
        template_path = skill_dir / "inputs" / "inputs.template.json"
        example_path = skill_dir / "inputs" / "inputs.example.json"
        
        try:
            if template_path.exists():
                template_data = json.loads(template_path.read_text(encoding="utf-8"))
            else:
                template_data = {}
        except Exception:
            template_data = {}
            
        try:
            if example_path.exists():
                example_data = json.loads(example_path.read_text(encoding="utf-8"))
            else:
                example_data = {}
        except Exception:
            example_data = {}
            
        input_schema = build_json_schema(template_data, example_data)
        
        tools_by_skill[skill_name] = Tool(
            name=skill_name,
            description=description,
            inputSchema=input_schema
        )
        
    return list(tools_by_skill.values())

@app.call_tool()
async def handle_call_tool(name: str, arguments: dict | None) -> list[TextContent]:
    if arguments is None:
        arguments = {}
        
    url = f"http://localhost:{EDITOR_SERVER_PORT}/api/skills/{name}/run/stream"
    
    final_output = None
    stderr_logs = []
    
    try:
        async with httpx.AsyncClient() as client:
            async with client.stream("POST", url, json={"params": arguments}, timeout=None) as response:
                if response.status_code != 200:
                    error_text = await response.aread()
                    return [TextContent(type="text", text=f"Error starting task: {response.status_code} {error_text.decode('utf-8')}")]
                
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                        
                    data_str = line[len("data: "):].strip()
                    if not data_str:
                        continue
                        
                    try:
                        event = json.loads(data_str)
                        ev_type = event.get("event")
                        
                        if ev_type == "stderr":
                            stderr_logs.append(event.get("line", ""))
                        elif ev_type == "done":
                            success = event.get("success")
                            if success:
                                final_output = event.get("outputs", {})
                                return [TextContent(type="text", text=json.dumps(final_output, indent=2))]
                            else:
                                combined_log = event.get("combined_log", "\n".join(stderr_logs))
                                return [TextContent(type="text", text=f"Task failed. Logs:\n{combined_log}")]
                                
                        elif ev_type == "error":
                            message = event.get("message", "Unknown error")
                            return [TextContent(type="text", text=f"Task error: {message}")]
                            
                    except json.JSONDecodeError:
                        continue
                        
    except httpx.RequestError as exc:
        return [TextContent(type="text", text=f"An error occurred while requesting {exc.request.url!r}. Make sure the AI Mime app is running!")]

    return [TextContent(type="text", text="Stream ended without completion signal.")]


async def tool_watcher():
    workflows_root = get_workflows_dir()
    last_mtime = 0
    
    while True:
        try:
            if workflows_root.exists():
                current_mtime = workflows_root.stat().st_mtime
                if current_mtime > last_mtime:
                    if last_mtime > 0:
                        # Send notification to clients to refresh tools
                        await app.request_context.session.send_tool_list_changed()
                    last_mtime = current_mtime
        except Exception:
            pass
        await asyncio.sleep(5)

async def main():
    # Start the background watcher task
    asyncio.create_task(tool_watcher())
    
    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await app.run(
            read_stream,
            write_stream,
            app.create_initialization_options()
        )

def cli_main():
    # Configure logger to write to stderr so it doesn't break JSON-RPC
    import logging
    import sys
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    asyncio.run(main())

if __name__ == "__main__":
    cli_main()
