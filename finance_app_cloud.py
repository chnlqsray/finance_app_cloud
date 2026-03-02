"""
美股投资仪表盘 - 使用 Streamlit 展示股票关键指标与走势
数据来源：Yahoo Finance（爬虫 + yfinance API + 手动计算，三重保障）
集成 CrewAI 多角色 AI 团队：数据分析师、情报研究员、投资总监
RAG 知识库（FAISS + 双保险嵌入引擎）、报告下载

【云端适配版 for Streamlit Cloud】
- LLM：Groq API（llama-3.3-70b-versatile），从 st.secrets 读取密钥
- 嵌入引擎双保险：① GoogleGenerativeAIEmbeddings（首选，API调用无本地下载）
                   → 失败时自动切换 ② HuggingFaceInferenceAPIEmbeddings（备选）
- 彻底移除本地 Ollama 依赖（127.0.0.1:11434）
- 所有 API 密钥统一从 .streamlit/secrets.toml 读取
"""

# =============================================================================
# 【signal 兼容补丁】必须在所有 import 之前执行
# Streamlit 在非主线程运行回调，CrewAI 遥测模块尝试注册 SIGTERM/SIGINT 会
# 抛出 ValueError: signal only works in main thread。打补丁静默忽略。
# =============================================================================
import signal as _signal_module
_original_signal = _signal_module.signal

def _safe_signal(sig, handler):
    try:
        return _original_signal(sig, handler)
    except (ValueError, OSError):
        pass

_signal_module.signal = _safe_signal

import os
# 禁用 CrewAI / OpenTelemetry 遥测，彻底规避信号注册问题
os.environ["OTEL_SDK_DISABLED"] = "true"
os.environ["CREWAI_TELEMETRY_OPT_OUT"] = "true"
# =============================================================================
# 【LLM 兼容层 v8 — ChatOpenAI + Groq 兼容接口（镜像本地版成功方案）】
#
# 完整错误历史：
#   v1-v3: dummy OPENAI_API_KEY → LiteLLM 拿假 key 真发 OpenAI 请求 → 401
#   v4:    ChatGroq + dummy key → CrewAI 内部组件误用假 key → 401
#   v5:    CrewAI LLM("groq/...") → LiteLLM Groq provider 缺失 → Fallback not available
#   v6:    ChatGroq + pop(OPENAI_API_KEY) → LiteLLM import 存在性检查 → OPENAI_API_KEY is required
#   v7:    CrewLLM("openai/...") → 仍走 LiteLLM 路由 → Fallback not available
#
# v8 根本解（镜像本地 finance_app_local.py 成功运行的方案）：
#   本地版用法：OPENAI_API_KEY="ollama" + OPENAI_API_BASE=Ollama + ChatOpenAI(base_url=Ollama)
#   云端版用法：OPENAI_API_KEY=Groq key（占位，runtime 替换）+
#              OPENAI_API_BASE=Groq url + ChatOpenAI(base_url=Groq)
#
#   核心原理：ChatOpenAI 是纯 LangChain 对象，CrewAI 直接调用其 .invoke()，
#   完全不经过 LiteLLM 路由层。OPENAI_API_KEY 有值只是为了通过 LiteLLM import
#   阶段的存在性检查；OPENAI_API_BASE 确保即使内部组件用 env var 发请求也打到 Groq。
# =============================================================================
# 【模块级占位】LiteLLM import 阶段会检查 OPENAI_API_KEY 是否存在（不检查有效性）。
# 若不存在则抛 "OPENAI_API_KEY is required"，连运行都到不了。
# 此处设占位符，在 _make_groq_llm() 中替换为真实 Groq key，并重定向 base URL。
os.environ.setdefault("OPENAI_API_KEY",  "placeholder-will-be-replaced-by-groq-key")
os.environ.setdefault("OPENAI_API_BASE", "https://api.groq.com/openai/v1")
os.environ.setdefault("OPENAI_BASE_URL", "https://api.groq.com/openai/v1")  # openai SDK >= 1.x 读此变量

import io
import re
import sys
import logging
import tempfile
import glob
import requests
from datetime import datetime, timezone
from bs4 import BeautifulSoup

import streamlit as st
import pandas as pd
import yfinance as yf
import altair as alt

from crewai import Agent, Task, Crew, Process
from langchain_openai import ChatOpenAI  # 镜像本地版做法：ChatOpenAI 是纯 LangChain 对象，CrewAI 直接调用，完全不经过 LiteLLM 路由
from crewai.tools import BaseTool
from langchain_community.tools import DuckDuckGoSearchRun
from pydantic import BaseModel, Field
from typing import Type

# Windows 控制台 UTF-8
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

# =============================================================================
# 【设计决策记录】为什么不让 AI 自己调用 DuckDuckGo 搜索？
# =============================================================================
# 经过实测，让 LLM（llama3.2）通过 CrewAI 工具调用机制自行执行搜索存在两个无法
# 绕过的缺陷：
#   1. 重复调用：llama3.2 会对同一个 query 反复调用工具直到耗尽 max_iter 预算，
#      所有基于"返回文字信号阻止重复"的方案均已验证无效。
#   2. 提前停止：llama3.2 只要自认为"信息充足"就会中断搜索、直接输出报告，
#      导致实际只搜了 2 条就停止（计划是 6 条）。
#
# 最终采用的可靠方案：搜索完全由 Python 层在 crew.kickoff() 前执行完毕，
# 结果作为纯文本直接注入 task 的 description，LLM 仅负责翻译整理，不做任何工具调用。
# 这是 run_crewai_analysis() 函数开头 "_pre_search_block" 的由来。
# =============================================================================

_ddg_runner = DuckDuckGoSearchRun()

# 预定义的6条搜索计划：(公司标识, 搜索词)
_PLANNED_SEARCHES: list = [
    ("META",  "Meta Platforms latest news 2026"),
    ("META",  "Meta Platforms competitive advantage moat 2026"),
    ("AMZN",  "Amazon AWS latest news 2026"),
    ("AMZN",  "Amazon competitive advantage moat 2026"),
    ("GOOG",  "Google Alphabet latest news 2026"),
    ("GOOG",  "Google Alphabet competitive moat AI 2026"),
]


# =============================================================================
# 【云端配置】从 st.secrets 读取 API 密钥
# =============================================================================

def _get_secret(key: str) -> str:
    """安全读取 st.secrets，缺失时返回空字符串而不是抛异常。"""
    try:
        return st.secrets[key]
    except Exception:
        return ""

GROQ_API_KEY    = _get_secret("GROQ_API_KEY")
GEMINI_API_KEY  = _get_secret("GEMINI_API_KEY")
HF_TOKEN        = _get_secret("HF_TOKEN")

# Groq API 状态（用于侧边栏指示灯）
_GROQ_READY   = bool(GROQ_API_KEY)
_GEMINI_READY = bool(GEMINI_API_KEY)
_HF_READY     = bool(HF_TOKEN)


# =============================================================================
# 【云端 LLM 配置】ChatOpenAI + Groq 兼容接口（镜像本地版方案）
# =============================================================================
# 架构变化说明：
# - 原来：logic_llm = deepseek-r1:7b（Ollama）/ tool_llm = llama3.2（Ollama）
# - 现在：所有 Agent 统一使用 llama-3.3-70b-versatile（Groq API）
#   理由：70B 模型指令遵循能力远优于本地 3B/7B，无需分开用两个模型
#   超时时间从 300s 缩短到 90s：Groq 推理速度极快，通常 5-15 秒完成

def _make_groq_llm():
    """
    【v8 最终方案】ChatOpenAI 指向 Groq 兼容接口。

    镜像本地版（finance_app_local.py）的成功模式：
      本地：ChatOpenAI(base_url="http://127.0.0.1:11434/v1", api_key="ollama")
      云端：ChatOpenAI(base_url="https://api.groq.com/openai/v1", api_key=GROQ_KEY)

    为什么 ChatOpenAI 而非其他方案？
    - ChatOpenAI 是纯 LangChain 对象，CrewAI 直接调用其 .invoke()
    - 完全不经过 LiteLLM 路由层，与 LiteLLM 是否有 Groq provider 完全无关
    - 本地版已验证此方案稳定运行，云端仅需换 base_url 和 api_key
    """
    if not GROQ_API_KEY:
        return None
    # 将真实 Groq key 写入环境变量：
    # ① 替换模块级占位符，确保 LiteLLM 存在性检查通过
    # ② 重定向 OPENAI_API_BASE，任何内部组件走 env var 发请求也只会打到 Groq
    os.environ["OPENAI_API_KEY"]  = GROQ_API_KEY
    os.environ["OPENAI_API_BASE"] = "https://api.groq.com/openai/v1"
    os.environ["OPENAI_BASE_URL"] = "https://api.groq.com/openai/v1"  # openai SDK >= 1.x 读此变量
    os.environ["GROQ_API_KEY"]    = GROQ_API_KEY
    return ChatOpenAI(
        model="llama-3.3-70b-versatile",
        base_url="https://api.groq.com/openai/v1",
        api_key=GROQ_API_KEY,
        temperature=0.1,
        max_retries=2,
    )


# =============================================================================
# 【双保险嵌入引擎】Google Gemini API → HuggingFace Inference API
# =============================================================================
# 为什么不用 HuggingFaceEmbeddings（本地下载）？
# - HuggingFaceEmbeddings 会把模型文件下载到容器本地（约 470MB）
# - Streamlit Cloud 免费容器内存 1GB，首次冷启动下载模型极易超时或爆内存
# - 改用 API 方式：嵌入计算在远端服务器完成，本地零内存占用

