# DWG 浏览器多段线 OCS 修复

2026-09-14。适用依赖：`@mlightcad/cad-simple-viewer@1.5.9`、`@mlightcad/data-model@1.12.2`、`@mlightcad/geometry-engine@3.12.2`。

总平面与室外电缆图中，一些标注多段线使用非默认 OCS 和 elevation。Web Studio 的浏览器转换器丢失这两个字段，绘制函数又把局部顶点直接作为世界坐标，导致对象落在远处。“适配视野”使用已绘制场景范围，因此把真正的图纸缩成很小一块。

修复有两部分：

- `web-studio/script/apply-dwg-preview-patch.mjs`：安装时对固定版本 viewer 的浏览器 ESM 转换器应用小型兼容补丁，保留 elevation、thickness 和 extrusion。按 LibreDWG 存在标志位读取（8、2、1），闭合位仍为 512。未设置 extrusion 时必须保留默认 (0,0,1)，不能采用底层返回的 (0,0,0)。脚本检查版本及源码形状，可重复执行；通过原子替换避免改动 pnpm 内容缓存的硬链接。
- `web-studio/src/lib/cad/dwg-polyline-ocs.ts`：打开预览前安装一次绘制/范围适配。继续使用上游的曲线及宽多段线细分，在临时的零 elevation 绘制对象上施加 OCS 到所属坐标系的变换，再由原有 INSERT 链路施加块变换。几何范围同步变换。源顶点和 DWG 字节不修改。

基向量作为矩阵列排列。没有直接调用当前 geometry-engine 的 `setFromExtrusionDirection`，因为该版本按行排列基向量，非对称 OCS 情况下方向不符；测试包含由 ezdxf 独立计算的斜平面坐标。

正常 `npm install` / `npm ci` 会自动运行补丁。若明确使用 `--ignore-scripts`，启动/构建前应执行：

```sh
cd web-studio
node script/apply-dwg-preview-patch.mjs
```

手动“适配视野”通过公开的 cadScene.box 和 zoomTo API，立即适配当前已绘制范围。它不再使用上游自动开图适配控制器，避免无限等待绘制结束，或在用户缩放后因自动适配被取消而不响应。空或非有限范围不会移动相机，部分预览的状态提示不变。

升级上述依赖时应重新检查，并同时移除/替换转换器补丁和运行时适配，防止与上游修复叠加。本修复针对 Web Studio 浏览器入口，不改变该依赖的 Node CJS 转换器。

验证包括：实际安装后的转换器函数、默认与非默认标志位、两张真实图的标注端点、镜像/斜平面、宽多段线、bulge 弧段、重复绘制不改源数据、嵌套 INSERT 组合、重复初始化，以及原有预览组件测试。共 38 项测试。

真实图回归另行记录浏览器绘制范围、控制台错误及适配后的截图，不能只凭 Sheet 数量判断渲染正确。供氢站原有的大图例、散置说明与其字号应保持原样。原预览器的缺失字体/参照和部分预览提示继续保留。
