# pon_mcp_v2 归档（i-have-adhd 风格）

- 当前状态：v1.8 浅色容器填充（2026-09-21）；承接 pon_mcp_v1（v1–v1.4）。本档归档 v1.5→v1.8 四轮架构图体验打磨
- 0. 怎么用这份归档
  - 下一步：打开幕布 → 搜 pon_mcp_v2 → 点开根节点，按需展开
  - 查找路径：不拥挤保证→一3；分组容器→一4；白屏转圈根因链→一5；浅色容器→一6；死路清单→二2
  - 时间预算：读完约 2 分钟；单章约 20 秒

## 一、版本迭代（v1.5 → v1.8）

- 1. 一句话结论
  - 架构图从"节点挤成一团、边界不清"打磨到"分层不挤 + 虚线分组框 + 浅色衬底层次清晰"，中间踩了三个白屏转圈坑并全部定位修复
- 2. 起因（用户反馈）
  - 架构图节点都挤在一起，没有对称美感
  - 节点间逻辑关系应该用连线 + 大虚线框组合表示
  - 容器背景要和内含节点做色度区分，否则边界不清晰
- 3. v1.5：不拥挤保证
  - 间距加大：横 50→80、纵 80→110
  - 新增 MIN_NODE_GAP=40 硬下限 + _separate_rects 碰撞分离器（贪心推开重叠/过近矩形，选需移动更小方向推）
  - 对显式坐标重叠也兜底（QA：3 个重叠节点 before=3→after=0）
  - 间隙数学：间隙 = max(左) - min(右)（正=分离、负=重叠）；第一版写反把重叠量当间隙导致坐标爆炸，QA 抓出修正
- 4. v1.5：分组容器（group 字段）
  - nodes 加 group（如"接入层"）→ 自动画虚线透明大框包住组内节点（CONT_PAD=48 内边距）
  - 两阶段分离：先容器互推（组内节点随框平移保 padding），再节点互推
  - 跨组连线照常锚在节点边，穿过透明虚线框
- 5. 白屏转圈根因链（三个坑，全部服务端 version 成功但前端崩）
  - 坑1（v1.6 误判）：以为 name="container" + 空 anchors 导致前端按形状名分派崩溃 → 改普通 rectangle，仍转圈（非根因）
  - 坑2（v1.7 真根因）：容器手写 dict 漏了 "path" 字段（矩形轮廓 move→line→line→line→close 绘制指令），前端拿不到 path 就崩 → 修复：复用 _shape("rectangle","basic",...) 生成完整矩形，只覆盖虚线+空填充
  - 坑3（v1.8）：setTheme 会全局覆盖 shape 填充色，我在其后追加一条 update 消息回写容器浅色 → 又转圈 → 根因：updates 对象格式是未抓包组合 → 回退该消息
- 6. v1.8：浅色容器填充（最终态）
  - 每主题新增 container_fill（同色系极浅色）：techblue=224,234,244 vs 节点 0,91,153
  - 视觉层次：浅蓝灰底框 → 深蓝节点 → 虚线边界
  - 注意：setTheme 覆盖问题仍在，浅色容器最终靠 create 时 fillStyle 设定（接受 setTheme 后可能被覆盖为深色，待后续抓包正确 update 格式再解）
- 7. 测试图（已归档，验收后删）
  - v1.7 验证通过图：arch_group_v17（path 修复版，不转圈）
  - v1.8b 最终图：https://www.processon.com/view/link/6ab0e337926a46649d112ee5（用户确认可打开）
  - v1.9 路由避让测试图：https://www.processon.com/view/link/6ab0ebbbaa338a4e8a984f45（10节点12连线，用户确认）
- 8. v1.9：连线路由避让（跨层连线不切无关节点）
  - 问题：复杂架构跨层连线的折线水平段切过中间层无关节点框/容器框
  - 修正：_avoid_boxes_on_link——折线水平段 y 自动向下扫描（步长20）到第一个不穿过任何无关节点/容器的水平走廊，向下到底反向上扫；端点自身除外
  - 原理：水平段落在"节点间空隙带"，垂直段接端点，视觉上连线走走廊不切框

## 二、经验沉淀

- 1. 核心铁律
  - 服务端返回 version:N（写入成功）绝不代表前端能渲染；无抓包样本的新形状字段/新消息格式一律先最小化真机试开，再批量用
  - 手写形状 dict 必须和已验证节点逐字段对齐（path/anchors/resizeDir/attribute 一个不能少）
