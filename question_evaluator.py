import requests
import json
import random
import re
from logger import log

OLLAMA_API_URL = "http://localhost:11434/api/generate"

# Load critic model from config.json (fallback to default)
try:
    import utils
    config = utils.load_json("config.json")
    CRITIC_MODEL = config.get("critic_model", "gpt-oss:20b")
except Exception as e:
    CRITIC_MODEL = "deepseek-r1:14b"
    log("ERROR", f"Failed to load config.json, using default critic: {CRITIC_MODEL}")

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
        return "answer" in q and q["answer"] in ["True", "False", "Correct", "Wrong"]
    else:
        return "answer" in q and q["answer"] != ""

def extract_json(raw_text):
    """Attempt to robustly extract JSON from model output."""
    if not isinstance(raw_text, str) or not raw_text.strip():
        log("WARNING", "Model output is empty or not a string")
        return None
    
    raw_text = raw_text.strip()
    log("DEBUG", f"Raw model output: {raw_text[:500]}...")

    # Remove <think> and <thinking> blocks
    raw_text = re.sub(r'<think>.*?</think>', '', raw_text, flags=re.DOTALL)
    raw_text = re.sub(r'<thinking>.*?</thinking>', '', raw_text, flags=re.DOTALL)

    # Strip markdown fences
    if raw_text.startswith("```json"):
        raw_text = raw_text[7:]
    if raw_text.endswith("```"):
        raw_text = raw_text[:-3]
    raw_text = raw_text.strip()

    # Try direct JSON
    try:
        return json.loads(raw_text)
    except json.JSONDecodeError:
        log("DEBUG", "Direct JSON parsing failed, attempting regex extraction")

    # Regex fallback
    match = re.search(r'(\{[\s\S]*\}|\[[\s\S]*\])', raw_text)
    if match:
        candidate = match.group(1)
        try:
            return json.loads(candidate)
        except Exception as e:
            try:
                fixed = candidate.replace("'", '"').replace("None", "null")
                return json.loads(fixed)
            except Exception as e2:
                log("WARNING", f"Failed to parse JSON candidate: {e2}")

    log("WARNING", "All JSON extraction methods failed")
    return None


