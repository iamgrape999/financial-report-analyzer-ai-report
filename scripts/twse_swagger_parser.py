#!/usr/bin/env python3
"""
Parse the TWSE OpenAPI Swagger spec and generate endpoint/field catalogs.

Usage:
    python scripts/twse_swagger_parser.py [--output-dir data/]

Outputs:
    data/twse_endpoint_catalog.csv  — one row per endpoint
    data/twse_field_catalog.csv     — one row per response field
    data/twse_api_summary.txt       — human-readable P0/P1/P2 tier list
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import os
import sys
import unicodedata
from pathlib import Path
from typing import Any

_SWAGGER_URL = "https://openapi.twse.com.tw/v1/swagger.json"
_FETCH_TIMEOUT = 30

# ── Tier classification keywords ─────────────────────────────────────────────

_TIER_RULES: list[tuple[str, list[str]]] = [
    ("P0", ["基本資料", "月營收", "資產負債", "損益", "現金流", "重大訊息", "股利", "大股東", "董監"]),
    ("P1", ["ESG", "酬金", "審計", "承認"]),
    ("P2", ["指數", "鉅額", "外資", "法人"]),
    ("P3", ["權證", "券商"]),
]


# ── Name normalisation ────────────────────────────────────────────────────────

def normalize_name(x: str) -> str:
    """Strip full-width characters, lower-case, and replace spaces with underscores."""
    result = []
    for ch in x:
        # Convert full-width ASCII to half-width
        cp = ord(ch)
        if 0xFF01 <= cp <= 0xFF5E:
            result.append(chr(cp - 0xFEE0))
        elif ch == "　":
            # Full-width space → regular space
            result.append(" ")
        else:
            result.append(ch)
    normalized = "".join(result)
    # Remove non-ASCII-printable characters that aren't alphanumeric/underscore
    cleaned = "".join(
        c if (c.isascii() and (c.isalnum() or c in "_- ")) else "_"
        for c in normalized
    )
    return cleaned.strip().replace(" ", "_").replace("-", "_").lower()


# ── Tier classifier ───────────────────────────────────────────────────────────

def _classify_tier(tag: str, summary: str) -> str:
    combined = (tag or "") + " " + (summary or "")
    for tier, keywords in _TIER_RULES:
        for kw in keywords:
            if kw in combined:
                return tier
    return "P3"  # default to lowest tier


# ── Endpoint ID ───────────────────────────────────────────────────────────────

def _endpoint_id(path: str, method: str) -> str:
    key = f"{method.upper()}:{path}"
    return hashlib.md5(key.encode()).hexdigest()[:12]


# ── Schema $ref resolver ──────────────────────────────────────────────────────

def _resolve_ref(ref: str, definitions: dict[str, Any]) -> dict[str, Any]:
    """Resolve a '#/definitions/Foo' $ref to its definition dict."""
    if ref.startswith("#/definitions/"):
        name = ref[len("#/definitions/"):]
        return definitions.get(name, {})
    return {}


def _extract_response_schema(
    operation: dict[str, Any],
    definitions: dict[str, Any],
) -> dict[str, Any]:
    """Return the schema dict for the 200 response body (or empty)."""
    responses = operation.get("responses", {})
    ok = responses.get("200", responses.get("201", {}))
    schema = ok.get("schema", {})

    # Unwrap array items
    if schema.get("type") == "array":
        items = schema.get("items", {})
        ref = items.get("$ref", "")
        if ref:
            return _resolve_ref(ref, definitions)
        return items

    # Direct $ref
    ref = schema.get("$ref", "")
    if ref:
        return _resolve_ref(ref, definitions)

    return schema


def _flatten_properties(
    schema: dict[str, Any],
    definitions: dict[str, Any],
    prefix: str = "",
) -> list[tuple[str, str, str]]:
    """
    Recursively flatten schema properties.

    Returns list of (source_column_name, data_type, description).
    """
    results: list[tuple[str, str, str]] = []
    props = schema.get("properties", {})
    for prop_name, prop_schema in props.items():
        full_name = f"{prefix}.{prop_name}" if prefix else prop_name

        # Resolve nested $ref
        if "$ref" in prop_schema:
            nested = _resolve_ref(prop_schema["$ref"], definitions)
            results.extend(_flatten_properties(nested, definitions, prefix=full_name))
            continue

        # Resolve array items
        if prop_schema.get("type") == "array":
            items = prop_schema.get("items", {})
            if "$ref" in items:
                nested = _resolve_ref(items["$ref"], definitions)
                results.extend(_flatten_properties(nested, definitions, prefix=full_name))
                continue

        data_type = prop_schema.get("type", prop_schema.get("format", "string"))
        description = prop_schema.get("description", prop_schema.get("example", ""))
        results.append((full_name, str(data_type), str(description)))

    return results


# ── Main parser ───────────────────────────────────────────────────────────────

def parse_swagger(swagger: dict[str, Any]) -> tuple[list[dict], list[dict]]:
    """
    Parse swagger dict into endpoint_catalog and field_catalog rows.

    Returns (endpoint_rows, field_rows).
    """
    definitions = swagger.get("definitions", {})
    paths = swagger.get("paths", {})

    endpoint_rows: list[dict] = []
    field_rows: list[dict] = []

    for path, path_item in paths.items():
        for method, operation in path_item.items():
            if method.lower() not in ("get", "post", "put", "patch", "delete"):
                continue
            if not isinstance(operation, dict):
                continue

            tags = operation.get("tags", [""])
            tag = tags[0] if tags else ""
            summary = operation.get("summary", "")
            operation_id = operation.get("operationId", "")
            eid = _endpoint_id(path, method)
            tier = _classify_tier(tag, summary)

            endpoint_rows.append({
                "endpoint_id":   eid,
                "path":          path,
                "method":        method.upper(),
                "tag":           tag,
                "summary":       summary,
                "operation_id":  operation_id,
                "tier":          tier,
            })

            # Extract response schema fields
            schema = _extract_response_schema(operation, definitions)
            props = _flatten_properties(schema, definitions)
            for source_col, data_type, description in props:
                field_rows.append({
                    "endpoint_id":           eid,
                    "path":                  path,
                    "source_column_name":    source_col,
                    "normalized_column_name": normalize_name(source_col),
                    "data_type":             data_type,
                    "description":           description,
                })

    return endpoint_rows, field_rows


def _build_summary(endpoint_rows: list[dict]) -> str:
    """Build human-readable P0/P1/P2/P3 tier list."""
    tiers: dict[str, list[str]] = {"P0": [], "P1": [], "P2": [], "P3": []}
    for row in endpoint_rows:
        tier = row.get("tier", "P3")
        label = f"  {row['method']:6s} {row['path']}"
        if row["summary"]:
            label += f"  # {row['summary']}"
        tiers.setdefault(tier, []).append(label)

    lines = ["TWSE OpenAPI Endpoint Tier Summary", "=" * 60, ""]
    for tier in ("P0", "P1", "P2", "P3"):
        items = tiers.get(tier, [])
        lines.append(f"── {tier} ({len(items)} endpoints) ──────────────────────────")
        lines.extend(items)
        lines.append("")

    return "\n".join(lines)


# ── CLI entry-point ───────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir",
        default="data/",
        help="Directory to write CSV and TXT output files (default: data/)",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Fetch swagger spec
    try:
        import urllib.request
        import json as _json

        print(f"Fetching {_SWAGGER_URL} …", file=sys.stderr)
        with urllib.request.urlopen(_SWAGGER_URL, timeout=_FETCH_TIMEOUT) as resp:
            raw = resp.read()
        swagger = _json.loads(raw)
        print(f"Fetched {len(raw):,} bytes.", file=sys.stderr)
    except Exception as exc:
        print(f"ERROR: failed to fetch swagger spec: {exc}", file=sys.stderr)
        return 1

    endpoint_rows, field_rows = parse_swagger(swagger)

    # Write endpoint catalog
    ep_path = output_dir / "twse_endpoint_catalog.csv"
    ep_fieldnames = ["endpoint_id", "path", "method", "tag", "summary", "operation_id", "tier"]
    with ep_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=ep_fieldnames)
        writer.writeheader()
        writer.writerows(endpoint_rows)
    print(f"Wrote {len(endpoint_rows)} endpoints → {ep_path}", file=sys.stderr)

    # Write field catalog
    fd_path = output_dir / "twse_field_catalog.csv"
    fd_fieldnames = [
        "endpoint_id", "path", "source_column_name",
        "normalized_column_name", "data_type", "description",
    ]
    with fd_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fd_fieldnames)
        writer.writeheader()
        writer.writerows(field_rows)
    print(f"Wrote {len(field_rows)} fields → {fd_path}", file=sys.stderr)

    # Write human-readable summary
    summary_path = output_dir / "twse_api_summary.txt"
    summary_text = _build_summary(endpoint_rows)
    summary_path.write_text(summary_text, encoding="utf-8")
    print(f"Wrote summary → {summary_path}", file=sys.stderr)

    # Print P0 endpoints to stdout
    p0_rows = [r for r in endpoint_rows if r.get("tier") == "P0"]
    print(f"\n── P0 Endpoints ({len(p0_rows)}) ──────────────────────────────────")
    for row in p0_rows:
        line = f"  {row['method']:6s} {row['path']}"
        if row["summary"]:
            line += f"  # {row['summary']}"
        print(line)

    return 0


if __name__ == "__main__":
    sys.exit(main())
