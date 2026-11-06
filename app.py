"""Run with: python app.py start --id classroom"""
import argparse
import json
import re
import sys
import time
import uuid
from datetime import datetime, timezone
from decimal import InvalidOperation
from pathlib import Path

from backends import DemoBackend, LiveBackend, action_key
from domain import DATA, execute, month, validate_call, validate_final
from console import fields, introduction, show_event, summary

RUNS = Path(__file__).resolve().parent / "runs"
TERMINAL = {"completed", "completed_without_report", "failed", "failed_validation", "cancelled"}
MAX_CALLS = 12


def save_state(folder, state):
    # Single-user teaching store. Atomic replace prevents half-written JSON.
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / "state.tmp"
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(folder / "state.json")


def emit(folder, event, **details):
    entry = {"at": datetime.now(timezone.utc).isoformat(), "type": "app." + event, **details}
    with (Path(folder) / "events.jsonl").open("a", encoding="utf-8") as log:
        log.write(json.dumps(entry) + "\n")
    show_event(event, details)


def new_state(identifier, mode="demo", current="2026-10", previous="2026-09"):
    month(current); month(previous)
    if current <= previous:
        raise ValueError("Current month must follow previous month")
    return {"id": identifier, "mode": mode, "status": "created",
            "creation_key": str(uuid.uuid4()), "session_id": None,
            "context": {"current_month": current, "previous_month": previous,
                        "currency": "EUR", "exclude_refunds": True},
            "results": {}, "tool_calls": 0, "pending": None, "published": False}


def drive(state, backend, folder, decision=None, wait_seconds=120, data_path=DATA):
    """Handle application work. The live model/tool loop stays on the service."""
    if state["status"] in TERMINAL:
        print("Run already ended:", state["status"])
        return state
    saved_pending = state.get("pending")
    approval_consumed = False
    deadline = time.monotonic() + wait_seconds
    if not state.get("session_id"):
        backend.create(state)
        state["status"] = "running"
        save_state(folder, state)
        emit(folder, "session.created", mode=state["mode"], session_id=state["session_id"])
    last_status = None
    while True:
        snapshot = backend.snapshot(state)
        status = snapshot["status"]
        if status != last_status:
            emit(folder, "progress.observed", status=status)
            last_status = status
        actions = snapshot.get("actions", [])
        for action in actions:
            if action.get("type") != "function_call":
                state["status"] = "waiting_for_external_input"
                save_state(folder, state)
                emit(folder, "external_input.required", action=action)
                return state
            key = action_key(action)
            cached = state["results"].get(key)
            if cached:
                if action != cached["action"]:
                    raise ValueError("Pending call identity was reused with different arguments")
                result = cached["result"]
                emit(folder, "tool.result_reused", call_id=action["call_id"])
            else:
                if state["tool_calls"] >= MAX_CALLS:
                    backend.cancel(state)
                    state["status"] = "cancelling"
                    save_state(folder, state)
                    emit(folder, "limit.reached", maximum_tool_calls=MAX_CALLS)
                    return state
                emit(folder, "tool.requested", action=action)
                try:
                    validate_call(action["name"], action["arguments"], state["context"])
                    approved = (decision == "approve" and not approval_consumed
                                and action == saved_pending)
                    rejected = (decision == "reject" and not approval_consumed
                                and action == saved_pending)
                    if action["name"] == "save_report" and not (approved or rejected):
                        state["pending"] = action
                        state["status"] = "waiting_for_approval"
                        save_state(folder, state)
                        emit(folder, "approval.required", action=action)
                        return state
                    if action["name"] == "save_report":
                        approval_consumed = True
                        emit(folder, "approval.decided", decision=decision, call_id=action["call_id"])
                    if rejected:
                        result = {"success": False, "error": "The user rejected saving this report. Do not retry."}
                    else:
                        emit(folder, "tool.started", name=action["name"])
                        output = execute(action["name"], action["arguments"], state, folder,
                                         approved=approved, data_path=data_path)
                        result = {"success": True, "output": json.dumps(output)}
                except (ValueError, PermissionError, OSError, KeyError, InvalidOperation) as exc:
                    result = {"success": False, "error": str(exc)}
                state["tool_calls"] += 1
                state["results"][key] = {"action": action, "result": result,
                                         "submission_key": str(uuid.uuid4())}
                # Persist business results BEFORE the network submission.
                save_state(folder, state)
                emit(folder, "tool.finished", name=action["name"], result=result)
            backend.submit(state, action, result)
            state["pending"] = None
            state["status"] = "running"
            save_state(folder, state)
            emit(folder, "tool.result_returned", call_id=action["call_id"], success=result["success"])
        if status == "completed":
            state["final_text"] = snapshot.get("output", "")
            try:
                state["verified_output"] = validate_final(state["final_text"], state, folder, data_path)
                state["status"] = "completed" if state["published"] else "completed_without_report"
            except (ValueError, OSError, KeyError, InvalidOperation) as exc:
                state["status"] = "failed_validation"
                state["validation_error"] = str(exc)
            state["pending"] = None
            if "remote_items" in state:
                (Path(folder)/"remote_items.json").write_text(json.dumps(state.pop("remote_items"), indent=2), encoding="utf-8")
            save_state(folder, state)
            emit(folder, "outcome.checked", status=state["status"], output=state.get("verified_output"),
                 error=state.get("validation_error"))
            return state
        if status in {"failed", "cancelled"}:
            state.update(status=status, pending=None, error=snapshot.get("error"))
            save_state(folder, state)
            emit(folder, "outcome.checked", status=status, error=state.get("error"))
            return state
        if time.monotonic() >= deadline:
            if state["status"] != "cancelling":
                state["status"] = "running"
            save_state(folder, state)
            emit(folder, "watch.ended", message="Remote work may continue; resume to check it")
            return state
        if not actions:
            time.sleep(1)


