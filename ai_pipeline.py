import requests
import json
import re
import random
from logger import log

OLLAMA_API_URL = "http://localhost:11434/api/generate"

# Load models from config.json (fallback to defaults)
try:
    import utils
    config = utils.load_json("config.json")
    GENERATOR_MODEL = config.get("generator_model", "gpt-oss:20b")
    CRITIC_MODEL = config.get("critic_model", "deepseek-r1:14b")
except Exception as e:
    GENERATOR_MODEL = "gpt-oss:20b"
    CRITIC_MODEL = "deepseek-r1:14b"
    log("ERROR", f"Failed to load config.json, using defaults: generator={GENERATOR_MODEL}, critic={CRITIC_MODEL}")

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

def analyze_question_difficulty(question):
    """Estimate difficulty based on numbers and simple heuristics."""
    text = (question.get("question", "") + " " + str(question.get("answer", ""))).lower()
    nums = [float(n) for n in re.findall(r'\d+\.?\d*', text) if n]
    steps = 1
    if any(op in text for op in ["+", "-", "*", "/", "average", "total", "difference", "sum", "product", "divide", "multiply"]):
        steps += text.count(" and ") + text.count(",")
    max_num = max(nums) if nums else 0
    if max_num < 20 and steps <= 1:
        return "Easy"
    elif max_num < 100 and steps <= 2:
        return "Medium"
    elif max_num < 1000 and steps > 2:
        return "Hard"
    return "Medium"

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
        estimated = analyze_question_difficulty(q)
        if estimated != gui_difficulty:
            log("WARNING", f"Difficulty mismatch for question '{q.get('question','')[:60]}': estimated {estimated}, GUI {gui_difficulty}. Using GUI difficulty.")
        q["difficulty"] = gui_difficulty
    else:
        q["difficulty"] = analyze_question_difficulty(q)
    q["topic"] = q.get("topic", "")
    q["subtopic"] = q.get("subtopic", "")
    return q

def extract_json(raw_text):
    """Attempt to robustly extract JSON from model output."""
    if not isinstance(raw_text, str):
        return raw_text
    raw_text = raw_text.strip()
    if raw_text.startswith("```json"):
        raw_text = raw_text[7:].rstrip("```").strip()
    elif raw_text.startswith("```"):
        raw_text = raw_text[3:].rstrip("```").strip()
    try:
        return json.loads(raw_text)
    except Exception:
        match = re.search(r'(\{[\s\S]*\}|\[[\s\S]*\])', raw_text)
        if match:
            candidate = match.group(1)
            try:
                return json.loads(candidate)
            except Exception:
                try:
                    fixed = candidate.replace("'", '"')
                    return json.loads(fixed)
                except Exception:
                    log("WARNING", "Failed to parse JSON from model output.")
    return None

def deduplicate_questions(questions):
    """Remove duplicate questions by question text (case-insensitive)."""
    seen = set()
    unique = []
    for q in questions:
        text = str(q.get("question", "")).strip().lower()
        if text and text not in seen:
            seen.add(text)
            unique.append(q)
    return unique

# ----------------------------
# Ollama call
# ----------------------------
def run_ollama(model, prompt):
    """Call Ollama local API and return concatenated text response."""
    log("DEBUG", f"Calling Ollama model={model}, prompt length={len(prompt) if prompt else 0}")
    try:
        response = requests.post(
            OLLAMA_API_URL,
            json={"model": model, "prompt": prompt},
            stream=True,
            timeout=60
        )
        response.raise_for_status()
        full_text = ""
        for line in response.iter_lines():
            if not line:
                continue
            try:
                obj = json.loads(line.decode("utf-8"))
                if isinstance(obj, dict) and "response" in obj:
                    full_text += obj["response"]
            except Exception as e:
                log("WARNING", f"Failed to parse Ollama stream line: {e}")
        return full_text
    except Exception as e:
        log("ERROR", f"Ollama call failed: {e}")
        return ""

