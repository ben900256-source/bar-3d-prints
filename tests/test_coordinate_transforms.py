from barprint.coordinate_transforms import GLB_TO_PRINT_AXIS_CORRECTION, glb_imported_to_print_point, transform_point


def test_glb_imported_point_maps_back_to_print_coordinates() -> None:
    print_point = (2.5, 3.25, 7.0)
    glb_imported_point = (print_point[0], -print_point[2], print_point[1])

    assert glb_imported_to_print_point(glb_imported_point) == print_point
    assert transform_point(glb_imported_point, GLB_TO_PRINT_AXIS_CORRECTION) == print_point
