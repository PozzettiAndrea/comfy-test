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

comfy-test runs up to 10 test levels in sequence:

| Level | Name | What It Does |
|-------|------|--------------|
| 1 | **SYNTAX** | Check project structure (pyproject.toml/requirements.txt), CP1252 compatibility, forbidden patterns |
| 2 | **COVERAGE** | Every registered node is used by at least one bundled workflow (opt-in: fails on unused nodes) |
| 3 | **INSTALL** | Clone ComfyUI, create environment, install node + dependencies |
| 4 | **REGISTRATION** | Start server, verify nodes appear in `/object_info` |
| 5 | **INSTANTIATION** | Test each node's constructor |
| 6 | **STATIC_CAPTURE** | Screenshot workflows (no execution) |
| 7 | **VALIDATION** | 4-level workflow validation (schema, graph, introspection, partial execution) |
| 8 | **EXECUTION_LIGHT** | Run workflows end-to-end, one screenshot each (no video; for weak runners — use instead of EXECUTION) |
| 9 | **EXECUTION** | Run workflows end-to-end, capture outputs + per-frame video |
| 10 | **CUSTOM** | Your own hook (`[test] custom = "tests/my_check.py"` exposing `run(ctx)`); runs last against the live server |

The default set is levels 1, 3-7, 9 (`coverage` and `execution_light` are opt-in; `levels = "all"` runs
everything). Each level depends on previous levels. You can run up to a specific level with `--level`:

```bash
comfy-test run --level registration  # Runs: SYNTAX -> INSTALL -> REGISTRATION
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
# Levels default to: syntax, install, registration, instantiation,
# static_capture, validation, execution

[test.workflows]
cpu = "all"

[test.platforms]
# Explicit opt-in allowlist — only listed platforms run.
platforms = ["linux-cpu", "macos-cpu", "windows-cpu", "windows-portable-cpu"]
```

### Full Config Example

```toml
[test]
# Name is auto-detected from directory name (e.g., "ComfyUI-MyNode")

# ComfyUI version to test against
comfyui_version = "latest"  # or a tag like "v0.2.0" or commit hash

# Python version (default: random from 3.11, 3.12, 3.13)
python_version = "3.11"

# Test levels to run. Default: syntax, install, registration, instantiation,
# static_capture, validation, execution. Options additionally: coverage,
# execution_light, custom. Or run everything:
levels = "all"

# Optional custom hook: a Python file exposing run(ctx) — raise to fail.
# Runs last, with the live server available via ctx.server / ctx.api.
custom = "tests/my_check.py"

# Platforms are an explicit opt-in ALLOWLIST — only listed targets run.
# Valid tokens: linux-cpu, macos-cpu, windows-cpu, windows-portable-cpu,
# macos-desktop, windows-desktop, linux-cuda, windows-cuda,
# windows-portable-cuda, windows-desktop-cuda
# (bare "linux"/"macos"/"windows"/"windows_portable" mean the cpu variant)
[test.platforms]
platforms = ["linux-cpu", "macos-cpu", "windows-cpu", "windows-portable-cpu"]

# Workflow configuration — accelerator is named by backend: cpu / cuda / rocm.
[test.workflows]
# Workflows to run on CPU runners (GitHub-hosted)
cpu = "all"  # or a list: ["test_basic.json"], or all-except: ["!heavy.json"]

# Workflows to run on CUDA runners (self-hosted); rocm reserved for later
cuda = ["test_cuda.json"]

# Timeout for workflow execution in seconds (default: 3600)
timeout = 120

# Platform-specific settings (enablement comes from the allowlist above)
[test.linux]
skip_workflow = false  # Skip workflow execution, only verify registration

[test.windows_portable]
comfyui_portable_version = "latest"  # Portable-specific version
```

### Workflow Discovery

Workflows are auto-discovered from the `workflows/` folder:
- All `.json` files in `workflows/` are found automatically
- Use `cpu = "all"` to run all discovered workflows on CPU
- Use `cuda = "all"` to run all discovered workflows on CUDA runners
- Or specify individual files: `cpu = ["basic.json", "advanced.json"]`
- Or exclude: `cpu = ["!heavy.json"]` runs everything except `heavy.json`

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
