import requests
import json
import re
from logger import log

OLLAMA_API_URL = "http://localhost:11434/api/generate"

# Load generator model from config.json (fallback to default)
try:
    import utils
    config = utils.load_json("config.json")
    GENERATOR_MODEL = config.get("generator_model", "gpt-oss:20b")
except Exception as e:
    GENERATOR_MODEL = "gpt-oss:20b"
    log("ERROR", f"Failed to load config.json, using default generator: {GENERATOR_MODEL}")

# ----------------------------
# Helpers
# ----------------------------

def is_valid_question(q):
    """Validate question dict depending on type."""
    if not isinstance(q, dict):
        return False
    if not q.get("question"):
        return False
    qtype = (q.get("type") or "").lower()
    if qtype in ["mcq", "multiple_choice", "multiple-choice"]:
        return isinstance(q.get("options"), list) and len(q["options"]) >= 2 and "answer" in q and q["answer"] != ""
    elif qtype in ["true/false", "true_false", "tf"]:
        return "answer" in q and q["answer"] in ["Correct", "Wrong"]
    else:
        return "answer" in q and q["answer"] != ""

def normalize_question(q, allowed_types=None, gui_difficulty=None):
    """Normalize type names, provide defaults, enforce GUI difficulty."""
    if not isinstance(q, dict):
        return None
    q_type = q.get("type", "").lower()
    if q_type in ["multiple_choice", "multiple-choice", "mcq"]:
        q["type"] = "MCQ"
    elif q_type in ["true/false", "true_false", "tf"]:
        q["type"] = "True/False"
    elif q_type in ["short", "short answer", "sa"]:
        q["type"] = "Short Answer"
    elif q_type in ["fill", "fill-in-the-blank", "fib"]:
        q["type"] = "Fill-in-the-Blank"
    elif q_type in ["num", "numerical", "numeric"]:
        q["type"] = "Numerical"
    else:
        q["type"] = "Short Answer"  # Default for unrecognized types
    if allowed_types and q["type"] not in allowed_types:
        return None
    if q["type"] == "MCQ":
        opts = q.get("options", [])
        opts = [str(o) for o in opts]
        answer = str(q.get("answer"))
        if answer not in opts:
            opts = opts[:3] + [answer] if len(opts) >= 3 else opts + [answer]
        while len(opts) < 4:
            opts.append(f"Option {len(opts)+1}")
        q["options"] = opts[:4]
    elif q["type"] == "True/False":
        q["options"] = ["Correct", "Wrong"]
        ans = str(q.get("answer")).strip().lower()
        q["answer"] = "Correct" if ans in ["true", "t", "correct"] else "Wrong"
    else:
        q["answer"] = str(q.get("answer"))
    if gui_difficulty:
        q["difficulty"] = gui_difficulty
    else:
        q["difficulty"] = "Medium"  # Default difficulty
    q["topic"] = q.get("topic", "")
    q["subtopic"] = q.get("subtopic", "")
    return q

def extract_json(raw_text):
    """Attempt to robustly extract JSON from model output."""
    if not isinstance(raw_text, str) or not raw_text.strip():
        log("WARNING", "Model output is empty or not a string")
        return None

    raw_text = raw_text.strip()
    log("DEBUG", f"Raw model output: {raw_text[:200]}...")

    # Handle streaming responses wrapped in {"response": "..."}
    try:
        api_data = json.loads(raw_text)
        if isinstance(api_data, dict) and "response" in api_data:
            raw_text = api_data["response"]
            log("DEBUG", f"Extracted 'response' field: {raw_text[:200]}...")
    except json.JSONDecodeError:
        log("DEBUG", "Raw text is not a JSON object, treating as direct output")

    if not raw_text.strip():
        log("WARNING", "Extracted response is empty")
        return None

    # Strip markdown fences
    raw_text = raw_text.strip()
    if raw_text.startswith("```json"):
        raw_text = raw_text[7:]
    if raw_text.endswith("```"):
        raw_text = raw_text[:-3]
    raw_text = raw_text.strip()

    # Try direct JSON first
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        log("DEBUG", "Direct JSON parsing failed, attempting regex extraction")

    # Regex fallback: extract first {...} or [...] block
    match = re.search(r'(\{[\s\S]*\}|\[[\s\S]*\])', raw_text)
    if match:
        candidate = match.group(1)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            try:
                fixed = candidate.replace("'", '"').replace("None", "null")
                return json.loads(fixed)
            except json.JSONDecodeError as e:
                log("WARNING", f"Failed to parse JSON candidate: {e}")

    log("WARNING", "All JSON extraction attempts failed")
    return None


