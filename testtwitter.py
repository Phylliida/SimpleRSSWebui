import zendriver as uc
import asyncio
import base64
import json
from pathlib import Path
from urllib.parse import urlparse, unquote


async def main():
    browser = await uc.start()
    page = await browser.get("about:blank")

    output_dir = Path("cached_images")
    output_dir.mkdir(exist_ok=True)

    lookup_map = {}
    counter = 0

    async def on_response(event: uc.cdp.network.ResponseReceived):
        nonlocal counter
        mime = event.response.mime_type or ""
        if "image" not in mime:
            return

        try:
            result = await page.send(
                uc.cdp.network.get_response_body(event.request_id)
            )
            body_str, is_base64 = result

            if is_base64:
                image_data = base64.b64decode(body_str)
            else:
                image_data = body_str.encode("utf-8")

            # Derive extension from MIME type
            ext = mime.split("/")[-1]
            ext = ext.replace("svg+xml", "svg").replace("jpeg", "jpg")

            # Extract a clean base name from the URL path
            parsed = urlparse(event.response.url)
            path_name = unquote(parsed.path.split("/")[-1])  # last segment
            # Strip query junk and get stem
            stem = Path(path_name).stem if path_name else ""
            # Fallback if stem is empty or just whitespace
            if not stem or not stem.strip():
                stem = "image"

            # Ensure unique filename by appending counter
            fname = f"{counter:04d}_{stem}.{ext}"
            counter += 1

            filepath = output_dir / fname
            filepath.write_bytes(image_data)

            # Store the mapping
            lookup_map[fname] = event.response.url

            print(f"Saved: {fname}  ←  {event.response.url[:100]}")

        except Exception as e:
            print(f"Failed to get body for {event.response.url[:80]}: {e}")

    page.add_handler(uc.cdp.network.ResponseReceived, on_response)
    await page.send(uc.cdp.network.enable())

    await page.get("https://example.com")
    await page.sleep(500)

    # Save the mapping to a JSON file
    mapping_path = output_dir / "url_mapping.json"
    mapping_path.write_text(json.dumps(lookup_map, indent=2))
    print(f"\nMapping saved to {mapping_path} ({len(lookup_map)} images)")

    browser.stop()


asyncio.run(main())
