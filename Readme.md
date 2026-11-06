# Lesson 3 practical application: Sales Report Assistant

This command-line application compares two months of sales, prepares a report, pauses for a person's approval, and saves the report only after approval. It makes the responsibilities from **Inside the Managed Agent Harness** visible through JSON state files and progress messages.

There are two modes:

| Mode | What makes decisions? | Where does orchestration run? | Setup |
| --- | --- | --- | --- |
| `demo` (default) | A small deterministic simulator | Locally, for teaching | Python only; no key or network |
| `live` | An OpenAI model | The managed Agents API | OpenAI application API key and Agents API access |

The demo is an explicitly labelled simulation, not an implementation of OpenAI's private harness. Live mode calls `/v1/agents/sessions`; it does not substitute the Agents SDK or a hand-written model/tool loop. The application handles pending function calls while the service manages model decisions and continuation. [Official runtime comparison](https://developers.openai.com/api/docs/guides/agents).

## Purpose and lesson concepts

The business request is: **Compare October 2026 sales with September 2026 in EUR, excluding refunds, and save the report after approval.** The supplied fictional CSV contains EUR 1,000.00 in September sales and EUR 1,250.00 in October sales: a 25.00% increase. Refund rows are deliberately present to test the exclusion rule.

| Lesson slide | What to look for in the application |
| --- | --- |
| 3: managed loop | Analysis tool request → result → report tool request → result → final answer |
| 3A: responsibilities | `backends.py` supplies decisions/remote state; `domain.py` enforces application rules |
| 3B: context | Instructions, explicit month inputs, units, refund policy, and earlier tool results |
| 3C: tool execution | Tool allowlist, exact argument checks, fixed data source, and approval before writing |
| 3D: integration | Results are returned with the original `turn_id` and `call_id`; errors are returned as errors |
| 3E: continuation | The process exits while waiting; a later command reloads state and resumes the same session |
| 3F: observability | Progress log, persisted records, explicit outcomes, and independent final-output checks |

The context summary here is application-provided policy, not a demonstration of the service's internal automatic compaction. No sandbox is needed: application tools read the bundled CSV and write inside the selected run folder. Live sessions explicitly use `environment: {"type": "none"}`. [Environment configuration](https://developers.openai.com/api/docs/guides/agents-api/configuration).

## Requirements and configuration

Use **Python 3.11 or newer**. There are no third-party dependencies. In a terminal opened in this folder:

```powershell
python --version
python app.py --help
```

All examples use PowerShell, but the Python commands also work on macOS/Linux. If your system uses `python3`, substitute it for `python`.

No configuration is needed for demo mode. Data comes from `data/sales.csv`; runtime files go to `runs/<id>/`. Paths are resolved relative to the application, so running from another directory also works. The application does not read keys from neighboring lesson files.

For live mode, set these environment variables **in the same terminal**:

```powershell
$env:OPENAI_API_KEY = "your-application-api-key"
$env:OPENAI_MODEL = "your-supported-Agents-API-model-id"
```

On macOS/Linux:

```bash
export OPENAI_API_KEY="your-application-api-key"
export OPENAI_MODEL="your-supported-Agents-API-model-id"
```

