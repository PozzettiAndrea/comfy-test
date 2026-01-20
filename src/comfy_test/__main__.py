"""Allow running as `python -m comfy_test`."""
from .cli import main
import sys

if __name__ == "__main__":
    sys.exit(main())
