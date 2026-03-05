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

# Streamlit 主容器的 selector，页面完全加载后出现
STREAMLIT_READY_SELECTOR = 'div[data-testid="stAppViewContainer"]'

# 冷启动最长等待时间（毫秒）：90秒，足够覆盖休眠后的重建过程
COLD_START_TIMEOUT_MS = 90_000

# 页面加载成功后额外停留时间（秒）：确保 WebSocket 心跳稳定发送
HEARTBEAT_STAY_SECONDS = 20


def wake_app(page, url: str, index: int) -> bool:
    """访问单个 Streamlit 应用，等待加载完成并停留以维持 WebSocket 连接。"""
    print(f"\n[{index}] 正在访问：{url}")
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        print(f"[{index}] 页面已加载，等待 Streamlit 容器就绪（最长 90 秒）…")

        page.wait_for_selector(STREAMLIT_READY_SELECTOR, timeout=COLD_START_TIMEOUT_MS)
        print(f"[{index}] ✅ Streamlit 容器已就绪，WebSocket 连接已建立。")

        print(f"[{index}] 停留 {HEARTBEAT_STAY_SECONDS} 秒，发送 WebSocket 心跳…")
        time.sleep(HEARTBEAT_STAY_SECONDS)

        screenshot_path = f"screenshot_{index}.png"
        page.screenshot(path=screenshot_path)
        print(f"[{index}] 截图已保存：{screenshot_path}")
        return True

    except PlaywrightTimeoutError:
        print(f"[{index}] ⚠️ 超时：应用在 90 秒内未完成加载（可能仍在冷启动中）。")
        try:
            page.screenshot(path=f"screenshot_{index}_timeout.png")
        except Exception:
            pass
        return False

    except Exception as e:
        print(f"[{index}] ❌ 发生错误：{e}")
        return False


def main():
    print("=== Streamlit 保活脚本启动 ===")
    results = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        for i, url in enumerate(URLS, start=1):
            # 每个 URL 使用独立的页面上下文，互不干扰
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
        print("\n部分应用未能在超时时间内完成加载，可能正处于冷启动中。")
        print("下次触发时应已恢复正常。")

    print("\n=== 脚本结束 ===")


if __name__ == "__main__":
    main()
