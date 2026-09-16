# processon-mcp 连接器配置指南（豆包客户端）

> 目的：把本地 `processon-mcp` MCP server 注册为豆包客户端的「自定义连接器」，
> 让你的 ProcessOn 永久账户直接在豆包对话里画图（流程图 / 架构图 / 思维导图 / UML 等），
> 把 ProcessOn 当作创作与思路推导的图形引擎。
>
> 项目地址：https://github.com/qingyuanLee/mcp_processon

## 一、前置：准备 ProcessOn API Token

1. 打开 https://smart.processon.com/user （用你的 ProcessOn 永久账户登录）。
2. 在「API 访问令牌」页面点 **创建令牌**，复制形如 `sk-po-xxxxxxxx` 的令牌。
3. 把令牌写入项目根目录的 `.env`（已被 `.gitignore` 忽略，不会上传）：

   ```
   PROCESSON_API_KEY=sk-po-你的令牌
   ```

   或：连接器启动时不读 `.env`，所以更稳妥的做法是在下面的「环境变量」里直接填。

## 二、注册自定义连接器

1. 打开豆包桌面客户端并**登录**你的账号。
2. 点击左侧边栏 **「技能 · 连接器 · 伙伴」**（未登录时入口不显示）。
3. 进入「连接器」页面，点右上角 **「+ 新建」→「新建自定义连接器」**。
4. 按下表填写：

| 字段 | 值 |
|------|-----|
| 服务器名称 | `processon-mcp` |
| 传输类型 | **STDIO**（本地进程，标准输入输出通信） |
| 命令 | `C:\Users\18133\work\mcp_processon\.venv\Scripts\processon-mcp.exe` |
| 参数 | （无，留空） |

5. 添加**环境变量**（连接器启动进程时不会自动读项目 `.env`，必须显式填写）：

| 环境变量 | 值 |
|----------|-----|
| `PROCESSON_API_KEY` | `sk-po-你的令牌` |

6. 点**保存**。若首次提示「连接器运行失败」，到「我的技能 → 连接器」列表里找到
   `processon-mcp`，点 **「重新启动」** 即可。
7. 确认：连接器出现在「连接器」列表「个人」分类下，开关为蓝色（已启用）。

## 三、验证

在豆包对话里直接说：

- 「画一个用户登录注册流程图，包含前端校验、后端鉴权、数据库查询、发 Token」
- 「把这段 Markdown 生成一张思维导图」
- 「画一个微服务架构图：网关、用户服务、订单服务、MySQL、Redis」
- 「画一个电商下单的时序图」

AI 会自动调用 `processon_*` 工具，返回**预览图**和**在线编辑链接**（可直接点开二次编辑）。

## 四、提供的工具

| 工具 | 作用 |
|------|------|
| `processon_whoami` | 查看 ProcessOn 登录状态 |
| `processon_generate_chart` | 自然语言 → 可编辑在线图表（核心） |
| `processon_md_to_mindmap` | Markdown → 可编辑思维导图 |
| `processon_cache_info` / `processon_cache_clear` | 缓存查看 / 清理 |

## 五、常见问题

- **认证失败 / 401 / 403**：Token 填错或过期。回 https://smart.processon.com/user
  重新复制令牌，更新连接器环境变量后重启。
- **保存后连接器不可用**：确认本机环境正常。自检命令（PowerShell）：
  ```
  C:\Users\18133\work\mcp_processon\.venv\Scripts\processon-mcp.exe --help
  ```
  能打印帮助说明环境完好。
- **生成超时**：复杂图表生成较慢，服务端等待上限 180 秒；可把 prompt 拆小一点。
- **仅限本地电脑**：自定义连接器依赖本机进程，浏览器版 / 移动端 / 云电脑不可用。

## 备选：HTTP 方式

如果 STDIO 异常，可让服务常驻后用 HTTP 接入：

```powershell
cd C:\Users\18133\work\mcp_processon
Start-Process -WindowStyle Hidden -FilePath ".\.venv\Scripts\processon-mcp.exe" -ArgumentList "--transport","streamable-http","--port","3100"
```

然后连接器传输类型选 **HTTP**，URL 填 `http://localhost:3100/mcp`。
（STDIO 由客户端管理进程生命周期，推荐优先用。）