def get_embedding_function(engine_choice: str = "auto"):
    """
    嵌入引擎，支持用户手动选择：
    engine_choice:
      "auto"       → 优先 Gemini，失败自动切换 HF
      "gemini"     → 强制 Gemini，失败则报错（不切换）
      "huggingface"→ 强制 HuggingFace，跳过 Gemini
    """
    import time

    class _RateLimitedGeminiEmbeddings:
        """限速包装器：每 chunk 间隔 0.65s + 429 指数退避重试。"""
        _RETRY_DELAYS = [3, 6, 15, 30, 60]

        def __init__(self, base_embeddings):
            self._base = base_embeddings

        def _call_with_retry(self, fn, *args, **kwargs):
            for attempt, wait in enumerate(self._RETRY_DELAYS, 1):
                try:
                    return fn(*args, **kwargs)
                except Exception as e:
                    err_str = str(e).upper()
                    is_quota = any(k in err_str for k in ("429", "RESOURCE_EXHAUSTED", "QUOTA", "RATE"))
                    if is_quota and attempt < len(self._RETRY_DELAYS):
                        time.sleep(wait)
                    else:
                        raise

        def embed_query(self, text: str) -> list:
            return self._call_with_retry(self._base.embed_query, text)

        def embed_documents(self, texts: list) -> list:
            results = []
            for i, text in enumerate(texts):
                if i > 0:
                    time.sleep(0.65)
                results.append(self._call_with_retry(self._base.embed_query, text))
            return results

    use_gemini = engine_choice in ("auto", "gemini")
    use_hf     = engine_choice in ("auto", "huggingface")

    # ── Google Gemini ──────────────────────────────────────────────────────
    if use_gemini and GEMINI_API_KEY:
        try:
            from langchain_google_genai import GoogleGenerativeAIEmbeddings

            # 使用静态优先级列表替代 google.generativeai 动态发现（消除废弃包警告）。
            # google.generativeai 已停止更新，官方建议迁移至 google.genai；
            # 但 langchain_google_genai.GoogleGenerativeAIEmbeddings 内部仍依赖旧包，
            # 此处至少消除我们自己的直接 import。
            # 模型优先级：gemini-embedding-001（最新推荐）→ text-embedding-004 → embedding-001
            _GEMINI_EMBED_MODELS = [
                "models/gemini-embedding-001",
                "models/text-embedding-004",
                "models/embedding-001",
            ]
            target_model = None
            last_exc = None
            for _candidate in _GEMINI_EMBED_MODELS:
                try:
                    _probe = GoogleGenerativeAIEmbeddings(
                        model=_candidate,
                        google_api_key=GEMINI_API_KEY,
                        transport="rest",
                        task_type="retrieval_document",
                    )
                    _probe.embed_query("probe")   # 实际发请求，确认模型可用
                    target_model = _candidate
                    base_emb = _probe
                    break
                except Exception as _e:
                    last_exc = _e
                    continue
            if target_model is None:
                raise RuntimeError(f"所有 Gemini 嵌入模型均不可用，最后错误：{last_exc}")
            wrapped = _RateLimitedGeminiEmbeddings(base_emb)
            wrapped.embed_query("test")
            return wrapped, f"Google Gemini ({target_model})，限速保护已启用"
        except Exception as e:
            msg = f"⚠️ Google Gemini 嵌入失败：{e}"
            if engine_choice == "gemini":
                st.sidebar.error(msg + "（已强制选择 Gemini，不会切换备选）")
                return None, "不可用"
            st.sidebar.warning(msg + "，自动切换 HuggingFace")

    # ── HuggingFace Inference API ──────────────────────────────────────────
    # 使用 BAAI/bge-m3：支持中英文跨语言检索（8192 token 上限），
    # 优于 all-MiniLM-L6-v2（仅 256 token 且不支持中文查询）。
    if use_hf and HF_TOKEN:
        try:
            from langchain_huggingface import HuggingFaceEndpointEmbeddings
            embeddings = HuggingFaceEndpointEmbeddings(
                model="BAAI/bge-m3",
                huggingfacehub_api_token=HF_TOKEN,
                # timeout 参数已移除：HuggingFaceEndpointEmbeddings 当前版本
                # Pydantic schema 不接受此参数（extra_forbidden），传入会直接报错
            )
            embeddings.embed_query("test")
            return embeddings, "HuggingFace Inference API (BAAI/bge-m3)"
        except Exception as e:
            st.sidebar.warning(f"⚠️ HuggingFace 嵌入引擎失败：{e}")

    return None, "不可用"


# =============================================================================
# 【整合自 crawler.py】Yahoo Finance Key Statistics 爬虫
# =============================================================================

_CRAWLER_BASE_URLS = [
    "https://uk.finance.yahoo.com/quote/{ticker}/key-statistics/",
    "https://finance.yahoo.com/quote/{ticker}/key-statistics/",
]

_CRAWLER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

_LABEL_FORWARD_PE = "Forward P/E (current)"
_LABEL_PEG = "PEG Ratio (5 yr expected)"


def _get_value_from_sibling_or_cell(soup: BeautifulSoup, label: str) -> str:
    """在 HTML 中精准定位标签文本，并取相邻/同行的数值。"""
    for tr in soup.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        first_text = (tds[0].get_text() or "").strip()
        if label in first_text or first_text == label:
            val = (tds[1].get_text() or "").strip()
            if val and val != "--":
                return val
    for tag in soup.find_all(string=re.compile(re.escape(label), re.I)):
        parent = tag.parent if hasattr(tag, "parent") else None
        if not parent:
            continue
        if parent.name == "td":
            row = parent.find_parent("tr")
            if row:
                cells = row.find_all("td")
                for i, c in enumerate(cells):
                    if c == parent and i + 1 < len(cells):
                        val = (cells[i + 1].get_text() or "").strip()
                        if val and val != "--":
                            return val
        nxt = parent.find_next_sibling()
        if nxt:
            val = (nxt.get_text() or "").strip()
            if val and val != "--" and re.search(r"[\d.,]+", val):
                return val
    return "N/A"


def _get_value_from_json(html: str, keys: list) -> str:
    """从页面内嵌 JSON 中按 key 提取 raw 或 fmt 数值。"""
    try:
        for key in keys:
            m = re.search(rf'"{key}"\s*:\s*{{\s*"raw"\s*:\s*([0-9.]+)', html)
            if m:
                return m.group(1)
            m = re.search(rf'"{key}"\s*:\s*{{\s*"fmt"\s*:\s*"([^"]+)"', html)
            if m:
                return m.group(1)
    except Exception:
        pass
    return "N/A"


def _get_forward_pe_from_html_regex(html: str) -> str:
    """从 HTML 中按标签文本或 JSON 键名兜底提取 Forward P/E。"""
    try:
        for key in ("forwardPE", "priceEpsCurrentYear"):
            block = re.search(rf'"{key}"\s*:\s*\{{[^}}]{{0,80}}}}', html)
            if block:
                sub = block.group(0)
                m = re.search(r'"raw"\s*:\s*([0-9.]+)', sub)
                if m:
                    return m.group(1)
                m = re.search(r'"fmt"\s*:\s*"([0-9.]+)"', sub)
                if m:
                    return m.group(1)
        sd = re.search(r'"summaryDetail"\s*:\s*\{', html)
        if sd:
            start = sd.end()
            end = min(start + 2500, len(html))
            sub = html[start:end]
            m = re.search(r'"forwardPE"\s*:\s*\{\s*"raw"\s*:\s*([0-9.]+)', sub)
            if m:
                return m.group(1)
            m = re.search(r'"forwardPE"\s*:\s*\{\s*"fmt"\s*:\s*"([0-9.]+)"', sub)
            if m:
                return m.group(1)
        m = re.search(
            r"Forward\s*P/?\s*E\s*\(current\)[\s\S]{0,300}?([0-9]+[.,]?[0-9]*)",
            html, re.IGNORECASE,
        )
        if m:
            return m.group(1).replace(",", ".")
        m = re.search(
            r"Forward\s*P/E\s*\(current\)[\s\S]{0,200}?([0-9]+\.[0-9]+)",
            html, re.IGNORECASE,
        )
        if m:
            return m.group(1)
    except Exception:
        pass
    return "N/A"


def _get_peg_from_html_regex(html: str) -> str:
    """从 HTML 中按标签文本兜底提取 PEG Ratio。"""
    try:
        m = re.search(
            r"PEG\s*Ratio\s*\(5\s*yr\s*expected\)[\s\S]{0,300}?([0-9]+[.,]?[0-9]*)",
            html, re.IGNORECASE,
        )
        if m:
            return m.group(1).replace(",", ".")
        m = re.search(
            r"PEG\s*Ratio[\s\S]{0,200}?([0-9]+\.[0-9]+)",
            html, re.IGNORECASE,
        )
        if m:
            return m.group(1)
    except Exception:
        pass
    return "N/A"


