# DWGParser：完整存储，按需读取

**状态：完整 JSON 存储沿用 v9，当前并发链路为 v10。** T0202 从 v9 的 291.452 秒进一步降至 170.268 秒；v8/v9 产物及源码快照保留，旧批量导入和 55 题评测继续暂停。本文记录存储取舍，最新调度与消融结果见[并发验证](dwg-parser-concurrency-validation.md)。

## 本轮解决什么

T0202 v8 生成了 4,305 个 parser JSON，其中 2,145 个 metadata 文件。它们各自满足模型输入预算，却让目录难以浏览。JSON 已明确不做 Embedding，也不走自动 VLM 或摘要，因此取消持久化 JSON 的 2048 估算 tokens / 6000 字符限制，将存储粒度与读取范围分开。

导入提速以实测为准：v8 的 560.419 秒中，解析、图像 VLM、产物生成和摘要前准备合计 172.818 秒；逐 Sheet 摘要及调度占 382.024 秒；发布、索引与返回占 5.577 秒。不能把全部耗时归因于小 JSON 写盘。本轮同时消除 Sheet 摘要的逐张串行等待，复用已有队列并发。

## 产物

```text
<dwg-document>/
├── <original-name>.dwg
├── manifest.json                 # OV 系统清单
├── metadata.json                 # 顶层字段、块定义及内部图形、非图形记录
├── drawing.json                  # 跨 Sheet、框外或位置未知的放置对象
└── sheets/
    └── <sheet-id>/
        ├── manifest.json         # OV 系统清单
        ├── context*.md
        └── drawing.json          # 本 Sheet 的放置对象
```

每个作用域是一份完整 JSON，不再按字符数切成文件或把单个对象写成多个 fragment。新封装使用 `openviking.dwg-json/v1`，保持 `records` 数组以及原对象序号。空的 drawing 作用域保留空数组。旧 `openviking.dwg-json-chunk/v1` 分片仍可读取、重组；新解析不再生成它们。

metadata 中的块定义包含实际线条、文字和嵌套 INSERT，不能理解成少量说明字段。drawing 中的 INSERT 通过原生引用使用这些定义。定义保留一次，未使用的定义也保留；不持久化额外的展开实例图谱或 handle→文件目录。

JSON 采用多行格式，用户和 Agent 可复用已有 `read(offset, limit)` 按行读取，并结合已有查找能力定位内容。本轮不增加 handle/block/bbox 查询接口或分页框架。按行读取返回的局部文本不承诺是独立合法 JSON；服务端现有读取实现仍会读取文件后切行，不能称为流式或按对象随机访问。

## 模型处理边界

| 输入/产物 | 自动模型处理 | Embedding |
|---|---|---|
| 临时 Sheet 图像 | 沿用 v8 的说明/独立表格识别及 Sheet 候选校验 | 不做 |
| Sheet `context*.md` | 沿用 Markdown 摘要规则 | 沿用现有索引链路 |
| 所有 DWG JSON | 本地整理、校验、登记和读取 | 不做 |

正式 DWG 导入只允许 Sheet `context*.md` 进入摘要与索引。父目录刷新、混合目录、reindex、git restore 和最终 Embedding 入队同时识别新旧 DWG JSON schema；数据文件、纯数据目录及其陈旧隐藏摘要不能成为模型输入。该判定依据产物身份，不依赖文件名。问答时按需读取 JSON 证据仍允许。

取消的是 JSON 的存储预算。Markdown 仍遵守现有分段与 Embedding 预算。旧 `dwg.json_max_tokens`、`dwg.json_max_chars` 配置随无用写入逻辑删除，已有配置如包含这两个字段，需要删除对应 `dwg` 段。

## 摘要与重用

在保持 Markdown 生成规则、摘要提示、调用策略和模型配置不变的前提下，先校验 Sheet 标识、目录和内容白名单，再将所有 Sheet 摘要交给现有队列，统一等待完成，之后封存清单和发布。不能对共享同一 telemetry tracker 的旧逐 Sheet 等待函数直接 `gather`，否则会竞争清理请求状态。失败仍使整个文档无法发布，并保留原有回滚边界。

多 Sheet 批次超过等待 deadline 后，会先等待已接收的 worker 收束，确保回滚后不再有迟到写入，再返回超时错误。因此错误返回可能晚于 timeout；本次实测使用 `timeout=None`。

重复评测优先使用原生带向量 ovpack：它保留原始 DWG、解析产物、摘要及向量，可恢复到独立 OV，避免重复 LibreDWG、渲染、图像 VLM 和摘要。本轮不新增 parser cache 或评测专用入库框架。parser、模型配置或提示改变时旧包不能冒充新结果。

T0202 的包恢复实测为 **0.750 秒**，恢复及重导出窗口内模型调用尝试为 0，内容、索引及向量字节一致；新 OV 初始化不计入此时间。首次恢复暴露的 Bundle 清单写入问题已修复，失败现场单独保留。

## 保全约束

- 原始 DWG 字节不变，不导出 `sheet.dwg`，不额外保存完整 `structure.json` 副本。
- 除已实际转入 Markdown 的正文外，保留全部原字段、类型、数组内容及顺序；原 `OBJECTS` 序号恢复源对象顺序。
- 正文只有在实际写入 MD、且所有真实使用实例覆盖后才可移除；图区标注、未知用途、不同值的文本别名及原生表格结构仍保留。
- 空间排序只决定对象排列和作用域，不改写原坐标。不能唯一归属某个 Sheet 的放置对象保留在文档 drawing；未知归属保留 `partial`。
- 原生引用、必要的 Markdown 实例来源、清单哈希与完整目录登记继续保留。

## 逐项验证

1. 固定同一份原生输入、Sheet 和 Markdown 选择，只比较 v8 小分片与 v9 完整 JSON：字段、归属、顺序、MD 字节一致；记录文件数与 JSON 生成耗时。
2. 用同步事件测试 Sheet 摘要确实能重叠运行，所有任务完成前不能发布，单项失败仍回滚；不新增生产实验开关。
3. 在独立 OV 重新导入 T0202，记录真实阶段耗时、Sheet/MD 数量和模型调用边界。VLM 返回可能存在随机性，实测有差异时明确报告，不将全部差异归因于代码。
4. 核对新 JSON 和源对象、MD 来源、清单以及真实向量记录；JSON 向量必须为 0。通过正式读取 API 验证按行读取完整文件。
5. 导出原生 ovpack，再恢复到独立空作用域，验证产物/向量一致，确认重用过程不调用模型。

本轮不减少 Sheet 的 VLM 调用，不绕开产物登记，也不启动多 DWG 并发。是否进一步削减模型工作，要用实际耗时和固定题集验证。没有 55 题 A/B 结果前，不宣称答题能力不变。

T0202 实测保留 19 Sheet、24 Markdown、37,749 个对象，parser JSON 从 4,305 份变为 21 份。图像 VLM 19 次，Markdown 摘要输入 24 份，最高同时处理 4 个 Sheet；JSON 摘要输入与向量均为 0。JSON 生成 2.403 秒，校验登记 0.522 秒；图像 VLM 105.726 秒、摘要批次 135.900 秒是主要耗时。两次实跑有 12/24 份 Markdown 字节不同，说明 VLM 返回仍有波动；不能将这轮结果称为固定输出的纯调度性能对照。固定输入的存储测试单独通过。

参见：[ADR-0013](../adr/0013-make-dwg-sheets-the-retrieval-unit.md) · [验证记录](dwg-parser-json-validation.md)。
