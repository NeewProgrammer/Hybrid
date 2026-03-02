import os
import time
import json
import re
import ffmpeg
import requests
import gc
import concurrent.futures
import threading
import torch
import logging
import wave
import numpy as np
import base64
from pathlib import Path
from PySide6.QtCore import QThread, Signal, QObject

# 尝试导入 funasr，如果失败则在运行时报错
try:
    from funasr import AutoModel
except ImportError:
    AutoModel = None

from utils import format_timestamp, get_xf_sign

logger = logging.getLogger(__name__)

class WorkerSignals(QObject):
    """
    定义跨线程信号
    """
    progress = Signal(str, int)  # (Message, Percentage)
    error = Signal(str)
    finished = Signal(str)       # Output SRT path
    log = Signal(str)

class SubtitleWorker(QThread):
    """
    负责字幕生成核心流水的后台线程类。
    完整流水线：
    1. FFmpeg 提取音频（WAV）
    2. VAD（语音活动检测）剔除静音
    3. SenseVoice（语音识别）生成带时间戳的富文本
    4. 翻译引擎（Gemini 或 讯飞）处理识别文本
    5. 生成最终的双语 SRT 字幕文件
    """
    def __init__(self, video_path, config):
        """
        初始化工作线程
        @param video_path 视频文件的绝对路径
        @param config 字典配置，包含 app_id, api_key, api_secret, provider, gemini_api_key 等
        """
        super().__init__()
        self.video_path = video_path
        self.config = config
        self.signals = WorkerSignals()
        self._is_cancelled = False

    def cancel(self):
        self._is_cancelled = True

    def run(self):
        temp_wav = None
        model = None
        try:
            self.signals.log.emit(f"开始处理: {self.video_path}")
            
            # 1. 提取音频
            if self._is_cancelled: return
            self.signals.progress.emit("正在提取音频...", 5)
            temp_wav = self.extract_audio(self.video_path)
            self.signals.log.emit(f"音频已提取: {temp_wav}")

            # 2. 本地 ASR 识别 (VAD + ASR 手动流水线)
            if self._is_cancelled: return
            self.signals.progress.emit("正在加载模型 (VAD + SenseVoice)...", 10)
            
            # 检查显存/内存并加载模型
            device = "cuda" if torch.cuda.is_available() else "cpu"
            self.signals.log.emit(f"推理设备: {device}")
            
            if AutoModel is None:
                raise ImportError("未安装 funasr 库，无法进行语音识别。")

            # 分别加载 VAD 和 ASR 模型
            # VAD 用于切分长音频
            vad_model = AutoModel(
                model="iic/speech_fsmn_vad_zh-cn-16k-common-pytorch",
                trust_remote_code=True,
                device=device,
                disable_update=True
            )
            
            # SenseVoice 用于识别
            asr_model = AutoModel(
                model="iic/SenseVoiceSmall",
                trust_remote_code=True,
                device=device,
                disable_update=True
            )
            
            if self._is_cancelled: return
            self.signals.progress.emit("正在进行 VAD 语音活动检测...", 20)
            
            # 读取音频数据到 numpy 数组 (funasr 需要)
            with wave.open(temp_wav, 'rb') as wf:
                params = wf.getparams()
                nchannels, sampwidth, framerate, nframes = params[:4]
                str_data = wf.readframes(nframes)
                # int16 -> float32 normalization usually done by libraries, 
                # but funasr/modelscope often accepts int16 numpy array directly if specified?
                # Actually funasr models often accept file path string too.
                # Let's use file path for VAD first.
            
            # VAD 推理
            # generate(input=path) returns segments list
            # VAD output format: [[start_ms, end_ms], ...]
            vad_res = vad_model.generate(input=temp_wav)
            
            # vad_res is usually a list of results (if input is list). Since input is str, it might be single result?
            # Check funasr VAD output structure: usually [{'value': [[s,e], ...]}] or just [[s,e]...]
            # For fsmn-vad, generate returns a list of result items.
            vad_segments = []
            if isinstance(vad_res, list) and len(vad_res) > 0:
                item = vad_res[0]
                if 'value' in item:
                    vad_segments = item['value'] # [[s, e], ...]
                elif isinstance(item, list):
                    vad_segments = item # Maybe direct list?
            
            # Fallback if VAD fails or detects nothing?
            if not vad_segments:
                 # If VAD returns nothing, maybe silent or too short?
                 # Try processing whole file as one segment
                 self.signals.log.emit("VAD 未检测到明显语音，将尝试识别完整音频。")
                 # Need total duration
                 with wave.open(temp_wav, 'rb') as wf:
                     duration_ms = (wf.getnframes() / wf.getframerate()) * 1000
                 vad_segments = [[0, duration_ms]]
            
            self.signals.log.emit(f"VAD 检测到 {len(vad_segments)} 个语音片段。")
            
            # 准备识别
            raw_segments = []
            
            # 读取音频为 numpy 数组以便切片 (int16 is fine for funasr usually)
            audio_np = np.frombuffer(str_data, dtype=np.int16)
            
            total_segments = len(vad_segments)
            
            for i, (seg_start, seg_end) in enumerate(vad_segments):
                if self._is_cancelled: return
                
                # Update progress (20% -> 50%)
                progress = 20 + int((i / total_segments) * 30)
                self.signals.progress.emit(f"正在识别第 {i+1}/{total_segments} 片段...", progress)
                
                # 切片音频
                # samples = ms * rate / 1000
                start_sample = int(seg_start * 16000 / 1000)
                end_sample = int(seg_end * 16000 / 1000)
                
                # Boundary check
                if start_sample >= len(audio_np): continue
                end_sample = min(end_sample, len(audio_np))
                
                chunk_data = audio_np[start_sample:end_sample]
                if len(chunk_data) < 1600: # Ignore < 0.1s
                    continue
                
                # ASR 推理
                # 必须转换为 float32，否则 torchaudio 可能会报 "not implemented for 'Short'"
                chunk_data_float = chunk_data.astype(np.float32)
                
                res = asr_model.generate(
                    input=chunk_data_float,
                    cache={},
                    language="auto",
                    use_itn=False,
                    timestamp=True 
                )
                
                # Parse result
                # res is usually list of dicts or single dict
                item_res = res[0] if isinstance(res, list) else res
                text_rich = item_res.get('text', '')
                
                # Parse rich text for subs
                parsed_subs = self.parse_asr_result({'text': text_rich})
                
                if not parsed_subs:
                    # Fallback: use VAD boundaries + cleaned text
                    clean_txt = self.clean_text(text_rich)
                    if clean_txt:
                        fallback_seg = {
                            'start': seg_start,
                            'end': seg_end,
                            'text': clean_txt
                        }
                        
                        if len(clean_txt) > 80:
                            raw_segments.extend(self.split_long_segment(fallback_seg))
                        else:
                            raw_segments.append(fallback_seg)
                else:
                    # Offset timestamps (parsed_subs already handled splitting)
                    for sub in parsed_subs:
                        sub['start'] += seg_start
                        sub['end'] += seg_start
                        raw_segments.append(sub)
                
            # 释放模型
            del vad_model
            del asr_model
            gc.collect()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()

            self.signals.log.emit(f"ASR 识别完成，共收集 {len(raw_segments)} 条字幕。")


            # 3. 机器翻译
            translated_segments = []
            provider = self.config.get('provider', 'gemini')

            if provider == 'gemini':
                self.signals.progress.emit("正在批量翻译 (Gemini)...", 45)
                texts = [self.clean_text(seg['text']) for seg in raw_segments]
                translations = self.translate_batch_gemini(texts)
                for seg, translated_text in zip(raw_segments, translations):
                    translated_segments.append({
                        "start": seg['start'],
                        "end": seg['end'],
                        "original": self.clean_text(seg['text']),
                        "translated": translated_text
                    })
            else:
                for i, seg in enumerate(raw_segments):
                    if self._is_cancelled: return
                    progress_percent = 40 + int((i / total_segments) * 50)
                    self.signals.progress.emit(f"正在翻译第 {i+1}/{total_segments} 句...", progress_percent)
                    clean_text = self.clean_text(seg['text'])
                    if not clean_text.strip():
                        translated_text = ""
                    else:
                        try:
                            translated_text = self.translate_text(clean_text)
                            time.sleep(0.2)
                        except Exception as e:
                            logger.error(f"翻译失败: {e}")
                            self.signals.log.emit(f"警告: 第 {i+1} 句翻译失败")
                            translated_text = "[翻译失败]"
                    translated_segments.append({
                        "start": seg['start'],
                        "end": seg['end'],
                        "original": clean_text,
                        "translated": translated_text
                    })

            # 4. 生成 SRT
            if self._is_cancelled: return
            self.signals.progress.emit("正在生成 SRT 文件...", 95)
            
            output_srt = str(Path(self.video_path).with_suffix('.srt'))
            self.generate_srt(translated_segments, output_srt)
            
            self.signals.progress.emit("完成!", 100)
            self.signals.finished.emit(output_srt)

        except Exception as e:
            logger.error(f"任务出错: {e}", exc_info=True)
            self.signals.error.emit(str(e))
        finally:
            # 清理临时文件
            if temp_wav and os.path.exists(temp_wav):
                try:
                    os.remove(temp_wav)
                except:
                    pass
            # 再次确保模型释放
            if model:
                del model
                gc.collect()

    def extract_audio(self, video_path):
        """使用 ffmpeg 提取 16k 16bit 单声道 wav"""
        temp_wav = str(Path(video_path).with_suffix('.wav')) # 生成同名 wav 作为临时文件
        # 如果存在先删除
        if os.path.exists(temp_wav):
            os.remove(temp_wav)
            
        try:
            (
                ffmpeg
                .input(video_path)
                .output(temp_wav, ac=1, ar=16000, acodec='pcm_s16le', loglevel='error')
                .run(overwrite_output=True)
            )
        except ffmpeg.Error as e:
            raise RuntimeError(f"FFmpeg 提取音频失败: {e.stderr.decode() if e.stderr else str(e)}")
            
        return temp_wav

    def parse_asr_result(self, result):
        """
        解析 SenseVoiceSmall 的富文本结果
        Result 格式示例: {'text': '<|en|><|0.00|>Hello world<|1.50|>...'}
        或者 funasr 可能返回包含 result 列表的结构
        """
        segments = []
        full_text = ""
        
        # 提取文本内容
        if isinstance(result, dict) and 'text' in result:
            full_text = result['text']
        else:
            return []

        # SenseVoice 输出格式类似于: <|lang|><|emotion|><|time_start|>text<|time_end|>...
        # 例如: <|en|><|HAPPY|><|0.00|>Hello<|0.50|><|0.50|>World<|1.00|>
        # 正则匹配: ((?:<\|\d+\.\d+\|>)+)([^<]+)((?:<\|\d+\.\d+\|>)+) 
        # 但这比较复杂，我们可以简化逻辑：
        # 1. 找到所有 <|time|> 标签
        # 2. 它们之间的内容就是文本
        
        logger.info(f"Parsing Text: {full_text[:100]}...")
        
        # 正则：匹配 <|12.34|> 这种时间戳
        # 另外要忽略 <|en|>, <|happy|> 等非数字标签
        
        # 简单策略：按 <|time|> 分割
        # 匹配 <|seconds|>
        pattern = r'<\|(\d+\.\d+)\|>'
        parts = re.split(pattern, full_text)
        
        # re.split 会保留捕获组 (时间戳)
        # parts 结构: ['<|en|><|happy|>', '0.00', 'Text1', '1.50', 'Text2', ...]
        # 注意: 可能会有连续的时间戳，例如 <|0.50|><|0.50|> 表示上一句结束，下一句开始
        
        current_start = 0.0
        current_text = ""
        
        # 这里的解析逻辑需要适配 split 后的列表
        # 如果 parts[0] 是前缀标签， parts[1] 是第一个时间戳
        
        # 我们用 finditer 可能会更清晰
        # 寻找模式: (<\|\d+\.\d+\|>)(.*?)(<\|\d+\.\d+\|>) 
        # 但 SenseVoice 可能是 <|s|>text<|e|><|s2|>text2<|e2|> 
        # 或者 <|s|>text<|e|><|e|>text2... (共享边界)
        
        # 让我们尝试更稳健的方法：扫描所有 tag 和 text
        tokens = re.findall(r'(<\|[\w\.]+\|>)|([^<]+)', full_text)
        # tokens is list of tuples: [('<|en|>', ''), ('', 'Hello'), ('<|0.00|>', '')]
        
        curr_start = None
        buffer_text = []
        
        for tag, text in tokens:
            if text:
                buffer_text.append(text.strip())
            
            if tag:
                # 检查是否是时间戳
                match = re.match(r'<\|(\d+\.\d+)\|>', tag)
                if match:
                    time_val = float(match.group(1)) * 1000 # 转换为 ms
                    
                    if curr_start is None:
                        curr_start = time_val
                    else:
                        # 这是一个结束时间戳 (或者是下一句的开始)
                        # 如果 buffer 有文本，则构成一段
                        content = " ".join(buffer_text).strip()
                        if content:
                            segments.append({
                                'start': int(curr_start),
                                'end': int(time_val),
                                'text': content
                            })
                            buffer_text = [] # 清空 buffer
                        
                        # 更新 start 为当前时间 (假设它是下一段的开始)
                        # 注意：SenseVoice 有时每个词都有时间戳，这样会导致非常碎片化
                        # 我们可能需要合并短片段，或者让后续翻译模块处理
                        curr_start = time_val
                
                else:
                    # 其他标签 (lang, emotion)，忽略或用于 reset
                    pass
        
        # 合并过短的片段 (SenseVoice 可能会逐词输出)
        # 第一阶段：合并
        merged_segments = []
        if not segments:
            # 如果没有解析出任何时间戳，但有文本，说明整个是一句
            if full_text.strip():
                 # 这种情况下将在外部由 VAD 时间戳接管，但我们可以在这里预处理一下清洗
                 pass
            return []
            
        current_seg = segments[0]
        
        for next_seg in segments[1:]:
            duration = current_seg['end'] - current_seg['start']
            text_len = len(current_seg['text'])
            
            # 合并条件：
            # 1. 前一句太短 (< 1.5s) OR 文本太少 (< 10 chars)
            # 2. AND 合并后不应过长 (< 80 chars)
            should_merge = (duration < 1500 or text_len < 10) and (text_len + len(next_seg['text']) < 100)
            
            if should_merge:
                current_seg['end'] = next_seg['end']
                current_seg['text'] += " " + next_seg['text']
            else:
                merged_segments.append(current_seg)
                current_seg = next_seg
                
        merged_segments.append(current_seg)
        
        # 第二阶段：长句拆分 (防止字幕过长)
        final_segments = []
        for seg in merged_segments:
            # 清洗一下文本
            seg['text'] = self.clean_text(seg['text'])
            
            if len(seg['text']) > 80: # 超过 80 字符强制拆分
                final_segments.extend(self.split_long_segment(seg))
            else:
                final_segments.append(seg)
                
        return final_segments

    def split_long_segment(self, segment, max_chars=60):
        """将长字幕片段切分为多段，时间戳线性插值"""
        text = segment['text']
        start = segment['start']
        end = segment['end']
        duration = end - start
        
        # 简单判断是否包含空格（英语通常有空格，中文通常没有）
        if ' ' in text:
            # 按单词切分 (英语)
            parts = text.split()
            # 重新组合为 chunks
            chunks = []
            curr_chunk = []
            curr_len = 0
            for w in parts:
                if curr_len + len(w) > max_chars:
                    chunks.append(" ".join(curr_chunk))
                    curr_chunk = [w]
                    curr_len = len(w)
                else:
                    curr_chunk.append(w)
                    curr_len += len(w) + 1
            if curr_chunk:
                chunks.append(" ".join(curr_chunk))
        else:
            # 按字符切分 (中文/日文)
            chunks = [text[i:i+max_chars] for i in range(0, len(text), max_chars)]
            
        if not chunks:
            return []
            
        # 分配时间戳
        res = []
        seg_start = start
        total_len = len(text)
        
        for i, chunk_text in enumerate(chunks):
            chunk_len = len(chunk_text)
            # 线性插值计算时长
            if total_len > 0:
                seg_duration = int(duration * (chunk_len / total_len))
            else:
                seg_duration = int(duration / len(chunks))
            
            # 最后一个片段修正对齐 end
            if i == len(chunks) - 1:
                seg_end = end
            else:
                seg_end = min(seg_start + seg_duration, end)
            
            res.append({
                'start': int(seg_start),
                'end': int(seg_end),
                'text': chunk_text
            })
            seg_start = seg_end
            
        return res

    def clean_text(self, text):
        """清洗 SenseVoice 输出的情感标签，如 <|happy|>"""
        # 移除 <|...|> 格式的标签
        text = re.sub(r'<\|.*?\|>', '', text)
        return text.strip()

    def translate_text(self, text):
        """调用讯飞机器翻译 API"""
        app_id = self.config.get('app_id')
        api_key = self.config.get('api_key')
        api_secret = self.config.get('api_secret')
        
        if not all([app_id, api_key, api_secret]):
            return "[配置缺失]"

        # DEBUG: 打印正在使用的 AppID (部分掩码)
        safe_app_id = app_id[:2] + "****" + app_id[-2:] if len(app_id) > 4 else "****"
        # self.signals.log.emit(f"DEBUG: Using AppID: {safe_app_id}") # 过于频繁，仅在出错时更有用，或者只打一次
        # 这里为了排查 400 错误，先不打，节省日志空间，仅在 error block 里打
        
        # 讯飞机器翻译 (非 Niutrans) 接口参数构造
        # 尝试使用通用的 common/business/data 结构，这是标准机器翻译服务的常用格式
        
        # 源语言映射
        src_lang_map = {
            '英语': 'en',
            '日语': 'ja',
            '自动': 'en' 
        }
        user_lang = self.config.get('source_lang', '自动')
        from_lang = src_lang_map.get(user_lang, 'en')

        # 构造请求体 (V1/Standard Format)
        body_data = {
            "common": {
                "app_id": app_id
            },
            "business": {
                "from": from_lang,
                "to": "cn",
            },
            "data": {
                "text": base64.b64encode(text.encode('utf-8')).decode('utf-8')
            }
        }

        body_bytes = json.dumps(body_data).encode('utf-8')
        # 鉴权: 
        # 注意: 标准机器翻译 API 鉴权方式可能有所不同，通常也是 HMAC-SHA256
        # URL 通常也是 itrans.xfyun.cn/v2/its
        headers = get_xf_sign(app_id, api_key, api_secret, body_bytes)
        
        resp = requests.post("https://itrans.xfyun.cn/v2/its", headers=headers, data=body_bytes, timeout=10)
        
        if resp.status_code != 200:
            raise RuntimeError(f"API Error {resp.status_code}: {resp.text}")
            
        resp_json = resp.json()
        
        # 错误检查
        code = resp_json.get('code', -1)
        if code != 0:
             # V1 格式通常在根节点返回 code/message
            message = resp_json.get('message', 'Unknown Error')
            if 'header' in resp_json: # 兼容 V2 错误返回
                code = resp_json['header']['code']
                message = resp_json['header']['message']
            
            raise RuntimeError(f"API Error {code}: {message}")

        # 解析结果
        # V1 格式返回: {'data': {'result': {'trans_result': {'dst': ...}}}}
        try:
            if 'data' in resp_json and 'result' in resp_json['data']:
                trans_res = resp_json['data']['result']['trans_result']
                return trans_res['dst']
            # 兼容 V2 格式返回 (如果服务器自动转换了)
            elif 'payload' in resp_json:
                result_b64 = resp_json['payload']['result']['text']
                result_bin = base64.b64decode(result_b64)
                result_obj = json.loads(result_bin.decode('utf-8'))
                return result_obj['trans_result']['dst']
            else:
                raise RuntimeError(f"Unexpected response: {resp_json}")
        except Exception as e:
            logger.error(f"Parse Error: {e}")
            raise RuntimeError(f"Translation Parse Error: {e}")

    def translate_batch_gemini(self, texts: list) -> list:
        """
        将字幕并发分批发送给 Gemini 翻译（核心网络请求组件）。
        采用以下策略保障稳定性和效率：
        1. 线程池并发处理多个小批次（Batching + Concurrency），极大提升吞吐量。
        2. 采用流式请求（stream=True）维持长连接。防止 Gemini 生成耗时较长时，发起端 requests/urllib3 因为 timeout=120 提前强行终止（即 499 Client Closed Request 错误）。
        3. 指数退避重试（Exponential Backoff），应对临时性的 503 或 429 错误。

        @param texts 待翻译的原文列表
        @returns 翻译完成的中文字符串列表，顺序与 `texts` 严格一致
        """
        import google.generativeai as genai

        # ── 可调参数（根据 API 额度和网络状况进行微调） ─────────────────────
        BATCH_SIZE = 8           # 每批条数：减小单批任务量，能有效降低服务端生成超时的 504 错误
        MAX_WORKERS = 4          # 并发线程数：降低并发以换取单次连接的稳定性（免费 API Key 建议设为 1 或 2，防止 429 Too Many Requests）
        TIMEOUT_SECONDS = 120    # 流式超时时间（秒）：放大至 120s。因为设定了流式，所以只要服务端在吐字，连接就不会轻易断
        MAX_RETRIES = 3          # 最大重试次数
        # ─────────────────────────────────────────────────────────────

        api_key = self.config.get('gemini_api_key')
        if not api_key:
            self.signals.log.emit("⚠ Gemini API Key 未配置")
            return ["[配置缺失]"] * len(texts)

        src_lang_map = {'英语': 'English', '日语': 'Japanese', '自动': 'the source language'}
        src = src_lang_map.get(self.config.get('source_lang', '自动'), 'the source language')

        # 过滤空文本，记录原始索引以便回填
        indexed = [(i, t) for i, t in enumerate(texts) if t.strip()]
        results = [""] * len(texts)
        if not indexed:
            return results

        indices, non_empty = zip(*indexed)
        non_empty = list(non_empty)
        indices = list(indices)

        # 优先使用用户自定义提示词
        system_instruction = self.config.get("gemini_system_prompt", "").strip()
        if not system_instruction:
            from utils import DEFAULT_GEMINI_SYSTEM_PROMPT
            system_instruction = DEFAULT_GEMINI_SYSTEM_PROMPT

        genai.configure(api_key=api_key)
        model = genai.GenerativeModel(
            self.config.get('gemini_model', 'gemini-1.5-flash'),
            system_instruction=system_instruction
        )

        total = len(non_empty)
        total_batches = (total + BATCH_SIZE - 1) // BATCH_SIZE

        self.signals.log.emit(
            f"═══ Gemini 翻译启动 ═══\n"
            f"  总句数: {total} | 每批: {BATCH_SIZE} 句 | 共 {total_batches} 批\n"
            f"  并发线程: {MAX_WORKERS} | 超时: {TIMEOUT_SECONDS}s | 最大重试: {MAX_RETRIES} 次"
        )

        # 线程安全的进度计数器
        completed_lock = threading.Lock()
        completed_count = [0]  # 用列表包装以支持闭包内修改
        failed_batches = []    # 记录失败的批次号

        def _translate_single_batch(batch_num, batch_texts, batch_indices):
            """
            内部函数：在独立线程中执行单批次的翻译任务。
            @param batch_num 批次序号（用于日志显示）
            @param batch_texts 当前批次需要翻译的原始文本列表
            @param batch_indices 该批次文本在整体 `texts` 数组中的原始索引，用于最终结果的回填对齐
            """
            thread_name = threading.current_thread().name
            tag = f"[批次 {batch_num}/{total_batches}][{thread_name}]"

            if self._is_cancelled:
                self.signals.log.emit(f"{tag} ⏭ 任务已取消，跳过")
                return

            self.signals.log.emit(
                f"{tag} 📤 开始翻译 ({len(batch_texts)} 句)..."
            )

            prompt = (
                f"Translate these {src} subtitles to Simplified Chinese.\n"
                f"Return ONLY a JSON array of exactly {len(batch_texts)} translated strings, "
                f"same order, no extra text.\n"
                f"{json.dumps(batch_texts, ensure_ascii=False)}"
            )

            for attempt in range(1, MAX_RETRIES + 1):
                if self._is_cancelled:
                    self.signals.log.emit(f"{tag} ⏭ 任务已取消，停止重试")
                    return

                try:
                    self.signals.log.emit(
                        f"{tag} 🔄 第 {attempt}/{MAX_RETRIES} 次请求 (流式模式, 超时 {TIMEOUT_SECONDS}s)..."
                    )
                    t_start = time.time()
                    elapsed = 0

                    # NOTE: 使用 stream=True 保持 HTTP 长连接，防止服务端慢响应时
                    # 客户端提前超时断开（即 499 错误）
                    response_stream = model.generate_content(
                        prompt,
                        stream=True,
                        request_options={"timeout": TIMEOUT_SECONDS}
                    )

                    # 逐块拼接，持续保持连接活跃
                    raw_chunks = []
                    for chunk in response_stream:
                        if self._is_cancelled:
                            self.signals.log.emit(f"{tag} ⏭ 流式传输中断（任务取消）")
                            return
                        if chunk.text:
                            raw_chunks.append(chunk.text)

                    elapsed = time.time() - t_start
                    raw = "".join(raw_chunks).strip()

                    # HACK: 剥离可能由大模型返回的 markdown JSON 代码块包裹 (```json ... ```)
                    # 虽然我们在 prompt 中要求了“ONLY a JSON array”，但大模型偶尔仍会附带 markdown
                    if raw.startswith("```"):
                        raw = re.sub(r'^```[a-z]*\n?', '', raw)
                        raw = re.sub(r'\n?```$', '', raw)

                    translations = json.loads(raw)

                    # 强校验：确保返回的数组长度与发送的原文段落数严格对齐
                    if not isinstance(translations, list) or len(translations) != len(batch_texts):
                        raise ValueError(
                            f"返回数量不符: 期望 {len(batch_texts)} 条，"
                            f"实际收到 {len(translations) if isinstance(translations, list) else 'non-list'}"
                        )

                    # 将当前批次的翻译结果，回填到总的结果数组中的对应位置
                    for idx, translated in zip(batch_indices, translations):
                        results[idx] = str(translated)

                    # 更新进度
                    with completed_lock:
                        completed_count[0] += 1
                        done = completed_count[0]
                    progress = 45 + int((done / total_batches) * 45)
                    self.signals.progress.emit(
                        f"翻译中 ({done}/{total_batches} 批完成)...", progress
                    )

                    self.signals.log.emit(
                        f"{tag} ✅ 成功 — {len(batch_texts)} 句已翻译 "
                        f"(耗时 {elapsed:.1f}s, 进度 {done}/{total_batches})"
                    )
                    return  # 成功，退出重试循环

                except Exception as e:
                    elapsed = time.time() - t_start if 't_start' in dir() else 0
                    error_type = type(e).__name__

                    logger.error(f"Gemini 批次 {batch_num} 第 {attempt} 次失败: {e}")

                    if attempt < MAX_RETRIES:
                        # 指数退避: 2s, 4s, 8s
                        wait_sec = 2 ** attempt
                        self.signals.log.emit(
                            f"{tag} ❌ 第 {attempt} 次失败 [{error_type}]: {e}\n"
                            f"{tag} ⏳ 等待 {wait_sec}s 后重试..."
                        )
                        time.sleep(wait_sec)
                    else:
                        self.signals.log.emit(
                            f"{tag} ❌ 第 {attempt} 次失败 [{error_type}]: {e}\n"
                            f"{tag} 🚫 已达最大重试次数，标记为翻译失败"
                        )
                        for idx in batch_indices:
                            results[idx] = "[翻译失败]"

                        with completed_lock:
                            completed_count[0] += 1
                            failed_batches.append(batch_num)

        # ── 构建所有批次任务 ──────────────────────────────────
        batch_tasks = []
        for batch_start in range(0, total, BATCH_SIZE):
            batch_end = min(batch_start + BATCH_SIZE, total)
            batch_num = batch_start // BATCH_SIZE + 1
            batch_texts = non_empty[batch_start:batch_end]
            batch_indices_slice = indices[batch_start:batch_end]
            batch_tasks.append((batch_num, batch_texts, batch_indices_slice))

        # ── 并发执行 ─────────────────────────────────────────
        self.signals.log.emit(f"───────────────────────────────────")
        self.signals.log.emit(f"🚀 启动 {MAX_WORKERS} 个并发线程处理 {len(batch_tasks)} 个批次...")
        self.signals.log.emit(f"───────────────────────────────────")

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=MAX_WORKERS,
            thread_name_prefix="GeminiWorker"
        ) as executor:
            futures = {
                executor.submit(_translate_single_batch, num, texts_batch, idx_batch): num
                for num, texts_batch, idx_batch in batch_tasks
            }

            # 等待所有任务完成（as_completed 可以尽早发现异常）
            for future in concurrent.futures.as_completed(futures):
                batch_num = futures[future]
                try:
                    future.result()  # 触发异常传播
                except Exception as e:
                    # 理论上 _translate_single_batch 内部已经 catch 了所有异常，
                    # 这里是兜底保护
                    logger.error(f"线程异常 (批次 {batch_num}): {e}")
                    self.signals.log.emit(f"⚠ 批次 {batch_num} 线程异常: {e}")

        # ── 汇总 ─────────────────────────────────────────────
        success_count = total_batches - len(failed_batches)
        self.signals.log.emit(f"───────────────────────────────────")
        if failed_batches:
            self.signals.log.emit(
                f"⚠ Gemini 翻译完成: {success_count}/{total_batches} 批成功，"
                f"{len(failed_batches)} 批失败 (批次号: {failed_batches})"
            )
        else:
            self.signals.log.emit(
                f"✅ Gemini 翻译全部完成！{total_batches}/{total_batches} 批均成功"
            )
        self.signals.log.emit(f"───────────────────────────────────")

        return results

    def generate_srt(self, segments, output_path):
        """
        segments: [{'start': ms, 'end': ms, 'original': str, 'translated': str}]
        """
        with open(output_path, 'w', encoding='utf-8') as f:
            for i, seg in enumerate(segments):
                # 序号
                f.write(f"{i+1}\n")
                
                # 时间轴
                # ms -> s
                start_s = seg['start'] / 1000.0
                end_s = seg['end'] / 1000.0
                time_line = f"{format_timestamp(start_s)} --> {format_timestamp(end_s)}\n"
                f.write(time_line)
                
                # 内容 (中文在上，原文在下)
                f.write(f"{seg['translated']}\n")
                if seg['original']:
                    f.write(f"{seg['original']}\n")
                
                f.write("\n")
