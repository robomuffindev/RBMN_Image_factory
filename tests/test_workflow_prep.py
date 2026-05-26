"""Workflow preparation unit tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import PROJECT_ROOT
from app.services.comfyui.workflow import (
    ANTI_TEXT_SUFFIX,
    flatten_group_nodes,
    klein_workflow_path_for_refs,
    prepare_klein_workflow,
    remove_missing_nodes,
)

WORKFLOWS = PROJECT_ROOT / "app" / "services" / "comfyui" / "workflows"


def test_anti_text_suffix_appended():
    wf = prepare_klein_workflow(WORKFLOWS / "klein_t2i.json", "a forest", 1024, 1024, 42, [])
    # find positive prompt node
    nodes = [n for n in wf.values() if isinstance(n, dict) and (n.get("_meta") or {}).get("title") == "CLIP Text Encode (Positive Prompt)"]
    assert nodes
    text = nodes[0]["inputs"]["text"]
    assert ANTI_TEXT_SUFFIX.split(",")[1].strip() in text  # "no subtitles" appears once


def test_anti_text_suffix_not_doubled():
    wf = prepare_klein_workflow(WORKFLOWS / "klein_t2i.json", f"a forest{ANTI_TEXT_SUFFIX}", 1024, 1024, 42, [])
    nodes = [n for n in wf.values() if isinstance(n, dict) and (n.get("_meta") or {}).get("title") == "CLIP Text Encode (Positive Prompt)"]
    text = nodes[0]["inputs"]["text"]
    assert text.count("no watermarks") == 1


def test_width_height_seed_set():
    wf = prepare_klein_workflow(WORKFLOWS / "klein_t2i.json", "x", 768, 1152, 1234, [])
    for n in wf.values():
        if not isinstance(n, dict):
            continue
        title = (n.get("_meta") or {}).get("title")
        if title == "Width":
            assert n["inputs"]["value"] == 768
        if title == "Height":
            assert n["inputs"]["value"] == 1152
        if title == "RandomNoise":
            assert n["inputs"]["noise_seed"] == 1234


def test_ref_slots_populated():
    wf = prepare_klein_workflow(WORKFLOWS / "klein_4ref.json", "x", 1024, 1024, 1, ["a.png", "b.png", "c.png", "d.png"])
    titles_to_image = {(n.get("_meta") or {}).get("title"): n.get("inputs", {}).get("image") for n in wf.values() if isinstance(n, dict)}
    assert titles_to_image["Load Image"] == "a.png"
    assert titles_to_image["Reference 2 Image"] == "b.png"
    assert titles_to_image["Reference 3"] == "c.png"
    assert titles_to_image["Reference 4"] == "d.png"


def test_workflow_path_picker():
    assert klein_workflow_path_for_refs(WORKFLOWS, 0).name == "klein_t2i.json"
    assert klein_workflow_path_for_refs(WORKFLOWS, 1).name == "klein_1ref.json"
    assert klein_workflow_path_for_refs(WORKFLOWS, 4).name == "klein_4ref.json"
    assert klein_workflow_path_for_refs(WORKFLOWS, 9).name == "klein_4ref.json"  # clamped


def test_flatten_group_nodes():
    wf = {
        "1217:1089": {"class_type": "Foo", "inputs": {"x": 1}},
        "2": {"class_type": "Bar", "inputs": {"y": ["1217:1089", 0]}},
    }
    flat = flatten_group_nodes(wf)
    assert "1217_1089" in flat
    assert flat["2"]["inputs"]["y"] == ["1217_1089", 0]


def test_remove_missing_nodes():
    wf = {
        "1": {"class_type": "Foo", "inputs": {}},
        "2": {"class_type": "Missing", "inputs": {}},
        "3": {"class_type": "Bar", "inputs": {"x": ["2", 0]}},
    }
    removed = remove_missing_nodes(wf, {"Foo", "Bar"})
    assert any("Missing" in r for r in removed)
    assert "2" not in wf
    assert "x" not in wf["3"]["inputs"]
