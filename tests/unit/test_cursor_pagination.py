import base64
import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.common.schemas.common import (
    CursorPaginationRequest,
    CursorPayload,
    PaginationBaseRequest,
)
from app.common.utils.cursor_pagination import decode_cursor, encode_cursor


def _token(data: object) -> str:
    raw = json.dumps(data, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def test_cursor_round_trip_is_deterministic_and_url_safe() -> None:
    payload = CursorPayload(
        created_at=datetime(2026, 8, 26, 8, 30, tzinfo=timezone.utc),
        id=uuid4(),
    )

    encoded = encode_cursor(payload)

    assert encoded == encode_cursor(payload)
    assert "=" not in encoded
    assert decode_cursor(encoded) == payload


@pytest.mark.parametrize(
    "cursor",
    [
        "not valid base64!",
        "é",
        _token("not-an-object"),
        _token({"v": 2, "created_at": "2026-08-26T08:30:00Z", "id": str(uuid4())}),
        _token({"v": 1, "created_at": "2026-08-26T08:30:00Z", "id": "bad"}),
        _token(
            {
                "v": 1,
                "created_at": "2026-08-26T08:30:00Z",
                "id": str(uuid4()),
                "unexpected": True,
            }
        ),
        _token(
            {
                "v": 1,
                "created_at": "2026-08-26T08:30:00",
                "id": str(uuid4()),
            }
        ),
    ],
)
def test_decode_cursor_rejects_invalid_payloads(cursor: str) -> None:
    with pytest.raises(ValueError, match="Invalid pagination cursor"):
        decode_cursor(cursor)


def test_cursor_request_defaults_and_validation() -> None:
    query = CursorPaginationRequest()

    assert query.after is None
    assert query.before is None
    assert query.limit == 10

    with pytest.raises(ValidationError):
        CursorPaginationRequest(after="a", before="b")
    with pytest.raises(ValidationError):
        CursorPaginationRequest(limit=101)


def test_offset_pagination_request_remains_unchanged() -> None:
    query = PaginationBaseRequest()

    assert query.page == 1
    assert query.page_size == 10
