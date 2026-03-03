import sys
import os
import logging
from pathlib import Path
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                               QLabel, QLineEdit, QPushButton, QComboBox, QTextEdit, 
                               QGroupBox, QFormLayout, QFileDialog, QMessageBox, QProgressBar,
                               QDialog, QDialogButtonBox)
from PySide6.QtCore import Qt, QMimeData, Slot
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QFont

from utils import ConfigManager
from core_worker import SubtitleWorker

logger = logging.getLogger(__name__)

# ── 模型推荐判定规则（三级：⭐最佳 / ✅可用 / ❌不推荐） ────────
# NOTE: 当前使用场景为「字幕翻译」，推荐依据：
#   - 最佳推荐：经过验证的、速度快且翻译质量高的主力文本模型
#   - 可用：通用文本模型，能翻译但非最优选择
#   - 不推荐：非文本任务模型（embedding/音频/图像/视频/代码等）

# Gemini：最佳翻译模型白名单（关键词匹配，命中任一即为最佳）
# NOTE: flash 系列速度快且免费额度高，pro 系列质量更高但较慢
_GEMINI_BEST_KEYWORDS = [
    "gemini-2.5-flash", "gemini-2.5-pro",
    "gemini-2.0-flash",
    "gemini-1.5-flash", "gemini-1.5-pro",
]

# Gemini：明确不适合翻译的模型（关键词匹配，命中即为不推荐）
_GEMINI_EXCLUDE_KEYWORDS = [
    "embedding", "aqa", "imagen", "veo", "bisheng",
    "learnlm", "codestral", "exp",
]

# 千问：最佳翻译模型白名单
# NOTE: plus 性价比最高，max 质量最好，turbo 速度最快
_QWEN_BEST_KEYWORDS = [
    "qwen-plus", "qwen-max", "qwen-turbo", "qwen-long",
    "qwen3-235b", "qwen3-32b", "qwen3-30b",
]

# 千问：明确不适合翻译的模型
_QWEN_EXCLUDE_KEYWORDS = [
    "embedding", "audio", "-vl", "image", "ocr", "math",
    "coder", "code", "rerank", "paraformer", "sambert",
    "wordart", "cosyvoice", "farui", "marco",
]


def _rate_gemini_model(model_name: str) -> str:
    """
    对 Gemini 模型进行翻译场景的三级评分。
    @returns 'best' | 'ok' | 'bad'
    """
    name_lower = model_name.lower()
    # 先检查黑名单
    for kw in _GEMINI_EXCLUDE_KEYWORDS:
        if kw in name_lower:
            return "bad"
    # 再检查最佳白名单
    for kw in _GEMINI_BEST_KEYWORDS:
        if name_lower.startswith(kw):
            return "best"
    # 其余为可用但非最佳
    return "ok"


def _rate_qwen_model(model_name: str) -> str:
    """
    对千问模型进行翻译场景的三级评分。
    @returns 'best' | 'ok' | 'bad'
    """
    name_lower = model_name.lower()
    # 先检查黑名单
    for kw in _QWEN_EXCLUDE_KEYWORDS:
        if kw in name_lower:
            return "bad"
    # 再检查最佳白名单
    for kw in _QWEN_BEST_KEYWORDS:
        if name_lower.startswith(kw):
            return "best"
    # 其余为可用但非最佳
    return "ok"


# 评分到显示标签的映射
_RATING_LABELS = {
    "best": "⭐最佳推荐",
    "ok": "✅可用",
    "bad": "❌不推荐",
}

# 评分排序权重（数字越小排越前）
_RATING_ORDER = {"best": 0, "ok": 1, "bad": 2}


