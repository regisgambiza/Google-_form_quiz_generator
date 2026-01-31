import os
import json
import re
import requests
from datetime import datetime
from collections import defaultdict
import difflib
import copy

# ----------------------------
# Configuration
# ----------------------------
ACTIVITIES_FOLDER = "Activities"
OUTPUT_FOLDER = "Activities_Refined"
OLLAMA_API_URL = "http://localhost:11434/api/generate"
MODEL_CHECK_URL = "http://localhost:11434/api/tags"
CRITIC_MODELS = ["gemma2:9b", "llama3.1:8b","mistral:7b"]  # Large models + fallback
SINGLE_MODEL_MODE = False  # Use one model at a time (set to False for debate)
PRIMARY_MODEL = "llama3.1:8b"  # Primary large model
FALLBACK_MODEL = "gemma2:9b"  # Fallback if large models fail
BATCH_SIZE = 5  # Limit batch size for large models
os.makedirs(OUTPUT_FOLDER, exist_ok=True)
DEBUG_LOG_FILE = "ollama_debug.log"
MODEL_FAILURES = defaultdict(int)
MAX_FAILURES = 3

# ----------------------------
# Logger
# ----------------------------
def log(level, message):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{timestamp}] [{level}] {message}")
    with open(DEBUG_LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{timestamp}] [{level}] {message}\n")

# ----------------------------
# Helper Functions
# ----------------------------
def load_json_file(filepath):
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        log("ERROR", f"Failed to load JSON {filepath}: {e}")
        return None

def save_json_file(data, filepath):
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        log("INFO", f"Saved refined JSON: {filepath}")
    except Exception as e:
        log("ERROR", f"Failed to save JSON {filepath}: {e}")

def extract_json(raw_text):
    if not isinstance(raw_text, str) or not raw_text.strip():
        log("WARNING", f"Model output is empty or not a string: {raw_text}")
        return None
    raw_text = raw_text.strip()
    log("DEBUG", f"Raw model output: {raw_text[:500]}...")
    raw_text = re.sub(r'<think>.*?</think>', '', raw_text, flags=re.DOTALL)
    raw_text = re.sub(r'<thinking>.*?</thinking>', '', raw_text, flags=re.DOTALL)
    raw_text = re.sub(r'```python|```code|```.*', '', raw_text)  # Remove code blocks
    raw_text = re.sub(r'Here is the Python code.*?(?=\{)', '', raw_text, flags=re.DOTALL)  # Remove code intro
    if raw_text.startswith("```json"):
        raw_text = raw_text[7:]
    if raw_text.endswith("```"):
        raw_text = raw_text[:-3]
    raw_text = raw_text.strip()
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        log("DEBUG", "Direct JSON parsing failed, attempting regex extraction")
        match = re.search(r'(\{[\s\S]*\}|\[[\s\S]*\])', raw_text)
        if match:
            candidate = match.group(1)
            try:
                return json.loads(candidate)
            except Exception as e:
                try:
                    fixed = candidate.replace("'", '"').replace("None", "null").replace("True", "true").replace("False", "false")
                    fixed = re.sub(r',\s*}', '}', fixed)
                    fixed = re.sub(r',\s*\]', ']', fixed)
                    return json.loads(fixed)
                except Exception as e2:
                    log("WARNING", f"Failed to parse JSON candidate: {e2}")
    log("WARNING", "All JSON extraction methods failed")
    return None

def check_ollama_health():
    try:
        response = requests.get("http://localhost:11434/", timeout=10)
        response.raise_for_status()
        log("INFO", "Ollama server is responsive")
        return True
    except Exception as e:
        log("ERROR", f"Ollama server health check failed: {e}")
        return False

