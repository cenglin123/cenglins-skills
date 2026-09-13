import os
import tempfile
from pathlib import Path

import batch_transcribe
import download
import srt_guard
import srt_to_txt
import transcribe


SAMPLE_SRT = """1
00:00:00,000 --> 00:00:01,500
欢迎使用 faster whisper。

2
00:00:01,500 --> 00:00:03,000
这是第二行字幕。
"""


def main() -> int:
    assert transcribe.format_timestamp(3661.2344) == "01:01:01,234"

    with tempfile.TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        subtitle = root / "sample.srt"
        subtitle.write_text(SAMPLE_SRT, encoding="utf-8", newline="\n")

        entries = srt_guard.parse_srt(subtitle)
        assert len(entries) == 2
        assert entries[0].index == 1

        backup = root / "sample.srt.bak"
        assert srt_guard.backup(subtitle, backup, False) == 0
        assert backup.read_bytes() == subtitle.read_bytes()

        corrected = SAMPLE_SRT.replace("faster whisper", "faster-whisper")
        subtitle.write_text(corrected, encoding="utf-8", newline="\n")
        assert srt_guard.check(backup, subtitle) == 0

        text = srt_to_txt.extract_text(subtitle, join_lines=False)
        assert "faster-whisper" in text
        assert "00:00:" not in text

        media = root / "media"
        media.mkdir()
        (media / "a.mp4").touch()
        (media / "b.mkv").touch()
        (media / "ignore.txt").touch()
        videos = batch_transcribe.collect_videos(media, True, {".mp4", ".mkv"})
        assert [item.name for item in videos] == ["a.mp4", "b.mkv"]

        previous = Path.cwd()
        try:
            os.chdir(root)
            defaults = download.build_parser().parse_args([])
        finally:
            os.chdir(previous)
        assert Path(defaults.output_dir) == root / "Videos"
        assert Path(defaults.cookies) == root / "cookies.txt"

    print("video-to-srt self-test passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
