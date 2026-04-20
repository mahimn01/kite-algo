"""JSONSchema generation for every CLI subcommand.

`kite-algo tools describe` emits an array of tool specs ready to paste into
a Claude `tools` parameter or a GPT function-call spec. The schema is
derived by introspecting argparse — so when flags change in `build_parser`,
the schema auto-updates; there is no second source-of-truth to drift.

Output shape:

    [
      {
        "name": "place",
        "description": "Place a single order (validated, idempotent, rate-limited)",
        "input_schema": {
          "type": "object",
          "properties": {...},
          "required": [...]
        },
        "output_schema": {...},        // envelope + per-command data shape
        "examples": [...]              // from kite_algo/explain.py
      },
      ...
    ]

Conventions:
- Every `--flag` becomes a property named `flag` (no leading dashes,
  dashes replaced with underscores).
- argparse `choices=[...]` becomes JSONSchema `enum`.
- `type=int|float` → `integer` | `number`; `type=str` (default) → `string`;
  `action="store_true"` → `boolean`.
- Required properties = argparse `required=True` flags + positional args
  that have no default.
- Common flags (`--format`, `--fields`, `--summary`, `--explain`) are
  included on every command (they're in `_add_common`), so they show up
  once per tool spec.
"""

from __future__ import annotations

import argparse
from typing import Any


def _arg_to_jsonschema(action: argparse.Action) -> dict[str, Any]:
    """Map one argparse Action → a JSONSchema property."""
    prop: dict[str, Any] = {}

    # Description from argparse help text. Argparse help strings pre-escape
    # `%` as `%%` for its own format-expansion step — that's internal to
    # argparse and shouldn't leak into our emitted schema.
    if action.help:
        prop["description"] = action.help.replace("%%", "%")

    # Enums via choices.
    if action.choices:
        prop["enum"] = list(action.choices)

    # Type mapping.
    if isinstance(action, argparse._StoreTrueAction):
        prop["type"] = "boolean"
        prop["default"] = False
    elif isinstance(action, argparse._StoreFalseAction):
        prop["type"] = "boolean"
        prop["default"] = True
    else:
        py_type = action.type or str
        if py_type is int:
            prop["type"] = "integer"
        elif py_type is float:
            prop["type"] = "number"
        elif py_type is bool:
            prop["type"] = "boolean"
        else:
            prop["type"] = "string"

    # Default value.
    if action.default is not None and "default" not in prop:
        # Don't emit `default` for store_true since False is implied.
        if not isinstance(action, argparse._StoreTrueAction):
            prop["default"] = action.default

    return prop


def _flag_to_property_name(opt_string: str) -> str:
    """`--market-protection` → `market_protection`."""
    s = opt_string.lstrip("-")
    return s.replace("-", "_")


def _subparser_schema(subparser: argparse.ArgumentParser) -> dict[str, Any]:
    """Extract {properties, required} for one subcommand."""
    properties: dict[str, Any] = {}
    required: list[str] = []

    for action in subparser._actions:
        # Skip the help action.
        if isinstance(action, argparse._HelpAction):
            continue
        # Skip the `func` default and subparser container if present.
        if action.dest in ("cmd", "func"):
            continue
        # Skip positional without option string (none in our parser).
        if not action.option_strings:
            if action.default is argparse.SUPPRESS:
                continue
            # Positional with dest — include as required.
            properties[action.dest] = _arg_to_jsonschema(action)
            if action.required or action.default is None:
                required.append(action.dest)
            continue

        # The canonical flag is the longest option_string.
        long_flag = max(action.option_strings, key=len)
        name = _flag_to_property_name(long_flag)
        properties[name] = _arg_to_jsonschema(action)
        if action.required:
            required.append(name)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
    }
    if required:
        schema["required"] = sorted(required)
    return schema


def _output_schema_for(cmd_name: str) -> dict[str, Any]:
    """Default envelope output schema. Per-command `data` shape is modelled
    loosely — agents should rely on the envelope, not the inner data shape,
    because Kite's responses change over time.
    """
    return {
        "type": "object",
        "properties": {
            "ok": {"type": "boolean"},
            "cmd": {"const": cmd_name},
            "schema_version": {"type": "string"},
            "request_id": {"type": "string"},
            "data": {},  # command-specific; see --explain for details
            "warnings": {
                "type": "array",
                "items": {"type": "object"},
            },
            "meta": {"type": "object"},
            "error": {
                "type": "object",
                "properties": {
                    "code": {"type": "string"},
                    "class": {"type": "string"},
                    "message": {"type": "string"},
                    "retryable": {"type": "boolean"},
                    "field_errors": {"type": "array"},
                    "suggested_action": {"type": "string"},
                },
            },
        },
        "required": ["ok", "cmd", "schema_version", "request_id"],
    }


def _examples_for(cmd_name: str) -> list[dict]:
    """Pull 1-3 examples per command from the explain module's notes."""
    from kite_algo.explain import all_explanations
    body = all_explanations().get(cmd_name, {})
    notes = body.get("notes", [])
    return [{"description": n} for n in notes[:3]]


def describe_tools(parser: argparse.ArgumentParser) -> list[dict]:
    """Return the full tool-spec array for every subcommand in `parser`.

    Iterates over the subparsers action (dest="cmd") and synthesises one
    entry per command. The result is JSON-serialisable.
    """
    sub_action = None
    for a in parser._actions:
        if isinstance(a, argparse._SubParsersAction):
            sub_action = a
            break
    if sub_action is None:
        return []

    tools: list[dict] = []
    for name, sub in sub_action.choices.items():
        spec: dict[str, Any] = {
            "name": name,
            "description": sub.description or (sub.format_usage() or "").strip(),
            "input_schema": _subparser_schema(sub),
            "output_schema": _output_schema_for(name),
        }
        ex = _examples_for(name)
        if ex:
            spec["examples"] = ex
        tools.append(spec)
    tools.sort(key=lambda t: t["name"])
    return tools