def check_model_availability():
    available_models = []
    try:
        response = requests.get(MODEL_CHECK_URL, timeout=10)
        response.raise_for_status()
        data = response.json()
        available_models = [model["name"] for model in data.get("models", [])]
        log("INFO", f"Available models: {available_models}")
    except Exception as e:
        log("ERROR", f"Failed to check model availability: {e}")
    valid_models = [m for m in CRITIC_MODELS if m in available_models]
    if not valid_models:
        log("ERROR", "No configured models are available")
        return []
    if len(valid_models) < len(CRITIC_MODELS):
        log("WARNING", f"Some models unavailable. Using: {valid_models}")
    return valid_models

def run_ollama(model, prompt, max_retries=2, timeout=600):
    for attempt in range(max_retries):
        try:
            log("DEBUG", f"Ollama API call attempt {attempt + 1} for model {model}")
            response = requests.post(
                OLLAMA_API_URL,
                json={"model": model, "prompt": prompt, "stream": False, "max_tokens": 4096},
                timeout=timeout
            )
            response.raise_for_status()
            data = response.json()
            if "response" in data:
                log("DEBUG", f"Ollama API success for model {model}")
                return data["response"]
            else:
                log("WARNING", f"Ollama API response missing 'response': {data}")
        except requests.RequestException as e:
            log("ERROR", f"Ollama API call failed for model {model} (attempt {attempt + 1}): {e}")
    MODEL_FAILURES[model] += 1
    log("WARNING", f"Model {model} failed after {max_retries} attempts")
    return None

def diff_dicts(original, refined):
    orig_str = json.dumps(original, indent=2, sort_keys=True).splitlines()
    ref_str = json.dumps(refined, indent=2, sort_keys=True).splitlines()
    diff = difflib.unified_diff(orig_str, ref_str, fromfile='original', tofile='refined', lineterm='')
    return '\n'.join(diff)

# ----------------------------
# Question Evaluation Functions
# ----------------------------
def evaluate_question(question, topic=None, difficulty=None, model=None):
    if not model:
        log("WARNING", f"No model specified for question: {question.get('question')[:60]}...")
        return {"approved": True, "comments": "Skipped: No model", "suggested_refinement": None}
    
    log("INFO", f"Evaluating question: {question.get('question')[:60]}... with model {model}")
    prompt = f"""CRITIQUE THIS QUESTION AND RETURN JSON ONLY.
IMPORTANT: Return ONLY valid JSON, no thinking, no explanations, no narrative text, no markdown, no code.
Topic: {topic or 'General'}
Difficulty: {difficulty or 'Medium'}
Question: {json.dumps(question, ensure_ascii=False)}
Respond with: {{"approved": true/false, "comments": "brief explanation", "suggested_refinement": {{full refined question dict if suggesting changes, else null}}}}"""
    raw = run_ollama(model, prompt)
    if raw is None:
        log("WARNING", f"No response from model {model} for question: {question.get('question')[:60]}...")
        return {"approved": True, "comments": f"Skipped: No response from {model}", "suggested_refinement": None}
    parsed = extract_json(raw)
    if parsed and isinstance(parsed, dict) and "approved" in parsed:
        log("DEBUG", f"Model {model} approved={parsed['approved']} suggested_refinement={bool(parsed.get('suggested_refinement'))}")
        return parsed
    log("WARNING", f"Invalid response from model {model}, defaulting to approve")
    return {"approved": True, "comments": f"Fallback: Invalid response from {model}", "suggested_refinement": None}

