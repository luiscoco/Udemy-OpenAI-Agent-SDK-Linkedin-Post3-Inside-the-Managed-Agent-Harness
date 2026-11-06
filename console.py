"""Plain-text classroom presentation; events.jsonl remains machine-readable."""
import json


def section(title, explanation):
    print(f"\n{title}\n{'-' * len(title)}\n{explanation}", flush=True)


def fields(values):
    for key, value in values.items():
        label = key.replace("_", " ").capitalize()
        if isinstance(value, bool):
            value = "yes" if value else "no"
        elif isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=True)
        print(f"  {label}: {value}", flush=True)


def introduction(state, command):
    section("SALES REPORT ASSISTANT | Lesson 3", "Context -> tool request -> execution -> feedback -> approval -> verification")
    fields({"run": state["id"], "mode": state["mode"]})
    if state["mode"] == "demo":
        print("  Demo: a scripted local simulator; no model or API calls.")
    else:
        print("  Live: the service manages model decisions; this app executes application tools.")
    if command == "resume":
        section("CONTINUATION | Reload saved state", "A new process resumes the same session using state.json.")
        fields({"session": state.get("session_id"), "saved_status": state["status"]})


def show_event(event, details):
    if event == "context.prepared":
        section("1. CONTEXT | Define the task", "The application supplies the comparison and business rules.")
        fields(details["context"])
    elif event == "session.created":
        section("2. SESSION | Begin the workflow", "The session ID connects this run to later continuation.")
        fields({"session_id": details["session_id"]})
    elif event == "progress.observed":
        print(f"\n  Observed session/turn status: {details['status']} (progress alone does not prove success).", flush=True)
    elif event == "tool.requested":
        section("3. TOOL REQUEST | Inspect the requested action", "The backend requests a function. The application checks its name and arguments.")
        fields(details["action"])
    elif event == "tool.started":
        section("4. EXECUTION | Application runs the tool", "The request passed validation. Python performs the business operation.")
        fields(details)
    elif event == "tool.finished":
        result = details["result"]
        print(f"\n  Tool result: {'SUCCESS' if result['success'] else 'ERROR'}", flush=True)
        if result["success"]:
            # Decode only the display copy; the stored tool result is unchanged.
            try:
                output = json.loads(result["output"])
            except (ValueError, TypeError):
                output = result["output"]
            fields(output if isinstance(output, dict) else {"output": output})
        else:
            fields({"error": result["error"]})
    elif event == "tool.result_returned":
        section("5. FEEDBACK | Return the observation", "The result is returned with the original call ID so the backend can continue.")
        fields(details)
    elif event == "approval.required":
        section("6. HUMAN APPROVAL | Pause before saving", "The application saved the pending action and is waiting for your decision.")
        fields(details["action"])
        print("  This pending write has not been executed. Approval applies to this exact call.")
    elif event == "approval.decided":
        section("6. HUMAN APPROVAL | Apply the decision", "The saved pending call matches the current request.")
        fields(details)
    elif event == "outcome.checked":
        section("7. OUTCOME | Check the evidence", "For a completed turn, the app independently validates the final answer against tool evidence and report contents.")
        fields({"status": details["status"]})
        if details.get("output"):
            fields(details["output"])
        if details.get("error"):
            fields({"error": details["error"]})
    elif event == "tool.result_reused":
        section("RECOVERY | Reuse a saved result", "The application returns the cached result without repeating the tool execution.")
        fields(details)
    else:
        section(event.replace(".", " ").upper(), "Application observation")
        fields(details)


def summary(state, folder):
    section("RUN SUMMARY", "Inspect the saved files to connect the console walkthrough to the evidence.")
    fields({"run": state["id"], "mode": state["mode"], "outcome": state["status"]})
    print(f"\n  Files: {folder}\n  state.json   : saved context, pending action, and results\n  events.jsonl : timestamped JSON event log", flush=True)
    if state.get("published"):
        print("  report.md    : saved sales report")
    if state["status"] == "waiting_for_approval":
        print("\n  Next: review the pending action above, then choose one command:")
        print(f"    python app.py resume --id {state['id']} --decision approve")
        print(f"    python app.py resume --id {state['id']} --decision reject")
        print("\n  Teaching prompt: What has already run, and which operation still needs approval?")
    elif state["status"] in {"running", "cancelling"}:
        print(f"\n  Check continuation: python app.py resume --id {state['id']}")
