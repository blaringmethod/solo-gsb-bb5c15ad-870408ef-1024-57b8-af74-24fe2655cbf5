"""Tests for ``POST /materialize-defaults`` (Draft 2020-12 defaults)."""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def materialize(schema, document):
    return client.post(
        "/materialize-defaults", json={"schema": schema, "document": document}
    )


# ------------------------------------------------------------- basic fills


def test_fills_missing_object_properties_from_default():
    schema = {
        "type": "object",
        "properties": {
            "a": {"type": "integer", "default": 1},
            "b": {"type": "string", "default": "x"},
        },
    }
    r = materialize(schema, {})
    assert r.status_code == 200
    assert r.json() == {
        "document": {"a": 1, "b": "x"},
        "applied_paths": ["/a", "/b"],
        "warnings": [],
    }


def test_present_values_are_never_overwritten():
    schema = {
        "type": "object",
        "properties": {
            "a": {"default": 1},
            "b": {"default": 2},
            "c": {"default": 3},
        },
    }
    # Present scalar, present object and present explicit null all stay.
    doc = {"a": 99, "b": {"nested": True}, "c": None}
    r = materialize(schema, doc)
    assert r.status_code == 200
    assert r.json()["document"] == {"a": 99, "b": {"nested": True}, "c": None}
    assert r.json()["applied_paths"] == []


def test_missing_key_with_default_null_is_inserted_as_null():
    # "missing" differs from "explicitly null": a missing key gets the
    # default even when the default itself is null.
    schema = {"properties": {"a": {"default": None}}}
    r = materialize(schema, {})
    assert r.json()["document"] == {"a": None}
    assert r.json()["applied_paths"] == ["/a"]


def test_input_document_is_not_mutated_and_inserted_defaults_are_deep_copies():
    schema = {
        "properties": {
            "a": {"type": "object", "default": {},
                  "properties": {"k": {"default": 1}}},
            "b": {"type": "object", "default": {},
                  "properties": {"k": {"default": 1}}},
        }
    }
    doc = {}
    r = materialize(schema, doc)
    body = r.json()
    assert doc == {}  # untouched
    # Two insertions of the same default literal are independent copies.
    assert body["document"]["a"] is not body["document"]["b"]
    body["document"]["a"]["k"] = 999
    assert body["document"]["b"]["k"] == 1


def test_defaults_apply_regardless_of_required():
    # "required" changes validation, not whether defaults are written.
    schema = {
        "type": "object",
        "required": ["a"],
        "properties": {"a": {"default": 1}, "b": {"default": 2}},
    }
    r = materialize(schema, {})
    assert r.json()["document"] == {"a": 1, "b": 2}
    assert r.json()["applied_paths"] == ["/a", "/b"]


def test_no_default_no_fill():
    schema = {"properties": {"a": {"type": "integer"}}}
    r = materialize(schema, {})
    assert r.json() == {"document": {}, "applied_paths": [], "warnings": []}


def test_recurse_into_inserted_default():
    schema = {
        "type": "object",
        "properties": {
            "p": {
                "type": "object",
                "default": {},
                "properties": {"q": {"default": {"r": 1}}},
            }
        },
    }
    r = materialize(schema, {})
    assert r.json()["document"] == {"p": {"q": {"r": 1}}}
    assert r.json()["applied_paths"] == ["/p", "/p/q"]


def test_recurse_into_existing_values():
    schema = {
        "properties": {
            "p": {"type": "object", "properties": {"q": {"default": 1}}}
        }
    }
    r = materialize(schema, {"p": {}})
    assert r.json()["document"] == {"p": {"q": 1}}
    assert r.json()["applied_paths"] == ["/p/q"]


# ------------------------------------------------------------- arrays


def test_array_items_recurse_for_every_present_element():
    schema = {
        "type": "array",
        "items": {
            "type": "object",
            "properties": {"k": {"default": "v"}},
        },
    }
    r = materialize(schema, [{}, {"k": "mine"}])
    assert r.json()["document"] == [{"k": "v"}, {"k": "mine"}]
    assert r.json()["applied_paths"] == ["/0/k"]