def batch_evaluate_questions(questions, topic=None, difficulty=None, available_models=None):
    if not questions:
        return []
    if not available_models:
        return [{"index": i, "feedbacks": []} for i in range(len(questions))]
    
    result = []
    for start_idx in range(0, len(questions), BATCH_SIZE):
        batch = questions[start_idx:start_idx + BATCH_SIZE]
        prompt = f"""CRITIQUE THESE QUESTIONS AND RETURN JSON ONLY.
IMPORTANT: Return ONLY valid JSON array, no thinking, no explanations, no narrative text, no markdown, no code.
Topic: {topic or 'General'}
Difficulty: {difficulty or 'Medium'}
Questions: {json.dumps(batch, ensure_ascii=False)}
Respond with array of: {{"index": 0, "approved": true/false, "comments": "brief explanation", "suggested_refinement": {{full refined question dict if suggesting changes, else null}}}}"""
        all_feedback = []
        active_models = [m for m in available_models if MODEL_FAILURES[m] < MAX_FAILURES]
        if not active_models:
            log("WARNING", "All models exceeded failure limit, using fallback")
            return [{"index": i, "feedbacks": []} for i in range(len(questions))]
        
        models_to_use = active_models if not SINGLE_MODEL_MODE else [PRIMARY_MODEL]
        for model in models_to_use:
            if MODEL_FAILURES[model] >= MAX_FAILURES:
                continue
            raw = run_ollama(model, prompt)
            if raw is None:
                log("WARNING", f"Batch evaluation failed for {model}")
                fallback_fb = [{"index": i, "approved": True, "comments": f"Skipped: No response from {model}", "suggested_refinement": None} for i in range(len(batch))]
                all_feedback.append((model, fallback_fb))
                continue
            parsed = extract_json(raw)
            if parsed and isinstance(parsed, list) and len(parsed) == len(batch):
                log("DEBUG", f"Batch evaluation succeeded for {model}")
                all_feedback.append((model, parsed))
                MODEL_FAILURES[model] = 0
            else:
                log("WARNING", f"Invalid batch response from {model}")
                fallback_fb = [{"index": i, "approved": True, "comments": f"Fallback: Invalid response from {model}", "suggested_refinement": None} for i in range(len(batch))]
                all_feedback.append((model, fallback_fb))
                MODEL_FAILURES[model] += 1
        
        for idx in range(len(batch)):
            global_idx = start_idx + idx
            feedbacks = [f_list[idx] for m, f_list in all_feedback if idx < len(f_list)]
            result.append({"index": global_idx, "feedbacks": feedbacks})
    
    return result

def evaluate_and_refine_questions(questions, topic=None, difficulty=None, available_models=None):
    original_questions = copy.deepcopy(questions)
    refined_questions = copy.deepcopy(questions)
    batch_feedback = batch_evaluate_questions(questions, topic, difficulty, available_models)
    
    needing_refine = []
    needing_indices = []
    needing_feedbacks = []  # List of lists of feedbacks per question
    for fb_item in batch_feedback:
        idx = fb_item["index"]
        fbs = fb_item["feedbacks"]
        all_approved = all(f.get("approved", True) for f in fbs)
        has_sugg = any(f.get("suggested_refinement") is not None and f.get("suggested_refinement") for f in fbs)
        if not all_approved or has_sugg:
            needing_refine.append(questions[idx])
            needing_indices.append(idx)
            needing_feedbacks.append(fbs)
    
    refinements = {}  # idx: comments
    critic_refinements_applied = {}  # idx: (model, comments) if applied from critic
    
    if needing_refine:
        # Try primary model for overall refinement
        prompt = f"""REFINE THESE QUESTIONS BASED ON CRITIC FEEDBACK AND RETURN JSON ONLY.
IMPORTANT: Return ONLY a valid JSON array like [{{"local_index":0,"refined_question":{{"type":"...","question":"...","answer":"..."}}, "refinement_comments":"brief explanation of changes"}}], no other text, no code, no thinking.
Address ALL flagged issues from ANY critic model. Fix question text, options, answer, type, difficulty as needed. You have full freedom.
Original questions: {json.dumps(needing_refine, ensure_ascii=False)}
Critic feedbacks: {json.dumps(needing_feedbacks, ensure_ascii=False)}"""
        raw = run_ollama(PRIMARY_MODEL, prompt)
        parsed_refine = None
        if raw:
            parsed_refine = extract_json(raw)
            if isinstance(parsed_refine, list) and len(parsed_refine) == len(needing_refine):
                log("INFO", "Primary model refinement succeeded")
                for i, p in enumerate(parsed_refine):
                    if "refined_question" in p and p["refined_question"]:
                        refined_questions[needing_indices[i]] = p["refined_question"]
                        refinements[needing_indices[i]] = p.get("refinement_comments", "Refined by primary model")
            else:
                log("WARNING", "Primary refinement failed, falling back to individual critic suggestions")
        
        if not parsed_refine or not (isinstance(parsed_refine, list) and len(parsed_refine) == len(needing_refine)):
            # Fallback: apply first valid suggestion from critics for each
            for i, idx in enumerate(needing_indices):
                fbs = needing_feedbacks[i]
                for fb in fbs:
                    if fb.get("suggested_refinement") and isinstance(fb["suggested_refinement"], dict):
                        refined_questions[idx] = fb["suggested_refinement"]
                        refinements[idx] = fb.get("comments", "Applied suggestion from critic")
                        critic_refinements_applied[idx] = (fb.get("model", "unknown"), fb.get("comments", ""))
                        log("INFO", f"Applied critic suggestion to question {idx}")
                        break
                if idx not in refinements:
                    refinements[idx] = "; ".join([f.get("comments", "") for f in fbs if not f.get("approved", True)])
    
    return refined_questions, needing_indices, refinements, original_questions, critic_refinements_applied

