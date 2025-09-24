from google_auth import GoogleFormsClient
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer
from reportlab.lib.styles import getSampleStyleSheet
import os
import json
from logger import log
from collections import OrderedDict

def create_google_form(title, description, questions):
    log("DEBUG", "[EXPORT] Starting Google Form creation")
    
    # Validate and sanitize title
    title = title.strip() if title else "Untitled Activity"
    if not title:
        log("WARNING", "[EXPORT] Title is empty after sanitization, using default 'Untitled Activity'")
        title = "Untitled Activity"
    log("DEBUG", f"[EXPORT] Using title: '{title}'")
    
    # Initialize GoogleFormsClient
    log("DEBUG", "[EXPORT] Initializing Google Forms client")
    try:
        google_client = GoogleFormsClient()
        service = google_client.get_service()
        if service is None:
            log("ERROR", "[EXPORT] Failed to initialize Google Forms service: Service is None")
            return None
    except Exception as e:
        log("ERROR", f"[EXPORT] Failed to initialize Google Forms service: {e}")
        return None
    log("DEBUG", "[EXPORT] Google Forms client initialized successfully")

    try:
        # Create form with title
        log("DEBUG", f"[EXPORT] Creating form with title: '{title}'")
        form_body = {"info": {"title": title}}
        form = service.forms().create(body=form_body).execute()
        form_id = form["formId"]
        log("DEBUG", f"[EXPORT] Form created with ID: {form_id}")

        # Explicitly update title to ensure it's set
        log("DEBUG", "[EXPORT] Explicitly updating form title")
        title_update_request = {
            "updateFormInfo": {
                "info": {"title": title},
                "updateMask": "title"
            }
        }
        service.forms().batchUpdate(formId=form_id, body={"requests": [title_update_request]}).execute()
        log("DEBUG", "[EXPORT] Form title updated successfully")

        # Update description and enable quiz mode
        log("DEBUG", "[EXPORT] Updating form description and quiz settings")
        update_requests = [
            {
                "updateFormInfo": {
                    "info": {"description": description},
                    "updateMask": "description"
                }
            },
            {
                "updateSettings": {
                    "settings": {
                        "quizSettings": {
                            "isQuiz": True
                        }
                    },
                    "updateMask": "quizSettings.isQuiz"
                }
            }
        ]
        service.forms().batchUpdate(formId=form_id, body={"requests": update_requests}).execute()
        log("DEBUG", "[EXPORT] Form description and quiz settings updated successfully")

        # Group questions by type, preserving the order of first appearance
        questions_by_type = OrderedDict()
        for q in questions:
            qtype = q["type"]
            if qtype not in questions_by_type:
                questions_by_type[qtype] = []
            questions_by_type[qtype].append(q)

        # Add questions organized into sections by type (batched)
        log("DEBUG", f"[EXPORT] Preparing to add {len(questions)} questions organized by type")
        create_requests = []
        index = 0
        for qtype, type_questions in questions_by_type.items():
            if not type_questions:
                continue

            # Add page break to start a new section
            create_requests.append({
                "createItem": {
                    "item": {"pageBreakItem": {}},
                    "location": {"index": index}
                }
            })
            index += 1
            log("DEBUG", f"[EXPORT] Added page break for {qtype} section")

            # Add section title as a textItem
            create_requests.append({
                "createItem": {
                    "item": {
                        "title": f"{qtype} Questions",
                        "textItem": {}
                    },
                    "location": {"index": index}
                }
            })
            index += 1
            log("DEBUG", f"[EXPORT] Added section title for {qtype}")

            # Add questions for this type
            for i, q in enumerate(type_questions):
                log("DEBUG", f"[EXPORT] Processing {qtype} question {i+1}: {q.get('question', '')[:50]}...")
                if q["type"] in ["MCQ", "True/False"]:
                    options = q.get("options", [])
                    if not options:
                        if q["type"] == "True/False":
                            options = ["True", "False"]  # Default for True/False
                            log("WARNING", f"[EXPORT] Added default options for True/False question {i+1}")
                        else:
                            log("WARNING", f"[EXPORT] Skipping MCQ question {i+1} due to missing options")
                            continue
                    options = [{"value": opt} for opt in options]
                    correct_answers = {"answers": [{"value": q["answer"]}]}
                    question = {
                        "required": True,
                        "choiceQuestion": {
                            "type": "RADIO",
                            "options": options,
                            "shuffle": q["type"] == "MCQ"
                        },
                        "grading": {
                            "pointValue": 1,
                            "correctAnswers": correct_answers
                        }
                    }
                    log("DEBUG", f"[EXPORT] Question {i+1} configured as {q['type']} with {len(options)} options")
                else:
                    question = {
                        "required": True,
                        "textQuestion": {"paragraph": False},
                        "grading": {
                            "pointValue": 1,
                            "correctAnswers": {"answers": [{"value": q["answer"]}]}
                        }
                    }
                    log("DEBUG", f"[EXPORT] Question {i+1} configured as {q['type']} (text-based)")
                
                item = {
                    "title": q["question"],
                    "questionItem": {"question": question}
                }
                create_requests.append({
                    "createItem": {
                        "item": item,
                        "location": {"index": index}
                    }
                })
                index += 1
        
        if create_requests:
            log("DEBUG", f"[EXPORT] Sending batch update with {len(create_requests)} items (sections + questions)")
            service.forms().batchUpdate(formId=form_id, body={"requests": create_requests}).execute()
            log("DEBUG", "[EXPORT] All sections and questions added successfully")
        else:
            log("WARNING", "[EXPORT] No questions to add to the form")
        
        log("INFO", f"[EXPORT] Google Form created: {form_id}")
        return form_id
    except Exception as e:
        log("ERROR", f"[EXPORT] Failed to create Google Form: {e}")
        return None