def test_nested_arrays():
    schema = {
        "type": "array",
        "items": {
            "type": "array",
            "items": {"type": "object", "properties": {"k": {"default": 1}}},
        },
    }
    r = materialize(schema, [[{}], [{}, {"k": 2}]])
    assert r.json()["document"] == [[{"k": 1}], [{"k": 1}, {"k": 2}]]
    assert r.json()["applied_paths"] == ["/0/0/k", "/1/0/k"]


# ------------------------------------------------------------- references


def test_local_pointer_ref_applies_target_defaults():
    schema = {
        "$defs": {"base": {"properties": {"x": {"default": 1}}}},
        "$ref": "#/$defs/base",
        "properties": {"y": {"default": 2}},
    }
    r = materialize(schema, {})
    assert r.json()["document"] == {"x": 1, "y": 2}
    assert r.json()["applied_paths"] == ["/x", "/y"]


def test_ref_inside_properties():
    schema = {
        "$defs": {"name": {"properties": {"first": {"default": "?"}}}},
        "properties": {"who": {"$ref": "#/$defs/name"}},
    }
    r = materialize(schema, {"who": {}})
    assert r.json()["document"] == {"who": {"first": "?"}}
    assert r.json()["applied_paths"] == ["/who/first"]


def test_recursive_ref_walks_arbitrary_depth_and_terminates():
    schema = {
        "$defs": {
            "node": {
                "type": "object",
                "properties": {
                    "v": {"default": 0},
                    "children": {
                        "type": "array",
                        "items": {"$ref": "#/$defs/node"},
                    },
                },
            }
        },
        "$ref": "#/$defs/node",
    }
    doc = {"children": [{"v": 1}, {"children": [{"children": []}]}]}
    r = materialize(schema, doc)
    # "children" itself has no default: it is never inserted where
    # missing, and existing (empty) arrays are simply left in place.
    assert r.json()["document"] == {
        "v": 0,
        "children": [
            {"v": 1},
            {"v": 0, "children": [{"v": 0, "children": []}]},
        ],
    }
    assert r.json()["applied_paths"] == [
        "/children/1/children/0/v",
        "/children/1/v",
        "/v",
    ]


def test_self_referential_schema_does_not_loop():
    schema = {"$ref": "#", "properties": {"a": {"default": 1}}}
    r = materialize(schema, {})
    assert r.status_code == 200
    assert r.json()["document"] == {"a": 1}


def test_plain_name_anchor_ref():
    schema = {
        "$id": "https://example.test/root",
        "$defs": {"a": {"$anchor": "a", "properties": {"n": {"default": 1}}}},
        "properties": {"p": {"$ref": "#a"}},
    }
    r = materialize(schema, {"p": {}})
    assert r.json()["document"] == {"p": {"n": 1}}


# ------------------------------------------------------------- allOf


def test_all_of_runs_in_declaration_order_and_first_default_wins():
    schema = {
        "allOf": [
            {"properties": {"s": {"default": "first"},
                            "a": {"default": 1}}},
            {"properties": {"s": {"default": "second"},
                            "b": {"default": 2}}},
        ]
    }
    r = materialize(schema, {})
    assert r.json()["document"] == {"s": "first", "a": 1, "b": 2}
    assert r.json()["applied_paths"] == ["/a", "/b", "/s"]


def test_all_of_branch_can_contain_ref_and_nested_defaults():
    schema = {
        "$defs": {"base": {"properties": {"x": {"default": 1}}}},
        "allOf": [{"$ref": "#/$defs/base"}],
        "properties": {"y": {"type": "object", "default": {},
                             "properties": {"z": {"default": 2}}}},
    }
    r = materialize(schema, {})
    assert r.json()["document"] == {"x": 1, "y": {"z": 2}}
    assert r.json()["applied_paths"] == ["/x", "/y", "/y/z"]


# ------------------------------------------------------------- oneOf/anyOf


def _disjoint_choice():
    return {
        "oneOf": [
            {
                "type": "object",
                "required": ["a"],
                "properties": {"a": {"default": 1}, "x": {"default": 10}},
            },
            {
                "type": "object",
                "required": ["b"],
                "properties": {"b": {"default": 2}, "y": {"default": 20}},
            },
        ]
    }


def test_one_of_exactly_one_match_applies_that_branch():
    r = materialize(_disjoint_choice(), {"a": 0})
    assert r.status_code == 200
    assert r.json()["document"] == {"a": 0, "x": 10}
    assert r.json()["applied_paths"] == ["/x"]
    assert r.json()["warnings"] == []


