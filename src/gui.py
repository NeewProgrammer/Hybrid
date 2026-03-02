import sys
import os
from pathlib import Path
from PySide6.QtWidgets import (QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, 
                               QLabel, QLineEdit, QPushButton, QComboBox, QTextEdit, 
                               QGroupBox, QFormLayout, QFileDialog, QMessageBox, QProgressBar,
                               QDialog, QDialogButtonBox)
from PySide6.QtCore import Qt, QMimeData, Slot
from PySide6.QtGui import QDragEnterEvent, QDropEvent, QFont

from utils import ConfigManager
from core_worker import SubtitleWorker

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
    在此界面配置选择的翻译服务商（Provider: Gemini / 讯飞），
    以及对应的鉴权密钥、翻译模型和 System Prompt。
    提供获取 Gemini 可用模型的在线查询功能。
    """
    def __init__(self, config, parent=None):
        super().__init__(parent)
        self.setWindowTitle("API 设置")
        self.resize(480, 520)
        self.config = config
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout(self)

        # Provider selector
        top_form = QFormLayout()
        self.provider_combo = QComboBox()
        self.provider_combo.addItems(["Gemini", "讯飞"])
        self.provider_combo.setCurrentText(
            "Gemini" if self.config.get("provider", "gemini") == "gemini" else "讯飞"
        )
        top_form.addRow("翻译提供商:", self.provider_combo)
        layout.addLayout(top_form)

        # Gemini group
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

        # Xunfei group
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
        api_key = self.gemini_key_edit.text().strip()
        if not api_key:
            QMessageBox.warning(self, "提示", "请先填写 API Key")
            return
        try:
            import google.generativeai as genai
            genai.configure(api_key=api_key)
            models = [m.name.replace("models/", "") for m in genai.list_models()
                      if "generateContent" in m.supported_generation_methods]
            self.model_combo.clear()
            self.model_combo.addItems(models)
            saved = self.config.get("gemini_model", "gemini-1.5-flash")
            if saved in models:
                self.model_combo.setCurrentText(saved)
        except Exception as e:
            QMessageBox.critical(self, "获取失败", str(e))

    def _on_provider_changed(self, text):
        self.gemini_group.setVisible(text == "Gemini")
        self.xunfei_group.setVisible(text == "讯飞")

    def get_settings(self):
        return {
            "provider": "gemini" if self.provider_combo.currentText() == "Gemini" else "xunfei",
            "gemini_api_key": self.gemini_key_edit.text().strip(),
            "gemini_model": self.model_combo.currentText(),
            "gemini_system_prompt": self.prompt_edit.toPlainText().strip(),
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
        if provider == "gemini" and not self.current_config.get("gemini_api_key"):
            QMessageBox.warning(self, "提示", '请先点击"API 设置"填写 Gemini API Key')
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