def run_ollama(model, prompt):
    """Run a prompt through the Ollama API and return the response."""
    try:
        response = requests.post(OLLAMA_API_URL, json={"model": model, "prompt": prompt}, timeout=30)
        response.raise_for_status()
        raw_response = response.text
        log("DEBUG", f"Ollama API raw response: {raw_response[:200]}...")  # Log first 200 chars
        # Handle streaming or multi-line responses
        try:
            lines = raw_response.strip().split("\n")
            full_response = ""
            for line in lines:
                try:
                    data = json.loads(line)
                    if "response" in data:
                        full_response += data["response"]
                except json.JSONDecodeError:
                    continue
            return full_response or raw_response
        except Exception as e:
            log("DEBUG", f"Failed to parse streaming response: {e}, returning raw response")
            return raw_response
    except requests.RequestException as e:
        log("ERROR", f"Ollama API call failed for model {model}: {e}")
        return None

def generate_full_quiz(title, grade, difficulty, topics, question_type_counts, num_questions, activity_type, model, max_retries=3):
    """Generate a full quiz with retries on failure."""
    prompt = f"""
    Generate a quiz in JSON format with the following details:
    - Title: '{title}'
    - Grade: {grade}
    - Number of questions: {num_questions}
    - Difficulty: {difficulty}
    - Activity type: {activity_type}
    - Topics: {json.dumps(topics, indent=2)}
    - Question types: {json.dumps(question_type_counts, indent=2)}
    Return a JSON object with:
    - 'title': string
    - 'settings': object with 'topics', 'difficulty', 'grade', 'activity_type'
    - 'questions': list of question objects, each with 'question', 'type', 'answer', and optionally 'options', 'topic', 'subtopic'
    Ensure the output is valid JSON and contains only the requested quiz structure.
    """
    for attempt in range(max_retries):
        log("INFO", f"Attempting quiz generation (attempt {attempt + 1}/{max_retries})")
        raw = run_ollama(model, prompt)
        if raw is None:
            log("WARNING", "No response from Ollama API")
            continue
        parsed = extract_json(raw)
        if parsed and isinstance(parsed, dict) and "questions" in parsed:
            log("INFO", "Quiz generated successfully")
            return parsed
        log("WARNING", f"Invalid quiz response on attempt {attempt + 1}")
    log("ERROR", "Failed to generate quiz after all retries")
    return None

# ----------------------------
# Main Function
# ----------------------------

def generate_questions_simple(topics, num_questions, difficulty, question_type_counts, activity_type, title="Generated Quiz", grade="7", model=None):
    """Generate a quiz with specified parameters."""
    generator_model = model or GENERATOR_MODEL
    log("INFO", f"Starting question generation: num_questions={num_questions}, difficulty={difficulty}, question_types={question_type_counts}")
    quiz = generate_full_quiz(title, grade, difficulty, topics, question_type_counts, num_questions, activity_type, generator_model)
    if not quiz or "questions" not in quiz:
        log("ERROR", "Failed to generate quiz")
        return None
    questions = quiz["questions"]
    allowed_types = ["MCQ", "True/False", "Short Answer", "Fill-in-the-Blank", "Numerical"]
    normalized_questions = []
    for q in questions:
        nq = normalize_question(q, allowed_types=allowed_types, gui_difficulty=difficulty)
        if nq and is_valid_question(nq):
            normalized_questions.append(nq)
        else:
            log("WARNING", f"Dropping invalid question: {q.get('question', '')[:80]}")
    quiz["questions"] = normalized_questions[:num_questions]
    log("INFO", f"Generated quiz contains {len(quiz['questions'])} questions")
    return quiz

if __name__ == "__main__":
    SAMPLE_TOPICS = {
        "Chapter 1: Factors": {
            "subtopics": ["1.1 Divisibility tests"]
        }
    }
    q_types = {"MCQ": 2, "Short Answer": 2}
    quiz_obj = generate_questions_simple(SAMPLE_TOPICS, num_questions=4, difficulty="Easy", question_type_counts=q_types, activity_type="Class Activity", title="Factors Quiz", grade="7")
    if quiz_obj:
        print(json.dumps(quiz_obj, indent=2, ensure_ascii=False))
    else:
        print("Quiz generation failed.")