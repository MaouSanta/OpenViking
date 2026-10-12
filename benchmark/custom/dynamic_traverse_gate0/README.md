# Dynamic-traverse memory A/B, Gate 0

## Outcome: diagnostic only

OpenViking successfully compiled a 21-step circular-intersection calculation
contract into peer-private memory and recalled it in three fresh B sessions.
B was strict-correct 3/3 and made zero web calls. A made six web calls, but none
retrieved the official FHWA PDF. The private aliases `Gate 0` and
`circular-intersection-v1` existed only in B's compiled contract, so source
availability was asymmetric. Classify the run as `BLOCKED_WEB_SOURCE_RETRIEVAL`,
not as a fair token A/B.

This is a Gate 0 test of the circular-curve intersection kernel, not proof of a
complete dynamic-traverse road-design method.

## Frozen protocol

- Environment: OpenViking `0.4.13.dev8`, VikingBot model
  `doubao-seed-2-0-pro-260215`, 2026-08-11.
- A: `user_id=dynamic-a`, internet enabled, no compiled memory.
- B: `user_id=dynamic-b`, internet enabled, plus peer-private compiled memory.
- Same parameter-only prompt and output schema; three new sessions per arm.
- Between turns, only Agent-generated calculation files were moved to a
  temporary archive so the shared direct sandbox could not leak prior code.
- Oracle: `validate.py` uses a separate complex-number implementation and first
  reproduces the FHWA published control. Absolute tolerance: `5e-6 ft/rad`.
- Hidden task changes `R2` from 1250 to 1234.5 and `R3` from 950 to 987.25;
  these expected outputs were not in the source or compiled memory.

Compile result:

```text
task_id: cmp_a5623972437f4db5acf11a31479b552a
status: completed
created: viking://user/default/peers/dynamic-b/memories/entities/dynamic-traverse/dynamic_traverse_动态导线_circular-intersection-v1_计算规.md
warnings: []
```

## Results

| Run | Geometry + stations | Strict all fields | Memory hit | Web calls | Total tokens |
| --- | ---: | ---: | ---: | ---: | ---: |
| A1 | yes | no | no | 1 | 191342 |
| A2 | no | no | no | 2 | 37890 |
| A3 | yes | no | no | 3 | 57381 |
| B1 | yes | yes | yes | 0 | 42269 |
| B2 | yes | yes | yes | 0 | 77124 |
| B3 | yes | yes | yes | 0 | 41103 |

Median A/B total tokens were 57381/42269. The descriptive difference is
15112 tokens (26.34%), but it is **not** a valid correct-result saving because
there were no A/B strict double-PASS pairs and A failed the source-readiness
gate. Compile's one-time LLM token cost is also
not exposed by the current public Compile status API. ChatResponse token fields
cover Agent-loop prompt/completion tokens; retrieval embedding tokens are not
included.

A1 returned correct geometry and stations but used the wrong turn/sign
convention. A2 returned correct curve geometry but broke downstream station
propagation. A3 returned every numeric value, station, and residual correctly,
but mislabeled both turns. Thus `0/3` is only the strict-schema score; it does
not mean A failed to calculate all three cases. All A responses had
`relevant_memories=null`.

## Correct next experiment

Use three arms to separate two different benefits:

- A: online access to the same named public source; block the run unless the
  official PDF is retrieved.
- B: compiled formula/order memory; generate Python per turn.
- C: a validated executable Skill/SDK; map parameters and call Python directly.

The minimal C package is `SKILL.md` plus one parameterized stdlib script with
JSON input/output, residual rejection, and `--self-test`. Memory should retain
only aliases, scope, version, SDK URI, hash, and validation status. The Python
belongs in a Skill/Resource artifact, not inline memory prose.

## Reproduce

The exact prompt is embedded in `run.py`; the source and compiler Skill are
frozen beside it. Import them into an empty peer-private namespace and compile:

```bash
ov add-skill compiler --wait
ov mkdir viking://user/default/peers/dynamic-b/resources/dynamic-traverse-source
ov add-resource source.md --parent viking://user/default/peers/dynamic-b/resources/dynamic-traverse-source --wait
ov compile --from viking://user/default/peers/dynamic-b/resources/dynamic-traverse-source \
  --to viking://user/default/peers/dynamic-b/memories/entities/dynamic-traverse \
  --skill viking://user/default/skills/formula-memory-compiler --wait
```

Then execute:

```bash
python run.py A 1
python run.py B 1
python run.py A 2
python run.py B 2
python run.py A 3
python run.py B 3
python validate.py
```

Raw non-streaming ChatResponse JSON, including events, memory context, and exact
prompt/completion/total token counts, is under `results/`.