def fetch_key_stats(ticker: str) -> tuple:
    """
    请求 Key Statistics 页，用 BeautifulSoup 解析 HTML，提取 Forward P/E 与 PEG Ratio。
    先试 UK 再试 US 域名；若未找到则返回 'N/A'，不抛异常。
    """
    forward_pe, peg_ratio = "N/A", "N/A"
    for base_url in _CRAWLER_BASE_URLS:
        url = base_url.format(ticker=ticker)
        try:
            resp = requests.get(url, headers=_CRAWLER_HEADERS, timeout=15)
            resp.raise_for_status()
            html = resp.text
            soup = BeautifulSoup(html, "html.parser")

            fp = _get_value_from_sibling_or_cell(soup, _LABEL_FORWARD_PE)
            pr = _get_value_from_sibling_or_cell(soup, _LABEL_PEG)
            if fp != "N/A":
                forward_pe = fp
            if pr != "N/A":
                peg_ratio = pr

            if forward_pe == "N/A":
                for script in soup.find_all("script"):
                    src = script.string or ""
                    if "forwardPE" in src or "priceEpsCurrentYear" in src:
                        forward_pe = _get_value_from_json(src, ["forwardPE", "priceEpsCurrentYear"])
                        if forward_pe != "N/A":
                            break
                if forward_pe == "N/A":
                    forward_pe = _get_value_from_json(html, ["forwardPE", "priceEpsCurrentYear"])
            if peg_ratio == "N/A":
                for script in soup.find_all("script"):
                    src = script.string or ""
                    if "pegRatio" in src:
                        peg_ratio = _get_value_from_json(src, ["pegRatio"])
                        if peg_ratio != "N/A":
                            break
                if peg_ratio == "N/A":
                    peg_ratio = _get_value_from_json(html, ["pegRatio"])

            if forward_pe == "N/A":
                forward_pe = _get_forward_pe_from_html_regex(html)
            if peg_ratio == "N/A":
                peg_ratio = _get_peg_from_html_regex(html)

            if forward_pe != "N/A" and peg_ratio != "N/A":
                break
        except Exception:
            continue
    return forward_pe, peg_ratio


# =============================================================================
# 实时日志流：将 CrewAI verbose 输出重定向到 Streamlit placeholder
# =============================================================================

class StreamToStreamlit(io.StringIO):
    """将 sys.stdout / sys.stderr 写入操作实时渲染到 Streamlit placeholder。"""
    def __init__(self, placeholder):
        super().__init__()
        self.placeholder = placeholder
        self.ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')

    _NOISE_PREFIXES = (
        "Error in engine", "Impersonate", "response: http",
        "connect_tcp", "send_request", "receive_response",
    )

    def write(self, s: str):
        clean_text = self.ansi_escape.sub('', s)
        lines = clean_text.splitlines(keepends=True)
        filtered = "".join(
            line for line in lines
            if not any(line.lstrip().startswith(p) for p in self._NOISE_PREFIXES)
        )
        if not filtered:
            return len(s)
        super().write(filtered)
        try:
            self.placeholder.code(self.getvalue())
        except Exception:
            pass
        return len(s)

    def flush(self):
        pass


class _StreamlitLogHandler(logging.Handler):
    """将 Python logging 输出重定向到 StreamToStreamlit。"""
    def __init__(self, stream: StreamToStreamlit):
        super().__init__()
        self.stream = stream
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord):
        try:
            self.stream.write(self.format(record) + "\n")
        except Exception:
            pass


# =============================================================================
# 配置与常量
# =============================================================================
st.set_page_config(page_title="美股仪表盘", page_icon="📈", layout="wide")

PRESET_TICKERS = ["META", "AMZN", "GOOG"]
OPTION_TICKERS = ["META", "AMZN", "GOOG", "FTEC", "QQQ", "VOO", "NVDA", "MSFT", "AAPL"]
CACHE_TTL = 300
PERIOD_OPTIONS = [
    ("近 1 天", "1d"),
    ("近 5 天", "5d"),
    ("近 1 月", "1mo"),
    ("近 6 月", "6mo"),
    ("近 1 年", "1y"),
]

# =============================================================================
# 【yfinance 云端适配】自定义 requests.Session，注入浏览器 User-Agent
# =============================================================================
# Yahoo Finance 会封锁 AWS/GCP 等云端数据中心 IP，yfinance 直接请求时静默返回空数据。
# 解决方案：伪装成普通浏览器发起请求，绕过云端 IP 检测。
# _yf_session 在模块级创建，统一复用，避免重复建立连接。
# =============================================================================
import requests as _requests_mod
_yf_session = _requests_mod.Session()
_yf_session.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
})


# =============================================================================
# RAG 模块：构建 FAISS 向量库（双保险嵌入引擎）
# =============================================================================

def build_rag_vectorstore(uploaded_pdf_files=None, engine_choice: str = "auto"):
    """
    构建 RAG 向量库：
    1. 静态读取 ./knowledge_base/*.pdf
    2. 动态读取 Streamlit 上传的 PDF 文件
    合并后切分，用双保险嵌入引擎向量化，存入 FAISS 内存库。
    """
    try:
        from langchain_community.document_loaders import PyPDFLoader
        try:
            from langchain_text_splitters import RecursiveCharacterTextSplitter
        except ImportError:
            from langchain.text_splitter import RecursiveCharacterTextSplitter
        from langchain_community.vectorstores import FAISS
    except ImportError as e:
        st.warning(f"RAG 依赖库未安装，知识库功能已跳过：{e}")
        return None

    all_docs = []

    # 静态知识库已停用：大型研报 PDF（100-300页）会产生 500-1000+ chunks，
    # 超出 Gemini 免费层日限额。现仅支持网页上传精简财报（建议每份 ≤30页，共 ≤60页）。
    # 若需恢复静态加载，可取消下方注释。
    # kb_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "knowledge_base")
    # if os.path.isdir(kb_dir):
    #     for pdf_path in glob.glob(os.path.join(kb_dir, "*.pdf")):
    #         try:
    #             all_docs.extend(PyPDFLoader(pdf_path).load())
    #         except Exception as e:
    #             st.warning(f"读取静态 PDF 失败（{os.path.basename(pdf_path)}）：{e}")

    # 动态读取（仅上传文件）
    if uploaded_pdf_files:
        for uploaded_file in uploaded_pdf_files:
            try:
                with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp:
                    tmp.write(uploaded_file.read())
                    tmp_path = tmp.name
                all_docs.extend(PyPDFLoader(tmp_path).load())
                os.unlink(tmp_path)
            except Exception as e:
                st.warning(f"读取上传 PDF 失败（{uploaded_file.name}）：{e}")

    if not all_docs:
        return None

    splitter = RecursiveCharacterTextSplitter(chunk_size=800, chunk_overlap=80)
    chunks = splitter.split_documents(all_docs)
    if not chunks:
        return None

    # ── 双保险嵌入引擎 ─────────────────────────────────────────────────────
    embeddings, embed_source = get_embedding_function(engine_choice=engine_choice)
    if embeddings is None:
        st.warning(
            "⚠️ 嵌入引擎不可用（GEMINI_API_KEY 与 HF_TOKEN 均未配置或均失败），"
            "知识库功能已跳过。请在 secrets.toml 中配置至少一个密钥。"
        )
        return None

    st.sidebar.caption(f"🔌 嵌入引擎：{embed_source}")

    # Google Gemini 免费层限速保护：每 chunk 间隔 0.65s，chunk 数量较多时需要等待
    # chunk_size=800 相比原来的 500 减少约 37% 的 chunk 数量，降低 RPD 消耗
    # 例：30 个 chunk 约需 20s，60 个 chunk 约需 40s，属正常现象
    is_gemini = "Google Gemini" in embed_source
    chunk_count = len(chunks)
    if is_gemini and chunk_count > 10:
        est_sec = int(chunk_count * 0.65)
        st.sidebar.info(
            f"⏳ Google Gemini 免费层限速保护已启用（{chunk_count} 个 chunk，"
            f"预计约 {est_sec}s）。请耐心等待，勿重复点击。"
        )

    try:
        vectorstore = FAISS.from_documents(chunks, embeddings)
    except Exception as e:
        # ── Gemini 构建过程中额度耗尽，自动降级到 HuggingFace ──────────────
        err_str = str(e).upper()
        is_quota_error = any(k in err_str for k in ("429", "QUOTA", "RESOURCE_EXHAUSTED", "RATE"))
        if is_gemini and is_quota_error and HF_TOKEN:
            st.sidebar.warning(
                f"⚠️ Gemini 额度在构建过程中耗尽，自动切换 HuggingFace 重新构建知识库…"
            )
            fallback_emb, fallback_src = get_embedding_function(engine_choice="huggingface")
            if fallback_emb is None:
                st.warning("⚠️ HuggingFace 降级也失败，知识库构建中止。")
                return None
            st.sidebar.caption(f"🔌 嵌入引擎（降级）：{fallback_src}")
            try:
                vectorstore = FAISS.from_documents(chunks, fallback_emb)
            except Exception as e2:
                st.warning(f"⚠️ 知识库构建失败（HuggingFace 降级后仍出错）：{e2}")
                return None
        else:
            st.warning(f"⚠️ 知识库构建失败：{e}")
            return None
    return vectorstore


# =============================================================================
# RAG 向量库（session_state 持久化）
# =============================================================================
if "rag_vectorstore" not in st.session_state:
    st.session_state["rag_vectorstore"] = None


class _RAGInput(BaseModel):
    query: str = Field(
        default="",
        description="投资相关查询词，支持中英文。",
    )


