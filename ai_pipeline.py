import requests
import json
import re
from logger import log

OLLAMA_API_URL = "http://localhost:11434/api/generate"

# Load models from config.json (fallback to gemma2:9b for generator, deepseek-r1:14b for critic)
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
    required = {"question", "type", "options", "answer", "topic", "subtopic", "difficulty"}
    return isinstance(q, dict) and all(k in q for k in required) and q["question"]

def analyze_question_difficulty(question):
    """Analyze the question's content to estimate its difficulty based on numbers and steps."""
    text = question.get("question", "") + " " + question.get("answer", "")
    numbers = [float(n) for n in re.findall(r'\d+\.?\d*', text) if n]
    
    # Estimate steps by checking for operations or keywords
    steps = 1
    if any(op in text.lower() for op in ["+", "-", "*", "/", "average", "total", "difference"]):
        steps += text.lower().count("and") + text.lower().count(",")
    
    if max(numbers, default=0) < 20 and steps <= 1:
        return "Easy"
    elif max(numbers, default=0) < 100 and steps <= 2:
        return "Medium"
    elif max(numbers, default=0) < 1000 and steps > 2:
        return "Hard"
    return "Medium"  # Fallback if unclear

def normalize_question(q, allowed_types=None, gui_difficulty=None):
    """Ensure type/format consistency and validate difficulty against GUI setting."""
    q_type = q.get("type", "").lower()
    if q_type in ["multiple_choice", "multiple-choice", "mcq"]:
        q["type"] = "MCQ"
    elif q_type in ["true/false", "true_false", "tf"]:
        q["type"] = "True/False"
    elif q_type in ["short", "short answer", "sa"]:
        q["type"] = "Short Answer"
    elif q_type in ["fill", "fill-in-the-blank", "fib"]:
        q["type"] = "Fill-in-the-Blank"
    elif q_type in ["num", "numerical", "calculation"]:
        q["type"] = "Numerical"
    else:
        q["type"] = q.get("type", "Short Answer")

    # Drop questions not in allowed types
    if allowed_types and q["type"] not in allowed_types:
        return None

    if q["type"] == "MCQ" and not q.get("options"):
        q["options"] = [q["answer"]] + [f"Option {i}" for i in range(1, 4)]
    if q["type"] == "True/False":
        q["options"] = ["True", "False"]

    # Validate difficulty against GUI setting
    if gui_difficulty:
        estimated_difficulty = analyze_question_difficulty(q)
        if estimated_difficulty != gui_difficulty:
            log("WARNING", f"Difficulty mismatch for question '{q['question'][:50]}...': labeled {q['difficulty']}, estimated {estimated_difficulty}, GUI {gui_difficulty}. Using GUI difficulty.")
        q["difficulty"] = gui_difficulty
    else:
        q["difficulty"] = q.get("difficulty", analyze_question_difficulty(q))

    return q

