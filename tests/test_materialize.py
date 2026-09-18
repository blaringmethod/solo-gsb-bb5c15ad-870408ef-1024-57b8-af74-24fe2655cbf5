"""Tests for ``POST /materialize-defaults`` (Draft 2020-12 default filling)."""

import warnings

# Pydantic 2 emits a benign warning because Field(alias=...) is used on the
# shared request model; keep test output clean.
warnings.filterwarnings("ignore")

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def materialize(schema, document):
    return client.post(
        "/materialize-defaults", json={"schema": schema, "document": document}
    )


# ------------------------------------------------------------- basics


def test_fills_missing_properties_with_defaults():
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "integer", "default": 1},
            "b": {"type": "string", "default": "x"},
        },
    }
    r = materialize(schema, {"a": 5})
    assert r.status_code == 200
    assert r.json() == {
        "document": {"a": 5, "b": "x"},
        "applied_paths": ["/b"],
        "warnings": [],
    }


def test_existing_values_are_never_overwritten():
    schema = {
        "properties": {
            "n": {"default": 1},
            "f": {"default": False},
            "z": {"default": 0},
            "s": {"default": ""},
            "arr": {"default": []},
            "obj": {"default": {}},
        }
    }
    doc = {"n": 42, "f": True, "z": 7, "s": "v", "arr": [1], "obj": {"k": 1}}
    r = materialize(schema, doc)
    assert r.status_code == 200
    assert r.json()["document"] == doc
    assert r.json()["applied_paths"] == []


def test_explicit_null_is_never_overwritten():
    schema = {"properties": {"a": {"default": 1}, "b": {"default": None}}}
    r = materialize(schema, {"a": None, "b": None})
    assert r.json()["document"] == {"a": None, "b": None}
    assert r.json()["applied_paths"] == []


def test_default_null_is_inserted_for_missing_key():
    r = materialize({"properties": {"a": {"default": None}}}, {})
    assert r.json()["document"] == {"a": None}
    assert r.json()["applied_paths"] == ["/a"]


def test_document_and_defaults_are_deep_copied():
    default = {"n": 1}
    schema = {"properties": {"a": {"default": default},
                             "b": {"default": {},
                                   "properties": {"x": {"default": {"n": 1}}}}}}
    original_doc: dict = {}
    r = materialize(schema, original_doc)
    body = r.json()
    assert body["document"] == {"a": {"n": 1}, "b": {"x": {"n": 1}}}
    # The request document was not mutated.
    assert original_doc == {}
    # The schema's own default object was not shared into the document.
    assert default == {"n": 1}
    body["document"]["a"]["n"] = 99
    again = materialize(schema, {})
    assert again.json()["document"]["a"] == {"n": 1}


def test_root_level_default_does_not_replace_document():
    r = materialize({"type": "object", "default": {"x": 1}}, None)
    assert r.json() == {"document": None, "applied_paths": [], "warnings": []}


def test_document_may_be_any_json_value():
    assert materialize({"type": "integer"}, 42).json()["document"] == 42
    assert materialize({"type": "null"}, None).json()["document"] is None
    assert materialize({"type": "array"}, []).json()["document"] == []


def test_boolean_schemas_change_nothing():
    assert materialize(True, {"a": 1}).json() == {
        "document": {"a": 1}, "applied_paths": [], "warnings": []
    }
    assert materialize(False, {"a": 1}).json()["document"] == {"a": 1}


# ------------------------------------------------------------- recursion


def test_recurses_into_nested_objects():
    schema = {
        "properties": {
            "outer": {
                "type": "object",
                "properties": {"inner": {"default": "v"}},
            }
        }
    }
    r = materialize(schema, {"outer": {}})
    assert r.json()["document"] == {"outer": {"inner": "v"}}
    assert r.json()["applied_paths"] == ["/outer/inner"]


def test_inserted_default_is_then_recursed_into():
    schema = {
        "properties": {
            "c": {
                "type": "object",
                "default": {},
                "properties": {"x": {"default": 7}},
            }
        }
    }
    r = materialize(schema, {})
    assert r.json()["document"] == {"c": {"x": 7}}
    assert r.json()["applied_paths"] == ["/c", "/c/x"]


def test_inserted_default_array_is_recursed_via_items():
    schema = {
        "properties": {
            "arr": {
                "type": "array",
                "default": [{}, {}],
                "items": {"properties": {"v": {"default": 0}}},
            }
        }
    }
    r = materialize(schema, {})
    assert r.json()["document"] == {"arr": [{"v": 0}, {"v": 0}]}
    assert r.json()["applied_paths"] == ["/arr", "/arr/0/v", "/arr/1/v"]


