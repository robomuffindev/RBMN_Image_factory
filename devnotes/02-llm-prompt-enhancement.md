# LLM Prompt Enhancement — Architecture Reference

## Overview

The app uses LLMs (OpenAI, Anthropic, Gemini) to enhance user prompts into optimized generation prompts for Klein image generation and LTX video generation. The system also generates creative direction (video flow, concept, characters) from lyrics.

Everything lives in `backend/services/llm/prompt_enhancer.py`.

---

## Provider Abstraction

### Three providers, identical interface:

```python
class PromptEnhancer:
    @staticmethod
    def enhance(prompt, context, provider, api_key, model,
                is_video=False, system_prompt_override=None,
                gen_model_name=None, frame_type="first",
                prompt_guidance=None, two_pass_phase=None) -> str:
```

All three providers called identically:
- Temperature: 0.7
- Max tokens: 300
- System message + user message format
- Result collapsed to single paragraph

### Provider implementations:

```python
# OpenAI
client = openai.OpenAI(api_key=api_key)
response = client.chat.completions.create(
    model=model or "gpt-4-turbo",
    messages=[{"role": "system", "content": system_prompt},
              {"role": "user", "content": user_message}],
    temperature=0.7, max_tokens=300
)

# Anthropic
client = anthropic.Anthropic(api_key=api_key)
response = client.messages.create(
    model=model or "claude-3-sonnet-20240229",
    system=system_prompt,
    messages=[{"role": "user", "content": user_message}],
    max_tokens=300
)

# Gemini
genai.configure(api_key=api_key)
model = genai.GenerativeModel(model_name or "gemini-pro", system_instruction=system_prompt)
response = model.generate_content(user_message, generation_config={"temperature": 0.7, "max_output_tokens": 300})
```

### Provider resolution (`resolve_llm_config()` in settings.py):

```python
# Priority:
# 1. default_llm_provider if set AND has an API key
# 2. First available: openai → anthropic → gemini
# Default models: gpt-4o, claude-sonnet-4-20250514, gemini-2.0-flash
```

---

## System Prompts (5 Types)

### Prompt Type Resolution Logic

```python
if two_pass_phase == "base":     → "two_pass_base"
elif two_pass_phase == "composite": → "two_pass_composite"
elif is_video:                   → "video"
elif frame_type == "last":       → "image_last_frame"
else:                            → "image"
```

### 1. IMAGE_SYSTEM_PROMPT (Klein 9B First Frame)

Key rules:
- Single paragraph, no line breaks, no bullets
- Front-load: subject → action → environment → lighting → mood → camera
- Reference images use COMPOSITIONAL language ("the subject from the first image"), NOT "Image 1" tags
- **Lyrics are PRIMARY creative source** — concrete objects/actions MUST appear visually; metaphors translated to striking visuals
- 40-150 words
- Never include text/watermarks in description
- If no prompt provided, CREATE from context (lyrics first, then concept/style/characters/flow)

### 2. LAST_FRAME_IMAGE_SYSTEM_PROMPT (End State)

Key differences from first frame:
- Must be visually continuous with first frame (same scene, subjects, lighting)
- Only position/pose/action and camera angle change
- Describes where camera and subject END UP after 3-10 seconds
- First reference slot IS the first frame image

### 3. VIDEO_SYSTEM_PROMPT (LTX Video)

Key differences:
- Present tense, active voice
- Describes motion and action
- Film terminology for camera behavior
- Structure: scene anchor → subject/action → camera → style → motion cues
- 50-200 words depending on duration
- **Single paragraph is CRITICAL** — paragraph breaks create separate video segments in LTX

### 4. TWO_PASS_BASE_SYSTEM_PROMPT (Pass 1 — Scene Only)

Key rules:
- Zero reference images — never mention "the subject from the first image"
- Focus: environment, setting, atmosphere, lighting, camera, composition
- Generic placeholders ("a figure", "a silhouette") for where subjects will go
- **Scene diversity mandatory** — each scene DIFFERENT location, time of day, weather, camera angle, color palette
- Lyrics and storyboard drive the specific setting

