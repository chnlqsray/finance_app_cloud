"""
Streamlit 应用保活脚本
使用 Playwright 启动真实 Chromium 浏览器，建立 WebSocket 连接，
触发 Streamlit 活跃检测，防止应用进入休眠（12小时无活动触发）。

更新：自动处理 Streamlit 休眠拦截页（点击"Yes, get this app back up!"按钮）。
"""

import time
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

URLS = [
    "https://my-finance-ai.streamlit.app/",
    "https://my-movie-ai.streamlit.app/",
]

# Streamlit 主容器 selector（页面正常加载后出现）
STREAMLIT_READY_SELECTOR = 'div[data-testid="stAppViewContainer"]'

# 休眠拦截页的唤醒按钮 selector（同时覆盖 owner 和普通访客两种按钮）
WAKEUP_BUTTON_SELECTOR = 'button[data-testid="wakeup-button-owner"], button[data-testid="wakeup-button"]'

# 冷启动最长等待时间（毫秒）：120秒，覆盖点击唤醒后的重建过程
COLD_START_TIMEOUT_MS = 120_000

# 页面加载成功后额外停留时间（秒）：确保 WebSocket 心跳稳定发送
HEARTBEAT_STAY_SECONDS = 20


def wake_app(page, url: str, index: int) -> bool:
    """访问单个 Streamlit 应用，处理休眠拦截页，等待加载完成并停留维持 WebSocket 连接。"""
    print(f"\n[{index}] 正在访问：{url}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)

        # ── 检测是否出现休眠拦截页，若有则点击唤醒按钮 ──────────────────
        try:
            wakeup_btn = page.wait_for_selector(WAKEUP_BUTTON_SELECTOR, timeout=5_000)
            if wakeup_btn:
                print(f"[{index}] 检测到休眠拦截页，正在点击唤醒按钮…")
                wakeup_btn.click()
                print(f"[{index}] 唤醒按钮已点击，等待应用重新启动（最长 120 秒）…")
        except PlaywrightTimeoutError:
            # 5秒内未出现唤醒按钮，说明应用未休眠，正常加载中
            print(f"[{index}] 未检测到休眠页，应用正常加载中…")

        # ── 等待 Streamlit 主容器出现（无论是否经过唤醒）────────────────
        print(f"[{index}] 等待 Streamlit 容器就绪…")
        page.wait_for_selector(STREAMLIT_READY_SELECTOR, timeout=COLD_START_TIMEOUT_MS)
        print(f"[{index}] ✅ Streamlit 容器已就绪，WebSocket 连接已建立。")

        print(f"[{index}] 停留 {HEARTBEAT_STAY_SECONDS} 秒，发送 WebSocket 心跳…")
        time.sleep(HEARTBEAT_STAY_SECONDS)

        screenshot_path = f"screenshot_{index}.png"
        page.screenshot(path=screenshot_path)
        print(f"[{index}] 截图已保存：{screenshot_path}")
        return True

    except PlaywrightTimeoutError:
        print(f"[{index}] ⚠️ 超时：应用在 120 秒内未完成加载。")
        try:
            page.screenshot(path=f"screenshot_{index}_timeout.png")
        except Exception:
            pass
        return False

    except Exception as e:
        print(f"[{index}] ❌ 发生错误：{e}")
        try:
            page.screenshot(path=f"screenshot_{index}_error.png")
        except Exception:
            pass
        return False


def main():
    print("=== Streamlit 保活脚本启动 ===")
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

    print("\n=== 本次运行结果汇总 ===")
    all_ok = True
    for url, success in results:
        status = "✅ 成功" if success else "⚠️ 超时/失败"
        print(f"  {status}  {url}")
        if not success:
            all_ok = False

    if not all_ok:
        print("\n部分应用未能在超时时间内完成加载，下次触发时应已恢复正常。")

    print("\n=== 脚本结束 ===")


if __name__ == "__main__":
    main()
