---
name: fetch-weather-report
description: Fetch the current weather forecast for a specified location and return a structured summary.
---

# Fetch Weather Report Skill

## Inputs
- `location` (required, string): The city and state/country to search weather for (e.g. "San Francisco, CA").
- `units` (optional, string): The unit system to use, either "metric" or "imperial". Default is "metric".

## Run
Run via the executable bash script:
```bash
./run.sh [path/to/inputs.json]
```

Python runtime contract:
- `run.sh` uses the first available interpreter in this order: skill `.venv/bin/python`, workflow `.venv/bin/python`, then required `$AI_MIME_PYTHON_PATH`.
- If `requirements.txt` exists, include these exact build/repair commands for the developer to set up the virtualenv before packaging or for manual troubleshooting:
    ```bash
    "$AI_MIME_UV_PATH" venv .venv --python "$AI_MIME_PYTHON_PATH"
    "$AI_MIME_UV_PATH" pip install -r requirements.txt --python .venv/bin/python
    ```
- State clearly that the install commands are for skill build or manual repair. The automated runtime does not create or repair `.venv` when executing the skill.

## Outputs
- `weather_summary` (dict):
  - `location` (string): Resolved location name.
  - `temperature` (float): Current temperature.
  - `condition` (string): Weather condition description.

## Progress logs
The script outputs progress logs on `stderr` to track execution progress.
All logs must be written in clear, natural language suitable for an end-user overlay. Do not use structured JSON logs.
- "Fetching weather from API..."
- "It is sunny with 18.5 C"
- "Error: API timeout"

## Fallback
If the weather API fails or is unreachable, the execution falls back to performing a Google search for current weather and scraping the temperature using `browser_harness`. See `references/fallback_plan.md` for manual or automated fallback instructions.

## ask_llm decision points
1. **Weather Condition Parsing**:
   If the weather condition string returned by the API is fuzzy, the script calls `ask_llm` to categorize the weather condition into standard types ("Sunny", "Cloudy", "Rainy", "Snowy", "Unknown").
   ```python
   from llm_resolver import ask_llm
   decision = ask_llm(
       prompt=f"Categorize this weather description: '{raw_desc}'",
       schema={
           "type": "object",
           "properties": {
               "category": {"type": "string", "enum": ["Sunny", "Cloudy", "Rainy", "Snowy", "Unknown"]}
           },
           "required": ["category"]
       }
   )
   ```

## References
- [fallback_plan.md](references/fallback_plan.md): Step-by-step instructions for human/UI agent fallback execution.
