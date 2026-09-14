import re
import os
import sys
import numpy as np
import logging
from config_sherpa import config

logger = logging.getLogger(__name__)

MODEL_NAME = "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-2024-07-17"
MODEL_FILENAME = f"{MODEL_NAME}.tar.bz2"
MODEL_URLS = [
    f"https://ghproxy.net/https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{MODEL_FILENAME}",
    f"https://mirror.ghproxy.com/https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{MODEL_FILENAME}",
    f"https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{MODEL_FILENAME}",
]

# 浏览器 User-Agent，避免 GitHub 限速
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
}
MODEL_DIR_NAME = MODEL_NAME  # 解压后的目录名跟压缩包同名(去掉.tar.bz2)


class ASREngine:
    """
    基于 sherpa-onnx + SenseVoice 的语音识别引擎。

    sherpa-onnx 是纯 C++ ONNX 推理，不需要 PyTorch，
    SenseVoice 是阿里达摩院的中文语音识别模型，准确率远高于 Whisper。
    """

    # 中英文标点字符集，供两遍识别使用
    PUNCT_PATTERN = re.compile(r'[，。！？、；：,.!?;:]')

    def __init__(self):
        self.num_threads = config.SHERPA_NUM_THREADS
        self.models_dir = config.MODELS_DIR
        self.model_dir = os.path.join(self.models_dir, MODEL_DIR_NAME)

        # 模型文件路径
        onnx_name = "model.int8.onnx" if config.SHERPA_USE_INT8 else "model.onnx"
        self.model_path = os.path.join(self.model_dir, onnx_name)
        self.tokens_path = os.path.join(self.model_dir, "tokens.txt")

        logger.info(f"加载 sherpa-onnx 模型: {onnx_name}")

        # 如果模型不存在，下载
        if not os.path.exists(self.model_path):
            self._download_model()
        else:
            logger.info(f"找到本地模型: {self.model_dir}")

        # 加载模型
        import sherpa_onnx

        self.recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=self.model_path,
            tokens=self.tokens_path,
            num_threads=self.num_threads,
            use_itn=True,
        )
        logger.info("sherpa-onnx 模型加载成功")

    def _download_model(self):
        """从 GitHub Release 下载并解压模型。"""
        import requests as req
        import tarfile
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        os.makedirs(self.models_dir, exist_ok=True)
        archive_path = os.path.join(self.models_dir, MODEL_FILENAME)

        for url in MODEL_URLS:
            logger.info(f"下载模型: {url}")
            try:
                resp = req.get(
                    url, stream=True, verify=False,
                    timeout=600, headers=HEADERS,
                )
                resp.raise_for_status()

                total_size = int(resp.headers.get("content-length", 0))
                downloaded = 0
                last_pct = -1

                with open(archive_path, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total_size > 0:
                            pct = downloaded * 100 // total_size
                            if pct != last_pct:
                                last_pct = pct
                                sys.stdout.write(f"\r  下载进度: {pct}%")
                                sys.stdout.flush()

                print()
                logger.info("下载完成，正在解压...")

                # 解压 tar.bz2
                with tarfile.open(archive_path, "r:bz2") as tar:
                    tar.extractall(path=self.models_dir)

                os.remove(archive_path)
                logger.info(f"模型解压完成: {self.model_dir}")
                return  # 下载成功

            except Exception as e:
                logger.warning(f"下载失败: {url}\n  {e}")
                if os.path.exists(archive_path):
                    os.remove(archive_path)
                continue

        # 所有地址都失败，提示手动下载
        print()
        manual_url = ("https://github.com/k2-fsa/sherpa-onnx/releases/download/"
                      f"asr-models/{MODEL_FILENAME}")
        logger.error(
            f"\n{'='*60}\n"
            f" 模型下载失败，请手动下载并解压：\n\n"
            f"  1. 用浏览器打开：\n"
            f"     {manual_url}\n\n"
            f"  2. 下载后将 {MODEL_FILENAME} 解压到\n"
            f"     {os.path.abspath(self.models_dir)}/ 目录下\n\n"
            f"  3. 确保目录结构为：\n"
            f"     {os.path.abspath(self.model_dir)}/\n"
            f"       ├── model.onnx\n"
            f"       ├── model.int8.onnx\n"
            f"       └── tokens.txt\n\n"
            f"  4. 然后重新运行 python main_sherpa.py\n"
            f"{'='*60}"
        )
        raise RuntimeError(f"模型下载失败，请手动下载 {MODEL_FILENAME}")

    def transcribe(self, audio_data: bytes, sample_rate: int = 16000,
                   strip_punct: bool = False) -> str:
        try:
            duration = len(audio_data) / (sample_rate * 2)
            logger.debug(f"识别音频: {len(audio_data)} bytes, 时长: {duration:.2f}s")

            # PCM Int16 → Float32 [-1, 1]
            audio_array = (
                np.frombuffer(audio_data, dtype=np.int16).astype(np.float32)
                / 32768.0
            )

            # sherpa-onnx 流式推理
            stream = self.recognizer.create_stream()
            stream.accept_waveform(sample_rate, audio_array)
            self.recognizer.decode_stream(stream)
            text = stream.result.text

            # 清洗输出
            text = self._clean_output(text, strip_punct=strip_punct)

            return text

        except Exception as e:
            logger.error(f"ASR 识别错误: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return ""

    def _clean_output(self, text: str, strip_punct: bool = False) -> str:
        """清洗 SenseVoice 输出中的标签和多余的空白字符。"""
        # 去掉 <|zh|>, <|NEUTRAL|>, <|laughter|> 等标签
        text = re.sub(r'<\s*\|\s*[^|]*?\s*\|\s*>', '', text)
        # 去掉所有空白（中文不需要空格）
        text = re.sub(r'\s+', '', text)

        if strip_punct:
            # 第一遍：去掉所有中英文标点，只保留纯文本
            text = self.PUNCT_PATTERN.sub('', text)
        else:
            # 第二遍：合并连续重复标点
            text = re.sub(r'([，。！？、；：,.!?;:]){2,}', r'\1', text)

        # 去掉开头的标点
        text = re.sub(r'^[，。！？、；：,.!?;:]+', '', text)
        return text.strip()

    @staticmethod
    def format_sentences(text: str) -> str:
        """将连续文本按句子分行，句子之间空一行。"""
        if not text:
            return text
        text = re.sub(r'([。！？])(?!\n)', r'\1\n\n', text)
        text = re.sub(r'([.!?])\s+(?=[一-鿿A-Z])', r'\1\n\n', text)
        text = re.sub(r'\n{3,}', '\n\n', text)
        return text.strip()


asr_engine = ASREngine()