### 5. TWO_PASS_COMPOSITE_SYSTEM_PROMPT (Pass 2 — Characters In)

Key rules:
- Reference Image 1 = base scene (from Pass 1)
- Reference Images 2+ = character photos
- Anchors to scene: "In the scene from the first image, ..."
- Natural pose/position for characters
- Maintain lighting/atmosphere from base scene

---

## System Prompt Registry

### Three-tier fallback:

```python
def get_system_prompt(model_name, prompt_type, user_override, override_enabled):
    # 1. User override (if enabled and non-empty) — from Settings
    # 2. Model-specific built-in from BUILTIN_SYSTEM_PROMPTS
    # 3. Generic default (_GENERIC_DEFAULTS)
```

### Built-in registry:

```python
BUILTIN_SYSTEM_PROMPTS = {
    "flux2_klein_dev_9b": {image, image_last_frame, two_pass_base, two_pass_composite},
    "flux1_dev": {image, image_last_frame},
    "z_image": {image, image_last_frame},
    "qwen_edit": {image, image_last_frame},
    "ltx_2.3": {video},
    "wan_2.2": {video},
}
```

### Per-model prompt guidance

Extra text appended to system prompt with header:
```
ADDITIONAL PROMPT RULES AND GUIDANCE FROM USER:
{prompt_guidance}
```

---

## Context Assembly

### User message format (identical for all providers):

```python
if prompt:
    user_msg = f"Original prompt: {prompt}"
else:
    user_msg = "No prompt provided. Generate a new prompt entirely from the context below."

if context:
    user_msg += f"\n\nContext: {context}"
```

### Frontend manual enhance context

Frontend builds context from scene data and sends as `req.context` string. Includes:
- Model info
- Lyrics (primary)
- Flow idea
- Concept/style
- Characters
- Camera action
- Image direction

### Auto-gen enhance context (`_build_auto_enhance_context()`)

Assembled programmatically:

```python
context_parts = []
context_parts.append(f"Image generation model: {model_name}")
context_parts.append(f"Scene timing: {start_time:.1f}s - {end_time:.1f}s, frame_type: {frame_type}")

if concept_text:
    context_parts.append(f"Video concept: {concept_text}")
if style_text:
    context_parts.append(f"Visual style: {style_text}")
if image_direction:
    context_parts.append(f"Image direction: {image_direction}")

# Characters (up to 2 with images) — compositional reference instructions
for i, char in enumerate(characters_with_images[:2]):
    ref_slot = i + 1  # refs start at 1
    context_parts.append(f"Character '{char['name']}': {char['description']}. "
                         f"Reference photo provided as image {ref_slot}.")

# Scene lyrics — PRIMARY creative source
if scene_lyrics:
    context_parts.append(f"SCENE LYRICS (PRIMARY CREATIVE SOURCE): {scene_lyrics}")

# Story flow idea
if use_story_flow and flow_idea:
    context_parts.append(f"SCENE STORYBOARD: {flow_idea}")

# Camera action
if camera_action:
    context_parts.append(f"Requested camera action: {camera_action}")

# Previous scene continuity
if prev_scene_ref and not ignore_prev_ref:
    context_parts.append("Previous scene reference image provided. "
                         "Match the art style but create different content.")
```

### Two-pass base context

```python
# Assembles:
# 1. Two-pass instructions
# 2. Scene diversity mandate
# 3. Scene timing and position
# 4. Concept + style
# 5. Image direction
# 6. Scene lyrics (for environment/mood)
# 7. Primary prompt: flow_idea if exists, else user prompt
```

**Key pattern**: When `flow_idea` exists, it becomes the primary prompt and the user's enhanced prompt is demoted to style/mood reference context.

---

## Post-Processing

### `_collapse_to_single_paragraph(text)`

Replaces all newlines and multi-spaces with single spaces. **Critical for LTX** — paragraph breaks create separate video segments with transitions.

