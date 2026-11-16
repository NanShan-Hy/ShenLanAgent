# 账号池持久绑定适配

固定上游CLIProxyAPI 8.0.72 / e96d4c4818851cf0e96d27b2bc8b67379e449d9e（MIT），仅增加管理认证保护的安全账号目录，以及客户端认证后的内部账号指定/实际选中账号回执。继续使用SDK原有选择、冷却、OAuth和协议实现；不是供应商发布的原版二进制。

构建：`PYTHONPATH=src .venv/bin/python scripts/build_account_pool_adapter.py`。固定Go1.26.10、源码及安装包SHA-256，应用本目录补丁后CGO_ENABLED=0编译，版本8.0.72-gateway.2；工具及安装包遵循工作区目录规范，不更改系统Go/Python。依赖版本由固定源码go.mod/go.sum约束。已存在不同哈希的同名工具拒绝覆盖。

内部接口：GET /v8/management/gateway-accounts?model=...，仅管理Key可访问，只返回账号ID、提供商与就绪标志，不返回凭据。模型调用增加X-Gateway-Account-ID，使用SDK PinnedAuthMetadataKey指定账号；回执X-Gateway-Account-ID及X-Gateway-Pinning-Version=1由SDK选中账号回调写入。外部平台Key无法调用管理接口；网关不转发客户提供的内部头，回执也不透传给客户。

部署前以真实适配二进制和合成三账号验证；源码补丁、构建脚本、哈希清单和原MIT许可证分别保留。原工具8.0.72及前期构建候选不删除。真实账号目录仅在运行服务中使用，不进入构建环境。
