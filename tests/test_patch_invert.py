from fastapi.testclient import TestClient

from app.main import app
from app.jsonutil import json_equal

client = TestClient(app)


def invert(document, operations):
    return client.post(
        "/patch/invert", json={"document": document, "operations": operations}
    )


def roundtrip(document, operations):
    """Apply operations, then the returned inverses, and return both bodies."""
    r = invert(document, operations)
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"document", "inverse_operations"}
    back = client.post(
        "/patch",
        json={"document": body["document"], "operations": body["inverse_operations"]},
    )
    assert back.status_code == 200, back.text
    assert json_equal(back.json()["document"], document)
    return body


# ------------------------------------------------------------- basic shapes


def test_empty_operations_inverts_to_empty_list():
    r = invert({"a": 1}, [])
    assert r.status_code == 200
    assert r.json() == {"document": {"a": 1}, "inverse_operations": []}


def test_add_new_object_key_is_inverted_by_remove():
    body = roundtrip({"a": 1}, [{"op": "add", "path": "/b", "value": 2}])
    assert body == {
        "document": {"a": 1, "b": 2},
        "inverse_operations": [{"op": "remove", "path": "/b"}],
    }


def test_add_over_existing_object_key_restores_overwritten_value():
    body = roundtrip(
        {"a": {"x": 1}}, [{"op": "add", "path": "/a", "value": {"y": 2}}]
    )
    assert body["document"] == {"a": {"y": 2}}
    assert body["inverse_operations"] == [
        {"op": "replace", "path": "/a", "value": {"x": 1}}
    ]


def test_remove_object_key_is_inverted_by_add_with_old_value():
    body = roundtrip(
        {"a": 1, "b": 2}, [{"op": "remove", "path": "/a"}]
    )
    assert body["document"] == {"b": 2}
    assert body["inverse_operations"] == [
        {"op": "add", "path": "/a", "value": 1}
    ]


def test_replace_object_member_restores_old_value():
    body = roundtrip({"a": 1}, [{"op": "replace", "path": "/a", "value": 9}])
    assert body["document"] == {"a": 9}
    assert body["inverse_operations"] == [
        {"op": "replace", "path": "/a", "value": 1}
    ]


def test_replace_entire_subtree_restores_the_whole_old_subtree():
    doc = {"a": {"deep": [1, 2, {"k": True}]}, "b": "keep"}
    body = roundtrip(doc, [{"op": "replace", "path": "/a", "value": None}])
    assert body["document"] == {"a": None, "b": "keep"}
    assert body["inverse_operations"] == [
        {"op": "replace", "path": "/a", "value": {"deep": [1, 2, {"k": True}]}}
    ]


# --------------------------------------------------------------- root ops


def test_add_at_root_inverted_by_replace_with_original_document():
    body = roundtrip({"a": 1}, [{"op": "add", "path": "", "value": [1, 2]}])
    assert body["document"] == [1, 2]
    assert body["inverse_operations"] == [
        {"op": "replace", "path": "", "value": {"a": 1}}
    ]


def test_replace_at_root_inverted_by_replace_with_original_document():
    body = roundtrip(42, [{"op": "replace", "path": "", "value": "x"}])
    assert body["document"] == "x"
    assert body["inverse_operations"] == [
        {"op": "replace", "path": "", "value": 42}
    ]


# --------------------------------------------------------------- arrays


def test_array_append_dash_inverted_by_remove_at_concrete_index():
    body = roundtrip([1, 2], [{"op": "add", "path": "/-", "value": 3}])
    assert body["document"] == [1, 2, 3]
    assert body["inverse_operations"] == [{"op": "remove", "path": "/2"}]


def test_array_insert_at_index_inverted_by_remove_at_same_index():
    body = roundtrip([1, 2, 3], [{"op": "add", "path": "/1", "value": 9}])
    assert body["document"] == [1, 9, 2, 3]
    assert body["inverse_operations"] == [{"op": "remove", "path": "/1"}]


def test_array_remove_inverted_by_add_at_same_index_with_old_element():
    body = roundtrip([1, 2, 3], [{"op": "remove", "path": "/1"}])
    assert body["document"] == [1, 3]
    assert body["inverse_operations"] == [
        {"op": "add", "path": "/1", "value": 2}
    ]


def test_array_replace_inverted_by_replace_with_old_element():
    body = roundtrip([1, 2, 3], [{"op": "replace", "path": "/1", "value": 9}])
    assert body["document"] == [1, 9, 3]
    assert body["inverse_operations"] == [
        {"op": "replace", "path": "/1", "value": 2}
    ]


