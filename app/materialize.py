"""Materialise JSON Schema ``default`` annotations into a document.

Given a Draft 2020-12 schema and an instance, :func:`materialize_defaults`
returns a deep copy of the instance with missing object properties filled in
from the schema's ``default`` annotations:

* an existing value -- including an explicit JSON ``null`` -- is never
  overwritten; only a genuinely absent object member is filled in;
* recursion follows ``properties`` (one subschema per present/filled member),
  array ``items`` (every element), ``$ref`` and ``allOf`` (each branch in
  declaration order);
* ``oneOf`` / ``anyOf`` contribute defaults only when the instance at that
  point matches *exactly one* branch; with zero or several matches the whole
  keyword is skipped and a warning is recorded;
* a default that is itself an object or array is inserted first and then
  recursed into, so its own nested defaults are filled in too.

The schema is compiled with :func:`app.validate.build_validator`, so the same
rules apply as for ``POST /validate``: an invalid schema, a non-local
(non-``#``) reference or an unresolvable local reference raises
:class:`~app.validate.SchemaValidationError` before the document is touched.
"""

from __future__ import annotations

import copy
import sys
from collections import deque
from typing import Any, Deque

import referencing
import referencing.exceptions
from referencing.jsonschema import DRAFT202012

from .jsonutil import escape_pointer
from .validate import build_validator, pointer_sort_key

# Keywords this walker understands. Everything else (type, if/then/else,
# additionalProperties, prefixItems, ...) carries no defaults and is ignored.
_REF_KEYWORD = "$ref"
_ALLOF_KEYWORD = "allOf"
_ONE_KEYWORDS = ("oneOf", "anyOf")
_PROPERTIES_KEYWORD = "properties"
_ITEMS_KEYWORD = "items"
_DEFAULT_KEYWORD = "default"


def _render_pointer(path: Deque[Any]) -> str:
    return "".join(f"/{escape_pointer(str(token))}" for token in path)


def materialize_defaults(
    schema: Any, document: Any
) -> tuple[Any, list[str], list[dict]]:
    """Fill missing defaults; return ``(document, applied_paths, warnings)``.

    ``document`` is never mutated: it and every inserted default are deep
    copied. ``applied_paths`` and ``warnings`` come back sorted by RFC 6901
    instance path (array indices numerically, before member names).
    """
    # Compiling first reproduces /validate's 422 behaviour for invalid
    # schemas, remote refs and unresolvable local refs.
    validator = build_validator(schema)

    # A resolver rooted at the request schema makes every fragment-only $ref
    # resolve relative to the request document, even from inside a subschema.
    # build_validator already rejected every non-local or unresolvable
    # reference statically, so a plain registry (no network retrieval) is
    # enough here.
    resolver = referencing.Registry().resolver_with_root(
        DRAFT202012.create_resource(schema)
    )

    document = copy.deepcopy(document)
    applied: set[str] = set()
    warnings: list[dict] = []

    previous_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(max(previous_limit, 10000))
    try:
        _walk(
            schema=schema,
            instance=document,
            instance_path=deque(),
            schema_path=(),
            validator=validator,
            resolver=resolver,
            applied=applied,
            warnings=warnings,
            # Frames of (schema node id, container id) currently being walked:
            # revisiting the same schema node against the same container means
            # a self-referential cycle such as {"$ref": "#"} or
            # {"allOf": [{"$ref": "#"}]} -- nothing new can be added.
            active=set(),
        )
    finally:
        sys.setrecursionlimit(previous_limit)

    applied_paths = sorted(applied, key=pointer_sort_key)
    deduped: list[dict] = []
    seen: set[tuple] = set()
    for warning in warnings:
        key = (
            warning["instance_path"],
            warning["schema_path"],
            warning["reason"],
        )
        if key not in seen:
            seen.add(key)
            deduped.append(warning)
    deduped.sort(
        key=lambda w: (
            pointer_sort_key(w["instance_path"]),
            pointer_sort_key(w["schema_path"]),
            w["reason"],
        )
    )
    return document, applied_paths, deduped


def _resolve_ref(resolver, ref: str) -> Any:
    """Resolve a fragment-only ``$ref`` to its target subschema.

    The resolver is rooted at the request schema, so references resolve the
    same from inside any subschema (unlike an evolved validator, which would
    treat a fragment as relative to that subschema's ``$id``).
    """
    return resolver.lookup(ref).contents


def _branch_matches(validator, branch: Any, instance: Any) -> bool:
    """True iff ``instance`` validates against branch schema ``branch``.

    ``descend`` keeps the root validator's resolver, so references inside the
    branch resolve against the request schema. A cyclic branch (e.g.
    ``{"$ref": "#"}`` inside a oneOf) can overflow the stack: treat that as a
    non-match rather than a bad request, mirroring /validate which reports the
    cycle itself as the error only when validation reaches it.
    """
    try:
        return not list(validator.descend(instance, branch))
    except referencing.exceptions.Unresolvable:
        # build_validator already resolved every reference statically, so this
        # should be unreachable; a lazy failure simply means "does not match".
        return False
    except RecursionError:
        return False


