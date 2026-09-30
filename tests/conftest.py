"""The application resolves bundled resources relative to its entry-point script."""
import sys
from pathlib import Path
from unittest.mock import patch


with patch.object(sys, "argv", [str(Path(__file__).resolve().parents[1] / "main.py"), *sys.argv[1:]]):
    import constants  # noqa: F401