- 2. 死路清单（勿重复）
  - name="container" + 空 anchors：前端分派崩溃转圈
  - 手写 rectangle 漏 "path"：前端画不出轮廓，转圈
  - setTheme 后追加 update 消息回写字段：updates 对象格式未抓包，转圈
  - mind_free addConnection 省略几何坐标：同根因（v1 已记）
- 3. 调试方法
  - 白屏转圈二分法：先画无容器版定位是容器还是分离逻辑；离线 dump content JSON 逐字段对比节点 vs 容器找缺失字段
  - 间隙/坐标类 bug：QA 脚本断言（0 冲突、padding≥阈值）+ 打印坐标范围防爆炸
- 4. 与 v1 的关系
  - v1 是"三编辑器打通"（能画）；v2 是"画得好看"（不挤、分组、层次）
  - 复用 v1 的 _shape/make_node/make_link，只在 draw_flowchart 加分组与分离层

## 三、vsdx 导出（v2 新增）

### 能力
- `processon_get_chart_def(chart_id)`：读回图定义
  - `canvas/get/chartdefids` 拿 mainCanvasId/definitionId
  - `diagraming/get/chart/def?defId=...` 拿完整 elements JSON（纠正"ProcessOn 无读回接口"旧认知）
- `processon_export_vsdx(chart_id, out_path)`：读回 + 纯 Python 渲染成 .vsdx（Visio）
  - `vsdx_exporter.py`：纯标准库 zipfile 生成 OPC zip（9 个部件）
  - 覆盖：矩形/菱形/胶囊节点、linker 折线连线、分组容器
  - 坐标：PPI=96，像素→英寸，y 轴翻转；颜色 r,g,b→#RRGGBB

### 关键发现
- **vsdx 无服务端下载接口**：`visio.sdk.umd.js`（850KB）grep 无任何 /api 调用——纯前端 mxGraph→VSDX 转换库，浏览器现场转
- **开源 Python 三选**：`vsdx` 库（读强写弱）、`bpmn-to-visio`（真从零生成 OPC，最佳参考，已卸载）、`aspose-diagram`（商业付费）。选 A：自写最小 OPC 生成器

### 渲染踩坑（四轮迭代，drawio 实测验证）
- v1：节点文字写死白色 + 漏 TextBlock 单元格（TxtPinX/Y/W/H/LocPin）→ 文字看不见/乱跑
- v2：补 TextBlock、文字按节点实际色、容器深字
- v3：显式 FillPattern=1 实心填充 + 容器文字移到框顶部
- **v4 真根因（z-order）**：drawio/ProcessOn 把"被大矩形容器盖住的下层节点"识别成容器子形状→丢弃
  - 原以为 attribute.container 判定容器，**实测全是 False**——判定错了
  - 真正的容器是 fillColor="224,234,244" 的浅蓝矩形（字符串色值，非 list）
  - 修复：按 fillColor 识别容器，**容器排最底层、节点排上层、连线最后**
  - 验证：drawio 导入后 13 节点全显示（接入层3+业务层4+数据层3+客户端等）

### 边界
- .vsdx 主要用途 Visio/drawio 线下编辑/存档
- 导回 ProcessOn 往返会丢样式（ProcessOn 导入器简化形状/折叠组，非导出 bug）

## 三点五、jpg 高清导出（v2 新增）

### 能力
- `processon_export_jpg(chart_id, out_path, export_type="jpghd")`
  - step1：调 `/api/personal/chart/export/get/user/power?chartId=..&exportType=jpghd`
    触发服务端导出，返回 task id（**已验证可用**）
  - step2：轮询拿 KS3 CDN 下载 URL
  - step3：流式下载到本地
- 实测：浏览器手动导出得 135KB jpg（arch_route_test）

### 当前状态（诚实记录）
- power 触发接口已跑通（返回 task id 如 `v2_f96cee4c...`）
- **轮询下载 URL 接口未完全摸清**：试了 result/poll/status/info 四个路径全 404
- 当前方法 60 秒轮询失败后报错提示"用浏览器导出"
- 后续：需在浏览器 DevTools 抓"文件→导出→JPG"完整请求链，补进 poll_paths

### 边界
- export_type: jpghd（高清 VIP）/ jpg（普通）
- 与 vsdx 导出互补：jpg 是位图预览，vsdx 是矢量可编辑

## 四、文件与归档

- 代码：src/processon_mcp/processon_client.py（_separate_rects、make_container、两阶段布局、THEMES.container_fill、get_chart_def、export_chart_to_vsdx）
- 新增：src/processon_mcp/vsdx_exporter.py（纯 Python OPC 生成器）
- 本档：pon_mcp_v2.md（本地唯一源）；幕布同步同名文档；ProcessOn v1 归档图追加分支
