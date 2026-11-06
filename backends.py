"""Two interchangeable sources of session snapshots; demo makes no API calls."""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from domain import INSTRUCTIONS, TOOL_DEFINITIONS, final_payload


class DemoBackend:
    """A scripted classroom simulator, not a model or an OpenAI harness."""
    def create(self, state):
        state["session_id"] = "demo-" + state["id"]
        state["demo_stage"] = 0

    def snapshot(self, state):
        stage = state["demo_stage"]
        if stage == -1:
            return {"status": "cancelled", "actions": []}
        if stage >= 2:
            if not state.get("analysis"):
                text = json.dumps({"error": "Analysis failed; no figures fabricated"})
            else:
                text = json.dumps(final_payload(state))
            return {"status": "completed", "actions": [], "output": text}
        context = state["context"]
        name = "analyze_sales" if stage == 0 else "save_report"
        args = ({"current_month": context["current_month"],
                 "previous_month": context["previous_month"]} if stage == 0 else
                {"report_id": state["analysis"]["report_id"]})
        action = {"type": "function_call", "turn_id": "demo-turn-1",
                  "call_id": f"demo-call-{stage+1}", "name": name, "arguments": args}
        return {"status": "waiting", "actions": [action]}

    def submit(self, state, action, result):
        # The local simulator retains observations just as teaching evidence.
        state.setdefault("demo_observations", []).append({"action": action, "result": result})
        state["demo_stage"] = (state["demo_stage"] + 1 if result["success"]
                               or action["name"] == "save_report" else 2)

    def cancel(self, state):
        state["demo_stage"] = -1


class LiveBackend:
    """Managed Agents API adapter. HTTP polling observes the remote loop."""
    BASE = "https://api.openai.com/v1"

    def __init__(self, request=None):
        self.model = os.environ.get("OPENAI_MODEL", "")
        self.key = os.environ.get("OPENAI_API_KEY", "")
        self.request = request or self._request
        if request is None and (not self.key or not self.model):
            raise ValueError("Live mode requires OPENAI_API_KEY and OPENAI_MODEL")

    def _request(self, method, path, payload=None, idempotency_key=None):
        headers = {"Authorization": "Bearer " + self.key,
                   "OpenAI-Beta": "agents=v1", "Content-Type": "application/json"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        body = json.dumps(payload).encode() if payload is not None else None
        request = urllib.request.Request(self.BASE + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            # Do not print request headers, credentials, or raw remote response bodies.
            raise RuntimeError(f"OpenAI HTTP {exc.code}; check access, permissions, model, and request schema") from None
        except urllib.error.URLError as exc:
            raise RuntimeError("OpenAI network request failed; state is preserved for resume") from None

    def create(self, state):
        context = state["context"]
        task = (f"Compare {context['current_month']} with {context['previous_month']}. "
                "Use EUR sales, excluding refunds. Analyze the bundled sales data "
                "with the tool, then request saving the report. Use the tool results "
                "for the final JSON answer.")
        payload = {"agent": {"model": self.model, "instructions": INSTRUCTIONS,
                             "tools": TOOL_DEFINITIONS},
                   "environment": {"type": "none"}, "input": task}
        created = self.request("POST", "/agents/sessions", payload, state["creation_key"])
        state["session_id"] = created["id"]

    def pages(self, path):
        """Consume every page of saved items/turns, in the requested order."""
        while True:
            page = self.request("GET", path)
            yield from page["data"]
            if not page.get("has_more"):
                return
            cursor = page.get("last_id")
            if not cursor:
                raise RuntimeError("Pagination response is missing its cursor")
            # Replace, rather than accumulate, the after cursor.
            parts = urllib.parse.urlsplit(path)
            query = dict(urllib.parse.parse_qsl(parts.query))
            query["after"] = cursor
            path = parts.path + "?" + urllib.parse.urlencode(query)

    def snapshot(self, state):
        base = "/agents/sessions/" + urllib.parse.quote(state["session_id"], safe="")
        session = self.request("GET", base)
        state["remote_session_status"] = session["status"]
        actions = session.get("required_actions") or []
        if session["status"] == "failed":
            return {"status": "failed", "actions": [], "error": session.get("error")}
        if actions:
            return {"status": "waiting", "actions": actions}
        # Session idle is not proof of success. Read the actual root turn outcome.
        turn = next((t for t in self.pages(base + "/turns?order=desc&limit=100")
                     if t.get("subagent_id") is None), None)
        status = turn["status"] if turn else "queued"
        result = {"status": status, "actions": [], "error": turn.get("error") if turn else None}
        if status == "completed":
            items = list(self.pages(base + "/items?order=asc&limit=100"))
            state["remote_items"] = items
            messages = [item for item in items if item.get("type") == "message"
                        and item.get("role") == "assistant"]
            final = [item for item in messages if item.get("phase") == "final_answer"]
            selected = (final or messages)[-1:]
            result["output"] = "\n".join(
                part["text"] for item in selected for part in item.get("content", [])
                if part.get("type") == "output_text")
        return result

    def submit(self, state, action, result):
        path = "/agents/sessions/" + urllib.parse.quote(state["session_id"], safe="") + "/events"
        event = {"type": "agent.session.input.tool_result",
                 "turn_id": action["turn_id"], "call_id": action["call_id"], **result}
        token = state["results"][action_key(action)]["submission_key"]
        self.request("POST", path, {"events": [event]}, token)

    def cancel(self, state):
        path = "/agents/sessions/" + urllib.parse.quote(state["session_id"], safe="") + "/events"
        self.request("POST", path, {"events": [{"type": "agent.session.input.cancel"}]},
                     state["creation_key"] + "-cancel")


def action_key(action):
    # JSON preserves the boundary between identifiers (unlike string concatenation).
    return json.dumps([action["turn_id"], action["call_id"]])
