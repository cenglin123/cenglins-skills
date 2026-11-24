import argparse
import json
import os
import shutil
import subprocess
import sys
import sysconfig
from pathlib import Path

ENV_VAR = "VIDEO_TO_SRT_VENV"
MODEL_DIR_ENV_VAR = "VIDEO_TO_SRT_MODEL_DIR"
SELECTED_ENV = None
SELECTED_VENV_MODEL_DIR = None

DEFAULT_MODEL = "turbo"
DEFAULT_MODEL_DIR = os.environ.get(
    MODEL_DIR_ENV_VAR,
    str(Path.home() / ".cache" / "video-to-srt" / "models"),
)


def setup_cuda_dll_paths():
    """添加当前环境 nvidia 子包中的 CUDA DLL 路径（cudnn/cublas）。"""
    if sys.platform != "win32":
        return
    try:
        site_path = sysconfig.get_path("purelib")
        nvidia_path = Path(site_path) / "nvidia"
        if not nvidia_path.exists():
            return
        cuda_paths = []
        for subdir in ["cudnn", "cublas"]:
            bin_path = nvidia_path / subdir / "bin"
            if bin_path.exists():
                cuda_paths.append(str(bin_path))
        for path in cuda_paths:
            try:
                os.add_dll_directory(path)
            except (OSError, AttributeError):
                pass
            if path not in os.environ.get("PATH", ""):
                os.environ["PATH"] = path + os.pathsep + os.environ.get("PATH", "")
    except Exception:
        pass


def setup_cudnn8_paths():
    """ctranslate2 <=4.3 依赖 cuDNN 8；部分环境 pip 装的 nvidia 子包只带 cuDNN 9，
    GPU 卷积时会卡死并只打一行 "Could not locate cudnn_ops_infer64_8.dll"。

    这里从已知系统路径探测 cuDNN 8 并 prepend 到 PATH，保证 8.x 优先于 9.x。
    """
    if sys.platform != "win32":
        return
    candidates = [
        r"C:\Program Files\NVIDIA Corporation\NVIDIA Canvas",
        r"C:\Program Files\NVIDIA\CUDNN\v8.9\bin",
        r"C:\Program Files\NVIDIA\CUDNN\v8.8\bin",
        r"C:\Program Files\NVIDIA\CUDNN\v8.7\bin",
        r"C:\Program Files\NVIDIA\CUDNN\v8.6\bin",
        r"C:\Program Files\NVIDIA\CUDNN\v8.5\bin",
    ]
    found = []
    for p in candidates:
        d = Path(p)
        if d.is_dir() and (d / "cudnn_ops_infer64_8.dll").exists():
            found.append(str(d))
    if not found:
        return
    for d in found:
        cur = os.environ.get("PATH", "")
        if d not in cur.split(os.pathsep):
            # prepend：让 cudnn 8 在 PATH 上位于 cudnn 9 之前
            os.environ["PATH"] = d + os.pathsep + cur
        try:
            os.add_dll_directory(d)
        except (OSError, AttributeError):
            pass
    print(f"[video-to-srt] 已加入 cuDNN 8 路径 (ctranslate2 <=4.3 兼容): {found[0]}")


# 模块加载时立即生效：探测子进程和后续 auto-discovery 都能继承 PATH
setup_cudnn8_paths()


def _get_venv_python(venv_path: Path) -> Path | None:
    if sys.platform == "win32":
        python = venv_path / "Scripts" / "python.exe"
    else:
        python = venv_path / "bin" / "python"
    if python.exists():
        return python
    return None