# ----------------------------
# Activity Processing
# ----------------------------
def process_activity_file(filepath, available_models):
    activity = load_json_file(filepath)
    if not activity:
        return
    questions = activity.get("questions", [])
    if not questions:
        log("WARNING", f"No questions found in {filepath}")
        return
    topic = activity.get("settings", {}).get("topics", [[None]])[0][0]
    difficulty = activity.get("settings", {}).get("difficulty")
    refined_questions, needing_indices, refinements, original_questions, critic_applied = evaluate_and_refine_questions(questions, topic, difficulty, available_models)
    activity["questions"] = refined_questions
    output_path = os.path.join(OUTPUT_FOLDER, os.path.basename(filepath))
    save_json_file(activity, output_path)
    
    report_path = os.path.join(OUTPUT_FOLDER, f"changes_report_{os.path.basename(filepath)}.txt")
    with open(report_path, "w", encoding="utf-8") as rf:
        if not needing_indices:
            rf.write("All questions approved by all models. No refinements needed.\n")
            log("INFO", f"No changes needed for {filepath}")
        else:
            rf.write(f"Flagged questions ({len(needing_indices)} total):\n\n")
            for idx in sorted(needing_indices):
                orig = original_questions[idx]
                ref = refined_questions[idx]
                diff = diff_dicts(orig, ref)
                comm = refinements.get(idx, "No comments from primary or critics")
                if idx in critic_applied:
                    model, c_comm = critic_applied[idx]
                    comm += f" (Applied from critic {model}: {c_comm})"
                rf.write(f"Question {idx}:\n")
                if diff:
                    rf.write(f"Changes:\n{diff}\n")
                else:
                    rf.write("No changes made despite flag.\n")
                rf.write(f"Comments: {comm}\n\n")
            log("INFO", f"Changes report saved: {report_path}")

def process_all_activities():
    if not check_ollama_health():
        log("ERROR", "Aborting due to Ollama server unavailability")
        return
    available_models = check_model_availability()
    if not available_models:
        log("ERROR", "Aborting due to unavailable models")
        return
    global PRIMARY_MODEL
    if PRIMARY_MODEL not in available_models:
        log("WARNING", f"Primary model {PRIMARY_MODEL} unavailable, trying fallback")
        if FALLBACK_MODEL in available_models:
            PRIMARY_MODEL = FALLBACK_MODEL  # Temporarily switch
    for filename in os.listdir(ACTIVITIES_FOLDER):
        if filename.lower().endswith(".json"):
            log("INFO", f"Processing activity file: {filename}")
            process_activity_file(os.path.join(ACTIVITIES_FOLDER, filename), available_models)

# ----------------------------
# Main
# ----------------------------
if __name__ == "__main__":
    log("INFO", "Starting per-question evaluation of all activities...")
    process_all_activities()
    log("INFO", "All activities processed.")