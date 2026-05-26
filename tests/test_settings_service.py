from app.db.models import AppSettings
from app.services.settings_service import apply_update, is_masked, mask_secret, to_response


def test_mask_secret():
    assert mask_secret(None) is None
    assert mask_secret("") == ""
    assert mask_secret("abc") == "********"
    assert mask_secret("sk-abcdefgh") == "********efgh"


def test_is_masked():
    assert is_masked("********abcd")
    assert not is_masked("sk-realkey")


def test_apply_update_ignores_masked():
    row = AppSettings(id=1, openai_api_key="sk-real")
    apply_update(row, {"openai_api_key": "********eal"})  # masked-shaped
    assert row.openai_api_key == "sk-real"


def test_apply_update_writes_new_secret():
    row = AppSettings(id=1, openai_api_key="old")
    apply_update(row, {"openai_api_key": "sk-fresh"})
    assert row.openai_api_key == "sk-fresh"


def test_apply_update_cleans_urls():
    row = AppSettings(id=1)
    apply_update(row, {"comfyui_urls": [" http://a/ ", "", "http://b"]})
    assert row.comfyui_urls == ["http://a", "http://b"]


def test_response_masks_secrets():
    row = AppSettings(id=1, openai_api_key="sk-realkey")
    resp = to_response(row)
    assert resp["openai_api_key"].startswith("********")
