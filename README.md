<p align="center">
  <img src="static/images/story/hero.png" alt="A child in headphones writing in a notebook — watercolor" width="260">
</p>

<h1 align="center">TranscriptoAI</h1>
<p align="center"><strong>While you listen, the notes write themselves.</strong><br>
An AI notebook for lectures and meetings that transcribes, summarizes, explains the hard words —
and answers <em>only</em> from what was actually said.</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#what-it-does">What it does</a> ·
  <a href="#how-it-is-built">How it's built</a> ·
  <a href="#deploy">Deploy</a> ·
  <a href="#한국어">한국어</a>
</p>

---

## Why this exists

Most AI note apps will happily answer questions with facts that were never in the lecture.
That is worse than useless for studying — it is confidently wrong. TranscriptoAI treats the
transcript as the **only** source of truth:

> **You:** What did they say about BERT today?
> **AI:** That wasn't covered in today's class.

Every answer quotes the line it came from. Every summary is built from the words in the room.

## What it does

| | |
|---|---|
| **Five ways in** | Computer audio (Zoom / YouTube tab), the room (mic), both mixed, a pasted link (YouTube · TED), or an uploaded recording (Zoom / Teams local files) |
| **Live captions** | Audio is cut into ~6 s clips and transcribed as it comes; the running transcript primes the next clip so terminology stays consistent |
| **Study notes, not blurbs** | Lecture notes are held to a length floor (about half the transcript) and a second pass adds anything the first draft skipped. Every concept as the speaker defined it, every example, formulas, what was emphasised, self-test questions. Long recordings are summarised part by part and merged. Meetings get decisions, owners and open questions |
| **Exam sheet** | A second, condensed view of the same session: must-know definitions, formulas and numbers, easy-to-confuse pairs, ten likely exam questions with answers, a one-minute recap |
| **Slides beside the notes** | Attach the lecture's PDF: it opens in a panel next to the transcript, and its text is fed to the notes so terms and formulas match the slides |
| **Replay any line** | Recordings are kept; tap a transcript line to hear that moment again, with the highlight following playback |
| **Live translation** | Turn on a second line under each sentence in Korean, English, Japanese and more; cached per line |
| **Key terms** | Extracted from the transcript; tap one for an explanation that starts with how *this* speaker used it. Saved, so it is instant next time |
| **Grounded chat** | Answers only from the transcript, with quotes. Off-topic questions are refused in one sentence |
| **Folders** | Group a course's lectures and ask across all of them — "How did weeks 3 and 5 explain scheduling differently?" — with the source lecture named |
| **Speaker separation** | For uploaded meetings: who spoke, how much, and a summary that says who committed to what |
| **Plans, credits, codes** | Free 2 h / Student 20 h / Pro 45 h per month plus one-time credit packs that never expire, metered on transcribed audio only. Stripe Checkout for subscriptions and packs, webhook-driven fulfilment, billing history on the account page. Promo codes: percentage or fixed discounts floored so no sale drops below cost, and comp codes that grant a plan outright for the owner and reviewers |
| **Two languages** | English by default, Korean with one click — UI, toasts, and API errors alike. The recording's language is detected once and every AI output is written in it explicitly |

Audio is never stored: it is deleted the moment it becomes text.

## Quick start

Requires Python 3.12+ and `ffmpeg` (`brew install ffmpeg`).

```bash
git clone https://github.com/S2chloeS2/transcripto-ai && cd transcripto-ai
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # add OPENAI_API_KEY (required) and ASSEMBLYAI_API_KEY (recommended)
python check_keys.py            # confirms each key works and which engine will be used
python app.py                   # http://127.0.0.1:5001
```

For local development without Google OAuth, set `ALLOW_DEV_LOGIN=1` in `.env`; a test-account
button appears on the login page. It is refused outright when `FLASK_ENV=production`.

## How it is built

```
app.py        Flask routes, import pipeline, production guards, per-user rate limit
ai.py         OpenAI prompts — staged study notes, language detection, translation, grounding rules
engines.py    Transcription back ends behind one interface (AssemblyAI · Groq · OpenAI)
db.py         SQLite: users, sessions, segments, folders, keyword notes, usage
plans.py      Plans, credit packs, allowance and credit consumption, promo floor
billing.py    Stripe Checkout, webhook fulfilment, promo validation, comp codes
i18n.py       Korean-source → English translation table, per-visitor language
media.py      yt-dlp download (TED → YouTube fallback), ffmpeg split, SSRF guard
static/js/    capture.js (three audio sources, one interface) · session · folder · new
```