class DragDropWidget(QLabel):
    """
    自定义的拖拽上传控件组件。
    继承自 QLabel，支持通过鼠标点击选择视频文件，
    也支持直接将文件拖拽进入由于虚线包裹的上传区域。
    """
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAlignment(Qt.AlignCenter)
        self.setText("\n\n拖拽视频文件到此处\n或点击选择文件\n\n")
        self.setStyleSheet("""
            QLabel {
                border: 2px dashed #aaa;
                border-radius: 10px;
                color: #666;
                font-size: 16px;
                background-color: #f9f9f9;
            }
            QLabel:hover {
                border-color: #3b82f6;
                background-color: #eff6ff;
            }
        """)
        self.setAcceptDrops(True)
        self.filePath = None

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent):
        urls = event.mimeData().urls()
        if urls:
            file_path = urls[0].toLocalFile()
            self.set_file(file_path)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            file_path, _ = QFileDialog.getOpenFileName(
                self, "选择视频文件", "", "Video Files (*.mp4 *.mkv *.mov *.avi *.mp3 *.wav)"
            )
            if file_path:
                self.set_file(file_path)

    def set_file(self, file_path):
        self.filePath = file_path
        self.setText(f"已选择文件:\n{Path(file_path).name}")
        self.setStyleSheet("""
            QLabel {
                border: 2px solid #10b981;
                border-radius: 10px;
                color: #10b981;
                font-size: 16px;
                background-color: #ecfdf5;
            }
        """)
        # 通知父级或其他组件（如果有需要）

