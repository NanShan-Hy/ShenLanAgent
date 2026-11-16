# 离线分词资源

来源：OpenAI [tiktoken 0.14.0](https://github.com/openai/tiktoken)，MIT许可证随本目录的LICENSE.tiktoken保留。分词正则采用该版本tiktoken_ext/openai_public.py；开发时与已安装官方构造器逐项核对一致。

| 资源 | 官方下载位置 | SHA-256 |
| --- | --- | --- |
| cl100k_base.tiktoken | https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken | 223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7 |
| o200k_base.tiktoken | https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken | 446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d |

这些是公开词表，不含用户内容。资源与源码一起版本管理、打包、校验及备份；运行时从本目录读取并检查固定SHA-256，不发起下载或生成/tmp缓存。仅缓存公共Encoding对象，不缓存输入或Token数组。缺失或损坏时拒绝启动；已运行服务中若无法加载则返回脱敏503，不静默降低保护。

OpenAI与本机GPT账号池采用两种词表计数的较大值，加10%及2048 Token协议余量；按1024字符分块限制重复长字符串的分词耗时。该值用于本地上下文和Token配额预留，仍为估算，不代表供应商精确Token数。DeepSeek和其他兼容供应商继续采用原UTF-8字节上界。金额预冻结对所有供应商仍使用原字节上界，不使用较小的分词估算；实际结算仍需可靠供应商usage。
