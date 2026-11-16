# GPT账号池项目研究与采用记录

2026-11-15（UTC）。通过GitHub连接器检索并直接读取项目README、许可证及相关源码；本记录描述本次参考，星标数和广告内容不作为采用依据。

| 项目 | 仓库资料描述的相关能力 | 本项目采用方式 |
| --- | --- | --- |
| [CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI) | Codex OAuth、多账号轮询、会话亲和、Chat/Responses、管理接口 | 作为独立本机适配进程；网关只公开受控管理操作和项目API，保留自身权限/计费 |
| [Sub2API](https://github.com/Wei-Shaw/sub2api) | 订阅账号与API凭据管理、平台Key、并发和用量计费 | 借鉴分步配置和账号状态交互；保留自己的Python/SQLite实现，未复制其代码 |
| [New API](https://github.com/QuantumNous/new-api) | 渠道/模型、用户权限、Key限制、用量与资费 | 借鉴先选择模型和定价再分发项目Key的流程；未复制其代码 |

读取的许可证分别为[MIT](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.72/LICENSE)、[LGPL-3.0](https://github.com/Wei-Shaw/sub2api/blob/main/LICENSE)、[AGPL-3.0](https://github.com/QuantumNous/new-api/blob/main/LICENSE)。工具安装保留CLIProxyAPI原版LICENSE与README。这里只将CLIProxyAPI作为独立进程使用，不将另外两个项目嵌入现有产品。

## 固定版本及证据

采用[CLIProxyAPI v8.0.72](https://github.com/router-for-me/CLIProxyAPI/releases/tag/v8.0.72)，发布于2026-11-14。Linux amd64无插件包CLIProxyAPI_8.0.72_linux_amd64_no-plugin.tar.gz，GitHub发布资产标注SHA-256为12ff0146cc2f0b663088d7a8b597cef1b96767e7ac6c8844e3cd21669c2693c3；下载后独立核对，不执行远程安装脚本。管理API固定使用v8，不混用v0返回格式。

源文件依据：

- [v8管理契约](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.72/docs/management-api-v8.md)：授权回调接收和令牌交换完成分开；仅查询到ok后显示成功，支持取消未完成授权。
- [配置示例](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.72/config.example.yaml)：回环绑定、管理Key与调用Key、轮询、会话亲和、重试、日志、模型目录与插件开关。
- [Codex授权流程](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.72/internal/auth/codex/openai_auth.go)：PKCE授权链接及localhost:1455回调。网关接受用户主动粘贴的匹配回调，不读取其他工具登录文件。
- [账号状态修改](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.72/internal/api/handlers/management/auth_files_fields.go)、[凭据续期](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.72/internal/api/handlers/management/auth_files_refresh.go)：按实际契约提交name/disabled与单账号续期，白名单返回，不转发原始凭据或上游错误正文。
- [请求日志中间件](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.72/internal/api/middleware/request_logging.go)、[服务器装配](https://github.com/router-for-me/CLIProxyAPI/blob/v8.0.72/internal/api/server.go)：仅request-log:false仍有错误正文捕获；本机配置使用server.commercial-mode:true移除该中间件，同时关闭文件日志、统计聚合和进程stdio持久输出。这里的配置项是性能/日志选项，不表示商业接入资格。

## 本次具体改进

新增管理员GPT账号池页面：就绪步骤、共享范围、逐个登录授权、手动回调、授权结果轮询、账号批量启用/停用/续期、模型同步、批量资费发布及项目Key/Codex配置。账号摘要不转发令牌、路径、原始报错或未白名单字段。0.5已移除原生SIWC模块，统一管理本机GPT账号池。

账号池进程仅127.0.0.1:8095。管理Key与调用Key分别随机生成；网关仅对运维配置中指定的固定回环地址放行，不启用通用内网访问。后端额外重试0、每轮凭据尝试上限1、流式引导重试0；显式会话按项目Key和模型隔离后传入，会话不迁移。账号认证和额度状态交由后端逐账号管理，单个账号401/403/429不会错误地冻结全部独立订阅；原任务不重放，后续独立任务仅选择仍可用的账号。网关自身并发、RPM/TPM及资金限额照常执行，普通API分组冷却不改变。供应商精确剩余额度没有证据时显示未知，不推算成可合并余额。

所有账号配置仍由用户主动完成；本地合成供应商、真实二进制和真实客户端的验证，与真实账号的供应商调用结果分别登记。
