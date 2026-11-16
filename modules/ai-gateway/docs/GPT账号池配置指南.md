# GPT账号池配置指南（0.7）

管理员配置一个或多个账号，平台调度可用账号，再为不同项目签发独立平台Key。客户项目填写Base URL、平台Key及模型别名即可接入；不需要取得账号池内部凭据。旧“ChatGPT账号/商务接入”模块已移除。

网页问答可使用平台托管历史，同类任务续聊、账号策略、图片和附件及客户端MCP回传见[网页会话与MCP接入](网页会话与MCP接入.md)。运营管理→会话与调度配置严格绑定或连续优先；使用现有客户端完整历史时保持原调用方式。平台会话保存在网关，不是ChatGPT官网的聊天窗口。

## 接入地址与公网使用

平台Key可以供外网系统使用，调用方必须能访问对应的网关Base URL。当前网关仅在虚拟机127.0.0.1:8094监听，公网HTTPS入口尚未配置；Windows的127.0.0.1:8094是该电脑自己的SSH转发地址，不能直接给其他电脑或服务器作为公共API地址。外部系统使用自己的localhost只会连接其本机。

正式对外服务需要可达的固定公网入口及HTTPS域名，再将该域名的/v1作为客户Base URL；例如https://ai.example.com/v1仅为示例，并非已部署地址。公网入口部署后，外部调用不再依赖Windows电脑的SSH窗口。当前域名、入口部署位置及公网连通性待落实，不能把本机真实模型调用通过描述为公网可用。

## Windows 经 Mac 宿主机连接

先在Mac的系统设置→通用→共享启用远程登录，允许自己的Mac账号。普通API与Codex调用只转发8094；在Windows PowerShell逐行执行，按第一行提示输入Mac实际局域网IP：

```powershell
$vmHostIp = Read-Host "Mac LAN IP"
ssh.exe -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -J "xiaomianyang@$vmHostIp" -L 127.0.0.1:8094:127.0.0.1:8094 nanshan@172.16.61.128
```

输入提示对应的Mac/Ubuntu登录密码，密码输入时不回显。保持此SSH终端开启；`-N`连接后不进入远程Shell、没有持续输出属于正常现象。30秒保活用于检测失联，关闭窗口或Mac/虚拟机休眠后仍需重新连接。若命令直接返回PowerShell提示符，查看其错误，不将其视为隧道已运行。

另开一个Windows PowerShell检查实际入口：

```powershell
curl.exe --noproxy "*" -sS --max-time 10 http://127.0.0.1:8094/api/v1/health
```

应返回`status: ok`、`service: ai-gateway`及当前版本（本次0.5.4），再使用浏览器控制台或原平台Key调用Codex。若curl错误7，先恢复本地SSH转发；若提示端口已占用，查看自己原来的SSH窗口，不自动终止不明进程。只检查TCP端口开放不足以证明连接到正确网关，须核对健康响应。

仅在添加GPT账号、需要接收浏览器OAuth回调时，将`-L localhost:1455:127.0.0.1:8094`额外加入上述SSH命令。1455是Windows本机回调端口，虚拟机不新增1455监听。重建前先在自己原来的隧道窗口按Ctrl+C释放8094；若1455被其他程序占用，可使用下方手动备用，普通API调用继续使用只转发8094的命令。

## 账号、模型与Key配置

1. 打开GPT账号池，首次创建账号池连接，选择所属空间和任务共享范围。默认仅所选空间。
2. 添加GPT账号，确认任务范围，生成登录链接并在新标签页登录。五分钟内完成后，回调页面提示返回控制台，控制台自动确认凭据已保存和载入。
3. 若localhost:1455打不开，直接复制地址栏完整地址，返回控制台粘贴至“授权后返回的完整地址”并提交。该地址只用于这一次登录，不发送给其他人；复制错误时可修改，不会清空输入。
4. 等待账号登录成功。若失败，按具体错误处理；点击“重新登录”生成新链接，旧授权码不可重复使用。为其他账号重复添加，支持批量启用、停用、续期。
5. 成功后自动同步模型目录；也可手动同步。选择发布模型路由，填写平台售价、上下文和输出预算。发布是显式操作，不自动覆盖既有模型和资费；批次最多50项，冲突时整批不写入。
6. 生成项目Key，选择当前空间的应用和已发布模型。完整Key仅显示一次，保存对应Base URL及Codex配置。每个项目使用独立Key；可在应用与密钥页设置模型、有效期、IP、限额及撤销。
7. 在钱包与套餐确认当前空间有可用额度；沿用人工核款/内部额度流程，不自动充值。账号官方额度与平台钱包分别记录。
8. 将接入信息填写到Codex或其他系统的AI配置，主动发起一次小额测试，再检查请求用量。配置和模型同步本身不生成内容。

## Codex 与其他AI模块

0.5.4已修复将Codex工具定义、系统上下文的UTF-8字节数直接当作Token造成的429和上下文误拦截。继续使用原有项目Key，在原会话重新发送问题即可复测。OpenAI/本机账号池采用离线分词估算，资金冻结保持独立保守上界；真实超限仍按原配额和模型窗口处理，计算方式见[接入与额度](接入与额度.md)。已验证两种实际Codex工具模式的大上下文受控链路及一次真实账号Responses生成；Windows桌面显示以客户端复测为准。

在Windows运行客户端时，Base URL为`http://127.0.0.1:8094/v1`；其他机器/服务器需建立其自己的隧道，或填写实际可达的HTTPS网关地址，不能直接使用别的电脑的localhost。

Codex配置示例（用控制台生成的模型别名替换示例）：

