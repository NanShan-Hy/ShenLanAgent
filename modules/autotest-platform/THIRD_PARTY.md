# 复用、参考与许可证

记录日期：2026-11-15 UTC。TestDock 自有实现使用根目录 MIT 许可证；第三方组件按各自许可证使用，原文保留于 docs/licenses。未将候选平台的业务源码移植进本项目。

## GitHub 设计参考

| 项目 | 参考点 | 采用方式与边界 |
| --- | --- | --- |
| [MokaTest](https://github.com/Jinglong233/MokaTest) | 可视化 UI/API、项目、环境、成员、调度的产品组织 | 参考交互和领域概念；调研时为 BSL 1.1，属于受限源代码许可，未复制其业务代码 |
| [CaseHUB](https://github.com/Caoyongqi912/case_auto_hub) | Python + FastAPI + HTTPX + Playwright、环境与 Jenkins 接入设计 | 复用其采用的通用开源库；独立实现平台。调研时 README 声明 MIT，但缺少独立 LICENSE 文件，未复制仓库业务代码 |
| [testhub_platform](https://github.com/chenjigang4167/testhub_platform) | Web/API 模块、可视化步骤、定时执行的拆分 | 仅参考设计；GPL-3.0 业务代码未并入。其 App/OCR/AI 组件不属于本次范围 |

这些项目的重型依赖与移动端能力未直接搬入本机。当前采用 SQLite 单 worker，适配 2 核/约 3.8 GiB 的虚拟机；多环境指被测业务地址/变量，而不是浏览器类型。

## 实际依赖复用

- [FastAPI](https://github.com/fastapi/fastapi)：路由、校验、OpenAPI 与静态文件。
- [Playwright](https://github.com/microsoft/playwright-python)：真实 Chromium、定位器、自动等待、断言、截图及 Trace。
- [HTTPX](https://github.com/encode/httpx)：HTTP 请求、会话、鉴权接口链路。
- [JMESPath](https://github.com/jmespath/jmespath.py)：JSON 断言和响应提取表达式。
- [croniter](https://github.com/pallets-eco/croniter)：Cron 计算；项目自行实现持久去重、权限复查及重叠处理。
- [cryptography](https://github.com/pyca/cryptography)：Fernet 加密，密码哈希使用 Python 标准库 PBKDF2。
- Uvicorn、Pydantic、pytest 及下表间接依赖用于运行与验证。

精确锁定版本见 requirements.lock。以下清单根据实际安装包 metadata 生成，许可证原文和包中 NOTICE（如有）保留于 [licenses](docs/licenses/inventory.json)。Playwright 自带驱动的第三方声明也一并保留。Chromium Headless Shell 许可证、Ubuntu libasound2t64 与 Noto CJK 字体版权文件单独保存。Chromium、FFmpeg 等工具二进制仍由 Playwright 原生缓存管理，没有打包提交 Git。

| 包 | 版本 | 元数据许可证 |
| --- | --- | --- |
| annotated-doc | 0.0.6 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.16.0 | MIT |
| certifi | 2026.10.23 | MPL-2.0 |
| cffi | 2.1.1 | MIT-0 |
| click | 8.5.1 | BSD-3-Clause |
| croniter | 6.2.4 | MIT |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| fastapi | 0.163.0 | MIT |
| greenlet | 3.5.6 | MIT AND PSF-2.0 |
| h11 | 0.16.0 | MIT |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| idna | 3.20 | BSD-3-Clause |
| iniconfig | 2.3.1 | MIT |
| jmespath | 1.1.0 | MIT |
| opentelemetry-api | 1.45.2 | Apache-2.0 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pip | 26.3 | MIT |
| playwright | 1.65.2 | Apache-2.0 |
| pluggy | 1.7.0 | MIT |
| pycparser | 3.12 | BSD-3-Clause |
| pydantic | 2.14.3 | MIT |
| pydantic_core | 2.50.4 | MIT |
| Pygments | 2.21.0 | BSD-2-Clause |
| pytest | 9.2.0 | MIT |
| python-dateutil | 4.0.0 | BSD-3-Clause |
| python-multipart | 0.0.33 | Apache-2.0 |
| starlette | 1.10.0 | BSD-3-Clause |
| typing-inspection | 0.4.5 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| tzdata | 2026.6 | Apache-2.0 |
| uvicorn | 0.54.0 | BSD-3-Clause |

许可证说明只描述本次选型和本机安装状态；未来复制第三方业务代码或对外分发完整二进制包时，需要按实际引入内容重新保留对应声明。