### `_clean_enhanced_prompt(text)`

Strips LLM-added prefixes ("Enhanced prompt:", "Here is the enhanced prompt:") and surrounding double-quotes.

---

## Video Flow Generation

`POST /api/projects/{id}/concept/flow/generate`

1. Fetches all scenes ordered by `order_index`
2. Gets per-scene lyrics (word-level timestamp filtering)
3. Gets full lyrics text for overall narrative context
4. Builds scene list with timing + per-scene lyrics
5. System prompt: lyrics as PRIMARY driver; visual diversity; characters by name; return JSON array
6. Dynamic token budget: `max(2000, num_scenes * 150 + 500)`
7. Parses JSON array response with truncation repair fallback
8. Saves each idea to `scene.parameters["flow_idea"]`

Also auto-triggered during auto-gen if no scenes have flow ideas yet.

---

## "Base on Lyrics" Flow

`POST /api/projects/{id}/concept/base-on-lyrics`

1. Fetches lyrics (priority: user `initial_text` > Whisper `full_text`)
2. Determines which fields need generating (empty: song_title, concept_text, style_text)
3. Includes existing characters as context
4. System prompt: creative director for AI music videos; lyrics-driven concept; chronological narrative arc; concrete imagery
5. Asks LLM to return JSON with only missing fields
6. Parses JSON (strips markdown fences), merges with existing values

---

## Two-Pass Image Generation Flow

### Pass 1: Scene Composition (no characters)

1. Job created with `two_pass=True, two_pass_phase="base"`, no reference images
2. Dispatcher: `_build_two_pass_base_prompt()` → re-enhances via LLM with base system prompt
3. Generates scene-only image (environment, lighting, composition)
4. **Primary prompt selection**: `flow_idea` if it exists becomes the prompt, user's enhanced prompt becomes context

### Pass 2: Character Compositing (auto-chained)

1. On Pass 1 completion: `_create_two_pass_composite_job()` auto-fires
2. `_build_two_pass_composite_prompt()` builds context
3. Creates Pass 2 job: base scene as ref slot 1 + character refs as slots 2+
4. Generates final composited image → auto-sets as scene preview

### Storage

Both prompts stored in `scene.parameters`:
```python
scene.parameters["two_pass_scene_prompt"]       # Pass 1 prompt
scene.parameters["two_pass_composite_prompt"]   # Pass 2 prompt
scene.parameters["two_pass_original_prompt"]    # Original user prompt
scene.parameters["two_pass_enabled"] = True
```

---

## Klein-Specific Prompt Rules

### "Image N" Reference Syntax

Klein uses `"Image 1"`, `"Image 2"`, etc. in prompts for IP-Adapter reference alignment. But the LLM system prompt uses COMPOSITIONAL language ("the subject from the first image") instead, because:
1. The LLM doesn't know the exact Klein syntax
2. Compositional language produces better results than rigid syntax
3. The anti-text suffix prevents Klein from literally rendering "Image 1" as text

### Anti-Text Suffix

Always appended to every Klein prompt:
```
", no text, no subtitles, no captions, no words, no letters, no watermarks"
```

This is applied in `prepare_klein_workflow()`, not in the LLM enhance step.

---

## Camera Action Presets

24 film-industry motions available as dropdown:
pan_left, pan_right, tilt_up, tilt_down, dolly_in, dolly_out, dolly_zoom, crane_up, crane_down, orbit_left, orbit_right, orbit_360, steadicam, tracking_left, tracking_right, zoom_in, zoom_out, rack_focus, whip_pan, dutch_angle, aerial_descend, aerial_ascend, static, handheld

Camera action is included in enhance context but the LLM translates it into natural language for the prompt.

---

## Image Direction Presets

Project-level visual style:
Photorealistic, Cinematic, Cartoon, Anime, Sketch, Watercolor, Oil Painting, 3D Render, Comic Book, Pixel Art, Abstract, Surreal, Custom (free text)

Included in enhance context as `"Image direction: {preset_or_custom_text}"`.
