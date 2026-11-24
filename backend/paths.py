"""Repository paths shared by the server, exporters and command-line tools.

Moving source files must not relocate user state, outputs or shared dependencies.
All paths are based on this file, independent of the process working directory.
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = PROJECT_ROOT.parent
PAGES_DIR = PROJECT_ROOT / 'frontend' / 'pages'
TEMPLATES_DIR = PROJECT_ROOT / 'frontend' / 'templates'
JS_DIR = PROJECT_ROOT / 'frontend' / 'assets' / 'js'
CSS_DIR = PROJECT_ROOT / 'frontend' / 'assets' / 'css'
DEFAULT_STATE_DIR = PROJECT_ROOT / '.state'
DEFAULT_OUTPUT_ROOT = WORKSPACE_ROOT / 'outputs'
# Legacy path constant; never prepend cross-platform binary packages to sys.path.
SHARED_DEPENDENCY_DIR = WORKSPACE_ROOT / 'segmentation_workbench' / '.deps'