class RAGSearchTool(BaseTool):
    name: str = "search_investment_knowledge"
    description: str = (
        "在本地投资知识库（研报/财报/投资经典）中检索与 query 最相关的段落，"
        "返回 top-4 文档块合并后的文本。"
    )
    args_schema: Type[BaseModel] = _RAGInput

    def _run(self, query: str = "") -> str:
        vs = st.session_state.get("rag_vectorstore")
        if vs is None:
            return "当前无可用参考资料（知识库为空，请先构建知识库）。"
        if not query:
            return "查询词为空。"
        try:
            docs = vs.similarity_search(query, k=4)
            if not docs:
                return "知识库中未检索到相关内容。"
            return "\n\n---\n\n".join(
                f"【来源：{d.metadata.get('source', '未知')} "
                f"第{d.metadata.get('page', '?') + 1}页】\n{d.page_content}"
                for d in docs
            )
        except Exception as e:
            return f"检索过程中出错：{e}"


# =============================================================================
# 辅助函数
# =============================================================================

def parse_custom_tickers(text: str) -> list:
    if not text or not text.strip():
        return []
    parts = text.replace(",", " ").split()
    return list(dict.fromkeys(s.strip().upper() for s in parts if s.strip()))


def _parse_metric(s: str):
    if not s or s == "N/A":
        return None
    try:
        return float(str(s).replace(",", "."))
    except (ValueError, TypeError):
        return None


def _safe_float(val, default=None):
    if val is None:
        return default
    try:
        return float(val)
    except (TypeError, ValueError):
        return default


def _format_fcf_billions(fcf):
    v = _safe_float(fcf)
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    b = v / 1e9
    return f"{b:.2f}" if abs(b) >= 0.01 else f"{b:.4f}"


@st.cache_data(ttl=CACHE_TTL)
def get_stock_metrics(ticker: str) -> dict:
    """
    三重保障获取 Forward P/E 与 PEG Ratio。
    第一优先级：爬虫（BeautifulSoup）→ 'Web Scraper'
    第二优先级：yfinance API → 'yfinance API'
    第三优先级（仅 Forward P/E）：currentPrice / forwardEps → 'Calculated'
    """
    fpe_str, peg_str = fetch_key_stats(ticker)
    forward_pe = _parse_metric(fpe_str)
    peg = _parse_metric(peg_str)
    source_fpe = "Web Scraper" if forward_pe is not None else None
    source_peg = "Web Scraper" if peg is not None else None

    info = None
    if forward_pe is None or peg is None:
        try:
            info = yf.Ticker(ticker, session=_yf_session).info
        except Exception:
            info = {}

    if forward_pe is None and info:
        raw = info.get("forwardPE") or info.get("priceEpsCurrentYear")
        if raw is not None:
            try:
                forward_pe = round(float(raw), 2)
                source_fpe = "yfinance API"
            except (TypeError, ValueError):
                pass
    if forward_pe is None and info:
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        forward_eps = info.get("forwardEps")
        if price is not None and forward_eps is not None and forward_eps != 0:
            try:
                forward_pe = round(price / float(forward_eps), 2)
                source_fpe = "Calculated"
            except (TypeError, ValueError):
                pass
    if source_fpe is None:
        source_fpe = "N/A"

    if peg is None and info:
        raw = info.get("pegRatio") or info.get("trailingPegRatio")
        if raw is not None:
            try:
                peg = round(float(raw), 2)
                source_peg = "yfinance API"
            except (TypeError, ValueError):
                pass
    if source_peg is None:
        source_peg = "N/A"

    return {
        "forward_pe": forward_pe,
        "peg": peg,
        "source_forward_pe": source_fpe,
        "source_peg": source_peg,
    }


def get_one_stock_row(ticker: str) -> dict:
    try:
        metrics = get_stock_metrics(ticker)
        stock = yf.Ticker(ticker, session=_yf_session)
        info = stock.info
        price = info.get("currentPrice") or info.get("regularMarketPrice")
        prev_close = info.get("previousClose")
        daily_pct = (
            round((price - prev_close) / prev_close * 100, 2)
            if price is not None and prev_close and prev_close != 0
            else None
        )
        roe_raw = _safe_float(info.get("returnOnEquity"))
        roe_pct = round(roe_raw * 100, 2) if roe_raw is not None else None
        om_raw = info.get("operatingMargins")
        if isinstance(om_raw, dict):
            om_raw = list(om_raw.values())[0] if om_raw else None
        om_pct = round(_safe_float(om_raw) * 100, 2) if _safe_float(om_raw) is not None else None
        data_source = f"FPE: {metrics['source_forward_pe']} | PEG: {metrics['source_peg']}"
        return {
            "股票代码": ticker,
            "最新价 (USD)": round(price, 2) if price is not None else None,
            "日涨跌幅 (%)": daily_pct,
            "Forward P/E": metrics["forward_pe"],
            "PEG Ratio (5yr)": metrics["peg"],
            "P/B": round(_safe_float(info.get("priceToBook")), 2) if _safe_float(info.get("priceToBook")) is not None else None,
            "ROE (%)": roe_pct,
            "Operating Margin (%)": om_pct,
            "EPS (Trailing) ($)": round(_safe_float(info.get("trailingEps")), 2) if _safe_float(info.get("trailingEps")) is not None else None,
            "D/E (%)": round(_safe_float(info.get("debtToEquity")), 2) if _safe_float(info.get("debtToEquity")) is not None else None,
            "FCF (B)": _format_fcf_billions(info.get("freeCashflow")),
            "Current Ratio": round(_safe_float(info.get("currentRatio")), 2) if _safe_float(info.get("currentRatio")) is not None else None,
            "数据来源": data_source,
            "_info": info,
        }
    except Exception:
        return {
            "股票代码": ticker,
            "最新价 (USD)": None, "日涨跌幅 (%)": None,
            "Forward P/E": None, "PEG Ratio (5yr)": None, "P/B": None,
            "ROE (%)": None, "Operating Margin (%)": None, "EPS (Trailing) ($)": None,
            "D/E (%)": None, "FCF (B)": None, "Current Ratio": None,
            "数据来源": "—", "_info": {},
        }


@st.cache_data(ttl=CACHE_TTL)
def fetch_stock_data(tickers: list) -> tuple:
    rows = [get_one_stock_row(t) for t in tickers]
    for r in rows:
        r.pop("_info", None)
    col_order = [
        "股票代码", "最新价 (USD)", "日涨跌幅 (%)",
        "Forward P/E", "PEG Ratio (5yr)", "P/B",
        "ROE (%)", "Operating Margin (%)", "EPS (Trailing) ($)",
        "D/E (%)", "FCF (B)", "Current Ratio", "数据来源",
    ]
    df = pd.DataFrame(rows)[col_order]
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    return df, ts


@st.cache_data(ttl=CACHE_TTL)
def fetch_history(tickers: list, period: str) -> pd.DataFrame:
    if not tickers:
        return pd.DataFrame()
    try:
        hist = yf.download(
            tickers, period=period, auto_adjust=True,
            progress=False, group_by="ticker", threads=False,
            session=_yf_session,
        )
        if hist.empty:
            return pd.DataFrame()
        if isinstance(hist.columns, pd.MultiIndex):
            close = hist.xs("Close", axis=1, level=1).copy()
        else:
            close = hist[["Close"]].copy() if "Close" in hist.columns else hist.iloc[:, :1].copy()
            close.columns = [tickers[0]]
        return close.ffill().dropna(how="all")
    except Exception:
        return pd.DataFrame()


def calc_period_returns(hist_df: pd.DataFrame) -> pd.DataFrame:
    if hist_df.empty or hist_df.shape[0] < 2:
        return pd.DataFrame()
    first_close = hist_df.iloc[0]
    last_close = hist_df.iloc[-1]
    period_pct = ((last_close - first_close) / first_close * 100).round(2)
    return pd.DataFrame({
        "股票代码": period_pct.index.tolist(),
        "区间涨跌幅 (%)": period_pct.values.tolist(),
    })


def _cell_str(val) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "N/A"
    return str(val)


def format_stock_data_for_llm(df: pd.DataFrame) -> str:
    if df is None or df.empty:
        return ""
    lines = []
    for _, row in df.iterrows():
        parts = [
            f"股票代码: {row.get('股票代码', '—')}",
            f"Forward P/E: {_cell_str(row.get('Forward P/E'))}",
            f"PEG: {_cell_str(row.get('PEG Ratio (5yr)'))}",
            f"P/B: {_cell_str(row.get('P/B'))}",
            f"ROE (%): {_cell_str(row.get('ROE (%)'))}",
            f"Operating Margin (%): {_cell_str(row.get('Operating Margin (%)'))}",
            f"EPS (Trailing) ($): {_cell_str(row.get('EPS (Trailing) ($)'))}",
            f"D/E (%): {_cell_str(row.get('D/E (%)'))}",
            f"FCF (B): {_cell_str(row.get('FCF (B)'))}",
            f"Current Ratio: {_cell_str(row.get('Current Ratio'))}",
            f"来源: {row.get('数据来源', '—')}",
        ]
        lines.append(", ".join(parts))
    return "\n".join(lines)


# =============================================================================
# CrewAI 分析主函数
# =============================================================================

