# Architecture
The app comprises of 4 steps:
- Record: when the user records a task
- Reflect: we convert the task into a reusable parametrized schema
- Edit Workflow: to edit the workflow schema granularly, update parameters, add subtask dependency
- Replay: We update the parametrized schema with task parameters and rerun the task.

## Record:
- Tracks keyboard and mouse inputs (e.g., click, type “spotify”, press Enter, press Esc). We call these events.
- Captures a screenshot at the start, and then before each event.
- After every event, saves a step as (pre-action screenshot, action). The screenshot represents the screen state right before the action; next event's screenshot reflects the screen state after that action.
- Stores the full task as an ordered list of these (screenshot, action) steps, which is later used for replay. The number of steps is roughly equal to the number of events.
- During recording, **Ctrl + I** can be used to pause and add extra structured context at the right moment:
    - **Extract**: capture the current pre-action screenshot and store an `extract` event (query + optional candidate values) to be used later during replay. This can be used to extract information from a specific page in the workflow.
    - **Details**: attach a free-form “details” note onto the **next** event/step (useful when the intent/target is hard to infer from coordinates alone) or adding useful context to the step.

Example [manifest.jsonl](manifest.jsonl)


## Reflect:
- Given a list of (pre-action screenshot, action), we do multiple LLM passes to make it a parametrized schema.

We made use of gpt-5-mini for both LLM Pass A and B

LLM Pass A: For each: (preaction screenshot, action, post-action screenshot), we compute:
```
    - expected_current_state: expected screen on which action is to be taken
    - intent: intent of the action
    - target: description of the target element on the UI
    - post_action: UI changes after action
    - action_value: song name/enter key etc
```

LLM Pass B: all the steps are appended together from LLM Pass A to compute:

    - detailed_task_description
    - subtasks: list of subtasks
    - task_params: parameters of the task
    - steps:
        - paraetried action value
        - subtask

    Task -> Subtask 1 -> Step 1
                      -> Step 2
                      -> Step 3
         -> Subtask 2 -> Step 4
                      -> Step 5
                      -> Step 6

For example for a task like:
```
task="play numb on spotify on mac"

subtask1="Open Spotify using Spotlight and bring it to the foreground"
    step1=Enter the search query "spotify" into the Spotlight search field
    step2=Click on spotify from the available results

subtask2="Search for and play the song {song_name} within Spotify"
    step3=Clear the current text in the search field
    step4=Enter the song name into the search field
    step5=Start playback of the top search result song ....
```
An example final schema is present here: [spotify_example](schema.json)

## Edit Workflow:
- Reflection produces a good first draft, but workflows become more reliable when we can edit the schema granularly.
- In this step we use the Workflow Editor to:
    - **Update parameters** (`schema.json.task_params`): add/delete params and tune example values so templated strings like `{song_name}` are correct and reusable.
    - **Update subtasks and steps** (`schema.json.plan.subtasks[*]`): edit subtask text (expected outcome), add/delete subtasks, and edit step intent/action fields for clarity.
    - **Add dependencies** (`subtasks[*].dependencies`): define a dependency graph between subtasks by referencing upstream `EXTRACT` variable names. This allows later subtasks to consume results from earlier subtasks.
    - **Review the workflow completely**: ensure subtasks are in the right order, dependencies only point to upstream extracts, and each subtask has enough step examples to ground the replay model.

## Replay:
- When the user clicks on a workflow to replay, we get the fixed set of parameters from the user
- We then replace the parameters in the schema.json
- We run task as sequence of subtasks. We pass subtask and expected output to the computer use model to achieve. We pass the steps as reference examples to do the task. Following is a pseudocode for running the task:
```
    for subtask in subtasks:
        screenshot = take_screenshot()
        action = predict_action(user_query)
        if action== done:
            continue
        else:
            execute_action()
```
**done** function is called by the llm when the expected subtask result is achieved. It is used to indicate the completion of the current subtask.

in **predict_action**, we build a compact **user_query** prompt (plus the current screenshot) that contains:
- Overall task name
- Current subtask text **and expected outcome**
- Resolved params (from `schema.json.task_params` + user overrides)
- Current `task_memory` (cross-subtask state carried forward)
- Recent `history` for this subtask (last few actions/observations) for this **subtask** only
- `reference_steps` examples derived from `schema.plan.steps` for this **subtask** only
- **Dependency-driven extracts**: if a subtask declares dependencies (from “Edit Workflow”), replay injects the upstream extract values as additional context for the current subtask. This allows results of one subtask (e.g., an extracted restaurant name/address) to be used reliably in later subtasks without re-extracting or re-searching.

**Required output of `predict_action`** (exactly one tool call):
- **action**: either a `computer_use` OS action (mouse/keyboard/etc.) or `done`
- **observation**: a step-specific, current-screen observation to justify the action/done
- **memory**: an updated `task_memory` string to carry forward to the next iteration/subtask

We made use of qwen-3-vl-plus model for predict_action.
