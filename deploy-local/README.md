# 本机 HTTPS 验收

适用 AC-003 的本机预检和 AC-014 官网入口。官网与工作台在同一 HTTPS 端口使用不同主机名，分别为 `localhost` 和 `127.0.0.1`。浏览器只向工作台发送主机专用 Cookie。Caddy 仅监听 IPv4 回环，官网 HTTP 上游监听 IPv6 回环，工作台 HTTP 上游监听 IPv4 回环。

代理先验证完整 Host 和写请求的唯一 Origin，再把通过的请求转给原有本机工作台。工作台自己的会话、数据和权限检查继续生效。代理为会话 Cookie 添加 Secure，并把工作台的出站名单转换为验收用 HTTPS 来源，保留同源路径和 WebRTC 禁止规则。官网转发去除 Cookie。

## 受控浏览器回归

使用 Caddy 2.11.4、OpenSSL、Python 和 Chrome。在源码根目录运行。

```sh
node tests/verify-site.mjs
ROOT_TEST_CADDY=/path/to/caddy node tests/verify-site.mjs --https
```

HTTPS 用例创建隔离目录、专用模型替身、一次性邀请码及临时证书。Chrome 仅对该证书的公钥启用局部信任。核对 Host、Origin、Cookie、表格上传和保存边界后，从官网进入并注册，在工作台生成工具、操作并整页刷新。运行结束即关闭临时服务。

## 从固定候选启动

在固定源码目录打开三个终端。下面以 HTTPS 端口 8801、工作台上游 8803 和官网上游 8804 为例，需使用同一组空闲端口。已有体验服务使用独立端口和数据目录。

第一个终端创建专用数据目录及单次邀请码，使用本机已配置的模型 Key 启动工作台。

```sh
umask 077
state_dir="$(mktemp -d "${TMPDIR:-/tmp}/root-local-acceptance.XXXXXX")"
printf '%s\n' "$state_dir"
python3 -B workbench.py --runs-dir "$state_dir" --create-invite --invite-hours 24 --invite-uses 1 > "$state_dir/qa-invite"
python3 -B workbench.py --runs-dir "$state_dir" --port 8803
```

第二个终端启动官网并指向同一固定候选的 HTTPS 工作台。

```sh
python3 -B public_site.py --port 8804 --workbench-url https://127.0.0.1:8801/
```

第三个终端将 `state_dir` 设为第一个终端创建的目录，生成临时证书后启动本机 Caddy。

```sh
umask 077
state_dir=/path/to/private/root-local-acceptance
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -subj '/CN=localhost' \
  -addext 'subjectAltName=DNS:localhost,IP:127.0.0.1' \
  -keyout "$state_dir/localhost.key" -out "$state_dir/localhost.pem"
ROOT_LOCAL_PORT=8801 ROOT_LOCAL_WORKBENCH_PORT=8803 ROOT_LOCAL_SITE_PORT=8804 \
  ROOT_LOCAL_CERT="$state_dir/localhost.pem" ROOT_LOCAL_CERT_KEY="$state_dir/localhost.key" \
  XDG_DATA_HOME="$state_dir/caddy-data" XDG_CONFIG_HOME="$state_dir/caddy-config" \
  caddy run --config deploy-local/Caddyfile --adapter caddyfile
```

本机入口为 [ROOT 官网](https://localhost:8801/)。手动验收可按下列方式启动一次性 Chrome 配置，仅接受该本机证书的公钥。邀请码保留在专用目录中，由验收者按需读取。

```sh
spki="$(openssl x509 -in "$state_dir/localhost.pem" -pubkey -noout | \
  openssl pkey -pubin -outform DER | openssl dgst -sha256 -binary | openssl base64)"
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --user-data-dir="$state_dir/chrome" --host-resolver-rules='MAP localhost 127.0.0.1' \
  --ignore-certificate-errors-spki-list="$spki" https://localhost:8801/
```

代理表格识别请求上限默认为 6,356,992 字节，等于默认原始表格大小的六倍加 65,536 字节。修改工作台 `ROOT_TABLE_MAX_BYTES` 时，将 Caddy 的 `ROOT_LOCAL_INSPECT_LIMIT` 同步设为这个公式的结果。代理在表格识别路径使用该值。表格生成及工具数据请求在 16 MiB 内。分阶段发布请求上限为 512 KiB，其余请求按 16,000 字节限制。

本机证书、监听、邀请码和数据隔离用于开发及个人验收。正式 AC-003 还需用户授权拟部署版本，按拟部署环境核对代理信任、认证限流、日志留存、访问期限、额度和回退，然后从新的浏览器完成线上 G5。公开仓库、评审开放和笔试提交分别按已有授权执行。

[Caddy 回环绑定](https://caddyserver.com/docs/caddyfile/directives/bind) · [请求大小](https://caddyserver.com/docs/caddyfile/directives/request_body) · [反向代理与响应头](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)
