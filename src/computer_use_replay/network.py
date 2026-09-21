"""Explicit request grants for browser-driven single-page applications."""

from __future__ import annotations

import json
from urllib.parse import parse_qsl

from pydantic import Field, PrivateAttr, model_serializer, model_validator

from computer_use_replay.contracts import Input, Strict


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


class RequestRule(Strict):
    _path: Input = PrivateAttr()
    path: str = Field(min_length=1, max_length=200)
    methods: tuple[str, ...] = ("GET",)
    query_keys: tuple[str, ...] = ()
    body_keys: tuple[str, ...] = ()
    body_equals: dict[str, str | int | bool] = Field(default_factory=dict)
    query_values: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    body_values: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    discard: bool = False
    # A server-rendered (non-SPA) target's own redirect, reviewed one hop at a
    # time. Empty by default: every existing rule keeps failing closed on any
    # redirect, exactly as before this field existed.
    redirect_to: tuple[str, ...] = ()

    @model_serializer(mode="wrap")
    def serialized(self, handler):
        data = handler(self)
        for field in ("query_values", "body_values", "redirect_to"):
            if not data[field]:
                del data[field]
        return data

    @model_validator(mode="after")
    def shape(self):
        self._path = Input(kind="identifier", max_length=1000, pattern=self.path)
        if not self.methods or set(self.methods) - {"GET", "POST"}:
            raise ValueError("request rule requires a path pattern and supported methods")
        if any(key.split(".")[0] not in self.body_keys for key in self.body_equals):
            raise ValueError("body constraints require declared fields")
        if self.redirect_to and (
            self.discard
            or len(set(self.redirect_to)) != len(self.redirect_to)
            or any(not d.startswith("/") or d.startswith("//") for d in self.redirect_to)
        ):
            raise ValueError(
                "redirect grants require distinct relative destinations on a live rule"
            )
        for constraints, keys in (
            (self.query_values, self.query_keys),
            (self.body_values, self.body_keys),
        ):
            if not constraints.keys() <= set(keys) or any(not v for v in constraints.values()):
                raise ValueError("optional value constraints require declared fields and values")
        return self

    def permits_redirect(self, path):
        return path in self.redirect_to

    def matches(self, path, method, query):
        try:
            self._path.validate_value(path)
        except ValueError:
            return False
        if method not in self.methods:
            return False
        pairs = parse_qsl(query, keep_blank_values=True, max_num_fields=128)
        keys = [key for key, _ in pairs]
        return (
            len(keys) == len(set(keys))
            and set(keys) <= set(self.query_keys)
            and all(
                key not in self.query_values or value in self.query_values[key]
                for key, value in pairs
            )
        )

    def permits_body(self, body):
        if len(body) > 1024 * 1024:
            return False
        try:
            if body.lstrip().startswith("{"):
                values = json.loads(body, object_pairs_hook=_unique_object)
            else:
                pairs = parse_qsl(body, keep_blank_values=True, max_num_fields=128)
                values = dict(pairs)
                if len(values) != len(pairs):
                    return False
            if not isinstance(values, dict) or set(values) - set(self.body_keys):
                return False
            if any(
                key in values and values[key] not in allowed
                for key, allowed in self.body_values.items()
            ):
                return False
            for path, expected in self.body_equals.items():
                parts = path.split(".")
                actual = values[parts[0]]
                for key in parts[1:]:
                    if isinstance(actual, str):
                        actual = json.loads(actual, object_pairs_hook=_unique_object)
                    actual = actual[key]
                if type(actual) is not type(expected) or actual != expected:
                    return False
            return True
        except (ValueError, KeyError, TypeError, RecursionError):
            return False