Choose a model your project can use with the Agents API. No model is silently selected. Live requests are billable and transmit the task and tool results to OpenAI. The application API key needs `api.agents.read`, `api.agents.write`, and `api.responses.write`. Requests include the documented `OpenAI-Beta: agents=v1` header. [Agents API setup](https://developers.openai.com/api/docs/guides/agents-api/quickstart).

Environment variables are read directly; `.env` files are not loaded. Never put a key in the CSV, state files, or source code.

## Run the application

### 1. Start the offline classroom demonstration

```powershell
python app.py start --id classroom
```

You will see context preparation, the analysis request, validation, execution, and the result being returned. The next tool requests a report write. The program prints the exact pending arguments and exits with `waiting_for_approval`. At this point, `runs/classroom/report.md` does **not** exist.

```powershell
python app.py status --id classroom
```

Open `runs/classroom/state.json` to inspect the context, pending call, cached results, and application outcome. `events.jsonl` records each application event as one JSON line. These local event names begin with `app.` and are not claimed to be the service's streaming event names.

### 2. Approve and resume in a new process

```powershell
python app.py resume --id classroom --decision approve
```

The application reloads the run, rechecks the pending action, validates the saved analysis against the current CSV, writes `report.md`, returns the tool result, and validates the final answer. The outcome should become `completed`.

The final answer contains these checked values:

```json
{
  "report_id": "sales-2026-10-vs-2026-09",
  "currency": "EUR",
  "current_total": "1250.00",
  "previous_total": "1000.00",
  "change_pct": "25.00",
  "published": true
}
```

`published` means saved locally as `report.md`; the example sends no email and publishes nothing externally. Run identifiers must be unique, and an existing run cannot be overwritten by `start`.

### 3. Explore rejection and cancellation

```powershell
python app.py start --id rejected
python app.py resume --id rejected --decision reject

python app.py start --id cancelled
python app.py cancel --id cancelled
python app.py status --id cancelled
```

Rejection returns a failed tool result; the demo then finishes with `published: false` and outcome `completed_without_report`. Cancellation produces the separate outcome `cancelled`. Neither creates a report. A repeated `resume` on a terminal run does not repeat tool work.

### 4. Run against the managed Agents API

```powershell
python app.py start --id live-example --mode live
python app.py status --id live-example
python app.py resume --id live-example --decision approve
```

The CLI polls saved session state and turn records rather than consuming a streaming connection. Local polling messages are observations, not a replay of every server event. The saved `session_id` identifies the same remote session across processes. Pending tools come from `required_actions`, not historical function-call items. [Function tools and results](https://developers.openai.com/api/docs/guides/agents-api/tools/functions).

If the polling window ends while the service is still working, the status remains `running`. Continue watching with:

```powershell
python app.py resume --id live-example
```

Approval applies to the exact pending tool call displayed by that run. A changed or new report call needs another approval. Live model decisions and wording can vary. If the model returns an unexpected final format, the run becomes `failed_validation` rather than reporting unverified success.

`cancel` submits a remote cancellation request in live mode; the run remains `cancelling` until a later `resume` observes the turn's actual outcome. Ending the CLI or reaching a polling timeout does not itself stop remote inference.

## Project structure

```text
Practical Application/
  app.py                  CLI, approval workflow, state persistence, event log
  domain.py               Sales calculations, tool definitions, final validation
  backends.py             Offline simulator and managed Agents API REST adapter
  test_application.py     Offline integration and failure-path tests
  data/sales.csv          Fictional sales and refunds
  Readme.md               This student guide
  runs/                   Created when the application runs
```

## How the application was built

### Step 1: Define a small business task and prepare context

The CLI stores the comparison periods explicitly, rather than relying on a moving interpretation of “this month.” The instruction string in `domain.py` asks for tool-backed evidence and a final JSON object. Application-owned policy fixes EUR units and refund exclusion.

```python
context = {
    "current_month": args.current,
    "previous_month": args.previous,
    "currency": "EUR",
    "exclude_refunds": True,
}
```

The demo supports this defined comparison workflow; it does not interpret arbitrary natural-language tasks. You can select other months represented by the CSV with `--current YYYY-MM --previous YYYY-MM`.

### Step 2: Expose narrow tools and validate every request

`TOOL_DEFINITIONS` describes `analyze_sales` and `save_report`. JSON Schema helps the model form requests; Python validation remains the application's responsibility.

```python
if name == "analyze_sales":
    require_keys(arguments, {"current_month", "previous_month"})
    if arguments != {
        "current_month": context["current_month"],
        "previous_month": context["previous_month"],
    }:
        raise ValueError("Requested months do not match this run")
```

The model cannot choose a filesystem path, currency, or arbitrary function name. Monetary totals use `Decimal`, preserving cents. The application excludes refund rows before calculating totals. A missing month or a zero previous total is an explicit error, not a made-up percentage.

### Step 3: Keep the managed loop in the service

The live adapter creates a real managed session through a standard-library HTTP client:

```python
payload = {
    "agent": {"model": self.model, "instructions": INSTRUCTIONS,
              "tools": TOOL_DEFINITIONS},
    "environment": {"type": "none"},
    "input": task,
}
```

The CLI polls `/agents/sessions/{session_id}` and `/turns`, executes only pending application tools, and sends results through `/events`. It never calls a model directly in a local decision loop. The offline backend deliberately scripts the same sequence to make classroom results repeatable.

### Step 4: Return observations with their call identity

```python
event = {
    "type": "agent.session.input.tool_result",
    "turn_id": action["turn_id"],
    "call_id": action["call_id"],
    **result,
}
```

Success returns serialized JSON in `output`; failures return `success: false` and `error`. The harness can then reassess with the observation. The application saves results before submitting them. If the submission fails, a later resume reuses the saved result instead of repeating a completed operation.

### Step 5: Pause, persist, and bind approval to the action

Before `save_report` runs, the CLI saves its full pending action and exits. On resume, it retrieves the current pending calls and compares the saved action with the current one. Approval is consumed only for an identical match.

```python
approved = decision == "approve" and action == saved_pending
if action["name"] == "save_report" and not approved:
    state["pending"] = action
    state["status"] = "waiting_for_approval"
    save_state(folder, state)
    return state
```

`save_report` uses the previously verified analysis from application state. It recomputes the source totals to detect changed data, and writes a deterministic local report. It does not accept model-generated financial figures or a model-provided output path. Completed results are cached by turn/call ID, and existing reports are reused only when their contents match.

### Step 6: Verify outcomes separately from progress

```python
actual = json.loads(final_text)
expected = final_payload(state)
if actual != expected:
    raise ValueError("Final answer does not match verified tool evidence")
```

The validator also checks the source data and report contents. It rejects missing evidence, extra fields, wrong numbers, and inconsistent publication status. A remotely completed turn and a locally verified business result are separate checks. `idle` by itself never means success. [Session outcomes](https://developers.openai.com/api/docs/guides/agents-api/sessions).

`events.jsonl` is useful for explanation; `state.json`, saved source evidence, and `report.md` determine the application's checked result. Raw live items are saved as `remote_items.json` for inspection, including paginated history. [Events and saved items](https://developers.openai.com/api/docs/guides/agents-api/sessions/events).

## Verify your installation

```powershell
python -m unittest -v
```

Tests use temporary directories and a scripted HTTP boundary. They cover separate-process resume, approval, rejection, cancellation, unknown tools, altered arguments, source changes, invalid final output, result reuse, and live request/response handling. They make no paid API calls.

## Exercises for students

1. Start a run and inspect its files before approval. Which components have already done work, and which action is still pending?
2. Add a refund row. Why should the report's sales total remain unchanged?
3. Change a sales amount after the approval pause, then resume. Explain why the application rejects stale evidence.
4. Change the simulated final total in `DemoBackend.snapshot`. Explain why a completed simulated turn still fails validation.
5. Add a read-only tool with its schema and Python validator. Identify which layer executes it in each mode.
6. In live mode, inspect `remote_items.json` and compare tool calls with the local application event log.

## Troubleshooting and teaching boundaries

| Symptom | What to do |
| --- | --- |
| Run ID already exists | Pick a new `--id`; previous runs are preserved |
| Waiting for approval | Inspect the pending action, then resume with `--decision approve` or `reject` |
| Missing live configuration | Set `OPENAI_API_KEY` and `OPENAI_MODEL` in the current terminal |
| HTTP 401/403/404 | Check the application key, permissions, Agents API access, and model availability |
| Network or timeout error | State is preserved; resume the same ID rather than starting another remote session |
| `failed_validation` | Inspect the final text and tool evidence; do not treat it as a verified report outcome |
| Source-data change | Start a fresh run to analyze the updated CSV |

This is a single-user classroom application. Run one command per run ID at a time; local JSON files are trusted and there is no multi-process locking or authenticated approval service. Application tool calls are capped at 12, and each watch window is bounded by `--wait-seconds` (default 120). This tool-call cap is not a remote token or spending limit. A live limit violation requests cancellation; remote cancellation confirmation still depends on the service.

Session creation uses a stored idempotency key, and tool-result submissions use stable per-call keys. No automatic network retries are hidden in the HTTP adapter. After a transport failure, resume explicitly. The service manages its own session history and context; a local file is not unlimited model memory.

To move beyond the classroom, add authenticated users and approvals, transactional storage, concurrency control, stronger job recovery, and service-side usage policies. The example intentionally keeps those concerns visible and small enough to study.