def _walk(
    *,
    schema: Any,
    instance: Any,
    instance_path: Deque[Any],
    schema_path: tuple,
    validator,
    resolver,
    applied: set[str],
    warnings: list[dict],
    active: set[tuple[int, int]],
) -> None:
    """Apply defaults described by ``schema`` to ``instance`` in place."""
    if not isinstance(schema, dict):
        # Boolean schemas (true/false) carry no keywords.
        return

    if isinstance(instance, (dict, list)):
        frame = (id(schema), id(instance))
        if frame in active:
            return
        active.add(frame)
    else:
        frame = None

    try:
        # 1. properties: fill missing defaults, then recurse into each present
        #    member (pre-existing or freshly inserted). An existing member --
        #    even one whose value is an explicit null -- is never replaced.
        properties = schema.get(_PROPERTIES_KEYWORD)
        if isinstance(instance, dict) and isinstance(properties, dict):
            for name, subschema in properties.items():
                member_path = instance_path.copy()
                member_path.append(name)
                if name not in instance:
                    if (
                        isinstance(subschema, dict)
                        and _DEFAULT_KEYWORD in subschema
                    ):
                        # Deep copy so the schema's default value is never
                        # shared with the document or mutated by recursion.
                        value = copy.deepcopy(subschema[_DEFAULT_KEYWORD])
                        instance[name] = value
                        applied.add(_render_pointer(member_path))
                if name in instance and isinstance(subschema, dict):
                    _walk(
                        schema=subschema,
                        instance=instance[name],
                        instance_path=member_path,
                        schema_path=schema_path
                        + (_PROPERTIES_KEYWORD, name),
                        validator=validator,
                        resolver=resolver,
                        applied=applied,
                        warnings=warnings,
                        active=active,
                    )

        # 2. items: recurse into every array element.
        items = schema.get(_ITEMS_KEYWORD)
        if isinstance(instance, list) and isinstance(items, dict):
            for index, element in enumerate(instance):
                element_path = instance_path.copy()
                element_path.append(index)
                _walk(
                    schema=items,
                    instance=element,
                    instance_path=element_path,
                    schema_path=schema_path + (_ITEMS_KEYWORD,),
                    validator=validator,
                    resolver=resolver,
                    applied=applied,
                    warnings=warnings,
                    active=active,
                )

        # 3. $ref: descend into the referenced schema. Crossing a reference
        #    adds no tokens to schema_path, matching the schema paths reported
        #    by /validate (jsonschema resolves references inline).
        ref = schema.get(_REF_KEYWORD)
        if isinstance(ref, str):
            target = _resolve_ref(resolver, ref)
            _walk(
                schema=target,
                instance=instance,
                instance_path=instance_path,
                schema_path=schema_path,
                validator=validator,
                resolver=resolver,
                applied=applied,
                warnings=warnings,
                active=active,
            )

        # 4. allOf: every branch is walked in declaration order.
        all_of = schema.get(_ALLOF_KEYWORD)
        if isinstance(all_of, list):
            for index, branch in enumerate(all_of):
                _walk(
                    schema=branch,
                    instance=instance,
                    instance_path=instance_path,
                    schema_path=schema_path + (_ALLOF_KEYWORD, index),
                    validator=validator,
                    resolver=resolver,
                    applied=applied,
                    warnings=warnings,
                    active=active,
                )

        # 5. oneOf / anyOf: a branch contributes defaults only when the
        #    instance at this point matches *exactly one* branch; with zero or
        #    several matches the whole keyword is skipped and a warning is
        #    recorded.
        for keyword in _ONE_KEYWORDS:
            branches = schema.get(keyword)
            if not isinstance(branches, list):
                continue
            matches = [
                index
                for index, branch in enumerate(branches)
                if _branch_matches(validator, branch, instance)
            ]
            keyword_path = _render_pointer(instance_path)
            schema_pointer = _schema_pointer(schema_path + (keyword,))
            if len(matches) == 1:
                index = matches[0]
                _walk(
                    schema=branches[index],
                    instance=instance,
                    instance_path=instance_path,
                    schema_path=schema_path + (keyword, index),
                    validator=validator,
                    resolver=resolver,
                    applied=applied,
                    warnings=warnings,
                    active=active,
                )
            elif not matches:
                warnings.append(
                    {
                        "instance_path": keyword_path,
                        "schema_path": schema_pointer,
                        "reason": (
                            f"{keyword} skipped: the instance matches 0 of "
                            f"{len(branches)} branches; defaults applied only "
                            "when exactly one branch matches"
                        ),
                    }
                )
            else:
                warnings.append(
                    {
                        "instance_path": keyword_path,
                        "schema_path": schema_pointer,
                        "reason": (
                            f"{keyword} skipped: the instance matches "
                            f"{len(matches)} of {len(branches)} branches "
                            f"(indices {', '.join(map(str, matches))}); "
                            "defaults applied only when exactly one branch "
                            "matches"
                        ),
                    }
                )
    finally:
        if frame is not None:
            active.discard(frame)


def _schema_pointer(schema_path: tuple) -> str:
    """Render an in-schema path of keys/indices as an RFC 6901 pointer."""
    return "".join(
        f"/{escape_pointer(str(token))}" for token in schema_path
    )
