from PyQt5.QtWidgets import (
    QMainWindow, QFileDialog, QMessageBox, QTableWidgetItem, QDialog,
    QVBoxLayout, QLineEdit, QComboBox, QPushButton, QLabel, QTreeWidgetItem
)
from PyQt5.QtCore import Qt, QThread, pyqtSignal
from PyQt5 import uic
import utils
import ai_pipeline
import export
import webbrowser
import json
import os
import os.path
from datetime import datetime
from logger import log
from google_form_dialog import GoogleFormDialog
from google_auth import GoogleFormsClient

class QuestionEditDialog(QDialog):
    def __init__(self, question, parent=None):
        super().__init__(parent)
        log("DEBUG", "Entering QuestionEditDialog")
        self.setWindowTitle("Edit Question")
        self.layout = QVBoxLayout(self)
        self.question_input = QLineEdit(question.get("question", ""))
        self.type_combo = QComboBox()
        self.type_combo.addItems(["MCQ", "Short Answer", "True/False", "Fill-in-the-Blank", "Numerical"])
        self.type_combo.setCurrentText(question.get("type", "MCQ"))
        self.options_input = QLineEdit(",".join(question.get("options", [])))
        self.answer_input = QLineEdit(question.get("answer", ""))
        self.save_btn = QPushButton("Save")
        self.layout.addWidget(QLabel("Question:"))
        self.layout.addWidget(self.question_input)
        self.layout.addWidget(QLabel("Type:"))
        self.layout.addWidget(self.type_combo)
        self.layout.addWidget(QLabel("Options (comma-separated, for MCQ/TF):"))
        self.layout.addWidget(self.options_input)
        self.layout.addWidget(QLabel("Answer:"))
        self.layout.addWidget(self.answer_input)
        self.layout.addWidget(self.save_btn)
        self.save_btn.clicked.connect(self.accept)
        log("DEBUG", "QuestionEditDialog initialized")

    def get_question(self):
        log("DEBUG", "Getting question from dialog")
        return {
            "question": self.question_input.text(),
            "type": self.type_combo.currentText(),
            "options": self.options_input.text().split(",") if self.options_input.text() and self.type_combo.currentText() in ["MCQ", "True/False"] else [],
            "answer": self.answer_input.text(),
            "topic": "",
            "subtopic": "",
            "difficulty": ""
        }

class GenerationThread(QThread):
    generation_complete = pyqtSignal(list, dict)  # Emit questions list + task
    error_occurred = pyqtSignal(str)

    def __init__(self, topics, num_questions, difficulty, question_types, activity_type, task):
        super().__init__()
        log("DEBUG", f"GenerationThread __init__ called with topics={topics}, num_questions={num_questions}")
        self.topics = topics
        self.num_questions = num_questions
        self.difficulty = difficulty
        self.question_types = question_types
        self.activity_type = activity_type
        self.task = task

    def run(self):
        log("DEBUG", "GenerationThread.run() started")
        try:
            quiz = ai_pipeline.generate_questions_simple(
                self.topics,
                self.num_questions,
                self.difficulty,
                self.question_types,
                self.activity_type,
                title=self.task.get("title", "Generated Quiz"),
                grade="7"
            )
            if quiz and "questions" in quiz:
                log("DEBUG", f"GenerationThread.run() completed with {len(quiz['questions'])} questions")
                self.generation_complete.emit(quiz["questions"], self.task)
            else:
                raise Exception("Quiz generation returned no questions")
        except Exception as e:
            log("ERROR", f"Error in GenerationThread.run(): {e}")
            import traceback
            traceback.print_exc()
            self.error_occurred.emit(str(e))

