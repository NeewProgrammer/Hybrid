# Windows 桌面端智能视频字幕生成工具 (Hybrid版)

本项目是一个运行于 Windows 平台的桌面应用程序，用于将外语（英语、日语）视频自动转换为中英/中日双语字幕文件（.srt）。

## 核心功能

*   **本地语音识别 (ASR)**: 使用 SenseVoiceSmall 模型进行离线语音识别。
*   **云端机器翻译 (MT)**: 使用讯飞机器翻译 API 进行翻译。
*   **双语字幕生成**: 生成标准 SRT 格式的时间轴精准字幕。

## 环境要求

1.  **Python 3.8+**
2.  **FFmpeg**: 必须安装 FFmpeg 并将其添加到系统 PATH 中。
    *   验证方法: 在终端运行 `ffmpeg -version`。
3.  **依赖库**: 运行 `pip install -r requirements.txt` 安装所需 Python 库。

## 安装与运行

1.  克隆或下载本项目。
2.  安装依赖:
    ```bash
    pip install -r requirements.txt
    ```
### 2. 生成 EXE 可执行文件
为了方便分发和使用，您可以将项目打包为独立的 .exe 文件：

1.  **双击运行 `install_env.bat`**  
    (确保已安装 PyInstaller 打包工具)
2.  **双击运行 `build_exe.bat`**  
    (从命令行构建，自动处理隐藏依赖)

构建完成后，可执行文件位于 `dist\HybridSubtitleTool\HybridSubtitleTool.exe`。
您可以将整个 `HybridSubtitleTool` 文件夹复制到其他电脑上运行（需确保目标电脑已安装 FFmpeg）。

## 手动运行开发版
1. 安装依赖: `pip install -r requirements.txt`
2. 运行: `python src/main.py`

## 注意事项

*   首次运行会自动从 ModelScope 下载 ASR 模型，请保持网络连接。
*   需要自行申请讯飞开放平台的机器翻译 API 权限 (AppID, APISecret, APIKey)。
