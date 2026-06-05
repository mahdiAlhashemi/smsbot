"""Generate 3D icon logo concepts for the NumberHub bot via the OpenAI Image API.

The API key is read from the OPENAI_API_KEY env var (never hard-coded / committed).
Saves PNGs into ./assets/.
"""
from __future__ import annotations

import base64
import os
import pathlib
import sys
import time

import httpx

API = "https://api.openai.com/v1/images/generations"
MODELS = ["gpt-image-2", "gpt-image-1"]  # try newest first, fall back
OUT = pathlib.Path("assets")
OUT.mkdir(exist_ok=True)

COMMON = (
    "Premium 3D rendered app icon, glossy rounded-square shape, vibrant indigo-to-violet "
    "gradient background, soft studio lighting, smooth glass and plastic materials, subtle "
    "reflections and soft shadows, modern, minimal, clean, centered composition that also "
    "reads well cropped to a circle, high detail, dribbble style, NO text, NO letters."
)

CONCEPTS = {
    "numberhub_hub": (
        "A central glowing 3D hub sphere with thin luminous connection lines radiating out to "
        "small floating 3D chat/SMS speech-bubble nodes and rounded phone-number tiles around it, "
        "like a network hub. " + COMMON
    ),
    "numberhub_bubble": (
        "A floating glossy 3D white rounded speech/SMS bubble with a bold glowing hash '#' symbol "
        "inside it and a couple of tiny sparkles, hovering above a soft shadow. " + COMMON
    ),
    "numberhub_node": (
        "A bold 3D letter-N-like shape built from connected glowing nodes and luminous lines in a "
        "network-hub style, with a small floating SMS speech bubble accent beside it. " + COMMON
    ),
}


def main() -> int:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        print("ERROR: OPENAI_API_KEY not set")
        return 1

    # Image renders are slow; allow a generous read timeout and isolate each call.
    timeout = httpx.Timeout(420.0, connect=30.0)
    model = [MODELS[0]]  # mutable so a fallback persists across concepts
    saved = []

    def generate(name: str, prompt: str, client: httpx.Client) -> None:
        body = {"model": model[0], "prompt": prompt, "size": "1024x1024",
                "quality": "high", "n": 1}
        t0 = time.monotonic()
        print(f"-> generating {name} with {model[0]} ...", flush=True)
        try:
            r = client.post(API, headers={"Authorization": f"Bearer {key}"}, json=body)
        except httpx.TimeoutException:
            print(f"   TIMEOUT after {time.monotonic() - t0:.0f}s — skipping {name}")
            return
        if r.status_code in (404,) or (
            r.status_code == 400 and "model" in r.text.lower() and model[0] == MODELS[0]
        ):
            model[0] = MODELS[1]
            print(f"   {MODELS[0]} unavailable, retrying with {model[0]} ...", flush=True)
            body["model"] = model[0]
            try:
                r = client.post(API, headers={"Authorization": f"Bearer {key}"}, json=body)
            except httpx.TimeoutException:
                print(f"   TIMEOUT after {time.monotonic() - t0:.0f}s — skipping {name}")
                return
        if r.status_code != 200:
            print(f"   FAILED [{r.status_code}]: {r.text[:400]}")
            return
        b64 = r.json()["data"][0].get("b64_json")
        if not b64:
            print(f"   no image data for {name}")
            return
        path = OUT / f"{name}.png"
        path.write_bytes(base64.b64decode(b64))
        print(f"   saved {path} ({path.stat().st_size // 1024} KB) in {time.monotonic() - t0:.0f}s")
        saved.append(str(path))

    only = sys.argv[1] if len(sys.argv) > 1 else None
    with httpx.Client(timeout=timeout) as client:
        for name, prompt in CONCEPTS.items():
            if only and only not in name:
                continue
            generate(name, prompt, client)

    print("\nDONE. Saved:", saved if saved else "NOTHING")
    return 0 if saved else 2


if __name__ == "__main__":
    sys.exit(main())
