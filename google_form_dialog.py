from PyQt5.QtWidgets import QDialog, QMessageBox
from PyQt5 import uic
from logger import log
import export
import webbrowser

class GoogleFormDialog(QDialog):
    def __init__(self, settings, activity_type, difficulty, parent=None):
        super().__init__(parent)
        log("DEBUG", "Entering GoogleFormDialog")
        uic.loadUi("google_form.ui", self)
        self.settings = settings  # Full settings dict from GUI
        self.activity_type = activity_type
        self.difficulty = difficulty
        self.form_id = None

        # Connect signals
        self.expressRadio.toggled.connect(self.toggle_inputs)
        self.customRadio.toggled.connect(self.toggle_inputs)
        self.continueBtn.clicked.connect(self.add_to_queue)

        # Set default values for Express mode
        self.titleInput.setText(f"{self.activity_type} Quiz")
        self.descInput.setPlainText(f"Generated for {self.difficulty} difficulty")
        self.toggle_inputs()

        log("DEBUG", "GoogleFormDialog initialized")

    def toggle_inputs(self):
        """Enable/disable input fields based on Express/Custom mode."""
        is_custom = self.customRadio.isChecked()
        self.titleLabel.setEnabled(is_custom)
        self.titleInput.setEnabled(is_custom)
        self.descLabel.setEnabled(is_custom)
        self.descInput.setEnabled(is_custom)
        log("DEBUG", f"Input fields {'enabled' if is_custom else 'disabled'} for {'Custom' if is_custom else 'Express'} mode")

    def add_to_queue(self):
        """Add task to parent's queue."""
        log("DEBUG", "Adding task to queue")
        try:
            if self.expressRadio.isChecked():
                title = f"{self.activity_type} Quiz"
                description = f"Generated for {self.difficulty} difficulty"
            else:
                title = self.titleInput.text().strip()
                description = self.descInput.toPlainText().strip()

            if not title:
                QMessageBox.warning(self, "Invalid Input", "Form title cannot be empty!")
                return

            task = {
                "settings": self.settings,
                "title": title,
                "description": description
            }

            self.parent().task_queue.append(task)
            self.parent().update_queue_table()  # Update queue table in parent
            log("INFO", f"Task added to queue: {title}")
            QMessageBox.information(self, "Queued", f"{title} added to queue! It will be processed soon.")
            self.accept()
        except Exception as e:
            log("ERROR", f"Failed to add task: {e}")
            QMessageBox.critical(self, "Error", f"Failed to add task: {e}")