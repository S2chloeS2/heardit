"""OpenAI calls for TranscriptoAI.

Every prompt here is grounded in one session's transcript. The chat in
particular is constrained to it: if the transcript does not cover a question,
the assistant says so instead of answering from general knowledge, which is the
whole point of taking notes from a specific lecture or meeting.
"""

import json
import os
import re

import openai

import engines

CHAT_MODEL = os.getenv("CHAT_MODEL", "gpt-4o-mini")
# Summaries carry the study value, so they default to the stronger model.
# Everything else (chat, keywords, translation) stays on the cheap one.
SUMMARY_MODEL = os.getenv("SUMMARY_MODEL", "gpt-4o")

# Re-exported so callers keep importing one module for limits.
MAX_UPLOAD_BYTES = engines.MAX_UPLOAD_BYTES


def _client():
    key = os.getenv("OPENAI_API_KEY")
    if not key:
        raise RuntimeError(
            "OPENAI_API_KEY 가 설정되지 않았습니다. .env 에 넣은 뒤 "
            "check_keys.py 로 확인해보세요."
        )
    return openai.OpenAI(api_key=key)


def _chat(messages, model=None, **kwargs):
    return _client().chat.completions.create(
        model=model or CHAT_MODEL, messages=messages, **kwargs
    ).choices[0].message.content.strip()


# ------------------------------------------------------------------ language

# Codes the app names explicitly in prompts. Anything else is passed through
# as the model's own name for it.
LANGUAGE_NAMES = {
    "en": "English", "ko": "Korean", "ja": "Japanese", "zh": "Chinese",
    "es": "Spanish", "fr": "French", "de": "German", "pt": "Portuguese",
    "it": "Italian", "ru": "Russian", "vi": "Vietnamese", "hi": "Hindi",
    "ar": "Arabic", "id": "Indonesian", "th": "Thai", "tr": "Turkish",
}


def language_name(code):
    return LANGUAGE_NAMES.get((code or "").lower(), code or "the transcript's language")


def detect_language(text):
    """ISO 639-1 code of the language `text` is written in.

    Scripts that identify a language on their own are decided locally; Latin
    script could be a dozen languages, so a tiny model call settles those.
    """
    sample = (text or "").strip()[:1500]
    if not sample:
        return None
    letters = [c for c in sample if c.isalpha()]
    if not letters:
        return None
    hangul = sum(1 for c in letters if "\uac00" <= c <= "\ud7a3")
    kana = sum(1 for c in letters if "\u3040" <= c <= "\u30ff")
    han = sum(1 for c in letters if "\u4e00" <= c <= "\u9fff")
    n = len(letters)
    if hangul / n > 0.3:
        return "ko"
    if kana / n > 0.1:
        return "ja"
    if han / n > 0.3:
        return "zh"
    try:
        code = _chat(
            [
                {"role": "system", "content": "Reply with only the ISO 639-1 code of the language the text is written in, e.g. en, es, fr."},
                {"role": "user", "content": sample},
            ],
            max_tokens=5,
            temperature=0,
        ).lower().strip(" .`'\"")
        return code[:2] if re.fullmatch(r"[a-z]{2}", code[:2]) else "en"
    except Exception:
        return "en"


def _language_rule(lang):
    """A prompt line that names the output language outright.

    Telling the model 'not English' made it translate English lectures into
    Korean; naming the target language is unambiguous.
    """
    if lang:
        return (f"LANGUAGE: write every word of your output in {language_name(lang)}, "
                "whatever language the transcript is in. Technical terms may stay "
                "as spoken. These instructions are in English, which must not "
                "influence your output language.")
    return ("LANGUAGE: write every word of your output in the language the "
            "transcript is spoken in. These instructions are in English, which "
            "must not influence your output language.")


# ------------------------------------------------------------- transcription

def transcribe(path, kind="lecture", diarize=False, prompt=None, language=None):
    """Transcribe a file and drop Whisper's filler-for-silence.

    Returns engines.transcribe's shape with hallucinated segments removed.
    """
    result = engines.transcribe(
        path, kind=kind, diarize=diarize, prompt=prompt, language=language
    )
    result["segments"] = [
        seg for seg in result["segments"] if not _is_hallucination(seg["text"])
    ]
    return result