def test_one_of_no_match_warns_and_applies_nothing():
    r = materialize(_disjoint_choice(), {})
    assert r.status_code == 200
    assert r.json()["document"] == {}
    assert r.json()["applied_paths"] == []
    (warning,) = r.json()["warnings"]
    assert warning["instance_path"] == ""
    assert warning["schema_path"] == "/oneOf"
    assert "none" in warning["reason"]


def test_one_of_multiple_matches_warns_and_applies_nothing():
    r = materialize(_disjoint_choice(), {"a": 1, "b": 2})
    body = r.json()
    assert body["document"] == {"a": 1, "b": 2}
    assert body["applied_paths"] == []
    (warning,) = body["warnings"]
    assert warning["instance_path"] == ""
    assert warning["schema_path"] == "/oneOf"
    assert "2 branches" in warning["reason"]
    assert "0,1" in warning["reason"]


def test_any_of_needs_exactly_one_match_as_well():
    schema = {
        "anyOf": [
            {"type": "object", "properties": {"x": {"default": 1}}},
            {"type": "object", "properties": {"y": {"default": 2}}},
        ]
    }
    # An empty object matches both branches -> ambiguous -> skipped.
    r = materialize(schema, {})
    assert r.json()["document"] == {}
    assert r.json()["warnings"][0]["reason"].startswith("anyOf")
    # A string matches neither.
    r = materialize(schema, "scalar")
    assert r.json()["document"] == "scalar"
    assert "none" in r.json()["warnings"][0]["reason"]
    # {"x": 1} still matches branch 0 AND branch 1 (additional properties
    # allowed), so disjointness must come from the schema itself.
    strict = {
        "anyOf": [
            {"type": "object", "required": ["x"], "additionalProperties": False,
             "properties": {"x": {"default": 1}, "t": {"default": "t"}}},
            {"type": "object", "required": ["y"], "additionalProperties": False,
             "properties": {"y": {"default": 2}}},
        ]
    }
    r = materialize(strict, {"x": 9})
    assert r.json()["document"] == {"x": 9, "t": "t"}
    assert r.json()["applied_paths"] == ["/t"]
    assert r.json()["warnings"] == []


def test_branch_match_uses_the_original_instance_not_branch_defaults():
    # Branch 0 would default "a" into existence; if branch defaults fed the
    # matching, every branch could self-satisfy "required" and matching
    # would become ambiguous. Matching must see only the original instance.
    schema = {
        "oneOf": [
            {"required": ["a"], "properties": {"a": {"default": 1}}},
            {"required": ["b"], "properties": {"b": {"default": 2}}},
        ]
    }
    r = materialize(schema, {})
    body = r.json()
    assert body["document"] == {}
    assert body["applied_paths"] == []
    assert "none" in body["warnings"][0]["reason"]


def test_warning_paths_point_at_instance_and_declaration_site():
    schema = {
        "$defs": {
            "choice": {"oneOf": [{"type": "string"}, {"type": "integer"}]}
        },
        "properties": {"p": {"$ref": "#/$defs/choice"}},
    }
    r = materialize(schema, {"p": None})
    (warning,) = r.json()["warnings"]
    assert warning["instance_path"] == "/p"
    assert warning["schema_path"] == "/$defs/choice/oneOf"


def test_fills_at_ancestor_nodes_are_visible_when_descending():
    # A default written by the parent schema is part of the instance when
    # the child subschema judges its oneOf. (Fills made at the *same*
    # schema node, on the other hand, must not influence that node's own
    # oneOf: branches are judged on the original instance.)
    schema = {
        "properties": {
            "child": {
                "allOf": [{"properties": {"tag": {"default": "t"}}}],
                "oneOf": [
                    {"required": ["tag"],
                     "properties": {"tag": {"default": "t"},
                                    "v": {"default": 1}}},
                    {"required": ["other"]},
                ],
            }
        }
    }
    # The child itself exists (no fill needed); allOf at the child node
    # fills "tag" before oneOf at that same node is judged -> the oneOf
    # still sees the original child instance (without "tag") -> warning.
    r = materialize(schema, {"child": {}})
    body = r.json()
    assert body["document"] == {"child": {"tag": "t"}}
    assert body["applied_paths"] == ["/child/tag"]
    assert len(body["warnings"]) == 1
    assert body["warnings"][0]["instance_path"] == "/child"

    # Parent fills the whole child from a default; the recursed visit of
    # that fresh value then sees the allOf-written tag for the oneOf.
    schema2 = {
        "properties": {
            "child": {
                "default": {},
                "allOf": [{"properties": {"tag": {"default": "t"}}}],
                "oneOf": [
                    {"required": ["tag"],
                     "properties": {"tag": {"default": "t"},
                                    "v": {"default": 1}}},
                    {"required": ["other"]},
                ],
            }
        }
    }
    r = materialize(schema2, {})
    body = r.json()
    assert body["document"] == {"child": {"tag": "t"}}
    assert body["applied_paths"] == ["/child", "/child/tag"]
    assert len(body["warnings"]) == 1