# ----------------------------
# Generator
# ----------------------------
def generate_full_quiz(title, grade, difficulty, topics, question_type_counts, num_questions, activity_type, model=GENERATOR_MODEL):
    """Generate full quiz JSON in one Ollama call."""
    log("INFO", f"Generating full quiz: title={title}, grade={grade}, difficulty={difficulty}, topics={topics}, types={question_type_counts}")
    topic_str = '\n'.join([f"{topic}: {subtopic}" for topic in topics for subtopic in topics[topic]["subtopics"]])
    type_sections = ''
    if question_type_counts.get("MCQ", 0) > 0:
        type_sections += (
            f'Multiple Choice Table ({question_type_counts.get("MCQ", 0)} questions)\n'
            'Format: 6 columns → Question | A) | B) | C) | D) | Correct Answer\n'
            'Show the actual correct answer (e.g., 14, 3x + 5), not the letter.\n'
            'Randomize the position of the correct answer.\n'
            'Ensure even distribution: across the questions, there should be roughly the same number of A, B, C, D as correct answers.\n'
            'Avoid repetition and ambiguity.\n\n'
        )
    if question_type_counts.get("True/False", 0) > 0:
        type_sections += (
            f'True/False Table ({question_type_counts.get("True/False", 0)} questions)\n'
            'Format: 3 columns → Question | Correct/Wrong | Correct Answer\n'
            'Do not use “True/False”. Instead use Correct/Wrong.\n'
            'Ensure logical accuracy and clarity.\n\n'
        )
    if question_type_counts.get("Short Answer", 0) > 0:
        type_sections += (
            f'Short Answer Table ({question_type_counts.get("Short Answer", 0)} questions)\n'
            'Format: 2 columns → Question | Expected Answer\n'
            'Keep questions clear, aligned with the given topics.\n'
            'Write only the expected answer (no explanation).\n\n'
        )
    if question_type_counts.get("Fill-in-the-Blank", 0) > 0:
        type_sections += (
            f'Fill-in-the-Blank Table ({question_type_counts.get("Fill-in-the-Blank", 0)} questions)\n'
            'Format: 2 columns → Question | Expected Answer\n'
            'Keep questions clear, aligned with the given topics.\n'
            'Write only the expected answer (no explanation).\n\n'
        )
    if question_type_counts.get("Numerical", 0) > 0:
        type_sections += (
            f'Numerical Table ({question_type_counts.get("Numerical", 0)} questions)\n'
            'Format: 2 columns → Question | Expected Answer\n'
            'Keep questions clear, aligned with the given topics.\n'
            'Write only the expected answer (no explanation).\n\n'
        )
    settings_topics = [[topic, subtopic] for topic in topics for subtopic in topics[topic]["subtopics"]]
    prompt = (
        f'I need a quiz with {num_questions} questions total based on the following topics:\n[{topic_str}]\n\n'
        f'Generate the questions in the following structures:\n{type_sections}\n'
        f'General Rules:\n'
        f'Total = {num_questions} questions.\n'
        f'Questions should reflect the supplied topics closely (use textbook style).\n'
        f'Keep language simple but precise.\n'
        f'Check spelling, grammar, and math logic before finalizing.\n'
        f'Do not number the questions in the table (only text of the question).\n'
        f'Use word problems relevant to Thailand where learners should extract information.\n'
        f'Return results in .json format like, {{\n'
        f'  "title": "{title}",\n'
        f'  "description": "Generated for {difficulty} difficulty",\n'
        f'  "settings": {{\n'
        f'    "grade": "{grade}",\n'
        f'    "topics": {json.dumps(settings_topics)},\n'
        f'    "difficulty": "{difficulty}",\n'
        f'    "activity_type": "{activity_type}",\n'
        f'    "question_types": {json.dumps(question_type_counts)}\n'
        f'  }},\n'
        f'  "questions": [\n'
        f'    # list of question objects, each with: "question", "type" (e.g. "MCQ", "True/False", "Short Answer", "Fill-in-the-Blank", "Numerical"), "options" (list for MCQ and True/False), "answer" (string), "topic", "subtopic", "difficulty"\n'
        f'  ]\n'
        f'}}\n'
        f'Return only the JSON object.'
    )
    attempt = 0
    max_attempts = 3
    while attempt < max_attempts:
        raw = run_ollama(model, prompt)
        parsed = extract_json(raw)
        if isinstance(parsed, dict) and "questions" in parsed and len(parsed["questions"]) > 0:
            log("INFO", f"Generated quiz with {len(parsed['questions'])} questions on attempt {attempt+1}")
            return parsed
        attempt += 1
        log("WARNING", f"Generation attempt {attempt} failed; retrying...")
    log("ERROR", "Failed to generate valid quiz after max attempts.")
    return None

