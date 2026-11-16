# 网页问答、托管会话与外部MCP（0.7）

标准Base URL、平台Key、模型别名不变。已有Codex或其他客户端每次发送完整历史时，继续使用/v1/chat/completions或/v1/responses及HTTP/SSE；不自动叠加平台历史。MCP由接入方配置、鉴权、发现和执行，网关只处理客户端工具协议。它不是ChatGPT网站聊天窗口的远程控制接口，平台会话也不自动出现在ChatGPT官网历史中。

## 网页后端的接入顺序

1. 网站后端保管平台Key；浏览器访问自己的后端，由后端根据已验证登录信息产生稳定的外部用户标识。多用户共用Key时，在运营管理→会话与调度→会话与身份规则，为对应应用配置“可信后端传外部用户”。缺少标识将拒绝，不回退到所有网站用户共用历史。
2. 后端请求POST /api/v1/conversations/resolve，Bearer平台Key，JSON为`{"model":"你的模型别名","protocol":"responses","task_type":"sales_analysis","external_subject":"网站用户ID","authorization_version":"当前资料授权版本"}`。同平台账号、应用、外部用户、授权版本和类型默认取得当前会话。类型由业务方明确指定，缺省general_chat，不使用模型猜测分类。
3. 保存返回的id和revision。后续模型调用发送本轮增量，而非再次发送全部历史；请求头X-Gateway-Context-Mode=managed、X-Gateway-Conversation-ID=id、X-Gateway-Context-Revision=revision，并提供唯一Idempotency-Key。模型JSON及返回格式保持原Chat/Responses协议，store:false仍指供应商侧；本平台的托管保存由上述显式模式控制。
4. 请求头X-Gateway-Subject携带同一外部用户，X-Gateway-Authorization-Version携带同一授权版本。会话API、模型调用、上传/读取文件均使用相同身份头。外部业务权限收回或范围变化时，后端必须更新授权版本或明确关闭/新建会话，不能继续沿用包含旧权限资料的缓存。网关不替外部网站验证其登录与项目权限。
5. 成功响应头返回X-Gateway-Conversation-ID、X-Gateway-Turn-ID、X-Gateway-Context-Revision。每轮完成后使用新版本；遇到409先GET会话查询状态，不能自动重发。模型发送结果未知时也不自动重放。

用户点击“新聊天”时resolve增加new_session:true，更新同类任务的默认会话；已有窗口继续持有自己的id，不会被自动转到新窗口。不同环境宜使用不同应用或明确不同task_type。更换会话模型/协议需明确新建，账号策略由后台设定，调用方不能通过请求头强制指定具体上游账号。

## 工具调用与全局分析

接入方把自己配置的MCP工具转成function/custom/namespace或Responses execution:client工具搜索声明。模型返回function_call/custom_tool_call后，由网站后端、Codex或其他外部运行时执行MCP，将匹配call_id的结果作为下一轮增量提交。并行工具需一并返回当前pending_calls全部结果；待回传时不能混入另一个新任务。网关不会连接MCP、执行命令、接受托管type:mcp工具或获得MCP令牌。

模型可以汇总多个工具提供的项目/知识/报表，资料范围和实时权限由外部运行时控制；应在工具结果中保留项目、来源、时间和证据。平台Key不赋予业务系统数据权限。工具记录区分模型请求与客户端回传，不能独立证明远程电脑已经执行动作。

## 图片和附件

POST /v1/files使用Bearer Key及相同身份头，multipart字段file和purpose=user_data；单文件20MiB。返回平台file_id，Responses输入为`{"type":"input_file","file_id":"file-..."}`，Chat内容为`{"type":"file","file":{"file_id":"file-..."}}`。单次分析最多4附件/4视觉页。支持PNG/JPEG/WebP、PDF、DOCX、XLSX、CSV/TSV、TXT/Markdown及常见代码文本；不执行宏、脚本或文档指令。直接图片也支持data:image/...;base64输入，不主动抓取远程URL。

GET /v1/files/{id}提供状态、分段、页数或工作表信息；GET /v1/files/{id}/content读取原文件；DELETE清理文件。长文件state=needs_selection时，在文件输入中增加selected_sections，如`["page:101"]`、`["Sheet1:rows:1-500"]`或文件信息返回的文本行段标签。不会静默截断整份文件。请求JSON仍为4MiB上限，大文件使用上传接口；解析超时、损坏、压缩展开超限均明确拒绝。

普通文档先在受限子进程提取文本/表格和来源位置，图片及扫描PDF需要已验证视觉模型。管理员在“模型图片能力”确认实际模型支持、最大图片数和单图保守Token预算后启用；图片归一化最长边1024像素，不按Base64长度计算Token。真实usage仍按模型资费结算。不支持的模型明确拒绝，不自动更换或丢弃附件。

## 历史、摘要、失败与清理

GET /api/v1/conversations/{id}查询版本、状态、待回传工具及摘要；GET .../{id}/turns分页查询轮次，GET .../{id}/turns/{turn}/content?section=input|output&cursor=0分页读取原内容。正文仅授权调用方或运营管理员可读，管理员读取留审计。POST .../{id}/close关闭，DELETE .../{id}清理托管内容，保留请求和账本；附件独立保留/清理。

默认会话与附件共1GiB，手动清理；运行上下文单独加密，认证头和MCP连接凭据不保存。接近模型窗口80%时自动摘要，正常保留近期历史并避免拆断工具调用/结果；视觉历史可提前摘要以控制媒体数量。原文仍加密保留，摘要不能保证逐字无损，也不代表原模型推断已确认为事实。摘要是单独模型调用，计入同一Key/账号额度、资费和请求审计，不隐式免费。摘要失败不静默截断历史。

托管调用每次预留40MiB容量用于输入和响应，完成后释放未用部分；容量/磁盘不足在派发前拒绝。派发后保存失败，非流式响应头X-Gateway-Context-Warning=needs_recovery，流式在终态前发送context_save_failed错误；保留已收到的模型结果，查询会话后明确恢复。既有请求审计仍是记录失败继续策略，不能用审计记录代替执行上下文。

POST .../{id}/recover发送`{"acknowledge_unknown":true}`，不会重放或更改原财务状态；可额外提供原协议response对象恢复接入方已保存的结果，来源标记client_restored。未提供结果时从已完成历史继续，未确认工具会明确标为取消待回传，不声称其实际未执行。POST .../{id}/cancel-tools发送`{"acknowledge_cancellation":true}`可取消等待；接入方仍需自己管理正在运行的外部工具。

后台支持严格固定账号和上下文连续优先。严格账号不可用时等待或明确迁移；连续优先在后续新调用中重选账号并留记录。绑定跨账号池重启保存；跨账号只续接可读内容、工具结果和摘要，供应商私有推理状态不能保证迁移。新会话继承应用/任务类型规则快照，修改规则不静默迁移已有任务。

示例见examples/managed_chat.py。对外网站后端沿用受控网络/SSH访问，公网HTTPS仍按既有部署材料单独配置；不应把127.0.0.1当作云端网站可直接访问的地址。
