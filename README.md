# ROOT 工具工作台

输入一段自然语言需求，DeepSeek 生成一个独立单页工具。生成结果在当前页的隔离预览中操作，刷新后恢复最近可用工具和该浏览器中保存的数据。

## 本机启动

在项目根目录用 Python 3.9 或更新版本启动，无需安装 Python 依赖。

```sh
python3 workbench.py
```

打开终端显示的 [本机工作台](http://127.0.0.1:8765)。启动行同时显示构建标识。保持同一个地址和端口，才能访问同一份浏览器数据。更换端口可用 `--port 8766`。

预览需要执行 `Connection-Allowlist` 出站限制的浏览器，已在 Chrome 156 验证。工作台启动时会检查限制是否生效，检查失败时停用生成和已有工具预览。

### 配置 DeepSeek

服务默认读取 `DEEPSEEK_API_KEY` 环境变量。已经配置时直接运行启动命令。没有 Key 时可查看工作台并恢复已保存的工具。

**在当前终端配置一次（zsh）**

```zsh
read -rs 'DEEPSEEK_API_KEY?DeepSeek API Key: '
printf '\n'
export DEEPSEEK_API_KEY
python3 workbench.py
```

输入不回显。同一个终端中重启可以继续使用该环境变量。只想临时启动一次时可用 `python3 workbench.py --prompt-key`。

**使用 macOS 钥匙串跨终端启动**

钥匙串条目使用项目名称 `ROOT Demo DeepSeek` 和账户 `ROOT`。首次配置可以在「钥匙串访问」中新增该密码项目。条目已存在时，直接从项目根目录运行下列命令。macOS 可能要求授权读取。

```zsh
DEEPSEEK_API_KEY="$(security find-generic-password -s 'ROOT Demo DeepSeek' -a ROOT -w)" python3 workbench.py --port 8771
```

Key 只在本机服务端用于调用 DeepSeek。工具需求会发送给模型，生成记录和源码保存在本机 `runs/`。预览工具的操作数据保存在当前浏览器。

## 开发自测

```sh
python3 -m unittest discover -s tests -p test_generation.py
node tests/verify-workbench.mjs
node tests/verify-webrtc-isolation.mjs runs/webrtc-check
node tests/verify-webrtc-isolation.mjs runs/webrtc-nested-check --nested
node tests/verify-webrtc-isolation.mjs runs/webrtc-no-policy-check --without-allowlist
```

Chrome 回归需要 Node.js 22+ 和本机 Chrome，可用 `CHROME_BIN` 指定浏览器路径。默认用模型替身、临时目录和全新 Chrome 配置。

在服务端配置好 Key 后，在项目目录的另一终端运行真实模型自测。命令默认访问 8765 端口，可用 `WORKBENCH_URL` 覆盖。先跑第一条可加 `--main`，完整模式提交两次真实生成请求。

```sh
node tests/verify-workbench.mjs --live
```

验收范围和证据见[实施计划](实施计划.md)。
