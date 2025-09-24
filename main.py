import sys
from PyQt5.QtWidgets import QApplication
from gui import QuizGeneratorGUI
from logger import log

def main():
    log("INFO", "Starting application...")
    app = QApplication(sys.argv)
    window = QuizGeneratorGUI()
    window.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()