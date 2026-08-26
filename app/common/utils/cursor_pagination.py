import base64
import binascii
import json

from pydantic import ValidationError

from app.common.schemas.common import CursorPayload

MAX_CURSOR_LENGTH = 2048


def encode_cursor(payload: CursorPayload) -> str:
    """Encode validated cursor contents as compact URL-safe Base64 JSON."""
    raw_payload = json.dumps(
        payload.model_dump(mode="json"),
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(raw_payload).decode("ascii").rstrip("=")


def decode_cursor(cursor: str) -> CursorPayload:
    """Decode and strictly validate an opaque cursor.

    Raises:
        ValueError: If the token or decoded payload is invalid.
    """
    if not cursor or len(cursor) > MAX_CURSOR_LENGTH:
        raise ValueError("Invalid pagination cursor")

    padding = "=" * (-len(cursor) % 4)
    try:
        raw_payload = base64.b64decode(
            cursor + padding,
            altchars=b"-_",
            validate=True,
        )
        data = json.loads(raw_payload.decode("utf-8"))
        return CursorPayload.model_validate(data)
    except (
        binascii.Error,
        UnicodeDecodeError,
        json.JSONDecodeError,
        TypeError,
        ValidationError,
        ValueError,
    ) as exc:
        raise ValueError("Invalid pagination cursor") from exc