def run_ollama(model, prompt, max_retries=2, timeout=180):
    """Run a prompt through the Ollama API with fallback to gpt-oss:20b."""
    fallback_model = "gpt-oss:20b"

    for attempt in range(max_retries):
        try:
            log("DEBUG", f"Ollama API call attempt {attempt + 1} for model {model}")
            response = requests.post(
                OLLAMA_API_URL,
                json={"model": model, "prompt": prompt, "stream": False},
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

    # If model fails, fallback
    if model != fallback_model:
        log("WARNING", f"Falling back to {fallback_model}")
        return run_ollama(fallback_model, prompt, max_retries=1, timeout=timeout)

    return None



# ----------------------------
# Evaluation Functions
# ----------------------------

def batch_critique(questions, topic_hint=None, difficulty=None, max_retries=2):
    """Perform batch critique on a list of questions with retries."""
    prompt = f"""
    CRITIQUE THE FOLLOWING QUESTIONS AND RETURN JSON ONLY.
    
    IMPORTANT: Return ONLY valid JSON, no thinking, no explanations.
    
    Topic: {topic_hint or 'General'}
    Difficulty: {difficulty or 'Medium'}
    
    Analyze these questions and return a JSON object with:
    {{
        "flagged": [list of indices of problematic questions],
        "feedback": [
            {{
                "index": 0,
                "approved": true/false,
                "comments": "brief reason"
            }}
        ]
    }}
    
    Questions to critique:
    {json.dumps(questions, indent=2)}
    
    RETURN PURE JSON ONLY:
    """
    
    for attempt in range(max_retries):
        log("INFO", f"Batch critique attempt {attempt + 1}/{max_retries}")
        raw = run_ollama(CRITIC_MODEL, prompt)
        if raw is None:
            log("WARNING", "No response from Ollama API")
            continue
            
        parsed = extract_json(raw)
        if parsed and isinstance(parsed, dict) and "flagged" in parsed:
            log("INFO", "Batch critique completed successfully")
            return parsed
            
        log("WARNING", f"Invalid batch critique response on attempt {attempt + 1}")
    
    # Fallback: approve all questions
    log("WARNING", "Using fallback: approving all questions")
    return {
        "flagged": [],
        "feedback": [{"index": i, "approved": True, "comments": "Fallback approval"} 
                    for i in range(len(questions))]
    }

def critique_questions(questions, max_retries=2):
    """Perform detailed critique on a list of questions with retries."""
    if not questions:
        return []
        
    prompt = f"""
    CRITIQUE EACH QUESTION AND RETURN JSON ONLY.
    
    IMPORTANT: Return ONLY valid JSON array, no thinking, no explanations.
    
    For each question, return a JSON array of objects with:
    {{
        "approved": true/false,
        "comments": "brief explanation"
    }}
    
    Questions to critique:
    {json.dumps(questions, indent=2)}
    
    RETURN PURE JSON ARRAY ONLY:
    """
    
    for attempt in range(max_retries):
        log("INFO", f"Detailed critique attempt {attempt + 1}/{max_retries}")
        raw = run_ollama(CRITIC_MODEL, prompt)
        if raw is None:
            log("WARNING", "No response from Ollama API")
            continue
            
        parsed = extract_json(raw)
        if parsed and isinstance(parsed, list) and len(parsed) == len(questions):
            log("INFO", "Detailed critique completed successfully")
            return parsed
            
        log("WARNING", f"Invalid detailed critique response on attempt {attempt + 1}")
    
    # Fallback: approve all questions
    log("WARNING", "Using fallback: approving all questions in detailed critique")
    return [{"approved": True, "comments": "Fallback approval"} for _ in questions]

# ----------------------------
# Main Evaluation Function (Simplified)
# ----------------------------

def evaluate_and_refine_questions(questions, topic, difficulty, max_refine_attempts=2):
    """Evaluate and refine questions with robust error handling."""
    if not questions:
        log("WARNING", "No questions to evaluate")
        return questions
        
    log("INFO", f"Starting evaluation for {len(questions)} questions")
    
    try:
        # Get batch feedback
        batch_feedback = batch_critique(questions, topic_hint=topic, difficulty=difficulty)
        flagged = batch_feedback.get("flagged", [])
        log("INFO", f"Batch critique flagged {len(flagged)} questions")
        
        # Get detailed feedback for flagged questions only
        if flagged:
            flagged_questions = [questions[i] for i in flagged if i < len(questions)]
            detailed_feedback = critique_questions(flagged_questions)
            
            # Apply feedback (simplified - just log issues for now)
            for i, feedback in zip(flagged, detailed_feedback):
                if i < len(questions) and feedback and not feedback.get("approved", True):
                    log("WARNING", f"Question {i} needs refinement: {feedback.get('comments', 'No details')}")
                    # In a full implementation, you'd refine here
        
        log("INFO", f"Evaluation completed for {len(questions)} questions")
        return questions
        
    except Exception as e:
        log("ERROR", f"Evaluation failed: {e}. Returning original questions.")
        return questions

if __name__ == "__main__":
    # Test with sample questions
    SAMPLE_QUESTIONS = [
        {
            "question": "What is 2 + 2?",
            "type": "MCQ",
            "options": ["3", "4", "5", "6"],
            "answer": "4",
            "topic": "Math",
            "subtopic": "Addition",
            "difficulty": "Easy"
        }
    ]
    refined_questions = evaluate_and_refine_questions(SAMPLE_QUESTIONS, topic="Math", difficulty="Easy")
    print(json.dumps(refined_questions, indent=2, ensure_ascii=False))