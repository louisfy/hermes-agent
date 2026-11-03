"""Read explicitly supplied webhook filter JSON without echoing its contents."""

import json
from pathlib import Path

_MAX_FILTER_BYTES = 64 * 1024


def _reject_constant(_value: str):
    raise ValueError("--filters requires standard JSON (no NaN or Infinity)")


def read_filter_args(args) -> list | dict | None:
    """None means no edit; an empty list explicitly removes the existing filter."""
    raw = getattr(args, "filters", None)
    filename = getattr(args, "filters_file", None)
    if raw is None and filename is None:
        return None
    if raw is not None and filename is not None:
        raise ValueError("--filters and --filters-file are mutually exclusive")
    try:
        if filename is not None:
            with Path(filename).expanduser().open("rb") as source:
                data = source.read(_MAX_FILTER_BYTES + 1)
        else:
            data = raw.encode("utf-8")
        if len(data) > _MAX_FILTER_BYTES:
            raise ValueError("webhook filters exceed the 64 KiB limit")
        text = data.decode("utf-8-sig")
    except (OSError, UnicodeError) as exc:
        raise ValueError("cannot read webhook filters; provide a readable UTF-8 JSON file or JSON value") from exc
    try:
        filters = json.loads(text, parse_constant=_reject_constant)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ValueError("invalid webhook filter JSON; provide a JSON list or object") from exc
    from gateway.platforms.webhook_filters import validate_route_filters

    validate_route_filters(filters)
    return filters


def apply_route_filters(args, route: dict, existing: dict) -> None:
    filters = read_filter_args(args)
    if filters is not None:
        route["filters"] = filters
    elif "filters" in existing:
        route["filters"] = existing["filters"]


def print_filter_summary(route: dict, indent: str) -> None:
    if "filters" in route:
        count = len(route["filters"]) if isinstance(route["filters"], list) else 1
        print(f"{indent}Filters: {count} top-level rule(s)")