class SettingsDialog(QDialog):
    """
    API 偏好设置对话框。
    在此界面配置选择的翻译服务商（Provider: Gemini / 千问 / 讯飞），
    以及对应的鉴权密钥、翻译模型和 System Prompt。
    提供获取 Gemini / 千问可用模型的在线查询功能，并标注推荐/不推荐。
    """
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("API 设置")
        self.resize(520, 600)
        self.config = config
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)

        # Provider selector
        top_form = QFormLayout()
        self.provider_combo = QComboBox()
        self.provider_combo.addItems(["Gemini", "千问", "讯飞"])
        # 映射 provider 内部值到显示文本
        provider_display_map = {"gemini": "Gemini", "qwen": "千问", "xunfei": "讯飞"}
        saved_provider = self.config.get("provider", "gemini")
        self.provider_combo.setCurrentText(
            provider_display_map.get(saved_provider, "Gemini")
        )
        top_form.addRow("翻译提供商:", self.provider_combo)
        layout.addLayout(top_form)

        # ── Gemini 设置组 ──────────────────────────────────────
        self.gemini_group = QGroupBox("Gemini 设置")
        gemini_form = QFormLayout(self.gemini_group)
        self.gemini_key_edit = QLineEdit(self.config.get("gemini_api_key", ""))
        self.gemini_key_edit.setEchoMode(QLineEdit.Password)
        self.gemini_key_edit.setPlaceholderText("AIza...")
        gemini_form.addRow("API Key:", self.gemini_key_edit)

        model_row = QHBoxLayout()
        self.model_combo = QComboBox()
        saved_model = self.config.get("gemini_model", "gemini-1.5-flash")
        self.model_combo.addItem(saved_model)
        self.model_combo.setCurrentText(saved_model)
        self.fetch_models_btn = QPushButton("获取模型列表")
        self.fetch_models_btn.clicked.connect(self._fetch_gemini_models)
        model_row.addWidget(self.model_combo, 1)
        model_row.addWidget(self.fetch_models_btn)
        gemini_form.addRow("模型:", model_row)

        # 翻译提示词编辑框（System Prompt）
        self.prompt_edit = QTextEdit()
        self.prompt_edit.setPlaceholderText("在此输入发送给 Gemini 的翻译指令...")
        self.prompt_edit.setPlainText(self.config.get("gemini_system_prompt", ""))
        # NOTE: 固定高度约 6 行，保持对话框紧凑
        self.prompt_edit.setFixedHeight(140)
        gemini_form.addRow("翻译提示词:\n(System Prompt)", self.prompt_edit)
        layout.addWidget(self.gemini_group)

        # ── 千问设置组 ──────────────────────────────────────────
        self.qwen_group = QGroupBox("千问 设置")
        qwen_form = QFormLayout(self.qwen_group)
        self.qwen_key_edit = QLineEdit(self.config.get("qwen_api_key", ""))
        self.qwen_key_edit.setEchoMode(QLineEdit.Password)
        self.qwen_key_edit.setPlaceholderText("sk-...")
        qwen_form.addRow("API Key:", self.qwen_key_edit)

        qwen_model_row = QHBoxLayout()
        self.qwen_model_combo = QComboBox()
        saved_qwen_model = self.config.get("qwen_model", "qwen-plus")
        self.qwen_model_combo.addItem(saved_qwen_model)
        self.qwen_model_combo.setCurrentText(saved_qwen_model)
        self.fetch_qwen_models_btn = QPushButton("获取模型列表")
        self.fetch_qwen_models_btn.clicked.connect(self._fetch_qwen_models)
        qwen_model_row.addWidget(self.qwen_model_combo, 1)
        qwen_model_row.addWidget(self.fetch_qwen_models_btn)
        qwen_form.addRow("模型:", qwen_model_row)

        # 千问翻译提示词编辑框（System Prompt）
        self.qwen_prompt_edit = QTextEdit()
        self.qwen_prompt_edit.setPlaceholderText("在此输入发送给千问的翻译指令...")
        self.qwen_prompt_edit.setPlainText(self.config.get("qwen_system_prompt", ""))
        self.qwen_prompt_edit.setFixedHeight(140)
        qwen_form.addRow("翻译提示词:\n(System Prompt)", self.qwen_prompt_edit)
        layout.addWidget(self.qwen_group)

        # ── 讯飞设置组 ──────────────────────────────────────────
        self.xunfei_group = QGroupBox("讯飞 设置")
        xunfei_form = QFormLayout(self.xunfei_group)
        self.app_id_edit = QLineEdit(self.config.get("app_id", ""))
        self.api_secret_edit = QLineEdit(self.config.get("api_secret", ""))
        self.api_secret_edit.setEchoMode(QLineEdit.Password)
        self.api_key_edit = QLineEdit(self.config.get("api_key", ""))
        self.api_key_edit.setEchoMode(QLineEdit.Password)
        xunfei_form.addRow("AppID:", self.app_id_edit)
        xunfei_form.addRow("APISecret:", self.api_secret_edit)
        xunfei_form.addRow("APIKey:", self.api_key_edit)
        layout.addWidget(self.xunfei_group)

        # Source lang (shared)
        lang_form = QFormLayout()
        self.lang_combo = QComboBox()
        self.lang_combo.addItems(["自动", "英语", "日语"])
        self.lang_combo.setCurrentText(self.config.get("source_lang", "自动"))
        lang_form.addRow("源语言:", self.lang_combo)
        layout.addLayout(lang_form)

        self.provider_combo.currentTextChanged.connect(self._on_provider_changed)
        self._on_provider_changed(self.provider_combo.currentText())

        button_box = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def _fetch_gemini_models(self):
        """
        通过 Gemini API Key 查询可用模型列表。
        每个模型会根据翻译场景标注「✅推荐」或「❌不推荐」。
        """
        api_key = self.gemini_key_edit.text().strip()
        if not api_key:
            QMessageBox.warning(self, "提示", "请先填写 Gemini API Key")
            return

        logger.info("[Gemini] 开始查询可用模型列表...")
        self.fetch_models_btn.setEnabled(False)
        self.fetch_models_btn.setText("查询中...")

        try:
            import google.generativeai as genai
            genai.configure(api_key=api_key)
            logger.info("[Gemini] 已配置 API Key，正在调用 list_models()...")

            raw_models = [
                m.name.replace("models/", "") for m in genai.list_models()
                if "generateContent" in m.supported_generation_methods
            ]

            logger.info(f"[Gemini] 查询成功，获取到 {len(raw_models)} 个支持 generateContent 的模型")

            # 三级评分 + 按评分排序（最佳在前，不推荐在后）
            rated = [(m, _rate_gemini_model(m)) for m in raw_models]
            rated.sort(key=lambda x: _RATING_ORDER[x[1]])

            # 构建带标注的显示列表
            display_items = []
            best_count = ok_count = bad_count = 0
            for m, rating in rated:
                label = _RATING_LABELS[rating]
                display_items.append(f"{m}  {label}")
                if rating == "best":
                    best_count += 1
                elif rating == "ok":
                    ok_count += 1
                else:
                    bad_count += 1
                logger.info(f"  [Gemini] 模型: {m} -> {label}")

            self.model_combo.clear()
            self.model_combo.addItems(display_items)

            # 尝试恢复之前保存的模型选择，否则默认选中第一个（已按评分排序，最佳在前）
            saved = self.config.get("gemini_model", "gemini-1.5-flash")
            for i, item in enumerate(display_items):
                if item.startswith(saved):
                    self.model_combo.setCurrentIndex(i)
                    break

            logger.info(
                f"[Gemini] 模型列表已更新："
                f"{best_count} 个最佳推荐 / {ok_count} 个可用 / {bad_count} 个不推荐"
            )

        except Exception as e:
            error_msg = str(e)
            logger.error(f"[Gemini] 查询模型列表失败: {error_msg}", exc_info=True)

            # 为用户提供更具体的错误诊断
            if "API_KEY_INVALID" in error_msg or "401" in error_msg:
                detail = "API Key 无效或已过期，请检查后重试。"
            elif "PERMISSION_DENIED" in error_msg or "403" in error_msg:
                detail = "API Key 权限不足，请确认该 Key 已开通 Generative AI 服务。"
            elif "RESOURCE_EXHAUSTED" in error_msg or "429" in error_msg:
                detail = "请求频率超限，请稍后重试。"
            elif "connection" in error_msg.lower() or "timeout" in error_msg.lower():
                detail = "网络连接失败，请检查网络环境（是否需要代理）。"
            else:
                detail = error_msg

            QMessageBox.critical(self, "获取 Gemini 模型列表失败", f"错误详情：\n{detail}")

        finally:
            self.fetch_models_btn.setEnabled(True)
            self.fetch_models_btn.setText("获取模型列表")

    def _fetch_qwen_models(self):
        """
        通过千问 API Key 查询 DashScope 平台可用模型列表。
        使用 OpenAI 兼容接口的 models.list() 方法。
        每个模型会根据翻译场景标注「✅推荐」或「❌不推荐」。
        """
        api_key = self.qwen_key_edit.text().strip()
        if not api_key:
            QMessageBox.warning(self, "提示", "请先填写千问 API Key")
            return

        logger.info("[千问] 开始查询可用模型列表...")
        self.fetch_qwen_models_btn.setEnabled(False)
        self.fetch_qwen_models_btn.setText("查询中...")

        try:
            from openai import OpenAI
            from utils import QWEN_BASE_URL

            logger.info(f"[千问] 正在连接 DashScope API (base_url={QWEN_BASE_URL})...")
            client = OpenAI(api_key=api_key, base_url=QWEN_BASE_URL)

            response = client.models.list()
            raw_models = sorted([m.id for m in response.data])

            logger.info(f"[千问] 查询成功，获取到 {len(raw_models)} 个可用模型")

            # 三级评分 + 按评分排序
            rated = [(m, _rate_qwen_model(m)) for m in raw_models]
            rated.sort(key=lambda x: _RATING_ORDER[x[1]])

            # 构建带标注的显示列表
            display_items = []
            best_count = ok_count = bad_count = 0
            for m, rating in rated:
                label = _RATING_LABELS[rating]
                display_items.append(f"{m}  {label}")
                if rating == "best":
                    best_count += 1
                elif rating == "ok":
                    ok_count += 1
                else:
                    bad_count += 1
                logger.info(f"  [千问] 模型: {m} -> {label}")

            self.qwen_model_combo.clear()
            self.qwen_model_combo.addItems(display_items)

            # 尝试恢复之前保存的模型选择
            saved = self.config.get("qwen_model", "qwen-plus")
            for i, item in enumerate(display_items):
                if item.startswith(saved):
                    self.qwen_model_combo.setCurrentIndex(i)
                    break

            logger.info(
                f"[千问] 模型列表已更新："
                f"{best_count} 个最佳推荐 / {ok_count} 个可用 / {bad_count} 个不推荐"
            )

        except Exception as e:
            error_msg = str(e)
            logger.error(f"[千问] 查询模型列表失败: {error_msg}", exc_info=True)

            # 为用户提供更具体的错误诊断
            if "Unauthorized" in error_msg or "401" in error_msg:
                detail = "API Key 无效或已过期，请检查后重试。"
            elif "Forbidden" in error_msg or "403" in error_msg:
                detail = "API Key 权限不足，请确认该 Key 已开通模型服务。"
            elif "429" in error_msg:
                detail = "请求频率超限，请稍后重试。"
            elif "connection" in error_msg.lower() or "timeout" in error_msg.lower():
                detail = "网络连接失败，请检查网络环境或 DashScope 服务状态。"
            else:
                detail = error_msg

            QMessageBox.critical(self, "获取千问模型列表失败", f"错误详情：\n{detail}")

        finally:
            self.fetch_qwen_models_btn.setEnabled(True)
            self.fetch_qwen_models_btn.setText("获取模型列表")

    def _on_provider_changed(self, text):
        """根据选中的翻译提供商，联动显示/隐藏对应的设置组"""
        self.gemini_group.setVisible(text == "Gemini")
        self.qwen_group.setVisible(text == "千问")
        self.xunfei_group.setVisible(text == "讯飞")

    def get_settings(self):
        """
        收集对话框中当前所有设置项，返回配置字典。
        模型名称会剥离尾部的推荐/不推荐标注，只保留纯模型 ID。
        """
        # 从「模型名  ✅推荐」格式中提取纯模型名
        gemini_model_raw = self.model_combo.currentText()
        gemini_model = gemini_model_raw.split("  ")[0].strip()

        qwen_model_raw = self.qwen_model_combo.currentText()
        qwen_model = qwen_model_raw.split("  ")[0].strip()

        # 映射显示文本到内部 provider 值
        provider_map = {"Gemini": "gemini", "千问": "qwen", "讯飞": "xunfei"}
        provider = provider_map.get(self.provider_combo.currentText(), "gemini")

        return {
            "provider": provider,
            "gemini_api_key": self.gemini_key_edit.text().strip(),
            "gemini_model": gemini_model,
            "gemini_system_prompt": self.prompt_edit.toPlainText().strip(),
            "qwen_api_key": self.qwen_key_edit.text().strip(),
            "qwen_model": qwen_model,
            "qwen_system_prompt": self.qwen_prompt_edit.toPlainText().strip(),
            "app_id": self.app_id_edit.text().strip(),
            "api_key": self.api_key_edit.text().strip(),
            "api_secret": self.api_secret_edit.text().strip(),
            "source_lang": self.lang_combo.currentText()
        }

