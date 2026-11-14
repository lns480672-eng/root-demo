# ROOT 工作台接口

[openapi.json](openapi.json) 使用 OpenAPI 3.1.1。可将本地文件导入支持 OpenAPI 3.1 的 Swagger 编辑器或 API 客户端，查看路径、请求和响应结构、会话要求及错误码。[OpenAPI 3.1.1 规范](https://spec.openapis.org/oas/v3.1.1.html)

## 本机联调

1. 使用工作台实际启动的来源，例如 `http://127.0.0.1:8765`。该文档中的服务器地址是示例，端口以启动参数为准。
2. 登录后保留服务端设置的 `root_session` Cookie。向 `GET /api/session` 读取当前会话的 `marker`，之后把它作为 `X-Root-Session` 请求头，连同 Cookie 发送。会话变更时重新读取 marker。
3. 所有 POST 都使用 `Content-Type: application/json` 与工作台同源的 `Origin`。浏览器自动设置 Origin，命令行联调需要自行传入。带身份的请求应在同源环境或本机运行，不把 Cookie、marker、邀请码或模型凭据放入共享请求示例。
4. 默认新工具生成向 `POST /api/generate` 提交需求和稳定的 `requestId`。修订则向 `POST /api/tools/{toolId}/revise` 另外提交当前版本。收到 202 后跟踪任务状态与阶段。完成后取绑定候选。用计划提交的完整快照调用 `POST /api/staged/{taskId}/verify-preview`，服务端运行检查通过后，用同一载荷调用 `publish`。修订先向用户展示 `revisionPlan`。用户确认后先调用 `confirm-revision` 登记，再附上候选的 `runId`、`revision` 和 `revisionPlan.confirmation`。取消时用候选 nonce 调用 `cancel-revision`。数据发生冲突时重新获取方案并请用户确认。发布回执丢失时用原 nonce 调用 `resolve-publish`，已发布时返回工具，未发布时撤销原凭据并重新取得候选。旧单轮路径供本地对照使用。

管理员发码使用 `POST /api/admin/invites`，发送 UUID v4 请求编号。明文邀请码只在 201 响应中返回一次。重放同一请求编号会返回 `invite_already_issued`，也不会创建第二枚码。

## 契约核对

JSON 语法可用 `python3 -m json.tool api/openapi.json` 检查。规范通过独立的 `openapi-spec-validator` 0.7.2 校验。另从固定提交 `4b75ed38cb24f7dec0af6fe8370dc48ba0299183` 解包启动隔离工作台，用临时 SQLite、合成账号和模型替身对默认配置的 32 个操作校验真实 HTTP 状态与 JSON 结构。两项旧修订兼容操作单列。核对了草稿修订、分析重复请求、表格预填、管理员失败率、发码重放及用后失效、统一生成两类计划、续跑、越权和发布幂等。修订发布验证了并发写入导致的冲突，以及重新读取最新快照后保留数据。

维护时同步调整响应 schema，并从新的固定提交复跑实际请求。

## 工具版本与 Git 提交

`GET /api/tools/{toolId}/versions` 在每条已生效版本返回完整的 Git `commit`。工作台为每个工具绑定一个私有仓库，分阶段任务的过程提交不会出现在工具版本列表。直接导表在首次保存时记录代码快照。恢复会用旧版代码在同一仓库追加一个提交，并继续保留 SQLite 中的当前业务记录。

## 修改页只读代码

正式版使用 `POST /api/tools/{toolId}/source`，携带当前 `version`。已就绪的修改候选使用 `POST /api/staged/{taskId}/source`，携带候选响应中的 `toolId`、`runId`、`version`、`commit`、`sha256` 和 `nonce`。两个入口都验证工具所属账号。候选已经取消、失败或应用时，需重新读取当前已应用版本。

响应返回 `toolId`、`version`、`applied` 和按项目相对路径排列的 `files`，每项含 `path` 和 `text`。没有保留生成制品的旧工具返回空列表。候选和正式版的代码均从对应的生成制品校验后读取。浏览器把文件按文本显示。
