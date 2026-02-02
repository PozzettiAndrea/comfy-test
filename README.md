# comfy-test

Testing infrastructure for ComfyUI custom nodes.

Test your nodes install and work correctly across **Linux**, **macOS**, **Windows**, and **Windows Portable**. No pytest code needed.

## Quick Start

Add these files to your custom node repository:

### 1. `comfy-test.toml`

```toml
[test]
# Name is auto-detected from directory

[test.workflows]
cpu = "all"  # Run all workflows in workflows/ folder
```

### 2. `.github/workflows/test-install.yml`

```yaml
name: Test Installation
on: [push, pull_request]

jobs:
  test:
    uses: PozzettiAndrea/comfy-test/.github/workflows/test-matrix.yml@main
```

### 3. `workflows/test.json`

A minimal ComfyUI workflow that uses your nodes. Export from ComfyUI.

**Done!** Push to GitHub and your tests will run automatically on all platforms.

## Test Levels

comfy-test runs 7 test levels in sequence:

| Level | Name | What It Does |
|-------|------|--------------|
| 1 | **SYNTAX** | Check project structure (pyproject.toml/requirements.txt), CP1252 compatibility |
| 2 | **INSTALL** | Clone ComfyUI, create environment, install node + dependencies |
| 3 | **REGISTRATION** | Start server, verify nodes appear in `/object_info` |
| 4 | **INSTANTIATION** | Test each node's constructor |
| 5 | **STATIC_CAPTURE** | Screenshot workflows (no execution) |
| 6 | **VALIDATION** | 4-level workflow validation (schema, graph, introspection, partial execution) |
| 7 | **EXECUTION** | Run workflows end-to-end, capture outputs |

Each level depends on previous levels. You can run up to a specific level with `--level`:

```bash
comfy-test run --level registration  # Runs: SYNTAX → INSTALL → REGISTRATION
```

## Workflow Validation (4 Levels)

The VALIDATION level runs comprehensive checks before execution:

| Level | Name | What It Checks |
|-------|------|----------------|
| 1 | **Schema** | Widget values match allowed enums, types, and ranges |
| 2 | **Graph** | Connections are valid, all referenced nodes exist |
| 3 | **Introspection** | Node definitions are well-formed (INPUT_TYPES, RETURN_TYPES, FUNCTION) |
| 4 | **Partial Execution** | Runs non-CUDA nodes to verify they work |

### Detecting CUDA Nodes

To mark nodes as requiring CUDA (excluded from partial execution), use `comfy-env.toml`:

```toml
[cuda]
packages = ["nvdiffrast", "flash-attn"]
```

## Configuration Reference

### Minimal Config

```toml
[test]
# Everything has sensible defaults - this is all you need

[test.workflows]
cpu = "all"
```

### Full Config Example

```toml
[test]
# Name is auto-detected from directory name (e.g., "ComfyUI-MyNode")

# ComfyUI version to test against
comfyui_version = "latest"  # or a tag like "v0.2.0" or commit hash

# Python version (default: random from 3.11, 3.12, 3.13)
python_version = "3.11"

# Test levels to run (default: all)
# Options: syntax, install, registration, instantiation, static_capture, validation, execution
levels = ["syntax", "install", "registration", "instantiation", "static_capture", "validation", "execution"]
# Or use: levels = "all"

# Enable/disable platforms (all enabled by default)
[test.platforms]
linux = true
macos = true
windows = true
windows_portable = true

# Workflow configuration
[test.workflows]
# Workflows to run on CPU runners (GitHub-hosted)
cpu = "all"  # or list specific files: ["test_basic.json", "test_advanced.json"]

# Workflows to run on GPU runners (self-hosted)
gpu = ["test_gpu.json"]

# Timeout for workflow execution in seconds (default: 3600)
timeout = 120

# Platform-specific settings
[test.linux]
enabled = true
skip_workflow = false  # Skip workflow execution, only verify registration

[test.macos]
enabled = true
skip_workflow = false

[test.windows]
enabled = true
skip_workflow = false

[test.windows_portable]
enabled = true
skip_workflow = false
comfyui_portable_version = "latest"  # Portable-specific version
```

### Workflow Discovery

Workflows are auto-discovered from the `workflows/` folder:
- All `.json` files in `workflows/` are found automatically
- Use `cpu = "all"` to run all discovered workflows on CPU
- Use `gpu = "all"` to run all discovered workflows on GPU
- Or specify individual files: `cpu = ["basic.json", "advanced.json"]`

## CLI

```bash
# Install
pip install comfy-test

# Initialize config and GitHub workflow
comfy-test init

# Run tests locally
comfy-test run --platform linux

# Run specific level only
comfy-test run --level registration

# Dry run (show what would happen)
comfy-test run --dry-run

# Publish results to GitHub Pages
comfy-test publish ./results --repo owner/repo
```

## CUDA Packages on CPU-only CI

comfy-test runs on CPU-only GitHub Actions runners. For nodes that use CUDA packages:

1. **Installation works** - comfy-test sets `COMFY_ENV_CUDA_VERSION=12.8` so comfy-env can resolve wheel URLs
2. **Import may fail** - CUDA packages typically fail to import without a GPU

For full CUDA testing, use a self-hosted runner with a GPU.

## License

MIT
