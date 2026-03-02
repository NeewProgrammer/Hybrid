import sys
import os

# 确保 src 目录在 path 中，方便导入模块
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from gui import MainWindow
from PySide6.QtWidgets import QApplication

if __name__ == "__main__":
    # Create QApplication instance
    app = QApplication.instance()
    if not app:
        app = QApplication(sys.argv)
        
    window = MainWindow()
    window.show()
    
    sys.exit(app.exec())
