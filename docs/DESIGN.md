# 本地 MCP 与云端中继的边界

本项目是独立的远程访问层。现有 `wind-terminal-mcp` 仍负责 WindPy 接口、指标发现、原始回执、本地查询预算和本地 MCP。新 `wind-terminal-relay` 负责跨设备接入、鉴权、队列、客户端隔离和用量汇总，两者独立发布。

| 项目 / 入口 | 工作内容 | 安装 Wind 的要求 |
|---|---|---|
| `wind-terminal-mcp` / `wind_terminal_api` | 调用本机 WindPy | 在该执行机器安装、登录 Wind，并具备相应权限 |
| `wind-terminal-relay` / `wind_terminal_relay` 客户端 | 在 Mac、Windows 或 Linux 上启动本地代理，发送 HTTPS 请求 | 客户端不需要 Wind SDK |
| relay 云端网关 | 鉴权、持久排队、返回结果、汇总用量 | 不需要 Wind SDK |
| relay 实体执行端 | 在原 WindPy 电脑使用外部 core 执行任务 | 复用已有 core、SDK、Wind 登录和共享数据目录 |

relay 是访问通道，native 数据来源仍为 `wind_terminal_api`；上游 `source`、频率、单位、日期及 null/零/空字符串不改写。Alice MCP (`alice_mcp`) 是另一个数据源，凭据、额度和缓存不能混用。

## 多设备并行

“多设备同时提交”与“多个 WindPy 请求同时执行”分开处理。客户端可以并发提交，网关以 SQLite 事务领取任务，实体执行端全局同一时间执行一个任务。若有执行结果不确定的任务，暂停领取新的任务，等待核查或原结果回传；不能通过重建 job_id 重复取数。

每个人的每台设备分配独立 `client_id` 和独立凭据，例如 `owner-mac`、`owner-win`、`colleague1-win`。独立凭据可分别撤销、统计、限制，某台设备泄露或离开时不需要替换全组凭据。当前调度公平单位是 client_id，即设备；一个人登记多台设备会获得多个调度席位，不宣称已实现按人公平。

调度按客户端轮流领取，客户端内部按提交顺序处理。一个客户端批量提交不会一直占用后续队列；正在执行的长任务不会被抢占，因此其他人仍需等待这一任务结束。默认网关每客户端最多 5 个未完成任务、全局最多 100 个排队任务、每客户端每分钟最多接收 30 个新任务。轮询已有 job、读取状态和同 job_id 幂等重送不占新任务次数。上述限制是服务防拥塞设置，不是 Wind 数据许可或官方账户额度。

现有 core 的 `shared_profile` 必须继续指向原 MCP 实际数据目录，使 relay 和直接本地 MCP 共用原生锁与保守查询预算。直接本地 MCP 不经过 relay 的公平队列，用量不能归到 relay 的某个客户端；只在 core 的共享预算记录中体现。

## 用量监控口径

监控采用滚动 UTC 时间窗口。普通客户端只查看自己的聚合，管理员凭据只读取全体聚合与各客户端用量，不借此读取别人的查询参数、原始值或回执。

以下数字必须分开：

- 接收的任务数：一个工具任务可能没有 Wind 调用，也可能展开多个调用。
- 排队、执行中、结果不确定、完成与失败：任务完成交付不等于数据查询成功。
- 原生调用预留次数：每次进入 core 的预算预留记录一次；失败、超时及抢锁失败也可能计入，不等于 SDK 实际执行次数。
- 已知的估算数据单元：按请求网格估算，可能包含非交易日，不是 Wind 官方扣量。
- 当前任务的缓存命中：不把历史回执内的调用记录重复计量。
- mock：合成测试用量，与 native 用量分开；缺少计量的旧结果及尚未回传的任务标记为未知。

Wind 官方剩余额度、账号外的人工操作/其他软件用量、SDK 实际执行总数，若没有权威观测就保持未知。管理员不能用“剩余本地预算”替代“Wind 官方剩余额度”。

`relay_status.execution_paused` 当前只表示网关有结果不确定的任务，不表示实体执行端不存在配额停机标记。若执行端因 `halt.json` 停止，网关可能显示离线但 `execution_paused=false`；暂停原因需结合执行端日志和标记核查。

全局 Wind 查询软上限继续通过原 core 共享目录的 `query-budget.json` 配置，支持时间窗口、原生调用预留总数、估算单元总数及单次估算单元上限。未明确账号额度前，本版本不自行编造默认账户限额；`null` 表示未限制。设备提交速率与该全局预算是两层不同的保护。

缓存保持按客户端隔离，不默认共享回执。未来如需公共数据缓存，可另外设计带新鲜度和权限检查的缓存层；本版本不自动合并不同客户端相同内容的请求。

## Mac / Windows

两个平台使用同一套 Python 客户端和 MCP 协议。平台差异集中在 Python 可执行路径、路径转义以及私有文件权限：Mac/Linux 的虚拟环境路径是 `.venv/bin/python`，Windows 是 `.venv\\Scripts\\python.exe`。使用配置生成器输出 Codex TOML 或 WorkBuddy JSON，然后只合并 `wind_terminal_relay` 条目。

这是 stdio 本地代理模式：由 Codex/WorkBuddy 启动客户端子进程，子进程再访问远程 HTTPS 网关。云端网关的 URL 不能直接作为 stdio 命令使用，也不能假设 `/v1/jobs` 是标准远程 MCP URL。

官方参考：[MCP stdio transport](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/main/docs/specification/2026-07-28/basic/transports/stdio.mdx)、[Python venv](https://docs.python.org/3.12/library/venv.html)。

## GitHub 与版本

建议现有公开仓库 `wind-terminal-mcp` 的主分支保持本地产品；新仓库使用 `wind-terminal-relay`，独立 README、安装步骤、发布包、测试和版本。现有 relay 试验分支与 PR 保留为迁移历史，未合并进本地产品主分支。本目录整理不代表已创建或发布新 GitHub 仓库。

relay 升级不直接升级 core。执行端配置 `mcp_root` 指向已有本地项目，读取随 relay 发布的工具契约并核对 `server.py` 哈希；core 升级后需要重新生成、验证契约，再发布兼容的 relay 版本，不能关闭校验强行兼容。

本地 MCP 与 relay 可以同时配置，但提示任务时明确选择本机或远程执行端。AI 不应在两条通道之间静默切换或因同一任务等待较久而重复提交。
