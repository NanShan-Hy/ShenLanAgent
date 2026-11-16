# AI Gateway

独立多租户AI网关，为Codex、测试平台和其他项目提供平台Key、模型路由、分层额度、预付钱包及套餐。账号、数据库、服务和管理界面独立，不依赖TestDock或AI工作台。

- 用户中心：空间/成员、应用、Key、模型目录、请求用量、钱包、套餐、充值订单。
- 运营后台：资源池、上游凭据、模型与版本化资费、配额、套餐、人工核款/退款、用量对账、审计。
- 连接与调度：显式连接测试、模型目录、连接池状态、调用尝试记录；HTTP连接复用、故障熔断、单路半开验证和受限安全切换。
- GPT账号池配置向导：独立CLIProxyAPI适配服务、管理员添加多个GPT账号、自动回调及手动备用、具体登录失败诊断、批量账号管理、模型同步与资费发布、项目Key和Codex配置，后续可在控制台完成配置。
- Codex兼容：支持Responses Lite的additional_tools输入声明及客户端工具搜索；使用离线分词估算上下文/Token预算，金额保持独立保守冻结；保留工具作用域和顺序，服务端托管工具仍未开放。
- API：`/v1/models`、`/v1/chat/completions`、`/v1/responses`、`/v1/responses/compact`；HTTP/SSE，文本及客户端工具调用。
- 持续会话：显式托管模式、按账号/应用/外部用户/授权版本/任务类型续聊、完整历史加密、自动摘要与版本冲突保护；后台选择严格固定账号或上下文连续优先，实际账号绑定跨账号池重启保留。
- 网页与MCP：标准文字/流式接口保持，支持接入方自己配置执行的STDIO/HTTP MCP；工具声明、调用、回传与等待状态完整往返，网关不执行客户工具。
- 图片与附件：平台私有上传/分段/读取/清理，支持图片、PDF/扫描件、DOCX、XLSX及文本；视觉路由需验证能力与预算后启用，会话/附件默认共1GiB、手动清理。
- 请求审计：运营管理员可按用户/空间/应用/Key/模型/状态查看调用指标、完整本轮问答和工具过程；正文加密、脱敏、分页查看，默认1GiB、手动清理，记录失败继续调用并明确标记。
- 安全：当前身份/空间/应用/Key授权交集，Key摘要存储、上游凭据加密、消费先冻结后结算；请求审计正文仅运营管理员可读，托管历史供当前授权调用方和运营管理员查看；认证资料与明文正文不进入普通日志，查看/清理留操作审计。
- HTTPS准备：提供关闭敏感访问日志和SSE缓冲的代理模板与配置生成器。
- 0.5统一使用GPT账号池，旧SIWC商务页面、接口与运行代码已移除；账号登录保存并载入后继续模型和Key配置。旧数据表只读保留，不再参与调度。普通API资源仍可使用OpenAI、DeepSeek及兼容API Key。在线支付未接入，首期人工核款。

## 快速开始

```bash
cd /home/nanshan/workspace/projects/ai-gateway
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
PYTHONPATH=src .venv/bin/python -m aigateway.cli init
PYTHONPATH=src .venv/bin/python -m aigateway.cli serve
```

本机已按隔离环境安装依赖（项目pip 26.3）；上述命令供重建时使用。本机系统venv缺少pip时，可先执行python3 -m venv --without-pip .venv，再用.venv/bin/python运行workspace/downloads/installers/pip/get-pip.py引导，不改动系统Python。默认配置位于workspace/services/ai-gateway/config/settings.json；支持全局参数`--config /绝对路径/settings.json`或环境变量`AI_GATEWAY_CONFIG`。

默认仅监听 `http://127.0.0.1:8094`。初始账号admin，随机密码保存在服务config/initial-admin.txt（600），不在输出或文档展示。首次登录修改密码。Windows经Mac宿主机访问时，使用[账号池配置指南](docs/GPT账号池配置指南.md)中的ProxyJump保活命令，建立本机8094转发并检查健康响应；172.16.61.128是VMware私有网络地址，Windows另一台电脑通常须通过Mac中转。

普通API和Codex调用只需8094。添加GPT账号时再按指南启用1455浏览器回调转发；1455为浏览器本机端口，虚拟机仍只监听原网关端口。SSH窗口须保持运行，断开后重新建立连接。

随后打开本地 `http://127.0.0.1:8094`。其他项目也可通过SSH隧道或完成HTTPS反代后接入。

## 使用顺序

配置GPT账号池请打开侧栏“GPT账号池”，按[账号池配置指南](docs/GPT账号池配置指南.md)完成共享范围、账号授权、模型发布和项目Key。以下是普通API资源的配置流程。

1. 运营管理→创建API资源池；同一供应商组织或共享额度使用相同限流分组。
2. 添加真实上游API凭据，点击“连接测试”读取模型目录；选择模型创建路由，核对开放协议和人民币资费后再启用。目录读取成功只证明可访问列表，实际调用仍须验证。
3. 用户创建充值订单；运营管理员核实线下到账后填写依据并入账，或明确发放虚拟额度。
4. 应用与密钥→为每个项目创建独立Key；配置模型范围、有效期、IP和额度，保存一次性显示的完整Key。
5. 按接入文档设置Base URL、Key及模型；先做明确的小额试调用并检查用量记录。

## 文档与示例

- [网页会话与MCP接入](docs/网页会话与MCP接入.md)：外部网站、托管请求头、身份隔离、图片与附件及恢复；[后端示例](examples/managed_chat.py)。
- [开发计划书](docs/开发计划书.md)
- [接入与额度](docs/接入与额度.md)：协议、SDK、Codex、工作台配置、费用、套餐与异常。
- [GPT账号池配置指南](docs/GPT账号池配置指南.md)、[GitHub项目研究](docs/GitHub项目研究.md)：管理员本机账号池、首次配置与参考依据。
- [运行与维护](docs/运行与维护.md)：目录、配置、部署、服务、备份恢复及对外访问。
- [HTTPS代理模板](deploy/nginx-site.conf.template)、[配置生成脚本](scripts/render_https_proxy.py)：默认仅本机高端口，真实域名、证书及公网入口待配置。
- [验收记录](docs/验收记录.md)：实际测试、客户端和部署结果、待联调事项。
- [系统体检与改进清单](docs/系统体检与改进清单.md)：已复现缺陷、模块检查范围和请求审计边界。
- [Codex配置示例](examples/codex.config.toml)、[Python客户端](examples/python_client.py)。

管理界面提供接入文档，OpenAPI JSON位于`/api/openapi.json`。接口管理路径以`/api/v1`为前缀；管理会话与推理Bearer Key分别鉴权。管理POST/PUT/PATCH/DELETE需同源和`X-Requested-With: AIGateway`。

## 本机存储

源码projects/ai-gateway，依赖项目.venv；服务services/ai-gateway/app、config、data、logs分开。开发数据data/ai-gateway/dev，测试tmp/ai-gateway，手动备份backups/ai-gateway，旧程序archives/ai-gateway。配置/主密钥/数据库/备份目录700，敏感文件600；真实凭据与数据不提交Git。遵循[本机目录规范](../../docs/虚拟机目录与存储规范.md)。

## 验证

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q --basetemp=/home/nanshan/workspace/tmp/ai-gateway/qa-唯一标识
node --input-type=module --check < src/aigateway/static/app.js
```

测试用独立库和本地协议响应，不调用真实模型、不进行真实收付款；Codex探针使用临时项目和一次性合成Key，不改写已有Codex配置/账号文件。真实供应商、实际收付款、公开HTTPS及整机重启状态见验收记录。
