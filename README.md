# open-computer-use

`open-computer-use` 是一个面向 Linux 图形桌面的 GUI 智能体项目。  
它通过文本模型、浏览器自动化、桌面控制、WPS 表格操作和视觉兜底能力，执行文件处理、网页检索、文本编辑、表格填写等任务。

当前项目只保留一个启动入口：

```bash
python3 main.py
```

本文档按 `Kylin 2503` 环境编写，重点说明如何部署、运行和排查问题。

## 1. 适用环境

推荐环境：

- 操作系统：Kylin 2503
- Python：3.8 及以上
- 图形会话：X11
- 桌面：可正常使用鼠标、键盘、浏览器、WPS

推荐已安装的软件或系统组件：

- `xdotool`
- `xclip`
- `wmctrl`
- `fcitx` 或 `fcitx5`
- 搜狗输入法（可选）
- WPS Office（建议包含 `et` 表格组件）
- Chromium / Chrome / 系统默认浏览器

不建议在纯终端、无图形界面、Wayland 兼容性较差的环境下直接运行。

## 2. 项目能力概览

当前项目主要支持：

- 浏览器打开与搜索
- 搜索结果提取与整理
- 文本文件创建、覆盖、打开
- WPS 表格打开与单元格写入
- 文件夹创建、文件复制、移动、重命名、删除
- GUI 对话式执行
- 审计 / 授权 / 决策 / 记忆记录
- 常规执行失败后的视觉兜底

当前项目更适合：

- 中文桌面办公任务
- 浏览器检索 + 保存结果
- Kylin + X11 + WPS 的自动化场景

## 3. 目录说明

主要目录如下：

```text
open-computer-use/
├── main.py
├── requirements.txt
├── README.md
├── os_computer_use/
│   ├── agents/
│   ├── desktop/
│   ├── gui/
│   ├── llm/
│   ├── runtime/
│   ├── app_runtime.py
│   ├── logging.py
│   └── logging_utils.py
├── scripts/
├── memory/
├── output/
├── screenshots/
└── tests/
```

重点说明：

- `main.py`
  - 唯一启动入口
- `os_computer_use/agents/`
  - 意图理解、规划、执行、审计、决策、记忆
- `os_computer_use/desktop/`
  - 浏览器、桌面控制、截图、WPS 操作、视觉执行
- `os_computer_use/gui/`
  - PyQt5 图形界面
- `os_computer_use/llm/`
  - 本地模型和 OpenRouter provider
- `scripts/`
  - 本地视觉模型下载和服务启动脚本
- `memory/`
  - 记忆数据
- `output/`
  - 运行产物和日志
- `screenshots/`
  - 视觉兜底截图

## 4. 系统依赖安装（Kylin 2503）

先更新系统索引：

```bash
sudo apt update
```

建议安装以下系统依赖：

```bash
sudo apt install -y \
  python3 python3-venv python3-pip \
  xdotool xclip wmctrl scrot \
  fcitx fcitx-frontend-qt5 fcitx-module-dbus \
  python3-pyqt5 \
  libatspi2.0-0 gir1.2-atspi-2.0 \
  libxcb-cursor0 \
  unzip curl git
```

如果你使用 WPS 和搜狗输入法，还建议确认：

```bash
which et
ps aux | grep -E "fcitx|sogou" | grep -v grep
echo $XDG_SESSION_TYPE
```

预期：

- `et` 可用
- `fcitx` 正常运行
- `XDG_SESSION_TYPE=x11`

## 5. Python 环境准备

