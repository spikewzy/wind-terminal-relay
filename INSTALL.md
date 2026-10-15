# 独立中继安装与接入

推荐 Python 3.12。当前锁定依赖中的 `rpds-py` 要求 Python 3.11+，不能把此依赖锁用于 Python 3.10。各平台分别创建自己的虚拟环境，不复制另一台电脑的 `.venv`。

客户端只需要 Python、中继源码和属于该设备的客户端配置；不需要 Wind 金融终端，也不要运行原本地 MCP 的 `install.py` 或 `verify_delivery.py`。

## 一、Mac / Linux 客户端

先确认 `python3 --version` 为 3.11 或更新版本，然后在中继解压目录执行：

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock.txt
.venv/bin/python -m pip check
```

把管理员提供的本设备凭据保存为 `.relay/client-me.json`，不要展示或上传文件内容。目录和文件只允许本用户读取：

```sh
chmod 700 .relay
chmod 600 .relay/client-me.json
```

在这台电脑本地生成配置：

```sh
.venv/bin/python relay_setup.py client-config --config .relay/client-me.json --format codex
.venv/bin/python relay_setup.py client-config --config .relay/client-me.json --format workbuddy
```

选择对应客户端格式即可，不需要同时使用两个。输出只包含 Python、代理脚本与私有配置的绝对路径，不包含 token。

## 二、Windows 客户端

在 PowerShell 中进入中继目录。使用 Python 3.12 建立虚拟环境；已安装其他受支持版本时可改为其 Python 绝对路径：

```powershell
py -3.12 -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.lock.txt
& ".\.venv\Scripts\python.exe" -m pip check
```

如果没有 `py`，使用实际安装的 Python，例如：

```powershell
& "C:\Python312\python.exe" -m venv .venv
```

`C:\Python312` 只是示例，必须替换为本机实际路径。无需修改 PowerShell 执行策略或激活虚拟环境，直接调用 `.venv\Scripts\python.exe` 即可。

将本设备配置保存为 `.relay\client-me.json`，通过文件夹“属性 → 安全”限制本地普通用户访问；Linux 的 `chmod 600` 不等同于 Windows ACL。不要放进多人共享目录。

```powershell
& ".\.venv\Scripts\python.exe" relay_setup.py client-config --config ".\.relay\client-me.json" --format codex
& ".\.venv\Scripts\python.exe" relay_setup.py client-config --config ".\.relay\client-me.json" --format workbuddy
```

依赖锁里的 `pywin32` 是官方 MCP SDK 的 Windows 依赖，不是 WindPy，也不会安装 Wind 金融终端。云端纯网关可以使用更小的 `requirements-gateway.lock.txt`，其中无需 MCP SDK。

## 三、加入 Codex / WorkBuddy

将生成的 `wind_terminal_relay` 条目合并到现有 MCP 设置，保留其他条目，然后重载该连接。不要用中继设置覆盖本地 `wind_terminal_api` 设置。路径包含空格或中文时保留生成器给出的完整引号和转义；不能把 Mac 上生成的 Python 路径直接发给 Windows 用户。

Codex 输出为 TOML，WorkBuddy 输出为 JSON。配置生成器不会修改客户端设置；Codex 的路径与格式依据 [官方 MCP 文档](https://developers.openai.com/codex/mcp)。

第一次接入只做通信核查：

```text
只使用 wind_terminal_relay。先调用 relay_status，确认执行端在线且 worker_mode=native。
先不要取数，报告中继状态。如果提交任务后返回 result_ready=false，只查询相同 job_id，不能重新提交。
```

`worker_online=true` 表示近期中继通信正常，不证明 Wind 已登录；需要取数时再进行一次范围很小的明确查询，核对实际结果和原始回执。

日常用量可调用 `relay_usage` 查看本设备的聚合统计。管理员凭据与客户端凭据分开保存；管理员工具及队列限制的口径见 [DESIGN.md](docs/DESIGN.md)。迁移已有部署时先读 [MIGRATION.md](docs/MIGRATION.md)，不要重新初始化现有凭据。

## 四、云端网关

仅新部署时，在受控电脑一次性生成网关、执行端和每台设备的凭据。例如：

```sh
python relay_setup.py init --output .relay --url https://relay.example.com --clients owner-mac colleague-windows
```

地址必须替换为已有 HTTPS 入口。初始化目录必须为空；不要对现有生产配置重新初始化。生成器不打印 token：网关配置只留在云端，worker 配置只交给执行电脑，每个 `client-*.json` 只交给对应设备。

管理员配置 `admin.json` 单独保存，不能当作普通客户端配置分发。

云端无需安装 Wind，可以使用以下依赖锁：

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements-gateway.lock.txt
.venv/bin/python relay_gateway.py --config .relay/gateway.json
```

