# 迁移配置清单

- machine-state.json：原Linux虚拟机只读采集结果；不表示新机已验收。
- packages.tsv：原机dpkg软件版本清单，用于核对。
- artifacts.json：归档安装包与账号池适配版的来源校验信息；安装包不随源码发布。
- model-manifest.json：公开OCR/嵌入模型的哈希清单；模型文件由受控复制或模块下载脚本准备。
- systemd-user/：当前10个业务服务和1个slice的可复核单元，保留原路径与资源限制。
- config/：三个业务服务路径配置及账号池去敏示例；不是生产配置，不能用占位Key覆盖旧Key。
- proxy/：宿主机HTTP代理与no_proxy示例。

完整步骤见 [迁移总手册](../../docs/迁移总手册.md) 和 [代理与网络](../../docs/代理与网络迁移.md)。配置数据仍需配套私密迁移包，仓库不包含账号、主密钥、数据库、OAuth凭据、花生壳SN或宿主机订阅。