def transcribe_text(path, prompt=None, **kwargs):
    """Convenience wrapper for callers that only want the words."""
    return engines.plain_text(transcribe(path, prompt=prompt, **kwargs))


# Whisper invents text when handed silence or music. These are the phrases it
# reaches for; dropping them keeps junk out of summaries built from quiet audio.
_HALLUCINATIONS = {
    "you", "thank you", "thanks for watching", "thank you for watching",
    "시청해주셔서 감사합니다", "구독과 좋아요 부탁드립니다", "감사합니다",
    "ご視聴ありがとうございました", "字幕by", "please subscribe",
}


def _is_hallucination(text):
    """True when a clip produced nothing but Whisper's filler for silence."""
    stripped = (text or "").lower().strip(" .!?,。")
    if not stripped:
        return True
    if stripped in _HALLUCINATIONS:
        return True
    # A clip that is one short phrase repeated is also filler, not speech.
    words = stripped.split()
    return len(words) > 6 and len(set(words)) <= 2


# ------------------------------------------------------------- summarisation

SUMMARY_SYSTEM = (
    "You turn transcripts of lectures and meetings into study notes that someone "
    "could revise from for an exam without re-listening. You work only from the "
    "transcript you are given. Never add facts, examples, or definitions that do "
    "not appear in it. Transcripts come from speech recognition and contain "
    "errors and false starts; read through them, but do not invent content to "
    "fill gaps.\n\n"
    "Output rules:\n"
    "- Output markdown only: headings and bullet points. No preamble, no closing "
    "remark, no code fences.\n"
    "- Use exactly the section headings you are given, translated into the output "
    "language. A heading line contains the heading and nothing else; the "
    "guidance after each heading below describes what goes under it and must "
    "never be copied into the notes.\n"
    "- Be exhaustive rather than brief. Every point the speaker made belongs "
    "somewhere in the notes. Compressing three points into one sentence is a "
    "failure; a reader should not need the recording.\n\n{language}"
)

LECTURE_SHAPE = (
    "Sections, in this order:\n"
    "1. 'Overview': what this session covered and why it matters, in three to "
    "five sentences.\n"
    "2. 'Key concepts': one bullet per concept or term, with the term in bold, "
    "defined the way the speaker defined it. Add sub-bullets for properties, "
    "conditions, and contrasts the speaker drew with other concepts. Every "
    "concept mentioned gets a bullet, even briefly mentioned ones.\n"
    "3. 'Examples and explanations': every example, analogy, story, demonstration "
    "or worked problem, each with the point it was making. One bullet per "
    "example.\n"
    "4. 'Formulas, procedures and figures': every equation, algorithm, step "
    "list, number, statistic, date, name and reference mentioned. Skip this "
    "section only if there is nothing.\n"
    "5. 'What the speaker emphasised': things flagged as important, repeated, "
    "or said to be on the exam, in an assignment, or worth remembering.\n"
    "6. 'Questions to check yourself': six to ten short questions this material "
    "answers, for self-testing, each followed by a one-line answer in a "
    "sub-bullet.\n"
    "Keep the speaker's own terminology and the order in which ideas were built up."
)

MEETING_SHAPE = (
    "Sections, in this order:\n"
    "1. 'Overview': purpose of the meeting and who took part, if known.\n"
    "2. 'Decisions': each decision, with the reasoning given for it.\n"
    "3. 'Action items': who committed to what, and any deadline mentioned. One "
    "bullet per item.\n"
    "4. 'Discussion': each topic raised, the positions taken and by whom, and "
    "where it landed. Sub-bullets for the detail.\n"
    "5. 'Figures and references': numbers, dates, names, documents and tools "
    "mentioned.\n"
    "6. 'Open questions': anything left unresolved or deferred, and who owns "
    "following it up.\n"
    "When the transcript is labelled with speakers, name who raised each point "
    "and who owns each action."
)

# Transcripts longer than this are summarised in stages so nothing is dropped
# from the middle of a two-hour lecture.
CHUNK_CHARS = 14_000

# Notes shorter than this share of the transcript are treated as too thin.
# Speech is repetitive, so half its length in notes is already exhaustive.
NOTES_RATIO = 0.45
NOTES_MIN_CHARS = 1_500
NOTES_MAX_CHARS = 14_000
# A first draft shorter than this share of the target gets a second pass
# that walks the transcript again and adds what was left out.
EXPAND_BELOW = 0.85


