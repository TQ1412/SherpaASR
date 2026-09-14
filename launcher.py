"""
启动器 — 双击启动语音识别系统，关闭窗口即退出
"""
import os
import sys
import threading

# --- Win7 诊断日志 ---
_DIAG_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'sherpa_diag.log' if not getattr(sys, 'frozen', False) else
                         os.path.join(os.path.dirname(sys.executable), 'sherpa_diag.log'))
def _diag(msg):
    try:
        with open(_DIAG_LOG, 'a', encoding='utf-8') as f:
            f.write(msg + '\n')
    except:
        pass

_diag(f'=== START {sys.version} ===')
_diag(f'sys.frozen={getattr(sys, "frozen", False)}')
_diag(f'sys._MEIPASS={getattr(sys, "_MEIPASS", "NOT SET")}')
_diag(f'cwd={os.getcwd()}')
_diag(f'sys.path={sys.path}')

# 最先尝试直接导入 _sherpa_onnx
try:
    import _sherpa_onnx
    _diag(f'import _sherpa_onnx: OK (file={getattr(_sherpa_onnx, "__file__", "?")})')
except Exception as e:
    _diag(f'import _sherpa_onnx: FAILED - {e}')

# 尝试导入 sherpa_onnx 包
try:
    import sherpa_onnx
    _diag(f'import sherpa_onnx: OK')
except Exception as e:
    _diag(f'import sherpa_onnx: FAILED - {e}')
# --- 诊断结束 ---


def resource_path(relative_path):
    base_path = getattr(sys, '_MEIPASS', os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base_path, relative_path)


def _flush_sessions(timeout=30.0):
    """窗口关闭后把当前录音落盘再退出。

    用户录完直接点右上角叉是最自然的操作，而 session 的保存原本只在
    WebSocket 收尾流程里做——进程一死，整通通话的 WAV 和文稿就全没了。
    这里等两个声道都断开（服务端已自行保存），再兜底补一次写盘。

    flush 会去抢 session 的锁，所以即使服务端还在写也不会写坏文件。
    """
    import time
    from main_sherpa import all_sessions_detached, flush_all_sessions

    deadline = time.time() + timeout
    while time.time() < deadline and not all_sessions_detached():
        time.sleep(0.25)
    _diag('all sessions detached, flushing...')
    flush_all_sessions()
    _diag('sessions flushed, exiting')


def main():
    if getattr(sys, 'frozen', False):
        os.chdir(os.path.dirname(sys.executable))

    import uvicorn
    from main_sherpa import app
    import webview

    def run_server():
        uvicorn.run(app, host="127.0.0.1", port=8001, log_level="warning")

    server_thread = threading.Thread(target=run_server, daemon=True)
    server_thread.start()

    # 创建桌面窗口，内嵌前端页面
    window = webview.create_window(
        title='实时语音识别系统',
        url='http://127.0.0.1:8001',
        width=1200,
        height=800,
        min_size=(900, 600),
    )
    webview.start()

    # 窗口关闭后先落盘，再退出
    try:
        _flush_sessions()
    except Exception as e:
        _diag(f'flush sessions failed: {e}')
    os._exit(0)


if __name__ == "__main__":
    main()
