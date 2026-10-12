---
name: formula-memory-compiler
description: Compile engineering formula sources into a concise, self-contained calculation memory with aliases, strict dependency order, units, branches, and back-substitution checks.
---

# Formula Memory Compiler

Read every supplied source file. Produce exactly one reader-oriented Wiki page
in the requested Memory target. Preserve only claims and numeric examples that
are present in the sources; never invent a formula or a missing gold value.

The page must be self-contained and optimized for later automatic recall when
a user supplies parameters but names only the method. It must include:

1. method name, source/report identifier, and retrieval aliases in Chinese and English;
2. scope boundary, coordinates, units, angle convention, and left/right sign rule;
3. every formula in strict dependency order, with explicit indices;
4. the tangent-gap and station propagation order;
5. required back-substitution residuals and failure conditions;
6. public worked values explicitly present in the source;
7. a short warning that this Gate 0 is not full road-design compliance.

Use a specific title containing `dynamic traverse`, `动态导线`, and
`circular-intersection-v1`. Use the supplied source identifier in
`source_ids`. Submit the page with `submit_wiki_bundle`; do not create raw
artifact files and do not use `exec`.