def _target_length(transcript_chars):
    return max(NOTES_MIN_CHARS, min(NOTES_MAX_CHARS, int(transcript_chars * NOTES_RATIO)))


def _length_rule(transcript_chars):
    target = _target_length(transcript_chars)
    return (f"LENGTH: the transcript is about {transcript_chars:,} characters. Write "
            f"at least {target:,} characters of notes. Notes shorter than that have "
            "left material out; when unsure, include the detail.")


def _expand_notes(notes, transcript, shape, system, target, model=None):
    """Second pass when the first came back thin: add what was left out."""
    return _strip_fences(_chat(
        [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": (
                    "These notes are too short for the transcript: they are "
                    f"{len(notes):,} characters and should be at least {target:,}. "
                    "Go through the transcript from start to end and add every "
                    "point, definition, example, figure and remark that the notes "
                    "do not yet contain, in the right section. Keep everything "
                    "already there. Return the complete, expanded notes.\n\n"
                    f"{shape}\n\nCurrent notes:\n{notes}\n\nTranscript:\n{transcript}"
                ),
            },
        ],
        model=model or SUMMARY_MODEL,
        max_tokens=12000,
    ))


def _strip_fences(text):
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    return text.strip()


def _split_transcript(text, size=CHUNK_CHARS):
    parts, buf = [], []
    used = 0
    for line in text.replace("\r", "").split("\n"):
        # Long unbroken transcripts have no newlines; cut on sentence ends.
        pieces = re.split(r"(?<=[.!?。])\s+", line) if len(line) > size else [line]
        for piece in pieces:
            if used + len(piece) > size and buf:
                parts.append("\n".join(buf))
                buf, used = [], 0
            buf.append(piece)
            used += len(piece) + 1
    if buf:
        parts.append("\n".join(buf))
    return parts


def _notes_for_chunk(chunk, index, total, kind, lang, model=None):
    """Detailed notes for one slice of a long transcript."""
    shape = LECTURE_SHAPE if kind == "lecture" else MEETING_SHAPE
    system = SUMMARY_SYSTEM.format(language=_language_rule(lang))
    notes = _strip_fences(_chat(
        [
            {"role": "system", "content": system},
            {
                "role": "user",
                "content": (
                    f"This is part {index} of {total} of one recording. Write complete "
                    f"notes for THIS PART only.\n\n{shape}\n\n{_length_rule(len(chunk))}\n\n"
                    f"Transcript part {index}:\n{chunk}"
                ),
            },
        ],
        model=model or SUMMARY_MODEL,
        max_tokens=8000,
    ))
    target = _target_length(len(chunk))
    if len(notes) < target * EXPAND_BELOW:
        notes = _expand_notes(notes, chunk, shape, system, target, model=model)
    return notes


def _title_and_keywords(notes, transcript, lang):
    """A name and 8-15 terms, from the finished notes (cheap model)."""
    raw = _chat(
        [
            {
                "role": "system",
                "content": (
                    "You label study notes. Return JSON with exactly these keys:\n"
                    '  "title": a short specific name for this recording, 3-7 words\n'
                    '  "keywords": 8-15 terms that actually appear in the notes and '
                    "that a listener might want explained, most important first.\n"
                    + _language_rule(lang)
                ),
            },
            {"role": "user", "content": f"Notes:\n{notes[:8000]}\n\nStart of transcript:\n{transcript[:2000]}"},
        ],
        response_format={"type": "json_object"},
        max_tokens=400,
    )
    try:
        data = json.loads(raw)
    except ValueError:
        data = {}
    return _as_text(data.get("title")), _as_keywords(data.get("keywords"))


SLIDES_RULE = (
    "Slides for this session are attached after the transcript. The transcript "
    "is still the source of what was said; use the slides to get terms, "
    "formulas, figures and headings exactly right, and to fill in anything the "
    "speaker referred to on screen without reading aloud. Mark items that "
    "appear only on the slides with '(slides)'."
)


