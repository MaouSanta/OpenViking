# Direct OpenViking G1 回旋线 A/B/C 诊断实验

状态：`DIAGNOSTIC_COMPLETE_ALL_STRICT_PASS`。这是一次可复现的单题、单次/组诊断，
不是统计显著性结论，也不代表完整道路动态导线设计合规。

## 为什么换这道题

公开基准来自 Bertolazzi–Frego 的 G1 Hermite clothoid 拟合。隐藏输入采用道路
测量 `(E,N,azimuth)`，终点方位角跨越 `360°`，曲率由负跨正，并要求输出
`s=0.37L` 内部桩点。Agent 必须完成坐标约定转换、角度最近展开、非线性
Newton 选支、参数恢复及独立端点回代；不是把固定步骤换一组半径重放。

三组均为 `fork_turns=none` 的全新 Codex 子 Agent，工作目录隔离；没有调用
VikingBot、`ov chat` 或 `/bot/v1/chat`。A 先运行，之后才导入 B 公式卡；B
完成后才安装 C SDK。

| 组 | 知识路径 | strict | total | uncached input | output | reasoning | 工具调用 |
|---|---|---:|---:|---:|---:|---:|---:|
| A | 公开名称 + 联网原文 | PASS | 923,386 | 68,106 | 11,248 | 5,012 | 14 |
| B | `ov find/read` 公式卡，自写 Python | PASS | 351,404 | 37,292 | 10,240 | 5,599 | 8 |
| C | `ov find/read/tree/get` SDK，直接执行 | PASS | 365,601 | 18,658 | 3,647 | 1,515 | 10 |

结论：A→B total token 减少 `61.9%`，A→C 减少 `60.4%`，同时三组数值均
通过终点、canonical 参数和内部桩点三层验证。SDK 相对公式卡把 output 降低
`64.4%`、reasoning 降低 `72.9%`，证明“固化可执行实现”避免了重写算法；
但 cold C 多出 `tree/get/self-test` 等工具轮次，反复计入大块 cached context，
因此 total 反而比 B 高 `4.0%`。所以当前证据支持“OpenViking 避免联网重建”，
尚不支持“冷启动 SDK 的 total 一定低于公式卡”。下一步只值得测试一件事：
把 SDK materialize/cache 合并为一次工具调用，再做 3–5 个新输入的交错重复。

## 固化内容与验证

- B：`viking://resources/g1-clothoid-formula-card-v1/formula_memory.md`
- C：`viking://agent/skills/g1-clothoid-road-sdk`
- C 下载脚本与本地 canonical SDK 的 SHA-256 均为
  `79cc62e597ed1a74072e1b0d452f4704b3245b445caa905897e07ffe87820656`。
- 数值门槛：终点位置 `<=1 mm`、方向 `<=1e-5 rad`；长度 `<=1e-4 m`，
  起终曲率 `<=1e-8 1/m`；内部桩点位置 `<=1 mm`、方向 `<=1e-5 rad`。

运行校验：

```bash
python3 benchmark/custom/direct_ov_clothoid/validate.py
```

完整结构化证据见 `results.json`。公开来源：
[论文](https://arxiv.org/abs/1305.6644)、
[作者参考实现](https://github.com/ebertolazzi/G1fitting)、
[BSD-2-Clause 许可](https://github.com/ebertolazzi/G1fitting/blob/master/licence.txt)。