def test_warnings_are_deduplicated_and_sorted():
    schema = {
        "type": "object",
        "properties": {
            "z": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
            "a": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
        },
    }
    r = materialize(schema, {"z": True, "a": True})
    assert [w["instance_path"] for w in r.json()["warnings"]] == ["/a", "/z"]


# ------------------------------------------------------------- path ordering


def test_applied_paths_follow_rfc6901_sort_order():
    # Array indices numeric and before member names; names by code point.
    schema = {
        "properties": {
            "z": {"default": 1},
            "list": {
                "type": "array",
                "items": {"properties": {"v": {"default": True}}},
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


def test_special_characters_in_applied_paths_are_rfc6901_escaped():
    schema = {
        "properties": {"a/b": {"properties": {"~x": {"default": 1}}}}
    }
    r = materialize(schema, {"a/b": {}})
    assert r.json()["applied_paths"] == ["/a~1b/~0x"]


# ------------------------------------------------------------- misc


def test_boolean_schemas_change_nothing():
    assert materialize(True, {"x": 1}).json() == {
        "document": {"x": 1}, "applied_paths": [], "warnings": []
    }
    assert materialize(False, {"x": 1}).json() == {
        "document": {"x": 1}, "applied_paths": [], "warnings": []
    }


def test_root_default_is_not_materialized():
    # The endpoint always receives a document; there is no "missing root".
    r = materialize({"default": {"x": 1}, "properties": {}}, None)
    assert r.json()["document"] is None
    assert r.json()["applied_paths"] == []


def test_non_object_instance_skips_properties_without_error():
    r = materialize({"properties": {"a": {"default": 1}}}, "scalar")
    assert r.json()["document"] == "scalar"
    assert r.json()["applied_paths"] == []


# ------------------------------------------------------------- 422 cases


def test_remote_reference_is_422_with_validate_body_shape():
    r = materialize({"$ref": "https://evil.example/schema"}, {})
    assert r.status_code == 422
    body = r.json()
    assert body["detail"] == "invalid schema"
    assert body["reason"] == "remote reference"
    assert body["ref"] == "https://evil.example/schema"
    assert "message" in body


def test_unresolvable_local_reference_is_422():
    r = materialize({"properties": {"x": {"$ref": "#/$defs/nope"}}}, {})
    assert r.status_code == 422
    body = r.json()
    assert body["reason"] == "unresolvable reference"
    assert body["ref"] == "#/$defs/nope"


def test_invalid_schema_is_422():
    r = materialize({"type": "nope"}, {})
    assert r.status_code == 422
    assert r.json()["reason"] == "invalid schema"


def test_non_object_schema_is_422():
    r = materialize("not-a-schema", {})
    assert r.status_code == 422
    assert r.json()["reason"] == "invalid schema"


def test_remote_ref_nested_inside_oneof_branch_is_422():
    schema = {
        "properties": {
            "x": {"oneOf": [{"$ref": "http://evil.example/s"}]}
        }
    }
    r = materialize(schema, {"x": {}})
    assert r.status_code == 422
    assert r.json()["reason"] == "remote reference"


# ------------------------------------------------------------- request shape


def test_missing_document_field_is_422():
    r = client.post("/materialize-defaults", json={"schema": {}})
    assert r.status_code == 422


def test_missing_schema_field_is_422():
    r = client.post("/materialize-defaults", json={"document": 1})
    assert r.status_code == 422


def test_malformed_json_body_is_422():
    r = client.post(
        "/materialize-defaults",
        content="{not valid json",
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422
