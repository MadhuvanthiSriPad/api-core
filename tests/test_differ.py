"""Tests for the contract differ module."""

import pytest

from propagate.differ import diff_contracts, ContractDiff


def _make_spec(paths=None, components=None):
    spec = {"openapi": "3.1.0", "info": {"title": "Test", "version": "1.0"}}
    if paths:
        spec["paths"] = paths
    if components:
        spec["components"] = components
    return spec


class TestDiffContracts:
    def test_no_changes(self):
        spec = _make_spec(paths={
            "/test": {"get": {"responses": {"200": {"description": "OK"}}}}
        })
        assert diff_contracts(spec, spec) == []

    def test_operation_added(self):
        old = _make_spec(paths={})
        new = _make_spec(paths={
            "/test": {"post": {"responses": {"201": {"description": "Created"}}}}
        })
        diffs = diff_contracts(old, new)
        assert len(diffs) == 1
        assert diffs[0].diff_type == "operation_added"
        assert diffs[0].path == "/test"
        assert diffs[0].method == "post"

    def test_operation_removed(self):
        old = _make_spec(paths={
            "/test": {"delete": {"responses": {"204": {"description": "Deleted"}}}}
        })
        new = _make_spec(paths={"/test": {}})
        diffs = diff_contracts(old, new)
        assert len(diffs) == 1
        assert diffs[0].diff_type == "operation_removed"

    def test_field_added_required(self):
        old = _make_spec(paths={
            "/test": {"post": {
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                }}}},
                "responses": {},
            }}
        })
        new = _make_spec(paths={
            "/test": {"post": {
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "priority": {"type": "string"},
                    },
                    "required": ["name", "priority"],
                }}}},
                "responses": {},
            }}
        })
        diffs = diff_contracts(old, new)
        assert any(d.diff_type == "field_added_required" and "priority" in d.field for d in diffs)

    def test_field_removed(self):
        old = _make_spec(paths={
            "/test": {"post": {
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "old_field": {"type": "string"},
                    },
                }}}},
                "responses": {},
            }}
        })
        new = _make_spec(paths={
            "/test": {"post": {
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                }}}},
                "responses": {},
            }}
        })
        diffs = diff_contracts(old, new)
        assert any(d.diff_type == "field_removed" and "old_field" in d.field for d in diffs)

    def test_field_type_changed(self):
        old = _make_spec(paths={
            "/test": {"post": {
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"count": {"type": "string"}},
                }}}},
                "responses": {},
            }}
        })
        new = _make_spec(paths={
            "/test": {"post": {
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"count": {"type": "integer"}},
                }}}},
                "responses": {},
            }}
        })
        diffs = diff_contracts(old, new)
        assert any(d.diff_type == "field_type_changed" for d in diffs)

    def test_field_optional_to_required(self):
        old = _make_spec(paths={
            "/test": {"post": {
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "tag": {"type": "string"}},
                    "required": ["name"],
                }}}},
                "responses": {},
            }}
        })
        new = _make_spec(paths={
            "/test": {"post": {
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}, "tag": {"type": "string"}},
                    "required": ["name", "tag"],
                }}}},
                "responses": {},
            }}
        })
        diffs = diff_contracts(old, new)
        assert any(d.diff_type == "field_optional_to_required" and "tag" in d.field for d in diffs)

    def test_enum_values_removed(self):
        old = _make_spec(paths={
            "/test": {"post": {
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"status": {"type": "string", "enum": ["a", "b", "c"]}},
                }}}},
                "responses": {},
            }}
        })
        new = _make_spec(paths={
            "/test": {"post": {
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"status": {"type": "string", "enum": ["a", "b"]}},
                }}}},
                "responses": {},
            }}
        })
        diffs = diff_contracts(old, new)
        assert any(d.diff_type == "enum_values_removed" for d in diffs)

    def test_nested_field_changes(self):
        old = _make_spec(paths={
            "/test": {"post": {
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "config": {
                            "type": "object",
                            "properties": {
                                "key": {"type": "string"},
                                "old_nested": {"type": "integer"},
                            },
                        },
                    },
                }}}},
                "responses": {},
            }}
        })
        new = _make_spec(paths={
            "/test": {"post": {
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "config": {
                            "type": "object",
                            "properties": {
                                "key": {"type": "string"},
                                "new_nested": {"type": "string"},
                            },
                        },
                    },
                }}}},
                "responses": {},
            }}
        })
        diffs = diff_contracts(old, new)
        types = {d.diff_type for d in diffs}
        assert "nested_field_removed" in types
        assert "nested_field_added" in types

    def test_array_item_type_changed(self):
        old = _make_spec(paths={
            "/test": {"get": {
                "responses": {"200": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "items": {"type": "array", "items": {"type": "string"}},
                    },
                }}}}}
            }}
        })
        new = _make_spec(paths={
            "/test": {"get": {
                "responses": {"200": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "items": {"type": "array", "items": {"type": "integer"}},
                    },
                }}}}}
            }}
        })
        diffs = diff_contracts(old, new)
        assert any(d.diff_type == "array_item_type_changed" for d in diffs)

    def test_response_field_removed(self):
        old = _make_spec(paths={
            "/test": {"get": {
                "responses": {"200": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "name": {"type": "string"},
                    },
                }}}}}
            }}
        })
        new = _make_spec(paths={
            "/test": {"get": {
                "responses": {"200": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                    },
                }}}}}
            }}
        })
        diffs = diff_contracts(old, new)
        assert any(d.diff_type == "field_removed" and "name" in d.field for d in diffs)

    def test_deeply_nested_recursion(self):
        """Test that _diff_nested recurses more than one level deep."""
        old = _make_spec(paths={
            "/test": {"post": {
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "outer": {
                            "type": "object",
                            "properties": {
                                "inner": {
                                    "type": "object",
                                    "properties": {
                                        "deep_field": {"type": "string"},
                                    },
                                },
                            },
                        },
                    },
                }}}},
                "responses": {},
            }}
        })
        new = _make_spec(paths={
            "/test": {"post": {
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "outer": {
                            "type": "object",
                            "properties": {
                                "inner": {
                                    "type": "object",
                                    "properties": {
                                        "deep_field": {"type": "integer"},
                                    },
                                },
                            },
                        },
                    },
                }}}},
                "responses": {},
            }}
        })
        diffs = diff_contracts(old, new)
        # Should detect the type change two levels deep
        assert any(
            d.diff_type == "nested_field_type_changed" and "deep_field" in d.field
            for d in diffs
        )

    def test_ref_resolution(self):
        """Test that $ref schemas are properly resolved."""
        old = _make_spec(
            paths={
                "/test": {"post": {
                    "requestBody": {"content": {"application/json": {"schema": {
                        "$ref": "#/components/schemas/Input"
                    }}}},
                    "responses": {},
                }}
            },
            components={"schemas": {"Input": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            }}},
        )
        new = _make_spec(
            paths={
                "/test": {"post": {
                    "requestBody": {"content": {"application/json": {"schema": {
                        "$ref": "#/components/schemas/Input"
                    }}}},
                    "responses": {},
                }}
            },
            components={"schemas": {"Input": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "priority": {"type": "string"},
                },
                "required": ["name", "priority"],
            }}},
        )
        diffs = diff_contracts(old, new)
        assert any(d.diff_type == "field_added_required" and "priority" in d.field for d in diffs)

    def test_empty_specs(self):
        old = _make_spec()
        new = _make_spec()
        assert diff_contracts(old, new) == []

    def test_multilevel_ref_resolution(self):
        """$ref schemas at multiple nesting levels are resolved correctly."""
        old = _make_spec(
            paths={
                "/test": {"post": {
                    "requestBody": {"content": {"application/json": {"schema": {
                        "$ref": "#/components/schemas/Outer"
                    }}}},
                    "responses": {},
                }}
            },
            components={"schemas": {
                "Outer": {
                    "type": "object",
                    "properties": {
                        "inner": {"$ref": "#/components/schemas/Inner"},
                    },
                    "required": ["inner"],
                },
                "Inner": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                },
            }},
        )
        new = _make_spec(
            paths={
                "/test": {"post": {
                    "requestBody": {"content": {"application/json": {"schema": {
                        "$ref": "#/components/schemas/Outer"
                    }}}},
                    "responses": {},
                }}
            },
            components={"schemas": {
                "Outer": {
                    "type": "object",
                    "properties": {
                        "inner": {"$ref": "#/components/schemas/Inner"},
                        "extra": {"type": "string"},
                    },
                    "required": ["inner", "extra"],
                },
                "Inner": {
                    "type": "object",
                    "properties": {"value": {"type": "integer"}},
                },
            }},
        )
        diffs = diff_contracts(old, new)
        assert any(d.diff_type == "field_added_required" and "extra" in d.field for d in diffs)

    def test_empty_request_body_to_populated(self):
        """One side has no request body, the other does."""
        old = _make_spec(paths={
            "/test": {"post": {"responses": {"201": {"description": "Created"}}}}
        })
        new = _make_spec(paths={
            "/test": {"post": {
                "requestBody": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {"name": {"type": "string"}},
                    "required": ["name"],
                }}}},
                "responses": {"201": {"description": "Created"}},
            }}
        })
        diffs = diff_contracts(old, new)
        assert any(d.diff_type == "field_added_required" for d in diffs)

    def test_mixed_parameter_changes(self):
        """Query and header parameters changing simultaneously."""
        old = _make_spec(paths={
            "/test": {"get": {
                "parameters": [
                    {"name": "page", "in": "query", "schema": {"type": "integer"}},
                    {"name": "X-Token", "in": "header", "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "OK"}},
            }}
        })
        new = _make_spec(paths={
            "/test": {"get": {
                "parameters": [
                    {"name": "page", "in": "query", "schema": {"type": "string"}},
                    {"name": "X-Request-Id", "in": "header", "required": True, "schema": {"type": "string"}},
                ],
                "responses": {"200": {"description": "OK"}},
            }}
        })
        diffs = diff_contracts(old, new)
        types = {d.diff_type for d in diffs}
        assert "parameter_type_changed" in types
        assert "parameter_removed" in types
        assert "parameter_added_required" in types

    def test_response_only_changes(self):
        """Changes only in response schema, no request body."""
        old = _make_spec(paths={
            "/test": {"get": {
                "responses": {"200": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "integer"},
                        "status": {"type": "string"},
                    },
                }}}}}
            }}
        })
        new = _make_spec(paths={
            "/test": {"get": {
                "responses": {"200": {"content": {"application/json": {"schema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                    },
                }}}}}
            }}
        })
        diffs = diff_contracts(old, new)
        assert any(d.diff_type == "field_type_changed" for d in diffs)
        assert any(d.diff_type == "field_removed" and "status" in d.field for d in diffs)

    def test_response_array_item_schema_changes_are_detected(self):
        """Array response item changes should count as response field changes."""
        old = _make_spec(paths={
            "/sessions": {"get": {
                "responses": {"200": {"content": {"application/json": {"schema": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "usage": {
                                "type": "object",
                                "properties": {
                                    "cached_tokens": {"type": "integer"},
                                },
                            },
                        },
                    },
                }}}}}
            }}
        })
        new = _make_spec(paths={
            "/sessions": {"get": {
                "responses": {"200": {"content": {"application/json": {"schema": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "usage": {
                                "type": "object",
                                "properties": {
                                    "cache_read_tokens": {"type": "integer"},
                                },
                            },
                        },
                    },
                }}}}}
            }}
        })

        diffs = diff_contracts(old, new)
        assert any(d.diff_type == "nested_field_removed" and "usage.cached_tokens" in d.field for d in diffs)
        assert any(d.diff_type == "nested_field_added" and "usage.cache_read_tokens" in d.field for d in diffs)
