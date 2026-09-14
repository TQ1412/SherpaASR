import logging

import numpy as np
import noisereduce as nr

from config_sherpa import config

logger = logging.getLogger(__name__)


class NoiseReducer:
    """逐块降噪 + 噪音门。

    注意：降噪是在 3s 块上独立做的，块与块之间谱估计不连续，边界会有
    突变；而第二遍整句识别会把这些块拼回一整句，等于把拼接伪影喂给了
    精度更高的那一遍。默认关闭（ENABLE_NOISE=false）。开启后如果发现
    整句结果反而变差，优先怀疑这里——正解是把降噪移到整句层面。
    """

    def __init__(self, sample_rate=16000):
        self.sample_rate = sample_rate
        self.enabled = config.ENABLE_NOISE
        self.reduction_strength = config.NOISE_REDUCTION_STRENGTH
        self.gate_threshold = config.NOISE_GATE_THRESHOLD

        if self.enabled:
            logger.info(
                "降噪器初始化完成，采样率: %dHz, 强度: %.2f, 阈值: %.3f",
                sample_rate, self.reduction_strength, self.gate_threshold,
            )
        else:
            logger.info("降噪器已禁用")

    def reduce_noise_advanced(self, audio_data: bytes) -> bytes:
        """降噪 + 噪音门。任何失败都原样返回输入，不影响识别主流程。"""
        if not self.enabled:
            return audio_data

        try:
            audio_float = np.frombuffer(audio_data, dtype=np.int16).astype(np.float32) / 32768.0

            reduced_noise = nr.reduce_noise(
                y=audio_float,
                sr=self.sample_rate,
                stationary=False,
                prop_decrease=self.reduction_strength,
                n_fft=1024,
                win_length=512,
                hop_length=256,
            )

            # 噪音门：压掉低音量部分
            mask = np.abs(reduced_noise) > self.gate_threshold
            reduced_noise = reduced_noise * mask

            return (reduced_noise * 32768.0).astype(np.int16).tobytes()

        except Exception as e:
            logger.error(f"降噪处理失败，使用原始音频: {e}")
            return audio_data

    def set_enabled(self, enabled: bool):
        """启用或禁用降噪。"""
        self.enabled = enabled
        logger.info("降噪已%s", "启用" if enabled else "禁用")

    def set_reduction_strength(self, strength: float):
        """设置降噪强度 (0.0 ~ 1.0)。"""
        self.reduction_strength = max(0.0, min(1.0, strength))
        logger.info("降噪强度设置为: %.2f", self.reduction_strength)

    def set_gate_threshold(self, threshold: float):
        """设置噪音门阈值 (0.0 ~ 1.0)。"""
        self.gate_threshold = max(0.0, min(1.0, threshold))
        logger.info("噪音门阈值设置为: %.3f", self.gate_threshold)

    def get_config(self) -> dict:
        """返回当前降噪配置。"""
        return {
            "enabled": self.enabled,
            "reduction_strength": self.reduction_strength,
            "gate_threshold": self.gate_threshold,
        }


# 创建全局降噪器实例
noise_reducer = NoiseReducer(sample_rate=16000)
