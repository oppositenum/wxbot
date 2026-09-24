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
    fn = src[src.index("def gen_image"):src.index("\ndef available")]
    assert "if reference and not grok_image" in fn


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
