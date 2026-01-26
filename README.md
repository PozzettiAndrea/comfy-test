# comfy-test

Installation testing infrastructure for ComfyUI custom nodes.

Test your nodes install and work correctly across **Linux**, **Windows**, and **Windows Portable** - with just config files, no pytest code needed.

## Quick Start

Add these files to your custom node repository:

### 1. `comfy-test.toml`

```toml
[test]
name = "ComfyUI-MyNode"

[test.verification]
expected_nodes = ["MyNode1", "MyNode2"]

[test.workflow]
file = "tests/workflows/smoke_test.json"
timeout = 120
```

### 2. `.github/workflows/test-install.yml`

```yaml
name: Test Installation
on: [push, pull_request]

jobs:
  test:
    uses: PozzettiAndrea/comfy-test/.github/workflows/test-matrix.yml@main
```

### 3. `tests/workflows/smoke_test.json`

A minimal ComfyUI workflow that uses your nodes. Export from ComfyUI.

**Done!** Push to GitHub and your tests will run automatically on all platforms.

## What It Tests

1. **Setup** - Clones ComfyUI, creates environment, installs dependencies
2. **Install** - Copies your node, runs `install.py`, installs `requirements.txt`
3. **Verify** - Starts ComfyUI, checks your nodes appear in `/object_info`
4. **Validate** - Runs 4-level workflow validation (see below)
5. **Execute** - Runs your test workflow, verifies it completes without errors

## Workflow Validation (4 Levels)

When a workflow file is configured, comfy-test runs comprehensive validation before execution:

| Level | Name | What It Checks |
|-------|------|----------------|
| 1 | **Schema** | Widget values match allowed enums, types, and ranges |
| 2 | **Graph** | Connections are valid, all referenced nodes exist |
| 3 | **Introspection** | Node definitions are well-formed (INPUT_TYPES, RETURN_TYPES, FUNCTION) |
| 4 | **Partial Execution** | Runs non-CUDA nodes to verify they work |

### Level 1: Schema Validation

Validates widget values in your workflow against node schemas from `/object_info`:

- **Enum values** - Checks dropdown selections are in the allowed list
- **INT/FLOAT ranges** - Validates numbers are within min/max bounds
- **Type checking** - Ensures STRING, BOOLEAN, INT, FLOAT values have correct types

```
[schema] Node 5 (LoadTrellis2Models): 'attn_backend': 'auto' not in allowed values ['flash_attn', 'xformers', 'sdpa', 'sageattn']
```

### Level 2: Graph Validation

Validates the workflow graph structure:

- **Node existence** - All linked nodes actually exist
- **Connection types** - Output types match input types (IMAGE -> IMAGE)
- **Slot validity** - Input/output slot indices are valid

```
[graph] Node 12 (SaveImage): Type mismatch: KSampler outputs LATENT, but SaveImage expects IMAGE
```

### Level 3: Node Introspection

Validates node definitions from the ComfyUI API:

- **INPUT_TYPES** - Returns valid dict with required/optional structure
- **RETURN_TYPES** - Is a list matching RETURN_NAMES length
- **FUNCTION** - Method name is defined

```
[introspection] Node 3 (BrokenNode): Node has no FUNCTION defined
```

### Level 4: Partial Execution

Executes the "prefix" of your workflow - nodes that don't require CUDA:

- Identifies nodes that don't depend on CUDA packages
- Converts workflow to ComfyUI prompt format
- Submits partial workflow to the API
- Reports which nodes executed successfully

This catches runtime errors in non-GPU code paths (file loading, preprocessing, etc.) even on CPU-only CI.

```
[Step 3c/4] Partial execution (3 non-CUDA nodes)...
  Executed 3 nodes successfully
```

### Detecting CUDA Nodes

To mark nodes as requiring CUDA (so they're excluded from partial execution), list them in your `comfy-test.toml`:

```toml
[test.validation]
cuda_node_types = ["KSampler", "VAEDecode", "MyGPUNode"]
```

Or use `comfy-env.toml` to specify CUDA packages - any node importing those packages will be detected automatically.

## Configuration Reference

```toml
[test]
name = "ComfyUI-MyNode"           # Test suite name
comfyui_version = "latest"        # ComfyUI version (tag, commit, or "latest")
python_version = "3.10"           # Python version
timeout = 300                     # Setup timeout in seconds

[test.platforms]
linux = true                      # Test on Linux
windows = true                    # Test on Windows
windows_portable = true           # Test on Windows Portable

[test.verification]
expected_nodes = [                # Nodes that must exist after install
    "MyNode1",
    "MyNode2",
]

[test.workflow]
file = "tests/workflows/smoke.json"  # Workflow to run
timeout = 120                        # Workflow timeout

[test.windows_portable]
comfyui_portable_version = "latest"  # Portable version to download
```

## CUDA Packages on CPU-only CI

comfy-test runs on CPU-only GitHub Actions runners. For nodes that use CUDA packages (nvdiffrast, flash-attn, etc.):

1. **Installation works** - comfy-test sets `COMFY_ENV_CUDA_VERSION=12.8` so comfy-env can resolve wheel URLs even without a GPU
2. **Import may fail** - CUDA packages typically fail to import without a GPU

**Best practice for CUDA nodes:**
- Use lazy imports in production (better UX, graceful errors)
- Consider strict imports mode for testing to catch missing deps

```python
# In your node's __init__.py
import os

if os.environ.get('COMFY_TEST_STRICT_IMPORTS'):
    # Test mode: import everything now to catch missing deps
    import nvdiffrast  # Will fail on CPU, but that's expected
else:
    # Production: lazy import when needed
    nvdiffrast = None
```

For full CUDA testing, use a self-hosted runner with a GPU.

## CLI

```bash
# Install
pip install comfy-test

# Show config
comfy-test info

# Run tests locally
comfy-test run --platform linux

# Dry run (show what would happen)
comfy-test run --dry-run

# Generate GitHub workflow
comfy-test init-ci
```

## License

MIT