def test_items_applies_to_every_existing_element():
    schema = {
        "properties": {
            "xs": {
                "type": "array",
                "items": {"properties": {"v": {"default": 0}}},
            }
        }
    }
    r = materialize(schema, {"xs": [{}, {"v": 9}, {}]})
    assert r.json()["document"] == {"xs": [{"v": 0}, {"v": 9}, {"v": 0}]}
    assert r.json()["applied_paths"] == ["/xs/0/v", "/xs/2/v"]


def test_default_value_itself_is_data_not_a_schema():
    # A key literally named "default" inside a default value is inserted as-is;
    # nested materialisation only happens through declared subschemas.
    schema = {"properties": {"c": {"default": {"default": 7}}}}
    r = materialize(schema, {})
    assert r.json()["document"] == {"c": {"default": 7}}
    assert r.json()["applied_paths"] == ["/c"]


def test_deeply_nested_arrays_and_objects():
    schema = {
        "properties": {
            "rows": {
                "items": {
                    "properties": {
                        "cells": {
                            "items": {"properties": {"kind": {"default": "n"}}}
                        }
                    }
                }
            }
        }
    }
    doc = {"rows": [{"cells": [{}, {"kind": "x"}]}, {"cells": [{}]}]}
    r = materialize(schema, doc)
    assert r.json()["document"] == {
        "rows": [
            {"cells": [{"kind": "n"}, {"kind": "x"}]},
            {"cells": [{"kind": "n"}]},
        ]
    }
    assert r.json()["applied_paths"] == [
        "/rows/0/cells/0/kind",
        "/rows/1/cells/0/kind",
    ]


# ------------------------------------------------------------- $ref / allOf


def test_ref_fills_defaults_from_target():
    schema = {
        "$defs": {
            "named": {
                "type": "object",
                "properties": {"first": {"default": "ada"}},
            }
        },
        "$ref": "#/$defs/named",
    }
    r = materialize(schema, {})
    assert r.json()["document"] == {"first": "ada"}
    assert r.json()["applied_paths"] == ["/first"]


def test_ref_inside_items_and_properties():
    schema = {
        "$defs": {"el": {"properties": {"v": {"default": 0}}}},
        "properties": {
            "p": {"$ref": "#/$defs/el"},
            "xs": {"type": "array", "items": {"$ref": "#/$defs/el"}},
        },
    }
    r = materialize(schema, {"p": {}, "xs": [{}, {"v": 5}]})
    assert r.json()["document"] == {
        "p": {"v": 0},
        "xs": [{"v": 0}, {"v": 5}],
    }
    assert r.json()["applied_paths"] == ["/p/v", "/xs/0/v"]


def test_allof_branches_run_in_declaration_order():
    schema = {
        "allOf": [
            {"properties": {"a": {"default": 1},
                            "shared": {"default": "first"}}},
            {"properties": {"b": {"default": 2},
                            "shared": {"default": "second"}}},
        ]
    }
    r = materialize(schema, {})
    # The earlier branch fills a still-missing key; the later branch never
    # overwrites it, but still contributes its own distinct keys.
    assert r.json()["document"] == {"a": 1, "shared": "first", "b": 2}
    assert r.json()["applied_paths"] == ["/a", "/b", "/shared"]


def test_allof_combined_with_local_properties_and_ref():
    schema = {
        "$defs": {"mix": {"properties": {"from_ref": {"default": "r"}}}},
        "properties": {"local": {"default": "L"}},
        "allOf": [
            {"properties": {"from_allof": {"default": "A"}}},
            {"$ref": "#/$defs/mix"},
        ],
    }
    r = materialize(schema, {})
    assert r.json()["document"] == {
        "local": "L",
        "from_allof": "A",
        "from_ref": "r",
    }
    assert r.json()["applied_paths"] == ["/from_allof", "/from_ref", "/local"]


def test_self_referential_schema_terminates():
    # {"$ref": "#"} must not recurse forever.
    r = materialize({"$ref": "#"}, {"a": 1})
    assert r.status_code == 200
    assert r.json()["document"] == {"a": 1}


def test_recursive_tree_schema_is_walked_at_every_level():
    schema = {
        "$defs": {
            "node": {
                "properties": {
                    "value": {"default": 0},
                    "children": {
                        "type": "array",
                        "items": {"$ref": "#/$defs/node"},
                    },
                }
            }
        },
        "$ref": "#/$defs/node",
    }
    doc = {"children": [{"value": 1}, {"children": [{}]}]}
    r = materialize(schema, doc)
    assert r.json()["document"] == {
        "value": 0,
        "children": [
            {"value": 1},
            {"value": 0, "children": [{"value": 0}]},
        ],
    }
    assert r.json()["applied_paths"] == [
        "/children/1/children/0/value",
        "/children/1/value",
        "/value",
    ]
    # The input document was not mutated by the walk.
    assert doc == {"children": [{"value": 1}, {"children": [{}]}]}