def test_array_index_shifts_are_handled_across_insert_and_remove():
    operations = [
        {"op": "add", "path": "/0", "value": "a"},
        {"op": "add", "path": "/0", "value": "b"},
        {"op": "remove", "path": "/2"},
        {"op": "add", "path": "/-", "value": "z"},
    ]
    body = roundtrip([1, 2], operations)
    # Forward: [1,2] -> [a,1,2] -> [b,a,1,2] -> [b,a,2] -> [b,a,2,z]
    assert body["document"] == ["b", "a", 2, "z"]
    # Reverse order; the remove at forward index 2 read "1" (post-shift),
    # and the inserts undo from the outermost shift first.
    assert body["inverse_operations"] == [
        {"op": "remove", "path": "/3"},
        {"op": "add", "path": "/2", "value": 1},
        {"op": "remove", "path": "/0"},
        {"op": "remove", "path": "/0"},
    ]


def test_interleaved_array_operations_on_nested_path_roundtrip():
    doc = {"a": [{"v": 1}, {"v": 2}, {"v": 3}], "b": [0]}
    operations = [
        {"op": "add", "path": "/a/1", "value": {"v": 9}},
        {"op": "replace", "path": "/a/0/v", "value": 10},
        {"op": "remove", "path": "/a/3"},
        {"op": "add", "path": "/b/-", "value": 1},
    ]
    body = roundtrip(doc, operations)
    assert body["document"] == {
        "a": [{"v": 10}, {"v": 9}, {"v": 2}],
        "b": [0, 1],
    }
    assert body["inverse_operations"] == [
        {"op": "remove", "path": "/b/1"},
        {"op": "add", "path": "/a/3", "value": {"v": 3}},
        {"op": "replace", "path": "/a/0/v", "value": 1},
        {"op": "remove", "path": "/a/1"},
    ]


# ------------------------------------------------------------------ test


def test_test_contributes_no_inverse_but_still_validates():
    operations = [
        {"op": "test", "path": "/a", "value": 1},
        {"op": "add", "path": "/b", "value": 2},
        {"op": "test", "path": "/b", "value": 2},
    ]
    body = roundtrip({"a": 1}, operations)
    assert body["document"] == {"a": 1, "b": 2}
    assert body["inverse_operations"] == [
        {"op": "remove", "path": "/b"}
    ]


def test_only_test_operations_yield_empty_inverse():
    body = roundtrip(
        {"a": [1, 2]},
        [
            {"op": "test", "path": "", "value": {"a": [1, 2]}},
            {"op": "test", "path": "/a/0", "value": 1},
        ],
    )
    assert body["document"] == {"a": [1, 2]}
    assert body["inverse_operations"] == []


def test_failed_test_is_409_without_partial_results():
    operations = [
        {"op": "add", "path": "/b", "value": 2},
        {"op": "test", "path": "/a", "value": 99},
    ]
    r = invert({"a": 1}, operations)
    assert r.status_code == 409
    body = r.json()
    assert set(body) == {"detail", "failed_index", "path", "reason"}
    assert body["failed_index"] == 1
    assert "document" not in body
    assert "inverse_operations" not in body


# --------------------------------------------------------- real pre-values


def test_inverse_uses_value_before_each_step_not_original_or_input_value():
    operations = [
        {"op": "replace", "path": "/a", "value": 2},
        {"op": "replace", "path": "/a", "value": 3},
        {"op": "replace", "path": "/a", "value": 4},
    ]
    body = roundtrip({"a": 1}, operations)
    assert body["document"] == {"a": 4}
    assert body["inverse_operations"] == [
        {"op": "replace", "path": "/a", "value": 3},
        {"op": "replace", "path": "/a", "value": 2},
        {"op": "replace", "path": "/a", "value": 1},
    ]


def test_added_then_modified_value_is_undone_before_the_add():
    operations = [
        {"op": "add", "path": "/k", "value": {"n": 1}},
        {"op": "replace", "path": "/k/n", "value": 2},
        {"op": "add", "path": "/k/m", "value": 3},
    ]
    body = roundtrip({}, operations)
    assert body["document"] == {"k": {"n": 2, "m": 3}}
    assert body["inverse_operations"] == [
        {"op": "remove", "path": "/k/m"},
        {"op": "replace", "path": "/k/n", "value": 1},
        {"op": "remove", "path": "/k"},
    ]


def test_removed_ancestor_makes_descendant_changes_part_of_restore():
    operations = [
        {"op": "replace", "path": "/a/x", "value": 9},
        {"op": "remove", "path": "/a"},
    ]
    doc = {"a": {"x": 1}, "b": 2}
    body = roundtrip(doc, operations)
    assert body["document"] == {"b": 2}
    assert body["inverse_operations"] == [
        {"op": "add", "path": "/a", "value": {"x": 9}},
        {"op": "replace", "path": "/a/x", "value": 1},
    ]


