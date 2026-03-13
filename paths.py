"""
Kiki Path Utilities
===================

Central module for resolving all file paths relative to the project root.
Import PROJECT_ROOT or use project_path() to build paths anywhere in the codebase.
"""

from pathlib import Path

# The root of the KikiFast project (directory containing this file)
PROJECT_ROOT = Path(__file__).resolve().parent


def project_path(*parts: str) -> Path:
    """
    Build an absolute path relative to the project root.

    Example:
        project_path("hotwords", "hey-kiki_en_windows_v4_0_0.ppn")
        → C:/Users/.../KikiFast/hotwords/hey-kiki_en_windows_v4_0_0.ppn
    """
    return PROJECT_ROOT.joinpath(*parts)
