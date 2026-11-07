[English](README.md) | 简体中文

# TeXDeck

<p align="center">
  <img src="docs/promo.webp" alt="TeXDeck：放进 PDF，拿回 Beamer" width="100%">
</p>

丢进一份 PDF，拿回一套 Beamer：编译好的 PDF、`.tex` 源码，以及能直接导入 Overleaf 的 zip。

<p align="center">
  <img src="docs/hero.png" alt="左侧是源 PDF 页，右侧是生成的 Beamer 幻灯片" width="100%">
</p>

视觉模型把页面当图片读，按你选的模板写出完整的 `main.tex`，一分钟左右出结果。之后可以用一句话改某一页，也可以改整套。左图的源文件是 [`docs/demo/gold-price-2024.pdf`](docs/demo/gold-price-2024.pdf)，可以自己跑一遍。幻灯片默认用英文，要中文就在附加要求里写一句。

**不需要 API key**：直接用你已经开通的 ChatGPT 或 Claude 订阅，也支持 API key。

## 安装

**桌面版**：从 [Releases](https://github.com/HuanPP/TeXDeck/releases) 下载（macOS Apple 芯片、Windows、Linux），自带 TeX 引擎，不用另装 TeX。安装包没有签名：macOS 上右键选“打开”，Windows 上点“更多信息”，再点“仍要运行”。

**Docker**：

```bash
git clone https://github.com/HuanPP/TeXDeck.git && cd TeXDeck
docker compose up --build        # 然后打开 http://localhost:8000
```

**源码运行**：需要 Python 3.12+、Node 20+，以及 XeLaTeX 或 [Tectonic](https://tectonic-typesetting.github.io)。

```bash
uv venv .venv --python 3.12 && uv pip install -e server --python .venv/bin/python
cp server/.env.example server/.env && ./server/run.sh     # API 在 :8000
cd web && npm install && npm run dev                      # 界面在 :5173
```

## 选模型

模型有三种用法，其中两种直接用你已有的订阅。配置写在 `.env`，桌面版写在数据目录的 `texdeck.env`。

**ChatGPT 订阅**：点顶栏的 **Sign in with ChatGPT**，不用另外配置。

**Claude 订阅**：通过 Claude Code 使用，见[下文](#用-claude-code-走-claude-订阅)。

**API key**：任意 OpenAI 兼容的视觉模型：

```bash
TEXDECK_LLM_API_KEY=...
TEXDECK_LLM_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
TEXDECK_LLM_MODEL=gemini-pro-latest      # 推荐；Kimi 的 vision 模型也能用
```

纯文本模型用不了，因为页面是以图片形式发给模型的。

### 用 Claude Code 走 Claude 订阅

TeXDeck 以无头模式启动你本机的 `claude` CLI（`claude -p`，不开工具，不加载 CLAUDE.md 和插件），用的是这个 CLI 登录的账号，TeXDeck 本身接触不到凭据。先装好 Claude Code，用 `claude auth login` 登录（默认就是 Claude 订阅，可用 `claude auth status` 确认；环境里设了 `ANTHROPIC_API_KEY` 的话 CLI 会改走 API 计费），再设置：

```bash
TEXDECK_LLM_PROVIDER=claude-code
TEXDECK_CLAUDE_CODE_MODEL=sonnet         # 也可以是 opus 或完整模型名
```

运行 TeXDeck 的机器上要有这个 CLI，所以源码运行和桌面版可用，Docker 镜像里不行。

## 模板

<p align="center">
  <img src="docs/templates.png" alt="七套内置模板的标题页" width="100%">
</p>

内置七套模板：Academic、Emerald、Graphite、Navy、Scholar、SimplePlus、Execushares，都支持中文，许可证见 [NOTICE](NOTICE)。

项目里没有提示词库。每套模板的 `main.tex` 都是一套完整的示例幻灯片，模型照着它写。做自己的模板：复制 `templates/academic`，改 `template.json` 里的 `key` 和 `name`，再改 `main.tex`。示例里放真图表，生成结果里就有真图表。

## 须知

模型写出的 TeX 如果经过自动修复仍然编译不过，任务会直接失败并给出真实日志，不会交出半残的幻灯片。点 Regenerate 重跑一般就能成功。页数要求只是参考。输入上限为 10 份 PDF、合计 200 页。

```bash
cd server && ../.venv/bin/python -m pytest      # 含每套模板的真实编译
python desktop/build.py                         # 为本机打包桌面版
```

## 许可证

应用代码采用 MIT。内置主题各自保留原许可证，见 [NOTICE](NOTICE)。
