"""Isolated native smoke-test entry point, never used in release packages."""

import os
import tempfile
from pathlib import Path

os.environ["PERCH_DATA_DIR"] = str(Path(tempfile.gettempdir()) / "perch-native-smoke")
from perch import main  # noqa: E402
import sys  # noqa: E402

sys.argv = [sys.argv[0], "--demo", "--port", "8796"]
main()
