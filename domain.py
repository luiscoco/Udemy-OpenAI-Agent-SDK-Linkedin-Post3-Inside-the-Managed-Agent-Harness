"""Application-owned tools and checks; no model or transport code."""
import csv
import json
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data" / "sales.csv"
INSTRUCTIONS = """You are a sales report assistant. Use analyze_sales for the exact
months in the user input; never estimate figures. Currency is EUR and refunds
are excluded. Then request save_report using the returned report_id. Approval
is handled by the application, not by you. If saving is rejected, do not retry
the write or invent success. If a tool fails, explain failure and do not invent
figures. Once analysis and saving (or rejection) are resolved, return ONLY a JSON
object with exactly report_id, currency, current_total, previous_total,
change_pct, published. Numeric values must be decimal strings with two digits
after the decimal point. published must be a boolean based on the actual tool
result. Do not delegate or call other tools."""

TOOL_DEFINITIONS = [
    {"type": "function", "name": "analyze_sales",
     "description": "Compare two months of EUR sales, excluding refunds.",
     "parameters": {"type": "object", "properties": {
         "current_month": {"type": "string", "description": "YYYY-MM"},
         "previous_month": {"type": "string", "description": "YYYY-MM"}},
         "required": ["current_month", "previous_month"], "additionalProperties": False}},
    {"type": "function", "name": "save_report",
     "description": "Save the verified analysis locally. Application approval is required.",
     "parameters": {"type": "object", "properties": {"report_id": {"type": "string"}},
         "required": ["report_id"], "additionalProperties": False}},
]


def month(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise ValueError("Month must use YYYY-MM")
    date.fromisoformat(value + "-01")
    return value


def require_keys(arguments, keys):
    if not isinstance(arguments, dict) or set(arguments) != keys:
        raise ValueError("Tool arguments do not match the allowed schema")
    if not all(isinstance(v, str) for v in arguments.values()):
        raise ValueError("Tool arguments must be strings")


def analyze(current_month, previous_month, data_path=DATA):
    current_month, previous_month = month(current_month), month(previous_month)
    if current_month <= previous_month:
        raise ValueError("Current month must follow previous month")
    totals = {current_month: Decimal(0), previous_month: Decimal(0)}
    counts = dict.fromkeys(totals, 0)
    ids = set()
    with Path(data_path).open(newline="", encoding="utf-8-sig") as source:
        for row in csv.DictReader(source):
            if row["order_id"] in ids:
                raise ValueError("Duplicate order identifier in source")
            ids.add(row["order_id"])
            period = date.fromisoformat(row["date"]).strftime("%Y-%m")
            if row["currency"] != "EUR" or row["kind"] not in {"sale", "refund"}:
                raise ValueError("Unsupported source currency or row kind")
            amount = Decimal(row["amount"])
            if not amount.is_finite() or amount != amount.quantize(Decimal("0.01")):
                raise ValueError("Amounts must be finite values in cents")
            if (row["kind"] == "sale" and amount < 0) or (row["kind"] == "refund" and amount > 0):
                raise ValueError("Source amount has the wrong sign")
            if period in totals and row["kind"] == "sale":
                totals[period] += amount
                counts[period] += 1
    if not all(counts.values()):
        raise ValueError("Both months must contain sales")
    before, after = totals[previous_month], totals[current_month]
    if before == 0:
        raise ValueError("Cannot compute percentage change from a zero total")
    return {"report_id": f"sales-{current_month}-vs-{previous_month}",
            "current_month": current_month, "previous_month": previous_month,
            "currency": "EUR", "current_total": f"{after:.2f}",
            "previous_total": f"{before:.2f}",
            "change_pct": f"{(after-before)/before*100:.2f}"}


def report_text(analysis):
    return (f"# Sales comparison: {analysis['current_month']} vs {analysis['previous_month']}\n\n"
            f"Currency: EUR. Refunds excluded.\n\n"
            f"Previous sales: EUR {analysis['previous_total']}\n\n"
            f"Current sales: EUR {analysis['current_total']}\n\n"
            f"Change: {analysis['change_pct']}%\n\n"
            f"Source: data/sales.csv\nReport ID: {analysis['report_id']}\n")


def verify_analysis(state, data_path=DATA):
    evidence = state.get("analysis")
    if not evidence:
        raise ValueError("No verified analysis is available")
    context = state["context"]
    fresh = analyze(context["current_month"], context["previous_month"], data_path)
    if evidence != fresh:
        raise ValueError("Source data changed; start a new analysis")
    return evidence


def validate_call(name, arguments, context):
    if name == "analyze_sales":
        require_keys(arguments, {"current_month", "previous_month"})
        if arguments != {"current_month": context["current_month"],
                         "previous_month": context["previous_month"]}:
            raise ValueError("Requested months do not match this run")
    elif name == "save_report":
        require_keys(arguments, {"report_id"})
        expected = f"sales-{context['current_month']}-vs-{context['previous_month']}"
        if arguments["report_id"] != expected:
            raise ValueError("Unknown report identifier")
    else:
        raise ValueError("Tool is not in the application allowlist")


def execute(name, arguments, state, folder, *, approved=False, data_path=DATA):
    validate_call(name, arguments, state["context"])
    if name == "analyze_sales":
        result = analyze(**arguments, data_path=data_path)
        state["analysis"] = result
        return result
    if not approved:
        raise PermissionError("The report write requires explicit approval")
    analysis = verify_analysis(state, data_path)
    target = Path(folder) / "report.md"
    text = report_text(analysis)
    if target.exists() and target.read_text(encoding="utf-8") != text:
        raise ValueError("Existing report differs; refusing to overwrite it")
    target.write_text(text, encoding="utf-8")
    state["published"] = True
    return {"report_id": analysis["report_id"], "published": True, "file": "report.md"}


def final_payload(state):
    evidence = state.get("analysis")
    if not evidence:
        raise ValueError("No analysis evidence for the final answer")
    fields = ("report_id", "currency", "current_total", "previous_total", "change_pct")
    return {**{k: evidence[k] for k in fields}, "published": bool(state.get("published"))}


def validate_final(text, state, folder, data_path=DATA):
    verify_analysis(state, data_path)
    actual = json.loads(text)
    expected = final_payload(state)
    if actual != expected or type(actual.get("published")) is not bool:
        raise ValueError("Final answer does not match verified tool evidence")
    target = Path(folder) / "report.md"
    if expected["published"]:
        if not target.exists() or target.read_text(encoding="utf-8") != report_text(state["analysis"]):
            raise ValueError("The saved report is missing or inconsistent")
    elif target.exists():
        raise ValueError("An unapproved report exists")
    return actual
