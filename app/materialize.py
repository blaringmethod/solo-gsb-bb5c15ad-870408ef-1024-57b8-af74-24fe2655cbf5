"""Draft 2020-12 ``default`` materialization on a deep-copied document.

The :func:`materialize_defaults` entry point takes a JSON Schema (Draft
2020-12) and a JSON document and writes ``default`` annotations for
object properties the instance is missing. It never overwrites a value
that is present, and a key explicitly holding JSON ``null`` counts as
present and is kept untouched. Every inserted default is itself
recursed into, so nested missing properties/array elements receive
their defaults too. The input document is never modified.

Schema construction reuses :func:`app.validate.build_validator`, so the
same rules apply: the schema must be a valid Draft 2020-12 schema and
may only use document-local ``#`` references; anything else raises
:class:`~app.validate.SchemaValidationError` (HTTP 422, same body shape
as ``POST /validate``).

The walk follows ``properties``, array ``items``, ``$ref`` and
``allOf`` (subschemas applied in declaration order). ``oneOf`` /
``anyOf`` are conditional: the (still-pristine) instance at the path is
validated against each branch, and only when *exactly one* branch
matches are that branch's defaults applied; with zero matches or more
than one match the keyword is skipped and a warning carrying
``instance_path``, ``schema_path`` and ``reason`` is emitted. Other
applicator keywords (``if``/``then``/``else``, ``not``,
``prefixItems``, ``patternProperties`` ...) are not consulted for
defaults.
"""

from __future__ import annotations

import sys
from copy import deepcopy
from typing import Any

import referencing
import referencing.exceptions
from referencing.jsonschema import DRAFT202012

from .jsonutil import escape_pointer
from .validate import (
    SchemaValidationError,
    _SUBSCHEMA_ARRAY_KEYS,
    _SUBSCHEMA_KEYS,
    _SUBSCHEMA_MAP_KEYS,
    build_validator,
    path_sort_key,
)


def _index_schema_nodes(root: Any) -> dict[int, str]:
    """Map every *object* schema node inside ``root`` to its schema pointer.

    The pointer is the JSON Pointer of the node's declaration site inside
    the root schema document, used to populate ``schema_path`` in
    warnings. Boolean schemas are not indexed (they carry no defaults or
    keywords). The walk follows exactly the subschema-bearing keywords,
    so data inside annotations such as ``default`` / ``enum`` is ignored.
    """
    index: dict[int, str] = {}

    def walk(node: Any, location: str) -> None:
        if not isinstance(node, dict):
            return
        index[id(node)] = location
        for key, value in node.items():
            token = escape_pointer(str(key))
            child_location = f"{location}/{token}"
            if key in _SUBSCHEMA_KEYS:
                walk(value, child_location)
            elif key in _SUBSCHEMA_ARRAY_KEYS and isinstance(value, list):
                for i, subschema in enumerate(value):
                    walk(subschema, f"{child_location}/{i}")
            elif key in _SUBSCHEMA_MAP_KEYS and isinstance(value, dict):
                for name, subschema in value.items():
                    walk(subschema, f"{child_location}/{escape_pointer(str(name))}")

    walk(root, "")
    return index


