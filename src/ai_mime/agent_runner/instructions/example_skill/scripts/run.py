import argparse
import sys
import json
import os
import shlex
import subprocess

def log(msg):
    print(msg, file=sys.stderr, flush=True)

def run_browser_harness_step(step_title, script_code):
    """Reference helper showing how to shell out to browser-harness for browser automation.
    
    IMPORTANT: Inner browser harness scripts should write their progress logs to sys.stderr 
    (e.g., `import sys; print(msg, file=sys.stderr, flush=True)`) so they bypass the stdout buffer and 
    stream directly to the UI overlay in real-time.
    """
    log(step_title)

    harness_bin = os.environ.get("AI_MIME_BROWSER_HARNESS_BIN")
    if not harness_bin:
        log("Error: AI_MIME_BROWSER_HARNESS_BIN not configured")
        sys.exit(1)

    cmd = [harness_bin, "-c", script_code]
    try:
        # Run browser script in subprocess. Only capture stdout; stderr streams directly to UI.
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, check=True)
        # Parse return value or page info from stdout if needed
        log("Completed: " + step_title)
    except subprocess.CalledProcessError as e:
        log(f"Error: Browser harness failed with exit code {e.returncode}")
        sys.exit(1)

def run_ui_agent_step(step_title, task_prompt, response_schema=None):
    """Reference helper showing how to shell out to the UI Agent for native macOS automation.

    Pass the high-level step-by-step instructions you recorded during Phase B exploration
    directly as the natural language task prompt. The UI Agent will drive the exact same
    cua MCP server to execute them. Optionally pass a response_schema dict to enforce
    structured JSON output.
    """
    log(step_title)

    ui_agent_cmd = os.environ.get("AI_MIME_UI_AGENT_CMD")
    if not ui_agent_cmd:
        log("Error: AI_MIME_UI_AGENT_CMD not configured")
        sys.exit(1)

    # Split the command string (handles both bare executable or prefixed python -m calls)
    cmd = shlex.split(ui_agent_cmd) + [task_prompt]
    if response_schema:
        cmd += ["--schema", json.dumps(response_schema)]
    cmd += ["--json"]

    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, text=True, check=True)
        result = json.loads(proc.stdout)

        if result.get("success"):
            summary = result.get("summary", "")
            if summary:
                log(f"Completed: {summary}")
            return result.get("result_json") or {}
        else:
            log("Error: " + (result.get("error") or "UI Agent task failed"))
            sys.exit(1)
    except subprocess.CalledProcessError as e:
        log(f"Error: UI Agent command failed to run: {e}")
        sys.exit(1)
    except json.JSONDecodeError:
        log("Error: UI Agent returned invalid JSON output")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description="Fetch weather report skill")
    parser.add_argument("--inputs-json", required=True, help="Path to inputs JSON file")
    args = parser.parse_args()

    # Load and parse input values
    try:
        with open(args.inputs_json, "r", encoding="utf-8") as f:
            inputs = json.load(f)
    except Exception as e:
        print(f"Error reading inputs: {e}", file=sys.stderr)
        sys.exit(1)

    location = inputs.get("location")
    units = inputs.get("units", "metric")

    if not location:
        print("Missing required input: location", file=sys.stderr)
        sys.exit(1)

    # --- 1. Pure Python step example ---
    log("Preparing query parameters...")
    # Pure Python logic: simple, fast, robust. Preferred executor path.
    param_units = "C" if units == "metric" else "F"
    log("Prepared parameters.")

    # --- 2. Browser Harness (CDP) step example (Commented out reference) ---
    # cdp_script = f"browser.goto('https://weather.com'); browser.type('input[name=search]', '{location}'); browser.press('Enter');"
    # run_browser_harness_step("Get Weather from Website...", cdp_script)

    # --- 3. UI Agent (Mac native / cua) step example ---
    # To execute native-UI steps, we pass a high-level description of actions to AI_MIME_UI_AGENT_CMD.
    # Note: Avoid writing fragile coordinates in prompts unless strictly required; high-level step descriptions are preferred.
    ui_task_prompt = (
        f"In the open Weather application on the Mac:\n"
        f"1. Find and click on the search bar or text input.\n"
        f"2. Type '{location}' and press the Enter key.\n"
        f"3. Wait for the screen to refresh and show current conditions."
    )
    # Define an optional schema dict to guarantee structured JSON output from the UI Agent
    ui_task_schema = {
        "type": "object",
        "properties": {
            "temperature": {"type": "number"},
            "condition": {"type": "string"}
        },
        "required": ["temperature", "condition"]
    }

    # Run the UI Agent to perform the native actions autonomously (using the response schema)
    # result_data = run_ui_agent_step(
    #     "Input Weather Details in App...",
    #     ui_task_prompt,
    #     response_schema=ui_task_schema
    # )

    # Mock the final summary result for validation test matching
    weather_summary = {
        "location": location,
        "temperature": 21.0 if units == "metric" else 69.8,
        "condition": "Sunny"
    }

    # Signal successful completion of the overall workflow by printing result to stdout
    log("Successfully fetched weather data.")
    print(json.dumps({
        "event": "workflow_done",
        "outputs": {"weather_summary": weather_summary}
    }, ensure_ascii=False), flush=True)

if __name__ == "__main__":
    main()
