# ROOT 工具工作台

凭邀请码注册并登录，输入自然语言需求，DeepSeek 在工作台里生成工具或网站原型。登录同一账号可以从「我的工具」重新打开已保存的结果和数据。

## Demo 生成范围

「直接开发」支持按需求自动选择类别，也可以手动选择清单、台账或看板、计算工具、单页落地页和多页品牌官网。品牌官网的页面导航在工作台的隔离预览内运行，保存后可重新打开。生成失败的需求可从「生成历史」带回编辑，再提交时启动新任务。

后续计划包括自定义组件、复杂交互、数据库、外部服务和公开发布。范围和验收条件见[生成能力路线图](实施计划.md#生成能力路线图)。

结构化台账的「继续修改」支持只改标题、按指定字段新增筛选、明确移除 CSV 导出和已支持的整体配色。其余现有功能及记录继续保留。

## 本机启动

在项目根目录用 Python 3.9 或更新版本启动，无需安装 Python 依赖。

```sh
python3 -B workbench.py --port 8773 --runs-dir runs/accounts-local
```

打开终端显示的 [本机工作台](http://127.0.0.1:8773)。启动行显示构建标识。首次体验时，由本机操作员从同一目录生成邀请码并交给评审，默认 24 小时内可注册一次。

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
