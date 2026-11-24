#!/usr/bin/env python3
"""Compatibility entry point; implementation lives in backend/app.py."""
from pathlib import Path
import sys

if __package__ in {None, ''}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pointcloud_workbench.app import main

if __name__ == '__main__':
    sys.exit(main())