def test_allof_self_cycle_terminates():
    r = materialize({"allOf": [{"$ref": "#"}]}, {})
    assert r.status_code == 200
    assert r.json()["document"] == {}


# ------------------------------------------------------------- oneOf / anyOf


def test_oneof_applies_unique_matching_branch():
    schema = {
        "oneOf": [
            {"type": "object", "required": ["kind"],
             "properties": {"a_only": {"default": 1}}},
            {"type": "array"},
        ]
    }
    r = materialize(schema, {"kind": "x"})
    assert r.json()["document"] == {"kind": "x", "a_only": 1}
    assert r.json()["applied_paths"] == ["/a_only"]
    assert r.json()["warnings"] == []


def test_oneof_zero_matches_warns_and_changes_nothing():
    schema = {
        "oneOf": [
            {"type": "object", "required": ["a"],
             "properties": {"x": {"default": 1}}},
            {"type": "object", "required": ["b"],
             "properties": {"x": {"default": 2}}},
        ]
    }
    r = materialize(schema, 5)
    assert r.json()["document"] == 5
    assert r.json()["applied_paths"] == []
    warnings_ = r.json()["warnings"]
    assert len(warnings_) == 1
    assert warnings_[0]["instance_path"] == ""
    assert warnings_[0]["schema_path"] == "/oneOf"
    assert "0 of 2" in warnings_[0]["reason"]


def test_oneof_multiple_matches_warns_and_changes_nothing():
    # Without required, both object branches match {}: ambiguous -> skip.
    schema = {
        "oneOf": [
            {"properties": {"x": {"default": 1}}},
            {"properties": {"y": {"default": 2}}},
        ]
    }
    r = materialize(schema, {})
    assert r.json()["document"] == {}
    assert r.json()["applied_paths"] == []
    warnings_ = r.json()["warnings"]
    assert len(warnings_) == 1
    assert warnings_[0]["schema_path"] == "/oneOf"
    assert "2 of 2" in warnings_[0]["reason"]
    assert "indices 0, 1" in warnings_[0]["reason"]


def test_anyof_uses_same_exactly_one_rule():
    schema = {
        "anyOf": [
            {"type": "string"},
            {"type": "object", "properties": {"x": {"default": 1}}},
        ]
    }
    unique = materialize(schema, {})
    assert unique.json()["document"] == {"x": 1}
    assert unique.json()["warnings"] == []

    ambiguous_schema = {
        "anyOf": [
            {"properties": {"x": {"default": 1}}},
            {"properties": {"y": {"default": 2}}},
        ]
    }
    ambiguous = materialize(ambiguous_schema, {})
    assert ambiguous.json()["document"] == {}
    assert ambiguous.json()["warnings"][0]["schema_path"] == "/anyOf"
    assert "2 of 2" in ambiguous.json()["warnings"][0]["reason"]


def test_branch_reference_resolves_against_request_schema():
    schema = {
        "$defs": {"o": {"type": "object",
                        "properties": {"x": {"default": 1}}}},
        "oneOf": [{"$ref": "#/$defs/o"}, {"type": "array"}],
    }
    r = materialize(schema, {})
    assert r.json()["document"] == {"x": 1}
    assert r.json()["applied_paths"] == ["/x"]
    # The array instance uniquely matches the second branch (no defaults).
    r2 = materialize(schema, [])
    assert r2.json()["document"] == []
    assert r2.json()["warnings"] == []


def test_branch_defaults_recurse_through_items():
    schema = {
        "oneOf": [
            {"type": "object",
             "properties": {"xs": {"items": {"properties": {
                 "v": {"default": 0}}}}}},
            {"type": "array"},
        ]
    }
    r = materialize(schema, {"xs": [{}, {"v": 3}]})
    assert r.json()["document"] == {"xs": [{"v": 0}, {"v": 3}]}
    assert r.json()["applied_paths"] == ["/xs/0/v"]


def test_cyclic_branch_is_treated_as_non_match():
    # A self-referential branch cannot be evaluated; that branch simply does
    # not match (the request itself still succeeds).
    r = materialize({"oneOf": [{"$ref": "#"}]}, 1)
    assert r.status_code == 200
    assert r.json()["document"] == 1
    assert r.json()["warnings"][0]["reason"].startswith("oneOf skipped")
    assert "0 of 1" in r.json()["warnings"][0]["reason"]