def materialize_defaults(
    schema: Any, document: Any
) -> tuple[Any, list[str], list[dict]]:
    """Write ``default`` values onto a deep copy of ``document``.

    Returns ``(materialized_document, applied_paths, warnings)`` where
    ``applied_paths`` are RFC 6901 pointers of every property that
    received a default (unsorted; the caller sorts them) and each
    warning has ``instance_path`` / ``schema_path`` / ``reason``.

    Raises :class:`SchemaValidationError` exactly as
    :func:`~app.validate.build_validator` does for invalid schemas and
    non-local or unresolvable references.
    """
    # Compiling the validator performs the full legality check and the
    # document-local reference restrictions shared with /validate.
    validator = build_validator(schema)

    root_resource = DRAFT202012.create_resource(schema)
    resolver = referencing.Registry().resolver_with_root(root_resource)
    locations = _index_schema_nodes(schema)

    result = deepcopy(document)
    applied: list[str] = []
    applied_seen: set[str] = set()
    warnings: list[dict] = []
    warnings_seen: set[tuple[str, str, str]] = set()

    # Recursion guards, both keyed by (instance node id, schema node id):
    # * ``frames`` holds the pairs on the active descent -- a pair already
    #   present means this $ref/applicator chain is cyclic for this very
    #   instance node and must stop;
    # * ``done`` holds pairs fully processed once, which suppresses
    #   redundant re-descent through diamond-shaped schemas (filling
    #   defaults is idempotent, so visiting the same pair twice adds
    #   nothing except duplicate warnings).
    frames: set[tuple[int, int]] = set()
    done: set[tuple[int, int]] = set()

    def record_warning(instance_path: str, schema_path: str, reason: str) -> None:
        key = (instance_path, schema_path, reason)
        if key not in warnings_seen:
            warnings_seen.add(key)
            warnings.append(
                {
                    "instance_path": instance_path,
                    "schema_path": schema_path,
                    "reason": reason,
                }
            )

    def resolve_ref(subschema: dict) -> Any:
        ref = subschema.get("$ref")
        if not isinstance(ref, str):
            return None
        try:
            return resolver.lookup(ref).contents
        except referencing.exceptions.Unresolvable as exc:  # pragma: no cover
            # build_validator already statically resolved every reference.
            raise SchemaValidationError(
                "unresolvable reference",
                f"a local reference cannot be resolved: {str(exc).splitlines()[0]}",
                ref,
            ) from exc

    def schema_location_of(node: Any, fallback: str) -> str:
        if isinstance(node, dict):
            return locations.get(id(node), fallback)
        return fallback

    def matches(branch: Any, instance_snapshot: Any) -> bool:
        """True iff ``instance_snapshot`` validates against ``branch``."""
        branch_validator = validator.evolve(schema=branch)
        return next(branch_validator.iter_errors(instance_snapshot), None) is None

    def visit(node: Any, subschema: Any, path: str) -> None:
        if not isinstance(subschema, dict):
            # Boolean schema: true changes nothing, false forbids the
            # instance but carries no defaults either way.
            return

        pair = (id(node), id(subschema))
        if pair in frames or pair in done:
            return
        frames.add(pair)
        try:
            # The "original instance" oneOf/anyOf branches must be judged
            # against: snapshot it before anything at this schema node writes.
            snapshot = (
                deepcopy(node)
                if "oneOf" in subschema or "anyOf" in subschema
                else node
            )

            # $ref first: Draft 2020-12 treats it as an ordinary applicator,
            # so the referenced target's defaults apply alongside the
            # sibling keywords handled below.
            target = resolve_ref(subschema)
            if target is not None:
                visit(node, target, path)

            # allOf in declaration order: earlier branch defaults are visible
            # to later branches at the same path.
            all_of = subschema.get("allOf")
            if isinstance(all_of, list):
                for branch in all_of:
                    visit(node, branch, path)

            # oneOf/anyOf apply a branch only when the pristine instance
            # matches exactly one of them.
            for keyword in ("oneOf", "anyOf"):
                branches = subschema.get(keyword)
                if not isinstance(branches, list):
                    continue
                matched = [
                    i for i, branch in enumerate(branches) if matches(branch, snapshot)
                ]
                if len(matched) == 1:
                    visit(node, branches[matched[0]], path)
                else:
                    if not matched:
                        reason = (
                            f"{keyword}: instance matches none of "
                            f"{len(branches)} branches"
                        )
                    else:
                        indices = ",".join(str(i) for i in matched)
                        reason = (
                            f"{keyword}: instance matches {len(matched)} branches "
                            f"(indices {indices}); skipping all"
                        )
                    record_warning(
                        path,
                        f"{schema_location_of(subschema, '')}/{keyword}",
                        reason,
                    )

            # Object properties: fill missing keys from "default", then
            # recurse into every present property declared as an object schema.
            properties = subschema.get("properties")
            if isinstance(node, dict) and isinstance(properties, dict):
                for name, prop_schema in properties.items():
                    if name not in node:
                        if isinstance(prop_schema, dict) and "default" in prop_schema:
                            # A deep copy so repeated uses of one schema node
                            # never share the inserted value.
                            node[name] = deepcopy(prop_schema["default"])
                            prop_path = f"{path}/{escape_pointer(str(name))}"
                            if prop_path not in applied_seen:
                                applied_seen.add(prop_path)
                                applied.append(prop_path)
                        else:
                            # Absent and no default: nothing to recurse into.
                            continue
                    # Present (possibly just inserted): descend with the
                    # property subschema, which also applies the defaults
                    # nested inside the fresh value.
                    visit(
                        node[name],
                        prop_schema,
                        f"{path}/{escape_pointer(str(name))}",
                    )

            # Array items: a single items schema applies to every element.
            items = subschema.get("items")
            if isinstance(node, list) and isinstance(items, dict):
                for i, element in enumerate(node):
                    visit(element, items, f"{path}/{i}")
        finally:
            frames.discard(pair)
            done.add(pair)

    previous_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(previous_limit, 10000))
    try:
        visit(result, schema, "")
    except RecursionError as exc:
        raise SchemaValidationError(
            "unresolvable reference",
            "reference resolution cycles or the document is nested too deeply",
        ) from exc
    finally:
        sys.setrecursionlimit(previous_limit)

    applied.sort(key=path_sort_key)
    warnings.sort(
        key=lambda w: (
            path_sort_key(w["instance_path"]),
            path_sort_key(w["schema_path"]),
            w["reason"],
        )
    )
    return result, applied, warnings
