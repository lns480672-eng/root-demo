# AC-003 整版公网部署

每次上线按[发布分支与版本号](../../质量门禁设计.md#发布分支与版本号)核对工作台发行版本、发布分支、提交和构建标识。

历史部署示例：固定源码 f5d11b4251bc33757cdb772bca03b5022f88ba1c，工作台构建 v4-e22b861e0ab5。单独打包云端配置，不修改已验收源码。运行包只包含工作台、官网、标准库程序和此目录的部署配置。SHA-256 校验后以 root 所有、服务用户只读方式解包。

用户从官网首页进入工作台。Caddy 根据 Host 分流。临时官网使用 site.example.invalid，官网不保存或转发工作台会话。旧工作台保留 https://workbench.example.invalid/ 以维持原主机专用会话、账号和工具。网站在 IPv6 回环，工作台在 IPv4 回环，认证前置限流在另一个 IPv4 回环端口。官网、限流和有数据访问权限的工作台分别运行。新站点证书使用 Caddy ACME 自动签发。

工作台代理先校验 Host、POST Origin 和请求体，再改写为回环服务期望的 Host 与 Origin；浏览器 Cookie 加 Secure，保持 HttpOnly、SameSite=Strict、主机专用。预览的 Connection-Allowlist 转到同一 HTTPS 工作台来源，继续阻止 WebRTC。工作台登录、注册、后台登录、首次设置和后台改密共用认证限流，每 IP 每分钟 10 次，全站 80 次。额度默认个人生成成功保存 5 次，全站实际调用 30 次，分析个人每天 6 次、全站 60 次。失败只记实际模型调用。真实模型 Key 使用现有服务器配置，不进入运行包。

切换脚本先验证代理和 systemd，暂停旧生成，等待在途结束，停旧服务并备份，复制旧数据到新目录，在复制件中迁移个人成功、实际用量和已知结果。旧目录保留作为原始回退源。切换后本机以系统信任验证两个 HTTPS 入口及正确构建；失败自动恢复原代理和旧服务，保留失败现场。上线后的业务回退须先冻结新写入、备份新数据，再评估增量保留；不使用旧快照覆盖新数据。正式对外邀请从实际发出日起按确认的开放及留存周期管理。

AC-003 线上测试用专用邀请码及合成数据。分别核对官网、HTTPS、注册一次性、登录 Cookie 隔离、写来源、生成、操作及刷新恢复。授权管理员账号配置需关联实际指定账号。未登录和普通账号禁止管理。

AC-021 候选版本的首次交付需要已启用新版认证限流的 HTTPS 后台。新环境保持 `ROOT_TRIAL_ADMINS` 为空。用工作台服务账号运行 `python3 -B workbench.py --runs-dir /var/lib/root-demo-whole --create-admin-setup-code`。交付人员经私密渠道把一次性初始化码给负责人。负责人打开 `/admin` 自行输入初始化码并设置密码。凭据有效期为一小时，未完成设置时可重新签发，旧码随即失效。初始化完成后签发命令拒绝再次生成。现有环境保留原管理员配置。

切换验收期间，代理只允许本机访问待切换的工作台。检查通过后开放用户流量。失败时，切换脚本恢复旧服务和代理，移除本次安装的配置，将失败状态保留在备份目录，随后可以重试。在源码根目录运行 `python3 -B tests/verify_cutover.py deploy/whole/switch.sh`，可用临时目录和替身命令演练服务验证失败及 HTTPS 检查失败。

## 已运行整版的升级

从线上 `f5d11b4-41e65a7` 到 `386c053-f35f021` 使用 `upgrade.sh`。两版共用现有数据目录，先启用公网切换保护，再检查在途任务并备份状态。候选通过本机可信 HTTPS 和数据库完整性、内容指纹对照后放开公网。失败恢复原指针与代理，保留备份及现有数据库供排查。官网和登录限流进程继续服务。首装迁移脚本用于初始部署。

旅程版更新到 `a0f8fc7-journey` 时，工作台分阶段发布路径与表格写入共用 16 MiB 请求上限。升级后核对原表结构及数据指纹，允许三个新增业务表在启动时创建并为空。回退时沿用旧发布指针和原代理，保留新增空表便于重试。

## 运行校验候选的升级预检

本目录的 switch.sh 与 upgrade.sh 固定于旧发行版。当前候选不得直接使用这两份历史脚本。

- 保留工作台服务的非 root 用户、Chromium 沙箱、NoNewPrivileges 和 640 MiB 内存上限。目标机临时服务测试表明，96 的 TasksMax 会触顶并超时，192 可启动启用沙箱的 Chrome。部署时检查 Node.js、Chromium 和完整候选运行验证，检查通过后再切换。
- 代理将 /api/staged/*/verify-preview 与 /publish 放在同一个 16 MiB 请求体规则，代理响应超时 135 秒，工作台预览检查请求超时 20 秒。
- 新增 admin_sessions 表和 staged_jobs.category、staged_candidates.runtime_proof。旧版写入 staged_candidates 时用固定五列，不能直接复用迁移后的数据库。关闭公网写入并停服务后备份数据库和状态。切换期间若冒烟失败，用切换前备份恢复数据库及状态，再恢复旧指针、服务单元和代理。重新开放后要保留新写入，通过专门的数据兼容方案回退。
- 官网进程从 current 的工作目录读取源码。切换应用时同时重启官网服务并验证首页实际公告以及工作台链接。

### 0.8 Agent 升级验收

- AC-AGENT-DEPLOY-01。维护窗口内，使用停写后的完整状态目录创建独立副本。保留原目录用于回退。复制前后核对全目录内容和 SQLite 完整性，拒绝复制中的变化、活动 SQLite 日志和符号链接。
- AC-AGENT-DEPLOY-02。仅正式准备的副本能以生产 Agent 模式启动。校验副本位置和文件身份。用原目录、普通试用目录或拷贝标记直接启动均失败。
- AC-AGENT-DEPLOY-03。生产默认个人 5 次成功保存、共享 500 次调用、单任务 16 次、单次 120 秒，支持环境变量覆盖。部署前由运维在目标主机验证完整状态副本、依赖、真实模型和回退。试用额度沿用独立配置。

本地回归使用 `python3 -B -m unittest discover -s tests -p 'test_agent_production.py'`。

运维在维护模式下停旧服务，使用旧服务账号在生产主机创建候选副本。root 预建独立父目录 `/var/lib/root-demo-agent`，所有者为 `root-demo:root-demo`，模式为 0700。保持 `/var/lib` 及原状态目录权限配置。准备、切换及验证时使用相同的目标绝对路径。目标机须核对 systemd 启动后的实际目录权限和旧目录写入隔离。若恢复备份导致数据库文件身份改变，须按维护流程在新路径重新准备并核对状态。

```sh
python3 -B workbench.py --prepare-coding-agent-production-from /var/lib/root-demo-whole \
  --runs-dir /var/lib/root-demo-agent/v08
```

候选服务的 systemd 配置须允许写入该状态目录，并注入 `ROOT_PI_MODULES`。按 [Agent 额度技术规格](../../specs/coding-agent-trial/spec.md#生成调用与额度) 核对拟部署环境的有效配置。更新已有实例时，在维护窗口备份实际环境文件和相关 systemd 覆盖后，明确设置以下值。已有环境变量会覆盖代码默认值。

```ini
ROOT_AGENT_MAX_MODEL_CALLS=100
ROOT_ACCOUNT_GENERATIONS=1000
ROOT_TOTAL_GENERATIONS=10000
```

核对管理员／账号额度读回与已有用量。Agent 单次运行超时沿用环境配置，未设置时为 120 秒。回退时同步恢复备份的环境配置。

```ini
StateDirectory=
StateDirectory=root-demo-agent
StateDirectoryMode=0700
RestartPreventExitStatus=2
ExecStart=
ExecStart=/usr/bin/python3 -B workbench.py --port 8774 --runs-dir /var/lib/root-demo-agent/v08 --coding-agent-production
```

在维护模式下完成原目录全量指纹、候选准备、目标机真实 Agent 验证和回退演练。准备过程校验 SQLite 和本地裸 Git 仓库，刷盘后才写入生产标记。切换前记录原 systemd 配置、源码指针、状态目录和指纹。放开流量前如需回退，停止候选服务，恢复原 systemd 配置和源码指针，执行 `systemctl daemon-reload`，从原状态目录启动旧服务，再确认版本、状态指纹及冒烟后解除维护。

开放写入后遇到故障，先暂停写入并保留此时最新的完整状态。优先使用当前发布制品在该状态的隔离副本验证关闭 Agent 的路径，或准备修复版本。旧程序回退仅在同一最新副本上验证应用操作、已保存版本与记录全部兼容后执行。无法验证兼容时维持维护并前滚修复。
