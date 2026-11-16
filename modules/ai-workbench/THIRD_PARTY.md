# 第三方组件与复用

AI工作台自有实现使用根目录MIT许可。复用了本机TestDock 1.1（MIT）的存储/认证惯例、部分管理界面样式与CaseInput校验模型（drafts.py）；第三方许可证不改写，保存于docs/licenses。来源项目在 /home/nanshan/workspace/projects/autotest-platform，运行时通过授权HTTP接口连接，不直接导入其运行服务。

实际依赖采用FastAPI/Uvicorn/HTTPX/cryptography、NumPy、FastEmbed/ONNX Runtime、RapidOCR、pypdfium2、python-docx、openpyxl/defusedxml/Pillow。TestDock桥接契约测试额外使用croniter，浏览器QA复用既有TestDock虚拟环境中的Playwright和原生浏览器缓存。

模型：BAAI/bge-small-zh-v1.5（MIT），FastEmbed公开模型缓存本地512维；RapidOCR（Apache-2.0）默认的PP-OCRv6小检测/识别和移动方向分类器，模型来自公开的RapidAI/RapidOCR ModelScope版本v3.10.0。模型二进制位于workspace/tools/ai-models，不进入源码仓库；摘要清单保存于该目录manifest.json。文件解析与模型处理都在本机进行，公开模型下载不上传项目资料。

官方来源：[FastEmbed](https://github.com/qdrant/fastembed)、[BGE](https://huggingface.co/BAAI/bge-small-zh-v1.5)、[RapidOCR](https://github.com/RapidAI/RapidOCR)、[PDFium包装](https://pypdfium2.readthedocs.io/)、[OpenAI API](https://developers.openai.com/api/reference/resources/chat)、[DeepSeek API](https://api-docs.deepseek.com/)。没有复制外部AI平台业务源码，没有使用用户Codex/ChatGPT登录凭据作为模型API密钥。

安装包元数据清单与版权原文见 [许可证清单](docs/licenses/inventory.json)。所有依赖以requirements.lock中的实际锁定版本为准。

| 包 | 版本 | 许可证 |
| --- | --- | --- |
| annotated-doc | 0.0.6 | MIT |
| annotated-types | 0.8.0 | MIT |
| anyio | 4.16.0 | MIT |
| certifi | 2026.10.23 | MPL-2.0 |
| cffi | 2.1.1 | MIT-0 |
| charset-normalizer | 3.5.2 | MIT |
| click | 8.5.1 | BSD-3-Clause |
| colorlog | 6.12.0 | MIT License |
| croniter | 6.2.4 | MIT |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| defusedxml | 0.7.1 | PSFL |
| et_xmlfile | 2.0.0 | MIT |
| fastapi | 0.163.0 | MIT |
| fastembed | 0.9.1 | Apache-2.0 |
| filelock | 4.2.10 | MIT |
| flatbuffers | 25.12.19 | Apache 2.0 |
| fsspec | 2026.9.0 | BSD-3-Clause |
| h11 | 0.16.0 | MIT |
| hf-xet | 1.8.0 | Apache-2.0 |
| httpcore | 1.0.9 | BSD-3-Clause |
| httpx | 0.28.1 | BSD-3-Clause |
| huggingface_hub | 1.33.0 | Apache-2.0 |
| idna | 3.20 | BSD-3-Clause |
| iniconfig | 2.3.1 | MIT |
| jmespath | 1.1.0 | MIT |
| loguru | 0.7.3 | 见原文 |
| lxml | 6.1.3 | BSD-3-Clause |
| mmh3 | 5.3.1 | MIT License |
| numpy | 2.5.4 | BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0 |
| omegaconf | 2.4.0 | 见原文 |
| onnxruntime | 1.32.0 | MIT License |
| opencv-python-headless | 5.0.0.93 | Apache 2.0 |
| openpyxl | 3.1.5 | MIT |
| opentelemetry-api | 1.45.2 | Apache-2.0 |
| packaging | 26.3 | Apache-2.0 OR BSD-2-Clause |
| pillow | 12.3.0 | MIT-CMU |
| pip | 26.3 | MIT |
| pluggy | 1.7.0 | MIT |
| protobuf | 7.36.2 | 3-Clause BSD License |
| py_rust_stemmers | 0.1.8 | 见原文 |
| pyclipper | 1.4.0 | MIT |
| pycparser | 3.12 | BSD-3-Clause |
| pydantic | 2.14.3 | MIT |
| pydantic_core | 2.50.4 | MIT |
| Pygments | 2.21.0 | BSD-2-Clause |
| pypdfium2 | 5.15.2 | BSD-3-Clause, Apache-2.0, dependency licenses |
| pytest | 9.2.0 | MIT |
| python-dateutil | 4.0.0 | BSD-3-Clause |
| python-docx | 1.2.0 | MIT |
| python-multipart | 0.0.33 | Apache-2.0 |
| PyYAML | 6.0.3 | MIT |
| rapidocr | 3.10.0 | Apache-2.0 |
| requests | 2.34.2 | Apache-2.0 |
| shapely | 2.2.0 | BSD-3-Clause |
| six | 1.17.0 | MIT |
| starlette | 1.10.0 | BSD-3-Clause |
| tokenizers | 0.23.3 | 见原文 |
| tqdm | 4.70.1 | MPL-2.0 AND MIT |
| typing-inspection | 0.4.5 | MIT |
| typing_extensions | 4.16.0 | PSF-2.0 |
| tzdata | 2026.6 | Apache-2.0 |
| urllib3 | 2.8.0 | MIT |
| uvicorn | 0.54.0 | BSD-3-Clause |
