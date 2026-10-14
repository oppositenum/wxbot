import os
from core import love_and_deepspace as lnds


def test_named_characters():
    assert lnds.named_in("画一个秦彻给女主挡头上车") == ["秦彻"]
    assert lnds.named_in("黎深和夏以昼") == ["夏以昼", "黎深"]
    assert lnds.named_in("随便画只猫") == []


def test_decorate_prompt_only_for_cast():
    raw = "画一个秦彻挡雨"
    out = lnds.decorate_prompt(raw)
    assert "Love and Deepspace" in out
    assert "秦彻" in out
    assert lnds.decorate_prompt("画一只猫") == "画一只猫"


def test_grok_imagine_skips_edits_endpoint():
    src = open("core/llm.py", encoding="utf-8").read()
    fn = src[src.index("def _post_image"):src.index("\ndef available")]
    assert "if reference and not grok_image" in fn
    assert "_grok_image_unavailable" in src
    assert "falling back to" in src


def test_grok_media_503_falls_back_to_gpt(monkeypatch):
    from core import llm
    calls = []

    def fake_post_image(prompt, size, cfg, reference, base, key, model, grok_image, quality):
        calls.append((model, grok_image, key))
        if grok_image:
            raise RuntimeError('模型接口 HTTP 503；未切换 provider/model：{"error":{"message":"No eligible Grok media accounts"}}')
        return b"png-bytes"

    monkeypatch.setattr(llm, "_post_image", fake_post_image)
    monkeypatch.setattr(llm, "image_creds", lambda cfg: ("https://grok.example/v1", "grok-key", "grok-imagine-image-2.0"))
    monkeypatch.setattr(llm, "image_follow", lambda cfg: "grok")
    monkeypatch.setattr(llm, "image_quality", lambda cfg: "low")
    monkeypatch.setattr(llm, "creds", lambda cfg, provider: ("https://gpt.example/v1", "gpt-key", "gpt-4o") if provider == "gpt" else ("", "", ""))
    out = llm.gen_image("画一只猫", cfg={"image": {"follow": "grok", "model": "grok-imagine-image-2.0"}})
    assert out == b"png-bytes"
    assert calls[0][0] == "grok-imagine-image-2.0" and calls[0][1] is True
    assert calls[1][0] == "gpt-image-1" and calls[1][1] is False


def test_reference_files_exist():
    for name in lnds.CHARACTERS:
        folder = os.path.join(lnds.asset_dir(), name)
        assert os.path.isdir(folder), folder
        files = [f for f in os.listdir(folder) if f.lower().endswith((".jpg", ".jpeg"))]
        assert files, name
    path = lnds.reference_path("画一个黎深")
    assert path and os.path.isfile(path)
    data = lnds.reference_bytes("秦彻立绘")
    assert data and data[:2] == b"\xff\xd8"
