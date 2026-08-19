"""Shaping JSON once, at the point where it is cheapest to shape it.

Two ideas live here, and they are the same idea seen from both ends.

The first is that a result set has one shape, not one shape per row. Renaming
snake_case columns to camelCase is a split, a capitalize per word and a join;
doing it per cell turns a few dozen rewrites into hundreds of thousands.

The second is that JSON which is already correct should never be decoded only to
be encoded again. The Cost Data SPIT read model stores its rows in exactly the
shape and key case the response sends, so the fastest thing the request can do
with them is nothing at all: hand the stored bytes to the body untouched. That
is what RawJsonSplicer is for.
"""

import json

from datetime import date, datetime
from decimal import Decimal
from uuid import uuid4


def camel_case(column):
    return "".join(
        word if index == 0 else word.capitalize()
        for index, word in enumerate(column.split("_"))
    )


_key_map_cache = {}


def camel_keys(columns):
    """The camelCase names for one result set's columns, computed once.

    Keyed by the column tuple rather than recomputed per row, because a query
    returns the same columns for every row it will ever return.
    """
    key = tuple(columns)
    names = _key_map_cache.get(key)
    if names is None:
        names = [camel_case(column) for column in key]
        _key_map_cache[key] = names
    return names


def json_value(value):
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def json_rows(rows):
    """Shape a whole result set for JSON.

    Every row in a result set carries the same columns in the same order, so the
    snake_case to camelCase rewrite is done once for the set instead of once per
    cell. On six datasets of a few thousand rows each that was tens of thousands
    of identical string rebuilds per request.
    """
    if not rows:
        return []
    names = camel_keys(rows[0])
    return [
        dict(zip(names, [json_value(value) for value in row.values()]))
        for row in rows
    ]


def compact_json(payload):
    return json.dumps(payload, separators=(",", ":"))


class RawJsonSplicer:
    """Serialize a payload that carries already-serialized JSON inside it.

    json.dumps has no way to say "this string is JSON, emit it verbatim", and
    the usual workarounds - a JSONEncoder subclass overriding iterencode - give
    up the C encoder for the whole payload, which costs more than the parse they
    avoid. So the fragment travels through json.dumps as a placeholder string
    and is swapped in afterwards, leaving the fast encoder to do everything
    else.

    Reserve one fragment rather than several where you can: each swap rewrites
    the whole body, so seven datasets spliced separately copy the body seven
    times, while one object holding all seven copies it once.

    A forgotten splice fails loudly rather than quietly: the placeholder is a
    plain string, so a body built without dumps() carries a visible token
    instead of the data, and the tests below assert on exactly that.
    """

    __slots__ = ("_fragments",)

    def __init__(self):
        self._fragments = {}

    def reserve(self, raw_json_text):
        """Register serialized JSON and return the placeholder that stands for it."""
        # uuid4 rather than a counter: the token has to be something no value in
        # the payload could also be, and it has to survive json.dumps unescaped,
        # so it is plain ASCII with no quoting-relevant characters.
        token = f"raw-json-fragment-{uuid4().hex}"
        self._fragments[token] = raw_json_text
        return token

    def dumps(self, payload):
        body = compact_json(payload)
        for token, raw_json_text in self._fragments.items():
            quoted = f'"{token}"'
            if quoted not in body:
                raise ValueError(
                    "A reserved JSON fragment is missing from the payload"
                )
            body = body.replace(quoted, raw_json_text, 1)
        return body.encode("utf-8")

    def __bool__(self):
        return bool(self._fragments)
