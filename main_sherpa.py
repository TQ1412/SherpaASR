"""
实时语音识别系统 — sherpa-onnx (SenseVoice) 版本
运行: python main_sherpa.py
"""

import logging
import os
import struct
import sys
import threading
import io
import asyncio
import time
from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import uvicorn
from pydub import AudioSegment
from concurrent.futures import ThreadPoolExecutor

from vad import VoiceActivityDetector
from asr_sherpa import asr_engine
from denoise import noise_reducer
from config_sherpa import config

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="实时语音识别系统 (sherpa-onnx)")

chunk_executor = ThreadPoolExecutor(max_workers=2)
merge_executor = ThreadPoolExecutor(max_workers=1)

# 前端页面与本服务同源（均由 127.0.0.1:8001 提供），无需 CORS 中间件。


def base_dir() -> str:
    """打包后为 exe 所在目录，开发时为脚本所在目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


vad = VoiceActivityDetector(
    aggressiveness=config.VAD_AGGRESSIVENESS,
    sample_rate=config.SAMPLE_RATE,
    frame_duration=config.VAD_FRAME_DURATION,
)

# 录音保存 — 全局 session 管理
sessions: dict = {}
_save_dir = os.path.abspath(config.SAVE_DIR)
_noise_version = 0  # 降噪配置变更时自增，通知 ws 重置缓冲区


def validate_save_dir(raw: str) -> str:
    """校验并返回可写的绝对目录路径，失败抛 ValueError。"""
    if not raw or not raw.strip():
        raise ValueError("保存目录不能为空")
    path = os.path.abspath(os.path.expandvars(os.path.expanduser(raw.strip())))
    if os.path.exists(path) and not os.path.isdir(path):
        raise ValueError(f"该路径已存在且不是目录：{path}")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as e:
        raise ValueError(f"无法创建目录：{e}")
    probe = os.path.join(path, ".write_probe")
    try:
        with open(probe, "w", encoding="utf-8") as f:
            f.write("ok")
        os.remove(probe)
    except OSError as e:
        raise ValueError(f"目录不可写：{e}")
    return path


class RecordingSession:
    """管理一个录音 session 下各声道的音频和识别结果。

    两个声道（客服/客户）共用同一个 session 对象，因此按活动连接数决定
    何时落盘：只有两个声道都断开才算录完，否则先断的那个会把只录了一半
    的结果定稿。

    add_audio / record_* 由事件循环线程调用，save 可能被事件循环线程和
    启动器的主线程同时调用（关窗时），因此全部加锁。
    """

    def __init__(self, session_id: str, save_dir: str):
        self.session_id = session_id
        self.save_dir = save_dir
        self.channels: dict = {}  # channel_name -> {audio, chunk_texts, merge_ranges, max_chunk_id}
        self._lock = threading.RLock()
        self._conns = 0

    @property
    def detached(self) -> bool:
        """是否所有声道都已断开。"""
        with self._lock:
            return self._conns == 0

    def attach(self):
        with self._lock:
            self._conns += 1

    def detach(self) -> bool:
        """声道断开。返回是否已无活动连接（可以落盘）。"""
        with self._lock:
            self._conns = max(0, self._conns - 1)
            return self._conns == 0

    def _ensure_channel(self, channel: str):
        if channel not in self.channels:
            self.channels[channel] = {
                "audio": bytearray(),
                "chunk_texts": {},
                "merge_ranges": {},
                "max_chunk_id": 0,
            }

    def add_audio(self, channel: str, data: bytes):
        with self._lock:
            self._ensure_channel(channel)
            self.channels[channel]["audio"].extend(data)

    def record_result(self, channel: str, chunk_id: int, text: str):
        with self._lock:
            self._ensure_channel(channel)
            ch = self.channels[channel]
            ch["chunk_texts"][chunk_id] = text
            ch["max_chunk_id"] = max(ch["max_chunk_id"], chunk_id)

    def record_update(self, channel: str, start_chunk: int, end_chunk: int, text: str):
        with self._lock:
            self._ensure_channel(channel)
            key = f"{start_chunk}-{end_chunk}"
            self.channels[channel]["merge_ranges"][key] = text

    def _assemble_segments(self, channel: str) -> list:
        """按时间戳组装结构化识别结果。"""
        ch = self.channels.get(channel)
        if not ch:
            return []
        chunk_s = config.CHUNK_SECONDS
        segments = []
        i = 1
        while i <= ch["max_chunk_id"]:
            merged = False
            for key, merge_text in ch["merge_ranges"].items():
                start_c, end_c = key.split("-")
                if int(start_c) == i:
                    if merge_text:
                        segments.append({
                            "start": round((i - 1) * chunk_s, 2),
                            "end": round(int(end_c) * chunk_s, 2),
                            "text": merge_text,
                            "source": "merge",
                        })
                    i = int(end_c) + 1
                    merged = True
                    break
            if not merged:
                text = ch["chunk_texts"].get(i, "")
                if text:
                    segments.append({
                        "start": round((i - 1) * chunk_s, 2),
                        "end": round(i * chunk_s, 2),
                        "text": text,
                        "source": "chunk",
                    })
                i += 1
        return segments

    def _assemble_text(self, channel: str) -> str:
        raw = "".join(seg["text"] for seg in self._assemble_segments(channel))
        return asr_engine.format_sentences(raw)

    def save(self):
        """把当前 session 落盘。

        可重复调用，后写的会覆盖先写的——单声道提前断开时可能写出不完整的
        结果，靠关窗前的最后一次 flush 校正。
        """
        import json

        with self._lock:
            if not self.channels:
                return

            session_dir = os.path.join(self.save_dir, self.session_id)
            os.makedirs(session_dir, exist_ok=True)

            for ch_name, ch in self.channels.items():
                audio_bytes = bytes(ch["audio"])
                if len(audio_bytes) == 0:
                    continue

                # 写 WAV 文件
                wav_path = os.path.join(session_dir, f"{ch_name}.wav")
                self._write_wav(wav_path, audio_bytes)

                # 写 txt（纯文本，方便阅读）
                text = self._assemble_text(ch_name)
                txt_path = os.path.join(session_dir, f"{ch_name}.txt")
                with open(txt_path, "w", encoding="utf-8") as f:
                    f.write(text)

                # 写 json（带时间戳的结构化结果）
                segments = self._assemble_segments(ch_name)
                json_path = os.path.join(session_dir, f"{ch_name}.json")
                with open(json_path, "w", encoding="utf-8") as f:
                    json.dump({
                        "session_id": self.session_id,
                        "channel": ch_name,
                        "sample_rate": config.SAMPLE_RATE,
                        "chunk_seconds": config.CHUNK_SECONDS,
                        "segments": segments,
                    }, f, ensure_ascii=False, indent=2)

                logger.info(
                    "已保存 %s: wav=%d bytes, text=%d chars → %s",
                    ch_name, len(audio_bytes), len(text), session_dir,
                )

    def _write_wav(self, path: str, pcm_data: bytes):
        sample_rate = config.SAMPLE_RATE
        num_channels = 1
        bits_per_sample = 16
        byte_rate = sample_rate * num_channels * bits_per_sample // 8
        block_align = num_channels * bits_per_sample // 8
        data_size = len(pcm_data)
        riff_size = 36 + data_size

        with open(path, "wb") as f:
            f.write(b"RIFF")
            f.write(struct.pack("<I", riff_size))
            f.write(b"WAVE")
            f.write(b"fmt ")
            f.write(struct.pack("<I", 16))  # fmt chunk size
            f.write(struct.pack("<H", 1))   # PCM format
            f.write(struct.pack("<H", num_channels))
            f.write(struct.pack("<I", sample_rate))
            f.write(struct.pack("<I", byte_rate))
            f.write(struct.pack("<H", block_align))
            f.write(struct.pack("<H", bits_per_sample))
            f.write(b"data")
            f.write(struct.pack("<I", data_size))
            f.write(pcm_data)


def all_sessions_detached() -> bool:
    """所有 session 是否都已无活动连接（服务端可以安全落盘）。"""
    return all(s.detached for s in list(sessions.values()))


def flush_all_sessions():
    """把内存中的 session 全部落盘。

    由启动器在窗口关闭后从主线程调用。此时前端已断开，不再有新音频，
    直接取快照写盘即可；这一步是整通录音的最后一道保险。
    """
    for sid, sess in list(sessions.items()):
        try:
            sess.save()
        except Exception as e:
            logger.error("保存 session %s 失败: %s", sid, e)


class SaveConfigRequest(BaseModel):
    save_dir: str = Field(..., description="录音保存目录路径")


class SaveConfigResponse(BaseModel):
    save_dir: str


class UtteranceTracker:
    """追踪静音边界，输出完整话语段用于第二遍标点修正。

    状态机: WAITING_SPEECH → IN_UTTERANCE → (连续静音) → FLUSH → WAITING_SPEECH
    只缓冲有语音的 chunk，静音 chunk 不计入话语音频（提升 ASR 质量）。
    """

    def __init__(self, silence_threshold: int = 2, max_chunks: int = 10):
        self.buffer: list = []          # [(chunk_data, chunk_id), ...]
        self.consecutive_silence = 0
        self.silence_threshold = silence_threshold
        self.max_chunks = max_chunks

    def add_chunk(self, chunk_data: bytes, chunk_id: int,
                  has_speech: bool):
        """处理一个 chunk。若检测到话语边界则返回 (audio, start, end)，否则 None。"""
        if has_speech:
            self.buffer.append((chunk_data, chunk_id))
            self.consecutive_silence = 0
            if len(self.buffer) >= self.max_chunks:
                return self._flush()
            return None
        else:
            if self.buffer:
                self.consecutive_silence += 1
                if self.consecutive_silence >= self.silence_threshold:
                    return self._flush()
            return None

    def _flush(self):
        if not self.buffer:
            return None
        audio = b''.join(c[0] for c in self.buffer)
        start_chunk = self.buffer[0][1]
        end_chunk = self.buffer[-1][1]
        self.buffer.clear()
        self.consecutive_silence = 0
        return (audio, start_chunk, end_chunk)

    def flush_remaining(self):
        """强制排空残留话语段（断开/结束时调用）。"""
        return self._flush()


# ---------------------------------------------------------------------------
# GET /
# ---------------------------------------------------------------------------
@app.get("/")
async def root():
    with open(os.path.join(base_dir(), "index.html"), "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


# ---------------------------------------------------------------------------
# WebSocket /ws/transcribe  — 文件上传识别
# ---------------------------------------------------------------------------
@app.websocket("/ws/transcribe")
async def websocket_transcribe(websocket: WebSocket):
    await websocket.accept()
    logger.info("WebSocket /ws/transcribe 连接已建立")

    try:
        data = await websocket.receive_json()
        file_data_array = data.get("file_data")
        file_name = data.get("file_name", "unknown.wav")

        file_data = bytes(file_data_array)
        audio = AudioSegment.from_file(io.BytesIO(file_data))
        if audio.channels > 1:
            audio = audio.set_channels(1)
        if audio.frame_rate != config.SAMPLE_RATE:
            audio = audio.set_frame_rate(config.SAMPLE_RATE)

        audio_data = audio.raw_data
        chunk_size = int(config.SAMPLE_RATE * config.CHUNK_SECONDS * 2)
        total_chunks = (len(audio_data) + chunk_size - 1) // chunk_size

        logger.info(f"共 {total_chunks} 个音频块")

        loop = asyncio.get_running_loop()
        tracker = UtteranceTracker(
            silence_threshold=config.SILENCE_CHUNKS_FOR_BOUNDARY,
            max_chunks=config.MAX_UTTERANCE_CHUNKS,
        )

        for idx in range(total_chunks):
            start = idx * chunk_size
            end = min(start + chunk_size, len(audio_data))
            chunk = audio_data[start:end]
            if len(chunk) < chunk_size:
                chunk = chunk + b'\x00' * (chunk_size - len(chunk))

            chunk = noise_reducer.reduce_noise_advanced(chunk)
            t0 = time.time()

            # 第一遍：无标点即时结果
            text = await loop.run_in_executor(
                chunk_executor, asr_engine.transcribe, chunk, 16000, True
            )
            if text:
                await websocket.send_json({
                    "type": "result",
                    "chunk_id": idx + 1,
                    "text": text,
                    "total": total_chunks,
                })

            # 第二遍：静音边界触发话语段标点修正
            if vad.is_available:
                speech_frames = vad.detect_speech(chunk)
                has_speech = sum(speech_frames) > len(speech_frames) * 0.2
            else:
                has_speech = True

            utterance = tracker.add_chunk(chunk, idx + 1, has_speech)
            if utterance:
                audio, start_c, end_c = utterance
                merged_text = await loop.run_in_executor(
                    merge_executor, asr_engine.transcribe, audio, 16000, False
                )
                if merged_text:
                    await websocket.send_json({
                        "type": "update",
                        "start_chunk": start_c,
                        "end_chunk": end_c,
                        "text": merged_text,
                    })

            elapsed = time.time() - t0
            wait = max(0, config.CHUNK_SECONDS - elapsed)
            if wait > 0:
                await asyncio.sleep(wait)

        # 排空残留话语段
        remaining = tracker.flush_remaining()
        if remaining:
            audio, start_c, end_c = remaining
            merged_text = await loop.run_in_executor(
                merge_executor, asr_engine.transcribe, audio, 16000, False
            )
            if merged_text:
                await websocket.send_json({
                    "type": "update",
                    "start_chunk": start_c,
                    "end_chunk": end_c,
                    "text": merged_text,
                })

        await websocket.send_json({
            "type": "complete",
            "message": "识别完成",
            "segments": total_chunks,
        })

    except WebSocketDisconnect:
        logger.info("WebSocket 连接已断开")
    except Exception as e:
        logger.error(f"WebSocket /ws/transcribe 错误: {e}")
        try:
            await websocket.send_json({"type": "error", "message": str(e)})
        except Exception:
            pass


# ---------------------------------------------------------------------------
# WebSocket /ws  — 实时麦克风录音
# ---------------------------------------------------------------------------
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    # 采样率握手：前端把 AudioContext 的实际采样率带上来，
    # 对不上就直接拒绝——否则会按 16k 解码非 16k 音频，转写全是乱码。
    rate = websocket.query_params.get("rate")
    if rate:
        try:
            rate_matches = int(rate) == config.SAMPLE_RATE
        except ValueError:
            rate_matches = False
        if not rate_matches:
            await websocket.accept()
            logger.error("采样率不匹配: 前端 %s Hz, 后端 %s Hz", rate, config.SAMPLE_RATE)
            await websocket.send_json({
                "type": "error",
                "message": f"采样率不匹配（前端 {rate}Hz / 后端 {config.SAMPLE_RATE}Hz），"
                           f"识别结果会失真，请检查浏览器音频设备设置",
            })
            await websocket.close()
            return

    await websocket.accept()
    channel = websocket.query_params.get("channel", "default")
    session_id = websocket.query_params.get("session", "")

    # 注册 session
    if session_id:
        if session_id not in sessions:
            sessions[session_id] = RecordingSession(session_id, _save_dir)
        session = sessions[session_id]
        session.attach()
    else:
        session = None

    logger.info(f"WebSocket /ws 连接已建立 (channel={channel}, session={session_id or '无'})")

    chunk_size = int(config.SAMPLE_RATE * config.CHUNK_SECONDS * 2)

    audio_queue = asyncio.Queue()
    current_chunk = b''
    chunk_index = 0

    result_queue = asyncio.Queue()
    expected_index = 1

    merge_result_queue = asyncio.Queue()
    merge_expected_index = 1

    utterance_tracker = UtteranceTracker(
        silence_threshold=config.SILENCE_CHUNKS_FOR_BOUNDARY,
        max_chunks=config.MAX_UTTERANCE_CHUNKS,
    )
    utterance_batch_counter = 1

    # 在途识别任务：收尾时要等它们把结果写进 session，否则最后几块会丢
    inflight = set()
    active_tasks = set()

    def spawn(coro):
        task = asyncio.create_task(coro)
        inflight.add(task)
        task.add_done_callback(inflight.discard)
        return task

    async def process_chunk(chunk_data: bytes, chunk_id: int):
        """第一遍：单块无标点识别。

        结果先写进 session 再入队上屏，这样 socket 中途断开也不会丢掉
        已经识别出来的内容；无论成功失败都必须入队，否则保序队列会永远
        卡在这个 chunk 上，此后整个声道不再上屏。
        """
        text = ""
        try:
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(
                chunk_executor, asr_engine.transcribe, chunk_data, 16000, True
            )
        except Exception as e:
            logger.error(f"块 {chunk_id} 识别失败: {e}")
        if text and session:
            try:
                session.record_result(channel, chunk_id, text)
            except Exception as e:
                logger.error(f"块 {chunk_id} 写入 session 失败: {e}")
        await result_queue.put({"index": chunk_id, "text": text})

    async def process_utterance(batch_audio: bytes, batch_id: int,
                                start_chunk: int, end_chunk: int):
        """第二遍：整句带标点重识别，覆盖同一区间的第一遍结果。"""
        text = ""
        try:
            loop = asyncio.get_running_loop()
            text = await loop.run_in_executor(
                merge_executor, asr_engine.transcribe, batch_audio, 16000, False
            )
        except Exception as e:
            logger.error(f"话语段 {batch_id} 修正失败: {e}")
        if text and session:
            try:
                session.record_update(channel, start_chunk, end_chunk, text)
            except Exception as e:
                logger.error(f"话语段 {batch_id} 写入 session 失败: {e}")
        await merge_result_queue.put({
            "index": batch_id,
            "start_chunk": start_chunk,
            "end_chunk": end_chunk,
            "text": text,
        })

    async def send_results():
        """按 chunk 顺序上屏。

        识别是并发跑的，结果到达顺序不定，乱序的先缓存下来，等前面的
        凑齐再连续吐出去。（原先的做法是塞回队列尾再 sleep 10ms 重试，
        队列里只剩它一条时会以 100Hz 空转。）
        """
        nonlocal expected_index
        pending = {}
        while True:
            try:
                item = await result_queue.get()
                pending[item["index"]] = item
                while expected_index in pending:
                    cur = pending.pop(expected_index)
                    if cur["text"]:
                        await websocket.send_json({
                            "type": "result",
                            "chunk_id": cur["index"],
                            "text": cur["text"],
                        })
                    expected_index += 1
            except Exception:
                break

    async def send_merge_results():
        """按话语段顺序下发带标点的修正结果，同样需要保序。"""
        nonlocal merge_expected_index
        pending = {}
        while True:
            try:
                item = await merge_result_queue.get()
                pending[item["index"]] = item
                while merge_expected_index in pending:
                    cur = pending.pop(merge_expected_index)
                    if cur["text"]:
                        await websocket.send_json({
                            "type": "update",
                            "start_chunk": cur["start_chunk"],
                            "end_chunk": cur["end_chunk"],
                            "text": cur["text"],
                        })
                    merge_expected_index += 1
            except Exception:
                break

    async def process_audio_queue():
        """消费音频块：降噪 → VAD → 第一遍即时识别 + 第二遍整句修正。

        降噪和 VAD 的异常都在本块内兜住：它们抛出去会让这个 chunk 永远
        产生不了结果，保序队列就卡死了。
        """
        nonlocal chunk_index, utterance_batch_counter
        noise_version = _noise_version
        while True:
            try:
                chunk = await audio_queue.get()

                # 降噪参数变更时清空话语追踪器
                if noise_version != _noise_version:
                    noise_version = _noise_version
                    utterance_tracker.flush_remaining()

                chunk_index += 1
                idx = chunk_index

                try:
                    denoised = noise_reducer.reduce_noise_advanced(chunk)
                except Exception as e:
                    logger.error(f"块 {idx} 降噪失败，改用原始音频: {e}")
                    denoised = chunk

                # VAD：检测是否包含语音（仅 webrtcvad 可用时生效）
                has_speech = True
                if vad.is_available:
                    try:
                        speech_frames = vad.detect_speech(denoised)
                        has_speech = sum(speech_frames) > len(speech_frames) * 0.2
                    except Exception as e:
                        logger.error(f"块 {idx} VAD 失败，按有语音处理: {e}")

                # === 第一遍：即时无标点识别 ===
                if has_speech:
                    spawn(process_chunk(denoised, idx))
                else:
                    await result_queue.put({"index": idx, "text": ""})

                # === 第二遍：静音边界触发话语段标点修正 ===
                utterance = utterance_tracker.add_chunk(denoised, idx, has_speech)
                if utterance:
                    audio, start_c, end_c = utterance
                    batch_id = utterance_batch_counter
                    utterance_batch_counter += 1
                    spawn(process_utterance(audio, batch_id, start_c, end_c))
            except Exception as e:
                logger.error(f"处理音频块出错: {e}")

    t1 = asyncio.create_task(send_results())
    t2 = asyncio.create_task(send_merge_results())
    t3 = asyncio.create_task(process_audio_queue())
    active_tasks.update([t1, t2, t3])

    try:
        while True:
            data = await websocket.receive_bytes()
            current_chunk += data

            # 累积音频到 session
            if session:
                session.add_audio(channel, data)

            while len(current_chunk) >= chunk_size:
                chunk = current_chunk[:chunk_size]
                current_chunk = current_chunk[chunk_size:]
                await audio_queue.put(chunk)

    except WebSocketDisconnect:
        logger.info("WebSocket /ws 连接已断开")
    except Exception as e:
        logger.error(f"WebSocket /ws 错误: {e}")
    finally:
        # 剩余未满一块的音频也记入 session
        if session and current_chunk:
            session.add_audio(channel, current_chunk)

        # 排空残留话语段（尾音无静音的情况）
        remaining_utterance = utterance_tracker.flush_remaining()
        if remaining_utterance:
            audio, start_c, end_c = remaining_utterance
            try:
                loop = asyncio.get_running_loop()
                text = await loop.run_in_executor(
                    merge_executor, asr_engine.transcribe, audio, 16000, False
                )
                if text and session:
                    session.record_update(channel, start_c, end_c, text)
            except Exception as e:
                logger.error("尾段修正失败: %s", e)

        # 先停掉生产者，再等在途识别把结果写进 session，最后才落盘。
        # 否则关窗时最后几块已识别的内容会丢。
        if not t3.done():
            t3.cancel()
        deadline = time.time() + 20
        while inflight and time.time() < deadline:
            await asyncio.wait(set(inflight), timeout=max(0.0, deadline - time.time()))
        if inflight:
            logger.warning("仍有 %d 个识别任务未完成，结果可能不完整", len(inflight))

        # 保存 session 文件（两个声道都断开时才算录完，见 detach）。
        # 写成功后就把 session 丢掉，否则 sessions 只增不减，坐席连一天
        # 内存会持续涨，关窗时还要把历史 session 全部重写一遍。
        if session:
            try:
                if session.detach():
                    session.save()
                    sessions.pop(session_id, None)
            except Exception as e:
                logger.error("保存 session 失败: %s", e)

        for task in active_tasks:
            if not task.done():
                task.cancel()
        logger.info("所有任务已清理")


# ---------------------------------------------------------------------------
# 保存路径 API
# ---------------------------------------------------------------------------
@app.get("/api/save-config", response_model=SaveConfigResponse)
async def get_save_config():
    return {"save_dir": _save_dir}


@app.put("/api/save-config", response_model=SaveConfigResponse)
async def update_save_config(req: SaveConfigRequest):
    global _save_dir
    try:
        _save_dir = validate_save_dir(req.save_dir)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    logger.info("保存目录已更新: %s", _save_dir)
    return {"save_dir": _save_dir}


# ---------------------------------------------------------------------------
# 降噪配置 API
# ---------------------------------------------------------------------------
class NoiseConfigRequest(BaseModel):
    enabled: bool = False
    reduction_strength: float = Field(default=0.3, ge=0.0, le=1.0)
    gate_threshold: float = Field(default=0.01, ge=0.0, le=1.0)


@app.get("/noise/config")
async def get_noise_config():
    return noise_reducer.get_config()


@app.put("/noise/config")
async def update_noise_config(req: NoiseConfigRequest):
    global _noise_version
    noise_reducer.set_enabled(req.enabled)
    noise_reducer.set_reduction_strength(req.reduction_strength)
    noise_reducer.set_gate_threshold(req.gate_threshold)
    _noise_version += 1
    logger.info("降噪配置已更新: %s (v=%d)", req.model_dump(), _noise_version)
    return noise_reducer.get_config()


if __name__ == "__main__":
    # 只监听回环地址：这是本地录音工具，且 /api/save-config 能改落盘目录，
    # 绑 0.0.0.0 会把通话录音和转写内容暴露给整个局域网。
    uvicorn.run(app, host="127.0.0.1", port=8001)
