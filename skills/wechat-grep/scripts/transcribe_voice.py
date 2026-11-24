#!/usr/bin/env python3
"""微信语音 WAV → 文本（sherpa-onnx Zipformer CTC 本地 ASR，中英文）。

配合 weflow CLI 的 `chat voice` 导出的 WAV 使用；亦可直接转任意 WAV。
模型：zipformer-small-ctc-int8（k2-fsa，Apache-2.0；本机由 WeChatDataAnalysis
下载至 %APPDATA%\\wechat-data-analysis-desktop\\voice_models\\，脚本自动定位；
内网分发建议把模型目录复制进 skill 的 models/ 下作为自带模型）。

用法：
    py -3.x transcribe_voice.py <wav 文件或目录> [--model <模型目录>] [--show-meta]
    （py -3.x = 3.8-3.13 任一可用解释器；依赖：sherpa-onnx==2.0.1 + numpy）

输出：每个 WAV 一行  <文件名>\\t<文本>
"""
import argparse
import glob
import json
import os
import struct
import sys
import wave

MODEL_CANDIDATES = [
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                 "models", "zipformer-small-ctc-int8"),
    os.path.join(os.environ.get("APPDATA", ""), "wechat-data-analysis-desktop",
                 "voice_models", "zipformer-small-ctc-int8"),
]
TARGET_RATE = 16000


def resolve_model_dir(explicit):
    if explicit:
        return explicit
    for cand in MODEL_CANDIDATES:
        if os.path.exists(os.path.join(cand, "ctc.int8.onnx")):
            return cand
    # 兜底：模型文件可能在子目录（如 data/）
    for cand in MODEL_CANDIDATES:
        hits = glob.glob(os.path.join(cand, "**", "ctc.int8.onnx"), recursive=True)
        if hits:
            return os.path.dirname(hits[0])
    return None


def load_wav_float32(path):
    """读取 WAV，返回 (float32 单声道 samples, sample_rate)。"""
    import numpy as np

    with wave.open(path, "rb") as w:
        rate = w.getframerate()
        channels = w.getnchannels()
        width = w.getsampwidth()
        frames = w.getnframes()
        raw = w.readframes(frames)
    if width == 2:
        samples = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        samples = np.frombuffer(raw, dtype="<i4").astype(np.float32) / 2147483648.0
    elif width == 1:
        samples = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128) / 128.0
    else:
        raise ValueError(f"不支持的采样位宽 {width}")
    if channels > 1:
        samples = samples[::channels]
    if rate != TARGET_RATE:
        # 线性插值重采样到 16k（ASR 特征足够）
        n_out = int(round(len(samples) * TARGET_RATE / rate))
        idx = np.linspace(0, len(samples) - 1, n_out)
        samples = np.interp(idx, np.arange(len(samples)), samples).astype(np.float32)
    return samples, rate


def main():
    ap = argparse.ArgumentParser(description="微信语音 WAV 本地转写（Zipformer CTC）")
    ap.add_argument("inputs", nargs="+", help="WAV 文件或目录")
    ap.add_argument("--model", help="模型目录（默认自动定位）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    try:
        import sherpa_onnx
        import numpy as np
    except ImportError:
        print("缺少依赖 sherpa-onnx/numpy：需要 Python 3.8-3.13（sherpa-onnx 官方轮子范围），"
              "请用 py -3.8 / py -3.12 / py -3.13 等任一可用解释器安装依赖后运行："
              "py -3.x -m pip install sherpa-onnx==2.0.1 numpy", file=sys.stderr)
        return 1

    model_dir = resolve_model_dir(args.model)
    if not model_dir:
        print("未找到模型目录（ctc.int8.onnx）。--model 指定，或从 WDA voice_models 复制到 models/ 下。", file=sys.stderr)
        return 1
    onnx_hits = glob.glob(os.path.join(model_dir, "**", "ctc.int8.onnx"), recursive=True)
    token_hits = glob.glob(os.path.join(model_dir, "**", "tokens.txt"), recursive=True)
    if not onnx_hits or not token_hits:
        print(f"模型目录无效：{model_dir} 缺少 ctc.int8.onnx/tokens.txt。", file=sys.stderr)
        return 1
    onnx, tokens = onnx_hits[0], token_hits[0]
    print(f"[模型] {model_dir}", file=sys.stderr)

    recognizer = sherpa_onnx.OfflineRecognizer.from_zipformer_ctc(
        model=onnx, tokens=tokens, num_threads=2,
        sample_rate=16000, feature_dim=80, provider="cpu",
    )

    files = []
    for inp in args.inputs:
        if os.path.isdir(inp):
            files.extend(sorted(glob.glob(os.path.join(inp, "**", "*.wav"), recursive=True)))
        else:
            files.append(inp)
    if not files:
        print("输入中未找到 WAV 文件（目录为空或不含 *.wav）。先用 "
              "weflow chat voice <SID> --out <目录> 导出语音，再传入该目录。", file=sys.stderr)
        return 1

    results = []
    for wav_path in files:
        try:
            samples, rate = load_wav_float32(wav_path)
        except Exception as e:
            print(f"[跳过] {wav_path}: {e}", file=sys.stderr)
            continue
        texts = []
        # Zipformer 导出对超长输入会 Reshape 失败，按 WDA 同款 15 秒分段推理
        chunk = 15 * TARGET_RATE
        n_chunks = max(1, (len(samples) + chunk - 1) // chunk)
        for ci in range(n_chunks):
            seg = samples[ci * chunk : (ci + 1) * chunk]
            if len(seg) < TARGET_RATE // 10:  # 尾部不足 0.1s 丢弃
                continue
            s = recognizer.create_stream()
            s.accept_waveform(TARGET_RATE, seg)
            recognizer.decode_stream(s)
            texts.append(s.result.text.strip())
        text = "".join(texts).strip()
        results.append({"file": wav_path, "text": text})
        if args.json:
            continue
        name = os.path.basename(wav_path)
        print(f"{name}\t{text}")

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
