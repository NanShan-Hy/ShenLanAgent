# 工作区 AI · 知识与记忆

独立的本地知识、消息、长期记忆和受控AI分析工作台，与TestDock按当前账号权限连接。默认检索全部可访问项目；私人会话、派生内容、通知、附件与缓存均遵循来源权限。

## 使用入口

本机 http://127.0.0.1:8090；虚拟机局域网 http://172.16.61.128:8090。管理员初始凭据位于 /home/nanshan/workspace/services/ai-workbench/config/initial-admin.txt（600），首次登录后修改密码。

- [体系与权限设计](docs/体系设计.md)
- [使用与维护](docs/运行与维护.md)
- [开发计划](docs/开发计划书.md)
- [验收记录](docs/验收记录.md)
- [第三方组件与复用](THIRD_PARTY.md)

## 功能

本地文档/代码/PDF/Word/Excel与中英文OCR、FTS5+512维向量混合检索、私人AI会话、项目讨论/回复/提及/通知、个人/项目/共享记忆审核、资料版本及来源失效、后台任务/取消/重试、OpenAI与DeepSeek配置、模型外发与Token额度、TestDock授权绑定/报告证据/失败订阅/可选自动分析、用例草案人工确认应用。

模型服务当前预置但未启用，密钥不回显；没有配置真实模型密钥，不声称已完成OpenAI/DeepSeek真实生成联调。本地知识检索、OCR、讨论和记忆管理可独立使用。接口协议与草案闭环使用本地测试服务验证，记录中单独标明。

## 开发检查

Python 3.12，项目.venv隔离，requirements.lock锁定实际依赖。自动化测试使用项目tmp下独立库与本地被测服务，不操作生产数据。

```bash
cd /home/nanshan/workspace/projects/ai-workbench
OPENBLAS_NUM_THREADS=1 .venv/bin/python -m pytest -q --basetemp="/home/nanshan/workspace/tmp/ai-workbench/check-$(date -u +%Y%m%dT%H%M%S)"
node --input-type=module --check < src/aiworkbench/static/app.js
PYTHONPATH=src .venv/bin/python -m compileall -q src scripts
```

API文档 /api/docs。使用系统Python建立.venv --without-pip后通过工作区已归档的get-pip引导，再安装requirements.lock；未修改系统Python或Node。公开模型通过scripts/prepare_models.py准备，运行时嵌入模型只读本地缓存。

服务使用用户级systemd，无人值守运行状态以工作区软件与服务清单为准。历史备份和资料不自动清理，遵循 [本机存储规范](../../docs/虚拟机目录与存储规范.md)。

## 0.2 站内助手集成

TestDock 1.3通过HMAC签名及与来源登录绑定的短期委托读取本人知识/记忆，不复制密码、不跨库访问。已有本人绑定复用，新身份无密码且不提升管理员权限。原工作台UI、知识/OCR和模型配置保留。schema2→3升级前备份并显式迁移；集成接口与来源实时权限同测，参见TestDock[全局AI助手说明](../autotest-platform/docs/全局AI助手.md)。
