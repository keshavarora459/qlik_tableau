"""Pytest root configuration for mapping."""

import sys
from pathlib import Path

# Add workspace root and current repo root to sys.path
repo_root = Path(__file__).resolve().parent
workspace_root = repo_root.parent.parent

if str(workspace_root) not in sys.path:
    sys.path.insert(0, str(workspace_root))
if str(repo_root) not in sys.path:
    sys.path.insert(0, str(repo_root))
