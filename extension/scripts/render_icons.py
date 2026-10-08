"""
Render the extension icons (16/32/48/128 px PNG) from the dashboard's favicon.

Development tool only: the PNGs are committed, so building or loading the
extension never needs this. Requires Playwright for Python and a Chromium:

    pip install playwright && python -m playwright install chromium
    python extension/scripts/render_icons.py [--chromium /path/to/chrome]

Run from the repository root (reads frontend/public/favicon.svg, writes extension/icons/).
"""
import argparse
import base64
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[2]
SVG = ROOT / "frontend" / "public" / "favicon.svg"
OUT = ROOT / "extension" / "icons"
SIZES = (16, 32, 48, 128)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--chromium", help="Chromium executable (default: Playwright's own)")
    args = parser.parse_args()

    svg_b64 = base64.b64encode(SVG.read_bytes()).decode("ascii")
    OUT.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(executable_path=args.chromium) if args.chromium else p.chromium.launch()
        try:
            for size in SIZES:
                page = browser.new_page(viewport={"width": size, "height": size}, device_scale_factor=1)
                page.set_content(
                    "<!doctype html><html><body style='margin:0;background:transparent'>"
                    f"<img src='data:image/svg+xml;base64,{svg_b64}' width='{size}' height='{size}' "
                    "style='display:block'></body></html>"
                )
                page.wait_for_function("document.images[0].complete")
                target = OUT / f"icon{size}.png"
                page.screenshot(path=str(target), omit_background=True,
                                clip={"x": 0, "y": 0, "width": size, "height": size})
                page.close()
                print(f"wrote {target.relative_to(ROOT)}")
        finally:
            browser.close()


if __name__ == "__main__":
    main()
