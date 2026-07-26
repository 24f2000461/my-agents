import hashlib
import json
import logging
import time
import traceback
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from . import incident_store as store
from . import incident_otel as otel
from .incident_llm import plan as llm_plan

log = logging.getLogger("incident")
router = APIRouter()

MAX_RESPONSE_BYTES = 768 * 1024
PROFILE = "ga5-incident-agent/v2"


def canonical_json(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def new_id() -> str:
    import secrets
    return "id_" + secrets.token_hex(6)  # 3 + 12 = 15 chars, well over 8


def _err(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error": code, "message": message},
                         media_type="application/json")


def _bounded(payload: Dict[str, Any]) -> Response:
    body = canonical_json(payload)
    if len(body.encode("utf-8")) > MAX_RESPONSE_BYTES:
        return _err(500, "response_too_large", "internal: response exceeded 768 KiB bound")
    return Response(content=body, status_code=200, media_type="application/json")


def _now_ns() -> int:
    return time.time_ns()


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------

def _validate_create_body(body: Dict[str, Any]) -> Optional[str]:
    if not isinstance(body, dict):
        return "body must be a JSON object"
    if body.get("profile") != PROFILE:
        return f"unsupported profile (expected {PROFILE!r})"
    if not isinstance(body.get("runId"), str) or not body["runId"]:
        return "runId must be a nonempty string"
    incident = body.get("incident")
    if not isinstance(incident, dict):
        return "incident must be an object"
    for k in ("incidentId", "title", "service", "severity", "transcript", "allowedRootCauses"):
        if k not in incident:
            return f"incident missing required field {k!r}"
    if not isinstance(incident["allowedRootCauses"], list) or not incident["allowedRootCauses"]:
        return "incident.allowedRootCauses must be a nonempty list"
    if not isinstance(body.get("toolCatalog"), list):
        return "toolCatalog must be a list"
    policy = body.get("policy")
    if not isinstance(policy, dict):
        return "policy must be an object"
    for k in ("maximumDiagnostics", "effectTools", "approvalRequiredFor", "doNotExport"):
        if k not in policy:
            return f"policy missing required field {k!r}"
    return None


# --------------------------------------------------------------------------
# Trace/span bootstrap for a new run
# --------------------------------------------------------------------------

def _bootstrap_trace(incoming_traceparent: Optional[str]):
    parsed = otel.parse_traceparent(incoming_traceparent) if incoming_traceparent else None
    if parsed:
        trace_id, parent_id, _flags = parsed
    else:
        trace_id, parent_id = otel.new_trace_id(), None
    server_span_id = otel.new_span_id()
    agent_span_id = otel.new_span_id()
    model_span_id = otel.new_span_id()
    return trace_id, parent_id, server_span_id, agent_span_id, model_span_id


def _make_action(action_id: str, tool_name: str, phase: str, arguments: dict,
                  evidence: List[str], trace_id: str) -> Dict[str, Any]:
    span_id = otel.new_span_id()
    return {
        "actionId": action_id,
        "callId": action_id,
        "phase": phase,
        "toolName": tool_name,
        "arguments": arguments,
        "evidence": evidence,
        "executeToolSpanId": otel.new_span_id(),
        "state": "pending",
        "attempts": [{
            "attempt": 1, "spanId": span_id,
            "traceparent": otel.make_traceparent(trace_id, span_id),
            "receiptId": None, "nonce": None, "status": None,
            "resultClass": None, "errorType": None,
            "startNs": _now_ns(), "endNs": None,
        }],
    }


def _dispatch_of(action: Dict[str, Any], attempt_idx: int = -1) -> Dict[str, Any]:
    att = action["attempts"][attempt_idx]
    return {
        "actionId": action["actionId"], "callId": action["callId"], "phase": action["phase"],
        "toolName": action["toolName"], "arguments": action["arguments"],
        "evidence": action["evidence"], "attempt": att["attempt"],
        "traceparent": att["traceparent"],
    }


# --------------------------------------------------------------------------
# OTLP construction
# --------------------------------------------------------------------------

def _build_otlp(state: Dict[str, Any]) -> Dict[str, Any]:
    trace_id = state["traceId"]
    run_id = state["runId"]
    marker = state["publicMarker"]
    base_attrs = {"ga5.run.id": run_id, "ga5.public.marker": marker}
    now = _now_ns()
    spans = []

    spans.append(otel.make_span(
        trace_id, state["serverSpanId"], state["parentSpanId"], "POST /v2/incidents",
        otel.SPAN_KIND_SERVER, state["startNs"], now,
        {**base_attrs, "http.request.method": "POST"},
    ))
    spans.append(otel.make_span(
        trace_id, state["agentSpanId"], state["serverSpanId"],
        f"invoke_agent {state['agentName']}", otel.SPAN_KIND_INTERNAL,
        state["startNs"], now, {**base_attrs, "gen_ai.operation.name": "invoke_agent"},
    ))
    spans.append(otel.make_span(
        trace_id, state["modelSpanId"], state["agentSpanId"], "chat incident-plan",
        otel.SPAN_KIND_CLIENT, state["startNs"], now,
        {**base_attrs, "gen_ai.operation.name": "chat", "gen_ai.request.model": state["modelName"]},
    ))

    for action_id in state["actionOrder"]:
        action = state["actions"][action_id]
        exec_span_id = action["executeToolSpanId"]
        spans.append(otel.make_span(
            trace_id, exec_span_id, state["agentSpanId"], f"execute_tool {action['toolName']}",
            otel.SPAN_KIND_INTERNAL, state["startNs"], now,
            {**base_attrs, "ga5.action.id": action_id, "gen_ai.tool.name": action["toolName"],
             "gen_ai.tool.call.id": action["callId"], "gen_ai.operation.name": "execute_tool"},
        ))
        for att in action["attempts"]:
            attrs = {
                **base_attrs, "ga5.action.id": action_id, "ga5.attempt": att["attempt"],
                "http.request.method": "POST", "http.request.resend_count": att["attempt"] - 1,
            }
            status_code = 0
            if att["receiptId"]:
                attrs["ga5.receipt.id"] = att["receiptId"]
            if att["nonce"]:
                attrs["ga5.receipt.nonce"] = att["nonce"]
            if att["status"] is not None:
                attrs["http.response.status_code"] = att["status"]
            if att["errorType"]:
                attrs["error.type"] = att["errorType"]
                status_code = 2
            elif att["status"] == 503:
                attrs["error.type"] = "503"
                status_code = 2
            spans.append(otel.make_span(
                trace_id, att["spanId"], exec_span_id, f"POST tool/{action['toolName']}",
                otel.SPAN_KIND_CLIENT, att["startNs"], att["endNs"] or now, attrs, status_code,
            ))

    if state.get("joinSpanId"):
        links = [{"traceId": trace_id, "spanId": state["actions"][aid]["executeToolSpanId"]}
                  for aid in state["diagnosticActionIds"]]
        spans.append(otel.make_span(
            trace_id, state["joinSpanId"], state["agentSpanId"], "incident.join",
            otel.SPAN_KIND_INTERNAL, state["startNs"], now, base_attrs, links=links,
        ))

    if state.get("approvalGateSpanId") and state.get("approvals"):
        for appr in state["approvals"].values():
            attrs = {**base_attrs, "ga5.approval.id": appr["approvalId"]}
            if appr.get("nonce"):
                attrs["ga5.approval.nonce"] = appr["nonce"]
            spans.append(otel.make_span(
                trace_id, state["approvalGateSpanId"], state["agentSpanId"], "approval_gate",
                otel.SPAN_KIND_INTERNAL, state["startNs"], now, attrs,
            ))
            break  # one approval_gate span; attrs reflect the (single) approval in scope

    return {"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]}


# --------------------------------------------------------------------------
# Response builders
# --------------------------------------------------------------------------

def _waiting_response(run_id: str, state: Dict[str, Any], dispatches: List[Dict[str, Any]],
                       approvals: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "runId": run_id, "status": "waiting",
        "diagnosis": state["diagnosis"], "dispatches": dispatches, "approvals": approvals,
    }


def _terminal_response(run_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "runId": run_id, "status": state["status"],
        "diagnosis": state["diagnosis"],
        "chosenEffect": state.get("chosenEffect"),
        "suppressed": state.get("suppressed", []),
        "actionLog": state["actionLog"],
        "receiptLog": state["receiptLog"],
        "otlp": _build_otlp(state),
    }


# --------------------------------------------------------------------------
# POST /v2/incidents
# --------------------------------------------------------------------------

@router.post("/v2/incidents")
async def create_incident(request: Request):
    try:
        raw_bytes = await request.body()
        try:
            body = json.loads(raw_bytes)
        except json.JSONDecodeError:
            return _err(400, "malformed_json", "request body is not valid JSON")

        err = _validate_create_body(body)
        if err:
            return _err(422, "invalid_request", err)

        run_id = body["runId"]
        req_hash = sha256_hex(canonical_json(body))

        existing = store.get_run(run_id)
        if existing is not None:
            if existing["requestHash"] != req_hash:
                return _err(409, "run_conflict", "runId already used with different content")
            return _bounded(existing["initialResponse"])

        incoming_tp = request.headers.get("traceparent")
        trace_id, parent_id, server_span_id, agent_span_id, model_span_id = _bootstrap_trace(incoming_tp)

        incident = body["incident"]
        tool_catalog = body["toolCatalog"]
        policy = body["policy"]
        max_diag = int(policy.get("maximumDiagnostics", 3))

        decision = llm_plan(incident, tool_catalog, policy, max_diag)

        diagnostic_actions = []
        for d in decision["diagnostics"][: max(1, min(3, max_diag))]:
            aid = new_id()
            action = _make_action(aid, d["toolName"], "diagnostic", d.get("arguments", {}),
                                   d.get("evidence", []), trace_id)
            diagnostic_actions.append(action)

        effect_action_id = new_id()  # reserved now, dispatched later

        state = {
            "runId": run_id, "agentName": body.get("agentName", "incident-response"),
            "publicMarker": body.get("publicMarker", ""),
            "modelName": __import__("os").environ.get("GROQ_MODEL") or __import__("os").environ.get("MAILROOM_MODEL", "unknown"),
            "traceId": trace_id, "parentSpanId": parent_id,
            "serverSpanId": server_span_id, "agentSpanId": agent_span_id, "modelSpanId": model_span_id,
            "startNs": _now_ns(),
            "policy": policy,
            "diagnosis": {"rootCause": decision["rootCause"], "evidence": decision["evidence"]},
            "actions": {a["actionId"]: a for a in diagnostic_actions},
            "actionOrder": [a["actionId"] for a in diagnostic_actions],
            "diagnosticActionIds": [a["actionId"] for a in diagnostic_actions],
            "effectActionId": effect_action_id,
            "effectPlan": decision["effect"],
            "effectDispatched": False,
            "joinSpanId": otel.new_span_id() if len(diagnostic_actions) >= 2 else None,
            "approvalGateSpanId": (otel.new_span_id()
                                    if decision["effect"].get("toolName") in policy.get("approvalRequiredFor", [])
                                    else None),
            "approvals": {},
            "chosenEffect": None,
            "suppressed": [],
            "status": "waiting",
            "actionLog": [_dispatch_of(a) for a in diagnostic_actions],
            "receiptLog": [],
        }

        dispatches = [_dispatch_of(a) for a in diagnostic_actions]
        response = _waiting_response(run_id, state, dispatches, [])

        store.create_run(run_id, req_hash, response, state)
        return _bounded(response)
    except Exception:
        log.error("unhandled error in create_incident\n%s", traceback.format_exc())
        return _err(500, "internal_error", "unexpected server error; see logs")


# --------------------------------------------------------------------------
# POST /v2/incidents/{runId}/receipts
# --------------------------------------------------------------------------

@router.post("/v2/incidents/{run_id}/receipts")
async def post_receipts(run_id: str, request: Request):
    try:
        raw_bytes = await request.body()
        try:
            body = json.loads(raw_bytes)
        except json.JSONDecodeError:
            return _err(400, "malformed_json", "request body is not valid JSON")

        if not isinstance(body, dict) or not isinstance(body.get("receiptId"), str) or not body["receiptId"]:
            return _err(422, "invalid_request", "receiptId is required")

        run = store.get_run(run_id)
        if run is None:
            return _err(404, "unknown_run", "no run with this runId")

        content_hash = sha256_hex(canonical_json(body))
        prior = store.get_receipt(run_id, body["receiptId"])
        if prior is not None:
            if prior["contentHash"] != content_hash:
                return _err(409, "receipt_conflict", "receiptId already used with different content")
            return _bounded(prior["response"])

        state = run["state"]
        if state["status"] in ("completed", "failed"):
            return _err(422, "invalid_state", "run already reached a terminal state")

        response, valid = _process_receipt(run_id, state, body)
        if not valid:
            return _err(422, "invalid_receipt", "receipt does not match any pending call/approval")

        store.update_state(run_id, state)
        store.put_receipt(run_id, body["receiptId"], content_hash, response)
        return _bounded(response)
    except Exception:
        log.error("unhandled error in post_receipts\n%s", traceback.format_exc())
        return _err(500, "internal_error", "unexpected server error; see logs")


def _process_receipt(run_id: str, state: Dict[str, Any], body: Dict[str, Any]):
    receipt_id = body["receiptId"]
    outcomes = body.get("outcomes") or []
    approvals_in = body.get("approvals") or []
    if not outcomes and not approvals_in:
        return None, False

    # --- validate everything first (atomic: don't mutate on any invalid item) ---
    for oc in outcomes:
        aid, cid, attempt = oc.get("actionId"), oc.get("callId"), oc.get("attempt")
        action = state["actions"].get(aid)
        if action is None or action["callId"] != cid:
            return None, False
        last = action["attempts"][-1]
        if last["attempt"] != attempt or last["status"] is not None:
            return None, False
    for ap in approvals_in:
        approval = state["approvals"].get(ap.get("approvalId"))
        if approval is None or approval["decision"] is not None:
            return None, False

    # --- apply ---
    retry_dispatches = []
    for oc in outcomes:
        action = state["actions"][oc["actionId"]]
        last = action["attempts"][-1]
        last["status"] = oc.get("status")
        last["resultClass"] = oc.get("resultClass")
        last["errorType"] = oc.get("errorType")
        last["receiptId"] = receipt_id
        last["nonce"] = oc.get("nonce")
        last["endNs"] = _now_ns()
        state["receiptLog"].append({
            "receiptId": receipt_id, "actionId": action["actionId"], "callId": action["callId"],
            "attempt": last["attempt"], "status": last["status"], "resultClass": last["resultClass"],
            "nonce": last["nonce"],
        })
        if last["status"] == 200:
            action["state"] = "confirmed" if action["phase"] == "diagnostic" else "executed"
        elif last["status"] == 503 and last["attempt"] == 1:
            new_span = otel.new_span_id()
            action["attempts"].append({
                "attempt": 2, "spanId": new_span,
                "traceparent": otel.make_traceparent(state["traceId"], new_span),
                "receiptId": None, "nonce": None, "status": None,
                "resultClass": None, "errorType": None, "startNs": _now_ns(), "endNs": None,
            })
            action["state"] = "pending"
            dispatch = _dispatch_of(action)
            retry_dispatches.append(dispatch)
            state["actionLog"].append(dispatch)
        else:
            action["state"] = "failed"

    for ap in approvals_in:
        approval = state["approvals"][ap["approvalId"]]
        approval["decision"] = ap.get("decision")
        approval["nonce"] = ap.get("nonce")
        state["receiptLog"].append({
            "receiptId": receipt_id, "approvalId": approval["approvalId"],
            "decision": approval["decision"], "nonce": approval["nonce"],
        })

    if retry_dispatches:
        response = _waiting_response(run_id, state, retry_dispatches, [])
        return response, True

    return _advance(run_id, state), True


def _advance(run_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
    """Move the state machine forward once no retries are outstanding."""
    diag_actions = [state["actions"][aid] for aid in state["diagnosticActionIds"]]

    if any(a["state"] == "failed" for a in diag_actions):
        state["suppressed"] = list(set(state["suppressed"] + [state["effectActionId"]]))
        state["status"] = "failed"
        return _terminal_response(run_id, state)

    if not all(a["state"] == "confirmed" for a in diag_actions):
        return _waiting_response(run_id, state, [], [])  # still waiting on other diagnostics

    if not state["effectDispatched"]:
        effect_plan = state["effectPlan"]
        tool_name = effect_plan.get("toolName")
        needs_approval = tool_name in state["policy"].get("approvalRequiredFor", [])

        pending_approval = next(
            (a for a in state["approvals"].values() if a["actionId"] == state["effectActionId"]), None)

        if needs_approval and pending_approval is None:
            approval_id = new_id()
            digest = sha256_hex(canonical_json(effect_plan.get("arguments", {})))
            state["approvals"][approval_id] = {
                "approvalId": approval_id, "actionId": state["effectActionId"],
                "toolName": tool_name, "argumentsDigest": digest, "decision": None, "nonce": None,
            }
            return _waiting_response(run_id, state, [], [{
                "approvalId": approval_id, "actionId": state["effectActionId"],
                "toolName": tool_name, "argumentsDigest": digest,
            }])

        if needs_approval and pending_approval is not None:
            if pending_approval["decision"] != "approved":
                state["suppressed"] = list(set(state["suppressed"] + [state["effectActionId"]]))
                state["status"] = "failed"
                return _terminal_response(run_id, state)
            # approved -> fall through to dispatch

        action = _make_action(state["effectActionId"], tool_name, "effect",
                               effect_plan.get("arguments", {}), state["diagnosis"]["evidence"],
                               state["traceId"])
        state["actions"][action["actionId"]] = action
        state["actionOrder"].append(action["actionId"])
        state["effectDispatched"] = True
        dispatch = _dispatch_of(action)
        state["actionLog"].append(dispatch)
        return _waiting_response(run_id, state, [dispatch], [])

    effect_action = state["actions"][state["effectActionId"]]
    if effect_action["state"] == "executed":
        state["chosenEffect"] = effect_action["toolName"]
        state["status"] = "completed"
        return _terminal_response(run_id, state)
    if effect_action["state"] == "failed":
        state["status"] = "failed"
        return _terminal_response(run_id, state)
    return _waiting_response(run_id, state, [], [])


# --------------------------------------------------------------------------
# GET /v2/incidents/{runId}
# --------------------------------------------------------------------------

@router.get("/v2/incidents/{run_id}")
async def get_incident(run_id: str):
    try:
        run = store.get_run(run_id)
        if run is None:
            return _err(404, "unknown_run", "no run with this runId")
        state = run["state"]
        if state["status"] in ("completed", "failed"):
            return _bounded(_terminal_response(run_id, state))
        pending_dispatches = []
        for aid in state["actionOrder"]:
            a = state["actions"][aid]
            if a["state"] == "pending":
                pending_dispatches.append(_dispatch_of(a))
        pending_approvals = [
            {"approvalId": ap["approvalId"], "actionId": ap["actionId"],
             "toolName": ap["toolName"], "argumentsDigest": ap["argumentsDigest"]}
            for ap in state["approvals"].values() if ap["decision"] is None
        ]
        return _bounded(_waiting_response(run_id, state, pending_dispatches, pending_approvals))
    except Exception:
        log.error("unhandled error in get_incident\n%s", traceback.format_exc())
        return _err(500, "internal_error", "unexpected server error; see logs")