def main():
    parser = argparse.ArgumentParser(description="Lesson 3: sales-report assistant with managed-harness concepts")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("start", "resume", "status", "cancel"):
        p = sub.add_parser(command)
        p.add_argument("--id", required=True, help="Unique run identifier; letters, digits, - and _")
        if command in {"start", "resume"}:
            p.add_argument("--wait-seconds", type=int, default=120)
        if command == "start":
            p.add_argument("--mode", choices=["demo", "live"], default="demo")
            p.add_argument("--current", default="2026-10")
            p.add_argument("--previous", default="2026-09")
        if command == "resume":
            p.add_argument("--decision", choices=["approve", "reject"])
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,49}", args.id):
        parser.error("Use a run ID with 1 to 50 letters/digits/-/_")
    if getattr(args, "wait_seconds", 1) < 1:
        parser.error("--wait-seconds must be positive")
    folder = RUNS / args.id
    try:
        if args.command == "start":
            if folder.exists():
                raise ValueError("Run ID already exists; choose a new --id")
            # Check live configuration before creating any local run.
            backend = LiveBackend() if args.mode == "live" else DemoBackend()
            state = new_state(args.id, args.mode, args.current, args.previous)
            save_state(folder, state)
            introduction(state, args.command)
            emit(folder, "context.prepared", context=state["context"], mode=state["mode"])
        else:
            state = json.loads((folder/"state.json").read_text(encoding="utf-8"))
            introduction(state, args.command)
            if args.command == "status":
                fields({k: state.get(k) for k in
                        ("session_id", "pending", "verified_output", "validation_error")
                        if state.get(k) is not None})
                summary(state, folder)
                return 0
            backend = LiveBackend() if state["mode"] == "live" else DemoBackend()
        if args.command == "cancel":
            if state["status"] not in TERMINAL:
                if state.get("session_id"):
                    backend.cancel(state)
                    state["status"] = "cancelling" if state["mode"] == "live" else "cancelled"
                else:
                    state["status"] = "cancelled"
                state["pending"] = None
                save_state(folder, state)
                emit(folder, "cancellation.requested", status=state["status"])
        else:
            state = drive(state, backend, folder, getattr(args, "decision", None), args.wait_seconds)
        summary(state, folder)
        return 1 if state["status"] in {"failed", "failed_validation"} else 0
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        print("Error:", str(exc), file=sys.stderr)
        print("If this run already has state, resume the same --id after correcting the issue.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
