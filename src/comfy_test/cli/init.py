"""Init command for comfy-test CLI."""

import shutil
import sys
from importlib.resources import files
from pathlib import Path


def cmd_init(args) -> int:
    """Initialize comfy-test config and GitHub workflow.

    Creates:
    - comfy-test.toml (config file)
    - .github/workflows/ (GitHub Actions workflow)
    """
    cwd = Path.cwd()
    config_path = cwd / "comfy-test.toml"

    # Get templates directory from package
    try:
        templates = files("comfy_test") / "templates"
    except (TypeError, FileNotFoundError):
        # Fallback: templates at project root
        templates = Path(__file__).parent.parent.parent.parent / "templates"

    # Check existing files
    if not args.force:
        if config_path.exists():
            print(f"Config file already exists: {config_path}", file=sys.stderr)
            print("Use --force to overwrite", file=sys.stderr)
            return 1

    # Copy comfy-test.toml
    template_config = templates / "comfy-test.toml"
    shutil.copy(template_config, config_path)
    print(f"Created {config_path}")

    # Copy github/ -> .github/ (includes workflow)
    template_github = templates / "github"
    github_dir = cwd / ".github"
    if template_github.is_dir():
        shutil.copytree(template_github, github_dir, dirs_exist_ok=True)
        print(f"Created {github_dir}/")

    return 0


def add_init_parser(subparsers):
    """Add the init subcommand parser."""
    init_parser = subparsers.add_parser(
        "init",
        help="Create comfy-test.toml config and GitHub workflow",
    )
    init_parser.add_argument(
        "--force", "-f",
        action="store_true",
        help="Overwrite existing files",
    )
    init_parser.set_defaults(func=cmd_init)
