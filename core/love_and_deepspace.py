"""Love and Deepspace official portrait refs for image generation."""
import os

import config

CHARACTERS = ("夏以昼", "沈星回", "祁煜", "秦彻", "黎深")

STYLE_PROMPT = (
    "Strictly match Love and Deepspace (恋与深空) official 3D character art: "
    "Unreal-engine photoreal cinematic quality, refined 3D face, subsurface-scattering skin, "
    "visible but not rough pores, individual glossy hair strands with volume, large clear eyes "
    "with catchlights and emotion, handsome clean premium look, not overly feminine, not brutish. "
    "Cinematic lighting, accurate leather/fabric/metal, shallow-depth or atmospheric background. "
    "Half-body or close-up, slight 3/4 angle, eye contact. "
    "No 2D flat/cel/thick-paint illustration, no chibi, no plastic skin, no melted hair. "
    "Face structure, hair, and lighting must stay consistent with the attached official reference."
)


def asset_dir():
    nested = os.path.join(config.PROJECT_DIR, "assets", "love-and-deepspace")
    if os.path.isdir(os.path.join(nested, CHARACTERS[0])):
        return nested
    flat = os.path.join(config.PROJECT_DIR, "assets")
    if os.path.isdir(os.path.join(flat, CHARACTERS[0])):
        return flat
    return nested


def named_in(text):
    t = text or ""
    return [name for name in CHARACTERS if name in t]


def reference_path(text):
    names = named_in(text)
    if not names:
        return None
    folder = os.path.join(asset_dir(), names[0])
    if not os.path.isdir(folder):
        return None
    files = [os.path.join(folder, f) for f in os.listdir(folder)
             if f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))]
    if not files:
        return None
    preferred = [p for p in files if "立绘" in os.path.basename(p) or "官方" in os.path.basename(p)]
    pool = preferred or files
    return max(pool, key=os.path.getsize)


def reference_bytes(text):
    path = reference_path(text)
    if not path:
        return None
    with open(path, "rb") as f:
        return f.read()


def decorate_prompt(prompt):
    if not named_in(prompt):
        return prompt
    return (STYLE_PROMPT + " Character and scene: " + (prompt or "").strip())[:1800]
