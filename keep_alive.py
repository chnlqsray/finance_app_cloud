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

# Streamlit 主容器 selector
STREAMLIT_READY_SELECTOR = 'div[data-testid="stAppViewContainer"]'

# 冷启动最长等待时间（毫秒）
COLD_START_TIMEOUT_MS = 120_000

# 页面加载成功后额外停留时间（秒）
HEARTBEAT_STAY_SECONDS = 20


def try_click_wakeup_button(page, index: int) -> bool:
    """
    用 JavaScript 直接点击唤醒按钮，完全绕过 Playwright 的可见性判断。
    返回 True 表示找到并点击了按钮，False 表示未找到（应用未休眠）。
    """
    clicked = page.evaluate('''() => {
        const btn = document.querySelector('button[data-testid="wakeup-button-owner"]')
                 || document.querySelector('button[data-testid="wakeup-button"]');
        if (btn) {
            btn.click();
            return true;
        }
        return false;
    }''')
    return clicked


def wake_app(page, url: str, index: int) -> bool:
    """访问单个 Streamlit 应用，处理休眠拦截页，等待加载完成并停留维持 WebSocket 连接。"""
    print(f"\n[{index}] 正在访问：{url}", flush=True)
    try:
        # networkidle：等待 React 完整渲染后再继续
        print(f"[{index}] 等待页面完整渲染（networkidle）…", flush=True)
        page.goto(url, wait_until="networkidle", timeout=60_000)

        # ── 阶段截图①：渲染完成后立即截图 ───────────────────────────────
        page.screenshot(path=f"screenshot_{index}_1_after_load.png")
        print(f"[{index}] 截图①已保存（页面渲染后）", flush=True)

        # ── 用 JavaScript 检测并点击唤醒按钮（绕过 Playwright 可见性判断）─
        clicked = try_click_wakeup_button(page, index)
        if clicked:
            print(f"[{index}] ✅ 检测到休眠页，已用 JS 点击唤醒按钮，等待应用重启…", flush=True)

            # ── 阶段截图②：点击后 10 秒截图 ──────────────────────────────
            time.sleep(10)
            page.screenshot(path=f"screenshot_{index}_2_after_click.png")
            print(f"[{index}] 截图②已保存（点击唤醒后10秒）", flush=True)
        else:
            print(f"[{index}] 未检测到休眠页，应用正常加载中…", flush=True)

        # ── 等待 Streamlit 主容器出现 ─────────────────────────────────────
        # 修复：使用 state="attached" 而非默认的 state="visible"
        # 原因：Streamlit 容器可能被透明遮罩层覆盖，导致 visible 判断失败
        print(f"[{index}] 等待 Streamlit 容器就绪（最长 120 秒）…", flush=True)
        page.wait_for_selector(
            STREAMLIT_READY_SELECTOR,
            timeout=COLD_START_TIMEOUT_MS,
            state="attached"
        )
        print(f"[{index}] ✅ Streamlit 容器已就绪，WebSocket 连接已建立。", flush=True)

        print(f"[{index}] 停留 {HEARTBEAT_STAY_SECONDS} 秒，发送 WebSocket 心跳…", flush=True)
        time.sleep(HEARTBEAT_STAY_SECONDS)

        # ── 阶段截图③：最终完成截图 ──────────────────────────────────────
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