class MainWindow(QMainWindow):
    """
    应用程序主窗口（Main Window）。
    承载拖拽上传组件、设置按钮、任务控制（开始/取消）、进度条和状态日志输出。
    负责初始化配置加载，并将 UI 的操作绑定到后端的 SubtitleWorker 独立工作线程。
    """
    def __init__(self):
        super().__init__()
        self.setWindowTitle("智能视频字幕生成工具 (Hybrid)")
        self.resize(600, 700)
        self.config_manager = ConfigManager()
        self.worker = None
        self.current_config = {}

        self.setup_ui()
        self.load_settings()

    def setup_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)
        main_layout = QVBoxLayout(central_widget)
        main_layout.setSpacing(15)
        main_layout.setContentsMargins(20, 20, 20, 20)

        # 1. 标题
        title_label = QLabel("字幕生成工具")
        title_label.setFont(QFont("Segoe UI", 18, QFont.Bold))
        title_label.setAlignment(Qt.AlignCenter)
        main_layout.addWidget(title_label)

        # 2. 文件上传区
        self.upload_area = DragDropWidget()
        self.upload_area.setFixedHeight(150)
        main_layout.addWidget(self.upload_area)

        # 3. 设置按钮
        self.settings_btn = QPushButton("⚙️ API 设置")
        self.settings_btn.setFixedHeight(40)
        self.settings_btn.clicked.connect(self.open_settings)
        main_layout.addWidget(self.settings_btn)

        # 4. 操作按钮
        btn_layout = QHBoxLayout()
        self.start_btn = QPushButton("开始生成")
        self.start_btn.setFixedHeight(40)
        self.start_btn.setStyleSheet("background-color: #3b82f6; color: white; font-weight: bold; border-radius: 5px;")
        self.start_btn.clicked.connect(self.start_process)
        
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setFixedHeight(40)
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setStyleSheet("background-color: #ef4444; color: white; font-weight: bold; border-radius: 5px;")
        self.cancel_btn.clicked.connect(self.cancel_process)

        btn_layout.addWidget(self.start_btn)
        btn_layout.addWidget(self.cancel_btn)
        main_layout.addLayout(btn_layout)

        # 5. 进度条
        self.progress_bar = QProgressBar()
        self.progress_bar.setValue(0)
        self.progress_bar.setAlignment(Qt.AlignCenter)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.hide()
        main_layout.addWidget(self.progress_bar)

        # 6. 日志区
        self.log_area = QTextEdit()
        self.log_area.setReadOnly(True)
        self.log_area.setPlaceholderText("运行日志将显示在这里...")
        main_layout.addWidget(self.log_area)

    def load_settings(self):
        self.current_config = self.config_manager.load_config()

    def open_settings(self):
        dialog = SettingsDialog(self.current_config, self)
        if dialog.exec():
            new_config = dialog.get_settings()
            self.current_config.update(new_config)
            self.config_manager.save_config(self.current_config)
            self.append_log("API 配置已更新")

    def start_process(self):
        file_path = self.upload_area.filePath
        if not file_path:
            QMessageBox.warning(self, "提示", "请先选择视频文件")
            return

        provider = self.current_config.get("provider", "gemini")

        # 根据选中的翻译提供商，校验必要的配置项
        if provider == "gemini" and not self.current_config.get("gemini_api_key"):
            QMessageBox.warning(self, "提示", '请先点击"API 设置"填写 Gemini API Key')
            self.open_settings()
            return
        elif provider == "qwen" and not self.current_config.get("qwen_api_key"):
            QMessageBox.warning(self, "提示", '请先点击"API 设置"填写千问 API Key')
            self.open_settings()
            return
        elif provider == "xunfei" and not all([
            self.current_config.get("app_id"),
            self.current_config.get("api_key"),
            self.current_config.get("api_secret")
        ]):
            QMessageBox.warning(self, "提示", '请先点击"API 设置"填写讯飞配置信息')
            self.open_settings()
            return
            
        config = self.current_config

        self.log_area.clear()
        self.progress_bar.setValue(0)
        self.progress_bar.show()
        self.toggle_ui(running=True)

        self.worker = SubtitleWorker(file_path, config)
        self.worker.signals.log.connect(self.append_log)
        self.worker.signals.progress.connect(self.update_progress)
        self.worker.signals.error.connect(self.handle_error)
        self.worker.signals.finished.connect(self.handle_finish)
        
        self.worker.start()

    def cancel_process(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.append_log("正在取消任务...")
            self.cancel_btn.setEnabled(False)

    def toggle_ui(self, running):
        self.start_btn.setEnabled(not running)
        self.cancel_btn.setEnabled(running)
        self.upload_area.setEnabled(not running)
        self.settings_btn.setEnabled(not running)

    @Slot(str)
    def append_log(self, text):
        self.log_area.append(text)

    @Slot(str, int)
    def update_progress(self, msg, val):
        self.progress_bar.setValue(val)
        self.progress_bar.setFormat(f"{msg} %p%")

    @Slot(str)
    def handle_error(self, err_msg):
        self.append_log(f"\n[错误] {err_msg}")
        self.toggle_ui(running=False)
        self.progress_bar.hide()
        QMessageBox.critical(self, "任务失败", err_msg)

    @Slot(str)
    def handle_finish(self, output_path):
        self.append_log(f"\n[完成] 字幕文件已保存至: {output_path}")
        self.toggle_ui(running=False)
        QMessageBox.information(self, "完成", f"字幕生成成功！\n文件路径: {output_path}")

if __name__ == "__main__":
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec())
