# Package DWG as deterministic bundle artifacts

**Status:** accepted

For a `.dwg` source ingested with `bundle=true`, LibreDWG JSON is an assembly-time intermediate, not a published artifact. The sealed bundle contains the unchanged source DWG, deterministic `beam_topology.json`, deterministic `drawing_text.json`, ordinary OpenViking `.abstract.md` and `.overview.md`, optional parser-produced `preview.png`, and a `manifest.json` written last in staging. The manifest inventories every published artifact except itself by role, relative path, schema, and SHA-256. DWG-specific VLM navigation is removed; ordinary semantic generation consumes the parsed bundle artifacts. Any required parse, artifact, or semantic failure terminates the request without retry or partial publication.
