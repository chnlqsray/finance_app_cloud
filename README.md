# 美股智能投研仪表盘 · US Equity Research Dashboard

一款自动化美股投资研究平台，实时拉取多维财务指标，通过 CrewAI 多智能体流程与 RAG 知识库生成结构化中文投研报告。由本人主导需求与架构，通过与 AI 协作完成开发，已部署至 Streamlit Cloud 与 HuggingFace Spaces，可公开访问。

An automated US equity research platform that fetches real-time financial metrics and generates structured Chinese investment reports through a CrewAI multi-agent workflow and a FAISS-powered RAG knowledge base. Designed and directed by me, implemented through LLM collaboration, and deployed to Streamlit Cloud and HuggingFace Spaces.

🌐 **[Streamlit Cloud](https://my-finance-ai.streamlit.app/)** · **[HuggingFace Spaces](https://huggingface.co/spaces/chnlqsray/finance-dashboard)**

▶ **[YouTube 演示视频](https://youtu.be/oHZ3-8EK-4U)** · **[Bilibili 演示视频](https://www.bilibili.com/video/BV1WScyzDEtX/)**

---

## 产品功能 · Features

**数据层**：实时获取 12 项财务指标，包括 Forward P/E、PEG、ROE、自由现金流等；Forward P/E 优先调用 FMP API，额度耗尽时自动降级至 yfinance，其余指标统一走 yfinance，确保数据可用性。

**Data layer**: Fetches 12 financial metrics in real time, including Forward P/E, PEG, ROE, and free cash flow. Forward P/E is sourced from FMP API with automatic fallback to yfinance on quota exhaustion; all other metrics use yfinance directly.

**多智能体分析**：CrewAI 三角色顺序协作——量化数据分析师负责财务指标解读，全球市场情报专家负责竞争格局与行业动态研究，首席投资官负责综合判断与资金配置建议，最终生成四节式结构化投研报告（资金配置方案、深度持仓逻辑、财务对比表、首席投资官总结）。

**Multi-agent analysis**: CrewAI orchestrates three agents in sequence — a quantitative analyst interprets financial metrics, a market intelligence researcher covers competitive dynamics, and a chief investment officer synthesises the findings into an allocation recommendation. The output is a structured four-section Chinese investment report.

**RAG 知识库**：预置投资哲学书籍向量化检索，支持用户上传 PDF 动态扩充知识库；嵌入引擎采用双保险设计，优先调用 Google Gemini API，失败时自动切换 HuggingFace Inference API，避免本地模型下载导致的内存超限问题。

**RAG knowledge base**: Pre-indexes investment philosophy texts for vector retrieval; users can upload PDFs to extend the knowledge base dynamically. The embedding engine uses a dual-fallback design — Google Gemini API as primary, HuggingFace Inference API as secondary — avoiding local model downloads that would exceed Streamlit Cloud's memory limits.

**搜索外置化**：DuckDuckGo 新闻检索在 `crew.kickoff()` 前由 Python 层预执行完毕，结果作为纯文本注入 task description，LLM 仅负责整理分析，不做工具调用。这是解决 LLM 重复调用工具与提前停止两个稳定性问题的根本方案。

**Externalised search**: DuckDuckGo news retrieval is executed entirely by Python before `crew.kickoff()`, and results are injected into the task description as plain text. The LLM is responsible only for analysis, not tool invocation — this was the definitive fix for two LLM reliability issues: repeated tool calls and premature stopping.

---

## 自动化运维 · Automated Operations

本仓库同时托管应用保活系统（`keep_alive.py` + `keep_alive.yml`），支撑投研仪表盘与电影雷达两款应用在 Streamlit Cloud 和 HuggingFace Spaces 上全天候在线运行。

This repository also hosts the keep-alive system (`keep_alive.py` + `keep_alive.yml`) that keeps both the finance dashboard and movie radar applications running around the clock on Streamlit Cloud and HuggingFace Spaces.

**保活机制 · Keep-alive mechanism**：GitHub Actions 每 6 小时触发（UTC 0/6/12/18 点），启动 Playwright 无头 Chromium 浏览器，依次访问两款 Streamlit 应用与两个 HuggingFace Spaces。针对 Streamlit 休眠拦截页，通过 testid 精确匹配（`wakeup-button-owner` / `wakeup-button-viewer`）加文字匹配双重兜底，自动点击唤醒按钮；针对 HuggingFace 休眠遮罩，匹配"Restart this Space"按钮完成唤醒。每个目标均保存三张截图（加载后、点击后、最终状态）上传至 Actions Artifacts，保留 7 天，供人工验证。

**GitHub Actions triggers at UTC 0/6/12/18**: Launches a headless Chromium browser via Playwright, visiting both Streamlit apps and both HuggingFace Spaces in sequence. For Streamlit sleep interception pages, wake-up buttons are detected via testid (owner/viewer variants) with text-match fallback; for HuggingFace sleep overlays, the "Restart this Space" button is matched. Three screenshots per target (post-load, post-click, final state) are uploaded to Actions Artifacts with a 7-day retention for audit.

**Chromium 缓存 · Chromium caching**：通过 `actions/cache` 对 Playwright Chromium 二进制文件（~200MB）进行版本级缓存，缓存命中时跳过下载，显著缩短 job 运行时间。

**Chromium caching**: Uses `actions/cache` to cache the Playwright Chromium binary (~200 MB) by version, skipping downloads on cache hit and significantly reducing job runtime.

---

## 技术栈 · Tech Stack

| 类别 | 依赖 |
|------|------|
| 界面框架 | Streamlit |
| 多智能体编排 | CrewAI |
| LLM 接入层 | langchain-openai (ChatOpenAI → Groq API, llama-3.3-70b-versatile) |
| 向量检索 | FAISS |
| 嵌入引擎 | Google Gemini API / HuggingFace Inference API（双保险） |
| 数据源 | FMP API, yfinance, DuckDuckGo Search |
| 数据处理 | Pandas, Altair |
| 自动化运维 | GitHub Actions, Playwright (Chromium) |

---

## 部署配置 · Deployment

本项目部署于 Streamlit Cloud 与 HuggingFace Spaces。运行需在 `.streamlit/secrets.toml` 中配置以下密钥：

This project is deployed on Streamlit Cloud and HuggingFace Spaces. The following API keys must be configured in `.streamlit/secrets.toml`:

```toml
GROQ_API_KEY     = "your_groq_api_key"
GEMINI_API_KEY   = "your_gemini_api_key"
HF_TOKEN         = "your_huggingface_token"
FMP_API_KEY      = "your_fmp_api_key"   # 可选，缺失时自动降级 yfinance
```

---

## 工程决策记录 · Engineering Notes

**为什么用 ChatOpenAI 而非 ChatGroq 或 CrewAI LLM？**

经过多轮调试验证：ChatOpenAI 是纯 LangChain 对象，CrewAI 直接调用其 `.invoke()`，完全不经过 LiteLLM 路由层。ChatGroq、`CrewAI LLM("groq/...")` 等方案均因 LiteLLM 路由层的各类兼容性问题而失败。最终方案镜像本地 Ollama 成功模式：`ChatOpenAI(base_url=Groq_URL, api_key=GROQ_KEY)`。

**Why ChatOpenAI instead of ChatGroq or CrewAI LLM?**

Extensive debugging confirmed that ChatOpenAI is a pure LangChain object invoked directly by CrewAI, completely bypassing the LiteLLM routing layer. ChatGroq and `CrewAI LLM("groq/...")` both failed due to various LiteLLM compatibility issues. The final solution mirrors the local Ollama approach: `ChatOpenAI(base_url=Groq_URL, api_key=GROQ_KEY)`.

---

*Independently designed and delivered · 2025–2026*