建议使用虚拟环境：

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install --upgrade pip
```

安装 Python 依赖：

```bash
pip install -r requirements.txt
```

## 6. requirements 说明

`requirements.txt` 已包含：

- 核心运行依赖
- PyQt5 GUI 依赖
- Linux AT-SPI 相关依赖
- 本地视觉模型依赖
- llama.cpp 视觉后端依赖
- 模型下载脚本依赖

其中以下依赖只在特定场景使用：

- `torch` / `transformers` / `accelerate`
  - 本地 Hugging Face 视觉后端
- `llama-cpp-python`
  - 本地 llama.cpp 视觉后端
- `huggingface_hub`
  - 模型下载脚本
- `pytest`
  - 测试

## 7. 启动方式

### 7.1 默认启动

```bash
python3 main.py
```

程序会启动 GUI，并在启动阶段预热模型能力。

### 7.2 指定文本模型和视觉模型来源

文本和视觉都支持：

- `local`
- `openrouter`

示例：

```bash
python3 main.py --text-provider openrouter --vision-provider local
```

全部使用本地：

```bash
python3 main.py --text-provider local --vision-provider local
```

全部使用 OpenRouter：

```bash
python3 main.py --text-provider openrouter --vision-provider openrouter
```

### 7.3 显式指定 OpenRouter 模型

```bash
python3 main.py \
  --text-provider openrouter \
  --vision-provider openrouter \
  --text-model openai/gpt-oss-20b:free \
  --vision-model nvidia/nemotron-nano-12b-v2-vl:free
```

### 7.4 显式指定本地模型

```bash
python3 main.py \
  --text-provider local \
  --vision-provider local \
  --local-reasoning-model /data/usershare/models/qwen2.5-3b-instruct-q4_k_m.gguf \
  --local-vision-model /data/usershare/models/Qwen3-VL-2B-Instruct-GGUF \
  --local-vision-backend llama_cpp \
  --llama-cpp-vision-url http://127.0.0.1:8080/v1 \
  --llama-cpp-vision-model qwen3-vl-2b-instruct