def export_activities_from_folder(activities_dir):
    log("DEBUG", f"[EXPORT] Starting batch export from Activities folder: {activities_dir}")
    if not os.path.exists(activities_dir):
        log("ERROR", f"[EXPORT] Activities folder {activities_dir} does not exist")
        return []
    
    exported_forms = []
    for filename in os.listdir(activities_dir):
        if filename.endswith(".json"):
            file_path = os.path.join(activities_dir, filename)
            log("DEBUG", f"[EXPORT] Processing activity file: {file_path}")
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    activity_data = json.load(f)
                title = activity_data.get("title", "Untitled Activity")
                description = activity_data.get("description", "")
                questions = activity_data.get("questions", [])
                log("DEBUG", f"[EXPORT] Loaded activity: '{title}' with {len(questions)} questions")
                form_id = create_google_form(title, description, questions)
                if form_id:
                    exported_forms.append(form_id)
                    log("INFO", f"[EXPORT] Successfully exported activity '{title}' as Google Form: {form_id}")
                else:
                    log("ERROR", f"[EXPORT] Failed to export activity '{title}'")
            except Exception as e:
                log("ERROR", f"[EXPORT] Failed to process activity file {file_path}: {e}")
    
    log("INFO", f"[EXPORT] Batch export complete: {len(exported_forms)} forms created")
    return exported_forms

def convert_to_kahoot_excel(questions):
    import pandas as pd
    log("DEBUG", "[EXPORT] Starting Kahoot Excel export")
    data = []
    for q in questions:
        row = {
            "Question": q["question"],
            "Answer": q["answer"],
            "Time Limit (sec)": 30
        }
        if q["type"] == "MCQ" or q["type"] == "True/False":
            for i, opt in enumerate(q.get("options", [])[:4], 1):
                row[f"Answer {i}"] = opt
        data.append(row)
    df = pd.DataFrame(data)
    file_name = "kahoot_export.xlsx"
    df.to_excel(file_name, index=False)
    log("INFO", f"[EXPORT] Kahoot Excel created: {file_name}")
    return file_name

def export_to_pdf(questions):
    log("DEBUG", "[EXPORT] Starting PDF export")
    file_name = "quiz_export.pdf"
    doc = SimpleDocTemplate(file_name, pagesize=letter)
    styles = getSampleStyleSheet()
    story = []
    for i, q in enumerate(questions, 1):
        story.append(Paragraph(f"<b>Question {i}: {q['question']}</b>", styles["Heading2"]))
        if q.get("options"):
            for j, opt in enumerate(q["options"], 1):
                story.append(Paragraph(f"{j}. {opt}", styles["BodyText"]))
        story.append(Paragraph(f"Answer: {q['answer']}", styles["BodyText"]))
        story.append(Spacer(1, 12))
    doc.build(story)
    log("INFO", f"[EXPORT] PDF created: {file_name}")
    return file_name