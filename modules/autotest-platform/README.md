# TestDock 自动化测试平台 · 1.3

1.3 新增[全局 AI 助手](docs/全局AI助手.md)：站内问答与快捷侧栏、工作台知识和记忆、报告/截图/附件分析、网关托管续聊，以及逐项确认的资产、环境普通配置、计划和回归操作。默认关闭真实模型接入，管理员填写专用Key后单独联调；原测试业务与独立工作台保留。

面向小团队的本机自托管平台，通过可视化步骤编排 Web UI、HTTP 接口及混合用例，在多个被测环境复用套件。移动端按需求暂缓。

## 入口与使用

- 本机 http://127.0.0.1:8088；当前虚拟机局域网 http://172.16.61.128:8088（随网络变化）。
- 初始账号与随机密码：`/home/nanshan/workspace/services/autotest-platform/config/initial-admin.txt`，仅当前用户可读。首次登录后在右上角账户设置修改密码。
- API 文档 `/api/docs`；健康检查 `/api/health`。
- [开发计划书](docs/开发计划书.md)、[运行与维护](docs/运行与维护.md)、[验收记录](docs/验收记录.md)、[复用与第三方说明](THIRD_PARTY.md)。

创建项目 → 配置环境地址、普通/敏感变量 → 创建步骤与断言 → 组合有序套件 → 选择环境执行 → 查看逐步报告。内置两套真实本地演示目标和 Web/API 冒烟用例；可添加自己的业务环境，演示不代表已接入用户业务系统。

## 功能与实现

项目成员 admin/tester/viewer 权限、可视化 Web 步骤、HTTP 请求、JMESPath 断言和响应提取、加密敏感变量、用例版本及执行快照、持久队列、手动/Cron/CI 触发、令牌与幂等提交、取消与超时、逐步报告、失败截图/Playwright Trace/JUnit。

FastAPI + SQLite + HTTPX + Playwright Chromium；前端为原生 JavaScript 模块，无需 Node 构建。2 核虚拟机默认单执行器串行运行；每个用例隔离会话和变量，步骤内可传递数据。运行配置、数据、附件、密钥、日志和备份均放在源码外，遵循 [本机目录规范](../../docs/虚拟机目录与存储规范.md)。

## 业务接入

页面侧栏“接入指南”提供完整操作说明和销售流程示例下载；对应文档为 [业务接入手册](docs/业务接入手册.md)。平台保持领域无关，化工品销售仅作为商品→报价→订单的示例，便于复制改成其他项目。

1.1 新增资产包导入/导出、OpenAPI 3 JSON 预览与选择导入、多个环境一次提交、原快照复现/最新配置失败重跑、全部历史筛选与分页。请求实际体积不超过 2MiB，HTTP 解压后响应不超过 5MiB；默认队列串行执行。无新增依赖和数据库迁移。

## 检查

```bash
cd /home/nanshan/workspace/projects/autotest-platform
.venv/bin/python -m pytest -q --basetemp="/home/nanshan/workspace/tmp/autotest-platform/check-$(date -u +%Y%m%dT%H%M%S)"
node --input-type=module --check < src/testdock/static/app.js
PYTHONPATH=src .venv/bin/python -m compileall -q src scripts
```

自动化测试创建独立数据库及临时服务器，不操作服务数据。安装、部署、CI 与恢复命令见运行说明。用户级 systemd 已配置；当前Linger=yes且用户服务已启用；整机重启/注销实测未执行，详见本机清单。

## AI工作台联动

[独立AI工作台](../ai-workbench/README.md)运行于8090。账户设置可生成60秒一次性绑定码，并可随时撤销用户委托；报告的AI分析入口导入本人有权限的只读证据。来源角色由本平台实时验证。模型无密钥时仍可同步资产、本地检索；AI草案仅在人为确认后新增用例，不执行测试。


## 1.2 每日回归与结果推送

定时任务支持每天/工作日时间选择、未来时间预览、多环境批次及统一汇总。新增通知推送页面，支持钉钉内部应用、存量Webhook和Server酱个人微信，具备持久队列、重试、发送状态及人工重发。详见[每日任务与通知说明](docs/通知与每日任务.md)。真实渠道需在页面填写凭据，本地协议测试不代表实际手机送达。服务的无人值守配置以本机软件服务清单和本次验收记录为准。