def run_crewai_analysis(stock_data_str: str, thinking_placeholder, df=None):
    """
    运行 CrewAI 多角色分析：数据分析师 → 情报研究员 → 投资总监。
    【云端版变化】
    - 所有 Agent 统一使用 LiteLLM 原生 Groq 路由（"groq/llama-3.3-70b-versatile" 字符串）
    - 不再区分 logic_llm / tool_llm，单一模型承担所有角色
    - GROQ_API_KEY 在调用前写入 os.environ 供 LiteLLM 读取
    - RAG 检索、预搜索、预计算逻辑与本地版完全一致
    """

    # ── 检查 Groq API 密钥 ─────────────────────────────────────────────────
    llm = _make_groq_llm()
    if llm is None:
        return (
            "❌ **Groq API 密钥未配置**\n\n"
            "请在 `.streamlit/secrets.toml` 中添加：\n"
            "```toml\nGROQ_API_KEY = \"your-key-here\"\n```\n"
            "密钥申请：https://console.groq.com"
        )

    # ── Python 层双轨 RAG 预取 ─────────────────────────────────────────────
    RAG_CHUNK_CHARS = 220
    RAG_MAX_CHUNKS  = 3

    def _rag_search(vs, queries, k=2, max_chunks=RAG_MAX_CHUNKS):
        seen, chunks = set(), []

        def _is_noisy_chunk(text: str) -> bool:
            """过滤器：跳过 URL、邮箱、元数据页等低质量 chunk。"""
            if re.search(r"https?://", text):          # 含 URL
                return True
            if re.search(r"[\w.+-]+@[\w-]+\.\w+", text):  # 含邮箱
                return True
            noisy_phrases = (
                "download", "contact", "investor relations",
                "press release", "table of contents", "click here",
                "copyright ©", "all rights reserved",
            )
            lower = text.lower()
            if sum(1 for p in noisy_phrases if p in lower) >= 2:  # ≥2 个噪音词
                return True
            # 有效词（长度≥3 的词）不足 15 个，视为稀疏/元数据页
            meaningful_words = [w for w in text.split() if len(w) >= 3]
            if len(meaningful_words) < 15:
                return True
            return False

        for q in queries:
            for doc in vs.similarity_search(q, k=k):
                key = doc.page_content[:80]
                if key not in seen:
                    if _is_noisy_chunk(doc.page_content):
                        continue  # 跳过噪音 chunk
                    seen.add(key)
                    src = doc.metadata.get("source", "未知").replace("\\", "/").split("/")[-1]
                    page = doc.metadata.get("page", "?")
                    page_label = page + 1 if isinstance(page, int) else page
                    body = doc.page_content[:RAG_CHUNK_CHARS]
                    if len(doc.page_content) > RAG_CHUNK_CHARS:
                        body += "…"
                    chunks.append(f"[{src} p{page_label}] {body}")
        return chunks[:max_chunks]

    # ── RAG 搜索：按公司分组，从英文季报中提取财务亮点与风险因子 ────────────
    # 知识库内容为3家公司的英文季报（约10页/份），无投资哲学类内容。
    # 查询词使用英文财报关键词，按公司分组检索，确保每家公司都有相关内容。
    # rag_philosophy 已废弃：中文哲学词汇无法匹配英文季报，改由模型自身知识提供。
    rag_annual_report_text = ""
    vs = st.session_state.get("rag_vectorstore")
    if vs is not None:
        try:
            ar_chunks = _rag_search(vs, [
                # Meta 相关
                "Meta revenue advertising AI Reality Labs capital expenditure",
                # Amazon 相关
                "Amazon AWS cloud revenue operating income free cash flow",
                # Google 相关
                "Google Alphabet search revenue cloud Gemini AI operating income",
                # 通用风险/展望
                "risk factors competition regulation antitrust outlook guidance",
            ], k=2, max_chunks=6)
            rag_annual_report_text = "\n\n---\n\n".join(ar_chunks) if ar_chunks else "（未检索到季报相关内容）"
        except Exception as e:
            rag_annual_report_text = f"（知识库检索出错：{e}）"
    else:
        rag_annual_report_text = "（知识库为空，季报补充数据不可用）"

    # ── Python 层预计算风险标注 ────────────────────────────────────────────
    risk_notes_lines = []
    if df is not None and not df.empty:
        for _, row in df.iterrows():
            ticker = row.get("股票代码", "?")
            de_val = row.get("D/E (%)")
            cr_val = row.get("Current Ratio")
            fcf_val = row.get("FCF (B)")
            flags = []
            try:
                de_float = float(de_val)
                flags.append(f"D/E={de_float:.2f}%{'> 100%（高杠杆）' if de_float > 100 else '≤ 100%（杠杆正常）'}")
            except (TypeError, ValueError):
                flags.append("D/E=N/A")
            try:
                cr_float = float(cr_val)
                flags.append(f"Current Ratio={cr_float:.2f}{'< 1（流动性偏低）' if cr_float < 1.0 else '≥ 1（流动性正常）'}")
            except (TypeError, ValueError):
                flags.append("Current Ratio=N/A")
            try:
                fcf_float = float(str(fcf_val).replace("B", "").replace("N/A", ""))
                flags.append(f"FCF={fcf_val}（{'负值，警惕现金流风险' if fcf_float < 0 else '正值，现金流充裕'}）")
            except (TypeError, ValueError):
                if str(fcf_val).strip() in ("N/A", "", "None"):
                    flags.append("FCF=N/A（数据缺失，建议谨慎）")
            try:
                high_lev = (float(de_val) > 100) and (float(cr_val) < 1.0)
            except Exception:
                high_lev = False
            verdict = "⚠️ 高杠杆风险成立" if high_lev else "✅ 高杠杆风险不成立"
            risk_notes_lines.append(f"  {ticker}：{', '.join(flags)}。→ {verdict}")
    else:
        risk_notes_lines.append("  （无法获取 DataFrame，跳过预计算）")

    risk_notes_str = (
        "【Python预计算风险标注（权威结论，禁止覆盖）】\n"
        + "\n".join(risk_notes_lines)
        + "\n注意：「高杠杆风险」成立条件为 D/E > 100% 且 Current Ratio < 1 同时满足，"
        "上方已给出每只股票的判断结果，你必须照搬此结论，不得自行重新计算。"
    )

    # ── Python 层预计算各指标排名 ──────────────────────────────────────────
    ranking_lines = []
    if df is not None and not df.empty:
        numeric_cols = {
            "Forward P/E":         ("越低越好", True),
            "PEG Ratio (5yr)":     ("越低越好", True),
            "P/B":                 ("参考",    True),
            "ROE (%)":             ("越高越好", False),
            "Operating Margin (%)":("越高越好", False),
            "EPS (Trailing) ($)":  ("越高越好", False),
            "D/E (%)":             ("越低越好", True),
            "FCF (B)":             ("越高越好", False),
            "Current Ratio":       ("越高越好", False),
        }
        for col, (hint, asc) in numeric_cols.items():
            if col not in df.columns:
                continue
            try:
                vals = {}
                for _, row in df.iterrows():
                    v = row.get(col)
                    try:
                        vals[row["股票代码"]] = float(str(v).replace("B", "").strip())
                    except Exception:
                        pass
                if not vals:
                    continue
                sorted_tickers = sorted(vals.keys(), key=lambda t: vals[t], reverse=(not asc))
                rank_desc = "  ".join(
                    f"{t}={vals[t]}（第{i+1}名）"
                    for i, t in enumerate(sorted_tickers)
                )
                ranking_lines.append(f"  {col}（{hint}）：{rank_desc}")
            except Exception:
                pass
    else:
        ranking_lines.append("  （无法获取 DataFrame，跳过排名预计算）")

    ranking_str = (
        "【Python预计算各指标排名（权威结论，禁止覆盖）】\n"
        + "\n".join(ranking_lines)
        + "\n注意：括号内的「第N名」已是最终结论，你必须直接引用，严禁自行重新比较大小或修改排名。"
    )

    # ── Python 层预先执行全部6条搜索 ─────────────────────────────────────
    thinking_placeholder.markdown("🔍 **正在预先执行网络搜索（共6条）……**")
    _pre_search_results: list = []
    for _idx, (_co, _q) in enumerate(_PLANNED_SEARCHES, 1):
        thinking_placeholder.markdown(
            f"🔍 搜索进度：**{_idx}/6** | 公司：{_co} | 查询：`{_q}`"
        )
        try:
            _raw = _ddg_runner.run(_q)
            _snippet = str(_raw or "(no result)")
            if len(_snippet) > 600:
                _snippet = _snippet[:600] + "...[截断]"
        except Exception as _e:
            _snippet = f"(搜索失败: {_e})"
        _pre_search_results.append(f"【{_co}】查询词: {_q}\n结果: {_snippet}")
    _pre_search_block = "\n\n".join(_pre_search_results)
    thinking_placeholder.markdown("✅ **6条搜索全部完成，正在启动AI分析团队……**")

    # ── Agent 定义 ─────────────────────────────────────────────────────────
    data_analyst = Agent(
        role="资深定量分析师",
        goal=(
            "基于表格中的多维财务数据，评估盈利质量（ROE、Operating Margin）"
            "和财务健康度（D/E、Current Ratio、FCF）。"
            "重要：D/E 数值已是百分比（39.16 = 39.16%），高杠杆风险由系统预计算给出，直接引用结论，不得自行判断。"
        ),
        backstory="你只相信数字，逻辑严密，从不主观臆测。你严格遵循系统给出的预计算风险结论，不会自行推翻。",
        llm=llm,
        verbose=True,
        allow_delegation=False,
    )

    news_researcher = Agent(
        role="全球市场情报专家",
        goal=(
            "将系统预先提供的英文搜索结果翻译并整理为中文市场情报报告。\n"
            "任务描述中已包含全部搜索结果，直接根据这些内容撰写报告，无需调用任何工具。"
        ),
        backstory=(
            "你是精通中英双语的资深情报分析师，擅长从英文新闻和研究报告中提炼关键信息，"
            "用流畅的中文呈现护城河分析、风险要点和最新动态。"
        ),
        tools=[],
        llm=llm,
        verbose=True,
        allow_delegation=False,
        max_iter=4,
    )

    chief_strategist = Agent(
        role="首席投资官",
        goal=(
            "综合财务数据与护城河情报，制定一份长线投资方案。"
            "**核心任务：必须给出 $10,000 本金的具体分配比例和金额**，并确保总和为 100%。"
            "以「长期复利能力」为主（60%）、「短期估值」为辅（40%），结合非线性逻辑进行决策。"
            "报告第4节需引用芒格或格雷厄姆的投资哲学：若任务描述中附有知识库检索摘要，"
            "优先引用其中的原文或观点；若无摘要，则直接运用你自身所掌握的芒格/格雷厄姆"
            "经典投资原则（如护城河、能力圈、安全边际等），无需说明来源。"
        ),
        backstory=(
            "你是一位拥有 30 年经验、遵循查理·芒格风格的顶级长线投资总监。"
            "你极度厌恶平庸的'撒胡椒面'式投资。你敢于在确信的高 ROE、强护城河企业上重仓。"
            "你非常严谨，每一笔资金分配都能给出精确的数学理由和定性逻辑。"
        ),
        llm=llm,
        verbose=True,
        allow_delegation=False,
    )

    # ── Task 定义 ──────────────────────────────────────────────────────────
    task_analysis = Task(
        description=(
            "请严格基于以下美股表格数据进行分析。\n\n"
            "【重要数据说明】\n"
            "- D/E 列的数值单位已是百分比（例如：39.16 表示 39.16%，不是 39.16 倍）。\n"
            "- 「高杠杆风险」成立条件：D/E 数值 > 100 且 Current Ratio < 1.0，两者必须同时满足。\n"
            "- 系统已在下方「Python预计算风险标注」和「Python预计算各指标排名」中给出所有结论，"
            "你必须照搬这些结论，严禁自行重新计算、比较大小或修改排名。\n\n"
            + risk_notes_str + "\n\n"
            + ranking_str + "\n\n"
            "表格数据（D/E 单位=百分比，Current Ratio 为倍数）：\n"
            f"{stock_data_str}\n\n"
            "分析要求：\n"
            "1. 不仅看 PEG 和 P/E，还需结合盈利质量（ROE、利润率）和财务健康度做综合评估。\n"
            "2. 风险与排名：直接使用上方两个预计算块的结论，逐字引用，严禁自行修改或重新比较。\n"
            "3. 对每只股票给出量化总结，括号内的排名描述必须与预计算结果完全一致。"
        ),
        expected_output="一份基于多维财务指标的量化分析报告，含排名、理由及照搬自预计算的风险标注。",
        agent=data_analyst,
    )

    task_news = Task(
        description=(
            "⚠️【格式强制令】你是全球市场情报专家，不是财务分析师。\n"
            "本任务的唯一输出格式是：重要新闻 / 风险点 / 护城河分析，共三节。\n"
            "严禁输出任何以下内容：ROE排名、D/E数值、Current Ratio、Operating Margin排名、"
            "财务健康度评估、盈利质量评估、高杠杆风险判断。\n"
            "上述财务分析已由前一个Agent完成，你无需重复，重复即为错误。\n\n"
            "系统已通过 Python 完成全部6次网络搜索，结果如下。\n"
            "你的任务：将以下英文搜索结果翻译整理为中文市场情报报告，无需使用任何工具。\n\n"
            "【搜索结果（共6条，META×2 / AMZN×2 / GOOG×2）】\n"
            f"{_pre_search_block}\n\n"
            f"参考财务数据（仅供护城河分析引用，禁止做财务排名）：\n{stock_data_str}\n\n"
            "【输出格式（逐字遵守，不得更改节标题）】\n"
            "### Meta（META）\n"
            "#### 重要新闻\n（基于搜索结果，1-3条最新动态）\n"
            "#### 风险点\n（1-2条主要风险，来自新闻而非财务数据）\n"
            "#### 护城河分析\n（结合新闻与业务特征，描述竞争壁垒）\n\n"
            "### 亚马逊（AMZN）\n"
            "#### 重要新闻\n（基于搜索结果）\n"
            "#### 风险点\n（来自新闻）\n"
            "#### 护城河分析\n（竞争壁垒）\n\n"
            "### 谷歌（GOOG）\n"
            "#### 重要新闻\n（基于搜索结果）\n"
            "#### 风险点\n（来自新闻）\n"
            "#### 护城河分析\n（竞争壁垒）"
        ),
        expected_output=(
            "一份中文市场情报摘要，含 Meta（META）、亚马逊（AMZN）、谷歌（GOOG）三个独立章节，"
            "每节严格按「重要新闻 / 风险点 / 护城河分析」三小节展开，"
            "内容来自搜索结果，不含任何财务指标排名或盈利质量评估。"
        ),
        agent=news_researcher,
    )

    # ── RAG 注入文本 ───────────────────────────────────────────────────────
    # 只注入季报财务数据，不再注入哲学类内容（季报中不存在此类内容）。
    # [:1200] 避免 3 个 chunk × 400 字符被硬截断成残缺引用（如 "[t"）。
    _ar_content = rag_annual_report_text.strip()
    rag_inject_annual_report = (
        _ar_content[:1200]
        if _ar_content and not _ar_content.startswith("（") else ""
    )

    # ── Python 层预建财务对比表 ────────────────────────────────────────────
    metrics_table_md = ""
    if df is not None and not df.empty:
        rows_md = []
        for _, row in df.iterrows():
            def _v(col):
                val = row.get(col)
                if val is None or (isinstance(val, float) and pd.isna(val)):
                    return "N/A"
                return str(val)
            rows_md.append(
                f"| {_v('股票代码')} | {_v('Forward P/E')} | {_v('PEG Ratio (5yr)')} "
                f"| {_v('ROE (%)')} | {_v('Operating Margin (%)')} "
                f"| {_v('D/E (%)')} | {_v('FCF (B)')} | {_v('Current Ratio')} |"
            )
        metrics_table_md = (
            "| 股票代码 | Forward P/E | PEG | ROE | Operating Margin | D/E (%) | FCF | Current Ratio |\n"
            "| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |\n"
            + "\n".join(rows_md)
        )
    else:
        metrics_table_md = "（数据不可用）"

    # ── Python 层预计算资金分配比例 ───────────────────────────────────────
    def _compute_allocations(df_: "pd.DataFrame") -> dict:
        """返回 {ticker: pct_int} 的分配字典，总和精确=100，所有值为5的倍数。"""
        import math

        metric_defs = [
            ("ROE (%)",             0.35, True),
            ("FCF (B)",             0.25, True),
            ("Operating Margin (%)",0.20, True),
            ("PEG Ratio (5yr)",     0.15, False),
            ("D/E (%)",             0.03, False),
            ("Current Ratio",       0.02, True),
        ]

        rows = {row["股票代码"]: row for _, row in df_.iterrows()}
        tickers = list(rows.keys())
        n = len(tickers)
        if n == 0:
            return {}

        raw = {col: {} for col, _, _ in metric_defs}
        for t in tickers:
            for col, _, _ in metric_defs:
                val = rows[t].get(col)
                try:
                    raw[col][t] = float(str(val).replace("B", "").strip())
                except Exception:
                    raw[col][t] = float("nan")

        norm_scores = {t: 0.0 for t in tickers}
        for col, weight, higher_better in metric_defs:
            vals = [raw[col][t] for t in tickers]
            valid = [v for v in vals if not math.isnan(v)]
            if len(valid) == 0:
                for t in tickers:
                    norm_scores[t] += weight * 0.5
                continue
            vmin, vmax = min(valid), max(valid)
            for t in tickers:
                v = raw[col][t]
                if math.isnan(v):
                    normalized = 0.5
                elif vmax == vmin:
                    normalized = 0.5
                elif higher_better:
                    normalized = (v - vmin) / (vmax - vmin)
                else:
                    normalized = (vmax - v) / (vmax - vmin)
                norm_scores[t] += weight * normalized

        total_score = sum(norm_scores.values()) or 1.0
        init_pct = {t: norm_scores[t] / total_score * 100 for t in tickers}

        FLOOR = 5.0
        pct = dict(init_pct)
        pinned = set()
        for _ in range(n + 1):
            below = {t: FLOOR - pct[t] for t in tickers if t not in pinned and pct[t] < FLOOR}
            if not below:
                break
            deficit = sum(below.values())
            for t in below:
                pct[t] = FLOOR
                pinned.add(t)
            above = {t: pct[t] for t in tickers if t not in pinned}
            above_sum = sum(above.values())
            if above_sum <= 0:
                for t in tickers:
                    pct[t] = 100.0 / n
                break
            if above_sum <= deficit:
                for t in above:
                    pct[t] = FLOOR
                    pinned.add(t)
                total_floor = FLOOR * n
                if total_floor <= 100.0:
                    best = max(tickers, key=lambda t: norm_scores[t])
                    pct[best] = 100.0 - FLOOR * (n - 1)
                else:
                    for t in tickers:
                        pct[t] = 100.0 / n
                break
            scale = (above_sum - deficit) / above_sum
            for t in above:
                pct[t] *= scale

        # Step 5：四舍五入到最近的5%整数倍，再强制总和=100
        STEP = 5
        FLOOR_STEP = 5
        rounded = {t: int(round(pct[t] / STEP) * STEP) for t in tickers}
        for t in tickers:
            if rounded[t] < FLOOR_STEP:
                rounded[t] = FLOOR_STEP

        total = sum(rounded.values())
        diff  = 100 - total
        if diff != 0:
            sorted_by_score = sorted(tickers, key=lambda t: norm_scores[t], reverse=True)
            steps = abs(diff) // STEP
            direction = 1 if diff > 0 else -1
            for _ in range(steps):
                for candidate in sorted_by_score:
                    new_val = rounded[candidate] + direction * STEP
                    if new_val >= FLOOR_STEP:
                        rounded[candidate] = new_val
                        break

        return rounded

    TOTAL_CAPITAL = 10000
    if df is not None and not df.empty:
        tickers_list = df["股票代码"].tolist()
        n = len(tickers_list)
        alloc_pct  = _compute_allocations(df)
        alloc_rows = "\n".join(
            f"| {t} | [填写投资逻辑标签] "
            f"| {alloc_pct.get(t, 0)}% "
            f"| ${alloc_pct.get(t, 0) * TOTAL_CAPITAL // 100} "
            f"| [填写0-100] |"
            for t in tickers_list
        )
        pct_sum = sum(alloc_pct.values())
        alloc_skeleton = (
            f"【系统已预算完毕：以下表格含全部 {n} 只股票，比例和金额已由 Python 精确计算（总和={pct_sum}%），"
            "你只需填写【投资逻辑标签】和【20年信心指数】，其余数字禁止修改。】\n"
            "| 股票代码 | 投资逻辑标签 | 建议比例 (%) | 建议金额 ($) | 20年信心指数 (0-100) |\n"
            "| :--- | :--- | :--- | :--- | :--- |\n"
            + alloc_rows + "\n"
            f"| **总计** | -- | **{pct_sum}%** | **${TOTAL_CAPITAL}** | -- |\n"
        )
    else:
        tickers_list = []
        alloc_pct   = {}
        alloc_skeleton = (
            "| 股票代码 | 投资逻辑标签 | 建议比例 (%) | 建议金额 ($) | 20年信心指数 (0-100) |\n"
            "| :--- | :--- | :--- | :--- | :--- |\n"
            "| **总计** | -- | **100%** | **$10,000** | -- |\n"
        )

    task_report = Task(
        description=(
            "任务：制定 $10,000 / 20年视野的 META、AMZN、GOOG 长线组合。\n"
            "比例和金额已由系统计算完毕，禁止修改数字。\n"
            "只需：① 第1节填投资逻辑标签和信心指数；② 第2节写持仓理由和风险。\n\n"

            "**【强制约束1：公司名称】**\n"
            "全文中公司名称只允许使用以下格式，禁止任何其他翻译、别名或音译：\n"
            "- META → 只写「Meta（META）」\n"
            "- AMZN → 只写「亚马逊（AMZN）」\n"
            "- GOOG → 只写「谷歌（GOOG）」\n\n"

            "**【强制约束2：20年信心指数含义】**\n"
            "信心指数 = 你对该公司20年后护城河仍然稳固的主观确信度（0=不确信，100=极度确信）。\n"
            "它与分配比例是两个独立维度：\n"
            "  · 分配比例由Python基于ROE/FCF/利润率等6项财务指标量化计算，反映当前财务相对优势。\n"
            "  · 信心指数是你对护城河深度与商业模式可持续性的定性判断，可独立于比例高低。\n"
            "例如：META信心85、谷歌信心95，两者都是高确信，但谷歌的FCF和ROE双项第一，\n"
            "导致量化分配比例差距较大——这是正常且合理的，请如实填写，不要为追求一致性而虚报。\n\n"

            + (
                "【季报补充数据（仅供第2节持仓逻辑参考，不影响报告结构和数字）】\n"
                f"{rag_inject_annual_report}\n"
                "【季报补充数据结束，请勿续写以上内容，立即按下方格式输出完整报告】\n\n"
                if rag_inject_annual_report else ""
            )

            + "**报告格式（必须完整输出）**\n"

            "### 1. 💰 最终资金分配方案\n"
            "以下表格比例和金额已由系统精确计算，**禁止修改任何数字**，"
            "只需在每行填入投资逻辑标签（5字以内）和信心指数：\n"
            + alloc_skeleton + "\n"

            "\n### 2. 深度持仓逻辑\n"
            "请严格按以下三节结构输出，每节标题固定，禁止更改：\n\n"
            "#### Meta（META）\n"
            "持仓理由：（结合ROE排名、Operating Margin护城河、广告平台网络效应）\n"
            "最大毁灭性风险：（一句话）\n\n"
            "#### 亚马逊（AMZN）\n"
            "持仓理由：（结合AWS云业务护城河、物流壁垒、ROE与FCF数据）\n"
            "最大毁灭性风险：（一句话）\n\n"
            "#### 谷歌（GOOG）\n"
            "持仓理由：（结合ROE第一、FCF第一、搜索+AI双护城河）\n"
            "最大毁灭性风险：（一句话）\n\n"

            "### 3. 关键财务指标对比\n"
            "**以下表格已由系统精确生成，请原样输出，一个字符都不要修改：**\n"
            + metrics_table_md + "\n\n"

            "\n### 4. 首席投资官总结\n"
            "一句话概括组合策略，并引用一条你熟知的芒格或格雷厄姆经典投资原则。\n"
        ),
        expected_output="按报告格式完整输出上述四节内容，数字禁止修改，公司名称使用规定格式。",
        agent=chief_strategist,
        context=[task_analysis, task_news],
    )

    crew = Crew(
        agents=[data_analyst, news_researcher, chief_strategist],
        tasks=[task_analysis, task_news, task_report],
        process=Process.sequential,
        verbose=True,
        memory=False,
        # llm= 和 manager_llm= 均不在 Crew 级别设置。
        # Agent 级别已通过 llm="groq/..." 字符串直接绑定 LiteLLM Groq 路由，
        # Crew 级别重复设置反而触发额外路由检查。
    )

    # ── 重定向 stdout / stderr / logging ──────────────────────────────────
    _stream = StreamToStreamlit(thinking_placeholder)
    _old_stdout = sys.stdout
    _old_stderr = sys.stderr
    sys.stdout = _stream
    sys.stderr = _stream

    _NOISY_LOGGERS = [
        "httpcore", "httpx", "urllib3", "requests",
        "openai", "openai._base_client", "asyncio", "selector_events",
    ]
    _old_noisy_levels = {}
    for _name in _NOISY_LOGGERS:
        _lg = logging.getLogger(_name)
        _old_noisy_levels[_name] = _lg.level
        _lg.setLevel(logging.WARNING)

    _log_handler = _StreamlitLogHandler(_stream)
    _log_handler.setLevel(logging.INFO)
    _root_logger = logging.getLogger()
    _old_log_level = _root_logger.level
    _root_logger.addHandler(_log_handler)
    if _root_logger.level == logging.NOTSET or _root_logger.level > logging.INFO:
        _root_logger.setLevel(logging.INFO)

    try:
        result = crew.kickoff()
    finally:
        sys.stdout = _old_stdout
        sys.stderr = _old_stderr
        _root_logger.removeHandler(_log_handler)
        _root_logger.setLevel(_old_log_level)
        for _name, _lvl in _old_noisy_levels.items():
            logging.getLogger(_name).setLevel(_lvl)

    if hasattr(result, "raw"):
        final_text = result.raw
    elif hasattr(result, "result"):
        final_text = result.result
    else:
        final_text = str(result) if result is not None else ""

    # 兜底：若模型输出原始 JSON 而非报告文本
    stripped = (final_text or "").strip()
    if stripped.startswith('{"name":') or stripped.startswith("{'name':"):
        final_text = (
            "⚠️ **首席投资官未能生成完整报告**\n\n"
            "模型未正确执行，将工具调用 JSON 作为最终输出。\n\n"
            "**建议处理方式：**\n"
            "1. 点击「启动 AI 深度投研团队」按钮**重试**\n"
            "2. 检查 Groq API 密钥是否有效\n\n"
            f"**原始输出（供调试）：**\n```json\n{stripped[:500]}\n```"
        )
    return final_text


