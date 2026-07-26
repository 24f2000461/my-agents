"""W3C Trace Context + minimal OTLP JSON span builders."""
import secrets

HEXDIGITS = set("0123456789abcdef")


def new_trace_id() -> str:
    while True:
        v = secrets.token_hex(16)  # 16 bytes -> 32 hex chars
        if v != "0" * 32:
            return v


def new_span_id() -> str:
    while True:
        v = secrets.token_hex(8)  # 8 bytes -> 16 hex chars
        if v != "0" * 16:
            return v


def parse_traceparent(tp: str):
    """Returns (trace_id, parent_span_id, flags) or None if invalid, per W3C spec."""
    if not tp:
        return None
    parts = tp.strip().split("-")
    if len(parts) < 4:
        return None
    version, trace_id, parent_id, flags = parts[0], parts[1], parts[2], parts[3]
    if len(version) != 2 or len(trace_id) != 32 or len(parent_id) != 16 or len(flags) != 2:
        return None
    combined = version + trace_id + parent_id + flags
    if not all(c in HEXDIGITS for c in combined):
        return None
    if trace_id == "0" * 32 or parent_id == "0" * 16:
        return None
    return trace_id, parent_id, flags


def make_traceparent(trace_id: str, span_id: str, sampled: bool = True) -> str:
    return f"00-{trace_id}-{span_id}-{'01' if sampled else '00'}"


def _attr_value(v):
    if isinstance(v, bool):
        return {"boolValue": v}
    if isinstance(v, int):
        return {"intValue": str(v)}
    if isinstance(v, float):
        return {"doubleValue": v}
    return {"stringValue": str(v)}


def make_span(trace_id: str, span_id: str, parent_span_id, name: str, kind: int,
              start_ns: int, end_ns: int, attributes: dict, status_code: int = 0,
              links: list = None) -> dict:
    d = {
        "traceId": trace_id,
        "spanId": span_id,
        "parentSpanId": parent_span_id or "",
        "name": name,
        "kind": kind,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "attributes": [{"key": k, "value": _attr_value(v)} for k, v in attributes.items()],
        "status": {"code": status_code},
    }
    if links:
        d["links"] = links
    return d


SPAN_KIND_INTERNAL = 1
SPAN_KIND_SERVER = 2
SPAN_KIND_CLIENT = 3
