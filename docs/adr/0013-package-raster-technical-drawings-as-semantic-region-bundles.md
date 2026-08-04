# Package raster technical drawings as semantic-region bundles

**Status:** accepted

A caller-declared raster technical drawing ingested with `bundle=true` and `image_mode=technical_drawing` is sealed as one flat Atomic Resource Bundle containing the unchanged source, lossless PNG crops of VLM-proposed view-level regions, compact Bundle semantic sidecars, and Manifest-owned source-pixel linkage. Its L1 synthesizes the drawing and includes one concise navigation entry per artifact instead of repeating one long description per crop. It does not use fixed tiles, masks, a duplicate preview, or a region sidecar because overlapping raw crops preserved the useful drawing context in the pilot and the Manifest already inventories each derived artifact.

Recognition is explicitly fallible: an unavailable model or invalid region response publishes the authoritative source with a failed-recognition Manifest and no fabricated crops, while invalid image input or any post-recognition crop, hash, semantic, or publication failure aborts atomically. This exception preserves user data when semantic enrichment fails without weakening the integrity of a successfully recognized Bundle.