class QuizGeneratorGUI(QMainWindow):
    def __init__(self):
        super().__init__()
        log("INFO", "QuizGeneratorGUI __init__ started")
        uic.loadUi("gui.ui", self)
        self.task_queue = []
        self.refined_questions = []
        self.google_client = GoogleFormsClient()
        self.form_id = None
        self.current_thread = None
        self.activities_dir = "Activities"
        if not os.path.exists(self.activities_dir):
            os.makedirs(self.activities_dir)
            log("INFO", f"Created Activities folder: {self.activities_dir}")
        try:
            topics_data = utils.load_json("topics.json")
            grades = list(topics_data.keys())
            if not grades:
                raise ValueError("No grades found in topics.json")
            self.gradeCombo.addItems(grades)
            log("DEBUG", f"Populated gradeCombo with {grades}")
        except Exception as e:
            log("ERROR", f"Failed to load topics.json for gradeCombo: {e}")
            QMessageBox.critical(self, "Error", f"Failed to load grades from topics.json: {e}")
            self.gradeCombo.addItem("No Grades Available")
        self.gradeCombo.currentTextChanged.connect(self.load_topics)
        self.addToQueueBtn.clicked.connect(self.add_to_queue)
        self.startGenerationBtn.clicked.connect(self.start_queue_processing)
        self.saveSettingsBtn.clicked.connect(self.save_settings)
        self.loadSettingsBtn.clicked.connect(self.load_settings)
        self.importQuestionsBtn.clicked.connect(self.import_questions)
        self.questionTable.cellClicked.connect(self.handle_table_click)
        self.exportActivitiesBtn.clicked.connect(self.export_activities)
        if self.gradeCombo.count() > 0 and self.gradeCombo.currentText() != "No Grades Available":
            self.load_topics(self.gradeCombo.currentText())
        else:
            log("WARNING", "No valid grades to load topics")
            QMessageBox.warning(self, "Warning", "No valid grades found in topics.json.")
        log("INFO", "QuizGeneratorGUI initialized")

    def load_topics(self, grade):
        log("DEBUG", f"Loading topics for grade: {grade}")
        self.topicTree.clear()
        if not grade or grade == "No Grades Available":
            log("WARNING", "Invalid or empty grade selected")
            QMessageBox.warning(self, "Warning", "Invalid or empty grade selected.")
            return
        try:
            topics_data = utils.load_json("topics.json")
            grade_data = topics_data.get(grade, {})
            if not grade_data:
                log("WARNING", f"No topics found for grade: {grade}")
                QMessageBox.warning(self, "Warning", f"No topics found for grade {grade}")
                return
            for main_topic, topic_info in grade_data.items():
                subtopics = topic_info.get("subtopics", []) if isinstance(topic_info, dict) else topic_info
                if not isinstance(subtopics, list):
                    log("WARNING", f"Invalid subtopics format for topic {main_topic}: {subtopics}")
                    subtopics = []
                parent = QTreeWidgetItem(self.topicTree, [main_topic])
                parent.setFlags(parent.flags() | Qt.ItemIsUserCheckable)
                parent.setCheckState(0, Qt.Unchecked)
                for subtopic in subtopics:
                    child = QTreeWidgetItem(parent, [subtopic])
                    child.setFlags(child.flags() | Qt.ItemIsUserCheckable)
                    child.setCheckState(0, Qt.Unchecked)
                if not subtopics:
                    log("WARNING", f"No subtopics found for topic {main_topic}")
            self.topicTree.expandAll()
            log("DEBUG", f"Topics loaded for grade {grade}: {list(grade_data.keys())}")
        except Exception as e:
            log("ERROR", f"Failed to load topics for grade {grade}: {e}")
            QMessageBox.critical(self, "Error", f"Failed to load topics: {e}")

    def get_selected_topics(self):
        topics = {}
        for i in range(self.topicTree.topLevelItemCount()):
            parent = self.topicTree.topLevelItem(i)
            main_topic = parent.text(0)
            subtopics = []
            for j in range(parent.childCount()):
                child = parent.child(j)
                if child.checkState(0) == Qt.Checked:
                    subtopics.append(child.text(0))
            if subtopics or parent.checkState(0) == Qt.Checked:
                topics[main_topic] = {"subtopics": subtopics}
        log("DEBUG", f"Selected topics: {topics}")
        return topics

    def get_question_types(self):
        qtypes = {}
        if self.qtypeMCQ.isChecked():
            qtypes["MCQ"] = self.qtypeMCQSpin.value()
        if self.qtypeShort.isChecked():
            qtypes["Short Answer"] = self.qtypeShortSpin.value()
        if self.qtypeTF.isChecked():
            qtypes["True/False"] = self.qtypeTFSpin.value()
        if self.qtypeFill.isChecked():
            qtypes["Fill-in-the-Blank"] = self.qtypeFillSpin.value()
        if self.qtypeNum.isChecked():
            qtypes["Numerical"] = self.qtypeNumSpin.value()
        return qtypes

    def add_to_queue(self):
        log("DEBUG", "Adding task to queue")
        topics = self.get_selected_topics()
        if not topics:
            QMessageBox.warning(self, "Invalid Input", "Please select at least one topic!")
            return
        num_questions = sum(self.get_question_types().values())
        if num_questions < 1:
            QMessageBox.warning(self, "Invalid Input", "Please select at least one question type with a non-zero count!")
            return
        dialog = GoogleFormDialog(
            {
                "topics": topics,
                "question_types": self.get_question_types(),
                "export_format": self.exportFormatCombo.currentText(),
                "difficulty": self.difficultyCombo.currentText(),
                "activity_type": self.activityCombo.currentText(),
                "grade": self.gradeCombo.currentText()
            },
            self.activityCombo.currentText(),
            self.difficultyCombo.currentText(),
            self
        )
        dialog.exec_()

    def start_queue_processing(self):
        if not self.task_queue:
            QMessageBox.warning(self, "Empty Queue", "No tasks in queue to process!")
            return
        if self.current_thread and self.current_thread.isRunning():
            QMessageBox.warning(self, "Processing", "A task is already being processed!")
            return
        task = self.task_queue[0]
        settings = task["settings"]
        self.current_thread = GenerationThread(
            settings["topics"],
            sum(settings["question_types"].values()),
            settings["difficulty"],
            settings["question_types"],
            settings["activity_type"],
            task
        )
        self.current_thread.generation_complete.connect(self.on_generation_complete)
        self.current_thread.error_occurred.connect(self.on_generation_error)
        self.current_thread.start()
        self.queueStatusLabel.setText("Status: Processing...")
        log("INFO", "Started queue processing")

    def on_generation_complete(self, questions, task):
        log("INFO", f"Generation complete with {len(questions)} questions")
        self.refined_questions = questions
        self.current_thread = None
        self.update_question_table()
        try:
            utils.save_json(os.path.join(self.activities_dir, f"quiz_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"), {
                "title": task["title"],
                "description": task["description"],
                "questions": questions
            })
            log("INFO", f"Saved questions to Activities folder")
        except Exception as e:
            log("ERROR", f"Failed to save questions: {e}")
        export_format = task["settings"]["export_format"]
        if export_format == "Google Forms":
            form_id = export.create_google_form(task["title"], task["description"], questions)
            if form_id:
                self.form_id = form_id
                form_url = f"https://docs.google.com/forms/d/{form_id}/edit"
                webbrowser.open(form_url)
                log("INFO", f"Google Form created and opened: {form_url}")
            else:
                log("ERROR", "Failed to create Google Form")
                QMessageBox.critical(self, "Error", "Failed to create Google Form")
        elif export_format == "Kahoot":
            export.convert_to_kahoot_excel(questions)
        elif export_format == "PDF":
            export.export_to_pdf(questions)
        elif export_format == "JSON":
            utils.save_json(f"quiz_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json", questions)
        self.task_queue.pop(0)
        self.update_queue_table()
        self.process_next_task()

    def process_next_task(self):
        if self.task_queue and not self.current_thread:
            self.start_queue_processing()
        else:
            self.queueStatusLabel.setText("Status: Idle")

    def on_generation_error(self, error):
        log("ERROR", f"Generation error: {error}")
        self.task_queue.pop(0)
        self.current_thread = None
        self.update_queue_table()
        self.queueStatusLabel.setText("Status: Idle")
        QMessageBox.critical(self, "Error", f"Generation failed: {error}")

    def export_activities(self):
        log("INFO", "Starting batch export of activities")
        try:
            exported_forms = export.export_activities_from_folder(self.activities_dir)
            if exported_forms:
                QMessageBox.information(self, "Success", f"Exported {len(exported_forms)} Google Forms from Activities folder!")
                for form_id in exported_forms:
                    form_url = f"https://docs.google.com/forms/d/{form_id}/edit"
                    webbrowser.open(form_url)
                    log("INFO", f"Opened Google Form: {form_url}")
            else:
                QMessageBox.warning(self, "Warning", "No activities found in Activities folder or export failed!")
        except Exception as e:
            log("ERROR", f"Failed to export activities: {e}")
            QMessageBox.critical(self, "Error", f"Failed to export activities: {e}")

    def update_queue_table(self):
        log("DEBUG", "Updating queue table")
        self.queueTable.setRowCount(len(self.task_queue))
        for row, task in enumerate(self.task_queue):
            self.queueTable.setItem(row, 0, QTableWidgetItem(task["title"]))
            self.queueTable.setItem(row, 1, QTableWidgetItem("Pending"))
            details = f"Questions: {sum(task['settings']['question_types'].values())}, Format: {task['settings']['export_format']}"
            self.queueTable.setItem(row, 2, QTableWidgetItem(details))

    def update_question_table(self):
        log("DEBUG", "Updating question table")
        self.questionTable.setRowCount(len(self.refined_questions))
        self.questionTable.setHorizontalHeaderLabels(["Topic", "Subtopic", "Question", "Type", "Answer", "Actions"])
        for row, q in enumerate(self.refined_questions):
            self.questionTable.setItem(row, 0, QTableWidgetItem(q.get("topic", "")))
            self.questionTable.setItem(row, 1, QTableWidgetItem(q.get("subtopic", "")))
            self.questionTable.setItem(row, 2, QTableWidgetItem(q.get("question", "")))
            self.questionTable.setItem(row, 3, QTableWidgetItem(q.get("type", "")))
            self.questionTable.setItem(row, 4, QTableWidgetItem(q.get("answer", "")))
            self.questionTable.setItem(row, 5, QTableWidgetItem("Edit"))

    def save_settings(self):
        log("DEBUG", "Saving settings")
        settings = {
            "grade": self.gradeCombo.currentText(),
            "topics": self.get_selected_topics(),
            "difficulty": self.difficultyCombo.currentText(),
            "activity_type": self.activityCombo.currentText(),
            "export_format": self.exportFormatCombo.currentText(),
            "question_types": self.get_question_types()
        }
        try:
            utils.save_json("settings.json", settings)
            QMessageBox.information(self, "Success", "Settings saved successfully!")
        except Exception as e:
            log("ERROR", f"Failed to save settings: {e}")
            QMessageBox.critical(self, "Error", f"Failed to save settings: {e}")

    def load_settings(self):
        log("DEBUG", "Loading settings")
        try:
            settings = utils.load_json("settings.json")
            self.load_settings_from_dict(settings)
            QMessageBox.information(self, "Success", "Settings loaded successfully!")
        except Exception as e:
            log("ERROR", f"Failed to load settings: {e}")
            QMessageBox.critical(self, "Error", f"Failed to load settings: {e}")

    def import_questions(self):
        log("DEBUG", "Importing questions")
        file_name, _ = QFileDialog.getOpenFileName(self, "Import Questions", "", "JSON Files (*.json)")
        if not file_name:
            return
        try:
            self.refined_questions = utils.load_json(file_name)
            self.update_question_table()
            utils.save_json("questions.json", self.refined_questions)
            log("INFO", "Questions imported and saved to questions.json")
        except Exception as e:
            log("ERROR", f"Failed to import questions: {e}")
            QMessageBox.critical(self, "Error", f"Failed to import questions: {e}")

    def handle_table_click(self, row, column):
        log("DEBUG", f"Table clicked: row={row}, column={column}")
        if column == 5:
            question = self.refined_questions[row]
            dialog = QuestionEditDialog(question, self)
            if dialog.exec_():
                self.refined_questions[row] = dialog.get_question()
                self.update_question_table()
                try:
                    utils.save_json("questions.json", self.refined_questions)
                    log("INFO", "Updated questions saved to questions.json")
                except Exception as e:
                    log("ERROR", f"Failed to save updated questions: {e}")
                    QMessageBox.critical(self, "Error", f"Failed to save updated questions: {e}")

    def load_settings_from_dict(self, settings):
        log("DEBUG", f"Loading settings from dict: {settings}")
        self.gradeCombo.setCurrentText(settings.get("grade", ""))
        self.difficultyCombo.setCurrentText(settings.get("difficulty", ""))
        self.activityCombo.setCurrentText(settings.get("activity_type", ""))
        self.exportFormatCombo.setCurrentText(settings.get("export_format", ""))
        for i in range(self.topicTree.topLevelItemCount()):
            parent = self.topicTree.topLevelItem(i)
            main_topic = parent.text(0)
            if main_topic in settings.get("topics", {}):
                parent.setCheckState(0, Qt.Checked)
                subtopics = settings["topics"][main_topic]["subtopics"]
                for j in range(parent.childCount()):
                    child = parent.child(j)
                    if child.text(0) in subtopics:
                        child.setCheckState(0, Qt.Checked)
        qtypes = settings.get("question_types", {})
        self.qtypeMCQ.setChecked("MCQ" in qtypes)
        self.qtypeMCQSpin.setValue(qtypes.get("MCQ", 0))
        self.qtypeShort.setChecked("Short Answer" in qtypes)
        self.qtypeShortSpin.setValue(qtypes.get("Short Answer", 0))
        self.qtypeTF.setChecked("True/False" in qtypes)
        self.qtypeTFSpin.setValue(qtypes.get("True/False", 0))
        self.qtypeFill.setChecked("Fill-in-the-Blank" in qtypes)
        self.qtypeFillSpin.setValue(qtypes.get("Fill-in-the-Blank", 0))
        self.qtypeNum.setChecked("Numerical" in qtypes)
        self.qtypeNumSpin.setValue(qtypes.get("Numerical", 0))