def summarize(transcript, kind="lecture", lang=None, slides=None, model=None, premium=True):
    """Return {title, summary (markdown), keywords: [...], language} for a transcript.

    Short recordings go to the model in one pass. Long ones are summarised
    part by part first, then merged, so the notes stay detailed throughout
    rather than fading after the first twenty minutes. The notes come back
    as plain markdown (long JSON strings truncate and mis-escape); a second,
    cheap call names the session and picks the keywords.
    """
    detected = detect_language(transcript)
    lang = lang or detected
    model = model or (SUMMARY_MODEL if premium else CHAT_MODEL)
    shape = LECTURE_SHAPE if kind == "lecture" else MEETING_SHAPE
    chunks = _split_transcript(transcript)
    system = SUMMARY_SYSTEM.format(language=_language_rule(lang))

    if len(chunks) > 1:
        partials = [
            _notes_for_chunk(chunk, i, len(chunks), kind, lang, model=model)
            for i, chunk in enumerate(chunks, start=1)
        ]
        source = "\n\n---\n\n".join(
            f"PART {i}\n{p}" for i, p in enumerate(partials, start=1)
        )
        total_notes = sum(len(p) for p in partials)
        task = (
            "Merge these part-by-part notes into ONE set of notes for the whole "
            "recording. Keep every concept, example, figure and action item; "
            "combine only true duplicates; order by topic rather than by part. "
            f"The merged notes must be at least {int(total_notes * 0.8):,} "
            "characters: merging is reorganising, not shortening."
        )
        prompt = f"{task}\n\n{shape}\n\nNotes for each part, in order:\n{source}"
    else:
        prompt = (
            f"Write the notes for this recording.\n\n{shape}\n\n"
            f"{_length_rule(len(transcript))}\n\nTranscript:\n{transcript}"
        )
    if slides:
        prompt += f"\n\n{SLIDES_RULE}\n\nSlides:\n{slides[:40_000]}"

    notes = _strip_fences(_chat(
        [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        model=model,
        max_tokens=12000,
    ))
    if len(chunks) == 1 and premium:
        target = _target_length(len(transcript))
        if len(notes) < target * EXPAND_BELOW:
            notes = _expand_notes(notes, transcript, shape, system, target, model=model)
    notes, trailing = _split_keyword_section(notes)

    title, keywords = _title_and_keywords(notes, transcript, lang)
    return {
        "title": title or "Untitled session",
        "summary": notes,
        "keywords": keywords or _as_keywords(trailing),
        "language": detected,
    }


EXAM_SHAPE = (
    "Sections, in this order, as '##' headings translated into the output language:\n"
    "1. 'Must-know definitions': every term with a one-line definition, "
    "bolded term first. Ten to twenty-five bullets.\n"
    "2. 'Formulas and numbers': every equation, procedure, figure, date and "
    "name, one per bullet. Skip if none.\n"
    "3. 'Easy to confuse': pairs or groups of concepts that are compared or "
    "often mixed up, with the distinguishing point.\n"
    "4. 'Likely exam questions': ten questions of the kind an instructor "
    "would set on this material, each with a concise model answer in a "
    "sub-bullet.\n"
    "5. 'One-minute recap': five to eight sentences that carry the whole "
    "session, for the walk into the exam."
)


def exam_sheet(notes, kind="lecture", lang=None):
    """A condensed cram sheet built from the full notes."""
    if kind != "lecture":
        return ""
    return _strip_fences(_chat(
        [
            {
                "role": "system",
                "content": (
                    "You compress study notes into a cram sheet for the night before "
                    "an exam. Work only from the notes given: no outside facts. Output "
                    "markdown only, headings and bullets, no preamble.\n\n"
                    + _language_rule(lang)
                ),
            },
            {"role": "user", "content": f"{EXAM_SHAPE}\n\nNotes:\n{notes}"},
        ],
        model=SUMMARY_MODEL,
        max_tokens=5000,
    ))


_KEYWORD_HEADINGS = ("keywords", "key terms", "핵심 용어", "키워드", "キーワード", "关键词")


def _split_keyword_section(markdown):
    """Cut a trailing 'Keywords' section off the notes.

    The model sometimes appends the keyword list to the notes. Returns
    (notes, keyword_text_or_empty).
    """
    match = re.search(r"\n#{1,4}\s*(%s)\s*:?\s*\n" % "|".join(re.escape(h) for h in _KEYWORD_HEADINGS),
                      "\n" + markdown, re.I)
    if not match:
        return markdown, ""
    cut = match.start()
    head = ("\n" + markdown)[:cut].strip()
    tail = ("\n" + markdown)[match.end():]
    # Only treat it as the keyword list if nothing else follows it.
    if re.search(r"\n#{1,4}\s", tail):
        return markdown, ""
    tail = re.sub(r"^[-*]\s*", "", tail.strip(), flags=re.M).replace("\n", ", ")
    return head, tail


def _as_text(value):
    return value.strip() if isinstance(value, str) else ""


def _as_markdown(value, depth=2):
    """Flatten whatever shape the model returned into markdown.

    Asking for a markdown string usually works, but the model sometimes nests
    the sections as objects or arrays instead. Rather than fail the request,
    render those into the same markdown the UI already knows how to display.
    """
    if isinstance(value, str):
        return value.strip()

    if isinstance(value, list):
        parts = []
        for item in value:
            if isinstance(item, str):
                parts.append(f"- {item.strip()}")
            else:
                parts.append(_as_markdown(item, depth))
        return "\n".join(p for p in parts if p)

    if isinstance(value, dict):
        # A {heading, points} shaped section, in any of its common spellings.
        heading = value.get("heading") or value.get("title") or value.get("section")
        body = value.get("points") or value.get("bullets") or value.get("content")
        if heading and body is not None:
            return f"{'#' * depth} {heading}\n{_as_markdown(body, depth + 1)}"

        parts = []
        for key, item in value.items():
            # The model sometimes folds the whole response object into the
            # summary field. Drop the sibling keys so they do not surface as
            # empty "keywords" / "title" sections in the notes.
            if key.lower() in {"keywords", "keyword", "title", "tags"}:
                continue
            rendered = _as_markdown(item, depth + 1)
            if rendered.strip():
                parts.append(f"{'#' * depth} {key}\n{rendered}")
        return "\n\n".join(parts)

    return ""


def _as_keywords(value):
    if isinstance(value, str):
        value = [v for v in value.split(",")]
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if isinstance(item, dict):
            item = item.get("term") or item.get("keyword") or item.get("name") or ""
        text = str(item).strip()
        if text and text not in out:
            out.append(text)
    return out[:15]


def explain_keyword(keyword, transcript, lang=None):
    """Explain one term, anchored to how the speaker actually used it."""
    return _chat(
        [
            {
                "role": "system",
                "content": (
                    "You explain a term to someone who just heard it in a lecture or "
                    "meeting. Lead with how it was used in this transcript, then add "
                    "the background needed to make sense of it. Be clear that the "
                    "background is context you are adding. Keep it under 200 words.\n\n"
                    + _language_rule(lang)
                ),
            },
            {
                "role": "user",
                "content": f'Term: "{keyword}"\n\nTranscript:\n{transcript}',
            },
        ],
        max_tokens=400,
    )


# ---------------------------------------------------------------------- chat

CHAT_SYSTEM = (
    "You answer questions about ONE transcript, given below. That transcript is "
    "your only source.\n\n"
    "Rules:\n"
    "1. If the transcript answers the question, answer from it and quote the "
    "relevant line.\n"
    "2. If it does not, say so plainly in ONE short sentence, in the "
    "transcript's language, and stop. Do not answer from general knowledge, "
    "and do not add a translation or a second sentence in another language.\n"
    "3. If the transcript touches the topic only partly, answer what it covers "
    "and say what it leaves out.\n"
    "4. Speech recognition makes mistakes. If a passage looks garbled, say what "
    "you think was meant rather than treating it as fact.\n"
    "5. {language} If the question is clearly asked in a different language "
    "from the transcript, answer in the question's language instead. A "
    "technical term written in English does not make the answer English.\n\n"
    "TRANSCRIPT:\n{transcript}"
)


def answer(question, transcript, history=None, lang=None, slides=None):
    if not transcript.strip() and not slides:
        return "There is no transcript for this session yet, so there is nothing to answer from."

    rule = (f"Answer in {language_name(lang)}."
            if lang else "Answer in the language the transcript is spoken in.")
    source = transcript
    if slides:
        source = (f"{transcript}\n\n--- SLIDES (the lecture's own material; treat as part of "
                  f"the session and say 'the slides' when citing them) ---\n{slides[:40_000]}")
    messages = [{"role": "system", "content": CHAT_SYSTEM.format(transcript=source, language=rule)}]
    for turn in (history or [])[-10:]:
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": question})
    return _chat(messages, max_tokens=700)


# --------------------------------------------------------------- folder chat

FOLDER_SYSTEM = (
    "You answer questions about a FOLDER of related recordings — for example "
    "every lecture of one course. The material below is your only source.\n\n"
    "Rules:\n"
    "1. Answer only from the material. Quote the relevant line, and say which "
    "recording (by its title) it came from.\n"
    "2. If several recordings touch the topic, bring them together and note "
    "how the later one built on or changed the earlier one.\n"
    "3. If nothing in the material covers the question, say so in ONE short "
    "sentence in the material's language and stop. Do not add general knowledge.\n"
    "4. Speech recognition makes mistakes; read through obvious errors.\n"
    "5. Answer in the language the material is spoken in, unless the question "
    "is clearly in another language. These instructions being in English must "
    "not affect your output language.\n\n"
    "MATERIAL:\n{material}"
)

_STOP = set("the a an of to in is are was were and or for on at by with that this "
            "그 이 저 것 수 등 및 또 더 좀 는 은 이 가 을 를 에 의 로 와 과".split())


def _terms(text):
    return {t for t in re.findall(r"[\w가-힣]{2,}", (text or "").lower()) if t not in _STOP}


def build_folder_material(question, corpus, max_chars=40_000, top_segments=45):
    """Assemble what the folder chat gets to see.

    Every session's summary goes in (they are short and give the shape of the
    course). Then the segments most related to the question, from any session,
    ranked by shared terms — so a folder of twelve lectures still fits in the
    model's window and the answer can cite the exact line.
    """
    q_terms = _terms(question)
    parts, used = [], 0

    for s in corpus:
        block = f"### {s['title']} ({s['date']})\n{s['summary'].strip() or '(요약 없음)'}\n"
        parts.append(block)
        used += len(block)

    scored = []
    for s in corpus:
        for text in s["segments"]:
            overlap = len(q_terms & _terms(text))
            if overlap:
                scored.append((overlap, s["title"], text))
    scored.sort(key=lambda x: -x[0])

    if scored:
        parts.append("\n### 관련 구절\n")
        for _, title, text in scored[:top_segments]:
            line = f"[{title}] {text}\n"
            if used + len(line) > max_chars:
                break
            parts.append(line)
            used += len(line)

    return "".join(parts)


def answer_folder(question, corpus, history=None):
    if not any(s["segments"] for s in corpus):
        return "이 폴더에는 아직 받아적은 내용이 없어서 답할 근거가 없습니다."
    material = build_folder_material(question, corpus)
    messages = [{"role": "system", "content": FOLDER_SYSTEM.format(material=material)}]
    for turn in (history or [])[-10:]:
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": question})
    return _chat(messages, max_tokens=900)


# --------------------------------------------------------------- translation

def translate_segments(segments, target):
    """Translate {id: text} pairs into `target`. Returns {id: translation}.

    One call per batch: the segments are numbered and the model returns the
    same numbers, so a dropped line cannot shift every translation after it.
    """
    if not segments:
        return {}
    numbered = "\n".join(f"[{i}] {text}" for i, text in segments.items())
    raw = _chat(
        [
            {
                "role": "system",
                "content": (
                    f"Translate each numbered line into {language_name(target)}. "
                    "These are lines of a live transcript, so keep them faithful "
                    "and natural; do not merge, drop or reorder lines. Return JSON: "
                    '{"translations": {"<number>": "<translation>", ...}} with '
                    "every number present."
                ),
            },
            {"role": "user", "content": numbered},
        ],
        response_format={"type": "json_object"},
        max_tokens=4000,
        temperature=0.2,
    )
    data = json.loads(raw).get("translations") or {}
    out = {}
    for key, value in data.items():
        try:
            seg_id = int(str(key).strip("[] "))
        except ValueError:
            continue
        if seg_id in segments and isinstance(value, str) and value.strip():
            out[seg_id] = value.strip()
    return out


# ------------------------------------------------------------------- helpers

def looks_like_url(text):
    return bool(re.match(r"^https?://\S+$", (text or "").strip()))
