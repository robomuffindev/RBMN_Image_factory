# Workflow JSON Reference

## How ComfyUI Workflows Work

ComfyUI workflows are JSON dicts where each key is a node ID (string) and each value is a node definition:

```json
{
  "1": {
    "class_type": "CLIPTextEncode",
    "inputs": {
      "text": "a photo of a cat",
      "clip": ["5", 0]        // connection: node "5", output index 0
    },
    "_meta": {
      "title": "CLIP Text Encode (Positive Prompt)"
    }
  },
  "5": {
    "class_type": "CLIPLoader",
    "inputs": { "clip_name": "model.safetensors" }
  }
}
```

Connections are `[node_id_str, output_index_int]` arrays.

---

## Node Title Conventions (Our Workflows)

We identify nodes by `_meta.title`, not by ID. This makes workflows portable across ComfyUI versions.

### Klein Image Workflow Nodes

| Title | Purpose | Key Inputs |
|-------|---------|------------|
| `CLIP Text Encode (Positive Prompt)` | Main prompt | `text` |
| `Width` | Image width | `int` value |
| `Height` | Image height | `int` value |
| `RandomNoise` | Seed | `noise_seed` |
| `Load Image` | Reference image 1 | `image` (filename) |
| `Reference 2 Image` | Reference image 2 | `image` (filename) |
| `Reference 3` | Reference image 3 | `image` (filename) |
| `Reference 4` | Reference image 4 | `image` (filename) |

### LTX Video Workflow Nodes (I2V / FF-LF)

| Title | Purpose | Key Inputs |
|-------|---------|------------|
| `CLIP Text Encode (Prompt)` | Video prompt | `text` |
| `WIDTH` | Video width | `int` value |
| `HEIGHT` | Video height | `int` value |
| `Audio - Video Duration` | Duration seconds | `float` value |
| `Framerate` | FPS | `int` value |
| `RandomNoise` | Seed | `noise_seed` |
| `Load Audio` | Audio file path | `audio` |
| `LOAD IMAGE` | First frame (I2V mode) | `image` |
| `LOAD FIRST IMAGE FRAME` | First frame (FF/LF mode) | `image` |
| `LOAD LAST IMAGE FRAME` | Last frame (FF/LF mode) | `image` |
| `Unet Loader (GGUF)` | Optional model override | `unet_name` |

### V2V Extend Workflow Nodes

| Title | Purpose | Key Inputs |
|-------|---------|------------|
| `LOAD PREVIOUS VIDEO` | Previous scene's video | `video`, `force_rate`, `skip_first_frames`, `frame_load_cap` |
| `LTXV Extend Sampler` | Sampling with overlap | `frame_overlap` (default 16) |
| `RandomNoise` | Pass 1 seed | `noise_seed` |
| `RandomNoise Pass 2` | Pass 2 seed (seed+1) | `noise_seed` |

### Transition Workflow Nodes

| Title | Purpose | Key Inputs |
|-------|---------|------------|
| `LOAD FIRST FRAME` | End of scene A | `image` |
| `LOAD LAST FRAME` | Start of scene B | `image` |
| `Power Lora Loader (rgthree)` | Transition LoRA | `strength` on the transition lora slot |

---

## Workflow File Locations

Our built-in workflow JSONs live in:
```
backend/services/comfyui/workflows/
├── klein_t2i.json          # Text-to-image (no refs)
├── klein_1ref.json         # 1 reference image
├── klein_2ref.json         # 2 reference images
├── klein_3ref.json         # 3 reference images
├── klein_4ref.json         # 4 reference images
├── ltx_i2v.json           # Image-to-video (single frame)
├── ltx_fflf.json          # First frame / Last frame
├── ltx_v2v_extend.json    # V2V extending (multi-frame overlap)
├── ltx_v2v_pass1.json     # V2V Pass 1 (with audio conditioning)
├── ltx_v2v_pass2.json     # V2V Pass 2 (upscale + refine)
├── ltx_transition.json    # AI transition clip
└── whisper.json           # Whisper transcription via ComfyUI
```

---

## Automatic Fixup Functions

These are applied to ALL video workflows after node mutations:

### 1. `_update_resize_longer_edge(workflow, longer_edge)`

Finds ALL `ResizeImagesByLongerEdge` nodes, sets `longer_edge = max(width, height)`.