# ----------------------------
# Critic
# ----------------------------
def batch_critique(questions, topic_hint=None, difficulty="Medium"):
    """Batch critique for systemic issues."""
    prompt = (
        f'Review this set of {len(questions)} math questions. Identify any systemic issues, such as errors in solutions, '
        f'unclear wording, inconsistent difficulty, or lack of topic variety. Provide a summary of patterns and flag any '
        f'questions that need detailed review.\n\n'
        f'Questions:\n{json.dumps(questions, indent=2)}\n\n'
        f'Output JSON:\n{{"summary": "brief summary of issues", "flagged": [list of question indices (0-based) that need deeper review]}}'
    )
    raw = run_ollama(CRITIC_MODEL, prompt)
    parsed = extract_json(raw)
    if isinstance(parsed, dict) and "flagged" in parsed:
        return parsed
    log("WARNING", "Batch critique failed to return valid JSON.")
    return {"summary": "Critique failed", "flagged": []}

def critique_questions(questions):
    """Detailed critique for a list of questions."""
    prompt = (
        f'You are a math teacher critiquing questions for Grade 7/8 Thai students.\n'
        f'For each question, evaluate for correctness, clarity, difficulty, and relevance. Provide specific feedback on any errors or improvements needed.\n\n'
        f'Questions:\n{json.dumps(questions, indent=2)}\n\n'
        f'Output a list of JSON objects, one per question:\n'
        f'[{{"approved": true if no major issues else false, "issues": ["list of issues"], "suggestions": ["list of suggestions"]}} for each]'
    )
    raw = run_ollama(CRITIC_MODEL, prompt)
    parsed = extract_json(raw)
    if isinstance(parsed, list) and len(parsed) == len(questions):
        return parsed
    log("WARNING", f"Critique failed to return valid list for {len(questions)} questions.")
    return [{"approved": False, "issues": ["Critique failed"], "suggestions": []} for _ in questions]

# ----------------------------
# Refiner
# ----------------------------
def refine_question(question, feedback, topic, subtopic, difficulty, qtype, max_attempts=3):
    """Refine a single question using provided feedback."""
    log("INFO", f"Refining question (topic={topic} subtopic={subtopic}) with feedback issues={feedback.get('issues')}")
    prompt = (
        f'You are a question fixer for Grade 7/8 Thai students. Refine the following single question to address the listed issues.\n'
        f'Keep English very simple and use Thai-relevant contexts (baht, temple, market, tuk-tuk, rice, school).\n\n'
        f'Original question object:\n{json.dumps(question, indent=2, ensure_ascii=False)}\n\n'
        f'Feedback issues:\n{json.dumps(feedback.get("issues", []), indent=2, ensure_ascii=False)}\n\n'
        f'Suggestions:\n{json.dumps(feedback.get("suggestions", []), indent=2, ensure_ascii=False)}\n\n'
        f'Requirements:\n'
        f'- Output a single JSON object with fields: question, type, options (if MCQ or True/False), answer, topic, subtopic, difficulty\n'
        f'- The question MUST follow difficulty: {difficulty}\n'
        f'- For MCQ include 4 plausible options and place the correct answer as the "answer" field (not letter)\n'
        f'- For True/False use Correct/Wrong as options\n'
        f'- Use Thai contexts, avoid ambiguity, ensure the answer is correct.\n\n'
        f'Return only the JSON object.'
    )
    attempt = 0
    while attempt < max_attempts:
        raw = run_ollama(GENERATOR_MODEL, prompt)
        parsed = extract_json(raw)
        if isinstance(parsed, dict) and is_valid_question(parsed):
            refined = normalize_question(parsed, allowed_types=[qtype], gui_difficulty=difficulty)
            if refined:
                refined["topic"] = topic or refined.get("topic", "")
                refined["subtopic"] = subtopic or refined.get("subtopic", "")
                refined["difficulty"] = difficulty
                log("INFO", f"Refinement successful on attempt {attempt+1} for question: {refined.get('question')[:80]}")
                return refined
        attempt += 1
        log("WARNING", f"Refinement attempt {attempt} failed; retrying...")
    log("WARNING", f"All refinement attempts failed for question: {question.get('question','')[:80]}")
    return None

