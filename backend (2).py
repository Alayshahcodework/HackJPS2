
import os, re, json, tempfile, traceback, random
from pathlib import Path
from typing import List, Dict, Any

from flask import Flask, request, jsonify, send_from_directory, session
from werkzeug.security import generate_password_hash, check_password_hash
from PyPDF2 import PdfReader, errors as pdf_err
import docx, pytesseract
from PIL import Image
from dotenv import load_dotenv
from openai import OpenAI

# ── env / API client ─────────────────────────────────────────────────────
load_dotenv()
client = OpenAI()
pytesseract.pytesseract.tesseract_cmd = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe"
)

# ── Flask / session setup ────────────────────────────────────────────────
app            = Flask(__name__)
app.secret_key = os.getenv("SESSION_KEY", os.urandom(24).hex())

# ── super-tiny JSON “DB” for accounts ────────────────────────────────────
DB = Path("users.json")
if not DB.exists(): DB.write_text("{}")
USERS = json.loads(DB.read_text())
def _save(): DB.write_text(json.dumps(USERS, indent=2))
def me():    return session.get("user")

# ── auth routes ──────────────────────────────────────────────────────────
@app.post("/signup")
def signup():
    d=request.get_json(True); u,p=d["username"],d["password"]
    if u in USERS: return jsonify(msg="Username exists"),400
    USERS[u]=generate_password_hash(p); _save(); session["user"]=u
    return jsonify(ok=True)

@app.post("/login")
def login():
    d=request.get_json(True); u,p=d["username"],d["password"]
    if u not in USERS or not check_password_hash(USERS[u],p):
        return jsonify(msg="Bad credentials"),401
    session["user"]=u; return jsonify(ok=True)

@app.get("/logout")
def logout(): session.pop("user",None); return jsonify(ok=True)

@app.get("/whoami")
def whoami(): return jsonify(user=me())

# ── document helpers ─────────────────────────────────────────────────────
def extract_text(path: str, mime: str) -> str:
    if mime=="application/pdf":
        try: return " ".join(p.extract_text()or"" for p in PdfReader(path).pages)
        except pdf_err.DependencyError: return ""
    if mime.endswith(("msword","document")):
        return " ".join(p.text for p in docx.Document(path).paragraphs)
    return open(path,"r",encoding="utf8",errors="ignore").read()

def level_phrase(n:int)->str:
    return ("for an everyday 8th-grade reader." if n<=8 else
            "for a 10th-grade reader."          if n<=10 else
            "for a 12th-grade reader."          if n<=12 else
            "for a college reader."             if n<=16 else
            "for a master’s-level audience."    if n<=18 else
            "for a PhD-level audience.")

def rewrite(txt:str,domain:str,level:int)->str:
    prompt=(f"Rewrite the following {domain} document {level_phrase(level)} "
            "Start with one concise overview paragraph, then bullet-point key facts, "
            "and finish with numbered ‘Next Steps’. Do not mention grade levels.")
    r=client.chat.completions.create(model="gpt-3.5-turbo",
                                     messages=[{"role":"user","content":prompt+"\n\n"+txt}],
                                     max_tokens=450,temperature=0.5)
    return r.choices[0].message.content.strip()

def ocr_image(p): return pytesseract.image_to_string(Image.open(p))

# ── AI / analysis routes ─────────────────────────────────────────────────
@app.post("/summarize")
def summarize():
    lvl=int(request.form.get("level","10")); f=request.files["file"]
    with tempfile.NamedTemporaryFile(delete=False) as tmp:
        f.save(tmp.name); raw=extract_text(tmp.name,f.mimetype)
    if len(raw.strip())<30:
        return jsonify(domain="unknown",summary="Sorry, I couldn’t extract readable text.")
    meds=["patient","diagnosis","mg"]; laws=["contract","plaintiff","liable"]
    dom="medical" if sum(w in raw.lower() for w in meds)>=sum(w in raw.lower() for w in laws) else "legal"
    return jsonify(domain=dom,summary=rewrite(raw,dom,lvl))

@app.post("/prescription")
def prescription():
    f=request.files["file"]
    with tempfile.NamedTemporaryFile(delete=False,suffix=".img") as tmp:
        f.save(tmp.name); txt=ocr_image(tmp.name)
    if len(txt.strip())<20:
        return jsonify(rx="Sorry — I couldn’t read that prescription clearly.")
    prompt=("You are a pharmacy assistant. From the OCR text of a prescription, provide:\n"
            "• **Drug name**\n• **What it treats / how it works**\n• **Dosage & timing**\n"
            "• **Important precautions or side-effects**\n"
            "Answer as concise Markdown bullet lists without referencing OCR.\n\n"+txt)
    r=client.chat.completions.create(model="gpt-3.5-turbo",
                                     messages=[{"role":"user","content":prompt}],max_tokens=300,temperature=0.4)
    return jsonify(rx=r.choices[0].message.content.strip())