**Why**: Workflow JSONs have hardcoded resolution values. If your project uses a different resolution, the resize nodes would distort the aspect ratio.

### 2. `_fix_image_resize_stretch(workflow)`

Changes ALL `ImageResizeKJv2` nodes from `keep_proportion="stretch"` to `"resize"`.

**Why**: Stretch mode causes height distortion at scene transitions due to minor aspect ratio differences from latent space rounding.

### 3. `_update_ltxv_preprocess_compression(workflow, img_compression=35)`

Sets ALL `LTXVPreprocess` nodes to `img_compression=35`.

**Why**: Default of 18 was too aggressive, causing visible content shifts when chaining scenes with last-frame-as-first-frame. 35 is the balanced value.

---

## Graph Manipulation

### Flatten Group Nodes

ComfyUI group nodes use composite IDs like `"1217:1089"`. Some execution engines skip cross-boundary dependencies.

```python
flatten_group_nodes(workflow)
# Converts "1217:1089" → "1217_1089" in keys AND all connection references
# Must be called BEFORE stamp_vhs_unique_prefix()
```

### Strip Non-Essential Nodes

```python
strip_non_essential_nodes(workflow)
# Removes: Image Comparer (rgthree) — debug display
# KEEPS: easy cleanGpuUsed, easy clearCacheAll — VRAM cleanup (prevents OOM!)
# Rewires connections to bypass removed nodes
```

### Remove Missing Nodes

```python
available_types = set(client.get_object_info().keys())
remove_missing_nodes(workflow, available_types)
# Checks every node's class_type against available types
# Removes missing nodes with connection rewiring
# Prevents entire dependency chain failures from missing optional nodes
```

### VHS Unique Prefix

```python
stamp_vhs_unique_prefix(workflow, unique_tag)
# Sets filename_prefix on VHS_VideoCombine to "{original}_j{tag}"
# VHS names files as {prefix}_{counter:05d}_.mp4
# With unique prefix, counter is always 00001
# Enables reliable fallback download via try_download_output()
```

---

## Dynamic Workflow System (Custom Workflows)

Users can upload their own workflow JSONs. The system introspects them and creates field mappings.

### Introspection (`POST /api/workflows/introspect`)

1. Parses the uploaded JSON
2. Scans all nodes for recognizable input patterns
3. Suggests `field_mappings`: `[{node_title, input_name, field_type, description, default_value}]`
4. Field types: prompt, negative_prompt, width, height, seed, image, video, audio, duration, framerate, steps, cfg

### Runtime Application (`prepare_workflow_from_config()`)

```python
def prepare_workflow_from_config(workflow_json, field_mappings, values):
    workflow = deepcopy(workflow_json)
    for mapping in field_mappings:
        value = values.get(mapping.field_type)
        if value is not None:
            set_node_input(workflow, mapping.node_title, mapping.input_name, value)
    return workflow
```

### Per-Server Assignment

Custom workflows can be assigned to specific ComfyUI servers via `server_url` field on WorkflowConfig. The dispatcher routes jobs to the correct server.

---

## Image Upload Before Workflow Submission

Reference images must be uploaded to the ComfyUI server before submitting a workflow that references them:

```python
# Upload to ComfyUI
result = client.upload_image(local_path, filename)
stored_name = result.get("name", filename)  # ComfyUI may rename

# Use stored_name in workflow node inputs
set_node_input(workflow, "Load Image", "image", stored_name)
```

ComfyUI stores uploaded images in its `input/` directory. The node's `image` input takes just the filename, not a full path.

---

## Key Lessons for New Projects

1. **Always use `_meta.title` for node identification** — node IDs change between exports
2. **Always apply fixup functions** — resolution mismatches cause subtle bugs
3. **Always flatten group nodes** — cross-boundary dependencies can silently fail
4. **Always stamp VHS unique prefix** — `/history` is unreliable for VHS output
5. **Upload images BEFORE submitting workflow** — ComfyUI doesn't auto-fetch
6. **Keep GPU cleanup nodes** — removing them causes OOM on 16GB GPUs
7. **Anti-text suffix for Klein** — it WILL render text without it
8. **Single paragraph for LTX** — line breaks create separate video segments
9. **Transition LoRA trigger word** — `"zhuanchang"` must be at END of prompt