def test_warning_schema_path_points_through_properties():
    schema = {
        "properties": {
            "p": {"oneOf": [{"type": "integer"}, {"type": "string"}]}
        }
    }
    r = materialize(schema, {"p": []})
    w = r.json()["warnings"]
    assert w[0] == {
        "instance_path": "/p",
        "schema_path": "/properties/p/oneOf",
        "reason": w[0]["reason"],
    }


def test_warning_schema_path_points_through_ref_inline():
    # Crossing a $ref resolves inline (as in /validate schema paths): the
    # oneOf inside the referenced target is reported at the referencing site.
    schema = {
        "$defs": {"s": {"oneOf": [{"type": "integer"}, {"type": "string"}]}},
        "$ref": "#/$defs/s",
    }
    r = materialize(schema, [])
    w = r.json()["warnings"]
    assert len(w) == 1
    assert w[0]["instance_path"] == ""
    assert w[0]["schema_path"] == "/oneOf"


def test_warnings_are_sorted_and_deduplicated():
    schema = {
        "properties": {
            "z": {"oneOf": [{"type": "integer"}, {"type": "string"}]},
            "list": {"items": {"oneOf": [{"type": "integer"},
                                         {"type": "string"}]}},
            "a": {"oneOf": [{"type": "integer"}, {"type": "string"}]},
        }
    }
    r = materialize(schema, {"z": [], "list": [[], []], "a": []})
    paths = [(w["instance_path"], w["schema_path"])
             for w in r.json()["warnings"]]
    # Member names code-point order; array indices numeric and before names.
    assert paths == [
        ("/a", "/properties/a/oneOf"),
        ("/list/0", "/properties/list/items/oneOf"),
        ("/list/1", "/properties/list/items/oneOf"),
        ("/z", "/properties/z/oneOf"),
    ]


# ------------------------------------------------------------- ordering / escaping


def test_applied_paths_sorted_like_validate():
    schema = {
        "properties": {
            "z": {"default": 1},
            "list": {
                "type": "array",
                "items": {"properties": {"v": {"default": 0}}},
            },
            "a": {"default": 2},
        }
    }
    r = materialize(schema, {"list": [{}, {}, {}]})
    assert r.json()["applied_paths"] == [
        "/a",
        "/list/0/v",
        "/list/1/v",
        "/list/2/v",
        "/z",
    ]


def test_applied_paths_are_rfc6901_escaped_and_unique():
    schema = {
        "properties": {
            "a/b": {"default": 1},
            "~z": {"default": 2},
        }
    }
    r = materialize(schema, {})
    assert r.json()["applied_paths"] == ["/a~1b", "/~0z"]


def test_same_path_is_reported_once_even_when_seen_via_two_keywords():
    # /x is filled by local properties; a later allOf branch reaches the same
    # key (now present) and must not duplicate the applied path.
    schema = {
        "properties": {"x": {"default": 1}},
        "allOf": [{"properties": {"x": {"default": 2},
                                  "y": {"default": 3}}}],
    }
    r = materialize(schema, {})
    assert r.json()["document"] == {"x": 1, "y": 3}
    assert r.json()["applied_paths"] == ["/x", "/y"]


# ------------------------------------------------------------- 422 cases


def test_remote_reference_is_422_like_validate():
    r = materialize({"$ref": "https://example.com/schema"}, {})
    assert r.status_code == 422
    body = r.json()
    assert body["detail"] == "invalid schema"
    assert body["reason"] == "remote reference"
    assert body["ref"] == "https://example.com/schema"
    assert "message" in body


def test_unresolvable_local_reference_is_422():
    r = materialize({"$ref": "#/$defs/missing"}, {})
    assert r.status_code == 422
    body = r.json()
    assert body["reason"] == "unresolvable reference"
    assert body["ref"] == "#/$defs/missing"


def test_invalid_schema_is_422():
    r = materialize({"type": "nope"}, {})
    assert r.status_code == 422
    assert r.json()["reason"] == "invalid schema"


def test_non_object_schema_is_422():
    r = materialize("not-a-schema", {})
    assert r.status_code == 422
    assert r.json()["reason"] == "invalid schema"


def test_invalid_schema_is_rejected_without_touching_document():
    # Even with a document that would "match", the bad schema is 422.
    assert materialize({"$ref": "#/$defs/missing"}, None).status_code == 422


# ------------------------------------------------------------- request shape


def test_missing_document_field_is_422():
    r = client.post("/materialize-defaults", json={"schema": {"type": "integer"}})
    assert r.status_code == 422


def test_missing_schema_field_is_422():
    r = client.post("/materialize-defaults", json={"document": 1})
    assert r.status_code == 422


def test_malformed_json_body_is_422():
    r = client.post(
        "/materialize-defaults",
        content="{not json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422