```toml
model_provider = "gateway"
model = "gpt/your-model"
web_search = "disabled"

[model_providers.gateway]
name = "AI Gateway"
base_url = "http://127.0.0.1:8094/v1"
wire_api = "responses"
env_key = "AI_GATEWAY_API_KEY"
requires_openai_auth = false
supports_websockets = false
request_max_retries = 0
stream_max_retries = 0
```

在启动Codex的PowerShell窗口设置`$env:AI_GATEWAY_API_KEY = "sk-gw-你的平台Key"`。其他AI模块选择OpenAI兼容接口，填写同样的Base URL、Key和模型名。平台支持Responses、Chat Completions和SSE/客户端工具；账号令牌不会发给客户项目。

## 本机账号池出站代理（0.5.1）

本机已将现有Mac Clash局域网HTTP代理`http://172.16.61.1:7890`显式配置给账号池，写在服务私有`config/config.yaml`的`requests.proxy-url`中；运行中的管理配置读取已确认。仅终端导出http_proxy/https_proxy不会让用户级systemd服务自动继承。不要将172.16.61.1当作Mac对外局域网IP，两者用途不同。

维护时只修改既有requests段中的proxy-url字段，保留内部Key、auth-dir、轮询/重试和日志开关等其他配置。config目录700、文件600，不提交Git。Mac须保持Clash运行并允许虚拟机访问该代理端口；地址或端口变更时更新私有配置后重启账号池。该设置用于账号池的认证、续期和上游请求，本机管理连接仍直连127.0.0.1:8095。

自动回调打开`/auth/callback/result?status=accepted`表示回调已接收，直接返回原控制台等待最终状态，不要再复制这个结果页地址手动提交。只有无法打开原始回调页面时才使用包含code/state的原始地址作为手动备用。控制台的最终网络/地区限制提示与输入框校验提示分别显示。

## 调度与失败提示

新会话在当前可用、支持模型的账号间公平轮询；同会话优先保持账号1小时。额度或授权故障时排除相应账号，后续请求可重选，已经发送的请求不自动重放。不同平台Key和模型的会话隔离。未获得可靠剩余额度时显示“未知”，不将多个账号额度显示为合并余额。

- 登录过期/授权码失效：重新生成链接，不提交旧地址。
- state不匹配：复制本次登录的完整回调地址，不能混用其他标签页或旧事务。
- 服务器网络失败：检查虚拟机本身的DNS、认证站连通性和出站代理；浏览器能登录不等于服务器能交换凭据。
- 令牌交换HTTP 403等拒绝：按页面HTTP状态检查服务器出口和账号访问权限；不会将它误报为localhost端口错误。
- 保存失败：检查账号池数据目录权限和磁盘空间。
- 凭据已保存、等待载入：等待状态确认，不重复交换授权码；持续异常时检查服务状态。

配置就绪与实际生成成功分别显示。退出、登录切换、服务重启或超时后需重新添加；回调始终校验原发起人的有效会话和一次性state。OAuth后台协议参考[CLIProxyAPI v8契约](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.72/docs/management-api-v8.md)。

## 本机维护入口

访问仍为README中的127.0.0.1:8094或SSH隧道；公开HTTPS按运行文档另行配置。账号池管理端口8095只允许本机，不作为外部项目Base URL使用。内部两种Key在services/gpt-account-pool/config/connector.json中，目录700/文件600；用户授权文件在该服务data/credentials，不提交Git。网关只保存加密的内部调用凭据，不复制本机已有Codex登录文件。

```bash
cd /home/nanshan/workspace/projects/ai-gateway
PYTHONPATH=src .venv/bin/python scripts/manage_account_pool.py status
systemctl --user status gpt-account-pool.service
```

若重建环境，先运行manage_account_pool.py install安装固定版本并创建私有配置，再在网关部署命令传入`--account-pool-config /home/nanshan/workspace/services/gpt-account-pool/config/connector.json`。安装程序只建立空账号池，不替你登录或生成内容。日志正文及账号池进程stdio不持久化，健康和错误通过网关的白名单状态显示；systemd仍保留服务启停状态。

## 备份与恢复

网关数据库和独立账号池分别备份。先核对空间，保留全部历史快照，由人工执行保留策略：

```bash
PYTHONPATH=src .venv/bin/python -m aigateway.cli backup
PYTHONPATH=src .venv/bin/python scripts/manage_account_pool.py backup
PYTHONPATH=src .venv/bin/python scripts/manage_account_pool.py verify \
  --snapshot /home/nanshan/workspace/backups/gpt-account-pool/快照标识 \
  --output /home/nanshan/workspace/tmp/ai-gateway/全新账号池恢复目录
```

独立账号池备份会短暂进入网关维护，等当前请求结束后停止账号池进程、一致性复制配置/授权文件、生成哈希，再恢复原服务状态；不自动删除旧备份。恢复验证不覆盖原服务、不自动启动；旧授权留在恢复副本archived-source中，新配置使用新内部Key和空授权目录，需要重新登录，避免复用已经轮换的refresh token。网关恢复工具会清除account_pool_config连接，防止恢复副本管理原账号池。正式恢复需要人工核对两份快照及资金流水。

GitHub调研及采用依据见[研究记录](GitHub项目研究.md)，部署及实际验证结果见[验收记录](验收记录.md)。

## 0.5.2 Codex工具声明兼容

新版Codex桌面端/CLI可能将工具定义放在input additional_tools项中，0.5.2已支持其中的客户端function/custom/namespace及execution:client工具搜索，保留输入顺序和历史作用域。原0.5.1会在网关校验阶段报“请通过tools显式提供客户端工具定义”，该错误与账号登录、代理和Key连通性分别处理。服务端托管工具仍未开放。平台钱包需有可用消费额度，管理员内部使用可经运营管理中的“发放额度”登记虚拟额度。