# ----------------------------
# Main Pipeline
# ----------------------------
def generate_questions_simple(topics, num_questions, difficulty, question_type_counts, activity_type, title="Generated Quiz", grade="7", model=None, max_refine_attempts=3):
    """Main pipeline with hybrid critique strategy."""
    generator_model = model or GENERATOR_MODEL
    log("INFO", f"Starting hybrid generation pipeline: num_questions={num_questions}, difficulty={difficulty}, question_types={question_type_counts}")
    quiz = generate_full_quiz(title, grade, difficulty, topics, question_type_counts, num_questions, activity_type, generator_model)
    if not quiz or "questions" not in quiz:
        log("ERROR", "Failed to generate quiz.")
        return None
    questions = quiz["questions"]
    n = len(questions)
    log("INFO", f"Generator returned {n} questions")
    batch_feedback = batch_critique(questions, topic_hint=quiz["settings"].get("topics"), difficulty=difficulty)
    flagged = batch_feedback.get("flagged", [])
    log("INFO", f"Batch critique flagged indices: {flagged}")
    sample_size = min(max(5, int(max(1, n) * 0.2)), 10)
    candidates = [i for i in range(n) if i not in flagged]
    random_sample = random.sample(candidates, min(len(candidates), sample_size))
    targeted_indices = sorted(list(set(flagged + random_sample)))
    log("INFO", f"Targeted indices for deep critique: {targeted_indices}")
    for idx in targeted_indices:
        q_obj = questions[idx]
        crits = critique_questions([q_obj])
        if not crits:
            log("WARNING", f"No critique returned for question index {idx}")
            continue
        feedback = crits[0]
        if feedback.get("approved", False):
            log("INFO", f"Question index {idx} approved by targeted critique.")
            continue
        for attempt in range(max_refine_attempts):
            refined = refine_question(q_obj, feedback, q_obj.get("topic", ""), q_obj.get("subtopic", ""), difficulty, q_obj.get("type"), max_attempts=1)
            if refined and is_valid_question(refined):
                post_crit = critique_questions([refined])[0]
                if post_crit.get("approved", False):
                    questions[idx] = refined
                    log("INFO", f"Refined question at index {idx} approved after attempt {attempt+1}.")
                    break
                feedback = post_crit
                log("INFO", f"Refined question still not approved; continuing refinement attempts.")
            else:
                log("WARNING", f"Refinement attempt {attempt+1} returned invalid question or None.")
        if not is_valid_question(questions[idx]):
            log("WARNING", f"Could not refine question at index {idx}; leaving original.")
    final_questions = []
    allowed_types = ["MCQ", "True/False", "Short Answer", "Fill-in-the-Blank", "Numerical"]
    for q in questions:
        nq = normalize_question(q, allowed_types=allowed_types, gui_difficulty=difficulty)
        if nq and is_valid_question(nq):
            final_questions.append(nq)
        else:
            log("WARNING", f"Dropping invalid question: {q.get('question', '')[:80]}")
    quiz["questions"] = final_questions[:num_questions]
    log("INFO", f"Final quiz contains {len(quiz['questions'])} questions.")
    return quiz

if __name__ == "__main__":
    SAMPLE_TOPICS = {
        "Chapter 7: Place value, rounding and decimals": {
            "subtopics": [
                "7.1 Multiplying and dividing by 0.1 and 0.01",
                "7.2 Rounding to significant figures",
                "7.3 Multiplying and dividing with integers and decimals"
            ]
        }
    }
    q_types = {"MCQ": 5, "True/False": 5, "Short Answer": 5, "Fill-in-the-Blank": 5, "Numerical": 5}
    quiz_obj = generate_questions_simple(SAMPLE_TOPICS, num_questions=25, difficulty="Easy", question_type_counts=q_types, activity_type="Test", title="Decimals & Rounding Quiz", grade="7")
    if quiz_obj:
        print(json.dumps(quiz_obj, indent=2, ensure_ascii=False))
    else:
        print("Quiz generation failed.")