# =============================================================================
# 侧边栏
# =============================================================================
st.sidebar.header("📋 选择股票")

selected_from_options = st.sidebar.multiselect(
    "从下列股票中勾选",
    options=OPTION_TICKERS,
    default=PRESET_TICKERS,
    help="预设：META, Amazon, GOOG；可选：FTEC, QQQ, VOO, 英伟达, 微软, 苹果",
)

custom_input = st.sidebar.text_input(
    "自选股代码",
    placeholder="例如：AAPL, MSFT 或 NVDA TSLA",
    help="多个代码用逗号或空格分隔，将与上方已选股票合并展示",
)
custom_tickers = parse_custom_tickers(custom_input or "")
all_tickers = list(dict.fromkeys([*selected_from_options, *custom_tickers]))

st.sidebar.header("📉 走势图周期")
period_label_to_value = {label: value for label, value in PERIOD_OPTIONS}
selected_period_label = st.sidebar.selectbox(
    "选择时间范围",
    options=[opt[0] for opt in PERIOD_OPTIONS],
    index=4,
    help="折线图将显示该时间范围内的收盘价走势",
)
selected_period = period_label_to_value[selected_period_label]

# -----------------------------------------------------------------------------
# 侧边栏：API 状态指示灯
# -----------------------------------------------------------------------------
st.sidebar.markdown("---")
st.sidebar.header("🔑 API 状态")