A few decisions worth knowing:

- **Per-clip recording** rather than one long stream: each clip carries its own container
  header, so the server can decode it alone and show text within seconds.
- **Engine selection by session type.** Lectures go to the cheapest engine; meetings go to
  AssemblyAI for speaker labels. Keys that are absent simply fall back to OpenAI.
- **Folder chat without embeddings.** Every summary in the folder goes in; then segments are
  ranked by term overlap with the question. A dozen lectures fit the context window and the
  answer can still quote the exact line.
- **Whisper hallucination filter.** Silence and music make Whisper invent phrases; the common
  ones are dropped before they reach a summary.
- **Ownership on every route.** A note that isn't yours returns 404, not 403 — a stranger
  can't learn it exists.

## Deploy

`Procfile` (gunicorn) and `render.yaml` are included — connect the repo on
[Render](https://render.com), add four secrets (`OPENAI_API_KEY`, `ASSEMBLYAI_API_KEY`,
`GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`), and register
`https://<your-domain>/auth/callback` in Google Cloud Console.

With `FLASK_ENV=production` the app refuses to start if dev login is enabled or a secret is
missing. Rate limiting, upload caps, private-network blocking, and secure cookies are always on.
`scripts/backup_db.sh` snapshots SQLite safely while running. Recordings are stored under
`AUDIO_DIR` (next to the database by default, `/var/data/audio` on Render's disk); on Render's
free tier there is no disk, so both are lost on redeploy. Notes use `SUMMARY_MODEL` (gpt-4o by
default); chat, keywords and translation stay on gpt-4o-mini.

**Payments.** Set `STRIPE_SECRET_KEY` and `STRIPE_WEBHOOK_SECRET` and point a Stripe webhook at
`/api/billing/webhook` (events: `checkout.session.completed`, `invoice.paid`,
`customer.subscription.deleted`). Without them a local build simulates purchases so the flow can
be tested, and production shows "payments not open yet". Promo codes are managed with
`scripts/promo.py` (`add CODE --percent 20`, `add CODE --comp pro --months 12`, `list`, `rm`).

## Status

Built as a course project at Columbia University (*Designing for Generative AI*, Fall 2024),
then rebuilt for real use in 2026. Development history, including the approaches that did not
work, lives in [`transcripto-ai-archive`](https://github.com/S2chloeS2/transcripto-ai-archive).

Illustrations are original watercolor-style images generated for this project.

---

## 한국어

**듣는 동안, 노트는 알아서 완성된다.** 강의와 회의를 받아적고, 요약하고, 낯선 용어를
풀어주며, **그 자리에서 나온 말에 대해서만** 답하는 AI 노트입니다.

- **다섯 가지 입력** — 컴퓨터 소리(Zoom·유튜브 탭) · 마이크 · 둘 다 · 링크(유튜브·TED) · 녹음 파일(Zoom·Teams)
- **근거 있는 챗** — 스크립트에 없는 질문엔 "오늘 다루지 않았습니다"로 거부, 있는 건 원문 인용
- **폴더 챗** — 과목 단위로 묶어 한 학기 강의 전체에 한꺼번에 질문, 어느 주차에서 나온 말인지 표시
- **화자 분리** — 업로드한 회의는 발언자별로 나뉘고, 요약이 "누가 무엇을 약속했는지"를 짚음
- **플랜·사용량** — 무료 5h / 스탠다드 25h / 프로 60h, 전사한 오디오 길이만 계량
- **영어 기본 · 한국어 전환** — 상단 버튼 한 번으로 화면·알림·API 오류까지 전부

실행은 위 *Quick start* 와 같습니다. 배포·백업·출시 전 체크리스트는 *Deploy* 절을 보세요.
녹음은 기록을 삭제할 때까지 보관되어, 스크립트의 문장을 누르면 그 부분부터 다시 들을 수 있습니다.
요약은 시험 대비 노트 수준으로 자세히 쓰고, 번역을 켜면 각 문장 아래에 한 줄씩 번역이 붙습니다.