@app.post("/translate")
def translate():
    d=request.get_json(True); text,lang=d["text"],d["lang"]
    if not lang: return jsonify(translated="")
    r=client.chat.completions.create(model="gpt-3.5-turbo",
                                     messages=[{"role":"user","content":f"Translate to {lang} keeping bullet formatting:\n\n{text}"}],
                                     max_tokens=800,temperature=0.3)
    return jsonify(translated=r.choices[0].message.content.strip())

@app.post("/chat")
def chat():
    d=request.get_json(True); q,ctx=d["question"],d["context"]
    r=client.chat.completions.create(model="gpt-3.5-turbo",
                                     messages=[{"role":"system","content":"Answer using only the provided summary."},
                                               {"role":"assistant","content":ctx},
                                               {"role":"user","content":q}],
                                     max_tokens=300,temperature=0.4)
    return jsonify(answer=r.choices[0].message.content.strip())

@app.post("/chat-image")
def chat_image():
    img=request.files["image"]; ctx=request.form.get("context","")
    with tempfile.NamedTemporaryFile(delete=False,suffix=".img") as tmp:
        img.save(tmp.name); txt=ocr_image(tmp.name)
    r=client.chat.completions.create(model="gpt-3.5-turbo",
                                     messages=[{"role":"system","content":"If the image is a Rx, extract details; else say you can’t read it."},
                                               {"role":"assistant","content":ctx},
                                               {"role":"user","content":"OCR:\n"+txt}],
                                     max_tokens=250,temperature=0.4)
    return jsonify(answer=r.choices[0].message.content.strip())

@app.post("/define")
def define():
    term=request.get_json(True)["term"]
    r=client.chat.completions.create(model="gpt-3.5-turbo",
                                     messages=[{"role":"user","content":"Explain in ≤30 plain words:\n"+term}],
                                     max_tokens=60,temperature=0.3)
    return jsonify(definition=r.choices[0].message.content.strip())

# ── QUIZ GENERATOR (hardened) ────────────────────────────────────────────
def _clean_quiz(raw: str) -> List[Dict[str, Any]]:
    """
    Ensure every question has the required keys and sane values.
    Remove malformed entries, fix minor format issues.
    """
    try:
        data = json.loads(raw)
    except Exception:
        return []
    fixed: List[Dict[str, Any]] = []
    for q in data:
        if not isinstance(q, dict): continue
        question = str(q.get("question","")).strip()
        qtype    = str(q.get("type","mcq")).lower()
        answer   = str(q.get("answer","")).strip()
        options  = q.get("options", [])
        if qtype not in ("mcq","written"): qtype="mcq"
        if qtype=="mcq":
            if not options or not isinstance(options, list):
                # fabricate 4 dummy options (one correct)
                distractors = [f"Option {c}" for c in "BCD"]
                options = [answer] + distractors
            # shuffle so correct isn’t always first
            random.shuffle(options)
        fixed.append({"question":question,"type":qtype,
                      "options":options,"answer":answer})
    return fixed

@app.post("/make_quiz")
def make_quiz():
    req = request.get_json(True)
    summary, diff, num = req["summary"], req["difficulty"], req["num"]

    base_prompt = (
            f"Create a {diff.lower()} quiz of {num} questions based ONLY on the text below. "
            "Use a mix of multiple-choice (label options A-D) and short-answer questions. "
            "Return STRICT JSON array where each item has keys: question, type ('mcq'|'written'), "
            "options (array), answer. NO markdown, no commentary.\n\nTEXT:\n" + summary
    )

    def ask_gpt(prompt_text:str)->str:
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role":"user","content":prompt_text}],
            max_tokens=800,temperature=0.7
        ).choices[0].message.content
        return re.sub(r"```(?:json)?|```","",resp).strip()

    # try up to 3 self-repairs
    raw = ask_gpt(base_prompt)
    for _ in range(3):
        fixed = _clean_quiz(raw)
        if len(fixed) == num: break
        # ask GPT to repair using its previous output
        raw = ask_gpt("Your previous response was malformed. "
                      "Return ONLY the JSON array with correct schema.\n"+raw)

    quiz = _clean_quiz(raw)
    return jsonify(quiz)

# ── static files ─────────────────────────────────────────────────────────
@app.route("/",defaults={"path":""})
@app.route("/<path:path>")
def send_front(path):
    if path=="" or not path.endswith((".html",".js",".css")): path="index.html"
    return send_from_directory("frontend",path)

# ── run ───────────────────────────────────────────────────────────────────
if __name__=="__main__":
    app.run(debug=True,port=5000)