def test_pointer_escaping_is_preserved_in_inverse_paths():
    body = roundtrip(
        {"a/b": 1, "~x": 2},
        [
            {"op": "remove", "path": "/a~1b"},
            {"op": "add", "path": "/~0y", "value": 3},
        ],
    )
    assert body["document"] == {"~x": 2, "~y": 3}
    assert body["inverse_operations"] == [
        {"op": "remove", "path": "/~0y"},
        {"op": "add", "path": "/a~1b", "value": 1},
    ]


def test_input_document_is_not_mutated():
    doc = {"a": [1, 2]}
    r = invert(doc, [{"op": "add", "path": "/a/-", "value": 3}])
    assert r.status_code == 200
    assert doc == {"a": [1, 2]}


def test_inverse_values_are_deep_copied():
    nested = {"x": [1, 2]}
    r = invert({}, [{"op": "add", "path": "/k", "value": nested}])
    assert r.status_code == 200
    inverse = r.json()["inverse_operations"][0]
    assert inverse == {"op": "remove", "path": "/k"}
    # The forward add carries a deep copy; mutating the request value after
    # the call must not corrupt either document.
    nested["x"].append(3)
    assert r.json()["document"] == {"k": {"x": [1, 2]}}


# ---------------------------------------------------------- error mapping


def test_missing_target_conflict_uses_patch_409_shape():
    r = invert({"a": 1}, [{"op": "remove", "path": "/z"}])
    assert r.status_code == 409
    body = r.json()
    assert body == {
        "detail": "patch conflict",
        "failed_index": 0,
        "path": "/z",
        "reason": "object has no key 'z'",
    }


def test_array_out_of_bounds_conflict_uses_patch_409_shape():
    r = invert([1], [{"op": "remove", "path": "/5"}])
    assert r.status_code == 409
    body = r.json()
    assert body["detail"] == "patch conflict"
    assert body["failed_index"] == 0
    assert body["path"] == "/5"
    assert "bounds" in body["reason"]


def test_malformed_operation_uses_patch_422_shape():
    r = invert({"a": 1}, [{"op": "add", "path": "/a"}])
    assert r.status_code == 422
    body = r.json()
    assert body["detail"] == "invalid patch operation"
    assert body["failed_index"] == 0
    assert body["path"] == "/a"
    assert "reason" in body


def test_unknown_op_is_422():
    r = invert({}, [{"op": "move", "path": "/a", "from": "/b"}])
    assert r.status_code == 422


def test_dash_on_remove_is_422():
    r = invert([1], [{"op": "remove", "path": "/-"}])
    assert r.status_code == 422


def test_failure_halfway_through_returns_nothing():
    operations = [
        {"op": "add", "path": "/b", "value": 2},
        {"op": "add", "path": "/c", "value": 3},
        {"op": "remove", "path": "/missing"},
    ]
    r = invert({"a": 1}, operations)
    assert r.status_code == 409
    body = r.json()
    assert set(body) == {"detail", "failed_index", "path", "reason"}
    assert body["failed_index"] == 2


def test_missing_request_fields_rejected():
    r = client.post("/patch/invert", json={"document": {}})
    assert r.status_code == 422


def test_operations_must_be_array():
    r = client.post(
        "/patch/invert", json={"document": {}, "operations": "x"}
    )
    assert r.status_code == 422


# ------------------------------------------------- larger random-ish checks


def test_mixed_object_and_array_saga_roundtrips_exactly():
    doc = {
        "list": [1, 2, 3, 4],
        "obj": {"keep": {"nested": [True, False]}, "gone": "x"},
        "n": 0,
    }
    operations = [
        {"op": "test", "path": "/n", "value": 0},
        {"op": "add", "path": "/list/2", "value": 99},
        {"op": "remove", "path": "/list/0"},
        {"op": "replace", "path": "/list/3", "value": 40},
        {"op": "add", "path": "/obj/new", "value": [1]},
        {"op": "remove", "path": "/obj/gone"},
        {"op": "replace", "path": "/obj/keep/nested/0", "value": None},
        {"op": "add", "path": "/list/-", "value": 5},
    ]
    body = roundtrip(doc, operations)
    assert body["document"] == {
        "list": [2, 99, 3, 40, 5],
        "obj": {"keep": {"nested": [None, False]}, "new": [1]},
        "n": 0,
    }
    assert len(body["inverse_operations"]) == 7  # test contributes none
