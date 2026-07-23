# Use explicit atomic resource bundle roots

**Status:** accepted

An atomic resource bundle is identified by an explicit marker, not by whether its directory currently has children. It is a generic, opt-in resource primitive rather than a 3D-only type: 3D is its first consumer, while existing resources and ordinary directories remain unchanged. Its required immutable root-level `manifest.json` is the authoritative identity and source metadata record: it inventories the original asset, source facts, and one canonical Resource Kind with machine-verifiable values. A bundle boundary may contain ordinary directories but never another bundle boundary. Its normalized `key=value` index tags are synchronized projections for exact retrieval. V1 creates no generated overview or Parser sidecar. Retrieval returns the bundle root and its match evidence only; an agent reads the asset card or greps the bundle on demand.
