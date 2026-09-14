import numpy as np
import logging

try:
    from webrtcvad import Vad
    HAVE_WEBRTCVAD = True
except ImportError:
    HAVE_WEBRTCVAD = False

logger = logging.getLogger(__name__)


class _DummyVad:
    """webrtcvad 不可用时的降级替代，始终返回 False（无语音）。"""
    def is_speech(self, frame, sample_rate):
        return False


class VoiceActivityDetector:
    def __init__(self, aggressiveness=2, sample_rate=16000, frame_duration=30):
        self.sample_rate = sample_rate
        self.frame_duration = frame_duration
        self.frame_size = int(sample_rate * frame_duration / 1000)

        if HAVE_WEBRTCVAD:
            self.vad = Vad(aggressiveness)
            self.is_available = True
            logger.info(f"VAD 初始化完成 (aggressiveness={aggressiveness})")
        else:
            self.vad = _DummyVad()
            self.is_available = False
            logger.warning("webrtcvad 未安装，VAD 已禁用")
        
    def detect_speech(self, audio_data):
        """
        检测语音活动
        
        Args:
            audio_data: 音频数据 (numpy array 或 bytes)
            
        Returns:
            list: 每帧是否包含语音的布尔值列表
        """
        if isinstance(audio_data, bytes):
            audio_data = np.frombuffer(audio_data, dtype=np.int16)
            
        frames = []
        for i in range(0, len(audio_data), self.frame_size):
            frame = audio_data[i:i + self.frame_size]
            if len(frame) < self.frame_size:
                frame = np.pad(frame, (0, self.frame_size - len(frame)), 'constant')
            frames.append(frame.tobytes())
            
        results = []
        for frame in frames:
            try:
                is_speech = self.vad.is_speech(frame, self.sample_rate)
                results.append(is_speech)
            except Exception as e:
                logger.error(f"VAD 检测错误: {e}")
                results.append(False)

        return results