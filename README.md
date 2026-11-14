# ROOT 工具工作台

凭邀请码注册并登录，输入自然语言需求，DeepSeek 在工作台里生成工具或网站原型。登录同一账号可以从「我的工具」重新打开已保存的结果和数据。

## 快速入口

| 要做什么 | 从这里开始 |
| --- | --- |
| 在本机体验工具生成与续改 | [工作台启动与邀请码](#本机启动) |
| 看需求、Bug、接手人和验收进度 | [开发看板](http://127.0.0.1:17437/dev-board) · [启动说明](#开发看板与-wiki) |
| 查历史缺陷、回归记录和技术决策 | [开发 Wiki](http://127.0.0.1:17437/) · [查阅与更新规则](质量门禁设计.md#开发-wiki) |
| 查需求范围、设计稿和验收依据 | [实施计划](实施计划.md) · [设计稿](DESIGN.md) · [测试证据索引](测试证据索引.md) |
| 办理提测、接测、验收或部署 | [开发流程图与分工](docs/开发与验收流程.md) · [本地 CI 操作](docs/开发与验收流程.md#本地-ci-操作) · [部署说明](deploy/whole/README.md) |

看板和 Wiki 都是本机服务。打开链接前，先按下方说明启动。

## Demo 生成范围

「直接开发」支持按需求自动选择类别，也可以手动选择清单、台账或看板、计算工具、单页落地页和多页品牌官网。品牌官网的页面导航在工作台的隔离预览内运行，保存后可重新打开。生成失败的需求可从「生成历史」带回编辑，再提交时启动新任务。

后续计划包括自定义组件、复杂交互、数据库、外部服务和公开发布。范围和验收条件见[生成能力路线图](实施计划.md#生成能力路线图)。

从自然语言新建台账或看板时，DeepSeek 根据已确认的字段生成 HTML、CSS 和 JavaScript。工作台在隔离环境中检查并运行生成结果，由服务端按账号和工具保存记录。

新建的台账和看板可以通过「继续修改」调整页面 CSS。先查看改动，再确认应用到同一个工具，已有记录会保留。首发使用新运行目录，从当前版本新建工具开始体验。

## 本机启动

在项目根目录用 Python 3.9 或更新版本启动，无需安装 Python 依赖。

```sh
python3 -B workbench.py --port 8773 --runs-dir runs/accounts-local
```

打开终端显示的 [本机工作台](http://127.0.0.1:8773)。启动行显示构建标识。首次体验时，由本机操作员从同一目录生成邀请码并交给评审，默认 72 小时内可注册一次。

```sh
python3 -B workbench.py --runs-dir runs/accounts-local --create-invite
```

工作台使用该运行目录下的 SQLite 和生成产物。更换浏览器后登录同一账号即可恢复。第 4 版匿名浏览器数据与账号工作台独立保存。

预览需要执行 `Connection-Allowlist` 出站限制的浏览器，已在 Chrome 156 验证。工作台启动时会检查限制是否生效，检查失败时停用生成和已有工具预览。

分阶段候选发布还需要服务端的 Node.js 和支持出站限制的 Chrome 或 Chromium。服务使用临时浏览器配置独立运行候选。可用 `ROOT_VERIFY_NODE` 和 `ROOT_VERIFY_CHROME` 指定程序路径。Linux 以非 root 服务用户运行，并启用 Chromium 沙箱。无法运行检查时，发布会等待可用的验证环境。

### 配置 DeepSeek

服务默认读取 `DEEPSEEK_API_KEY` 环境变量。已经配置时直接运行启动命令。没有 Key 时可查看工作台并恢复已保存的工具。

**在当前终端配置一次（zsh）**

```zsh
read -rs 'DEEPSEEK_API_KEY?DeepSeek API Key: '
printf '\n'
export DEEPSEEK_API_KEY
python3 -B workbench.py --port 8773 --runs-dir runs/accounts-local
```

输入不回显。同一个终端中重启可以继续使用该环境变量。只想临时启动一次时可用 `python3 workbench.py --prompt-key`。

**使用 macOS 钥匙串跨终端启动**

钥匙串条目使用项目名称 `ROOT Demo DeepSeek` 和账户 `ROOT`。首次配置可以在「钥匙串访问」中新增该密码项目。条目已存在时，直接从项目根目录运行下列命令。macOS 可能要求授权读取。

```zsh
DEEPSEEK_API_KEY="$(security find-generic-password -s 'ROOT Demo DeepSeek' -a ROOT -w)" python3 -B workbench.py --port 8773 --runs-dir runs/accounts-local
```

Key 只在本机服务端用于调用 DeepSeek。工具需求会发送给模型，生成记录和源码保存在本机运行目录。工具操作数据经工作台宿主写入所属账号的 SQLite。

## 本地 CI

需求就绪、开工、提测、QA 接测、产品验收和部署按[流程图与角色分工](docs/开发与验收流程.md)办理。各阶段的命令、触发时机和所需证据见[本地 CI 操作](docs/开发与验收流程.md#本地-ci-操作)。用以下命令核对一个事项的正式进度。

```sh
python3 -B tests/local-ci.py status <任务索引.json>
```

## 开发看板与 Wiki

本机已经配置 `dev-wiki` 和 ROOT Wiki 内容时，从仓库根目录运行以下命令。

```sh
python3 -B docs/dev-board.py
```

打开[开发看板](http://127.0.0.1:17437/dev-board)、[Wiki 首页](http://127.0.0.1:17437/)或[Wiki 版本记录](http://127.0.0.1:17437/wiki-versions)。只启动看板时，运行 `python3 -B docs/dev-board.py --board-only`。

看板每次刷新用本地 CI 核对阶段和回执。事项登记、换版和状态索引见[开发流程](docs/开发与验收流程.md#任务状态索引)。看板依赖本机的任务索引和回执，只有源码的检出会显示相应记录缺失。Wiki 内容与原始测试证据随本机 `runs/` 保留；已有试点的命令和配置见该目录下的 `llm-wiki/README.md`。

## 开发自测

```sh
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py'
node tests/verify-workbench.mjs
node tests/verify-webrtc-isolation.mjs runs/webrtc-check
node tests/verify-webrtc-isolation.mjs runs/webrtc-nested-check --nested
node tests/verify-webrtc-isolation.mjs runs/webrtc-no-policy-check --without-allowlist
```

Chrome 回归需要 Node.js 22+ 和本机 Chrome，可用 `CHROME_BIN` 指定浏览器路径。默认用模型替身、临时目录和全新 Chrome 配置。

在服务端配置好 Key 后，从项目目录运行真实模型自测。它会用受控邀请注册一个本地测试账号并发起两次真实生成请求。只跑第一条可加 `--main`。

```sh
ROOT_TEST_INVITE="$(python3 -B workbench.py --runs-dir runs/accounts-local --create-invite)" \
  WORKBENCH_URL=http://127.0.0.1:8773 WORKBENCH_RUNS_DIR=runs/accounts-local \
  node tests/verify-workbench.mjs --live
```

验收范围和证据见[实施计划](实施计划.md)。技术架构、账号边界和 AC-007 的预留设计见[账号与工具设计](账号与工具设计.md)。

验收用例、关键断言、结果与截图入口见[测试证据索引](测试证据索引.md)。