def _status_badge(ready: bool, label: str) -> str:
    icon = "🟢" if ready else "🔴"
    status = "已配置" if ready else "未配置"
    return f"{icon} **{label}**：{status}"

st.sidebar.markdown(_status_badge(_GROQ_READY,   "Groq API Key"))
st.sidebar.markdown(_status_badge(_GEMINI_READY, "Gemini API Key"))
st.sidebar.markdown(_status_badge(_HF_READY,     "HuggingFace Token"))

if not _GROQ_READY:
    st.sidebar.error("⚠️ GROQ_API_KEY 未配置，AI 分析功能将无法使用。")
if not _GEMINI_READY and not _HF_READY:
    st.sidebar.warning("⚠️ 嵌入引擎密钥均未配置，RAG 知识库功能将不可用。")

# -----------------------------------------------------------------------------
# 侧边栏：RAG 知识库管理
# -----------------------------------------------------------------------------
st.sidebar.markdown("---")
st.sidebar.header("📚 知识库管理 (RAG)")

# ── 嵌入引擎选择器 ─────────────────────────────────────────────────────────
_engine_options = {
    "🤖 自动（优先 Gemini，失败切换 HF）": "auto",
    "🔵 Google Gemini（免费层，有日限额）": "gemini",
    "🟠 HuggingFace Inference API":         "huggingface",
}
_engine_label = st.sidebar.selectbox(
    "嵌入引擎",
    options=list(_engine_options.keys()),
    index=0,
    help=(
        "Gemini：免费层每天约 1500 次请求，财报PDF较短时推荐。 "
        "HuggingFace：无日限额，但 Inference API 响应较慢。 "
        "Gemini 日限额用完后请手动切换为 HuggingFace。"
    ),
)
_selected_engine = _engine_options[_engine_label]

