# Read large images by coordinates

MCP `read` and VikingBot `openviking_multi_read` now return model-sized image content. Ordinary text reads retain their existing interface. No VLM call is made inside the image read operation.

## Usage

Read an image first:

```json
{"uris": "viking://resources/picture/picture.png"}
```

For large images the result contains the full-image thumbnail, original dimensions, and instructions to choose a center point. Read the detail through the same tool:

```json
{"uris": "viking://resources/picture/picture.png", "x": 0.6, "y": 0.4, "width": 1024, "height": 1024}
```

VikingBot uses a one-element `uris` array. Coordinates are normalized to `[0, 1]` in the **EXIF-oriented full image**, with the origin at the top left. They never refer to the previous crop. Width and height are original pixels, defaulting to 2048 each and constrained to 1–2048. Edge windows shift into the image; smaller source images are not enlarged. Returned bounds are `[left, top, right, bottom]` in original pixels, with exclusive right/bottom edges.

HTTP callers opt in via `GET /api/v1/content/read?uri=...&multimodal=true&x=...&y=...`. The response result contains `kind`, `uri`, original and output dimensions, `bounds`, explanatory `text`, `mime_type` and base64 `data`. The async Python SDK exposes `read_image`; existing `read` remains text-only.

## Storage and compatibility

New large-image imports preserve the original, an oriented JPEG preview, and a relocatable `image-source.json` association. They no longer eagerly generate tiles or tile summaries. The original gets a deterministic reference summary; only the preview is sent to the VLM for visual summarization. A read of either associated image URI resolves to the same full-image coordinate system. First reads use the saved preview without loading the original bytes.

The existing thresholds remain configurable: more than 10 MiB or either side above 4096 pixels. A file above the inline transport budget also receives a preview. Returned media obeys the existing raw-byte inline budget (3,932,160 bytes), including batch results. Crops prefer PNG, falling back to JPEG compression if needed. This is a configured size policy, not automatic detection of a model's remaining context.

Old tile-based resources remain readable. Cropping an old `_preview.jpg` next to `tiles/` reports that the original must be re-imported. No automatic migration or tile deletion occurs. Missing originals are never replaced with enlarged thumbnails.

All source, preview, and metadata access uses the caller's identity. Associated paths must be sibling filenames. Source files are limited to 512 MiB, Pillow's decompression safeguards remain enabled, and the service limits concurrent image reads to two. Arbitrary gigapixel images are not guaranteed. Raster images are decoded through Pillow; animated/multipage images use their first frame. DWG/PDF vector rendering is outside this feature.

VikingBot preserves MCP image blocks as images. Providers with native tool-result media receive them there; other chat-style VLMs receive a labeled user image message **after all tool responses in that round**. Existing media budgets and text-only log representations remain in effect. The configured model must support vision.
