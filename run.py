import tkinter as tk
from tkinter import ttk, scrolledtext
from tkinterdnd2 import DND_FILES, TkinterDnD
from pathlib import Path
import subprocess
from PIL import Image
import sys
import threading

# 全局变量
dropped_files = []
OUTPUT_DIR = Path(__file__).parent / "output"
output_to_dir_var = None  # 控制是否输出到output文件夹的开关
is_processing = False
progress_var = None
channel_var = None
log_text = None
progress_label = None
root = None

# -------------------------- 核心修复：健壮的拖放文件解析函数 --------------------------
def parse_dropped_files(raw_data):
    """
    解析拖放的文件路径，兼容以下场景：
    1. Windows标准多文件格式：{路径1} {路径2}（路径含空格）
    2. 无大括号空格分隔：路径1 路径2（路径不含空格）
    3. 路径带引号："路径1" "路径2"（路径含空格）
    4. 单文件（任意格式）
    """
    file_paths = []
    if not raw_data:
        return file_paths

    # 场景1：处理Windows标准多文件格式（首尾大括号）
    if raw_data.startswith("{") and raw_data.endswith("}"):
        inner_data = raw_data[1:-1]
        split_paths = inner_data.split("} {")
        for path in split_paths:
            clean_path = path.strip().strip('"').strip("'")
            if clean_path:
                file_paths.append(clean_path)
    else:
        # 场景2/3：无大括号 → 处理引号包裹/空格分隔的多文件
        temp_paths = []
        # 先按双引号拆分（处理带空格的路径："C:/a b.mp3" "C:/c d.mp3"）
        parts = raw_data.split('"')
        for part in parts:
            part = part.strip()
            if part:  # 非空部分才保留
                temp_paths.append(part)
        
        # 如果按引号拆分后只有1个元素 → 说明是纯空格分隔（无引号）
        if len(temp_paths) == 1:
            # 拆分空格分隔的路径（仅当路径本身不含空格时有效，是最常见的场景）
            temp_paths = [p for p in temp_paths[0].split() if p.strip()]
        
        # 清理每个路径
        for path in temp_paths:
            clean_path = path.strip().strip('"').strip("'")
            if clean_path:
                file_paths.append(clean_path)

    # 去重 + 过滤真实存在的文件
    valid_paths = []
    for path in list(set(file_paths)):
        if path and Path(path).is_file():
            valid_paths.append(path)
    return valid_paths

# -------------------------- 工具函数 --------------------------
def ensure_output_dir():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR

def get_output_file(original_file: Path, ext: str) -> Path:
    if output_to_dir_var.get():  # 勾选则输出到output文件夹
        return ensure_output_dir() / f"{original_file.stem}{ext}"
    else:  # 未勾选则输出到原文件同目录
        return original_file.parent / f"{original_file.stem}_fin{ext}"

# -------------------------- 耗时处理函数 --------------------------
def run_ffmpeg_safe(input_file: Path, output_file: Path, channels: str) -> tuple[bool, str]:
    ffmpeg_cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(input_file),
        "-ac", channels, "-c:a", "libvorbis", str(output_file), "-y"
    ]
    try:
        result = subprocess.run(
            ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="ignore", timeout=10
        )
        if result.returncode != 0:
            return False, f"FFmpeg错误：{result.stderr[:100]}"
        if not output_file.exists() or output_file.stat().st_size == 0:
            return False, "输出文件为空"
        return True, ""
    except subprocess.TimeoutExpired:
        return False, "处理超时（10秒）"
    except FileNotFoundError:
        return False, "未找到ffmpeg（需安装）"
    except Exception as e:
        return False, f"异常：{str(e)[:100]}"