st.sidebar.caption("📌 建议上传精简财报（每份 ≤30页，总计 ≤3份），避免 chunk 过多超限额。")

uploaded_pdfs = st.sidebar.file_uploader(
    "上传财报或研报 PDF",
    type=["pdf"],
    accept_multiple_files=True,
    help="仅支持上传文件，静态 knowledge_base 文件夹已停用（防止大型研报产生过多 chunk 超限额）",
)

if st.sidebar.button("🔨 构建 / 更新知识库", use_container_width=True):
    if not uploaded_pdfs:
        st.sidebar.warning("⚠️ 请先上传至少一个 PDF 文件。")
    else:
        with st.sidebar:
            with st.spinner("正在构建向量知识库，请稍候..."):
                vs = build_rag_vectorstore(
                    uploaded_pdf_files=uploaded_pdfs,
                    engine_choice=_selected_engine,
                )
                st.session_state["rag_vectorstore"] = vs
                if vs is None:
                    st.warning("⚠️ 知识库构建失败，请检查 PDF 文件和嵌入引擎配置。")

if st.session_state["rag_vectorstore"] is not None:
    st.sidebar.success("📖 知识库就绪")
else:
    st.sidebar.info("📭 知识库为空（首席投资官将跳过 RAG 检索）")

# =============================================================================
# 主界面
# =============================================================================
st.title("📈 美股关键指标")
st.caption("数据来源：Yahoo Finance。Forward P/E 与 PEG 采用三重保障：爬虫 → yfinance API → 手动计算；表格中「数据来源」列标明每个数值的来源。")

if not all_tickers:
    st.warning("请在左侧至少选择一只股票或输入自选股代码。")
    st.stop()

with st.spinner("正在获取股票数据..."):
    df, fetch_ts = fetch_stock_data(all_tickers)

st.subheader("指标一览")
st.dataframe(
    df,
    width="stretch",
    hide_index=True,
    column_config={
        "股票代码": st.column_config.TextColumn("股票代码", width="small"),
        "最新价 (USD)": st.column_config.NumberColumn("最新价 (USD)", format="%.2f", width="small"),
        "日涨跌幅 (%)": st.column_config.NumberColumn("日涨跌幅 (%)", format="%.2f", width="small"),
        "Forward P/E": st.column_config.NumberColumn("Forward P/E", format="%.2f", width="small"),
        "PEG Ratio (5yr)": st.column_config.NumberColumn("PEG Ratio (5yr)", format="%.2f", width="small"),
        "P/B": st.column_config.NumberColumn("P/B", format="%.2f", width="small"),
        "ROE (%)": st.column_config.NumberColumn("ROE (%)", format="%.2f", width="small"),
        "Operating Margin (%)": st.column_config.NumberColumn("Operating Margin (%)", format="%.2f", width="small"),
        "EPS (Trailing) ($)": st.column_config.NumberColumn("EPS (Trailing) ($)", format="%.2f", width="small"),
        "D/E (%)": st.column_config.NumberColumn("D/E (%)", format="%.2f", width="small"),
        "FCF (B)": st.column_config.TextColumn("FCF (B)", width="small"),
        "Current Ratio": st.column_config.NumberColumn("Current Ratio", format="%.2f", width="small"),
        "数据来源": st.column_config.TextColumn("数据来源", width="medium", help="FPE: Forward P/E 来源 | PEG: PEG Ratio 来源"),
    },
)

st.caption(f"**数据更新时间**：{fetch_ts}（指标缓存约 {CACHE_TTL // 60} 分钟，以下为本次拉取结果）")

with st.expander("📊 核心投资指标速查手册"):
    st.markdown("""
- **Forward P/E（前瞻市盈率）**：反映市场对未来盈利的定价。长线关注其与历史均值的偏离度。
- **PEG Ratio（市盈率相对增长比率）**：核心指标，越小越好，通常 < 1 被视为低估。
- **P/B（市净率）**：衡量股价与净资产的关系。科技股通常较高，但过高可能意味着估值泡沫。
- **ROE (%)(净资产收益率)**：核心指标，衡量公司赚钱效率。长期投资者应寻找稳定在 15–20% 以上的企业。
- **Operating Margin (%)(运营利润率)**：反映核心业务盈利能力。高利润率意味着更强的抗风险「护城河」。
- **EPS (Trailing) ($)（每股收益）**：反映过去一年的盈利实绩。
- **D/E (%)（债务股本比）**：衡量财务杠杆。风险指标：长线投资者应警惕 > 100% 且持续上升的企业。
- **FCF (B)（自由现金流，亿）**：公司的「真金白银」。持续的正现金流是长线持有（如 20 年）的基础。
- **Current Ratio（流动比率）**：衡量短期偿债能力。理想值应 > 1.2，低于 1 需警惕流动性危机。
""")

# -----------------------------------------------------------------------------
# AI 深度投研团队
# -----------------------------------------------------------------------------
st.markdown("---")
if st.button("🚀 启动 AI 深度投研团队", type="primary", width="stretch"):
    if not _GROQ_READY:
        st.error("❌ Groq API Key 未配置，请在 `.streamlit/secrets.toml` 中添加 `GROQ_API_KEY`。")
    else:
        stock_data_str = format_stock_data_for_llm(df)
        if not stock_data_str.strip():
            st.warning("当前没有可用的股票数据，请先加载表格后再试。")
        else:
            with st.expander("🕵️ AI 团队协作中 (执行进度展示)", expanded=True):
                st.info("提示：Groq 云端推理，速度通常比本地 Ollama 快数倍，请稍候。")
                with st.spinner("团队成员正在深度研判中，请稍候..."):
                    thinking_placeholder = st.empty()
                    try:
                        final_report = run_crewai_analysis(stock_data_str, thinking_placeholder, df=df)
                    except Exception as e:
                        thinking_placeholder.error(
                            f"运行失败：{e}\n"
                            "请检查 Groq API 密钥是否有效，以及网络连接是否正常。"
                        )
                        final_report = ""
            if final_report:
                st.subheader("📄 最终投资建议报告")
                st.markdown(final_report)
                report_filename = f"美股投资建议报告_{datetime.now().strftime('%Y%m%d')}.md"
                st.download_button(
                    label="📥 下载 Markdown 格式报告",
                    data=final_report.encode("utf-8"),
                    file_name=report_filename,
                    mime="text/markdown",
                )

# 走势折线图
st.subheader("股价走势")
with st.spinner("正在获取历史行情..."):
    hist_df = fetch_history(all_tickers, selected_period)

if hist_df.empty or hist_df.shape[0] < 2:
    st.info("当前周期下暂无足够历史数据可绘制走势图，请尝试其他周期或股票。")
else:
    vmin = hist_df.min().min()
    vmax = hist_df.max().max()
    padding = max((vmax - vmin) * 0.05, 0.01) if vmax > vmin else 1.0
    y_min = max(0, vmin - padding)
    y_max = vmax + padding
    hist_reset = hist_df.reset_index()
    date_col = hist_reset.columns[0]
    chart_df = hist_reset.melt(id_vars=[date_col], var_name="股票", value_name="收盘价").rename(
        columns={date_col: "日期"}
    )
    c = (
        alt.Chart(chart_df)
        .mark_line(strokeWidth=2)
        .encode(
            x=alt.X("日期", type="temporal"),
            y=alt.Y("收盘价:Q", scale=alt.Scale(domain=[y_min, y_max])),
            color="股票:N",
        )
        .properties(height=400)
    )
    st.altair_chart(c, width="stretch")

# 区间涨跌幅
period_df = calc_period_returns(hist_df)
if not period_df.empty:
    st.subheader("所选周期区间涨跌幅")
    st.caption(f"时间范围：{selected_period_label}（与上方走势图一致）")
    st.dataframe(period_df, width="stretch", hide_index=True)

st.caption("三重保障：① Web Scraper = Key Statistics 页爬虫 ② yfinance API = info.forwardPE / pegRatio ③ Calculated = 最新价 ÷ forwardEps（仅 Forward P/E）。")
