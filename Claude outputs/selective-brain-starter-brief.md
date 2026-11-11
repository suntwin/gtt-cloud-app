# Selective Brain — starter brief

**Goal:** help Siyonah prepare for the Victorian Selective Entry High School (SEHS) exam with a fixed daily routine that's driven by data and feels like a game. Small, regular sessions she runs on her own, with the plan decided ahead of time instead of on mood.

**Where the idea comes from:** my trading scanner (Streamlit + Supabase) turned random trading into a repeatable process. It works through a check-in before acting, a plan made the night before, tagging every outcome, a journal, and rules that stay fixed for weeks. Selective Brain applies the same structure to exam prep.

## First step: confirm the facts

- The current official exam format, sections, timing and test date (Victorian Department of Education, plus the test provider).
- Siyonah's year level, and how many weeks are left until the test.
- What prep material we already have (books, practice tests, tutoring).

Tutoring sites describe the test as **Reading, Mathematics, Verbal reasoning, Quantitative reasoning, and Writing (2 tasks)**. Verify this before building.

## How the trading process maps to exam prep

| Trading scanner | Selective Brain |
|---|---|
| Market note required before the scanner | A 1-minute check-in (energy, mood, time available) sets today's session size: Full / Normal / Light / Rest |
| Scanner with screens and columns | Question log: section, topic, question type, right/wrong, seconds taken, difficulty |
| Tomorrow's list, made the night before | Tomorrow's practice set, auto-suggested from the data (weak-but-improving topics first), confirmed together |
| Tagging each breakout | Every wrong answer tagged by why: concept gap, careless slip, ran out of time, misread question |
| Rel Wk Dist gate (don't chase extended) | Harder questions only after a topic is above about 80% accuracy |
| Clean-up | Mastered topics retired, with spaced-repetition reviews so they stick |
| Buy signal review | "Did yesterday's plan happen? Did it work?" |
| Journal | Her own short notes: what clicked, what was hard |
| Rules fixed for 8 weeks | The routine stays stable; change it only when the data says so |

## Making it a game (she owns it)

- Streaks for days with a completed session, with a "rest day" allowed so a streak isn't broken by a planned break.
- A topic map that turns from red to amber to green as accuracy rises.
- XP and levels for completed sessions and for fixing tagged mistakes, not just for right answers.
- Badges such as "5 careless slips fixed", "Beat my time on Quant", "7-day streak".
- A personal-best chart for time per question.
- A weekly 15-minute review together. No daily parent monitoring.

## Tech

- Streamlit on Streamlit Community Cloud, with a new GitHub repo.
- Supabase (new project or new tables) with Row Level Security; keys kept in Streamlit Secrets, never in code.
- Phone/iPad first: big buttons, few taps.

## Version 1 (keep it small)

1. Daily check-in, which sets the session size.
2. Log a practice session: section, topic, number right out of number attempted, time, and why for each wrong answer.
3. Dashboard: streak, topic map, accuracy and time trend per section, top 3 mistake reasons.
4. Tomorrow's plan: suggested topics plus confirm.
5. Weekly review page.

**Later:** an in-app question bank or timed mini-tests, full mock-exam tracking, a writing-task rubric with self-scoring, and badges.

## Open questions

- Questions: enter scores from books or practice tests, or build an in-app question bank?
- Does she use an iPad, phone or laptop?
- Logins: one shared login, or separate parent and child views?
