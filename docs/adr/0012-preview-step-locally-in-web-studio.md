# Preview STEP locally in Web Studio

**Status:** accepted

Web Studio downloads a selected `.stp` or `.step` source through the existing authenticated content API, parses and triangulates up to `50 MiB` locally with bundled `occt-import-js`, and renders the transient result with the existing Three.js dependency. The preview does not change the backend, publish a converted GLB, or add derived files to an Atomic Resource Bundle; server-side conversion remains an option only if measured browser limits require it later.
