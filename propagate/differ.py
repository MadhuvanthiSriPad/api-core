"""OpenAPI contract differ — detects changes between old and new contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import yaml


@dataclass
class ContractDiff:
    path: str           # e.g. "/api/v1/sessions"
    method: str         # e.g. "post"
    field: str          # e.g. "request.body.priority"
    old_value: Any
    new_value: Any
    diff_type: str      # field_added_required, field_removed, field_type_changed, field_moved, response_structure_changed


def load_contract(path: str) -> dict:
    """Load and parse an OpenAPI YAML file."""
    with open(path) as f:
        return yaml.safe_load(f)


def _resolve_ref(spec: dict, ref: str) -> dict:
    """Resolve a $ref pointer within the spec."""
    parts = ref.lstrip("#/").split("/")
    node = spec
    for part in parts:
        node = node.get(part, {})
    return node


def _get_schema_properties(spec: dict, schema: dict) -> dict:
    """Get properties from a schema, resolving $ref if needed."""
    if "$ref" in schema:
        schema = _resolve_ref(spec, schema["$ref"])
    return schema.get("properties", {})


def _get_required_fields(spec: dict, schema: dict) -> set:
    """Get required fields from a schema, resolving $ref if needed."""
    if "$ref" in schema:
        schema = _resolve_ref(spec, schema["$ref"])
    return set(schema.get("required", []))


def diff_contracts(old_spec: dict, new_spec: dict) -> list[ContractDiff]:
    """Compare two OpenAPI specs and return a list of differences."""
    diffs: list[ContractDiff] = []

    old_paths = old_spec.get("paths", {})
    new_paths = new_spec.get("paths", {})

    all_paths = set(old_paths.keys()) | set(new_paths.keys())

    for path in sorted(all_paths):
        old_path_item = old_paths.get(path, {})
        new_path_item = new_paths.get(path, {})

        all_methods = set(old_path_item.keys()) | set(new_path_item.keys())
        # Filter to HTTP methods only
        http_methods = {"get", "post", "put", "patch", "delete", "options", "head"}
        all_methods = all_methods & http_methods

        for method in sorted(all_methods):
            old_op = old_path_item.get(method, {})
            new_op = new_path_item.get(method, {})

            if not old_op and new_op:
                diffs.append(ContractDiff(
                    path=path, method=method, field="operation",
                    old_value=None, new_value="added",
                    diff_type="operation_added",
                ))
                continue

            if old_op and not new_op:
                diffs.append(ContractDiff(
                    path=path, method=method, field="operation",
                    old_value="exists", new_value=None,
                    diff_type="operation_removed",
                ))
                continue

            # Compare request body schemas
            old_req_body = old_op.get("requestBody", {})
            new_req_body = new_op.get("requestBody", {})
            if old_req_body or new_req_body:
                old_schema = (old_req_body.get("content", {})
                              .get("application/json", {})
                              .get("schema", {}))
                new_schema = (new_req_body.get("content", {})
                              .get("application/json", {})
                              .get("schema", {}))

                old_props = _get_schema_properties(old_spec, old_schema)
                new_props = _get_schema_properties(new_spec, new_schema)
                old_required = _get_required_fields(old_spec, old_schema)
                new_required = _get_required_fields(new_spec, new_schema)

                # Check for new required fields (breaking)
                for field_name in new_required - old_required:
                    if field_name not in old_props:
                        diffs.append(ContractDiff(
                            path=path, method=method,
                            field=f"request.body.{field_name}",
                            old_value=None,
                            new_value=new_props.get(field_name),
                            diff_type="field_added_required",
                        ))

                # Check for removed fields
                for field_name in set(old_props.keys()) - set(new_props.keys()):
                    diffs.append(ContractDiff(
                        path=path, method=method,
                        field=f"request.body.{field_name}",
                        old_value=old_props[field_name],
                        new_value=None,
                        diff_type="field_removed",
                    ))

                # Check for type changes
                for field_name in set(old_props.keys()) & set(new_props.keys()):
                    old_type = old_props[field_name].get("type")
                    new_type = new_props[field_name].get("type")
                    if old_type != new_type:
                        diffs.append(ContractDiff(
                            path=path, method=method,
                            field=f"request.body.{field_name}",
                            old_value=old_type,
                            new_value=new_type,
                            diff_type="field_type_changed",
                        ))

            # Compare response schemas
            for status_code in set(old_op.get("responses", {}).keys()) | set(new_op.get("responses", {}).keys()):
                old_resp = old_op.get("responses", {}).get(status_code, {})
                new_resp = new_op.get("responses", {}).get(status_code, {})

                old_resp_schema = (old_resp.get("content", {})
                                   .get("application/json", {})
                                   .get("schema", {}))
                new_resp_schema = (new_resp.get("content", {})
                                   .get("application/json", {})
                                   .get("schema", {}))

                old_resp_props = _get_schema_properties(old_spec, old_resp_schema)
                new_resp_props = _get_schema_properties(new_spec, new_resp_schema)

                # Check for removed response fields
                for field_name in set(old_resp_props.keys()) - set(new_resp_props.keys()):
                    diffs.append(ContractDiff(
                        path=path, method=method,
                        field=f"response.{status_code}.{field_name}",
                        old_value=old_resp_props[field_name],
                        new_value=None,
                        diff_type="field_removed",
                    ))

                # Check for new response fields with object type (structure change)
                for field_name in set(new_resp_props.keys()) - set(old_resp_props.keys()):
                    new_field = new_resp_props[field_name]
                    if new_field.get("type") == "object":
                        diffs.append(ContractDiff(
                            path=path, method=method,
                            field=f"response.{status_code}.{field_name}",
                            old_value=None,
                            new_value=new_field,
                            diff_type="response_structure_changed",
                        ))

                # Check for type changes in response
                for field_name in set(old_resp_props.keys()) & set(new_resp_props.keys()):
                    old_type = old_resp_props[field_name].get("type")
                    new_type = new_resp_props[field_name].get("type")
                    if old_type != new_type:
                        diffs.append(ContractDiff(
                            path=path, method=method,
                            field=f"response.{status_code}.{field_name}",
                            old_value=old_type,
                            new_value=new_type,
                            diff_type="field_type_changed",
                        ))

    return diffs
