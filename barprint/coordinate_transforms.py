from __future__ import annotations

from collections.abc import Sequence


GLB_TO_PRINT_AXIS_CORRECTION: tuple[tuple[float, float, float], ...] = (
    (1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0),
    (0.0, -1.0, 0.0),
)


def transform_point(
    point: Sequence[float],
    matrix: Sequence[Sequence[float]],
) -> tuple[float, float, float]:
    x, y, z = point
    return (
        matrix[0][0] * x + matrix[0][1] * y + matrix[0][2] * z,
        matrix[1][0] * x + matrix[1][1] * y + matrix[1][2] * z,
        matrix[2][0] * x + matrix[2][1] * y + matrix[2][2] * z,
    )


def glb_imported_to_print_point(point: Sequence[float]) -> tuple[float, float, float]:
    return transform_point(point, GLB_TO_PRINT_AXIS_CORRECTION)