Windows 将 Python 命令换为 `& ".\.venv\Scripts\python.exe"`。网关默认仅监听 `127.0.0.1:8765`，通过已经配置的 HTTPS 反向代理或加密组网入口访问。远程桌面 `3389` 端口不是中继 API。不要用公网明文 HTTP 传递凭据。

### 现有网关：管理员与设备管理

先备份现有私有配置。以下命令修改云端的 `gateway.json`，应在受控云端环境执行，不上传文件内容。

旧网关没有管理员时新增只读管理员凭据，不重置现有客户端或工作端：

```sh
.venv/bin/python relay_setup.py issue-admin --gateway .relay/gateway.json \
  --url https://relay.example.com --output .relay/admin.json
```

新增另一台设备，各设备使用不同 `client_id`：

```sh
.venv/bin/python relay_setup.py add-client --gateway .relay/gateway.json \
  --client-id colleague-windows --url https://relay.example.com \
  --output .relay/client-colleague-windows.json
```

只分发新生成的这一份客户端文件。撤销某设备：

```sh
.venv/bin/python relay_setup.py revoke-client --gateway .relay/gateway.json \
  --client-id colleague-windows
```

上述凭据变更写入配置后，需在当前任务空闲时重启网关才能生效，不自动中断生产进程。管理员默认查询滚动 UTC 7×24 小时统计：

```sh
.venv/bin/python relay_admin.py --config .relay/admin.json --days 7
```

`--days` 支持 1 到 31 天；设为 1 可查看最近 24 小时。结果只包含汇总，不包含 token、其他客户端的查询参数、原始值或回执。旧结果缺少新增计量时保持未知，不补造历史用量。调用预留观测按实际观测时刻筛选，不用任务入队时间代替；官方上限与剩余额度未知，详见 [额度规则](docs/QUOTA.md)。

## 五、独立目录中的实体执行端

执行端仍需要已有匹配版本的 `wind-terminal-mcp` 核心以及可用的官方 WindPy。它只导入核心工具实现，不启动第二个原 MCP stdio 服务。

中继源码可以放在另一个目录；`worker.json` 必须明确指向已有核心与共享运行目录，例如：

```json
{
  "url": "https://relay.example.com",
  "token": "REPLACE_WITH_THE_PRIVATE_WORKER_TOKEN",
  "data_directory": "worker-state",
  "mcp_root": "/absolute/path/to/wind-terminal-mcp",
  "shared_profile": "/absolute/path/to/wind-terminal-mcp/runtime",
  "allow_private_http": false
}
```

使用**原核心已能运行 WindPy 的虚拟环境**启动新的中继脚本，保留它原有的 SDK 环境变量：

```sh
/absolute/path/to/wind-terminal-mcp/.venv/bin/python \
  /absolute/path/to/wind-terminal-relay/relay_worker.py \
  --config /private/path/to/worker.json --mode native
```

文件里的相对 `data_directory` 按配置文件所在目录解析。`shared_profile` 必须与原 MCP 的实际数据目录相同，才能共用原生调用锁与预算；各客户端回执和缓存仍分开保存。

原 `server.py` 必须与 `references/relay-tool-contract.json` 中的 SHA256 匹配。若出现 `CONTRACT_MISMATCH`，先核对两个项目支持的版本，不删除校验、不覆盖原核心。已有执行端只能在确认当前任务和待回传结果处理完之后迁移，不能同时运行两个 native 执行端。

`--mode mock` 可在有核心的执行环境中使用合成数据离线验证；默认模式也是 mock。普通客户端和纯网关不需要安装该核心。

## 六、离线检查与发布

```sh
python -m unittest discover -s tests -v
python build_release.py
```

没有原核心的环境只能验证客户端、网关和队列；核心适配专项测试应有明确跳过说明，不能把它计为实机成功。GitHub 工作流执行 macOS、Windows、Linux 的 Python 3.12 离线检查，结果与实机验收边界见 [验收记录](docs/VERIFICATION.md)。

发布包 `dist/wind-terminal-relay-0.1.0.zip` 只包含 allowlist 源码、文档、模板和测试，附逐文件哈希与包哈希。不要把 `.relay`、token、数据库、Wind SDK 或其他用户回执添加到包里。

