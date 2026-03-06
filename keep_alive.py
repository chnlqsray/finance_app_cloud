"""
Streamlit 应用保活脚本
使用 Playwright 启动真实 Chromium 浏览器，建立 WebSocket 连接，
触发 Streamlit 活跃检测，防止应用进入休眠（12小时无活动触发）。
"""

import time
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

URLS = [
    "https://my-finance-ai.streamlit.app/",
    "https://my-movie-ai.streamlit.app/",
]

# Streamlit 主容器 selector（页面正常加载后出现）
STREAMLIT_READY_SELECTOR = 'div[data-testid="stAppViewContainer"]'

# 休眠拦截页的唤醒按钮 selector
WAKEUP_BUTTON_SELECTOR = 'button[data-testid="wakeup-button-owner"], button[data-testid="wakeup-button"]'

# ── 关键修复：等待唤醒按钮渲染的时间从 5 秒延长至 20 秒 ──
# 原因：Streamlit 休眠页由 React 渲染，DOM 加载完毕后按钮还需几秒才出现
WAKEUP_BUTTON_WAIT_MS = 20_000

# 冷启动最长等待时间：90 秒（实测唤醒在 60 秒内完成，留有余量）
COLD_START_TIMEOUT_MS = 120_000

# 页面加载成功后额外停留时间（秒）：确保 WebSocket 心跳稳定发送
HEARTBEAT_STAY_SECONDS = 20


def wake_app(page, url: str, index: int) -> bool:
    """访问单个 Streamlit 应用，处理休眠拦截页，等待加载完成并停留维持 WebSocket 连接。"""
    print(f"\n[{index}] 正在访问：{url}", flush=True)
    try:
        # ── 核心修复：改用 networkidle，等待 React 真正渲染完毕 ──────────
        # 原因：domcontentloaded 只代表 HTML 骨架加载完，此时页面仍是空白
        # networkidle 代表网络请求静止，React 已完成渲染，按钮/容器才真正出现
        print(f"[{index}] 等待页面完整渲染（networkidle）…", flush=True)
        page.goto(url, wait_until="networkidle", timeout=60_000)

        # ── 阶段截图①：页面渲染完成后立即截图 ───────────────────────────
        page.screenshot(path=f"screenshot_{index}_1_after_load.png")
        print(f"[{index}] 截图①已保存（页面渲染后）", flush=True)

        # ── 检测是否出现休眠拦截页（等待 20 秒让 React 完成渲染）────────
        try:
            wakeup_btn = page.wait_for_selector(
                WAKEUP_BUTTON_SELECTOR,
                timeout=WAKEUP_BUTTON_WAIT_MS
            )
            if wakeup_btn:
                print(f"[{index}] 检测到休眠拦截页，正在点击唤醒按钮…", flush=True)
                wakeup_btn.click()
                print(f"[{index}] 唤醒按钮已点击，等待应用重新启动…", flush=True)

                # ── 阶段截图②：点击唤醒按钮 10 秒后截图 ──────────────
                time.sleep(10)
                page.screenshot(path=f"screenshot_{index}_2_after_click.png")
                print(f"[{index}] 截图②已保存（点击唤醒后10秒）", flush=True)

        except PlaywrightTimeoutError:
            # 20秒内未出现唤醒按钮，说明应用未休眠，正常加载中
            print(f"[{index}] 未检测到休眠页，应用正常加载中…", flush=True)

        # ── 等待 Streamlit 主容器出现（无论是否经过唤醒）────────────────
        print(f"[{index}] 等待 Streamlit 容器就绪（最长 90 秒）…", flush=True)
        page.wait_for_selector(STREAMLIT_READY_SELECTOR, timeout=COLD_START_TIMEOUT_MS)
        print(f"[{index}] ✅ Streamlit 容器已就绪，WebSocket 连接已建立。", flush=True)

        print(f"[{index}] 停留 {HEARTBEAT_STAY_SECONDS} 秒，发送 WebSocket 心跳…", flush=True)
        time.sleep(HEARTBEAT_STAY_SECONDS)

        # ── 阶段截图③：最终完成截图 ────────────────────────────────
        page.screenshot(path=f"screenshot_{index}_3_final.png")
        print(f"[{index}] 截图③已保存（加载完成）", flush=True)
        return True

    except PlaywrightTimeoutError:
        print(f"[{index}] ⚠️ 超时：应用在规定时间内未完成加载。", flush=True)
        try:
            page.screenshot(path=f"screenshot_{index}_timeout.png")
        except Exception:
            pass
        return False

    except Exception as e:
        print(f"[{index}] ❌ 发生错误：{e}", flush=True)
        try:
            page.screenshot(path=f"screenshot_{index}_error.png")
        except Exception:
            pass
        return False


def main():
    print("=== Streamlit 保活脚本启动 ===", flush=True)
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for i, url in enumerate(URLS, start=1):
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                )
            )
            page = context.new_page()
            success = wake_app(page, url, i)
            results.append((url, success))
            context.close()

        browser.close()

    print("\n=== 本次运行结果汇总 ===", flush=True)
    all_ok = True
    for url, success in results:
        status = "✅ 成功" if success else "⚠️ 超时/失败"
        print(f"  {status}  {url}", flush=True)
        if not success:
            all_ok = False

    if not all_ok:
        print("\n部分应用未能在超时时间内完成加载，下次触发时应已恢复正常。", flush=True)

    print("\n=== 脚本结束 ===", flush=True)


if __name__ == "__main__":
    main()
