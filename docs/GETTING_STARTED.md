# Getting started (no experience needed)

This page walks you from "I just downloaded it" to "she says hi", on Windows, macOS or Linux.
Pick the path that matches what you have. You can always add the other parts later.

## Words you will see

| word | plain meaning |
| --- | --- |
| **terminal** | a window where you type commands. Windows: *PowerShell*/*Command Prompt* (Start menu). macOS: *Terminal* (Spotlight). Linux: your terminal app. |
| **Python** | the programming language Sakura is written in. You install it once. Use version **3.11** (3.12 also works). |
| **LLM / "brain"** | the AI model that writes her replies. It can run in the cloud (OpenRouter) or on your own computer (Ollama, llama.cpp). |
| **STT** | speech-to-text: her *ears*. |
| **TTS** | text-to-speech: her *voice*. |
| **GPU** | your graphics card. Faster for AI. NVIDIA is best supported; Apple Silicon and CPU also work. |
| **VRAM** | the graphics card's own memory. AI models must fit in it. |

## The easy way: the first-run wizard

Install Python (Step 0 below), then **run `Install_OpenSakura.bat` (regardless of OS), or open a terminal in the project folder and run:

```shell
python scripts/oobe.py
```

It looks at your computer (RAM, graphics card, free disk), recommends one of the three paths below, asks which brain you want,
runs the installers for you, writes the settings and checks that everything works. Add `--dry-run` to see what it would do
first. It never accepts her voice licence for you. If you prefer to do it by hand, follow the steps below.

## Step 0: what you need

- Python **3.11** (3.12 also works; other versions do not). [python.org](https://www.python.org/downloads/release/python-3119/)
  (on Windows tick *Add python.exe to PATH*), or `winget install Python.Python.3.11`; macOS: `brew install python@3.11`;
  Ubuntu/Debian: `sudo apt install python3.11 python3.11-venv`. Already have a different Python? Keep it: install 3.11
  beside it (`uv python install 3.11` works everywhere) and the setup finds the right one.
- [Git](https://git-scm.com/downloads), or download the project as a ZIP and unzip it
- A modern browser (Chrome, Edge, Firefox, Safari)
- (if you are <18 years old) Time, grow older become adult :p

## Step 1: choose your path

| I want ... | path | needs | cost |
| --- | --- | --- | --- |
| to **try her quickly**, text chat + 3D avatar | **A. Text only** | any computer, no GPU | a few cents of OpenRouter credit |
| **everything free** on my own computer, text | **B. Local brain** | 8 GB+ RAM (a GPU helps) | free |
| to **talk out loud** with her voice | **C. Full voice** | NVIDIA GPU best; Mac or CPU works but slower | OpenRouter credit or free local brain |

### Path A: text only

1. Open a terminal in the project folder (the one containing `launch.py`).
2. Run the setup:
   `python scripts/setup.py --text-only` OR
   - Windows: `setup.bat --text-only`
   - macOS / Linux: `bash scripts/setup.bat --text-only`
3. Get a brain: make an account at [openrouter.ai](https://openrouter.ai), add a few dollars, create a key
   (openrouter.ai/keys). Open the file named `.env` in the project folder with any text editor and paste the key after
   `OPENROUTER_API_KEY=`. Save.
4. Start her:
   `Start_OpenSakura.bat --text-only` (or `./Start_OpenSakura.bat`, its the same)
5. Your browser opens **http://localhost:8010**. Type a message in the **Play** tab.

### Path B: a free brain on your computer

Do Path A steps 1, 2, 4, 5, but for step 3 choose one:

- **Ollama** (easiest): install from [ollama.com](https://ollama.com/download), then in a terminal `ollama pull qwen3:4b`
  (small, for 8 GB machines) or `qwen3:8b` (better, needs ~16 GB). In the page open **Setup -> LLM**, set every role's
  provider to **Ollama**, type the model name, press **Save LLM**.
- **llama.cpp or LM Studio**: follow [OPENAI_COMPATIBLE.md](OPENAI_COMPATIBLE.md).

No NVIDIA card? Both work on CPU, Apple GPUs and many AMD/Intel GPUs (replies are just slower on CPU). If replies feel
very slow, pick a smaller model (4B).

### Path C: full voice

1. Run the full setup (it downloads about 12 GB, so use a good connection):
   `python scripts/setup.py` OR
   - Windows: `setup.bat`
   - macOS / Linux: `bash scripts/setup.bat`

   It detects your hardware (NVIDIA, AMD on Linux, Apple Silicon, or CPU) and prints the exact next commands.
2. Install her voice, as the setup output says. Typical:
   - NVIDIA or Mac: `python scripts/setup_tts.py`
   - anything else: `python scripts/setup_tts.py --device cpu`

   You will be shown her personal-use licence; type `yes` to accept.
3. Give her a brain (Path A step 3, or Path B).
4. Check, then start:
   - Windows: `.venv\Scripts\python launch.py --check` then `Start_OpenSakura.bat`
   - macOS / Linux: `.venv/bin/python launch.py --check` then `./Start_OpenSakura.bat`
5. In the page click the microphone and talk, or hold **Space**.

## Step 2: checking that it works

- `launch.py --check` prints a list. `OK` is good, `!!` is something you must fix, `--` is only information.
- In the page, **Setup -> Health** shows green dots for what is running.

## If something goes wrong

| symptom | try |
| --- | --- |
| `OpenSakura needs Python 3.11 (recommended) or 3.12` | install Python 3.11 (Step 0), close and reopen the terminal, run the setup again |
| the page does not open | go to http://localhost:8010 yourself; check the terminal for red `!!` lines |
| "no API key" / she never answers | put your key in `.env` and restart, or pick a brain in **Setup -> LLM** |
| the setup stopped half-way | just run the same command again; finished steps are skipped |
| replies take forever | smaller model, or OpenRouter; the CPU is slow for big models |
| no sound | the page must be clicked once; **play voice** ticked; **Setup -> Health** TTS must be green |
| `Permission denied` on `.bat` | `bash Start_OpenSakura.bat` or `chmod +x Start_OpenSakura.bat scripts/setup.bat` |

More detail: the main [README](../README.md) (voice troubleshooting table) and [OPENROUTER.md](OPENROUTER.md).
