from __future__ import annotations

import pytest
from pydantic import ValidationError

from astrbot_plugin_embodiment_bridge.core.models import (
    SpatialContextRequest,
    SpatialContextSnapshot,
)


def valid_payload() -> dict[str, object]:
    return {
        "session_id": "session-1",
        "schema_version": 1,
        "revision": 0,
        "floor_count": 1,
        "seat_count": 2,
        "bed_count": 1,
        "table_count": 1,
        "wall_count": 4,
        "door_count": 1,
        "window_count": 2,
        "scene_capture_available": True,
        "occlusion_available": False,
    }


def test_spatial_context_request_accepts_only_the_semantic_schema() -> None:
    request = SpatialContextRequest.model_validate(valid_payload())

    assert request.schema_version == 1
    assert request.revision == 0
    assert request.floor_count == 1

    sensitive_or_unbounded_fields = (
        "type",
        "image",
        "image_url",
        "mesh",
        "coordinates",
        "dimensions",
        "room_id",
        "anchor_id",
        "description",
        "protocol_version",
    )
    for field_name in sensitive_or_unbounded_fields:
        with pytest.raises(ValidationError):
            SpatialContextRequest.model_validate(
                {**valid_payload(), field_name: "must-not-be-accepted"}
            )


def test_spatial_context_request_requires_every_declared_field() -> None:
    for field_name in valid_payload():
        incomplete = valid_payload()
        incomplete.pop(field_name)
        with pytest.raises(ValidationError):
            SpatialContextRequest.model_validate(incomplete)


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("schema_version", 2),
        ("schema_version", True),
        ("revision", -1),
        ("floor_count", -1),
        ("seat_count", 65),
        ("bed_count", True),
        ("scene_capture_available", 1),
        ("occlusion_available", "false"),
    ],
)
def test_spatial_context_request_rejects_invalid_or_coerced_values(
    field_name: str,
    value: object,
) -> None:
    with pytest.raises(ValidationError):
        SpatialContextRequest.model_validate({**valid_payload(), field_name: value})


def test_spatial_context_snapshot_is_detached_from_session_and_frozen() -> None:
    request = SpatialContextRequest.model_validate(valid_payload())
    snapshot = SpatialContextSnapshot.from_request(request)

    assert "session_id" not in snapshot.model_dump()
    with pytest.raises(ValidationError):
        snapshot.revision = 1
    with pytest.raises(ValidationError):
        SpatialContextSnapshot.model_validate(
            {**snapshot.model_dump(), "schema_version": True}
        )
