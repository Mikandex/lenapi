"""Project-local command launcher."""

import sys
from pathlib import Path

project_dir = Path(__file__).resolve().parent
sys.path[:] = [entry for entry in sys.path if Path(entry or ".").resolve() != project_dir]
sys.path.insert(0, str(project_dir.parent))
from lenapi.cli import main
sys.path.insert(0, str(project_dir))


if __name__ == "__main__":
    main()