def process_single_file(file: Path, channels: str):
    suffix = file.suffix.lower()
    audio_exts = [".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".wma"]
    image_exts = [".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".webp", ".gif"]

    if suffix in audio_exts:
        output_file = get_output_file(file, ".ogg")
        success, err = run_ffmpeg_safe(file, output_file, "1" if channels == "单声道" else "2")
        if success:
            try:
                file.unlink()
                log(f"✅ 音频处理完成：{file.name}")
            except Exception as e:
                log(f"✅ 音频转换成功，删原文件失败：{e}")
        else:
            log(f"❌ 音频处理失败：{file.name} - {err}")
    elif suffix in image_exts:
        output_file = get_output_file(file, ".png")
        try:
            with Image.open(file) as img:
                img_mode = "RGBA" if img.mode in ("RGBA", "LA") else "RGB"
                img.convert(img_mode).save(
                    output_file, format="PNG", optimize=True, compress_level=9, exif=None
                )
            try:
                file.unlink()
                log(f"✅ 图片处理完成：{file.name}")
            except Exception as e:
                log(f"⚠️ 图片转换成功，删原文件失败：{file.name} - {str(e)[:100]}")
        except Exception as e:
            log(f"❌ 图片处理失败：{file.name} - {str(e)[:100]}")
    else:
        log(f"⚠️ 不支持的文件：{file.name}")

# -------------------------- 进度反馈函数 --------------------------
def log(msg: str):
    """线程安全的日志输出"""
    def safe_log():
        log_text.config(state=tk.NORMAL)
        log_text.insert(tk.END, msg + "\n")
        log_text.see(tk.END)
        log_text.config(state=tk.DISABLED)
    root.after(0, safe_log)

def update_progress(current: int, total: int):
    """线程安全更新进度条+文字提示"""
    percent = (current / total) * 100 if total > 0 else 0
    def safe_update():
        progress_var.set(percent)
        progress_label.config(text=f"进度：{current}/{total} ({percent:.1f}%)")
    root.after(0, safe_update)

def batch_process(channels):
    """子线程批量处理（修复声道值获取，去掉sleep）"""
    global is_processing
    try:  # 新增顶层捕获
        total_files = len(dropped_files)
        if total_files == 0:
            log("⚠️ 无文件可处理！")
            is_processing = False
            return

        log("\n========== 开始处理 ==========")
        log(f"📁 输出目录：{ensure_output_dir().absolute()}")
        update_progress(0, total_files)

        # 遍历处理
        for idx, file in enumerate(dropped_files, 1):
            if not is_processing:
                break
            process_single_file(file, channels)
            update_progress(idx, total_files)

        # 处理完成
        update_progress(total_files, total_files)
        log(f"\n🎉 处理完成！共处理 {total_files} 个文件")
        log(f"📂 结果文件：{OUTPUT_DIR.absolute()}\n")
    except Exception as e:
        log(f"❌ 批量处理异常：{str(e)}")
    finally:  # 新增finally确保重置状态
        is_processing = False

# -------------------------- GUI事件处理（核心修复：批量拖放+追加逻辑） --------------------------
def on_drop(event):
    """拖放文件：追加而非覆盖，兼容批量拖放"""
    global dropped_files
    try:
        # 1. 打印原始拖放数据（方便排查问题）
        raw_data = event.data.strip()
        # log(f"🔍 原始拖放数据：{raw_data}")

        # 2. 解析有效文件路径
        valid_paths = parse_dropped_files(raw_data)
        if not valid_paths:
            log("⚠️ 本次未识别到有效文件（可能格式不支持/路径错误）")
            update_progress(0, len(dropped_files))
            return

        # 3. 追加新文件（去重，避免重复添加）
        new_files = []
        for path in valid_paths:
            file = Path(path)
            if file not in dropped_files:  # 去重
                dropped_files.append(file)
                new_files.append(file)

        # 4. 反馈结果
        if new_files:
            for f in new_files:
                log(f"📥 已添加：{f.name}")
            log(f"✅ 本次添加 {len(new_files)} 个有效文件，累计 {len(dropped_files)} 个")
        else:
            log(f"⚠️ 本次拖放的文件已存在，未重复添加")

        # 5. 更新进度
        update_progress(0, len(dropped_files))

    except Exception as e:
        log(f"❌ 拖放解析失败：{str(e)}")
        update_progress(0, len(dropped_files))

def on_process_click():
    """启动处理（主线程获取声道值，可靠）"""
    global is_processing
    if is_processing:
        log("⚠️ 正在处理中，请勿重复点击！")
        return
    is_processing = True
    # 主线程直接获取声道值，无需sleep
    channels = channel_var.get() or "单声道"
    threading.Thread(target=batch_process, args=(channels,), daemon=True).start()

def clear_files():
    """清空文件+重置进度"""
    global dropped_files, is_processing
    dropped_files.clear()
    is_processing = False
    update_progress(0, 0)
    log("\n🗑️ 已清空文件列表，进度已重置")

# -------------------------- GUI初始化 --------------------------
if __name__ == "__main__":
    root = TkinterDnD.Tk()
    root.title("素材处理工具（音频转OGG + 图片转PNG）")
    root.geometry("550x650")  # 放大窗口，方便看日志

    # 初始化tk相关全局变量
    progress_var = tk.DoubleVar()
    channel_var = tk.StringVar(value="单声道")

    # 顶部控制面板
    top_frame = ttk.Frame(root, padding="10")
    top_frame.pack(fill=tk.X)
    ttk.Label(top_frame, text="音频声道：").pack(side=tk.LEFT, padx=5)
    ttk.Radiobutton(top_frame, text="单声道", variable=channel_var, value="单声道").pack(side=tk.LEFT)
    ttk.Radiobutton(top_frame, text="双声道", variable=channel_var, value="双声道").pack(side=tk.LEFT)
    # 新增：输出目录开关复选框
    output_to_dir_var = tk.BooleanVar(value=False)  # 默认False（同目录）
    ttk.Checkbutton(top_frame, text="输出到output文件夹", variable=output_to_dir_var).pack(side=tk.RIGHT, padx=5)
    ttk.Button(top_frame, text="清空列表", command=clear_files).pack(side=tk.RIGHT, padx=5)
    ttk.Button(top_frame, text="开始处理", command=on_process_click).pack(side=tk.RIGHT)

    # 进度区域
    progress_frame = ttk.Frame(root, padding="10")
    progress_frame.pack(fill=tk.X, padx=10)
    progress_label = ttk.Label(progress_frame, text="进度：0/0 (0%)")
    progress_label.pack(side=tk.LEFT, padx=5)
    progress_bar = ttk.Progressbar(progress_frame, variable=progress_var, maximum=100)
    progress_bar.pack(fill=tk.X, expand=True, padx=5)

    # 拖放区域
    drop_frame = ttk.Frame(root, padding="10", relief=tk.GROOVE)
    drop_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
    drop_frame.configure(height=200)  # 强制设置最小高度（比如300像素，可按需调整）
    drop_frame.pack_propagate(False)  # 禁止Frame随内容收缩，保留最小高度
    ttk.Label(
        drop_frame,
        text="📌 批量拖入多个音频/图片文件到此处\n（支持多次拖放追加，路径含空格也可解析）",
        font=("微软雅黑", 12)
    ).pack()
    # 延迟绑定DND事件，等窗口完全初始化
    def init_dnd():
        drop_frame.drop_target_register(DND_FILES)
        drop_frame.dnd_bind('<<Drop>>', on_drop)
    root.after(200, init_dnd)  # 延迟200ms绑定

    # 日志区域
    log_frame = ttk.Frame(root, padding="10")
    log_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)
    ttk.Label(log_frame, text="处理日志（含拖放解析详情）：").pack(anchor=tk.W)
    log_text = scrolledtext.ScrolledText(log_frame, state=tk.DISABLED, font=("Consolas", 9))
    log_text.pack(fill=tk.BOTH, expand=True)

    def safe_quit():
        root.quit()  # 先退出主循环，再终止进程
        root.destroy()
        sys.exit(0)
        root.protocol("WM_DELETE_WINDOW", safe_quit)
    root.mainloop()