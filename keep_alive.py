"""
Streamlit 应用保活脚本
使用 Playwright 启动真实 Chromium 浏览器，维持 WebSocket 连接，
防止应用进入休眠（12小时无活动触发）。

核心逻辑：
- 以 networkidle 作为"页面已加载"的唯一判断标准，不再依赖 DOM 选择器
- 用 locator + force=True 点击唤醒按钮，绕过任何遮罩/可见性限制
- 休眠应用：点击唤醒 → 等待60秒重启 → 停留发送心跳
- 活跃应用：直接停留20秒发送心跳
"""

import time
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

URLS = [
    "https://my-finance-ai.streamlit.app/",
    "https://my-movie-ai.streamlit.app/",
]

# networkidle 超时（毫秒）：等待页面完整渲染
PAGE_LOAD_TIMEOUT_MS = 60_000

# 点击唤醒按钮后等待应用重启的时间（秒）：实测60秒内完成
RESTART_WAIT_SECONDS = 60

# 应用已活跃时的心跳停留时间（秒）
HEARTBEAT_STAY_SECONDS = 20


def wake_app(page, url: str, index: int) -> bool:
    """访问单个 Streamlit 应用，处理休眠拦截页，停留维持 WebSocket 连接。"""
    print(f"\n[{index}] 正在访问：{url}", flush=True)
    try:
        # networkidle = 网络请求静止，React 已完成渲染，这是"页面已加载"的唯一信号
        # 不再依赖任何 DOM 选择器来判断加载完成
        print(f"[{index}] 等待页面完整渲染（networkidle）…", flush=True)
        page.goto(url, wait_until="networkidle", timeout=PAGE_LOAD_TIMEOUT_MS)
        print(f"[{index}] ✅ 页面渲染完成（networkidle 触发）", flush=True)
        # React 在本地完成渲染需要额外时间，不产生网络请求
        # 必须等待 3 秒让 React 把按钮画出来，否则 btn.count() 会返回 0
        page.wait_for_timeout(3000)

        # ── 阶段截图①：渲染完成后立即截图，确认页面状态 ─────────────────
        page.screenshot(path=f"screenshot_{index}_1_after_load.png")
        print(f"[{index}] 截图①已保存（页面渲染后）", flush=True)

        # ── 尝试点击唤醒按钮（用 locator + force=True，绕过遮罩/可见性）──
        # locator 比 querySelector 更强，能穿透部分 Shadow DOM
        # force=True 彻底忽略元素是否可交互，直接触发点击事件
        btn = page.locator('button[data-testid="wakeup-button-owner"], button[data-testid="wakeup-button"]')
        if btn.count() > 0:
            print(f"[{index}] 检测到休眠页，正在强制点击唤醒按钮…", flush=True)
            btn.first.click(force=True)
            print(f"[{index}] 唤醒按钮已点击，等待 {RESTART_WAIT_SECONDS} 秒让应用重启…", flush=True)

            # ── 阶段截图②：点击后10秒截图，确认唤醒是否触发 ─────────────
            time.sleep(10)
            page.screenshot(path=f"screenshot_{index}_2_after_click.png")
            print(f"[{index}] 截图②已保存（点击唤醒后10秒）", flush=True)

            # 继续等待剩余重启时间
            time.sleep(RESTART_WAIT_SECONDS - 10)
        else:
            print(f"[{index}] 未检测到休眠页，应用已处于活跃状态", flush=True)
            print(f"[{index}] 停留 {HEARTBEAT_STAY_SECONDS} 秒，发送 WebSocket 心跳…", flush=True)
            time.sleep(HEARTBEAT_STAY_SECONDS)

        # ── 阶段截图③：最终完成截图 ──────────────────────────────────────
        page.screenshot(path=f"screenshot_{index}_3_final.png")
        print(f"[{index}] 截图③已保存（最终状态）", flush=True)
        print(f"[{index}] ✅ 保活完成", flush=True)
        return True

    except PlaywrightTimeoutError:
        print(f"[{index}] ⚠️ 页面在 {PAGE_LOAD_TIMEOUT_MS // 1000} 秒内未完成渲染（networkidle 超时）", flush=True)
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
        status = "✅ 成功" if success else "⚠️ 失败"
        print(f"  {status}  {url}", flush=True)
        if not success:
            all_ok = False

    if not all_ok:
        print("\n⚠️ 部分应用的页面未能完成渲染，请检查截图排查原因。", flush=True)

    print("\n=== 脚本结束 ===", flush=True)


if __name__ == "__main__":
    main()