def extract_json(raw_text):
    """Extract JSON substring from messy output."""
    raw_text = raw_text.strip()
    if raw_text.startswith("```json"):
        raw_text = raw_text[7:].rstrip("```").strip()
    elif raw_text.startswith("```"):
        raw_text = raw_text[3:].rstrip("```").strip()

    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        pass

    match = re.search(r'(\[.*\]|\{.*\})', raw_text, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return []
    return []

def deduplicate_questions(questions):
    """Remove duplicates based on question text (case-insensitive)."""
    seen = set()
    unique = []
    for q in questions:
        text = q.get("question", "").strip().lower()
        if text and text not in seen:
            seen.add(text)
            unique.append(q)
    return unique

# ----------------------------
# Core Ollama call
# ----------------------------
def run_ollama(model, prompt):
    log("DEBUG", f"Calling Ollama model={model}, prompt length={len(prompt)}")
    try:
        response = requests.post(
            OLLAMA_API_URL,
            json={"model": model, "prompt": prompt},
            stream=True
        )
        response.raise_for_status()

        full_text = ""
        for line in response.iter_lines():
            if not line:
                continue
            try:
                obj = json.loads(line.decode("utf-8"))
                if "response" in obj:
                    full_text += obj["response"]
            except Exception as e:
                log("WARNING", f"Skipping bad line: {line[:100]}... ({e})")

        log("DEBUG", f"Ollama raw output preview: {full_text[:200]}...")
        return full_text.strip()
    except Exception as e:
        log("ERROR", f"Ollama call failed: {e}")
        return ""

# ----------------------------
# Critique questions
# ----------------------------
def critique_questions(questions, topic, subtopic, difficulty, qtype):
    """
    Use critic model to evaluate questions and provide feedback, respecting GUI difficulty.
    Always enforces at least one issue or suggestion.
    """
    log("INFO", f"Critiquing {len(questions)} questions with model={CRITIC_MODEL}, GUI difficulty={difficulty}")
    critique_prompt = f"""
Evaluate the following {len(questions)} questions for a {qtype} quiz on {topic} ({subtopic}, {difficulty} difficulty).

For each question, check:
1. Factual accuracy (is the answer correct?).
2. Topic relevance (does it match {topic}/{subtopic}?).
3. Difficulty alignment (does it match {difficulty}? Use: Easy: <20, 1-step; Medium: <100, 2 steps; Hard: <1000, multi-step).
4. Clarity (is it clear and well-phrased?).
5. For MCQ: Are options realistic distractors (common mistakes, plausible values)?
6. Variety: Does it avoid repeating the same theme or scenario?
7. Cognitive mix: Is there a balance of direct calculation and conceptual/identify-type questions?
8. Language: Simple, short English for Grade 7–8 in Thailand?

Important:
- You MUST return feedback for every question.
- Each feedback object MUST include:
  * "index": the question index (0-based),
  * "issues": a non-empty list of strings (if no real issues, use ["Looks good"]),
  * "suggestions": a list of improvements (can be empty only if issues=["Looks good"]),
  * "approved": true or false.
- Do NOT skip any question.
- Do NOT leave "issues" as empty or "No feedback provided".
- Respect the GUI-provided difficulty ({difficulty}) unless the question content clearly violates the rules.

Input questions:
{json.dumps(questions, indent=2)}
"""
    raw = run_ollama(CRITIC_MODEL, critique_prompt)
    feedback = extract_json(raw)
    
    if not isinstance(feedback, list):
        log("ERROR", f"Critic model returned invalid feedback: {feedback}")
        feedback = []

    feedback_map = {f["index"]: f for f in feedback if isinstance(f, dict) and "index" in f}
    result = []
    for i, q in enumerate(questions):
        f = feedback_map.get(i)
        if not f or not isinstance(f, dict):
            f = {}
        # Enforce structure
        issues = f.get("issues") if isinstance(f.get("issues"), list) else []
        suggestions = f.get("suggestions") if isinstance(f.get("suggestions"), list) else []
        approved = bool(f.get("approved", False))

        # Check difficulty alignment
        estimated_difficulty = analyze_question_difficulty(q)
        if estimated_difficulty != difficulty:
            issues.append(f"Difficulty mismatch: content suggests {estimated_difficulty}, but GUI specifies {difficulty}.")
            suggestions.append(f"Adjust question content to match {difficulty} or update label.")
            approved = False
        elif not issues:
            issues = ["Looks good"]
            approved = True

        if not suggestions and issues != ["Looks good"]:
            suggestions = ["Rephrase question or simplify wording"]

        enforced = {
            "index": i,
            "question": q,
            "issues": issues,
            "suggestions": suggestions,
            "approved": approved
        }
        result.append(enforced)
        log("INFO", f"Critique for question {i}: approved={enforced['approved']}, issues={enforced['issues']}, suggestions={enforced['suggestions']}")
    
    return result

# ----------------------------
# Refine questions
# ----------------------------
def refine_question(question, feedback, topic, subtopic, difficulty, qtype):
    """Refine a single question based on critic feedback, respecting GUI difficulty."""
    log("INFO", f"Refining question: {question['question'][:50]}... with issues: {feedback['issues']}")
    prompt = f"""
Refine the following {qtype} question for {topic} ({subtopic}, {difficulty} difficulty) based on feedback.

Context:
- Students are Grade 7 or 8 in Thailand.
- Keep the English very simple.
- Use Thai everyday life examples (baht, food, rice, football, tuk-tuks, school, transport, farming, etc.).
- Stick to add, subtract, multiply, divide, squares, cubes, indices, roots.
- Ensure it is different in theme from other questions in the batch.
- For MCQs, provide realistic distractors.
- Follow difficulty rules: Easy (<20, 1-step), Medium (<100, 2 steps), Hard (<1000, multi-step).
- MUST match the GUI-specified difficulty: {difficulty}.

Original question: {json.dumps(question, indent=2)}
Issues: {feedback['issues']}
Suggestions: {feedback['suggestions']}
Return a single JSON object with fields: question, type, options, answer, topic, subtopic, difficulty.
"""
    raw = run_ollama(GENERATOR_MODEL, prompt)
    refined = extract_json(raw)
    
    if isinstance(refined, dict) and is_valid_question(refined):
        refined = normalize_question(refined, allowed_types=[qtype], gui_difficulty=difficulty)
        if not refined:
            return None
        refined["topic"] = refined.get("topic") or topic
        refined["subtopic"] = refined.get("subtopic") or subtopic
        refined["difficulty"] = difficulty  # Enforce GUI difficulty
        log("INFO", f"Refined question: {refined['question'][:50]}...")
        return refined
    else:
        log("WARNING", f"Failed to refine question, discarding: {question['question'][:50]}...")
        return None

# ----------------------------
# Optimized generator with retry + safeguard
# ----------------------------
def generate_questions_simple(topics, num_questions, difficulty, question_type_counts, activity_type, model=None, max_retries=3):
    generator_model = model or GENERATOR_MODEL
    log("INFO", f"Starting question generation with generator={generator_model}, critic={CRITIC_MODEL}, num_questions={num_questions}, difficulty={difficulty}")
    questions = []
    api_call_count = 0
    retries = 0

    subtopic_pairs = [(topic, subtopic) for topic, data in topics.items() for subtopic in data["subtopics"]]
    total_subtopics = len(subtopic_pairs)
    if total_subtopics == 0:
        log("ERROR", "No subtopics provided, cannot generate questions")
        return []

    total_qtype_counts = sum(question_type_counts.values())
    if total_qtype_counts == 0:
        log("ERROR", "No question types selected")
        return []

    # Retry loop until enough questions are gathered
    while len(questions) < num_questions and retries < max_retries:
        remaining = num_questions - len(questions)
        log("INFO", f"Still need {remaining} more questions (retry {retries+1}/{max_retries})...")

        for qtype, _ in question_type_counts.items():
            if len(questions) >= num_questions:
                break
            per_batch = max(1, remaining // len(question_type_counts))

            for topic, subtopic in subtopic_pairs:
                if len(questions) >= num_questions:
                    break

                prompt = f"""
Generate {per_batch} questions of type {qtype} for Thai students in Grade 7 or 8.

CRITICAL REQUIREMENTS:
- ENGLISH LEVEL: Use VERY SIMPLE English suitable for ESL learners (Grade 7-8 Thailand)
- THAI CONTEXT: Use everyday Thai situations (baht, food, markets, schools, temples, transportation, family life)
- MATH LEVEL: Align with Thailand Basic Education Curriculum for Grade 7/8
- SENTENCE STRUCTURE: Short, clear sentences. Avoid complex grammar.
 

CONTEXT REQUIREMENTS:
- Use Thai currency (baht), Thai food prices, Thai measurements
- Examples: shopping at markets, school activities, family budgeting, transportation costs
- Cultural references: temples, festivals (Songkran, Loy Krathong), Thai sports
- Realistic numbers for Thai context (prices, measurements, quantities)

MATHEMATICAL CONTENT (Grade 7-8 Thailand Curriculum):
- Numbers & Operations: integers, fractions, decimals, percentages, ratios
- Basic Algebra: simple equations, patterns, relationships
- Geometry: basic shapes, angles, measurements, area, perimeter
- Data: simple graphs, charts, averages
- Applied math: everyday problem-solving

QUESTION STRUCTURE:
- Clear and unambiguous wording
- One mathematical concept per question
- Logical progression from easy to medium difficulty
- Culturally appropriate scenarios

For {topic} - {subtopic}:
- Focus specifically on this mathematical concept
- Create varied scenarios that illustrate different applications
- Ensure questions test understanding, not just calculation

Difficulty: {difficulty}
- Easy: Single-step problems with numbers <20
- Medium: 1-2 step problems with numbers <100  
- Hard: Multi-step problems with numbers <1000

Return JSON array with {per_batch} question objects containing: question, type, options, answer, topic, subtopic, difficulty.
"""
                api_call_count += 1
                raw = run_ollama(generator_model, prompt)
                parsed = extract_json(raw)

                if not isinstance(parsed, list):
                    log("WARNING", f"Non-list response from generator: {parsed}")
                    continue

                parsed = deduplicate_questions(parsed)
                critiques = critique_questions(parsed, topic, subtopic, difficulty, qtype)
                api_call_count += 1

                for critique in critiques:
                    if len(questions) >= num_questions:
                        break
                    question = critique["question"]

                    if critique["approved"]:
                        question = normalize_question(question, allowed_types=question_type_counts.keys(), gui_difficulty=difficulty)
                        if not question:
                            continue
                        question["type"] = qtype
                        question["topic"] = question.get("topic") or topic
                        question["subtopic"] = question.get("subtopic") or subtopic
                        question["difficulty"] = difficulty  # Enforce GUI difficulty
                        questions.append(question)
                    else:
    # Try refining up to 3 times before giving up
                        refined = None
                        for attempt in range(3):
                            refined = refine_question(question, critique, topic, subtopic, difficulty, qtype)
                            api_call_count += 1
                            if refined and is_valid_question(refined):
                                refined = normalize_question(refined, allowed_types=question_type_counts.keys(), gui_difficulty=difficulty)
                                if refined:
                                    questions.append(refined)
                                    break
                        if not refined:
                            log("WARNING", f"Refinement failed after 3 attempts for question: {question['question'][:50]}...")


                            retries += 1

    if len(questions) < num_questions:
        log("WARNING", f"Reached max retries ({max_retries}) but only got {len(questions)}/{num_questions} questions")

    log("INFO", f"Generated {len(questions)} questions using {api_call_count} API calls (retries={retries})")
    return questions[:num_questions]