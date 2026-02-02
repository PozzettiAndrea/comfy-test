"""Paths command for comfy-test CLI."""

import os
import sys
from pathlib import Path


# Environment variable names
ENV_LOGS_DIR = "COMFY_TEST_LOGS_DIR"
ENV_WORKSPACE_DIR = "COMFY_TEST_WORKSPACE_DIR"

# Defaults
DEFAULT_LOGS_DIR = Path.home() / "comfy-test-logs"
DEFAULT_WORKSPACE_DIR = Path.home() / "test_workspaces"


def get_logs_dir() -> Path:
    """Get logs directory from env var or default."""
    return Path(os.environ.get(ENV_LOGS_DIR, DEFAULT_LOGS_DIR))


def get_workspace_dir() -> Path:
    """Get workspace directory from env var or default."""
    return Path(os.environ.get(ENV_WORKSPACE_DIR, DEFAULT_WORKSPACE_DIR))


def are_paths_configured() -> bool:
    """Check if path env vars are set."""
    return ENV_LOGS_DIR in os.environ and ENV_WORKSPACE_DIR in os.environ


def _detect_shell_config() -> Path:
    """Detect user's shell config file."""
    shell = os.environ.get("SHELL", "")
    home = Path.home()

    if "zsh" in shell:
        return home / ".zshrc"
    elif "bash" in shell:
        # Prefer .bashrc, fall back to .bash_profile
        bashrc = home / ".bashrc"
        if bashrc.exists():
            return bashrc
        return home / ".bash_profile"
    else:
        # Default to .profile
        return home / ".profile"


def _prompt_path(var_name: str, description: str, default: Path) -> Path:
    """Prompt user for a path with default option."""
    print(f"\n{var_name} is not set.")
    print(f"{description}")
    print(f"  [1] {default} (default)")
    print("  [2] Custom path")

    choice = input("> ").strip()
    if choice == "2":
        custom = input("Enter path: ").strip()
        return Path(custom).expanduser().resolve()
    return default


def run_setup_wizard() -> dict:
    """Run interactive setup wizard for paths.

    Returns dict with logs_dir and workspace_dir.
    """
    print("[comfy-test] Path configuration needed\n")

    # Get logs dir
    if ENV_LOGS_DIR in os.environ:
        logs_dir = Path(os.environ[ENV_LOGS_DIR])
        print(f"{ENV_LOGS_DIR} already set: {logs_dir}")
    else:
        logs_dir = _prompt_path(
            ENV_LOGS_DIR,
            "Where should test logs be saved?",
            DEFAULT_LOGS_DIR,
        )

    # Get workspace dir
    if ENV_WORKSPACE_DIR in os.environ:
        workspace_dir = Path(os.environ[ENV_WORKSPACE_DIR])
        print(f"{ENV_WORKSPACE_DIR} already set: {workspace_dir}")
    else:
        workspace_dir = _prompt_path(
            ENV_WORKSPACE_DIR,
            "Where should test workspaces be created?",
            DEFAULT_WORKSPACE_DIR,
        )

    # Offer to add to shell config
    shell_config = _detect_shell_config()
    print(f"\nAdd to your shell config ({shell_config}):")
    print(f'  export {ENV_LOGS_DIR}="{logs_dir}"')
    print(f'  export {ENV_WORKSPACE_DIR}="{workspace_dir}"')

    add_to_shell = input("\nAdd these now? [Y/n] ").strip().lower()
    if add_to_shell != "n":
        _add_to_shell_config(shell_config, logs_dir, workspace_dir)
        print(f"Added to {shell_config}")
        print(f"Run: source {shell_config} (or restart terminal)")

        # Also set in current environment
        os.environ[ENV_LOGS_DIR] = str(logs_dir)
        os.environ[ENV_WORKSPACE_DIR] = str(workspace_dir)

    return {"logs_dir": logs_dir, "workspace_dir": workspace_dir}


def _add_to_shell_config(config_path: Path, logs_dir: Path, workspace_dir: Path) -> None:
    """Add exports to shell config file."""
    exports = f"""
# comfy-test paths
export {ENV_LOGS_DIR}="{logs_dir}"
export {ENV_WORKSPACE_DIR}="{workspace_dir}"
"""

    # Check if already present
    if config_path.exists():
        content = config_path.read_text()
        if ENV_LOGS_DIR in content:
            print(f"Warning: {ENV_LOGS_DIR} already in {config_path}, skipping")
            return

    # Append to file
    with open(config_path, "a") as f:
        f.write(exports)


def cmd_paths(args) -> int:
    """Show or configure paths."""
    if args.set:
        run_setup_wizard()
        return 0

    # Show current paths
    logs_dir = get_logs_dir()
    workspace_dir = get_workspace_dir()

    logs_set = ENV_LOGS_DIR in os.environ
    workspace_set = ENV_WORKSPACE_DIR in os.environ

    print("Current paths:")
    print(f"  {ENV_LOGS_DIR}:      {logs_dir}", end="")
    print("" if logs_set else " (default - not set)")

    print(f"  {ENV_WORKSPACE_DIR}: {workspace_dir}", end="")
    print("" if workspace_set else " (default - not set)")

    if not logs_set or not workspace_set:
        print(f"\nRun 'comfy-test paths --set' to configure")

    return 0


def add_paths_parser(subparsers):
    """Add the paths subcommand parser."""
    paths_parser = subparsers.add_parser(
        "paths",
        help="Show or configure test paths",
    )
    paths_parser.add_argument(
        "--set",
        action="store_true",
        help="Run setup wizard to configure paths",
    )
    paths_parser.set_defaults(func=cmd_paths)
