"""Metric, per-capture multimodal surface point clouds."""

from pathlib import Path

# Keep the established pointcloud_workbench.* import paths while storing Python
# implementation files under backend/. The project root still contains tests
# and the original CLI entry points, so it remains on the package search path.
__path__.insert(0, str(Path(__file__).resolve().parent / 'backend'))