```

## 8. OpenRouter 配置

如果使用 OpenRouter，至少需要：

```bash
export OPENROUTER_API_KEY="你的 API Key"
```

如果系统依赖代理出网，建议同时打开：

```bash
export OPENROUTER_TRUST_ENV=true
```

这会让项目里的 `requests` 读取 `HTTP_PROXY` / `HTTPS_PROXY` / `ALL_PROXY`。

推荐启动方式：

```bash
export OPENROUTER_API_KEY="你的 API Key"
export OPENROUTER_TRUST_ENV=true
python3 main.py --text-provider openrouter --vision-provider openrouter
```

## 9. 本地模型配置

项目支持两类本地视觉路线。

### 9.1 llama.cpp 视觉后端

适合已经有可用本地推理服务的环境。

环境变量示例：

```bash
export OCU_TEXT_PROVIDER=local
export OCU_VISION_PROVIDER=local
export LOCAL_REASONING_MODEL_PATH=/data/usershare/models/qwen2.5-3b-instruct-q4_k_m.gguf
export LOCAL_VISION_BACKEND=llama_cpp
export LOCAL_LLAMA_CPP_VISION_BASE_URL=http://127.0.0.1:8080/v1
export LOCAL_LLAMA_CPP_VISION_MODEL=qwen3-vl-2b-instruct
```

### 9.2 Hugging Face 视觉后端

适合本地直接加载视觉模型目录。

```bash
export OCU_TEXT_PROVIDER=local
export OCU_VISION_PROVIDER=local
export LOCAL_REASONING_MODEL_PATH=/data/usershare/models/qwen2.5-3b-instruct-q4_k_m.gguf
export LOCAL_VISION_BACKEND=hf
export LOCAL_VISION_MODEL_PATH=/data/usershare/models/Qwen3-VL-2B-Instruct-GGUF
```

## 10. scripts 目录怎么用

`scripts/` 不是主入口，但对本地模型部署有帮助。

常见脚本：

```bash
python3 scripts/download_hf_model.py
python3 scripts/download_qwen3_vl_gguf.py
python3 scripts/run_local_vision_main.py
bash scripts/run_local_vision_main.sh
```

用途分别是：

- 下载 Hugging Face 模型
- 下载 GGUF 视觉模型
- 启动本地视觉服务

## 11. Kylin 2503 下的输入法说明

本项目当前针对 `Qt5 + X11 + fcitx` 做了适配。

启动时会自动处理这些环境变量：

- `QT_IM_MODULE`
- `GTK_IM_MODULE`
- `QT4_IM_MODULE`
- `XMODIFIERS`

如果你使用搜狗输入法，推荐检查：

```bash
find /usr -name "libfcitx*platforminputcontext*" 2>/dev/null
python3 -c "import PyQt5; print(PyQt5.__file__)"
echo $XDG_SESSION_TYPE
```

重点结论：

- 当前 GUI 基于 `PyQt5`
- 推荐 `X11`
- 推荐 `fcitx` 前端
- 不再依赖 Qt6 输入法链路

## 12. WPS 相关说明

当前表格自动化针对 `Kylin + WPS Office + et` 做了专门处理。

已知实现特点：

- 使用 `spreadsheet.open` 打开 `.et` / `.xlsx` / `.xls`
- 使用键盘导航方式写单元格
- 同一工作簿写入会串行执行，避免并发抢焦点
- 新文件不存在时可自动创建

建议提前确认：

```bash
which et
```

如果没有输出，说明 WPS 表格组件未正确安装。

## 13. 浏览器搜索相关说明

当前浏览器搜索流程是：

1. 打开或复用浏览器页面
2. 在浏览器中可视化输入搜索词
3. 提取首屏结果
4. 如果是天气类查询，优先提取天气摘要
5. 必要时继续细化搜索或点击更相关结果页

这意味着项目不是“静默拼 URL”，而是尽量保留真实浏览器搜索过程。

## 14. 常见运行命令

### OpenRouter 文本 + OpenRouter 视觉

```bash
export OPENROUTER_API_KEY="你的 API Key"
export OPENROUTER_TRUST_ENV=true
python3 main.py --text-provider openrouter --vision-provider openrouter
```

### 本地文本 + 本地视觉

```bash
python3 main.py --text-provider local --vision-provider local
```

### OpenRouter 文本 + 本地视觉

```bash
export OPENROUTER_API_KEY="你的 API Key"
python3 main.py --text-provider openrouter --vision-provider local
```

## 15. 常见问题排查

### 15.1 GUI 能启动，但任务执行不稳定

优先检查：

- 是否在 X11 图形桌面中运行
- 是否可以正常截图
- `xdotool` / `xclip` / `wmctrl` 是否安装
- 当前窗口是否允许焦点切换

### 15.2 输入法不能正常输入中文

优先检查：

```bash
echo $XDG_SESSION_TYPE
echo $QT_IM_MODULE
echo $GTK_IM_MODULE
echo $XMODIFIERS
ps aux | grep -E "fcitx|sogou" | grep -v grep
```

推荐目标：

- `XDG_SESSION_TYPE=x11`
- `QT_IM_MODULE=fcitx` 或 `xim`
- `fcitx` 正常运行

### 15.3 OpenRouter 请求失败

如果看到类似：

```text
SSLError / EOF occurred in violation of protocol
```

先排查：

```bash
curl -I https://openrouter.ai/api/v1
env | grep -i proxy
```

如果依赖代理出网：

```bash
export OPENROUTER_TRUST_ENV=true
```

### 15.4 WPS 能打开，但写入不稳定

优先检查：

- WPS 窗口是否真实可见
- 是否正在手动操作 WPS
- 是否有多个 WPS / 表格窗口争抢焦点
- 当前桌面是否允许键盘事件注入

### 15.5 视觉兜底没有截图

优先检查：

- 本地视觉模型是否已准备好
- 本地视觉服务是否正在运行
- `screenshots/` 是否有写权限
- 当前任务是否真的进入了视觉兜底分支

## 16. 运行产物目录

运行过程中常见目录：

- `output/`
  - 日志和执行产物
- `memory/`
  - 记忆和会话数据
- `screenshots/`
  - 视觉兜底截图

这些目录属于运行产物，建议保留，但不要把业务代码放进去。

## 17. 推荐部署步骤（Kylin 2503）

如果你想最快跑起来，建议按这个顺序：

1. 安装系统依赖
2. 安装 Python 和虚拟环境
3. `pip install -r requirements.txt`
4. 安装并确认 WPS、浏览器、fcitx
5. 确认当前是 `X11`
6. 选择模型方案：
   - 只想快：`OpenRouter`
   - 想离线：本地模型
7. 执行：

```bash
python3 main.py
```