# 云电脑上的 Wind 中继监控

这是已有中继的独立附件。旧试验版与独立 relay 0.1.0 都可使用；不重装、不升级、不重启原网关，不导入 WindPy，也不发起行情查询。只需要云电脑已有的 Python，不需要安装新的依赖。

## 装到当前云电脑

1. 将压缩包里的 `monitor` 文件夹放进 `C:\WindRelay\app`。最终应有 `C:\WindRelay\app\monitor\Install.ps1`，不要多套一层文件夹。
2. 在**云电脑**上，以管理员身份打开 PowerShell，完整复制这一行运行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "C:\WindRelay\app\monitor\Install.ps1"
```

3. 浏览器打开 `http://127.0.0.1:8766/`，等待十几秒，看时间是否持续更新。以后双击云电脑桌面的 `WindRelay-Monitor` 即可。

安装器只增加 `WindRelayMonitor` 任务和桌面入口。它开机后在后台运行，不需要打开 PowerShell。原来的 `WindRelayGateway` 仍按原方式运行；关掉监控页不会关网关。监控器自己的端口为 8766，网关仍为 8765。

如果 PowerShell 提示已有监控任务，请去任务计划程序找 `WindRelayMonitor`，不要重复安装。如果使用过临时 `Start.cmd`，先在那个窗口按 Ctrl+C 停止临时监控，再安装后台任务；不用停网关。

默认从上一层目录读取 `.relay/gateway.json` 和 `.relay/client-owner.json`。这两个文件继续留在云电脑原处，不能上传到 GitHub、发群里或放进监控压缩包。不同目录、凭据文件名的部署，可手动运行并指定路径：

```powershell
& "C:\WindRelay\app\.venv\Scripts\python.exe" "C:\WindRelay\app\monitor\monitor.py" --config "C:\WindRelay\app\.relay\gateway.json" --client-config "C:\WindRelay\app\.relay\client-owner.json"
```

安装器默认使用 SYSTEM 运行独立监控任务，只访问本机文件和网络。若系统定制权限阻止读取这些文件，应由管理员检查任务结果和文件权限；不要把整个 `.relay` 目录开放给所有用户。安装完成仍需确认页面，不能把“任务已运行”当作实际健康检查。

## 页面怎么看

| 位置 | 说明 |
|---|---|
| 云端网关 | 用本机与现有公网地址分别请求 `/v1/status`，不创建 Wind 任务。公网检查从云电脑自身发出，不能证明每位使用者的网络都正常。 |
| 取数电脑 | 最近一次执行端心跳，30 秒内算在线；显示 native / mock。心跳在线不能证明 Wind 已登录。 |
| 任务队列 | 排队、执行、不确定任务。租约过期只在页面标记待核查，绝不改库或重做任务。 |
| 最近成功取数 | 最近保留的 `query_wind_data` 成功记录，要求 native、终端来源、非缓存与原始 ErrorCode=0。其他封装取数暂不计入此项，暂无记录不代表从未成功。 |
| 设备与任务 | 按凭据身份统计，任务编号可复制给 agent 排查。任务不是 API 调用；能力检查等也算任务。 |
| 用量观察 | 有新版本账本时读取最近 168 小时的 native 预留、估算格数和额度失败；旧版本显示未知。观测只覆盖此中继，不是账户总用量或官方账单。 |

最近任务只列 30 条；图表和设备表统计数据库仍保留的最近 24 小时任务，旧版一般会清理超过保留期的结果，因此不是完整历史报表。7 天预留按事件发生时间筛选，任务统计按入队时间筛选，二者口径不同。

页面不会显示任务参数、价格、返回行情正文或 token。它只绑定云电脑 `127.0.0.1`，请在远程桌面内打开；不要给 8766 配置 Funnel 或开放公网端口。管理员页面有跨设备任务元数据，本机同一电脑的用户能访问，不是多用户权限隔离门户。

## 关了怎么恢复

- 只是关浏览器：再双击桌面入口。
- 监控器断了：任务计划程序 → `WindRelayMonitor` → 运行。
- 网关断了：检查 `WindRelayGateway`；监控器不会替你重启它。
- 取数电脑离线：检查信创电脑、电源、网络和用户级中继服务。
- 不确定任务 / Wind 额度错误：核对原任务与执行端日志，不要删故障标记或重复提交。

不再使用时，到任务计划程序结束并删除 **WindRelayMonitor**，再删除桌面入口和 `monitor` 文件夹；不要删除 `WindRelayGateway` 或 `.relay`。

## 让 agent 帮你安装

> 请安装这个独立 Wind 中继监控器。将 monitor 文件夹放进现有网关安装目录，先检查 Python、gateway.json、client-owner.json 和 8766 端口；阅读 monitor/README.md，再运行 Install.ps1。保留现有网关、取数执行端和凭据，不启动第二份网关，不导入 WindPy，不取行情，不输出 token，不清理故障标记。完成后检查 WindRelayMonitor 任务和浏览器页面的状态更新时间；区分安装成功、监控器可用和取数线路状态，不把在线心跳当成真实取数验证。

## 其他系统与限制

Mac / Linux 管理机同样可以用 `python3 monitor/monitor.py --config /path/to/gateway.json --client-config /path/to/client-owner.json`，但必须有网关本地数据库；这是网关侧监控器，不是普通设备的接入客户端。自动安装脚本仅面向 Windows。

监控不写入数据库或调用其维护函数，不修改 halt / inflight，不重试或取消任务，不自动登录 Wind。没有持久化监控历史或外部推送通知。云电脑或监控进程关机后，页面只能提示刷新中断，不能继续在停机的机器上监控。取数电脑的本地 halt / inflight 文件不在云端，不能直接显示。
