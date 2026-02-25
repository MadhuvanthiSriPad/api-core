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


def _resolve_schema(spec: dict, schema: dict) -> dict:
    """Resolve a schema, following $ref if present."""
    if "$ref" in schema:
        return _resolve_ref(spec, schema["$ref"])
    return schema


def _get_schema_properties(spec: dict, schema: dict) -> dict:
    """Get properties from a schema, resolving $ref if needed."""
    schema = _resolve_schema(spec, schema)
    # For array responses/requests of objects, diff against item fields.
    if schema.get("type") == "array":
        item_schema = _resolve_schema(spec, schema.get("items", {}))
        return item_schema.get("properties", {})
    return schema.get("properties", {})


def _get_required_fields(spec: dict, schema: dict) -> set:
    """Get required fields from a schema, resolving $ref if needed."""
    schema = _resolve_schema(spec, schema)
    if schema.get("type") == "array":
        item_schema = _resolve_schema(spec, schema.get("items", {}))
        return set(item_schema.get("required", []))
    return set(schema.get("required", []))


def _diff_nested(
    old_spec: dict,
    new_spec: dict,
    old_field: dict,
    new_field: dict,
    path: str,
    method: str,
    field_prefix: str,
    diffs: list[ContractDiff],
) -> None:
    """Recursively detect changes in nested object and array schemas."""
    old_resolved = _resolve_schema(old_spec, old_field)
    new_resolved = _resolve_schema(new_spec, new_field)

    # Nested object: compare sub-properties
    if old_resolved.get("type") == "object" and new_resolved.get("type") == "object":
        old_sub = old_resolved.get("properties", {})
        new_sub = new_resolved.get("properties", {})

        for sub_name in set(old_sub.keys()) - set(new_sub.keys()):
            diffs.append(ContractDiff(
                path=path, method=method,
                field=f"{field_prefix}.{sub_name}",
                old_value=old_sub[sub_name],
                new_value=None,
                diff_type="nested_field_removed",
            ))

        for sub_name in set(new_sub.keys()) - set(old_sub.keys()):
            diffs.append(ContractDiff(
                path=path, method=method,
                field=f"{field_prefix}.{sub_name}",
                old_value=None,
                new_value=new_sub[sub_name],
                diff_type="nested_field_added",
            ))

        for sub_name in set(old_sub.keys()) & set(new_sub.keys()):
            old_t = old_sub[sub_name].get("type")
            new_t = new_sub[sub_name].get("type")
            if old_t != new_t:
                diffs.append(ContractDiff(
                    path=path, method=method,
                    field=f"{field_prefix}.{sub_name}",
                    old_value=old_t,
                    new_value=new_t,
                    diff_type="nested_field_type_changed",
                ))
            # Recurse into nested objects/arrays
            old_sub_resolved = _resolve_schema(old_spec, old_sub[sub_name])
            new_sub_resolved = _resolve_schema(new_spec, new_sub[sub_name])
            if (old_sub_resolved.get("type") in ("object", "array")
                    or new_sub_resolved.get("type") in ("object", "array")):
                _diff_nested(
                    old_spec, new_spec,
                    old_sub[sub_name], new_sub[sub_name],
                    path, method, f"{field_prefix}.{sub_name}",
                    diffs,
                )

    # Array items: compare item schema
    if old_resolved.get("type") == "array" and new_resolved.get("type") == "array":
        old_items = old_resolved.get("items", {})
        new_items = new_resolved.get("items", {})
        old_item_resolved = _resolve_schema(old_spec, old_items)
        new_item_resolved = _resolve_schema(new_spec, new_items)
        old_item_type = old_item_resolved.get("type")
        new_item_type = new_item_resolved.get("type")
        if old_item_type and new_item_type and old_item_type != new_item_type:
            diffs.append(ContractDiff(
                path=path, method=method,
                field=f"{field_prefix}.items",
                old_value=old_item_type,
                new_value=new_item_type,
                diff_type="array_item_type_changed",
            ))
        # Recurse into array item schemas if they are objects/arrays
        if old_item_type in ("object", "array") or new_item_type in ("object", "array"):
            _diff_nested(
                old_spec, new_spec,
                old_items, new_items,
                path, method, f"{field_prefix}.items",
                diffs,
            )


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

            # Compare parameters (query, path, header)
            old_params = {(p.get("name"), p.get("in")): p for p in old_op.get("parameters", [])}
            new_params = {(p.get("name"), p.get("in")): p for p in new_op.get("parameters", [])}

            for key in set(new_params.keys()) - set(old_params.keys()):
                param = new_params[key]
                if param.get("required", False):
                    diffs.append(ContractDiff(
                        path=path, method=method,
                        field=f"parameter.{key[1]}.{key[0]}",
                        old_value=None, new_value=param,
                        diff_type="parameter_added_required",
                    ))

            for key in set(old_params.keys()) - set(new_params.keys()):
                diffs.append(ContractDiff(
                    path=path, method=method,
                    field=f"parameter.{key[1]}.{key[0]}",
                    old_value=old_params[key], new_value=None,
                    diff_type="parameter_removed",
                ))

            for key in set(old_params.keys()) & set(new_params.keys()):
                old_p = old_params[key]
                new_p = new_params[key]
                old_p_schema = old_p.get("schema", {})
                new_p_schema = new_p.get("schema", {})
                if old_p_schema.get("type") != new_p_schema.get("type"):
                    diffs.append(ContractDiff(
                        path=path, method=method,
                        field=f"parameter.{key[1]}.{key[0]}",
                        old_value=old_p_schema.get("type"),
                        new_value=new_p_schema.get("type"),
                        diff_type="parameter_type_changed",
                    ))

            # Compare request body content types
            old_req_body = old_op.get("requestBody", {})
            new_req_body = new_op.get("requestBody", {})
            old_content_types = set(old_req_body.get("content", {}).keys())
            new_content_types = set(new_req_body.get("content", {}).keys())
            if old_content_types and new_content_types and old_content_types != new_content_types:
                diffs.append(ContractDiff(
                    path=path, method=method,
                    field="request.content_type",
                    old_value=sorted(old_content_types),
                    new_value=sorted(new_content_types),
                    diff_type="content_type_changed",
                ))

            # Compare security schemes
            old_security = old_op.get("security", [])
            new_security = new_op.get("security", [])
            if old_security != new_security:
                old_schemes = sorted(k for s in old_security for k in s.keys()) if old_security else []
                new_schemes = sorted(k for s in new_security for k in s.keys()) if new_security else []
                if old_schemes != new_schemes:
                    diffs.append(ContractDiff(
                        path=path, method=method,
                        field="security",
                        old_value=old_schemes or None,
                        new_value=new_schemes or None,
                        diff_type="security_changed",
                    ))
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

                # Check for new required fields (breaking) — both brand-new and optional→required
                for field_name in new_required - old_required:
                    if field_name not in old_props:
                        diffs.append(ContractDiff(
                            path=path, method=method,
                            field=f"request.body.{field_name}",
                            old_value=None,
                            new_value=new_props.get(field_name),
                            diff_type="field_added_required",
                        ))
                    else:
                        # Existing optional field promoted to required (breaking)
                        diffs.append(ContractDiff(
                            path=path, method=method,
                            field=f"request.body.{field_name}",
                            old_value="optional",
                            new_value="required",
                            diff_type="field_optional_to_required",
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

                # Check for type changes, enum narrowing, and nested schema changes
                for field_name in set(old_props.keys()) & set(new_props.keys()):
                    old_field = old_props[field_name]
                    new_field = new_props[field_name]
                    old_type = old_field.get("type")
                    new_type = new_field.get("type")
                    if old_type != new_type:
                        diffs.append(ContractDiff(
                            path=path, method=method,
                            field=f"request.body.{field_name}",
                            old_value=old_type,
                            new_value=new_type,
                            diff_type="field_type_changed",
                        ))

                    # Enum value narrowing (removing allowed values is breaking)
                    old_enum = set(old_field.get("enum", []))
                    new_enum = set(new_field.get("enum", []))
                    if old_enum and new_enum:
                        removed_values = old_enum - new_enum
                        if removed_values:
                            diffs.append(ContractDiff(
                                path=path, method=method,
                                field=f"request.body.{field_name}",
                                old_value=sorted(old_enum),
                                new_value=sorted(new_enum),
                                diff_type="enum_values_removed",
                            ))

                    # Nested object schema changes
                    _diff_nested(
                        old_spec, new_spec, old_field, new_field,
                        path, method, f"request.body.{field_name}",
                        diffs,
                    )

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

                # Check for type changes, enum narrowing, and nested changes in response
                for field_name in set(old_resp_props.keys()) & set(new_resp_props.keys()):
                    old_field = old_resp_props[field_name]
                    new_field = new_resp_props[field_name]
                    old_type = old_field.get("type")
                    new_type = new_field.get("type")
                    if old_type != new_type:
                        diffs.append(ContractDiff(
                            path=path, method=method,
                            field=f"response.{status_code}.{field_name}",
                            old_value=old_type,
                            new_value=new_type,
                            diff_type="field_type_changed",
                        ))

                    # Enum narrowing in response
                    old_enum = set(old_field.get("enum", []))
                    new_enum = set(new_field.get("enum", []))
                    if old_enum and new_enum:
                        removed_values = old_enum - new_enum
                        if removed_values:
                            diffs.append(ContractDiff(
                                path=path, method=method,
                                field=f"response.{status_code}.{field_name}",
                                old_value=sorted(old_enum),
                                new_value=sorted(new_enum),
                                diff_type="enum_values_removed",
                            ))

                    # Nested object/array schema changes in response
                    _diff_nested(
                        old_spec, new_spec, old_field, new_field,
                        path, method, f"response.{status_code}.{field_name}",
                        diffs,
                    )

    return diffs