def _probe_venv_python(python_exe: Path) -> dict | None:
    """探测环境是否可用，并尝试实际加载 tiny 模型验证 CUDA/cuDNN 真可用。"""
    code = (
        "import json, sys, sysconfig, pathlib, os, tempfile\n"
        "from pathlib import Path\n"
        "site = Path(sysconfig.get_path('purelib'))\n"
        "nvidia_dll_dirs = []\n"
        "nvidia = site / 'nvidia'\n"
        "if nvidia.exists():\n"
        "    for p in nvidia.rglob('bin'):\n"
        "        if any(p.glob('*.dll')) or any(p.glob('*.so*')):\n"
        "            nvidia_dll_dirs.append(str(p))\n"
        "            os.environ['PATH'] = str(p) + os.pathsep + os.environ.get('PATH', '')\n"
        "            try:\n"
        "                os.add_dll_directory(str(p))\n"
        "            except Exception:\n"
        "                pass\n"
        "res = {'faster_whisper': False, 'torch_cuda': False, 'ctranslate2_cuda': False, 'nvidia_dll_dirs': nvidia_dll_dirs, 'site_path': str(site), 'whisper_gpu_load_ok': False}\n"
        "try:\n"
        "    import faster_whisper\n"
        "    res['faster_whisper'] = True\n"
        "    res['faster_whisper_version'] = getattr(faster_whisper, '__version__', 'unknown')\n"
        "except Exception as e:\n"
        "    res['faster_whisper_error'] = str(e)\n"
        "try:\n"
        "    import torch\n"
        "    res['torch_cuda'] = torch.cuda.is_available()\n"
        "except Exception:\n"
        "    pass\n"
        "try:\n"
        "    import ctranslate2\n"
        "    res['ctranslate2_cuda'] = getattr(ctranslate2, 'get_cuda_device_count', lambda: 0)() > 0\n"
        "    res['ctranslate2_version'] = getattr(ctranslate2, '__version__', 'unknown')\n"
        "except Exception:\n"
        "    pass\n"
        "try:\n"
        "    import faster_whisper\n"
        "    from faster_whisper import WhisperModel\n"
        "    if res.get('torch_cuda') or res.get('ctranslate2_cuda'):\n"
        "        model = WhisperModel('tiny', device='cuda', compute_type='float16')\n"
        "        res['whisper_gpu_load_ok'] = True\n"
        "except Exception as e:\n"
        "    res['whisper_gpu_load_error'] = str(e)\n"
        "print(json.dumps(res))\n"
    )
    try:
        proc = subprocess.run(
            [str(python_exe), "-c", code],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        if proc.returncode != 0:
            return None
        lines = [line for line in proc.stdout.strip().splitlines() if line]
        if not lines:
            return None
        data = json.loads(lines[-1])
        if not data.get("faster_whisper"):
            return None
        return data
    except Exception:
        return None


def _is_gpu_capable(info: dict) -> bool:
    """GPU 可用 = 能 import torch/ctranslate2 且能实际加载 faster-whisper cuda。"""
    return bool(info.get("whisper_gpu_load_ok"))


def _scan_for_venvs(root: Path, max_depth: int = 3):
    names = {".venv", "venv", "env"}
    try:
        for dirpath, dirnames, _ in os.walk(root):
            depth = dirpath.count(os.sep) - str(root).count(os.sep)
            if depth >= max_depth:
                del dirnames[:]
                continue
            for name in list(dirnames):
                if name in names:
                    yield Path(dirpath) / name
                    dirnames.remove(name)
    except Exception:
        return


def _iter_candidate_venvs():
    seen = set()
    candidates = []

    def add_path(path: Path):
        try:
            resolved = path.resolve(strict=False)
            if resolved in seen:
                return
            seen.add(resolved)
            if resolved.exists():
                candidates.append(resolved)
        except Exception:
            return

    env_override = os.environ.get(ENV_VAR)
    if env_override:
        add_path(Path(env_override))

    fixed = [
        Path("C:/Project/faster-whisper/whisper-srt/.venv"),
        Path("C:/Project/faster-whisper/whisper-srt/venv"),
        Path.cwd() / ".venv",
        Path.cwd() / "venv",
        Path.home() / ".venvs" / "faster-whisper",
    ]
    for p in fixed:
        add_path(p)

    project_root = Path("C:/Project")
    if project_root.exists():
        for venv in _scan_for_venvs(project_root, max_depth=3):
            add_path(venv)

    try:
        for proj in Path.home().glob("Project*"):
            if proj.is_dir():
                for venv in _scan_for_venvs(proj, max_depth=3):
                    add_path(venv)
    except Exception:
        pass

    yield from candidates


def _discover_venv_models(venv_path: Path) -> Path | None:
    candidates = []
    if venv_path.name.lower() in {".venv", "venv", "env"}:
        candidates.append(venv_path.parent / "models")
    candidates.extend([
        venv_path / "models",
        venv_path.parent / "models",
    ])
    for p in candidates:
        try:
            if p.exists() and p.is_dir() and any(p.iterdir()):
                return p.resolve()
        except Exception:
            pass
    return None


def find_best_whisper_env():
    env_override = os.environ.get(ENV_VAR)
    skip_discovery = bool(os.environ.get("VIDEO_TO_SRT_SKIP_DISCOVERY"))

    # Fast path 1：VIDEO_TO_SRT_SKIP_DISCOVERY=1 → 直接用当前解释器，跳过所有候选扫描
    if skip_discovery:
        if env_override:
            print(f"[video-to-srt] VIDEO_TO_SRT_SKIP_DISCOVERY=1，忽略 VIDEO_TO_SRT_VENV={env_override}")
        current_python = Path(sys.executable)
        current_info = _probe_venv_python(current_python)
        if current_info is not None:
            print(f"[video-to-srt] 跳过自动发现，使用当前解释器: {current_python}")
            return {"venv": current_python.parent, "python": current_python, "info": current_info}

    # Fast path 2：显式指定了 VIDEO_TO_SRT_VENV 但在该平台不可用
    #（典型场景：指向 Linux venv 跑在 Windows 上，或 venv 已损坏）
    # → 跳过候选扫描直接回退当前解释器，避免多秒探测浪费时间
    if env_override:
        venv = Path(env_override)
        py = _get_venv_python(venv)
        if py is not None:
            info = _probe_venv_python(py)
            if info is not None:
                return {"venv": venv, "python": py, "info": info}
            print(f"[video-to-srt] 警告: {env_override} 的 faster-whisper 探测失败")
        else:
            print(f"[video-to-srt] 警告: {env_override} 在 {sys.platform} 上未找到 python 可执行文件")
        print("[video-to-srt] 回退当前解释器（设 VIDEO_TO_SRT_SKIP_DISCOVERY=1 可显式跳过）")
        current_python = Path(sys.executable)
        current_info = _probe_venv_python(current_python)
        if current_info is not None:
            return {"venv": current_python.parent, "python": current_python, "info": current_info}
        return {"venv": current_python.parent, "python": current_python, "info": None}

    forced_entry = None
    best_gpu = None
    best_cpu = None

    for venv in _iter_candidate_venvs():
        python = _get_venv_python(venv)
        if python is None:
            continue
        info = _probe_venv_python(python)
        if info is None:
            continue
        entry = {"venv": venv, "python": python, "info": info}
        if env_override and venv.resolve(strict=False) == Path(env_override).resolve(strict=False):
            forced_entry = entry
            break
        if _is_gpu_capable(info):
            if best_gpu is None:
                best_gpu = entry
        else:
            if best_cpu is None:
                best_cpu = entry

    if forced_entry is not None:
        return forced_entry
    if env_override and forced_entry is None:
        print(f"[video-to-srt] 警告: 指定的环境 {env_override} 不可用，尝试自动发现")

    if best_gpu is not None:
        return best_gpu
    if best_cpu is not None:
        return best_cpu

    current_python = Path(sys.executable)
    current_info = _probe_venv_python(current_python)
    if current_info is not None:
        return {"venv": current_python.parent, "python": current_python, "info": current_info}

    return {"venv": current_python.parent, "python": current_python, "info": None}


def _prepare_env_paths(env: dict):
    for d in env.get("info", {}).get("nvidia_dll_dirs", []):
        p = Path(d)
        if not p.exists():
            continue
        try:
            os.add_dll_directory(str(p))
        except (OSError, AttributeError):
            pass
        if str(p) not in os.environ.get("PATH", ""):
            os.environ["PATH"] = str(p) + os.pathsep + os.environ.get("PATH", "")

    scripts_dir = env["venv"] / ("Scripts" if sys.platform == "win32" else "bin")
    if scripts_dir.exists() and str(scripts_dir) not in os.environ.get("PATH", ""):
        os.environ["PATH"] = str(scripts_dir) + os.pathsep + os.environ.get("PATH", "")


def ensure_best_python():
    global SELECTED_ENV, SELECTED_VENV_MODEL_DIR
    SELECTED_ENV = find_best_whisper_env()
    SELECTED_VENV_MODEL_DIR = _discover_venv_models(SELECTED_ENV["venv"])

    selected_python = Path(SELECTED_ENV["python"]).resolve(strict=False)
    current_python = Path(sys.executable).resolve(strict=False)

    if selected_python != current_python:
        print(f"[video-to-srt] 自动切换到可用 faster-whisper 环境: {selected_python}")
        sys.stdout.flush()
        _prepare_env_paths(SELECTED_ENV)
        os.environ[ENV_VAR] = str(SELECTED_ENV["venv"])
        script = Path(__file__).resolve()
        argv = [str(selected_python), str(script)] + sys.argv[1:]
        os.execv(str(selected_python), argv)

    _prepare_env_paths(SELECTED_ENV)
    return SELECTED_ENV


def format_timestamp(seconds: float) -> str:
    total_ms = max(0, round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def get_duration(video_path: Path) -> float:
    if shutil.which("ffprobe") is None:
        raise RuntimeError("未找到 ffprobe；请先安装 ffmpeg 并加入 PATH")
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe 失败：{result.stderr.strip() or '未知错误'}")
    try:
        return float(json.loads(result.stdout)["format"]["duration"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"无法解析视频时长：{exc}") from exc


def resolve_device(requested: str, info: dict | None = None) -> str:
    if requested != "auto":
        return requested
    # 环境探测里已经实际加载过 tiny 模型时，以它为准
    if info is not None and "whisper_gpu_load_ok" in info:
        return "cuda" if info.get("whisper_gpu_load_ok") else "cpu"
    try:
        import ctranslate2

        return "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
    except Exception:
        return "cpu"


def download_model(model_name: str, model_dir: Path) -> str:
    local_dir = model_dir / model_name.replace("/", "--")
    if local_dir.exists() and any(local_dir.iterdir()):
        print(f"模型已存在：{local_dir}")
        return str(local_dir)
    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        raise RuntimeError(
            "缺少 huggingface_hub；请在项目虚拟环境中安装 requirements.txt"
        ) from exc
    model_mapping = {
        "tiny": "Systran/faster-whisper-tiny",
        "base": "Systran/faster-whisper-base",
        "small": "Systran/faster-whisper-small",
        "medium": "Systran/faster-whisper-medium",
        "large-v1": "Systran/faster-whisper-large-v1",
        "large-v2": "Systran/faster-whisper-large-v2",
        "large-v3": "Systran/faster-whisper-large-v3",
        "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    }
    repo_id = model_mapping.get(model_name, model_name)
    print(f"下载模型：{repo_id}")
    local_dir.mkdir(parents=True, exist_ok=True)
    snapshot_download(repo_id=repo_id, local_dir=str(local_dir))
    print(f"模型下载完成：{local_dir}")
    return str(local_dir)


def transcribe(args: argparse.Namespace) -> Path:
    try:
        from faster_whisper import WhisperModel
        from tqdm import tqdm
    except ImportError as exc:
        raise RuntimeError(
            "缺少转写依赖；请在项目虚拟环境中安装 requirements.txt"
        ) from exc

    setup_cuda_dll_paths()

    video = Path(args.video).expanduser().resolve()
    if not video.is_file():
        raise FileNotFoundError(f"视频文件不存在：{video}")

    output = Path(args.output).expanduser().resolve() if args.output else video.with_suffix(".srt")
    if output.exists() and not args.force:
        raise FileExistsError(f"输出已存在：{output}；使用 --force 覆盖")
    output.parent.mkdir(parents=True, exist_ok=True)

    duration = get_duration(video)
    requested_device = args.device
    device = resolve_device(requested_device, SELECTED_ENV.get("info") if SELECTED_ENV else None)
    compute_type = args.compute_type or ("float16" if device == "cuda" else "int8")
    model_dir = Path(args.model_dir).expanduser().resolve()
    model_dir.mkdir(parents=True, exist_ok=True)

    # --model 既可以是模型名，也可以是已下载的本地模型目录
    if Path(args.model).exists():
        model_ref = str(Path(args.model).expanduser().resolve())
    else:
        model_ref = download_model(args.model, model_dir)

    print(f"视频：{video}")
    print(f"时长：{format_timestamp(duration)}")
    print(f"模型：{args.model}；设备：{device}；计算类型：{compute_type}")

    try:
        model = WhisperModel(model_ref, device=device, compute_type=compute_type)
    except Exception as exc:
        if device == "cuda":
            print(f"CUDA 初始化失败，回退到 CPU：{exc}", file=sys.stderr)
            device = "cpu"
            compute_type = args.compute_type or "int8"
            model = WhisperModel(model_ref, device=device, compute_type=compute_type)
        else:
            raise

    language = None if args.language in (None, "", "auto") else args.language
    segments, info = model.transcribe(
        str(video),
        language=language,
        beam_size=args.beam_size,
        vad_filter=not args.no_vad,
        word_timestamps=False,
    )
    print(f"检测语言：{info.language}（置信度 {info.language_probability:.1%}）")

    temporary = output.with_name(output.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            interactive_progress = sys.stderr.isatty()
            next_report = 0
            with tqdm(total=duration, unit="s", desc="转写进度", disable=not interactive_progress) as progress:
                for index, segment in enumerate(segments, start=1):
                    text = segment.text.strip()
                    if not text:
                        continue
                    handle.write(
                        f"{index}\n{format_timestamp(segment.start)} --> "
                        f"{format_timestamp(segment.end)}\n{text}\n\n"
                    )
                    processed = max(0.0, min(segment.end, duration))
                    progress.update(processed - progress.n)
                    percent = int(processed * 100 / duration) if duration else 100
                    if not interactive_progress and (percent >= next_report or processed >= duration):
                        print(
                            f"转写进度：{format_timestamp(processed)} / {format_timestamp(duration)} "
                            f"({percent}%)",
                            flush=True,
                        )
                        next_report = min(100, (percent // 5 + 1) * 5)
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()

    print(f"SRT 已保存：{output}")
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="使用 faster-whisper 将视频转写为 SRT")
    parser.add_argument("video", help="输入视频文件")
    parser.add_argument("-o", "--output", help="输出 SRT；默认与视频同名")
    parser.add_argument("-l", "--language", default="auto", help="语言代码；默认 auto 自动检测")
    parser.add_argument("-m", "--model", default=DEFAULT_MODEL, help="模型名称或本地模型路径；默认 turbo")
    parser.add_argument("-d", "--device", choices=("cpu", "cuda", "auto"), default="auto")
    parser.add_argument("--compute-type", help="CTranslate2 计算类型；默认按设备选择")
    parser.add_argument("--model-dir", default=str(DEFAULT_MODEL_DIR), help="模型缓存目录")
    parser.add_argument("--beam-size", type=int, default=5)
    parser.add_argument("--no-vad", action="store_true", help="关闭 VAD 过滤")
    parser.add_argument("-f", "--force", action="store_true", help="覆盖已存在的输出")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        ensure_best_python()

        # 模型目录优先级：显式 --model-dir > VIDEO_TO_SRT_MODEL_DIR > 已发现环境旁 models > 用户缓存
        if (
            MODEL_DIR_ENV_VAR not in os.environ
            and args.model_dir == str(DEFAULT_MODEL_DIR)
            and SELECTED_VENV_MODEL_DIR is not None
            and SELECTED_VENV_MODEL_DIR.exists()
        ):
            args.model_dir = str(SELECTED_VENV_MODEL_DIR)
            print(f"[video-to-srt] 使用环境内模型目录: {args.model_dir}")

        transcribe(args)
        return 0
    except Exception as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
