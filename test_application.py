"""Meaningful offline checks. Run: python -m unittest -v"""
import contextlib
import io
import json
import subprocess
import sys
import shutil
import unittest
import uuid
from pathlib import Path

from app import drive, new_state, save_state
from backends import DemoBackend, LiveBackend, action_key
from domain import DATA, analyze, execute, final_payload, validate_final


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        # Keep temporary writes inside the project under restricted IDE sandboxes.
        root = Path(__file__).resolve().parent
        self.folder = (root / ("test-run-" + uuid.uuid4().hex)).resolve()
        assert self.folder.is_relative_to(root)
        self.folder.mkdir()
        self.addCleanup(self.remove_test_folder)
        self.state = new_state("test")
        save_state(self.folder, self.state)

    def remove_test_folder(self):
        root = Path(__file__).resolve().parent
        target = self.folder.resolve()
        if target.parent != root or not target.name.startswith("test-run-"):
            raise ValueError("Refusing cleanup outside the test directory")
        shutil.rmtree(target)

    def run_work(self, backend=None, decision=None, data_path=DATA):
        with contextlib.redirect_stdout(io.StringIO()):
            return drive(self.state, backend or DemoBackend(), self.folder, decision,
                         wait_seconds=2, data_path=data_path)

    def test_pause_then_approve_after_reload(self):
        self.run_work()
        self.assertEqual(self.state["status"], "waiting_for_approval")
        self.assertFalse((self.folder/"report.md").exists())
        self.state = json.loads((self.folder/"state.json").read_text())
        self.run_work(decision="approve")
        self.assertEqual(self.state["status"], "completed")
        self.assertEqual(self.state["verified_output"]["change_pct"], "25.00")
        self.assertIn("1250.00", (self.folder/"report.md").read_text())

    def test_rejection_returns_error_without_writing(self):
        self.run_work()
        self.run_work(decision="reject")
        self.assertEqual(self.state["status"], "completed_without_report")
        self.assertFalse((self.folder/"report.md").exists())
        result = self.state["results"][action_key(self.state["demo_observations"][-1]["action"])]
        self.assertFalse(result["result"]["success"])

    def test_cancel_is_distinct_from_completion(self):
        self.run_work()
        DemoBackend().cancel(self.state)
        self.run_work()
        self.assertEqual(self.state["status"], "cancelled")
        self.assertFalse((self.folder/"report.md").exists())

    def test_repeated_resume_does_not_repeat_tools(self):
        self.run_work(); self.run_work(decision="approve")
        calls = self.state["tool_calls"]
        self.run_work(decision="approve")
        self.assertEqual(self.state["tool_calls"], calls)

    def test_source_change_during_pause_blocks_report(self):
        source = self.folder/"sales.csv"
        source.write_text(DATA.read_text())
        self.run_work(data_path=source)
        source.write_text(source.read_text().replace("450.00", "451.00"))
        self.run_work(decision="approve", data_path=source)
        self.assertFalse((self.folder/"report.md").exists())
        self.assertEqual(self.state["status"], "failed_validation")

    def test_approval_is_bound_to_original_call(self):
        self.run_work()
        class ChangedCall(DemoBackend):
            def snapshot(self, state):
                snapshot = super().snapshot(state)
                for action in snapshot["actions"]:
                    action["call_id"] = "a-different-call"
                return snapshot
        self.run_work(ChangedCall(), decision="approve")
        self.assertEqual(self.state["status"], "waiting_for_approval")
        self.assertFalse((self.folder/"report.md").exists())
        self.assertEqual(self.state["pending"]["call_id"], "a-different-call")

    def test_submission_failure_reuses_saved_execution(self):
        self.run_work()
        class LostResponse(DemoBackend):
            def submit(self, state, action, result):
                raise RuntimeError("Simulated disconnect before submission")
        with self.assertRaises(RuntimeError):
            self.run_work(LostResponse(), decision="approve")
        self.assertTrue((self.folder/"report.md").exists())
        self.state = json.loads((self.folder/"state.json").read_text())
        calls = self.state["tool_calls"]
        self.run_work()
        self.assertEqual(self.state["status"], "completed")
        self.assertEqual(self.state["tool_calls"], calls)

    def test_unknown_tool_returns_failed_observation(self):
        class UnknownTool(DemoBackend):
            def snapshot(self, state):
                snapshot = super().snapshot(state)
                if snapshot["actions"]:
                    snapshot["actions"][0]["name"] = "delete_files"
                return snapshot
        self.run_work(UnknownTool())
        first = next(iter(self.state["results"].values()))
        self.assertFalse(first["result"]["success"])
        self.assertEqual(self.state["status"], "failed_validation")

    def test_zero_baseline_and_missing_month_are_errors(self):
        source = self.folder/"zero.csv"
        source.write_text("order_id,date,kind,amount,currency\na,2026-09-01,sale,0.00,EUR\nb,2026-10-01,sale,10.00,EUR\n")
        with self.assertRaisesRegex(ValueError, "zero"):
            analyze("2026-10", "2026-09", source)
        with self.assertRaisesRegex(ValueError, "Both months"):
            analyze("2026-11", "2026-09", source)

    def test_malicious_arguments_and_unapproved_write_rejected(self):
        for name, args in [("read_anything", {}), ("save_report", {"report_id": "../../secrets"}),
                           ("analyze_sales", {"current_month": "2026-11", "previous_month": "2026-09"}),
                           ("analyze_sales", {"current_month": "2026-10", "previous_month": "2026-09", "path": "secret"})]:
            with self.subTest(name=name, args=args), self.assertRaises(ValueError):
                execute(name, args, self.state, self.folder)
        with self.assertRaises(PermissionError):
            execute("save_report", {"report_id": "sales-2026-10-vs-2026-09"}, self.state, self.folder)

    def test_wrong_final_answer_is_not_verified(self):
        self.run_work(); self.run_work(decision="approve")
        wrong = final_payload(self.state)
        wrong["current_total"] = "999999.00"
        with self.assertRaisesRegex(ValueError, "evidence"):
            validate_final(json.dumps(wrong), self.state, self.folder)
        (self.folder/"report.md").write_text("tampered")
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            validate_final(json.dumps(final_payload(self.state)), self.state, self.folder)

    def test_tool_budget_requests_cancellation(self):
        self.state["tool_calls"] = 12
        self.run_work()
        self.assertEqual(self.state["status"], "cancelling")
        self.run_work()
        self.assertEqual(self.state["status"], "cancelled")

    def test_cli_resume_in_separate_process(self):
        # Override only the storage root; use the actual argument parser and CLI.
        code = "import app; from pathlib import Path; app.RUNS=Path(%r); raise SystemExit(app.main())" % str(self.folder)
        root = Path(__file__).resolve().parent
        for args in [("start", "--id", "process"), ("resume", "--id", "process", "--decision", "approve")]:
            result = subprocess.run([sys.executable, "-c", code, *args], cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        state = json.loads((self.folder/"process"/"state.json").read_text())
        self.assertEqual(state["status"], "completed")


class LiveContractTests(unittest.TestCase):
    """Exercise real adapter wiring against documented JSON-shaped responses."""
    def test_creation_tool_result_and_pagination(self):
        calls = []
        def request(method, path, payload=None, idempotency_key=None):
            calls.append((method, path, payload, idempotency_key))
            if path == "/agents/sessions":
                return {"id": "session-1"}
            if "/items?" in path:
                if "after=" not in path:
                    return {"data": [{"id": "first", "type": "function_call"}],
                            "has_more": True, "last_id": "first"}
                return {"data": [{"type": "message", "role": "assistant", "phase": "final_answer",
                                   "content": [{"type": "output_text", "text": '{"checked":true}'}]}], "has_more": False}
            if "/turns?" in path:
                return {"data": [{"status": "completed", "subagent_id": None}], "has_more": False}
            if method == "GET":
                return {"status": "idle", "required_actions": []}
            return {}
        backend = LiveBackend(request=request)
        backend.model = "test-model"
        state = new_state("live", mode="live")
        backend.create(state)
        payload = calls[0][2]
        self.assertEqual(payload["environment"], {"type": "none"})
        self.assertEqual(payload["agent"]["model"], "test-model")
        self.assertEqual(calls[0][3], state["creation_key"])
        snapshot = backend.snapshot(state)
        self.assertEqual(snapshot["output"], '{"checked":true}')
        self.assertEqual(len(state["remote_items"]), 2)
        action = {"turn_id": "turn-1", "call_id": "call-1"}
        state["results"][action_key(action)] = {"submission_key": "stable-key"}
        backend.submit(state, action, {"success": False, "error": "Rejected"})
        event = calls[-1][2]["events"][0]
        self.assertEqual(event, {"type": "agent.session.input.tool_result", "turn_id": "turn-1",
                                 "call_id": "call-1", "success": False, "error": "Rejected"})
        self.assertEqual(calls[-1][3], "stable-key")
        backend.cancel(state)
        self.assertEqual(calls[-1][2]["events"][0]["type"], "agent.session.input.cancel")

    def test_pending_calls_come_from_session_not_history(self):
        action = {"type": "function_call", "turn_id": "turn", "call_id": "call",
                  "name": "analyze_sales", "arguments": {"current_month": "2026-10", "previous_month": "2026-09"}}
        def request(method, path, payload=None, idempotency_key=None):
            self.assertNotIn("/items", path)
            return {"status": "requires_action", "required_actions": [action]}
        snapshot = LiveBackend(request=request).snapshot({"session_id": "session"})
        self.assertEqual(snapshot["actions"], [action])

    def test_idle_session_does_not_mean_completed(self):
        def request(method, path, payload=None, idempotency_key=None):
            if "/turns?" in path:
                return {"data": [{"status": "waiting", "subagent_id": None}], "has_more": False}
            return {"status": "idle", "required_actions": []}
        snapshot = LiveBackend(request=request).snapshot({"session_id": "session"})
        self.assertEqual(snapshot["status"], "waiting")


if __name__ == "__main__":
    unittest.main()
