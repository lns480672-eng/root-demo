# ROOT 工作台接口

[openapi.json](openapi.json) 使用 OpenAPI 3.1.1。可将本地文件导入支持 OpenAPI 3.1 的 Swagger 编辑器或 API 客户端，查看路径、请求和响应结构、会话要求及错误码。[OpenAPI 3.1.1 规范](https://spec.openapis.org/oas/v3.1.1.html)

## 本机联调

1. 使用工作台实际启动的来源，例如 `http://127.0.0.1:8765`。该文档中的服务器地址是示例，端口以启动参数为准。
2. 登录后保留服务端设置的 `root_session` Cookie。向 `GET /api/session` 读取当前会话的 `marker`，之后把它作为 `X-Root-Session` 请求头，连同 Cookie 发送。会话变更时重新读取 marker。
3. 所有 POST 都使用 `Content-Type: application/json` 与工作台同源的 `Origin`。浏览器自动设置 Origin，命令行联调需要自行传入。带身份的请求应在同源环境或本机运行，不把 Cookie、marker、邀请码或模型凭据放入共享请求示例。
4. 默认新工具生成向 `POST /api/generate` 提交需求和稳定的 `requestId`。修订则向 `POST /api/tools/{toolId}/revise` 另外提交当前版本，两者均返回同一类生成任务。收到 202 后跟踪任务状态与阶段。失败后用同一任务续跑。完成后取得绑定候选，工作台通过隔离预览再提交发布。发布回执丢失时先查询同一任务，已发布就按返回的 `toolId` 重新打开。修订候选的 `id` 仍为原工具，发布时带回候选的 `runId` 和 `revision`。修订遇到最新数据冲突时，重新获取候选和数据后再预览。只用于本地测试对照的 `ROOT_STAGED_ENABLED=0` 仍可能返回传统 200 候选。

管理员发码使用 `POST /api/admin/invites`，发送 UUID v4 请求编号。明文邀请码只在 201 响应中返回一次。重放同一请求编号会返回 `invite_already_issued`，也不会创建第二枚码。

## 契约核对

首版接口形态按 `66304db72dd410e2446d113b9d26c95e3a850cfd` 固定候选核对。JSON 语法可用 `python3 -m json.tool api/openapi.json` 检查。规范通过独立的 `openapi-spec-validator` 0.7.2 校验。另从固定提交解包启动隔离工作台，用临时 SQLite、合成账号和模型替身对 33 个操作校验真实 HTTP 状态、JSON 请求与响应结构。核对了普通工具、草稿修订、分析重复请求、表格预填、管理员补额与失败率、统一生成的单实现块与多阶段任务、失败续跑、越权和发布重放。

新增发码契约按独立提交 `dd3e53d` 核对。统一修订按集成分支当前请求与响应编写，两项需在同一固定候选上联合复验。维护时同步调整响应 schema，并从新的固定提交复跑实际请求。
