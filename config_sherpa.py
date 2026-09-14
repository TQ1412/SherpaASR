import logging
import os
import sys

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


def base_dir() -> str:
    """打包后为 exe 所在目录，开发时为脚本所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


_BASE = base_dir()

# 用绝对路径加载，否则从别的目录启动会读不到配置
load_dotenv(os.path.join(_BASE, ".env.sherpa"))


def _path(env_value, default: str):
    """相对路径一律按程序所在目录解析，不依赖运行时 cwd；都没给返回 None。"""
    raw = env_value or default
    if not raw:
        return None
    if not os.path.isabs(raw):
        raw = os.path.join(_BASE, raw)
    return os.path.abspath(raw)


def _is_writable_dir(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        probe = os.path.join(path, ".write_probe")
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def _default_save_dir() -> str:
    """默认把录音存在程序目录下的 results。

    装到 Program Files 时普通用户写不进去，那样会出现"录音和转写都正常、
    落盘静默失败"的情况，所以这里先探一下可写性，不可写就退到文档目录。
    要固定位置的话，直接在 .env.sherpa 里设 SAVE_DIR。
    """
    preferred = os.path.join(_BASE, "results")
    if _is_writable_dir(preferred):
        return preferred

    home = os.path.expanduser("~")
    docs = os.path.join(home, "Documents")
    # 文档目录可能被 OneDrive 重定向或不存在，那就在本地应用数据目录下
    fallback = os.path.join(
        docs if os.path.isdir(docs) else (os.environ.get("LOCALAPPDATA") or home),
        "SherpaASR", "results",
    )
    logger.warning("程序目录不可写(%s)，录音改存到: %s", preferred, fallback)
    return fallback


class Config:
    # sherpa-onnx 模型配置
    SHERPA_NUM_THREADS = int(os.getenv("SHERPA_NUM_THREADS", "4"))
    SHERPA_USE_INT8 = os.getenv("SHERPA_USE_INT8", "true").lower() == "true"  # int8 量化版速度更快

    # 模型存放路径（默认程序同级 models，可改为绝对路径如 D:/models）
    MODELS_DIR = _path(os.getenv("MODELS_DIR"), "models")

    # 音频配置
    SAMPLE_RATE = 16000
    CHUNK_SECONDS = float(os.getenv("CHUNK_SECONDS", "3.0"))

    # 降噪（默认关闭）
    ENABLE_NOISE = os.getenv("ENABLE_NOISE", "false").lower() == "true"
    NOISE_REDUCTION_STRENGTH = float(os.getenv("NOISE_REDUCTION_STRENGTH", "0.3"))
    NOISE_GATE_THRESHOLD = float(os.getenv("NOISE_GATE_THRESHOLD", "0.01"))

    # VAD
    VAD_AGGRESSIVENESS = int(os.getenv("VAD_AGGRESSIVENESS", "2"))
    VAD_FRAME_DURATION = int(os.getenv("VAD_FRAME_DURATION", "30"))

    # 第二遍修正：静音边界检测
    SILENCE_CHUNKS_FOR_BOUNDARY = int(os.getenv("SILENCE_CHUNKS_FOR_BOUNDARY", "2"))  # 连续 N 个静音 chunk 视为话语边界
    MAX_UTTERANCE_CHUNKS = int(os.getenv("MAX_UTTERANCE_CHUNKS", "10"))  # 单个话语段最大 chunk 数

    # 录音保存：显式配置优先，否则程序目录 results，不可写则退到文档目录
    SAVE_DIR = _path(os.getenv("SAVE_DIR"), None) or _default_save_dir()


config = Config()
